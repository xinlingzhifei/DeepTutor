from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE_HEAD = "a" * 40
RELEASE_TAG = f"yfeistai-first-release-20260825-{SOURCE_HEAD[:8]}"
OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
RELEASE_RUN = {
    "runId": "first-release-run-20260825",
    "environmentId": "test-environment",
}


def _load_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_evidence_module():
    path = ROOT / "scripts" / "classroom_release_evidence.py"
    assert path.is_file(), "classroom release evidence writer is missing"
    return _load_path("classroom_release_evidence_under_test", path)


def _load_renderer():
    return _load_path(
        "render_platform_compose_for_evidence_test",
        ROOT / "scripts" / "render_platform_compose.py",
    )


def _load_verifier():
    return _load_path(
        "verify_classroom_release_for_evidence_test",
        ROOT / "scripts" / "verify_classroom_release.py",
    )


def _write_candidate_root(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    renderer = _load_renderer()
    candidate_root = tmp_path / "candidate"
    deploy = candidate_root / "deploy"
    deploy.mkdir(parents=True)
    platform_compose = candidate_root / "docker-compose.platform.yml"
    data_plane_compose = candidate_root / "docker-compose.data-plane.yml"
    platform_compose.write_bytes((ROOT / "docker-compose.platform.yml").read_bytes())
    data_plane_compose.write_bytes((ROOT / "docker-compose.data-plane.yml").read_bytes())
    calls = 0

    def resolve(_reference: str) -> str:
        nonlocal calls
        calls += 1
        return "sha256:" + f"{calls:064x}"

    lock = renderer.write_image_lock(
        deploy / "image-lock.json",
        digest_resolver=resolve,
        compose_paths=(platform_compose, data_plane_compose),
        source_repository="xinlingzhifei/DeepTutor",
        source_head=SOURCE_HEAD,
        release_tag=RELEASE_TAG,
        openmaic_head=OPENMAIC_HEAD,
    )
    return candidate_root, lock["candidate"]


def test_pass_receipt_requires_explicit_checks_not_only_zero_exit(tmp_path: Path) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    output = tmp_path / "teacher-flow.json"
    output.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="checks"):
        module.write_pass_receipt(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="teacher_flow",
            observed_at="2026-08-25T00:00:00Z",
            native_exit=0,
            checks={"teacherFlowPassed": False},
        )

    assert output.read_bytes() == b"sentinel"


def test_pass_receipt_is_bound_to_candidate_and_release_run(tmp_path: Path) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    output = tmp_path / "teacher-flow.json"

    receipt = module.write_pass_receipt(
        output,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        evidence="teacher_flow",
        observed_at="2026-08-25T00:00:00Z",
        native_exit=0,
        checks={"teacherFlowPassed": True},
    )

    assert receipt == json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schemaVersion"] == 2
    assert receipt["candidate"] == candidate
    assert receipt["releaseRun"] == RELEASE_RUN
    assert receipt["evidence"] == "teacher_flow"
    assert receipt["receipt"] == {
        "producer": "playwright",
        "observedAt": "2026-08-25T00:00:00Z",
        "result": {
            "outcome": "pass",
            "nativeExit": 0,
            "checks": {"teacherFlowPassed": True},
        },
    }


def test_manifest_assembler_hashes_candidate_bound_receipts(tmp_path: Path) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    artifacts = candidate_root / "artifacts"
    artifacts.mkdir()
    receipt_path = artifacts / "teacher_flow.json"
    module.write_pass_receipt(
        receipt_path,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        evidence="teacher_flow",
        observed_at="2026-08-25T00:00:00Z",
        native_exit=0,
        checks={"teacherFlowPassed": True},
    )
    manifest_path = candidate_root / "release-evidence.json"

    manifest = module.assemble_manifest(
        manifest_path,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={"teacher_flow": receipt_path},
    )

    receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    assert manifest == json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 3
    assert manifest["candidate"] == candidate
    assert manifest["releaseRun"] == RELEASE_RUN
    assert manifest["evidence"] == {
        "teacher_flow": {
            "status": "pass",
            "detail": "teacher_flow verified by playwright",
            "artifact": "artifacts/teacher_flow.json",
            "artifactSha256": receipt_sha256,
        }
    }
    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest_path,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )
    assert result.layers["teacher_flow"].status == "pass"
    assert set(result.missing) == set(verifier.REQUIRED_LAYERS) - {"teacher_flow"}


@pytest.mark.parametrize("case", ("candidate", "release-run", "body"))
def test_manifest_assembler_rejects_mismatched_or_tampered_receipts(
    tmp_path: Path,
    case: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    receipt_path = candidate_root / "teacher_flow.json"
    module.write_pass_receipt(
        receipt_path,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        evidence="teacher_flow",
        observed_at="2026-08-25T00:00:00Z",
        native_exit=0,
        checks={"teacherFlowPassed": True},
    )
    if case == "body":
        receipt_path.write_bytes(receipt_path.read_bytes() + b"\nnot-json")
    else:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if case == "candidate":
            receipt["candidate"]["sourceHead"] = "b" * 40
        else:
            receipt["releaseRun"]["runId"] = "another-run"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ValueError, match="receipt"):
        module.assemble_manifest(
            candidate_root / "release-evidence.json",
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"teacher_flow": receipt_path},
        )


def test_manifest_publish_preserves_existing_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    receipt_path = candidate_root / "teacher_flow.json"
    module.write_pass_receipt(
        receipt_path,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        evidence="teacher_flow",
        observed_at="2026-08-25T00:00:00Z",
        native_exit=0,
        checks={"teacherFlowPassed": True},
    )
    manifest_path = candidate_root / "release-evidence.json"
    manifest_path.write_bytes(b"sentinel")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated manifest publish failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated manifest publish failure"):
        module.assemble_manifest(
            manifest_path,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"teacher_flow": receipt_path},
        )

    assert manifest_path.read_bytes() == b"sentinel"
    assert list(candidate_root.glob(".*.tmp")) == []


def test_manifest_rejects_output_path_that_would_overwrite_receipt(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    receipt_path = candidate_root / "teacher_flow.json"
    module.write_pass_receipt(
        receipt_path,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        evidence="teacher_flow",
        observed_at="2026-08-25T00:00:00Z",
        native_exit=0,
        checks={"teacherFlowPassed": True},
    )
    original = receipt_path.read_bytes()

    with pytest.raises(ValueError, match="receipt"):
        module.assemble_manifest(
            receipt_path,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"teacher_flow": receipt_path},
        )

    assert receipt_path.read_bytes() == original
