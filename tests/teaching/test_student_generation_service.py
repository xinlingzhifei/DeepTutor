from __future__ import annotations

from dataclasses import dataclass, field

import pytest

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
from deeptutor.teaching.services.student_generation import (
    StudentGenerationInputs,
    StudentGenerationResult,
    StudentGenerationService,
)


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
