"""Add durable fixed-contract teaching metric rollups and projection backlog."""

from __future__ import annotations

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa

revision: str = "20260825_0019"
down_revision: str | None = "20260824_0018"
branch_labels: str | None = None
depends_on: str | None = None


def _migration_scope() -> str:
    return context.get_x_argument(as_dictionary=True)["scope"]


def _tenant_schema() -> str:
    return context.get_x_argument(as_dictionary=True)["tenant_schema"]


def _locked_tenant_state(source_revision: str):
    state = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT tenant_id, revision, status
                FROM platform.tenant_schema_states
                WHERE schema_name = :tenant_schema
                FOR UPDATE
                """
            ),
            {"tenant_schema": _tenant_schema()},
        )
        .mappings()
        .one_or_none()
    )
    if state is not None and (state["status"] != "active" or state["revision"] != source_revision):
        raise RuntimeError("tenant schema state revision does not match migration source")
    return state


def _update_tenant_state(state, source_revision: str, target_revision: str) -> None:
    if state is None:
        return
    result = op.get_bind().execute(
        sa.text(
            """
            UPDATE platform.tenant_schema_states
            SET revision = :target_revision,
                verified_at = now(),
                updated_at = now()
            WHERE tenant_id = :tenant_id
              AND schema_name = :tenant_schema
              AND status = 'active'
              AND revision = :source_revision
            """
        ),
        {
            "tenant_id": state["tenant_id"],
            "tenant_schema": _tenant_schema(),
            "source_revision": source_revision,
            "target_revision": target_revision,
        },
    )
    if result.rowcount != 1:
        raise RuntimeError("tenant schema revision update was lost")


def _upgrade_platform() -> None:
    op.create_table(
        "teaching_metric_counter_rollups",
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("shard", sa.SmallInteger(), nullable=False),
        sa.Column(
            "total",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "shard BETWEEN 0 AND 15",
            name="ck_teaching_metric_counter_rollups_shard",
        ),
        sa.CheckConstraint(
            "total >= 0",
            name="ck_teaching_metric_counter_rollups_total",
        ),
        sa.CheckConstraint(
            "(metric = 'generation_jobs_total' "
            "AND category IN ('queued', 'running', 'completed', 'failed', 'canceled')) "
            "OR (metric = 'generation_retries_total' "
            "AND category IN "
            "('timeout', 'unavailable', 'lease_lost', 'rate_limited', 'unknown')) "
            "OR (metric = 'quota_units_total' "
            "AND category IN ('reserved', 'consumed', 'released')) "
            "OR (metric = 'learning_events_total' "
            "AND category IN "
            "('classroom.started', 'scene.completed', 'quiz.graded', 'hint.used', "
            "'pbl.milestone_completed', 'classroom.completed')) "
            "OR (metric = 'artifact_validation_failures_total' "
            "AND category IN "
            "('schema_invalid', 'receipt_mismatch', 'hash_mismatch', 'size_mismatch', "
            "'missing_artifact', 'unknown'))",
            name="ck_teaching_metric_counter_rollups_metric_category",
        ),
        sa.PrimaryKeyConstraint(
            "metric",
            "category",
            "shard",
            name="pk_teaching_metric_counter_rollups",
        ),
        schema="platform",
    )
    op.create_table(
        "teaching_metric_histogram_rollups",
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("shard", sa.SmallInteger(), nullable=False),
        sa.Column(
            "count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "sum_seconds",
            sa.Float(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "shard BETWEEN 0 AND 15",
            name="ck_teaching_metric_histogram_rollups_shard",
        ),
        sa.CheckConstraint(
            "count >= 0",
            name="ck_teaching_metric_histogram_rollups_count",
        ),
        sa.CheckConstraint(
            "sum_seconds >= 0 AND sum_seconds < 'Infinity'::double precision",
            name="ck_teaching_metric_histogram_rollups_sum_seconds",
        ),
        sa.CheckConstraint(
            "count > 0 OR sum_seconds = 0",
            name="ck_teaching_metric_histogram_rollups_count_sum",
        ),
        sa.CheckConstraint(
            "(metric = 'generation_queue_seconds' AND category = '' "
            "AND bucket IN "
            "('0.1', '0.5', '1', '2', '5', '10', '30', '60', '120', '300', '+Inf')) "
            "OR (metric = 'generation_stage_seconds' "
            "AND category IN ('outline', 'content', 'export') "
            "AND bucket IN "
            "('0.5', '1', '2', '5', '10', '30', '60', '120', '300', '900', "
            "'1800', '+Inf'))",
            name="ck_teaching_metric_histogram_rollups_metric_category_bucket",
        ),
        sa.PrimaryKeyConstraint(
            "metric",
            "category",
            "bucket",
            "shard",
            name="pk_teaching_metric_histogram_rollups",
        ),
        schema="platform",
    )
    op.create_table(
        "teaching_learning_projection_backlog",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["platform.tenants.id"],
            name="fk_teaching_learning_projection_backlog_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "event_id",
            name="pk_teaching_learning_projection_backlog",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_teaching_projection_backlog_received_at",
        "teaching_learning_projection_backlog",
        ["received_at"],
        schema="platform",
    )


def _upgrade_tenant() -> None:
    state = _locked_tenant_state("20260824_0018")
    if state is not None:
        connection = op.get_bind()
        quoted_schema = connection.dialect.identifier_preparer.quote_schema(_tenant_schema())
        connection.execute(
            sa.text(
                f"LOCK TABLE {quoted_schema}.learning_projection_queue IN SHARE ROW EXCLUSIVE MODE"
            )
        )
        connection.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION
                    {quoted_schema}.sync_teaching_projection_backlog()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                DECLARE
                    authoritative_tenant_id text;
                BEGIN
                    SELECT state.tenant_id
                    INTO authoritative_tenant_id
                    FROM platform.tenant_schema_states AS state
                    WHERE state.schema_name = TG_TABLE_SCHEMA
                      AND state.status = 'active';

                    IF TG_OP = 'DELETE' THEN
                        IF OLD.tenant_id IS DISTINCT FROM authoritative_tenant_id THEN
                            RAISE EXCEPTION
                                'learning projection queue tenant binding is invalid';
                        END IF;
                        DELETE FROM platform.teaching_learning_projection_backlog
                        WHERE tenant_id = OLD.tenant_id
                          AND event_id = OLD.event_id;
                        RETURN NULL;
                    END IF;

                    IF NEW.tenant_id IS DISTINCT FROM authoritative_tenant_id THEN
                        RAISE EXCEPTION
                            'learning projection queue tenant binding is invalid';
                    END IF;
                    IF TG_OP = 'UPDATE' AND (
                        OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
                        OR OLD.event_id IS DISTINCT FROM NEW.event_id
                    ) THEN
                        RAISE EXCEPTION
                            'learning projection queue identity is immutable';
                    END IF;
                    IF TG_OP = 'UPDATE'
                       AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                        RETURN NULL;
                    END IF;

                    IF NEW.status IN ('pending', 'running', 'failed') THEN
                        INSERT INTO platform.teaching_learning_projection_backlog (
                            tenant_id,
                            event_id,
                            received_at
                        )
                        SELECT
                            NEW.tenant_id,
                            NEW.event_id,
                            events.received_at
                        FROM {quoted_schema}.learning_events AS events
                        WHERE events.event_id = NEW.event_id
                          AND events.tenant_id = NEW.tenant_id
                        ON CONFLICT (tenant_id, event_id) DO UPDATE
                        SET received_at = EXCLUDED.received_at;
                    ELSE
                        DELETE FROM platform.teaching_learning_projection_backlog
                        WHERE tenant_id = NEW.tenant_id
                          AND event_id = NEW.event_id;
                    END IF;
                    RETURN NULL;
                END;
                $function$
                """
            )
        )
        connection.execute(
            sa.text(
                f"""
                CREATE CONSTRAINT TRIGGER teaching_projection_backlog_sync
                AFTER INSERT OR UPDATE OR DELETE
                ON {quoted_schema}.learning_projection_queue
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW
                EXECUTE FUNCTION {quoted_schema}.sync_teaching_projection_backlog()
                """
            )
        )
        connection.execute(
            sa.text(
                f"""
                INSERT INTO platform.teaching_learning_projection_backlog (
                    tenant_id,
                    event_id,
                    received_at
                )
                SELECT
                    :tenant_id,
                    q.event_id,
                    events.received_at
                FROM {quoted_schema}.learning_projection_queue AS q
                JOIN {quoted_schema}.learning_events AS events
                  ON events.event_id = q.event_id
                 AND events.tenant_id = :tenant_id
                WHERE q.tenant_id = :tenant_id
                  AND q.status IN ('pending', 'running', 'failed')
                ON CONFLICT (tenant_id, event_id) DO UPDATE
                SET received_at = EXCLUDED.received_at
                """
            ),
            {"tenant_id": state["tenant_id"]},
        )
    _update_tenant_state(state, "20260824_0018", "20260825_0019")


