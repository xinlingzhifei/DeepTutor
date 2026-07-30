"""Persist resource scopes on platform role grants."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260730_0002"
down_revision: str | None = "20260728_0001"
branch_labels: str | None = None
depends_on: str | None = None


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


def upgrade() -> None:
    if _migration_scope() != "platform":
        return

    op.add_column(
        "role_grants",
        sa.Column("scope_type", sa.String(length=16), nullable=True),
        schema="platform",
    )
    op.add_column(
        "role_grants",
        sa.Column("scope_id", sa.String(length=64), nullable=True),
        schema="platform",
    )
    op.execute(
        sa.text(
            """
            UPDATE platform.role_grants
            SET scope_type = 'tenant', scope_id = tenant_id
            WHERE scope_type IS NULL OR scope_id IS NULL
            """
        )
    )
    op.alter_column(
        "role_grants",
        "scope_type",
        existing_type=sa.String(length=16),
        nullable=False,
        schema="platform",
    )
    op.alter_column(
        "role_grants",
        "scope_id",
        existing_type=sa.String(length=64),
        nullable=False,
        schema="platform",
    )
    op.drop_constraint(
        "pk_role_grants",
        "role_grants",
        type_="primary",
        schema="platform",
    )
    op.create_primary_key(
        "pk_role_grants",
        "role_grants",
        ["tenant_id", "user_id", "role", "scope_type", "scope_id"],
        schema="platform",
    )
    op.create_check_constraint(
        "ck_role_grants_scope_type",
        "role_grants",
        "scope_type IN ('tenant', 'course', 'class')",
        schema="platform",
    )
    op.create_index(
        "ix_role_grants_tenant_user_scope",
        "role_grants",
        ["tenant_id", "user_id", "scope_type", "scope_id"],
        schema="platform",
    )


def downgrade() -> None:
    if _migration_scope() != "platform":
        return

    has_resource_scopes = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT EXISTS (
                SELECT 1
                FROM platform.role_grants
                WHERE scope_type <> 'tenant' OR scope_id <> tenant_id
            )
            """
            )
        )
        .scalar()
    )
    if has_resource_scopes:
        raise CommandError("cannot downgrade scoped role grants while resource-scoped data exists")

    op.drop_index(
        "ix_role_grants_tenant_user_scope",
        table_name="role_grants",
        schema="platform",
    )
    op.drop_constraint(
        "ck_role_grants_scope_type",
        "role_grants",
        type_="check",
        schema="platform",
    )
    op.drop_constraint(
        "pk_role_grants",
        "role_grants",
        type_="primary",
        schema="platform",
    )
    op.create_primary_key(
        "pk_role_grants",
        "role_grants",
        ["tenant_id", "user_id", "role"],
        schema="platform",
    )
    op.drop_column("role_grants", "scope_id", schema="platform")
    op.drop_column("role_grants", "scope_type", schema="platform")
