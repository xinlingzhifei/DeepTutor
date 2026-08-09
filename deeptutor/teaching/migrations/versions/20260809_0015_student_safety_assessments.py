"""Add trusted, exact student safety assessment evidence."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260809_0015"
down_revision: str | None = "20260809_0014"
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
        "student_safety_assessments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("class_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("content_mode", sa.String(length=32), nullable=False),
        sa.Column("web_search_requested", sa.Boolean(), nullable=False),
        sa.Column("generally_safe", sa.Boolean(), nullable=False),
        sa.Column("minor_safe", sa.Boolean(), nullable=False),
        sa.Column("restricted_topic", sa.Boolean(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assessment_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["class_id", "course_id"],
            ["tenant.classes.id", "tenant.classes.course_id"],
            name="fk_student_safety_assessments_class_course",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["course_id", "tenant_id"],
            [
                "tenant.course_generation_policies.course_id",
                "tenant.course_generation_policies.tenant_id",
            ],
            name="fk_student_safety_assessments_policy_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_student_safety_assessments"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_student_safety_assessments_id_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "course_id",
            "class_id",
            "mode",
            "content_mode",
            "web_search_requested",
            "assessment_version",
            name="uq_student_safety_assessments_binding_version",
        ),
        sa.CheckConstraint("mode IN ('micro', 'full')", name="mode"),
        sa.CheckConstraint(
            "content_mode IN ('source_grounded', 'open_creation')",
            name="content_mode",
        ),
        sa.CheckConstraint("assessment_version > 0", name="assessment_version"),
        sa.CheckConstraint("length(reviewed_by) > 0", name="reviewed_by"),
        sa.CheckConstraint("reviewed_at < expires_at", name="validity_window"),
        schema="tenant",
    )
    op.create_index(
        "ix_student_safety_assessments_lookup",
        "student_safety_assessments",
        [
            "tenant_id",
            "course_id",
            "class_id",
            "mode",
            "content_mode",
            "web_search_requested",
            "expires_at",
        ],
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260809_0014", "20260809_0015")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    connection = op.get_bind()
    connection.execute(
        sa.text(
            f"LOCK TABLE {quoted_schema}.student_safety_assessments "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    has_data = connection.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM "
            f"{quoted_schema}.student_safety_assessments)"
        )
    ).scalar()
    if has_data:
        raise CommandError(
            "cannot downgrade student safety assessments: durable evidence exists"
        )
    op.drop_table("student_safety_assessments", schema=tenant_schema)
    _sync_tenant_schema_revision("20260809_0015", "20260809_0014")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
