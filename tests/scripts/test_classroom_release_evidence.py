from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE_HEAD = "a" * 40
RELEASE_TAG = f"yfeistai-first-release-20260825-{SOURCE_HEAD[:8]}"
OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
RELEASE_RUN = {
    "runId": "first-release-run-20260825",
    "environmentId": "test-environment",
}
BASE_URL = "https://candidate.example.test"


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


def _load_runtime_attestation_producer():
    return _load_path(
        "classroom_runtime_attestation_for_release_evidence",
        ROOT / "scripts" / "classroom_runtime_attestation.py",
    )


def _load_runtime_attestation_support():
    return _load_path(
        "classroom_runtime_attestation_support_for_release_evidence",
        ROOT / "tests" / "scripts" / "test_classroom_runtime_attestation.py",
    )


def _load_verify_release_support():
    return _load_path(
        "verify_classroom_release_support_for_evidence_test",
        ROOT / "tests" / "scripts" / "test_verify_classroom_release.py",
    )


def _load_classroom_export_support():
    return _load_path(
        "classroom_export_contract_support_for_release_evidence",
        ROOT / "tests" / "scripts" / "test_classroom_export_contract.py",
    )


def _load_tenant_isolation_support():
    return _load_path(
        "tenant_isolation_contract_support_for_release_evidence",
        ROOT / "tests" / "scripts" / "test_tenant_isolation_contract.py",
    )


def _load_openmaic_smoke_support():
    return _load_path(
        "openmaic_smoke_contract_support_for_release_evidence",
        ROOT / "tests" / "scripts" / "test_openmaic_smoke_contract.py",
    )


def _load_gateway_public_support():
    return _load_path(
        "gateway_public_test_support_for_release_evidence",
        ROOT / "tests" / "scripts" / "gateway_public_test_support.py",
    )


def _load_gateway_public_contract():
    return _load_path(
        "gateway_public_contract_for_release_evidence",
        ROOT / "scripts" / "gateway_public_contract.py",
    )


def _load_backup_restore_support():
    return _load_path(
        "backup_restore_probe_support_for_release_evidence",
        ROOT / "tests" / "scripts" / "test_backup_restore_probe.py",
    )


def test_backup_restore_writer_requires_canonical_probe_output(tmp_path: Path) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate_document = _write_candidate_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    (bundle_root / "runtime" / "backup-restore").mkdir(parents=True)

    with pytest.raises(ValueError, match="backup restore report"):
        module.write_backup_restore_receipt(
            candidate_root=candidate_root,
            bundle_root=bundle_root,
            release_run=RELEASE_RUN,
            database_ownership="runner-owned-disposable",
            object_namespace_ownership="runner-owned-disposable",
        )

    assert not (bundle_root / "artifacts" / "backup_restore.json").exists()


def test_backup_restore_writer_and_assembler_bind_canonical_report(tmp_path: Path) -> None:
    module = _load_evidence_module()
    support = _load_backup_restore_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    report_path = support.write_release_probe_fixture(
        bundle_root,
        candidate=candidate,
        release_run=RELEASE_RUN,
    )

    receipt = module.write_backup_restore_receipt(
        candidate_root=candidate_root,
        bundle_root=bundle_root,
        release_run=RELEASE_RUN,
        database_ownership="runner-owned-disposable",
        object_namespace_ownership="runner-owned-disposable",
    )

    assert receipt["provenance"] == {
        "backupRestoreReport": {
            "artifact": "runtime/backup-restore/backup-restore-report.json",
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        }
    }
    assert receipt["receipt"]["result"]["checks"] == {
        "newDatabaseRestored": True,
        "distinctVersionedBucketRestored": True,
        "receiptsVerified": True,
    }
    manifest = module.assemble_manifest(
        bundle_root / "release-evidence.json",
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={"backup_restore": bundle_root / "artifacts" / "backup_restore.json"},
    )
    assert manifest["evidence"]["backup_restore"]["status"] == "pass"


def test_backup_restore_writer_preserves_existing_receipt_without_replace(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    support = _load_backup_restore_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    support.write_release_probe_fixture(
        bundle_root,
        candidate=candidate,
        release_run=RELEASE_RUN,
    )
    receipt_path = bundle_root / "artifacts" / "backup_restore.json"
    receipt_path.parent.mkdir()
    receipt_path.write_bytes(b"competing-receipt\n")

    with pytest.raises(FileExistsError, match="already exists"):
        module.write_backup_restore_receipt(
            candidate_root=candidate_root,
            bundle_root=bundle_root,
            release_run=RELEASE_RUN,
            database_ownership="runner-owned-disposable",
            object_namespace_ownership="runner-owned-disposable",
        )

    assert receipt_path.read_bytes() == b"competing-receipt\n"


def test_backup_restore_writer_rejects_artifacts_parent_replacement_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    support = _load_backup_restore_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    support.write_release_probe_fixture(
        bundle_root,
        candidate=candidate,
        release_run=RELEASE_RUN,
    )
    artifacts_root = bundle_root / "artifacts"
    artifacts_root.mkdir()
    retained_root = bundle_root / "artifacts.retained"
    alternate_root = bundle_root / "artifacts.alternate"
    alternate_root.mkdir()
    original_publish = module._publish_classroom_no_replace
    attack_outcome: str | None = None

    def replace_parent(boundary, source, target, *, source_handle) -> None:
        nonlocal attack_outcome
        try:
            os.replace(artifacts_root, retained_root)
            os.replace(alternate_root, artifacts_root)
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32}:
                raise
            attack_outcome = "permission-blocked"
            raise ValueError(
                "backup restore publication ancestor replacement was blocked by the held lease"
            ) from exc
        attack_outcome = "identity-rejected"
        original_publish(
            boundary,
            source,
            target,
            source_handle=source_handle,
        )

    monkeypatch.setattr(module, "_publish_classroom_no_replace", replace_parent)

    with pytest.raises(
        ValueError,
        match="ancestor|boundary|publication|changed|blocked|lease",
    ):
        module.write_backup_restore_receipt(
            candidate_root=candidate_root,
            bundle_root=bundle_root,
            release_run=RELEASE_RUN,
            database_ownership="runner-owned-disposable",
            object_namespace_ownership="runner-owned-disposable",
        )

    assert attack_outcome in {"permission-blocked", "identity-rejected"}
    assert not (artifacts_root / "backup_restore.json").exists()
    assert not (retained_root / "backup_restore.json").exists()


def test_backup_restore_writer_postcommit_cleanup_failure_preserves_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    support = _load_backup_restore_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    support.write_release_probe_fixture(
        bundle_root,
        candidate=candidate,
        release_run=RELEASE_RUN,
    )
    original_remove = module._remove_classroom_entries
    injected = False

    def fail_cleanup(boundary, entries, *, label: str) -> None:
        nonlocal injected
        original_remove(boundary, entries, label=label)
        if label == "backup restore staging evidence":
            injected = True
            raise OSError("injected backup restore staging cleanup failure")

    monkeypatch.setattr(module, "_remove_classroom_entries", fail_cleanup)

    receipt = module.write_backup_restore_receipt(
        candidate_root=candidate_root,
        bundle_root=bundle_root,
        release_run=RELEASE_RUN,
        database_ownership="runner-owned-disposable",
        object_namespace_ownership="runner-owned-disposable",
    )

    receipt_path = bundle_root / "artifacts" / "backup_restore.json"
    assert injected is True
    assert json.loads(receipt_path.read_bytes()) == receipt
    assert not list(receipt_path.parent.glob(".backup-restore-*.tmp"))


def _teacher_playwright_report() -> dict[str, object]:
    file = "tests/e2e/classroom-first-release.live.spec.ts"
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
                "specs": [
                    {
                        "title": "[release-evidence:teacher_flow] teacher flow",
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
                        "id": "first-release-live-teacher",
                        "file": file,
                        "line": 1,
                        "column": 1,
                    }
                ],
            }
        ],
        "errors": [],
        "stats": {
            "startTime": "2026-08-25T00:00:00.000Z",
            "duration": 10,
            "expected": 1,
            "unexpected": 0,
            "flaky": 0,
            "skipped": 0,
        },
    }


def _write_candidate_root(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    renderer = _load_renderer()
    candidate_root = tmp_path / "candidate"
    deploy = candidate_root / "deploy"
    deploy.mkdir(parents=True)
    base_compose = candidate_root / "docker-compose.yml"
    platform_compose = candidate_root / "docker-compose.platform.yml"
    data_plane_compose = candidate_root / "docker-compose.data-plane.yml"
    base_compose.write_bytes((ROOT / "docker-compose.yml").read_bytes())
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
    _write_runtime_attestation(candidate_root, lock)
    return candidate_root, lock["candidate"]


def _write_runtime_attestation(candidate_root: Path, lock: dict[str, object]) -> Path:
    producer = _load_runtime_attestation_producer()
    support = _load_runtime_attestation_support()
    candidate, expected_services = producer._load_candidate(candidate_root)
    assert candidate == lock["candidate"]
    base_compose = producer.yaml.load(
        (candidate_root / "docker-compose.yml").read_text(encoding="utf-8"),
        Loader=producer._ComposeLoader,
    )
    base_services = base_compose.get("services") if isinstance(base_compose, dict) else None
    assert isinstance(base_services, dict)

    def merged_restart(service: str, settings: dict[str, object]) -> str:
        restart = settings["restart"]
        if isinstance(restart, str) and restart:
            return restart
        base_service = base_services.get(service)
        assert isinstance(base_service, dict)
        inherited = base_service.get("restart", "no")
        assert isinstance(inherited, str) and inherited
        return inherited

    support.SERVICES = {
        service: {
            "restart": merged_restart(service, settings),
            "health": ("healthy" if service in producer._REQUIRED_HEALTHY_SERVICES else "none"),
        }
        for service, settings in expected_services.items()
    }
    references = {
        service: str(settings["image"]) for service, settings in expected_services.items()
    }
    producer.produce_runtime_attestation(
        candidate_root=candidate_root,
        deployment_root=ROOT,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        observed_at="2026-08-25T00:00:00Z",
        base_url=BASE_URL,
        runner=support.FakeDocker(references),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        environ={"SystemRoot": "C:/Windows"},
    )
    return candidate_root / "runtime" / "runtime-attestation.json"


def _write_teacher_probe_receipt(
    module,
    *,
    candidate_root: Path,
    candidate: dict[str, object],
    working_directory: Path,
) -> tuple[Path, Path, Path]:
    output = candidate_root / "artifacts" / "teacher_flow.json"
    raw_report = candidate_root / "raw" / "teacher_flow.json"
    execution_record = candidate_root / "raw" / "teacher_flow.execution.json"

    def run_probe(
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        report = _teacher_playwright_report()
        target = Path(env["YFEISTAI_EVIDENCE_REPORT"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0)

    module.run_probe_receipt(
        output,
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        evidence="teacher_flow",
        observed_at="2026-08-25T00:00:00Z",
        base_url=BASE_URL,
        raw_report_path=raw_report,
        execution_record_path=execution_record,
        recipe="teacher_flow",
        working_directory=working_directory,
        timeout_seconds=300,
        runner=run_probe,
    )
    return output, raw_report, execution_record


def test_pass_receipt_requires_explicit_checks_not_only_zero_exit(tmp_path: Path) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    output = tmp_path / "source-head.json"
    output.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="checks"):
        module.write_pass_receipt(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="source_head",
            observed_at="2026-08-25T00:00:00Z",
            native_exit=0,
            checks={"headMatches": True, "worktreeClean": False},
        )

    assert output.read_bytes() == b"sentinel"


def test_direct_pass_receipt_cannot_self_attest_probe_backed_evidence(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    output = tmp_path / "teacher-flow.json"
    output.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="probe"):
        module.write_pass_receipt(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="teacher_flow",
            observed_at="2026-08-25T00:00:00Z",
            native_exit=0,
            checks={"teacherFlowPassed": True},
        )

    assert output.read_bytes() == b"sentinel"


def test_probe_receipt_is_derived_from_executed_candidate_bound_report(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    output = candidate_root / "artifacts" / "teacher_flow.json"
    raw_report = candidate_root / "raw" / "teacher_flow.json"
    execution_record = candidate_root / "raw" / "teacher_flow.execution.json"
    raw_report.parent.mkdir()
    command = [
        sys.executable,
        str(ROOT / "scripts" / "classroom_release_probe.py"),
        "teacher_flow",
    ]
    captured: dict[str, object] = {}

    def run_probe(
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(arguments=arguments, cwd=cwd, env=env, timeout=timeout)
        report = _teacher_playwright_report()
        Path(env["YFEISTAI_EVIDENCE_REPORT"]).write_text(
            json.dumps(report),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    receipt = module.run_probe_receipt(
        output,
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        evidence="teacher_flow",
        observed_at="2026-08-25T00:00:00Z",
        base_url=BASE_URL,
        raw_report_path=raw_report,
        execution_record_path=execution_record,
        recipe="teacher_flow",
        working_directory=tmp_path,
        timeout_seconds=300,
        runner=run_probe,
    )

    raw_body = raw_report.read_bytes()
    assert captured["arguments"] == command
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["timeout"] == 300
    environment = captured["env"]
    assert isinstance(environment, dict)
    staged_report = Path(environment["YFEISTAI_EVIDENCE_REPORT"])
    assert staged_report != raw_report.resolve()
    assert staged_report.parent == raw_report.resolve().parent
    assert staged_report.name.startswith(".teacher_flow.json.")
    assert staged_report.name.endswith(".staging")
    assert environment["YFEISTAI_CANDIDATE_ROOT"] == str(candidate_root.resolve())
    assert environment["YFEISTAI_RELEASE_RUN_ID"] == RELEASE_RUN["runId"]
    assert environment["YFEISTAI_ENVIRONMENT_ID"] == RELEASE_RUN["environmentId"]
    assert environment["YFEISTAI_EVIDENCE"] == "teacher_flow"
    assert environment["YFEISTAI_PROBE_TIMEOUT_SECONDS"] == "270"
    assert environment["WEB_BASE_URL"] == BASE_URL
    assert not staged_report.exists()
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
    expected_command = module.probe_command_record("teacher_flow")
    attestation_path = candidate_root / "runtime" / "runtime-attestation.json"
    attestation_proof = {
        "artifact": "runtime/runtime-attestation.json",
        "sha256": hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
    }
    assert receipt["provenance"] == {
        "recipe": "teacher_flow",
        "command": expected_command,
        "rawReport": {
            "artifact": "raw/teacher_flow.json",
            "sha256": hashlib.sha256(raw_body).hexdigest(),
        },
        "execution": {
            "artifact": "raw/teacher_flow.execution.json",
            "sha256": hashlib.sha256(execution_record.read_bytes()).hexdigest(),
        },
        "runtimeAttestation": attestation_proof,
    }
    execution = json.loads(execution_record.read_text(encoding="utf-8"))
    assert execution == {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": RELEASE_RUN,
        "evidence": "teacher_flow",
        "recipe": "teacher_flow",
        "command": receipt["provenance"]["command"],
        "observedAt": "2026-08-25T00:00:00Z",
        "baseUrl": BASE_URL,
        "nativeExit": 0,
        "rawReportSha256": hashlib.sha256(raw_body).hexdigest(),
        "runtimeAttestation": attestation_proof,
    }


def test_probe_receipt_binds_the_fixed_runtime_attestation(tmp_path: Path) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path, _raw, execution_path = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    expected = {
        "artifact": "runtime/runtime-attestation.json",
        "sha256": hashlib.sha256(attestation.read_bytes()).hexdigest(),
    }

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    assert receipt["provenance"]["runtimeAttestation"] == expected
    assert execution["runtimeAttestation"] == expected
    assert execution["baseUrl"] == BASE_URL


def test_probe_reads_runtime_attestation_only_through_anchored_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    real_read_bytes = Path.read_bytes

    def reject_path_read(path: Path) -> bytes:
        if Path(os.path.abspath(path)) == Path(os.path.abspath(attestation)):
            pytest.fail("runtime attestation must not be read through a path lookup")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_path_read)

    receipt, _raw, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )

    assert receipt.is_file()


@pytest.mark.parametrize(
    "case",
    ("missing", "candidate", "release-run", "base-url", "digest", "state", "symlink"),
)
def test_probe_rejects_invalid_fixed_runtime_attestation_before_execution(
    tmp_path: Path, case: str
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    if case == "missing":
        attestation.replace(attestation.with_name("not-the-fixed-attestation.json"))
    elif case == "symlink":
        real = attestation.with_name("runtime-attestation.real.json")
        attestation.replace(real)
        try:
            attestation.symlink_to(real)
        except OSError:
            pytest.skip("symlinks are unavailable on this Windows test host")
    else:
        document = json.loads(attestation.read_text(encoding="utf-8"))
        if case == "candidate":
            document["candidate"]["sourceHead"] = "b" * 40
        elif case == "release-run":
            document["releaseRun"]["runId"] = "attacker-run"
        elif case == "base-url":
            document["baseUrl"] = "https://attacker.example.test"
        elif case == "digest":
            document["containers"][0]["repoDigests"] = [
                "registry.example/attacker@sha256:" + "f" * 64
            ]
        else:
            document["containers"][0]["restarting"] = True
        attestation.write_text(json.dumps(document), encoding="utf-8")

    def must_not_run(*_args, **_options):
        pytest.fail("Playwright must not run before runtime attestation validation")

    output = candidate_root / "artifacts" / "teacher_flow.json"
    with pytest.raises(ValueError, match="runtime|candidate"):
        module.run_probe_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="teacher_flow",
            observed_at="2026-08-25T00:00:00Z",
            base_url=BASE_URL,
            raw_report_path=candidate_root / "raw" / "teacher_flow.json",
            execution_record_path=candidate_root / "raw" / "teacher_flow.execution.json",
            recipe="teacher_flow",
            working_directory=tmp_path,
            timeout_seconds=300,
            runner=must_not_run,
        )

    assert not output.exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX nonblocking attestation opens")
def test_probe_rejects_runtime_attestation_fifo_before_execution(tmp_path: Path) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    attestation.replace(attestation.with_name("runtime-attestation.real.json"))
    os.mkfifo(attestation)

    def must_not_run(*_args, **_options):
        pytest.fail("Playwright must not run before bounded runtime attestation validation")

    output = candidate_root / "artifacts" / "teacher_flow.json"
    with pytest.raises(ValueError, match="runtime attestation|fixed boundary|regular"):
        module.run_probe_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="teacher_flow",
            observed_at="2026-08-25T00:00:00Z",
            base_url=BASE_URL,
            raw_report_path=candidate_root / "raw" / "teacher_flow.json",
            execution_record_path=candidate_root / "raw" / "teacher_flow.execution.json",
            recipe="teacher_flow",
            working_directory=tmp_path,
            timeout_seconds=300,
            runner=must_not_run,
        )

    assert not output.exists()


@pytest.mark.parametrize("case", ("changed", "missing"))
def test_probe_rejects_runtime_attestation_drift_after_execution(tmp_path: Path, case: str) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    output = candidate_root / "artifacts" / "teacher_flow.json"
    raw_report = candidate_root / "raw" / "teacher_flow.json"
    execution = candidate_root / "raw" / "teacher_flow.execution.json"

    def drift(arguments, *, cwd, env, timeout):
        del cwd, timeout
        staged = Path(env["YFEISTAI_EVIDENCE_REPORT"])
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps(_teacher_playwright_report()), encoding="utf-8")
        if case == "changed":
            attestation.write_bytes(attestation.read_bytes() + b"\n")
        else:
            attestation.replace(attestation.with_name("moved-during-probe.json"))
        return subprocess.CompletedProcess(arguments, 0)

    with pytest.raises(ValueError, match="changed|unavailable"):
        module.run_probe_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="teacher_flow",
            observed_at="2026-08-25T00:00:00Z",
            base_url=BASE_URL,
            raw_report_path=raw_report,
            execution_record_path=execution,
            recipe="teacher_flow",
            working_directory=tmp_path,
            timeout_seconds=300,
            runner=drift,
        )

    assert not output.exists()
    assert not raw_report.exists()
    assert not execution.exists()
    failure_directories = list((candidate_root / "failures" / "teacher_flow").glob("*"))
    assert len(failure_directories) == 1
    assert {path.name for path in failure_directories[0].iterdir()} == {"failure.json"}


