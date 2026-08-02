"""Fail-closed OpenMAIC data-plane selection contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NoReturn, Protocol

DataPlaneMode = Literal["shared", "dedicated"]


class DataPlaneUnavailable(RuntimeError):
    """No eligible OpenMAIC data plane exists for the current tenant."""

    def __init__(self) -> None:
        super().__init__("data plane is unavailable")


@dataclass(frozen=True, slots=True)
class ProviderProfileRecord:
    """Secret-free provider metadata plus one opaque secret reference."""

    profile_id: str
    scope: DataPlaneMode
    tenant_id: str | None
    owner_key: str
    provider_type: str
    model_name: str
    api_base_url: str | None
    secret_ref: str
    status: str


@dataclass(frozen=True, slots=True)
class DataPlaneRouteRecord:
    """Trusted control-plane route record."""

    route_id: str
    tenant_id: str | None
    owner_key: str
    mode: DataPlaneMode
    base_url: str
    worker_pool: str
    queue_name: str
    provider_profile_id: str
    status: str
    health_status: str


@dataclass(frozen=True, slots=True)
class DataPlaneResolution:
    """Repository result before the selector removes sensitive metadata."""

    tenant_id: str
    tenant_mode: DataPlaneMode
    route: DataPlaneRouteRecord | None
    provider_profile: ProviderProfileRecord | None


@dataclass(frozen=True, slots=True)
class DataPlaneSelection:
    """Opaque route references safe to hand to later job orchestration."""

    tenant_id: str
    route_ref: str
    provider_profile_ref: str
    mode: DataPlaneMode
    worker_pool_ref: str
    queue_ref: str


@dataclass(frozen=True, slots=True)
class DataPlaneDecision:
    """Secret-free audit payload for one route-selection decision."""

    tenant_id: str
    route_ref: str | None
    provider_profile_ref: str | None
    mode: DataPlaneMode | None
    decision: Literal["selected", "unavailable"]
    reason_code: str


class DataPlaneSelectionRepository(Protocol):
    async def resolve(self, tenant_id: str) -> DataPlaneResolution | None: ...

    async def record_decision(self, decision: DataPlaneDecision) -> None: ...


class _TeachingSettings(Protocol):
    enabled: bool


class DataPlaneSelector:
    """Select one route without exposing endpoint or provider secret metadata."""

    def __init__(
        self,
        *,
        settings: _TeachingSettings,
        repository: DataPlaneSelectionRepository,
    ) -> None:
        self._settings = settings
        self._repository = repository

    async def _reject(
        self,
        tenant_id: str,
        resolution: DataPlaneResolution,
        *,
        reason_code: str,
    ) -> NoReturn:
        route = resolution.route
        profile = resolution.provider_profile
        await self._repository.record_decision(
            DataPlaneDecision(
                tenant_id=tenant_id,
                route_ref=route.route_id if route is not None else None,
                provider_profile_ref=(profile.profile_id if profile is not None else None),
                mode=resolution.tenant_mode,
                decision="unavailable",
                reason_code=reason_code,
            )
        )
        raise DataPlaneUnavailable()

    async def resolve(self, tenant_id: str) -> DataPlaneSelection | None:
        if not self._settings.enabled:
            return None
        resolution = await self._repository.resolve(tenant_id)
        if resolution is None:
            await self._repository.record_decision(
                DataPlaneDecision(
                    tenant_id=tenant_id,
                    route_ref=None,
                    provider_profile_ref=None,
                    mode=None,
                    decision="unavailable",
                    reason_code="no_route",
                )
            )
            raise DataPlaneUnavailable()
        route = resolution.route
        profile = resolution.provider_profile
        if resolution.tenant_id != tenant_id:
            await self._reject(
                tenant_id,
                resolution,
                reason_code="tenant_mismatch",
            )
        if route is None:
            await self._reject(
                tenant_id,
                resolution,
                reason_code="no_route",
            )
        if profile is None:
            await self._reject(
                tenant_id,
                resolution,
                reason_code="no_provider_profile",
            )
        if resolution.tenant_mode != route.mode:
            await self._reject(
                tenant_id,
                resolution,
                reason_code="route_mode_mismatch",
            )
        expected_tenant_id = None if route.mode == "shared" else tenant_id
        expected_owner_key = "shared" if route.mode == "shared" else tenant_id
        if route.tenant_id != expected_tenant_id or route.owner_key != expected_owner_key:
            await self._reject(
                tenant_id,
                resolution,
                reason_code="route_owner_mismatch",
            )
        if (
            profile.profile_id != route.provider_profile_id
            or profile.scope != route.mode
            or profile.tenant_id != expected_tenant_id
            or profile.owner_key != expected_owner_key
        ):
            await self._reject(
                tenant_id,
                resolution,
                reason_code="provider_binding_mismatch",
            )
        if route.status != "active":
            await self._reject(
                tenant_id,
                resolution,
                reason_code="route_inactive",
            )
        if route.health_status != "healthy":
            await self._reject(
                tenant_id,
                resolution,
                reason_code="route_unhealthy",
            )
        if profile.status != "active":
            await self._reject(
                tenant_id,
                resolution,
                reason_code="provider_inactive",
            )
        selection = DataPlaneSelection(
            tenant_id=tenant_id,
            route_ref=route.route_id,
            provider_profile_ref=profile.profile_id,
            mode=route.mode,
            worker_pool_ref=route.worker_pool,
            queue_ref=route.queue_name,
        )
        await self._repository.record_decision(
            DataPlaneDecision(
                tenant_id=tenant_id,
                route_ref=route.route_id,
                provider_profile_ref=profile.profile_id,
                mode=route.mode,
                decision="selected",
                reason_code="healthy",
            )
        )
        return selection
