"""Tenant-bound persistence for authoritative student generation decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
import uuid

from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.models.classrooms import (
    ClassroomAsset,
    ClassroomDraft,
    TenantSourceBinding,
)
from deeptutor.teaching.models.classrooms import (
    TeachingBrief as TeachingBriefRecord,
)
from deeptutor.teaching.models.platform import AuditLog
from deeptutor.teaching.models.platform import RoleGrant as RoleGrantRecord
from deeptutor.teaching.models.student_generation import (
    CourseGenerationPolicyRecord,
    StudentClassroomAssetRecord,
    StudentGenerationApprovalRecord,
    StudentGenerationRequestRecord,
    StudentSafetyAssessmentRecord,
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
    StudentGenerationApprovalConflict,
    StudentGenerationApprovalDetails,
    StudentGenerationApprovalNotFound,
    StudentGenerationInputs,
    StudentGenerationRequestDetails,
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


class SqlAlchemyStudentSafetyEvaluator:
    """Load one current, exact, reviewer-issued assessment or deny safely."""

    def __init__(self, engine: AsyncEngine, tenant_id: str) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        self._tenant_id = tenant_id
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._session_factory = async_sessionmaker(
            translated,
            expire_on_commit=False,
        )

    @staticmethod
    def _denied() -> StudentSafetyAssessment:
        return StudentSafetyAssessment(
            generally_safe=False,
            minor_safe=False,
            restricted_topic=True,
        )

    async def assess(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentSafetyAssessment:
        async with self._session_factory() as session:
            async with session.begin():
                return await self.assess_locked(
                    session,
                    tenant_id,
                    learner_id,
                    request,
                )

    async def assess_locked(
        self,
        session: AsyncSession,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentSafetyAssessment:
        if tenant_id != self._tenant_id or not learner_id or len(learner_id) > 128:
            return self._denied()
        decision_time = await session.scalar(text("SELECT clock_timestamp()"))
        if decision_time is None:
            raise StudentGenerationConfigurationError(
                "database safety assessment time is unavailable"
            )
        decision_time = decision_time.astimezone(timezone.utc)
        records = tuple(
            await session.scalars(
                select(StudentSafetyAssessmentRecord)
                .where(
                    StudentSafetyAssessmentRecord.tenant_id == self._tenant_id,
                    StudentSafetyAssessmentRecord.course_id == request.course_id,
                    StudentSafetyAssessmentRecord.class_id == request.class_id,
                    StudentSafetyAssessmentRecord.mode == request.mode,
                    StudentSafetyAssessmentRecord.content_mode == request.content_mode,
                    StudentSafetyAssessmentRecord.web_search_requested
                    == request.web_search_requested,
                    StudentSafetyAssessmentRecord.reviewed_at <= decision_time,
                    StudentSafetyAssessmentRecord.expires_at > decision_time,
                )
                .order_by(StudentSafetyAssessmentRecord.id)
                .with_for_update(read=True)
            )
        )
        if len(records) != 1:
            return self._denied()
        record = records[0]
        if (
            record.tenant_id != self._tenant_id
            or record.course_id != request.course_id
            or record.class_id != request.class_id
            or record.mode != request.mode
            or record.content_mode != request.content_mode
            or record.web_search_requested != request.web_search_requested
            or not record.reviewed_by.strip()
            or record.assessment_version <= 0
            or record.reviewed_at.tzinfo is None
            or record.expires_at.tzinfo is None
            or record.reviewed_at >= record.expires_at
            or record.reviewed_at > decision_time
            or record.expires_at <= decision_time
        ):
            return self._denied()
        return StudentSafetyAssessment(
            generally_safe=record.generally_safe,
            minor_safe=record.minor_safe,
            restricted_topic=record.restricted_topic,
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

    async def _assess_safety_locked(
        self,
        session: AsyncSession,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentSafetyAssessment:
        if isinstance(self._safety_evaluator, SqlAlchemyStudentSafetyEvaluator):
            safety = await self._safety_evaluator.assess_locked(
                session,
                self._tenant_id,
                learner_id,
                request,
            )
        else:
            safety = await self._safety_evaluator.assess(
                self._tenant_id,
                learner_id,
                request,
            )
        return self._validated_safety(safety)

    @staticmethod
    def _validated_safety(safety: object) -> StudentSafetyAssessment:
        if type(safety) is not StudentSafetyAssessment:
            raise StudentGenerationConfigurationError(
                "student safety assessment is invalid"
            )
        return safety

    async def _assess_external_safety(
        self,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentSafetyAssessment | None:
        if isinstance(self._safety_evaluator, SqlAlchemyStudentSafetyEvaluator):
            return None
        return self._validated_safety(
            await self._safety_evaluator.assess(
                self._tenant_id,
                learner_id,
                request,
            )
        )

    @staticmethod
    def _source_binding_statement(
        *,
        tenant_id: str,
        selected_request: StudentGenerationRequest,
        required_snapshot_id: str | None = None,
    ):
        statement = select(TenantSourceBinding).where(
            TenantSourceBinding.tenant_id == tenant_id,
            TenantSourceBinding.course_id == selected_request.course_id,
            or_(
                TenantSourceBinding.class_id.is_(None),
                TenantSourceBinding.class_id == selected_request.class_id,
            ),
        )
        if required_snapshot_id is not None:
            statement = statement.where(
                TenantSourceBinding.source_snapshot_id == required_snapshot_id
            )
        return statement

    @staticmethod
    def _approval_source_snapshot_statement(
        *,
        tenant_id: str,
        request_id: str,
    ):
        return (
            select(
                StudentClassroomAssetRecord,
                ClassroomAsset,
                ClassroomDraft,
                TeachingBriefRecord,
            )
            .select_from(StudentClassroomAssetRecord)
            .join(
                ClassroomAsset,
                and_(
                    ClassroomAsset.id == StudentClassroomAssetRecord.asset_id,
                    ClassroomAsset.tenant_id
                    == StudentClassroomAssetRecord.tenant_id,
                ),
            )
            .join(
                ClassroomDraft,
                and_(
                    ClassroomDraft.classroom_id == ClassroomAsset.id,
                    ClassroomDraft.tenant_id == ClassroomAsset.tenant_id,
                ),
            )
            .join(
                TeachingBriefRecord,
                and_(
                    TeachingBriefRecord.id == ClassroomDraft.teaching_brief_id,
                    TeachingBriefRecord.tenant_id == ClassroomDraft.tenant_id,
                ),
            )
            .where(
                StudentClassroomAssetRecord.tenant_id == tenant_id,
                StudentClassroomAssetRecord.request_id == request_id,
            )
        )

    async def _load_inputs_locked(
        self,
        session: AsyncSession,
        learner_id: str,
        request: StudentGenerationRequest,
        safety: StudentSafetyAssessment,
        *,
        approval_granted: bool = False,
        required_source_snapshot_id: str | None = None,
        excluded_quota_request_id: str | None = None,
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
                self._source_binding_statement(
                    tenant_id=self._tenant_id,
                    selected_request=request,
                    required_snapshot_id=required_source_snapshot_id,
                ).with_for_update(read=True)
            )
            source_permitted = source_binding is not None

        decision_time = await session.scalar(text("SELECT clock_timestamp()"))
        if decision_time is None:
            raise StudentGenerationConfigurationError("database decision time is unavailable")
        decision_time = decision_time.astimezone(timezone.utc)
        day_start = decision_time.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)

        quota_statement = select(StudentGenerationRequestRecord).where(
            StudentGenerationRequestRecord.tenant_id == self._tenant_id,
            StudentGenerationRequestRecord.learner_id == learner_id,
            StudentGenerationRequestRecord.course_id == request.course_id,
            StudentGenerationRequestRecord.quota_state.in_(("reserved", "settled")),
            StudentGenerationRequestRecord.created_at >= month_start,
        )
        if excluded_quota_request_id is not None:
            quota_statement = quota_statement.where(
                StudentGenerationRequestRecord.id != excluded_quota_request_id
            )
        quota_records = tuple(
            await session.scalars(
                quota_statement.with_for_update(read=True)
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
                    approval_granted=approval_granted,
                ),
                quota=StudentGenerationQuota(
                    daily_used_units=daily_used,
                    monthly_used_units=monthly_used,
                ),
            ),
            decision_time,
        )

    @staticmethod
    def _request(record: StudentGenerationRequestRecord) -> StudentGenerationRequest:
        try:
            return StudentGenerationRequest(
                course_id=record.course_id,
                class_id=record.class_id,
                mode=record.mode,  # type: ignore[arg-type]
                content_mode=record.content_mode,  # type: ignore[arg-type]
                web_search_requested=record.web_search_requested,
            )
        except ValueError:
            raise StudentGenerationConfigurationError(
                "stored student generation request is invalid"
            ) from None

    @staticmethod
    def _approval_details(
        approval: StudentGenerationApprovalRecord,
        request: StudentGenerationRequestRecord,
    ) -> StudentGenerationApprovalDetails:
        return StudentGenerationApprovalDetails(
            approval_id=approval.id,
            request_id=request.id,
            learner_id=request.learner_id,
            course_id=request.course_id,
            class_id=request.class_id,
            reason=approval.reason,
            status=approval.status,
            decided_by=approval.decided_by,
        )

    async def estimate(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ):
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not learner_id or len(learner_id) > 128:
            raise ValueError("learner_id is invalid")
        safety = await self._assess_external_safety(learner_id, request)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {
                        "lock_key": (
                            f"student-generation:{self._tenant_id}:"
                            f"{request.course_id}:{learner_id}"
                        )
                    },
                )
                if safety is None:
                    safety = await self._assess_safety_locked(
                        session,
                        learner_id,
                        request,
                    )
                inputs, _ = await self._load_inputs_locked(
                    session,
                    learner_id,
                    request,
                    safety,
                )
                return estimate_student_request(
                    policy=inputs.policy,
                    request=request,
                    context=inputs.context,
                    quota=inputs.quota,
                )

    def _approval_statement(self):
        return (
            select(
                StudentGenerationApprovalRecord,
                StudentGenerationRequestRecord,
            )
            .join(
                StudentGenerationRequestRecord,
                (
                    StudentGenerationRequestRecord.id
                    == StudentGenerationApprovalRecord.request_id
                )
                & (
                    StudentGenerationRequestRecord.tenant_id
                    == StudentGenerationApprovalRecord.tenant_id
                ),
            )
            .where(StudentGenerationApprovalRecord.tenant_id == self._tenant_id)
        )

    async def list_approval_details(
        self,
        tenant_id: str,
    ) -> tuple[StudentGenerationApprovalDetails, ...]:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        unbound_student_asset = exists(
            select(StudentClassroomAssetRecord.asset_id)
            .join(
                ClassroomDraft,
                and_(
                    ClassroomDraft.classroom_id
                    == StudentClassroomAssetRecord.asset_id,
                    ClassroomDraft.tenant_id
                    == StudentClassroomAssetRecord.tenant_id,
                ),
            )
            .where(
                StudentClassroomAssetRecord.tenant_id == self._tenant_id,
                StudentClassroomAssetRecord.request_id
                == StudentGenerationRequestRecord.id,
                ClassroomDraft.generation_job_id.is_(None),
            )
        )
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    self._approval_statement()
                    .where(
                        or_(
                            StudentGenerationApprovalRecord.status == "pending",
                            and_(
                                StudentGenerationApprovalRecord.status == "approved",
                                StudentGenerationRequestRecord.decision_outcome
                                == "accepted",
                                StudentGenerationRequestRecord.quota_state.in_(
                                    ("reserved", "settled")
                                ),
                                unbound_student_asset,
                            ),
                        )
                    )
                    .order_by(
                        StudentGenerationApprovalRecord.requested_at,
                        StudentGenerationApprovalRecord.id,
                    )
                )
            ).all()
        return tuple(self._approval_details(row[0], row[1]) for row in rows)

    async def get_approval_details(
        self,
        tenant_id: str,
        approval_id: str,
    ) -> StudentGenerationApprovalDetails | None:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not approval_id or len(approval_id) > 64:
            raise ValueError("approval_id is invalid")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    self._approval_statement().where(
                        StudentGenerationApprovalRecord.id == approval_id
                    )
                )
            ).one_or_none()
        return self._approval_details(row[0], row[1]) if row is not None else None

    async def get_request_details(
        self,
        tenant_id: str,
        request_id: str,
    ) -> StudentGenerationRequestDetails | None:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not request_id or len(request_id) > 64:
            raise ValueError("request_id is invalid")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        StudentGenerationRequestRecord,
                        StudentGenerationApprovalRecord,
                    )
                    .outerjoin(
                        StudentGenerationApprovalRecord,
                        (
                            StudentGenerationApprovalRecord.request_id
                            == StudentGenerationRequestRecord.id
                        )
                        & (
                            StudentGenerationApprovalRecord.tenant_id
                            == StudentGenerationRequestRecord.tenant_id
                        ),
                    )
                    .where(
                        StudentGenerationRequestRecord.id == request_id,
                        StudentGenerationRequestRecord.tenant_id == self._tenant_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        request, approval = row
        return StudentGenerationRequestDetails(
            request_id=request.id,
            learner_id=request.learner_id,
            course_id=request.course_id,
            class_id=request.class_id,
            mode=request.mode,
            decision_outcome=request.decision_outcome,
            decision_reason=request.decision_reason,
            quota_state=request.quota_state,
            scene_range=(request.scene_min, request.scene_max),
            duration_minutes_range=(
                request.duration_minutes_min,
                request.duration_minutes_max,
            ),
            estimated_units=request.estimated_units,
            requires_outline_confirmation=request.requires_outline_confirmation,
            approval_id=approval.id if approval is not None else None,
            approval_status=approval.status if approval is not None else None,
        )

    async def release_reservation(
        self,
        tenant_id: str,
        request_id: str,
        actor_id: str,
    ) -> None:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not actor_id or len(actor_id) > 128:
            raise ValueError("actor_id is invalid")
        async with self._session_factory() as session:
            async with session.begin():
                request = await session.scalar(
                    select(StudentGenerationRequestRecord)
                    .where(
                        StudentGenerationRequestRecord.id == request_id,
                        StudentGenerationRequestRecord.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if request is None:
                    raise StudentGenerationApprovalNotFound(request_id)
                if request.quota_state == "reserved":
                    request.quota_state = "released"
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=actor_id,
                            action="student_generation.quota_released",
                            resource_type="student_generation_request",
                            resource_id=request_id,
                        )
                    )
                    await session.flush()

    async def cancel_request(
        self,
        tenant_id: str,
        learner_id: str,
        request_id: str,
    ) -> None:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not learner_id or len(learner_id) > 128:
            raise ValueError("learner_id is invalid")
        if not request_id or len(request_id) > 64:
            raise ValueError("request_id is invalid")
        async with self._session_factory() as session:
            async with session.begin():
                approval = await session.scalar(
                    select(StudentGenerationApprovalRecord)
                    .where(
                        StudentGenerationApprovalRecord.request_id == request_id,
                        StudentGenerationApprovalRecord.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                request = await session.scalar(
                    select(StudentGenerationRequestRecord)
                    .where(
                        StudentGenerationRequestRecord.id == request_id,
                        StudentGenerationRequestRecord.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if request is None or request.learner_id != learner_id:
                    raise StudentGenerationApprovalNotFound(request_id)
                changed = False
                if approval is not None and approval.status == "pending":
                    decision_time = await session.scalar(text("SELECT clock_timestamp()"))
                    if decision_time is None:
                        raise StudentGenerationConfigurationError(
                            "database decision time is unavailable"
                        )
                    approval.status = "expired"
                    approval.decided_by = learner_id
                    approval.decided_at = decision_time.astimezone(timezone.utc)
                    changed = True
                if request.quota_state == "reserved":
                    request.quota_state = "released"
                    changed = True
                if changed:
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=learner_id,
                            action="student_generation.canceled",
                            resource_type="student_generation_request",
                            resource_id=request_id,
                        )
                    )
                    await session.flush()

    async def abort_approved_request(
        self,
        tenant_id: str,
        reviewer_id: str,
        approval_id: str,
    ) -> None:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not reviewer_id or len(reviewer_id) > 128:
            raise ValueError("reviewer_id is invalid")
        if not approval_id or len(approval_id) > 64:
            raise ValueError("approval_id is invalid")
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        self._approval_statement()
                        .where(StudentGenerationApprovalRecord.id == approval_id)
                        .with_for_update(
                            of=(
                                StudentGenerationApprovalRecord,
                                StudentGenerationRequestRecord,
                            )
                        )
                    )
                ).one_or_none()
                if row is None:
                    raise StudentGenerationApprovalNotFound(approval_id)
                approval, request = row
                if (
                    approval.id != approval_id
                    or approval.tenant_id != self._tenant_id
                    or approval.request_id != request.id
                    or request.tenant_id != self._tenant_id
                    or reviewer_id == request.learner_id
                ):
                    raise StudentGenerationApprovalNotFound(approval_id)
                already_aborted = (
                    approval.status == "expired" and request.quota_state == "released"
                )
                if not already_aborted and (
                    approval.status != "approved"
                    or approval.decided_by is None
                    or approval.decided_at is None
                    or request.decision_outcome != "accepted"
                    or request.quota_state not in {"reserved", "settled", "released"}
                ):
                    raise StudentGenerationApprovalConflict(approval_id)
                binding_row = (
                    await session.execute(
                        self._approval_source_snapshot_statement(
                            tenant_id=self._tenant_id,
                            request_id=request.id,
                        ).with_for_update(
                            read=True,
                            of=(
                                StudentClassroomAssetRecord,
                                ClassroomAsset,
                                ClassroomDraft,
                                TeachingBriefRecord,
                            ),
                        )
                    )
                ).one_or_none()
                if binding_row is None:
                    raise StudentGenerationApprovalNotFound(approval_id)
                marker, asset, draft, brief = binding_row
                if (
                    marker.tenant_id != self._tenant_id
                    or marker.request_id != request.id
                    or marker.asset_id != asset.id
                    or asset.tenant_id != self._tenant_id
                    or asset.owner_id != request.learner_id
                    or draft.tenant_id != self._tenant_id
                    or draft.classroom_id != asset.id
                    or draft.teaching_brief_id != brief.id
                    or draft.generation_job_id is not None
                    or brief.tenant_id != self._tenant_id
                    or brief.course_id != request.course_id
                    or brief.class_id != request.class_id
                ):
                    raise StudentGenerationApprovalNotFound(approval_id)
                if already_aborted:
                    return
                approval.status = "expired"
                if request.quota_state == "reserved":
                    request.quota_state = "released"
                session.add(
                    AuditLog(
                        tenant_id=self._tenant_id,
                        actor_id=reviewer_id,
                        action="student_generation.approved_start_failed",
                        resource_type="student_generation_request",
                        resource_id=request.id,
                    )
                )
                await session.flush()

    async def approve_and_reserve(
        self,
        tenant_id: str,
        reviewer_id: str,
        approval_id: str,
        expected: StudentGenerationApprovalDetails,
    ) -> StudentGenerationApprovalDetails:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not reviewer_id or len(reviewer_id) > 128:
            raise ValueError("reviewer_id is invalid")
        current = await self.get_approval_details(tenant_id, approval_id)
        if current is None:
            raise StudentGenerationApprovalNotFound(approval_id)
        async with self._session_factory() as session:
            prelocked_request = await session.scalar(
                select(StudentGenerationRequestRecord).where(
                    StudentGenerationRequestRecord.id == current.request_id,
                    StudentGenerationRequestRecord.tenant_id == self._tenant_id,
                )
            )
        if prelocked_request is None:
            raise StudentGenerationApprovalNotFound(approval_id)
        request = self._request(prelocked_request)
        safety = await self._assess_external_safety(current.learner_id, request)

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {
                        "lock_key": (
                            f"student-generation:{self._tenant_id}:"
                            f"{request.course_id}:{current.learner_id}"
                        )
                    },
                )
                row = (
                    await session.execute(
                        self._approval_statement()
                        .where(StudentGenerationApprovalRecord.id == approval_id)
                        .with_for_update(
                            of=(
                                StudentGenerationApprovalRecord,
                                StudentGenerationRequestRecord,
                            )
                        )
                    )
                ).one_or_none()
                if row is None:
                    raise StudentGenerationApprovalNotFound(approval_id)
                approval, stored_request = row
                locked_details = self._approval_details(approval, stored_request)
                if (
                    reviewer_id == stored_request.learner_id
                    or expected.approval_id != approval_id
                    or locked_details.approval_id != expected.approval_id
                    or locked_details.request_id != expected.request_id
                    or locked_details.learner_id != expected.learner_id
                    or locked_details.course_id != expected.course_id
                    or locked_details.class_id != expected.class_id
                    or locked_details.reason != expected.reason
                    or expected.status not in {"pending", "approved"}
                    or (
                        locked_details.status != expected.status
                        and not (
                            expected.status == "pending"
                            and locked_details.status == "approved"
                        )
                    )
                    or (
                        locked_details.status == expected.status
                        and locked_details.decided_by != expected.decided_by
                    )
                    or approval.id != approval_id
                    or approval.tenant_id != self._tenant_id
                    or approval.request_id != stored_request.id
                    or stored_request.tenant_id != self._tenant_id
                ):
                    raise StudentGenerationApprovalNotFound(approval_id)
                if approval.status not in {"pending", "approved"}:
                    raise StudentGenerationApprovalConflict(approval_id)
                locked_request = self._request(stored_request)
                if locked_request != request:
                    raise StudentGenerationConfigurationError(
                        "student approval request changed during re-evaluation"
                    )
                request = locked_request
                source_row = (
                    await session.execute(
                        self._approval_source_snapshot_statement(
                            tenant_id=self._tenant_id,
                            request_id=stored_request.id,
                        ).with_for_update(
                            read=True,
                            of=(
                                StudentClassroomAssetRecord,
                                ClassroomAsset,
                                ClassroomDraft,
                                TeachingBriefRecord,
                            ),
                        )
                    )
                ).one_or_none()
                if source_row is None:
                    raise StudentGenerationConfigurationError(
                        "student approval classroom binding is unavailable"
                    )
                marker, asset, draft, brief = source_row
                target_state = (
                    "generating_content"
                    if locked_request.mode == "micro"
                    else "generating_outline"
                )
                if (
                    marker.tenant_id != self._tenant_id
                    or marker.request_id != stored_request.id
                    or marker.asset_id != asset.id
                    or asset.tenant_id != self._tenant_id
                    or asset.owner_id != stored_request.learner_id
                    or draft.tenant_id != self._tenant_id
                    or draft.classroom_id != asset.id
                    or draft.teaching_brief_id != brief.id
                    or brief.tenant_id != self._tenant_id
                    or brief.course_id != stored_request.course_id
                    or brief.class_id != stored_request.class_id
                ):
                    raise StudentGenerationApprovalNotFound(approval_id)
                if approval.status == "pending" and (
                    stored_request.decision_outcome != "approval_required"
                    or stored_request.quota_state != "none"
                    or stored_request.decision_reason != approval.reason
                    or approval.decided_by is not None
                    or approval.decided_at is not None
                    or draft.generation_job_id is not None
                    or asset.lifecycle_state != "draft"
                ):
                    raise StudentGenerationApprovalConflict(approval_id)
                if approval.status == "approved" and (
                    stored_request.decision_outcome != "accepted"
                    or stored_request.quota_state not in {"reserved", "settled"}
                    or approval.decided_by is None
                    or approval.decided_at is None
                ):
                    raise StudentGenerationApprovalConflict(approval_id)
                if asset.lifecycle_state in {"canceled", "failed"}:
                    approval.status = "expired"
                    if stored_request.quota_state == "reserved":
                        stored_request.quota_state = "released"
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=reviewer_id,
                            action="student_generation.approval_recovery_expired",
                            resource_type="student_generation_request",
                            resource_id=stored_request.id,
                        )
                    )
                    await session.flush()
                    return self._approval_details(approval, stored_request)
                if approval.status == "approved" and draft.generation_job_id is not None:
                    return locked_details
                if approval.status == "approved" and asset.lifecycle_state not in {
                    "draft",
                    target_state,
                }:
                    raise StudentGenerationApprovalConflict(approval_id)
                required_source_snapshot_id = brief.source_snapshot_id
                if (
                    locked_request.content_mode == "source_grounded"
                    and required_source_snapshot_id is None
                ):
                    raise StudentGenerationConfigurationError(
                        "student approval source binding is unavailable"
                    )
                if safety is None:
                    safety = await self._assess_safety_locked(
                        session,
                        stored_request.learner_id,
                        locked_request,
                    )
                inputs, decision_time = await self._load_inputs_locked(
                    session,
                    stored_request.learner_id,
                    locked_request,
                    safety,
                    approval_granted=True,
                    required_source_snapshot_id=required_source_snapshot_id,
                    excluded_quota_request_id=(
                        stored_request.id if approval.status == "approved" else None
                    ),
                )
                estimate = estimate_student_request(
                    policy=inputs.policy,
                    request=locked_request,
                    context=inputs.context,
                    quota=inputs.quota,
                )
                decision = evaluate_student_request(
                    policy=inputs.policy,
                    request=locked_request,
                    context=inputs.context,
                    quota=inputs.quota,
                )
                approved = decision.outcome == "accepted"
                replay = approval.status == "approved"
                if replay:
                    if not approved:
                        approval.status = "expired"
                        if stored_request.quota_state in {"reserved", "settled"}:
                            stored_request.quota_state = "released"
                    else:
                        stored_request.scene_min = estimate.scene_range[0]
                        stored_request.scene_max = estimate.scene_range[1]
                        stored_request.duration_minutes_min = (
                            estimate.duration_minutes_range[0]
                        )
                        stored_request.duration_minutes_max = (
                            estimate.duration_minutes_range[1]
                        )
                        stored_request.estimated_units = estimate.quota_units
                        stored_request.requires_outline_confirmation = (
                            estimate.requires_outline_confirmation
                        )
                        stored_request.decision_outcome = decision.outcome
                        stored_request.decision_reason = decision.reason
                        stored_request.evaluated_checks = ",".join(
                            decision.evaluated_checks
                        )
                        stored_request.quota_state = "reserved"
                else:
                    approval.status = "approved" if approved else "expired"
                    approval.decided_by = reviewer_id
                    approval.decided_at = decision_time
                    stored_request.scene_min = estimate.scene_range[0]
                    stored_request.scene_max = estimate.scene_range[1]
                    stored_request.duration_minutes_min = estimate.duration_minutes_range[0]
                    stored_request.duration_minutes_max = estimate.duration_minutes_range[1]
                    stored_request.estimated_units = estimate.quota_units
                    stored_request.requires_outline_confirmation = (
                        estimate.requires_outline_confirmation
                    )
                    stored_request.decision_outcome = decision.outcome
                    stored_request.decision_reason = decision.reason
                    stored_request.evaluated_checks = ",".join(decision.evaluated_checks)
                    stored_request.quota_state = "reserved" if approved else "none"
                session.add(
                    AuditLog(
                        tenant_id=self._tenant_id,
                        actor_id=reviewer_id,
                        action=(
                            "student_generation.approval_recovery_validated"
                            if replay and approved
                            else (
                                "student_generation.approved"
                                if approved
                                else "student_generation.approval_expired"
                            )
                        ),
                        resource_type="student_generation_request",
                        resource_id=stored_request.id,
                    )
                )
                await session.flush()
                return self._approval_details(approval, stored_request)

    async def reject(
        self,
        tenant_id: str,
        reviewer_id: str,
        approval_id: str,
        expected: StudentGenerationApprovalDetails,
    ) -> StudentGenerationApprovalDetails:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant_id does not match repository binding")
        if not reviewer_id or len(reviewer_id) > 128:
            raise ValueError("reviewer_id is invalid")
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        self._approval_statement()
                        .where(StudentGenerationApprovalRecord.id == approval_id)
                        .with_for_update(
                            of=(
                                StudentGenerationApprovalRecord,
                                StudentGenerationRequestRecord,
                            )
                        )
                    )
                ).one_or_none()
                if row is None:
                    raise StudentGenerationApprovalNotFound(approval_id)
                approval, stored_request = row
                locked_details = self._approval_details(approval, stored_request)
                if (
                    reviewer_id == stored_request.learner_id
                    or locked_details.approval_id != expected.approval_id
                    or locked_details.request_id != expected.request_id
                    or locked_details.learner_id != expected.learner_id
                    or locked_details.course_id != expected.course_id
                    or locked_details.class_id != expected.class_id
                    or locked_details.reason != expected.reason
                ):
                    raise StudentGenerationApprovalNotFound(approval_id)
                if approval.status != "pending":
                    raise StudentGenerationApprovalConflict(approval_id)
                decision_time = await session.scalar(text("SELECT clock_timestamp()"))
                if decision_time is None:
                    raise StudentGenerationConfigurationError(
                        "database decision time is unavailable"
                    )
                approval.status = "rejected"
                approval.decided_by = reviewer_id
                approval.decided_at = decision_time.astimezone(timezone.utc)
                session.add(
                    AuditLog(
                        tenant_id=self._tenant_id,
                        actor_id=reviewer_id,
                        action="student_generation.rejected",
                        resource_type="student_generation_request",
                        resource_id=stored_request.id,
                    )
                )
                await session.flush()
                return self._approval_details(approval, stored_request)

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
        safety = await self._assess_external_safety(learner_id, request)
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
                if safety is None:
                    safety = await self._assess_safety_locked(
                        session,
                        learner_id,
                        request,
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
