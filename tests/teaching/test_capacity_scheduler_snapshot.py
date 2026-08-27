from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.teaching.repositories.capacity_scheduler import (
    SqlAlchemyCapacitySchedulerRepository,
    build_capacity_queue_snapshot_statement,
    build_capacity_slot_snapshot_statement,
    build_generation_claim_event_statement,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


def test_capacity_snapshot_statements_are_bounded_to_requested_jobs_and_their_pools() -> None:
    queue_sql = _sql(build_capacity_queue_snapshot_statement(("job-a", "job-b")))
    slot_sql = _sql(
        build_capacity_slot_snapshot_statement(
            worker_pool_refs=("pool-a",),
            tenant_ids=("tenant-a", "tenant-b"),
        )
    )
    event_sql = _sql(build_generation_claim_event_statement(("job-a", "job-b")))

    assert "platform.generation_queue.job_id in ('job-a', 'job-b')" in queue_sql
    assert "platform.generation_queue.slot_pool = 'generation'" in queue_sql
    assert "lease_token" not in queue_sql.split("from", 1)[0]
    assert "platform.generation_slots.worker_pool_ref in ('pool-a')" in slot_sql
    assert "platform.generation_slots.scope = 'global'" in slot_sql
    assert "platform.generation_slots.owner_key in ('tenant-a', 'tenant-b')" in slot_sql
    assert "lease_token" not in slot_sql.split("from", 1)[0]
    assert "platform.audit_log.action = 'generation.job_claimed'" in event_sql
    assert "platform.audit_log.resource_type = 'generation_job'" in event_sql
    assert "platform.audit_log.resource_id in ('job-a', 'job-b')" in event_sql
    assert "actor_id" not in event_sql.split("from", 1)[0]


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

    return SqlAlchemyCapacitySchedulerRepository(session_factory), session


@pytest.mark.asyncio
async def test_repository_reads_one_repeatable_read_only_scheduler_snapshot() -> None:
    repository, session = _repository(
        (
            _Result(),
            _Result(scalar=NOW),
            _Result(
                (
                    {
                        "job_id": "job-a",
                        "tenant_id": "tenant-a",
                        "worker_pool_ref": "pool-a",
                        "status": "claimed",
                        "claimed_at": NOW,
                    },
                    {
                        "job_id": "job-b",
                        "tenant_id": "tenant-b",
                        "worker_pool_ref": "pool-a",
                        "status": "queued",
                        "claimed_at": None,
                    },
                )
            ),
            _Result(
                (
                    {
                        "cursor": 41,
                        "job_id": "job-b",
                        "tenant_id": "tenant-b",
                        "claimed_at": NOW,
                    },
                    {
                        "cursor": 42,
                        "job_id": "job-a",
                        "tenant_id": "tenant-a",
                        "claimed_at": NOW,
                    },
                )
            ),
            _Result(
                (
                    {
                        "worker_pool_ref": "pool-a",
                        "scope": "global",
                        "owner_key": "shared",
                        "ordinal": 0,
                        "claimed_job_id": "job-a",
                        "claimed_tenant_id": "tenant-a",
                    },
                    {
                        "worker_pool_ref": "pool-a",
                        "scope": "global",
                        "owner_key": "shared",
                        "ordinal": 1,
                        "claimed_job_id": None,
                        "claimed_tenant_id": None,
                    },
                    {
                        "worker_pool_ref": "pool-a",
                        "scope": "tenant",
                        "owner_key": "tenant-a",
                        "ordinal": 0,
                        "claimed_job_id": "job-a",
                        "claimed_tenant_id": "tenant-a",
                    },
                    {
                        "worker_pool_ref": "pool-a",
                        "scope": "tenant",
                        "owner_key": "tenant-a",
                        "ordinal": 1,
                        "claimed_job_id": None,
                        "claimed_tenant_id": None,
                    },
                    {
                        "worker_pool_ref": "pool-a",
                        "scope": "tenant",
                        "owner_key": "tenant-b",
                        "ordinal": 0,
                        "claimed_job_id": None,
                        "claimed_tenant_id": None,
                    },
                    {
                        "worker_pool_ref": "pool-a",
                        "scope": "tenant",
                        "owner_key": "tenant-b",
                        "ordinal": 1,
                        "claimed_job_id": None,
                        "claimed_tenant_id": None,
                    },
                )
            ),
        )
    )

    snapshot = await repository.fetch_snapshot(("job-b", "job-a"))

    assert session.transaction_entries == session.transaction_exits == 1
    assert len(session.statements) == 5
    transaction_sql = str(session.statements[0]).upper()
    assert "SET TRANSACTION" in transaction_sql
    assert "REPEATABLE READ" in transaction_sql
    assert "READ ONLY" in transaction_sql
    assert snapshot.observed_at == NOW
    assert snapshot.missing_job_ids == ()
    assert [(event.cursor, event.job_id) for event in snapshot.claim_events] == [
        (41, "job-b"),
        (42, "job-a"),
    ]
    assert [(job.job_id, job.status) for job in snapshot.jobs] == [
        ("job-a", "claimed"),
        ("job-b", "queued"),
    ]
    assert len(snapshot.pools) == 1
    pool = snapshot.pools[0]
    assert pool.worker_pool_ref == "pool-a"
    assert pool.global_slot_capacity == 2
    assert [(item.tenant_id, item.capacity) for item in pool.tenant_capacities] == [
        ("tenant-a", 2),
        ("tenant-b", 2),
    ]
    assert [(claim.job_id, claim.tenant_id, claim.ordinal) for claim in pool.active] == [
        ("job-a", "tenant-a", 0)
    ]


@pytest.mark.asyncio
async def test_repository_reports_missing_jobs_without_querying_unrelated_pools() -> None:
    repository, session = _repository(
        (
            _Result(),
            _Result(scalar=NOW),
            _Result(()),
            _Result(()),
        )
    )

    snapshot = await repository.fetch_snapshot(("job-missing",))

    assert snapshot.jobs == ()
    assert snapshot.pools == ()
    assert snapshot.missing_job_ids == ("job-missing",)
    assert snapshot.claim_events == ()
    assert len(session.statements) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_ids",
    [(), ("job-a", "job-a"), ("",), ("job with spaces",), tuple(f"job-{i}" for i in range(129))],
)
async def test_repository_rejects_invalid_job_id_sets_before_database_access(job_ids) -> None:
    accessed = False

    @asynccontextmanager
    async def session_factory():
        nonlocal accessed
        accessed = True
        yield

    repository = SqlAlchemyCapacitySchedulerRepository(session_factory)

    with pytest.raises(ValueError, match="job ids"):
        await repository.fetch_snapshot(job_ids)
    assert accessed is False
