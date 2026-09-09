"""Fail-closed deployment preflight for the private teaching platform."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Protocol

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from docker_compose import (
    _OBSERVER_RUNTIME_ENVIRONMENT,
    _PROVIDER_RUNTIME_ENVIRONMENT,
    load_observer_release_admission,
    platform_compose_topology_arguments,
)
from gateway_public_contract import gateway_service_ports, parse_gateway_candidate_ports
from openmaic_observer_service import (
    OpenMAICObserverServiceError,
    parse_observer_auth_expiry,
    validate_observer_endpoints,
)
from platform_preflight_contract import (
    PHASES,
    canonical_candidate_network_report,
    failed_candidate_network_report,
    parse_candidate_network_report,
)
from render_platform_compose import (
    CandidateArtifactError,
    candidate_artifact_paths,
    load_image_lock,
    validated_candidate_artifact_lease,
)

from deeptutor.teaching.openmaic.provider_config import (
    PROVIDER_CONFIGURATION_DIRECTORY,
    ProviderConfiguration,
    ProviderConfigurationError,
    ProviderNotConfigured,
    provider_configuration_path,
    read_provider_configuration,
    validate_registered_provider_configuration,
)
from deeptutor.teaching.secret_permissions import secret_file_is_restricted

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ZERO_DIGEST = "sha256:" + "0" * 64
DEFAULT_REQUIRED_SECRETS = (
    "platform_database_password",
    "platform_database_app_password",
    "platform_database_migration_password",
    "minio_bootstrap_access_key",
    "minio_bootstrap_secret_key",
    "classroom_ticket_secret",
    "openmaic_service_secret",
)
OBSERVER_SECRET_FILES = {
    "openmaic_observer_auth_token": "observer-auth.token",
    "openmaic_observer_health_token": "candidate-health.token",
    "openmaic_observer_attestation_signing_key": "attestation-signing-key.pem",
}
_RELEASE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IMMUTABLE_IMAGE_NAME = re.compile(
    r"^(?=.{1,512}$)"
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?/)*"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
)
_MAX_OBSERVER_SECRET_BYTES = 1024 * 1024
_OBSERVER_CONTAINER_UID = 1000
_OBSERVER_CONTAINER_GID = 1000
OBSERVER_SECRET_PREFLIGHT_FIELD = "observerSecretPreflight"


def _dns_name_matches(pattern: str, hostname: str) -> bool:
    expected = pattern.rstrip(".").encode("idna").decode("ascii").lower()
    actual = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if expected.startswith("*.") and "*" not in expected[2:]:
        suffix = expected[2:]
        return actual.endswith(f".{suffix}") and actual.count(".") == suffix.count(".") + 1
    return actual == expected


@dataclass(frozen=True, slots=True)
class PreflightResult:
    errors: tuple[str, ...]
    observer_secret_preflight: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


class RuntimePreflightProbe(Protocol):
    async def inspect(
        self,
        *,
        settings: Any,
        secret_dir: Path,
        image_lock_path: Path,
        project_root: Path,
        candidate_root: Path,
        observer_admission: dict[str, object] | None = None,
    ) -> tuple[str, ...]: ...


async def run_runtime_preflight(
    *,
    settings: Any,
    secret_dir: Path,
    image_lock_path: Path,
    project_root: Path,
    candidate_root: Path,
    observer_admission: dict[str, object] | None = None,
    probe: RuntimePreflightProbe | None = None,
) -> tuple[str, ...]:
    runtime_probe = probe or DefaultRuntimePreflightProbe()
    errors = await runtime_probe.inspect(
        settings=settings,
        secret_dir=Path(secret_dir),
        image_lock_path=Path(image_lock_path),
        project_root=Path(project_root),
        candidate_root=Path(candidate_root),
        observer_admission=observer_admission,
    )
    return tuple(dict.fromkeys(errors))


@dataclass(frozen=True, slots=True)
class _ActiveTenant:
    tenant_id: str
    schema_name: str
    secret_ref: str | None


@dataclass(frozen=True, slots=True)
class _DatabaseInspection:
    errors: tuple[str, ...]
    active_tenants: tuple[_ActiveTenant, ...]
    shared_route: tuple[str, str] | None


async def _inspect_database_runtime(settings: Any) -> _DatabaseInspection:
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from deeptutor.teaching.models import (
        DataPlaneRoute,
        Tenant,
        TenantSchemaState,
        TenantStorageCredential,
    )
    from deeptutor.teaching.provisioning_worker import TENANT_SCHEMA_REVISION
    from deeptutor.teaching.schema_names import tenant_schema_name

    database_url = getattr(settings, "database_url", None)
    if not getattr(settings, "enabled", False) or database_url is None:
        return _DatabaseInspection(
            ("database connectivity", "database migrations"),
            (),
            None,
        )
    engine = create_async_engine(database_url.get_secret_value(), poolclass=NullPool)
    try:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return _DatabaseInspection(
                ("database connectivity", "database migrations"),
                (),
                None,
            )

        errors: list[str] = []
        active_tenants: list[_ActiveTenant] = []
        shared_route: tuple[str, str] | None = None
        try:
            async with engine.connect() as connection:
                platform_revision = await connection.scalar(
                    text("SELECT version_num FROM platform.alembic_version")
                )
                rows = (
                    await connection.execute(
                        select(
                            Tenant.id,
                            TenantSchemaState.schema_name,
                            TenantSchemaState.revision,
                            TenantStorageCredential.secret_ref,
                            TenantStorageCredential.status,
                        )
                        .join(
                            TenantSchemaState,
                            TenantSchemaState.tenant_id == Tenant.id,
                            isouter=True,
                        )
                        .join(
                            TenantStorageCredential,
                            TenantStorageCredential.tenant_id == Tenant.id,
                            isouter=True,
                        )
                        .where(Tenant.status == "active")
                        .order_by(Tenant.id)
                    )
                ).all()
                route = (
                    await connection.execute(
                        select(DataPlaneRoute.id, DataPlaneRoute.base_url).where(
                            DataPlaneRoute.mode == "shared",
                            DataPlaneRoute.tenant_id.is_(None),
                            DataPlaneRoute.owner_key == "shared",
                            DataPlaneRoute.status == "active",
                        )
                    )
                ).one_or_none()
                if route is not None:
                    shared_route = (route.id, route.base_url)
                if platform_revision != TENANT_SCHEMA_REVISION:
                    errors.append("database migrations")
                for tenant_id, schema_name, revision, secret_ref, credential_status in rows:
                    expected_schema = tenant_schema_name(tenant_id)
                    if schema_name != expected_schema or revision != TENANT_SCHEMA_REVISION:
                        errors.append("database migrations")
                        continue
                    tenant_revision = await connection.scalar(
                        text(f'SELECT version_num FROM "{expected_schema}".alembic_version')
                    )
                    if tenant_revision != TENANT_SCHEMA_REVISION:
                        errors.append("database migrations")
                    active_tenants.append(
                        _ActiveTenant(
                            tenant_id=tenant_id,
                            schema_name=expected_schema,
                            secret_ref=(secret_ref if credential_status == "active" else None),
                        )
                    )
        except Exception:
            errors.append("database migrations")
        return _DatabaseInspection(
            tuple(dict.fromkeys(errors)),
            tuple(active_tenants),
            shared_route,
        )
    finally:
        await engine.dispose()


def _root_minio_probe(settings: Any, secret_dir: Path) -> None:
    import io
    import secrets
    from urllib.parse import urlsplit

    from minio import Minio

    endpoint = urlsplit(settings.object_store_endpoint or "")
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.netloc
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("invalid object store endpoint")
    access_key = (secret_dir / "minio_bootstrap_access_key").read_text(encoding="utf-8").strip()
    secret_key = (secret_dir / "minio_bootstrap_secret_key").read_text(encoding="utf-8").strip()
    if not access_key or not secret_key:
        raise ValueError("invalid object store bootstrap credential")
    client = Minio(
        endpoint.netloc,
        access_key=access_key,
        secret_key=secret_key,
        secure=endpoint.scheme == "https",
        region=settings.object_store_region,
    )
    bucket = settings.object_store_bucket
    if not client.bucket_exists(bucket):
        raise ValueError("object store bucket is unavailable")
    key = f".preflight/{secrets.token_hex(16)}"
    response = None
    try:
        client.put_object(bucket, key, io.BytesIO(b"ok"), length=2)
        response = client.get_object(bucket, key)
        if response.read() != b"ok":
            raise ValueError("object store probe mismatch")
    finally:
        if response is not None:
            response.close()
            response.release_conn()
        client.remove_object(bucket, key)


async def _inspect_object_store_runtime(
    settings: Any,
    secret_dir: Path,
    active_tenants: Sequence[_ActiveTenant],
) -> tuple[str, ...]:
    from deeptutor.teaching.artifacts import tenant_artifact_prefix
    from deeptutor.teaching.minio_tenant_storage import (
        RuntimeMinioTenantStorageAdmin,
        TenantSecretStore,
    )
    from deeptutor.teaching.provisioning_worker import ProvisioningStepError

    if getattr(settings, "object_store_mode", None) != "s3":
        return ("object store bucket probe",)
    errors: list[str] = []
    try:
        await asyncio.to_thread(_root_minio_probe, settings, Path(secret_dir))
    except Exception:
        errors.append("object store bucket probe")
    credentials_root = getattr(settings, "object_store_tenant_credentials_dir", None)
    if credentials_root is None:
        return tuple(dict.fromkeys([*errors, "active tenant credential"]))
    try:
        credential_path = Path(credentials_root)
        if credential_path.is_symlink() or not credential_path.is_dir():
            raise ValueError("tenant credential root is unavailable")
        store = TenantSecretStore(credential_path)
        admin = RuntimeMinioTenantStorageAdmin(
            settings=settings,
            bootstrap_access_key_file=Path(secret_dir) / "minio_bootstrap_access_key",
            bootstrap_secret_key_file=Path(secret_dir) / "minio_bootstrap_secret_key",
        )
    except Exception:
        return tuple(dict.fromkeys([*errors, "active tenant credential"]))
    for tenant in active_tenants:
        if tenant.secret_ref is None:
            errors.append("active tenant credential")
            continue
        try:
            credentials = store.load(tenant.secret_ref, tenant_id=tenant.tenant_id)
        except Exception:
            errors.append("active tenant credential")
            continue
        try:
            await admin.verify(
                credentials=credentials,
                own_prefix=tenant_artifact_prefix(tenant.tenant_id),
                denied_prefix="tenants/__cross_tenant_probe__/",
            )
        except ProvisioningStepError as exc:
            if exc.code in {
                "cross_prefix_access_allowed",
                "cross_prefix_probe_inconclusive",
            }:
                errors.append("tenant cross-prefix denial probe")
            else:
                errors.append("tenant own-prefix probe")
        except Exception:
            errors.append("tenant own-prefix probe")
    return tuple(dict.fromkeys(errors))


async def _inspect_openmaic_runtime(
    shared_route: tuple[str, str] | None,
    secret_dir: Path,
) -> tuple[str, ...]:
    if shared_route is None:
        return ("OpenMAIC health and contract 1.0",)
    try:
        import httpx

        from deeptutor.teaching.openmaic.auth import read_service_secret
        from deeptutor.teaching.openmaic.client import OpenMAICClient

        route_id, base_url = shared_route
        service_secret = read_service_secret(
            (Path(secret_dir) / "openmaic_service_secret").resolve(strict=True)
        )
        async with httpx.AsyncClient() as http_client:
            client = OpenMAICClient(
                http_client,
                base_url=base_url,
                tenant_id="preflight",
                route_id=route_id,
                service_secret=service_secret,
            )
            await client.assert_compatible()
    except Exception:
        return ("OpenMAIC health and contract 1.0",)
    return ()


async def inspect_candidate_network_phase(
    *,
    phase: str,
    settings: Any,
    secret_dir: Path,
) -> dict[str, Any]:
    """Inspect one candidate-network dependency boundary without Docker access."""

    if phase not in {"database-object-store", "openmaic"}:
        raise ValueError("unsupported candidate-network preflight phase")

    database = await _inspect_database_runtime(settings)
    if phase == "openmaic":
        errors = tuple(
            dict.fromkeys(
                (
                    *database.errors,
                    *await _inspect_openmaic_runtime(database.shared_route, Path(secret_dir)),
                )
            )
        )
        checks = {"openmaicContractCompatible": not errors}
    else:
        object_store_errors = await _inspect_object_store_runtime(
            settings,
            Path(secret_dir),
            database.active_tenants,
        )
        inventory_errors = () if database.active_tenants else ("active tenant inventory",)
        errors = tuple(dict.fromkeys((*database.errors, *inventory_errors, *object_store_errors)))
        blocked = set(errors)
        database_blockers = {"database connectivity", "database migrations"}
        credential_blockers = database_blockers | {
            "active tenant inventory",
            "object store bucket probe",
            "active tenant credential",
        }
        own_prefix_blockers = credential_blockers | {"tenant own-prefix probe"}
        cross_prefix_blockers = own_prefix_blockers | {"tenant cross-prefix denial probe"}
        checks = {
            "activeTenantCredentialsValid": blocked.isdisjoint(credential_blockers),
            "databaseConnected": "database connectivity" not in blocked,
            "objectStoreRoundTrip": "object store bucket probe" not in blocked,
            "revisionsMatch": blocked.isdisjoint(database_blockers),
            "tenantCrossPrefixDenied": blocked.isdisjoint(cross_prefix_blockers),
            "tenantOwnPrefixAccessible": blocked.isdisjoint(own_prefix_blockers),
        }

    return {
        "schemaVersion": 1,
        "producer": "platform-preflight",
        "phase": phase,
        "checks": checks,
        "errors": list(errors),
    }


async def _inspect_registered_provider_runtime(
    settings: Any, configuration: ProviderConfiguration, active_tenants: Sequence[_ActiveTenant]
) -> tuple[str, ...]:
    """Read the configured database; reuse the repository's complete active bindings."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from deeptutor.teaching.models import ProviderProfile
    from deeptutor.teaching.repositories.data_planes import (
        SqlAlchemyDataPlaneRepository,
        _profile_record,
    )

    engine = None
    try:
        if not settings.enabled or settings.database_url is None:
            raise ProviderConfigurationError()
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = SqlAlchemyDataPlaneRepository(session_factory=factory)
        async with factory() as session:
            profile = await session.scalar(
                select(ProviderProfile).where(ProviderProfile.id == configuration.profile_id)
            )
        if profile is None:
            raise ProviderConfigurationError()
        validate_registered_provider_configuration(configuration, _profile_record(profile))
        route = await repository.resolve_shared_health_route()
        if (
            route is None
            or route.provider_profile_id != configuration.profile_id
            or route.health_status != "healthy"
        ):
            raise ProviderConfigurationError()
        for tenant in active_tenants:
            resolution = await repository.resolve(tenant.tenant_id)
            if resolution is None:
                raise ProviderConfigurationError()
            if resolution.tenant_mode != "shared":
                continue  # Dedicated processes have their own canonical configuration and registration.
            selection = await repository.resolve_worker_selection(
                tenant_id=tenant.tenant_id,
                route_id=route.route_id,
                provider_profile_id=configuration.profile_id,
                worker_pool_ref=route.worker_pool,
                queue_ref=route.queue_name,
            )
            if selection is None or await repository.resolve_bound_route(selection) is None:
                raise ProviderConfigurationError()
            bound_profile = await repository.resolve_bound_profile(selection)
            if bound_profile is None:
                raise ProviderConfigurationError()
            validate_registered_provider_configuration(configuration, bound_profile)
    except Exception:
        return ("OpenMAIC registered provider binding",)
    finally:
        if engine is not None:
            await engine.dispose()
    return ()


