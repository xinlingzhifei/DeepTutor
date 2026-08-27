from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import inspect
import re

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import teaching_catalog
from deeptutor.api.routers.auth import require_platform_admin
from deeptutor.services.auth import TokenPayload
from deeptutor.teaching.models import AuditLog, QuotaLedger, StudentSafetyAssessmentRecord
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.repositories.student_generation_administration import (
    QuotaGrantView,
    SqlAlchemyStudentGenerationAdministrationRepository,
    StudentGenerationAdministrationConflict,
    StudentSafetyAssessmentView,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant

NOW = datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc)


def _context() -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant-a",
        user_id="platform-admin-a",
        permissions=permissions_for_roles(
            {"platform_admin"},
            scope_type="tenant",
            scope_id="tenant-a",
            tenant_id="tenant-a",
        ),
    )


def _admin() -> TokenPayload:
    return TokenPayload(
        username="platform-admin-a",
        role="admin",
        user_id="platform-admin-a",
    )


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def grant_quota(self, **kwargs) -> QuotaGrantView:
        self.calls.append(("grant_quota", kwargs))
        return QuotaGrantView(
            grant_id="quota-123",
            tenant_id="tenant-a",
            units=200,
            balance=200,
            created=True,
        )

    async def create_safety_assessment(self, **kwargs) -> StudentSafetyAssessmentView:
        self.calls.append(("create_safety_assessment", kwargs))
        return StudentSafetyAssessmentView(
            assessment_id="safety-123",
            tenant_id="tenant-a",
            course_id="course-a",
            class_id="class-a",
            mode="micro",
            content_mode="open_creation",
            web_search_requested=False,
            generally_safe=True,
            minor_safe=True,
            restricted_topic=False,
            reviewed_by="platform-admin-a",
            reviewed_at=NOW,
            assessment_version=2,
            expires_at=NOW + timedelta(hours=2),
            created=True,
        )


def _client(repository: _Repository) -> TestClient:
    application = FastAPI()
    application.include_router(teaching_catalog.router, prefix="/api/v1/teaching")
    application.dependency_overrides[require_tenant] = _context
    application.dependency_overrides[require_platform_admin] = _admin
    application.dependency_overrides[
        teaching_catalog.get_student_generation_administration_repository
    ] = lambda: repository
    return TestClient(application)


def test_platform_admin_grants_generation_quota_idempotently() -> None:
    repository = _Repository()

    response = _client(repository).post(
        "/api/v1/teaching/generation-quota-grants",
        headers={"Idempotency-Key": "capacity-run-a-tenant-a"},
        json={"units": 200},
    )

    assert response.status_code == 200
    assert response.json() == {
        "grantId": "quota-123",
        "tenantId": "tenant-a",
        "units": 200,
        "balance": 200,
        "created": True,
    }
    assert repository.calls == [
        (
            "grant_quota",
            {
                "actor_id": "platform-admin-a",
                "idempotency_key": "capacity-run-a-tenant-a",
                "units": 200,
            },
        )
    ]


