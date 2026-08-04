"""Pin classroom export inputs, outputs, idempotency, and tenant policy."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260804_0012"
down_revision: str | None = "20260803_0011"
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

    op.create_table(
        "classroom_export_policies",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "allow_mp4",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_classroom_export_policies"),
        schema="tenant",
    )

    for column in (
        sa.Column("classroom_id", sa.String(length=128), nullable=True),
        sa.Column("classroom_draft_id", sa.String(length=128), nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=True),
        sa.Column("input_document_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "input_media_manifest_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
        sa.Column("input_manifest_object_key", sa.String(length=512), nullable=True),
        sa.Column("input_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("relative_name", sa.String(length=512), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ):
        op.add_column("classroom_exports", column, schema=tenant_schema)

    op.drop_constraint(
        "fk_classroom_exports_classroom_version_id_classroom_versions",
        "classroom_exports",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.alter_column(
        "classroom_exports",
        "classroom_version_id",
        existing_type=sa.String(length=128),
        nullable=True,
        schema=tenant_schema,
    )
    op.alter_column(
        "classroom_exports",
        "object_key",
        existing_type=sa.String(length=512),
        nullable=True,
        schema=tenant_schema,
    )
    op.alter_column(
        "classroom_exports",
        "sha256",
        existing_type=sa.String(length=64),
        nullable=True,
        schema=tenant_schema,
    )
    op.alter_column(
        "classroom_exports",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'preparing_input'"),
        schema=tenant_schema,
    )

    op.create_check_constraint(
        "ck_classroom_exports_record_shape",
        "classroom_exports",
        "(classroom_id IS NULL AND classroom_version_id IS NOT NULL "
        "AND classroom_draft_id IS NULL AND draft_revision IS NULL "
        "AND input_document_sha256 IS NULL "
        "AND input_media_manifest_sha256 IS NULL "
        "AND idempotency_key IS NULL AND request_sha256 IS NULL "
        "AND input_manifest_object_key IS NULL "
        "AND input_manifest_sha256 IS NULL "
        "AND relative_name IS NULL AND size_bytes IS NULL "
        "AND mime_type IS NULL) OR "
        "(classroom_id IS NOT NULL "
        "AND input_document_sha256 IS NOT NULL "
        "AND input_media_manifest_sha256 IS NOT NULL "
        "AND idempotency_key IS NOT NULL AND request_sha256 IS NOT NULL)",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_exports_target",
        "classroom_exports",
        "classroom_id IS NULL OR ("
        "(classroom_version_id IS NOT NULL AND classroom_draft_id IS NULL) OR "
        "(classroom_version_id IS NULL AND classroom_draft_id IS NOT NULL))",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_exports_draft_revision",
        "classroom_exports",
        "classroom_id IS NULL OR ("
        "(classroom_draft_id IS NULL AND draft_revision IS NULL) OR "
        "(classroom_draft_id IS NOT NULL AND draft_revision IS NOT NULL "
        "AND draft_revision > 0))",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_exports_format",
        "classroom_exports",
        "classroom_id IS NULL OR export_format IN ('classroom_zip', 'pptx', 'offline_html', 'mp4')",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_exports_hashes",
        "classroom_exports",
        "classroom_id IS NULL OR ("
        "input_document_sha256 ~ '^[0-9a-f]{64}$' AND "
        "input_media_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
        "request_sha256 ~ '^[0-9a-f]{64}$' AND "
        "(input_manifest_sha256 IS NULL OR "
        "input_manifest_sha256 ~ '^[0-9a-f]{64}$') AND "
        "(sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'))",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_exports_input_receipt",
        "classroom_exports",
        "(input_manifest_object_key IS NULL AND input_manifest_sha256 IS NULL) OR "
        "(input_manifest_object_key IS NOT NULL "
        "AND input_manifest_sha256 IS NOT NULL)",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_exports_output_receipt",
        "classroom_exports",
        "classroom_id IS NULL OR ("
        "(relative_name IS NULL AND object_key IS NULL AND sha256 IS NULL "
        "AND size_bytes IS NULL AND mime_type IS NULL AND status <> 'ready') OR "
        "(relative_name IS NOT NULL AND object_key IS NOT NULL "
        "AND sha256 IS NOT NULL AND size_bytes IS NOT NULL "
        "AND size_bytes >= 0 AND mime_type IS NOT NULL AND status = 'ready'))",
        schema=tenant_schema,
    )

    op.create_foreign_key(
        "fk_classroom_exports_asset_tenant_classroom_assets",
        "classroom_exports",
        "classroom_assets",
        ["classroom_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_classroom_exports_version_classroom_tenant",
        "classroom_exports",
        "classroom_versions",
        ["classroom_version_id", "classroom_id", "tenant_id"],
        ["id", "classroom_id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_classroom_exports_draft_classroom_tenant",
        "classroom_exports",
        "classroom_drafts",
        ["classroom_draft_id", "classroom_id", "tenant_id"],
        ["id", "classroom_id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_classroom_exports_tenant_idempotency",
        "classroom_exports",
        ["tenant_id", "idempotency_key"],
        schema=tenant_schema,
    )
    op.create_unique_constraint(
        "uq_classroom_exports_tenant_generation_job",
        "classroom_exports",
        ["tenant_id", "generation_job_id"],
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260803_0011", "20260804_0012")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted = f'"{tenant_schema}"'
    connection = op.get_bind()
    for table_name in ("classroom_exports", "classroom_export_policies"):
        connection.execute(sa.text(f"LOCK TABLE {quoted}.{table_name} IN ACCESS EXCLUSIVE MODE"))
    new_exports = connection.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {quoted}.classroom_exports "
            "WHERE classroom_id IS NOT NULL)"
        )
    ).scalar()
    policies = connection.execute(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted}.classroom_export_policies)")
    ).scalar()
    if new_exports or policies:
        raise CommandError("cannot downgrade classroom exports: durable task-6 data exists")

    for constraint_name, constraint_type in (
        ("uq_classroom_exports_tenant_generation_job", "unique"),
        ("uq_classroom_exports_tenant_idempotency", "unique"),
        ("fk_classroom_exports_draft_classroom_tenant", "foreignkey"),
        ("fk_classroom_exports_version_classroom_tenant", "foreignkey"),
        ("fk_classroom_exports_asset_tenant_classroom_assets", "foreignkey"),
        ("ck_classroom_exports_output_receipt", "check"),
        ("ck_classroom_exports_input_receipt", "check"),
        ("ck_classroom_exports_hashes", "check"),
        ("ck_classroom_exports_format", "check"),
        ("ck_classroom_exports_draft_revision", "check"),
        ("ck_classroom_exports_target", "check"),
        ("ck_classroom_exports_record_shape", "check"),
    ):
        op.drop_constraint(
            constraint_name,
            "classroom_exports",
            type_=constraint_type,
            schema=tenant_schema,
        )

    for column_name in (
        "updated_at",
        "mime_type",
        "size_bytes",
        "relative_name",
        "input_manifest_sha256",
        "input_manifest_object_key",
        "request_sha256",
        "idempotency_key",
        "input_media_manifest_sha256",
        "input_document_sha256",
        "draft_revision",
        "classroom_draft_id",
        "classroom_id",
    ):
        op.drop_column("classroom_exports", column_name, schema=tenant_schema)

    op.alter_column(
        "classroom_exports",
        "classroom_version_id",
        existing_type=sa.String(length=128),
        nullable=False,
        schema=tenant_schema,
    )
    op.alter_column(
        "classroom_exports",
        "object_key",
        existing_type=sa.String(length=512),
        nullable=False,
        schema=tenant_schema,
    )
    op.alter_column(
        "classroom_exports",
        "sha256",
        existing_type=sa.String(length=64),
        nullable=False,
        schema=tenant_schema,
    )
    op.alter_column(
        "classroom_exports",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'ready'"),
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_classroom_exports_classroom_version_id_classroom_versions",
        "classroom_exports",
        "classroom_versions",
        ["classroom_version_id"],
        ["id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.drop_table("classroom_export_policies", schema=tenant_schema)
    _sync_tenant_schema_revision("20260804_0012", "20260803_0011")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
