from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.models import (
    DataPlaneRoute,
    ProviderProfile,
    TeachingMetricCounterRollup,
    TeachingMetricHistogramRollup,
    Tenant,
)
from deeptutor.teaching.models.jobs import (
    GenerationJob,
    GenerationQueue,
    GenerationSlot,
    TenantSchedulerState,
)
from deeptutor.teaching.repositories.metric_rollups import metric_rollup_shard
from deeptutor.teaching.scheduler import (
    PRIORITY_RANK,
    FairScheduler,
    SchedulerClaimConflict,
)
from deeptutor.teaching.schema_names import tenant_schema_name

pytestmark = pytest.mark.usefixtures("clean_generation_runtime_state")


async def _insert_active_tenants(engine, tenant_ids: tuple[str, ...]) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            session.add_all(
                [
                    Tenant(
                        id=tenant_id,
                        name=tenant_id,
                        status="active",
                        data_plane_mode="shared",
                    )
                    for tenant_id in tenant_ids
                ]
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


async def _set_tenant_status(engine, tenant_id: str, status: str) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Tenant).where(Tenant.id == tenant_id).values(status=status)
            )


async def _insert_queued_job(
    engine,
    *,
    tenant_id: str,
    job_id: str,
    priority: str,
    job_kind: str = "generation",
    phase: str = "outline",
    export_format: str | None = None,
    slot_pool: str = "generation",
    data_plane_route_id: str = "shared-primary",
    provider_profile_id: str = "platform-default",
    worker_pool_ref: str = "shared-generation",
    queue_ref: str = "openmaic.shared",
) -> None:
    translated_engine = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    session_factory = async_sessionmaker(
        translated_engine,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                GenerationJob(
                    id=job_id,
                    tenant_id=tenant_id,
                    job_kind=job_kind,
                    phase=phase,
                    export_format=export_format,
                    status="queued",
                    priority=PRIORITY_RANK[priority],
                    quota_units=1,
                    actor_id="scheduler-test",
                    owner_id="scheduler-test",
                    visibility="tenant",
                    request_id=f"request-{job_id}",
                    idempotency_key=f"idempotency-{job_id}",
                    classroom_draft_id="draft-1",
                    batch_id=None,
                    request_sha256="b" * 64,
                    data_plane_route_id=data_plane_route_id,
                    provider_profile_id=provider_profile_id,
                    worker_pool_ref=worker_pool_ref,
                    queue_ref=queue_ref,
                    request_payload="{}",
                )
            )
            session.add(
                GenerationQueue(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    job_kind=job_kind,
                    phase=phase,
                    data_plane_route_id=data_plane_route_id,
                    provider_profile_id=provider_profile_id,
                    worker_pool_ref=worker_pool_ref,
                    queue_ref=queue_ref,
                    slot_pool=slot_pool,
                    priority=PRIORITY_RANK[priority],
                    status="queued",
                )
            )


async def _metric_counter_total(session, *, category: str, shard: int) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(TeachingMetricCounterRollup.total, 0)).where(
                TeachingMetricCounterRollup.metric == "generation_jobs_total",
                TeachingMetricCounterRollup.category == category,
                TeachingMetricCounterRollup.shard == shard,
            )
        )
        or 0
    )


async def _queue_histogram_totals(session, *, shard: int) -> tuple[int, float]:
    count, sum_seconds = (
        await session.execute(
            select(
                func.coalesce(func.sum(TeachingMetricHistogramRollup.count), 0),
                func.coalesce(func.sum(TeachingMetricHistogramRollup.sum_seconds), 0.0),
            ).where(
                TeachingMetricHistogramRollup.metric == "generation_queue_seconds",
                TeachingMetricHistogramRollup.category == "",
                TeachingMetricHistogramRollup.shard == shard,
            )
        )
    ).one()
    return int(count), float(sum_seconds)


