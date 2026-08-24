from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import math

import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.teaching.repositories import metric_rollups as metric_rollup_repository
from deeptutor.teaching.repositories.metric_rollups import (
    MetricRollupConsistencyError,
    delete_learning_projection_backlog,
    increment_counter_rollup,
    insert_learning_projection_backlog,
    metric_rollup_shard,
    observe_histogram_rollup,
)


class _Result:
    def __init__(self, row=None) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _Session:
    def __init__(self, results=()) -> None:
        self.statements = []
        self._results = iter(results)

    async def execute(self, statement):
        self.statements.append(statement)
        return next(self._results, _Result())

    def begin(self):
        raise AssertionError("metric rollup helpers must not begin transactions")

    async def commit(self):
        raise AssertionError("metric rollup helpers must not commit transactions")

    async def flush(self):
        raise AssertionError("metric rollup helpers must not flush transactions")


def _compiled(statement):
    return statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )


def _sql(statement) -> str:
    return " ".join(str(_compiled(statement)).lower().split())


@pytest.mark.asyncio
async def test_counter_rollup_batch_aggregates_and_locks_in_full_key_order() -> None:
    batch_helper = getattr(metric_rollup_repository, "increment_counter_rollups", None)
    observation_type = getattr(metric_rollup_repository, "CounterRollupObservation", None)
    assert batch_helper is not None and observation_type is not None
    session = _Session()

    observations = [
        observation_type(
            metric="learning_events_total",
            category="scene.completed",
            fact_key="event-private-2",
            amount=1,
        ),
        observation_type(
            metric="generation_jobs_total",
            category="completed",
            fact_key="job-private-1",
            amount=2,
        ),
        observation_type(
            metric="generation_jobs_total",
            category="completed",
            fact_key="job-private-1",
            amount=3,
        ),
    ]

    await batch_helper(session, observations=observations)

    compiled = [_compiled(statement) for statement in session.statements]
    lock_keys = [
        (params.params["metric"], params.params["category"], params.params["shard"])
        for params in compiled
    ]
    assert lock_keys == sorted(lock_keys)
    assert len(compiled) == 2
    assert (
        next(
            params.params["total"]
            for params in compiled
            if params.params["metric"] == "generation_jobs_total"
        )
        == 5
    )


@pytest.mark.asyncio
async def test_counter_rollup_batch_validates_every_observation_before_writing() -> None:
    session = _Session()

    with pytest.raises(ValueError, match="metric rollup input is invalid"):
        await metric_rollup_repository.increment_counter_rollups(
            session,
            observations=(
                metric_rollup_repository.CounterRollupObservation(
                    metric="generation_jobs_total",
                    category="completed",
                    fact_key="job-private-1",
                    amount=1,
                ),
                metric_rollup_repository.CounterRollupObservation(
                    metric="generation_jobs_total",
                    category="private",
                    fact_key="job-private-2",
                    amount=1,
                ),
            ),
        )

    assert session.statements == []


@pytest.mark.parametrize(
    ("fact_key", "metric", "category"),
    (
        ("job-1", "generation_jobs_total", "completed"),
        ("event-1", "learning_events_total", "quiz.graded"),
        ("artifact-1", "artifact_validation_failures_total", "hash_mismatch"),
    ),
)
def test_metric_rollup_shard_is_stable_sha256_and_internal(
    fact_key: str,
    metric: str,
    category: str,
) -> None:
    digest = hashlib.sha256(f"{fact_key}\0{metric}\0{category}".encode()).digest()
    expected = int.from_bytes(digest, "big") % 16

    assert metric_rollup_shard(fact_key, metric, category) == expected
    assert metric_rollup_shard(fact_key, metric, category) == expected
    assert 0 <= expected <= 15


@pytest.mark.asyncio
async def test_counter_rollup_is_one_atomic_additive_upsert() -> None:
    session = _Session()

    await increment_counter_rollup(
        session,
        metric="generation_retries_total",
        category="lease_lost",
        fact_key="job-private-1",
        amount=3,
    )

    assert len(session.statements) == 1
    statement = session.statements[0]
    compiled = _compiled(statement)
    sql = _sql(statement)
    assert "on conflict (metric, category, shard) do update" in sql
    assert "total = (platform.teaching_metric_counter_rollups.total +" in sql
    assert "updated_at = now()" in sql
    assert compiled.params["metric"] == "generation_retries_total"
    assert compiled.params["category"] == "lease_lost"
    assert compiled.params["total"] == 3
    assert compiled.params["shard"] == metric_rollup_shard(
        "job-private-1",
        "generation_retries_total",
        "lease_lost",
    )
    assert "job-private-1" not in repr(compiled.params)


