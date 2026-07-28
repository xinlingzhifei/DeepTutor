from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from deeptutor.teaching.models import PlatformBase, TenantBase
from deeptutor.teaching.schema_names import tenant_schema_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]
HEAD_REVISION = "20260728_0001"


@dataclass(frozen=True)
class MigrationDatabase:
    url: str
    password: str
    environment: dict[str, str]


@pytest.fixture(scope="module")
def migration_database(tmp_path_factory):
    password = "MIGRATION_PASSWORD_SENTINEL_8d7f2"
    with PostgresContainer(
        "postgres:16-alpine",
        username="migration_user",
        password=password,
        dbname="teaching",
    ) as postgres:
        sync_url = make_url(postgres.get_connection_url())
        async_url = sync_url.set(drivername="postgresql+asyncpg").render_as_string(
            hide_password=False
        )

        runtime_home = tmp_path_factory.mktemp("teaching-runtime")
        settings_dir = runtime_home / "data" / "user" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "platform.json").write_text(
            json.dumps({"enabled": True}),
            encoding="utf-8",
        )

        environment = os.environ.copy()
        environment["DEEPTUTOR_HOME"] = str(runtime_home)
        environment["DEEPTUTOR_PLATFORM_DATABASE_URL"] = async_url
        yield MigrationDatabase(
            url=async_url,
            password=password,
            environment=environment,
        )


def _run_alembic(
    database: MigrationDatabase,
    *x_arguments: str,
    action: str = "upgrade",
    revision: str | None = "head",
    cwd: Path = PROJECT_ROOT,
    config_file: Path | None = None,
    action_arguments: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "alembic"]
    if config_file is not None:
        command.extend(("-c", str(config_file)))
    for argument in x_arguments:
        command.extend(("-x", argument))
    command.append(action)
    if revision is not None:
        command.append(revision)
    command.extend(action_arguments)
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=database.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Alembic subprocess timed out after 120 seconds")


def _assert_secret_safe_output(
    database: MigrationDatabase,
    completed: subprocess.CompletedProcess[str],
) -> str:
    output = f"{completed.stdout}\n{completed.stderr}"
    if database.password in output:
        pytest.fail("migration process exposed the database password")
    return output.replace(database.url, "<redacted-database-url>")


def _assert_migration_succeeded(
    database: MigrationDatabase,
    completed: subprocess.CompletedProcess[str],
) -> None:
    safe_output = _assert_secret_safe_output(database, completed)
    assert completed.returncode == 0, safe_output


async def _inspect_database(
    database_url: str,
    tenant_schemas: tuple[str, str],
) -> tuple[
    dict[str, set[str]],
    dict[tuple[str, str], set[str]],
    dict[str, list[str]],
    str,
]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            table_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT table_schema, table_name
                        FROM information_schema.tables
                        WHERE table_type = 'BASE TABLE'
                          AND table_schema IN (
                              'platform',
                              'tenant',
                              :tenant_a,
                              :tenant_b
                          )
                        """
                    ),
                    {
                        "tenant_a": tenant_schemas[0],
                        "tenant_b": tenant_schemas[1],
                    },
                )
            ).all()
            tables_by_schema: dict[str, set[str]] = {}
            for schema, table in table_rows:
                tables_by_schema.setdefault(schema, set()).add(table)

            column_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT table_schema, table_name, column_name
                        FROM information_schema.columns
                        WHERE table_schema IN (
                            'platform',
                            :tenant_a,
                            :tenant_b
                        )
                        """
                    ),
                    {
                        "tenant_a": tenant_schemas[0],
                        "tenant_b": tenant_schemas[1],
                    },
                )
            ).all()
            columns_by_table: dict[tuple[str, str], set[str]] = {}
            for schema, table, column in column_rows:
                columns_by_table.setdefault((schema, table), set()).add(column)

            versions: dict[str, list[str]] = {}
            for schema in ("platform", *tenant_schemas):
                version_rows = (
                    await connection.execute(
                        text(f'SELECT version_num FROM "{schema}".alembic_version')
                    )
                ).scalars()
                versions[schema] = list(version_rows)

            server_version = await connection.scalar(
                text("SELECT current_setting('server_version')")
            )
    finally:
        await engine.dispose()

    assert isinstance(server_version, str)
    return tables_by_schema, columns_by_table, versions, server_version


