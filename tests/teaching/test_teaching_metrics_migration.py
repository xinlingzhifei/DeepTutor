from __future__ import annotations

from importlib import import_module
from pathlib import Path

from alembic.util import CommandError
import pytest
from sqlalchemy.dialects import postgresql


def test_teaching_metrics_migration_is_declared_head_and_dual_scope() -> None:
    from deeptutor.teaching.migrations.runner import TEACHING_MIGRATION_HEAD_REVISION

    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260825_0019_teaching_metric_rollups.py"
    )

    assert TEACHING_MIGRATION_HEAD_REVISION == "20260825_0019"
    assert migration.is_file()
    source = migration.read_text(encoding="utf-8")
    assert 'revision: str = "20260825_0019"' in source
    assert 'down_revision: str | None = "20260824_0018"' in source
    assert 'if _migration_scope() == "platform"' in source
    assert "_upgrade_platform()" in source
    assert "_upgrade_tenant()" in source
    assert "_downgrade_tenant()" in source


def test_tenant_metrics_migration_backfills_only_current_nonterminal_backlog() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260825_0019_teaching_metric_rollups.py"
    ).read_text(encoding="utf-8")

    assert "teaching_learning_projection_backlog" in migration
    assert "learning_projection_queue" in migration
    assert "learning_events" in migration
    assert "q.status IN ('pending', 'running', 'failed')" in migration
    assert "events.received_at" in migration
    upgrade = migration.split("def _upgrade_tenant()", 1)[1].split("def _downgrade_tenant()", 1)[0]
    assert upgrade.index("INSERT INTO platform.teaching_learning_projection_backlog") < (
        upgrade.index("_update_tenant_state(")
    )
    assert "INSERT INTO platform.teaching_metric_counter_rollups" not in migration
    assert "INSERT INTO platform.teaching_metric_histogram_rollups" not in migration


def test_tenant_metrics_migration_serializes_legacy_queue_writers_before_backfill(
    monkeypatch,
) -> None:
    migration = import_module(
        "deeptutor.teaching.migrations.versions.20260825_0019_teaching_metric_rollups"
    )
    statements: list[str] = []

    class Result:
        rowcount = 1

        def mappings(self):
            return self

        def one_or_none(self):
            return {
                "tenant_id": "tenant-a",
                "revision": "20260824_0018",
                "status": "active",
            }

    class Connection:
        dialect = postgresql.dialect()

        def execute(self, statement, _parameters=None):
            statements.append(str(statement))
            return Result()

    connection = Connection()
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration, "_tenant_schema", lambda: "tenant_0123456789abcdef")

    migration._upgrade_tenant()

    sql = "\n".join(statements)
    lock_index = sql.index(
        "LOCK TABLE tenant_0123456789abcdef.learning_projection_queue IN SHARE ROW EXCLUSIVE MODE"
    )
    function_index = sql.index("CREATE OR REPLACE FUNCTION")
    trigger_index = sql.index("CREATE CONSTRAINT TRIGGER")
    backfill_index = sql.rindex("INSERT INTO platform.teaching_learning_projection_backlog")
    ledger_index = sql.index("UPDATE platform.tenant_schema_states")

    assert lock_index < function_index < trigger_index < backfill_index < ledger_index
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "AFTER INSERT OR UPDATE OR DELETE" in sql
    assert "NEW.status IN ('pending', 'running', 'failed')" in sql
    assert "DELETE FROM platform.teaching_learning_projection_backlog" in sql
    assert "TG_TABLE_SCHEMA" in sql
    assert "FROM platform.tenant_schema_states" in sql
    assert "OLD.tenant_id IS DISTINCT FROM NEW.tenant_id" in sql
    assert "OLD.event_id IS DISTINCT FROM NEW.event_id" in sql
    assert "learning projection queue identity is immutable" in sql


def test_tenant_metrics_downgrade_removes_mirror_before_ledger_rollback() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260825_0019_teaching_metric_rollups.py"
    ).read_text(encoding="utf-8")
    downgrade = migration.split("def _downgrade_tenant()", 1)[1]

    assert "IN ACCESS EXCLUSIVE MODE" in downgrade
    assert (
        downgrade.index("LOCK TABLE")
        < downgrade.index("DROP TRIGGER IF EXISTS teaching_projection_backlog_sync")
        < downgrade.index("DROP FUNCTION IF EXISTS")
        < downgrade.index("DELETE FROM platform.teaching_learning_projection_backlog")
        < downgrade.index("_update_tenant_state(")
    )


