"""Verify and register the canonical shared OpenMAIC data plane."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_, select

from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.database import dispose_platform_engine, platform_session
from deeptutor.teaching.models import DataPlaneRoute, ProviderProfile
from deeptutor.teaching.openmaic.auth import read_service_secret
from deeptutor.teaching.openmaic.client import (
    EXPECTED_APP_VERSION,
    EXPECTED_UPSTREAM_COMMIT,
    REQUIRED_CAPABILITIES,
    REQUIRED_EXPORT_FORMATS,
    SUPPORTED_CONTRACT_VERSION,
    OpenMAICClient,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class SharedDataPlaneRegistration:
    route_id: str = "shared-primary"
    base_url: str = "http://openmaic:3000"
    worker_pool: str = "shared-generation"
    queue_name: str = "openmaic.shared"
    provider_profile_id: str = "platform-default"
    provider_type: str = "openmaic-server-configured"
    model_name: str = "server-selected-model"
    secret_ref: str = "shared/providers/platform-default"

    def __post_init__(self) -> None:
        for name in (
            "route_id",
            "worker_pool",
            "queue_name",
            "provider_profile_id",
            "provider_type",
            "model_name",
        ):
            if _IDENTIFIER.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"{name} is invalid")
        endpoint = urlsplit(self.base_url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("base_url is invalid")
        expected_ref = f"shared/providers/{self.provider_profile_id}"
        if self.secret_ref != expected_ref:
            raise ValueError("secret_ref is outside the shared boundary")


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
    registration: SharedDataPlaneRegistration,
) -> bool:
    return (
        profile.id == registration.provider_profile_id
        and profile.scope == "shared"
        and profile.tenant_id is None
        and profile.owner_key == "shared"
        and profile.provider_type == registration.provider_type
        and profile.model_name == registration.model_name
        and profile.api_base_url is None
        and profile.secret_ref == registration.secret_ref
        and profile.status == "active"
    )


def _route_matches(
    route: DataPlaneRoute,
    registration: SharedDataPlaneRegistration,
) -> bool:
    return (
        route.id == registration.route_id
        and route.tenant_id is None
        and route.owner_key == "shared"
        and route.mode == "shared"
        and route.base_url == registration.base_url.rstrip("/")
        and route.worker_pool == registration.worker_pool
        and route.queue_name == registration.queue_name
        and route.provider_profile_id == registration.provider_profile_id
        and route.status == "active"
    )


async def persist_shared_data_plane(
    registration: SharedDataPlaneRegistration,
) -> None:
    """Atomically install or refresh the canonical shared route."""

    async with platform_session() as session:
        async with session.begin():
            route = await session.scalar(
                select(DataPlaneRoute)
                .where(
                    or_(
                        DataPlaneRoute.id == registration.route_id,
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
            now = datetime.now(timezone.utc)
            if route is not None or profile is not None:
                if (
                    route is not None
                    and profile is not None
                    and _route_matches(route, registration)
                    and _profile_matches(profile, registration)
                ):
                    route.health_status = "healthy"
                    route.health_checked_at = now
                    return
                raise ValueError("shared data-plane registration conflicts")

            profile = ProviderProfile(
                id=registration.provider_profile_id,
                scope="shared",
                tenant_id=None,
                owner_key="shared",
                provider_type=registration.provider_type,
                model_name=registration.model_name,
                api_base_url=None,
                secret_ref=registration.secret_ref,
                status="active",
            )
            session.add(profile)
            await session.flush()
            session.add(
                DataPlaneRoute(
                    id=registration.route_id,
                    tenant_id=None,
                    owner_key="shared",
                    mode="shared",
                    base_url=registration.base_url.rstrip("/"),
                    worker_pool=registration.worker_pool,
                    queue_name=registration.queue_name,
                    provider_profile_id=registration.provider_profile_id,
                    status="active",
                    health_status="healthy",
                    health_checked_at=now,
                )
            )


async def register_shared_data_plane(
    registration: SharedDataPlaneRegistration,
    *,
    verify_health: Callable[
        [SharedDataPlaneRegistration],
        Awaitable[VerifiedDataPlaneHealth],
    ],
    persist: Callable[[SharedDataPlaneRegistration], Awaitable[None]] | None = None,
) -> None:
    health = await verify_health(registration)
    if (
        health.service != "openmaic"
        or health.upstream_commit != EXPECTED_UPSTREAM_COMMIT
        or health.app_version != EXPECTED_APP_VERSION
        or SUPPORTED_CONTRACT_VERSION not in health.contract_versions
        or not REQUIRED_CAPABILITIES.issubset(health.capabilities)
        or not REQUIRED_EXPORT_FORMATS.issubset(health.export_formats)
    ):
        raise ValueError("shared data-plane contract is incompatible")
    persist_registration = persist or persist_shared_data_plane
    await persist_registration(registration)


async def _verify_runtime(
    registration: SharedDataPlaneRegistration,
) -> VerifiedDataPlaneHealth:
    settings = load_platform_settings()
    if settings.openmaic_service_secret_file is None:
        raise ValueError("OpenMAIC service secret is unavailable")
    service_secret = read_service_secret(settings.openmaic_service_secret_file)
    async with httpx.AsyncClient(timeout=20.0) as http_client:
        client = OpenMAICClient(
            http_client,
            base_url=registration.base_url,
            tenant_id="shared-bootstrap",
            route_id=registration.route_id,
            service_secret=service_secret,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://openmaic:3000")
    return parser


async def _run(registration: SharedDataPlaneRegistration) -> None:
    try:
        await register_shared_data_plane(
            registration,
            verify_health=_verify_runtime,
        )
    finally:
        await dispose_platform_engine()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    asyncio.run(
        _run(
            SharedDataPlaneRegistration(base_url=arguments.base_url.rstrip("/")),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
