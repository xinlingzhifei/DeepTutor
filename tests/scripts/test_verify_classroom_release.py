from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

_RELEASE_RUN = {
    "runId": "first-release-run-20260824",
    "environmentId": "test-environment",
}
_SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
_OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
_LIVE_FIXTURE_TOKEN = "classroom-verifier-fixture-token"
_GATEWAY_DOCKER_HOST_IDENTITY_SHA256 = "7" * 64
_OPENMAIC_OBSERVER_ID = "shared-ingress-observer-openmaic-01"
_OPENMAIC_OBSERVER_ORIGIN = "https://observer.example.test"
_OPENMAIC_CONTROL_ORIGIN = "https://shared-ingress.example.test"


@pytest.fixture(autouse=True)
def _live_fixture_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", _LIVE_FIXTURE_TOKEN)
    for name in (
        "YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256",
        "YFEISTAI_GATEWAY_TRUST_KEYRING",
        "YFEISTAI_GATEWAY_TRUST_KEYRING_SHA256",
        "YFEISTAI_GATEWAY_OBSERVER_CHALLENGE",
        "YFEISTAI_GATEWAY_HOST_CHALLENGE",
        "YFEISTAI_GATEWAY_TRUSTED_NOW",
        "YFEISTAI_OPENMAIC_EXPECTED_OBSERVER_ATTESTATION_SHA256",
        "YFEISTAI_OPENMAIC_EXPECTED_OBSERVER_ID",
        "YFEISTAI_OPENMAIC_EXPECTED_OBSERVER_ORIGIN",
        "YFEISTAI_OPENMAIC_EXPECTED_SHARED_INGRESS_CONTROL_ORIGIN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_verifier_cli_starts_directly_from_repository_root() -> None:
    root = Path(__file__).parents[2]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / "verify_classroom_release.py"), "--help"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert "--evidence" in completed.stdout