@pytest.mark.parametrize("consumer", ("assembler", "file-runtime"))
def test_consumers_revalidate_runtime_attestation(tmp_path: Path, consumer: str) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt, _raw, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    manifest = candidate_root / "release-evidence.json"
    if consumer == "file-runtime":
        module.assemble_manifest(
            manifest,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"teacher_flow": receipt},
        )
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    attestation.write_bytes(attestation.read_bytes() + b"\n")

    if consumer == "assembler":
        with pytest.raises(ValueError, match="attestation.*digest"):
            module.assemble_manifest(
                manifest,
                candidate_root=candidate_root,
                release_run=RELEASE_RUN,
                receipt_paths={"teacher_flow": receipt},
            )
    else:
        result = verifier.verify(
            verifier.FileReleaseRuntime(
                manifest,
                expected_source_head=SOURCE_HEAD,
                candidate_root=candidate_root,
            )
        )
        assert result.layers["teacher_flow"].status == "fail"
        assert "attestation" in result.layers["teacher_flow"].detail


def test_probe_receipt_preserves_existing_output_when_command_fails(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    output = candidate_root / "artifacts" / "teacher_flow.json"
    output.parent.mkdir()
    output.write_bytes(b"sentinel")
    raw_report = candidate_root / "raw" / "teacher_flow.json"

    def fail_probe(*_args, **_options):
        pytest.fail("probe must not replace a pre-existing canonical receipt")

    with pytest.raises(ValueError, match="must not already exist"):
        module.run_probe_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="teacher_flow",
            observed_at="2026-08-25T00:00:00Z",
            base_url=BASE_URL,
            raw_report_path=raw_report,
            execution_record_path=candidate_root / "raw" / "teacher_flow.execution.json",
            recipe="teacher_flow",
            working_directory=tmp_path,
            timeout_seconds=300,
            runner=fail_probe,
        )

    assert output.read_bytes() == b"sentinel"
    assert not raw_report.exists()


def test_failed_probe_preserves_secret_free_diagnostics_and_canonical_paths_are_retryable(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    output = candidate_root / "artifacts" / "teacher_flow.json"
    raw_report = candidate_root / "raw" / "teacher_flow.json"
    execution = candidate_root / "raw" / "teacher_flow.execution.json"
    staged_paths: list[Path] = []

    def fail_probe(arguments, *, cwd, env, timeout):
        del cwd, timeout
        staged = Path(env["YFEISTAI_EVIDENCE_REPORT"])
        staged_paths.append(staged)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"native failure diagnostics")
        return subprocess.CompletedProcess(arguments, 7)

    with pytest.raises(ValueError, match="native exit 7"):
        module.run_probe_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="teacher_flow",
            observed_at="2026-08-25T00:00:00Z",
            base_url=BASE_URL,
            raw_report_path=raw_report,
            execution_record_path=execution,
            recipe="teacher_flow",
            working_directory=tmp_path,
            timeout_seconds=300,
            runner=fail_probe,
        )

    assert staged_paths[0] != raw_report.resolve()
    assert not output.exists()
    assert not raw_report.exists()
    assert not execution.exists()
    failure_directories = list((candidate_root / "failures" / "teacher_flow").glob("*"))
    assert len(failure_directories) == 1
    assert {path.name for path in failure_directories[0].iterdir()} == {"failure.json"}
    failure = json.loads((failure_directories[0] / "failure.json").read_bytes())
    assert failure["nativeExit"] == 7
    assert failure["artifacts"] == {}

    def pass_probe(arguments, *, cwd, env, timeout):
        del cwd, timeout
        staged = Path(env["YFEISTAI_EVIDENCE_REPORT"])
        staged_paths.append(staged)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps(_teacher_playwright_report()), encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0)

    receipt = module.run_probe_receipt(
        output,
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        evidence="teacher_flow",
        observed_at="2026-08-25T00:01:00Z",
        base_url=BASE_URL,
        raw_report_path=raw_report,
        execution_record_path=execution,
        recipe="teacher_flow",
        working_directory=tmp_path,
        timeout_seconds=300,
        runner=pass_probe,
    )

    assert staged_paths[1] != staged_paths[0]
    assert receipt == json.loads(output.read_text(encoding="utf-8"))
    assert raw_report.exists()
    assert execution.exists()
    assert (failure_directories[0] / "failure.json").exists()


@pytest.mark.parametrize("case", ("invalid-raw", "outer-timeout"))
def test_invalid_or_timed_out_probe_leaves_only_failure_diagnostics(
    tmp_path: Path,
    case: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    output = candidate_root / "artifacts" / "teacher_flow.json"
    raw_report = candidate_root / "raw" / "teacher_flow.json"
    execution = candidate_root / "raw" / "teacher_flow.execution.json"

    def fail_probe(arguments, *, cwd, env, timeout):
        del cwd
        staged = Path(env["YFEISTAI_EVIDENCE_REPORT"])
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"invalid or timed out diagnostics")
        if case == "outer-timeout":
            raise subprocess.TimeoutExpired(arguments, timeout)
        return subprocess.CompletedProcess(arguments, 0)

    with pytest.raises((ValueError, subprocess.TimeoutExpired)):
        module.run_probe_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            evidence="teacher_flow",
            observed_at="2026-08-25T00:00:00Z",
            base_url=BASE_URL,
            raw_report_path=raw_report,
            execution_record_path=execution,
            recipe="teacher_flow",
            working_directory=tmp_path,
            timeout_seconds=300,
            runner=fail_probe,
        )

    assert not output.exists()
    assert not raw_report.exists()
    assert not execution.exists()
    failure_directories = list((candidate_root / "failures" / "teacher_flow").glob("*"))
    assert len(failure_directories) == 1
    assert {path.name for path in failure_directories[0].iterdir()} == {"failure.json"}


def test_receipt_publication_failure_rolls_back_proof_and_allows_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    output = candidate_root / "artifacts" / "teacher_flow.json"
    raw_report = candidate_root / "raw" / "teacher_flow.json"
    execution = candidate_root / "raw" / "teacher_flow.execution.json"

    def pass_probe(arguments, *, cwd, env, timeout):
        del cwd, timeout
        staged = Path(env["YFEISTAI_EVIDENCE_REPORT"])
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps(_teacher_playwright_report()), encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0)

    real_replace = module.os.replace
    failed = False

    def fail_receipt_once(source: Path, target: Path) -> None:
        nonlocal failed
        if Path(target).resolve() == output.resolve() and not failed:
            failed = True
            raise OSError("simulated receipt publication failure")
        real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_receipt_once)
    arguments = {
        "candidate_root": candidate_root,
        "bundle_root": candidate_root,
        "release_run": RELEASE_RUN,
        "evidence": "teacher_flow",
        "base_url": BASE_URL,
        "raw_report_path": raw_report,
        "execution_record_path": execution,
        "recipe": "teacher_flow",
        "working_directory": tmp_path,
        "timeout_seconds": 300,
        "runner": pass_probe,
    }
    with pytest.raises(OSError, match="simulated receipt publication failure"):
        module.run_probe_receipt(
            output,
            observed_at="2026-08-25T00:00:00Z",
            **arguments,
        )

    assert not output.exists()
    assert not raw_report.exists()
    assert not execution.exists()
    failure_root = candidate_root / "failures" / "teacher_flow"
    failure_directories = list(failure_root.glob("*"))
    assert len(failure_directories) == 1
    assert {path.name for path in failure_directories[0].iterdir()} == {
        "execution.json",
        "failure.json",
    }

    receipt = module.run_probe_receipt(
        output,
        observed_at="2026-08-25T00:01:00Z",
        **arguments,
    )
    assert receipt == json.loads(output.read_text(encoding="utf-8"))
    assert raw_report.exists()
    assert execution.exists()


def test_manifest_assembler_rejects_tampered_probe_raw_report(tmp_path: Path) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt, raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    raw_report.write_bytes(raw_report.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="raw report"):
        module.assemble_manifest(
            candidate_root / "release-evidence.json",
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"teacher_flow": receipt},
        )


def test_file_runtime_rejects_tampered_probe_raw_report(tmp_path: Path) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt, raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    manifest = candidate_root / "release-evidence.json"
    module.assemble_manifest(
        manifest,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={"teacher_flow": receipt},
    )
    raw_report.write_bytes(raw_report.read_bytes() + b"\n")

    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )

    assert result.layers["teacher_flow"].status == "fail"
    assert "raw report" in result.layers["teacher_flow"].detail


def test_file_runtime_rejects_receipt_without_execution_proof(tmp_path: Path) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt, _raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    artifact = json.loads(receipt.read_text(encoding="utf-8"))
    artifact.pop("provenance")
    receipt.write_text(json.dumps(artifact), encoding="utf-8")
    manifest = candidate_root / "release-evidence.json"
    manifest_document = {
        "schemaVersion": 3,
        "candidate": candidate,
        "releaseRun": RELEASE_RUN,
        "evidence": {
            "teacher_flow": {
                "status": "pass",
                "detail": "teacher_flow verified by playwright",
                "artifact": "artifacts/teacher_flow.json",
                "artifactSha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            }
        },
    }
    manifest.write_text(json.dumps(manifest_document), encoding="utf-8")

    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )

    assert result.layers["teacher_flow"].status == "fail"
    assert "execution proof" in result.layers["teacher_flow"].detail


