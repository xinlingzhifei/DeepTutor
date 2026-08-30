"""Run the dedicated-outage producer and proof-last publish its execution receipt."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import BinaryIO, Protocol

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from openmaic_dedicated_outage_probe import (  # noqa: E402
    DockerDedicatedPlaneController,
    OpenMAICDedicatedOutageProbeError,
    OutageProbeConfig,
    _atomic_publish,
    _existing_regular_body,
    _load_config,
    _publish_no_replace,
)
from openmaic_smoke_contract import (  # noqa: E402
    canonical_openmaic_dedicated_outage_attestation,
    openmaic_dedicated_outage_command_record,
    parse_openmaic_dedicated_outage_attestation,
)


class OpenMAICDedicatedOutageRunnerError(RuntimeError):
    """Stable, secret-free failure raised by the outer outage runner."""


class _CompletedProcess(Protocol):
    returncode: int
    stdout: bytes
    stderr: bytes


class _RunningProcess(Protocol):
    pid: int
    returncode: int | None

    def communicate(self, *, timeout: float) -> tuple[bytes, bytes]: ...

    def send_signal(self, selected_signal: int) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _ChildTerminalState:
    returncode: int
    stdout: bytes
    stderr: bytes
    operation_timed_out: bool = False


Runner = Callable[..., _CompletedProcess]
ProcessFactory = Callable[..., _RunningProcess]
ReceiptBuilder = Callable[[bytes, dict[str, object]], bytes]
Publisher = Callable[[Path, bytes], None]

_OUTAGE_CLEANUP_GRACE_SECONDS = 120


def _interrupt_child(process: _RunningProcess) -> None:
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
        return
    os.killpg(process.pid, signal.SIGINT)


def _hard_kill_child_tree(process: _RunningProcess) -> None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        process.kill()
        return
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
        else:
            if completed.returncode != 0:
                process.kill()
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()


def _force_stop_and_reconcile(
    process: _RunningProcess,
    *,
    cleanup_grace_seconds: float,
    reconcile_after_forced_stop: Callable[[], None],
) -> None:
    _hard_kill_child_tree(process)
    try:
        process.communicate(timeout=cleanup_grace_seconds)
    except (OSError, subprocess.SubprocessError) as exc:
        if type(process.returncode) is not int:
            raise OpenMAICDedicatedOutageRunnerError("outage_child_reap_failed") from exc
    if type(process.returncode) is not int:
        raise OpenMAICDedicatedOutageRunnerError("outage_child_reap_failed")
    try:
        reconcile_after_forced_stop()
    except Exception as exc:
        raise OpenMAICDedicatedOutageRunnerError("outage_parent_reconciliation_failed") from exc


def _capture_child_terminal_state(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    operation_timeout_seconds: float,
    cleanup_grace_seconds: float,
    process_factory: ProcessFactory = subprocess.Popen,
    interrupt_child: Callable[[_RunningProcess], None] = _interrupt_child,
    reconcile_after_forced_stop: Callable[[], None],
) -> _ChildTerminalState:
    """Capture child terminal state without interrupting its cleanup window."""

    if operation_timeout_seconds <= 0 or cleanup_grace_seconds <= 0:
        raise OpenMAICDedicatedOutageRunnerError("outage_child_timeout_invalid")
    options: dict[str, object] = {
        "cwd": Path(cwd),
        "env": dict(environment),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = process_factory(list(command), **options)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise OpenMAICDedicatedOutageRunnerError("outage_child_failed") from exc
    try:
        stdout, stderr = process.communicate(timeout=operation_timeout_seconds)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        try:
            interrupt_child(process)
            stdout, stderr = process.communicate(timeout=cleanup_grace_seconds)
            returncode = process.returncode
        except subprocess.TimeoutExpired as exc:
            _force_stop_and_reconcile(
                process,
                cleanup_grace_seconds=cleanup_grace_seconds,
                reconcile_after_forced_stop=reconcile_after_forced_stop,
            )
            raise OpenMAICDedicatedOutageRunnerError("outage_child_cleanup_timeout") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            _force_stop_and_reconcile(
                process,
                cleanup_grace_seconds=cleanup_grace_seconds,
                reconcile_after_forced_stop=reconcile_after_forced_stop,
            )
            raise OpenMAICDedicatedOutageRunnerError("outage_child_interrupt_failed") from exc
        if type(returncode) is not int:
            raise OpenMAICDedicatedOutageRunnerError("outage_child_failed")
        return _ChildTerminalState(
            returncode=returncode,
            stdout=bytes(stdout or b""),
            stderr=bytes(stderr or b""),
            operation_timed_out=True,
        )
    except KeyboardInterrupt:
        try:
            interrupt_child(process)
            process.communicate(timeout=cleanup_grace_seconds)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            _force_stop_and_reconcile(
                process,
                cleanup_grace_seconds=cleanup_grace_seconds,
                reconcile_after_forced_stop=reconcile_after_forced_stop,
            )
        except (OSError, subprocess.SubprocessError):
            _force_stop_and_reconcile(
                process,
                cleanup_grace_seconds=cleanup_grace_seconds,
                reconcile_after_forced_stop=reconcile_after_forced_stop,
            )
        else:
            try:
                reconcile_after_forced_stop()
            except Exception as exc:
                raise OpenMAICDedicatedOutageRunnerError(
                    "outage_parent_reconciliation_failed"
                ) from exc
        raise
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise OpenMAICDedicatedOutageRunnerError("outage_child_failed") from exc
    if type(returncode) is not int:
        raise OpenMAICDedicatedOutageRunnerError("outage_child_failed")
    return _ChildTerminalState(
        returncode=returncode,
        stdout=bytes(stdout or b""),
        stderr=bytes(stderr or b""),
    )


def _execute_child_and_publish(
    command: Sequence[str],
    output_path: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    runner: Runner | None = subprocess.run,
    receipt_builder: ReceiptBuilder,
    publisher: Publisher = _atomic_publish,
    stdout: BinaryIO | None = None,
    operation_timeout_seconds: float | None = None,
    cleanup_grace_seconds: float = _OUTAGE_CLEANUP_GRACE_SECONDS,
    reconcile_after_forced_stop: Callable[[], None] | None = None,
) -> int:
    """Capture a child terminal state, then emit and proof-last publish its receipt."""

    try:
        if runner is None:
            if operation_timeout_seconds is None:
                raise OpenMAICDedicatedOutageRunnerError("outage_child_timeout_invalid")
            if reconcile_after_forced_stop is None:
                raise OpenMAICDedicatedOutageRunnerError("outage_reconciler_unavailable")
            completed = _capture_child_terminal_state(
                command,
                cwd=cwd,
                environment=environment,
                operation_timeout_seconds=operation_timeout_seconds,
                cleanup_grace_seconds=cleanup_grace_seconds,
                reconcile_after_forced_stop=reconcile_after_forced_stop,
            )
        else:
            completed = runner(
                list(command),
                cwd=Path(cwd),
                env=dict(environment),
                check=False,
                capture_output=True,
            )
        child_stdout = bytes(completed.stdout or b"")
        child_stderr = bytes(completed.stderr or b"")
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise OpenMAICDedicatedOutageRunnerError("outage_child_failed") from exc
    child_failed = (
        type(completed.returncode) is not int
        or completed.returncode != 0
        or getattr(completed, "operation_timed_out", False) is not False
    )
    if child_failed and reconcile_after_forced_stop is not None:
        try:
            reconcile_after_forced_stop()
        except Exception as exc:
            raise OpenMAICDedicatedOutageRunnerError("outage_parent_reconciliation_failed") from exc
    if (
        child_failed
        or not child_stdout
        or len(child_stdout) > 64 * 1024
        or len(child_stderr) > 64 * 1024
    ):
        raise OpenMAICDedicatedOutageRunnerError("outage_child_failed")
    execution = {
        "nativeExit": completed.returncode,
        "stdoutSha256": hashlib.sha256(child_stdout).hexdigest(),
        "stderrSha256": hashlib.sha256(child_stderr).hexdigest(),
    }
    try:
        receipt = receipt_builder(child_stdout, execution)
    except Exception as exc:
        raise OpenMAICDedicatedOutageRunnerError("outage_receipt_invalid") from exc
    if not isinstance(receipt, bytes) or not receipt or len(receipt) > 64 * 1024:
        raise OpenMAICDedicatedOutageRunnerError("outage_receipt_invalid")
    destination = stdout or sys.stdout.buffer
    try:
        written = destination.write(receipt)
        if written is not None and written != len(receipt):
            raise OSError("short stdout write")
        destination.flush()
    except OSError as exc:
        raise OpenMAICDedicatedOutageRunnerError("outage_receipt_output_failed") from exc
    try:
        publisher(Path(output_path), receipt)
    except Exception as exc:
        raise OpenMAICDedicatedOutageRunnerError("outage_receipt_publish_failed") from exc
    return 0


def _archive_observer_anchor(config: OutageProbeConfig) -> None:
    body = _existing_regular_body(config.observer_attestation_path)
    if hashlib.sha256(body).hexdigest() != config.observer_attestation_sha256:
        raise OpenMAICDedicatedOutageRunnerError("observer_trust_anchor_invalid")
    target = config.candidate_root / "runtime" / "openmaic-shared-ingress-observer-attestation.json"
    _publish_no_replace(
        target,
        body,
        allow_identical=True,
        existing_error="observer_attestation_already_exists",
    )


def _reconcile_dedicated_plane(config: OutageProbeConfig) -> None:
    controller = DockerDedicatedPlaneController(config)
    document = controller._inspect()
    identity = controller._identity(document)
    running, _health = controller._running(document)
    if not running:
        controller.start(identity)
    controller.wait_ready(identity)


def _build_receipt(
    config: OutageProbeConfig,
    child_stdout: bytes,
    execution: Mapping[str, object],
) -> bytes:
    try:
        inner = json.loads(child_stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OpenMAICDedicatedOutageRunnerError("outage_receipt_invalid") from exc
    if (
        not isinstance(inner, dict)
        or "execution" in inner
        or canonical_openmaic_dedicated_outage_attestation(inner) != child_stdout
    ):
        raise OpenMAICDedicatedOutageRunnerError("outage_receipt_invalid")
    marker_body = _existing_regular_body(config.attempt_marker_path)
    report = dict(inner)
    report["execution"] = {
        "command": openmaic_dedicated_outage_command_record(),
        **dict(execution),
    }
    body = canonical_openmaic_dedicated_outage_attestation(report)
    parse_openmaic_dedicated_outage_attestation(
        body,
        candidate=config.candidate,
        release_run=config.release_run,
        expected_base_url=config.base_url,
        expected_runtime_attestation_sha256=config.runtime_attestation_sha256,
        expected_observer_attestation_sha256=config.observer_attestation_sha256,
        expected_observer_id=config.observer_id,
        expected_observer_origin=config.observer_origin,
        expected_shared_ingress_control_origin=config.shared_ingress_control_origin,
        expected_tenant_id=config.dedicated_tenant_id,
        attempt_marker_body=marker_body,
        expected_docker_host_identity_sha256=config.docker_host_identity_sha256,
    )
    if config.admin_token.get_secret_value().encode("utf-8") in body:
        raise OpenMAICDedicatedOutageRunnerError("outage_receipt_contains_secret")
    _archive_observer_anchor(config)
    return body


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("first-release",))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        config = _load_config(os.environ, cwd=Path.cwd())
        command = [
            sys.executable,
            str(SCRIPTS_ROOT / "openmaic_dedicated_outage_probe.py"),
            "--profile",
            args.profile,
        ]
        return _execute_child_and_publish(
            command,
            config.output_path,
            cwd=config.candidate_root,
            environment=os.environ,
            receipt_builder=lambda child_stdout, execution: _build_receipt(
                config,
                child_stdout,
                execution,
            ),
            runner=None,
            operation_timeout_seconds=config.timeout_seconds,
            cleanup_grace_seconds=_OUTAGE_CLEANUP_GRACE_SECONDS,
            reconcile_after_forced_stop=lambda: _reconcile_dedicated_plane(config),
        )
    except (
        OpenMAICDedicatedOutageProbeError,
        OpenMAICDedicatedOutageRunnerError,
        OSError,
        ValueError,
    ):
        sys.stderr.write("openmaic_dedicated_outage_runner_failed\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("openmaic_dedicated_outage_runner_interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["OpenMAICDedicatedOutageRunnerError", "main"]
