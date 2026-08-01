"""Controlled tenant APIs for durable classroom generation jobs."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from deeptutor.api.routers.auth import require_platform_enabled
from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.contracts import (
    ClassroomMode,
    ExportFormat,
    ExportRequest,
    GenerationPriority,
    GenerationRequest,
    OutlineBundle,
    Sha256,
    TeachingBrief,
    canonical_json_bytes,
    canonical_outline_sha256,
)
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.models.jobs import TERMINAL_JOB_STATUSES
from deeptutor.teaching.object_store import (
    ClassroomArtifactStore,
    ObjectStoreConfigurationError,
    ObjectStoreNotFound,
)
from deeptutor.teaching.openmaic.auth import ServiceSecretUnavailable
from deeptutor.teaching.openmaic.client import OpenMAICError
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneSelection,
    DataPlaneSelector,
    DataPlaneUnavailable,
)
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.quota import InsufficientQuota
from deeptutor.teaching.repositories.data_planes import SqlAlchemyDataPlaneRepository
from deeptutor.teaching.repositories.jobs import (
    CancellationRequest,
    ContentRequeueConflict,
    GenerationJobDetails,
    GenerationJobRequest,
    IdempotencyConflict,
    SqlAlchemyGenerationJobRepository,
    build_explicit_retry_request,
)
from deeptutor.teaching.scheduler import PRIORITY_RANK
from deeptutor.teaching.tenant_context import TenantContext, require_tenant

router = APIRouter(dependencies=[Depends(require_platform_enabled)])
_DOWNLOAD_TTL_SECONDS = 60


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        populate_by_name=True,
    )


class ClassroomJobCreateRequest(_ApiModel):
    """Public generation input; trusted tenant, job, and route fields are absent."""

    schema_version: Literal["1.0"]
    request_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    phase: Literal["outline", "micro"]
    classroom_mode: ClassroomMode
    teaching_brief_id: str = Field(min_length=1)
    teaching_brief_sha256: Sha256
    teaching_brief: TeachingBrief
    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    scene_budget: int = Field(ge=1)
    duration_minutes: int = Field(ge=1)
    requested_exports: list[ExportFormat] = Field(min_length=1)
    callback_context: str = Field(min_length=1, pattern=r"^[^:\s]+$")
    priority: GenerationPriority
    quota_units: int = Field(default=1, ge=1)
    visibility: Literal["private", "class", "tenant"] = "private"
    classroom_draft_id: str | None = Field(default=None, min_length=1, max_length=64)

    def to_generation_request(
        self,
        *,
        context: TenantContext,
        selection: DataPlaneSelection,
        job_id: str,
    ) -> GenerationRequest:
        return GenerationRequest(
            schema_version=self.schema_version,
            tenant_id=context.tenant_id,
            request_id=self.request_id,
            job_id=job_id,
            idempotency_key=self.idempotency_key,
            phase=self.phase,
            classroom_mode=self.classroom_mode,
            teaching_brief_id=self.teaching_brief_id,
            teaching_brief_sha256=self.teaching_brief_sha256,
            teaching_brief=self.teaching_brief,
            confirmed_outline=None,
            confirmed_outline_sha256=None,
            template_id=self.template_id,
            template_version=self.template_version,
            scene_budget=self.scene_budget,
            duration_minutes=self.duration_minutes,
            requested_exports=self.requested_exports,
            callback_context=self.callback_context,
            data_plane_route_id=selection.route_ref,
            priority=self.priority,
        )


class ConfirmOutlineRequest(_ApiModel):
    confirmed_outline: OutlineBundle
    confirmed_outline_sha256: Sha256

    @model_validator(mode="after")
    def validate_confirmation(self) -> ConfirmOutlineRequest:
        if (
            self.confirmed_outline.confirmation_metadata.status != "confirmed"
            or canonical_outline_sha256(self.confirmed_outline)
            != self.confirmed_outline_sha256
        ):
            raise ValueError("confirmed outline hash does not match canonical JSON")
        return self


class RetryJobRequest(_ApiModel):
    request_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)


class JobStatusResponse(BaseModel):
    job_id: str
    job_kind: str
    phase: str
    status: str
    progress_percent: int
    waiting_reason: str | None
    cancellable: bool
    retryable: bool
    outline: dict[str, Any] | None
    error_category: str | None
    error_code: str | None
    retry_of_job_id: str | None
    export_format: str | None
    download_ready: bool


class JobCancellationGateway(Protocol):
    async def cancel(self, request: CancellationRequest) -> None: ...


class DownloadStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> ClassroomArtifactStore: ...


def get_job_repository() -> SqlAlchemyGenerationJobRepository:
    return SqlAlchemyGenerationJobRepository()


def get_data_plane_selector() -> DataPlaneSelector:
    return DataPlaneSelector(
        settings=load_platform_settings(),
        repository=SqlAlchemyDataPlaneRepository(),
    )


def get_cancellation_gateway() -> JobCancellationGateway:
    from deeptutor.teaching.processes import RuntimeCancellationGateway

    return RuntimeCancellationGateway(load_platform_settings())


def get_download_store_provider() -> DownloadStoreProvider:
    from deeptutor.teaching.processes import RuntimeStoreProvider

    return RuntimeStoreProvider(load_platform_settings())


def _server_job_id(tenant_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\0{idempotency_key}".encode()).hexdigest()
    return f"job-{digest[:48]}"


def _resource_for_brief(tenant_id: str, brief: TeachingBrief) -> ResourceScope:
    return ResourceScope(
        tenant_id=tenant_id,
        course_id=brief.course_id,
        class_id=brief.target_class_id,
    )


def _resource_for_generation(request: GenerationRequest) -> ResourceScope:
    return _resource_for_brief(request.tenant_id, request.teaching_brief)


def _allows_any(
    context: TenantContext,
    permissions: set[str],
    resource: ResourceScope,
) -> bool:
    return any(
        grant.allows_resource(permission, resource)
        for grant in context.permissions
        for permission in permissions
    )


def _require_create_access(
    context: TenantContext,
    *,
    classroom_mode: ClassroomMode,
    brief: TeachingBrief,
) -> None:
    if brief.tenant_id != context.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Resource denied")
    resource = _resource_for_brief(context.tenant_id, brief)
    generation_permissions = (
        {"classroom.generate.micro"}
        if classroom_mode == "micro"
        else {"classroom.create", "classroom.generate.full"}
    )
    if not _allows_any(context, generation_permissions, resource):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Resource denied")
    if brief.content_mode == "source_grounded" and not _allows_any(
        context,
        {"source.use"},
        resource,
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Resource denied")


def _parse_request(details: GenerationJobDetails) -> GenerationRequest | ExportRequest:
    try:
        if details.job_kind == "generation":
            return GenerationRequest.model_validate_json(details.request_payload)
        return ExportRequest.model_validate_json(details.request_payload)
    except ValidationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job request is unavailable",
        ) from None


def _can_access(
    context: TenantContext,
    details: GenerationJobDetails,
    *,
    mutate: bool,
) -> bool:
    if details.tenant_id != context.tenant_id:
        return False
    if details.owner_id == context.user_id:
        return True
    if details.visibility == "private":
        return False
    try:
        parsed = _parse_request(details)
    except HTTPException:
        return False
    if parsed.tenant_id != context.tenant_id:
        return False
    resource = (
        _resource_for_generation(parsed)
        if isinstance(parsed, GenerationRequest)
        else ResourceScope(tenant_id=context.tenant_id)
    )
    permissions = (
        {"classroom.edit", "tenant.manage"}
        if mutate
        else {
            "classroom.create",
            "classroom.edit",
            "classroom.generate.micro",
            "classroom.generate.full",
            "tenant.manage",
        }
    )
    return _allows_any(context, permissions, resource)


async def _authorized_job(
    repository: SqlAlchemyGenerationJobRepository,
    context: TenantContext,
    job_id: str,
    *,
    mutate: bool = False,
) -> GenerationJobDetails:
    details = await repository.get_job_details(context.tenant_id, job_id)
    if details is None or not _can_access(context, details, mutate=mutate):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return details


def _outline_payload(details: GenerationJobDetails) -> dict[str, Any] | None:
    if details.job_kind != "generation" or details.result_payload is None:
        return None
    try:
        outline = OutlineBundle.model_validate_json(details.result_payload)
    except ValidationError:
        return None
    return outline.model_dump(mode="json", by_alias=True, exclude_none=True)


def _response(details: GenerationJobDetails) -> JobStatusResponse:
    terminal = details.status in TERMINAL_JOB_STATUSES
    return JobStatusResponse(
        job_id=details.job_id,
        job_kind=details.job_kind,
        phase=details.phase,
        status=details.status,
        progress_percent=details.progress_percent,
        waiting_reason=details.waiting_reason,
        cancellable=not terminal,
        retryable=details.status in {"failed", "canceled"},
        outline=_outline_payload(details),
        error_category=details.error_category,
        error_code=details.error_code,
        retry_of_job_id=details.retry_of_job_id,
        export_format=details.export_format,
        download_ready=details.job_kind == "export" and details.status == "succeeded",
    )


def _generation_job_request(
    details: GenerationJobDetails,
) -> GenerationJobRequest:
    priority = next(
        (name for name, rank in PRIORITY_RANK.items() if rank == details.priority),
        None,
    )
    if priority is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job cannot be retried")
    return GenerationJobRequest(
        tenant_id=details.tenant_id,
        job_id=details.job_id,
        job_kind=details.job_kind,
        phase=details.phase,
        export_format=details.export_format,
        priority=priority,
        quota_units=details.quota_units,
        actor_id=details.actor_id,
        owner_id=details.owner_id,
        visibility=details.visibility,
        request_id=details.request_id,
        idempotency_key=details.idempotency_key,
        request_sha256=details.request_sha256,
        data_plane_route_id=details.data_plane_route_id,
        provider_profile_id=details.provider_profile_id,
        worker_pool_ref=details.worker_pool_ref,
        queue_ref=details.queue_ref,
        request_payload=details.request_payload,
        classroom_draft_id=details.classroom_draft_id,
        batch_id=details.batch_id,
    )


@router.post(
    "/classroom-jobs",
    response_model=JobStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_classroom_job(
    request: ClassroomJobCreateRequest,
    context: TenantContext = Depends(require_tenant),
    repository: SqlAlchemyGenerationJobRepository = Depends(get_job_repository),
    selector: DataPlaneSelector = Depends(get_data_plane_selector),
) -> JobStatusResponse:
    try:
        _require_create_access(
            context,
            classroom_mode=request.classroom_mode,
            brief=request.teaching_brief,
        )
        selection = await selector.resolve(context.tenant_id)
        if selection is None:
            raise DataPlaneUnavailable()
        job_id = _server_job_id(context.tenant_id, request.idempotency_key)
        generation = request.to_generation_request(
            context=context,
            selection=selection,
            job_id=job_id,
        )
        payload = canonical_json_bytes(generation).decode()
        await repository.create_job_and_reserve(
            GenerationJobRequest(
                tenant_id=context.tenant_id,
                job_id=job_id,
                job_kind="generation",
                phase="content" if generation.phase == "micro" else generation.phase,
                export_format=None,
                priority=generation.priority,
                quota_units=request.quota_units,
                actor_id=context.user_id,
                owner_id=context.user_id,
                visibility=request.visibility,
                request_id=generation.request_id,
                idempotency_key=generation.idempotency_key,
                request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                data_plane_route_id=selection.route_ref,
                provider_profile_id=selection.provider_profile_ref,
                worker_pool_ref=selection.worker_pool_ref,
                queue_ref=selection.queue_ref,
                request_payload=payload,
                classroom_draft_id=request.classroom_draft_id,
            )
        )
    except ValidationError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid generation request",
        ) from None
    except (DataPlaneBindingUnavailable, DataPlaneUnavailable):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Generation data plane unavailable",
        ) from None
    except InsufficientQuota:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Insufficient quota")
    except IdempotencyConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Idempotency conflict")
    details = await repository.get_job_details(context.tenant_id, job_id)
    if details is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Job unavailable")
    return _response(details)


@router.get("/classroom-jobs/{job_id}", response_model=JobStatusResponse)
async def get_classroom_job(
    job_id: str,
    context: TenantContext = Depends(require_tenant),
    repository: SqlAlchemyGenerationJobRepository = Depends(get_job_repository),
) -> JobStatusResponse:
    details = await _authorized_job(repository, context, job_id)
    if details.job_kind != "generation":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _response(details)


@router.post(
    "/classroom-jobs/{job_id}/confirm-outline",
    response_model=JobStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_outline(
    job_id: str,
    confirmation: ConfirmOutlineRequest,
    context: TenantContext = Depends(require_tenant),
    repository: SqlAlchemyGenerationJobRepository = Depends(get_job_repository),
) -> JobStatusResponse:
    details = await _authorized_job(repository, context, job_id, mutate=True)
    if (
        details.job_kind != "generation"
        or details.phase != "outline"
        or details.status != "awaiting_confirmation"
        or confirmation.confirmed_outline.confirmation_metadata.confirmed_by
        != context.user_id
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Outline cannot be confirmed")
    original = _parse_request(details)
    if not isinstance(original, GenerationRequest):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Outline cannot be confirmed")
    content_payload = original.model_dump(mode="json", by_alias=True, exclude_none=True)
    content_payload.update(
        phase="content",
        confirmedOutline=confirmation.confirmed_outline.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        ),
        confirmedOutlineSha256=confirmation.confirmed_outline_sha256,
    )
    try:
        content_request = GenerationRequest.model_validate(content_payload)
    except ValidationError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Confirmed outline is invalid",
        ) from None
    payload = canonical_json_bytes(content_request).decode()
    payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
    try:
        requeued = await repository.requeue_confirmed_content(
            context.tenant_id,
            job_id,
            request_payload=payload,
            request_sha256=payload_sha256,
        )
    except DataPlaneBindingUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Generation data plane unavailable",
        ) from None
    except ContentRequeueConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Outline cannot be confirmed",
        ) from None
    if not requeued:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Outline cannot be confirmed")
    updated = await repository.get_job_details(context.tenant_id, job_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _response(updated)


@router.post(
    "/classroom-jobs/{job_id}/cancel",
    response_model=JobStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_job(
    job_id: str,
    context: TenantContext = Depends(require_tenant),
    repository: SqlAlchemyGenerationJobRepository = Depends(get_job_repository),
    gateway: JobCancellationGateway = Depends(get_cancellation_gateway),
) -> JobStatusResponse:
    details = await _authorized_job(repository, context, job_id, mutate=True)
    if details.job_kind != "generation":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    cancellation = await repository.request_cancel(context.tenant_id, job_id)
    if cancellation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job cannot be canceled")
    if cancellation.running:
        try:
            await gateway.cancel(cancellation)
        except (DataPlaneUnavailable, OpenMAICError, ServiceSecretUnavailable):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Cancellation is pending",
            ) from None
        await repository.finish_requested_cancellation(context.tenant_id, job_id)
    updated = await repository.get_job_details(context.tenant_id, job_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _response(updated)


@router.post(
    "/classroom-jobs/{job_id}/retry",
    response_model=JobStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_job(
    job_id: str,
    retry: RetryJobRequest,
    context: TenantContext = Depends(require_tenant),
    repository: SqlAlchemyGenerationJobRepository = Depends(get_job_repository),
) -> JobStatusResponse:
    details = await _authorized_job(repository, context, job_id, mutate=True)
    if details.job_kind != "generation" or details.status not in {"failed", "canceled"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job cannot be retried")
    new_job_id = _server_job_id(context.tenant_id, retry.idempotency_key)
    try:
        retried = build_explicit_retry_request(
            _generation_job_request(details),
            job_id=new_job_id,
            request_id=retry.request_id,
            idempotency_key=retry.idempotency_key,
        )
        await repository.create_job_and_reserve(retried)
    except IdempotencyConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Idempotency conflict")
    except InsufficientQuota:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Insufficient quota")
    except DataPlaneBindingUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Generation data plane unavailable",
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job cannot be retried",
        ) from None
    updated = await repository.get_job_details(context.tenant_id, new_job_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Job unavailable")
    return _response(updated)


@router.get("/classroom-exports/{job_id}", response_model=JobStatusResponse)
async def get_classroom_export(
    job_id: str,
    context: TenantContext = Depends(require_tenant),
    repository: SqlAlchemyGenerationJobRepository = Depends(get_job_repository),
) -> JobStatusResponse:
    details = await _authorized_job(repository, context, job_id)
    if details.job_kind != "export":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found")
    return _response(details)


@router.get("/classroom-exports/{job_id}/download")
async def download_classroom_export(
    job_id: str,
    context: TenantContext = Depends(require_tenant),
    repository: SqlAlchemyGenerationJobRepository = Depends(get_job_repository),
    stores: DownloadStoreProvider = Depends(get_download_store_provider),
):
    details = await _authorized_job(repository, context, job_id)
    if details.job_kind != "export":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found")
    if details.status != "succeeded":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Export is not ready")
    artifact = await repository.get_export_artifact(context.tenant_id, job_id)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found")
    store = await stores.store_for_tenant(context.tenant_id)
    try:
        signed_url = await store.presign_download(
            artifact.object_key,
            _DOWNLOAD_TTL_SECONDS,
        )
    except ObjectStoreConfigurationError:
        try:
            stream = await store.open(artifact.object_key)
        except ObjectStoreNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found")
        filename = quote(PurePosixPath(artifact.relative_name).name, safe="")
        return StreamingResponse(
            stream,
            media_type=artifact.mime_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )
    except ObjectStoreNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found")
    return RedirectResponse(signed_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


__all__ = [
    "get_cancellation_gateway",
    "get_data_plane_selector",
    "get_download_store_provider",
    "get_job_repository",
    "router",
]
