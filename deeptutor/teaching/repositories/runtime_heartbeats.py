"""SQLAlchemy persistence for teaching runtime process heartbeats."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from sqlalchemy import delete, func, or_, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deeptutor.teaching.database import platform_session
from deeptutor.teaching.models import TeachingRuntimeProcessHeartbeat
from deeptutor.teaching.runtime_heartbeat import (
    RUNTIME_HEARTBEAT_PRUNE_BATCH_SIZE,
    RUNTIME_HEARTBEAT_RETENTION_SECONDS,
    RuntimeHeartbeatSnapshot,
    RuntimeProcessRole,
)

_RETENTION_INTERVAL = text(
    f"INTERVAL '{RUNTIME_HEARTBEAT_RETENTION_SECONDS // (24 * 60 * 60)} days'"
)


def _retention_cutoff():
    return func.now() - _RETENTION_INTERVAL


def build_heartbeat_statement(role: RuntimeProcessRole, instance_id: str):
    return (
        update(TeachingRuntimeProcessHeartbeat)
        .where(
            TeachingRuntimeProcessHeartbeat.role == role,
            TeachingRuntimeProcessHeartbeat.instance_id == instance_id,
            TeachingRuntimeProcessHeartbeat.status == "running",
        )
        .values(heartbeat_at=func.now(), updated_at=func.now())
    )


def build_mark_stopped_statement(role: RuntimeProcessRole, instance_id: str):
    return (
        update(TeachingRuntimeProcessHeartbeat)
        .where(
            TeachingRuntimeProcessHeartbeat.role == role,
            TeachingRuntimeProcessHeartbeat.instance_id == instance_id,
            TeachingRuntimeProcessHeartbeat.status == "running",
        )
        .values(
            status="stopped",
            stopped_at=func.now(),
            updated_at=func.now(),
        )
    )


def build_latest_running_heartbeats_statement(
    roles: Sequence[RuntimeProcessRole],
):
    return (
        select(
            TeachingRuntimeProcessHeartbeat.role,
            func.greatest(
                0.0,
                func.extract(
                    "epoch",
                    func.now() - func.max(TeachingRuntimeProcessHeartbeat.heartbeat_at),
                ),
            ).label("age_seconds"),
        )
        .where(
            TeachingRuntimeProcessHeartbeat.status == "running",
            TeachingRuntimeProcessHeartbeat.role.in_(tuple(roles)),
            TeachingRuntimeProcessHeartbeat.heartbeat_at >= _retention_cutoff(),
        )
        .group_by(TeachingRuntimeProcessHeartbeat.role)
    )


def build_retention_prune_statement():
    expired_instances = (
        select(
            TeachingRuntimeProcessHeartbeat.role,
            TeachingRuntimeProcessHeartbeat.instance_id,
        )
        .where(
            or_(
                (
                    (TeachingRuntimeProcessHeartbeat.status == "stopped")
                    & (TeachingRuntimeProcessHeartbeat.stopped_at < _retention_cutoff())
                ),
                (
                    (TeachingRuntimeProcessHeartbeat.status == "running")
                    & (TeachingRuntimeProcessHeartbeat.heartbeat_at < _retention_cutoff())
                ),
            )
        )
        .order_by(
            TeachingRuntimeProcessHeartbeat.updated_at,
            TeachingRuntimeProcessHeartbeat.role,
            TeachingRuntimeProcessHeartbeat.instance_id,
        )
        .limit(RUNTIME_HEARTBEAT_PRUNE_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    return delete(TeachingRuntimeProcessHeartbeat).where(
        tuple_(
            TeachingRuntimeProcessHeartbeat.role,
            TeachingRuntimeProcessHeartbeat.instance_id,
        ).in_(expired_instances)
    )


class SqlAlchemyRuntimeHeartbeatRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        if self._session_factory is None:
            async with platform_session() as session:
                yield session
            return
        async with self._session_factory() as session:
            yield session

    async def register(self, role: RuntimeProcessRole, instance_id: str) -> None:
        async with self._session() as session:
            await session.execute(build_retention_prune_statement())
            session.add(
                TeachingRuntimeProcessHeartbeat(
                    role=role,
                    instance_id=instance_id,
                    status="running",
                )
            )
            await session.commit()

    async def heartbeat(self, role: RuntimeProcessRole, instance_id: str) -> bool:
        async with self._session() as session:
            result = await session.execute(build_heartbeat_statement(role, instance_id))
            await session.commit()
            return result.rowcount == 1

    async def mark_stopped(self, role: RuntimeProcessRole, instance_id: str) -> bool:
        async with self._session() as session:
            result = await session.execute(build_mark_stopped_statement(role, instance_id))
            await session.commit()
            return result.rowcount == 1

    async def latest_running_heartbeats(
        self,
        roles: Sequence[RuntimeProcessRole],
    ) -> tuple[RuntimeHeartbeatSnapshot, ...]:
        if not roles:
            return ()
        async with self._session() as session:
            rows = (await session.execute(build_latest_running_heartbeats_statement(roles))).all()
        return tuple(
            RuntimeHeartbeatSnapshot(role=row.role, age_seconds=float(row.age_seconds))
            for row in rows
        )


def get_runtime_heartbeat_repository() -> SqlAlchemyRuntimeHeartbeatRepository:
    return SqlAlchemyRuntimeHeartbeatRepository()


__all__ = [
    "SqlAlchemyRuntimeHeartbeatRepository",
    "build_heartbeat_statement",
    "build_latest_running_heartbeats_statement",
    "build_mark_stopped_statement",
    "build_retention_prune_statement",
    "get_runtime_heartbeat_repository",
]