def test_histogram_migration_requires_empty_bins_to_have_zero_sum() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260825_0019_teaching_metric_rollups.py"
    ).read_text(encoding="utf-8")

    assert "count > 0 OR sum_seconds = 0" in migration


def test_platform_metrics_downgrade_checks_tenant_ledgers_before_dropping_tables() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260825_0019_teaching_metric_rollups.py"
    ).read_text(encoding="utf-8")
    platform_downgrade = migration.split("def _downgrade_platform()", 1)[1].split(
        "def downgrade()",
        1,
    )[0]

    assert '"LOCK TABLE platform.tenant_provisioning_jobs, "' in platform_downgrade
    assert '"platform.tenant_schema_states IN SHARE MODE"' in platform_downgrade
    assert "SELECT EXISTS" in platform_downgrade
    assert "FROM platform.tenant_schema_states" in platform_downgrade
    assert "status = 'active'" not in platform_downgrade
    assert "revision = '20260825_0019'" in platform_downgrade
    assert platform_downgrade.index("SELECT EXISTS") < platform_downgrade.index(
        'op.drop_table("teaching_learning_projection_backlog"'
    )


def test_platform_metrics_downgrade_rejects_non_active_current_ledger(
    monkeypatch,
) -> None:
    migration = import_module(
        "deeptutor.teaching.migrations.versions.20260825_0019_teaching_metric_rollups"
    )
    statements: list[str] = []
    dropped_tables: list[str] = []

    class Result:
        def __init__(self, value: bool | None) -> None:
            self._value = value

        def scalar(self) -> bool | None:
            return self._value

    class Connection:
        def execute(self, statement):
            sql = str(statement)
            statements.append(sql)
            if "FROM platform.tenant_provisioning_jobs" in sql:
                return Result(False)
            if "SELECT EXISTS" in sql:
                # Model one pending 0019 ledger: a status-filtered query misses it,
                # while a revision-only query detects it.
                return Result("status = 'active'" not in sql)
            return Result(None)

    monkeypatch.setattr(migration.op, "get_bind", Connection)
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda table_name, **_kwargs: dropped_tables.append(table_name),
    )

    with pytest.raises(CommandError, match="downgrade tenant schemas before platform metrics"):
        migration._downgrade_platform()

    assert dropped_tables == []
    assert "status = 'active'" not in " ".join(statements)


@pytest.mark.parametrize(
    ("job_status", "blocked"),
    (("pending", True), ("running", True), ("failed", True), ("completed", False)),
)
def test_platform_metrics_downgrade_blocks_unfinished_provisioning_jobs(
    monkeypatch,
    job_status: str,
    blocked: bool,
) -> None:
    migration = import_module(
        "deeptutor.teaching.migrations.versions.20260825_0019_teaching_metric_rollups"
    )
    statements: list[str] = []
    dropped_tables: list[str] = []

    class Result:
        def __init__(self, value: bool | None) -> None:
            self._value = value

        def scalar(self) -> bool | None:
            return self._value

    class Connection:
        def execute(self, statement):
            sql = str(statement)
            statements.append(sql)
            if "FROM platform.tenant_provisioning_jobs" in sql:
                if "status <> 'completed'" not in sql:
                    return Result(True)
                return Result(job_status != "completed")
            if "SELECT EXISTS" in sql:
                return Result(False)
            return Result(None)

    monkeypatch.setattr(migration.op, "get_bind", Connection)
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda table_name, **_kwargs: dropped_tables.append(table_name),
    )

    if blocked:
        with pytest.raises(
            CommandError,
            match="complete tenant provisioning jobs before platform metrics",
        ):
            migration._downgrade_platform()
        assert dropped_tables == []
    else:
        migration._downgrade_platform()
        assert dropped_tables == [
            "teaching_learning_projection_backlog",
            "teaching_metric_histogram_rollups",
            "teaching_metric_counter_rollups",
        ]

    lock_statement = statements[0]
    assert "platform.tenant_provisioning_jobs" in lock_statement
    assert "platform.tenant_schema_states" in lock_statement
    assert "SHARE MODE" in lock_statement
