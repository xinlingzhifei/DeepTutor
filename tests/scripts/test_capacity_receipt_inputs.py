from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.scripts import test_classroom_release_evidence as support
from tests.scripts.test_running_container_receipt_publication import _replacement_model

_INPUTS = (
    "deploy/image-lock.json",
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.data-plane.yml",
    "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
    "runtime/runtime-attestation.json",
)

_PROOF = "runtime/capacity-profile-attestation.json"
_OUTPUTS = (
    "artifacts/capacity_profile.json",
    "artifacts/learning_event_idempotency.json",
    _PROOF,
)


def _case(tmp_path, monkeypatch, *, phase=None, mutate=lambda: None):
    monkeypatch.setenv("YFEISTAI_OBSERVER_SIGNER_IMAGE", support.OBSERVER_SIGNER_IMAGE)
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "capacity-token")
    module = support._load_evidence_module()
    root, candidate = support._write_candidate_root(tmp_path)
    calls = []
    published = []

    def runner(command, **_kwargs):
        calls.append(command)
        stdout = (
            json.dumps(
                support._live_capacity_report(candidate), sort_keys=True, separators=(",", ":")
            )
            + "\n"
        )
        if phase == "runner-return":
            mutate()
        return subprocess.CompletedProcess(command, 0, stdout, "")

    original_publish = module._publish_no_replace

    def publish(staged, target):
        original_publish(staged, target)
        published.append(target.relative_to(root).as_posix())
        if phase == "proof-published" and target == root / _PROOF:
            mutate()

    monkeypatch.setattr(module, "_publish_no_replace", publish)
    arguments = {
        "candidate_root": root,
        "bundle_root": root,
        "release_run": support.RELEASE_RUN,
        "timeout_seconds": 600,
        "runner": runner,
    }
    return module, root, calls, published, arguments


def test_capacity_rejects_runtime_body_drift_after_real_proof_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mutate():
        runtime.write_bytes(runtime.read_bytes() + b"\n")

    module, root, calls, published, arguments = _case(
        tmp_path, monkeypatch, phase="proof-published", mutate=mutate
    )
    runtime = root / "runtime/runtime-attestation.json"
    with pytest.raises(ValueError, match="(?i)attestation|changed|binding"):
        module.write_capacity_profile_receipt(**arguments)
    assert len(calls) == 1
    assert published == list(_OUTPUTS)
    assert all(not (root / path).exists() for path in _OUTPUTS)


@pytest.mark.parametrize("phase", ("runner-return", "proof-published"))
@pytest.mark.parametrize("relative_input", _INPUTS)
def test_capacity_rejects_original_input_identity_replacement(
    tmp_path, monkeypatch, phase, relative_input
):
    mutation = None

    def mutate():
        assert mutation is not None
        mutation()

    module, root, calls, published, arguments = _case(
        tmp_path, monkeypatch, phase=phase, mutate=mutate
    )
    target = root / relative_input
    replacement = tmp_path / "same-byte-replacement"
    original_body = target.read_bytes()
    replacement.write_bytes(original_body)
    with _replacement_model(monkeypatch, target, replacement) as replace:
        mutation = replace
        with pytest.raises(ValueError, match="(?i)candidate|artifact|changed|binding|attestation"):
            module.write_capacity_profile_receipt(**arguments)
    assert len(calls) == 1
    assert published == ([] if phase == "runner-return" else list(_OUTPUTS))
    assert all(not (root / path).exists() for path in _OUTPUTS)
    assert target.read_bytes() == original_body


