"""Add immutable knowledge entitlements and durable source-upload receipts."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260803_0009"
down_revision: str | None = "20260802_0008"
branch_labels: str | None = None
depends_on: str | None = None

_ADMIN_KNOWLEDGE_OWNER_ID = "admin-workspace"
_TENANT_SOURCE_OWNER_ID = "tenant-workspace"
_KNOWLEDGE_RESOURCE_PATTERN = (
    "^(admin|user):kb:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    "[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


def _tenant_schema() -> str:
    return context.get_x_argument(as_dictionary=True)["tenant_schema"]


def _sync_tenant_schema_revision(source_revision: str, target_revision: str) -> None:
    tenant_schema = _tenant_schema()
    connection = op.get_bind()
    state = (
        connection.execute(
            sa.text(
                """
                SELECT revision, status
                FROM platform.tenant_schema_states
                WHERE schema_name = :tenant_schema
                FOR UPDATE
                """
            ),
            {"tenant_schema": tenant_schema},
        )
        .mappings()
        .one_or_none()
    )
    if state is not None and (state["status"] != "active" or state["revision"] != source_revision):
        raise RuntimeError("tenant schema state revision does not match migration source")
    result = connection.execute(
        sa.text(
            """
            UPDATE platform.tenant_schema_states
            SET revision = :target_revision,
                verified_at = now(),
                updated_at = now()
            WHERE schema_name = :tenant_schema
              AND status = 'active'
              AND revision = :source_revision
            """
        ),
        {
            "source_revision": source_revision,
            "target_revision": target_revision,
            "tenant_schema": tenant_schema,
        },
    )
    if state is not None and result.rowcount != 1:
        raise RuntimeError("tenant schema state revision update was not applied")


def _upgrade_platform() -> None:
    op.create_table(
        "tenant_knowledge_entitlements",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("knowledge_resource_id", sa.String(length=128), nullable=False),
        sa.Column("resource_owner_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column("granted_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_tenant_knowledge_entitlements_status",
        ),
        sa.CheckConstraint(
            f"knowledge_resource_id ~ '{_KNOWLEDGE_RESOURCE_PATTERN}'",
            name="ck_tenant_knowledge_entitlements_resource_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_knowledge_entitlements_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "knowledge_resource_id",
            "resource_owner_id",
            name="pk_tenant_knowledge_entitlements",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tenant_knowledge_entitlements_resource_owner_status",
        "tenant_knowledge_entitlements",
        ["knowledge_resource_id", "resource_owner_id", "status"],
        schema="platform",
    )


def _lock_source_tables(quoted_schema: str) -> None:
    op.get_bind().execute(
        sa.text(
            "LOCK TABLE "
            f"{quoted_schema}.classes, "
            f"{quoted_schema}.source_snapshots, "
            f"{quoted_schema}.source_uploads, "
            f"{quoted_schema}.teaching_briefs, "
            f"{quoted_schema}.tenant_source_bindings "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )


def _guard_tenant_upgrade(quoted_schema: str) -> None:
    connection = op.get_bind()
    _lock_source_tables(quoted_schema)
    if connection.execute(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.source_uploads)")
    ).scalar():
        raise CommandError(
            "cannot upgrade durable source uploads: legacy object receipts "
            "cannot be ownership-verified"
        )
    if connection.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.source_snapshots "
            "WHERE source_type = 'pdf')"
        )
    ).scalar():
        raise CommandError(
            "cannot upgrade durable source uploads: legacy PDF snapshots "
            "have no ownership-verifiable receipt"
        )
    if connection.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.source_snapshots "
            "WHERE source_type = 'knowledge_base' "
            "AND source_id !~ :resource_pattern)"
        ),
        {"resource_pattern": _KNOWLEDGE_RESOURCE_PATTERN},
    ).scalar():
        raise CommandError(
            "cannot upgrade source snapshots: knowledge resource IDs must use "
            "immutable UUID generations"
        )
    if connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM {quoted_schema}.tenant_source_bindings binding
                JOIN {quoted_schema}.source_snapshots snapshot
                  ON snapshot.id = binding.source_snapshot_id
                WHERE snapshot.tenant_id <> binding.tenant_id
            )
            """
        )
    ).scalar():
        raise CommandError(
            "cannot upgrade source bindings: snapshot and binding tenants differ"
        )
    if connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM {quoted_schema}.tenant_source_bindings binding
                JOIN {quoted_schema}.classes class_record
                  ON class_record.id = binding.class_id
                WHERE binding.class_id IS NOT NULL
                  AND (
                      binding.course_id IS NULL
                      OR class_record.course_id <> binding.course_id
                  )
            )
            """
        )
    ).scalar():
        raise CommandError(
            "cannot upgrade source bindings: class and course scopes differ"
        )
    if connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM {quoted_schema}.teaching_briefs brief
                JOIN {quoted_schema}.source_snapshots snapshot
                  ON snapshot.id = brief.source_snapshot_id
                WHERE brief.source_snapshot_id IS NOT NULL
                  AND snapshot.tenant_id <> brief.tenant_id
            )
            """
        )
    ).scalar():
        raise CommandError(
            "cannot upgrade teaching briefs: snapshot and brief tenants differ"
        )
    if connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM {quoted_schema}.teaching_briefs brief
                JOIN {quoted_schema}.classes class_record
                  ON class_record.id = brief.class_id
                WHERE brief.class_id IS NOT NULL
                  AND (
                      brief.course_id IS NULL
                      OR class_record.course_id <> brief.course_id
                  )
            )
            """
        )
    ).scalar():
        raise CommandError(
            "cannot upgrade teaching briefs: class and course scopes differ"
        )


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    _guard_tenant_upgrade(quoted_schema)

    op.add_column(
        "source_snapshots",
        sa.Column("resource_owner_id", sa.String(length=128), nullable=True),
        schema=tenant_schema,
    )
    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {quoted_schema}.source_snapshots
            SET resource_owner_id = CASE
                WHEN source_type = 'knowledge_base'
                 AND source_id LIKE 'admin:kb:%'
                    THEN :admin_owner_id
                WHEN source_type = 'knowledge_base'
                    THEN created_by
                ELSE :tenant_owner_id
            END
            """
        ),
        {
            "admin_owner_id": _ADMIN_KNOWLEDGE_OWNER_ID,
            "tenant_owner_id": _TENANT_SOURCE_OWNER_ID,
        },
    )
    op.alter_column(
        "source_snapshots",
        "resource_owner_id",
        existing_type=sa.String(length=128),
        nullable=False,
        schema=tenant_schema,
    )
    op.add_column(
        "source_snapshots",
        sa.Column("source_upload_id", sa.String(length=128), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "source_snapshots",
        sa.Column("display_name", sa.String(length=512), nullable=True),
        schema=tenant_schema,
    )

    op.drop_constraint(
        "fk_source_uploads_source_snapshot_id_source_snapshots",
        "source_uploads",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_column("source_uploads", "source_snapshot_id", schema=tenant_schema)
    op.drop_column("source_uploads", "filename", schema=tenant_schema)
    op.add_column(
        "source_uploads",
        sa.Column("ownership_token", sa.String(length=32), nullable=False),
        schema=tenant_schema,
    )
    op.add_column(
        "source_uploads",
        sa.Column("object_revision", sa.String(length=256), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "source_uploads",
        sa.Column("object_version_id", sa.String(length=256), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "source_uploads",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "source_uploads",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema=tenant_schema,
    )
    op.alter_column(
        "source_uploads",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'writing'"),
        existing_nullable=False,
        schema=tenant_schema,
    )

    op.create_unique_constraint(
        "uq_source_uploads_id_tenant",
        "source_uploads",
        ["id", "tenant_id"],
        schema=tenant_schema,
    )
    op.create_unique_constraint(
        "uq_source_uploads_tenant_sha256",
        "source_uploads",
        ["tenant_id", "sha256"],
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_source_uploads_status",
        "source_uploads",
        "status IN ('writing', 'uploaded', 'cleanup_pending', 'failed')",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_source_uploads_sha256",
        "source_uploads",
        "sha256 ~ '^[0-9a-f]{64}$'",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_source_uploads_ownership_token",
        "source_uploads",
        "ownership_token ~ '^[0-9a-f]{32}$'",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_source_uploads_receipt_state",
        "source_uploads",
        "(status = 'writing' AND object_revision IS NULL "
        "AND last_error_code IS NULL) OR "
        "(status = 'uploaded' AND object_revision IS NOT NULL "
        "AND last_error_code IS NULL) OR "
        "(status IN ('cleanup_pending', 'failed') AND last_error_code IS NOT NULL)",
        schema=tenant_schema,
    )

    op.drop_constraint(
        "uq_source_snapshots_tenant_source_revision",
        "source_snapshots",
        type_="unique",
        schema=tenant_schema,
    )
    op.create_unique_constraint(
        "uq_source_snapshots_tenant_source_revision",
        "source_snapshots",
        [
            "tenant_id",
            "source_type",
            "source_id",
            "resource_owner_id",
            "source_revision",
            "permission_sha256",
        ],
        schema=tenant_schema,
    )
    op.create_unique_constraint(
        "uq_source_snapshots_id_tenant",
        "source_snapshots",
        ["id", "tenant_id"],
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_source_snapshots_upload_tenant",
        "source_snapshots",
        "source_uploads",
        ["source_upload_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_source_snapshots_pdf_upload",
        "source_snapshots",
        "(source_type = 'pdf' AND source_upload_id IS NOT NULL "
        "AND display_name IS NOT NULL) OR "
        "(source_type <> 'pdf' AND source_upload_id IS NULL)",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_source_snapshots_knowledge_generation",
        "source_snapshots",
        "source_type <> 'knowledge_base' OR "
        f"source_id ~ '{_KNOWLEDGE_RESOURCE_PATTERN}'",
        schema=tenant_schema,
    )

    op.create_unique_constraint(
        "uq_classes_id_course",
        "classes",
        ["id", "course_id"],
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_tenant_source_bindings_source_snapshot_id_source_snapshots",
        "tenant_source_bindings",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_tenant_source_bindings_class_id_classes",
        "tenant_source_bindings",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_tenant_source_bindings_snapshot_tenant",
        "tenant_source_bindings",
        "source_snapshots",
        ["source_snapshot_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tenant_source_bindings_class_course",
        "tenant_source_bindings",
        "classes",
        ["class_id", "course_id"],
        ["id", "course_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_tenant_source_bindings_class_requires_course",
        "tenant_source_bindings",
        "class_id IS NULL OR course_id IS NOT NULL",
        schema=tenant_schema,
    )

    op.drop_constraint(
        "fk_teaching_briefs_source_snapshot_id_source_snapshots",
        "teaching_briefs",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_teaching_briefs_class_id_classes",
        "teaching_briefs",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_teaching_briefs_snapshot_tenant",
        "teaching_briefs",
        "source_snapshots",
        ["source_snapshot_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_teaching_briefs_class_course",
        "teaching_briefs",
        "classes",
        ["class_id", "course_id"],
        ["id", "course_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_teaching_briefs_class_requires_course",
        "teaching_briefs",
        "class_id IS NULL OR course_id IS NOT NULL",
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260802_0008", "20260803_0009")


def upgrade() -> None:
    if _migration_scope() == "platform":
        _upgrade_platform()
    else:
        _upgrade_tenant()


def _downgrade_platform() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE platform.tenant_knowledge_entitlements IN ACCESS EXCLUSIVE MODE")
    )
    has_rows = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM platform.tenant_knowledge_entitlements)")
    ).scalar()
    if has_rows:
        raise CommandError(
            "cannot downgrade knowledge entitlements: active authorization data exists"
        )
    op.drop_table("tenant_knowledge_entitlements", schema="platform")


def _guard_tenant_downgrade(quoted_schema: str) -> None:
    connection = op.get_bind()
    _lock_source_tables(quoted_schema)
    if connection.execute(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.source_uploads)")
    ).scalar():
        raise CommandError(
            "cannot downgrade durable source uploads: ownership receipts exist"
        )
    owner_mismatch = connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM {quoted_schema}.source_snapshots
                WHERE resource_owner_id <> CASE
                    WHEN source_type = 'knowledge_base'
                     AND source_id LIKE 'admin:kb:%'
                        THEN :admin_owner_id
                    WHEN source_type = 'knowledge_base'
                        THEN created_by
                    ELSE :tenant_owner_id
                END
            )
            """
        ),
        {
            "admin_owner_id": _ADMIN_KNOWLEDGE_OWNER_ID,
            "tenant_owner_id": _TENANT_SOURCE_OWNER_ID,
        },
    ).scalar()
    if owner_mismatch:
        raise CommandError("cannot downgrade source owners: owner evidence is not reconstructible")
    duplicate_legacy_identity = connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM {quoted_schema}.source_snapshots
                GROUP BY tenant_id, source_type, source_id, source_revision
                HAVING count(*) > 1
            )
            """
        )
    ).scalar()
    if duplicate_legacy_identity:
        raise CommandError("cannot downgrade source owners: owner-scoped snapshots would collide")


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    _guard_tenant_downgrade(quoted_schema)

    op.drop_constraint(
        "ck_teaching_briefs_class_requires_course",
        "teaching_briefs",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_teaching_briefs_class_course",
        "teaching_briefs",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_teaching_briefs_snapshot_tenant",
        "teaching_briefs",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_teaching_briefs_source_snapshot_id_source_snapshots",
        "teaching_briefs",
        "source_snapshots",
        ["source_snapshot_id"],
        ["id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_teaching_briefs_class_id_classes",
        "teaching_briefs",
        "classes",
        ["class_id"],
        ["id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "ck_tenant_source_bindings_class_requires_course",
        "tenant_source_bindings",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_tenant_source_bindings_class_course",
        "tenant_source_bindings",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_tenant_source_bindings_snapshot_tenant",
        "tenant_source_bindings",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_tenant_source_bindings_source_snapshot_id_source_snapshots",
        "tenant_source_bindings",
        "source_snapshots",
        ["source_snapshot_id"],
        ["id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tenant_source_bindings_class_id_classes",
        "tenant_source_bindings",
        "classes",
        ["class_id"],
        ["id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "fk_source_snapshots_upload_tenant",
        "source_snapshots",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "ck_source_snapshots_pdf_upload",
        "source_snapshots",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "ck_source_snapshots_knowledge_generation",
        "source_snapshots",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "uq_source_snapshots_id_tenant",
        "source_snapshots",
        type_="unique",
        schema=tenant_schema,
    )

    op.add_column(
        "source_uploads",
        sa.Column("source_snapshot_id", sa.String(length=128), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "source_uploads",
        sa.Column("filename", sa.String(length=512), nullable=False),
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_source_uploads_source_snapshot_id_source_snapshots",
        "source_uploads",
        "source_snapshots",
        ["source_snapshot_id"],
        ["id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    for constraint_name, constraint_type in (
        ("ck_source_uploads_receipt_state", "check"),
        ("ck_source_uploads_ownership_token", "check"),
        ("ck_source_uploads_sha256", "check"),
        ("ck_source_uploads_status", "check"),
        ("uq_source_uploads_tenant_sha256", "unique"),
        ("uq_source_uploads_id_tenant", "unique"),
    ):
        op.drop_constraint(
            constraint_name,
            "source_uploads",
            type_=constraint_type,
            schema=tenant_schema,
        )
    op.alter_column(
        "source_uploads",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'uploaded'"),
        existing_nullable=False,
        schema=tenant_schema,
    )
    for column_name in (
        "updated_at",
        "last_error_code",
        "object_version_id",
        "object_revision",
        "ownership_token",
    ):
        op.drop_column("source_uploads", column_name, schema=tenant_schema)

    op.drop_column("source_snapshots", "display_name", schema=tenant_schema)
    op.drop_column("source_snapshots", "source_upload_id", schema=tenant_schema)
    op.drop_constraint(
        "uq_source_snapshots_tenant_source_revision",
        "source_snapshots",
        type_="unique",
        schema=tenant_schema,
    )
    op.create_unique_constraint(
        "uq_source_snapshots_tenant_source_revision",
        "source_snapshots",
        ["tenant_id", "source_type", "source_id", "source_revision"],
        schema=tenant_schema,
    )
    op.drop_column("source_snapshots", "resource_owner_id", schema=tenant_schema)
    op.drop_constraint(
        "uq_classes_id_course",
        "classes",
        type_="unique",
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260803_0009", "20260802_0008")


def downgrade() -> None:
    if _migration_scope() == "platform":
        _downgrade_platform()
    else:
        _downgrade_tenant()
