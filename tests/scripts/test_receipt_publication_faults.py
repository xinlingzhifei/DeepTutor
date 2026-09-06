from __future__ import annotations

from contextlib import nullcontext
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from tests.scripts.test_classroom_release_evidence import (
    BASE_URL,
    OBSERVER_SIGNER_IMAGE,
    RELEASE_RUN,
    ROOT,
    _load_evidence_module,
    _teacher_playwright_report,
    _write_candidate_root,
)
from tests.scripts.test_receipt_publication_attempt import publication

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="requires isolated native Linux publisher fixtures below trusted ancestors",
)


def _publish_temporary(module, kind, target, owned):
    if kind == "json":
        module._atomic_write_json_no_replace(target, {"original": True}, owned_publications=owned)
    elif kind == "legacy-json":
        module._atomic_write_json(target, {"original": True})
    elif kind == "gateway":
        module._stage_gateway_json(target.parent, {"original": True})
    else:
        module._publish_release_supervisor_public_key(
            target, b"original public key\x00\xff", owned_publications=owned
        )


def _assert_closed(descriptor):
    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("kind", ("json", "public-key", "legacy-json", "gateway"))
def test_named_temporary_parent_swap_rejects_wrong_pin_and_preserves_both_directories(
    tmp_path, monkeypatch, kind
):
    module = _load_evidence_module()
    parent = tmp_path / "output"
    parent.mkdir()
    sentinel = parent / "unrelated.json"
    sentinel.write_bytes(b"original directory sentinel")
    parent_identity = module._file_identity(parent.stat())
    sentinel_identity = module._file_identity(sentinel.stat())
    displaced = tmp_path / "original-output"
    target = parent / "receipt.json"
    temporary = module.tempfile.NamedTemporaryFile
    captured = {}

    def create_then_rotate(*args, **kwargs):
        handle = temporary(*args, **kwargs)
        captured["handle"] = handle
        captured["descriptor"] = handle.fileno()
        captured["identity"] = module._file_identity(os.fstat(handle.fileno()))
        captured["name"] = Path(handle.name).name
        os.rename(parent, displaced)
        parent.mkdir()
        captured["parent_identity"] = module._file_identity(parent.stat())
        sentinel.write_bytes(b"replacement directory sentinel")
        captured["sentinel_identity"] = module._file_identity(sentinel.stat())
        foreign = parent / captured["name"]
        foreign.write_bytes(b"unknown same-name temporary\x00\xff")
        captured["foreign_identity"] = module._file_identity(foreign.stat())
        return handle

    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", create_then_rotate)
    owned = {}
    with publication(module, tmp_path) as attempt:
        with pytest.raises(ValueError, match="parent/name binding changed"):
            _publish_temporary(module, kind, target, owned)
        assert captured["handle"].closed
        _assert_closed(captured["descriptor"])
        assert attempt.originals == {}
        assert owned == {}
        assert not list((tmp_path / "publisher-recovery").iterdir())
    assert not target.exists()
    assert module._file_identity(displaced.stat()) == parent_identity
    assert module._file_identity(parent.stat()) == captured["parent_identity"]
    assert parent_identity != captured["parent_identity"]
    original_sentinel = displaced / sentinel.name
    assert (
        original_sentinel.read_bytes(),
        module._file_identity(original_sentinel.stat()),
    ) == (b"original directory sentinel", sentinel_identity)
    assert (sentinel.read_bytes(), module._file_identity(sentinel.stat())) == (
        b"replacement directory sentinel",
        captured["sentinel_identity"],
    )
    original = displaced / captured["name"]
    foreign = parent / captured["name"]
    assert (original.read_bytes(), module._file_identity(original.stat())) == (
        b"",
        captured["identity"],
    )
    assert (foreign.read_bytes(), module._file_identity(foreign.stat())) == (
        b"unknown same-name temporary\x00\xff",
        captured["foreign_identity"],
    )


