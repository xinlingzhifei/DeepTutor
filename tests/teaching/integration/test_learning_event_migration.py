from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
import importlib
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.teaching.schema_names import tenant_schema_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIOUS_REVISION = "20260809_0015"
LEARNING_REVISION = "20260810_0016"
LEARNING_TABLES = {
    "learning_sessions",
    "learning_events",
    "learning_projection_queue",
    "quiz_attempts",
    "mastery_evidence",
    "mastery_levels",
    "learning_progress",
    "learning_event_quarantine",
}


def _run_tenant_migration(database, schema_name: str, command: str, target: str):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            "scope=tenant",
            "-x",
            f"tenant_schema={schema_name}",
            command,
            target,
        ],
        cwd=PROJECT_ROOT,
        env=database.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


async def _register_tenant(engine, tenant_id: str, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO platform.tenants "
                "(id, name, status, data_plane_mode) "
                "VALUES (:tenant_id, 'Learning tenant', 'active', 'shared')"
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


async def _cleanup_tenant(engine, tenant_id: str, schema_name: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM platform.audit_log WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        await connection.execute(DropSchema(schema_name, cascade=True))
        await connection.execute(
            text("DELETE FROM platform.tenant_schema_states WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
            {"tenant_id": tenant_id},
        )


async def _seed_learning_session(
    engine,
    *,
    tenant_id: str,
    schema_name: str,
    session_id: str = "session-1",
) -> None:
    sha = "a" * 64
    quoted_schema = f'"{schema_name}"'
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.courses (id, title) "
                "VALUES ('course-1', 'Learning course')"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.classes (id, course_id, name) "
                "VALUES ('class-1', 'course-1', 'Learning class')"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.generation_jobs ("
                "id, tenant_id, job_kind, phase, status, priority, quota_units, "
                "actor_id, owner_id, visibility, request_id, idempotency_key, "
                "resource_course_id, resource_class_id, request_sha256, "
                "data_plane_route_id, provider_profile_id, worker_pool_ref, "
                "queue_ref, request_payload, progress_percent"
                ") VALUES ("
                "'job-1', :tenant_id, 'generation', 'content', 'succeeded', 0, 1, "
                "'teacher-1', 'teacher-1', 'class', 'request-1', 'idempotency-1', "
                "'course-1', 'class-1', :sha, 'route-1', 'provider-1', "
                "'workers-1', 'queue-1', '{}', 100"
                ")"
            ),
            {"tenant_id": tenant_id, "sha": sha},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.classroom_assets "
                "(id, tenant_id, owner_id, title, lifecycle_state) "
                "VALUES ('asset-1', :tenant_id, 'teacher-1', "
                "'Learning classroom', 'published')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.classroom_versions ("
                "id, tenant_id, classroom_id, version_number, generation_job_id, "
                "document_sha256, media_manifest_sha256, document_object_key"
                ") VALUES ("
                "'version-1', :tenant_id, 'asset-1', 1, 'job-1', :sha, :sha, "
                "'tenants/example/classrooms/asset-1/version-1/classroom.json'"
                ")"
            ),
            {"tenant_id": tenant_id, "sha": sha},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.assignments "
                "(id, tenant_id, classroom_version_id, class_id, assigned_by) "
                "VALUES ('assignment-1', :tenant_id, 'version-1', 'class-1', "
                "'teacher-1')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.learning_sessions ("
                "id, tenant_id, user_id, classroom_version_id, assignment_id, status"
                ") VALUES ("
                ":session_id, :tenant_id, 'student-1', 'version-1', "
                "'assignment-1', 'active'"
                ")"
            ),
            {"session_id": session_id, "tenant_id": tenant_id},
        )


def _event(
    repository_module,
    event_id: str,
    *,
    tenant_id: str,
    user_id: str = "student-1",
    session_id: str = "session-1",
):
    return repository_module.LearningEventAppend(
        event_id=event_id,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user_id,
        classroom_version_id="version-1",
        event_type="scene.completed",
        occurred_at=datetime.now(UTC),
        scene_id="scene-1",
        knowledge_point_id="kp-1",
        payload={"schema_version": "1.0", "scene_id": "scene-1"},
    )


