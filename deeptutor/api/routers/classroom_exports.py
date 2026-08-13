"""Controlled APIs for exports pinned to one classroom draft or version."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from deeptutor.api.routers.auth import require_platform_enabled
from deeptutor.api.routers.classroom_content import (
    classroom_content_response,
    get_classroom_content_service,
)
from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.object_store import (
    ClassroomArtifactStore,
    ObjectStoreAccessDenied,
    ObjectStoreConfigurationError,
    ObjectStoreConflictError,
    ObjectStoreError,
    ObjectStoreIntegrityError,
    ObjectStoreNotFound,
)
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneSelector,
    DataPlaneUnavailable,
)
from deeptutor.teaching.quota import InsufficientQuota
from deeptutor.teaching.repositories.data_planes import SqlAlchemyDataPlaneRepository
from deeptutor.teaching.repositories.exports import (
    SqlAlchemyClassroomExportRepository,
)
from deeptutor.teaching.repositories.jobs import SqlAlchemyGenerationJobRepository
from deeptutor.teaching.services.classroom_content import (
    ClassroomContentAccessDenied,
    ClassroomContentIntegrityError,
    ClassroomContentNotFound,
    ClassroomContentUnavailable,
)
from deeptutor.teaching.services.export_jobs import SqlAlchemyExportJobGateway
from deeptutor.teaching.services.exports import (
    ClassroomExportError,
    ClassroomExportInputMaterializer,
    ClassroomExportService,
    ExportAccessDenied,
    ExportIdempotencyConflict,
    ExportNotFound,
    ExportPolicyDenied,
    ExportRecord,
    ExportRevisionConflict,
    InvalidExportInput,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant
from deeptutor.teaching.tickets import TicketExpired, TicketInvalid, TicketScopeError

router = APIRouter(dependencies=[Depends(require_platform_enabled)])
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})


def get_classroom_content_service_factory():
    """Defer ticket-secret loading until a student ticket is actually supplied."""

    return get_classroom_content_service


class ExportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["classroom_zip", "pptx", "offline_html", "mp4"]


class ExportStatusResponse(BaseModel):
    job_id: str
    job_kind: Literal["export"] = "export"
    phase: Literal["export"] = "export"
    status: str
    progress_percent: int
    waiting_reason: str | None
    cancellable: bool
    retryable: bool
    outline: None = None
    error_category: str | None
    error_code: str | None
    retry_of_job_id: str | None
    export_format: str
    download_ready: bool


class ClassroomExportServiceLike(Protocol):
    async def create_for_draft(
        self,
        context: TenantContext,
        asset_id: str,
        export_format: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ExportRecord: ...

    async def create_for_version(
        self,
        context: TenantContext,
        version_id: str,
        export_format: str,
        *,
        idempotency_key: str,
    ) -> ExportRecord: ...

    async def get(
        self,
        context: TenantContext,
        export_id: str,
    ) -> ExportRecord | None: ...


class ExportStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> ClassroomArtifactStore: ...


def get_export_store_provider() -> ExportStoreProvider:
    from deeptutor.teaching.processes import RuntimeStoreProvider

    return RuntimeStoreProvider(load_platform_settings())


def get_classroom_export_service(
    context: TenantContext = Depends(require_tenant),
    stores: ExportStoreProvider = Depends(get_export_store_provider),
) -> ClassroomExportService:
    settings = load_platform_settings()
    repository = SqlAlchemyClassroomExportRepository(
        get_platform_engine(),
        context.tenant_id,
        stores,
    )
    gateway = SqlAlchemyExportJobGateway(
        SqlAlchemyGenerationJobRepository(),
        repository,
        DataPlaneSelector(
            settings=settings,
            repository=SqlAlchemyDataPlaneRepository(),
        ),
    )
    return ClassroomExportService(
        repository,
        ClassroomExportInputMaterializer(stores),
        gateway,
        mp4_enabled=lambda _tenant_id: repository.mp4_enabled(),
    )


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


def _public_status(record: ExportRecord) -> str:
    if record.status in {"preparing_input", "input_ready"}:
        return "created"
    return record.status


def _ready(record: ExportRecord) -> bool:
    return (
        _public_status(record) == "succeeded"
        and record.relative_name is not None
        and record.object_key is not None
        and record.sha256 is not None
        and record.size_bytes is not None
        and record.mime_type is not None
    )


def _response(record: ExportRecord) -> ExportStatusResponse:
    public_status = _public_status(record)
    terminal = public_status in _TERMINAL_STATUSES
    if public_status == "succeeded" and not _ready(record):
        raise HTTPException(status_code=409, detail="Export state is unavailable")
    if public_status in {"failed", "canceled"} and (
        record.error_category is None or record.error_code is None
    ):
        raise HTTPException(status_code=409, detail="Export state is unavailable")
    return ExportStatusResponse(
        job_id=record.job_id or record.export_id,
        status=public_status,
        progress_percent=100 if public_status == "succeeded" else record.progress_percent,
        waiting_reason=None if terminal else record.waiting_reason,
        cancellable=not terminal,
        retryable=public_status in {"failed", "canceled"},
        error_category=record.error_category if public_status in {"failed", "canceled"} else None,
        error_code=record.error_code if public_status in {"failed", "canceled"} else None,
        retry_of_job_id=record.retry_of_job_id,
        export_format=record.export_format,
        download_ready=_ready(record),
    )


async def _create(operation) -> ExportStatusResponse:
    try:
        return _response(await operation)
    except ExportNotFound:
        raise HTTPException(status_code=404, detail="Classroom not found") from None
    except ExportAccessDenied:
        raise HTTPException(status_code=403, detail="Classroom access denied") from None
    except ExportPolicyDenied:
        raise HTTPException(
            status_code=403,
            detail="MP4_EXPORT_DISABLED_BY_TENANT_POLICY",
        ) from None
    except (ExportRevisionConflict, ExportIdempotencyConflict):
        raise HTTPException(status_code=409, detail="Export request conflicts") from None
    except ObjectStoreConflictError:
        raise HTTPException(status_code=409, detail="Export input conflicts") from None
    except InsufficientQuota:
        raise HTTPException(status_code=409, detail="Insufficient quota") from None
    except (InvalidExportInput, ValueError):
        raise HTTPException(status_code=422, detail="Export input is invalid") from None
    except (ObjectStoreNotFound, ObjectStoreAccessDenied, ObjectStoreIntegrityError):
        raise HTTPException(status_code=409, detail="Export input is unavailable") from None
    except (
        DataPlaneBindingUnavailable,
        DataPlaneUnavailable,
        ObjectStoreConfigurationError,
        ObjectStoreError,
        SQLAlchemyError,
    ):
        raise HTTPException(status_code=503, detail="Export service is unavailable") from None
    except ClassroomExportError:
        raise HTTPException(status_code=503, detail="Export service is unavailable") from None


@router.post(
    "/classrooms/{asset_id}/draft/exports",
    response_model=ExportStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_draft_export(
    asset_id: str,
    request: ExportCreateRequest,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomExportServiceLike = Depends(get_classroom_export_service),
) -> ExportStatusResponse:
    revision = _parse_if_match(if_match)
    return await _create(
        service.create_for_draft(
            context,
            asset_id,
            request.format,
            expected_revision=revision,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/classroom-versions/{version_id}/exports",
    response_model=ExportStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_version_export(
    version_id: str,
    request: ExportCreateRequest,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ],
    context: TenantContext = Depends(require_tenant),
    service: ClassroomExportServiceLike = Depends(get_classroom_export_service),
) -> ExportStatusResponse:
    return await _create(
        service.create_for_version(
            context,
            version_id,
            request.format,
            idempotency_key=idempotency_key,
        )
    )


async def _authorized_export(
    service: ClassroomExportServiceLike,
    context: TenantContext,
    export_id: str,
) -> ExportRecord:
    record = await service.get(context, export_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Export not found")
    return record


@router.get(
    "/classroom-exports/{export_id}",
    response_model=ExportStatusResponse,
)
async def get_classroom_export(
    export_id: str,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomExportServiceLike = Depends(get_classroom_export_service),
) -> ExportStatusResponse:
    return _response(await _authorized_export(service, context, export_id))


@router.get("/classroom-exports/{export_id}/download")
async def download_classroom_export(
    export_id: str,
    classroom_ticket: Annotated[
        str | None,
        Header(alias="X-Classroom-Ticket", min_length=1, max_length=8192),
    ] = None,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomExportServiceLike = Depends(get_classroom_export_service),
    stores: ExportStoreProvider = Depends(get_export_store_provider),
    content_service_factory=Depends(get_classroom_content_service_factory),
):
    if classroom_ticket is not None:
        content_service = content_service_factory()
        try:
            content = await content_service.open_export(
                context,
                export_id=export_id,
                token=classroom_ticket,
            )
        except TicketExpired:
            raise HTTPException(status_code=401, detail="Classroom ticket expired") from None
        except TicketScopeError:
            raise HTTPException(
                status_code=403,
                detail="Classroom ticket scope denied",
            ) from None
        except TicketInvalid:
            raise HTTPException(status_code=401, detail="Classroom ticket invalid") from None
        except ClassroomContentAccessDenied:
            raise HTTPException(status_code=403, detail="Export access denied") from None
        except ClassroomContentNotFound:
            raise HTTPException(status_code=404, detail="Export not found") from None
        except ClassroomContentIntegrityError:
            raise HTTPException(
                status_code=503,
                detail="Export download is unavailable",
            ) from None
        except ClassroomContentUnavailable:
            raise HTTPException(
                status_code=503,
                detail="Export download is unavailable",
            ) from None
        return classroom_content_response(
            content,
            attachment_filename=content.filename or export_id,
        )
    record = await _authorized_export(service, context, export_id)
    if not _ready(record):
        raise HTTPException(status_code=409, detail="Export is not ready")
    assert record.object_key is not None
    assert record.relative_name is not None
    assert record.mime_type is not None
    try:
        store = await stores.store_for_tenant(context.tenant_id)
    except ObjectStoreConfigurationError:
        raise HTTPException(
            status_code=503,
            detail="Export download is unavailable",
        ) from None
    try:
        stream = await store.open(record.object_key)
    except (ObjectStoreNotFound, ObjectStoreAccessDenied):
        raise HTTPException(status_code=404, detail="Export not found") from None
    except (ObjectStoreIntegrityError, ObjectStoreError):
        raise HTTPException(
            status_code=503,
            detail="Export download is unavailable",
        ) from None
    filename = quote(PurePosixPath(record.relative_name).name, safe="")
    return StreamingResponse(
        stream,
        media_type=record.mime_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


__all__ = [
    "get_classroom_export_service",
    "get_export_store_provider",
    "router",
]
