from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import sys

import pytest

from tests.scripts.test_classroom_release_evidence import (
    RELEASE_RUN,
    _load_evidence_module,
    _write_candidate_root,
)
from tests.scripts.test_source_head_receipt_publication import _case as source_case

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="requires isolated native Linux publisher fixtures below trusted ancestors",
)


def publication(module, tmp_path):
    recovery = tmp_path / "publisher-recovery"
    recovery.mkdir(mode=0o700, exist_ok=True)
    assert hasattr(module, "receipt_publication"), "publication attempt policy/pins are missing"
    return module.receipt_publication(recovery_root=recovery, worker_uid=1000)


def test_source_producer_requires_policy_before_git_or_output(tmp_path):
    module, _root, output, arguments, sentinel, _state, calls = source_case(tmp_path)
    before = sentinel.read_bytes()
    with pytest.raises(ValueError, match="receipt publication policy"):
        module.write_source_head_receipt(**arguments)
    assert calls == []
    assert not output.exists()
    assert sentinel.read_bytes() == before


@pytest.mark.parametrize("cleanup", ("manifest", "classroom"))
@pytest.mark.parametrize("replace", (False, True), ids=("owned", "foreign"))
def test_cleanup_uses_original_pin_after_producer_handle_and_boundary_close(
    tmp_path, monkeypatch, cleanup, replace
):
    module = _load_evidence_module()
    root = tmp_path / "bundle"
    for name in ("raw", "runtime", "deploy"):
        (root / name).mkdir(parents=True)
    with publication(module, tmp_path):
        if cleanup == "manifest":
            boundary = module._ManifestPublicationBoundary.open(root)
            parent_key = "bundle"
        else:
            boundary = module._GatewayPublicationBoundary.open(root, root)
            boundary.ensure_publication_parents()
            parent_key = "runtime"
        target = boundary.leases[parent_key].path / "receipt.json"
        handle, identity, body = module._create_classroom_json_staging(
            boundary, parent_key=parent_key, path=target, document={"original": True}
        )
        handle.close()
        if cleanup == "manifest":
            boundary.close()
            boundary = module._ManifestPublicationBoundary.open(root)
        recovery = sys.modules["receipt_recovery"]
        rename = recovery._rename_no_replace
        displaced = target.with_name("original-backup.json")
        injected = []

        def swap_then_detach(source_fd, source, destination_fd, destination):
            if replace and source == target.name and not injected:
                os.rename(target, displaced)
                target.write_bytes(b"foreign receipt")
                injected.append((target.stat().st_dev, target.stat().st_ino))
            return rename(source_fd, source, destination_fd, destination)

        monkeypatch.setattr(recovery, "_rename_no_replace", swap_then_detach)
        error = None
        try:
            if cleanup == "manifest":
                module._remove_manifest_staging(boundary, target, expected_identity=identity)
            else:
                module._remove_classroom_entries(boundary, {target: identity}, label="receipt")
        except (OSError, ValueError, RuntimeError) as exc:
            error = exc
        finally:
            boundary.close()
        if replace:
            assert injected, "must reach private detach boundary"
            assert error is not None
            assert target.read_bytes() == b"foreign receipt"
            assert (target.stat().st_dev, target.stat().st_ino) == injected[0]
            assert displaced.read_bytes() == body
        else:
            assert error is None
            assert not target.exists()
        records = [
            json.loads(path.read_bytes())
            for path in (tmp_path / "publisher-recovery").glob("*.recovery.json")
        ]
        assert records[-1]["state"] == ("restored" if replace else "removed")


