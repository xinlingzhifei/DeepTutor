"""Create tamper-evident metadata for one explicit teaching backup directory."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
import ctypes
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable
from urllib.parse import urlsplit

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OBJECT_STORE_NAMESPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_BACKUP_MANIFEST_SCHEMA_VERSION = 3
_PG_DUMP_MAGIC = b"PGDMP"
_PG_ENVIRONMENT_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)


async def _await_owned_operation(operation: Awaitable[Any]) -> Any:
    async def run_operation() -> Any:
        return await operation

    operation_task = asyncio.create_task(run_operation())
    first_cancellation: asyncio.CancelledError | None = None
    while not operation_task.done():
        try:
            await asyncio.shield(operation_task)
        except asyncio.CancelledError as cancellation:
            if first_cancellation is None:
                first_cancellation = cancellation
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
    try:
        result = operation_task.result()
    except BaseException as operation_failure:
        if first_cancellation is not None:
            first_cancellation.add_note(
                f"owned operation failed: {type(operation_failure).__name__}"
            )
            raise first_cancellation
        raise
    if first_cancellation is not None:
        raise first_cancellation
    return result


async def _await_owned_cleanup(cleanup: Awaitable[None]) -> None:
    await _await_owned_operation(cleanup)


def _set_private_mode(path: Path, mode: int) -> None:
    if os.name != "nt":
        os.chmod(path, mode)
        return
    from deeptutor.teaching.secret_permissions import (
        restrict_secret_file,
        secret_file_is_restricted,
    )

    restrict_secret_file(path)
    if not secret_file_is_restricted(path):
        raise PermissionError("backup path permissions could not be restricted")


def _create_private_directory(path: Path) -> None:
    path.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    _set_private_mode(path, _PRIVATE_DIRECTORY_MODE)


def _write_private_new_file(path: Path, payload: bytes = b"") -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE_FILE_MODE)
    try:
        _set_private_mode(path, _PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _pg_environment(password: str) -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in _PG_ENVIRONMENT_ALLOWLIST if name in os.environ
    }
    environment["PGPASSWORD"] = password
    return environment


def _fsync_file(path: Path) -> None:
    with Path(path).open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    if os.name == "nt":
        os.rename(source_path, destination_path)
        return
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        raise RuntimeError("atomic no-replace backup publish is unavailable") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source_path),
        -100,
        os.fsencode(destination_path),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), destination_path)
    raise OSError(error_number, os.strerror(error_number), destination_path)


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _require_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_count(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _require_object_identity(tenant_id: str, key: str) -> None:
    if not isinstance(tenant_id, str) or _TENANT_ID.fullmatch(tenant_id) is None:
        raise ValueError("tenant_id is invalid")
    if not isinstance(key, str) or not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise ValueError("object key is invalid")
    if any(part in {"", ".", ".."} for part in key.split("/")):
        raise ValueError("object key is invalid")
    if not key.startswith(f"tenants/{tenant_id}/"):
        raise ValueError("object key is outside its tenant prefix")


def _require_version_id(version_id: str) -> str:
    if (
        not isinstance(version_id, str)
        or not version_id
        or len(version_id) > 1024
        or any(ord(character) < 0x20 for character in version_id)
    ):
        raise ValueError("object version_id is invalid")
    return version_id


def _require_content_type(content_type: str) -> str:
    if (
        not isinstance(content_type, str)
        or not content_type
        or content_type != content_type.strip().lower()
        or ";" in content_type
        or any(ord(character) < 0x20 for character in content_type)
    ):
        raise ValueError("object content_type is invalid")
    return content_type


def _require_owner_token(owner_token: str) -> str:
    if (
        not isinstance(owner_token, str)
        or len(owner_token) != 32
        or any(character not in "0123456789abcdef" for character in owner_token)
    ):
        raise ValueError("object owner_token is invalid")
    return owner_token


def _require_source_revision(source_revision: str) -> str:
    if (
        not isinstance(source_revision, str)
        or not source_revision
        or len(source_revision) > 1024
        or any(ord(character) < 0x20 for character in source_revision)
    ):
        raise ValueError("object source_revision is invalid")
    return source_revision


def _canonical_http_etag(value: str) -> str:
    revision = _require_source_revision(value)
    if revision.startswith('"') and revision.endswith('"') and len(revision) >= 2:
        return revision
    if '"' in revision or "\\" in revision:
        raise ValueError("object source revision is invalid")
    return f'"{revision}"'


def object_store_identity_sha256(namespace_id: str, bucket: str) -> str:
    if (
        not isinstance(namespace_id, str)
        or _OBJECT_STORE_NAMESPACE_ID.fullmatch(namespace_id) is None
        or not isinstance(bucket, str)
        or not bucket
        or bucket != bucket.strip()
        or "/" in bucket
        or "\x00" in bucket
    ):
        raise ValueError("object store identity is invalid")
    return hashlib.sha256(
        _canonical_json(
            {
                "bucket": bucket,
                "namespaceId": namespace_id,
            }
        )
    ).hexdigest()


def canonical_object_store_endpoint(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2048
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("object store endpoint is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("object store endpoint is invalid") from None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or not isinstance(hostname, str)
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("object store endpoint is invalid")
    canonical_host = hostname.lower()
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        canonical_host = f"{canonical_host}:{port}"
    return f"{scheme}://{canonical_host}"


def physical_object_store_identity_sha256(
    endpoint: object,
    region: object,
    bucket: object,
    owner_id_sha256: object,
) -> str:
    if (
        not isinstance(region, str)
        or not region
        or region != region.strip()
        or len(region) > 191
        or any(ord(character) < 0x20 for character in region)
        or not isinstance(bucket, str)
        or not bucket
        or bucket != bucket.strip()
        or "/" in bucket
        or "\x00" in bucket
        or not isinstance(owner_id_sha256, str)
        or _SHA256.fullmatch(owner_id_sha256) is None
        or owner_id_sha256 == "0" * 64
    ):
        raise ValueError("object store physical identity is invalid")
    return hashlib.sha256(
        _canonical_json(
            {
                "bucket": bucket,
                "endpoint": canonical_object_store_endpoint(endpoint),
                "ownerIdSha256": owner_id_sha256,
                "region": region.lower(),
            }
        )
    ).hexdigest()


def _require_object_store_owner_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1024
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("source object store owner is invalid")
    return value


def _payload_file_for_key(key: str) -> str:
    key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"objects/{key_digest}.blob"


def _require_minio_boolean(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value == "true":
            return True
        if value == "false":
            return False
    raise ValueError(f"MinIO returned an invalid {field}")


def _require_custom_pg_dump(path: Path) -> None:
    dump = Path(path)
    if dump.is_symlink() or not dump.is_file():
        raise RuntimeError("pg_dump did not create a valid custom-format archive")
    try:
        with dump.open("rb") as handle:
            header = handle.read(len(_PG_DUMP_MAGIC) + 1)
    except OSError:
        raise RuntimeError("pg_dump did not create a valid custom-format archive") from None
    if len(header) <= len(_PG_DUMP_MAGIC) or not header.startswith(_PG_DUMP_MAGIC):
        raise RuntimeError("pg_dump did not create a valid custom-format archive")


def run_pg_dump(
    *,
    pg_dump: Path,
    destination: Path,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    snapshot_id: str,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Run one custom-format dump without placing the password in argv or output."""

    if not password or "\x00" in password:
        raise ValueError("PostgreSQL password is invalid")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("PostgreSQL port is invalid")
    connection_fields = (host, database, user, snapshot_id)
    if any(
        not isinstance(value, str) or not value or "\x00" in value for value in connection_fields
    ):
        raise ValueError("PostgreSQL dump configuration is invalid")
    executable = str(Path(pg_dump))
    if not executable or "\x00" in executable:
        raise ValueError("pg_dump executable is invalid")

    output = Path(destination)
    argv = (
        executable,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--no-password",
        f"--host={host}",
        f"--port={port}",
        f"--username={user}",
        f"--dbname={database}",
        f"--snapshot={snapshot_id}",
        f"--file={output}",
    )
    try:
        result = runner(
            argv,
            env=_pg_environment(password),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("pg_dump could not be executed") from None
    return_code = getattr(result, "returncode", None)
    if not isinstance(return_code, int) or return_code != 0:
        safe_code = return_code if isinstance(return_code, int) else "unknown"
        raise RuntimeError(f"pg_dump failed with exit code {safe_code}")
    _require_custom_pg_dump(output)


@dataclass(frozen=True, slots=True)
class _RuntimeConfig:
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    object_store_endpoint: str
    object_store_namespace_id: str
    object_store_bucket: str
    object_store_region: str


def _load_runtime_config(path: Path) -> _RuntimeConfig:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("backup config is unavailable")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("backup config is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("backup config is invalid")
    if payload.get("enabled") is not True or payload.get("object_store_mode") != "s3":
        raise ValueError("backup requires an enabled S3 platform config")
    if payload.get("database_url") not in (None, ""):
        raise ValueError("backup config must keep the database password in a secret file")
    fields = (
        "database_host",
        "database_name",
        "database_user",
        "object_store_endpoint",
        "object_store_namespace_id",
        "object_store_bucket",
        "object_store_region",
    )
    if any(
        not isinstance(payload.get(name), str)
        or not payload[name].strip()
        or "\x00" in payload[name]
        for name in fields
    ):
        raise ValueError("backup config is invalid")
    port = payload.get("database_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("backup config is invalid")
    endpoint = payload["object_store_endpoint"]
    parsed_endpoint = urlsplit(endpoint)
    if (
        parsed_endpoint.scheme not in {"http", "https"}
        or not parsed_endpoint.netloc
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.path not in {"", "/"}
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise ValueError("backup config is invalid")
    try:
        object_store_identity_sha256(
            payload["object_store_namespace_id"],
            payload["object_store_bucket"],
        )
    except ValueError:
        raise ValueError("backup config is invalid") from None
    return _RuntimeConfig(
        database_host=payload["database_host"],
        database_port=port,
        database_name=payload["database_name"],
        database_user=payload["database_user"],
        object_store_endpoint=endpoint,
        object_store_namespace_id=payload["object_store_namespace_id"],
        object_store_bucket=payload["object_store_bucket"],
        object_store_region=payload["object_store_region"],
    )


def _read_secret(root: Path, name: str) -> str:
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"backup secret {name} is unavailable")
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError):
        raise ValueError(f"backup secret {name} could not be read") from None
    if not value or "\x00" in value:
        raise ValueError(f"backup secret {name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class OperatorBackupSecrets:
    database_password: str = field(repr=False)
    database_migration_password: str = field(repr=False)
    minio_access_key: str = field(repr=False)
    minio_secret_key: str = field(repr=False)


def load_operator_backup_config(path: Path) -> _RuntimeConfig:
    """Load the shared operator config contract used by backup and restore."""

    return _load_runtime_config(path)


def load_operator_backup_secrets(
    secret_dir: Path,
    config: _RuntimeConfig,
) -> OperatorBackupSecrets:
    """Read the exact operator secrets without exposing their values."""

    if not isinstance(config, _RuntimeConfig):
        raise ValueError("backup config is invalid")
    root = Path(secret_dir)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("backup secret directory is unavailable")
    try:
        root = root.resolve(strict=True)
    except OSError:
        raise ValueError("backup secret directory is unavailable") from None
    return OperatorBackupSecrets(
        database_password=_read_secret(root, "platform_database_app_password"),
        database_migration_password=_read_secret(
            root,
            "platform_database_migration_password",
        ),
        minio_access_key=_read_secret(root, "minio_bootstrap_access_key"),
        minio_secret_key=_read_secret(root, "minio_bootstrap_secret_key"),
    )


@dataclass(frozen=True, slots=True, order=True)
class ObjectInventoryEntry:
    tenant_id: str
    key: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _require_object_identity(self.tenant_id, self.key)
        _require_digest(self.sha256, "object sha256")
        _require_count(self.size, "object size")


@dataclass(frozen=True, slots=True)
class DatabaseObjectReference:
    tenant_id: str
    key: str
    sha256: str
    size: int | None = None
    version_id: str | None = None
    content_type: str | None = None
    owner_token: str | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        _require_object_identity(self.tenant_id, self.key)
        _require_digest(self.sha256, "database object sha256")
        if self.size is not None:
            _require_count(self.size, "database object size")
        if self.version_id is not None:
            _require_version_id(self.version_id)
        if self.content_type is not None:
            _require_content_type(self.content_type)
        if self.owner_token is not None:
            _require_owner_token(self.owner_token)
        if self.source_revision is not None:
            _require_source_revision(self.source_revision)


@dataclass(frozen=True, slots=True, order=True)
class VersionedObject:
    tenant_id: str
    key: str
    version_id: str
    content_type: str
    owner_token: str
    source_revision: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _require_object_identity(self.tenant_id, self.key)
        _require_version_id(self.version_id)
        _require_content_type(self.content_type)
        _require_owner_token(self.owner_token)
        _require_source_revision(self.source_revision)
        _require_digest(self.sha256, "object metadata sha256")
        _require_count(self.size, "object metadata size")


class MinioVersionedObjectStore:
    """Version-pinned enumeration and streaming reads over a MinIO client."""

    def __init__(self, client: Any, *, bucket: str) -> None:
        if not isinstance(bucket, str) or not bucket or "\x00" in bucket:
            raise ValueError("object store bucket is invalid")
        self._client = client
        self._bucket = bucket

    def _enumerate_object_versions(self) -> tuple[VersionedObject, ...]:
        versioning = self._client.get_bucket_versioning(self._bucket)
        if getattr(versioning, "status", None) != "Enabled":
            raise ValueError("object store bucket versioning is not enabled")
        latest_keys: set[str] = set()
        versions: list[VersionedObject] = []
        records = self._client.list_objects(
            self._bucket,
            prefix="tenants/",
            recursive=True,
            include_version=True,
        )
        for record in records:
            if not _require_minio_boolean(getattr(record, "is_latest", None), "is_latest"):
                continue
            key = getattr(record, "object_name", None)
            if not isinstance(key, str) or not key:
                raise ValueError("MinIO returned an invalid object key")
            if key in latest_keys:
                raise ValueError("MinIO returned duplicate latest object versions")
            latest_keys.add(key)
            if getattr(record, "is_delete_marker", False) is True:
                continue
            parts = key.split("/")
            if len(parts) < 3 or parts[0] != "tenants":
                raise ValueError("MinIO object is outside the tenant prefix")
            version_id = getattr(record, "version_id", None)
            try:
                stat = self._client.stat_object(
                    self._bucket,
                    key,
                    version_id=version_id,
                )
            except Exception:
                raise ValueError("MinIO object metadata could not be read") from None
            metadata = getattr(stat, "metadata", None)
            if not hasattr(metadata, "items"):
                raise ValueError("MinIO object metadata is invalid")
            normalized_metadata = {str(name).lower(): value for name, value in metadata.items()}
            owner_token = normalized_metadata.get("x-amz-meta-owner")
            metadata_sha256 = normalized_metadata.get("x-amz-meta-sha256")
            content_type = getattr(stat, "content_type", None) or normalized_metadata.get(
                "content-type"
            )
            source_revision = _canonical_http_etag(getattr(stat, "etag", None))
            stat_version_id = getattr(stat, "version_id", None)
            stat_size = getattr(stat, "size", None)
            if stat_version_id != version_id:
                raise ValueError("MinIO object metadata version does not match enumeration")
            versions.append(
                VersionedObject(
                    tenant_id=parts[1],
                    key=key,
                    version_id=version_id,
                    content_type=content_type,
                    owner_token=owner_token,
                    source_revision=source_revision,
                    sha256=metadata_sha256,
                    size=stat_size,
                )
            )
        return tuple(sorted(versions))

    async def enumerate_object_versions(self) -> tuple[VersionedObject, ...]:
        return await _await_owned_operation(asyncio.to_thread(self._enumerate_object_versions))

    def _read_object_version(self, source: VersionedObject, destination: Path) -> None:
        target = Path(destination)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise ValueError("object payload destination is unsafe")
            _set_private_mode(target, _PRIVATE_FILE_MODE)
        else:
            _write_private_new_file(target)
        response = self._client.get_object(
            self._bucket,
            source.key,
            version_id=source.version_id,
        )
        try:
            with target.open("r+b") as handle:
                handle.truncate(0)
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
            _set_private_mode(target, _PRIVATE_FILE_MODE)
        finally:
            primary_failure = sys.exception()
            cleanup_failure: BaseException | None = None
            for cleanup, failure_note in (
                (response.close, "object response close cleanup failed"),
                (response.release_conn, "object response release cleanup failed"),
            ):
                try:
                    cleanup()
                except BaseException as failure:
                    if primary_failure is not None:
                        primary_failure.add_note(failure_note)
                    elif cleanup_failure is None:
                        cleanup_failure = failure
                    else:
                        cleanup_failure.add_note(failure_note)
            if primary_failure is None and cleanup_failure is not None:
                raise cleanup_failure

    async def read_object_version(self, source: VersionedObject, destination: Path) -> None:
        await _await_owned_operation(
            asyncio.to_thread(self._read_object_version, source, Path(destination))
        )


@dataclass(frozen=True, slots=True, order=True)
class RestorableObjectInventoryEntry(ObjectInventoryEntry):
    version_id: str
    payload_file: str
    content_type: str
    owner_token: str
    source_revision: str

    def __post_init__(self) -> None:
        ObjectInventoryEntry.__post_init__(self)
        _require_version_id(self.version_id)
        if self.payload_file != _payload_file_for_key(self.key):
            raise ValueError("object payload file is invalid")
        _require_content_type(self.content_type)
        _require_owner_token(self.owner_token)
        _require_source_revision(self.source_revision)


@dataclass(frozen=True, slots=True)
class DatabaseBackup:
    file: str
    sha256: str
    size: int
    identity_sha256: str

    def __post_init__(self) -> None:
        if not self.file or Path(self.file).name != self.file:
            raise ValueError("database backup file is invalid")
        _require_digest(self.sha256, "database sha256")
        _require_digest(self.identity_sha256, "database identity sha256")
        _require_count(self.size, "database size")


@dataclass(frozen=True, slots=True)
class BackupManifest:
    schema_version: int
    created_at: datetime
    database: DatabaseBackup
    object_inventory_file: str
    object_inventory_sha256: str
    object_count: int
    source_object_store_identity_sha256: str
    platform_schema_revision: str
    schema_revisions: dict[str, str]
    classroom_versions_count: int
    learning_events_count: int

    def __post_init__(self) -> None:
        if self.schema_version != _BACKUP_MANIFEST_SCHEMA_VERSION:
            raise ValueError("backup manifest schema version is unsupported")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("backup manifest time must be timezone-aware")
        if Path(self.object_inventory_file).name != self.object_inventory_file:
            raise ValueError("object inventory file is invalid")
        _require_digest(self.object_inventory_sha256, "object inventory sha256")
        _require_digest(
            self.source_object_store_identity_sha256,
            "source object store identity sha256",
        )
        _require_count(self.object_count, "object count")
        _require_count(self.classroom_versions_count, "classroom versions count")
        _require_count(self.learning_events_count, "learning events count")
        if not self.platform_schema_revision:
            raise ValueError("platform schema revision is invalid")
        for tenant_id, revision in self.schema_revisions.items():
            if not tenant_id or not revision:
                raise ValueError("schema revision entry is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "createdAt": self.created_at.isoformat(),
            "database": {
                "file": self.database.file,
                "sha256": self.database.sha256,
                "size": self.database.size,
                "identitySha256": self.database.identity_sha256,
            },
            "objectInventoryFile": self.object_inventory_file,
            "objectInventorySha256": self.object_inventory_sha256,
            "objectCount": self.object_count,
            "sourceObjectStoreIdentitySha256": self.source_object_store_identity_sha256,
            "platformSchemaRevision": self.platform_schema_revision,
            "schemaRevisions": dict(sorted(self.schema_revisions.items())),
            "classroomVersionsCount": self.classroom_versions_count,
            "learningEventsCount": self.learning_events_count,
        }


@dataclass(frozen=True, slots=True)
class TeachingBackupFacts:
    database_identity_sha256: str
    platform_schema_revision: str
    schema_revisions: dict[str, str]
    classroom_versions_count: int
    learning_events_count: int
    database_object_references: tuple[DatabaseObjectReference, ...] = ()

    @property
    def referenced_object_keys(self) -> tuple[str, ...]:
        return tuple(reference.key for reference in self.database_object_references)

    def __post_init__(self) -> None:
        _require_digest(self.database_identity_sha256, "database identity sha256")
        if not self.platform_schema_revision:
            raise ValueError("platform schema revision is invalid")
        _require_count(self.classroom_versions_count, "classroom versions count")
        _require_count(self.learning_events_count, "learning events count")
        for tenant_id, revision in self.schema_revisions.items():
            if not tenant_id or not revision:
                raise ValueError("schema revision entry is invalid")
        if len(set(self.referenced_object_keys)) != len(self.database_object_references):
            raise ValueError("database object references contain duplicate keys")
        for reference in self.database_object_references:
            parts = reference.key.split("/")
            if parts[1] not in self.schema_revisions or parts[1] != reference.tenant_id:
                raise ValueError("referenced object key is outside the database tenant inventory")


_DATABASE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


async def _dump_postgres_snapshot(
    destination: Path,
    *,
    config: _RuntimeConfig,
    password: str,
    pg_dump: Path,
    connect: Callable[..., Awaitable[Any]] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> TeachingBackupFacts:
    """Use one exported snapshot for the dump and its database-side facts."""

    if connect is None:
        import asyncpg

        connect = asyncpg.connect
    connection = await connect(
        host=config.database_host,
        port=config.database_port,
        database=config.database_name,
        user=config.database_user,
        password=password,
    )
    transaction = connection.transaction(isolation="repeatable_read", readonly=True)
    started = False
    finished = False
    try:
        await transaction.start()
        started = True
        snapshot_id = await connection.fetchval("SELECT pg_export_snapshot()")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("PostgreSQL exported snapshot is unavailable")
        database_oid = await connection.fetchval(
            "SELECT oid::text FROM pg_database WHERE datname = current_database()"
        )
        if not isinstance(database_oid, str) or not database_oid:
            raise ValueError("PostgreSQL database identity is unavailable")
        system_identifier = await connection.fetchval(
            "SELECT system_identifier::text FROM pg_control_system()"
        )
        if not isinstance(system_identifier, str) or not system_identifier:
            raise ValueError("PostgreSQL system identity is unavailable")
        platform_schema_revision = await connection.fetchval(
            "SELECT version_num FROM platform.alembic_version"
        )
        if not isinstance(platform_schema_revision, str) or not platform_schema_revision:
            raise ValueError("PostgreSQL platform schema revision is unavailable")
        rows = await connection.fetch(
            "SELECT tenant_id::text AS tenant_id, schema_name, revision, status "
            "FROM platform.tenant_schema_states ORDER BY tenant_id"
        )
        revisions: dict[str, str] = {}
        database_object_references: dict[str, DatabaseObjectReference] = {}
        classroom_versions_count = 0
        learning_events_count = 0
        for row in rows:
            tenant_id = row["tenant_id"]
            schema_name = row["schema_name"]
            revision = row["revision"]
            status = row["status"]
            if (
                not isinstance(tenant_id, str)
                or _TENANT_ID.fullmatch(tenant_id) is None
                or not isinstance(schema_name, str)
                or _DATABASE_SCHEMA.fullmatch(schema_name) is None
                or not isinstance(status, str)
            ):
                raise ValueError("PostgreSQL tenant schema inventory is invalid")
            if status != "active":
                continue
            if not isinstance(revision, str) or not revision:
                raise ValueError("PostgreSQL active tenant schema revision is invalid")
            if tenant_id in revisions:
                raise ValueError("PostgreSQL tenant schema inventory contains duplicates")
            quoted_schema = '"' + schema_name.replace('"', '""') + '"'
            actual_revision = await connection.fetchval(
                "SELECT CASE WHEN COUNT(*) = 1 THEN MIN(version_num) END "
                f"FROM {quoted_schema}.alembic_version"
            )
            if (
                not isinstance(actual_revision, str)
                or not actual_revision
                or actual_revision != revision
            ):
                raise ValueError("PostgreSQL tenant schema revision drift detected")
            revisions[tenant_id] = revision
            version_count = await connection.fetchval(
                f"SELECT COUNT(*) FROM {quoted_schema}.classroom_versions"
            )
            event_count = await connection.fetchval(
                f"SELECT COUNT(*) FROM {quoted_schema}.learning_events"
            )
            classroom_versions_count += _require_count(
                version_count,
                "classroom versions count",
            )
            learning_events_count += _require_count(
                event_count,
                "learning events count",
            )
            object_rows = await connection.fetch(
                f"SELECT object_key::text AS object_key, sha256::text AS sha256, "
                "size_bytes::bigint AS size_bytes, "
                "object_version_id::text AS version_id, "
                "NULL::text AS content_type, ownership_token::text AS owner_token, "
                "object_revision::text AS source_revision "
                f"FROM {quoted_schema}.source_uploads "
                "WHERE object_key IS NOT NULL "
                "UNION ALL SELECT document_object_key::text AS object_key, "
                "document_sha256::text AS sha256, NULL::bigint AS size_bytes, "
                "NULL::text AS version_id, NULL::text AS content_type, "
                "NULL::text AS owner_token, NULL::text AS source_revision "
                f"FROM {quoted_schema}.classroom_versions "
                "WHERE document_object_key IS NOT NULL "
                "UNION ALL SELECT object_key::text AS object_key, sha256::text AS sha256, "
                "size_bytes::bigint AS size_bytes, NULL::text AS version_id, "
                "mime_type::text AS content_type, NULL::text AS owner_token, "
                "NULL::text AS source_revision "
                f"FROM {quoted_schema}.classroom_artifacts "
                "WHERE object_key IS NOT NULL "
                "UNION ALL SELECT object_key::text AS object_key, sha256::text AS sha256, "
                "size_bytes::bigint AS size_bytes, NULL::text AS version_id, "
                "mime_type::text AS content_type, ownership_token::text AS owner_token, "
                "object_revision::text AS source_revision "
                f"FROM {quoted_schema}.classroom_draft_media "
                "WHERE object_key IS NOT NULL "
                "UNION ALL SELECT input_manifest_object_key::text AS object_key, "
                "input_manifest_sha256::text AS sha256, NULL::bigint AS size_bytes, "
                "NULL::text AS version_id, NULL::text AS content_type, "
                "NULL::text AS owner_token, NULL::text AS source_revision "
                f"FROM {quoted_schema}.classroom_exports "
                "WHERE input_manifest_object_key IS NOT NULL "
                "UNION ALL SELECT object_key::text AS object_key, sha256::text AS sha256, "
                "size_bytes::bigint AS size_bytes, NULL::text AS version_id, "
                "mime_type::text AS content_type, NULL::text AS owner_token, "
                "NULL::text AS source_revision "
                f"FROM {quoted_schema}.classroom_exports "
                "WHERE object_key IS NOT NULL"
            )
            for object_row in object_rows:
                object_key = object_row["object_key"]
                reference = DatabaseObjectReference(
                    tenant_id=tenant_id,
                    key=object_key,
                    sha256=object_row["sha256"],
                    size=object_row["size_bytes"],
                    version_id=object_row["version_id"],
                    content_type=object_row["content_type"],
                    owner_token=object_row["owner_token"],
                    source_revision=object_row["source_revision"],
                )
                existing = database_object_references.get(object_key)
                if existing is not None:
                    if (
                        existing.sha256 != reference.sha256
                        or (
                            existing.size is not None
                            and reference.size is not None
                            and existing.size != reference.size
                        )
                        or (
                            existing.version_id is not None
                            and reference.version_id is not None
                            and existing.version_id != reference.version_id
                        )
                        or (
                            existing.content_type is not None
                            and reference.content_type is not None
                            and existing.content_type != reference.content_type
                        )
                        or (
                            existing.owner_token is not None
                            and reference.owner_token is not None
                            and existing.owner_token != reference.owner_token
                        )
                        or (
                            existing.source_revision is not None
                            and reference.source_revision is not None
                            and existing.source_revision != reference.source_revision
                        )
                    ):
                        raise ValueError("database object references contain conflicting receipts")
                    reference = DatabaseObjectReference(
                        tenant_id=tenant_id,
                        key=object_key,
                        sha256=reference.sha256,
                        size=existing.size if existing.size is not None else reference.size,
                        version_id=(
                            existing.version_id
                            if existing.version_id is not None
                            else reference.version_id
                        ),
                        content_type=(
                            existing.content_type
                            if existing.content_type is not None
                            else reference.content_type
                        ),
                        owner_token=(
                            existing.owner_token
                            if existing.owner_token is not None
                            else reference.owner_token
                        ),
                        source_revision=(
                            existing.source_revision
                            if existing.source_revision is not None
                            else reference.source_revision
                        ),
                    )
                database_object_references[object_key] = reference
        identity_payload = {
            "databaseName": config.database_name,
            "databaseOid": database_oid,
            "systemIdentifier": system_identifier,
        }
        facts = TeachingBackupFacts(
            database_identity_sha256=hashlib.sha256(_canonical_json(identity_payload)).hexdigest(),
            platform_schema_revision=platform_schema_revision,
            schema_revisions=revisions,
            classroom_versions_count=classroom_versions_count,
            learning_events_count=learning_events_count,
            database_object_references=tuple(
                database_object_references[key] for key in sorted(database_object_references)
            ),
        )
        await _await_owned_operation(
            asyncio.to_thread(
                run_pg_dump,
                pg_dump=pg_dump,
                destination=Path(destination),
                host=config.database_host,
                port=config.database_port,
                database=config.database_name,
                user=config.database_user,
                password=password,
                snapshot_id=snapshot_id,
                runner=runner,
            )
        )
        dump = Path(destination)
        if dump.is_symlink() or not dump.is_file():
            raise ValueError("pg_dump did not create a regular database dump")
        await transaction.commit()
        finished = True
        return facts
    finally:
        primary_failure = sys.exception()
        if started and not finished:
            try:
                await _await_owned_cleanup(transaction.rollback())
            except BaseException:
                if primary_failure is not None:
                    primary_failure.add_note("PostgreSQL snapshot rollback cleanup failed")
                else:
                    raise
        try:
            await _await_owned_cleanup(connection.close())
        except BaseException:
            if primary_failure is not None:
                primary_failure.add_note("PostgreSQL snapshot connection cleanup failed")
            else:
                raise


@dataclass(frozen=True, slots=True)
class VerifiedTeachingBackup:
    directory: Path
    database_dump: Path
    manifest: BackupManifest
    object_inventory: tuple[ObjectInventoryEntry, ...]
    object_payloads: tuple[Path, ...] = ()
    manifest_sha256: str = ""
    object_inventory_sha256: str = ""
    database_sha256: str = ""
    object_payload_sha256s: tuple[str, ...] = ()
    archive_fingerprint_sha256: str = ""


def _archive_fingerprint(
    *,
    manifest_sha256: str,
    object_inventory_sha256: str,
    database_sha256: str,
    object_payload_sha256s: tuple[str, ...],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "manifestSha256": manifest_sha256,
                "objectInventorySha256": object_inventory_sha256,
                "databaseSha256": database_sha256,
                "objectPayloadSha256s": list(object_payload_sha256s),
            }
        )
    ).hexdigest()


def _inventory_payload(entries: Iterable[ObjectInventoryEntry]) -> list[dict[str, object]]:
    ordered = tuple(sorted(entries, key=lambda entry: (entry.tenant_id, entry.key)))
    keys = tuple((entry.tenant_id, entry.key) for entry in ordered)
    if len(set(keys)) != len(keys):
        raise ValueError("object inventory contains a duplicate object key")
    return [asdict(entry) for entry in ordered]


def inventory_sha256(entries: Iterable[ObjectInventoryEntry]) -> str:
    return hashlib.sha256(_canonical_json(_inventory_payload(entries))).hexdigest()


def write_backup_manifest(
    output_dir: Path,
    *,
    database_dump: Path,
    database_identity_sha256: str,
    object_inventory: Iterable[ObjectInventoryEntry],
    source_object_store_namespace_id: str,
    source_object_store_bucket: str,
    platform_schema_revision: str,
    schema_revisions: dict[str, str],
    classroom_versions_count: int,
    learning_events_count: int,
    created_at: datetime,
    source_object_store_identity_sha256: str | None = None,
) -> BackupManifest:
    requested_output = Path(output_dir)
    if requested_output.is_symlink() or not requested_output.is_dir():
        raise ValueError("backup output directory is unsafe")
    requested_dump = Path(database_dump)
    if requested_dump.is_symlink() or not requested_dump.is_file():
        raise ValueError("database dump must be a regular file in the backup directory")
    try:
        output = requested_output.resolve(strict=True)
        dump = requested_dump.resolve(strict=True)
    except OSError:
        raise ValueError("backup inputs are unavailable") from None
    if dump.parent != output:
        raise ValueError("database dump must be a regular file in the backup directory")
    _set_private_mode(output, _PRIVATE_DIRECTORY_MODE)
    _set_private_mode(dump, _PRIVATE_FILE_MODE)

    inventory_path = output / "objects.json"
    manifest_path = output / "manifest.json"
    checksum_path = output / "manifest.sha256"
    for target in (inventory_path, manifest_path, checksum_path):
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)

    entries = tuple(sorted(object_inventory, key=lambda entry: (entry.tenant_id, entry.key)))
    inventory_bytes = _canonical_json(_inventory_payload(entries))
    database_sha256, database_size = _digest_file(dump)
    manifest = BackupManifest(
        schema_version=_BACKUP_MANIFEST_SCHEMA_VERSION,
        created_at=created_at,
        database=DatabaseBackup(
            file=dump.name,
            sha256=database_sha256,
            size=database_size,
            identity_sha256=_require_digest(
                database_identity_sha256,
                "database identity sha256",
            ),
        ),
        object_inventory_file=inventory_path.name,
        object_inventory_sha256=hashlib.sha256(inventory_bytes).hexdigest(),
        object_count=len(entries),
        source_object_store_identity_sha256=(
            object_store_identity_sha256(
                source_object_store_namespace_id,
                source_object_store_bucket,
            )
            if source_object_store_identity_sha256 is None
            else _require_digest(
                source_object_store_identity_sha256,
                "source object store identity sha256",
            )
        ),
        platform_schema_revision=platform_schema_revision,
        schema_revisions=dict(sorted(schema_revisions.items())),
        classroom_versions_count=classroom_versions_count,
        learning_events_count=learning_events_count,
    )
    manifest_bytes = _canonical_json(manifest.to_payload())
    checksum_bytes = _canonical_json({"manifestSha256": hashlib.sha256(manifest_bytes).hexdigest()})

    _write_private_new_file(inventory_path, inventory_bytes)
    _write_private_new_file(manifest_path, manifest_bytes)
    _write_private_new_file(checksum_path, checksum_bytes)
    return manifest


async def create_teaching_backup(
    output_dir: Path,
    *,
    dump_database: Callable[[Path], Awaitable[TeachingBackupFacts]],
    inventory_objects: Callable[[], Awaitable[Iterable[ObjectInventoryEntry]]],
    source_object_store_namespace_id: str,
    source_object_store_bucket: str,
    created_at: datetime | None = None,
) -> BackupManifest:
    """Create one new backup without inspecting or deleting sibling backups."""

    requested = Path(output_dir)
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError:
        raise ValueError("backup output parent is unavailable") from None
    output = parent / requested.name
    if requested.name in {"", ".", ".."} or output.exists() or output.is_symlink():
        raise FileExistsError(output)
    _create_private_directory(output)

    database_dump = output / "database.dump"
    _write_private_new_file(database_dump)
    facts = await dump_database(database_dump)
    if database_dump.is_symlink() or not database_dump.is_file():
        raise ValueError("database dump was not created")
    _set_private_mode(database_dump, _PRIVATE_FILE_MODE)
    inventory = tuple(await inventory_objects())
    return write_backup_manifest(
        output,
        database_dump=database_dump,
        database_identity_sha256=facts.database_identity_sha256,
        object_inventory=inventory,
        source_object_store_namespace_id=source_object_store_namespace_id,
        source_object_store_bucket=source_object_store_bucket,
        platform_schema_revision=facts.platform_schema_revision,
        schema_revisions=facts.schema_revisions,
        classroom_versions_count=facts.classroom_versions_count,
        learning_events_count=facts.learning_events_count,
        created_at=created_at or datetime.now(timezone.utc),
    )


async def create_restorable_teaching_backup(
    output_dir: Path,
    *,
    dump_database: Callable[[Path], Awaitable[TeachingBackupFacts]],
    enumerate_object_versions: Callable[[], Awaitable[Iterable[VersionedObject]]],
    read_object_version: Callable[[VersionedObject, Path], Awaitable[None]],
    observe_source_object_store_owner_id: Callable[[], Awaitable[str]],
    source_object_store_endpoint: str,
    source_object_store_region: str,
    source_object_store_namespace_id: str,
    source_object_store_bucket: str,
    created_at: datetime | None = None,
) -> BackupManifest:
    """Stage a database dump and version-pinned object bytes, then publish once.

    PostgreSQL and the object store are captured independently. This function does
    not claim a transactionally atomic snapshot across those two systems.
    """

    requested = Path(output_dir)
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError:
        raise ValueError("backup output parent is unavailable") from None
    output = parent / requested.name
    if requested.name in {"", ".", ".."} or requested.name.endswith(".partial"):
        raise ValueError("backup output directory name is invalid")
    staging = output.with_name(f"{output.name}.partial")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if staging.exists() or staging.is_symlink():
        raise FileExistsError(staging)
    try:
        owner_id = _require_object_store_owner_id(await observe_source_object_store_owner_id())
        source_object_store_identity_sha256 = physical_object_store_identity_sha256(
            source_object_store_endpoint,
            source_object_store_region,
            source_object_store_bucket,
            hashlib.sha256(owner_id.encode("utf-8")).hexdigest(),
        )
        object_store_identity_sha256(
            source_object_store_namespace_id,
            source_object_store_bucket,
        )
    except ValueError:
        raise ValueError("source object store identity is invalid") from None
    _create_private_directory(staging)

    database_dump = staging / "database.dump"
    _write_private_new_file(database_dump)
    facts = await dump_database(database_dump)
    if database_dump.is_symlink() or not database_dump.is_file():
        raise ValueError("database dump was not created")
    _set_private_mode(database_dump, _PRIVATE_FILE_MODE)

    enumerated = tuple(await enumerate_object_versions())
    if any(not isinstance(item, VersionedObject) for item in enumerated):
        raise ValueError("object enumeration entry is invalid")
    sources = tuple(sorted(enumerated, key=lambda item: (item.tenant_id, item.key)))
    source_keys = tuple((item.tenant_id, item.key) for item in sources)
    if len(set(source_keys)) != len(source_keys):
        raise ValueError("object enumeration contains a duplicate object key")
    database_tenants = set(facts.schema_revisions)
    if any(source.tenant_id not in database_tenants for source in sources):
        raise ValueError("object enumeration contains a tenant outside the database snapshot")
    enumerated_keys = {source.key for source in sources}
    missing_references = set(facts.referenced_object_keys).difference(enumerated_keys)
    if missing_references:
        raise ValueError("object enumeration is missing database-referenced object keys")

    payload_root = staging / "objects"
    _create_private_directory(payload_root)
    inventory: list[RestorableObjectInventoryEntry] = []
    payload_paths: list[Path] = []
    for source in sources:
        payload_file = _payload_file_for_key(source.key)
        destination = staging.joinpath(*payload_file.split("/"))
        await read_object_version(source, destination)
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("versioned object payload was not created")
        if payload_root.is_symlink() or destination.resolve(strict=True).parent != payload_root:
            raise ValueError("versioned object payload is unsafe")
        _set_private_mode(destination, _PRIVATE_FILE_MODE)
        payload_sha256, payload_size = _digest_file(destination)
        if payload_sha256 != source.sha256 or payload_size != source.size:
            raise ValueError("object metadata does not match versioned payload")
        payload_paths.append(destination)
        inventory.append(
            RestorableObjectInventoryEntry(
                tenant_id=source.tenant_id,
                key=source.key,
                sha256=payload_sha256,
                size=payload_size,
                version_id=source.version_id,
                payload_file=payload_file,
                content_type=source.content_type,
                owner_token=source.owner_token,
                source_revision=source.source_revision,
            )
        )
    inventory_by_key = {entry.key: entry for entry in inventory}
    for reference in facts.database_object_references:
        entry = inventory_by_key[reference.key]
        if (
            entry.sha256 != reference.sha256
            or (reference.size is not None and entry.size != reference.size)
            or (reference.version_id is not None and entry.version_id != reference.version_id)
            or (reference.content_type is not None and entry.content_type != reference.content_type)
            or (reference.owner_token is not None and entry.owner_token != reference.owner_token)
            or (
                reference.source_revision is not None
                and entry.source_revision != reference.source_revision
            )
        ):
            raise ValueError("database object receipt does not match versioned payload")

    try:
        final_owner_id = _require_object_store_owner_id(
            await observe_source_object_store_owner_id()
        )
        final_source_object_store_identity_sha256 = physical_object_store_identity_sha256(
            source_object_store_endpoint,
            source_object_store_region,
            source_object_store_bucket,
            hashlib.sha256(final_owner_id.encode("utf-8")).hexdigest(),
        )
    except ValueError:
        raise ValueError("source object store identity changed during capture") from None
    if final_source_object_store_identity_sha256 != source_object_store_identity_sha256:
        raise ValueError("source object store identity changed during capture")

    manifest = write_backup_manifest(
        staging,
        database_dump=database_dump,
        database_identity_sha256=facts.database_identity_sha256,
        object_inventory=inventory,
        source_object_store_namespace_id=source_object_store_namespace_id,
        source_object_store_bucket=source_object_store_bucket,
        platform_schema_revision=facts.platform_schema_revision,
        schema_revisions=facts.schema_revisions,
        classroom_versions_count=facts.classroom_versions_count,
        learning_events_count=facts.learning_events_count,
        created_at=created_at or datetime.now(timezone.utc),
        source_object_store_identity_sha256=source_object_store_identity_sha256,
    )
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    for archive_file in (
        database_dump,
        *payload_paths,
        staging / manifest.object_inventory_file,
        staging / "manifest.json",
        staging / "manifest.sha256",
    ):
        _fsync_file(archive_file)
    _fsync_directory(payload_root)
    _fsync_directory(staging)
    _publish_directory_no_replace(staging, output)
    _fsync_directory(parent)
    return manifest


def _parse_backup_manifest(manifest_bytes: bytes) -> BackupManifest:
    try:
        payload = json.loads(manifest_bytes)
        database = payload["database"]
        return BackupManifest(
            schema_version=payload["schemaVersion"],
            created_at=datetime.fromisoformat(payload["createdAt"]),
            database=DatabaseBackup(
                file=database["file"],
                sha256=database["sha256"],
                size=database["size"],
                identity_sha256=database["identitySha256"],
            ),
            object_inventory_file=payload["objectInventoryFile"],
            object_inventory_sha256=payload["objectInventorySha256"],
            object_count=payload["objectCount"],
            source_object_store_identity_sha256=payload["sourceObjectStoreIdentitySha256"],
            platform_schema_revision=payload["platformSchemaRevision"],
            schema_revisions=dict(payload["schemaRevisions"]),
            classroom_versions_count=payload["classroomVersionsCount"],
            learning_events_count=payload["learningEventsCount"],
        )
    except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("backup manifest is invalid") from None


def load_backup_manifest(path: Path) -> BackupManifest:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("backup manifest is unavailable")
    try:
        manifest_bytes = source.read_bytes()
    except OSError:
        raise ValueError("backup manifest is unavailable") from None
    return _parse_backup_manifest(manifest_bytes)


def _parse_object_inventory(inventory_bytes: bytes) -> tuple[ObjectInventoryEntry, ...]:
    try:
        payload = json.loads(inventory_bytes)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("object inventory is invalid") from None
    if not isinstance(payload, list):
        raise ValueError("object inventory is invalid")
    entries: list[ObjectInventoryEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("object inventory is invalid")
        fields = set(item)
        legacy_fields = {"tenant_id", "key", "sha256", "size"}
        restorable_fields = legacy_fields | {
            "version_id",
            "payload_file",
            "content_type",
            "owner_token",
            "source_revision",
        }
        try:
            if fields == legacy_fields:
                entries.append(ObjectInventoryEntry(**item))
            elif fields == restorable_fields:
                entries.append(RestorableObjectInventoryEntry(**item))
            else:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("object inventory is invalid") from None
    inventory = tuple(entries)
    _inventory_payload(inventory)
    return inventory


def _load_object_inventory(path: Path) -> tuple[ObjectInventoryEntry, ...]:
    try:
        inventory_bytes = Path(path).read_bytes()
    except OSError:
        raise ValueError("object inventory is invalid") from None
    return _parse_object_inventory(inventory_bytes)


def load_verified_backup(directory: Path) -> VerifiedTeachingBackup:
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("backup directory is unavailable")
    try:
        root = root.resolve(strict=True)
    except OSError:
        raise ValueError("backup directory is unavailable") from None
    if root.name.endswith(".partial"):
        raise ValueError("backup directory is incomplete")

    manifest_path = root / "manifest.json"
    checksum_path = root / "manifest.sha256"
    if manifest_path.is_symlink() or checksum_path.is_symlink():
        raise ValueError("backup manifest is unsafe")
    try:
        manifest_bytes = manifest_path.read_bytes()
        checksum_payload = json.loads(checksum_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("backup manifest checksum is invalid") from None
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        not isinstance(checksum_payload, dict)
        or set(checksum_payload) != {"manifestSha256"}
        or checksum_payload["manifestSha256"] != manifest_sha256
    ):
        raise ValueError("backup manifest checksum is invalid")

    manifest = _parse_backup_manifest(manifest_bytes)
    inventory_path = root / manifest.object_inventory_file
    if inventory_path.is_symlink() or not inventory_path.is_file():
        raise ValueError("object inventory is unavailable")
    inventory_bytes = inventory_path.read_bytes()
    object_inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
    if object_inventory_sha256 != manifest.object_inventory_sha256:
        raise ValueError("object inventory checksum does not match")
    inventory = _parse_object_inventory(inventory_bytes)
    if len(inventory) != manifest.object_count:
        raise ValueError("object inventory count does not match")

    database_dump = root / manifest.database.file
    if database_dump.is_symlink() or not database_dump.is_file():
        raise ValueError("database dump is unavailable")
    database_sha256, database_size = _digest_file(database_dump)
    if database_sha256 != manifest.database.sha256 or database_size != manifest.database.size:
        raise ValueError("database dump checksum does not match")

    restorable_entries = tuple(
        entry for entry in inventory if isinstance(entry, RestorableObjectInventoryEntry)
    )
    object_payloads: list[Path] = []
    object_payload_sha256s: list[str] = []
    if restorable_entries:
        payload_root = root / "objects"
        if payload_root.is_symlink() or not payload_root.is_dir():
            raise ValueError("object payload directory is unavailable")
        try:
            resolved_payload_root = payload_root.resolve(strict=True)
        except OSError:
            raise ValueError("object payload directory is unavailable") from None
        if resolved_payload_root.parent != root:
            raise ValueError("object payload directory is unsafe")
        for entry in restorable_entries:
            payload_path = root.joinpath(*entry.payload_file.split("/"))
            if payload_path.is_symlink() or not payload_path.is_file():
                raise ValueError("object payload is unavailable")
            try:
                resolved_payload = payload_path.resolve(strict=True)
            except OSError:
                raise ValueError("object payload is unavailable") from None
            if resolved_payload.parent != resolved_payload_root:
                raise ValueError("object payload is unsafe")
            payload_sha256, payload_size = _digest_file(resolved_payload)
            if payload_sha256 != entry.sha256 or payload_size != entry.size:
                raise ValueError("object payload checksum does not match")
            object_payloads.append(resolved_payload)
            object_payload_sha256s.append(payload_sha256)
    payload_fingerprints = tuple(object_payload_sha256s)
    return VerifiedTeachingBackup(
        directory=root,
        database_dump=database_dump,
        manifest=manifest,
        object_inventory=inventory,
        object_payloads=tuple(object_payloads),
        manifest_sha256=manifest_sha256,
        object_inventory_sha256=object_inventory_sha256,
        database_sha256=database_sha256,
        object_payload_sha256s=payload_fingerprints,
        archive_fingerprint_sha256=_archive_fingerprint(
            manifest_sha256=manifest_sha256,
            object_inventory_sha256=object_inventory_sha256,
            database_sha256=database_sha256,
            object_payload_sha256s=payload_fingerprints,
        ),
    )


def reverify_verified_backup(backup: VerifiedTeachingBackup) -> VerifiedTeachingBackup:
    """Re-read a verified archive and reject any fingerprint change."""

    if not isinstance(backup, VerifiedTeachingBackup):
        raise ValueError("verified backup is invalid")
    current = load_verified_backup(backup.directory)
    expected = (
        backup.manifest_sha256,
        backup.object_inventory_sha256,
        backup.database_sha256,
        backup.object_payload_sha256s,
        backup.archive_fingerprint_sha256,
    )
    actual = (
        current.manifest_sha256,
        current.object_inventory_sha256,
        current.database_sha256,
        current.object_payload_sha256s,
        current.archive_fingerprint_sha256,
    )
    if actual != expected:
        raise ValueError("backup changed after verification")
    return current


async def run_operator_backup(
    *,
    config_path: Path,
    secret_dir: Path,
    output_dir: Path,
    pg_dump: Path,
    created_at: datetime | None = None,
) -> BackupManifest:
    import boto3
    from minio import Minio

    config = load_operator_backup_config(config_path)
    secrets = load_operator_backup_secrets(secret_dir, config)
    endpoint = urlsplit(config.object_store_endpoint)
    client = Minio(
        endpoint.netloc,
        access_key=secrets.minio_access_key,
        secret_key=secrets.minio_secret_key,
        secure=endpoint.scheme == "https",
        region=config.object_store_region,
    )
    object_store = MinioVersionedObjectStore(client, bucket=config.object_store_bucket)
    owner_client = boto3.client(
        "s3",
        endpoint_url=config.object_store_endpoint,
        region_name=config.object_store_region,
        aws_access_key_id=secrets.minio_access_key,
        aws_secret_access_key=secrets.minio_secret_key,
    )

    async def dump_database(destination: Path) -> TeachingBackupFacts:
        return await _dump_postgres_snapshot(
            destination,
            config=config,
            password=secrets.database_password,
            pg_dump=pg_dump,
        )

    async def observe_source_object_store_owner_id() -> str:
        try:
            response = await _await_owned_operation(
                asyncio.to_thread(
                    owner_client.get_bucket_acl,
                    Bucket=config.object_store_bucket,
                )
            )
            owner = response["Owner"]
            owner_id = owner["ID"]
        except (KeyError, TypeError, AttributeError):
            raise ValueError("source object store owner is invalid") from None
        except Exception:
            raise RuntimeError("source object store owner could not be observed") from None
        return _require_object_store_owner_id(owner_id)

    try:
        return await create_restorable_teaching_backup(
            output_dir,
            dump_database=dump_database,
            enumerate_object_versions=object_store.enumerate_object_versions,
            read_object_version=object_store.read_object_version,
            observe_source_object_store_owner_id=observe_source_object_store_owner_id,
            source_object_store_endpoint=config.object_store_endpoint,
            source_object_store_region=config.object_store_region,
            source_object_store_namespace_id=config.object_store_namespace_id,
            source_object_store_bucket=config.object_store_bucket,
            created_at=created_at,
        )
    finally:
        primary_failure = sys.exception()
        try:
            await _await_owned_cleanup(asyncio.to_thread(owner_client.close))
        except BaseException:
            if primary_failure is not None:
                primary_failure.add_note("owner client cleanup failed")
            else:
                raise


def _backup_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a restorable teaching PostgreSQL and MinIO backup"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--secret-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pg-dump", required=True, type=Path)
    return parser


def parse_backup_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return _backup_argument_parser().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_backup_arguments(argv)
    try:
        manifest = asyncio.run(
            run_operator_backup(
                config_path=arguments.config,
                secret_dir=arguments.secret_dir,
                output_dir=arguments.output,
                pg_dump=arguments.pg_dump,
            )
        )
    except Exception:
        print("teaching backup failed", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "createdAt": manifest.created_at.isoformat(),
                "objectCount": manifest.object_count,
                "output": str(arguments.output),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