@pytest.mark.parametrize("consumer", ("assembler", "file-runtime"))
@pytest.mark.parametrize(
    "mutation",
    (
        "command-id",
        "version",
        "inner-argv",
        "live-spec",
        "project",
        "grep",
        "reporter",
        "workers",
        "retries",
        "report-format",
        "environment-policy",
        "descriptor-hash",
        "logical-launcher",
        "argv-hash",
        "recipe",
    ),
)
def test_consumers_reject_mutated_canonical_probe_command(
    tmp_path: Path,
    mutation: str,
    consumer: str,
) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt, _raw_report, execution_path = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    artifact = json.loads(receipt.read_text(encoding="utf-8"))
    provenance = artifact["provenance"]
    command = provenance["command"]
    descriptor = command["descriptor"]
    descriptor_mutations = {
        "command-id": ("commandId", "attacker.command"),
        "version": ("version", 2),
        "live-spec": ("liveSpec", "tests/e2e/classroom-first-release.spec.ts"),
        "project": ("project", "teaching-flow"),
        "grep": ("grep", "attacker"),
        "reporter": ("reporter", "line"),
        "workers": ("workers", 2),
        "retries": ("retries", 1),
        "report-format": ("reportFormat", "custom-summary"),
        "environment-policy": ("environmentPolicyVersion", 3),
    }
    if mutation == "inner-argv":
        descriptor["innerNpmArgv"].append("--headed")
    elif mutation in descriptor_mutations:
        name, value = descriptor_mutations[mutation]
        descriptor[name] = value
    elif mutation == "descriptor-hash":
        command["descriptorSha256"] = "b" * 64
    elif mutation == "logical-launcher":
        command["logicalLauncher"][0] = "pypy"
        command["argvSha256"] = hashlib.sha256(
            json.dumps(command["logicalLauncher"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    elif mutation == "argv-hash":
        command["argvSha256"] = "c" * 64
    else:
        provenance["recipe"] = "student_micro_flow"
    if mutation not in {"descriptor-hash", "argv-hash", "recipe"}:
        command["descriptorSha256"] = hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    execution["command"] = command
    execution["recipe"] = provenance["recipe"]
    execution_path.write_text(json.dumps(execution), encoding="utf-8")
    provenance["execution"]["sha256"] = hashlib.sha256(execution_path.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(artifact), encoding="utf-8")

    if consumer == "assembler":
        with pytest.raises(ValueError, match="execution proof"):
            module.assemble_manifest(
                candidate_root / "release-evidence.json",
                candidate_root=candidate_root,
                release_run=RELEASE_RUN,
                receipt_paths={"teacher_flow": receipt},
            )
    else:
        manifest = candidate_root / "release-evidence.json"
        receipt_body = receipt.read_bytes()
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 3,
                    "candidate": candidate,
                    "releaseRun": RELEASE_RUN,
                    "evidence": {
                        "teacher_flow": {
                            "status": "pass",
                            "detail": "teacher_flow verified by playwright",
                            "artifact": "artifacts/teacher_flow.json",
                            "artifactSha256": hashlib.sha256(receipt_body).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = verifier.verify(
            verifier.FileReleaseRuntime(
                manifest,
                expected_source_head=SOURCE_HEAD,
                candidate_root=candidate_root,
            )
        )
        assert result.layers["teacher_flow"].status == "fail"
        assert "execution proof" in result.layers["teacher_flow"].detail


def test_manifest_assembler_hashes_candidate_bound_receipts(tmp_path: Path) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path, _raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
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


def test_manifest_assembler_rejects_bundle_parent_replacement_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path, _raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    receipt_body = receipt_path.read_bytes()
    output = candidate_root / "release-evidence.json"
    retained_root = tmp_path / "retained-candidate"
    original_validate = module._validated_receipt
    switched = False

    def validate(*args, **kwargs):
        nonlocal switched
        result = original_validate(*args, **kwargs)
        candidate_root.rename(retained_root)
        candidate_root.mkdir()
        (candidate_root / "artifacts").mkdir()
        switched = True
        return result

    monkeypatch.setattr(module, "_validated_receipt", validate)

    with pytest.raises(ValueError, match="boundary|changed"):
        module.assemble_manifest(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"teacher_flow": receipt_path},
        )

    assert switched
    assert not output.exists()
    assert not (retained_root / "release-evidence.json").exists()
    assert (retained_root / "artifacts" / "teacher_flow.json").read_bytes() == receipt_body


def test_manifest_assembler_publishes_canonical_proof_last_without_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path, _raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    output = candidate_root / "release-evidence.json"
    events: list[str] = []
    original_validate = module._validated_receipt
    original_publish = module._publish_no_replace

    def validate(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        events.append("validated")
        return result

    def publish(source: Path, target: Path) -> None:
        assert target == output
        if not target.exists():
            staged_document = json.loads(source.read_bytes())
            assert staged_document["evidence"]["teacher_flow"]["status"] == "pass"
            events.append("commit")
        original_publish(source, target)

    def fsync_directory(path: Path) -> None:
        assert path == output.parent
        events.append(
            "directory-fsync-after-commit" if output.exists() else "directory-fsync-before-commit"
        )

    def forbid_replace(*_args, **_kwargs):
        pytest.fail("manifest publication must not replace its target")

    monkeypatch.setattr(module, "_validated_receipt", validate)
    monkeypatch.setattr(module, "_publish_no_replace", publish)
    monkeypatch.setattr(module, "_fsync_directory", fsync_directory, raising=False)
    monkeypatch.setattr(module.os, "replace", forbid_replace)

    manifest = module.assemble_manifest(
        output,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={"teacher_flow": receipt_path},
    )

    assert events[0] == "validated"
    assert events[-1] == "directory-fsync-after-commit"
    assert events.index("commit") < len(events) - 1
    canonical_body = module._json_bytes(manifest)
    assert output.read_bytes() == canonical_body
    with pytest.raises(FileExistsError):
        module.assemble_manifest(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"teacher_flow": receipt_path},
        )
    assert output.read_bytes() == canonical_body


def test_manifest_assembler_cleanup_failure_does_not_mask_primary_publication_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path, _raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    output = candidate_root / "release-evidence.json"

    class PublicationAbort(BaseException):
        pass

    primary = PublicationAbort("injected manifest publication abort")
    original_unlink = Path.unlink

    def abort_publication(*_args, **_kwargs):
        raise primary

    def fail_staging_cleanup(*_args, **_kwargs):
        raise OSError("injected manifest staging cleanup failure")

    def fail_owned_staging_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        if (
            path.parent == output.parent
            and path.name.startswith(f".{output.name}.")
            and path.name.endswith(".tmp")
        ):
            raise OSError("injected manifest staging cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(module, "_publish_no_replace", abort_publication)
    monkeypatch.setattr(
        module,
        "_remove_manifest_staging",
        fail_staging_cleanup,
        raising=False,
    )
    monkeypatch.setattr(module.os, "replace", abort_publication)
    monkeypatch.setattr(Path, "unlink", fail_owned_staging_cleanup)

    with pytest.raises(PublicationAbort) as caught:
        module.assemble_manifest(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"teacher_flow": receipt_path},
        )

    assert caught.value is primary
    assert any(
        "cleanup" in note and "injected manifest staging cleanup failure" in note
        for note in getattr(primary, "__notes__", ())
    )
    assert not output.exists()


@pytest.mark.parametrize("case", ("candidate", "release-run", "body"))
def test_manifest_assembler_rejects_mismatched_or_tampered_receipts(
    tmp_path: Path,
    case: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path, _raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
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
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path, _raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
    )
    manifest_path = candidate_root / "release-evidence.json"
    manifest_path.write_bytes(b"sentinel")

    def fail_publish(_source: Path, _target: Path) -> None:
        pytest.fail("manifest publisher must not run for an existing target")

    monkeypatch.setattr(module, "_publish_no_replace", fail_publish)

    with pytest.raises(FileExistsError, match="release manifest already exists"):
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
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path, _raw_report, _execution = _write_teacher_probe_receipt(
        module,
        candidate_root=candidate_root,
        candidate=candidate,
        working_directory=tmp_path,
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


def test_source_head_receipt_is_derived_from_clean_matching_git_checkout(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()
    calls: list[tuple[str, ...]] = []

    def run_git(arguments: list[str], *, cwd: Path):
        assert cwd == source_root
        calls.append(tuple(arguments))
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            stdout = SOURCE_HEAD + "\n"
        elif arguments[-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]:
            stdout = ""
        elif arguments[-3:] == ["remote", "get-url", "origin"]:
            stdout = "https://github.com/xinlingzhifei/DeepTutor.git\n"
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    output = tmp_path / "source-head.json"
    receipt = module.write_source_head_receipt(
        output,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        source_root=source_root,
        observed_at="2026-08-25T00:00:00Z",
        git_runner=run_git,
    )

    assert receipt["candidate"] == candidate
    assert receipt["receipt"]["producer"] == "git-probe"
    assert receipt["receipt"]["result"] == {
        "outcome": "pass",
        "nativeExit": 0,
        "checks": {"headMatches": True, "worktreeClean": True},
    }
    assert len(calls) == 4


@pytest.mark.parametrize("case", ("head", "dirty", "origin", "exit"))
def test_source_head_receipt_rejects_untrusted_git_checkout_before_publish(
    tmp_path: Path,
    case: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()

    def run_git(arguments: list[str], *, cwd: Path):
        assert cwd == source_root
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            stdout = ("b" * 40 if case == "head" else SOURCE_HEAD) + "\n"
        elif arguments[-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]:
            stdout = " M changed.py\n" if case == "dirty" else ""
        else:
            stdout = (
                "https://github.com/someone/another-repository.git\n"
                if case == "origin"
                else "https://github.com/xinlingzhifei/DeepTutor.git\n"
            )
        return subprocess.CompletedProcess(
            arguments,
            1 if case == "exit" else 0,
            stdout=stdout,
            stderr="probe failed" if case == "exit" else "",
        )

    output = tmp_path / "source-head.json"
    output.write_bytes(b"sentinel")
    with pytest.raises(ValueError, match="Git"):
        module.write_source_head_receipt(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            source_root=source_root,
            observed_at="2026-08-25T00:00:00Z",
            git_runner=run_git,
        )

    assert output.read_bytes() == b"sentinel"


def test_source_head_receipt_rejects_candidate_change_during_probe_before_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    changed_candidate = json.loads(json.dumps(candidate))
    changed_candidate["sourceHead"] = "b" * 40
    changed_candidate["releaseTag"] = (
        f"yfeistai-first-release-20260825-{changed_candidate['sourceHead'][:8]}"
    )
    candidates = iter((candidate, changed_candidate))
    monkeypatch.setattr(module, "_candidate", lambda _root: next(candidates))
    source_root = tmp_path / "source"
    source_root.mkdir()

    def run_git(arguments: list[str], *, cwd: Path):
        assert cwd == source_root
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            stdout = SOURCE_HEAD + "\n"
        elif arguments[-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]:
            stdout = ""
        else:
            stdout = "https://github.com/xinlingzhifei/DeepTutor.git\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    output = tmp_path / "source-head.json"
    with pytest.raises(ValueError, match="candidate changed"):
        module.write_source_head_receipt(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            source_root=source_root,
            observed_at="2026-08-25T00:00:00Z",
            git_runner=run_git,
        )

    assert not output.exists()


def test_source_head_receipt_rejects_git_head_change_during_probe_before_publish(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()
    head_reads = 0

    def run_git(arguments: list[str], *, cwd: Path):
        nonlocal head_reads
        assert cwd == source_root
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            head_reads += 1
            stdout = (SOURCE_HEAD if head_reads == 1 else "b" * 40) + "\n"
        elif arguments[-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]:
            stdout = ""
        else:
            stdout = "https://github.com/xinlingzhifei/DeepTutor.git\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    output = tmp_path / "source-head.json"
    with pytest.raises(ValueError, match="Git HEAD changed"):
        module.write_source_head_receipt(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            source_root=source_root,
            observed_at="2026-08-25T00:00:00Z",
            git_runner=run_git,
        )

    assert not output.exists()


def test_image_digest_receipt_is_derived_from_candidate_lock_and_compose(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    output = tmp_path / "image-digests.json"

    receipt = module.write_image_digest_receipt(
        output,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        observed_at="2026-08-25T00:00:00Z",
    )

    assert receipt["candidate"] == candidate
    assert receipt["receipt"]["producer"] == "image-lock"
    assert receipt["receipt"]["result"] == {
        "outcome": "pass",
        "nativeExit": 0,
        "checks": {"lockMatches": True, "composeMatches": True},
    }


def test_image_digest_receipt_rejects_compose_drift_before_publish(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    compose_path = candidate_root / "docker-compose.platform.yml"
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8").replace(
            "ghcr.io/xinlingzhifei/deeptutor:",
            "ghcr.io/xinlingzhifei/deeptutor-drifted:",
            1,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "image-digests.json"
    output.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="Compose"):
        module.write_image_digest_receipt(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            observed_at="2026-08-25T00:00:00Z",
        )

    assert output.read_bytes() == b"sentinel"


def test_running_containers_receipt_is_derived_from_fixed_runtime_attestation(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    output = candidate_root / "artifacts" / "running_containers.json"
    attestation = candidate_root / "runtime" / "runtime-attestation.json"

    receipt = module.write_running_containers_receipt(
        output,
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
    )

    assert receipt["candidate"] == candidate
    assert receipt["receipt"] == {
        "producer": "docker-compose",
        "observedAt": "2026-08-25T00:00:00Z",
        "result": {
            "outcome": "pass",
            "nativeExit": 0,
            "checks": {"stableContainerSet": True},
        },
    }
    assert receipt["provenance"] == {
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": hashlib.sha256(attestation.read_bytes()).hexdigest(),
        }
    }

    manifest = candidate_root / "release-evidence.json"
    module.assemble_manifest(
        manifest,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={"running_containers": output},
    )
    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )

    assert result.layers["running_containers"].status == "pass"


def test_running_containers_receipt_rejects_invalid_attestation_before_publish(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    document = json.loads(attestation.read_text(encoding="utf-8"))
    document["afterSnapshot"] = []
    attestation.write_text(json.dumps(document), encoding="utf-8")
    output = candidate_root / "artifacts" / "running_containers.json"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="attestation"):
        module.write_running_containers_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
        )

    assert output.read_bytes() == b"sentinel"


@pytest.mark.parametrize("case", ("attestation", "candidate"))
def test_running_containers_receipt_rejects_publish_window_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    output = candidate_root / "artifacts" / "running_containers.json"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"sentinel")
    original_validate = module.validate_runtime_attestation
    drifted = False

    def validate_and_drift(*args, **kwargs):
        nonlocal drifted
        result = original_validate(*args, **kwargs)
        if not drifted:
            drifted = True
            if case == "attestation":
                attestation.write_bytes(attestation.read_bytes() + b"\n")
            else:
                lock_path = candidate_root / "deploy" / "image-lock.json"
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                lock["candidate"]["sourceHead"] = "b" * 40
                lock_path.write_text(json.dumps(lock), encoding="utf-8")
        return result

    monkeypatch.setattr(module, "validate_runtime_attestation", validate_and_drift)
    with pytest.raises(ValueError, match="changed"):
        module.write_running_containers_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
        )

    assert output.read_bytes() == b"sentinel"


@pytest.mark.parametrize(
    "relative_output",
    (
        "deploy/image-lock.json",
        "docker-compose.platform.yml",
        "release-evidence.json",
    ),
)
def test_running_containers_receipt_rejects_noncanonical_output_path(
    tmp_path: Path,
    relative_output: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    output = candidate_root / relative_output
    if not output.exists():
        output.write_bytes(b"sentinel")
    original = output.read_bytes()

    with pytest.raises(ValueError, match="canonical"):
        module.write_running_containers_receipt(
            output,
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
        )

    assert output.read_bytes() == original


@pytest.mark.parametrize("consumer", ("assembler", "file-runtime"))
def test_consumers_reject_running_containers_without_attestation_provenance(
    tmp_path: Path,
    consumer: str,
) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path = candidate_root / "artifacts" / "running_containers.json"
    module._write_pass_receipt_from_candidate(
        receipt_path,
        candidate=candidate,
        release_run=RELEASE_RUN,
        evidence="running_containers",
        observed_at="2026-08-25T00:00:00Z",
        native_exit=0,
        checks={"stableContainerSet": True},
    )
    manifest = candidate_root / "release-evidence.json"

    if consumer == "assembler":
        with pytest.raises(ValueError, match="runtime attestation proof"):
            module.assemble_manifest(
                manifest,
                candidate_root=candidate_root,
                release_run=RELEASE_RUN,
                receipt_paths={"running_containers": receipt_path},
            )
        return

    receipt_body = receipt_path.read_bytes()
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 3,
                "candidate": candidate,
                "releaseRun": RELEASE_RUN,
                "evidence": {
                    "running_containers": {
                        "status": "pass",
                        "detail": "running_containers verified by docker-compose",
                        "artifact": "artifacts/running_containers.json",
                        "artifactSha256": hashlib.sha256(receipt_body).hexdigest(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )

    assert result.layers["running_containers"].status == "fail"
    assert "runtime attestation proof" in result.layers["running_containers"].detail


@pytest.mark.parametrize("consumer", ("assembler", "file-runtime"))
def test_consumers_revalidate_running_containers_attestation(
    tmp_path: Path,
    consumer: str,
) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path = candidate_root / "artifacts" / "running_containers.json"
    module.write_running_containers_receipt(
        receipt_path,
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
    )
    manifest = candidate_root / "release-evidence.json"
    if consumer == "file-runtime":
        receipt_body = receipt_path.read_bytes()
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 3,
                    "candidate": candidate,
                    "releaseRun": RELEASE_RUN,
                    "evidence": {
                        "running_containers": {
                            "status": "pass",
                            "detail": "running_containers verified by docker-compose",
                            "artifact": "artifacts/running_containers.json",
                            "artifactSha256": hashlib.sha256(receipt_body).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
    attestation = candidate_root / "runtime" / "runtime-attestation.json"
    attestation.write_bytes(attestation.read_bytes() + b"\n")

    if consumer == "assembler":
        with pytest.raises(ValueError, match="attestation.*digest"):
            module.assemble_manifest(
                manifest,
                candidate_root=candidate_root,
                release_run=RELEASE_RUN,
                receipt_paths={"running_containers": receipt_path},
            )
        return

    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )
    assert result.layers["running_containers"].status == "fail"
    assert "attestation" in result.layers["running_containers"].detail


def test_release_evidence_cli_derives_running_containers_receipt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_evidence_module()
    output = tmp_path / "bundle" / "artifacts" / "running_containers.json"
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def write_receipt(path: Path, **arguments: object) -> dict[str, object]:
        captured["path"] = path
        captured.update(arguments)
        return {}

    monkeypatch.setattr(module, "write_running_containers_receipt", write_receipt)
    assert (
        module.main(
            [
                "running-containers",
                "--output",
                str(output),
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
            ]
        )
        == 0
    )
    assert captured == {
        "path": output,
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
    }
    assert capsys.readouterr().out == f"{output}\n"


def test_platform_preflight_receipts_are_derived_from_two_fixed_container_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    calls: list[dict[str, object]] = []
    docker_configs: list[Path] = []
    replayed_proofs: list[bytes] = []
    trusted_docker = (tmp_path / "trusted" / "docker.exe").resolve()
    resolver_calls = 0
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("COMPOSE_ANSI", "never")
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "must-not-reach-docker")

    def runner(arguments, *, cwd, env, timeout):
        phase = arguments[arguments.index("--runtime-phase") + 1]
        service = "tenant-provisioner" if phase == "database-object-store" else "deeptutor"
        checks = (
            {
                "activeTenantCredentialsValid": True,
                "databaseConnected": True,
                "objectStoreRoundTrip": True,
                "revisionsMatch": True,
                "tenantCrossPrefixDenied": True,
                "tenantOwnPrefixAccessible": True,
            }
            if phase == "database-object-store"
            else {"openmaicContractCompatible": True}
        )
        report = {
            "schemaVersion": 1,
            "producer": "platform-preflight",
            "phase": phase,
            "checks": checks,
            "errors": [],
        }
        stdout = (
            json.dumps(
                report,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        docker_config = Path(arguments[2])
        docker_configs.append(docker_config)
        calls.append(
            {
                "arguments": arguments,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
                "phase": phase,
                "service": service,
            }
        )
        return subprocess.CompletedProcess(arguments, 0, stdout, "")

    def docker_resolver() -> Path:
        nonlocal resolver_calls
        resolver_calls += 1
        return trusted_docker

    def derive_receipt_checks(body: bytes, **_arguments):
        replayed_proofs.append(body)
        return (
            {
                "database_revisions": {"revisionsMatch": True},
                "service_health": {"allServicesHealthy": True},
            },
            "2026-08-25T00:01:00Z",
        )

    monkeypatch.setattr(
        module,
        "_current_observed_at",
        lambda: "2026-08-25T00:01:00Z",
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "derive_platform_preflight_receipt_checks",
        derive_receipt_checks,
        raising=False,
    )
    receipts = module.write_platform_preflight_receipts(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=120,
        runner=runner,
        docker_resolver=docker_resolver,
    )

    assert set(receipts) == {"database_revisions", "service_health"}
    assert resolver_calls == 1
    assert [call["phase"] for call in calls] == ["database-object-store", "openmaic"]
    assert all(call["cwd"] == candidate_root.resolve() for call in calls)
    assert all(call["timeout"] == 120 for call in calls)
    assert all(
        not any(name.startswith(("DOCKER_", "COMPOSE_")) for name in call["env"]) for call in calls
    )
    assert all("YFEISTAI_LIVE_FIXTURE_TOKEN" not in call["env"] for call in calls)
    assert len(set(docker_configs)) == 1
    assert not docker_configs[0].exists()

    proof_path = candidate_root / "runtime" / "platform-preflight-attestation.json"
    assert replayed_proofs == [proof_path.read_bytes()]
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["candidate"] == candidate
    assert proof["releaseRun"] == RELEASE_RUN
    assert proof["observedAt"] == "2026-08-25T00:01:00Z"
    assert [execution["service"] for execution in proof["executions"]] == [
        "tenant-provisioner",
        "deeptutor",
    ]
    assert [execution["containerId"] for execution in proof["executions"]] == [
        "container-tenant-provisioner",
        "container-deeptutor",
    ]
    for call, execution in zip(calls, proof["executions"], strict=True):
        actual_command = call["arguments"]
        logical_command = execution["command"]
        assert actual_command[0] == str(trusted_docker)
        assert logical_command[0] == "docker"
        assert actual_command[1] == logical_command[1]
        assert actual_command[2] == str(docker_configs[0])
        assert actual_command[3:] == logical_command[3:]
        assert execution["command"][2] == "<isolated-docker-config>"
        assert execution["nativeExit"] == 0
        assert hashlib.sha256(execution["stdout"].encode()).hexdigest() == execution["stdoutSha256"]

    manifest = candidate_root / "release-evidence.json"
    module.assemble_manifest(
        manifest,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={
            evidence: candidate_root / "artifacts" / f"{evidence}.json" for evidence in receipts
        },
    )
    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )
    assert result.layers["database_revisions"].status == "pass"
    assert result.layers["service_health"].status == "pass"


def test_capacity_profile_receipt_is_derived_from_fixed_live_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    calls: list[dict[str, object]] = []
    token = "capacity-token-must-not-be-serialized"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token)
    monkeypatch.setenv("COMPOSE_FILE", "attacker-compose.yml")

    def runner(arguments, *, cwd, env, timeout):
        report = _live_capacity_report(candidate)
        stdout = (
            json.dumps(
                report,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        calls.append(
            {
                "arguments": arguments,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(arguments, 0, stdout, "")

    receipt = module.write_capacity_profile_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=600,
        runner=runner,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["cwd"] == candidate_root.resolve()
    assert call["timeout"] == 600
    assert call["arguments"][-2:] == ["--profile", "first-release"]
    assert call["env"]["YFEISTAI_LIVE_FIXTURE_TOKEN"] == token
    assert "COMPOSE_FILE" not in call["env"]
    proof_path = candidate_root / "runtime" / "capacity-profile-attestation.json"
    receipt_path = candidate_root / "artifacts" / "capacity_profile.json"
    idempotency_path = candidate_root / "artifacts" / "learning_event_idempotency.json"
    assert receipt == json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["receipt"]["producer"] == "classroom-capacity-probe"
    assert token not in proof_path.read_text(encoding="utf-8")
    assert token not in receipt_path.read_text(encoding="utf-8")
    assert token not in idempotency_path.read_text(encoding="utf-8")
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["summary"]["checks"] == {
        "thresholdsPassed": True,
        "rawSamplesRecorded": True,
        "resourceObservationsRecorded": True,
        "resourceAccountingComplete": True,
        "resourceBoundaryStable": True,
    }
    assert proof["execution"]["command"] == module.capacity_profile_command_record()
    proof_sha256 = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    idempotency = json.loads(idempotency_path.read_text(encoding="utf-8"))
    assert idempotency["receipt"] == {
        "producer": "classroom-capacity-probe",
        "observedAt": "2026-08-25T00:01:00Z",
        "result": {
            "outcome": "pass",
            "nativeExit": 0,
            "checks": {
                "duplicateCountedOnce": True,
                "ticketReplayRejected": True,
                "projectionVisible": True,
            },
        },
    }
    expected_provenance = {
        "capacityAttestation": {
            "artifact": "runtime/capacity-profile-attestation.json",
            "sha256": proof_sha256,
        }
    }
    assert receipt["provenance"] == expected_provenance
    assert idempotency["provenance"] == expected_provenance

    manifest = candidate_root / "release-evidence.json"
    module.assemble_manifest(
        manifest,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={
            "capacity_profile": receipt_path,
            "learning_event_idempotency": idempotency_path,
        },
    )
    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )
    assert result.layers["capacity_profile"].status == "pass"
    assert result.layers["learning_event_idempotency"].status == "pass"


def _live_capacity_report(candidate: dict[str, object]) -> dict[str, object]:
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
            "observedAt": f"2026-08-25T00:00:0{sequence}Z",
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
                "occurred_at": "2026-08-25T00:00:00Z",
            },
            {
                "schema_version": "1.0",
                "event_id": event_ids[1],
                "event_type": "quiz.graded",
                "occurred_at": "2026-08-25T00:00:00Z",
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
                "occurred_at": "2026-08-25T00:00:00Z",
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
        "releaseRun": RELEASE_RUN,
        "observedAt": "2026-08-25T00:01:00Z",
        "baseUrl": BASE_URL,
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


def _write_capacity_dependency(
    module,
    candidate_root: Path,
    candidate: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "classroom-release-token")

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(
                _live_capacity_report(candidate),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            "",
        )

    module.write_capacity_profile_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=600,
        runner=runner,
    )


def _tenant_isolation_report(
    candidate: dict[str, object],
    *,
    capacity_sha256: str,
    tenant_ids: tuple[str, str] = ("tenant-00", "tenant-01"),
) -> tuple[object, dict[str, object]]:
    support = _load_tenant_isolation_support()
    report = json.loads(json.dumps(support._report()))
    original_owner, original_foreign = support.CAPACITY_TENANT_IDS
    owner_tenant_id, foreign_tenant_id = tenant_ids
    report.update(
        candidate=candidate,
        releaseRun=RELEASE_RUN,
        baseUrl=BASE_URL,
        capacityProof={
            "reportSha256": capacity_sha256,
            "tenantIds": list(tenant_ids),
        },
    )
    for principal in report["principals"]:
        principal["tenantId"] = (
            owner_tenant_id if principal["tenantId"] == original_owner else foreign_tenant_id
        )
    report["crossTenantPrincipal"]["tenantId"] = foreign_tenant_id
    for observation in report["observations"]:
        observation["target"]["ownerTenantId"] = owner_tenant_id
        for operation in observation["operations"]:
            operation["tenantId"] = (
                owner_tenant_id if operation["tenantId"] == original_owner else foreign_tenant_id
            )
    return support, report


def _classroom_export_runner(
    module,
    candidate: dict[str, object],
    calls: list[dict[str, object]],
):
    support = _load_classroom_export_support()

    def runner(arguments, *, cwd, env, timeout):
        staging = Path(env["YFEISTAI_CLASSROOM_EXPORT_STAGING_DIR"])
        assert staging.is_dir()
        assert not list(staging.iterdir())
        assert env["YFEISTAI_ACCEPTANCE_TENANT_ID"] == "tenant-00"
        support._write_valid_artifacts(staging)
        report = support._report(staging)
        report.update(
            candidate=candidate,
            releaseRun=RELEASE_RUN,
            baseUrl=BASE_URL,
            tenantId="tenant-00",
        )
        calls.append(
            {
                "arguments": arguments,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            support._module().canonical_classroom_export_report(report).decode("utf-8"),
            "",
        )

    return runner


@pytest.mark.skipif(os.name != "nt", reason="Windows relative-handle contract")
def test_classroom_raw_leases_open_relative_to_retained_staging_handle_for_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    support = _load_classroom_export_support()
    assert "boundary" in inspect.signature(module._open_classroom_artifact_leases).parameters

    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    boundary = module._ClassroomPublicationBoundary.open(bundle_root, "a" * 32)
    leases = {}
    try:
        assert boundary.staging is not None
        support._write_valid_artifacts(boundary.staging)
        staging_handle = boundary.leases["staging/attempt"].handle
        calls: list[tuple[object, str, int, bool]] = []
        real_open = module._open_windows_regular_file_relative

        def record_relative_open(
            directory_handle,
            name: str,
            *,
            share_access: int,
            deletable: bool = False,
        ):
            calls.append((directory_handle, name, share_access, deletable))
            return real_open(
                directory_handle,
                name,
                share_access=share_access,
                deletable=deletable,
            )

        monkeypatch.setattr(
            module,
            "_open_windows_regular_file_relative",
            record_relative_open,
        )
        leases = module._open_classroom_artifact_leases(boundary)

        assert {name for _parent, name, _share, _delete in calls} == set(
            module.CLASSROOM_EXPORT_PATHS.values()
        )
        assert all(parent is staging_handle for parent, _name, _share, _delete in calls)
        assert all(share == 0x00000007 for _parent, _name, share, _delete in calls)
        assert all(deletable is True for _parent, _name, _share, deletable in calls)

        for kind, lease in leases.items():
            target = boundary.raw_root / module.CLASSROOM_EXPORT_PATHS[kind]
            module._publish_classroom_no_replace(
                boundary,
                lease.path,
                target,
                source_handle=lease.handle,
            )
            assert (
                os.stat(target, follow_symlinks=False).st_ino
                == os.fstat(lease.handle.fileno()).st_ino
            )
    finally:
        for lease in leases.values():
            lease.handle.close()
        boundary.close()


def test_classroom_exports_receipt_publishes_bound_raw_artifacts_and_proof_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    calls: list[dict[str, object]] = []
    publications: list[str] = []
    real_publish = module._publish_classroom_no_replace

    def record_publication(boundary, source: Path, target: Path, *, source_handle) -> None:
        real_publish(boundary, source, target, source_handle=source_handle)
        publications.append(target.relative_to(candidate_root).as_posix())

    monkeypatch.setattr(module, "_publish_classroom_no_replace", record_publication)
    receipt = module.write_classroom_exports_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=600,
        runner=_classroom_export_runner(module, candidate, calls),
    )

    proof_path = candidate_root / "runtime" / "classroom-exports-attestation.json"
    receipt_path = candidate_root / "artifacts" / "classroom_exports.json"
    raw_root = candidate_root / "raw" / "classroom-exports"
    assert publications[-1] == "runtime/classroom-exports-attestation.json"
    assert set(publications[:-2]) == {
        "raw/classroom-exports/classroom.zip",
        "raw/classroom-exports/classroom.pptx",
        "raw/classroom-exports/classroom.html",
        "raw/classroom-exports/classroom.mp4",
    }
    assert publications[-2] == "artifacts/classroom_exports.json"
    assert {path.name for path in raw_root.iterdir()} == {
        "classroom.zip",
        "classroom.pptx",
        "classroom.html",
        "classroom.mp4",
    }
    assert receipt == json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["receipt"]["producer"] == "classroom-export-probe"
    proof_sha256 = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    assert receipt["provenance"] == {
        "classroomExportsAttestation": {
            "artifact": "runtime/classroom-exports-attestation.json",
            "sha256": proof_sha256,
        }
    }
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["tenantId"] == "tenant-00"
    assert proof["execution"]["command"] == module.classroom_exports_command_record()
    assert proof["capacityAttestation"]["artifact"] == ("runtime/capacity-profile-attestation.json")
    assert set(proof["rawArtifacts"]) == set(module.CLASSROOM_EXPORT_PATHS)
    assert len(calls) == 1
    assert calls[0]["arguments"][-2:] == ["--profile", "first-release"]
    assert calls[0]["cwd"] == candidate_root.resolve()
    assert not list((candidate_root / "staging").iterdir())

    manifest = candidate_root / "release-evidence.json"
    module.assemble_manifest(
        manifest,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={"classroom_exports": receipt_path},
    )
    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )
    assert result.layers["classroom_exports"].status == "pass"


def test_classroom_exports_publication_failure_leaves_no_proof_or_partial_raw_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    proof_path = candidate_root / "runtime" / "classroom-exports-attestation.json"
    receipt_path = candidate_root / "artifacts" / "classroom_exports.json"
    raw_root = candidate_root / "raw" / "classroom-exports"
    real_publish = module._publish_classroom_no_replace

    def fail_proof(boundary, source: Path, target: Path, *, source_handle) -> None:
        if target == proof_path:
            raise OSError("simulated classroom proof publication failure")
        real_publish(boundary, source, target, source_handle=source_handle)

    monkeypatch.setattr(module, "_publish_classroom_no_replace", fail_proof)
    with pytest.raises(OSError, match="simulated classroom proof publication failure"):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=_classroom_export_runner(module, candidate, []),
        )

    assert not proof_path.exists()
    assert not receipt_path.exists()
    assert not raw_root.exists() or not list(raw_root.iterdir())
    assert not list(candidate_root.rglob("*.staging"))
    failure_directories = list((candidate_root / "failures" / "classroom-exports").glob("*"))
    assert len(failure_directories) == 1
    assert (failure_directories[0] / "failure.json").is_file()


def test_classroom_exports_discards_secret_bearing_raw_artifact_instead_of_archiving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    token = "classroom-token-must-never-be-archived"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token)
    base_runner = _classroom_export_runner(module, candidate, [])

    def runner(arguments, *, cwd, env, timeout):
        completed = base_runner(arguments, cwd=cwd, env=env, timeout=timeout)
        artifact = Path(env["YFEISTAI_CLASSROOM_EXPORT_STAGING_DIR"]) / "classroom.html"
        artifact.write_bytes(artifact.read_bytes() + token.encode("utf-8"))
        return completed

    with pytest.raises(ValueError, match="raw artifact contains a live fixture token"):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    failure_directories = list((candidate_root / "failures" / "classroom-exports").glob("*"))
    assert len(failure_directories) == 1
    assert {path.name for path in failure_directories[0].iterdir()} == {"failure.json"}
    assert not any(
        token.encode("utf-8") in path.read_bytes()
        for path in candidate_root.rglob("*")
        if path.is_file()
    )
    assert not any(os.path.lexists(path) for path in _classroom_formal_paths(candidate_root))
    assert not list((candidate_root / "staging").iterdir())


@pytest.mark.parametrize(
    ("kind", "artifact_name", "member_name", "token"),
    (
        ("classroom_zip", "classroom.zip", "media/voice.mp3", b"first-release-audio"),
        ("pptx", "classroom.pptx", "ppt/slides/slide1.xml", b"Verified classroom export"),
    ),
    ids=("classroom_zip", "pptx"),
)
def test_classroom_exports_discards_secret_bearing_deflated_member_instead_of_archiving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    artifact_name: str,
    member_name: str,
    token: bytes,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token.decode("utf-8"))
    base_runner = _classroom_export_runner(module, candidate, [])

    def runner(arguments, *, cwd, env, timeout):
        completed = base_runner(arguments, cwd=cwd, env=env, timeout=timeout)
        staging = Path(env["YFEISTAI_CLASSROOM_EXPORT_STAGING_DIR"])
        artifact = staging / artifact_name
        with zipfile.ZipFile(artifact) as archive:
            info = archive.getinfo(member_name)
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert token in archive.read(info)
        assert token not in artifact.read_bytes()
        assert all(
            token not in (staging / name).read_bytes()
            for other_kind, name in module.CLASSROOM_EXPORT_PATHS.items()
            if other_kind != kind
        )
        return completed

    with pytest.raises(ValueError, match="raw artifact contains a live fixture token"):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    failure_directories = list((candidate_root / "failures" / "classroom-exports").glob("*"))
    assert len(failure_directories) == 1
    assert {path.name for path in failure_directories[0].iterdir()} == {"failure.json"}
    assert not any(os.path.lexists(path) for path in _classroom_formal_paths(candidate_root))
    assert not list((candidate_root / "staging").iterdir())


def _classroom_formal_paths(candidate_root: Path) -> tuple[Path, ...]:
    return (
        candidate_root / "runtime" / "classroom-exports-attestation.json",
        candidate_root / "artifacts" / "classroom_exports.json",
        *(
            candidate_root / "raw" / "classroom-exports" / name
            for name in ("classroom.zip", "classroom.pptx", "classroom.html", "classroom.mp4")
        ),
    )


def _directory_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("parent_name", ("runtime", "artifacts", "raw", "staging"))
def test_classroom_exports_rejects_redirected_bundle_parent_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_name: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    parent = candidate_root / parent_name
    redirected = tmp_path / f"redirected-{parent_name}"
    if parent.exists():
        os.replace(parent, redirected)
    else:
        redirected.mkdir()
    before = _directory_snapshot(redirected)
    try:
        parent.symlink_to(redirected, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this test host")
    before_children = {path.name for path in candidate_root.iterdir()}

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("redirected publication parents must stop before probe execution")

    with pytest.raises(ValueError, match="boundary|symlink|junction|reparse|directory"):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=forbidden_runner,
        )

    assert parent.is_symlink()
    assert _directory_snapshot(redirected) == before
    assert {path.name for path in candidate_root.iterdir()} == before_children
    assert not any(os.path.lexists(path) for path in _classroom_formal_paths(candidate_root))


def test_classroom_exports_never_publishes_through_a_redirected_target_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    raw_root = candidate_root / "raw" / "classroom-exports"
    retained_raw_root = candidate_root / "raw" / "classroom-exports-retained"
    redirected = tmp_path / "redirected-publication"
    redirected.mkdir()
    real_publish = module._publish_classroom_no_replace
    redirected_once = False

    def redirect_before_publication(
        boundary,
        source: Path,
        target: Path,
        *,
        source_handle,
    ) -> None:
        nonlocal redirected_once
        if not redirected_once and target.parent == raw_root:
            os.replace(raw_root, retained_raw_root)
            try:
                raw_root.symlink_to(redirected, target_is_directory=True)
            except OSError:
                os.replace(retained_raw_root, raw_root)
                pytest.skip("directory symlinks are unavailable on this test host")
            redirected_once = True
        real_publish(
            boundary,
            source,
            target,
            source_handle=source_handle,
        )

    monkeypatch.setattr(module, "_publish_classroom_no_replace", redirect_before_publication)
    with pytest.raises(ValueError, match="publication directory changed"):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=_classroom_export_runner(module, candidate, []),
        )

    assert redirected_once is True
    assert not list(redirected.iterdir())
    assert not list(retained_raw_root.iterdir())
    assert not any(os.path.lexists(path) for path in _classroom_formal_paths(candidate_root))


@pytest.mark.parametrize("drift", ("raw", "receipt"))
def test_classroom_exports_retracts_proof_when_evidence_drifts_after_proof_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    proof_path = candidate_root / "runtime" / "classroom-exports-attestation.json"
    drift_path = (
        candidate_root / "raw" / "classroom-exports" / "classroom.html"
        if drift == "raw"
        else candidate_root / "artifacts" / "classroom_exports.json"
    )
    real_publish = module._publish_classroom_no_replace

    def drift_after_proof(boundary, source: Path, target: Path, *, source_handle) -> None:
        real_publish(boundary, source, target, source_handle=source_handle)
        if target == proof_path:
            replacement = candidate_root / f"tampered-{drift}.replacement"
            replacement.write_bytes(b"tampered after proof publication")
            os.replace(replacement, drift_path)

    monkeypatch.setattr(module, "_publish_classroom_no_replace", drift_after_proof)
    with pytest.raises(ValueError, match="published .* changed"):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=_classroom_export_runner(module, candidate, []),
        )

    assert drift_path.read_bytes() == b"tampered after proof publication"
    assert not any(
        os.path.lexists(path)
        for path in _classroom_formal_paths(candidate_root)
        if path != drift_path
    )


def test_classroom_exports_preserves_competing_publication_when_no_replace_loses_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    competing_path = candidate_root / "raw" / "classroom-exports" / "classroom.zip"
    competing_body = b"competing writer evidence"
    real_publish = module._publish_classroom_no_replace

    def lose_race(boundary, source: Path, target: Path, *, source_handle) -> None:
        if target == competing_path:
            target.write_bytes(competing_body)
        real_publish(boundary, source, target, source_handle=source_handle)

    monkeypatch.setattr(module, "_publish_classroom_no_replace", lose_race)
    with pytest.raises(OSError):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=_classroom_export_runner(module, candidate, []),
        )

    assert competing_path.read_bytes() == competing_body
    assert not any(
        os.path.lexists(path)
        for path in _classroom_formal_paths(candidate_root)
        if path != competing_path
    )


def test_classroom_exports_retracts_formal_links_after_process_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    proof_path = candidate_root / "runtime" / "classroom-exports-attestation.json"
    real_publish = module._publish_classroom_no_replace

    def interrupt_after_proof(boundary, source: Path, target: Path, *, source_handle) -> None:
        real_publish(boundary, source, target, source_handle=source_handle)
        if target == proof_path:
            raise KeyboardInterrupt

    def forbidden_archive(**_arguments: object) -> Path:
        pytest.fail("process interrupts must retract without publishing a failure archive")

    monkeypatch.setattr(module, "_publish_classroom_no_replace", interrupt_after_proof)
    monkeypatch.setattr(module, "_record_probe_failure", forbidden_archive)
    with pytest.raises(KeyboardInterrupt):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=_classroom_export_runner(module, candidate, []),
        )

    assert not any(os.path.lexists(path) for path in _classroom_formal_paths(candidate_root))


def test_classroom_exports_retracts_formal_links_before_failure_archive_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    proof_path = candidate_root / "runtime" / "classroom-exports-attestation.json"
    real_publish = module._publish_classroom_no_replace

    def fail_after_proof(boundary, source: Path, target: Path, *, source_handle) -> None:
        real_publish(boundary, source, target, source_handle=source_handle)
        if target == proof_path:
            raise OSError("failure after proof publication")

    def interrupt_archive(**_arguments: object) -> Path:
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_publish_classroom_no_replace", fail_after_proof)
    monkeypatch.setattr(module, "_record_probe_failure", interrupt_archive)
    with pytest.raises(KeyboardInterrupt):
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=_classroom_export_runner(module, candidate, []),
        )

    assert not any(os.path.lexists(path) for path in _classroom_formal_paths(candidate_root))
    assert not list((candidate_root / "staging").iterdir())


def test_classroom_exports_archive_failure_preserves_primary_error_and_retracts_formal_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    proof_path = candidate_root / "runtime" / "classroom-exports-attestation.json"
    real_publish = module._publish_classroom_no_replace

    def fail_proof(boundary, source: Path, target: Path, *, source_handle) -> None:
        if target == proof_path:
            raise OSError("primary proof publication failure")
        real_publish(boundary, source, target, source_handle=source_handle)

    def fail_archive(**_arguments: object) -> Path:
        raise RuntimeError("failure archive unavailable")

    monkeypatch.setattr(module, "_publish_classroom_no_replace", fail_proof)
    monkeypatch.setattr(module, "_record_probe_failure", fail_archive)
    with pytest.raises(OSError, match="primary proof publication failure") as failure:
        module.write_classroom_exports_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=_classroom_export_runner(module, candidate, []),
        )

    assert isinstance(failure.value.__cause__, RuntimeError)
    assert "failure archive unavailable" in str(failure.value.__cause__)
    assert not any(os.path.lexists(path) for path in _classroom_formal_paths(candidate_root))


