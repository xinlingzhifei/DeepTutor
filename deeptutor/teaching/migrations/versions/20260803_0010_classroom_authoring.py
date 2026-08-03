"""Persist classroom outlines, validation reports, and draft media receipts."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260803_0010"
down_revision: str | None = "20260803_0009"
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
    for column in (
        sa.Column("generation_job_id", sa.String(length=64), nullable=True),
        sa.Column("outline_document", sa.Text(), nullable=True),
        sa.Column("outline_sha256", sa.String(length=64), nullable=True),
        sa.Column("confirmed_outline_sha256", sa.String(length=64), nullable=True),
        sa.Column("validation_report", sa.Text(), nullable=True),
        sa.Column("validation_report_sha256", sa.String(length=64), nullable=True),
    ):
        op.add_column("classroom_drafts", column, schema=tenant_schema)
    op.create_foreign_key(
        "fk_classroom_drafts_job_tenant_generation_jobs",
        "classroom_drafts",
        "generation_jobs",
        ["generation_job_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="SET NULL",
    )

    op.create_table(
        "classroom_draft_media",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_id", sa.String(length=128), nullable=False),
        sa.Column("uploaded_by", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="writing", nullable=False),
        sa.Column("ownership_token", sa.String(length=32), nullable=False),
        sa.Column("object_revision", sa.String(length=256), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_classroom_draft_media_sha256",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0 AND size_bytes <= 104857600",
            name="ck_classroom_draft_media_size_bytes",
        ),
        sa.CheckConstraint(
            "status IN ('writing', 'uploaded', 'failed')",
            name="ck_classroom_draft_media_status",
        ),
        sa.CheckConstraint(
            "ownership_token ~ '^[0-9a-f]{32}$'",
            name="ck_classroom_draft_media_ownership_token",
        ),
        sa.CheckConstraint(
            "(status = 'writing' AND object_revision IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'uploaded' AND object_revision IS NOT NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'failed' AND last_error_code IS NOT NULL)",
            name="ck_classroom_draft_media_receipt_state",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_classroom_draft_media_asset_tenant_classroom_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classroom_draft_media"),
        sa.UniqueConstraint(
            "id",
            "classroom_id",
            "tenant_id",
            name="uq_classroom_draft_media_id_classroom_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "object_key",
            name="uq_classroom_draft_media_tenant_object_key",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_classroom_draft_media_asset_created",
        "classroom_draft_media",
        ["classroom_id", "created_at"],
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260803_0009", "20260803_0010")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    connection = op.get_bind()
    connection.execute(
        sa.text(f"LOCK TABLE {quoted_schema}.classroom_drafts IN ACCESS EXCLUSIVE MODE")
    )
    connection.execute(
        sa.text(f"LOCK TABLE {quoted_schema}.classroom_draft_media IN ACCESS EXCLUSIVE MODE")
    )
    media_exists = connection.execute(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.classroom_draft_media)")
    ).scalar()
    authored_exists = connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM {quoted_schema}.classroom_drafts
                WHERE outline_document IS NOT NULL
                   OR outline_sha256 IS NOT NULL
                   OR confirmed_outline_sha256 IS NOT NULL
                   OR validation_report IS NOT NULL
                   OR validation_report_sha256 IS NOT NULL
            )
            """
        )
    ).scalar()
    if media_exists or authored_exists:
        raise CommandError(
            "cannot downgrade classroom authoring: draft authoring data exists"
        )

    op.drop_index(
        "ix_classroom_draft_media_asset_created",
        table_name="classroom_draft_media",
        schema=tenant_schema,
    )
    op.drop_table("classroom_draft_media", schema=tenant_schema)
    op.drop_constraint(
        "fk_classroom_drafts_job_tenant_generation_jobs",
        "classroom_drafts",
        type_="foreignkey",
        schema=tenant_schema,
    )
    for column_name in (
        "validation_report_sha256",
        "validation_report",
        "confirmed_outline_sha256",
        "outline_sha256",
        "outline_document",
        "generation_job_id",
    ):
        op.drop_column("classroom_drafts", column_name, schema=tenant_schema)
    _sync_tenant_schema_revision("20260803_0010", "20260803_0009")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