@pytest.mark.asyncio
async def test_learning_migration_matches_orm_constraints_indexes_and_revision(
    generation_database,
) -> None:
    assert (
        PROJECT_ROOT / "deeptutor/teaching/migrations/versions/20260810_0016_learning_events.py"
    ).is_file()
    tenant_id = f"learning-schema-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        previous = _run_tenant_migration(
            generation_database, schema_name, "upgrade", PREVIOUS_REVISION
        )
        assert previous.returncode == 0, f"{previous.stdout}\n{previous.stderr}"
        await _register_tenant(engine, tenant_id, schema_name)

        upgraded = _run_tenant_migration(
            generation_database, schema_name, "upgrade", LEARNING_REVISION
        )
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"

        async with engine.connect() as connection:
            tables = set(
                await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema_name"
                    ),
                    {"schema_name": schema_name},
                )
            )
            migrated_columns = (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = :schema_name "
                        "AND table_name = ANY(:table_names)"
                    ),
                    {"schema_name": schema_name, "table_names": list(LEARNING_TABLES)},
                )
            ).all()
            constraint_names = set(
                await connection.scalars(
                    text(
                        "SELECT con.conname FROM pg_constraint con "
                        "JOIN pg_class rel ON rel.oid = con.conrelid "
                        "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
                        "WHERE ns.nspname = :schema_name "
                        "AND rel.relname = ANY(:table_names)"
                    ),
                    {"schema_name": schema_name, "table_names": list(LEARNING_TABLES)},
                )
            )
            event_indexes = set(
                await connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = :schema_name "
                        "AND tablename = 'learning_events'"
                    ),
                    {"schema_name": schema_name},
                )
            )
            revision = await connection.scalar(
                text(f'SELECT version_num FROM "{schema_name}".alembic_version')
            )
            state_revision = await connection.scalar(
                text(
                    "SELECT revision FROM platform.tenant_schema_states "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )

        models = importlib.import_module("deeptutor.teaching.models.learning")
        expected_models = (
            models.LearningSession,
            models.LearningEvent,
            models.LearningProjectionQueueItem,
            models.QuizAttempt,
            models.MasteryEvidence,
            models.MasteryLevel,
            models.LearningProgress,
            models.LearningEventQuarantine,
        )
        expected_tables = {model.__table__.name: model.__table__ for model in expected_models}
        actual_columns: dict[str, set[str]] = defaultdict(set)
        actual_nullable: dict[tuple[str, str], bool] = {}
        for table_name, column_name, is_nullable in migrated_columns:
            actual_columns[table_name].add(column_name)
            actual_nullable[(table_name, column_name)] = is_nullable == "YES"

        assert LEARNING_TABLES.issubset(tables)
        assert dict(actual_columns) == {
            table_name: set(table.c.keys()) for table_name, table in expected_tables.items()
        }
        assert actual_nullable == {
            (table_name, column.name): column.nullable
            for table_name, table in expected_tables.items()
            for column in table.c
        }
        assert {
            "uq_learning_events_event_id",
            "uq_learning_events_session_seq",
            "uq_quiz_attempts_event_id",
            "uq_mastery_evidence_event_id",
            "uq_mastery_levels_user_knowledge",
            "ck_learning_sessions_authority",
        }.issubset(constraint_names)
        assert {
            "ix_learning_events_event_type",
            "ix_learning_events_occurred_at",
            "ix_learning_events_session_id",
            "ix_learning_events_classroom_version_id",
            "ix_learning_events_knowledge_point_id",
        }.issubset(event_indexes)
        assert (revision, state_revision) == (LEARNING_REVISION, LEARNING_REVISION)
    finally:
        await _cleanup_tenant(engine, tenant_id, schema_name)
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_learning_tables_can_downgrade_to_previous_revision(
    generation_database,
) -> None:
    tenant_id = f"learning-empty-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        previous = _run_tenant_migration(
            generation_database, schema_name, "upgrade", PREVIOUS_REVISION
        )
        assert previous.returncode == 0, f"{previous.stdout}\n{previous.stderr}"
        await _register_tenant(engine, tenant_id, schema_name)
        upgraded = _run_tenant_migration(
            generation_database, schema_name, "upgrade", LEARNING_REVISION
        )
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"

        downgraded = _run_tenant_migration(
            generation_database, schema_name, "downgrade", PREVIOUS_REVISION
        )
        assert downgraded.returncode == 0, f"{downgraded.stdout}\n{downgraded.stderr}"

        async with engine.connect() as connection:
            tables = set(
                await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema_name"
                    ),
                    {"schema_name": schema_name},
                )
            )
            revision = await connection.scalar(
                text(f'SELECT version_num FROM "{schema_name}".alembic_version')
            )
            state_revision = await connection.scalar(
                text(
                    "SELECT revision FROM platform.tenant_schema_states "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
        assert LEARNING_TABLES.isdisjoint(tables)
        assert (revision, state_revision) == (PREVIOUS_REVISION, PREVIOUS_REVISION)
    finally:
        await _cleanup_tenant(engine, tenant_id, schema_name)
        await engine.dispose()


@pytest.mark.asyncio
async def test_repository_appends_idempotently_in_order_and_blocks_fact_downgrade(
    generation_database,
) -> None:
    tenant_id = f"learning-events-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        previous = _run_tenant_migration(
            generation_database, schema_name, "upgrade", PREVIOUS_REVISION
        )
        assert previous.returncode == 0, f"{previous.stdout}\n{previous.stderr}"
        await _register_tenant(engine, tenant_id, schema_name)
        upgraded = _run_tenant_migration(
            generation_database, schema_name, "upgrade", LEARNING_REVISION
        )
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"
        await _seed_learning_session(
            engine,
            tenant_id=tenant_id,
            schema_name=schema_name,
        )

        repository_module = importlib.import_module(
            "deeptutor.teaching.repositories.learning_events"
        )
        repository = repository_module.SqlAlchemyLearningEventRepository(engine, tenant_id)

        event_a = _event(repository_module, "event-a", tenant_id=tenant_id)
        first = await repository.append(event_a)
        duplicate = await repository.append(event_a)

        event_b = _event(repository_module, "event-b", tenant_id=tenant_id)
        second = await repository.append(event_b)

        assert (first.outcome, first.seq) == ("accepted", 1)
        assert (duplicate.outcome, duplicate.seq) == ("duplicate", 1)
        assert (second.outcome, second.seq) == ("accepted", 2)
        assert await repository.count_events("session-1") == 2
        assert await repository.count_projection_items("session-1") == 2

        concurrent_commands = []
        for event_id in ("event-c", "event-d"):
            concurrent_commands.append(_event(repository_module, event_id, tenant_id=tenant_id))
        concurrent = await asyncio.gather(
            *(repository.append(command) for command in concurrent_commands)
        )
        assert sorted(result.seq for result in concurrent) == [3, 4]
        assert await repository.count_events("session-1") == 4
        assert await repository.count_projection_items("session-1") == 4

        wrong_user = _event(
            repository_module,
            "event-wrong",
            tenant_id=tenant_id,
            user_id="student-2",
        )
        with pytest.raises(repository_module.LearningEventBindingError):
            await repository.append(wrong_user)

        wrong_tenant = _event(
            repository_module,
            "event-other-tenant",
            tenant_id="tenant-placeholder",
        )
        with pytest.raises(repository_module.LearningEventBindingError):
            await repository.append(wrong_tenant)

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".learning_sessions ('
                    "id, tenant_id, user_id, classroom_version_id, assignment_id, status"
                    ") VALUES ("
                    "'session-2', :tenant_id, 'student-1', 'version-1', "
                    "'assignment-1', 'active'"
                    ")"
                ),
                {"tenant_id": tenant_id},
            )
        wrong_session = _event(
            repository_module,
            "event-a",
            tenant_id=tenant_id,
            session_id="session-2",
        )
        with pytest.raises(repository_module.LearningEventBindingError):
            await repository.append(wrong_session)

        refused = _run_tenant_migration(
            generation_database, schema_name, "downgrade", PREVIOUS_REVISION
        )
        assert refused.returncode != 0
        assert "cannot downgrade learning events: durable facts exist" in (
            refused.stdout + refused.stderr
        )
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text(f'SELECT version_num FROM "{schema_name}".alembic_version')
            )
            state_revision = await connection.scalar(
                text(
                    "SELECT revision FROM platform.tenant_schema_states "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
        assert (revision, state_revision) == (LEARNING_REVISION, LEARNING_REVISION)
    finally:
        await _cleanup_tenant(engine, tenant_id, schema_name)
        await engine.dispose()
