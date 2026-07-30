"""Replace legacy schema routes with fail-closed data-plane routing."""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision: str = "20260730_0004"
down_revision: str | None = "20260730_0003"
branch_labels: str | None = None
depends_on: str | None = None


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


def _preserve_legacy_schema_facts() -> None:
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
                CASE WHEN status = 'active' THEN '20260730_0003' ELSE NULL END,
                CASE WHEN status = 'active' THEN 'active' ELSE 'pending' END,
                CASE WHEN status = 'active' THEN updated_at ELSE NULL END,
                updated_at
            FROM platform.data_plane_routes
            ON CONFLICT (tenant_id) DO NOTHING
            """
        )
    )


def _create_provider_profiles() -> None:
    op.create_table(
        "provider_profiles",
        sa.Column("id", sa.String(length=63), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("owner_key", sa.String(length=64), nullable=False),
        sa.Column("provider_type", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("api_base_url", sa.String(length=512), nullable=True),
        sa.Column("secret_ref", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
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
            "scope IN ('shared', 'dedicated')",
            name="ck_provider_profiles_scope",
        ),
        sa.CheckConstraint(
            "("
            "scope = 'shared' AND tenant_id IS NULL AND owner_key = 'shared'"
            ") OR ("
            "scope = 'dedicated' AND tenant_id IS NOT NULL "
            "AND owner_key = tenant_id"
            ")",
            name="ck_provider_profiles_owner_scope",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_provider_profiles_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_provider_profiles_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_profiles"),
        sa.UniqueConstraint(
            "id",
            "scope",
            "owner_key",
            name="uq_provider_profiles_route_binding",
        ),
        sa.UniqueConstraint(
            "secret_ref",
            name="uq_provider_profiles_secret_ref",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_provider_profiles_tenant_status",
        "provider_profiles",
        ["tenant_id", "status"],
        schema="platform",
    )


def _create_data_plane_routes() -> None:
    op.create_table(
        "data_plane_routes",
        sa.Column("id", sa.String(length=63), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("owner_key", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("worker_pool", sa.String(length=128), nullable=False),
        sa.Column("queue_name", sa.String(length=128), nullable=False),
        sa.Column("provider_profile_id", sa.String(length=63), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "health_status",
            sa.String(length=32),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
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
            "mode IN ('shared', 'dedicated')",
            name="ck_data_plane_routes_mode",
        ),
        sa.CheckConstraint(
            "("
            "mode = 'shared' AND tenant_id IS NULL AND owner_key = 'shared'"
            ") OR ("
            "mode = 'dedicated' AND tenant_id IS NOT NULL "
            "AND owner_key = tenant_id"
            ")",
            name="ck_data_plane_routes_owner_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_data_plane_routes_status",
        ),
        sa.CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'unhealthy')",
            name="ck_data_plane_routes_health_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_data_plane_routes_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_profile_id", "mode", "owner_key"],
            [
                "platform.provider_profiles.id",
                "platform.provider_profiles.scope",
                "platform.provider_profiles.owner_key",
            ],
            name="fk_data_plane_routes_provider_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_plane_routes"),
        sa.UniqueConstraint(
            "tenant_id",
            name="uq_data_plane_routes_tenant_id",
        ),
        sa.UniqueConstraint(
            "worker_pool",
            name="uq_data_plane_routes_worker_pool",
        ),
        sa.UniqueConstraint(
            "queue_name",
            name="uq_data_plane_routes_queue_name",
        ),
        schema="platform",
    )
    op.create_index(
        "uq_data_plane_routes_global_shared",
        "data_plane_routes",
        ["mode"],
        unique=True,
        schema="platform",
        postgresql_where=sa.text("mode = 'shared'"),
    )
    op.create_index(
        "ix_data_plane_routes_status_health",
        "data_plane_routes",
        ["status", "health_status"],
        schema="platform",
    )


def upgrade() -> None:
    if _migration_scope() != "platform":
        return

    _preserve_legacy_schema_facts()
    op.drop_index(
        "ix_data_plane_routes_status",
        table_name="data_plane_routes",
        schema="platform",
    )
    op.drop_table("data_plane_routes", schema="platform")
    op.add_column(
        "tenants",
        sa.Column(
            "data_plane_mode",
            sa.String(length=16),
            server_default=sa.text("'shared'"),
            nullable=False,
        ),
        schema="platform",
    )
    op.create_check_constraint(
        "ck_tenants_data_plane_mode",
        "tenants",
        "data_plane_mode IN ('shared', 'dedicated')",
        schema="platform",
    )
    _create_provider_profiles()
    _create_data_plane_routes()


def _create_legacy_routes() -> None:
    op.create_table(
        "data_plane_routes",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("schema_name", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
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
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_data_plane_routes_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_data_plane_routes"),
        sa.UniqueConstraint(
            "schema_name",
            name="uq_data_plane_routes_schema_name",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_data_plane_routes_status",
        "data_plane_routes",
        ["status"],
        schema="platform",
    )
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
            """
        )
    )


def downgrade() -> None:
    if _migration_scope() != "platform":
        return

    op.drop_table("data_plane_routes", schema="platform")
    op.drop_table("provider_profiles", schema="platform")
    op.drop_constraint(
        "ck_tenants_data_plane_mode",
        "tenants",
        schema="platform",
        type_="check",
    )
    op.drop_column("tenants", "data_plane_mode", schema="platform")
    _create_legacy_routes()
