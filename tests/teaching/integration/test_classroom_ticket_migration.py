from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import importlib
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.models import LearningSession
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.learning_sessions import (
    LearningSessionAuthorityError,
    LearningSessionService,
)
from deeptutor.teaching.tenant_context import TenantContext
from deeptutor.teaching.tickets import ClassroomTicketService

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIOUS_REVISION = "20260810_0016"
TICKET_REVISION = "20260810_0017"
TICKET_TABLE = "classroom_ticket_consumptions"


@dataclass(frozen=True, slots=True)
class LearningRuntime:
    database_url: str
    tenant_id: str
    schema_name: str
    secret_file: Path


def _context(runtime: LearningRuntime) -> TenantContext:
    return TenantContext(
        tenant_id=runtime.tenant_id,
        schema_name=runtime.schema_name,
        user_id="student-a",
        permissions=frozenset(),
    )


def _runtime_service(runtime: LearningRuntime):
    engine = create_async_engine(runtime.database_url, poolclass=NullPool)
    ticket_service = ClassroomTicketService.from_settings(
        PlatformSettings(classroom_ticket_secret_file=runtime.secret_file)
    )
    return (
        engine,
        ticket_service,
        LearningSessionService(
            engine=engine,
            ticket_service=ticket_service,
        ),
    )


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
                "VALUES (:tenant_id, 'Ticket tenant', 'active', 'shared')"
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


async def _seed_assignment_session(
    engine,
    *,
    tenant_id: str,
    schema_name: str,
) -> None:
    sha = "a" * 64
    quoted_schema = f'"{schema_name}"'
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.courses (id, title) "
                "VALUES ('course-1', 'Ticket course')"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.classes (id, course_id, name) "
                "VALUES ('class-1', 'course-1', 'Ticket class')"
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
                "'Ticket classroom', 'published')"
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
                f"INSERT INTO {quoted_schema}.learning_sessions "
                "(id, tenant_id, user_id, classroom_version_id, assignment_id) "
                "VALUES ('session-1', :tenant_id, 'student-1', 'version-1', "
                "'assignment-1')"
            ),
            {"tenant_id": tenant_id},
        )


