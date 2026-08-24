from __future__ import annotations

import asyncio
from functools import cache
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from deeptutor.teaching.secret_permissions import secret_file_is_restricted

ROOT = Path(__file__).resolve().parents[2]


@cache
def _script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "script_name",
    (
        "init_platform_secrets.py",
        "migrate_teaching.py",
        "platform_preflight.py",
        "provision_tenant_storage.py",
    ),
)
def test_platform_scripts_start_directly_from_repository_root(
    script_name: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_secret_initializer_never_overwrites_existing_secret(tmp_path: Path) -> None:
    initialize_secret = _script("init_platform_secrets").initialize_secret

    target = tmp_path / "openmaic_service_secret"
    target.write_text("keep-me", encoding="utf-8")

    created = initialize_secret(target, bytes_count=32)

    assert created is False
    assert target.read_text(encoding="utf-8") == "keep-me"


def test_secret_initializer_never_publishes_partial_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _script("init_platform_secrets")
    target = tmp_path / "openmaic_service_secret"
    original_write = module.os.write
    writes = 0

    def interrupted_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(descriptor, payload[:3])
        raise OSError("simulated interrupted secret write")

    monkeypatch.setattr(module.os, "write", interrupted_write)

    with pytest.raises(OSError, match="simulated interrupted secret write"):
        module.initialize_secret(target, bytes_count=32)

    assert not target.exists()
    assert list(tmp_path.glob(".openmaic_service_secret.*.tmp")) == []


def test_platform_secret_initializer_creates_only_generated_secrets(
    tmp_path: Path,
    capsys,
) -> None:
    module = _script("init_platform_secrets")
    GENERATED_SECRET_SPECS = module.GENERATED_SECRET_SPECS
    initialize_platform_secrets = module.initialize_platform_secrets

    created = initialize_platform_secrets(tmp_path)

    assert {path.name for path in created} == set(GENERATED_SECRET_SPECS)
    assert not (tmp_path / "gateway_fullchain.pem").exists()
    assert not (tmp_path / "gateway_private_key.pem").exists()
    assert capsys.readouterr().out == ""
    for path in created:
        assert path.read_text(encoding="utf-8").strip()
        assert secret_file_is_restricted(path)
    minio_secret = (tmp_path / "minio_bootstrap_secret_key").read_text(encoding="utf-8").strip()
    assert 8 <= len(minio_secret) <= 40


def test_database_role_bootstrap_keeps_passwords_out_of_sql() -> None:
    build_database_role_statements = _script("migrate_teaching").build_database_role_statements

    app_password = "APP_PASSWORD_SENTINEL"
    migration_password = "MIGRATION_PASSWORD_SENTINEL"
    statements = build_database_role_statements(
        app_password=app_password,
        migration_password=migration_password,
    )

    rendered = "\n".join(statement.sql for statement in statements)
    assert app_password not in rendered
    assert migration_password not in rendered
    assert "yfeistai_app" in rendered
    assert "yfeistai_migrator" in rendered
    assert all("SUPERUSER" not in statement.sql for statement in statements)
    assert any(statement.parameters.get("password") == app_password for statement in statements)
    assert any(
        statement.parameters.get("password") == migration_password for statement in statements
    )


def test_tenant_migration_failure_is_safe_and_stops_later_tenants() -> None:
    module = _script("migrate_teaching")
    TenantMigrationError = module.TenantMigrationError
    migrate_tenant_schemas = module.migrate_tenant_schemas

    calls: list[str] = []

    async def migrate(schema_name: str) -> None:
        calls.append(schema_name)
        if schema_name.endswith("2" * 16):
            raise RuntimeError("PASSWORD_SENTINEL")

    try:
        asyncio.run(
            migrate_tenant_schemas(
                (
                    ("tenant-a", "tenant_" + "1" * 16, "old-a"),
                    ("tenant-b", "tenant_" + "2" * 16, "old-b"),
                    ("tenant-c", "tenant_" + "3" * 16, "old-c"),
                ),
                migrate=migrate,
            )
        )
    except TenantMigrationError as exc:
        message = str(exc)
    else:
        raise AssertionError("tenant migration failure was not propagated")

    assert calls == ["tenant_" + "1" * 16, "tenant_" + "2" * 16]
    assert "tenant-b" in message
    assert "tenant_" + "2" * 16 in message
    assert "old-b" in message
    assert "PASSWORD_SENTINEL" not in message


def test_tenant_directory_lock_is_held_until_migrations_and_grants_finish(
    monkeypatch,
) -> None:
    module = _script("migrate_teaching")
    trace: list[str] = []
    lock_held = False
    schema_name = module.tenant_schema_name("tenant-a")

    class Result:
        def all(self):
            return (("tenant-a", schema_name, "old-a"),)

    class Connection:
        async def execute(self, statement, parameters=None):
            nonlocal lock_held
            sql = str(statement)
            if "pg_advisory_lock(" in sql:
                lock_held = True
                trace.append("lock")
            elif sql.startswith("SELECT tenants.id"):
                assert lock_held is True
                trace.append("snapshot")
                return Result()
            return Result()

        async def scalar(self, statement):
            nonlocal lock_held
            assert "pg_advisory_unlock(" in str(statement)
            assert lock_held is True
            lock_held = False
            trace.append("unlock")
            return True

        async def commit(self):
            return None

        async def rollback(self):
            return None

    connection = Connection()

    class Context:
        async def __aenter__(self):
            trace.append("connect")
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            assert lock_held is False
            trace.append("disconnect")

    class Engine:
        def connect(self):
            return Context()

    async def migrate_platform() -> None:
        assert lock_held is True
        trace.append("migrate:platform")

    async def migrate(schema_name: str) -> None:
        assert lock_held is True
        trace.append(f"migrate:{schema_name}")

    async def grant_app_access(active_connection, schemas) -> None:
        assert lock_held is True
        assert active_connection is connection
        trace.append(f"grant:{','.join(schemas)}")

    monkeypatch.setattr(module, "_grant_app_access_on_connection", grant_app_access)

    asyncio.run(
        module.migrate_locked_tenant_directory(
            Engine(),
            migrate_platform=migrate_platform,
            migrate=migrate,
        )
    )

    assert trace == [
        "connect",
        "lock",
        "migrate:platform",
        "snapshot",
        f"migrate:{schema_name}",
        f"grant:{schema_name}",
        "unlock",
        "disconnect",
    ]


def test_platform_migration_cancellation_waits_for_real_thread_and_unlock(
    monkeypatch,
) -> None:
    from deeptutor.teaching.migrations import facade as migration_facade

    module = _script("migrate_teaching")
    migration_started = threading.Event()
    release_migration = threading.Event()

    class AdminEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    class Connection:
        def __init__(self) -> None:
            self.locked = False
            self.unlock_started = asyncio.Event()
            self.allow_unlock = asyncio.Event()

        async def execute(self, statement, parameters=None):
            assert "pg_advisory_lock(" in str(statement)
            self.locked = True

        async def scalar(self, statement):
            assert "pg_advisory_unlock(" in str(statement)
            self.unlock_started.set()
            await self.allow_unlock.wait()
            self.locked = False
            return True

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

        async def invalidate(self) -> None:
            self.locked = False

    connection = Connection()

    class ConnectionContext:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            assert connection.locked is False

    class MigrationEngine(AdminEngine):
        def connect(self):
            return ConnectionContext()

    admin_engine = AdminEngine()
    migration_engine = MigrationEngine()
    engines = iter((admin_engine, migration_engine))

    async def bootstrap(*_args, **_kwargs) -> None:
        return None

    def migrate(**kwargs) -> None:
        assert kwargs["scope"] == "platform"
        migration_started.set()
        if not release_migration.wait(timeout=2):
            raise AssertionError("test did not release the platform migration")

    monkeypatch.setattr(module, "_read_secret", lambda *_args: "secret")
    monkeypatch.setattr(
        module,
        "_database_url",
        lambda _settings, *, user, password: f"postgresql+asyncpg://{user}@db/test",
    )
    monkeypatch.setattr(module, "create_async_engine", lambda _url: next(engines))
    monkeypatch.setattr(module, "_execute_role_bootstrap", bootstrap)
    monkeypatch.setattr(migration_facade, "_run_migration_unlocked", migrate)

    async def exercise() -> None:
        task = asyncio.create_task(module.migrate_platform_and_tenants(object()))
        try:
            assert await asyncio.wait_for(
                asyncio.to_thread(migration_started.wait, 1),
                timeout=1,
            )
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            assert connection.unlock_started.is_set() is False
            assert task.done() is False
            assert connection.locked is True
            release_migration.set()
            await asyncio.wait_for(connection.unlock_started.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0.01)
            assert task.done() is False
            assert connection.locked is True
            connection.allow_unlock.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release_migration.set()
            connection.allow_unlock.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())

    assert admin_engine.disposed is True
    assert migration_engine.disposed is True