def _load_verifier():
    path = Path(__file__).parents[2] / "scripts" / "verify_classroom_release.py"
    spec = importlib.util.spec_from_file_location("task7_verify_classroom_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_classroom_export_support():
    path = Path(__file__).parent / "test_classroom_export_contract.py"
    spec = importlib.util.spec_from_file_location(
        "classroom_export_contract_support_for_release_verifier",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_tenant_isolation_support():
    path = Path(__file__).parent / "test_tenant_isolation_contract.py"
    spec = importlib.util.spec_from_file_location(
        "tenant_isolation_contract_support_for_release_verifier",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_gateway_public_support():
    path = Path(__file__).parent / "gateway_public_test_support.py"
    spec = importlib.util.spec_from_file_location(
        "gateway_public_test_support_for_release_verifier",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_gateway_public_contract():
    path = Path(__file__).parents[2] / "scripts" / "gateway_public_contract.py"
    spec = importlib.util.spec_from_file_location(
        "gateway_public_contract_for_release_verifier",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_backup_restore_support():
    path = Path(__file__).parent / "test_backup_restore_probe.py"
    spec = importlib.util.spec_from_file_location(
        "backup_restore_probe_support_for_release_verifier",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _gateway_trusted_root(bundle_root: Path) -> Path:
    return bundle_root.parent / f"{bundle_root.name}-trusted-gateway-controller"


def _gateway_trust_pair_from_bundle(bundle_root: Path) -> dict[str, object]:
    support = _load_gateway_public_support()
    keyring_path = _gateway_trusted_root(bundle_root) / "gateway-trust-keyring.json"
    observer_attestation_path = bundle_root / support.GATEWAY_OBSERVER_ATTESTATION_ARTIFACT
    observer_envelope_path = bundle_root / support.GATEWAY_OBSERVER_TRUST_ENVELOPE_ARTIFACT
    host_envelope_path = bundle_root / support.GATEWAY_HOST_TRUST_ENVELOPE_ARTIFACT
    host_receipt_path = bundle_root / support.GATEWAY_HOST_PROVISIONING_RECEIPT_ARTIFACT
    paths = {
        "keyring": keyring_path,
        "observer_attestation": observer_attestation_path,
        "observer_envelope": observer_envelope_path,
        "host_envelope": host_envelope_path,
        "host_receipt": host_receipt_path,
    }
    bodies = {name: path.read_bytes() for name, path in paths.items()}
    return {
        "keyring_path": keyring_path,
        "keyring_sha256": hashlib.sha256(bodies["keyring"]).hexdigest(),
        "observer_challenge": support.GATEWAY_OBSERVER_CHALLENGE,
        "host_challenge": support.GATEWAY_HOST_CHALLENGE,
        "trusted_now": support.TRUSTED_NOW,
        "observer_attestation_path": observer_attestation_path,
        "observer_attestation_sha256": hashlib.sha256(bodies["observer_attestation"]).hexdigest(),
        "observer_envelope_path": observer_envelope_path,
        "observer_envelope_sha256": hashlib.sha256(bodies["observer_envelope"]).hexdigest(),
        "host_envelope_path": host_envelope_path,
        "host_envelope_sha256": hashlib.sha256(bodies["host_envelope"]).hexdigest(),
        "host_receipt_path": host_receipt_path,
        "host_receipt": json.loads(bodies["host_receipt"]),
        "host_receipt_sha256": hashlib.sha256(bodies["host_receipt"]).hexdigest(),
    }


def _gateway_runtime_arguments(bundle_root: Path) -> dict[str, object]:
    support = _load_gateway_public_support()
    trust_pair = _gateway_trust_pair_from_bundle(bundle_root)
    return {
        "expected_gateway_docker_host_identity_sha256": trust_pair["host_receipt_sha256"],
        **support.gateway_trust_arguments(trust_pair),
    }


def _openmaic_observer_runtime_arguments(bundle_root: Path) -> dict[str, str]:
    observer_path = bundle_root / "runtime" / "openmaic-shared-ingress-observer-attestation.json"
    return {
        "expected_openmaic_observer_attestation_sha256": hashlib.sha256(
            observer_path.read_bytes()
        ).hexdigest(),
        "expected_openmaic_observer_id": _OPENMAIC_OBSERVER_ID,
        "expected_openmaic_observer_origin": _OPENMAIC_OBSERVER_ORIGIN,
        "expected_openmaic_shared_ingress_control_origin": _OPENMAIC_CONTROL_ORIGIN,
    }


def _complete_bundle_runtime(module, manifest: Path, **kwargs):
    return module.FileReleaseRuntime(
        manifest,
        expected_outage_docker_host_identity_sha256=(_GATEWAY_DOCKER_HOST_IDENTITY_SHA256),
        **kwargs,
        **_gateway_runtime_arguments(manifest.parent),
        **_openmaic_observer_runtime_arguments(manifest.parent),
    )


def _gateway_cli_arguments(bundle_root: Path) -> list[str]:
    arguments = _gateway_runtime_arguments(bundle_root)
    observer_arguments = _openmaic_observer_runtime_arguments(bundle_root)
    return [
        "--outage-docker-host-identity-sha256",
        _GATEWAY_DOCKER_HOST_IDENTITY_SHA256,
        "--gateway-docker-host-identity-sha256",
        str(arguments["expected_gateway_docker_host_identity_sha256"]),
        "--gateway-trust-keyring",
        str(arguments["trusted_keyring_path"]),
        "--gateway-trust-keyring-sha256",
        str(arguments["expected_trusted_keyring_sha256"]),
        "--gateway-observer-challenge",
        str(arguments["expected_observer_challenge"]),
        "--gateway-host-challenge",
        str(arguments["expected_host_challenge"]),
        "--gateway-trusted-now",
        str(arguments["trusted_now"]),
        "--openmaic-observer-attestation-sha256",
        observer_arguments["expected_openmaic_observer_attestation_sha256"],
        "--openmaic-observer-id",
        observer_arguments["expected_openmaic_observer_id"],
        "--openmaic-observer-origin",
        observer_arguments["expected_openmaic_observer_origin"],
        "--openmaic-shared-ingress-control-origin",
        observer_arguments["expected_openmaic_shared_ingress_control_origin"],
    ]


def _static_openmaic_cli_arguments() -> list[str]:
    return [
        "--openmaic-observer-attestation-sha256",
        "9" * 64,
        "--openmaic-observer-id",
        _OPENMAIC_OBSERVER_ID,
        "--openmaic-observer-origin",
        _OPENMAIC_OBSERVER_ORIGIN,
        "--openmaic-shared-ingress-control-origin",
        _OPENMAIC_CONTROL_ORIGIN,
    ]


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


def test_learning_event_idempotency_uses_capacity_probe_contract() -> None:
    module = _load_verifier()

    assert module.RECEIPT_CONTRACTS["learning_event_idempotency"] == (
        "classroom-capacity-probe",
        ("duplicateCountedOnce", "ticketReplayRejected", "projectionVisible"),
    )


def test_classroom_exports_uses_the_fixed_export_probe_contract() -> None:
    module = _load_verifier()

    assert module.RECEIPT_CONTRACTS["classroom_exports"] == (
        "classroom-export-probe",
        ("zipOpened", "pptxOpened", "offlineHtmlOpened", "mp4Opened"),
    )


def test_tenant_isolation_uses_the_fixed_probe_contract() -> None:
    module = _load_verifier()

    assert module.RECEIPT_CONTRACTS["tenant_isolation"] == (
        "tenant-isolation-probe",
        ("databaseIsolated", "objectsIsolated", "exportsIsolated", "eventsIsolated"),
    )


def test_backup_restore_receipt_without_canonical_provenance_is_rejected(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    candidate = _candidate("a" * 40)
    document = _artifact_document(module, candidate, "backup_restore")

    error = module.probe_provenance_error(
        document,
        evidence="backup_restore",
        candidate=candidate,
        release_run=_RELEASE_RUN,
        bundle_root=tmp_path,
        candidate_root=tmp_path,
    )

    assert error == "backup restore execution proof is missing or invalid"


@pytest.mark.parametrize(
    ("artifact_name", "expected_error"),
    (
        ("restore-validation.json", "backup restore operator artifact digest does not match"),
        ("target-config.snapshot.json", "backup restore target config digest does not match"),
        (
            "target-provisioning-receipt.json",
            "backup restore target provisioning receipt digest does not match",
        ),
        ("source-provenance.json", "backup restore source provenance digest does not match"),
    ),
)
def test_backup_restore_receipt_replays_every_canonical_artifact(
    tmp_path: Path,
    artifact_name: str,
    expected_error: str,
) -> None:
    module = _load_verifier()
    candidate = _candidate("a" * 40)
    _write_candidate_files(tmp_path, candidate)
    report_path = _load_backup_restore_support().write_release_probe_fixture(
        tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
    )
    provenance = {
        "backupRestoreReport": {
            "artifact": "runtime/backup-restore/backup-restore-report.json",
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        }
    }
    document = _artifact_document(
        module,
        candidate,
        "backup_restore",
        provenance=provenance,
    )
    document["receipt"]["observedAt"] = json.loads(report_path.read_bytes())["observedAt"]
    arguments = {
        "evidence": "backup_restore",
        "candidate": candidate,
        "release_run": _RELEASE_RUN,
        "bundle_root": tmp_path,
        "candidate_root": tmp_path,
    }
    assert module.probe_provenance_error(document, **arguments) is None

    artifact_path = tmp_path / "runtime" / "backup-restore" / artifact_name
    artifact_path.write_bytes(artifact_path.read_bytes() + b" ")

    assert module.probe_provenance_error(document, **arguments) == expected_error


def test_backup_restore_verifier_uses_owned_source_archive_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    candidate = _candidate("a" * 40)
    _write_candidate_files(tmp_path, candidate)
    report_path = _load_backup_restore_support().write_release_probe_fixture(
        tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
    )
    provenance = {
        "backupRestoreReport": {
            "artifact": "runtime/backup-restore/backup-restore-report.json",
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        }
    }
    document = _artifact_document(
        module,
        candidate,
        "backup_restore",
        provenance=provenance,
    )
    document["receipt"]["observedAt"] = json.loads(report_path.read_bytes())["observedAt"]
    loaded_paths: list[Path] = []
    original_loader = module.load_verified_backup

    def load_verified_backup(path: Path):
        loaded_paths.append(Path(path))
        return original_loader(path)

    monkeypatch.setattr(module, "load_verified_backup", load_verified_backup)

    assert (
        module.probe_provenance_error(
            document,
            evidence="backup_restore",
            candidate=candidate,
            release_run=_RELEASE_RUN,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
        )
        is None
    )
    assert loaded_paths
    assert all(not path.is_relative_to(tmp_path) for path in loaded_paths)


def test_backup_restore_verifier_rejects_subtree_replacement_during_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    candidate = _candidate("a" * 40)
    _write_candidate_files(tmp_path, candidate)
    report_path = _load_backup_restore_support().write_release_probe_fixture(
        tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
    )
    provenance = {
        "backupRestoreReport": {
            "artifact": "runtime/backup-restore/backup-restore-report.json",
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        }
    }
    document = _artifact_document(
        module,
        candidate,
        "backup_restore",
        provenance=provenance,
    )
    document["receipt"]["observedAt"] = json.loads(report_path.read_bytes())["observedAt"]
    evidence_root = tmp_path / "runtime" / "backup-restore"
    replacement = tmp_path / "runtime" / "backup-restore.replacement"
    displaced = tmp_path / "runtime" / "backup-restore.displaced"
    shutil.copytree(evidence_root, replacement)
    original_parser = module.parse_backup_restore_report

    def replace_before_return(*args: object, **kwargs: object):
        parsed = original_parser(*args, **kwargs)
        evidence_root.replace(displaced)
        replacement.replace(evidence_root)
        return parsed

    monkeypatch.setattr(module, "parse_backup_restore_report", replace_before_return)

    error = module.probe_provenance_error(
        document,
        evidence="backup_restore",
        candidate=candidate,
        release_run=_RELEASE_RUN,
        bundle_root=tmp_path,
        candidate_root=tmp_path,
    )

    assert error == "backup restore evidence boundary changed during replay"


def _playwright_report(
    evidence: str,
    *,
    count: int,
) -> dict[str, object]:
    file = "tests/e2e/classroom-first-release.live.spec.ts"
    tailwind_titles = [
        (
            "[release-evidence:tailwind4_visual_matrix] "
            f"route={route} viewport={viewport} appearance={appearance}"
        )
        for route in (
            "/login",
            "/home",
            "/knowledge",
            "/settings/appearance",
            "/settings/llm",
            "/space/learning",
        )
        for viewport in ("desktop", "mobile")
        for appearance in ("snow", "light", "dark", "glass")
    ]
    specs = []
    for index in range(count):
        title = (
            tailwind_titles[index]
            if evidence == "tailwind4_visual_matrix" and index < len(tailwind_titles)
            else f"[release-evidence:{evidence}] case {index + 1}"
        )
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


@pytest.mark.parametrize(
    "carrier",
    (
        "stdout-text",
        "stdout-buffer",
        "stderr",
        "attachment-body",
        "steps",
        "test-annotations",
        "result-annotations",
        "project-metadata",
    ),
)
def test_derive_probe_checks_rejects_playwright_data_bearing_result_fields(
    carrier: str,
) -> None:
    module = _load_verifier()
    report = _playwright_report("teacher_flow", count=1)
    test = _first_probe_test(report)
    results = test["results"]
    assert isinstance(results, list) and results
    result = results[0]
    assert isinstance(result, dict)
    if carrier == "stdout-text":
        result["stdout"] = [{"text": "sensitive output"}]
    elif carrier == "stdout-buffer":
        result["stdout"] = [{"buffer": "c2Vuc2l0aXZlIG91dHB1dA=="}]
    elif carrier == "stderr":
        result["stderr"] = [{"text": "sensitive error"}]
    elif carrier == "attachment-body":
        result["attachments"] = [{"name": "trace", "body": "c2VjcmV0"}]
    elif carrier == "steps":
        result["steps"] = []
    elif carrier == "test-annotations":
        test["annotations"] = [{"type": "secret"}]
    elif carrier == "result-annotations":
        result["annotations"] = [{"type": "secret"}]
    else:
        config = report["config"]
        assert isinstance(config, dict)
        projects = config["projects"]
        assert isinstance(projects, list) and projects
        project = projects[0]
        assert isinstance(project, dict)
        project["metadata"] = {"secret": "value"}

    with pytest.raises(ValueError, match="persistence boundary"):
        module.derive_probe_checks(
            "teacher_flow",
            raw_report=json.dumps(report).encode(),
            candidate=_candidate("a" * 40),
            release_run=_RELEASE_RUN,
        )


def test_derive_probe_checks_rejects_duplicate_tailwind_matrix_titles() -> None:
    module = _load_verifier()
    report = _playwright_report("tailwind4_visual_matrix", count=48)
    suites = report["suites"]
    assert isinstance(suites, list) and suites
    suite = suites[0]
    assert isinstance(suite, dict)
    specs = suite["specs"]
    assert isinstance(specs, list) and len(specs) == 48
    first = specs[0]
    duplicate = specs[-1]
    assert isinstance(first, dict) and isinstance(duplicate, dict)
    duplicate["title"] = first["title"]

    with pytest.raises(ValueError, match="fixed recipe"):
        module.derive_probe_checks(
            "tailwind4_visual_matrix",
            raw_report=json.dumps(report).encode(),
            candidate=_candidate("a" * 40),
            release_run=_RELEASE_RUN,
        )


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


def _openmaic_shared_report(
    candidate: dict[str, object],
    *,
    runtime_attestation_sha256: str,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "producer": "openmaic-smoke",
        "plane": "shared",
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "observedAt": "2026-08-24T00:00:00Z",
        "baseUrl": "https://candidate.example.test",
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_attestation_sha256,
        },
        "fixture": {
            "tenantId": "tenant-openmaic-shared-01",
            "teacherUserId": "teacher-openmaic-shared-01",
            "courseId": "course-openmaic-shared-01",
            "classId": "class-openmaic-shared-01",
        },
        "binding": {
            "routeId": "shared-primary",
            "providerProfileId": "platform-default",
            "workerPoolRef": "shared-generation",
            "queueRef": "openmaic.shared",
        },
        "generation": {
            "jobId": "job-openmaic-shared-01",
            "jobStatus": "succeeded",
            "assetId": "asset-openmaic-shared-01",
            "classroomStatus": "succeeded",
            "classroomVersionId": "version-openmaic-shared-01",
            "documentSha256": "b" * 64,
            "documentSizeBytes": 4096,
            "documentEtag": '"sha256-' + "b" * 64 + '"',
        },
    }


def _openmaic_dedicated_report(
    candidate: dict[str, object],
    *,
    runtime_attestation_sha256: str,
) -> dict[str, object]:
    report = _openmaic_shared_report(
        candidate,
        runtime_attestation_sha256=runtime_attestation_sha256,
    )
    report["plane"] = "dedicated"
    report["fixture"] = {
        "tenantId": "tenant-openmaic-dedicated-01",
        "teacherUserId": "teacher-openmaic-dedicated-01",
        "courseId": "course-openmaic-dedicated-01",
        "classId": "class-openmaic-dedicated-01",
    }
    report["binding"] = {
        "routeId": "dedicated-tenant-openmaic-01",
        "routeTenantId": "tenant-openmaic-dedicated-01",
        "routeOwnerKey": "tenant-openmaic-dedicated-01",
        "providerProfileId": "provider-tenant-openmaic-01",
        "providerScope": "dedicated",
        "providerTenantId": "tenant-openmaic-dedicated-01",
        "providerOwnerKey": "tenant-openmaic-dedicated-01",
        "workerPoolRef": "generation-tenant-openmaic-01",
        "queueRef": "openmaic.tenant-openmaic-01",
        "attemptCount": 2,
        "sharedRouteAttemptCount": 0,
        "dedicatedRouteAttemptCount": 2,
        "selectedRouteAttemptCount": 1,
        "unavailableRouteAttemptCount": 1,
        "routeAttemptHistoryComplete": True,
    }
    return report


def _openmaic_dedicated_outage_attestation(
    candidate: dict[str, object],
    *,
    runtime_attestation_sha256: str,
    observer_attestation_sha256: str,
    attempt_marker_reference: dict[str, object],
) -> dict[str, object]:
    tenant_id = "tenant-openmaic-dedicated-01"
    route_id = "dedicated-tenant-openmaic-01"
    observer_trust_anchor = {
        "sha256": observer_attestation_sha256,
        "observerId": "shared-ingress-observer-openmaic-01",
        "observerOrigin": "https://observer.example.test",
        "sharedIngressControlOrigin": "https://shared-ingress.example.test",
    }
    report = {
        "schemaVersion": 1,
        "producer": "openmaic-dedicated-outage",
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "observedAt": "2026-08-24T00:00:01Z",
        "baseUrl": "https://candidate.example.test",
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_attestation_sha256,
        },
        "observerAttestation": {
            "artifact": "runtime/openmaic-shared-ingress-observer-attestation.json",
            "sha256": observer_attestation_sha256,
            "observerId": "shared-ingress-observer-openmaic-01",
            "observerOrigin": "https://observer.example.test",
            "sharedIngressControlOrigin": "https://shared-ingress.example.test",
        },
        "fixture": {
            "tenantId": tenant_id,
            "attemptMarker": attempt_marker_reference,
            "cleanupBoundary": {
                "reason": "formal-delete-api-unavailable",
                "reversibleResourcesDeleted": [
                    "classEnrollment",
                    "tenantMembership",
                    "teacherIdentity",
                ],
                "retainedAuditResources": [
                    {"resourceType": "course", "resourceId": "course-outage-01"},
                    {"resourceType": "class", "resourceId": "class-outage-01"},
                    {"resourceType": "generationQuotaGrant", "resourceId": tenant_id},
                    {"resourceType": "classroomAsset", "resourceId": "asset-outage-01"},
                    {
                        "resourceType": "generationJob",
                        "resourceId": "job-openmaic-dedicated-outage-01",
                    },
                    {"resourceType": "classroomAsset", "resourceId": "asset-canary-01"},
                    {
                        "resourceType": "generationJob",
                        "resourceId": "job-openmaic-dedicated-canary-01",
                    },
                ],
            },
        },
        "provenance": {
            "attemptMarker": attempt_marker_reference,
            "observerTrustAnchor": observer_trust_anchor,
            "dockerBoundary": {
                "dockerHostIdentitySha256": _GATEWAY_DOCKER_HOST_IDENTITY_SHA256,
                "daemonIdentityBeforeSha256": "2" * 64,
                "daemonIdentityAfterSha256": "2" * 64,
                "inventoryBeforeSha256": "3" * 64,
                "inventoryAfterSha256": "3" * 64,
            },
        },
        "outage": {
            "dedicatedPlaneStopped": True,
            "routeId": route_id,
            "jobId": "job-openmaic-dedicated-outage-01",
            "jobStatus": "failed",
            "errorCode": "dedicated_data_plane_unavailable",
            "attemptCount": 2,
            "sharedRouteAttemptCount": 0,
            "dedicatedRouteAttemptCount": 2,
            "selectedRouteAttemptCount": 2,
            "unavailableRouteAttemptCount": 0,
            "routeAttemptHistoryComplete": True,
        },
        "sharedIngress": {
            "observationId": "shared-ingress-openmaic-dedicated-outage-01",
            "requestCountBefore": 7,
            "requestCountAfter": 7,
        },
        "restoration": {
            "dedicatedPlaneRestored": True,
            "routeId": route_id,
            "canaryJobId": "job-openmaic-dedicated-canary-01",
            "canaryJobStatus": "succeeded",
            "attemptCount": 1,
            "sharedRouteAttemptCount": 0,
            "dedicatedRouteAttemptCount": 1,
            "selectedRouteAttemptCount": 1,
            "unavailableRouteAttemptCount": 0,
            "routeAttemptHistoryComplete": True,
        },
    }
    child_stdout = (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    report["execution"] = {
        "command": {
            "runner": "python",
            "script": "scripts/openmaic_dedicated_outage_probe.py",
            "arguments": ["--profile", "first-release"],
        },
        "nativeExit": 0,
        "stdoutSha256": hashlib.sha256(child_stdout).hexdigest(),
        "stderrSha256": hashlib.sha256(b"").hexdigest(),
    }
    return report


def _openmaic_dedicated_outage_attempt_marker(
    candidate: dict[str, object],
    *,
    observer_attestation_sha256: str,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "producer": "openmaic-dedicated-outage-attempt",
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "observerTrustAnchor": {
            "sha256": observer_attestation_sha256,
            "observerId": "shared-ingress-observer-openmaic-01",
            "observerOrigin": "https://observer.example.test",
            "sharedIngressControlOrigin": "https://shared-ingress.example.test",
        },
        "fixturePlan": {
            "tenantId": "tenant-openmaic-dedicated-01",
            "routeId": "dedicated-tenant-openmaic-01",
            "cleanupBoundary": "identity-membership-enrollment-only",
            "retainedResourceTypes": [
                "course",
                "class",
                "generationQuotaGrant",
                "classroomAsset",
                "generationJob",
            ],
        },
    }


def _openmaic_shared_ingress_observer_attestation() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "producer": "openmaic-shared-ingress-observer",
        "releaseRun": _RELEASE_RUN,
        "observedAt": "2026-08-24T00:00:00Z",
        "observer": {
            "observerId": "shared-ingress-observer-openmaic-01",
            "observerUrl": "https://observer.example.test",
            "sharedIngressControlUrl": ("https://shared-ingress.example.test/v1/control-canaries"),
        },
    }


def _write_probe_proof(
    tmp_path: Path,
    module,
    candidate: dict[str, object],
    evidence: str,
    *,
    gateway_trust_pair: dict[str, object] | None = None,
) -> dict[str, object] | None:
    if evidence == "running_containers":
        attestation_path = tmp_path / "runtime" / "runtime-attestation.json"
        return {
            "runtimeAttestation": {
                "artifact": "runtime/runtime-attestation.json",
                "sha256": hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
            }
        }
    if evidence in {"database_revisions", "service_health"}:
        attestation_path = _write_platform_preflight_attestation(tmp_path, candidate)
        return {
            "platformPreflightAttestation": {
                "artifact": "runtime/platform-preflight-attestation.json",
                "sha256": hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
            }
        }
    if evidence in {"capacity_profile", "learning_event_idempotency"}:
        report = _capacity_profile_report(candidate)
        report_body = (
            json.dumps(
                report,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        parsed = module.parse_capacity_profile_report(
            report_body,
            candidate=candidate,
            release_run=_RELEASE_RUN,
            expected_base_url="https://candidate.example.test",
        )
        runtime_path = tmp_path / "runtime" / "runtime-attestation.json"
        attestation = {
            "schemaVersion": 1,
            "candidate": candidate,
            "releaseRun": _RELEASE_RUN,
            "observedAt": "2026-08-24T00:00:00Z",
            "baseUrl": "https://candidate.example.test",
            "runtimeAttestation": {
                "artifact": "runtime/runtime-attestation.json",
                "sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            },
            "execution": {
                "command": module.capacity_profile_command_record(),
                "nativeExit": 0,
                "stdout": report_body.decode(),
                "stdoutSha256": hashlib.sha256(report_body).hexdigest(),
                "stderr": "",
            },
            "summary": module.derive_capacity_profile_summary(parsed),
        }
        attestation_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
        attestation_path.write_text(json.dumps(attestation, sort_keys=True), encoding="utf-8")
        return {
            "capacityAttestation": {
                "artifact": "runtime/capacity-profile-attestation.json",
                "sha256": hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
            }
        }
    if evidence == "tenant_isolation":
        support = _load_tenant_isolation_support()
        contract = support._module()
        runtime_path = tmp_path / "runtime" / "runtime-attestation.json"
        capacity_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
        capacity_body = capacity_path.read_bytes()
        capacity_sha256 = hashlib.sha256(capacity_body).hexdigest()
        capacity_document = json.loads(capacity_body)
        capacity_execution = capacity_document["execution"]
        assert isinstance(capacity_execution, dict)
        capacity_report = json.loads(str(capacity_execution["stdout"]))
        capacity_completions = capacity_report["sessionCompletions"]
        assert isinstance(capacity_completions, list)
        capacity_tenant_ids = tuple(
            sorted(
                {
                    str(completion["tenantId"])
                    for completion in capacity_completions
                    if isinstance(completion, dict)
                }
            )
        )
        selected_tenant_ids = capacity_tenant_ids[:2]
        assert selected_tenant_ids == tuple(sorted(selected_tenant_ids))
        report = support._replace_string(
            support._report(),
            support.OWNER_TENANT_ID,
            selected_tenant_ids[0],
        )
        report = support._replace_string(
            report,
            support.FOREIGN_TENANT_ID,
            selected_tenant_ids[1],
        )
        assert isinstance(report, dict)
        report.update(
            candidate=candidate,
            releaseRun=_RELEASE_RUN,
            observedAt="2026-08-24T00:00:00Z",
            capacityProof={
                "reportSha256": capacity_sha256,
                "tenantIds": list(selected_tenant_ids),
            },
        )
        report_body = contract.canonical_tenant_isolation_report(report)
        parsed = contract.parse_tenant_isolation_report(
            report_body,
            candidate=candidate,
            release_run=_RELEASE_RUN,
            expected_base_url="https://candidate.example.test",
            expected_capacity_report_sha256=capacity_sha256,
            expected_capacity_tenant_ids=selected_tenant_ids,
            forbidden_secret_values=(),
        )
        assert contract.derive_tenant_isolation_checks(parsed) == {
            "databaseIsolated": True,
            "objectsIsolated": True,
            "exportsIsolated": True,
            "eventsIsolated": True,
        }
        attestation = {
            "schemaVersion": 1,
            "candidate": candidate,
            "releaseRun": _RELEASE_RUN,
            "observedAt": report["observedAt"],
            "baseUrl": report["baseUrl"],
            "runtimeAttestation": {
                "artifact": "runtime/runtime-attestation.json",
                "sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            },
            "capacityAttestation": {
                "artifact": "runtime/capacity-profile-attestation.json",
                "sha256": capacity_sha256,
            },
            "execution": {
                "command": contract.tenant_isolation_command_record(),
                "nativeExit": 0,
                "stdout": report_body.decode("utf-8"),
                "stdoutSha256": hashlib.sha256(report_body).hexdigest(),
                "stderr": "",
            },
        }
        attestation_path = tmp_path / "runtime" / "tenant-isolation-attestation.json"
        attestation_path.write_text(json.dumps(attestation, sort_keys=True), encoding="utf-8")
        return {
            "tenantIsolationAttestation": {
                "artifact": "runtime/tenant-isolation-attestation.json",
                "sha256": hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
            }
        }
    if evidence in {"openmaic_shared_plane", "openmaic_dedicated_plane"}:
        plane = "shared" if evidence == "openmaic_shared_plane" else "dedicated"
        runtime_path = tmp_path / "runtime" / "runtime-attestation.json"
        runtime_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
        report_factory = (
            _openmaic_shared_report if plane == "shared" else _openmaic_dedicated_report
        )
        report = report_factory(
            candidate,
            runtime_attestation_sha256=runtime_sha256,
        )
        stdout = (
            json.dumps(
                report,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        proof = {
            "schemaVersion": 1,
            "candidate": candidate,
            "releaseRun": _RELEASE_RUN,
            "observedAt": report["observedAt"],
            "baseUrl": report["baseUrl"],
            "runtimeAttestation": report["runtimeAttestation"],
            "execution": {
                "command": {
                    "runner": "python",
                    "script": "scripts/openmaic_smoke_probe.py",
                    "arguments": ["--plane", plane, "--profile", "first-release"],
                },
                "nativeExit": 0,
                "stdout": stdout.decode("utf-8"),
                "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
                "stderr": "",
            },
            "summary": {
                "fixture": report["fixture"],
                "binding": report["binding"],
                "generation": report["generation"],
                "checks": (
                    {"sharedGenerationPassed": True}
                    if plane == "shared"
                    else {
                        "dedicatedGenerationPassed": True,
                        "noSharedClientIssued": True,
                    }
                ),
            },
        }
        proof_name = f"openmaic-{plane}-plane-attestation.json"
        proof_path = tmp_path / "runtime" / proof_name
        proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
        provenance_key = (
            "openmaicSharedPlaneAttestation"
            if plane == "shared"
            else "openmaicDedicatedPlaneAttestation"
        )
        provenance = {
            provenance_key: {
                "artifact": f"runtime/{proof_name}",
                "sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
            }
        }
        if plane == "dedicated":
            observer_path = (
                tmp_path / "runtime" / "openmaic-shared-ingress-observer-attestation.json"
            )
            observer_body = (
                json.dumps(
                    _openmaic_shared_ingress_observer_attestation(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            observer_path.write_bytes(observer_body)
            observer_sha256 = hashlib.sha256(observer_body).hexdigest()
            marker = _openmaic_dedicated_outage_attempt_marker(
                candidate,
                observer_attestation_sha256=observer_sha256,
            )
            marker_body = (
                json.dumps(
                    marker,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            marker_path = tmp_path / "runtime" / "openmaic-dedicated-outage-attempt.json"
            marker_path.write_bytes(marker_body)
            marker_reference = {
                "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
                "sha256": hashlib.sha256(marker_body).hexdigest(),
            }
            outage_path = tmp_path / "runtime" / "openmaic-dedicated-outage-attestation.json"
            outage = _openmaic_dedicated_outage_attestation(
                candidate,
                runtime_attestation_sha256=runtime_sha256,
                observer_attestation_sha256=observer_sha256,
                attempt_marker_reference=marker_reference,
            )
            outage_body = (
                json.dumps(
                    outage,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            outage_path.write_bytes(outage_body)
            provenance["openmaicDedicatedOutageAttestation"] = {
                "artifact": "runtime/openmaic-dedicated-outage-attestation.json",
                "sha256": hashlib.sha256(outage_body).hexdigest(),
            }
            provenance["openmaicSharedIngressObserverAttestation"] = {
                "artifact": "runtime/openmaic-shared-ingress-observer-attestation.json",
                "sha256": observer_sha256,
            }
        return provenance
    if evidence == "classroom_exports":
        support = _load_classroom_export_support()
        raw_root = tmp_path / "raw" / "classroom-exports"
        support._write_valid_artifacts(raw_root)
        report = support._report(raw_root)
        report.update(
            candidate=candidate,
            releaseRun=_RELEASE_RUN,
            observedAt="2026-08-24T00:00:00Z",
            baseUrl="https://candidate.example.test",
            tenantId="tenant-00",
        )
        stdout = support._module().canonical_classroom_export_report(report)
        runtime_path = tmp_path / "runtime" / "runtime-attestation.json"
        capacity_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
        proof = {
            "schemaVersion": 1,
            "candidate": candidate,
            "releaseRun": _RELEASE_RUN,
            "observedAt": report["observedAt"],
            "baseUrl": report["baseUrl"],
            "tenantId": report["tenantId"],
            "runtimeAttestation": {
                "artifact": "runtime/runtime-attestation.json",
                "sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            },
            "capacityAttestation": {
                "artifact": "runtime/capacity-profile-attestation.json",
                "sha256": hashlib.sha256(capacity_path.read_bytes()).hexdigest(),
            },
            "execution": {
                "command": module.classroom_exports_command_record(),
                "nativeExit": 0,
                "stdout": stdout.decode("utf-8"),
                "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
                "stderr": "",
            },
            "rawArtifacts": {
                kind: {
                    "artifact": f"raw/classroom-exports/{relative_path}",
                    "sha256": hashlib.sha256((raw_root / relative_path).read_bytes()).hexdigest(),
                    "sizeBytes": (raw_root / relative_path).stat().st_size,
                }
                for kind, relative_path in module.CLASSROOM_EXPORT_PATHS.items()
            },
        }
        proof_path = tmp_path / "runtime" / "classroom-exports-attestation.json"
        proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
        return {
            "classroomExportsAttestation": {
                "artifact": "runtime/classroom-exports-attestation.json",
                "sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
            }
        }
    if evidence == "backup_restore":
        report_path = _load_backup_restore_support().write_release_probe_fixture(
            tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )
        return {
            "backupRestoreReport": {
                "artifact": "runtime/backup-restore/backup-restore-report.json",
                "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            }
        }
    if evidence == "gateway_only_public":
        assert gateway_trust_pair is not None
        support = _load_gateway_public_support()
        contract = _load_gateway_public_contract()
        proof = support.proof_document(
            contract,
            root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
            attestation_sha256=str(gateway_trust_pair["observer_attestation_sha256"]),
            docker_host_identity_sha256=str(gateway_trust_pair["host_receipt_sha256"]),
        )
        proof["trustPair"] = support.gateway_trust_references(gateway_trust_pair)
        proof_path = tmp_path / "runtime" / "gateway-only-public-attestation.json"
        proof_path.write_bytes(support.canonical_json(proof))
        return support.receipt_provenance(proof_path)
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
    attestation_path = tmp_path / "runtime" / "runtime-attestation.json"
    attestation_proof = {
        "artifact": "runtime/runtime-attestation.json",
        "sha256": hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
    }
    execution = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "evidence": evidence,
        "recipe": recipe_id,
        "command": command,
        "observedAt": "2026-08-24T00:00:00Z",
        "baseUrl": "https://candidate.example.test",
        "nativeExit": 0,
        "rawReportSha256": raw_sha256,
        "runtimeAttestation": attestation_proof,
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
        "runtimeAttestation": attestation_proof,
    }


def _capacity_profile_report(candidate: dict[str, object]) -> dict[str, object]:
    samples = [
        {
            "metric": metric,
            "tenantId": f"tenant-{sequence % 50:02d}",
            "subjectId": f"session-{sequence:03d}",
            "sequence": sequence,
            "latencyMs": 10.0,
            "success": True,
        }
        for metric in ("core_api", "event_ingest", "mastery_projection_visible")
        for sequence in range(200)
    ]
    job_samples = [
        {
            "metric": "job_submission_visible",
            "tenantId": "tenant-00" if sequence < 3 else f"tenant-{sequence - 2:02d}",
            "subjectId": f"job-{sequence:03d}",
            "sequence": sequence,
            "latencyMs": 10.0,
            "success": True,
        }
        for sequence in range(52)
    ]
    samples.extend(job_samples)
    active_groups = (
        job_samples[:2] + job_samples[3:21],
        job_samples[21:41],
        job_samples[41:52],
        job_samples[2:3],
    )
    claim_order = job_samples[:2] + job_samples[3:] + job_samples[2:3]
    resource_observations = [
        {
            "sequence": sequence,
            "phase": phase,
            "observedAt": f"2026-08-24T00:00:0{sequence}Z",
            "available": True,
            "totalRssBytes": 600,
            "limitBytes": 10_000,
            "availableBytes": 8_000,
            "limitSource": "cgroup",
            "usageRatio": 0.06,
            "partial": False,
            "processes": [
                {"label": "supervisor", "count": 1, "rssBytes": 100},
                {"label": "backend", "count": 1, "rssBytes": 200},
                {"label": "web", "count": 1, "rssBytes": 300},
            ],
        }
        for sequence, phase in enumerate(
            ("baseline", "generation_saturated", "sessions_saturated", "final")
        )
    ]
    event_binding = hashlib.sha256(b"session-000").hexdigest()
    event_ids = [
        f"session-{event_binding}-started",
        f"session-{event_binding}-quiz",
        f"session-{event_binding}-completed",
    ]
    request_envelope = {
        "events": [
            {
                "schema_version": "1.0",
                "event_id": event_ids[0],
                "event_type": "classroom.started",
                "occurred_at": "2026-08-24T00:00:00Z",
            },
            {
                "schema_version": "1.0",
                "event_id": event_ids[1],
                "event_type": "quiz.graded",
                "occurred_at": "2026-08-24T00:00:00Z",
                "scene_id": "scene-00",
                "knowledge_point_id": "kp-00",
                "assessment_id": "scene-00",
                "question_id": "question-00",
                "answer": ["answer-a"],
            },
            {
                "schema_version": "1.0",
                "event_id": event_ids[2],
                "event_type": "classroom.completed",
                "occurred_at": "2026-08-24T00:00:00Z",
            },
        ]
    }
    request_hash = hashlib.sha256(
        json.dumps(
            request_envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    response_rows = [
        {"eventId": event_id, "seq": sequence}
        for sequence, event_id in enumerate(event_ids, start=1)
    ]
    return {
        "schemaVersion": 2,
        "producer": "classroom-capacity-probe",
        "capacityModel": "deployed-candidate",
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "observedAt": "2026-08-24T00:00:00Z",
        "baseUrl": "https://candidate.example.test",
        "profile": {
            "name": "first-release",
            "declaredRegisteredUsers": 100_000,
            "declaredDailyActiveUsers": 10_000,
            "executedTenants": 50,
            "executedConcurrentSessions": 200,
            "sharedGenerationSlots": 20,
            "defaultTenantSlots": 2,
        },
        "workload": {
            "generationJobsSubmitted": 52,
            "learningSessionsStarted": 200,
            "learningSessionsCompleted": 200,
        },
        "rawSamples": samples,
        "schedulerSource": "admin-atomic-db-claim-audit",
        "schedulerClaims": [
            {
                "sequence": sequence,
                "jobId": sample["subjectId"],
                "tenantId": sample["tenantId"],
            }
            for sequence, sample in enumerate(claim_order)
        ],
        "schedulerObservations": [
            {
                "sequence": sequence,
                "active": [
                    {"jobId": sample["subjectId"], "tenantId": sample["tenantId"]}
                    for sample in active
                ],
            }
            for sequence, active in enumerate(active_groups)
        ],
        "sessionObservations": [
            {
                "sequence": 0,
                "active": [
                    {
                        "sessionId": f"session-{sequence:03d}",
                        "tenantId": f"tenant-{sequence % 50:02d}",
                        "classroomVersionId": f"version-{sequence % 50:02d}",
                        "knowledgePointId": f"kp-{sequence % 50:02d}",
                    }
                    for sequence in range(200)
                ],
            }
        ],
        "sessionCompletions": [
            {
                "sessionId": f"session-{sequence:03d}",
                "tenantId": f"tenant-{sequence % 50:02d}",
                "status": "completed",
            }
            for sequence in range(200)
        ],
        "idempotencyObservation": {
            "tenantId": "tenant-00",
            "sessionId": "session-000",
            "classroomVersionId": "version-00",
            "knowledgePointId": "kp-00",
            "eventIds": event_ids,
            "requestEnvelope": request_envelope,
            "requestSha256": request_hash,
            "firstTicketSha256": "4" * 64,
            "freshTicketSha256": "5" * 64,
            "firstResponse": {
                "statusCode": 202,
                "accepted": response_rows,
                "duplicate": [],
                "quarantined": [],
            },
            "ticketReplay": {
                "statusCode": 409,
                "detail": "Classroom ticket already used",
            },
            "freshResponse": {
                "statusCode": 202,
                "accepted": [],
                "duplicate": list(response_rows),
                "quarantined": [],
            },
            "quizProjection": {
                "expectedDelta": 4,
                "baseline": {
                    "classroomVersionId": "version-00",
                    "sessionCount": 4,
                    "completedCount": 0,
                    "knowledgePointId": "kp-00",
                    "validQuizCount": 0,
                    "correctQuizCount": 0,
                    "evidenceCount": 0,
                },
                "visible": {
                    "classroomVersionId": "version-00",
                    "sessionCount": 4,
                    "completedCount": 0,
                    "knowledgePointId": "kp-00",
                    "validQuizCount": 4,
                    "correctQuizCount": 4,
                    "evidenceCount": 4,
                },
                "reread": {
                    "classroomVersionId": "version-00",
                    "sessionCount": 4,
                    "completedCount": 4,
                    "knowledgePointId": "kp-00",
                    "validQuizCount": 4,
                    "correctQuizCount": 4,
                    "evidenceCount": 4,
                },
            },
        },
        "resourceSource": {
            "method": "GET",
            "path": "/api/v1/system/memory",
            "scope": "deeptutor-api-container-process-tree",
        },
        "resourceObservations": resource_observations,
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
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "docker-compose.yml",
        tmp_path / "docker-compose.yml",
    )
    digests = candidate["imageDigests"]
    assert isinstance(digests, dict)
    release_tag = candidate["releaseTag"]
    assert isinstance(release_tag, str)
    specifications = {
        "deeptutor": (
            "ghcr.io/xinlingzhifei/deeptutor",
            release_tag,
            digests["deeptutor"],
        ),
        "openmaic": (
            "ghcr.io/xinlingzhifei/openmaic",
            release_tag,
            digests["openmaic"],
        ),
        "openmaic_render": (
            "ghcr.io/xinlingzhifei/openmaic-render",
            release_tag,
            digests["openmaic_render"],
        ),
        "nginx": ("nginx", "1.29.8-alpine3.23", "sha256:" + "4" * 64),
        "postgres": ("postgres", "16.14-alpine3.24", "sha256:" + "5" * 64),
        "minio": (
            "minio/minio",
            "RELEASE.2025-04-22T22-12-26Z",
            "sha256:" + "6" * 64,
        ),
        "minio_client": (
            "minio/mc",
            "RELEASE.2025-04-16T18-13-26Z",
            "sha256:" + "7" * 64,
        ),
    }
    lock = {
        "schemaVersion": 2,
        "candidate": candidate,
        "images": {
            name: {
                "repository": repository,
                "tag": tag,
                "digest": digest,
                "reference": f"{repository}:{tag}@{digest}",
            }
            for name, (repository, tag, digest) in specifications.items()
        },
    }
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "image-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    references = {
        name: f"{repository}:{tag}@{digest}"
        for name, (repository, tag, digest) in specifications.items()
    }
    platform_compose_path = tmp_path / "docker-compose.platform.yml"
    platform_compose_path.write_text(
        json.dumps(
            {
                "services": {
                    "deeptutor": {"image": references["deeptutor"]},
                    "gateway": {"image": references["nginx"]},
                    "postgres": {"image": references["postgres"]},
                    "minio": {"image": references["minio"]},
                    "minio-bootstrap": {
                        "image": references["minio_client"],
                        "restart": "no",
                    },
                    "teaching-migrate": {
                        "image": references["deeptutor"],
                        "restart": "no",
                    },
                    "tenant-provisioner": {"image": references["deeptutor"]},
                    "shared-data-plane-bootstrap": {
                        "image": references["deeptutor"],
                        "restart": "no",
                    },
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
    platform_compose = json.loads(platform_compose_path.read_bytes())
    platform_services = platform_compose["services"]
    for service in platform_services.values():
        service["networks"] = ["platform-internal"]
    platform_services["deeptutor"]["networks"].append("platform-service-egress")
    platform_services["gateway"]["networks"].append("platform-edge")
    platform_services["openmaic"]["networks"].append("shared-provider-egress")
    platform_compose["networks"] = {
        "platform-internal": {"internal": True},
        "platform-edge": {},
        "platform-service-egress": {},
        "shared-provider-egress": {},
    }
    platform_compose_path.write_text(
        json.dumps(platform_compose, sort_keys=True),
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
    _write_runtime_attestation(tmp_path, candidate, references)


def _write_runtime_attestation(
    root: Path,
    candidate: dict[str, object],
    references: dict[str, str],
) -> None:
    service_images = {
        "deeptutor": references["deeptutor"],
        "gateway": references["nginx"],
        "postgres": references["postgres"],
        "minio": references["minio"],
        "minio-bootstrap": references["minio_client"],
        "teaching-migrate": references["deeptutor"],
        "tenant-provisioner": references["deeptutor"],
        "openmaic": references["openmaic"],
        "shared-data-plane-bootstrap": references["deeptutor"],
        "openmaic-render": references["openmaic_render"],
        "teaching-dispatcher": references["deeptutor"],
        "teaching-worker": references["deeptutor"],
        "teaching-export-worker": references["deeptutor"],
        "teaching-reaper": references["deeptutor"],
        "learning-projector": references["deeptutor"],
    }
    one_shots = {"minio-bootstrap", "teaching-migrate", "shared-data-plane-bootstrap"}
    healthy = {"deeptutor", "postgres", "minio", "openmaic", "openmaic-render"}

    def repo_digest(reference: str) -> str:
        tagged, digest = reference.rsplit("@", 1)
        return f"{tagged.rsplit(':', 1)[0]}@{digest}"

    def canonical_json(document: object) -> str:
        return json.dumps(document, separators=(",", ":"), sort_keys=True)

    empty_environment_sha256 = hashlib.sha256(b"{}").hexdigest()
    compose_hashes = {
        service: hashlib.sha256(f"compose:{service}".encode()).hexdigest()
        for service in service_images
    }
    compose_security = {
        "services": {
            service: {
                "image": reference,
                "restart": "no" if service in one_shots else "unless-stopped",
                "profiles": [],
                "privileged": False,
                "capAdd": [],
                "capDrop": [],
                "command": None,
                "entrypoint": None,
                "user": None,
                "environmentHashes": {},
                "volumes": [],
                "secrets": [],
                "configs": [],
            }
            for service, reference in sorted(service_images.items())
        },
        "volumes": {},
        "secrets": {},
        "configs": {},
    }

    containers: list[dict[str, object]] = []
    for service in sorted(service_images):
        reference = service_images[service]
        one_shot = service in one_shots
        image_id = "sha256:local-" + hashlib.sha256(reference.encode()).hexdigest()
        security = {
            "configHash": compose_hashes[service],
            "privileged": False,
            "mounts": [],
            "capAdd": [],
            "capDrop": [],
            "command": None,
            "entrypoint": None,
            "user": "",
            "environmentSha256": empty_environment_sha256,
        }
        containers.append(
            {
                "containerId": f"container-{service}",
                "service": service,
                "project": "yfeistai-platform",
                "configImage": reference,
                "localImageId": image_id,
                "state": "exited" if one_shot else "running",
                "running": not one_shot,
                "restarting": False,
                "health": "healthy" if service in healthy else "none",
                "exitCode": 0,
                "security": security,
                "imageId": image_id,
                "repoDigests": [repo_digest(reference)],
            }
        )
    snapshot = [
        {
            "containerId": container["containerId"],
            "service": container["service"],
            "image": container["configImage"],
            "state": container["state"],
            "health": container["health"],
            "exitCode": container["exitCode"],
            "securitySha256": hashlib.sha256(
                canonical_json(container["security"]).encode()
            ).hexdigest(),
        }
        for container in containers
    ]
    docker_prefix = [
        "docker",
        "--config",
        "<isolated-docker-config>",
        "--context",
        "default",
    ]
    container_format = (
        '{"containerId":{{json .Id}},"localImageId":{{json .Image}},'
        '"configImage":{{json .Config.Image}},'
        '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
        '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
        '"configHash":{{json (index .Config.Labels "com.docker.compose.config-hash")}},'
        '"privileged":{{json .HostConfig.Privileged}},"mounts":{{json .Mounts}},'
        '"capAdd":{{json .HostConfig.CapAdd}},"capDrop":{{json .HostConfig.CapDrop}},'
        '"command":{{json .Config.Cmd}},"entrypoint":{{json .Config.Entrypoint}},'
        '"user":{{json .Config.User}},"environment":{{json .Config.Env}},'
        '"state":{{json .State.Status}},"running":{{json .State.Running}},'
        '"restarting":{{json .State.Restarting}},"exitCode":{{json .State.ExitCode}},'
        '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}}'
    )
    image_format = (
        '{"imageId":{{json .Id}},"repoDigests":{{json .RepoDigests}},'
        '"command":{{json .Config.Cmd}},"entrypoint":{{json .Config.Entrypoint}},'
        '"user":{{json .Config.User}},"environment":{{json .Config.Env}},'
        '"volumes":{{json .Config.Volumes}}}'
    )
    compose_topology = [
        "compose",
        "--env-file",
        "<deployment-root>/data/user/settings/docker.env",
        "--project-directory",
        "<deployment-root>",
        "--project-name",
        "yfeistai-platform",
        "-f",
        "<candidate-root>/docker-compose.yml",
        "-f",
        "<candidate-root>/docker-compose.platform.yml",
    ]
    ps = [
        "ps",
        "-a",
        "--no-trunc",
        "--filter",
        "label=com.docker.compose.project=yfeistai-platform",
        "--format",
        "{{json .ID}}",
    ]
    containers_by_id = sorted(containers, key=lambda item: str(item["containerId"]))
    ps_stdout = "\n".join(json.dumps(container["containerId"]) for container in containers_by_id)

    def command(arguments: list[str], stdout: str) -> dict[str, object]:
        return {
            "argv": [*docker_prefix, *arguments],
            "nativeExit": 0,
            "stdout": stdout,
            "stdoutSha256": hashlib.sha256(stdout.encode()).hexdigest(),
        }

    container_records = []
    for container in containers_by_id:
        security = container["security"]
        assert isinstance(security, dict)
        container_records.append(
            command(
                [
                    "container",
                    "inspect",
                    "--format",
                    container_format,
                    str(container["containerId"]),
                ],
                json.dumps(
                    {
                        "containerId": container["containerId"],
                        "localImageId": container["localImageId"],
                        "configImage": container["configImage"],
                        "project": container["project"],
                        "service": container["service"],
                        "configHash": security["configHash"],
                        "privileged": security["privileged"],
                        "mounts": [],
                        "capAdd": security["capAdd"],
                        "capDrop": security["capDrop"],
                        "command": security["command"],
                        "entrypoint": security["entrypoint"],
                        "user": security["user"],
                        "environmentHashes": {},
                        "state": container["state"],
                        "running": container["running"],
                        "restarting": container["restarting"],
                        "exitCode": container["exitCode"],
                        "health": container["health"],
                    }
                ),
            )
        )
    image_records = []
    for reference in sorted(set(service_images.values())):
        container = next(item for item in containers if item["configImage"] == reference)
        image_records.append(
            command(
                ["image", "inspect", "--format", image_format, reference],
                json.dumps(
                    {
                        "imageId": container["imageId"],
                        "repoDigests": container["repoDigests"],
                        "command": None,
                        "entrypoint": None,
                        "user": "",
                        "environmentHashes": {},
                        "volumes": None,
                    }
                ),
            )
        )
    ps_record = command(ps, ps_stdout)
    docker_context_record = command(
        [
            "context",
            "inspect",
            "default",
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ],
        json.dumps("npipe:////./pipe/dockerDesktopLinuxEngine"),
    )
    docker_info_record = command(
        [
            "info",
            "--format",
            '{"serverId":{{json .ID}},"osType":{{json .OSType}}}',
        ],
        json.dumps({"serverId": "daemon-yfeistai-01", "osType": "linux"}),
    )
    document = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "observedAt": "2026-08-24T00:00:00Z",
        "baseUrl": "https://candidate.example.test",
        "project": "yfeistai-platform",
        "beforeSnapshot": snapshot,
        "afterSnapshot": snapshot,
        "containers": containers,
        "commands": [
            command(
                [*compose_topology, "config", "--format", "json"],
                canonical_json(compose_security),
            ),
            command(
                [*compose_topology, "config", "--hash", "*"],
                "\n".join(
                    f"{service} {compose_hashes[service]}" for service in sorted(compose_hashes)
                ),
            ),
            docker_context_record,
            docker_info_record,
            ps_record,
            *container_records,
            *image_records,
            ps_record,
            *container_records,
            docker_context_record,
            docker_info_record,
        ],
        "dockerHostIdentity": {
            "context": "default",
            "endpoint": "npipe:////./pipe/dockerDesktopLinuxEngine",
            "serverId": "daemon-yfeistai-01",
            "dockerHostIdentitySha256": _GATEWAY_DOCKER_HOST_IDENTITY_SHA256,
        },
    }
    runtime = root / "runtime"
    runtime.mkdir()
    (runtime / "runtime-attestation.json").write_text(json.dumps(document), encoding="utf-8")


def _write_platform_preflight_attestation(
    root: Path,
    candidate: dict[str, object],
) -> Path:
    runtime_attestation_path = root / "runtime" / "runtime-attestation.json"
    runtime_attestation = json.loads(runtime_attestation_path.read_text(encoding="utf-8"))
    containers = runtime_attestation["containers"]
    assert isinstance(containers, list)
    container_ids = {
        container["service"]: container["containerId"]
        for container in containers
        if isinstance(container, dict)
    }
    reports = {
        "database-object-store": {
            "schemaVersion": 1,
            "producer": "platform-preflight",
            "phase": "database-object-store",
            "checks": {
                "activeTenantCredentialsValid": True,
                "databaseConnected": True,
                "objectStoreRoundTrip": True,
                "revisionsMatch": True,
                "tenantCrossPrefixDenied": True,
                "tenantOwnPrefixAccessible": True,
            },
            "errors": [],
        },
        "openmaic": {
            "schemaVersion": 1,
            "producer": "platform-preflight",
            "phase": "openmaic",
            "checks": {"openmaicContractCompatible": True},
            "errors": [],
        },
    }
    services = {
        "database-object-store": "tenant-provisioner",
        "openmaic": "deeptutor",
    }
    executions: list[dict[str, object]] = []
    for phase in ("database-object-store", "openmaic"):
        service = services[phase]
        container_id = container_ids[service]
        assert isinstance(container_id, str)
        stdout = (
            json.dumps(
                reports[phase],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        executions.append(
            {
                "phase": phase,
                "service": service,
                "containerId": container_id,
                "command": [
                    "docker",
                    "--config",
                    "<isolated-docker-config>",
                    "--context",
                    "default",
                    "exec",
                    "--user",
                    "1000:1000",
                    container_id,
                    "python",
                    "/app/scripts/platform_preflight.py",
                    "--runtime-phase",
                    phase,
                    "--config",
                    "/app/data/user/settings/platform.json",
                    "--secret-dir",
                    "/run/secrets",
                ],
                "nativeExit": 0,
                "stdout": stdout,
                "stdoutSha256": hashlib.sha256(stdout.encode()).hexdigest(),
            }
        )
    document = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "observedAt": "2026-08-24T00:00:00Z",
        "baseUrl": "https://candidate.example.test",
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": hashlib.sha256(runtime_attestation_path.read_bytes()).hexdigest(),
        },
        "executions": executions,
    }
    path = root / "runtime" / "platform-preflight-attestation.json"
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return path


def _write_complete_bundle(
    tmp_path: Path,
    module,
    *,
    source_head: str,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    candidate = _candidate(source_head)
    module.PROJECT_ROOT = tmp_path
    _write_candidate_files(tmp_path, candidate)
    gateway_support = _load_gateway_public_support()
    gateway_trust_pair = gateway_support.write_gateway_trust_pair(
        tmp_path,
        _load_gateway_public_contract(),
        trusted_root=_gateway_trusted_root(tmp_path),
        candidate=candidate,
        release_run=_RELEASE_RUN,
        runtime_path=tmp_path / "runtime" / "runtime-attestation.json",
    )
    evidence: dict[str, object] = {}
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in module.REQUIRED_LAYERS:
        artifact_path = artifacts / f"{name}.json"
        provenance = _write_probe_proof(
            tmp_path,
            module,
            candidate,
            name,
            gateway_trust_pair=gateway_trust_pair,
        )
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
    gateway_support = _load_gateway_public_support()
    gateway_trust_pair = gateway_support.write_gateway_trust_pair(
        tmp_path,
        _load_gateway_public_contract(),
        trusted_root=_gateway_trusted_root(tmp_path),
        candidate=candidate,
        release_run=_RELEASE_RUN,
        runtime_path=tmp_path / "runtime" / "runtime-attestation.json",
    )
    for name, raw in evidence.items():
        assert isinstance(raw, dict)
        artifact_path = tmp_path / str(raw["artifact"])
        provenance = _write_probe_proof(
            tmp_path,
            module,
            candidate,
            name,
            gateway_trust_pair=gateway_trust_pair,
        )
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


@pytest.mark.parametrize("evidence", ("database_revisions", "service_health"))
def test_file_runtime_rejects_self_attested_preflight_receipt_without_bound_provenance(
    tmp_path: Path,
    evidence: str,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map[evidence]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact.pop("provenance")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, artifact["candidate"], evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers[evidence].status == "fail"
    assert "preflight execution proof is missing or invalid" in result.layers[evidence].detail


def test_file_runtime_rejects_self_attested_capacity_receipt_without_bound_provenance(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["capacity_profile"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact.pop("provenance")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["capacity_profile"].status == "fail"
    assert (
        "capacity execution proof is missing or invalid" in result.layers["capacity_profile"].detail
    )


def test_file_runtime_rejects_self_attested_learning_event_receipt_without_bound_provenance(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["learning_event_idempotency"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact.pop("provenance")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["learning_event_idempotency"].status == "fail"
    assert (
        "capacity execution proof is missing or invalid"
        in result.layers["learning_event_idempotency"].detail
    )


def test_file_runtime_rejects_self_attested_classroom_exports_without_bound_provenance(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["classroom_exports"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact.pop("provenance")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["classroom_exports"].status == "fail"
    assert (
        "classroom exports execution proof is missing or invalid"
        in result.layers["classroom_exports"].detail
    )


def test_file_runtime_rejects_self_attested_tenant_isolation_without_bound_provenance(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["tenant_isolation"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact.pop("provenance")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["tenant_isolation"].status == "fail"
    assert (
        "tenant isolation execution proof is missing or invalid"
        in result.layers["tenant_isolation"].detail
    )


def test_file_runtime_rejects_self_attested_openmaic_shared_plane_without_bound_provenance(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["openmaic_shared_plane"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact.pop("provenance")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["openmaic_shared_plane"].status == "fail"
    assert (
        "OpenMAIC shared plane execution proof is missing or invalid"
        in result.layers["openmaic_shared_plane"].detail
    )


def test_file_runtime_rejects_self_attested_openmaic_dedicated_plane_without_bound_provenance(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["openmaic_dedicated_plane"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact.pop("provenance")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["openmaic_dedicated_plane"].status == "fail"
    assert (
        "OpenMAIC dedicated plane execution proof is missing or invalid"
        in result.layers["openmaic_dedicated_plane"].detail
    )


def test_file_runtime_rejects_dedicated_success_without_independent_outage_provenance(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["openmaic_dedicated_plane"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    provenance = artifact["provenance"]
    assert isinstance(provenance, dict)
    provenance.pop("openmaicDedicatedOutageAttestation")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["openmaic_dedicated_plane"].status == "fail"
    assert (
        "OpenMAIC dedicated outage execution proof is missing or invalid"
        in result.layers["openmaic_dedicated_plane"].detail
    )


def test_outage_attempt_marker_replay_uses_external_observer_anchor_and_exact_candidate_run(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    candidate = _candidate("a" * 40)
    observer_sha256 = "7" * 64
    marker = {
        "schemaVersion": 1,
        "producer": "openmaic-dedicated-outage-attempt",
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "observerTrustAnchor": {
            "sha256": observer_sha256,
            "observerId": "shared-ingress-observer-openmaic-01",
            "observerOrigin": "https://observer.example.test",
            "sharedIngressControlOrigin": "https://shared-ingress.example.test",
        },
        "fixturePlan": {
            "tenantId": "tenant-openmaic-dedicated-01",
            "routeId": "dedicated-tenant-openmaic-01",
            "cleanupBoundary": "identity-membership-enrollment-only",
            "retainedResourceTypes": [
                "course",
                "class",
                "generationQuotaGrant",
                "classroomAsset",
                "generationJob",
            ],
        },
    }
    marker_path = tmp_path / "runtime" / "openmaic-dedicated-outage-attempt.json"
    marker_path.parent.mkdir()
    marker_body = (
        json.dumps(marker, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    marker_path.write_bytes(marker_body)
    reference = {
        "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
        "sha256": hashlib.sha256(marker_body).hexdigest(),
    }

    assert (
        module._replay_openmaic_dedicated_outage_attempt_marker(
            tmp_path,
            reference,
            candidate=candidate,
            release_run=_RELEASE_RUN,
            expected_observer_attestation_sha256=observer_sha256,
            expected_observer_id="shared-ingress-observer-openmaic-01",
            expected_observer_origin="https://observer.example.test",
            expected_shared_ingress_control_origin="https://shared-ingress.example.test",
            expected_tenant_id="tenant-openmaic-dedicated-01",
            expected_route_id="dedicated-tenant-openmaic-01",
        )
        == marker
    )

    changed = copy.deepcopy(marker)
    changed["observerTrustAnchor"]["sha256"] = "9" * 64
    changed_body = (
        json.dumps(changed, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    marker_path.write_bytes(changed_body)
    changed_reference = {**reference, "sha256": hashlib.sha256(changed_body).hexdigest()}
    with pytest.raises(ValueError, match="observer"):
        module._replay_openmaic_dedicated_outage_attempt_marker(
            tmp_path,
            changed_reference,
            candidate=candidate,
            release_run=_RELEASE_RUN,
            expected_observer_attestation_sha256=observer_sha256,
            expected_observer_id="shared-ingress-observer-openmaic-01",
            expected_observer_origin="https://observer.example.test",
            expected_shared_ingress_control_origin="https://shared-ingress.example.test",
            expected_tenant_id="tenant-openmaic-dedicated-01",
            expected_route_id="dedicated-tenant-openmaic-01",
        )


def test_dedicated_outage_receipt_replays_actual_marker_with_external_docker_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    candidate = _candidate("a" * 40)
    observer_sha256 = "7" * 64
    docker_host_sha256 = "6" * 64
    marker = {
        "schemaVersion": 1,
        "producer": "openmaic-dedicated-outage-attempt",
        "candidate": candidate,
        "releaseRun": _RELEASE_RUN,
        "observerTrustAnchor": {
            "sha256": observer_sha256,
            "observerId": "shared-ingress-observer-openmaic-01",
            "observerOrigin": "https://observer.example.test",
            "sharedIngressControlOrigin": "https://shared-ingress.example.test",
        },
        "fixturePlan": {
            "tenantId": "tenant-openmaic-dedicated-01",
            "routeId": "dedicated-tenant-openmaic-01",
            "cleanupBoundary": "identity-membership-enrollment-only",
            "retainedResourceTypes": [
                "course",
                "class",
                "generationQuotaGrant",
                "classroomAsset",
                "generationJob",
            ],
        },
    }
    marker_body = (
        json.dumps(marker, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    marker_reference = {
        "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
        "sha256": hashlib.sha256(marker_body).hexdigest(),
    }
    document = {
        "baseUrl": "https://candidate.example.test",
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": "5" * 64,
        },
        "observerAttestation": {
            "artifact": "runtime/openmaic-shared-ingress-observer-attestation.json",
            "sha256": observer_sha256,
            "observerId": "shared-ingress-observer-openmaic-01",
            "observerOrigin": "https://observer.example.test",
            "sharedIngressControlOrigin": "https://shared-ingress.example.test",
        },
        "fixture": {
            "tenantId": "tenant-openmaic-dedicated-01",
            "attemptMarker": marker_reference,
        },
        "provenance": {
            "attemptMarker": marker_reference,
            "observerTrustAnchor": marker["observerTrustAnchor"],
            "dockerBoundary": {
                "dockerHostIdentitySha256": docker_host_sha256,
                "daemonIdentityBeforeSha256": "2" * 64,
                "daemonIdentityAfterSha256": "2" * 64,
                "inventoryBeforeSha256": "3" * 64,
                "inventoryAfterSha256": "3" * 64,
            },
        },
        "outage": {"routeId": "dedicated-tenant-openmaic-01"},
    }
    body = (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()
    replayed: list[dict[str, object]] = []

    monkeypatch.setattr(module, "validate_runtime_attestation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        module,
        "_proof_bytes",
        lambda *_args, **_kwargs: (
            b"observer-body",
            SimpleNamespace(close=lambda: None),
        ),
    )
    monkeypatch.setattr(
        module,
        "parse_openmaic_shared_ingress_observer_attestation",
        lambda *_args, **_kwargs: {
            "observer": {
                "observerId": "shared-ingress-observer-openmaic-01",
                "observerUrl": "https://observer.example.test",
                "sharedIngressControlUrl": (
                    "https://shared-ingress.example.test/v1/control-canaries"
                ),
            }
        },
    )

    def replay_marker(*_args, return_body=False, **kwargs):
        assert return_body is True
        assert kwargs["expected_observer_attestation_sha256"] == observer_sha256
        replayed.append(kwargs)
        return marker, marker_body

    monkeypatch.setattr(module, "_replay_openmaic_dedicated_outage_attempt_marker", replay_marker)

    def parse_outage(_body, **kwargs):
        assert kwargs["attempt_marker_body"] == marker_body
        assert kwargs["expected_docker_host_identity_sha256"] == docker_host_sha256
        return {"observedAt": "2026-08-30T00:00:01Z"}

    monkeypatch.setattr(module, "parse_openmaic_dedicated_outage_attestation", parse_outage)
    monkeypatch.setattr(
        module,
        "derive_openmaic_dedicated_outage_checks",
        lambda _report: {"noSharedFallback": True},
    )

    assert module.derive_openmaic_dedicated_outage_receipt_checks(
        body,
        bundle_root=tmp_path,
        candidate_root=tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
        expected_tenant_id="tenant-openmaic-dedicated-01",
        expected_docker_host_identity_sha256=docker_host_sha256,
        expected_openmaic_observer_attestation_sha256=observer_sha256,
        expected_openmaic_observer_id=_OPENMAIC_OBSERVER_ID,
        expected_openmaic_observer_origin=_OPENMAIC_OBSERVER_ORIGIN,
        expected_openmaic_shared_ingress_control_origin=_OPENMAIC_CONTROL_ORIGIN,
    ) == ({"noSharedFallback": True}, "2026-08-30T00:00:01Z")
    assert len(replayed) == 1


def test_file_runtime_rejects_dedicated_success_without_observer_provenance(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["openmaic_dedicated_plane"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    provenance = artifact["provenance"]
    assert isinstance(provenance, dict)
    provenance.pop("openmaicSharedIngressObserverAttestation")
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["openmaic_dedicated_plane"].status == "fail"
    assert (
        "OpenMAIC shared-ingress observer execution proof is missing or invalid"
        in result.layers["openmaic_dedicated_plane"].detail
    )


def test_file_runtime_rejects_self_consistent_untrusted_shared_ingress_observer_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    manifest, _evidence, _candidate_document = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    monkeypatch.setattr(
        module,
        "derive_openmaic_dedicated_plane_receipt_checks",
        lambda *_args, **_kwargs: (
            {
                "dedicatedGenerationPassed": True,
                "noSharedClientIssued": True,
            },
            "2026-08-24T00:00:00Z",
        ),
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
            expected_outage_docker_host_identity_sha256=(_GATEWAY_DOCKER_HOST_IDENTITY_SHA256),
            **_gateway_runtime_arguments(tmp_path),
        )
    )

    assert result.layers["openmaic_dedicated_plane"].status == "fail"
    assert "external observer trust anchor" in result.layers["openmaic_dedicated_plane"].detail


@pytest.mark.parametrize(
    "tamper",
    ("candidate", "release_run", "runtime", "command", "stdout_digest", "summary"),
)
def test_openmaic_shared_plane_replay_rejects_bound_proof_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    module = _load_verifier()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "openmaic-shared-plane-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    if tamper == "candidate":
        proof["candidate"]["sourceHead"] = "b" * 40
    elif tamper == "release_run":
        proof["releaseRun"]["runId"] = "run-other"
    elif tamper == "runtime":
        proof["runtimeAttestation"]["sha256"] = "0" * 64
    elif tamper == "command":
        proof["execution"]["command"]["script"] = "scripts/other.py"
    elif tamper == "stdout_digest":
        proof["execution"]["stdoutSha256"] = "0" * 64
    else:
        proof["summary"]["generation"]["jobId"] = "job-other"

    with pytest.raises(ValueError):
        module.derive_openmaic_shared_plane_receipt_checks(
            json.dumps(proof, sort_keys=True).encode(),
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )


def test_openmaic_dedicated_plane_replay_rejects_bound_proof_tampering(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    derive = getattr(module, "derive_openmaic_dedicated_plane_receipt_checks", None)
    assert callable(derive), "OpenMAIC dedicated-plane proof replay is missing"
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "openmaic-dedicated-plane-attestation.json"
    original = json.loads(proof_path.read_text(encoding="utf-8"))

    for tamper in (
        "candidate",
        "release_run",
        "runtime",
        "command",
        "stdout_digest",
        "binding",
        "summary",
    ):
        proof = copy.deepcopy(original)
        if tamper == "candidate":
            proof["candidate"]["sourceHead"] = "b" * 40
        elif tamper == "release_run":
            proof["releaseRun"]["runId"] = "run-other"
        elif tamper == "runtime":
            proof["runtimeAttestation"]["sha256"] = "0" * 64
        elif tamper == "command":
            proof["execution"]["command"]["arguments"][1] = "shared"
        elif tamper == "stdout_digest":
            proof["execution"]["stdoutSha256"] = "0" * 64
        elif tamper == "binding":
            report = json.loads(proof["execution"]["stdout"])
            report["binding"]["routeTenantId"] = "tenant-other"
            stdout = (
                json.dumps(
                    report,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
            proof["execution"]["stdout"] = stdout
            proof["execution"]["stdoutSha256"] = hashlib.sha256(stdout.encode()).hexdigest()
        else:
            proof["summary"]["generation"]["jobId"] = "job-other"

        with pytest.raises(ValueError):
            derive(
                json.dumps(proof, sort_keys=True).encode(),
                bundle_root=tmp_path,
                candidate_root=tmp_path,
                candidate=candidate,
                release_run=_RELEASE_RUN,
            )


def test_openmaic_shared_plane_replay_rejects_current_live_token_in_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    token = "live-fixture-token-public-shaped"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token)
    proof_path = tmp_path / "runtime" / "openmaic-shared-plane-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    report = json.loads(proof["execution"]["stdout"])
    report["fixture"]["teacherUserId"] = token
    stdout = json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    proof["execution"]["stdout"] = stdout
    proof["execution"]["stdoutSha256"] = hashlib.sha256(stdout.encode()).hexdigest()
    proof["summary"]["fixture"]["teacherUserId"] = token

    with pytest.raises(ValueError, match="strict report is invalid"):
        module.derive_openmaic_shared_plane_receipt_checks(
            json.dumps(proof, sort_keys=True).encode(),
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )


@pytest.mark.parametrize("case", ("missing", "tampered", "symlink", "dangling", "extra"))
def test_file_runtime_rejects_invalid_classroom_export_raw_boundaries(
    tmp_path: Path,
    case: str,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    raw_root = tmp_path / "raw" / "classroom-exports"
    target = raw_root / "classroom.html"
    if case == "tampered":
        target.write_bytes(b"tampered")
    elif case == "extra":
        (raw_root / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    else:
        retained = tmp_path / "retained-classroom.html"
        os.replace(target, retained)
        if case == "symlink":
            try:
                target.symlink_to(retained)
            except OSError:
                pytest.skip("file symlinks are unavailable on this test host")
        elif case == "dangling":
            try:
                target.symlink_to(tmp_path / "does-not-exist.html")
            except OSError:
                pytest.skip("file symlinks are unavailable on this test host")

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["classroom_exports"].status == "fail"
    assert "classroom export" in result.layers["classroom_exports"].detail


def test_classroom_export_replay_rejects_raw_drift_during_contract_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_body = (tmp_path / "runtime" / "classroom-exports-attestation.json").read_bytes()
    target = tmp_path / "raw" / "classroom-exports" / "classroom.html"
    original_derive = module.derive_classroom_export_checks

    def derive_and_drift(*args, **kwargs):
        checks = original_derive(*args, **kwargs)
        target.write_bytes(b"changed during replay")
        return checks

    monkeypatch.setattr(module, "derive_classroom_export_checks", derive_and_drift)

    with pytest.raises(ValueError, match="raw artifact.*changed"):
        module.derive_classroom_exports_receipt_checks(
            proof_body,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )


@pytest.mark.skipif(os.name != "nt", reason="exercises retained Windows directory handles")
def test_classroom_export_raw_snapshot_rejects_directory_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_body = (tmp_path / "runtime" / "classroom-exports-attestation.json").read_bytes()
    raw_parent = tmp_path / "raw"
    original = raw_parent / "classroom-exports"
    alternate = raw_parent / "classroom-exports-alternate"
    alternate.mkdir()
    for artifact in original.iterdir():
        (alternate / artifact.name).write_bytes(artifact.read_bytes())
    retained = raw_parent / "classroom-exports-original"
    real_open_relative = module._open_windows_directory_relative
    swapped = False

    def open_relative_with_aba(parent_handle, name):
        nonlocal swapped
        if name != "classroom-exports" or swapped:
            return real_open_relative(parent_handle, name)
        os.replace(original, retained)
        os.replace(alternate, original)
        try:
            opened = real_open_relative(parent_handle, name)
        finally:
            os.replace(original, alternate)
            os.replace(retained, original)
        swapped = True
        return opened

    monkeypatch.setattr(module, "_open_windows_directory_relative", open_relative_with_aba)

    with pytest.raises(ValueError, match="raw boundary changed"):
        module.derive_classroom_exports_receipt_checks(
            proof_body,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )

    assert swapped is True
    assert set(path.name for path in original.iterdir()) == {
        "classroom.zip",
        "classroom.pptx",
        "classroom.html",
        "classroom.mp4",
    }


def test_classroom_export_replay_requires_the_live_fixture_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_body = (tmp_path / "runtime" / "classroom-exports-attestation.json").read_bytes()
    monkeypatch.delenv("YFEISTAI_LIVE_FIXTURE_TOKEN")

    with pytest.raises(ValueError, match="live fixture token"):
        module.derive_classroom_exports_receipt_checks(
            proof_body,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )


def test_classroom_export_replay_rejects_a_fixture_token_in_bound_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_body = (tmp_path / "runtime" / "classroom-exports-attestation.json").read_bytes()
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "Classroom")

    with pytest.raises(ValueError, match="live fixture token"):
        module.derive_classroom_exports_receipt_checks(
            proof_body,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )


@pytest.mark.parametrize(
    ("artifact_name", "member_name", "token"),
    (
        ("classroom.zip", "media/voice.mp3", b"first-release-audio"),
        ("classroom.pptx", "ppt/slides/slide1.xml", b"Verified classroom export"),
    ),
    ids=("classroom_zip", "pptx"),
)
def test_classroom_export_replay_rejects_secret_bearing_deflated_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_name: str,
    member_name: str,
    token: bytes,
) -> None:
    module = _load_verifier()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    artifact = tmp_path / "raw" / "classroom-exports" / artifact_name
    with zipfile.ZipFile(artifact) as archive:
        info = archive.getinfo(member_name)
        assert info.compress_type == zipfile.ZIP_DEFLATED
        assert token in archive.read(info)
    assert token not in artifact.read_bytes()
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token.decode("utf-8"))
    proof_body = (tmp_path / "runtime" / "classroom-exports-attestation.json").read_bytes()

    with pytest.raises(ValueError, match="live fixture token"):
        module.derive_classroom_exports_receipt_checks(
            proof_body,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )


def _capacity_profile_proof(
    tmp_path: Path,
    module,
) -> tuple[bytes, dict[str, object]]:
    candidate = _candidate("a" * 40)
    module.PROJECT_ROOT = tmp_path
    _write_candidate_files(tmp_path, candidate)
    _write_probe_proof(tmp_path, module, candidate, "capacity_profile")
    return (
        (tmp_path / "runtime" / "capacity-profile-attestation.json").read_bytes(),
        candidate,
    )


def test_derive_capacity_profile_tenant_ids_replays_all_executed_tenants(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    proof_body, candidate = _capacity_profile_proof(tmp_path, module)

    tenant_ids = module.derive_capacity_profile_tenant_ids(
        proof_body,
        bundle_root=tmp_path,
        candidate_root=tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
    )

    assert tenant_ids == tuple(f"tenant-{index:02d}" for index in range(50))


def test_derive_capacity_profile_tenant_ids_rejects_non_true_checks_and_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    proof_body, candidate = _capacity_profile_proof(tmp_path, module)
    replay_checks = module.derive_capacity_profile_receipt_checks
    checks, observed_at = replay_checks(
        proof_body,
        bundle_root=tmp_path,
        candidate_root=tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
    )

    for rejected_check in checks:
        rejected_checks = {name: True for name in checks}
        rejected_checks[rejected_check] = False

        def replay_with_rejected_check(*_args, **_kwargs):
            return rejected_checks, observed_at

        monkeypatch.setattr(
            module,
            "derive_capacity_profile_receipt_checks",
            replay_with_rejected_check,
        )
        with pytest.raises(ValueError, match="capacity execution proof checks"):
            module.derive_capacity_profile_tenant_ids(
                proof_body,
                bundle_root=tmp_path,
                candidate_root=tmp_path,
                candidate=candidate,
                release_run=_RELEASE_RUN,
            )

    monkeypatch.setattr(module, "derive_capacity_profile_receipt_checks", replay_checks)
    tampered = json.loads(proof_body)
    tampered["summary"]["checks"]["thresholdsPassed"] = False
    tampered_body = json.dumps(tampered, sort_keys=True).encode("utf-8")

    with pytest.raises(ValueError, match="capacity execution proof"):
        module.derive_capacity_profile_tenant_ids(
            tampered_body,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
        )


def test_file_runtime_replays_capacity_profile_raw_samples(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["capacity_profile"].status == "pass"


def test_file_runtime_rejects_tampered_capacity_samples_even_when_proof_is_rehashed(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    report = json.loads(proof["execution"]["stdout"])
    for sample in report["rawSamples"]:
        if sample["metric"] == "core_api":
            sample["latencyMs"] = 500.0
    report_body = (
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    proof["execution"]["stdout"] = report_body.decode()
    proof["execution"]["stdoutSha256"] = hashlib.sha256(report_body).hexdigest()
    parsed = module.parse_capacity_profile_report(
        report_body,
        candidate=candidate,
        release_run=_RELEASE_RUN,
        expected_base_url="https://candidate.example.test",
    )
    proof["summary"] = module.derive_capacity_profile_summary(parsed)
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    _rebind_capacity_proof(tmp_path, module, manifest, evidence_map, candidate)

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["capacity_profile"].status == "fail"
    assert "receipt does not match" in result.layers["capacity_profile"].detail


def test_file_runtime_rejects_simulated_capacity_profile(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    report = json.loads(proof["execution"]["stdout"])
    report["capacityModel"] = "simulated"
    report_body = (
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    proof["execution"]["stdout"] = report_body.decode()
    proof["execution"]["stdoutSha256"] = hashlib.sha256(report_body).hexdigest()
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    _rebind_capacity_proof(tmp_path, module, manifest, evidence_map, candidate)

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["capacity_profile"].status == "fail"
    assert "deployed candidate" in result.layers["capacity_profile"].detail


def test_file_runtime_rejects_capacity_summary_numeric_type_tamper(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["summary"]["metrics"]["core_api"]["count"] = 200.0
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    _rebind_capacity_proof(tmp_path, module, manifest, evidence_map, candidate)

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["capacity_profile"].status == "fail"
    assert "summary does not match raw samples" in result.layers["capacity_profile"].detail


def test_file_runtime_rejects_capacity_raw_change_with_stale_summary(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    report = json.loads(proof["execution"]["stdout"])
    for sample in report["rawSamples"]:
        if sample["metric"] == "core_api":
            sample["latencyMs"] = 11.0
    report_body = (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    proof["execution"]["stdout"] = report_body.decode()
    proof["execution"]["stdoutSha256"] = hashlib.sha256(report_body).hexdigest()
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    _rebind_capacity_proof(tmp_path, module, manifest, evidence_map, candidate)

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["capacity_profile"].status == "fail"
    assert "summary does not match raw samples" in result.layers["capacity_profile"].detail


def test_file_runtime_rejects_float_capacity_receipt_schema_version(tmp_path: Path) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["capacity_profile"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["schemaVersion"] = float(module.ARTIFACT_SCHEMA_VERSION)
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["capacity_profile"].status == "fail"
    assert "envelope is invalid" in result.layers["capacity_profile"].detail


def _rebind_capacity_proof(
    tmp_path: Path,
    module,
    manifest: Path,
    evidence_map: dict[str, object],
    candidate: dict[str, object],
) -> None:
    proof_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
    proof_sha256 = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    for evidence in ("capacity_profile", "learning_event_idempotency"):
        evidence_entry = evidence_map[evidence]
        assert isinstance(evidence_entry, dict)
        artifact_path = tmp_path / str(evidence_entry["artifact"])
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["provenance"]["capacityAttestation"]["sha256"] = proof_sha256
        artifact_body = json.dumps(artifact, sort_keys=True).encode()
        artifact_path.write_bytes(artifact_body)
        evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )


def _rebind_tenant_isolation_proof(
    tmp_path: Path,
    module,
    manifest: Path,
    evidence_map: dict[str, object],
    candidate: dict[str, object],
    proof: dict[str, object],
) -> None:
    proof_path = tmp_path / "runtime" / "tenant-isolation-attestation.json"
    proof_body = json.dumps(proof, sort_keys=True).encode("utf-8")
    proof_path.write_bytes(proof_body)
    evidence_entry = evidence_map["tenant_isolation"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["provenance"]["tenantIsolationAttestation"]["sha256"] = hashlib.sha256(
        proof_body
    ).hexdigest()
    artifact_body = json.dumps(artifact, sort_keys=True).encode("utf-8")
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )


def test_derive_tenant_isolation_receipt_checks_replays_runtime_capacity_and_strict_report(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    _manifest, _evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_body = (tmp_path / "runtime" / "tenant-isolation-attestation.json").read_bytes()

    checks, observed_at = module.derive_tenant_isolation_receipt_checks(
        proof_body,
        bundle_root=tmp_path,
        candidate_root=tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
    )

    assert checks == {
        "databaseIsolated": True,
        "objectsIsolated": True,
        "exportsIsolated": True,
        "eventsIsolated": True,
    }
    assert observed_at == "2026-08-24T00:00:00Z"


@pytest.mark.parametrize("dependency", ("runtime", "capacity"))
def test_file_runtime_rejects_rehashed_tenant_isolation_dependency_proof_tampering(
    tmp_path: Path,
    dependency: str,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    runtime_path = tmp_path / "runtime" / "runtime-attestation.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if dependency == "runtime":
        runtime["project"] = "attacker-project"
    runtime_body = json.dumps(runtime, sort_keys=True).encode("utf-8")
    runtime_path.write_bytes(runtime_body)
    runtime_sha256 = hashlib.sha256(runtime_body).hexdigest()

    capacity_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
    capacity = json.loads(capacity_path.read_text(encoding="utf-8"))
    capacity["runtimeAttestation"]["sha256"] = runtime_sha256
    if dependency == "capacity":
        capacity["summary"]["checks"]["thresholdsPassed"] = False
    capacity_body = json.dumps(capacity, sort_keys=True).encode("utf-8")
    capacity_path.write_bytes(capacity_body)
    capacity_sha256 = hashlib.sha256(capacity_body).hexdigest()

    tenant_path = tmp_path / "runtime" / "tenant-isolation-attestation.json"
    tenant_proof = json.loads(tenant_path.read_text(encoding="utf-8"))
    tenant_proof["runtimeAttestation"]["sha256"] = runtime_sha256
    tenant_proof["capacityAttestation"]["sha256"] = capacity_sha256
    report = json.loads(tenant_proof["execution"]["stdout"])
    report["capacityProof"]["reportSha256"] = capacity_sha256
    contract = _load_tenant_isolation_support()._module()
    report_body = contract.canonical_tenant_isolation_report(report)
    tenant_proof["execution"]["stdout"] = report_body.decode("utf-8")
    tenant_proof["execution"]["stdoutSha256"] = hashlib.sha256(report_body).hexdigest()
    _rebind_tenant_isolation_proof(
        tmp_path,
        module,
        manifest,
        evidence_map,
        candidate,
        tenant_proof,
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["tenant_isolation"].status == "fail"
    assert dependency in result.layers["tenant_isolation"].detail.lower()


def test_file_runtime_rejects_rehashed_tampered_tenant_isolation_proof(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "tenant-isolation-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    report = json.loads(proof["execution"]["stdout"])
    database = next(item for item in report["observations"] if item["layer"] == "database")
    owner_after = next(
        item for item in database["operations"] if item["name"] == "owner-policy-after"
    )
    owner_after["stateSha256"] = "c" * 64
    contract = _load_tenant_isolation_support()._module()
    report_body = contract.canonical_tenant_isolation_report(report)
    proof["execution"]["stdout"] = report_body.decode("utf-8")
    proof["execution"]["stdoutSha256"] = hashlib.sha256(report_body).hexdigest()
    _rebind_tenant_isolation_proof(
        tmp_path,
        module,
        manifest,
        evidence_map,
        candidate,
        proof,
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["tenant_isolation"].status == "fail"
    assert "tenant isolation" in result.layers["tenant_isolation"].detail.lower()


def test_file_runtime_rejects_tenant_isolation_capacity_pair_drift(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "tenant-isolation-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    report = json.loads(proof["execution"]["stdout"])
    tenant_ids = report["capacityProof"]["tenantIds"]
    report["capacityProof"]["tenantIds"] = list(reversed(tenant_ids))
    contract = _load_tenant_isolation_support()._module()
    report_body = contract.canonical_tenant_isolation_report(report)
    proof["execution"]["stdout"] = report_body.decode("utf-8")
    proof["execution"]["stdoutSha256"] = hashlib.sha256(report_body).hexdigest()
    _rebind_tenant_isolation_proof(
        tmp_path,
        module,
        manifest,
        evidence_map,
        candidate,
        proof,
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["tenant_isolation"].status == "fail"
    assert "capacity" in result.layers["tenant_isolation"].detail.lower()


def test_file_runtime_rejects_tampered_learning_event_receipt_even_when_rehashed(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["learning_event_idempotency"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["receipt"]["observedAt"] = "2026-08-24T00:00:01Z"
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, candidate, evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["learning_event_idempotency"].status == "fail"
    assert (
        "does not match capacity execution proof"
        in result.layers["learning_event_idempotency"].detail
    )


def test_file_runtime_replays_learning_event_checks_from_raw_capacity_stdout(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "capacity-profile-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    report = json.loads(proof["execution"]["stdout"])
    report["idempotencyObservation"]["freshResponse"]["duplicate"][1]["seq"] = 99
    report_body = (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    proof["execution"]["stdout"] = report_body.decode()
    proof["execution"]["stdoutSha256"] = hashlib.sha256(report_body).hexdigest()
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    _rebind_capacity_proof(tmp_path, module, manifest, evidence_map, candidate)

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["learning_event_idempotency"].status == "fail"
    assert (
        "idempotency observation is invalid" in result.layers["learning_event_idempotency"].detail
    )


@pytest.mark.parametrize("evidence", ("database_revisions", "service_health"))
def test_file_runtime_rejects_tampered_platform_preflight_execution(
    tmp_path: Path,
    evidence: str,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "platform-preflight-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    execution = proof["executions"][0 if evidence == "database_revisions" else 1]
    report = json.loads(execution["stdout"])
    check = "revisionsMatch" if evidence == "database_revisions" else "openmaicContractCompatible"
    report["checks"][check] = False
    report["errors"] = [
        "database migrations"
        if evidence == "database_revisions"
        else "OpenMAIC health and contract 1.0"
    ]
    stdout = (
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    execution["stdout"] = stdout
    execution["stdoutSha256"] = hashlib.sha256(stdout.encode()).hexdigest()
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")

    evidence_entry = evidence_map[evidence]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["provenance"]["platformPreflightAttestation"]["sha256"] = hashlib.sha256(
        proof_path.read_bytes()
    ).hexdigest()
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, artifact["candidate"], evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers[evidence].status == "fail"
    assert "preflight" in result.layers[evidence].detail.lower()


def test_file_runtime_rejects_boolean_platform_preflight_schema_version(
    tmp_path: Path,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    proof_path = tmp_path / "runtime" / "platform-preflight-attestation.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["schemaVersion"] = True
    proof_body = json.dumps(proof, sort_keys=True).encode()
    proof_path.write_bytes(proof_body)
    proof_sha256 = hashlib.sha256(proof_body).hexdigest()

    for evidence in ("database_revisions", "service_health"):
        evidence_entry = evidence_map[evidence]
        assert isinstance(evidence_entry, dict)
        artifact_path = tmp_path / str(evidence_entry["artifact"])
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["provenance"]["platformPreflightAttestation"]["sha256"] = proof_sha256
        artifact_body = json.dumps(artifact, sort_keys=True).encode()
        artifact_path.write_bytes(artifact_body)
        evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()

    manifest.write_text(
        json.dumps(_manifest_document(module, proof["candidate"], evidence_map)),
        encoding="utf-8",
    )
    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["database_revisions"].status == "fail"
    assert result.layers["service_health"].status == "fail"


def test_platform_preflight_proof_is_read_through_fixed_no_follow_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    _manifest, evidence_map, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["database_revisions"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    proof = artifact["provenance"]["platformPreflightAttestation"]
    proof_path = tmp_path / proof["artifact"]
    expected_body = proof_path.read_bytes()
    original_read_bytes = Path.read_bytes

    def reject_check_then_open(path: Path) -> bytes:
        if path.resolve() == proof_path.resolve():
            pytest.fail("platform preflight proof must use the fixed no-follow reader")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_check_then_open)

    assert module._proof_bytes(
        tmp_path,
        proof,
        label="platform preflight attestation",
    ) == (expected_body, "runtime/platform-preflight-attestation.json")


def test_platform_preflight_proof_rejects_oversized_fixed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    proof_path = runtime_root / "platform-preflight-attestation.json"
    oversized = b"x" * (1024 * 1024 + 1)
    proof_path.write_bytes(oversized)
    proof = {
        "artifact": "runtime/platform-preflight-attestation.json",
        "sha256": hashlib.sha256(oversized).hexdigest(),
    }

    if sys.platform == "win32":
        monkeypatch.setattr(
            module,
            "_read_windows_file_handle",
            lambda _handle: pytest.fail("oversized proof must be rejected before allocation"),
        )

    assert (
        module._proof_bytes(
            tmp_path,
            proof,
            label="platform preflight attestation",
        )
        == "platform preflight attestation cannot be read from its fixed boundary"
    )


def test_capacity_profile_proof_is_read_through_fixed_no_follow_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    _manifest, evidence_map, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["capacity_profile"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    proof = artifact["provenance"]["capacityAttestation"]
    proof_path = tmp_path / proof["artifact"]
    expected_body = proof_path.read_bytes()
    original_read_bytes = Path.read_bytes

    def reject_check_then_open(path: Path) -> bytes:
        if path.resolve() == proof_path.resolve():
            pytest.fail("capacity proof must use the fixed no-follow reader")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_check_then_open)

    assert module._proof_bytes(
        tmp_path,
        proof,
        label="capacity execution attestation",
    ) == (expected_body, "runtime/capacity-profile-attestation.json")


def test_tenant_isolation_proof_is_read_through_fixed_no_follow_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    _manifest, evidence_map, _candidate_document = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["tenant_isolation"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    proof = artifact["provenance"]["tenantIsolationAttestation"]
    proof_path = tmp_path / proof["artifact"]
    expected_body = proof_path.read_bytes()
    original_read_bytes = Path.read_bytes

    def reject_check_then_open(path: Path) -> bytes:
        if path.resolve() == proof_path.resolve():
            pytest.fail("tenant isolation proof must use the fixed no-follow reader")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_check_then_open)

    assert module._proof_bytes(
        tmp_path,
        proof,
        label="tenant isolation attestation",
    ) == (expected_body, "runtime/tenant-isolation-attestation.json")


def test_capacity_profile_proof_rejects_oversized_fixed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    proof_path = runtime_root / "capacity-profile-attestation.json"
    oversized = b"x" * (1024 * 1024 + 1)
    proof_path.write_bytes(oversized)
    proof = {
        "artifact": "runtime/capacity-profile-attestation.json",
        "sha256": hashlib.sha256(oversized).hexdigest(),
    }

    if sys.platform == "win32":
        monkeypatch.setattr(
            module,
            "_read_windows_file_handle",
            lambda _handle: pytest.fail("oversized proof must be rejected before allocation"),
        )

    assert (
        module._proof_bytes(
            tmp_path,
            proof,
            label="capacity execution attestation",
        )
        == "capacity execution attestation cannot be read from its fixed boundary"
    )


@pytest.mark.parametrize("extra_location", ("result", "checks"))
def test_file_runtime_rejects_extra_preflight_receipt_fields(
    tmp_path: Path,
    extra_location: str,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    evidence_entry = evidence_map["database_revisions"]
    assert isinstance(evidence_entry, dict)
    artifact_path = tmp_path / str(evidence_entry["artifact"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    result_document = artifact["receipt"]["result"]
    if extra_location == "result":
        result_document["attackerAssertion"] = True
    else:
        result_document["checks"]["attackerAssertion"] = True
    artifact_body = json.dumps(artifact, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_body)
    evidence_entry["artifactSha256"] = hashlib.sha256(artifact_body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, artifact["candidate"], evidence_map)),
        encoding="utf-8",
    )

    result = module.verify(
        module.FileReleaseRuntime(
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["database_revisions"].status == "fail"
    assert "receipt" in result.layers["database_revisions"].detail


def test_file_runtime_requires_the_same_candidate_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    _bind_gateway_expected_environment(monkeypatch, tmp_path)

    result = module.verify(
        _complete_bundle_runtime(
            module,
            manifest,
            expected_source_head="b" * 40,
        )
    )

    assert result.ok is False
    assert result.failed == ("source_head",)
    assert "does not match" in result.layers["source_head"].detail


def test_file_runtime_rejects_bundle_parent_replacement_after_receipt_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    manifest, _, _ = _write_complete_bundle(
        bundle_root,
        module,
        source_head="a" * 40,
    )
    retained_root = tmp_path / "retained-bundle"
    original_provenance = module.probe_provenance_error
    switched = False

    def replay(*args, **kwargs):
        nonlocal switched
        result = original_provenance(*args, **kwargs)
        bundle_root.rename(retained_root)
        bundle_root.mkdir()
        switched = True
        return result

    monkeypatch.setattr(module, "probe_provenance_error", replay)
    runtime = module.FileReleaseRuntime(
        manifest,
        expected_source_head="a" * 40,
        candidate_root=bundle_root,
    )

    result = runtime.result("teacher_flow")

    assert switched
    assert result is not None
    assert result.status == "fail"
    assert "boundary" in result.detail or "changed" in result.detail


def test_probe_command_record_is_stable_across_verifier_source_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    _bind_gateway_expected_environment(monkeypatch, tmp_path)
    module.SCRIPTS_ROOT = Path("E:/different-verifier-root/scripts")

    result = module.verify(
        _complete_bundle_runtime(
            module,
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    manifest, _, _ = _write_complete_bundle(
        tmp_path,
        module,
        source_head="c" * 40,
    )
    _bind_gateway_expected_environment(monkeypatch, tmp_path)
    lock = json.loads((tmp_path / "deploy" / "image-lock.json").read_text())
    references = {name: record["reference"] for name, record in lock["images"].items()}
    platform = f"""\
x-images:
  deeptutor: &deeptutor-image {json.dumps(references["deeptutor"])}
  openmaic: &openmaic-image {json.dumps(references["openmaic"])}
  openmaic-render: &openmaic-render-image {json.dumps(references["openmaic_render"])}
  nginx: &nginx-image {json.dumps(references["nginx"])}
  postgres: &postgres-image {json.dumps(references["postgres"])}
  minio: &minio-image {json.dumps(references["minio"])}
  minio-client: &minio-client-image {json.dumps(references["minio_client"])}
x-teaching-process: &teaching-process
  image: *deeptutor-image
  networks:
    - platform-internal
services:
  pocketbase:
    ports: !reset []
    profiles: [legacy]
  deeptutor:
    image: *deeptutor-image
    build: !reset null
    networks: !override
      - platform-internal
      - platform-service-egress
  gateway:
    image: *nginx-image
    networks:
      - platform-internal
      - platform-edge
  postgres:
    image: *postgres-image
    networks:
      - platform-internal
  minio:
    image: *minio-image
    networks:
      - platform-internal
  minio-bootstrap:
    image: *minio-client-image
    restart: "no"
    networks:
      - platform-internal
  teaching-migrate:
    image: *deeptutor-image
    restart: "no"
    networks:
      - platform-internal
  tenant-provisioner:
    <<: *teaching-process
  shared-data-plane-bootstrap:
    image: *deeptutor-image
    restart: "no"
    networks:
      - platform-internal
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
    networks:
      - platform-internal
      - shared-provider-egress
  openmaic-render:
    image: *openmaic-render-image
    networks:
      - platform-internal
networks:
  platform-internal:
    internal: true
  platform-edge: {{}}
  platform-service-egress: {{}}
  shared-provider-egress: {{}}
"""
    (tmp_path / "docker-compose.platform.yml").write_text(platform, encoding="utf-8")

    result = module.verify(
        _complete_bundle_runtime(
            module,
            manifest,
            expected_source_head="c" * 40,
        )
    )

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
    _bind_gateway_expected_environment(monkeypatch, tmp_path)
    observed_heads = iter((source_head, ""))
    monkeypatch.setattr(module, "_git_head", lambda: next(observed_heads))

    exit_code = module.main(
        [
            "--evidence",
            str(manifest),
            "--json",
            *_gateway_cli_arguments(tmp_path),
        ]
    )
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
    _bind_gateway_expected_environment(monkeypatch, candidate_root)
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
            *_gateway_cli_arguments(candidate_root),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ready"
    assert payload["candidate"]["sourceHead"] == source_head


@pytest.mark.parametrize("source", ("cli", "environment"))
def test_verifier_cli_resolves_trusted_gateway_docker_host_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    module = _load_verifier()
    captured: dict[str, object] = {}

    class CapturingRuntime:
        def __init__(self, path: Path, **kwargs) -> None:
            captured["path"] = path
            captured.update(kwargs)

    result = SimpleNamespace(ok=True)
    monkeypatch.setattr(module, "FileReleaseRuntime", CapturingRuntime)
    monkeypatch.setattr(module, "verify", lambda _runtime: result)
    monkeypatch.setattr(module, "report_payload", lambda _result: {"status": "ready"})
    monkeypatch.setattr(module, "_git_head", lambda: "a" * 40)
    monkeypatch.delenv("YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256", raising=False)
    argv = [
        "--evidence",
        str(tmp_path / "evidence.json"),
        "--gateway-trust-keyring",
        str(tmp_path / "gateway-trust-keyring.json"),
        "--gateway-trust-keyring-sha256",
        "6" * 64,
        "--gateway-observer-challenge",
        "8" * 64,
        "--gateway-host-challenge",
        "9" * 64,
        "--gateway-trusted-now",
        "2026-08-30T04:05:00Z",
        "--json",
        *_static_openmaic_cli_arguments(),
    ]
    if source == "cli":
        argv.extend(
            [
                "--gateway-docker-host-identity-sha256",
                _GATEWAY_DOCKER_HOST_IDENTITY_SHA256,
            ]
        )
    else:
        monkeypatch.setenv(
            "YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256",
            _GATEWAY_DOCKER_HOST_IDENTITY_SHA256,
        )

    assert module.main(argv) == 0
    assert captured["expected_gateway_docker_host_identity_sha256"] == (
        _GATEWAY_DOCKER_HOST_IDENTITY_SHA256
    )


def test_verify_cli_requires_and_forwards_gateway_external_trust_inputs_before_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    captured: dict[str, object] = {}
    verify_calls = 0
    keyring = tmp_path / "trusted" / "gateway-trust-keyring.json"
    trust_inputs = (
        (
            "--gateway-trust-keyring",
            "YFEISTAI_GATEWAY_TRUST_KEYRING",
            "trusted_keyring_path",
            str(keyring),
            str(keyring.with_name("other-keyring.json")),
        ),
        (
            "--gateway-trust-keyring-sha256",
            "YFEISTAI_GATEWAY_TRUST_KEYRING_SHA256",
            "expected_trusted_keyring_sha256",
            "6" * 64,
            "7" * 64,
        ),
        (
            "--gateway-observer-challenge",
            "YFEISTAI_GATEWAY_OBSERVER_CHALLENGE",
            "expected_observer_challenge",
            "8" * 64,
            "a" * 64,
        ),
        (
            "--gateway-host-challenge",
            "YFEISTAI_GATEWAY_HOST_CHALLENGE",
            "expected_host_challenge",
            "9" * 64,
            "b" * 64,
        ),
        (
            "--gateway-trusted-now",
            "YFEISTAI_GATEWAY_TRUSTED_NOW",
            "trusted_now",
            "2026-08-30T04:00:30Z",
            "2026-08-30T04:00:31Z",
        ),
    )

    class CapturingRuntime:
        def __init__(self, path: Path, **kwargs) -> None:
            captured["path"] = path
            captured.update(kwargs)

    result = SimpleNamespace(ok=True)

    def verify(runtime) -> SimpleNamespace:
        nonlocal verify_calls
        verify_calls += 1
        assert isinstance(runtime, CapturingRuntime)
        return result

    monkeypatch.setattr(module, "FileReleaseRuntime", CapturingRuntime)
    monkeypatch.setattr(module, "verify", verify)
    monkeypatch.setattr(module, "report_payload", lambda _result: {"status": "ready"})
    monkeypatch.setattr(module, "_git_head", lambda: "a" * 40)
    for _flag, environment_name, _keyword, _value, _mismatch in trust_inputs:
        monkeypatch.delenv(environment_name, raising=False)
    monkeypatch.delenv("YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256", raising=False)

    def argv(*, omitted_flag: str | None = None) -> list[str]:
        arguments = [
            "--evidence",
            str(tmp_path / "release-evidence.json"),
            "--gateway-docker-host-identity-sha256",
            "5" * 64,
            "--json",
            *_static_openmaic_cli_arguments(),
        ]
        for flag, _environment_name, _keyword, value, _mismatch in trust_inputs:
            if flag != omitted_flag:
                arguments.extend((flag, value))
        return arguments

    assert module.main(argv()) == 0
    assert verify_calls == 1
    assert captured["expected_gateway_docker_host_identity_sha256"] == "5" * 64
    for flag, _environment_name, keyword, value, _mismatch in trust_inputs:
        expected = Path(value) if flag == "--gateway-trust-keyring" else value
        assert captured[keyword] == expected

    for flag, _environment_name, _keyword, _value, _mismatch in trust_inputs:
        before = verify_calls
        with pytest.raises(SystemExit):
            module.main(argv(omitted_flag=flag))
        assert verify_calls == before

    for _flag, environment_name, keyword, value, mismatch in trust_inputs:
        monkeypatch.setenv(environment_name, value)
        captured.clear()
        assert module.main(argv()) == 0
        expected = Path(value) if keyword == "trusted_keyring_path" else value
        assert captured[keyword] == expected

        monkeypatch.setenv(environment_name, mismatch)
        before = verify_calls
        with pytest.raises(SystemExit):
            module.main(argv())
        assert verify_calls == before
        monkeypatch.delenv(environment_name)


def test_verify_cli_requires_and_forwards_openmaic_observer_trust_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    expected_flags = {
        "--openmaic-observer-attestation-sha256",
        "--openmaic-observer-id",
        "--openmaic-observer-origin",
        "--openmaic-shared-ingress-control-origin",
    }
    assert expected_flags <= set(module._parser()._option_string_actions)

    captured: dict[str, object] = {}

    class CapturingRuntime:
        def __init__(self, path: Path, **kwargs) -> None:
            captured["path"] = path
            captured.update(kwargs)

    result = SimpleNamespace(ok=True)
    monkeypatch.setattr(module, "FileReleaseRuntime", CapturingRuntime)
    monkeypatch.setattr(module, "verify", lambda _runtime: result)
    monkeypatch.setattr(module, "report_payload", lambda _result: {"status": "ready"})
    monkeypatch.setattr(module, "_git_head", lambda: "a" * 40)

    observer_inputs = (
        (
            "--openmaic-observer-attestation-sha256",
            "expected_openmaic_observer_attestation_sha256",
            "9" * 64,
        ),
        ("--openmaic-observer-id", "expected_openmaic_observer_id", _OPENMAIC_OBSERVER_ID),
        (
            "--openmaic-observer-origin",
            "expected_openmaic_observer_origin",
            _OPENMAIC_OBSERVER_ORIGIN,
        ),
        (
            "--openmaic-shared-ingress-control-origin",
            "expected_openmaic_shared_ingress_control_origin",
            _OPENMAIC_CONTROL_ORIGIN,
        ),
    )

    def argv(*, omitted_flag: str | None = None) -> list[str]:
        arguments = [
            "--evidence",
            str(tmp_path / "release-evidence.json"),
            "--gateway-docker-host-identity-sha256",
            "5" * 64,
            "--gateway-trust-keyring",
            str(tmp_path / "gateway-trust-keyring.json"),
            "--gateway-trust-keyring-sha256",
            "6" * 64,
            "--gateway-observer-challenge",
            "8" * 64,
            "--gateway-host-challenge",
            "7" * 64,
            "--gateway-trusted-now",
            "2026-08-30T04:05:00Z",
            "--json",
        ]
        for flag, _keyword, value in observer_inputs:
            if flag != omitted_flag:
                arguments.extend((flag, value))
        return arguments

    assert module.main(argv()) == 0
    for _flag, keyword, value in observer_inputs:
        assert captured[keyword] == value

    for flag, _keyword, _value in observer_inputs:
        with pytest.raises(SystemExit):
            module.main(argv(omitted_flag=flag))


@pytest.mark.parametrize("case", ("missing", "zero"))
def test_verifier_cli_rejects_absent_or_zero_gateway_docker_host_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    module = _load_verifier()
    monkeypatch.setattr(
        module,
        "FileReleaseRuntime",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid gateway Docker host identity reached evidence verification"
        ),
    )
    attempts = [([], None)]
    if case == "zero":
        attempts = [
            (["--gateway-docker-host-identity-sha256", "0" * 64], None),
            ([], "0" * 64),
        ]
    for extra_arguments, environment_value in attempts:
        monkeypatch.delenv(
            "YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256",
            raising=False,
        )
        if environment_value is not None:
            monkeypatch.setenv(
                "YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256",
                environment_value,
            )
        with pytest.raises(SystemExit):
            module.main(
                [
                    "--evidence",
                    str(tmp_path / "evidence.json"),
                    "--gateway-trust-keyring",
                    str(tmp_path / "gateway-trust-keyring.json"),
                    "--gateway-trust-keyring-sha256",
                    "6" * 64,
                    "--gateway-observer-challenge",
                    "8" * 64,
                    "--gateway-host-challenge",
                    "9" * 64,
                    "--gateway-trusted-now",
                    "2026-08-30T04:05:00Z",
                    "--json",
                    *extra_arguments,
                ]
            )


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


@pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX directory cleanup")
def test_open_posix_directory_no_follow_closes_current_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    current_fd = 741
    closed: list[int] = []

    def open_directory(path, _flags, *args, **kwargs) -> int:
        if path == "/":
            assert not args and not kwargs
            return current_fd
        assert path == "bundle"
        assert kwargs == {"dir_fd": current_fd}
        raise KeyboardInterrupt("injected POSIX directory open interruption")

    monkeypatch.setattr(module.os, "open", open_directory)
    monkeypatch.setattr(module.os, "close", closed.append)

    with pytest.raises(KeyboardInterrupt, match="injected POSIX directory open interruption"):
        module._open_posix_directory_no_follow(Path("/bundle/runtime"))

    assert closed == [current_fd]


@pytest.mark.skipif(sys.platform != "win32", reason="exercises Windows directory cleanup")
def test_open_windows_directory_no_follow_closes_current_on_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    current_handle = object()
    closed: list[object] = []

    monkeypatch.setattr(
        module,
        "_open_windows_directory_handle",
        lambda _path, *, deletable=False: (current_handle, (1, 1)),
    )

    def fail_relative(handle: object, component: str) -> tuple[object, tuple[int, int]]:
        assert handle is current_handle
        assert component == "bundle"
        raise SystemExit("injected Windows directory open interruption")

    monkeypatch.setattr(module, "_open_windows_directory_relative", fail_relative)
    monkeypatch.setattr(module, "_close_windows_handle", closed.append)

    with pytest.raises(SystemExit, match="injected Windows directory open interruption"):
        module._open_windows_directory_no_follow(Path("C:/bundle/runtime"))

    assert closed == [current_handle]


def _rewrite_gateway_artifact(
    tmp_path: Path,
    module,
    manifest: Path,
    evidence_map: dict[str, object],
    document: dict[str, object],
) -> None:
    entry = evidence_map["gateway_only_public"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    body = json.dumps(document, sort_keys=True).encode()
    artifact_path.write_bytes(body)
    entry["artifactSha256"] = hashlib.sha256(body).hexdigest()
    manifest.write_text(
        json.dumps(_manifest_document(module, document["candidate"], evidence_map)),
        encoding="utf-8",
    )


def _bind_gateway_expected_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> str:
    support = _load_gateway_public_support()
    attestation_path = tmp_path / "runtime" / "gateway-external-observer-attestation.json"
    monkeypatch.setenv(
        support.EXPECTED_ATTESTATION_ENV,
        hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv(support.EXPECTED_OBSERVER_ID_ENV, support.OBSERVER_ID)
    monkeypatch.setenv(support.EXPECTED_OBSERVER_ORIGIN_ENV, support.OBSERVER_ORIGIN)
    monkeypatch.setenv(support.TRUSTED_NOW_ENV, support.TRUSTED_NOW)
    monkeypatch.setenv(support.RUN_STARTED_AT_ENV, support.RUN_STARTED_AT)
    monkeypatch.setenv(support.RUN_ENDED_AT_ENV, support.RUN_ENDED_AT)
    monkeypatch.setenv(
        support.EXPECTED_DOCKER_HOST_IDENTITY_ENV,
        str(_gateway_trust_pair_from_bundle(tmp_path)["host_receipt_sha256"]),
    )
    sentinel = "gateway-verifier-sentinel-secret-must-not-leak"
    monkeypatch.setenv("YFEISTAI_GATEWAY_SENTINEL_SECRET", sentinel)
    return sentinel


def _gateway_host_identity_proof(
    tmp_path: Path,
    module,
    monkeypatch: pytest.MonkeyPatch,
    *,
    proof_context: str | None = None,
    proof_endpoint: str | None = None,
    proof_server_id: str | None = None,
    proof_identity_sha256: str | None = None,
) -> tuple[dict[str, object], bytes, dict[str, object]]:
    support = _load_gateway_public_support()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    trust_pair = _gateway_trust_pair_from_bundle(tmp_path)
    trusted_host_identity_sha256 = str(trust_pair["host_receipt_sha256"])
    runtime_path = tmp_path / "runtime" / "runtime-attestation.json"
    runtime_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()

    def validate_runtime(_path: Path, **kwargs):
        assert kwargs["expected_sha256"] == runtime_sha256
        return json.loads(runtime_path.read_bytes())

    monkeypatch.setattr(module, "validate_runtime_attestation", validate_runtime)
    _bind_gateway_expected_environment(monkeypatch, tmp_path)
    proof = json.loads((tmp_path / "runtime" / "gateway-only-public-attestation.json").read_bytes())
    proof["docker"]["daemon"]["context"] = proof_context or support.DOCKER_CONTEXT
    proof["docker"]["daemon"]["endpoint"] = proof_endpoint or support.DOCKER_ENDPOINT
    proof["docker"]["daemon"]["serverId"] = proof_server_id or support.DOCKER_SERVER_ID
    proof["docker"]["daemon"]["dockerHostIdentitySha256"] = (
        proof_identity_sha256 or trusted_host_identity_sha256
    )
    return candidate, support.canonical_json(proof), trust_pair


def _gateway_external_trust_proof(
    tmp_path: Path,
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    support = _load_gateway_public_support()
    _manifest, _evidence, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    runtime_path = tmp_path / "runtime" / "runtime-attestation.json"
    trust_pair = _gateway_trust_pair_from_bundle(tmp_path)
    runtime_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()

    def validate_runtime(path: Path, **kwargs):
        assert Path(path) == runtime_path
        assert kwargs["expected_sha256"] == runtime_sha256
        return json.loads(runtime_path.read_bytes())

    monkeypatch.setattr(module, "validate_runtime_attestation", validate_runtime)
    _bind_gateway_expected_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(
        support.EXPECTED_DOCKER_HOST_IDENTITY_ENV,
        str(trust_pair["host_receipt_sha256"]),
    )
    proof = json.loads((tmp_path / "runtime" / "gateway-only-public-attestation.json").read_bytes())
    return candidate, proof, trust_pair


@pytest.mark.parametrize(
    "trust_case",
    (
        "valid",
        "observer-envelope-tampered",
        "host-envelope-tampered",
        "host-receipt-tampered",
        "keyring-tampered",
    ),
)
def test_gateway_only_public_verifier_replays_external_trust_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trust_case: str,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    candidate, proof, trust_pair = _gateway_external_trust_proof(
        tmp_path,
        module,
        monkeypatch,
    )
    if trust_case != "valid":
        input_name = trust_case.removesuffix("-tampered")
        path_keys = {
            "observer-envelope": "observer_envelope_path",
            "host-envelope": "host_envelope_path",
            "host-receipt": "host_receipt_path",
            "keyring": "keyring_path",
        }
        reference_keys = {
            "observer-envelope": "observerEnvelope",
            "host-envelope": "hostProvisionerEnvelope",
            "host-receipt": "hostProvisioningReceipt",
        }
        path = Path(trust_pair[path_keys[input_name]])
        path.write_bytes(path.read_bytes() + b" ")
        if input_name in reference_keys:
            proof["trustPair"][reference_keys[input_name]]["sha256"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        assert proof["summary"]["checks"] == {
            "gatewayPublic": True,
            "internalPortsClosed": True,
        }

    arguments = {
        "bundle_root": tmp_path,
        "candidate_root": tmp_path,
        "candidate": candidate,
        "release_run": _RELEASE_RUN,
        "expected_docker_host_identity_sha256": str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
    }
    proof_body = support.canonical_json(proof)
    if trust_case == "valid":
        checks, observed_at = module.derive_gateway_only_public_receipt_checks(
            proof_body,
            **arguments,
        )
        assert checks == {"gatewayPublic": True, "internalPortsClosed": True}
        assert observed_at == "2026-08-30T04:00:01Z"
    else:
        with pytest.raises(ValueError, match="gateway trust"):
            module.derive_gateway_only_public_receipt_checks(
                proof_body,
                **arguments,
            )


@pytest.mark.parametrize("legacy_environment_case", ("absent", "conflicting"))
def test_gateway_verifier_uses_signed_observer_policy_without_legacy_trust_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_environment_case: str,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    candidate, proof, trust_pair = _gateway_external_trust_proof(
        tmp_path,
        module,
        monkeypatch,
    )
    legacy_environment = {
        support.EXPECTED_ATTESTATION_ENV: "f" * 64,
        support.EXPECTED_OBSERVER_ID_ENV: "conflicting-external-observer",
        support.EXPECTED_OBSERVER_ORIGIN_ENV: "https://conflicting-observer.example.net",
        support.TRUSTED_NOW_ENV: "2026-08-30T04:06:00Z",
        support.RUN_STARTED_AT_ENV: "2026-08-30T04:03:00Z",
        support.RUN_ENDED_AT_ENV: "2026-08-30T04:09:00Z",
    }
    for name, conflicting_value in legacy_environment.items():
        if legacy_environment_case == "absent":
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, conflicting_value)

    checks, observed_at = module.derive_gateway_only_public_receipt_checks(
        support.canonical_json(proof),
        bundle_root=tmp_path,
        candidate_root=tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
    )

    assert checks == {"gatewayPublic": True, "internalPortsClosed": True}
    assert observed_at == "2026-08-30T04:00:01Z"


@pytest.mark.parametrize(
    "trust_case",
    (
        "valid",
        "observer-envelope-tampered",
        "host-envelope-tampered",
        "host-receipt-tampered",
        "keyring-tampered",
    ),
)
def test_file_runtime_replays_gateway_external_trust_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trust_case: str,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    candidate, proof, trust_pair = _gateway_external_trust_proof(
        tmp_path,
        module,
        monkeypatch,
    )
    if trust_case != "valid":
        input_name = trust_case.removesuffix("-tampered")
        path_keys = {
            "observer-envelope": "observer_envelope_path",
            "host-envelope": "host_envelope_path",
            "host-receipt": "host_receipt_path",
            "keyring": "keyring_path",
        }
        reference_keys = {
            "observer-envelope": "observerEnvelope",
            "host-envelope": "hostProvisionerEnvelope",
            "host-receipt": "hostProvisioningReceipt",
        }
        path = Path(trust_pair[path_keys[input_name]])
        path.write_bytes(path.read_bytes() + b" ")
        if input_name in reference_keys:
            reference = proof["trustPair"][reference_keys[input_name]]
            assert isinstance(reference, dict)
            reference["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        assert proof["summary"]["checks"] == {
            "gatewayPublic": True,
            "internalPortsClosed": True,
        }

    proof_path = tmp_path / "runtime" / "gateway-only-public-attestation.json"
    proof_path.write_bytes(support.canonical_json(proof))
    manifest = tmp_path / "release-evidence.json"
    manifest_document = json.loads(manifest.read_bytes())
    evidence_map = manifest_document["evidence"]
    assert isinstance(evidence_map, dict)
    entry = evidence_map["gateway_only_public"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    artifact = json.loads(artifact_path.read_bytes())
    artifact["provenance"] = support.receipt_provenance(proof_path)
    _rewrite_gateway_artifact(tmp_path, module, manifest, evidence_map, artifact)

    runtime = module.FileReleaseRuntime(
        manifest,
        expected_source_head=str(candidate["sourceHead"]),
        candidate_root=tmp_path,
        expected_gateway_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
    )
    result = runtime.result("gateway_only_public")
    assert result is not None
    if trust_case == "valid":
        assert result.status == "pass"
    else:
        assert result.status == "fail"
        assert "gateway trust" in result.detail


def _write_fixed_gateway_candidate_networks(tmp_path: Path, support) -> None:
    runtime = json.loads((tmp_path / "runtime" / "runtime-attestation.json").read_bytes())
    fixed_compose = Path(__file__).resolve().parents[2] / "docker-compose.platform.yml"
    expected_networks = support.expected_service_networks(fixed_compose, runtime)
    prefix = f"{support.DOCKER_PROJECT}_"
    logical_networks = {
        service: [name.removeprefix(prefix) for name in networks]
        for service, networks in expected_networks.items()
    }
    assert all(
        name.startswith(prefix) for networks in expected_networks.values() for name in networks
    )

    compose_path = tmp_path / "docker-compose.platform.yml"
    compose = json.loads(compose_path.read_bytes())
    services = compose["services"]
    for service, networks in logical_networks.items():
        services[service]["networks"] = networks
    compose["networks"] = {
        network: {}
        for network in sorted(
            {network for networks in logical_networks.values() for network in networks}
        )
    }
    compose_path.write_text(json.dumps(compose, sort_keys=True), encoding="utf-8")


def test_gateway_only_public_uses_external_probe_contract() -> None:
    module = _load_verifier()

    assert module.RECEIPT_CONTRACTS["gateway_only_public"] == (
        "gateway-external-probe",
        ("gatewayPublic", "internalPortsClosed"),
    )


def test_gateway_only_public_verifier_binds_runtime_and_proof_to_trusted_docker_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    candidate, proof_body, trust_pair = _gateway_host_identity_proof(
        tmp_path,
        module,
        monkeypatch,
    )

    checks, observed_at = module.derive_gateway_only_public_receipt_checks(
        proof_body,
        bundle_root=tmp_path,
        candidate_root=tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
    )

    assert checks == {"gatewayPublic": True, "internalPortsClosed": True}
    assert observed_at == "2026-08-30T04:00:01Z"


@pytest.mark.parametrize(
    "case",
    ("context", "endpoint", "server-id", "host-identity"),
)
def test_gateway_only_public_verifier_rejects_self_consistent_untrusted_docker_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    fixture_kwargs: dict[str, str] = {}
    if case == "context":
        fixture_kwargs["proof_context"] = "attacker"
    elif case == "endpoint":
        fixture_kwargs["proof_endpoint"] = "npipe:////./pipe/attacker"
    elif case == "server-id":
        fixture_kwargs["proof_server_id"] = "daemon-attacker"
    else:
        fixture_kwargs["proof_identity_sha256"] = "8" * 64
    candidate, proof_body, trust_pair = _gateway_host_identity_proof(
        tmp_path,
        module,
        monkeypatch,
        **fixture_kwargs,
    )

    with pytest.raises(ValueError, match="Docker host identity"):
        module.derive_gateway_only_public_receipt_checks(
            proof_body,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
        )


@pytest.mark.parametrize(
    "replacement_kind",
    (
        "directory",
        pytest.param(
            "windows-reparse",
            marks=pytest.mark.skipif(
                os.name != "nt",
                reason="exercises a Windows directory reparse race",
            ),
        ),
    ),
)
def test_gateway_only_public_verifier_rejects_byte_identical_raw_ancestor_replacement_during_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    candidate, proof_body, trust_pair = _gateway_host_identity_proof(
        tmp_path,
        module,
        monkeypatch,
    )
    raw_root = tmp_path / "raw"
    retained_root = tmp_path / "raw.retained"
    alternate_root = tmp_path / "raw.alternate"
    alternate_root.mkdir()
    report_path = raw_root / "gateway-public-observation.json"
    assert report_path.is_file()
    (alternate_root / report_path.name).write_bytes(report_path.read_bytes())
    original_parse = module.parse_gateway_public_report
    attack_outcome: str | None = None

    def parse_after_replacement(*args, **kwargs):
        nonlocal attack_outcome
        if attack_outcome is None:
            raw_moved = False
            try:
                os.replace(raw_root, retained_root)
                raw_moved = True
                if replacement_kind == "directory":
                    os.replace(alternate_root, raw_root)
                else:
                    try:
                        raw_root.symlink_to(alternate_root, target_is_directory=True)
                    except OSError:
                        os.replace(retained_root, raw_root)
                        pytest.skip(
                            "directory reparse points are unavailable on this Windows test host"
                        )
            except OSError as exc:
                if raw_moved and not os.path.lexists(raw_root):
                    os.replace(retained_root, raw_root)
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32}:
                    raise
                attack_outcome = "permission-blocked"
                raise ValueError(
                    "gateway external observation ancestor replacement was blocked by the held lease"
                ) from exc
            attack_outcome = "identity-rejected"
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(module, "parse_gateway_public_report", parse_after_replacement)

    with pytest.raises(
        ValueError,
        match="external observation|ancestor|boundary|changed|reparse|blocked|lease",
    ):
        module.derive_gateway_only_public_receipt_checks(
            proof_body,
            bundle_root=tmp_path,
            candidate_root=tmp_path,
            candidate=candidate,
            release_run=_RELEASE_RUN,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
        )

    assert attack_outcome in {"permission-blocked", "identity-rejected"}
    if attack_outcome == "permission-blocked":
        assert raw_root.is_dir()
        assert not raw_root.is_symlink()
        assert not retained_root.exists()


def test_file_runtime_accepts_replayed_gateway_only_public_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    manifest, _evidence, _candidate_document = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    sentinel = _bind_gateway_expected_environment(monkeypatch, tmp_path)

    result = module.verify(
        _complete_bundle_runtime(
            module,
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["gateway_only_public"].status == "pass"
    assert sentinel not in repr(result.layers["gateway_only_public"])


def test_file_runtime_accepts_gateway_proof_with_exact_candidate_compose_network_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    contract = _load_gateway_public_contract()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    trust_pair = _gateway_trust_pair_from_bundle(tmp_path)
    sentinel = _bind_gateway_expected_environment(monkeypatch, tmp_path)
    _write_fixed_gateway_candidate_networks(tmp_path, support)
    proof_path = tmp_path / "runtime" / "gateway-only-public-attestation.json"
    observer_sha256 = hashlib.sha256(
        (tmp_path / "runtime" / "gateway-external-observer-attestation.json").read_bytes()
    ).hexdigest()
    proof = support.proof_document(
        contract,
        root=tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
        attestation_sha256=observer_sha256,
        compose_path=tmp_path / "docker-compose.platform.yml",
        docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
    )
    proof["trustPair"] = support.gateway_trust_references(trust_pair)
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    entry = evidence_map["gateway_only_public"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    artifact = json.loads(artifact_path.read_bytes())
    artifact["provenance"] = support.receipt_provenance(proof_path)
    _rewrite_gateway_artifact(tmp_path, module, manifest, evidence_map, artifact)

    result = module.verify(
        _complete_bundle_runtime(
            module,
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["gateway_only_public"].status == "pass"
    assert sentinel not in repr(result.layers["gateway_only_public"])


@pytest.mark.parametrize("network_drift", ("missing-network", "additional-network"))
def test_file_runtime_rejects_gateway_network_set_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_drift: str,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    contract = _load_gateway_public_contract()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    trust_pair = _gateway_trust_pair_from_bundle(tmp_path)
    _bind_gateway_expected_environment(monkeypatch, tmp_path)
    _write_fixed_gateway_candidate_networks(tmp_path, support)
    proof_path = tmp_path / "runtime" / "gateway-only-public-attestation.json"
    observer_sha256 = hashlib.sha256(
        (tmp_path / "runtime" / "gateway-external-observer-attestation.json").read_bytes()
    ).hexdigest()
    proof = support.proof_document(
        contract,
        root=tmp_path,
        candidate=candidate,
        release_run=_RELEASE_RUN,
        attestation_sha256=observer_sha256,
        compose_path=tmp_path / "docker-compose.platform.yml",
        network_drift=network_drift,
        docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
    )
    proof["trustPair"] = support.gateway_trust_references(trust_pair)
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    entry = evidence_map["gateway_only_public"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    artifact = json.loads(artifact_path.read_bytes())
    artifact["provenance"] = support.receipt_provenance(proof_path)
    _rewrite_gateway_artifact(tmp_path, module, manifest, evidence_map, artifact)

    result = module.verify(
        _complete_bundle_runtime(
            module,
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["gateway_only_public"].status == "fail"
    assert "network set" in result.layers["gateway_only_public"].detail


def test_file_runtime_rejects_handwritten_gateway_self_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_verifier()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    _bind_gateway_expected_environment(monkeypatch, tmp_path)
    document = _artifact_document(module, candidate, "gateway_only_public")
    _rewrite_gateway_artifact(tmp_path, module, manifest, evidence_map, document)

    result = module.verify(
        _complete_bundle_runtime(
            module,
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["gateway_only_public"].status == "fail"
    assert (
        "gateway-only-public execution proof is missing or invalid"
        in result.layers["gateway_only_public"].detail
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("missing-proof", "does not exist"),
        ("proof-candidate", "candidate binding"),
        ("proof-runtime", "runtime attestation"),
        ("observer-input", "observer attestation"),
        ("external-observation", "external observation"),
        ("docker-port-set", "published ports"),
        ("daemon-endpoint", "host identity"),
        ("daemon-server", "host identity"),
        ("network-mode", "network mode"),
        ("docker-command-count", "Docker observation commands"),
    ),
)
def test_file_runtime_rejects_gateway_provenance_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: str,
) -> None:
    module = _load_verifier()
    support = _load_gateway_public_support()
    contract = _load_gateway_public_contract()
    manifest, evidence_map, candidate = _write_complete_bundle(
        tmp_path,
        module,
        source_head="a" * 40,
    )
    trust_pair = _gateway_trust_pair_from_bundle(tmp_path)
    _bind_gateway_expected_environment(monkeypatch, tmp_path)
    entry = evidence_map["gateway_only_public"]
    assert isinstance(entry, dict)
    artifact_path = tmp_path / str(entry["artifact"])
    artifact = json.loads(artifact_path.read_bytes())
    proof_path = tmp_path / "runtime" / "gateway-only-public-attestation.json"

    if case == "missing-proof":
        proof_path.rename(tmp_path / "runtime" / "gateway-only-public-attestation.missing")
    elif case == "observer-input":
        observer_path = tmp_path / "runtime" / "gateway-external-observer-attestation.json"
        observer_path.write_bytes(observer_path.read_bytes() + b" ")
    elif case == "external-observation":
        report_path = tmp_path / "raw" / "gateway-public-observation.json"
        report_path.write_bytes(report_path.read_bytes() + b" ")
    else:
        proof = json.loads(proof_path.read_bytes())
        if case == "proof-candidate":
            proof["candidate"]["sourceHead"] = "b" * 40
        elif case == "proof-runtime":
            proof["runtimeAttestation"]["sha256"] = "6" * 64
        elif case == "daemon-endpoint":
            proof["docker"]["daemon"]["endpoint"] = "npipe:////./pipe/attacker"
        elif case == "daemon-server":
            proof["docker"]["daemon"]["serverId"] = "daemon-attacker"
        elif case == "network-mode":
            proof["docker"]["beforeSnapshot"][0]["networkMode"] = "host"
            proof["docker"]["afterSnapshot"][0]["networkMode"] = "host"
        elif case == "docker-command-count":
            proof["docker"]["commands"].pop()
        else:
            observer_sha256 = hashlib.sha256(
                (tmp_path / "runtime" / "gateway-external-observer-attestation.json").read_bytes()
            ).hexdigest()
            proof["docker"] = support.proof_document(
                contract,
                root=tmp_path,
                candidate=candidate,
                release_run=_RELEASE_RUN,
                attestation_sha256=observer_sha256,
                drift="internal-service-port",
                docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            )["docker"]
        proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
        artifact["provenance"] = support.receipt_provenance(proof_path)
        _rewrite_gateway_artifact(tmp_path, module, manifest, evidence_map, artifact)

    result = module.verify(
        _complete_bundle_runtime(
            module,
            manifest,
            expected_source_head="a" * 40,
            candidate_root=tmp_path,
        )
    )

    assert result.layers["gateway_only_public"].status == "fail"
    assert expected in result.layers["gateway_only_public"].detail
