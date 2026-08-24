"""Fixed-contract Prometheus rendering for durable teaching metric snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Final, Protocol

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)

GENERATION_STATUSES: Final = ("queued", "running", "completed", "failed", "canceled")
GENERATION_STAGES: Final = ("outline", "content", "export")
RETRY_REASONS: Final = (
    "timeout",
    "unavailable",
    "lease_lost",
    "rate_limited",
    "unknown",
)
SLOT_POOLS: Final = ("generation", "mp4_export")
QUOTA_OPERATIONS: Final = ("reserved", "consumed", "released")
LEARNING_EVENT_TYPES: Final = (
    "classroom.started",
    "scene.completed",
    "quiz.graded",
    "hint.used",
    "pbl.milestone_completed",
    "classroom.completed",
)
VALIDATION_REASONS: Final = (
    "schema_invalid",
    "receipt_mismatch",
    "hash_mismatch",
    "size_mismatch",
    "missing_artifact",
    "unknown",
)
OPENMAIC_MODES: Final = ("shared", "dedicated")

QUEUE_BUCKETS: Final = (
    "0.1",
    "0.5",
    "1",
    "2",
    "5",
    "10",
    "30",
    "60",
    "120",
    "300",
    "+Inf",
)
STAGE_BUCKETS: Final = (
    "0.5",
    "1",
    "2",
    "5",
    "10",
    "30",
    "60",
    "120",
    "300",
    "900",
    "1800",
    "+Inf",
)

COUNTER_CATEGORIES: Final = {
    "generation_jobs_total": GENERATION_STATUSES,
    "generation_retries_total": RETRY_REASONS,
    "quota_units_total": QUOTA_OPERATIONS,
    "learning_events_total": LEARNING_EVENT_TYPES,
    "artifact_validation_failures_total": VALIDATION_REASONS,
}
HISTOGRAM_CATEGORIES: Final = {
    "generation_queue_seconds": ("",),
    "generation_stage_seconds": GENERATION_STAGES,
}
HISTOGRAM_BUCKETS: Final = {
    "generation_queue_seconds": QUEUE_BUCKETS,
    "generation_stage_seconds": STAGE_BUCKETS,
}
GAUGE_CATEGORIES: Final = {
    "generation_slots_in_use": SLOT_POOLS,
    "learning_projection_lag_seconds": ("",),
    "openmaic_health": OPENMAIC_MODES,
}


@dataclass(frozen=True, slots=True)
class CounterRollup:
    metric: str
    category: str
    total: int


@dataclass(frozen=True, slots=True)
class HistogramRollup:
    metric: str
    category: str
    bucket: str
    count: int
    sum_seconds: float


@dataclass(frozen=True, slots=True)
class GaugeValue:
    metric: str
    category: str
    value: float | int


@dataclass(frozen=True, slots=True)
class TeachingMetricsSnapshot:
    counters: tuple[CounterRollup, ...] = ()
    histograms: tuple[HistogramRollup, ...] = ()
    gauges: tuple[GaugeValue, ...] = ()


def _snapshot_error() -> ValueError:
    return ValueError("metrics snapshot is invalid")


def _valid_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_number(value: object) -> bool:
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


@dataclass(frozen=True, slots=True)
class _NormalizedSnapshot:
    counters: dict[tuple[str, str], int]
    histograms: dict[tuple[str, str, str], tuple[int, float]]
    gauges: dict[tuple[str, str], float]


def _normalize_snapshot(snapshot: TeachingMetricsSnapshot) -> _NormalizedSnapshot:
    if not isinstance(snapshot, TeachingMetricsSnapshot):
        raise _snapshot_error()
    counters: dict[tuple[str, str], int] = {}
    histograms: dict[tuple[str, str, str], tuple[int, float]] = {}
    gauges: dict[tuple[str, str], float] = {}
    try:
        for row in snapshot.counters:
            key = (row.metric, row.category)
            if (
                row.metric not in COUNTER_CATEGORIES
                or row.category not in COUNTER_CATEGORIES[row.metric]
                or not _valid_count(row.total)
                or key in counters
            ):
                raise _snapshot_error()
            counters[key] = row.total
        for row in snapshot.histograms:
            key = (row.metric, row.category, row.bucket)
            if (
                row.metric not in HISTOGRAM_CATEGORIES
                or row.category not in HISTOGRAM_CATEGORIES[row.metric]
                or row.bucket not in HISTOGRAM_BUCKETS[row.metric]
                or not _valid_count(row.count)
                or not _valid_number(row.sum_seconds)
                or (row.count == 0 and float(row.sum_seconds) != 0.0)
                or key in histograms
            ):
                raise _snapshot_error()
            histograms[key] = (row.count, float(row.sum_seconds))
        for row in snapshot.gauges:
            key = (row.metric, row.category)
            if (
                row.metric not in GAUGE_CATEGORIES
                or row.category not in GAUGE_CATEGORIES[row.metric]
                or not _valid_number(row.value)
                or key in gauges
                or (row.metric == "openmaic_health" and row.value not in (0, 1))
                or (row.metric == "generation_slots_in_use" and not _valid_count(row.value))
            ):
                raise _snapshot_error()
            gauges[key] = float(row.value)
    except (AttributeError, TypeError):
        raise _snapshot_error() from None
    return _NormalizedSnapshot(counters, histograms, gauges)


def validate_teaching_metrics_snapshot(
    snapshot: TeachingMetricsSnapshot,
) -> TeachingMetricsSnapshot:
    """Fail closed when a durable snapshot violates the fixed public contract."""

    _normalize_snapshot(snapshot)
    return snapshot


class _MetricCollector(Protocol):
    def collect(self) -> list[object]: ...


class _SnapshotCollector:
    def __init__(self, snapshot: _NormalizedSnapshot) -> None:
        self._snapshot = snapshot

    def _histogram(
        self,
        metric: str,
        category: str,
    ) -> tuple[list[tuple[str, float]], float]:
        cumulative = 0
        sum_value = 0.0
        buckets: list[tuple[str, float]] = []
        for bucket in HISTOGRAM_BUCKETS[metric]:
            count, bin_sum = self._snapshot.histograms.get(
                (metric, category, bucket),
                (0, 0.0),
            )
            cumulative += count
            sum_value += bin_sum
            buckets.append((bucket, float(cumulative)))
        return buckets, sum_value

    def collect(self) -> list[object]:
        families: list[object] = []

        queue_buckets, queue_sum = self._histogram("generation_queue_seconds", "")
        families.append(
            HistogramMetricFamily(
                "yfeistai_generation_queue_seconds",
                "Seconds an eligible generation attempt waited before it was claimed.",
                buckets=queue_buckets,
                sum_value=queue_sum,
            )
        )

        stages = HistogramMetricFamily(
            "yfeistai_generation_stage_seconds",
            "Seconds spent in one generation stage.",
            labels=("stage",),
        )
        for stage in GENERATION_STAGES:
            buckets, sum_value = self._histogram("generation_stage_seconds", stage)
            stages.add_metric((stage,), buckets, sum_value)
        families.append(stages)

        counter_contracts = (
            (
                "generation_jobs_total",
                "Committed generation lifecycle state entries.",
                "status",
            ),
            ("generation_retries_total", "Generation retries by stable reason.", "reason"),
        )
        for metric, documentation, label in counter_contracts:
            family = CounterMetricFamily(
                f"yfeistai_{metric}",
                documentation,
                labels=(label,),
            )
            for category in COUNTER_CATEGORIES[metric]:
                family.add_metric(
                    (category,),
                    self._snapshot.counters.get((metric, category), 0),
                )
            families.append(family)

        slots = GaugeMetricFamily(
            "yfeistai_generation_slots_in_use",
            "Global generation slots currently in use.",
            labels=("pool",),
        )
        for pool in SLOT_POOLS:
            slots.add_metric(
                (pool,),
                self._snapshot.gauges.get(("generation_slots_in_use", pool), 0),
            )
        families.append(slots)

        quota = CounterMetricFamily(
            "yfeistai_quota_units_total",
            "Quota unit changes by operation.",
            labels=("operation",),
        )
        for operation in QUOTA_OPERATIONS:
            quota.add_metric(
                (operation,),
                self._snapshot.counters.get(("quota_units_total", operation), 0),
            )
        families.append(quota)

        events = CounterMetricFamily(
            "yfeistai_learning_events_total",
            "Accepted learning events by canonical type.",
            labels=("event_type",),
        )
        for event_type in LEARNING_EVENT_TYPES:
            events.add_metric(
                (event_type,),
                self._snapshot.counters.get(("learning_events_total", event_type), 0),
            )
        families.append(events)

        families.append(
            GaugeMetricFamily(
                "yfeistai_learning_projection_lag_seconds",
                "Age in seconds of the oldest nonterminal learning projection.",
                value=self._snapshot.gauges.get(
                    ("learning_projection_lag_seconds", ""),
                    0,
                ),
            )
        )

        validation = CounterMetricFamily(
            "yfeistai_artifact_validation_failures_total",
            "Artifact validation failures by stable reason.",
            labels=("reason",),
        )
        for reason in VALIDATION_REASONS:
            validation.add_metric(
                (reason,),
                self._snapshot.counters.get(
                    ("artifact_validation_failures_total", reason),
                    0,
                ),
            )
        families.append(validation)

        openmaic = GaugeMetricFamily(
            "yfeistai_openmaic_health",
            "Durable database admission health; this is not live network health.",
            labels=("mode",),
        )
        for mode in OPENMAIC_MODES:
            openmaic.add_metric(
                (mode,),
                self._snapshot.gauges.get(("openmaic_health", mode), 0),
            )
        families.append(openmaic)
        return families


class TeachingMetrics:
    """Render an absolute durable snapshot without process-local mutation."""

    def render(self, snapshot: TeachingMetricsSnapshot) -> bytes:
        normalized = _normalize_snapshot(snapshot)
        registry = CollectorRegistry()
        collector: _MetricCollector = _SnapshotCollector(normalized)
        registry.register(collector)
        return generate_latest(registry)


__all__ = [
    "COUNTER_CATEGORIES",
    "CounterRollup",
    "GAUGE_CATEGORIES",
    "GaugeValue",
    "HISTOGRAM_BUCKETS",
    "HISTOGRAM_CATEGORIES",
    "HistogramRollup",
    "TeachingMetrics",
    "TeachingMetricsSnapshot",
    "validate_teaching_metrics_snapshot",
]
