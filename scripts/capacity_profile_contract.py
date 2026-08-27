"""Strict contract for one candidate-bound live classroom capacity probe."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime
import json
import math
from math import ceil
import re
from urllib.parse import urlsplit

MAX_CAPACITY_REPORT_BYTES = 512 * 1024
CAPACITY_SCHEMA_VERSION = 1
CAPACITY_PRODUCER = "classroom-capacity-probe"
CAPACITY_MODEL = "deployed-candidate"
CAPACITY_METRICS = (
    "core_api",
    "event_ingest",
    "job_submission_visible",
    "mastery_projection_visible",
)
CAPACITY_LIMITS_MS = {
    "core_api": 500.0,
    "event_ingest": 1_000.0,
    "job_submission_visible": 2_000.0,
    "mastery_projection_visible": 60_000.0,
}
CAPACITY_PROFILE = {
    "name": "first-release",
    "declaredRegisteredUsers": 100_000,
    "declaredDailyActiveUsers": 10_000,
    "executedTenants": 50,
    "executedConcurrentSessions": 200,
    "sharedGenerationSlots": 20,
    "defaultTenantSlots": 2,
}
CAPACITY_WORKLOAD = {
    "generationJobsSubmitted": 52,
    "learningSessionsStarted": 200,
    "learningSessionsCompleted": 200,
}
CAPACITY_SESSION_METRICS = (
    "core_api",
    "event_ingest",
    "mastery_projection_visible",
)
CAPACITY_SAMPLE_COUNTS = {
    "core_api": 200,
    "event_ingest": 200,
    "job_submission_visible": 52,
    "mastery_projection_visible": 200,
}
CAPACITY_RAW_SAMPLE_COUNT = sum(CAPACITY_SAMPLE_COUNTS.values())
CAPACITY_RESOURCE_SOURCE = {
    "method": "GET",
    "path": "/api/v1/system/memory",
    "scope": "deeptutor-api-container-process-tree",
}
CAPACITY_RESOURCE_PHASES = (
    "baseline",
    "generation_saturated",
    "sessions_saturated",
    "final",
)

_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def capacity_profile_command_record() -> dict[str, object]:
    """Return the secret-free logical command recorded in release evidence."""

    return {
        "runner": "python",
        "script": "scripts/classroom_capacity_probe.py",
        "arguments": ["--profile", "first-release"],
    }


def canonical_capacity_profile_report(report: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def exact_json_equal(left: object, right: object) -> bool:
    """Compare parsed JSON without Python's bool/int or int/float coercions."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            exact_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            exact_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _valid_observed_at(raw: object) -> bool:
    if not isinstance(raw, str) or _OBSERVED_AT.fullmatch(raw) is None:
        return False
    try:
        datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _valid_base_url(raw: object) -> bool:
    if not isinstance(raw, str) or not raw or raw != raw.rstrip("/"):
        return False
    parsed = urlsplit(raw)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _exact_nonnegative_int(raw: object) -> int | None:
    return raw if type(raw) is int and raw >= 0 else None


def _sample_latency(raw: object) -> float | None:
    if type(raw) not in {int, float}:
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0 or value > 600_000:
        return None
    return value


