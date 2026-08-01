"""Create durable generation jobs, quota ledger, outbox, queue, and slots."""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa

revision: str = "20260730_0005"
down_revision: str | None = "20260730_0004"
branch_labels: str | None = None
depends_on: str | None = None


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


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


def _upgrade_platform() -> None:
    op.add_column(
        "tenant_provisioning_jobs",
        sa.Column("target_revision", sa.String(length=64), nullable=True),
        schema="platform",
    )
    op.execute(
        sa.text(
            """
            UPDATE platform.tenant_provisioning_jobs
            SET attempt_count = GREATEST(attempt_count, 0),
                max_attempts = GREATEST(max_attempts, attempt_count + 1, 1)
            WHERE attempt_count < 0
               OR max_attempts <= attempt_count
               OR max_attempts <= 0
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE platform.tenants AS tenant
            SET status = 'failed', updated_at = now()
            FROM platform.tenant_provisioning_jobs AS job
            WHERE job.tenant_id = tenant.id
              AND job.operation = 'provision'
              AND job.status = 'running'
              AND (
                  job.lease_owner IS NULL
                  OR job.lease_token IS NULL
                  OR job.lease_expires_at IS NULL
                  OR job.heartbeat_at IS NULL
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE platform.tenant_provisioning_jobs
            SET status = 'failed',
                completed_at = COALESCE(completed_at, now()),
                error_category = COALESCE(error_category, 'worker'),
                error_code = COALESCE(error_code, 'invalid_lease_state'),
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                updated_at = now()
            WHERE status = 'running'
              AND (
                  lease_owner IS NULL
                  OR lease_token IS NULL
                  OR lease_expires_at IS NULL
                  OR heartbeat_at IS NULL
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE platform.tenant_provisioning_jobs
            SET lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                updated_at = now()
            WHERE status != 'running'
              AND (
                  lease_owner IS NOT NULL
                  OR lease_token IS NOT NULL
                  OR lease_expires_at IS NOT NULL
                  OR heartbeat_at IS NOT NULL
              )
            """
        )
    )
    op.create_check_constraint(
        "ck_tenant_provisioning_jobs_operation",
        "tenant_provisioning_jobs",
        "operation IN ('provision', 'upgrade_schema')",
        schema="platform",
    )
    op.create_check_constraint(
        "ck_tenant_provisioning_jobs_operation_target",
        "tenant_provisioning_jobs",
        "(operation = 'provision' AND target_revision IS NULL) OR "
        "(operation = 'upgrade_schema' AND target_revision IS NOT NULL)",
        schema="platform",
    )
    op.create_check_constraint(
        "ck_tenant_provisioning_jobs_attempts",
        "tenant_provisioning_jobs",
        "attempt_count >= 0 AND max_attempts > 0 AND attempt_count < max_attempts",
        schema="platform",
    )
    op.create_check_constraint(
        "ck_tenant_provisioning_jobs_status_lease_fence",
        "tenant_provisioning_jobs",
        "(status = 'running' AND lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL) OR (status != 'running' "
        "AND lease_owner IS NULL AND lease_token IS NULL "
        "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
        schema="platform",
    )
    op.create_unique_constraint(
        "uq_tenant_provisioning_jobs_upgrade_target",
        "tenant_provisioning_jobs",
        ["tenant_id", "operation", "target_revision"],
        schema="platform",
    )
    op.create_table(
        "outbox_messages",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("job_kind", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("data_plane_route_id", sa.String(length=63), nullable=False),
        sa.Column("provider_profile_id", sa.String(length=63), nullable=False),
        sa.Column("worker_pool_ref", sa.String(length=128), nullable=False),
        sa.Column("queue_ref", sa.String(length=128), nullable=False),
        sa.Column("slot_pool", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "job_kind IN ('generation', 'export')",
            name="ck_outbox_messages_job_kind",
        ),
        sa.CheckConstraint(
            "("
            "job_kind = 'generation' AND phase IN ('outline', 'content')"
            ") OR ("
            "job_kind = 'export' AND phase = 'export'"
            ")",
            name="ck_outbox_messages_kind_phase",
        ),
        sa.CheckConstraint(
            "slot_pool IN ('generation', 'mp4_export')",
            name="ck_outbox_messages_slot_pool",
        ),
        sa.CheckConstraint(
            "priority >= 0",
            name="ck_outbox_messages_priority",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_outbox_messages_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_outbox_messages"),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "phase",
            name="uq_outbox_messages_tenant_job_phase",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_outbox_messages_undelivered",
        "outbox_messages",
        ["available_at", "created_at"],
        schema="platform",
        postgresql_where=sa.text("delivered_at IS NULL"),
    )

    op.create_table(
        "generation_queue",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("job_kind", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("data_plane_route_id", sa.String(length=63), nullable=False),
        sa.Column("provider_profile_id", sa.String(length=63), nullable=False),
        sa.Column("worker_pool_ref", sa.String(length=128), nullable=False),
        sa.Column("queue_ref", sa.String(length=128), nullable=False),
        sa.Column("slot_pool", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "job_kind IN ('generation', 'export')",
            name="ck_generation_queue_job_kind",
        ),
        sa.CheckConstraint(
            "("
            "job_kind = 'generation' AND phase IN ('outline', 'content')"
            ") OR ("
            "job_kind = 'export' AND phase = 'export'"
            ")",
            name="ck_generation_queue_kind_phase",
        ),
        sa.CheckConstraint(
            "slot_pool IN ('generation', 'mp4_export')",
            name="ck_generation_queue_slot_pool",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed')",
            name="ck_generation_queue_status",
        ),
        sa.CheckConstraint(
            "priority >= 0",
            name="ck_generation_queue_priority",
        ),
        sa.CheckConstraint(
            "("
            "status = 'queued' AND claimed_at IS NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL"
            ") OR ("
            "status = 'claimed' AND claimed_at IS NOT NULL "
            "AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL"
            ")",
            name="ck_generation_queue_lease_fence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_generation_queue_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "job_id",
            name="pk_generation_queue",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_generation_queue_claim",
        "generation_queue",
        [
            "worker_pool_ref",
            "slot_pool",
            "status",
            "available_at",
            "priority",
            "enqueued_at",
        ],
        schema="platform",
    )

    op.create_table(
        "generation_slots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("worker_pool_ref", sa.String(length=128), nullable=False),
        sa.Column("slot_pool", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner_key", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("claimed_tenant_id", sa.String(length=64), nullable=True),
        sa.Column("claimed_job_id", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "slot_pool IN ('generation', 'mp4_export')",
            name="ck_generation_slots_slot_pool",
        ),
        sa.CheckConstraint(
            "("
            "scope = 'global' AND tenant_id IS NULL AND owner_key = 'shared'"
            ") OR ("
            "scope = 'tenant' AND tenant_id IS NOT NULL "
            "AND owner_key = tenant_id"
            ")",
            name="ck_generation_slots_scope_owner",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_generation_slots_ordinal",
        ),
        sa.CheckConstraint(
            "("
            "claimed_tenant_id IS NULL AND claimed_job_id IS NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL"
            ") OR ("
            "claimed_tenant_id IS NOT NULL AND claimed_job_id IS NOT NULL "
            "AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL"
            ")",
            name="ck_generation_slots_claim_fence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_generation_slots_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["claimed_tenant_id", "claimed_job_id"],
            [
                "platform.generation_queue.tenant_id",
                "platform.generation_queue.job_id",
            ],
            name="fk_generation_slots_claimed_job_generation_queue",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generation_slots"),
        sa.UniqueConstraint(
            "worker_pool_ref",
            "slot_pool",
            "scope",
            "owner_key",
            "ordinal",
            name="uq_generation_slots_worker_pool_scope_owner_ordinal",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_generation_slots_available",
        "generation_slots",
        [
            "worker_pool_ref",
            "slot_pool",
            "scope",
            "owner_key",
            "claimed_job_id",
        ],
        schema="platform",
    )

    op.create_table(
        "tenant_scheduler_state",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("worker_pool_ref", sa.String(length=128), nullable=False),
        sa.Column("slot_pool", sa.String(length=32), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "slot_pool IN ('generation', 'mp4_export')",
            name="ck_tenant_scheduler_state_slot_pool",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_tenant_scheduler_state_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "worker_pool_ref",
            "slot_pool",
            name="pk_tenant_scheduler_state",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tenant_scheduler_state_fairness",
        "tenant_scheduler_state",
        ["worker_pool_ref", "slot_pool", "last_dispatched_at"],
        schema="platform",
    )


def _upgrade_tenant() -> None:
    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("job_kind", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("export_format", sa.String(length=32), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'created'"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("quota_units", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("classroom_draft_id", sa.String(length=64), nullable=True),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("data_plane_route_id", sa.String(length=63), nullable=False),
        sa.Column("provider_profile_id", sa.String(length=63), nullable=False),
        sa.Column("worker_pool_ref", sa.String(length=128), nullable=False),
        sa.Column("queue_ref", sa.String(length=128), nullable=False),
        sa.Column("request_payload", sa.Text(), nullable=False),
        sa.Column(
            "progress_percent",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("waiting_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("result_ref", sa.String(length=512), nullable=True),
        sa.Column("artifact_manifest_ref", sa.String(length=512), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "job_kind IN ('generation', 'export')",
            name="ck_generation_jobs_job_kind",
        ),
        sa.CheckConstraint(
            "(job_kind = 'generation' AND phase = 'outline' "
            "AND export_format IS NULL AND status IN ("
            "'created', 'quota_reserved', 'queued', 'generating_outline', "
            "'awaiting_confirmation', 'failed', 'canceled'"
            ")) OR ("
            "job_kind = 'generation' AND phase = 'content' "
            "AND export_format IS NULL AND status IN ("
            "'created', 'quota_reserved', 'queued', 'generating_content', "
            "'validating', 'materializing', 'succeeded', 'failed', 'canceled'"
            ")) OR ("
            "job_kind = 'export' AND phase = 'export' "
            "AND export_format IN ('classroom_zip', 'pptx', 'offline_html', 'mp4') "
            "AND status IN ("
            "'created', 'quota_reserved', 'queued', 'exporting', 'validating', "
            "'materializing', 'succeeded', 'failed', 'canceled'"
            "))",
            name="ck_generation_jobs_kind_phase_format_status",
        ),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_generation_jobs_progress_percent",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'class', 'tenant')",
            name="ck_generation_jobs_visibility",
        ),
        sa.CheckConstraint(
            "priority >= 0",
            name="ck_generation_jobs_priority",
        ),
        sa.CheckConstraint(
            "quota_units > 0",
            name="ck_generation_jobs_quota_units",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="ck_generation_jobs_attempts",
        ),
        sa.CheckConstraint(
            "("
            "status IN ("
            "'generating_outline', 'generating_content', 'exporting', "
            "'validating', 'materializing'"
            ") AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL"
            ") OR ("
            "status NOT IN ("
            "'generating_outline', 'generating_content', 'exporting', "
            "'validating', 'materializing'"
            ") AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL"
            ")",
            name="ck_generation_jobs_status_lease_fence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_generation_jobs_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generation_jobs"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            name="uq_generation_jobs_tenant_request",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_generation_jobs_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_generation_jobs_id_tenant",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_generation_jobs_status_available",
        "generation_jobs",
        ["status", "next_attempt_at"],
        schema="tenant",
    )

    op.create_table(
        "quota_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entry_type IN ('grant', 'reserve', 'release', 'settle')",
            name="ck_quota_ledger_entry_type",
        ),
        sa.CheckConstraint(
            "("
            "entry_type = 'grant' AND job_id IS NULL AND units > 0"
            ") OR ("
            "entry_type = 'reserve' AND job_id IS NOT NULL AND units < 0"
            ") OR ("
            "entry_type = 'release' AND job_id IS NOT NULL AND units > 0"
            ") OR ("
            "entry_type = 'settle' AND job_id IS NOT NULL AND units = 0"
            ")",
            name="ck_quota_ledger_entry_units",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            [
                "tenant.generation_jobs.id",
                "tenant.generation_jobs.tenant_id",
            ],
            name="fk_quota_ledger_job_tenant_generation_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quota_ledger"),
        sa.UniqueConstraint(
            "job_id",
            "entry_type",
            name="uq_quota_ledger_job_entry",
        ),
        schema="tenant",
    )
    op.create_index(
        "ix_quota_ledger_tenant_created",
        "quota_ledger",
        ["tenant_id", "created_at"],
        schema="tenant",
    )
    _sync_tenant_schema_revision("20260730_0004", "20260730_0005")


def upgrade() -> None:
    if _migration_scope() == "platform":
        _upgrade_platform()
    else:
        _upgrade_tenant()


def _downgrade_platform() -> None:
    op.drop_index(
        "ix_tenant_scheduler_state_fairness",
        table_name="tenant_scheduler_state",
        schema="platform",
    )
    op.drop_table("tenant_scheduler_state", schema="platform")
    op.drop_index(
        "ix_generation_slots_available",
        table_name="generation_slots",
        schema="platform",
    )
    op.drop_table("generation_slots", schema="platform")
    op.drop_index(
        "ix_generation_queue_claim",
        table_name="generation_queue",
        schema="platform",
    )
    op.drop_table("generation_queue", schema="platform")
    op.drop_index(
        "ix_outbox_messages_undelivered",
        table_name="outbox_messages",
        schema="platform",
    )
    op.drop_table("outbox_messages", schema="platform")
    op.execute(
        sa.text("DELETE FROM platform.tenant_provisioning_jobs WHERE operation = 'upgrade_schema'")
    )
    op.drop_constraint(
        "uq_tenant_provisioning_jobs_upgrade_target",
        "tenant_provisioning_jobs",
        type_="unique",
        schema="platform",
    )
    op.drop_constraint(
        "ck_tenant_provisioning_jobs_status_lease_fence",
        "tenant_provisioning_jobs",
        type_="check",
        schema="platform",
    )
    op.drop_constraint(
        "ck_tenant_provisioning_jobs_attempts",
        "tenant_provisioning_jobs",
        type_="check",
        schema="platform",
    )
    op.drop_constraint(
        "ck_tenant_provisioning_jobs_operation_target",
        "tenant_provisioning_jobs",
        type_="check",
        schema="platform",
    )
    op.drop_constraint(
        "ck_tenant_provisioning_jobs_operation",
        "tenant_provisioning_jobs",
        type_="check",
        schema="platform",
    )
    op.drop_column(
        "tenant_provisioning_jobs",
        "target_revision",
        schema="platform",
    )


def _downgrade_tenant() -> None:
    _sync_tenant_schema_revision("20260730_0005", "20260730_0004")
    op.drop_index(
        "ix_quota_ledger_tenant_created",
        table_name="quota_ledger",
        schema="tenant",
    )
    op.drop_table("quota_ledger", schema="tenant")
    op.drop_index(
        "ix_generation_jobs_status_available",
        table_name="generation_jobs",
        schema="tenant",
    )
    op.drop_table("generation_jobs", schema="tenant")


def downgrade() -> None:
    if _migration_scope() == "platform":
        _downgrade_platform()
    else:
        _downgrade_tenant()
