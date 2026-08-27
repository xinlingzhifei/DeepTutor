from __future__ import annotations

from functools import cache
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
METRICS = (
    "core_api",
    "event_ingest",
    "job_submission_visible",
    "mastery_projection_visible",
)
SESSION_METRICS = ("core_api", "event_ingest", "mastery_projection_visible")


@cache
def _module():
    path = ROOT / "scripts" / "capacity_profile_contract.py"
    spec = importlib.util.spec_from_file_location("capacity_profile_contract_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate() -> dict[str, object]:
    return {
        "sourceRepository": "xinlingzhifei/DeepTutor",
        "sourceHead": "a" * 40,
        "releaseTag": "yfeistai-first-release-20260827-aaaaaaaa",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": "sha256:" + "1" * 64,
            "openmaic": "sha256:" + "2" * 64,
            "openmaic_render": "sha256:" + "3" * 64,
        },
    }


def _release_run() -> dict[str, str]:
    return {"runId": "run-capacity", "environmentId": "environment-capacity"}


def _report() -> dict[str, object]:
    samples: list[dict[str, object]] = []
    for metric in SESSION_METRICS:
        for sequence in range(200):
            samples.append(
                {
                    "metric": metric,
                    "tenantId": f"tenant-{sequence % 50:02d}",
                    "subjectId": f"session-{sequence:03d}",
                    "sequence": sequence,
                    "latencyMs": 10.0,
                    "success": True,
                }
            )
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
            "observedAt": f"2026-08-27T00:00:0{sequence}Z",
            "available": True,
            "totalRssBytes": 600 + sequence * 60,
            "limitBytes": 10_000,
            "availableBytes": 8_000 - sequence * 100,
            "limitSource": "cgroup",
            "usageRatio": (600 + sequence * 60) / 10_000,
            "partial": False,
            "processes": [
                {"label": "supervisor", "count": 1, "rssBytes": 100 + sequence * 10},
                {"label": "backend", "count": 1, "rssBytes": 200 + sequence * 20},
                {"label": "web", "count": 1, "rssBytes": 300 + sequence * 30},
            ],
        }
        for sequence, phase in enumerate(
            ("baseline", "generation_saturated", "sessions_saturated", "final")
        )
    ]
    return {
        "schemaVersion": 1,
        "producer": "classroom-capacity-probe",
        "capacityModel": "deployed-candidate",
        "candidate": _candidate(),
        "releaseRun": _release_run(),
        "observedAt": "2026-08-27T00:00:00Z",
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
        "resourceSource": {
            "method": "GET",
            "path": "/api/v1/system/memory",
            "scope": "deeptutor-api-container-process-tree",
        },
        "resourceObservations": resource_observations,
    }


def _body(report: dict[str, object]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def test_capacity_profile_report_replays_fixed_live_workload() -> None:
    module = _module()
    report = _report()

    parsed = module.parse_capacity_profile_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url="https://candidate.example.test",
    )
    summary = module.derive_capacity_profile_summary(parsed)

    assert summary["checks"] == {
        "thresholdsPassed": True,
        "rawSamplesRecorded": True,
        "resourceObservationsRecorded": True,
        "resourceAccountingComplete": True,
        "resourceBoundaryStable": True,
    }
    assert summary["workload"] == report["workload"]
    assert summary["scheduler"] == {
        "maxGlobalActiveObserved": 20,
        "maxTenantActiveObserved": 2,
        "maxConcurrentSessionsObserved": 200,
        "foreignTenantsBeforeNoisyThird": 49,
        "observedGenerationJobs": 52,
        "completedLearningSessions": 200,
    }
    assert summary["metrics"]["core_api"] == {
        "count": 200,
        "p50Ms": 10.0,
        "p95Ms": 10.0,
        "p99Ms": 10.0,
        "errorRate": 0.0,
    }
    assert summary["checks"]["resourceObservationsRecorded"] is True
    assert summary["checks"]["resourceAccountingComplete"] is True
    assert summary["checks"]["resourceBoundaryStable"] is True
    assert summary["resources"] == {
        "scope": "deeptutor-api-container-process-tree",
        "sampleCount": 4,
        "peakTotalRssBytes": 780,
        "peakUsageRatio": 0.078,
        "minimumAvailableBytes": 7_700,
        "limitSource": "cgroup",
        "limitBytes": 10_000,
        "partialObserved": False,
    }


def test_capacity_profile_summary_rejects_partial_resource_accounting() -> None:
    module = _module()
    report = _report()
    report["resourceObservations"][1]["partial"] = True

    parsed = module.parse_capacity_profile_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url="https://candidate.example.test",
    )

    assert (
        module.derive_capacity_profile_summary(parsed)["checks"]["resourceAccountingComplete"]
        is False
    )


def test_capacity_profile_summary_rejects_resource_boundary_drift() -> None:
    module = _module()
    report = _report()
    observation = report["resourceObservations"][2]
    observation["limitBytes"] = 20_000
    observation["usageRatio"] = observation["totalRssBytes"] / 20_000

    parsed = module.parse_capacity_profile_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url="https://candidate.example.test",
    )

    assert (
        module.derive_capacity_profile_summary(parsed)["checks"]["resourceBoundaryStable"] is False
    )


