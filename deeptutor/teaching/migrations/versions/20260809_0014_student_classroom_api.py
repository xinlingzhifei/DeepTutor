"""Add durable student classroom and teacher-copy audit links."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260809_0014"
down_revision: str | None = "20260809_0013"
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
    if state is not None and (
        state["status"] != "active" or state["revision"] != source_revision
    ):
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
        raise RuntimeError("tenant schema revision update was lost")


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    op.create_table(
        "student_classroom_assets",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_student_classroom_assets_asset_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            [
                "tenant.student_generation_requests.id",
                "tenant.student_generation_requests.tenant_id",
            ],
            name="fk_student_classroom_assets_request_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("asset_id", name="pk_student_classroom_assets"),
        sa.UniqueConstraint(
            "asset_id",
            "tenant_id",
            name="uq_student_classroom_assets_asset_tenant",
        ),
        sa.UniqueConstraint(
            "request_id",
            "tenant_id",
            name="uq_student_classroom_assets_request_tenant",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_student_classroom_assets_tenant_created",
        "student_classroom_assets",
        ["tenant_id", "created_at"],
        schema=tenant_schema,
    )
    op.create_table(
        "student_classroom_copies",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_asset_id", sa.String(length=128), nullable=False),
        sa.Column("teacher_asset_id", sa.String(length=128), nullable=False),
        sa.Column("copied_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_asset_id", "tenant_id"],
            [
                "tenant.student_classroom_assets.asset_id",
                "tenant.student_classroom_assets.tenant_id",
            ],
            name="fk_student_classroom_copies_source_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["teacher_asset_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_student_classroom_copies_teacher_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_student_classroom_copies"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_student_classroom_copies_id_tenant",
        ),
        sa.UniqueConstraint(
            "teacher_asset_id",
            "tenant_id",
            name="uq_student_classroom_copies_teacher_tenant",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_student_classroom_copies_source_created",
        "student_classroom_copies",
        ["tenant_id", "source_asset_id", "created_at"],
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260809_0013", "20260809_0014")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    connection = op.get_bind()
    table_names = ("student_classroom_copies", "student_classroom_assets")
    for table_name in table_names:
        connection.execute(
            sa.text(f"LOCK TABLE {quoted_schema}.{table_name} IN ACCESS EXCLUSIVE MODE")
        )
    has_data = any(
        connection.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.{table_name})")
        ).scalar()
        for table_name in table_names
    )
    if has_data:
        raise CommandError(
            "cannot downgrade student classroom API: durable classroom links exist"
        )
    op.drop_table("student_classroom_copies", schema=tenant_schema)
    op.drop_table("student_classroom_assets", schema=tenant_schema)
    _sync_tenant_schema_revision("20260809_0014", "20260809_0013")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
