from __future__ import annotations

import copy
from functools import cache
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://candidate.example.test"
RUNTIME_ATTESTATION_SHA256 = "a" * 64
LIVE_FIXTURE_TOKEN = "platform-admin-token-must-never-appear"
OBSERVER_ATTESTATION_SHA256 = "9" * 64
OBSERVER_ID = "shared-ingress-observer-openmaic-01"
OBSERVER_URL = "https://observer.example.test"
SHARED_CONTROL_URL = "https://shared-ingress.example.test/v1/control-canaries"
DOCKER_HOST_IDENTITY_SHA256 = "1" * 64


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


def _observer_attestation() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "producer": "openmaic-shared-ingress-observer",
        "releaseRun": _release_run(),
        "observedAt": "2026-08-29T00:00:00Z",
        "observer": {
            "observerId": OBSERVER_ID,
            "observerUrl": OBSERVER_URL,
            "sharedIngressControlUrl": SHARED_CONTROL_URL,
        },
    }


def _parse(report: dict[str, object]) -> dict[str, object]:
    return _module().parse_openmaic_smoke_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url=BASE_URL,
        expected_runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        forbidden_secret_values=(LIVE_FIXTURE_TOKEN.encode("utf-8"),),
    )


def _dedicated_report() -> dict[str, object]:
    report = _report()
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


def _parse_dedicated(report: dict[str, object]) -> dict[str, object]:
    return _module().parse_openmaic_smoke_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url=BASE_URL,
        expected_runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        forbidden_secret_values=(LIVE_FIXTURE_TOKEN.encode("utf-8"),),
        expected_plane="dedicated",
    )


