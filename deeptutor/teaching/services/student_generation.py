"""Single orchestration boundary for student classroom policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.policies.student_generation import (
    CourseGenerationPolicy,
    PolicyDecision,
    StudentGenerationEstimate,
    StudentGenerationEvaluationContext,
    StudentGenerationQuota,
    StudentGenerationRequest,
)
from deeptutor.teaching.tenant_context import TenantContext


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


@dataclass(frozen=True, slots=True)
class StudentGenerationApprovalDetails:
    approval_id: str
    request_id: str
    learner_id: str
    course_id: str
    class_id: str
    reason: str
    status: str
    decided_by: str | None


@dataclass(frozen=True, slots=True)
class StudentGenerationRequestDetails:
    request_id: str
    learner_id: str
    course_id: str
    class_id: str
    mode: str
    decision_outcome: str
    decision_reason: str
    quota_state: str
    scene_range: tuple[int, int]
    duration_minutes_range: tuple[int, int]
    estimated_units: int
    requires_outline_confirmation: bool
    approval_id: str | None
    approval_status: str | None


class StudentGenerationApprovalNotFound(LookupError):
    """The approval is absent or outside the reviewer's resource scope."""


class StudentGenerationApprovalConflict(RuntimeError):
    """The approval is no longer pending."""


class StudentGenerationRepository(Protocol):
    async def estimate(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentGenerationEstimate: ...

    async def evaluate_and_record(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentGenerationResult: ...

    async def cancel_request(
        self,
        tenant_id: str,
        learner_id: str,
        request_id: str,
    ) -> None: ...


class StudentGenerationApprovalRepository(Protocol):
    async def list_approval_details(
        self,
        tenant_id: str,
    ) -> tuple[StudentGenerationApprovalDetails, ...]: ...

    async def get_approval_details(
        self,
        tenant_id: str,
        approval_id: str,
    ) -> StudentGenerationApprovalDetails | None: ...

    async def approve_and_reserve(
        self,
        tenant_id: str,
        reviewer_id: str,
        approval_id: str,
        expected: StudentGenerationApprovalDetails,
    ) -> StudentGenerationApprovalDetails: ...

    async def reject(
        self,
        tenant_id: str,
        reviewer_id: str,
        approval_id: str,
        expected: StudentGenerationApprovalDetails,
    ) -> StudentGenerationApprovalDetails: ...


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

    async def estimate(
        self,
        request: StudentGenerationRequest,
    ) -> StudentGenerationEstimate:
        return await self._repository.estimate(
            self._tenant_id,
            self._learner_id,
            request,
        )

    async def evaluate(
        self,
        request: StudentGenerationRequest,
    ) -> StudentGenerationResult:
        return await self._repository.evaluate_and_record(
            self._tenant_id,
            self._learner_id,
            request,
        )

    async def cancel(self, request_id: str) -> None:
        await self._repository.cancel_request(
            self._tenant_id,
            self._learner_id,
            request_id,
        )


class StudentGenerationApprovalService:
    """Authorize reviewers while keeping re-evaluation inside the repository."""

    def __init__(
        self,
        *,
        tenant_id: str,
        repository: StudentGenerationApprovalRepository,
    ) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        self._tenant_id = tenant_id
        self._repository = repository

    def _can_review(
        self,
        context: TenantContext,
        approval: StudentGenerationApprovalDetails,
    ) -> bool:
        if (
            context.tenant_id != self._tenant_id
            or approval.learner_id == context.user_id
        ):
            return False
        resource = ResourceScope(
            tenant_id=self._tenant_id,
            course_id=approval.course_id,
            class_id=approval.class_id,
        )
        return any(
            permission.allows_resource("classroom.approve", resource)
            for permission in context.permissions
        )

    async def list(
        self,
        context: TenantContext,
    ) -> tuple[StudentGenerationApprovalDetails, ...]:
        approvals = await self._repository.list_approval_details(self._tenant_id)
        return tuple(
            approval for approval in approvals if self._can_review(context, approval)
        )

    async def _required(
        self,
        context: TenantContext,
        approval_id: str,
    ) -> StudentGenerationApprovalDetails:
        approval = await self._repository.get_approval_details(
            self._tenant_id,
            approval_id,
        )
        if approval is None or not self._can_review(context, approval):
            raise StudentGenerationApprovalNotFound(approval_id)
        return approval

    async def approve(
        self,
        context: TenantContext,
        approval_id: str,
    ) -> StudentGenerationApprovalDetails:
        approval = await self._required(context, approval_id)
        return await self._repository.approve_and_reserve(
            self._tenant_id,
            context.user_id,
            approval_id,
            approval,
        )

    async def reject(
        self,
        context: TenantContext,
        approval_id: str,
    ) -> StudentGenerationApprovalDetails:
        approval = await self._required(context, approval_id)
        return await self._repository.reject(
            self._tenant_id,
            context.user_id,
            approval_id,
            approval,
        )
