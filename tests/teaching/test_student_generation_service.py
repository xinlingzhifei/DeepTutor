from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.policies.student_generation import (
    CourseGenerationPolicy,
    StudentGenerationEvaluationContext,
    StudentGenerationQuota,
    StudentGenerationRequest,
    estimate_student_request,
    evaluate_student_request,
)
from deeptutor.teaching.repositories.student_generation import (
    SqlAlchemyStudentGenerationRepository,
    StudentSafetyAssessment,
)
from deeptutor.teaching.services import student_generation as student_generation_service
from deeptutor.teaching.services.student_generation import (
    StudentGenerationApprovalDetails,
    StudentGenerationApprovalNotFound,
    StudentGenerationApprovalService,
    StudentGenerationInputs,
    StudentGenerationResult,
    StudentGenerationService,
)
from deeptutor.teaching.tenant_context import TenantContext


def request(*, mode: str = "micro") -> StudentGenerationRequest:
    return StudentGenerationRequest(
        course_id="course-1",
        class_id="class-1",
        mode=mode,  # type: ignore[arg-type]
        content_mode="source_grounded",
        web_search_requested=False,
    )


def inputs(
    *,
    allow_student_full: bool = True,
    daily_used_units: int = 0,
    monthly_used_units: int = 0,
) -> StudentGenerationInputs:
    return StudentGenerationInputs(
        policy=CourseGenerationPolicy(
            allowed_content_modes=frozenset({"source_grounded"}),
            daily_student_units=20,
            monthly_student_units=200,
            allow_student_micro=True,
            allow_student_full=allow_student_full,
        ),
        context=StudentGenerationEvaluationContext(
            enrolled=True,
            has_generation_permission=True,
            source_permitted=True,
            generally_safe=True,
            minor_safe=True,
            restricted_topic=False,
            approval_granted=False,
        ),
        quota=StudentGenerationQuota(
            daily_used_units=daily_used_units,
            monthly_used_units=monthly_used_units,
        ),
    )


@pytest.mark.parametrize(
    "field",
    ["generally_safe", "minor_safe", "restricted_topic"],
)
def test_trusted_safety_assessment_requires_strict_booleans(field: str) -> None:
    values: dict[str, object] = {
        "generally_safe": True,
        "minor_safe": True,
        "restricted_topic": False,
    }
    values[field] = "false"

    with pytest.raises(ValueError, match=field):
        StudentSafetyAssessment(**values)  # type: ignore[arg-type]


def test_production_repository_exposes_only_authoritative_decision_boundary() -> None:
    assert "evaluate_and_record" in SqlAlchemyStudentGenerationRepository.__dict__
    assert "load_inputs" not in SqlAlchemyStudentGenerationRepository.__dict__
    assert "record_decision" not in SqlAlchemyStudentGenerationRepository.__dict__


def test_production_repository_owns_estimate_and_approval_recheck_boundaries() -> None:
    methods = SqlAlchemyStudentGenerationRepository.__dict__

    assert "estimate" in methods
    assert "list_approval_details" in methods
    assert "get_approval_details" in methods
    assert "approve_and_reserve" in methods
    assert "reject" in methods


