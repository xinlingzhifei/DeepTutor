from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
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
    StudentSafetyAssessmentRecord,
)
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.policies.student_generation import StudentGenerationRequest
from deeptutor.teaching.repositories import student_generation as student_generation_repository
from deeptutor.teaching.repositories.classrooms import (
    ClassroomPersistenceError,
    SqlAlchemyClassroomRepository,
)
from deeptutor.teaching.repositories.jobs import SqlAlchemyGenerationJobRepository
from deeptutor.teaching.repositories.student_generation import (
    SqlAlchemyStudentGenerationRepository,
    SqlAlchemyStudentSafetyEvaluator,
    StudentSafetyAssessment,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.student_classrooms import (
    SqlAlchemyStudentClassroomGeneration,
)
from deeptutor.teaching.services.student_generation import (
    StudentGenerationApprovalNotFound,
    StudentGenerationApprovalService,
    StudentGenerationService,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant

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
async def test_student_safety_duration_migration_backfills_and_blocks_lossy_downgrade(
    generation_database,
) -> None:
    tenant_id = f"safety-duration-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        migrated_0020 = run_tenant_migration(
            generation_database,
            schema_name,
            "20260825_0020",
        )
        assert migrated_0020.returncode == 0, f"{migrated_0020.stdout}\n{migrated_0020.stderr}"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.tenants (id, name, status) "
                    "VALUES (:tenant_id, 'Safety duration migration', 'active')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.tenant_schema_states "
                    "(tenant_id, schema_name, revision, status) "
                    "VALUES (:tenant_id, :schema_name, '20260825_0020', 'active')"
                ),
                {"tenant_id": tenant_id, "schema_name": schema_name},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".courses (id, title) '
                    "VALUES ('course-1', 'Course 1')"
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
                    f'INSERT INTO "{schema_name}".course_generation_policies '
                    "(course_id, tenant_id, allowed_content_modes, daily_student_units, "
                    "monthly_student_units, updated_by) VALUES "
                    "('course-1', :tenant_id, 'open_creation', 10, 100, 'reviewer-1')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".student_safety_assessments '
                    "(id, tenant_id, course_id, class_id, mode, content_mode, "
                    "web_search_requested, generally_safe, minor_safe, restricted_topic, "
                    "reviewed_by, reviewed_at, assessment_version, expires_at) VALUES "
                    "('assessment-1', :tenant_id, 'course-1', 'class-1', 'micro', "
                    "'open_creation', false, true, true, false, 'reviewer-1', "
                    "statement_timestamp(), 1, "
                    "statement_timestamp() + interval '7200.5 seconds')"
                ),
                {"tenant_id": tenant_id},
            )

        upgraded = run_tenant_migration(generation_database, schema_name, "20260827_0021")
        assert upgraded.returncode == 0, f"{upgraded.stdout}\n{upgraded.stderr}"

        async def inspect() -> dict[str, object]:
            async with engine.connect() as connection:
                valid_nullable = await connection.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema = :schema_name "
                        "AND table_name = 'student_safety_assessments' "
                        "AND column_name = 'valid_for_seconds'"
                    ),
                    {"schema_name": schema_name},
                )
                requested_nullable = await connection.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema = :schema_name "
                        "AND table_name = 'student_safety_assessments' "
                        "AND column_name = 'requested_expires_at'"
                    ),
                    {"schema_name": schema_name},
                )
                duration = None
                requested_window_seconds = None
                active_matches_requested = None
                if valid_nullable is not None:
                    duration = await connection.scalar(
                        text(
                            f'SELECT valid_for_seconds FROM "{schema_name}".'
                            "student_safety_assessments WHERE id = 'assessment-1'"
                        )
                    )
                if requested_nullable is not None:
                    requested_window_seconds = await connection.scalar(
                        text(
                            "SELECT EXTRACT(EPOCH FROM "
                            f'(requested_expires_at - reviewed_at))::double precision FROM "{schema_name}".'
                            "student_safety_assessments WHERE id = 'assessment-1'"
                        )
                    )
                    active_matches_requested = await connection.scalar(
                        text(
                            f'SELECT expires_at = requested_expires_at FROM "{schema_name}".'
                            "student_safety_assessments WHERE id = 'assessment-1'"
                        )
                    )

                async def constraint_exists(name: str) -> bool:
                    return bool(
                        await connection.scalar(
                            text(
                                "SELECT EXISTS ("
                                "SELECT 1 FROM information_schema.table_constraints "
                                "WHERE constraint_schema = :schema_name "
                                "AND table_name = 'student_safety_assessments' "
                                "AND constraint_name = :constraint_name)"
                            ),
                            {"schema_name": schema_name, "constraint_name": name},
                        )
                    )

                valid_constraint_exists = await constraint_exists(
                    "ck_student_safety_assessments_valid_for_seconds"
                )
                supersession_constraint_exists = await constraint_exists(
                    "ck_student_safety_assessments_supersession_window"
                )
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
            return {
                "duration": duration,
                "validNullable": valid_nullable,
                "requestedNullable": requested_nullable,
                "requestedWindowSeconds": requested_window_seconds,
                "activeMatchesRequested": active_matches_requested,
                "validConstraint": valid_constraint_exists,
                "supersessionConstraint": supersession_constraint_exists,
                "alembicRevision": alembic_revision,
                "stateRevision": state_revision,
            }

        assert await inspect() == {
            "duration": 7201,
            "validNullable": "NO",
            "requestedNullable": "NO",
            "requestedWindowSeconds": 7200.5,
            "activeMatchesRequested": True,
            "validConstraint": True,
            "supersessionConstraint": True,
            "alembicRevision": "20260827_0021",
            "stateRevision": "20260827_0021",
        }

        downgraded = run_tenant_migration(
            generation_database,
            schema_name,
            "-20260825_0020",
        )
        assert downgraded.returncode == 0, f"{downgraded.stdout}\n{downgraded.stderr}"
        assert await inspect() == {
            "duration": None,
            "validNullable": None,
            "requestedNullable": None,
            "requestedWindowSeconds": None,
            "activeMatchesRequested": None,
            "validConstraint": False,
            "supersessionConstraint": False,
            "alembicRevision": "20260825_0020",
            "stateRevision": "20260825_0020",
        }

        reupgraded = run_tenant_migration(generation_database, schema_name, "20260827_0021")
        assert reupgraded.returncode == 0, f"{reupgraded.stdout}\n{reupgraded.stderr}"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".student_safety_assessments '
                    "SET expires_at = expires_at - interval '1 hour' "
                    "WHERE id = 'assessment-1'"
                )
            )

        refused = run_tenant_migration(
            generation_database,
            schema_name,
            "-20260825_0020",
        )
        assert refused.returncode != 0
        assert "immutable request duration is required" in (refused.stdout + refused.stderr)
        assert await inspect() == {
            "duration": 7201,
            "validNullable": "NO",
            "requestedNullable": "NO",
            "requestedWindowSeconds": 7200.5,
            "activeMatchesRequested": False,
            "validConstraint": True,
            "supersessionConstraint": True,
            "alembicRevision": "20260827_0021",
            "stateRevision": "20260827_0021",
        }
    finally:
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
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_student_route_uses_exact_current_durable_safety_evidence(
    generation_database,
    monkeypatch,
) -> None:
    from deeptutor.api.routers import student_classrooms

    tenant_id = f"student-safety-{uuid.uuid4().hex[:12]}"
    learner_id = f"student-{uuid.uuid4().hex[:12]}"
    route_suffix = uuid.uuid4().hex[:12]
    provider_id = f"provider-{route_suffix}"
    route_id = f"route-{route_suffix}"
    worker_pool = f"workers-{route_suffix}"
    queue_ref = f"queue-{route_suffix}"
    schema_name = tenant_schema_name(tenant_id)
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    request = StudentGenerationRequest(
        course_id="course-1",
        class_id="class-1",
        mode="micro",
        content_mode="open_creation",
        web_search_requested=False,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.tenants "
                    "(id, name, status, data_plane_mode) "
                    "VALUES (:tenant_id, 'Student safety', 'active', 'shared')"
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
                    "INSERT INTO platform.provider_profiles "
                    "(id, scope, tenant_id, owner_key, provider_type, model_name, "
                    "api_base_url, secret_ref, status) VALUES "
                    "(:provider_id, 'shared', NULL, 'shared', 'openai-compatible', "
                    "'student-test-model', NULL, :secret_ref, 'active')"
                ),
                {
                    "provider_id": provider_id,
                    "secret_ref": f"tests/student/{route_suffix}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.data_plane_routes "
                    "(id, tenant_id, owner_key, mode, base_url, worker_pool, "
                    "queue_name, provider_profile_id, status, health_status) VALUES "
                    "(:route_id, NULL, 'shared', 'shared', 'http://openmaic.invalid', "
                    ":worker_pool, :queue_ref, :provider_id, 'active', 'healthy')"
                ),
                {
                    "route_id": route_id,
                    "worker_pool": worker_pool,
                    "queue_ref": queue_ref,
                    "provider_id": provider_id,
                },
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".courses (id, title) VALUES '
                    "('course-1', 'Physics'), ('course-2', 'Chemistry')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".classes (id, course_id, name) VALUES '
                    "('class-1', 'course-1', 'Class 1'), "
                    "('class-2', 'course-2', 'Class 2')"
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
                    "(course_id, tenant_id, allow_student_micro, allow_student_full, "
                    "allowed_content_modes, allow_web_search, daily_student_units, "
                    "monthly_student_units, updated_by) VALUES "
                    "('course-1', :tenant_id, true, true, "
                    "'source_grounded,open_creation', true, 0, 100, 'teacher-1'), "
                    "('course-2', :tenant_id, true, true, "
                    "'source_grounded,open_creation', true, 0, 100, 'teacher-1')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".student_safety_assessments '
                    "(id, tenant_id, course_id, class_id, mode, content_mode, "
                    "web_search_requested, generally_safe, minor_safe, restricted_topic, "
                    "reviewed_by, reviewed_at, assessment_version, valid_for_seconds, "
                    "requested_expires_at, expires_at) "
                    "VALUES ('assessment-current', :tenant_id, 'course-1', 'class-1', "
                    "'micro', 'open_creation', false, true, true, false, 'teacher-1', "
                    "statement_timestamp() - interval '1 minute', 1, 3660, "
                    "statement_timestamp() + interval '1 hour', "
                    "statement_timestamp() + interval '1 hour'), "
                    "('assessment-expired', :tenant_id, 'course-1', 'class-1', "
                    "'micro', 'open_creation', true, true, true, false, 'teacher-1', "
                    "statement_timestamp() - interval '2 hours', 1, 3600, "
                    "statement_timestamp() - interval '1 hour', "
                    "statement_timestamp() - interval '1 hour')"
                ),
                {"tenant_id": tenant_id},
            )

        job_repository = SqlAlchemyGenerationJobRepository(engine)
        await job_repository.grant_quota(
            tenant_id,
            grant_id=f"grant-{route_suffix}",
            units=100,
        )

        class Selector:
            async def resolve(self, selected_tenant_id: str):
                assert selected_tenant_id == tenant_id
                return DataPlaneSelection(
                    tenant_id=tenant_id,
                    route_ref=route_id,
                    provider_profile_ref=provider_id,
                    mode="shared",
                    worker_pool_ref=worker_pool,
                    queue_ref=queue_ref,
                )

        evaluator = SqlAlchemyStudentSafetyEvaluator(engine, tenant_id)
        current = await evaluator.assess(tenant_id, learner_id, request)
        assert current == StudentSafetyAssessment(True, True, False)
        assert await evaluator.assess("tenant-other", learner_id, request) == (
            StudentSafetyAssessment(False, False, True)
        )
        for drifted in (
            StudentGenerationRequest("course-2", "class-2", "micro", "open_creation", False),
            StudentGenerationRequest("course-1", "class-1", "full", "open_creation", False),
            StudentGenerationRequest("course-1", "class-1", "micro", "source_grounded", False),
            StudentGenerationRequest("course-1", "class-1", "micro", "open_creation", True),
        ):
            assert await evaluator.assess(tenant_id, learner_id, drifted) == (
                StudentSafetyAssessment(False, False, True)
            )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".student_safety_assessments '
                    "(id, tenant_id, course_id, class_id, mode, content_mode, "
                    "web_search_requested, generally_safe, minor_safe, restricted_topic, "
                    "reviewed_by, reviewed_at, assessment_version, valid_for_seconds, "
                    "requested_expires_at, expires_at) "
                    "VALUES ('assessment-ambiguous', :tenant_id, 'course-1', 'class-1', "
                    "'micro', 'open_creation', false, true, true, false, 'teacher-1', "
                    "statement_timestamp() - interval '1 minute', 2, 3660, "
                    "statement_timestamp() + interval '1 hour', "
                    "statement_timestamp() + interval '1 hour')"
                ),
                {"tenant_id": tenant_id},
            )
        assert await evaluator.assess(tenant_id, learner_id, request) == (
            StudentSafetyAssessment(False, False, True)
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'DELETE FROM "{schema_name}".student_safety_assessments '
                    "WHERE id = 'assessment-ambiguous'"
                )
            )

        context = TenantContext(
            tenant_id=tenant_id,
            schema_name=schema_name,
            user_id=learner_id,
            permissions=permissions_for_roles(
                {"student"},
                scope_type="class",
                scope_id="class-1",
                tenant_id=tenant_id,
            ),
        )
        monkeypatch.setattr(student_classrooms, "get_platform_engine", lambda: engine)
        application = FastAPI()
        application.include_router(student_classrooms.router, prefix="/api/v1")
        selected_context = {"value": context}
        application.dependency_overrides[require_tenant] = lambda: selected_context["value"]
        application.dependency_overrides[student_classrooms.get_source_repository] = lambda: (
            object()
        )
        application.dependency_overrides[student_classrooms.get_source_store_provider] = lambda: (
            object()
        )
        application.dependency_overrides[student_classrooms.get_job_repository] = lambda: (
            job_repository
        )
        application.dependency_overrides[student_classrooms.get_data_plane_selector] = Selector
        application.dependency_overrides[student_classrooms.get_cancellation_gateway] = lambda: (
            object()
        )
        client = TestClient(application)
        payload = {
            "courseId": "course-1",
            "classId": "class-1",
            "mode": "micro",
            "contentMode": "open_creation",
            "webSearchRequested": False,
        }

        accepted = client.post("/api/v1/student-classrooms", json=payload)

        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["status"] == "awaiting_approval"
        assert accepted.json()["generationJobId"] is None
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(f'SELECT count(*) FROM "{schema_name}".generation_jobs')
                )
                == 0
            )
        reviewer = TenantContext(
            tenant_id=tenant_id,
            schema_name=schema_name,
            user_id="teacher-1",
            permissions=permissions_for_roles(
                {"content_reviewer"},
                scope_type="class",
                scope_id="class-1",
                tenant_id=tenant_id,
            ),
        )
        approval_service = StudentGenerationApprovalService(
            tenant_id=tenant_id,
            repository=SqlAlchemyStudentGenerationRepository(
                engine,
                tenant_id,
                safety_evaluator=evaluator,
            ),
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".classroom_assets SET owner_id = '
                    "'tampered-owner' WHERE id = :asset_id"
                ),
                {"asset_id": accepted.json()["assetId"]},
            )
        with pytest.raises(StudentGenerationApprovalNotFound):
            await approval_service.approve(reviewer, accepted.json()["approvalId"])
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".classroom_assets SET owner_id = '
                    ":learner_id WHERE id = :asset_id"
                ),
                {
                    "asset_id": accepted.json()["assetId"],
                    "learner_id": learner_id,
                },
            )
            original_brief_id = await connection.scalar(
                text(
                    f'SELECT teaching_brief_id FROM "{schema_name}".classroom_drafts '
                    "WHERE classroom_id = :asset_id"
                ),
                {"asset_id": accepted.json()["assetId"]},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".teaching_briefs '
                    "(id, tenant_id, source_snapshot_id, course_id, class_id, "
                    "brief_version, document, document_sha256, created_by) "
                    "SELECT 'brief-wrong-binding', tenant_id, source_snapshot_id, "
                    "'course-2', 'class-2', brief_version, document, "
                    "document_sha256, created_by FROM "
                    f'"{schema_name}".teaching_briefs WHERE id = :brief_id'
                ),
                {"brief_id": original_brief_id},
            )
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".classroom_drafts SET teaching_brief_id = '
                    "'brief-wrong-binding' WHERE classroom_id = :asset_id"
                ),
                {"asset_id": accepted.json()["assetId"]},
            )
        with pytest.raises(StudentGenerationApprovalNotFound):
            await approval_service.approve(reviewer, accepted.json()["approvalId"])
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".classroom_drafts SET teaching_brief_id = '
                    ":brief_id WHERE classroom_id = :asset_id"
                ),
                {
                    "brief_id": original_brief_id,
                    "asset_id": accepted.json()["assetId"],
                },
            )
        approved = await approval_service.approve(
            reviewer,
            accepted.json()["approvalId"],
        )
        assert approved.status == "approved"
        async with engine.begin() as connection:
            initially_reserved = (
                await connection.execute(
                    text(
                        f"SELECT scene_min, scene_max, duration_minutes_min, "
                        f"duration_minutes_max, estimated_units, quota_state FROM "
                        f'"{schema_name}".student_generation_requests '
                        "WHERE id = :request_id"
                    ),
                    {"request_id": accepted.json()["requestId"]},
                )
            ).one()
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".student_generation_requests SET '
                    "quota_state = 'settled' WHERE id = :request_id"
                ),
                {"request_id": accepted.json()["requestId"]},
            )
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".course_generation_policies SET '
                    "micro_scene_limit = 2, daily_student_units = 2, "
                    "monthly_student_units = 2, updated_by = 'teacher-2' "
                    "WHERE course_id = 'course-1'"
                )
            )
        assert tuple(initially_reserved) == (1, 5, 3, 25, 5, "reserved")
        refreshed_approval = await approval_service.approve(
            reviewer,
            accepted.json()["approvalId"],
        )
        assert refreshed_approval.status == "approved"
        classroom_repository = SqlAlchemyClassroomRepository(engine, tenant_id)
        unbound_record = await classroom_repository.start_student_generation(
            accepted.json()["assetId"],
            "micro",
        )
        recovery_generation = SqlAlchemyStudentClassroomGeneration(
            job_repository,
            Selector(),
        )
        unbound_stage = await recovery_generation.start(
            context=context,
            record=unbound_record,
            estimate=SimpleNamespace(scene_range=(1, 2), quota_units=2),
            mode="micro",
            actor_id=reviewer.user_id,
        )
        assert (await classroom_repository.get_workflow(unbound_record.asset_id)).job_id is None

        second_reviewer = TenantContext(
            tenant_id=tenant_id,
            schema_name=schema_name,
            user_id="teacher-2",
            permissions=permissions_for_roles(
                {"content_reviewer"},
                scope_type="class",
                scope_id="class-1",
                tenant_id=tenant_id,
            ),
        )
        selected_context["value"] = second_reviewer

        recoverable = client.get("/api/v1/student-generation-approvals")

        assert recoverable.status_code == 200, recoverable.text
        assert [item["approvalId"] for item in recoverable.json()["items"]] == [
            approved.approval_id
        ]
        recovered = client.post(
            f"/api/v1/student-generation-approvals/{accepted.json()['approvalId']}/approve",
            json={},
        )

        assert recovered.status_code == 202, recovered.text
        recovered_job_id = recovered.json()["generationJobId"]
        assert recovered.json()["status"] == "approved"
        assert recovered_job_id == unbound_stage.job_id
        async with engine.connect() as connection:
            refreshed_reservation = (
                await connection.execute(
                    text(
                        f"SELECT scene_min, scene_max, duration_minutes_min, "
                        f"duration_minutes_max, estimated_units, quota_state FROM "
                        f'"{schema_name}".student_generation_requests '
                        "WHERE id = :request_id"
                    ),
                    {"request_id": accepted.json()["requestId"]},
                )
            ).one()
            refreshed_job_units = await connection.scalar(
                text(f'SELECT quota_units FROM "{schema_name}".generation_jobs WHERE id = :job_id'),
                {"job_id": recovered_job_id},
            )
        assert tuple(refreshed_reservation) == (1, 2, 3, 10, 2, "reserved")
        assert refreshed_job_units == 2
        hidden_after_binding = client.get("/api/v1/student-generation-approvals")
        assert hidden_after_binding.status_code == 200
        assert hidden_after_binding.json()["items"] == []
        async with engine.connect() as connection:
            job_count = await connection.scalar(
                text(
                    f'SELECT count(*) FROM "{schema_name}".generation_jobs '
                    "WHERE classroom_draft_id IS NOT NULL"
                )
            )
            bound_job_id = await connection.scalar(
                text(
                    f'SELECT generation_job_id FROM "{schema_name}".classroom_drafts '
                    "WHERE classroom_id = :asset_id"
                ),
                {"asset_id": accepted.json()["assetId"]},
            )
        assert (job_count, bound_job_id) == (1, recovered_job_id)

        selected_context["value"] = context
        weak_idempotent_restart = client.post(
            "/api/v1/student-classrooms",
            json=payload,
        )
        assert weak_idempotent_restart.status_code == 202
        weak_idempotent_approval = await approval_service.approve(
            reviewer,
            weak_idempotent_restart.json()["approvalId"],
        )
        assert weak_idempotent_approval.status == "approved"
        weak_idempotent_record = await classroom_repository.start_student_generation(
            weak_idempotent_restart.json()["assetId"],
            "micro",
        )
        weak_idempotent_stage = await recovery_generation.start(
            context=context,
            record=replace(weak_idempotent_record, owner_id="wrong-owner"),
            estimate=SimpleNamespace(scene_range=(1, 2), quota_units=2),
            mode="micro",
            actor_id=reviewer.user_id,
        )

        class FirstReadUnavailableJobRepository:
            def __init__(self, delegate) -> None:
                self._delegate = delegate
                self._detail_reads = 0
                self.create_returned = False

            def __getattr__(self, name: str):
                return getattr(self._delegate, name)

            async def create_job_and_reserve(self, request):
                await self._delegate.create_job_and_reserve(request)
                self.create_returned = True

            async def get_job_details(self, selected_tenant_id: str, job_id: str):
                self._detail_reads += 1
                if self._detail_reads == 1:
                    raise RuntimeError("pre-read unavailable")
                return await self._delegate.get_job_details(
                    selected_tenant_id,
                    job_id,
                )

        weak_idempotent_repository = FirstReadUnavailableJobRepository(job_repository)
        application.dependency_overrides[student_classrooms.get_job_repository] = lambda: (
            weak_idempotent_repository
        )
        selected_context["value"] = reviewer
        weak_idempotent_replay = client.post(
            "/api/v1/student-generation-approvals/"
            f"{weak_idempotent_restart.json()['approvalId']}/approve",
            json={},
        )
        application.dependency_overrides[student_classrooms.get_job_repository] = lambda: (
            job_repository
        )
        async with engine.connect() as connection:
            weak_idempotent_state = (
                await connection.execute(
                    text(
                        f"SELECT approval.status, request.quota_state, "
                        f"asset.lifecycle_state, draft.generation_job_id FROM "
                        f'"{schema_name}".student_generation_approvals AS approval '
                        f'JOIN "{schema_name}".student_generation_requests AS request '
                        "ON request.id = approval.request_id "
                        f'JOIN "{schema_name}".student_classroom_assets AS marker '
                        "ON marker.request_id = request.id "
                        f'JOIN "{schema_name}".classroom_assets AS asset '
                        "ON asset.id = marker.asset_id "
                        f'JOIN "{schema_name}".classroom_drafts AS draft '
                        "ON draft.classroom_id = asset.id "
                        "WHERE approval.id = :approval_id"
                    ),
                    {"approval_id": weak_idempotent_restart.json()["approvalId"]},
                )
            ).one()
            weak_idempotent_job_state = (
                await connection.execute(
                    text(
                        f'SELECT status, cancel_requested FROM "{schema_name}".'
                        "generation_jobs WHERE id = :job_id"
                    ),
                    {"job_id": weak_idempotent_stage.job_id},
                )
            ).one()
            weak_idempotent_active_jobs = await connection.scalar(
                text(
                    f'SELECT count(*) FROM "{schema_name}".generation_jobs '
                    "WHERE classroom_draft_id = :draft_id "
                    "AND status NOT IN ('succeeded', 'failed', 'canceled')"
                ),
                {"draft_id": weak_idempotent_record.draft_id},
            )
        assert weak_idempotent_replay.status_code == 503, weak_idempotent_replay.text
        assert weak_idempotent_repository.create_returned is True
        assert weak_idempotent_repository._detail_reads == 2
        assert tuple(weak_idempotent_state) == (
            "expired",
            "released",
            "canceled",
            None,
        )
        assert weak_idempotent_job_state[0] == "canceled" or weak_idempotent_job_state[1] is True
        assert weak_idempotent_active_jobs == 0

        selected_context["value"] = context
        cross_reviewer_restart = client.post("/api/v1/student-classrooms", json=payload)
        assert cross_reviewer_restart.status_code == 202, cross_reviewer_restart.text
        approved_by_first_reviewer = await approval_service.approve(
            reviewer,
            cross_reviewer_restart.json()["approvalId"],
        )
        assert approved_by_first_reviewer.status == "approved"
        stale_record = await classroom_repository.start_student_generation(
            cross_reviewer_restart.json()["assetId"],
            "micro",
        )
        stale_stage = await recovery_generation.start(
            context=context,
            record=stale_record,
            estimate=SimpleNamespace(scene_range=(1, 2), quota_units=2),
            mode="micro",
            actor_id=reviewer.user_id,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".course_generation_policies SET '
                    "micro_scene_limit = 1, daily_student_units = 1, "
                    "monthly_student_units = 1, updated_by = 'teacher-3' "
                    "WHERE course_id = 'course-1'"
                )
            )

        selected_context["value"] = second_reviewer
        failed_restart = client.post(
            "/api/v1/student-generation-approvals/"
            f"{cross_reviewer_restart.json()['approvalId']}/approve",
            json={},
        )
        async with engine.begin() as connection:
            failed_restart_state = (
                await connection.execute(
                    text(
                        f"SELECT approval.status, request.quota_state, "
                        f"asset.lifecycle_state, draft.generation_job_id FROM "
                        f'"{schema_name}".student_generation_approvals AS approval '
                        f'JOIN "{schema_name}".student_generation_requests AS request '
                        "ON request.id = approval.request_id "
                        f'JOIN "{schema_name}".student_classroom_assets AS marker '
                        "ON marker.request_id = request.id "
                        f'JOIN "{schema_name}".classroom_assets AS asset '
                        "ON asset.id = marker.asset_id "
                        f'JOIN "{schema_name}".classroom_drafts AS draft '
                        "ON draft.classroom_id = asset.id "
                        "WHERE approval.id = :approval_id"
                    ),
                    {"approval_id": cross_reviewer_restart.json()["approvalId"]},
                )
            ).one()
            stale_job_state = (
                await connection.execute(
                    text(
                        f'SELECT status, cancel_requested FROM "{schema_name}".'
                        "generation_jobs WHERE id = :job_id"
                    ),
                    {"job_id": stale_stage.job_id},
                )
            ).one()
            active_job_count = await connection.scalar(
                text(
                    f'SELECT count(*) FROM "{schema_name}".generation_jobs '
                    "WHERE classroom_draft_id = :draft_id "
                    "AND status NOT IN ('succeeded', 'failed', 'canceled')"
                ),
                {"draft_id": stale_record.draft_id},
            )
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".course_generation_policies SET '
                    "micro_scene_limit = 2, daily_student_units = 2, "
                    "monthly_student_units = 2, updated_by = 'teacher-2' "
                    "WHERE course_id = 'course-1'"
                )
            )
        assert tuple(failed_restart_state) == ("expired", "released", "canceled", None)
        assert stale_job_state[0] == "canceled" or stale_job_state[1] is True
        assert active_job_count == 0
        assert failed_restart.status_code == 503, failed_restart.text

        selected_context["value"] = context
        raced = client.post("/api/v1/student-classrooms", json=payload)
        assert raced.status_code == 202, raced.text
        raced_record = await classroom_repository.start_student_generation(
            raced.json()["assetId"],
            "micro",
        )
        raced_generation = SqlAlchemyStudentClassroomGeneration(
            job_repository,
            Selector(),
        )
        raced_stage = await raced_generation.start(
            context=context,
            record=raced_record,
            estimate=SimpleNamespace(scene_range=(1, 5), quota_units=5),
            mode="micro",
            actor_id=learner_id,
        )
        await classroom_repository.mark_canceled(raced_record.asset_id)
        try:
            with pytest.raises(ClassroomPersistenceError):
                await classroom_repository.attach_generation_job(
                    raced_record.asset_id,
                    raced_stage.job_id,
                    "content",
                )
        finally:
            await raced_generation.request_cancel(tenant_id, raced_stage.job_id)
            await SqlAlchemyStudentGenerationRepository(
                engine,
                tenant_id,
                safety_evaluator=evaluator,
            ).cancel_request(
                tenant_id,
                learner_id,
                raced.json()["requestId"],
            )

        source_revoked = client.post("/api/v1/student-classrooms", json=payload)
        assert source_revoked.status_code == 202, source_revoked.text
        source_reserved = await approval_service.approve(
            reviewer,
            source_revoked.json()["approvalId"],
        )
        assert source_reserved.status == "approved"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".student_generation_requests SET '
                    "quota_state = 'settled' WHERE id = :request_id"
                ),
                {"request_id": source_revoked.json()["requestId"]},
            )
            source_brief_id = await connection.scalar(
                text(
                    f'SELECT teaching_brief_id FROM "{schema_name}".classroom_drafts '
                    "WHERE classroom_id = :asset_id"
                ),
                {"asset_id": source_revoked.json()["assetId"]},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".source_snapshots '
                    "(id, tenant_id, source_type, source_id, resource_owner_id, "
                    "source_upload_id, display_name, source_revision, content_sha256, "
                    "permission_sha256, citation_manifest, created_by) VALUES "
                    "('snapshot-approval-original', :tenant_id, 'manual', "
                    "'source-approval-original', 'tenant-workspace', NULL, NULL, "
                    "'revision-1', :content_sha256, :permission_sha256, '[]', 'teacher-1')"
                ),
                {
                    "tenant_id": tenant_id,
                    "content_sha256": "a" * 64,
                    "permission_sha256": "b" * 64,
                },
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".tenant_source_bindings '
                    "(id, tenant_id, source_snapshot_id, course_id, class_id, bound_by) "
                    "VALUES ('binding-approval-original', :tenant_id, "
                    "'snapshot-approval-original', 'course-1', 'class-1', 'teacher-1')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".teaching_briefs '
                    "(id, tenant_id, source_snapshot_id, course_id, class_id, "
                    "brief_version, document, document_sha256, created_by) "
                    "SELECT 'brief-source-approval', tenant_id, "
                    "'snapshot-approval-original', course_id, class_id, brief_version, "
                    "document, document_sha256, created_by FROM "
                    f'"{schema_name}".teaching_briefs WHERE id = :brief_id'
                ),
                {"brief_id": source_brief_id},
            )
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".classroom_drafts SET teaching_brief_id = '
                    "'brief-source-approval' WHERE classroom_id = :asset_id"
                ),
                {"asset_id": source_revoked.json()["assetId"]},
            )
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".student_generation_requests SET '
                    "content_mode = 'source_grounded' WHERE id = :request_id"
                ),
                {"request_id": source_revoked.json()["requestId"]},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".student_safety_assessments '
                    "(id, tenant_id, course_id, class_id, mode, content_mode, "
                    "web_search_requested, generally_safe, minor_safe, restricted_topic, "
                    "reviewed_by, reviewed_at, assessment_version, valid_for_seconds, "
                    "requested_expires_at, expires_at) VALUES "
                    "('assessment-source-grounded', :tenant_id, 'course-1', 'class-1', "
                    "'micro', 'source_grounded', false, true, true, false, 'teacher-1', "
                    "statement_timestamp() - interval '1 minute', 1, 3660, "
                    "statement_timestamp() + interval '1 hour', "
                    "statement_timestamp() + interval '1 hour')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    f'DELETE FROM "{schema_name}".tenant_source_bindings '
                    "WHERE id = 'binding-approval-original'"
                )
            )

        expired_source = await approval_service.approve(
            reviewer,
            source_revoked.json()["approvalId"],
        )

        assert expired_source.status == "expired"
        async with engine.connect() as connection:
            source_request_state = (
                await connection.execute(
                    text(
                        f'SELECT decision_outcome, quota_state FROM "{schema_name}".'
                        "student_generation_requests WHERE id = :request_id"
                    ),
                    {"request_id": source_revoked.json()["requestId"]},
                )
            ).one()
        assert source_request_state == ("accepted", "released")

        locked = client.post("/api/v1/student-classrooms", json=payload)
        assert locked.status_code == 202, locked.text
        approval_gate_key = int(uuid.uuid4().hex[:12], 16)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'CREATE FUNCTION "{schema_name}".hold_student_approval_update() '
                    "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                    f"PERFORM pg_advisory_xact_lock({approval_gate_key}); "
                    "RETURN NEW; END $$"
                )
            )
            await connection.execute(
                text(
                    f"CREATE TRIGGER hold_student_approval_update BEFORE UPDATE ON "
                    f'"{schema_name}".student_generation_requests FOR EACH ROW '
                    f"WHEN (OLD.id = '{locked.json()['requestId']}') "
                    f'EXECUTE FUNCTION "{schema_name}".hold_student_approval_update()'
                )
            )
        gate_connection = await engine.connect()
        approval_task = None
        owner_update_task = None
        try:
            gate_pid = await gate_connection.scalar(text("SELECT pg_backend_pid()"))
            await gate_connection.execute(
                text("SELECT pg_advisory_lock(:gate_key)"),
                {"gate_key": approval_gate_key},
            )
            approval_task = asyncio.create_task(
                approval_service.approve(reviewer, locked.json()["approvalId"])
            )
            for _attempt in range(100):
                async with engine.connect() as connection:
                    approval_is_waiting = await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks waiting "
                            "JOIN pg_locks holding ON "
                            "holding.locktype = waiting.locktype "
                            "AND holding.database IS NOT DISTINCT FROM waiting.database "
                            "AND holding.classid = waiting.classid "
                            "AND holding.objid = waiting.objid "
                            "AND holding.objsubid = waiting.objsubid "
                            "WHERE waiting.locktype = 'advisory' "
                            "AND waiting.granted = false AND holding.granted = true "
                            "AND holding.pid = :gate_pid)"
                        ),
                        {"gate_pid": gate_pid},
                    )
                if approval_is_waiting:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("approval did not reach the locked update gate")

            async def tamper_locked_owner() -> None:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            f'UPDATE "{schema_name}".classroom_assets SET owner_id = '
                            "'tampered-after-lock' WHERE id = :asset_id"
                        ),
                        {"asset_id": locked.json()["assetId"]},
                    )

            owner_update_task = asyncio.create_task(tamper_locked_owner())
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(owner_update_task),
                    timeout=0.25,
                )
            await gate_connection.execute(
                text("SELECT pg_advisory_unlock(:gate_key)"),
                {"gate_key": approval_gate_key},
            )
            locked_approved = await approval_task
            await owner_update_task
        finally:
            await gate_connection.execute(text("SELECT pg_advisory_unlock_all()"))
            await gate_connection.close()
            if approval_task is not None and not approval_task.done():
                approval_task.cancel()
            if owner_update_task is not None and not owner_update_task.done():
                owner_update_task.cancel()
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"DROP TRIGGER IF EXISTS hold_student_approval_update ON "
                        f'"{schema_name}".student_generation_requests'
                    )
                )
                await connection.execute(
                    text(f'DROP FUNCTION IF EXISTS "{schema_name}".hold_student_approval_update()')
                )
        assert locked_approved.status == "approved"
        with pytest.raises(StudentGenerationApprovalNotFound):
            await approval_service.approve(reviewer, locked.json()["approvalId"])
        cleanup_repository = SqlAlchemyStudentGenerationRepository(
            engine,
            tenant_id,
            safety_evaluator=evaluator,
        )
        with pytest.raises(StudentGenerationApprovalNotFound):
            await cleanup_repository.abort_approved_request(
                tenant_id,
                reviewer.user_id,
                locked.json()["approvalId"],
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'UPDATE "{schema_name}".classroom_assets SET owner_id = '
                    ":learner_id WHERE id = :asset_id"
                ),
                {
                    "learner_id": learner_id,
                    "asset_id": locked.json()["assetId"],
                },
            )
        await cleanup_repository.abort_approved_request(
            tenant_id,
            reviewer.user_id,
            locked.json()["approvalId"],
        )
        await classroom_repository.mark_canceled(locked.json()["assetId"])

        selected_context["value"] = reviewer
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DELETE FROM "{schema_name}".student_safety_assessments')
            )

        replayed_bound = client.post(
            f"/api/v1/student-generation-approvals/{accepted.json()['approvalId']}/approve",
            json={},
        )

        assert replayed_bound.status_code == 202, replayed_bound.text
        assert replayed_bound.json()["generationJobId"] == recovered_job_id
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        f'SELECT count(*) FROM "{schema_name}".generation_jobs WHERE id = :job_id'
                    ),
                    {"job_id": recovered_job_id},
                )
                == 1
            )

        missing = client.post("/api/v1/student-classrooms", json=payload)

        assert missing.status_code == 403
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
                text("DELETE FROM platform.tenant_schema_states WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text("DELETE FROM platform.data_plane_routes WHERE id = :route_id"),
                {"route_id": route_id},
            )
            await connection.execute(
                text("DELETE FROM platform.provider_profiles WHERE id = :provider_id"),
                {"provider_id": provider_id},
            )
            await connection.execute(
                text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()


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
                        "'student_classroom_assets', 'student_classroom_copies', "
                        "'student_safety_assessments')"
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
                        "'student_classroom_assets', 'student_classroom_copies', "
                        "'student_safety_assessments')"
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
            "student_safety_assessments",
        }
        learning_tables = {
            "classroom_ticket_consumptions",
            "learning_sessions",
            "learning_events",
            "learning_projection_queue",
            "quiz_attempts",
            "mastery_evidence",
            "mastery_levels",
            "learning_progress",
            "learning_event_quarantine",
            "pbl_grading_results",
            "pbl_grading_idempotency_keys",
        }
        assert tables - tables_before == new_tables | learning_tables
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
            "fk_student_safety_assessments_class_course",
            "fk_student_safety_assessments_policy_tenant",
            "uq_student_safety_assessments_binding_version",
            "ck_student_safety_assessments_valid_for_seconds",
            "ck_student_safety_assessments_supersession_window",
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
                StudentSafetyAssessmentRecord,
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
        assert (revision, state_revision) == ("20260827_0021", "20260827_0021")

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
        assert "pbl_grading_results" not in remaining
        assert "pbl_grading_idempotency_keys" not in remaining
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
        assert (revision, state_revision) == ("20260827_0021", "20260827_0021")
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