@pytest.mark.parametrize("kind", ("json", "public-key"))
@pytest.mark.parametrize("interruption", (KeyboardInterrupt, SystemExit))
def test_staging_recovery_error_preserves_primary_control_flow_and_note(
    tmp_path, monkeypatch, kind, interruption
):
    module = _load_evidence_module()
    recovery = sys.modules[module.PosixReceiptRecovery.__module__]
    target = tmp_path / "receipt.json"
    primary = interruption("injected post-link interruption")
    real_link = module._link_posix_file_descriptor
    linked = []
    detach_attempts = []

    def link_then_interrupt(original_fd, parent_fd, name):
        real_link(original_fd, parent_fd, name)
        linked.append(module._file_identity(os.fstat(original_fd)))
        raise primary

    def fail_detach(source_fd, source, destination_fd, destination):
        detach_attempts.append((source, destination))
        raise OSError(errno.EIO, "injected staging recovery detach failure")

    monkeypatch.setattr(module, "_link_posix_file_descriptor", link_then_interrupt)
    monkeypatch.setattr(recovery, "_rename_no_replace", fail_detach)
    owned = {}
    with publication(module, tmp_path) as attempt:
        with pytest.raises(interruption) as caught:
            _publish_temporary(module, kind, target, owned)
        assert caught.value is primary
        assert len(linked) == len(detach_attempts) == 1
        assert owned[target] == linked[0]
        assert any(
            "receipt staging recovery failed" in note and "during detach" in note
            for note in caught.value.__notes__
        )
        staged = tmp_path / detach_attempts[0][0]
        original = attempt.original(staged)
        pins = [
            descriptor
            for pin in attempt.originals.values()
            for descriptor in (pin.descriptor, pin.parent_descriptor)
        ]
        assert module._file_identity(os.fstat(original.descriptor)) == linked[0]
        assert module._file_identity(os.fstat(original.parent_descriptor)) == module._file_identity(
            tmp_path.stat()
        )
        body = staged.read_bytes()
        assert body and os.pread(original.descriptor, len(body), 0) == body
        assert (target.read_bytes(), module._file_identity(target.stat())) == (body, linked[0])
        records = [
            json.loads(path.read_bytes())
            for path in (tmp_path / "publisher-recovery").glob("*.recovery.json")
        ]
        assert len(records) == 1 and records[0]["state"] == "prepared"
        assert records[0]["expectedIdentity"] == list(linked[0])
        assert not Path(records[0]["recoveryPath"]).exists()
    for descriptor in pins:
        _assert_closed(descriptor)
    assert staged.read_bytes() == target.read_bytes() == body


@pytest.mark.parametrize("failure", ("digest", "pin"))
def test_second_artifact_failure_closes_original_handles_but_retains_attempt_pins(
    tmp_path, monkeypatch, failure
):
    module = _load_evidence_module()
    root = tmp_path / "bundle"
    root.mkdir()
    opened = []
    real_open = module._open_classroom_artifact_relative
    real_digest = module._classroom_file_digest
    real_dup = os.dup
    injected = []

    def record_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    def fail_second_digest(handle):
        if len(opened) == 2 and handle is opened[1]:
            injected.append("digest")
            raise OSError(errno.EIO, "injected second artifact digest failure")
        return real_digest(handle)

    def fail_second_pin(descriptor):
        if len(opened) == 2 and descriptor == opened[1].fileno():
            injected.append("pin")
            raise OSError(errno.EMFILE, "injected second artifact pin failure")
        return real_dup(descriptor)

    monkeypatch.setattr(module, "_open_classroom_artifact_relative", record_open)
    if failure == "digest":
        monkeypatch.setattr(module, "_classroom_file_digest", fail_second_digest)
    else:
        monkeypatch.setattr(os, "dup", fail_second_pin)
    with publication(module, tmp_path) as attempt:
        boundary = module._ClassroomPublicationBoundary.open(root, "a" * 32)
        assert boundary.staging is not None
        bodies = {}
        for kind, name in module.CLASSROOM_EXPORT_PATHS.items():
            path = boundary.staging / name
            bodies[path] = f"original {kind} artifact".encode()
            path.write_bytes(bodies[path])
        try:
            with pytest.raises(OSError, match=f"second artifact {failure} failure"):
                module._open_classroom_artifact_leases(boundary)
            assert injected == [failure]
            assert len(opened) == 2
            original_closed = [handle.closed for handle in opened]
            assert len(attempt.originals) == (2 if failure == "digest" else 1)
            pins = []
            for path, original in attempt.originals.items():
                pins.extend((original.descriptor, original.parent_descriptor))
                assert module._file_identity(os.fstat(original.descriptor)) == original.identity
                assert os.pread(original.descriptor, len(bodies[path]), 0) == bodies[path]
                assert module._file_identity(
                    os.fstat(original.parent_descriptor)
                ) == module._file_identity(boundary.staging.stat())
        finally:
            # Record the production close result first; do not leak a failing test's handles.
            for handle in opened:
                handle.close()
            boundary.close()
    for descriptor in pins:
        _assert_closed(descriptor)
    assert original_closed == [True, True], "the second unaccepted artifact handle leaked"
    assert all(path.read_bytes() == body for path, body in bodies.items())


