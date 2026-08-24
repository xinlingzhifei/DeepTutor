from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.dispatcher import OutboxDispatcher
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
    QuotaLedger,
)
from deeptutor.teaching.repositories.jobs import (
    GenerationJobRequest,
    SqlAlchemyGenerationJobRepository,
    _quota_event_id,
)
from deeptutor.teaching.scheduler import FairScheduler
from deeptutor.teaching.schema_names import tenant_schema_name

pytestmark = pytest.mark.usefixtures("clean_generation_runtime_state")


def _request(tenant_id: str, job_id: str, *, max_attempts: int = 5) -> GenerationJobRequest:
    return GenerationJobRequest(
        tenant_id=tenant_id,
        job_id=job_id,
        job_kind="generation",
        phase="outline",
        export_format=None,
        priority="teacher",
        quota_units=3,
        actor_id="teacher-a",
        owner_id="teacher-a",
        visibility="private",
        request_id=f"request-{job_id}",
        idempotency_key=f"idempotency-{job_id}",
        request_sha256=hashlib.sha256(b"{}").hexdigest(),
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        request_payload="{}",
        max_attempts=max_attempts,
    )


async def _insert_active_shared_tenant(engine, tenant_id: str) -> None:
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


async def _claim(scheduler: FairScheduler):
    claim = await scheduler.claim(
        "generation",
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        worker_id="worker-a",
        lease_seconds=60,
        job_kind="generation",
    )
    assert claim is not None
    return claim


async def _counter_totals(engine) -> dict[tuple[str, str], int]:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    TeachingMetricCounterRollup.metric,
                    TeachingMetricCounterRollup.category,
                    func.sum(TeachingMetricCounterRollup.total),
                )
                .where(
                    TeachingMetricCounterRollup.metric.in_(
                        (
                            "generation_jobs_total",
                            "generation_retries_total",
                            "quota_units_total",
                        )
                    )
                )
                .group_by(
                    TeachingMetricCounterRollup.metric,
                    TeachingMetricCounterRollup.category,
                )
            )
        ).all()
    return {(metric, category): int(total) for metric, category, total in rows}


async def _stage_counts(engine) -> dict[str, int]:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    TeachingMetricHistogramRollup.category,
                    func.sum(TeachingMetricHistogramRollup.count),
                )
                .where(TeachingMetricHistogramRollup.metric == "generation_stage_seconds")
                .group_by(TeachingMetricHistogramRollup.category)
            )
        ).all()
    return {category: int(count) for category, count in rows}


def _delta(after: dict, before: dict, key) -> int:
    return after.get(key, 0) - before.get(key, 0)