def test_busy_tenant_cannot_hide_another_tenants_work(
    generation_database: Any,
) -> None:
    tenant_ids = ("fair-tenant-a", "fair-tenant-b")
    for tenant_id in tenant_ids:
        generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        try:
            await _insert_active_tenants(engine, tenant_ids)
            for index in range(20):
                await _insert_queued_job(
                    engine,
                    tenant_id=tenant_ids[0],
                    job_id=f"batch-{index}",
                    priority="batch",
                )
            await _insert_queued_job(
                engine,
                tenant_id=tenant_ids[1],
                job_id="teacher-1",
                priority="teacher",
            )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_generation_capacity(
                tenant_ids,
                worker_pool_ref="shared-generation",
            )

            blocker_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with blocker_factory() as blocker:
                async with blocker.begin():
                    await blocker.execute(
                        update(Tenant).where(Tenant.id == tenant_ids[1]).values(status="disabled")
                    )
                    first = await asyncio.wait_for(
                        scheduler.claim(
                            "generation",
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            worker_id="fair-worker-disabled-check",
                            lease_seconds=60,
                        ),
                        timeout=2,
                    )
            assert first is not None and first.tenant_id == tenant_ids[0]
            await _set_tenant_status(engine, tenant_ids[1], "active")
            claimed = [
                first,
                *[
                    await scheduler.claim(
                        "generation",
                        data_plane_route_id="shared-primary",
                        provider_profile_id="platform-default",
                        worker_pool_ref="shared-generation",
                        queue_ref="openmaic.shared",
                        worker_id=f"fair-worker-{index}",
                        lease_seconds=60,
                    )
                    for index in range(2)
                ],
            ]

            assert all(job is not None for job in claimed)
            tenant_counts = Counter(job.tenant_id for job in claimed if job is not None)
            assert tenant_counts[tenant_ids[1]] == 1
            assert tenant_counts[tenant_ids[0]] <= 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_claimers_respect_twenty_global_two_tenant_and_mp4_pool(
    generation_database: Any,
) -> None:
    tenant_ids = tuple(f"capacity-tenant-{index:02d}" for index in range(10))
    for tenant_id in tenant_ids:
        generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        try:
            await _insert_active_tenants(engine, tenant_ids)
            for tenant_id in tenant_ids:
                for index in range(3):
                    await _insert_queued_job(
                        engine,
                        tenant_id=tenant_id,
                        job_id=f"shared-job-{index}",
                        priority="batch",
                    )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_generation_capacity(
                tenant_ids,
                worker_pool_ref="shared-generation",
            )

            async def claim_with_retry(index: int):
                for attempt in range(10):
                    claimed = await scheduler.claim(
                        "generation",
                        data_plane_route_id="shared-primary",
                        provider_profile_id="platform-default",
                        worker_pool_ref="shared-generation",
                        queue_ref="openmaic.shared",
                        worker_id=f"capacity-worker-{index}-{attempt}",
                        lease_seconds=60,
                    )
                    if claimed is not None:
                        return claimed
                    await asyncio.sleep(0.01)
                return None

            results = await asyncio.gather(*(claim_with_retry(index) for index in range(30)))
            claimed = [result for result in results if result is not None]
            assert len(claimed) == 20
            tenant_counts = Counter(job.tenant_id for job in claimed)
            assert all(count <= 2 for count in tenant_counts.values())

            await _insert_queued_job(
                engine,
                tenant_id=tenant_ids[0],
                job_id="mp4-export",
                priority="teacher",
                job_kind="export",
                phase="export",
                export_format="mp4",
                slot_pool="mp4_export",
            )
            await scheduler.ensure_slots(
                [tenant_ids[0]],
                worker_pool_ref="shared-generation",
                slot_pool="mp4_export",
                global_limit=1,
                tenant_limit=1,
            )
            mp4_claim = await scheduler.claim(
                "mp4_export",
                data_plane_route_id="shared-primary",
                provider_profile_id="platform-default",
                worker_pool_ref="shared-generation",
                queue_ref="openmaic.shared",
                worker_id="mp4-worker",
                lease_seconds=60,
            )
            assert mp4_claim is not None
            assert mp4_claim.status == "exporting"

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                generation_claims = await session.scalar(
                    select(func.count())
                    .select_from(GenerationSlot)
                    .where(
                        GenerationSlot.slot_pool == "generation",
                        GenerationSlot.worker_pool_ref == "shared-generation",
                        GenerationSlot.scope == "global",
                        GenerationSlot.claimed_job_id.is_not(None),
                    )
                )
                mp4_claims = await session.scalar(
                    select(func.count())
                    .select_from(GenerationSlot)
                    .where(
                        GenerationSlot.slot_pool == "mp4_export",
                        GenerationSlot.worker_pool_ref == "shared-generation",
                        GenerationSlot.scope == "global",
                        GenerationSlot.claimed_job_id.is_not(None),
                    )
                )
                assert generation_claims == 20
                assert mp4_claims == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_shared_worker_cannot_claim_dedicated_job_with_the_same_job_id(
    generation_database: Any,
) -> None:
    shared_tenant = "routing-shared-tenant"
    dedicated_tenant = "routing-dedicated-tenant"
    for tenant_id in (shared_tenant, dedicated_tenant):
        generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        try:
            await _insert_active_tenants(
                engine,
                (shared_tenant, dedicated_tenant),
            )
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(Tenant)
                        .where(Tenant.id == dedicated_tenant)
                        .values(data_plane_mode="dedicated")
                    )
                    session.add(
                        ProviderProfile(
                            id="dedicated-provider",
                            scope="dedicated",
                            tenant_id=dedicated_tenant,
                            owner_key=dedicated_tenant,
                            provider_type="openai-compatible",
                            model_name="test-model",
                            api_base_url=None,
                            secret_ref=f"tests/{dedicated_tenant}/provider",
                            status="active",
                        )
                    )
                    await session.flush()
                    session.add(
                        DataPlaneRoute(
                            id="dedicated-route",
                            tenant_id=dedicated_tenant,
                            owner_key=dedicated_tenant,
                            mode="dedicated",
                            base_url="http://openmaic-dedicated.invalid",
                            worker_pool="dedicated-worker-pool",
                            queue_name="openmaic.dedicated",
                            provider_profile_id="dedicated-provider",
                            status="active",
                            health_status="healthy",
                        )
                    )
            await _insert_queued_job(
                engine,
                tenant_id=shared_tenant,
                job_id="same-job",
                priority="teacher",
            )
            await _insert_queued_job(
                engine,
                tenant_id=dedicated_tenant,
                job_id="same-job",
                priority="teacher",
                data_plane_route_id="dedicated-route",
                provider_profile_id="dedicated-provider",
                worker_pool_ref="dedicated-worker-pool",
                queue_ref="openmaic.dedicated",
            )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_slots(
                [shared_tenant],
                worker_pool_ref="shared-generation",
                slot_pool="generation",
                global_limit=1,
                tenant_limit=1,
            )
            await scheduler.ensure_slots(
                [dedicated_tenant],
                worker_pool_ref="dedicated-worker-pool",
                slot_pool="generation",
                global_limit=1,
                tenant_limit=1,
            )

            shared_claim = await scheduler.claim(
                "generation",
                data_plane_route_id="shared-primary",
                provider_profile_id="platform-default",
                worker_pool_ref="shared-generation",
                queue_ref="openmaic.shared",
                worker_id="shared-worker",
                lease_seconds=60,
            )
            assert shared_claim is not None
            assert shared_claim.tenant_id == shared_tenant

            async with session_factory() as session:
                dedicated_queue = await session.get(
                    GenerationQueue,
                    (dedicated_tenant, "same-job"),
                )
                assert dedicated_queue is not None
                assert dedicated_queue.status == "queued"

            dedicated_claim = await scheduler.claim(
                "generation",
                data_plane_route_id="dedicated-route",
                provider_profile_id="dedicated-provider",
                worker_pool_ref="dedicated-worker-pool",
                queue_ref="openmaic.dedicated",
                worker_id="dedicated-worker",
                lease_seconds=60,
            )
            assert dedicated_claim is not None
            assert dedicated_claim.tenant_id == dedicated_tenant
            assert dedicated_claim.job_id == shared_claim.job_id == "same-job"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_stale_queue_head_is_removed_without_starving_the_next_job(
    generation_database: Any,
) -> None:
    tenant_id = "stale-head-tenant"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        try:
            await _insert_active_tenants(engine, (tenant_id,))
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        GenerationQueue(
                            tenant_id=tenant_id,
                            job_id="a-stale-job",
                            job_kind="generation",
                            phase="outline",
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            slot_pool="generation",
                            priority=PRIORITY_RANK["teacher"],
                            status="queued",
                        )
                    )
            await _insert_queued_job(
                engine,
                tenant_id=tenant_id,
                job_id="z-fresh-job",
                priority="teacher",
            )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_slots(
                [tenant_id],
                worker_pool_ref="shared-generation",
                slot_pool="generation",
                global_limit=1,
                tenant_limit=1,
            )

            claimed = await scheduler.claim(
                "generation",
                data_plane_route_id="shared-primary",
                provider_profile_id="platform-default",
                worker_pool_ref="shared-generation",
                queue_ref="openmaic.shared",
                worker_id="stale-head-worker",
                lease_seconds=60,
            )

            assert claimed is not None and claimed.job_id == "z-fresh-job"
            async with session_factory() as session:
                assert (
                    await session.get(
                        GenerationQueue,
                        (tenant_id, "a-stale-job"),
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_route_disable_commits_before_a_waiting_scheduler_claim(
    generation_database: Any,
) -> None:
    tenant_id = "binding-claim-race-tenant"
    job_id = "binding-claim-race-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenants(engine, (tenant_id,))
            await _insert_queued_job(
                engine,
                tenant_id=tenant_id,
                job_id=job_id,
                priority="teacher",
            )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_slots(
                [tenant_id],
                worker_pool_ref="shared-generation",
                slot_pool="generation",
                global_limit=1,
                tenant_limit=1,
            )
            claim_task = None
            async with session_factory() as blocker:
                async with blocker.begin():
                    route = await blocker.scalar(
                        select(DataPlaneRoute)
                        .where(DataPlaneRoute.id == "shared-primary")
                        .with_for_update()
                    )
                    assert route is not None
                    claim_task = asyncio.create_task(
                        scheduler.claim(
                            "generation",
                            data_plane_route_id="shared-primary",
                            provider_profile_id="platform-default",
                            worker_pool_ref="shared-generation",
                            queue_ref="openmaic.shared",
                            worker_id="binding-race-worker",
                            lease_seconds=60,
                        )
                    )
                    await asyncio.sleep(0.05)
                    assert not claim_task.done()
                    route.status = "disabled"
            assert claim_task is not None
            assert await claim_task is None
            translated_engine = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            translated_factory = async_sessionmaker(
                translated_engine,
                expire_on_commit=False,
            )
            async with translated_factory() as session:
                job = await session.get(GenerationJob, job_id)
                queue = await session.get(GenerationQueue, (tenant_id, job_id))
                assert job is not None and job.status == "queued"
                assert queue is not None and queue.status == "queued"
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


def test_successful_claim_records_one_running_job_and_eligible_queue_wait(
    generation_database: Any,
) -> None:
    tenant_id = "metric-claim-tenant"
    job_id = "metric-claim-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenants(engine, (tenant_id,))
            await _insert_queued_job(
                engine,
                tenant_id=tenant_id,
                job_id=job_id,
                priority="teacher",
            )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_generation_capacity(
                [tenant_id],
                worker_pool_ref="shared-generation",
            )
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(GenerationQueue)
                        .where(
                            GenerationQueue.tenant_id == tenant_id,
                            GenerationQueue.job_id == job_id,
                        )
                        .values(available_at=func.now() - text("INTERVAL '5 seconds'"))
                    )

            fact_key = f"{tenant_id}/{job_id}/outline/1"
            running_shard = metric_rollup_shard(
                fact_key,
                "generation_jobs_total",
                "running",
            )
            queue_shard = metric_rollup_shard(
                fact_key,
                "generation_queue_seconds",
                "",
            )
            async with session_factory() as session:
                running_before = await _metric_counter_total(
                    session,
                    category="running",
                    shard=running_shard,
                )
                queue_before = await _queue_histogram_totals(
                    session,
                    shard=queue_shard,
                )

            claimed = await scheduler.claim(
                "generation",
                data_plane_route_id="shared-primary",
                provider_profile_id="platform-default",
                worker_pool_ref="shared-generation",
                queue_ref="openmaic.shared",
                worker_id="metric-worker",
                lease_seconds=60,
            )
            assert claimed is not None and claimed.attempt_count == 1
            assert (
                await scheduler.claim(
                    "generation",
                    data_plane_route_id="shared-primary",
                    provider_profile_id="platform-default",
                    worker_pool_ref="shared-generation",
                    queue_ref="openmaic.shared",
                    worker_id="metric-worker-duplicate",
                    lease_seconds=60,
                )
                is None
            )

            async with session_factory() as session:
                running_after = await _metric_counter_total(
                    session,
                    category="running",
                    shard=running_shard,
                )
                queue_after = await _queue_histogram_totals(
                    session,
                    shard=queue_shard,
                )
                assert running_after == running_before + 1
                assert queue_after[0] == queue_before[0] + 1
                assert queue_after[1] >= queue_before[1] + 5.0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_platform_claim_failure_rolls_back_the_tenant_job_and_all_locks(
    generation_database: Any,
) -> None:
    tenant_id = "rollback-slot-claim-tenant"
    job_id = "rollback-slot-claim-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenants(engine, (tenant_id,))
            await _insert_queued_job(
                engine,
                tenant_id=tenant_id,
                job_id=job_id,
                priority="teacher",
            )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_generation_capacity(
                [tenant_id],
                worker_pool_ref="shared-generation",
            )
            fact_key = f"{tenant_id}/{job_id}/outline/1"
            running_shard = metric_rollup_shard(
                fact_key,
                "generation_jobs_total",
                "running",
            )
            queue_shard = metric_rollup_shard(
                fact_key,
                "generation_queue_seconds",
                "",
            )
            async with session_factory() as session:
                running_before = await _metric_counter_total(
                    session,
                    category="running",
                    shard=running_shard,
                )
                queue_before = await _queue_histogram_totals(
                    session,
                    shard=queue_shard,
                )
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            """
                            CREATE FUNCTION platform.fail_generation_slot_claim()
                            RETURNS trigger
                            LANGUAGE plpgsql
                            AS $$
                            BEGIN
                                IF NEW.claimed_job_id = 'rollback-slot-claim-job' THEN
                                    RAISE EXCEPTION 'injected slot failure';
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
                            CREATE TRIGGER fail_generation_slot_claim
                            BEFORE UPDATE ON platform.generation_slots
                            FOR EACH ROW
                            EXECUTE FUNCTION platform.fail_generation_slot_claim()
                            """
                        )
                    )
            try:
                with pytest.raises(DBAPIError) as caught:
                    await scheduler.claim(
                        "generation",
                        data_plane_route_id="shared-primary",
                        provider_profile_id="platform-default",
                        worker_pool_ref="shared-generation",
                        queue_ref="openmaic.shared",
                        worker_id="rollback-slot-worker",
                        lease_seconds=60,
                    )
                assert "injected slot failure" in str(caught.value.orig)
            finally:
                async with session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                """
                                DROP TRIGGER fail_generation_slot_claim
                                ON platform.generation_slots
                                """
                            )
                        )
                        await session.execute(
                            text(
                                """
                                DROP FUNCTION platform.fail_generation_slot_claim()
                                """
                            )
                        )

            translated_engine = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            translated_factory = async_sessionmaker(
                translated_engine,
                expire_on_commit=False,
            )
            async with translated_factory() as session:
                job = await session.scalar(
                    select(GenerationJob).where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == tenant_id,
                    )
                )
                queue = await session.get(
                    GenerationQueue,
                    (tenant_id, job_id),
                )
                claimed_slots = await session.scalar(
                    select(func.count())
                    .select_from(GenerationSlot)
                    .where(
                        GenerationSlot.claimed_tenant_id == tenant_id,
                        GenerationSlot.claimed_job_id == job_id,
                    )
                )
                state = await session.get(
                    TenantSchedulerState,
                    (tenant_id, "shared-generation", "generation"),
                )
                assert job is not None
                assert job.status == "queued"
                assert job.attempt_count == 0
                assert job.lease_token is None
                assert queue is not None and queue.status == "queued"
                assert queue.lease_token is None
                assert claimed_slots == 0
                assert state is not None and state.last_dispatched_at is None
                assert (
                    await _metric_counter_total(
                        session,
                        category="running",
                        shard=running_shard,
                    )
                    == running_before
                )
                assert await _queue_histogram_totals(session, shard=queue_shard) == queue_before
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_metric_rollup_failure_rolls_back_job_queue_slots_and_scheduler_state(
    generation_database: Any,
) -> None:
    tenant_id = "rollback-claim-tenant"
    job_id = "rollback-claim-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(
            generation_database.url,
            poolclass=NullPool,
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _insert_active_tenants(engine, (tenant_id,))
            await _insert_queued_job(
                engine,
                tenant_id=tenant_id,
                job_id=job_id,
                priority="teacher",
            )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_generation_capacity(
                [tenant_id],
                worker_pool_ref="shared-generation",
            )
            fact_key = f"{tenant_id}/{job_id}/outline/1"
            running_shard = metric_rollup_shard(
                fact_key,
                "generation_jobs_total",
                "running",
            )
            queue_shard = metric_rollup_shard(
                fact_key,
                "generation_queue_seconds",
                "",
            )
            async with session_factory() as session:
                running_before = await _metric_counter_total(
                    session,
                    category="running",
                    shard=running_shard,
                )
                queue_before = await _queue_histogram_totals(
                    session,
                    shard=queue_shard,
                )
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            """
                            CREATE FUNCTION platform.fail_generation_queue_metric()
                            RETURNS trigger
                            LANGUAGE plpgsql
                            AS $$
                            BEGIN
                                IF NEW.metric = 'generation_queue_seconds' THEN
                                    RAISE EXCEPTION 'injected metric rollup failure';
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
                            CREATE TRIGGER fail_generation_queue_metric
                            BEFORE INSERT OR UPDATE
                            ON platform.teaching_metric_histogram_rollups
                            FOR EACH ROW
                            EXECUTE FUNCTION platform.fail_generation_queue_metric()
                            """
                        )
                    )
            try:
                with pytest.raises(DBAPIError) as caught:
                    await scheduler.claim(
                        "generation",
                        data_plane_route_id="shared-primary",
                        provider_profile_id="platform-default",
                        worker_pool_ref="shared-generation",
                        queue_ref="openmaic.shared",
                        worker_id="rollback-worker",
                        lease_seconds=60,
                    )
                assert "injected metric rollup failure" in str(caught.value.orig)
            finally:
                async with session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                """
                                DROP TRIGGER fail_generation_queue_metric
                                ON platform.teaching_metric_histogram_rollups
                                """
                            )
                        )
                        await session.execute(
                            text(
                                """
                                DROP FUNCTION platform.fail_generation_queue_metric()
                                """
                            )
                        )

            translated_engine = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            translated_factory = async_sessionmaker(
                translated_engine,
                expire_on_commit=False,
            )
            async with translated_factory() as session:
                job = await session.scalar(
                    select(GenerationJob).where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == tenant_id,
                    )
                )
                queue = await session.get(
                    GenerationQueue,
                    (tenant_id, job_id),
                )
                claimed_slots = await session.scalar(
                    select(func.count())
                    .select_from(GenerationSlot)
                    .where(
                        GenerationSlot.claimed_tenant_id == tenant_id,
                        GenerationSlot.claimed_job_id == job_id,
                    )
                )
                state = await session.get(
                    TenantSchedulerState,
                    (tenant_id, "shared-generation", "generation"),
                )
                assert job is not None
                assert job.status == "queued"
                assert job.attempt_count == 0
                assert job.lease_token is None
                assert queue is not None and queue.status == "queued"
                assert queue.lease_token is None
                assert claimed_slots == 0
                assert state is not None and state.last_dispatched_at is None
                assert (
                    await _metric_counter_total(
                        session,
                        category="running",
                        shard=running_shard,
                    )
                    == running_before
                )
                assert await _queue_histogram_totals(session, shard=queue_shard) == queue_before
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("case", "job_kind", "phase", "export_format", "slot_pool", "job_update"),
    (
        (
            "priority",
            "generation",
            "outline",
            None,
            "generation",
            {"priority": PRIORITY_RANK["student_micro"]},
        ),
        (
            "mp4-into-generation",
            "export",
            "export",
            "pptx",
            "generation",
            {"export_format": "mp4"},
        ),
        (
            "non-mp4-into-mp4",
            "export",
            "export",
            "mp4",
            "mp4_export",
            {"export_format": "pptx"},
        ),
    ),
)
def test_authoritative_job_shape_drift_rolls_back_every_claim_side_effect(
    generation_database: Any,
    case: str,
    job_kind: str,
    phase: str,
    export_format: str | None,
    slot_pool: str,
    job_update: dict[str, object],
) -> None:
    tenant_id = f"claim-drift-{case}-tenant"
    job_id = f"claim-drift-{case}-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        translated_engine = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        translated_factory = async_sessionmaker(
            translated_engine,
            expire_on_commit=False,
        )
        try:
            await _insert_active_tenants(engine, (tenant_id,))
            await _insert_queued_job(
                engine,
                tenant_id=tenant_id,
                job_id=job_id,
                priority="teacher",
                job_kind=job_kind,
                phase=phase,
                export_format=export_format,
                slot_pool=slot_pool,
            )
            scheduler = FairScheduler(engine)
            await scheduler.ensure_slots(
                [tenant_id],
                worker_pool_ref="shared-generation",
                slot_pool=slot_pool,
                global_limit=1,
                tenant_limit=1,
            )
            async with translated_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(GenerationJob)
                        .where(
                            GenerationJob.id == job_id,
                            GenerationJob.tenant_id == tenant_id,
                        )
                        .values(**job_update)
                    )

            fact_key = f"{tenant_id}/{job_id}/{phase}/1"
            running_shard = metric_rollup_shard(
                fact_key,
                "generation_jobs_total",
                "running",
            )
            queue_shard = metric_rollup_shard(
                fact_key,
                "generation_queue_seconds",
                "",
            )
            async with session_factory() as session:
                running_before = await _metric_counter_total(
                    session,
                    category="running",
                    shard=running_shard,
                )
                queue_before = await _queue_histogram_totals(
                    session,
                    shard=queue_shard,
                )

            with pytest.raises(
                SchedulerClaimConflict,
                match="tenant job shape no longer matches queue projection",
            ):
                await scheduler.claim(
                    slot_pool,
                    data_plane_route_id="shared-primary",
                    provider_profile_id="platform-default",
                    worker_pool_ref="shared-generation",
                    queue_ref="openmaic.shared",
                    worker_id=f"claim-drift-{case}-worker",
                    lease_seconds=60,
                )

            async with translated_factory() as session:
                job = await session.get(GenerationJob, job_id)
                queue = await session.get(GenerationQueue, (tenant_id, job_id))
                claimed_slots = await session.scalar(
                    select(func.count())
                    .select_from(GenerationSlot)
                    .where(
                        GenerationSlot.claimed_tenant_id == tenant_id,
                        GenerationSlot.claimed_job_id == job_id,
                    )
                )
                state = await session.get(
                    TenantSchedulerState,
                    (tenant_id, "shared-generation", slot_pool),
                )
                assert job is not None and job.status == "queued"
                assert job.attempt_count == 0 and job.lease_token is None
                assert queue is not None and queue.status == "queued"
                assert queue.lease_token is None
                assert claimed_slots == 0
                assert state is not None and state.last_dispatched_at is None
                assert (
                    await _metric_counter_total(
                        session,
                        category="running",
                        shard=running_shard,
                    )
                    == running_before
                )
                assert await _queue_histogram_totals(session, shard=queue_shard) == queue_before
        finally:
            await engine.dispose()

    asyncio.run(scenario())
