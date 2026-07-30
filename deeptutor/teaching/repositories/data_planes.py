"""Fail-closed PostgreSQL access for OpenMAIC data-plane routing."""

from __future__ import annotations

from typing import cast

from sqlalchemy import and_, func, or_, select, update

from deeptutor.teaching.database import platform_session
from deeptutor.teaching.models import (
    AuditLog,
    DataPlaneRoute,
    ProviderProfile,
    Tenant,
)
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneDecision,
    DataPlaneMode,
    DataPlaneResolution,
    DataPlaneRouteRecord,
    DataPlaneSelection,
    ProviderProfileRecord,
)

_AUDIT_ACTION_PREFIX = "teaching.data_plane"
_AUDIT_RESOURCE_PREFIX = "data_plane_route"


def _route_record(route: DataPlaneRoute) -> DataPlaneRouteRecord:
    return DataPlaneRouteRecord(
        route_id=route.id,
        tenant_id=route.tenant_id,
        owner_key=route.owner_key,
        mode=cast(DataPlaneMode, route.mode),
        base_url=route.base_url,
        worker_pool=route.worker_pool,
        queue_name=route.queue_name,
        provider_profile_id=route.provider_profile_id,
        status=route.status,
        health_status=route.health_status,
    )


def _profile_record(profile: ProviderProfile) -> ProviderProfileRecord:
    return ProviderProfileRecord(
        profile_id=profile.id,
        scope=cast(DataPlaneMode, profile.scope),
        tenant_id=profile.tenant_id,
        owner_key=profile.owner_key,
        provider_type=profile.provider_type,
        model_name=profile.model_name,
        api_base_url=profile.api_base_url,
        secret_ref=profile.secret_ref,
        status=profile.status,
    )


def _audit_resource_id(decision: DataPlaneDecision) -> str | None:
    if decision.route_ref is None or decision.provider_profile_ref is None:
        return None
    resource_id = f"{decision.route_ref}/{decision.provider_profile_ref}"
    if len(resource_id) > 128:
        return None
    return resource_id


