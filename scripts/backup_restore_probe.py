"""Produce strict evidence for one source backup restored into isolated targets.

The existing restore operator owns the live PostgreSQL and object-store work. This
probe validates the source archive, invokes that operator with secret-file paths
rather than secret values, revalidates the unchanged source archive, and publishes
one candidate-bound canonical report. It never deletes backup artifacts or targets.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, TypeAlias
import warnings

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from backup_restore_contract import (
    BACKUP_RESTORE_PRODUCER,
    BACKUP_RESTORE_SCHEMA_VERSION,
    canonical_backup_restore_report,
    canonical_source_provenance,
    parse_backup_restore_report,
    parse_target_provisioning_receipt,
    physical_object_store_identity_sha256,
    validate_backup_restore_identity,
    validate_backup_restore_source_provenance,
)
from backup_restore_contract import (  # noqa: E402
    _contains_sensitive_field as _contract_contains_sensitive_field,
)
from classroom_runtime_attestation import (  # noqa: E402
    _assert_no_link_ancestors,
    _close_windows_handle,
    _create_windows_staging_file,
    _delete_windows_file_on_close,
    _file_identity,
    _open_posix_directory_path_no_follow,
    _open_windows_directory_handle,
    _open_windows_regular_file_relative,
    _read_windows_file_handle,
    _windows_handle_identity,
)

_RESTORE_VALIDATION_ARTIFACT = "restore-validation.json"
_BACKUP_RESTORE_ARTIFACT = "backup-restore-report.json"
_SOURCE_PROVENANCE_ARTIFACT = "source-provenance.json"
_TARGET_CONFIG_ARTIFACT = "target-config.snapshot.json"
_PROVISIONING_RECEIPT_ARTIFACT = "target-provisioning-receipt.json"
_SOURCE_ARCHIVE_SNAPSHOT_DIRECTORY = "source-backup.snapshot"
_TARGET_SECRET_SNAPSHOT_PREFIX = ".target-secrets."
_RESTORE_OPERATOR_SECRET_NAMES = (
    "platform_database_app_password",
    "platform_database_migration_password",
    "minio_bootstrap_access_key",
    "minio_bootstrap_secret_key",
)
_EXPECTED_RESTORE_VALIDATIONS = [
    "platform_schema_revision",
    "schema_revisions",
    "classroom_versions",
    "learning_events",
    "database_object_references",
    "source_snapshots",
    "media",
    "quota",
    "audit",
    "app_role_access",
]
_OWNERSHIP_VALUES = {"runner-owned-disposable", "retained-audit"}
_RESTORE_DATABASE_USER = "yfeistai_migrator"
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ENVIRONMENT_NAMES = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}
_MAX_OPERATOR_OUTPUT_BYTES = 1024 * 1024
_MAX_RESTORE_REPORT_BYTES = 64 * 1024
_MAX_IDENTITY_FILE_BYTES = 64 * 1024
_PROCESS_CLEANUP_GRACE_SECONDS = 10.0
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_READONLY_DIRECTORY_MODE = 0o500
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_READONLY_FILE_MODE = 0o400

CommandRunner: TypeAlias = Callable[..., subprocess.CompletedProcess[bytes]]
VerifiedBackupLoader: TypeAlias = Callable[[Path], object]
VerifiedBackupRechecker: TypeAlias = Callable[[object], object]
TargetConfigLoader: TypeAlias = Callable[[Path], object]


@dataclass(frozen=True, slots=True)
class BackupRestoreProbeConfig:
    candidate: Mapping[str, object]
    release_run: Mapping[str, object]
    source_provenance: Mapping[str, object]
    backup_directory: Path
    target_config_path: Path
    provisioning_receipt_path: Path
    target_secret_directory: Path
    output_directory: Path
    python_executable: Path
    pg_restore_executable: Path
    database_ownership: str
    object_namespace_ownership: str
    timeout_seconds: int = 1800
    forbidden_secret_values: tuple[bytes, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _SourceArchiveSnapshotEntry:
    relative_parts: tuple[str, ...]
    kind: str
    device: int
    inode: int
    size: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class _SourceArchiveSnapshot:
    directory: Path
    entries: tuple[_SourceArchiveSnapshotEntry, ...]


@dataclass(frozen=True, slots=True)
class _SecretSnapshotFile:
    name: str
    body: bytes = field(repr=False)


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


def object_store_identity_sha256(namespace_id: str, bucket: str) -> str:
    if (
        not isinstance(namespace_id, str)
        or _PUBLIC_ID.fullmatch(namespace_id) is None
        or not isinstance(bucket, str)
        or _PUBLIC_ID.fullmatch(bucket) is None
    ):
        raise ValueError("restore target object namespace identity is invalid")
    return hashlib.sha256(
        _canonical_json({"bucket": bucket, "namespaceId": namespace_id})
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None and value != "0" * 64


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("backup restore probe clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_verified_backup_loader(path: Path) -> object:
    from backup_teaching import load_verified_backup

    return load_verified_backup(path)


def _default_verified_backup_rechecker(backup: object) -> object:
    from backup_teaching import reverify_verified_backup

    return reverify_verified_backup(backup)


def _default_target_config_loader(path: Path) -> object:
    from backup_teaching import load_operator_backup_config

    return load_operator_backup_config(path)


def _default_command_runner(
    arguments: Sequence[str | Path],
    *,
    cwd: Path,
    env: Mapping[str, str],
    deadline_monotonic: float,
    cleanup_grace_seconds: float = _PROCESS_CLEANUP_GRACE_SECONDS,
    check: bool,
    capture_output: bool,
    monotonic: Callable[[], float] = time.monotonic,
) -> subprocess.CompletedProcess[bytes]:
    argv = [str(argument) for argument in arguments]
    remaining = deadline_monotonic - monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise subprocess.TimeoutExpired(argv, max(0.0, remaining))
    if (
        isinstance(cleanup_grace_seconds, bool)
        or not isinstance(cleanup_grace_seconds, (int, float))
        or not math.isfinite(cleanup_grace_seconds)
        or cleanup_grace_seconds <= 0
    ):
        raise ValueError("backup restore process cleanup grace is invalid")
    cleanup_grace_seconds = float(cleanup_grace_seconds)
    options: dict[str, object] = {
        "cwd": cwd,
        "env": dict(env),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE if capture_output else subprocess.DEVNULL,
        "stderr": subprocess.PIPE if capture_output else subprocess.DEVNULL,
        "shell": False,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        )
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(argv, **options)
    try:
        stdout, stderr = process.communicate(timeout=remaining)
    except BaseException as primary_failure:
        cleanup_deadline = monotonic() + cleanup_grace_seconds
        cleanup_failures: list[str] = []
        try:
            _terminate_process_tree(process)
        except BaseException:
            cleanup_failures.append("process tree termination")
            try:
                process.kill()
            except BaseException:
                cleanup_failures.append("process fallback termination")
        cleanup_remaining = max(0.0, cleanup_deadline - monotonic())
        if cleanup_remaining > 0:
            try:
                process.communicate(timeout=cleanup_remaining)
            except BaseException:
                cleanup_failures.append("process reap")
        else:
            cleanup_failures.append("process reap deadline")
        if cleanup_failures:
            primary_failure.add_note(
                "backup restore cleanup incomplete: " + ", ".join(cleanup_failures)
            )
        raise
    completed = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        return
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        raise OSError("Windows system root is unavailable")
    taskkill = Path(system_root) / "System32" / "taskkill.exe"
    result = subprocess.run(
        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        timeout=10,
        env={"SystemRoot": system_root, "WINDIR": system_root},
    )
    if result.returncode != 0 and process.poll() is None:
        raise subprocess.SubprocessError("taskkill did not terminate the process tree")


def _resolve_existing_file(path: Path, field_name: str) -> Path:
    source = Path(path)
    try:
        resolved = source.resolve(strict=True)
        file_stat = resolved.stat()
    except OSError:
        raise ValueError(f"backup restore {field_name} is unavailable") from None
    if source.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"backup restore {field_name} is unavailable")
    return resolved


def _resolve_existing_directory(path: Path, field_name: str) -> Path:
    source = Path(path)
    try:
        resolved = source.resolve(strict=True)
        directory_stat = resolved.stat()
    except OSError:
        raise ValueError(f"backup restore {field_name} is unavailable") from None
    if source.is_symlink() or not stat.S_ISDIR(directory_stat.st_mode):
        raise ValueError(f"backup restore {field_name} is unavailable")
    return resolved


def _prepare_output_directory(path: Path) -> Path:
    requested = Path(path)
    if requested.name in {"", ".", ".."}:
        raise ValueError("backup restore output directory is invalid")
    if requested.exists() or requested.is_symlink():
        raise FileExistsError("backup restore output directory already exists")
    try:
        parent = requested.parent.resolve(strict=True)
        parent_stat = parent.stat()
    except OSError:
        raise ValueError("backup restore output directory parent is unavailable") from None
    if requested.parent.is_symlink() or not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("backup restore output directory parent is unsafe")
    target = parent / requested.name
    target.mkdir(mode=0o700)
    return target


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(file_stat, "st_file_attributes", 0) & reparse_flag)


def _archive_stat_signature(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _require_safe_archive_stat(file_stat: os.stat_result, *, directory: bool) -> None:
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if _is_reparse_point(file_stat) or not expected_kind(file_stat.st_mode):
        raise ValueError("backup restore source archive contains an unsafe entry")


def _archive_directory_entries(path: Path) -> tuple[tuple[str, os.stat_result], ...]:
    try:
        with os.scandir(path) as iterator:
            entries = tuple(
                sorted(
                    ((entry.name, Path(entry.path).lstat()) for entry in iterator),
                    key=lambda item: item[0],
                )
            )
    except OSError:
        raise ValueError("backup restore source archive could not be inspected") from None
    if any(name in {"", ".", ".."} for name, _file_stat in entries):
        raise ValueError("backup restore source archive contains an unsafe entry")
    return entries


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while snapshotting source archive")
        remaining = remaining[written:]


def _copy_archive_file_no_follow(
    source: Path,
    destination: Path,
    relative_parts: tuple[str, ...],
    expected_source_stat: os.stat_result,
) -> _SourceArchiveSnapshotEntry:
    _require_safe_archive_stat(expected_source_stat, directory=False)
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_descriptor = os.open(source, source_flags)
    destination_descriptor = -1
    digest = hashlib.sha256()
    copied_size = 0
    try:
        opened_source_stat = os.fstat(source_descriptor)
        if _archive_stat_signature(opened_source_stat) != _archive_stat_signature(
            expected_source_stat
        ):
            raise ValueError("backup restore source archive changed while being snapshotted")
        destination_descriptor = os.open(destination, destination_flags, 0o600)
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied_size += len(chunk)
            _write_all(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
        final_source_stat = os.fstat(source_descriptor)
        if _archive_stat_signature(final_source_stat) != _archive_stat_signature(
            opened_source_stat
        ):
            raise ValueError("backup restore source archive changed while being snapshotted")
        final_destination_stat = os.fstat(destination_descriptor)
        if copied_size != final_destination_stat.st_size or (
            final_destination_stat.st_dev,
            final_destination_stat.st_ino,
        ) == (opened_source_stat.st_dev, opened_source_stat.st_ino):
            raise ValueError("backup restore source archive snapshot is unsafe")
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)
    current_source_stat = source.lstat()
    if _archive_stat_signature(current_source_stat) != _archive_stat_signature(
        expected_source_stat
    ):
        raise ValueError("backup restore source archive changed while being snapshotted")
    os.chmod(destination, 0o400)
    destination_stat = destination.lstat()
    _require_safe_archive_stat(destination_stat, directory=False)
    return _SourceArchiveSnapshotEntry(
        relative_parts=relative_parts,
        kind="file",
        device=destination_stat.st_dev,
        inode=destination_stat.st_ino,
        size=copied_size,
        sha256=digest.hexdigest(),
    )


def _copy_archive_directory_no_follow(
    source: Path,
    destination: Path,
    relative_parts: tuple[str, ...],
    expected_source_stat: os.stat_result,
    entries: list[_SourceArchiveSnapshotEntry],
) -> None:
    _require_safe_archive_stat(expected_source_stat, directory=True)
    current_source_stat = source.lstat()
    if _archive_stat_signature(current_source_stat) != _archive_stat_signature(
        expected_source_stat
    ):
        raise ValueError("backup restore source archive changed while being snapshotted")
    initial_entries = _archive_directory_entries(source)
    for name, source_stat in initial_entries:
        source_child = source / name
        destination_child = destination / name
        child_parts = (*relative_parts, name)
        if stat.S_ISDIR(source_stat.st_mode) and not _is_reparse_point(source_stat):
            destination_child.mkdir(mode=0o700)
            _copy_archive_directory_no_follow(
                source_child,
                destination_child,
                child_parts,
                source_stat,
                entries,
            )
        else:
            entries.append(
                _copy_archive_file_no_follow(
                    source_child,
                    destination_child,
                    child_parts,
                    source_stat,
                )
            )
    final_entries = _archive_directory_entries(source)
    if tuple(
        (name, _archive_stat_signature(file_stat)) for name, file_stat in final_entries
    ) != tuple((name, _archive_stat_signature(file_stat)) for name, file_stat in initial_entries):
        raise ValueError("backup restore source archive changed while being snapshotted")
    final_source_stat = source.lstat()
    if _archive_stat_signature(final_source_stat) != _archive_stat_signature(expected_source_stat):
        raise ValueError("backup restore source archive changed while being snapshotted")
    _fsync_directory(destination)
    os.chmod(destination, 0o500)
    destination_stat = destination.lstat()
    _require_safe_archive_stat(destination_stat, directory=True)
    entries.append(
        _SourceArchiveSnapshotEntry(
            relative_parts=relative_parts,
            kind="directory",
            device=destination_stat.st_dev,
            inode=destination_stat.st_ino,
            size=0,
            sha256=None,
        )
    )


def _snapshot_source_archive(source: Path, output_directory: Path) -> _SourceArchiveSnapshot:
    snapshot_directory = output_directory / _SOURCE_ARCHIVE_SNAPSHOT_DIRECTORY
    try:
        source_stat = source.lstat()
        _require_safe_archive_stat(source_stat, directory=True)
        snapshot_directory.mkdir(mode=0o700)
        entries: list[_SourceArchiveSnapshotEntry] = []
        _copy_archive_directory_no_follow(
            source,
            snapshot_directory,
            (),
            source_stat,
            entries,
        )
    except ValueError:
        raise
    except OSError:
        raise ValueError("backup restore source archive could not be snapshotted safely") from None
    snapshot = _SourceArchiveSnapshot(
        directory=snapshot_directory,
        entries=tuple(sorted(entries, key=lambda entry: (entry.relative_parts, entry.kind))),
    )
    _verify_source_archive_snapshot(snapshot)
    return snapshot


def _read_snapshot_file_entry(
    path: Path,
    relative_parts: tuple[str, ...],
    expected_stat: os.stat_result,
) -> _SourceArchiveSnapshotEntry:
    _require_safe_archive_stat(expected_stat, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        opened_stat = os.fstat(descriptor)
        if _archive_stat_signature(opened_stat) != _archive_stat_signature(expected_stat):
            raise ValueError("backup restore source archive snapshot changed")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        final_stat = os.fstat(descriptor)
        if _archive_stat_signature(final_stat) != _archive_stat_signature(opened_stat):
            raise ValueError("backup restore source archive snapshot changed")
    finally:
        os.close(descriptor)
    current_stat = path.lstat()
    if _archive_stat_signature(current_stat) != _archive_stat_signature(expected_stat):
        raise ValueError("backup restore source archive snapshot changed")
    return _SourceArchiveSnapshotEntry(
        relative_parts=relative_parts,
        kind="file",
        device=current_stat.st_dev,
        inode=current_stat.st_ino,
        size=size,
        sha256=digest.hexdigest(),
    )


def _collect_source_archive_snapshot_entries(
    directory: Path,
    relative_parts: tuple[str, ...] = (),
) -> tuple[_SourceArchiveSnapshotEntry, ...]:
    directory_stat = directory.lstat()
    _require_safe_archive_stat(directory_stat, directory=True)
    entries: list[_SourceArchiveSnapshotEntry] = []
    for name, child_stat in _archive_directory_entries(directory):
        child = directory / name
        child_parts = (*relative_parts, name)
        if stat.S_ISDIR(child_stat.st_mode) and not _is_reparse_point(child_stat):
            entries.extend(_collect_source_archive_snapshot_entries(child, child_parts))
        else:
            entries.append(_read_snapshot_file_entry(child, child_parts, child_stat))
    current_directory_stat = directory.lstat()
    if _archive_stat_signature(current_directory_stat) != _archive_stat_signature(directory_stat):
        raise ValueError("backup restore source archive snapshot changed")
    entries.append(
        _SourceArchiveSnapshotEntry(
            relative_parts=relative_parts,
            kind="directory",
            device=current_directory_stat.st_dev,
            inode=current_directory_stat.st_ino,
            size=0,
            sha256=None,
        )
    )
    return tuple(entries)


def _verify_source_archive_snapshot(snapshot: _SourceArchiveSnapshot) -> None:
    try:
        current = tuple(
            sorted(
                _collect_source_archive_snapshot_entries(snapshot.directory),
                key=lambda entry: (entry.relative_parts, entry.kind),
            )
        )
    except (OSError, ValueError):
        raise ValueError("backup restore source archive snapshot changed") from None
    if current != snapshot.entries:
        raise ValueError("backup restore source archive snapshot changed")


def _require_backup_uses_snapshot(backup: object, snapshot: _SourceArchiveSnapshot) -> None:
    try:
        backup_directory = Path(backup.directory)
        if backup_directory.is_symlink():
            raise ValueError
        backup_directory = backup_directory.resolve(strict=True)
        if backup_directory != snapshot.directory:
            raise ValueError
        expected_files = {
            entry.relative_parts: entry for entry in snapshot.entries if entry.kind == "file"
        }
        referenced_paths = (backup.database_dump, *getattr(backup, "object_payloads", ()))
        for referenced_path in referenced_paths:
            file_path = Path(referenced_path)
            if file_path.is_symlink():
                raise ValueError
            resolved_file = file_path.resolve(strict=True)
            relative_parts = resolved_file.relative_to(snapshot.directory).parts
            expected = expected_files[relative_parts]
            file_stat = resolved_file.stat()
            if (file_stat.st_dev, file_stat.st_ino) != (expected.device, expected.inode):
                raise ValueError
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        raise ValueError("backup restore verified source is not bound to its snapshot") from None


def _canonical_target_config(path: Path) -> bytes:
    source = _resolve_existing_file(path, "target config")
    try:
        body = source.read_bytes()
        if not 0 < len(body) <= _MAX_IDENTITY_FILE_BYTES:
            raise ValueError
        payload = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("backup restore target config is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("backup restore target config is invalid")
    return _canonical_json(payload)


def _canonical_provisioning_receipt(path: Path) -> bytes:
    source = _resolve_existing_file(path, "target provisioning receipt")
    try:
        body = source.read_bytes()
        if not 0 < len(body) <= _MAX_IDENTITY_FILE_BYTES:
            raise ValueError
        payload = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("backup restore target provisioning receipt is invalid") from None
    if not isinstance(payload, dict) or body != _canonical_json(payload):
        raise ValueError("backup restore target provisioning receipt must be canonical JSON")
    return body


def _write_input_snapshot(path: Path, body: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise RuntimeError("backup restore input snapshot could not be published") from None


def _safe_environment(environment: Mapping[str, str]) -> dict[str, str]:
    by_upper = {name.upper(): value for name, value in environment.items()}
    return {
        name: by_upper[name]
        for name in _SAFE_ENVIRONMENT_NAMES
        if isinstance(by_upper.get(name), str) and by_upper[name]
    }


def _reject_secret_bytes(payload: bytes, secret_values: Sequence[bytes], context: str) -> None:
    for secret in secret_values:
        if not isinstance(secret, bytes) or not secret:
            continue
        if secret in payload:
            raise RuntimeError(f"backup restore {context} contains a secret value")


def _json_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _json_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _json_strings(nested)


def _reject_secret_json_bytes(
    payload: bytes,
    secret_values: Sequence[bytes],
    context: str,
) -> None:
    _reject_secret_bytes(payload, secret_values, context)
    try:
        decoded = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError(f"backup restore {context} could not be scanned") from None
    _reject_decoded_json_secrets(decoded, secret_values, context)


def _reject_decoded_json_secrets(
    decoded: object,
    secret_values: Sequence[bytes],
    context: str,
) -> None:
    if _contract_contains_sensitive_field(decoded):
        raise RuntimeError(f"backup restore {context} contains a sensitive field")
    decoded_secrets: list[str] = []
    for secret in secret_values:
        try:
            decoded_secrets.append(secret.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    if any(secret in text for text in _json_strings(decoded) for secret in decoded_secrets):
        raise RuntimeError(f"backup restore {context} contains a secret value")


def _reject_command_output_secrets(
    payload: bytes,
    secret_values: Sequence[bytes],
    context: str,
) -> None:
    _reject_secret_bytes(payload, secret_values, context)
    try:
        decoded = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError):
        return
    _reject_decoded_json_secrets(decoded, secret_values, context)


def _validate_config(config: BackupRestoreProbeConfig) -> None:
    if not isinstance(config.candidate, Mapping) or not isinstance(config.release_run, Mapping):
        raise ValueError("backup restore candidate binding is invalid")
    validate_backup_restore_identity(config.candidate, config.release_run)
    if not isinstance(config.source_provenance, Mapping):
        raise ValueError("backup restore source provenance is invalid")
    if set(config.release_run) != {"runId", "environmentId"} or any(
        not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None
        for value in config.release_run.values()
    ):
        raise ValueError("backup restore release run is invalid")
    if config.database_ownership not in _OWNERSHIP_VALUES:
        raise ValueError("backup restore database ownership is invalid")
    if config.object_namespace_ownership not in _OWNERSHIP_VALUES:
        raise ValueError("backup restore object namespace ownership is invalid")
    if (
        isinstance(config.timeout_seconds, bool)
        or not isinstance(config.timeout_seconds, int)
        or not 1 <= config.timeout_seconds <= 3600
    ):
        raise ValueError("backup restore timeout is invalid")
    for secret in config.forbidden_secret_values:
        if not isinstance(secret, bytes) or not secret or len(secret) > _MAX_IDENTITY_FILE_BYTES:
            raise ValueError("backup restore secret scanner input is invalid")


def _source_payload(backup: object) -> dict[str, object]:
    try:
        manifest = backup.manifest
        payload: dict[str, object] = {
            "manifestSha256": backup.manifest_sha256,
            "archiveFingerprintSha256": backup.archive_fingerprint_sha256,
            "databaseIdentitySha256": manifest.database.identity_sha256,
            "databaseSha256": backup.database_sha256,
            "objectStoreIdentitySha256": manifest.source_object_store_identity_sha256,
            "objectInventorySha256": backup.object_inventory_sha256,
            "platformSchemaRevision": manifest.platform_schema_revision,
            "schemaRevisions": dict(sorted(manifest.schema_revisions.items())),
            "classroomVersionsCount": manifest.classroom_versions_count,
            "learningEventsCount": manifest.learning_events_count,
            "objectCount": manifest.object_count,
        }
    except (AttributeError, TypeError, ValueError):
        raise ValueError("backup restore source manifest is invalid") from None
    digest_fields = (
        "manifestSha256",
        "archiveFingerprintSha256",
        "databaseIdentitySha256",
        "databaseSha256",
        "objectStoreIdentitySha256",
        "objectInventorySha256",
    )
    if any(not _valid_sha256(payload[field]) for field in digest_fields):
        raise ValueError("backup restore source manifest is invalid")
    return payload


def _read_restore_validation(
    path: Path,
    *,
    run_id: str,
    source: Mapping[str, object],
    target_config: object,
    target_config_sha256: str,
    provisioning_receipt_sha256: str,
    database_ownership: str,
    object_namespace_ownership: str,
) -> tuple[dict[str, object], bytes]:
    try:
        file_stat = path.stat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or not 0 < file_stat.st_size <= _MAX_RESTORE_REPORT_BYTES
        ):
            raise ValueError
        body = path.read_bytes()
        payload = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("backup restore operator report is invalid") from None
    if not isinstance(payload, dict) or body != _canonical_json(payload):
        raise ValueError("backup restore operator report must be canonical JSON")
    expected_fields = {
        "schemaVersion",
        "runId",
        "ok",
        "targetDatabaseIdentitySha256",
        "objectPrefix",
        "validated",
        "failures",
        "sourceArchive",
        "database",
        "objects",
        "crossSystemAtomic",
        "target",
    }
    if set(payload) != expected_fields:
        raise ValueError("backup restore operator report schema is invalid")
    if (
        payload.get("schemaVersion") != 3
        or payload.get("runId") != run_id
        or payload.get("ok") is not True
        or payload.get("objectPrefix") != ""
        or payload.get("validated") != _EXPECTED_RESTORE_VALIDATIONS
        or payload.get("failures") != []
        or payload.get("crossSystemAtomic") is not False
    ):
        raise ValueError("backup restore operator report findings are invalid")
    if payload.get("sourceArchive") != {
        "archiveFingerprintSha256": source["archiveFingerprintSha256"],
        "manifestSha256": source["manifestSha256"],
    }:
        raise ValueError("backup restore operator report provenance is invalid")
    target_database_identity = payload.get("targetDatabaseIdentitySha256")
    if (
        not _valid_sha256(target_database_identity)
        or target_database_identity == source["databaseIdentitySha256"]
    ):
        raise ValueError("backup restore target database is not distinct")
    if payload.get("database") != {
        "dumpRestoreSingleTransaction": True,
        "postRestoreMutationsAtomic": False,
    }:
        raise ValueError("backup restore database findings are invalid")
    try:
        target_database_host = target_config.database_host
        target_database_port = target_config.database_port
        target_database_name = target_config.database_name
        target_object_endpoint = getattr(target_config, "object_endpoint", None) or getattr(
            target_config,
            "object_store_endpoint",
        )
        target_object_region = getattr(target_config, "object_region", None) or getattr(
            target_config,
            "object_store_region",
        )
        target_object_namespace = getattr(
            target_config,
            "object_namespace_id",
            None,
        ) or getattr(target_config, "object_store_namespace_id")
        target_bucket = getattr(target_config, "object_bucket", None) or getattr(
            target_config,
            "object_store_bucket",
        )
    except AttributeError:
        raise ValueError("backup restore target configuration is invalid") from None
    if payload.get("objects") != {
        "createOnly": True,
        "isolation": "empty_target_bucket",
        "readbackVerified": True,
        "restoredCount": source["objectCount"],
        "targetBucket": target_bucket,
    }:
        raise ValueError("backup restore object findings are invalid")
    target = payload.get("target")
    if not isinstance(target, dict) or set(target) != {
        "targetConfigSha256",
        "provisioningReceiptSha256",
        "database",
        "objects",
        "concurrencyExclusion",
    }:
        raise ValueError("backup restore operator target observations are invalid")
    database = target.get("database")
    objects = target.get("objects")
    exclusion = target.get("concurrencyExclusion")
    if (
        not isinstance(database, dict)
        or set(database) != {"host", "port", "name", "identitySha256", "ownership", "pre", "post"}
        or not isinstance(objects, dict)
        or set(objects)
        != {
            "endpoint",
            "region",
            "namespaceId",
            "bucket",
            "identitySha256",
            "ownership",
            "pre",
            "post",
        }
        or not isinstance(exclusion, dict)
        or set(exclusion) != {"mode", "identitySha256", "heldThroughPostValidation"}
    ):
        raise ValueError("backup restore operator target observations are invalid")
    database_pre = database.get("pre")
    database_post = database.get("post")
    objects_pre = objects.get("pre")
    objects_post = objects.get("post")
    if (
        not isinstance(database_pre, dict)
        or set(database_pre) != {"identitySha256", "userObjectCount", "currentRole", "owner"}
        or not isinstance(database_post, dict)
        or set(database_post) != {"identitySha256", "userObjectCount", "currentRole", "owner"}
        or not isinstance(objects_pre, dict)
        or set(objects_pre)
        != {
            "identitySha256",
            "versioningEnabled",
            "objectCount",
            "versionCount",
            "deleteMarkerCount",
            "ownerIdSha256",
        }
        or not isinstance(objects_post, dict)
        or set(objects_post) != set(objects_pre)
    ):
        raise ValueError("backup restore operator target observations are invalid")
    try:
        expected_object_identity = physical_object_store_identity_sha256(
            target_object_endpoint,
            target_object_region,
            target_bucket,
            objects_pre.get("ownerIdSha256"),
        )
    except ValueError:
        raise ValueError("backup restore operator target observations are invalid") from None
    database_identity = database.get("identitySha256")
    object_identity = objects.get("identitySha256")
    if (
        target.get("targetConfigSha256") != target_config_sha256
        or target.get("provisioningReceiptSha256") != provisioning_receipt_sha256
        or database.get("host") != target_database_host
        or database.get("port") != target_database_port
        or database.get("name") != target_database_name
        or database_identity != target_database_identity
        or database.get("ownership") != database_ownership
        or database_pre.get("identitySha256") != database_identity
        or database_post.get("identitySha256") != database_identity
        or database_pre.get("userObjectCount") != 0
        or isinstance(database_post.get("userObjectCount"), bool)
        or not isinstance(database_post.get("userObjectCount"), int)
        or database_post["userObjectCount"] <= 0
        or database_pre.get("currentRole") != _RESTORE_DATABASE_USER
        or database_post.get("currentRole") != _RESTORE_DATABASE_USER
        or database_pre.get("owner") != _RESTORE_DATABASE_USER
        or database_post.get("owner") != _RESTORE_DATABASE_USER
        or objects.get("endpoint") != target_object_endpoint
        or objects.get("region") != target_object_region
        or objects.get("namespaceId") != target_object_namespace
        or objects.get("bucket") != target_bucket
        or object_identity != expected_object_identity
        or object_identity == source["objectStoreIdentitySha256"]
        or objects.get("ownership") != object_namespace_ownership
        or objects_pre.get("identitySha256") != object_identity
        or objects_post.get("identitySha256") != object_identity
        or objects_pre.get("versioningEnabled") is not True
        or objects_post.get("versioningEnabled") is not True
        or objects_pre.get("objectCount") != 0
        or objects_pre.get("versionCount") != 0
        or objects_pre.get("deleteMarkerCount") != 0
        or objects_post.get("objectCount") != source["objectCount"]
        or objects_post.get("versionCount") != source["objectCount"]
        or objects_post.get("deleteMarkerCount") != 0
        or not _valid_sha256(objects_pre.get("ownerIdSha256"))
        or objects_pre.get("ownerIdSha256") != objects_post.get("ownerIdSha256")
        or exclusion.get("mode") != "postgresql-session-advisory-lock"
        or exclusion.get("identitySha256") != database_identity
        or exclusion.get("heldThroughPostValidation") is not True
    ):
        raise ValueError("backup restore operator target observations are invalid")
    return payload, body


def _command_arguments(
    config: BackupRestoreProbeConfig,
    *,
    backup_directory: Path,
    target_config: Path,
    target_config_sha256: str,
    provisioning_receipt: Path,
    provisioning_receipt_sha256: str,
    target_secret_directory: Path,
    output_directory: Path,
    python_executable: Path,
    pg_restore_executable: Path,
    deadline_monotonic: float,
    secret_values: Sequence[bytes],
) -> list[str]:
    candidate_sha256 = hashlib.sha256(_canonical_json(dict(config.candidate))).hexdigest()
    arguments = [
        str(python_executable),
        str((SCRIPTS_ROOT / "restore_teaching_validation.py").resolve(strict=True)),
        "--backup-dir",
        str(backup_directory),
        "--target-config",
        str(target_config),
        "--target-config-sha256",
        target_config_sha256,
        "--provisioning-receipt",
        str(provisioning_receipt),
        "--provisioning-receipt-sha256",
        provisioning_receipt_sha256,
        "--target-secret-dir",
        str(target_secret_directory),
        "--run-id",
        str(config.release_run["runId"]),
        "--environment-id",
        str(config.release_run["environmentId"]),
        "--candidate-sha256",
        candidate_sha256,
        "--report",
        str(output_directory / _RESTORE_VALIDATION_ARTIFACT),
        "--pg-restore",
        str(pg_restore_executable),
        "--database-ownership",
        config.database_ownership,
        "--object-namespace-ownership",
        config.object_namespace_ownership,
        "--deadline-monotonic",
        repr(deadline_monotonic),
    ]
    serialized = "\n".join(arguments).encode("utf-8")
    _reject_secret_bytes(serialized, secret_values, "command argv")
    return arguments


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_report_directory(path: Path) -> tuple[object | int, tuple[int, int]]:
    if os.name == "nt":
        _assert_no_link_ancestors(path)
        return _open_windows_directory_handle(path)
    return _open_posix_directory_path_no_follow(path)


def _close_report_directory(handle: object | int) -> None:
    if os.name == "nt":
        _close_windows_handle(handle)
    else:
        os.close(int(handle))


def _assert_report_directory_unchanged(
    path: Path,
    handle: object | int,
    expected_identity: tuple[int, int],
) -> None:
    if os.name == "nt":
        if _windows_handle_identity(handle, directory=True) != expected_identity:
            raise ValueError("backup restore evidence report directory changed")
        reopened, current_identity = _open_windows_directory_handle(path)
        try:
            if current_identity != expected_identity:
                raise ValueError("backup restore evidence report directory changed")
        finally:
            _close_windows_handle(reopened)
        return
    details = os.fstat(int(handle))
    if not stat.S_ISDIR(details.st_mode) or _file_identity(details) != expected_identity:
        raise ValueError("backup restore evidence report directory changed")
    reopened, current_identity = _open_posix_directory_path_no_follow(path)
    try:
        if current_identity != expected_identity:
            raise ValueError("backup restore evidence report directory changed")
    finally:
        os.close(reopened)


def _report_descriptor_identity(descriptor: int) -> tuple[int, int]:
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("backup restore evidence report staging file is invalid")
    if os.name == "nt":
        import msvcrt

        return _windows_handle_identity(
            msvcrt.get_osfhandle(descriptor),
            directory=False,
        )
    return _file_identity(details)


def _report_relative_identity(
    directory_handle: object | int,
    name: str,
) -> tuple[int, int]:
    if Path(name).name != name or not name:
        raise ValueError("backup restore evidence report filename is invalid")
    if os.name == "nt":
        handle, identity = _open_windows_regular_file_relative(
            directory_handle,
            name,
            share_access=0x00000001 | 0x00000002 | 0x00000004,
        )
        try:
            return identity
        finally:
            _close_windows_handle(handle)
    details = os.stat(
        name,
        dir_fd=int(directory_handle),
        follow_symlinks=False,
    )
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("backup restore evidence report file is invalid")
    return _file_identity(details)


def _create_report_staging(
    directory_handle: object | int,
    target_name: str,
) -> tuple[int, str | None, tuple[int, int]]:
    if os.name == "nt":
        import msvcrt

        for _attempt in range(32):
            name = f".{target_name}.{secrets.token_hex(12)}.tmp"
            try:
                native_handle, identity = _create_windows_staging_file(
                    directory_handle,
                    name,
                    b"",
                )
            except OSError as exc:
                error = getattr(exc, "winerror", None) or exc.errno
                if error in {errno.EEXIST, 80, 183}:
                    continue
                raise
            try:
                handle_value = getattr(native_handle, "value", native_handle)
                descriptor = msvcrt.open_osfhandle(
                    int(handle_value),
                    os.O_RDWR | getattr(os, "O_BINARY", 0),
                )
            except BaseException:
                try:
                    _delete_windows_file_on_close(native_handle)
                finally:
                    _close_windows_handle(native_handle)
                raise
            return descriptor, name, identity
        raise RuntimeError("backup restore evidence report staging name is unavailable")

    temporary_flag = getattr(os, "O_TMPFILE", 0)
    if not temporary_flag:
        raise OSError(errno.ENOTSUP, "unnamed report staging requires O_TMPFILE")
    descriptor = os.open(
        ".",
        os.O_RDWR | os.O_CLOEXEC | temporary_flag,
        0o600,
        dir_fd=int(directory_handle),
    )
    try:
        return descriptor, None, _report_descriptor_identity(descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _link_report_file_descriptor(
    file_descriptor: int,
    directory_handle: object | int,
    target_name: str,
) -> None:
    if Path(target_name).name != target_name or not target_name:
        raise ValueError("backup restore evidence report target name is invalid")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class _FileLinkInformation(ctypes.Structure):
            _fields_ = (
                ("replaceIfExists", wintypes.BOOLEAN),
                ("rootDirectory", wintypes.HANDLE),
                ("fileNameLength", wintypes.DWORD),
                ("fileName", wintypes.WCHAR * len(target_name)),
            )

        class _IoStatusBlock(ctypes.Structure):
            _fields_ = (("status", ctypes.c_void_p), ("information", ctypes.c_size_t))

        information = _FileLinkInformation()
        information.replaceIfExists = False
        information.rootDirectory = directory_handle
        information.fileNameLength = len(target_name.encode("utf-16-le"))
        information.fileName = target_name
        io_status = _IoStatusBlock()
        ntdll = ctypes.WinDLL("ntdll")
        set_information = ntdll.NtSetInformationFile
        set_information.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
        )
        set_information.restype = wintypes.LONG
        status = set_information(
            msvcrt.get_osfhandle(file_descriptor),
            ctypes.byref(io_status),
            ctypes.byref(information),
            _FileLinkInformation.fileName.offset + information.fileNameLength,
            11,
        )
        if status < 0:
            status_to_error = ntdll.RtlNtStatusToDosError
            status_to_error.argtypes = (wintypes.LONG,)
            status_to_error.restype = wintypes.ULONG
            error = status_to_error(status)
            raise OSError(error, "cannot publish backup restore evidence report")
        return

    import ctypes

    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "descriptor-relative hard links require Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    linkat.restype = ctypes.c_int
    encoded_target = os.fsencode(target_name)
    if linkat(file_descriptor, b"", int(directory_handle), encoded_target, 0x1000) == 0:
        return
    first_error = ctypes.get_errno()
    if first_error not in {errno.EINVAL, errno.ENOENT, errno.EPERM}:
        raise OSError(first_error, "cannot publish backup restore evidence report")
    proc_source = os.fsencode(f"/proc/self/fd/{file_descriptor}")
    if linkat(-100, proc_source, int(directory_handle), encoded_target, 0x400) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "cannot publish backup restore evidence report")


def _read_report_relative(
    directory_handle: object | int,
    name: str,
) -> tuple[tuple[int, int], bytes]:
    if os.name == "nt":
        handle, identity = _open_windows_regular_file_relative(
            directory_handle,
            name,
            share_access=0x00000001 | 0x00000002 | 0x00000004,
        )
        try:
            return identity, _read_windows_file_handle(handle)
        finally:
            _close_windows_handle(handle)
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=int(directory_handle),
    )
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("backup restore evidence report file is invalid")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return _file_identity(details), b"".join(chunks)
    finally:
        os.close(descriptor)


def _close_report_staging_handle(
    descriptor: int,
    directory_handle: object | int,
    staging_name: str | None,
    expected_identity: tuple[int, int],
) -> None:
    if os.name == "nt":
        import msvcrt

        delete_failure: BaseException | None = None
        try:
            _delete_windows_file_on_close(msvcrt.get_osfhandle(descriptor))
        except BaseException as exc:
            delete_failure = exc
        try:
            os.close(descriptor)
        except BaseException as exc:
            if delete_failure is not None:
                delete_failure.add_note(
                    "backup restore evidence staging descriptor close also failed"
                )
                raise delete_failure
            raise exc
        if delete_failure is None:
            return
        if staging_name is None:
            raise delete_failure
        fallback_handle: object | None = None
        try:
            try:
                fallback_handle, identity = _open_windows_regular_file_relative(
                    directory_handle,
                    staging_name,
                    share_access=0x00000001 | 0x00000002 | 0x00000004,
                    deletable=True,
                )
            except FileNotFoundError:
                return
            if identity != expected_identity:
                raise ValueError("backup restore evidence staging file changed")
            _delete_windows_file_on_close(fallback_handle)
        except BaseException as fallback_failure:
            delete_failure.add_note(
                "backup restore evidence exact-identity staging cleanup retry failed: "
                f"{type(fallback_failure).__name__}"
            )
            raise delete_failure
        finally:
            if fallback_handle is not None:
                _close_windows_handle(fallback_handle)
        return
    os.close(descriptor)


def _fsync_report_directory(directory_handle: object | int) -> None:
    if os.name != "nt":
        os.fsync(int(directory_handle))


def _record_report_staging_residual() -> None:
    message = "published backup restore evidence report retained an owned staging residual"
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except BaseException:
        try:
            sys.stderr.write(f"warning: {message}\n")
        except BaseException:
            pass


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path = Path(os.path.abspath(path))
    body = canonical_backup_restore_report(report)
    directory_handle, directory_identity = _open_report_directory(path.parent)
    descriptor: int | None = None
    staging_name: str | None = None
    staging_identity: tuple[int, int] | None = None
    committed = False
    try:
        _assert_report_directory_unchanged(
            path.parent,
            directory_handle,
            directory_identity,
        )
        try:
            descriptor, staging_name, staging_identity = _create_report_staging(
                directory_handle,
                path.name,
            )
        except OSError:
            raise RuntimeError(
                "backup restore evidence report staging could not be created"
            ) from None
        if staging_name is not None and (
            _report_relative_identity(directory_handle, staging_name) != staging_identity
        ):
            raise ValueError("backup restore evidence report staging file changed")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if _report_descriptor_identity(descriptor) != staging_identity or (
            staging_name is not None
            and _report_relative_identity(directory_handle, staging_name) != staging_identity
        ):
            raise ValueError("backup restore evidence report staging file changed")
        _assert_report_directory_unchanged(
            path.parent,
            directory_handle,
            directory_identity,
        )
        try:
            _link_report_file_descriptor(descriptor, directory_handle, path.name)
        except OSError as exc:
            error = getattr(exc, "winerror", None) or exc.errno
            if error in {errno.EEXIST, 80, 183}:
                raise FileExistsError("backup restore evidence report already exists") from None
            raise
        committed = True
        try:
            published_identity, published_body = _read_report_relative(
                directory_handle,
                path.name,
            )
        except BaseException as readback_failure:
            try:
                published_identity, published_body = _read_report_relative(
                    directory_handle,
                    path.name,
                )
            except BaseException as reconciliation_failure:
                readback_failure.add_note(
                    "published backup restore evidence report could not be reconciled: "
                    f"{type(reconciliation_failure).__name__}"
                )
                raise readback_failure
        if published_identity != staging_identity or published_body != body:
            raise ValueError("published backup restore evidence report changed")
        # The no-clobber hard link is the commit point. A later directory-sync
        # limitation cannot make the already-published canonical proof partial.
        try:
            _fsync_report_directory(directory_handle)
        except BaseException:
            pass
    finally:
        primary_failure = sys.exception()
        cleanup_failure: BaseException | None = None
        if descriptor is not None and staging_identity is not None:
            try:
                _close_report_staging_handle(
                    descriptor,
                    directory_handle,
                    staging_name,
                    staging_identity,
                )
            except BaseException as exc:
                if primary_failure is not None and not committed:
                    primary_failure.add_note(
                        "backup restore evidence staging descriptor cleanup failed"
                    )
                elif committed:
                    _record_report_staging_residual()
                elif not committed and cleanup_failure is None:
                    cleanup_failure = exc
        try:
            _close_report_directory(directory_handle)
        except BaseException as exc:
            if primary_failure is not None and not committed:
                primary_failure.add_note("backup restore evidence directory cleanup failed")
            elif not committed and cleanup_failure is None:
                cleanup_failure = exc
        if cleanup_failure is not None:
            raise cleanup_failure


def run_backup_restore_probe(
    config: BackupRestoreProbeConfig,
    *,
    verified_backup_loader: VerifiedBackupLoader = _default_verified_backup_loader,
    verified_backup_rechecker: VerifiedBackupRechecker = _default_verified_backup_rechecker,
    target_config_loader: TargetConfigLoader = _default_target_config_loader,
    command_runner: CommandRunner = _default_command_runner,
    environment: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
) -> Path:
    """Run one restore validation and publish evidence without cleaning any target."""

    _validate_config(config)
    secret_snapshot_files = _read_secret_snapshot_files(config.target_secret_directory)
    live_secret_values = tuple(
        item.body.rstrip(b"\r\n") for item in secret_snapshot_files if item.body.rstrip(b"\r\n")
    )
    secret_values = tuple(dict.fromkeys((*live_secret_values, *config.forbidden_secret_values)))
    if not secret_values:
        raise ValueError("backup restore secret scanner requires secret material")
    source_backup_directory = _resolve_existing_directory(
        config.backup_directory,
        "backup directory",
    )
    target_config_body = _canonical_target_config(config.target_config_path)
    provisioning_receipt_body = _canonical_provisioning_receipt(config.provisioning_receipt_path)
    try:
        source_provenance_body = canonical_source_provenance(dict(config.source_provenance))
    except (TypeError, ValueError):
        raise ValueError("backup restore source provenance is invalid") from None
    identity_bodies = (
        ("candidate input", _canonical_json(dict(config.candidate))),
        ("release run input", _canonical_json(dict(config.release_run))),
        ("target config input", target_config_body),
        ("target provisioning receipt input", provisioning_receipt_body),
        ("source provenance input", source_provenance_body),
    )
    for context, body in identity_bodies:
        _reject_secret_json_bytes(body, secret_values, context)
    provisioning_receipt_sha256 = hashlib.sha256(provisioning_receipt_body).hexdigest()
    candidate_sha256 = hashlib.sha256(_canonical_json(dict(config.candidate))).hexdigest()
    parse_target_provisioning_receipt(
        provisioning_receipt_body,
        provisioning_receipt_sha256=provisioning_receipt_sha256,
        candidate_sha256=candidate_sha256,
        release_run=config.release_run,
        database_disposition=config.database_ownership,
        object_store_disposition=config.object_namespace_ownership,
    )
    python_executable = _resolve_existing_file(config.python_executable, "Python executable")
    pg_restore_executable = _resolve_existing_file(
        config.pg_restore_executable,
        "pg_restore executable",
    )
    try:
        requested_output = (
            Path(config.output_directory).parent.resolve(strict=True)
            / Path(config.output_directory).name
        )
    except OSError:
        raise ValueError("backup restore output directory parent is unavailable") from None
    try:
        requested_output.relative_to(source_backup_directory)
    except ValueError:
        pass
    else:
        raise ValueError("backup restore output directory must be outside the source archive")

    output_directory = _prepare_output_directory(config.output_directory)
    source_snapshot = _snapshot_source_archive(source_backup_directory, output_directory)
    try:
        backup = verified_backup_loader(source_snapshot.directory)
        _require_backup_uses_snapshot(backup, source_snapshot)
        _verify_source_archive_snapshot(source_snapshot)
    except Exception:
        raise ValueError("backup restore source archive is invalid") from None
    source = _source_payload(backup)
    try:
        validate_backup_restore_source_provenance(
            config.source_provenance,
            candidate=config.candidate,
            release_run=config.release_run,
            source=source,
        )
    except (TypeError, ValueError):
        raise ValueError("backup restore source provenance is invalid") from None
    _reject_secret_json_bytes(source_provenance_body, secret_values, "source provenance input")
    _reject_secret_json_bytes(_canonical_json(source), secret_values, "source archive input")
    source_provenance_sha256 = hashlib.sha256(source_provenance_body).hexdigest()
    source["provenanceSha256"] = source_provenance_sha256
    target_config_sha256 = hashlib.sha256(target_config_body).hexdigest()

    source_provenance_path = output_directory / _SOURCE_PROVENANCE_ARTIFACT
    target_config_path = output_directory / _TARGET_CONFIG_ARTIFACT
    provisioning_receipt_path = output_directory / _PROVISIONING_RECEIPT_ARTIFACT
    _write_input_snapshot(source_provenance_path, source_provenance_body)
    _write_input_snapshot(target_config_path, target_config_body)
    _write_input_snapshot(provisioning_receipt_path, provisioning_receipt_body)
    try:
        target_config = target_config_loader(target_config_path)
    except Exception:
        raise ValueError("backup restore target configuration is invalid") from None
    try:
        target_database_id = target_config.database_name
        target_namespace = getattr(target_config, "object_namespace_id", None) or getattr(
            target_config,
            "object_store_namespace_id",
        )
        target_bucket = getattr(target_config, "object_bucket", None) or getattr(
            target_config,
            "object_store_bucket",
        )
    except AttributeError:
        raise ValueError("backup restore target configuration is invalid") from None
    if (
        not isinstance(target_database_id, str)
        or _PUBLIC_ID.fullmatch(target_database_id) is None
        or not isinstance(target_namespace, str)
        or _PUBLIC_ID.fullmatch(target_namespace) is None
        or not isinstance(target_bucket, str)
        or _PUBLIC_ID.fullmatch(target_bucket) is None
    ):
        raise ValueError("backup restore target configuration is invalid")
    target_namespace_id = f"{target_namespace}:{target_bucket}"
    if _PUBLIC_ID.fullmatch(target_namespace_id) is None:
        raise ValueError("backup restore target object namespace identity is invalid")

    started_at = now()
    started_monotonic = monotonic()
    deadline_monotonic = started_monotonic + config.timeout_seconds
    if not math.isfinite(deadline_monotonic):
        raise ValueError("backup restore deadline is invalid")
    target_secret_snapshot = _materialize_target_secret_snapshot(
        output_directory,
        secret_snapshot_files,
    )
    try:
        arguments = _command_arguments(
            config,
            backup_directory=source_snapshot.directory,
            target_config=target_config_path,
            target_config_sha256=target_config_sha256,
            provisioning_receipt=provisioning_receipt_path,
            provisioning_receipt_sha256=provisioning_receipt_sha256,
            target_secret_directory=target_secret_snapshot,
            output_directory=output_directory,
            python_executable=python_executable,
            pg_restore_executable=pg_restore_executable,
            deadline_monotonic=deadline_monotonic,
            secret_values=secret_values,
        )
        try:
            completed = command_runner(
                arguments,
                cwd=SCRIPTS_ROOT.parent,
                env=_safe_environment(environment or os.environ),
                deadline_monotonic=deadline_monotonic,
                cleanup_grace_seconds=_PROCESS_CLEANUP_GRACE_SECONDS,
                check=False,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("backup restore command failed") from None
    finally:
        primary_failure = sys.exception()
        try:
            _cleanup_target_secret_snapshot(target_secret_snapshot, secret_snapshot_files)
        except BaseException:
            if primary_failure is not None:
                primary_failure.add_note("backup restore target secret snapshot cleanup failed")
            else:
                raise
    finished_monotonic = monotonic()
    finished_at = now()
    stdout = bytes(completed.stdout or b"")
    stderr = bytes(completed.stderr or b"")
    if len(stdout) > _MAX_OPERATOR_OUTPUT_BYTES or len(stderr) > _MAX_OPERATOR_OUTPUT_BYTES:
        raise RuntimeError("backup restore command output is too large")
    _reject_command_output_secrets(stdout, secret_values, "command output")
    _reject_command_output_secrets(stderr, secret_values, "command output")
    if isinstance(completed.returncode, bool) or completed.returncode != 0:
        raise RuntimeError("backup restore command failed")

    try:
        _verify_source_archive_snapshot(source_snapshot)
        current_backup = verified_backup_rechecker(backup)
        _require_backup_uses_snapshot(current_backup, source_snapshot)
        _verify_source_archive_snapshot(source_snapshot)
    except Exception:
        raise ValueError("backup restore source archive changed after verification") from None
    current_source = _source_payload(current_backup)
    current_source["provenanceSha256"] = source_provenance_sha256
    if current_source != source:
        raise ValueError("backup restore source archive changed after verification")

    restore_path = output_directory / _RESTORE_VALIDATION_ARTIFACT
    restore_payload, restore_body = _read_restore_validation(
        restore_path,
        run_id=str(config.release_run["runId"]),
        source=source,
        target_config=target_config,
        target_config_sha256=target_config_sha256,
        provisioning_receipt_sha256=provisioning_receipt_sha256,
        database_ownership=config.database_ownership,
        object_namespace_ownership=config.object_namespace_ownership,
    )
    target_database_identity = restore_payload["targetDatabaseIdentitySha256"]
    operator_target = restore_payload["target"]
    operator_database = operator_target["database"]
    operator_database_pre = operator_database["pre"]
    operator_database_post = operator_database["post"]
    operator_objects = operator_target["objects"]
    operator_objects_pre = operator_objects["pre"]
    operator_objects_post = operator_objects["post"]
    target_object_identity = operator_objects["identitySha256"]
    operator_exclusion = operator_target["concurrencyExclusion"]
    parse_target_provisioning_receipt(
        provisioning_receipt_body,
        provisioning_receipt_sha256=provisioning_receipt_sha256,
        candidate_sha256=candidate_sha256,
        release_run=config.release_run,
        database_disposition=config.database_ownership,
        object_store_disposition=config.object_namespace_ownership,
        database_identity_sha256=target_database_identity,
        object_store_identity_sha256=target_object_identity,
    )
    operator_target_observations_sha256 = hashlib.sha256(
        _canonical_json(operator_target)
    ).hexdigest()
    restore_sha256 = hashlib.sha256(restore_body).hexdigest()
    duration_ms = max(1, round((finished_monotonic - started_monotonic) * 1000))
    permission_finding_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "role": "yfeistai_app",
                "restoreValidationSha256": restore_sha256,
                "targetDatabaseIdentitySha256": target_database_identity,
                "validated": "app_role_access",
            }
        )
    ).hexdigest()
    report: dict[str, Any] = {
        "schemaVersion": BACKUP_RESTORE_SCHEMA_VERSION,
        "producer": BACKUP_RESTORE_PRODUCER,
        "candidate": dict(config.candidate),
        "releaseRun": dict(config.release_run),
        "observedAt": _utc_text(now()),
        "consistency": {
            "databaseSnapshot": "postgresql-consistent-dump",
            "objectSnapshot": "version-pinned-inventory",
            "crossSystemAtomic": False,
            "partialBackupArtifacts": "retained",
        },
        "source": source,
        "target": {
            "databaseId": target_database_id,
            "databaseIdentitySha256": target_database_identity,
            "databaseOwnership": config.database_ownership,
            "databaseWasEmpty": operator_database_pre["userObjectCount"] == 0,
            "databaseDistinctFromSource": (
                target_database_identity != source["databaseIdentitySha256"]
            ),
            "databaseHost": operator_database["host"],
            "databasePort": operator_database["port"],
            "databaseCurrentRole": operator_database_post["currentRole"],
            "databaseOwner": operator_database_post["owner"],
            "databasePreRestoreUserObjectCount": operator_database_pre["userObjectCount"],
            "databasePostRestoreUserObjectCount": operator_database_post["userObjectCount"],
            "objectNamespaceId": target_namespace_id,
            "objectStoreIdentitySha256": target_object_identity,
            "objectNamespaceOwnership": config.object_namespace_ownership,
            "objectNamespaceWasEmpty": (
                operator_objects_pre["objectCount"] == 0
                and operator_objects_pre["versionCount"] == 0
                and operator_objects_pre["deleteMarkerCount"] == 0
            ),
            "objectNamespaceDistinctFromSource": (
                target_object_identity != source["objectStoreIdentitySha256"]
            ),
            "objectVersioningEnabled": (
                operator_objects_pre["versioningEnabled"] is True
                and operator_objects_post["versioningEnabled"] is True
            ),
            "objectEndpoint": operator_objects["endpoint"],
            "objectRegion": operator_objects["region"],
            "objectNamespace": operator_objects["namespaceId"],
            "objectBucket": operator_objects["bucket"],
            "objectOwnerIdSha256": operator_objects_post["ownerIdSha256"],
            "objectPreRestoreObjectCount": operator_objects_pre["objectCount"],
            "objectPostRestoreObjectCount": operator_objects_post["objectCount"],
            "objectPreRestoreVersionCount": operator_objects_pre["versionCount"],
            "objectPostRestoreVersionCount": operator_objects_post["versionCount"],
            "objectPreRestoreDeleteMarkerCount": operator_objects_pre["deleteMarkerCount"],
            "objectPostRestoreDeleteMarkerCount": operator_objects_post["deleteMarkerCount"],
            "concurrencyExclusionMode": operator_exclusion["mode"],
            "concurrencyExclusionIdentitySha256": operator_exclusion["identitySha256"],
            "concurrencyExclusionHeldThroughPostValidation": operator_exclusion[
                "heldThroughPostValidation"
            ],
            "operatorTargetObservationsSha256": operator_target_observations_sha256,
            "targetConfigSha256": target_config_sha256,
            "provisioningReceiptSha256": provisioning_receipt_sha256,
        },
        "execution": {
            "commands": [
                {
                    "sequence": 1,
                    "name": "restore-and-verify",
                    "argv": arguments,
                    "nativeExit": completed.returncode,
                    "startedAt": _utc_text(started_at),
                    "finishedAt": _utc_text(finished_at),
                    "durationMs": duration_ms,
                    "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
                    "stderrSha256": hashlib.sha256(stderr).hexdigest(),
                    "artifact": _RESTORE_VALIDATION_ARTIFACT,
                    "artifactSha256": restore_sha256,
                }
            ],
            "artifactSha256s": {
                "sourceManifest": source["manifestSha256"],
                "sourceObjectInventory": source["objectInventorySha256"],
                "sourceDatabaseDump": source["databaseSha256"],
                "sourceProvenance": source_provenance_sha256,
                "targetConfigSnapshot": target_config_sha256,
                "targetProvisioningReceipt": provisioning_receipt_sha256,
                "restoreValidation": restore_sha256,
            },
        },
        "findings": {
            "database": {
                "restored": True,
                "dumpRestoreSingleTransaction": True,
                "postRestoreMutationsAtomic": False,
                "sourceDatabaseSha256": source["databaseSha256"],
                "platformSchemaRevision": source["platformSchemaRevision"],
                "schemaRevisions": source["schemaRevisions"],
                "classroomVersionsCount": source["classroomVersionsCount"],
                "learningEventsCount": source["learningEventsCount"],
                "preRestoreUserObjectCount": operator_database_pre["userObjectCount"],
                "postRestoreUserObjectCount": operator_database_post["userObjectCount"],
            },
            "objects": {
                "restored": True,
                "createOnly": True,
                "readbackVerified": True,
                "objectCount": source["objectCount"],
                "inventorySha256": source["objectInventorySha256"],
                "contentHashesVerified": True,
                "sourceRevisionsVerified": True,
                "versionIdsVerified": True,
                "preRestoreObjectCount": operator_objects_pre["objectCount"],
                "postRestoreObjectCount": operator_objects_post["objectCount"],
                "preRestoreVersionCount": operator_objects_pre["versionCount"],
                "postRestoreVersionCount": operator_objects_post["versionCount"],
                "preRestoreDeleteMarkerCount": operator_objects_pre["deleteMarkerCount"],
                "postRestoreDeleteMarkerCount": operator_objects_post["deleteMarkerCount"],
            },
            "permissions": {
                "role": "yfeistai_app",
                "verified": True,
                "findingSha256": permission_finding_sha256,
            },
        },
        "retention": {
            "policy": "no-destructive-cleanup",
            "cleanupAttempted": False,
            "fullCleanupClaimed": False,
            "targets": [
                {
                    "kind": "source-backup",
                    "id": source["manifestSha256"],
                    "ownership": "retained-audit",
                },
                {
                    "kind": "database",
                    "id": target_database_id,
                    "ownership": config.database_ownership,
                },
                {
                    "kind": "object-namespace",
                    "id": target_namespace_id,
                    "ownership": config.object_namespace_ownership,
                },
                {
                    "kind": "report",
                    "id": config.release_run["runId"],
                    "ownership": "retained-audit",
                },
            ],
        },
    }
    body = canonical_backup_restore_report(report)
    _reject_secret_json_bytes(body, secret_values, "evidence report")
    parse_backup_restore_report(
        body,
        candidate=config.candidate,
        release_run=config.release_run,
        expected_source_manifest_sha256=str(source["manifestSha256"]),
        expected_source_archive_fingerprint_sha256=str(source["archiveFingerprintSha256"]),
        expected_database_ownership=config.database_ownership,
        expected_object_namespace_ownership=config.object_namespace_ownership,
        operator_artifact_body=restore_body,
        verified_backup=current_backup,
        source_provenance_body=source_provenance_body,
        target_config_body=target_config_body,
        provisioning_receipt_body=provisioning_receipt_body,
        forbidden_secret_values=secret_values,
    )
    report_path = output_directory / _BACKUP_RESTORE_ARTIFACT
    _write_report(report_path, report)
    return report_path


def _load_canonical_mapping(path: Path, field_name: str) -> dict[str, object]:
    source = _resolve_existing_file(path, field_name)
    try:
        body = source.read_bytes()
        if not 0 < len(body) <= _MAX_IDENTITY_FILE_BYTES:
            raise ValueError
        payload = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError(f"backup restore {field_name} is invalid") from None
    if not isinstance(payload, dict) or body != _canonical_json(payload):
        raise ValueError(f"backup restore {field_name} must be canonical JSON")
    return payload


def _secret_directory_entries(path: Path) -> tuple[tuple[str, os.stat_result], ...]:
    try:
        with os.scandir(path) as iterator:
            return tuple(
                sorted(
                    ((entry.name, Path(entry.path).lstat()) for entry in iterator),
                    key=lambda item: item[0],
                )
            )
    except OSError:
        raise ValueError("backup restore target secret directory is unavailable") from None


def _read_secret_file_no_follow(path: Path, expected_stat: os.stat_result) -> bytes:
    if (
        _is_reparse_point(expected_stat)
        or not stat.S_ISREG(expected_stat.st_mode)
        or expected_stat.st_size > _MAX_IDENTITY_FILE_BYTES
    ):
        raise ValueError("backup restore target secret directory contains an unsafe entry")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("backup restore target secret directory is unavailable") from None
    try:
        opened_stat = os.fstat(descriptor)
        if _archive_stat_signature(opened_stat) != _archive_stat_signature(expected_stat):
            raise ValueError("backup restore target secret changed while being snapshotted")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_IDENTITY_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_IDENTITY_FILE_BYTES:
                raise ValueError("backup restore target secret file is too large")
        final_stat = os.fstat(descriptor)
        if _archive_stat_signature(final_stat) != _archive_stat_signature(opened_stat):
            raise ValueError("backup restore target secret changed while being snapshotted")
    except OSError:
        raise ValueError("backup restore target secret directory is unavailable") from None
    finally:
        os.close(descriptor)
    try:
        current_stat = path.lstat()
    except OSError:
        raise ValueError("backup restore target secret directory is unavailable") from None
    if _archive_stat_signature(current_stat) != _archive_stat_signature(expected_stat):
        raise ValueError("backup restore target secret changed while being snapshotted")
    return b"".join(chunks)


def _read_secret_snapshot_files(secret_directory: Path) -> tuple[_SecretSnapshotFile, ...]:
    root = _resolve_existing_directory(secret_directory, "target secret directory")
    try:
        root_stat = root.lstat()
    except OSError:
        raise ValueError("backup restore target secret directory is unavailable") from None
    if _is_reparse_point(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("backup restore target secret directory is unsafe")
    initial_entries = _secret_directory_entries(root)
    entries_by_name = dict(initial_entries)
    snapshot_files: list[_SecretSnapshotFile] = []
    for name in _RESTORE_OPERATOR_SECRET_NAMES:
        file_stat = entries_by_name.get(name)
        if file_stat is None or _is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("backup restore target secret is unavailable or unsafe")
        snapshot_files.append(
            _SecretSnapshotFile(
                name=name,
                body=_read_secret_file_no_follow(root / name, file_stat),
            )
        )
    final_entries = _secret_directory_entries(root)
    try:
        final_root_stat = root.lstat()
    except OSError:
        raise ValueError("backup restore target secret directory is unavailable") from None
    if tuple(
        (name, _archive_stat_signature(file_stat)) for name, file_stat in final_entries
    ) != tuple(
        (name, _archive_stat_signature(file_stat)) for name, file_stat in initial_entries
    ) or _archive_stat_signature(final_root_stat) != _archive_stat_signature(root_stat):
        raise ValueError("backup restore target secret changed while being snapshotted")
    if any(not item.body.rstrip(b"\r\n") for item in snapshot_files):
        raise ValueError("backup restore target secret directory contains invalid secret material")
    return tuple(snapshot_files)


def _set_private_secret_mode(path: Path, mode: int) -> None:
    if os.name != "nt":
        os.chmod(path, mode)
        return
    from deeptutor.teaching.secret_permissions import (
        restrict_secret_file,
        secret_file_is_restricted,
    )

    restrict_secret_file(path)
    if not secret_file_is_restricted(path):
        raise PermissionError("backup restore target secret snapshot permissions are unsafe")


def _write_secret_snapshot_file(path: Path, body: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, _PRIVATE_FILE_MODE)
    try:
        _set_private_secret_mode(path, _PRIVATE_FILE_MODE)
        _write_all(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _set_private_secret_mode(path, _PRIVATE_READONLY_FILE_MODE)
    file_stat = path.lstat()
    if (
        _is_reparse_point(file_stat)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_size != len(body)
    ):
        raise ValueError("backup restore target secret snapshot is unsafe")


def _cleanup_target_secret_snapshot(
    snapshot_directory: Path,
    snapshot_files: Sequence[_SecretSnapshotFile],
) -> None:
    try:
        directory_stat = snapshot_directory.lstat()
    except FileNotFoundError:
        return
    if _is_reparse_point(directory_stat) or not stat.S_ISDIR(directory_stat.st_mode):
        raise RuntimeError("backup restore target secret snapshot cleanup is unsafe")
    cleanup_failure: BaseException | None = None
    try:
        _set_private_secret_mode(snapshot_directory, _PRIVATE_DIRECTORY_MODE)
    except BaseException as exc:
        cleanup_failure = exc
    for item in snapshot_files:
        try:
            (snapshot_directory / item.name).unlink(missing_ok=True)
        except BaseException as exc:
            if cleanup_failure is None:
                cleanup_failure = exc
    try:
        snapshot_directory.rmdir()
    except BaseException as exc:
        if cleanup_failure is None:
            cleanup_failure = exc
    if cleanup_failure is not None:
        raise cleanup_failure


def _materialize_target_secret_snapshot(
    output_directory: Path,
    snapshot_files: Sequence[_SecretSnapshotFile],
) -> Path:
    snapshot_directory: Path | None = None
    try:
        evidence_root = output_directory.parent.resolve(strict=True)
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        try:
            temporary_root.relative_to(evidence_root)
        except ValueError:
            pass
        else:
            raise ValueError(
                "backup restore target secret snapshot root is inside the evidence tree"
            )
        snapshot_directory = Path(
            tempfile.mkdtemp(
                dir=temporary_root,
                prefix=_TARGET_SECRET_SNAPSHOT_PREFIX,
            )
        )
        _set_private_secret_mode(snapshot_directory, _PRIVATE_DIRECTORY_MODE)
        for item in snapshot_files:
            _write_secret_snapshot_file(snapshot_directory / item.name, item.body)
        _fsync_directory(snapshot_directory)
        _set_private_secret_mode(snapshot_directory, _PRIVATE_READONLY_DIRECTORY_MODE)
        return snapshot_directory
    except BaseException:
        primary_failure = sys.exception()
        if snapshot_directory is not None:
            try:
                _cleanup_target_secret_snapshot(snapshot_directory, snapshot_files)
            except BaseException:
                if primary_failure is not None:
                    primary_failure.add_note("backup restore target secret snapshot cleanup failed")
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("first-release",), default="first-release")
    parser.add_argument("--candidate-file", required=True, type=Path)
    parser.add_argument("--release-run-file", required=True, type=Path)
    parser.add_argument("--source-provenance-file", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--target-config", required=True, type=Path)
    parser.add_argument("--provisioning-receipt-file", required=True, type=Path)
    parser.add_argument("--target-secret-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pg-restore", required=True, type=Path)
    parser.add_argument(
        "--database-ownership",
        required=True,
        choices=tuple(sorted(_OWNERSHIP_VALUES)),
    )
    parser.add_argument(
        "--object-namespace-ownership",
        required=True,
        choices=tuple(sorted(_OWNERSHIP_VALUES)),
    )
    parser.add_argument("--timeout-seconds", default=1800, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        config = BackupRestoreProbeConfig(
            candidate=_load_canonical_mapping(arguments.candidate_file, "candidate file"),
            release_run=_load_canonical_mapping(arguments.release_run_file, "release run file"),
            source_provenance=_load_canonical_mapping(
                arguments.source_provenance_file,
                "source provenance file",
            ),
            backup_directory=arguments.backup_dir,
            target_config_path=arguments.target_config,
            provisioning_receipt_path=arguments.provisioning_receipt_file,
            target_secret_directory=arguments.target_secret_dir,
            output_directory=arguments.output_dir,
            python_executable=Path(sys.executable),
            pg_restore_executable=arguments.pg_restore,
            database_ownership=arguments.database_ownership,
            object_namespace_ownership=arguments.object_namespace_ownership,
            timeout_seconds=arguments.timeout_seconds,
        )
        run_backup_restore_probe(config)
    except Exception:
        print("backup restore probe failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