@pytest.mark.parametrize("failure", ("candidate-exit", "runtime-after-candidate-exit"))
def test_capacity_checks_normal_candidate_exit_before_committing_receipts(
    tmp_path, monkeypatch, failure
):
    module, root, calls, published, arguments = _case(tmp_path, monkeypatch)
    original_lease = module.validated_candidate_artifact_lease
    injected = []

    @contextmanager
    def lease(*args, **kwargs):
        with original_lease(*args, **kwargs) as artifact:
            yield artifact
        if (root / _PROOF).exists():
            injected.append(failure)
            if failure == "candidate-exit":
                raise ValueError("injected candidate exit failure")
            runtime = root / "runtime/runtime-attestation.json"
            runtime.write_bytes(runtime.read_bytes() + b"\n")

    monkeypatch.setattr(module, "validated_candidate_artifact_lease", lease)
    with pytest.raises(ValueError, match="(?i)candidate|attestation|changed|binding"):
        module.write_capacity_profile_receipt(**arguments)
    assert len(calls) == 1
    assert published == list(_OUTPUTS)
    assert injected == [failure]
    assert all(not (root / path).exists() for path in _OUTPUTS)


@pytest.mark.parametrize("directory", ("runtime", "artifacts"), ids=("input", "output"))
def test_capacity_directory_close_failure_retracts_owned_receipts(tmp_path, monkeypatch, directory):
    module, root, _calls, published, arguments = _case(tmp_path, monkeypatch)
    real_close = module._close_receipt_input_directory
    injected = []

    def close_then_fail(boundary):
        real_close(boundary)
        if boundary.root == root / directory and (root / _PROOF).exists() and not injected:
            injected.append(directory)
            assert not boundary.leases
            raise OSError("injected capacity directory failure after real close")

    monkeypatch.setattr(module, "_close_receipt_input_directory", close_then_fail)
    with pytest.raises(OSError, match="injected capacity directory failure after real close"):
        module.write_capacity_profile_receipt(**arguments)
    assert published == list(_OUTPUTS)
    assert injected == [directory]
    assert all(not (root / path).exists() for path in _OUTPUTS)


def test_capacity_runtime_file_close_failure_retracts_owned_receipts(tmp_path, monkeypatch):
    module, root, _calls, published, arguments = _case(tmp_path, monkeypatch)
    original_open = module._open_classroom_artifact_relative
    handles = []

    class FailAfterRealClose:
        def __init__(self, handle):
            self.handle = handle
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            self.close_calls += 1
            assert self.close_calls == 1
            assert (root / _PROOF).exists()
            self.handle.close()
            raise OSError("injected capacity runtime file failure after real close")

    def open_input(directory, name, **kwargs):
        handle = original_open(directory, name, **kwargs)
        if directory.path == root / "runtime" and name == "runtime-attestation.json":
            handle = FailAfterRealClose(handle)
            handles.append(handle)
        return handle

    monkeypatch.setattr(module, "_open_classroom_artifact_relative", open_input)
    with pytest.raises(OSError, match="injected capacity runtime file failure after real close"):
        module.write_capacity_profile_receipt(**arguments)
    assert published == list(_OUTPUTS)
    assert len(handles) == 1
    assert handles[0].handle.closed
    assert handles[0].close_calls == 1
    assert all(not (root / path).exists() for path in _OUTPUTS)


