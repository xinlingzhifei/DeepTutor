from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

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


@dataclass(frozen=True)
class InstalledMigration:
    command: Path
    wheel: Path
    cli_wheel: Path


def _clean_python_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _assert_subprocess_succeeded(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"


@pytest.fixture(scope="module")
def installed_migration(tmp_path_factory) -> InstalledMigration:
    build_root = tmp_path_factory.mktemp("installed-migration")
    source_root = build_root / "source"
    source_root.mkdir()
    for filename in ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml"):
        shutil.copy2(PROJECT_ROOT / filename, source_root / filename)
    for package_directory in ("deeptutor", "deeptutor_cli", "deeptutor_web"):
        shutil.copytree(
            PROJECT_ROOT / package_directory,
            source_root / package_directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    cli_project = source_root / "packaging" / "deeptutor-cli"
    cli_project.mkdir(parents=True)
    for filename in ("README.md", "pyproject.toml"):
        shutil.copy2(
            PROJECT_ROOT / "packaging" / "deeptutor-cli" / filename,
            cli_project / filename,
        )

    full_wheelhouse = build_root / "full-wheel"
    full_wheelhouse.mkdir()
    full_build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(full_wheelhouse),
            str(source_root),
        ],
        cwd=build_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    _assert_subprocess_succeeded(full_build)
    wheels = tuple(full_wheelhouse.glob("deeptutor-*.whl"))
    assert len(wheels) == 1

    cli_wheelhouse = build_root / "cli-wheel"
    cli_wheelhouse.mkdir()
    cli_build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(cli_wheelhouse),
            str(source_root / "packaging" / "deeptutor-cli"),
        ],
        cwd=build_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    _assert_subprocess_succeeded(cli_build)
    cli_wheels = tuple(cli_wheelhouse.glob("deeptutor_cli-*.whl"))
    assert len(cli_wheels) == 1

    environment_root = build_root / "venv"
    create_environment = subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            "--system-site-packages",
            str(environment_root),
        ],
        cwd=build_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    _assert_subprocess_succeeded(create_environment)
    scripts_directory = environment_root / ("Scripts" if os.name == "nt" else "bin")
    python_name = "python.exe" if os.name == "nt" else "python"
    install = subprocess.run(
        [
            str(scripts_directory / python_name),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(wheels[0]),
        ],
        cwd=build_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    _assert_subprocess_succeeded(install)
    command_name = "deeptutor-migrate.exe" if os.name == "nt" else "deeptutor-migrate"
    return InstalledMigration(
        command=scripts_directory / command_name,
        wheel=wheels[0],
        cli_wheel=cli_wheels[0],
    )


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

        environment = _clean_python_environment()
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


def _run_packaged_migration(
    installed: InstalledMigration,
    database: MigrationDatabase,
    *,
    action: str,
    scope: str,
    tenant_schema: str | None = None,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    command = [str(installed.command), action, "--scope", scope]
    if tenant_schema is not None:
        command.extend(("--tenant-schema", tenant_schema))
    if not installed.command.is_file():
        return subprocess.CompletedProcess(
            command,
            returncode=127,
            stdout="",
            stderr="installed migration entry point is missing",
        )
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
        pytest.fail("Packaged migration subprocess timed out after 120 seconds")


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
    return {(schema, table.name): set(table.columns.keys()) for table in metadata.tables.values()}


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
    installed_migration,
    migration_database,
    tmp_path,
):
    tenant_schema = tenant_schema_name("external/alembic/cwd")
    assert PROJECT_ROOT not in tmp_path.resolve().parents
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == set()

    completed = _run_packaged_migration(
        installed_migration,
        migration_database,
        action="upgrade",
        scope="tenant",
        tenant_schema=tenant_schema,
        cwd=tmp_path,
    )

    _assert_migration_succeeded(migration_database, completed)
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == {
        "alembic_version",
        "classes",
        "courses",
        "enrollments",
    }


def test_wheel_packages_migrations_and_full_app_entrypoint(
    installed_migration,
    tmp_path,
) -> None:
    with zipfile.ZipFile(installed_migration.wheel) as archive:
        names = set(archive.namelist())
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = archive.read(entry_points_name).decode("utf-8")
    assert {
        "deeptutor/teaching/migrations/__init__.py",
        "deeptutor/teaching/migrations/env.py",
        "deeptutor/teaching/migrations/script.py.mako",
        "deeptutor/teaching/migrations/versions/__init__.py",
        "deeptutor/teaching/migrations/versions/20260728_0001_foundation.py",
    }.issubset(names)
    assert "deeptutor-migrate = deeptutor.teaching.migrations.cli:main" in entry_points

    with zipfile.ZipFile(installed_migration.cli_wheel) as archive:
        cli_entry_points_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
        )
        cli_entry_points = archive.read(cli_entry_points_name).decode("utf-8")
    assert "deeptutor-migrate" not in cli_entry_points

    assert PROJECT_ROOT not in tmp_path.resolve().parents
    assert installed_migration.command.is_file()
    completed = subprocess.run(
        [str(installed_migration.command), "--help"],
        cwd=tmp_path,
        env=_clean_python_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    _assert_subprocess_succeeded(completed)
    assert "upgrade" in completed.stdout
    assert "downgrade" in completed.stdout


def test_packaged_entrypoint_runs_platform_and_tenant_scopes(
    installed_migration,
    migration_database,
    tmp_path,
) -> None:
    tenant_schema = tenant_schema_name("packaged/platform-and-tenant")

    for scope, schema in (("platform", None), ("tenant", tenant_schema)):
        completed = _run_packaged_migration(
            installed_migration,
            migration_database,
            action="upgrade",
            scope=scope,
            tenant_schema=schema,
            cwd=tmp_path,
        )
        _assert_migration_succeeded(migration_database, completed)

    assert asyncio.run(_table_names(migration_database.url, "platform")) == {
        "alembic_version",
        "audit_log",
        "data_plane_routes",
        "role_grants",
        "tenant_memberships",
        "tenant_provisioning_jobs",
        "tenant_storage_credentials",
        "tenants",
    }
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == {
        "alembic_version",
        "classes",
        "courses",
        "enrollments",
    }


@pytest.mark.parametrize(
    ("arguments", "expected_message"),
    [
        (
            ("upgrade", "--scope", "platform", "--tenant-schema", "tenant_0123456789abcdef"),
            "scope must be exactly platform or tenant",
        ),
        (
            ("upgrade", "--scope", "tenant"),
            "scope must be exactly platform or tenant",
        ),
        (
            ("upgrade", "--scope", "tenant", "--tenant-schema", "platform"),
            "tenant_schema must match tenant_[0-9a-f]{16}",
        ),
    ],
)
def test_packaged_entrypoint_rejects_invalid_scope_exactly(
    installed_migration,
    migration_database,
    tmp_path,
    arguments,
    expected_message,
) -> None:
    command = [str(installed_migration.command), *arguments]
    if installed_migration.command.is_file():
        completed = subprocess.run(
            command,
            cwd=tmp_path,
            env=migration_database.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    else:
        completed = subprocess.CompletedProcess(
            command,
            returncode=127,
            stdout="",
            stderr="installed migration entry point is missing",
        )

    safe_output = _assert_secret_safe_output(migration_database, completed)
    assert completed.returncode != 0
    assert expected_message in safe_output


@pytest.mark.parametrize(
    ("platform_document", "expected_message"),
    [
        ('{"enabled":false}', "platform database is disabled"),
        ("not-json", "platform database settings are invalid"),
    ],
)
def test_packaged_entrypoint_fails_closed_on_unavailable_platform_settings(
    installed_migration,
    tmp_path,
    platform_document,
    expected_message,
) -> None:
    runtime_home = tmp_path / expected_message.replace(" ", "-")
    settings_dir = runtime_home / "data" / "user" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "platform.json").write_text(
        platform_document,
        encoding="utf-8",
    )
    environment = _clean_python_environment()
    environment["DEEPTUTOR_HOME"] = str(runtime_home)
    environment.pop("DEEPTUTOR_PLATFORM_DATABASE_URL", None)
    command = [str(installed_migration.command), "upgrade", "--scope", "platform"]
    if installed_migration.command.is_file():
        completed = subprocess.run(
            command,
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    else:
        completed = subprocess.CompletedProcess(
            command,
            returncode=127,
            stdout="",
            stderr="installed migration entry point is missing",
        )

    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode != 0
    assert expected_message in output


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
    assert "teaching migrations support only upgrade and downgrade" in safe_output


def test_revision_autogenerate_is_unsupported_without_writing_a_file(
    migration_database,
):
    versions_dir = PROJECT_ROOT / "deeptutor" / "teaching" / "migrations" / "versions"
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
    assert "teaching migrations support only upgrade and downgrade" in safe_output
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
