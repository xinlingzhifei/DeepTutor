"""Teacher-facing two-stage classroom authoring API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from deeptutor.api.routers.classroom_jobs import (
    get_data_plane_selector,
    get_job_repository,
)
from deeptutor.api.routers.teaching_catalog import (
    get_source_repository,
    get_source_store_provider,
)
from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.repositories.classrooms import (
    ClassroomPersistenceError,
    SqlAlchemyClassroomRepository,
)
from deeptutor.teaching.services.classrooms import (
    ClassroomAccessDenied,
    ClassroomConfirmationConflict,
    ClassroomIdempotencyConflict,
    ClassroomNotFound,
    ClassroomRevisionConflict,
    ClassroomService,
    ClassroomServiceError,
    DraftMediaContent,
    InvalidClassroomState,
    InvalidDraftDocument,
    InvalidDraftMedia,
    SqlAlchemyClassroomGeneration,
)
from deeptutor.teaching.source_snapshots import SourceSnapshotBuilder
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


class KnowledgePointRequest(_ApiModel):
    knowledge_point_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=4000)


class CreateClassroomRequest(_ApiModel):
    title: str = Field(min_length=1, max_length=255)
    course_id: str = Field(min_length=1, max_length=64)
    class_id: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=4000)
    grade_band: str = Field(min_length=1, max_length=128)
    audience: str = Field(min_length=1, max_length=128)
    duration_minutes: int = Field(ge=1, le=600)
    classroom_mode: Literal["full"]
    web_policy: Literal["disabled", "enabled"]
    allowed_web_domains: list[str] = Field(default_factory=list, max_length=32)
    template_id: str = Field(min_length=1, max_length=128)
    template_version: str = Field(min_length=1, max_length=64)
    knowledge_points: list[KnowledgePointRequest] = Field(min_length=1, max_length=100)
    content_mode: Literal["source_grounded", "open_creation"]
    open_creation_acknowledged: bool = False
    source_type: Literal["knowledge_base", "pdf"] | None = None
    source_ref: str | None = Field(default=None, min_length=1, max_length=256)
    requested_exports: list[
        Literal["classroom_zip", "pptx", "offline_html", "mp4"]
    ] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_source_and_network_policy(self):
        if self.web_policy == "disabled" and self.allowed_web_domains:
            raise ValueError("disabled web policy cannot allow domains")
        if self.content_mode == "open_creation":
            if not self.open_creation_acknowledged:
                raise ValueError("open creation requires acknowledgement")
            if self.source_type is not None or self.source_ref is not None:
                raise ValueError("open creation cannot select a source")
        elif self.source_type is None or self.source_ref is None:
            raise ValueError("source-grounded creation requires a source")
        return self


class OutlineUpdateRequest(_ApiModel):
    outline: dict[str, Any]


class DraftUpdateRequest(_ApiModel):
    document: dict[str, Any]


class ClassroomResponse(_ApiModel):
    asset_id: str
    draft_id: str
    job_id: str | None = None
    lifecycle_state: str
    status: str
    title: str
    course_id: str
    class_id: str
    owner_id: str
    revision: int
    outline: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    classroom_version_id: str | None = None
    confirmed_outline_sha256: str | None = None
    validation_report: dict[str, Any] | None = None
    idempotency_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "idempotency_key",
            "creation_idempotency_key",
        ),
        serialization_alias="idempotencyKey",
    )


class ClassroomListResponse(_ApiModel):
    items: list[ClassroomResponse]


class DraftMediaResponse(_ApiModel):
    id: str
    mime_type: str
    sha256: str
    size_bytes: int


class ClassroomServiceLike(Protocol):
    async def create(
        self,
        context: TenantContext,
        request: CreateClassroomRequest,
        idempotency_key: str | None = None,
    ): ...

    async def list(self, context: TenantContext): ...

    async def get(self, context: TenantContext, asset_id: str): ...

    async def get_draft(self, context: TenantContext, asset_id: str): ...

    async def update_outline(
        self,
        context: TenantContext,
        asset_id: str,
        outline: dict[str, Any],
        expected_revision: int,
    ): ...

    async def confirm_outline(self, context: TenantContext, asset_id: str): ...

    async def update_draft(
        self,
        context: TenantContext,
        asset_id: str,
        document: dict[str, Any],
        expected_revision: int,
    ): ...

    async def upload_media(
        self,
        context: TenantContext,
        asset_id: str,
        upload: UploadFile,
        declared_sha256: str,
    ): ...

    async def get_media(self, context: TenantContext, asset_id: str, media_id: str): ...

    async def validate(self, context: TenantContext, asset_id: str): ...


def get_classroom_repository(
    context: TenantContext = Depends(require_tenant),
) -> SqlAlchemyClassroomRepository:
    return SqlAlchemyClassroomRepository(get_platform_engine(), context.tenant_id)


def get_classroom_service(
    context: TenantContext = Depends(require_tenant),
    repository=Depends(get_classroom_repository),
    source_repository=Depends(get_source_repository),
    store_provider=Depends(get_source_store_provider),
    job_repository=Depends(get_job_repository),
    data_plane_selector=Depends(get_data_plane_selector),
) -> ClassroomService:
    snapshots = SourceSnapshotBuilder(
        context,
        source_repository,
        store_provider=store_provider,
    )
    return ClassroomService(
        repository,
        TeachingBriefBuilder(context, snapshots),
        SqlAlchemyClassroomGeneration(job_repository, data_plane_selector),
        store_provider,
    )


def _response(record: object) -> ClassroomResponse:
    return ClassroomResponse.model_validate(record, from_attributes=True)


def _required_record(record: object | None) -> object:
    if record is None:
        raise HTTPException(status_code=404, detail="Classroom not found")
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
    except ClassroomRevisionConflict:
        raise HTTPException(status_code=409, detail="Draft revision is stale") from None
    except ClassroomConfirmationConflict:
        raise HTTPException(
            status_code=409,
            detail="Outline confirmation conflicts",
        ) from None
    except ClassroomIdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail="Classroom idempotency key conflicts",
        ) from None
    except ClassroomNotFound:
        raise HTTPException(status_code=404, detail="Classroom not found") from None
    except ClassroomAccessDenied:
        raise HTTPException(status_code=403, detail="Classroom access denied") from None
    except InvalidDraftMedia as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except InvalidDraftDocument as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except InvalidClassroomState as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ClassroomPersistenceError, SQLAlchemyError):
        raise HTTPException(
            status_code=503,
            detail="Classroom persistence is unavailable",
        ) from None
    except ClassroomServiceError:
        raise HTTPException(status_code=422, detail="Classroom request is invalid") from None


@router.post("/classrooms", response_model=ClassroomResponse, status_code=202)
async def create_classroom(
    request: CreateClassroomRequest,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ] = None,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> ClassroomResponse:
    return _response(
        await _call(
            service.create(
                context,
                request,
                idempotency_key=idempotency_key,
            )
        )
    )


@router.get("/classrooms", response_model=ClassroomListResponse)
async def list_classrooms(
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> ClassroomListResponse:
    records = await _call(service.list(context))
    return ClassroomListResponse(items=[_response(record) for record in records])


@router.get("/classrooms/{asset_id}", response_model=ClassroomResponse)
async def get_classroom(
    asset_id: str,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> ClassroomResponse:
    return _response(_required_record(await _call(service.get(context, asset_id))))


@router.get("/classrooms/{asset_id}/draft", response_model=ClassroomResponse)
async def get_classroom_draft(
    asset_id: str,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> ClassroomResponse:
    return _response(_required_record(await _call(service.get_draft(context, asset_id))))


@router.put("/classrooms/{asset_id}/outline", response_model=ClassroomResponse)
async def update_classroom_outline(
    asset_id: str,
    request: OutlineUpdateRequest,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> ClassroomResponse:
    record = await _call(
        service.update_outline(
            context,
            asset_id,
            request.outline,
            _parse_if_match(if_match),
        )
    )
    return _response(record)


@router.post("/classrooms/{asset_id}/confirm-outline", response_model=ClassroomResponse, status_code=202)
async def confirm_classroom_outline(
    asset_id: str,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> ClassroomResponse:
    return _response(await _call(service.confirm_outline(context, asset_id)))


@router.put("/classrooms/{asset_id}/draft", response_model=ClassroomResponse)
async def update_classroom_draft(
    asset_id: str,
    request: DraftUpdateRequest,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> ClassroomResponse:
    record = await _call(
        service.update_draft(
            context,
            asset_id,
            request.document,
            _parse_if_match(if_match),
        )
    )
    return _response(record)


@router.post(
    "/classrooms/{asset_id}/draft-media",
    response_model=DraftMediaResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_classroom_draft_media(
    asset_id: str,
    file: Annotated[UploadFile, File()],
    sha256: Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")],
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> DraftMediaResponse:
    record = await _call(service.upload_media(context, asset_id, file, sha256))
    if isinstance(record, Mapping):
        return DraftMediaResponse(
            id=str(record["id"]),
            mime_type=str(record["mime_type"]),
            sha256=str(record["sha256"]),
            size_bytes=int(record["size_bytes"]),
        )
    return DraftMediaResponse(
        id=record.id,
        mime_type=record.mime_type,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
    )


@router.get("/classrooms/{asset_id}/draft-media/{media_id}")
async def get_classroom_draft_media(
    asset_id: str,
    media_id: str,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
):
    media = _required_record(await _call(service.get_media(context, asset_id, media_id)))
    if isinstance(media, DraftMediaContent):
        return StreamingResponse(
            media.body,
            media_type=media.mime_type,
            headers={
                "Content-Length": str(media.size_bytes),
                "ETag": f'"sha256-{media.sha256}"',
                "Cache-Control": "private, no-store",
            },
        )
    if isinstance(media, Mapping):
        content = media.get("content")
        mime_type = media.get("mime_type")
    else:
        content = getattr(media, "content", None)
        mime_type = getattr(media, "mime_type", None)
    if not isinstance(content, (bytes, bytearray)) or not isinstance(mime_type, str):
        raise HTTPException(status_code=404, detail="Draft media not found")
    return StreamingResponse(iter((bytes(content),)), media_type=mime_type)


@router.post("/classrooms/{asset_id}/validate", response_model=ClassroomResponse)
async def validate_classroom(
    asset_id: str,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomServiceLike = Depends(get_classroom_service),
) -> ClassroomResponse:
    return _response(_required_record(await _call(service.validate(context, asset_id))))


__all__ = [
    "ClassroomNotFound",
    "ClassroomRevisionConflict",
    "get_classroom_service",
    "get_classroom_repository",
    "router",
]