def test_migration_cli_routes_all_scopes_through_lock_aware_facade(
    monkeypatch,
) -> None:
    from deeptutor.teaching.migrations import cli as module

    calls: list[tuple[str, str, str | None]] = []

    async def run_lock_aware_migration(
        *,
        action: str,
        scope: str,
        tenant_schema: str | None = None,
    ) -> None:
        calls.append((action, scope, tenant_schema))

    def run_bare_migration(
        *,
        action: str,
        scope: str,
        tenant_schema: str | None = None,
    ) -> None:
        calls.append((f"bare:{action}", scope, tenant_schema))

    monkeypatch.setattr(
        module,
        "run_lock_aware_migration",
        run_lock_aware_migration,
        raising=False,
    )
    monkeypatch.setattr(module, "run_migration", run_bare_migration, raising=False)

    assert module.main(("upgrade", "--scope", "platform")) == 0
    assert (
        module.main(
            (
                "downgrade",
                "--scope",
                "tenant",
                "--tenant-schema",
                "tenant_0123456789abcdef",
            )
        )
        == 0
    )

    assert calls == [
        ("upgrade", "platform", None),
        ("downgrade", "tenant", "tenant_0123456789abcdef"),
    ]


def test_migration_cli_validates_scope_before_opening_database_engine(
    monkeypatch,
    capsys,
) -> None:
    from deeptutor.teaching.migrations import cli as module

    facade_started = False

    async def unexpected_facade(**_kwargs) -> None:
        nonlocal facade_started
        facade_started = True
        raise AssertionError("migration facade must not start for invalid scope")

    monkeypatch.setattr(module, "run_lock_aware_migration", unexpected_facade)

    with pytest.raises(SystemExit) as captured:
        module.main(("upgrade", "--scope", "tenant"))

    assert captured.value.code == 2
    assert facade_started is False
    assert capsys.readouterr().err == (
        "deeptutor-migrate: error: scope must be exactly platform or tenant\n"
    )


