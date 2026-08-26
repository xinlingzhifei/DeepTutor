"""Scoped teaching catalog and source APIs."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from deeptutor.multi_user.knowledge_access import resolve_kb
from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.object_store import ObjectStoreError
from deeptutor.teaching.policies.student_generation import ContentMode
from deeptutor.teaching.processes import RuntimeStoreProvider
from deeptutor.teaching.repositories.catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    ClassRecord,
    CourseGenerationPolicyView,
    CourseRecord,
    EnrollmentRecord,
    SqlAlchemyCatalogRepository,
)
from deeptutor.teaching.repositories.sources import (
    SourceConflictError,
    SourceNotFoundError,
    SourceRecord,
    SqlAlchemySourceRepository,
)
from deeptutor.teaching.services.catalog import CatalogAccessDeniedError, CatalogService
from deeptutor.teaching.services.sources import (
    InvalidPdfSourceError,
    InvalidSourceBindingError,
    SourceAccessDeniedError,
    SourceService,
    SourceUploadTooLargeError,
    UnsupportedSourceMediaError,
    knowledge_resource_exists,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant

router = APIRouter()
_T = TypeVar("_T")
_CONTENT_MODE_ORDER: tuple[ContentMode, ...] = ("source_grounded", "open_creation")


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        populate_by_name=True,
    )


class CreateCourseRequest(_ApiModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    title: str = Field(min_length=1, max_length=255)


class CreateClassRequest(_ApiModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=255)


class EnrollmentRequest(_ApiModel):
    user_id: str = Field(min_length=1, max_length=128)


class ReplaceCourseGenerationPolicyRequest(_ApiModel):
    allow_student_micro: bool = Field(strict=True)
    allow_student_full: bool = Field(strict=True)
    allowed_content_modes: list[ContentMode] = Field(min_length=1)
    allow_web_search: bool = Field(strict=True)
    require_approval_for_restricted_topics: bool = Field(strict=True)
    minor_safety_mode: bool = Field(strict=True)
    micro_scene_limit: int = Field(ge=1, le=5, strict=True)
    full_scene_limit: int = Field(ge=1, le=24, strict=True)
    daily_student_units: int = Field(ge=0, strict=True)
    monthly_student_units: int = Field(ge=0, strict=True)

    @field_validator("allowed_content_modes")
    @classmethod
    def _content_modes_are_unique(cls, value: list[ContentMode]) -> list[ContentMode]:
        if len(value) != len(set(value)):
            raise ValueError("allowedContentModes must contain unique values")
        return value


class BindKnowledgeRequest(_ApiModel):
    knowledge_resource_id: str = Field(min_length=1, max_length=256)
    course_id: str = Field(min_length=1, max_length=64)
    class_id: str | None = Field(default=None, min_length=1, max_length=64)


class CourseResponse(_ApiModel):
    id: str
    title: str
    status: str
    created_at: datetime | None = None


class ClassResponse(_ApiModel):
    id: str
    course_id: str
    name: str
    status: str
    created_at: datetime | None = None


class EnrollmentResponse(_ApiModel):
    class_id: str
    user_id: str
    status: str
    created_at: datetime | None = None


class CourseGenerationPolicyResponse(_ApiModel):
    tenant_id: str
    course_id: str
    allow_student_micro: bool
    allow_student_full: bool
    allowed_content_modes: list[ContentMode]
    allow_web_search: bool
    require_approval_for_restricted_topics: bool
    minor_safety_mode: bool
    micro_scene_limit: int
    full_scene_limit: int
    daily_student_units: int
    monthly_student_units: int
    updated_by: str
    updated_at: datetime


class SourceResponse(_ApiModel):
    binding_id: str
    source_type: str
    source_id: str
    filename: str | None
    sha256: str
    size_bytes: int | None
    course_id: str | None
    class_id: str | None
    created_at: datetime | None = None


class CourseListResponse(_ApiModel):
    items: list[CourseResponse]


class ClassListResponse(_ApiModel):
    items: list[ClassResponse]


class EnrollmentListResponse(_ApiModel):
    items: list[EnrollmentResponse]


class SourceListResponse(_ApiModel):
    items: list[SourceResponse]


def get_catalog_repository(
    context: TenantContext = Depends(require_tenant),
) -> SqlAlchemyCatalogRepository:
    return SqlAlchemyCatalogRepository(context.tenant_id)


def get_source_repository(
    context: TenantContext = Depends(require_tenant),
) -> SqlAlchemySourceRepository:
    return SqlAlchemySourceRepository(context.tenant_id)


def get_source_store_provider() -> RuntimeStoreProvider:
    return RuntimeStoreProvider(load_platform_settings())


def get_knowledge_resolver():
    return resolve_kb


def get_knowledge_resource_exists():
    return knowledge_resource_exists


def get_catalog_service(
    repository=Depends(get_catalog_repository),
) -> CatalogService:
    return CatalogService(repository)


def get_source_service(
    repository=Depends(get_source_repository),
    store_provider=Depends(get_source_store_provider),
    knowledge_resolver=Depends(get_knowledge_resolver),
    knowledge_exists=Depends(get_knowledge_resource_exists),
) -> SourceService:
    return SourceService(repository, store_provider, knowledge_resolver, knowledge_exists)


def _course_response(record: CourseRecord) -> CourseResponse:
    return CourseResponse.model_validate(record, from_attributes=True)


def _class_response(record: ClassRecord) -> ClassResponse:
    return ClassResponse.model_validate(record, from_attributes=True)


def _enrollment_response(record: EnrollmentRecord) -> EnrollmentResponse:
    return EnrollmentResponse(
        class_id=record.class_id,
        user_id=record.learner_id,
        status=record.status,
        created_at=record.created_at,
    )


def _course_generation_policy_response(
    record: CourseGenerationPolicyView,
) -> CourseGenerationPolicyResponse:
    return CourseGenerationPolicyResponse(
        tenant_id=record.tenant_id,
        course_id=record.course_id,
        allow_student_micro=record.allow_student_micro,
        allow_student_full=record.allow_student_full,
        allowed_content_modes=[
            mode for mode in _CONTENT_MODE_ORDER if mode in record.allowed_content_modes
        ],
        allow_web_search=record.allow_web_search,
        require_approval_for_restricted_topics=record.require_approval_for_restricted_topics,
        minor_safety_mode=record.minor_safety_mode,
        micro_scene_limit=record.micro_scene_limit,
        full_scene_limit=record.full_scene_limit,
        daily_student_units=record.daily_student_units,
        monthly_student_units=record.monthly_student_units,
        updated_by=record.updated_by,
        updated_at=record.updated_at,
    )


def _source_response(record: SourceRecord) -> SourceResponse:
    return SourceResponse.model_validate(record, from_attributes=True)


def _raise_catalog_error(exc: Exception) -> None:
    if isinstance(exc, CatalogAccessDeniedError):
        raise HTTPException(status_code=403, detail="Catalog access denied") from exc
    if isinstance(exc, CatalogNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, CatalogConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, SQLAlchemyError):
        raise HTTPException(status_code=503, detail="Catalog persistence is unavailable") from exc
    raise exc


def _raise_source_error(exc: Exception) -> None:
    if isinstance(exc, SourceAccessDeniedError):
        raise HTTPException(status_code=403, detail="Source access denied") from exc
    if isinstance(exc, SourceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, SourceConflictError):
        raise HTTPException(status_code=409, detail="Source conflicts with existing state") from exc
    if isinstance(exc, SourceUploadTooLargeError):
        raise HTTPException(status_code=413, detail="PDF exceeds the 100 MiB limit") from exc
    if isinstance(exc, UnsupportedSourceMediaError):
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    if isinstance(exc, InvalidPdfSourceError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, InvalidSourceBindingError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, ObjectStoreError):
        raise HTTPException(status_code=503, detail="Source storage is unavailable") from exc
    if isinstance(exc, SQLAlchemyError):
        raise HTTPException(status_code=503, detail="Source persistence is unavailable") from exc
    raise exc


async def _catalog_result(operation: Awaitable[_T]) -> _T:
    try:
        return await operation
    except Exception as exc:
        _raise_catalog_error(exc)
        raise AssertionError("unreachable") from exc


async def _source_result(operation: Awaitable[_T]) -> _T:
    try:
        return await operation
    except Exception as exc:
        _raise_source_error(exc)
        raise AssertionError("unreachable") from exc


@router.get("/courses", response_model=CourseListResponse)
async def list_courses(
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> CourseListResponse:
    records = await _catalog_result(service.list_courses(context))
    return CourseListResponse(items=[_course_response(record) for record in records])


@router.post("/courses", response_model=CourseResponse, status_code=status.HTTP_201_CREATED)
async def create_course(
    request: CreateCourseRequest,
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> CourseResponse:
    record = await _catalog_result(
        service.create_course(
            context,
            course_id=request.id,
            title=request.title,
        )
    )
    return _course_response(record)


@router.get(
    "/courses/{course_id}/generation-policy",
    response_model=CourseGenerationPolicyResponse,
)
async def get_course_generation_policy(
    course_id: str,
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> CourseGenerationPolicyResponse:
    record = await _catalog_result(
        service.get_course_generation_policy(context, course_id=course_id)
    )
    return _course_generation_policy_response(record)


@router.put(
    "/courses/{course_id}/generation-policy",
    response_model=CourseGenerationPolicyResponse,
)
async def replace_course_generation_policy(
    course_id: str,
    request: ReplaceCourseGenerationPolicyRequest,
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> CourseGenerationPolicyResponse:
    record = await _catalog_result(
        service.replace_course_generation_policy(
            context,
            course_id=course_id,
            allow_student_micro=request.allow_student_micro,
            allow_student_full=request.allow_student_full,
            allowed_content_modes=frozenset(request.allowed_content_modes),
            allow_web_search=request.allow_web_search,
            require_approval_for_restricted_topics=(request.require_approval_for_restricted_topics),
            minor_safety_mode=request.minor_safety_mode,
            micro_scene_limit=request.micro_scene_limit,
            full_scene_limit=request.full_scene_limit,
            daily_student_units=request.daily_student_units,
            monthly_student_units=request.monthly_student_units,
        )
    )
    return _course_generation_policy_response(record)


@router.get("/courses/{course_id}/classes", response_model=ClassListResponse)
async def list_classes(
    course_id: str,
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> ClassListResponse:
    records = await _catalog_result(service.list_classes(context, course_id=course_id))
    return ClassListResponse(items=[_class_response(record) for record in records])


@router.post(
    "/courses/{course_id}/classes",
    response_model=ClassResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_class(
    course_id: str,
    request: CreateClassRequest,
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> ClassResponse:
    record = await _catalog_result(
        service.create_class(
            context,
            course_id=course_id,
            class_id=request.id,
            name=request.name,
        )
    )
    return _class_response(record)


@router.get("/classes/{class_id}/enrollments", response_model=EnrollmentListResponse)
async def list_enrollments(
    class_id: str,
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> EnrollmentListResponse:
    records = await _catalog_result(service.list_enrollments(context, class_id=class_id))
    return EnrollmentListResponse(items=[_enrollment_response(record) for record in records])


@router.post(
    "/classes/{class_id}/enrollments",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_enrollment(
    class_id: str,
    request: EnrollmentRequest,
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> EnrollmentResponse:
    record = await _catalog_result(
        service.add_enrollment(
            context,
            class_id=class_id,
            learner_id=request.user_id,
        )
    )
    return _enrollment_response(record)


@router.delete(
    "/classes/{class_id}/enrollments/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_enrollment(
    class_id: str,
    user_id: str,
    context: TenantContext = Depends(require_tenant),
    service: CatalogService = Depends(get_catalog_service),
) -> Response:
    await _catalog_result(
        service.remove_enrollment(
            context,
            class_id=class_id,
            learner_id=user_id,
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sources", response_model=SourceListResponse)
async def list_sources(
    context: TenantContext = Depends(require_tenant),
    service: SourceService = Depends(get_source_service),
) -> SourceListResponse:
    records = await _source_result(service.list_sources(context))
    return SourceListResponse(items=[_source_response(record) for record in records])


@router.post(
    "/sources/pdf",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_pdf_source(
    file: Annotated[UploadFile, File()],
    course_id: Annotated[str, Form(alias="courseId", min_length=1, max_length=64)],
    class_id: Annotated[
        str | None,
        Form(alias="classId", min_length=1, max_length=64),
    ] = None,
    context: TenantContext = Depends(require_tenant),
    service: SourceService = Depends(get_source_service),
) -> SourceResponse:
    record = await _source_result(
        service.upload_pdf(
            context,
            upload=file,
            course_id=course_id,
            class_id=class_id,
        )
    )
    return _source_response(record)


@router.post(
    "/sources/bind",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bind_knowledge_source(
    request: BindKnowledgeRequest,
    context: TenantContext = Depends(require_tenant),
    service: SourceService = Depends(get_source_service),
) -> SourceResponse:
    record = await _source_result(
        service.bind_knowledge_resource(
            context,
            knowledge_resource_id=request.knowledge_resource_id,
            course_id=request.course_id,
            class_id=request.class_id,
        )
    )
    return _source_response(record)


@router.delete("/sources/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    binding_id: str,
    context: TenantContext = Depends(require_tenant),
    service: SourceService = Depends(get_source_service),
) -> Response:
    await _source_result(service.delete_source(context, binding_id=binding_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = [
    "get_catalog_repository",
    "get_catalog_service",
    "get_knowledge_resolver",
    "get_knowledge_resource_exists",
    "get_source_repository",
    "get_source_service",
    "get_source_store_provider",
    "router",
]