@pytest.mark.parametrize("cleanup", ("manifest", "classroom"))
def test_unpinned_identity_cannot_authorize_cleanup(tmp_path, cleanup):
    module = _load_evidence_module()
    root = tmp_path / "bundle"
    for name in ("raw", "runtime", "deploy"):
        (root / name).mkdir(parents=True)
    with publication(module, tmp_path):
        if cleanup == "manifest":
            boundary = module._ManifestPublicationBoundary.open(root)
            target = root / "unknown.json"
        else:
            boundary = module._GatewayPublicationBoundary.open(root, root)
            boundary.ensure_publication_parents()
            target = root / "runtime" / "unknown.json"
        target.write_bytes(b"not owned by this attempt")
        identity = module._file_identity(target.stat())
        try:
            with pytest.raises((ValueError, RuntimeError), match="pin|removed"):
                if cleanup == "manifest":
                    module._remove_manifest_staging(boundary, target, expected_identity=identity)
                else:
                    module._remove_classroom_entries(boundary, {target: identity}, label="receipt")
        finally:
            boundary.close()
        assert target.read_bytes() == b"not owned by this attempt"
        assert not list((tmp_path / "publisher-recovery").iterdir())


def test_staging_write_failure_keeps_original_pin_through_cleanup(tmp_path, monkeypatch):
    module = _load_evidence_module()
    root = tmp_path / "bundle"
    root.mkdir()
    target = root / "staged.json"
    with publication(module, tmp_path):
        boundary = module._ManifestPublicationBoundary.open(root)
        write = os.write

        def fail_receipt_write(fd, body):
            if body.startswith(b"{\n"):
                raise OSError(errno.ENOSPC, "injected receipt write failure")
            return write(fd, body)

        monkeypatch.setattr(os, "write", fail_receipt_write)
        try:
            with pytest.raises(OSError, match="injected receipt write"):
                module._create_classroom_json_staging(
                    boundary, parent_key="bundle", path=target, document={"original": True}
                )
        finally:
            boundary.close()
        assert not target.exists()
        assert list((tmp_path / "publisher-recovery").glob("*.recovery.json"))


@pytest.mark.parametrize("replace", (False, True), ids=("owned", "foreign"))
def test_source_receipt_late_directory_close_preserves_attempt_pin(tmp_path, monkeypatch, replace):
    module, _root, output, arguments, sentinel, _state, _calls = source_case(tmp_path)
    close = module._close_image_receipt_boundary
    displaced = output.with_name("displaced.json")
    injected = []

    def close_then_fail(boundary):
        close(boundary)
        if not injected and output.exists():
            injected.append(True)
            if replace:
                os.rename(output, displaced)
                output.write_bytes(b"other publisher")
            raise OSError("injected final directory close")

    monkeypatch.setattr(module, "_close_image_receipt_boundary", close_then_fail)
    with publication(module, tmp_path):
        with pytest.raises(OSError, match="injected final directory close"):
            module.write_source_head_receipt(**arguments)
    assert injected
    assert sentinel.read_bytes() == b"preserved input"
    if replace:
        assert output.read_bytes() == b"other publisher"
        assert json.loads(displaced.read_bytes())["evidence"] == "source_head"
    else:
        assert not output.exists()


@pytest.mark.parametrize("kind", ("json", "public-key"))
def test_temporary_file_pin_survives_close_and_native_publish_then_error(
    tmp_path, monkeypatch, kind
):
    module = _load_evidence_module()
    target = tmp_path / "receipt.json"
    owned = {}
    publish = module._publish_no_replace

    def publish_then_fail(source, destination):
        publish(source, destination)
        raise OSError("injected post-link error")

    monkeypatch.setattr(module, "_publish_no_replace", publish_then_fail)
    with publication(module, tmp_path):
        with pytest.raises(OSError, match="injected post-link"):
            if kind == "json":
                module._atomic_write_json_no_replace(
                    target, {"original": True}, owned_publications=owned
                )
            else:
                module._publish_release_supervisor_public_key(
                    target, b"public key", owned_publications=owned
                )
        assert target in owned, "ownership must be registered before native publication"
        boundary = module._ManifestPublicationBoundary.open(tmp_path)
        try:
            module._remove_manifest_staging(boundary, target, expected_identity=owned[target])
        finally:
            boundary.close()
        assert not target.exists()


