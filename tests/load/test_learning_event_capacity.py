from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


def _load_harness():
    path = Path(__file__).parents[2] / "scripts" / "load_classroom.py"
    spec = importlib.util.spec_from_file_location("task6_load_classroom", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_first_release_learning_latency_thresholds_and_raw_samples() -> None:
    module = _load_harness()
    profile = module.load_profile("first-release")

    report = module.run_profile(
        profile,
        provider_delay_ms=1,
        provider_error_rate=0,
        seed=19,
    )

    assert report.passed is True
    assert report.violations == ()
    assert report.summary["event_ingest"].p95_ms < 1_000
    assert report.summary["core_api"].p95_ms < 500
    assert report.summary["job_submission_visible"].p95_ms < 2_000
    assert report.summary["mastery_projection_visible"].p95_ms < 60_000
    assert all(
        summary.p50_ms <= summary.p95_ms <= summary.p99_ms for summary in report.summary.values()
    )
    assert len(report.raw_samples) == profile.concurrent_classrooms * 5
    assert {sample.metric for sample in report.raw_samples} == {
        "core_api",
        "event_ingest",
        "generation_provider",
        "job_submission_visible",
        "mastery_projection_visible",
    }
    assert report.resource_usage.wall_seconds >= 0
    assert report.resource_usage.process_cpu_seconds >= 0
    assert report.resource_usage.peak_traced_bytes > 0


def test_capacity_report_is_saved_with_samples_and_percentiles(tmp_path: Path) -> None:
    module = _load_harness()
    report = module.run_profile(
        module.load_profile("first-release"),
        provider_delay_ms=0,
        provider_error_rate=0,
        seed=23,
    )
    target = tmp_path / "first-release-capacity.json"

    module.write_report(report, target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["capacityModel"] == "simulated"
    assert payload["profile"]["concurrentClassrooms"] == 200
    assert payload["summary"]["event_ingest"]["p95Ms"] < 1_000
    assert payload["scheduler"]["maxConcurrentClassrooms"] == 200
    assert len(payload["rawSamples"]) == 1_000
    assert payload["resourceUsage"]["peakTracedBytes"] > 0