@pytest.mark.parametrize("case", ("phase", "ratio", "sum", "bool-int"))
def test_capacity_profile_report_rejects_invalid_resource_observations(case: str) -> None:
    module = _module()
    report = _report()
    observation = report["resourceObservations"][0]
    if case == "phase":
        observation["phase"] = "unexpected"
    elif case == "ratio":
        observation["usageRatio"] = 0.99
    elif case == "sum":
        observation["processes"][0]["rssBytes"] += 1
    else:
        observation["totalRssBytes"] = True

    with pytest.raises(ValueError, match="resource"):
        module.parse_capacity_profile_report(
            _body(report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_base_url="https://candidate.example.test",
        )


def test_capacity_profile_report_rejects_simulated_model() -> None:
    module = _module()
    report = _report()
    report["capacityModel"] = "simulated"

    with pytest.raises(ValueError, match="deployed candidate"):
        module.parse_capacity_profile_report(
            _body(report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_base_url="https://candidate.example.test",
        )


def test_capacity_profile_report_rejects_incomplete_tenant_distribution() -> None:
    module = _module()
    report = _report()
    samples = report["rawSamples"]
    assert isinstance(samples, list)
    samples[0]["tenantId"] = "tenant-01"

    with pytest.raises(ValueError, match="sample distribution"):
        module.parse_capacity_profile_report(
            _body(report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_base_url="https://candidate.example.test",
        )


def test_capacity_profile_report_recomputes_failed_latency_threshold() -> None:
    module = _module()
    report = _report()
    samples = report["rawSamples"]
    assert isinstance(samples, list)
    for sample in samples:
        if sample["metric"] == "core_api":
            sample["latencyMs"] = 500.0

    parsed = module.parse_capacity_profile_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url="https://candidate.example.test",
    )

    assert module.derive_capacity_profile_summary(parsed)["checks"] == {
        "thresholdsPassed": False,
        "rawSamplesRecorded": True,
        "resourceObservationsRecorded": True,
        "resourceAccountingComplete": True,
        "resourceBoundaryStable": True,
    }


def test_capacity_profile_report_rejects_boolean_numeric_fields() -> None:
    module = _module()
    report = _report()
    report["schedulerObservations"][0]["sequence"] = True

    with pytest.raises(ValueError, match="scheduler observation"):
        module.parse_capacity_profile_report(
            _body(report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_base_url="https://candidate.example.test",
        )


def test_capacity_profile_summary_replays_scheduler_observations() -> None:
    module = _module()
    report = _report()
    for observation in report["schedulerObservations"]:
        observation["active"] = observation["active"][:19]

    parsed = module.parse_capacity_profile_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url="https://candidate.example.test",
    )

    assert module.derive_capacity_profile_summary(parsed)["checks"] == {
        "thresholdsPassed": False,
        "rawSamplesRecorded": True,
        "resourceObservationsRecorded": True,
        "resourceAccountingComplete": True,
        "resourceBoundaryStable": True,
    }


def test_capacity_profile_summary_replays_authoritative_claim_order() -> None:
    module = _module()
    report = _report()
    claims = report["schedulerClaims"]
    assert isinstance(claims, list)
    claims[2], claims[-1] = claims[-1], claims[2]
    for sequence, claim in enumerate(claims):
        claim["sequence"] = sequence

    parsed = module.parse_capacity_profile_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url="https://candidate.example.test",
    )

    assert module.derive_capacity_profile_summary(parsed)["checks"] == {
        "thresholdsPassed": False,
        "rawSamplesRecorded": True,
        "resourceObservationsRecorded": True,
        "resourceAccountingComplete": True,
        "resourceBoundaryStable": True,
    }


def test_capacity_profile_report_requires_each_job_once_in_claim_audit() -> None:
    module = _module()
    report = _report()
    claims = report["schedulerClaims"]
    assert isinstance(claims, list)
    claims[-1] = {**claims[-2], "sequence": len(claims) - 1}

    with pytest.raises(ValueError, match="scheduler claims"):
        module.parse_capacity_profile_report(
            _body(report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_base_url="https://candidate.example.test",
        )


def test_capacity_profile_report_rejects_float_profile_counts() -> None:
    module = _module()
    report = _report()
    report["profile"]["executedTenants"] = 50.0

    with pytest.raises(ValueError, match="profile"):
        module.parse_capacity_profile_report(
            _body(report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_base_url="https://candidate.example.test",
        )


def test_capacity_profile_report_rejects_oversized_body_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    def forbidden_loads(_body: bytes):
        pytest.fail("oversized capacity reports must be rejected before JSON decoding")

    monkeypatch.setattr(module.json, "loads", forbidden_loads)

    with pytest.raises(ValueError, match="too large"):
        module.parse_capacity_profile_report(
            b" " * (module.MAX_CAPACITY_REPORT_BYTES + 1),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_base_url="https://candidate.example.test",
        )


def test_capacity_profile_report_rejects_noncanonical_json() -> None:
    module = _module()
    report = _report()
    body = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()

    with pytest.raises(ValueError, match="not canonical"):
        module.parse_capacity_profile_report(
            body,
            candidate=_candidate(),
            release_run=_release_run(),
            expected_base_url="https://candidate.example.test",
        )


def test_capacity_profile_command_record_is_stable() -> None:
    assert _module().capacity_profile_command_record() == {
        "runner": "python",
        "script": "scripts/classroom_capacity_probe.py",
        "arguments": ["--profile", "first-release"],
    }
