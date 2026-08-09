from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import importlib
import importlib.util
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from deeptutor.api.routers import student_classrooms
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.repositories.classrooms import SqlAlchemyClassroomRepository
from deeptutor.teaching.services.classrooms import ClassroomRecord
from deeptutor.teaching.services.student_generation import (
    StudentGenerationApprovalDetails,
    StudentGenerationApprovalNotFound,
    StudentGenerationRequestDetails,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


def _teacher_context(user_id: str = "teacher-a") -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant-a",
        user_id=user_id,
        permissions=permissions_for_roles(
            {"content_reviewer", "teacher"},
            scope_type="class",
            scope_id="class-a",
            tenant_id="tenant-a",
        ),
    )


class _ApprovalService:
    def __init__(self) -> None:
        self.policy_rechecks = 0
        self.quota_reservations = 0
        self.source = {
            "asset_id": "student-asset-1",
            "draft_id": "student-draft-1",
            "owner_id": "alice",
            "revision": 4,
            "document": {"title": "Student version"},
        }
        self.approvals = {
            "approval-1": {
                "approval_id": "approval-1",
                "request_id": "request-1",
                "asset_id": "student-asset-1",
                "learner_id": "alice",
                "course_id": "course-a",
                "class_id": "class-a",
                "reason": "quota_exceeded",
                "status": "pending",
                "decided_by": None,
                "generation_job_id": None,
            }
        }

    async def list_approvals(self, _context: TenantContext):
        return tuple(self.approvals.values())

    async def approve(self, context: TenantContext, approval_id: str, _comment: str | None):
        record = self.approvals[approval_id]
        self.policy_rechecks += 1
        self.quota_reservations += 1
        record.update(
            status="approved",
            decided_by=context.user_id,
            generation_job_id="student-job-approved",
        )
        return record

    async def reject(self, context: TenantContext, approval_id: str, _comment: str | None):
        record = self.approvals[approval_id]
        record.update(
            status="rejected",
            decided_by=context.user_id,
            generation_job_id=None,
        )
        return record

    async def copy_to_teacher_draft(self, context: TenantContext, asset_id: str):
        assert asset_id == self.source["asset_id"]
        return {
            "asset_id": "teacher-asset-copy-1",
            "draft_id": "teacher-draft-copy-1",
            "source_student_asset_id": asset_id,
            "owner_id": context.user_id,
            "status": "editing",
            "revision": 1,
        }


