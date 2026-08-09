"""Trusted runtime composition for private student classrooms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelector
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.processes import RuntimeCancellationGateway, RuntimeStoreProvider
from deeptutor.teaching.repositories.catalog import (
    SqlAlchemyCatalogRepository,
    StudentClassroomOptionBinding,
)
from deeptutor.teaching.repositories.classrooms import SqlAlchemyClassroomRepository
from deeptutor.teaching.repositories.data_planes import SqlAlchemyDataPlaneRepository
from deeptutor.teaching.repositories.jobs import SqlAlchemyGenerationJobRepository
from deeptutor.teaching.repositories.sources import SqlAlchemySourceRepository
from deeptutor.teaching.repositories.student_generation import (
    SqlAlchemyStudentGenerationRepository,
    SqlAlchemyStudentSafetyEvaluator,
)
from deeptutor.teaching.services.classrooms import ClassroomService
from deeptutor.teaching.services.student_classrooms import (
    SqlAlchemyStudentClassroomGeneration,
    SqlAlchemyStudentClassroomWorkflow,
    StudentClassroomService,
)
from deeptutor.teaching.services.student_generation import (
    StudentGenerationApprovalService,
    StudentGenerationService,
)
from deeptutor.teaching.source_snapshots import SourceSnapshotBuilder
from deeptutor.teaching.tenant_context import TenantContext


class StudentClassroomBindingUnavailable(RuntimeError):
    """The current learner does not have one unambiguous active class."""


class _CatalogBindingRepository(Protocol):
    async def list_active_enrollment_class_ids(
        self,
        course_id: str,
        learner_id: str,
    ) -> tuple[str, ...]: ...


class _StudentClassroomOptionsRepository(Protocol):
    async def list_student_classroom_option_bindings(
        self,
        learner_id: str,
    ) -> tuple[StudentClassroomOptionBinding, ...]: ...


@dataclass(frozen=True, slots=True)
class StudentClassroomOption:
    course_id: str
    title: str
    allowed_modes: tuple[Literal["micro", "full"], ...]
    allowed_content_modes: tuple[
        Literal["source_grounded", "open_creation"], ...
    ]


_CONTENT_MODE_ENCODINGS = {
    "source_grounded": ("source_grounded",),
    "open_creation": ("open_creation",),
    "source_grounded,open_creation": ("source_grounded", "open_creation"),
}


class StudentClassroomOptionsService:
    """Expose only unambiguous, policy-backed courses for the current learner."""

    def __init__(self, repository: _StudentClassroomOptionsRepository) -> None:
        self._repository = repository

    async def list(self, context: TenantContext) -> tuple[StudentClassroomOption, ...]:
        bindings = await self._repository.list_student_classroom_option_bindings(
            context.user_id
        )
        grouped: dict[str, list[StudentClassroomOptionBinding]] = {}
        for binding in bindings:
            grouped.setdefault(binding.course_id, []).append(binding)

        options: list[StudentClassroomOption] = []
        for course_bindings in grouped.values():
            if len(course_bindings) != 1:
                continue
            binding = course_bindings[0]
            content_modes = _CONTENT_MODE_ENCODINGS.get(binding.allowed_content_modes)
            if content_modes is None:
                continue
            resource = ResourceScope(
                tenant_id=context.tenant_id,
                course_id=binding.course_id,
                class_id=binding.class_id,
            )
            allowed_modes: list[Literal["micro", "full"]] = []
            if binding.allow_student_micro and any(
                permission.allows_resource("classroom.generate.micro", resource)
                for permission in context.permissions
            ):
                allowed_modes.append("micro")
            if binding.allow_student_full and any(
                permission.allows_resource("classroom.generate.full", resource)
                for permission in context.permissions
            ):
                allowed_modes.append("full")
            if not allowed_modes:
                continue
            options.append(
                StudentClassroomOption(
                    course_id=binding.course_id,
                    title=binding.title,
                    allowed_modes=tuple(allowed_modes),
                    allowed_content_modes=content_modes,
                )
            )
        return tuple(options)


async def resolve_student_class_id(
    context: TenantContext,
    course_id: str,
    *,
    repository: _CatalogBindingRepository | None = None,
) -> str:
    """Resolve one server-trusted class binding from active enrollment state."""

    selected_repository = repository or SqlAlchemyCatalogRepository(context.tenant_id)
    class_ids = await selected_repository.list_active_enrollment_class_ids(
        course_id,
        context.user_id,
    )
    if len(class_ids) != 1:
        raise StudentClassroomBindingUnavailable(
            "student classroom requires exactly one active class enrollment"
        )
    return class_ids[0]


def build_student_classroom_service(
    context: TenantContext,
    *,
    request_repository=None,
    classroom_repository=None,
    source_repository=None,
    store_provider=None,
    job_repository=None,
    data_plane_selector=None,
    cancellation_gateway=None,
) -> StudentClassroomService:
    """Compose the Task 2 policy/workflow service without importing API routers."""

    engine = None
    settings = None

    def runtime_engine():
        nonlocal engine
        if engine is None:
            engine = get_platform_engine()
        return engine

    def runtime_settings():
        nonlocal settings
        if settings is None:
            settings = load_platform_settings()
        return settings

    if request_repository is None:
        safety_evaluator = SqlAlchemyStudentSafetyEvaluator(
            runtime_engine(),
            context.tenant_id,
        )
        request_repository = SqlAlchemyStudentGenerationRepository(
            runtime_engine(),
            context.tenant_id,
            safety_evaluator=safety_evaluator,
        )
    if classroom_repository is None:
        classroom_repository = SqlAlchemyClassroomRepository(
            runtime_engine(),
            context.tenant_id,
        )
    if source_repository is None:
        source_repository = SqlAlchemySourceRepository(
            context.tenant_id,
            runtime_engine(),
        )
    if store_provider is None:
        store_provider = RuntimeStoreProvider(runtime_settings())
    if job_repository is None:
        job_repository = SqlAlchemyGenerationJobRepository(runtime_engine())
    if data_plane_selector is None:
        data_plane_selector = DataPlaneSelector(
            settings=runtime_settings(),
            repository=SqlAlchemyDataPlaneRepository(),
        )
    if cancellation_gateway is None:
        cancellation_gateway = RuntimeCancellationGateway(runtime_settings())

    snapshots = SourceSnapshotBuilder(
        context,
        source_repository,
        store_provider=store_provider,
    )
    brief_builder = TeachingBriefBuilder(context, snapshots)
    generation = SqlAlchemyStudentClassroomGeneration(
        job_repository,
        data_plane_selector,
        cancellation_gateway,
    )
    classroom_service = ClassroomService(
        classroom_repository,
        brief_builder,
        generation,
        store_provider,
        student_owner_only=True,
    )
    workflow = SqlAlchemyStudentClassroomWorkflow(
        repository=classroom_repository,
        classroom_service=classroom_service,
        brief_builder=brief_builder,
        generation=generation,
        request_repository=request_repository,
    )
    return StudentClassroomService(
        policy_service=StudentGenerationService(
            tenant_id=context.tenant_id,
            learner_id=context.user_id,
            repository=request_repository,
        ),
        workflow=workflow,
        approval_service=StudentGenerationApprovalService(
            tenant_id=context.tenant_id,
            repository=request_repository,
        ),
    )


__all__ = [
    "StudentClassroomBindingUnavailable",
    "StudentClassroomOption",
    "StudentClassroomOptionsService",
    "build_student_classroom_service",
    "resolve_student_class_id",
]
