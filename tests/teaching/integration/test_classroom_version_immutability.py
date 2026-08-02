from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.dispatcher import OutboxDispatcher
from deeptutor.teaching.models import (
    AuditLog,
    DataPlaneRoute,
    ProviderProfile,
    Tenant,
)
from deeptutor.teaching.models.classrooms import (
    ClassroomAsset,
    ClassroomVersion,
    Publication,
    transition,
)
from deeptutor.teaching.models.jobs import GenerationJob
from deeptutor.teaching.repositories.classrooms import (
    ClassroomDocumentReference,
    ImmutableVersionError,
    PublishedClassroomVersion,
    SqlAlchemyClassroomRepository,
)
from deeptutor.teaching.repositories.jobs import (
    GenerationJobRequest,
    MaterializedArtifactInput,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.scheduler import FairScheduler
from deeptutor.teaching.schema_names import tenant_schema_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROVIDER_ID = "classroom-lifecycle-provider"
ROUTE_ID = "classroom-lifecycle-route"
WORKER_POOL = "classroom-lifecycle-workers"
QUEUE_REF = "openmaic.classroom.lifecycle"


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    repository: SqlAlchemyClassroomRepository
    engine: AsyncEngine
    tenant_id: str
    asset_id: str
    generation_job_id: str
    source_version_id: str


@pytest_asyncio.fixture
async def repository_context(generation_database) -> RepositoryContext:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"classroom-{suffix}"
    asset_id = f"asset-{suffix}"
    generation_job_id = f"job-{suffix}"
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    translated = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=tenant_id,
                    name="Classroom lifecycle test tenant",
                    status="active",
                    data_plane_mode="shared",
                )
            )
            await session.flush()
            await session.execute(
                insert(ProviderProfile)
                .values(
                    id=PROVIDER_ID,
                    scope="shared",
                    tenant_id=None,
                    owner_key="shared",
                    provider_type="openai-compatible",
                    model_name="classroom-test-model",
                    api_base_url=None,
                    secret_ref="tests/classroom/provider",
                    status="active",
                )
                .on_conflict_do_nothing(index_elements=[ProviderProfile.id])
            )
            await session.execute(
                insert(DataPlaneRoute)
                .values(
                    id=ROUTE_ID,
                    tenant_id=None,
                    owner_key="shared",
                    mode="shared",
                    base_url="http://openmaic.invalid",
                    worker_pool=WORKER_POOL,
                    queue_name=QUEUE_REF,
                    provider_profile_id=PROVIDER_ID,
                    status="active",
                    health_status="healthy",
                )
                .on_conflict_do_nothing(index_elements=[DataPlaneRoute.id])
            )

    generation_repository = SqlAlchemyGenerationJobRepository(engine)
    await generation_repository.grant_quota(
        tenant_id,
        grant_id=f"grant-{suffix}",
        units=10,
    )
    payload = "{}"
    await generation_repository.create_job_and_reserve(
        GenerationJobRequest(
            tenant_id=tenant_id,
            job_id=generation_job_id,
            job_kind="generation",
            phase="content",
            export_format=None,
            priority="teacher",
            quota_units=1,
            actor_id="teacher-1",
            owner_id="teacher-1",
            visibility="private",
            request_id=f"request-{suffix}",
            idempotency_key=f"idempotency-{suffix}",
            request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
            data_plane_route_id=ROUTE_ID,
            provider_profile_id=PROVIDER_ID,
            worker_pool_ref=WORKER_POOL,
            queue_ref=QUEUE_REF,
            request_payload=payload,
        )
    )
    assert await OutboxDispatcher(engine).dispatch_next() is not None
    scheduler = FairScheduler(engine)
    await scheduler.ensure_generation_capacity(
        (tenant_id,),
        worker_pool_ref=WORKER_POOL,
    )
    claim = await scheduler.claim(
        "generation",
        data_plane_route_id=ROUTE_ID,
        provider_profile_id=PROVIDER_ID,
        worker_pool_ref=WORKER_POOL,
        queue_ref=QUEUE_REF,
        worker_id=f"worker-{suffix}",
        lease_seconds=60,
    )
    assert claim is not None
    await generation_repository.transition_claim(
        claim,
        expected_status="generating_content",
        target_status="validating",
        progress_percent=80,
    )
    await generation_repository.transition_claim(
        claim,
        expected_status="validating",
        target_status="materializing",
        progress_percent=90,
    )
    target = await generation_repository.prepare_promotion(
        claim,
        classroom_id=asset_id,
    )
    manifest_sha256 = "a" * 64
    await generation_repository.bind_promotion_manifest(
        claim,
        manifest_sha256=manifest_sha256,
    )
    await generation_repository.mark_object_committed(
        claim,
        manifest_sha256=manifest_sha256,
    )
    source_version_id = f"{asset_id}:generated-v{target.version_number}"
    document = ClassroomDocumentReference(
        sha256="2" * 64,
        media_manifest_sha256="3" * 64,
        object_key=f"classrooms/{asset_id}/generated-v1/classroom.json",
    )
    await generation_repository.finalize_generation(
        claim,
        classroom_version_id=source_version_id,
        document_sha256=document.sha256,
        media_manifest_sha256=document.media_manifest_sha256,
        manifest_sha256=manifest_sha256,
        artifacts=(
            MaterializedArtifactInput(
                relative_name="classroom.json",
                object_key=document.object_key,
                sha256=document.sha256,
                size_bytes=128,
                mime_type="application/json",
                artifact_kind="dsl_json",
            ),
        ),
    )

    async with session_factory() as session:
        async with session.begin():
            source_version = await session.get(ClassroomVersion, source_version_id)
            asset = await session.get(ClassroomAsset, asset_id)
            assert source_version is not None
            assert source_version.generation_job_id == generation_job_id
            assert asset is not None
            asset.lifecycle_state = transition(asset.lifecycle_state, "validated")
            asset.lifecycle_state = transition(asset.lifecycle_state, "approved")

    context = RepositoryContext(
        repository=SqlAlchemyClassroomRepository(engine, tenant_id),
        engine=engine,
        tenant_id=tenant_id,
        asset_id=asset_id,
        generation_job_id=generation_job_id,
        source_version_id=source_version_id,
    )
    try:
        yield context
    finally:
        await engine.dispose()


