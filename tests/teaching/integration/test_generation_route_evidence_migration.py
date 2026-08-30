from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.teaching.schema_names import tenant_schema_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIOUS_REVISION = "20260828_0022"
ROUTE_EVIDENCE_REVISION = "20260830_0023"


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
        cwd=PROJECT_ROOT,
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
        cwd=PROJECT_ROOT,
        env=database.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _assert_succeeded(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"


def _attempt_insert() -> str:
    return (
        "INSERT INTO platform.generation_route_attempts ("
        "tenant_id, job_id, attempt_count, phase, decision, data_plane_mode, "
        "data_plane_route_id, provider_profile_id, worker_pool_ref, queue_ref, worker_id"
        ") VALUES ("
        ":tenant_id, :job_id, :attempt_count, :phase, :decision, :data_plane_mode, "
        ":data_plane_route_id, :provider_profile_id, :worker_pool_ref, :queue_ref, "
        ":worker_id)"
    )


def test_platform_generation_route_attempts_are_constrained_append_only_and_downgrade_safe(
    generation_database,
) -> None:
    tenant_id = f"route-attempt-{uuid.uuid4().hex[:12]}"
    values = {
        "tenant_id": tenant_id,
        "job_id": "job-1",
        "attempt_count": 1,
        "phase": "content",
        "decision": "selected",
        "data_plane_mode": "dedicated",
        "data_plane_route_id": "route-1",
        "provider_profile_id": "profile-1",
        "worker_pool_ref": "workers-1",
        "queue_ref": "queue-1",
        "worker_id": "worker-1",
    }

    async def exercise() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO platform.tenants "
                        "(id, name, status, data_plane_mode) "
                        "VALUES (:tenant_id, 'Route attempt tenant', 'active', 'dedicated')"
                    ),
                    {"tenant_id": tenant_id},
                )

            invalid_values = (
                {**values, "attempt_count": 0},
                {**values, "phase": "invalid"},
                {**values, "decision": "invalid"},
                {**values, "data_plane_mode": "invalid"},
                {**values, "worker_id": " "},
                {**values, "tenant_id": "missing-tenant"},
            )
            for invalid in invalid_values:
                async with engine.connect() as connection:
                    transaction = await connection.begin()
                    with pytest.raises(DBAPIError):
                        await connection.execute(text(_attempt_insert()), invalid)
                    await transaction.rollback()

            async with engine.begin() as connection:
                await connection.execute(text(_attempt_insert()), values)

            async with engine.connect() as connection:
                transaction = await connection.begin()
                with pytest.raises(DBAPIError):
                    await connection.execute(text(_attempt_insert()), values)
                await transaction.rollback()

            mutations = (
                "UPDATE platform.generation_route_attempts "
                "SET decision = 'unavailable' WHERE tenant_id = :tenant_id",
                "DELETE FROM platform.generation_route_attempts WHERE tenant_id = :tenant_id",
                "TRUNCATE TABLE platform.generation_route_attempts",
            )
            for statement in mutations:
                async with engine.connect() as connection:
                    transaction = await connection.begin()
                    with pytest.raises(DBAPIError):
                        await connection.execute(text(statement), {"tenant_id": tenant_id})
                    await transaction.rollback()
        finally:
            await engine.dispose()

    async def remove_durable_fact_for_fixture_recovery() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE platform.generation_route_attempts DISABLE TRIGGER "
                        "generation_route_attempts_append_only"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE platform.generation_route_attempts DISABLE TRIGGER "
                        "generation_route_attempts_append_only_truncate"
                    )
                )
                await connection.execute(
                    text(
                        "DELETE FROM platform.generation_route_attempts "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "ALTER TABLE platform.generation_route_attempts ENABLE TRIGGER "
                        "generation_route_attempts_append_only"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE platform.generation_route_attempts ENABLE TRIGGER "
                        "generation_route_attempts_append_only_truncate"
                    )
                )
        finally:
            await engine.dispose()

    async def platform_table_exists() -> bool:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return bool(
                    await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                            "WHERE table_schema = 'platform' "
                            "AND table_name = 'generation_route_attempts')"
                        )
                    )
                )
        finally:
            await engine.dispose()

    async def cleanup_tenant() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(exercise())
        blocked = _run_platform_migration(
            generation_database,
            "downgrade",
            PREVIOUS_REVISION,
        )
        assert blocked.returncode != 0
        assert "durable facts exist" in f"{blocked.stdout}\n{blocked.stderr}"

        asyncio.run(remove_durable_fact_for_fixture_recovery())
        downgraded = _run_platform_migration(
            generation_database,
            "downgrade",
            PREVIOUS_REVISION,
        )
        _assert_succeeded(downgraded)
        assert asyncio.run(platform_table_exists()) is False
    finally:
        upgraded = _run_platform_migration(generation_database, "upgrade", "head")
        _assert_succeeded(upgraded)
        asyncio.run(cleanup_tenant())


