from __future__ import annotations

from contextlib import asynccontextmanager
import math

import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.teaching.metrics import TeachingMetrics
from deeptutor.teaching.models import (
    TeachingLearningProjectionBacklog,
    TeachingMetricCounterRollup,
    TeachingMetricHistogramRollup,
)
from deeptutor.teaching.repositories.metrics import (
    SqlAlchemyTeachingMetricsRepository,
    build_counter_snapshot_statement,
    build_generation_slots_snapshot_statement,
    build_histogram_snapshot_statement,
    build_openmaic_admission_snapshot_statement,
    build_projection_lag_snapshot_statement,
)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


def test_platform_metric_models_enforce_fixed_domains_and_internal_shards() -> None:
    counter = TeachingMetricCounterRollup.__table__
    histogram = TeachingMetricHistogramRollup.__table__
    backlog = TeachingLearningProjectionBacklog.__table__
    counter_checks = " ".join(
        str(constraint.sqltext)
        for constraint in counter.constraints
        if hasattr(constraint, "sqltext")
    )
    histogram_checks = " ".join(
        str(constraint.sqltext)
        for constraint in histogram.constraints
        if hasattr(constraint, "sqltext")
    )

    assert {column.name for column in counter.columns} == {
        "metric",
        "category",
        "shard",
        "total",
        "updated_at",
    }
    assert {column.name for column in counter.primary_key.columns} == {
        "metric",
        "category",
        "shard",
    }
    assert "shard BETWEEN 0 AND 15" in counter_checks
    assert "total >= 0" in counter_checks
    for metric in (
        "generation_jobs_total",
        "generation_retries_total",
        "quota_units_total",
        "learning_events_total",
        "artifact_validation_failures_total",
    ):
        assert metric in counter_checks

    assert {column.name for column in histogram.columns} == {
        "metric",
        "category",
        "bucket",
        "shard",
        "count",
        "sum_seconds",
        "updated_at",
    }
    assert {column.name for column in histogram.primary_key.columns} == {
        "metric",
        "category",
        "bucket",
        "shard",
    }
    assert "shard BETWEEN 0 AND 15" in histogram_checks
    assert "count >= 0" in histogram_checks
    assert "sum_seconds >= 0" in histogram_checks
    assert "generation_queue_seconds" in histogram_checks
    assert "generation_stage_seconds" in histogram_checks
    assert "+Inf" in histogram_checks

    assert {column.name for column in backlog.columns} == {
        "tenant_id",
        "event_id",
        "received_at",
    }
    assert {column.name for column in backlog.primary_key.columns} == {
        "tenant_id",
        "event_id",
    }
    assert {foreign_key.target_fullname for foreign_key in backlog.foreign_keys} == {
        "platform.tenants.id"
    }


def test_histogram_model_requires_empty_bins_to_have_zero_sum() -> None:
    histogram_checks = " ".join(
        str(constraint.sqltext)
        for constraint in TeachingMetricHistogramRollup.__table__.constraints
        if hasattr(constraint, "sqltext")
    )

    assert "count > 0 OR sum_seconds = 0" in histogram_checks


def test_metrics_snapshot_statements_use_fixed_aggregate_dimensions() -> None:
    counter_sql = _sql(build_counter_snapshot_statement())
    histogram_sql = _sql(build_histogram_snapshot_statement())
    slot_sql = _sql(build_generation_slots_snapshot_statement())
    lag_sql = _sql(build_projection_lag_snapshot_statement())

    assert "sum(platform.teaching_metric_counter_rollups.total)" in counter_sql
    assert "group by platform.teaching_metric_counter_rollups.metric" in counter_sql
    assert "teaching_metric_counter_rollups.shard" not in counter_sql.split("group by", 1)[1]
    assert "sum(platform.teaching_metric_histogram_rollups.count)" in histogram_sql
    assert "sum(platform.teaching_metric_histogram_rollups.sum_seconds)" in histogram_sql
    assert "teaching_metric_histogram_rollups.shard" not in histogram_sql.split("group by", 1)[1]
    assert "platform.generation_slots.scope = 'global'" in slot_sql
    assert "platform.generation_slots.claimed_job_id is not null" in slot_sql
    assert (
        "platform.generation_slots.tenant_id"
        not in slot_sql.split("select", 1)[1].split("from", 1)[0]
    )
    assert "platform.teaching_learning_projection_backlog.received_at" in lag_sql
    assert "now()" in lag_sql
    assert "greatest" in lag_sql


