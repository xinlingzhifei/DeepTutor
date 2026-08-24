from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deeptutor.teaching.health import REQUIRED_HEALTH_COMPONENTS, TeachingHealthService
from deeptutor.teaching.repositories.runtime_heartbeats import (
    SqlAlchemyRuntimeHeartbeatRepository,
)
from deeptutor.teaching.runtime_heartbeat import (
    RUNTIME_HEARTBEAT_PRUNE_BATCH_SIZE,
    RUNTIME_PROCESS_ROLES,
)
from deeptutor.teaching.schema_names import tenant_schema_name

ROOT = Path(__file__).resolve().parents[3]


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


async def _runtime_table_contract(database_url: str):
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'platform' AND table_name = "
                            "'teaching_runtime_process_heartbeats'"
                        )
                    )
                ).scalars()
            )
            constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE conrelid = "
                            "'platform.teaching_runtime_process_heartbeats'::regclass"
                        )
                    )
                ).scalars()
            )
            indexes = set(
                (
                    await connection.execute(
                        text(
                            "SELECT indexname FROM pg_indexes WHERE schemaname = "
                            "'platform' AND tablename = "
                            "'teaching_runtime_process_heartbeats'"
                        )
                    )
                ).scalars()
            )
        rejected = (
            {"role": "", "instance_id": "instance-a", "status": "running"},
            {"role": "dispatcher", "instance_id": "", "status": "running"},
            {"role": "not-a-runtime-role", "instance_id": "instance-b", "status": "running"},
            {"role": "dispatcher", "instance_id": "instance-c", "status": "invalid"},
            {"role": "dispatcher", "instance_id": "instance-d", "status": "stopped"},
        )
        for values in rejected:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text(
                            "INSERT INTO platform.teaching_runtime_process_heartbeats "
                            "(role, instance_id, status) VALUES "
                            "(:role, :instance_id, :status)"
                        ),
                        values,
                    )
                await transaction.rollback()
    finally:
        await engine.dispose()
    return columns, constraints, indexes


def test_runtime_heartbeat_migration_roundtrip_and_constraints(
    generation_database,
) -> None:
    downgraded = _run_platform_migration(
        generation_database,
        "downgrade",
        "20260810_0017",
    )
    try:
        assert downgraded.returncode == 0, f"{downgraded.stdout}\n{downgraded.stderr}"

        async def table_is_absent() -> bool:
            engine = create_async_engine(generation_database.url)
            try:
                async with engine.connect() as connection:
                    return not bool(
                        await connection.scalar(
                            text(
                                "SELECT to_regclass("
                                "'platform.teaching_runtime_process_heartbeats') IS NOT NULL"
                            )
                        )
                    )
            finally:
                await engine.dispose()

        assert asyncio.run(table_is_absent())
    finally:
        upgraded = _run_platform_migration(generation_database, "upgrade", "head")
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"

    columns, constraints, indexes = asyncio.run(_runtime_table_contract(generation_database.url))
    assert columns == {
        "role",
        "instance_id",
        "started_at",
        "heartbeat_at",
        "status",
        "stopped_at",
        "updated_at",
    }
    assert {
        "pk_teaching_runtime_process_heartbeats",
        "ck_teaching_runtime_process_heartbeats_role_not_empty",
        "ck_teaching_runtime_process_heartbeats_role",
        "ck_teaching_runtime_process_heartbeats_instance_id_not_empty",
        "ck_teaching_runtime_process_heartbeats_status",
        "ck_teaching_runtime_process_heartbeats_status_stopped_at",
        "ck_teaching_runtime_process_heartbeats_timestamps",
    }.issubset(constraints)
    assert {
        "ix_teaching_runtime_process_heartbeats_role_heartbeat_running",
        "ix_teaching_runtime_process_heartbeats_heartbeat_running_ttl",
        "ix_teaching_runtime_process_heartbeats_stopped_at_retention",
    }.issubset(indexes)


