"""Review policy and teacher/content-review API acceptance tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import classroom_reviews
from deeptutor.teaching.contracts import (
    ClassroomDocument,
    TeachingBrief,
    canonical_json_bytes,
    canonical_teaching_brief_sha256,
)
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.services.review_repository import SqlAlchemyReviewRepository
from deeptutor.teaching.services.reviews import (
    ReviewAccessDenied,
    ReviewBaseline,
    ReviewConflict,
    ReviewDetailEvidence,
    ReviewPersistenceError,
    ReviewPolicy,
    ReviewRecord,
    ReviewService,
    ReviewSourceFragment,
    ReviewTarget,
    ReviewValidationStale,
)
from deeptutor.teaching.tenant_context import TenantContext
from tests.teaching.test_contracts import valid_teaching_brief
from tests.teaching_contract_fixtures import valid_classroom_document


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
        self.detail_evidence: ReviewDetailEvidence | None = None

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

    async def get_detail(self, review_id: str) -> ReviewDetailEvidence | None:
        if review_id != self.record.id:
            return None
        return self.detail_evidence

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


def _document(title: str) -> dict[str, object]:
    payload = valid_classroom_document()
    payload["classroom_id"] = "asset-1"
    payload["classroom_version_id"] = "version-1"
    payload["media_manifest"] = []
    payload["export_manifest"] = []
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list) and isinstance(scenes[0], dict)
    scenes[0]["title"] = title
    normalized = ClassroomDocument.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    unhashed = dict(normalized)
    unhashed.pop("fileSha256")
    normalized["fileSha256"] = hashlib.sha256(
        canonical_json_bytes(unhashed)
    ).hexdigest()
    return normalized


class _Store:
    def __init__(self, documents: dict[str, bytes]) -> None:
        self.documents = documents

    async def open(self, object_key: str):
        async def body():
            yield self.documents[object_key]

        return body()


class _StoreProvider:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def store_for_tenant(self, tenant_id: str):
        assert tenant_id == "tenant-a"
        return self.store


def _detail_service() -> tuple[ReviewService, _ReviewRepository]:
    repository = _ReviewRepository()
    submitted = _document("Updated lesson")
    baseline_document = _document("Original lesson")
    baseline_bytes = canonical_json_bytes(baseline_document)
    baseline = ReviewBaseline(
        version_id="version-1",
        version_number=1,
        document_sha256=hashlib.sha256(baseline_bytes).hexdigest(),
        document_object_key="tenants/tenant-a/classrooms/asset-1/versions/1/classroom.json",
    )
    repository.detail_evidence = ReviewDetailEvidence(
        review=repository.record,
        target=repository.target,
        title="Lesson",
        course_id="course-a",
        target_class_id="class-a",
        document=submitted,
        validation_report={"valid": True},
        source_fragments=(
            ReviewSourceFragment(
                fragment_id="fragment-1",
                source_id="source-1",
                text="Newton described motion.",
                content_sha256=hashlib.sha256(
                    b"Newton described motion."
                ).hexdigest(),
            ),
        ),
        baseline=baseline,
    )
    store = _Store({baseline.document_object_key: baseline_bytes})
    return ReviewService(repository, _StoreProvider(store)), repository


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


@pytest.mark.asyncio
async def test_content_reviewer_can_read_exact_review_evidence_without_edit_permission() -> None:
    service, _ = _detail_service()
    reviewer = _context(
        "reviewer-1",
        ("classroom.approve", "course", "course-a"),
    )

    detail = await service.detail(reviewer, "review-1")

    assert detail.source_fragments[0].text == "Newton described motion."
    assert detail.baseline is not None and detail.baseline.version_id == "version-1"
    assert "/openmaic/scenes/0/title" in detail.changed_paths


@pytest.mark.asyncio
async def test_review_detail_denies_missing_permission_and_self_review() -> None:
    service, _ = _detail_service()

    with pytest.raises(ReviewAccessDenied):
        await service.detail(_context("reviewer-1"), "review-1")
    with pytest.raises(ReviewAccessDenied, match="self-review"):
        await service.detail(
            _context(
                "author-1",
                ("classroom.approve", "tenant", "tenant-a"),
            ),
            "review-1",
        )


def test_review_detail_rejects_a_tampered_draft_hash_binding() -> None:
    document = _document("Reviewed lesson")
    encoded = canonical_json_bytes(document).decode()
    document_sha256 = hashlib.sha256(encoded.encode()).hexdigest()
    asset = SimpleNamespace(id="asset-1")
    draft = SimpleNamespace(
        id="draft-1",
        classroom_id="asset-1",
        base_version_id="version-1",
        revision=7,
        document=encoded,
        document_sha256=document_sha256,
    )
    review = SimpleNamespace(
        classroom_id="asset-1",
        classroom_draft_id="draft-1",
        draft_revision=7,
        document_sha256="f" * 64,
    )

    with pytest.raises(ReviewValidationStale, match="stale"):
        SqlAlchemyReviewRepository._reviewed_document(asset, draft, review)


def test_review_detail_rejects_a_tampered_source_permission_scope() -> None:
    payload = valid_teaching_brief()
    payload.update(
        tenant_id="tenant-a",
        course_id="course-a",
        target_class_id="class-a",
    )
    text = "Newton described motion."
    content_sha256 = hashlib.sha256(text.encode()).hexdigest()
    payload["source_snapshot"] = {
        "snapshot_id": "snapshot-1",
        "created_at": "2026-08-09T00:00:00Z",
        "content_sha256": "c" * 64,
    }
    payload["source_fragments"] = [
        {
            "fragment_id": "fragment-1",
            "source_id": "source-1",
            "text": text,
            "content_sha256": content_sha256,
        }
    ]
    payload["permission_summary"] = {
        "allowed_source_ids": ["source-1"],
        "allowed_fragment_ids": ["fragment-1"],
        "usage_scope": "tenant:tenant-a/course:course-a/class:class-a",
        "attribution_required": True,
    }
    contract = TeachingBrief.model_validate(payload)
    payload["content_sha256"] = canonical_teaching_brief_sha256(contract)
    contract = TeachingBrief.model_validate(payload)
    manifest = {
        "schema_version": 1,
        "snapshot_id": "snapshot-1",
        "source_kind": "knowledge_base",
        "source_id": "source-1",
        "source_snapshot_sha256": "c" * 64,
        "fragments": [
            {
                "fragment_id": "fragment-1",
                "source_id": "source-1",
                "text": text,
                "content_sha256": content_sha256,
                "permission": "source.use",
                "document_id": "document-1",
                "page": 1,
                "section": "Motion",
            }
        ],
        "source_refs": [
            {
                "citation_id": "citation-1",
                "source_id": "source-1",
                "fragment_id": "fragment-1",
                "document_id": "document-1",
                "page": 1,
                "section": "Motion",
            }
        ],
        "permission_summary": {
            "permissions": ["source.use"],
            "scope_type": "class",
            "scope_id": "class-b",
        },
        "query_sha256": "d" * 64,
        "retrieval": {
            "provider": "llamaindex",
            "retrieval_view_signature": "e" * 64,
        },
        "created_by": "author-1",
    }
    snapshot = SimpleNamespace(
        id="snapshot-1",
        tenant_id="tenant-a",
        source_id="source-1",
        content_sha256="c" * 64,
        citation_manifest=json.dumps(manifest),
    )
    brief = SimpleNamespace(
        tenant_id="tenant-a",
        course_id="course-a",
        class_id="class-a",
        source_snapshot_id="snapshot-1",
    )

    with pytest.raises(ReviewPersistenceError, match="source evidence"):
        SqlAlchemyReviewRepository._source_fragments(snapshot, brief, contract)


def _client(service: ReviewService, context: TenantContext) -> TestClient:
    app = FastAPI()
    app.include_router(classroom_reviews.router, prefix="/api/v1")
    app.dependency_overrides[classroom_reviews.require_tenant] = lambda: context
    app.dependency_overrides[classroom_reviews.get_review_service] = lambda: service
    app.dependency_overrides[classroom_reviews.get_review_detail_service] = lambda: service
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


def test_review_detail_api_returns_exact_camel_case_evidence() -> None:
    service, _ = _detail_service()
    reviewer = _context(
        "reviewer-1",
        ("classroom.approve", "tenant", "tenant-a"),
    )

    response = _client(service, reviewer).get(
        "/api/v1/classroom-reviews/review-1"
    )

    assert response.status_code == 200
    assert response.json() == {
        "review": {
            "id": "review-1",
            "assetId": "asset-1",
            "draftId": "draft-1",
            "draftRevision": 7,
            "documentSha256": "a" * 64,
            "validationReportSha256": "b" * 64,
            "submittedBy": "author-1",
            "scope": "tenant",
            "classId": None,
            "status": "pending",
            "warnings": [{"code": "needs_caption", "severity": "warning"}],
            "reviewerId": None,
            "comment": None,
        },
        "title": "Lesson",
        "courseId": "course-a",
        "targetClassId": "class-a",
        "document": _document("Updated lesson"),
        "validationReport": {"valid": True},
        "sourceFragments": [
            {
                "fragmentId": "fragment-1",
                "sourceId": "source-1",
                "text": "Newton described motion.",
                "contentSha256": hashlib.sha256(
                    b"Newton described motion."
                ).hexdigest(),
            }
        ],
        "baseline": {
            "versionId": "version-1",
            "versionNumber": 1,
            "documentSha256": hashlib.sha256(
                canonical_json_bytes(_document("Original lesson"))
            ).hexdigest(),
        },
        "changedPaths": ["/openmaic/scenes/0/title"],
    }
