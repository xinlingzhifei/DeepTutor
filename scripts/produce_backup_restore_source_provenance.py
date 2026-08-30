#!/usr/bin/env python
"""Produce one candidate-bound source provenance record from a verified backup."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import warnings

SCRIPTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_ROOT.parent
for import_root in (PROJECT_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from backup_restore_contract import (  # noqa: E402
    canonical_source_provenance,
    validate_backup_restore_identity,
    validate_backup_restore_source_provenance,
)
from backup_teaching import (  # noqa: E402
    load_verified_backup,
    reverify_verified_backup,
)

VerifiedBackupLoader = Callable[[Path], object]
VerifiedBackupRechecker = Callable[[object], object]
_MAX_IDENTITY_BYTES = 64 * 1024
_WINDOWS_REPARSE_POINT = 0x400


class SourceProvenanceCommittedError(RuntimeError):
    """A formal provenance file exists, but post-commit reconciliation failed."""

    def __init__(self, message: str, output_path: Path) -> None:
        self.output_path = Path(output_path)
        super().__init__(f"source provenance committed but {message}: {self.output_path}")


@dataclass(frozen=True, slots=True)
class SourceProvenanceConfig:
    candidate: Mapping[str, object]
    release_run: Mapping[str, object]
    backup_directory: Path
    output_path: Path


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    return stat.S_ISLNK(file_stat.st_mode) or bool(
        getattr(file_stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _path_identity(path: Path) -> tuple[int, int]:
    try:
        file_stat = Path(path).lstat()
    except OSError:
        raise ValueError("source provenance path identity is unavailable") from None
    if _is_reparse_point(file_stat):
        raise ValueError("source provenance path identity is unsafe")
    return file_stat.st_dev, file_stat.st_ino


def _assert_safe_path_chain(path: Path, label: str) -> None:
    absolute = Path(path)
    if not absolute.is_absolute():
        raise ValueError(f"{label} path chain is unsafe")
    current = Path(absolute.anchor)
    components = absolute.parts[1:]
    for component in components:
        current /= component
        try:
            file_stat = current.lstat()
        except OSError:
            raise ValueError(f"{label} path chain is unsafe") from None
        if _is_reparse_point(file_stat):
            raise ValueError(f"{label} path chain is unsafe")


def _require_boundary_identity(path: Path, expected: tuple[int, int], label: str) -> None:
    _assert_safe_path_chain(path, label)
    if _path_identity(path) != expected:
        raise ValueError(f"{label} identity changed")


def _source_payload(backup: object) -> dict[str, object]:
    try:
        return {
            "manifestSha256": backup.manifest_sha256,
            "archiveFingerprintSha256": backup.archive_fingerprint_sha256,
            "databaseIdentitySha256": backup.manifest.database.identity_sha256,
            "objectStoreIdentitySha256": backup.manifest.source_object_store_identity_sha256,
        }
    except (AttributeError, TypeError, ValueError):
        raise ValueError("source provenance verified backup is invalid") from None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_no_replace(path: Path, body: bytes) -> None:
    descriptor, staging_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
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
        staging_stat = staging.lstat()
        if (
            _is_reparse_point(staging_stat)
            or not stat.S_ISREG(staging_stat.st_mode)
            or staging_stat.st_size != len(body)
            or (os.name != "nt" and stat.S_IMODE(staging_stat.st_mode) != 0o600)
        ):
            raise RuntimeError("source provenance staging file is invalid")
        os.link(staging, path)
        committed = True
        try:
            target_stat = path.lstat()
            if (
                _is_reparse_point(target_stat)
                or not stat.S_ISREG(target_stat.st_mode)
                or (target_stat.st_dev, target_stat.st_ino)
                != (staging_stat.st_dev, staging_stat.st_ino)
                or target_stat.st_size != len(body)
                or path.read_bytes() != body
            ):
                raise OSError("committed target reconciliation failed")
        except OSError as error:
            raise SourceProvenanceCommittedError(
                "target reconciliation failed",
                path,
            ) from error
        try:
            _fsync_directory(path.parent)
        except OSError as error:
            raise SourceProvenanceCommittedError(
                "directory durability reconciliation failed",
                path,
            ) from error
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
                    f"source provenance staging cleanup failed: {cleanup_error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            else:
                raise


def produce_backup_restore_source_provenance(
    config: SourceProvenanceConfig,
    *,
    verified_backup_loader: VerifiedBackupLoader = load_verified_backup,
    verified_backup_rechecker: VerifiedBackupRechecker = reverify_verified_backup,
) -> Path:
    """Verify one backup and publish its exact candidate/run-bound provenance."""

    candidate, release_run = validate_backup_restore_identity(
        config.candidate,
        config.release_run,
    )
    backup_directory = Path(config.backup_directory)
    output_path = Path(config.output_path)
    if not backup_directory.is_absolute() or not output_path.is_absolute():
        raise ValueError("source provenance paths must be absolute")
    _assert_safe_path_chain(backup_directory, "source archive")
    _assert_safe_path_chain(output_path.parent, "output parent")
    if not backup_directory.is_dir():
        raise ValueError("source provenance source archive is unavailable")
    if not output_path.parent.is_dir():
        raise ValueError("source provenance output parent is unavailable")
    try:
        resolved_backup = backup_directory.resolve(strict=True)
        resolved_output_parent = output_path.parent.resolve(strict=True)
    except OSError:
        raise ValueError("source provenance paths are unavailable") from None
    output = resolved_output_parent / output_path.name
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    output_under_backup = output.is_relative_to(resolved_backup)
    backup_under_output_parent = resolved_backup.is_relative_to(resolved_output_parent)
    if output_under_backup:
        raise ValueError("source provenance output must be outside the source archive")
    if backup_under_output_parent:
        raise ValueError("source provenance source and output paths must be disjoint")
    output_parent_stat = resolved_output_parent.lstat()
    if os.name != "nt" and (
        output_parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(output_parent_stat.st_mode) & 0o022
    ):
        raise ValueError("source provenance output parent is not restricted")
    backup_identity = _path_identity(resolved_backup)
    output_parent_identity = _path_identity(resolved_output_parent)
    backup = verified_backup_loader(resolved_backup)
    backup = verified_backup_rechecker(backup)
    _require_boundary_identity(resolved_backup, backup_identity, "source archive")
    _require_boundary_identity(resolved_output_parent, output_parent_identity, "output parent")
    source = _source_payload(backup)
    provenance = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": release_run,
        "source": source,
    }
    body = validate_backup_restore_source_provenance(
        provenance,
        candidate=candidate,
        release_run=release_run,
        source=source,
    )
    backup = verified_backup_rechecker(backup)
    if _source_payload(backup) != source:
        raise ValueError("backup changed immediately before commit")
    _require_boundary_identity(resolved_backup, backup_identity, "source archive")
    _require_boundary_identity(resolved_output_parent, output_parent_identity, "output parent")
    _publish_no_replace(output, body)
    try:
        _require_boundary_identity(resolved_backup, backup_identity, "source archive")
        _require_boundary_identity(resolved_output_parent, output_parent_identity, "output parent")
        backup = verified_backup_rechecker(backup)
        if _source_payload(backup) != source:
            raise ValueError("backup changed after commit")
    except BaseException as error:
        raise SourceProvenanceCommittedError("archive reconciliation failed", output) from error
    return output


def _load_canonical_mapping(path: Path, field: str) -> dict[str, object]:
    source = Path(path).absolute()
    _assert_safe_path_chain(source, field)
    parent_identity = _path_identity(source.parent)
    try:
        before = source.lstat()
    except OSError:
        raise ValueError(f"{field} is unavailable")
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_IDENTITY_BYTES:
        raise ValueError(f"{field} is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError:
        raise ValueError(f"{field} is unavailable") from None
    try:
        opened = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if opened_identity != before_identity or not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{field} changed while reading")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = source.lstat()
    except OSError:
        raise ValueError(f"{field} changed while reading") from None
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    current_identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    )
    if (
        after_identity != before_identity
        or current_identity != before_identity
        or len(body) != before.st_size
    ):
        raise ValueError(f"{field} changed while reading")
    try:
        _require_boundary_identity(source.parent, parent_identity, field)
        _assert_safe_path_chain(source, field)
    except ValueError:
        raise ValueError(f"{field} changed while reading") from None
    if not 0 < len(body) <= _MAX_IDENTITY_BYTES:
        raise ValueError(f"{field} is invalid")
    try:
        payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(f"{field} is invalid") from None
    if not isinstance(payload, dict) or canonical_source_provenance(payload) != body:
        raise ValueError(f"{field} must be canonical JSON")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--release-run", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        produce_backup_restore_source_provenance(
            SourceProvenanceConfig(
                candidate=_load_canonical_mapping(arguments.candidate, "candidate"),
                release_run=_load_canonical_mapping(arguments.release_run, "release run"),
                backup_directory=arguments.backup_dir,
                output_path=arguments.output,
            )
        )
    except SourceProvenanceCommittedError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