def _client(service: _ApprovalService) -> TestClient:
    application = FastAPI()
    application.include_router(student_classrooms.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = _teacher_context
    application.dependency_overrides[
        student_classrooms.get_student_classroom_service
    ] = lambda: service
    return TestClient(application)


def test_teacher_approval_rechecks_policy_and_reserves_quota_before_job() -> None:
    service = _ApprovalService()

    response = _client(service).post(
        "/api/v1/student-generation-approvals/approval-1/approve",
        json={"comment": "Approved for this lesson"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "approved"
    assert response.json()["generationJobId"] == "student-job-approved"
    assert service.policy_rechecks == 1
    assert service.quota_reservations == 1


def test_teacher_can_list_and_reject_pending_generation_approval() -> None:
    service = _ApprovalService()
    client = _client(service)

    listed = client.get("/api/v1/student-generation-approvals")
    rejected = client.post(
        "/api/v1/student-generation-approvals/approval-1/reject",
        json={"comment": "Use the assigned material"},
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["learnerId"] == "alice"
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["generationJobId"] is None


def test_out_of_scope_approval_is_hidden_as_not_found() -> None:
    class HiddenApprovalService(_ApprovalService):
        async def approve(
            self,
            _context: TenantContext,
            approval_id: str,
            _comment: str | None,
        ):
            raise StudentGenerationApprovalNotFound(f"private:{approval_id}")

    approval_id = "approval-private"
    response = _client(HiddenApprovalService()).post(
        f"/api/v1/student-generation-approvals/{approval_id}/approve",
        json={"comment": None},
    )

    assert response.status_code == 404
    assert approval_id not in response.text


def test_copy_creates_new_teacher_asset_and_preserves_student_version() -> None:
    service = _ApprovalService()
    before = deepcopy(service.source)

    response = _client(service).post(
        "/api/v1/student-classrooms/student-asset-1/copy-to-teacher-draft"
    )

    assert response.status_code == 201
    assert response.json() == {
        "assetId": "teacher-asset-copy-1",
        "draftId": "teacher-draft-copy-1",
        "sourceStudentAssetId": "student-asset-1",
        "ownerId": "teacher-a",
        "status": "editing",
        "revision": 1,
    }
    assert response.json()["assetId"] != service.source["asset_id"]
    assert response.json()["draftId"] != service.source["draft_id"]
    assert service.source == before


def test_student_asset_and_teacher_copy_have_durable_audit_links() -> None:
    models = importlib.import_module("deeptutor.teaching.models.student_generation")
    student_asset_type = getattr(models, "StudentClassroomAssetRecord", None)
    copy_type = getattr(models, "StudentClassroomCopyRecord", None)

    assert student_asset_type is not None, "student asset link model is missing"
    assert copy_type is not None, "student-to-teacher copy audit model is missing"
    student_table = student_asset_type.__table__
    copy_table = copy_type.__table__

    student_foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in student_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("asset_id", "tenant_id"),
        ("tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"),
    ) in student_foreign_keys
    assert (
        ("request_id", "tenant_id"),
        (
            "tenant.student_generation_requests.id",
            "tenant.student_generation_requests.tenant_id",
        ),
    ) in student_foreign_keys

    copy_foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in copy_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("source_asset_id", "tenant_id"),
        (
            "tenant.student_classroom_assets.asset_id",
            "tenant.student_classroom_assets.tenant_id",
        ),
    ) in copy_foreign_keys
    assert (
        ("teacher_asset_id", "tenant_id"),
        ("tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"),
    ) in copy_foreign_keys
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("teacher_asset_id", "tenant_id")
        for constraint in copy_table.constraints
    )


def test_student_safety_assessment_model_binds_exact_trusted_request_shape() -> None:
    models = importlib.import_module("deeptutor.teaching.models.student_generation")
    assessment_type = getattr(models, "StudentSafetyAssessmentRecord", None)

    assert assessment_type is not None, "durable student safety evidence is missing"
    table = assessment_type.__table__
    assert {
        "id",
        "tenant_id",
        "course_id",
        "class_id",
        "mode",
        "content_mode",
        "web_search_requested",
        "generally_safe",
        "minor_safe",
        "restricted_topic",
        "reviewed_by",
        "reviewed_at",
        "assessment_version",
        "expires_at",
    }.issubset(table.columns.keys())


def test_student_safety_assessment_migration_follows_student_classroom_api() -> None:
    module_name = (
        "deeptutor.teaching.migrations.versions."
        "20260809_0015_student_safety_assessments"
    )

    assert importlib.util.find_spec(module_name) is not None
    migration = importlib.import_module(module_name)
    assert migration.revision == "20260809_0015"
    assert migration.down_revision == "20260809_0014"


def test_student_classroom_api_migration_is_the_tenant_head() -> None:
    module_name = (
        "deeptutor.teaching.migrations.versions."
        "20260809_0014_student_classroom_api"
    )
    assert importlib.util.find_spec(module_name) is not None, (
        "student classroom API migration is missing"
    )
    migration = importlib.import_module(module_name)

    assert migration.revision == "20260809_0014"
    assert migration.down_revision == "20260809_0013"


def test_classroom_repository_exposes_atomic_student_workflow_operations() -> None:
    methods = SqlAlchemyClassroomRepository.__dict__

    assert "get_student_workflow" in methods
    assert "attach_generation_job" in methods
    assert "start_student_generation" in methods
    assert "mark_canceled" in methods
    assert "copy_student_to_teacher_draft" in methods


@pytest.mark.asyncio
async def test_replayed_approval_returns_the_already_bound_job_without_restarting() -> None:
    service_module = importlib.import_module(
        "deeptutor.teaching.services.student_classrooms"
    )
    record = ClassroomRecord(
        tenant_id="tenant-a",
        asset_id="student-asset-1",
        draft_id="student-draft-1",
        job_id="student-job-1",
        lifecycle_state="generating_content",
        status="generating_content",
        title="Student classroom",
        course_id="course-a",
        class_id="class-a",
        owner_id="alice",
        teaching_brief=SimpleNamespace(),
        revision=1,
        outline=None,
        document={},
        classroom_version_id=None,
        confirmed_outline_sha256=None,
        validation_report=None,
        student_generation_request_id="request-1",
    )
    approval = StudentGenerationApprovalDetails(
        approval_id="approval-1",
        request_id="request-1",
        learner_id="alice",
        course_id="course-a",
        class_id="class-a",
        reason="quota_exceeded",
        status="approved",
        decided_by="teacher-a",
    )
    details = StudentGenerationRequestDetails(
        request_id="request-1",
        learner_id="alice",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        decision_outcome="accepted",
        decision_reason="accepted",
        quota_state="reserved",
        scene_range=(1, 5),
        duration_minutes_range=(3, 25),
        estimated_units=5,
        requires_outline_confirmation=False,
        approval_id="approval-1",
        approval_status="approved",
    )

    class ClassroomRepository:
        async def get_student_workflow(self, _request_id: str):
            return record

        async def start_student_generation(self, *_args):
            raise AssertionError("a bound approval replay must not restart its job")

    class RequestRepository:
        async def get_request_details(self, _tenant_id: str, _request_id: str):
            return details

    class Generation:
        async def start(self, **_kwargs):
            raise AssertionError("a bound approval replay must not create a job")

    workflow = service_module.SqlAlchemyStudentClassroomWorkflow(
        repository=ClassroomRepository(),
        classroom_service=SimpleNamespace(),
        brief_builder=SimpleNamespace(),
        generation=Generation(),
        request_repository=RequestRepository(),
    )

    replayed = await workflow.start_approved_generation(
        _teacher_context(),
        approval,
    )

    assert replayed.generation_job_id == "student-job-1"
    assert replayed.status == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["asset", "job", "job_cancel"])
