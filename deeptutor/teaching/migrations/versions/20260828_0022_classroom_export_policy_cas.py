"""Persist classroom export policy revisions and mutation ownership."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260828_0022"
down_revision: str | None = "20260827_0021"
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


def _qualified_policy_table() -> str:
    connection = op.get_bind()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(_tenant_schema())
    return f'{quoted_schema}."classroom_export_policies"'


def _qualified_operation_table() -> str:
    connection = op.get_bind()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(_tenant_schema())
    return f'{quoted_schema}."classroom_export_policy_operations"'


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    policy_table = _qualified_policy_table()
    op.add_column(
        "classroom_export_policies",
        sa.Column(
            "exists",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        schema=tenant_schema,
    )
    op.add_column(
        "classroom_export_policies",
        sa.Column("revision", sa.String(length=64), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "classroom_export_policies",
        sa.Column("operation_id", sa.String(length=32), nullable=True),
        schema=tenant_schema,
    )
    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {policy_table}
            SET revision = md5(random()::text || clock_timestamp()::text)
                || md5(random()::text || clock_timestamp()::text)
            WHERE revision IS NULL
            """
        )
    )
    op.alter_column(
        "classroom_export_policies",
        "revision",
        existing_type=sa.String(length=64),
        nullable=False,
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_export_policies_tombstone",
        "classroom_export_policies",
        '"exists" OR NOT allow_mp4',
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_export_policies_revision",
        "classroom_export_policies",
        "revision ~ '^[0-9a-f]{64}$'",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_classroom_export_policies_operation_id",
        "classroom_export_policies",
        "operation_id IS NULL OR operation_id ~ '^[0-9a-f]{32}$'",
        schema=tenant_schema,
    )
    op.create_table(
        "classroom_export_policy_operations",
        sa.Column("operation_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("mutation", sa.String(length=16), nullable=False),
        sa.Column("expected_revision", sa.String(length=64), nullable=False),
        sa.Column("allow_mp4", sa.Boolean(), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("result_exists", sa.Boolean(), nullable=False),
        sa.Column("result_allow_mp4", sa.Boolean(), nullable=False),
        sa.Column("result_revision", sa.String(length=64), nullable=False),
        sa.Column("result_operation_id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_classroom_export_policy_operations"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.classroom_export_policies.tenant_id"],
            name="fk_classroom_export_policy_operations_policy",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "operation_id ~ '^[0-9a-f]{32}$'",
            name="ck_classroom_export_policy_operations_operation_id",
        ),
        sa.CheckConstraint(
            "expected_revision = 'absent' OR expected_revision ~ '^[0-9a-f]{64}$'",
            name="ck_classroom_export_policy_operations_expected_revision",
        ),
        sa.CheckConstraint(
            "result_revision ~ '^[0-9a-f]{64}$'",
            name="ck_classroom_export_policy_operations_result_revision",
        ),
        sa.CheckConstraint(
            "result_operation_id = operation_id",
            name="ck_classroom_export_policy_operations_result_operation",
        ),
        sa.CheckConstraint(
            "(mutation = 'replace' AND allow_mp4 IS NOT NULL AND result_exists "
            "AND result_allow_mp4 = allow_mp4) OR "
            "(mutation = 'delete' AND allow_mp4 IS NULL AND NOT result_exists "
            "AND NOT result_allow_mp4)",
            name="ck_classroom_export_policy_operations_shape",
        ),
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260827_0021", "20260828_0022")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    operation_table = _qualified_operation_table()
    policy_table = _qualified_policy_table()
    connection = op.get_bind()
    connection.execute(
        sa.text(f"LOCK TABLE {policy_table}, {operation_table} IN ACCESS EXCLUSIVE MODE")
    )
    durable_state = connection.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {operation_table}) "
            f'OR EXISTS (SELECT 1 FROM {policy_table} WHERE NOT "exists")'
        )
    ).scalar()
    if durable_state:
        raise CommandError("cannot downgrade classroom export policy CAS history")
    op.drop_table("classroom_export_policy_operations", schema=tenant_schema)
    op.drop_constraint(
        "ck_classroom_export_policies_operation_id",
        "classroom_export_policies",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "ck_classroom_export_policies_tombstone",
        "classroom_export_policies",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "ck_classroom_export_policies_revision",
        "classroom_export_policies",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_column("classroom_export_policies", "operation_id", schema=tenant_schema)
    op.drop_column("classroom_export_policies", "revision", schema=tenant_schema)
    op.drop_column("classroom_export_policies", "exists", schema=tenant_schema)
    _sync_tenant_schema_revision("20260828_0022", "20260827_0021")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
