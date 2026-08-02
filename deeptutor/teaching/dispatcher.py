"""Transactional outbox delivery into the durable generation queue."""

from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import Select, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.models import Tenant
from deeptutor.teaching.models.jobs import (
    GenerationQueue,
    OutboxMessage,
    TenantSchedulerState,
)
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
                updated_job = await session.execute(
                    text(
                        f"""
                        UPDATE {_tenant_generation_jobs_table(message.tenant_id)}
                        SET status = 'queued', updated_at = :now
                        WHERE id = :job_id
                          AND tenant_id = :tenant_id
                          AND job_kind = :job_kind
                          AND phase = :phase
                          AND data_plane_route_id = :data_plane_route_id
                          AND provider_profile_id = :provider_profile_id
                          AND worker_pool_ref = :worker_pool_ref
                          AND queue_ref = :queue_ref
                          AND status IN ('quota_reserved', 'queued')
                        RETURNING id
                        """
                    ),
                    {
                        "now": now,
                        "job_id": message.job_id,
                        "tenant_id": message.tenant_id,
                        "job_kind": message.job_kind,
                        "phase": message.phase,
                        "data_plane_route_id": message.data_plane_route_id,
                        "provider_profile_id": message.provider_profile_id,
                        "worker_pool_ref": message.worker_pool_ref,
                        "queue_ref": message.queue_ref,
                    },
                )
                if updated_job.scalar_one_or_none() is None:
                    terminal_status = await session.scalar(
                        text(
                            f"""
                            SELECT status
                            FROM {_tenant_generation_jobs_table(message.tenant_id)}
                            WHERE id = :job_id
                              AND tenant_id = :tenant_id
                              AND status IN ('succeeded', 'failed', 'canceled')
                            FOR UPDATE
                            """
                        ),
                        {
                            "job_id": message.job_id,
                            "tenant_id": message.tenant_id,
                        },
                    )
                    if terminal_status is not None:
                        message.delivered_at = now
                        await session.flush()
                        return DispatchedJob(
                            tenant_id=message.tenant_id,
                            job_id=message.job_id,
                            job_kind=message.job_kind,
                            phase=message.phase,
                            slot_pool=message.slot_pool,
                            data_plane_route_id=message.data_plane_route_id,
                            provider_profile_id=message.provider_profile_id,
                            worker_pool_ref=message.worker_pool_ref,
                            queue_ref=message.queue_ref,
                        )
                    raise OutboxDispatchConflict("tenant job cannot accept outbox delivery")
                queue_insert = (
                    insert(GenerationQueue)
                    .values(
                        tenant_id=message.tenant_id,
                        job_id=message.job_id,
                        job_kind=message.job_kind,
                        phase=message.phase,
                        data_plane_route_id=message.data_plane_route_id,
                        provider_profile_id=message.provider_profile_id,
                        worker_pool_ref=message.worker_pool_ref,
                        queue_ref=message.queue_ref,
                        slot_pool=message.slot_pool,
                        priority=message.priority,
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
                            "job_kind": message.job_kind,
                            "phase": message.phase,
                            "data_plane_route_id": message.data_plane_route_id,
                            "provider_profile_id": message.provider_profile_id,
                            "worker_pool_ref": message.worker_pool_ref,
                            "queue_ref": message.queue_ref,
                            "slot_pool": message.slot_pool,
                            "priority": message.priority,
                            "status": "queued",
                            "available_at": message.available_at,
                            "enqueued_at": now,
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
                        worker_pool_ref=message.worker_pool_ref,
                        slot_pool=message.slot_pool,
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
                return DispatchedJob(
                    tenant_id=message.tenant_id,
                    job_id=message.job_id,
                    job_kind=message.job_kind,
                    phase=message.phase,
                    slot_pool=message.slot_pool,
                    data_plane_route_id=message.data_plane_route_id,
                    provider_profile_id=message.provider_profile_id,
                    worker_pool_ref=message.worker_pool_ref,
                    queue_ref=message.queue_ref,
                )
