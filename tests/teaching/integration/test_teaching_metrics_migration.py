from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.teaching.repositories.metrics import SqlAlchemyTeachingMetricsRepository
from deeptutor.teaching.schema_names import tenant_schema_name

ROOT = Path(__file__).resolve().parents[3]
PREVIOUS_REVISION = "20260824_0018"
METRICS_REVISION = "20260825_0019"


def _run_platform_migration(database, action: str, revision: str):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            "scope=platform",
            action,
            revision,
        ],
        cwd=ROOT,
        env=database.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _run_tenant_migration(database, schema_name: str, action: str, revision: str):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            "scope=tenant",
            "-x",
            f"tenant_schema={schema_name}",
            action,
            revision,
        ],
        cwd=ROOT,
        env=database.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


async def _platform_tables(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return set(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'platform'"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()


def test_metrics_platform_migration_constraints_and_cross_connection_visibility(
    generation_database,
) -> None:
    downgraded = _run_platform_migration(
        generation_database,
        "downgrade",
        PREVIOUS_REVISION,
    )
    assert downgraded.returncode == 0, f"{downgraded.stdout}\n{downgraded.stderr}"
    try:
        tables = asyncio.run(_platform_tables(generation_database.url))
        assert {
            "teaching_metric_counter_rollups",
            "teaching_metric_histogram_rollups",
            "teaching_learning_projection_backlog",
        }.isdisjoint(tables)
    finally:
        upgraded = _run_platform_migration(generation_database, "upgrade", "head")
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"

    async def exercise() -> None:
        writer = create_async_engine(generation_database.url, poolclass=NullPool)
        reader = create_async_engine(generation_database.url, poolclass=NullPool)
        repository = SqlAlchemyTeachingMetricsRepository(
            async_sessionmaker(reader, expire_on_commit=False)
        )
        try:
            async with writer.begin() as connection:
                await connection.execute(
                    text("DELETE FROM platform.teaching_metric_counter_rollups")
                )
                await connection.execute(
                    text("DELETE FROM platform.teaching_metric_histogram_rollups")
                )

            connection = await writer.connect()
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO platform.teaching_metric_counter_rollups "
                    "(metric, category, shard, total) VALUES "
                    "('generation_retries_total', 'timeout', 0, 4)"
                )
            )
            before_commit = await repository.fetch_snapshot()
            assert before_commit.counters == ()
            await transaction.commit()
            await connection.close()

            after_commit = await repository.fetch_snapshot()
            assert [(row.metric, row.category, row.total) for row in after_commit.counters] == [
                ("generation_retries_total", "timeout", 4)
            ]

            connection = await writer.connect()
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO platform.teaching_metric_counter_rollups "
                    "(metric, category, shard, total) VALUES "
                    "('generation_retries_total', 'timeout', 1, 3)"
                )
            )
            await transaction.rollback()
            await connection.close()
            after_rollback = await repository.fetch_snapshot()
            assert after_rollback.counters[0].total == 4

            async with writer.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO platform.teaching_metric_counter_rollups "
                        "(metric, category, shard, total) VALUES "
                        "('generation_retries_total', 'timeout', 1, 3)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO platform.teaching_metric_histogram_rollups "
                        "(metric, category, bucket, shard, count, sum_seconds) VALUES "
                        "('generation_queue_seconds', '', '0.5', 0, 2, 0.7)"
                    )
                )
            visible = await repository.fetch_snapshot()
            assert visible.counters[0].total == 7
            assert visible.histograms[0].count == 2
            assert visible.histograms[0].sum_seconds == pytest.approx(0.7)

            invalid_statements = (
                "INSERT INTO platform.teaching_metric_counter_rollups "
                "(metric, category, shard, total) VALUES ('private', 'private', 0, 1)",
                "INSERT INTO platform.teaching_metric_counter_rollups "
                "(metric, category, shard, total) VALUES "
                "('generation_retries_total', 'timeout', 16, 1)",
                "INSERT INTO platform.teaching_metric_counter_rollups "
                "(metric, category, shard, total) VALUES "
                "('generation_retries_total', 'timeout', 2, -1)",
                "INSERT INTO platform.teaching_metric_histogram_rollups "
                "(metric, category, bucket, shard, count, sum_seconds) VALUES "
                "('generation_queue_seconds', '', 'private', 0, 1, 1)",
                "INSERT INTO platform.teaching_metric_histogram_rollups "
                "(metric, category, bucket, shard, count, sum_seconds) VALUES "
                "('generation_queue_seconds', '', '0.1', 0, 0, 0.1)",
                "INSERT INTO platform.teaching_metric_histogram_rollups "
                "(metric, category, bucket, shard, count, sum_seconds) VALUES "
                "('generation_queue_seconds', '', '0.1', 0, 1, 'NaN')",
                "INSERT INTO platform.teaching_metric_histogram_rollups "
                "(metric, category, bucket, shard, count, sum_seconds) VALUES "
                "('generation_queue_seconds', '', '0.1', 0, 1, 'Infinity')",
                "INSERT INTO platform.teaching_metric_histogram_rollups "
                "(metric, category, bucket, shard, count, sum_seconds) VALUES "
                "('generation_queue_seconds', '', '0.1', 0, 1, '-Infinity')",
            )
            for statement in invalid_statements:
                async with writer.connect() as invalid_connection:
                    invalid_transaction = await invalid_connection.begin()
                    with pytest.raises(DBAPIError):
                        await invalid_connection.execute(text(statement))
                    await invalid_transaction.rollback()
        finally:
            await writer.dispose()
            await reader.dispose()

    asyncio.run(exercise())


