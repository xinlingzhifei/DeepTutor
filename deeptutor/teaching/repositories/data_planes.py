"""Fail-closed PostgreSQL access for OpenMAIC data-plane routing."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError

from deeptutor.teaching.database import platform_session
from deeptutor.teaching.models import (
    AuditLog,
    DataPlaneRoute,
    GenerationRouteAttempt,
    ProviderProfile,
    Tenant,
)
from deeptutor.teaching.openmaic.data_planes import (
    ROUTE_BINDING_CONFIG_REVISION,
    DataPlaneAttemptDecision,
    DataPlaneDecision,
    DataPlaneMode,
    DataPlaneResolution,
    DataPlaneRouteRecord,
    DataPlaneSelection,
    DedicatedDataPlaneHealthInventory,
    JobRouteAttemptConflict,
    JobRouteAttemptSummary,
    ProviderProfileRecord,
    provider_config_digest,
    route_config_digest,
)
from deeptutor.teaching.repositories.jobs import JobLeaseLost

_AUDIT_ACTION_PREFIX = "teaching.data_plane"
_AUDIT_RESOURCE_PREFIX = "data_plane_route"


def _required_route_attempt_value(value: str, name: str, max_length: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"{name} is invalid")


def _valid_nonzero_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value != "0" * 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _route_attempt_sqlstate(error: DBAPIError) -> str | None:
    candidate: BaseException | None = error.orig
    for _ in range(3):
        if candidate is None:
            return None
        sqlstate = getattr(candidate, "sqlstate", None)
        if isinstance(sqlstate, str):
            return sqlstate
        candidate = candidate.__cause__ or candidate.__context__
    return None


_RECORD_ROUTE_ATTEMPT = text(
    """
    SELECT platform.record_generation_route_attempt(
        :tenant_id,
        :job_id,
        :phase,
        :attempt_count,
        :data_plane_mode,
        :data_plane_route_id,
        :provider_profile_id,
        :worker_pool_ref,
        :queue_ref,
        :worker_id,
        :lease_token,
        :decision,
        :config_revision,
        :route_config_digest,
        :provider_config_digest
    )
    """
)
_READ_ROUTE_ATTEMPTS = text(
    """
    SELECT * FROM platform.read_generation_route_attempts(
        :tenant_id,
        :job_id,
        :data_plane_mode,
        :data_plane_route_id,
        :provider_profile_id,
        :worker_pool_ref,
        :queue_ref
    )
    """
)


def _validated_route_attempt_counts(
    attempts: Sequence[GenerationRouteAttempt],
    *,
    phase: str,
    expected_attempt_count: int,
    expected_data_plane_mode: DataPlaneMode,
    expected_route_id: str,
    expected_provider_profile_id: str,
    expected_worker_pool_ref: str,
    expected_queue_ref: str,
) -> tuple[int, int] | None:
    if len(attempts) != expected_attempt_count:
        return None
    if phase not in {"outline", "content", "export"}:
        return None

    selected_attempt_count = 0
    unavailable_attempt_count = 0
    content_started = False
    for expected_number, attempt in enumerate(attempts, start=1):
        if phase == "content":
            if attempt.phase == "content":
                content_started = True
            elif attempt.phase != "outline" or content_started:
                return None
        elif attempt.phase != phase:
            return None
        if (
            attempt.attempt_count != expected_number
            or attempt.data_plane_mode != expected_data_plane_mode
            or attempt.data_plane_route_id != expected_route_id
            or attempt.provider_profile_id != expected_provider_profile_id
            or attempt.worker_pool_ref != expected_worker_pool_ref
            or attempt.queue_ref != expected_queue_ref
            or not attempt.worker_id.strip()
            or attempt.decision not in {"selected", "unavailable"}
            or (
                attempt.decision == "selected"
                and (
                    attempt.config_revision != ROUTE_BINDING_CONFIG_REVISION
                    or not _valid_nonzero_sha256(attempt.route_config_digest)
                    or not _valid_nonzero_sha256(attempt.provider_config_digest)
                )
            )
            or (
                attempt.decision == "unavailable"
                and any(
                    value is not None
                    for value in (
                        attempt.config_revision,
                        attempt.route_config_digest,
                        attempt.provider_config_digest,
                    )
                )
            )
        ):
            return None
        if attempt.decision == "selected":
            selected_attempt_count += 1
        else:
            unavailable_attempt_count += 1

    if phase == "content" and not content_started:
        return None
    if selected_attempt_count + unavailable_attempt_count != expected_attempt_count:
        return None
    return selected_attempt_count, unavailable_attempt_count


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


def build_shared_health_route_statement():
    """Read an active shared route with its complete active profile binding."""

    return (
        select(DataPlaneRoute)
        .join(
            ProviderProfile,
            and_(
                ProviderProfile.id == DataPlaneRoute.provider_profile_id,
                ProviderProfile.scope == DataPlaneRoute.mode,
                ProviderProfile.tenant_id.is_(None),
                ProviderProfile.owner_key == DataPlaneRoute.owner_key,
            ),
        )
        .where(
            DataPlaneRoute.mode == "shared",
            DataPlaneRoute.tenant_id.is_(None),
            DataPlaneRoute.owner_key == "shared",
            DataPlaneRoute.status == "active",
            ProviderProfile.status == "active",
        )
    )


def build_dedicated_health_inventory_statement():
    """Inventory every active dedicated tenant and its route without provider secrets."""

    return (
        select(
            Tenant,
            DataPlaneRoute,
            ProviderProfile.id.label("health_profile_id"),
        )
        .outerjoin(
            DataPlaneRoute,
            and_(
                DataPlaneRoute.tenant_id == Tenant.id,
                DataPlaneRoute.mode == "dedicated",
            ),
        )
        .outerjoin(
            ProviderProfile,
            and_(
                ProviderProfile.id == DataPlaneRoute.provider_profile_id,
                ProviderProfile.scope == DataPlaneRoute.mode,
                ProviderProfile.tenant_id == Tenant.id,
                ProviderProfile.owner_key == Tenant.id,
                ProviderProfile.status == "active",
            ),
        )
        .where(
            Tenant.status == "active",
            Tenant.data_plane_mode == "dedicated",
        )
        .order_by(Tenant.id)
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

    async def resolve_shared_health_route(self) -> DataPlaneRouteRecord | None:
        async with platform_session() as session:
            route = await session.scalar(build_shared_health_route_statement())
        return _route_record(route) if route is not None else None

    async def resolve_dedicated_health_inventory(
        self,
    ) -> DedicatedDataPlaneHealthInventory:
        async with platform_session() as session:
            rows = (await session.execute(build_dedicated_health_inventory_statement())).all()
        routes: list[DataPlaneRouteRecord] = []
        unavailable = 0
        for tenant, route, health_profile_id in rows:
            if (
                route is None
                or health_profile_id is None
                or route.status != "active"
                or route.mode != "dedicated"
                or route.tenant_id != tenant.id
                or route.owner_key != tenant.id
                or route.provider_profile_id != health_profile_id
            ):
                unavailable += 1
                continue
            routes.append(_route_record(route))
        return DedicatedDataPlaneHealthInventory(
            active_tenants=len(rows),
            routes=tuple(routes),
            unavailable_tenants=unavailable,
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

    async def record_job_route_attempt(
        self,
        *,
        tenant_id: str,
        job_id: str,
        phase: str,
        attempt_count: int,
        mode: DataPlaneMode,
        data_plane_route_id: str,
        provider_profile_id: str,
        worker_pool_ref: str,
        queue_ref: str,
        worker_id: str,
        lease_token: str,
        outcome: DataPlaneAttemptDecision,
        config_revision: str | None = None,
        route_config_digest: str | None = None,
        provider_config_digest: str | None = None,
    ) -> None:
        """Persist one worker claim's attempted plane before exposing a client."""

        for value, name, max_length in (
            (tenant_id, "tenant_id", 64),
            (job_id, "job_id", 64),
            (phase, "phase", 16),
            (data_plane_route_id, "data_plane_route_id", 63),
            (provider_profile_id, "provider_profile_id", 63),
            (worker_pool_ref, "worker_pool_ref", 128),
            (queue_ref, "queue_ref", 128),
            (worker_id, "worker_id", 128),
            (lease_token, "lease_token", 64),
        ):
            _required_route_attempt_value(value, name, max_length)
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count <= 0
        ):
            raise ValueError("attempt_count must be a positive integer")
        if mode not in {"shared", "dedicated"}:
            raise ValueError("mode is invalid")
        if phase not in {"outline", "content", "export"}:
            raise ValueError("phase is invalid")
        if outcome not in {"selected", "unavailable"}:
            raise ValueError("outcome is invalid")
        if outcome == "selected":
            if config_revision != ROUTE_BINDING_CONFIG_REVISION:
                raise ValueError("config_revision is invalid")
            for value, name in (
                (route_config_digest, "route_config_digest"),
                (provider_config_digest, "provider_config_digest"),
            ):
                if not _valid_nonzero_sha256(value):
                    raise ValueError(f"{name} is invalid")
        elif any(
            value is not None
            for value in (config_revision, route_config_digest, provider_config_digest)
        ):
            raise ValueError("unavailable route attempt cannot claim configuration")

        parameters = {
            "tenant_id": tenant_id,
            "job_id": job_id,
            "phase": phase,
            "attempt_count": attempt_count,
            "data_plane_mode": mode,
            "data_plane_route_id": data_plane_route_id,
            "provider_profile_id": provider_profile_id,
            "worker_pool_ref": worker_pool_ref,
            "queue_ref": queue_ref,
            "worker_id": worker_id,
            "decision": outcome,
            "lease_token": lease_token,
            "config_revision": config_revision,
            "route_config_digest": route_config_digest,
            "provider_config_digest": provider_config_digest,
        }
        try:
            async with platform_session() as session:
                async with session.begin():
                    recorded = await session.scalar(_RECORD_ROUTE_ATTEMPT, parameters)
                    if recorded is not True:
                        raise RuntimeError("database did not confirm route attempt evidence")
        except DBAPIError as exc:
            sqlstate = _route_attempt_sqlstate(exc)
            if sqlstate == "PGR01":
                raise ValueError("route attempt was rejected by database validation") from None
            if sqlstate == "PGR02":
                raise JobLeaseLost("route attempt lease fence no longer matches") from None
            if sqlstate == "PGR03":
                raise JobRouteAttemptConflict() from None
            raise RuntimeError("route attempt evidence write failed") from None

    async def resolve_job_route_audit(
        self,
        tenant_id: str,
        job_id: str,
        *,
        phase: str,
        expected_attempt_count: int,
        expected_data_plane_mode: DataPlaneMode,
        expected_route_id: str,
        expected_provider_profile_id: str,
        expected_worker_pool_ref: str,
        expected_queue_ref: str,
    ) -> JobRouteAttemptSummary | None:
        """Validate a complete 1..N job-bound route-attempt history."""

        if (
            isinstance(expected_attempt_count, bool)
            or not isinstance(expected_attempt_count, int)
            or expected_attempt_count <= 0
        ):
            raise ValueError("expected_attempt_count must be a positive integer")
        if expected_data_plane_mode not in {"shared", "dedicated"}:
            return None

        async with platform_session() as session:
            result = await session.execute(
                _READ_ROUTE_ATTEMPTS,
                {
                    "tenant_id": tenant_id,
                    "job_id": job_id,
                    "data_plane_mode": expected_data_plane_mode,
                    "data_plane_route_id": expected_route_id,
                    "provider_profile_id": expected_provider_profile_id,
                    "worker_pool_ref": expected_worker_pool_ref,
                    "queue_ref": expected_queue_ref,
                },
            )
            attempts = [GenerationRouteAttempt(**dict(row)) for row in result.mappings().all()]
        counts = _validated_route_attempt_counts(
            attempts,
            phase=phase,
            expected_attempt_count=expected_attempt_count,
            expected_data_plane_mode=expected_data_plane_mode,
            expected_route_id=expected_route_id,
            expected_provider_profile_id=expected_provider_profile_id,
            expected_worker_pool_ref=expected_worker_pool_ref,
            expected_queue_ref=expected_queue_ref,
        )
        if counts is None:
            return None
        selected_attempt_count, unavailable_attempt_count = counts
        last_attempt = attempts[-1]
        return JobRouteAttemptSummary(
            data_plane_mode=expected_data_plane_mode,
            attempt_count=expected_attempt_count,
            shared_attempt_count=(
                expected_attempt_count if expected_data_plane_mode == "shared" else 0
            ),
            dedicated_attempt_count=(
                expected_attempt_count if expected_data_plane_mode == "dedicated" else 0
            ),
            selected_attempt_count=selected_attempt_count,
            unavailable_attempt_count=unavailable_attempt_count,
            last_attempt_phase=cast(
                Literal["outline", "content", "export"],
                last_attempt.phase,
            ),
            last_attempt_decision=cast(
                DataPlaneAttemptDecision,
                last_attempt.decision,
            ),
            final_phase_selected=(
                last_attempt.phase == phase and last_attempt.decision == "selected"
            ),
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
            if profile is None or selection.config_revision != ROUTE_BINDING_CONFIG_REVISION:
                return None
            record = _profile_record(profile)
            if provider_config_digest(record) != selection.provider_config_digest:
                return None
            return record

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
            or selection.config_revision != ROUTE_BINDING_CONFIG_REVISION
            or route_config_digest(route) != selection.route_config_digest
            or provider_config_digest(profile) != selection.provider_config_digest
        ):
            return None
        return route

    async def resolve_worker_selection(
        self,
        *,
        tenant_id: str,
        route_id: str,
        provider_profile_id: str,
        worker_pool_ref: str,
        queue_ref: str,
    ) -> DataPlaneSelection | None:
        """Resolve one persisted job binding without trusting process input."""

        resolution = await self.resolve(tenant_id)
        if resolution is None:
            return None
        route = resolution.route
        profile = resolution.provider_profile
        if (
            route is None
            or profile is None
            or route.route_id != route_id
            or route.provider_profile_id != provider_profile_id
            or route.worker_pool != worker_pool_ref
            or route.queue_name != queue_ref
            or route.status != "active"
            or route.health_status != "healthy"
            or profile.profile_id != provider_profile_id
            or profile.status != "active"
        ):
            return None
        return DataPlaneSelection(
            tenant_id=tenant_id,
            route_ref=route.route_id,
            provider_profile_ref=profile.profile_id,
            mode=route.mode,
            worker_pool_ref=route.worker_pool,
            queue_ref=route.queue_name,
            config_revision=ROUTE_BINDING_CONFIG_REVISION,
            route_config_digest=route_config_digest(route),
            provider_config_digest=provider_config_digest(profile),
        )

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
