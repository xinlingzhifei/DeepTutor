"""Bounded, secret-safe active dependency probes for teaching health."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
import math
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import and_, func, literal_column, or_, select, text

from deeptutor.services.config import PlatformSettings
from deeptutor.services.config.platform_settings import (
    validate_object_store_endpoint,
    validate_render_health_url,
)
from deeptutor.teaching.database import platform_session
from deeptutor.teaching.health import ACTIVE_HEALTH_COMPONENTS
from deeptutor.teaching.health_logging import redact_health_transport_logs
from deeptutor.teaching.migrations.runner import TEACHING_MIGRATION_HEAD_REVISION
from deeptutor.teaching.models import Tenant, TenantSchemaState
from deeptutor.teaching.object_store import (
    LocalClassroomArtifactStore,
    ObjectStoreError,
    check_s3_object_store_health,
    run_s3_health_sync,
)
from deeptutor.teaching.openmaic.client import (
    ClientTimeouts,
    IncompatibleOpenMAIC,
    OpenMAICContractHealthClient,
)
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneRouteRecord,
    DedicatedDataPlaneHealthInventory,
)
from deeptutor.teaching.storage_credentials import (
    ResolvedStorageCredentials,
    StorageHealthInventoryRepository,
    TenantStorageCredentialRecord,
)

ACTIVE_PROBE_TIMEOUT_SECONDS = 2.0
ACTIVE_PROBE_REQUEST_TIMEOUT_SECONDS = 3.0
TEACHING_SCHEMA_REVISION = TEACHING_MIGRATION_HEAD_REVISION
OPENMAIC_HEALTH_CONNECT_TIMEOUT_SECONDS = 0.5
OPENMAIC_HEALTH_READ_TIMEOUT_SECONDS = 1.0
DEDICATED_HEALTH_PROBE_CONCURRENCY = 4
OBJECT_STORE_HEALTH_PROBE_CONCURRENCY = 4

# Keep the established public health component names.  Dedicated planes are
# represented only by one aggregate so route and tenant identifiers never enter
# the health response.
ACTIVE_PROBE_COMPONENTS = ACTIVE_HEALTH_COMPONENTS

_REASON_CODES = frozenset(
    {
        "probe_failed",
        "probe_timeout",
        "request_timeout",
        "database_unavailable",
        "platform_revision_missing",
        "platform_revision_mismatch",
        "tenant_revision_missing",
        "tenant_revision_outdated",
        "object_store_unavailable",
        "contract_mismatch",
        "render_unhealthy",
        "route_unavailable",
    }
)

HealthProbe = Callable[[], Awaitable[None]]
SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


class HealthProbeFailure(RuntimeError):
    """One allowlisted dependency failure safe to expose to an administrator."""

    def __init__(self, reason: str) -> None:
        if reason not in _REASON_CODES:
            raise ValueError("health probe reason is invalid")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ActiveProbeResult:
    status: Literal["healthy", "unhealthy"]
    reason: str | None = None


class DatabaseHealthProbe:
    """Execute a read-only query through the cached platform session boundary."""

    def __init__(self, session_factory: SessionFactory = platform_session) -> None:
        self._session_factory = session_factory

    async def __call__(self) -> None:
        try:
            async with self._session_factory() as session:
                result = await session.scalar(text("SELECT 1"))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise HealthProbeFailure("database_unavailable") from None
        if result != 1:
            raise HealthProbeFailure("database_unavailable")


@dataclass(frozen=True, slots=True)
class MigrationHealthSnapshot:
    platform_revision: str | None
    active_tenants: int
    current_tenants: int
    missing_tenants: int
    outdated_tenants: int


class MigrationHealthRepository(Protocol):
    async def fetch_snapshot(self) -> MigrationHealthSnapshot: ...


def build_migration_health_statement():
    """Aggregate the authoritative migration ledger without tenant/schema identifiers.

    The ledger advances only after successful migrations. This intentionally avoids
    O(N) physical-schema queries; out-of-band schema tampering is not ledger evidence.
    """

    current = and_(
        TenantSchemaState.tenant_id.is_not(None),
        TenantSchemaState.status == "active",
        TenantSchemaState.revision == TEACHING_SCHEMA_REVISION,
    )
    outdated = and_(
        TenantSchemaState.tenant_id.is_not(None),
        or_(
            TenantSchemaState.status != "active",
            TenantSchemaState.revision.is_(None),
            TenantSchemaState.revision != TEACHING_SCHEMA_REVISION,
        ),
    )
    return (
        select(
            literal_column("(SELECT version_num FROM platform.alembic_version)").label(
                "platform_revision"
            ),
            func.count(Tenant.id).label("active_tenants"),
            func.count().filter(current).label("current_tenants"),
            func.count().filter(TenantSchemaState.tenant_id.is_(None)).label("missing_tenants"),
            func.count().filter(outdated).label("outdated_tenants"),
        )
        .select_from(Tenant)
        .outerjoin(TenantSchemaState, TenantSchemaState.tenant_id == Tenant.id)
        .where(Tenant.status == "active")
    )


class SqlAlchemyMigrationHealthRepository:
    """Read platform and active-tenant migration evidence from one DB domain."""

    def __init__(self, session_factory: SessionFactory = platform_session) -> None:
        self._session_factory = session_factory

    async def fetch_snapshot(self) -> MigrationHealthSnapshot:
        async with self._session_factory() as session:
            row = (await session.execute(build_migration_health_statement())).one()
        return MigrationHealthSnapshot(
            platform_revision=(
                str(row.platform_revision) if row.platform_revision is not None else None
            ),
            active_tenants=int(row.active_tenants),
            current_tenants=int(row.current_tenants),
            missing_tenants=int(row.missing_tenants),
            outdated_tenants=int(row.outdated_tenants),
        )


class MigrationHealthProbe:
    """Require the platform and every active tenant to be at the packaged head."""

    def __init__(self, repository: MigrationHealthRepository) -> None:
        self._repository = repository

    async def __call__(self) -> None:
        snapshot = await self._repository.fetch_snapshot()
        if snapshot.platform_revision is None:
            raise HealthProbeFailure("platform_revision_missing")
        if snapshot.platform_revision != TEACHING_SCHEMA_REVISION:
            raise HealthProbeFailure("platform_revision_mismatch")
        if snapshot.missing_tenants:
            raise HealthProbeFailure("tenant_revision_missing")
        if snapshot.outdated_tenants or snapshot.current_tenants != snapshot.active_tenants:
            raise HealthProbeFailure("tenant_revision_outdated")


class StorageCredentialResolver(Protocol):
    def resolve(
        self,
        record: TenantStorageCredentialRecord,
        *,
        tenant_id: str,
    ) -> ResolvedStorageCredentials: ...


class S3HealthCheck(Protocol):
    async def __call__(
        self,
        *,
        tenant_id: str,
        endpoint: str,
        bucket: str,
        region: str,
        credentials: ResolvedStorageCredentials,
    ) -> None: ...


class ObjectStoreHealthProbe:
    """Probe configured object storage without creating or deleting objects."""

    def __init__(
        self,
        settings: PlatformSettings,
        *,
        local_root: Any | None = None,
        s3_inventory_repository: StorageHealthInventoryRepository | None = None,
        s3_credential_resolver: StorageCredentialResolver | None = None,
        s3_health_check: S3HealthCheck = check_s3_object_store_health,
        s3_concurrency: int = OBJECT_STORE_HEALTH_PROBE_CONCURRENCY,
    ) -> None:
        if (
            isinstance(s3_concurrency, bool)
            or not isinstance(s3_concurrency, int)
            or s3_concurrency < 1
        ):
            raise ValueError("object-store health concurrency must be positive")
        self._settings = settings
        if local_root is None:
            from deeptutor.runtime.home import get_runtime_data_root

            local_root = get_runtime_data_root() / "teaching" / "object-store"
        self._local_root = local_root
        self._s3_inventory_repository = s3_inventory_repository
        self._s3_credential_resolver = s3_credential_resolver
        self._s3_health_check = s3_health_check
        self._s3_concurrency = s3_concurrency

    async def _resolve_credentials(
        self,
        record: TenantStorageCredentialRecord,
    ) -> ResolvedStorageCredentials:
        resolver = self._s3_credential_resolver
        if resolver is None:
            raise ObjectStoreError("S3 health credential resolver is unavailable")
        return await run_s3_health_sync(
            resolver.resolve,
            record,
            tenant_id=record.tenant_id,
        )

    async def _probe_s3(self) -> None:
        repository = self._s3_inventory_repository
        if repository is None:
            raise ObjectStoreError("S3 health inventory is unavailable")
        inventory = await repository.fetch_health_inventory()
        counts = (inventory.active_tenants, inventory.unavailable_tenants)
        tenant_ids = [record.tenant_id for record in inventory.credentials]
        if (
            any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0
                for count in counts
            )
            or inventory.active_tenants
            != len(inventory.credentials) + inventory.unavailable_tenants
            or len(set(tenant_ids)) != len(tenant_ids)
            or any(not isinstance(tenant_id, str) or not tenant_id for tenant_id in tenant_ids)
        ):
            raise ObjectStoreError("S3 health credential inventory is unavailable")
        if inventory.active_tenants == 0:
            return
        if (
            inventory.unavailable_tenants
            or len(inventory.credentials) != inventory.active_tenants
            or len({record.tenant_id for record in inventory.credentials})
            != inventory.active_tenants
        ):
            raise ObjectStoreError("S3 health credential inventory is unavailable")
        endpoint = self._settings.object_store_endpoint
        if endpoint is None:
            raise ObjectStoreError("S3 health endpoint is unavailable")
        try:
            endpoint = validate_object_store_endpoint(endpoint)
        except ValueError:
            raise ObjectStoreError("S3 health endpoint is unavailable") from None
        records = iter(inventory.credentials)

        async def worker() -> None:
            for record in records:
                credentials = await self._resolve_credentials(record)
                await self._s3_health_check(
                    tenant_id=record.tenant_id,
                    endpoint=endpoint,
                    bucket=self._settings.object_store_bucket,
                    region=self._settings.object_store_region,
                    credentials=credentials,
                )

        tasks = [
            asyncio.create_task(
                worker(),
                name="object-store-tenant-health",
            )
            for _ in range(min(self._s3_concurrency, len(inventory.credentials)))
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def __call__(self) -> None:
        try:
            if self._settings.object_store_mode == "local":
                await LocalClassroomArtifactStore.health_check_root(self._local_root)
            else:
                await self._probe_s3()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise HealthProbeFailure("object_store_unavailable") from None


class DataPlaneHealthRepository(Protocol):
    async def resolve_shared_health_route(self) -> DataPlaneRouteRecord | None: ...

    async def resolve_dedicated_health_inventory(
        self,
    ) -> DedicatedDataPlaneHealthInventory: ...


class CompatibleOpenMAICClient(Protocol):
    async def assert_compatible(self) -> object: ...


class OpenMAICHealthClientFactory(Protocol):
    async def create(self, route: DataPlaneRouteRecord) -> CompatibleOpenMAICClient: ...


class OpenMAICDataPlaneHealthProbes:
    """Probe shared and dedicated OpenMAIC routes without exposing identities."""

    def __init__(
        self,
        *,
        repository: DataPlaneHealthRepository,
        client_factory: OpenMAICHealthClientFactory,
        dedicated_concurrency: int = DEDICATED_HEALTH_PROBE_CONCURRENCY,
    ) -> None:
        if (
            isinstance(dedicated_concurrency, bool)
            or not isinstance(dedicated_concurrency, int)
            or dedicated_concurrency < 1
        ):
            raise ValueError("dedicated health concurrency must be positive")
        self._repository = repository
        self._client_factory = client_factory
        self._dedicated_concurrency = dedicated_concurrency

    async def _route_reason(self, route: DataPlaneRouteRecord) -> str | None:
        if route.status != "active" or route.mode not in {"shared", "dedicated"}:
            return "route_unavailable"
        try:
            client = await self._client_factory.create(route)
            await client.assert_compatible()
        except asyncio.CancelledError:
            raise
        except IncompatibleOpenMAIC:
            return "contract_mismatch"
        except Exception:
            return "route_unavailable"
        return None

    async def probe_shared(self) -> None:
        route = await self._repository.resolve_shared_health_route()
        if route is None or route.mode != "shared":
            raise HealthProbeFailure("route_unavailable")
        reason = await self._route_reason(route)
        if reason is not None:
            raise HealthProbeFailure(reason)

    async def probe_dedicated(self) -> None:
        inventory = await self._repository.resolve_dedicated_health_inventory()
        counts = (inventory.active_tenants, inventory.unavailable_tenants)
        route_ids = [route.route_id for route in inventory.routes]
        tenant_ids = [route.tenant_id for route in inventory.routes]
        if (
            any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0
                for count in counts
            )
            or inventory.active_tenants != len(inventory.routes) + inventory.unavailable_tenants
            or len(set(route_ids)) != len(route_ids)
            or any(not isinstance(route_id, str) or not route_id for route_id in route_ids)
            or len(set(tenant_ids)) != len(tenant_ids)
            or any(not isinstance(tenant_id, str) or not tenant_id for tenant_id in tenant_ids)
            or any(
                route.mode != "dedicated"
                or route.status != "active"
                or route.tenant_id != route.owner_key
                for route in inventory.routes
            )
        ):
            raise HealthProbeFailure("route_unavailable")
        if inventory.active_tenants == 0:
            return
        if inventory.unavailable_tenants or len(inventory.routes) != inventory.active_tenants:
            raise HealthProbeFailure("route_unavailable")
        routes = iter(inventory.routes)
        reasons: list[str | None] = []

        async def worker() -> None:
            for route in routes:
                reasons.append(await self._route_reason(route))

        tasks = [
            asyncio.create_task(
                worker(),
                name="dedicated-data-plane-health",
            )
            for _ in range(min(self._dedicated_concurrency, len(inventory.routes)))
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if "contract_mismatch" in reasons:
            raise HealthProbeFailure("contract_mismatch")
        if any(reason is not None for reason in reasons):
            raise HealthProbeFailure("route_unavailable")


class RuntimeOpenMAICHealthClientFactory:
    """Build credential-free clients for the private immutable health route."""

    def __init__(
        self,
        settings: PlatformSettings,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._http_client = http_client

    async def create(self, route: DataPlaneRouteRecord) -> OpenMAICContractHealthClient:
        if not self._settings.enabled or route.status != "active":
            raise RuntimeError("OpenMAIC health route is unavailable")
        tenant_id = route.tenant_id if route.mode == "dedicated" else "shared-health"
        expected_owner = tenant_id if route.mode == "dedicated" else "shared"
        if not tenant_id or route.owner_key != expected_owner:
            raise RuntimeError("OpenMAIC health route is unavailable")
        return OpenMAICContractHealthClient(
            self._http_client,
            base_url=route.base_url,
            timeouts=ClientTimeouts(
                connect=OPENMAIC_HEALTH_CONNECT_TIMEOUT_SECONDS,
                read=OPENMAIC_HEALTH_READ_TIMEOUT_SECONDS,
                total=OPENMAIC_HEALTH_CONNECT_TIMEOUT_SECONDS
                + OPENMAIC_HEALTH_READ_TIMEOUT_SECONDS,
            ),
        )


class RenderHealthProbe:
    """Check one trusted shared-render URL without redirects or request input."""

    def __init__(self, *, health_url: str, http_client: httpx.AsyncClient) -> None:
        self._health_url = validate_render_health_url(health_url)
        self._http_client = http_client

    async def __call__(self) -> None:
        try:
            with redact_health_transport_logs():
                async with self._http_client.stream(
                    "GET",
                    self._health_url,
                    headers={"accept-encoding": "identity"},
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        connect=OPENMAIC_HEALTH_CONNECT_TIMEOUT_SECONDS,
                        read=OPENMAIC_HEALTH_READ_TIMEOUT_SECONDS,
                        write=OPENMAIC_HEALTH_READ_TIMEOUT_SECONDS,
                        pool=OPENMAIC_HEALTH_CONNECT_TIMEOUT_SECONDS,
                    ),
                ) as response:
                    status_code = response.status_code
        except asyncio.CancelledError:
            raise
        except Exception:
            raise HealthProbeFailure("render_unhealthy") from None
        if status_code != 200:
            raise HealthProbeFailure("render_unhealthy")


def _positive_timeout(value: float, field: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return float(value)


class ActiveHealthProbeService:
    """Coordinate concurrent probes with bounded async request waits."""

    def __init__(
        self,
        probes: Mapping[str, HealthProbe],
        *,
        probe_timeout_seconds: float = ACTIVE_PROBE_TIMEOUT_SECONDS,
        request_timeout_seconds: float = ACTIVE_PROBE_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if set(probes) != set(ACTIVE_PROBE_COMPONENTS):
            raise ValueError("active health probes must cover every dependency")
        self._probes = dict(probes)
        self._probe_timeout_seconds = _positive_timeout(
            probe_timeout_seconds,
            "probe timeout",
        )
        self._request_timeout_seconds = _positive_timeout(
            request_timeout_seconds,
            "request timeout",
        )

    async def _run_one(self, probe: HealthProbe) -> ActiveProbeResult:
        try:
            async with asyncio.timeout(self._probe_timeout_seconds):
                await probe()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return ActiveProbeResult(status="unhealthy", reason="probe_timeout")
        except HealthProbeFailure as exc:
            return ActiveProbeResult(status="unhealthy", reason=exc.reason)
        except Exception:
            return ActiveProbeResult(status="unhealthy", reason="probe_failed")
        return ActiveProbeResult(status="healthy")

    async def probe(self) -> dict[str, ActiveProbeResult]:
        tasks = {
            component: asyncio.create_task(
                self._run_one(probe),
                name=f"teaching-health-probe:{component}",
            )
            for component, probe in self._probes.items()
        }
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                values = await asyncio.gather(*tasks.values())
        except asyncio.CancelledError:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            raise
        except TimeoutError:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            return {
                component: (
                    task.result()
                    if task.done() and not task.cancelled() and task.exception() is None
                    else ActiveProbeResult(status="unhealthy", reason="request_timeout")
                )
                for component, task in tasks.items()
            }
        return dict(zip(tasks, values, strict=True))


__all__ = [
    "ACTIVE_PROBE_COMPONENTS",
    "ACTIVE_PROBE_REQUEST_TIMEOUT_SECONDS",
    "ACTIVE_PROBE_TIMEOUT_SECONDS",
    "ActiveHealthProbeService",
    "ActiveProbeResult",
    "DatabaseHealthProbe",
    "HealthProbeFailure",
    "MigrationHealthProbe",
    "MigrationHealthSnapshot",
    "ObjectStoreHealthProbe",
    "OpenMAICDataPlaneHealthProbes",
    "RenderHealthProbe",
    "RuntimeOpenMAICHealthClientFactory",
    "SqlAlchemyMigrationHealthRepository",
    "TEACHING_SCHEMA_REVISION",
    "build_migration_health_statement",
]