class DefaultRuntimePreflightProbe:
    async def inspect(
        self,
        *,
        settings: Any,
        secret_dir: Path,
        image_lock_path: Path,
        project_root: Path,
        candidate_root: Path,
        observer_admission: dict[str, object] | None = None,
    ) -> tuple[str, ...]:
        try:
            _validated_observer_environment(observer_admission)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return ("observer release manifest unavailable or changed",)
        provider_root = (
            project_root / "data" / "system" / "secrets" / PROVIDER_CONFIGURATION_DIRECTORY
        )
        try:
            provider = read_provider_configuration(
                provider_root, scope="shared", tenant_id=None, profile_id="platform-default"
            )
        except ProviderNotConfigured:
            provider = None
        except ProviderConfigurationError:
            return ("OpenMAIC provider configuration",)
        database = await _inspect_database_runtime(settings)
        errors = list(database.errors)
        if provider is not None:
            errors.extend(
                await _inspect_registered_provider_runtime(
                    settings, provider, database.active_tenants
                )
            )
        errors.extend(
            await _inspect_object_store_runtime(
                settings,
                secret_dir,
                database.active_tenants,
            )
        )
        errors.extend(await _inspect_openmaic_runtime(database.shared_route, secret_dir))
        errors.extend(
            _inspect_compose_runtime(
                project_root,
                candidate_root=candidate_root,
                image_lock_path=image_lock_path,
                observer_admission=observer_admission,
                provider_configuration=provider,
                provider_secrets_root=provider_root,
            )
        )
        return tuple(dict.fromkeys(errors))


