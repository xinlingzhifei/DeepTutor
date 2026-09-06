from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.scripts.platform_candidate_test_support import write_schema3_candidate_root

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "_hard_kill_child_tree", lambda process: process.kill())
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


def test_outer_does_not_reconcile_until_hard_killed_child_is_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "_hard_kill_child_tree", lambda process: process.kill())
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


_OUTAGE_PUBLICATIONS = (
    "openmaic-shared-ingress-observer-attestation.sig",
    "openmaic-shared-ingress-observer-attestation.json",
    "openmaic-dedicated-outage-attestation.json",
)
_OUTAGE_PHASES = (
    "child-returned",
    "observer-signature-published",
    "observer-body-published",
    "stdout-flushed",
    "receipt-published",
)


def _input_snapshot(paths: tuple[Path, ...]):
    return {path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino) for path in paths}


def _candidate_outage_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.scripts import test_openmaic_dedicated_outage_probe as support

    module = _module()
    probe = support._module()
    root, lock = write_schema3_candidate_root(tmp_path)
    runtime_root = root / "runtime"
    runtime_root.mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    config = support._config(probe, inputs)
    private_key, signature_path, public_key_path, public_key_sha = (
        support._observer_signature_fixture(inputs)
    )
    observer_body = support._observer_attestation_body(
        support._bound_observer_attestation(root, public_key_spki_sha256=public_key_sha)
    )
    config.observer_attestation_path.write_bytes(observer_body)
    support._write_observer_signature(private_key, signature_path, observer_body)
    runtime_path = runtime_root / "runtime-attestation.json"
    runtime_path.write_bytes(b'{"preserved":"runtime input"}\n')
    config = replace(
        config,
        candidate_root=root,
        candidate=lock["candidate"],
        attempt_marker_path=runtime_root / "openmaic-dedicated-outage-attempt.json",
        output_path=runtime_root / _OUTAGE_PUBLICATIONS[-1],
        openmaic_image_reference=lock["images"]["openmaic"]["reference"],
        runtime_attestation_sha256=hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
        observer_attestation_sha256=hashlib.sha256(observer_body).hexdigest(),
        observer_attestation_signature_path=signature_path,
        observer_attestation_public_key_path=public_key_path,
        observer_attestation_public_key_spki_sha256=public_key_sha,
    )
    # Produce a real canonical inner report and attempt marker using only local
    # fixture methods. The parent still performs real parsing and Ed25519 verification.
    child_body = asyncio.run(
        probe.run_dedicated_outage_probe(
            config,
            runtime=support._Runtime(probe),
            observed_at=lambda: support.OBSERVER_OBSERVED_AT.isoformat().replace("+00:00", "Z"),
        )
    )
    protected = (
        config.observer_attestation_path,
        signature_path,
        public_key_path,
        config.attempt_marker_path,
        runtime_path,
    )
    snapshot = _input_snapshot(protected)
    events: list[str] = []
    hooks = {"after": lambda _phase: None}

    class Output(_Output):
        def flush(self) -> None:
            super().flush()
            events.append("stdout-flushed")
            hooks["after"]("stdout-flushed")

    output = Output([])

    def capture(command, **kwargs):
        assert command[-2:] == ["--profile", "first-release"]
        assert kwargs["cwd"] == root
        events.append("child-returned")
        hooks["after"]("child-returned")
        return module._ChildTerminalState(returncode=0, stdout=child_body, stderr=b"")

    real_execute = module._execute_child_and_publish

    def execute(*args, **kwargs):
        assert kwargs["runner"] is None
        return real_execute(*args, **kwargs, stdout=output)

    guard_type = sys.modules["classroom_runtime_attestation"]._RuntimeDirectoryGuard
    real_replace = guard_type.replace
    phase_for_name = dict(
        zip(
            _OUTAGE_PUBLICATIONS,
            (_OUTAGE_PHASES[1], _OUTAGE_PHASES[2], _OUTAGE_PHASES[4]),
            strict=True,
        )
    )

    def publish(guard, source: str, target: str) -> None:
        real_replace(guard, source, target)
        if guard.runtime_root == runtime_root and target in phase_for_name:
            phase = phase_for_name[target]
            events.append(phase)
            hooks["after"](phase)

    monkeypatch.setattr(guard_type, "replace", publish)
    monkeypatch.setattr(module, "_capture_child_terminal_state", capture)
    monkeypatch.setattr(module, "_execute_child_and_publish", execute)
    monkeypatch.setattr(module, "_load_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        module, "datetime", SimpleNamespace(now=lambda _zone: support.OBSERVER_OBSERVED_AT)
    )
    monkeypatch.setenv("YFEISTAI_CANDIDATE_ROOT", str(root))
    monkeypatch.chdir(root)
    return module, config, output, events, hooks, snapshot


