from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.multi_user.models import ADMIN_KNOWLEDGE_OWNER_ID
from deeptutor.teaching.artifacts import StoredArtifact
from deeptutor.teaching.models.classrooms import (
    SourceSnapshot,
    SourceUpload,
    TenantSourceBinding,
)
from deeptutor.teaching.models.platform import (
    Tenant,
    TenantKnowledgeEntitlement,
    TenantMembership,
)
from deeptutor.teaching.repositories import sources as source_repository_module
from deeptutor.teaching.repositories.catalog import (
    CatalogNotFoundError,
    SqlAlchemyCatalogRepository,
)
from deeptutor.teaching.repositories.sources import (
    NewKnowledgeSnapshot,
    NewPdfSnapshot,
    NewUploadReceipt,
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
    knowledge_resource_id = f"admin:kb:{uuid.uuid4()}"
    generation_database.migrate_tenant(tenant_a)
    generation_database.migrate_tenant(tenant_b)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    try:
        await _seed_memberships(
            engine,
            (tenant_a, "student-a", "active"),
            (tenant_b, "student-b", "active"),
        )
        async with engine.begin() as connection:
            await connection.execute(
                insert(TenantKnowledgeEntitlement),
                {
                    "tenant_id": tenant_a,
                    "knowledge_resource_id": knowledge_resource_id,
                    "resource_owner_id": ADMIN_KNOWLEDGE_OWNER_ID,
                    "status": "active",
                    "granted_by": "admin-a",
                },
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
            resource_id=knowledge_resource_id,
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
        assert knowledge_record.source_id == knowledge_resource_id
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
            await source_a.reserve_upload(
                NewUploadReceipt(
                    upload_id=f"foreign-{suffix}",
                    object_key=(f"tenants/{tenant_b}/sources/foreign-{suffix}/source.pdf"),
                    sha256="e" * 64,
                    size_bytes=1,
                    uploaded_by="teacher-a",
                    ownership_token=uuid.uuid4().hex,
                )
            )
        upload_id = f"upload-{suffix}"
        ownership_token = uuid.uuid4().hex
        receipt = NewUploadReceipt(
            upload_id=upload_id,
            object_key=f"tenants/{tenant_a}/sources/{upload_id}/source.pdf",
            sha256=digest,
            size_bytes=123,
            uploaded_by="teacher-a",
            ownership_token=ownership_token,
        )
        reserved = await asyncio.gather(
            source_a.reserve_upload(receipt),
            source_a.reserve_upload(receipt),
        )
        assert {item.upload_id for item in reserved} == {upload_id}
        assert {item.status for item in reserved} == {"writing"}

        artifact = StoredArtifact(
            key=receipt.object_key,
            sha256=digest,
            size=123,
            content_type="application/pdf",
            ownership_token=ownership_token,
            revision="revision-1",
        )
        completed = await source_a.complete_upload(upload_id, artifact)
        snapshot_id = f"pdf-{suffix}"
        snapshot = NewPdfSnapshot(
            snapshot_id=snapshot_id,
            upload_id=upload_id,
            display_name="one.pdf",
            permission_sha256="d" * 64,
        )
        results = await asyncio.gather(
            source_a.bind_uploaded_pdf(
                completed,
                snapshot,
                binding_id=source_binding_id(
                    tenant_a,
                    snapshot_id,
                    "course-a",
                    "class-a",
                ),
                course_id="course-a",
                class_id="class-a",
                actor_id="teacher-a",
            ),
            source_a.bind_uploaded_pdf(
                completed,
                snapshot,
                binding_id=source_binding_id(
                    tenant_a,
                    snapshot_id,
                    "course-a",
                    "class-a",
                ),
                course_id="course-a",
                class_id="class-a",
                actor_id="teacher-a",
            ),
        )

        renamed_snapshot = NewPdfSnapshot(
            snapshot_id=f"pdf-renamed-{suffix}",
            upload_id=upload_id,
            display_name="two.pdf",
            permission_sha256=snapshot.permission_sha256,
        )
        renamed = await source_a.bind_uploaded_pdf(
            completed,
            renamed_snapshot,
            binding_id=source_binding_id(
                tenant_a,
                renamed_snapshot.snapshot_id,
                "course-a",
                "class-a",
            ),
            course_id="course-a",
            class_id="class-a",
            actor_id="teacher-a",
        )

        assert results[0].binding_id == results[1].binding_id
        assert renamed.binding_id != results[0].binding_id
        bindings = await source_a.list_bindings(None, None)
        assert len(bindings) == 3
        assert {record.filename for record in bindings if record.source_type == "pdf"} == {
            "one.pdf",
            "two.pdf",
        }
        async with engine.connect() as connection:
            upload_count = await connection.scalar(
                text(f'SELECT count(*) FROM "{tenant_schema_name(tenant_a)}".source_uploads')
            )
        assert upload_count == 1
        assert await source_b.list_bindings(None, None) == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_upload_receipt_state_rejects_illegal_terminals(
    generation_database,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"receipt-state-{suffix}"
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    tenant_engine = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )

    def row(index: int, **overrides) -> dict[str, object]:
        values: dict[str, object] = {
            "id": f"upload-{index}",
            "tenant_id": tenant_id,
            "uploaded_by": "teacher-a",
            "object_key": (
                f"tenants/{tenant_id}/sources/upload-{index}/source.pdf"
            ),
            "sha256": format(index, "x") * 64,
            "size_bytes": 1,
            "status": "writing",
            "ownership_token": format(index, "x") * 32,
            "object_revision": None,
            "object_version_id": None,
            "last_error_code": None,
        }
        values.update(overrides)
        return values

    invalid_rows = (
        row(1, status="uploaded"),
        row(2, status="failed"),
        row(3, status="writing", last_error_code="write_failed"),
        row(
            4,
            status="uploaded",
            object_revision="revision-4",
            last_error_code="unexpected_error",
        ),
        row(5, status="cleanup_pending"),
        row(6, status="unknown"),
    )
    valid_rows = (
        row(10),
        row(11, status="uploaded", object_revision="revision-11"),
        row(12, status="failed", last_error_code="write_failed"),
        row(13, status="cleanup_pending", last_error_code="cleanup_requested"),
    )
    try:
        for invalid in invalid_rows:
            with pytest.raises(IntegrityError):
                async with tenant_engine.begin() as connection:
                    await connection.execute(insert(SourceUpload), invalid)
        async with tenant_engine.begin() as connection:
            await connection.execute(insert(SourceUpload), valid_rows)
        async with tenant_engine.connect() as connection:
            statuses = tuple(
                await connection.scalars(
                    text(
                        f'SELECT status FROM "{tenant_schema_name(tenant_id)}".'
                        "source_uploads ORDER BY id"
                    )
                )
            )
        assert statuses == ("writing", "uploaded", "failed", "cleanup_pending")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_upload_sha256_is_unique_only_within_each_tenant(
    generation_database,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    schema_owner = f"receipt-dedupe-{suffix}"
    schema_name = tenant_schema_name(schema_owner)
    generation_database.migrate_tenant(schema_owner)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)

    def row(upload_id: str, tenant_id: str, token: str) -> dict[str, object]:
        return {
            "id": upload_id,
            "tenant_id": tenant_id,
            "uploaded_by": "teacher-a",
            "object_key": f"tenants/{tenant_id}/sources/{upload_id}/source.pdf",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "status": "writing",
            "ownership_token": token * 32,
            "object_revision": None,
            "object_version_id": None,
            "last_error_code": None,
        }

    tenant_engine = engine.execution_options(
        schema_translate_map={"tenant": schema_name}
    )
    try:
        async with tenant_engine.begin() as connection:
            await connection.execute(
                insert(SourceUpload),
                row("upload-a", "tenant-a", "a"),
            )
        with pytest.raises(IntegrityError):
            async with tenant_engine.begin() as connection:
                await connection.execute(
                    insert(SourceUpload),
                    row("upload-b", "tenant-a", "b"),
                )
        async with tenant_engine.begin() as connection:
            await connection.execute(
                insert(SourceUpload),
                row("upload-c", "tenant-b", "c"),
            )
        async with tenant_engine.connect() as connection:
            identities = tuple(
                (
                    await connection.execute(
                        text(
                            f'SELECT tenant_id, sha256 FROM "{schema_name}".'
                            "source_uploads ORDER BY tenant_id"
                        )
                    )
                ).all()
            )
        assert identities == (("tenant-a", "a" * 64), ("tenant-b", "a" * 64))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_scope_composite_constraints_reject_cross_resource_tuples(
    generation_database,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"source-scope-{suffix}"
    schema_name = tenant_schema_name(tenant_id)
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)

    async def execute(statement: str, parameters: dict[str, object]) -> None:
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters)

    try:
        await execute(
            f"""
            INSERT INTO "{schema_name}".courses (id, title)
            VALUES ('course-a', 'Course A'), ('course-b', 'Course B')
            """,
            {},
        )
        await execute(
            f"""
            INSERT INTO "{schema_name}".classes (id, course_id, name)
            VALUES ('class-a', 'course-a', 'Class A')
            """,
            {},
        )
        await execute(
            f"""
            INSERT INTO "{schema_name}".source_uploads (
                id, tenant_id, uploaded_by, object_key, sha256, size_bytes,
                status, ownership_token, object_revision
            ) VALUES (
                'upload-a', :tenant_id, 'teacher-a', :object_key, :sha256, 1,
                'uploaded', :ownership_token, 'revision-a'
            )
            """,
            {
                "tenant_id": tenant_id,
                "object_key": f"tenants/{tenant_id}/sources/upload-a/source.pdf",
                "sha256": "a" * 64,
                "ownership_token": "a" * 32,
            },
        )
        await execute(
            f"""
            INSERT INTO "{schema_name}".source_snapshots (
                id, tenant_id, source_type, source_id, resource_owner_id,
                source_upload_id, display_name, source_revision,
                content_sha256, permission_sha256, citation_manifest, created_by
            ) VALUES (
                'snapshot-a', :tenant_id, 'pdf', 'upload-a', 'tenant-workspace',
                'upload-a', 'book.pdf', :sha256, :sha256, :permission_sha256,
                '[]', 'teacher-a'
            )
            """,
            {
                "tenant_id": tenant_id,
                "sha256": "a" * 64,
                "permission_sha256": "b" * 64,
            },
        )

        invalid_statements = (
            f"""
            INSERT INTO "{schema_name}".source_snapshots (
                id, tenant_id, source_type, source_id, resource_owner_id,
                source_upload_id, display_name, source_revision,
                content_sha256, permission_sha256, citation_manifest, created_by
            ) VALUES (
                'snapshot-cross-tenant', 'other-tenant', 'pdf', 'upload-a',
                'tenant-workspace', 'upload-a', 'book.pdf', :sha256, :sha256,
                :permission_sha256, '[]', 'teacher-a'
            )
            """,
            f"""
            INSERT INTO "{schema_name}".tenant_source_bindings (
                id, tenant_id, source_snapshot_id, course_id, class_id, bound_by
            ) VALUES (
                'binding-cross-tenant', 'other-tenant', 'snapshot-a',
                'course-a', NULL, 'teacher-a'
            )
            """,
            f"""
            INSERT INTO "{schema_name}".tenant_source_bindings (
                id, tenant_id, source_snapshot_id, course_id, class_id, bound_by
            ) VALUES (
                'binding-cross-course', :tenant_id, 'snapshot-a',
                'course-b', 'class-a', 'teacher-a'
            )
            """,
            f"""
            INSERT INTO "{schema_name}".teaching_briefs (
                id, tenant_id, source_snapshot_id, course_id, class_id,
                brief_version, document, document_sha256, created_by
            ) VALUES (
                'brief-cross-tenant', 'other-tenant', 'snapshot-a',
                'course-a', NULL, 1, '{{}}', :sha256, 'teacher-a'
            )
            """,
            f"""
            INSERT INTO "{schema_name}".teaching_briefs (
                id, tenant_id, source_snapshot_id, course_id, class_id,
                brief_version, document, document_sha256, created_by
            ) VALUES (
                'brief-cross-course', :tenant_id, NULL,
                'course-b', 'class-a', 1, '{{}}', :sha256, 'teacher-a'
            )
            """,
        )
        for statement in invalid_statements:
            with pytest.raises(IntegrityError):
                await execute(
                    statement,
                    {
                        "tenant_id": tenant_id,
                        "sha256": "c" * 64,
                        "permission_sha256": "d" * 64,
                    },
                )

        await execute(
            f"""
            INSERT INTO "{schema_name}".tenant_source_bindings (
                id, tenant_id, source_snapshot_id, course_id, class_id, bound_by
            ) VALUES (
                'binding-valid', :tenant_id, 'snapshot-a',
                'course-a', 'class-a', 'teacher-a'
            )
            """,
            {"tenant_id": tenant_id},
        )
        await execute(
            f"""
            INSERT INTO "{schema_name}".teaching_briefs (
                id, tenant_id, source_snapshot_id, course_id, class_id,
                brief_version, document, document_sha256, created_by
            ) VALUES (
                'brief-valid', :tenant_id, 'snapshot-a',
                'course-a', 'class-a', 1, '{{}}', :sha256, 'teacher-a'
            )
            """,
            {"tenant_id": tenant_id, "sha256": "e" * 64},
        )
        async with engine.connect() as connection:
            binding_count = await connection.scalar(
                text(f'SELECT count(*) FROM "{schema_name}".tenant_source_bindings')
            )
            brief_count = await connection.scalar(
                text(f'SELECT count(*) FROM "{schema_name}".teaching_briefs')
            )
        assert (int(binding_count or 0), int(brief_count or 0)) == (1, 1)
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
    resource_id = f"admin:kb:{uuid.uuid4()}"
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
                f"admin:kb:{uuid.uuid4()}",
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
    resource_id = f"user:kb:{uuid.uuid4()}"
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


@pytest.mark.asyncio
async def test_revocation_locked_first_prevents_knowledge_binding(
    generation_database,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"entitlement-revoke-first-{suffix}"
    resource_id = f"admin:kb:{uuid.uuid4()}"
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    denied_error = getattr(
        source_repository_module,
        "SourceEntitlementDeniedError",
        PermissionError,
    )
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
                    "resource_owner_id": ADMIN_KNOWLEDGE_OWNER_ID,
                    "status": "active",
                    "granted_by": "admin-a",
                },
            )
        catalog = SqlAlchemyCatalogRepository(tenant_id, engine)
        await catalog.create_course("course-a", "Course A")
        repository = SqlAlchemySourceRepository(tenant_id, engine)
        snapshot = NewKnowledgeSnapshot(
            snapshot_id=f"kb-source-{suffix}",
            resource_id=resource_id,
            resource_owner_id=ADMIN_KNOWLEDGE_OWNER_ID,
            revision="binding-v1",
            content_sha256="a" * 64,
            permission_sha256="b" * 64,
        )
        binding_id = source_binding_id(
            tenant_id,
            snapshot.snapshot_id,
            "course-a",
            None,
        )

        revoke_connection = await engine.connect()
        revoke_transaction = await revoke_connection.begin()
        await revoke_connection.execute(
            text(
                """
                UPDATE platform.tenant_knowledge_entitlements
                   SET status = 'disabled'
                 WHERE tenant_id = :tenant_id
                   AND knowledge_resource_id = :resource_id
                   AND resource_owner_id = :resource_owner_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "resource_id": resource_id,
                "resource_owner_id": ADMIN_KNOWLEDGE_OWNER_ID,
            },
        )
        bind_task = asyncio.create_task(
            repository.bind_knowledge_resource(
                snapshot,
                binding_id=binding_id,
                course_id="course-a",
                class_id=None,
                actor_id="teacher-a",
            )
        )
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(bind_task), timeout=0.5)
        finally:
            await revoke_transaction.commit()
            await revoke_connection.close()

        with pytest.raises(denied_error):
            await bind_task
        async with repository._session_factory() as session:
            assert await session.get(SourceSnapshot, snapshot.snapshot_id) is None
            assert await session.get(TenantSourceBinding, binding_id) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_lock_linearizes_before_concurrent_revocation(
    generation_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"entitlement-bind-first-{suffix}"
    resource_id = f"admin:kb:{uuid.uuid4()}"
    generation_database.migrate_tenant(tenant_id)
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
                    "resource_owner_id": ADMIN_KNOWLEDGE_OWNER_ID,
                    "status": "active",
                    "granted_by": "admin-a",
                },
            )
        catalog = SqlAlchemyCatalogRepository(tenant_id, engine)
        await catalog.create_course("course-a", "Course A")
        repository = SqlAlchemySourceRepository(tenant_id, engine)
        snapshot = NewKnowledgeSnapshot(
            snapshot_id=f"kb-source-{suffix}",
            resource_id=resource_id,
            resource_owner_id=ADMIN_KNOWLEDGE_OWNER_ID,
            revision="binding-v1",
            content_sha256="c" * 64,
            permission_sha256="d" * 64,
        )
        binding_id = source_binding_id(
            tenant_id,
            snapshot.snapshot_id,
            "course-a",
            None,
        )
        entered_binding = asyncio.Event()
        release_binding = asyncio.Event()
        original_ensure_binding = repository._ensure_binding

        async def paused_ensure_binding(*args, **kwargs):
            entered_binding.set()
            await release_binding.wait()
            return await original_ensure_binding(*args, **kwargs)

        monkeypatch.setattr(repository, "_ensure_binding", paused_ensure_binding)
        bind_task = asyncio.create_task(
            repository.bind_knowledge_resource(
                snapshot,
                binding_id=binding_id,
                course_id="course-a",
                class_id=None,
                actor_id="teacher-a",
            )
        )
        await asyncio.wait_for(entered_binding.wait(), timeout=5)

        async def revoke() -> None:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE platform.tenant_knowledge_entitlements
                           SET status = 'disabled'
                         WHERE tenant_id = :tenant_id
                           AND knowledge_resource_id = :resource_id
                           AND resource_owner_id = :resource_owner_id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "resource_id": resource_id,
                        "resource_owner_id": ADMIN_KNOWLEDGE_OWNER_ID,
                    },
                )

        revoke_task = asyncio.create_task(revoke())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(revoke_task), timeout=0.5)
        finally:
            release_binding.set()

        record = await bind_task
        await revoke_task
        assert record.binding_id == binding_id
        assert not await repository.is_knowledge_resource_entitled(
            resource_id,
            ADMIN_KNOWLEDGE_OWNER_ID,
        )
    finally:
        await engine.dispose()
