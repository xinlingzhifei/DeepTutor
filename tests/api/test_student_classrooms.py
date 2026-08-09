from __future__ import annotations

from dataclasses import replace
import importlib
import importlib.util
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.policies.student_generation import (
    PolicyDecision,
    StudentGenerationEstimate,
)
from deeptutor.teaching.services.classrooms import ClassroomRecord, ClassroomService
from deeptutor.teaching.services.student_classrooms import StudentClassroomNotFound
from deeptutor.teaching.services.student_generation import (
    StudentGenerationRequestDetails,
    StudentGenerationResult,
)
from deeptutor.teaching.source_snapshots import SourceAccessDenied
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


def _context(user_id: str) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant-a",
        user_id=user_id,
        permissions=permissions_for_roles(
            {"student"},
            scope_type="class",
            scope_id="class-a",
            tenant_id="tenant-a",
        ),
    )


def _request() -> dict[str, object]:
    return {
        "courseId": "course-a",
        "classId": "class-a",
        "mode": "micro",
        "contentMode": "open_creation",
        "webSearchRequested": False,
    }


class _StudentClassroomService:
    def __init__(self, *, approval_required: bool = False) -> None:
        self.approval_required = approval_required
        self.create_calls = 0
        self.generation_jobs: list[str] = []
        self.assets: dict[str, dict[str, object]] = {}

    async def create(self, context: TenantContext, request):
        self.create_calls += 1
        asset_id = f"student-asset-{self.create_calls}"
        if self.approval_required:
            record = {
                "asset_id": asset_id,
                "request_id": "student-request-pending",
                "approval_id": "student-approval-pending",
                "generation_job_id": None,
                "status": "awaiting_approval",
                "course_id": request.course_id,
                "class_id": request.class_id,
                "mode": request.mode,
                "owner_id": context.user_id,
                "revision": 1,
                "outline": None,
            }
        else:
            job_id = f"student-job-{self.create_calls}"
            self.generation_jobs.append(job_id)
            record = {
                "asset_id": asset_id,
                "request_id": "student-request-accepted",
                "approval_id": None,
                "generation_job_id": job_id,
                "status": "queued",
                "course_id": request.course_id,
                "class_id": request.class_id,
                "mode": request.mode,
                "owner_id": context.user_id,
                "revision": 1,
                "outline": None,
            }
        self.assets[asset_id] = record
        return record

    async def get(self, context: TenantContext, asset_id: str):
        record = self.assets.get(asset_id)
        if record is None or record["owner_id"] != context.user_id:
            return None
        return record

    async def estimate(self, _context: TenantContext, request):
        return {
            "scene_range": (1, 5),
            "duration_minutes_range": (3, 25),
            "quota_units": 5,
            "requires_outline_confirmation": request.mode == "full",
            "requires_approval": self.approval_required,
        }

    async def list(self, context: TenantContext):
        return tuple(
            record
            for record in self.assets.values()
            if record["owner_id"] == context.user_id
        )

    async def update_outline(
        self,
        context: TenantContext,
        asset_id: str,
        outline: dict[str, object],
        expected_revision: int,
    ):
        record = await self.get(context, asset_id)
        if record is None:
            return None
        if record["revision"] != expected_revision:
            raise RuntimeError("revision conflict")
        record.update(outline=outline, revision=expected_revision + 1)
        return record

    async def confirm_outline(self, context: TenantContext, asset_id: str):
        record = await self.get(context, asset_id)
        if record is None:
            return None
        record["status"] = "queued"
        return record

    async def cancel(self, context: TenantContext, asset_id: str):
        record = await self.get(context, asset_id)
        if record is None:
            return None
        record["status"] = "canceled"
        return record


def _student_router_module():
    module_name = "deeptutor.api.routers.student_classrooms"
    assert importlib.util.find_spec(module_name) is not None, (
        "student classroom API router has not been implemented"
    )
    return importlib.import_module(module_name)


