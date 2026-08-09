"""Single orchestration boundary for student classroom policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from deeptutor.teaching.policies.student_generation import (
    CourseGenerationPolicy,
    PolicyDecision,
    StudentGenerationEstimate,
    StudentGenerationEvaluationContext,
    StudentGenerationQuota,
    StudentGenerationRequest,
)


@dataclass(frozen=True, slots=True)
class StudentGenerationInputs:
    policy: CourseGenerationPolicy
    context: StudentGenerationEvaluationContext
    quota: StudentGenerationQuota


@dataclass(frozen=True, slots=True)
class StudentGenerationResult:
    estimate: StudentGenerationEstimate
    decision: PolicyDecision
    request_id: str
    approval_id: str | None
    generation_job_id: None = None


class StudentGenerationRepository(Protocol):
    async def evaluate_and_record(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentGenerationResult: ...


class StudentGenerationService:
    """Delegate one request to the authoritative transactional boundary."""

    def __init__(
        self,
        *,
        tenant_id: str,
        learner_id: str,
        repository: StudentGenerationRepository,
    ) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        if not learner_id or len(learner_id) > 128:
            raise ValueError("learner_id is invalid")
        self._tenant_id = tenant_id
        self._learner_id = learner_id
        self._repository = repository

    async def evaluate(
        self,
        request: StudentGenerationRequest,
    ) -> StudentGenerationResult:
        return await self._repository.evaluate_and_record(
            self._tenant_id,
            self._learner_id,
            request,
        )
