from __future__ import annotations

import copy
from functools import cache
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://candidate.example.test"
RUNTIME_ATTESTATION_SHA256 = "a" * 64
LIVE_FIXTURE_TOKEN = "platform-admin-token-must-never-appear"


@cache
def _module():
    path = ROOT / "scripts" / "openmaic_smoke_contract.py"
    assert path.is_file(), "OpenMAIC smoke contract is missing"
    spec = importlib.util.spec_from_file_location("openmaic_smoke_contract_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate() -> dict[str, object]:
    return {
        "sourceRepository": "xinlingzhifei/DeepTutor",
        "sourceHead": "c" * 40,
        "releaseTag": "yfeistai-first-release-20260829-cccccccc",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": "sha256:" + "d" * 64,
            "openmaic": "sha256:" + "e" * 64,
            "openmaic_render": "sha256:" + "f" * 64,
        },
    }


def _release_run() -> dict[str, str]:
    return {
        "runId": "run-openmaic-shared-plane",
        "environmentId": "environment-openmaic-shared-plane",
    }


def _report(
    *,
    candidate: dict[str, object] | None = None,
    release_run: dict[str, str] | None = None,
    runtime_attestation_sha256: str = RUNTIME_ATTESTATION_SHA256,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "producer": "openmaic-smoke",
        "plane": "shared",
        "candidate": candidate or _candidate(),
        "releaseRun": release_run or _release_run(),
        "observedAt": "2026-08-29T00:00:00Z",
        "baseUrl": BASE_URL,
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


def _body(report: dict[str, object]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _parse(report: dict[str, object]) -> dict[str, object]:
    return _module().parse_openmaic_smoke_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url=BASE_URL,
        expected_runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        forbidden_secret_values=(LIVE_FIXTURE_TOKEN.encode("utf-8"),),
    )


def test_shared_report_requires_successful_materialized_generation_on_canonical_shared_binding() -> (
    None
):
    module = _module()
    report = _report()

    assert module.OPENMAIC_SMOKE_PRODUCER == "openmaic-smoke"
    assert module.openmaic_shared_plane_command_record() == {
        "runner": "python",
        "script": "scripts/openmaic_smoke_probe.py",
        "arguments": ["--plane", "shared", "--profile", "first-release"],
    }
    assert module.canonical_openmaic_smoke_report(report) == _body(report)
    assert _parse(report) == report
    assert module.derive_openmaic_shared_plane_checks(_parse(report)) == {
        "sharedGenerationPassed": True
    }

    invalid_reports: list[dict[str, object]] = []
    for path, value in (
        (("candidate", "sourceHead"), "0" * 40),
        (("releaseRun", "runId"), "another-run"),
        (("baseUrl",), "https://other.example.test"),
        (("runtimeAttestation", "sha256"), "0" * 64),
        (("fixture", "tenantId"), ""),
        (("fixture", "teacherUserId"), LIVE_FIXTURE_TOKEN),
        (("binding", "routeId"), "dedicated-tenant-a"),
        (("binding", "providerProfileId"), "provider-tenant-a"),
        (("binding", "workerPoolRef"), "generation-tenant-a"),
        (("binding", "queueRef"), "openmaic.tenant-a"),
        (("generation", "jobStatus"), "failed"),
        (("generation", "classroomStatus"), "generating_content"),
        (("generation", "classroomVersionId"), ""),
        (("generation", "documentEtag"), '"sha256-' + "c" * 64 + '"'),
        (("generation", "assetId"), LIVE_FIXTURE_TOKEN),
    ):
        changed = copy.deepcopy(report)
        target = changed
        for key in path[:-1]:
            nested = target[key]
            assert isinstance(nested, dict)
            target = nested
        target[path[-1]] = value
        invalid_reports.append(changed)

    for invalid in invalid_reports:
        with pytest.raises(ValueError):
            _parse(invalid)
