from __future__ import annotations

import hashlib
from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import classrooms
from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.services.classrooms import (
    ClassroomMediaBinding,
    InvalidDraftDocument,
    build_validation_report,
    validate_draft_document_references,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant
from tests.teaching_contract_fixtures import valid_classroom_document


def _canonical_document(
    *,
    canvas: dict[str, object] | None = None,
    html: str | None = None,
    media_id: str | None = None,
    media_download_path: str | None = None,
) -> dict[str, object]:
    payload = valid_classroom_document()
    payload["export_manifest"] = []
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list) and isinstance(scenes[0], dict)
    if html is not None:
        scenes[0]["type"] = "interactive"
        scenes[0]["content"] = {
            "type": "interactive",
            "html": html,
            "bridge_version": "1.0",
            "sandbox": {"allow_scripts": True, "allow_same_origin": False},
        }
    elif canvas is not None:
        scenes[0]["content"] = {"type": "slide", "canvas": canvas}
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    if media_id is None:
        payload["media_manifest"] = []
    else:
        manifest[0].update(
            media_id=media_id,
            relative_path=f"media/{media_id}.png",
            mime_type="image/png",
            temporary_download_path=(
                media_download_path
                or f"/api/yfeistai/v1/artifacts/job-a/media/{media_id}.png"
            ),
        )
    provisional = ClassroomDocument.model_validate(payload)
    unhashed = provisional.model_dump(mode="json", by_alias=True, exclude_none=True)
    unhashed.pop("fileSha256")
    payload["file_sha256"] = hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    return ClassroomDocument.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )


