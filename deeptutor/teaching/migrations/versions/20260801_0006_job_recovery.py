"""Add recoverable materialization and immutable classroom artifacts."""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision: str = "20260801_0006"
down_revision: str | None = "20260730_0005"
branch_labels: str | None = None
depends_on: str | None = None


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


def _tenant_schema() -> str:
    return context.get_x_argument(as_dictionary=True)["tenant_schema"]


def _sync_tenant_schema_revision(source_revision: str, target_revision: str) -> None:
    tenant_schema = context.get_x_argument(as_dictionary=True)["tenant_schema"]
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


def _upgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    op.add_column(
        "generation_jobs",
        sa.Column("result_payload", sa.Text(), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "generation_jobs",
        sa.Column("retry_of_job_id", sa.String(length=64), nullable=True),
        schema=tenant_schema,
    )
    op.add_column(
        "generation_jobs",
        sa.Column(
            "dsl_repair_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        schema=tenant_schema,
    )
    op.create_foreign_key(
        "fk_generation_jobs_retry_of_job_id_generation_jobs",
        "generation_jobs",
        "generation_jobs",
        ["retry_of_job_id"],
        ["id"],
        source_schema=tenant_schema,
        referent_schema=tenant_schema,
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_generation_jobs_dsl_repair_attempts",
        "generation_jobs",
        "dsl_repair_attempts >= 0 AND dsl_repair_attempts <= 2",
        schema=tenant_schema,
    )

    op.create_table(
        "artifact_promotion_states",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_id", sa.String(length=128), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'prepared'"),
            nullable=False,
        ),
        sa.Column("object_committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
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
            "version_number > 0",
            name="ck_artifact_promotion_states_version_number",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'object_committed', 'finalized')",
            name="ck_artifact_promotion_states_status",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            ["tenant.generation_jobs.id", "tenant.generation_jobs.tenant_id"],
            name="fk_artifact_promotion_job_tenant_generation_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_artifact_promotion_states"),
        sa.UniqueConstraint(
            "tenant_id",
            "classroom_id",
            "version_number",
            name="uq_artifact_promotion_tenant_classroom_version",
        ),
        schema="tenant",
    )

    op.create_table(
        "classroom_versions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_id", sa.String(length=128), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("generation_job_id", sa.String(length=64), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("media_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("document_object_key", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name="ck_classroom_versions_version_number",
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id", "tenant_id"],
            ["tenant.generation_jobs.id", "tenant.generation_jobs.tenant_id"],
            name="fk_classroom_versions_job_tenant_generation_jobs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classroom_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "classroom_id",
            "version_number",
            name="uq_classroom_versions_tenant_classroom_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "generation_job_id",
            name="uq_classroom_versions_tenant_generation_job",
        ),
        schema="tenant",
    )

    op.create_table(
        "classroom_artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_job_id", sa.String(length=64), nullable=False),
        sa.Column("classroom_version_id", sa.String(length=128), nullable=True),
        sa.Column("artifact_kind", sa.String(length=16), nullable=False),
        sa.Column("relative_name", sa.String(length=512), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=160), nullable=False),
        sa.Column("input_document_sha256", sa.String(length=64), nullable=True),
        sa.Column("input_media_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('dsl_json', 'media', 'export')",
            name="ck_classroom_artifacts_artifact_kind",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_classroom_artifacts_size_bytes",
        ),
        sa.ForeignKeyConstraint(
            ["source_job_id", "tenant_id"],
            ["tenant.generation_jobs.id", "tenant.generation_jobs.tenant_id"],
            name="fk_classroom_artifacts_job_tenant_generation_jobs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_classroom_artifacts_classroom_version_id_classroom_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classroom_artifacts"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_job_id",
            "relative_name",
            name="uq_classroom_artifacts_tenant_job_name",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "object_key",
            name="uq_classroom_artifacts_tenant_object_key",
        ),
        schema="tenant",
    )

    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {quoted_schema}.reject_immutable_classroom_record()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'immutable classroom record';
            END;
            $$
            """
        )
    )
    for table_name in ("classroom_versions", "classroom_artifacts"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER reject_{table_name}_mutation
                BEFORE UPDATE OR DELETE ON {quoted_schema}.{table_name}
                FOR EACH ROW EXECUTE FUNCTION {quoted_schema}.reject_immutable_classroom_record()
                """
            )
        )
    _sync_tenant_schema_revision("20260730_0005", "20260801_0006")


def upgrade() -> None:
    if _migration_scope() == "tenant":
        _upgrade_tenant()


def _downgrade_tenant() -> None:
    tenant_schema = _tenant_schema()
    quoted_schema = f'"{tenant_schema}"'
    _sync_tenant_schema_revision("20260801_0006", "20260730_0005")
    for table_name in ("classroom_artifacts", "classroom_versions"):
        op.execute(
            sa.text(f"DROP TRIGGER reject_{table_name}_mutation ON {quoted_schema}.{table_name}")
        )
    op.execute(sa.text(f"DROP FUNCTION {quoted_schema}.reject_immutable_classroom_record()"))
    op.drop_table("classroom_artifacts", schema=tenant_schema)
    op.drop_table("classroom_versions", schema=tenant_schema)
    op.drop_table("artifact_promotion_states", schema=tenant_schema)
    op.drop_constraint(
        "ck_generation_jobs_dsl_repair_attempts",
        "generation_jobs",
        type_="check",
        schema=tenant_schema,
    )
    op.drop_constraint(
        "fk_generation_jobs_retry_of_job_id_generation_jobs",
        "generation_jobs",
        type_="foreignkey",
        schema=tenant_schema,
    )
    op.drop_column("generation_jobs", "dsl_repair_attempts", schema=tenant_schema)
    op.drop_column("generation_jobs", "retry_of_job_id", schema=tenant_schema)
    op.drop_column("generation_jobs", "result_payload", schema=tenant_schema)


def downgrade() -> None:
    if _migration_scope() == "tenant":
        _downgrade_tenant()
