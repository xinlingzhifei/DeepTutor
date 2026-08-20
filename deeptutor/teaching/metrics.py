"""Low-cardinality Prometheus metrics for the private teaching runtime."""

from __future__ import annotations

import hashlib
import math
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

_GENERATION_STATUSES: Final = frozenset({"queued", "running", "completed", "failed", "canceled"})
_GENERATION_STAGES: Final = frozenset({"outline", "content", "export"})
_RETRY_REASONS: Final = frozenset(
    {"timeout", "unavailable", "lease_lost", "rate_limited", "unknown"}
)
_QUOTA_OPERATIONS: Final = frozenset({"reserved", "consumed", "released"})
_LEARNING_EVENT_TYPES: Final = frozenset(
    {
        "classroom.started",
        "scene.completed",
        "quiz.graded",
        "hint.used",
        "pbl.milestone_completed",
        "classroom.completed",
    }
)
_VALIDATION_REASONS: Final = frozenset(
    {
        "schema_invalid",
        "receipt_mismatch",
        "hash_mismatch",
        "size_mismatch",
        "missing_artifact",
        "unknown",
    }
)


def _short_hash(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def hash_tenant_id(tenant_id: str) -> str:
    return _short_hash(tenant_id, "tenant_id")


def _category(value: str, allowed: frozenset[str], field: str) -> str:
    if value not in allowed:
        raise ValueError(f"{field} is invalid")
    return value


def _nonnegative_number(value: float | int, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{field} is invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} is invalid")
    return number


class TeachingMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry(auto_describe=True)
        self.generation_queue = Histogram(
            "yfeistai_generation_queue_seconds",
            "Seconds a generation job waited before it was claimed.",
            ("tenant",),
            registry=self.registry,
        )
        self.generation_stage = Histogram(
            "yfeistai_generation_stage_seconds",
            "Seconds spent in one generation stage.",
            ("tenant", "stage"),
            registry=self.registry,
        )
        self.generation_jobs = Counter(
            "yfeistai_generation_jobs_total",
            "Generation job outcomes.",
            ("tenant", "status"),
            registry=self.registry,
        )
        self.generation_retries = Counter(
            "yfeistai_generation_retries_total",
            "Generation retries by stable reason.",
            ("tenant", "reason"),
            registry=self.registry,
        )
        self.generation_slots = Gauge(
            "yfeistai_generation_slots_in_use",
            "Generation slots currently in use.",
            ("tenant",),
            registry=self.registry,
        )
        self.quota_units = Counter(
            "yfeistai_quota_units_total",
            "Quota unit changes by operation.",
            ("tenant", "operation"),
            registry=self.registry,
        )
        self.learning_events = Counter(
            "yfeistai_learning_events_total",
            "Accepted learning events by canonical type.",
            ("tenant", "event_type"),
            registry=self.registry,
        )
        self.learning_projection_lag = Gauge(
            "yfeistai_learning_projection_lag_seconds",
            "Age in seconds of the oldest pending learning projection.",
            ("tenant",),
            registry=self.registry,
        )
        self.artifact_validation_failures = Counter(
            "yfeistai_artifact_validation_failures_total",
            "Artifact validation failures by stable reason.",
            ("tenant", "reason"),
            registry=self.registry,
        )
        self.openmaic_health = Gauge(
            "yfeistai_openmaic_health",
            "OpenMAIC route health, where 1 is healthy and 0 is unhealthy.",
            ("route",),
            registry=self.registry,
        )

    def observe_generation_queue(self, *, tenant_id: str, seconds: float) -> None:
        seconds = _nonnegative_number(seconds, "seconds")
        self.generation_queue.labels(hash_tenant_id(tenant_id)).observe(seconds)

    def observe_generation_stage(
        self,
        *,
        tenant_id: str,
        stage: str,
        seconds: float,
    ) -> None:
        stage = _category(stage, _GENERATION_STAGES, "generation stage")
        seconds = _nonnegative_number(seconds, "seconds")
        self.generation_stage.labels(hash_tenant_id(tenant_id), stage).observe(seconds)

    def record_generation_job(self, *, tenant_id: str, status: str) -> None:
        status = _category(status, _GENERATION_STATUSES, "generation status")
        self.generation_jobs.labels(hash_tenant_id(tenant_id), status).inc()

    def record_generation_retry(self, *, tenant_id: str, reason: str) -> None:
        reason = _category(reason, _RETRY_REASONS, "retry reason")
        self.generation_retries.labels(hash_tenant_id(tenant_id), reason).inc()

    def set_generation_slots(self, *, tenant_id: str, in_use: int) -> None:
        in_use = _nonnegative_number(in_use, "slots")
        self.generation_slots.labels(hash_tenant_id(tenant_id)).set(in_use)

    def add_quota_units(
        self,
        *,
        tenant_id: str,
        operation: str,
        units: int,
    ) -> None:
        operation = _category(operation, _QUOTA_OPERATIONS, "quota operation")
        units = _nonnegative_number(units, "quota units")
        self.quota_units.labels(hash_tenant_id(tenant_id), operation).inc(units)

    def record_learning_event(self, *, tenant_id: str, event_type: str) -> None:
        event_type = _category(event_type, _LEARNING_EVENT_TYPES, "event type")
        self.learning_events.labels(hash_tenant_id(tenant_id), event_type).inc()

    def set_projection_lag(self, *, tenant_id: str, seconds: float) -> None:
        seconds = _nonnegative_number(seconds, "seconds")
        self.learning_projection_lag.labels(hash_tenant_id(tenant_id)).set(seconds)

    def record_artifact_validation_failure(
        self,
        *,
        tenant_id: str,
        reason: str,
    ) -> None:
        reason = _category(reason, _VALIDATION_REASONS, "validation reason")
        self.artifact_validation_failures.labels(hash_tenant_id(tenant_id), reason).inc()

    def set_openmaic_health(self, *, route_id: str, healthy: bool) -> None:
        route_hash = _short_hash(route_id, "route_id")
        self.openmaic_health.labels(route_hash).set(1 if healthy else 0)

    def render(self) -> bytes:
        return generate_latest(self.registry)


_metrics = TeachingMetrics()


def get_teaching_metrics() -> TeachingMetrics:
    return _metrics
