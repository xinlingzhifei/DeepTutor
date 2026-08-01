"""Tenant-scoped object stores for classroom artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
import hashlib
import hmac
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile
from typing import Any, Mapping, Protocol
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from deeptutor.multi_user.context import get_current_tenant
from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.artifacts import (
    ArtifactManifestEntry,
    ArtifactManifestError,
    ClassroomArtifactManifest,
    StoredArtifact,
    classroom_artifact_key,
    temporary_artifact_key,
    tenant_artifact_prefix,
)
from deeptutor.teaching.storage_credentials import (
    ActiveStorageCredentialRepository,
    SqlAlchemyStorageCredentialRepository,
    StorageCredentialError,
    TenantStorageCredentialResolver,
)

_READ_CHUNK_SIZE = 64 * 1024
_MAX_COMMIT_MARKER_SIZE = 4 * 1024 * 1024
_JSON_CONTENT_TYPE = "application/json"
_CLAIM_NAME = ".deeptutor-publish-claim.json"
_COMMIT_NAME = ".deeptutor-commit.json"
_INTERNAL_NAMES = frozenset({_CLAIM_NAME, _COMMIT_NAME})
_LOCAL_SCRATCH_PREFIX = ".deeptutor-scratch-"


class ObjectStoreError(Exception):
    """Base class for safe object-store failures."""


class ObjectStoreAccessDenied(ObjectStoreError):
    """The requested key is outside the store tenant."""


class ObjectStoreNotFound(ObjectStoreError):
    """The requested object does not exist."""


class ObjectStoreIntegrityError(ObjectStoreError):
    """A streamed object did not match its declared integrity metadata."""


class ObjectStoreConfigurationError(ObjectStoreError):
    """Object-store configuration is not safe to use."""


class ObjectStoreConflictError(ObjectStoreError):
    """Promotion would overwrite an existing immutable artifact."""


class ClassroomArtifactStore(Protocol):
    """Minimal streaming protocol shared by local and S3 stores."""

    tenant_id: str

    async def put_verified(
        self,
        key: str,
        body: AsyncIterator[bytes],
        sha256: str,
        size: int,
        *,
        content_type: str = "application/octet-stream",
    ) -> StoredArtifact: ...

    async def open(self, key: str) -> AsyncIterator[bytes]: ...

    async def presign_download(
        self,
        key: str,
        expires_seconds: int,
    ) -> str: ...

    async def list_prefix(self, prefix: str) -> tuple[str, ...]: ...

    async def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        sha256: str,
        size: int,
        content_type: str = "application/octet-stream",
        claim: StoredArtifact | None = None,
    ) -> StoredArtifact: ...

    async def delete(self, key: str) -> None: ...

    async def delete_owned(self, artifact: StoredArtifact) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def confirmed_publish(
        self,
        manifest: ClassroomArtifactManifest,
    ) -> tuple[StoredArtifact, ...] | None: ...

    async def acquire_publish_claim(
        self,
        manifest: ClassroomArtifactManifest,
    ) -> StoredArtifact: ...

    async def commit_publish(
        self,
        manifest: ClassroomArtifactManifest,
        artifacts: tuple[StoredArtifact, ...],
        claim: StoredArtifact,
    ) -> StoredArtifact: ...

    async def confirm_publish(
        self,
        manifest: ClassroomArtifactManifest,
        artifacts: tuple[StoredArtifact, ...],
        claim: StoredArtifact,
    ) -> bool: ...


def _validate_sha256(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ObjectStoreIntegrityError("sha256 must be a lowercase hexadecimal digest")
    return value


def _validate_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObjectStoreIntegrityError("size must be a non-negative integer")
    return value


def _validate_content_type(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip().lower()
        or ";" in value
        or "\r" in value
        or "\n" in value
    ):
        raise ObjectStoreIntegrityError("content type is invalid")
    return value


def _stored_artifact(
    key: str,
    sha256: str,
    size: int,
    content_type: str,
    owner_token: str | None,
    revision: str | None,
    version_id: str | None = None,
) -> StoredArtifact:
    return StoredArtifact(
        key=key,
        sha256=sha256,
        size=size,
        content_type=content_type,
        ownership_token=owner_token,
        revision=revision,
        version_id=version_id,
    )


def _is_owner_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _claim_payload(owner_token: str) -> bytes:
    return json.dumps(
        {"attempt": owner_token, "schema": 1},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_storage_path(value: str, *, allow_trailing_slash: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or PureWindowsPath(value).drive
    ):
        raise ObjectStoreAccessDenied("object key is not a safe tenant key")
    normalized = value[:-1] if allow_trailing_slash and value.endswith("/") else value
    if not normalized or any(segment in {"", ".", ".."} for segment in normalized.split("/")):
        raise ObjectStoreAccessDenied("object key is not a safe tenant key")
    if not allow_trailing_slash and value.endswith("/"):
        raise ObjectStoreAccessDenied("object key is not a safe tenant key")
    return value


def _canonical_artifact_kind(key: str, tenant_id: str) -> str | None:
    parts = key.split("/")
    try:
        if len(parts) >= 5 and parts[:3] == ["tenants", tenant_id, "temporary"]:
            rebuilt = temporary_artifact_key(
                tenant_id,
                parts[3],
                "/".join(parts[4:]),
            )
            return "temporary" if rebuilt == key else None
        if (
            len(parts) >= 7
            and parts[:3] == ["tenants", tenant_id, "classrooms"]
            and parts[4] == "versions"
        ):
            rebuilt = classroom_artifact_key(
                tenant_id,
                parts[3],
                int(parts[5]),
                "/".join(parts[6:]),
            )
            return "classroom" if rebuilt == key else None
    except (TypeError, ValueError):
        pass
    return None


def _is_canonical_artifact_prefix(prefix: str, tenant_id: str) -> bool:
    tenant_prefix = tenant_artifact_prefix(tenant_id)
    if prefix == tenant_prefix:
        return True
    if not prefix.endswith("/") or not prefix.startswith(tenant_prefix):
        return False
    normalized = prefix[:-1]
    parts = normalized.split("/")[2:]
    if not parts:
        return True
    if parts[0] == "temporary":
        completions = {1: "/__job__/__name__", 2: "/__name__"}
        candidate = normalized + completions.get(len(parts), "")
        return _canonical_artifact_kind(candidate, tenant_id) == "temporary"
    if parts[0] == "classrooms":
        completions = {
            1: "/__asset__/versions/1/__name__",
            2: "/versions/1/__name__",
            3: "/1/__name__",
            4: "/__name__",
        }
        candidate = normalized + completions.get(len(parts), "")
        return _canonical_artifact_kind(candidate, tenant_id) == "classroom"
    return False


def _classroom_key_parts(key: str, tenant_id: str) -> tuple[str, int, str]:
    if _canonical_artifact_kind(key, tenant_id) != "classroom":
        raise ObjectStoreAccessDenied("object key is not a canonical classroom key")
    parts = key.split("/")
    return parts[3], int(parts[5]), "/".join(parts[6:])


def _version_internal_key(
    tenant_id: str,
    asset_id: str,
    version: int,
    name: str,
) -> str:
    return classroom_artifact_key(tenant_id, asset_id, version, name)


def _internal_key_for(key: str, tenant_id: str, name: str) -> str:
    asset_id, version, _ = _classroom_key_parts(key, tenant_id)
    return _version_internal_key(tenant_id, asset_id, version, name)


def _is_internal_key(key: str, tenant_id: str) -> bool:
    if _canonical_artifact_kind(key, tenant_id) != "classroom":
        return False
    return _classroom_key_parts(key, tenant_id)[2] in _INTERNAL_NAMES


def _validate_json_bytes(payload: bytes) -> None:
    try:
        json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObjectStoreIntegrityError(
            "application/json object must contain valid UTF-8 JSON"
        ) from exc


def _verify_seekable(
    handle,
    expected_sha256: str,
    expected_size: int,
    content_type: str,
) -> None:
    handle.seek(0)
    digest = hashlib.sha256()
    received = 0
    json_bytes = bytearray() if content_type == _JSON_CONTENT_TYPE else None
    while chunk := handle.read(_READ_CHUNK_SIZE):
        received += len(chunk)
        digest.update(chunk)
        if json_bytes is not None:
            json_bytes.extend(chunk)
    if received != expected_size:
        raise ObjectStoreIntegrityError("object size does not match")
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise ObjectStoreIntegrityError("object sha256 does not match")
    if json_bytes is not None:
        _validate_json_bytes(bytes(json_bytes))
    handle.seek(0)


async def _write_verified(
    handle,
    body: AsyncIterator[bytes],
    expected_sha256: str,
    expected_size: int,
    content_type: str,
) -> None:
    digest = hashlib.sha256()
    received = 0
    json_bytes = bytearray() if content_type == _JSON_CONTENT_TYPE else None
    try:
        async for chunk in body:
            if not isinstance(chunk, bytes):
                raise ObjectStoreIntegrityError("object body chunks must be bytes")
            received += len(chunk)
            if received > expected_size:
                raise ObjectStoreIntegrityError("object body exceeds declared size")
            digest.update(chunk)
            if json_bytes is not None:
                json_bytes.extend(chunk)
            await asyncio.to_thread(handle.write, chunk)
    except ObjectStoreIntegrityError:
        raise
    except (TypeError, AttributeError) as exc:
        raise ObjectStoreIntegrityError("object body must be an async byte stream") from exc

    if received != expected_size:
        raise ObjectStoreIntegrityError("object body is shorter than declared size")
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise ObjectStoreIntegrityError("object body sha256 does not match")
    if json_bytes is not None:
        _validate_json_bytes(bytes(json_bytes))
    await asyncio.to_thread(handle.flush)
    await asyncio.to_thread(lambda: os.fsync(handle.fileno()))
    await asyncio.to_thread(handle.seek, 0)


def _commit_payload(
    manifest: ClassroomArtifactManifest,
    artifacts: tuple[StoredArtifact, ...],
    owner_token: str,
) -> bytes:
    if not _is_owner_token(owner_token):
        raise ObjectStoreIntegrityError("publication owner token is invalid")
    if len(artifacts) != len(manifest.entries):
        raise ObjectStoreIntegrityError("promoted artifacts do not match the manifest")
    entries: list[list[object]] = []
    for entry, artifact in zip(manifest.entries, artifacts, strict=True):
        if (
            artifact.key
            != classroom_artifact_key(
                manifest.tenant_id,
                manifest.asset_id,
                manifest.version,
                entry.relative_name,
            )
            or artifact.sha256 != entry.sha256
            or artifact.size != entry.size
            or artifact.content_type != entry.content_type
        ):
            raise ObjectStoreIntegrityError("promoted artifacts do not match the manifest")
        entries.append([entry.relative_name, entry.content_type, entry.sha256, entry.size])
    return json.dumps(
        {
            "asset_id": manifest.asset_id,
            "attempt": owner_token,
            "entries": entries,
            "schema": 1,
            "tenant_id": manifest.tenant_id,
            "version": manifest.version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parse_commit_payload(
    payload: bytes,
    *,
    marker_key: str,
    tenant_id: str,
) -> tuple[str, tuple[StoredArtifact, ...]]:
    if len(payload) > _MAX_COMMIT_MARKER_SIZE:
        raise ObjectStoreIntegrityError("classroom commit marker is too large")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObjectStoreIntegrityError("classroom commit marker is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"asset_id", "attempt", "entries", "schema", "tenant_id", "version"}
        or document.get("schema") != 1
    ):
        raise ObjectStoreIntegrityError("classroom commit marker is invalid")
    asset_id, version, marker_name = _classroom_key_parts(marker_key, tenant_id)
    attempt = document.get("attempt")
    if (
        marker_name != _COMMIT_NAME
        or document.get("tenant_id") != tenant_id
        or document.get("asset_id") != asset_id
        or document.get("version") != version
        or not _is_owner_token(attempt)
        or not isinstance(document.get("entries"), list)
        or not document["entries"]
    ):
        raise ObjectStoreIntegrityError("classroom commit marker is invalid")
    keys: set[str] = set()
    artifacts: list[StoredArtifact] = []
    for raw_entry in document["entries"]:
        if not isinstance(raw_entry, list) or len(raw_entry) != 4:
            raise ObjectStoreIntegrityError("classroom commit marker is invalid")
        try:
            entry = ArtifactManifestEntry(*raw_entry)
            entry.validate()
            key = classroom_artifact_key(
                tenant_id,
                asset_id,
                version,
                entry.relative_name,
            )
        except (ArtifactManifestError, TypeError, ValueError) as exc:
            raise ObjectStoreIntegrityError("classroom commit marker is invalid") from exc
        if key in keys:
            raise ObjectStoreIntegrityError("classroom commit marker is invalid")
        keys.add(key)
        artifacts.append(
            _stored_artifact(
                key,
                entry.sha256,
                entry.size,
                entry.content_type,
                attempt,
                None,
            )
        )
    return attempt, tuple(artifacts)


class _TenantScopedStore:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self._tenant_prefix = tenant_artifact_prefix(tenant_id)

    def _require_scoped_key(self, key: str) -> str:
        validated = _validate_storage_path(key)
        if not validated.startswith(self._tenant_prefix):
            raise ObjectStoreAccessDenied("object key is outside the current tenant")
        return validated

    def _require_key(self, key: str) -> str:
        validated = self._require_scoped_key(key)
        if _canonical_artifact_kind(validated, self.tenant_id) is None:
            raise ObjectStoreAccessDenied("object key is not a canonical artifact key")
        return validated

    def _require_prefix(self, prefix: str) -> str:
        validated = _validate_storage_path(prefix, allow_trailing_slash=True)
        if not validated.startswith(self._tenant_prefix):
            raise ObjectStoreAccessDenied("object prefix is outside the current tenant")
        if not _is_canonical_artifact_prefix(validated, self.tenant_id):
            raise ObjectStoreAccessDenied("object prefix is not a canonical artifact prefix")
        return validated

    def _require_temporary_key(self, key: str) -> str:
        validated = self._require_scoped_key(key)
        if _canonical_artifact_kind(validated, self.tenant_id) != "temporary":
            raise ObjectStoreAccessDenied("uploads must use the current tenant temporary prefix")
        return validated

    def _require_classroom_key(self, key: str) -> str:
        validated = self._require_scoped_key(key)
        if _canonical_artifact_kind(validated, self.tenant_id) != "classroom":
            raise ObjectStoreAccessDenied(
                "promotion destination must use the current tenant classroom prefix"
            )
        return validated

    async def _read_raw_bytes(self, key: str, max_size: int) -> bytes | None:
        raise NotImplementedError

    async def _create_internal(
        self,
        key: str,
        payload: bytes,
        owner_token: str,
        claim: StoredArtifact | None = None,
    ) -> StoredArtifact:
        raise NotImplementedError

    async def _require_current_claim(
        self,
        claim: StoredArtifact,
        target_key: str,
    ) -> str:
        raise NotImplementedError

    async def _validate_published_artifact(
        self,
        artifact: StoredArtifact,
    ) -> StoredArtifact | None:
        raise NotImplementedError

    def _claim_owner_for(self, claim: StoredArtifact, target_key: str) -> str:
        if (
            claim.key != _internal_key_for(target_key, self.tenant_id, _CLAIM_NAME)
            or claim.ownership_token is None
            or claim.revision is None
        ):
            raise ObjectStoreConflictError("publication claim is not current")
        owner_token = claim.ownership_token
        if not _is_owner_token(owner_token):
            raise ObjectStoreConflictError("publication claim is not current")
        return owner_token

    async def acquire_publish_claim(
        self,
        manifest: ClassroomArtifactManifest,
    ) -> StoredArtifact:
        owner_token = uuid.uuid4().hex
        key = _version_internal_key(
            manifest.tenant_id,
            manifest.asset_id,
            manifest.version,
            _CLAIM_NAME,
        )
        return await self._create_internal(key, _claim_payload(owner_token), owner_token)

    async def commit_publish(
        self,
        manifest: ClassroomArtifactManifest,
        artifacts: tuple[StoredArtifact, ...],
        claim: StoredArtifact,
    ) -> StoredArtifact:
        key = _version_internal_key(
            manifest.tenant_id,
            manifest.asset_id,
            manifest.version,
            _COMMIT_NAME,
        )
        owner_token = self._claim_owner_for(claim, key)
        payload = _commit_payload(manifest, artifacts, owner_token)
        return await self._create_internal(
            key,
            payload,
            owner_token,
            claim,
        )

    async def confirm_publish(
        self,
        manifest: ClassroomArtifactManifest,
        artifacts: tuple[StoredArtifact, ...],
        claim: StoredArtifact,
    ) -> bool:
        key = _version_internal_key(
            manifest.tenant_id,
            manifest.asset_id,
            manifest.version,
            _COMMIT_NAME,
        )
        owner_token = await self._require_current_claim(claim, key)
        expected = _commit_payload(manifest, artifacts, owner_token)
        actual = await self._read_raw_bytes(key, _MAX_COMMIT_MARKER_SIZE)
        if actual is None:
            return False
        if not hmac.compare_digest(actual, expected):
            raise ObjectStoreConflictError("publication marker does not match this attempt")
        marker = _stored_artifact(
            key,
            hashlib.sha256(expected).hexdigest(),
            len(expected),
            _JSON_CONTENT_TYPE,
            owner_token,
            None,
        )
        return await self._validate_published_artifact(marker) is not None

    async def confirmed_publish(
        self,
        manifest: ClassroomArtifactManifest,
    ) -> tuple[StoredArtifact, ...] | None:
        """Return an exact, marker-authorized publication for ``manifest``."""

        manifest.validate_for_tenant(self.tenant_id)
        marker_key = _version_internal_key(
            manifest.tenant_id,
            manifest.asset_id,
            manifest.version,
            _COMMIT_NAME,
        )
        payload = await self._read_raw_bytes(marker_key, _MAX_COMMIT_MARKER_SIZE)
        if payload is None:
            return None
        attempt, artifacts = _parse_commit_payload(
            payload,
            marker_key=marker_key,
            tenant_id=self.tenant_id,
        )
        expected = tuple(
            (
                classroom_artifact_key(
                    manifest.tenant_id,
                    manifest.asset_id,
                    manifest.version,
                    entry.relative_name,
                ),
                entry.sha256,
                entry.size,
                entry.content_type,
            )
            for entry in manifest.entries
        )
        actual = tuple(
            (artifact.key, artifact.sha256, artifact.size, artifact.content_type)
            for artifact in artifacts
        )
        if actual != expected:
            raise ObjectStoreConflictError("publication marker does not match the manifest")

        marker = _stored_artifact(
            marker_key,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            _JSON_CONTENT_TYPE,
            attempt,
            None,
        )
        if await self._validate_published_artifact(marker) is None:
            raise ObjectStoreIntegrityError("publication marker is not durable")
        confirmed: list[StoredArtifact] = []
        for artifact in artifacts:
            validated = await self._validate_published_artifact(artifact)
            if validated is None:
                raise ObjectStoreIntegrityError("published artifact is not durable")
            confirmed.append(validated)
        return tuple(confirmed)

    async def _published_artifacts_for(self, key: str) -> dict[str, StoredArtifact]:
        marker_key = _internal_key_for(key, self.tenant_id, _COMMIT_NAME)
        try:
            payload = await self._read_raw_bytes(marker_key, _MAX_COMMIT_MARKER_SIZE)
            if payload is None:
                return {}
            attempt, artifacts = _parse_commit_payload(
                payload,
                marker_key=marker_key,
                tenant_id=self.tenant_id,
            )
            marker = _stored_artifact(
                marker_key,
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                _JSON_CONTENT_TYPE,
                attempt,
                None,
            )
            validated: dict[str, StoredArtifact] = {}
            for artifact in (marker, *artifacts):
                result = await self._validate_published_artifact(artifact)
                if result is None:
                    return {}
                if artifact.key != marker_key:
                    validated[artifact.key] = result
            return validated
        except (
            ObjectStoreConflictError,
            ObjectStoreIntegrityError,
            ObjectStoreNotFound,
        ):
            return {}

    async def _require_visible_artifact(
        self,
        key: str,
    ) -> tuple[str, StoredArtifact | None]:
        safe_key = self._require_key(key)
        if _canonical_artifact_kind(safe_key, self.tenant_id) == "temporary":
            return safe_key, None
        if _is_internal_key(safe_key, self.tenant_id):
            raise ObjectStoreNotFound("object was not found")
        artifact = (await self._published_artifacts_for(safe_key)).get(safe_key)
        if artifact is None:
            raise ObjectStoreNotFound("object was not found")
        return safe_key, artifact

    async def _filter_visible_keys(
        self,
        prefix: str,
        raw_keys: Iterable[object],
    ) -> tuple[str, ...]:
        marker_cache: dict[str, dict[str, StoredArtifact]] = {}
        visible: list[str] = []
        for raw_key in raw_keys:
            if not isinstance(raw_key, str):
                raise ObjectStoreAccessDenied("backend returned an invalid object key")
            safe_key = self._require_key(raw_key)
            if not safe_key.startswith(prefix):
                raise ObjectStoreAccessDenied("backend returned an object outside the prefix")
            kind = _canonical_artifact_kind(safe_key, self.tenant_id)
            if kind == "temporary":
                visible.append(safe_key)
                continue
            if _is_internal_key(safe_key, self.tenant_id):
                continue
            marker_key = _internal_key_for(safe_key, self.tenant_id, _COMMIT_NAME)
            if marker_key not in marker_cache:
                marker_cache[marker_key] = await self._published_artifacts_for(safe_key)
            if safe_key in marker_cache[marker_key]:
                visible.append(safe_key)
        return tuple(sorted(visible))


def _local_stat_revision(stat) -> str:
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def _local_revision(path: Path) -> str:
    return _local_stat_revision(path.stat())


def _verified_local_spool(
    path: Path,
    artifact: StoredArtifact,
):
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with path.open("rb") as source:
            revision = _local_stat_revision(os.fstat(source.fileno()))
            if artifact.revision is not None and revision != artifact.revision:
                raise ObjectStoreIntegrityError("local object revision changed")
            while chunk := source.read(_READ_CHUNK_SIZE):
                spool.write(chunk)
        spool.flush()
        _verify_seekable(
            spool,
            artifact.sha256,
            artifact.size,
            artifact.content_type,
        )
        return spool, revision
    except FileNotFoundError:
        spool.close()
        raise ObjectStoreNotFound("object was not found") from None
    except BaseException:
        spool.close()
        raise


def _quarantine_owned_local(path: Path, artifact: StoredArtifact) -> None:
    quarantine = path.with_name(f"{_LOCAL_SCRATCH_PREFIX}{uuid.uuid4().hex}.quarantine")
    try:
        os.replace(path, quarantine)
    except FileNotFoundError:
        return
    except OSError:
        raise ObjectStoreError("local object could not be quarantined safely") from None

    try:
        if quarantine.is_symlink() or artifact.revision is None:
            raise ObjectStoreError("object ownership could not be verified")
        if _local_revision(quarantine) != artifact.revision:
            raise ObjectStoreError("object ownership could not be verified")
        _verify_file(
            quarantine,
            artifact.sha256,
            artifact.size,
            artifact.content_type,
        )
    except (OSError, ObjectStoreError):
        try:
            os.link(quarantine, path)
        except OSError:
            pass
        raise ObjectStoreError("object ownership could not be verified") from None

    try:
        quarantine.unlink()
    except OSError:
        raise ObjectStoreError("owned object remains quarantined") from None


def _normalized_filesystem_path(path: Path) -> str:
    value = os.path.normcase(os.path.abspath(str(path.resolve(strict=False))))
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return value


def _ensure_within_local_root(root: Path, candidate: Path) -> None:
    normalized_root = _normalized_filesystem_path(root)
    normalized_candidate = _normalized_filesystem_path(candidate)
    try:
        contained = os.path.commonpath((normalized_root, normalized_candidate))
    except ValueError:
        contained = ""
    if contained != normalized_root:
        raise ObjectStoreAccessDenied("local object path is outside the storage root")


def _verify_file(
    path: Path,
    expected_sha256: str,
    expected_size: int,
    content_type: str,
) -> None:
    try:
        with path.open("rb") as handle:
            _verify_seekable(handle, expected_sha256, expected_size, content_type)
    except ObjectStoreIntegrityError:
        raise
    except OSError as exc:
        raise ObjectStoreNotFound("object was not found") from exc


def _atomic_local_create(
    destination: Path,
    source: Path | bytes,
    expected_sha256: str,
    expected_size: int,
    content_type: str,
) -> str:
    handle = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix=_LOCAL_SCRATCH_PREFIX,
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    )
    scratch = Path(handle.name)
    try:
        if isinstance(source, bytes):
            handle.write(source)
        else:
            with source.open("rb") as source_handle:
                while chunk := source_handle.read(_READ_CHUNK_SIZE):
                    handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
        _verify_seekable(
            handle,
            expected_sha256,
            expected_size,
            content_type,
        )
        handle.close()
        try:
            os.link(scratch, destination)
        except FileExistsError:
            raise ObjectStoreConflictError("classroom artifact version already exists") from None
        except OSError:
            raise ObjectStoreError("local atomic object creation failed") from None
        scratch.unlink(missing_ok=True)
        _verify_file(
            destination,
            expected_sha256,
            expected_size,
            content_type,
        )
        return _local_revision(destination)
    finally:
        handle.close()
        scratch.unlink(missing_ok=True)


class LocalClassroomArtifactStore(_TenantScopedStore):
    """Filesystem adapter intended only for explicit development/test use."""

    def __init__(self, root: Path, tenant_id: str) -> None:
        super().__init__(tenant_id)
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve(strict=True)

    def _assert_no_symlink(self, candidate: Path) -> None:
        try:
            relative = candidate.relative_to(self._root)
        except ValueError as exc:
            raise ObjectStoreAccessDenied("local object path is outside the storage root") from exc
        current = self._root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ObjectStoreAccessDenied("local object path contains a symlink")

    def _path_for(self, key: str) -> Path:
        safe_key = self._require_key(key)
        candidate = self._root.joinpath(*safe_key.split("/"))
        self._assert_no_symlink(candidate)
        _ensure_within_local_root(self._root, candidate)
        return candidate

    def _path_for_prefix(self, prefix: str) -> Path:
        safe_prefix = self._require_prefix(prefix).rstrip("/")
        candidate = self._root.joinpath(*safe_prefix.split("/"))
        self._assert_no_symlink(candidate)
        _ensure_within_local_root(self._root, candidate)
        return candidate

    async def _read_raw_bytes(self, key: str, max_size: int) -> bytes | None:
        path = self._path_for(key)

        def read() -> bytes | None:
            if not path.is_file():
                return None
            with path.open("rb") as handle:
                payload = handle.read(max_size + 1)
            if len(payload) > max_size:
                raise ObjectStoreIntegrityError("internal object is too large")
            return payload

        return await asyncio.to_thread(read)

    async def _validate_published_artifact(
        self,
        artifact: StoredArtifact,
    ) -> StoredArtifact | None:
        path = self._path_for(artifact.key)
        try:
            spool, revision = await asyncio.to_thread(
                _verified_local_spool,
                path,
                artifact,
            )
        except (ObjectStoreIntegrityError, ObjectStoreNotFound):
            return None
        await asyncio.to_thread(spool.close)
        return _stored_artifact(
            artifact.key,
            artifact.sha256,
            artifact.size,
            artifact.content_type,
            artifact.ownership_token,
            revision,
        )

    async def put_verified(
        self,
        key: str,
        body: AsyncIterator[bytes],
        sha256: str,
        size: int,
        *,
        content_type: str = "application/octet-stream",
    ) -> StoredArtifact:
        safe_key = self._require_temporary_key(key)
        destination = self._path_for(safe_key)
        expected_sha256 = _validate_sha256(sha256)
        expected_size = _validate_size(size)
        safe_content_type = _validate_content_type(content_type)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        destination = self._path_for(safe_key)
        owner_token = uuid.uuid4().hex

        temporary_handle = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=_LOCAL_SCRATCH_PREFIX,
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        )
        temporary_path = Path(temporary_handle.name)
        try:
            await _write_verified(
                temporary_handle,
                body,
                expected_sha256,
                expected_size,
                safe_content_type,
            )
            await asyncio.to_thread(temporary_handle.close)
            await asyncio.to_thread(os.replace, temporary_path, destination)
        except BaseException:
            await asyncio.to_thread(temporary_handle.close)
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
            raise
        return _stored_artifact(
            safe_key,
            expected_sha256,
            expected_size,
            safe_content_type,
            owner_token,
            await asyncio.to_thread(_local_revision, destination),
        )

    async def open(self, key: str) -> AsyncIterator[bytes]:
        safe_key, artifact = await self._require_visible_artifact(key)
        path = self._path_for(safe_key)
        if not await asyncio.to_thread(path.is_file):
            raise ObjectStoreNotFound("object was not found")

        async def read_chunks() -> AsyncIterator[bytes]:
            if artifact is None:
                handle = await asyncio.to_thread(path.open, "rb")
            else:
                try:
                    handle, _ = await asyncio.to_thread(
                        _verified_local_spool,
                        path,
                        artifact,
                    )
                except ObjectStoreIntegrityError:
                    raise ObjectStoreNotFound("object was not found") from None
            try:
                while chunk := await asyncio.to_thread(
                    handle.read,
                    _READ_CHUNK_SIZE,
                ):
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

        return read_chunks()

    async def presign_download(
        self,
        key: str,
        expires_seconds: int,
    ) -> str:
        safe_key, _ = await self._require_visible_artifact(key)
        path = self._path_for(safe_key)
        if (
            isinstance(expires_seconds, bool)
            or not isinstance(expires_seconds, int)
            or expires_seconds < 1
        ):
            raise ValueError("expires_seconds must be a positive integer")
        if not await asyncio.to_thread(path.is_file):
            raise ObjectStoreNotFound("object was not found")
        raise ObjectStoreConfigurationError(
            "local object storage cannot create a revision-bound download; use verified open"
        )

    async def list_prefix(self, prefix: str) -> tuple[str, ...]:
        safe_prefix = self._require_prefix(prefix)
        path = self._path_for_prefix(safe_prefix)

        def collect() -> tuple[str, ...]:
            if not path.exists():
                return ()
            keys: list[str] = []
            for candidate in path.rglob("*"):
                if candidate.is_symlink():
                    raise ObjectStoreAccessDenied("local object path contains a symlink")
                if not candidate.is_file():
                    continue
                self._assert_no_symlink(candidate)
                relative = candidate.relative_to(self._root).as_posix()
                if candidate.name.startswith(_LOCAL_SCRATCH_PREFIX):
                    continue
                keys.append(relative)
            return tuple(keys)

        raw_keys = await asyncio.to_thread(collect)
        return await self._filter_visible_keys(safe_prefix, raw_keys)

    async def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        sha256: str,
        size: int,
        content_type: str = "application/octet-stream",
        claim: StoredArtifact | None = None,
    ) -> StoredArtifact:
        safe_source = self._require_temporary_key(source_key)
        safe_destination = self._require_classroom_key(destination_key)
        if _is_internal_key(safe_destination, self.tenant_id):
            raise ObjectStoreAccessDenied("internal publication keys are reserved")
        source = self._path_for(safe_source)
        destination = self._path_for(safe_destination)
        expected_sha256 = _validate_sha256(sha256)
        expected_size = _validate_size(size)
        safe_content_type = _validate_content_type(content_type)
        owner_token = (
            await self._require_current_claim(claim, safe_destination)
            if claim is not None
            else uuid.uuid4().hex
        )
        if not await asyncio.to_thread(source.is_file):
            raise ObjectStoreNotFound("source object was not found")
        await asyncio.to_thread(
            _verify_file,
            source,
            expected_sha256,
            expected_size,
            safe_content_type,
        )
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        destination = self._path_for(safe_destination)
        revision = await asyncio.to_thread(
            _atomic_local_create,
            destination,
            source,
            expected_sha256,
            expected_size,
            safe_content_type,
        )
        return _stored_artifact(
            safe_destination,
            expected_sha256,
            expected_size,
            safe_content_type,
            owner_token,
            revision,
        )

    async def _require_current_claim(
        self,
        claim: StoredArtifact,
        target_key: str,
    ) -> str:
        owner_token = self._claim_owner_for(claim, target_key)
        claim_path = self._path_for(claim.key)
        try:
            revision = await asyncio.to_thread(_local_revision, claim_path)
            payload = await asyncio.to_thread(claim_path.read_bytes)
        except OSError:
            raise ObjectStoreConflictError("publication claim is not current") from None
        if revision != claim.revision or not hmac.compare_digest(
            payload, _claim_payload(owner_token)
        ):
            raise ObjectStoreConflictError("publication claim is not current")
        return owner_token

    async def _create_internal(
        self,
        key: str,
        payload: bytes,
        owner_token: str,
        claim: StoredArtifact | None = None,
    ) -> StoredArtifact:
        safe_key = self._require_classroom_key(key)
        if not _is_internal_key(safe_key, self.tenant_id):
            raise ObjectStoreAccessDenied("publication metadata key is invalid")
        if claim is not None:
            current_owner = await self._require_current_claim(claim, safe_key)
            if current_owner != owner_token:
                raise ObjectStoreConflictError("publication claim is not current")
        destination = self._path_for(safe_key)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        destination = self._path_for(safe_key)
        digest = hashlib.sha256(payload).hexdigest()
        revision = await asyncio.to_thread(
            _atomic_local_create,
            destination,
            payload,
            digest,
            len(payload),
            _JSON_CONTENT_TYPE,
        )
        return _stored_artifact(
            safe_key,
            digest,
            len(payload),
            _JSON_CONTENT_TYPE,
            owner_token,
            revision,
        )

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def delete_owned(self, artifact: StoredArtifact) -> None:
        path = self._path_for(artifact.key)
        try:
            revision = await asyncio.to_thread(_local_revision, path)
        except FileNotFoundError:
            return
        if artifact.revision is None or revision != artifact.revision:
            raise ObjectStoreError("object ownership could not be verified")
        await asyncio.to_thread(_quarantine_owned_local, path, artifact)

    async def exists(self, key: str) -> bool:
        path = self._path_for(key)
        return await asyncio.to_thread(path.is_file)


def _is_s3_precondition_error(error: ClientError) -> bool:
    response = error.response
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status in {409, 412} or code in {
        "ConditionalRequestConflict",
        "PreconditionFailed",
    }


def _raise_s3_error(error: BaseException) -> None:
    if isinstance(error, ClientError):
        response = error.response
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 403 or code in {
            "AccessDenied",
            "AllAccessDisabled",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
        }:
            raise ObjectStoreAccessDenied("S3 access was denied") from None
        if status == 404 or code in {
            "NoSuchBucket",
            "NoSuchKey",
            "NotFound",
        }:
            raise ObjectStoreNotFound("S3 object was not found") from None
        if _is_s3_precondition_error(error):
            raise ObjectStoreConflictError("classroom artifact version already exists") from None
    raise ObjectStoreError("S3 operation failed") from None


def _etag(response: Mapping[str, Any]) -> str:
    value = response.get("ETag")
    if not isinstance(value, str) or not value:
        raise ObjectStoreError("S3 response did not include an object revision")
    return value


def _version_id(response: Mapping[str, Any]) -> str:
    value = response.get("VersionId")
    if not isinstance(value, str) or not value or value == "null":
        raise ObjectStoreConfigurationError(
            "S3 bucket versioning must be enabled for classroom artifacts"
        )
    return value


async def _verified_s3_spool(
    response: Mapping[str, Any],
    expected_sha256: str,
    expected_size: int,
    content_type: str,
):
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    body = response["Body"]

    def read() -> None:
        try:
            while chunk := body.read(_READ_CHUNK_SIZE):
                spool.write(chunk)
            spool.flush()
            _verify_seekable(spool, expected_sha256, expected_size, content_type)
        finally:
            body.close()

    try:
        await asyncio.to_thread(read)
    except BaseException:
        await asyncio.to_thread(spool.close)
        raise
    return spool


async def _get_object_response(client, **arguments):
    request = asyncio.create_task(asyncio.to_thread(client.get_object, **arguments))
    try:
        return await asyncio.shield(request)
    except asyncio.CancelledError:
        while not request.done():
            try:
                await asyncio.shield(request)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not request.cancelled():
            try:
                response = request.result()
            except BaseException:
                pass
            else:
                body = response.get("Body")
                if body is not None:
                    await asyncio.to_thread(body.close)
        raise


class S3ClassroomArtifactStore(_TenantScopedStore):
    """S3/MinIO adapter using one tenant's explicit SigV4 credentials."""

    def __init__(
        self,
        *,
        tenant_id: str,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
    ) -> None:
        super().__init__(tenant_id)
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                endpoint,
                bucket,
                region,
                access_key,
                secret_key,
            )
        ):
            raise ObjectStoreConfigurationError(
                "explicit tenant S3 endpoint, bucket, region, and credentials are required"
            )
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=None,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

    async def require_versioning(self) -> None:
        try:
            response = await asyncio.to_thread(
                self._client.get_bucket_versioning,
                Bucket=self._bucket,
            )
        except (BotoCoreError, ClientError):
            raise ObjectStoreConfigurationError(
                "S3 bucket versioning could not be verified"
            ) from None
        if response.get("Status") != "Enabled":
            raise ObjectStoreConfigurationError(
                "S3 bucket versioning must be enabled for classroom artifacts"
            )

    async def _head_object(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        arguments = {"Bucket": self._bucket, "Key": key}
        if version_id is not None:
            arguments["VersionId"] = version_id
        try:
            return await asyncio.to_thread(
                self._client.head_object,
                **arguments,
            )
        except ClientError as exc:
            response = exc.response
            code = str(response.get("Error", {}).get("Code", ""))
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404 or code in {"NoSuchBucket", "NoSuchKey", "NotFound"}:
                return None
            _raise_s3_error(exc)
        except BotoCoreError as exc:
            _raise_s3_error(exc)

    async def _read_raw_bytes(self, key: str, max_size: int) -> bytes | None:
        safe_key = self._require_key(key)
        try:
            response = await _get_object_response(
                self._client,
                Bucket=self._bucket,
                Key=safe_key,
            )
        except ClientError as exc:
            response_data = exc.response
            code = str(response_data.get("Error", {}).get("Code", ""))
            status = response_data.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404 or code in {"NoSuchKey", "NotFound"}:
                return None
            _raise_s3_error(exc)
        except BotoCoreError as exc:
            _raise_s3_error(exc)
        body = response["Body"]
        try:
            payload = await asyncio.to_thread(body.read, max_size + 1)
        finally:
            await asyncio.to_thread(body.close)
        if len(payload) > max_size:
            raise ObjectStoreIntegrityError("internal object is too large")
        return payload

    async def put_verified(
        self,
        key: str,
        body: AsyncIterator[bytes],
        sha256: str,
        size: int,
        *,
        content_type: str = "application/octet-stream",
    ) -> StoredArtifact:
        safe_key = self._require_temporary_key(key)
        expected_sha256 = _validate_sha256(sha256)
        expected_size = _validate_size(size)
        safe_content_type = _validate_content_type(content_type)
        owner_token = uuid.uuid4().hex
        spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        try:
            await _write_verified(
                spool,
                body,
                expected_sha256,
                expected_size,
                safe_content_type,
            )
            return await self._put_s3_verified(
                safe_key,
                spool,
                sha256=expected_sha256,
                size=expected_size,
                content_type=safe_content_type,
                owner_token=owner_token,
                reconcile_unclaimed=True,
                create_only=False,
            )
        finally:
            await asyncio.to_thread(spool.close)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        safe_key, artifact = await self._require_visible_artifact(key)
        if not await self.exists(safe_key):
            raise ObjectStoreNotFound("object was not found")

        async def read_chunks() -> AsyncIterator[bytes]:
            if artifact is not None:
                spool = await self._spool_validated_s3_artifact(artifact)
                try:
                    while chunk := await asyncio.to_thread(
                        spool.read,
                        _READ_CHUNK_SIZE,
                    ):
                        yield chunk
                finally:
                    await asyncio.shield(asyncio.to_thread(spool.close))
                return
            try:
                response = await _get_object_response(
                    self._client,
                    Bucket=self._bucket,
                    Key=safe_key,
                )
            except (BotoCoreError, ClientError) as exc:
                _raise_s3_error(exc)
            streaming_body = response["Body"]
            try:
                while chunk := await asyncio.to_thread(
                    streaming_body.read,
                    _READ_CHUNK_SIZE,
                ):
                    yield chunk
            finally:
                await asyncio.shield(asyncio.to_thread(streaming_body.close))

        return read_chunks()

    async def presign_download(
        self,
        key: str,
        expires_seconds: int,
    ) -> str:
        safe_key, artifact = await self._require_visible_artifact(key)
        if (
            isinstance(expires_seconds, bool)
            or not isinstance(expires_seconds, int)
            or expires_seconds < 1
        ):
            raise ValueError("expires_seconds must be a positive integer")
        if artifact is None or artifact.version_id is None:
            raise ObjectStoreConfigurationError(
                "S3 download is not bound to a validated version; use verified open"
            )
        try:
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": safe_key,
                    "VersionId": artifact.version_id,
                },
                ExpiresIn=expires_seconds,
            )
        except (BotoCoreError, ClientError) as exc:
            _raise_s3_error(exc)

    async def list_prefix(self, prefix: str) -> tuple[str, ...]:
        safe_prefix = self._require_prefix(prefix)
        keys: list[object] = []
        continuation_token: str | None = None
        while True:
            arguments: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": safe_prefix,
            }
            if continuation_token is not None:
                arguments["ContinuationToken"] = continuation_token
            try:
                response = await asyncio.to_thread(
                    self._client.list_objects_v2,
                    **arguments,
                )
            except (BotoCoreError, ClientError) as exc:
                _raise_s3_error(exc)
            contents = response.get("Contents", ())
            if not isinstance(contents, (list, tuple)):
                raise ObjectStoreAccessDenied("backend returned an invalid object listing")
            for item in contents:
                if not isinstance(item, dict) or "Key" not in item:
                    raise ObjectStoreAccessDenied("backend returned an invalid object key")
                keys.append(item["Key"])
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if not isinstance(continuation_token, str) or not continuation_token:
                raise ObjectStoreError("S3 listing returned an invalid continuation")
        return await self._filter_visible_keys(safe_prefix, keys)

    async def _spool_s3_revision(
        self,
        key: str,
        head: Mapping[str, Any],
        expected_sha256: str,
        expected_size: int,
        content_type: str,
        changed_message: str,
    ) -> tuple[Mapping[str, Any], Any, str]:
        revision = _etag(head)
        version_id = _version_id(head)
        arguments: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "IfMatch": revision,
            "VersionId": version_id,
        }
        try:
            response = await _get_object_response(self._client, **arguments)
        except ClientError as exc:
            if _is_s3_precondition_error(exc):
                raise ObjectStoreIntegrityError(changed_message) from None
            _raise_s3_error(exc)
        except BotoCoreError as exc:
            _raise_s3_error(exc)
        spool = await _verified_s3_spool(
            response,
            expected_sha256,
            expected_size,
            content_type,
        )
        return response, spool, revision

    async def _spool_validated_s3_artifact(self, artifact: StoredArtifact):
        if artifact.revision is None or artifact.version_id is None:
            raise ObjectStoreIntegrityError("validated S3 object revision is missing")
        response, spool, revision = await self._spool_s3_revision(
            artifact.key,
            {
                "ETag": artifact.revision,
                "VersionId": artifact.version_id,
            },
            artifact.sha256,
            artifact.size,
            artifact.content_type,
            "validated S3 object changed before it could be opened",
        )
        metadata = response.get("Metadata", {})
        if (
            _etag(response) != revision
            or _version_id(response) != artifact.version_id
            or response.get("ContentType") != artifact.content_type
            or not isinstance(metadata, dict)
            or metadata.get("owner") != artifact.ownership_token
            or metadata.get("sha256") != artifact.sha256
        ):
            await asyncio.to_thread(spool.close)
            raise ObjectStoreIntegrityError("validated S3 object metadata changed")
        return spool

    async def _spool_s3_source(
        self,
        source_key: str,
        expected_sha256: str,
        expected_size: int,
        content_type: str,
    ):
        head = await self._head_object(source_key)
        if head is None:
            raise ObjectStoreNotFound("source object was not found")
        if head.get("ContentLength") != expected_size:
            raise ObjectStoreIntegrityError("source object size does not match")
        _, spool, _ = await self._spool_s3_revision(
            source_key,
            head,
            expected_sha256,
            expected_size,
            content_type,
            "source object changed during promotion",
        )
        return spool

    async def _reconcile_s3_created(
        self,
        key: str,
        expected_sha256: str,
        expected_size: int,
        content_type: str,
        owner_token: str,
        claim: StoredArtifact | None,
        *,
        version_id: str | None = None,
    ) -> StoredArtifact | None:
        if claim is not None:
            current_owner = await self._require_current_claim(claim, key)
            if current_owner != owner_token:
                raise ObjectStoreConflictError("publication claim is not current")
        head = await self._head_object(key, version_id=version_id)
        if head is None:
            return None
        metadata = head.get("Metadata", {})
        if not isinstance(metadata, dict) or metadata.get("owner") != owner_token:
            raise ObjectStoreConflictError("object is not owned by this publication attempt")
        if (
            metadata.get("sha256") != expected_sha256
            or head.get("ContentLength") != expected_size
            or head.get("ContentType") != content_type
        ):
            raise ObjectStoreIntegrityError("stored object metadata does not match")
        response, spool, revision = await self._spool_s3_revision(
            key,
            head,
            expected_sha256,
            expected_size,
            content_type,
            "destination object changed during verification",
        )
        await asyncio.to_thread(spool.close)
        if _etag(response) != revision:
            raise ObjectStoreIntegrityError("destination object revision does not match")
        actual_version_id = _version_id(head)
        if version_id is not None and actual_version_id != version_id:
            raise ObjectStoreIntegrityError("destination object version does not match")
        if _version_id(response) != actual_version_id:
            raise ObjectStoreIntegrityError("destination object version does not match")
        if response.get("ContentType") != content_type:
            raise ObjectStoreIntegrityError("destination content type does not match")
        return _stored_artifact(
            key,
            expected_sha256,
            expected_size,
            content_type,
            owner_token,
            revision,
            actual_version_id,
        )

    async def _validate_published_artifact(
        self,
        artifact: StoredArtifact,
    ) -> StoredArtifact | None:
        owner_token = artifact.ownership_token
        if not _is_owner_token(owner_token):
            return None
        try:
            return await self._reconcile_s3_created(
                artifact.key,
                artifact.sha256,
                artifact.size,
                artifact.content_type,
                owner_token,
                None,
            )
        except (
            ObjectStoreConflictError,
            ObjectStoreIntegrityError,
            ObjectStoreNotFound,
        ):
            return None

    async def _require_current_claim(
        self,
        claim: StoredArtifact,
        target_key: str,
    ) -> str:
        owner_token = self._claim_owner_for(claim, target_key)
        head = await self._head_object(claim.key)
        metadata = head.get("Metadata", {}) if head is not None else {}
        if (
            head is None
            or not isinstance(metadata, dict)
            or metadata.get("owner") != owner_token
            or metadata.get("sha256") != claim.sha256
            or head.get("ContentLength") != claim.size
            or head.get("ContentType") != claim.content_type
            or _etag(head) != claim.revision
            or _version_id(head) != claim.version_id
        ):
            raise ObjectStoreConflictError("publication claim is not current")
        return owner_token

    async def _put_s3_verified(
        self,
        key: str,
        body,
        *,
        sha256: str,
        size: int,
        content_type: str,
        owner_token: str,
        claim: StoredArtifact | None = None,
        reconcile_unclaimed: bool = False,
        create_only: bool = True,
    ) -> StoredArtifact:
        if claim is not None:
            current_owner = await self._require_current_claim(claim, key)
            if current_owner != owner_token:
                raise ObjectStoreConflictError("publication claim is not current")
        arguments: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": body,
            "ContentLength": size,
            "ContentType": content_type,
            "Metadata": {"owner": owner_token, "sha256": sha256},
        }
        if create_only:
            arguments["IfNoneMatch"] = "*"
        try:
            response = await asyncio.to_thread(
                self._client.put_object,
                **arguments,
            )
        except (BotoCoreError, ClientError) as exc:
            if not isinstance(exc, ClientError) or not _is_s3_precondition_error(exc):
                if claim is not None or reconcile_unclaimed:
                    reconciled = await self._reconcile_s3_created(
                        key,
                        sha256,
                        size,
                        content_type,
                        owner_token,
                        claim,
                    )
                    if reconciled is not None:
                        return reconciled
            _raise_s3_error(exc)
        revision = _etag(response)
        version_id = _version_id(response)
        reconciled = await self._reconcile_s3_created(
            key,
            sha256,
            size,
            content_type,
            owner_token,
            claim,
        )
        if (
            reconciled is None
            or reconciled.revision != revision
            or reconciled.version_id != version_id
        ):
            raise ObjectStoreIntegrityError("destination object changed during verification")
        return reconciled

    async def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        sha256: str,
        size: int,
        content_type: str = "application/octet-stream",
        claim: StoredArtifact | None = None,
    ) -> StoredArtifact:
        safe_source = self._require_temporary_key(source_key)
        safe_destination = self._require_classroom_key(destination_key)
        if _is_internal_key(safe_destination, self.tenant_id):
            raise ObjectStoreAccessDenied("internal publication keys are reserved")
        expected_sha256 = _validate_sha256(sha256)
        expected_size = _validate_size(size)
        safe_content_type = _validate_content_type(content_type)
        owner_token = (
            await self._require_current_claim(claim, safe_destination)
            if claim is not None
            else uuid.uuid4().hex
        )
        spool = await self._spool_s3_source(
            safe_source,
            expected_sha256,
            expected_size,
            safe_content_type,
        )
        try:
            return await self._put_s3_verified(
                safe_destination,
                spool,
                sha256=expected_sha256,
                size=expected_size,
                content_type=safe_content_type,
                owner_token=owner_token,
                claim=claim,
            )
        finally:
            await asyncio.to_thread(spool.close)

    async def _create_internal(
        self,
        key: str,
        payload: bytes,
        owner_token: str,
        claim: StoredArtifact | None = None,
    ) -> StoredArtifact:
        safe_key = self._require_classroom_key(key)
        if not _is_internal_key(safe_key, self.tenant_id):
            raise ObjectStoreAccessDenied("publication metadata key is invalid")
        digest = hashlib.sha256(payload).hexdigest()
        return await self._put_s3_verified(
            safe_key,
            payload,
            sha256=digest,
            size=len(payload),
            content_type=_JSON_CONTENT_TYPE,
            owner_token=owner_token,
            claim=claim,
            reconcile_unclaimed=claim is None,
        )

    async def delete(self, key: str) -> None:
        safe_key = self._require_key(key)
        try:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=safe_key,
            )
        except (BotoCoreError, ClientError) as exc:
            _raise_s3_error(exc)

    async def delete_owned(self, artifact: StoredArtifact) -> None:
        safe_key = self._require_key(artifact.key)
        if (
            artifact.ownership_token is None
            or artifact.revision is None
            or artifact.version_id is None
        ):
            raise ObjectStoreError("object ownership could not be verified")
        reconciled = await self._reconcile_s3_created(
            safe_key,
            artifact.sha256,
            artifact.size,
            artifact.content_type,
            artifact.ownership_token,
            None,
            version_id=artifact.version_id,
        )
        if (
            reconciled is None
            or reconciled.revision != artifact.revision
            or reconciled.version_id != artifact.version_id
        ):
            raise ObjectStoreError("object ownership could not be verified")
        try:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=safe_key,
                VersionId=artifact.version_id,
            )
        except (BotoCoreError, ClientError) as exc:
            _raise_s3_error(exc)

    async def exists(self, key: str) -> bool:
        safe_key = self._require_key(key)
        return await self._head_object(safe_key) is not None