def valid_version(context: RepositoryContext) -> PublishedClassroomVersion:
    return PublishedClassroomVersion(
        id=f"{context.asset_id}:published-v2",
        classroom_id=context.asset_id,
        version_number=2,
        source_version_id=context.source_version_id,
        document=ClassroomDocumentReference(
            sha256="2" * 64,
            media_manifest_sha256="3" * 64,
            object_key=f"classrooms/{context.asset_id}/v1/classroom.json",
        ),
        publication_id=f"publication-{context.asset_id}",
        actor_id="teacher-1",
        scope="tenant",
    )


def changed_document(context: RepositoryContext) -> ClassroomDocumentReference:
    return ClassroomDocumentReference(
        sha256="4" * 64,
        media_manifest_sha256="5" * 64,
        object_key=f"classrooms/{context.asset_id}/v1/changed.json",
    )


def migrate_tenant_revision(generation_database, tenant_id: str, revision: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            "scope=tenant",
            "-x",
            f"tenant_schema={tenant_schema_name(tenant_id)}",
            "upgrade",
            revision,
        ],
        cwd=PROJECT_ROOT,
        env=generation_database.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"


@pytest.mark.asyncio
async def test_plan02_versions_are_backfilled_to_stable_assets(generation_database) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"backfill-{suffix}"
    classroom_id = f"legacy-classroom-{suffix}"
    job_id = f"legacy-job-{suffix}"
    migrate_tenant_revision(generation_database, tenant_id, "20260801_0007")
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    translated = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    try:
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    Tenant(
                        id=tenant_id,
                        name="Plan 02 backfill tenant",
                        status="active",
                        data_plane_mode="shared",
                    )
                )
                await session.flush()
                session.add(
                    GenerationJob(
                        id=job_id,
                        tenant_id=tenant_id,
                        job_kind="generation",
                        phase="content",
                        export_format=None,
                        status="succeeded",
                        priority=10,
                        quota_units=1,
                        actor_id="legacy-teacher",
                        owner_id="legacy-owner",
                        visibility="private",
                        request_id=f"legacy-request-{suffix}",
                        idempotency_key=f"legacy-idempotency-{suffix}",
                        request_sha256="6" * 64,
                        data_plane_route_id="legacy-route",
                        provider_profile_id="legacy-provider",
                        worker_pool_ref="legacy-workers",
                        queue_ref="legacy-queue",
                        request_payload="{}",
                        progress_percent=100,
                        attempt_count=0,
                        max_attempts=5,
                        cancel_requested=False,
                        dsl_repair_attempts=0,
                    )
                )
                await session.flush()
                await session.execute(
                    insert(ClassroomVersion.__table__).values(
                        id=f"{classroom_id}:v1",
                        tenant_id=tenant_id,
                        classroom_id=classroom_id,
                        version_number=1,
                        generation_job_id=job_id,
                        document_sha256="7" * 64,
                        media_manifest_sha256="8" * 64,
                        document_object_key=f"classrooms/{classroom_id}/v1/classroom.json",
                    )
                )

        migrate_tenant_revision(generation_database, tenant_id, "head")

        async with session_factory() as session:
            asset = await session.get(ClassroomAsset, classroom_id)
            version = await session.get(ClassroomVersion, f"{classroom_id}:v1")

        assert asset is not None
        assert asset.tenant_id == tenant_id
        assert asset.owner_id == "legacy-owner"
        assert asset.lifecycle_state == "editing"
        assert version is not None
        assert version.classroom_id == asset.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publishing_creates_version_publication_audit_and_pointer(
    repository_context: RepositoryContext,
) -> None:
    version = await repository_context.repository.insert_published_version(
        valid_version(repository_context)
    )

    translated = repository_context.engine.execution_options(
        schema_translate_map={
            "tenant": tenant_schema_name(repository_context.tenant_id),
        }
    )
    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    async with session_factory() as session:
        asset = await session.get(ClassroomAsset, repository_context.asset_id)
        source_version = await session.get(
            ClassroomVersion,
            repository_context.source_version_id,
        )
        published_version = await session.get(ClassroomVersion, version.id)
        publication = await session.scalar(
            select(Publication).where(Publication.classroom_version_id == version.id)
        )
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.tenant_id == repository_context.tenant_id,
                AuditLog.action == "teaching.classroom.published",
                AuditLog.resource_id == version.id,
            )
        )
        version_count = await session.scalar(select(func.count()).select_from(ClassroomVersion))

    assert asset is not None
    assert asset.lifecycle_state == "published"
    assert asset.current_published_version_id == version.id
    assert publication is not None
    assert publication.classroom_id == repository_context.asset_id
    assert publication.actor_id == "teacher-1"
    assert audit is not None
    assert source_version is not None
    assert source_version.generation_job_id == repository_context.generation_job_id
    assert source_version.source_version_id is None
    assert published_version is not None
    assert published_version.generation_job_id is None
    assert published_version.source_version_id == source_version.id
    assert version_count == 2