def test_outer_runner_publishes_one_candidate_bound_receipt_and_archives_signed_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, config, output, events, _hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)

    assert module.main(["--profile", "first-release"]) == 0

    assert events == list(_OUTAGE_PHASES)
    assert config.output_path.read_bytes() == output.body
    assert (config.candidate_root / "runtime" / _OUTAGE_PUBLICATIONS[0]).read_bytes() == (
        config.observer_attestation_signature_path.read_bytes()
    )
    assert (config.candidate_root / "runtime" / _OUTAGE_PUBLICATIONS[1]).read_bytes() == (
        config.observer_attestation_path.read_bytes()
    )
    assert _input_snapshot(tuple(snapshot)) == snapshot


@pytest.mark.parametrize("phase", _OUTAGE_PHASES)
@pytest.mark.parametrize(
    "candidate_file",
    (
        "deploy/image-lock.json",
        "docker-compose.yml",
        "docker-compose.platform.yml",
        "docker-compose.data-plane.yml",
        "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
    ),
    ids=("lock", "base", "platform", "data-plane", "observer"),
)
def test_outer_runner_rejects_candidate_replacement_through_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_file: str,
    phase: str,
) -> None:
    module, config, output, events, hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)
    target = config.candidate_root / candidate_file
    replacement = tmp_path / "same-bytes-candidate-file"
    replacement.write_bytes(target.read_bytes())
    real_stat, real_lstat = Path.stat, Path.lstat
    changed = False

    def stat(path, *args, **kwargs):
        return real_stat(replacement if changed and Path(path) == target else path, *args, **kwargs)

    def lstat(path):
        return real_lstat(replacement if changed and Path(path) == target else path)

    def mutate(actual_phase):
        nonlocal changed
        if actual_phase != phase:
            return
        if os.name != "nt":
            os.replace(replacement, target)
        changed = True

    if os.name == "nt":
        # Windows retained handles deny native replacement; model the changed
        # pathname identity without pretending this is a sharing-lock bypass.
        monkeypatch.setattr(Path, "stat", stat)
        monkeypatch.setattr(Path, "lstat", lstat)
    hooks["after"] = mutate

    result = module.main(["--profile", "first-release"])

    assert changed and events == list(_OUTAGE_PHASES[: _OUTAGE_PHASES.index(phase) + 1])
    assert result == 1
    assert not any((config.output_path.parent / name).exists() for name in _OUTAGE_PUBLICATIONS)
    assert not list(config.output_path.parent.glob(".*.staged"))
    assert _input_snapshot(tuple(snapshot)) == snapshot
    assert bool(output.body) is (phase in {"stdout-flushed", "receipt-published"})


def test_outer_runner_rejects_initial_candidate_drift_before_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, config, output, events, _hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)
    base = config.candidate_root / "docker-compose.yml"
    base.write_bytes(base.read_bytes() + b"\n# candidate drift\n")

    assert module.main(["--profile", "first-release"]) == 1

    assert events == [] and output.body == b""
    assert not any((config.output_path.parent / name).exists() for name in _OUTAGE_PUBLICATIONS)
    assert _input_snapshot(tuple(snapshot)) == snapshot