async def _cleanup_actions(
    actions: Iterable[tuple[str, Callable[[], Awaitable[None]]]],
) -> list[str]:
    failures: list[str] = []
    for label, action in actions:
        try:
            await action()
        except ObjectStoreNotFound:
            pass
        except Exception as exc:
            failures.append(f"{label} ({type(exc).__name__})")
    return failures


async def _finish_exception_cleanup(
    actions: Iterable[tuple[str, Callable[[], Awaitable[None]]]],
) -> list[str]:
    cleanup_task = asyncio.create_task(_cleanup_actions(actions))
    while True:
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            if cleanup_task.done():
                return cleanup_task.result()


def _attach_cleanup_failures(error: BaseException, failures: Iterable[str]) -> None:
    rendered = tuple(failures)
    if rendered:
        error.add_note("cleanup failed: " + ", ".join(rendered))


async def _cleanup_owned(
    store: ClassroomArtifactStore,
    groups: Iterable[tuple[str, Iterable[StoredArtifact]]],
    *,
    protected: bool = False,
) -> list[str]:
    actions = (
        (label, lambda artifact=artifact: store.delete_owned(artifact))
        for label, artifacts in groups
        for artifact in artifacts
    )
    runner = _finish_exception_cleanup if protected else _cleanup_actions
    return await runner(actions)