def _dedicated_outage_attestation(
    *,
    candidate: dict[str, object] | None = None,
    release_run: dict[str, str] | None = None,
    runtime_attestation_sha256: str = RUNTIME_ATTESTATION_SHA256,
) -> dict[str, object]:
    tenant_id = "tenant-openmaic-dedicated-01"
    route_id = "dedicated-tenant-openmaic-01"
    bound_candidate = candidate or _candidate()
    bound_release_run = release_run or _release_run()
    marker = _dedicated_outage_attempt_marker(
        candidate=bound_candidate,
        release_run=bound_release_run,
    )
    marker_body = _body(marker)
    marker_reference = {
        "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
        "sha256": hashlib.sha256(marker_body).hexdigest(),
    }
    report = {
        "schemaVersion": 1,
        "producer": "openmaic-dedicated-outage",
        "candidate": bound_candidate,
        "releaseRun": bound_release_run,
        "observedAt": "2026-08-29T00:00:01Z",
        "baseUrl": BASE_URL,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_attestation_sha256,
        },
        "observerAttestation": {
            "artifact": "runtime/openmaic-shared-ingress-observer-attestation.json",
            "sha256": OBSERVER_ATTESTATION_SHA256,
            "observerId": OBSERVER_ID,
            "observerOrigin": "https://observer.example.test",
            "sharedIngressControlOrigin": "https://shared-ingress.example.test",
        },
        "fixture": {
            "tenantId": tenant_id,
            "attemptMarker": marker_reference,
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
            "attemptMarker": marker_reference,
            "observerTrustAnchor": dict(marker["observerTrustAnchor"]),
            "dockerBoundary": {
                "dockerHostIdentitySha256": DOCKER_HOST_IDENTITY_SHA256,
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
    child_stdout = _body(report)
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


def _dedicated_outage_attempt_marker(
    *,
    candidate: dict[str, object] | None = None,
    release_run: dict[str, str] | None = None,
    observer_sha256: str = OBSERVER_ATTESTATION_SHA256,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "producer": "openmaic-dedicated-outage-attempt",
        "candidate": candidate or _candidate(),
        "releaseRun": release_run or _release_run(),
        "observerTrustAnchor": {
            "sha256": observer_sha256,
            "observerId": OBSERVER_ID,
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


def test_dedicated_report_requires_successful_generation_on_candidate_bound_tenant_route() -> None:
    module = _module()
    report = _dedicated_report()

    assert module.openmaic_dedicated_plane_command_record() == {
        "runner": "python",
        "script": "scripts/openmaic_smoke_probe.py",
        "arguments": ["--plane", "dedicated", "--profile", "first-release"],
    }
    assert _parse_dedicated(report) == report
    assert module.derive_openmaic_dedicated_plane_checks(_parse_dedicated(report)) == {
        "dedicatedGenerationPassed": True,
        "noSharedClientIssued": True,
    }
    assert "noSharedFallback" not in module.derive_openmaic_dedicated_plane_checks(
        _parse_dedicated(report)
    )

    invalid_reports: list[dict[str, object]] = []
    for path, value in (
        (("plane",), "shared"),
        (("fixture", "tenantId"), "tenant-other"),
        (("binding", "routeTenantId"), "tenant-other"),
        (("binding", "routeOwnerKey"), "tenant-other"),
        (("binding", "providerScope"), "shared"),
        (("binding", "providerTenantId"), "tenant-other"),
        (("binding", "providerOwnerKey"), "tenant-other"),
        (("binding", "routeId"), "shared-primary"),
        (("binding", "providerProfileId"), "platform-default"),
        (("binding", "attemptCount"), 0),
        (("binding", "sharedRouteAttemptCount"), 1),
        (("binding", "dedicatedRouteAttemptCount"), 1),
        (("binding", "selectedRouteAttemptCount"), 2),
        (("binding", "unavailableRouteAttemptCount"), 2),
        (("binding", "routeAttemptHistoryComplete"), False),
        (("generation", "jobStatus"), "failed"),
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
            _parse_dedicated(invalid)


def test_shared_ingress_observer_attestation_is_canonical_and_candidate_independent() -> None:
    module = _module()
    attestation = _observer_attestation()
    body = module.canonical_openmaic_shared_ingress_observer_attestation(attestation)

    assert (
        module.parse_openmaic_shared_ingress_observer_attestation(
            body,
            release_run=_release_run(),
        )
        == attestation
    )
    assert "candidate" not in attestation

    for path, value in (
        (("producer",), "openmaic-dedicated-outage"),
        (("releaseRun", "environmentId"), "foreign-environment"),
        (("observer", "observerId"), ""),
        (("observer", "observerUrl"), "https://shared-ingress.example.test"),
        (("observer", "sharedIngressControlUrl"), OBSERVER_URL + "/control"),
    ):
        changed = copy.deepcopy(attestation)
        target = changed
        for key in path[:-1]:
            nested = target[key]
            assert isinstance(nested, dict)
            target = nested
        target[path[-1]] = value
        with pytest.raises(ValueError):
            module.parse_openmaic_shared_ingress_observer_attestation(
                module.canonical_openmaic_shared_ingress_observer_attestation(changed),
                release_run=_release_run(),
            )


def test_dedicated_outage_attestation_independently_proves_no_shared_fallback() -> None:
    module = _module()
    attestation = _dedicated_outage_attestation()
    body = module.canonical_openmaic_dedicated_outage_attestation(attestation)
    marker_body = _body(_dedicated_outage_attempt_marker())

    parsed = module.parse_openmaic_dedicated_outage_attestation(
        body,
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url=BASE_URL,
        expected_runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        expected_observer_attestation_sha256=OBSERVER_ATTESTATION_SHA256,
        expected_observer_id=OBSERVER_ID,
        expected_observer_origin="https://observer.example.test",
        expected_shared_ingress_control_origin="https://shared-ingress.example.test",
        expected_tenant_id="tenant-openmaic-dedicated-01",
        attempt_marker_body=marker_body,
        expected_docker_host_identity_sha256=DOCKER_HOST_IDENTITY_SHA256,
    )

    assert parsed == attestation
    assert module.derive_openmaic_dedicated_outage_checks(parsed) == {"noSharedFallback": True}

    for path, value in (
        (("outage", "dedicatedPlaneStopped"), False),
        (("outage", "sharedRouteAttemptCount"), 1),
        (("outage", "selectedRouteAttemptCount"), 1),
        (("sharedIngress", "requestCountAfter"), 8),
        (("restoration", "dedicatedPlaneRestored"), False),
        (("restoration", "sharedRouteAttemptCount"), 1),
        (("restoration", "canaryJobStatus"), "failed"),
    ):
        changed = copy.deepcopy(attestation)
        target = changed
        for key in path[:-1]:
            nested = target[key]
            assert isinstance(nested, dict)
            target = nested
        target[path[-1]] = value
        with pytest.raises(ValueError):
            module.parse_openmaic_dedicated_outage_attestation(
                module.canonical_openmaic_dedicated_outage_attestation(changed),
                candidate=_candidate(),
                release_run=_release_run(),
                expected_base_url=BASE_URL,
                expected_runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
                expected_observer_attestation_sha256=OBSERVER_ATTESTATION_SHA256,
                expected_observer_id=OBSERVER_ID,
                expected_observer_origin="https://observer.example.test",
                expected_shared_ingress_control_origin=("https://shared-ingress.example.test"),
                expected_tenant_id="tenant-openmaic-dedicated-01",
                attempt_marker_body=marker_body,
                expected_docker_host_identity_sha256=DOCKER_HOST_IDENTITY_SHA256,
            )


def test_dedicated_outage_attempt_marker_replays_candidate_run_and_observer_anchor() -> None:
    module = _module()
    marker = _dedicated_outage_attempt_marker()
    body = module.canonical_openmaic_dedicated_outage_attempt_marker(marker)

    assert (
        module.parse_openmaic_dedicated_outage_attempt_marker(
            body,
            candidate=_candidate(),
            release_run=_release_run(),
            expected_observer_attestation_sha256=OBSERVER_ATTESTATION_SHA256,
            expected_observer_id=OBSERVER_ID,
            expected_observer_origin="https://observer.example.test",
            expected_shared_ingress_control_origin="https://shared-ingress.example.test",
            expected_tenant_id="tenant-openmaic-dedicated-01",
            expected_route_id="dedicated-tenant-openmaic-01",
        )
        == marker
    )

    for path, value in (
        (("candidate", "sourceCommit"), "f" * 40),
        (("releaseRun", "runId"), "other-run"),
        (("observerTrustAnchor", "sha256"), "8" * 64),
        (("observerTrustAnchor", "observerId"), "foreign-observer"),
        (("observerTrustAnchor", "observerOrigin"), "https://foreign.example.test"),
    ):
        changed = copy.deepcopy(marker)
        target = changed
        for key in path[:-1]:
            nested = target[key]
            assert isinstance(nested, dict)
            target = nested
        target[path[-1]] = value
        with pytest.raises(ValueError):
            module.parse_openmaic_dedicated_outage_attempt_marker(
                module.canonical_openmaic_dedicated_outage_attempt_marker(changed),
                candidate=_candidate(),
                release_run=_release_run(),
                expected_observer_attestation_sha256=OBSERVER_ATTESTATION_SHA256,
                expected_observer_id=OBSERVER_ID,
                expected_observer_origin="https://observer.example.test",
                expected_shared_ingress_control_origin="https://shared-ingress.example.test",
                expected_tenant_id="tenant-openmaic-dedicated-01",
                expected_route_id="dedicated-tenant-openmaic-01",
            )


def test_dedicated_outage_receipt_replays_child_stdio_marker_and_boundary_provenance() -> None:
    module = _module()
    marker_body = (
        json.dumps(
            _dedicated_outage_attempt_marker(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    report = _dedicated_outage_attestation()
    report.pop("execution")
    report["fixture"]["attemptMarker"]["sha256"] = hashlib.sha256(marker_body).hexdigest()
    report["provenance"] = {
        "attemptMarker": dict(report["fixture"]["attemptMarker"]),
        "observerTrustAnchor": dict(_dedicated_outage_attempt_marker()["observerTrustAnchor"]),
        "dockerBoundary": {
            "dockerHostIdentitySha256": "1" * 64,
            "daemonIdentityBeforeSha256": "2" * 64,
            "daemonIdentityAfterSha256": "2" * 64,
            "inventoryBeforeSha256": "3" * 64,
            "inventoryAfterSha256": "3" * 64,
        },
    }
    child_stdout = module.canonical_openmaic_dedicated_outage_attestation(report)
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
    body = module.canonical_openmaic_dedicated_outage_attestation(report)

    parsed = module.parse_openmaic_dedicated_outage_attestation(
        body,
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url=BASE_URL,
        expected_runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        expected_observer_attestation_sha256=OBSERVER_ATTESTATION_SHA256,
        expected_observer_id=OBSERVER_ID,
        expected_observer_origin="https://observer.example.test",
        expected_shared_ingress_control_origin="https://shared-ingress.example.test",
        expected_tenant_id="tenant-openmaic-dedicated-01",
        attempt_marker_body=marker_body,
        expected_docker_host_identity_sha256=DOCKER_HOST_IDENTITY_SHA256,
    )

    assert parsed == report
