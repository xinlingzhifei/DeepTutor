"""Add durable teaching runtime process heartbeats."""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision: str = "20260824_0018"
down_revision: str | None = "20260810_0017"
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


def _upgrade_platform() -> None:
    op.create_table(
        "teaching_runtime_process_heartbeats",
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'running'"),
            nullable=False,
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(role)) > 0",
            name="ck_teaching_runtime_process_heartbeats_role_not_empty",
        ),
        sa.CheckConstraint(
            "role IN ('tenant_provisioner', 'dispatcher', 'generation_worker', "
            "'export_worker', 'projector', 'reaper')",
            name="ck_teaching_runtime_process_heartbeats_role",
        ),
        sa.CheckConstraint(
            "length(btrim(instance_id)) > 0",
            name="ck_teaching_runtime_process_heartbeats_instance_id_not_empty",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'stopped')",
            name="ck_teaching_runtime_process_heartbeats_status",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND stopped_at IS NULL) OR "
            "(status = 'stopped' AND stopped_at IS NOT NULL)",
            name="ck_teaching_runtime_process_heartbeats_status_stopped_at",
        ),
        sa.CheckConstraint(
            "heartbeat_at >= started_at AND updated_at >= started_at "
            "AND (stopped_at IS NULL OR stopped_at >= started_at)",
            name="ck_teaching_runtime_process_heartbeats_timestamps",
        ),
        sa.PrimaryKeyConstraint(
            "role",
            "instance_id",
            name="pk_teaching_runtime_process_heartbeats",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_teaching_runtime_process_heartbeats_role_heartbeat_running",
        "teaching_runtime_process_heartbeats",
        ["role", sa.text("heartbeat_at DESC")],
        schema="platform",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_teaching_runtime_process_heartbeats_heartbeat_running_ttl",
        "teaching_runtime_process_heartbeats",
        ["heartbeat_at"],
        schema="platform",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_teaching_runtime_process_heartbeats_stopped_at_retention",
        "teaching_runtime_process_heartbeats",
        ["stopped_at"],
        schema="platform",
        postgresql_where=sa.text("status = 'stopped'"),
    )


def upgrade() -> None:
    if _migration_scope() == "platform":
        _upgrade_platform()
    else:
        _sync_tenant_schema_revision("20260810_0017", "20260824_0018")


def downgrade() -> None:
    if _migration_scope() == "platform":
        op.drop_table("teaching_runtime_process_heartbeats", schema="platform")
    else:
        _sync_tenant_schema_revision("20260824_0018", "20260810_0017")