class ClassroomArtifactPromotionService:
    """Publish a manifest atomically through a claim and commit marker."""

    def __init__(self, store: ClassroomArtifactStore) -> None:
        self._store = store

    async def promote(
        self,
        manifest: ClassroomArtifactManifest,
        bodies: Mapping[str, AsyncIterator[bytes]],
    ) -> tuple[StoredArtifact, ...]:
        manifest.validate_for_tenant(self._store.tenant_id)
        declared_names = {entry.relative_name for entry in manifest.entries}
        if set(bodies) != declared_names:
            raise ArtifactManifestError("uploaded files must exactly match the manifest")

        confirmed = await self._store.confirmed_publish(manifest)
        if confirmed is not None:
            return confirmed

        destination_keys = [
            classroom_artifact_key(
                manifest.tenant_id,
                manifest.asset_id,
                manifest.version,
                entry.relative_name,
            )
            for entry in manifest.entries
        ]
        claim: StoredArtifact | None = None
        temporary_keys: list[str] = []
        temporary: list[StoredArtifact] = []
        promoted: list[StoredArtifact] = []
        copy_attempted = False
        commit_attempted = False
        committed = False
        try:
            claim = await self._store.acquire_publish_claim(manifest)
            attempt = claim.ownership_token
            if not _is_owner_token(attempt):
                raise ObjectStoreConflictError("publication claim is not current")
            temporary_keys = [
                temporary_artifact_key(
                    manifest.tenant_id,
                    manifest.job_id,
                    f"{attempt}/{entry.relative_name}",
                )
                for entry in manifest.entries
            ]
            for destination_key in destination_keys:
                if await self._store.exists(destination_key):
                    raise ObjectStoreConflictError("classroom artifact version already exists")

            for entry, temporary_key in zip(
                manifest.entries,
                temporary_keys,
                strict=True,
            ):
                temporary.append(
                    await self._store.put_verified(
                        temporary_key,
                        bodies[entry.relative_name],
                        entry.sha256,
                        entry.size,
                        content_type=entry.content_type,
                    )
                )

            for entry, temporary_key, destination_key in zip(
                manifest.entries,
                temporary_keys,
                destination_keys,
                strict=True,
            ):
                copy_attempted = True
                promoted.append(
                    await self._store.copy(
                        temporary_key,
                        destination_key,
                        sha256=entry.sha256,
                        size=entry.size,
                        content_type=entry.content_type,
                        claim=claim,
                    )
                )

            commit_attempted = True
            try:
                await self._store.commit_publish(manifest, tuple(promoted), claim)
            except BaseException as commit_error:
                confirmation = asyncio.create_task(
                    self._store.confirm_publish(manifest, tuple(promoted), claim)
                )
                try:
                    committed = await asyncio.shield(confirmation)
                except BaseException as confirmation_error:
                    if isinstance(commit_error, asyncio.CancelledError):
                        commit_error.add_note(
                            f"publication confirmation failed: {type(confirmation_error).__name__}"
                        )
                        raise
                    raise confirmation_error from None
                if not committed or isinstance(commit_error, asyncio.CancelledError):
                    raise
            else:
                committed = True

            cleanup_failures = await _cleanup_owned(
                self._store,
                [("temporary object", temporary)],
            )
            if not cleanup_failures and claim is not None:
                cleanup_failures.extend(
                    await _cleanup_owned(
                        self._store,
                        [("publication claim", [claim])],
                    )
                )
            if cleanup_failures:
                error = ObjectStoreError("artifact publication committed but cleanup failed")
                _attach_cleanup_failures(error, cleanup_failures)
                raise error
            return tuple(promoted)
        except BaseException as primary:
            cleanup_groups: list[tuple[str, Iterable[StoredArtifact]]] = []
            if not committed and not commit_attempted:
                cleanup_groups.append(("promoted object", promoted))
            cleanup_failures = await _cleanup_owned(
                self._store,
                cleanup_groups,
                protected=True,
            )
            cleanup_failures.extend(
                await _cleanup_owned(
                    self._store,
                    [("temporary object", temporary)],
                    protected=True,
                )
            )
            ambiguous_write = copy_attempted or commit_attempted
            if claim is not None and not cleanup_failures and (committed or not ambiguous_write):
                cleanup_failures.extend(
                    await _cleanup_owned(
                        self._store,
                        [("publication claim", [claim])],
                        protected=True,
                    )
                )
            _attach_cleanup_failures(primary, cleanup_failures)
            raise


