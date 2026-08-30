from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = PROJECT_ROOT / "scripts" / "openmaic_dedicated_outage_runner.py"


def _module():
    assert RUNNER_PATH.is_file(), "dedicated outage outer runner is missing"
    spec = importlib.util.spec_from_file_location(
        "openmaic_dedicated_outage_runner_under_test",
        RUNNER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


class _Output:
    def __init__(self, events: list[str], *, fail_at: str | None = None) -> None:
        self._events = events
        self._fail_at = fail_at
        self.body = b""

    def write(self, body: bytes) -> int:
        self._events.append("stdout-write")
        if self._fail_at == "write":
            raise OSError("simulated stdout write failure")
        self.body += body
        return len(body)

    def flush(self) -> None:
        self._events.append("stdout-flush")
        if self._fail_at == "flush":
            raise OSError("simulated stdout flush failure")


def test_outer_runner_captures_child_exit_and_publishes_receipt_after_stdout_flush(
    tmp_path: Path,
) -> None:
    module = _module()
    events: list[str] = []
    child_stdout = b'{"producer":"openmaic-dedicated-outage"}\n'
    child_stderr = b""
    output = _Output(events)
    target = tmp_path / "runtime" / "openmaic-dedicated-outage-attestation.json"
    target.parent.mkdir()

    def runner(command, **kwargs):
        events.append("child-exit")
        assert command[-2:] == ["--profile", "first-release"]
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        return SimpleNamespace(returncode=0, stdout=child_stdout, stderr=child_stderr)

    def receipt_builder(stdout: bytes, execution: dict[str, object]) -> bytes:
        events.append("receipt-built")
        assert stdout == child_stdout
        assert execution == {
            "nativeExit": 0,
            "stdoutSha256": hashlib.sha256(child_stdout).hexdigest(),
            "stderrSha256": hashlib.sha256(child_stderr).hexdigest(),
        }
        return b'{"execution":{"nativeExit":0}}\n'

    def publisher(path: Path, body: bytes) -> None:
        events.append("published")
        assert path == target
        path.write_bytes(body)

    result = module._execute_child_and_publish(
        ["python", "probe.py", "--profile", "first-release"],
        target,
        cwd=tmp_path,
        environment={"PATH": "trusted"},
        runner=runner,
        receipt_builder=receipt_builder,
        publisher=publisher,
        stdout=output,
    )

    assert result == 0
    assert events == [
        "child-exit",
        "receipt-built",
        "stdout-write",
        "stdout-flush",
        "published",
    ]
    assert target.read_bytes() == output.body


@pytest.mark.parametrize("failure", ("child-exit", "write", "flush"))
def test_outer_runner_never_publishes_success_before_child_and_output_complete(
    tmp_path: Path,
    failure: str,
) -> None:
    module = _module()
    published: list[bytes] = []
    child = SimpleNamespace(
        returncode=7 if failure == "child-exit" else 0,
        stdout=b'{"producer":"openmaic-dedicated-outage"}\n',
        stderr=b"child failed" if failure == "child-exit" else b"",
    )
    output = _Output([], fail_at=failure if failure in {"write", "flush"} else None)

    with pytest.raises(module.OpenMAICDedicatedOutageRunnerError):
        module._execute_child_and_publish(
            ["python", "probe.py", "--profile", "first-release"],
            tmp_path / "outage.json",
            cwd=tmp_path,
            environment={"PATH": "trusted"},
            runner=lambda *_args, **_kwargs: child,
            receipt_builder=lambda _body, _execution: b'{"execution":{"nativeExit":0}}\n',
            publisher=lambda _path, body: published.append(body),
            stdout=output,
        )

    assert published == []
    assert not (tmp_path / "outage.json").exists()


@pytest.mark.parametrize("cleanup_completes", (True, False), ids=("reconciled", "hard-kill"))
def test_outer_operation_timeout_allows_reconciliation_before_hard_kill(
    tmp_path: Path,
    cleanup_completes: bool,
) -> None:
    module = _module()
    events: list[str] = []

    class Child:
        returncode: int | None = None

        def communicate(self, *, timeout: float):
            events.append(f"communicate:{timeout:g}")
            if len([event for event in events if event.startswith("communicate:")]) == 1:
                raise subprocess.TimeoutExpired(["python", "probe.py"], timeout)
            if not cleanup_completes and "kill" not in events:
                raise subprocess.TimeoutExpired(["python", "probe.py"], timeout)
            self.returncode = 1
            return b"", b"openmaic_dedicated_outage_probe_failed\n"

        def kill(self) -> None:
            events.append("kill")

    child = Child()

    def process_factory(arguments, **kwargs):
        events.append("spawn")
        assert arguments == ["python", "probe.py", "--profile", "first-release"]
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"] == {"PATH": "trusted"}
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        return child

    def interrupt(process) -> None:
        assert process is child
        events.append("interrupt")

    if cleanup_completes:
        completed = module._capture_child_terminal_state(
            ["python", "probe.py", "--profile", "first-release"],
            cwd=tmp_path,
            environment={"PATH": "trusted"},
            operation_timeout_seconds=10,
            cleanup_grace_seconds=30,
            process_factory=process_factory,
            interrupt_child=interrupt,
            reconcile_after_forced_stop=lambda: events.append("reconcile"),
        )
        assert completed.returncode == 1
        assert "kill" not in events
    else:
        with pytest.raises(module.OpenMAICDedicatedOutageRunnerError):
            module._capture_child_terminal_state(
                ["python", "probe.py", "--profile", "first-release"],
                cwd=tmp_path,
                environment={"PATH": "trusted"},
                operation_timeout_seconds=10,
                cleanup_grace_seconds=30,
                process_factory=process_factory,
                interrupt_child=interrupt,
                reconcile_after_forced_stop=lambda: events.append("reconcile"),
            )
        assert "kill" in events
        assert "reconcile" in events

    assert events[:4] == ["spawn", "communicate:10", "interrupt", "communicate:30"]


def test_outer_hard_kill_runs_parent_reconciliation_before_returning_failure(
    tmp_path: Path,
) -> None:
    module = _module()
    events: list[str] = []

    class Child:
        pid = 101
        returncode: int | None = None

        def communicate(self, *, timeout: float):
            events.append(f"communicate:{timeout:g}")
            if "kill" not in events:
                raise subprocess.TimeoutExpired(["python", "probe.py"], timeout)
            self.returncode = -9
            return b"", b""

        def kill(self) -> None:
            events.append("kill")

    child = Child()

    with pytest.raises(
        module.OpenMAICDedicatedOutageRunnerError,
        match="outage_child_cleanup_timeout",
    ):
        module._capture_child_terminal_state(
            ["python", "probe.py"],
            cwd=tmp_path,
            environment={"PATH": "trusted"},
            operation_timeout_seconds=10,
            cleanup_grace_seconds=30,
            process_factory=lambda *_args, **_kwargs: child,
            interrupt_child=lambda _process: events.append("interrupt"),
            reconcile_after_forced_stop=lambda: events.append("reconcile"),
        )

    assert events == [
        "communicate:10",
        "interrupt",
        "communicate:30",
        "kill",
        "communicate:30",
        "reconcile",
    ]


def test_outer_does_not_reconcile_until_hard_killed_child_is_reaped(tmp_path: Path) -> None:
    module = _module()
    events: list[str] = []

    class Child:
        pid = 103
        returncode: int | None = None

        def communicate(self, *, timeout: float):
            events.append(f"communicate:{timeout:g}")
            raise subprocess.TimeoutExpired(["python", "probe.py"], timeout)

        def kill(self) -> None:
            events.append("kill")

    with pytest.raises(
        module.OpenMAICDedicatedOutageRunnerError,
        match="outage_child_reap_failed",
    ):
        module._capture_child_terminal_state(
            ["python", "probe.py"],
            cwd=tmp_path,
            environment={"PATH": "trusted"},
            operation_timeout_seconds=10,
            cleanup_grace_seconds=30,
            process_factory=lambda *_args, **_kwargs: Child(),
            interrupt_child=lambda _process: events.append("interrupt"),
            reconcile_after_forced_stop=lambda: events.append("reconcile"),
        )

    assert "kill" in events
    assert "reconcile" not in events


def test_outer_keyboard_interrupt_waits_for_child_reconciliation_before_propagating(
    tmp_path: Path,
) -> None:
    module = _module()
    events: list[str] = []

    class Child:
        pid = 102
        returncode: int | None = None

        def communicate(self, *, timeout: float):
            events.append(f"communicate:{timeout:g}")
            if "interrupt" not in events:
                raise KeyboardInterrupt
            self.returncode = 130
            return b"", b"openmaic_dedicated_outage_probe_failed\n"

        def kill(self) -> None:
            events.append("kill")

    child = Child()

    with pytest.raises(KeyboardInterrupt):
        module._capture_child_terminal_state(
            ["python", "probe.py"],
            cwd=tmp_path,
            environment={"PATH": "trusted"},
            operation_timeout_seconds=10,
            cleanup_grace_seconds=30,
            process_factory=lambda *_args, **_kwargs: child,
            interrupt_child=lambda _process: events.append("interrupt"),
            reconcile_after_forced_stop=lambda: events.append("reconcile"),
        )

    assert events == ["communicate:10", "interrupt", "communicate:30", "reconcile"]


def test_outer_nonzero_child_reconciles_before_returning_failure(tmp_path: Path) -> None:
    module = _module()
    events: list[str] = []
    child = SimpleNamespace(
        returncode=1,
        stdout=b"",
        stderr=b"openmaic_dedicated_outage_probe_failed\n",
    )

    with pytest.raises(
        module.OpenMAICDedicatedOutageRunnerError,
        match="outage_child_failed",
    ):
        module._execute_child_and_publish(
            ["python", "probe.py"],
            tmp_path / "outage.json",
            cwd=tmp_path,
            environment={"PATH": "trusted"},
            runner=lambda *_args, **_kwargs: child,
            receipt_builder=lambda _body, _execution: pytest.fail("receipt must not build"),
            reconcile_after_forced_stop=lambda: events.append("reconcile"),
        )

    assert events == ["reconcile"]


@pytest.mark.parametrize("running", (False, True), ids=("stopped", "running"))
def test_parent_reconciliation_restores_only_the_exact_dedicated_plane(
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
) -> None:
    module = _module()
    events: list[str] = []
    config = object()
    document = {"Id": "dedicated-container"}
    identity = object()

    class Controller:
        def _inspect(self):
            events.append("inspect")
            return document

        def _identity(self, actual):
            assert actual is document
            events.append("identity")
            return identity

        def _running(self, actual):
            assert actual is document
            events.append("running")
            return running, "healthy" if running else None

        def start(self, actual):
            assert actual is identity
            events.append("start")

        def wait_ready(self, actual):
            assert actual is identity
            events.append("ready")

    monkeypatch.setattr(
        module,
        "DockerDedicatedPlaneController",
        lambda actual: Controller() if actual is config else pytest.fail("wrong config"),
    )

    module._reconcile_dedicated_plane(config)

    assert events == [
        "inspect",
        "identity",
        "running",
        *([] if running else ["start"]),
        "ready",
    ]
