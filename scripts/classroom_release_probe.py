#!/usr/bin/env python3
"""Run one fixed Playwright classroom-release evidence recipe."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from classroom_release_probe_contract import (  # noqa: E402
    EVIDENCE_GREP,
    probe_command_descriptor,
)

TIMEOUT_EXIT = 124
TERMINATION_WAIT_SECONDS = 5

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LIVE_ENVIRONMENT = re.compile(r"^YFEISTAI_LIVE_[A-Z0-9_]+$")
_OS_ENVIRONMENT = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)
_FIXED_CHILD_ENVIRONMENT = (
    "WEB_BASE_URL",
    "YFEISTAI_CANDIDATE_ROOT",
    "YFEISTAI_RELEASE_RUN_ID",
    "YFEISTAI_ENVIRONMENT_ID",
    "YFEISTAI_EVIDENCE",
)
WINDOWS_RUNTIME_ROOTS = (Path("C:/Program Files/nodejs"),)
POSIX_RUNTIME_ROOTS = (Path("/usr/local"), Path("/usr"))


def _terminate_owned_process_tree(
    process: object,
    *,
    platform: str,
    environment: Mapping[str, str] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    kill_process_group: Callable[[int, int], None] | None = None,
) -> None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("owned process id is invalid")
    if platform == "win32":
        host_environment = os.environ if environment is None else environment
        system_root = Path(
            host_environment.get("SystemRoot", host_environment.get("WINDIR", "C:/Windows"))
        )
        taskkill = system_root / "System32" / "taskkill.exe"
        if not taskkill.is_absolute() or not taskkill.is_file() or taskkill.is_symlink():
            raise RuntimeError("trusted Windows process-tree helper is unavailable")
        command_runner(
            [str(taskkill), "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TERMINATION_WAIT_SECONDS,
        )
    else:
        selected_killpg = kill_process_group or getattr(os, "killpg")
        selected_killpg(pid, signal.SIGTERM)
    try:
        process.wait(timeout=TERMINATION_WAIT_SECONDS)  # type: ignore[attr-defined]
    except subprocess.TimeoutExpired:
        if platform == "win32":
            command_runner(
                [str(taskkill), "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TERMINATION_WAIT_SECONDS,
            )
        else:
            selected_killpg(pid, signal.SIGKILL)
        process.wait(timeout=TERMINATION_WAIT_SECONDS)  # type: ignore[attr-defined]


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout: int,
    check: bool,
    timeout_seconds: int,
    popen_factory: Callable[..., object] = subprocess.Popen,
    tree_terminator: Callable[..., None] = _terminate_owned_process_tree,
    platform: str = sys.platform,
) -> subprocess.CompletedProcess[bytes]:
    options: dict[str, object] = {"cwd": cwd, "env": env, "stdout": stdout}
    if platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = popen_factory(arguments, **options)
    try:
        captured, _stderr = process.communicate(timeout=timeout_seconds)  # type: ignore[attr-defined]
        returncode = process.returncode  # type: ignore[attr-defined]
    except subprocess.TimeoutExpired:
        tree_terminator(process, platform=platform)
        captured, _stderr = process.communicate()  # type: ignore[attr-defined]
        returncode = TIMEOUT_EXIT
    if not isinstance(captured, bytes):
        captured = b""
    completed = subprocess.CompletedProcess(arguments, returncode, stdout=captured)
    if check and completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, arguments, output=captured)
    return completed


def _required_environment(environ: Mapping[str, str], evidence: str) -> dict[str, str]:
    required = {
        name: environ.get(name, "")
        for name in (
            "YFEISTAI_EVIDENCE_REPORT",
            "YFEISTAI_CANDIDATE_ROOT",
            "YFEISTAI_RELEASE_RUN_ID",
            "YFEISTAI_ENVIRONMENT_ID",
            "YFEISTAI_EVIDENCE",
            "YFEISTAI_PROBE_TIMEOUT_SECONDS",
            "WEB_BASE_URL",
        )
    }
    if any(not value for value in required.values()):
        raise ValueError("probe environment is incomplete")
    report = Path(required["YFEISTAI_EVIDENCE_REPORT"])
    candidate_root = Path(required["YFEISTAI_CANDIDATE_ROOT"])
    if not report.is_absolute() or not candidate_root.is_absolute() or not candidate_root.is_dir():
        raise ValueError("probe filesystem boundary is invalid")
    if required["YFEISTAI_EVIDENCE"] != evidence:
        raise ValueError("probe evidence identity does not match")
    if any(
        _IDENTITY.fullmatch(required[name]) is None
        for name in ("YFEISTAI_RELEASE_RUN_ID", "YFEISTAI_ENVIRONMENT_ID")
    ):
        raise ValueError("probe release identity is invalid")
    base_url = urlsplit(required["WEB_BASE_URL"])
    if base_url.scheme not in {"http", "https"} or not base_url.netloc:
        raise ValueError("probe web base URL is invalid")
    return required


def _trusted_runtime_file(path: Path, *, root: Path) -> bool:
    if not root.is_absolute() or not path.is_absolute():
        return False
    try:
        relative = path.relative_to(root)
        resolved_root = root.resolve(strict=True)
        resolved_root.relative_to(root.resolve(strict=True))
        cursor = root
        for index, part in enumerate(relative.parts):
            cursor /= part
            resolved = cursor.resolve(strict=True)
            resolved.relative_to(resolved_root)
            if index < len(relative.parts) - 1 and not resolved.is_dir():
                return False
        resolved_path = path.resolve(strict=True)
    except (OSError, ValueError):
        return False
    return resolved_path.is_file()


def resolve_fixed_node_runtime(
    *,
    platform: str = sys.platform,
    trusted_roots: Sequence[Path] | None = None,
) -> tuple[str, Path]:
    windows = platform == "win32"
    roots = (
        tuple(trusted_roots)
        if trusted_roots is not None
        else (WINDOWS_RUNTIME_ROOTS if windows else POSIX_RUNTIME_ROOTS)
    )
    for root in roots:
        fixed_root = Path(root)
        runtime = fixed_root if windows else fixed_root / "bin"
        npm = runtime / ("npm.cmd" if windows else "npm")
        node = runtime / ("node.exe" if windows else "node")
        if _trusted_runtime_file(npm, root=fixed_root) and _trusted_runtime_file(
            node, root=fixed_root
        ):
            return str(npm), runtime
    raise ValueError("trusted Node.js runtime is unavailable")


def _child_environment(environ: Mapping[str, str], *, runtime: Path) -> dict[str, str]:
    by_upper = {name.upper(): value for name, value in environ.items()}
    child = {
        name: by_upper[name] for name in _OS_ENVIRONMENT if name in by_upper and by_upper[name]
    }
    child.update({name: environ[name] for name in _FIXED_CHILD_ENVIRONMENT})
    child.update(
        {
            name: value
            for name, value in environ.items()
            if _LIVE_ENVIRONMENT.fullmatch(name) is not None
        }
    )
    child["PATH"] = str(runtime)
    return child


def _command(evidence: str, npm: str) -> list[str]:
    inner_argv = probe_command_descriptor(evidence)["innerNpmArgv"]
    if not isinstance(inner_argv, list) or not all(isinstance(value, str) for value in inner_argv):
        raise ValueError("probe command descriptor is invalid")
    return [npm, *inner_argv]


def _atomic_write(path: Path, body: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    published = False
    try:
        with temporary.open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        published = True
    finally:
        if not published:
            temporary.unlink(missing_ok=True)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", choices=tuple(EVIDENCE_GREP))
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = _run,
    runtime_resolver: Callable[[], tuple[str, Path]] = resolve_fixed_node_runtime,
) -> int:
    args = _parse_args(argv)
    environment = dict(os.environ if environ is None else environ)
    required = _required_environment(environment, args.evidence)
    npm, runtime = runtime_resolver()
    try:
        timeout_seconds = int(required["YFEISTAI_PROBE_TIMEOUT_SECONDS"])
    except ValueError as exc:
        raise ValueError("probe timeout is invalid") from exc
    if timeout_seconds <= 0:
        raise ValueError("probe timeout is invalid")
    completed = runner(
        _command(args.evidence, npm),
        cwd=Path.cwd().resolve(),
        env=_child_environment(environment, runtime=runtime),
        stdout=subprocess.PIPE,
        check=False,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(completed.stdout, bytes):
        raise ValueError("Playwright stdout is unavailable")
    if not isinstance(completed.returncode, int) or isinstance(completed.returncode, bool):
        raise ValueError("Playwright native exit is invalid")
    _atomic_write(Path(required["YFEISTAI_EVIDENCE_REPORT"]), completed.stdout)
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
