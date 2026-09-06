from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.scripts import test_running_container_receipt_publication as runtime_support
from tests.scripts import test_source_head_receipt_publication as source_support


def _snapshot(path: Path) -> tuple[bytes, tuple[int, int]]:
    state = path.stat()
    return path.read_bytes(), (state.st_dev, state.st_ino)


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, producer: str, *, invalid: bool):
    if producer == "source":
        module, _root, output, arguments, sentinel, state, _calls = source_support._case(tmp_path)
        if invalid:
            state["status"] = " M uncommitted-source.py"
        return (
            module,
            module.write_source_head_receipt,
            arguments,
            Path(arguments["source_root"]),
            output,
            sentinel,
            {sentinel: _snapshot(sentinel)},
        )
    module, _root, _candidate, output, attestation, sentinel, arguments = runtime_support._case(
        tmp_path, monkeypatch
    )
    if invalid:
        document = json.loads(attestation.read_bytes())
        document["afterSnapshot"] = []
        attestation.write_bytes(module._json_bytes(document))
    return (
        module,
        module.write_running_containers_receipt,
        arguments,
        attestation.parent,
        output,
        sentinel,
        {path: _snapshot(path) for path in (sentinel, attestation)},
    )


@pytest.mark.parametrize("producer", ("source", "runtime"))
@pytest.mark.parametrize(
    "failure_mode",
    (
        pytest.param(
            "native-before-close",
            marks=pytest.mark.skipif(os.name != "nt", reason="Windows native CloseHandle failure"),
        ),
        "after-real-close",
        pytest.param(
            "validation-and-native-close",
            marks=pytest.mark.skipif(os.name != "nt", reason="Windows native CloseHandle failure"),
        ),
    ),
)
def test_receipt_input_directory_close_failure_preserves_ownership_and_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    producer: str,
    failure_mode: str,
) -> None:
    invalid = failure_mode == "validation-and-native-close"
    module, write, arguments, input_root, output, sentinel, protected = _case(
        tmp_path, monkeypatch, producer, invalid=invalid
    )
    close_directory = module._close_image_receipt_boundary
    native_close = module._close_windows_handle
    boundaries = []
    directory_calls = []
    native_calls = []
    closing_input = False
    native_failed = False
    after_close_failed = False

    def native_close_once(handle):
        nonlocal native_failed
        if closing_input:
            if failure_mode != "after-real-close" and not native_failed:
                native_failed = True
                native_calls.append("failed-before-close")
                raise OSError("injected receipt input native close failure")
            native_close(handle)
            native_calls.append("closed")
            return
        native_close(handle)

    def close_input(boundary):
        nonlocal closing_input, after_close_failed
        if boundary.root != input_root:
            return close_directory(boundary)
        if not boundaries:
            boundaries.append(boundary)
        assert boundaries == [boundary]
        directory_calls.append(output.exists())
        closing_input = True
        try:
            close_directory(boundary)
            if failure_mode == "after-real-close" and not after_close_failed:
                after_close_failed = True
                assert boundary.leases == {}, "the real close must already release ownership"
                raise OSError("injected receipt input failure after real close")
        finally:
            closing_input = False

    monkeypatch.setattr(module, "_close_image_receipt_boundary", close_input)
    if os.name == "nt":
        monkeypatch.setattr(module, "_close_windows_handle", native_close_once)
    if invalid:
        primary_message = "Git worktree is not clean" if producer == "source" else "attestation"
        with pytest.raises(ValueError, match=primary_message) as captured:
            write(**arguments)
        assert any(
            "receipt input cleanup failed" in note
            and "injected receipt input native close failure" in note
            for note in getattr(captured.value, "__notes__", ())
        ), "cleanup diagnostics must not replace the real validation failure"
    else:
        message = (
            "injected receipt input native close failure"
            if failure_mode == "native-before-close"
            else "injected receipt input failure after real close"
        )
        with pytest.raises(OSError, match=message):
            write(**arguments)
    assert len(boundaries) == 1
    assert boundaries[0].leases == {}, "an ExitStack callback cannot abandon its owned input"
    if failure_mode == "after-real-close":
        assert directory_calls == [True], "never retry a released directory handle"
        assert after_close_failed
        if os.name == "nt":
            assert native_calls == ["closed"]
    else:
        assert directory_calls == [not invalid, not invalid]
        assert native_failed
        assert native_calls == ["failed-before-close", "closed"]
    assert list(output.parent.iterdir()) == [sentinel]
    assert {path: _snapshot(path) for path in protected} == protected


def test_runtime_receipt_input_file_close_failure_retracts_publication_and_closes_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, write, arguments, input_root, output, sentinel, protected = _case(
        tmp_path, monkeypatch, "runtime", invalid=False
    )
    open_file = module._open_classroom_artifact_relative
    close_directory = module._close_receipt_input_directory
    files = []
    directories = []

    class FailAfterRealClose:
        def __init__(self, handle):
            self.handle = handle
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            self.close_calls += 1
            assert self.close_calls == 1, "the original file handle must not be closed again"
            assert output.exists(), "inject only after the real receipt has been published"
            self.handle.close()
            assert self.handle.closed
            raise OSError("injected runtime input file failure after real close")

    def open_input(parent, name, **kwargs):
        handle = open_file(parent, name, **kwargs)
        if name != "runtime-attestation.json":
            return handle
        wrapped = FailAfterRealClose(handle)
        files.append(wrapped)
        return wrapped

    def close_input_directory(boundary):
        close_directory(boundary)
        if boundary.root == input_root:
            directories.append(boundary)

    monkeypatch.setattr(module, "_open_classroom_artifact_relative", open_input)
    monkeypatch.setattr(module, "_close_receipt_input_directory", close_input_directory)
    with pytest.raises(OSError, match="injected runtime input file failure after real close"):
        write(**arguments)
    assert len(files) == 1
    assert files[0].close_calls == 1
    assert files[0].handle.closed
    assert len(directories) == 1
    assert directories[0].leases == {}
    assert list(output.parent.iterdir()) == [sentinel]
    assert {path: _snapshot(path) for path in protected} == protected
