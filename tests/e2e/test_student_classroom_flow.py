"""Acceptance coverage for private student-owned classroom generation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.api.routers import student_classrooms
from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.contracts import (
    ClassroomDocument,
    GenerationMetadata,
    GenerationRequest,
    KnowledgeCoverage,
    OutlineBundle,
    OutlineConfirmationMetadata,
    OutlineScene,
    canonical_json_bytes,
)
from deeptutor.teaching.dispatcher import OutboxDispatcher
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.repositories.classrooms import SqlAlchemyClassroomRepository
from deeptutor.teaching.repositories.jobs import (
    MaterializedArtifactInput,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.repositories.student_generation import (
    SqlAlchemyStudentGenerationRepository,
    SqlAlchemyStudentSafetyEvaluator,
)
from deeptutor.teaching.scheduler import FairScheduler
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.classrooms import ClassroomService
from deeptutor.teaching.services.student_classrooms import (
    SqlAlchemyStudentClassroomGeneration,
    SqlAlchemyStudentClassroomWorkflow,
    StudentClassroomService,
)
from deeptutor.teaching.services.student_generation import (
    StudentGenerationApprovalService,
    StudentGenerationService,
)
from deeptutor.teaching.source_snapshots import SourceSnapshotBuilder
from deeptutor.teaching.tenant_context import TenantContext, require_tenant
from tests.teaching_contract_fixtures import valid_classroom_document

pytest_plugins = ("tests.teaching.integration.conftest",)


class _UnusedSourceRepository:
    async def require_authorized_source(self, **_kwargs):  # pragma: no cover
        raise AssertionError("open creation must not resolve a source")

    async def persist_authorized_snapshot(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("open creation must not persist a source")


class _UnusedStoreProvider:
    async def store_for_tenant(self, _tenant_id: str):  # pragma: no cover
        raise AssertionError("open creation must not access object storage")


class _Selector:
    def __init__(self, selection: DataPlaneSelection) -> None:
        self._selection = selection

    async def resolve(self, tenant_id: str) -> DataPlaneSelection:
        assert tenant_id == self._selection.tenant_id
        return self._selection


def _context(tenant_id: str, user_id: str, role: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=tenant_schema_name(tenant_id),
        user_id=user_id,
        permissions=permissions_for_roles(
            {role},
            scope_type="tenant",
            scope_id=tenant_id,
            tenant_id=tenant_id,
        ),
    )


def _service(
    *,
    engine,
    context: TenantContext,
    selector: _Selector,
    job_repository: SqlAlchemyGenerationJobRepository,
) -> StudentClassroomService:
    request_repository = SqlAlchemyStudentGenerationRepository(
        engine,
        context.tenant_id,
        safety_evaluator=SqlAlchemyStudentSafetyEvaluator(
            engine,
            context.tenant_id,
        ),
    )
    classroom_repository = SqlAlchemyClassroomRepository(engine, context.tenant_id)
    snapshots = SourceSnapshotBuilder(
        context,
        _UnusedSourceRepository(),
        store_provider=_UnusedStoreProvider(),
    )
    brief_builder = TeachingBriefBuilder(context, snapshots)
    generation = SqlAlchemyStudentClassroomGeneration(job_repository, selector)
    classroom_service = ClassroomService(
        classroom_repository,
        brief_builder,
        generation,
        _UnusedStoreProvider(),
        student_owner_only=True,
    )
    workflow = SqlAlchemyStudentClassroomWorkflow(
        repository=classroom_repository,
        classroom_service=classroom_service,
        brief_builder=brief_builder,
        generation=generation,
        request_repository=request_repository,
    )
    return StudentClassroomService(
        policy_service=StudentGenerationService(
            tenant_id=context.tenant_id,
            learner_id=context.user_id,
            repository=request_repository,
        ),
        workflow=workflow,
        approval_service=StudentGenerationApprovalService(
            tenant_id=context.tenant_id,
            repository=request_repository,
        ),
        source_authorizer=snapshots,
    )


def _open_creation_document(asset_id: str, version_id: str) -> tuple[str, str, str]:
    raw = valid_classroom_document()
    raw["classroom_id"] = asset_id
    raw["classroom_version_id"] = version_id
    raw["content_mode"] = "open_creation"
    raw["open_creation"] = True
    raw["source_refs"] = []
    for mapping in raw["knowledge_point_mappings"]:
        mapping["source_refs"] = []
    raw["media_manifest"] = []
    raw["export_manifest"] = []
    normalized = ClassroomDocument.model_validate(raw).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    without_hash = dict(normalized)
    without_hash.pop("fileSha256")
    normalized["fileSha256"] = hashlib.sha256(
        canonical_json_bytes(without_hash)
    ).hexdigest()
    document = ClassroomDocument.model_validate(normalized)
    payload = canonical_json_bytes(document).decode()
    return (
        payload,
        hashlib.sha256(payload.encode()).hexdigest(),
        hashlib.sha256(canonical_json_bytes(normalized["mediaManifest"])).hexdigest(),
    )


async def _finish_micro_job(
    *,
    engine,
    tenant_id: str,
    asset_id: str,
    selection: DataPlaneSelection,
    repository: SqlAlchemyGenerationJobRepository,
) -> str:
    dispatched = await OutboxDispatcher(engine).dispatch_next()
    assert dispatched is not None
    scheduler = FairScheduler(engine)
    await scheduler.ensure_generation_capacity(
        (tenant_id,),
        worker_pool_ref=selection.worker_pool_ref,
    )
    claim = await scheduler.claim(
        "generation",
        data_plane_route_id=selection.route_ref,
        provider_profile_id=selection.provider_profile_ref,
        worker_pool_ref=selection.worker_pool_ref,
        queue_ref=selection.queue_ref,
        worker_id=f"student-e2e-worker-{uuid.uuid4().hex[:8]}",
        lease_seconds=60,
    )
    assert claim is not None
    assert claim.phase == "content"
    await repository.transition_claim(
        claim,
        expected_status="generating_content",
        target_status="validating",
        progress_percent=80,
    )
    await repository.transition_claim(
        claim,
        expected_status="validating",
        target_status="materializing",
        progress_percent=90,
    )
    target = await repository.prepare_promotion(claim, classroom_id=asset_id)
    manifest_sha256 = "c" * 64
    await repository.bind_promotion_manifest(
        claim,
        manifest_sha256=manifest_sha256,
    )
    await repository.mark_object_committed(
        claim,
        manifest_sha256=manifest_sha256,
    )
    version_id = f"{asset_id}:generated-v{target.version_number}"
    payload, document_sha256, media_manifest_sha256 = _open_creation_document(
        asset_id,
        version_id,
    )
    await repository.finalize_generation(
        claim,
        classroom_version_id=version_id,
        document_payload=payload,
        document_sha256=document_sha256,
        media_manifest_sha256=media_manifest_sha256,
        manifest_sha256=manifest_sha256,
        artifacts=(
            MaterializedArtifactInput(
                relative_name="classroom.json",
                object_key=f"tests/{tenant_id}/{version_id}/classroom.json",
                sha256=document_sha256,
                size_bytes=len(payload.encode()),
                mime_type="application/json",
                artifact_kind="dsl_json",
            ),
        ),
    )
    return version_id


async def _complete_outline_job(
    *,
    engine,
    tenant_id: str,
    selection: DataPlaneSelection,
    repository: SqlAlchemyGenerationJobRepository,
) -> str:
    dispatched = await OutboxDispatcher(engine).dispatch_next()
    assert dispatched is not None
    scheduler = FairScheduler(engine)
    await scheduler.ensure_generation_capacity(
        (tenant_id,),
        worker_pool_ref=selection.worker_pool_ref,
    )
    claim = await scheduler.claim(
        "generation",
        data_plane_route_id=selection.route_ref,
        provider_profile_id=selection.provider_profile_ref,
        worker_pool_ref=selection.worker_pool_ref,
        queue_ref=selection.queue_ref,
        worker_id=f"student-outline-worker-{uuid.uuid4().hex[:8]}",
        lease_seconds=60,
    )
    assert claim is not None
    assert claim.phase == "outline"
    details = await repository.get_job_details(tenant_id, claim.job_id)
    assert details is not None
    request = GenerationRequest.model_validate_json(details.request_payload)
    outline = OutlineBundle(
        schema_version="1.0",
        outline_id=f"outline-{claim.job_id}",
        outline_version=1,
        confirmation_metadata=OutlineConfirmationMetadata(status="draft"),
        title="Student full classroom",
        language="en-US",
        scenes=[
            OutlineScene(
                scene_id="scene-student-full",
                title="First explanation",
                summary="Explain the selected topic.",
                knowledge_point_ids=["student-topic"],
                source_refs=[],
            )
        ],
        knowledge_coverage=[
            KnowledgeCoverage(
                knowledge_point_id="student-topic",
                scene_ids=["scene-student-full"],
            )
        ],
        source_refs=[],
        estimated_scene_count=1,
        generation_metadata=GenerationMetadata(
            generator="openmaic",
            generator_version="0.3.1",
            model_id="server-selected-model",
            generated_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            teaching_brief_id=request.teaching_brief_id,
            teaching_brief_sha256=request.teaching_brief_sha256,
            template_id=request.template_id,
            template_version=request.template_version,
        ),
        contract_sha256=(
            "a45b0310d5b58a8e2d461ccfa9d60be24615583825a1f3a4f4460672cbd19ba5"
        ),
    )
    await repository.complete_outline(
        claim,
        result_payload=canonical_json_bytes(outline).decode(),
    )
    return claim.job_id


@pytest.mark.asyncio
async def test_student_micro_full_and_approval_flows_stay_private(
    generation_database,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"student-e2e-{suffix}"
    learner_id = f"learner-{suffix}"
    schema_name = tenant_schema_name(tenant_id)
    provider_id = f"student-provider-{suffix}"
    route_id = f"student-route-{suffix}"
    worker_pool = f"student-workers-{suffix}"
    queue_ref = f"student.queue.{suffix}"
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    selection = DataPlaneSelection(
        tenant_id=tenant_id,
        route_ref=route_id,
        provider_profile_ref=provider_id,
        mode="shared",
        worker_pool_ref=worker_pool,
        queue_ref=queue_ref,
    )
    job_repository = SqlAlchemyGenerationJobRepository(engine)
    learner = _context(tenant_id, learner_id, "student")
    other_learner_id = f"other-{suffix}"
    teacher_id = f"teacher-{suffix}"
    other_learner = _context(tenant_id, other_learner_id, "student")
    teacher = _context(tenant_id, teacher_id, "content_reviewer")
    selected_context = {"value": learner}
    application = FastAPI()
    application.include_router(student_classrooms.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = lambda: selected_context["value"]
    application.dependency_overrides[
        student_classrooms.get_student_classroom_service
    ] = lambda: _service(
        engine=engine,
        context=selected_context["value"],
        selector=_Selector(selection),
        job_repository=job_repository,
    )

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.tenants "
                    "(id, name, status, data_plane_mode) "
                    "VALUES (:tenant_id, 'Student E2E', 'active', 'shared')"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.tenant_memberships (tenant_id, user_id) "
                    "VALUES (:tenant_id, :learner_id), (:tenant_id, :other_learner_id), "
                    "(:tenant_id, :teacher_id)"
                ),
                {
                    "tenant_id": tenant_id,
                    "learner_id": learner_id,
                    "other_learner_id": other_learner_id,
                    "teacher_id": teacher_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.role_grants "
                    "(tenant_id, user_id, role, scope_type, scope_id) "
                    "VALUES (:tenant_id, :learner_id, 'student', 'tenant', :tenant_id), "
                    "(:tenant_id, :other_learner_id, 'student', 'tenant', :tenant_id), "
                    "(:tenant_id, :teacher_id, 'content_reviewer', 'tenant', :tenant_id)"
                ),
                {
                    "tenant_id": tenant_id,
                    "learner_id": learner_id,
                    "other_learner_id": other_learner_id,
                    "teacher_id": teacher_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.provider_profiles "
                    "(id, scope, tenant_id, owner_key, provider_type, model_name, "
                    "api_base_url, secret_ref, status) VALUES "
                    "(:provider_id, 'shared', NULL, 'shared', 'openai-compatible', "
                    "'student-e2e-model', NULL, :secret_ref, 'active')"
                ),
                {
                    "provider_id": provider_id,
                    "secret_ref": f"tests/student-e2e/{suffix}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.data_plane_routes "
                    "(id, tenant_id, owner_key, mode, base_url, worker_pool, queue_name, "
                    "provider_profile_id, status, health_status) VALUES "
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
                    "('course-micro', 'Micro physics'), "
                    "('course-full', 'Full physics'), "
                    "('course-blocked', 'Blocked physics'), "
                    "('course-approval', 'Approval physics')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".classes (id, course_id, name) VALUES '
                    "('class-micro', 'course-micro', 'Micro class'), "
                    "('class-full', 'course-full', 'Full class'), "
                    "('class-blocked', 'course-blocked', 'Blocked class'), "
                    "('class-approval', 'course-approval', 'Approval class')"
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".enrollments '
                    "(class_id, learner_id, status) "
                    "VALUES ('class-micro', :learner_id, 'active'), "
                    "('class-full', :learner_id, 'active'), "
                    "('class-blocked', :learner_id, 'active'), "
                    "('class-approval', :learner_id, 'active'), "
                    "('class-micro', :other_learner_id, 'active')"
                ),
                {
                    "learner_id": learner_id,
                    "other_learner_id": other_learner_id,
                },
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".course_generation_policies '
                    "(course_id, tenant_id, allow_student_micro, allow_student_full, "
                    "allowed_content_modes, allow_web_search, micro_scene_limit, "
                    "daily_student_units, monthly_student_units, updated_by) VALUES "
                    "('course-micro', :tenant_id, true, true, 'open_creation', false, "
                    "2, 20, 100, :teacher_id), "
                    "('course-full', :tenant_id, true, true, 'open_creation', false, "
                    "2, 100, 200, :teacher_id), "
                    "('course-blocked', :tenant_id, true, false, 'open_creation', false, "
                    "2, 20, 100, :teacher_id), "
                    "('course-approval', :tenant_id, true, true, 'open_creation', false, "
                    "2, 0, 0, :teacher_id)"
                ),
                {"tenant_id": tenant_id, "teacher_id": teacher_id},
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".student_safety_assessments '
                    "(id, tenant_id, course_id, class_id, mode, content_mode, "
                    "web_search_requested, generally_safe, minor_safe, restricted_topic, "
                    "reviewed_by, reviewed_at, assessment_version, expires_at) VALUES "
                    "('safety-micro', :tenant_id, 'course-micro', 'class-micro', "
                    "'micro', 'open_creation', false, true, true, false, 'teacher-e2e', "
                    "clock_timestamp(), 1, clock_timestamp() + interval '1 hour'), "
                    "('safety-full', :tenant_id, 'course-full', 'class-full', "
                    "'full', 'open_creation', false, true, true, false, :teacher_id, "
                    "clock_timestamp(), 1, clock_timestamp() + interval '1 hour'), "
                    "('safety-blocked', :tenant_id, 'course-blocked', 'class-blocked', "
                    "'full', 'open_creation', false, true, true, false, :teacher_id, "
                    "clock_timestamp(), 1, clock_timestamp() + interval '1 hour'), "
                    "('safety-approval', :tenant_id, 'course-approval', 'class-approval', "
                    "'micro', 'open_creation', false, true, true, false, :teacher_id, "
                    "clock_timestamp(), 1, clock_timestamp() + interval '1 hour')"
                ),
                {"tenant_id": tenant_id, "teacher_id": teacher_id},
            )
        await job_repository.grant_quota(
            tenant_id,
            grant_id=f"student-grant-{suffix}",
            units=100,
        )

        with TestClient(application) as client:
            created = client.post(
                "/api/v1/student-classrooms",
                json={
                    "courseId": "course-micro",
                    "classId": "class-micro",
                    "mode": "micro",
                    "contentMode": "open_creation",
                    "webSearchRequested": False,
                },
            )
            assert created.status_code == 202, created.text
            body = created.json()
            assert body["ownerId"] == learner_id
            assert body["status"] == "quota_reserved"
            assert body["approvalId"] is None
            assert body["generationJobId"] is not None

            version_id = await _finish_micro_job(
                engine=engine,
                tenant_id=tenant_id,
                asset_id=body["assetId"],
                selection=selection,
                repository=job_repository,
            )
            result = client.get(f'/api/v1/student-classrooms/{body["assetId"]}')
            assert result.status_code == 200, result.text
            assert result.json()["status"] == "succeeded"
            assert result.json()["classroomVersionId"] == version_id

        async with engine.connect() as connection:
            job_rows = (
                await connection.execute(
                    text(
                        f'SELECT owner_id, visibility, phase, status, result_ref FROM '
                        f'"{schema_name}".generation_jobs WHERE id = :job_id'
                    ),
                    {"job_id": body["generationJobId"]},
                )
            ).all()
            version_count = await connection.scalar(
                text(
                    f'SELECT count(*) FROM "{schema_name}".classroom_versions '
                    "WHERE classroom_id = :asset_id"
                ),
                {"asset_id": body["assetId"]},
            )
        assert job_rows == [(learner_id, "private", "content", "succeeded", version_id)]
        assert version_count == 1

        with TestClient(application) as client:
            selected_context["value"] = other_learner
            hidden = client.get(f'/api/v1/student-classrooms/{body["assetId"]}')
            assert hidden.status_code == 404

            selected_context["value"] = learner
            full_created = client.post(
                "/api/v1/student-classrooms",
                json={
                    "courseId": "course-full",
                    "classId": "class-full",
                    "mode": "full",
                    "contentMode": "open_creation",
                    "webSearchRequested": False,
                },
            )
            assert full_created.status_code == 202, full_created.text
            full = full_created.json()
            assert full["status"] == "quota_reserved"
            full_job_id = await _complete_outline_job(
                engine=engine,
                tenant_id=tenant_id,
                selection=selection,
                repository=job_repository,
            )
            assert full_job_id == full["generationJobId"]

            outline_ready = client.get(
                f'/api/v1/student-classrooms/{full["assetId"]}'
            )
            assert outline_ready.status_code == 200, outline_ready.text
            outline_body = outline_ready.json()
            assert outline_body["status"] == "awaiting_confirmation"
            edited_outline = dict(outline_body["outline"])
            edited_outline["title"] = "Student-reviewed full classroom"
            edited = client.put(
                f'/api/v1/student-classrooms/{full["assetId"]}/outline',
                headers={"If-Match": f'"revision-{outline_body["revision"]}"'},
                json={"outline": edited_outline},
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["outline"]["title"] == (
                "Student-reviewed full classroom"
            )
            confirmed = client.post(
                f'/api/v1/student-classrooms/{full["assetId"]}/confirm-outline'
            )
            assert confirmed.status_code == 202, confirmed.text
            assert confirmed.json()["status"] == "queued"
            full_version_id = await _finish_micro_job(
                engine=engine,
                tenant_id=tenant_id,
                asset_id=full["assetId"],
                selection=selection,
                repository=job_repository,
            )
            full_result = client.get(
                f'/api/v1/student-classrooms/{full["assetId"]}'
            )
            assert full_result.status_code == 200, full_result.text
            assert full_result.json()["status"] == "succeeded"
            assert full_result.json()["classroomVersionId"] == full_version_id

            disabled = client.post(
                "/api/v1/student-classrooms",
                json={
                    "courseId": "course-blocked",
                    "classId": "class-blocked",
                    "mode": "full",
                    "contentMode": "open_creation",
                    "webSearchRequested": False,
                },
            )
            assert disabled.status_code == 403, disabled.text

            approval_required = client.post(
                "/api/v1/student-classrooms",
                json={
                    "courseId": "course-approval",
                    "classId": "class-approval",
                    "mode": "micro",
                    "contentMode": "open_creation",
                    "webSearchRequested": False,
                },
            )
            assert approval_required.status_code == 202, approval_required.text
            approval = approval_required.json()
            assert approval["status"] == "awaiting_approval"
            assert approval["approvalId"] is not None
            assert approval["generationJobId"] is None

            async with engine.connect() as connection:
                approval_job_count = await connection.scalar(
                    text(
                        f'SELECT count(*) FROM "{schema_name}".generation_jobs '
                        "WHERE resource_course_id = 'course-approval'"
                    )
                )
                blocked_job_count = await connection.scalar(
                    text(
                        f'SELECT count(*) FROM "{schema_name}".generation_jobs '
                        "WHERE resource_course_id = 'course-blocked'"
                    )
                )
            assert approval_job_count == 0
            assert blocked_job_count == 0

            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f'UPDATE "{schema_name}".course_generation_policies SET '
                        "micro_scene_limit = 1, updated_by = :teacher_id "
                        "WHERE course_id = 'course-approval'"
                    ),
                    {"teacher_id": teacher_id},
                )
            selected_context["value"] = teacher
            approved = client.post(
                "/api/v1/student-generation-approvals/"
                f'{approval["approvalId"]}/approve',
                json={},
            )
            assert approved.status_code == 202, approved.text
            approved_body = approved.json()
            assert approved_body["status"] == "approved"
            assert approved_body["generationJobId"] is not None

        dispatched_approval = await OutboxDispatcher(engine).dispatch_next()
        assert dispatched_approval is not None
        assert dispatched_approval.job_id == approved_body["generationJobId"]

        async with engine.connect() as connection:
            approval_state = (
                await connection.execute(
                    text(
                        f'SELECT request.scene_max, request.estimated_units, '
                        f'request.quota_state, request.evaluated_checks, job.owner_id, '
                        f'job.visibility, job.quota_units, job.status FROM '
                        f'"{schema_name}".student_generation_requests AS request '
                        f'JOIN "{schema_name}".generation_jobs AS job '
                        "ON job.id = :job_id WHERE request.id = :request_id"
                    ),
                    {
                        "job_id": approved_body["generationJobId"],
                        "request_id": approval["requestId"],
                    },
                )
            ).one()
        assert tuple(approval_state) == (
            1,
            1,
            "reserved",
            (
                "enrollment,permission,course_mode,tenant_policy,source_permission,"
                "safety,quota,approval,accepted"
            ),
            learner_id,
            "private",
            1,
            "queued",
        )
    finally:
        application.dependency_overrides.clear()
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM platform.generation_slots WHERE worker_pool_ref = :worker_pool"),
                {"worker_pool": worker_pool},
            )
            await connection.execute(DropSchema(schema_name, cascade=True))
            await connection.execute(
                text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
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
        await engine.dispose()
