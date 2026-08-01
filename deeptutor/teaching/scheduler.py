"""Fair durable scheduling across shared generation and MP4 slot pools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import secrets

from sqlalchemy import Select, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.job_route_binding import lock_active_job_binding
from deeptutor.teaching.models import Tenant
from deeptutor.teaching.models.jobs import (
    GenerationQueue,
    GenerationSlot,
    TenantSchedulerState,
)
from deeptutor.teaching.schema_names import tenant_schema_name

GENERATION_GLOBAL_SLOT_LIMIT = 20
STANDARD_TENANT_SLOT_LIMIT = 2
_TENANT_SCHEMA_PATTERN = re.compile(r"tenant_[0-9a-f]{16}")
PRIORITY_RANK = {
    "batch": 100,
    "full": 200,
    "teacher": 300,
    "interaction": 400,
    "student_micro": 500,
}


@dataclass(frozen=True, slots=True)
class ClaimedGenerationJob:
    tenant_id: str
    job_id: str
    job_kind: str
    phase: str
    status: str
    slot_pool: str
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str
    attempt_count: int
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    global_slot_id: int
    tenant_slot_id: int


def slot_pool_for(job_kind: str, export_format: str | None) -> str:
    """Separate expensive MP4 renders from the classroom generation pool."""

    if job_kind == "generation" and export_format is None:
        return "generation"
    if job_kind == "export" and export_format == "mp4":
        return "mp4_export"
    if job_kind == "export" and export_format in {
        "classroom_zip",
        "pptx",
        "offline_html",
    }:
        return "generation"
    raise ValueError("job kind and export format are inconsistent")


def build_tenant_claim_statement(
    data_plane_route_id: str,
    provider_profile_id: str,
    worker_pool_ref: str,
    queue_ref: str,
    slot_pool: str,
) -> Select[tuple[TenantSchedulerState]]:
    """Lock the least-recently dispatched tenant that has due queued work."""

    due_job = (
        select(GenerationQueue.job_id).where(
            GenerationQueue.tenant_id == TenantSchedulerState.tenant_id,
            GenerationQueue.data_plane_route_id == data_plane_route_id,
            GenerationQueue.provider_profile_id == provider_profile_id,
            GenerationQueue.worker_pool_ref == worker_pool_ref,
            GenerationQueue.queue_ref == queue_ref,
            GenerationQueue.slot_pool == slot_pool,
            GenerationQueue.status == "queued",
            GenerationQueue.available_at <= func.now(),
        )
    ).exists()
    available_tenant_slot = (
        select(GenerationSlot.id).where(
            GenerationSlot.slot_pool == slot_pool,
            GenerationSlot.worker_pool_ref == worker_pool_ref,
            GenerationSlot.scope == "tenant",
            GenerationSlot.owner_key == TenantSchedulerState.tenant_id,
            GenerationSlot.claimed_job_id.is_(None),
        )
    ).exists()
    return (
        select(TenantSchedulerState)
        .join(Tenant, Tenant.id == TenantSchedulerState.tenant_id)
        .where(
            Tenant.status == "active",
            TenantSchedulerState.worker_pool_ref == worker_pool_ref,
            TenantSchedulerState.slot_pool == slot_pool,
            due_job,
            available_tenant_slot,
        )
        .order_by(
            TenantSchedulerState.last_dispatched_at.asc().nulls_first(),
            TenantSchedulerState.tenant_id,
        )
        .limit(1)
        .with_for_update(
            of=(TenantSchedulerState, Tenant),
            skip_locked=True,
        )
    )


def _tenant_generation_jobs_table(tenant_id: str) -> str:
    schema = tenant_schema_name(tenant_id)
    if _TENANT_SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError("tenant schema is invalid")
    return f'"{schema}".generation_jobs'


class FairScheduler:
    """Claim jobs with global, tenant, and job locks in one transaction."""

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._configured_engine = engine

    def _engine(self) -> AsyncEngine:
        return self._configured_engine or get_platform_engine()

    async def ensure_slots(
        self,
        tenant_ids: Iterable[str],
        *,
        worker_pool_ref: str,
        slot_pool: str,
        global_limit: int,
        tenant_limit: int,
    ) -> None:
        if (
            not isinstance(worker_pool_ref, str)
            or not worker_pool_ref
            or len(worker_pool_ref) > 128
            or "\n" in worker_pool_ref
            or "\r" in worker_pool_ref
        ):
            raise ValueError("worker_pool_ref is invalid")
        if slot_pool not in {"generation", "mp4_export"}:
            raise ValueError("slot_pool is invalid")
        if isinstance(global_limit, bool) or not isinstance(global_limit, int) or global_limit <= 0:
            raise ValueError("global_limit must be a positive integer")
        if isinstance(tenant_limit, bool) or not isinstance(tenant_limit, int) or tenant_limit <= 0:
            raise ValueError("tenant_limit must be a positive integer")
        tenants = tuple(dict.fromkeys(tenant_ids))
        if not tenants:
            return
        session_factory = async_sessionmaker(
            self._engine(),
            expire_on_commit=False,
        )
        async with session_factory() as session:
            async with session.begin():
                active_tenants = set(
                    (
                        await session.scalars(
                            select(Tenant.id).where(
                                Tenant.id.in_(tenants),
                                Tenant.status == "active",
                            )
                        )
                    ).all()
                )
                if active_tenants != set(tenants):
                    raise ValueError("all slot owners must be active tenants")
                for ordinal in range(global_limit):
                    await session.execute(
                        insert(GenerationSlot)
                        .values(
                            worker_pool_ref=worker_pool_ref,
                            slot_pool=slot_pool,
                            scope="global",
                            owner_key="shared",
                            tenant_id=None,
                            ordinal=ordinal,
                        )
                        .on_conflict_do_nothing(
                            constraint=("uq_generation_slots_worker_pool_scope_owner_ordinal")
                        )
                    )
                for tenant_id in tenants:
                    await session.execute(
                        insert(TenantSchedulerState)
                        .values(
                            tenant_id=tenant_id,
                            worker_pool_ref=worker_pool_ref,
                            slot_pool=slot_pool,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                TenantSchedulerState.tenant_id,
                                TenantSchedulerState.worker_pool_ref,
                                TenantSchedulerState.slot_pool,
                            ]
                        )
                    )
                    for ordinal in range(tenant_limit):
                        await session.execute(
                            insert(GenerationSlot)
                            .values(
                                worker_pool_ref=worker_pool_ref,
                                slot_pool=slot_pool,
                                scope="tenant",
                                owner_key=tenant_id,
                                tenant_id=tenant_id,
                                ordinal=ordinal,
                            )
                            .on_conflict_do_nothing(
                                constraint=("uq_generation_slots_worker_pool_scope_owner_ordinal")
                            )
                        )

    async def ensure_generation_capacity(
        self,
        tenant_ids: Iterable[str],
        *,
        worker_pool_ref: str,
    ) -> None:
        await self.ensure_slots(
            tenant_ids,
            worker_pool_ref=worker_pool_ref,
            slot_pool="generation",
            global_limit=GENERATION_GLOBAL_SLOT_LIMIT,
            tenant_limit=STANDARD_TENANT_SLOT_LIMIT,
        )

    async def claim(
        self,
        slot_pool: str,
        *,
        data_plane_route_id: str,
        provider_profile_id: str,
        worker_pool_ref: str,
        queue_ref: str,
        worker_id: str,
        lease_seconds: int,
    ) -> ClaimedGenerationJob | None:
        for value, name, max_length in (
            (data_plane_route_id, "data_plane_route_id", 63),
            (provider_profile_id, "provider_profile_id", 63),
            (worker_pool_ref, "worker_pool_ref", 128),
            (queue_ref, "queue_ref", 128),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > max_length
                or "\n" in value
                or "\r" in value
            ):
                raise ValueError(f"{name} is invalid")
        if ":" in data_plane_route_id or any(
            character.isspace() for character in data_plane_route_id
        ):
            raise ValueError("data_plane_route_id is invalid")
        if slot_pool not in {"generation", "mp4_export"}:
            raise ValueError("slot_pool is invalid")
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or len(worker_id) > 128
            or "\n" in worker_id
            or "\r" in worker_id
        ):
            raise ValueError("worker_id is invalid")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        session_factory = async_sessionmaker(
            self._engine(),
            expire_on_commit=False,
        )
        async with session_factory() as session:
            async with session.begin():
                now = await session.scalar(select(func.now()))
                if not isinstance(now, datetime):
                    raise RuntimeError("database clock is unavailable")
                global_slot = await session.scalar(
                    select(GenerationSlot)
                    .where(
                        GenerationSlot.worker_pool_ref == worker_pool_ref,
                        GenerationSlot.slot_pool == slot_pool,
                        GenerationSlot.scope == "global",
                        GenerationSlot.owner_key == "shared",
                        GenerationSlot.claimed_job_id.is_(None),
                    )
                    .order_by(GenerationSlot.ordinal)
                    .limit(1)
                    .with_for_update(
                        of=GenerationSlot,
                        skip_locked=True,
                    )
                )
                if global_slot is None:
                    return None
                tenant_state = await session.scalar(
                    build_tenant_claim_statement(
                        data_plane_route_id,
                        provider_profile_id,
                        worker_pool_ref,
                        queue_ref,
                        slot_pool,
                    )
                )
                if tenant_state is None:
                    return None
                if not await lock_active_job_binding(
                    session,
                    tenant_id=tenant_state.tenant_id,
                    data_plane_route_id=data_plane_route_id,
                    provider_profile_id=provider_profile_id,
                    worker_pool_ref=worker_pool_ref,
                    queue_ref=queue_ref,
                ):
                    return None
                tenant_slot = await session.scalar(
                    select(GenerationSlot)
                    .where(
                        GenerationSlot.worker_pool_ref == worker_pool_ref,
                        GenerationSlot.slot_pool == slot_pool,
                        GenerationSlot.scope == "tenant",
                        GenerationSlot.owner_key == tenant_state.tenant_id,
                        GenerationSlot.claimed_job_id.is_(None),
                    )
                    .order_by(GenerationSlot.ordinal)
                    .limit(1)
                    .with_for_update(
                        of=GenerationSlot,
                        skip_locked=True,
                    )
                )
                if tenant_slot is None:
                    return None
                tenant_job_table = _tenant_generation_jobs_table(tenant_state.tenant_id)
                while True:
                    queue_job = await session.scalar(
                        select(GenerationQueue)
                        .where(
                            GenerationQueue.tenant_id == tenant_state.tenant_id,
                            GenerationQueue.data_plane_route_id == data_plane_route_id,
                            GenerationQueue.provider_profile_id == provider_profile_id,
                            GenerationQueue.worker_pool_ref == worker_pool_ref,
                            GenerationQueue.queue_ref == queue_ref,
                            GenerationQueue.slot_pool == slot_pool,
                            GenerationQueue.status == "queued",
                            GenerationQueue.available_at <= now,
                        )
                        .order_by(
                            GenerationQueue.priority.desc(),
                            GenerationQueue.enqueued_at,
                            GenerationQueue.job_id,
                        )
                        .limit(1)
                        .with_for_update(
                            of=GenerationQueue,
                            skip_locked=True,
                        )
                    )
                    if queue_job is None:
                        return None
                    target_status = {
                        ("generation", "outline"): "generating_outline",
                        ("generation", "content"): "generating_content",
                        ("export", "export"): "exporting",
                    }.get((queue_job.job_kind, queue_job.phase))
                    if target_status is None:
                        await session.delete(queue_job)
                        await session.flush()
                        continue
                    lease_token = secrets.token_hex(32)
                    lease_expires_at = now + timedelta(seconds=lease_seconds)
                    claimed_job = await session.execute(
                        text(
                            f"""
                            UPDATE {tenant_job_table}
                            SET status = :target_status,
                                attempt_count = attempt_count + 1,
                                lease_owner = :lease_owner,
                                lease_token = :lease_token,
                                lease_expires_at = :lease_expires_at,
                                heartbeat_at = :now,
                                started_at = COALESCE(started_at, :now),
                                waiting_reason = NULL,
                                updated_at = :now
                            WHERE id = :job_id
                              AND tenant_id = :tenant_id
                              AND job_kind = :job_kind
                              AND phase = :phase
                              AND data_plane_route_id = :data_plane_route_id
                              AND provider_profile_id = :provider_profile_id
                              AND worker_pool_ref = :worker_pool_ref
                              AND queue_ref = :queue_ref
                              AND status = 'queued'
                              AND cancel_requested = false
                              AND attempt_count < max_attempts
                              AND next_attempt_at <= :now
                            RETURNING attempt_count
                            """
                        ),
                        {
                            "target_status": target_status,
                            "lease_owner": worker_id,
                            "lease_token": lease_token,
                            "lease_expires_at": lease_expires_at,
                            "now": now,
                            "job_id": queue_job.job_id,
                            "tenant_id": queue_job.tenant_id,
                            "job_kind": queue_job.job_kind,
                            "phase": queue_job.phase,
                            "data_plane_route_id": data_plane_route_id,
                            "provider_profile_id": provider_profile_id,
                            "worker_pool_ref": worker_pool_ref,
                            "queue_ref": queue_ref,
                        },
                    )
                    attempt_count = claimed_job.scalar_one_or_none()
                    if attempt_count is None:
                        await session.delete(queue_job)
                        await session.flush()
                        continue
                    break
                queue_job.status = "claimed"
                queue_job.claimed_at = now
                queue_job.lease_owner = worker_id
                queue_job.lease_token = lease_token
                queue_job.lease_expires_at = lease_expires_at
                queue_job.heartbeat_at = now
                for slot in (global_slot, tenant_slot):
                    slot.claimed_tenant_id = queue_job.tenant_id
                    slot.claimed_job_id = queue_job.job_id
                    slot.lease_owner = worker_id
                    slot.lease_token = lease_token
                    slot.lease_expires_at = lease_expires_at
                    slot.heartbeat_at = now
                tenant_state.last_dispatched_at = now
                tenant_state.updated_at = now
                await session.flush()
                return ClaimedGenerationJob(
                    tenant_id=queue_job.tenant_id,
                    job_id=queue_job.job_id,
                    job_kind=queue_job.job_kind,
                    phase=queue_job.phase,
                    status=target_status,
                    slot_pool=slot_pool,
                    data_plane_route_id=data_plane_route_id,
                    provider_profile_id=provider_profile_id,
                    worker_pool_ref=worker_pool_ref,
                    queue_ref=queue_ref,
                    attempt_count=attempt_count,
                    lease_owner=worker_id,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    global_slot_id=global_slot.id,
                    tenant_slot_id=tenant_slot.id,
                )
