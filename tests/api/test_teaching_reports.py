from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deeptutor.teaching.permissions import ResourceScope, permissions_for_roles
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


class _ReportRepository:
    async def student_in_class(self, tenant_id: str, class_id: str, user_id: str):
        return user_id == "student-a"

    async def class_scope(self, tenant_id: str, class_id: str):
        scopes = {
            "class-a": ResourceScope(tenant_id, "course-a", "class-a"),
            "class-b": ResourceScope(tenant_id, "course-b", "class-b"),
        }
        return scopes.get(class_id)

    async def class_report(self, tenant_id: str, class_id: str, user_id: str | None = None):
        from deeptutor.teaching.services.reports import LearningReportMetrics

        return LearningReportMetrics(
            session_count=4,
            completed_count=3,
            completion_rate=0.75,
            completed_scene_count=12,
            valid_quiz_count=5,
            correct_quiz_count=4,
            hint_count=2,
            pbl_milestone_count=1,
            mastery=({"knowledge_point_id": "kp-1", "level": 0.8, "evidence_count": 5},),
            projection_lag_seconds=8.5,
        )

    async def version_scopes(self, tenant_id: str, version_id: str):
        if version_id == "version-private":
            return (ResourceScope(tenant_id),)
        if version_id != "version-a":
            return None
        return (ResourceScope(tenant_id, "course-a", "class-a"),)

    async def classroom_report(self, tenant_id: str, version_id: str, access):
        if version_id == "version-private":
            assert access.tenant_wide
            assert not access.class_ids
        return await self.class_report(tenant_id, "class-a")

    async def quarantine(self, tenant_id: str, access):
        assert tenant_id == "tenant-a"
        assert access.class_ids == frozenset({"class-a"})
        return (
            {
                "event_id": "event-unsafe",
                "event_type": "quiz.graded",
                "classroom_version_id": "version-a",
                "reason_code": "quiz_answer_invalid",
                "quarantined_at": "2026-08-13T08:00:00Z",
                "payload": {"answer": "SECRET-ANSWER"},
                "details": {"provider": "SECRET-PROVIDER"},
            },
        )


def _context(
    *,
    role: str = "teacher",
    scope_type: str = "class",
    scope_id: str = "class-a",
    tenant_id: str = "tenant-a",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=tenant_schema_name(tenant_id),
        user_id="teacher-a" if role != "student" else "student-a",
        permissions=permissions_for_roles(
            {role},
            scope_type=scope_type,
            scope_id=scope_id,
            tenant_id=tenant_id,
        ),
    )


def _client(context: TenantContext) -> TestClient:
    from deeptutor.api.routers import teaching_reports
    from deeptutor.teaching.services.reports import TeachingReportService

    app = FastAPI()
    app.include_router(teaching_reports.router, prefix="/api/v1/teaching-reports")
    app.dependency_overrides[require_tenant] = lambda: context
    app.dependency_overrides[teaching_reports.get_teaching_report_service] = lambda: (
        TeachingReportService(_ReportRepository())
    )
    return TestClient(app)


def test_teacher_class_report_is_scoped_and_exposes_required_metrics() -> None:
    client = _client(_context())

    allowed = client.get("/api/v1/teaching-reports/classes/class-a")
    denied = client.get("/api/v1/teaching-reports/classes/class-b")

    assert allowed.status_code == 200
    assert allowed.json() == {
        "classId": "class-a",
        "sessionCount": 4,
        "completedCount": 3,
        "completionRate": 0.75,
        "completedSceneCount": 12,
        "validQuizCount": 5,
        "correctQuizCount": 4,
        "hintCount": 2,
        "pblMilestoneCount": 1,
        "mastery": [{"knowledgePointId": "kp-1", "level": 0.8, "evidenceCount": 5}],
        "projectionLagSeconds": 8.5,
    }
    assert denied.status_code == 403


def test_student_report_and_classroom_report_reuse_the_authorized_scope() -> None:
    client = _client(_context())

    student = client.get("/api/v1/teaching-reports/classes/class-a/students/student-a")
    classroom = client.get("/api/v1/teaching-reports/classrooms/version-a")

    assert student.status_code == 200
    assert student.json()["userId"] == "student-a"
    assert student.json()["validQuizCount"] == 5
    assert classroom.status_code == 200
    assert classroom.json()["classroomVersionId"] == "version-a"
    assert classroom.json()["pblMilestoneCount"] == 1


def test_private_classroom_report_requires_tenant_wide_learning_event_read() -> None:
    class_scoped = _client(_context())
    tenant_scoped = _client(
        _context(
            role="org_admin",
            scope_type="tenant",
            scope_id="tenant-a",
        )
    )

    assert (
        class_scoped.get("/api/v1/teaching-reports/classrooms/version-private").status_code == 403
    )
    allowed = tenant_scoped.get("/api/v1/teaching-reports/classrooms/version-private")
    assert allowed.status_code == 200
    assert allowed.json()["classroomVersionId"] == "version-private"


def test_student_report_does_not_enumerate_a_user_outside_the_class() -> None:
    response = _client(_context()).get(
        "/api/v1/teaching-reports/classes/class-a/students/student-b"
    )

    assert response.status_code == 404


def test_student_role_and_other_tenant_cannot_read_reports() -> None:
    student = _client(_context(role="student"))
    other_tenant = _client(replace(_context(), tenant_id="tenant-b"))

    assert student.get("/api/v1/teaching-reports/classes/class-a").status_code == 403
    assert other_tenant.get("/api/v1/teaching-reports/classes/class-a").status_code == 403


def test_report_context_rejects_a_mismatched_physical_schema() -> None:
    response = _client(replace(_context(), schema_name=tenant_schema_name("tenant-b"))).get(
        "/api/v1/teaching-reports/classes/class-a"
    )

    assert response.status_code == 403


def test_quarantine_requires_learning_event_read_and_never_returns_raw_payload() -> None:
    response = _client(_context()).get("/api/v1/teaching-reports/quarantine")

    assert response.status_code == 200
    body = response.text
    assert response.json()["items"][0]["reasonCode"] == "quiz_answer_invalid"
    assert "payload" not in body
    assert "details" not in body
    assert "SECRET-ANSWER" not in body
    assert "SECRET-PROVIDER" not in body


def test_teaching_report_routes_register_only_with_the_platform() -> None:
    from deeptutor.api.main import _register_classroom_learning_routes

    disabled = FastAPI()
    assert not _register_classroom_learning_routes(
        disabled,
        enabled=False,
        dependencies=[],
    )
    assert all("teaching-reports" not in route.path for route in disabled.routes)

    enabled = FastAPI()
    assert _register_classroom_learning_routes(
        enabled,
        enabled=True,
        dependencies=[],
    )
    paths = {route.path for route in enabled.routes}
    assert "/api/v1/teaching-reports/classes/{class_id}" in paths
    assert "/api/v1/teaching-reports/quarantine" in paths