def _parse_samples(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list) or len(raw) != CAPACITY_RAW_SAMPLE_COUNT:
        raise ValueError("capacity report sample count is invalid")
    samples: list[dict[str, object]] = []
    by_metric: dict[str, dict[int, dict[str, object]]] = defaultdict(dict)
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "metric",
            "tenantId",
            "subjectId",
            "sequence",
            "latencyMs",
            "success",
        }:
            raise ValueError("capacity report sample is invalid")
        metric = item.get("metric")
        tenant_id = item.get("tenantId")
        subject_id = item.get("subjectId")
        sequence = _exact_nonnegative_int(item.get("sequence"))
        latency_ms = _sample_latency(item.get("latencyMs"))
        if (
            metric not in CAPACITY_METRICS
            or not isinstance(tenant_id, str)
            or _PUBLIC_ID.fullmatch(tenant_id) is None
            or not isinstance(subject_id, str)
            or _PUBLIC_ID.fullmatch(subject_id) is None
            or sequence is None
            or sequence >= CAPACITY_SAMPLE_COUNTS[metric]
            or latency_ms is None
            or type(item.get("success")) is not bool
            or sequence in by_metric[metric]
        ):
            raise ValueError("capacity report sample is invalid")
        normalized = dict(item)
        normalized["latencyMs"] = latency_ms
        by_metric[metric][sequence] = normalized
        samples.append(normalized)

    expected_sequences = {
        metric: set(range(CAPACITY_SAMPLE_COUNTS[metric])) for metric in CAPACITY_METRICS
    }
    if set(by_metric) != set(CAPACITY_METRICS) or any(
        set(by_metric[metric]) != expected_sequences[metric] for metric in CAPACITY_METRICS
    ):
        raise ValueError("capacity report sample distribution is invalid")
    reference = by_metric[CAPACITY_SESSION_METRICS[0]]
    for metric in CAPACITY_SESSION_METRICS[1:]:
        for sequence in expected_sequences[metric]:
            if (
                by_metric[metric][sequence]["tenantId"] != reference[sequence]["tenantId"]
                or by_metric[metric][sequence]["subjectId"] != reference[sequence]["subjectId"]
            ):
                raise ValueError("capacity report sample distribution is invalid")
    tenant_counts = Counter(
        str(reference[sequence]["tenantId"])
        for sequence in expected_sequences[CAPACITY_SESSION_METRICS[0]]
    )
    if len(tenant_counts) != CAPACITY_PROFILE["executedTenants"] or set(tenant_counts.values()) != {
        CAPACITY_SAMPLE_COUNTS[CAPACITY_SESSION_METRICS[0]] // CAPACITY_PROFILE["executedTenants"]
    }:
        raise ValueError("capacity report sample distribution is invalid")
    if (
        len(
            {
                str(reference[index]["subjectId"])
                for index in expected_sequences[CAPACITY_SESSION_METRICS[0]]
            }
        )
        != (CAPACITY_SAMPLE_COUNTS[CAPACITY_SESSION_METRICS[0]])
    ):
        raise ValueError("capacity report sample distribution is invalid")
    jobs = by_metric["job_submission_visible"]
    job_tenant_counts = Counter(
        str(jobs[sequence]["tenantId"]) for sequence in expected_sequences["job_submission_visible"]
    )
    if (
        len(job_tenant_counts) != CAPACITY_PROFILE["executedTenants"]
        or sorted(job_tenant_counts.values()) != [1] * 49 + [3]
        or len(
            {
                str(jobs[index]["subjectId"])
                for index in expected_sequences["job_submission_visible"]
            }
        )
        != CAPACITY_SAMPLE_COUNTS["job_submission_visible"]
    ):
        raise ValueError("capacity report job distribution is invalid")
    return samples


def _subject_tenants(samples: list[dict[str, object]], metric: str) -> dict[str, str]:
    return {
        str(sample["subjectId"]): str(sample["tenantId"])
        for sample in samples
        if sample["metric"] == metric
    }


def _parse_scheduler_claims(
    raw: object,
    *,
    samples: list[dict[str, object]],
) -> list[dict[str, object]]:
    subject_tenants = _subject_tenants(samples, "job_submission_visible")
    if not isinstance(raw, list) or len(raw) != len(subject_tenants):
        raise ValueError("capacity report scheduler claims are invalid")
    claims: list[dict[str, object]] = []
    seen: set[str] = set()
    for sequence, item in enumerate(raw):
        if (
            not isinstance(item, dict)
            or set(item) != {"sequence", "jobId", "tenantId"}
            or _exact_nonnegative_int(item.get("sequence")) != sequence
        ):
            raise ValueError("capacity report scheduler claims are invalid")
        job_id = item.get("jobId")
        tenant_id = item.get("tenantId")
        if (
            not isinstance(job_id, str)
            or not isinstance(tenant_id, str)
            or subject_tenants.get(job_id) != tenant_id
            or job_id in seen
        ):
            raise ValueError("capacity report scheduler claims are invalid")
        seen.add(job_id)
        claims.append({"sequence": sequence, "jobId": job_id, "tenantId": tenant_id})
    if seen != set(subject_tenants):
        raise ValueError("capacity report scheduler claims are invalid")
    return claims