def test_approval_source_recheck_is_bound_to_the_original_snapshot() -> None:
    source_statement = getattr(
        SqlAlchemyStudentGenerationRepository,
        "_source_binding_statement",
        None,
    )
    approval_source_statement = getattr(
        SqlAlchemyStudentGenerationRepository,
        "_approval_source_snapshot_statement",
        None,
    )
    assert source_statement is not None
    assert approval_source_statement is not None

    source_sql = str(
        source_statement(
            tenant_id="tenant-1",
            selected_request=request(),
            required_snapshot_id="source-snapshot-original",
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    approval_sql = str(
        approval_source_statement(
            tenant_id="tenant-1",
            request_id="request-1",
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "tenant_source_bindings.source_snapshot_id = 'source-snapshot-original'" in source_sql
    assert "student_classroom_assets.request_id = 'request-1'" in approval_sql
    assert "classroom_assets.owner_id" in approval_sql
    assert "student_classroom_assets.tenant_id = 'tenant-1'" in approval_sql
    assert "classroom_drafts" in approval_sql
    assert "classroom_drafts.teaching_brief_id" in approval_sql
    assert "teaching_briefs" in approval_sql
    assert "teaching_briefs.course_id" in approval_sql
    assert "teaching_briefs.class_id" in approval_sql
    assert "teaching_briefs.source_snapshot_id" in approval_sql


@dataclass
class FakeStudentGenerationRepository:
    loaded: StudentGenerationInputs
    calls: list[tuple[str, str, StudentGenerationRequest]] = field(default_factory=list)

    async def evaluate_and_record(
        self,
        tenant_id: str,
        learner_id: str,
        selected_request: StudentGenerationRequest,
    ) -> StudentGenerationResult:
        self.calls.append((tenant_id, learner_id, selected_request))
        estimate = estimate_student_request(
            policy=self.loaded.policy,
            request=selected_request,
            context=self.loaded.context,
            quota=self.loaded.quota,
        )
        decision = evaluate_student_request(
            policy=self.loaded.policy,
            request=selected_request,
            context=self.loaded.context,
            quota=self.loaded.quota,
        )
        approval_id = "approval-1" if decision.outcome == "approval_required" else None
        return StudentGenerationResult(
            estimate=estimate,
            decision=decision,
            request_id="request-1",
            approval_id=approval_id,
        )


@pytest.mark.asyncio
async def test_service_persists_denied_decision_at_the_single_boundary() -> None:
    repository = FakeStudentGenerationRepository(inputs(allow_student_full=False))
    service = StudentGenerationService(
        tenant_id="tenant-1",
        learner_id="student-1",
        repository=repository,
    )

    result = await service.evaluate(request(mode="full"))

    assert result.decision.outcome == "denied"
    assert result.decision.reason == "full_classroom_disabled"
    assert result.request_id == "request-1"
    assert result.approval_id is None
    assert result.generation_job_id is None
    assert len(repository.calls) == 1


@pytest.mark.asyncio
async def test_over_quota_micro_requires_approval_without_queueing_a_job() -> None:
    repository = FakeStudentGenerationRepository(
        inputs(daily_used_units=20, monthly_used_units=200)
    )
    service = StudentGenerationService(
        tenant_id="tenant-1",
        learner_id="student-1",
        repository=repository,
    )

    result = await service.evaluate(request())

    assert result.decision.outcome == "approval_required"
    assert result.decision.reason == "quota_exceeded"
    assert result.decision.estimated_units > 0
    assert result.request_id == "request-1"
    assert result.approval_id == "approval-1"
    assert result.generation_job_id is None
    assert len(repository.calls) == 1


@pytest.mark.asyncio
async def test_accepted_request_is_recorded_but_task_one_creates_no_job() -> None:
    repository = FakeStudentGenerationRepository(inputs())
    service = StudentGenerationService(
        tenant_id="tenant-1",
        learner_id="student-1",
        repository=repository,
    )

    result = await service.evaluate(request())

    assert result.decision.outcome == "accepted"
    assert result.request_id == "request-1"
    assert result.approval_id is None
    assert result.generation_job_id is None
    assert repository.calls == [("tenant-1", "student-1", request())]


@pytest.mark.asyncio
async def test_estimate_uses_authoritative_repository_without_recording_a_request() -> None:
    class EstimateRepository(FakeStudentGenerationRepository):
        estimate_calls: list[tuple[str, str, StudentGenerationRequest]] = []

        async def estimate(
            self,
            tenant_id: str,
            learner_id: str,
            selected_request: StudentGenerationRequest,
        ):
            self.estimate_calls.append((tenant_id, learner_id, selected_request))
            return estimate_student_request(
                policy=self.loaded.policy,
                request=selected_request,
                context=self.loaded.context,
                quota=self.loaded.quota,
            )

    repository = EstimateRepository(inputs())
    service = StudentGenerationService(
        tenant_id="tenant-1",
        learner_id="student-1",
        repository=repository,
    )

    assert callable(getattr(service, "estimate", None)), (
        "StudentGenerationService has no authoritative estimate boundary"
    )
    estimate = await service.estimate(request())

    assert estimate.quota_units == 5
    assert repository.estimate_calls == [("tenant-1", "student-1", request())]
    assert repository.calls == []


@pytest.mark.asyncio
async def test_teacher_approval_delegates_locked_recheck_and_reservation_to_repository() -> None:
    details_type = getattr(
        student_generation_service,
        "StudentGenerationApprovalDetails",
        None,
    )
    approval_service_type = getattr(
        student_generation_service,
        "StudentGenerationApprovalService",
        None,
    )
    assert details_type is not None, "student generation approval details are missing"
    assert approval_service_type is not None, "student generation approval service is missing"
    approval = details_type(
        approval_id="approval-1",
        request_id="request-1",
        learner_id="student-1",
        course_id="course-1",
        class_id="class-1",
        reason="quota_exceeded",
        status="pending",
        decided_by=None,
    )

    class ApprovalRepository:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, object]] = []

        async def list_approval_details(self, _tenant_id: str):
            return (approval,)

        async def get_approval_details(self, _tenant_id: str, approval_id: str):
            return approval if approval_id == approval.approval_id else None

        async def approve_and_reserve(
            self,
            tenant_id: str,
            reviewer_id: str,
            approval_id: str,
            expected,
        ):
            self.calls.append((tenant_id, reviewer_id, approval_id, expected))
            return details_type(
                approval_id=approval.approval_id,
                request_id=approval.request_id,
                learner_id=approval.learner_id,
                course_id=approval.course_id,
                class_id=approval.class_id,
                reason=approval.reason,
                status="approved",
                decided_by=reviewer_id,
            )

    context = TenantContext(
        tenant_id="tenant-1",
        schema_name="tenant_tenant-1",
        user_id="teacher-1",
        permissions=permissions_for_roles(
            {"content_reviewer"},
            scope_type="class",
            scope_id="class-1",
            tenant_id="tenant-1",
        ),
    )
    repository = ApprovalRepository()
    service = approval_service_type(
        tenant_id="tenant-1",
        repository=repository,
    )

    decided = await service.approve(context, "approval-1")

    assert decided.status == "approved"
    assert repository.calls == [
        ("tenant-1", "teacher-1", "approval-1", approval)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "roles", "scope_id"),
    [
        ("student-1", {"student"}, "class-1"),
        ("teacher-2", {"content_reviewer"}, "class-other"),
    ],
)
async def test_self_or_out_of_scope_reviewers_cannot_observe_or_mutate_approval(
    user_id: str,
    roles: set[str],
    scope_id: str,
) -> None:
    approval = StudentGenerationApprovalDetails(
        approval_id="approval-1",
        request_id="request-1",
        learner_id="student-1",
        course_id="course-1",
        class_id="class-1",
        reason="quota_exceeded",
        status="pending",
        decided_by=None,
    )

    class Repository:
        mutation_calls = 0

        async def list_approval_details(self, _tenant_id: str):
            return (approval,)

        async def get_approval_details(self, _tenant_id: str, _approval_id: str):
            return approval

        async def approve_and_reserve(self, *_args):
            self.mutation_calls += 1
            raise AssertionError("unauthorized approval must not mutate")

    repository = Repository()
    service = StudentGenerationApprovalService(
        tenant_id="tenant-1",
        repository=repository,
    )
    context = TenantContext(
        tenant_id="tenant-1",
        schema_name="tenant_tenant-1",
        user_id=user_id,
        permissions=permissions_for_roles(
            roles,
            scope_type="class",
            scope_id=scope_id,
            tenant_id="tenant-1",
        ),
    )

    assert await service.list(context) == ()
    with pytest.raises(StudentGenerationApprovalNotFound):
        await service.approve(context, approval.approval_id)
    assert repository.mutation_calls == 0