@pytest.mark.parametrize("rejection", ("absent-policy", "cross-device"))
def test_probe_policy_rejection_precedes_output_mkdir_and_runner(tmp_path, monkeypatch, rejection):
    module = _load_evidence_module()
    root = tmp_path / "not-created-bundle"
    output = root / "artifacts" / "receipt.json"
    expected = "receipt publication policy"
    if rejection == "cross-device":
        other_filesystem = Path("/dev/shm")
        if (
            not other_filesystem.is_dir()
            or other_filesystem.stat().st_dev == tmp_path.stat().st_dev
        ):
            pytest.skip("a second native filesystem is unavailable")
        root = other_filesystem / f"yfeistai-admission-{uuid.uuid4().hex}"
        output = root / "artifacts" / "receipt.json"
        expected = "destination filesystem"
    assert not root.exists()
    mkdir_calls = []
    runner_calls = []

    def forbid_mkdir(path, *args, **kwargs):
        mkdir_calls.append(os.fspath(path))
        raise AssertionError("publication attempted mkdir before policy admission")

    def forbid_runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        raise AssertionError("publication invoked probe before policy admission")

    scope = publication(module, tmp_path) if rejection == "cross-device" else nullcontext()
    with scope:
        monkeypatch.setattr(os, "mkdir", forbid_mkdir)
        with pytest.raises(ValueError, match=expected):
            module.run_probe_receipt(
                output,
                candidate_root=tmp_path / "candidate-must-not-be-read",
                bundle_root=root,
                release_run=RELEASE_RUN,
                evidence="teacher_flow",
                observed_at="2026-09-06T00:00:00Z",
                base_url="http://127.0.0.1:1",
                raw_report_path=root / "raw" / "report.json",
                execution_record_path=root / "executions" / "record.json",
                recipe="teacher_flow",
                working_directory=tmp_path,
                timeout_seconds=30,
                runner=forbid_runner,
            )
    assert mkdir_calls == runner_calls == []
    assert not root.exists()
    if rejection == "cross-device":
        assert not list((tmp_path / "publisher-recovery").iterdir())


