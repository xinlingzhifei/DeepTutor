from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

_RELEASE_RUN = {
    "runId": "first-release-run-20260824",
    "environmentId": "test-environment",
}
_SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
_OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"


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
        "sourceRepository": _SOURCE_REPOSITORY,
        "sourceHead": source_head,
        "releaseTag": f"yfeistai-first-release-20260825-{source_head[:8]}",
        "openmaicHead": _OPENMAIC_HEAD,
        "imageDigests": {
            "deeptutor": "sha256:" + "1" * 64,
            "openmaic": "sha256:" + "2" * 64,
            "openmaic_render": "sha256:" + "3" * 64,
        },
    }


def _playwright_report(
    evidence: str,
    *,
    count: int,
) -> dict[str, object]:
    file = "tests/e2e/classroom-first-release.live.spec.ts"
    specs = []
    for index in range(count):
        title = f"[release-evidence:{evidence}] case {index + 1}"
        specs.append(
            {
                "title": title,
                "ok": True,
                "tags": [],
                "tests": [
                    {
                        "timeout": 30_000,
                        "annotations": [],
                        "expectedStatus": "passed",
                        "projectId": "first-release-live",
                        "projectName": "first-release-live",
                        "results": [
                            {
                                "workerIndex": 0,
                                "parallelIndex": 0,
                                "status": "passed",
                                "duration": 10,
                                "error": None,
                                "errors": [],
                                "stdout": [],
                                "stderr": [],
                                "retry": 0,
                                "startTime": "2026-08-25T00:00:00.000Z",
                                "attachments": [],
                                "annotations": [],
                            }
                        ],
                        "status": "expected",
                    }
                ],
                "id": f"first-release-live-{index}",
                "file": file,
                "line": index + 1,
                "column": 1,
            }
        )
    return {
        "config": {
            "projects": [
                {
                    "id": "first-release-live",
                    "name": "first-release-live",
                    "retries": 0,
                }
            ]
        },
        "suites": [
            {
                "title": "classroom-first-release.live.spec.ts",
                "file": file,
                "column": 0,
                "line": 0,
                "specs": specs,
            }
        ],
        "errors": [],
        "stats": {
            "startTime": "2026-08-25T00:00:00.000Z",
            "duration": count * 10,
            "expected": count,
            "unexpected": 0,
            "flaky": 0,
            "skipped": 0,
        },
    }


def test_derive_probe_checks_from_native_playwright_json() -> None:
    module = _load_verifier()
    candidate = _candidate("a" * 40)
    raw = json.dumps(_playwright_report("teacher_flow", count=1)).encode()

    checks = module.derive_probe_checks(
        "teacher_flow",
        raw_report=raw,
        candidate=candidate,
        release_run=_RELEASE_RUN,
    )

    assert checks == {"teacherFlowPassed": True}


def _first_probe_spec(report: dict[str, object]) -> dict[str, object]:
    suites = report["suites"]
    assert isinstance(suites, list) and suites
    suite = suites[0]
    assert isinstance(suite, dict)
    specs = suite["specs"]
    assert isinstance(specs, list) and specs
    spec = specs[0]
    assert isinstance(spec, dict)
    return spec


def _first_probe_test(report: dict[str, object]) -> dict[str, object]:
    spec = _first_probe_spec(report)
    tests = spec["tests"]
    assert isinstance(tests, list) and tests
    test = tests[0]
    assert isinstance(test, dict)
    return test