@pytest.mark.parametrize(
    ("metric", "category", "seconds", "expected_bucket"),
    (
        ("generation_queue_seconds", "", 0.0, "0.1"),
        ("generation_queue_seconds", "", 0.1, "0.1"),
        ("generation_queue_seconds", "", 0.100001, "0.5"),
        ("generation_queue_seconds", "", 300.0, "300"),
        ("generation_queue_seconds", "", 300.001, "+Inf"),
        ("generation_stage_seconds", "outline", 0.5, "0.5"),
        ("generation_stage_seconds", "content", 0.500001, "1"),
        ("generation_stage_seconds", "export", 1800.0, "1800"),
        ("generation_stage_seconds", "export", 1800.001, "+Inf"),
    ),
)
@pytest.mark.asyncio
async def test_histogram_rollup_selects_one_fixed_noncumulative_bin(
    metric: str,
    category: str,
    seconds: float,
    expected_bucket: str,
) -> None:
    session = _Session()

    await observe_histogram_rollup(
        session,
        metric=metric,
        category=category,
        fact_key="observation-private-1",
        seconds=seconds,
    )

    assert len(session.statements) == 1
    statement = session.statements[0]
    compiled = _compiled(statement)
    sql = _sql(statement)
    assert compiled.params["bucket"] == expected_bucket
    assert compiled.params["count"] == 1
    assert compiled.params["sum_seconds"] == seconds
    assert "on conflict (metric, category, bucket, shard) do update" in sql
    assert "count = (platform.teaching_metric_histogram_rollups.count +" in sql
    assert "sum_seconds = (platform.teaching_metric_histogram_rollups.sum_seconds +" in sql
    assert "updated_at = now()" in sql
    assert "observation-private-1" not in repr(compiled.params)


@pytest.mark.asyncio
async def test_backlog_insert_and_terminal_delete_are_exact() -> None:
    received_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC)
    session = _Session((_Result(), _Result(("tenant-a", "event-a"))))

    await insert_learning_projection_backlog(
        session,
        tenant_id="tenant-a",
        event_id="event-a",
        received_at=received_at,
    )
    await delete_learning_projection_backlog(
        session,
        tenant_id="tenant-a",
        event_id="event-a",
    )

    insert_statement, delete_statement = session.statements
    insert_compiled = _compiled(insert_statement)
    insert_sql = _sql(insert_statement)
    delete_compiled = _compiled(delete_statement)
    delete_sql = _sql(delete_statement)
    assert "insert into platform.teaching_learning_projection_backlog" in insert_sql
    assert "on conflict" not in insert_sql
    assert insert_compiled.params == {
        "tenant_id": "tenant-a",
        "event_id": "event-a",
        "received_at": received_at,
    }
    assert "delete from platform.teaching_learning_projection_backlog" in delete_sql
    assert "tenant_id =" in delete_sql
    assert "event_id =" in delete_sql
    assert "returning platform.teaching_learning_projection_backlog.tenant_id" in delete_sql
    assert "platform.teaching_learning_projection_backlog.event_id" in delete_sql
    assert delete_compiled.params["tenant_id_1"] == "tenant-a"
    assert delete_compiled.params["event_id_1"] == "event-a"


@pytest.mark.asyncio
async def test_terminal_backlog_delete_fails_closed_when_row_is_missing() -> None:
    session = _Session((_Result(None),))

    with pytest.raises(
        MetricRollupConsistencyError,
        match="learning projection backlog row is missing",
    ):
        await delete_learning_projection_backlog(
            session,
            tenant_id="tenant-a",
            event_id="event-a",
        )

    assert len(session.statements) == 1