def _generic_probe_case(tmp_path, monkeypatch):
    secret = b"generic-probe-raw-secret-must-not-be-archived"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", secret.decode())
    monkeypatch.setenv("YFEISTAI_OBSERVER_SIGNER_IMAGE", OBSERVER_SIGNER_IMAGE)
    module = _load_evidence_module()
    root, _candidate = _write_candidate_root(tmp_path)
    paths = {
        "receipt": root / "artifacts" / "teacher_flow.json",
        "report": root / "raw" / "teacher_flow.json",
        "execution": root / "raw" / "teacher_flow.execution.json",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    state = {}

    def run_probe(arguments, *, cwd, env, timeout):
        assert cwd == ROOT.resolve() and timeout == 300
        assert "staged_report" not in state
        report = _teacher_playwright_report()
        # Use a valid native report identifier as a raw-byte sentinel; nonempty
        # stdout is rightly rejected before publication by the existing policy.
        report["suites"][0]["specs"][0]["id"] = secret.decode()
        staged = Path(env["YFEISTAI_EVIDENCE_REPORT"])
        staged.write_bytes(json.dumps(report).encode())
        state["staged_report"] = staged
        state["raw_body"] = staged.read_bytes()
        return subprocess.CompletedProcess(arguments, 0)

    arguments = {
        "output_path": paths["receipt"],
        "candidate_root": root,
        "bundle_root": root,
        "release_run": RELEASE_RUN,
        "evidence": "teacher_flow",
        "observed_at": "2026-08-25T00:00:00Z",
        "base_url": BASE_URL,
        "raw_report_path": paths["report"],
        "execution_record_path": paths["execution"],
        "recipe": "teacher_flow",
        "working_directory": ROOT,
        "timeout_seconds": 300,
        "runner": run_probe,
    }
    return module, root, paths, arguments, state, secret


def _assert_no_raw_secret_archived(tmp_path, secret):
    for failure_root in tmp_path.rglob("failures"):
        for path in failure_root.rglob("*"):
            if path.is_file():
                assert secret not in path.read_bytes(), f"raw secret was archived in {path}"


def _observe_producer_pin_close(module, monkeypatch):
    real_close = module._ReceiptPublicationAttempt.close
    snapshots = {}

    def record_then_close(attempt):
        for path, original in attempt.originals.items():
            details = os.fstat(original.descriptor)
            snapshots[path] = {
                "original": original,
                "identity": module._file_identity(details),
                "parent_identity": module._file_identity(os.fstat(original.parent_descriptor)),
                "body": os.pread(original.descriptor, details.st_size, 0),
                "links": details.st_nlink,
            }
        return real_close(attempt)

    monkeypatch.setattr(module._ReceiptPublicationAttempt, "close", record_then_close)
    return snapshots


@pytest.mark.parametrize(
    "rotation", ("raw-parent", "receipt-parent", "bundle-missing", "bundle-replacement")
)
def test_generic_probe_compensation_uses_original_parents_after_rotation(
    tmp_path, monkeypatch, rotation
):
    module, root, paths, arguments, state, secret = _generic_probe_case(tmp_path, monkeypatch)
    primary = OSError("injected publication failure after parent rotation")
    real_link = module._link_posix_file_descriptor
    receipt_parent_identity = module._file_identity(paths["receipt"].parent.stat())
    rotated = {
        "raw-parent": paths["report"].parent,
        "receipt-parent": paths["receipt"].parent,
        "bundle-missing": root,
        "bundle-replacement": root,
    }[rotation]
    displaced = tmp_path / "displaced-publication-parent"
    sentinel = rotated / "unrelated.bin"
    sentinel.write_bytes(b"original parent sentinel\x00\xff")
    sentinel_identity = module._file_identity(sentinel.stat())
    rotated_identity = module._file_identity(rotated.stat())
    captured = {}
    competitors = {}
    close_snapshots = _observe_producer_pin_close(module, monkeypatch)

    def link_then_rotate(original_fd, parent_fd, name):
        real_link(original_fd, parent_fd, name)
        if (
            name == paths["receipt"].name
            and module._file_identity(os.fstat(parent_fd)) == receipt_parent_identity
            and not captured
        ):
            attempt = module._require_receipt_attempt()
            for label, path in paths.items():
                original = attempt.original(path)
                captured[label] = (original, path.read_bytes())
            os.rename(rotated, displaced)
            if rotation != "bundle-missing":
                rotated.mkdir()
                for label, path in paths.items():
                    if path.is_relative_to(rotated):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        body = f"foreign {label} from replacement parent".encode()
                        path.write_bytes(body)
                        competitors[path] = (body, module._file_identity(path.stat()))
                sentinel.write_bytes(b"replacement parent sentinel")
                competitors[sentinel] = (
                    sentinel.read_bytes(),
                    module._file_identity(sentinel.stat()),
                )
            raise primary

    monkeypatch.setattr(module, "_link_posix_file_descriptor", link_then_rotate)
    with publication(module, tmp_path):
        with pytest.raises(OSError) as caught:
            module.run_probe_receipt(**arguments)
        assert caught.value is primary
        assert set(captured) == set(paths), "must reach the final native receipt link"
        assert module._file_identity(displaced.stat()) == rotated_identity
        original_sentinel = displaced / sentinel.relative_to(rotated)
        assert (
            original_sentinel.read_bytes(),
            module._file_identity(original_sentinel.stat()),
        ) == (b"original parent sentinel\x00\xff", sentinel_identity)
        for path, expected in competitors.items():
            assert (path.read_bytes(), module._file_identity(path.stat())) == expected
        for label, path in paths.items():
            original, body = captured[label]
            snapshot = close_snapshots[path]
            assert snapshot["identity"] == original.identity
            assert snapshot["body"] == body
            _assert_closed(original.descriptor)
            _assert_closed(original.parent_descriptor)
            actual = displaced / path.relative_to(rotated) if path.is_relative_to(rotated) else path
            assert snapshot["parent_identity"] == module._file_identity(actual.parent.stat())
            assert not actual.exists(), f"owned {label} survived failed producer compensation"
        assert close_snapshots[paths["report"]]["links"] == 0
        staged = state["staged_report"]
        actual_staged = (
            displaced / staged.relative_to(rotated) if staged.is_relative_to(rotated) else staged
        )
        assert not actual_staged.exists()
    _assert_no_raw_secret_archived(tmp_path, secret)


def test_generic_probe_archive_failure_still_retracts_all_formal_outputs(tmp_path, monkeypatch):
    module, root, paths, arguments, state, secret = _generic_probe_case(tmp_path, monkeypatch)
    primary = OSError("injected post-receipt publication failure")
    archive_error = OSError(errno.EIO, "injected failure archive mkdir failure")
    real_link = module._link_posix_file_descriptor
    real_mkdir = os.mkdir
    receipt_parent_identity = module._file_identity(paths["receipt"].parent.stat())
    root_identity = module._file_identity(root.stat())
    linked = []
    archive_attempts = []
    close_snapshots = _observe_producer_pin_close(module, monkeypatch)

    def link_then_fail(original_fd, parent_fd, name):
        real_link(original_fd, parent_fd, name)
        if (
            name == paths["receipt"].name
            and module._file_identity(os.fstat(parent_fd)) == receipt_parent_identity
            and not linked
        ):
            linked.append(True)
            raise primary

    def fail_archive_mkdir(path, mode=0o777, *, dir_fd=None):
        if (
            path == "failures"
            and dir_fd is not None
            and module._file_identity(os.fstat(dir_fd)) == root_identity
        ):
            archive_attempts.append(True)
            raise archive_error
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module, "_link_posix_file_descriptor", link_then_fail)
    monkeypatch.setattr(os, "mkdir", fail_archive_mkdir)
    with publication(module, tmp_path):
        with pytest.raises(OSError) as caught:
            module.run_probe_receipt(**arguments)
        assert caught.value is primary
        assert linked == archive_attempts == [True]
        assert any("injected failure archive mkdir failure" in note for note in primary.__notes__)
        for label, path in paths.items():
            assert not path.exists(), f"archive failure left formal {label} published"
            snapshot = close_snapshots[path]
            original = snapshot["original"]
            assert snapshot["identity"] == original.identity
            _assert_closed(original.descriptor)
            _assert_closed(original.parent_descriptor)
        assert not state["staged_report"].exists()
        assert close_snapshots[paths["report"]]["links"] == 0
    _assert_no_raw_secret_archived(tmp_path, secret)


