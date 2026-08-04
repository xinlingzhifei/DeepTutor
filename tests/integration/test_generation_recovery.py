from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.dispatcher import OutboxDispatcher
from deeptutor.teaching.models import DataPlaneRoute, ProviderProfile, Tenant
from deeptutor.teaching.models.jobs import (
    ArtifactPromotionState,
    ClassroomArtifact,
    ClassroomVersion,
    GenerationJob,
    GenerationQueue,
    GenerationSlot,
    QuotaLedger,
)
from deeptutor.teaching.repositories.jobs import (
    GenerationJobRequest,
    MaterializedArtifactInput,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.scheduler import FairScheduler
from deeptutor.teaching.schema_names import tenant_schema_name
from tests.teaching_contract_fixtures import valid_classroom_document

pytest_plugins = ("tests.teaching.integration.conftest",)


def _document_payload(media_body: bytes) -> tuple[str, str, str]:
    raw = valid_classroom_document()
    raw["classroom_id"] = "classroom-recovery"
    raw["classroom_version_id"] = "classroom-recovery-v1"
    media = raw["media_manifest"][0]
    media["sha256"] = hashlib.sha256(media_body).hexdigest()
    media["size_bytes"] = len(media_body)
    raw["openmaic"]["scenes"][0]["actions"].append(
        {"type": "play_audio", "mediaId": media["media_id"]}
    )
    provisional = ClassroomDocument.model_validate(raw)
    normalized = provisional.model_dump(mode="json", by_alias=True, exclude_none=True)
    without_hash = dict(normalized)
    without_hash.pop("fileSha256")
    normalized["fileSha256"] = hashlib.sha256(canonical_json_bytes(without_hash)).hexdigest()
    document = ClassroomDocument.model_validate(normalized)
    payload = canonical_json_bytes(document).decode()
    media_sha256 = hashlib.sha256(canonical_json_bytes(normalized["mediaManifest"])).hexdigest()
    return payload, hashlib.sha256(payload.encode()).hexdigest(), media_sha256


async def _seed_binding(engine, tenant_id: str) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=tenant_id,
                    name="Generation recovery tenant",
                    status="active",
                    data_plane_mode="shared",
                )
            )
            await session.flush()
            await session.execute(
                insert(ProviderProfile)
                .values(
                    id="recovery-provider",
                    scope="shared",
                    tenant_id=None,
                    owner_key="shared",
                    provider_type="openai-compatible",
                    model_name="recovery-model",
                    api_base_url=None,
                    secret_ref="tests/recovery/provider",
                    status="active",
                )
                .on_conflict_do_nothing(index_elements=[ProviderProfile.id])
            )
            await session.execute(
                insert(DataPlaneRoute)
                .values(
                    id="recovery-route",
                    tenant_id=None,
                    owner_key="shared",
                    mode="shared",
                    base_url="http://openmaic.invalid",
                    worker_pool="recovery-workers",
                    queue_name="openmaic.recovery",
                    provider_profile_id="recovery-provider",
                    status="active",
                    health_status="healthy",
                )
                .on_conflict_do_nothing(index_elements=[DataPlaneRoute.id])
            )


async def _make_due(engine, tenant_id: str, job_id: str) -> None:
    translated = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    factory = async_sessionmaker(translated, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.now()))
            await session.execute(
                update(GenerationJob).where(GenerationJob.id == job_id).values(next_attempt_at=now)
            )
            await session.execute(
                update(GenerationQueue)
                .where(
                    GenerationQueue.tenant_id == tenant_id,
                    GenerationQueue.job_id == job_id,
                )
                .values(available_at=now)
            )


async def _expire_claim(engine, tenant_id: str, job_id: str) -> None:
    translated = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    factory = async_sessionmaker(translated, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            expired_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(lease_expires_at=expired_at, heartbeat_at=expired_at)
            )
            await session.execute(
                update(GenerationQueue)
                .where(
                    GenerationQueue.tenant_id == tenant_id,
                    GenerationQueue.job_id == job_id,
                )
                .values(lease_expires_at=expired_at, heartbeat_at=expired_at)
            )
            await session.execute(
                update(GenerationSlot)
                .where(
                    GenerationSlot.claimed_tenant_id == tenant_id,
                    GenerationSlot.claimed_job_id == job_id,
                )
                .values(lease_expires_at=expired_at, heartbeat_at=expired_at)
            )


