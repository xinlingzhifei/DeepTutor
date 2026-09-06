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
_PROOF = "runtime/platform-preflight-attestation.json"
_OUTPUTS = (_PROOF, "artifacts/database_revisions.json", "artifacts/service_health.json")


def _case(tmp_path, monkeypatch, *, phase=None, mutate=lambda: None):
    monkeypatch.setenv("YFEISTAI_OBSERVER_SIGNER_IMAGE", support.OBSERVER_SIGNER_IMAGE)
    module = support._load_evidence_module()
    root, candidate = support._write_candidate_root(tmp_path)
    calls = []

    def runner(command, **_kwargs):
        current_phase = command[command.index("--runtime-phase") + 1]
        calls.append(current_phase)
        names = (
            (
                "activeTenantCredentialsValid",
                "databaseConnected",
                "objectStoreRoundTrip",
                "revisionsMatch",
                "tenantCrossPrefixDenied",
                "tenantOwnPrefixAccessible",
            )
            if current_phase == "database-object-store"
            else ("openmaicContractCompatible",)
        )
        stdout = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "producer": "platform-preflight",
                    "phase": current_phase,
                    "checks": dict.fromkeys(names, True),
                    "errors": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        if phase == "first-phase" and len(calls) == 1:
            mutate()
        return subprocess.CompletedProcess(command, 0, stdout, "")

    original_publish = module._publish_no_replace

    def publish(staged, target):
        original_publish(staged, target)
        if phase == "proof-published" and target == root / _PROOF:
            mutate()

    monkeypatch.setattr(module, "_publish_no_replace", publish)
    monkeypatch.setattr(module, "_current_observed_at", lambda: "2026-08-25T00:01:00Z")
    arguments = {
        "candidate_root": root,
        "bundle_root": root,
        "release_run": support.RELEASE_RUN,
        "timeout_seconds": 120,
        "runner": runner,
        "docker_resolver": lambda: tmp_path / "fixed-docker.exe",
    }
    return module, root, candidate, calls, arguments


@pytest.mark.parametrize("phase", ("first-phase", "proof-published"))
@pytest.mark.parametrize("relative_input", _INPUTS)
def test_preflight_rejects_original_input_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, relative_input: str
) -> None:
    mutation = None

    def mutate():
        assert mutation is not None
        mutation()

    module, root, _candidate, calls, arguments = _case(
        tmp_path, monkeypatch, phase=phase, mutate=mutate
    )
    target = root / relative_input
    replacement = tmp_path / "same-byte-replacement"
    replacement.write_bytes(target.read_bytes())
    with _replacement_model(monkeypatch, target, replacement) as replace:
        mutation = replace
        with pytest.raises(
            ValueError, match="(?i)candidate|artifact|changed|lock|compose|attestation"
        ):
            module.write_platform_preflight_receipts(**arguments)
    assert calls == (
        ["database-object-store"]
        if phase == "first-phase"
        else ["database-object-store", "openmaic"]
    )
    assert all(not (root / path).exists() for path in _OUTPUTS)


@pytest.mark.parametrize("phase", ("first-phase", "proof-published"))
def test_preflight_rejects_runtime_body_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    def mutate():
        runtime.write_bytes(runtime.read_bytes() + b"\n")

    module, root, _candidate, calls, arguments = _case(
        tmp_path, monkeypatch, phase=phase, mutate=mutate
    )
    runtime = root / "runtime/runtime-attestation.json"
    with pytest.raises(ValueError, match="(?i)attestation|changed|binding"):
        module.write_platform_preflight_receipts(**arguments)
    assert len(calls) == (1 if phase == "first-phase" else 2)
    assert all(not (root / path).exists() for path in _OUTPUTS)


@pytest.mark.parametrize("failure", ("candidate-exit", "runtime-after-candidate-exit"))
def test_preflight_checks_normal_candidate_exit_before_committing_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    module, root, _candidate, calls, arguments = _case(tmp_path, monkeypatch)
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
        module.write_platform_preflight_receipts(**arguments)
    assert calls == ["database-object-store", "openmaic"]
    assert injected == [failure]
    assert all(not (root / path).exists() for path in _OUTPUTS)