def test_release_evidence_cli_runs_fixed_classroom_exports_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_evidence_module()
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def write_receipt(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {}

    monkeypatch.setattr(module, "write_classroom_exports_receipt", write_receipt, raising=False)
    assert (
        module.main(
            [
                "classroom-exports",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--timeout-seconds",
                "600",
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "timeout_seconds": 600,
    }
    assert capsys.readouterr().out == (
        f"{bundle_root / 'runtime' / 'classroom-exports-attestation.json'}\n"
    )


def test_tenant_isolation_receipt_replays_capacity_pair_and_publishes_proof_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    capacity_path = candidate_root / "runtime" / "capacity-profile-attestation.json"
    capacity_sha256 = hashlib.sha256(capacity_path.read_bytes()).hexdigest()
    token = "tenant-isolation-token-must-not-be-serialized"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token)
    monkeypatch.setenv("COMPOSE_FILE", "attacker-compose.yml")
    calls: list[dict[str, object]] = []
    publications: list[str] = []
    real_publish = module._publish_classroom_no_replace

    def runner(arguments, *, cwd, env, timeout):
        support, report = _tenant_isolation_report(
            candidate,
            capacity_sha256=capacity_sha256,
        )
        calls.append(
            {
                "arguments": arguments,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            support._module().canonical_tenant_isolation_report(report).decode("utf-8"),
            "",
        )

    def record_publication(boundary, source: Path, target: Path, *, source_handle) -> None:
        real_publish(boundary, source, target, source_handle=source_handle)
        publications.append(target.relative_to(candidate_root).as_posix())

    monkeypatch.setattr(module, "_publish_classroom_no_replace", record_publication)
    receipt = module.write_tenant_isolation_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=600,
        runner=runner,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["cwd"] == candidate_root.resolve()
    assert call["timeout"] == 600
    assert call["arguments"][-2:] == ["--profile", "first-release"]
    assert call["env"]["YFEISTAI_LIVE_FIXTURE_TOKEN"] == token
    assert call["env"]["YFEISTAI_CANDIDATE_ROOT"] == str(candidate_root.resolve())
    assert call["env"]["YFEISTAI_RELEASE_RUN_ID"] == RELEASE_RUN["runId"]
    assert call["env"]["YFEISTAI_ENVIRONMENT_ID"] == RELEASE_RUN["environmentId"]
    assert call["env"]["YFEISTAI_TENANT_ISOLATION_TIMEOUT_SECONDS"] == "570"
    assert call["env"]["YFEISTAI_CAPACITY_ATTESTATION_PATH"] == str(capacity_path)
    assert call["env"]["YFEISTAI_CAPACITY_ATTESTATION_SHA256"] == capacity_sha256
    assert json.loads(call["env"]["YFEISTAI_CAPACITY_TENANT_IDS"]) == [
        "tenant-00",
        "tenant-01",
    ]
    assert call["env"]["WEB_BASE_URL"] == BASE_URL
    assert "COMPOSE_FILE" not in call["env"]

    receipt_path = candidate_root / "artifacts" / "tenant_isolation.json"
    proof_path = candidate_root / "runtime" / "tenant-isolation-attestation.json"
    assert publications == [
        "artifacts/tenant_isolation.json",
        "runtime/tenant-isolation-attestation.json",
    ]
    assert receipt == json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["evidence"] == "tenant_isolation"
    assert receipt["receipt"]["producer"] == "tenant-isolation-probe"
    assert receipt["receipt"]["result"]["checks"] == {
        "databaseIsolated": True,
        "objectsIsolated": True,
        "exportsIsolated": True,
        "eventsIsolated": True,
    }
    proof_sha256 = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    assert receipt["provenance"] == {
        "tenantIsolationAttestation": {
            "artifact": "runtime/tenant-isolation-attestation.json",
            "sha256": proof_sha256,
        }
    }
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["capacityAttestation"] == {
        "artifact": "runtime/capacity-profile-attestation.json",
        "sha256": capacity_sha256,
    }
    assert proof["execution"]["command"] == module.tenant_isolation_command_record()
    assert proof["summary"]["checks"] == receipt["receipt"]["result"]["checks"]
    assert json.loads(proof["execution"]["stdout"])["capacityProof"] == {
        "reportSha256": capacity_sha256,
        "tenantIds": ["tenant-00", "tenant-01"],
    }
    assert token not in receipt_path.read_text(encoding="utf-8")
    assert token not in proof_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("secret", "secret"),
        ("tenant-pair-drift", "capacity|tenant"),
    ),
    ids=("secret", "tenant-pair-drift"),
)
def test_tenant_isolation_rejects_secret_or_capacity_tenant_pair_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    capacity_path = candidate_root / "runtime" / "capacity-profile-attestation.json"
    capacity_sha256 = hashlib.sha256(capacity_path.read_bytes()).hexdigest()
    token = "tenant-isolation-secret-must-not-be-archived"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token)

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        support, report = _tenant_isolation_report(
            candidate,
            capacity_sha256=capacity_sha256,
        )
        if case == "secret":
            report["principals"][0]["actorId"] = token
        else:
            report["capacityProof"]["tenantIds"] = ["tenant-01", "tenant-02"]
        return subprocess.CompletedProcess(
            arguments,
            0,
            support._module().canonical_tenant_isolation_report(report).decode("utf-8"),
            "",
        )

    with pytest.raises(ValueError, match=message):
        module.write_tenant_isolation_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    assert not (candidate_root / "artifacts" / "tenant_isolation.json").exists()
    assert not (candidate_root / "runtime" / "tenant-isolation-attestation.json").exists()
    if case == "secret":
        assert not any(
            token.encode("utf-8") in path.read_bytes()
            for path in candidate_root.rglob("*")
            if path.is_file()
        )


def test_tenant_isolation_publication_failure_leaves_no_formal_receipt_or_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    _write_capacity_dependency(module, candidate_root, candidate, monkeypatch)
    capacity_path = candidate_root / "runtime" / "capacity-profile-attestation.json"
    capacity_sha256 = hashlib.sha256(capacity_path.read_bytes()).hexdigest()
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "tenant-isolation-token")
    proof_path = candidate_root / "runtime" / "tenant-isolation-attestation.json"
    receipt_path = candidate_root / "artifacts" / "tenant_isolation.json"
    real_publish = module._publish_classroom_no_replace

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        support, report = _tenant_isolation_report(
            candidate,
            capacity_sha256=capacity_sha256,
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            support._module().canonical_tenant_isolation_report(report).decode("utf-8"),
            "",
        )

    def fail_proof(boundary, source: Path, target: Path, *, source_handle) -> None:
        if target == proof_path:
            raise OSError("simulated tenant isolation proof publication failure")
        real_publish(boundary, source, target, source_handle=source_handle)

    monkeypatch.setattr(module, "_publish_classroom_no_replace", fail_proof)
    with pytest.raises(OSError, match="simulated tenant isolation proof publication failure"):
        module.write_tenant_isolation_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    assert not proof_path.exists()
    assert not receipt_path.exists()
    assert not list(candidate_root.rglob("*.staging"))