@pytest.mark.parametrize("archive_error_type", (ValueError, NotADirectoryError))
def test_generic_probe_archive_boundary_and_restore_conflict_preserve_primary_recovery_notes(
    tmp_path, monkeypatch, archive_error_type
):
    module, root, paths, arguments, state, secret = _generic_probe_case(tmp_path, monkeypatch)
    recovery = sys.modules[module.PosixReceiptRecovery.__module__]
    primary = OSError("injected producer failure before conflicting recovery")
    archive_error = archive_error_type("injected archive boundary admission failure")
    real_link = module._link_posix_file_descriptor
    real_mkdir = os.mkdir
    real_rename = recovery._rename_no_replace
    real_remove = module.PosixReceiptRecovery.remove_owned
    receipt_parent_identity = module._file_identity(paths["receipt"].parent.stat())
    root_identity = module._file_identity(root.stat())
    displaced = paths["receipt"].with_name("original-receipt-backup.json")
    foreign_body = b"foreign detached receipt\x00\xff"
    occupant_body = b"foreign restore conflict occupant\xff\x00"
    captured = {}
    archive_attempts = []
    recovery_events = []
    conflicts = []
    close_snapshots = _observe_producer_pin_close(module, monkeypatch)

    def link_then_fail(original_fd, parent_fd, name):
        real_link(original_fd, parent_fd, name)
        if (
            name == paths["receipt"].name
            and module._file_identity(os.fstat(parent_fd)) == receipt_parent_identity
            and "original" not in captured
        ):
            original = module._require_receipt_attempt().original(paths["receipt"])
            captured["original"] = (paths["receipt"].read_bytes(), original.identity)
            raise primary

    def fail_archive_mkdir(path, mode=0o777, *, dir_fd=None):
        if (
            path == "failures"
            and dir_fd is not None
            and module._file_identity(os.fstat(dir_fd)) == root_identity
        ):
            archive_attempts.append(True)
            raise archive_error
        return real_mkdir(path, mode, dir_fd=dir_fd)

    def compete_at_detach_and_restore(source_fd, source, target_fd, target):
        if (
            source == paths["receipt"].name
            and module._file_identity(os.fstat(source_fd)) == receipt_parent_identity
            and not recovery_events
        ):
            os.rename(paths["receipt"], displaced)
            paths["receipt"].write_bytes(foreign_body)
            captured["foreign_identity"] = module._file_identity(paths["receipt"].stat())
            recovery_events.append("detach")
        elif (
            target == paths["receipt"].name
            and module._file_identity(os.fstat(target_fd)) == receipt_parent_identity
            and recovery_events == ["detach"]
        ):
            original = module._require_receipt_attempt().original(paths["receipt"])
            assert module._file_identity(os.fstat(original.descriptor)) == captured["original"][1]
            paths["receipt"].write_bytes(occupant_body)
            captured["occupant_identity"] = module._file_identity(paths["receipt"].stat())
            recovery_events.append("restore")
        return real_rename(source_fd, source, target_fd, target)

    def observe_remove_error(self, *args, **kwargs):
        try:
            return real_remove(self, *args, **kwargs)
        except recovery.ReceiptRecoveryError as error:
            if kwargs["source_path"] == paths["receipt"]:
                conflicts.append(error)
            raise

    monkeypatch.setattr(module, "_link_posix_file_descriptor", link_then_fail)
    monkeypatch.setattr(os, "mkdir", fail_archive_mkdir)
    monkeypatch.setattr(recovery, "_rename_no_replace", compete_at_detach_and_restore)
    monkeypatch.setattr(module.PosixReceiptRecovery, "remove_owned", observe_remove_error)
    with publication(module, tmp_path):
        with pytest.raises(OSError) as caught:
            module.run_probe_receipt(**arguments)
        assert caught.value is primary
        assert archive_attempts == [True]
        assert recovery_events == ["detach", "restore"]
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert isinstance(conflict.__cause__, FileExistsError)
        assert conflict.location_confirmed
        retained = conflict.recovery_path
        journal = conflict.journal_path
        assert retained is not None and retained.parent == tmp_path / "publisher-recovery"
        assert journal.parent == retained.parent
        assert (retained.read_bytes(), module._file_identity(retained.stat())) == (
            foreign_body,
            captured["foreign_identity"],
        )
        assert (paths["receipt"].read_bytes(), module._file_identity(paths["receipt"].stat())) == (
            occupant_body,
            captured["occupant_identity"],
        )
        assert (displaced.read_bytes(), module._file_identity(displaced.stat())) == captured[
            "original"
        ]
        record = json.loads(journal.read_bytes())
        assert record["state"] == "retained"
        assert record["recoveryPath"] == str(retained)
        assert record["expectedIdentity"] == list(captured["original"][1])
        assert record["observedIdentity"] == list(captured["foreign_identity"])
        assert not paths["report"].exists() and not paths["execution"].exists()
        assert not state["staged_report"].exists()
        for snapshot in close_snapshots.values():
            _assert_closed(snapshot["original"].descriptor)
            _assert_closed(snapshot["original"].parent_descriptor)
        _assert_no_raw_secret_archived(tmp_path, secret)
        notes = "\n".join(getattr(primary, "__notes__", ()))
        assert str(retained) in notes, "primary diagnostics lost the retained foreign receipt path"
        assert str(journal) in notes, "primary diagnostics lost the retained recovery journal path"


