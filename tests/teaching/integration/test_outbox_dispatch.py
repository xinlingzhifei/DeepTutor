from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.dispatcher import OutboxDispatcher
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.models import DataPlaneRoute, ProviderProfile, Tenant
from deeptutor.teaching.models.jobs import (
    GenerationJob,
    GenerationQueue,
    GenerationSlot,
    OutboxMessage,
)
from deeptutor.teaching.repositories.jobs import (
    ContentRequeueConflict,
    GenerationJobRequest,
    IdempotencyConflict,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.schema_names import tenant_schema_name


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
            job = await repository.get_job(tenant_id, job_id)
            assert job is not None and job.status == "queued"
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