async def test_approved_start_failure_expires_authorization_and_cancels_asset(
    failure_phase: str,
) -> None:
    service_module = importlib.import_module(
        "deeptutor.teaching.services.student_classrooms"
    )
    events: list[str] = []
    record = ClassroomRecord(
        tenant_id="tenant-a",
        asset_id="student-asset-1",
        draft_id="student-draft-1",
        job_id=None,
        lifecycle_state="draft",
        status="draft",
        title="Student classroom",
        course_id="course-a",
        class_id="class-a",
        owner_id="alice",
        teaching_brief=SimpleNamespace(),
        revision=1,
        outline=None,
        document={},
        classroom_version_id=None,
        confirmed_outline_sha256=None,
        validation_report=None,
        student_generation_request_id="request-1",
    )
    approval = StudentGenerationApprovalDetails(
        approval_id="approval-1",
        request_id="request-1",
        learner_id="alice",
        course_id="course-a",
        class_id="class-a",
        reason="quota_exceeded",
        status="approved",
        decided_by="teacher-a",
    )
    details = StudentGenerationRequestDetails(
        request_id="request-1",
        learner_id="alice",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        decision_outcome="accepted",
        decision_reason="accepted",
        quota_state="reserved",
        scene_range=(1, 5),
        duration_minutes_range=(3, 25),
        estimated_units=5,
        requires_outline_confirmation=False,
        approval_id="approval-1",
        approval_status="approved",
    )

    class ClassroomRepository:
        async def get_student_workflow(self, _request_id: str):
            return record

        async def start_student_generation(self, _asset_id: str, _mode: str):
            events.append("asset-started")
            if failure_phase == "asset":
                raise RuntimeError("asset unavailable")
            return replace(
                record,
                lifecycle_state="generating_content",
                status="generating_content",
            )

        async def mark_canceled(self, _asset_id: str):
            events.append("asset-canceled")
            return replace(record, lifecycle_state="canceled", status="canceled")

        async def attach_generation_job(
            self,
            _asset_id: str,
            _job_id: str,
            _phase: str,
        ):
            events.append("attach-failed")
            raise RuntimeError("attach unavailable")

    class RequestRepository:
        async def get_request_details(self, _tenant_id: str, _request_id: str):
            return details

        async def abort_approved_request(
            self,
            _tenant_id: str,
            reviewer_id: str,
            approval_id: str,
        ):
            assert (reviewer_id, approval_id) == ("teacher-b", "approval-1")
            events.append("approval-expired")

    class Generation:
        async def start(self, **_kwargs):
            if failure_phase == "job":
                events.append("job-start-failed")
                raise RuntimeError("queue unavailable")
            events.append("job-started")
            return SimpleNamespace(job_id="student-job-1", status="queued")

        async def request_cancel(self, _tenant_id: str, _job_id: str):
            events.append("job-cancel-failed")
            raise RuntimeError("cancel unavailable")

    workflow = service_module.SqlAlchemyStudentClassroomWorkflow(
        repository=ClassroomRepository(),
        classroom_service=SimpleNamespace(),
        brief_builder=SimpleNamespace(),
        generation=Generation(),
        request_repository=RequestRepository(),
    )

    if failure_phase == "job_cancel":
        with pytest.raises(service_module.StudentClassroomUnavailable) as captured:
            await workflow.start_approved_generation(
                _teacher_context("teacher-b"), approval
            )
        assert str(captured.value.primary_error) == "attach unavailable"
        assert [str(error) for error in captured.value.compensation_errors] == [
            "cancel unavailable"
        ]
    else:
        with pytest.raises(RuntimeError, match="unavailable"):
            await workflow.start_approved_generation(
                _teacher_context("teacher-b"), approval
            )

    expected = ["asset-started"]
    if failure_phase == "job":
        expected.append("job-start-failed")
    elif failure_phase == "job_cancel":
        expected.extend(["job-started", "attach-failed"])
    expected.append("asset-canceled")
    if failure_phase == "job_cancel":
        expected.append("job-cancel-failed")
    expected.append("approval-expired")
    assert events == expected