def test_generic_probe_link_then_error_discards_sensitive_staging_alias_without_archiving(
    tmp_path, monkeypatch
):
    module, _root, paths, arguments, state, secret = _generic_probe_case(tmp_path, monkeypatch)
    primary = OSError("injected raw report post-link failure")
    real_link = module._link_posix_file_descriptor
    raw_parent_identity = module._file_identity(paths["report"].parent.stat())
    linked = []
    close_snapshots = _observe_producer_pin_close(module, monkeypatch)

    def link_then_fail(original_fd, parent_fd, name):
        real_link(original_fd, parent_fd, name)
        if (
            name == paths["report"].name
            and module._file_identity(os.fstat(parent_fd)) == raw_parent_identity
            and not linked
        ):
            linked.append(module._file_identity(os.fstat(original_fd)))
            raise primary

    monkeypatch.setattr(module, "_link_posix_file_descriptor", link_then_fail)
    with publication(module, tmp_path):
        with pytest.raises(OSError) as caught:
            module.run_probe_receipt(**arguments)
        assert caught.value is primary
        assert len(linked) == 1
        assert not any(path.exists() for path in paths.values())
        assert not state["staged_report"].exists()
        snapshot = close_snapshots[state["staged_report"]]
        original = snapshot["original"]
        assert snapshot["identity"] == linked[0]
        assert snapshot["body"] == state["raw_body"]
        _assert_closed(original.descriptor)
        _assert_closed(original.parent_descriptor)
        _assert_no_raw_secret_archived(tmp_path, secret)
        assert snapshot["links"] == 0, "raw staging alias survived in archive"