@pytest.mark.parametrize(
    "case",
    (
        "tampered-stats",
        "nan-duration",
        "extra-spec",
        "wrong-file",
        "wrong-project",
        "retry",
        "flaky",
        "skipped",
        "unexpected",
        "global-error",
        "wrong-title",
    ),
)
def test_derive_probe_checks_rejects_tampered_native_playwright_json(case: str) -> None:
    module = _load_verifier()
    report = _playwright_report("teacher_flow", count=1)
    stats = report["stats"]
    assert isinstance(stats, dict)
    spec = _first_probe_spec(report)
    test = _first_probe_test(report)
    results = test["results"]
    assert isinstance(results, list) and results
    result = results[0]
    assert isinstance(result, dict)
    if case == "tampered-stats":
        stats["expected"] = 2
    elif case == "nan-duration":
        stats["duration"] = float("nan")
    elif case == "extra-spec":
        suites = report["suites"]
        assert isinstance(suites, list)
        suite = suites[0]
        assert isinstance(suite, dict)
        specs = suite["specs"]
        assert isinstance(specs, list)
        specs.append(copy.deepcopy(spec))
        stats["expected"] = 2
    elif case == "wrong-file":
        spec["file"] = "tests/e2e/classroom-first-release.spec.ts"
    elif case == "wrong-project":
        test["projectId"] = "teaching-flow"
        test["projectName"] = "teaching-flow"
    elif case == "retry":
        result["retry"] = 1
    elif case in {"flaky", "skipped", "unexpected"}:
        test["status"] = case
        stats["expected"] = 0
        stats[case] = 1
    elif case == "global-error":
        report["errors"] = [{"message": "global setup failed"}]
    else:
        spec["title"] = "teacher flow without the fixed evidence marker"

    with pytest.raises(ValueError):
        module.derive_probe_checks(
            "teacher_flow",
            raw_report=json.dumps(report).encode(),
            candidate=_candidate("a" * 40),
            release_run=_RELEASE_RUN,
        )


@pytest.mark.parametrize(
    "case",
    ("extra-project", "retries-false", "attempt-duration-nan", "attempt-duration-false"),
)
def test_derive_probe_checks_rejects_invalid_project_or_attempt_metric(case: str) -> None:
    module = _load_verifier()
    report = _playwright_report("teacher_flow", count=1)
    config = report["config"]
    assert isinstance(config, dict)
    projects = config["projects"]
    assert isinstance(projects, list) and projects
    project = projects[0]
    assert isinstance(project, dict)
    test = _first_probe_test(report)
    results = test["results"]
    assert isinstance(results, list) and results
    result = results[0]
    assert isinstance(result, dict)
    if case == "extra-project":
        projects.append({"id": "other", "name": "other", "retries": 0})
    elif case == "retries-false":
        project["retries"] = False
    elif case == "attempt-duration-nan":
        result["duration"] = float("nan")
    else:
        result["duration"] = False

    with pytest.raises(ValueError):
        module.derive_probe_checks(
            "teacher_flow",
            raw_report=json.dumps(report).encode(),
            candidate=_candidate("a" * 40),
            release_run=_RELEASE_RUN,
        )


def _artifact_document(
    module,
    candidate: dict[str, object],
    evidence: str,
    *,
    release_run: dict[str, str] = _RELEASE_RUN,
    provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    producer, required_checks = module.RECEIPT_CONTRACTS[evidence]
    document = {
        "schemaVersion": module.ARTIFACT_SCHEMA_VERSION,
        "candidate": candidate,
        "releaseRun": release_run,
        "evidence": evidence,
        "receipt": {
            "producer": producer,
            "observedAt": "2026-08-24T00:00:00Z",
            "result": {
                "outcome": "pass",
                "nativeExit": 0,
                "checks": {check: True for check in required_checks},
            },
        },
    }
    if provenance is not None:
        document["provenance"] = provenance
    return document


def _write_probe_proof(
    tmp_path: Path,
    module,
    candidate: dict[str, object],
    evidence: str,
) -> dict[str, object] | None:
    recipe = module.PROBE_RECIPES.get(evidence)
    if recipe is None:
        return None
    recipe_id, expected_count = recipe
    proof_root = tmp_path / "proof"
    proof_root.mkdir(exist_ok=True)
    raw_path = proof_root / f"{evidence}.json"
    execution_path = proof_root / f"{evidence}.execution.json"
    raw_document = _playwright_report(evidence, count=expected_count)
    raw_path.write_text(json.dumps(raw_document), encoding="utf-8")
    raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    command = module.probe_command_record(evidence)
    execution = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "evidence": evidence,
        "recipe": recipe_id,
        "command": command,
        "observedAt": "2026-08-24T00:00:00Z",
        "nativeExit": 0,
        "rawReportSha256": raw_sha256,
    }
    execution_path.write_text(json.dumps(execution), encoding="utf-8")
    return {
        "recipe": recipe_id,
        "command": command,
        "rawReport": {
            "artifact": raw_path.relative_to(tmp_path).as_posix(),
            "sha256": raw_sha256,
        },
        "execution": {
            "artifact": execution_path.relative_to(tmp_path).as_posix(),
            "sha256": hashlib.sha256(execution_path.read_bytes()).hexdigest(),
        },
    }