def test_release_evidence_cli_runs_fixed_tenant_isolation_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_evidence_module()
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def write_receipt(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {}

    monkeypatch.setattr(module, "write_tenant_isolation_receipt", write_receipt, raising=False)
    assert (
        module.main(
            [
                "tenant-isolation",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--timeout-seconds",
                "600",
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "timeout_seconds": 600,
    }
    assert capsys.readouterr().out == (
        f"{bundle_root / 'runtime' / 'tenant-isolation-attestation.json'}\n"
    )


def test_openmaic_shared_plane_receipt_is_derived_from_fixed_live_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    assert callable(getattr(module, "write_openmaic_shared_plane_receipt", None)), (
        "fixed OpenMAIC shared-plane receipt producer is missing"
    )
    support = _load_openmaic_smoke_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    runtime_path = candidate_root / "runtime" / "runtime-attestation.json"
    runtime_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    token = "openmaic-shared-plane-token-must-not-be-serialized"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token)
    monkeypatch.setenv("COMPOSE_FILE", "attacker-compose.yml")
    calls: list[dict[str, object]] = []
    publications: list[str] = []
    real_publish = module._publish_classroom_no_replace

    def runner(arguments, *, cwd, env, timeout):
        report = support._report(
            candidate=candidate,
            release_run=RELEASE_RUN,
            runtime_attestation_sha256=runtime_sha256,
        )
        calls.append(
            {
                "arguments": arguments,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            support._body(report).decode("utf-8"),
            "",
        )

    def record_publication(boundary, source: Path, target: Path, *, source_handle) -> None:
        real_publish(boundary, source, target, source_handle=source_handle)
        publications.append(target.relative_to(candidate_root).as_posix())

    monkeypatch.setattr(module, "_publish_classroom_no_replace", record_publication)
    receipt = module.write_openmaic_shared_plane_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=600,
        runner=runner,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["arguments"][-4:] == [
        "--plane",
        "shared",
        "--profile",
        "first-release",
    ]
    assert call["cwd"] == candidate_root.resolve()
    assert call["timeout"] == 600
    assert call["env"]["YFEISTAI_LIVE_FIXTURE_TOKEN"] == token
    assert call["env"]["YFEISTAI_CANDIDATE_ROOT"] == str(candidate_root.resolve())
    assert call["env"]["YFEISTAI_RELEASE_RUN_ID"] == RELEASE_RUN["runId"]
    assert call["env"]["YFEISTAI_ENVIRONMENT_ID"] == RELEASE_RUN["environmentId"]
    assert call["env"]["YFEISTAI_OPENMAIC_SMOKE_TIMEOUT_SECONDS"] == "570"
    assert call["env"]["WEB_BASE_URL"] == BASE_URL
    assert "COMPOSE_FILE" not in call["env"]

    receipt_path = candidate_root / "artifacts" / "openmaic_shared_plane.json"
    proof_path = candidate_root / "runtime" / "openmaic-shared-plane-attestation.json"
    assert publications == [
        "artifacts/openmaic_shared_plane.json",
        "runtime/openmaic-shared-plane-attestation.json",
    ]
    assert receipt == json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["evidence"] == "openmaic_shared_plane"
    assert receipt["receipt"]["producer"] == "openmaic-smoke"
    assert receipt["receipt"]["result"]["checks"] == {"sharedGenerationPassed": True}
    proof_sha256 = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    assert receipt["provenance"] == {
        "openmaicSharedPlaneAttestation": {
            "artifact": "runtime/openmaic-shared-plane-attestation.json",
            "sha256": proof_sha256,
        }
    }
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["runtimeAttestation"] == {
        "artifact": "runtime/runtime-attestation.json",
        "sha256": runtime_sha256,
    }
    assert proof["execution"]["command"] == module.openmaic_shared_plane_command_record()
    assert proof["summary"]["fixture"] == {
        "tenantId": "tenant-openmaic-shared-01",
        "teacherUserId": "teacher-openmaic-shared-01",
        "courseId": "course-openmaic-shared-01",
        "classId": "class-openmaic-shared-01",
    }
    assert proof["summary"]["binding"] == {
        "routeId": "shared-primary",
        "providerProfileId": "platform-default",
        "workerPoolRef": "shared-generation",
        "queueRef": "openmaic.shared",
    }
    assert proof["summary"]["checks"] == {"sharedGenerationPassed": True}
    assert token not in receipt_path.read_text(encoding="utf-8")
    assert token not in proof_path.read_text(encoding="utf-8")

    manifest = candidate_root / "release-evidence.json"
    module.assemble_manifest(
        manifest,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={"openmaic_shared_plane": receipt_path},
    )
    verifier = _load_verifier()
    result = verifier.verify(
        verifier.FileReleaseRuntime(
            manifest,
            expected_source_head=SOURCE_HEAD,
            candidate_root=candidate_root,
        )
    )
    assert result.layers["openmaic_shared_plane"].status == "pass"


def test_dedicated_outage_binding_replays_actual_marker_and_external_docker_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    marker_body = b'{"producer":"openmaic-dedicated-outage-attempt"}\n'
    marker_reference = {
        "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
        "sha256": hashlib.sha256(marker_body).hexdigest(),
    }
    outage_body = (
        json.dumps(
            {
                "fixture": {
                    "tenantId": "tenant-openmaic-dedicated-01",
                    "attemptMarker": marker_reference,
                },
                "provenance": {"attemptMarker": marker_reference},
                "outage": {"routeId": "dedicated-tenant-openmaic-01"},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    replay_calls: list[dict[str, object]] = []

    def replay_marker(*_args, return_body=False, **kwargs):
        assert return_body is True
        replay_calls.append(kwargs)
        return {"producer": "openmaic-dedicated-outage-attempt"}, marker_body

    def parse_outage(body: bytes, **kwargs):
        assert body == outage_body
        assert kwargs["attempt_marker_body"] == marker_body
        assert kwargs["expected_docker_host_identity_sha256"] == "6" * 64
        return {"producer": "openmaic-dedicated-outage"}

    monkeypatch.setattr(
        module,
        "_replay_openmaic_dedicated_outage_attempt_marker",
        replay_marker,
        raising=False,
    )
    monkeypatch.setattr(module, "parse_openmaic_dedicated_outage_attestation", parse_outage)

    assert module._parse_bound_openmaic_dedicated_outage_attestation(
        outage_body,
        bundle_root=candidate_root,
        candidate=candidate,
        release_run=RELEASE_RUN,
        expected_base_url="https://candidate.example.test",
        expected_runtime_attestation_sha256="5" * 64,
        expected_observer_attestation_sha256="7" * 64,
        expected_observer_id="shared-ingress-observer-openmaic-01",
        expected_observer_origin="https://observer.example.test",
        expected_shared_ingress_control_origin="https://shared-ingress.example.test",
        expected_tenant_id="tenant-openmaic-dedicated-01",
        expected_docker_host_identity_sha256="6" * 64,
    ) == {"producer": "openmaic-dedicated-outage"}
    assert len(replay_calls) == 1


def test_dedicated_outage_rejects_self_consistent_observer_without_external_trust_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    verifier = _load_verifier()
    support = _load_verify_release_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    support._RELEASE_RUN = RELEASE_RUN
    provenance = support._write_probe_proof(
        candidate_root,
        verifier,
        candidate,
        "openmaic_dedicated_plane",
    )
    receipt = candidate_root / "artifacts" / "openmaic_dedicated_plane.json"
    receipt.parent.mkdir(exist_ok=True)
    receipt.write_text(
        json.dumps(
            support._artifact_document(
                verifier,
                candidate,
                "openmaic_dedicated_plane",
                release_run=RELEASE_RUN,
                provenance=provenance,
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    verifier_globals = module.probe_provenance_error.__globals__
    monkeypatch.setitem(
        verifier_globals,
        "derive_openmaic_dedicated_plane_receipt_checks",
        lambda *_args, **_kwargs: (
            {
                "dedicatedGenerationPassed": True,
                "noSharedClientIssued": True,
            },
            "2026-08-24T00:00:00Z",
        ),
    )
    monkeypatch.setitem(
        verifier_globals,
        "validate_runtime_attestation",
        lambda *_args, **_kwargs: {},
    )
    output = candidate_root / "externally-anchored-release-evidence.json"

    with pytest.raises(ValueError, match="external observer trust anchor"):
        module.assemble_manifest(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"openmaic_dedicated_plane": receipt},
            expected_outage_docker_host_identity_sha256="7" * 64,
        )

    observer_path = candidate_root / "runtime" / "openmaic-shared-ingress-observer-attestation.json"
    with pytest.raises(ValueError, match="OpenMAIC dedicated outage execution proof"):
        module.assemble_manifest(
            output,
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            receipt_paths={"openmaic_dedicated_plane": receipt},
            expected_outage_docker_host_identity_sha256="7" * 64,
            expected_openmaic_observer_attestation_sha256="8" * 64,
            expected_openmaic_observer_id="foreign-observer-01",
            expected_openmaic_observer_origin="https://foreign-observer.example.test",
            expected_openmaic_shared_ingress_control_origin=(
                "https://foreign-control.example.test"
            ),
        )

    manifest = module.assemble_manifest(
        output,
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        receipt_paths={"openmaic_dedicated_plane": receipt},
        expected_outage_docker_host_identity_sha256="7" * 64,
        expected_openmaic_observer_attestation_sha256=hashlib.sha256(
            observer_path.read_bytes()
        ).hexdigest(),
        expected_openmaic_observer_id="shared-ingress-observer-openmaic-01",
        expected_openmaic_observer_origin="https://observer.example.test",
        expected_openmaic_shared_ingress_control_origin=("https://shared-ingress.example.test"),
    )

    assert manifest["evidence"]["openmaic_dedicated_plane"]["status"] == "pass"


def test_openmaic_dedicated_plane_receipt_is_derived_from_fixed_live_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    writer = getattr(module, "write_openmaic_dedicated_plane_receipt", None)
    assert callable(writer), "fixed OpenMAIC dedicated-plane receipt producer is missing"
    support = _load_openmaic_smoke_support()
    expected_host_identity_sha256 = support.DOCKER_HOST_IDENTITY_SHA256
    candidate_root, candidate = _write_candidate_root(tmp_path)
    runtime_path = candidate_root / "runtime" / "runtime-attestation.json"
    runtime_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    token = "openmaic-dedicated-plane-token-must-not-be-serialized"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token)
    monkeypatch.delenv("YFEISTAI_DEDICATED_TENANT_ID", raising=False)

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("dedicated-plane writer must reject an invalid tenant ID before running")

    for invalid_tenant_id in (None, " tenant-openmaic-dedicated-01"):
        if invalid_tenant_id is None:
            monkeypatch.delenv("YFEISTAI_DEDICATED_TENANT_ID", raising=False)
        else:
            monkeypatch.setenv("YFEISTAI_DEDICATED_TENANT_ID", invalid_tenant_id)
        with pytest.raises(
            ValueError,
            match="dedicated-plane tenant ID is unavailable or invalid",
        ):
            writer(
                candidate_root=candidate_root,
                bundle_root=candidate_root,
                release_run=RELEASE_RUN,
                timeout_seconds=600,
                expected_outage_docker_host_identity_sha256=expected_host_identity_sha256,
                runner=forbidden_runner,
            )

    dedicated_tenant_id = "tenant-openmaic-dedicated-01"
    monkeypatch.setenv("YFEISTAI_DEDICATED_TENANT_ID", dedicated_tenant_id)
    monkeypatch.setenv("COMPOSE_FILE", "attacker-compose.yml")
    with pytest.raises(ValueError, match="shared-ingress observer attestation is invalid"):
        writer(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            expected_outage_docker_host_identity_sha256=expected_host_identity_sha256,
            runner=forbidden_runner,
        )

    outage_path = candidate_root / "runtime" / "openmaic-dedicated-outage-attestation.json"
    observer_path = candidate_root / "runtime" / "openmaic-shared-ingress-observer-attestation.json"
    observer = support._observer_attestation()
    observer["releaseRun"] = RELEASE_RUN
    observer_body = support._module().canonical_openmaic_shared_ingress_observer_attestation(
        observer
    )
    observer_path.write_bytes(observer_body)
    observer_sha256 = hashlib.sha256(observer_body).hexdigest()
    marker_path = candidate_root / "runtime" / "openmaic-dedicated-outage-attempt.json"
    marker = support._dedicated_outage_attempt_marker(
        candidate=candidate,
        release_run=RELEASE_RUN,
        observer_sha256=observer_sha256,
    )
    marker_body = support._body(marker)
    marker_path.write_bytes(marker_body)
    marker_reference = {
        "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
        "sha256": hashlib.sha256(marker_body).hexdigest(),
    }
    outage_report = support._dedicated_outage_attestation(
        candidate=candidate,
        release_run=RELEASE_RUN,
        runtime_attestation_sha256=runtime_sha256,
    )
    outage_report["observerAttestation"]["sha256"] = observer_sha256
    outage_report["fixture"]["attemptMarker"] = marker_reference
    outage_report["provenance"]["attemptMarker"] = marker_reference
    outage_report["provenance"]["observerTrustAnchor"] = marker["observerTrustAnchor"]
    execution = outage_report.pop("execution")
    execution["stdoutSha256"] = hashlib.sha256(support._body(outage_report)).hexdigest()
    outage_report["execution"] = execution
    outage_path.write_bytes(
        support._module().canonical_openmaic_dedicated_outage_attestation(outage_report)
    )
    calls: list[dict[str, object]] = []
    publications: list[str] = []
    real_publish = module._publish_classroom_no_replace

    def runner(arguments, *, cwd, env, timeout):
        report = support._dedicated_report()
        report["candidate"] = candidate
        report["releaseRun"] = RELEASE_RUN
        report["runtimeAttestation"] = {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        }
        calls.append(
            {
                "arguments": arguments,
                "cwd": cwd,
                "env": env,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            support._body(report).decode("utf-8"),
            "",
        )

    def record_publication(boundary, source: Path, target: Path, *, source_handle) -> None:
        real_publish(boundary, source, target, source_handle=source_handle)
        publications.append(target.relative_to(candidate_root).as_posix())

    monkeypatch.setattr(module, "_publish_classroom_no_replace", record_publication)
    receipt = writer(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=600,
        expected_outage_docker_host_identity_sha256=expected_host_identity_sha256,
        runner=runner,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["arguments"][-4:] == [
        "--plane",
        "dedicated",
        "--profile",
        "first-release",
    ]
    assert call["cwd"] == candidate_root.resolve()
    assert call["timeout"] == 600
    assert call["env"]["YFEISTAI_LIVE_FIXTURE_TOKEN"] == token
    assert call["env"]["YFEISTAI_DEDICATED_TENANT_ID"] == dedicated_tenant_id
    assert dedicated_tenant_id not in call["arguments"]
    assert "COMPOSE_FILE" not in call["env"]

    receipt_path = candidate_root / "artifacts" / "openmaic_dedicated_plane.json"
    proof_path = candidate_root / "runtime" / "openmaic-dedicated-plane-attestation.json"
    assert publications == [
        "artifacts/openmaic_dedicated_plane.json",
        "runtime/openmaic-dedicated-plane-attestation.json",
    ]
    assert receipt == json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["evidence"] == "openmaic_dedicated_plane"
    assert receipt["receipt"]["result"]["checks"] == {
        "dedicatedGenerationPassed": True,
        "noSharedClientIssued": True,
        "noSharedFallback": True,
    }
    proof_sha256 = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    assert receipt["provenance"] == {
        "openmaicDedicatedPlaneAttestation": {
            "artifact": "runtime/openmaic-dedicated-plane-attestation.json",
            "sha256": proof_sha256,
        },
        "openmaicDedicatedOutageAttestation": {
            "artifact": "runtime/openmaic-dedicated-outage-attestation.json",
            "sha256": hashlib.sha256(outage_path.read_bytes()).hexdigest(),
        },
        "openmaicSharedIngressObserverAttestation": {
            "artifact": "runtime/openmaic-shared-ingress-observer-attestation.json",
            "sha256": hashlib.sha256(observer_body).hexdigest(),
        },
    }
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["execution"]["command"] == module.openmaic_dedicated_plane_command_record()
    assert proof["summary"]["binding"]["routeTenantId"] == proof["summary"]["fixture"]["tenantId"]
    assert proof["summary"]["checks"] == {
        "dedicatedGenerationPassed": True,
        "noSharedClientIssued": True,
    }
    assert token not in receipt_path.read_text(encoding="utf-8")
    assert token not in proof_path.read_text(encoding="utf-8")
    assert "YFEISTAI_DEDICATED_TENANT_ID" not in receipt_path.read_text(encoding="utf-8")
    assert "YFEISTAI_DEDICATED_TENANT_ID" not in proof_path.read_text(encoding="utf-8")


def test_openmaic_shared_plane_publication_failure_leaves_no_formal_receipt_or_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    support = _load_openmaic_smoke_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    runtime_sha256 = hashlib.sha256(
        (candidate_root / "runtime" / "runtime-attestation.json").read_bytes()
    ).hexdigest()
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "openmaic-shared-plane-token")
    proof_path = candidate_root / "runtime" / "openmaic-shared-plane-attestation.json"
    receipt_path = candidate_root / "artifacts" / "openmaic_shared_plane.json"
    real_publish = module._publish_classroom_no_replace

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        report = support._report(
            candidate=candidate,
            release_run=RELEASE_RUN,
            runtime_attestation_sha256=runtime_sha256,
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            support._body(report).decode("utf-8"),
            "",
        )

    def fail_proof(boundary, source: Path, target: Path, *, source_handle) -> None:
        if target == proof_path:
            raise OSError("simulated OpenMAIC shared-plane proof publication failure")
        real_publish(boundary, source, target, source_handle=source_handle)

    monkeypatch.setattr(module, "_publish_classroom_no_replace", fail_proof)
    with pytest.raises(
        OSError,
        match="simulated OpenMAIC shared-plane proof publication failure",
    ):
        module.write_openmaic_shared_plane_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    assert not proof_path.exists()
    assert not receipt_path.exists()
    assert not list((candidate_root / "staging").iterdir())


@pytest.mark.parametrize(
    ("parent_name", "published_name"),
    (
        ("artifacts", "openmaic_shared_plane.json"),
        ("runtime", "openmaic-shared-plane-attestation.json"),
    ),
)
def test_openmaic_shared_plane_never_publishes_through_a_replaced_target_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_name: str,
    published_name: str,
) -> None:
    module = _load_evidence_module()
    support = _load_openmaic_smoke_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    runtime_sha256 = hashlib.sha256(
        (candidate_root / "runtime" / "runtime-attestation.json").read_bytes()
    ).hexdigest()
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "openmaic-shared-plane-token")
    target_parent = candidate_root / parent_name
    retained_parent = candidate_root / f"{parent_name}-retained"
    redirected = tmp_path / f"redirected-openmaic-shared-plane-{parent_name}"
    redirected.mkdir()
    real_publish = module._publish_classroom_no_replace
    redirected_once = False
    replacement_blocked = False
    retained_snapshot: dict[str, bytes] | None = None

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        report = support._report(
            candidate=candidate,
            release_run=RELEASE_RUN,
            runtime_attestation_sha256=runtime_sha256,
        )
        return subprocess.CompletedProcess(
            arguments,
            0,
            support._body(report).decode("utf-8"),
            "",
        )

    def replace_parent(boundary, source: Path, target: Path, *, source_handle) -> None:
        nonlocal redirected_once, replacement_blocked, retained_snapshot
        if not redirected_once and target.parent == target_parent:
            retained_snapshot = _directory_snapshot(target_parent)
            try:
                os.replace(target_parent, retained_parent)
            except PermissionError as exc:
                replacement_blocked = True
                raise ValueError("publication directory changed") from exc
            try:
                target_parent.symlink_to(redirected, target_is_directory=True)
            except OSError:
                os.replace(retained_parent, target_parent)
                pytest.skip("directory symlinks are unavailable on this test host")
            redirected_once = True
        real_publish(boundary, source, target, source_handle=source_handle)

    monkeypatch.setattr(module, "_publish_classroom_no_replace", replace_parent)
    with pytest.raises(ValueError, match="publication directory changed"):
        module.write_openmaic_shared_plane_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    assert redirected_once is True or replacement_blocked is True
    assert not list(redirected.iterdir())
    assert retained_snapshot is not None
    retained_or_original = retained_parent if redirected_once else target_parent
    assert _directory_snapshot(retained_or_original) == retained_snapshot
    assert not (retained_or_original / published_name).exists()
    assert not (candidate_root / "artifacts" / "openmaic_shared_plane.json").exists()
    assert not (candidate_root / "runtime" / "openmaic-shared-plane-attestation.json").exists()


def test_release_evidence_cli_runs_fixed_openmaic_shared_plane_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_evidence_module()
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def write_receipt(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {}

    monkeypatch.setattr(
        module,
        "write_openmaic_shared_plane_receipt",
        write_receipt,
        raising=False,
    )
    assert (
        module.main(
            [
                "openmaic-shared-plane",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--timeout-seconds",
                "600",
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "timeout_seconds": 600,
    }
    assert capsys.readouterr().out == (
        f"{bundle_root / 'runtime' / 'openmaic-shared-plane-attestation.json'}\n"
    )


def test_release_evidence_cli_runs_fixed_openmaic_dedicated_plane_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_evidence_module()
    support = _load_openmaic_smoke_support()
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def write_receipt(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {}

    monkeypatch.setattr(
        module,
        "write_openmaic_dedicated_plane_receipt",
        write_receipt,
        raising=False,
    )
    assert (
        module.main(
            [
                "openmaic-dedicated-plane",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--timeout-seconds",
                "600",
                "--outage-docker-host-identity-sha256",
                support.DOCKER_HOST_IDENTITY_SHA256,
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "timeout_seconds": 600,
        "expected_outage_docker_host_identity_sha256": support.DOCKER_HOST_IDENTITY_SHA256,
    }
    assert capsys.readouterr().out == (
        f"{bundle_root / 'runtime' / 'openmaic-dedicated-plane-attestation.json'}\n"
    )


def test_capacity_profile_stops_before_runner_without_live_fixture_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    monkeypatch.delenv("YFEISTAI_LIVE_FIXTURE_TOKEN", raising=False)

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("capacity profile must stop before subprocess execution")

    with pytest.raises(ValueError, match="live fixture token is unavailable"):
        module.write_capacity_profile_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=forbidden_runner,
        )

    assert not (candidate_root / "runtime" / "capacity-profile-attestation.json").exists()
    assert not (candidate_root / "artifacts" / "capacity_profile.json").exists()
    assert not (candidate_root / "artifacts" / "learning_event_idempotency.json").exists()


def test_capacity_profile_rejects_live_fixture_token_in_canonical_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    token = "capacityTokenMustStaySecret"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", token)

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        report = _live_capacity_report(candidate)
        for sample in report["rawSamples"]:
            if sample["sequence"] == 0:
                sample["subjectId"] = token
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n",
            "",
        )

    with pytest.raises(ValueError, match="serialized live fixture token"):
        module.write_capacity_profile_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    assert not (candidate_root / "runtime" / "capacity-profile-attestation.json").exists()
    assert not (candidate_root / "artifacts" / "capacity_profile.json").exists()
    assert not (candidate_root / "artifacts" / "learning_event_idempotency.json").exists()


def test_capacity_profile_default_runner_rejects_oversized_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()

    def run(arguments, **options):
        assert "capture_output" not in options
        stdout = options["stdout"]
        stdout.write(b"x" * (module.MAX_CAPACITY_REPORT_BYTES + 1))
        stdout.flush()
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(module.subprocess, "run", run)

    with pytest.raises(subprocess.SubprocessError, match="output is too large"):
        module._run_capacity_profile(
            ["python", "capacity-probe"],
            cwd=tmp_path,
            env={},
            timeout=30,
        )


@pytest.mark.parametrize(
    "case",
    ("native-exit", "failed-report", "argv-drift", "runtime-drift", "candidate-drift"),
)
def test_capacity_profile_receipt_fails_closed_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    runtime_attestation = candidate_root / "runtime" / "runtime-attestation.json"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "capacity-token")

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        report = _live_capacity_report(candidate)
        if case == "failed-report":
            report["rawSamples"][0]["success"] = False
        stdout = json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n"
        if case == "runtime-drift":
            runtime_attestation.write_bytes(runtime_attestation.read_bytes() + b"\n")
        if case == "candidate-drift":
            lock_path = candidate_root / "deploy" / "image-lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["candidate"]["sourceHead"] = "b" * 40
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
        completed_arguments = ["attacker-command"] if case == "argv-drift" else arguments
        return subprocess.CompletedProcess(
            completed_arguments,
            7 if case == "native-exit" else 0,
            stdout,
            "probe failed" if case == "native-exit" else "",
        )

    with pytest.raises(ValueError, match="capacity profile"):
        module.write_capacity_profile_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    assert not (candidate_root / "runtime" / "capacity-profile-attestation.json").exists()
    assert not (candidate_root / "artifacts" / "capacity_profile.json").exists()
    assert not (candidate_root / "artifacts" / "learning_event_idempotency.json").exists()


@pytest.mark.parametrize(
    "relative_target",
    (
        "runtime/capacity-profile-attestation.json",
        "artifacts/capacity_profile.json",
        "artifacts/learning_event_idempotency.json",
    ),
)
def test_capacity_profile_refuses_any_preexisting_publication_target_before_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_target: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    target = candidate_root / relative_target
    target.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b'{"ownedBy":"another-writer"}\n'
    target.write_bytes(sentinel)
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "capacity-token")

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("preexisting evidence must stop before capacity subprocess execution")

    with pytest.raises(ValueError, match="evidence already exists"):
        module.write_capacity_profile_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=forbidden_runner,
        )

    assert target.read_bytes() == sentinel


def test_capacity_profile_refuses_dangling_target_entry_via_lexists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    dangling_target = candidate_root / "artifacts" / "learning_event_idempotency.json"
    real_lexists = module.os.path.lexists
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "capacity-token")

    def lexists(path: object) -> bool:
        return Path(path).resolve() == dangling_target.resolve() or real_lexists(path)

    monkeypatch.setattr(module.os.path, "lexists", lexists)

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("dangling evidence must stop before capacity subprocess execution")

    with pytest.raises(ValueError, match="evidence already exists"):
        module.write_capacity_profile_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=forbidden_runner,
        )

    assert not dangling_target.exists()


def test_capacity_profile_concurrent_target_is_never_overwritten_or_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    receipt_path = candidate_root / "artifacts" / "capacity_profile.json"
    sentinel = b'{"ownedBy":"another-writer"}\n'
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "capacity-token")

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(_live_capacity_report(candidate), separators=(",", ":"), sort_keys=True)
            + "\n",
            "",
        )

    real_candidate = module._candidate
    candidate_reads = 0

    def inject_concurrent_target(path: Path) -> dict[str, object]:
        nonlocal candidate_reads
        value = real_candidate(path)
        candidate_reads += 1
        if candidate_reads == 3:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_bytes(sentinel)
        return value

    monkeypatch.setattr(module, "_candidate", inject_concurrent_target)

    with pytest.raises(ValueError, match="release binding changed before publication"):
        module.write_capacity_profile_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    assert receipt_path.read_bytes() == sentinel
    failure_root = candidate_root / "failures" / "capacity-profile"
    assert all(path.read_bytes() != sentinel for path in failure_root.rglob("*.json"))


@pytest.mark.parametrize(
    ("failed_name", "expected_archive_names"),
    (
        (
            "capacity-receipt",
            {
                "failure.json",
                "proof.json",
                "capacity-receipt.json",
                "idempotency-receipt.json",
            },
        ),
        (
            "idempotency-receipt",
            {
                "failure.json",
                "proof.json",
                "idempotency-receipt.json",
                "published-capacity-receipt.json",
            },
        ),
        (
            "proof",
            {
                "failure.json",
                "proof.json",
                "published-capacity-receipt.json",
                "published-idempotency-receipt.json",
            },
        ),
    ),
)
def test_capacity_profile_publication_failure_relocates_owned_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_name: str,
    expected_archive_names: set[str],
) -> None:
    module = _load_evidence_module()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    proof_path = candidate_root / "runtime" / "capacity-profile-attestation.json"
    receipt_path = candidate_root / "artifacts" / "capacity_profile.json"
    idempotency_path = candidate_root / "artifacts" / "learning_event_idempotency.json"
    monkeypatch.setenv("YFEISTAI_LIVE_FIXTURE_TOKEN", "capacity-token")

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(_live_capacity_report(candidate), separators=(",", ":"), sort_keys=True)
            + "\n",
            "",
        )

    real_publish = module._publish_no_replace

    failed_path = {
        "capacity-receipt": receipt_path,
        "idempotency-receipt": idempotency_path,
        "proof": proof_path,
    }[failed_name]

    def fail_publication(source: Path, target: Path) -> None:
        if Path(target).resolve() == failed_path.resolve():
            raise OSError(f"simulated {failed_name} publication failure")
        real_publish(source, target)

    monkeypatch.setattr(module, "_publish_no_replace", fail_publication)

    with pytest.raises(OSError, match=f"simulated {failed_name} publication failure"):
        module.write_capacity_profile_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=600,
            runner=runner,
        )

    assert not proof_path.exists()
    assert not receipt_path.exists()
    assert not idempotency_path.exists()
    assert not list(candidate_root.rglob("*.staging"))
    failure_directories = list((candidate_root / "failures" / "capacity-profile").glob("*"))
    assert len(failure_directories) == 1
    assert {path.name for path in failure_directories[0].iterdir()} == expected_archive_names


