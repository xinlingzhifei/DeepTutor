from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.brief_builder import (
    KnowledgePointSpec,
    TeachingBriefBuilder,
    TeachingBriefSpec,
)
from deeptutor.teaching.contracts import (
    OutlineBundle,
    OutlineConfirmationMetadata,
    canonical_json_bytes,
    canonical_outline_sha256,
)
from deeptutor.teaching.dispatcher import OutboxDispatchConflict, OutboxDispatcher
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.models import (
    DataPlaneRoute,
    ProviderProfile,
    TeachingMetricCounterRollup,
    Tenant,
)
from deeptutor.teaching.models.classrooms import (
    BatchItem,
    BatchJob,
    ClassroomAsset,
    ClassroomDraft,
    TeachingBrief,
)
from deeptutor.teaching.models.jobs import (
    GenerationJob,
    GenerationQueue,
    GenerationSlot,
    OutboxMessage,
    TenantSchedulerState,
)
from deeptutor.teaching.models.tenant import Course, TeachingClass
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.repositories.classrooms import SqlAlchemyClassroomRepository
from deeptutor.teaching.repositories.jobs import (
    ContentRequeueConflict,
    GenerationJobRequest,
    IdempotencyConflict,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.repositories.metric_rollups import metric_rollup_shard
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.batches import (
    BatchPersistenceError,
    BatchReplayClassroomRequest,
    BatchReplayKnowledgePoint,
    BatchService,
    SqlAlchemyBatchJobGateway,
    SqlAlchemyBatchRepository,
)
from deeptutor.teaching.services.classrooms import ClassroomConfirmationConflict
from deeptutor.teaching.tenant_context import TenantContext
from tests.teaching_contract_fixtures import valid_outline_bundle

pytestmark = pytest.mark.usefixtures("clean_generation_runtime_state")


def _request(tenant_id: str, job_id: str) -> GenerationJobRequest:
    return GenerationJobRequest(
        tenant_id=tenant_id,
        job_id=job_id,
        job_kind="generation",
        phase="outline",
        export_format=None,
        priority="teacher",
        quota_units=3,
        actor_id="teacher-1",
        owner_id="teacher-1",
        visibility="class",
        request_id=f"request-{job_id}",
        idempotency_key=f"idempotency-{job_id}",
        classroom_draft_id="draft-1",
        batch_id=None,
        request_sha256=hashlib.sha256(b"{}").hexdigest(),
        data_plane_mode="shared",
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        request_payload="{}",
    )


async def _insert_active_tenant(engine, tenant_id: str) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=tenant_id,
                    name=tenant_id,
                    status="active",
                    data_plane_mode="shared",
                )
            )
            await session.execute(
                insert(ProviderProfile)
                .values(
                    id="platform-default",
                    scope="shared",
                    tenant_id=None,
                    owner_key="shared",
                    provider_type="openai-compatible",
                    model_name="test-model",
                    api_base_url=None,
                    secret_ref="tests/shared/provider",
                    status="active",
                )
                .on_conflict_do_nothing(index_elements=[ProviderProfile.id])
            )
            await session.execute(
                insert(DataPlaneRoute)
                .values(
                    id="shared-primary",
                    tenant_id=None,
                    owner_key="shared",
                    mode="shared",
                    base_url="http://openmaic.invalid",
                    worker_pool="shared-generation",
                    queue_name="openmaic.shared",
                    provider_profile_id="platform-default",
                    status="active",
                    health_status="healthy",
                )
                .on_conflict_do_nothing(index_elements=[DataPlaneRoute.id])
            )


async def _queued_metric_total(session, shard: int) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(TeachingMetricCounterRollup.total, 0)).where(
                TeachingMetricCounterRollup.metric == "generation_jobs_total",
                TeachingMetricCounterRollup.category == "queued",
                TeachingMetricCounterRollup.shard == shard,
            )
        )
        or 0
    )


def test_job_quota_and_platform_outbox_roll_back_as_one_postgres_transaction(
    generation_database: Any,
) -> None:
    tenant_id = "atomic-tenant"
    job_id = "atomic-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            assert (
                await repository.grant_quota(
                    tenant_id,
                    grant_id="grant-atomic",
                    units=10,
                )
                == 10
            )
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        OutboxMessage(
                            event_id="preexisting-outline-event",
                            tenant_id=tenant_id,
                            job_id=job_id,
                            job_kind="generation",
                            phase="outline",
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            slot_pool="generation",
                            priority=300,
                            event_type="preexisting",
                            payload="preexisting",
                            delivered_at=datetime.now(UTC),
                        )
                    )

            with pytest.raises(IntegrityError):
                await repository.create_job_and_reserve(_request(tenant_id, job_id))

            assert await repository.get_job(tenant_id, job_id) is None
            assert await repository.quota_balance(tenant_id) == 10
            async with session_factory() as session:
                outbox_count = await session.scalar(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert outbox_count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_job_creation_rechecks_the_locked_control_plane_binding(
    generation_database: Any,
) -> None:
    tenant_id = "binding-create-tenant"
    job_id = "binding-create-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-binding-create",
                units=10,
            )
            with pytest.raises(DataPlaneBindingUnavailable):
                await repository.create_job_and_reserve(
                    replace(
                        _request(tenant_id, job_id),
                        worker_pool_ref="caller-forged-pool",
                    )
                )
            assert await repository.get_job(tenant_id, job_id) is None
            assert await repository.quota_balance(tenant_id) == 10
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_route_disable_commits_before_a_waiting_job_creation_can_write(
    generation_database: Any,
) -> None:
    tenant_id = "binding-create-race-tenant"
    job_id = "binding-create-race-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-binding-create-race",
                units=10,
            )
            create_task = None
            async with session_factory() as blocker:
                async with blocker.begin():
                    route = await blocker.scalar(
                        select(DataPlaneRoute)
                        .where(DataPlaneRoute.id == "shared-primary")
                        .with_for_update()
                    )
                    assert route is not None
                    create_task = asyncio.create_task(
                        repository.create_job_and_reserve(_request(tenant_id, job_id))
                    )
                    await asyncio.sleep(0.05)
                    assert not create_task.done()
                    route.status = "disabled"
            assert create_task is not None
            with pytest.raises(DataPlaneBindingUnavailable):
                await create_task
            assert await repository.get_job(tenant_id, job_id) is None
            assert await repository.quota_balance(tenant_id) == 10
        finally:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(DataPlaneRoute)
                        .where(DataPlaneRoute.id == "shared-primary")
                        .values(status="active")
                    )
            await engine.dispose()

    asyncio.run(scenario())