def _manifest_document(
    module,
    candidate: dict[str, object],
    evidence: dict[str, object],
) -> dict[str, object]:
    return {
        "schemaVersion": module.EVIDENCE_SCHEMA_VERSION,
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "evidence": evidence,
    }


def _write_candidate_files(tmp_path: Path, candidate: dict[str, object]) -> None:
    digests = candidate["imageDigests"]
    assert isinstance(digests, dict)
    release_tag = candidate["releaseTag"]
    assert isinstance(release_tag, str)
    specifications = {
        "deeptutor": "ghcr.io/xinlingzhifei/deeptutor",
        "openmaic": "ghcr.io/xinlingzhifei/openmaic",
        "openmaic_render": "ghcr.io/xinlingzhifei/openmaic-render",
    }
    lock = {
        "schemaVersion": 2,
        "candidate": candidate,
        "images": {
            name: {
                "repository": repository,
                "tag": release_tag,
                "digest": digests[name],
                "reference": f"{repository}:{release_tag}@{digests[name]}",
            }
            for name, repository in specifications.items()
        },
    }
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "image-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    references = {
        name: f"{repository}:{release_tag}@{digests[name]}"
        for name, repository in specifications.items()
    }
    (tmp_path / "docker-compose.platform.yml").write_text(
        json.dumps(
            {
                "services": {
                    "deeptutor": {"image": references["deeptutor"]},
                    "teaching-migrate": {"image": references["deeptutor"]},
                    "tenant-provisioner": {"image": references["deeptutor"]},
                    "shared-data-plane-bootstrap": {"image": references["deeptutor"]},
                    "teaching-dispatcher": {"image": references["deeptutor"]},
                    "teaching-worker": {"image": references["deeptutor"]},
                    "teaching-export-worker": {"image": references["deeptutor"]},
                    "teaching-reaper": {"image": references["deeptutor"]},
                    "learning-projector": {"image": references["deeptutor"]},
                    "openmaic": {"image": references["openmaic"]},
                    "openmaic-render": {"image": references["openmaic_render"]},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "docker-compose.data-plane.yml").write_text(
        json.dumps(
            {
                "services": {
                    "openmaic": {"image": references["openmaic"]},
                    "openmaic-render": {"image": references["openmaic_render"]},
                }
            }
        ),
        encoding="utf-8",
    )


def _write_complete_bundle(
    tmp_path: Path,
    module,
    *,
    source_head: str,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    candidate = _candidate(source_head)
    module.PROJECT_ROOT = tmp_path
    _write_candidate_files(tmp_path, candidate)
    evidence: dict[str, object] = {}
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in module.REQUIRED_LAYERS:
        artifact_path = artifacts / f"{name}.json"
        provenance = _write_probe_proof(tmp_path, module, candidate, name)
        artifact_body = json.dumps(
            _artifact_document(module, candidate, name, provenance=provenance),
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
        json.dumps(_manifest_document(module, candidate, evidence)),
        encoding="utf-8",
    )
    return manifest, evidence, candidate


def _rewrite_bundle_candidate(
    tmp_path: Path,
    module,
    manifest: Path,
    evidence: dict[str, object],
    candidate: dict[str, object],
) -> None:
    for name, raw in evidence.items():
        assert isinstance(raw, dict)
        artifact_path = tmp_path / str(raw["artifact"])
        provenance = _write_probe_proof(tmp_path, module, candidate, name)
        artifact_body = json.dumps(
            _artifact_document(module, candidate, name, provenance=provenance),
            sort_keys=True,
        ).encode()
        artifact_path.write_bytes(artifact_body)
        raw["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence)),
        encoding="utf-8",
    )


class FakeRuntime:
    def __init__(self) -> None:
        self.results: dict[str, object] = {}
        self.candidate: dict[str, object] | None = _candidate("a" * 40)
        self.release_run: dict[str, str] | None = dict(_RELEASE_RUN)
        self.evidence_bundle_sha256: str | None = "f" * 64

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


def test_probe_command_record_is_stable_across_verifier_source_roots(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    module.SCRIPTS_ROOT = Path("E:/different-verifier-root/scripts")

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.ok is True


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
        json.dumps(_manifest_document(module, candidate, evidence)),
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
        json.dumps(_manifest_document(module, candidate, evidence)),
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
        _artifact_document(module, _candidate("0" * 40), "student_full_flow"),
        sort_keys=True,
    ).encode()
    artifact_path.write_bytes(artifact_body)
    entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence)),
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
            _artifact_document(module, candidate, name),
            sort_keys=True,
        ).encode()
        artifact_path.write_bytes(artifact_body)
        raw["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence)),
        encoding="utf-8",
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="1" * 40))

    assert result.layers["image_digests"].status == "fail"
    assert "candidate image digests" in result.layers["image_digests"].detail


