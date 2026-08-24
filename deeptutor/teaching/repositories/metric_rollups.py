"""Atomic teaching-metric rollup writes composed inside caller transactions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import math

from sqlalchemy import delete, func, insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deeptutor.teaching.metrics import (
    COUNTER_CATEGORIES,
    HISTOGRAM_BUCKETS,
    HISTOGRAM_CATEGORIES,
)
from deeptutor.teaching.models.platform import (
    TeachingLearningProjectionBacklog,
    TeachingMetricCounterRollup,
    TeachingMetricHistogramRollup,
)

_SHARD_COUNT = 16


class MetricRollupConsistencyError(RuntimeError):
    """A durable rollup fact expected by the caller is missing."""


@dataclass(frozen=True, slots=True)
class CounterRollupObservation:
    """One private fact to add to a fixed counter series."""

    metric: str
    category: str
    fact_key: str
    amount: int


def _metric_error() -> ValueError:
    return ValueError("metric rollup input is invalid")


def _backlog_error() -> ValueError:
    return ValueError("learning projection backlog input is invalid")


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _valid_metric_category(metric: object, category: object) -> bool:
    if not isinstance(metric, str) or not isinstance(category, str):
        return False
    categories = COUNTER_CATEGORIES.get(metric) or HISTOGRAM_CATEGORIES.get(metric)
    return categories is not None and category in categories


def metric_rollup_shard(fact_key: str, metric: str, category: str) -> int:
    """Select one stable internal shard without storing the private fact key."""

    if not _nonempty(fact_key) or not _valid_metric_category(metric, category):
        raise _metric_error()
    digest = hashlib.sha256(f"{fact_key}\0{metric}\0{category}".encode()).digest()
    return int.from_bytes(digest, "big") % _SHARD_COUNT


async def increment_counter_rollup(
    session: AsyncSession,
    *,
    metric: str,
    category: str,
    fact_key: str,
    amount: int,
) -> None:
    """Atomically add a positive amount to one fixed counter series."""

    await increment_counter_rollups(
        session,
        observations=(
            CounterRollupObservation(
                metric=metric,
                category=category,
                fact_key=fact_key,
                amount=amount,
            ),
        ),
    )


async def increment_counter_rollups(
    session: AsyncSession,
    *,
    observations: Iterable[CounterRollupObservation],
) -> None:
    """Aggregate and add counter facts in one deterministic lock order."""

    aggregated: dict[tuple[str, str, int], int] = {}
    for observation in observations:
        if not isinstance(observation, CounterRollupObservation):
            raise _metric_error()
        metric = observation.metric
        category = observation.category
        fact_key = observation.fact_key
        amount = observation.amount
        if (
            not isinstance(metric, str)
            or not isinstance(category, str)
            or metric not in COUNTER_CATEGORIES
            or category not in COUNTER_CATEGORIES[metric]
            or not _nonempty(fact_key)
            or isinstance(amount, bool)
            or not isinstance(amount, int)
            or amount <= 0
        ):
            raise _metric_error()
        key = (metric, category, metric_rollup_shard(fact_key, metric, category))
        aggregated[key] = aggregated.get(key, 0) + amount

    for (metric, category, shard), amount in sorted(aggregated.items()):
        statement = postgresql_insert(TeachingMetricCounterRollup).values(
            metric=metric,
            category=category,
            shard=shard,
            total=amount,
        )
        statement = statement.on_conflict_do_update(
            index_elements=("metric", "category", "shard"),
            set_={
                "total": TeachingMetricCounterRollup.total + amount,
                "updated_at": func.now(),
            },
        )
        await session.execute(statement)


def _histogram_bucket(metric: str, seconds: float) -> str:
    for bucket in HISTOGRAM_BUCKETS[metric]:
        if bucket == "+Inf" or seconds <= float(bucket):
            return bucket
    raise AssertionError("fixed histogram buckets must end at +Inf")


async def observe_histogram_rollup(
    session: AsyncSession,
    *,
    metric: str,
    category: str,
    fact_key: str,
    seconds: float,
) -> None:
    """Atomically add one observation to its fixed noncumulative bucket bin."""

    if (
        not isinstance(metric, str)
        or not isinstance(category, str)
        or metric not in HISTOGRAM_CATEGORIES
        or category not in HISTOGRAM_CATEGORIES[metric]
        or not _nonempty(fact_key)
        or isinstance(seconds, bool)
        or not isinstance(seconds, (float, int))
    ):
        raise _metric_error()
    try:
        seconds_value = float(seconds)
    except (OverflowError, TypeError, ValueError):
        raise _metric_error() from None
    if not math.isfinite(seconds_value) or seconds_value < 0:
        raise _metric_error()
    bucket = _histogram_bucket(metric, seconds_value)
    shard = metric_rollup_shard(fact_key, metric, category)
    statement = postgresql_insert(TeachingMetricHistogramRollup).values(
        metric=metric,
        category=category,
        bucket=bucket,
        shard=shard,
        count=1,
        sum_seconds=seconds_value,
    )
    statement = statement.on_conflict_do_update(
        index_elements=("metric", "category", "bucket", "shard"),
        set_={
            "count": TeachingMetricHistogramRollup.count + 1,
            "sum_seconds": TeachingMetricHistogramRollup.sum_seconds + seconds_value,
            "updated_at": func.now(),
        },
    )
    await session.execute(statement)


def _validate_backlog_identity(tenant_id: str, event_id: str) -> None:
    if not _nonempty(tenant_id) or not _nonempty(event_id):
        raise _backlog_error()


async def insert_learning_projection_backlog(
    session: AsyncSession,
    *,
    tenant_id: str,
    event_id: str,
    received_at: datetime,
) -> None:
    """Insert one exact nonterminal projection mirror row."""

    _validate_backlog_identity(tenant_id, event_id)
    try:
        timezone_aware = (
            isinstance(received_at, datetime)
            and received_at.tzinfo is not None
            and received_at.utcoffset() is not None
        )
    except Exception:
        timezone_aware = False
    if not timezone_aware:
        raise _backlog_error()
    await session.execute(
        insert(TeachingLearningProjectionBacklog).values(
            tenant_id=tenant_id,
            event_id=event_id,
            received_at=received_at,
        )
    )


async def delete_learning_projection_backlog(
    session: AsyncSession,
    *,
    tenant_id: str,
    event_id: str,
) -> None:
    """Delete one terminal projection mirror row and fail if it is absent."""

    _validate_backlog_identity(tenant_id, event_id)
    statement = (
        delete(TeachingLearningProjectionBacklog)
        .where(
            TeachingLearningProjectionBacklog.tenant_id == tenant_id,
            TeachingLearningProjectionBacklog.event_id == event_id,
        )
        .returning(
            TeachingLearningProjectionBacklog.tenant_id,
            TeachingLearningProjectionBacklog.event_id,
        )
    )
    result = await session.execute(statement)
    if result.one_or_none() is None:
        raise MetricRollupConsistencyError("learning projection backlog row is missing")


__all__ = [
    "CounterRollupObservation",
    "MetricRollupConsistencyError",
    "delete_learning_projection_backlog",
    "increment_counter_rollup",
    "increment_counter_rollups",
    "insert_learning_projection_backlog",
    "metric_rollup_shard",
    "observe_histogram_rollup",
]