def test_platform_preflight_stops_before_runner_when_trusted_docker_is_unavailable(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)

    def unavailable() -> Path:
        raise ValueError("trusted Docker CLI is unavailable")

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("preflight must stop before subprocess execution")

    with pytest.raises(ValueError, match="trusted Docker CLI is unavailable"):
        module.write_platform_preflight_receipts(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=120,
            runner=forbidden_runner,
            docker_resolver=unavailable,
        )

    assert not (candidate_root / "runtime" / "platform-preflight-attestation.json").exists()
    assert not (candidate_root / "artifacts" / "database_revisions.json").exists()
    assert not (candidate_root / "artifacts" / "service_health.json").exists()


def test_platform_preflight_default_runner_rejects_oversized_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()

    def run(arguments, **options):
        assert "capture_output" not in options
        stdout = options["stdout"]
        stdout.write(b"x" * ((16 * 1024) + 1))
        stdout.flush()
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(module.subprocess, "run", run)

    with pytest.raises(subprocess.SubprocessError, match="output is too large"):
        module._run_platform_preflight_phase(
            ["trusted-docker"],
            cwd=tmp_path,
            env={},
            timeout=30,
        )


@pytest.mark.parametrize(
    "case",
    ("native-exit", "failed-report", "argv-drift", "runtime-drift", "candidate-drift"),
)
def test_platform_preflight_receipts_fail_closed_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    runtime_attestation = candidate_root / "runtime" / "runtime-attestation.json"

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        phase = arguments[arguments.index("--runtime-phase") + 1]
        checks = (
            {
                "activeTenantCredentialsValid": True,
                "databaseConnected": True,
                "objectStoreRoundTrip": True,
                "revisionsMatch": True,
                "tenantCrossPrefixDenied": True,
                "tenantOwnPrefixAccessible": True,
            }
            if phase == "database-object-store"
            else {"openmaicContractCompatible": True}
        )
        errors: list[str] = []
        if case == "native-exit" and phase == "openmaic":
            return subprocess.CompletedProcess(arguments, 7, "", "phase failed")
        if case == "failed-report" and phase == "openmaic":
            checks["openmaicContractCompatible"] = False
            errors.append("OpenMAIC health and contract 1.0")
        report = {
            "schemaVersion": 1,
            "producer": "platform-preflight",
            "phase": phase,
            "checks": checks,
            "errors": errors,
        }
        stdout = (
            json.dumps(
                report,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        if case == "runtime-drift" and phase == "openmaic":
            runtime_attestation.write_bytes(runtime_attestation.read_bytes() + b"\n")
        if case == "candidate-drift" and phase == "openmaic":
            lock_path = candidate_root / "deploy" / "image-lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["candidate"]["sourceHead"] = "b" * 40
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
        completed_arguments = (
            ["attacker-controlled-command"]
            if case == "argv-drift" and phase == "openmaic"
            else arguments
        )
        return subprocess.CompletedProcess(completed_arguments, 0, stdout, "")

    monkeypatch.setattr(
        module,
        "_current_observed_at",
        lambda: "2026-08-25T00:01:00Z",
        raising=False,
    )
    with pytest.raises(ValueError, match="preflight|attestation"):
        module.write_platform_preflight_receipts(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=120,
            runner=runner,
            docker_resolver=lambda: (tmp_path / "trusted-docker").resolve(),
        )

    assert not (candidate_root / "runtime" / "platform-preflight-attestation.json").exists()
    assert not (candidate_root / "artifacts" / "database_revisions.json").exists()
    assert not (candidate_root / "artifacts" / "service_health.json").exists()


def test_platform_preflight_concurrent_target_is_never_overwritten_or_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate_document = _write_candidate_root(tmp_path)
    service_path = candidate_root / "artifacts" / "service_health.json"
    sentinel = b'{"ownedBy":"another-writer"}\n'

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        phase = arguments[arguments.index("--runtime-phase") + 1]
        checks = (
            {
                "activeTenantCredentialsValid": True,
                "databaseConnected": True,
                "objectStoreRoundTrip": True,
                "revisionsMatch": True,
                "tenantCrossPrefixDenied": True,
                "tenantOwnPrefixAccessible": True,
            }
            if phase == "database-object-store"
            else {"openmaicContractCompatible": True}
        )
        report = {
            "schemaVersion": 1,
            "producer": "platform-preflight",
            "phase": phase,
            "checks": checks,
            "errors": [],
        }
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n",
            "",
        )

    real_candidate = module._candidate
    candidate_reads = 0

    def inject_concurrent_target(path: Path) -> dict[str, object]:
        nonlocal candidate_reads
        candidate = real_candidate(path)
        candidate_reads += 1
        if candidate_reads == 3:
            service_path.parent.mkdir(parents=True, exist_ok=True)
            service_path.write_bytes(sentinel)
        return candidate

    monkeypatch.setattr(module, "_candidate", inject_concurrent_target)
    monkeypatch.setattr(
        module,
        "_current_observed_at",
        lambda: "2026-08-25T00:01:00Z",
        raising=False,
    )

    with pytest.raises(ValueError, match="release binding|already exists"):
        module.write_platform_preflight_receipts(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=120,
            runner=runner,
            docker_resolver=lambda: (tmp_path / "trusted-docker").resolve(),
        )

    assert service_path.read_bytes() == sentinel
    failure_root = candidate_root / "failures" / "platform-preflight"
    assert all(path.read_bytes() != sentinel for path in failure_root.rglob("*.json"))


def test_platform_preflight_publication_failure_relocates_all_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    proof_path = candidate_root / "runtime" / "platform-preflight-attestation.json"
    database_path = candidate_root / "artifacts" / "database_revisions.json"
    service_path = candidate_root / "artifacts" / "service_health.json"
    docker_configs: list[Path] = []

    def runner(arguments, *, cwd, env, timeout):
        del cwd, env, timeout
        phase = arguments[arguments.index("--runtime-phase") + 1]
        checks = (
            {
                "activeTenantCredentialsValid": True,
                "databaseConnected": True,
                "objectStoreRoundTrip": True,
                "revisionsMatch": True,
                "tenantCrossPrefixDenied": True,
                "tenantOwnPrefixAccessible": True,
            }
            if phase == "database-object-store"
            else {"openmaicContractCompatible": True}
        )
        report = {
            "schemaVersion": 1,
            "producer": "platform-preflight",
            "phase": phase,
            "checks": checks,
            "errors": [],
        }
        docker_configs.append(Path(arguments[2]))
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n",
            "",
        )

    real_publish = module._publish_no_replace

    def fail_service_receipt(source: Path, target: Path) -> None:
        if Path(target).resolve() == service_path.resolve():
            raise OSError("simulated service receipt publication failure")
        real_publish(source, target)

    monkeypatch.setattr(module, "_publish_no_replace", fail_service_receipt)
    monkeypatch.setattr(
        module,
        "_current_observed_at",
        lambda: "2026-08-25T00:01:00Z",
        raising=False,
    )

    with pytest.raises(OSError, match="simulated service receipt publication failure"):
        module.write_platform_preflight_receipts(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=120,
            runner=runner,
            docker_resolver=lambda: (tmp_path / "trusted-docker").resolve(),
        )

    assert not proof_path.exists()
    assert not database_path.exists()
    assert not service_path.exists()
    assert not list(candidate_root.rglob("*.staging"))
    assert len(set(docker_configs)) == 1
    assert not docker_configs[0].exists()
    failure_directories = list((candidate_root / "failures" / "platform-preflight").glob("*"))
    assert len(failure_directories) == 1
    assert {path.name for path in failure_directories[0].iterdir()} == {
        "failure.json",
        "proof.json",
        "published-database-revisions.json",
        "service_health.json",
    }


def test_probe_failure_archive_rejects_symlink_boundary(tmp_path: Path) -> None:
    module = _load_evidence_module()
    bundle_root = tmp_path / "bundle"
    outside_root = tmp_path / "outside"
    bundle_root.mkdir()
    outside_root.mkdir()
    failures = bundle_root / "failures"
    try:
        failures.symlink_to(outside_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this test host")
    staged = bundle_root / "staged.json"
    staged.write_text("staged", encoding="utf-8")

    with pytest.raises(ValueError, match="failure archive boundary"):
        module._record_probe_failure(
            bundle_root=bundle_root,
            evidence="platform-preflight",
            recipe="candidate-network-phases",
            attempt_id="fixed-attempt",
            reason="simulated failure",
            native_exit=0,
            artifacts={"proof": staged},
        )

    assert staged.read_text(encoding="utf-8") == "staged"
    assert list(outside_root.iterdir()) == []


def test_probe_failure_archive_uses_anchored_relative_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_evidence_module()
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    staged = bundle_root / "staged.json"
    staged.write_text("staged", encoding="utf-8")
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *_args, **_kwargs: pytest.fail("failure archive must not use path-based replace"),
    )

    failure_dir = module._record_probe_failure(
        bundle_root=bundle_root,
        evidence="platform-preflight",
        recipe="candidate-network-phases",
        attempt_id="fixed-attempt",
        reason="simulated failure",
        native_exit=0,
        artifacts={"proof": staged},
    )

    assert not staged.exists()
    assert (failure_dir / "proof.json").read_text(encoding="utf-8") == "staged"
    assert (failure_dir / "failure.json").is_file()