def test_file_runtime_rejects_nonzero_candidate_digests_that_do_not_match_image_lock(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="2" * 40,
    )
    image_digests = candidate["imageDigests"]
    assert isinstance(image_digests, dict)
    image_digests["deeptutor"] = "sha256:" + "4" * 64
    for name, raw in evidence.items():
        assert isinstance(raw, dict)
        artifact_path = tmp_path / str(raw["artifact"])
        artifact_body = json.dumps(
            _artifact_document(module, candidate, name),
            sort_keys=True,
        ).encode()
        artifact_path.write_bytes(artifact_body)
        raw["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence)),
        encoding="utf-8",
    )

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="2" * 40))

    assert result.layers["image_digests"].status == "fail"
    assert "image lock" in result.layers["image_digests"].detail


@pytest.mark.parametrize(
    ("case", "expected_detail"),
    (
        pytest.param("source-head", "source head", id="source-head"),
        pytest.param("release-tag", "release tag", id="release-tag"),
        pytest.param("openmaic-head", "OpenMAIC head", id="openmaic-head"),
        pytest.param("image-digest", "image digests", id="image-digest"),
    ),
)
def test_file_runtime_rejects_candidate_metadata_that_does_not_match_image_lock(
    tmp_path: Path,
    case: str,
    expected_detail: str,
) -> None:
    module = _load_verifier()
    manifest, evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    expected_source_head = str(candidate["sourceHead"])
    if case == "source-head":
        candidate["sourceHead"] = "b" * 40
        candidate["releaseTag"] = "yfeistai-first-release-20260825-bbbbbbbb"
        expected_source_head = "b" * 40
    elif case == "release-tag":
        candidate["releaseTag"] = "yfeistai-first-release-20260826-aaaaaaaa"
    elif case == "openmaic-head":
        candidate["openmaicHead"] = "c" * 40
    else:
        image_digests = candidate["imageDigests"]
        assert isinstance(image_digests, dict)
        image_digests["openmaic"] = "sha256:" + "4" * 64
    _rewrite_bundle_candidate(tmp_path, module, manifest, evidence, candidate)

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head=expected_source_head,
        )
    )

    assert result.layers["image_digests"].status == "fail"
    assert expected_detail in result.layers["image_digests"].detail