@pytest.mark.parametrize("existing_anchors", (False, True), ids=("new-anchors", "retained-anchors"))
def test_outer_runner_lease_exit_failure_removes_only_owned_publications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_anchors: bool,
) -> None:
    module, config, output, events, _hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)
    existing = ()
    if existing_anchors:
        existing = tuple(config.output_path.parent / name for name in _OUTAGE_PUBLICATIONS[:2])
        for path, source in zip(
            existing,
            (config.observer_attestation_signature_path, config.observer_attestation_path),
            strict=True,
        ):
            path.write_bytes(source.read_bytes())
        snapshot.update(_input_snapshot(existing))
    real_lease = sys.modules["render_platform_compose"].validated_candidate_artifact_lease
    injected = False

    @contextmanager
    def exit_failure(*args, **kwargs):
        nonlocal injected
        with real_lease(*args, **kwargs) as artifact:
            yield artifact
        assert config.output_path.is_file() and output.body
        injected = True
        raise ValueError("injected candidate lease exit failure")

    monkeypatch.setattr(module, "validated_candidate_artifact_lease", exit_failure, raising=False)

    result = module.main(["--profile", "first-release"])

    assert injected and events[-1] == "receipt-published"
    assert result == 1 and not config.output_path.exists()
    for name in _OUTAGE_PUBLICATIONS[:2]:
        assert (config.output_path.parent / name).exists() is existing_anchors
    assert not list(config.output_path.parent.glob(".*.staged"))
    assert _input_snapshot(tuple(snapshot)) == snapshot


@pytest.mark.parametrize("same_content", (False, True), ids=("different-content", "same-content"))
def test_outer_runner_preserves_competing_archive_when_no_replace_loses_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_content: bool,
) -> None:
    module, config, output, events, hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)
    guard_type = sys.modules["classroom_runtime_attestation"]._RuntimeDirectoryGuard
    real_replace = guard_type.replace
    competitor = config.output_path.parent / _OUTAGE_PUBLICATIONS[0]
    competing_body = (
        config.observer_attestation_signature_path.read_bytes()
        if same_content
        else b"competing observer signature"
    )
    injected = False
    competing_snapshot = {}

    def competing_publish(guard, source, target):
        nonlocal injected
        if target == competitor.name:
            competitor.write_bytes(competing_body)
            competing_snapshot.update(_input_snapshot((competitor,)))
            injected = True
        return real_replace(guard, source, target)

    def abort_after_flush(phase):
        if phase == "stdout-flushed":
            raise ValueError("failure after borrowing a competing identical archive")

    if same_content:
        hooks["after"] = abort_after_flush
    monkeypatch.setattr(guard_type, "replace", competing_publish)

    assert module.main(["--profile", "first-release"]) == 1

    assert injected
    assert events == (
        ["child-returned", "observer-body-published", "stdout-flushed"]
        if same_content
        else ["child-returned"]
    )
    assert bool(output.body) is same_content
    assert _input_snapshot((competitor,)) == competing_snapshot
    assert not any((config.output_path.parent / name).exists() for name in _OUTAGE_PUBLICATIONS[1:])
    assert not list(config.output_path.parent.glob(".*.staged"))
    assert _input_snapshot(tuple(snapshot)) == snapshot


@pytest.mark.parametrize("close_at", (*_OUTAGE_PUBLICATIONS, "directory-close"))
def test_outer_runner_close_failure_retracts_formal_outputs_after_ownership_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    close_at: str,
) -> None:
    module, config, output, events, _hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)
    guard_type = sys.modules["classroom_runtime_attestation"]._RuntimeDirectoryGuard
    real_release, real_close = guard_type.release_owned_file, guard_type.close
    injected = False

    def release(guard, identity):
        nonlocal injected
        name = guard._owned_file_names.get(identity)
        real_release(guard, identity)
        if not injected and guard.runtime_root == config.output_path.parent and name == close_at:
            injected = True
            raise OSError("injected failure after output handle release")

    def close(guard, **kwargs):
        nonlocal injected
        real_close(guard, **kwargs)
        if (
            not injected
            and close_at == "directory-close"
            and guard.runtime_root == config.output_path.parent
            and events[-1:] == ["receipt-published"]
        ):
            injected = True
            raise OSError("injected failure after output directory close")

    monkeypatch.setattr(guard_type, "release_owned_file", release)
    monkeypatch.setattr(guard_type, "close", close)

    assert module.main(["--profile", "first-release"]) == 1

    assert injected and events == list(_OUTAGE_PHASES) and output.body
    assert not any((config.output_path.parent / name).exists() for name in _OUTAGE_PUBLICATIONS)
    assert not list(config.output_path.parent.glob(".*.staged"))
    assert _input_snapshot(tuple(snapshot)) == snapshot


