from __future__ import annotations

from dataclasses import replace
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import auth as auth_router
from deeptutor.api.routers import classroom_exports as exports_router
from deeptutor.teaching.object_store import (
    ObjectStoreAccessDenied,
    ObjectStoreError,
    ObjectStoreIntegrityError,
)
from deeptutor.teaching.services.exports import (
    ExportPolicyDenied,
    ExportRecord,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


def _context(*, tenant_id: str = "tenant-a") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=f"tenant_{tenant_id}",
        user_id="teacher-a",
        permissions=frozenset(),
    )


def _record(*, status: str = "queued") -> ExportRecord:
    return ExportRecord(
        tenant_id="tenant-a",
        export_id="export-a",
        job_id="export-a",
        idempotency_key="request-key",
        request_sha256="a" * 64,
        created_by="teacher-a",
        owner_id="teacher-a",
        course_id="course-a",
        class_id="class-a",
        asset_id="asset-a",
        export_format="pptx",
        classroom_draft_id="draft-a",
        classroom_version_id=None,
        draft_revision=3,
        input_document_sha256="b" * 64,
        input_media_manifest_sha256="c" * 64,
        status=status,  # type: ignore[arg-type]
        progress_percent=100 if status == "succeeded" else 20,
        relative_name="lesson.pptx" if status == "succeeded" else None,
        object_key=(
            "tenants/tenant-a/classrooms/asset-a/exports/export-a/lesson.pptx"
            if status == "succeeded"
            else None
        ),
        sha256="d" * 64 if status == "succeeded" else None,
        size_bytes=15 if status == "succeeded" else None,
        mime_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            if status == "succeeded"
            else None
        ),
    )


class FakeService:
    def __init__(self) -> None:
        self.record = _record()
        self.draft_calls: list[tuple[object, ...]] = []
        self.version_calls: list[tuple[object, ...]] = []
        self.policy_denied = False
        self.hidden = False
        self.create_error: Exception | None = None

    async def create_for_draft(
        self,
        context,
        asset_id,
        export_format,
        *,
        expected_revision,
        idempotency_key,
    ):
        self.draft_calls.append(
            (context, asset_id, export_format, expected_revision, idempotency_key)
        )
        if self.policy_denied:
            raise ExportPolicyDenied("disabled")
        if self.create_error is not None:
            raise self.create_error
        return self.record

    async def create_for_version(
        self,
        context,
        version_id,
        export_format,
        *,
        idempotency_key,
    ):
        self.version_calls.append(
            (context, version_id, export_format, idempotency_key)
        )
        if self.create_error is not None:
            raise self.create_error
        return replace(
            self.record,
            classroom_draft_id=None,
            classroom_version_id=version_id,
            draft_revision=None,
            export_format=export_format,
        )

    async def get(self, context, export_id):
        if self.hidden or export_id != self.record.export_id:
            return None
        return self.record


class FakeStore:
    def __init__(self) -> None:
        self.presign_calls: list[tuple[str, int]] = []
        self.open_calls: list[str] = []
        self.signed_url = "https://signed.example/token"
        self.open_error: Exception | None = None

    async def presign_download(self, key: str, expires_seconds: int) -> str:
        self.presign_calls.append((key, expires_seconds))
        return self.signed_url

    async def open(self, key: str):
        self.open_calls.append(key)
        if self.open_error is not None:
            raise self.open_error

        async def chunks():
            yield b"verified-export"

        return chunks()


class FakeStores:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.tenants: list[str] = []

    async def store_for_tenant(self, tenant_id: str):
        self.tenants.append(tenant_id)
        return self.store


@pytest.fixture
def api_harness():
    app = FastAPI()
    service = FakeService()
    store = FakeStore()
    stores = FakeStores(store)
    app.include_router(exports_router.router, prefix="/api/v1")
    app.dependency_overrides[auth_router.require_platform_enabled] = lambda: None
    app.dependency_overrides[require_tenant] = _context
    app.dependency_overrides[exports_router.get_classroom_export_service] = (
        lambda: service
    )
    app.dependency_overrides[exports_router.get_export_store_provider] = lambda: stores
    return app, service, stores, store


def test_draft_export_requires_revision_and_returns_safe_job_envelope(api_harness) -> None:
    app, service, _stores, _store = api_harness

    response = TestClient(app).post(
        "/api/v1/classrooms/asset-a/draft/exports",
        headers={
            "If-Match": '"revision-3"',
            "Idempotency-Key": "request-key",
        },
        json={"format": "pptx"},
    )

    assert response.status_code == 202
    assert service.draft_calls[0][1:] == (
        "asset-a",
        "pptx",
        3,
        "request-key",
    )
    assert response.json() == {
        "job_id": "export-a",
        "job_kind": "export",
        "phase": "export",
        "status": "queued",
        "progress_percent": 20,
        "waiting_reason": None,
        "cancellable": True,
        "retryable": False,
        "outline": None,
        "error_category": None,
        "error_code": None,
        "retry_of_job_id": None,
        "export_format": "pptx",
        "download_ready": False,
    }
    assert "object" not in json.dumps(response.json()).lower()


