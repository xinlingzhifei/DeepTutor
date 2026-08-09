"""Student-owned classroom orchestration over policy and task boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Protocol
import uuid

from deeptutor.teaching.brief_builder import KnowledgePointSpec, TeachingBriefSpec
from deeptutor.teaching.contracts import GenerationRequest, canonical_json_bytes
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelector
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.policies.student_generation import (
    StudentGenerationEstimate,
    StudentGenerationRequest,
)
from deeptutor.teaching.repositories.jobs import (
    GenerationJobRequest,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.services.classrooms import (
    ClassroomRecord,
    ClassroomService,
    GenerationStage,
    NewClassroomWorkflow,
    SqlAlchemyClassroomGeneration,
)
from deeptutor.teaching.services.student_generation import (
    StudentGenerationApprovalDetails,
    StudentGenerationApprovalService,
    StudentGenerationRequestDetails,
    StudentGenerationResult,
    StudentGenerationService,
)
from deeptutor.teaching.tenant_context import TenantContext


class StudentClassroomError(RuntimeError):
    pass


class StudentClassroomDenied(StudentClassroomError, PermissionError):
    pass


class StudentClassroomNotFound(StudentClassroomError, LookupError):
    pass


class StudentClassroomConflict(StudentClassroomError):
    pass


class StudentClassroomWorkflow(Protocol):
    async def create(
        self,
        context: TenantContext,
        request: object,
        result: StudentGenerationResult,
    ): ...

    async def start_generation(
        self,
        context: TenantContext,
        record: object,
        estimate: object,
    ): ...

    async def list(self, context: TenantContext): ...

    async def get(self, context: TenantContext, asset_id: str): ...

    async def update_outline(
        self,
        context: TenantContext,
        asset_id: str,
        outline: dict[str, object],
        expected_revision: int,
    ): ...

    async def confirm_outline(self, context: TenantContext, asset_id: str): ...

    async def cancel(self, context: TenantContext, asset_id: str): ...

    async def approval_response(
        self,
        approval: StudentGenerationApprovalDetails,
    ): ...

    async def start_approved_generation(
        self,
        context: TenantContext,
        approval: StudentGenerationApprovalDetails,
    ): ...

    async def copy_to_teacher_draft(
        self,
        context: TenantContext,
        asset_id: str,
    ): ...


@dataclass(frozen=True, slots=True)
class StudentClassroomView:
    asset_id: str
    request_id: str
    approval_id: str | None
    generation_job_id: str | None
    status: str
    course_id: str
    class_id: str
    mode: str
    owner_id: str
    revision: int
    outline: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class StudentGenerationApprovalView:
    approval_id: str
    request_id: str
    asset_id: str
    learner_id: str
    course_id: str
    class_id: str
    reason: str
    status: str
    decided_by: str | None
    generation_job_id: str | None


class StudentJobCancellationGateway(Protocol):
    async def cancel(self, request) -> None: ...


class SqlAlchemyStudentClassroomGeneration:
    """Create private student jobs through the existing durable job kernel."""

    def __init__(
        self,
        repository: SqlAlchemyGenerationJobRepository,
        selector: DataPlaneSelector,
        cancellation_gateway: StudentJobCancellationGateway | None = None,
    ) -> None:
        self._repository = repository
        self._selector = selector
        self._cancellation_gateway = cancellation_gateway
        self._full_generation = SqlAlchemyClassroomGeneration(repository, selector)

    @staticmethod
    def _micro_job_id(tenant_id: str, asset_id: str) -> str:
        digest = hashlib.sha256(f"{tenant_id}\0{asset_id}\0micro".encode()).hexdigest()
        return f"job-{digest[:48]}"

    async def start(
        self,
        *,
        context: TenantContext,
        record: ClassroomRecord,
        estimate,
        mode: str,
        actor_id: str,
    ) -> GenerationStage:
        brief = record.teaching_brief
        if brief is None or mode not in {"micro", "full"}:
            raise StudentClassroomConflict("student generation brief is unavailable")
        selection = await self._selector.resolve(context.tenant_id)
        if selection is None:
            raise StudentClassroomConflict("generation data plane is unavailable")
        job_id = (
            self._micro_job_id(context.tenant_id, record.asset_id)
            if mode == "micro"
            else SqlAlchemyClassroomGeneration._job_id(
                context.tenant_id,
                record.asset_id,
            )
        )
        phase = "micro" if mode == "micro" else "outline"
        priority = "student_micro" if mode == "micro" else "full"
        generation = GenerationRequest(
            schema_version="1.0",
            tenant_id=context.tenant_id,
            request_id=f"request-{job_id[4:]}",
            job_id=job_id,
            idempotency_key=f"student-classroom-{phase}-{record.asset_id}",
            phase=phase,
            classroom_mode=mode,
            teaching_brief_id=brief.brief_id,
            teaching_brief_sha256=brief.content_sha256,
            teaching_brief=brief,
            confirmed_outline=None,
            confirmed_outline_sha256=None,
            template_id=brief.template_policy.template_id,
            template_version=brief.template_policy.template_version,
            scene_budget=estimate.scene_range[1],
            duration_minutes=brief.duration_minutes,
            requested_exports=["classroom_zip"],
            callback_context=record.draft_id,
            data_plane_route_id=selection.route_ref,
            priority=priority,
        )
        payload = canonical_json_bytes(generation).decode("utf-8")
        await self._repository.create_job_and_reserve(
            GenerationJobRequest(
                tenant_id=context.tenant_id,
                job_id=job_id,
                job_kind="generation",
                phase="content" if phase == "micro" else "outline",
                export_format=None,
                priority=priority,
                quota_units=estimate.quota_units,
                actor_id=actor_id,
                owner_id=record.owner_id,
                visibility="private",
                request_id=generation.request_id,
                idempotency_key=generation.idempotency_key,
                request_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                data_plane_route_id=selection.route_ref,
                provider_profile_id=selection.provider_profile_ref,
                worker_pool_ref=selection.worker_pool_ref,
                queue_ref=selection.queue_ref,
                request_payload=payload,
                classroom_draft_id=record.draft_id,
                resource_course_id=record.course_id,
                resource_class_id=record.class_id,
                public_request_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            )
        )
        try:
            details = await self._repository.get_job_details(context.tenant_id, job_id)
            if details is None:
                raise StudentClassroomConflict(
                    "student generation job is unavailable"
                )
        except Exception:
            try:
                await self.request_cancel(context.tenant_id, job_id)
            except Exception as cancellation_error:
                raise StudentClassroomConflict(
                    "student generation job compensation failed"
                ) from cancellation_error
            raise
        return self._full_generation._stage(details)

    async def get_stage(
        self,
        *,
        context: TenantContext,
        job_id: str,
    ) -> GenerationStage:
        return await self._full_generation.get_stage(context=context, job_id=job_id)

    async def start_content(self, **kwargs) -> GenerationStage:
        return await self._full_generation.start_content(**kwargs)

    async def request_cancel(self, tenant_id: str, job_id: str):
        cancellation = await self._repository.request_cancel(tenant_id, job_id)
        if cancellation is None:
            details = await self._repository.get_job_details(tenant_id, job_id)
            if details is not None and details.status in {
                "succeeded",
                "failed",
                "canceled",
            }:
                return True
            return None
        if cancellation.running:
            if self._cancellation_gateway is None:
                raise StudentClassroomConflict("student cancellation gateway is unavailable")
            await self._cancellation_gateway.cancel(cancellation)
            await self._repository.finish_requested_cancellation(tenant_id, job_id)
        return cancellation


class SqlAlchemyStudentClassroomWorkflow:
    """Bind student policy records to the existing classroom state machine."""

    def __init__(
        self,
        *,
        repository,
        classroom_service: ClassroomService,
        brief_builder,
        generation,
        request_repository,
    ) -> None:
        self._repository = repository
        self._classroom_service = classroom_service
        self._brief_builder = brief_builder
        self._generation = generation
        self._request_repository = request_repository

    @staticmethod
    def _identifier(prefix: str, tenant_id: str, request_id: str) -> str:
        digest = hashlib.sha256(f"{tenant_id}\0{request_id}\0{prefix}".encode()).hexdigest()
        return f"student-{prefix}-{digest[:48]}"

    def _brief_spec(self, request: object) -> TeachingBriefSpec:
        course_id = str(getattr(request, "course_id"))
        class_id = str(getattr(request, "class_id"))
        mode = getattr(request, "mode")
        title = str(getattr(request, "title", "") or "Student classroom")
        objective = str(getattr(request, "objective", "") or title)
        return TeachingBriefSpec(
            course_id=course_id,
            class_id=class_id,
            objective=objective,
            grade_band=str(getattr(request, "grade_band", "") or "student"),
            audience=str(getattr(request, "audience", "") or "student"),
            duration_minutes=int(
                getattr(request, "duration_minutes", 0) or (15 if mode == "micro" else 45)
            ),
            classroom_mode=mode,
            web_policy=(
                "enabled" if getattr(request, "web_search_requested") else "disabled"
            ),
            template_id=str(getattr(request, "template_id", "") or "student-default"),
            template_version=str(getattr(request, "template_version", "") or "1"),
            knowledge_points=(
                KnowledgePointSpec(
                    knowledge_point_id="student-topic",
                    title=title,
                    description=objective,
                ),
            ),
            content_mode=getattr(request, "content_mode"),
            open_creation_acknowledged=(
                getattr(request, "content_mode") == "open_creation"
            ),
        )

    async def _build_brief(self, request: object):
        spec = self._brief_spec(request)
        if spec.content_mode == "open_creation":
            return self._brief_builder.open_creation(spec).contract
        source_type = getattr(request, "source_type", None)
        source_ref = getattr(request, "source_ref", None)
        if source_type == "knowledge_base" and source_ref:
            return (await self._brief_builder.from_kb(source_ref, spec)).contract
        if source_type == "pdf" and source_ref:
            return (await self._brief_builder.from_pdf(source_ref, spec)).contract
        raise StudentClassroomConflict("source-grounded classroom requires a source")

    @staticmethod
    def _view(
        record: ClassroomRecord,
        *,
        request_id: str,
        approval_id: str | None,
        mode: str,
        status: str,
    ) -> StudentClassroomView:
        return StudentClassroomView(
            asset_id=record.asset_id,
            request_id=request_id,
            approval_id=approval_id,
            generation_job_id=record.job_id,
            status=status,
            course_id=record.course_id,
            class_id=record.class_id,
            mode=mode,
            owner_id=record.owner_id,
            revision=record.revision,
            outline=record.outline,
        )

    async def create(
        self,
        context: TenantContext,
        request: object,
        result: StudentGenerationResult,
    ) -> StudentClassroomView:
        brief = await self._build_brief(request)
        asset_id = self._identifier("asset", context.tenant_id, result.request_id)
        draft_id = self._identifier("draft", context.tenant_id, result.request_id)
        awaiting_approval = result.decision.outcome == "approval_required"
        initial_state = (
            "draft"
            if awaiting_approval
            else (
                "generating_content"
                if getattr(request, "mode") == "micro"
                else "generating_outline"
            )
        )
        record = await self._repository.create_workflow(
            NewClassroomWorkflow(
                tenant_id=context.tenant_id,
                asset_id=asset_id,
                draft_id=draft_id,
                owner_id=context.user_id,
                title=str(getattr(request, "title", "") or "Student classroom"),
                teaching_brief=brief,
                creation_idempotency_key=result.request_id,
                creation_request_sha256=hashlib.sha256(
                    result.request_id.encode("utf-8")
                ).hexdigest(),
                initial_lifecycle_state=initial_state,
                student_generation_request_id=result.request_id,
            )
        )
        return self._view(
            record,
            request_id=result.request_id,
            approval_id=result.approval_id,
            mode=str(getattr(request, "mode")),
            status="awaiting_approval" if awaiting_approval else "preparing",
        )

    async def start_generation(
        self,
        context: TenantContext,
        record: object,
        estimate: object,
    ) -> StudentClassroomView:
        if not isinstance(record, StudentClassroomView):
            raise StudentClassroomConflict("student classroom view is invalid")
        workflow = await self._repository.get_workflow(record.asset_id)
        if (
            workflow is None
            or workflow.student_generation_request_id != record.request_id
            or workflow.owner_id != context.user_id
        ):
            raise StudentClassroomNotFound(record.asset_id)
        stage = None
        try:
            stage = await self._generation.start(
                context=context,
                record=workflow,
                estimate=estimate,
                mode=record.mode,
                actor_id=context.user_id,
            )
            workflow = await self._repository.attach_generation_job(
                record.asset_id,
                stage.job_id,
                "content" if record.mode == "micro" else "outline",
            )
        except Exception:
            if stage is not None:
                await self._generation.request_cancel(
                    context.tenant_id,
                    stage.job_id,
                )
            await self._request_repository.cancel_request(
                context.tenant_id,
                context.user_id,
                record.request_id,
            )
            await self._repository.mark_canceled(record.asset_id)
            raise
        return self._view(
            workflow,
            request_id=record.request_id,
            approval_id=record.approval_id,
            mode=record.mode,
            status=stage.status,
        )

    async def _details(
        self,
        context: TenantContext,
        record: ClassroomRecord,
    ) -> StudentGenerationRequestDetails:
        request_id = record.student_generation_request_id
        if request_id is None:
            raise StudentClassroomNotFound(record.asset_id)
        details = await self._request_repository.get_request_details(
            context.tenant_id,
            request_id,
        )
        if (
            details is None
            or details.learner_id != record.owner_id
            or details.course_id != record.course_id
            or details.class_id != record.class_id
        ):
            raise StudentClassroomConflict("student classroom policy binding is invalid")
        return details

    @staticmethod
    def _status(record: ClassroomRecord, details: StudentGenerationRequestDetails) -> str:
        if record.lifecycle_state == "canceled":
            return "canceled"
        if details.approval_status == "pending":
            return "awaiting_approval"
        if details.approval_status in {"rejected", "expired"}:
            return details.approval_status
        return record.status

    async def list(self, context: TenantContext) -> tuple[StudentClassroomView, ...]:
        records = await self._classroom_service.list(context)
        views: list[StudentClassroomView] = []
        for record in records:
            details = await self._details(context, record)
            views.append(
                self._view(
                    record,
                    request_id=details.request_id,
                    approval_id=details.approval_id,
                    mode=details.mode,
                    status=self._status(record, details),
                )
            )
        return tuple(views)

    async def get(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> StudentClassroomView | None:
        record = await self._classroom_service.get(context, asset_id)
        if record is None:
            return None
        details = await self._details(context, record)
        return self._view(
            record,
            request_id=details.request_id,
            approval_id=details.approval_id,
            mode=details.mode,
            status=self._status(record, details),
        )

    async def update_outline(
        self,
        context: TenantContext,
        asset_id: str,
        outline: dict[str, object],
        expected_revision: int,
    ) -> StudentClassroomView:
        record = await self._classroom_service.update_outline(
            context,
            asset_id,
            outline,
            expected_revision,
        )
        details = await self._details(context, record)
        return self._view(
            record,
            request_id=details.request_id,
            approval_id=details.approval_id,
            mode=details.mode,
            status=self._status(record, details),
        )

    async def confirm_outline(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> StudentClassroomView:
        record = await self._classroom_service.confirm_outline(context, asset_id)
        details = await self._details(context, record)
        return self._view(
            record,
            request_id=details.request_id,
            approval_id=details.approval_id,
            mode=details.mode,
            status="queued",
        )

    async def cancel(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> StudentClassroomView | None:
        record = await self._classroom_service.get(context, asset_id)
        if record is None:
            return None
        details = await self._details(context, record)
        if (
            record.lifecycle_state == "canceled"
            and details.quota_state in {"none", "released", "settled"}
            and details.approval_status != "pending"
        ):
            return self._view(
                record,
                request_id=details.request_id,
                approval_id=details.approval_id,
                mode=details.mode,
                status="canceled",
            )
        record = await self._repository.mark_canceled(asset_id)
        if record.job_id is not None:
            cancellation = await self._generation.request_cancel(
                context.tenant_id,
                record.job_id,
            )
            if cancellation is None:
                raise StudentClassroomConflict("student classroom cannot be canceled")
        await self._request_repository.cancel_request(
            context.tenant_id,
            context.user_id,
            details.request_id,
        )
        return self._view(
            record,
            request_id=details.request_id,
            approval_id=details.approval_id,
            mode=details.mode,
            status="canceled",
        )

    async def approval_response(
        self,
        approval: StudentGenerationApprovalDetails,
    ) -> StudentGenerationApprovalView:
        record = await self._repository.get_student_workflow(approval.request_id)
        if record is None or record.owner_id != approval.learner_id:
            raise StudentClassroomConflict("student approval asset binding is invalid")
        return StudentGenerationApprovalView(
            approval_id=approval.approval_id,
            request_id=approval.request_id,
            asset_id=record.asset_id,
            learner_id=approval.learner_id,
            course_id=approval.course_id,
            class_id=approval.class_id,
            reason=approval.reason,
            status=approval.status,
            decided_by=approval.decided_by,
            generation_job_id=record.job_id,
        )

    async def start_approved_generation(
        self,
        context: TenantContext,
        approval: StudentGenerationApprovalDetails,
    ) -> StudentGenerationApprovalView:
        record = await self._repository.get_student_workflow(approval.request_id)
        if record is None or record.owner_id != approval.learner_id:
            raise StudentClassroomConflict("student approval asset binding is invalid")
        details = await self._request_repository.get_request_details(
            context.tenant_id,
            approval.request_id,
        )
        if (
            details is None
            or details.request_id != approval.request_id
            or details.learner_id != approval.learner_id
            or details.course_id != approval.course_id
            or details.class_id != approval.class_id
            or details.approval_id != approval.approval_id
            or details.approval_status != "approved"
            or details.decision_outcome != "accepted"
            or details.quota_state not in {"reserved", "settled"}
        ):
            raise StudentClassroomConflict("student approval reservation is unavailable")
        asset_id = record.asset_id
        target_state = (
            "generating_content" if details.mode == "micro" else "generating_outline"
        )
        if record.job_id is not None:
            if record.lifecycle_state in {"canceled", "failed", "draft"}:
                raise StudentClassroomConflict(
                    "student approval job binding is unavailable"
                )
            return StudentGenerationApprovalView(
                approval_id=approval.approval_id,
                request_id=approval.request_id,
                asset_id=record.asset_id,
                learner_id=approval.learner_id,
                course_id=approval.course_id,
                class_id=approval.class_id,
                reason=approval.reason,
                status=approval.status,
                decided_by=approval.decided_by,
                generation_job_id=record.job_id,
            )
        if record.lifecycle_state not in {"draft", target_state}:
            raise StudentClassroomConflict("student approval asset is not recoverable")
        stage = None
        try:
            record = await self._repository.start_student_generation(
                asset_id,
                details.mode,
            )
            owner_context = TenantContext(
                tenant_id=context.tenant_id,
                schema_name=context.schema_name,
                user_id=record.owner_id,
                permissions=frozenset(),
            )
            estimate = StudentGenerationEstimate(
                scene_range=details.scene_range,
                duration_minutes_range=details.duration_minutes_range,
                quota_units=details.estimated_units,
                requires_outline_confirmation=details.requires_outline_confirmation,
                requires_approval=False,
            )
            stage = await self._generation.start(
                context=owner_context,
                record=record,
                estimate=estimate,
                mode=details.mode,
                actor_id=context.user_id,
            )
            record = await self._repository.attach_generation_job(
                record.asset_id,
                stage.job_id,
                "content" if details.mode == "micro" else "outline",
            )
        except Exception:
            if stage is not None:
                await self._generation.request_cancel(
                    context.tenant_id,
                    stage.job_id,
                )
            await self._request_repository.abort_approved_request(
                context.tenant_id,
                context.user_id,
                approval.approval_id,
            )
            await self._repository.mark_canceled(asset_id)
            raise
        return StudentGenerationApprovalView(
            approval_id=approval.approval_id,
            request_id=approval.request_id,
            asset_id=record.asset_id,
            learner_id=approval.learner_id,
            course_id=approval.course_id,
            class_id=approval.class_id,
            reason=approval.reason,
            status=approval.status,
            decided_by=approval.decided_by,
            generation_job_id=record.job_id,
        )

    async def copy_to_teacher_draft(
        self,
        context: TenantContext,
        asset_id: str,
    ):
        record = await self._repository.get_workflow(asset_id)
        if record is None or record.student_generation_request_id is None:
            return None
        resource = ResourceScope(
            tenant_id=context.tenant_id,
            course_id=record.course_id,
            class_id=record.class_id,
        )
        if not any(
            permission.allows_resource("classroom.create", resource)
            for permission in context.permissions
        ):
            return None
        nonce = uuid.uuid4().hex
        return await self._repository.copy_student_to_teacher_draft(
            asset_id,
            f"asset-{nonce}",
            f"draft-{nonce}",
            f"copy-{nonce}",
            context.user_id,
        )


class StudentClassroomService:
    """Keep policy decisions and classroom state changes behind service ports."""

    def __init__(
        self,
        *,
        policy_service: StudentGenerationService,
        workflow: StudentClassroomWorkflow,
        approval_service: StudentGenerationApprovalService,
    ) -> None:
        self._policy_service = policy_service
        self._workflow = workflow
        self._approval_service = approval_service

    @staticmethod
    def _policy_request(request: object) -> StudentGenerationRequest:
        return StudentGenerationRequest(
            course_id=str(getattr(request, "course_id")),
            class_id=str(getattr(request, "class_id")),
            mode=getattr(request, "mode"),
            content_mode=getattr(request, "content_mode"),
            web_search_requested=getattr(request, "web_search_requested"),
        )

    async def estimate(self, _context: TenantContext, request: object):
        return await self._policy_service.estimate(self._policy_request(request))

    async def create(self, context: TenantContext, request: object):
        result = await self._policy_service.evaluate(self._policy_request(request))
        if result.decision.outcome == "denied":
            raise StudentClassroomDenied(result.decision.reason)
        try:
            record = await self._workflow.create(context, request, result)
            if result.decision.outcome == "approval_required":
                return record
            return await self._workflow.start_generation(
                context,
                record,
                result.estimate,
            )
        except Exception:
            await self._policy_service.cancel(result.request_id)
            raise

    async def list(self, context: TenantContext):
        return await self._workflow.list(context)

    async def get(self, context: TenantContext, asset_id: str):
        return await self._workflow.get(context, asset_id)

    async def update_outline(
        self,
        context: TenantContext,
        asset_id: str,
        outline: dict[str, object],
        expected_revision: int,
    ):
        return await self._workflow.update_outline(
            context,
            asset_id,
            outline,
            expected_revision,
        )

    async def confirm_outline(self, context: TenantContext, asset_id: str):
        return await self._workflow.confirm_outline(context, asset_id)

    async def cancel(self, context: TenantContext, asset_id: str):
        return await self._workflow.cancel(context, asset_id)

    async def list_approvals(self, context: TenantContext):
        approvals = await self._approval_service.list(context)
        return tuple(
            [await self._workflow.approval_response(approval) for approval in approvals]
        )

    async def approve(
        self,
        context: TenantContext,
        approval_id: str,
        _comment: str | None,
    ):
        approval = await self._approval_service.approve(context, approval_id)
        if approval.status != "approved":
            return await self._workflow.approval_response(approval)
        return await self._workflow.start_approved_generation(context, approval)

    async def reject(
        self,
        context: TenantContext,
        approval_id: str,
        _comment: str | None,
    ):
        approval = await self._approval_service.reject(context, approval_id)
        return await self._workflow.approval_response(approval)

    async def copy_to_teacher_draft(
        self,
        context: TenantContext,
        asset_id: str,
    ):
        return await self._workflow.copy_to_teacher_draft(context, asset_id)
