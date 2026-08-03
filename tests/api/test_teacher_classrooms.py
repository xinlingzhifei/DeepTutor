from __future__ import annotations

from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import classrooms
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.services.classrooms import (
    InvalidDraftDocument,
    build_validation_report,
    validate_draft_document_references,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


def _context(
    *,
    tenant_id: str = "tenant-a",
    user_id: str = "teacher-a",
    scope_type: str = "class",
    scope_id: str = "class-a",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=f"tenant_{tenant_id}",
        user_id=user_id,
        permissions=permissions_for_roles(
            {"teacher"},
            scope_type=scope_type,
            scope_id=scope_id,
            tenant_id=tenant_id,
        ),
    )


class _WorkflowService:
    def __init__(self) -> None:
        self.assets = {
            "asset-1": {
                "asset_id": "asset-1",
                "draft_id": "draft-1",
                "job_id": "job-1",
                "lifecycle_state": "awaiting_outline",
                "status": "awaiting_confirmation",
                "title": "Motion",
                "course_id": "course-a",
                "class_id": "class-a",
                "owner_id": "teacher-a",
                "revision": 3,
                "outline": {"title": "Motion outline"},
                "classroom_version_id": None,
            }
        }
        self.media: dict[tuple[str, str, str], dict[str, object]] = {}

    async def create(self, _context, request):
        assert request.classroom_mode == "full"
        return self.assets["asset-1"]

    async def list(self, _context):
        return tuple(self.assets.values())

    async def get(self, _context, asset_id):
        return self.assets.get(asset_id)

    async def get_draft(self, _context, asset_id):
        return self.assets.get(asset_id)

    async def update_outline(self, _context, asset_id, _outline, expected_revision):
        record = self.assets[asset_id]
        if expected_revision != record["revision"]:
            raise classrooms.ClassroomRevisionConflict()
        return record

    async def confirm_outline(self, _context, asset_id):
        return {
            **self.assets[asset_id],
            "status": "queued",
            "confirmed_outline_sha256": "a" * 64,
        }

    async def update_draft(self, _context, asset_id, _document, expected_revision):
        record = self.assets[asset_id]
        if expected_revision != record["revision"]:
            raise classrooms.ClassroomRevisionConflict()
        return record

    async def upload_media(self, context, asset_id, upload, declared_sha256):
        body = upload.file.read()
        record = {
            "id": "media-0123456789abcdef0123456789abcdef",
            "mime_type": upload.content_type,
            "sha256": declared_sha256,
            "size_bytes": len(body),
            "content": body,
        }
        self.media[(context.tenant_id, asset_id, str(record["id"]))] = record
        return record

    async def get_media(self, context, asset_id, media_id):
        return self.media.get((context.tenant_id, asset_id, media_id))

    async def validate(self, _context, asset_id):
        return self.assets.get(asset_id)


def _client(service: _WorkflowService, context: TenantContext | None = None) -> TestClient:
    application = FastAPI()
    application.include_router(classrooms.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = lambda: context or _context()
    application.dependency_overrides[classrooms.get_classroom_service] = lambda: service
    return TestClient(application)


def _full_classroom_request() -> dict[str, object]:
    return {
        "title": "Motion",
        "courseId": "course-a",
        "classId": "class-a",
        "objective": "Explain motion",
        "gradeBand": "grade-8",
        "audience": "intermediate",
        "durationMinutes": 45,
        "classroomMode": "full",
        "webPolicy": "disabled",
        "templateId": "template-a",
        "templateVersion": "1",
        "knowledgePoints": [
            {
                "knowledgePointId": "kp-motion",
                "title": "Motion",
                "description": "Describe displacement and velocity",
            }
        ],
        "contentMode": "open_creation",
        "openCreationAcknowledged": True,
        "requestedExports": ["classroom_zip"],
    }


def test_teacher_full_classroom_stops_for_outline_confirmation() -> None:
    client = _client(_WorkflowService())

    response = client.post("/api/v1/classrooms", json=_full_classroom_request())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "awaiting_confirmation"
    assert body["outline"]
    assert body["classroomVersionId"] is None


def test_stale_draft_revision_is_rejected() -> None:
    client = _client(_WorkflowService())

    response = client.put(
        "/api/v1/classrooms/asset-1/draft",
        headers={"If-Match": '"revision-2"'},
        json={"document": {"dslVersion": "0.1.0", "mediaIds": []}},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Draft revision is stale"}


def test_draft_media_is_bound_to_asset_and_tenant() -> None:
    service = _WorkflowService()
    client = _client(service)
    body = b"\x89PNG\r\n\x1a\nimage"

    media = client.post(
        "/api/v1/classrooms/asset-1/draft-media",
        data={"sha256": "8" * 64},
        files={"file": ("diagram.png", BytesIO(body), "image/png")},
    )
    assert media.status_code == 201

    response = client.get(
        f"/api/v1/classrooms/asset-b/draft-media/{media.json()['id']}"
    )

    assert response.status_code == 404
    assert "asset-1" not in response.text


def test_raw_object_keys_and_arbitrary_urls_are_rejected_before_draft_save() -> None:
    for document in (
        {"objectKey": "tenants/tenant-a/temporary/secret"},
        {"src": "https://attacker.invalid/image.png"},
        {"mediaIds": ["tenants/tenant-a/temporary/secret"]},
    ):
        with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
            validate_draft_document_references(document)


def test_validation_report_has_nine_named_sections_and_explicit_severity() -> None:
    report = build_validation_report(
        {
            "dslVersion": "0.1.0",
            "scenes": [
                {
                    "id": "scene-1",
                    "type": "interactive",
                    "title": "Unsafe demo",
                    "content": {"html": "<script>alert(1)</script>"},
                }
            ],
            "mediaIds": [],
            "knowledgePointMappings": [],
            "sourceRefs": [],
        },
        required_knowledge_point_ids=("kp-motion",),
        grounded=True,
        available_media_ids=frozenset(),
    )

    assert set(report["sections"]) == {
        "dsl_integrity",
        "media_integrity",
        "knowledge_point_coverage",
        "source_traceability",
        "unsupported_claims",
        "quiz_answerability",
        "interactive_security",
        "accessibility",
        "export_readiness",
    }
    assert report["valid"] is False
    assert report["severeFindings"]
    assert report["warnings"]
    for section in report["sections"].values():
        assert section["status"] in {"pass", "warning", "error"}
        for issue in section["issues"]:
            assert set(issue) == {"severity", "code", "message", "path"}


def test_disabled_teaching_does_not_register_teacher_classroom_routes() -> None:
    from deeptutor.api.main import _register_teacher_classroom_routes

    application = FastAPI()
    registered = _register_teacher_classroom_routes(
        application,
        enabled=False,
        dependencies=[],
    )

    assert registered is False
    assert all("/classrooms" not in route.path for route in application.routes)
