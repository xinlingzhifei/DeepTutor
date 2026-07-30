"""Persist tenant provisioning worker leases and verified prerequisites."""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision: str = "20260730_0003"
down_revision: str | None = "20260730_0002"
branch_labels: str | None = None
depends_on: str | None = None


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


def upgrade() -> None:
    if _migration_scope() != "platform":
        return

    op.create_table(
        "tenant_schema_states",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("schema_name", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_schema_states_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_schema_states"),
        sa.UniqueConstraint("schema_name", name="uq_tenant_schema_states_schema_name"),
        schema="platform",
    )
    op.create_index(
        "ix_tenant_schema_states_status",
        "tenant_schema_states",
        ["status"],
        schema="platform",
    )
    op.execute(
        sa.text(
            """
            INSERT INTO platform.tenant_schema_states (
                tenant_id,
                schema_name,
                revision,
                status,
                verified_at,
                updated_at
            )
            SELECT
                tenant_id,
                schema_name,
                CASE WHEN status = 'active' THEN '20260730_0002' ELSE NULL END,
                CASE WHEN status = 'active' THEN 'active' ELSE 'pending' END,
                CASE WHEN status = 'active' THEN updated_at ELSE NULL END,
                updated_at
            FROM platform.data_plane_routes
            ON CONFLICT (tenant_id) DO NOTHING
            """
        )
    )

    op.create_table(
        "tenant_storage_states",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_payload", sa.Text(), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_secret_ref", sa.String(length=512), nullable=True),
        sa.Column("credential_fingerprint", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_storage_states_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_storage_states"),
        schema="platform",
    )
    op.create_index(
        "ix_tenant_storage_states_status",
        "tenant_storage_states",
        ["status"],
        schema="platform",
    )

    op.create_table(
        "tenant_default_policy_states",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_payload", sa.Text(), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_default_policy_states_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_default_policy_states"),
        schema="platform",
    )
    op.create_index(
        "ix_tenant_default_policy_states_status",
        "tenant_default_policy_states",
        ["status"],
        schema="platform",
    )

    op.add_column(
        "tenant_provisioning_jobs",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        schema="platform",
    )
    op.add_column(
        "tenant_provisioning_jobs",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema="platform",
    )
    for column in (
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("tenant_provisioning_jobs", column, schema="platform")
    op.create_index(
        "ix_tenant_provisioning_jobs_claim",
        "tenant_provisioning_jobs",
        ["status", "next_attempt_at", "lease_expires_at"],
        schema="platform",
    )


def downgrade() -> None:
    if _migration_scope() != "platform":
        return

    op.execute(
        sa.text(
            """
            INSERT INTO platform.data_plane_routes (
                tenant_id,
                schema_name,
                status,
                created_at,
                updated_at
            )
            SELECT
                tenant_id,
                schema_name,
                status,
                updated_at,
                updated_at
            FROM platform.tenant_schema_states
            ON CONFLICT (tenant_id) DO UPDATE
            SET schema_name = EXCLUDED.schema_name,
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at
            """
        )
    )

    op.drop_index(
        "ix_tenant_provisioning_jobs_claim",
        table_name="tenant_provisioning_jobs",
        schema="platform",
    )
    for column_name in (
        "completed_at",
        "started_at",
        "error_code",
        "error_category",
        "heartbeat_at",
        "lease_expires_at",
        "lease_owner",
        "next_attempt_at",
        "max_attempts",
    ):
        op.drop_column("tenant_provisioning_jobs", column_name, schema="platform")

    op.drop_index(
        "ix_tenant_default_policy_states_status",
        table_name="tenant_default_policy_states",
        schema="platform",
    )
    op.drop_table("tenant_default_policy_states", schema="platform")
    op.drop_index(
        "ix_tenant_storage_states_status",
        table_name="tenant_storage_states",
        schema="platform",
    )
    op.drop_table("tenant_storage_states", schema="platform")
    op.drop_index(
        "ix_tenant_schema_states_status",
        table_name="tenant_schema_states",
        schema="platform",
    )
    op.drop_table("tenant_schema_states", schema="platform")
