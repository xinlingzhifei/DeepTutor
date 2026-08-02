"""Add classroom lifecycle, publication, source, and batch records."""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision: str = "20260802_0008"
down_revision: str | None = "20260801_0007"
branch_labels: str | None = None
depends_on: str | None = None


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


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'

    op.create_table(
        "source_snapshots",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_revision", sa.String(length=128), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("permission_sha256", sa.String(length=64), nullable=False),
        sa.Column("citation_manifest", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_snapshots"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_type",
            "source_id",
            "source_revision",
            name="uq_source_snapshots_tenant_source_revision",
        ),
        schema="tenant",
    )
    op.create_table(
        "source_uploads",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("uploaded_by", sa.String(length=128), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'uploaded'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_source_uploads_size_bytes"),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"],
            ["tenant.source_snapshots.id"],
            name="fk_source_uploads_source_snapshot_id_source_snapshots",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_uploads"),
        sa.UniqueConstraint(
            "tenant_id",
            "object_key",
            name="uq_source_uploads_tenant_object_key",
        ),
        schema="tenant",
    )
    op.create_table(
        "tenant_source_bindings",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=True),
        sa.Column("class_id", sa.String(length=64), nullable=True),
        sa.Column("bound_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "course_id IS NOT NULL OR class_id IS NOT NULL",
            name="ck_tenant_source_bindings_resource_scope",
        ),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_tenant_source_bindings_class_id_classes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["tenant.courses.id"],
            name="fk_tenant_source_bindings_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"],
            ["tenant.source_snapshots.id"],
            name="fk_tenant_source_bindings_source_snapshot_id_source_snapshots",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_source_bindings"),
        schema="tenant",
    )
    op.create_index(
        "ix_tenant_source_bindings_snapshot",
        "tenant_source_bindings",
        ["source_snapshot_id"],
        schema=tenant_schema,
    )
    op.create_table(
        "teaching_briefs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("course_id", sa.String(length=64), nullable=True),
        sa.Column("class_id", sa.String(length=64), nullable=True),
        sa.Column("brief_version", sa.Integer(), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "brief_version > 0",
            name="ck_teaching_briefs_brief_version",
        ),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_teaching_briefs_class_id_classes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["tenant.courses.id"],
            name="fk_teaching_briefs_course_id_courses",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"],
            ["tenant.source_snapshots.id"],
            name="fk_teaching_briefs_source_snapshot_id_source_snapshots",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_teaching_briefs"),
        schema="tenant",
    )
    op.create_table(
        "classroom_assets",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("current_published_version_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ("
            "'draft', 'generating_outline', 'awaiting_outline', "
            "'generating_content', 'editing', 'submitted', 'rejected', "
            "'validated', 'approved', 'published', 'failed', 'canceled'"
            ")",
            name="ck_classroom_assets_lifecycle_state",
        ),
        sa.ForeignKeyConstraint(
            ["current_published_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_classroom_assets_current_version_classroom_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classroom_assets"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_classroom_assets_id_tenant",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_classroom_assets_tenant_owner_state",
        "classroom_assets",
        ["tenant_id", "owner_id", "lifecycle_state"],
        schema=tenant_schema,
    )

    op.execute(
        sa.text(
            f"""
            INSERT INTO {quoted_schema}.classroom_assets (
                id,
                tenant_id,
                owner_id,
                title,
                lifecycle_state
            )
            SELECT DISTINCT ON (versions.classroom_id)
                versions.classroom_id,
                versions.tenant_id,
                jobs.owner_id,
                NULL,
                'editing'
            FROM {quoted_schema}.classroom_versions AS versions
            JOIN {quoted_schema}.generation_jobs AS jobs
              ON jobs.id = versions.generation_job_id
             AND jobs.tenant_id = versions.tenant_id
            ORDER BY versions.classroom_id, versions.version_number DESC
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    op.create_foreign_key(
        "fk_classroom_versions_asset_tenant_classroom_assets",
        "classroom_versions",
        "classroom_assets",
        ["classroom_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )

    op.create_table(
        "classroom_drafts",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_id", sa.String(length=128), nullable=False),
        sa.Column("teaching_brief_id", sa.String(length=128), nullable=True),
        sa.Column("base_version_id", sa.String(length=128), nullable=True),
        sa.Column(
            "revision",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("revision > 0", name="ck_classroom_drafts_revision"),
        sa.ForeignKeyConstraint(
            ["base_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_classroom_drafts_base_version_id_classroom_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_classroom_drafts_asset_tenant_classroom_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["teaching_brief_id"],
            ["tenant.teaching_briefs.id"],
            name="fk_classroom_drafts_teaching_brief_id_teaching_briefs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classroom_drafts"),
        schema="tenant",
    )
    op.create_index(
        "ix_classroom_drafts_classroom_updated",
        "classroom_drafts",
        ["classroom_id", "updated_at"],
        schema=tenant_schema,
    )
    op.create_table(
        "classroom_exports",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("generation_job_id", sa.String(length=64), nullable=True),
        sa.Column("export_format", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'ready'"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_classroom_exports_classroom_version_id_classroom_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id", "tenant_id"],
            ["tenant.generation_jobs.id", "tenant.generation_jobs.tenant_id"],
            name="fk_classroom_exports_job_tenant_generation_jobs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classroom_exports"),
        sa.UniqueConstraint(
            "tenant_id",
            "object_key",
            name="uq_classroom_exports_tenant_object_key",
        ),
        schema="tenant",
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_draft_id", sa.String(length=128), nullable=False),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('submitted', 'approved', 'rejected')",
            name="ck_approvals_decision",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_draft_id"],
            ["tenant.classroom_drafts.id"],
            name="fk_approvals_classroom_draft_id_classroom_drafts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_approvals_asset_tenant_classroom_assets",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        schema="tenant",
    )
    op.create_index(
        "ix_approvals_classroom_created",
        "approvals",
        ["classroom_id", "created_at"],
        schema=tenant_schema,
    )
    op.create_table(
        "publications",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("class_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(scope = 'class' AND class_id IS NOT NULL) OR "
            "(scope IN ('private', 'tenant') AND class_id IS NULL)",
            name="ck_publications_scope_class",
        ),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_publications_class_id_classes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_publications_asset_tenant_classroom_assets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_publications_classroom_version_id_classroom_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_publications"),
        sa.UniqueConstraint(
            "tenant_id",
            "classroom_version_id",
            "scope",
            "class_id",
            name="uq_publications_tenant_version_scope_class",
        ),
        schema="tenant",
    )
    op.create_table(
        "assignments",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("class_id", sa.String(length=64), nullable=False),
        sa.Column("assigned_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_assignments_class_id_classes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_assignments_classroom_version_id_classroom_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignments"),
        sa.UniqueConstraint(
            "tenant_id",
            "class_id",
            "classroom_version_id",
            name="uq_assignments_tenant_class_version",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_assignments_class_active",
        "assignments",
        ["class_id", "revoked_at"],
        schema=tenant_schema,
    )
    op.create_table(
        "batch_jobs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'created'"),
            nullable=False,
        ),
        sa.Column(
            "item_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "succeeded_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "failed_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_count >= 0 AND succeeded_count >= 0 AND failed_count >= 0 "
            "AND succeeded_count + failed_count <= item_count",
            name="ck_batch_jobs_counts",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_batch_jobs"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_batch_jobs_id_tenant"),
        schema="tenant",
    )
    op.create_table(
        "batch_items",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("batch_job_id", sa.String(length=128), nullable=False),
        sa.Column("generation_job_id", sa.String(length=64), nullable=True),
        sa.Column("classroom_draft_id", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'created'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["batch_job_id", "tenant_id"],
            ["tenant.batch_jobs.id", "tenant.batch_jobs.tenant_id"],
            name="fk_batch_items_batch_tenant_batch_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_draft_id"],
            ["tenant.classroom_drafts.id"],
            name="fk_batch_items_classroom_draft_id_classroom_drafts",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id", "tenant_id"],
            ["tenant.generation_jobs.id", "tenant.generation_jobs.tenant_id"],
            name="fk_batch_items_job_tenant_generation_jobs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_batch_items"),
        sa.UniqueConstraint(
            "tenant_id",
            "batch_job_id",
            "id",
            name="uq_batch_items_tenant_batch_item",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_batch_items_batch_status",
        "batch_items",
        ["batch_job_id", "status"],
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260801_0007", "20260802_0008")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    _sync_tenant_schema_revision("20260802_0008", "20260801_0007")
    op.drop_table("batch_items", schema=tenant_schema)
    op.drop_table("batch_jobs", schema=tenant_schema)
    op.drop_table("assignments", schema=tenant_schema)
    op.drop_table("publications", schema=tenant_schema)
    op.drop_table("approvals", schema=tenant_schema)
    op.drop_table("classroom_exports", schema=tenant_schema)
    op.drop_table("classroom_drafts", schema=tenant_schema)
    op.drop_constraint(
        "fk_classroom_versions_asset_tenant_classroom_assets",
        "classroom_versions",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_table("classroom_assets", schema=tenant_schema)
    op.drop_table("teaching_briefs", schema=tenant_schema)
    op.drop_table("tenant_source_bindings", schema=tenant_schema)
    op.drop_table("source_uploads", schema=tenant_schema)
    op.drop_table("source_snapshots", schema=tenant_schema)


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
