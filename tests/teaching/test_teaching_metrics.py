from __future__ import annotations

from dataclasses import replace
import math
import re

import pytest

from deeptutor.teaching.metrics import (
    CounterRollup,
    GaugeValue,
    HistogramRollup,
    TeachingMetrics,
    TeachingMetricsSnapshot,
)

REQUIRED_METRICS = (
    "yfeistai_generation_queue_seconds",
    "yfeistai_generation_stage_seconds",
    "yfeistai_generation_jobs_total",
    "yfeistai_generation_retries_total",
    "yfeistai_generation_slots_in_use",
    "yfeistai_quota_units_total",
    "yfeistai_learning_events_total",
    "yfeistai_learning_projection_lag_seconds",
    "yfeistai_artifact_validation_failures_total",
    "yfeistai_openmaic_health",
)


def _snapshot() -> TeachingMetricsSnapshot:
    return TeachingMetricsSnapshot(
        counters=(
            CounterRollup("generation_jobs_total", "completed", 7),
            CounterRollup("generation_retries_total", "timeout", 2),
            CounterRollup("quota_units_total", "consumed", 11),
            CounterRollup("learning_events_total", "quiz.graded", 5),
            CounterRollup("artifact_validation_failures_total", "hash_mismatch", 3),
        ),
        histograms=(
            HistogramRollup("generation_queue_seconds", "", "0.1", 2, 0.1),
            HistogramRollup("generation_queue_seconds", "", "0.5", 3, 1.2),
            HistogramRollup("generation_queue_seconds", "", "+Inf", 1, 301.0),
            HistogramRollup("generation_stage_seconds", "content", "1", 4, 2.5),
        ),
        gauges=(
            GaugeValue("generation_slots_in_use", "generation", 2),
            GaugeValue("learning_projection_lag_seconds", "", 12.5),
            GaugeValue("openmaic_health", "shared", 1),
            GaugeValue("openmaic_health", "dedicated", 0),
        ),
    )


def test_absolute_snapshot_exposes_every_fixed_family_and_series() -> None:
    payload = TeachingMetrics().render(_snapshot()).decode("utf-8")

    for metric_name in REQUIRED_METRICS:
        assert f"# TYPE {metric_name}" in payload

    for stage in ("outline", "content", "export"):
        assert f'yfeistai_generation_stage_seconds_count{{stage="{stage}"}}' in payload
    for status in ("queued", "running", "completed", "failed", "canceled"):
        assert f'yfeistai_generation_jobs_total{{status="{status}"}}' in payload
    for reason in ("timeout", "unavailable", "lease_lost", "rate_limited", "unknown"):
        assert f'yfeistai_generation_retries_total{{reason="{reason}"}}' in payload
    for pool in ("generation", "mp4_export"):
        assert f'yfeistai_generation_slots_in_use{{pool="{pool}"}}' in payload
    for operation in ("reserved", "consumed", "released"):
        assert f'yfeistai_quota_units_total{{operation="{operation}"}}' in payload
    for event_type in (
        "classroom.started",
        "scene.completed",
        "quiz.graded",
        "hint.used",
        "pbl.milestone_completed",
        "classroom.completed",
    ):
        assert f'yfeistai_learning_events_total{{event_type="{event_type}"}}' in payload
    for reason in (
        "schema_invalid",
        "receipt_mismatch",
        "hash_mismatch",
        "size_mismatch",
        "missing_artifact",
        "unknown",
    ):
        assert f'yfeistai_artifact_validation_failures_total{{reason="{reason}"}}' in payload
    for mode in ("shared", "dedicated"):
        assert f'yfeistai_openmaic_health{{mode="{mode}"}}' in payload

    assert "yfeistai_generation_queue_seconds_count 6.0" in payload
    assert 'yfeistai_generation_stage_seconds_count{stage="outline"} 0.0' in payload
    assert 'yfeistai_generation_jobs_total{status="queued"} 0.0' in payload
    assert 'yfeistai_generation_jobs_total{status="completed"} 7.0' in payload
    assert 'yfeistai_generation_slots_in_use{pool="mp4_export"} 0.0' in payload
    assert "yfeistai_learning_projection_lag_seconds 12.5" in payload


