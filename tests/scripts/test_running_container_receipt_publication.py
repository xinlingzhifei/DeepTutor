from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path

import pytest

from tests.scripts import test_classroom_release_evidence as support

_CANDIDATE_INPUTS = (
    "deploy/image-lock.json",
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.data-plane.yml",
    "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
)
_PHASES = ("receipt-built", "receipt-published")


def _snapshot(path: Path) -> tuple[bytes, tuple[int, int]]:
    state = path.stat()
    return path.read_bytes(), (state.st_dev, state.st_ino)


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("YFEISTAI_OBSERVER_SIGNER_IMAGE", support.OBSERVER_SIGNER_IMAGE)
    module = support._load_evidence_module()
    root, candidate = support._write_candidate_root(tmp_path)
    output = root / "artifacts" / "running_containers.json"
    output.parent.mkdir()
    sentinel = output.parent / "unrelated-evidence.json"
    sentinel.write_bytes(b"unrelated evidence must survive")
    attestation = root / "runtime" / "runtime-attestation.json"
    arguments = {
        "output_path": output,
        "candidate_root": root,
        "bundle_root": root,
        "release_run": support.RELEASE_RUN,
    }
    return module, root, candidate, output, attestation, sentinel, arguments


@contextmanager
def _replacement_model(monkeypatch: pytest.MonkeyPatch, target: Path, replacement: Path):
    """Model Windows deny-share races; perform the actual replacement on POSIX."""
    original_stat, original_lstat = os.stat, os.lstat
    identity = (original_stat(replacement).st_dev, original_stat(replacement).st_ino)
    assert identity != (original_stat(target).st_dev, original_stat(target).st_ino)
    target_key = os.path.normcase(os.path.abspath(target))
    active = False

    def redirected(path):
        if not active:
            return path
        try:
            key = os.path.normcase(os.path.abspath(os.fspath(path)))
        except (TypeError, ValueError):
            return path
        return replacement if key == target_key else path

    def replace():
        nonlocal active
        assert not active
        if os.name != "nt":
            os.replace(replacement, target)
        active = True
        state = os.lstat(target)
        assert (state.st_dev, state.st_ino) == identity

    with monkeypatch.context() as patch:
        if os.name == "nt":
            patch.setattr(
                os, "stat", lambda path, *a, **kw: original_stat(redirected(path), *a, **kw)
            )
            patch.setattr(
                os, "lstat", lambda path, *a, **kw: original_lstat(redirected(path), *a, **kw)
            )
        yield replace


def _phase_hooks(module, monkeypatch, *, phase, mutation):
    build = module._pass_receipt_from_candidate
    events = []

    def at(name):
        events.append(name)
        if name == phase:
            mutation()

    def build_then_mutate(**kwargs):
        document = build(**kwargs)
        at("receipt-built")
        return document

    def publish_then_mutate(publish):
        publish()
        at("receipt-published")

    monkeypatch.setattr(module, "_pass_receipt_from_candidate", build_then_mutate)
    support._intercept_image_receipt_publication(module, monkeypatch, publish_then_mutate)
    return events


@pytest.mark.parametrize("phase", _PHASES)
@pytest.mark.parametrize(
    "relative_input", _CANDIDATE_INPUTS, ids=("lock", "base", "platform", "data-plane", "observer")
)
def test_running_receipt_rejects_candidate_replacement_through_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, relative_input: str
) -> None:
    module, root, _candidate, output, attestation, sentinel, arguments = _case(
        tmp_path, monkeypatch
    )
    protected = _snapshot(attestation), _snapshot(sentinel)
    target = root / relative_input
    replacement = tmp_path / "same-byte-candidate-replacement"
    replacement.write_bytes(target.read_bytes())
    with _replacement_model(monkeypatch, target, replacement) as replace:
        events = _phase_hooks(module, monkeypatch, phase=phase, mutation=replace)
        try:
            with pytest.raises(ValueError, match="(?i)candidate|artifact|changed|lock|compose"):
                module.write_running_containers_receipt(**arguments)
        finally:
            assert events == list(_PHASES[: _PHASES.index(phase) + 1])
    assert not output.exists()
    assert list(output.parent.iterdir()) == [sentinel]
    assert (_snapshot(attestation), _snapshot(sentinel)) == protected


@pytest.mark.parametrize("phase", _PHASES)
@pytest.mark.parametrize("mutation_kind", ("same-byte-identity", "bytes"))
def test_running_receipt_rejects_attestation_drift_through_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, mutation_kind: str
) -> None:
    module, _root, _candidate, output, attestation, sentinel, arguments = _case(
        tmp_path, monkeypatch
    )
    sentinel_before = _snapshot(sentinel)
    attestation_before = _snapshot(attestation)
    replacement = tmp_path / "same-byte-attestation-replacement"
    replacement.write_bytes(attestation_before[0])
    with _replacement_model(monkeypatch, attestation, replacement) as replace:

        def mutate():
            if mutation_kind == "same-byte-identity":
                replace()
            else:
                attestation.write_bytes(attestation_before[0] + b"\n")

        events = _phase_hooks(module, monkeypatch, phase=phase, mutation=mutate)
        try:
            with pytest.raises(ValueError, match="(?i)attestation|artifact|changed"):
                module.write_running_containers_receipt(**arguments)
        finally:
            assert events == list(_PHASES[: _PHASES.index(phase) + 1])
    assert not output.exists()
    assert list(output.parent.iterdir()) == [sentinel]
    assert _snapshot(sentinel) == sentinel_before
    assert attestation.read_bytes() == attestation_before[0] + (
        b"\n" if mutation_kind == "bytes" else b""
    )
    if os.name == "nt" or mutation_kind == "bytes":
        assert _snapshot(attestation)[1] == attestation_before[1]