def _media_binding(document: dict[str, object]) -> ClassroomMediaBinding:
    manifest = document["mediaManifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    item = manifest[0]
    return ClassroomMediaBinding(
        media_id=str(item["mediaId"]),
        relative_name=str(item["relativePath"]),
        mime_type=str(item["mimeType"]),
        sha256=str(item["sha256"]),
        size_bytes=int(item["sizeBytes"]),
    )


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
        self.last_idempotency_key: str | None = None
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

    async def create(self, _context, request, idempotency_key=None):
        assert request.classroom_mode == "full"
        self.last_idempotency_key = idempotency_key
        return {
            **self.assets["asset-1"],
            "idempotency_key": idempotency_key or "auto-test-request-key",
        }

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


def test_create_forwards_and_echoes_a_strong_idempotency_key() -> None:
    service = _WorkflowService()

    response = _client(service).post(
        "/api/v1/classrooms",
        headers={"Idempotency-Key": "classroom-request-1"},
        json=_full_classroom_request(),
    )

    assert response.status_code == 202
    assert service.last_idempotency_key == "classroom-request-1"
    assert response.json()["idempotencyKey"] == "classroom-request-1"


def test_create_rejects_a_malformed_idempotency_key_before_service_call() -> None:
    service = _WorkflowService()

    response = _client(service).post(
        "/api/v1/classrooms",
        headers={"Idempotency-Key": "bad key"},
        json=_full_classroom_request(),
    )

    assert response.status_code == 422
    assert service.last_idempotency_key is None


def test_create_idempotency_conflict_is_an_explicit_409() -> None:
    class _ConflictService(_WorkflowService):
        async def create(self, _context, _request, idempotency_key=None):
            raise classrooms.ClassroomIdempotencyConflict()

    response = _client(_ConflictService()).post(
        "/api/v1/classrooms",
        headers={"Idempotency-Key": "classroom-request-1"},
        json=_full_classroom_request(),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Classroom idempotency key conflicts"}


def test_stale_draft_revision_is_rejected() -> None:
    client = _client(_WorkflowService())

    response = client.put(
        "/api/v1/classrooms/asset-1/draft",
        headers={"If-Match": '"revision-2"'},
        json={"document": {"dslVersion": "0.1.0", "mediaIds": []}},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Draft revision is stale"}


def test_outline_confirmation_binding_conflict_is_an_explicit_409() -> None:
    class _ConflictService(_WorkflowService):
        async def confirm_outline(self, _context, _asset_id):
            raise classrooms.ClassroomConfirmationConflict()

    response = _client(_ConflictService()).post("/api/v1/classrooms/asset-1/confirm-outline")

    assert response.status_code == 409
    assert response.json() == {"detail": "Outline confirmation conflicts"}


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

    response = client.get(f"/api/v1/classrooms/asset-b/draft-media/{media.json()['id']}")

    assert response.status_code == 404
    assert "asset-1" not in response.text


def test_raw_object_keys_and_arbitrary_urls_are_rejected_before_draft_save() -> None:
    for document in (
        {"objectKey": "tenants/tenant-a/temporary/secret"},
        {"src": "https://attacker.invalid/image.png"},
        {"mediaIds": ["tenants/tenant-a/temporary/secret"]},
    ):
        with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
            validate_draft_document_references(_canonical_document(canvas=document))


@pytest.mark.parametrize(
    "document",
    [
        {"imageUrl": "prefix HTTPS://attacker.invalid/image.png"},
        {"posterUri": "\tHtTpS : //attacker.invalid/video.mp4"},
        {"audioUrl": "%68%74%74%70%73%3A%2F%2Fattacker.invalid/a.mp3"},
        {"downloadUrl": "&#x66;tp://attacker.invalid/file"},
        {"src": "//attacker.invalid/image.png"},
        {"mediaPath": "../temporary/other-asset/image.png"},
        {"content": {"html": "<p>Open https://attacker.invalid now</p>"}},
    ],
)
def test_media_and_url_fields_reject_obfuscated_or_embedded_raw_references(
    document: dict[str, object],
) -> None:
    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(_canonical_document(canvas=document))


def test_resource_url_reference_semantics_are_rejected_fail_closed() -> None:
    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(
            _canonical_document(canvas={"resourceUrl": "https://attacker.invalid/resource.json"})
        )


def test_thumbnail_path_rejects_a_relative_object_reference() -> None:
    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(
            _canonical_document(canvas={"thumbnailPath": "previews/chapter-1.png"})
        )


def test_thumbnail_path_field_is_rejected_without_url_syntax() -> None:
    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(
            _canonical_document(canvas={"thumbnailPath": "chapter-one"})
        )


def test_reference_values_are_recursively_decoded_to_a_bounded_fixed_point() -> None:
    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(
            _canonical_document(canvas={"thumbnailPath": "%2525252e%2525252e%2525252fsecret"})
        )


def test_asset_url_reference_semantics_are_rejected_fail_closed() -> None:
    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(
            _canonical_document(canvas={"assetUrl": "//attacker.invalid/asset.png"})
        )


def test_image_srcset_reference_semantics_are_rejected_fail_closed() -> None:
    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(
            _canonical_document(
                canvas={"imageSrcSet": "preview.png 1x, https://attacker.invalid/preview.png 2x"}
            )
        )


def test_ordinary_teaching_text_may_name_a_url_without_becoming_a_reference() -> None:
    document = _canonical_document(
        canvas={
            "title": "Why https://example.edu uses TLS",
            "text": "Compare HTTP://example.edu in this lesson.",
        }
    )

    assert validate_draft_document_references(document) == frozenset()


def test_path_like_field_names_do_not_reject_svg_or_teaching_text() -> None:
    document = _canonical_document(
        canvas={
            "path": "M 10 10 L 20 20 Z",
            "learningPath": "Compare speed/distance, then explain the result.",
            "resourceTitle": "Chapter 2 classroom discussion",
            "thumbnailLabel": "Lesson preview",
        }
    )

    assert validate_draft_document_references(document) == frozenset()


@pytest.mark.parametrize(
    "html",
    [
        "<img src=https://attacker.invalid/pixel.png onload=steal()>",
        "<a href='jav&#x61;script:steal()'>click</a>",
        "<style>body{background:url(https://attacker.invalid)}</style>",
        "<iframe srcdoc='<p>nested</p>'></iframe>",
        "<div><span>unbalanced</div>",
    ],
)
def test_interactive_html_parser_blocks_unsafe_or_malformed_markup(html: str) -> None:
    document = _canonical_document(html=html)

    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(document)

    report = build_validation_report(
        document,
        required_knowledge_point_ids=(),
        grounded=False,
        available_media_bindings=(),
    )
    assert report["sections"]["interactive_security"]["status"] == "error"


def test_svg_presentation_attributes_cannot_load_external_url_resources() -> None:
    document = _canonical_document(
        html=(
            '<svg viewBox="0 0 10 10">'
            '<circle cx="5" cy="5" r="4" '
            'fill="url(//evil.invalid/pixel.svg)"></circle>'
            "</svg>"
        )
    )

    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(document)


@pytest.mark.parametrize(
    "fill",
    [
        "U R L ( //evil.invalid/pixel.svg )",
        "url(&#x2f;&#x2f;evil.invalid/pixel.svg)",
        "%75%72%6c%28%2f%2fevil.invalid%2fpixel.svg%29",
    ],
)
def test_svg_css_url_obfuscation_is_rejected(fill: str) -> None:
    document = _canonical_document(
        html=(f'<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="{fill}"></circle></svg>')
    )

    with pytest.raises(InvalidDraftDocument, match="unsafe reference"):
        validate_draft_document_references(document)


def test_safe_interactive_fragment_and_opaque_media_id_are_accepted() -> None:
    media_id = "media-0123456789abcdef0123456789abcdef"
    document = _canonical_document(
        html=(
            "<div role='group'><button id='run' type='button'>Run</button>"
            f"<img data-media-id='{media_id}' alt='Graph'></div>"
        ),
        media_id=media_id,
    )

    assert validate_draft_document_references(
        document,
        available_media_bindings=(_media_binding(document),),
    ) == frozenset({media_id})


def test_semantic_media_id_suffix_is_validated_and_collected() -> None:
    media_id = "media-0123456789abcdef0123456789abcdef"
    document = _canonical_document(
        canvas={"thumbnailMediaId": media_id},
        media_id=media_id,
    )

    assert validate_draft_document_references(
        document,
        available_media_bindings=(_media_binding(document),),
    ) == frozenset({media_id})


def test_unused_media_manifest_item_is_rejected() -> None:
    media_id = "media-0123456789abcdef0123456789abcdef"
    document = _canonical_document(media_id=media_id)

    with pytest.raises(InvalidDraftDocument, match="match referenced media exactly"):
        validate_draft_document_references(
            document,
            available_media_bindings=(_media_binding(document),),
        )


@pytest.mark.parametrize(
    "download_path",
    [
        "foo/bar",
        "/api/yfeistai/v1/artifacts/job-a/media/other.png",
        "/api/yfeistai/v1/artifacts/job-a/media%2Fmedia-a.png",
    ],
)
def test_media_manifest_requires_controlled_path_bound_to_relative_path(
    download_path: str,
) -> None:
    media_id = "media-0123456789abcdef0123456789abcdef"
    document = _canonical_document(
        canvas={"thumbnailMediaId": media_id},
        media_id=media_id,
        media_download_path=download_path,
    )

    with pytest.raises(InvalidDraftDocument, match="controlled artifact path"):
        validate_draft_document_references(
            document,
            available_media_bindings=(_media_binding(document),),
        )


def test_validation_report_has_nine_named_sections_and_explicit_severity() -> None:
    report = build_validation_report(
        _canonical_document(html="<script>alert(1)</script>"),
        required_knowledge_point_ids=("kp-motion",),
        grounded=True,
        available_media_bindings=(),
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
