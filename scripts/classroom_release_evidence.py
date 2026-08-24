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

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from render_platform_compose import validate_image_lock_bindings  # noqa: E402
from verify_classroom_release import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    RECEIPT_CONTRACTS,
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
    """Write one passing receipt only from explicit, candidate-bound facts."""
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
    _atomic_write_json(Path(output_path), document)
    return document


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
