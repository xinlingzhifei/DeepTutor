"""PostgreSQL repository for atomic tenant jobs, quota, and outbox writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.job_route_binding import (
    DataPlaneBindingUnavailable,
    lock_active_job_binding,
)
from deeptutor.teaching.models import Tenant
from deeptutor.teaching.models.jobs import (
    LEASED_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    GenerationJob,
    GenerationQueue,
    GenerationSlot,
    InvalidJobTransition,
    OutboxMessage,
    QuotaLedger,
    require_job_transition,
)
from deeptutor.teaching.quota import reserve_quota
from deeptutor.teaching.scheduler import PRIORITY_RANK, slot_pool_for
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
    max_attempts: int = 5

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


@dataclass(frozen=True, slots=True)
class GenerationJobRecord:
    tenant_id: str
    job_id: str
    job_kind: str
    phase: str
    status: str
    priority: int
    quota_units: int


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
                if not await lock_active_job_binding(
                    session,
                    tenant_id=request.tenant_id,
                    data_plane_route_id=request.data_plane_route_id,
                    provider_profile_id=request.provider_profile_id,
                    worker_pool_ref=request.worker_pool_ref,
                    queue_ref=request.queue_ref,
                ):
                    raise DataPlaneBindingUnavailable()
                existing = await session.scalar(
                    select(GenerationJob).where(
                        GenerationJob.tenant_id == request.tenant_id,
                        GenerationJob.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if not self._matches_idempotent_request(existing, request):
                        raise IdempotencyConflict()
                    return self._record(existing)
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
                    request_sha256=request.request_sha256.lower(),
                    data_plane_route_id=request.data_plane_route_id,
                    provider_profile_id=request.provider_profile_id,
                    worker_pool_ref=request.worker_pool_ref,
                    queue_ref=request.queue_ref,
                    request_payload=request.request_payload,
                    max_attempts=request.max_attempts,
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
            and existing.request_sha256 == request.request_sha256.lower()
            and existing.data_plane_route_id == request.data_plane_route_id
            and existing.provider_profile_id == request.provider_profile_id
            and existing.worker_pool_ref == request.worker_pool_ref
            and existing.queue_ref == request.queue_ref
            and existing.request_payload == request.request_payload
            and existing.max_attempts == request.max_attempts
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
    ) -> bool:
        """Move a confirmed outline to content and append its second event."""

        session_factory = self._session_factory(tenant_id)
        async with session_factory() as session:
            async with session.begin():
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
                result = await session.execute(
                    update(GenerationJob)
                    .where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == tenant_id,
                        GenerationJob.job_kind == "generation",
                        GenerationJob.phase == "outline",
                        GenerationJob.status == "awaiting_confirmation",
                    )
                    .values(
                        phase="content",
                        status="queued",
                        waiting_reason=None,
                        updated_at=now,
                    )
                    .returning(
                        GenerationJob.priority,
                        GenerationJob.data_plane_route_id,
                        GenerationJob.provider_profile_id,
                        GenerationJob.worker_pool_ref,
                        GenerationJob.queue_ref,
                    )
                )
                binding = result.one_or_none()
                if binding is None:
                    return False
                session.add(
                    OutboxMessage(
                        event_id=_event_id(tenant_id, job_id, "content"),
                        tenant_id=tenant_id,
                        job_id=job_id,
                        job_kind="generation",
                        phase="content",
                        data_plane_route_id=binding.data_plane_route_id,
                        provider_profile_id=binding.provider_profile_id,
                        worker_pool_ref=binding.worker_pool_ref,
                        queue_ref=binding.queue_ref,
                        slot_pool="generation",
                        priority=binding.priority,
                        event_type="generation_job.content_ready",
                        payload="content",
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