def test_openmaic_admission_snapshot_uses_complete_active_binding_without_secrets() -> None:
    sql = _sql(build_openmaic_admission_snapshot_statement())
    shared_sql, dedicated_sql = sql.split("union all", 1)

    assert "platform.tenants.status = 'active'" in sql
    assert "platform.tenants.data_plane_mode" in sql
    assert "platform.data_plane_routes.status = 'active'" in sql
    assert "platform.data_plane_routes.health_status = 'healthy'" in sql
    assert "platform.provider_profiles.status = 'active'" in sql
    assert "platform.provider_profiles.scope = platform.data_plane_routes.mode" in sql
    assert "platform.provider_profiles.owner_key = platform.data_plane_routes.owner_key" in sql
    assert "platform.provider_profiles.secret_ref" not in sql
    assert "'shared'" in sql
    assert "'dedicated'" in sql
    assert ") > 0 and (exists" in shared_sql
    assert ") = 0" not in shared_sql
    assert ") = 0" in dedicated_sql


class _Result:
    def __init__(self, rows=(), scalar=None) -> None:
        self._rows = tuple(rows)
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


class _Transaction:
    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        self._session.transaction_entries += 1

    async def __aexit__(self, *_args):
        self._session.transaction_exits += 1


class _Session:
    def __init__(self, results) -> None:
        self.results = iter(results)
        self.statements = []
        self.transaction_entries = 0
        self.transaction_exits = 0

    def begin(self):
        return _Transaction(self)

    async def execute(self, statement):
        self.statements.append(statement)
        return next(self.results)


def _repository(results):
    session = _Session(results)

    @asynccontextmanager
    async def session_factory():
        yield session

    return SqlAlchemyTeachingMetricsRepository(session_factory), session


@pytest.mark.asyncio
async def test_repository_fetches_one_read_only_absolute_snapshot() -> None:
    repository, session = _repository(
        (
            _Result(),
            _Result(
                (
                    {"metric": "generation_retries_total", "category": "timeout", "total": 4},
                    {"metric": "generation_jobs_total", "category": "completed", "total": 7},
                )
            ),
            _Result(
                (
                    {
                        "metric": "generation_queue_seconds",
                        "category": "",
                        "bucket": "0.5",
                        "count": 3,
                        "sum_seconds": 1.2,
                    },
                )
            ),
            _Result(({"category": "generation", "value": 2},)),
            _Result(scalar=8.5),
            _Result(
                (
                    {"category": "shared", "value": 1},
                    {"category": "dedicated", "value": 1},
                )
            ),
        )
    )

    snapshot = await repository.fetch_snapshot()
    payload = TeachingMetrics().render(snapshot).decode("utf-8")

    assert session.transaction_entries == session.transaction_exits == 1
    assert len(session.statements) == 6
    transaction_sql = str(session.statements[0]).upper()
    assert "SET TRANSACTION" in transaction_sql
    assert "REPEATABLE READ" in transaction_sql
    assert "READ ONLY" in transaction_sql
    assert 'yfeistai_generation_retries_total{reason="timeout"} 4.0' in payload
    assert 'yfeistai_generation_jobs_total{status="completed"} 7.0' in payload
    assert 'yfeistai_generation_slots_in_use{pool="generation"} 2.0' in payload
    assert "yfeistai_learning_projection_lag_seconds 8.5" in payload
    assert 'yfeistai_openmaic_health{mode="dedicated"} 1.0' in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counter_rows,histogram_rows,slot_rows,lag,admission_rows",
    [
        (
            (
                {"metric": "generation_retries_total", "category": "timeout", "total": 1},
                {"metric": "generation_retries_total", "category": "timeout", "total": 2},
            ),
            (),
            (),
            0,
            ({"category": "shared", "value": 1}, {"category": "dedicated", "value": 1}),
        ),
        (
            ({"metric": "private", "category": "private", "total": 1},),
            (),
            (),
            0,
            ({"category": "shared", "value": 1}, {"category": "dedicated", "value": 1}),
        ),
        (
            (),
            (
                {
                    "metric": "generation_queue_seconds",
                    "category": "",
                    "bucket": "0.1",
                    "count": 1,
                    "sum_seconds": math.inf,
                },
            ),
            (),
            0,
            ({"category": "shared", "value": 1}, {"category": "dedicated", "value": 1}),
        ),
        (
            (),
            (),
            ({"category": "generation", "value": -1},),
            0,
            ({"category": "shared", "value": 1}, {"category": "dedicated", "value": 1}),
        ),
        (
            (),
            (),
            (),
            math.nan,
            ({"category": "shared", "value": 1}, {"category": "dedicated", "value": 1}),
        ),
        (
            (),
            (),
            (),
            0,
            ({"category": "shared", "value": 1},),
        ),
    ],
)
async def test_repository_fails_closed_on_invalid_database_snapshot_rows(
    counter_rows,
    histogram_rows,
    slot_rows,
    lag,
    admission_rows,
) -> None:
    repository, _session = _repository(
        (
            _Result(),
            _Result(counter_rows),
            _Result(histogram_rows),
            _Result(slot_rows),
            _Result(scalar=lag),
            _Result(admission_rows),
        )
    )

    with pytest.raises(ValueError, match="metrics snapshot is invalid"):
        await repository.fetch_snapshot()
