"""Review policy and teacher/content-review API acceptance tests."""

from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import classroom_reviews
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.services.reviews import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewPolicy,
    ReviewRecord,
    ReviewService,
    ReviewTarget,
    ReviewValidationStale,
)
from deeptutor.teaching.tenant_context import TenantContext


def _context(
    user_id: str,
    *permissions: tuple[str, str, str],
) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant_a",
        user_id=user_id,
        permissions=frozenset(
            ScopedPermission(
                permission=permission,
                scope_type=scope_type,
                scope_id=scope_id,
                tenant_id="tenant-a",
            )
            for permission, scope_type, scope_id in permissions
        ),
    )


def _review(*, status: str = "pending") -> ReviewRecord:
    return ReviewRecord(
        id="review-1",
        tenant_id="tenant-a",
        asset_id="asset-1",
        draft_id="draft-1",
        draft_revision=7,
        document_sha256="a" * 64,
        validation_report_sha256="b" * 64,
        submitted_by="author-1",
        scope="tenant",
        class_id=None,
        status=status,
        warnings=({"code": "needs_caption", "severity": "warning"},),
        reviewer_id=None,
        comment=None,
    )


class _ReviewRepository:
    def __init__(self, *, policy: ReviewPolicy | None = None) -> None:
        self.policy = policy or ReviewPolicy()
        self.target = ReviewTarget(
            tenant_id="tenant-a",
            asset_id="asset-1",
            owner_id="author-1",
            course_id="course-a",
            class_id="class-a",
        )
        self.record = _review()
        self.decisions: list[tuple[str, str, str, str]] = []
        self.reject_revision = 8
        self.stale = False

    async def get_policy(self) -> ReviewPolicy:
        return self.policy

    async def get_target(self, asset_id: str) -> ReviewTarget | None:
        return self.target if asset_id == self.target.asset_id else None

    async def submit(self, command) -> ReviewRecord:
        if self.stale:
            raise ReviewValidationStale("validation is stale")
        self.record = replace(
            self.record,
            submitted_by=command.actor_id,
            scope=command.scope,
            class_id=command.class_id,
        )
        return self.record

    async def list_pending(self) -> tuple[ReviewRecord, ...]:
        return (self.record,)

    async def get_review(self, review_id: str) -> ReviewRecord | None:
        return self.record if review_id == self.record.id else None

    async def decide(self, command) -> ReviewRecord:
        if self.record.status != "pending":
            raise ReviewConflict("review was already decided")
        self.decisions.append(
            (command.review_id, command.actor_id, command.decision, command.comment)
        )
        self.record = replace(
            self.record,
            status=command.decision,
            reviewer_id=command.actor_id,
            comment=command.comment,
            draft_revision=(
                self.reject_revision
                if command.decision == "rejected"
                else self.record.draft_revision
            ),
        )
        return self.record


@pytest.mark.asyncio
async def test_content_author_cannot_approve_own_submission() -> None:
    repository = _ReviewRepository()
    service = ReviewService(repository)
    author = _context(
        "author-1",
        ("classroom.approve", "tenant", "tenant-a"),
    )

    with pytest.raises(ReviewAccessDenied, match="self-review"):
        await service.approve(author, "review-1", "approved")

    assert repository.decisions == []


@pytest.mark.asyncio
async def test_org_review_requires_resource_scoped_approval_permission() -> None:
    repository = _ReviewRepository()
    service = ReviewService(repository)
    wrong_class = _context(
        "reviewer-1",
        ("classroom.approve", "class", "class-b"),
    )
    with pytest.raises(ReviewAccessDenied):
        await service.approve(wrong_class, "review-1", "approved")

    reviewer = _context(
        "reviewer-1",
        ("classroom.approve", "course", "course-a"),
    )
    approved = await service.approve(reviewer, "review-1", "approved")
    assert approved.status == "approved"
    assert approved.reviewer_id == "reviewer-1"