def test_outline_completion_uses_monotonic_whole_job_progress(
    generation_database,
) -> None:
    tenant_id = "outline-progress"
    job_id = "outline-progress-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _seed_binding(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(tenant_id, grant_id="outline-progress-grant", units=10)
            payload = "{}"
            await repository.create_job_and_reserve(
                GenerationJobRequest(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    job_kind="generation",
                    phase="outline",
                    export_format=None,
                    priority="teacher",
                    quota_units=2,
                    actor_id="teacher-progress",
                    owner_id="teacher-progress",
                    visibility="private",
                    request_id="request-outline-progress",
                    idempotency_key="idempotency-outline-progress",
                    request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                    data_plane_route_id="recovery-route",
                    provider_profile_id="recovery-provider",
                    worker_pool_ref="recovery-workers",
                    queue_ref="openmaic.recovery",
                    request_payload=payload,
                )
            )
            assert await OutboxDispatcher(engine).dispatch_next() is not None
            scheduler = FairScheduler(engine)
            await scheduler.ensure_generation_capacity(
                (tenant_id,),
                worker_pool_ref="recovery-workers",
            )
            claim = await scheduler.claim(
                "generation",
                data_plane_route_id="recovery-route",
                provider_profile_id="recovery-provider",
                worker_pool_ref="recovery-workers",
                queue_ref="openmaic.recovery",
                worker_id="outline-progress-worker",
                lease_seconds=60,
            )
            assert claim is not None

            await repository.complete_outline(claim, result_payload="{}")

            details = await repository.get_job_details(tenant_id, job_id)
            assert details is not None
            assert details.status == "awaiting_confirmation"
            assert details.progress_percent == 50
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_expired_lease_and_object_commit_are_recovered_exactly_once(
    generation_database,
) -> None:
    tenant_id = "generation-recovery"
    job_id = "recovery-job"
    generation_database.migrate_tenant(tenant_id)

    async def scenario() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            await _seed_binding(engine, tenant_id)
            repository = SqlAlchemyGenerationJobRepository(engine)
            await repository.grant_quota(tenant_id, grant_id="recovery-grant", units=20)
            payload = "{}"
            await repository.create_job_and_reserve(
                GenerationJobRequest(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    job_kind="generation",
                    phase="content",
                    export_format=None,
                    priority="teacher",
                    quota_units=5,
                    actor_id="teacher-recovery",
                    owner_id="teacher-recovery",
                    visibility="private",
                    request_id="request-recovery",
                    idempotency_key="idempotency-recovery",
                    request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                    data_plane_route_id="recovery-route",
                    provider_profile_id="recovery-provider",
                    worker_pool_ref="recovery-workers",
                    queue_ref="openmaic.recovery",
                    request_payload=payload,
                )
            )
            assert await OutboxDispatcher(engine).dispatch_next() is not None
            scheduler = FairScheduler(engine)
            await scheduler.ensure_generation_capacity(
                (tenant_id,),
                worker_pool_ref="recovery-workers",
            )
            claim1 = await scheduler.claim(
                "generation",
                data_plane_route_id="recovery-route",
                provider_profile_id="recovery-provider",
                worker_pool_ref="recovery-workers",
                queue_ref="openmaic.recovery",
                worker_id="worker-crashed-before-validation",
                lease_seconds=60,
            )
            assert claim1 is not None and claim1.attempt_count == 1
            renewed_until = await repository.heartbeat_claim(claim1, lease_seconds=60)
            translated = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            heartbeat_factory = async_sessionmaker(translated, expire_on_commit=False)
            async with heartbeat_factory() as heartbeat_session:
                heartbeat_job = await heartbeat_session.get(GenerationJob, job_id)
                heartbeat_queue = await heartbeat_session.scalar(
                    select(GenerationQueue).where(GenerationQueue.job_id == job_id)
                )
                heartbeat_slots = (
                    await heartbeat_session.scalars(
                        select(GenerationSlot).where(GenerationSlot.claimed_job_id == job_id)
                    )
                ).all()
                assert heartbeat_job is not None and heartbeat_queue is not None
                assert len(heartbeat_slots) == 2
                assert {
                    heartbeat_job.lease_expires_at,
                    heartbeat_queue.lease_expires_at,
                    *(slot.lease_expires_at for slot in heartbeat_slots),
                } == {renewed_until}

            await _expire_claim(engine, tenant_id, job_id)
            reaped1 = await repository.reap_one_expired()
            assert reaped1 is not None and reaped1.terminal_status is None
            assert await repository.reap_one_expired() is None
            await _make_due(engine, tenant_id, job_id)
            claim2 = await scheduler.claim(
                "generation",
                data_plane_route_id="recovery-route",
                provider_profile_id="recovery-provider",
                worker_pool_ref="recovery-workers",
                queue_ref="openmaic.recovery",
                worker_id="worker-crashed-after-object-commit",
                lease_seconds=60,
            )
            assert claim2 is not None and claim2.attempt_count == 2
            await repository.transition_claim(
                claim2,
                expected_status="generating_content",
                target_status="validating",
                progress_percent=80,
            )
            await repository.transition_claim(
                claim2,
                expected_status="validating",
                target_status="materializing",
                progress_percent=90,
            )
            target = await repository.prepare_promotion(
                claim2,
                classroom_id="classroom-recovery",
            )
            manifest_sha256 = "c" * 64
            await repository.bind_promotion_manifest(
                claim2,
                manifest_sha256=manifest_sha256,
            )
            await repository.mark_object_committed(
                claim2,
                manifest_sha256=manifest_sha256,
            )

            await _expire_claim(engine, tenant_id, job_id)
            reaped2 = await repository.reap_one_expired()
            assert reaped2 is not None and reaped2.terminal_status is None
            await _make_due(engine, tenant_id, job_id)
            claim3 = await scheduler.claim(
                "generation",
                data_plane_route_id="recovery-route",
                provider_profile_id="recovery-provider",
                worker_pool_ref="recovery-workers",
                queue_ref="openmaic.recovery",
                worker_id="worker-recovered",
                lease_seconds=60,
            )
            assert claim3 is not None and claim3.attempt_count == 3
            await repository.transition_claim(
                claim3,
                expected_status="generating_content",
                target_status="validating",
                progress_percent=80,
            )
            await repository.transition_claim(
                claim3,
                expected_status="validating",
                target_status="materializing",
                progress_percent=90,
            )
            recovered_target = await repository.prepare_promotion(
                claim3,
                classroom_id="classroom-recovery",
            )
            assert recovered_target.version_number == target.version_number
            assert recovered_target.status == "object_committed"

            document_key = (
                f"tenants/{tenant_id}/classrooms/classroom-recovery/"
                f"versions/{target.version_number}/classroom.json"
            )
            media_key = (
                f"tenants/{tenant_id}/classrooms/classroom-recovery/"
                f"versions/{target.version_number}/media/voice.mp3"
            )
            media_body = b"recovered-media" * 4
            document_payload, document_sha256, media_manifest_sha256 = _document_payload(media_body)
            await repository.finalize_generation(
                claim3,
                classroom_version_id="classroom-recovery-v1",
                document_payload=document_payload,
                document_sha256=document_sha256,
                media_manifest_sha256=media_manifest_sha256,
                manifest_sha256=manifest_sha256,
                artifacts=(
                    MaterializedArtifactInput(
                        relative_name="classroom.json",
                        object_key=document_key,
                        sha256=document_sha256,
                        size_bytes=len(document_payload.encode()),
                        mime_type="application/json",
                        artifact_kind="dsl_json",
                    ),
                    MaterializedArtifactInput(
                        relative_name="media/voice.mp3",
                        object_key=media_key,
                        sha256=hashlib.sha256(media_body).hexdigest(),
                        size_bytes=len(media_body),
                        mime_type="audio/mpeg",
                        artifact_kind="media",
                    ),
                ),
            )

            translated = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            factory = async_sessionmaker(translated, expire_on_commit=False)
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(ClassroomVersion)) == 1
                assert (
                    await session.scalar(select(func.count()).select_from(ClassroomArtifact)) == 2
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(QuotaLedger)
                        .where(
                            QuotaLedger.job_id == job_id,
                            QuotaLedger.entry_type == "settle",
                        )
                    )
                    == 1
                )
                job = await session.get(GenerationJob, job_id)
                state = await session.get(ArtifactPromotionState, job_id)
                assert job is not None and job.status == "succeeded"
                assert job.attempt_count == 3
                assert job.lease_token is None
                assert state is not None and state.status == "finalized"
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(GenerationQueue)
                        .where(GenerationQueue.job_id == job_id)
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(GenerationSlot)
                        .where(GenerationSlot.claimed_job_id == job_id)
                    )
                    == 0
                )
            assert await repository.request_cancel(tenant_id, job_id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