class ClassroomArtifactStoreFactory:
    """Create a tenant-bound store without ambient or shared credentials."""

    def __init__(
        self,
        settings: PlatformSettings,
        *,
        credential_repository: ActiveStorageCredentialRepository | None = None,
        local_root: Path | None = None,
        allow_local: bool = False,
    ) -> None:
        self._settings = settings
        self._credential_repository = credential_repository
        self._local_root = local_root
        self._allow_local = allow_local

    async def create(self, tenant_id: str) -> ClassroomArtifactStore:
        try:
            current_tenant = get_current_tenant()
        except RuntimeError:
            raise ObjectStoreConfigurationError(
                "tenant context is required for object storage"
            ) from None
        if tenant_id != current_tenant.tenant_id:
            raise ObjectStoreConfigurationError(
                "object storage tenant must match the current tenant"
            )
        tenant_id = current_tenant.tenant_id
        tenant_artifact_prefix(tenant_id)
        if self._settings.object_store_mode == "local":
            if not self._allow_local:
                raise ObjectStoreConfigurationError(
                    "local object storage requires explicit development/test opt-in"
                )
            if self._local_root is None:
                raise ObjectStoreConfigurationError("local object storage root is required")
            return LocalClassroomArtifactStore(self._local_root, tenant_id)

        if not self._settings.enabled:
            raise ObjectStoreConfigurationError(
                "S3 object storage requires the platform runtime to be enabled"
            )
        repository = self._credential_repository or SqlAlchemyStorageCredentialRepository()
        record = await repository.get_active(tenant_id)
        if record is None:
            raise ObjectStoreConfigurationError("active tenant storage credential is required")
        credentials_root = self._settings.object_store_tenant_credentials_dir
        endpoint = self._settings.object_store_endpoint
        if credentials_root is None or endpoint is None:
            raise ObjectStoreConfigurationError("tenant S3 storage configuration is incomplete")
        try:
            credentials = TenantStorageCredentialResolver(credentials_root).resolve(
                record,
                tenant_id=tenant_id,
            )
        except StorageCredentialError:
            raise ObjectStoreConfigurationError(
                "tenant storage credential could not be resolved"
            ) from None
        store = S3ClassroomArtifactStore(
            tenant_id=tenant_id,
            endpoint=endpoint,
            bucket=self._settings.object_store_bucket,
            region=self._settings.object_store_region,
            access_key=credentials.access_key,
            secret_key=credentials.secret_key,
        )
        await store.require_versioning()
        return store
