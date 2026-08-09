"""Capability adapter for private student classroom generation."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from typing import Callable, Literal, Protocol

from deeptutor.agents._shared.capability_result import emit_capability_result
from deeptutor.agents.interactive_classroom.request_config import (
    InteractiveClassroomRequestConfig,
    validate_interactive_classroom_request_config,
)
from deeptutor.core.capability_protocol import BaseCapability, CapabilityManifest
from deeptutor.core.context import UnifiedContext
from deeptutor.core.stream_bus import StreamBus
from deeptutor.i18n import StatusI18n
from deeptutor.runtime.request_contracts import get_capability_request_schema
from deeptutor.teaching.policies.student_generation import StudentGenerationEstimate
from deeptutor.teaching.services.student_classroom_runtime import (
    build_student_classroom_service,
    resolve_student_class_id,
)
from deeptutor.teaching.services.student_classrooms import (
    StudentClassroomDenied,
    StudentClassroomView,
)
from deeptutor.teaching.tenant_context import (
    TenantContext,
    resolve_runtime_tenant_context,
)


class StudentClassroomServiceLike(Protocol):
    async def estimate(
        self,
        context: TenantContext,
        request: object,
    ) -> StudentGenerationEstimate: ...

    async def create(
        self,
        context: TenantContext,
        request: object,
    ) -> StudentClassroomView: ...


ServiceFactory = Callable[[TenantContext], StudentClassroomServiceLike]
ClassResolver = Callable[[TenantContext, str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class _StudentClassroomCommand:
    course_id: str
    class_id: str
    mode: Literal["micro", "full"]
    content_mode: Literal["source_grounded", "open_creation"]
    web_search_requested: bool
    source_type: Literal["knowledge_base"] | None
    source_ref: str | None
    title: str
    objective: str


_ZERO_COST_SUMMARY = {
    "total_cost_usd": 0.0,
    "total_tokens": 0,
    "total_calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
}


class InteractiveClassroomCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="interactive_classroom",
        description="Generate a private interactive classroom for the current course.",
        stages=["policy_check", "briefing", "outline", "queued"],
        tools_used=["rag"],
        cli_aliases=["classroom"],
        request_schema=get_capability_request_schema("interactive_classroom"),
    )

    def __init__(
        self,
        *,
        service: StudentClassroomServiceLike | None = None,
        service_factory: ServiceFactory | None = None,
        class_resolver: ClassResolver | None = None,
    ) -> None:
        if service is not None and service_factory is not None:
            raise ValueError("provide either service or service_factory")
        self._service = service
        self._service_factory = service_factory or build_student_classroom_service
        self._class_resolver = class_resolver or resolve_student_class_id

    def _resolve_service(self, context: TenantContext) -> StudentClassroomServiceLike:
        return self._service or self._service_factory(context)

    @staticmethod
    def _command(
        context: UnifiedContext,
        config: InteractiveClassroomRequestConfig,
        class_id: str,
    ) -> _StudentClassroomCommand:
        class_id = class_id.strip()
        if not class_id or len(class_id) > 64:
            raise ValueError("trusted classroom class_id is unavailable")

        question = config.question.strip() or context.user_message.strip()
        if len(question) > 4000:
            raise ValueError("question cannot exceed 4000 characters")
        source_ref = (
            context.knowledge_bases[0]
            if config.content_mode == "source_grounded" and context.knowledge_bases
            else None
        )
        if config.content_mode == "source_grounded" and not source_ref:
            raise ValueError("source-grounded classroom requires a knowledge base")

        return _StudentClassroomCommand(
            course_id=config.course_id,
            class_id=class_id,
            mode=config.mode,
            content_mode=config.content_mode,
            web_search_requested=False,
            source_type="knowledge_base" if source_ref is not None else None,
            source_ref=source_ref,
            title=question[:255] or "Student classroom",
            objective=question or "Student classroom",
        )

    async def run(self, context: UnifiedContext, stream: StreamBus) -> None:
        tenant_context = resolve_runtime_tenant_context()
        config = validate_interactive_classroom_request_config(context.config_overrides)
        class_id = await self._class_resolver(tenant_context, config.course_id)
        service = self._resolve_service(tenant_context)
        request = self._command(context, config, class_id)
        i18n = StatusI18n(self.name, context.language, module="interactive_classroom")

        async with stream.stage("policy_check", source=self.name):
            await stream.progress(
                message=i18n.t("policy_checking", "Checking classroom generation policy."),
                source=self.name,
                stage="policy_check",
            )
            estimate = await service.estimate(tenant_context, request)

        try:
            async with stream.stage("briefing", source=self.name):
                await stream.progress(
                    message=i18n.t(
                        "building_grounded_brief",
                        "Building a source-grounded teaching brief.",
                    ),
                    source=self.name,
                    stage="briefing",
                )
                classroom = await service.create(tenant_context, request)
        except StudentClassroomDenied:
            await stream.progress(
                message=i18n.t(
                    "generation_denied",
                    "Generation is not allowed for this request.",
                ),
                source=self.name,
                stage="policy_check",
            )
            raise

        if request.mode == "full":
            async with stream.stage("outline", source=self.name):
                outline_key = (
                    "awaiting_outline_confirmation"
                    if classroom.outline is not None
                    or classroom.status == "awaiting_outline_confirmation"
                    else "outline_queued"
                )
                outline_default = (
                    "The classroom outline is awaiting your confirmation."
                    if outline_key == "awaiting_outline_confirmation"
                    else "The classroom outline has been queued."
                )
                await stream.progress(
                    message=i18n.t(outline_key, outline_default),
                    source=self.name,
                    stage="outline",
                )

        async with stream.stage("queued", source=self.name):
            status_key, status_default = self._queued_status(request, classroom)
            await stream.progress(
                message=i18n.t(status_key, status_default),
                source=self.name,
                stage="queued",
            )

        await emit_capability_result(
            stream,
            {
                "response": i18n.t(status_key, status_default),
                "estimate": asdict(estimate),
                "approval_id": classroom.approval_id,
                "job_id": classroom.generation_job_id,
                "outline": classroom.outline,
                "classroom": asdict(classroom),
                "metadata": {"cost_summary": dict(_ZERO_COST_SUMMARY)},
            },
            source=self.name,
        )

    @staticmethod
    def _queued_status(
        request: _StudentClassroomCommand,
        classroom: StudentClassroomView,
    ) -> tuple[str, str]:
        if classroom.approval_id is not None or classroom.status == "awaiting_approval":
            return "awaiting_approval", "The request is awaiting teacher approval."
        if request.mode == "micro":
            return "micro_queued", "The micro-classroom has been queued."
        if classroom.outline is not None or classroom.status == "awaiting_outline_confirmation":
            return (
                "awaiting_outline_confirmation",
                "The classroom outline is awaiting your confirmation.",
            )
        return "outline_queued", "The classroom outline has been queued."


__all__ = ["InteractiveClassroomCapability", "StudentClassroomServiceLike"]