@pytest.mark.asyncio
async def test_each_teacher_copy_gets_a_new_asset_and_draft_identity() -> None:
    service_module = importlib.import_module(
        "deeptutor.teaching.services.student_classrooms"
    )
    source = ClassroomRecord(
        tenant_id="tenant-a",
        asset_id="student-asset-1",
        draft_id="student-draft-1",
        job_id="student-job-1",
        lifecycle_state="editing",
        status="succeeded",
        title="Student classroom",
        course_id="course-a",
        class_id="class-a",
        owner_id="alice",
        teaching_brief=SimpleNamespace(),
        revision=4,
        outline=None,
        document={"title": "Student version"},
        classroom_version_id="version-1",
        confirmed_outline_sha256=None,
        validation_report=None,
        student_generation_request_id="request-1",
    )

    class Repository:
        def __init__(self) -> None:
            self.copies: list[tuple[str, str, str]] = []

        async def get_workflow(self, _asset_id: str):
            return source

        async def copy_student_to_teacher_draft(
            self,
            _source_asset_id: str,
            target_asset_id: str,
            target_draft_id: str,
            copy_id: str,
            _copied_by: str,
        ):
            self.copies.append((target_asset_id, target_draft_id, copy_id))
            return SimpleNamespace(asset_id=target_asset_id, draft_id=target_draft_id)

    repository = Repository()
    workflow = service_module.SqlAlchemyStudentClassroomWorkflow(
        repository=repository,
        classroom_service=SimpleNamespace(),
        brief_builder=SimpleNamespace(),
        generation=SimpleNamespace(),
        request_repository=SimpleNamespace(),
    )

    await workflow.copy_to_teacher_draft(_teacher_context(), source.asset_id)
    await workflow.copy_to_teacher_draft(_teacher_context(), source.asset_id)

    assert len(repository.copies) == 2
    assert all(first != second for first, second in zip(*repository.copies))
    assert source.revision == 4
    assert source.document == {"title": "Student version"}
