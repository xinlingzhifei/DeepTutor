"""Add append-only classroom learning events and projection state."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260810_0016"
down_revision: str | None = "20260809_0015"
branch_labels: str | None = None
depends_on: str | None = None

_LEARNING_TABLES = (
    "learning_sessions",
    "learning_events",
    "learning_projection_queue",
    "quiz_attempts",
    "mastery_evidence",
    "mastery_levels",
    "learning_progress",
    "learning_event_quarantine",
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
        raise RuntimeError("tenant schema revision update was lost")


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "learning_sessions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("assignment_id", sa.String(length=128), nullable=True),
        sa.Column("student_asset_id", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "next_seq",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("last_cursor", jsonb, nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_learning_sessions_classroom_version_id_classroom_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["tenant.assignments.id"],
            name="fk_learning_sessions_assignment_id_assignments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["student_asset_id", "tenant_id"],
            [
                "tenant.student_classroom_assets.asset_id",
                "tenant.student_classroom_assets.tenant_id",
            ],
            name="fk_learning_sessions_student_asset_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_learning_sessions"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_learning_sessions_id_tenant",
        ),
        sa.CheckConstraint(
            "(assignment_id IS NOT NULL AND student_asset_id IS NULL) OR "
            "(assignment_id IS NULL AND student_asset_id IS NOT NULL)",
            name="ck_learning_sessions_authority",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'abandoned')",
            name="ck_learning_sessions_status",
        ),
        sa.CheckConstraint("next_seq > 0", name="ck_learning_sessions_next_seq"),
        sa.CheckConstraint(
            "(status = 'active' AND completed_at IS NULL) OR "
            "(status IN ('completed', 'abandoned') AND completed_at IS NOT NULL)",
            name="ck_learning_sessions_completion",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_learning_sessions_user_status",
        "learning_sessions",
        ["user_id", "status"],
        schema=tenant_schema,
    )
    op.create_index(
        "ix_learning_sessions_classroom_version",
        "learning_sessions",
        ["classroom_version_id"],
        schema=tenant_schema,
    )

    op.create_table(
        "learning_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scene_id", sa.String(length=128), nullable=True),
        sa.Column("knowledge_point_id", sa.String(length=128), nullable=True),
        sa.Column("payload", jsonb, nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_learning_events_classroom_version_id_classroom_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["tenant.learning_sessions.id", "tenant.learning_sessions.tenant_id"],
            name="fk_learning_events_session_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_learning_events"),
        sa.UniqueConstraint("event_id", name="uq_learning_events_event_id"),
        sa.UniqueConstraint(
            "session_id",
            "seq",
            name="uq_learning_events_session_seq",
        ),
        sa.CheckConstraint("seq > 0", name="ck_learning_events_seq"),
        sa.CheckConstraint(
            "length(event_id) > 0",
            name="ck_learning_events_event_id",
        ),
        sa.CheckConstraint(
            "length(event_type) > 0",
            name="ck_learning_events_event_type",
        ),
        schema="tenant",
    )
    for index_name, column_name in (
        ("ix_learning_events_event_type", "event_type"),
        ("ix_learning_events_occurred_at", "occurred_at"),
        ("ix_learning_events_session_id", "session_id"),
        ("ix_learning_events_classroom_version_id", "classroom_version_id"),
        ("ix_learning_events_knowledge_point_id", "knowledge_point_id"),
    ):
        op.create_index(
            index_name,
            "learning_events",
            [column_name],
            schema=tenant_schema,
        )

    op.create_table(
        "learning_projection_queue",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("8"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["tenant.learning_events.event_id"],
            name="fk_learning_projection_queue_event_id_learning_events",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_learning_projection_queue"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'quarantined')",
            name="ck_learning_projection_queue_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="ck_learning_projection_queue_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL "
            "AND heartbeat_at IS NULL)",
            name="ck_learning_projection_queue_lease_fence",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_learning_projection_queue_available",
        "learning_projection_queue",
        ["status", "available_at", "created_at"],
        schema=tenant_schema,
    )
    op.create_index(
        "ix_learning_projection_queue_session",
        "learning_projection_queue",
        ["session_id"],
        schema=tenant_schema,
    )

    op.create_table(
        "quiz_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("question_id", sa.String(length=128), nullable=False),
        sa.Column("knowledge_point_id", sa.String(length=128), nullable=False),
        sa.Column("answer_payload", jsonb, nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("grading_source", sa.String(length=32), nullable=False),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["tenant.learning_events.event_id"],
            name="fk_quiz_attempts_event_id_learning_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quiz_attempts"),
        sa.UniqueConstraint("event_id", name="uq_quiz_attempts_event_id"),
        sa.CheckConstraint(
            "score >= 0 AND score <= 1",
            name="ck_quiz_attempts_score",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_quiz_attempts_user_knowledge",
        "quiz_attempts",
        ["user_id", "knowledge_point_id"],
        schema=tenant_schema,
    )
    op.create_index(
        "ix_quiz_attempts_session",
        "quiz_attempts",
        ["session_id"],
        schema=tenant_schema,
    )

    op.create_table(
        "mastery_evidence",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("knowledge_point_id", sa.String(length=128), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("correctness", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("grading_source", sa.String(length=32), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["tenant.learning_events.event_id"],
            name="fk_mastery_evidence_event_id_learning_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mastery_evidence"),
        sa.UniqueConstraint("event_id", name="uq_mastery_evidence_event_id"),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_mastery_evidence_score",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_mastery_evidence_user_knowledge",
        "mastery_evidence",
        ["user_id", "knowledge_point_id", "recorded_at"],
        schema=tenant_schema,
    )

    op.create_table(
        "mastery_levels",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("knowledge_point_id", sa.String(length=128), nullable=False),
        sa.Column(
            "level",
            sa.Float(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "evidence_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_evidence_event_id", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["last_evidence_event_id"],
            ["tenant.learning_events.event_id"],
            name="fk_mastery_levels_last_evidence_event_id_learning_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mastery_levels"),
        sa.UniqueConstraint(
            "user_id",
            "knowledge_point_id",
            name="uq_mastery_levels_user_knowledge",
        ),
        sa.CheckConstraint(
            "level >= 0 AND level <= 1",
            name="ck_mastery_levels_level",
        ),
        sa.CheckConstraint(
            "evidence_count >= 0",
            name="ck_mastery_levels_evidence_count",
        ),
        schema="tenant",
    )

    op.create_table(
        "learning_progress",
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("last_event_id", sa.String(length=128), nullable=True),
        sa.Column(
            "last_event_seq",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "completed_scene_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_scene_id", sa.String(length=128), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["tenant.learning_sessions.id"],
            name="fk_learning_progress_session_id_learning_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_event_id"],
            ["tenant.learning_events.event_id"],
            name="fk_learning_progress_last_event_id_learning_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", name="pk_learning_progress"),
        sa.CheckConstraint(
            "status IN ('active', 'completed')",
            name="ck_learning_progress_status",
        ),
        sa.CheckConstraint(
            "last_event_seq >= 0 AND completed_scene_count >= 0",
            name="ck_learning_progress_counts",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name="ck_learning_progress_completion",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_learning_progress_user_status",
        "learning_progress",
        ["user_id", "status"],
        schema=tenant_schema,
    )
    op.create_index(
        "ix_learning_progress_classroom_version",
        "learning_progress",
        ["classroom_version_id"],
        schema=tenant_schema,
    )

    op.create_table(
        "learning_event_quarantine",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("knowledge_point_id", sa.String(length=128), nullable=True),
        sa.Column("payload", jsonb, nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("details", jsonb, nullable=True),
        sa.Column(
            "quarantined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["tenant.learning_sessions.id"],
            name="fk_learning_event_quarantine_session_id_learning_sessions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_learning_event_quarantine"),
        sa.CheckConstraint(
            "length(reason_code) > 0",
            name="ck_learning_event_quarantine_reason_code",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_learning_event_quarantine_event",
        "learning_event_quarantine",
        ["event_id"],
        schema=tenant_schema,
    )
    op.create_index(
        "ix_learning_event_quarantine_reason",
        "learning_event_quarantine",
        ["reason_code", "quarantined_at"],
        schema=tenant_schema,
    )

    _sync_tenant_schema_revision("20260809_0015", "20260810_0016")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    connection = op.get_bind()
    qualified_tables = ", ".join(f"{quoted_schema}.{table_name}" for table_name in _LEARNING_TABLES)
    connection.execute(sa.text(f"LOCK TABLE {qualified_tables} IN ACCESS EXCLUSIVE MODE"))
    for table_name in _LEARNING_TABLES:
        has_data = connection.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.{table_name})")
        ).scalar()
        if has_data:
            raise CommandError("cannot downgrade learning events: durable facts exist")

    for table_name in (
        "learning_event_quarantine",
        "learning_progress",
        "mastery_levels",
        "mastery_evidence",
        "quiz_attempts",
        "learning_projection_queue",
        "learning_events",
        "learning_sessions",
    ):
        op.drop_table(table_name, schema=tenant_schema)
    _sync_tenant_schema_revision("20260810_0016", "20260809_0015")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
