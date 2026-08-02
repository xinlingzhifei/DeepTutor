from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.models import AuditLog, Tenant
from deeptutor.teaching.models.classrooms import (
    ClassroomAsset,
    ClassroomVersion,
    Publication,
)
from deeptutor.teaching.models.jobs import GenerationJob
from deeptutor.teaching.repositories.classrooms import (
    ClassroomDocumentReference,
    ImmutableVersionError,
    PublishedClassroomVersion,
    SqlAlchemyClassroomRepository,
)
from deeptutor.teaching.schema_names import tenant_schema_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    repository: SqlAlchemyClassroomRepository
    engine: object
    tenant_id: str
    asset_id: str
    generation_job_id: str


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
            session.add(
                GenerationJob(
                    id=generation_job_id,
                    tenant_id=tenant_id,
                    job_kind="generation",
                    phase="content",
                    export_format=None,
                    status="succeeded",
                    priority=10,
                    quota_units=1,
                    actor_id="teacher-1",
                    owner_id="teacher-1",
                    visibility="private",
                    request_id=f"request-{suffix}",
                    idempotency_key=f"idempotency-{suffix}",
                    request_sha256="1" * 64,
                    data_plane_route_id="test-route",
                    provider_profile_id="test-provider",
                    worker_pool_ref="test-workers",
                    queue_ref="test-queue",
                    request_payload="{}",
                    progress_percent=100,
                    attempt_count=0,
                    max_attempts=5,
                    cancel_requested=False,
                    dsl_repair_attempts=0,
                )
            )
            session.add(
                ClassroomAsset(
                    id=asset_id,
                    tenant_id=tenant_id,
                    owner_id="teacher-1",
                    title="Immutable classroom",
                    lifecycle_state="approved",
                )
            )

    context = RepositoryContext(
        repository=SqlAlchemyClassroomRepository(engine, tenant_id),
        engine=engine,
        tenant_id=tenant_id,
        asset_id=asset_id,
        generation_job_id=generation_job_id,
    )
    try:
        yield context
    finally:
        await engine.dispose()


def valid_version(context: RepositoryContext) -> PublishedClassroomVersion:
    return PublishedClassroomVersion(
        id=f"{context.asset_id}:v1",
        classroom_id=context.asset_id,
        version_number=1,
        generation_job_id=context.generation_job_id,
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
                session.add(
                    ClassroomVersion(
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

    assert asset is not None
    assert asset.lifecycle_state == "published"
    assert asset.current_published_version_id == version.id
    assert publication is not None
    assert publication.classroom_id == repository_context.asset_id
    assert publication.actor_id == "teacher-1"
    assert audit is not None


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