def test_successful_source_publication_closes_attempt_pins(tmp_path):
    module, _root, output, arguments, _sentinel, _state, _calls = source_case(tmp_path)
    before = set(os.listdir("/proc/self/fd"))
    with publication(module, tmp_path):
        result = module.write_source_head_receipt(**arguments)
    assert result == json.loads(output.read_bytes())
    assert not list(output.parent.glob("*.tmp"))
    assert set(os.listdir("/proc/self/fd")) == before


def test_public_producer_retry_uses_fresh_original_pins(tmp_path, monkeypatch):
    module, _root, output, arguments, _sentinel, _state, _calls = source_case(tmp_path)
    close = module._close_image_receipt_boundary
    failed = []

    def close_then_fail_once(boundary):
        close(boundary)
        if not failed and output.exists():
            failed.append(True)
            raise OSError("first publication close failed")

    monkeypatch.setattr(module, "_close_image_receipt_boundary", close_then_fail_once)
    with publication(module, tmp_path):
        with pytest.raises(OSError, match="first publication close failed"):
            module.write_source_head_receipt(**arguments)
        assert not output.exists()
        result = module.write_source_head_receipt(**arguments)
        assert json.loads(output.read_bytes()) == result


def test_existing_pin_parent_close_is_never_retried(tmp_path, monkeypatch):
    module = _load_evidence_module()
    root = tmp_path / "bundle"
    root.mkdir()
    with publication(module, tmp_path):
        boundary = module._ManifestPublicationBoundary.open(root)
        path = root / "staged.json"
        handle, _identity, _body = module._create_classroom_json_staging(
            boundary, parent_key="bundle", path=path, document={"original": True}
        )
        parent = int(boundary.leases["bundle"].handle)
        duplicate, close = os.dup, os.close
        parents, closes = [], []
        primary = OSError(errno.EIO, "injected post-close error")

        def track_duplicate(descriptor):
            result = duplicate(descriptor)
            if descriptor == parent:
                parents.append(result)
            return result

        def fail_after_close(descriptor):
            if descriptor in parents:
                closes.append(descriptor)
                close(descriptor)
                raise primary
            close(descriptor)

        with monkeypatch.context() as injection:
            injection.setattr(os, "dup", track_duplicate)
            injection.setattr(os, "close", fail_after_close)
            with pytest.raises(OSError) as caught:
                module._pin_receipt(path, handle.fileno(), parent_fd=parent)
            assert caught.value is primary
            assert closes == parents, "a native close must never be retried"
        handle.close()
        boundary.close()


def test_cli_admits_explicit_policy_and_publishes_image_receipt(tmp_path):
    module = _load_evidence_module()
    root, _candidate = _write_candidate_root(tmp_path, include_runtime_attestation=False)
    recovery = tmp_path / "recovery"
    recovery.mkdir(mode=0o700)
    output = tmp_path / "receipts" / "images.json"
    result = module.main(
        [
            "--receipt-recovery-root",
            str(recovery),
            "--receipt-worker-uid",
            "1000",
            "image-digests",
            "--output",
            str(output),
            "--candidate-root",
            str(root),
            "--run-id",
            RELEASE_RUN["runId"],
            "--environment-id",
            RELEASE_RUN["environmentId"],
            "--observed-at",
            "2026-09-06T00:00:00Z",
        ]
    )
    assert result == 0
    assert json.loads(output.read_bytes())["evidence"] == "image_digests"


def test_original_parent_pin_rejects_rebinding_to_replaced_directory(tmp_path):
    module = _load_evidence_module()
    root = tmp_path / "bundle"
    root.mkdir()
    with publication(module, tmp_path):
        first = module._ManifestPublicationBoundary.open(root)
        handle, _identity, _body = module._create_classroom_json_staging(
            first, parent_key="bundle", path=root / "staged.json", document={"original": True}
        )
        target = root / "receipt.json"
        module._publish_manifest_no_replace(first, target, source_handle=handle)
        os.rename(root, tmp_path / "old-bundle")
        root.mkdir()
        second = module._ManifestPublicationBoundary.open(root)
        try:
            with pytest.raises(ValueError, match="parent.*changed"):
                module._publish_manifest_no_replace(second, target, source_handle=handle)
            assert not target.exists()
        finally:
            handle.close()
            first.close()
            second.close()


