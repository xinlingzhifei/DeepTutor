"""Preserve immutable student safety idempotency inputs."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260827_0021"
down_revision: str | None = "20260825_0020"
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


def _qualified_assessments_table() -> str:
    connection = op.get_bind()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(_tenant_schema())
    return f"{quoted_schema}.student_safety_assessments"


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    assessments_table = _qualified_assessments_table()
    op.add_column(
        "student_safety_assessments",
        sa.Column("valid_for_seconds", sa.Integer(), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "student_safety_assessments",
        sa.Column("requested_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema=tenant_schema,
    )
    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {assessments_table}
            SET valid_for_seconds = GREATEST(
                1,
                CEIL(EXTRACT(EPOCH FROM (expires_at - reviewed_at)))::integer
            ),
                requested_expires_at = expires_at
            WHERE valid_for_seconds IS NULL
               OR requested_expires_at IS NULL
            """
        )
    )
    op.alter_column(
        "student_safety_assessments",
        "valid_for_seconds",
        existing_type=sa.Integer(),
        nullable=False,
        schema=tenant_schema,
    )
    op.alter_column(
        "student_safety_assessments",
        "requested_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_student_safety_assessments_valid_for_seconds",
        "student_safety_assessments",
        "valid_for_seconds > 0",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_student_safety_assessments_supersession_window",
        "student_safety_assessments",
        "expires_at <= requested_expires_at",
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260825_0020", "20260827_0021")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    assessments_table = _qualified_assessments_table()
    connection = op.get_bind()
    connection.execute(sa.text(f"LOCK TABLE {assessments_table} IN ACCESS EXCLUSIVE MODE"))
    has_shortened_window = connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM {assessments_table}
                WHERE expires_at <> requested_expires_at
            )
            """
        )
    ).scalar()
    if has_shortened_window:
        raise CommandError(
            "cannot downgrade student safety idempotency: immutable request duration "
            "is required by superseded evidence"
        )
    op.drop_constraint(
        "ck_student_safety_assessments_supersession_window",
        "student_safety_assessments",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "ck_student_safety_assessments_valid_for_seconds",
        "student_safety_assessments",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_column(
        "student_safety_assessments",
        "requested_expires_at",
        schema=tenant_schema,
    )
    op.drop_column(
        "student_safety_assessments",
        "valid_for_seconds",
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260827_0021", "20260825_0020")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
