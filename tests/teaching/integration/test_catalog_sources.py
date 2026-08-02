from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.repositories.catalog import SqlAlchemyCatalogRepository
from deeptutor.teaching.repositories.sources import (
    NewKnowledgeSnapshot,
    NewUpload,
    SourceConflictError,
    SourceNotFoundError,
    SqlAlchemySourceRepository,
    source_binding_id,
)


@pytest.mark.asyncio
async def test_catalog_and_sources_are_isolated_and_upload_dedupe_is_atomic(
    generation_database,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_a = f"catalog-a-{suffix}"
    tenant_b = f"catalog-b-{suffix}"
    generation_database.migrate_tenant(tenant_a)
    generation_database.migrate_tenant(tenant_b)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        catalog_a = SqlAlchemyCatalogRepository(tenant_a, engine)
        catalog_b = SqlAlchemyCatalogRepository(tenant_b, engine)
        source_a = SqlAlchemySourceRepository(tenant_a, engine)
        source_b = SqlAlchemySourceRepository(tenant_b, engine)

        await catalog_a.create_course("course-a", "Tenant A course")
        await catalog_a.create_class("course-a", "class-a", "Tenant A class")
        first_enrollment = await catalog_a.add_enrollment("class-a", "student-a")
        replayed_enrollment = await catalog_a.add_enrollment("class-a", "student-a")
        await catalog_b.create_course("course-a", "Tenant B course")

        assert first_enrollment == replayed_enrollment
        assert [item.title for item in await catalog_a.list_courses(None)] == ["Tenant A course"]
        assert [item.title for item in await catalog_b.list_courses(None)] == ["Tenant B course"]

        knowledge_snapshot = NewKnowledgeSnapshot(
            snapshot_id=f"kb-source-{suffix}",
            resource_id="admin:kb:math",
            revision="binding-v1",
            content_sha256="a" * 64,
            permission_sha256="b" * 64,
        )
        knowledge_binding_id = source_binding_id(
            tenant_a,
            knowledge_snapshot.snapshot_id,
            "course-a",
            "class-a",
        )
        knowledge_record = await source_a.bind_knowledge_resource(
            knowledge_snapshot,
            binding_id=knowledge_binding_id,
            course_id="course-a",
            class_id="class-a",
            actor_id="teacher-a",
        )
        assert knowledge_record.source_id == "admin:kb:math"
        assert await source_b.list_bindings(None, None) == ()
        with pytest.raises(SourceNotFoundError):
            await source_b.get_binding(knowledge_binding_id)

        digest = "c" * 64
        with pytest.raises(SourceConflictError, match="outside the tenant"):
            await source_a.create_upload_binding(
                NewUpload(
                    upload_id=f"foreign-{suffix}",
                    snapshot_id=f"foreign-pdf-{suffix}",
                    filename="foreign.pdf",
                    object_key=(f"tenants/{tenant_b}/temporary/foreign-{suffix}/source.pdf"),
                    sha256="e" * 64,
                    size_bytes=1,
                ),
                binding_id=source_binding_id(
                    tenant_a,
                    f"foreign-pdf-{suffix}",
                    "course-a",
                    "class-a",
                ),
                course_id="course-a",
                class_id="class-a",
                actor_id="teacher-a",
                permission_sha256="f" * 64,
            )
        upload_one = NewUpload(
            upload_id=f"upload-one-{suffix}",
            snapshot_id=f"pdf-one-{suffix}",
            filename="one.pdf",
            object_key=f"tenants/{tenant_a}/temporary/upload-one-{suffix}/source.pdf",
            sha256=digest,
            size_bytes=123,
        )
        upload_two = NewUpload(
            upload_id=f"upload-two-{suffix}",
            snapshot_id=f"pdf-two-{suffix}",
            filename="two.pdf",
            object_key=f"tenants/{tenant_a}/temporary/upload-two-{suffix}/source.pdf",
            sha256=digest,
            size_bytes=123,
        )

        results = await asyncio.gather(
            source_a.create_upload_binding(
                upload_one,
                binding_id=source_binding_id(
                    tenant_a,
                    upload_one.snapshot_id,
                    "course-a",
                    "class-a",
                ),
                course_id="course-a",
                class_id="class-a",
                actor_id="teacher-a",
                permission_sha256="d" * 64,
            ),
            source_a.create_upload_binding(
                upload_two,
                binding_id=source_binding_id(
                    tenant_a,
                    upload_two.snapshot_id,
                    "course-a",
                    "class-a",
                ),
                course_id="course-a",
                class_id="class-a",
                actor_id="teacher-a",
                permission_sha256="d" * 64,
            ),
        )

        assert {retained for _, retained in results} == {False, True}
        assert results[0][0].binding_id == results[1][0].binding_id
        assert len(await source_a.list_bindings(None, None)) == 2
        assert await source_b.list_bindings(None, None) == ()
    finally:
        await engine.dispose()
