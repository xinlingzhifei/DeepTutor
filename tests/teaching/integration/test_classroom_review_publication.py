"""PostgreSQL proof for durable review, publication, and assignment migration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.teaching.contracts import canonical_json_bytes
from deeptutor.teaching.models import AuditLog, Tenant
from deeptutor.teaching.models.classrooms import (
    Approval,
    Assignment,
    AssignmentMigration,
    ClassLearningState,
    ClassroomAsset,
    ClassroomDraft,
    ClassroomReviewRequest,
    ClassroomVersion,
    TeachingBrief,
)
from deeptutor.teaching.models.jobs import (
    ArtifactPromotionState,
    ClassroomArtifact,
    GenerationJob,
)
from deeptutor.teaching.models.tenant import Course, TeachingClass
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from deeptutor.teaching.services.publications import (
    ActiveLearningConflict,
    PublicationConflict,
    PublicationService,
)
from deeptutor.teaching.services.review_repository import SqlAlchemyReviewRepository
from deeptutor.teaching.services.reviews import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewService,
    ReviewValidationStale,
)
from deeptutor.teaching.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class ReviewDatabase:
    engine: AsyncEngine
    tenant_id: str
    asset_id: str
    draft_id: str
    source_version_id: str
    document_sha256: str
    validation_sha256: str


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
async def review_database(generation_database) -> ReviewDatabase:
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
    document = canonical_json_bytes({"scenes": []}).decode()
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
    object_key = f"classrooms/{asset_id}/generated/classroom.json"
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
                    id=f"brief-{suffix}",
                    tenant_id=tenant_id,
                    source_snapshot_id=None,
                    course_id="course-a",
                    class_id="class-a",
                    brief_version=1,
                    document="{}",
                    document_sha256="3" * 64,
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
                    teaching_brief_id=f"brief-{suffix}",
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
                    media_manifest_sha256="4" * 64,
                    document_object_key=object_key,
                )
            )
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
    try:
        yield ReviewDatabase(
            engine=engine,
            tenant_id=tenant_id,
            asset_id=asset_id,
            draft_id=draft_id,
            source_version_id=source_version_id,
            document_sha256=document_sha256,
            validation_sha256=report_sha256,
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
    publications = PublicationService(repository)
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
            .where(AuditLog.action == "teaching.class_learning_state.updated")
        )
    assert stored is not None and stored.revoked_at is None
    assert migration_count == 1
    assert learning is not None and learning.state == "active"
    assert audit_count == 1
