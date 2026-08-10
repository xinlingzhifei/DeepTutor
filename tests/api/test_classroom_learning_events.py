from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


def _context(user_id: str = "student-a") -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name=tenant_schema_name("tenant-a"),
        user_id=user_id,
        permissions=frozenset(),
    )


def _event(event_id: str = "event-1") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": "scene.completed",
        "occurred_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC).isoformat(),
        "scene_id": "scene-1",
        "knowledge_point_id": "kp-1",
    }


class _SessionService:
    def __init__(self) -> None:
        self.create_calls: list[tuple[object, object, object]] = []
        self.cursor_calls: list[tuple[object, str, object]] = []

    async def create(self, context, *, assignment_id=None, student_asset_id=None):
        self.create_calls.append((context, assignment_id, student_asset_id))
        return SimpleNamespace(
            id="session-a",
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            classroom_version_id="version-a",
            assignment_id=assignment_id,
            student_asset_id=student_asset_id,
            status="active",
            last_cursor={"last_event_seq": 0},
            started_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            completed_at=None,
        )

    async def get(self, context, *, session_id):
        return await self.create(context, assignment_id="assignment-a")

    async def update_cursor(self, context, *, session_id, cursor):
        self.cursor_calls.append((context, session_id, cursor))
        record = await self.create(context, assignment_id="assignment-a")
        return SimpleNamespace(**{**record.__dict__, "last_cursor": cursor})

    async def complete(self, context, *, session_id):
        record = await self.create(context, assignment_id="assignment-a")
        return SimpleNamespace(
            **{
                **record.__dict__,
                "status": "completed",
                "completed_at": datetime(2026, 8, 10, 12, 1, tzinfo=UTC),
            }
        )

    async def issue_event_ticket(self, context, *, session_id, ttl_seconds=300):
        assert (context.tenant_id, context.user_id, session_id, ttl_seconds) == (
            "tenant-a",
            "student-a",
            "session-a",
            300,
        )
        return "event-ticket"


class _IngestionService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, str, object]] = []

    async def ingest(self, context, *, session_id, token, batch):
        self.calls.append((context, session_id, token, batch))
        return SimpleNamespace(
            accepted=(SimpleNamespace(event_id="event-1", seq=1),),
            duplicate=(),
            quarantined=(),
        )


def _client(
    session_service: _SessionService | None = None,
    ingestion_service: _IngestionService | None = None,
) -> tuple[TestClient, _SessionService, _IngestionService]:
    from deeptutor.api.routers import classroom_learning

    sessions = session_service or _SessionService()
    ingestion = ingestion_service or _IngestionService()
    application = FastAPI()
    application.include_router(classroom_learning.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = _context
    application.dependency_overrides[classroom_learning.get_learning_session_service] = lambda: (
        sessions
    )
    application.dependency_overrides[classroom_learning.get_learning_event_ingestion_service] = (
        lambda: ingestion
    )
    return TestClient(application), sessions, ingestion


def test_session_create_forbids_all_client_authority_fields() -> None:
    client, sessions, _ = _client()

    response = client.post(
        "/api/v1/classroom-sessions",
        json={
            "assignment_id": "assignment-a",
            "tenant_id": "tenant-forged",
            "user_id": "user-forged",
            "session_id": "session-forged",
            "classroom_version_id": "version-forged",
        },
    )

    assert response.status_code == 422
    assert sessions.create_calls == []


def test_event_api_forbids_client_identity_and_uses_authenticated_context() -> None:
    client, _, ingestion = _client()
    forged = _event()
    forged["tenant_id"] = "tenant-forged"

    rejected = client.post(
        "/api/v1/classroom-sessions/session-a/events",
        headers={"X-Classroom-Ticket": "event-ticket"},
        json={"events": [forged]},
    )
    accepted = client.post(
        "/api/v1/classroom-sessions/session-a/events",
        headers={"X-Classroom-Ticket": "event-ticket"},
        json={"events": [_event()]},
    )

    assert rejected.status_code == 422
    assert accepted.status_code == 202
    assert accepted.json() == {
        "accepted": [{"event_id": "event-1", "seq": 1}],
        "duplicate": [],
        "quarantined": [],
    }
    assert len(ingestion.calls) == 1
    context, session_id, token, batch = ingestion.calls[0]
    assert (context.tenant_id, context.user_id, session_id, token) == (
        "tenant-a",
        "student-a",
        "session-a",
        "event-ticket",
    )
    assert batch.events[0].event_id == "event-1"


def test_event_api_rejects_actual_body_larger_than_256_kib() -> None:
    client, _, ingestion = _client()
    body = b'{"events":[]}' + (b" " * 262_145)

    response = client.post(
        "/api/v1/classroom-sessions/session-a/events",
        headers={
            "X-Classroom-Ticket": "event-ticket",
            "Content-Type": "application/json",
            "Content-Length": "1",
        },
        content=body,
    )

    assert response.status_code == 413
    assert ingestion.calls == []


def test_session_cursor_get_complete_and_event_ticket_are_server_bound() -> None:
    client, sessions, _ = _client()

    created = client.post(
        "/api/v1/classroom-sessions",
        json={"assignment_id": "assignment-a"},
    )
    fetched = client.get("/api/v1/classroom-sessions/session-a")
    ticket = client.post("/api/v1/classroom-sessions/session-a/event-ticket")
    cursor = client.put(
        "/api/v1/classroom-sessions/session-a/cursor",
        json={"cursor": {"scene_id": "scene-1", "last_event_seq": 1}},
    )
    completed = client.post("/api/v1/classroom-sessions/session-a/complete")

    assert created.status_code == 201
    assert created.json()["classroom_version_id"] == "version-a"
    assert fetched.status_code == 200
    assert ticket.json() == {"ticket": "event-ticket", "expires_in": 300}
    assert cursor.status_code == 200
    assert cursor.json()["last_cursor"]["scene_id"] == "scene-1"
    assert completed.json()["status"] == "completed"
    assert sessions.cursor_calls[0][2] == {"scene_id": "scene-1", "last_event_seq": 1}