@pytest.mark.asyncio
async def test_publication_rejects_a_missing_materialized_source_version(
    repository_context: RepositoryContext,
) -> None:
    published = replace(
        valid_version(repository_context),
        source_version_id="missing-source-version",
    )

    with pytest.raises(ValueError, match="source classroom version does not exist"):
        await repository_context.repository.insert_published_version(published)

    translated = repository_context.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(repository_context.tenant_id)}
    )
    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    async with session_factory() as session:
        asset = await session.get(ClassroomAsset, repository_context.asset_id)
        publication_count = await session.scalar(select(func.count()).select_from(Publication))
    assert asset is not None and asset.lifecycle_state == "approved"
    assert asset.current_published_version_id is None
    assert publication_count == 0


@pytest.mark.asyncio
async def test_publication_rejects_a_source_version_from_another_asset(
    repository_context: RepositoryContext,
) -> None:
    other_asset_id = f"other-{repository_context.asset_id}"
    translated = repository_context.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(repository_context.tenant_id)}
    )
    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                ClassroomAsset(
                    id=other_asset_id,
                    tenant_id=repository_context.tenant_id,
                    owner_id="teacher-1",
                    title="Other classroom",
                    lifecycle_state="approved",
                )
            )
    published = replace(
        valid_version(repository_context),
        id=f"{other_asset_id}:published-v1",
        classroom_id=other_asset_id,
        version_number=1,
        publication_id=f"publication-{other_asset_id}",
    )

    with pytest.raises(ValueError, match="source classroom version belongs to another asset"):
        await repository_context.repository.insert_published_version(published)

    async with session_factory() as session:
        asset = await session.get(ClassroomAsset, other_asset_id)
        publication_count = await session.scalar(select(func.count()).select_from(Publication))
    assert asset is not None and asset.lifecycle_state == "approved"
    assert asset.current_published_version_id is None
    assert publication_count == 0


@pytest.mark.asyncio
async def test_published_version_cannot_be_updated(
    repository_context: RepositoryContext,
) -> None:
    version = await repository_context.repository.insert_published_version(
        valid_version(repository_context)
    )

    with pytest.raises(ImmutableVersionError):
        await repository_context.repository.replace_document(
            version.id,
            changed_document(repository_context),
        )


@pytest.mark.asyncio
async def test_published_version_cannot_be_deleted(
    repository_context: RepositoryContext,
) -> None:
    version = await repository_context.repository.insert_published_version(
        valid_version(repository_context)
    )

    with pytest.raises(ImmutableVersionError):
        await repository_context.repository.delete_version(version.id)
