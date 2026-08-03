"""Persist review bindings, publication idempotency, and assignment migration audit."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260803_0011"
down_revision: str | None = "20260803_0010"
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
    if state is not None and (
        state["status"] != "active" or state["revision"] != source_revision
    ):
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


def _create_append_only_guards(tenant_schema: str) -> None:
    quoted = f'"{tenant_schema}"'
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {quoted}.reject_review_audit_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'append-only classroom audit record';
            END;
            $$
            """
        )
    )
    for table_name in ("approvals", "publications", "assignment_migrations"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table_name}_append_only
                BEFORE UPDATE OR DELETE ON {quoted}.{table_name}
                FOR EACH ROW EXECUTE FUNCTION {quoted}.reject_review_audit_mutation()
                """
            )
        )


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    op.create_table(
        "classroom_review_policies",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "teacher_self_publish",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "org_content_requires_review",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "platform_template_requires_review",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "prohibit_self_review",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_classroom_review_policies"),
        schema="tenant",
    )
    op.create_table(
        "classroom_review_requests",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_id", sa.String(length=128), nullable=False),
        sa.Column("classroom_draft_id", sa.String(length=128), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("validation_report_sha256", sa.String(length=64), nullable=False),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("class_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "warnings",
            sa.Text(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "draft_revision > 0",
            name="ck_classroom_review_requests_draft_revision",
        ),
        sa.CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$' AND "
            "validation_report_sha256 ~ '^[0-9a-f]{64}$' AND "
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_classroom_review_requests_hashes",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_classroom_review_requests_status",
        ),
        sa.CheckConstraint(
            "(scope = 'class' AND class_id IS NOT NULL) OR "
            "(scope IN ('tenant', 'platform') AND class_id IS NULL)",
            name="ck_classroom_review_requests_scope_class",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decided_at IS NULL "
            "AND decision_comment IS NULL) OR "
            "(status IN ('approved', 'rejected') AND decided_by IS NOT NULL "
            "AND decided_at IS NOT NULL AND decision_comment IS NOT NULL)",
            name="ck_classroom_review_requests_decision_binding",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_draft_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_drafts.id",
                "tenant.classroom_drafts.classroom_id",
                "tenant.classroom_drafts.tenant_id",
            ],
            name="fk_classroom_review_requests_draft_classroom_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_classroom_review_requests_asset_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_classroom_review_requests_class",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classroom_review_requests"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_classroom_review_requests_id_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_classroom_review_requests_tenant_idempotency",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_classroom_review_requests_pending",
        "classroom_review_requests",
        ["status", "created_at"],
        schema=tenant_schema,
    )

    op.add_column(
        "approvals",
        sa.Column("review_request_id", sa.String(length=128), nullable=True),
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_approvals_review_request_tenant",
        "approvals",
        "classroom_review_requests",
        ["review_request_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_approvals_terminal_review_decision",
        "approvals",
        ["tenant_id", "review_request_id"],
        unique=True,
        schema=tenant_schema,
        postgresql_where=sa.text("decision IN ('approved', 'rejected')"),
    )

    op.drop_constraint(
        "ck_publications_scope_class",
        "publications",
        type_="check",
        schema=tenant_schema,
    )
    for column in (
        sa.Column("review_request_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
    ):
        op.add_column("publications", column, schema=tenant_schema)
    op.create_check_constraint(
        "ck_publications_scope_class",
        "publications",
        "(scope = 'class' AND class_id IS NOT NULL) OR "
        "(scope IN ('private', 'tenant', 'platform') AND class_id IS NULL)",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_publications_idempotency_binding",
        "publications",
        "(idempotency_key IS NULL AND request_sha256 IS NULL) OR "
        "(idempotency_key IS NOT NULL AND request_sha256 IS NOT NULL)",
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_publications_review_request_tenant",
        "publications",
        "classroom_review_requests",
        ["review_request_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_publications_tenant_idempotency",
        "publications",
        ["tenant_id", "idempotency_key"],
        schema=tenant_schema,
    )
    op.create_unique_constraint(
        "uq_publications_tenant_review_request",
        "publications",
        ["tenant_id", "review_request_id"],
        schema=tenant_schema,
    )

    for column in (
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
    ):
        op.add_column("assignments", column, schema=tenant_schema)
    op.create_check_constraint(
        "ck_assignments_idempotency_binding",
        "assignments",
        "(idempotency_key IS NULL AND request_sha256 IS NULL) OR "
        "(idempotency_key IS NOT NULL AND request_sha256 IS NOT NULL)",
        schema=tenant_schema,
    )
    op.create_unique_constraint(
        "uq_assignments_tenant_idempotency",
        "assignments",
        ["tenant_id", "idempotency_key"],
        schema=tenant_schema,
    )

    op.create_table(
        "class_learning_states",
        sa.Column("class_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column(
            "active_session_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(state IN ('unknown', 'idle') AND active_session_count = 0) OR "
            "(state = 'active' AND active_session_count > 0)",
            name="ck_class_learning_states_state_count",
        ),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_class_learning_states_class_id_classes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("class_id", name="pk_class_learning_states"),
        sa.UniqueConstraint(
            "class_id",
            "tenant_id",
            name="uq_class_learning_states_class_tenant",
        ),
        schema="tenant",
    )
    op.create_table(
        "assignment_migrations",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("old_assignment_id", sa.String(length=128), nullable=False),
        sa.Column("old_version_id", sa.String(length=128), nullable=False),
        sa.Column("new_version_id", sa.String(length=128), nullable=False),
        sa.Column("new_assignment_id", sa.String(length=128), nullable=True),
        sa.Column("class_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'refused_active_learning', "
            "'refused_guard_unavailable')",
            name="ck_assignment_migrations_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'succeeded' AND new_assignment_id IS NOT NULL) OR "
            "(outcome <> 'succeeded' AND new_assignment_id IS NULL)",
            name="ck_assignment_migrations_outcome_assignment",
        ),
        sa.ForeignKeyConstraint(
            ["old_assignment_id"],
            ["tenant.assignments.id"],
            name="fk_assignment_migrations_old_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["new_assignment_id"],
            ["tenant.assignments.id"],
            name="fk_assignment_migrations_new_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["old_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_assignment_migrations_old_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["new_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_assignment_migrations_new_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_assignment_migrations_class",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignment_migrations"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_assignment_migrations_tenant_idempotency",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_assignment_migrations_class_created",
        "assignment_migrations",
        ["class_id", "created_at"],
        schema=tenant_schema,
    )
    _create_append_only_guards(tenant_schema)
    _sync_tenant_schema_revision("20260803_0010", "20260803_0011")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _drop_append_only_guards(tenant_schema: str) -> None:
    quoted = f'"{tenant_schema}"'
    for table_name in ("approvals", "publications", "assignment_migrations"):
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {table_name}_append_only "
                f"ON {quoted}.{table_name}"
            )
        )
    op.execute(
        sa.text(f"DROP FUNCTION IF EXISTS {quoted}.reject_review_audit_mutation()")
    )


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted = f'"{tenant_schema}"'
    connection = op.get_bind()
    for table_name in (
        "classroom_review_requests",
        "classroom_review_policies",
        "assignment_migrations",
        "class_learning_states",
    ):
        connection.execute(
            sa.text(f"LOCK TABLE {quoted}.{table_name} IN ACCESS EXCLUSIVE MODE")
        )
        if connection.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {quoted}.{table_name})")
        ).scalar():
            raise CommandError(
                "cannot downgrade classroom review/publication: durable data exists"
            )
    publication_extensions = connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM {quoted}.publications
                WHERE scope = 'platform'
                   OR review_request_id IS NOT NULL
                   OR idempotency_key IS NOT NULL
                   OR request_sha256 IS NOT NULL
            )
            """
        )
    ).scalar()
    assignment_extensions = connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM {quoted}.assignments
                WHERE idempotency_key IS NOT NULL OR request_sha256 IS NOT NULL
            )
            """
        )
    ).scalar()
    approval_extensions = connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM {quoted}.approvals
                WHERE review_request_id IS NOT NULL
            )
            """
        )
    ).scalar()
    if publication_extensions or assignment_extensions or approval_extensions:
        raise CommandError(
            "cannot downgrade classroom review/publication: durable bindings exist"
        )

    _drop_append_only_guards(tenant_schema)
    op.drop_index(
        "ix_assignment_migrations_class_created",
        table_name="assignment_migrations",
        schema=tenant_schema,
    )
    op.drop_table("assignment_migrations", schema=tenant_schema)
    op.drop_table("class_learning_states", schema=tenant_schema)

    op.drop_constraint(
        "uq_assignments_tenant_idempotency",
        "assignments",
        type_="unique",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "ck_assignments_idempotency_binding",
        "assignments",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_column("assignments", "request_sha256", schema=tenant_schema)
    op.drop_column("assignments", "idempotency_key", schema=tenant_schema)

    op.drop_constraint(
        "uq_publications_tenant_review_request",
        "publications",
        type_="unique",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "uq_publications_tenant_idempotency",
        "publications",
        type_="unique",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_publications_review_request_tenant",
        "publications",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "ck_publications_idempotency_binding",
        "publications",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "ck_publications_scope_class",
        "publications",
        type_="check",
        schema=tenant_schema,
    )
    op.create_check_constraint(
        "ck_publications_scope_class",
        "publications",
        "(scope = 'class' AND class_id IS NOT NULL) OR "
        "(scope IN ('private', 'tenant') AND class_id IS NULL)",
        schema=tenant_schema,
    )
    for column_name in ("request_sha256", "idempotency_key", "review_request_id"):
        op.drop_column("publications", column_name, schema=tenant_schema)

    op.drop_index(
        "uq_approvals_terminal_review_decision",
        table_name="approvals",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_approvals_review_request_tenant",
        "approvals",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_column("approvals", "review_request_id", schema=tenant_schema)
    op.drop_index(
        "ix_classroom_review_requests_pending",
        table_name="classroom_review_requests",
        schema=tenant_schema,
    )
    op.drop_table("classroom_review_requests", schema=tenant_schema)
    op.drop_table("classroom_review_policies", schema=tenant_schema)
    _sync_tenant_schema_revision("20260803_0011", "20260803_0010")


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
