"""Validate a dedicated OpenMAIC data-plane boundary before persistence."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.models import (
    AuditLog,
    DataPlaneRoute,
    ProviderProfile,
    Tenant,
)
from deeptutor.teaching.openmaic.auth import read_service_secret
from deeptutor.teaching.openmaic.client import (
    EXPECTED_APP_VERSION,
    EXPECTED_UPSTREAM_COMMIT,
    REQUIRED_CAPABILITIES,
    REQUIRED_EXPORT_FORMATS,
    SUPPORTED_CONTRACT_VERSION,
    ClientTimeouts,
    OpenMAICClient,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _endpoint(value: str, field: str, *, allow_path: bool = False) -> str:
    parsed = urlsplit(value)
    path_parts = parsed.path.split("/")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and parsed.path not in {"", "/"})
        or "\\" in parsed.path
        or any(part in {".", ".."} for part in path_parts)
        or any(character in parsed.path for character in "\r\n\x00")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field} is invalid")
    return value.rstrip("/")


def provider_secret_reference_exists(
    secrets_root: Path,
    tenant_id: str,
    profile_id: str,
    secret_ref: str,
) -> bool:
    """Check one exact dedicated secret file without returning its contents."""

    try:
        safe_tenant = _identifier(tenant_id, "tenant_id")
        safe_profile = _identifier(profile_id, "provider_profile_id")
    except ValueError:
        return False
    expected = f"tenants/{safe_tenant}/providers/{safe_profile}"
    if secret_ref != expected:
        return False
    root = Path(secrets_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        return False
    try:
        root = root.resolve(strict=True)
        current = root
        for part in expected.split("/"):
            current = current / part
            if current.is_symlink():
                return False
        secret_file = current.resolve(strict=True)
        secret_file.relative_to(root)
        if secret_file.is_symlink() or not secret_file.is_file():
            return False
        value = secret_file.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError, ValueError):
        return False
    return bool(value) and "\x00" not in value


@dataclass(frozen=True, slots=True)
class DedicatedDataPlaneRegistration:
    tenant_id: str
    route_id: str
    base_url: str
    worker_pool: str
    queue_name: str
    provider_profile_id: str
    provider_type: str
    model_name: str
    provider_api_base_url: str
    secret_ref: str

    def __post_init__(self) -> None:
        for field in (
            "tenant_id",
            "route_id",
            "worker_pool",
            "queue_name",
            "provider_profile_id",
            "provider_type",
            "model_name",
        ):
            _identifier(getattr(self, field), field)
        _endpoint(self.base_url, "base_url")
        _endpoint(
            self.provider_api_base_url,
            "provider_api_base_url",
            allow_path=True,
        )
        expected = f"tenants/{self.tenant_id}/providers/{self.provider_profile_id}"
        if self.secret_ref != expected:
            raise ValueError("secret_ref is outside the dedicated tenant boundary")


@dataclass(frozen=True, slots=True)
class VerifiedDataPlaneHealth:
    service: str
    upstream_commit: str
    app_version: str
    contract_versions: tuple[str, ...]
    capabilities: tuple[str, ...]
    export_formats: tuple[str, ...]


def _profile_matches(
    profile: ProviderProfile,
    registration: DedicatedDataPlaneRegistration,
) -> bool:
    return (
        profile.id == registration.provider_profile_id
        and profile.scope == "dedicated"
        and profile.tenant_id == registration.tenant_id
        and profile.owner_key == registration.tenant_id
        and profile.provider_type == registration.provider_type
        and profile.model_name == registration.model_name
        and profile.api_base_url == registration.provider_api_base_url
        and profile.secret_ref == registration.secret_ref
        and profile.status == "active"
    )


def _route_matches(
    route: DataPlaneRoute,
    registration: DedicatedDataPlaneRegistration,
) -> bool:
    return (
        route.id == registration.route_id
        and route.tenant_id == registration.tenant_id
        and route.owner_key == registration.tenant_id
        and route.mode == "dedicated"
        and route.base_url == registration.base_url
        and route.worker_pool == registration.worker_pool
        and route.queue_name == registration.queue_name
        and route.provider_profile_id == registration.provider_profile_id
        and route.status == "active"
    )


class SqlAlchemyDedicatedDataPlaneWriter:
    """Explicit-engine writer for one dedicated data-plane registration."""

    def __init__(self, database_engine: AsyncEngine) -> None:
        self._session_factory = async_sessionmaker(
            database_engine,
            expire_on_commit=False,
        )

    async def require_active_tenant(self, tenant_id: str) -> None:
        async with self._session_factory() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None or tenant.status != "active":
            raise ValueError("target tenant is not active")

    async def persist(self, registration: DedicatedDataPlaneRegistration) -> None:
        """Atomically install one verified dedicated route in the control schema."""

        async with self._session_factory() as session:
            async with session.begin():
                tenant = await session.scalar(
                    select(Tenant)
                    .where(Tenant.id == registration.tenant_id)
                    .with_for_update(of=Tenant)
                )
                if tenant is None or tenant.status != "active":
                    raise ValueError("target tenant is not active")
                route = await session.scalar(
                    select(DataPlaneRoute)
                    .where(
                        or_(
                            DataPlaneRoute.id == registration.route_id,
                            DataPlaneRoute.tenant_id == registration.tenant_id,
                            DataPlaneRoute.worker_pool == registration.worker_pool,
                            DataPlaneRoute.queue_name == registration.queue_name,
                        )
                    )
                    .with_for_update(of=DataPlaneRoute)
                )
                profile = await session.scalar(
                    select(ProviderProfile)
                    .where(
                        or_(
                            ProviderProfile.id == registration.provider_profile_id,
                            ProviderProfile.secret_ref == registration.secret_ref,
                        )
                    )
                    .with_for_update(of=ProviderProfile)
                )
                if route is not None or profile is not None:
                    if (
                        route is not None
                        and profile is not None
                        and tenant.data_plane_mode == "dedicated"
                        and _route_matches(route, registration)
                        and _profile_matches(profile, registration)
                    ):
                        route.health_status = "healthy"
                        route.health_checked_at = datetime.now(timezone.utc)
                        return
                    raise ValueError("dedicated data-plane registration conflicts")

                now = datetime.now(timezone.utc)
                profile = ProviderProfile(
                    id=registration.provider_profile_id,
                    scope="dedicated",
                    tenant_id=registration.tenant_id,
                    owner_key=registration.tenant_id,
                    provider_type=registration.provider_type,
                    model_name=registration.model_name,
                    api_base_url=registration.provider_api_base_url,
                    secret_ref=registration.secret_ref,
                    status="active",
                )
                session.add(profile)
                await session.flush()
                session.add(
                    DataPlaneRoute(
                        id=registration.route_id,
                        tenant_id=registration.tenant_id,
                        owner_key=registration.tenant_id,
                        mode="dedicated",
                        base_url=registration.base_url,
                        worker_pool=registration.worker_pool,
                        queue_name=registration.queue_name,
                        provider_profile_id=registration.provider_profile_id,
                        status="active",
                        health_status="healthy",
                        health_checked_at=now,
                    )
                )
                tenant.data_plane_mode = "dedicated"
                tenant.updated_at = now
                session.add(
                    AuditLog(
                        tenant_id=registration.tenant_id,
                        actor_id=None,
                        action="teaching.data_plane.registered",
                        resource_type="data_plane_route:dedicated",
                        resource_id=(f"{registration.route_id}/{registration.provider_profile_id}"),
                    )
                )


async def register_dedicated_data_plane(
    registration: DedicatedDataPlaneRegistration,
    *,
    verify_tenant: Callable[[str], Awaitable[None]],
    verify_health: Callable[
        [DedicatedDataPlaneRegistration],
        Awaitable[VerifiedDataPlaneHealth],
    ],
    verify_secret: Callable[[str, str, str], bool],
    persist: Callable[[DedicatedDataPlaneRegistration], Awaitable[None]],
) -> None:
    await verify_tenant(registration.tenant_id)
    if not verify_secret(
        registration.tenant_id,
        registration.provider_profile_id,
        registration.secret_ref,
    ):
        raise ValueError("dedicated Provider secret binding is invalid")

    health = await verify_health(registration)
    if (
        health.service != "openmaic"
        or health.upstream_commit != EXPECTED_UPSTREAM_COMMIT
        or health.app_version != EXPECTED_APP_VERSION
        or SUPPORTED_CONTRACT_VERSION not in health.contract_versions
        or not REQUIRED_CAPABILITIES.issubset(health.capabilities)
        or not REQUIRED_EXPORT_FORMATS.issubset(health.export_formats)
    ):
        raise ValueError("dedicated data-plane contract is incompatible")

    await persist(registration)


async def _verify_runtime(
    registration: DedicatedDataPlaneRegistration,
    service_secret_file: Path,
) -> VerifiedDataPlaneHealth:
    service_secret = read_service_secret(service_secret_file)
    timeouts = ClientTimeouts(connect=5.0, read=20.0, total=30.0)
    async with httpx.AsyncClient(timeout=timeouts.httpx_timeout()) as http_client:
        client = OpenMAICClient(
            http_client,
            base_url=registration.base_url,
            tenant_id=registration.tenant_id,
            route_id=registration.route_id,
            service_secret=service_secret,
            timeouts=timeouts,
        )
        health = await client.health()
    return VerifiedDataPlaneHealth(
        service=health.service,
        upstream_commit=health.upstream_commit,
        app_version=health.app_version,
        contract_versions=health.contract_versions,
        capabilities=health.capabilities,
        export_formats=health.export_formats,
    )


async def _run(
    registration: DedicatedDataPlaneRegistration,
    *,
    config: Path,
    provider_secrets_root: Path,
    service_secret_file: Path,
) -> None:
    settings = load_platform_settings(config)
    if not settings.enabled or settings.database_url is None:
        raise ValueError("platform database is not configured")
    database_engine = create_async_engine(
        settings.database_url.get_secret_value(),
        poolclass=NullPool,
    )
    try:
        writer = SqlAlchemyDedicatedDataPlaneWriter(database_engine)
        await register_dedicated_data_plane(
            registration,
            verify_tenant=writer.require_active_tenant,
            verify_secret=lambda tenant_id, profile_id, secret_ref: (
                provider_secret_reference_exists(
                    provider_secrets_root,
                    tenant_id,
                    profile_id,
                    secret_ref,
                )
            ),
            verify_health=lambda value: _verify_runtime(value, service_secret_file),
            persist=writer.persist,
        )
    finally:
        await database_engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--provider-secrets-root", type=Path, required=True)
    parser.add_argument("--service-secret-file", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--worker-pool", required=True)
    parser.add_argument("--queue-name", required=True)
    parser.add_argument("--provider-profile-id", required=True)
    parser.add_argument("--provider-type", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--provider-api-base-url", required=True)
    return parser


def _registration_from_arguments(
    arguments: argparse.Namespace,
) -> DedicatedDataPlaneRegistration:
    secret_ref = f"tenants/{arguments.tenant_id}/providers/{arguments.provider_profile_id}"
    return DedicatedDataPlaneRegistration(
        tenant_id=arguments.tenant_id,
        route_id=arguments.route_id,
        base_url=arguments.base_url,
        worker_pool=arguments.worker_pool,
        queue_name=arguments.queue_name,
        provider_profile_id=arguments.provider_profile_id,
        provider_type=arguments.provider_type,
        model_name=arguments.model_name,
        provider_api_base_url=arguments.provider_api_base_url,
        secret_ref=secret_ref,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    registration = _registration_from_arguments(arguments)
    asyncio.run(
        _run(
            registration,
            config=arguments.config,
            provider_secrets_root=arguments.provider_secrets_root,
            service_secret_file=arguments.service_secret_file,
        )
    )
    print(
        "registered dedicated data plane "
        f"tenant={registration.tenant_id} "
        f"route={registration.route_id} "
        f"profile={registration.provider_profile_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
