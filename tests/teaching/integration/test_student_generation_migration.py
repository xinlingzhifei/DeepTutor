from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.teaching.models.student_generation import (
    CourseGenerationPolicyRecord,
    StudentClassroomAssetRecord,
    StudentClassroomCopyRecord,
    StudentGenerationApprovalRecord,
    StudentGenerationRequestRecord,
)
from deeptutor.teaching.policies.student_generation import StudentGenerationRequest
from deeptutor.teaching.repositories import student_generation as student_generation_repository
from deeptutor.teaching.repositories.student_generation import (
    SqlAlchemyStudentGenerationRepository,
    StudentSafetyAssessment,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.student_generation import StudentGenerationService

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class SafeStudentGenerationEvaluator:
    async def assess(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentSafetyAssessment:
        return StudentSafetyAssessment(
            generally_safe=True,
            minor_safe=True,
            restricted_topic=False,
        )


class BarrierStudentGenerationEvaluator(SafeStudentGenerationEvaluator):
    def __init__(self, parties: int = 2) -> None:
        self._parties = parties
        self._arrivals = 0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def assess(
        self,
        tenant_id: str,
        learner_id: str,
        request: StudentGenerationRequest,
    ) -> StudentSafetyAssessment:
        async with self._lock:
            self._arrivals += 1
            if self._arrivals == self._parties:
                self._ready.set()
        await self._ready.wait()
        return await super().assess(tenant_id, learner_id, request)


def run_tenant_migration(generation_database, schema_name: str, target: str):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            "scope=tenant",
            "-x",
            f"tenant_schema={schema_name}",
            "upgrade" if not target.startswith("-") else "downgrade",
            target.removeprefix("-"),
        ],
        cwd=PROJECT_ROOT,
        env=generation_database.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.asyncio
async def test_postgres_student_generation_migration_constraints_state_and_downgrade(
    generation_database,
) -> None:
    tenant_id = f"student-generation-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        to_previous = run_tenant_migration(
            generation_database,
            schema_name,
            "20260804_0012",
        )
        assert to_previous.returncode == 0, f"{to_previous.stdout}\n{to_previous.stderr}"
        async with engine.connect() as connection:
            tables_before = set(
                await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema_name"
                    ),
                    {"schema_name": schema_name},
                )
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.tenants "
                    "(id, name, status, data_plane_mode) "
                    "VALUES (:tenant_id, 'Student generation', 'active', 'shared')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.tenant_schema_states "
                    "(tenant_id, schema_name, revision, status) "
                    "VALUES (:tenant_id, :schema_name, '20260804_0012', 'active')"
                ),
                {"tenant_id": tenant_id, "schema_name": schema_name},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".courses (id, title) '
                    "VALUES ('course-1', 'Physics')"
                )
            )

        upgraded = run_tenant_migration(generation_database, schema_name, "head")
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
            revision = await connection.scalar(
                text(f'SELECT version_num FROM "{schema_name}".alembic_version')
            )
            state_revision = await connection.scalar(
                text(
                    "SELECT revision FROM platform.tenant_schema_states "
                    "WHERE schema_name = :schema_name"
                ),
                {"schema_name": schema_name},
            )
            constraint_names = set(
                await connection.scalars(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_schema = :schema_name "
                        "AND table_name IN ('course_generation_policies', "
                        "'student_generation_requests', 'student_generation_approvals', "
                        "'student_classroom_assets', 'student_classroom_copies')"
                    ),
                    {"schema_name": schema_name},
                )
            )
            pending_index = await connection.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = :schema_name "
                    "AND indexname = 'uq_student_generation_approvals_pending_request'"
                ),
                {"schema_name": schema_name},
            )
            platform_tables = set(
                await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'platform' AND table_name IN "
                        "('course_generation_policies', 'student_generation_requests', "
                        "'student_generation_approvals')"
                    )
                )
            )
            migrated_columns = (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = :schema_name "
                        "AND table_name IN ('course_generation_policies', "
                        "'student_generation_requests', 'student_generation_approvals', "
                        "'student_classroom_assets', 'student_classroom_copies')"
                    ),
                    {"schema_name": schema_name},
                )
            ).all()
        new_tables = {
            "course_generation_policies",
            "student_generation_requests",
            "student_generation_approvals",
            "student_classroom_assets",
            "student_classroom_copies",
        }
        assert tables - tables_before == new_tables
        assert {
            "ck_course_generation_policies_micro_scene_limit",
            "ck_course_generation_policies_full_scene_limit",
            "ck_course_generation_policies_daily_student_units",
            "ck_student_generation_requests_scene_range",
            "ck_student_generation_requests_outline_confirmation",
            "ck_student_generation_requests_quota_state",
            "ck_student_generation_requests_quota_lifecycle",
            "ck_student_generation_approvals_decision_shape",
            "fk_student_generation_requests_class_course_classes",
            "fk_student_generation_requests_policy_tenant",
            "fk_student_generation_approvals_request_tenant",
            "fk_student_classroom_assets_asset_tenant",
            "fk_student_classroom_assets_request_tenant",
            "fk_student_classroom_copies_source_tenant",
            "fk_student_classroom_copies_teacher_tenant",
            "uq_student_classroom_assets_request_tenant",
            "uq_student_classroom_copies_teacher_tenant",
        }.issubset(constraint_names)
        assert pending_index is not None
        assert "UNIQUE INDEX" in pending_index
        assert "WHERE" in pending_index and "pending" in pending_index
        assert platform_tables == set()
        expected_tables = {
            model.__table__.name: model.__table__
            for model in (
                CourseGenerationPolicyRecord,
                StudentGenerationRequestRecord,
                StudentGenerationApprovalRecord,
                StudentClassroomAssetRecord,
                StudentClassroomCopyRecord,
            )
        }
        actual_column_names: dict[str, set[str]] = {table_name: set() for table_name in new_tables}
        actual_nullable: dict[tuple[str, str], bool] = {}
        for table_name, column_name, is_nullable in migrated_columns:
            actual_column_names[table_name].add(column_name)
            actual_nullable[(table_name, column_name)] = is_nullable == "YES"
        assert actual_column_names == {
            table_name: set(table.c.keys()) for table_name, table in expected_tables.items()
        }
        assert actual_nullable == {
            (table_name, column.name): column.nullable
            for table_name, table in expected_tables.items()
            for column in table.c
        }
        assert (revision, state_revision) == ("20260809_0014", "20260809_0014")

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f'INSERT INTO "{schema_name}".course_generation_policies '
                        "(course_id, tenant_id, allowed_content_modes, "
                        "micro_scene_limit, daily_student_units, "
                        "monthly_student_units, updated_by) VALUES "
                        "('course-1', :tenant_id, 'source_grounded', 6, 1, 1, "
                        "'teacher-1')"
                    ),
                    {"tenant_id": tenant_id},
                )

        downgraded = run_tenant_migration(
            generation_database,
            schema_name,
            "-20260804_0012",
        )
        assert downgraded.returncode == 0, f"{downgraded.stdout}\n{downgraded.stderr}"
        async with engine.connect() as connection:
            remaining = set(
                await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema_name"
                    ),
                    {"schema_name": schema_name},
                )
            )
            state_revision = await connection.scalar(
                text(
                    "SELECT revision FROM platform.tenant_schema_states "
                    "WHERE schema_name = :schema_name"
                ),
                {"schema_name": schema_name},
            )
        assert "course_generation_policies" not in remaining
        assert "student_generation_requests" not in remaining
        assert "student_generation_approvals" not in remaining
        assert state_revision == "20260804_0012"

        upgraded = run_tenant_migration(generation_database, schema_name, "head")
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".course_generation_policies '
                    "(course_id, tenant_id, allowed_content_modes, "
                    "daily_student_units, monthly_student_units, updated_by) "
                    "VALUES ('course-1', :tenant_id, 'source_grounded', 1, 1, 'teacher-1')"
                ),
                {"tenant_id": tenant_id},
            )

        refused = run_tenant_migration(
            generation_database,
            schema_name,
            "-20260804_0012",
        )
        assert refused.returncode != 0
        assert "cannot downgrade student generation" in (refused.stdout + refused.stderr)
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text(f'SELECT version_num FROM "{schema_name}".alembic_version')
            )
            state_revision = await connection.scalar(
                text(
                    "SELECT revision FROM platform.tenant_schema_states "
                    "WHERE schema_name = :schema_name"
                ),
                {"schema_name": schema_name},
            )
        assert (revision, state_revision) == ("20260809_0014", "20260809_0014")
    finally:
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
        await engine.dispose()


