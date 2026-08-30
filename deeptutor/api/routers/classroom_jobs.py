"""Controlled tenant APIs for durable classroom generation jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from deeptutor.api.routers.auth import require_platform_admin, require_platform_enabled
from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.contracts import (
    ClassroomMode,
    ExportFormat,
    ExportRequest,
    GenerationPriority,
    GenerationRequest,
    OutlineBundle,
    OutlineConfirmationMetadata,
    Sha256,
    TeachingBrief,
    canonical_json_bytes,
    canonical_outline_sha256,
    canonical_teaching_brief_sha256,
    validate_outline_binding,
)
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.models.jobs import TERMINAL_JOB_STATUSES
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


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        populate_by_name=True,
    )


@dataclass(frozen=True, slots=True)
class TrustedTeachingBrief:
    brief: TeachingBrief
    resource: ResourceScope
    priority: GenerationPriority
    quota_units: int
    visibility: Literal["private", "class", "tenant"]
    classroom_draft_id: str | None = None


class TrustedTeachingBriefResolver(Protocol):
    async def resolve(
        self,
        *,
        context: TenantContext,
        teaching_brief_id: str,
        teaching_brief_sha256: str,
        phase: str,
        classroom_mode: ClassroomMode,
        requested_exports: tuple[ExportFormat, ...],
    ) -> TrustedTeachingBrief | None: ...


class TrustedTeachingBriefUnavailable(RuntimeError):
    pass


class _UnavailableTrustedTeachingBriefResolver:
    async def resolve(self, **_kwargs: object) -> TrustedTeachingBrief | None:
        raise TrustedTeachingBriefUnavailable()


class ClassroomJobCreateRequest(_ApiModel):
    """Public generation input; trusted tenant, job, and route fields are absent."""

    schema_version: Literal["1.0"]
    request_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    phase: Literal["outline", "micro"]
    classroom_mode: ClassroomMode
    teaching_brief_id: str = Field(min_length=1)
    teaching_brief_sha256: Sha256
    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    scene_budget: int = Field(ge=1)
    duration_minutes: int = Field(ge=1)
    requested_exports: list[ExportFormat] = Field(min_length=1)
    callback_context: str = Field(min_length=1, pattern=r"^[^:\s]+$")

    def to_generation_request(
        self,
        *,
        context: TenantContext,
        selection: DataPlaneSelection,
        trusted: TrustedTeachingBrief,
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
            teaching_brief=trusted.brief,
            confirmed_outline=None,
            confirmed_outline_sha256=None,
            template_id=self.template_id,
            template_version=self.template_version,
            scene_budget=self.scene_budget,
            duration_minutes=self.duration_minutes,
            requested_exports=self.requested_exports,
            callback_context=self.callback_context,
            data_plane_route_id=selection.route_ref,
            priority=trusted.priority,
        )


class ConfirmOutlineRequest(_ApiModel):
    confirmed_outline: OutlineBundle
    confirmed_outline_sha256: Sha256

    @model_validator(mode="after")
    def validate_confirmation(self) -> ConfirmOutlineRequest:
        if (
            self.confirmed_outline.confirmation_metadata.status != "confirmed"
            or canonical_outline_sha256(self.confirmed_outline) != self.confirmed_outline_sha256
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


class GenerationBindingSnapshotResponse(_ApiModel):
    schema_version: Literal[1] = 1
    tenant_id: str
    job_id: str
    job_kind: str
    phase: str
    status: str
    progress_percent: int
    classroom_version_id: str | None
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str
    data_plane_mode: Literal["shared", "dedicated"]
    route_tenant_id: str | None
    route_owner_key: str
    provider_scope: Literal["shared", "dedicated"]
    provider_tenant_id: str | None
    provider_owner_key: str
    attempt_count: int
    shared_route_attempt_count: int
    dedicated_route_attempt_count: int
    selected_route_attempt_count: int
    unavailable_route_attempt_count: int
    route_attempt_history_complete: Literal[True] = True


class JobCancellationGateway(Protocol):
    async def cancel(self, request: CancellationRequest) -> None: ...


def get_job_repository() -> SqlAlchemyGenerationJobRepository:
    return SqlAlchemyGenerationJobRepository()


def get_data_plane_repository() -> SqlAlchemyDataPlaneRepository:
    return SqlAlchemyDataPlaneRepository()


def get_data_plane_selector() -> DataPlaneSelector:
    return DataPlaneSelector(
        settings=load_platform_settings(),
        repository=get_data_plane_repository(),
    )


def get_trusted_teaching_brief_resolver() -> TrustedTeachingBriefResolver:
    return _UnavailableTrustedTeachingBriefResolver()


def get_cancellation_gateway() -> JobCancellationGateway:
    from deeptutor.teaching.processes import RuntimeCancellationGateway

    return RuntimeCancellationGateway(load_platform_settings())


def _server_job_id(tenant_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\0{idempotency_key}".encode()).hexdigest()
    return f"job-{digest[:48]}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_trusted_brief(
    request: ClassroomJobCreateRequest,
    context: TenantContext,
    trusted: TrustedTeachingBrief,
) -> None:
    brief = trusted.brief
    canonical_sha256 = canonical_teaching_brief_sha256(brief)
    request_matches_brief = (
        request.teaching_brief_id == brief.brief_id
        and hmac.compare_digest(request.teaching_brief_sha256, canonical_sha256)
        and request.classroom_mode == brief.classroom_mode
        and request.template_id == brief.template_policy.template_id
        and request.template_version == brief.template_policy.template_version
        and request.duration_minutes == brief.duration_minutes
    )
    if not request_matches_brief:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Teaching brief changed",
        )
    trusted_binding_is_valid = (
        hmac.compare_digest(brief.content_sha256, canonical_sha256)
        and brief.tenant_id == context.tenant_id
        and trusted.resource.tenant_id == context.tenant_id
        and trusted.resource.course_id == brief.course_id
        and trusted.resource.class_id == brief.target_class_id
    )
    if not trusted_binding_is_valid:
        raise TrustedTeachingBriefUnavailable()


def _resource_for_brief(tenant_id: str, brief: TeachingBrief) -> ResourceScope:
    return ResourceScope(
        tenant_id=tenant_id,
        course_id=brief.course_id,
        class_id=brief.target_class_id,
    )


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
    if details.job_kind != "generation":
        return False
    if details.resource_course_id is None or details.resource_class_id is None:
        return False
    resource = ResourceScope(
        tenant_id=context.tenant_id,
        course_id=details.resource_course_id,
        class_id=details.resource_class_id,
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


def _confirmation_matches_issued_outline(
    details: GenerationJobDetails,
    original: GenerationRequest,
    confirmed: OutlineBundle,
) -> bool:
    if details.result_payload is None:
        return False
    try:
        issued = OutlineBundle.model_validate_json(details.result_payload)
    except ValidationError:
        return False
    try:
        validate_outline_binding(
            issued,
            original,
            expected_confirmation_status="draft",
        )
    except ValueError:
        return False
    exclude_confirmation = {"confirmation_metadata"}
    issued_semantics = issued.model_dump(
        mode="json",
        by_alias=True,
        exclude=exclude_confirmation,
        exclude_none=True,
    )
    confirmed_semantics = confirmed.model_dump(
        mode="json",
        by_alias=True,
        exclude=exclude_confirmation,
        exclude_none=True,
    )
    return issued_semantics == confirmed_semantics


def _response(details: GenerationJobDetails) -> JobStatusResponse:
    terminal = details.status in TERMINAL_JOB_STATUSES
    failed_or_canceled = details.status in {"failed", "canceled"}
    return JobStatusResponse(
        job_id=details.job_id,
        job_kind=details.job_kind,
        phase=details.phase,
        status=details.status,
        progress_percent=100 if details.status == "succeeded" else details.progress_percent,
        waiting_reason=None if terminal else details.waiting_reason,
        cancellable=not terminal,
        retryable=failed_or_canceled,
        outline=_outline_payload(details),
        error_category=details.error_category if failed_or_canceled else None,
        error_code=details.error_code if failed_or_canceled else None,
        retry_of_job_id=details.retry_of_job_id,
        export_format=details.export_format,
        download_ready=details.job_kind == "export" and details.status == "succeeded",
    )


@router.get(
    "/system/classroom-jobs/{tenant_id}/{job_id}/binding",
    response_model=GenerationBindingSnapshotResponse,
    dependencies=[Depends(require_platform_admin)],
)
async def generation_binding_snapshot(
    tenant_id: Annotated[
        str,
        Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
    ],
    job_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
    ],
    repository: SqlAlchemyGenerationJobRepository = Depends(get_job_repository),
    data_plane_repository: SqlAlchemyDataPlaneRepository = Depends(get_data_plane_repository),
) -> GenerationBindingSnapshotResponse:
    """Return the secret-free immutable route binding for one generation job."""

    details = await repository.get_job_details(tenant_id, job_id)
    if details is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    mode = details.data_plane_mode
    expected_tenant_id = None if mode == "shared" else details.tenant_id
    expected_owner_key = "shared" if mode == "shared" else details.tenant_id
    route_audit = (
        await data_plane_repository.resolve_job_route_audit(
            tenant_id=details.tenant_id,
            job_id=details.job_id,
            phase=details.phase,
            expected_attempt_count=details.attempt_count,
            expected_data_plane_mode=mode,
            expected_route_id=details.data_plane_route_id,
            expected_provider_profile_id=details.provider_profile_id,
            expected_worker_pool_ref=details.worker_pool_ref,
            expected_queue_ref=details.queue_ref,
        )
        if mode in {"shared", "dedicated"} and details.attempt_count > 0
        else None
    )
    if (
        route_audit is None
        or route_audit.data_plane_mode != mode
        or route_audit.attempt_count != details.attempt_count
        or route_audit.shared_attempt_count + route_audit.dedicated_attempt_count
        != details.attempt_count
        or route_audit.selected_attempt_count + route_audit.unavailable_attempt_count
        != details.attempt_count
        or (mode == "shared" and route_audit.shared_attempt_count != details.attempt_count)
        or (mode == "shared" and route_audit.dedicated_attempt_count != 0)
        or (mode == "dedicated" and route_audit.dedicated_attempt_count != details.attempt_count)
        or (mode == "dedicated" and route_audit.shared_attempt_count != 0)
        or (
            details.status == "succeeded"
            and (
                route_audit.selected_attempt_count < 1
                or route_audit.final_phase_selected is not True
            )
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Generation data plane binding unavailable",
        )
    classroom_version_id = (
        details.result_ref
        if details.job_kind == "generation"
        and details.phase == "content"
        and details.status == "succeeded"
        else None
    )
    return GenerationBindingSnapshotResponse(
        tenant_id=details.tenant_id,
        job_id=details.job_id,
        job_kind=details.job_kind,
        phase=details.phase,
        status=details.status,
        progress_percent=details.progress_percent,
        classroom_version_id=classroom_version_id,
        data_plane_route_id=details.data_plane_route_id,
        provider_profile_id=details.provider_profile_id,
        worker_pool_ref=details.worker_pool_ref,
        queue_ref=details.queue_ref,
        data_plane_mode=mode,
        route_tenant_id=expected_tenant_id,
        route_owner_key=expected_owner_key,
        provider_scope=mode,
        provider_tenant_id=expected_tenant_id,
        provider_owner_key=expected_owner_key,
        attempt_count=route_audit.attempt_count,
        shared_route_attempt_count=route_audit.shared_attempt_count,
        dedicated_route_attempt_count=route_audit.dedicated_attempt_count,
        selected_route_attempt_count=route_audit.selected_attempt_count,
        unavailable_route_attempt_count=route_audit.unavailable_attempt_count,
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
        data_plane_mode=details.data_plane_mode,
        data_plane_route_id=details.data_plane_route_id,
        provider_profile_id=details.provider_profile_id,
        worker_pool_ref=details.worker_pool_ref,
        queue_ref=details.queue_ref,
        request_payload=details.request_payload,
        classroom_draft_id=details.classroom_draft_id,
        batch_id=details.batch_id,
        resource_course_id=details.resource_course_id,
        resource_class_id=details.resource_class_id,
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
    brief_resolver: TrustedTeachingBriefResolver = Depends(get_trusted_teaching_brief_resolver),
) -> JobStatusResponse:
    try:
        job_id = _server_job_id(context.tenant_id, request.idempotency_key)
        public_request_sha256 = hashlib.sha256(canonical_json_bytes(request)).hexdigest()
        existing = await repository.get_job_details(context.tenant_id, job_id)
        if existing is not None:
            if (
                existing.job_kind != "generation"
                or existing.actor_id != context.user_id
                or existing.idempotency_key != request.idempotency_key
                or existing.public_request_sha256 != public_request_sha256
            ):
                raise IdempotencyConflict()
            return _response(existing)
        trusted = await brief_resolver.resolve(
            context=context,
            teaching_brief_id=request.teaching_brief_id,
            teaching_brief_sha256=request.teaching_brief_sha256,
            phase=request.phase,
            classroom_mode=request.classroom_mode,
            requested_exports=tuple(request.requested_exports),
        )
        if trusted is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Teaching brief not found",
            )
        _validate_trusted_brief(request, context, trusted)
        _require_create_access(
            context,
            classroom_mode=request.classroom_mode,
            brief=trusted.brief,
        )
        selection = await selector.resolve(context.tenant_id)
        if selection is None:
            raise DataPlaneUnavailable()
        generation = request.to_generation_request(
            context=context,
            selection=selection,
            trusted=trusted,
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
                quota_units=trusted.quota_units,
                actor_id=context.user_id,
                owner_id=context.user_id,
                visibility=trusted.visibility,
                request_id=generation.request_id,
                idempotency_key=generation.idempotency_key,
                request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                data_plane_mode=selection.mode,
                data_plane_route_id=selection.route_ref,
                provider_profile_id=selection.provider_profile_ref,
                worker_pool_ref=selection.worker_pool_ref,
                queue_ref=selection.queue_ref,
                request_payload=payload,
                classroom_draft_id=trusted.classroom_draft_id,
                resource_course_id=trusted.resource.course_id,
                resource_class_id=trusted.resource.class_id,
                public_request_sha256=public_request_sha256,
            )
        )
    except TrustedTeachingBriefUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trusted teaching brief unavailable",
        ) from None
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Job unavailable"
        )
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
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Outline cannot be confirmed"
        )
    original = _parse_request(details)
    if not isinstance(original, GenerationRequest):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Outline cannot be confirmed"
        )
    server_confirmed_outline = confirmation.confirmed_outline.model_copy(
        update={
            "confirmation_metadata": OutlineConfirmationMetadata(
                status="confirmed",
                confirmed_at=_utc_now(),
                confirmed_by=context.user_id,
            )
        }
    )
    if not _confirmation_matches_issued_outline(
        details,
        original,
        server_confirmed_outline,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Outline cannot be confirmed"
        )
    content_payload = original.model_dump(mode="json", by_alias=True, exclude_none=True)
    content_payload.update(
        phase="content",
        confirmedOutline=server_confirmed_outline.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        ),
        confirmedOutlineSha256=canonical_outline_sha256(server_confirmed_outline),
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Outline cannot be confirmed"
        )
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
    public_request_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "retryOfJobId": job_id,
                "request": retry.model_dump(mode="json", by_alias=True),
            }
        )
    ).hexdigest()
    try:
        retried = build_explicit_retry_request(
            _generation_job_request(details),
            job_id=new_job_id,
            request_id=retry.request_id,
            idempotency_key=retry.idempotency_key,
            actor_id=context.user_id,
            public_request_sha256=public_request_sha256,
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Job unavailable"
        )
    return _response(updated)


__all__ = [
    "TrustedTeachingBrief",
    "TrustedTeachingBriefResolver",
    "get_cancellation_gateway",
    "get_data_plane_selector",
    "get_job_repository",
    "get_trusted_teaching_brief_resolver",
    "router",
]