@pytest.mark.parametrize("same_content", (False, True))
def test_running_receipt_preserves_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_content: bool
) -> None:
    module, _root, _candidate, output, attestation, sentinel, arguments = _case(
        tmp_path, monkeypatch
    )
    module.write_running_containers_receipt(**arguments)
    if not same_content:
        output.write_bytes(b"previous release receipt")
    before = _snapshot(output), _snapshot(attestation), _snapshot(sentinel)
    with pytest.raises(FileExistsError):
        module.write_running_containers_receipt(**arguments)
    assert (_snapshot(output), _snapshot(attestation), _snapshot(sentinel)) == before
    assert set(output.parent.iterdir()) == {output, sentinel}


def test_running_receipt_preserves_competing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _root, _candidate, output, attestation, sentinel, arguments = _case(
        tmp_path, monkeypatch
    )
    protected = _snapshot(attestation), _snapshot(sentinel)
    competitor = []

    def compete(publish):
        output.write_bytes(b"competing receipt")
        competitor.append(_snapshot(output))
        publish()

    support._intercept_image_receipt_publication(module, monkeypatch, compete)
    with pytest.raises(OSError):
        module.write_running_containers_receipt(**arguments)
    assert competitor == [_snapshot(output)]
    assert (_snapshot(attestation), _snapshot(sentinel)) == protected
    assert set(output.parent.iterdir()) == {output, sentinel}


@pytest.mark.parametrize("after_publish", (False, True))
def test_running_receipt_publication_failure_retracts_only_owned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_publish: bool
) -> None:
    module, _root, _candidate, output, attestation, sentinel, arguments = _case(
        tmp_path, monkeypatch
    )
    protected = _snapshot(attestation), _snapshot(sentinel)
    calls = []

    def fail(publish):
        if after_publish:
            publish()
            assert output.exists()
        calls.append(after_publish)
        raise OSError("injected running receipt publication failure")

    support._intercept_image_receipt_publication(module, monkeypatch, fail)
    with pytest.raises(OSError, match="injected running receipt publication failure"):
        module.write_running_containers_receipt(**arguments)
    assert calls == [after_publish]
    assert list(output.parent.iterdir()) == [sentinel]
    assert (_snapshot(attestation), _snapshot(sentinel)) == protected


def test_running_receipt_lease_exit_failure_retracts_owned_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _root, _candidate, output, attestation, sentinel, arguments = _case(
        tmp_path, monkeypatch
    )
    protected = _snapshot(attestation), _snapshot(sentinel)
    lease = module.validated_candidate_artifact_lease
    published_before_exit = []

    @contextmanager
    def fail_after_exit(*args, **kwargs):
        with lease(*args, **kwargs) as artifact:
            yield artifact
        published_before_exit.append(output.exists())
        raise OSError("injected running candidate lease exit failure")

    monkeypatch.setattr(module, "validated_candidate_artifact_lease", fail_after_exit)
    with pytest.raises(OSError, match="injected running candidate lease exit failure"):
        module.write_running_containers_receipt(**arguments)
    assert published_before_exit == [True]
    assert list(output.parent.iterdir()) == [sentinel]
    assert (_snapshot(attestation), _snapshot(sentinel)) == protected


def test_running_receipt_rechecks_attestation_after_normal_candidate_lease_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _root, _candidate, output, attestation, sentinel, arguments = _case(
        tmp_path, monkeypatch
    )
    before = _snapshot(attestation)
    sentinel_before = _snapshot(sentinel)
    lease = module.validated_candidate_artifact_lease
    published_before_exit = []

    @contextmanager
    def drift_after_exit(*args, **kwargs):
        with lease(*args, **kwargs) as artifact:
            yield artifact
        published_before_exit.append(output.exists())
        attestation.write_bytes(before[0] + b"\n")

    monkeypatch.setattr(module, "validated_candidate_artifact_lease", drift_after_exit)
    with pytest.raises(ValueError, match="(?i)attestation|changed"):
        module.write_running_containers_receipt(**arguments)
    assert published_before_exit == [True]
    assert list(output.parent.iterdir()) == [sentinel]
    assert _snapshot(attestation) == (before[0] + b"\n", before[1])
    assert _snapshot(sentinel) == sentinel_before


def test_running_receipt_bound_publication_remains_assembler_consumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, root, candidate, output, attestation, sentinel, arguments = _case(tmp_path, monkeypatch)
    protected = _snapshot(attestation), _snapshot(sentinel)
    document = module.write_running_containers_receipt(**arguments)
    assert output.read_bytes() == module._json_bytes(document)
    assert document["candidate"] == candidate
    assert document["releaseRun"] == support.RELEASE_RUN
    assert document["provenance"] == {
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": hashlib.sha256(attestation.read_bytes()).hexdigest(),
        }
    }
    manifest_path = root / "release-evidence.json"
    manifest = support._assemble_manifest(
        module,
        manifest_path,
        candidate_root=root,
        expected_supervisor_public_key_spki_sha256=(support.TEST_SUPERVISOR_PUBLIC_KEY_SPKI_SHA256),
        release_run=support.RELEASE_RUN,
        receipt_paths={"running_containers": output},
    )
    assert manifest["evidence"]["running_containers"]["status"] == "pass"
    verifier = support._load_verifier()
    result = verifier.verify(
        support._file_release_runtime(
            verifier,
            manifest_path,
            expected_supervisor_public_key_spki_sha256=(
                support.TEST_SUPERVISOR_PUBLIC_KEY_SPKI_SHA256
            ),
            expected_source_head=support.SOURCE_HEAD,
            candidate_root=root,
        )
    )
    assert result.layers["running_containers"].status == "pass"
    assert (_snapshot(attestation), _snapshot(sentinel)) == protected