class SqlAlchemyDataPlaneRepository:
    """Read the exact tenant route and persist secret-free decisions."""

    async def resolve(self, tenant_id: str) -> DataPlaneResolution | None:
        async with platform_session() as session:
            result = await session.execute(
                select(Tenant, DataPlaneRoute, ProviderProfile)
                .outerjoin(
                    DataPlaneRoute,
                    or_(
                        and_(
                            Tenant.data_plane_mode == "shared",
                            DataPlaneRoute.mode == "shared",
                            DataPlaneRoute.tenant_id.is_(None),
                            DataPlaneRoute.owner_key == "shared",
                        ),
                        and_(
                            Tenant.data_plane_mode == "dedicated",
                            DataPlaneRoute.mode == "dedicated",
                            DataPlaneRoute.tenant_id == Tenant.id,
                            DataPlaneRoute.owner_key == Tenant.id,
                        ),
                    ),
                )
                .outerjoin(
                    ProviderProfile,
                    and_(
                        ProviderProfile.id == DataPlaneRoute.provider_profile_id,
                        ProviderProfile.scope == DataPlaneRoute.mode,
                        ProviderProfile.owner_key == DataPlaneRoute.owner_key,
                    ),
                )
                .where(
                    Tenant.id == tenant_id,
                    Tenant.status == "active",
                )
            )
            row = result.one_or_none()
            if row is None:
                return None
            tenant, route, profile = row
            return DataPlaneResolution(
                tenant_id=tenant.id,
                tenant_mode=cast(DataPlaneMode, tenant.data_plane_mode),
                route=_route_record(route) if route is not None else None,
                provider_profile=(_profile_record(profile) if profile is not None else None),
            )

    async def record_decision(self, decision: DataPlaneDecision) -> None:
        async with platform_session() as session:
            async with session.begin():
                tenant_exists = await session.scalar(
                    select(Tenant.id).where(Tenant.id == decision.tenant_id)
                )
                if tenant_exists is None:
                    return
                mode = decision.mode or "none"
                session.add(
                    AuditLog(
                        tenant_id=decision.tenant_id,
                        actor_id=None,
                        action=f"{_AUDIT_ACTION_PREFIX}.{decision.decision}",
                        resource_type=f"{_AUDIT_RESOURCE_PREFIX}:{mode}",
                        resource_id=_audit_resource_id(decision),
                    )
                )

    async def resolve_bound_profile(
        self,
        selection: DataPlaneSelection,
    ) -> ProviderProfileRecord | None:
        expected_tenant_id = None if selection.mode == "shared" else selection.tenant_id
        expected_owner_key = "shared" if selection.mode == "shared" else selection.tenant_id
        async with platform_session() as session:
            profile = await session.scalar(
                select(ProviderProfile)
                .join(
                    DataPlaneRoute,
                    and_(
                        DataPlaneRoute.provider_profile_id == ProviderProfile.id,
                        DataPlaneRoute.mode == ProviderProfile.scope,
                        DataPlaneRoute.owner_key == ProviderProfile.owner_key,
                    ),
                )
                .join(Tenant, Tenant.id == selection.tenant_id)
                .where(
                    Tenant.status == "active",
                    Tenant.data_plane_mode == selection.mode,
                    DataPlaneRoute.id == selection.route_ref,
                    DataPlaneRoute.tenant_id == expected_tenant_id,
                    DataPlaneRoute.owner_key == expected_owner_key,
                    DataPlaneRoute.mode == selection.mode,
                    DataPlaneRoute.worker_pool == selection.worker_pool_ref,
                    DataPlaneRoute.queue_name == selection.queue_ref,
                    DataPlaneRoute.status == "active",
                    DataPlaneRoute.health_status == "healthy",
                    ProviderProfile.id == selection.provider_profile_ref,
                    ProviderProfile.tenant_id == expected_tenant_id,
                    ProviderProfile.owner_key == expected_owner_key,
                    ProviderProfile.scope == selection.mode,
                    ProviderProfile.status == "active",
                )
            )
            return _profile_record(profile) if profile is not None else None

    async def resolve_bound_route(
        self,
        selection: DataPlaneSelection,
    ) -> DataPlaneRouteRecord | None:
        """Re-read and validate the complete active route/profile binding."""

        resolution = await self.resolve(selection.tenant_id)
        if resolution is None or resolution.tenant_mode != selection.mode:
            return None
        route = resolution.route
        profile = resolution.provider_profile
        if route is None or profile is None:
            return None
        expected_tenant_id = None if selection.mode == "shared" else selection.tenant_id
        expected_owner_key = "shared" if selection.mode == "shared" else selection.tenant_id
        if (
            route.route_id != selection.route_ref
            or route.tenant_id != expected_tenant_id
            or route.owner_key != expected_owner_key
            or route.mode != selection.mode
            or route.worker_pool != selection.worker_pool_ref
            or route.queue_name != selection.queue_ref
            or route.provider_profile_id != selection.provider_profile_ref
            or route.status != "active"
            or route.health_status != "healthy"
            or profile.profile_id != selection.provider_profile_ref
            or profile.scope != selection.mode
            or profile.tenant_id != expected_tenant_id
            or profile.owner_key != expected_owner_key
            or profile.status != "active"
        ):
            return None
        return route

    async def set_health(self, route_id: str, health_status: str) -> bool:
        if health_status not in {"healthy", "unhealthy"}:
            raise ValueError("health_status must be healthy or unhealthy")
        async with platform_session() as session:
            async with session.begin():
                result = await session.execute(
                    update(DataPlaneRoute)
                    .where(DataPlaneRoute.id == route_id)
                    .values(
                        health_status=health_status,
                        health_checked_at=func.now(),
                        updated_at=func.now(),
                    )
                )
                return result.rowcount == 1