def test_alembic_migration_transaction_sets_database_timeouts_before_ddl(
    monkeypatch,
) -> None:
    from deeptutor.teaching.migrations import runner as module

    migration_entry = getattr(module, "_run_migrations_in_transaction", None)
    assert callable(migration_entry), "Alembic must use the timeout-aware transaction entry"

    trace: list[str] = []
    captured_sql = ""
    captured_parameters: dict[str, str] = {}

    class Transaction:
        def __enter__(self) -> None:
            trace.append("transaction-enter")

        def __exit__(self, exc_type, exc, traceback) -> None:
            trace.append("transaction-exit")

    class MigrationContext:
        def configure(self, **kwargs) -> None:
            trace.append("configure")

        def begin_transaction(self) -> Transaction:
            return Transaction()

        def run_migrations(self) -> None:
            trace.append("migration-ddl")

    class Connection:
        def execute(self, statement, parameters) -> None:
            nonlocal captured_sql, captured_parameters
            captured_sql = str(statement)
            captured_parameters = parameters
            trace.append("database-timeouts")

    monkeypatch.setattr(module, "context", MigrationContext(), raising=False)
    migration_entry(Connection(), module.MigrationScope("platform", "platform"))

    assert trace == [
        "configure",
        "transaction-enter",
        "database-timeouts",
        "migration-ddl",
        "transaction-exit",
    ]
    assert "set_config('lock_timeout', :lock_timeout, true)" in captured_sql
    assert "set_config('statement_timeout', :statement_timeout, true)" in captured_sql
    assert captured_parameters == {
        "lock_timeout": f"{module.MIGRATION_LOCK_TIMEOUT_MS}ms",
        "statement_timeout": f"{module.MIGRATION_STATEMENT_TIMEOUT_MS}ms",
    }


