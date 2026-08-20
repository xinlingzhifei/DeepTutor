from __future__ import annotations

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


class FakeRuntime:
    def __init__(self) -> None:
        self.results: dict[str, object] = {}

    def set_result(self, name: str, status: str, detail: str = "verified") -> None:
        module = _load_verifier()
        self.results[name] = module.LayerEvidence(status=status, detail=detail)

    def result(self, name: str):
        return self.results.get(name)


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
    evidence = {
        name: {
            "status": "pass",
            "detail": f"{name} verified",
            "artifact": f"evidence/{name}.json",
        }
        for name in module.REQUIRED_LAYERS
    }
    manifest = tmp_path / "release-evidence.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "candidate": {"sourceHead": "a" * 40},
                "evidence": evidence,
            }
        ),
        encoding="utf-8",
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="b" * 40))

    assert result.ok is False
    assert result.failed == ("source_head",)
    assert "does not match" in result.layers["source_head"].detail


def test_file_runtime_rejects_unproven_or_malformed_passes(tmp_path: Path) -> None:
    module = _load_verifier()
    evidence = {
        name: {
            "status": "pass",
            "detail": "verified",
            "artifact": f"evidence/{name}.json",
        }
        for name in module.REQUIRED_LAYERS
    }
    evidence["teacher_flow"] = {"status": "pass", "detail": "", "artifact": ""}
    evidence["student_full_flow"] = {"status": "unknown", "detail": "not run"}
    manifest = tmp_path / "release-evidence.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "candidate": {"sourceHead": "c" * 40},
                "evidence": evidence,
            }
        ),
        encoding="utf-8",
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="c" * 40))

    assert result.ok is False
    assert result.layers["teacher_flow"].status == "fail"
    assert result.layers["student_full_flow"].status == "fail"


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
