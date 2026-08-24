"""Durable lifecycle heartbeats for independent teaching runtime processes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import logging
import math
from typing import Literal, Protocol, TypeVar
from uuid import uuid4

RuntimeProcessRole = Literal[
    "tenant_provisioner",
    "dispatcher",
    "generation_worker",
    "export_worker",
    "projector",
    "reaper",
]
RUNTIME_PROCESS_ROLES: tuple[RuntimeProcessRole, ...] = (
    "tenant_provisioner",
    "dispatcher",
    "generation_worker",
    "export_worker",
    "projector",
    "reaper",
)
RUNTIME_HEARTBEAT_RETENTION_SECONDS = 7 * 24 * 60 * 60
RUNTIME_HEARTBEAT_PRUNE_BATCH_SIZE = 500

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeatSnapshot:
    role: RuntimeProcessRole
    age_seconds: float


class RuntimeHeartbeatRepository(Protocol):
    async def register(self, role: RuntimeProcessRole, instance_id: str) -> None: ...

    async def heartbeat(self, role: RuntimeProcessRole, instance_id: str) -> bool: ...

    async def mark_stopped(self, role: RuntimeProcessRole, instance_id: str) -> bool: ...

    async def latest_running_heartbeats(
        self,
        roles: Sequence[RuntimeProcessRole],
    ) -> tuple[RuntimeHeartbeatSnapshot, ...]: ...


class RuntimeHeartbeatUnavailable(RuntimeError):
    """The process can no longer publish its durable liveness signal."""


def new_runtime_instance_id(role: RuntimeProcessRole) -> str:
    """Return one opaque, per-start process fence without host or user data."""

    if role not in RUNTIME_PROCESS_ROLES:
        raise ValueError("teaching runtime role is invalid")
    return f"{role}:{uuid4().hex}"


class RuntimeHeartbeatSupervisor:
    """Run one process workload while its independent DB heartbeat remains live."""

    def __init__(
        self,
        repository: RuntimeHeartbeatRepository,
        *,
        role: RuntimeProcessRole,
        heartbeat_interval_seconds: float = 30,
        instance_id: str | None = None,
    ) -> None:
        if role not in RUNTIME_PROCESS_ROLES:
            raise ValueError("teaching runtime role is invalid")
        if not math.isfinite(heartbeat_interval_seconds) or heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._repository = repository
        self._role = role
        self._heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self._instance_id = instance_id or new_runtime_instance_id(role)

    async def _heartbeat_until_cancelled(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            try:
                current = await self._repository.heartbeat(
                    self._role,
                    self._instance_id,
                )
            except Exception:
                raise RuntimeHeartbeatUnavailable(
                    "teaching runtime heartbeat unavailable"
                ) from None
            if not current:
                raise RuntimeHeartbeatUnavailable("teaching runtime heartbeat unavailable")

    async def run(self, workload: Callable[[], Awaitable[_T]]) -> _T:
        try:
            await self._repository.register(self._role, self._instance_id)
        except Exception:
            raise RuntimeHeartbeatUnavailable("teaching runtime heartbeat unavailable") from None

        workload_task = asyncio.create_task(workload())
        heartbeat_task = asyncio.create_task(
            self._heartbeat_until_cancelled(),
            name=f"teaching-runtime-heartbeat:{self._role}",
        )
        failed = False
        try:
            done, _ = await asyncio.wait(
                {workload_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if workload_task in done:
                result = await workload_task
                if heartbeat_task not in done:
                    return result
                await heartbeat_task
                raise RuntimeHeartbeatUnavailable("teaching runtime heartbeat unavailable")
            await heartbeat_task
            raise RuntimeHeartbeatUnavailable("teaching runtime heartbeat unavailable")
        except BaseException:
            failed = True
            raise
        finally:
            for task in (workload_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                workload_task,
                heartbeat_task,
                return_exceptions=True,
            )
            try:
                stopped = await self._repository.mark_stopped(
                    self._role,
                    self._instance_id,
                )
            except Exception:
                if not failed:
                    raise RuntimeHeartbeatUnavailable(
                        "teaching runtime heartbeat unavailable"
                    ) from None
                _LOGGER.warning("Teaching runtime stop heartbeat was not persisted")
            else:
                if not stopped and not failed:
                    raise RuntimeHeartbeatUnavailable("teaching runtime heartbeat unavailable")
                if not stopped:
                    _LOGGER.warning("Teaching runtime stop heartbeat fence was lost")


__all__ = [
    "RUNTIME_HEARTBEAT_PRUNE_BATCH_SIZE",
    "RUNTIME_HEARTBEAT_RETENTION_SECONDS",
    "RUNTIME_PROCESS_ROLES",
    "RuntimeHeartbeatRepository",
    "RuntimeHeartbeatSnapshot",
    "RuntimeHeartbeatSupervisor",
    "RuntimeHeartbeatUnavailable",
    "RuntimeProcessRole",
    "new_runtime_instance_id",
]