def test_outer_runner_tail_cleanup_preserves_a_same_byte_competing_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, config, output, events, _hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)
    guard_type = sys.modules["classroom_runtime_attestation"]._RuntimeDirectoryGuard
    real_close = guard_type.close
    competing_snapshot = {}

    def close(guard, **kwargs):
        real_close(guard, **kwargs)
        if (
            not competing_snapshot
            and guard.runtime_root == config.output_path.parent
            and events[-1:] == ["receipt-published"]
        ):
            replacement = tmp_path / "same-byte-competing-receipt"
            replacement.write_bytes(config.output_path.read_bytes())
            replacement.replace(config.output_path)
            competing_snapshot.update(_input_snapshot((config.output_path,)))
            raise OSError("injected directory close failure after competing replacement")

    monkeypatch.setattr(guard_type, "close", close)

    assert module.main(["--profile", "first-release"]) == 1

    assert competing_snapshot and events == list(_OUTAGE_PHASES) and output.body
    assert _input_snapshot((config.output_path,)) == competing_snapshot
    assert not any((config.output_path.parent / name).exists() for name in _OUTAGE_PUBLICATIONS[:2])
    assert _input_snapshot(tuple(snapshot)) == snapshot


@pytest.mark.skipif(os.name != "nt", reason="Windows CloseHandle failure boundary")
def test_outer_runner_failed_native_close_retracts_the_still_open_owned_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, config, output, events, _hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)
    runtime_module = sys.modules["classroom_runtime_attestation"]
    guard_type = runtime_module._RuntimeDirectoryGuard
    real_release = guard_type.release_owned_file
    real_close = runtime_module._close_windows_handle
    state = {"handle": None, "injected": False, "open": False}

    def release(guard, identity):
        if guard._owned_file_names.get(identity) == config.output_path.name:
            state["handle"] = guard._owned_file_handles[identity]
        real_release(guard, identity)

    def close(handle):
        if handle == state["handle"] and not state["injected"]:
            state["injected"] = True
            state["open"] = True
            raise OSError("injected native CloseHandle failure before close")
        real_close(handle)
        if handle == state["handle"]:
            state["open"] = False

    monkeypatch.setattr(guard_type, "release_owned_file", release)
    monkeypatch.setattr(runtime_module, "_close_windows_handle", close)
    try:
        assert module.main(["--profile", "first-release"]) == 1
        assert state["injected"] and events == list(_OUTAGE_PHASES) and output.body
        assert not any((config.output_path.parent / name).exists() for name in _OUTAGE_PUBLICATIONS)
        assert _input_snapshot(tuple(snapshot)) == snapshot
    finally:
        if state["open"]:
            real_close(state["handle"])


def test_outer_runner_reconciles_failed_child_even_when_candidate_has_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, config, output, events, _hooks, snapshot = _candidate_outage_case(tmp_path, monkeypatch)
    base = config.candidate_root / "docker-compose.yml"
    replacement = tmp_path / "changed-candidate-identity"
    replacement.write_bytes(base.read_bytes())
    real_stat, real_lstat = Path.stat, Path.lstat
    changed = False

    def stat(path, *args, **kwargs):
        return real_stat(replacement if changed and Path(path) == base else path, *args, **kwargs)

    def lstat(path):
        return real_lstat(replacement if changed and Path(path) == base else path)

    def failed_child(*_args, **_kwargs):
        nonlocal changed
        events.append("child-failed")
        changed = True
        return module._ChildTerminalState(returncode=1, stdout=b"", stderr=b"failure")

    with monkeypatch.context() as phase_patch:
        phase_patch.setattr(Path, "stat", stat)
        phase_patch.setattr(Path, "lstat", lstat)
        phase_patch.setattr(module, "_capture_child_terminal_state", failed_child)
        phase_patch.setattr(
            module, "_reconcile_dedicated_plane", lambda _config: events.append("reconciled")
        )
        assert module.main(["--profile", "first-release"]) == 1

    assert events == ["child-failed", "reconciled"] and output.body == b""
    assert _input_snapshot(tuple(snapshot)) == snapshot