async def _seed_learning_runtime_authorities(
    engine,
    *,
    tenant_id: str,
    schema_name: str,
) -> None:
    quoted_schema = f'"{schema_name}"'
    sha = "b" * 64
    class_rows = (
        "('class-valid', 'course-1', 'Valid class', 'active'), "
        "('class-revoked', 'course-1', 'Revoked class', 'active'), "
        "('class-inactive-enrollment', 'course-1', 'Inactive enrollment', 'active'), "
        "('class-inactive-class', 'course-1', 'Inactive class', 'inactive'), "
        "('class-lifecycle', 'course-1', 'Lifecycle class', 'active'), "
        "('class-concurrent', 'course-1', 'Concurrent class', 'active'), "
        "('class-personal', 'course-1', 'Personal class', 'active')"
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.courses (id, title) "
                "VALUES ('course-1', 'Runtime course')"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.classes (id, course_id, name, status) "
                f"VALUES {class_rows}"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.enrollments "
                "(class_id, learner_id, status) VALUES "
                "('class-valid', 'student-a', 'active'), "
                "('class-revoked', 'student-a', 'active'), "
                "('class-inactive-enrollment', 'student-a', 'inactive'), "
                "('class-inactive-class', 'student-a', 'active'), "
                "('class-lifecycle', 'student-a', 'active'), "
                "('class-concurrent', 'student-a', 'active'), "
                "('class-personal', 'student-a', 'active')"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.course_generation_policies "
                "(course_id, tenant_id, allow_student_micro, allow_student_full, "
                "allowed_content_modes, daily_student_units, monthly_student_units, "
                "updated_by) VALUES "
                "('course-1', :tenant_id, true, true, 'open_creation', 100, 1000, "
                "'teacher-1')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.generation_jobs ("
                "id, tenant_id, job_kind, phase, status, priority, quota_units, "
                "actor_id, owner_id, visibility, request_id, idempotency_key, "
                "resource_course_id, resource_class_id, request_sha256, "
                "data_plane_route_id, provider_profile_id, worker_pool_ref, "
                "queue_ref, request_payload, progress_percent"
                ") VALUES "
                "('job-assigned', :tenant_id, 'generation', 'content', 'succeeded', "
                "0, 1, 'teacher-1', 'teacher-1', 'class', 'request-assigned', "
                "'idem-assigned', 'course-1', 'class-valid', :sha, 'route-1', "
                "'provider-1', 'workers-1', 'queue-1', '{}', 100), "
                "('job-private', :tenant_id, 'generation', 'content', 'succeeded', "
                "0, 1, 'student-a', 'student-a', 'private', 'request-private-job', "
                "'idem-private', 'course-1', 'class-personal', :sha, 'route-1', "
                "'provider-1', 'workers-1', 'queue-1', '{}', 100), "
                "('job-other', :tenant_id, 'generation', 'content', 'succeeded', "
                "0, 1, 'student-b', 'student-b', 'private', 'request-other-job', "
                "'idem-other', 'course-1', 'class-personal', :sha, 'route-1', "
                "'provider-1', 'workers-1', 'queue-1', '{}', 100)"
            ),
            {"tenant_id": tenant_id, "sha": sha},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.classroom_assets "
                "(id, tenant_id, owner_id, title, lifecycle_state) VALUES "
                "('asset-assigned', :tenant_id, 'teacher-1', 'Assigned', 'published'), "
                "('asset-private', :tenant_id, 'student-a', 'Private', 'editing'), "
                "('asset-other', :tenant_id, 'student-b', 'Other', 'editing')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.classroom_drafts "
                "(id, tenant_id, classroom_id, generation_job_id, document, "
                "document_sha256, created_by, updated_by) VALUES "
                "('draft-private', :tenant_id, 'asset-private', 'job-private', '{}', "
                ":sha, 'student-a', 'student-a'), "
                "('draft-other', :tenant_id, 'asset-other', 'job-other', '{}', "
                ":sha, 'student-b', 'student-b')"
            ),
            {"tenant_id": tenant_id, "sha": sha},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.classroom_versions "
                "(id, tenant_id, classroom_id, version_number, generation_job_id, "
                "document_sha256, media_manifest_sha256, document_object_key) VALUES "
                "('version-assigned', :tenant_id, 'asset-assigned', 1, "
                "'job-assigned', :sha, :sha, 'assigned/classroom.json'), "
                "('version-private', :tenant_id, 'asset-private', 1, 'job-private', "
                ":sha, :sha, 'private/classroom.json'), "
                "('version-other', :tenant_id, 'asset-other', 1, 'job-other', "
                ":sha, :sha, 'other/classroom.json')"
            ),
            {"tenant_id": tenant_id, "sha": sha},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.assignments "
                "(id, tenant_id, classroom_version_id, class_id, assigned_by, "
                "revoked_at) VALUES "
                "('assignment-valid', :tenant_id, 'version-assigned', 'class-valid', "
                "'teacher-1', NULL), "
                "('assignment-revoked', :tenant_id, 'version-assigned', "
                "'class-revoked', 'teacher-1', clock_timestamp()), "
                "('assignment-inactive-enrollment', :tenant_id, 'version-assigned', "
                "'class-inactive-enrollment', 'teacher-1', NULL), "
                "('assignment-inactive-class', :tenant_id, 'version-assigned', "
                "'class-inactive-class', 'teacher-1', NULL), "
                "('assignment-lifecycle', :tenant_id, 'version-assigned', "
                "'class-lifecycle', 'teacher-1', NULL), "
                "('assignment-concurrent', :tenant_id, 'version-assigned', "
                "'class-concurrent', 'teacher-1', NULL)"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.student_generation_requests "
                "(id, tenant_id, learner_id, course_id, class_id, mode, content_mode, "
                "web_search_requested, scene_min, scene_max, duration_minutes_min, "
                "duration_minutes_max, estimated_units, quota_state, "
                "requires_outline_confirmation, decision_outcome, decision_reason, "
                "evaluated_checks) VALUES "
                "('request-private', :tenant_id, 'student-a', 'course-1', "
                "'class-personal', 'micro', 'open_creation', false, 1, 3, 1, 10, "
                "1, 'settled', false, 'accepted', 'allowed', 'policy'), "
                "('request-other', :tenant_id, 'student-b', 'course-1', "
                "'class-personal', 'micro', 'open_creation', false, 1, 3, 1, 10, "
                "1, 'settled', false, 'accepted', 'allowed', 'policy')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted_schema}.student_classroom_assets "
                "(asset_id, tenant_id, request_id) VALUES "
                "('asset-private', :tenant_id, 'request-private'), "
                "('asset-other', :tenant_id, 'request-other')"
            ),
            {"tenant_id": tenant_id},
        )