def test_job_lifecycle_rollups_are_exact_and_visible_to_a_second_connection(
    generation_database: Any,
) -> None:
    tenant_id = "metric-job-lifecycle-tenant"
    job_id = "metric-job-lifecycle"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        writer = create_async_engine(generation_database.url, poolclass=NullPool)
        reader = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _insert_active_shared_tenant(writer, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(writer)
            scheduler = FairScheduler(writer)
            await scheduler.ensure_generation_capacity(
                (tenant_id,),
                worker_pool_ref="shared-generation",
            )
            await repository.grant_quota(tenant_id, grant_id="grant-job-metrics", units=20)
            counters_before = await _counter_totals(reader)
            stages_before = await _stage_counts(reader)

            request = _request(tenant_id, job_id)
            await repository.create_job_and_reserve(request)
            await repository.create_job_and_reserve(request)
            assert await OutboxDispatcher(writer).dispatch_next() is not None

            first_outline = await _claim(scheduler)
            assert await repository.retry_claim(
                first_outline,
                error_category="read_timeout",
                error_code="openmaic_timeout",
                delay_seconds=0,
            )
            second_outline = await _claim(scheduler)
            await repository.complete_outline(second_outline, result_payload="{}")

            content_payload = json.dumps(
                {
                    "dataPlaneRouteId": request.data_plane_route_id,
                    "idempotencyKey": request.idempotency_key,
                    "jobId": job_id,
                    "phase": "content",
                    "requestId": request.request_id,
                    "tenantId": tenant_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            assert await repository.requeue_confirmed_content(
                tenant_id,
                job_id,
                request_payload=content_payload,
                request_sha256=hashlib.sha256(content_payload.encode()).hexdigest(),
            )
            assert await OutboxDispatcher(writer).dispatch_next() is not None

            first_content = await _claim(scheduler)
            assert await repository.retry_claim(
                first_content,
                error_category="read_timeout",
                error_code="openmaic_timeout",
                delay_seconds=0,
            )
            second_content = await _claim(scheduler)
            await repository.fail_claim(
                second_content,
                error_category="contract_invalid",
                error_code="invalid-output",
            )

            counters_after = await _counter_totals(reader)
            stages_after = await _stage_counts(reader)
            assert (
                _delta(
                    counters_after,
                    counters_before,
                    ("quota_units_total", "reserved"),
                )
                == 3
            )
            assert (
                _delta(
                    counters_after,
                    counters_before,
                    ("quota_units_total", "released"),
                )
                == 3
            )
            assert (
                _delta(
                    counters_after,
                    counters_before,
                    ("generation_jobs_total", "queued"),
                )
                == 4
            )
            assert (
                _delta(
                    counters_after,
                    counters_before,
                    ("generation_jobs_total", "failed"),
                )
                == 1
            )
            assert (
                _delta(
                    counters_after,
                    counters_before,
                    ("generation_retries_total", "timeout"),
                )
                == 2
            )
            assert _delta(stages_after, stages_before, "outline") == 2
            assert _delta(stages_after, stages_before, "content") == 2
        finally:
            await writer.dispose()
            await reader.dispose()

    asyncio.run(scenario())


def test_reaper_release_conflict_does_not_increment_released_quota(
    generation_database: Any,
) -> None:
    tenant_id = "metric-reaper-conflict-tenant"
    job_id = "metric-reaper-conflict"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        reader = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _insert_active_shared_tenant(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            scheduler = FairScheduler(engine)
            await scheduler.ensure_generation_capacity(
                (tenant_id,),
                worker_pool_ref="shared-generation",
            )
            await repository.grant_quota(tenant_id, grant_id="grant-reaper-metrics", units=20)
            await repository.create_job_and_reserve(_request(tenant_id, job_id, max_attempts=1))
            assert await OutboxDispatcher(engine).dispatch_next() is not None
            claim = await _claim(scheduler)
            expired_at = datetime.now(UTC) - timedelta(seconds=5)
            translated = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            session_factory = async_sessionmaker(translated, expire_on_commit=False)
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(GenerationJob)
                        .where(
                            GenerationJob.tenant_id == tenant_id,
                            GenerationJob.id == job_id,
                        )
                        .values(lease_expires_at=expired_at)
                    )
                    await session.execute(
                        update(GenerationQueue)
                        .where(
                            GenerationQueue.tenant_id == tenant_id,
                            GenerationQueue.job_id == job_id,
                        )
                        .values(lease_expires_at=expired_at)
                    )
                    await session.execute(
                        update(GenerationSlot)
                        .where(
                            GenerationSlot.claimed_tenant_id == tenant_id,
                            GenerationSlot.claimed_job_id == job_id,
                        )
                        .values(lease_expires_at=expired_at)
                    )
                    session.add(
                        QuotaLedger(
                            id=_quota_event_id("release", job_id),
                            tenant_id=tenant_id,
                            job_id=job_id,
                            entry_type="release",
                            units=3,
                        )
                    )
            before = await _counter_totals(reader)

            reaped = await repository.reap_one_expired()

            assert reaped is not None and reaped.terminal_status == "failed"
            assert claim.attempt_count == 1
            after = await _counter_totals(reader)
            assert _delta(after, before, ("quota_units_total", "released")) == 0
            assert _delta(after, before, ("generation_jobs_total", "failed")) == 1
        finally:
            await engine.dispose()
            await reader.dispose()

    asyncio.run(scenario())
