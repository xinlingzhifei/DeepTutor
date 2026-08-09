"""Private student classroom and teacher approval API."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from deeptutor.api.routers.classroom_jobs import (
    get_cancellation_gateway,
    get_data_plane_selector,
    get_job_repository,
)
from deeptutor.api.routers.teaching_catalog import (
    get_source_repository,
    get_source_store_provider,
)
from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.quota import InsufficientQuota
from deeptutor.teaching.repositories.classrooms import (
    ClassroomAssetNotFoundError,
    ClassroomPersistenceError,
    SqlAlchemyClassroomRepository,
)
from deeptutor.teaching.repositories.jobs import IdempotencyConflict
from deeptutor.teaching.repositories.student_generation import (
    SqlAlchemyStudentGenerationRepository,
    SqlAlchemyStudentSafetyEvaluator,
    StudentGenerationConfigurationError,
)
from deeptutor.teaching.services.classrooms import (
    ClassroomAccessDenied,
    ClassroomConfirmationConflict,
    ClassroomIdempotencyConflict,
    ClassroomNotFound,
    ClassroomRevisionConflict,
    ClassroomService,
    ClassroomServiceError,
    InvalidClassroomState,
)
from deeptutor.teaching.services.student_classrooms import (
    SqlAlchemyStudentClassroomGeneration,
    SqlAlchemyStudentClassroomWorkflow,
    StudentClassroomConflict,
    StudentClassroomDenied,
    StudentClassroomNotFound,
    StudentClassroomService,
)
from deeptutor.teaching.services.student_generation import (
    StudentGenerationApprovalConflict,
    StudentGenerationApprovalNotFound,
    StudentGenerationApprovalService,
    StudentGenerationService,
)
from deeptutor.teaching.source_snapshots import (
    SourceAccessDenied,
    SourceSnapshotBuilder,
    SourceSnapshotUnavailable,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant

router = APIRouter()


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        populate_by_name=True,
    )


class StudentClassroomCreateRequest(_ApiModel):
    course_id: str = Field(min_length=1, max_length=64)
    class_id: str = Field(min_length=1, max_length=64)
    mode: Literal["micro", "full"]
    content_mode: Literal["source_grounded", "open_creation"]
    web_search_requested: bool = False
    source_type: Literal["knowledge_base", "pdf"] | None = None
    source_ref: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_source_selection(self):
        if self.content_mode == "open_creation":
            if self.source_type is not None or self.source_ref is not None:
                raise ValueError("open creation cannot select a source")
        elif self.source_type is None or self.source_ref is None:
            raise ValueError("source-grounded creation requires a source")
        return self


class StudentClassroomResponse(_ApiModel):
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
    outline: dict[str, Any] | None = None


class StudentClassroomListResponse(_ApiModel):
    items: list[StudentClassroomResponse]


class StudentGenerationEstimateResponse(_ApiModel):
    scene_range: tuple[int, int]
    duration_minutes_range: tuple[int, int]
    quota_units: int
    requires_outline_confirmation: bool
    requires_approval: bool


class StudentOutlineUpdateRequest(_ApiModel):
    outline: dict[str, Any]


class ApprovalDecisionRequest(_ApiModel):
    comment: str | None = Field(default=None, max_length=2000)


class StudentGenerationApprovalResponse(_ApiModel):
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


class StudentGenerationApprovalListResponse(_ApiModel):
    items: list[StudentGenerationApprovalResponse]


class TeacherDraftCopyResponse(_ApiModel):
    asset_id: str
    draft_id: str
    source_student_asset_id: str
    owner_id: str
    status: str
    revision: int


class StudentClassroomServiceLike(Protocol):
    async def estimate(
        self,
        context: TenantContext,
        request: StudentClassroomCreateRequest,
    ): ...

    async def create(
        self,
        context: TenantContext,
        request: StudentClassroomCreateRequest,
    ): ...

    async def list(self, context: TenantContext): ...

    async def get(self, context: TenantContext, asset_id: str): ...

    async def update_outline(
        self,
        context: TenantContext,
        asset_id: str,
        outline: dict[str, Any],
        expected_revision: int,
    ): ...

    async def confirm_outline(self, context: TenantContext, asset_id: str): ...

    async def cancel(self, context: TenantContext, asset_id: str): ...

    async def list_approvals(self, context: TenantContext): ...

    async def approve(
        self,
        context: TenantContext,
        approval_id: str,
        comment: str | None,
    ): ...

    async def reject(
        self,
        context: TenantContext,
        approval_id: str,
        comment: str | None,
    ): ...

    async def copy_to_teacher_draft(
        self,
        context: TenantContext,
        asset_id: str,
    ): ...


def get_student_safety_evaluator(
    context: TenantContext = Depends(require_tenant),
) -> SqlAlchemyStudentSafetyEvaluator:
    return SqlAlchemyStudentSafetyEvaluator(
        get_platform_engine(),
        context.tenant_id,
    )


def get_student_generation_repository(
    context: TenantContext = Depends(require_tenant),
    safety_evaluator=Depends(get_student_safety_evaluator),
) -> SqlAlchemyStudentGenerationRepository:
    return SqlAlchemyStudentGenerationRepository(
        get_platform_engine(),
        context.tenant_id,
        safety_evaluator=safety_evaluator,
    )


def get_student_classroom_repository(
    context: TenantContext = Depends(require_tenant),
) -> SqlAlchemyClassroomRepository:
    return SqlAlchemyClassroomRepository(get_platform_engine(), context.tenant_id)


def get_student_classroom_service(
    context: TenantContext = Depends(require_tenant),
    request_repository=Depends(get_student_generation_repository),
    classroom_repository=Depends(get_student_classroom_repository),
    source_repository=Depends(get_source_repository),
    store_provider=Depends(get_source_store_provider),
    job_repository=Depends(get_job_repository),
    data_plane_selector=Depends(get_data_plane_selector),
    cancellation_gateway=Depends(get_cancellation_gateway),
) -> StudentClassroomService:
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


def _response(record: object) -> StudentClassroomResponse:
    return StudentClassroomResponse.model_validate(record, from_attributes=True)


def _required(record: object | None) -> object:
    if record is None:
        raise HTTPException(status_code=404, detail="Student classroom not found")
    return record


def _parse_if_match(value: str | None) -> int:
    prefix = '"revision-'
    if (
        value is None
        or not value.startswith(prefix)
        or not value.endswith('"')
        or not value[len(prefix) : -1].isdigit()
    ):
        raise HTTPException(status_code=400, detail="If-Match is invalid")
    revision = int(value[len(prefix) : -1])
    if revision < 1:
        raise HTTPException(status_code=400, detail="If-Match is invalid")
    return revision


async def _call(operation):
    try:
        return await operation
    except (
        StudentClassroomNotFound,
        StudentGenerationApprovalNotFound,
        ClassroomNotFound,
        ClassroomAssetNotFoundError,
        ClassroomAccessDenied,
        SourceAccessDenied,
    ):
        raise HTTPException(status_code=404, detail="Student classroom not found") from None
    except StudentClassroomDenied:
        raise HTTPException(status_code=403, detail="Student generation is denied") from None
    except (
        StudentClassroomConflict,
        StudentGenerationApprovalConflict,
        ClassroomRevisionConflict,
        ClassroomConfirmationConflict,
        ClassroomIdempotencyConflict,
        IdempotencyConflict,
        InsufficientQuota,
        InvalidClassroomState,
    ):
        raise HTTPException(status_code=409, detail="Student classroom conflicts") from None
    except (
        ClassroomPersistenceError,
        DataPlaneBindingUnavailable,
        SourceSnapshotUnavailable,
        StudentGenerationConfigurationError,
        SQLAlchemyError,
    ):
        raise HTTPException(
            status_code=503,
            detail="Student classroom service is unavailable",
        ) from None
    except (ClassroomServiceError, ValueError):
        raise HTTPException(status_code=422, detail="Student classroom request is invalid") from None


@router.post(
    "/student-classrooms/estimate",
    response_model=StudentGenerationEstimateResponse,
)
async def estimate_student_classroom(
    request: StudentClassroomCreateRequest,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentGenerationEstimateResponse:
    return StudentGenerationEstimateResponse.model_validate(
        await _call(service.estimate(context, request)),
        from_attributes=True,
    )


@router.post(
    "/student-classrooms",
    response_model=StudentClassroomResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_student_classroom(
    request: StudentClassroomCreateRequest,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentClassroomResponse:
    return _response(await _call(service.create(context, request)))


@router.get(
    "/student-classrooms",
    response_model=StudentClassroomListResponse,
)
async def list_student_classrooms(
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentClassroomListResponse:
    records = await _call(service.list(context))
    return StudentClassroomListResponse(items=[_response(record) for record in records])


@router.get(
    "/student-classrooms/{asset_id}",
    response_model=StudentClassroomResponse,
)
async def get_student_classroom(
    asset_id: str,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentClassroomResponse:
    return _response(_required(await _call(service.get(context, asset_id))))


@router.put(
    "/student-classrooms/{asset_id}/outline",
    response_model=StudentClassroomResponse,
)
async def update_student_classroom_outline(
    asset_id: str,
    request: StudentOutlineUpdateRequest,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentClassroomResponse:
    return _response(
        _required(
            await _call(
                service.update_outline(
                    context,
                    asset_id,
                    request.outline,
                    _parse_if_match(if_match),
                )
            )
        )
    )


@router.post(
    "/student-classrooms/{asset_id}/confirm-outline",
    response_model=StudentClassroomResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_student_classroom_outline(
    asset_id: str,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentClassroomResponse:
    return _response(
        _required(await _call(service.confirm_outline(context, asset_id)))
    )


@router.post(
    "/student-classrooms/{asset_id}/cancel",
    response_model=StudentClassroomResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_student_classroom(
    asset_id: str,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentClassroomResponse:
    return _response(_required(await _call(service.cancel(context, asset_id))))


@router.get(
    "/student-generation-approvals",
    response_model=StudentGenerationApprovalListResponse,
)
async def list_student_generation_approvals(
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentGenerationApprovalListResponse:
    records = await _call(service.list_approvals(context))
    return StudentGenerationApprovalListResponse(
        items=[
            StudentGenerationApprovalResponse.model_validate(item, from_attributes=True)
            for item in records
        ]
    )


async def _decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    context: TenantContext,
    service: StudentClassroomServiceLike,
    decision: Literal["approve", "reject"],
) -> StudentGenerationApprovalResponse:
    operation = getattr(service, decision)(context, approval_id, request.comment)
    record = await _call(operation)
    if record is None:
        raise HTTPException(status_code=404, detail="Student generation approval not found")
    return StudentGenerationApprovalResponse.model_validate(record, from_attributes=True)


@router.post(
    "/student-generation-approvals/{approval_id}/approve",
    response_model=StudentGenerationApprovalResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_student_generation(
    approval_id: str,
    request: ApprovalDecisionRequest,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentGenerationApprovalResponse:
    return await _decide_approval(approval_id, request, context, service, "approve")


@router.post(
    "/student-generation-approvals/{approval_id}/reject",
    response_model=StudentGenerationApprovalResponse,
)
async def reject_student_generation(
    approval_id: str,
    request: ApprovalDecisionRequest,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> StudentGenerationApprovalResponse:
    return await _decide_approval(approval_id, request, context, service, "reject")


@router.post(
    "/student-classrooms/{asset_id}/copy-to-teacher-draft",
    response_model=TeacherDraftCopyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def copy_student_classroom_to_teacher_draft(
    asset_id: str,
    context: TenantContext = Depends(require_tenant),
    service: StudentClassroomServiceLike = Depends(get_student_classroom_service),
) -> TeacherDraftCopyResponse:
    record = await _call(service.copy_to_teacher_draft(context, asset_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Student classroom not found")
    return TeacherDraftCopyResponse.model_validate(record, from_attributes=True)