def _client(service: _StudentClassroomService, selected_user: dict[str, str]) -> TestClient:
    student_classrooms = _student_router_module()
    application = FastAPI()
    application.include_router(student_classrooms.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = lambda: _context(
        selected_user["id"]
    )
    application.dependency_overrides[
        student_classrooms.get_student_classroom_service
    ] = lambda: service
    return TestClient(application)


def test_student_asset_is_private_to_its_owner() -> None:
    service = _StudentClassroomService()
    selected_user = {"id": "alice"}
    client = _client(service, selected_user)

    created = client.post("/api/v1/student-classrooms", json=_request())
    assert created.status_code == 202
    asset_id = created.json()["assetId"]

    selected_user["id"] = "bob"
    hidden = client.get(f"/api/v1/student-classrooms/{asset_id}")

    assert hidden.status_code == 404
    assert asset_id not in hidden.text


def test_over_quota_request_waits_for_approval_without_creating_a_job() -> None:
    service = _StudentClassroomService(approval_required=True)
    client = _client(service, {"id": "alice"})

    response = client.post("/api/v1/student-classrooms", json=_request())

    assert response.status_code == 202
    assert response.json()["status"] == "awaiting_approval"
    assert response.json()["generationJobId"] is None
    assert service.generation_jobs == []


def test_student_create_rejects_client_selected_trusted_fields() -> None:
    service = _StudentClassroomService()
    body = _request()
    body.update(
        userId="bob",
        tenantId="tenant-other",
        objectKey="tenant-other/private/input.json",
        providerProfileId="provider-client-selected",
    )

    response = _client(service, {"id": "alice"}).post(
        "/api/v1/student-classrooms",
        json=body,
    )

    assert response.status_code == 422
    assert service.create_calls == 0


def test_student_source_selection_uses_logical_source_ids_only() -> None:
    service = _StudentClassroomService()
    source_grounded = _request()
    source_grounded.update(
        contentMode="source_grounded",
        sourceType="knowledge_base",
        sourceRef="kb-course-a",
    )

    accepted = _client(service, {"id": "alice"}).post(
        "/api/v1/student-classrooms/estimate",
        json=source_grounded,
    )
    missing_source = dict(source_grounded)
    missing_source.pop("sourceRef")
    rejected = _client(service, {"id": "alice"}).post(
        "/api/v1/student-classrooms/estimate",
        json=missing_source,
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 422


def test_student_not_found_errors_hide_asset_identifiers() -> None:
    class MissingService(_StudentClassroomService):
        async def get(self, _context: TenantContext, asset_id: str):
            raise StudentClassroomNotFound(f"private:{asset_id}")

    asset_id = "student-asset-private"
    response = _client(MissingService(), {"id": "bob"}).get(
        f"/api/v1/student-classrooms/{asset_id}"
    )

    assert response.status_code == 404
    assert asset_id not in response.text


def test_unauthorized_source_is_hidden_as_not_found() -> None:
    class HiddenSourceService(_StudentClassroomService):
        async def create(self, _context: TenantContext, request):
            raise SourceAccessDenied(f"private:{request.source_ref}")

    body = _request()
    body.update(
        contentMode="source_grounded",
        sourceType="knowledge_base",
        sourceRef="kb-private",
    )
    response = _client(HiddenSourceService(), {"id": "alice"}).post(
        "/api/v1/student-classrooms",
        json=body,
    )

    assert response.status_code == 404
    assert "kb-private" not in response.text


def test_student_estimate_and_owner_list_use_the_trusted_context() -> None:
    service = _StudentClassroomService()
    selected_user = {"id": "alice"}
    client = _client(service, selected_user)
    alice = client.post("/api/v1/student-classrooms", json=_request())
    selected_user["id"] = "bob"
    client.post("/api/v1/student-classrooms", json=_request())
    selected_user["id"] = "alice"

    estimate = client.post("/api/v1/student-classrooms/estimate", json=_request())
    listed = client.get("/api/v1/student-classrooms")

    assert estimate.status_code == 200
    assert estimate.json() == {
        "sceneRange": [1, 5],
        "durationMinutesRange": [3, 25],
        "quotaUnits": 5,
        "requiresOutlineConfirmation": False,
        "requiresApproval": False,
    }
    assert listed.status_code == 200
    assert [item["assetId"] for item in listed.json()["items"]] == [
        alice.json()["assetId"]
    ]


def test_student_can_edit_confirm_and_cancel_only_their_outline() -> None:
    service = _StudentClassroomService()
    client = _client(service, {"id": "alice"})
    created = client.post("/api/v1/student-classrooms", json=_request()).json()
    asset_id = created["assetId"]

    updated = client.put(
        f"/api/v1/student-classrooms/{asset_id}/outline",
        headers={"If-Match": '\"revision-1\"'},
        json={"outline": {"title": "Alice outline"}},
    )
    confirmed = client.post(
        f"/api/v1/student-classrooms/{asset_id}/confirm-outline"
    )
    canceled = client.post(f"/api/v1/student-classrooms/{asset_id}/cancel")

    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert confirmed.status_code == 202
    assert confirmed.json()["status"] == "queued"
    assert canceled.status_code == 202
    assert canceled.json()["status"] == "canceled"


async def _build_real_student_service(*, outcome: str):
    module_name = "deeptutor.teaching.services.student_classrooms"
    assert importlib.util.find_spec(module_name) is not None, (
        "student classroom orchestration service has not been implemented"
    )
    service_module = importlib.import_module(module_name)
    events: list[str] = []

    class PolicyService:
        async def evaluate(self, _request):
            events.append("policy")
            return StudentGenerationResult(
                estimate=StudentGenerationEstimate(
                    scene_range=(1, 5),
                    duration_minutes_range=(3, 25),
                    quota_units=5,
                    requires_outline_confirmation=False,
                    requires_approval=outcome == "approval_required",
                ),
                decision=PolicyDecision(
                    outcome=outcome,  # type: ignore[arg-type]
                    reason="quota_exceeded" if outcome == "approval_required" else "accepted",
                    estimated_units=5,
                    evaluated_checks=("quota", "approval"),
                ),
                request_id="request-1",
                approval_id=("approval-1" if outcome == "approval_required" else None),
            )

    class Workflow:
        async def create(self, context, request, result):
            events.append("asset")
            return {
                "asset_id": "student-asset-1",
                "request_id": result.request_id,
                "approval_id": result.approval_id,
                "generation_job_id": None,
                "status": (
                    "awaiting_approval"
                    if result.decision.outcome == "approval_required"
                    else "preparing"
                ),
                "course_id": request.course_id,
                "class_id": request.class_id,
                "mode": request.mode,
                "owner_id": context.user_id,
                "revision": 1,
                "outline": None,
            }

        async def start_generation(self, _context, record, _estimate):
            events.append("job")
            return {**record, "generation_job_id": "student-job-1", "status": "queued"}

    service = service_module.StudentClassroomService(
        policy_service=PolicyService(),
        workflow=Workflow(),
        approval_service=SimpleNamespace(),
    )
    return service, events


@pytest.mark.asyncio
async def test_real_service_does_not_enter_job_kernel_while_awaiting_approval() -> None:
    service, events = await _build_real_student_service(outcome="approval_required")
    request = SimpleNamespace(
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
    )

    result = await service.create(_context("alice"), request)

    assert result["status"] == "awaiting_approval"
    assert result["generation_job_id"] is None
    assert events == ["policy", "asset"]


@pytest.mark.asyncio
async def test_real_service_starts_job_only_after_authoritative_acceptance() -> None:
    service, events = await _build_real_student_service(outcome="accepted")
    request = SimpleNamespace(
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
    )

    result = await service.create(_context("alice"), request)

    assert result["generation_job_id"] == "student-job-1"
    assert events == ["policy", "asset", "job"]


@pytest.mark.asyncio
async def test_policy_request_is_canceled_if_asset_creation_fails() -> None:
    service_module = importlib.import_module(
        "deeptutor.teaching.services.student_classrooms"
    )
    events: list[str] = []

    class PolicyService:
        async def evaluate(self, _request):
            events.append("policy")
            return StudentGenerationResult(
                estimate=StudentGenerationEstimate(
                    scene_range=(1, 5),
                    duration_minutes_range=(3, 25),
                    quota_units=5,
                    requires_outline_confirmation=False,
                    requires_approval=False,
                ),
                decision=PolicyDecision(
                    outcome="accepted",
                    reason="accepted",
                    estimated_units=5,
                    evaluated_checks=("quota",),
                ),
                request_id="request-1",
                approval_id=None,
            )

        async def cancel(self, request_id: str):
            assert request_id == "request-1"
            events.append("policy-canceled")

    class Workflow:
        async def create(self, _context, _request, _result):
            events.append("asset-failed")
            raise RuntimeError("asset unavailable")

    service = service_module.StudentClassroomService(
        policy_service=PolicyService(),
        workflow=Workflow(),
        approval_service=SimpleNamespace(),
    )
    request = SimpleNamespace(
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
    )

    with pytest.raises(RuntimeError, match="asset unavailable"):
        await service.create(_context("alice"), request)

    assert events == ["policy", "asset-failed", "policy-canceled"]


def test_teacher_classroom_service_cannot_open_student_owned_asset() -> None:
    assert "student_generation_request_id" in ClassroomRecord.__dataclass_fields__, (
        "classroom records do not carry the durable student-asset marker"
    )
    record = ClassroomRecord(
        tenant_id="tenant-a",
        asset_id="student-asset-1",
        draft_id="student-draft-1",
        job_id=None,
        lifecycle_state="draft",
        status="awaiting_approval",
        title="Student classroom",
        course_id="course-a",
        class_id="class-a",
        owner_id="alice",
        teaching_brief=None,
        revision=1,
        outline=None,
        document={},
        classroom_version_id=None,
        confirmed_outline_sha256=None,
        validation_report=None,
        student_generation_request_id="request-1",
    )
    teacher = _context("teacher-a")
    teacher = TenantContext(
        tenant_id=teacher.tenant_id,
        schema_name=teacher.schema_name,
        user_id=teacher.user_id,
        permissions=permissions_for_roles(
            {"teacher"},
            scope_type="class",
            scope_id="class-a",
            tenant_id="tenant-a",
        ),
    )
    teacher_service = ClassroomService(None, None, None, None)  # type: ignore[arg-type]
    student_service = ClassroomService(  # type: ignore[arg-type]
        None,
        None,
        None,
        None,
        student_owner_only=True,
    )

    assert teacher_service._can_edit(teacher, record) is False
    assert student_service._can_edit(_context("alice"), record) is True
    assert student_service._can_edit(_context("bob"), record) is False


@pytest.mark.asyncio
async def test_sql_workflow_persists_student_marker_before_any_job() -> None:
    service_module = importlib.import_module(
        "deeptutor.teaching.services.student_classrooms"
    )
    workflow_type = getattr(
        service_module,
        "SqlAlchemyStudentClassroomWorkflow",
        None,
    )
    assert workflow_type is not None, "SQL student classroom workflow is missing"

    class Repository:
        def __init__(self) -> None:
            self.created = None

        async def create_workflow(self, workflow):
            self.created = workflow
            return ClassroomRecord(
                tenant_id=workflow.tenant_id,
                asset_id=workflow.asset_id,
                draft_id=workflow.draft_id,
                job_id=None,
                lifecycle_state=workflow.initial_lifecycle_state,
                status=workflow.initial_lifecycle_state,
                title=workflow.title,
                course_id=workflow.teaching_brief.course_id,
                class_id=workflow.teaching_brief.target_class_id,
                owner_id=workflow.owner_id,
                teaching_brief=workflow.teaching_brief,
                revision=1,
                outline=None,
                document={},
                classroom_version_id=None,
                confirmed_outline_sha256=None,
                validation_report=None,
                creation_idempotency_key=workflow.creation_idempotency_key,
                creation_request_sha256=workflow.creation_request_sha256,
                student_generation_request_id=workflow.student_generation_request_id,
            )

    class BriefBuilder:
        def open_creation(self, spec):
            return SimpleNamespace(
                contract=SimpleNamespace(
                    course_id=spec.course_id,
                    target_class_id=spec.class_id,
                )
            )

    repository = Repository()
    workflow = workflow_type(
        repository=repository,
        classroom_service=SimpleNamespace(),
        brief_builder=BriefBuilder(),
        generation=SimpleNamespace(),
        request_repository=SimpleNamespace(),
    )
    result = StudentGenerationResult(
        estimate=StudentGenerationEstimate(
            scene_range=(1, 5),
            duration_minutes_range=(3, 25),
            quota_units=5,
            requires_outline_confirmation=False,
            requires_approval=True,
        ),
        decision=PolicyDecision(
            outcome="approval_required",
            reason="quota_exceeded",
            estimated_units=5,
            evaluated_checks=("quota", "approval"),
        ),
        request_id="request-1",
        approval_id="approval-1",
    )
    request = SimpleNamespace(
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
    )

    record = await workflow.create(_context("alice"), request, result)

    assert repository.created.student_generation_request_id == "request-1"
    assert repository.created.initial_lifecycle_state == "draft"
    assert record.generation_job_id is None
    assert record.status == "awaiting_approval"


@pytest.mark.asyncio
async def test_cancel_pending_approval_expires_policy_before_closing_asset() -> None:
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
        teaching_brief=None,
        revision=1,
        outline=None,
        document={},
        classroom_version_id=None,
        confirmed_outline_sha256=None,
        validation_report=None,
        student_generation_request_id="request-1",
    )
    details = StudentGenerationRequestDetails(
        request_id="request-1",
        learner_id="alice",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        decision_outcome="approval_required",
        decision_reason="quota_exceeded",
        quota_state="none",
        scene_range=(1, 5),
        duration_minutes_range=(3, 25),
        estimated_units=5,
        requires_outline_confirmation=False,
        approval_id="approval-1",
        approval_status="pending",
    )

    class ClassroomRepository:
        async def mark_canceled(self, _asset_id: str):
            events.append("asset-canceled")
            return replace(record, lifecycle_state="canceled", status="canceled")

    class ClassroomLookup:
        async def get(self, _context, _asset_id: str):
            return record

    class RequestRepository:
        async def get_request_details(self, _tenant_id: str, _request_id: str):
            return details

        async def cancel_request(
            self,
            _tenant_id: str,
            learner_id: str,
            _request_id: str,
        ):
            assert learner_id == "alice"
            events.append("approval-expired")

    workflow = service_module.SqlAlchemyStudentClassroomWorkflow(
        repository=ClassroomRepository(),
        classroom_service=ClassroomLookup(),
        brief_builder=SimpleNamespace(),
        generation=SimpleNamespace(),
        request_repository=RequestRepository(),
    )

    canceled = await workflow.cancel(_context("alice"), record.asset_id)

    assert canceled.status == "canceled"
    assert events == ["approval-expired", "asset-canceled"]
    assert workflow._status(
        replace(record, lifecycle_state="canceled", status="canceled"),
        replace(details, approval_status="expired"),
    ) == "canceled"


@pytest.mark.asyncio
async def test_job_attach_failure_cancels_orphan_job_policy_and_asset() -> None:
    service_module = importlib.import_module(
        "deeptutor.teaching.services.student_classrooms"
    )
    events: list[str] = []
    record = ClassroomRecord(
        tenant_id="tenant-a",
        asset_id="student-asset-1",
        draft_id="student-draft-1",
        job_id=None,
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

    class Repository:
        async def get_workflow(self, _asset_id: str):
            return record

        async def attach_generation_job(self, *_args):
            events.append("attach-failed")
            raise RuntimeError("attach unavailable")

        async def mark_canceled(self, _asset_id: str):
            events.append("asset-canceled")
            return replace(record, lifecycle_state="canceled", status="canceled")

    class Generation:
        async def start(self, **_kwargs):
            events.append("job-created")
            return SimpleNamespace(job_id="student-job-1", status="queued")

        async def request_cancel(self, _tenant_id: str, _job_id: str):
            events.append("job-canceled")

    class RequestRepository:
        async def cancel_request(self, *_args):
            events.append("policy-canceled")

    workflow = service_module.SqlAlchemyStudentClassroomWorkflow(
        repository=Repository(),
        classroom_service=SimpleNamespace(),
        brief_builder=SimpleNamespace(),
        generation=Generation(),
        request_repository=RequestRepository(),
    )
    view = service_module.StudentClassroomView(
        asset_id=record.asset_id,
        request_id="request-1",
        approval_id=None,
        generation_job_id=None,
        status="preparing",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        owner_id="alice",
        revision=1,
        outline=None,
    )

    with pytest.raises(RuntimeError, match="attach unavailable"):
        await workflow.start_generation(
            _context("alice"),
            view,
            SimpleNamespace(),
        )

    assert events == [
        "job-created",
        "attach-failed",
        "job-canceled",
        "policy-canceled",
        "asset-canceled",
    ]


@pytest.mark.asyncio
async def test_cancel_is_idempotent_after_job_and_asset_are_terminal() -> None:
    service_module = importlib.import_module(
        "deeptutor.teaching.services.student_classrooms"
    )
    record = ClassroomRecord(
        tenant_id="tenant-a",
        asset_id="student-asset-1",
        draft_id="student-draft-1",
        job_id="student-job-1",
        lifecycle_state="canceled",
        status="canceled",
        title="Student classroom",
        course_id="course-a",
        class_id="class-a",
        owner_id="alice",
        teaching_brief=None,
        revision=1,
        outline=None,
        document={},
        classroom_version_id=None,
        confirmed_outline_sha256=None,
        validation_report=None,
        student_generation_request_id="request-1",
    )
    details = StudentGenerationRequestDetails(
        request_id="request-1",
        learner_id="alice",
        course_id="course-a",
        class_id="class-a",
        mode="micro",
        decision_outcome="accepted",
        decision_reason="accepted",
        quota_state="released",
        scene_range=(1, 5),
        duration_minutes_range=(3, 25),
        estimated_units=5,
        requires_outline_confirmation=False,
        approval_id=None,
        approval_status=None,
    )

    class ClassroomLookup:
        async def get(self, _context, _asset_id: str):
            return record

    class RequestRepository:
        async def get_request_details(self, *_args):
            return details

    class Generation:
        async def request_cancel(self, *_args):
            raise AssertionError("terminal job must not be canceled again")

    workflow = service_module.SqlAlchemyStudentClassroomWorkflow(
        repository=SimpleNamespace(),
        classroom_service=ClassroomLookup(),
        brief_builder=SimpleNamespace(),
        generation=Generation(),
        request_repository=RequestRepository(),
    )

    result = await workflow.cancel(_context("alice"), record.asset_id)

    assert result.status == "canceled"


def test_student_classroom_routes_are_registered_only_when_platform_is_enabled() -> None:
    from deeptutor.api import main

    registration = getattr(main, "_register_student_classroom_routes", None)
    assert registration is not None, "student classroom router registration is missing"
    disabled = FastAPI()
    assert registration(disabled, enabled=False, dependencies=[]) is False
    assert all("student-classrooms" not in route.path for route in disabled.routes)

    enabled = FastAPI()
    assert registration(enabled, enabled=True, dependencies=[]) is True
    paths = {route.path for route in enabled.routes}
    assert "/api/v1/student-classrooms" in paths
    assert "/api/v1/student-generation-approvals" in paths


def test_real_application_registers_student_routes_when_platform_is_enabled(
    monkeypatch,
) -> None:
    from deeptutor.api import main
    from deeptutor.services import config as runtime_config

    original = runtime_config.load_platform_settings
    monkeypatch.setattr(
        runtime_config,
        "load_platform_settings",
        lambda: SimpleNamespace(enabled=True),
    )
    try:
        enabled_main = importlib.reload(main)
        paths = {route.path for route in enabled_main.app.routes}
        assert "/api/v1/student-classrooms" in paths
        assert "/api/v1/student-generation-approvals" in paths
    finally:
        monkeypatch.setattr(runtime_config, "load_platform_settings", original)
        importlib.reload(main)
