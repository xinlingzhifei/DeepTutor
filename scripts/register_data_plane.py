"""Validate a dedicated OpenMAIC data-plane boundary before persistence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import urlsplit

from sqlalchemy import or_, select

from deeptutor.teaching.database import platform_session
from deeptutor.teaching.models import (
    AuditLog,
    DataPlaneRoute,
    ProviderProfile,
    Tenant,
)
from deeptutor.teaching.openmaic.client import (
    EXPECTED_APP_VERSION,
    EXPECTED_UPSTREAM_COMMIT,
    REQUIRED_CAPABILITIES,
    REQUIRED_EXPORT_FORMATS,
    SUPPORTED_CONTRACT_VERSION,
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


async def persist_dedicated_data_plane(
    registration: DedicatedDataPlaneRegistration,
) -> None:
    """Atomically install one verified dedicated route in the control schema."""

    async with platform_session() as session:
        async with session.begin():
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == registration.tenant_id).with_for_update(of=Tenant)
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
    verify_health: Callable[
        [DedicatedDataPlaneRegistration],
        Awaitable[VerifiedDataPlaneHealth],
    ],
    verify_secret: Callable[[str, str, str], bool],
    persist: Callable[[DedicatedDataPlaneRegistration], Awaitable[None]] | None = None,
) -> None:
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

    persist_registration = persist or persist_dedicated_data_plane
    await persist_registration(registration)
