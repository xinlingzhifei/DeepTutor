from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest

from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext, require_tenant
from deeptutor.teaching.tickets import TicketScopeError


def _context(user_id: str = "student-a") -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name=tenant_schema_name("tenant-a"),
        user_id=user_id,
        permissions=frozenset(),
    )


class _ContentService:
    def __init__(self) -> None:
        self.ticket_calls: list[tuple[object, str, str, str]] = []
        self.read_calls: list[tuple[str, object, str, str | None]] = []
        self.read_error: Exception | None = None

    async def issue_read_ticket(
        self,
        context,
        *,
        session_id,
        action,
        resource_id,
        ttl_seconds=60,
    ):
        assert ttl_seconds == 60
        self.ticket_calls.append((context, session_id, action, resource_id))
        return "read-ticket"

    async def open_document(self, context, *, version_id, token):
        self.read_calls.append(("document", context, version_id, token))
        if self.read_error is not None:
            raise self.read_error
        return SimpleNamespace(
            body=b'{"schemaVersion":"1.0","safe":true}',
            mime_type="application/json",
            sha256="a" * 64,
            size_bytes=35,
            filename=None,
        )

    async def open_media(self, context, *, version_id, media_id, token):
        self.read_calls.append(("media", context, f"{version_id}:{media_id}", token))
        if token == "ticket-for-media-b":
            raise TicketScopeError("wrong resource")
        return SimpleNamespace(
            body=b"media-a",
            mime_type="image/png",
            sha256="b" * 64,
            size_bytes=7,
            filename=None,
        )


def _client(service: _ContentService | None = None) -> tuple[TestClient, _ContentService]:
    from deeptutor.api.routers import classroom_content

    selected = service or _ContentService()
    application = FastAPI()
    application.include_router(classroom_content.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = _context
    application.dependency_overrides[classroom_content.get_classroom_content_service] = lambda: (
        selected
    )
    return TestClient(application, raise_server_exceptions=False), selected


def test_read_ticket_request_is_resource_scoped_and_forbids_authority_fields() -> None:
    client, service = _client()

    issued = client.post(
        "/api/v1/classroom-sessions/session-a/read-ticket",
        json={"action": "classroom.media.read", "resource_id": "media-a"},
    )
    forged = client.post(
        "/api/v1/classroom-sessions/session-a/read-ticket",
        json={
            "action": "classroom.media.read",
            "resource_id": "media-a",
            "user_id": "student-forged",
            "classroom_version_id": "version-forged",
        },
    )

    assert issued.status_code == 200
    assert issued.json() == {"ticket": "read-ticket", "expires_in": 60}
    assert forged.status_code == 422
    assert len(service.ticket_calls) == 1
    context, session_id, action, resource_id = service.ticket_calls[0]
    assert (context.user_id, session_id, action, resource_id) == (
        "student-a",
        "session-a",
        "classroom.media.read",
        "media-a",
    )


def test_student_media_route_rejects_ticket_for_another_resource() -> None:
    client, service = _client()

    response = client.get(
        "/api/v1/classroom-versions/version-a/media/media-a",
        headers={"X-Classroom-Ticket": "ticket-for-media-b"},
    )

    assert response.status_code == 403
    assert service.read_calls == [("media", _context(), "version-a:media-a", "ticket-for-media-b")]


def test_document_and_media_stream_from_backend_without_storage_identifiers() -> None:
    client, service = _client()

    document = client.get(
        "/api/v1/classroom-versions/version-a/document",
        headers={"X-Classroom-Ticket": "document-ticket"},
    )
    teacher_media = client.get("/api/v1/classroom-versions/version-a/media/media-a")

    assert document.status_code == 200
    assert document.headers["cache-control"] == "private, no-store"
    assert document.json() == {"schemaVersion": "1.0", "safe": True}
    assert "object_key" not in document.text
    assert "openmaic" not in document.text.lower()
    assert teacher_media.status_code == 200
    assert teacher_media.content == b"media-a"
    assert service.read_calls == [
        ("document", _context(), "version-a", "document-ticket"),
        ("media", _context(), "version-a:media-a", None),
    ]


def test_every_content_route_keeps_require_tenant_login_dependency() -> None:
    from deeptutor.api.routers import classroom_content

    routes = [route for route in classroom_content.router.routes if isinstance(route, APIRoute)]
    assert routes
    for route in routes:
        dependency_calls = {
            dependency.call
            for dependency in route.dependant.dependencies
            if dependency.call is not None
        }
        assert require_tenant in dependency_calls


@pytest.mark.parametrize(
    ("error_name", "expected_status"),
    [
        ("denied", 403),
        ("not_found", 404),
        ("integrity", 503),
    ],
)
def test_content_failures_have_stable_sanitized_status(
    error_name: str,
    expected_status: int,
) -> None:
    from deeptutor.teaching.services.classroom_content import (
        ClassroomContentAccessDenied,
        ClassroomContentIntegrityError,
        ClassroomContentNotFound,
    )

    errors = {
        "denied": ClassroomContentAccessDenied("private detail"),
        "not_found": ClassroomContentNotFound("private detail"),
        "integrity": ClassroomContentIntegrityError("private detail"),
    }
    service = _ContentService()
    service.read_error = errors[error_name]
    client, _ = _client(service)

    response = client.get("/api/v1/classroom-versions/version-a/document")

    assert response.status_code == expected_status
    assert "private detail" not in response.text


def test_learning_and_content_routes_register_only_when_platform_is_enabled() -> None:
    from deeptutor.api.main import _register_classroom_learning_routes

    disabled = FastAPI()
    assert not _register_classroom_learning_routes(
        disabled,
        enabled=False,
        dependencies=[],
    )
    assert all("classroom-sessions" not in route.path for route in disabled.routes)
    assert all("classroom-versions" not in route.path for route in disabled.routes)

    enabled = FastAPI()
    assert _register_classroom_learning_routes(
        enabled,
        enabled=True,
        dependencies=[],
    )
    paths = {route.path for route in enabled.routes}
    assert "/api/v1/classroom-sessions" in paths
    assert "/api/v1/classroom-versions/{version_id}/document" in paths