@pytest.mark.parametrize(
    "kwargs",
    (
        {
            "metric": "private_metric",
            "category": "completed",
            "fact_key": "job-1",
            "amount": 1,
        },
        {
            "metric": [],
            "category": "completed",
            "fact_key": "job-1",
            "amount": 1,
        },
        {
            "metric": "generation_jobs_total",
            "category": "private",
            "fact_key": "job-1",
            "amount": 1,
        },
        {
            "metric": "generation_jobs_total",
            "category": "completed",
            "fact_key": "job-1",
            "amount": 0,
        },
        {
            "metric": "generation_jobs_total",
            "category": "completed",
            "fact_key": "job-1",
            "amount": True,
        },
        {
            "metric": "generation_jobs_total",
            "category": "completed",
            "fact_key": "   ",
            "amount": 1,
        },
    ),
)
@pytest.mark.asyncio
async def test_counter_rollup_rejects_invalid_input(kwargs) -> None:
    session = _Session()

    with pytest.raises(ValueError, match="metric rollup input is invalid"):
        await increment_counter_rollup(session, **kwargs)

    assert session.statements == []


@pytest.mark.parametrize(
    "kwargs",
    (
        {
            "metric": "private_metric",
            "category": "",
            "fact_key": "fact-1",
            "seconds": 1.0,
        },
        {
            "metric": [],
            "category": "",
            "fact_key": "fact-1",
            "seconds": 1.0,
        },
        {
            "metric": "generation_stage_seconds",
            "category": "private",
            "fact_key": "fact-1",
            "seconds": 1.0,
        },
        {
            "metric": "generation_queue_seconds",
            "category": "",
            "fact_key": "fact-1",
            "seconds": math.nan,
        },
        {
            "metric": "generation_queue_seconds",
            "category": "",
            "fact_key": "fact-1",
            "seconds": math.inf,
        },
        {
            "metric": "generation_queue_seconds",
            "category": "",
            "fact_key": "fact-1",
            "seconds": 10**1000,
        },
        {
            "metric": "generation_queue_seconds",
            "category": "",
            "fact_key": "fact-1",
            "seconds": -1.0,
        },
        {
            "metric": "generation_queue_seconds",
            "category": "",
            "fact_key": "fact-1",
            "seconds": True,
        },
        {
            "metric": "generation_queue_seconds",
            "category": "",
            "fact_key": "",
            "seconds": 1.0,
        },
    ),
)
@pytest.mark.asyncio
async def test_histogram_rollup_rejects_invalid_input(kwargs) -> None:
    session = _Session()

    with pytest.raises(ValueError, match="metric rollup input is invalid"):
        await observe_histogram_rollup(session, **kwargs)

    assert session.statements == []


@pytest.mark.parametrize(
    ("helper", "kwargs"),
    (
        (
            insert_learning_projection_backlog,
            {
                "tenant_id": "",
                "event_id": "event-a",
                "received_at": datetime(2026, 8, 25, tzinfo=UTC),
            },
        ),
        (
            insert_learning_projection_backlog,
            {
                "tenant_id": "tenant-a",
                "event_id": "   ",
                "received_at": datetime(2026, 8, 25, tzinfo=UTC),
            },
        ),
        (
            insert_learning_projection_backlog,
            {
                "tenant_id": "tenant-a",
                "event_id": "event-a",
                "received_at": datetime(2026, 8, 25),
            },
        ),
        (
            delete_learning_projection_backlog,
            {"tenant_id": " ", "event_id": "event-a"},
        ),
    ),
)
@pytest.mark.asyncio
async def test_backlog_helpers_reject_invalid_input(helper, kwargs) -> None:
    session = _Session()

    with pytest.raises(ValueError, match="learning projection backlog input is invalid"):
        await helper(session, **kwargs)

    assert session.statements == []


@pytest.mark.asyncio
async def test_metric_rollup_helpers_never_own_the_transaction() -> None:
    session = _Session((_Result(), _Result(), _Result(), _Result(("tenant-a", "event-a"))))
    received_at = datetime(2026, 8, 25, tzinfo=UTC)

    await increment_counter_rollup(
        session,
        metric="generation_jobs_total",
        category="completed",
        fact_key="job-1",
        amount=1,
    )
    await observe_histogram_rollup(
        session,
        metric="generation_queue_seconds",
        category="",
        fact_key="job-1",
        seconds=1.0,
    )
    await insert_learning_projection_backlog(
        session,
        tenant_id="tenant-a",
        event_id="event-a",
        received_at=received_at,
    )
    await delete_learning_projection_backlog(
        session,
        tenant_id="tenant-a",
        event_id="event-a",
    )

    assert len(session.statements) == 4