def test_preflight_receipts_replay_with_real_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, root, candidate, calls, arguments = _case(tmp_path, monkeypatch)
    documents = module.write_platform_preflight_receipts(**arguments)
    verifier = support._load_verifier()
    checks, observed_at = verifier.derive_platform_preflight_receipt_checks(
        (root / _PROOF).read_bytes(),
        bundle_root=root,
        candidate_root=root,
        candidate=candidate,
        release_run=support.RELEASE_RUN,
        observer_signer_image=support.OBSERVER_SIGNER_IMAGE,
    )
    assert calls == ["database-object-store", "openmaic"]
    assert observed_at == "2026-08-25T00:01:00Z"
    assert checks == {
        "database_revisions": {"revisionsMatch": True},
        "service_health": {"allServicesHealthy": True},
    }
    for evidence, document in documents.items():
        assert document["receipt"]["result"]["checks"] == checks[evidence]
        assert json.loads((root / f"artifacts/{evidence}.json").read_bytes()) == document
    manifest = root / "release-evidence.json"
    support._assemble_manifest(
        module,
        manifest,
        candidate_root=root,
        expected_supervisor_public_key_spki_sha256=support.TEST_SUPERVISOR_PUBLIC_KEY_SPKI_SHA256,
        release_run=support.RELEASE_RUN,
        receipt_paths={name: root / f"artifacts/{name}.json" for name in documents},
    )
    result = verifier.verify(
        support._file_release_runtime(
            verifier,
            manifest,
            expected_supervisor_public_key_spki_sha256=support.TEST_SUPERVISOR_PUBLIC_KEY_SPKI_SHA256,
            expected_source_head=support.SOURCE_HEAD,
            candidate_root=root,
        )
    )
    assert result.layers["database_revisions"].status == "pass"
    assert result.layers["service_health"].status == "pass"


def test_preflight_compensates_link_that_succeeds_before_raising(tmp_path, monkeypatch):
    module, root, _candidate, _calls, arguments = _case(tmp_path, monkeypatch)
    publish = module._publish_no_replace

    def fail_after_link(source, target):
        publish(source, target)
        if target.name == "service_health.json":
            raise OSError("injected post-link failure")

    monkeypatch.setattr(module, "_publish_no_replace", fail_after_link)
    with pytest.raises(OSError, match="injected post-link failure"):
        module.write_platform_preflight_receipts(**arguments)
    assert all(not (root / path).exists() for path in _OUTPUTS)


def test_preflight_preserves_foreign_receipt_during_input_failure(tmp_path, monkeypatch):
    def mutate():
        os.replace(foreign, root / "artifacts/database_revisions.json")
        runtime = root / "runtime/runtime-attestation.json"
        runtime.write_bytes(runtime.read_bytes() + b"\n")

    module, root, _candidate, _calls, arguments = _case(
        tmp_path, monkeypatch, phase="proof-published", mutate=mutate
    )
    foreign = tmp_path / "foreign-receipt"
    body = b"unrelated competing receipt"
    foreign.write_bytes(body)
    identity = foreign.stat().st_ino
    with pytest.raises(ValueError, match="attestation|changed"):
        module.write_platform_preflight_receipts(**arguments)
    target = root / "artifacts/database_revisions.json"
    assert target.stat().st_ino == identity
    assert target.read_bytes() == body
    assert not (root / _PROOF).exists()
    assert not (root / "artifacts/service_health.json").exists()
    assert all(path.read_bytes() != body for path in (root / "failures").rglob("*.json"))


def test_preflight_archive_failure_does_not_mask_publication_error(tmp_path, monkeypatch):
    module, _root, _candidate, _calls, arguments = _case(tmp_path, monkeypatch)

    def fail_publication(*_args):
        raise OSError("primary publication failure")

    def fail_archive(**_kwargs):
        raise OSError("secondary archive failure")

    monkeypatch.setattr(module, "_publish_no_replace", fail_publication)
    monkeypatch.setattr(module, "_record_probe_failure", fail_archive)
    with pytest.raises(OSError, match="primary publication failure") as caught:
        module.write_platform_preflight_receipts(**arguments)
    assert any("secondary archive failure" in note for note in caught.value.__notes__)


@pytest.mark.parametrize("directory", ("runtime", "artifacts"), ids=("input", "output"))
def test_preflight_directory_close_failure_retracts_owned_receipts(
    tmp_path, monkeypatch, directory
):
    module, root, _candidate, calls, arguments = _case(tmp_path, monkeypatch)
    runtime = root / "runtime/runtime-attestation.json"
    body, identity = runtime.read_bytes(), runtime.stat().st_ino
    real_close = module._close_receipt_input_directory
    injected = []

    def close_then_fail(boundary):
        real_close(boundary)
        if boundary.root == root / directory and (root / _PROOF).exists() and not injected:
            injected.append(directory)
            raise OSError("injected preflight directory close failure")

    monkeypatch.setattr(module, "_close_receipt_input_directory", close_then_fail)
    with pytest.raises(OSError, match="injected preflight directory close failure"):
        module.write_platform_preflight_receipts(**arguments)
    assert injected == [directory]
    assert calls == ["database-object-store", "openmaic"]
    assert all(not (root / path).exists() for path in _OUTPUTS)
    assert runtime.read_bytes() == body
    assert runtime.stat().st_ino == identity
