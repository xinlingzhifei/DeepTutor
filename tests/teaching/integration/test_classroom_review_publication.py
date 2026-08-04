"""PostgreSQL proof for durable review, publication, and assignment migration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.teaching.artifacts import (
    ArtifactManifestEntry,
    ClassroomArtifactManifest,
    classroom_artifact_key,
)
from deeptutor.teaching.brief_builder import (
    KnowledgePointSpec,
    TeachingBriefBuilder,
    TeachingBriefSpec,
)
from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.models import AuditLog, Tenant
from deeptutor.teaching.models.classrooms import (
    Approval,
    Assignment,
    AssignmentMigration,
    ClassLearningState,
    ClassroomAsset,
    ClassroomDraft,
    ClassroomPublicationMaterialization,
    ClassroomReviewRequest,
    ClassroomVersion,
    Publication,
    TeachingBrief,
)
from deeptutor.teaching.models.jobs import (
    ArtifactPromotionState,
    ClassroomArtifact,
    GenerationJob,
)
from deeptutor.teaching.models.tenant import Course, TeachingClass
from deeptutor.teaching.object_store import (
    ClassroomArtifactPromotionService,
    LocalClassroomArtifactStore,
)
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.repositories.classrooms import SqlAlchemyClassroomRepository
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.classrooms import ClassroomService
from deeptutor.teaching.services.publication_materializer import (
    ClassroomPublicationMaterializer,
)
from deeptutor.teaching.services.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from deeptutor.teaching.services.publications import (
    ActiveLearningConflict,
    AssignCommand,
    MigrateAssignmentCommand,
    PublicationConflict,
    PublicationService,
    PublicationValidationStale,
)
from deeptutor.teaching.services.review_repository import SqlAlchemyReviewRepository
from deeptutor.teaching.services.reviews import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewPolicy,
    ReviewService,
    ReviewValidationStale,
    SubmitReviewCommand,
)
from deeptutor.teaching.tenant_context import TenantContext
from tests.teaching_contract_fixtures import valid_classroom_document


def _canonical_document(
    asset_id: str,
    version_id: str,
    *,
    title: str,
    text_content: str,
    media_id: str | None = None,
    media_body: bytes | None = None,
) -> dict[str, object]:
    payload = valid_classroom_document()
    payload["classroom_id"] = asset_id
    payload["classroom_version_id"] = version_id
    payload["content_mode"] = "open_creation"
    payload["open_creation"] = True
    payload["source_refs"] = []
    payload["export_manifest"] = []
    mappings = payload["knowledge_point_mappings"]
    assert isinstance(mappings, list) and isinstance(mappings[0], dict)
    mappings[0]["knowledge_point_id"] = "kp-motion"
    mappings[0]["source_refs"] = []
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list) and isinstance(scenes[0], dict)
    scenes[0]["title"] = title
    scenes[0]["content"] = {
        "type": "slide",
        "canvas": {"text": text_content},
    }
    if media_id is None:
        payload["media_manifest"] = []
    else:
        assert media_body is not None
        manifest = payload["media_manifest"]
        assert isinstance(manifest, list) and isinstance(manifest[0], dict)
        manifest[0].update(
            media_id=media_id,
            relative_path=f"media/{media_id}.png",
            mime_type="image/png",
            sha256=hashlib.sha256(media_body).hexdigest(),
            size_bytes=len(media_body),
            temporary_download_path=f"downloads/media/{media_id}.png",
        )
        canvas = scenes[0]["content"]["canvas"]
        canvas["mediaId"] = media_id
    provisional = ClassroomDocument.model_validate(payload)
    normalized = provisional.model_dump(mode="json", by_alias=True, exclude_none=True)
    without_hash = dict(normalized)
    without_hash.pop("fileSha256")
    normalized["fileSha256"] = hashlib.sha256(canonical_json_bytes(without_hash)).hexdigest()
    return ClassroomDocument.model_validate(normalized).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )


@dataclass(frozen=True, slots=True)
class ReviewDatabase:
    engine: AsyncEngine
    tenant_id: str
    asset_id: str
    draft_id: str
    source_version_id: str
    document_sha256: str
    validation_sha256: str
    store: LocalClassroomArtifactStore


async def _body(payload: bytes):
    yield payload


async def _read_all(body) -> bytes:
    return b"".join([chunk async for chunk in body])


class _StoreProvider:
    def __init__(self, store: LocalClassroomArtifactStore) -> None:
        self._store = store

    async def store_for_tenant(self, _tenant_id: str):
        return self._store


class _LoseFirstMaterializationResponse:
    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._lost = False

    async def materialize(self, plan):
        confirmed = await self._delegate.materialize(plan)
        if not self._lost:
            self._lost = True
            raise RuntimeError("materialization response lost")
        return confirmed


class _ChangeDraftAfterMaterialization:
    def __init__(
        self,
        delegate,
        database: ReviewDatabase,
        document: dict[str, object],
    ) -> None:
        self._delegate = delegate
        self._database = database
        self._document = document

    async def materialize(self, plan):
        confirmed = await self._delegate.materialize(plan)
        translated = self._database.engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(self._database.tenant_id)}
        )
        sessions = async_sessionmaker(translated, expire_on_commit=False)
        async with sessions() as session:
            async with session.begin():
                draft = await session.scalar(
                    select(ClassroomDraft)
                    .where(ClassroomDraft.id == self._database.draft_id)
                    .with_for_update()
                )
                assert draft is not None
                document = canonical_json_bytes(self._document).decode()
                draft.revision += 1
                draft.document = document
                draft.document_sha256 = hashlib.sha256(document.encode()).hexdigest()
                draft.validation_report = None
                draft.validation_report_sha256 = None
                draft.validation_revision = None
                draft.validation_document_sha256 = None
        return confirmed


class _FlipPolicyAfterServiceRead:
    def __init__(self, delegate: SqlAlchemyReviewRepository) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def get_policy(self) -> ReviewPolicy:
        stale = await self._delegate.get_policy()
        await self._delegate.set_policy(
            ReviewPolicy(prohibit_self_review=True),
            actor_id="policy-admin",
        )
        return stale


class _Upload:
    def __init__(self, body: bytes, mime_type: str) -> None:
        self._body = BytesIO(body)
        self.content_type = mime_type

    async def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    async def close(self) -> None:
        self._body.close()


def _job(tenant_id: str, job_id: str, *, owner_id: str = "author-1") -> GenerationJob:
    now = datetime.now(timezone.utc)
    return GenerationJob(
        id=job_id,
        tenant_id=tenant_id,
        job_kind="generation",
        phase="content",
        export_format=None,
        status="succeeded",
        priority=100,
        quota_units=1,
        actor_id=owner_id,
        owner_id=owner_id,
        visibility="private",
        request_id=f"request-{job_id}",
        idempotency_key=f"generation-{job_id}",
        classroom_draft_id=None,
        batch_id=None,
        resource_course_id="course-a",
        resource_class_id="class-a",
        public_request_sha256=None,
        request_sha256="1" * 64,
        data_plane_route_id="route-a",
        provider_profile_id="provider-a",
        worker_pool_ref="workers-a",
        queue_ref="queue-a",
        request_payload="{}",
        progress_percent=100,
        waiting_reason=None,
        attempt_count=1,
        max_attempts=5,
        next_attempt_at=now,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        heartbeat_at=None,
        cancel_requested=False,
        error_category=None,
        error_code=None,
        result_ref=f"version-{job_id}",
        artifact_manifest_ref="2" * 64,
        result_payload="{}",
        retry_of_job_id=None,
        dsl_repair_attempts=0,
        started_at=now,
        canceled_at=None,
        completed_at=now,
    )


@pytest_asyncio.fixture
async def review_database(generation_database, tmp_path: Path) -> ReviewDatabase:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"review-{suffix}"
    asset_id = f"asset-{suffix}"
    draft_id = f"draft-{suffix}"
    job_id = f"job-{suffix}"
    source_version_id = f"source-{suffix}"
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    translated = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    sessions = async_sessionmaker(translated, expire_on_commit=False)
    generated_media = b"\x89PNG\r\n\x1a\ngenerated-media"
    source_document = _canonical_document(
        asset_id,
        source_version_id,
        title="Generated source",
        text_content="Original generated lesson",
        media_id="media-generated",
        media_body=generated_media,
    )
    document = canonical_json_bytes(source_document).decode()
    document_sha256 = hashlib.sha256(document.encode()).hexdigest()
    report = {
        "valid": True,
        "severeFindings": [],
        "warnings": [{"severity": "warning", "code": "needs_caption"}],
        "sections": {},
        "draftRevision": 1,
        "documentSha256": document_sha256,
    }
    report_document = canonical_json_bytes(report).decode()
    report_sha256 = hashlib.sha256(report_document.encode()).hexdigest()
    store = LocalClassroomArtifactStore(tmp_path / "object-store", tenant_id)
    source_manifest = ClassroomArtifactManifest(
        tenant_id=tenant_id,
        job_id=job_id,
        asset_id=asset_id,
        version=1,
        entries=(
            ArtifactManifestEntry(
                relative_name="classroom.json",
                content_type="application/json",
                sha256=document_sha256,
                size=len(document.encode()),
            ),
            ArtifactManifestEntry(
                relative_name="media/media-generated.png",
                content_type="image/png",
                sha256=hashlib.sha256(generated_media).hexdigest(),
                size=len(generated_media),
            ),
        ),
    )
    source_artifacts = await ClassroomArtifactPromotionService(store).promote(
        source_manifest,
        {
            "classroom.json": _body(document.encode()),
            "media/media-generated.png": _body(generated_media),
        },
    )
    source_artifacts_by_name = {
        item.key.rsplit("/", maxsplit=1)[-1]: item for item in source_artifacts
    }
    source_artifact = source_artifacts_by_name["classroom.json"]
    media_artifact = next(
        item for item in source_artifacts if item.key.endswith("/media/media-generated.png")
    )
    object_key = source_artifact.key
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
                template_id="template-1",
                template_version="1",
                knowledge_points=(
                    KnowledgePointSpec(
                        knowledge_point_id="kp-motion",
                        title="Motion",
                        description="Describe motion.",
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
        separators=(",", ":"),
        sort_keys=True,
    )
    async with sessions() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id, name="Review tenant", status="active"))
            await session.flush()
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
            session.add(Course(id="course-b", title="Course B", status="active"))
            await session.flush()
            session.add(
                TeachingClass(
                    id="class-b",
                    course_id="course-b",
                    name="Class B",
                    status="active",
                )
            )
            await session.flush()
            session.add(_job(tenant_id, job_id))
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
            session.add(
                ClassroomAsset(
                    id=asset_id,
                    tenant_id=tenant_id,
                    owner_id="author-1",
                    title="Lesson",
                    lifecycle_state="editing",
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
                    revision=1,
                    document=document,
                    document_sha256=document_sha256,
                    outline_document=None,
                    outline_sha256=None,
                    confirmed_outline_sha256=None,
                    validation_report=report_document,
                    validation_report_sha256=report_sha256,
                    validation_revision=1,
                    validation_document_sha256=document_sha256,
                    creation_idempotency_key=None,
                    creation_request_sha256=None,
                    created_by="author-1",
                    updated_by="author-1",
                )
            )
            await session.flush()
            session.add(
                ClassroomVersion(
                    id=source_version_id,
                    tenant_id=tenant_id,
                    classroom_id=asset_id,
                    version_number=1,
                    generation_job_id=job_id,
                    source_version_id=None,
                    document_sha256=document_sha256,
                    media_manifest_sha256=hashlib.sha256(
                        canonical_json_bytes(source_document["mediaManifest"])
                    ).hexdigest(),
                    document_object_key=object_key,
                )
            )
            draft = await session.get(ClassroomDraft, draft_id)
            assert draft is not None
            draft.base_version_id = source_version_id
            await session.flush()
            session.add(
                ArtifactPromotionState(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    classroom_id=asset_id,
                    version_number=1,
                    manifest_sha256="2" * 64,
                    status="finalized",
                    object_committed_at=datetime.now(timezone.utc),
                    finalized_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                ClassroomArtifact(
                    id=f"artifact-{suffix}",
                    tenant_id=tenant_id,
                    source_job_id=job_id,
                    classroom_version_id=source_version_id,
                    artifact_kind="dsl_json",
                    relative_name="classroom.json",
                    object_key=object_key,
                    sha256=document_sha256,
                    size_bytes=len(document.encode()),
                    mime_type="application/json",
                    input_document_sha256=None,
                    input_media_manifest_sha256=None,
                )
            )
            session.add(
                ClassroomArtifact(
                    id=f"artifact-media-{suffix}",
                    tenant_id=tenant_id,
                    source_job_id=job_id,
                    classroom_version_id=source_version_id,
                    artifact_kind="media",
                    relative_name="media/media-generated.png",
                    object_key=media_artifact.key,
                    sha256=hashlib.sha256(generated_media).hexdigest(),
                    size_bytes=len(generated_media),
                    mime_type="image/png",
                    input_document_sha256=None,
                    input_media_manifest_sha256=None,
                )
            )
    try:
        yield ReviewDatabase(
            engine=engine,
            tenant_id=tenant_id,
            asset_id=asset_id,
            draft_id=draft_id,
            source_version_id=source_version_id,
            document_sha256=document_sha256,
            validation_sha256=report_sha256,
            store=store,
        )
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(tenant_schema_name(tenant_id), cascade=True))
                await connection.execute(
                    text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()


def _context(
    database: ReviewDatabase,
    user_id: str,
    *permissions: str,
    class_id: str = "class-a",
) -> TenantContext:
    return TenantContext(
        tenant_id=database.tenant_id,
        schema_name=tenant_schema_name(database.tenant_id),
        user_id=user_id,
        permissions=frozenset(
            ScopedPermission(
                permission=permission,
                scope_type="class",
                scope_id=class_id,
                tenant_id=database.tenant_id,
            )
            for permission in permissions
        ),
    )


def _tenant_sessions(database: ReviewDatabase):
    translated = database.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(database.tenant_id)}
    )
    return async_sessionmaker(translated, expire_on_commit=False)


async def _wait_for_blocked_queries(
    database: ReviewDatabase,
    fragment: str,
    tasks: tuple[asyncio.Task[object], ...],
) -> None:
    for _ in range(200):
        if any(task.done() for task in tasks):
            results = await asyncio.gather(*tasks, return_exceptions=True)
            raise AssertionError(f"query escaped forced barrier: {results!r}")
        async with database.engine.connect() as connection:
            waiter_count = await connection.scalar(
                text(
                    "SELECT count(DISTINCT locks.pid) "
                    "FROM pg_locks AS locks "
                    "JOIN pg_stat_activity AS activity ON activity.pid = locks.pid "
                    "WHERE NOT locks.granted "
                    "AND activity.datname = current_database() "
                    "AND activity.query ILIKE :fragment"
                ),
                {"fragment": f"%{fragment}%"},
            )
        if waiter_count is not None and waiter_count >= len(tasks):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("concurrent queries did not reach the forced lock barrier")


async def _clone_review_target(
    database: ReviewDatabase,
    suffix: str,
) -> tuple[str, str]:
    asset_id = f"{database.asset_id}-{suffix}"
    draft_id = f"{database.draft_id}-{suffix}"
    sessions = _tenant_sessions(database)
    async with sessions() as session:
        async with session.begin():
            source = await session.get(ClassroomDraft, database.draft_id)
            assert source is not None
            session.add(
                ClassroomAsset(
                    id=asset_id,
                    tenant_id=database.tenant_id,
                    owner_id="author-1",
                    title=f"Lesson {suffix}",
                    lifecycle_state="editing",
                )
            )
            await session.flush()
            session.add(
                ClassroomDraft(
                    id=draft_id,
                    tenant_id=database.tenant_id,
                    classroom_id=asset_id,
                    generation_job_id=source.generation_job_id,
                    teaching_brief_id=source.teaching_brief_id,
                    base_version_id=None,
                    revision=source.revision,
                    document=source.document,
                    document_sha256=source.document_sha256,
                    outline_document=source.outline_document,
                    outline_sha256=source.outline_sha256,
                    confirmed_outline_sha256=source.confirmed_outline_sha256,
                    validation_report=source.validation_report,
                    validation_report_sha256=source.validation_report_sha256,
                    validation_revision=source.validation_revision,
                    validation_document_sha256=source.validation_document_sha256,
                    creation_idempotency_key=None,
                    creation_request_sha256=None,
                    created_by="author-1",
                    updated_by="author-1",
                )
            )
    return asset_id, draft_id


async def _approve_edited_draft(
    database: ReviewDatabase,
    document: dict[str, object],
    *,
    submit_key: str,
):
    context = _context(
        database,
        "author-1",
        "classroom.edit",
        "classroom.submit",
    )
    classrooms = ClassroomService(
        SqlAlchemyClassroomRepository(database.engine, database.tenant_id),
        object(),
        object(),
        None,
    )
    updated = await classrooms.update_draft(
        context,
        database.asset_id,
        document,
        expected_revision=1,
    )
    validated = await classrooms.validate(context, database.asset_id)
    assert validated.revision == updated.revision == 2
    assert (
        validated.validation_document_sha256
        == hashlib.sha256(canonical_json_bytes(updated.document)).hexdigest()
    )
    reviews = ReviewService(SqlAlchemyReviewRepository(database.engine, database.tenant_id))
    review = await reviews.submit(
        context,
        database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key=submit_key,
    )
    await reviews.approve(
        _context(database, "reviewer-1", "classroom.approve"),
        review.id,
        "approved edited lesson",
    )
    return review


async def _publish_approved_baseline(
    database: ReviewDatabase,
    *,
    key_prefix: str,
):
    reviews = ReviewService(SqlAlchemyReviewRepository(database.engine, database.tenant_id))
    review = await reviews.submit(
        _context(database, "author-1", "classroom.submit"),
        database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key=f"{key_prefix}-submit",
    )
    await reviews.approve(
        _context(database, "reviewer-1", "classroom.approve"),
        review.id,
        "approved",
    )
    repository = SqlAlchemyPublicationRepository(database.engine, database.tenant_id)
    published = await PublicationService(
        repository,
        ClassroomPublicationMaterializer(_StoreProvider(database.store)),
    ).publish(
        _context(database, "publisher-1", "classroom.publish"),
        database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key=f"{key_prefix}-publish",
    )
    return repository, published


async def _seed_published_version(
    database: ReviewDatabase,
    *,
    source_version_id: str,
    version_number: int,
) -> str:
    version_id = f"migration-version-{version_number}"
    sessions = _tenant_sessions(database)
    async with sessions() as session:
        async with session.begin():
            source = await session.get(ClassroomVersion, source_version_id)
            assert source is not None
            session.add(
                ClassroomVersion(
                    id=version_id,
                    tenant_id=database.tenant_id,
                    classroom_id=database.asset_id,
                    version_number=version_number,
                    generation_job_id=None,
                    source_version_id=source_version_id,
                    document_sha256=source.document_sha256,
                    media_manifest_sha256=source.media_manifest_sha256,
                    document_object_key=source.document_object_key,
                )
            )
            await session.flush()
            session.add(
                Publication(
                    id=f"migration-publication-{version_number}",
                    tenant_id=database.tenant_id,
                    classroom_id=database.asset_id,
                    classroom_version_id=version_id,
                    actor_id="publisher-1",
                    scope="tenant",
                    class_id=None,
                    review_request_id=None,
                    idempotency_key=f"migration-seed-{version_number}",
                    request_sha256=hashlib.sha256(version_id.encode()).hexdigest(),
                )
            )
    return version_id


@pytest.mark.asyncio
async def test_generated_media_is_published_from_the_exact_base_version_artifact(
    review_database: ReviewDatabase,
) -> None:
    _, published = await _publish_approved_baseline(
        review_database,
        key_prefix="generated-media",
    )

    media_key = classroom_artifact_key(
        review_database.tenant_id,
        review_database.asset_id,
        published.version_number,
        "media/media-generated.png",
    )
    assert await _read_all(await review_database.store.open(media_key)) == (
        b"\x89PNG\r\n\x1a\ngenerated-media"
    )
    sessions = _tenant_sessions(review_database)
    async with sessions() as session:
        reservation = await session.scalar(
            select(ClassroomPublicationMaterialization).where(
                ClassroomPublicationMaterialization.idempotency_key == "generated-media-publish"
            )
        )
    assert reservation is not None
    receipts = json.loads(reservation.source_media_receipts)
    assert [(item["mediaId"], item["sourceKind"]) for item in receipts] == [
        ("media-generated", "version_artifact")
    ]


@pytest.mark.asyncio
async def test_review_decision_is_atomic_append_only_and_rejects_self_review(
    review_database: ReviewDatabase,
) -> None:
    repository = SqlAlchemyReviewRepository(
        review_database.engine,
        review_database.tenant_id,
    )
    service = ReviewService(repository)
    author = _context(
        review_database,
        "author-1",
        "classroom.submit",
        "classroom.approve",
    )
    review = await service.submit(
        author,
        review_database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="submit-review-1",
    )
    assert review.warnings[0]["code"] == "needs_caption"
    with pytest.raises(ReviewAccessDenied):
        await service.approve(author, review.id, "approved")

    reviewer_a = _context(review_database, "reviewer-a", "classroom.approve")
    reviewer_b = _context(review_database, "reviewer-b", "classroom.approve")
    results = await asyncio.gather(
        service.approve(reviewer_a, review.id, "approved"),
        service.reject(reviewer_b, review.id, "rejected"),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, ReviewConflict) for item in results) == 1

    translated = review_database.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(review_database.tenant_id)}
    )
    sessions = async_sessionmaker(translated, expire_on_commit=False)
    async with sessions() as session:
        approval_count = await session.scalar(select(func.count()).select_from(Approval))
        assert approval_count == 2
        event = await session.scalar(select(Approval).limit(1))
        assert event is not None
    async with sessions() as session:
        with pytest.raises(DBAPIError, match="append-only classroom audit record"):
            async with session.begin():
                await session.execute(
                    update(Approval).where(Approval.id == event.id).values(reason="changed")
                )


@pytest.mark.asyncio
async def test_review_decision_transaction_enforces_the_current_self_review_policy(
    review_database: ReviewDatabase,
) -> None:
    repository = SqlAlchemyReviewRepository(
        review_database.engine,
        review_database.tenant_id,
    )
    await repository.set_policy(
        ReviewPolicy(prohibit_self_review=False),
        actor_id="policy-admin",
    )
    author = _context(
        review_database,
        "author-1",
        "classroom.submit",
        "classroom.approve",
    )
    review = await ReviewService(repository).submit(
        author,
        review_database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="submit-policy-race",
    )

    with pytest.raises(ReviewAccessDenied, match="self-review"):
        await ReviewService(_FlipPolicyAfterServiceRead(repository)).approve(
            author,
            review.id,
            "must use current policy",
        )

    stored = await repository.get_review(review.id)
    assert stored is not None and stored.status == "pending"


@pytest.mark.asyncio
async def test_submit_rechecks_exact_validation_binding(
    review_database: ReviewDatabase,
) -> None:
    translated = review_database.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(review_database.tenant_id)}
    )
    sessions = async_sessionmaker(translated, expire_on_commit=False)
    async with sessions() as session:
        async with session.begin():
            draft = await session.get(ClassroomDraft, review_database.draft_id)
            assert draft is not None
            draft.revision = 2
    service = ReviewService(
        SqlAlchemyReviewRepository(review_database.engine, review_database.tenant_id)
    )
    with pytest.raises(ReviewValidationStale):
        await service.submit(
            _context(review_database, "author-1", "classroom.submit"),
            review_database.asset_id,
            scope="tenant",
            class_id=None,
            idempotency_key="submit-review-1",
        )


@pytest.mark.asyncio
async def test_concurrent_review_submission_rechecks_idempotency_after_target_lock(
    review_database: ReviewDatabase,
) -> None:
    repository = SqlAlchemyReviewRepository(
        review_database.engine,
        review_database.tenant_id,
    )
    same = SubmitReviewCommand(
        tenant_id=review_database.tenant_id,
        asset_id=review_database.asset_id,
        actor_id="author-1",
        scope="tenant",
        class_id=None,
        idempotency_key="submit-race-same",
    )
    sessions = _tenant_sessions(review_database)
    async with sessions() as blocker:
        async with blocker.begin():
            await blocker.execute(
                select(ClassroomAsset)
                .where(ClassroomAsset.id == review_database.asset_id)
                .with_for_update()
            )
            same_tasks = (
                asyncio.create_task(repository.submit(same)),
                asyncio.create_task(repository.submit(same)),
            )
            await _wait_for_blocked_queries(
                review_database,
                "classroom_assets",
                same_tasks,
            )
    same_results = await asyncio.gather(*same_tasks, return_exceptions=True)
    assert not any(isinstance(result, Exception) for result in same_results)
    assert same_results[0] == same_results[1]

    different_asset_id, _ = await _clone_review_target(
        review_database,
        "different-payload",
    )
    base = SubmitReviewCommand(
        tenant_id=review_database.tenant_id,
        asset_id=different_asset_id,
        actor_id="author-1",
        scope="tenant",
        class_id=None,
        idempotency_key="submit-race-different",
    )
    changed = SubmitReviewCommand(
        tenant_id=base.tenant_id,
        asset_id=base.asset_id,
        actor_id="author-2",
        scope=base.scope,
        class_id=base.class_id,
        idempotency_key=base.idempotency_key,
    )
    async with sessions() as blocker:
        async with blocker.begin():
            await blocker.execute(
                select(ClassroomAsset)
                .where(ClassroomAsset.id == different_asset_id)
                .with_for_update()
            )
            different_tasks = (
                asyncio.create_task(repository.submit(base)),
                asyncio.create_task(repository.submit(changed)),
            )
            await _wait_for_blocked_queries(
                review_database,
                "classroom_assets",
                different_tasks,
            )
    different_results = await asyncio.gather(
        *different_tasks,
        return_exceptions=True,
    )
    errors = [result for result in different_results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], ReviewConflict)
    assert str(errors[0]) == "review idempotency key conflicts"


@pytest.mark.asyncio
async def test_concurrent_assignment_rechecks_idempotency_after_target_locks(
    review_database: ReviewDatabase,
) -> None:
    repository, published = await _publish_approved_baseline(
        review_database,
        key_prefix="assign-race",
    )
    same = AssignCommand(
        tenant_id=review_database.tenant_id,
        asset_id=review_database.asset_id,
        version_id=published.version_id,
        class_id="class-a",
        actor_id="publisher-1",
        idempotency_key="assign-race-same",
    )
    sessions = _tenant_sessions(review_database)
    async with sessions() as blocker:
        async with blocker.begin():
            await blocker.execute(
                select(ClassroomVersion)
                .where(ClassroomVersion.id == published.version_id)
                .with_for_update()
            )
            same_tasks = (
                asyncio.create_task(repository.assign(same)),
                asyncio.create_task(repository.assign(same)),
            )
            await _wait_for_blocked_queries(
                review_database,
                "classroom_versions",
                same_tasks,
            )
    same_results = await asyncio.gather(*same_tasks, return_exceptions=True)
    assert not any(isinstance(result, Exception) for result in same_results)
    assert same_results[0] == same_results[1]

    async with sessions() as session:
        async with session.begin():
            session.add(
                TeachingClass(
                    id="class-c",
                    course_id="course-a",
                    name="Class C",
                    status="active",
                )
            )
    base = AssignCommand(
        tenant_id=review_database.tenant_id,
        asset_id=review_database.asset_id,
        version_id=published.version_id,
        class_id="class-c",
        actor_id="publisher-1",
        idempotency_key="assign-race-different",
    )
    changed = AssignCommand(
        tenant_id=base.tenant_id,
        asset_id=base.asset_id,
        version_id=base.version_id,
        class_id=base.class_id,
        actor_id="publisher-2",
        idempotency_key=base.idempotency_key,
    )
    async with sessions() as blocker:
        async with blocker.begin():
            await blocker.execute(
                select(ClassroomVersion)
                .where(ClassroomVersion.id == published.version_id)
                .with_for_update()
            )
            different_tasks = (
                asyncio.create_task(repository.assign(base)),
                asyncio.create_task(repository.assign(changed)),
            )
            await _wait_for_blocked_queries(
                review_database,
                "classroom_versions",
                different_tasks,
            )
    different_results = await asyncio.gather(
        *different_tasks,
        return_exceptions=True,
    )
    errors = [result for result in different_results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], PublicationConflict)
    assert str(errors[0]) == "assignment idempotency key conflicts"


@pytest.mark.asyncio
async def test_concurrent_migration_rechecks_idempotency_after_target_locks(
    review_database: ReviewDatabase,
) -> None:
    repository, published = await _publish_approved_baseline(
        review_database,
        key_prefix="migration-race",
    )
    assignment = await repository.assign(
        AssignCommand(
            tenant_id=review_database.tenant_id,
            asset_id=review_database.asset_id,
            version_id=published.version_id,
            class_id="class-a",
            actor_id="publisher-1",
            idempotency_key="migration-race-assignment",
        )
    )
    await repository.set_learning_state(
        class_id="class-a",
        state="idle",
        active_session_count=0,
        actor_id="learning-runtime",
    )
    version_three = await _seed_published_version(
        review_database,
        source_version_id=published.version_id,
        version_number=3,
    )
    version_four = await _seed_published_version(
        review_database,
        source_version_id=version_three,
        version_number=4,
    )
    base = MigrateAssignmentCommand(
        tenant_id=review_database.tenant_id,
        assignment_id=assignment.assignment_id,
        old_version_id=published.version_id,
        new_version_id=version_three,
        class_id="class-a",
        actor_id="publisher-1",
        reason="move to version three",
        idempotency_key="migration-race-different",
    )
    changed = MigrateAssignmentCommand(
        tenant_id=base.tenant_id,
        assignment_id=base.assignment_id,
        old_version_id=base.old_version_id,
        new_version_id=base.new_version_id,
        class_id=base.class_id,
        actor_id=base.actor_id,
        reason="different reason",
        idempotency_key=base.idempotency_key,
    )
    sessions = _tenant_sessions(review_database)
    async with sessions() as blocker:
        async with blocker.begin():
            await blocker.execute(
                select(Assignment)
                .where(Assignment.id == assignment.assignment_id)
                .with_for_update()
            )
            different_tasks = (
                asyncio.create_task(repository.migrate(base)),
                asyncio.create_task(repository.migrate(changed)),
            )
            await _wait_for_blocked_queries(
                review_database,
                "assignments",
                different_tasks,
            )
    different_results = await asyncio.gather(
        *different_tasks,
        return_exceptions=True,
    )
    errors = [result for result in different_results if isinstance(result, Exception)]
    migrations = [result for result in different_results if not isinstance(result, Exception)]
    assert len(migrations) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], PublicationConflict)
    assert str(errors[0]) == "migration idempotency key conflicts"
    migrated_assignment_id = migrations[0].new_assignment_id
    assert migrated_assignment_id is not None

    same = MigrateAssignmentCommand(
        tenant_id=review_database.tenant_id,
        assignment_id=migrated_assignment_id,
        old_version_id=version_three,
        new_version_id=version_four,
        class_id="class-a",
        actor_id="publisher-1",
        reason="move to version four",
        idempotency_key="migration-race-same",
    )
    async with sessions() as blocker:
        async with blocker.begin():
            await blocker.execute(
                select(Assignment).where(Assignment.id == migrated_assignment_id).with_for_update()
            )
            same_tasks = (
                asyncio.create_task(repository.migrate(same)),
                asyncio.create_task(repository.migrate(same)),
            )
            await _wait_for_blocked_queries(
                review_database,
                "assignments",
                same_tasks,
            )
    same_results = await asyncio.gather(*same_tasks, return_exceptions=True)
    assert not any(isinstance(result, Exception) for result in same_results)
    assert same_results[0] == same_results[1]


@pytest.mark.asyncio
async def test_edited_approved_draft_is_materialized_as_the_published_version(
    review_database: ReviewDatabase,
) -> None:
    edited_document = _canonical_document(
        review_database.asset_id,
        review_database.source_version_id,
        title="Reviewed lesson",
        text_content="The approved edited lesson",
    )
    await _approve_edited_draft(
        review_database,
        edited_document,
        submit_key="submit-edited-review",
    )
    published = await PublicationService(
        SqlAlchemyPublicationRepository(
            review_database.engine,
            review_database.tenant_id,
        ),
        ClassroomPublicationMaterializer(_StoreProvider(review_database.store)),
    ).publish(
        _context(review_database, "publisher-1", "classroom.publish"),
        review_database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="publish-edited-review",
    )

    canonical_document = canonical_json_bytes(edited_document)
    assert published.document_sha256 == hashlib.sha256(canonical_document).hexdigest()
    translated = review_database.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(review_database.tenant_id)}
    )
    sessions = async_sessionmaker(translated, expire_on_commit=False)
    async with sessions() as session:
        version = await session.get(ClassroomVersion, published.version_id)
    assert version is not None
    assert version.source_version_id == review_database.source_version_id
    assert version.document_object_key != (
        f"tenants/{review_database.tenant_id}/classrooms/"
        f"{review_database.asset_id}/versions/1/classroom.json"
    )
    assert (
        await _read_all(await review_database.store.open(version.document_object_key))
        == canonical_document
    )


@pytest.mark.asyncio
async def test_materialization_response_loss_retries_the_same_reserved_version(
    review_database: ReviewDatabase,
) -> None:
    edited_document = _canonical_document(
        review_database.asset_id,
        review_database.source_version_id,
        title="Retry-safe lesson",
        text_content="Publish this exact reviewed revision once",
    )
    await _approve_edited_draft(
        review_database,
        edited_document,
        submit_key="submit-response-loss",
    )
    repository = SqlAlchemyPublicationRepository(
        review_database.engine,
        review_database.tenant_id,
    )
    materializer = ClassroomPublicationMaterializer(_StoreProvider(review_database.store))
    publisher = _context(review_database, "publisher-1", "classroom.publish")
    with pytest.raises(RuntimeError, match="response lost"):
        await PublicationService(
            repository,
            _LoseFirstMaterializationResponse(materializer),
        ).publish(
            publisher,
            review_database.asset_id,
            scope="tenant",
            class_id=None,
            idempotency_key="publish-response-loss",
        )

    published = await PublicationService(repository, materializer).publish(
        publisher,
        review_database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="publish-response-loss",
    )
    retried = await PublicationService(repository, materializer).publish(
        publisher,
        review_database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="publish-response-loss",
    )
    assert retried == published
    assert published.version_number == 2

    translated = review_database.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(review_database.tenant_id)}
    )
    sessions = async_sessionmaker(translated, expire_on_commit=False)
    async with sessions() as session:
        version_count = await session.scalar(
            select(func.count())
            .select_from(ClassroomVersion)
            .where(ClassroomVersion.classroom_id == review_database.asset_id)
        )
        reservation = await session.scalar(
            select(ClassroomPublicationMaterialization).where(
                ClassroomPublicationMaterialization.idempotency_key == "publish-response-loss"
            )
        )
    assert version_count == 2
    assert reservation is not None and reservation.status == "finalized"
    assert reservation.version_id == published.version_id


@pytest.mark.asyncio
async def test_publish_refuses_a_draft_changed_after_object_materialization(
    review_database: ReviewDatabase,
) -> None:
    approved_document = _canonical_document(
        review_database.asset_id,
        review_database.source_version_id,
        title="Approved lesson",
        text_content="This exact revision was approved",
    )
    changed_document = _canonical_document(
        review_database.asset_id,
        review_database.source_version_id,
        title="Unreviewed change",
        text_content="This revision was not approved",
    )
    await _approve_edited_draft(
        review_database,
        approved_document,
        submit_key="submit-finalize-race",
    )
    materializer = ClassroomPublicationMaterializer(_StoreProvider(review_database.store))
    with pytest.raises(PublicationValidationStale):
        await PublicationService(
            SqlAlchemyPublicationRepository(
                review_database.engine,
                review_database.tenant_id,
            ),
            _ChangeDraftAfterMaterialization(
                materializer,
                review_database,
                changed_document,
            ),
        ).publish(
            _context(review_database, "publisher-1", "classroom.publish"),
            review_database.asset_id,
            scope="tenant",
            class_id=None,
            idempotency_key="publish-finalize-race",
        )

    translated = review_database.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(review_database.tenant_id)}
    )
    sessions = async_sessionmaker(translated, expire_on_commit=False)
    async with sessions() as session:
        version_count = await session.scalar(
            select(func.count())
            .select_from(ClassroomVersion)
            .where(ClassroomVersion.classroom_id == review_database.asset_id)
        )
        asset = await session.get(ClassroomAsset, review_database.asset_id)
        reservation = await session.scalar(
            select(ClassroomPublicationMaterialization).where(
                ClassroomPublicationMaterialization.idempotency_key == "publish-finalize-race"
            )
        )
    assert version_count == 1
    assert asset is not None and asset.current_published_version_id is None
    assert asset.lifecycle_state == "approved"
    assert reservation is not None and reservation.status == "object_committed"


@pytest.mark.asyncio
async def test_publish_promotes_only_receipt_verified_media_referenced_by_the_draft(
    review_database: ReviewDatabase,
) -> None:
    context = _context(
        review_database,
        "author-1",
        "classroom.edit",
        "classroom.submit",
    )
    classrooms = ClassroomService(
        SqlAlchemyClassroomRepository(
            review_database.engine,
            review_database.tenant_id,
        ),
        object(),
        object(),
        _StoreProvider(review_database.store),
    )
    referenced_body = b"\x89PNG\r\n\x1a\nreferenced-image"
    unused_body = b"\x89PNG\r\n\x1a\nunused-image"
    referenced = await classrooms.upload_media(
        context,
        review_database.asset_id,
        _Upload(referenced_body, "image/png"),
        hashlib.sha256(referenced_body).hexdigest(),
    )
    unused = await classrooms.upload_media(
        context,
        review_database.asset_id,
        _Upload(unused_body, "image/png"),
        hashlib.sha256(unused_body).hexdigest(),
    )
    edited_document = _canonical_document(
        review_database.asset_id,
        review_database.source_version_id,
        title="Reviewed media lesson",
        text_content="Use the reviewed diagram",
        media_id=referenced.id,
        media_body=referenced_body,
    )
    await _approve_edited_draft(
        review_database,
        edited_document,
        submit_key="submit-edited-media",
    )
    published = await PublicationService(
        SqlAlchemyPublicationRepository(
            review_database.engine,
            review_database.tenant_id,
        ),
        ClassroomPublicationMaterializer(_StoreProvider(review_database.store)),
    ).publish(
        _context(review_database, "publisher-1", "classroom.publish"),
        review_database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="publish-edited-media",
    )

    referenced_key = classroom_artifact_key(
        review_database.tenant_id,
        review_database.asset_id,
        published.version_number,
        f"media/{referenced.id}.png",
    )
    unused_key = classroom_artifact_key(
        review_database.tenant_id,
        review_database.asset_id,
        published.version_number,
        f"media/{unused.id}.png",
    )
    assert await _read_all(await review_database.store.open(referenced_key)) == referenced_body
    assert not await review_database.store.exists(unused_key)

    translated = review_database.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(review_database.tenant_id)}
    )
    sessions = async_sessionmaker(translated, expire_on_commit=False)
    async with sessions() as session:
        version = await session.get(ClassroomVersion, published.version_id)
        reservation = await session.scalar(
            select(ClassroomPublicationMaterialization).where(
                ClassroomPublicationMaterialization.idempotency_key == "publish-edited-media"
            )
        )
    assert version is not None and reservation is not None
    assert version.media_manifest_sha256 == reservation.media_manifest_sha256
    confirmed = json.loads(reservation.confirmed_artifacts or "[]")
    assert [item["mediaId"] for item in confirmed] == [None, referenced.id]


@pytest.mark.asyncio
async def test_publish_assign_and_explicit_migration_are_durable_and_idempotent(
    review_database: ReviewDatabase,
) -> None:
    reviews = ReviewService(
        SqlAlchemyReviewRepository(review_database.engine, review_database.tenant_id)
    )
    review = await reviews.submit(
        _context(review_database, "author-1", "classroom.submit"),
        review_database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="submit-review-1",
    )
    await reviews.approve(
        _context(review_database, "reviewer-1", "classroom.approve"),
        review.id,
        "approved",
    )
    repository = SqlAlchemyPublicationRepository(
        review_database.engine,
        review_database.tenant_id,
    )
    publications = PublicationService(
        repository,
        ClassroomPublicationMaterializer(_StoreProvider(review_database.store)),
    )
    publisher = _context(
        review_database,
        "publisher-1",
        "classroom.publish",
        "classroom.assign",
    )
    published = await publications.publish(
        publisher,
        review_database.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="publish-review-1",
    )
    assert (
        await publications.publish(
            publisher,
            review_database.asset_id,
            scope="tenant",
            class_id=None,
            idempotency_key="publish-review-1",
        )
    ) == published
    with pytest.raises(PublicationConflict):
        await publications.publish(
            publisher,
            review_database.asset_id,
            scope="tenant",
            class_id=None,
            idempotency_key="publish-same-review-again",
        )
    with pytest.raises(PublicationConflict):
        await publications.assign(
            _context(
                review_database,
                "publisher-b",
                "classroom.assign",
                class_id="class-b",
            ),
            published.version_id,
            class_id="class-b",
            idempotency_key="assign-cross-course",
        )
    assignment = await publications.assign(
        publisher,
        published.version_id,
        class_id="class-a",
        idempotency_key="assign-review-1",
    )

    await repository.set_learning_state(
        class_id="class-a",
        state="active",
        active_session_count=1,
        actor_id="learning-runtime",
    )
    with pytest.raises(ActiveLearningConflict):
        await publications.migrate(
            publisher,
            assignment.assignment_id,
            old_version_id=published.version_id,
            new_version_id=published.version_id,
            class_id="class-a",
            reason="explicit retry proof",
            idempotency_key="migration-review-refused",
        )
    refused = await repository.get_migration("migration-review-refused")
    assert refused is not None and refused.outcome == "refused_active_learning"

    translated = review_database.engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(review_database.tenant_id)}
    )
    sessions = async_sessionmaker(translated, expire_on_commit=False)
    async with sessions() as session:
        stored = await session.get(Assignment, assignment.assignment_id)
        migration_count = await session.scalar(
            select(func.count()).select_from(AssignmentMigration)
        )
        learning = await session.get(ClassLearningState, "class-a")
        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.tenant_id == review_database.tenant_id,
                AuditLog.action == "teaching.class_learning_state.updated",
            )
        )
    assert stored is not None and stored.revoked_at is None
    assert migration_count == 1
    assert learning is not None and learning.state == "active"
    assert audit_count == 1
