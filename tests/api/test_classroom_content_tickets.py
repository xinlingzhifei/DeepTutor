from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest
from starlette.requests import ClientDisconnect

from deeptutor.teaching.permissions import permissions_for_roles
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
        return _StreamingResource(
            (b'{"schemaVersion":"1.0","safe":true}',),
            mime_type="application/json",
            sha256="a" * 64,
        )

    async def open_media(self, context, *, version_id, media_id, token):
        self.read_calls.append(("media", context, f"{version_id}:{media_id}", token))
        if token == "ticket-for-media-b":
            raise TicketScopeError("wrong resource")
        return _StreamingResource(
            (b"media-a",),
            mime_type="image/png",
            sha256="b" * 64,
        )


class _StreamingResource:
    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        mime_type: str = "application/octet-stream",
        sha256: str = "c" * 64,
        filename: str | None = None,
    ) -> None:
        self.chunks = chunks
        self.body = b"".join(chunks)
        self.mime_type = mime_type
        self.sha256 = sha256
        self.size_bytes = len(self.body)
        self.filename = filename
        self.closed = False

    def iter_chunks(self):
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


def _asgi_scope() -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/content",
        "raw_path": b"/content",
        "query_string": b"",
        "headers": [],
        "client": ("test", 123),
        "server": ("test", 80),
    }


@pytest.mark.asyncio
async def test_content_response_sends_multiple_chunks_and_closes_resource() -> None:
    from deeptutor.api.routers import classroom_content

    resource = _StreamingResource((b"chunk-a", b"chunk-b", b"chunk-c"))
    response = classroom_content.classroom_content_response(resource)
    messages: list[dict[str, object]] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await response(_asgi_scope(), receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    headers = {key.decode(): value.decode() for key, value in start["headers"]}
    assert [
        message["body"]
        for message in messages
        if message["type"] == "http.response.body" and message.get("body")
    ] == list(resource.chunks)
    assert headers["content-length"] == str(resource.size_bytes)
    assert headers["etag"] == f'"sha256-{resource.sha256}"'
    assert resource.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RuntimeError, RuntimeError),
        (OSError, ClientDisconnect),
        (asyncio.CancelledError, asyncio.CancelledError),
    ],
)
async def test_content_response_closes_resource_on_stream_failure(failure, expected) -> None:
    from deeptutor.api.routers import classroom_content

    resource = _StreamingResource((b"chunk-a", b"chunk-b"))
    response = classroom_content.classroom_content_response(resource)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise failure("stream interrupted")

    with pytest.raises(expected):
        await response(_asgi_scope(), receive, send)

    assert resource.closed


def _client(service: _ContentService | None = None) -> tuple[TestClient, _ContentService]:
    from deeptutor.api.routers import classroom_content

    selected = service or _ContentService()
    application = FastAPI()
    application.include_router(classroom_content.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = _context
    application.dependency_overrides[classroom_content.get_classroom_content_service] = lambda: (
        selected
    )
    application.dependency_overrides[classroom_content.get_classroom_content_reader_service] = (
        lambda: selected
    )
    application.dependency_overrides[classroom_content.get_classroom_content_service_factory] = (
        lambda: lambda: selected
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


def test_teacher_document_and_media_do_not_construct_ticket_backed_service() -> None:
    from deeptutor.api.routers import classroom_content

    owner = _context("teacher-owner")
    teacher = TenantContext(
        tenant_id="tenant-a",
        schema_name=tenant_schema_name("tenant-a"),
        user_id="teacher-scoped",
        permissions=permissions_for_roles(
            {"teacher"},
            scope_type="class",
            scope_id="class-a",
            tenant_id="tenant-a",
        ),
    )
    reader = _ContentService()
    ticket_provider_calls = 0

    def fail_if_ticket_service_is_constructed():
        nonlocal ticket_provider_calls
        ticket_provider_calls += 1
        raise RuntimeError("ticket-backed content service must stay lazy")

    application = FastAPI()
    application.include_router(classroom_content.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = lambda: owner
    application.dependency_overrides[classroom_content.get_classroom_content_reader_service] = (
        lambda: reader
    )
    application.dependency_overrides[classroom_content.get_classroom_content_service] = (
        fail_if_ticket_service_is_constructed
    )
    client = TestClient(application, raise_server_exceptions=False)

    document = client.get("/api/v1/classroom-versions/version-a/document")
    application.dependency_overrides[require_tenant] = lambda: teacher
    media = client.get("/api/v1/classroom-versions/version-a/media/media-a")

    assert document.status_code == 200
    assert media.status_code == 200
    assert ticket_provider_calls == 0
    assert reader.read_calls == [
        ("document", owner, "version-a", None),
        ("media", teacher, "version-a:media-a", None),
    ]


def test_ticketed_media_constructs_ticket_service_and_never_falls_back() -> None:
    from deeptutor.api.routers import classroom_content

    reader = _ContentService()
    ticketed = _ContentService()
    ticket_provider_calls = 0

    def build_ticket_service():
        nonlocal ticket_provider_calls
        ticket_provider_calls += 1
        return ticketed

    application = FastAPI()
    application.include_router(classroom_content.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = _context
    application.dependency_overrides[classroom_content.get_classroom_content_reader_service] = (
        lambda: reader
    )
    application.dependency_overrides[classroom_content.get_classroom_content_service_factory] = (
        lambda: build_ticket_service
    )
    client = TestClient(application, raise_server_exceptions=False)

    response = client.get(
        "/api/v1/classroom-versions/version-a/media/media-a",
        headers={"X-Classroom-Ticket": "ticket-for-media-b"},
    )

    assert response.status_code == 403
    assert ticket_provider_calls == 1
    assert ticketed.read_calls == [("media", _context(), "version-a:media-a", "ticket-for-media-b")]
    assert reader.read_calls == []


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
        ("unavailable", 503),
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
        ClassroomContentUnavailable,
    )

    errors = {
        "denied": ClassroomContentAccessDenied("private detail"),
        "not_found": ClassroomContentNotFound("private detail"),
        "integrity": ClassroomContentIntegrityError("private detail"),
        "unavailable": ClassroomContentUnavailable("private detail"),
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
