"""Deterministic first-release classroom capacity harness.

This harness models the release envelope with a controllable simulated
OpenMAIC provider.  It does not claim to measure a deployed environment; the
saved report identifies the model as simulated so the final release gate can
keep synthetic capacity evidence separate from live-system evidence.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
import json
from math import ceil
import os
from pathlib import Path
import random
import time
import tracemalloc


@dataclass(frozen=True, slots=True)
class LoadProfile:
    name: str
    tenants: int
    registered_users: int
    daily_active_users: int
    concurrent_classrooms: int
    shared_generation_slots: int
    default_tenant_slots: int


@dataclass(frozen=True, slots=True)
class RawSample:
    metric: str
    tenant_id: str
    sequence: int
    latency_ms: float
    success: bool


@dataclass(frozen=True, slots=True)
class MetricSummary:
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    error_rate: float


@dataclass(frozen=True, slots=True)
class SchedulerSummary:
    total_jobs: int
    max_global_active: int
    max_tenant_active: int
    max_concurrent_classrooms: int
    foreign_tenants_before_noisy_third: int


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    wall_seconds: float
    process_cpu_seconds: float
    peak_traced_bytes: int


@dataclass(frozen=True, slots=True)
class CapacityReport:
    capacity_model: str
    profile: LoadProfile
    summary: dict[str, MetricSummary]
    scheduler: SchedulerSummary
    resource_usage: ResourceUsage
    raw_samples: tuple[RawSample, ...]
    violations: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.violations


_PROFILES = {
    "first-release": LoadProfile(
        name="first-release",
        tenants=50,
        registered_users=100_000,
        daily_active_users=10_000,
        concurrent_classrooms=200,
        shared_generation_slots=20,
        default_tenant_slots=2,
    )
}

_LATENCY_LIMITS_MS = {
    "event_ingest": 1_000.0,
    "core_api": 500.0,
    "job_submission_visible": 2_000.0,
    "mastery_projection_visible": 60_000.0,
}


def load_profile(name: str) -> LoadProfile:
    try:
        return _PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown classroom load profile: {name}") from None


class SimulatedOpenMaicProvider:
    def __init__(self, *, delay_ms: float, error_rate: float, seed: int) -> None:
        if delay_ms < 0:
            raise ValueError("provider delay must be non-negative")
        if not 0 <= error_rate <= 1:
            raise ValueError("provider error rate must be between zero and one")
        self._delay_seconds = delay_ms / 1_000
        self._error_rate = error_rate
        self._random = random.Random(seed)

    async def generate(self, *, tenant_id: str, sequence: int) -> RawSample:
        started = time.perf_counter()
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        success = self._random.random() >= self._error_rate
        return RawSample(
            metric="generation_provider",
            tenant_id=tenant_id,
            sequence=sequence,
            latency_ms=(time.perf_counter() - started) * 1_000,
            success=success,
        )


def _jobs(profile: LoadProfile) -> tuple[tuple[str, int], ...]:
    tenant_ids = tuple(f"tenant-{index:02d}" for index in range(profile.tenants))
    foreign_jobs = tuple((tenant_id, index) for index, tenant_id in enumerate(tenant_ids[1:], 1))
    noisy_count = profile.concurrent_classrooms - len(foreign_jobs)
    noisy_jobs = tuple((tenant_ids[0], index) for index in range(noisy_count))
    return (*noisy_jobs, *foreign_jobs)


async def _run_provider_load(
    profile: LoadProfile,
    provider: SimulatedOpenMaicProvider,
) -> tuple[tuple[RawSample, ...], SchedulerSummary]:
    queues: dict[str, deque[tuple[str, int]]] = defaultdict(deque)
    for job in _jobs(profile):
        queues[job[0]].append(job)
    ready = deque(sorted(queues))
    active: dict[asyncio.Task[RawSample], str] = {}
    active_by_tenant: Counter[str] = Counter()
    dispatch_order: list[str] = []
    samples: list[RawSample] = []
    max_global_active = 0
    max_tenant_active = 0

    while ready or active:
        while ready and len(active) < profile.shared_generation_slots:
            launched = False
            for _ in range(len(ready)):
                tenant_id = ready.popleft()
                if active_by_tenant[tenant_id] >= profile.default_tenant_slots:
                    ready.append(tenant_id)
                    continue
                queued = queues[tenant_id]
                if not queued:
                    continue
                _, sequence = queued.popleft()
                task = asyncio.create_task(
                    provider.generate(tenant_id=tenant_id, sequence=sequence)
                )
                active[task] = tenant_id
                active_by_tenant[tenant_id] += 1
                dispatch_order.append(tenant_id)
                if queued:
                    ready.append(tenant_id)
                max_global_active = max(max_global_active, len(active))
                max_tenant_active = max(max_tenant_active, active_by_tenant[tenant_id])
                launched = True
                break
            if not launched:
                break

        if not active:
            raise RuntimeError("capacity scheduler made no progress")
        completed, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            tenant_id = active.pop(task)
            active_by_tenant[tenant_id] -= 1
            samples.append(task.result())

    noisy_tenant = "tenant-00"
    noisy_seen = 0
    foreign_before_third: set[str] = set()
    for tenant_id in dispatch_order:
        if tenant_id == noisy_tenant:
            noisy_seen += 1
            if noisy_seen == 3:
                break
        else:
            foreign_before_third.add(tenant_id)
    return (
        tuple(samples),
        SchedulerSummary(
            total_jobs=len(samples),
            max_global_active=max_global_active,
            max_tenant_active=max_tenant_active,
            max_concurrent_classrooms=0,
            foreign_tenants_before_noisy_third=len(foreign_before_third),
        ),
    )


async def _run_classroom_load(
    profile: LoadProfile,
) -> tuple[tuple[RawSample, ...], int]:
    metric_delays = {
        "event_ingest": 0.001,
        "core_api": 0.001,
        "job_submission_visible": 0.002,
        "mastery_projection_visible": 0.003,
    }
    active_sessions = 0
    peak_sessions = 0

    async def run_session(sequence: int) -> tuple[RawSample, ...]:
        nonlocal active_sessions, peak_sessions
        tenant_id = f"tenant-{sequence % profile.tenants:02d}"
        active_sessions += 1
        peak_sessions = max(peak_sessions, active_sessions)
        samples: list[RawSample] = []
        try:
            for metric, delay_seconds in metric_delays.items():
                started = time.perf_counter()
                await asyncio.sleep(delay_seconds)
                samples.append(
                    RawSample(
                        metric=metric,
                        tenant_id=tenant_id,
                        sequence=sequence,
                        latency_ms=(time.perf_counter() - started) * 1_000,
                        success=True,
                    )
                )
            return tuple(samples)
        finally:
            active_sessions -= 1

    session_samples = await asyncio.gather(
        *(run_session(sequence) for sequence in range(profile.concurrent_classrooms))
    )
    return tuple(sample for session in session_samples for sample in session), peak_sessions


async def _execute_profile(
    profile: LoadProfile,
    provider: SimulatedOpenMaicProvider,
) -> tuple[tuple[RawSample, ...], SchedulerSummary]:
    provider_samples, scheduler = await _run_provider_load(profile, provider)
    classroom_samples, peak_sessions = await _run_classroom_load(profile)
    return (
        (*provider_samples, *classroom_samples),
        SchedulerSummary(
            total_jobs=scheduler.total_jobs,
            max_global_active=scheduler.max_global_active,
            max_tenant_active=scheduler.max_tenant_active,
            max_concurrent_classrooms=peak_sessions,
            foreign_tenants_before_noisy_third=(scheduler.foreign_tenants_before_noisy_third),
        ),
    )


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, ceil((percentile / 100) * len(ordered)) - 1)
    return round(ordered[index], 3)


def _summaries(samples: tuple[RawSample, ...]) -> dict[str, MetricSummary]:
    grouped: dict[str, list[RawSample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.metric].append(sample)
    return {
        metric: MetricSummary(
            count=len(metric_samples),
            p50_ms=_percentile([sample.latency_ms for sample in metric_samples], 50),
            p95_ms=_percentile([sample.latency_ms for sample in metric_samples], 95),
            p99_ms=_percentile([sample.latency_ms for sample in metric_samples], 99),
            error_rate=round(
                sum(not sample.success for sample in metric_samples) / len(metric_samples),
                6,
            ),
        )
        for metric, metric_samples in sorted(grouped.items())
    }


def run_profile(
    profile: LoadProfile,
    *,
    provider_delay_ms: float = 20,
    provider_error_rate: float = 0,
    seed: int = 1,
) -> CapacityReport:
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    tracemalloc.start()
    try:
        provider = SimulatedOpenMaicProvider(
            delay_ms=provider_delay_ms,
            error_rate=provider_error_rate,
            seed=seed,
        )
        raw_samples, scheduler = asyncio.run(_execute_profile(profile, provider))
        summary = _summaries(raw_samples)
        violations = tuple(
            f"{metric} p95 {summary[metric].p95_ms}ms exceeds {limit}ms"
            for metric, limit in _LATENCY_LIMITS_MS.items()
            if summary[metric].p95_ms >= limit
        )
        _, peak_traced_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return CapacityReport(
        capacity_model="simulated",
        profile=profile,
        summary=summary,
        scheduler=scheduler,
        resource_usage=ResourceUsage(
            wall_seconds=round(time.perf_counter() - started_wall, 6),
            process_cpu_seconds=round(time.process_time() - started_cpu, 6),
            peak_traced_bytes=peak_traced_bytes,
        ),
        raw_samples=tuple(raw_samples),
        violations=violations,
    )


def _report_payload(report: CapacityReport) -> dict[str, object]:
    return {
        "capacityModel": report.capacity_model,
        "passed": report.passed,
        "violations": list(report.violations),
        "profile": {
            "name": report.profile.name,
            "tenants": report.profile.tenants,
            "registeredUsers": report.profile.registered_users,
            "dailyActiveUsers": report.profile.daily_active_users,
            "concurrentClassrooms": report.profile.concurrent_classrooms,
            "sharedGenerationSlots": report.profile.shared_generation_slots,
            "defaultTenantSlots": report.profile.default_tenant_slots,
        },
        "summary": {
            metric: {
                "count": value.count,
                "p50Ms": value.p50_ms,
                "p95Ms": value.p95_ms,
                "p99Ms": value.p99_ms,
                "errorRate": value.error_rate,
            }
            for metric, value in report.summary.items()
        },
        "scheduler": {
            "totalJobs": report.scheduler.total_jobs,
            "maxGlobalActive": report.scheduler.max_global_active,
            "maxTenantActive": report.scheduler.max_tenant_active,
            "maxConcurrentClassrooms": report.scheduler.max_concurrent_classrooms,
            "foreignTenantsBeforeNoisyThird": (report.scheduler.foreign_tenants_before_noisy_third),
        },
        "resourceUsage": {
            "wallSeconds": report.resource_usage.wall_seconds,
            "processCpuSeconds": report.resource_usage.process_cpu_seconds,
            "peakTracedBytes": report.resource_usage.peak_traced_bytes,
        },
        "rawSamples": [
            {
                "metric": sample.metric,
                "tenantId": sample.tenant_id,
                "sequence": sample.sequence,
                "latencyMs": round(sample.latency_ms, 3),
                "success": sample.success,
            }
            for sample in report.raw_samples
        ],
    }


def write_report(report: CapacityReport, path: Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        staged.write_text(
            json.dumps(_report_payload(report), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(staged, target)
    finally:
        if staged.exists():
            staged.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="first-release", choices=sorted(_PROFILES))
    parser.add_argument("--provider-delay-ms", type=float, default=20)
    parser.add_argument("--provider-error-rate", type=float, default=0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    profile = load_profile(args.profile)
    report = run_profile(
        profile,
        provider_delay_ms=args.provider_delay_ms,
        provider_error_rate=args.provider_error_rate,
        seed=args.seed,
    )
    output = args.output or Path("data/user/load-reports") / f"{profile.name}.json"
    write_report(report, output)
    print(
        json.dumps(
            {
                "capacityModel": report.capacity_model,
                "passed": report.passed,
                "report": str(output),
                "violations": list(report.violations),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
