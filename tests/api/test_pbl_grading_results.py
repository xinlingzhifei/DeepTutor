from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, object]] = []

    async def record(self, context, *, session_id, command):
        self.calls.append((context, session_id, command))
        return SimpleNamespace(
            result_id="result-a",
            event_id=command.event_id,
            passed=command.passed,
            score=command.score,
            source_reference=command.source_reference,
            grading_source="teacher_review",
            graded_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
        )


def _client() -> tuple[TestClient, _Service]:
    from deeptutor.api.routers import classroom_learning

    service = _Service()
    context = TenantContext(
        tenant_id="tenant-a",
        schema_name=tenant_schema_name("tenant-a"),
        user_id="teacher-a",
        permissions=frozenset(),
    )
    application = FastAPI()
    application.include_router(classroom_learning.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = lambda: context
    application.dependency_overrides[classroom_learning.get_pbl_grading_service] = lambda: service
    return TestClient(application), service


def test_teacher_result_api_accepts_only_minimal_request_and_derives_source() -> None:
    client, service = _client()

    response = client.post(
        "/api/v1/classroom-sessions/session-a/pbl-results",
        headers={"Idempotency-Key": "grade-request-1"},
        json={
            "eventId": "event-a",
            "passed": True,
            "score": 0.8,
            "sourceReference": "review-42",
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "resultId": "result-a",
        "eventId": "event-a",
        "passed": True,
        "score": 0.8,
        "sourceReference": "review-42",
        "gradingSource": "teacher_review",
        "gradedAt": "2026-08-25T12:00:00Z",
    }
    assert len(service.calls) == 1
    _context, session_id, command = service.calls[0]
    assert session_id == "session-a"
    assert command.idempotency_key == "grade-request-1"


def test_teacher_result_api_forbids_client_selected_authority_fields() -> None:
    client, service = _client()

    for field, value in (
        ("gradingSource", "teacher_review"),
        ("correctness", True),
        ("graderId", "teacher-forged"),
        ("gradedBy", "teacher-forged"),
        ("knowledgePointId", "kp-forged"),
        ("sceneId", "scene-forged"),
        ("milestoneId", "milestone-forged"),
        ("classId", "class-forged"),
        ("tenantId", "tenant-forged"),
        ("userId", "student-forged"),
        ("sessionId", "session-forged"),
        ("classroomVersionId", "version-forged"),
    ):
        response = client.post(
            "/api/v1/classroom-sessions/session-a/pbl-results",
            headers={"Idempotency-Key": "grade-request-1"},
            json={
                "eventId": "event-a",
                "passed": True,
                "sourceReference": "review-42",
                field: value,
            },
        )
        assert response.status_code == 422, field
    assert service.calls == []


def test_teacher_result_api_validates_score_source_and_idempotency_header() -> None:
    client, service = _client()

    for headers, payload in (
        ({}, {"eventId": "event-a", "passed": True, "sourceReference": "review-42"}),
        (
            {"Idempotency-Key": "short"},
            {"eventId": "event-a", "passed": True, "sourceReference": "review-42"},
        ),
        (
            {"Idempotency-Key": "grade-request-1"},
            {"eventId": "event-a", "passed": True, "sourceReference": "   "},
        ),
        (
            {"Idempotency-Key": "grade-request-1"},
            {
                "eventId": "event-a",
                "passed": False,
                "score": 1.01,
                "sourceReference": "review-42",
            },
        ),
    ):
        assert (
            client.post(
                "/api/v1/classroom-sessions/session-a/pbl-results",
                headers=headers,
                json=payload,
            ).status_code
            == 422
        )
    assert service.calls == []
