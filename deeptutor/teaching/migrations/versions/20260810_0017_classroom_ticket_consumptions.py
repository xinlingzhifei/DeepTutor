"""Add durable classroom ticket consumption facts."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260810_0017"
down_revision: str | None = "20260810_0016"
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
        "classroom_ticket_consumptions",
        sa.Column("jti", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=False),
        sa.Column("allowed_action", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            [
                "tenant.learning_sessions.id",
                "tenant.learning_sessions.tenant_id",
            ],
            name="fk_classroom_ticket_consumptions_session_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_classroom_ticket_consumptions_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "jti",
            name="pk_classroom_ticket_consumptions",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_classroom_ticket_consumptions_validity",
        ),
        sa.CheckConstraint(
            "allowed_action = 'learning_event.append'",
            name="ck_classroom_ticket_consumptions_allowed_action",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_classroom_ticket_consumptions_expires_at",
        "classroom_ticket_consumptions",
        ["expires_at"],
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260810_0016", "20260810_0017")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    connection = op.get_bind()
    connection.execute(
        sa.text(
            f"LOCK TABLE {quoted_schema}.classroom_ticket_consumptions IN ACCESS EXCLUSIVE MODE"
        )
    )
    has_data = connection.execute(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted_schema}.classroom_ticket_consumptions)")
    ).scalar()
    if has_data:
        raise CommandError(
            "cannot downgrade classroom tickets: durable ticket consumption facts exist"
        )
    op.drop_table("classroom_ticket_consumptions", schema=tenant_schema)
    _sync_tenant_schema_revision("20260810_0017", "20260810_0016")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