def _parse_observations(
    raw: object,
    *,
    samples: list[dict[str, object]],
    kind: str,
) -> list[dict[str, object]]:
    if kind == "scheduler":
        id_key = "jobId"
        metric = "job_submission_visible"
        maximum_active = CAPACITY_WORKLOAD["generationJobsSubmitted"]
    else:
        id_key = "sessionId"
        metric = CAPACITY_SESSION_METRICS[0]
        maximum_active = CAPACITY_PROFILE["executedConcurrentSessions"]
    if not isinstance(raw, list) or not raw or len(raw) > 4096:
        raise ValueError(f"capacity report {kind} observations are invalid")
    subject_tenants = _subject_tenants(samples, metric)
    observations: list[dict[str, object]] = []
    for sequence, item in enumerate(raw):
        if (
            not isinstance(item, dict)
            or set(item) != {"sequence", "active"}
            or _exact_nonnegative_int(item.get("sequence")) != sequence
            or not isinstance(item.get("active"), list)
            or len(item["active"]) > maximum_active
        ):
            raise ValueError(f"capacity report {kind} observation is invalid")
        active: list[dict[str, str]] = []
        seen: set[str] = set()
        for record in item["active"]:
            if not isinstance(record, dict) or set(record) != {id_key, "tenantId"}:
                raise ValueError(f"capacity report {kind} observation is invalid")
            subject_id = record.get(id_key)
            tenant_id = record.get("tenantId")
            if (
                not isinstance(subject_id, str)
                or not isinstance(tenant_id, str)
                or subject_tenants.get(subject_id) != tenant_id
                or subject_id in seen
            ):
                raise ValueError(f"capacity report {kind} observation is invalid")
            seen.add(subject_id)
            active.append({id_key: subject_id, "tenantId": tenant_id})
        observations.append({"sequence": sequence, "active": active})
    return observations


def _parse_session_completions(
    raw: object,
    *,
    samples: list[dict[str, object]],
) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > CAPACITY_WORKLOAD["learningSessionsStarted"]:
        raise ValueError("capacity report session completions are invalid")
    subject_tenants = _subject_tenants(samples, CAPACITY_SESSION_METRICS[0])
    completions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"sessionId", "tenantId", "status"}:
            raise ValueError("capacity report session completion is invalid")
        session_id = item.get("sessionId")
        tenant_id = item.get("tenantId")
        if (
            not isinstance(session_id, str)
            or not isinstance(tenant_id, str)
            or item.get("status") != "completed"
            or subject_tenants.get(session_id) != tenant_id
            or session_id in seen
        ):
            raise ValueError("capacity report session completion is invalid")
        seen.add(session_id)
        completions.append({"sessionId": session_id, "tenantId": tenant_id, "status": "completed"})
    return completions


