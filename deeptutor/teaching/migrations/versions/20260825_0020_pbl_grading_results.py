"""Add immutable trusted PBL grading results."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260825_0020"
down_revision: str | None = "20260825_0019"
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
        raise RuntimeError("tenant schema revision update was lost")


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    op.create_table(
        "pbl_grading_results",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("scene_id", sa.String(length=128), nullable=False),
        sa.Column("milestone_id", sa.String(length=128), nullable=False),
        sa.Column("knowledge_point_id", sa.String(length=128), nullable=False),
        sa.Column("rubric_sha256", sa.String(length=64), nullable=False),
        sa.Column("correctness", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("grading_source", sa.String(length=32), nullable=False),
        sa.Column("source_reference", sa.String(length=256), nullable=False),
        sa.Column("graded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "graded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["tenant.learning_events.event_id"],
            name="fk_pbl_grading_results_event_id_learning_events",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_pbl_grading_results_classroom_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["tenant.learning_sessions.id", "tenant.learning_sessions.tenant_id"],
            name="fk_pbl_grading_results_session_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pbl_grading_results"),
        sa.UniqueConstraint("event_id", name="uq_pbl_grading_results_event_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_pbl_grading_results_tenant_idempotency",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_pbl_grading_results_score",
        ),
        sa.CheckConstraint(
            "grading_source = 'teacher_review'",
            name="ck_pbl_grading_results_grading_source",
        ),
        sa.CheckConstraint(
            "length(source_reference) > 0",
            name="ck_pbl_grading_results_source_reference",
        ),
        sa.CheckConstraint(
            "length(graded_by) > 0",
            name="ck_pbl_grading_results_graded_by",
        ),
        sa.CheckConstraint(
            "length(scene_id) > 0",
            name="ck_pbl_grading_results_scene_id",
        ),
        sa.CheckConstraint(
            "length(milestone_id) > 0",
            name="ck_pbl_grading_results_milestone_id",
        ),
        sa.CheckConstraint(
            "length(knowledge_point_id) > 0",
            name="ck_pbl_grading_results_knowledge_point_id",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0",
            name="ck_pbl_grading_results_idempotency_key",
        ),
        sa.CheckConstraint(
            "rubric_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_pbl_grading_results_rubric_sha256",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_pbl_grading_results_request_sha256",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_pbl_grading_results_user_knowledge",
        "pbl_grading_results",
        ["user_id", "knowledge_point_id", "graded_at"],
        schema=tenant_schema,
    )
    op.create_index(
        "ix_pbl_grading_results_session",
        "pbl_grading_results",
        ["session_id"],
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260825_0019", "20260825_0020")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    connection = op.get_bind()
    connection.execute(
        sa.text(f"LOCK TABLE {quoted_schema}.pbl_grading_results IN ACCESS EXCLUSIVE MODE")
    )
    has_data = connection.execute(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.pbl_grading_results)")
    ).scalar()
    if has_data:
        raise CommandError("cannot downgrade PBL grading: durable facts exist")
    op.drop_table("pbl_grading_results", schema=tenant_schema)
    _sync_tenant_schema_revision("20260825_0020", "20260825_0019")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
