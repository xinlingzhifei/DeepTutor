"""Read one bounded, atomic view of generation scheduler state."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from deeptutor.teaching.database import platform_session
from deeptutor.teaching.models import AuditLog, GenerationQueue, GenerationSlot

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_CAPACITY_SNAPSHOT_JOBS = 128


@dataclass(frozen=True, slots=True)
class CapacitySnapshotJob:
    job_id: str
    tenant_id: str
    worker_pool_ref: str
    status: str
    claimed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ActiveCapacityClaim:
    job_id: str
    tenant_id: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class GenerationClaimEvent:
    cursor: int
    job_id: str
    tenant_id: str
    claimed_at: datetime


@dataclass(frozen=True, slots=True)
class TenantSlotCapacity:
    tenant_id: str
    capacity: int


@dataclass(frozen=True, slots=True)
class CapacitySchedulerPoolSnapshot:
    worker_pool_ref: str
    global_slot_capacity: int
    tenant_capacities: tuple[TenantSlotCapacity, ...]
    active: tuple[ActiveCapacityClaim, ...]


@dataclass(frozen=True, slots=True)
class CapacitySchedulerSnapshot:
    observed_at: datetime
    jobs: tuple[CapacitySnapshotJob, ...]
    claim_events: tuple[GenerationClaimEvent, ...]
    missing_job_ids: tuple[str, ...]
    pools: tuple[CapacitySchedulerPoolSnapshot, ...]


def build_capacity_queue_snapshot_statement(job_ids: Sequence[str]):
    return (
        select(
            GenerationQueue.job_id,
            GenerationQueue.tenant_id,
            GenerationQueue.worker_pool_ref,
            GenerationQueue.status,
            GenerationQueue.claimed_at,
        )
        .where(
            GenerationQueue.job_id.in_(tuple(job_ids)),
            GenerationQueue.slot_pool == "generation",
        )
        .order_by(GenerationQueue.job_id, GenerationQueue.tenant_id)
    )


def build_capacity_slot_snapshot_statement(
    *,
    worker_pool_refs: Sequence[str],
    tenant_ids: Sequence[str],
):
    return (
        select(
            GenerationSlot.worker_pool_ref,
            GenerationSlot.scope,
            GenerationSlot.owner_key,
            GenerationSlot.ordinal,
            GenerationSlot.claimed_job_id,
            GenerationSlot.claimed_tenant_id,
        )
        .where(
            GenerationSlot.worker_pool_ref.in_(tuple(worker_pool_refs)),
            GenerationSlot.slot_pool == "generation",
            or_(
                GenerationSlot.scope == "global",
                and_(
                    GenerationSlot.scope == "tenant",
                    GenerationSlot.owner_key.in_(tuple(tenant_ids)),
                ),
            ),
        )
        .order_by(
            GenerationSlot.worker_pool_ref,
            GenerationSlot.scope,
            GenerationSlot.owner_key,
            GenerationSlot.ordinal,
        )
    )


def build_generation_claim_event_statement(job_ids: Sequence[str]):
    return (
        select(
            AuditLog.id.label("cursor"),
            AuditLog.resource_id.label("job_id"),
            AuditLog.tenant_id,
            AuditLog.created_at.label("claimed_at"),
        )
        .where(
            AuditLog.action == "generation.job_claimed",
            AuditLog.resource_type == "generation_job",
            AuditLog.resource_id.in_(tuple(job_ids)),
        )
        .order_by(AuditLog.id)
    )


def _valid_job_ids(job_ids: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(job_ids)
    if (
        not normalized
        or len(normalized) > MAX_CAPACITY_SNAPSHOT_JOBS
        or len(set(normalized)) != len(normalized)
        or any(
            not isinstance(job_id, str) or _PUBLIC_ID.fullmatch(job_id) is None
            for job_id in normalized
        )
    ):
        raise ValueError("capacity snapshot job ids are invalid")
    return tuple(sorted(normalized))


def _required_string(value: Any, name: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError(f"capacity scheduler {name} is invalid")
    return value


def _ordinal(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("capacity scheduler slot ordinal is invalid")
    return value


class SqlAlchemyCapacitySchedulerRepository:
    """Read requested jobs and their slots in one repeatable read-only transaction."""

    def __init__(self, session_factory: SessionFactory = platform_session) -> None:
        self._session_factory = session_factory

    async def fetch_snapshot(
        self,
        job_ids: Sequence[str],
    ) -> CapacitySchedulerSnapshot:
        requested = _valid_job_ids(job_ids)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                observed_at = (await session.execute(select(func.now()))).scalar_one()
                queue_rows = (
                    (await session.execute(build_capacity_queue_snapshot_statement(requested)))
                    .mappings()
                    .all()
                )
                event_rows = (
                    (await session.execute(build_generation_claim_event_statement(requested)))
                    .mappings()
                    .all()
                )
                worker_pool_refs = tuple(
                    sorted(
                        {
                            _required_string(row["worker_pool_ref"], "worker pool")
                            for row in queue_rows
                        }
                    )
                )
                tenant_ids = tuple(
                    sorted(
                        {
                            _required_string(row["tenant_id"], "tenant id", maximum=64)
                            for row in queue_rows
                        }
                    )
                )
                slot_rows = (
                    (
                        await session.execute(
                            build_capacity_slot_snapshot_statement(
                                worker_pool_refs=worker_pool_refs,
                                tenant_ids=tenant_ids,
                            )
                        )
                    )
                    .mappings()
                    .all()
                    if worker_pool_refs
                    else ()
                )

        if not isinstance(observed_at, datetime):
            raise ValueError("capacity scheduler database clock is invalid")
        jobs: list[CapacitySnapshotJob] = []
        seen_jobs: set[str] = set()
        for row in queue_rows:
            job_id = _required_string(row["job_id"], "job id")
            tenant_id = _required_string(row["tenant_id"], "tenant id", maximum=64)
            worker_pool_ref = _required_string(row["worker_pool_ref"], "worker pool")
            status = row["status"]
            claimed_at = row["claimed_at"]
            if (
                job_id not in requested
                or job_id in seen_jobs
                or status not in {"queued", "claimed"}
                or (status == "queued" and claimed_at is not None)
                or (status == "claimed" and not isinstance(claimed_at, datetime))
            ):
                raise ValueError("capacity scheduler queue snapshot is invalid")
            seen_jobs.add(job_id)
            jobs.append(
                CapacitySnapshotJob(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    worker_pool_ref=worker_pool_ref,
                    status=status,
                    claimed_at=claimed_at,
                )
            )

        claim_events: list[GenerationClaimEvent] = []
        previous_cursor = -1
        for row in event_rows:
            cursor = row["cursor"]
            job_id = _required_string(row["job_id"], "claim event job id")
            tenant_id = _required_string(
                row["tenant_id"],
                "claim event tenant id",
                maximum=64,
            )
            claimed_at = row["claimed_at"]
            if (
                isinstance(cursor, bool)
                or not isinstance(cursor, int)
                or cursor <= previous_cursor
                or job_id not in requested
                or not isinstance(claimed_at, datetime)
            ):
                raise ValueError("capacity scheduler claim event is invalid")
            previous_cursor = cursor
            claim_events.append(
                GenerationClaimEvent(
                    cursor=cursor,
                    job_id=job_id,
                    tenant_id=tenant_id,
                    claimed_at=claimed_at,
                )
            )

        grouped: dict[str, list[dict[str, Any]]] = {
            worker_pool_ref: [] for worker_pool_ref in worker_pool_refs
        }
        for row in slot_rows:
            worker_pool_ref = _required_string(row["worker_pool_ref"], "worker pool")
            if worker_pool_ref not in grouped:
                raise ValueError("capacity scheduler slot snapshot is invalid")
            grouped[worker_pool_ref].append(dict(row))

        pools: list[CapacitySchedulerPoolSnapshot] = []
        target_tenants = set(tenant_ids)
        for worker_pool_ref, rows in sorted(grouped.items()):
            global_ordinals: set[int] = set()
            tenant_ordinals: dict[str, set[int]] = {}
            active: list[ActiveCapacityClaim] = []
            for row in rows:
                scope = row["scope"]
                owner_key = _required_string(row["owner_key"], "slot owner", maximum=64)
                ordinal = _ordinal(row["ordinal"])
                claimed_job_id = row["claimed_job_id"]
                claimed_tenant_id = row["claimed_tenant_id"]
                if (claimed_job_id is None) != (claimed_tenant_id is None):
                    raise ValueError("capacity scheduler slot claim is invalid")
                if scope == "global" and owner_key == "shared":
                    if ordinal in global_ordinals:
                        raise ValueError("capacity scheduler slot snapshot is invalid")
                    global_ordinals.add(ordinal)
                    if claimed_job_id is not None:
                        active.append(
                            ActiveCapacityClaim(
                                job_id=_required_string(claimed_job_id, "claimed job id"),
                                tenant_id=_required_string(
                                    claimed_tenant_id,
                                    "claimed tenant id",
                                    maximum=64,
                                ),
                                ordinal=ordinal,
                            )
                        )
                elif scope == "tenant" and owner_key in target_tenants:
                    ordinals = tenant_ordinals.setdefault(owner_key, set())
                    if ordinal in ordinals:
                        raise ValueError("capacity scheduler slot snapshot is invalid")
                    ordinals.add(ordinal)
                else:
                    raise ValueError("capacity scheduler slot snapshot is invalid")
            if not global_ordinals:
                raise ValueError("capacity scheduler global slots are unavailable")
            pools.append(
                CapacitySchedulerPoolSnapshot(
                    worker_pool_ref=worker_pool_ref,
                    global_slot_capacity=len(global_ordinals),
                    tenant_capacities=tuple(
                        TenantSlotCapacity(tenant_id=tenant_id, capacity=len(ordinals))
                        for tenant_id, ordinals in sorted(tenant_ordinals.items())
                    ),
                    active=tuple(sorted(active, key=lambda claim: claim.ordinal)),
                )
            )

        active_target_jobs = {
            claim.job_id for pool in pools for claim in pool.active if claim.job_id in seen_jobs
        }
        if {job.job_id for job in jobs if job.status == "claimed"} != active_target_jobs:
            raise ValueError("capacity scheduler queue and slot state diverged")
        return CapacitySchedulerSnapshot(
            observed_at=observed_at,
            jobs=tuple(sorted(jobs, key=lambda job: job.job_id)),
            claim_events=tuple(claim_events),
            missing_job_ids=tuple(sorted(set(requested) - seen_jobs)),
            pools=tuple(pools),
        )


def get_capacity_scheduler_repository() -> SqlAlchemyCapacitySchedulerRepository:
    return SqlAlchemyCapacitySchedulerRepository()


__all__ = [
    "ActiveCapacityClaim",
    "CapacitySchedulerPoolSnapshot",
    "CapacitySchedulerSnapshot",
    "CapacitySnapshotJob",
    "GenerationClaimEvent",
    "MAX_CAPACITY_SNAPSHOT_JOBS",
    "SqlAlchemyCapacitySchedulerRepository",
    "TenantSlotCapacity",
    "build_capacity_queue_snapshot_statement",
    "build_capacity_slot_snapshot_statement",
    "build_generation_claim_event_statement",
    "get_capacity_scheduler_repository",
]
