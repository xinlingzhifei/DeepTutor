"""Provision tenant-scoped MinIO credentials without exposing bootstrap keys."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path, PureWindowsPath
import secrets
import stat
from typing import Any, Protocol
from urllib.parse import urlsplit

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.artifacts import tenant_artifact_prefix
from deeptutor.teaching.provisioning_worker import (
    ProvisioningStepError,
    StorageProvisioningResult,
)
from deeptutor.teaching.secret_permissions import restrict_secret_file

_ACCESS_KEY_FILE = "object-store-access-key"
_SECRET_KEY_FILE = "object-store-secret-key"


def tenant_secret_ref(tenant_id: str) -> str:
    """Return the opaque, server-derived directory for one tenant."""

    tenant_artifact_prefix(tenant_id)
    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    return f"tenant_{digest[:16]}"


def secret_ref_is_bound_to_tenant(secret_ref: str, tenant_id: str) -> bool:
    if not isinstance(secret_ref, str) or not secret_ref:
        return False
    if (
        secret_ref.startswith("/")
        or secret_ref.endswith("/")
        or "\\" in secret_ref
        or ":" in secret_ref
        or "\x00" in secret_ref
        or PureWindowsPath(secret_ref).drive
    ):
        return False
    parts = secret_ref.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if parts == [tenant_id, "object-store"]:
        return True
    root = tenant_secret_ref(tenant_id)
    if parts[0] != root:
        return False
    return len(parts) == 1 or (
        len(parts) == 3
        and parts[1] == "rotations"
        and len(parts[2]) >= 16
        and all(character.isalnum() or character in "-_" for character in parts[2])
    )


def build_tenant_policy(*, bucket: str, tenant_prefix: str) -> dict[str, Any]:
    if not bucket or "/" in bucket or "\x00" in bucket:
        raise ValueError("object-store bucket is invalid")
    if not tenant_prefix.startswith("tenants/") or not tenant_prefix.endswith("/"):
        raise ValueError("tenant prefix is invalid")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TenantPrefixList",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
                "Condition": {"StringLike": {"s3:prefix": [f"{tenant_prefix}*"]}},
            },
            {
                "Sid": "TenantObjects",
                "Effect": "Allow",
                "Action": [
                    "s3:DeleteObject",
                    "s3:GetObject",
                    "s3:ListBucketMultipartUploads",
                    "s3:ListMultipartUploadParts",
                    "s3:PutObject",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/{tenant_prefix}*"],
            },
        ],
    }


@dataclass(frozen=True, slots=True, repr=False)
class TenantCredentialPair:
    access_key: str
    secret_key: str

    def __post_init__(self) -> None:
        for value in (self.access_key, self.secret_key):
            if not value or "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError("tenant credential is invalid")

    @property
    def access_key_fingerprint(self) -> str:
        return hashlib.sha256(self.access_key.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return "TenantCredentialPair(access_key=<redacted>, secret_key=<redacted>)"


class TenantStorageAdmin(Protocol):
    async def create(
        self,
        *,
        tenant_id: str,
        policy: dict[str, Any],
    ) -> TenantCredentialPair: ...

    async def verify(
        self,
        *,
        credentials: TenantCredentialPair,
        own_prefix: str,
        denied_prefix: str,
    ) -> None: ...

    async def revoke(self, access_key: str) -> None: ...


class TenantCredentialPublisher(Protocol):
    async def current_secret_ref(self, tenant_id: str) -> str | None: ...

    async def publish(self, result: StorageProvisioningResult) -> None: ...


def _safe_ref_parts(secret_ref: str) -> tuple[str, ...]:
    if (
        not secret_ref
        or secret_ref.startswith("/")
        or secret_ref.endswith("/")
        or "\\" in secret_ref
        or ":" in secret_ref
        or "\x00" in secret_ref
        or PureWindowsPath(secret_ref).drive
    ):
        raise ValueError("tenant secret reference is unsafe")
    parts = tuple(secret_ref.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("tenant secret reference is unsafe")
    return parts


def _write_secret(path: Path, value: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        payload = f"{value}\n".encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    restrict_secret_file(path)


class TenantSecretStore:
    """Store credential pairs below one explicit, non-symlink root."""

    def __init__(self, root: Path) -> None:
        candidate = Path(root)
        if not candidate.is_absolute() or candidate.is_symlink():
            raise ValueError("tenant credential root must be an absolute directory")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("tenant credential root is unsafe")
        self._root = candidate.resolve(strict=True)

    def _directory(self, secret_ref: str, *, require: bool) -> Path:
        current = self._root
        for part in _safe_ref_parts(secret_ref):
            current = current / part
            if current.is_symlink():
                raise ValueError("tenant credential directory is unsafe")
        try:
            resolved = current.resolve(strict=require)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as exc:
            raise ValueError("tenant credential directory is unavailable") from exc
        return resolved

    def base_ref(self, tenant_id: str) -> str:
        return tenant_secret_ref(tenant_id)

    def exists(self, secret_ref: str) -> bool:
        try:
            directory = self._directory(secret_ref, require=True)
        except ValueError:
            return False
        return (directory / _ACCESS_KEY_FILE).is_file() and (directory / _SECRET_KEY_FILE).is_file()

    def load(self, secret_ref: str, *, tenant_id: str) -> TenantCredentialPair:
        if not secret_ref_is_bound_to_tenant(secret_ref, tenant_id):
            raise ValueError("tenant credential reference is not bound to tenant")
        directory = self._directory(secret_ref, require=True)
        values: list[str] = []
        for name in (_ACCESS_KEY_FILE, _SECRET_KEY_FILE):
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("tenant credential file is unavailable")
            try:
                path.resolve(strict=True).relative_to(self._root)
                value = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError("tenant credential file is unavailable") from exc
            if not value or "\x00" in value:
                raise ValueError("tenant credential file is invalid")
            values.append(value)
        return TenantCredentialPair(access_key=values[0], secret_key=values[1])

    def publish(
        self,
        *,
        tenant_id: str,
        credentials: TenantCredentialPair,
        rotate: bool,
    ) -> str:
        base = self.base_ref(tenant_id)
        secret_ref = f"{base}/rotations/{secrets.token_urlsafe(18)}" if rotate else base
        directory = self._directory(secret_ref, require=False)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            resolved = directory.resolve(strict=True)
            resolved.relative_to(self._root)
            if directory.is_symlink():
                raise ValueError("tenant credential directory is unsafe")
            destinations = (
                (directory / _ACCESS_KEY_FILE, credentials.access_key),
                (directory / _SECRET_KEY_FILE, credentials.secret_key),
            )
            for path, _value in destinations:
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError("tenant credential file is unsafe")
            if all(path.is_file() for path, _value in destinations):
                raise FileExistsError("tenant credential pair already exists")

            staged: list[tuple[Path, Path]] = []
            for destination, value in destinations:
                staging = directory / (f".{destination.name}.{secrets.token_urlsafe(12)}.tmp")
                _write_secret(staging, value)
                staged.append((staging, destination))
            for staging, destination in staged:
                os.replace(staging, destination)
        except Exception:
            # The directory is intentionally retained on failure.  A partial
            # credential is never returned or referenced by the database.
            raise
        return secret_ref


def _storage_result(
    tenant_id: str,
    *,
    secret_ref: str,
    credentials: TenantCredentialPair,
) -> StorageProvisioningResult:
    expected = StorageProvisioningResult.local(tenant_id)
    return StorageProvisioningResult(
        mode="s3",
        policy_version=expected.policy_version,
        policy_payload=expected.policy_payload,
        policy_hash=expected.policy_hash,
        secret_ref=secret_ref,
        access_key_fingerprint=credentials.access_key_fingerprint,
    )


async def provision_tenant_storage(
    *,
    settings: PlatformSettings,
    tenant_id: str,
    admin: TenantStorageAdmin,
    secret_store: TenantSecretStore,
    publisher: TenantCredentialPublisher,
    rotate: bool = False,
) -> StorageProvisioningResult:
    if settings.object_store_mode != "s3":
        raise ValueError("tenant MinIO provisioning requires S3 mode")
    own_prefix = tenant_artifact_prefix(tenant_id)
    denied_prefix = "tenants/__cross_tenant_probe__/"
    base_ref = secret_store.base_ref(tenant_id)
    current_ref = getattr(publisher, "current_secret_ref", None)
    published_ref = await current_ref(tenant_id) if current_ref is not None else None
    old_ref = published_ref
    orphaned_base = False
    if old_ref is None and secret_store.exists(base_ref):
        old_ref = base_ref
        orphaned_base = current_ref is not None
    if old_ref is not None and (
        not secret_ref_is_bound_to_tenant(old_ref, tenant_id) or not secret_store.exists(old_ref)
    ):
        raise ProvisioningStepError(
            category="storage",
            code="invalid_credential_metadata",
            retryable=False,
        )
    old_credentials = (
        secret_store.load(old_ref, tenant_id=tenant_id) if old_ref is not None else None
    )

    recover_orphaned_base = False
    if old_credentials is not None and not rotate:
        try:
            await admin.verify(
                credentials=old_credentials,
                own_prefix=own_prefix,
                denied_prefix=denied_prefix,
            )
        except ProvisioningStepError as exc:
            if not orphaned_base or exc.retryable:
                raise
            recover_orphaned_base = True
        else:
            result = _storage_result(
                tenant_id,
                secret_ref=old_ref,
                credentials=old_credentials,
            )
            await publisher.publish(result)
            return result

    policy = build_tenant_policy(
        bucket=settings.object_store_bucket,
        tenant_prefix=own_prefix,
    )
    credentials = await admin.create(tenant_id=tenant_id, policy=policy)
    try:
        await admin.verify(
            credentials=credentials,
            own_prefix=own_prefix,
            denied_prefix=denied_prefix,
        )
        secret_ref = secret_store.publish(
            tenant_id=tenant_id,
            credentials=credentials,
            rotate=rotate or recover_orphaned_base,
        )
        result = _storage_result(
            tenant_id,
            secret_ref=secret_ref,
            credentials=credentials,
        )
        await publisher.publish(result)
    except Exception:
        await admin.revoke(credentials.access_key)
        raise
    if old_credentials is not None and not recover_orphaned_base:
        await admin.revoke(old_credentials.access_key)
    return result


class _NoopPublisher:
    async def current_secret_ref(self, tenant_id: str) -> str | None:
        from deeptutor.teaching.storage_credentials import (
            SqlAlchemyStorageCredentialRepository,
        )

        record = await SqlAlchemyStorageCredentialRepository().get_active(tenant_id)
        return None if record is None else record.secret_ref

    async def publish(self, result: StorageProvisioningResult) -> None:
        del result


def _read_bootstrap_secret(path: Path) -> str:
    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError
        value = candidate.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProvisioningStepError(
            category="storage",
            code="bootstrap_secret_unavailable",
            retryable=False,
        ) from exc
    if not value or "\x00" in value:
        raise ProvisioningStepError(
            category="storage",
            code="bootstrap_secret_invalid",
            retryable=False,
        )
    return value


class RuntimeMinioTenantStorageAdmin:
    """Official MinIO Admin/S3 adapter used only by tenant-provisioner."""

    def __init__(
        self,
        *,
        settings: PlatformSettings,
        bootstrap_access_key_file: Path,
        bootstrap_secret_key_file: Path,
    ) -> None:
        self._settings = settings
        self._bootstrap_access_key_file = Path(bootstrap_access_key_file)
        self._bootstrap_secret_key_file = Path(bootstrap_secret_key_file)

    @classmethod
    def from_settings(cls, settings: PlatformSettings) -> "RuntimeMinioTenantStorageAdmin":
        access_path = getattr(settings, "minio_bootstrap_access_key_file", None)
        secret_path = getattr(settings, "minio_bootstrap_secret_key_file", None)
        return cls(
            settings=settings,
            bootstrap_access_key_file=Path(
                access_path
                or os.environ.get(
                    "MINIO_ROOT_USER_FILE",
                    "/run/secrets/minio_bootstrap_access_key",
                )
            ),
            bootstrap_secret_key_file=Path(
                secret_path
                or os.environ.get(
                    "MINIO_ROOT_PASSWORD_FILE",
                    "/run/secrets/minio_bootstrap_secret_key",
                )
            ),
        )

    def _endpoint(self) -> tuple[str, bool]:
        parsed = urlsplit(self._settings.object_store_endpoint or "")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ProvisioningStepError(
                category="storage",
                code="endpoint_invalid",
                retryable=False,
            )
        return parsed.netloc, parsed.scheme == "https"

    def _clients(self, credentials: TenantCredentialPair | None = None):
        try:
            from minio import Minio
            from minio.credentials.providers import StaticProvider
            from minio.minioadmin import MinioAdmin
        except ImportError as exc:
            raise ProvisioningStepError(
                category="storage",
                code="minio_admin_sdk_unavailable",
                retryable=False,
            ) from exc

        endpoint, secure = self._endpoint()
        bootstrap = TenantCredentialPair(
            access_key=_read_bootstrap_secret(self._bootstrap_access_key_file),
            secret_key=_read_bootstrap_secret(self._bootstrap_secret_key_file),
        )
        admin = MinioAdmin(
            endpoint=endpoint,
            credentials=StaticProvider(bootstrap.access_key, bootstrap.secret_key),
            region=self._settings.object_store_region,
            secure=secure,
        )
        pair = credentials or bootstrap
        client = Minio(
            endpoint=endpoint,
            access_key=pair.access_key,
            secret_key=pair.secret_key,
            secure=secure,
            region=self._settings.object_store_region,
        )
        return admin, client

    async def create(
        self,
        *,
        tenant_id: str,
        policy: dict[str, Any],
    ) -> TenantCredentialPair:
        del tenant_id
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        credentials = TenantCredentialPair(
            access_key="YT" + "".join(secrets.choice(alphabet) for _ in range(18)),
            secret_key=secrets.token_urlsafe(24),
        )

        def create_sync() -> None:
            admin, _ = self._clients()
            admin.add_service_account(
                access_key=credentials.access_key,
                secret_key=credentials.secret_key,
                name="yfeistai-tenant",
                description="yFeiSTAI tenant object-store credential",
                policy=policy,
            )

        try:
            await asyncio.to_thread(create_sync)
        except ProvisioningStepError:
            raise
        except Exception as exc:
            raise ProvisioningStepError(
                category="storage",
                code="service_account_create_failed",
                retryable=True,
            ) from exc
        return credentials

    async def verify(
        self,
        *,
        credentials: TenantCredentialPair,
        own_prefix: str,
        denied_prefix: str,
    ) -> None:
        await asyncio.to_thread(
            self._verify_sync,
            credentials,
            own_prefix,
            denied_prefix,
        )

    def _verify_sync(
        self,
        credentials: TenantCredentialPair,
        own_prefix: str,
        denied_prefix: str,
    ) -> None:
        from minio.error import S3Error

        _, client = self._clients(credentials)
        bucket = self._settings.object_store_bucket
        probe = f"{own_prefix}.provisioning-probe-{secrets.token_hex(8)}"
        denied_probe = f"{denied_prefix}.provisioning-probe-{secrets.token_hex(8)}"
        try:
            client.put_object(bucket, probe, io.BytesIO(b"ok"), length=2)
            client.stat_object(bucket, probe)
            if not any(
                item.object_name == probe for item in client.list_objects(bucket, prefix=own_prefix)
            ):
                raise ProvisioningStepError(
                    category="storage",
                    code="own_prefix_probe_failed",
                    retryable=False,
                )
            denied_operations = (
                lambda: client.put_object(
                    bucket,
                    denied_probe,
                    io.BytesIO(b"denied"),
                    length=6,
                ),
                lambda: list(client.list_objects(bucket, prefix=denied_prefix)),
                lambda: client.stat_object(bucket, denied_probe),
            )
            for operation in denied_operations:
                try:
                    operation()
                except S3Error as exc:
                    if exc.code != "AccessDenied":
                        raise ProvisioningStepError(
                            category="storage",
                            code="cross_prefix_probe_inconclusive",
                            retryable=False,
                        ) from exc
                else:
                    raise ProvisioningStepError(
                        category="storage",
                        code="cross_prefix_access_allowed",
                        retryable=False,
                    )
        except ProvisioningStepError:
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise ProvisioningStepError(
                category="storage",
                code="probe_unavailable",
                retryable=True,
            ) from exc
        except S3Error as exc:
            raise ProvisioningStepError(
                category="storage",
                code="own_prefix_probe_failed",
                retryable=False,
            ) from exc
        finally:
            try:
                client.remove_object(bucket, probe)
            except Exception:
                pass

    async def revoke(self, access_key: str) -> None:
        def revoke_sync() -> None:
            admin, _ = self._clients()
            admin.delete_service_account(access_key)

        try:
            await asyncio.to_thread(revoke_sync)
        except ProvisioningStepError:
            raise
        except Exception as exc:
            raise ProvisioningStepError(
                category="storage",
                code="service_account_revoke_failed",
                retryable=True,
            ) from exc

    async def provision_and_verify(
        self,
        *,
        settings: PlatformSettings,
        tenant_id: str,
        tenant_prefix: str,
    ) -> StorageProvisioningResult:
        if settings is not self._settings or tenant_prefix != tenant_artifact_prefix(tenant_id):
            raise ProvisioningStepError(
                category="storage",
                code="tenant_binding_invalid",
                retryable=False,
            )
        root = settings.object_store_tenant_credentials_dir
        if root is None:
            raise ProvisioningStepError(
                category="storage",
                code="credential_root_unavailable",
                retryable=False,
            )
        return await provision_tenant_storage(
            settings=settings,
            tenant_id=tenant_id,
            admin=self,
            secret_store=TenantSecretStore(root),
            publisher=_NoopPublisher(),
        )


__all__ = [
    "RuntimeMinioTenantStorageAdmin",
    "TenantCredentialPair",
    "TenantCredentialPublisher",
    "TenantSecretStore",
    "TenantStorageAdmin",
    "build_tenant_policy",
    "provision_tenant_storage",
    "secret_ref_is_bound_to_tenant",
    "tenant_secret_ref",
]
