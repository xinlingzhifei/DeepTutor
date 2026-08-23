from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def _load_verifier():
    path = Path(__file__).parents[2] / "scripts" / "verify_classroom_release.py"
    spec = importlib.util.spec_from_file_location("task7_verify_classroom_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate(source_head: str) -> dict[str, object]:
    return {
        "sourceHead": source_head,
        "imageDigests": {
            "deeptutor": "sha256:" + "1" * 64,
            "openmaic": "sha256:" + "2" * 64,
            "openmaic_render": "sha256:" + "3" * 64,
        },
    }


def _write_complete_bundle(
    tmp_path: Path,
    module,
    *,
    source_head: str,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    candidate = _candidate(source_head)
    evidence: dict[str, object] = {}
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in module.REQUIRED_LAYERS:
        artifact_path = artifacts / f"{name}.json"
        artifact_body = json.dumps(
            {
                "schemaVersion": 1,
                "candidate": candidate,
                "evidence": name,
            },
            sort_keys=True,
        ).encode()
        artifact_path.write_bytes(artifact_body)
        evidence[name] = {
            "status": "pass",
            "detail": f"{name} verified",
            "artifact": artifact_path.relative_to(tmp_path).as_posix(),
            "artifactSha256": hashlib.sha256(artifact_body).hexdigest(),
        }
    manifest = tmp_path / "release-evidence.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "candidate": candidate,
                "evidence": evidence,
            }
        ),
        encoding="utf-8",
    )
    return manifest, evidence, candidate


class FakeRuntime:
    def __init__(self) -> None:
        self.results: dict[str, object] = {}

    def set_result(self, name: str, status: str, detail: str = "verified") -> None:
        module = _load_verifier()
        self.results[name] = module.LayerEvidence(status=status, detail=detail)

    def result(self, name: str):
        return self.results.get(name)


def test_verifier_requires_the_exact_first_release_acceptance_matrix() -> None:
    module = _load_verifier()

    expected = (
        "teacher_flow",
        "student_micro_flow",
        "student_full_flow",
        "content_operations_flow",
        "classroom_exports",
        "tenant_isolation",
        "learning_event_idempotency",
        "openmaic_shared_plane",
        "openmaic_dedicated_plane",
        "tailwind4_visual_matrix",
        "backup_restore",
        "gateway_only_public",
    )

    assert module.REQUIRED_ACCEPTANCE_EVIDENCE == expected
    assert set(expected) <= set(module.REQUIRED_LAYERS)


def test_verifier_fails_when_any_business_flow_is_missing() -> None:
    module = _load_verifier()
    runtime = FakeRuntime()
    runtime.set_result("teacher_flow", "pass")
    runtime.set_result("student_micro_flow", "pass")

    result = module.verify(runtime)

    assert result.ok is False
    assert result.status == "not_ready"
    assert "content_operations_flow" in result.missing
    assert "source_head" in result.missing


def test_infrastructure_success_cannot_replace_business_evidence() -> None:
    module = _load_verifier()
    runtime = FakeRuntime()
    for name in (
        "source_head",
        "image_digests",
        "database_revisions",
        "running_containers",
        "service_health",
        "public_routes",
    ):
        runtime.set_result(name, "pass")

    result = module.verify(runtime)

    assert result.ok is False
    assert result.failed == ()
    assert "teacher_flow" in result.missing
    assert "backup_restore" in result.missing
    assert "capacity_profile" in result.missing


def test_one_failed_layer_blocks_an_otherwise_complete_release() -> None:
    module = _load_verifier()
    runtime = FakeRuntime()
    for name in module.REQUIRED_LAYERS:
        runtime.set_result(name, "pass")
    runtime.set_result("backup_restore", "fail", "restore digest mismatch")

    result = module.verify(runtime)

    assert result.ok is False
    assert result.missing == ()
    assert result.failed == ("backup_restore",)
    assert result.layers["backup_restore"].detail == "restore digest mismatch"


