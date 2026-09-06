#!/usr/bin/env python
"""Produce role-separated Gateway trust artifacts for first-release evidence."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
from typing import BinaryIO
import warnings

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SCRIPTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_ROOT.parent
for import_root in (PROJECT_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from gateway_public_contract import (  # noqa: E402
    GATEWAY_OBSERVER_ATTESTATION_PRODUCER,
    GATEWAY_OBSERVER_ATTESTATION_SCHEMA_VERSION,
    MAX_GATEWAY_PUBLIC_REPORT_BYTES,
    _canonical_json_object,
    _validate_candidate,
    _validate_release_run,
    gateway_public_command_record,
    parse_gateway_observer_attestation,
    validate_gateway_public_report,
)
from gateway_trust_contract import (  # noqa: E402
    MAX_GATEWAY_TRUST_AGE_SECONDS,
    MAX_GATEWAY_TRUST_FUTURE_SKEW_SECONDS,
    MAX_GATEWAY_TRUST_TTL_SECONDS,
    _parse_gateway_host_receipt,
    _utc_timestamp,
    validate_gateway_candidate_contract_bytes,
)
from render_platform_compose import (  # noqa: E402
    candidate_artifact_paths,
    validate_image_lock_bindings,  # noqa: F401 - retained test seam
)

MachineIdReader = Callable[[], bytes]
DockerResolver = Callable[[], Path]
DockerSocketStatReader = Callable[[Path], object]
HostAuthorityRunner = Callable[..., subprocess.CompletedProcess[str]]
FileIdentity = tuple[int, int, int, int, int, int, int | None]
CandidateArtifactIdentity = tuple[Path, FileIdentity, str]
_MACHINE_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHYSICAL_HOST_ID_DOMAIN = b"yfeistai.gateway-physical-host-id.v1\0"
_SIGNATURE_DOMAIN = b"yfeistai.gateway-trust-envelope.v2\0"
_WINDOWS_REPARSE_POINT = 0x400
_MAX_KEY_BYTES = 16 * 1024
_GATEWAY_DOCKER_EXECUTABLE = "/usr/bin/docker"
_GATEWAY_DOCKER_CONTEXT = "default"
_FIXED_GATEWAY_DOCKER_CONFIG = "/etc/yfeistai/docker-authority"
_GATEWAY_DOCKER_CONFIG = _FIXED_GATEWAY_DOCKER_CONFIG
_GATEWAY_DOCKER_LOCAL_ENDPOINT = "unix:///var/run/docker.sock"
_GATEWAY_DOCKER_LOCAL_SOCKET = Path("/var/run/docker.sock")
_GATEWAY_DOCKER_TIMEOUT_SECONDS = 10
_MAX_GATEWAY_DOCKER_COMMAND_OUTPUT_BYTES = 128 * 1024
_MAX_GATEWAY_CANDIDATE_CONTRACT_BYTES = 2 * 1024 * 1024
_LINUX_MACHINE_ID_PATH = Path("/etc/machine-id")
_CANDIDATE_ARTIFACT_RELATIVE_PATHS = (
    "deploy/image-lock.json",
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.data-plane.yml",
    "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
)


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    return stat.S_ISLNK(file_stat.st_mode) or bool(
        getattr(file_stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _assert_safe_existing_path(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            file_stat = current.lstat()
        except OSError:
            raise ValueError(f"{label} is unavailable") from None
        if _is_reparse_point(file_stat):
            raise ValueError(f"{label} path is unsafe")


def _assert_private_key_permissions(
    file_stat: object,
    *,
    platform_name: str,
    effective_user_id: int | None,
) -> None:
    if platform_name == "nt":
        raise ValueError("Windows private key DACL cannot be verified")
    if (
        effective_user_id is None
        or getattr(file_stat, "st_uid", None) != effective_user_id
        or stat.S_IMODE(getattr(file_stat, "st_mode")) & 0o077
    ):
        raise ValueError("gateway private key permissions are not restricted")


def _file_identity(file_stat: object) -> FileIdentity:
    return (
        getattr(file_stat, "st_dev"),
        getattr(file_stat, "st_ino"),
        getattr(file_stat, "st_size"),
        getattr(file_stat, "st_mtime_ns"),
        getattr(file_stat, "st_ctime_ns"),
        getattr(file_stat, "st_mode"),
        getattr(file_stat, "st_uid", None),
    )


def _read_regular_file_no_follow(
    path: Path,
    *,
    label: str,
    private: bool = False,
    max_bytes: int = _MAX_KEY_BYTES,
) -> bytes:
    source = Path(path)
    _assert_safe_existing_path(source, label=label)
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= max_bytes:
        raise ValueError(f"{label} is invalid")
    if private:
        _assert_private_key_permissions(
            before,
            platform_name=os.name,
            effective_user_id=None if os.name == "nt" else os.geteuid(),
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError:
        raise ValueError(f"{label} is unavailable") from None
    try:
        opened = os.fstat(descriptor)
        identity = _file_identity(before)
        if (
            _is_reparse_point(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _file_identity(opened) != identity
        ):
            raise ValueError(f"{label} changed while reading")
        if private:
            _assert_private_key_permissions(
                opened,
                platform_name=os.name,
                effective_user_id=None if os.name == "nt" else os.geteuid(),
            )
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError(f"{label} changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"{label} changed while reading")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = source.lstat()
    if _file_identity(after) != identity or _file_identity(current) != identity:
        raise ValueError(f"{label} changed while reading")
    if private:
        _assert_private_key_permissions(
            after,
            platform_name=os.name,
            effective_user_id=None if os.name == "nt" else os.geteuid(),
        )
        _assert_private_key_permissions(
            current,
            platform_name=os.name,
            effective_user_id=None if os.name == "nt" else os.geteuid(),
        )
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class _CandidateArtifactLease:
    path: Path
    descriptor: int
    identity: FileIdentity


@dataclass(frozen=True, slots=True)
class _CandidateContract:
    root: Path
    root_identity: FileIdentity
    artifact_identities: tuple[CandidateArtifactIdentity, ...]
    candidate_canonical: bytes
    leases: tuple[_CandidateArtifactLease, ...] = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def candidate(self) -> dict[str, object]:
        candidate = json.loads(self.candidate_canonical)
        if not isinstance(candidate, dict):  # pragma: no cover - construction invariant
            raise ValueError("gateway candidate contract is invalid")
        return candidate

    @property
    def claim(self) -> dict[str, object]:
        artifacts = {
            path.relative_to(self.root).as_posix(): {"sha256": sha256}
            for path, _identity, sha256 in self.artifact_identities
        }
        if tuple(artifacts) != _CANDIDATE_ARTIFACT_RELATIVE_PATHS:
            raise ValueError("gateway candidate contract artifact set is invalid")
        return {"schemaVersion": 1, "artifacts": artifacts}

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
                current_root, current_artifacts = _candidate_contract_state(
                    self.root,
                    tuple(path for path, _identity, _sha256 in self.artifact_identities),
                )
                if (
                    current_root != self.root_identity
                    or current_artifacts != self.artifact_identities
                ):
                    raise ValueError
            else:
                _assert_candidate_artifact_leases_current(
                    self.root,
                    self.root_identity,
                    self.leases,
                    tuple(sha256 for _path, _identity, sha256 in self.artifact_identities),
                )
        except ValueError:
            raise ValueError(
                "gateway candidate contract changed during host authority operation"
            ) from None

    def assert_unchanged(self) -> None:
        self.assert_current()


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
    source = Path(path)
    _assert_safe_existing_path(source, label="gateway candidate contract artifact")
    try:
        before = source.lstat()
    except OSError:
        raise ValueError("gateway candidate contract artifact is unavailable") from None
    if (
        not stat.S_ISREG(before.st_mode)
        or not 0 < before.st_size <= _MAX_GATEWAY_CANDIDATE_CONTRACT_BYTES
    ):
        raise ValueError("gateway candidate contract artifact is invalid")
    try:
        descriptor = _open_candidate_contract_descriptor(source)
    except OSError:
        raise ValueError("gateway candidate contract artifact is unavailable") from None
    keep_open = False
    try:
        opened = os.fstat(descriptor)
        identity = _file_identity(before)
        if (
            _is_reparse_point(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _file_identity(opened) != identity
        ):
            raise ValueError("gateway candidate contract artifact changed while reading")
        lease = _CandidateArtifactLease(
            path=source,
            descriptor=descriptor,
            identity=identity,
        )
        keep_open = True
        return lease
    finally:
        if not keep_open:
            os.close(descriptor)


def _read_candidate_contract_artifact(lease: _CandidateArtifactLease) -> bytes:
    try:
        os.lseek(lease.descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = lease.identity[2]
        while remaining:
            chunk = os.read(lease.descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError("gateway candidate contract artifact changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(lease.descriptor, 1):
            raise ValueError("gateway candidate contract artifact changed while reading")
    except OSError:
        raise ValueError("gateway candidate contract artifact changed while reading") from None
    return b"".join(chunks)


def _assert_candidate_artifact_leases_current(
    root: Path,
    root_identity: FileIdentity,
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
            _assert_safe_existing_path(
                lease.path,
                label="gateway candidate contract artifact",
            )
            current = lease.path.lstat()
            if (
                _is_reparse_point(opened)
                or _is_reparse_point(current)
                or not stat.S_ISREG(current.st_mode)
                or _file_identity(opened) != lease.identity
                or _file_identity(current) != lease.identity
            ):
                raise ValueError("gateway candidate contract artifact changed while reading")
        _assert_safe_existing_path(root, label="gateway candidate root")
        current_root = root.lstat()
        if not stat.S_ISDIR(current_root.st_mode) or _file_identity(current_root) != root_identity:
            raise ValueError("gateway candidate contract artifact changed while reading")
    except (OSError, ValueError):
        raise ValueError("gateway candidate contract artifact changed while reading") from None


def _candidate_contract_state(
    root: Path,
    artifact_paths: tuple[Path, ...],
) -> tuple[FileIdentity, tuple[CandidateArtifactIdentity, ...]]:
    leases: list[_CandidateArtifactLease] = []
    try:
        _assert_safe_existing_path(root, label="gateway candidate root")
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("gateway candidate root is invalid")
        root_identity = _file_identity(root_stat)
        for path in artifact_paths:
            leases.append(_open_candidate_contract_artifact(path))
        artifacts = tuple(
            (
                lease.path,
                lease.identity,
                hashlib.sha256(_read_candidate_contract_artifact(lease)).hexdigest(),
            )
            for lease in leases
        )
        _assert_candidate_artifact_leases_current(
            root,
            root_identity,
            tuple(leases),
            tuple(sha256 for _path, _identity, sha256 in artifacts),
        )
    except OSError:
        raise ValueError("gateway candidate contract is unavailable or invalid") from None
    finally:
        for lease in reversed(leases):
            os.close(lease.descriptor)
    return root_identity, artifacts


def _load_candidate_contract(candidate_root: Path) -> _CandidateContract:
    root = Path(candidate_root)
    _assert_safe_existing_path(root, label="gateway candidate root")
    if not root.is_dir():
        raise ValueError("gateway candidate root is invalid")
    resolved_root = root.resolve(strict=True)
    paths = candidate_artifact_paths(resolved_root)
    artifact_paths = (
        paths.image_lock,
        paths.base_compose,
        paths.platform_compose,
        paths.data_plane_compose,
        paths.observer_overlay,
    )
    _assert_safe_existing_path(resolved_root, label="gateway candidate root")
    root_stat = resolved_root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("gateway candidate root is invalid")
    root_identity = _file_identity(root_stat)
    leases: list[_CandidateArtifactLease] = []
    keep_open = False
    try:
        for path in artifact_paths:
            leases.append(_open_candidate_contract_artifact(path))
        bodies = tuple(_read_candidate_contract_artifact(lease) for lease in leases)
        artifact_identities = tuple(
            (
                lease.path,
                lease.identity,
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
            raw_candidate = validated.get("candidate")
            candidate_claim = validated.get("candidateContract")
            candidate = (
                _validate_candidate(dict(raw_candidate))
                if isinstance(raw_candidate, dict)
                else None
            )
            expected_claim = {
                "schemaVersion": 1,
                "artifacts": {
                    path.relative_to(resolved_root).as_posix(): {"sha256": sha256}
                    for path, _identity, sha256 in artifact_identities
                },
            }
            if candidate is None or candidate_claim != expected_claim:
                raise ValueError
        except (OSError, TypeError, ValueError):
            raise ValueError("gateway candidate contract is unavailable or invalid") from None
        try:
            _assert_candidate_artifact_leases_current(
                resolved_root,
                root_identity,
                tuple(leases),
                tuple(sha256 for _path, _identity, sha256 in artifact_identities),
            )
        except ValueError:
            raise ValueError(
                "gateway candidate contract changed during host authority operation"
            ) from None
        contract = _CandidateContract(
            root=resolved_root,
            root_identity=root_identity,
            artifact_identities=artifact_identities,
            candidate_canonical=_canonical_json(candidate),
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


def _host_authority_output_path(path: Path, *, candidate_root: Path) -> Path:
    output = Path(path)
    if not output.is_absolute():
        raise ValueError("gateway host authority output path must be absolute")
    _assert_safe_existing_path(output.parent, label="gateway host authority output parent")
    if not output.parent.is_dir():
        raise ValueError("gateway host authority output parent is invalid")
    resolved_output = output.parent.resolve(strict=True) / output.name
    if resolved_output == candidate_root or candidate_root in resolved_output.parents:
        raise ValueError("gateway host authority output must be outside candidate root")
    return resolved_output


def _canonical_json(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _read_linux_machine_id() -> bytes:
    return _read_regular_file_no_follow(
        _LINUX_MACHINE_ID_PATH,
        label="Linux machine-id",
        max_bytes=128,
    )


def _resolve_fixed_docker_executable() -> Path:
    executable = Path(_GATEWAY_DOCKER_EXECUTABLE)
    _assert_safe_existing_path(executable, label="gateway Docker executable")
    file_stat = executable.lstat()
    if (
        os.name == "nt"
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != 0
        or stat.S_IMODE(file_stat.st_mode) & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise ValueError("fixed gateway Docker executable is unavailable")
    return executable


def _read_fixed_docker_socket_stat(path: Path) -> object:
    if Path(path) != _GATEWAY_DOCKER_LOCAL_SOCKET:
        raise ValueError("gateway Docker socket is unavailable or invalid")
    try:
        return Path(path).lstat()
    except OSError:
        raise ValueError("gateway Docker socket is unavailable or invalid") from None


def _gateway_docker_socket_identity(
    reader: DockerSocketStatReader,
) -> FileIdentity:
    try:
        socket_stat = reader(_GATEWAY_DOCKER_LOCAL_SOCKET)
        identity = _file_identity(socket_stat)
    except (OSError, TypeError, ValueError):
        raise ValueError("gateway Docker socket is unavailable or invalid") from None
    if (
        _is_reparse_point(socket_stat)
        or not stat.S_ISSOCK(getattr(socket_stat, "st_mode", 0))
        or getattr(socket_stat, "st_uid", None) != 0
        or any(type(value) is not int for value in identity)
    ):
        raise ValueError("gateway Docker socket is unavailable or invalid")
    return identity


def _gateway_docker_config_identity(path: Path) -> tuple[tuple[str, FileIdentity], ...]:
    root = Path(path)
    _assert_safe_existing_path(root, label="gateway Docker authority config")
    try:
        root_stat = root.lstat()
    except OSError:
        raise ValueError("gateway Docker authority config is unavailable") from None
    if (
        os.name == "nt"
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != 0
        or stat.S_IMODE(root_stat.st_mode) & 0o022
    ):
        raise ValueError("gateway Docker authority config is unavailable")
    identities: list[tuple[str, FileIdentity]] = [(".", _file_identity(root_stat))]
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current = Path(current_root)
        for name in (*directory_names, *file_names):
            child = current / name
            child_stat = child.lstat()
            if (
                _is_reparse_point(child_stat)
                or not (stat.S_ISDIR(child_stat.st_mode) or stat.S_ISREG(child_stat.st_mode))
                or child_stat.st_uid != 0
                or stat.S_IMODE(child_stat.st_mode) & 0o022
            ):
                raise ValueError("gateway Docker authority config is unavailable")
            identities.append((child.relative_to(root).as_posix(), _file_identity(child_stat)))
    return tuple(identities)


def _run_gateway_host_authority_command(
    arguments: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        arguments,
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
        process.kill()
        process.wait()
        raise ValueError("gateway Docker authority command failed")

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total_bytes = 0
    output_lock = threading.Lock()
    overflow = threading.Event()
    reader_errors: list[OSError] = []

    def drain(label: str, stream: BinaryIO) -> None:
        nonlocal total_bytes
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    raise OSError("gateway Docker authority output is invalid")
                with output_lock:
                    if total_bytes + len(chunk) > _MAX_GATEWAY_DOCKER_COMMAND_OUTPUT_BYTES:
                        overflow.set()
                    else:
                        buffers[label].extend(chunk)
                        total_bytes += len(chunk)
                if overflow.is_set():
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
        except OSError as exc:
            reader_errors.append(exc)
            try:
                process.kill()
            except OSError:
                pass

    readers = tuple(
        threading.Thread(target=drain, args=entry, daemon=True)
        for entry in (("stdout", process.stdout), ("stderr", process.stderr))
    )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join()
        if overflow.is_set():
            raise ValueError("gateway Docker authority output size is invalid") from None
        raise
    for reader in readers:
        reader.join()
    if reader_errors:
        raise ValueError("gateway Docker authority output is invalid") from None
    if overflow.is_set():
        raise ValueError("gateway Docker authority output size is invalid")
    try:
        stdout = bytes(buffers["stdout"]).decode("utf-8")
        stderr = bytes(buffers["stderr"]).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("gateway Docker authority output is invalid") from None
    return subprocess.CompletedProcess(
        arguments,
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _gateway_host_authority_json(
    arguments: tuple[str, ...],
    *,
    runner: HostAuthorityRunner,
) -> object:
    try:
        completed = runner(
            arguments,
            env={"DOCKER_CONFIG": _GATEWAY_DOCKER_CONFIG, "LC_ALL": "C"},
            timeout=_GATEWAY_DOCKER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("gateway Docker authority command failed") from None
    if (
        type(getattr(completed, "returncode", None)) is not int
        or completed.returncode != 0
        or not isinstance(getattr(completed, "stdout", None), str)
    ):
        raise ValueError("gateway Docker authority command failed")
    stderr = getattr(completed, "stderr", "")
    if not isinstance(stderr, str):
        raise ValueError("gateway Docker authority command failed")
    try:
        stdout_size = len(completed.stdout.encode("utf-8"))
        stderr_size = len(stderr.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("gateway Docker authority output is invalid") from None
    if stdout_size + stderr_size > _MAX_GATEWAY_DOCKER_COMMAND_OUTPUT_BYTES:
        raise ValueError("gateway Docker authority output size is invalid")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise ValueError("gateway Docker authority output is invalid") from None


def _collect_gateway_host_authority_facts(
    *,
    machine_id_reader: MachineIdReader = _read_linux_machine_id,
    docker_resolver: DockerResolver = _resolve_fixed_docker_executable,
    docker_socket_stat_reader: DockerSocketStatReader | None = None,
    runner: HostAuthorityRunner = _run_gateway_host_authority_command,
) -> dict[str, object]:
    """Collect two stable observations from the fixed Linux Docker authority."""

    endpoint_command = (
        _GATEWAY_DOCKER_EXECUTABLE,
        "context",
        "inspect",
        _GATEWAY_DOCKER_CONTEXT,
        "--format",
        "{{json .Endpoints.docker.Host}}",
    )
    info_command = (
        _GATEWAY_DOCKER_EXECUTABLE,
        "--context",
        _GATEWAY_DOCKER_CONTEXT,
        "info",
        "--format",
        "{{json .}}",
    )

    formal_sources = (
        machine_id_reader is _read_linux_machine_id
        and docker_resolver is _resolve_fixed_docker_executable
        and docker_socket_stat_reader is None
        and runner is _run_gateway_host_authority_command
    )
    effective_socket_stat_reader = (
        _read_fixed_docker_socket_stat if formal_sources else docker_socket_stat_reader
    )
    observations: list[dict[str, object]] = []
    for round_index in range(2):
        docker = Path(docker_resolver())
        docker_path = docker.as_posix()
        docker_config = _GATEWAY_DOCKER_CONFIG
        if round_index == 0 and (
            docker_path != _GATEWAY_DOCKER_EXECUTABLE
            or docker_config != _FIXED_GATEWAY_DOCKER_CONFIG
        ):
            raise ValueError("fixed gateway Docker authority is unavailable")
        if formal_sources:
            executable_before = _file_identity(docker.lstat())
            config_before: object = _gateway_docker_config_identity(Path(docker_config))
        else:
            executable_before = docker_path
            config_before = docker_config
        socket_before = (
            _gateway_docker_socket_identity(effective_socket_stat_reader)
            if effective_socket_stat_reader is not None
            else None
        )
        physical_host_id = derive_physical_host_id_sha256(machine_id_reader=machine_id_reader)
        endpoint = _gateway_host_authority_json(endpoint_command, runner=runner)
        info = _gateway_host_authority_json(info_command, runner=runner)
        if (
            not isinstance(endpoint, str)
            or not endpoint
            or endpoint != endpoint.strip()
            or not isinstance(info, dict)
        ):
            raise ValueError("gateway Docker authority output is invalid")
        server_id = info.get("ID")
        os_type = info.get("OSType")
        if (
            not isinstance(server_id, str)
            or not server_id
            or server_id != server_id.strip()
            or os_type != "linux"
        ):
            raise ValueError("gateway Docker authority output is invalid")
        if docker_config != _GATEWAY_DOCKER_CONFIG:
            raise ValueError(
                "gateway Docker authority sources changed between authority observations"
            )
        if formal_sources:
            executable_after: object = _file_identity(docker.lstat())
            config_after: object = _gateway_docker_config_identity(Path(docker_config))
            if executable_after != executable_before or config_after != config_before:
                raise ValueError(
                    "gateway Docker authority sources changed between authority observations"
                )
        socket_after = (
            _gateway_docker_socket_identity(effective_socket_stat_reader)
            if effective_socket_stat_reader is not None
            else None
        )
        if socket_after != socket_before:
            raise ValueError("gateway Docker socket changed between authority observations")
        observations.append(
            {
                "physicalHostIdSha256": physical_host_id,
                "dockerExecutable": docker_path,
                "dockerConfig": docker_config,
                "dockerExecutableIdentity": executable_before,
                "dockerConfigIdentity": config_before,
                "dockerSocketIdentity": socket_before,
                "dockerEndpoint": endpoint,
                "dockerServerId": server_id,
                "osType": os_type,
            }
        )
    if observations[0] != observations[1]:
        raise ValueError(
            "gateway Docker authority sources or facts changed between authority observations"
        )
    observation = observations[0]
    if (
        observation["dockerExecutable"] != _GATEWAY_DOCKER_EXECUTABLE
        or observation["dockerConfig"] != _FIXED_GATEWAY_DOCKER_CONFIG
    ):
        raise ValueError("gateway Docker authority sources changed between authority observations")
    if observation["dockerEndpoint"] != _GATEWAY_DOCKER_LOCAL_ENDPOINT:
        raise ValueError("gateway Docker authority requires a fixed local Docker endpoint")
    return {
        "physicalHostIdSha256": observation["physicalHostIdSha256"],
        "dockerContext": _GATEWAY_DOCKER_CONTEXT,
        "dockerEndpoint": observation["dockerEndpoint"],
        "dockerServerId": observation["dockerServerId"],
        "osType": observation["osType"],
    }


def derive_physical_host_id_sha256(
    *, machine_id_reader: MachineIdReader = _read_linux_machine_id
) -> str:
    """Derive the replayable v1 host pseudonym from Linux machine-id."""

    raw = machine_id_reader()
    if not isinstance(raw, bytes):
        raise ValueError("Linux machine-id bytes are invalid")
    try:
        normalized = raw.decode("ascii").strip().lower()
    except UnicodeDecodeError:
        raise ValueError("Linux machine-id is invalid") from None
    if _MACHINE_ID.fullmatch(normalized) is None or normalized == "0" * 32:
        raise ValueError("Linux machine-id is invalid")
    return hashlib.sha256(_PHYSICAL_HOST_ID_DOMAIN + normalized.encode("ascii")).hexdigest()


def build_gateway_host_receipt(
    *,
    candidate_root: Path,
    release_run: Mapping[str, str],
    docker_endpoint: str,
    docker_server_id: str,
    machine_id_reader: MachineIdReader = _read_linux_machine_id,
) -> bytes:
    """Build one canonical candidate/run-bound Linux Docker host receipt."""

    with _load_candidate_contract(Path(candidate_root)) as contract:
        body = _build_gateway_host_receipt_from_facts(
            candidate=contract.candidate,
            candidate_contract=contract.claim,
            release_run=release_run,
            host={
                "physicalHostIdSha256": derive_physical_host_id_sha256(
                    machine_id_reader=machine_id_reader
                ),
                "dockerContext": _GATEWAY_DOCKER_CONTEXT,
                "dockerEndpoint": docker_endpoint,
                "dockerServerId": docker_server_id,
                "osType": "linux",
            },
        )
        contract.assert_current()
    return body


def _build_gateway_host_receipt_from_facts(
    *,
    candidate: Mapping[str, object],
    candidate_contract: Mapping[str, object],
    release_run: Mapping[str, str],
    host: Mapping[str, object],
) -> bytes:
    environment_id = release_run.get("environmentId")
    document: dict[str, object] = {
        "schemaVersion": 2,
        "producer": "gateway-docker-host-provisioner",
        "candidate": dict(candidate),
        "candidateContract": dict(candidate_contract),
        "releaseRun": dict(release_run),
        "environmentId": environment_id,
        "host": dict(host),
    }
    body = _canonical_json(document)
    _parse_gateway_host_receipt(
        body,
        candidate=candidate,
        candidate_contract=candidate_contract,
        release_run=release_run,
        expected_environment_id=str(environment_id),
    )
    return body


def build_gateway_observer_attestation(
    raw_observation_body: bytes,
    *,
    observed_at: str,
) -> bytes:
    """Bind one canonical successful raw probe into the v1 observer attestation."""

    report = _canonical_json_object(
        raw_observation_body,
        label="external observation",
        limit=MAX_GATEWAY_PUBLIC_REPORT_BYTES,
    )
    candidate = report.get("candidate")
    release_run = report.get("releaseRun")
    runtime = report.get("runtimeAttestation")
    observer = report.get("observer")
    target = report.get("target")
    if (
        not isinstance(candidate, dict)
        or not isinstance(release_run, dict)
        or not isinstance(runtime, dict)
        or not isinstance(observer, dict)
        or not isinstance(target, dict)
    ):
        raise ValueError("gateway raw observation bindings are invalid")
    runtime_sha256 = runtime.get("sha256")
    observer_id = observer.get("observerId")
    observer_origin = observer.get("origin")
    base_url = report.get("baseUrl")
    if not all(
        isinstance(value, str) for value in (runtime_sha256, observer_id, observer_origin, base_url)
    ):
        raise ValueError("gateway raw observation bindings are invalid")
    validate_gateway_public_report(
        report,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
        expected_runtime_attestation_sha256=runtime_sha256,
        expected_observer_id=observer_id,
        expected_observer_origin=observer_origin,
    )
    raw_sha256 = hashlib.sha256(raw_observation_body).hexdigest()
    document: dict[str, object] = {
        "schemaVersion": GATEWAY_OBSERVER_ATTESTATION_SCHEMA_VERSION,
        "producer": GATEWAY_OBSERVER_ATTESTATION_PRODUCER,
        "candidate": candidate,
        "releaseRun": release_run,
        "observedAt": observed_at,
        "observer": observer,
        "target": {
            "origin": base_url,
            "expectedTlsHostname": target.get("hostname"),
            "resolvedAddresses": target.get("resolvedAddresses"),
        },
        "runtimeAttestation": runtime,
        "externalObservation": {
            "artifact": "raw/gateway-public-observation.json",
            "sha256": raw_sha256,
        },
        "execution": {
            "command": gateway_public_command_record(),
            "nativeExit": 0,
            "stdoutSha256": raw_sha256,
            "stderr": "",
            "stderrSha256": hashlib.sha256(b"").hexdigest(),
        },
    }
    body = _canonical_json(document)
    parse_gateway_observer_attestation(
        body,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
        expected_runtime_attestation_sha256=runtime_sha256,
        expected_external_observation_sha256=raw_sha256,
        expected_observer_id=observer_id,
        expected_observer_origin=observer_origin,
        expected_attestation_sha256=hashlib.sha256(body).hexdigest(),
    )
    return body


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    body = _read_regular_file_no_follow(path, label="gateway private key", private=True)
    try:
        private_key = serialization.load_pem_private_key(body, password=None)
    except (TypeError, ValueError):
        raise ValueError("gateway private key is invalid") from None
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("gateway private key is invalid")
    canonical = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    if body != canonical:
        raise ValueError("gateway private key is not canonical PKCS8 PEM")
    return private_key


def _public_key_entry(public_key_body: bytes, *, payload_type: str) -> dict[str, object]:
    if not isinstance(public_key_body, bytes) or not 0 < len(public_key_body) <= _MAX_KEY_BYTES:
        raise ValueError("gateway public key is invalid")
    try:
        public_key = serialization.load_pem_public_key(public_key_body)
    except (TypeError, ValueError):
        raise ValueError("gateway public key is invalid") from None
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("gateway public key is invalid")
    canonical_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if public_key_body != canonical_pem:
        raise ValueError("gateway public key is not canonical PEM")
    spki = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return {
        "algorithm": "ed25519",
        "keyId": hashlib.sha256(spki).hexdigest(),
        "payloadTypes": [payload_type],
        "publicKeySpkiBase64": base64.b64encode(spki).decode("ascii"),
    }


def build_gateway_trust_keyring(
    *,
    observer_public_key_body: bytes,
    host_public_key_body: bytes,
) -> bytes:
    """Build the canonical role-separated keyring from public keys only."""

    observer = _public_key_entry(
        observer_public_key_body,
        payload_type="gateway-observer",
    )
    host = _public_key_entry(
        host_public_key_body,
        payload_type="gateway-host-provisioner",
    )
    if observer["keyId"] == host["keyId"]:
        raise ValueError("gateway trust roles require different public keys")
    return _canonical_json({"schemaVersion": 1, "keys": [observer, host]})


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class GatewayTrustCommittedError(RuntimeError):
    """The requested artifact was linked, but post-commit reconciliation failed."""

    def __init__(self, message: str, output_path: Path) -> None:
        self.output_path = Path(output_path)
        super().__init__(f"gateway trust artifact committed but {message}: {self.output_path}")


def _assert_candidate_contract_unchanged(
    contract: _CandidateContract,
    *,
    committed_output: Path | None = None,
) -> None:
    try:
        contract.assert_unchanged()
    except ValueError as error:
        if committed_output is not None:
            raise GatewayTrustCommittedError(
                "candidate contract changed during publication",
                committed_output,
            ) from error
        raise


def _directory_identity(file_stat: object) -> tuple[int, int, int, int | None]:
    return (
        getattr(file_stat, "st_dev"),
        getattr(file_stat, "st_ino"),
        getattr(file_stat, "st_mode"),
        getattr(file_stat, "st_uid", None),
    )


def _assert_formal_publication_authority() -> None:
    if os.name == "nt":
        raise ValueError("authoritative Windows no-replace publication is unavailable")


def _validate_output_boundary(path: Path) -> tuple[Path, tuple[int, int, int, int | None]]:
    output = Path(path)
    if not output.is_absolute():
        raise ValueError("gateway trust output path must be absolute")
    _assert_safe_existing_path(output.parent, label="gateway trust output parent")
    parent_stat = output.parent.lstat()
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("gateway trust output parent is invalid")
    if os.name != "nt" and (
        parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) & 0o022
    ):
        raise ValueError("gateway trust output parent is not restricted")
    if os.path.lexists(output):
        raise FileExistsError(output)
    return output, _directory_identity(parent_stat)


def _publish_no_replace_portable(path: Path, body: bytes) -> None:
    output, parent_identity = _validate_output_boundary(path)
    descriptor, staging_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    staging = Path(staging_name)
    descriptor_open = True
    committed = False
    try:
        if os.name != "nt":
            os.chmod(staging, 0o600)
        handle = os.fdopen(descriptor, "wb")
        descriptor_open = False
        with handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        staged = staging.lstat()
        if (
            _is_reparse_point(staged)
            or not stat.S_ISREG(staged.st_mode)
            or staged.st_size != len(body)
            or (os.name != "nt" and stat.S_IMODE(staged.st_mode) != 0o600)
        ):
            raise RuntimeError("gateway trust staging file is invalid")
        if _directory_identity(output.parent.lstat()) != parent_identity:
            raise RuntimeError("gateway trust output parent identity changed")
        os.link(staging, output)
        committed = True
        published = output.lstat()
        if (
            _is_reparse_point(published)
            or not stat.S_ISREG(published.st_mode)
            or (published.st_dev, published.st_ino) != (staged.st_dev, staged.st_ino)
            or output.read_bytes() != body
        ):
            raise GatewayTrustCommittedError("target reconciliation failed", output)
        if _directory_identity(output.parent.lstat()) != parent_identity:
            try:
                if output.lstat().st_ino == staged.st_ino:
                    output.unlink()
                    committed = False
            except OSError:
                pass
            raise RuntimeError("gateway trust output parent identity changed")
        try:
            _fsync_directory(output.parent)
        except OSError as error:
            raise GatewayTrustCommittedError("directory durability failed", output) from error
    except GatewayTrustCommittedError:
        raise
    except BaseException as error:
        if committed:
            raise GatewayTrustCommittedError("post-link reconciliation failed", output) from error
        raise
    finally:
        primary_failure = sys.exception()
        if descriptor_open:
            try:
                os.close(descriptor)
            except BaseException as cleanup_error:
                if primary_failure is not None:
                    primary_failure.add_note(f"staging descriptor cleanup failed: {cleanup_error}")
                else:
                    raise
        try:
            staging.unlink(missing_ok=True)
        except BaseException as cleanup_error:
            if primary_failure is not None:
                primary_failure.add_note(f"staging cleanup failed: {cleanup_error}")
            elif committed:
                warnings.warn(
                    f"gateway trust staging cleanup failed: {cleanup_error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            else:
                raise


def _publish_no_replace_posix(path: Path, body: bytes) -> None:
    output, parent_identity = _validate_output_boundary(path)
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(output.parent, parent_flags)
    staging_name = f".{output.name}.{secrets.token_hex(16)}.tmp"
    staging_fd: int | None = None
    committed = False
    try:
        if _directory_identity(os.fstat(parent_fd)) != parent_identity:
            raise ValueError("gateway trust output parent identity changed")
        staging_fd = os.open(
            staging_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(body)
        while view:
            written = os.write(staging_fd, view)
            if written <= 0:
                raise OSError("gateway trust staging write failed")
            view = view[written:]
        os.fsync(staging_fd)
        staged = os.fstat(staging_fd)
        if (
            not stat.S_ISREG(staged.st_mode)
            or staged.st_size != len(body)
            or stat.S_IMODE(staged.st_mode) != 0o600
        ):
            raise RuntimeError("gateway trust staging file is invalid")
        os.close(staging_fd)
        staging_fd = None
        if (
            _directory_identity(os.fstat(parent_fd)) != parent_identity
            or _directory_identity(output.parent.lstat()) != parent_identity
        ):
            raise RuntimeError("gateway trust output parent identity changed")
        os.link(
            staging_name,
            output.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        committed = True
        published_fd = os.open(
            output.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            published = os.fstat(published_fd)
            chunks: list[bytes] = []
            remaining = published.st_size
            while remaining:
                chunk = os.read(published_fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(published_fd)
        if (published.st_dev, published.st_ino) != (staged.st_dev, staged.st_ino) or b"".join(
            chunks
        ) != body:
            raise GatewayTrustCommittedError("target reconciliation failed", output)
        if (
            _directory_identity(os.fstat(parent_fd)) != parent_identity
            or _directory_identity(output.parent.lstat()) != parent_identity
        ):
            raise GatewayTrustCommittedError("output parent identity changed", output)
        try:
            os.fsync(parent_fd)
        except OSError as error:
            raise GatewayTrustCommittedError("directory durability failed", output) from error
    except GatewayTrustCommittedError:
        raise
    except BaseException as error:
        if committed:
            raise GatewayTrustCommittedError("post-link reconciliation failed", output) from error
        raise
    finally:
        primary_failure = sys.exception()
        if staging_fd is not None:
            try:
                os.close(staging_fd)
            except BaseException as cleanup_error:
                if primary_failure is not None:
                    primary_failure.add_note(f"staging descriptor cleanup failed: {cleanup_error}")
                else:
                    raise
        try:
            os.unlink(staging_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except BaseException as cleanup_error:
            if primary_failure is not None:
                primary_failure.add_note(f"staging cleanup failed: {cleanup_error}")
            elif committed:
                warnings.warn(
                    f"gateway trust staging cleanup failed: {cleanup_error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            else:
                raise
        try:
            os.close(parent_fd)
        except BaseException as cleanup_error:
            if primary_failure is not None:
                primary_failure.add_note(f"parent descriptor cleanup failed: {cleanup_error}")
            elif committed:
                warnings.warn(
                    f"gateway trust parent descriptor cleanup failed: {cleanup_error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            else:
                raise


def _publish_no_replace(path: Path, body: bytes) -> None:
    _assert_formal_publication_authority()
    if os.name == "nt":  # test-only seam; production authority above always rejects Windows
        _publish_no_replace_portable(path, body)
    else:
        _publish_no_replace_posix(path, body)


def write_gateway_trust_keyring(
    *,
    output_path: Path,
    candidate_root: Path,
    observer_public_key_path: Path,
    host_public_key_path: Path,
) -> Path:
    """Publish one immutable canonical public-only trust keyring."""

    candidate = Path(candidate_root)
    output = Path(output_path)
    _assert_safe_existing_path(candidate, label="gateway candidate root")
    _assert_safe_existing_path(output.parent, label="gateway trust output parent")
    if not candidate.is_dir() or not output.parent.is_dir():
        raise ValueError("gateway trust keyring path boundary is invalid")
    resolved_candidate = candidate.resolve(strict=True)
    resolved_output = output.parent.resolve(strict=True) / output.name
    if resolved_output == resolved_candidate or resolved_candidate in resolved_output.parents:
        raise ValueError("gateway trust keyring must be outside candidate root")
    candidate_stat = resolved_candidate.lstat()
    candidate_identity = _file_identity(candidate_stat)

    def public_key_body(path: Path, *, role: str) -> bytes:
        source = Path(path)
        _assert_safe_existing_path(source, label=f"gateway {role} public key")
        resolved_source = source.resolve(strict=True)
        if resolved_source == resolved_candidate or resolved_candidate in resolved_source.parents:
            raise ValueError("gateway public key source must be outside candidate root")
        return _read_regular_file_no_follow(
            resolved_source,
            label=f"gateway {role} public key",
        )

    body = build_gateway_trust_keyring(
        observer_public_key_body=public_key_body(
            observer_public_key_path,
            role="observer",
        ),
        host_public_key_body=public_key_body(
            host_public_key_path,
            role="host",
        ),
    )
    _publish_no_replace(resolved_output, body)
    current_candidate = resolved_candidate.lstat()
    if _file_identity(current_candidate) != candidate_identity:
        raise GatewayTrustCommittedError(
            "candidate root changed during keyring publication",
            resolved_output,
        )
    return resolved_output


def _challenge_for(challenges: Mapping[str, object], *, payload_type: str) -> str:
    schema_version = challenges.get("schemaVersion")
    if (
        set(challenges) != {"schemaVersion", "observerChallenge", "hostChallenge"}
        or type(schema_version) is not int
        or schema_version != 1
    ):
        raise ValueError("gateway trust challenge document is invalid")
    observer = challenges.get("observerChallenge")
    host = challenges.get("hostChallenge")
    if (
        not isinstance(observer, str)
        or _SHA256.fullmatch(observer) is None
        or observer == "0" * 64
        or not isinstance(host, str)
        or _SHA256.fullmatch(host) is None
        or host == "0" * 64
        or observer == host
    ):
        raise ValueError("gateway trust independent challenges are required")
    return observer if payload_type == "gateway-observer" else host


def _current_utc() -> datetime:
    return datetime.now(timezone.utc)


def _validate_envelope_time_window(issued: datetime, expires: datetime) -> None:
    now = _current_utc()
    if expires <= issued:
        raise ValueError("gateway trust expiry must follow issuance")
    if expires - issued > timedelta(seconds=MAX_GATEWAY_TRUST_TTL_SECONDS):
        raise ValueError("gateway trust envelope lifetime is too long")
    if now - issued > timedelta(seconds=MAX_GATEWAY_TRUST_AGE_SECONDS):
        raise ValueError("gateway trust envelope is too old")
    if issued - now > timedelta(seconds=MAX_GATEWAY_TRUST_FUTURE_SKEW_SECONDS):
        raise ValueError("gateway trust envelope was issued in the future")
    if now >= expires:
        raise ValueError("gateway trust envelope is expired")


@dataclass(frozen=True, slots=True)
class _SingleRoleSigner:
    _private_key: Ed25519PrivateKey
    _candidate_root: Path

    PAYLOAD_TYPE = ""
    ISSUER = ""
    ARTIFACT = ""

    @classmethod
    def from_private_key_file(
        cls,
        path: Path,
        *,
        candidate_root: Path,
    ):
        source = Path(path)
        candidate = Path(candidate_root)
        _assert_safe_existing_path(candidate, label="gateway candidate root")
        _assert_safe_existing_path(source, label="gateway private key")
        if not candidate.is_dir():
            raise ValueError("gateway candidate root is invalid")
        resolved_candidate = candidate.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
        if resolved_source == resolved_candidate or resolved_candidate in resolved_source.parents:
            raise ValueError("gateway private key source must be outside candidate root")
        source = resolved_source
        return cls(_load_private_key(source), resolved_candidate)

    def _candidate_contract_for_signing(self) -> _CandidateContract:
        return _load_candidate_contract(self._candidate_root)

    def _validate_artifact(
        self,
        artifact_body: bytes,
        *,
        candidate: Mapping[str, object],
        candidate_contract: Mapping[str, object],
        release_run: Mapping[str, str],
    ) -> None:
        raise NotImplementedError

    def sign(
        self,
        *,
        candidate: Mapping[str, object],
        release_run: Mapping[str, str],
        challenges: Mapping[str, object],
        artifact_body: bytes,
        issued_at: str,
        expires_at: str,
    ) -> bytes:
        validated_candidate = _validate_candidate(dict(candidate))
        validated_run = _validate_release_run(dict(release_run))
        issued = _utc_timestamp(issued_at, label="issued")
        expires = _utc_timestamp(expires_at, label="expiry")
        _validate_envelope_time_window(issued, expires)
        challenge = _challenge_for(challenges, payload_type=self.PAYLOAD_TYPE)
        if not isinstance(artifact_body, bytes) or not artifact_body:
            raise ValueError("gateway trust artifact bytes are invalid")
        candidate_contract = self._candidate_contract_for_signing()
        with candidate_contract:
            candidate_contract.assert_current()
            if validated_candidate != candidate_contract.candidate:
                raise ValueError("gateway signer candidate does not match candidate contract")
            candidate_contract_claim = candidate_contract.claim
            self._validate_artifact(
                artifact_body,
                candidate=validated_candidate,
                candidate_contract=candidate_contract_claim,
                release_run=validated_run,
            )
            payload: dict[str, object] = {
                "schemaVersion": 2,
                "issuer": self.ISSUER,
                "issuedAt": issued_at,
                "expiresAt": expires_at,
                "challenge": challenge,
                "candidate": validated_candidate,
                "candidateContract": candidate_contract_claim,
                "releaseRun": validated_run,
                "environmentId": validated_run["environmentId"],
                "claims": {
                    "artifact": self.ARTIFACT,
                    "artifactSha256": hashlib.sha256(artifact_body).hexdigest(),
                },
            }
            spki = self._private_key.public_key().public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            signature_input = (
                _SIGNATURE_DOMAIN
                + self.PAYLOAD_TYPE.encode("ascii")
                + b"\0"
                + _canonical_json(payload)
            )
            document: dict[str, object] = {
                "schemaVersion": 2,
                "payloadType": self.PAYLOAD_TYPE,
                "algorithm": "ed25519",
                "keyId": hashlib.sha256(spki).hexdigest(),
                "payload": payload,
                "signatureBase64": base64.b64encode(self._private_key.sign(signature_input)).decode(
                    "ascii"
                ),
            }
            body = _canonical_json(document)
            candidate_contract.assert_current()
        return body


class GatewayObserverSigner(_SingleRoleSigner):
    """Signer fixed to the external-observer role and artifact."""

    PAYLOAD_TYPE = "gateway-observer"
    ISSUER = "external-observer-authority"
    ARTIFACT = "runtime/gateway-external-observer-attestation.json"

    def _validate_artifact(
        self,
        artifact_body: bytes,
        *,
        candidate: Mapping[str, object],
        candidate_contract: Mapping[str, object],
        release_run: Mapping[str, str],
    ) -> None:
        del candidate_contract
        try:
            document = _canonical_json_object(
                artifact_body,
                label="observer attestation",
                limit=MAX_GATEWAY_PUBLIC_REPORT_BYTES,
            )
            observer = document.get("observer")
            target = document.get("target")
            runtime = document.get("runtimeAttestation")
            observation = document.get("externalObservation")
            if not all(
                isinstance(value, dict) for value in (observer, target, runtime, observation)
            ):
                raise ValueError("observer attestation bindings are invalid")
            parse_gateway_observer_attestation(
                artifact_body,
                candidate=candidate,
                release_run=release_run,
                expected_base_url=target.get("origin"),
                expected_runtime_attestation_sha256=runtime.get("sha256"),
                expected_external_observation_sha256=observation.get("sha256"),
                expected_observer_id=observer.get("observerId"),
                expected_observer_origin=observer.get("origin"),
                expected_attestation_sha256=hashlib.sha256(artifact_body).hexdigest(),
            )
        except (TypeError, ValueError):
            raise ValueError("gateway observer artifact is invalid or incorrectly bound") from None


@dataclass(frozen=True, slots=True, init=False)
class GatewayHostProvisionerSigner(_SingleRoleSigner):
    """Signer fixed to the deployment-authority role and host receipt."""

    _candidate_contract: _CandidateContract

    PAYLOAD_TYPE = "gateway-host-provisioner"
    ISSUER = "deployment-authority"
    ARTIFACT = "runtime/gateway-docker-host-provisioning-receipt.json"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("gateway host signer must be created from a candidate contract")

    @classmethod
    def from_private_key_file(
        cls,
        path: Path,
        *,
        candidate_root: Path,
    ):
        source = Path(path)
        candidate = Path(candidate_root)
        _assert_safe_existing_path(candidate, label="gateway candidate root")
        _assert_safe_existing_path(source, label="gateway private key")
        if not candidate.is_dir():
            raise ValueError("gateway candidate root is invalid")
        resolved_candidate = candidate.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
        if resolved_source == resolved_candidate or resolved_candidate in resolved_source.parents:
            raise ValueError("gateway private key source must be outside candidate root")
        contract = _load_candidate_contract(resolved_candidate)
        with contract:
            contract.assert_current()
        signer = object.__new__(cls)
        object.__setattr__(signer, "_private_key", _load_private_key(resolved_source))
        object.__setattr__(signer, "_candidate_root", contract.root)
        object.__setattr__(signer, "_candidate_contract", contract)
        return signer

    def _candidate_contract_for_signing(self) -> _CandidateContract:
        return _load_candidate_contract(self._candidate_root)

    def sign(
        self,
        *,
        candidate: Mapping[str, object],
        release_run: Mapping[str, str],
        challenges: Mapping[str, object],
        artifact_body: bytes,
        issued_at: str,
        expires_at: str,
    ) -> bytes:
        validated_candidate = _validate_candidate(dict(candidate))
        self._candidate_contract.assert_unchanged()
        if validated_candidate != self._candidate_contract.candidate:
            raise ValueError("gateway host candidate does not match candidate contract")
        body = _SingleRoleSigner.sign(
            self,
            candidate=validated_candidate,
            release_run=release_run,
            challenges=challenges,
            artifact_body=artifact_body,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        self._candidate_contract.assert_unchanged()
        return body

    def _validate_artifact(
        self,
        artifact_body: bytes,
        *,
        candidate: Mapping[str, object],
        candidate_contract: Mapping[str, object],
        release_run: Mapping[str, str],
    ) -> None:
        try:
            receipt = _parse_gateway_host_receipt(
                artifact_body,
                candidate=candidate,
                candidate_contract=candidate_contract,
                release_run=release_run,
                expected_environment_id=release_run["environmentId"],
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("gateway host artifact is invalid or incorrectly bound") from None
        if receipt.get("host") != _collect_gateway_host_authority_facts():
            raise ValueError("gateway host authority facts do not match artifact")


def _load_canonical_mapping(path: Path, *, label: str) -> dict[str, object]:
    body = _read_regular_file_no_follow(
        Path(path),
        label=label,
        max_bytes=MAX_GATEWAY_PUBLIC_REPORT_BYTES,
    )
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(f"{label} is invalid") from None
    if not isinstance(document, dict) or _canonical_json(document) != body:
        raise ValueError(f"{label} must be canonical JSON")
    return document


def _add_sign_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_candidate_override: bool,
) -> None:
    parser.add_argument("--candidate-root", required=True, type=Path)
    if include_candidate_override:
        parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--release-run", required=True, type=Path)
    parser.add_argument("--challenges", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--issued-at", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--output", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    host_receipt = commands.add_parser("host-receipt", allow_abbrev=False)
    host_receipt.add_argument("--candidate-root", required=True, type=Path)
    host_receipt.add_argument("--release-run", required=True, type=Path)
    host_receipt.add_argument("--output", required=True, type=Path)

    observer_attestation = commands.add_parser("observer-attestation", allow_abbrev=False)
    observer_attestation.add_argument("--raw-observation", required=True, type=Path)
    observer_attestation.add_argument("--observed-at", required=True)
    observer_attestation.add_argument("--output", required=True, type=Path)

    _add_sign_arguments(
        commands.add_parser("sign-observer", allow_abbrev=False),
        include_candidate_override=True,
    )
    _add_sign_arguments(
        commands.add_parser("sign-host", allow_abbrev=False),
        include_candidate_override=False,
    )

    keyring = commands.add_parser("build-keyring", allow_abbrev=False)
    keyring.add_argument("--candidate-root", required=True, type=Path)
    keyring.add_argument("--observer-public-key", required=True, type=Path)
    keyring.add_argument("--host-public-key", required=True, type=Path)
    keyring.add_argument("--output", required=True, type=Path)
    return parser


def _sign_from_arguments(
    arguments: argparse.Namespace, signer_type: type[_SingleRoleSigner]
) -> None:
    committed_output: Path | None = None
    try:
        with _load_candidate_contract(arguments.candidate_root) as contract:
            candidate = (
                contract.candidate
                if signer_type is GatewayHostProvisionerSigner
                else _load_canonical_mapping(arguments.candidate, label="candidate")
            )
            output = (
                _host_authority_output_path(arguments.output, candidate_root=contract.root)
                if signer_type is GatewayHostProvisionerSigner
                else Path(arguments.output)
            )
            signer = signer_type.from_private_key_file(
                arguments.private_key,
                candidate_root=arguments.candidate_root,
            )
            body = signer.sign(
                candidate=candidate,
                release_run=_load_canonical_mapping(
                    arguments.release_run,
                    label="release run",
                ),
                challenges=_load_canonical_mapping(
                    arguments.challenges,
                    label="challenges",
                ),
                artifact_body=_read_regular_file_no_follow(
                    arguments.artifact,
                    label="gateway trust artifact",
                    max_bytes=MAX_GATEWAY_PUBLIC_REPORT_BYTES,
                ),
                issued_at=arguments.issued_at,
                expires_at=arguments.expires_at,
            )
            _assert_candidate_contract_unchanged(contract)
            _publish_no_replace(output, body)
            committed_output = output
            _assert_candidate_contract_unchanged(
                contract,
                committed_output=output,
            )
    except ValueError as error:
        if committed_output is not None:
            raise GatewayTrustCommittedError(
                "candidate contract changed during publication",
                committed_output,
            ) from error
        raise


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "host-receipt":
            committed_output: Path | None = None
            try:
                with _load_candidate_contract(arguments.candidate_root) as contract:
                    output = _host_authority_output_path(
                        arguments.output,
                        candidate_root=contract.root,
                    )
                    body = _build_gateway_host_receipt_from_facts(
                        candidate=contract.candidate,
                        candidate_contract=contract.claim,
                        release_run=_load_canonical_mapping(
                            arguments.release_run,
                            label="release run",
                        ),
                        host=_collect_gateway_host_authority_facts(),
                    )
                    _assert_candidate_contract_unchanged(contract)
                    _publish_no_replace(output, body)
                    committed_output = output
                    _assert_candidate_contract_unchanged(
                        contract,
                        committed_output=output,
                    )
            except ValueError as error:
                if committed_output is not None:
                    raise GatewayTrustCommittedError(
                        "candidate contract changed during publication",
                        committed_output,
                    ) from error
                raise
        elif arguments.command == "observer-attestation":
            body = build_gateway_observer_attestation(
                _read_regular_file_no_follow(
                    arguments.raw_observation,
                    label="gateway raw observation",
                    max_bytes=MAX_GATEWAY_PUBLIC_REPORT_BYTES,
                ),
                observed_at=arguments.observed_at,
            )
            _publish_no_replace(arguments.output, body)
        elif arguments.command == "sign-observer":
            _sign_from_arguments(arguments, GatewayObserverSigner)
        elif arguments.command == "sign-host":
            _sign_from_arguments(arguments, GatewayHostProvisionerSigner)
        elif arguments.command == "build-keyring":
            write_gateway_trust_keyring(
                output_path=arguments.output,
                candidate_root=arguments.candidate_root,
                observer_public_key_path=arguments.observer_public_key,
                host_public_key_path=arguments.host_public_key,
            )
        else:  # pragma: no cover - argparse constrains the command
            raise ValueError("gateway trust command is invalid")
    except GatewayTrustCommittedError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
