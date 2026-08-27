"""Write candidate-bound classroom release receipts and evidence manifests."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Protocol
import uuid

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from classroom_release_probe_contract import probe_command_record  # noqa: E402
from classroom_runtime_attestation import (  # noqa: E402
    _close_windows_handle,
    _create_windows_directory_relative,
    _create_windows_staging_file,
    _file_identity,
    _open_posix_directory_path_no_follow,
    _open_windows_directory_handle,
    _open_windows_directory_relative,
    _open_windows_regular_file_relative,
    _rename_windows_file_relative,
    resolve_fixed_docker,
)
from platform_preflight_contract import (  # noqa: E402
    MAX_CANDIDATE_NETWORK_REPORT_BYTES,
    materialize_candidate_network_phase_command,
    parse_candidate_network_report,
)
from platform_preflight_contract import (
    PHASE_SERVICES as PREFLIGHT_PHASE_SERVICES,
)
from platform_preflight_contract import (
    PHASES as PREFLIGHT_PHASES,
)
from render_platform_compose import validate_image_lock_bindings  # noqa: E402
from verify_classroom_release import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    PROBE_RECIPES,
    RECEIPT_CONTRACTS,
    derive_platform_preflight_receipt_checks,
    derive_probe_checks,
    probe_provenance_error,
    read_runtime_attestation_artifact,
    validate_runtime_attestation,
)

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class GitRunner(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]: ...


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


_DIRECT_RECEIPT_EVIDENCE = frozenset(("source_head", "image_digests"))
_PROBE_CLEANUP_MARGIN_SECONDS = 30


def _candidate(candidate_root: Path) -> dict[str, Any]:
    root = Path(candidate_root)
    lock = validate_image_lock_bindings(
        root / "deploy" / "image-lock.json",
        compose_paths=(
            root / "docker-compose.platform.yml",
            root / "docker-compose.data-plane.yml",
        ),
        require_candidate=True,
    )
    candidate = lock.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("candidate image lock is invalid")
    return json.loads(json.dumps(candidate))


def _release_run(raw: Mapping[str, object]) -> dict[str, str]:
    if set(raw) != {"runId", "environmentId"}:
        raise ValueError("release run identity is invalid")
    values: dict[str, str] = {}
    for name in ("runId", "environmentId"):
        value = raw.get(name)
        if not isinstance(value, str) or _RELEASE_ID.fullmatch(value) is None:
            raise ValueError("release run identity is invalid")
        values[name] = value
    return values


def _valid_observed_at(raw: object) -> bool:
    if not isinstance(raw, str) or _OBSERVED_AT.fullmatch(raw) is None:
        return False
    try:
        datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _validate_pass_result(
    evidence: str,
    *,
    native_exit: object,
    checks: Mapping[str, object],
) -> tuple[str, dict[str, bool]]:
    contract = RECEIPT_CONTRACTS.get(evidence)
    if contract is None:
        raise ValueError("evidence layer is invalid")
    producer, required_checks = contract
    if not isinstance(native_exit, int) or isinstance(native_exit, bool) or native_exit != 0:
        raise ValueError("native exit does not prove passing evidence")
    if set(checks) != set(required_checks) or any(
        checks.get(name) is not True for name in required_checks
    ):
        raise ValueError("evidence checks must be explicit and passing")
    return producer, {name: True for name in required_checks}


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            staged = Path(handle.name)
            handle.write(_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _publish_no_replace(source: Path, target: Path) -> None:
    """Publish one staged regular file without replacing an existing target."""

    os.link(source, target, follow_symlinks=False)


def write_pass_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    release_run: Mapping[str, object],
    evidence: str,
    observed_at: str,
    native_exit: int,
    checks: Mapping[str, object],
) -> dict[str, object]:
    """Write one derived receipt; executable evidence must use a fresh probe."""
    if evidence not in _DIRECT_RECEIPT_EVIDENCE:
        raise ValueError("probe-backed evidence must be derived from an executed probe")
    return _write_pass_receipt_from_candidate(
        output_path,
        candidate=_candidate(candidate_root),
        release_run=release_run,
        evidence=evidence,
        observed_at=observed_at,
        native_exit=native_exit,
        checks=checks,
    )


def _write_pass_receipt_from_candidate(
    output_path: Path,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
    evidence: str,
    observed_at: str,
    native_exit: int,
    checks: Mapping[str, object],
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    bound_run = _release_run(release_run)
    if not _valid_observed_at(observed_at):
        raise ValueError("receipt observedAt is invalid")
    producer, bound_checks = _validate_pass_result(
        evidence,
        native_exit=native_exit,
        checks=checks,
    )
    document: dict[str, object] = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "candidate": json.loads(json.dumps(candidate)),
        "releaseRun": bound_run,
        "evidence": evidence,
        "receipt": {
            "producer": producer,
            "observedAt": observed_at,
            "result": {
                "outcome": "pass",
                "nativeExit": native_exit,
                "checks": bound_checks,
            },
        },
    }
    if provenance is not None:
        document["provenance"] = json.loads(json.dumps(provenance))
    _atomic_write_json(Path(output_path), document)
    return document


def _run_probe(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        check=False,
        timeout=timeout,
    )


def _run_platform_preflight_phase(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_buffer,
        tempfile.TemporaryFile(mode="w+b") as stderr_buffer,
    ):
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=env,
            stdout=stdout_buffer,
            stderr=stderr_buffer,
            check=False,
            timeout=timeout,
        )
        if any(
            os.fstat(buffer.fileno()).st_size > MAX_CANDIDATE_NETWORK_REPORT_BYTES
            for buffer in (stdout_buffer, stderr_buffer)
        ):
            raise subprocess.SubprocessError("platform preflight output is too large")
        stdout_buffer.seek(0)
        stderr_buffer.seek(0)
        return subprocess.CompletedProcess(
            arguments,
            completed.returncode,
            stdout_buffer.read().decode("utf-8", errors="strict"),
            stderr_buffer.read().decode("utf-8", errors="strict"),
        )


def _current_observed_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bundle_artifact(path: Path, *, bundle_root: Path, label: str) -> tuple[Path, str]:
    resolved_root = Path(bundle_root).resolve()
    resolved_path = Path(path).resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside the evidence bundle") from exc
    if not relative.parts:
        raise ValueError(f"{label} is invalid")
    return resolved_path, relative.as_posix()


def _probe_report(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    evidence: str,
) -> tuple[dict[str, object], dict[str, bool]]:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("probe raw report is invalid") from exc
    if not isinstance(document, dict):
        raise ValueError("probe raw report is invalid")
    checks = derive_probe_checks(
        evidence,
        raw_report=body,
        candidate=candidate,
        release_run=release_run,
    )
    return document, checks


def _probe_command(evidence: str, recipe: str) -> list[str]:
    registered = PROBE_RECIPES.get(evidence)
    if registered is None or recipe != registered[0]:
        raise ValueError("probe recipe is invalid")
    return [
        sys.executable,
        str(SCRIPTS_ROOT / "classroom_release_probe.py"),
        evidence,
    ]


def _failure_archive_document(
    *,
    evidence: str,
    recipe: str,
    reason: str,
    native_exit: int | None,
    moved: Mapping[str, str],
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "evidence": evidence,
        "recipe": recipe,
        "reason": reason,
        "nativeExit": native_exit,
        "artifacts": dict(moved),
    }


def _bundle_relative_file(path: Path, *, bundle_root: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(bundle_root)
    except ValueError as exc:
        raise ValueError("failure archive source is outside the evidence bundle") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("failure archive source is invalid")
    return relative


def _open_or_create_windows_directory(
    parent_handle: object,
    name: str,
) -> tuple[object, tuple[int, int]]:
    try:
        return _open_windows_directory_relative(parent_handle, name)
    except FileNotFoundError:
        try:
            return _create_and_reopen_windows_directory(parent_handle, name)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {80, 183}:
                raise
            return _open_windows_directory_relative(parent_handle, name)


def _create_and_reopen_windows_directory(
    parent_handle: object,
    name: str,
) -> tuple[object, tuple[int, int]]:
    created_handle, created_identity = _create_windows_directory_relative(parent_handle, name)
    _close_windows_handle(created_handle)
    opened_handle, opened_identity = _open_windows_directory_relative(parent_handle, name)
    if opened_identity != created_identity:
        _close_windows_handle(opened_handle)
        raise ValueError("failure archive directory identity changed during creation")
    return opened_handle, opened_identity


def _relocate_windows_bundle_file(
    source: Path,
    *,
    bundle_root: Path,
    bundle_handle: object,
    target_handle: object,
    target_name: str,
) -> bool:
    relative = _bundle_relative_file(source, bundle_root=bundle_root)
    directory_handles: list[object] = []
    current = bundle_handle
    source_handle: object | None = None
    try:
        try:
            for component in relative.parts[:-1]:
                current, _identity = _open_windows_directory_relative(current, component)
                directory_handles.append(current)
            source_handle, _identity = _open_windows_regular_file_relative(
                current,
                relative.name,
                share_access=0x00000001 | 0x00000002 | 0x00000004,
                deletable=True,
            )
        except FileNotFoundError:
            return False
        _rename_windows_file_relative(
            source_handle,
            target_handle,
            target_name,
            replace_existing=False,
        )
        return True
    finally:
        if source_handle is not None:
            _close_windows_handle(source_handle)
        for handle in reversed(directory_handles):
            _close_windows_handle(handle)


def _record_probe_failure_windows(
    *,
    root: Path,
    evidence: str,
    recipe: str,
    attempt_id: str,
    reason: str,
    native_exit: int | None,
    artifacts: Mapping[str, Path],
) -> None:
    handles: list[object] = []
    try:
        bundle_handle, _identity = _open_windows_directory_handle(root, deletable=True)
        handles.append(bundle_handle)
        failure_root_handle, _identity = _open_or_create_windows_directory(
            bundle_handle,
            "failures",
        )
        handles.append(failure_root_handle)
        evidence_handle, _identity = _open_or_create_windows_directory(
            failure_root_handle,
            evidence,
        )
        handles.append(evidence_handle)
        failure_handle, _identity = _create_and_reopen_windows_directory(
            evidence_handle,
            attempt_id,
        )
        handles.append(failure_handle)
        moved: dict[str, str] = {}
        for name, source in artifacts.items():
            target_name = f"{name}.json"
            if _relocate_windows_bundle_file(
                Path(source),
                bundle_root=root,
                bundle_handle=bundle_handle,
                target_handle=failure_handle,
                target_name=target_name,
            ):
                moved[name] = f"failures/{evidence}/{attempt_id}/{target_name}"
        failure_file, _identity = _create_windows_staging_file(
            failure_handle,
            "failure.json",
            _json_bytes(
                _failure_archive_document(
                    evidence=evidence,
                    recipe=recipe,
                    reason=reason,
                    native_exit=native_exit,
                    moved=moved,
                )
            ),
        )
        _close_windows_handle(failure_file)
    finally:
        for handle in reversed(handles):
            _close_windows_handle(handle)


def _open_or_create_posix_directory(
    parent_fd: int,
    name: str,
    *,
    exclusive: bool,
) -> int:
    if Path(name).name != name or not name:
        raise ValueError("failure archive boundary is invalid")
    if exclusive:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    else:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_descriptor = os.open(name, flags, dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
        os.close(file_descriptor)
        raise ValueError("failure archive boundary is invalid")
    return file_descriptor


def _rename_posix_between_no_replace(
    source_fd: int,
    source: str,
    target_fd: int,
    target: str,
) -> None:
    if any(Path(name).name != name or not name for name in (source, target)):
        raise ValueError("failure archive entry name is invalid")
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2 is required for safe failure archival")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                source_fd,
                os.fsencode(source),
                target_fd,
                os.fsencode(target),
                1,
            )
            != 0
        ):
            error = ctypes.get_errno()
            raise OSError(error, f"cannot safely archive failure entry: {target}")
        return
    raise OSError(
        errno.ENOSYS,
        "atomic no-replace failure archival is unavailable on this POSIX platform",
    )


def _relocate_posix_bundle_file(
    source: Path,
    *,
    bundle_root: Path,
    bundle_fd: int,
    target_fd: int,
    target_name: str,
) -> bool:
    relative = _bundle_relative_file(source, bundle_root=bundle_root)
    directory_fds: list[int] = []
    current = bundle_fd
    source_file: int | None = None
    try:
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            for component in relative.parts[:-1]:
                current = os.open(component, directory_flags, dir_fd=current)
                directory_fds.append(current)
            source_file = os.open(
                relative.name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
        except FileNotFoundError:
            return False
        source_details = os.fstat(source_file)
        if not stat.S_ISREG(source_details.st_mode):
            raise ValueError("failure archive source is not a regular file")
        source_identity = _file_identity(source_details)
        _rename_posix_between_no_replace(current, relative.name, target_fd, target_name)
        target_details = os.stat(target_name, dir_fd=target_fd, follow_symlinks=False)
        if _file_identity(target_details) != source_identity or not stat.S_ISREG(
            target_details.st_mode
        ):
            raise ValueError("failure archive target identity changed")
        os.fsync(current)
        os.fsync(target_fd)
        return True
    finally:
        if source_file is not None:
            os.close(source_file)
        for file_descriptor in reversed(directory_fds):
            os.close(file_descriptor)


def _write_posix_new_file(directory_fd: int, name: str, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    file_descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        offset = 0
        while offset < len(body):
            written = os.write(file_descriptor, body[offset:])
            if written <= 0:
                raise OSError("failure archive write made no progress")
            offset += written
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    os.fsync(directory_fd)


def _record_probe_failure_posix(
    *,
    root: Path,
    evidence: str,
    recipe: str,
    attempt_id: str,
    reason: str,
    native_exit: int | None,
    artifacts: Mapping[str, Path],
) -> None:
    handles: list[int] = []
    try:
        bundle_fd, _identity = _open_posix_directory_path_no_follow(root)
        handles.append(bundle_fd)
        failure_root_fd = _open_or_create_posix_directory(
            bundle_fd,
            "failures",
            exclusive=False,
        )
        handles.append(failure_root_fd)
        evidence_fd = _open_or_create_posix_directory(
            failure_root_fd,
            evidence,
            exclusive=False,
        )
        handles.append(evidence_fd)
        failure_fd = _open_or_create_posix_directory(
            evidence_fd,
            attempt_id,
            exclusive=True,
        )
        handles.append(failure_fd)
        moved: dict[str, str] = {}
        for name, source in artifacts.items():
            target_name = f"{name}.json"
            if _relocate_posix_bundle_file(
                Path(source),
                bundle_root=root,
                bundle_fd=bundle_fd,
                target_fd=failure_fd,
                target_name=target_name,
            ):
                moved[name] = f"failures/{evidence}/{attempt_id}/{target_name}"
        _write_posix_new_file(
            failure_fd,
            "failure.json",
            _json_bytes(
                _failure_archive_document(
                    evidence=evidence,
                    recipe=recipe,
                    reason=reason,
                    native_exit=native_exit,
                    moved=moved,
                )
            ),
        )
    finally:
        for file_descriptor in reversed(handles):
            os.close(file_descriptor)


def _record_probe_failure(
    *,
    bundle_root: Path,
    evidence: str,
    recipe: str,
    attempt_id: str,
    reason: str,
    native_exit: int | None,
    artifacts: Mapping[str, Path],
) -> Path:
    components = (evidence, attempt_id, *artifacts)
    if any(
        not isinstance(component, str)
        or not component
        or Path(component).name != component
        or component in {".", ".."}
        for component in components
    ):
        raise ValueError("failure archive boundary is invalid")
    root = Path(bundle_root).resolve()
    arguments = {
        "root": root,
        "evidence": evidence,
        "recipe": recipe,
        "attempt_id": attempt_id,
        "reason": reason,
        "native_exit": native_exit,
        "artifacts": artifacts,
    }
    try:
        if os.name == "nt":
            _record_probe_failure_windows(**arguments)
        else:
            _record_probe_failure_posix(**arguments)
    except ValueError as exc:
        if str(exc).startswith("failure archive"):
            raise
        raise ValueError("failure archive boundary is invalid") from exc
    return root / "failures" / evidence / attempt_id


def run_probe_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    evidence: str,
    observed_at: str,
    base_url: str,
    raw_report_path: Path,
    execution_record_path: Path,
    recipe: str,
    working_directory: Path,
    timeout_seconds: int,
    runner: CommandRunner = _run_probe,
) -> dict[str, object]:
    """Execute one probe and publish its receipt only from a fresh bound report."""
    if evidence in _DIRECT_RECEIPT_EVIDENCE or evidence not in RECEIPT_CONTRACTS:
        raise ValueError("probe evidence layer is invalid")
    candidate = _candidate(candidate_root)
    bound_run = _release_run(release_run)
    if not _valid_observed_at(observed_at):
        raise ValueError("receipt observedAt is invalid")
    resolved_output, _output_artifact = _bundle_artifact(
        output_path,
        bundle_root=bundle_root,
        label="receipt output",
    )
    resolved_report, report_artifact = _bundle_artifact(
        raw_report_path,
        bundle_root=bundle_root,
        label="probe raw report",
    )
    resolved_execution, execution_artifact = _bundle_artifact(
        execution_record_path,
        bundle_root=bundle_root,
        label="probe execution record",
    )
    resolved_attestation = Path(bundle_root).resolve() / "runtime" / "runtime-attestation.json"
    attestation_artifact = "runtime/runtime-attestation.json"
    if len({resolved_output, resolved_report, resolved_execution, resolved_attestation}) != 4:
        raise ValueError("probe proof files must use distinct paths")
    if resolved_output.exists() or resolved_report.exists() or resolved_execution.exists():
        raise ValueError("probe proof files must not already exist")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= _PROBE_CLEANUP_MARGIN_SECONDS
    ):
        raise ValueError("probe timeout is invalid")
    attestation_body, attestation_sha256 = read_runtime_attestation_artifact(
        resolved_attestation,
        bundle_root=bundle_root,
    )
    attestation = validate_runtime_attestation(
        resolved_attestation,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_sha256=attestation_sha256,
    )
    base_url = attestation["baseUrl"]
    assert isinstance(base_url, str)
    attestation_proof = {
        "artifact": attestation_artifact,
        "sha256": attestation_sha256,
    }
    arguments = _probe_command(evidence, recipe)
    cwd = Path(working_directory).resolve()
    if not cwd.is_dir():
        raise ValueError("probe working directory is invalid")
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    attempt_id = uuid.uuid4().hex
    staged_report = resolved_report.parent / f".{resolved_report.name}.{attempt_id}.staging"
    environment = os.environ.copy()
    environment.update(
        {
            "YFEISTAI_EVIDENCE_REPORT": str(staged_report),
            "YFEISTAI_CANDIDATE_ROOT": str(Path(candidate_root).resolve()),
            "YFEISTAI_RELEASE_RUN_ID": bound_run["runId"],
            "YFEISTAI_ENVIRONMENT_ID": bound_run["environmentId"],
            "YFEISTAI_EVIDENCE": evidence,
            "YFEISTAI_PROBE_TIMEOUT_SECONDS": str(timeout_seconds - _PROBE_CLEANUP_MARGIN_SECONDS),
            "WEB_BASE_URL": base_url,
        }
    )
    try:
        completed = runner(
            arguments,
            cwd=cwd,
            env=environment,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        _record_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="outer probe deadline expired",
            native_exit=None,
            artifacts={"raw": staged_report},
        )
        raise
    native_exit = completed.returncode
    if not isinstance(native_exit, int) or isinstance(native_exit, bool):
        _record_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe native exit is invalid",
            native_exit=None,
            artifacts={"raw": staged_report},
        )
        raise ValueError("probe native exit is invalid")
    if native_exit != 0:
        _record_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe native exit does not prove passing evidence",
            native_exit=native_exit,
            artifacts={"raw": staged_report},
        )
        raise ValueError(f"probe native exit {native_exit} does not prove passing evidence")
    try:
        raw_body = staged_report.read_bytes()
    except OSError as exc:
        _record_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe raw report is unavailable",
            native_exit=native_exit,
            artifacts={"raw": staged_report},
        )
        raise ValueError("probe raw report is unavailable") from exc
    try:
        _report, checks = _probe_report(
            raw_body,
            candidate=candidate,
            release_run=bound_run,
            evidence=evidence,
        )
        candidate_after = _candidate(candidate_root)
        if candidate_after != candidate:
            raise ValueError("candidate changed while the probe was running")
        try:
            attestation_after, attestation_after_sha256 = read_runtime_attestation_artifact(
                resolved_attestation,
                bundle_root=bundle_root,
            )
        except ValueError as exc:
            raise ValueError(
                "runtime attestation became unavailable while the probe was running"
            ) from exc
        if attestation_after != attestation_body or attestation_after_sha256 != attestation_sha256:
            raise ValueError("runtime attestation changed while the probe was running")
        validate_runtime_attestation(
            resolved_attestation,
            bundle_root=bundle_root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
            expected_sha256=attestation_sha256,
        )
    except ValueError:
        _record_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe raw report or candidate validation failed",
            native_exit=native_exit,
            artifacts={"raw": staged_report},
        )
        raise
    command_record = probe_command_record(evidence)
    raw_sha256 = hashlib.sha256(raw_body).hexdigest()
    execution = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": bound_run,
        "evidence": evidence,
        "recipe": recipe,
        "command": command_record,
        "observedAt": observed_at,
        "baseUrl": base_url,
        "nativeExit": native_exit,
        "rawReportSha256": raw_sha256,
        "runtimeAttestation": attestation_proof,
    }
    try:
        os.replace(staged_report, resolved_report)
        _atomic_write_json(resolved_execution, execution)
        execution_sha256 = hashlib.sha256(resolved_execution.read_bytes()).hexdigest()
        return _write_pass_receipt_from_candidate(
            resolved_output,
            candidate=candidate,
            release_run=bound_run,
            evidence=evidence,
            observed_at=observed_at,
            native_exit=native_exit,
            checks=checks,
            provenance={
                "recipe": recipe,
                "command": command_record,
                "rawReport": {
                    "artifact": report_artifact,
                    "sha256": raw_sha256,
                },
                "execution": {
                    "artifact": execution_artifact,
                    "sha256": execution_sha256,
                },
                "runtimeAttestation": attestation_proof,
            },
        )
    except Exception:
        _record_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe proof publication failed",
            native_exit=native_exit,
            artifacts={
                "raw": resolved_report if resolved_report.exists() else staged_report,
                "execution": resolved_execution,
                "receipt": resolved_output,
            },
        )
        raise


def _run_git(arguments: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"safe.directory={cwd.as_posix()}",
            *arguments,
        ],
        cwd=cwd,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )


def _git_stdout(
    runner: GitRunner,
    arguments: list[str],
    *,
    cwd: Path,
) -> str:
    try:
        result = runner(arguments, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Git probe could not run") from exc
    if (
        not isinstance(result.returncode, int)
        or isinstance(result.returncode, bool)
        or result.returncode != 0
        or not isinstance(result.stdout, str)
    ):
        raise ValueError("Git probe failed")
    return result.stdout.strip()


def _github_repository(remote: str) -> str | None:
    match = _GITHUB_REMOTE.fullmatch(remote)
    return match.group(1) if match is not None else None


def write_source_head_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    release_run: Mapping[str, object],
    source_root: Path,
    observed_at: str,
    git_runner: GitRunner = _run_git,
) -> dict[str, object]:
    """Probe one trusted, clean Git checkout and bind it to the candidate."""
    candidate = _candidate(candidate_root)
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise ValueError("Git source root is unavailable")
    head = _git_stdout(git_runner, ["rev-parse", "HEAD"], cwd=root)
    status = _git_stdout(
        git_runner,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
    )
    origin = _git_stdout(git_runner, ["remote", "get-url", "origin"], cwd=root)
    final_head = _git_stdout(git_runner, ["rev-parse", "HEAD"], cwd=root)
    try:
        final_candidate = _candidate(candidate_root)
    except ValueError as exc:
        raise ValueError("release candidate changed during Git probe") from exc
    if final_candidate != candidate:
        raise ValueError("release candidate changed during Git probe")
    if head != candidate["sourceHead"]:
        raise ValueError("Git HEAD does not match the release candidate")
    if final_head != head:
        raise ValueError("Git HEAD changed during release probe")
    if status:
        raise ValueError("Git worktree is not clean")
    if _github_repository(origin) != candidate["sourceRepository"]:
        raise ValueError("Git origin does not match the release candidate")
    return _write_pass_receipt_from_candidate(
        output_path,
        candidate=candidate,
        release_run=release_run,
        evidence="source_head",
        observed_at=observed_at,
        native_exit=0,
        checks={"headMatches": True, "worktreeClean": True},
    )


def write_image_digest_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    release_run: Mapping[str, object],
    observed_at: str,
) -> dict[str, object]:
    """Revalidate the candidate lock and both Compose files before receipt."""
    candidate = _candidate(candidate_root)
    return _write_pass_receipt_from_candidate(
        output_path,
        candidate=candidate,
        release_run=release_run,
        evidence="image_digests",
        observed_at=observed_at,
        native_exit=0,
        checks={"lockMatches": True, "composeMatches": True},
    )


def write_running_containers_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
) -> dict[str, object]:
    """Derive stable-container evidence from the fixed runtime attestation."""
    resolved_output, _output_artifact = _bundle_artifact(
        output_path,
        bundle_root=bundle_root,
        label="receipt output",
    )
    canonical_output = Path(bundle_root).resolve() / "artifacts" / "running_containers.json"
    if resolved_output != canonical_output:
        raise ValueError("running containers receipt must use its canonical output path")
    candidate = _candidate(candidate_root)
    bound_run = _release_run(release_run)
    resolved_attestation = Path(bundle_root).resolve() / "runtime" / "runtime-attestation.json"
    attestation_body, attestation_sha256 = read_runtime_attestation_artifact(
        resolved_attestation,
        bundle_root=bundle_root,
    )
    attestation = validate_runtime_attestation(
        resolved_attestation,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=attestation_sha256,
    )
    observed_at = attestation["observedAt"]
    assert isinstance(observed_at, str)
    try:
        candidate_after = _candidate(candidate_root)
    except ValueError as exc:
        raise ValueError("candidate changed while container evidence was derived") from exc
    if candidate_after != candidate:
        raise ValueError("candidate changed while container evidence was derived")
    try:
        attestation_after, attestation_after_sha256 = read_runtime_attestation_artifact(
            resolved_attestation,
            bundle_root=bundle_root,
        )
    except ValueError as exc:
        raise ValueError("runtime attestation changed while evidence was derived") from exc
    if attestation_after != attestation_body or attestation_after_sha256 != attestation_sha256:
        raise ValueError("runtime attestation changed while evidence was derived")
    validate_runtime_attestation(
        resolved_attestation,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=attestation_sha256,
    )
    return _write_pass_receipt_from_candidate(
        resolved_output,
        candidate=candidate,
        release_run=bound_run,
        evidence="running_containers",
        observed_at=observed_at,
        native_exit=0,
        checks={"stableContainerSet": True},
        provenance={
            "runtimeAttestation": {
                "artifact": "runtime/runtime-attestation.json",
                "sha256": attestation_sha256,
            }
        },
    )


def write_platform_preflight_receipts(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    runner: CommandRunner = _run_platform_preflight_phase,
    docker_resolver: Callable[[], Path] = resolve_fixed_docker,
) -> dict[str, dict[str, object]]:
    """Run both fixed candidate-network phases and publish their bound receipts."""

    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ValueError("platform preflight timeout is invalid")
    root = Path(bundle_root).resolve()
    candidate_path = Path(candidate_root).resolve()
    proof_path = root / "runtime" / "platform-preflight-attestation.json"
    receipt_paths = {
        evidence: root / "artifacts" / f"{evidence}.json"
        for evidence in ("database_revisions", "service_health")
    }
    targets = (proof_path, *receipt_paths.values())
    if any(path.exists() for path in targets):
        raise ValueError("platform preflight evidence already exists")

    candidate = _candidate(candidate_path)
    bound_run = _release_run(release_run)
    runtime_path = root / "runtime" / "runtime-attestation.json"
    runtime_body, runtime_sha256 = read_runtime_attestation_artifact(
        runtime_path,
        bundle_root=root,
    )
    runtime = validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=runtime_sha256,
    )
    containers = runtime.get("containers")
    if not isinstance(containers, list):
        raise ValueError("platform preflight runtime containers are invalid")
    container_ids = {
        container.get("service"): container.get("containerId")
        for container in containers
        if isinstance(container, dict)
    }
    base_url = runtime.get("baseUrl")
    if not isinstance(base_url, str):
        raise ValueError("platform preflight runtime URL is invalid")

    allowed_environment = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "WINDIR",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_environment
    }
    executions: list[dict[str, object]] = []
    docker = Path(docker_resolver())
    with tempfile.TemporaryDirectory(prefix="yfeistai-preflight-docker-") as config_dir:
        for phase in PREFLIGHT_PHASES:
            service = PREFLIGHT_PHASE_SERVICES[phase]
            container_id = container_ids.get(service)
            if not isinstance(container_id, str):
                raise ValueError("platform preflight container identity is invalid")
            actual_command, logical_command = materialize_candidate_network_phase_command(
                phase,
                container_id,
                docker_executable=docker,
                docker_config=Path(config_dir).resolve(),
            )
            try:
                completed = runner(
                    actual_command,
                    cwd=candidate_path,
                    env=environment,
                    timeout=timeout_seconds,
                )
            except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
                raise ValueError("platform preflight phase could not run") from exc
            if (
                not isinstance(completed.returncode, int)
                or isinstance(completed.returncode, bool)
                or completed.returncode != 0
                or completed.args != actual_command
                or not isinstance(completed.stdout, str)
                or completed.stderr != ""
            ):
                raise ValueError("platform preflight phase did not exit cleanly")
            try:
                stdout_body = completed.stdout.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("platform preflight phase stdout is invalid") from exc
            report = parse_candidate_network_report(stdout_body, expected_phase=phase)
            checks = report["checks"]
            errors = report["errors"]
            assert isinstance(checks, dict) and isinstance(errors, list)
            if errors or any(value is not True for value in checks.values()):
                raise ValueError("platform preflight phase did not prove passing evidence")
            executions.append(
                {
                    "phase": phase,
                    "service": service,
                    "containerId": container_id,
                    "command": logical_command,
                    "nativeExit": 0,
                    "stdout": completed.stdout,
                    "stdoutSha256": hashlib.sha256(stdout_body).hexdigest(),
                }
            )
        if any(Path(config_dir).iterdir()):
            raise ValueError("platform preflight isolated Docker config was modified")

    try:
        candidate_after = _candidate(candidate_path)
        runtime_after, runtime_after_sha256 = read_runtime_attestation_artifact(
            runtime_path,
            bundle_root=root,
        )
    except ValueError as exc:
        raise ValueError("platform preflight release binding changed") from exc
    if (
        candidate_after != candidate
        or runtime_after != runtime_body
        or runtime_after_sha256 != runtime_sha256
    ):
        raise ValueError("platform preflight release binding changed")
    validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_sha256=runtime_sha256,
    )

    observed_at = _current_observed_at()
    if not _valid_observed_at(observed_at):
        raise ValueError("platform preflight observedAt is invalid")
    proof = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": bound_run,
        "observedAt": observed_at,
        "baseUrl": base_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        },
        "executions": executions,
    }
    attempt_id = uuid.uuid4().hex
    staged_proof = proof_path.with_name(f".{proof_path.name}.{attempt_id}.staging")
    staged_receipts = {
        evidence: path.with_name(f".{path.name}.{attempt_id}.staging")
        for evidence, path in receipt_paths.items()
    }
    staged_paths = {
        "proof": staged_proof,
        **staged_receipts,
    }
    published_paths: dict[str, Path] = {}
    try:
        _atomic_write_json(staged_proof, proof)
        proof_body = staged_proof.read_bytes()
        proof_sha256 = hashlib.sha256(proof_body).hexdigest()
        derived_checks, replayed_observed_at = derive_platform_preflight_receipt_checks(
            proof_body,
            bundle_root=root,
            candidate_root=candidate_path,
            candidate=candidate,
            release_run=bound_run,
        )
        if replayed_observed_at != observed_at:
            raise ValueError("platform preflight replay timestamp changed")
        provenance = {
            "platformPreflightAttestation": {
                "artifact": "runtime/platform-preflight-attestation.json",
                "sha256": proof_sha256,
            }
        }
        documents = {
            "database_revisions": _write_pass_receipt_from_candidate(
                staged_receipts["database_revisions"],
                candidate=candidate,
                release_run=bound_run,
                evidence="database_revisions",
                observed_at=observed_at,
                native_exit=0,
                checks=derived_checks["database_revisions"],
                provenance=provenance,
            ),
            "service_health": _write_pass_receipt_from_candidate(
                staged_receipts["service_health"],
                candidate=candidate,
                release_run=bound_run,
                evidence="service_health",
                observed_at=observed_at,
                native_exit=0,
                checks=derived_checks["service_health"],
                provenance=provenance,
            ),
        }
        candidate_before_publish = _candidate(candidate_path)
        runtime_before_publish, sha_before_publish = read_runtime_attestation_artifact(
            runtime_path,
            bundle_root=root,
        )
        if (
            candidate_before_publish != candidate
            or runtime_before_publish != runtime_body
            or sha_before_publish != runtime_sha256
            or any(path.exists() for path in targets)
        ):
            raise ValueError("platform preflight release binding changed before publication")
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_paths["database_revisions"].parent.mkdir(parents=True, exist_ok=True)
        publication_order = (
            (
                "database_revisions",
                staged_receipts["database_revisions"],
                receipt_paths["database_revisions"],
            ),
            (
                "service_health",
                staged_receipts["service_health"],
                receipt_paths["service_health"],
            ),
            ("proof", staged_proof, proof_path),
        )
        for name, staged, target in publication_order:
            _publish_no_replace(staged, target)
            published_paths[f"published-{name.replace('_', '-')}"] = target
            if target.read_bytes() != staged.read_bytes():
                raise ValueError("platform preflight published evidence changed")
            staged.unlink()
        return documents
    except Exception:
        _record_probe_failure(
            bundle_root=root,
            evidence="platform-preflight",
            recipe="candidate-network-phases",
            attempt_id=attempt_id,
            reason="platform preflight proof publication failed",
            native_exit=0,
            artifacts={
                **staged_paths,
                **published_paths,
            },
        )
        raise


def _validated_receipt(
    path: Path,
    *,
    evidence: str,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    bundle_root: Path,
    candidate_root: Path,
) -> tuple[dict[str, object], bytes, str]:
    try:
        body = path.read_bytes()
        document = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt is unavailable or invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") != ARTIFACT_SCHEMA_VERSION
        or document.get("candidate") != candidate
        or document.get("releaseRun") != release_run
        or document.get("evidence") != evidence
    ):
        raise ValueError("receipt envelope does not match the evidence bundle")
    receipt = document.get("receipt")
    contract = RECEIPT_CONTRACTS.get(evidence)
    if not isinstance(receipt, dict) or contract is None:
        raise ValueError("receipt is invalid")
    producer, required_checks = contract
    result = receipt.get("result")
    if (
        set(receipt) != {"producer", "observedAt", "result"}
        or receipt.get("producer") != producer
        or not _valid_observed_at(receipt.get("observedAt"))
        or not isinstance(result, dict)
        or set(result) != {"outcome", "nativeExit", "checks"}
        or result.get("outcome") != "pass"
    ):
        raise ValueError("receipt is invalid")
    native_exit = result.get("nativeExit")
    checks = result.get("checks")
    if not isinstance(checks, dict):
        raise ValueError("receipt is invalid")
    _validate_pass_result(evidence, native_exit=native_exit, checks=checks)
    provenance_error = probe_provenance_error(
        document,
        evidence=evidence,
        candidate=candidate,
        release_run=release_run,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
    )
    if provenance_error is not None:
        raise ValueError(provenance_error)
    return document, body, producer


def assemble_manifest(
    output_path: Path,
    *,
    candidate_root: Path,
    release_run: Mapping[str, object],
    receipt_paths: Mapping[str, Path],
) -> dict[str, object]:
    """Validate receipt bytes and publish their schema-v3 manifest last."""
    candidate = _candidate(candidate_root)
    bound_run = _release_run(release_run)
    target = Path(output_path)
    resolved_target = target.resolve()
    bundle_root = resolved_target.parent
    evidence_entries: dict[str, object] = {}
    for evidence, raw_path in receipt_paths.items():
        receipt_path = Path(raw_path).resolve()
        if receipt_path == resolved_target:
            raise ValueError("receipt path must not be the manifest output path")
        try:
            relative_path = receipt_path.relative_to(bundle_root)
        except ValueError as exc:
            raise ValueError("receipt is outside the evidence bundle") from exc
        _document, body, producer = _validated_receipt(
            receipt_path,
            evidence=evidence,
            candidate=candidate,
            release_run=bound_run,
            bundle_root=bundle_root,
            candidate_root=candidate_root,
        )
        evidence_entries[evidence] = {
            "status": "pass",
            "detail": f"{evidence} verified by {producer}",
            "artifact": relative_path.as_posix(),
            "artifactSha256": hashlib.sha256(body).hexdigest(),
        }
    manifest: dict[str, object] = {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "candidate": candidate,
        "releaseRun": bound_run,
        "evidence": evidence_entries,
    }
    _atomic_write_json(target, manifest)
    return manifest


def _add_common_receipt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--observed-at", required=True)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    source_head = commands.add_parser("source-head")
    _add_common_receipt_arguments(source_head)
    source_head.add_argument("--source-root", type=Path, required=True)

    image_digests = commands.add_parser("image-digests")
    _add_common_receipt_arguments(image_digests)

    running_containers = commands.add_parser("running-containers")
    running_containers.add_argument("--output", type=Path, required=True)
    running_containers.add_argument("--candidate-root", type=Path, required=True)
    running_containers.add_argument("--bundle-root", type=Path, required=True)
    running_containers.add_argument("--run-id", required=True)
    running_containers.add_argument("--environment-id", required=True)

    platform_preflight = commands.add_parser("platform-preflight")
    platform_preflight.add_argument("--candidate-root", type=Path, required=True)
    platform_preflight.add_argument("--bundle-root", type=Path, required=True)
    platform_preflight.add_argument("--run-id", required=True)
    platform_preflight.add_argument("--environment-id", required=True)
    platform_preflight.add_argument("--timeout-seconds", type=int, required=True)

    produce = commands.add_parser("produce")
    _add_common_receipt_arguments(produce)
    produce.add_argument("--evidence", choices=tuple(sorted(PROBE_RECIPES)), required=True)
    produce.add_argument("--bundle-root", type=Path, required=True)
    produce.add_argument("--working-directory", type=Path, required=True)
    produce.add_argument("--timeout-seconds", type=int, required=True)
    produce.add_argument("--base-url", required=True)

    assemble = commands.add_parser("assemble")
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("--candidate-root", type=Path, required=True)
    assemble.add_argument("--run-id", required=True)
    assemble.add_argument("--environment-id", required=True)
    assemble.add_argument("--receipt", action="append", required=True)
    return parser.parse_args(argv)


def _receipt_paths(values: Sequence[str]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for value in values:
        evidence, separator, raw_path = value.partition("=")
        if not separator or evidence not in RECEIPT_CONTRACTS or evidence in paths or not raw_path:
            raise ValueError("--receipt must contain unique evidence=path values")
        paths[evidence] = Path(raw_path)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    release_run = {
        "runId": args.run_id,
        "environmentId": args.environment_id,
    }
    if args.command == "source-head":
        write_source_head_receipt(
            args.output,
            candidate_root=args.candidate_root,
            release_run=release_run,
            source_root=args.source_root,
            observed_at=args.observed_at,
        )
    elif args.command == "image-digests":
        write_image_digest_receipt(
            args.output,
            candidate_root=args.candidate_root,
            release_run=release_run,
            observed_at=args.observed_at,
        )
    elif args.command == "running-containers":
        write_running_containers_receipt(
            args.output,
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
        )
    elif args.command == "platform-preflight":
        write_platform_preflight_receipts(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            timeout_seconds=args.timeout_seconds,
        )
        print(args.bundle_root / "runtime" / "platform-preflight-attestation.json")
        return 0
    elif args.command == "produce":
        recipe, _expected_count = PROBE_RECIPES[args.evidence]
        run_probe_receipt(
            args.output,
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            evidence=args.evidence,
            observed_at=args.observed_at,
            base_url=args.base_url,
            raw_report_path=args.bundle_root / "raw" / f"{args.evidence}.json",
            execution_record_path=args.bundle_root / "executions" / f"{args.evidence}.json",
            recipe=recipe,
            working_directory=args.working_directory,
            timeout_seconds=args.timeout_seconds,
        )
    else:
        assemble_manifest(
            args.output,
            candidate_root=args.candidate_root,
            release_run=release_run,
            receipt_paths=_receipt_paths(args.receipt),
        )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