def test_file_runtime_rejects_candidate_not_referenced_by_both_production_compose_files(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="3" * 40,
    )
    data_plane = tmp_path / "docker-compose.data-plane.yml"
    data_plane.write_text("services: {}\n", encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="3" * 40))

    assert result.layers["image_digests"].status == "fail"
    assert "production Compose" in result.layers["image_digests"].detail


@pytest.mark.parametrize(
    "case",
    ("comment-only", "unused-extension", "invalid-yaml", "wrong-service"),
)
def test_file_runtime_rejects_non_service_compose_reference(
    tmp_path: Path,
    case: str,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="9" * 40,
    )
    lock = json.loads((tmp_path / "deploy" / "image-lock.json").read_text(encoding="utf-8"))
    references = {name: record["reference"] for name, record in lock["images"].items()}
    platform_path = tmp_path / "docker-compose.platform.yml"
    services = {
        "deeptutor": {"image": "ghcr.io/example/wrong:latest"},
        "openmaic": {"image": references["openmaic"]},
        "openmaic-render": {"image": references["openmaic_render"]},
    }
    if case == "comment-only":
        body = json.dumps({"services": services}) + f"\n# {references['deeptutor']}\n"
    elif case == "unused-extension":
        body = json.dumps({"x-unused": references["deeptutor"], "services": services})
    elif case == "invalid-yaml":
        body = "services: [\n" + "\n".join(f"# {value}" for value in references.values())
    else:
        services["unused"] = {"image": references["deeptutor"]}
        body = json.dumps({"services": services})
    platform_path.write_text(body, encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="9" * 40))

    assert result.layers["image_digests"].status == "fail"
    assert "production Compose" in result.layers["image_digests"].detail


def test_file_runtime_accepts_compose_tags_and_merged_custom_image_services(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="c" * 40,
    )
    lock = json.loads((tmp_path / "deploy" / "image-lock.json").read_text())
    references = {name: record["reference"] for name, record in lock["images"].items()}
    platform = f"""\
x-images:
  deeptutor: &deeptutor-image {json.dumps(references["deeptutor"])}
  openmaic: &openmaic-image {json.dumps(references["openmaic"])}
  openmaic-render: &openmaic-render-image {json.dumps(references["openmaic_render"])}
x-teaching-process: &teaching-process
  image: *deeptutor-image
services:
  pocketbase:
    ports: !reset []
  deeptutor:
    image: *deeptutor-image
    build: !reset null
    networks: !override
      - platform-internal
  teaching-migrate:
    image: *deeptutor-image
  tenant-provisioner:
    <<: *teaching-process
  shared-data-plane-bootstrap:
    image: *deeptutor-image
  teaching-dispatcher:
    <<: *teaching-process
  teaching-worker:
    <<: *teaching-process
  teaching-export-worker:
    <<: *teaching-process
  teaching-reaper:
    <<: *teaching-process
  learning-projector:
    <<: *teaching-process
  openmaic:
    image: *openmaic-image
  openmaic-render:
    image: *openmaic-render-image
"""
    (tmp_path / "docker-compose.platform.yml").write_text(platform, encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="c" * 40))

    assert result.ok is True