@pytest.mark.asyncio
async def test_repository_uses_database_decision_time_for_quota_window(
    generation_database,
    monkeypatch,
) -> None:
    tenant_id = f"student-clock-{uuid.uuid4().hex[:12]}"
    learner_id = f"student-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.tenants "
                    "(id, name, status, data_plane_mode) "
                    "VALUES (:tenant_id, 'Student clock', 'active', 'shared')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.tenant_memberships (tenant_id, user_id) "
                    "VALUES (:tenant_id, :learner_id)"
                ),
                {"tenant_id": tenant_id, "learner_id": learner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.role_grants "
                    "(tenant_id, user_id, role, scope_type, scope_id) "
                    "VALUES (:tenant_id, :learner_id, 'student', 'class', 'class-1')"
                ),
                {"tenant_id": tenant_id, "learner_id": learner_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".courses (id, title) '
                    "VALUES ('course-1', 'Physics')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".classes (id, course_id, name) '
                    "VALUES ('class-1', 'course-1', 'Class 1')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".enrollments '
                    "(class_id, learner_id, status) "
                    "VALUES ('class-1', :learner_id, 'active')"
                ),
                {"learner_id": learner_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".course_generation_policies '
                    "(course_id, tenant_id, allow_student_micro, "
                    "allow_student_full, allowed_content_modes, daily_student_units, "
                    "monthly_student_units, updated_by) VALUES "
                    "('course-1', :tenant_id, true, false, 'open_creation', 5, 100, "
                    "'teacher-1')"
                ),
                {"tenant_id": tenant_id},
            )
            database_time = await connection.scalar(text("SELECT clock_timestamp()"))

        class SkewedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                shifted = database_time + timedelta(days=1)
                return shifted if tz is None else shifted.astimezone(tz)

        repository = SqlAlchemyStudentGenerationRepository(
            engine,
            tenant_id,
            safety_evaluator=SafeStudentGenerationEvaluator(),
        )
        service = StudentGenerationService(
            tenant_id=tenant_id,
            learner_id=learner_id,
            repository=repository,
        )
        request = StudentGenerationRequest(
            course_id="course-1",
            class_id="class-1",
            mode="micro",
            content_mode="open_creation",
            web_search_requested=False,
        )
        with monkeypatch.context() as skewed_clock:
            skewed_clock.setattr(student_generation_repository, "datetime", SkewedDateTime)
            first = await service.evaluate(request)
            second = await service.evaluate(request)

        assert first.decision.outcome == "accepted"
        assert second.decision.outcome == "approval_required"
        async with engine.connect() as connection:
            accepted_created_at = await connection.scalar(
                text(
                    f'SELECT created_at FROM "{schema_name}".student_generation_requests '
                    "WHERE id = :request_id"
                ),
                {"request_id": first.request_id},
            )
        assert accepted_created_at.date() == database_time.date()
        assert abs(accepted_created_at - database_time) < timedelta(seconds=10)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM platform.audit_log WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(DropSchema(schema_name, cascade=True))
            await connection.execute(
                text("DELETE FROM platform.role_grants WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text("DELETE FROM platform.tenant_memberships WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_repository_loads_trusted_facts_and_atomically_audits_decisions(
    generation_database,
) -> None:
    tenant_id = f"student-policy-{uuid.uuid4().hex[:12]}"
    learner_id = f"student-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.tenants "
                    "(id, name, status, data_plane_mode) "
                    "VALUES (:tenant_id, 'Student policy', 'active', 'shared')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.tenant_memberships (tenant_id, user_id) "
                    "VALUES (:tenant_id, :learner_id)"
                ),
                {"tenant_id": tenant_id, "learner_id": learner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.role_grants "
                    "(tenant_id, user_id, role, scope_type, scope_id) "
                    "VALUES (:tenant_id, :learner_id, 'student', 'class', 'class-1')"
                ),
                {"tenant_id": tenant_id, "learner_id": learner_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".courses (id, title) '
                    "VALUES ('course-1', 'Physics')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".classes (id, course_id, name) '
                    "VALUES ('class-1', 'course-1', 'Class 1')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".enrollments '
                    "(class_id, learner_id, status) "
                    "VALUES ('class-1', :learner_id, 'active')"
                ),
                {"learner_id": learner_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".course_generation_policies '
                    "(course_id, tenant_id, allow_student_micro, "
                    "allow_student_full, allowed_content_modes, daily_student_units, "
                    "monthly_student_units, updated_by) VALUES "
                    "('course-1', :tenant_id, true, false, "
                    "'source_grounded,open_creation', 5, 5, "
                    "'teacher-1')"
                ),
                {"tenant_id": tenant_id},
            )

        repository = SqlAlchemyStudentGenerationRepository(
            engine,
            tenant_id,
            safety_evaluator=BarrierStudentGenerationEvaluator(),
        )
        service = StudentGenerationService(
            tenant_id=tenant_id,
            learner_id=learner_id,
            repository=repository,
        )
        micro_request = StudentGenerationRequest(
            course_id="course-1",
            class_id="class-1",
            mode="micro",
            content_mode="open_creation",
            web_search_requested=False,
        )
        concurrent = await asyncio.gather(
            service.evaluate(micro_request),
            service.evaluate(micro_request),
        )
        full = await service.evaluate(
            StudentGenerationRequest(
                course_id="course-1",
                class_id="class-1",
                mode="full",
                content_mode="open_creation",
                web_search_requested=False,
            )
        )

        assert sorted(result.decision.outcome for result in concurrent) == [
            "accepted",
            "approval_required",
        ]
        assert sum(result.approval_id is not None for result in concurrent) == 1
        assert all(result.generation_job_id is None for result in concurrent)
        assert full.decision.outcome == "denied"
        assert full.decision.reason == "full_classroom_disabled"
        assert full.approval_id is None
        async with engine.connect() as connection:
            requests = await connection.scalar(
                text(
                    f'SELECT count(*) FROM "{schema_name}".student_generation_requests '
                    "WHERE tenant_id = :tenant_id AND learner_id = :learner_id"
                ),
                {"tenant_id": tenant_id, "learner_id": learner_id},
            )
            approvals = await connection.scalar(
                text(
                    f'SELECT count(*) FROM "{schema_name}".student_generation_approvals '
                    "WHERE tenant_id = :tenant_id AND status = 'pending'"
                ),
                {"tenant_id": tenant_id},
            )
            audit_actions = set(
                await connection.scalars(
                    text(
                        "SELECT action FROM platform.audit_log "
                        "WHERE tenant_id = :tenant_id "
                        "AND resource_type = 'student_generation_request'"
                    ),
                    {"tenant_id": tenant_id},
                )
            )
        assert requests == 3
        assert approvals == 1
        assert audit_actions == {
            "student_generation.approval_required",
            "student_generation.denied",
        }

        async with engine.begin() as connection:
            accepted_request_id = await connection.scalar(
                text(
                    f'SELECT id FROM "{schema_name}".student_generation_requests '
                    "WHERE decision_outcome = 'accepted'"
                )
            )
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".student_generation_requests '
                    "SET quota_state = 'released' WHERE id = :request_id"
                ),
                {"request_id": accepted_request_id},
            )
        released_service = StudentGenerationService(
            tenant_id=tenant_id,
            learner_id=learner_id,
            repository=SqlAlchemyStudentGenerationRepository(
                engine,
                tenant_id,
                safety_evaluator=SafeStudentGenerationEvaluator(),
            ),
        )
        after_release = await released_service.evaluate(micro_request)
        assert after_release.decision.outcome == "accepted"

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".student_generation_requests '
                    "SET quota_state = 'released' WHERE id = :request_id"
                ),
                {"request_id": after_release.request_id},
            )
            gate_key = int(uuid.uuid4().hex[:12], 16)
            await connection.execute(
                text(
                    f'CREATE FUNCTION "{schema_name}".hold_student_request_insert() '
                    "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                    f"PERFORM pg_advisory_xact_lock({gate_key}); "
                    "RETURN NEW; END $$"
                )
            )
            await connection.execute(
                text(
                    f"CREATE TRIGGER hold_student_request_insert BEFORE INSERT ON "
                    f'"{schema_name}".student_generation_requests FOR EACH ROW '
                    f'EXECUTE FUNCTION "{schema_name}".hold_student_request_insert()'
                )
            )

        gate_connection = await engine.connect()
        try:
            gate_pid = await gate_connection.scalar(text("SELECT pg_backend_pid()"))
            await gate_connection.execute(
                text("SELECT pg_advisory_lock(:gate_key)"),
                {"gate_key": gate_key},
            )
            generation_task = asyncio.create_task(released_service.evaluate(micro_request))
            for _attempt in range(100):
                async with engine.connect() as connection:
                    insert_is_waiting = await connection.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM pg_locks waiting "
                            "JOIN pg_locks holding ON "
                            "holding.locktype = waiting.locktype "
                            "AND holding.database IS NOT DISTINCT FROM waiting.database "
                            "AND holding.classid = waiting.classid "
                            "AND holding.objid = waiting.objid "
                            "AND holding.objsubid = waiting.objsubid "
                            "WHERE waiting.locktype = 'advisory' "
                            "AND waiting.granted = false "
                            "AND holding.granted = true AND holding.pid = :gate_pid)"
                        ),
                        {"gate_pid": gate_pid},
                    )
                if insert_is_waiting:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("student request insert did not reach the advisory gate")

            async def revoke_enrollment() -> None:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            f'DELETE FROM "{schema_name}".enrollments '
                            "WHERE class_id = 'class-1' AND learner_id = :learner_id"
                        ),
                        {"learner_id": learner_id},
                    )

            revoke_task = asyncio.create_task(revoke_enrollment())
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(revoke_task), timeout=0.25)
            await gate_connection.execute(
                text("SELECT pg_advisory_unlock(:gate_key)"),
                {"gate_key": gate_key},
            )
            race_result = await generation_task
            await revoke_task
        finally:
            await gate_connection.execute(text("SELECT pg_advisory_unlock_all()"))
            await gate_connection.close()

        assert race_result.decision.outcome == "accepted"
        async with engine.connect() as connection:
            enrollment_exists = await connection.scalar(
                text(
                    f'SELECT EXISTS (SELECT 1 FROM "{schema_name}".enrollments '
                    "WHERE class_id = 'class-1' AND learner_id = :learner_id)"
                ),
                {"learner_id": learner_id},
            )
        assert enrollment_exists is False

        missing_enrollment = await released_service.evaluate(micro_request)
        assert missing_enrollment.decision.reason == "not_enrolled"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".enrollments '
                    "(class_id, learner_id, status) "
                    "VALUES ('class-1', :learner_id, 'active')"
                ),
                {"learner_id": learner_id},
            )
            await connection.execute(
                text(
                    "DELETE FROM platform.role_grants "
                    "WHERE tenant_id = :tenant_id AND user_id = :learner_id"
                ),
                {"tenant_id": tenant_id, "learner_id": learner_id},
            )
        missing_permission = await released_service.evaluate(micro_request)
        assert missing_permission.decision.reason == "generation_permission_denied"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.role_grants "
                    "(tenant_id, user_id, role, scope_type, scope_id) "
                    "VALUES (:tenant_id, :learner_id, 'student', 'class', 'class-1')"
                ),
                {"tenant_id": tenant_id, "learner_id": learner_id},
            )
        missing_source = await released_service.evaluate(
            StudentGenerationRequest(
                course_id="course-1",
                class_id="class-1",
                mode="micro",
                content_mode="source_grounded",
                web_search_requested=False,
            )
        )
        assert missing_source.decision.reason == "source_permission_denied"

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".courses (id, title) '
                    "VALUES ('course-2', 'Chemistry')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".classes (id, course_id, name) '
                    "VALUES ('class-2', 'course-2', 'Class 2')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".enrollments '
                    "(class_id, learner_id, status) "
                    "VALUES ('class-2', :learner_id, 'active')"
                ),
                {"learner_id": learner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.role_grants "
                    "(tenant_id, user_id, role, scope_type, scope_id) "
                    "VALUES (:tenant_id, :learner_id, 'student', 'class', 'class-2')"
                ),
                {"tenant_id": tenant_id, "learner_id": learner_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".course_generation_policies '
                    "(course_id, tenant_id, allow_student_micro, "
                    "allow_student_full, allowed_content_modes, daily_student_units, "
                    "monthly_student_units, updated_by) VALUES "
                    "('course-2', :tenant_id, true, false, 'open_creation', 5, 5, "
                    "'teacher-1')"
                ),
                {"tenant_id": tenant_id},
            )
        other_course = await released_service.evaluate(
            StudentGenerationRequest(
                course_id="course-2",
                class_id="class-2",
                mode="micro",
                content_mode="open_creation",
                web_search_requested=False,
            )
        )
        assert other_course.decision.outcome == "accepted"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM platform.audit_log WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(DropSchema(schema_name, cascade=True))
            await connection.execute(
                text("DELETE FROM platform.role_grants WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text("DELETE FROM platform.tenant_memberships WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()
