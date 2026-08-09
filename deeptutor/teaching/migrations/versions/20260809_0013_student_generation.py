"""Add course policy and durable student generation decisions."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260809_0013"
down_revision: str | None = "20260804_0012"
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
        "course_generation_policies",
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "allow_student_micro",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "allow_student_full",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("allowed_content_modes", sa.String(length=64), nullable=False),
        sa.Column(
            "allow_web_search",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "require_approval_for_restricted_topics",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "minor_safety_mode",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "micro_scene_limit",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column(
            "full_scene_limit",
            sa.Integer(),
            server_default=sa.text("24"),
            nullable=False,
        ),
        sa.Column("daily_student_units", sa.Integer(), nullable=False),
        sa.Column("monthly_student_units", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "allowed_content_modes IN ('source_grounded', 'open_creation', "
            "'source_grounded,open_creation')",
            name="ck_course_generation_policies_allowed_content_modes",
        ),
        sa.CheckConstraint(
            "micro_scene_limit >= 1 AND micro_scene_limit <= 5",
            name="ck_course_generation_policies_micro_scene_limit",
        ),
        sa.CheckConstraint(
            "full_scene_limit >= 1 AND full_scene_limit <= 24",
            name="ck_course_generation_policies_full_scene_limit",
        ),
        sa.CheckConstraint(
            "daily_student_units >= 0",
            name="ck_course_generation_policies_daily_student_units",
        ),
        sa.CheckConstraint(
            "monthly_student_units >= 0",
            name="ck_course_generation_policies_monthly_student_units",
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["tenant.courses.id"],
            name="fk_course_generation_policies_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "course_id",
            name="pk_course_generation_policies",
        ),
        sa.UniqueConstraint(
            "course_id",
            "tenant_id",
            name="uq_course_generation_policies_course_tenant",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_course_generation_policies_tenant",
        "course_generation_policies",
        ["tenant_id"],
        schema=tenant_schema,
    )

    op.create_table(
        "student_generation_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("learner_id", sa.String(length=128), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("class_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("content_mode", sa.String(length=32), nullable=False),
        sa.Column("web_search_requested", sa.Boolean(), nullable=False),
        sa.Column("scene_min", sa.Integer(), nullable=False),
        sa.Column("scene_max", sa.Integer(), nullable=False),
        sa.Column("duration_minutes_min", sa.Integer(), nullable=False),
        sa.Column("duration_minutes_max", sa.Integer(), nullable=False),
        sa.Column("estimated_units", sa.Integer(), nullable=False),
        sa.Column("quota_state", sa.String(length=16), nullable=False),
        sa.Column("requires_outline_confirmation", sa.Boolean(), nullable=False),
        sa.Column("decision_outcome", sa.String(length=32), nullable=False),
        sa.Column("decision_reason", sa.String(length=64), nullable=False),
        sa.Column("evaluated_checks", sa.String(length=256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('micro', 'full')",
            name="ck_student_generation_requests_mode",
        ),
        sa.CheckConstraint(
            "content_mode IN ('source_grounded', 'open_creation')",
            name="ck_student_generation_requests_content_mode",
        ),
        sa.CheckConstraint(
            "scene_min >= 1 AND scene_max >= scene_min AND scene_max <= 24 "
            "AND (mode <> 'micro' OR scene_max <= 5)",
            name="ck_student_generation_requests_scene_range",
        ),
        sa.CheckConstraint(
            "duration_minutes_min >= 1 AND duration_minutes_max >= duration_minutes_min",
            name="ck_student_generation_requests_duration_range",
        ),
        sa.CheckConstraint(
            "estimated_units > 0",
            name="ck_student_generation_requests_estimated_units",
        ),
        sa.CheckConstraint(
            "quota_state IN ('none', 'reserved', 'settled', 'released')",
            name="ck_student_generation_requests_quota_state",
        ),
        sa.CheckConstraint(
            "(decision_outcome = 'accepted' AND quota_state IN "
            "('reserved', 'settled', 'released')) OR "
            "(decision_outcome <> 'accepted' AND quota_state = 'none')",
            name="ck_student_generation_requests_quota_lifecycle",
        ),
        sa.CheckConstraint(
            "(mode = 'micro' AND requires_outline_confirmation = false) OR "
            "(mode = 'full' AND requires_outline_confirmation = true)",
            name="ck_student_generation_requests_outline_confirmation",
        ),
        sa.CheckConstraint(
            "decision_outcome IN ('denied', 'approval_required', 'accepted')",
            name="ck_student_generation_requests_decision_outcome",
        ),
        sa.CheckConstraint(
            "length(decision_reason) > 0 AND length(evaluated_checks) > 0",
            name="ck_student_generation_requests_decision_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["class_id", "course_id"],
            ["tenant.classes.id", "tenant.classes.course_id"],
            name="fk_student_generation_requests_class_course_classes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["course_id", "tenant_id"],
            [
                "tenant.course_generation_policies.course_id",
                "tenant.course_generation_policies.tenant_id",
            ],
            name="fk_student_generation_requests_policy_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_student_generation_requests"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_student_generation_requests_id_tenant",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_student_generation_requests_quota_usage",
        "student_generation_requests",
        ["tenant_id", "learner_id", "course_id", "quota_state", "created_at"],
        schema=tenant_schema,
    )

    op.create_table(
        "student_generation_approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_student_generation_approvals_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(status <> 'pending' AND decided_by IS NOT NULL "
            "AND decided_at IS NOT NULL)",
            name="ck_student_generation_approvals_decision_shape",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            [
                "tenant.student_generation_requests.id",
                "tenant.student_generation_requests.tenant_id",
            ],
            name="fk_student_generation_approvals_request_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_student_generation_approvals"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_student_generation_approvals_id_tenant",
        ),
        schema="tenant",
    )
    op.create_index(
        "uq_student_generation_approvals_pending_request",
        "student_generation_approvals",
        ["tenant_id", "request_id"],
        unique=True,
        schema=tenant_schema,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_student_generation_approvals_status_requested",
        "student_generation_approvals",
        ["tenant_id", "status", "requested_at"],
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260804_0012", "20260809_0013")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    connection = op.get_bind()
    table_names = (
        "student_generation_approvals",
        "student_generation_requests",
        "course_generation_policies",
    )
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
        raise CommandError("cannot downgrade student generation: durable task-1 data exists")
    op.drop_table("student_generation_approvals", schema=tenant_schema)
    op.drop_table("student_generation_requests", schema=tenant_schema)
    op.drop_table("course_generation_policies", schema=tenant_schema)
    _sync_tenant_schema_revision("20260809_0013", "20260804_0012")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
