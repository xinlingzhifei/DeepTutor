"""Fail-closed health reporting and allowlisted teaching failure logs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
import re
from typing import Callable, Literal, Mapping, Protocol

from deeptutor.teaching.runtime_heartbeat import (
    RUNTIME_PROCESS_ROLES,
    RuntimeHeartbeatRepository,
)

HealthStatus = Literal["healthy", "stale", "unhealthy", "unknown"]
DataPlaneMode = Literal["shared", "dedicated"]
DataPlaneService = Literal["openmaic", "render"]
HEARTBEAT_HEALTH_TIMEOUT_SECONDS = 1.0

ACTIVE_HEALTH_COMPONENTS = (
    "database",
    "migrations",
    "object_store",
    "openmaic_shared",
    "render_shared",
    "dedicated_data_planes",
)

REQUIRED_HEALTH_COMPONENTS = (
    "database",
    "migrations",
    "object_store",
    "tenant_provisioner",
    "dispatcher",
    "generation_worker",
    "export_worker",
    "projector",
    "openmaic_shared",
    "render_shared",
    "dedicated_data_planes",
    "reaper",
)

_STATUS_VALUES = frozenset({"healthy", "unhealthy", "unknown"})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    status: HealthStatus
    checked_at: datetime | None = None
    age_seconds: float | None = None
    reason: str | None = None
    service: DataPlaneService | None = None
    mode: DataPlaneMode | None = None


@dataclass(frozen=True, slots=True)
class TeachingHealthReport:
    status: Literal["healthy", "degraded"]
    generated_at: datetime
    components: dict[str, ComponentHealth]


@dataclass(frozen=True, slots=True)
class _Signal:
    status: Literal["healthy", "unhealthy", "unknown"]
    checked_at: datetime | None
    reason: str | None = None
    heartbeat: bool = False
    heartbeat_age_seconds: float | None = None
    service: DataPlaneService | None = None
    mode: DataPlaneMode | None = None


class ActiveComponentResult(Protocol):
    status: Literal["healthy", "unhealthy"]
    reason: str | None


_ACTIVE_COMPONENT_METADATA: dict[
    str,
    tuple[DataPlaneService | None, DataPlaneMode | None],
] = {
    "database": (None, None),
    "migrations": (None, None),
    "object_store": (None, None),
    "openmaic_shared": ("openmaic", "shared"),
    "render_shared": ("render", "shared"),
    "dedicated_data_planes": ("openmaic", "dedicated"),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_token(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


class TeachingHealthService:
    """Build one complete health report without treating missing signals as healthy."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = _utc_now,
        stale_after_seconds: float = 90,
        heartbeat_timeout_seconds: float = HEARTBEAT_HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if not math.isfinite(heartbeat_timeout_seconds) or heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be positive")
        self._now = now
        self._stale_after_seconds = float(stale_after_seconds)
        self._heartbeat_timeout_seconds = float(heartbeat_timeout_seconds)
        self._signals: dict[str, _Signal] = {
            component: _Signal(status="unknown", checked_at=None)
            for component in REQUIRED_HEALTH_COMPONENTS
        }
        self._data_planes: dict[str, _Signal] = {}

    def set_status(
        self,
        component: str,
        status: Literal["healthy", "unhealthy", "unknown"],
        *,
        reason: str | None = None,
    ) -> None:
        if component not in self._signals:
            raise ValueError("health component is invalid")
        if status not in _STATUS_VALUES:
            raise ValueError("health status is invalid")
        if reason is not None:
            _safe_token(reason, "health reason")
        self._signals[component] = _Signal(
            status=status,
            checked_at=self._now(),
            reason=reason,
        )

    def set_heartbeat(self, component: str, *, age_seconds: float = 0) -> None:
        if component not in self._signals:
            raise ValueError("health component is invalid")
        if not math.isfinite(age_seconds) or age_seconds < 0:
            raise ValueError("heartbeat age must be non-negative")
        self._signals[component] = _Signal(
            status="healthy",
            checked_at=self._now() - timedelta(seconds=age_seconds),
            heartbeat=True,
        )

    def set_data_plane_health(
        self,
        *,
        route_id: str,
        mode: DataPlaneMode,
        service: DataPlaneService,
        status: Literal["healthy", "unhealthy", "unknown"],
        reason: str | None = None,
    ) -> None:
        route_id = _safe_token(route_id, "data-plane route")
        if mode not in {"shared", "dedicated"}:
            raise ValueError("data-plane mode is invalid")
        if service not in {"openmaic", "render"}:
            raise ValueError("data-plane service is invalid")
        if status not in _STATUS_VALUES:
            raise ValueError("health status is invalid")
        if reason is not None:
            _safe_token(reason, "health reason")
        self._data_planes[route_id] = _Signal(
            status=status,
            checked_at=self._now(),
            reason=reason,
            service=service,
            mode=mode,
        )

    def _component(self, signal: _Signal, now: datetime) -> ComponentHealth:
        age_seconds: float | None = None
        status: HealthStatus = signal.status
        reason = signal.reason
        if signal.heartbeat_age_seconds is not None:
            age_seconds = max(0.0, signal.heartbeat_age_seconds)
        elif signal.checked_at is not None:
            age_seconds = max(0.0, (now - signal.checked_at).total_seconds())
        if signal.heartbeat and age_seconds is not None:
            if age_seconds > self._stale_after_seconds:
                status = "stale"
                reason = "heartbeat_stale"
            else:
                status = "healthy"
        return ComponentHealth(
            status=status,
            checked_at=signal.checked_at,
            age_seconds=age_seconds,
            reason=reason,
            service=signal.service,
            mode=signal.mode,
        )

    def _report(
        self,
        *,
        signal_overrides: dict[str, _Signal] | None = None,
        include_data_planes: bool = True,
        now: datetime | None = None,
    ) -> TeachingHealthReport:
        now = self._now() if now is None else now
        signals = dict(self._signals)
        if signal_overrides is not None:
            signals.update(signal_overrides)
        components = {name: self._component(signal, now) for name, signal in signals.items()}
        if include_data_planes:
            components.update(
                {
                    f"data_plane:{route_id}": self._component(signal, now)
                    for route_id, signal in self._data_planes.items()
                }
            )
        status = (
            "healthy"
            if components and all(item.status == "healthy" for item in components.values())
            else "degraded"
        )
        return TeachingHealthReport(
            status=status,
            generated_at=now,
            components=components,
        )

    def report(self) -> TeachingHealthReport:
        return self._report()

    async def _durable_overrides(
        self,
        repository: RuntimeHeartbeatRepository,
    ) -> dict[str, _Signal]:
        try:
            async with asyncio.timeout(self._heartbeat_timeout_seconds):
                snapshots = await repository.latest_running_heartbeats(RUNTIME_PROCESS_ROLES)
        except Exception:
            return {
                role: _Signal(
                    status="unknown",
                    checked_at=None,
                    reason="heartbeat_repository_unavailable",
                )
                for role in RUNTIME_PROCESS_ROLES
            }

        latest: dict[str, float] = {}
        for snapshot in snapshots:
            age_seconds = max(0.0, float(snapshot.age_seconds))
            existing = latest.get(snapshot.role)
            if existing is None or age_seconds < existing:
                latest[snapshot.role] = age_seconds
        return {
            role: (
                _Signal(
                    status="healthy",
                    checked_at=None,
                    heartbeat=True,
                    heartbeat_age_seconds=latest[role],
                )
                if role in latest
                else _Signal(
                    status="unknown",
                    checked_at=None,
                    reason="heartbeat_missing",
                )
            )
            for role in RUNTIME_PROCESS_ROLES
        }

    async def report_active(
        self,
        repository: RuntimeHeartbeatRepository,
        active_results: Mapping[str, ActiveComponentResult],
    ) -> TeachingHealthReport:
        """Merge request-local active results with only durable role heartbeats."""

        if set(active_results) != set(ACTIVE_HEALTH_COMPONENTS):
            raise ValueError("active health results must cover every dependency")
        checked_at = self._now()
        active_overrides: dict[str, _Signal] = {}
        for component in ACTIVE_HEALTH_COMPONENTS:
            result = active_results[component]
            if result.status not in {"healthy", "unhealthy"}:
                raise ValueError("active health status is invalid")
            if result.reason is not None:
                _safe_token(result.reason, "health reason")
            service, mode = _ACTIVE_COMPONENT_METADATA[component]
            active_overrides[component] = _Signal(
                status=result.status,
                checked_at=checked_at,
                reason=result.reason,
                service=service,
                mode=mode,
            )
        durable_overrides = await self._durable_overrides(repository)
        return self._report(
            signal_overrides={**active_overrides, **durable_overrides},
            include_data_planes=False,
            now=checked_at,
        )

    async def report_durable(
        self,
        repository: RuntimeHeartbeatRepository,
    ) -> TeachingHealthReport:
        return self._report(signal_overrides=await self._durable_overrides(repository))


_service = TeachingHealthService()


def get_teaching_health_service() -> TeachingHealthService:
    return _service


def _safe_log_identifier(value: str) -> str:
    return value if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) else "invalid"


def log_generation_failure(
    *,
    tenant_id: str,
    job_id: str,
    route_id: str,
    error_code: str,
    source_text: object | None = None,
    provider_key: object | None = None,
) -> None:
    """Log only correlation identifiers; sensitive inputs are deliberately discarded."""

    _ = (source_text, provider_key)
    _LOGGER.error(
        "Classroom generation failed",
        extra={
            "tenant_id": _safe_log_identifier(tenant_id),
            "job_id": _safe_log_identifier(job_id),
            "route_id": _safe_log_identifier(route_id),
            "error_code": _safe_log_identifier(error_code),
        },
    )
