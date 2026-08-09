"""Tenant-bound persistence for authoritative student generation decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
import uuid

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.models.classrooms import TenantSourceBinding
from deeptutor.teaching.models.platform import AuditLog
from deeptutor.teaching.models.platform import RoleGrant as RoleGrantRecord
from deeptutor.teaching.models.student_generation import (
    CourseGenerationPolicyRecord,
    StudentGenerationApprovalRecord,
    StudentGenerationRequestRecord,
)
from deeptutor.teaching.models.tenant import Enrollment, TeachingClass
from deeptutor.teaching.permissions import (
    ResourceScope,
    RoleGrant,
    permissions_for_grants,
)
from deeptutor.teaching.policies.student_generation import (
    CourseGenerationPolicy,
    StudentGenerationEvaluationContext,
    StudentGenerationQuota,
    StudentGenerationRequest,
    estimate_student_request,
    evaluate_student_request,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.student_generation import (
    StudentGenerationInputs,
    StudentGenerationResult,
)


class StudentGenerationConfigurationError(RuntimeError):
    """Required trusted policy state is missing or invalid."""


@dataclass(frozen=True, slots=True)
class StudentSafetyAssessment:
    generally_safe: bool
    minor_safe: bool
    restricted_topic: bool

    def __post_init__(self) -> None:
        for field in ("generally_safe", "minor_safe", "restricted_topic"):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"{field} must be boolean")


class StudentSafetyEvaluator(Protocol):
    async def assess(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentSafetyAssessment: ...


class FailClosedStudentSafetyEvaluator:
    """Deny generation until a trusted safety adapter is configured."""

    async def assess(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentSafetyAssessment:
        return StudentSafetyAssessment(
            generally_safe=False,
            minor_safe=False,
            restricted_topic=True,
        )


def _content_modes(encoded: str) -> frozenset[str]:
    encodings = {
        "source_grounded": frozenset({"source_grounded"}),
        "open_creation": frozenset({"open_creation"}),
        "source_grounded,open_creation": frozenset({"source_grounded", "open_creation"}),
    }
    modes = encodings.get(encoded)
    if modes is None:
        raise StudentGenerationConfigurationError("course generation content modes are invalid")
    return modes


class SqlAlchemyStudentGenerationRepository:
    """Re-read, decide, and persist within one locked tenant transaction."""

    def __init__(
        self,
        engine: AsyncEngine,
        tenant_id: str,
        *,
        safety_evaluator: StudentSafetyEvaluator | None = None,
    ) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._safety_evaluator = safety_evaluator or FailClosedStudentSafetyEvaluator()
        self._session_factory = async_sessionmaker(
            translated,
            expire_on_commit=False,
        )

    async def _load_inputs_locked(
        self,
        session: AsyncSession,
        learner_id: str,
        request: StudentGenerationRequest,
        safety: StudentSafetyAssessment,
    ) -> tuple[StudentGenerationInputs, datetime]:
        policy_record = await session.scalar(
            select(CourseGenerationPolicyRecord)
            .where(
                CourseGenerationPolicyRecord.course_id == request.course_id,
                CourseGenerationPolicyRecord.tenant_id == self._tenant_id,
            )
            .with_for_update(read=True)
        )
        if policy_record is None:
            raise StudentGenerationConfigurationError("course generation policy is unavailable")
        try:
            policy = CourseGenerationPolicy(
                allowed_content_modes=_content_modes(policy_record.allowed_content_modes),  # type: ignore[arg-type]
                daily_student_units=policy_record.daily_student_units,
                monthly_student_units=policy_record.monthly_student_units,
                allow_student_micro=policy_record.allow_student_micro,
                allow_student_full=policy_record.allow_student_full,
                allow_web_search=policy_record.allow_web_search,
                require_approval_for_restricted_topics=(
                    policy_record.require_approval_for_restricted_topics
                ),
                minor_safety_mode=policy_record.minor_safety_mode,
                micro_scene_limit=policy_record.micro_scene_limit,
                full_scene_limit=policy_record.full_scene_limit,
            )
        except ValueError:
            raise StudentGenerationConfigurationError(
                "course generation policy is invalid"
            ) from None

        enrollment_row = (
            await session.execute(
                select(Enrollment, TeachingClass)
                .join(TeachingClass, TeachingClass.id == Enrollment.class_id)
                .where(
                    Enrollment.class_id == request.class_id,
                    Enrollment.learner_id == learner_id,
                    Enrollment.status == "active",
                    TeachingClass.course_id == request.course_id,
                    TeachingClass.status == "active",
                )
                .with_for_update(read=True, of=(Enrollment, TeachingClass))
            )
        ).first()

        grant_records = tuple(
            await session.scalars(
                select(RoleGrantRecord)
                .where(
                    RoleGrantRecord.tenant_id == self._tenant_id,
                    RoleGrantRecord.user_id == learner_id,
                )
                .with_for_update(read=True)
            )
        )
        permissions = permissions_for_grants(
            (
                RoleGrant(
                    role=record.role,
                    scope_type=record.scope_type,
                    scope_id=record.scope_id,
                )
                for record in grant_records
            ),
            tenant_id=self._tenant_id,
        )
        resource = ResourceScope(
            tenant_id=self._tenant_id,
            course_id=request.course_id,
            class_id=request.class_id,
        )
        permission_name = f"classroom.generate.{request.mode}"
        has_permission = any(
            permission.allows_resource(permission_name, resource) for permission in permissions
        )

        if request.content_mode == "open_creation":
            source_permitted = True
        else:
            source_binding = await session.scalar(
                select(TenantSourceBinding)
                .where(
                    TenantSourceBinding.tenant_id == self._tenant_id,
                    TenantSourceBinding.course_id == request.course_id,
                    or_(
                        TenantSourceBinding.class_id.is_(None),
                        TenantSourceBinding.class_id == request.class_id,
                    ),
                )
                .with_for_update(read=True)
            )
            source_permitted = source_binding is not None

        decision_time = await session.scalar(text("SELECT clock_timestamp()"))
        if decision_time is None:
            raise StudentGenerationConfigurationError("database decision time is unavailable")
        decision_time = decision_time.astimezone(timezone.utc)
        day_start = decision_time.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)

        quota_records = tuple(
            await session.scalars(
                select(StudentGenerationRequestRecord)
                .where(
                    StudentGenerationRequestRecord.tenant_id == self._tenant_id,
                    StudentGenerationRequestRecord.learner_id == learner_id,
                    StudentGenerationRequestRecord.course_id == request.course_id,
                    StudentGenerationRequestRecord.quota_state.in_(("reserved", "settled")),
                    StudentGenerationRequestRecord.created_at >= month_start,
                )
                .with_for_update(read=True)
            )
        )
        daily_used = sum(
            record.estimated_units for record in quota_records if record.created_at >= day_start
        )
        monthly_used = sum(record.estimated_units for record in quota_records)

        return (
            StudentGenerationInputs(
                policy=policy,
                context=StudentGenerationEvaluationContext(
                    enrolled=enrollment_row is not None,
                    has_generation_permission=has_permission,
                    source_permitted=source_permitted,
                    generally_safe=safety.generally_safe,
                    minor_safe=safety.minor_safe,
                    restricted_topic=safety.restricted_topic,
                    approval_granted=False,
                ),
                quota=StudentGenerationQuota(
                    daily_used_units=daily_used,
                    monthly_used_units=monthly_used,
                ),
            ),
            decision_time,
        )

    async def evaluate_and_record(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentGenerationResult:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not learner_id or len(learner_id) > 128:
            raise ValueError("learner_id is invalid")
        safety = await self._safety_evaluator.assess(
            self._tenant_id,
            learner_id,
            request,
        )
        if type(safety) is not StudentSafetyAssessment:
            raise StudentGenerationConfigurationError("student safety assessment is invalid")

        request_id = f"student-request-{uuid.uuid4().hex}"
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {
                        "lock_key": (
                            f"student-generation:{self._tenant_id}:{request.course_id}:{learner_id}"
                        )
                    },
                )
                inputs, decision_time = await self._load_inputs_locked(
                    session,
                    learner_id,
                    request,
                    safety,
                )
                estimate = estimate_student_request(
                    policy=inputs.policy,
                    request=request,
                    context=inputs.context,
                    quota=inputs.quota,
                )
                decision = evaluate_student_request(
                    policy=inputs.policy,
                    request=request,
                    context=inputs.context,
                    quota=inputs.quota,
                )
                approval_id = (
                    f"student-approval-{uuid.uuid4().hex}"
                    if decision.outcome == "approval_required"
                    else None
                )
                session.add(
                    StudentGenerationRequestRecord(
                        id=request_id,
                        tenant_id=self._tenant_id,
                        learner_id=learner_id,
                        course_id=request.course_id,
                        class_id=request.class_id,
                        mode=request.mode,
                        content_mode=request.content_mode,
                        web_search_requested=request.web_search_requested,
                        scene_min=estimate.scene_range[0],
                        scene_max=estimate.scene_range[1],
                        duration_minutes_min=estimate.duration_minutes_range[0],
                        duration_minutes_max=estimate.duration_minutes_range[1],
                        estimated_units=estimate.quota_units,
                        quota_state=("reserved" if decision.outcome == "accepted" else "none"),
                        requires_outline_confirmation=(estimate.requires_outline_confirmation),
                        decision_outcome=decision.outcome,
                        decision_reason=decision.reason,
                        evaluated_checks=",".join(decision.evaluated_checks),
                        created_at=decision_time,
                    )
                )
                await session.flush()
                if approval_id is not None:
                    session.add(
                        StudentGenerationApprovalRecord(
                            id=approval_id,
                            tenant_id=self._tenant_id,
                            request_id=request_id,
                            reason=decision.reason,
                            status="pending",
                        )
                    )
                if decision.outcome in {"denied", "approval_required"}:
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=learner_id,
                            action=f"student_generation.{decision.outcome}",
                            resource_type="student_generation_request",
                            resource_id=request_id,
                        )
                    )
        return StudentGenerationResult(
            estimate=estimate,
            decision=decision,
            request_id=request_id,
            approval_id=approval_id,
        )
