"""Transactional authority for generation job data-plane bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.sql import Select

from deeptutor.teaching.models import DataPlaneRoute, ProviderProfile, Tenant


class DataPlaneBindingUnavailable(RuntimeError):
    """The requested job binding is not the tenant's active trusted route."""

    def __init__(self) -> None:
        super().__init__("generation data plane binding is unavailable")


@dataclass(frozen=True, slots=True)
class LockedJobBinding:
    """Database-authoritative binding frozen into one new job row."""

    data_plane_mode: Literal["shared", "dedicated"]
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str


def build_locked_job_binding_statement(
    *,
    tenant_id: str,
    data_plane_route_id: str,
    provider_profile_id: str,
    worker_pool_ref: str,
    queue_ref: str,
    data_plane_mode: str | None = None,
) -> Select[Any]:
    """Lock the complete active tenant/route/profile binding in one transaction."""

    owner_binding = or_(
        and_(
            DataPlaneRoute.mode == "shared",
            DataPlaneRoute.tenant_id.is_(None),
            DataPlaneRoute.owner_key == "shared",
            ProviderProfile.tenant_id.is_(None),
            ProviderProfile.owner_key == "shared",
        ),
        and_(
            DataPlaneRoute.mode == "dedicated",
            DataPlaneRoute.tenant_id == Tenant.id,
            DataPlaneRoute.owner_key == Tenant.id,
            ProviderProfile.tenant_id == Tenant.id,
            ProviderProfile.owner_key == Tenant.id,
        ),
    )
    statement = (
        select(Tenant, DataPlaneRoute, ProviderProfile)
        .select_from(Tenant)
        .join_from(Tenant, DataPlaneRoute, DataPlaneRoute.id == data_plane_route_id)
        .join_from(
            DataPlaneRoute,
            ProviderProfile,
            ProviderProfile.id == DataPlaneRoute.provider_profile_id,
        )
        .where(
            Tenant.id == tenant_id,
            Tenant.status == "active",
            Tenant.data_plane_mode == DataPlaneRoute.mode,
            DataPlaneRoute.id == data_plane_route_id,
            DataPlaneRoute.worker_pool == worker_pool_ref,
            DataPlaneRoute.queue_name == queue_ref,
            DataPlaneRoute.provider_profile_id == provider_profile_id,
            DataPlaneRoute.status == "active",
            DataPlaneRoute.health_status == "healthy",
            ProviderProfile.id == provider_profile_id,
            ProviderProfile.scope == DataPlaneRoute.mode,
            ProviderProfile.owner_key == DataPlaneRoute.owner_key,
            ProviderProfile.status == "active",
            owner_binding,
        )
        .with_for_update(of=(Tenant, DataPlaneRoute, ProviderProfile))
    )
    if data_plane_mode is not None:
        if data_plane_mode not in {"shared", "dedicated"}:
            raise ValueError("data_plane_mode is invalid")
        statement = statement.where(DataPlaneRoute.mode == data_plane_mode)
    return statement


async def lock_active_job_binding(
    session: Any,
    *,
    tenant_id: str,
    data_plane_route_id: str,
    provider_profile_id: str,
    worker_pool_ref: str,
    queue_ref: str,
    data_plane_mode: str | None = None,
) -> LockedJobBinding | None:
    """Return the authoritative complete binding after locking all source rows."""

    result = await session.execute(
        build_locked_job_binding_statement(
            tenant_id=tenant_id,
            data_plane_route_id=data_plane_route_id,
            provider_profile_id=provider_profile_id,
            worker_pool_ref=worker_pool_ref,
            queue_ref=queue_ref,
            data_plane_mode=data_plane_mode,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    tenant, route, profile = row
    if (
        tenant.data_plane_mode not in {"shared", "dedicated"}
        or route.mode != tenant.data_plane_mode
        or profile.scope != route.mode
    ):
        return None
    return LockedJobBinding(
        data_plane_mode=route.mode,
        data_plane_route_id=route.id,
        provider_profile_id=profile.id,
        worker_pool_ref=route.worker_pool,
        queue_ref=route.queue_name,
    )
