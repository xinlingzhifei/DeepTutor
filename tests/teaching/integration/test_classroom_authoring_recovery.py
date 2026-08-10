from __future__ import annotations

from dataclasses import replace
import hashlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.teaching.brief_builder import (
    KnowledgePointSpec,
    TeachingBriefBuilder,
    TeachingBriefSpec,
)
from deeptutor.teaching.contracts import canonical_json_bytes
from deeptutor.teaching.models.classrooms import ClassroomAsset
from deeptutor.teaching.models.platform import Tenant
from deeptutor.teaching.models.tenant import Course, TeachingClass
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.repositories.classrooms import SqlAlchemyClassroomRepository
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.classrooms import (
    ClassroomIdempotencyConflict,
    NewClassroomWorkflow,
    NewDraftMedia,
)
from deeptutor.teaching.tenant_context import TenantContext


@pytest.mark.asyncio
async def test_postgres_authoring_recovery_is_durable_and_atomically_bound(
    generation_database,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"authoring-{suffix}"
    schema_name = tenant_schema_name(tenant_id)
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    translated = engine.execution_options(
        schema_translate_map={"tenant": schema_name}
    )
    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    context = TenantContext(
        tenant_id=tenant_id,
        schema_name=schema_name,
        user_id="teacher-1",
        permissions=permissions_for_roles(
            {"teacher"},
            scope_type="class",
            scope_id="class-1",
            tenant_id=tenant_id,
        ),
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    Tenant(
                        id=tenant_id,
                        name="Authoring recovery tenant",
                        status="active",
                        data_plane_mode="shared",
                    )
                )
                session.add(Course(id="course-1", title="Physics"))
                session.add(
                    TeachingClass(
                        id="class-1",
                        course_id="course-1",
                        name="Class 1",
                    )
                )

        brief = TeachingBriefBuilder(context, object()).open_creation(
            TeachingBriefSpec(
                course_id="course-1",
                class_id="class-1",
                objective="Explain motion",
                grade_band="grade-8",
                audience="intermediate",
                duration_minutes=45,
                classroom_mode="full",
                web_policy="disabled",
                template_id="template-1",
                template_version="1",
                knowledge_points=(
                    KnowledgePointSpec(
                        knowledge_point_id="kp-motion",
                        title="Motion",
                        description="Describe velocity and displacement.",
                    ),
                ),
                content_mode="open_creation",
                open_creation_acknowledged=True,
            )
        ).contract
        repository = SqlAlchemyClassroomRepository(engine, tenant_id)
        workflow = NewClassroomWorkflow(
            tenant_id=tenant_id,
            asset_id=f"asset-{suffix}",
            draft_id=f"draft-{suffix}",
            owner_id=context.user_id,
            title="Motion",
            teaching_brief=brief,
            creation_idempotency_key=f"authoring-key-{suffix}",
            creation_request_sha256="1" * 64,
        )

        created = await repository.create_workflow(workflow)
        replayed = await repository.create_workflow(workflow)

        assert replayed.asset_id == created.asset_id
        assert replayed.draft_id == created.draft_id
        assert replayed.creation_idempotency_key == workflow.creation_idempotency_key
        with pytest.raises(ClassroomIdempotencyConflict):
            await repository.create_workflow(
                replace(workflow, creation_request_sha256="2" * 64)
            )

        second = await repository.create_workflow(
            NewClassroomWorkflow(
                tenant_id=tenant_id,
                asset_id=f"asset-second-{suffix}",
                draft_id=f"draft-second-{suffix}",
                owner_id=context.user_id,
                title="Motion",
                teaching_brief=brief,
                creation_idempotency_key=f"authoring-key-second-{suffix}",
                creation_request_sha256="1" * 64,
            )
        )
        assert second.asset_id != created.asset_id

        async with session_factory() as session:
            async with session.begin():
                asset = await session.get(ClassroomAsset, created.asset_id)
                assert asset is not None
                asset.lifecycle_state = "editing"

        original_document_sha256 = hashlib.sha256(
            canonical_json_bytes(created.document)
        ).hexdigest()
        document = {"dslVersion": "0.1.0", "scenes": []}
        document_sha256 = hashlib.sha256(canonical_json_bytes(document)).hexdigest()
        updated = await repository.update_document(
            created.asset_id,
            document,
            document_sha256,
            created.revision,
        )
        assert updated is not None
        stale_report = {
            "draftRevision": created.revision,
            "documentSha256": original_document_sha256,
            "sections": [],
        }
        assert (
            await repository.save_validation_report(
                created.asset_id,
                stale_report,
                hashlib.sha256(canonical_json_bytes(stale_report)).hexdigest(),
                created.revision,
                original_document_sha256,
            )
            is None
        )
        current_report = {
            "draftRevision": updated.revision,
            "documentSha256": document_sha256,
            "sections": [],
        }
        validated = await repository.save_validation_report(
            created.asset_id,
            current_report,
            hashlib.sha256(canonical_json_bytes(current_report)).hexdigest(),
            updated.revision,
            document_sha256,
        )
        assert validated is not None
        assert validated.validation_revision == updated.revision
        assert validated.validation_document_sha256 == document_sha256

        media_id = f"media-{uuid.uuid4().hex}"
        await repository.reserve_media(
            NewDraftMedia(
                id=media_id,
                classroom_id=created.asset_id,
                uploaded_by=context.user_id,
                object_key=f"tenants/{tenant_id}/temporary/{media_id}.png",
                mime_type="image/png",
                sha256="3" * 64,
                size_bytes=8,
                ownership_token="4" * 32,
            )
        )
        pending = await repository.mark_media_cleanup_pending(
            created.asset_id,
            media_id,
            "upload_failed",
        )
        assert pending.status == "cleanup_pending"
        assert [
            receipt.id
            for receipt in await repository.list_cleanup_pending(
                created.asset_id,
                limit=8,
            )
        ] == [media_id]
        assert await repository.list_cleanup_pending(second.asset_id, limit=8) == ()
        await repository.finish_media_cleanup(
            created.asset_id,
            media_id,
            "upload_failed",
        )
        failed = await repository.get_media_receipt(created.asset_id, media_id)
        assert failed is not None
        assert failed.status == "failed"
        assert await repository.list_cleanup_pending(created.asset_id, limit=8) == ()

        async with engine.connect() as connection:
            revision = await connection.scalar(
                text(f'SELECT version_num FROM "{schema_name}".alembic_version')
            )
            columns = set(
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema_name "
                        "AND table_name = 'classroom_drafts'"
                    ),
                    {"schema_name": schema_name},
                )
            )
        assert revision == "20260810_0016"
        assert {
            "creation_idempotency_key",
            "creation_request_sha256",
            "validation_revision",
            "validation_document_sha256",
        }.issubset(columns)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM platform.audit_log WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(DropSchema(schema_name, cascade=True))
            await connection.execute(
                text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()
