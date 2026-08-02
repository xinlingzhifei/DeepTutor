"""Add tenant knowledge-resource entitlements."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260803_0009"
down_revision: str | None = "20260802_0008"
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


def _upgrade_platform() -> None:
    op.create_table(
        "tenant_knowledge_entitlements",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("knowledge_resource_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column("granted_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_tenant_knowledge_entitlements_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_knowledge_entitlements_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "knowledge_resource_id",
            name="pk_tenant_knowledge_entitlements",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tenant_knowledge_entitlements_resource_status",
        "tenant_knowledge_entitlements",
        ["knowledge_resource_id", "status"],
        schema="platform",
    )


def upgrade() -> None:
    if _migration_scope() == "platform":
        _upgrade_platform()
    else:
        _sync_tenant_schema_revision("20260802_0008", "20260803_0009")


def _downgrade_platform() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE platform.tenant_knowledge_entitlements IN ACCESS EXCLUSIVE MODE")
    )
    has_rows = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM platform.tenant_knowledge_entitlements)")
    ).scalar()
    if has_rows:
        raise CommandError(
            "cannot downgrade knowledge entitlements: active authorization data exists"
        )
    op.drop_table("tenant_knowledge_entitlements", schema="platform")


def downgrade() -> None:
    if _migration_scope() == "platform":
        _downgrade_platform()
    else:
        _sync_tenant_schema_revision("20260803_0009", "20260802_0008")