@pytest.fixture(scope="module")
def learning_runtime(generation_database, tmp_path_factory) -> LearningRuntime:
    tenant_id = f"learning-runtime-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    previous = _run_tenant_migration(
        generation_database,
        schema_name,
        "upgrade",
        PREVIOUS_REVISION,
    )
    assert previous.returncode == 0, f"{previous.stdout}\n{previous.stderr}"
    asyncio.run(_register_tenant(engine, tenant_id, schema_name))
    upgraded = _run_tenant_migration(
        generation_database,
        schema_name,
        "upgrade",
        TICKET_REVISION,
    )
    assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"
    asyncio.run(
        _seed_learning_runtime_authorities(
            engine,
            tenant_id=tenant_id,
            schema_name=schema_name,
        )
    )
    secret_file = tmp_path_factory.mktemp("classroom-ticket-secret") / "secret"
    secret_file.write_text("runtime-ticket-secret-" + "c" * 48, encoding="utf-8")
    runtime = LearningRuntime(
        database_url=generation_database.url,
        tenant_id=tenant_id,
        schema_name=schema_name,
        secret_file=secret_file,
    )
    yield runtime
    asyncio.run(_cleanup_tenant(engine, tenant_id, schema_name))
    asyncio.run(engine.dispose())


def test_ticket_consumption_metadata_has_atomic_replay_constraints() -> None:
    models = importlib.import_module("deeptutor.teaching.models.learning")
    model = models.ClassroomTicketConsumption
    table = model.__table__

    assert {
        foreign_key.target_fullname
        for foreign_key in models.LearningSession.__table__.c.classroom_version_id.foreign_keys
    } == {"tenant.classroom_versions.id"}
    assert table.name == TICKET_TABLE
    assert table.primary_key.columns.keys() == ["jti"]
    assert {
        "jti",
        "tenant_id",
        "session_id",
        "user_id",
        "classroom_version_id",
        "allowed_action",
        "issued_at",
        "expires_at",
        "consumed_at",
    } == set(table.columns.keys())
    assert "ix_classroom_ticket_consumptions_expires_at" in {
        index.name for index in table.indexes if isinstance(index, Index)
    }
    assert any(
        tuple(constraint.columns.keys()) == ("session_id", "tenant_id")
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )
    assert {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and tuple(constraint.columns.keys()) == ("classroom_version_id",)
    } == {"fk_classroom_ticket_consumptions_version"}
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["ck_classroom_ticket_consumptions_validity"] == ("expires_at > issued_at")
    assert checks["ck_classroom_ticket_consumptions_allowed_action"] == (
        "allowed_action = 'learning_event.append'"
    )