def test_platform_admin_records_server_owned_student_safety_assessment() -> None:
    repository = _Repository()

    response = _client(repository).post(
        "/api/v1/teaching/courses/course-a/classes/class-a/student-safety-assessments",
        headers={"Idempotency-Key": "capacity-run-a-safety-tenant-a"},
        json={
            "mode": "micro",
            "contentMode": "open_creation",
            "webSearchRequested": False,
            "generallySafe": True,
            "minorSafe": True,
            "restrictedTopic": False,
            "validForSeconds": 7200,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "assessmentId": "safety-123",
        "tenantId": "tenant-a",
        "courseId": "course-a",
        "classId": "class-a",
        "mode": "micro",
        "contentMode": "open_creation",
        "webSearchRequested": False,
        "generallySafe": True,
        "minorSafe": True,
        "restrictedTopic": False,
        "reviewedBy": "platform-admin-a",
        "reviewedAt": NOW.isoformat().replace("+00:00", "Z"),
        "assessmentVersion": 2,
        "expiresAt": (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "created": True,
    }
    assert repository.calls == [
        (
            "create_safety_assessment",
            {
                "actor_id": "platform-admin-a",
                "idempotency_key": "capacity-run-a-safety-tenant-a",
                "course_id": "course-a",
                "class_id": "class-a",
                "mode": "micro",
                "content_mode": "open_creation",
                "web_search_requested": False,
                "generally_safe": True,
                "minor_safe": True,
                "restricted_topic": False,
                "valid_for_seconds": 7200,
            },
        )
    ]


def test_generation_administration_rejects_client_owned_scope_and_audit_fields() -> None:
    client = _client(_Repository())
    quota = client.post(
        "/api/v1/teaching/generation-quota-grants",
        headers={"Idempotency-Key": "capacity-run-a"},
        json={"units": 200, "tenantId": "tenant-b"},
    )
    safety = client.post(
        "/api/v1/teaching/courses/course-a/classes/class-a/student-safety-assessments",
        headers={"Idempotency-Key": "capacity-run-a"},
        json={
            "mode": "micro",
            "contentMode": "open_creation",
            "webSearchRequested": False,
            "generallySafe": True,
            "minorSafe": True,
            "restrictedTopic": False,
            "validForSeconds": 7200,
            "reviewedBy": "attacker",
            "assessmentVersion": 99,
            "reviewedAt": "2026-08-27T00:00:00Z",
        },
    )
    missing_key = client.post(
        "/api/v1/teaching/generation-quota-grants",
        json={"units": 200},
    )

    assert [quota.status_code, safety.status_code, missing_key.status_code] == [422, 422, 422]


def test_student_safety_route_rejects_invalid_catalog_ids_before_repository() -> None:
    repository = _Repository()

    response = _client(repository).post(
        "/api/v1/teaching/courses/course@bad/classes/class-a/student-safety-assessments",
        headers={"Idempotency-Key": "capacity-run-a"},
        json={
            "mode": "micro",
            "contentMode": "open_creation",
            "webSearchRequested": False,
            "generallySafe": True,
            "minorSafe": True,
            "restrictedTopic": False,
            "validForSeconds": 7200,
        },
    )

    assert response.status_code == 422
    assert repository.calls == []


def test_generation_administration_routes_require_platform_admin_and_tenant_context() -> None:
    routes = {
        route.path: route for route in teaching_catalog.router.routes if isinstance(route, APIRoute)
    }
    paths = (
        "/generation-quota-grants",
        "/courses/{course_id}/classes/{class_id}/student-safety-assessments",
    )

    for path in paths:
        dependencies = {item.call for item in routes[path].dependant.dependencies}
        assert require_platform_admin in dependencies
        assert require_tenant in dependencies
        assert teaching_catalog.get_student_generation_administration_repository in dependencies
        assert inspect.iscoroutinefunction(routes[path].endpoint)


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return None


class _Session:
    def __init__(self, *, scalar_values, scalar_collections=()) -> None:
        self._scalar_values = iter(scalar_values)
        self._scalar_collections = iter(scalar_collections)
        self.added: list[object] = []
        self.executed: list[tuple[object, object | None]] = []
        self.flushed = 0

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement, parameters=None):
        self.executed.append((statement, parameters))
        return None

    async def scalar(self, statement):
        self.executed.append((statement, None))
        return next(self._scalar_values)

    async def scalars(self, statement):
        self.executed.append((statement, None))
        return next(self._scalar_collections)

    def add_all(self, values) -> None:
        self.added.extend(values)

    async def flush(self) -> None:
        self.flushed += 1


def _repository(session: _Session) -> SqlAlchemyStudentGenerationAdministrationRepository:
    @asynccontextmanager
    async def session_factory():
        yield session

    return SqlAlchemyStudentGenerationAdministrationRepository(
        "tenant-a",
        session_factory=session_factory,
    )


@pytest.mark.asyncio
async def test_quota_grant_is_append_only_audited_and_replay_safe() -> None:
    session = _Session(scalar_values=("tenant-a", None, 200))
    repository = _repository(session)

    created = await repository.grant_quota(
        actor_id="platform-admin-a",
        idempotency_key="capacity-run-a",
        units=200,
    )

    grant = next(item for item in session.added if isinstance(item, QuotaLedger))
    audit = next(item for item in session.added if isinstance(item, AuditLog))
    assert created.created is True
    assert created.grant_id == grant.id
    assert (grant.tenant_id, grant.job_id, grant.entry_type, grant.units) == (
        "tenant-a",
        None,
        "grant",
        200,
    )
    assert (
        audit.tenant_id,
        audit.actor_id,
        audit.action,
        audit.resource_type,
        audit.resource_id,
    ) == (
        "tenant-a",
        "platform-admin-a",
        "teaching.generation_quota.granted",
        "quota_grant",
        grant.id,
    )
    assert session.flushed == 1

    replay_session = _Session(scalar_values=("tenant-a", grant, 137))
    replay = await _repository(replay_session).grant_quota(
        actor_id="platform-admin-b",
        idempotency_key="capacity-run-a",
        units=200,
    )
    assert replay.created is False
    assert replay.balance == 137
    assert replay_session.added == []
    assert replay_session.flushed == 0


@pytest.mark.asyncio
async def test_new_safety_assessment_supersedes_prior_current_evidence_and_audits() -> None:
    prior = StudentSafetyAssessmentRecord(
        id="safety-prior",
        tenant_id="tenant-a",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
        generally_safe=True,
        minor_safe=True,
        restricted_topic=False,
        reviewed_by="platform-admin-old",
        reviewed_at=NOW - timedelta(hours=1),
        assessment_version=4,
        valid_for_seconds=7200,
        requested_expires_at=NOW + timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
    )
    session = _Session(
        scalar_values=("tenant-a", None, "class-a", NOW, 4),
        scalar_collections=((prior,),),
    )
    repository = _repository(session)

    created = await repository.create_safety_assessment(
        actor_id="platform-admin-a",
        idempotency_key="capacity-run-a-safety",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
        generally_safe=True,
        minor_safe=True,
        restricted_topic=False,
        valid_for_seconds=7200,
    )

    assessment = next(
        item for item in session.added if isinstance(item, StudentSafetyAssessmentRecord)
    )
    audit = next(item for item in session.added if isinstance(item, AuditLog))
    assert prior.expires_at == NOW
    assert created.created is True
    assert created.assessment_id == assessment.id
    assert (assessment.assessment_version, assessment.reviewed_at, assessment.expires_at) == (
        5,
        NOW,
        NOW + timedelta(seconds=7200),
    )
    assert assessment.requested_expires_at == NOW + timedelta(seconds=7200)
    assert assessment.reviewed_by == "platform-admin-a"
    assert (
        audit.action,
        audit.resource_type,
        audit.resource_id,
    ) == (
        "teaching.student_safety.assessed",
        "student_safety_assessment",
        assessment.id,
    )
    advisory_parameters = [
        parameters
        for statement, parameters in session.executed
        if "pg_advisory_xact_lock" in str(statement)
    ]
    assert len(advisory_parameters) == 2
    assert [item["lock_key"] for item in advisory_parameters] == sorted(
        item["lock_key"] for item in advisory_parameters
    )
    assert all(re.fullmatch(r"[0-9a-f]{64}", item["lock_key"]) for item in advisory_parameters)
    assert session.flushed == 1


@pytest.mark.asyncio
async def test_safety_assessment_replay_is_immutable_and_conflicting_input_is_rejected() -> None:
    existing = StudentSafetyAssessmentRecord(
        id="safety-placeholder",
        tenant_id="tenant-a",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
        generally_safe=True,
        minor_safe=True,
        restricted_topic=False,
        reviewed_by="platform-admin-a",
        reviewed_at=NOW,
        assessment_version=1,
        valid_for_seconds=7200,
        requested_expires_at=NOW + timedelta(seconds=7200),
        expires_at=NOW + timedelta(seconds=7200),
    )
    repository = _repository(_Session(scalar_values=("tenant-a", existing)))
    expected_id = repository._assessment_id("capacity-run-a-safety")
    existing.id = expected_id

    replay = await repository.create_safety_assessment(
        actor_id="platform-admin-b",
        idempotency_key="capacity-run-a-safety",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
        generally_safe=True,
        minor_safe=True,
        restricted_topic=False,
        valid_for_seconds=7200,
    )

    assert replay.created is False
    assert replay.reviewed_by == "platform-admin-a"

    conflict_repository = _repository(_Session(scalar_values=("tenant-a", existing)))
    with pytest.raises(StudentGenerationAdministrationConflict):
        await conflict_repository.create_safety_assessment(
            actor_id="platform-admin-b",
            idempotency_key="capacity-run-a-safety",
            course_id="course-a",
            class_id="class-a",
            mode="micro",
            content_mode="open_creation",
            web_search_requested=False,
            generally_safe=False,
            minor_safe=True,
            restricted_topic=False,
            valid_for_seconds=7200,
        )


@pytest.mark.asyncio
async def test_superseded_safety_assessment_still_replays_from_immutable_request_duration() -> None:
    existing = StudentSafetyAssessmentRecord(
        id="safety-placeholder",
        tenant_id="tenant-a",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
        generally_safe=True,
        minor_safe=True,
        restricted_topic=False,
        reviewed_by="platform-admin-a",
        reviewed_at=NOW - timedelta(hours=2),
        assessment_version=1,
        valid_for_seconds=7200,
        requested_expires_at=NOW,
        expires_at=NOW - timedelta(hours=1),
    )
    repository = _repository(_Session(scalar_values=("tenant-a", existing)))
    existing.id = repository._assessment_id("capacity-run-a-safety")

    replay = await repository.create_safety_assessment(
        actor_id="platform-admin-b",
        idempotency_key="capacity-run-a-safety",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
        generally_safe=True,
        minor_safe=True,
        restricted_topic=False,
        valid_for_seconds=7200,
    )

    assert replay.created is False
    assert replay.reviewed_by == "platform-admin-a"
    assert replay.expires_at == NOW