@pytest.mark.asyncio
async def test_platform_template_review_requires_platform_permission() -> None:
    repository = _ReviewRepository()
    repository.record = replace(repository.record, scope="platform")
    service = ReviewService(repository)
    tenant_reviewer = _context(
        "reviewer-1",
        ("classroom.approve", "tenant", "tenant-a"),
    )
    with pytest.raises(ReviewAccessDenied):
        await service.approve(tenant_reviewer, "review-1", "approved")

    platform_reviewer = _context(
        "platform-reviewer",
        ("classroom.approve", "tenant", "tenant-a"),
        ("template.manage", "tenant", "tenant-a"),
    )
    approved = await service.approve(platform_reviewer, "review-1", "approved")
    assert approved.status == "approved"


@pytest.mark.asyncio
async def test_submit_rejects_stale_validation_binding() -> None:
    repository = _ReviewRepository()
    repository.stale = True
    service = ReviewService(repository)
    author = _context(
        "author-1",
        ("classroom.submit", "class", "class-a"),
    )

    with pytest.raises(ReviewValidationStale):
        await service.submit(
            author,
            "asset-1",
            scope="tenant",
            class_id=None,
            idempotency_key="submit-key-1",
        )


@pytest.mark.asyncio
async def test_review_warnings_remain_visible_and_reject_creates_revision() -> None:
    repository = _ReviewRepository()
    service = ReviewService(repository)
    reviewer = _context(
        "reviewer-1",
        ("classroom.approve", "tenant", "tenant-a"),
    )

    listed = await service.list(reviewer)
    assert listed[0].warnings[0]["code"] == "needs_caption"

    rejected = await service.reject(reviewer, "review-1", "add a caption")
    assert rejected.status == "rejected"
    assert rejected.draft_revision == 8

    with pytest.raises(ReviewConflict):
        await service.approve(reviewer, "review-1", "changed my mind")


def _client(service: ReviewService, context: TenantContext) -> TestClient:
    app = FastAPI()
    app.include_router(classroom_reviews.router, prefix="/api/v1")
    app.dependency_overrides[classroom_reviews.require_tenant] = lambda: context
    app.dependency_overrides[classroom_reviews.get_review_service] = lambda: service
    return TestClient(app)


def test_review_routes_are_only_registered_when_teaching_is_enabled() -> None:
    from deeptutor.api.main import _register_classroom_review_routes

    disabled = FastAPI()
    assert not _register_classroom_review_routes(
        disabled,
        enabled=False,
        dependencies=[],
    )
    assert all("/classroom-reviews" not in route.path for route in disabled.routes)

    enabled = FastAPI()
    assert _register_classroom_review_routes(
        enabled,
        enabled=True,
        dependencies=[],
    )
    assert any(
        route.path == "/api/v1/classroom-reviews" for route in enabled.routes
    )


def test_content_author_cannot_approve_own_submission_api() -> None:
    service = ReviewService(_ReviewRepository())
    author = _context(
        "author-1",
        ("classroom.approve", "tenant", "tenant-a"),
    )

    response = _client(service, author).post(
        "/api/v1/classroom-reviews/review-1/approve",
        json={"comment": "approved"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Classroom review access denied"}


def test_review_api_maps_stale_validation_and_returns_warnings() -> None:
    repository = _ReviewRepository()
    repository.stale = True
    author = _context(
        "author-1",
        ("classroom.submit", "class", "class-a"),
    )
    response = _client(ReviewService(repository), author).post(
        "/api/v1/classrooms/asset-1/submit",
        headers={"Idempotency-Key": "submit-key-1"},
        json={"scope": "tenant"},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Classroom validation is stale"}

    repository.stale = False
    reviewer = _context(
        "reviewer-1",
        ("classroom.approve", "tenant", "tenant-a"),
    )
    response = _client(ReviewService(repository), reviewer).get(
        "/api/v1/classroom-reviews"
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["warnings"][0]["code"] == "needs_caption"