@pytest.mark.asyncio
async def test_assignment_session_uses_server_version_and_active_authority(
    learning_runtime: LearningRuntime,
) -> None:
    engine, _, service = _runtime_service(learning_runtime)
    context = _context(learning_runtime)
    try:
        created = await service.create(context, assignment_id="assignment-valid")

        assert created.tenant_id == learning_runtime.tenant_id
        assert created.user_id == "student-a"
        assert created.assignment_id == "assignment-valid"
        assert created.student_asset_id is None
        assert created.classroom_version_id == "version-assigned"
        assert created.status == "active"
        assert created.last_cursor == {"last_event_seq": 0}

        for assignment_id in (
            "assignment-revoked",
            "assignment-inactive-enrollment",
            "assignment-inactive-class",
        ):
            with pytest.raises(LearningSessionAuthorityError):
                await service.create(context, assignment_id=assignment_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_session_requires_owned_record_and_exact_draft_version(
    learning_runtime: LearningRuntime,
) -> None:
    engine, _, service = _runtime_service(learning_runtime)
    context = _context(learning_runtime)
    try:
        created = await service.create(context, student_asset_id="asset-private")

        assert created.user_id == "student-a"
        assert created.assignment_id is None
        assert created.student_asset_id == "asset-private"
        assert created.classroom_version_id == "version-private"

        for student_asset_id in ("asset-other", "asset-missing"):
            with pytest.raises(LearningSessionAuthorityError):
                await service.create(context, student_asset_id=student_asset_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_first_sessions_create_one_class_state_without_lost_count(
    learning_runtime: LearningRuntime,
) -> None:
    first_engine, _, first_service = _runtime_service(learning_runtime)
    second_engine, _, second_service = _runtime_service(learning_runtime)
    context = _context(learning_runtime)
    quoted_schema = f'"{learning_runtime.schema_name}"'
    start = asyncio.Event()
    ready = 0
    ready_lock = asyncio.Lock()

    async def create_when_released(service: LearningSessionService):
        nonlocal ready
        async with ready_lock:
            ready += 1
            if ready == 2:
                start.set()
        await start.wait()
        return await service.create(context, assignment_id="assignment-concurrent")

    try:
        sessions = await asyncio.gather(
            create_when_released(first_service),
            create_when_released(second_service),
        )
        assert len({session.id for session in sessions}) == 2
        async with first_engine.connect() as connection:
            states = (
                await connection.execute(
                    text(
                        f"SELECT state, active_session_count FROM "
                        f"{quoted_schema}.class_learning_states "
                        "WHERE class_id = 'class-concurrent'"
                    )
                )
            ).all()
        assert [tuple(state) for state in states] == [("active", 2)]
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.asyncio
async def test_assignment_session_lifecycle_updates_class_learning_state_without_underflow(
    learning_runtime: LearningRuntime,
) -> None:
    engine, _, service = _runtime_service(learning_runtime)
    context = _context(learning_runtime)
    quoted_schema = f'"{learning_runtime.schema_name}"'
    try:
        first = await service.create(context, assignment_id="assignment-lifecycle")
        second = await service.create(context, assignment_id="assignment-lifecycle")
        async with engine.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        f"SELECT state, active_session_count FROM "
                        f"{quoted_schema}.class_learning_states "
                        "WHERE class_id = 'class-lifecycle'"
                    )
                )
            ).one()
        assert tuple(state) == ("active", 2)

        completed = await service.complete(context, session_id=first.id)
        assert completed.status == "completed"
        abandoned = await service.abandon(context, session_id=second.id)
        assert abandoned.status == "abandoned"
        async with engine.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        f"SELECT state, active_session_count FROM "
                        f"{quoted_schema}.class_learning_states "
                        "WHERE class_id = 'class-lifecycle'"
                    )
                )
            ).one()
        assert tuple(state) == ("idle", 0)

        with pytest.raises(LearningSessionAuthorityError):
            await service.complete(context, session_id=first.id)
        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    f"SELECT active_session_count FROM "
                    f"{quoted_schema}.class_learning_states "
                    "WHERE class_id = 'class-lifecycle'"
                )
            )
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_event_ticket_replay_is_persistent_across_service_instances(
    learning_runtime: LearningRuntime,
) -> None:
    engine, _, first_service = _runtime_service(learning_runtime)
    second_engine, _, second_service = _runtime_service(learning_runtime)
    context = _context(learning_runtime)
    try:
        learning_session = await first_service.create(
            context,
            assignment_id="assignment-valid",
        )
        token = await first_service.issue_event_ticket(
            context,
            session_id=learning_session.id,
        )

        async def protected_action(_database_session, claims):
            return claims.jti

        consumed_jti = await first_service.consume_event_ticket(
            context,
            session_id=learning_session.id,
            token=token,
            protected_action=protected_action,
        )
        assert consumed_jti

        tickets = importlib.import_module("deeptutor.teaching.tickets")
        with pytest.raises(tickets.TicketReplay):
            await second_service.consume_event_ticket(
                context,
                session_id=learning_session.id,
                token=token,
                protected_action=protected_action,
            )
    finally:
        await engine.dispose()
        await second_engine.dispose()


