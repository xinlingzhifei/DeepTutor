from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
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
    _write_runtime_attestation(candidate_root, lock)
    return candidate_root, lock["candidate"]


def _write_runtime_attestation(candidate_root: Path, lock: dict[str, object]) -> Path:
    images = lock["images"]
    assert isinstance(images, dict)
    references = {
        name: record["reference"] for name, record in images.items() if isinstance(record, dict)
    }
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
    one_shots = {
        "minio-bootstrap",
        "teaching-migrate",
        "shared-data-plane-bootstrap",
    }
    healthy = {"deeptutor", "postgres", "minio", "openmaic", "openmaic-render"}

    def repo_digest(reference: str) -> str:
        tagged, digest = reference.rsplit("@", 1)
        return f"{tagged.rsplit(':', 1)[0]}@{digest}"

    containers: list[dict[str, object]] = []
    for service in sorted(service_images):
        one_shot = service in one_shots
        reference = service_images[service]
        image_id = "sha256:local-" + hashlib.sha256(reference.encode()).hexdigest()
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
        '"state":{{json .State.Status}},"running":{{json .State.Running}},'
        '"restarting":{{json .State.Restarting}},"exitCode":{{json .State.ExitCode}},'
        '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}}'
    )
    image_format = '{"imageId":{{json .Id}},"repoDigests":{{json .RepoDigests}}}'
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

    container_records = [
        command(
            ["container", "inspect", "--format", container_format, str(container["containerId"])],
            json.dumps(
                {
                    name: container[name]
                    for name in (
                        "containerId",
                        "localImageId",
                        "configImage",
                        "project",
                        "service",
                        "state",
                        "running",
                        "restarting",
                        "exitCode",
                        "health",
                    )
                }
            ),
        )
        for container in containers_by_id
    ]
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
                    }
                ),
            )
        )
    ps_record = command(ps, ps_stdout)
    report = {
        "schemaVersion": 1,
        "candidate": lock["candidate"],
        "releaseRun": RELEASE_RUN,
        "observedAt": "2026-08-25T00:00:00Z",
        "baseUrl": BASE_URL,
        "project": "yfeistai-platform",
        "beforeSnapshot": snapshot,
        "afterSnapshot": snapshot,
        "containers": containers,
        "commands": [
            ps_record,
            *container_records,
            *image_records,
            ps_record,
            *container_records,
        ],
    }
    path = candidate_root / "runtime" / "runtime-attestation.json"
    path.parent.mkdir()
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


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
    assert list((candidate_root / "failures" / "teacher_flow").glob("*/raw.json"))


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


def test_failed_probe_preserves_diagnostics_and_canonical_paths_are_retryable(
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
    failure_raw = list((candidate_root / "failures" / "teacher_flow").glob("*/raw.json"))
    assert len(failure_raw) == 1
    assert failure_raw[0].read_bytes() == b"native failure diagnostics"
    assert list((candidate_root / "failures" / "teacher_flow").glob("*/failure.json"))

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
    assert failure_raw[0].exists()


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
    assert list((candidate_root / "failures" / "teacher_flow").glob("*/raw.json"))


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
    assert list(failure_root.glob("*/raw.json"))
    assert list(failure_root.glob("*/execution.json"))

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