def test_release_evidence_cli_runs_fixed_platform_preflight_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_evidence_module()
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def write_receipts(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {}

    monkeypatch.setattr(
        module,
        "write_platform_preflight_receipts",
        write_receipts,
        raising=False,
    )
    assert (
        module.main(
            [
                "platform-preflight",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--timeout-seconds",
                "120",
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "timeout_seconds": 120,
    }
    assert capsys.readouterr().out == (
        f"{bundle_root / 'runtime' / 'platform-preflight-attestation.json'}\n"
    )


def test_release_evidence_cli_runs_fixed_capacity_profile_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_evidence_module()
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def write_receipt(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {}

    monkeypatch.setattr(
        module,
        "write_capacity_profile_receipt",
        write_receipt,
        raising=False,
    )
    assert (
        module.main(
            [
                "capacity-profile",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--timeout-seconds",
                "300",
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "timeout_seconds": 300,
    }
    assert capsys.readouterr().out == (
        f"{bundle_root / 'runtime' / 'capacity-profile-attestation.json'}\n"
    )


@pytest.mark.parametrize("command", ("source-head", "image-digests"))
def test_release_evidence_cli_writes_candidate_receipts(
    tmp_path: Path,
    monkeypatch,
    capsys,
    command: str,
) -> None:
    module = _load_evidence_module()
    output = tmp_path / f"{command}.json"
    candidate_root = tmp_path / "candidate"
    source_root = tmp_path / "source"
    captured: dict[str, object] = {}

    def write_receipt(path: Path, **arguments: object) -> dict[str, object]:
        captured["path"] = path
        captured.update(arguments)
        return {}

    function_name = (
        "write_source_head_receipt" if command == "source-head" else "write_image_digest_receipt"
    )
    monkeypatch.setattr(module, function_name, write_receipt)
    arguments = [
        command,
        "--output",
        str(output),
        "--candidate-root",
        str(candidate_root),
        "--run-id",
        RELEASE_RUN["runId"],
        "--environment-id",
        RELEASE_RUN["environmentId"],
        "--observed-at",
        "2026-08-25T00:00:00Z",
    ]
    if command == "source-head":
        arguments.extend(("--source-root", str(source_root)))

    assert module.main(arguments) == 0
    assert captured == {
        "path": output,
        "candidate_root": candidate_root,
        "release_run": RELEASE_RUN,
        "observed_at": "2026-08-25T00:00:00Z",
        **({"source_root": source_root} if command == "source-head" else {}),
    }
    assert capsys.readouterr().out == f"{output}\n"


def test_release_evidence_cli_writes_backup_restore_receipt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_evidence_module()
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def write_receipt(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {}

    monkeypatch.setattr(module, "write_backup_restore_receipt", write_receipt)

    assert (
        module.main(
            [
                "backup-restore",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--database-ownership",
                "runner-owned-disposable",
                "--object-namespace-ownership",
                "runner-owned-disposable",
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "database_ownership": "runner-owned-disposable",
        "object_namespace_ownership": "runner-owned-disposable",
    }
    assert capsys.readouterr().out == f"{bundle_root / 'artifacts' / 'backup_restore.json'}\n"


def test_release_evidence_cli_assembles_named_receipts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_evidence_module()
    output = tmp_path / "release-evidence.json"
    candidate_root = tmp_path / "candidate"
    source_receipt = tmp_path / "source-head.json"
    image_receipt = tmp_path / "image-digests.json"
    captured: dict[str, object] = {}

    def assemble(path: Path, **arguments: object) -> dict[str, object]:
        captured["path"] = path
        captured.update(arguments)
        return {}

    monkeypatch.setattr(module, "assemble_manifest", assemble)
    assert (
        module.main(
            [
                "assemble",
                "--output",
                str(output),
                "--candidate-root",
                str(candidate_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--receipt",
                f"source_head={source_receipt}",
                "--receipt",
                f"image_digests={image_receipt}",
            ]
        )
        == 0
    )
    assert captured == {
        "path": output,
        "candidate_root": candidate_root,
        "release_run": RELEASE_RUN,
        "receipt_paths": {
            "source_head": source_receipt,
            "image_digests": image_receipt,
        },
    }
    assert capsys.readouterr().out == f"{output}\n"


def test_release_evidence_cli_produces_only_a_fixed_recipe(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_evidence_module()
    bundle_root = tmp_path / "bundle"
    candidate_root = bundle_root / "candidate"
    output = bundle_root / "artifacts" / "teacher_flow.json"
    working_directory = tmp_path / "source"
    captured: dict[str, object] = {}

    def produce(path: Path, **arguments: object) -> dict[str, object]:
        captured["path"] = path
        captured.update(arguments)
        return {}

    monkeypatch.setattr(module, "run_probe_receipt", produce)
    assert (
        module.main(
            [
                "produce",
                "--evidence",
                "teacher_flow",
                "--output",
                str(output),
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--working-directory",
                str(working_directory),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--observed-at",
                "2026-08-25T00:00:00Z",
                "--timeout-seconds",
                "300",
                "--base-url",
                BASE_URL,
            ]
        )
        == 0
    )
    assert captured == {
        "path": output,
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "evidence": "teacher_flow",
        "observed_at": "2026-08-25T00:00:00Z",
        "base_url": BASE_URL,
        "raw_report_path": bundle_root / "raw" / "teacher_flow.json",
        "execution_record_path": bundle_root / "executions" / "teacher_flow.json",
        "recipe": "teacher_flow",
        "working_directory": working_directory,
        "timeout_seconds": 300,
    }
    assert capsys.readouterr().out == f"{output}\n"


@pytest.mark.parametrize("forbidden", ("--command", "--native-exit", "--checks"))
def test_release_evidence_cli_rejects_self_attestation_inputs(
    tmp_path: Path,
    forbidden: str,
) -> None:
    module = _load_evidence_module()

    with pytest.raises(SystemExit):
        module._parse_args(
            [
                "produce",
                "--evidence",
                "teacher_flow",
                "--output",
                str(tmp_path / "receipt.json"),
                "--candidate-root",
                str(tmp_path / "candidate"),
                "--bundle-root",
                str(tmp_path / "bundle"),
                "--working-directory",
                str(tmp_path),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--observed-at",
                "2026-08-25T00:00:00Z",
                "--timeout-seconds",
                "300",
                "--base-url",
                BASE_URL,
                forbidden,
                "attacker-controlled",
            ]
        )


def _gateway_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_evidence_module()
    contract = _load_gateway_public_contract()
    support = _load_gateway_public_support()
    candidate_root, candidate = _write_candidate_root(tmp_path)
    runtime_path = candidate_root / "runtime" / "runtime-attestation.json"
    runtime = json.loads(runtime_path.read_bytes())
    context_stdout = json.dumps(support.DOCKER_ENDPOINT)
    info_stdout = json.dumps(
        {
            "osType": support.DOCKER_OS_TYPE,
            "serverId": support.DOCKER_SERVER_ID,
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    def host_command(arguments: list[str], stdout: str) -> dict[str, object]:
        return {
            "argv": [*support.DOCKER_LOGICAL_PREFIX, *arguments],
            "nativeExit": 0,
            "stdout": stdout,
            "stdoutSha256": hashlib.sha256(stdout.encode()).hexdigest(),
        }

    host_commands = [
        host_command(support.DOCKER_CONTEXT_ARGUMENTS, context_stdout),
        host_command(support.DOCKER_INFO_ARGUMENTS, info_stdout),
    ]
    runtime_commands = runtime["commands"]
    runtime["commands"] = [
        *runtime_commands[:2],
        *host_commands,
        *runtime_commands[2:],
        *host_commands,
    ]
    runtime_path.write_text(json.dumps(runtime, sort_keys=True), encoding="utf-8")
    trust_pair = support.write_gateway_trust_pair(
        candidate_root,
        contract,
        trusted_root=tmp_path / "trusted-gateway-controller",
        candidate=candidate,
        release_run=RELEASE_RUN,
        runtime_path=runtime_path,
    )

    def validate_runtime(path: Path, **_kwargs):
        assert Path(path) == runtime_path
        return json.loads(runtime_path.read_bytes())

    monkeypatch.setattr(module, "validate_runtime_attestation", validate_runtime)
    report_path = Path(trust_pair["report_path"])
    attestation_path = Path(trust_pair["observer_attestation_path"])
    monkeypatch.setenv(
        support.EXPECTED_ATTESTATION_ENV,
        str(trust_pair["observer_attestation_sha256"]),
    )
    monkeypatch.setenv(support.EXPECTED_OBSERVER_ID_ENV, support.OBSERVER_ID)
    monkeypatch.setenv(support.EXPECTED_OBSERVER_ORIGIN_ENV, support.OBSERVER_ORIGIN)
    monkeypatch.setenv(support.TRUSTED_NOW_ENV, support.TRUSTED_NOW)
    monkeypatch.setenv(support.RUN_STARTED_AT_ENV, support.RUN_STARTED_AT)
    monkeypatch.setenv(support.RUN_ENDED_AT_ENV, support.RUN_ENDED_AT)
    return (
        module,
        contract,
        support,
        candidate_root,
        runtime_path,
        report_path,
        attestation_path,
        trust_pair,
    )


def _gateway_external_trust_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    return _gateway_fixture(tmp_path, monkeypatch)


def _invalidate_gateway_trust_input(
    trust_pair: dict[str, object],
    invalid_input: str,
) -> None:
    input_name, mutation = invalid_input.rsplit("-", 1)
    path_keys = {
        "observer-envelope": "observer_envelope_path",
        "host-envelope": "host_envelope_path",
        "host-receipt": "host_receipt_path",
        "keyring": "keyring_path",
    }
    path = Path(trust_pair[path_keys[input_name]])
    if mutation == "missing":
        path.rename(path.with_name(f"{path.name}.missing"))
    else:
        path.write_bytes(path.read_bytes() + b" ")


@pytest.mark.parametrize(
    "invalid_input",
    (
        "observer-envelope-missing",
        "observer-envelope-tampered",
        "host-envelope-missing",
        "host-envelope-tampered",
        "host-receipt-missing",
        "host-receipt-tampered",
        "keyring-missing",
        "keyring-tampered",
    ),
)
def test_gateway_only_public_requires_external_trust_pair_before_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_input: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_external_trust_fixture(tmp_path, monkeypatch)
    _invalidate_gateway_trust_input(trust_pair, invalid_input)
    calls: list[list[str]] = []

    try:
        with pytest.raises(ValueError, match="gateway trust"):
            module.write_gateway_only_public_receipt(
                candidate_root=candidate_root,
                bundle_root=candidate_root,
                release_run=RELEASE_RUN,
                timeout_seconds=30,
                expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
                **support.gateway_trust_arguments(trust_pair),
                runner=support.docker_runner(runtime_path, calls=calls),
                docker_resolver=lambda: Path("C:/fixed/docker.exe"),
            )
    finally:
        assert calls == []


def test_gateway_only_public_binds_external_trust_pair_into_execution_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_external_trust_fixture(tmp_path, monkeypatch)

    module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(runtime_path),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    runtime = json.loads(runtime_path.read_bytes())
    proof = json.loads(
        (candidate_root / "runtime" / "gateway-only-public-attestation.json").read_bytes()
    )
    host_receipt = trust_pair["host_receipt"]
    assert isinstance(host_receipt, dict)
    host = host_receipt["host"]
    assert isinstance(host, dict)
    expected_runtime_identity = {
        "context": host["dockerContext"],
        "endpoint": host["dockerEndpoint"],
        "serverId": host["dockerServerId"],
        "dockerHostIdentitySha256": trust_pair["host_receipt_sha256"],
    }
    assert proof["trustPair"] == support.gateway_trust_references(trust_pair)
    assert runtime["dockerHostIdentity"] == expected_runtime_identity
    assert proof["docker"]["daemon"] == {
        **expected_runtime_identity,
        "osType": host["osType"],
    }


@pytest.mark.parametrize("legacy_environment", ("absent", "conflicting"))
def test_gateway_only_public_uses_signed_observer_policy_without_legacy_trust_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_environment: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_external_trust_fixture(tmp_path, monkeypatch)
    legacy_values = {
        support.EXPECTED_ATTESTATION_ENV: "f" * 64,
        support.EXPECTED_OBSERVER_ID_ENV: "conflicting-observer-01",
        support.EXPECTED_OBSERVER_ORIGIN_ENV: "https://conflicting-observer.example.net",
        support.TRUSTED_NOW_ENV: "2026-08-30T04:06:00Z",
        support.RUN_STARTED_AT_ENV: "2026-08-30T03:59:00Z",
        support.RUN_ENDED_AT_ENV: "2026-08-30T04:11:00Z",
    }
    for name, value in legacy_values.items():
        if legacy_environment == "absent":
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    receipt = module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(runtime_path),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    expected_checks = {"gatewayPublic": True, "internalPortsClosed": True}
    proof = json.loads(
        (candidate_root / "runtime" / "gateway-only-public-attestation.json").read_bytes()
    )
    assert proof["trustPair"] == support.gateway_trust_references(trust_pair)
    assert proof["summary"]["checks"] == expected_checks
    assert receipt["receipt"]["result"]["checks"] == expected_checks


def _expected_gateway_service_networks(support) -> dict[str, list[str]]:
    internal = f"{support.DOCKER_PROJECT}_platform-internal"
    expected = {
        service: [internal]
        for service in (
            "postgres",
            "minio",
            "minio-bootstrap",
            "teaching-migrate",
            "tenant-provisioner",
            "shared-data-plane-bootstrap",
            "openmaic-render",
            "teaching-dispatcher",
            "teaching-worker",
            "teaching-export-worker",
            "teaching-reaper",
            "learning-projector",
        )
    }
    expected.update(
        {
            "deeptutor": [
                internal,
                f"{support.DOCKER_PROJECT}_platform-service-egress",
            ],
            "gateway": [
                internal,
                f"{support.DOCKER_PROJECT}_platform-edge",
            ],
            "openmaic": [
                internal,
                f"{support.DOCKER_PROJECT}_shared-provider-egress",
            ],
        }
    )
    return expected


def test_gateway_only_public_candidate_compose_locks_each_runtime_service_network_set(
    tmp_path: Path,
) -> None:
    support = _load_gateway_public_support()
    candidate_root, _candidate = _write_candidate_root(tmp_path)
    runtime = json.loads((candidate_root / "runtime" / "runtime-attestation.json").read_bytes())

    assert support.expected_service_networks(
        candidate_root / "docker-compose.platform.yml",
        runtime,
    ) == _expected_gateway_service_networks(support)


def test_gateway_only_public_binds_trusted_docker_host_identity_to_runtime_and_observed_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(
        tmp_path,
        monkeypatch,
    )

    with pytest.raises(ValueError, match="Docker host identity"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256="8" * 64,
            **support.gateway_trust_arguments(trust_pair),
            runner=support.docker_runner(runtime_path),
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )
    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()

    module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(runtime_path),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    runtime = json.loads(runtime_path.read_bytes())
    proof = json.loads(
        (candidate_root / "runtime" / "gateway-only-public-attestation.json").read_bytes()
    )
    expected_identity = support.docker_host_identity(
        identity_sha256=str(trust_pair["host_receipt_sha256"])
    )
    assert runtime["dockerHostIdentity"] == expected_identity
    assert proof["docker"]["daemon"] == {
        **expected_identity,
        "osType": support.DOCKER_OS_TYPE,
    }


@pytest.mark.parametrize(
    ("case", "attacker_value"),
    (
        ("endpoint", "npipe:////./pipe/attacker"),
        ("serverId", "daemon-attacker"),
    ),
    ids=("endpoint", "server-id"),
)
def test_gateway_only_public_rejects_self_consistent_untrusted_docker_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    attacker_value: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(
        tmp_path,
        monkeypatch,
    )
    original_runner = support.docker_runner(runtime_path)

    def runner(arguments, *, cwd, env, timeout):
        logical = arguments[5:]
        if case == "endpoint" and logical == support.DOCKER_CONTEXT_ARGUMENTS:
            return subprocess.CompletedProcess(arguments, 0, json.dumps(attacker_value), "")
        if case == "serverId" and logical == support.DOCKER_INFO_ARGUMENTS:
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    {"osType": support.DOCKER_OS_TYPE, "serverId": attacker_value},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "",
            )
        return original_runner(arguments, cwd=cwd, env=env, timeout=timeout)

    with pytest.raises(ValueError, match="Docker host identity"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=runner,
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()


def test_gateway_only_public_receipt_records_exact_candidate_compose_network_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    compose_path = candidate_root / "docker-compose.platform.yml"
    calls: list[list[str]] = []

    module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(
            runtime_path,
            compose_path=compose_path,
            calls=calls,
        ),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    proof = json.loads(
        (candidate_root / "runtime" / "gateway-only-public-attestation.json").read_bytes()
    )
    expected_networks = _expected_gateway_service_networks(support)
    for snapshot_name in ("beforeSnapshot", "afterSnapshot"):
        snapshot = proof["docker"][snapshot_name]
        assert {
            row["service"]: {
                "networkMode": row["networkMode"],
                "networks": row["networks"],
            }
            for row in snapshot
        } == {
            service: {
                "networkMode": networks[-1],
                "networks": sorted(networks),
            }
            for service, networks in expected_networks.items()
        }
    inspect_calls = [
        arguments for arguments in calls if arguments[5:8] == ["container", "inspect", "--format"]
    ]
    assert inspect_calls
    assert all(arguments[8] == support.DOCKER_NETWORK_INSPECT_FORMAT for arguments in inspect_calls)


@pytest.mark.parametrize("network_drift", ("missing-network", "additional-network"))
def test_gateway_only_public_rejects_missing_or_additional_compose_network_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_drift: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="network set"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=support.docker_runner(
                runtime_path,
                compose_path=candidate_root / "docker-compose.platform.yml",
                network_drift=network_drift,
            ),
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()


def test_gateway_only_public_receipt_replays_external_and_docker_proof_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        report_path,
        attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    immutable_inputs = {
        report_path: report_path.read_bytes(),
        attestation_path: attestation_path.read_bytes(),
    }
    sentinel_secret = "gateway-sentinel-secret-must-not-leak"
    monkeypatch.setenv("YFEISTAI_GATEWAY_SENTINEL_SECRET", sentinel_secret)
    docker_calls: list[list[str]] = []
    docker_environments: list[dict[str, str]] = []
    publications: list[str] = []
    original_publish = module._publish_no_replace

    def publish(source: Path, target: Path) -> None:
        publications.append(target.relative_to(candidate_root).as_posix())
        original_publish(source, target)

    monkeypatch.setattr(module, "_publish_no_replace", publish)
    receipt = module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(
            runtime_path,
            calls=docker_calls,
            environments=docker_environments,
        ),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    proof_path = candidate_root / "runtime" / "gateway-only-public-attestation.json"
    receipt_path = candidate_root / "artifacts" / "gateway_only_public.json"
    proof = json.loads(proof_path.read_bytes())
    assert publications == [
        "artifacts/gateway_only_public.json",
        "runtime/gateway-only-public-attestation.json",
    ]
    assert proof["docker"]["beforeSnapshot"] == proof["docker"]["afterSnapshot"]
    assert proof["docker"]["daemon"] == {
        "context": support.DOCKER_CONTEXT,
        "endpoint": support.DOCKER_ENDPOINT,
        "serverId": support.DOCKER_SERVER_ID,
        "osType": support.DOCKER_OS_TYPE,
        "dockerHostIdentitySha256": trust_pair["host_receipt_sha256"],
    }
    runtime = json.loads(runtime_path.read_bytes())
    ordered_ids = sorted(str(row["containerId"]) for row in runtime["containers"])
    expected_round = [
        support.DOCKER_CONTEXT_ARGUMENTS,
        support.DOCKER_INFO_ARGUMENTS,
        support.DOCKER_PS_ARGUMENTS,
        *[
            [
                "container",
                "inspect",
                "--format",
                support.DOCKER_NETWORK_INSPECT_FORMAT,
                container_id,
            ]
            for container_id in ordered_ids
        ],
        support.DOCKER_CONTEXT_ARGUMENTS,
        support.DOCKER_INFO_ARGUMENTS,
    ]
    assert [arguments[5:] for arguments in docker_calls] == [
        *expected_round,
        *expected_round,
    ]
    assert all(Path(arguments[0]) == Path("C:/fixed/docker.exe") for arguments in docker_calls)
    assert all(arguments[1] == "--config" for arguments in docker_calls)
    assert all(arguments[3:5] == ["--context", "default"] for arguments in docker_calls)
    expected_networks = _expected_gateway_service_networks(support)
    assert all(
        row["networkMode"] == expected_networks[row["service"]][-1]
        for row in proof["docker"]["beforeSnapshot"]
    )
    assert [row for row in proof["docker"]["beforeSnapshot"] if row["publishedPorts"]] == [
        {
            "containerId": "container-gateway",
            "project": "yfeistai-platform",
            "service": "gateway",
            "networkMode": f"{support.DOCKER_PROJECT}_platform-edge",
            "networks": [
                f"{support.DOCKER_PROJECT}_platform-edge",
                f"{support.DOCKER_PROJECT}_platform-internal",
            ],
            "publishedPorts": [
                {
                    "containerPort": 80,
                    "hostIp": "0.0.0.0",
                    "hostPort": 80,
                    "protocol": "tcp",
                },
                {
                    "containerPort": 443,
                    "hostIp": "0.0.0.0",
                    "hostPort": 443,
                    "protocol": "tcp",
                },
            ],
        }
    ]
    assert proof["summary"]["checks"] == {
        "gatewayPublic": True,
        "internalPortsClosed": True,
    }
    assert receipt["provenance"] == support.receipt_provenance(proof_path)
    assert json.loads(receipt_path.read_bytes()) == receipt
    assert {path: path.read_bytes() for path in immutable_inputs} == immutable_inputs
    assert all(sentinel_secret not in environment.values() for environment in docker_environments)
    assert sentinel_secret not in proof_path.read_text(encoding="utf-8")
    assert sentinel_secret not in receipt_path.read_text(encoding="utf-8")


def test_gateway_only_public_proves_before_and_after_daemon_identity_for_each_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    calls: list[list[str]] = []

    module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(runtime_path, calls=calls),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    logical_calls = [arguments[5:] for arguments in calls]
    assert logical_calls.count(support.DOCKER_CONTEXT_ARGUMENTS) == 4
    assert logical_calls.count(support.DOCKER_INFO_ARGUMENTS) == 4
    runtime = json.loads(runtime_path.read_bytes())
    ordered_ids = sorted(str(row["containerId"]) for row in runtime["containers"])
    snapshot_commands = [
        support.DOCKER_PS_ARGUMENTS,
        *[
            [
                "container",
                "inspect",
                "--format",
                support.DOCKER_NETWORK_INSPECT_FORMAT,
                container_id,
            ]
            for container_id in ordered_ids
        ],
    ]
    bracketed_round = [
        support.DOCKER_CONTEXT_ARGUMENTS,
        support.DOCKER_INFO_ARGUMENTS,
        *snapshot_commands,
        support.DOCKER_CONTEXT_ARGUMENTS,
        support.DOCKER_INFO_ARGUMENTS,
    ]
    assert logical_calls == [*bracketed_round, *bracketed_round]
    proof = json.loads(
        (candidate_root / "runtime" / "gateway-only-public-attestation.json").read_bytes()
    )
    command_records = proof["docker"]["commands"]
    assert [record["argv"][5:] for record in command_records] == logical_calls
    context_observations = [
        json.loads(record["stdout"])
        for record in command_records
        if record["argv"][5:] == support.DOCKER_CONTEXT_ARGUMENTS
    ]
    info_observations = [
        json.loads(record["stdout"])
        for record in command_records
        if record["argv"][5:] == support.DOCKER_INFO_ARGUMENTS
    ]
    assert context_observations == [support.DOCKER_ENDPOINT] * 4
    assert (
        info_observations
        == [{"osType": support.DOCKER_OS_TYPE, "serverId": support.DOCKER_SERVER_ID}] * 4
    )


def test_gateway_only_public_rejects_second_round_post_snapshot_daemon_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    original_runner = support.docker_runner(runtime_path)
    context_observations = 0
    info_observations = 0

    def runner(arguments, *, cwd, env, timeout):
        nonlocal context_observations, info_observations
        logical = arguments[5:]
        if logical == support.DOCKER_CONTEXT_ARGUMENTS:
            context_observations += 1
            if context_observations == 4:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    json.dumps("npipe:////./pipe/attacker"),
                    "",
                )
        elif logical == support.DOCKER_INFO_ARGUMENTS:
            info_observations += 1
            if info_observations == 4:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    json.dumps(
                        {"osType": support.DOCKER_OS_TYPE, "serverId": "daemon-attacker"},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "",
                )
        return original_runner(arguments, cwd=cwd, env=env, timeout=timeout)

    with pytest.raises(ValueError, match="Docker.*(?:daemon|host identity)"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=runner,
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert context_observations == 4
    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()


def test_gateway_only_public_uses_single_decreasing_deadline_across_both_docker_rounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    now = [100.0]
    timeouts: list[int] = []
    original_runner = support.docker_runner(runtime_path)

    def monotonic() -> float:
        return now[0]

    def runner(arguments, *, cwd, env, timeout):
        timeouts.append(timeout)
        completed = original_runner(
            arguments,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
        now[0] += 1.0
        return completed

    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(monotonic=monotonic),
        raising=False,
    )

    module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=60,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=runner,
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    runtime = json.loads(runtime_path.read_bytes())
    command_count = 2 * (len(runtime["containers"]) + 5)
    assert timeouts == [60 - index for index in range(command_count)]


def test_gateway_only_public_rejects_exhausted_total_deadline_before_next_docker_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        report_path,
        attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    immutable_inputs = (report_path.read_bytes(), attestation_path.read_bytes())
    now = [200.0]
    calls: list[list[str]] = []
    original_runner = support.docker_runner(runtime_path)

    def monotonic() -> float:
        return now[0]

    def runner(arguments, *, cwd, env, timeout):
        calls.append([*arguments])
        completed = original_runner(
            arguments,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
        now[0] = 203.0
        return completed

    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(monotonic=monotonic),
        raising=False,
    )

    with pytest.raises(ValueError, match="deadline|budget|timeout"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=3,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=runner,
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert len(calls) == 1
    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()
    assert not list((candidate_root / "artifacts").glob(".gateway-only-public-*.tmp"))
    assert not list((candidate_root / "runtime").glob(".gateway-only-public-*.tmp"))
    assert (report_path.read_bytes(), attestation_path.read_bytes()) == immutable_inputs


@pytest.mark.parametrize(
    ("drift", "message"),
    (
        ("internal-service-port", "published ports"),
        ("daemon-endpoint", "daemon"),
        ("server-id", "daemon"),
        ("network-mode", "network mode"),
    ),
)
def test_gateway_only_public_rejects_docker_drift_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        report_path,
        attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    immutable_inputs = (report_path.read_bytes(), attestation_path.read_bytes())

    with pytest.raises(ValueError, match=message):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=support.docker_runner(runtime_path, drift=drift),
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()
    assert (report_path.read_bytes(), attestation_path.read_bytes()) == immutable_inputs


@pytest.mark.parametrize("input_name", ("report", "attestation"))
def test_gateway_only_public_rejects_symlinked_external_input_before_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_name: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        _runtime_path,
        report_path,
        attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    path = report_path if input_name == "report" else attestation_path
    real_path = path.with_name(f"{path.name}.real")
    path.replace(real_path)
    try:
        path.symlink_to(real_path)
    except OSError:
        pytest.skip("symlinks are unavailable on this Windows test host")

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("gateway writer must reject symlinked input before Docker")

    with pytest.raises(ValueError, match="symlink|reparse|no-follow"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=forbidden_runner,
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()


def test_gateway_only_public_rejects_symlinked_input_ancestor_before_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        _runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    raw_root = candidate_root / "raw"
    real_root = candidate_root / "raw.real"
    raw_root.replace(real_root)
    try:
        raw_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows test host")

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("gateway writer must reject a symlinked ancestor before Docker")

    with pytest.raises(ValueError, match="symlink|reparse|no-follow"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=forbidden_runner,
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )


@pytest.mark.parametrize(
    "drifted_input",
    ("external-observation", "observer-attestation", "candidate", "runtime"),
)
def test_gateway_only_public_rejects_input_or_candidate_runtime_mid_run_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drifted_input: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        report_path,
        attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    paths = {
        "external-observation": report_path,
        "observer-attestation": attestation_path,
        "candidate": candidate_root / "deploy" / "image-lock.json",
        "runtime": runtime_path,
    }
    drift_path = paths[drifted_input]
    original_runner = support.docker_runner(runtime_path)
    drifted = False

    def runner(arguments, *, cwd, env, timeout):
        nonlocal drifted
        if not drifted:
            drifted = True
            replacement = drift_path.with_name(f".{drift_path.name}.swap")
            replacement.write_bytes(drift_path.read_bytes() + b" ")
            replacement.replace(drift_path)
        return original_runner(arguments, cwd=cwd, env=env, timeout=timeout)

    with pytest.raises(ValueError, match="changed|identity|digest"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=runner,
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()


def test_gateway_only_public_rejects_byte_identical_input_ancestor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    raw_root = candidate_root / "raw"
    retained_root = candidate_root / "raw.retained"
    alternate_root = candidate_root / "raw.alternate"
    alternate_root.mkdir()
    os.link(report_path, alternate_root / report_path.name)
    original_runner = support.docker_runner(runtime_path)
    replaced = False

    def runner(arguments, *, cwd, env, timeout):
        nonlocal replaced
        if not replaced:
            os.replace(raw_root, retained_root)
            os.replace(alternate_root, raw_root)
            replaced = True
        return original_runner(arguments, cwd=cwd, env=env, timeout=timeout)

    with pytest.raises(ValueError, match="ancestor|boundary|changed"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=runner,
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert replaced is True
    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()


def test_gateway_only_public_rejects_publication_ancestor_replacement_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    artifacts_root = candidate_root / "artifacts"
    artifacts_root.mkdir(exist_ok=True)
    retained_root = candidate_root / "artifacts.retained"
    alternate_root = candidate_root / "artifacts.alternate"
    alternate_root.mkdir()
    original_stage = module._stage_gateway_json
    replaced = False

    def stage(parent: Path, document):
        nonlocal replaced
        if Path(parent) == artifacts_root and not replaced:
            os.replace(artifacts_root, retained_root)
            os.replace(alternate_root, artifacts_root)
            replaced = True
        return original_stage(parent, document)

    monkeypatch.setattr(module, "_stage_gateway_json", stage)

    with pytest.raises(ValueError, match="ancestor|boundary|publication|changed"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=support.docker_runner(runtime_path),
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert replaced is True
    assert not (artifacts_root / "gateway_only_public.json").exists()
    assert not (retained_root / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="exercises a Windows directory reparse race")
def test_gateway_only_public_rejects_published_parent_reparse_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    artifacts_root = candidate_root / "artifacts"
    artifacts_root.mkdir(exist_ok=True)
    retained_root = candidate_root / "artifacts.retained"
    redirected_root = candidate_root / "artifacts.redirected"
    original_assert_published = module._assert_gateway_published
    attack_outcome: str | None = None

    def assert_published(path: Path, *, body: bytes, identity: tuple[int, int]) -> None:
        nonlocal attack_outcome
        original_assert_published(path, body=body, identity=identity)
        if Path(path).parent != artifacts_root or attack_outcome is not None:
            return
        redirected_root.mkdir()
        os.link(path, redirected_root / Path(path).name)
        try:
            os.replace(artifacts_root, retained_root)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32}:
                raise
            attack_outcome = "permission-blocked"
            raise ValueError(
                "gateway publication ancestor replacement was blocked by the held lease"
            ) from exc
        try:
            artifacts_root.symlink_to(redirected_root, target_is_directory=True)
        except OSError:
            os.replace(retained_root, artifacts_root)
            pytest.skip("directory reparse points are unavailable on this Windows test host")
        attack_outcome = "identity-rejected"

    monkeypatch.setattr(module, "_assert_gateway_published", assert_published)

    with pytest.raises(
        ValueError,
        match="reparse|ancestor|boundary|publication|changed|blocked|lease",
    ):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=support.docker_runner(runtime_path),
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert attack_outcome in {"permission-blocked", "identity-rejected"}
    if attack_outcome == "permission-blocked":
        assert artifacts_root.is_dir()
        assert not artifacts_root.is_symlink()
        assert not retained_root.exists()


def test_gateway_only_public_baseexception_cleans_owned_staging_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    staged_paths: list[Path] = []
    decoy = candidate_root / "runtime" / ".gateway-only-public-decoy.tmp"
    decoy.write_bytes(b"unowned-sentinel")

    class InjectedAbort(BaseException):
        pass

    def abort_publish(source: Path, _target: Path) -> None:
        staged_paths.append(source)
        raise InjectedAbort("injected gateway publication abort")

    monkeypatch.setattr(module, "_publish_no_replace", abort_publish)

    with pytest.raises(InjectedAbort, match="injected gateway publication abort"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=support.docker_runner(runtime_path),
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert staged_paths
    assert all(not path.exists() for path in staged_paths)
    assert decoy.read_bytes() == b"unowned-sentinel"
    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()


def test_gateway_only_public_proof_failure_retracts_partial_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        report_path,
        attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    immutable_inputs = (report_path.read_bytes(), attestation_path.read_bytes())
    original_publish = module._publish_no_replace
    calls = 0

    def publish(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected proof publication failure")
        original_publish(source, target)

    monkeypatch.setattr(module, "_publish_no_replace", publish)

    with pytest.raises(OSError, match="injected proof publication failure"):
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=support.docker_runner(runtime_path),
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert not (candidate_root / "artifacts" / "gateway_only_public.json").exists()
    assert not (candidate_root / "runtime" / "gateway-only-public-attestation.json").exists()
    assert (report_path.read_bytes(), attestation_path.read_bytes()) == immutable_inputs


@pytest.mark.parametrize(
    "cleanup_failure",
    ("staging-unlink", "staging-handle-close", "directory-lease-close"),
)
def test_gateway_only_public_postcommit_cleanup_failure_preserves_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    receipt_path = candidate_root / "artifacts" / "gateway_only_public.json"
    proof_path = candidate_root / "runtime" / "gateway-only-public-attestation.json"
    injected = False

    def assert_formal_commit() -> None:
        assert receipt_path.is_file()
        assert proof_path.is_file()

    if cleanup_failure == "staging-unlink":
        original_remove = module._remove_classroom_entries

        def remove(boundary, entries, *, label: str) -> None:
            nonlocal injected
            original_remove(boundary, entries, label=label)
            if label == "gateway staging evidence":
                assert_formal_commit()
                injected = True
                raise OSError("injected postcommit staging unlink failure")

        monkeypatch.setattr(module, "_remove_classroom_entries", remove)
    elif cleanup_failure == "staging-handle-close":
        original_open = module._open_gateway_staged_input

        class CloseFailureHandle:
            def __init__(self, handle) -> None:
                self._handle = handle

            def fileno(self) -> int:
                return self._handle.fileno()

            def close(self) -> None:
                nonlocal injected
                self._handle.close()
                if not injected:
                    assert_formal_commit()
                    injected = True
                    raise OSError("injected postcommit staging handle close failure")

        def open_staged(boundary, path: Path, *, body: bytes, identity: tuple[int, int]):
            return CloseFailureHandle(
                original_open(
                    boundary,
                    path,
                    body=body,
                    identity=identity,
                )
            )

        monkeypatch.setattr(module, "_open_gateway_staged_input", open_staged)
    else:
        original_close = module._GatewayPublicationBoundary.close

        def close(boundary, *, suppress_errors: bool = False) -> None:
            nonlocal injected
            original_close(boundary, suppress_errors=suppress_errors)
            if not suppress_errors:
                assert_formal_commit()
                injected = True
                raise OSError("injected postcommit directory lease close failure")

        monkeypatch.setattr(module._GatewayPublicationBoundary, "close", close)

    receipt = module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(runtime_path),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    assert injected
    assert json.loads(receipt_path.read_bytes()) == receipt
    assert (
        json.loads(proof_path.read_bytes())["summary"]["checks"]
        == receipt["receipt"]["result"]["checks"]
    )
    assert not list((candidate_root / "artifacts").glob(".gateway-only-public-*.tmp"))
    assert not list((candidate_root / "runtime").glob(".gateway-only-public-*.tmp"))


def test_gateway_only_public_precommit_failure_remains_primary_when_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    receipt_path = candidate_root / "artifacts" / "gateway_only_public.json"
    proof_path = candidate_root / "runtime" / "gateway-only-public-attestation.json"
    original_publish = module._publish_no_replace
    original_remove = module._remove_classroom_entries
    publish_calls = 0
    cleanup_injected = False

    class InjectedAbort(BaseException):
        pass

    def publish(source: Path, target: Path) -> None:
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 2:
            raise InjectedAbort("injected precommit proof publication abort")
        original_publish(source, target)

    def remove(boundary, entries, *, label: str) -> None:
        nonlocal cleanup_injected
        original_remove(boundary, entries, label=label)
        if label == "gateway formal evidence":
            cleanup_injected = True
            raise OSError("injected precommit formal cleanup failure")

    monkeypatch.setattr(module, "_publish_no_replace", publish)
    monkeypatch.setattr(module, "_remove_classroom_entries", remove)

    with pytest.raises(
        InjectedAbort,
        match="injected precommit proof publication abort",
    ) as caught:
        module.write_gateway_only_public_receipt(
            candidate_root=candidate_root,
            bundle_root=candidate_root,
            release_run=RELEASE_RUN,
            timeout_seconds=30,
            expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
            **support.gateway_trust_arguments(trust_pair),
            runner=support.docker_runner(runtime_path),
            docker_resolver=lambda: Path("C:/fixed/docker.exe"),
        )

    assert cleanup_injected
    assert any(
        "gateway formal evidence cleanup failed" in note
        and "injected precommit formal cleanup failure" in note
        for note in getattr(caught.value, "__notes__", ())
    )
    assert not receipt_path.exists()
    assert not proof_path.exists()
    assert not list((candidate_root / "artifacts").glob(".gateway-only-public-*.tmp"))
    assert not list((candidate_root / "runtime").glob(".gateway-only-public-*.tmp"))


def test_release_evidence_cli_forwards_trusted_gateway_docker_host_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        _runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def write_receipt(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(
        module,
        "write_gateway_only_public_receipt",
        write_receipt,
        raising=False,
    )
    bundle_root = candidate_root

    assert (
        module.main(
            [
                "gateway-only-public",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--timeout-seconds",
                "30",
                "--docker-host-identity-sha256",
                str(trust_pair["host_receipt_sha256"]),
                "--gateway-trust-keyring",
                str(trust_pair["keyring_path"]),
                "--gateway-trust-keyring-sha256",
                str(trust_pair["keyring_sha256"]),
                "--gateway-observer-challenge",
                str(trust_pair["observer_challenge"]),
                "--gateway-host-challenge",
                str(trust_pair["host_challenge"]),
                "--gateway-trusted-now",
                str(trust_pair["trusted_now"]),
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "timeout_seconds": 30,
        "expected_docker_host_identity_sha256": trust_pair["host_receipt_sha256"],
        **support.gateway_trust_arguments(trust_pair),
    }
    assert capsys.readouterr().out == (
        f"{bundle_root / 'runtime' / 'gateway-only-public-attestation.json'}\n"
    )


def test_gateway_only_public_cli_forwards_external_trust_pair_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        _runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def write_receipt(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(module, "write_gateway_only_public_receipt", write_receipt)
    bundle_root = candidate_root

    assert (
        module.main(
            [
                "gateway-only-public",
                "--candidate-root",
                str(candidate_root),
                "--bundle-root",
                str(bundle_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--timeout-seconds",
                "30",
                "--docker-host-identity-sha256",
                str(trust_pair["host_receipt_sha256"]),
                "--gateway-trust-keyring",
                str(trust_pair["keyring_path"]),
                "--gateway-trust-keyring-sha256",
                str(trust_pair["keyring_sha256"]),
                "--gateway-observer-challenge",
                str(trust_pair["observer_challenge"]),
                "--gateway-host-challenge",
                str(trust_pair["host_challenge"]),
                "--gateway-trusted-now",
                str(trust_pair["trusted_now"]),
            ]
        )
        == 0
    )
    assert captured == {
        "candidate_root": candidate_root,
        "bundle_root": bundle_root,
        "release_run": RELEASE_RUN,
        "timeout_seconds": 30,
        "expected_docker_host_identity_sha256": trust_pair["host_receipt_sha256"],
        **support.gateway_trust_arguments(trust_pair),
    }
    assert capsys.readouterr().out == (
        f"{bundle_root / 'runtime' / 'gateway-only-public-attestation.json'}\n"
    )


def test_release_evidence_cli_assembles_gateway_receipt_with_trusted_docker_host_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_fixture(tmp_path, monkeypatch)
    receipt_path = candidate_root / "artifacts" / "gateway_only_public.json"
    output = candidate_root / "release-evidence.json"
    module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(runtime_path),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )

    assert (
        module.main(
            [
                "assemble",
                "--output",
                str(output),
                "--candidate-root",
                str(candidate_root),
                "--run-id",
                RELEASE_RUN["runId"],
                "--environment-id",
                RELEASE_RUN["environmentId"],
                "--receipt",
                f"gateway_only_public={receipt_path}",
                "--gateway-docker-host-identity-sha256",
                str(trust_pair["host_receipt_sha256"]),
                "--gateway-trust-keyring",
                str(trust_pair["keyring_path"]),
                "--gateway-trust-keyring-sha256",
                str(trust_pair["keyring_sha256"]),
                "--gateway-observer-challenge",
                str(trust_pair["observer_challenge"]),
                "--gateway-host-challenge",
                str(trust_pair["host_challenge"]),
                "--gateway-trusted-now",
                str(trust_pair["trusted_now"]),
            ]
        )
        == 0
    )
    manifest = json.loads(output.read_bytes())
    assert manifest["evidence"]["gateway_only_public"] == {
        "status": "pass",
        "detail": "gateway_only_public verified by gateway-external-probe",
        "artifact": "artifacts/gateway_only_public.json",
        "artifactSha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    }
    assert capsys.readouterr().out == f"{output}\n"


def _tamper_published_gateway_trust_input(
    support,
    candidate_root: Path,
    receipt_path: Path,
    trust_pair: dict[str, object],
    trust_case: str,
) -> None:
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
    path = Path(trust_pair[path_keys[trust_case]])
    path.write_bytes(path.read_bytes() + b" ")
    if trust_case == "keyring":
        return

    proof_path = candidate_root / "runtime" / "gateway-only-public-attestation.json"
    proof = json.loads(proof_path.read_bytes())
    proof["trustPair"][reference_keys[trust_case]]["sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    proof_body = support.canonical_json(proof)
    proof_path.write_bytes(proof_body)
    receipt = json.loads(receipt_path.read_bytes())
    receipt["provenance"]["gatewayOnlyPublicAttestation"]["sha256"] = hashlib.sha256(
        proof_body
    ).hexdigest()
    receipt_path.write_bytes(support.canonical_json(receipt))


@pytest.mark.parametrize(
    "trust_case",
    (
        "valid",
        "observer-envelope",
        "host-envelope",
        "host-receipt",
        "keyring",
    ),
)
def test_manifest_assembler_replays_gateway_external_trust_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trust_case: str,
) -> None:
    (
        module,
        _contract,
        support,
        candidate_root,
        runtime_path,
        _report_path,
        _attestation_path,
        trust_pair,
    ) = _gateway_external_trust_fixture(tmp_path, monkeypatch)
    receipt_path = candidate_root / "artifacts" / "gateway_only_public.json"
    output = candidate_root / "release-evidence.json"
    module.write_gateway_only_public_receipt(
        candidate_root=candidate_root,
        bundle_root=candidate_root,
        release_run=RELEASE_RUN,
        timeout_seconds=30,
        expected_docker_host_identity_sha256=str(trust_pair["host_receipt_sha256"]),
        **support.gateway_trust_arguments(trust_pair),
        runner=support.docker_runner(runtime_path),
        docker_resolver=lambda: Path("C:/fixed/docker.exe"),
    )
    if trust_case != "valid":
        _tamper_published_gateway_trust_input(
            support,
            candidate_root,
            receipt_path,
            trust_pair,
            trust_case,
        )

    arguments = [
        "assemble",
        "--output",
        str(output),
        "--candidate-root",
        str(candidate_root),
        "--run-id",
        RELEASE_RUN["runId"],
        "--environment-id",
        RELEASE_RUN["environmentId"],
        "--receipt",
        f"gateway_only_public={receipt_path}",
        "--gateway-docker-host-identity-sha256",
        str(trust_pair["host_receipt_sha256"]),
        "--gateway-trust-keyring",
        str(trust_pair["keyring_path"]),
        "--gateway-trust-keyring-sha256",
        str(trust_pair["keyring_sha256"]),
        "--gateway-observer-challenge",
        str(trust_pair["observer_challenge"]),
        "--gateway-host-challenge",
        str(trust_pair["host_challenge"]),
        "--gateway-trusted-now",
        str(trust_pair["trusted_now"]),
    ]
    if trust_case == "valid":
        assert module.main(arguments) == 0
        manifest = json.loads(output.read_bytes())
        assert manifest["evidence"]["gateway_only_public"] == {
            "status": "pass",
            "detail": "gateway_only_public verified by gateway-external-probe",
            "artifact": "artifacts/gateway_only_public.json",
            "artifactSha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        }
    else:
        try:
            with pytest.raises(ValueError, match="gateway trust"):
                module.main(arguments)
        finally:
            assert not output.exists()