def _expected_columns(metadata, schema: str) -> dict[tuple[str, str], set[str]]:
    return {
        (schema, table.name): set(table.columns.keys())
        for table in metadata.tables.values()
    }


async def _table_names(database_url: str, schema: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_type = 'BASE TABLE'
                          AND table_schema = :schema
                        """
                    ),
                    {"schema": schema},
                )
            ).scalars()
            return set(rows)
    finally:
        await engine.dispose()


def test_platform_engine_survives_independent_event_loops(
    migration_database,
    monkeypatch,
):
    from pydantic import SecretStr

    from deeptutor.services.config import PlatformSettings
    from deeptutor.teaching import database as database_module

    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr(migration_database.url),
    )
    monkeypatch.setattr(
        database_module,
        "load_platform_settings",
        lambda: settings,
    )

    async def select_one():
        async with database_module.platform_session() as session:
            return await session.scalar(text("SELECT 1"))

    try:
        assert asyncio.run(select_one()) == 1
        original_engine = database_module.get_platform_engine()
        assert asyncio.run(select_one()) == 1

        asyncio.run(database_module.dispose_platform_engine())
        rebuilt_engine = database_module.get_platform_engine()
        assert rebuilt_engine is not original_engine
        assert asyncio.run(select_one()) == 1
    finally:
        dispose = getattr(database_module, "dispose_platform_engine", None)
        if dispose is not None:
            asyncio.run(dispose())


def test_migration_runs_from_outside_repository(
    migration_database,
    tmp_path,
):
    tenant_schema = tenant_schema_name("external/alembic/cwd")
    assert PROJECT_ROOT not in tmp_path.resolve().parents
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == set()

    completed = _run_alembic(
        migration_database,
        "scope=tenant",
        f"tenant_schema={tenant_schema}",
        cwd=tmp_path,
        config_file=PROJECT_ROOT / "alembic.ini",
    )

    _assert_migration_succeeded(migration_database, completed)
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == {
        "alembic_version",
        "classes",
        "courses",
        "enrollments",
    }


@pytest.mark.parametrize(
    "x_arguments",
    [
        ("scope=platform",),
        (
            "scope=tenant",
            f"tenant_schema={tenant_schema_name('blocked/check')}",
        ),
    ],
)
def test_alembic_check_is_explicitly_unsupported(
    migration_database,
    x_arguments,
):
    completed = _run_alembic(
        migration_database,
        *x_arguments,
        action="check",
        revision=None,
    )

    safe_output = _assert_secret_safe_output(migration_database, completed)
    assert completed.returncode != 0, safe_output
    assert (
        "teaching migrations support only upgrade and downgrade"
        in safe_output
    )


def test_revision_autogenerate_is_unsupported_without_writing_a_file(
    migration_database,
):
    versions_dir = (
        PROJECT_ROOT / "deeptutor" / "teaching" / "migrations" / "versions"
    )
    files_before = {path.name for path in versions_dir.iterdir()}

    completed = _run_alembic(
        migration_database,
        "scope=platform",
        action="revision",
        revision=None,
        action_arguments=("--autogenerate", "-m", "must-not-be-created"),
    )

    safe_output = _assert_secret_safe_output(migration_database, completed)
    assert completed.returncode != 0, safe_output
    assert (
        "teaching migrations support only upgrade and downgrade"
        in safe_output
    )
    assert {path.name for path in versions_dir.iterdir()} == files_before


def test_foundation_migration_is_isolated_and_repeatable(migration_database):
    tenant_a = tenant_schema_name("tenant/a")
    tenant_b = tenant_schema_name("tenant/b")
    assert tenant_a != tenant_b

    for arguments in (
        ("scope=platform",),
        ("scope=tenant", f"tenant_schema={tenant_a}"),
        ("scope=tenant", f"tenant_schema={tenant_b}"),
        ("scope=platform",),
        ("scope=tenant", f"tenant_schema={tenant_a}"),
    ):
        _assert_migration_succeeded(
            migration_database,
            _run_alembic(migration_database, *arguments),
        )

    tables_by_schema, columns_by_table, versions, server_version = asyncio.run(
        _inspect_database(migration_database.url, (tenant_a, tenant_b))
    )

    assert tables_by_schema["platform"] == {
        "alembic_version",
        "audit_log",
        "data_plane_routes",
        "role_grants",
        "tenant_memberships",
        "tenant_provisioning_jobs",
        "tenant_storage_credentials",
        "tenants",
    }
    assert tables_by_schema[tenant_a] == {
        "alembic_version",
        "classes",
        "courses",
        "enrollments",
    }
    assert tables_by_schema[tenant_b] == {
        "alembic_version",
        "classes",
        "courses",
        "enrollments",
    }
    assert "tenant" not in tables_by_schema
    assert {
        key: columns
        for key, columns in columns_by_table.items()
        if key[0] == "platform" and key[1] != "alembic_version"
    } == _expected_columns(PlatformBase.metadata, "platform")
    expected_tenant_columns = _expected_columns(TenantBase.metadata, tenant_a)
    assert {
        key: columns
        for key, columns in columns_by_table.items()
        if key[0] == tenant_a and key[1] != "alembic_version"
    } == expected_tenant_columns
    assert {
        (tenant_a, table): columns
        for (schema, table), columns in columns_by_table.items()
        if schema == tenant_b and table != "alembic_version"
    } == expected_tenant_columns
    assert versions == {
        "platform": [HEAD_REVISION],
        tenant_a: [HEAD_REVISION],
        tenant_b: [HEAD_REVISION],
    }
    assert server_version.startswith("16.")

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=tenant",
            f"tenant_schema={tenant_b}",
            action="downgrade",
            revision="base",
        ),
    )
    tables_by_schema, _, versions, _ = asyncio.run(
        _inspect_database(migration_database.url, (tenant_a, tenant_b))
    )
    assert tables_by_schema[tenant_b] == {"alembic_version"}
    assert versions[tenant_b] == []
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=tenant",
            f"tenant_schema={tenant_b}",
        ),
    )

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=platform",
            action="downgrade",
            revision="base",
        ),
    )
    tables_by_schema, _, versions, _ = asyncio.run(
        _inspect_database(migration_database.url, (tenant_a, tenant_b))
    )
    assert tables_by_schema["platform"] == {"alembic_version"}
    assert versions["platform"] == []
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )


@pytest.mark.parametrize(
    ("x_arguments", "expected_message"),
    [
        ((), "scope must be exactly platform or tenant"),
        (("scope=unknown",), "scope must be exactly platform or tenant"),
        (("scope=tenant",), "scope must be exactly platform or tenant"),
        (
            ("scope=tenant", "tenant_schema=platform"),
            "tenant_schema must match tenant_[0-9a-f]{16}",
        ),
        (
            ("scope=tenant", "tenant_schema=tenant_DEADBEEFDEADBEEF"),
            "tenant_schema must match tenant_[0-9a-f]{16}",
        ),
        (
            (
                "scope=tenant",
                "tenant_schema=tenant_0123456789abcdef;DROP SCHEMA platform",
            ),
            "tenant_schema must match tenant_[0-9a-f]{16}",
        ),
        (
            ("scope=platform", "tenant_schema=tenant_0123456789abcdef"),
            "scope must be exactly platform or tenant",
        ),
        (
            ("scope=platform", "unexpected=value"),
            "scope must be exactly platform or tenant",
        ),
    ],
)
def test_migration_rejects_unrecognized_or_dangerous_scope_arguments(
    migration_database,
    x_arguments,
    expected_message,
):
    completed = _run_alembic(migration_database, *x_arguments)

    safe_output = _assert_secret_safe_output(migration_database, completed)
    assert completed.returncode != 0, safe_output
    assert expected_message in safe_output
