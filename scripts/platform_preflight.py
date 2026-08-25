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
import subprocess
import sys
from typing import Any, Protocol

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from render_platform_compose import (
    candidate_artifact_paths,
    load_image_lock,
    validate_image_lock_bindings,
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
    ) -> tuple[str, ...]: ...


async def run_runtime_preflight(
    *,
    settings: Any,
    secret_dir: Path,
    image_lock_path: Path,
    project_root: Path,
    candidate_root: Path,
    probe: RuntimePreflightProbe | None = None,
) -> tuple[str, ...]:
    runtime_probe = probe or DefaultRuntimePreflightProbe()
    errors = await runtime_probe.inspect(
        settings=settings,
        secret_dir=Path(secret_dir),
        image_lock_path=Path(image_lock_path),
        project_root=Path(project_root),
        candidate_root=Path(candidate_root),
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


class DefaultRuntimePreflightProbe:
    async def inspect(
        self,
        *,
        settings: Any,
        secret_dir: Path,
        image_lock_path: Path,
        project_root: Path,
        candidate_root: Path,
    ) -> tuple[str, ...]:
        database = await _inspect_database_runtime(settings)
        errors = list(database.errors)
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
            )
        )
        return tuple(dict.fromkeys(errors))


def _inspect_compose_runtime(
    project_root: Path,
    *,
    candidate_root: Path | None = None,
    image_lock_path: Path | None = None,
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
    environment.pop("COMPOSE_FILE", None)
    environment.pop("COMPOSE_PROFILES", None)
    errors: list[str] = []
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
        config_result = runner(
            [
                "docker",
                "compose",
                "-f",
                str(root / "docker-compose.yml"),
                "-f",
                str(artifact_paths.platform_compose),
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
        published_services = {
            service_name for service_name, service in services.items() if service.get("ports")
        }
        gateway_ports = {
            str(port.get("published"))
            for port in services.get("gateway", {}).get("ports", ())
            if isinstance(port, dict) and port.get("published") is not None
        }
        if (
            config_result.returncode != 0
            or published_services != {"gateway"}
            or gateway_ports != {"80", "443"}
        ):
            errors.append("gateway-only public ports")
        if effective_image_lock_path is not None:
            lock = json.loads(effective_image_lock_path.read_text(encoding="utf-8"))
            images = lock["images"]
            service_images = {
                "deeptutor": "deeptutor",
                "gateway": "nginx",
                "minio": "minio",
                "minio-bootstrap": "minio_client",
                "openmaic": "openmaic",
                "openmaic-render": "openmaic_render",
                "postgres": "postgres",
            }
            if any(
                services.get(service_name, {}).get("image")
                != images.get(lock_name, {}).get("reference")
                for service_name, lock_name in service_images.items()
            ):
                errors.append("image lock match")
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        errors.append("gateway-only public ports")
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


def run_preflight(
    *,
    secret_dir: Path,
    required_secret_names: Sequence[str] = DEFAULT_REQUIRED_SECRETS,
    tls_hostname: str | None = None,
    image_lock_path: Path | None = None,
    platform_config_path: Path | None = None,
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
    return PreflightResult(tuple(dict.fromkeys(errors)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the private platform deployment")
    parser.add_argument("--config", type=Path, default=Path("deploy/platform.example.json"))
    parser.add_argument("--secret-dir", type=Path, default=Path("data/system/secrets"))
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--image-lock", type=Path)
    parser.add_argument("--hostname")
    parser.add_argument("--offline-contract-check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.candidate_root is not None and arguments.image_lock is not None:
        parser.error("--candidate-root cannot be combined with --image-lock")
    candidate_root = arguments.candidate_root or PROJECT_ROOT
    if not candidate_root.is_absolute():
        parser.error("--candidate-root must be an absolute path")
    candidate_root = candidate_root.resolve()
    artifact_paths = candidate_artifact_paths(candidate_root)
    image_lock_path = arguments.image_lock or artifact_paths.image_lock
    result = run_preflight(
        secret_dir=arguments.secret_dir,
        required_secret_names=(
            () if arguments.offline_contract_check else DEFAULT_REQUIRED_SECRETS
        ),
        tls_hostname=(None if arguments.offline_contract_check else arguments.hostname),
        image_lock_path=image_lock_path,
        platform_config_path=arguments.config,
    )
    errors = list(result.errors)
    try:
        validate_image_lock_bindings(
            image_lock_path,
            compose_paths=(
                artifact_paths.platform_compose,
                artifact_paths.data_plane_compose,
            ),
            require_candidate=True,
        )
    except ValueError:
        errors.append("image lock candidate")
    if not arguments.offline_contract_check:
        if arguments.hostname is None:
            errors.append("gateway TLS hostname")
        if not errors:
            try:
                from deeptutor.services.config import load_platform_settings

                settings = load_platform_settings(arguments.config)
            except Exception:
                errors.append("platform settings")
            else:
                errors.extend(
                    asyncio.run(
                        run_runtime_preflight(
                            settings=settings,
                            secret_dir=arguments.secret_dir,
                            image_lock_path=image_lock_path,
                            project_root=PROJECT_ROOT,
                            candidate_root=candidate_root,
                        )
                    )
                )
    errors = list(dict.fromkeys(errors))
    if errors:
        for error in errors:
            print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
