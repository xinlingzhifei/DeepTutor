from __future__ import annotations

import math

import pytest

from deeptutor.teaching.metrics import TeachingMetrics, hash_tenant_id

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


def test_metrics_expose_required_names_with_hashed_tenant_labels() -> None:
    metrics = TeachingMetrics()
    tenant_id = "tenant-private-a"
    metrics.observe_generation_queue(tenant_id=tenant_id, seconds=1.25)
    metrics.record_generation_job(tenant_id=tenant_id, status="completed")
    metrics.set_openmaic_health(route_id="shared-openmaic", healthy=True)

    payload = metrics.render().decode("utf-8")

    for metric_name in REQUIRED_METRICS:
        assert metric_name in payload
    assert tenant_id not in payload
    assert hash_tenant_id(tenant_id) in payload
    assert "shared-openmaic" not in payload


def test_metric_labels_reject_unbounded_or_sensitive_categories() -> None:
    metrics = TeachingMetrics()

    with pytest.raises(ValueError, match="generation status"):
        metrics.record_generation_job(
            tenant_id="tenant-a",
            status="private textbook content",
        )
    with pytest.raises(ValueError, match="event type"):
        metrics.record_learning_event(
            tenant_id="tenant-a",
            event_type="user-u-secret",
        )


@pytest.mark.parametrize("seconds", [-1.0, math.inf, math.nan])
def test_metric_durations_reject_negative_or_non_finite_values(seconds: float) -> None:
    metrics = TeachingMetrics()

    with pytest.raises(ValueError, match="seconds"):
        metrics.observe_generation_queue(tenant_id="tenant-a", seconds=seconds)


def test_metric_counts_reject_negative_values() -> None:
    metrics = TeachingMetrics()

    with pytest.raises(ValueError, match="slots"):
        metrics.set_generation_slots(tenant_id="tenant-a", in_use=-1)
    with pytest.raises(ValueError, match="quota units"):
        metrics.add_quota_units(
            tenant_id="tenant-a",
            operation="consumed",
            units=-1,
        )