def test_skip_locked_dispatch_is_concurrent_and_queue_insertion_is_idempotent(
    generation_database: Any,
) -> None:
    tenant_id = "dispatch-tenant"
    job_id = "dispatch-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-dispatch",
                units=10,
            )
            request = _request(tenant_id, job_id)
            first_create, second_create = await asyncio.gather(
                repository.create_job_and_reserve(request),
                repository.create_job_and_reserve(request),
            )
            assert first_create.job_id == second_create.job_id == job_id
            assert await repository.quota_balance(tenant_id) == 7
            with pytest.raises(IdempotencyConflict):
                await repository.create_job_and_reserve(
                    replace(
                        request,
                        data_plane_route_id="other-route",
                    )
                )
            with pytest.raises(IdempotencyConflict):
                await repository.create_job_and_reserve(
                    replace(
                        request,
                        max_attempts=request.max_attempts + 1,
                    )
                )
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                        OutboxMessage.phase == "outline",
                    )
                )
                assert message is not None
                queued_shard = metric_rollup_shard(
                    message.event_id,
                    "generation_jobs_total",
                    "queued",
                )
                queued_before = await _queued_metric_total(session, queued_shard)
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(Tenant).where(Tenant.id == tenant_id).values(status="disabled")
                    )
                    assert (
                        await asyncio.wait_for(
                            OutboxDispatcher(engine).dispatch_next(),
                            timeout=2,
                        )
                        is None
                    )
            assert await OutboxDispatcher(engine).dispatch_next() is None
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(Tenant).where(Tenant.id == tenant_id).values(status="active")
                    )
            first, second = await asyncio.gather(
                OutboxDispatcher(engine).dispatch_next(),
                OutboxDispatcher(engine).dispatch_next(),
            )
            assert sum(result is not None for result in (first, second)) == 1
            async with session_factory() as session:
                async with session.begin():
                    queue_count = await session.scalar(
                        select(func.count())
                        .select_from(GenerationQueue)
                        .where(
                            GenerationQueue.tenant_id == tenant_id,
                            GenerationQueue.job_id == job_id,
                        )
                    )
                    assert queue_count == 1
                    await session.execute(
                        update(OutboxMessage)
                        .where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                            OutboxMessage.phase == "outline",
                        )
                        .values(delivered_at=None)
                    )

            async with session_factory() as session:
                queued_after_first = await _queued_metric_total(session, queued_shard)
                assert queued_after_first == queued_before + 1

            retried = await OutboxDispatcher(engine).dispatch_next()
            assert retried is not None
            async with session_factory() as session:
                queue_count = await session.scalar(
                    select(func.count())
                    .select_from(GenerationQueue)
                    .where(
                        GenerationQueue.tenant_id == tenant_id,
                        GenerationQueue.job_id == job_id,
                    )
                )
                assert queue_count == 1
                queued_after_repair = await _queued_metric_total(session, queued_shard)
                assert queued_after_repair == queued_after_first
            job = await repository.get_job(tenant_id, job_id)
            assert job is not None and job.status == "queued"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_dispatch_metric_rollup_failure_rolls_back_the_entire_queue_transition(
    generation_database: Any,
) -> None:
    tenant_id = "dispatch-metric-rollback-tenant"
    job_id = "dispatch-metric-rollback-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        translated_engine = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        tenant_sessions = async_sessionmaker(translated_engine, expire_on_commit=False)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-dispatch-metric-rollback",
                units=10,
            )
            await repository.create_job_and_reserve(_request(tenant_id, job_id))
            async with session_factory() as session:
                async with session.begin():
                    message = await session.scalar(
                        select(OutboxMessage).where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                        )
                    )
                    assert message is not None
                    queued_shard = metric_rollup_shard(
                        message.event_id,
                        "generation_jobs_total",
                        "queued",
                    )
                    queued_before = await _queued_metric_total(session, queued_shard)
                    await session.execute(
                        text(
                            """
                            CREATE FUNCTION platform.fail_dispatcher_queued_metric()
                            RETURNS trigger
                            LANGUAGE plpgsql
                            AS $$
                            BEGIN
                                IF NEW.metric = 'generation_jobs_total'
                                   AND NEW.category = 'queued' THEN
                                    RAISE EXCEPTION
                                        'injected dispatcher metric rollup failure';
                                END IF;
                                RETURN NEW;
                            END;
                            $$
                            """
                        )
                    )
                    await session.execute(
                        text(
                            """
                            CREATE TRIGGER fail_dispatcher_queued_metric
                            BEFORE INSERT OR UPDATE
                            ON platform.teaching_metric_counter_rollups
                            FOR EACH ROW
                            EXECUTE FUNCTION platform.fail_dispatcher_queued_metric()
                            """
                        )
                    )

            try:
                with pytest.raises(DBAPIError) as caught:
                    await OutboxDispatcher(engine).dispatch_next()
                assert "injected dispatcher metric rollup failure" in str(caught.value.orig)
            finally:
                async with session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                """
                                DROP TRIGGER fail_dispatcher_queued_metric
                                ON platform.teaching_metric_counter_rollups
                                """
                            )
                        )
                        await session.execute(
                            text(
                                """
                                DROP FUNCTION platform.fail_dispatcher_queued_metric()
                                """
                            )
                        )

            async with tenant_sessions() as session:
                job = await session.get(GenerationJob, job_id)
                assert job is not None and job.status == "quota_reserved"
            async with session_factory() as session:
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert message is not None and message.delivered_at is None
                assert await session.get(GenerationQueue, (tenant_id, job_id)) is None
                assert (
                    await session.get(
                        TenantSchedulerState,
                        (tenant_id, "shared-generation", "generation"),
                    )
                    is None
                )
                assert await _queued_metric_total(session, queued_shard) == queued_before
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_dispatch_uses_the_tenant_job_priority_instead_of_the_outbox_copy(
    generation_database: Any,
) -> None:
    tenant_id = "authoritative-priority-tenant"
    job_id = "authoritative-priority-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-authoritative-priority",
                units=10,
            )
            await repository.create_job_and_reserve(_request(tenant_id, job_id))
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(OutboxMessage)
                        .where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                        )
                        .values(priority=0)
                    )

            assert await OutboxDispatcher(engine).dispatch_next() is not None

            async with session_factory() as session:
                queue = await session.get(GenerationQueue, (tenant_id, job_id))
                assert queue is not None
                assert queue.priority == 300
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_dispatch_rejects_a_slot_pool_that_disagrees_with_the_tenant_job_shape(
    generation_database: Any,
) -> None:
    tenant_id = "slot-pool-drift-tenant"
    job_id = "slot-pool-drift-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        translated_engine = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        tenant_sessions = async_sessionmaker(translated_engine, expire_on_commit=False)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-slot-pool-drift",
                units=10,
            )
            await repository.create_job_and_reserve(_request(tenant_id, job_id))
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(OutboxMessage)
                        .where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                        )
                        .values(slot_pool="mp4_export")
                    )

            with pytest.raises(OutboxDispatchConflict):
                await OutboxDispatcher(engine).dispatch_next()

            async with tenant_sessions() as session:
                job = await session.get(GenerationJob, job_id)
                assert job is not None and job.status == "quota_reserved"
            async with session_factory() as session:
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert message is not None and message.delivered_at is None
                assert await session.get(GenerationQueue, (tenant_id, job_id)) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_already_queued_repair_rejects_a_drifted_slot_pool(
    generation_database: Any,
) -> None:
    tenant_id = "queued-slot-pool-drift-tenant"
    job_id = "queued-slot-pool-drift-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-queued-slot-pool-drift",
                units=10,
            )
            await repository.create_job_and_reserve(_request(tenant_id, job_id))
            assert await OutboxDispatcher(engine).dispatch_next() is not None
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(OutboxMessage)
                        .where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                        )
                        .values(delivered_at=None, slot_pool="mp4_export")
                    )

            with pytest.raises(OutboxDispatchConflict):
                await OutboxDispatcher(engine).dispatch_next()

            async with session_factory() as session:
                queue = await session.get(GenerationQueue, (tenant_id, job_id))
                assert queue is not None
                assert queue.slot_pool == "generation"
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert message is not None and message.delivered_at is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cancel_retires_outbox_and_dispatcher_consumes_terminal_stale_event(
    generation_database: Any,
) -> None:
    tenant_id = "cancel-outbox-tenant"
    job_id = "cancel-outbox-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-cancel-outbox",
                units=10,
            )
            await repository.create_job_and_reserve(_request(tenant_id, job_id))

            cancellation = await repository.request_cancel(tenant_id, job_id)

            assert cancellation is not None and not cancellation.running
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert message is not None and message.delivered_at is not None
                queued_shard = metric_rollup_shard(
                    message.event_id,
                    "generation_jobs_total",
                    "queued",
                )
                queued_before_terminal_repair = await _queued_metric_total(
                    session,
                    queued_shard,
                )
                assert (
                    await session.scalar(
                        select(GenerationQueue).where(
                            GenerationQueue.tenant_id == tenant_id,
                            GenerationQueue.job_id == job_id,
                        )
                    )
                    is None
                )

            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(OutboxMessage)
                        .where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                        )
                        .values(delivered_at=None)
                    )

            retired = await OutboxDispatcher(engine).dispatch_next()

            assert retired is not None
            async with session_factory() as session:
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert message is not None and message.delivered_at is not None
                assert (
                    await _queued_metric_total(session, queued_shard)
                    == queued_before_terminal_repair
                )
                assert (
                    await session.scalar(
                        select(GenerationQueue).where(
                            GenerationQueue.tenant_id == tenant_id,
                            GenerationQueue.job_id == job_id,
                        )
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_terminal_stale_outbox_is_not_retired_when_its_job_binding_drifted(
    generation_database: Any,
) -> None:
    tenant_id = "terminal-binding-drift-tenant"
    job_id = "terminal-binding-drift-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-terminal-binding-drift",
                units=10,
            )
            await repository.create_job_and_reserve(_request(tenant_id, job_id))
            cancellation = await repository.request_cancel(tenant_id, job_id)
            assert cancellation is not None and not cancellation.running
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(OutboxMessage)
                        .where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                        )
                        .values(
                            delivered_at=None,
                            worker_pool_ref="drifted-worker-pool",
                        )
                    )

            with pytest.raises(OutboxDispatchConflict):
                await OutboxDispatcher(engine).dispatch_next()

            async with session_factory() as session:
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert message is not None and message.delivered_at is None
                assert await session.get(GenerationQueue, (tenant_id, job_id)) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_dispatch_conflict_does_not_record_a_queued_lifecycle_entry(
    generation_database: Any,
) -> None:
    tenant_id = "dispatch-conflict-tenant"
    job_id = "dispatch-conflict-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(
                tenant_id,
                grant_id="grant-dispatch-conflict",
                units=10,
            )
            await repository.create_job_and_reserve(_request(tenant_id, job_id))
            async with session_factory() as session:
                async with session.begin():
                    message = await session.scalar(
                        select(OutboxMessage)
                        .where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                        )
                        .with_for_update()
                    )
                    assert message is not None
                    message.worker_pool_ref = "mismatched-worker-pool"

            async with session_factory() as session:
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert message is not None
                queued_shard = metric_rollup_shard(
                    message.event_id,
                    "generation_jobs_total",
                    "queued",
                )
                queued_before = await _queued_metric_total(session, queued_shard)

            with pytest.raises(OutboxDispatchConflict):
                await OutboxDispatcher(engine).dispatch_next()

            async with session_factory() as session:
                assert await _queued_metric_total(session, queued_shard) == queued_before
                message = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == tenant_id,
                        OutboxMessage.job_id == job_id,
                    )
                )
                assert message is not None and message.delivered_at is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_unstarted_only_cancel_does_not_cancel_an_outline_awaiting_confirmation(
    generation_database: Any,
) -> None:
    tenant_id = "batch-confirmation-tenant"
    job_id = "batch-confirmation-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _insert_active_tenant(engine, tenant_id)
            translated_engine = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            session_factory = async_sessionmaker(translated_engine, expire_on_commit=False)
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        GenerationJob(
                            id=job_id,
                            tenant_id=tenant_id,
                            job_kind="generation",
                            phase="outline",
                            export_format=None,
                            status="awaiting_confirmation",
                            priority=100,
                            quota_units=1,
                            actor_id="author-1",
                            owner_id="author-1",
                            visibility="class",
                            request_id="batch-confirmation-request",
                            idempotency_key="batch-confirmation-idempotency",
                            classroom_draft_id="draft-1",
                            batch_id="batch-1",
                            request_sha256="c" * 64,
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            request_payload="{}",
                        )
                    )

            repository = SqlAlchemyGenerationJobRepository(engine)
            cancellation = await repository.request_cancel(
                tenant_id,
                job_id,
                only_if_unstarted=True,
            )

            assert cancellation is None
            details = await repository.get_job_details(tenant_id, job_id)
            assert details is not None
            assert details.status == "awaiting_confirmation"
            assert details.cancel_requested is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_rejected_batch_jobs_are_terminal_replayable_and_tenant_bound(
    generation_database: Any,
) -> None:
    tenant_id = "batch-rejected-tenant"
    other_tenant_id = "batch-rejected-other"
    batch_id = f"batch-{'a' * 20}-{'b' * 32}"
    generation_database.migrate_tenant(tenant_id)
    generation_database.migrate_tenant(other_tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _insert_active_tenant(engine, tenant_id)
            await _insert_active_tenant(engine, other_tenant_id)
            context = TenantContext(
                tenant_id=tenant_id,
                schema_name=tenant_schema_name(tenant_id),
                user_id="author-1",
                permissions=permissions_for_roles(
                    {"content_author"},
                    scope_type="tenant",
                    scope_id=tenant_id,
                ),
            )
            request = BatchReplayClassroomRequest(
                title="Rejected classroom",
                course_id="course-a",
                class_id="class-a",
                objective="Explain motion",
                grade_band="grade-8",
                audience="intermediate",
                duration_minutes=45,
                classroom_mode="full",
                web_policy="disabled",
                allowed_web_domains=(),
                template_id="template-a",
                template_version="1",
                knowledge_points=(
                    BatchReplayKnowledgePoint(
                        knowledge_point_id="kp-motion",
                        title="Motion",
                        description="Describe displacement and velocity",
                    ),
                ),
                content_mode="open_creation",
                open_creation_acknowledged=True,
                source_type=None,
                source_ref=None,
                requested_exports=("classroom_zip",),
            )
            batch_repository = SqlAlchemyBatchRepository(engine, tenant_id)
            other_batch_repository = SqlAlchemyBatchRepository(engine, other_tenant_id)
            job_repository = SqlAlchemyGenerationJobRepository(engine)
            jobs = SqlAlchemyBatchJobGateway(job_repository)
            await batch_repository.create(batch_id, context.user_id, ("item-a",))
            await other_batch_repository.create(batch_id, "author-2", ("item-a",))

            first_job_id = await jobs.record_rejected(
                context,
                batch_id=batch_id,
                item_id="item-a",
                request=request,
            )
            first_item = await batch_repository.bind_rejected_item(
                batch_id,
                "item-a",
                generation_job_id=first_job_id,
            )

            assert first_item.status == "failed"
            assert first_item.generation_job_id == first_job_id
            assert first_item.classroom_draft_id is None
            details = await job_repository.get_job_details(tenant_id, first_job_id)
            assert details is not None
            assert details.status == "failed"
            assert details.priority == 100
            assert details.classroom_draft_id is None
            assert details.error_category == "request"
            assert details.error_code == "batch_item_rejected"
            assert details.retry_of_job_id is None
            projected = await batch_repository.get(batch_id)
            assert projected is not None
            assert projected.items[0].resource_course_id == "course-a"
            assert projected.items[0].resource_class_id == "class-a"
            access = BatchService(batch_repository, object(), object())
            assert await access.get(context, batch_id) is not None
            assert (
                await access.get(
                    replace(context, permissions=frozenset()),
                    batch_id,
                )
                is None
            )
            assert await jobs.rejected_input(context, job_id=first_job_id) == request

            with pytest.raises(BatchPersistenceError, match="unavailable"):
                await other_batch_repository.bind_rejected_item(
                    batch_id,
                    "item-a",
                    generation_job_id=first_job_id,
                )

            replay = await jobs.rejected_input(context, job_id=first_job_id)
            second_job_id = await jobs.record_rejected(
                context,
                batch_id=batch_id,
                item_id="item-a",
                request=replay,
                retry_of_job_id=first_job_id,
            )
            second_item = await batch_repository.rebind_failed_item(
                batch_id,
                "item-a",
                expected_job_id=first_job_id,
                new_job_id=second_job_id,
            )
            second_details = await job_repository.get_job_details(
                tenant_id,
                second_job_id,
            )

            assert second_item.status == "failed"
            assert second_item.generation_job_id == second_job_id
            assert second_item.classroom_draft_id is None
            assert second_details is not None
            assert second_details.status == "failed"
            assert second_details.retry_of_job_id == first_job_id
            session_factory = async_sessionmaker(
                engine.execution_options(
                    schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
                ),
                expire_on_commit=False,
            )
            async with session_factory() as session:
                queued = await session.scalar(
                    select(func.count())
                    .select_from(GenerationQueue)
                    .where(GenerationQueue.job_id.in_((first_job_id, second_job_id)))
                )
                outboxed = await session.scalar(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(OutboxMessage.job_id.in_((first_job_id, second_job_id)))
                )
            assert queued == 0
            assert outboxed == 0

            real_retry_job_id = "batch-real-retry-job"
            real_retry_draft_id = "batch-real-retry-draft"
            real_retry_asset_id = "batch-real-retry-asset"
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        ClassroomAsset(
                            id=real_retry_asset_id,
                            tenant_id=tenant_id,
                            owner_id=context.user_id,
                            title="Recovered classroom",
                            lifecycle_state="awaiting_outline",
                        )
                    )
                    await session.flush()
                    session.add(
                        GenerationJob(
                            id=real_retry_job_id,
                            tenant_id=tenant_id,
                            job_kind="generation",
                            phase="outline",
                            export_format=None,
                            status="awaiting_confirmation",
                            priority=100,
                            quota_units=45,
                            actor_id=context.user_id,
                            owner_id=context.user_id,
                            visibility="class",
                            request_id="batch-real-retry-request",
                            idempotency_key="batch-real-retry-idempotency",
                            classroom_draft_id=real_retry_draft_id,
                            batch_id=batch_id,
                            request_sha256="c" * 64,
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            request_payload="{}",
                            retry_of_job_id=second_job_id,
                        )
                    )
                    await session.flush()
                    session.add(
                        ClassroomDraft(
                            id=real_retry_draft_id,
                            tenant_id=tenant_id,
                            classroom_id=real_retry_asset_id,
                            generation_job_id=real_retry_job_id,
                            teaching_brief_id=None,
                            base_version_id=None,
                            revision=1,
                            document="{}",
                            document_sha256="d" * 64,
                            outline_document=None,
                            outline_sha256=None,
                            confirmed_outline_sha256=None,
                            validation_report=None,
                            validation_report_sha256=None,
                            validation_revision=None,
                            validation_document_sha256=None,
                            creation_idempotency_key=None,
                            creation_request_sha256=None,
                            created_by=context.user_id,
                            updated_by=context.user_id,
                        )
                    )

            raced_item = await batch_repository.rebind_failed_item(
                batch_id,
                "item-a",
                expected_job_id=second_job_id,
                new_job_id=real_retry_job_id,
            )

            assert raced_item.status == "awaiting_confirmation"
            assert raced_item.generation_job_id == real_retry_job_id
            assert raced_item.classroom_draft_id == real_retry_draft_id
            async with session_factory() as session:
                rebound_draft_job_id = await session.scalar(
                    select(ClassroomDraft.generation_job_id).where(
                        ClassroomDraft.id == real_retry_draft_id
                    )
                )
            assert rebound_draft_job_id == real_retry_job_id

            tampered = json.loads(second_details.request_payload)
            tampered["unexpected"] = "must-not-replay"
            tampered_payload = json.dumps(
                tampered,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            tampered_sha256 = hashlib.sha256(tampered_payload.encode()).hexdigest()
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(GenerationJob)
                        .where(GenerationJob.id == second_job_id)
                        .values(
                            request_payload=tampered_payload,
                            request_sha256=tampered_sha256,
                            public_request_sha256=tampered_sha256,
                        )
                    )
            with pytest.raises(BatchPersistenceError, match="stored rejected"):
                await jobs.rejected_input(context, job_id=second_job_id)

            del tampered["unexpected"]
            tampered["classroom"]["unexpected"] = "must-not-replay"
            tampered_payload = json.dumps(
                tampered,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            tampered_sha256 = hashlib.sha256(tampered_payload.encode()).hexdigest()
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(GenerationJob)
                        .where(GenerationJob.id == second_job_id)
                        .values(
                            request_payload=tampered_payload,
                            request_sha256=tampered_sha256,
                            public_request_sha256=tampered_sha256,
                        )
                    )
            with pytest.raises(BatchPersistenceError, match="stored rejected"):
                await jobs.rejected_input(context, job_id=second_job_id)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_batch_reconciliation_requires_confirmed_content_binding_and_read_is_clean(
    generation_database: Any,
) -> None:
    tenant_id = "batch-confirm-race"
    batch_id = f"batch-{'e' * 20}-{'f' * 32}"
    older_batch_id = f"batch-{'1' * 20}-{'2' * 32}"
    forbidden_batch_id = f"batch-{'3' * 20}-{'4' * 32}"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _insert_active_tenant(engine, tenant_id)
            repository = SqlAlchemyBatchRepository(engine, tenant_id)
            await repository.create(batch_id, "author-1", ("item-a", "item-b"))
            await repository.create(older_batch_id, "author-1", ("older-item",))
            await repository.create(
                forbidden_batch_id,
                "author-1",
                ("forbidden-item",),
            )
            translated_engine = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            session_factory = async_sessionmaker(
                translated_engine,
                expire_on_commit=False,
            )
            async with session_factory() as session:
                async with session.begin():
                    for suffix in ("a", "b"):
                        asset_id = f"batch-race-asset-{suffix}"
                        draft_id = f"batch-race-draft-{suffix}"
                        job_id = f"batch-race-job-{suffix}"
                        session.add(
                            ClassroomAsset(
                                id=asset_id,
                                tenant_id=tenant_id,
                                owner_id="author-1",
                                title=f"Batch race {suffix}",
                                lifecycle_state="awaiting_outline",
                            )
                        )
                        await session.flush()
                        session.add(
                            GenerationJob(
                                id=job_id,
                                tenant_id=tenant_id,
                                job_kind="generation",
                                phase="outline",
                                export_format=None,
                                status="awaiting_confirmation",
                                priority=100,
                                quota_units=45,
                                actor_id="author-1",
                                owner_id="author-1",
                                visibility="class",
                                request_id=f"batch-race-request-{suffix}",
                                idempotency_key=f"batch-race-key-{suffix}",
                                classroom_draft_id=draft_id,
                                batch_id=batch_id,
                                resource_course_id="course-a",
                                resource_class_id="class-a",
                                request_sha256=suffix * 64,
                                data_plane_route_id="shared-primary",
                                provider_profile_id="platform-default",
                                worker_pool_ref="shared-generation",
                                queue_ref="openmaic.shared",
                                request_payload="{}",
                            )
                        )
                        await session.flush()
                        session.add(
                            ClassroomDraft(
                                id=draft_id,
                                tenant_id=tenant_id,
                                classroom_id=asset_id,
                                generation_job_id=job_id,
                                teaching_brief_id=None,
                                base_version_id=None,
                                revision=1,
                                document="{}",
                                document_sha256=suffix * 64,
                                outline_document="{}",
                                outline_sha256=suffix * 64,
                                confirmed_outline_sha256=None,
                                validation_report=None,
                                validation_report_sha256=None,
                                validation_revision=None,
                                validation_document_sha256=None,
                                creation_idempotency_key=None,
                                creation_request_sha256=None,
                                created_by="author-1",
                                updated_by="author-1",
                            )
                        )
                    for (
                        page_job_id,
                        page_batch_id,
                        course_id,
                        class_id,
                        digest,
                    ) in (
                        (
                            "batch-page-older-job",
                            older_batch_id,
                            "course-a",
                            "class-b",
                            "7",
                        ),
                        (
                            "batch-page-forbidden-job",
                            forbidden_batch_id,
                            "course-z",
                            "class-z",
                            "8",
                        ),
                    ):
                        session.add(
                            GenerationJob(
                                id=page_job_id,
                                tenant_id=tenant_id,
                                job_kind="generation",
                                phase="outline",
                                export_format=None,
                                status="failed",
                                priority=100,
                                quota_units=1,
                                actor_id="author-1",
                                owner_id="author-1",
                                visibility="class",
                                request_id=f"{page_job_id}-request",
                                idempotency_key=f"{page_job_id}-key",
                                classroom_draft_id=None,
                                batch_id=page_batch_id,
                                resource_course_id=course_id,
                                resource_class_id=class_id,
                                request_sha256=digest * 64,
                                data_plane_route_id="batch-rejected",
                                provider_profile_id="batch-rejected",
                                worker_pool_ref="batch-rejected",
                                queue_ref="batch-rejected",
                                request_payload="{}",
                                error_category="request",
                                error_code="batch_item_rejected",
                            )
                        )

            for suffix in ("a", "b"):
                await repository.bind_item(
                    batch_id,
                    f"item-{suffix}",
                    generation_job_id=f"batch-race-job-{suffix}",
                    classroom_draft_id=f"batch-race-draft-{suffix}",
                    classroom_asset_id=f"batch-race-asset-{suffix}",
                    status="awaiting_confirmation",
                )
            await repository.bind_rejected_item(
                older_batch_id,
                "older-item",
                generation_job_id="batch-page-older-job",
            )
            await repository.bind_rejected_item(
                forbidden_batch_id,
                "forbidden-item",
                generation_job_id="batch-page-forbidden-job",
            )

            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ClassroomDraft)
                        .where(ClassroomDraft.id == "batch-race-draft-a")
                        .values(confirmed_outline_sha256="c" * 64)
                    )
                    await session.execute(
                        update(GenerationJob)
                        .where(GenerationJob.id == "batch-race-job-a")
                        .values(phase="content", status="succeeded")
                    )

            recovered = await repository.get(batch_id)

            assert recovered is not None
            assert tuple((item.id, item.status) for item in recovered.items) == (
                ("item-a", "succeeded"),
                ("item-b", "awaiting_confirmation"),
            )
            assert {
                (item.resource_course_id, item.resource_class_id) for item in recovered.items
            } == {("course-a", "class-a")}
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(BatchJob)
                        .where(BatchJob.id == batch_id)
                        .values(created_at=datetime(2026, 8, 4, tzinfo=UTC))
                    )
                    await session.execute(
                        update(BatchJob)
                        .where(BatchJob.id == older_batch_id)
                        .values(created_at=datetime(2026, 8, 3, tzinfo=UTC))
                    )
                    await session.execute(
                        update(BatchJob)
                        .where(BatchJob.id == forbidden_batch_id)
                        .values(created_at=datetime(2026, 8, 5, tzinfo=UTC))
                    )
            recovered = await repository.get(batch_id)
            assert recovered is not None
            async with session_factory() as session:
                stable_updated_at = await session.scalar(
                    select(BatchJob.updated_at).where(BatchJob.id == batch_id)
                )

            read_writes: list[str] = []
            read_statements: list[str] = []
            scoped_context = TenantContext(
                tenant_id=tenant_id,
                schema_name=tenant_schema_name(tenant_id),
                user_id="course-author",
                permissions=permissions_for_roles(
                    {"content_author"},
                    scope_type="course",
                    scope_id="course-a",
                    tenant_id=tenant_id,
                ),
            )
            service = BatchService(repository, object(), object())

            def capture_batch_writes(
                _connection,
                _cursor,
                statement,
                _parameters,
                _context,
                _executemany,
            ) -> None:
                normalized = statement.lstrip().lower()
                read_statements.append(normalized)
                if normalized.startswith("update ") and (
                    "batch_jobs" in normalized or "batch_items" in normalized
                ):
                    read_writes.append(statement)

            event.listen(
                engine.sync_engine,
                "before_cursor_execute",
                capture_batch_writes,
            )
            try:
                stable_get = await repository.get(batch_id)
                stable_list = await service.list(
                    scoped_context,
                    limit=1,
                    offset=0,
                )
                second_page = await service.list(
                    scoped_context,
                    limit=1,
                    offset=1,
                )
            finally:
                event.remove(
                    engine.sync_engine,
                    "before_cursor_execute",
                    capture_batch_writes,
                )
            assert stable_get == recovered
            assert stable_list == (recovered,)
            assert tuple(batch.id for batch in second_page) == (older_batch_id,)
            assert read_writes == []
            locked_batch_queries = tuple(
                statement
                for statement in read_statements
                if "batch_jobs" in statement and "for update" in statement
            )
            assert locked_batch_queries
            assert all("batch_jobs.id =" in statement for statement in locked_batch_queries)
            scope_projection_queries = tuple(
                statement
                for statement in read_statements
                if "batch_items" in statement
                and "generation_jobs" in statement
                and "resource_course_id" in statement
                and "batch_jobs" not in statement
            )
            assert len(scope_projection_queries) == 3
            assert any(
                "batch_jobs" in statement
                and "order by" in statement
                and "limit" in statement
                and "exists" in statement
                and "for update" not in statement
                for statement in read_statements
            )
            async with session_factory() as session:
                after_read_updated_at = await session.scalar(
                    select(BatchJob.updated_at).where(BatchJob.id == batch_id)
                )
            assert after_read_updated_at == stable_updated_at

            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(GenerationJob)
                        .where(GenerationJob.id == "batch-race-job-b")
                        .values(phase="content", status="succeeded")
                    )

            with pytest.raises(BatchPersistenceError, match="state is invalid"):
                await repository.get(batch_id)

            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ClassroomDraft)
                        .where(ClassroomDraft.id == "batch-race-draft-b")
                        .values(
                            generation_job_id="batch-race-job-a",
                            confirmed_outline_sha256="d" * 64,
                        )
                    )

            with pytest.raises(BatchPersistenceError, match="state is invalid"):
                await repository.get(batch_id)

            async with session_factory() as session:
                statuses = tuple(
                    await session.scalars(
                        select(BatchItem.status)
                        .where(BatchItem.batch_job_id == batch_id)
                        .order_by(BatchItem.id)
                    )
                )
            assert statuses == ("succeeded", "awaiting_confirmation")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_locked_outline_confirmation_recovers_only_the_same_reviewed_payload(
    generation_database: Any,
) -> None:
    tenant_id = "outline-confirm-recovery"
    asset_id = "outline-confirm-recovery-asset"
    draft_id = "outline-confirm-recovery-draft"
    job_id = "outline-confirm-recovery-job"
    reviewed_revision = 3
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _insert_active_tenant(engine, tenant_id)
            reviewed = OutlineBundle.model_validate(valid_outline_bundle()).model_copy(
                update={"confirmation_metadata": OutlineConfirmationMetadata(status="draft")}
            )
            reviewed_sha256 = canonical_outline_sha256(reviewed)
            confirmed = reviewed.model_copy(
                update={
                    "confirmation_metadata": OutlineConfirmationMetadata(
                        status="confirmed",
                        confirmed_at=datetime(2026, 8, 4, tzinfo=UTC),
                        confirmed_by="author-1",
                    )
                }
            )
            confirmed_sha256 = canonical_outline_sha256(confirmed)
            brief_context = TenantContext(
                tenant_id=tenant_id,
                schema_name=tenant_schema_name(tenant_id),
                user_id="author-1",
                permissions=frozenset(),
            )
            brief = (
                TeachingBriefBuilder(brief_context, object())
                .open_creation(
                    TeachingBriefSpec(
                        course_id="course-a",
                        class_id="class-a",
                        objective="Explain motion",
                        grade_band="grade-8",
                        audience="intermediate",
                        duration_minutes=45,
                        classroom_mode="full",
                        web_policy="disabled",
                        template_id="template-a",
                        template_version="1",
                        knowledge_points=(
                            KnowledgePointSpec(
                                knowledge_point_id="kp-motion",
                                title="Motion",
                                description="Describe displacement and velocity",
                            ),
                        ),
                        content_mode="open_creation",
                        open_creation_acknowledged=True,
                    )
                )
                .contract
            )
            brief_document = json.dumps(
                brief.model_dump(mode="json", by_alias=True, exclude_none=False),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            translated_engine = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            session_factory = async_sessionmaker(
                translated_engine,
                expire_on_commit=False,
            )
            async with session_factory() as session:
                async with session.begin():
                    session.add(Course(id="course-a", title="Course A", status="active"))
                    await session.flush()
                    session.add(
                        TeachingClass(
                            id="class-a",
                            course_id="course-a",
                            name="Class A",
                            status="active",
                        )
                    )
                    await session.flush()
                    session.add(
                        TeachingBrief(
                            id=brief.brief_id,
                            tenant_id=tenant_id,
                            source_snapshot_id=None,
                            course_id="course-a",
                            class_id="class-a",
                            brief_version=1,
                            document=brief_document,
                            document_sha256=brief.content_sha256,
                            created_by="author-1",
                        )
                    )
                    await session.flush()
                    session.add(
                        ClassroomAsset(
                            id=asset_id,
                            tenant_id=tenant_id,
                            owner_id="author-1",
                            title="Reviewed classroom",
                            lifecycle_state="awaiting_outline",
                        )
                    )
                    await session.flush()
                    session.add(
                        GenerationJob(
                            id=job_id,
                            tenant_id=tenant_id,
                            job_kind="generation",
                            phase="outline",
                            export_format=None,
                            status="awaiting_confirmation",
                            priority=100,
                            quota_units=45,
                            actor_id="author-1",
                            owner_id="author-1",
                            visibility="class",
                            request_id="outline-confirm-recovery-request",
                            idempotency_key="outline-confirm-recovery-key",
                            classroom_draft_id=draft_id,
                            batch_id=f"batch-{'c' * 20}-{'d' * 32}",
                            request_sha256="e" * 64,
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            request_payload="{}",
                        )
                    )
                    await session.flush()
                    session.add(
                        ClassroomDraft(
                            id=draft_id,
                            tenant_id=tenant_id,
                            classroom_id=asset_id,
                            generation_job_id=job_id,
                            teaching_brief_id=brief.brief_id,
                            base_version_id=None,
                            revision=reviewed_revision,
                            document="{}",
                            document_sha256="f" * 64,
                            outline_document=canonical_json_bytes(reviewed).decode(),
                            outline_sha256=reviewed_sha256,
                            confirmed_outline_sha256=None,
                            validation_report=None,
                            validation_report_sha256=None,
                            validation_revision=None,
                            validation_document_sha256=None,
                            creation_idempotency_key=None,
                            creation_request_sha256=None,
                            created_by="author-1",
                            updated_by="author-1",
                        )
                    )

            repository = SqlAlchemyClassroomRepository(engine, tenant_id)
            first = await repository.confirm_outline(
                asset_id,
                confirmed.model_dump(mode="json", by_alias=True, exclude_none=True),
                confirmed_sha256,
                reviewed_sha256,
                expected_revision=reviewed_revision,
                expected_outline_sha256=reviewed_sha256,
            )
            recovered = await repository.confirm_outline(
                asset_id,
                confirmed.model_dump(mode="json", by_alias=True, exclude_none=True),
                confirmed_sha256,
                reviewed_sha256,
                expected_revision=reviewed_revision,
                expected_outline_sha256=reviewed_sha256,
            )

            assert first.lifecycle_state == "generating_content"
            assert recovered.lifecycle_state == "generating_content"
            assert recovered.revision == reviewed_revision + 1
            assert recovered.confirmed_outline_sha256 == confirmed_sha256

            tampered = confirmed.model_copy(update={"title": "Tampered after durable confirmation"})
            tampered_payload = canonical_json_bytes(tampered).decode()
            tampered_sha256 = canonical_outline_sha256(tampered)
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ClassroomDraft)
                        .where(ClassroomDraft.id == draft_id)
                        .values(
                            outline_document=tampered_payload,
                            confirmed_outline_sha256=tampered_sha256,
                        )
                    )
            with pytest.raises(ClassroomConfirmationConflict):
                await repository.confirm_outline(
                    asset_id,
                    confirmed.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=True,
                    ),
                    confirmed_sha256,
                    reviewed_sha256,
                    expected_revision=reviewed_revision,
                    expected_outline_sha256=reviewed_sha256,
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_content_requeue_requires_the_outline_queue_and_slots_to_be_released(
    generation_database: Any,
) -> None:
    tenant_id = "content-requeue-tenant"
    job_id = "content-requeue-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        try:
            await _insert_active_tenant(engine, tenant_id)
            translated_engine = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            session_factory = async_sessionmaker(
                translated_engine,
                expire_on_commit=False,
            )
            now = datetime.now(UTC)
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        GenerationJob(
                            id=job_id,
                            tenant_id=tenant_id,
                            job_kind="generation",
                            phase="outline",
                            export_format=None,
                            status="awaiting_confirmation",
                            priority=300,
                            quota_units=1,
                            actor_id="teacher-1",
                            owner_id="teacher-1",
                            visibility="class",
                            request_id="content-requeue-request",
                            idempotency_key="content-requeue-idempotency",
                            classroom_draft_id="draft-1",
                            batch_id=None,
                            request_sha256="c" * 64,
                            data_plane_mode="shared",
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            request_payload="{}",
                        )
                    )
                    session.add(
                        GenerationQueue(
                            tenant_id=tenant_id,
                            job_id=job_id,
                            job_kind="generation",
                            phase="outline",
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            slot_pool="generation",
                            priority=300,
                            status="claimed",
                            claimed_at=now,
                            lease_owner="outline-worker",
                            lease_token="d" * 64,
                            lease_expires_at=now,
                            heartbeat_at=now,
                        )
                    )
                    session.add(
                        GenerationSlot(
                            worker_pool_ref="shared-generation",
                            slot_pool="generation",
                            scope="global",
                            owner_key="shared",
                            tenant_id=None,
                            ordinal=0,
                            claimed_tenant_id=tenant_id,
                            claimed_job_id=job_id,
                            lease_owner="outline-worker",
                            lease_token="d" * 64,
                            lease_expires_at=now,
                            heartbeat_at=now,
                        )
                    )

            repository = SqlAlchemyGenerationJobRepository(engine)
            content_payload = json.dumps(
                {
                    "tenantId": tenant_id,
                    "jobId": job_id,
                    "requestId": "content-requeue-request",
                    "idempotencyKey": "content-requeue-idempotency",
                    "dataPlaneRouteId": "shared-primary",
                    "phase": "content",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            content_sha256 = hashlib.sha256(content_payload.encode()).hexdigest()
            with pytest.raises(ContentRequeueConflict):
                await repository.requeue_confirmed_content(
                    tenant_id,
                    job_id,
                    request_payload=content_payload,
                    request_sha256=content_sha256,
                )

            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(GenerationSlot)
                        .where(
                            GenerationSlot.claimed_tenant_id == tenant_id,
                            GenerationSlot.claimed_job_id == job_id,
                        )
                        .values(
                            claimed_tenant_id=None,
                            claimed_job_id=None,
                            lease_owner=None,
                            lease_token=None,
                            lease_expires_at=None,
                            heartbeat_at=None,
                        )
                    )
                    await session.execute(
                        delete(GenerationQueue).where(
                            GenerationQueue.tenant_id == tenant_id,
                            GenerationQueue.job_id == job_id,
                        )
                    )

            assert await repository.requeue_confirmed_content(
                tenant_id,
                job_id,
                request_payload=content_payload,
                request_sha256=content_sha256,
            )
            job = await repository.get_job(tenant_id, job_id)
            assert job is not None
            assert job.phase == "content"
            assert job.status == "queued"
            details = await repository.get_job_details(tenant_id, job_id)
            assert details is not None
            assert details.request_payload == content_payload
            assert details.request_sha256 == content_sha256
        finally:
            await engine.dispose()

    asyncio.run(scenario())