def _downgrade_tenant() -> None:
    state = _locked_tenant_state("20260825_0019")
    if state is not None:
        connection = op.get_bind()
        quoted_schema = connection.dialect.identifier_preparer.quote_schema(_tenant_schema())
        connection.execute(
            sa.text(
                f"LOCK TABLE {quoted_schema}.learning_projection_queue IN ACCESS EXCLUSIVE MODE"
            )
        )
        connection.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS teaching_projection_backlog_sync "
                f"ON {quoted_schema}.learning_projection_queue"
            )
        )
        connection.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {quoted_schema}.sync_teaching_projection_backlog()")
        )
        connection.execute(
            sa.text(
                """
                DELETE FROM platform.teaching_learning_projection_backlog
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": state["tenant_id"]},
        )
    _update_tenant_state(state, "20260825_0019", "20260824_0018")


def _downgrade_platform() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "LOCK TABLE platform.tenant_provisioning_jobs, "
            "platform.tenant_schema_states IN SHARE MODE"
        )
    )
    has_unfinished_job = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM platform.tenant_provisioning_jobs
                WHERE status <> 'completed'
            )
            """
        )
    ).scalar()
    if has_unfinished_job:
        raise CommandError("complete tenant provisioning jobs before platform metrics")
    has_current_tenant = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM platform.tenant_schema_states
                WHERE revision = '20260825_0019'
            )
            """
        )
    ).scalar()
    if has_current_tenant:
        raise CommandError("downgrade tenant schemas before platform metrics")
    op.drop_table("teaching_learning_projection_backlog", schema="platform")
    op.drop_table("teaching_metric_histogram_rollups", schema="platform")
    op.drop_table("teaching_metric_counter_rollups", schema="platform")


def upgrade() -> None:
    if _migration_scope() == "platform":
        _upgrade_platform()
    else:
        # Counter and histogram history cannot be reconstructed safely. Revision
        # 0019 deliberately starts those durable rollups from an empty baseline.
        _upgrade_tenant()


def downgrade() -> None:
    if _migration_scope() == "platform":
        _downgrade_platform()
    else:
        _downgrade_tenant()