async def _seed_projection_backlog(
    database_url: str,
    *,
    tenant_id: str,
    schema_name: str,
) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    quoted_schema = f'"{schema_name}"'
    received_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.tenants "
                    "(id, name, status, data_plane_mode) "
                    "VALUES (:tenant_id, 'Metrics tenant', 'active', 'shared')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.tenant_schema_states "
                    "(tenant_id, schema_name, revision, status) "
                    "VALUES (:tenant_id, :schema_name, :revision, 'active')"
                ),
                {
                    "tenant_id": tenant_id,
                    "schema_name": schema_name,
                    "revision": PREVIOUS_REVISION,
                },
            )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted_schema}.courses (id, title) VALUES ('course-1', 'Course')"
                )
            )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted_schema}.classes (id, course_id, name) "
                    "VALUES ('class-1', 'course-1', 'Class')"
                )
            )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted_schema}.generation_jobs ("
                    "id, tenant_id, job_kind, phase, status, priority, quota_units, "
                    "actor_id, owner_id, visibility, request_id, idempotency_key, "
                    "resource_course_id, resource_class_id, request_sha256, "
                    "data_plane_route_id, provider_profile_id, worker_pool_ref, queue_ref, "
                    "request_payload, progress_percent) VALUES ("
                    "'job-1', :tenant_id, 'generation', 'content', 'succeeded', 0, 1, "
                    "'teacher-1', 'teacher-1', 'class', 'request-1', 'idempotency-1', "
                    "'course-1', 'class-1', :sha, 'route-1', 'profile-1', 'workers-1', "
                    "'queue-1', '{}', 100)"
                ),
                {"tenant_id": tenant_id, "sha": "a" * 64},
            )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted_schema}.classroom_assets "
                    "(id, tenant_id, owner_id, title, lifecycle_state) VALUES "
                    "('asset-1', :tenant_id, 'teacher-1', 'Classroom', 'published')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted_schema}.classroom_versions ("
                    "id, tenant_id, classroom_id, version_number, generation_job_id, "
                    "document_sha256, media_manifest_sha256, document_object_key) VALUES ("
                    "'version-1', :tenant_id, 'asset-1', 1, 'job-1', :sha, :sha, "
                    "'tenants/example/classrooms/asset-1/version-1/classroom.json')"
                ),
                {"tenant_id": tenant_id, "sha": "a" * 64},
            )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted_schema}.assignments "
                    "(id, tenant_id, classroom_version_id, class_id, assigned_by) VALUES "
                    "('assignment-1', :tenant_id, 'version-1', 'class-1', 'teacher-1')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted_schema}.learning_sessions "
                    "(id, tenant_id, user_id, classroom_version_id, assignment_id, status) "
                    "VALUES ('session-1', :tenant_id, 'student-1', 'version-1', "
                    "'assignment-1', 'active')"
                ),
                {"tenant_id": tenant_id},
            )
            statuses = ("pending", "running", "failed", "completed", "quarantined")
            for sequence, status in enumerate(statuses, start=1):
                event_id = f"event-{status}"
                await connection.execute(
                    text(
                        f"INSERT INTO {quoted_schema}.learning_events ("
                        "event_id, tenant_id, session_id, user_id, classroom_version_id, seq, "
                        "event_type, occurred_at, payload, received_at) VALUES ("
                        ":event_id, :tenant_id, 'session-1', 'student-1', 'version-1', "
                        ":sequence, 'classroom.started', :received_at, '{}', :received_at)"
                    ),
                    {
                        "event_id": event_id,
                        "tenant_id": tenant_id,
                        "sequence": sequence,
                        "received_at": received_at,
                    },
                )
                if status == "running":
                    await connection.execute(
                        text(
                            f"INSERT INTO {quoted_schema}.learning_projection_queue ("
                            "event_id, tenant_id, session_id, status, attempt_count, "
                            "lease_owner, lease_token, lease_expires_at, heartbeat_at) VALUES ("
                            ":event_id, :tenant_id, 'session-1', 'running', 1, 'worker-1', "
                            "'token-1', now() + interval '1 minute', now())"
                        ),
                        {"event_id": event_id, "tenant_id": tenant_id},
                    )
                else:
                    await connection.execute(
                        text(
                            f"INSERT INTO {quoted_schema}.learning_projection_queue "
                            "(event_id, tenant_id, session_id, status, attempt_count) VALUES "
                            "(:event_id, :tenant_id, 'session-1', :status, 1)"
                        ),
                        {"event_id": event_id, "tenant_id": tenant_id, "status": status},
                    )
    finally:
        await engine.dispose()


