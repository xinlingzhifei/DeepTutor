"""PostgreSQL repository for atomic tenant jobs, quota, and outbox writes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.job_errors import retry_delay_seconds
from deeptutor.teaching.job_route_binding import (
    DataPlaneBindingUnavailable,
    lock_active_job_binding,
)
from deeptutor.teaching.models import Tenant
from deeptutor.teaching.models.jobs import (
    LEASED_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    ArtifactPromotionState,
    ClassroomArtifact,
    ClassroomVersion,
    GenerationJob,
    GenerationQueue,
    GenerationSlot,
    InvalidJobTransition,
    OutboxMessage,
    QuotaLedger,
    require_job_transition,
)
from deeptutor.teaching.quota import reserve_quota
from deeptutor.teaching.scheduler import PRIORITY_RANK, ClaimedGenerationJob, slot_pool_for
from deeptutor.teaching.schema_names import tenant_schema_name

_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")


def _required(value: str, name: str, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _event_id(tenant_id: str, job_id: str, phase: str) -> str:
    value = f"{tenant_id}\0{job_id}\0{phase}".encode()
    return hashlib.sha256(value).hexdigest()


def _reservation_id(job_id: str) -> str:
    return hashlib.sha256(f"reserve\0{job_id}".encode()).hexdigest()


def _quota_event_id(entry_type: str, job_id: str) -> str:
    return hashlib.sha256(f"{entry_type}\0{job_id}".encode()).hexdigest()


def _artifact_id(job_id: str, relative_name: str) -> str:
    return hashlib.sha256(f"{job_id}\0{relative_name}".encode()).hexdigest()


def _tenant_table(tenant_id: str, table_name: str) -> str:
    if table_name not in {"generation_jobs", "quota_ledger"}:
        raise ValueError("tenant table is invalid")
    return f'"{tenant_schema_name(tenant_id)}"."{table_name}"'


def _opaque_route_id(value: str) -> str:
    route_id = _required(value, "data_plane_route_id", 63)
    if ":" in route_id or any(character.isspace() for character in route_id):
        raise ValueError("data_plane_route_id is invalid")
    return route_id


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.now()))
    if not isinstance(value, datetime):
        raise RuntimeError("database clock is unavailable")
    return value


@dataclass(frozen=True, slots=True)
class GenerationJobRequest:
    tenant_id: str
    job_id: str
    job_kind: str
    phase: str
    export_format: str | None
    priority: str
    quota_units: int
    actor_id: str
    owner_id: str
    visibility: str
    request_id: str
    idempotency_key: str
    request_sha256: str
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str
    request_payload: str
    classroom_draft_id: str | None = None
    batch_id: str | None = None
    resource_course_id: str | None = None
    resource_class_id: str | None = None
    public_request_sha256: str | None = None
    max_attempts: int = 5
    retry_of_job_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.tenant_id, "tenant_id", 64)
        _required(self.job_id, "job_id", 64)
        _required(self.actor_id, "actor_id", 128)
        _required(self.owner_id, "owner_id", 128)
        _required(self.request_id, "request_id", 64)
        _required(self.idempotency_key, "idempotency_key", 128)
        _opaque_route_id(self.data_plane_route_id)
        _required(self.provider_profile_id, "provider_profile_id", 63)
        _required(self.worker_pool_ref, "worker_pool_ref", 128)
        _required(self.queue_ref, "queue_ref", 128)
        _required(self.request_payload, "request_payload", 1_000_000)
        payload_error: ValueError | None = None
        try:
            parsed_payload = json.loads(self.request_payload)
        except (json.JSONDecodeError, UnicodeError):
            payload_error = ValueError("request_payload must be canonical JSON")
        if payload_error is not None:
            raise payload_error
        if not isinstance(parsed_payload, dict):
            raise ValueError("request_payload must be a JSON object")
        canonical_payload = json.dumps(
            parsed_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if self.request_payload != canonical_payload:
            raise ValueError("request_payload must be canonical JSON")
        if self.classroom_draft_id is not None:
            _required(self.classroom_draft_id, "classroom_draft_id", 64)
        if self.batch_id is not None:
            _required(self.batch_id, "batch_id", 64)
        if (self.resource_course_id is None) != (self.resource_class_id is None):
            raise ValueError("resource course and class IDs must be provided together")
        if self.resource_course_id is not None and self.resource_class_id is not None:
            _required(self.resource_course_id, "resource_course_id", 64)
            _required(self.resource_class_id, "resource_class_id", 64)
        if self.public_request_sha256 is not None and (
            len(self.public_request_sha256) != 64
            or any(character not in _LOWER_HEX_DIGITS for character in self.public_request_sha256)
        ):
            raise ValueError("public_request_sha256 must be a lowercase SHA-256 hex digest")
        if self.retry_of_job_id is not None:
            _required(self.retry_of_job_id, "retry_of_job_id", 64)
            if self.retry_of_job_id == self.job_id:
                raise ValueError("retry_of_job_id must differ from job_id")
        if self.visibility not in {"private", "class", "tenant"}:
            raise ValueError("visibility is invalid")
        if self.priority not in PRIORITY_RANK:
            raise ValueError("priority is invalid")
        if (
            isinstance(self.quota_units, bool)
            or not isinstance(self.quota_units, int)
            or self.quota_units <= 0
        ):
            raise ValueError("quota_units must be a positive integer")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts <= 0
        ):
            raise ValueError("max_attempts must be a positive integer")
        if len(self.request_sha256) != 64 or any(
            character not in _LOWER_HEX_DIGITS for character in self.request_sha256
        ):
            raise ValueError("request_sha256 must be a lowercase SHA-256 hex digest")
        payload_sha256 = hashlib.sha256(self.request_payload.encode("utf-8")).hexdigest()
        if self.request_sha256 != payload_sha256:
            raise ValueError("request_sha256 does not match request_payload")
        expected_phase = (
            self.phase in {"outline", "content"}
            if self.job_kind == "generation"
            else self.phase == "export"
        )
        if not expected_phase:
            raise ValueError("job kind and phase are inconsistent")
        slot_pool_for(self.job_kind, self.export_format)

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK[self.priority]

    @property
    def slot_pool(self) -> str:
        return slot_pool_for(self.job_kind, self.export_format)


def build_explicit_retry_request(
    original: GenerationJobRequest,
    *,
    job_id: str,
    request_id: str,
    idempotency_key: str,
    actor_id: str | None = None,
    public_request_sha256: str | None = None,
) -> GenerationJobRequest:
    """Clone immutable work only under a completely new public identity."""

    if (
        job_id == original.job_id
        or request_id == original.request_id
        or idempotency_key == original.idempotency_key
    ):
        raise ValueError("explicit retry requires a new job identity")
    try:
        payload = json.loads(original.request_payload)
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("original retry payload is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("original retry payload is invalid")
    job_key = "jobId" if "jobId" in payload else "job_id" if "job_id" in payload else None
    idempotency_key_name = (
        "idempotencyKey"
        if "idempotencyKey" in payload
        else "idempotency_key"
        if "idempotency_key" in payload
        else None
    )
    if job_key is None or idempotency_key_name is None:
        raise ValueError("original retry payload is missing its job identity")
    payload[job_key] = job_id
    payload[idempotency_key_name] = idempotency_key
    if "requestId" in payload:
        payload["requestId"] = request_id
    elif "request_id" in payload:
        payload["request_id"] = request_id
    retry_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return replace(
        original,
        job_id=job_id,
        request_id=request_id,
        idempotency_key=idempotency_key,
        request_payload=retry_payload,
        request_sha256=hashlib.sha256(retry_payload.encode()).hexdigest(),
        actor_id=actor_id if actor_id is not None else original.actor_id,
        public_request_sha256=public_request_sha256,
        retry_of_job_id=original.job_id,
    )


@dataclass(frozen=True, slots=True)
class GenerationJobRecord:
    tenant_id: str
    job_id: str
    job_kind: str
    phase: str
    status: str
    priority: int
    quota_units: int


@dataclass(frozen=True, slots=True)
class GenerationJobDetails:
    tenant_id: str
    job_id: str
    job_kind: str
    phase: str
    export_format: str | None
    status: str
    priority: int
    quota_units: int
    actor_id: str
    owner_id: str
    visibility: str
    request_id: str
    idempotency_key: str
    classroom_draft_id: str | None
    batch_id: str | None
    resource_course_id: str | None
    resource_class_id: str | None
    public_request_sha256: str | None
    request_sha256: str
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str
    request_payload: str
    progress_percent: int
    waiting_reason: str | None
    cancel_requested: bool
    error_category: str | None
    error_code: str | None
    result_payload: str | None
    result_ref: str | None
    retry_of_job_id: str | None


@dataclass(frozen=True, slots=True)
class ExportArtifactRecord:
    relative_name: str
    object_key: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class ClaimedJobPayload:
    request_payload: str
    request_sha256: str
    idempotency_key: str
    export_format: str | None
    cancel_requested: bool
    dsl_repair_attempts: int


@dataclass(frozen=True, slots=True)
class PromotionTarget:
    classroom_id: str
    version_number: int
    manifest_sha256: str | None
    status: str


@dataclass(frozen=True, slots=True)
class MaterializedArtifactInput:
    relative_name: str
    object_key: str
    sha256: str
    size_bytes: int
    mime_type: str
    artifact_kind: str


@dataclass(frozen=True, slots=True)
class CancellationRequest:
    tenant_id: str
    job_id: str
    running: bool
    phase: str
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str


@dataclass(frozen=True, slots=True)
class ReapedJob:
    tenant_id: str
    job_id: str
    terminal_status: str | None
    next_attempt_at: datetime | None


class JobLeaseLost(RuntimeError):
    """A stale worker attempted to mutate a fenced claim."""


class JobAlreadyTerminal(RuntimeError):
    """Another actor won the first-terminal-wins race."""


class TenantUnavailable(RuntimeError):
    """The job owner is not an active platform tenant."""


class IdempotencyConflict(RuntimeError):
    """An idempotency key is already bound to a different immutable request."""

    def __init__(self) -> None:
        super().__init__("generation job idempotency conflict")


class ContentRequeueConflict(RuntimeError):
    """The outline queue claim or one of its fenced slots is still present."""

    def __init__(self) -> None:
        super().__init__("outline claim must be released before content requeue")


def require_repository_transition(
    job_kind: str,
    current_status: str,
    target_status: str,
) -> None:
    """Allow only transitions that preserve the current lease class."""

    require_job_transition(job_kind, current_status, target_status)
    if target_status in TERMINAL_JOB_STATUSES:
        raise InvalidJobTransition("terminal transitions require completion API")
    if (current_status in LEASED_JOB_STATUSES) != (target_status in LEASED_JOB_STATUSES):
        raise InvalidJobTransition("lease boundary requires lifecycle API")


class SqlAlchemyGenerationJobRepository:
    """Use one translated PostgreSQL session for tenant and platform writes."""

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._configured_engine = engine

    def _engine(self) -> AsyncEngine:
        return self._configured_engine or get_platform_engine()

    def _session_factory(self, tenant_id: str) -> async_sessionmaker[AsyncSession]:
        translated_engine = self._engine().execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        return async_sessionmaker(translated_engine, expire_on_commit=False)

    @staticmethod
    async def _lock_active_tenant(
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        active_tenant = await session.scalar(
            select(Tenant.id)
            .where(
                Tenant.id == tenant_id,
                Tenant.status == "active",
            )
            .with_for_update()
        )
        if active_tenant is None:
            raise TenantUnavailable("tenant is unavailable")

    async def grant_quota(
        self,
        tenant_id: str,
        *,
        grant_id: str,
        units: int,
    ) -> int:
        _required(tenant_id, "tenant_id", 64)
        _required(grant_id, "grant_id", 64)
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise ValueError("units must be a positive integer")
        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            async with session.begin():
                await self._lock_active_tenant(session, tenant_id)
                session.add(
                    QuotaLedger(
                        id=grant_id,
                        tenant_id=tenant_id,
                        job_id=None,
                        entry_type="grant",
                        units=units,
                    )
                )
                await session.flush()
                return await self._quota_balance(session, tenant_id)

    @staticmethod
    async def _quota_balance(session: AsyncSession, tenant_id: str) -> int:
        balance = await session.scalar(
            select(func.coalesce(func.sum(QuotaLedger.units), 0)).where(
                QuotaLedger.tenant_id == tenant_id
            )
        )
        if not isinstance(balance, int):
            raise RuntimeError("quota balance is unavailable")
        return balance

    async def quota_balance(self, tenant_id: str) -> int:
        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            return await self._quota_balance(session, tenant_id)

    async def create_job_and_reserve(
        self,
        request: GenerationJobRequest,
    ) -> GenerationJobRecord:
        session_factory = self._session_factory(request.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:idempotency_lock_key, 0))"
                    ),
                    {
                        "idempotency_lock_key": hashlib.sha256(
                            (
                                "generation-job-idempotency\0"
                                f"{request.tenant_id}\0{request.idempotency_key}"
                            ).encode()
                        ).hexdigest()
                    },
                )
                existing = await session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.tenant_id == request.tenant_id,
                        GenerationJob.idempotency_key == request.idempotency_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if not self._matches_idempotent_request(existing, request):
                        raise IdempotencyConflict()
                    return self._record(existing)
                if not await lock_active_job_binding(
                    session,
                    tenant_id=request.tenant_id,
                    data_plane_route_id=request.data_plane_route_id,
                    provider_profile_id=request.provider_profile_id,
                    worker_pool_ref=request.worker_pool_ref,
                    queue_ref=request.queue_ref,
                ):
                    raise DataPlaneBindingUnavailable()
                balance = await self._quota_balance(session, request.tenant_id)
                reserve_quota(
                    balance=balance,
                    requested_units=request.quota_units,
                )
                now = await _database_now(session)
                job = GenerationJob(
                    id=request.job_id,
                    tenant_id=request.tenant_id,
                    job_kind=request.job_kind,
                    phase=request.phase,
                    export_format=request.export_format,
                    status="created",
                    priority=request.priority_rank,
                    quota_units=request.quota_units,
                    actor_id=request.actor_id,
                    owner_id=request.owner_id,
                    visibility=request.visibility,
                    request_id=request.request_id,
                    idempotency_key=request.idempotency_key,
                    classroom_draft_id=request.classroom_draft_id,
                    batch_id=request.batch_id,
                    resource_course_id=request.resource_course_id,
                    resource_class_id=request.resource_class_id,
                    public_request_sha256=request.public_request_sha256,
                    request_sha256=request.request_sha256.lower(),
                    data_plane_route_id=request.data_plane_route_id,
                    provider_profile_id=request.provider_profile_id,
                    worker_pool_ref=request.worker_pool_ref,
                    queue_ref=request.queue_ref,
                    request_payload=request.request_payload,
                    max_attempts=request.max_attempts,
                    retry_of_job_id=request.retry_of_job_id,
                    next_attempt_at=now,
                )
                session.add(job)
                await session.flush()
                session.add(
                    QuotaLedger(
                        id=_reservation_id(request.job_id),
                        tenant_id=request.tenant_id,
                        job_id=request.job_id,
                        entry_type="reserve",
                        units=-request.quota_units,
                    )
                )
                transition = await session.execute(
                    update(GenerationJob)
                    .where(
                        GenerationJob.id == request.job_id,
                        GenerationJob.tenant_id == request.tenant_id,
                        GenerationJob.status == "created",
                    )
                    .values(status="quota_reserved", updated_at=now)
                )
                if transition.rowcount != 1:
                    raise InvalidJobTransition("job reservation transition failed")
                session.add(
                    OutboxMessage(
                        event_id=_event_id(
                            request.tenant_id,
                            request.job_id,
                            request.phase,
                        ),
                        tenant_id=request.tenant_id,
                        job_id=request.job_id,
                        job_kind=request.job_kind,
                        phase=request.phase,
                        data_plane_route_id=request.data_plane_route_id,
                        provider_profile_id=request.provider_profile_id,
                        worker_pool_ref=request.worker_pool_ref,
                        queue_ref=request.queue_ref,
                        slot_pool=request.slot_pool,
                        priority=request.priority_rank,
                        event_type="generation_job.ready",
                        payload=request.request_sha256.lower(),
                        available_at=now,
                    )
                )
                await session.flush()
                return GenerationJobRecord(
                    tenant_id=request.tenant_id,
                    job_id=request.job_id,
                    job_kind=request.job_kind,
                    phase=request.phase,
                    status="quota_reserved",
                    priority=request.priority_rank,
                    quota_units=request.quota_units,
                )

    @staticmethod
    def _matches_idempotent_request(
        existing: GenerationJob,
        request: GenerationJobRequest,
    ) -> bool:
        if existing.public_request_sha256 is not None or request.public_request_sha256 is not None:
            return (
                existing.id == request.job_id
                and existing.job_kind == request.job_kind
                and existing.actor_id == request.actor_id
                and existing.idempotency_key == request.idempotency_key
                and existing.public_request_sha256 == request.public_request_sha256
            )
        return (
            existing.id == request.job_id
            and existing.job_kind == request.job_kind
            and existing.phase == request.phase
            and existing.export_format == request.export_format
            and existing.priority == request.priority_rank
            and existing.quota_units == request.quota_units
            and existing.actor_id == request.actor_id
            and existing.owner_id == request.owner_id
            and existing.visibility == request.visibility
            and existing.request_id == request.request_id
            and existing.classroom_draft_id == request.classroom_draft_id
            and existing.batch_id == request.batch_id
            and existing.resource_course_id == request.resource_course_id
            and existing.resource_class_id == request.resource_class_id
            and existing.request_sha256 == request.request_sha256.lower()
            and existing.data_plane_route_id == request.data_plane_route_id
            and existing.provider_profile_id == request.provider_profile_id
            and existing.worker_pool_ref == request.worker_pool_ref
            and existing.queue_ref == request.queue_ref
            and existing.request_payload == request.request_payload
            and existing.max_attempts == request.max_attempts
            and existing.retry_of_job_id == request.retry_of_job_id
        )

    @staticmethod
    def _record(job: GenerationJob) -> GenerationJobRecord:
        return GenerationJobRecord(
            tenant_id=job.tenant_id,
            job_id=job.id,
            job_kind=job.job_kind,
            phase=job.phase,
            status=job.status,
            priority=job.priority,
            quota_units=job.quota_units,
        )

    async def transition(
        self,
        tenant_id: str,
        job_id: str,
        *,
        expected_status: str,
        target_status: str,
    ) -> bool:
        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job_kind = await session.scalar(
                    select(GenerationJob.job_kind).where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == tenant_id,
                    )
                )
                if job_kind is None:
                    return False
                require_repository_transition(
                    job_kind,
                    expected_status,
                    target_status,
                )
                if expected_status == "awaiting_confirmation" and target_status == "queued":
                    raise InvalidJobTransition("content confirmation requires atomic requeue")
                result = await session.execute(
                    update(GenerationJob)
                    .where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == tenant_id,
                        GenerationJob.status == expected_status,
                    )
                    .values(
                        status=target_status,
                        updated_at=func.now(),
                    )
                )
                return result.rowcount == 1

    async def requeue_confirmed_content(
        self,
        tenant_id: str,
        job_id: str,
        *,
        request_payload: str,
        request_sha256: str,
    ) -> bool:
        """Move a confirmed outline to content and append its second event."""

        _required(request_payload, "request_payload", 1_000_000)
        if hashlib.sha256(request_payload.encode()).hexdigest() != request_sha256:
            raise ValueError("request_sha256 does not match request_payload")
        try:
            parsed_payload = json.loads(request_payload)
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("request_payload must be canonical JSON") from None
        if not isinstance(parsed_payload, dict) or parsed_payload.get("phase") != "content":
            raise ValueError("confirmed request payload must use content phase")
        if (
            json.dumps(
                parsed_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            != request_payload
        ):
            raise ValueError("request_payload must be canonical JSON")

        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == tenant_id,
                        GenerationJob.job_kind == "generation",
                        GenerationJob.phase == "outline",
                        GenerationJob.status == "awaiting_confirmation",
                    )
                    .with_for_update()
                )
                if job is None:
                    return False
                if (
                    parsed_payload.get("tenantId") != tenant_id
                    or parsed_payload.get("jobId") != job_id
                    or parsed_payload.get("requestId") != job.request_id
                    or parsed_payload.get("idempotencyKey") != job.idempotency_key
                    or parsed_payload.get("dataPlaneRouteId") != job.data_plane_route_id
                ):
                    raise ValueError("confirmed request identity does not match the job")
                if not await lock_active_job_binding(
                    session,
                    tenant_id=tenant_id,
                    data_plane_route_id=job.data_plane_route_id,
                    provider_profile_id=job.provider_profile_id,
                    worker_pool_ref=job.worker_pool_ref,
                    queue_ref=job.queue_ref,
                ):
                    raise DataPlaneBindingUnavailable()
                old_queue = await session.scalar(
                    select(GenerationQueue.job_id)
                    .where(
                        GenerationQueue.tenant_id == tenant_id,
                        GenerationQueue.job_id == job_id,
                    )
                    .with_for_update()
                )
                claimed_slot = await session.scalar(
                    select(GenerationSlot.id)
                    .where(
                        GenerationSlot.claimed_tenant_id == tenant_id,
                        GenerationSlot.claimed_job_id == job_id,
                    )
                    .with_for_update()
                )
                if old_queue is not None or claimed_slot is not None:
                    raise ContentRequeueConflict()
                now = await _database_now(session)
                job.phase = "content"
                job.status = "queued"
                job.request_payload = request_payload
                job.request_sha256 = request_sha256
                job.next_attempt_at = now
                job.waiting_reason = None
                job.progress_percent = max(job.progress_percent, 50)
                job.updated_at = now
                session.add(
                    OutboxMessage(
                        event_id=_event_id(tenant_id, job_id, "content"),
                        tenant_id=tenant_id,
                        job_id=job_id,
                        job_kind="generation",
                        phase="content",
                        data_plane_route_id=job.data_plane_route_id,
                        provider_profile_id=job.provider_profile_id,
                        worker_pool_ref=job.worker_pool_ref,
                        queue_ref=job.queue_ref,
                        slot_pool="generation",
                        priority=job.priority,
                        event_type="generation_job.content_ready",
                        payload=request_sha256,
                        available_at=now,
                    )
                )
                await session.flush()
                return True

    async def get_job(
        self,
        tenant_id: str,
        job_id: str,
    ) -> GenerationJobRecord | None:
        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            job = await session.scalar(
                select(GenerationJob).where(
                    GenerationJob.id == job_id,
                    GenerationJob.tenant_id == tenant_id,
                )
            )
            if job is None:
                return None
            return self._record(job)

    async def get_job_details(
        self,
        tenant_id: str,
        job_id: str,
    ) -> GenerationJobDetails | None:
        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            job = await session.scalar(
                select(GenerationJob).where(
                    GenerationJob.id == job_id,
                    GenerationJob.tenant_id == tenant_id,
                )
            )
            if job is None:
                return None
            return GenerationJobDetails(
                tenant_id=job.tenant_id,
                job_id=job.id,
                job_kind=job.job_kind,
                phase=job.phase,
                export_format=job.export_format,
                status=job.status,
                priority=job.priority,
                quota_units=job.quota_units,
                actor_id=job.actor_id,
                owner_id=job.owner_id,
                visibility=job.visibility,
                request_id=job.request_id,
                idempotency_key=job.idempotency_key,
                classroom_draft_id=job.classroom_draft_id,
                batch_id=job.batch_id,
                resource_course_id=job.resource_course_id,
                resource_class_id=job.resource_class_id,
                public_request_sha256=job.public_request_sha256,
                request_sha256=job.request_sha256,
                data_plane_route_id=job.data_plane_route_id,
                provider_profile_id=job.provider_profile_id,
                worker_pool_ref=job.worker_pool_ref,
                queue_ref=job.queue_ref,
                request_payload=job.request_payload,
                progress_percent=job.progress_percent,
                waiting_reason=job.waiting_reason,
                cancel_requested=job.cancel_requested,
                error_category=job.error_category,
                error_code=job.error_code,
                result_payload=job.result_payload,
                result_ref=job.result_ref,
                retry_of_job_id=job.retry_of_job_id,
            )

    async def get_export_artifact(
        self,
        tenant_id: str,
        job_id: str,
    ) -> ExportArtifactRecord | None:
        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            artifact = await session.scalar(
                select(ClassroomArtifact).where(
                    ClassroomArtifact.tenant_id == tenant_id,
                    ClassroomArtifact.source_job_id == job_id,
                    ClassroomArtifact.artifact_kind == "export",
                )
            )
            if artifact is None:
                return None
            return ExportArtifactRecord(
                relative_name=artifact.relative_name,
                object_key=artifact.object_key,
                mime_type=artifact.mime_type,
            )

    @staticmethod
    async def _lock_claim(
        session: AsyncSession,
        claim: ClaimedGenerationJob,
        *,
        require_unexpired: bool = True,
    ) -> tuple[GenerationJob, GenerationQueue, tuple[GenerationSlot, GenerationSlot], datetime]:
        now = await _database_now(session)
        job = await session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.id == claim.job_id,
                GenerationJob.tenant_id == claim.tenant_id,
            )
            .with_for_update()
        )
        queue = await session.scalar(
            select(GenerationQueue)
            .where(
                GenerationQueue.job_id == claim.job_id,
                GenerationQueue.tenant_id == claim.tenant_id,
            )
            .with_for_update()
        )
        slots = tuple(
            (
                await session.scalars(
                    select(GenerationSlot)
                    .where(GenerationSlot.id.in_((claim.global_slot_id, claim.tenant_slot_id)))
                    .order_by(GenerationSlot.id)
                    .with_for_update()
                )
            ).all()
        )
        expected_slot_ids = {claim.global_slot_id, claim.tenant_slot_id}
        if (
            job is None
            or queue is None
            or len(slots) != 2
            or {slot.id for slot in slots} != expected_slot_ids
            or job.status not in LEASED_JOB_STATUSES
            or job.lease_owner != claim.lease_owner
            or job.lease_token != claim.lease_token
            or queue.status != "claimed"
            or queue.lease_owner != claim.lease_owner
            or queue.lease_token != claim.lease_token
            or any(
                slot.claimed_tenant_id != claim.tenant_id
                or slot.claimed_job_id != claim.job_id
                or slot.lease_owner != claim.lease_owner
                or slot.lease_token != claim.lease_token
                for slot in slots
            )
        ):
            raise JobLeaseLost("job lease fence no longer matches")
        expiries = (
            job.lease_expires_at,
            queue.lease_expires_at,
            *(slot.lease_expires_at for slot in slots),
        )
        if require_unexpired and any(expiry is None or expiry <= now for expiry in expiries):
            raise JobLeaseLost("job lease has expired")
        return job, queue, (slots[0], slots[1]), now

    @staticmethod
    def _release_slots(slots: tuple[GenerationSlot, GenerationSlot]) -> None:
        for slot in slots:
            slot.claimed_tenant_id = None
            slot.claimed_job_id = None
            slot.lease_owner = None
            slot.lease_token = None
            slot.lease_expires_at = None
            slot.heartbeat_at = None

    @staticmethod
    def _clear_job_lease(job: GenerationJob) -> None:
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = None

    @staticmethod
    def _requeue(
        queue: GenerationQueue,
        slots: tuple[GenerationSlot, GenerationSlot],
        *,
        available_at: datetime,
    ) -> None:
        queue.status = "queued"
        queue.available_at = available_at
        queue.claimed_at = None
        queue.lease_owner = None
        queue.lease_token = None
        queue.lease_expires_at = None
        queue.heartbeat_at = None
        SqlAlchemyGenerationJobRepository._release_slots(slots)

    async def load_claimed_payload(
        self,
        claim: ClaimedGenerationJob,
    ) -> ClaimedJobPayload:
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, _, _, _ = await self._lock_claim(session, claim)
                return ClaimedJobPayload(
                    request_payload=job.request_payload,
                    request_sha256=job.request_sha256,
                    idempotency_key=job.idempotency_key,
                    export_format=job.export_format,
                    cancel_requested=job.cancel_requested,
                    dsl_repair_attempts=job.dsl_repair_attempts,
                )

    async def heartbeat_claim(
        self,
        claim: ClaimedGenerationJob,
        *,
        lease_seconds: int = 60,
    ) -> datetime:
        if lease_seconds != 60:
            raise ValueError("worker lease must be exactly 60 seconds")
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, queue, slots, now = await self._lock_claim(session, claim)
                expires_at = now + timedelta(seconds=lease_seconds)
                for leased in (job, queue, *slots):
                    leased.heartbeat_at = now
                    leased.lease_expires_at = expires_at
                await session.flush()
                return expires_at

    async def transition_claim(
        self,
        claim: ClaimedGenerationJob,
        *,
        expected_status: str,
        target_status: str,
        progress_percent: int,
    ) -> None:
        if target_status not in LEASED_JOB_STATUSES:
            raise ValueError("claim transition must retain its lease")
        if not 0 <= progress_percent <= 100:
            raise ValueError("progress_percent is invalid")
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, _, _, now = await self._lock_claim(session, claim)
                if job.status != expected_status:
                    raise JobLeaseLost("job status no longer matches")
                job.status = target_status
                job.progress_percent = max(job.progress_percent, progress_percent)
                job.updated_at = now
                await session.flush()

    async def complete_outline(
        self,
        claim: ClaimedGenerationJob,
        *,
        result_payload: str,
    ) -> None:
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, queue, slots, now = await self._lock_claim(session, claim)
                if job.status != "generating_outline" or job.cancel_requested:
                    raise JobAlreadyTerminal("outline completion lost cancellation race")
                job.status = "awaiting_confirmation"
                job.result_payload = result_payload
                job.progress_percent = max(job.progress_percent, 50)
                job.updated_at = now
                self._clear_job_lease(job)
                await session.delete(queue)
                self._release_slots(slots)
                await session.flush()

    async def increment_dsl_repair(
        self,
        claim: ClaimedGenerationJob,
    ) -> int:
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, _, _, now = await self._lock_claim(session, claim)
                if job.dsl_repair_attempts >= 2:
                    raise ValueError("DSL repair budget is exhausted")
                job.dsl_repair_attempts += 1
                job.updated_at = now
                await session.flush()
                return job.dsl_repair_attempts

    async def retry_claim(
        self,
        claim: ClaimedGenerationJob,
        *,
        error_category: str,
        error_code: str,
        delay_seconds: int,
    ) -> bool:
        if not 0 <= delay_seconds <= 300:
            raise ValueError("retry delay is invalid")
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, queue, slots, now = await self._lock_claim(session, claim)
                job.error_category = error_category
                job.error_code = error_code
                job.updated_at = now
                if not job.cancel_requested and job.attempt_count < job.max_attempts:
                    available_at = now + timedelta(seconds=delay_seconds)
                    job.status = "queued"
                    job.next_attempt_at = available_at
                    job.waiting_reason = "retry_backoff"
                    self._clear_job_lease(job)
                    self._requeue(queue, slots, available_at=available_at)
                    await session.flush()
                    return True
                terminal_status = "canceled" if job.cancel_requested else "failed"
                await self._finish_failed_or_canceled(
                    session,
                    job,
                    queue,
                    slots,
                    now=now,
                    terminal_status=terminal_status,
                )
                return False

    @staticmethod
    async def _finish_failed_or_canceled(
        session: AsyncSession,
        job: GenerationJob,
        queue: GenerationQueue,
        slots: tuple[GenerationSlot, GenerationSlot],
        *,
        now: datetime,
        terminal_status: str,
    ) -> None:
        if terminal_status not in {"failed", "canceled"}:
            raise ValueError("terminal status is invalid")
        job.status = terminal_status
        job.completed_at = now
        job.canceled_at = now if terminal_status == "canceled" else None
        job.waiting_reason = None
        job.updated_at = now
        SqlAlchemyGenerationJobRepository._clear_job_lease(job)
        await session.delete(queue)
        SqlAlchemyGenerationJobRepository._release_slots(slots)
        session.add(
            QuotaLedger(
                id=_quota_event_id("release", job.id),
                tenant_id=job.tenant_id,
                job_id=job.id,
                entry_type="release",
                units=job.quota_units,
            )
        )
        await session.flush()

    async def fail_claim(
        self,
        claim: ClaimedGenerationJob,
        *,
        error_category: str,
        error_code: str,
    ) -> None:
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, queue, slots, now = await self._lock_claim(session, claim)
                job.error_category = error_category
                job.error_code = error_code
                await self._finish_failed_or_canceled(
                    session,
                    job,
                    queue,
                    slots,
                    now=now,
                    terminal_status="canceled" if job.cancel_requested else "failed",
                )

    async def cancel_claim(self, claim: ClaimedGenerationJob) -> None:
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, queue, slots, now = await self._lock_claim(session, claim)
                job.cancel_requested = True
                job.error_category = "canceled"
                job.error_code = "job_canceled"
                await self._finish_failed_or_canceled(
                    session,
                    job,
                    queue,
                    slots,
                    now=now,
                    terminal_status="canceled",
                )

    async def request_cancel(
        self,
        tenant_id: str,
        job_id: str,
    ) -> CancellationRequest | None:
        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                undelivered_messages = (
                    await session.scalars(
                        select(OutboxMessage)
                        .where(
                            OutboxMessage.tenant_id == tenant_id,
                            OutboxMessage.job_id == job_id,
                            OutboxMessage.delivered_at.is_(None),
                        )
                        .order_by(OutboxMessage.event_id)
                        .with_for_update()
                    )
                ).all()
                job = await session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                if job is None or job.status in TERMINAL_JOB_STATUSES:
                    return None
                job.cancel_requested = True
                job.updated_at = now
                request = CancellationRequest(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    running=job.status in LEASED_JOB_STATUSES,
                    phase=job.phase,
                    data_plane_route_id=job.data_plane_route_id,
                    provider_profile_id=job.provider_profile_id,
                    worker_pool_ref=job.worker_pool_ref,
                    queue_ref=job.queue_ref,
                )
                if request.running:
                    await session.flush()
                    return request
                queue = await session.scalar(
                    select(GenerationQueue)
                    .where(
                        GenerationQueue.tenant_id == tenant_id,
                        GenerationQueue.job_id == job_id,
                    )
                    .with_for_update()
                )
                if queue is not None:
                    await session.delete(queue)
                for message in undelivered_messages:
                    message.delivered_at = now
                job.status = "canceled"
                job.error_category = "canceled"
                job.error_code = "job_canceled"
                job.canceled_at = now
                job.completed_at = now
                job.waiting_reason = None
                self._clear_job_lease(job)
                session.add(
                    QuotaLedger(
                        id=_quota_event_id("release", job.id),
                        tenant_id=tenant_id,
                        job_id=job.id,
                        entry_type="release",
                        units=job.quota_units,
                    )
                )
                await session.flush()
                return request

    async def finish_requested_cancellation(
        self,
        tenant_id: str,
        job_id: str,
    ) -> bool:
        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                job = await session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                if job is None or job.status in TERMINAL_JOB_STATUSES:
                    return False
                if not job.cancel_requested:
                    raise ValueError("job cancellation was not requested")
                queue = await session.scalar(
                    select(GenerationQueue)
                    .where(
                        GenerationQueue.tenant_id == tenant_id,
                        GenerationQueue.job_id == job_id,
                    )
                    .with_for_update()
                )
                slots = tuple(
                    (
                        await session.scalars(
                            select(GenerationSlot)
                            .where(
                                GenerationSlot.claimed_tenant_id == tenant_id,
                                GenerationSlot.claimed_job_id == job_id,
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                if queue is None or len(slots) != 2:
                    raise JobLeaseLost("running cancellation lease is incomplete")
                job.status = "canceled"
                job.error_category = "canceled"
                job.error_code = "job_canceled"
                job.canceled_at = now
                job.completed_at = now
                job.waiting_reason = None
                job.updated_at = now
                self._clear_job_lease(job)
                await session.delete(queue)
                self._release_slots((slots[0], slots[1]))
                session.add(
                    QuotaLedger(
                        id=_quota_event_id("release", job.id),
                        tenant_id=tenant_id,
                        job_id=job.id,
                        entry_type="release",
                        units=job.quota_units,
                    )
                )
                await session.flush()
                return True

    async def prepare_promotion(
        self,
        claim: ClaimedGenerationJob,
        *,
        classroom_id: str,
    ) -> PromotionTarget:
        _required(classroom_id, "classroom_id", 128)
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, _, _, now = await self._lock_claim(session, claim)
                if job.cancel_requested or job.status != "materializing":
                    raise JobAlreadyTerminal("materialization lost cancellation race")
                existing = await session.scalar(
                    select(ArtifactPromotionState)
                    .where(ArtifactPromotionState.job_id == claim.job_id)
                    .with_for_update()
                )
                if existing is None:
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:classroom_key, 0))"),
                        {
                            "classroom_key": hashlib.sha256(
                                (
                                    "classroom-promotion\0"
                                    f"{claim.tenant_id}\0{classroom_id}"
                                ).encode()
                            ).hexdigest()
                        },
                    )
                    max_version = max(
                        int(
                            await session.scalar(
                                select(
                                    func.coalesce(func.max(ClassroomVersion.version_number), 0)
                                ).where(
                                    ClassroomVersion.tenant_id == claim.tenant_id,
                                    ClassroomVersion.classroom_id == classroom_id,
                                )
                            )
                            or 0
                        ),
                        int(
                            await session.scalar(
                                select(
                                    func.coalesce(
                                        func.max(ArtifactPromotionState.version_number), 0
                                    )
                                ).where(
                                    ArtifactPromotionState.tenant_id == claim.tenant_id,
                                    ArtifactPromotionState.classroom_id == classroom_id,
                                )
                            )
                            or 0
                        ),
                    )
                    existing = ArtifactPromotionState(
                        job_id=claim.job_id,
                        tenant_id=claim.tenant_id,
                        classroom_id=classroom_id,
                        version_number=max_version + 1,
                        status="prepared",
                        updated_at=now,
                    )
                    session.add(existing)
                    await session.flush()
                elif existing.classroom_id != classroom_id:
                    raise ValueError("promotion target conflicts with durable state")
                return PromotionTarget(
                    classroom_id=existing.classroom_id,
                    version_number=existing.version_number,
                    manifest_sha256=existing.manifest_sha256,
                    status=existing.status,
                )

    async def bind_promotion_manifest(
        self,
        claim: ClaimedGenerationJob,
        *,
        manifest_sha256: str,
    ) -> PromotionTarget:
        if len(manifest_sha256) != 64 or any(
            character not in _LOWER_HEX_DIGITS for character in manifest_sha256
        ):
            raise ValueError("manifest_sha256 is invalid")
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                await self._lock_claim(session, claim)
                state = await session.scalar(
                    select(ArtifactPromotionState)
                    .where(ArtifactPromotionState.job_id == claim.job_id)
                    .with_for_update()
                )
                if state is None:
                    raise ValueError("promotion target is not prepared")
                if state.manifest_sha256 not in {None, manifest_sha256}:
                    raise ValueError("promotion manifest conflicts with durable state")
                state.manifest_sha256 = manifest_sha256
                state.updated_at = await _database_now(session)
                await session.flush()
                return PromotionTarget(
                    classroom_id=state.classroom_id,
                    version_number=state.version_number,
                    manifest_sha256=state.manifest_sha256,
                    status=state.status,
                )

    async def mark_object_committed(
        self,
        claim: ClaimedGenerationJob,
        *,
        manifest_sha256: str,
    ) -> None:
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, _, _, now = await self._lock_claim(session, claim)
                if job.cancel_requested:
                    raise JobAlreadyTerminal("materialization lost cancellation race")
                state = await session.scalar(
                    select(ArtifactPromotionState)
                    .where(ArtifactPromotionState.job_id == claim.job_id)
                    .with_for_update()
                )
                if state is None or state.manifest_sha256 != manifest_sha256:
                    raise ValueError("object commit does not match prepared manifest")
                if state.status == "prepared":
                    state.status = "object_committed"
                    state.object_committed_at = now
                    state.updated_at = now
                await session.flush()

    async def finalize_generation(
        self,
        claim: ClaimedGenerationJob,
        *,
        classroom_version_id: str,
        document_sha256: str,
        media_manifest_sha256: str,
        manifest_sha256: str,
        artifacts: tuple[MaterializedArtifactInput, ...],
    ) -> None:
        _required(classroom_version_id, "classroom_version_id", 128)
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, queue, slots, now = await self._lock_claim(session, claim)
                if job.status != "materializing" or job.cancel_requested:
                    raise JobAlreadyTerminal("materialization lost terminal race")
                state = await session.scalar(
                    select(ArtifactPromotionState)
                    .where(ArtifactPromotionState.job_id == claim.job_id)
                    .with_for_update()
                )
                if (
                    state is None
                    or state.status != "object_committed"
                    or state.manifest_sha256 != manifest_sha256
                ):
                    raise ValueError("object publication is not durably committed")
                document_artifact = next(
                    (item for item in artifacts if item.artifact_kind == "dsl_json"),
                    None,
                )
                if document_artifact is None or document_artifact.sha256 != document_sha256:
                    raise ValueError("document artifact is missing")
                session.add(
                    ClassroomVersion(
                        id=classroom_version_id,
                        tenant_id=claim.tenant_id,
                        classroom_id=state.classroom_id,
                        version_number=state.version_number,
                        generation_job_id=claim.job_id,
                        document_sha256=document_sha256,
                        media_manifest_sha256=media_manifest_sha256,
                        document_object_key=document_artifact.object_key,
                    )
                )
                await session.flush()
                for artifact in artifacts:
                    session.add(
                        ClassroomArtifact(
                            id=_artifact_id(claim.job_id, artifact.relative_name),
                            tenant_id=claim.tenant_id,
                            source_job_id=claim.job_id,
                            classroom_version_id=classroom_version_id,
                            artifact_kind=artifact.artifact_kind,
                            relative_name=artifact.relative_name,
                            object_key=artifact.object_key,
                            sha256=artifact.sha256,
                            size_bytes=artifact.size_bytes,
                            mime_type=artifact.mime_type,
                            input_document_sha256=None,
                            input_media_manifest_sha256=None,
                        )
                    )
                session.add(
                    QuotaLedger(
                        id=_quota_event_id("settle", job.id),
                        tenant_id=job.tenant_id,
                        job_id=job.id,
                        entry_type="settle",
                        units=0,
                    )
                )
                state.status = "finalized"
                state.finalized_at = now
                state.updated_at = now
                job.status = "succeeded"
                job.progress_percent = 100
                job.result_ref = classroom_version_id
                job.artifact_manifest_ref = manifest_sha256
                job.completed_at = now
                job.updated_at = now
                self._clear_job_lease(job)
                await session.delete(queue)
                self._release_slots(slots)
                await session.flush()

    async def finalize_export(
        self,
        claim: ClaimedGenerationJob,
        *,
        input_document_sha256: str,
        input_media_manifest_sha256: str,
        manifest_sha256: str,
        artifact: MaterializedArtifactInput,
    ) -> None:
        session_factory = self._session_factory(claim.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                job, queue, slots, now = await self._lock_claim(session, claim)
                if job.status != "materializing" or job.cancel_requested:
                    raise JobAlreadyTerminal("export materialization lost terminal race")
                state = await session.scalar(
                    select(ArtifactPromotionState)
                    .where(ArtifactPromotionState.job_id == claim.job_id)
                    .with_for_update()
                )
                if (
                    state is None
                    or state.status != "object_committed"
                    or state.manifest_sha256 != manifest_sha256
                ):
                    raise ValueError("object publication is not durably committed")
                source_version = await session.scalar(
                    select(ClassroomVersion)
                    .where(
                        ClassroomVersion.tenant_id == claim.tenant_id,
                        ClassroomVersion.document_sha256 == input_document_sha256,
                        ClassroomVersion.media_manifest_sha256 == input_media_manifest_sha256,
                    )
                    .order_by(ClassroomVersion.version_number.desc())
                    .limit(1)
                )
                if source_version is None:
                    raise ValueError("pinned classroom version is unavailable")
                session.add(
                    ClassroomArtifact(
                        id=_artifact_id(claim.job_id, artifact.relative_name),
                        tenant_id=claim.tenant_id,
                        source_job_id=claim.job_id,
                        classroom_version_id=source_version.id,
                        artifact_kind="export",
                        relative_name=artifact.relative_name,
                        object_key=artifact.object_key,
                        sha256=artifact.sha256,
                        size_bytes=artifact.size_bytes,
                        mime_type=artifact.mime_type,
                        input_document_sha256=input_document_sha256,
                        input_media_manifest_sha256=input_media_manifest_sha256,
                    )
                )
                session.add(
                    QuotaLedger(
                        id=_quota_event_id("settle", job.id),
                        tenant_id=job.tenant_id,
                        job_id=job.id,
                        entry_type="settle",
                        units=0,
                    )
                )
                state.status = "finalized"
                state.finalized_at = now
                state.updated_at = now
                job.status = "succeeded"
                job.progress_percent = 100
                job.result_ref = artifact.object_key
                job.artifact_manifest_ref = manifest_sha256
                job.completed_at = now
                job.updated_at = now
                self._clear_job_lease(job)
                await session.delete(queue)
                self._release_slots(slots)
                await session.flush()

    async def reap_one_expired(self) -> ReapedJob | None:
        """Fenced recovery of one expired job, queue row, and both slots."""

        session_factory = async_sessionmaker(self._engine(), expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                queue = await session.scalar(
                    select(GenerationQueue)
                    .where(
                        GenerationQueue.status == "claimed",
                        GenerationQueue.lease_expires_at <= now,
                    )
                    .order_by(GenerationQueue.lease_expires_at, GenerationQueue.job_id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if queue is None:
                    return None
                slots = tuple(
                    (
                        await session.scalars(
                            select(GenerationSlot)
                            .where(
                                GenerationSlot.claimed_tenant_id == queue.tenant_id,
                                GenerationSlot.claimed_job_id == queue.job_id,
                                GenerationSlot.lease_token == queue.lease_token,
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                if len(slots) != 2 or queue.lease_token is None:
                    raise JobLeaseLost("expired queue does not own exactly two fenced slots")
                jobs_table = _tenant_table(queue.tenant_id, "generation_jobs")
                job = (
                    (
                        await session.execute(
                            text(
                                f"""
                            SELECT status, attempt_count, max_attempts,
                                   cancel_requested, quota_units, lease_token,
                                   lease_expires_at
                            FROM {jobs_table}
                            WHERE id = :job_id AND tenant_id = :tenant_id
                            FOR UPDATE
                            """
                            ),
                            {"job_id": queue.job_id, "tenant_id": queue.tenant_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    job is None
                    or job["status"] not in LEASED_JOB_STATUSES
                    or job["lease_token"] != queue.lease_token
                    or job["lease_expires_at"] is None
                    or job["lease_expires_at"] > now
                ):
                    raise JobLeaseLost("expired queue does not match the tenant job fence")

                should_retry = not job["cancel_requested"] and int(job["attempt_count"]) < int(
                    job["max_attempts"]
                )
                terminal_status: str | None = None
                next_attempt_at: datetime | None = None
                if should_retry:
                    next_attempt_at = now + timedelta(
                        seconds=retry_delay_seconds(int(job["attempt_count"]))
                    )
                    changed = await session.execute(
                        text(
                            f"""
                            UPDATE {jobs_table}
                            SET status = 'queued',
                                next_attempt_at = :next_attempt_at,
                                waiting_reason = 'retry_backoff',
                                error_category = 'worker_lost',
                                error_code = 'lease_expired',
                                lease_owner = NULL,
                                lease_token = NULL,
                                lease_expires_at = NULL,
                                heartbeat_at = NULL,
                                updated_at = :now
                            WHERE id = :job_id
                              AND tenant_id = :tenant_id
                              AND lease_token = :lease_token
                              AND lease_expires_at <= :now
                            """
                        ),
                        {
                            "job_id": queue.job_id,
                            "tenant_id": queue.tenant_id,
                            "lease_token": queue.lease_token,
                            "next_attempt_at": next_attempt_at,
                            "now": now,
                        },
                    )
                    if changed.rowcount != 1:
                        raise JobLeaseLost("expired job reclaim lost its fence")
                    queue.status = "queued"
                    queue.available_at = next_attempt_at
                    queue.claimed_at = None
                    queue.lease_owner = None
                    queue.lease_token = None
                    queue.lease_expires_at = None
                    queue.heartbeat_at = None
                else:
                    terminal_status = "canceled" if job["cancel_requested"] else "failed"
                    changed = await session.execute(
                        text(
                            f"""
                            UPDATE {jobs_table}
                            SET status = :terminal_status,
                                error_category = :error_category,
                                error_code = :error_code,
                                canceled_at = CASE
                                    WHEN :terminal_status = 'canceled' THEN :now
                                    ELSE NULL
                                END,
                                completed_at = :now,
                                waiting_reason = NULL,
                                lease_owner = NULL,
                                lease_token = NULL,
                                lease_expires_at = NULL,
                                heartbeat_at = NULL,
                                updated_at = :now
                            WHERE id = :job_id
                              AND tenant_id = :tenant_id
                              AND lease_token = :lease_token
                              AND lease_expires_at <= :now
                            """
                        ),
                        {
                            "terminal_status": terminal_status,
                            "error_category": (
                                "canceled" if terminal_status == "canceled" else "worker_lost"
                            ),
                            "error_code": (
                                "job_canceled"
                                if terminal_status == "canceled"
                                else "lease_attempts_exhausted"
                            ),
                            "now": now,
                            "job_id": queue.job_id,
                            "tenant_id": queue.tenant_id,
                            "lease_token": queue.lease_token,
                        },
                    )
                    if changed.rowcount != 1:
                        raise JobLeaseLost("expired job terminalization lost its fence")
                    quota_table = _tenant_table(queue.tenant_id, "quota_ledger")
                    await session.execute(
                        text(
                            f"""
                            INSERT INTO {quota_table}
                                (id, tenant_id, job_id, entry_type, units)
                            VALUES
                                (:id, :tenant_id, :job_id, 'release', :units)
                            ON CONFLICT (job_id, entry_type) DO NOTHING
                            """
                        ),
                        {
                            "id": _quota_event_id("release", queue.job_id),
                            "tenant_id": queue.tenant_id,
                            "job_id": queue.job_id,
                            "units": int(job["quota_units"]),
                        },
                    )
                    await session.delete(queue)
                for slot in slots:
                    slot.claimed_tenant_id = None
                    slot.claimed_job_id = None
                    slot.lease_owner = None
                    slot.lease_token = None
                    slot.lease_expires_at = None
                    slot.heartbeat_at = None
                await session.flush()
                return ReapedJob(
                    tenant_id=queue.tenant_id,
                    job_id=queue.job_id,
                    terminal_status=terminal_status,
                    next_attempt_at=next_attempt_at,
                )