def test_current_schema_one_image_lock_is_historical_not_a_release_candidate(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    lock = json.loads(
        (module.PROJECT_ROOT / "deploy" / "image-lock.json").read_text(encoding="utf-8")
    )
    digests = {name: lock["images"][name]["digest"] for name in module.CUSTOM_IMAGE_NAMES}
    candidate = _candidate("f" * 40)
    candidate["imageDigests"] = digests
    runtime = module.FileReleaseRuntime(
        tmp_path / "unused.json",
        expected_source_head="f" * 40,
    )

    assert (
        runtime._candidate_binding_error(candidate)
        == "candidate image lock is unavailable or invalid"
    )


def test_file_runtime_rejects_stale_secondary_compose_service_image(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="d" * 40,
    )
    platform_path = tmp_path / "docker-compose.platform.yml"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    platform["services"]["teaching-worker"]["image"] = "ghcr.io/example/stale@sha256:" + ("e" * 64)
    platform_path.write_text(json.dumps(platform), encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="d" * 40))

    assert result.layers["image_digests"].status == "fail"
    assert "production Compose" in result.layers["image_digests"].detail


@pytest.mark.parametrize("case", ("schema-version", "reference", "repository", "tag"))
def test_file_runtime_rejects_invalid_image_lock_contract(
    tmp_path: Path,
    case: str,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="b" * 40,
    )
    lock_path = tmp_path / "deploy" / "image-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if case == "schema-version":
        lock["schemaVersion"] = 0
    elif case == "reference":
        lock["images"]["deeptutor"]["reference"] = (
            "ghcr.io/xinlingzhifei/deeptutor:first-release@sha256:" + "c" * 64
        )
    else:
        record = lock["images"]["deeptutor"]
        if case == "repository":
            record["repository"] = "ghcr.io/example/deeptutor"
        else:
            record["tag"] = "latest"
        record["reference"] = f"{record['repository']}:{record['tag']}@{record['digest']}"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    if case in {"repository", "tag"}:
        platform_path = tmp_path / "docker-compose.platform.yml"
        platform = json.loads(platform_path.read_text(encoding="utf-8"))
        for service in platform["services"].values():
            if service.get("image", "").endswith(str(record["digest"])):
                service["image"] = record["reference"]
        platform_path.write_text(json.dumps(platform), encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="b" * 40))

    assert result.layers["image_digests"].status == "fail"
    assert "image lock" in result.layers["image_digests"].detail


def test_report_payload_binds_candidate_and_evidence_bundle_sha256(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, _, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="4" * 40,
    )
    expected_bundle_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="4" * 40))
    payload = module.report_payload(result)

    assert payload["candidate"] == candidate
    assert payload["releaseRun"] == _RELEASE_RUN
    assert payload["evidenceBundleSha256"] == expected_bundle_sha256


def test_file_runtime_requires_release_run_and_environment_identity(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="5" * 40,
    )
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document.pop("releaseRun")
    manifest.write_text(json.dumps(document), encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="5" * 40))

    assert result.layers["source_head"].status == "fail"
    assert "release run" in result.layers["source_head"].detail


def test_file_runtime_rejects_artifact_from_another_run_or_environment(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="6" * 40,
    )
    entry = evidence["teacher_flow"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["releaseRun"] = {
        "runId": "other-run",
        "environmentId": "other-environment",
    }
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_document["evidence"] = evidence
    manifest.write_text(json.dumps(manifest_document), encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="6" * 40))

    assert result.layers["teacher_flow"].status == "fail"
    assert "release run" in result.layers["teacher_flow"].detail


def test_file_runtime_rejects_minimal_self_attested_artifact(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="7" * 40,
    )
    entry = evidence["teacher_flow"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact.pop("receipt")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["evidence"] = evidence
    manifest.write_text(json.dumps(document), encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="7" * 40))

    assert result.layers["teacher_flow"].status == "fail"
    assert "receipt" in result.layers["teacher_flow"].detail