@pytest.mark.parametrize(
    "completion",
    ("returncode7", "invalidbool", "TimeoutExpired", "KeyboardInterrupt", "SystemExit"),
)
def test_trusted_probe_completion_accepts_atomic_report_pin_before_failure_cleanup(
    tmp_path, monkeypatch, completion
):
    module, root, paths, arguments, state, secret = _generic_probe_case(tmp_path, monkeypatch)
    completion_errors = {
        "TimeoutExpired": subprocess.TimeoutExpired(["trusted-probe"], 300),
        "KeyboardInterrupt": KeyboardInterrupt("injected trusted probe interruption"),
        "SystemExit": SystemExit("injected trusted probe exit"),
    }
    body = json.dumps({"stdout": secret.decode(), "source": "trusted runner"}).encode()
    real_record = module._record_playwright_probe_failure
    accepted = []

    def publish_report_atomically(command, *, cwd, env, timeout):
        assert cwd == ROOT.resolve() and timeout == 300
        staged = Path(env["YFEISTAI_EVIDENCE_REPORT"])
        assert not staged.exists(), "runner report must not be precreated to obtain a pin"
        pending = staged.with_name(staged.name + ".runner-pending")
        pending.write_bytes(body)
        identity = module._file_identity(pending.stat())
        os.replace(pending, staged)
        state["staged_report"] = staged
        state["identity"] = identity
        assert module._file_identity(staged.stat()) == identity
        if completion in completion_errors:
            raise completion_errors[completion]
        return subprocess.CompletedProcess(command, 7 if completion == "returncode7" else True)

    def require_accepted_pin_before_cleanup(**kwargs):
        raw_report = kwargs["raw_report"]
        staged = raw_report[0] if isinstance(raw_report, tuple) else raw_report
        assert staged == state["staged_report"]
        attempt = module._require_receipt_attempt()
        assert staged in attempt.originals, "raw report must be accepted before cleanup starts"
        original = attempt.original(staged)
        assert module._file_identity(os.fstat(original.descriptor)) == state["identity"]
        assert os.pread(original.descriptor, len(body), 0) == body
        assert module._file_identity(os.fstat(original.parent_descriptor)) == module._file_identity(
            staged.parent.stat()
        )
        accepted.append(original)
        result = real_record(**kwargs)
        assert module._file_identity(os.fstat(original.descriptor)) == state["identity"]
        assert os.fstat(original.descriptor).st_nlink == 0
        assert os.pread(original.descriptor, len(body), 0) == body
        assert module._file_identity(os.fstat(original.parent_descriptor)) == module._file_identity(
            staged.parent.stat()
        )
        return result

    arguments["runner"] = publish_report_atomically
    monkeypatch.setattr(
        module, "_record_playwright_probe_failure", require_accepted_pin_before_cleanup
    )
    with publication(module, tmp_path):
        if completion in completion_errors:
            primary = completion_errors[completion]
            with pytest.raises(type(primary)) as caught:
                module.run_probe_receipt(**arguments)
            assert caught.value is primary
        else:
            message = (
                "probe native exit 7 does not prove passing evidence"
                if completion == "returncode7"
                else "probe native exit is invalid"
            )
            with pytest.raises(ValueError) as caught:
                module.run_probe_receipt(**arguments)
            assert str(caught.value) == message
        assert len(accepted) == 1
        _assert_closed(accepted[0].descriptor)
        _assert_closed(accepted[0].parent_descriptor)
        assert not state["staged_report"].exists()
        assert not any(path.exists() for path in paths.values())
        failure_paths = list((root / "failures" / "teacher_flow").glob("*/failure.json"))
        assert len(failure_paths) == 1
        failure = json.loads(failure_paths[0].read_bytes())
        assert failure["nativeExit"] == (7 if completion == "returncode7" else None)
        assert failure["artifacts"] == {}
        assert {path.name for path in failure_paths[0].parent.iterdir()} == {"failure.json"}
        _assert_no_raw_secret_archived(tmp_path, secret)
