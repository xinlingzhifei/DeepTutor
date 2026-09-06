"""Verify externally signed Gateway observation and host trust envelopes."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

try:
    from scripts.render_platform_compose import (
        _validate_image_lock_binding_bytes,
        candidate_artifact_paths,
        load_image_lock_bytes,
        validate_image_lock_bindings,  # noqa: F401 - retained test seam
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from render_platform_compose import (  # type: ignore[no-redef]
        _validate_image_lock_binding_bytes,
        candidate_artifact_paths,
        load_image_lock_bytes,
        validate_image_lock_bindings,  # noqa: F401 - retained test seam
    )

MAX_GATEWAY_TRUST_ENVELOPE_BYTES = 64 * 1024
MAX_GATEWAY_TRUST_KEYRING_BYTES = 64 * 1024
MAX_GATEWAY_TRUST_TTL_SECONDS = 600
MAX_GATEWAY_TRUST_AGE_SECONDS = 300
MAX_GATEWAY_TRUST_FUTURE_SKEW_SECONDS = 5

_SIGNATURE_DOMAIN = b"yfeistai.gateway-trust-envelope.v2\0"
_PAYLOAD_TYPES = {"gateway-observer", "gateway-host-provisioner"}
_ISSUERS = {
    "gateway-observer": "external-observer-authority",
    "gateway-host-provisioner": "deployment-authority",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_FIELDS = {
    "schemaVersion",
    "payloadType",
    "algorithm",
    "keyId",
    "payload",
    "signatureBase64",
}
_PAYLOAD_FIELDS = {
    "schemaVersion",
    "issuer",
    "issuedAt",
    "expiresAt",
    "challenge",
    "candidate",
    "candidateContract",
    "releaseRun",
    "environmentId",
    "claims",
}
_KEY_FIELDS = {
    "algorithm",
    "keyId",
    "payloadTypes",
    "publicKeySpkiBase64",
}
_OBSERVER_ARTIFACT = "runtime/gateway-external-observer-attestation.json"
_HOST_RECEIPT_ARTIFACT = "runtime/gateway-docker-host-provisioning-receipt.json"
_HOST_RECEIPT_FIELDS = {
    "schemaVersion",
    "producer",
    "candidate",
    "candidateContract",
    "releaseRun",
    "environmentId",
    "host",
}
_HOST_FIELDS = {
    "physicalHostIdSha256",
    "dockerContext",
    "dockerEndpoint",
    "dockerServerId",
    "osType",
}
_CANDIDATE_ARTIFACT_RELATIVE_PATHS = (
    "deploy/image-lock.json",
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.data-plane.yml",
    "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
)

_MAX_CANDIDATE_CONTRACT_BYTES = 2 * 1024 * 1024
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PathLstat = Callable[[Path], object]
_FileSignature = tuple[int, int, int, int, int | None, int, int | None]


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_document(body: bytes, *, limit: int, label: str) -> dict[str, object]:
    if not isinstance(body, bytes) or not body or len(body) > limit:
        raise ValueError(f"{label} size is invalid")
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError(f"{label} JSON is invalid") from None
    if not isinstance(document, dict) or _canonical_json(document) != body:
        raise ValueError(f"{label} is not canonical")
    return document


def _file_signature(value: os.stat_result) -> _FileSignature:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        getattr(value, "st_ctime_ns", None),
        value.st_mode,
        getattr(value, "st_uid", None),
    )


def _is_link_or_reparse(value: object) -> bool:
    return stat.S_ISLNK(getattr(value, "st_mode")) or bool(
        getattr(value, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _absolute_path_chain(path: Path, *, label: str) -> tuple[Path, ...]:
    target = Path(path)
    if not target.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    current = Path(target.anchor)
    chain = [current]
    for component in target.parts[1:]:
        current /= component
        chain.append(current)
    return tuple(chain)


def _trusted_path_policy(
    path: Path,
    *,
    platform_name: str | None,
    trusted_owner_uid: int | None,
    path_lstat: _PathLstat,
) -> tuple[object, tuple[tuple[Path, int, int, int, int | None], ...]]:
    chain = _absolute_path_chain(path, label="gateway trust keyring")
    final: object | None = None
    identities: list[tuple[Path, int, int, int, int | None]] = []
    for index, component in enumerate(chain):
        try:
            details = path_lstat(component)
        except OSError:
            raise ValueError("gateway trust keyring is unavailable") from None
        if _is_link_or_reparse(details):
            raise ValueError("gateway trust keyring path contains a symlink")
        is_file = index == len(chain) - 1
        mode = getattr(details, "st_mode")
        if is_file:
            if not stat.S_ISREG(mode):
                raise ValueError("gateway trust keyring is unsafe")
        elif not stat.S_ISDIR(mode):
            raise ValueError("gateway trust keyring path is unsafe")
        if platform_name is not None:
            if platform_name == "nt":
                raise ValueError("Windows gateway trust keyring DACL cannot be verified")
            if platform_name != "posix" or not isinstance(trusted_owner_uid, int):
                raise ValueError("gateway trust keyring owner policy is unavailable")
            owner_uid = getattr(details, "st_uid", None)
            if owner_uid not in {0, trusted_owner_uid}:
                raise ValueError("gateway trust keyring owner is not trusted")
            permissions = stat.S_IMODE(mode)
            if is_file:
                if permissions & 0o077:
                    raise ValueError("gateway trust keyring file permissions are not restricted")
            elif permissions & 0o022:
                raise ValueError("gateway trust keyring directory permissions are not restricted")
        identities.append(
            (
                component,
                getattr(details, "st_dev"),
                getattr(details, "st_ino"),
                mode,
                getattr(details, "st_uid", None),
            )
        )
        final = details
    if final is None:
        raise ValueError("gateway trust keyring is unavailable")
    return final, tuple(identities)


def _read_trusted_keyring(
    path: Path,
    *,
    candidate_root: Path,
    platform_name: str | None = None,
    trusted_owner_uid: int | None = None,
    path_lstat: _PathLstat | None = None,
) -> bytes:
    requested = Path(path)
    lstat_reader = path_lstat or (lambda value: value.lstat())
    try:
        if not Path(candidate_root).is_absolute():
            raise ValueError("candidate root path must be absolute")
        candidate = Path(candidate_root).resolve(strict=True)
        requested_stat, path_identities = _trusted_path_policy(
            requested,
            platform_name=platform_name,
            trusted_owner_uid=trusted_owner_uid,
            path_lstat=lstat_reader,
        )
        resolved = requested.resolve(strict=True)
    except ValueError:
        raise
    except OSError:
        raise ValueError("gateway trust keyring is unavailable") from None
    if resolved == candidate or candidate in resolved.parents:
        raise ValueError("gateway trust keyring must be outside candidate root")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError:
        raise ValueError("gateway trust keyring is unavailable") from None
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_size <= 0
            or opened_stat.st_size > MAX_GATEWAY_TRUST_KEYRING_BYTES
        ):
            raise ValueError("gateway trust keyring size is invalid")
        chunks: list[bytes] = []
        remaining = opened_stat.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ValueError("gateway trust keyring changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("gateway trust keyring changed while being read")
        final_stat = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current_stat = requested.lstat()
    except OSError:
        raise ValueError("gateway trust keyring changed while being read") from None
    if path_lstat is None:
        _current_final, current_path_identities = _trusted_path_policy(
            requested,
            platform_name=platform_name,
            trusted_owner_uid=trusted_owner_uid,
            path_lstat=lambda value: value.lstat(),
        )
    else:
        current_path_identities = path_identities
    if (
        _is_link_or_reparse(current_stat)
        or current_path_identities != path_identities
        or _file_signature(opened_stat) != _file_signature(final_stat)
        or _file_signature(opened_stat) != _file_signature(current_stat)
    ):
        raise ValueError("gateway trust keyring changed while being read")
    return b"".join(chunks)


def _candidate_root_signature(root: Path) -> _FileSignature:
    chain = _absolute_path_chain(root, label="gateway candidate root")
    final: object | None = None
    for component in chain:
        try:
            details = component.lstat()
        except OSError:
            raise ValueError("gateway candidate root is unavailable") from None
        if _is_link_or_reparse(details):
            raise ValueError("gateway candidate root path contains a symlink")
        if component != chain[-1] and not stat.S_ISDIR(getattr(details, "st_mode")):
            raise ValueError("gateway candidate root path is invalid")
        final = details
    if final is None or not stat.S_ISDIR(getattr(final, "st_mode")):
        raise ValueError("gateway candidate root is invalid")
    return _file_signature(final)


def _candidate_contract_artifact_stat(path: Path) -> os.stat_result:
    chain = _absolute_path_chain(path, label="gateway candidate contract artifact")
    artifact_stat: os.stat_result | None = None
    for index, component in enumerate(chain):
        try:
            details = component.lstat()
        except OSError:
            raise ValueError("gateway candidate contract artifact is unavailable") from None
        if _is_link_or_reparse(details):
            raise ValueError("gateway candidate contract artifact path contains a symlink")
        if index == len(chain) - 1:
            if not stat.S_ISREG(getattr(details, "st_mode")):
                raise ValueError("gateway candidate contract artifact is invalid")
            artifact_stat = details
        elif not stat.S_ISDIR(getattr(details, "st_mode")):
            raise ValueError("gateway candidate contract artifact path is invalid")
    if artifact_stat is None:
        raise ValueError("gateway candidate contract artifact is unavailable")
    return artifact_stat


@dataclass(frozen=True, slots=True)
class _CandidateArtifactLease:
    path: Path
    descriptor: int
    signature: _FileSignature


def _open_candidate_contract_descriptor(path: Path) -> int:
    if os.name != "nt":
        return os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )

    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, "gateway candidate contract artifact is unavailable")
    raw_handle = int(handle)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        return msvcrt.open_osfhandle(raw_handle, flags)
    except BaseException:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
        raise


def _open_candidate_contract_artifact(path: Path) -> _CandidateArtifactLease:
    before = _candidate_contract_artifact_stat(path)
    try:
        descriptor = _open_candidate_contract_descriptor(path)
    except OSError:
        raise ValueError("gateway candidate contract artifact is unavailable") from None
    keep_open = False
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_size > _MAX_CANDIDATE_CONTRACT_BYTES
            or _file_signature(opened) != _file_signature(before)
        ):
            raise ValueError("gateway candidate contract artifact is invalid")
        lease = _CandidateArtifactLease(
            path=path,
            descriptor=descriptor,
            signature=_file_signature(opened),
        )
        keep_open = True
        return lease
    finally:
        if not keep_open:
            os.close(descriptor)


def _read_candidate_contract_artifact(lease: _CandidateArtifactLease) -> bytes:
    descriptor = lease.descriptor
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = lease.signature[2]
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError("gateway candidate contract artifact changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("gateway candidate contract artifact changed while reading")
    except (OSError, ValueError):
        raise ValueError("gateway candidate contract artifact changed while reading") from None
    return b"".join(chunks)


def _assert_candidate_artifact_leases_current(
    root: Path,
    root_signature: _FileSignature,
    leases: tuple[_CandidateArtifactLease, ...],
    expected_sha256: tuple[str, ...],
) -> None:
    try:
        if len(leases) != len(expected_sha256):
            raise ValueError("gateway candidate contract artifact changed while reading")
        lease_digests = tuple(zip(leases, expected_sha256, strict=True))
        for lease, expected_digest in reversed(lease_digests):
            current_digest = hashlib.sha256(_read_candidate_contract_artifact(lease)).hexdigest()
            if current_digest != expected_digest:
                raise ValueError("gateway candidate contract artifact changed while reading")
        for lease in leases:
            opened = os.fstat(lease.descriptor)
            current = _candidate_contract_artifact_stat(lease.path)
            if (
                _is_link_or_reparse(opened)
                or _is_link_or_reparse(current)
                or _file_signature(opened) != lease.signature
                or _file_signature(current) != lease.signature
            ):
                raise ValueError("gateway candidate contract artifact changed while reading")
        if _candidate_root_signature(root) != root_signature:
            raise ValueError("gateway candidate contract artifact changed while reading")
    except OSError:
        raise ValueError("gateway candidate contract artifact changed while reading") from None


@dataclass(frozen=True, slots=True)
class _CandidateContract:
    root: Path
    root_signature: _FileSignature
    artifact_signatures: tuple[tuple[Path, _FileSignature, str], ...]
    candidate: dict[str, object]
    leases: tuple[_CandidateArtifactLease, ...] = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def claim(self) -> dict[str, object]:
        return _candidate_contract_claim(self.root, self.artifact_signatures)

    def __enter__(self) -> _CandidateContract:
        if self._closed:
            raise ValueError("gateway candidate contract lease is closed")
        return self

    def __exit__(self, error_type: object, _error: object, _traceback: object) -> None:
        try:
            if error_type is None:
                self.assert_current()
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        for lease in reversed(self.leases):
            try:
                os.close(lease.descriptor)
            except OSError:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def assert_current(self) -> None:
        try:
            if self._closed:
                current = _candidate_contract_snapshot(
                    self.root,
                    tuple(path for path, _signature, _sha256 in self.artifact_signatures),
                )
                if (current[0], current[1]) != (
                    self.root_signature,
                    self.artifact_signatures,
                ):
                    raise ValueError
            else:
                _assert_candidate_artifact_leases_current(
                    self.root,
                    self.root_signature,
                    self.leases,
                    tuple(sha256 for _path, _signature, sha256 in self.artifact_signatures),
                )
        except (OSError, ValueError):
            if not self._closed:
                try:
                    _assert_candidate_artifact_leases_current(
                        self.root,
                        self.root_signature,
                        self.leases,
                        tuple(sha256 for _path, _signature, sha256 in self.artifact_signatures),
                    )
                except (OSError, ValueError):
                    pass
            raise ValueError(
                "gateway candidate contract changed during trust verification"
            ) from None

    def assert_unchanged(self) -> None:
        self.assert_current()


def _candidate_contract_snapshot(
    root: Path,
    artifact_paths: tuple[Path, ...],
) -> tuple[
    _FileSignature,
    tuple[tuple[Path, _FileSignature, str], ...],
]:
    root_signature = _candidate_root_signature(root)
    leases: list[_CandidateArtifactLease] = []
    try:
        for path in artifact_paths:
            leases.append(_open_candidate_contract_artifact(path))
        artifacts = tuple(
            (
                lease.path,
                lease.signature,
                hashlib.sha256(_read_candidate_contract_artifact(lease)).hexdigest(),
            )
            for lease in leases
        )
        fixed_leases = tuple(leases)
        _assert_candidate_artifact_leases_current(
            root,
            root_signature,
            fixed_leases,
            tuple(sha256 for _path, _signature, sha256 in artifacts),
        )
        return root_signature, artifacts
    finally:
        for lease in reversed(leases):
            os.close(lease.descriptor)


def _candidate_contract_claim(
    root: Path,
    artifacts: tuple[tuple[Path, _FileSignature, str], ...],
) -> dict[str, object]:
    artifact_digests = {
        path.relative_to(root).as_posix(): {"sha256": sha256}
        for path, _signature, sha256 in artifacts
    }
    if tuple(artifact_digests) != _CANDIDATE_ARTIFACT_RELATIVE_PATHS:
        raise ValueError("gateway candidate contract artifact set is invalid")
    return {"schemaVersion": 1, "artifacts": artifact_digests}


def validate_gateway_candidate_contract_bytes(
    artifact_contents: tuple[tuple[Path, bytes], ...],
    *,
    require_candidate: bool,
) -> dict[str, object]:
    """Validate one gateway candidate contract from a single leased byte set."""

    if (
        len(artifact_contents) != len(_CANDIDATE_ARTIFACT_RELATIVE_PATHS)
        or tuple(path.as_posix() for path, _content in artifact_contents)
        != _CANDIDATE_ARTIFACT_RELATIVE_PATHS
        or any(
            not isinstance(content, bytes) or not content for _path, content in artifact_contents
        )
    ):
        raise ValueError("gateway candidate contract artifact set is invalid")
    lock = load_image_lock_bytes(
        artifact_contents[0][1],
        require_candidate=require_candidate,
    )
    _validate_image_lock_binding_bytes(lock, artifact_contents[1:])
    raw_candidate = lock.get("candidate")
    if not isinstance(raw_candidate, dict):
        raise ValueError("gateway candidate identity is invalid")
    candidate = json.loads(json.dumps(raw_candidate, allow_nan=False))
    if not isinstance(candidate, dict):
        raise ValueError("gateway candidate identity is invalid")
    claim = {
        "schemaVersion": 1,
        "artifacts": {
            path.as_posix(): {"sha256": hashlib.sha256(content).hexdigest()}
            for path, content in artifact_contents
        },
    }
    return {"candidate": candidate, "candidateContract": claim}


def _validate_candidate_contract_claim(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "artifacts"}:
        raise ValueError("gateway candidate contract claim is invalid")
    artifacts = value.get("artifacts")
    if (
        type(value.get("schemaVersion")) is not int
        or value.get("schemaVersion") != 1
        or not isinstance(artifacts, dict)
        or set(artifacts) != set(_CANDIDATE_ARTIFACT_RELATIVE_PATHS)
    ):
        raise ValueError("gateway candidate contract claim is invalid")
    for artifact in artifacts.values():
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"sha256"}
            or not isinstance(artifact.get("sha256"), str)
            or _SHA256.fullmatch(artifact["sha256"]) is None
        ):
            raise ValueError("gateway candidate contract claim is invalid")
    return json.loads(json.dumps(value, allow_nan=False))


def _load_candidate_contract(candidate_root: Path) -> _CandidateContract:
    root = Path(candidate_root)
    if not root.is_absolute():
        raise ValueError("gateway candidate root path must be absolute")
    _candidate_root_signature(root)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        raise ValueError("gateway candidate root is unavailable") from None
    paths = candidate_artifact_paths(resolved_root)
    artifact_paths = (
        paths.image_lock,
        paths.base_compose,
        paths.platform_compose,
        paths.data_plane_compose,
        paths.observer_overlay,
    )
    root_signature = _candidate_root_signature(resolved_root)
    leases: list[_CandidateArtifactLease] = []
    keep_open = False
    try:
        for path in artifact_paths:
            leases.append(_open_candidate_contract_artifact(path))
        bodies = tuple(_read_candidate_contract_artifact(lease) for lease in leases)
        artifact_signatures = tuple(
            (
                lease.path,
                lease.signature,
                hashlib.sha256(body).hexdigest(),
            )
            for lease, body in zip(leases, bodies, strict=True)
        )
        leased_contents = tuple(
            (Path(relative), body)
            for relative, body in zip(
                _CANDIDATE_ARTIFACT_RELATIVE_PATHS,
                bodies,
                strict=True,
            )
        )
        try:
            validated = validate_gateway_candidate_contract_bytes(
                leased_contents,
                require_candidate=True,
            )
        except (OSError, TypeError):
            raise ValueError("gateway candidate contract is unavailable or invalid") from None
        candidate = validated.get("candidate")
        candidate_claim = validated.get("candidateContract")
        expected_claim = _candidate_contract_claim(
            resolved_root,
            artifact_signatures,
        )
        if not isinstance(candidate, dict) or candidate_claim != expected_claim:
            raise ValueError("gateway candidate contract is unavailable or invalid")
        try:
            candidate = json.loads(json.dumps(candidate, allow_nan=False))
        except (TypeError, ValueError):
            raise ValueError("gateway candidate contract is unavailable or invalid") from None
        _assert_candidate_artifact_leases_current(
            resolved_root,
            root_signature,
            tuple(leases),
            tuple(sha256 for _path, _signature, sha256 in artifact_signatures),
        )
        contract = _CandidateContract(
            root=resolved_root,
            root_signature=root_signature,
            artifact_signatures=artifact_signatures,
            candidate=candidate,
            leases=tuple(leases),
        )
        keep_open = True
        return contract
    finally:
        if not keep_open:
            for lease in reversed(leases):
                try:
                    os.close(lease.descriptor)
                except OSError:
                    pass


def _trusted_keys(
    body: bytes,
) -> dict[str, tuple[Ed25519PublicKey, frozenset[str]]]:
    document = _canonical_document(
        body,
        limit=MAX_GATEWAY_TRUST_KEYRING_BYTES,
        label="gateway trust keyring",
    )
    keys = document.get("keys")
    if (
        set(document) != {"schemaVersion", "keys"}
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
    ):
        raise ValueError("gateway trust keyring schema is invalid")
    if not isinstance(keys, list) or not keys:
        raise ValueError("gateway trust keyring schema is invalid")
    trusted: dict[str, tuple[Ed25519PublicKey, frozenset[str]]] = {}
    for raw in keys:
        if not isinstance(raw, dict) or set(raw) != _KEY_FIELDS:
            raise ValueError("gateway trust keyring schema is invalid")
        key_id = raw.get("keyId")
        payload_types = raw.get("payloadTypes")
        encoded_key = raw.get("publicKeySpkiBase64")
        if (
            raw.get("algorithm") != "ed25519"
            or not isinstance(key_id, str)
            or _SHA256.fullmatch(key_id) is None
            or not isinstance(payload_types, list)
            or not payload_types
            or any(item not in _PAYLOAD_TYPES for item in payload_types)
            or len(set(payload_types)) != len(payload_types)
            or not isinstance(encoded_key, str)
            or key_id in trusted
        ):
            raise ValueError("gateway trust keyring schema is invalid")
        try:
            spki = base64.b64decode(encoded_key, validate=True)
            public_key = serialization.load_der_public_key(spki)
        except (ValueError, TypeError):
            raise ValueError("gateway trust keyring public key is invalid") from None
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("gateway trust keyring public key is invalid")
        canonical_spki = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if spki != canonical_spki or hashlib.sha256(spki).hexdigest() != key_id:
            raise ValueError("gateway trust key id is invalid")
        trusted[key_id] = (public_key, frozenset(payload_types))
    return trusted


def _utc_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"gateway trust {label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError(f"gateway trust {label} timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"gateway trust {label} timestamp is invalid")
    return parsed


def _parse_gateway_trust_envelope_from_keys(
    body: bytes,
    *,
    expected_payload_type: str,
    trusted_keys: Mapping[str, tuple[Ed25519PublicKey, frozenset[str]]],
    candidate: Mapping[str, object],
    candidate_contract: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_environment_id: str,
    expected_challenge: str,
    trusted_now: str,
) -> tuple[dict[str, object], str, frozenset[str]]:

    if expected_payload_type not in _PAYLOAD_TYPES:
        raise ValueError("gateway trust payload role is invalid")
    document = _canonical_document(
        body,
        limit=MAX_GATEWAY_TRUST_ENVELOPE_BYTES,
        label="gateway trust envelope",
    )
    if set(document) != _ENVELOPE_FIELDS:
        if "signatureBase64" not in document:
            raise ValueError("gateway trust signature is missing")
        raise ValueError("gateway trust envelope schema is invalid")
    payload_type = document.get("payloadType")
    key_id = document.get("keyId")
    payload = document.get("payload")
    encoded_signature = document.get("signatureBase64")
    if (
        type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 2
        or payload_type != expected_payload_type
        or document.get("algorithm") != "ed25519"
        or not isinstance(key_id, str)
        or _SHA256.fullmatch(key_id) is None
        or not isinstance(payload, dict)
        or not isinstance(encoded_signature, str)
    ):
        raise ValueError("gateway trust envelope schema is invalid")

    trusted_entry = trusted_keys.get(key_id)
    if trusted_entry is None:
        raise ValueError("gateway trust signature key is not trusted")
    public_key, authorized_payload_types = trusted_entry
    if payload_type not in authorized_payload_types:
        raise ValueError("gateway trust key role is invalid")
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, TypeError):
        raise ValueError("gateway trust signature is invalid") from None
    if len(signature) != 64:
        raise ValueError("gateway trust signature is invalid")
    signature_input = (
        _SIGNATURE_DOMAIN + payload_type.encode("ascii") + b"\0" + _canonical_json(payload)
    )
    try:
        public_key.verify(signature, signature_input)
    except (InvalidSignature, ValueError):
        raise ValueError("gateway trust signature is invalid") from None

    if (
        set(payload) != _PAYLOAD_FIELDS
        or type(payload.get("schemaVersion")) is not int
        or payload.get("schemaVersion") != 2
    ):
        raise ValueError("gateway trust payload schema is invalid")
    if payload.get("issuer") != _ISSUERS[payload_type]:
        raise ValueError("gateway trust payload issuer is invalid")
    issued_at = _utc_timestamp(payload.get("issuedAt"), label="issued")
    expires_at = _utc_timestamp(payload.get("expiresAt"), label="expiry")
    now = _utc_timestamp(trusted_now, label="trusted now")
    if issued_at - now > timedelta(seconds=MAX_GATEWAY_TRUST_FUTURE_SKEW_SECONDS):
        raise ValueError("gateway trust envelope was issued in the future")
    if expires_at <= issued_at or now >= expires_at:
        raise ValueError("gateway trust envelope is expired")
    if expires_at - issued_at > timedelta(seconds=MAX_GATEWAY_TRUST_TTL_SECONDS):
        raise ValueError("gateway trust envelope lifetime is too long")
    if now - issued_at > timedelta(seconds=MAX_GATEWAY_TRUST_AGE_SECONDS):
        raise ValueError("gateway trust envelope is too old")
    if (
        not isinstance(expected_challenge, str)
        or _SHA256.fullmatch(expected_challenge) is None
        or payload.get("challenge") != expected_challenge
    ):
        raise ValueError("gateway trust challenge does not match")
    if payload.get("candidate") != dict(candidate):
        raise ValueError("gateway trust candidate does not match")
    expected_candidate_contract = _validate_candidate_contract_claim(dict(candidate_contract))
    if payload.get("candidateContract") != expected_candidate_contract:
        raise ValueError("gateway trust candidate contract does not match signed payload")
    if payload.get("releaseRun") != dict(release_run):
        raise ValueError("gateway trust release run does not match")
    if payload.get("environmentId") != expected_environment_id:
        raise ValueError("gateway trust environment does not match")
    claims = payload.get("claims")
    if (
        not isinstance(claims, dict)
        or set(claims) != {"artifact", "artifactSha256"}
        or not isinstance(claims.get("artifact"), str)
        or not claims["artifact"]
        or not isinstance(claims.get("artifactSha256"), str)
        or _SHA256.fullmatch(claims["artifactSha256"]) is None
        or claims["artifactSha256"] == "0" * 64
    ):
        raise ValueError("gateway trust payload claims are invalid")
    return payload, key_id, authorized_payload_types


def _parse_gateway_trust_envelope(
    body: bytes,
    *,
    expected_payload_type: str,
    trusted_keyring_path: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    candidate_contract: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_environment_id: str,
    expected_challenge: str,
    trusted_now: str,
) -> dict[str, object]:
    """Return a verified payload rooted in a keyring outside the candidate."""

    keyring_body = _read_trusted_keyring(
        Path(trusted_keyring_path),
        candidate_root=Path(candidate_root),
    )
    payload, _key_id, _authorized_payload_types = _parse_gateway_trust_envelope_from_keys(
        body,
        expected_payload_type=expected_payload_type,
        trusted_keys=_trusted_keys(keyring_body),
        candidate=candidate,
        candidate_contract=candidate_contract,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
        expected_challenge=expected_challenge,
        trusted_now=trusted_now,
    )
    return payload


def _gateway_trust_claim(
    payload: Mapping[str, object],
    *,
    expected_artifact: str,
    artifact_body: bytes,
    label: str,
) -> None:
    claims = payload.get("claims")
    if not isinstance(claims, Mapping) or claims.get("artifact") != expected_artifact:
        raise ValueError(f"gateway trust {label} artifact is invalid")
    if not isinstance(artifact_body, bytes) or not artifact_body:
        raise ValueError(f"gateway trust {label} artifact bytes are invalid")
    if claims.get("artifactSha256") != hashlib.sha256(artifact_body).hexdigest():
        raise ValueError(f"gateway trust {label} artifact digest does not match")


def _parse_gateway_host_receipt(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    candidate_contract: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_environment_id: str,
) -> dict[str, object]:
    receipt = _canonical_document(
        body,
        limit=MAX_GATEWAY_TRUST_ENVELOPE_BYTES,
        label="gateway host provisioning receipt",
    )
    if (
        set(receipt) != _HOST_RECEIPT_FIELDS
        or type(receipt.get("schemaVersion")) is not int
        or receipt.get("schemaVersion") != 2
        or receipt.get("producer") != "gateway-docker-host-provisioner"
        or receipt.get("candidate") != dict(candidate)
        or receipt.get("candidateContract")
        != _validate_candidate_contract_claim(dict(candidate_contract))
        or receipt.get("releaseRun") != dict(release_run)
        or receipt.get("environmentId") != expected_environment_id
    ):
        raise ValueError("gateway trust host receipt schema or binding is invalid")
    host = receipt.get("host")
    if not isinstance(host, dict) or set(host) != _HOST_FIELDS:
        raise ValueError("gateway trust host receipt schema is invalid")
    physical_host_id = host.get("physicalHostIdSha256")
    endpoint = host.get("dockerEndpoint")
    server_id = host.get("dockerServerId")
    if (
        not isinstance(physical_host_id, str)
        or _SHA256.fullmatch(physical_host_id) is None
        or physical_host_id == "0" * 64
        or host.get("dockerContext") != "default"
        or host.get("osType") != "linux"
        or not isinstance(endpoint, str)
        or not endpoint
        or endpoint != endpoint.strip()
        or not isinstance(server_id, str)
        or not server_id
        or server_id != server_id.strip()
    ):
        raise ValueError("gateway trust host receipt host identity is invalid")
    return receipt


def _verify_gateway_trust_pair_under_lease(
    *,
    candidate_contract: _CandidateContract,
    observer_envelope_body: bytes,
    host_envelope_body: bytes,
    observer_artifact_body: bytes,
    host_receipt_body: bytes,
    trusted_keyring_path: Path,
    expected_trusted_keyring_sha256: str,
    release_run: Mapping[str, str],
    expected_environment_id: str,
    expected_observer_challenge: str,
    expected_host_challenge: str,
    trusted_now: str,
    trusted_keyring_platform_name: str,
    trusted_owner_uid: int | None = None,
) -> dict[str, object]:
    """Verify role-separated trust roots against one immutable candidate contract.

    The trusted keyring path and digest remain external-controller authority inputs.
    """

    candidate = candidate_contract.candidate
    candidate_contract_claim = candidate_contract.claim
    if (
        not isinstance(expected_observer_challenge, str)
        or _SHA256.fullmatch(expected_observer_challenge) is None
        or expected_observer_challenge == "0" * 64
        or not isinstance(expected_host_challenge, str)
        or _SHA256.fullmatch(expected_host_challenge) is None
        or expected_host_challenge == "0" * 64
        or expected_observer_challenge == expected_host_challenge
    ):
        raise ValueError("gateway trust independent challenges are required")
    if (
        not isinstance(expected_trusted_keyring_sha256, str)
        or _SHA256.fullmatch(expected_trusted_keyring_sha256) is None
        or expected_trusted_keyring_sha256 == "0" * 64
    ):
        raise ValueError("gateway trust keyring digest is invalid")
    keyring_body = _read_trusted_keyring(
        Path(trusted_keyring_path),
        candidate_root=candidate_contract.root,
        platform_name=trusted_keyring_platform_name,
        trusted_owner_uid=trusted_owner_uid,
    )
    if hashlib.sha256(keyring_body).hexdigest() != expected_trusted_keyring_sha256:
        raise ValueError("gateway trust keyring digest does not match")
    trusted_keys = _trusted_keys(keyring_body)
    observer, observer_key_id, observer_roles = _parse_gateway_trust_envelope_from_keys(
        observer_envelope_body,
        expected_payload_type="gateway-observer",
        trusted_keys=trusted_keys,
        candidate=candidate,
        candidate_contract=candidate_contract_claim,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
        expected_challenge=expected_observer_challenge,
        trusted_now=trusted_now,
    )
    host, host_key_id, host_roles = _parse_gateway_trust_envelope_from_keys(
        host_envelope_body,
        expected_payload_type="gateway-host-provisioner",
        trusted_keys=trusted_keys,
        candidate=candidate,
        candidate_contract=candidate_contract_claim,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
        expected_challenge=expected_host_challenge,
        trusted_now=trusted_now,
    )
    if observer_key_id == host_key_id:
        raise ValueError("gateway trust envelopes must use different keys")
    if observer_roles != frozenset({"gateway-observer"}) or host_roles != frozenset(
        {"gateway-host-provisioner"}
    ):
        raise ValueError("gateway trust keys must each have a single role")
    _gateway_trust_claim(
        observer,
        expected_artifact=_OBSERVER_ARTIFACT,
        artifact_body=observer_artifact_body,
        label="observer",
    )
    _gateway_trust_claim(
        host,
        expected_artifact=_HOST_RECEIPT_ARTIFACT,
        artifact_body=host_receipt_body,
        label="host",
    )
    host_receipt = _parse_gateway_host_receipt(
        host_receipt_body,
        candidate=candidate,
        candidate_contract=candidate_contract_claim,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
    )
    candidate_contract.assert_unchanged()
    return {
        "observer": observer,
        "hostProvisioner": host,
        "hostReceipt": host_receipt,
    }


def _verify_gateway_trust_pair(
    *,
    observer_envelope_body: bytes,
    host_envelope_body: bytes,
    observer_artifact_body: bytes,
    host_receipt_body: bytes,
    trusted_keyring_path: Path,
    expected_trusted_keyring_sha256: str,
    candidate_root: Path,
    release_run: Mapping[str, str],
    expected_environment_id: str,
    expected_observer_challenge: str,
    expected_host_challenge: str,
    trusted_now: str,
    trusted_keyring_platform_name: str,
    trusted_owner_uid: int | None = None,
) -> dict[str, object]:
    candidate_contract = _load_candidate_contract(Path(candidate_root))
    with candidate_contract:
        verified = _verify_gateway_trust_pair_under_lease(
            candidate_contract=candidate_contract,
            observer_envelope_body=observer_envelope_body,
            host_envelope_body=host_envelope_body,
            observer_artifact_body=observer_artifact_body,
            host_receipt_body=host_receipt_body,
            trusted_keyring_path=trusted_keyring_path,
            expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
            release_run=release_run,
            expected_environment_id=expected_environment_id,
            expected_observer_challenge=expected_observer_challenge,
            expected_host_challenge=expected_host_challenge,
            trusted_now=trusted_now,
            trusted_keyring_platform_name=trusted_keyring_platform_name,
            trusted_owner_uid=trusted_owner_uid,
        )
        candidate_contract.assert_current()
    return verified


def verify_gateway_trust_pair(
    *,
    observer_envelope_body: bytes,
    host_envelope_body: bytes,
    observer_artifact_body: bytes,
    host_receipt_body: bytes,
    trusted_keyring_path: Path,
    expected_trusted_keyring_sha256: str,
    candidate_root: Path,
    release_run: Mapping[str, str],
    expected_environment_id: str,
    expected_observer_challenge: str,
    expected_host_challenge: str,
    trusted_now: str,
) -> dict[str, object]:
    """Verify a trust pair using the current process' immutable OS policy."""

    trusted_owner_uid = os.geteuid() if os.name == "posix" and hasattr(os, "geteuid") else None
    return _verify_gateway_trust_pair(
        observer_envelope_body=observer_envelope_body,
        host_envelope_body=host_envelope_body,
        observer_artifact_body=observer_artifact_body,
        host_receipt_body=host_receipt_body,
        trusted_keyring_path=trusted_keyring_path,
        expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
        candidate_root=candidate_root,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
        expected_observer_challenge=expected_observer_challenge,
        expected_host_challenge=expected_host_challenge,
        trusted_now=trusted_now,
        trusted_keyring_platform_name=os.name,
        trusted_owner_uid=trusted_owner_uid,
    )


__all__ = [
    "MAX_GATEWAY_TRUST_AGE_SECONDS",
    "MAX_GATEWAY_TRUST_ENVELOPE_BYTES",
    "MAX_GATEWAY_TRUST_FUTURE_SKEW_SECONDS",
    "MAX_GATEWAY_TRUST_KEYRING_BYTES",
    "MAX_GATEWAY_TRUST_TTL_SECONDS",
    "verify_gateway_trust_pair",
]