def test_capacity_compound_input_close_preserves_first_error_and_owned_boundaries(
    tmp_path, monkeypatch
):
    module, root, _calls, published, arguments = _case(tmp_path, monkeypatch)
    foreign = root / "artifacts" / "foreign-receipt.json"
    foreign.parent.mkdir()
    foreign.write_bytes(b"unrelated receipt must survive compound input close failure")
    protected = {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
        for path in (*(root / name for name in _INPUTS), foreign)
    }
    original_open = module._open_classroom_artifact_relative
    original_close_directory = module._close_receipt_input_directory
    file_error = OSError("primary capacity runtime file failure after real close")
    directory_error = OSError("secondary capacity runtime directory failure after real close")
    events = []
    handles = []
    directories = []

    class FailAfterRealClose:
        def __init__(self, handle):
            self.handle = handle
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            self.close_calls += 1
            assert self.close_calls == 1, "do not retry a successfully closed input handle"
            assert (root / _PROOF).exists()
            self.handle.close()
            assert self.handle.closed
            events.append("file-closed")
            raise file_error

    def open_input(directory, name, **kwargs):
        handle = original_open(directory, name, **kwargs)
        if directory.path == root / "runtime" and name == "runtime-attestation.json":
            handle = FailAfterRealClose(handle)
            handles.append(handle)
        return handle

    def close_directory(boundary):
        original_close_directory(boundary)
        if boundary.root == root / "runtime" and (root / _PROOF).exists():
            assert events == ["file-closed"]
            assert not boundary.leases
            directories.append(boundary)
            events.append("directory-closed")
            raise directory_error

    monkeypatch.setattr(module, "_open_classroom_artifact_relative", open_input)
    monkeypatch.setattr(module, "_close_receipt_input_directory", close_directory)
    with pytest.raises(OSError) as caught:
        module.write_capacity_profile_receipt(**arguments)

    assert events == ["file-closed", "directory-closed"]
    assert len(handles) == 1 and handles[0].handle.closed and handles[0].close_calls == 1
    assert len(directories) == 1 and not directories[0].leases
    assert published == list(_OUTPUTS)
    assert all(not (root / path).exists() for path in _OUTPUTS)
    assert not list(root.rglob("*.staging"))
    assert {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino) for path in protected
    } == protected
    assert caught.value is file_error
    assert any(str(directory_error) in note for note in getattr(caught.value, "__notes__", ()))


@pytest.mark.skipif(os.name != "nt", reason="Windows native directory handle close")
def test_capacity_native_input_close_failure_retries_owned_handle(tmp_path, monkeypatch):
    module, root, _calls, published, arguments = _case(tmp_path, monkeypatch)
    original_close = module._close_image_receipt_boundary
    native_close = module._close_windows_handle
    closing_input = False
    outcomes = []
    boundaries = []

    def close_native(handle):
        if closing_input and not outcomes:
            outcomes.append("failed-before-close")
            raise OSError("injected native capacity input close failure")
        native_close(handle)
        if closing_input:
            outcomes.append("closed")

    def close_directory(boundary):
        nonlocal closing_input
        closing_input = boundary.root == root / "runtime" and (root / _PROOF).exists()
        if closing_input:
            boundaries.append(boundary)
        try:
            original_close(boundary)
        finally:
            closing_input = False

    monkeypatch.setattr(module, "_close_image_receipt_boundary", close_directory)
    monkeypatch.setattr(module, "_close_windows_handle", close_native)
    with pytest.raises(OSError, match="injected native capacity input close failure"):
        module.write_capacity_profile_receipt(**arguments)
    assert outcomes == ["failed-before-close", "closed"]
    assert len(boundaries) == 2 and boundaries[0] is boundaries[1]
    assert not boundaries[0].leases
    assert published == list(_OUTPUTS)
    assert all(not (root / path).exists() for path in _OUTPUTS)


