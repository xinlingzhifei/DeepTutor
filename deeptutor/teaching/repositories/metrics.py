"""Read one absolute teaching-metrics snapshot from PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from deeptutor.teaching.database import platform_session
from deeptutor.teaching.metrics import (
    OPENMAIC_MODES,
    CounterRollup,
    GaugeValue,
    HistogramRollup,
    TeachingMetricsSnapshot,
    validate_teaching_metrics_snapshot,
)
from deeptutor.teaching.models import (
    DataPlaneRoute,
    GenerationSlot,
    ProviderProfile,
    TeachingLearningProjectionBacklog,
    TeachingMetricCounterRollup,
    TeachingMetricHistogramRollup,
    Tenant,
)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def build_counter_snapshot_statement():
    return (
        select(
            TeachingMetricCounterRollup.metric,
            TeachingMetricCounterRollup.category,
            func.sum(TeachingMetricCounterRollup.total).label("total"),
        )
        .group_by(
            TeachingMetricCounterRollup.metric,
            TeachingMetricCounterRollup.category,
        )
        .order_by(
            TeachingMetricCounterRollup.metric,
            TeachingMetricCounterRollup.category,
        )
    )


def build_histogram_snapshot_statement():
    return (
        select(
            TeachingMetricHistogramRollup.metric,
            TeachingMetricHistogramRollup.category,
            TeachingMetricHistogramRollup.bucket,
            func.sum(TeachingMetricHistogramRollup.count).label("count"),
            func.sum(TeachingMetricHistogramRollup.sum_seconds).label("sum_seconds"),
        )
        .group_by(
            TeachingMetricHistogramRollup.metric,
            TeachingMetricHistogramRollup.category,
            TeachingMetricHistogramRollup.bucket,
        )
        .order_by(
            TeachingMetricHistogramRollup.metric,
            TeachingMetricHistogramRollup.category,
            TeachingMetricHistogramRollup.bucket,
        )
    )


def build_generation_slots_snapshot_statement():
    return (
        select(
            GenerationSlot.slot_pool.label("category"),
            func.count(GenerationSlot.id).label("value"),
        )
        .where(
            GenerationSlot.scope == "global",
            GenerationSlot.claimed_job_id.is_not(None),
        )
        .group_by(GenerationSlot.slot_pool)
        .order_by(GenerationSlot.slot_pool)
    )


def build_projection_lag_snapshot_statement():
    return select(
        func.coalesce(
            func.greatest(
                0.0,
                func.extract(
                    "epoch",
                    func.now() - func.min(TeachingLearningProjectionBacklog.received_at),
                ),
            ),
            0.0,
        ).label("value")
    )


def _active_tenant_count(mode: str):
    return (
        select(func.count(Tenant.id))
        .where(
            Tenant.status == "active",
            Tenant.data_plane_mode == mode,
        )
        .scalar_subquery()
    )


def _shared_binding_exists():
    return (
        select(literal(1))
        .select_from(DataPlaneRoute)
        .join(
            ProviderProfile,
            and_(
                ProviderProfile.id == DataPlaneRoute.provider_profile_id,
                ProviderProfile.scope == DataPlaneRoute.mode,
                ProviderProfile.tenant_id.is_(None),
                ProviderProfile.owner_key == DataPlaneRoute.owner_key,
            ),
        )
        .where(
            DataPlaneRoute.mode == "shared",
            DataPlaneRoute.tenant_id.is_(None),
            DataPlaneRoute.owner_key == "shared",
            DataPlaneRoute.status == "active",
            DataPlaneRoute.health_status == "healthy",
            ProviderProfile.status == "active",
        )
        .exists()
    )


def _admitted_dedicated_tenant_count():
    return (
        select(func.count(Tenant.id))
        .select_from(Tenant)
        .join(
            DataPlaneRoute,
            and_(
                DataPlaneRoute.mode == "dedicated",
                DataPlaneRoute.tenant_id == Tenant.id,
                DataPlaneRoute.owner_key == Tenant.id,
            ),
        )
        .join(
            ProviderProfile,
            and_(
                ProviderProfile.id == DataPlaneRoute.provider_profile_id,
                ProviderProfile.scope == DataPlaneRoute.mode,
                ProviderProfile.tenant_id == Tenant.id,
                ProviderProfile.owner_key == Tenant.id,
            ),
        )
        .where(
            Tenant.status == "active",
            Tenant.data_plane_mode == "dedicated",
            DataPlaneRoute.status == "active",
            DataPlaneRoute.health_status == "healthy",
            ProviderProfile.status == "active",
        )
        .scalar_subquery()
    )


def build_openmaic_admission_snapshot_statement():
    """Aggregate durable admission state; this is not a live network probe."""

    active_shared = _active_tenant_count("shared")
    active_dedicated = _active_tenant_count("dedicated")
    shared = select(
        literal("shared").label("category"),
        case(
            (and_(active_shared > 0, _shared_binding_exists()), 1),
            else_=0,
        ).label("value"),
    )
    dedicated = select(
        literal("dedicated").label("category"),
        case(
            (active_dedicated == 0, 1),
            (_admitted_dedicated_tenant_count() == active_dedicated, 1),
            else_=0,
        ).label("value"),
    )
    return shared.union_all(dedicated)


def _database_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError("metrics snapshot is invalid")
    converted = int(value)
    if converted < 0 or value != converted:
        raise ValueError("metrics snapshot is invalid")
    return converted


class SqlAlchemyTeachingMetricsRepository:
    """Read all metric evidence in one repeatable, read-only DB snapshot."""

    def __init__(self, session_factory: SessionFactory = platform_session) -> None:
        self._session_factory = session_factory

    async def fetch_snapshot(self) -> TeachingMetricsSnapshot:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                counter_rows = (
                    (await session.execute(build_counter_snapshot_statement())).mappings().all()
                )
                histogram_rows = (
                    (await session.execute(build_histogram_snapshot_statement())).mappings().all()
                )
                slot_rows = (
                    (await session.execute(build_generation_slots_snapshot_statement()))
                    .mappings()
                    .all()
                )
                lag = (
                    await session.execute(build_projection_lag_snapshot_statement())
                ).scalar_one()
                admission_rows = (
                    (await session.execute(build_openmaic_admission_snapshot_statement()))
                    .mappings()
                    .all()
                )

        admission_modes = [str(row["category"]) for row in admission_rows]
        if len(admission_modes) != len(OPENMAIC_MODES) or set(admission_modes) != set(
            OPENMAIC_MODES
        ):
            raise ValueError("metrics snapshot is invalid")
        snapshot = TeachingMetricsSnapshot(
            counters=tuple(
                CounterRollup(
                    metric=str(row["metric"]),
                    category=str(row["category"]),
                    total=_database_count(row["total"]),
                )
                for row in counter_rows
            ),
            histograms=tuple(
                HistogramRollup(
                    metric=str(row["metric"]),
                    category=str(row["category"]),
                    bucket=str(row["bucket"]),
                    count=_database_count(row["count"]),
                    sum_seconds=float(row["sum_seconds"]),
                )
                for row in histogram_rows
            ),
            gauges=(
                *(
                    GaugeValue(
                        "generation_slots_in_use",
                        str(row["category"]),
                        _database_count(row["value"]),
                    )
                    for row in slot_rows
                ),
                GaugeValue("learning_projection_lag_seconds", "", float(lag)),
                *(
                    GaugeValue(
                        "openmaic_health",
                        str(row["category"]),
                        _database_count(row["value"]),
                    )
                    for row in admission_rows
                ),
            ),
        )
        return validate_teaching_metrics_snapshot(snapshot)


def get_teaching_metrics_repository() -> SqlAlchemyTeachingMetricsRepository:
    return SqlAlchemyTeachingMetricsRepository()


__all__ = [
    "SqlAlchemyTeachingMetricsRepository",
    "build_counter_snapshot_statement",
    "build_generation_slots_snapshot_statement",
    "build_histogram_snapshot_statement",
    "build_openmaic_admission_snapshot_statement",
    "build_projection_lag_snapshot_statement",
    "get_teaching_metrics_repository",
]