def _validated_observer_environment(admission: dict[str, object] | None) -> dict[str, str]:
    if admission is None:
        raise ValueError("observer release manifest required")
    environment = admission.get("environment")
    revalidate = admission.get("_revalidate")
    if (
        not isinstance(environment, dict)
        or set(environment) != set(_OBSERVER_RUNTIME_ENVIRONMENT)
        or any(not isinstance(value, str) or not value for value in environment.values())
        or not callable(revalidate)
    ):
        raise ValueError("observer release admission is invalid")
    revalidate()
    return dict(environment)


def _inspect_observer_compose_environment(
    service: object, *, admitted_environment: dict[str, str]
) -> tuple[str, ...]:
    try:
        environment = service.get("environment") if isinstance(service, dict) else None
        if not isinstance(environment, dict):
            raise ValueError("observer environment missing")
        validate_observer_endpoints(
            environment_id=environment.get("YFEISTAI_ENVIRONMENT_ID", ""),
            observer_id=environment.get("YFEISTAI_SHARED_INGRESS_OBSERVER_ID", ""),
            release_run_id=environment.get("YFEISTAI_RELEASE_RUN_ID", ""),
            observer_url=environment.get("YFEISTAI_SHARED_INGRESS_OBSERVER_URL", ""),
            shared_ingress_control_url=environment.get("YFEISTAI_SHARED_INGRESS_CONTROL_URL", ""),
            candidate_health_url=environment.get(
                "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL", ""
            ),
            shared_ingress_upstream_url=environment.get("YFEISTAI_SHARED_INGRESS_UPSTREAM_URL", ""),
            internal_proxy_url=environment.get("YFEISTAI_SHARED_INGRESS_INTERNAL_PROXY_URL", ""),
        )
        parse_observer_auth_expiry(
            environment.get("YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT"), now=datetime.now(UTC)
        )
        if any(
            environment.get(name) != admitted_environment[name]
            for name in (
                "YFEISTAI_RELEASE_RUN_ID",
                "YFEISTAI_CANDIDATE_SHA256",
                "YFEISTAI_IMAGE_LOCK_SHA256",
                "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256",
                "YFEISTAI_ENVIRONMENT_ID",
                "YFEISTAI_SHARED_INGRESS_OBSERVER_ID",
                "YFEISTAI_SHARED_INGRESS_OBSERVER_URL",
                "YFEISTAI_SHARED_INGRESS_CONTROL_URL",
                "YFEISTAI_SHARED_INGRESS_UPSTREAM_URL",
                "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL",
                "YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT",
            )
        ):
            raise ValueError("observer release binding changed")
    except (OpenMAICObserverServiceError, KeyError, TypeError, ValueError):
        # Never expose supplied URLs (which may contain credentials) in diagnostics.
        return ("observer deployment configuration",)
    return ()


