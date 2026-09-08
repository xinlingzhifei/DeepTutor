"""Verify and register the canonical shared OpenMAIC data plane."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_, select

SCRIPTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_ROOT.parent
for import_root in (PROJECT_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from openmaic_smoke_contract import (  # noqa: E402
    MAX_OPENMAIC_SMOKE_REPORT_BYTES,
    current_openmaic_observer_candidate_binding,
    validate_openmaic_shared_ingress_observer_bound_context,
    verify_openmaic_shared_ingress_observer_attestation,
)
from render_platform_compose import validated_candidate_artifact_lease  # noqa: E402

from deeptutor.services.config import load_platform_settings  # noqa: E402
from deeptutor.teaching.database import dispose_platform_engine, platform_session  # noqa: E402
from deeptutor.teaching.models import DataPlaneRoute, ProviderProfile  # noqa: E402
from deeptutor.teaching.openmaic.auth import read_service_secret  # noqa: E402
from deeptutor.teaching.openmaic.client import (  # noqa: E402
    EXPECTED_APP_VERSION,
    EXPECTED_UPSTREAM_COMMIT,
    REQUIRED_CAPABILITIES,
    REQUIRED_EXPORT_FORMATS,
    SUPPORTED_CONTRACT_VERSION,
    OpenMAICClient,
)
from deeptutor.teaching.openmaic.provider_config import (  # noqa: E402
    PROVIDER_IDS,
    valid_provider_model_name,
    validate_provider_endpoint,
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
    provider_api_base_url: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "route_id",
            "worker_pool",
            "queue_name",
            "provider_profile_id",
            "provider_type",
        ):
            if _IDENTIFIER.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"{name} is invalid")
        if not valid_provider_model_name(self.model_name):
            raise ValueError("model_name is invalid")
        if self.provider_api_base_url is not None:
            validate_provider_endpoint(self.provider_api_base_url)
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
        and profile.api_base_url == registration.provider_api_base_url
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
    *,
    assert_candidate_unchanged: Callable[[], None],
) -> None:
    """Atomically install or refresh the canonical shared route."""

    async with platform_session() as session:
        async with session.begin():
            assert_candidate_unchanged()
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
            assert_candidate_unchanged()
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
            assert_candidate_unchanged()
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
                    assert_candidate_unchanged()
                    await session.flush()
                    assert_candidate_unchanged()
                    return
                raise ValueError("shared data-plane registration conflicts")

            profile = ProviderProfile(
                id=registration.provider_profile_id,
                scope="shared",
                tenant_id=None,
                owner_key="shared",
                provider_type=registration.provider_type,
                model_name=registration.model_name,
                api_base_url=registration.provider_api_base_url,
                secret_ref=registration.secret_ref,
                status="active",
            )
            session.add(profile)
            assert_candidate_unchanged()
            await session.flush()
            assert_candidate_unchanged()
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
            assert_candidate_unchanged()
            await session.flush()
            assert_candidate_unchanged()


async def register_shared_data_plane(
    registration: SharedDataPlaneRegistration,
    *,
    verify_health: Callable[
        [SharedDataPlaneRegistration],
        Awaitable[VerifiedDataPlaneHealth],
    ],
    persist: Callable[..., Awaitable[None]] | None = None,
    assert_candidate_unchanged: Callable[[], None],
) -> None:
    assert_candidate_unchanged()
    health = await verify_health(registration)
    assert_candidate_unchanged()
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
    assert_candidate_unchanged()
    await persist_registration(
        registration,
        assert_candidate_unchanged=assert_candidate_unchanged,
    )
    assert_candidate_unchanged()


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


def _read_required_binding_file(path: Path, *, maximum_bytes: int) -> bytes:
    supplied = Path(path)
    if not supplied.is_absolute():
        raise ValueError("shared data-plane observer binding path is invalid")
    try:
        body = supplied.read_bytes()
    except OSError as exc:
        raise ValueError("shared data-plane observer binding is unavailable") from exc
    if not body or len(body) > maximum_bytes:
        raise ValueError("shared data-plane observer binding is invalid")
    return body


def verify_shared_data_plane_observer_binding(
    registration: SharedDataPlaneRegistration,
    *,
    attestation_path: Path,
    signature_path: Path,
    public_key_path: Path,
    candidate_root: Path,
    release_run: Mapping[str, str],
    public_key_spki_sha256: str,
) -> dict[str, object]:
    """Verify the externally signed proxy binding before any runtime operation."""

    attestation = _read_required_binding_file(
        attestation_path,
        maximum_bytes=MAX_OPENMAIC_SMOKE_REPORT_BYTES,
    )
    signature = _read_required_binding_file(signature_path, maximum_bytes=512)
    trusted_now = datetime.now(timezone.utc)
    report = verify_openmaic_shared_ingress_observer_attestation(
        attestation,
        release_run=release_run,
        signature=signature,
        public_key_path=Path(public_key_path),
        candidate_root=Path(candidate_root),
        expected_public_key_spki_sha256=public_key_spki_sha256,
        trusted_now=trusted_now,
    )
    current_binding = current_openmaic_observer_candidate_binding(candidate_root)
    try:
        validate_openmaic_shared_ingress_observer_bound_context(
            report,
            expected_candidate_binding=current_binding,
            expected_public_key_spki_sha256=public_key_spki_sha256,
            trusted_now=trusted_now,
        )
    except ValueError as exc:
        raise ValueError("shared data-plane observer candidate binding is invalid") from exc
    observer = report.get("observer")
    if not isinstance(observer, dict) or observer.get("internalProxyUrl") != registration.base_url:
        raise ValueError("shared data-plane observer proxy binding is invalid")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://openmaic:3000")
    parser.add_argument("--provider-id", required=True, choices=sorted(PROVIDER_IDS))
    parser.add_argument("--provider-model-name", required=True)
    parser.add_argument("--provider-api-base-url", required=True)
    parser.add_argument("--observer-attestation", type=Path, required=True)
    parser.add_argument("--observer-attestation-signature", type=Path, required=True)
    parser.add_argument("--observer-attestation-public-key", type=Path, required=True)
    parser.add_argument(
        "--observer-attestation-public-key-spki-sha256",
        required=True,
    )
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--release-run-id", required=True)
    parser.add_argument("--environment-id", required=True)
    return parser


async def _run(
    registration: SharedDataPlaneRegistration,
    *,
    assert_candidate_unchanged: Callable[[], None],
) -> None:
    try:
        await register_shared_data_plane(
            registration,
            verify_health=_verify_runtime,
            assert_candidate_unchanged=assert_candidate_unchanged,
        )
    finally:
        await dispose_platform_engine()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    registration = SharedDataPlaneRegistration(
        base_url=arguments.base_url.rstrip("/"),
        provider_type=arguments.provider_id,
        model_name=arguments.provider_model_name,
        provider_api_base_url=arguments.provider_api_base_url,
    )
    candidate_root = Path(arguments.candidate_root).absolute()
    with validated_candidate_artifact_lease(candidate_root) as candidate_lease:
        candidate_lease.check_unchanged()
        verify_shared_data_plane_observer_binding(
            registration,
            attestation_path=arguments.observer_attestation,
            signature_path=arguments.observer_attestation_signature,
            public_key_path=arguments.observer_attestation_public_key,
            candidate_root=candidate_root,
            release_run={
                "runId": arguments.release_run_id,
                "environmentId": arguments.environment_id,
            },
            public_key_spki_sha256=(arguments.observer_attestation_public_key_spki_sha256),
        )
        candidate_lease.check_unchanged()
        asyncio.run(
            _run(
                registration,
                assert_candidate_unchanged=candidate_lease.check_unchanged,
            )
        )
        candidate_lease.check_unchanged()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