@pytest.mark.asyncio
async def test_runtime_heartbeat_repository_fences_concurrent_instances(
    generation_database,
) -> None:
    engine = create_async_engine(generation_database.url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = SqlAlchemyRuntimeHeartbeatRepository(session_factory)
    first = "dispatcher:11111111111111111111111111111111"
    second = "dispatcher:22222222222222222222222222222222"
    third = "dispatcher:33333333333333333333333333333333"

    async def report_from_repository():
        service = TeachingHealthService(
            now=lambda: datetime(2099, 1, 1, tzinfo=timezone.utc),
            stale_after_seconds=90,
        )
        for component in REQUIRED_HEALTH_COMPONENTS:
            service.set_status(component, "healthy")
        service.set_heartbeat("dispatcher")
        return await service.report_durable(repository)

    try:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE platform.teaching_runtime_process_heartbeats"))

        await asyncio.gather(
            repository.register("dispatcher", first),
            repository.register("dispatcher", second),
            *(
                repository.register(role, f"{role}:{'4' * 32}")
                for role in RUNTIME_PROCESS_ROLES
                if role != "dispatcher"
            ),
        )
        assert await repository.heartbeat("dispatcher", second)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE platform.teaching_runtime_process_heartbeats "
                    "SET started_at = now() - interval '121 seconds', "
                    "heartbeat_at = now() - interval '120 seconds', "
                    "updated_at = now() WHERE role = 'dispatcher' "
                    "AND instance_id = :instance_id"
                ),
                {"instance_id": first},
            )

        healthy = await report_from_repository()
        assert healthy.status == "healthy"
        assert healthy.components["dispatcher"].status == "healthy"

        assert await repository.mark_stopped("dispatcher", first)
        assert not await repository.heartbeat("dispatcher", first)
        assert not await repository.mark_stopped("dispatcher", first)

        snapshots = await repository.latest_running_heartbeats(("dispatcher",))
        assert len(snapshots) == 1
        assert snapshots[0].role == "dispatcher"
        assert 0 <= snapshots[0].age_seconds < 90

        await repository.register("dispatcher", third)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE platform.teaching_runtime_process_heartbeats "
                    "SET started_at = now() - interval '121 seconds', "
                    "heartbeat_at = now() - interval '120 seconds', "
                    "updated_at = now() WHERE role = 'dispatcher' "
                    "AND instance_id = :instance_id"
                ),
                {"instance_id": third},
            )
        assert await repository.mark_stopped("dispatcher", second)

        stale = await report_from_repository()
        assert stale.status == "degraded"
        assert stale.components["dispatcher"].status == "stale"

        assert await repository.mark_stopped("dispatcher", third)
        unavailable = await report_from_repository()
        assert unavailable.status == "degraded"
        assert unavailable.components["dispatcher"].status == "unknown"
        assert unavailable.components["dispatcher"].reason == "heartbeat_missing"

        async with engine.connect() as connection:
            statuses = (
                await connection.execute(
                    text(
                        "SELECT instance_id, status FROM "
                        "platform.teaching_runtime_process_heartbeats "
                        "WHERE role = 'dispatcher' ORDER BY instance_id"
                    )
                )
            ).all()
        assert statuses == [
            (first, "stopped"),
            (second, "stopped"),
            (third, "stopped"),
        ]

        old_instance = "dispatcher:expired-0001"
        old_count = RUNTIME_HEARTBEAT_PRUNE_BATCH_SIZE + 17
        fresh_instance = "dispatcher:fresh-retention"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.teaching_runtime_process_heartbeats "
                    "(role, instance_id, started_at, heartbeat_at, status, "
                    "stopped_at, updated_at) "
                    "SELECT 'dispatcher', "
                    "'dispatcher:expired-' || lpad(series::text, 4, '0'), "
                    "now() - interval '9 days', now() - interval '8 days', "
                    "CASE WHEN series % 2 = 0 THEN 'stopped' ELSE 'running' END, "
                    "CASE WHEN series % 2 = 0 THEN now() - interval '8 days' "
                    "ELSE NULL END, now() - interval '8 days' "
                    "FROM generate_series(1, :old_count) AS series"
                ),
                {"old_count": old_count},
            )
        await repository.register("dispatcher", fresh_instance)

        async with engine.connect() as connection:
            retained_after_first_prune = await connection.scalar(
                text(
                    "SELECT count(*) FROM platform.teaching_runtime_process_heartbeats "
                    "WHERE instance_id LIKE 'dispatcher:expired-%'"
                )
            )
            fresh_status = await connection.scalar(
                text(
                    "SELECT status FROM platform.teaching_runtime_process_heartbeats "
                    "WHERE role = 'dispatcher' AND instance_id = :instance_id"
                ),
                {"instance_id": fresh_instance},
            )
        assert retained_after_first_prune == 17
        assert fresh_status == "running"
        assert not await repository.heartbeat("dispatcher", old_instance)
        assert not await repository.mark_stopped("dispatcher", old_instance)

        await repository.register("dispatcher", "dispatcher:second-prune")
        async with engine.connect() as connection:
            retained_after_second_prune = await connection.scalar(
                text(
                    "SELECT count(*) FROM platform.teaching_runtime_process_heartbeats "
                    "WHERE instance_id LIKE 'dispatcher:expired-%'"
                )
            )
        assert retained_after_second_prune == 0
    finally:
        await engine.dispose()


def test_runtime_heartbeat_migration_keeps_tenant_revision_in_sync(
    generation_database,
) -> None:
    tenant_id = "runtime-heartbeat-migration"
    schema_name = tenant_schema_name(tenant_id)
    tenant_upgrade = _run_tenant_migration(
        generation_database,
        schema_name,
        "upgrade",
        "20260810_0017",
    )
    assert tenant_upgrade.returncode == 0, f"{tenant_upgrade.stdout}\n{tenant_upgrade.stderr}"

    async def seed_state() -> None:
        engine = create_async_engine(generation_database.url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO platform.tenants "
                        "(id, name, status, data_plane_mode) VALUES "
                        "(:tenant_id, 'Runtime heartbeat migration', 'active', 'shared')"
                    ),
                    {"tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO platform.tenant_schema_states "
                        "(tenant_id, schema_name, revision, status) VALUES "
                        "(:tenant_id, :schema_name, '20260810_0017', 'active')"
                    ),
                    {"tenant_id": tenant_id, "schema_name": schema_name},
                )
        finally:
            await engine.dispose()

    async def revisions() -> tuple[str, str]:
        engine = create_async_engine(generation_database.url)
        try:
            async with engine.connect() as connection:
                alembic_revision = await connection.scalar(
                    text(f'SELECT version_num FROM "{schema_name}".alembic_version')
                )
                state_revision = await connection.scalar(
                    text(
                        "SELECT revision FROM platform.tenant_schema_states "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
            return str(alembic_revision), str(state_revision)
        finally:
            await engine.dispose()

    asyncio.run(seed_state())
    upgraded = _run_tenant_migration(
        generation_database,
        schema_name,
        "upgrade",
        "head",
    )
    assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"
    assert asyncio.run(revisions()) == ("20260824_0018", "20260824_0018")

    downgraded = _run_tenant_migration(
        generation_database,
        schema_name,
        "downgrade",
        "20260810_0017",
    )
    assert downgraded.returncode == 0, f"{downgraded.stdout}\n{downgraded.stderr}"
    assert asyncio.run(revisions()) == ("20260810_0017", "20260810_0017")