@pytest.mark.parametrize(
    "failure_mode", ("normal-output", "publication-input", "publication-output")
)
def test_capacity_compound_cleanup_retains_primary_and_all_diagnostics(
    tmp_path, monkeypatch, failure_mode
):
    module, root, _calls, published, arguments = _case(tmp_path, monkeypatch)
    foreign = root / "artifacts" / "foreign-receipt.json"
    foreign.parent.mkdir()
    foreign.write_bytes(b"unrelated receipt must survive grouped cleanup failure")
    protected = {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
        for path in (*(root / name for name in _INPUTS), foreign)
    }
    original_open = module._open_classroom_artifact_relative
    original_close_directory = module._close_receipt_input_directory
    original_publish = module._publish_no_replace
    publication_error = OSError("primary capacity publication failure")
    first_close_error = OSError("first capacity resource failure after real close")
    second_close_error = OSError("second capacity resource failure after real close")
    input_failure = failure_mode == "publication-input"
    events = []
    handles = []
    close_counts = {}
    closed_directories = []

    class FailAfterRealClose:
        def __init__(self, handle):
            self.handle = handle
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            self.close_calls += 1
            assert self.close_calls == 1
            self.handle.close()
            assert self.handle.closed
            events.append("file-closed")
            raise first_close_error

    def open_input(directory, name, **kwargs):
        handle = original_open(directory, name, **kwargs)
        if directory.path == root / "runtime" and name == "runtime-attestation.json":
            handle = FailAfterRealClose(handle)
            handles.append(handle)
        return handle

    def close_directory(boundary):
        close_counts[id(boundary)] = close_counts.get(id(boundary), 0) + 1
        assert close_counts[id(boundary)] == 1, "do not reclose a released directory"
        original_close_directory(boundary)
        assert not boundary.leases
        if not input_failure and boundary.root == root / "artifacts":
            assert not events
            closed_directories.append(boundary)
            events.append("artifacts-closed")
            raise first_close_error
        first_event = "file-closed" if input_failure else "artifacts-closed"
        if boundary.root == root / "runtime" and events == [first_event]:
            closed_directories.append(boundary)
            events.append("runtime-closed")
            raise second_close_error

    def publish_then_fail(staged, target):
        original_publish(staged, target)
        if target == root / _PROOF:
            raise publication_error

    if input_failure:
        monkeypatch.setattr(module, "_open_classroom_artifact_relative", open_input)
    if failure_mode != "normal-output":
        monkeypatch.setattr(module, "_publish_no_replace", publish_then_fail)
    monkeypatch.setattr(module, "_close_receipt_input_directory", close_directory)
    with pytest.raises(OSError) as caught:
        module.write_capacity_profile_receipt(**arguments)

    assert events == ["file-closed" if input_failure else "artifacts-closed", "runtime-closed"]
    assert len(closed_directories) == (1 if input_failure else 2)
    assert all(count == 1 for count in close_counts.values())
    if input_failure:
        assert len(handles) == 1 and handles[0].handle.closed and handles[0].close_calls == 1
    assert published == list(_OUTPUTS)
    assert all(not (root / path).exists() for path in _OUTPUTS)
    assert not list(root.rglob("*.staging"))
    assert {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino) for path in protected
    } == protected
    expected_primary = first_close_error if failure_mode == "normal-output" else publication_error
    assert caught.value is expected_primary
    diagnostics = "\n".join(getattr(caught.value, "__notes__", ()))
    if failure_mode != "normal-output":
        assert str(first_close_error) in diagnostics
    assert str(second_close_error) in diagnostics


@pytest.mark.parametrize("relative_output", _OUTPUTS)
def test_capacity_compensates_real_native_link_that_succeeds_before_raising(
    tmp_path, monkeypatch, relative_output
):
    module, root, _calls, _published, arguments = _case(tmp_path, monkeypatch)
    helper = "_link_windows_file_relative" if os.name == "nt" else "_link_posix_file_descriptor"
    original_link = getattr(module, helper)
    linked = []

    def link_then_fail(source, directory, name):
        original_link(source, directory, name)
        if name == Path(relative_output).name:
            target = root / relative_output
            assert target.is_file()
            linked.append(target.stat().st_ino)
            raise OSError("injected error after real native capacity link")

    monkeypatch.setattr(module, helper, link_then_fail)
    with pytest.raises(OSError, match="injected error after real native capacity link"):
        module.write_capacity_profile_receipt(**arguments)
    assert len(linked) == 1
    assert all(not (root / path).exists() for path in _OUTPUTS)
    assert not list(root.rglob("*.staging"))