def _inspect_provider_compose_bindings(
    payload: dict[str, object], configuration: ProviderConfiguration | None, secrets_root: Path
) -> tuple[str, ...]:
    try:
        services = payload["services"]
        service = services["openmaic"]
        environment = service["environment"]
        if configuration is None or configuration.scope == "shared":
            if (
                environment.get("YFEISTAI_OPENMAIC_MODE") != "shared"
                or environment.get("YFEISTAI_OPENMAIC_PROVIDER_PROFILE_ID") != "platform-default"
                or environment.get("YFEISTAI_DATA_PLANE_ID", "")
                or environment.get("YFEISTAI_OPENMAIC_PROVIDER_CONFIG_FILE")
                != "/run/yfeistai-provider/platform-default"
            ):
                raise ValueError
            directory = secrets_root / "shared/providers"
            for name, target, read_only in (
                ("openmaic", "/run/yfeistai-provider", True),
                (
                    "deeptutor",
                    "/app/data/system/secrets/openmaic-providers/shared/providers",
                    False,
                ),
                (
                    "shared-data-plane-bootstrap",
                    "/run/yfeistai-provider-root/shared/providers",
                    True,
                ),
            ):
                mounts = [
                    m
                    for m in services[name].get("volumes", [])
                    if isinstance(m, dict) and m.get("target") == target
                ]
                if len(mounts) != 1:
                    raise ValueError
                mount = mounts[0]
                if (
                    mount.get("type") != "bind"
                    or Path(mount["source"]) != directory
                    or bool(mount.get("read_only", False)) != read_only
                ):
                    raise ValueError
            command = services["shared-data-plane-bootstrap"]["command"]
            if (
                command.count("--provider-config-root") != 1
                or command[command.index("--provider-config-root") + 1]
                != "/run/yfeistai-provider-root"
                or any(
                    flag in command
                    for flag in (
                        "--provider-id",
                        "--provider-model-name",
                        "--provider-api-base-url",
                    )
                )
            ):
                raise ValueError
            return ()
        if (
            environment.get("YFEISTAI_OPENMAIC_MODE") != configuration.scope
            or environment.get("YFEISTAI_OPENMAIC_PROVIDER_PROFILE_ID") != configuration.profile_id
            or environment.get("YFEISTAI_DATA_PLANE_ID", "") != (configuration.tenant_id or "")
        ):
            raise ValueError
        mounts = [
            entry
            for entry in service["secrets"]
            if isinstance(entry, dict) and entry.get("target") == "/app/server-providers.yml"
        ]
        if mounts != [
            {"source": "openmaic_provider_configuration", "target": "/app/server-providers.yml"}
        ]:
            raise ValueError
        expected_path = provider_configuration_path(
            secrets_root,
            scope=configuration.scope,
            tenant_id=configuration.tenant_id,
            profile_id=configuration.profile_id,
        )
        if Path(payload["secrets"]["openmaic_provider_configuration"]["file"]) != expected_path:
            raise ValueError
        if configuration.scope == "shared":
            command = services["shared-data-plane-bootstrap"]["command"]
            if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
                raise ValueError
            for flag, expected in (
                ("--provider-id", configuration.provider_id),
                ("--provider-model-name", configuration.model_name),
                ("--provider-api-base-url", configuration.base_url),
            ):
                if command.count(flag) != 1 or command[command.index(flag) + 1] != expected:
                    raise ValueError
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return ("OpenMAIC provider deployment configuration",)
    return ()


