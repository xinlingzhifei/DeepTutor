"""Trusted runtime composition for private student classrooms."""

from __future__ import annotations

from typing import Protocol

from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelector
from deeptutor.teaching.processes import RuntimeCancellationGateway, RuntimeStoreProvider
from deeptutor.teaching.repositories.catalog import SqlAlchemyCatalogRepository
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
    "build_student_classroom_service",
    "resolve_student_class_id",
]