def _parse_resource_observations(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list) or len(raw) != len(CAPACITY_RESOURCE_PHASES):
        raise ValueError("capacity report resource observations are invalid")
    observations: list[dict[str, object]] = []
    previous_at: datetime | None = None
    for sequence, (item, phase) in enumerate(zip(raw, CAPACITY_RESOURCE_PHASES, strict=True)):
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "sequence",
                "phase",
                "observedAt",
                "available",
                "totalRssBytes",
                "limitBytes",
                "availableBytes",
                "limitSource",
                "usageRatio",
                "partial",
                "processes",
            }
            or _exact_nonnegative_int(item.get("sequence")) != sequence
            or item.get("phase") != phase
            or not _valid_observed_at(item.get("observedAt"))
            or type(item.get("available")) is not bool
            or type(item.get("partial")) is not bool
        ):
            raise ValueError("capacity report resource observation is invalid")
        observed_at = datetime.fromisoformat(str(item["observedAt"]).removesuffix("Z") + "+00:00")
        if previous_at is not None and observed_at < previous_at:
            raise ValueError("capacity report resource observation is invalid")
        previous_at = observed_at
        total_rss = _exact_nonnegative_int(item.get("totalRssBytes"))
        limit = _exact_nonnegative_int(item.get("limitBytes"))
        available = _exact_nonnegative_int(item.get("availableBytes"))
        usage_ratio = _sample_latency(item.get("usageRatio"))
        if (
            total_rss is None
            or limit is None
            or limit == 0
            or available is None
            or available > limit
            or item.get("limitSource") not in {"cgroup", "host"}
            or usage_ratio is None
            or not math.isclose(
                usage_ratio,
                total_rss / limit,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("capacity report resource observation is invalid")
        raw_processes = item.get("processes")
        if not isinstance(raw_processes, list) or not raw_processes or len(raw_processes) > 32:
            raise ValueError("capacity report resource processes are invalid")
        processes: list[dict[str, object]] = []
        labels: set[str] = set()
        for process in raw_processes:
            if not isinstance(process, dict) or set(process) != {"label", "count", "rssBytes"}:
                raise ValueError("capacity report resource process is invalid")
            label = process.get("label")
            count = _exact_nonnegative_int(process.get("count"))
            rss_bytes = _exact_nonnegative_int(process.get("rssBytes"))
            if (
                not isinstance(label, str)
                or not label
                or len(label) > 32
                or "\r" in label
                or "\n" in label
                or label in labels
                or count is None
                or count == 0
                or rss_bytes is None
            ):
                raise ValueError("capacity report resource process is invalid")
            labels.add(label)
            processes.append({"label": label, "count": count, "rssBytes": rss_bytes})
        if sum(int(process["rssBytes"]) for process in processes) != total_rss:
            raise ValueError("capacity report resource accounting is invalid")
        normalized = dict(item)
        normalized["usageRatio"] = usage_ratio
        normalized["processes"] = processes
        observations.append(normalized)
    return observations


def parse_capacity_profile_report(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
) -> dict[str, object]:
    """Parse one canonical live report and enforce the fixed first-release workload."""

    if not isinstance(body, bytes) or len(body) > MAX_CAPACITY_REPORT_BYTES:
        raise ValueError("capacity report is too large")
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("capacity report is invalid") from exc
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion",
        "producer",
        "capacityModel",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "profile",
        "workload",
        "rawSamples",
        "schedulerSource",
        "schedulerClaims",
        "schedulerObservations",
        "sessionObservations",
        "sessionCompletions",
        "resourceSource",
        "resourceObservations",
    }:
        raise ValueError("capacity report is invalid")
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != (
        CAPACITY_SCHEMA_VERSION
    ):
        raise ValueError("capacity report is invalid")
    if document.get("producer") != CAPACITY_PRODUCER:
        raise ValueError("capacity report producer is invalid")
    if document.get("capacityModel") != CAPACITY_MODEL:
        raise ValueError("capacity report must measure a deployed candidate")
    if not exact_json_equal(document.get("candidate"), dict(candidate)) or not exact_json_equal(
        document.get("releaseRun"), dict(release_run)
    ):
        raise ValueError("capacity report release binding is invalid")
    if not _valid_observed_at(document.get("observedAt")):
        raise ValueError("capacity report timestamp is invalid")
    if not _valid_base_url(document.get("baseUrl")) or document.get("baseUrl") != expected_base_url:
        raise ValueError("capacity report URL is invalid")
    if not exact_json_equal(document.get("profile"), CAPACITY_PROFILE):
        raise ValueError("capacity report profile is invalid")
    if not exact_json_equal(document.get("workload"), CAPACITY_WORKLOAD):
        raise ValueError("capacity report workload is invalid")
    if not exact_json_equal(document.get("resourceSource"), CAPACITY_RESOURCE_SOURCE):
        raise ValueError("capacity report resource source is invalid")
    samples = _parse_samples(document.get("rawSamples"))
    if document.get("schedulerSource") != "admin-atomic-db-claim-audit":
        raise ValueError("capacity report scheduler source is invalid")
    scheduler_claims = _parse_scheduler_claims(
        document.get("schedulerClaims"),
        samples=samples,
    )
    scheduler_observations = _parse_observations(
        document.get("schedulerObservations"),
        samples=samples,
        kind="scheduler",
    )
    session_observations = _parse_observations(
        document.get("sessionObservations"),
        samples=samples,
        kind="session",
    )
    session_completions = _parse_session_completions(
        document.get("sessionCompletions"),
        samples=samples,
    )
    resource_observations = _parse_resource_observations(document.get("resourceObservations"))
    normalized: dict[str, object] = dict(document)
    normalized["rawSamples"] = samples
    normalized["schedulerClaims"] = scheduler_claims
    normalized["schedulerObservations"] = scheduler_observations
    normalized["sessionObservations"] = session_observations
    normalized["sessionCompletions"] = session_completions
    normalized["resourceObservations"] = resource_observations
    if canonical_capacity_profile_report(normalized) != body:
        raise ValueError("capacity report is not canonical")
    return normalized


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    index = max(0, ceil((percentile / 100) * len(ordered)) - 1)
    return round(ordered[index], 3)


def derive_capacity_profile_summary(report: Mapping[str, object]) -> dict[str, object]:
    samples = report.get("rawSamples")
    workload = report.get("workload")
    scheduler_claims = report.get("schedulerClaims")
    scheduler_observations = report.get("schedulerObservations")
    session_observations = report.get("sessionObservations")
    session_completions = report.get("sessionCompletions")
    resource_observations = report.get("resourceObservations")
    if (
        not isinstance(samples, list)
        or not isinstance(workload, dict)
        or not isinstance(scheduler_claims, list)
        or not isinstance(scheduler_observations, list)
        or not isinstance(session_observations, list)
        or not isinstance(session_completions, list)
        or not isinstance(resource_observations, list)
    ):
        raise ValueError("capacity report is invalid")
    metrics: dict[str, dict[str, int | float]] = {}
    thresholds_passed = True
    for metric in CAPACITY_METRICS:
        selected = [sample for sample in samples if sample.get("metric") == metric]
        latencies = [float(sample["latencyMs"]) for sample in selected]
        failures = sum(sample.get("success") is not True for sample in selected)
        p50_ms = _percentile(latencies, 50)
        p95_ms = _percentile(latencies, 95)
        p99_ms = _percentile(latencies, 99)
        error_rate = round(failures / len(selected), 6)
        metrics[metric] = {
            "count": len(selected),
            "p50Ms": p50_ms,
            "p95Ms": p95_ms,
            "p99Ms": p99_ms,
            "errorRate": error_rate,
        }
        thresholds_passed = thresholds_passed and (
            len(selected) == CAPACITY_SAMPLE_COUNTS[metric]
            and error_rate == 0
            and p95_ms < CAPACITY_LIMITS_MS[metric]
        )
    job_samples = [sample for sample in samples if sample.get("metric") == "job_submission_visible"]
    job_tenants = {str(sample["subjectId"]): str(sample["tenantId"]) for sample in job_samples}
    noisy_tenant = Counter(job_tenants.values()).most_common(1)[0][0]
    noisy_jobs = [
        str(sample["subjectId"])
        for sample in sorted(job_samples, key=lambda sample: int(sample["sequence"]))
        if sample["tenantId"] == noisy_tenant
    ]
    third_noisy_job = noisy_jobs[2]
    max_global_active = 0
    max_tenant_active = 0
    for observation in scheduler_observations:
        active = observation["active"]
        active_ids = {str(item["jobId"]) for item in active}
        max_global_active = max(max_global_active, len(active_ids))
        tenant_counts = Counter(str(item["tenantId"]) for item in active)
        max_tenant_active = max(max_tenant_active, max(tenant_counts.values(), default=0))
    third_claim_sequence = next(
        (int(claim["sequence"]) for claim in scheduler_claims if claim["jobId"] == third_noisy_job),
        None,
    )
    foreign_tenants_before_noisy_third = {
        str(claim["tenantId"])
        for claim in scheduler_claims
        if third_claim_sequence is not None
        and int(claim["sequence"]) < third_claim_sequence
        and claim["tenantId"] != noisy_tenant
    }
    max_concurrent_sessions = max(
        (len(observation["active"]) for observation in session_observations),
        default=0,
    )
    scheduler = {
        "maxGlobalActiveObserved": max_global_active,
        "maxTenantActiveObserved": max_tenant_active,
        "maxConcurrentSessionsObserved": max_concurrent_sessions,
        "foreignTenantsBeforeNoisyThird": len(foreign_tenants_before_noisy_third),
        "observedGenerationJobs": len(scheduler_claims),
        "completedLearningSessions": len(session_completions),
    }
    thresholds_passed = thresholds_passed and (
        scheduler
        == {
            "maxGlobalActiveObserved": CAPACITY_PROFILE["sharedGenerationSlots"],
            "maxTenantActiveObserved": CAPACITY_PROFILE["defaultTenantSlots"],
            "maxConcurrentSessionsObserved": CAPACITY_PROFILE["executedConcurrentSessions"],
            "foreignTenantsBeforeNoisyThird": CAPACITY_PROFILE["executedTenants"] - 1,
            "observedGenerationJobs": CAPACITY_WORKLOAD["generationJobsSubmitted"],
            "completedLearningSessions": CAPACITY_WORKLOAD["learningSessionsCompleted"],
        }
        and workload == CAPACITY_WORKLOAD
    )
    resource_accounting_complete = all(
        observation.get("available") is True and observation.get("partial") is False
        for observation in resource_observations
    )
    resource_boundaries = {
        (observation.get("limitSource"), observation.get("limitBytes"))
        for observation in resource_observations
    }
    resource_boundary_stable = len(resource_boundaries) == 1
    resource_observations_recorded = (
        len(resource_observations) == len(CAPACITY_RESOURCE_PHASES)
        and tuple(observation.get("phase") for observation in resource_observations)
        == CAPACITY_RESOURCE_PHASES
    )
    limit_source, limit_bytes = next(iter(resource_boundaries), (None, None))
    resources = {
        "scope": CAPACITY_RESOURCE_SOURCE["scope"],
        "sampleCount": len(resource_observations),
        "peakTotalRssBytes": max(
            (int(observation["totalRssBytes"]) for observation in resource_observations),
            default=0,
        ),
        "peakUsageRatio": round(
            max(
                (float(observation["usageRatio"]) for observation in resource_observations),
                default=0.0,
            ),
            6,
        ),
        "minimumAvailableBytes": min(
            (int(observation["availableBytes"]) for observation in resource_observations),
            default=0,
        ),
        "limitSource": limit_source if resource_boundary_stable else "mixed",
        "limitBytes": limit_bytes if resource_boundary_stable else None,
        "partialObserved": any(
            observation.get("partial") is True for observation in resource_observations
        ),
    }
    return {
        "checks": {
            "thresholdsPassed": thresholds_passed,
            "rawSamplesRecorded": len(samples) == CAPACITY_RAW_SAMPLE_COUNT,
            "resourceObservationsRecorded": resource_observations_recorded,
            "resourceAccountingComplete": resource_accounting_complete,
            "resourceBoundaryStable": resource_boundary_stable,
        },
        "metrics": metrics,
        "resources": resources,
        "workload": dict(workload),
        "scheduler": dict(scheduler),
    }


__all__ = [
    "CAPACITY_LIMITS_MS",
    "CAPACITY_METRICS",
    "CAPACITY_MODEL",
    "CAPACITY_PROFILE",
    "CAPACITY_PRODUCER",
    "CAPACITY_RAW_SAMPLE_COUNT",
    "CAPACITY_RESOURCE_PHASES",
    "CAPACITY_RESOURCE_SOURCE",
    "CAPACITY_SAMPLE_COUNTS",
    "CAPACITY_SCHEMA_VERSION",
    "CAPACITY_SESSION_METRICS",
    "CAPACITY_WORKLOAD",
    "MAX_CAPACITY_REPORT_BYTES",
    "canonical_capacity_profile_report",
    "capacity_profile_command_record",
    "derive_capacity_profile_summary",
    "exact_json_equal",
    "parse_capacity_profile_report",
]