def test_draft_export_rejects_invalid_if_match_before_service_call(api_harness) -> None:
    app, service, _stores, _store = api_harness

    response = TestClient(app).post(
        "/api/v1/classrooms/asset-a/draft/exports",
        headers={"If-Match": "revision-3", "Idempotency-Key": "request-key"},
        json={"format": "pptx"},
    )

    assert response.status_code == 400
    assert service.draft_calls == []


def test_version_export_does_not_require_if_match(api_harness) -> None:
    app, service, _stores, _store = api_harness

    response = TestClient(app).post(
        "/api/v1/classroom-versions/version-a/exports",
        headers={"Idempotency-Key": "request-key"},
        json={"format": "offline_html"},
    )

    assert response.status_code == 202
    assert service.version_calls[0][1:] == (
        "version-a",
        "offline_html",
        "request-key",
    )


def test_mp4_policy_denial_has_stable_public_reason(api_harness) -> None:
    app, service, _stores, _store = api_harness
    service.policy_denied = True

    response = TestClient(app).post(
        "/api/v1/classrooms/asset-a/draft/exports",
        headers={
            "If-Match": '"revision-3"',
            "Idempotency-Key": "request-key",
        },
        json={"format": "mp4"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "MP4_EXPORT_DISABLED_BY_TENANT_POLICY"


def test_other_tenant_or_unauthorized_actor_observes_export_as_not_found(
    api_harness,
) -> None:
    app, service, stores, _store = api_harness
    service.hidden = True

    status_response = TestClient(app).get("/api/v1/classroom-exports/export-a")
    download_response = TestClient(app).get(
        "/api/v1/classroom-exports/export-a/download"
    )

    assert status_response.status_code == 404
    assert download_response.status_code == 404
    assert stores.tenants == []


def test_download_streams_by_default_without_exposing_the_object_key(api_harness) -> None:
    app, service, stores, store = api_harness
    service.record = _record(status="succeeded")

    status_response = TestClient(app).get("/api/v1/classroom-exports/export-a")
    download_response = TestClient(app).get(
        "/api/v1/classroom-exports/export-a/download?expiresSeconds=3600"
    )

    assert status_response.status_code == 200
    assert status_response.json()["download_ready"] is True
    assert service.record.object_key not in json.dumps(status_response.json())
    assert download_response.status_code == 200
    assert download_response.content == b"verified-export"
    assert download_response.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''lesson.pptx"
    )
    assert stores.tenants == ["tenant-a"]
    assert store.presign_calls == []
    assert store.open_calls == [service.record.object_key]


def test_download_remains_a_controlled_stream_even_if_store_can_presign(
    api_harness,
) -> None:
    app, service, stores, store = api_harness
    service.record = _record(status="succeeded")

    response = TestClient(app).get(
        "/api/v1/classroom-exports/export-a/download",
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "location" not in response.headers
    assert response.content == b"verified-export"
    assert stores.tenants == ["tenant-a"]
    assert store.presign_calls == []
    assert store.open_calls == [service.record.object_key]


def test_download_rejects_an_export_that_is_not_ready_without_opening_storage(
    api_harness,
) -> None:
    app, _service, stores, store = api_harness

    response = TestClient(app).get("/api/v1/classroom-exports/export-a/download")

    assert response.status_code == 409
    assert stores.tenants == []
    assert store.open_calls == []


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ObjectStoreIntegrityError("corrupt"), 409),
        (ObjectStoreAccessDenied("wrong tenant"), 409),
        (ObjectStoreError("storage failed"), 503),
    ],
)
def test_create_maps_object_store_failures_without_exposing_details(
    api_harness,
    error: Exception,
    expected_status: int,
) -> None:
    app, service, _stores, _store = api_harness
    service.create_error = error

    response = TestClient(app).post(
        "/api/v1/classroom-versions/version-a/exports",
        headers={"Idempotency-Key": "request-store-failure"},
        json={"format": "pptx"},
    )

    assert response.status_code == expected_status
    assert str(error) not in response.text


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ObjectStoreAccessDenied("wrong tenant"), 404),
        (ObjectStoreIntegrityError("corrupt"), 503),
        (ObjectStoreError("storage failed"), 503),
    ],
)
def test_download_maps_object_store_failures_without_exposing_details(
    api_harness,
    error: Exception,
    expected_status: int,
) -> None:
    app, service, _stores, store = api_harness
    service.record = _record(status="succeeded")
    store.open_error = error

    response = TestClient(app).get("/api/v1/classroom-exports/export-a/download")

    assert response.status_code == expected_status
    assert str(error) not in response.text


def test_disabled_teaching_does_not_register_export_routes() -> None:
    from deeptutor.api.main import _register_classroom_export_routes

    app = FastAPI()

    assert not _register_classroom_export_routes(app, enabled=False, dependencies=[])
    assert all("classroom-exports" not in route.path for route in app.routes)