def test_online_migration_sets_timeouts_before_schema_and_migration_ddl(
    monkeypatch,
) -> None:
    from alembic import command, context
    import sqlalchemy.ext.asyncio as sqlalchemy_asyncio
    from sqlalchemy.schema import CreateSchema

    from deeptutor.teaching.migrations import runner

    trace: list[str] = []

    class SchemaTransaction:
        async def __aenter__(self) -> None:
            trace.append("schema-transaction-enter")

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            trace.append("schema-transaction-exit")

    class MigrationTransaction:
        def __enter__(self) -> None:
            trace.append("migration-transaction-enter")

        def __exit__(self, exc_type, exc, traceback) -> None:
            trace.append("migration-transaction-exit")

    class SyncConnection:
        def execute(self, statement, parameters) -> None:
            assert "set_config('lock_timeout'" in str(statement)
            trace.append("migration-timeouts")

    class Connection:
        def begin(self) -> SchemaTransaction:
            return SchemaTransaction()

        async def execute(self, statement, parameters=None) -> None:
            if isinstance(statement, CreateSchema):
                trace.append("create-schema")
                return
            assert "set_config('lock_timeout'" in str(statement)
            trace.append("schema-timeouts")

        async def commit(self) -> None:
            trace.append("schema-implicit-commit")

        async def run_sync(self, function, *args) -> None:
            function(SyncConnection(), *args)

    connection = Connection()

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            trace.append("connect")
            return connection

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            trace.append("disconnect")

    class Engine:
        def connect(self) -> ConnectionContext:
            return ConnectionContext()

        async def dispose(self) -> None:
            trace.append("dispose")

    class CommandOptions:
        cmd = (command.upgrade,)

    class AlembicConfig:
        cmd_opts = CommandOptions()

    monkeypatch.setattr(context, "config", AlembicConfig(), raising=False)
    monkeypatch.setattr(context, "get_x_argument", lambda: ("scope=platform",))
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(context, "configure", lambda **_kwargs: trace.append("configure"))
    monkeypatch.setattr(context, "begin_transaction", MigrationTransaction)
    monkeypatch.setattr(context, "run_migrations", lambda: trace.append("migration-ddl"))
    monkeypatch.setattr(runner, "load_migration_database_url", lambda: "postgresql://db")
    monkeypatch.setattr(
        sqlalchemy_asyncio, "create_async_engine", lambda *_args, **_kwargs: Engine()
    )

    path = ROOT / "deeptutor" / "teaching" / "migrations" / "env.py"
    spec = importlib.util.spec_from_file_location("timeout_order_migration_env", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    assert trace == [
        "connect",
        "schema-transaction-enter",
        "schema-timeouts",
        "create-schema",
        "schema-transaction-exit",
        "configure",
        "migration-transaction-enter",
        "migration-timeouts",
        "migration-ddl",
        "migration-transaction-exit",
        "disconnect",
        "dispose",
    ]


def test_tenant_directory_session_lock_releases_after_migration_failure() -> None:
    from deeptutor.teaching.tenant_directory_lock import (
        tenant_directory_session_lock,
    )

    trace: list[str] = []

    class Connection:
        async def execute(self, statement):
            assert "pg_advisory_lock(" in str(statement)
            trace.append("lock")

        async def scalar(self, statement):
            assert "pg_advisory_unlock(" in str(statement)
            trace.append("unlock")
            return True

        async def commit(self):
            trace.append("commit")

        async def rollback(self):
            trace.append("rollback")

        async def invalidate(self):
            trace.append("invalidate")

    async def exercise() -> None:
        async with tenant_directory_session_lock(Connection(), shared=False):
            trace.append("migration")
            raise RuntimeError("deterministic migration failure")

    with pytest.raises(RuntimeError, match="deterministic migration failure"):
        asyncio.run(exercise())

    assert trace == ["lock", "commit", "migration", "rollback", "unlock", "commit"]


def test_cleanup_cancellation_wins_over_body_error_and_preserves_error_chain() -> None:
    from deeptutor.teaching.tenant_directory_lock import (
        tenant_directory_session_lock,
    )

    async def exercise() -> asyncio.CancelledError:
        unlock_started = asyncio.Event()
        allow_unlock = asyncio.Event()

        class Connection:
            def __init__(self) -> None:
                self.locked = False
                self.unlock_completed = False
                self.invalidated = False

            async def execute(self, statement) -> None:
                self.locked = True

            async def scalar(self, statement) -> bool:
                unlock_started.set()
                await allow_unlock.wait()
                self.locked = False
                self.unlock_completed = True
                return True

            async def commit(self) -> None:
                return None

            async def rollback(self) -> None:
                return None

            async def invalidate(self) -> None:
                self.invalidated = True

        connection = Connection()

        async def fail_inside_lock() -> None:
            async with tenant_directory_session_lock(connection, shared=False):
                raise RuntimeError("migration body failed")

        task = asyncio.create_task(fail_inside_lock())
        await asyncio.wait_for(unlock_started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        assert connection.locked is True
        allow_unlock.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert connection.locked is False
        assert connection.unlock_completed is True
        assert connection.invalidated is True
        return captured.value

    cancellation = asyncio.run(exercise())

    assert isinstance(cancellation.__cause__, RuntimeError)
    assert str(cancellation.__cause__) == "migration body failed"


def test_tenant_directory_snapshot_derives_canonical_schema_for_missing_state() -> None:
    module = _script("migrate_teaching")

    class Result:
        def all(self):
            return (("tenant-without-state", None, None),)

    class Connection:
        async def execute(self, statement):
            return Result()

    class Context:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class Engine:
        def connect(self):
            return Context()

    rows = asyncio.run(module._tenant_rows(Engine()))

    assert rows == (
        (
            "tenant-without-state",
            module.tenant_schema_name("tenant-without-state"),
            None,
        ),
    )


def test_tenant_directory_snapshot_rejects_schema_mapping_mismatch() -> None:
    module = _script("migrate_teaching")

    class Result:
        def all(self):
            return (("tenant-with-mismatch", "tenant_0000000000000000", "old"),)

    class Connection:
        async def execute(self, statement):
            return Result()

    class Context:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class Engine:
        def connect(self):
            return Context()

    with pytest.raises(module.TenantDirectoryError, match="tenant-with-mismatch"):
        asyncio.run(module._tenant_rows(Engine()))