@pytest.mark.parametrize(
    "case",
    (
        "native-exit-one",
        "float-exit",
        "invalid-observed-at",
        "meaningless-result",
        "wrong-producer",
        "wrong-layer-check",
    ),
)
def test_file_runtime_rejects_semantically_invalid_receipt(
    tmp_path: Path,
    case: str,
) -> None:
    module = _load_verifier()
    manifest, evidence, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    entry = evidence["teacher_flow"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    receipt = artifact["receipt"]
    if case == "native-exit-one":
        receipt["result"] = {"nativeExit": 1}
    elif case == "float-exit":
        receipt["result"]["nativeExit"] = 0.0
    elif case == "invalid-observed-at":
        receipt["observedAt"] = "2026-99-99T99:99:99Z"
    elif case == "meaningless-result":
        receipt["result"] = {"claimed": True}
    elif case == "wrong-producer":
        receipt["producer"] = "untrusted-producer"
    else:
        receipt["result"] = {
            "nativeExit": 0,
            "outcome": "pass",
            "checks": {"studentFullFlow": True},
        }
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["evidence"] = evidence
    manifest.write_text(json.dumps(document), encoding="utf-8")

    result = module.verify(module.FileReleaseRuntime(manifest, expected_source_head="a" * 40))

    assert result.layers["teacher_flow"].status == "fail"
    assert "receipt" in result.layers["teacher_flow"].detail


def test_verify_rejects_all_pass_runtime_without_candidate_binding() -> None:
    module = _load_verifier()
    runtime = FakeRuntime()
    runtime.candidate = None
    runtime.release_run = None
    runtime.evidence_bundle_sha256 = None
    for name in module.REQUIRED_LAYERS:
        runtime.set_result(name, "pass")

    result = module.verify(runtime)

    assert result.ok is False
    assert "source_head" in result.failed
    assert "image_digests" in result.failed


def test_verify_fails_closed_when_runtime_identity_probe_raises() -> None:
    module = _load_verifier()
    valid_runtime = FakeRuntime()
    for name in module.REQUIRED_LAYERS:
        valid_runtime.set_result(name, "pass")

    class BrokenIdentityRuntime:
        release_run = valid_runtime.release_run
        evidence_bundle_sha256 = valid_runtime.evidence_bundle_sha256

        def result(self, name: str):
            return valid_runtime.result(name)

        @property
        def candidate(self):
            raise RuntimeError("candidate probe failed")

    result = module.verify(BrokenIdentityRuntime())

    assert result.ok is False
    assert "source_head" in result.failed
    assert "image_digests" in result.failed


def test_main_rechecks_source_tree_after_reading_evidence_bundle(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_verifier()
    source_head = "e" * 40
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head=source_head,
    )
    observed_heads = iter((source_head, ""))
    monkeypatch.setattr(module, "_git_head", lambda: next(observed_heads))

    exit_code = module.main(["--evidence", str(manifest), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["status"] == "not_ready"
    assert payload["layers"]["source_head"]["status"] == "fail"
    assert "changed during verification" in payload["layers"]["source_head"]["detail"]


def test_main_verifies_candidate_artifact_outside_the_clean_source_root(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_verifier()
    source_head = "e" * 40
    candidate_root = tmp_path / "candidate-artifact"
    candidate_root.mkdir()
    manifest, _, _ = _write_complete_bundle(
        candidate_root,
        module,
        source_head=source_head,
    )
    clean_source_root = tmp_path / "clean-source"
    clean_source_root.mkdir()
    module.PROJECT_ROOT = clean_source_root
    monkeypatch.setattr(module, "_git_head", lambda: source_head)

    exit_code = module.main(
        [
            "--evidence",
            str(manifest),
            "--candidate-root",
            str(candidate_root),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ready"
    assert payload["candidate"]["sourceHead"] == source_head


def test_git_head_rejects_a_dirty_source_candidate(monkeypatch) -> None:
    module = _load_verifier()
    calls: list[tuple[str, ...]] = []

    def run(argv, **_options):
        calls.append(tuple(argv))
        if argv[1:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="8" * 40 + "\n")
        if argv[1:] == ["status", "--porcelain=v1", "--untracked-files=normal"]:
            return SimpleNamespace(returncode=0, stdout=" M scripts/release.py\n")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module._git_head() == ""
    assert calls == [
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain=v1", "--untracked-files=normal"),
    ]


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