def test_tenant_metrics_migration_backfills_and_removes_nonterminal_backlog(
    generation_database,
) -> None:
    tenant_id = "metrics-migration-tenant"
    schema_name = tenant_schema_name(tenant_id)
    tenant_base = _run_tenant_migration(
        generation_database,
        schema_name,
        "upgrade",
        PREVIOUS_REVISION,
    )
    assert tenant_base.returncode == 0, f"{tenant_base.stdout}\n{tenant_base.stderr}"
    asyncio.run(
        _seed_projection_backlog(
            generation_database.url,
            tenant_id=tenant_id,
            schema_name=schema_name,
        )
    )

    async def inspect() -> tuple[str, str, set[str], set[datetime]]:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                tenant_revision = await connection.scalar(
                    text(f'SELECT version_num FROM "{schema_name}".alembic_version')
                )
                ledger_revision = await connection.scalar(
                    text(
                        "SELECT revision FROM platform.tenant_schema_states "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
                rows = (
                    await connection.execute(
                        text(
                            "SELECT event_id, received_at "
                            "FROM platform.teaching_learning_projection_backlog "
                            "WHERE tenant_id = :tenant_id"
                        ),
                        {"tenant_id": tenant_id},
                    )
                ).all()
            return (
                str(tenant_revision),
                str(ledger_revision),
                {str(row.event_id) for row in rows},
                {row.received_at for row in rows},
            )
        finally:
            await engine.dispose()

    async def legacy_enqueue_after_migration() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"INSERT INTO {schema_name}.learning_events ("
                        "event_id, tenant_id, session_id, user_id, classroom_version_id, seq, "
                        "event_type, occurred_at, payload, received_at) VALUES ("
                        "'event-legacy-after-upgrade', :tenant_id, 'session-1', 'student-1', "
                        "'version-1', 6, 'classroom.started', :received_at, '{}', :received_at)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "received_at": datetime(2026, 8, 25, 1, 2, 4, tzinfo=UTC),
                    },
                )
                await connection.execute(
                    text(
                        f"INSERT INTO {schema_name}.learning_projection_queue "
                        "(event_id, tenant_id, session_id, status, attempt_count) VALUES "
                        "('event-legacy-after-upgrade', :tenant_id, 'session-1', 'pending', 0)"
                    ),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()

    async def legacy_complete_after_migration() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"UPDATE {schema_name}.learning_projection_queue "
                        "SET status = 'completed' "
                        "WHERE event_id = 'event-legacy-after-upgrade'"
                    )
                )
        finally:
            await engine.dispose()

    async def legacy_rebind_after_migration() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            with pytest.raises(DBAPIError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            f"UPDATE {schema_name}.learning_projection_queue "
                            "SET tenant_id = 'another-tenant' "
                            "WHERE event_id = 'event-legacy-after-upgrade'"
                        )
                    )
        finally:
            await engine.dispose()

    try:
        upgraded = _run_tenant_migration(
            generation_database,
            schema_name,
            "upgrade",
            METRICS_REVISION,
        )
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"
        tenant_revision, ledger_revision, event_ids, received_at = asyncio.run(inspect())
        assert (tenant_revision, ledger_revision) == (METRICS_REVISION, METRICS_REVISION)
        assert event_ids == {"event-pending", "event-running", "event-failed"}
        assert received_at == {datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC)}

        asyncio.run(legacy_enqueue_after_migration())
        _tenant_revision, _ledger_revision, event_ids, _received_at = asyncio.run(inspect())
        assert event_ids == {
            "event-pending",
            "event-running",
            "event-failed",
            "event-legacy-after-upgrade",
        }

        asyncio.run(legacy_rebind_after_migration())
        _tenant_revision, _ledger_revision, event_ids, _received_at = asyncio.run(inspect())
        assert "event-legacy-after-upgrade" in event_ids

        asyncio.run(legacy_complete_after_migration())
        _tenant_revision, _ledger_revision, event_ids, _received_at = asyncio.run(inspect())
        assert event_ids == {"event-pending", "event-running", "event-failed"}

        downgraded = _run_tenant_migration(
            generation_database,
            schema_name,
            "downgrade",
            PREVIOUS_REVISION,
        )
        assert downgraded.returncode == 0, f"{downgraded.stdout}\n{downgraded.stderr}"
        tenant_revision, ledger_revision, event_ids, _received_at = asyncio.run(inspect())
        assert (tenant_revision, ledger_revision) == (PREVIOUS_REVISION, PREVIOUS_REVISION)
        assert event_ids == set()
    finally:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)

        async def cleanup() -> None:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
                await connection.execute(
                    text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
            await engine.dispose()

        asyncio.run(cleanup())


def test_metrics_migration_requires_tenant_first_platform_last_downgrade(
    generation_database,
) -> None:
    tenant_id = "metrics-downgrade-order-tenant"
    schema_name = tenant_schema_name(tenant_id)
    tenant_base = _run_tenant_migration(
        generation_database,
        schema_name,
        "upgrade",
        PREVIOUS_REVISION,
    )
    assert tenant_base.returncode == 0, f"{tenant_base.stdout}\n{tenant_base.stderr}"

    async def seed_state() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO platform.tenants "
                        "(id, name, status, data_plane_mode) "
                        "VALUES (:tenant_id, 'Metrics downgrade order', 'active', 'shared')"
                    ),
                    {"tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO platform.tenant_schema_states "
                        "(tenant_id, schema_name, revision, status) "
                        "VALUES (:tenant_id, :schema_name, :revision, 'active')"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "schema_name": schema_name,
                        "revision": PREVIOUS_REVISION,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO platform.tenant_provisioning_jobs ("
                        "id, tenant_id, operation, status, attempt_count, max_attempts, "
                        "lease_owner, lease_token, lease_expires_at, heartbeat_at, started_at"
                        ") VALUES ("
                        "'metrics-downgrade-gap-job', :tenant_id, 'provision', 'running', "
                        "1, 5, 'worker-1', 'lease-1', now() + interval '1 minute', now(), now()"
                        ")"
                    ),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_state())
    try:
        running_gap = _run_platform_migration(
            generation_database,
            "downgrade",
            PREVIOUS_REVISION,
        )
        assert running_gap.returncode != 0
        assert "complete tenant provisioning jobs before platform metrics" in (
            f"{running_gap.stdout}\n{running_gap.stderr}"
        )
        assert {
            "teaching_metric_counter_rollups",
            "teaching_metric_histogram_rollups",
            "teaching_learning_projection_backlog",
        }.issubset(asyncio.run(_platform_tables(generation_database.url)))

        async def complete_job() -> None:
            engine = create_async_engine(generation_database.url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE platform.tenant_provisioning_jobs "
                            "SET status = 'completed', lease_owner = NULL, lease_token = NULL, "
                            "lease_expires_at = NULL, heartbeat_at = NULL, completed_at = now() "
                            "WHERE id = 'metrics-downgrade-gap-job'"
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(complete_job())
        tenant_upgrade = _run_tenant_migration(
            generation_database,
            schema_name,
            "upgrade",
            METRICS_REVISION,
        )
        assert tenant_upgrade.returncode == 0, f"{tenant_upgrade.stdout}\n{tenant_upgrade.stderr}"

        platform_first = _run_platform_migration(
            generation_database,
            "downgrade",
            PREVIOUS_REVISION,
        )
        assert platform_first.returncode != 0
        assert "downgrade tenant schemas before platform metrics" in (
            f"{platform_first.stdout}\n{platform_first.stderr}"
        )
        assert {
            "teaching_metric_counter_rollups",
            "teaching_metric_histogram_rollups",
            "teaching_learning_projection_backlog",
        }.issubset(asyncio.run(_platform_tables(generation_database.url)))

        tenant_first = _run_tenant_migration(
            generation_database,
            schema_name,
            "downgrade",
            PREVIOUS_REVISION,
        )
        assert tenant_first.returncode == 0, f"{tenant_first.stdout}\n{tenant_first.stderr}"

        platform_last = _run_platform_migration(
            generation_database,
            "downgrade",
            PREVIOUS_REVISION,
        )
        assert platform_last.returncode == 0, f"{platform_last.stdout}\n{platform_last.stderr}"
        assert {
            "teaching_metric_counter_rollups",
            "teaching_metric_histogram_rollups",
            "teaching_learning_projection_backlog",
        }.isdisjoint(asyncio.run(_platform_tables(generation_database.url)))
    finally:
        platform_restore = _run_platform_migration(generation_database, "upgrade", "head")
        assert platform_restore.returncode == 0, (
            f"{platform_restore.stdout}\n{platform_restore.stderr}"
        )
        engine = create_async_engine(generation_database.url, poolclass=NullPool)

        async def cleanup() -> None:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
                await connection.execute(
                    text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
            await engine.dispose()

        asyncio.run(cleanup())