def test_late_close_parent_rotation_retracts_through_original_directory(tmp_path, monkeypatch):
    module, _root, output, arguments, _sentinel, _state, _calls = source_case(tmp_path)
    close = module._close_image_receipt_boundary
    displaced = tmp_path / "old-output-directory"
    injected = []

    def close_then_rotate(boundary):
        close(boundary)
        if not injected:
            injected.append(True)
            os.rename(output.parent, displaced)
            output.parent.mkdir()
            output.write_bytes(b"other directory receipt")
            raise OSError("injected directory rotation")

    monkeypatch.setattr(module, "_close_image_receipt_boundary", close_then_rotate)
    with publication(module, tmp_path):
        with pytest.raises(OSError, match="injected directory rotation"):
            module.write_source_head_receipt(**arguments)
    assert output.read_bytes() == b"other directory receipt"
    assert not (displaced / output.name).exists()
    assert (displaced / "unrelated.json").read_bytes() == b"preserved input"


def test_backup_receipt_native_link_then_error_retracts_output(tmp_path, monkeypatch):
    module = _load_evidence_module()
    root = tmp_path / "bundle"
    for name in ("raw", "runtime", "deploy"):
        (root / name).mkdir(parents=True)
    link = module._link_posix_file_descriptor

    def link_then_fail(*args, **kwargs):
        link(*args, **kwargs)
        raise OSError("injected backup post-link error")

    monkeypatch.setattr(module, "_link_posix_file_descriptor", link_then_fail)
    with publication(module, tmp_path):
        boundary = module._GatewayPublicationBoundary.open(root, root)
        boundary.ensure_publication_parents()
        try:
            with pytest.raises(OSError, match="injected backup post-link"):
                module._publish_backup_restore_receipt(boundary, {"backup": True})
        finally:
            boundary.close()
        assert not (root / "artifacts" / "backup_restore.json").exists()


@pytest.mark.parametrize("operation", ("archive", "discard"))
def test_archive_retracts_only_the_original_pinned_receipt(tmp_path, monkeypatch, operation):
    module = _load_evidence_module()
    root = tmp_path / "bundle"
    root.mkdir()
    archive = root / "archive"
    archive.mkdir()
    target = root / "receipt.json"
    with publication(module, tmp_path):
        boundary = module._ManifestPublicationBoundary.open(root)
        handle, identity, body = module._create_classroom_json_staging(
            boundary, parent_key="bundle", path=target, document={"original": True}
        )
        handle.close()
        archive_fd = os.open(archive, os.O_RDONLY | os.O_DIRECTORY)
        recovery = sys.modules["receipt_recovery"]
        rename = recovery._rename_no_replace
        injected = []

        def swap_then_detach(source_fd, source, destination_fd, destination):
            if source == target.name and not injected:
                os.rename(target, root / "original-backup")
                target.write_bytes(b"foreign archive input")
                injected.append(True)
            return rename(source_fd, source, destination_fd, destination)

        monkeypatch.setattr(recovery, "_rename_no_replace", swap_then_detach)
        try:
            with pytest.raises((RuntimeError, ValueError), match="foreign|preserved"):
                if operation == "archive":
                    module._relocate_posix_bundle_file(
                        target,
                        bundle_root=root,
                        bundle_fd=int(boundary.leases["bundle"].handle),
                        target_fd=archive_fd,
                        target_name="original.json",
                        expected_identity=identity,
                    )
                else:
                    module._discard_posix_bundle_file(
                        target,
                        bundle_root=root,
                        bundle_fd=int(boundary.leases["bundle"].handle),
                        expected_identity=identity,
                    )
        finally:
            os.close(archive_fd)
            boundary.close()
        assert injected
        assert target.read_bytes() == b"foreign archive input"
        if operation == "archive":
            assert (archive / "original.json").read_bytes() == body