def test_all_required_layers_produce_a_ready_report() -> None:
    module = _load_verifier()
    runtime = FakeRuntime()
    for name in module.REQUIRED_LAYERS:
        runtime.set_result(name, "pass", f"{name} evidence")

    result = module.verify(runtime)
    payload = module.report_payload(result)

    assert result.ok is True
    assert result.status == "ready"
    assert result.missing == ()
    assert result.failed == ()
    assert tuple(result.layers) == module.REQUIRED_LAYERS
    assert payload["status"] == "ready"
    assert payload["ok"] is True
    assert set(payload["layers"]) == set(module.REQUIRED_LAYERS)


def test_file_runtime_requires_the_same_candidate_head(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="b" * 40))

    assert result.ok is False
    assert result.failed == ("source_head",)
    assert "does not match" in result.layers["source_head"].detail


def test_file_runtime_rejects_unproven_or_malformed_passes(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="c" * 40,
    )
    evidence["teacher_flow"] = {"status": "pass", "detail": "", "artifact": ""}
    evidence["student_full_flow"] = {"status": "unknown", "detail": "not run"}
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "candidate": candidate,
                "evidence": evidence,
            }
        ),
        encoding="utf-8",
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="c" * 40))

    assert result.ok is False
    assert result.layers["teacher_flow"].status == "fail"
    assert result.layers["student_full_flow"].status == "fail"


def test_file_runtime_rejects_missing_or_tampered_artifacts(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="e" * 40,
    )
    teacher = evidence["teacher_flow"]
    assert isinstance(teacher, dict)
    teacher["artifact"] = "artifacts/missing.json"
    student = evidence["student_micro_flow"]
    assert isinstance(student, dict)
    student_path = tmp_path / str(student["artifact"])
    student_path.write_bytes(student_path.read_bytes() + b"\n")
    manifest.write_text(
        json.dumps({"schemaVersion": 2, "candidate": candidate, "evidence": evidence}),
        encoding="utf-8",
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="e" * 40))

    assert result.layers["teacher_flow"].status == "fail"
    assert "does not exist" in result.layers["teacher_flow"].detail
    assert result.layers["student_micro_flow"].status == "fail"
    assert "digest" in result.layers["student_micro_flow"].detail


def test_file_runtime_rejects_artifact_from_another_candidate(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="f" * 40,
    )
    entry = evidence["student_full_flow"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    artifact_body = json.dumps(
        {
            "schemaVersion": 1,
            "candidate": _candidate("0" * 40),
            "evidence": "student_full_flow",
        },
        sort_keys=True,
    ).encode()
    artifact_path.write_bytes(artifact_body)
    entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps({"schemaVersion": 2, "candidate": candidate, "evidence": evidence}),
        encoding="utf-8",
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="f" * 40))

    assert result.layers["student_full_flow"].status == "fail"
    assert "candidate" in result.layers["student_full_flow"].detail


def test_file_runtime_rejects_zero_candidate_image_digest(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="1" * 40,
    )
    image_digests = candidate["imageDigests"]
    assert isinstance(image_digests, dict)
    image_digests["openmaic"] = "sha256:" + "0" * 64
    for name, raw in evidence.items():
        assert isinstance(raw, dict)
        artifact_path = tmp_path / str(raw["artifact"])
        artifact_body = json.dumps(
            {"schemaVersion": 1, "candidate": candidate, "evidence": name},
            sort_keys=True,
        ).encode()
        artifact_path.write_bytes(artifact_body)
        raw["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps({"schemaVersion": 2, "candidate": candidate, "evidence": evidence}),
        encoding="utf-8",
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="1" * 40))

    assert result.layers["image_digests"].status == "fail"
    assert "candidate image digests" in result.layers["image_digests"].detail


def test_missing_evidence_manifest_fails_closed(tmp_path: Path) -> None:
    module = _load_verifier()

    result = module.verify(
        module.FileReleaseRuntime(
            tmp_path / "missing.json",
            expected_source_head="d" * 40,
        )
    )

    assert result.ok is False
    assert result.missing == module.REQUIRED_LAYERS
