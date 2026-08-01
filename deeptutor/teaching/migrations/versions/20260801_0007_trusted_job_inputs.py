"""Persist trusted classroom resource bindings for generation jobs."""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision: str = "20260801_0007"
down_revision: str | None = "20260801_0006"
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


def upgrade() -> None:
    if _migration_scope() != "tenant":
        return
    tenant_schema = _tenant_schema()
    op.add_column(
        "generation_jobs",
        sa.Column("resource_course_id", sa.String(length=64), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "generation_jobs",
        sa.Column("resource_class_id", sa.String(length=64), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "generation_jobs",
        sa.Column("public_request_sha256", sa.String(length=64), nullable=True),
        schema=tenant_schema,
    )
    _sync_tenant_schema_revision("20260801_0006", "20260801_0007")


def downgrade() -> None:
    if _migration_scope() != "tenant":
        return
    tenant_schema = _tenant_schema()
    _sync_tenant_schema_revision("20260801_0007", "20260801_0006")
    op.drop_column("generation_jobs", "public_request_sha256", schema=tenant_schema)
    op.drop_column("generation_jobs", "resource_class_id", schema=tenant_schema)
    op.drop_column("generation_jobs", "resource_course_id", schema=tenant_schema)