@pytest.mark.parametrize("relative_output", _OUTPUTS)
def test_capacity_compensates_publication_that_succeeds_before_raising(
    tmp_path, monkeypatch, relative_output
):
    module, root, _calls, published, arguments = _case(tmp_path, monkeypatch)
    original_publish = module._publish_no_replace
    linked = []

    def publish_then_fail(staged, target):
        original_publish(staged, target)
        if target == root / relative_output:
            assert target.stat().st_ino == staged.stat().st_ino
            linked.append(target)
            raise OSError("injected error after real capacity publication")

    monkeypatch.setattr(module, "_publish_no_replace", publish_then_fail)
    with pytest.raises(OSError, match="injected error after real capacity publication"):
        module.write_capacity_profile_receipt(**arguments)
    assert linked == [root / relative_output]
    assert published == list(_OUTPUTS[: _OUTPUTS.index(relative_output) + 1])
    assert all(not (root / path).exists() for path in _OUTPUTS)
    assert not list(root.rglob("*.staging"))


@pytest.mark.parametrize("relative_output", _OUTPUTS)
def test_capacity_preserves_foreign_output_during_input_failure(
    tmp_path, monkeypatch, relative_output
):
    def mutate():
        os.replace(foreign, root / relative_output)
        runtime = root / "runtime/runtime-attestation.json"
        runtime.write_bytes(runtime.read_bytes() + b"\n")

    module, root, _calls, published, arguments = _case(
        tmp_path, monkeypatch, phase="proof-published", mutate=mutate
    )
    foreign = tmp_path / "foreign-receipt"
    body = b"unrelated competing receipt"
    foreign.write_bytes(body)
    identity = foreign.stat().st_ino
    with pytest.raises(ValueError, match="attestation|changed|binding"):
        module.write_capacity_profile_receipt(**arguments)
    assert published == list(_OUTPUTS)
    target = root / relative_output
    assert target.stat().st_ino == identity
    assert target.read_bytes() == body
    assert all(not (root / path).exists() for path in _OUTPUTS if path != relative_output)
    assert all(path.read_bytes() != body for path in (root / "failures").rglob("*.json"))


def test_capacity_preserves_foreign_staging_during_input_failure(tmp_path, monkeypatch):
    module, root, _calls, _published, arguments = _case(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign-staging"
    body = b"unrelated competing staging"
    foreign.write_bytes(body)
    identity = foreign.stat().st_ino
    original_remove = module._remove_manifest_staging
    replacements = []

    def replace_after_staging_close(boundary, staged, **kwargs):
        # Native Windows staging leases deny replacement while open. Race at the
        # first real closed-handle removal boundary, without bypassing ownership.
        if staged.name.startswith(".capacity_profile.json.") and not replacements:
            os.replace(foreign, staged)
            replacements.append(staged)
            runtime = root / "runtime/runtime-attestation.json"
            runtime.write_bytes(runtime.read_bytes() + b"\n")
        return original_remove(boundary, staged, **kwargs)

    monkeypatch.setattr(module, "_remove_manifest_staging", replace_after_staging_close)
    # POSIX now detects the foreign replacement during recovery itself, before
    # the later input check; the competing bytes must still remain untouched.
    with pytest.raises(
        (ValueError, RuntimeError),
        match="attestation|changed|binding|foreign receipt was preserved",
    ):
        module.write_capacity_profile_receipt(**arguments)
    assert len(replacements) == 1
    assert replacements[0].stat().st_ino == identity
    assert replacements[0].read_bytes() == body
    assert all(not (root / path).exists() for path in _OUTPUTS)
    assert all(path.read_bytes() != body for path in (root / "failures").rglob("*.json"))


def test_capacity_archive_failure_does_not_mask_publication_error(tmp_path, monkeypatch):
    module, _root, _calls, _published, arguments = _case(tmp_path, monkeypatch)

    def fail_publication(*_args):
        raise OSError("primary capacity publication failure")

    def fail_archive(**_kwargs):
        raise OSError("secondary capacity archive failure")

    monkeypatch.setattr(module, "_publish_no_replace", fail_publication)
    monkeypatch.setattr(module, "_record_probe_failure", fail_archive)
    with pytest.raises(OSError, match="primary capacity publication failure") as caught:
        module.write_capacity_profile_receipt(**arguments)
    assert any("secondary capacity archive failure" in note for note in caught.value.__notes__)
