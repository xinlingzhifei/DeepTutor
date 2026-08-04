"""Content-operations API for partial-success classroom batches."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from deeptutor.api.routers.classroom_jobs import (
    get_data_plane_selector,
    get_job_repository,
)
from deeptutor.api.routers.classrooms import (
    CreateClassroomRequest,
    get_classroom_repository,
)
from deeptutor.api.routers.teaching_catalog import (
    get_source_repository,
    get_source_store_provider,
)
from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.openmaic.data_planes import DataPlaneUnavailable
from deeptutor.teaching.quota import InsufficientQuota
from deeptutor.teaching.repositories.classrooms import ClassroomPersistenceError
from deeptutor.teaching.services.batches import (
    BatchAccessDenied,
    BatchIdempotencyConflict,
    BatchItemInput,
    BatchItemRejected,
    BatchNotFound,
    BatchOutlineConflict,
    BatchPersistenceError,
    BatchService,
    BatchServiceError,
    InvalidBatchRequest,
    InvalidBatchState,
    SqlAlchemyBatchClassroomGateway,
    SqlAlchemyBatchJobGateway,
    SqlAlchemyBatchRepository,
)
from deeptutor.teaching.services.classrooms import ClassroomServiceError
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


class BatchClassroomItemRequest(CreateClassroomRequest):
    item_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CreateBatchRequest(_ApiModel):
    items: list[BatchClassroomItemRequest] = Field(min_length=1, max_length=100)


class OutlineConfirmationRequest(_ApiModel):
    revision: int = Field(ge=1)
    outline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelectedOutlineConfirmation(OutlineConfirmationRequest):
    item_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SelectedOutlineConfirmationsRequest(_ApiModel):
    items: list[SelectedOutlineConfirmation] = Field(min_length=1, max_length=100)


class BatchItemResponse(_ApiModel):
    id: str
    batch_id: str
    status: str
    generation_job_id: str | None = None
    classroom_draft_id: str | None = None
    classroom_asset_id: str | None = None


class BatchResponse(_ApiModel):
    id: str
    tenant_id: str
    actor_id: str
    status: str
    item_count: int
    succeeded_count: int
    failed_count: int
    items: list[BatchItemResponse]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BatchListResponse(_ApiModel):
    items: list[BatchResponse]


class BatchRetryResponse(_ApiModel):
    parent_item_id: str
    item: BatchItemResponse


class BatchServiceLike(Protocol):
    async def create(self, context, items, *, idempotency_key): ...

    async def list(self, context, *, limit=50, offset=0): ...

    async def get(self, context, batch_id): ...

    async def confirm_outline(
        self,
        context,
        batch_id,
        item_id,
        *,
        revision,
        outline_sha256,
    ): ...

    async def confirm_outlines(self, context, batch_id, confirmations): ...

    async def retry_item(self, context, batch_id, item_id): ...

    async def cancel(self, context, batch_id): ...


def get_batch_repository(
    context: TenantContext = Depends(require_tenant),
) -> SqlAlchemyBatchRepository:
    return SqlAlchemyBatchRepository(get_platform_engine(), context.tenant_id)


def get_batch_service(
    context: TenantContext = Depends(require_tenant),
    repository=Depends(get_batch_repository),
    classroom_repository=Depends(get_classroom_repository),
    source_repository=Depends(get_source_repository),
    store_provider=Depends(get_source_store_provider),
    job_repository=Depends(get_job_repository),
    data_plane_selector=Depends(get_data_plane_selector),
) -> BatchService:
    snapshots = SourceSnapshotBuilder(
        context,
        source_repository,
        store_provider=store_provider,
    )
    classrooms = SqlAlchemyBatchClassroomGateway(
        classroom_repository,
        TeachingBriefBuilder(context, snapshots),
        job_repository,
        data_plane_selector,
        store_provider,
    )
    return BatchService(
        repository,
        classrooms,
        SqlAlchemyBatchJobGateway(job_repository),
    )


def _batch_response(record: object) -> BatchResponse:
    return BatchResponse.model_validate(record, from_attributes=True)


async def _call(operation):
    try:
        return await operation
    except BatchNotFound:
        raise HTTPException(status_code=404, detail="Batch not found") from None
    except (BatchAccessDenied, SourceAccessDenied):
        raise HTTPException(status_code=403, detail="Batch access denied") from None
    except BatchIdempotencyConflict:
        raise HTTPException(status_code=409, detail="Batch idempotency key conflicts") from None
    except BatchOutlineConflict:
        raise HTTPException(
            status_code=409,
            detail="Batch outline confirmation conflicts",
        ) from None
    except InvalidBatchState:
        raise HTTPException(status_code=409, detail="Batch state conflicts") from None
    except InsufficientQuota:
        raise HTTPException(status_code=409, detail="Insufficient quota") from None
    except (InvalidBatchRequest, BatchItemRejected):
        raise HTTPException(
            status_code=422,
            detail="Batch request is invalid",
        ) from None
    except (
        BatchPersistenceError,
        ClassroomPersistenceError,
        DataPlaneBindingUnavailable,
        DataPlaneUnavailable,
        SourceSnapshotUnavailable,
        SQLAlchemyError,
        BatchServiceError,
        ClassroomServiceError,
        ValueError,
        PermissionError,
    ):
        raise HTTPException(
            status_code=503,
            detail="Batch processing is unavailable",
        ) from None


@router.post(
    "/classroom-batches",
    response_model=BatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_batch(
    request: CreateBatchRequest,
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
    service: BatchServiceLike = Depends(get_batch_service),
) -> BatchResponse:
    items = tuple(BatchItemInput(item.item_id, item) for item in request.items)
    return _batch_response(
        await _call(
            service.create(
                context,
                items,
                idempotency_key=idempotency_key,
            )
        )
    )


@router.get("/classroom-batches", response_model=BatchListResponse)
async def list_batches(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    context: TenantContext = Depends(require_tenant),
    service: BatchServiceLike = Depends(get_batch_service),
) -> BatchListResponse:
    batches = await _call(service.list(context, limit=limit, offset=offset))
    return BatchListResponse(items=[_batch_response(batch) for batch in batches])


@router.get("/classroom-batches/{batch_id}", response_model=BatchResponse)
async def get_batch(
    batch_id: str,
    context: TenantContext = Depends(require_tenant),
    service: BatchServiceLike = Depends(get_batch_service),
) -> BatchResponse:
    batch = await _call(service.get(context, batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _batch_response(batch)


@router.post(
    "/classroom-batches/{batch_id}/items/{item_id}/confirm-outline",
    response_model=BatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_item_outline(
    batch_id: str,
    item_id: str,
    request: OutlineConfirmationRequest,
    context: TenantContext = Depends(require_tenant),
    service: BatchServiceLike = Depends(get_batch_service),
) -> BatchResponse:
    return _batch_response(
        await _call(
            service.confirm_outline(
                context,
                batch_id,
                item_id,
                revision=request.revision,
                outline_sha256=request.outline_sha256,
            )
        )
    )


@router.post(
    "/classroom-batches/{batch_id}/confirm-outlines",
    response_model=BatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_selected_outlines(
    batch_id: str,
    request: SelectedOutlineConfirmationsRequest,
    context: TenantContext = Depends(require_tenant),
    service: BatchServiceLike = Depends(get_batch_service),
) -> BatchResponse:
    confirmations = tuple(
        (item.item_id, item.revision, item.outline_sha256)
        for item in request.items
    )
    return _batch_response(
        await _call(service.confirm_outlines(context, batch_id, confirmations))
    )


@router.post(
    "/classroom-batches/{batch_id}/items/{item_id}/retry",
    response_model=BatchRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_batch_item(
    batch_id: str,
    item_id: str,
    context: TenantContext = Depends(require_tenant),
    service: BatchServiceLike = Depends(get_batch_service),
) -> BatchRetryResponse:
    retried = await _call(service.retry_item(context, batch_id, item_id))
    return BatchRetryResponse.model_validate(retried, from_attributes=True)


@router.post(
    "/classroom-batches/{batch_id}/cancel",
    response_model=BatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_batch(
    batch_id: str,
    context: TenantContext = Depends(require_tenant),
    service: BatchServiceLike = Depends(get_batch_service),
) -> BatchResponse:
    return _batch_response(await _call(service.cancel(context, batch_id)))


__all__ = ["get_batch_repository", "get_batch_service", "router"]