def test_public_metric_labels_are_only_fixed_finite_dimensions() -> None:
    payload = TeachingMetrics().render(_snapshot()).decode("utf-8")
    label_names = {match.group(1) for match in re.finditer(r'(\w+)="[^"]*"', payload)}

    assert label_names == {
        "stage",
        "status",
        "reason",
        "pool",
        "operation",
        "event_type",
        "mode",
        "le",
    }
    for forbidden in (
        "tenant-private-a",
        "job-private-a",
        "user-private-a",
        "private-route-id",
        "provider-private-a",
        "https://private.example",
        "secret-private-a",
        "instance-private-a",
        "worker-private-a",
    ):
        assert forbidden not in payload.lower()


def test_rendering_the_same_absolute_snapshot_is_byte_stable_and_idempotent() -> None:
    snapshot = _snapshot()

    first = TeachingMetrics().render(snapshot)
    second = TeachingMetrics().render(snapshot)
    third = TeachingMetrics().render(snapshot)

    assert first == second == third
    assert first.count(b'yfeistai_generation_retries_total{reason="timeout"} 2.0') == 1


@pytest.mark.parametrize(
    "snapshot",
    [
        TeachingMetricsSnapshot(
            counters=(CounterRollup("private_metric", "unknown", 1),),
        ),
        TeachingMetricsSnapshot(
            counters=(CounterRollup("generation_retries_total", "private-reason", 1),),
        ),
        TeachingMetricsSnapshot(
            counters=(CounterRollup("generation_retries_total", "timeout", -1),),
        ),
        TeachingMetricsSnapshot(
            histograms=(HistogramRollup("generation_queue_seconds", "", "private", 1, 1.0),),
        ),
        TeachingMetricsSnapshot(
            histograms=(HistogramRollup("generation_queue_seconds", "", "0.1", 1, math.nan),),
        ),
        TeachingMetricsSnapshot(
            gauges=(GaugeValue("openmaic_health", "shared", 2),),
        ),
    ],
)
def test_invalid_absolute_snapshot_fails_closed(snapshot: TeachingMetricsSnapshot) -> None:
    with pytest.raises(ValueError, match="metrics snapshot is invalid"):
        TeachingMetrics().render(snapshot)


def test_histogram_snapshot_rejects_positive_sum_for_empty_bin() -> None:
    snapshot = TeachingMetricsSnapshot(
        histograms=(HistogramRollup("generation_queue_seconds", "", "0.1", 0, 0.1),),
    )

    with pytest.raises(ValueError, match="metrics snapshot is invalid"):
        TeachingMetrics().render(snapshot)


@pytest.mark.parametrize(
    "snapshot",
    [
        replace(
            _snapshot(),
            counters=(
                CounterRollup("generation_retries_total", "timeout", 1),
                CounterRollup("generation_retries_total", "timeout", 2),
            ),
        ),
        replace(
            _snapshot(),
            histograms=(
                HistogramRollup("generation_queue_seconds", "", "0.1", 1, 0.1),
                HistogramRollup("generation_queue_seconds", "", "0.1", 2, 0.2),
            ),
        ),
        replace(
            _snapshot(),
            gauges=(
                GaugeValue("openmaic_health", "shared", 1),
                GaugeValue("openmaic_health", "shared", 0),
            ),
        ),
    ],
)
def test_duplicate_absolute_snapshot_rows_fail_closed(
    snapshot: TeachingMetricsSnapshot,
) -> None:
    with pytest.raises(ValueError, match="metrics snapshot is invalid"):
        TeachingMetrics().render(snapshot)