@pytest.mark.asyncio
async def test_failed_protected_action_rolls_back_jti_and_allows_same_ticket_retry(
    learning_runtime: LearningRuntime,
) -> None:
    engine, _, service = _runtime_service(learning_runtime)
    context = _context(learning_runtime)
    quoted_schema = f'"{learning_runtime.schema_name}"'

    class ProtectedFailure(RuntimeError):
        pass

    try:
        learning_session = await service.create(
            context,
            assignment_id="assignment-valid",
        )
        token = await service.issue_event_ticket(
            context,
            session_id=learning_session.id,
        )

        async def failing_action(database_session, _claims):
            locked = await database_session.get(
                LearningSession,
                learning_session.id,
                with_for_update=True,
            )
            assert locked is not None
            locked.last_cursor = {"last_event_seq": 99}
            raise ProtectedFailure

        with pytest.raises(ProtectedFailure):
            await service.consume_event_ticket(
                context,
                session_id=learning_session.id,
                token=token,
                protected_action=failing_action,
            )

        async with engine.connect() as connection:
            consumption_count = await connection.scalar(
                text(
                    f"SELECT count(*) FROM {quoted_schema}.{TICKET_TABLE} "
                    "WHERE session_id = :session_id"
                ),
                {"session_id": learning_session.id},
            )
            cursor = await connection.scalar(
                text(
                    f"SELECT last_cursor FROM {quoted_schema}.learning_sessions "
                    "WHERE id = :session_id"
                ),
                {"session_id": learning_session.id},
            )
        assert consumption_count == 0
        assert cursor == {"last_event_seq": 0}

        async def successful_retry(database_session, claims):
            locked = await database_session.get(
                LearningSession,
                learning_session.id,
                with_for_update=True,
            )
            assert locked is not None
            locked.last_cursor = {"last_event_seq": 1}
            return claims.jti

        retry_jti = await service.consume_event_ticket(
            context,
            session_id=learning_session.id,
            token=token,
            protected_action=successful_retry,
        )
        assert retry_jti
        async with engine.connect() as connection:
            consumption_count = await connection.scalar(
                text(
                    f"SELECT count(*) FROM {quoted_schema}.{TICKET_TABLE} "
                    "WHERE session_id = :session_id"
                ),
                {"session_id": learning_session.id},
            )
            cursor = await connection.scalar(
                text(
                    f"SELECT last_cursor FROM {quoted_schema}.learning_sessions "
                    "WHERE id = :session_id"
                ),
                {"session_id": learning_session.id},
            )
        assert consumption_count == 1
        assert cursor == {"last_event_seq": 1}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ticket_consumption_migration_tracks_revision_and_empty_downgrade(
    generation_database,
) -> None:
    assert (
        PROJECT_ROOT
        / "deeptutor/teaching/migrations/versions/20260810_0017_classroom_ticket_consumptions.py"
    ).is_file()
    tenant_id = f"ticket-schema-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        previous = _run_tenant_migration(
            generation_database, schema_name, "upgrade", PREVIOUS_REVISION
        )
        assert previous.returncode == 0, f"{previous.stdout}\n{previous.stderr}"
        await _register_tenant(engine, tenant_id, schema_name)

        upgraded = _run_tenant_migration(
            generation_database, schema_name, "upgrade", TICKET_REVISION
        )
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"

        async with engine.connect() as connection:
            columns = set(
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema_name AND table_name = :table_name"
                    ),
                    {"schema_name": schema_name, "table_name": TICKET_TABLE},
                )
            )
            state_revision = await connection.scalar(
                text(
                    "SELECT revision FROM platform.tenant_schema_states "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
            alembic_revision = await connection.scalar(
                text(f'SELECT version_num FROM "{schema_name}".alembic_version')
            )
        assert columns == {
            "jti",
            "tenant_id",
            "session_id",
            "user_id",
            "classroom_version_id",
            "allowed_action",
            "issued_at",
            "expires_at",
            "consumed_at",
        }
        assert (state_revision, alembic_revision) == (
            TICKET_REVISION,
            TICKET_REVISION,
        )

        downgraded = _run_tenant_migration(
            generation_database, schema_name, "downgrade", PREVIOUS_REVISION
        )
        assert downgraded.returncode == 0, f"{downgraded.stdout}\n{downgraded.stderr}"
        async with engine.connect() as connection:
            table_exists = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :schema_name AND table_name = :table_name)"
                ),
                {"schema_name": schema_name, "table_name": TICKET_TABLE},
            )
            state_revision = await connection.scalar(
                text(
                    "SELECT revision FROM platform.tenant_schema_states "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
        assert table_exists is False
        assert state_revision == PREVIOUS_REVISION
    finally:
        await _cleanup_tenant(engine, tenant_id, schema_name)
        await engine.dispose()


@pytest.mark.asyncio
async def test_ticket_consumption_downgrade_fails_closed_with_replay_facts(
    generation_database,
) -> None:
    tenant_id = f"ticket-facts-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    now = datetime.now(UTC)
    try:
        previous = _run_tenant_migration(
            generation_database, schema_name, "upgrade", PREVIOUS_REVISION
        )
        assert previous.returncode == 0, f"{previous.stdout}\n{previous.stderr}"
        await _register_tenant(engine, tenant_id, schema_name)
        upgraded = _run_tenant_migration(
            generation_database, schema_name, "upgrade", TICKET_REVISION
        )
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"
        await _seed_assignment_session(
            engine,
            tenant_id=tenant_id,
            schema_name=schema_name,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".{TICKET_TABLE} '
                    "(jti, tenant_id, session_id, user_id, classroom_version_id, "
                    "allowed_action, issued_at, expires_at) "
                    "VALUES ('ticket-jti', :tenant_id, 'session-1', 'student-1', "
                    "'version-1', 'learning_event.append', :issued_at, :expires_at)"
                ),
                {
                    "tenant_id": tenant_id,
                    "issued_at": now,
                    "expires_at": now + timedelta(minutes=5),
                },
            )

        rejected = _run_tenant_migration(
            generation_database, schema_name, "downgrade", PREVIOUS_REVISION
        )
        assert rejected.returncode != 0
        assert "durable ticket consumption facts exist" in (rejected.stdout + rejected.stderr)
    finally:
        await _cleanup_tenant(engine, tenant_id, schema_name)
        await engine.dispose()
