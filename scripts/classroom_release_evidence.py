"""Write candidate-bound classroom release receipts and evidence manifests."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Protocol
import uuid

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from classroom_release_probe_contract import probe_command_record  # noqa: E402
from render_platform_compose import validate_image_lock_bindings  # noqa: E402
from verify_classroom_release import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    PROBE_RECIPES,
    RECEIPT_CONTRACTS,
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
    failure_dir = Path(bundle_root).resolve() / "failures" / evidence / attempt_id
    failure_dir.mkdir(parents=True, exist_ok=False)
    moved: dict[str, str] = {}
    for name, source in artifacts.items():
        path = Path(source)
        if not path.exists():
            continue
        target = failure_dir / f"{name}.json"
        os.replace(path, target)
        moved[name] = target.relative_to(Path(bundle_root).resolve()).as_posix()
    _atomic_write_json(
        failure_dir / "failure.json",
        {
            "schemaVersion": 1,
            "evidence": evidence,
            "recipe": recipe,
            "reason": reason,
            "nativeExit": native_exit,
            "artifacts": moved,
        },
    )
    return failure_dir


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
