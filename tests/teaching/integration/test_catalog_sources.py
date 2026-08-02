from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.multi_user.models import ADMIN_KNOWLEDGE_OWNER_ID
from deeptutor.teaching.models.platform import (
    Tenant,
    TenantKnowledgeEntitlement,
    TenantMembership,
)
from deeptutor.teaching.repositories.catalog import (
    CatalogNotFoundError,
    SqlAlchemyCatalogRepository,
)
from deeptutor.teaching.repositories.sources import (
    NewKnowledgeSnapshot,
    NewUpload,
    SourceConflictError,
    SourceNotFoundError,
    SqlAlchemySourceRepository,
    source_binding_id,
)
from deeptutor.teaching.schema_names import tenant_schema_name


async def _seed_memberships(engine, *rows: tuple[str, str, str]) -> None:
    tenant_ids = sorted({tenant_id for tenant_id, _, _ in rows})
    async with engine.begin() as connection:
        await connection.execute(
            insert(Tenant),
            [{"id": tenant_id, "name": tenant_id, "status": "active"} for tenant_id in tenant_ids],
        )
        await connection.execute(
            insert(TenantMembership),
            [
                {"tenant_id": tenant_id, "user_id": user_id, "status": status}
                for tenant_id, user_id, status in rows
            ],
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
        await _seed_memberships(
            engine,
            (tenant_a, "student-a", "active"),
            (tenant_b, "student-b", "active"),
        )
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
            resource_owner_id=ADMIN_KNOWLEDGE_OWNER_ID,
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
        async with engine.connect() as connection:
            owner_and_audit = (
                await connection.execute(
                    text(
                        f"""
                        SELECT snapshot.resource_owner_id,
                               snapshot.created_by,
                               binding.bound_by
                        FROM "{tenant_schema_name(tenant_a)}".source_snapshots snapshot
                        JOIN "{tenant_schema_name(tenant_a)}".tenant_source_bindings binding
                          ON binding.source_snapshot_id = snapshot.id
                        WHERE snapshot.id = :snapshot_id
                        """
                    ),
                    {"snapshot_id": knowledge_snapshot.snapshot_id},
                )
            ).one()
        assert tuple(owner_and_audit) == (
            ADMIN_KNOWLEDGE_OWNER_ID,
            "teacher-a",
            "teacher-a",
        )
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
                    object_key=(f"tenants/{tenant_b}/sources/foreign-{suffix}/source.pdf"),
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
            object_key=f"tenants/{tenant_a}/sources/upload-one-{suffix}/source.pdf",
            sha256=digest,
            size_bytes=123,
        )
        upload_two = NewUpload(
            upload_id=f"upload-two-{suffix}",
            snapshot_id=f"pdf-two-{suffix}",
            filename="two.pdf",
            object_key=f"tenants/{tenant_a}/sources/upload-two-{suffix}/source.pdf",
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


@pytest.mark.asyncio
async def test_enrollment_requires_active_membership_in_repository_tenant(
    generation_database,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_a = f"member-a-{suffix}"
    tenant_b = f"member-b-{suffix}"
    generation_database.migrate_tenant(tenant_a)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        await _seed_memberships(
            engine,
            (tenant_a, "student-active", "active"),
            (tenant_a, "student-inactive", "inactive"),
            (tenant_b, "student-other-tenant", "active"),
        )
        catalog = SqlAlchemyCatalogRepository(tenant_a, engine)
        await catalog.create_course("course-a", "Course A")
        await catalog.create_class("course-a", "class-a", "Class A")

        active = await catalog.add_enrollment("class-a", "student-active")

        assert active.learner_id == "student-active"
        for learner_id in (
            "student-missing",
            "student-inactive",
            "student-other-tenant",
        ):
            with pytest.raises(CatalogNotFoundError, match="active tenant member"):
                await catalog.add_enrollment("class-a", learner_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_entitlement_is_active_and_tenant_scoped(generation_database) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_a = f"entitled-a-{suffix}"
    tenant_b = f"entitled-b-{suffix}"
    resource_id = "admin:kb:math"
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                insert(Tenant),
                [
                    {"id": tenant_a, "name": tenant_a, "status": "active"},
                    {"id": tenant_b, "name": tenant_b, "status": "active"},
                ],
            )
            await connection.execute(
                insert(TenantKnowledgeEntitlement),
                [
                    {
                        "tenant_id": tenant_a,
                        "knowledge_resource_id": resource_id,
                        "resource_owner_id": ADMIN_KNOWLEDGE_OWNER_ID,
                        "status": "active",
                        "granted_by": "admin-a",
                    },
                    {
                        "tenant_id": tenant_b,
                        "knowledge_resource_id": resource_id,
                        "resource_owner_id": ADMIN_KNOWLEDGE_OWNER_ID,
                        "status": "disabled",
                        "granted_by": "admin-b",
                    },
                ],
            )

        source_a = SqlAlchemySourceRepository(tenant_a, engine)
        source_b = SqlAlchemySourceRepository(tenant_b, engine)

        assert (
            await source_a.is_knowledge_resource_entitled(
                resource_id,
                ADMIN_KNOWLEDGE_OWNER_ID,
            )
            is True
        )
        assert (
            await source_b.is_knowledge_resource_entitled(
                resource_id,
                ADMIN_KNOWLEDGE_OWNER_ID,
            )
            is False
        )
        assert (
            await source_b.is_knowledge_resource_entitled(
                "admin:kb:other",
                ADMIN_KNOWLEDGE_OWNER_ID,
            )
            is False
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_knowledge_entitlement_is_owner_scoped(generation_database) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"personal-entitlement-{suffix}"
    resource_id = "user:kb:course-a"
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                insert(Tenant),
                {"id": tenant_id, "name": tenant_id, "status": "active"},
            )
            await connection.execute(
                insert(TenantKnowledgeEntitlement),
                {
                    "tenant_id": tenant_id,
                    "knowledge_resource_id": resource_id,
                    "resource_owner_id": "alice",
                    "status": "active",
                    "granted_by": "admin-a",
                },
            )

        source = SqlAlchemySourceRepository(tenant_id, engine)

        assert await source.is_knowledge_resource_entitled(resource_id, "alice") is True
        assert await source.is_knowledge_resource_entitled(resource_id, "bob") is False
    finally:
        await engine.dispose()