@pytest.mark.parametrize(
    "legacy_status",
    ("queued", "generating_content"),
    ids=("queued", "leased"),
)
def test_tenant_generation_route_binding_upgrade_is_fail_closed_and_immutable(
    generation_database,
    legacy_status: str,
) -> None:
    tenant_id = f"route-binding-{legacy_status[:6]}-{uuid.uuid4().hex[:6]}"
    schema_name = tenant_schema_name(tenant_id)
    quoted_schema = f'"{schema_name}"'
    migrated = _run_tenant_migration(
        generation_database,
        schema_name,
        "upgrade",
        PREVIOUS_REVISION,
    )
    _assert_succeeded(migrated)

    async def seed_legacy_nonterminal() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO platform.tenants "
                        "(id, name, status, data_plane_mode) "
                        "VALUES (:tenant_id, 'Route binding tenant', 'active', 'shared')"
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
                        f"INSERT INTO {quoted_schema}.generation_jobs ("
                        "id, tenant_id, job_kind, phase, status, priority, quota_units, "
                        "actor_id, owner_id, visibility, request_id, idempotency_key, "
                        "request_sha256, data_plane_route_id, provider_profile_id, "
                        "worker_pool_ref, queue_ref, request_payload, progress_percent, "
                        "attempt_count, lease_owner, lease_token, lease_expires_at, heartbeat_at"
                        ") VALUES ("
                        "'legacy-job', :tenant_id, 'generation', 'content', :legacy_status, "
                        "0, 1, 'teacher-1', 'teacher-1', 'private', 'legacy-request', "
                        "'legacy-idempotency', :sha, 'legacy-route', 'legacy-profile', "
                        "'legacy-workers', 'legacy-queue', '{}', 0, :attempt_count, "
                        ":lease_owner, :lease_token, :lease_expires_at, :heartbeat_at)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "sha": "a" * 64,
                        "legacy_status": legacy_status,
                        "attempt_count": 1 if legacy_status == "generating_content" else 0,
                        "lease_owner": (
                            "legacy-worker" if legacy_status == "generating_content" else None
                        ),
                        "lease_token": (
                            "legacy-lease" if legacy_status == "generating_content" else None
                        ),
                        "lease_expires_at": (
                            datetime.now(UTC) + timedelta(minutes=5)
                            if legacy_status == "generating_content"
                            else None
                        ),
                        "heartbeat_at": (
                            datetime.now(UTC) if legacy_status == "generating_content" else None
                        ),
                    },
                )
                queue_claimed = legacy_status == "generating_content"
                await connection.execute(
                    text(
                        "INSERT INTO platform.generation_queue ("
                        "tenant_id, job_id, job_kind, phase, data_plane_route_id, "
                        "provider_profile_id, worker_pool_ref, queue_ref, slot_pool, "
                        "priority, status, claimed_at, lease_owner, lease_token, "
                        "lease_expires_at, heartbeat_at) VALUES ("
                        ":tenant_id, 'legacy-job', 'generation', 'content', "
                        "'legacy-route', 'legacy-profile', 'legacy-workers', "
                        "'legacy-queue', 'generation', 0, :queue_status, :claimed_at, "
                        ":lease_owner, :lease_token, :lease_expires_at, :heartbeat_at)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "queue_status": "claimed" if queue_claimed else "queued",
                        "claimed_at": datetime.now(UTC) if queue_claimed else None,
                        "lease_owner": "legacy-worker" if queue_claimed else None,
                        "lease_token": "legacy-lease" if queue_claimed else None,
                        "lease_expires_at": (
                            datetime.now(UTC) + timedelta(minutes=5) if queue_claimed else None
                        ),
                        "heartbeat_at": datetime.now(UTC) if queue_claimed else None,
                    },
                )
        finally:
            await engine.dispose()

    async def queued_row_status() -> str | None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(
                    text(
                        "SELECT status FROM platform.generation_queue "
                        "WHERE tenant_id = :tenant_id AND job_id = 'legacy-job'"
                    ),
                    {"tenant_id": tenant_id},
                )
                return str(value) if value is not None else None
        finally:
            await engine.dispose()

    async def remove_legacy_queue() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM platform.generation_queue "
                        "WHERE tenant_id = :tenant_id AND job_id = 'legacy-job'"
                    ),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()

    async def inspect_revision_and_mode_column() -> tuple[str, str, bool]:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                alembic_revision = await connection.scalar(
                    text(f"SELECT version_num FROM {quoted_schema}.alembic_version")
                )
                state_revision = await connection.scalar(
                    text(
                        "SELECT revision FROM platform.tenant_schema_states "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
                mode_exists = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = :schema_name "
                        "AND table_name = 'generation_jobs' "
                        "AND column_name = 'data_plane_mode')"
                    ),
                    {"schema_name": schema_name},
                )
                return str(alembic_revision), str(state_revision), bool(mode_exists)
        finally:
            await engine.dispose()

    async def replace_with_terminal_legacy_job() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"UPDATE {quoted_schema}.generation_jobs "
                        "SET status = 'succeeded', progress_percent = 100, "
                        "lease_owner = NULL, lease_token = NULL, "
                        "lease_expires_at = NULL, heartbeat_at = NULL "
                        "WHERE id = 'legacy-job'"
                    )
                )
        finally:
            await engine.dispose()

    async def exercise_immutable_binding() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                legacy_mode = await connection.scalar(
                    text(
                        f"SELECT data_plane_mode FROM {quoted_schema}.generation_jobs "
                        "WHERE id = 'legacy-job'"
                    )
                )
                assert legacy_mode is None

            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"INSERT INTO {quoted_schema}.generation_jobs ("
                        "id, tenant_id, job_kind, phase, status, priority, quota_units, "
                        "actor_id, owner_id, visibility, request_id, idempotency_key, "
                        "request_sha256, data_plane_mode, data_plane_route_id, "
                        "provider_profile_id, worker_pool_ref, queue_ref, request_payload, "
                        "progress_percent) VALUES ("
                        "'bound-job', :tenant_id, 'generation', 'content', 'succeeded', "
                        "0, 1, 'teacher-1', 'teacher-1', 'private', 'bound-request', "
                        "'bound-idempotency', :sha, 'shared', 'route-1', 'profile-1', "
                        "'workers-1', 'queue-1', '{}', 100)"
                    ),
                    {"tenant_id": tenant_id, "sha": "b" * 64},
                )

            mutations = {
                "data_plane_mode": "dedicated",
                "data_plane_route_id": "route-2",
                "provider_profile_id": "profile-2",
                "worker_pool_ref": "workers-2",
                "queue_ref": "queue-2",
            }
            for column, value in mutations.items():
                async with engine.connect() as connection:
                    transaction = await connection.begin()
                    with pytest.raises(DBAPIError):
                        await connection.execute(
                            text(
                                f"UPDATE {quoted_schema}.generation_jobs "
                                f"SET {column} = :value WHERE id = 'bound-job'"
                            ),
                            {"value": value},
                        )
                    await transaction.rollback()
        finally:
            await engine.dispose()

    async def delete_bound_job() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"DELETE FROM {quoted_schema}.generation_jobs WHERE id = 'bound-job'")
                )
        finally:
            await engine.dispose()

    async def cleanup() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
                await connection.execute(
                    text("DELETE FROM platform.tenant_schema_states WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
                await connection.execute(
                    text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_legacy_nonterminal())
        blocked_upgrade = _run_tenant_migration(
            generation_database,
            schema_name,
            "upgrade",
            "head",
        )
        assert blocked_upgrade.returncode != 0
        assert "legacy nonterminal jobs" in (f"{blocked_upgrade.stdout}\n{blocked_upgrade.stderr}")
        assert asyncio.run(inspect_revision_and_mode_column()) == (
            PREVIOUS_REVISION,
            PREVIOUS_REVISION,
            False,
        )
        assert asyncio.run(queued_row_status()) == (
            "claimed" if legacy_status == "generating_content" else "queued"
        )

        asyncio.run(remove_legacy_queue())
        asyncio.run(replace_with_terminal_legacy_job())
        upgraded = _run_tenant_migration(
            generation_database,
            schema_name,
            "upgrade",
            "head",
        )
        _assert_succeeded(upgraded)
        assert asyncio.run(inspect_revision_and_mode_column()) == (
            ROUTE_EVIDENCE_REVISION,
            ROUTE_EVIDENCE_REVISION,
            True,
        )
        asyncio.run(exercise_immutable_binding())

        blocked_downgrade = _run_tenant_migration(
            generation_database,
            schema_name,
            "downgrade",
            PREVIOUS_REVISION,
        )
        assert blocked_downgrade.returncode != 0
        assert "job bindings exist" in (f"{blocked_downgrade.stdout}\n{blocked_downgrade.stderr}")

        asyncio.run(delete_bound_job())
        downgraded = _run_tenant_migration(
            generation_database,
            schema_name,
            "downgrade",
            PREVIOUS_REVISION,
        )
        _assert_succeeded(downgraded)
        assert asyncio.run(inspect_revision_and_mode_column()) == (
            PREVIOUS_REVISION,
            PREVIOUS_REVISION,
            False,
        )
    finally:
        asyncio.run(cleanup())


def test_platform_downgrade_rejects_physically_upgraded_tenant_without_state_row(
    generation_database,
) -> None:
    tenant_id = f"route-untracked-{uuid.uuid4().hex[:10]}"
    schema_name = tenant_schema_name(tenant_id)

    async def seed_untracked_upgraded_schema() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO platform.tenants "
                        "(id, name, status, data_plane_mode) "
                        "VALUES (:tenant_id, 'Untracked route schema', 'active', 'shared')"
                    ),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()

        generation_database.migrate_tenant(tenant_id)

        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                mode_column_exists = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = :schema_name "
                        "AND table_name = 'generation_jobs' "
                        "AND column_name = 'data_plane_mode')"
                    ),
                    {"schema_name": schema_name},
                )
                assert mode_column_exists is True
                await connection.execute(
                    text("DELETE FROM platform.tenant_schema_states WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()

    async def cleanup() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
                await connection.execute(
                    text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_untracked_upgraded_schema())

        blocked = _run_platform_migration(
            generation_database,
            "downgrade",
            PREVIOUS_REVISION,
        )

        assert blocked.returncode != 0
        assert "downgrade tenant schemas before generation route evidence" in (
            f"{blocked.stdout}\n{blocked.stderr}"
        )
    finally:
        upgraded = _run_platform_migration(generation_database, "upgrade", "head")
        _assert_succeeded(upgraded)
        asyncio.run(cleanup())