def _inspect_compose_runtime(
    project_root: Path,
    *,
    candidate_root: Path | None = None,
    image_lock_path: Path | None = None,
    observer_admission: dict[str, object] | None = None,
    provider_configuration: ProviderConfiguration | None = None,
    provider_secrets_root: Path | None = None,
    runner=subprocess.run,
) -> tuple[str, ...]:
    root = Path(project_root)
    artifact_paths = candidate_artifact_paths(candidate_root or root)
    effective_image_lock_path = (
        Path(image_lock_path)
        if image_lock_path is not None
        else artifact_paths.image_lock
        if candidate_root is not None
        else None
    )
    environment = os.environ.copy()
    for name in (
        "COMPOSE_FILE",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "YFEISTAI_CANDIDATE_ROOT",
        "YFEISTAI_OBSERVER_RELEASE_MANIFEST",
        *_OBSERVER_RUNTIME_ENVIRONMENT,
        *_PROVIDER_RUNTIME_ENVIRONMENT,
    ):
        environment.pop(name, None)
    try:
        environment.update(_validated_observer_environment(observer_admission))
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return ("observer release manifest unavailable or changed",)
    environment["YFEISTAI_CANDIDATE_ROOT"] = str(artifact_paths.root)
    if provider_configuration is not None:
        environment.update(
            {
                "YFEISTAI_OPENMAIC_PROVIDER_ID": provider_configuration.provider_id,
                "YFEISTAI_OPENMAIC_MODEL_NAME": provider_configuration.model_name,
                "YFEISTAI_OPENMAIC_PROVIDER_ENDPOINT": provider_configuration.base_url,
            }
        )
    errors: list[str] = []
    shared_gateway = False
    try:
        version_result = runner(
            ["docker", "compose", "version", "--short"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        version_match = re.search(r"\d+\.\d+\.\d+", version_result.stdout)
        if (
            version_result.returncode != 0
            or version_match is None
            or tuple(int(part) for part in version_match.group().split(".")) < (2, 24, 4)
        ):
            errors.append("Docker Compose version")
    except (OSError, subprocess.SubprocessError, ValueError):
        errors.append("Docker Compose version")

    try:
        _validated_observer_environment(observer_admission)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return (*errors, "observer release manifest unavailable or changed")
    try:
        config_result = runner(
            [
                "docker",
                "compose",
                *platform_compose_topology_arguments(
                    deployment_root=root,
                    candidate_root=candidate_root or root,
                    env_file=root / "data" / "user" / "settings" / "docker.env",
                ),
                "--profile",
                "legacy-local",
                "config",
                "--format",
                "json",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        payload = json.loads(config_result.stdout)
        services = payload["services"]
        if provider_configuration is not None or provider_secrets_root is not None:
            if provider_secrets_root is None:
                raise ValueError("provider root required")
            errors.extend(
                _inspect_provider_compose_bindings(
                    payload, provider_configuration, provider_secrets_root
                )
            )
        errors.extend(
            _inspect_observer_compose_environment(
                services.get("openmaic-shared-ingress-observer"),
                admitted_environment=environment,
            )
        )
        expected_ports = parse_gateway_candidate_ports(
            artifact_paths.platform_compose.read_bytes(),
            overlay_bodies=(artifact_paths.observer_overlay.read_bytes(),),
        )
        if config_result.returncode != 0 or gateway_service_ports(services) != expected_ports:
            errors.append("gateway-only public ports")
        else:
            shared_gateway = expected_ports[0]["hostIp"] == "127.0.0.1"
        if effective_image_lock_path is not None:
            lock = json.loads(effective_image_lock_path.read_text(encoding="utf-8"))
            images = lock["images"]
            support_images = lock["supportImages"]
            service_images = {
                "deeptutor": "deeptutor",
                "gateway": "nginx",
                "minio": "minio",
                "minio-bootstrap": "minio_client",
                "openmaic": "openmaic",
                "openmaic-render": "openmaic_render",
                "pocketbase": "pocketbase",
                "postgres": "postgres",
            }
            support_service_images = {
                "openmaic-observer-attestation-signer": "observer_signer",
                "openmaic-shared-ingress-observer": "observer_signer",
            }
            if any(
                services.get(service_name, {}).get("image")
                != images.get(lock_name, {}).get("reference")
                for service_name, lock_name in service_images.items()
            ) or any(
                services.get(service_name, {}).get("image")
                != support_images.get(lock_name, {}).get("reference")
                for service_name, lock_name in support_service_images.items()
            ):
                errors.append("image lock match")
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        errors.append("gateway-only public ports")
    try:
        _validated_observer_environment(observer_admission)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        errors.append("observer release manifest unavailable or changed")
    if shared_gateway and not errors:
        # Docker <28 can expose localhost-published ports to peers on the same L2 network.
        engine_error = "Docker Engine localhost isolation requires 28.0.0"
        try:
            engine = runner(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            version = re.fullmatch(
                r"(\d+)\.(\d+)\.(\d+)(?:\+[A-Za-z0-9.-]+)?", engine.stdout.strip()
            )
            if (
                engine.returncode != 0
                or version is None
                or tuple(int(part) for part in version.groups()) < (28, 0, 0)
            ):
                errors.append(engine_error)
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append(engine_error)
        try:
            _validated_observer_environment(observer_admission)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            errors.append("observer release manifest unavailable or changed")
    if provider_configuration is not None:
        try:
            current_provider = read_provider_configuration(
                provider_secrets_root, scope="shared", tenant_id=None, profile_id="platform-default"
            )
            if current_provider != provider_configuration:
                raise ProviderConfigurationError()
        except ProviderConfigurationError:
            errors.append("OpenMAIC provider configuration changed")
    return tuple(dict.fromkeys(errors))


def _inspect_secret(path: Path, name: str) -> tuple[str, ...]:
    if path.is_symlink() or not path.is_file():
        return (f"{name} missing",)
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
        restricted = secret_file_is_restricted(path)
    except (OSError, UnicodeError):
        return (f"{name} unreadable",)
    errors: list[str] = []
    if not value:
        errors.append(f"{name} empty")
    if not restricted:
        errors.append(f"{name} permissions")
    return tuple(errors)


def _inspect_tls(secret_dir: Path, hostname: str) -> tuple[str, ...]:
    certificate = secret_dir / "gateway_fullchain.pem"
    private_key = secret_dir / "gateway_private_key.pem"
    errors: list[str] = []
    if certificate.is_symlink() or not certificate.is_file():
        errors.append("gateway TLS certificate")
    if private_key.is_symlink() or not private_key.is_file():
        errors.append("gateway TLS private key")
    if errors:
        return tuple(errors)

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(certificate.read_bytes())
        key = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
        if cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) != key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ):
            errors.append("gateway TLS key mismatch")
        names = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
        if not any(_dns_name_matches(name, hostname) for name in names):
            errors.append("gateway TLS hostname")
        expires_at = getattr(cert, "not_valid_after_utc", None)
        if expires_at is None:
            expires_at = cert.not_valid_after.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        if expires_at - now < timedelta(days=14):
            errors.append("gateway TLS expiry")
        if not secret_file_is_restricted(private_key):
            errors.append("gateway TLS private key permissions")
    except Exception:
        errors.append("gateway TLS certificate invalid")
    return tuple(errors)


def _inspect_image_lock(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        images = payload["images"]
        if not isinstance(images, dict) or not images:
            raise ValueError
    except (FileNotFoundError, KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return ("image lock invalid",)
    errors: list[str] = []
    for image in images.values():
        if not isinstance(image, dict):
            errors.append("image lock invalid")
            continue
        digest = image.get("digest")
        reference = image.get("reference")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or digest == _ZERO_DIGEST:
            errors.append("image lock digest")
        if (
            not isinstance(reference, str)
            or not isinstance(digest, str)
            or not reference.endswith(f"@{digest}")
        ):
            errors.append("image lock reference")
    try:
        load_image_lock(path, require_candidate=True)
    except ValueError:
        errors.append("image lock candidate")
    return tuple(dict.fromkeys(errors))


def _inspect_platform_config(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return ("platform settings",)
    errors: list[str] = []
    if payload.get("shared_generation_limit") != 20:
        errors.append("shared generation slots")
    if payload.get("default_tenant_generation_limit") != 2:
        errors.append("tenant generation slots")
    return tuple(errors)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = Path(path).lstat()
    except OSError:
        return False
    if stat.S_ISLNK(details.st_mode):
        return True
    is_junction = getattr(Path(path), "is_junction", None)
    try:
        if callable(is_junction) and is_junction():
            return True
    except OSError:
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(details, "st_file_attributes", 0) & reparse_flag)


def _observer_runtime_secret_proof(
    details: Any,
    *,
    file_name: str,
    platform_name: str = os.name,
) -> dict[str, object]:
    if platform_name != "posix":
        raise ValueError("observer runtime secret requires POSIX ownership metadata")
    if (
        type(details.st_uid) is not int
        or type(details.st_gid) is not int
        or details.st_uid != _OBSERVER_CONTAINER_UID
        or details.st_gid != _OBSERVER_CONTAINER_GID
    ):
        raise ValueError("observer runtime secret is not bound to observer container identity")
    mode = stat.S_IMODE(details.st_mode)
    if mode not in {0o400, 0o600}:
        raise ValueError("observer runtime secret mode is not restricted")
    return {
        "fileName": file_name,
        "mode": f"{mode:04o}",
        "ownerId": int(details.st_uid),
        "groupId": int(details.st_gid),
        "size": int(details.st_size),
    }


def _inspect_observer_secret_preflight(
    observer_secret_dir: Path,
    release_run_id: str,
    observer_signer_image: str,
) -> tuple[tuple[str, ...], dict[str, object] | None]:
    root = Path(observer_secret_dir)
    errors: list[str] = []
    if not root.is_absolute():
        return ("observer secret directory must be absolute",), None
    if _RELEASE_RUN_ID.fullmatch(release_run_id) is None:
        return ("observer secret release run id is invalid",), None
    image_name, separator, image_digest = observer_signer_image.rpartition("@sha256:")
    if (
        not separator
        or _IMMUTABLE_IMAGE_NAME.fullmatch(image_name) is None
        or _DIGEST.fullmatch(f"sha256:{image_digest}") is None
        or image_digest == "0" * 64
    ):
        return ("observer signer image is not immutable",), None
    run_directory = root / release_run_id
    if run_directory.parent != root or run_directory.name != release_run_id:
        return ("observer secret run directory is invalid",), None
    try:
        resolved_root = root.resolve(strict=True)
        resolved_run_directory = run_directory.resolve(strict=True)
        root_details = root.lstat()
        run_details = run_directory.lstat()
    except OSError:
        return ("observer secret run directory is unavailable",), None
    if (
        not stat.S_ISDIR(root_details.st_mode)
        or not stat.S_ISDIR(run_details.st_mode)
        or resolved_run_directory.parent != resolved_root
        or _is_link_or_reparse(root)
        or _is_link_or_reparse(run_directory)
    ):
        return ("observer secret run directory boundary is invalid",), None
    expected_owner_id = os.geteuid() if hasattr(os, "geteuid") else int(run_details.st_uid)
    run_mode = stat.S_IMODE(run_details.st_mode)
    if run_details.st_uid != expected_owner_id or run_mode & 0o077:
        errors.append("observer secret run directory is not owner-only")

    identities: set[tuple[int, int]] = set()
    values: set[bytes] = set()
    secret_proof: dict[str, object] = {}
    for name, file_name in OBSERVER_SECRET_FILES.items():
        path = run_directory / file_name
        try:
            details = path.lstat()
            if (
                _is_link_or_reparse(path)
                or not stat.S_ISREG(details.st_mode)
                or details.st_size <= 0
                or details.st_size > _MAX_OBSERVER_SECRET_BYTES
            ):
                raise ValueError
            try:
                proof = _observer_runtime_secret_proof(
                    details,
                    file_name=file_name,
                )
            except ValueError:
                errors.append(f"{name} is not bound to the observer container identity")
                proof = None
            if not secret_file_is_restricted(path):
                errors.append(f"{name} is not owner-only")
            body = path.read_bytes()
            confirmed = path.lstat()
            if (
                len(body) != details.st_size
                or _is_link_or_reparse(path)
                or (confirmed.st_dev, confirmed.st_ino) != (details.st_dev, details.st_ino)
                or confirmed.st_mode != details.st_mode
                or confirmed.st_uid != details.st_uid
                or confirmed.st_gid != details.st_gid
                or confirmed.st_size != details.st_size
            ):
                raise ValueError
        except (OSError, ValueError):
            errors.append(f"{name} is unavailable or invalid")
            continue
        identity = (details.st_dev, details.st_ino)
        if identity in identities or body in values:
            errors.append("observer secrets are not distinct")
        identities.add(identity)
        values.add(body)
        if proof is not None:
            secret_proof[name] = proof
    errors = list(dict.fromkeys(errors))
    try:
        confirmed_run = run_directory.lstat()
        confirmed_root = root.lstat()
    except OSError:
        errors.append("observer secret run directory changed during preflight")
    else:
        if (
            (confirmed_root.st_dev, confirmed_root.st_ino)
            != (root_details.st_dev, root_details.st_ino)
            or (confirmed_run.st_dev, confirmed_run.st_ino)
            != (run_details.st_dev, run_details.st_ino)
            or _is_link_or_reparse(root)
            or _is_link_or_reparse(run_directory)
        ):
            errors.append("observer secret run directory changed during preflight")
    errors = list(dict.fromkeys(errors))
    if errors or set(secret_proof) != set(OBSERVER_SECRET_FILES):
        return tuple(errors or ("observer secret preflight is incomplete",)), None
    return (), {
        "schemaVersion": 1,
        "releaseRunId": release_run_id,
        "runDirectory": str(resolved_run_directory),
        "ownerOnly": True,
        "noLinksOrReparsePoints": True,
        "distinctSecrets": True,
        "observerSignerImage": observer_signer_image,
        "secrets": secret_proof,
    }


def run_preflight(
    *,
    secret_dir: Path,
    required_secret_names: Sequence[str] = DEFAULT_REQUIRED_SECRETS,
    tls_hostname: str | None = None,
    image_lock_path: Path | None = None,
    platform_config_path: Path | None = None,
    observer_secret_dir: Path | None = None,
    release_run_id: str | None = None,
    observer_signer_image: str | None = None,
) -> PreflightResult:
    errors: list[str] = []
    root = Path(secret_dir)
    for name in required_secret_names:
        errors.extend(_inspect_secret(root / name, name))
    if tls_hostname is not None:
        errors.extend(_inspect_tls(root, tls_hostname))
    if image_lock_path is not None:
        errors.extend(_inspect_image_lock(Path(image_lock_path)))
    if platform_config_path is not None:
        errors.extend(_inspect_platform_config(Path(platform_config_path)))
    observer_secret_preflight_proof: dict[str, object] | None = None
    observer_inputs = (observer_secret_dir, release_run_id, observer_signer_image)
    if any(value is not None for value in observer_inputs) and not all(
        value is not None for value in observer_inputs
    ):
        errors.append("observer secret preflight inputs are incomplete")
    elif (
        observer_secret_dir is not None
        and release_run_id is not None
        and observer_signer_image is not None
    ):
        observer_errors, observer_secret_preflight_proof = _inspect_observer_secret_preflight(
            observer_secret_dir,
            release_run_id,
            observer_signer_image,
        )
        errors.extend(observer_errors)
    return PreflightResult(
        tuple(dict.fromkeys(errors)),
        observer_secret_preflight=observer_secret_preflight_proof,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the private platform deployment")
    parser.add_argument("--config", type=Path, default=Path("deploy/platform.example.json"))
    parser.add_argument("--secret-dir", type=Path, default=Path("data/system/secrets"))
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--image-lock", type=Path)
    parser.add_argument("--hostname")
    parser.add_argument("--observer-secret-dir", type=Path)
    parser.add_argument("--observer-release-manifest", type=Path)
    parser.add_argument("--release-run-id")
    parser.add_argument("--observer-signer-image")
    parser.add_argument("--offline-contract-check", action="store_true")
    parser.add_argument("--runtime-phase", choices=PHASES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.runtime_phase is not None:
        if (
            arguments.candidate_root is not None
            or arguments.image_lock is not None
            or arguments.hostname is not None
            or arguments.observer_secret_dir is not None
            or arguments.observer_release_manifest is not None
            or arguments.release_run_id is not None
            or arguments.observer_signer_image is not None
            or arguments.offline_contract_check
        ):
            parser.error("--runtime-phase cannot be combined with host preflight options")
        try:
            from deeptutor.services.config import load_platform_settings

            settings = load_platform_settings(arguments.config)
        except Exception:
            report = failed_candidate_network_report(
                arguments.runtime_phase,
                "platform settings",
            )
        else:
            try:
                report = asyncio.run(
                    inspect_candidate_network_phase(
                        phase=arguments.runtime_phase,
                        settings=settings,
                        secret_dir=arguments.secret_dir,
                    )
                )
            except Exception:
                report = failed_candidate_network_report(
                    arguments.runtime_phase,
                    "candidate network preflight",
                )
        try:
            body = canonical_candidate_network_report(report)
            validated = parse_candidate_network_report(
                body,
                expected_phase=arguments.runtime_phase,
            )
        except Exception:
            report = failed_candidate_network_report(
                arguments.runtime_phase,
                "candidate network preflight",
            )
            body = canonical_candidate_network_report(report)
            validated = parse_candidate_network_report(
                body,
                expected_phase=arguments.runtime_phase,
            )
        sys.stdout.write(body.decode("utf-8"))
        checks = validated["checks"]
        errors = validated["errors"]
        assert isinstance(checks, dict) and isinstance(errors, list)
        return 0 if not errors and all(value is True for value in checks.values()) else 1
    if arguments.candidate_root is not None and arguments.image_lock is not None:
        parser.error("--candidate-root cannot be combined with --image-lock")
    if arguments.image_lock is not None:
        print("image lock candidate")
        return 1
    observer_inputs = (
        arguments.observer_secret_dir,
        arguments.release_run_id,
        arguments.observer_signer_image,
    )
    if arguments.observer_release_manifest is not None:
        if not arguments.observer_release_manifest.is_absolute():
            parser.error("--observer-release-manifest must be an absolute path")
        if any(value is not None for value in observer_inputs):
            parser.error(
                "--observer-release-manifest cannot be combined with manual observer inputs"
            )
    if any(value is not None for value in observer_inputs) and not all(
        value is not None for value in observer_inputs
    ):
        parser.error(
            "--observer-secret-dir, --release-run-id, and --observer-signer-image "
            "must be provided together"
        )
    candidate_root = arguments.candidate_root or PROJECT_ROOT
    if not candidate_root.is_absolute():
        parser.error("--candidate-root must be an absolute path")
    candidate_root = candidate_root.resolve()
    artifact_paths = candidate_artifact_paths(candidate_root)
    image_lock_path = artifact_paths.image_lock
    candidate_lease = validated_candidate_artifact_lease(candidate_root)
    errors: list[str] = []
    try:
        with candidate_lease as artifact:
            observer_admission = None
            preflight_observer_inputs = {
                "observer_secret_dir": arguments.observer_secret_dir,
                "release_run_id": arguments.release_run_id,
                "observer_signer_image": arguments.observer_signer_image,
            }
            if arguments.observer_release_manifest is not None:
                try:
                    observer_admission = load_observer_release_admission(
                        manifest_path=arguments.observer_release_manifest,
                        candidate_root=candidate_root,
                        image_lock_bytes=artifact.image_lock_bytes,
                    )
                    inputs = observer_admission["platformPreflightInputs"]
                    preflight_observer_inputs = {
                        "observer_secret_dir": Path(inputs["observer_secret_dir"]),
                        "release_run_id": inputs["release_run_id"],
                        "observer_signer_image": inputs["observer_signer_image"],
                    }
                except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                    print("observer release manifest unavailable or invalid")
                    return 1
            result = run_preflight(
                secret_dir=arguments.secret_dir,
                required_secret_names=(
                    () if arguments.offline_contract_check else DEFAULT_REQUIRED_SECRETS
                ),
                tls_hostname=(None if arguments.offline_contract_check else arguments.hostname),
                image_lock_path=image_lock_path,
                platform_config_path=arguments.config,
                **preflight_observer_inputs,
            )
            errors.extend(result.errors)
            if not arguments.offline_contract_check:
                artifact.check_unchanged()
                if observer_admission is None:
                    errors.append("observer release manifest required")
                if arguments.hostname is None:
                    errors.append("gateway TLS hostname")
                if not errors:
                    try:
                        from deeptutor.services.config import load_platform_settings

                        settings = load_platform_settings(arguments.config)
                    except Exception:
                        errors.append("platform settings")
                    else:
                        artifact.check_unchanged()
                        errors.extend(
                            asyncio.run(
                                run_runtime_preflight(
                                    settings=settings,
                                    secret_dir=arguments.secret_dir,
                                    image_lock_path=image_lock_path,
                                    project_root=PROJECT_ROOT,
                                    candidate_root=candidate_root,
                                    observer_admission=observer_admission,
                                )
                            )
                        )
    except CandidateArtifactError:
        errors.append("image lock candidate")
    errors = list(dict.fromkeys(errors))
    if errors:
        for error in errors:
            print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
