"""Transactional outbox delivery into the durable generation queue."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.sql.elements import TextClause

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.models import Tenant
from deeptutor.teaching.models.jobs import (
    GenerationQueue,
    OutboxMessage,
    TenantSchedulerState,
)
from deeptutor.teaching.repositories.metric_rollups import increment_counter_rollup
from deeptutor.teaching.scheduler import slot_pool_for
from deeptutor.teaching.schema_names import tenant_schema_name

_TENANT_SCHEMA_PATTERN = re.compile(r"tenant_[0-9a-f]{16}")


class OutboxDispatchConflict(RuntimeError):
    """The tenant job or queue projection rejected this outbox event."""


@dataclass(frozen=True, slots=True)
class DispatchedJob:
    tenant_id: str
    job_id: str
    job_kind: str
    phase: str
    slot_pool: str
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str


@dataclass(frozen=True, slots=True)
class _AuthoritativeJobProjection:
    status: str
    job_kind: str
    phase: str
    export_format: str | None
    priority: int
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str


def _tenant_generation_jobs_table(tenant_id: str) -> str:
    schema = tenant_schema_name(tenant_id)
    if _TENANT_SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError("tenant schema is invalid")
    return f'"{schema}".generation_jobs'


def build_outbox_claim_statement() -> Select[tuple[OutboxMessage]]:
    """Lock the oldest due outbox row without blocking another dispatcher."""

    return (
        select(OutboxMessage)
        .join(Tenant, Tenant.id == OutboxMessage.tenant_id)
        .where(
            Tenant.status == "active",
            OutboxMessage.delivered_at.is_(None),
            OutboxMessage.available_at <= func.now(),
        )
        .order_by(
            OutboxMessage.available_at,
            OutboxMessage.created_at,
            OutboxMessage.tenant_id,
            OutboxMessage.job_id,
        )
        .limit(1)
        .with_for_update(of=(OutboxMessage, Tenant), skip_locked=True)
    )


def build_job_queue_transition_statement(tenant_id: str) -> TextClause:
    """Build the first and only quota-reserved to queued transition."""

    return text(
        f"""
        UPDATE {_tenant_generation_jobs_table(tenant_id)}
        SET status = 'queued', updated_at = :now
        WHERE id = :job_id
          AND tenant_id = :tenant_id
          AND job_kind = :job_kind
          AND phase = :phase
          AND data_plane_route_id = :data_plane_route_id
          AND provider_profile_id = :provider_profile_id
          AND worker_pool_ref = :worker_pool_ref
          AND queue_ref = :queue_ref
          AND status = 'quota_reserved'
        RETURNING status,
                  job_kind,
                  phase,
                  export_format,
                  priority,
                  data_plane_route_id,
                  provider_profile_id,
                  worker_pool_ref,
                  queue_ref
        """
    )


def _build_locked_job_projection_statement(tenant_id: str) -> TextClause:
    return text(
        f"""
        SELECT status,
               job_kind,
               phase,
               export_format,
               priority,
               data_plane_route_id,
               provider_profile_id,
               worker_pool_ref,
               queue_ref
        FROM {_tenant_generation_jobs_table(tenant_id)}
        WHERE id = :job_id
          AND tenant_id = :tenant_id
          AND job_kind = :job_kind
          AND phase = :phase
          AND data_plane_route_id = :data_plane_route_id
          AND provider_profile_id = :provider_profile_id
          AND worker_pool_ref = :worker_pool_ref
          AND queue_ref = :queue_ref
          AND status IN ('queued', 'succeeded', 'failed', 'canceled')
        FOR UPDATE
        """
    )


def _authoritative_job_projection(row: Any) -> _AuthoritativeJobProjection:
    return _AuthoritativeJobProjection(
        status=row["status"],
        job_kind=row["job_kind"],
        phase=row["phase"],
        export_format=row["export_format"],
        priority=row["priority"],
        data_plane_route_id=row["data_plane_route_id"],
        provider_profile_id=row["provider_profile_id"],
        worker_pool_ref=row["worker_pool_ref"],
        queue_ref=row["queue_ref"],
    )


class OutboxDispatcher:
    """Atomically project one tenant outbox event into the platform queue."""

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._configured_engine = engine

    def _engine(self) -> AsyncEngine:
        return self._configured_engine or get_platform_engine()

    async def dispatch_next(self) -> DispatchedJob | None:
        session_factory = async_sessionmaker(
            self._engine(),
            expire_on_commit=False,
        )
        async with session_factory() as session:
            async with session.begin():
                message = await session.scalar(build_outbox_claim_statement())
                if message is None:
                    return None
                now = await session.scalar(select(func.now()))
                if now is None:
                    raise RuntimeError("database clock is unavailable")
                job_parameters = {
                    "now": now,
                    "job_id": message.job_id,
                    "tenant_id": message.tenant_id,
                    "job_kind": message.job_kind,
                    "phase": message.phase,
                    "data_plane_route_id": message.data_plane_route_id,
                    "provider_profile_id": message.provider_profile_id,
                    "worker_pool_ref": message.worker_pool_ref,
                    "queue_ref": message.queue_ref,
                }
                updated_job = await session.execute(
                    build_job_queue_transition_statement(message.tenant_id),
                    job_parameters,
                )
                authoritative_row = updated_job.mappings().one_or_none()
                first_queue_transition = authoritative_row is not None
                if authoritative_row is None:
                    locked_job = await session.execute(
                        _build_locked_job_projection_statement(message.tenant_id),
                        job_parameters,
                    )
                    authoritative_row = locked_job.mappings().one_or_none()
                if authoritative_row is None:
                    raise OutboxDispatchConflict("tenant job cannot accept outbox delivery")
                job = _authoritative_job_projection(authoritative_row)
                try:
                    authoritative_slot_pool = slot_pool_for(job.job_kind, job.export_format)
                except ValueError as exc:
                    raise OutboxDispatchConflict("tenant job shape is invalid") from exc
                if authoritative_slot_pool != message.slot_pool:
                    raise OutboxDispatchConflict("outbox slot pool does not match tenant job shape")
                if job.status in {"succeeded", "failed", "canceled"}:
                    message.delivered_at = now
                    await session.flush()
                    return DispatchedJob(
                        tenant_id=message.tenant_id,
                        job_id=message.job_id,
                        job_kind=job.job_kind,
                        phase=job.phase,
                        slot_pool=authoritative_slot_pool,
                        data_plane_route_id=job.data_plane_route_id,
                        provider_profile_id=job.provider_profile_id,
                        worker_pool_ref=job.worker_pool_ref,
                        queue_ref=job.queue_ref,
                    )
                queue_insert = (
                    insert(GenerationQueue)
                    .values(
                        tenant_id=message.tenant_id,
                        job_id=message.job_id,
                        job_kind=job.job_kind,
                        phase=job.phase,
                        data_plane_route_id=job.data_plane_route_id,
                        provider_profile_id=job.provider_profile_id,
                        worker_pool_ref=job.worker_pool_ref,
                        queue_ref=job.queue_ref,
                        slot_pool=authoritative_slot_pool,
                        priority=job.priority,
                        status="queued",
                        available_at=message.available_at,
                        enqueued_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            GenerationQueue.tenant_id,
                            GenerationQueue.job_id,
                        ],
                        set_={
                            "job_kind": job.job_kind,
                            "phase": job.phase,
                            "data_plane_route_id": job.data_plane_route_id,
                            "provider_profile_id": job.provider_profile_id,
                            "worker_pool_ref": job.worker_pool_ref,
                            "queue_ref": job.queue_ref,
                            "slot_pool": authoritative_slot_pool,
                            "priority": job.priority,
                            "status": "queued",
                            "claimed_at": None,
                            "lease_owner": None,
                            "lease_token": None,
                            "lease_expires_at": None,
                            "heartbeat_at": None,
                        },
                        where=GenerationQueue.status == "queued",
                    )
                )
                queue_result = await session.execute(queue_insert)
                if queue_result.rowcount != 1:
                    raise OutboxDispatchConflict("claimed queue rows cannot be replaced")
                scheduler_state = (
                    insert(TenantSchedulerState)
                    .values(
                        tenant_id=message.tenant_id,
                        worker_pool_ref=job.worker_pool_ref,
                        slot_pool=authoritative_slot_pool,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            TenantSchedulerState.tenant_id,
                            TenantSchedulerState.worker_pool_ref,
                            TenantSchedulerState.slot_pool,
                        ]
                    )
                )
                await session.execute(scheduler_state)
                message.delivered_at = now
                await session.flush()
                if first_queue_transition:
                    await increment_counter_rollup(
                        session,
                        metric="generation_jobs_total",
                        category="queued",
                        fact_key=message.event_id,
                        amount=1,
                    )
                return DispatchedJob(
                    tenant_id=message.tenant_id,
                    job_id=message.job_id,
                    job_kind=job.job_kind,
                    phase=job.phase,
                    slot_pool=authoritative_slot_pool,
                    data_plane_route_id=job.data_plane_route_id,
                    provider_profile_id=job.provider_profile_id,
                    worker_pool_ref=job.worker_pool_ref,
                    queue_ref=job.queue_ref,
                )
