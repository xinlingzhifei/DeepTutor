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
from sqlalchemy import func, make_url, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from deeptutor.teaching.models import PlatformBase, TenantBase
from deeptutor.teaching.schema_names import tenant_schema_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FOUNDATION_REVISION = "20260728_0001"
SCOPED_GRANTS_REVISION = "20260730_0002"
PROVISIONING_REVISION = "20260730_0003"
HEAD_REVISION = "20260802_0008"


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

    platform_upgrade = _run_packaged_migration(
        installed_migration,
        migration_database,
        action="upgrade",
        scope="platform",
        cwd=tmp_path,
    )
    _assert_migration_succeeded(migration_database, platform_upgrade)

    tenant_upgrade = _run_packaged_migration(
        installed_migration,
        migration_database,
        action="upgrade",
        scope="tenant",
        tenant_schema=tenant_schema,
        cwd=tmp_path,
    )
    _assert_migration_succeeded(migration_database, tenant_upgrade)
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == {
        "alembic_version",
        "approvals",
        "artifact_promotion_states",
        "assignments",
        "batch_items",
        "batch_jobs",
        "classroom_artifacts",
        "classroom_assets",
        "classroom_drafts",
        "classroom_exports",
        "classroom_versions",
        "classes",
        "courses",
        "enrollments",
        "generation_jobs",
        "publications",
        "quota_ledger",
        "source_snapshots",
        "source_uploads",
        "teaching_briefs",
        "tenant_source_bindings",
    }

    tenant_downgrade = _run_packaged_migration(
        installed_migration,
        migration_database,
        action="downgrade",
        scope="tenant",
        tenant_schema=tenant_schema,
        cwd=tmp_path,
    )
    _assert_migration_succeeded(migration_database, tenant_downgrade)
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == {
        "alembic_version"
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
        "deeptutor/teaching/migrations/versions/20260730_0002_scoped_role_grants.py",
        "deeptutor/teaching/migrations/versions/20260730_0003_tenant_provisioning_worker.py",
        "deeptutor/teaching/migrations/versions/20260730_0004_data_plane_routing.py",
        "deeptutor/teaching/migrations/versions/20260730_0005_generation_jobs.py",
        "deeptutor/teaching/migrations/versions/20260801_0006_job_recovery.py",
        "deeptutor/teaching/migrations/versions/20260801_0007_trusted_job_inputs.py",
        "deeptutor/teaching/migrations/versions/20260802_0008_classroom_lifecycle.py",
    }.issubset(names)
    assert "deeptutor-migrate = deeptutor.teaching.migrations.cli:main" in entry_points
    assert "deeptutor-provisioner = deeptutor.teaching.provisioning_cli:main" in entry_points

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
        "generation_queue",
        "generation_slots",
        "outbox_messages",
        "provider_profiles",
        "role_grants",
        "tenant_default_policy_states",
        "tenant_memberships",
        "tenant_provisioning_jobs",
        "tenant_scheduler_state",
        "tenant_schema_states",
        "tenant_storage_credentials",
        "tenant_storage_states",
        "tenants",
    }
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == {
        "alembic_version",
        "approvals",
        "artifact_promotion_states",
        "assignments",
        "batch_items",
        "batch_jobs",
        "classroom_artifacts",
        "classroom_assets",
        "classroom_drafts",
        "classroom_exports",
        "classroom_versions",
        "classes",
        "courses",
        "enrollments",
        "generation_jobs",
        "publications",
        "quota_ledger",
        "source_snapshots",
        "source_uploads",
        "teaching_briefs",
        "tenant_source_bindings",
    }

    tenant_downgrade = _run_packaged_migration(
        installed_migration,
        migration_database,
        action="downgrade",
        scope="tenant",
        tenant_schema=tenant_schema,
        cwd=tmp_path,
    )
    _assert_migration_succeeded(migration_database, tenant_downgrade)
    assert asyncio.run(_table_names(migration_database.url, tenant_schema)) == {
        "alembic_version"
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
        "generation_queue",
        "generation_slots",
        "outbox_messages",
        "provider_profiles",
        "role_grants",
        "tenant_default_policy_states",
        "tenant_memberships",
        "tenant_provisioning_jobs",
        "tenant_scheduler_state",
        "tenant_schema_states",
        "tenant_storage_credentials",
        "tenant_storage_states",
        "tenants",
    }
    assert tables_by_schema[tenant_a] == {
        "alembic_version",
        "approvals",
        "artifact_promotion_states",
        "assignments",
        "batch_items",
        "batch_jobs",
        "classroom_artifacts",
        "classroom_assets",
        "classroom_drafts",
        "classroom_exports",
        "classroom_versions",
        "classes",
        "courses",
        "enrollments",
        "generation_jobs",
        "publications",
        "quota_ledger",
        "source_snapshots",
        "source_uploads",
        "teaching_briefs",
        "tenant_source_bindings",
    }
    assert tables_by_schema[tenant_b] == {
        "alembic_version",
        "approvals",
        "artifact_promotion_states",
        "assignments",
        "batch_items",
        "batch_jobs",
        "classroom_artifacts",
        "classroom_assets",
        "classroom_drafts",
        "classroom_exports",
        "classroom_versions",
        "classes",
        "courses",
        "enrollments",
        "generation_jobs",
        "publications",
        "quota_ledger",
        "source_snapshots",
        "source_uploads",
        "teaching_briefs",
        "tenant_source_bindings",
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

    for tenant_schema in (tenant_a, tenant_b):
        _assert_migration_succeeded(
            migration_database,
            _run_alembic(
                migration_database,
                "scope=tenant",
                f"tenant_schema={tenant_schema}",
                action="downgrade",
                revision="base",
            ),
        )
    tables_by_schema, _, versions, _ = asyncio.run(
        _inspect_database(migration_database.url, (tenant_a, tenant_b))
    )
    assert tables_by_schema[tenant_a] == {"alembic_version"}
    assert tables_by_schema[tenant_b] == {"alembic_version"}
    assert versions[tenant_a] == []
    assert versions[tenant_b] == []

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


def test_provisioning_worker_migration_roundtrips_0002_and_backfills_routes(
    migration_database,
) -> None:
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=platform",
            action="downgrade",
            revision=SCOPED_GRANTS_REVISION,
        ),
    )

    async def seed_legacy_route() -> None:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO platform.tenants (id, name, status)
                        VALUES (
                            'migration-worker-tenant',
                            'Migration Worker Tenant',
                            'provisioning'
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    )
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO platform.data_plane_routes (
                            tenant_id,
                            schema_name,
                            status
                        )
                        VALUES (
                            'migration-worker-tenant',
                            'tenant_0123456789abcdef',
                            'active'
                        )
                        ON CONFLICT (tenant_id) DO UPDATE
                        SET schema_name = EXCLUDED.schema_name,
                            status = EXCLUDED.status
                        """
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_legacy_route())
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )

    async def inspect_head() -> tuple[tuple[str, str, str], tuple[str, ...], str]:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.connect() as connection:
                state = (
                    await connection.execute(
                        text(
                            """
                            SELECT schema_name, revision, status
                            FROM platform.tenant_schema_states
                            WHERE tenant_id = 'migration-worker-tenant'
                            """
                        )
                    )
                ).one()
                job_columns = tuple(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT column_name
                                FROM information_schema.columns
                                WHERE table_schema = 'platform'
                                  AND table_name = 'tenant_provisioning_jobs'
                                  AND column_name IN (
                                      'lease_owner',
                                      'lease_token',
                                      'lease_expires_at',
                                      'heartbeat_at',
                                      'next_attempt_at',
                                      'max_attempts',
                                      'error_category',
                                      'error_code',
                                      'started_at',
                                      'completed_at'
                                  )
                                ORDER BY column_name
                                """
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                version = await connection.scalar(
                    text("SELECT version_num FROM platform.alembic_version")
                )
                return tuple(state), job_columns, str(version)
        finally:
            await engine.dispose()

    state, job_columns, version = asyncio.run(inspect_head())
    assert state == (
        "tenant_0123456789abcdef",
        SCOPED_GRANTS_REVISION,
        "active",
    )
    assert job_columns == (
        "completed_at",
        "error_category",
        "error_code",
        "heartbeat_at",
        "lease_expires_at",
        "lease_owner",
        "lease_token",
        "max_attempts",
        "next_attempt_at",
        "started_at",
    )
    assert version == HEAD_REVISION

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=platform",
            action="downgrade",
            revision=SCOPED_GRANTS_REVISION,
        ),
    )

    async def inspect_legacy_again() -> tuple[tuple[str, str], str | None, str]:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.connect() as connection:
                route = (
                    await connection.execute(
                        text(
                            """
                            SELECT schema_name, status
                            FROM platform.data_plane_routes
                            WHERE tenant_id = 'migration-worker-tenant'
                            """
                        )
                    )
                ).one()
                state_table = await connection.scalar(
                    text("SELECT to_regclass('platform.tenant_schema_states')")
                )
                version = await connection.scalar(
                    text("SELECT version_num FROM platform.alembic_version")
                )
                return tuple(route), state_table, str(version)
        finally:
            await engine.dispose()

    route, state_table, version = asyncio.run(inspect_legacy_again())
    assert route == ("tenant_0123456789abcdef", "active")
    assert state_table is None
    assert version == SCOPED_GRANTS_REVISION

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )


def test_data_plane_routing_migration_preserves_legacy_schema_fact_and_roundtrips(
    migration_database,
) -> None:
    tenant_id = "migration-data-plane-tenant"
    schema_name = tenant_schema_name(tenant_id)
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=platform",
            action="downgrade",
            revision=PROVISIONING_REVISION,
        ),
    )

    async def seed_legacy_route() -> None:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO platform.tenants (id, name, status)
                        VALUES (
                            :tenant_id,
                            'Migration Data Plane Tenant',
                            'active'
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        """
                        DELETE FROM platform.tenant_schema_states
                        WHERE tenant_id = :tenant_id
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO platform.data_plane_routes (
                            tenant_id,
                            schema_name,
                            status
                        )
                        VALUES (:tenant_id, :schema_name, 'active')
                        ON CONFLICT (tenant_id) DO UPDATE
                        SET schema_name = EXCLUDED.schema_name,
                            status = EXCLUDED.status
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "schema_name": schema_name,
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_legacy_route())
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )

    async def inspect_head() -> tuple[
        tuple[str, str, str],
        str,
        set[str],
        set[str],
        str,
    ]:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.connect() as connection:
                schema_state = (
                    await connection.execute(
                        text(
                            """
                            SELECT schema_name, revision, status
                            FROM platform.tenant_schema_states
                            WHERE tenant_id = :tenant_id
                            """
                        ),
                        {"tenant_id": tenant_id},
                    )
                ).one()
                tenant_mode = await connection.scalar(
                    text(
                        """
                        SELECT data_plane_mode
                        FROM platform.tenants
                        WHERE id = :tenant_id
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
                route_columns = set(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT column_name
                                FROM information_schema.columns
                                WHERE table_schema = 'platform'
                                  AND table_name = 'data_plane_routes'
                                """
                            )
                        )
                    ).scalars()
                )
                profile_columns = set(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT column_name
                                FROM information_schema.columns
                                WHERE table_schema = 'platform'
                                  AND table_name = 'provider_profiles'
                                """
                            )
                        )
                    ).scalars()
                )
                revision = await connection.scalar(
                    text("SELECT version_num FROM platform.alembic_version")
                )
                return (
                    tuple(schema_state),
                    str(tenant_mode),
                    route_columns,
                    profile_columns,
                    str(revision),
                )
        finally:
            await engine.dispose()

    state, tenant_mode, route_columns, profile_columns, revision = asyncio.run(inspect_head())
    assert state == (schema_name, PROVISIONING_REVISION, "active")
    assert tenant_mode == "shared"
    assert route_columns == {
        "id",
        "tenant_id",
        "owner_key",
        "mode",
        "base_url",
        "worker_pool",
        "queue_name",
        "provider_profile_id",
        "status",
        "health_status",
        "health_checked_at",
        "created_at",
        "updated_at",
    }
    assert profile_columns == {
        "id",
        "scope",
        "tenant_id",
        "owner_key",
        "provider_type",
        "model_name",
        "api_base_url",
        "secret_ref",
        "status",
        "created_at",
        "updated_at",
    }
    assert revision == HEAD_REVISION

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=platform",
            action="downgrade",
            revision=PROVISIONING_REVISION,
        ),
    )

    async def inspect_legacy_route() -> tuple[str, str]:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT schema_name, status
                            FROM platform.data_plane_routes
                            WHERE tenant_id = :tenant_id
                            """
                        ),
                        {"tenant_id": tenant_id},
                    )
                ).one()
                return tuple(row)
        finally:
            await engine.dispose()

    assert asyncio.run(inspect_legacy_route()) == (schema_name, "active")
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )


def test_scoped_role_grant_migration_backfills_and_refuses_lossy_downgrade(
    migration_database,
) -> None:
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=platform",
            action="downgrade",
            revision="base",
        ),
    )
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=platform",
            revision=FOUNDATION_REVISION,
        ),
    )

    async def seed_legacy_grant() -> None:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.begin() as connection:
                for statement in (
                    """
                        INSERT INTO platform.tenants (id, name, status)
                        VALUES ('migration-tenant', 'Migration Tenant', 'active')
                    """,
                    """
                        INSERT INTO platform.tenant_memberships
                            (tenant_id, user_id, status)
                        VALUES ('migration-tenant', 'migration-user', 'active')
                    """,
                    """
                        INSERT INTO platform.role_grants (tenant_id, user_id, role)
                        VALUES ('migration-tenant', 'migration-user', 'teacher')
                    """,
                ):
                    await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(seed_legacy_grant())
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )

    async def inspect_grant_schema() -> tuple:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.connect() as connection:
                grant = (
                    await connection.execute(
                        text(
                            """
                            SELECT scope_type, scope_id
                            FROM platform.role_grants
                            WHERE tenant_id = 'migration-tenant'
                            """
                        )
                    )
                ).one()
                columns = dict(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT column_name, is_nullable
                                FROM information_schema.columns
                                WHERE table_schema = 'platform'
                                  AND table_name = 'role_grants'
                                  AND column_name IN ('scope_type', 'scope_id')
                                """
                            )
                        )
                    ).all()
                )
                constraints = dict(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT constraint_name, check_clause
                                FROM information_schema.check_constraints
                                WHERE constraint_schema = 'platform'
                                  AND constraint_name = 'ck_role_grants_scope_type'
                                UNION ALL
                                SELECT tc.constraint_name,
                                       string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position)
                                FROM information_schema.table_constraints tc
                                JOIN information_schema.key_column_usage kcu
                                  ON kcu.constraint_schema = tc.constraint_schema
                                 AND kcu.constraint_name = tc.constraint_name
                                WHERE tc.table_schema = 'platform'
                                  AND tc.table_name = 'role_grants'
                                  AND tc.constraint_type = 'PRIMARY KEY'
                                GROUP BY tc.constraint_name
                                """
                            )
                        )
                    ).all()
                )
                index_definition = await connection.scalar(
                    text(
                        """
                        SELECT indexdef
                        FROM pg_indexes
                        WHERE schemaname = 'platform'
                          AND indexname = 'ix_role_grants_tenant_user_scope'
                        """
                    )
                )
                version = await connection.scalar(
                    text("SELECT version_num FROM platform.alembic_version")
                )
                return grant, columns, constraints, index_definition, version
        finally:
            await engine.dispose()

    grant, columns, constraints, index_definition, version = asyncio.run(inspect_grant_schema())
    assert tuple(grant) == ("tenant", "migration-tenant")
    assert columns == {"scope_id": "NO", "scope_type": "NO"}
    assert constraints["pk_role_grants"] == ("tenant_id,user_id,role,scope_type,scope_id")
    assert all(
        scope in constraints["ck_role_grants_scope_type"]
        for scope in (
            "tenant",
            "course",
            "class",
        )
    )
    assert index_definition is not None
    assert "(tenant_id, user_id, scope_type, scope_id)" in index_definition
    assert version == HEAD_REVISION

    async def set_scope(scope_type: str, scope_id: str) -> None:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE platform.role_grants
                        SET scope_type = :scope_type, scope_id = :scope_id
                        WHERE tenant_id = 'migration-tenant'
                        """
                    ),
                    {"scope_type": scope_type, "scope_id": scope_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(set_scope("course", "course-a"))
    refused = _run_alembic(
        migration_database,
        "scope=platform",
        action="downgrade",
        revision=FOUNDATION_REVISION,
    )
    assert refused.returncode != 0, _assert_secret_safe_output(
        migration_database,
        refused,
    )
    asyncio.run(set_scope("tenant", "migration-tenant"))
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=platform",
            action="downgrade",
            revision=FOUNDATION_REVISION,
        ),
    )

    async def inspect_legacy_grant() -> tuple:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.connect() as connection:
                legacy_grant = (
                    await connection.execute(
                        text(
                            """
                            SELECT tenant_id, user_id, role, granted_at IS NOT NULL
                            FROM platform.role_grants
                            WHERE tenant_id = 'migration-tenant'
                            """
                        )
                    )
                ).one()
                scope_columns = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = 'platform'
                              AND table_name = 'role_grants'
                              AND column_name IN ('scope_type', 'scope_id')
                            ORDER BY column_name
                            """
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                legacy_version = await connection.scalar(
                    text("SELECT version_num FROM platform.alembic_version")
                )
                return legacy_grant, tuple(scope_columns), legacy_version
        finally:
            await engine.dispose()

    legacy_grant, scope_columns, legacy_version = asyncio.run(inspect_legacy_grant())
    assert tuple(legacy_grant) == (
        "migration-tenant",
        "migration-user",
        "teacher",
        True,
    )
    assert scope_columns == ()
    assert legacy_version == FOUNDATION_REVISION

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    restored_grant, _, _, _, restored_version = asyncio.run(inspect_grant_schema())
    assert tuple(restored_grant) == ("tenant", "migration-tenant")
    assert restored_version == HEAD_REVISION


def _install_source_runtime_database(
    monkeypatch: pytest.MonkeyPatch,
    migration_database: MigrationDatabase,
):
    from pydantic import SecretStr

    from deeptutor.services import config as config_module
    from deeptutor.services.config import PlatformSettings
    from deeptutor.teaching import database as database_module

    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr(migration_database.url),
        object_store_mode="local",
    )
    monkeypatch.setattr(
        database_module,
        "load_platform_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        config_module,
        "load_platform_settings",
        lambda: settings,
    )
    return settings, database_module


def test_postgres_data_plane_routing_is_fail_closed_and_owner_bound(
    migration_database,
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    from deeptutor.teaching.models import (
        AuditLog,
        DataPlaneRoute,
        ProviderProfile,
        Tenant,
    )
    from deeptutor.teaching.openmaic.data_planes import (
        DataPlaneSelection,
        DataPlaneSelector,
        DataPlaneUnavailable,
    )
    from deeptutor.teaching.repositories.data_planes import (
        SqlAlchemyDataPlaneRepository,
    )

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _settings, database_module = _install_source_runtime_database(
        monkeypatch,
        migration_database,
    )

    async def exercise() -> None:
        await database_module.dispose_platform_engine()
        repository = SqlAlchemyDataPlaneRepository()
        standard_tenant = "routing-standard"
        private_tenant = "routing-private"
        missing_tenant = "routing-missing"
        wrong_owner_tenant = "routing-wrong-owner"
        binding_tenant = "routing-wrong-binding"
        foreign_tenant = "routing-foreign"
        shared_profile_id = "routing-shared-profile"
        private_profile_id = "routing-private-profile"
        foreign_profile_id = "routing-foreign-profile"
        shared_route_id = "routing-shared-route"
        private_route_id = "routing-private-route"
        try:
            async with database_module.platform_session() as session:
                async with session.begin():
                    session.add_all(
                        [
                            Tenant(
                                id=standard_tenant,
                                name="Routing standard",
                                status="active",
                                data_plane_mode="shared",
                            ),
                            Tenant(
                                id=private_tenant,
                                name="Routing private",
                                status="active",
                                data_plane_mode="dedicated",
                            ),
                            Tenant(
                                id=missing_tenant,
                                name="Routing missing",
                                status="active",
                                data_plane_mode="dedicated",
                            ),
                            Tenant(
                                id=wrong_owner_tenant,
                                name="Routing wrong owner",
                                status="active",
                                data_plane_mode="dedicated",
                            ),
                            Tenant(
                                id=binding_tenant,
                                name="Routing wrong binding",
                                status="active",
                                data_plane_mode="dedicated",
                            ),
                            Tenant(
                                id=foreign_tenant,
                                name="Routing foreign",
                                status="active",
                                data_plane_mode="dedicated",
                            ),
                        ]
                    )
                async with session.begin():
                    session.add_all(
                        [
                            ProviderProfile(
                                id=shared_profile_id,
                                scope="shared",
                                tenant_id=None,
                                owner_key="shared",
                                provider_type="openai-compatible",
                                model_name="shared-model",
                                api_base_url=None,
                                secret_ref=(f"shared/providers/{shared_profile_id}"),
                                status="active",
                            ),
                            ProviderProfile(
                                id=private_profile_id,
                                scope="dedicated",
                                tenant_id=private_tenant,
                                owner_key=private_tenant,
                                provider_type="openai-compatible",
                                model_name="private-model",
                                api_base_url=None,
                                secret_ref=(
                                    f"tenants/{private_tenant}/providers/{private_profile_id}"
                                ),
                                status="active",
                            ),
                            ProviderProfile(
                                id=foreign_profile_id,
                                scope="dedicated",
                                tenant_id=foreign_tenant,
                                owner_key=foreign_tenant,
                                provider_type="openai-compatible",
                                model_name="foreign-model",
                                api_base_url=None,
                                secret_ref=(
                                    f"tenants/{foreign_tenant}/providers/{foreign_profile_id}"
                                ),
                                status="active",
                            ),
                        ]
                    )
                async with session.begin():
                    session.add_all(
                        [
                            DataPlaneRoute(
                                id=shared_route_id,
                                tenant_id=None,
                                owner_key="shared",
                                mode="shared",
                                base_url="http://openmaic-shared:3000",
                                worker_pool="routing-shared-pool",
                                queue_name="routing.shared",
                                provider_profile_id=shared_profile_id,
                                status="active",
                                health_status="healthy",
                            ),
                            DataPlaneRoute(
                                id=private_route_id,
                                tenant_id=private_tenant,
                                owner_key=private_tenant,
                                mode="dedicated",
                                base_url="http://openmaic-private:3000",
                                worker_pool="routing-private-pool",
                                queue_name="routing.private",
                                provider_profile_id=private_profile_id,
                                status="active",
                                health_status="healthy",
                            ),
                        ]
                    )

            selector = DataPlaneSelector(
                settings=SimpleNamespace(enabled=True),
                repository=repository,
            )
            standard_selection = await selector.resolve(standard_tenant)
            assert standard_selection == DataPlaneSelection(
                tenant_id=standard_tenant,
                route_ref=shared_route_id,
                provider_profile_ref=shared_profile_id,
                mode="shared",
                worker_pool_ref="routing-shared-pool",
                queue_ref="routing.shared",
            )
            private_selection = await selector.resolve(private_tenant)
            assert private_selection == DataPlaneSelection(
                tenant_id=private_tenant,
                route_ref=private_route_id,
                provider_profile_ref=private_profile_id,
                mode="dedicated",
                worker_pool_ref="routing-private-pool",
                queue_ref="routing.private",
            )
            assert (
                await repository.resolve_bound_profile(private_selection)
            ).profile_id == private_profile_id
            bound_route = await repository.resolve_bound_route(private_selection)
            assert bound_route is not None
            assert bound_route.route_id == private_route_id
            assert bound_route.base_url == "http://openmaic-private:3000"

            assert await repository.set_health(
                private_route_id,
                "unhealthy",
            )
            assert await repository.resolve_bound_profile(private_selection) is None
            assert await repository.resolve_bound_route(private_selection) is None
            with pytest.raises(DataPlaneUnavailable):
                await selector.resolve(private_tenant)
            with pytest.raises(DataPlaneUnavailable):
                await selector.resolve(missing_tenant)

            async def assert_insert_rejected(instance) -> None:
                async with database_module.platform_session() as session:
                    with pytest.raises(IntegrityError):
                        async with session.begin():
                            session.add(instance)
                            await session.flush()

            await assert_insert_rejected(
                DataPlaneRoute(
                    id="routing-second-shared",
                    tenant_id=None,
                    owner_key="shared",
                    mode="shared",
                    base_url="http://openmaic-shared-2:3000",
                    worker_pool="routing-shared-pool-2",
                    queue_name="routing.shared.2",
                    provider_profile_id=shared_profile_id,
                    status="active",
                    health_status="healthy",
                )
            )
            await assert_insert_rejected(
                DataPlaneRoute(
                    id="routing-missing-dedicated-owner",
                    tenant_id=None,
                    owner_key=private_tenant,
                    mode="dedicated",
                    base_url="http://openmaic-invalid:3000",
                    worker_pool="routing-invalid-pool",
                    queue_name="routing.invalid",
                    provider_profile_id=private_profile_id,
                    status="active",
                    health_status="healthy",
                )
            )
            await assert_insert_rejected(
                DataPlaneRoute(
                    id="routing-wrong-owner-route",
                    tenant_id=wrong_owner_tenant,
                    owner_key=foreign_tenant,
                    mode="dedicated",
                    base_url="http://openmaic-wrong-owner:3000",
                    worker_pool="routing-wrong-owner-pool",
                    queue_name="routing.wrong-owner",
                    provider_profile_id=foreign_profile_id,
                    status="active",
                    health_status="healthy",
                )
            )
            await assert_insert_rejected(
                DataPlaneRoute(
                    id="routing-wrong-profile-binding",
                    tenant_id=binding_tenant,
                    owner_key=binding_tenant,
                    mode="dedicated",
                    base_url="http://openmaic-wrong-binding:3000",
                    worker_pool="routing-wrong-binding-pool",
                    queue_name="routing.wrong-binding",
                    provider_profile_id=foreign_profile_id,
                    status="active",
                    health_status="healthy",
                )
            )

            async with database_module.platform_session() as session:
                audits = (
                    await session.scalars(
                        select(AuditLog)
                        .where(
                            AuditLog.tenant_id.in_(
                                (
                                    standard_tenant,
                                    private_tenant,
                                    missing_tenant,
                                )
                            ),
                            AuditLog.action.like("teaching.data_plane.%"),
                        )
                        .order_by(AuditLog.id)
                    )
                ).all()
            assert [
                (
                    audit.tenant_id,
                    audit.action,
                    audit.resource_type,
                    audit.resource_id,
                )
                for audit in audits
            ] == [
                (
                    standard_tenant,
                    "teaching.data_plane.selected",
                    "data_plane_route:shared",
                    f"{shared_route_id}/{shared_profile_id}",
                ),
                (
                    private_tenant,
                    "teaching.data_plane.selected",
                    "data_plane_route:dedicated",
                    f"{private_route_id}/{private_profile_id}",
                ),
                (
                    private_tenant,
                    "teaching.data_plane.unavailable",
                    "data_plane_route:dedicated",
                    f"{private_route_id}/{private_profile_id}",
                ),
                (
                    missing_tenant,
                    "teaching.data_plane.unavailable",
                    "data_plane_route:dedicated",
                    None,
                ),
            ]
            audit_repr = repr(audits)
            assert "openmaic-shared" not in audit_repr
            assert "shared/providers" not in audit_repr
            assert "private/providers" not in audit_repr
        finally:
            await database_module.dispose_platform_engine()

    asyncio.run(exercise())


def test_active_0007_tenant_schema_upgrade_is_idempotent_and_recoverable(
    migration_database,
    monkeypatch,
) -> None:
    from deeptutor.teaching.models import (
        Tenant,
        TenantProvisioningJob,
        TenantSchemaState,
    )
    from deeptutor.teaching.provisioning_worker import (
        TENANT_SCHEMA_REVISION,
        AlembicTenantSchemaProvisioner,
        ProvisioningStepError,
        ProvisioningWorker,
    )
    from deeptutor.teaching.repositories.provisioning import (
        SqlAlchemyProvisioningRepository,
    )

    tenant_id = "active-schema-upgrade"
    schema_name = tenant_schema_name(tenant_id)
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=tenant",
            f"tenant_schema={schema_name}",
            revision="20260801_0007",
        ),
    )
    _settings, database_module = _install_source_runtime_database(
        monkeypatch,
        migration_database,
    )

    class RecoveringSchemaProvisioner:
        def __init__(self) -> None:
            self.calls = 0
            self.delegate = AlembicTenantSchemaProvisioner()

        async def provision(self, requested_tenant_id):
            self.calls += 1
            if self.calls == 1:
                raise ProvisioningStepError(
                    category="schema",
                    code="migration_unavailable",
                    retryable=True,
                )
            return await self.delegate.provision(requested_tenant_id)

    class ForbiddenStorageProvisioner:
        calls = 0

        async def provision(self, requested_tenant_id):
            self.calls += 1
            raise AssertionError("schema upgrade must not provision storage")

    class ForbiddenPolicyProvisioner:
        calls = 0

        async def provision(self, requested_tenant_id):
            self.calls += 1
            raise AssertionError("schema upgrade must not provision policy")

    async def exercise() -> None:
        await database_module.dispose_platform_engine()
        repository = SqlAlchemyProvisioningRepository()
        schema = RecoveringSchemaProvisioner()
        storage = ForbiddenStorageProvisioner()
        policy = ForbiddenPolicyProvisioner()
        worker = ProvisioningWorker(
            enabled=True,
            worker_id="active-upgrade-worker",
            repository=repository,
            schema_upgrade_reconciler=repository,
            schema_provisioner=schema,
            storage_provisioner=storage,
            policy_provisioner=policy,
            lease_seconds=60,
        )
        try:
            async with database_module.platform_session() as session:
                async with session.begin():
                    session.add(
                        Tenant(
                            id=tenant_id,
                            name="Active schema upgrade",
                            status="active",
                            data_plane_mode="shared",
                        )
                    )
                    session.add(
                        TenantSchemaState(
                            tenant_id=tenant_id,
                            schema_name=schema_name,
                            revision="20260801_0007",
                            status="active",
                        )
                    )

            assert await worker.run_once() is True
            async with database_module.platform_session() as session:
                tenant = await session.get(Tenant, tenant_id)
                state = await session.get(TenantSchemaState, tenant_id)
                job = await session.scalar(
                    select(TenantProvisioningJob).where(
                        TenantProvisioningJob.tenant_id == tenant_id,
                        TenantProvisioningJob.operation == "upgrade_schema",
                    )
                )
                assert tenant is not None and tenant.status == "active"
                assert state is not None and state.revision == "20260801_0007"
                assert job is not None
                assert job.target_revision == TENANT_SCHEMA_REVISION
                assert job.status == "pending"
                assert job.attempt_count == 1
                assert job.error_code == "migration_unavailable"

            async with database_module.platform_session() as session:
                async with session.begin():
                    job = await session.scalar(
                        select(TenantProvisioningJob).where(
                            TenantProvisioningJob.tenant_id == tenant_id,
                            TenantProvisioningJob.operation == "upgrade_schema",
                        )
                    )
                    assert job is not None
                    job.next_attempt_at = await session.scalar(select(func.now()))

            reclaimed = await repository.claim_next(
                "active-upgrade-worker",
                lease_seconds=60,
            )
            assert reclaimed is not None
            assert reclaimed.operation == "upgrade_schema"
            schema_result = await schema.provision(tenant_id)
            assert await repository.record_schema_ready(reclaimed, schema_result) is True
            # Simulate the worker disappearing immediately after the persistence call.
            # The schema revision and upgrade terminal state must share one transaction.
            assert await worker.run_once() is False
            async with database_module.platform_session() as session:
                tenant = await session.get(Tenant, tenant_id)
                state = await session.get(TenantSchemaState, tenant_id)
                jobs = (
                    await session.scalars(
                        select(TenantProvisioningJob).where(
                            TenantProvisioningJob.tenant_id == tenant_id,
                            TenantProvisioningJob.operation == "upgrade_schema",
                        )
                    )
                ).all()
                assert tenant is not None and tenant.status == "active"
                assert state is not None
                assert state.revision == TENANT_SCHEMA_REVISION
                assert state.status == "active"
                assert len(jobs) == 1
                assert jobs[0].status == "completed"
                assert jobs[0].target_revision == TENANT_SCHEMA_REVISION
                assert jobs[0].lease_owner is None
                assert jobs[0].lease_token is None
                assert jobs[0].lease_expires_at is None
                assert jobs[0].heartbeat_at is None

            async with database_module.get_platform_engine().connect() as connection:
                revision = await connection.scalar(
                    text(f'SELECT version_num FROM "{schema_name}".alembic_version')
                )
                tenant_fk = await connection.scalar(
                    text(
                        """
                        SELECT confrelid::regclass::text
                        FROM pg_constraint
                        WHERE conrelid = CAST(:table_name AS regclass)
                          AND conname = 'fk_generation_jobs_tenant_id_tenants'
                        """
                    ),
                    {"table_name": f'"{schema_name}".generation_jobs'},
                )
            assert revision == TENANT_SCHEMA_REVISION
            assert tenant_fk == "platform.tenants"
            assert schema.calls == 2
            assert storage.calls == 0
            assert policy.calls == 0
        finally:
            await database_module.dispose_platform_engine()

    asyncio.run(exercise())


def test_generation_tenant_migration_roundtrip_keeps_platform_revision_in_sync(
    migration_database,
) -> None:
    tenant_id = "generation-migration-roundtrip"
    schema_name = tenant_schema_name(tenant_id)
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=tenant",
            f"tenant_schema={schema_name}",
            revision="20260730_0004",
        ),
    )

    async def seed() -> None:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO platform.tenants (id, name, status)
                        VALUES (:tenant_id, 'Generation migration roundtrip', 'active')
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO platform.tenant_schema_states (
                            tenant_id, schema_name, revision, status
                        ) VALUES (
                            :tenant_id, :schema_name, '20260730_0004', 'active'
                        )
                        ON CONFLICT (tenant_id) DO UPDATE
                        SET schema_name = EXCLUDED.schema_name,
                            revision = EXCLUDED.revision,
                            status = EXCLUDED.status
                        """
                    ),
                    {"tenant_id": tenant_id, "schema_name": schema_name},
                )
        finally:
            await engine.dispose()

    async def inspect() -> tuple[str, set[str]]:
        engine = create_async_engine(migration_database.url)
        try:
            async with engine.connect() as connection:
                revision = await connection.scalar(
                    text(
                        "SELECT revision FROM platform.tenant_schema_states "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
                tables = set(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT table_name
                                FROM information_schema.tables
                                WHERE table_schema = :schema_name
                                """
                            ),
                            {"schema_name": schema_name},
                        )
                    )
                    .scalars()
                    .all()
                )
                return str(revision), tables
        finally:
            await engine.dispose()

    asyncio.run(seed())
    revision, _tables = asyncio.run(inspect())
    assert revision == "20260730_0004"

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=tenant",
            f"tenant_schema={schema_name}",
        ),
    )
    revision, tables = asyncio.run(inspect())
    assert revision == HEAD_REVISION
    assert {"generation_jobs", "quota_ledger"}.issubset(tables)

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=tenant",
            f"tenant_schema={schema_name}",
            action="downgrade",
            revision="20260730_0004",
        ),
    )
    revision, tables = asyncio.run(inspect())
    assert revision == "20260730_0004"
    assert {"generation_jobs", "quota_ledger"}.isdisjoint(tables)

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(
            migration_database,
            "scope=tenant",
            f"tenant_schema={schema_name}",
        ),
    )
    revision, tables = asyncio.run(inspect())
    assert revision == HEAD_REVISION
    assert {"generation_jobs", "quota_ledger"}.issubset(tables)


def test_postgres_claims_are_unique_and_stale_same_owner_token_is_fenced(
    migration_database,
    monkeypatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from deeptutor.teaching.models import (
        Tenant,
        TenantProvisioningJob,
        TenantSchemaState,
    )
    from deeptutor.teaching.provisioning_worker import (
        TENANT_SCHEMA_REVISION,
        ProvisioningClaim,
        SchemaProvisioningResult,
        StorageProvisioningResult,
        build_default_policy_result,
    )
    from deeptutor.teaching.repositories.provisioning import (
        SqlAlchemyProvisioningRepository,
    )

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _settings, database_module = _install_source_runtime_database(
        monkeypatch,
        migration_database,
    )

    async def exercise() -> None:
        await database_module.dispose_platform_engine()
        repository = SqlAlchemyProvisioningRepository()
        try:
            async with database_module.platform_session() as session:
                async with session.begin():
                    for suffix in ("a", "b"):
                        tenant_id = f"worker-claim-{suffix}"
                        session.add(
                            Tenant(
                                id=tenant_id,
                                name=f"Worker Claim {suffix}",
                                status="provisioning",
                            )
                        )
                        session.add(
                            TenantProvisioningJob(
                                id=f"job-worker-claim-{suffix}",
                                tenant_id=tenant_id,
                                operation="provision",
                                status="pending",
                                attempt_count=0,
                            )
                        )

            claims = await asyncio.gather(
                repository.claim_next("worker-claim-one", lease_seconds=60),
                repository.claim_next("worker-claim-two", lease_seconds=60),
            )
            assert all(claim is not None for claim in claims)
            assert {claim.job_id for claim in claims if claim is not None} == {
                "job-worker-claim-a",
                "job-worker-claim-b",
            }
            assert len({claim.lease_owner for claim in claims if claim is not None}) == 2

            async with database_module.platform_session() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            """
                            UPDATE platform.tenant_provisioning_jobs
                            SET status = 'completed',
                                lease_owner = NULL,
                                lease_token = NULL,
                                lease_expires_at = NULL,
                                heartbeat_at = NULL,
                                completed_at = now()
                            WHERE id IN (
                                'job-worker-claim-a',
                                'job-worker-claim-b'
                            )
                            """
                        )
                    )
                    await session.execute(
                        text(
                            """
                            UPDATE platform.tenants
                            SET status = 'active'
                            WHERE id IN ('worker-claim-a', 'worker-claim-b')
                            """
                        )
                    )
                    session.add(
                        Tenant(
                            id="worker-stale",
                            name="Worker Stale",
                            status="provisioning",
                        )
                    )
                    session.add(
                        TenantProvisioningJob(
                            id="job-worker-stale",
                            tenant_id="worker-stale",
                            operation="provision",
                            status="running",
                            attempt_count=2,
                            lease_owner="worker-same",
                            lease_token="lease-old",
                            lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                            heartbeat_at=datetime.now(UTC) - timedelta(minutes=2),
                        )
                    )

            reclaimed = await repository.claim_next(
                "worker-same",
                lease_seconds=60,
            )
            assert reclaimed is not None
            assert reclaimed.tenant_id == "worker-stale"
            assert reclaimed.job_id == "job-worker-stale"
            assert reclaimed.attempt_count == 3
            assert reclaimed.lease_owner == "worker-same"
            assert reclaimed.lease_token != "lease-old"
            old_claim = ProvisioningClaim(
                tenant_id="worker-stale",
                job_id="job-worker-stale",
                attempt_count=2,
                lease_owner="worker-same",
                lease_token="lease-old",
            )
            schema_result = SchemaProvisioningResult(
                schema_name=tenant_schema_name("worker-stale"),
                revision=TENANT_SCHEMA_REVISION,
            )
            storage_result = StorageProvisioningResult.local("worker-stale")
            policy_result = build_default_policy_result()
            assert await repository.heartbeat(old_claim, lease_seconds=60) is False
            assert await repository.record_schema_ready(old_claim, schema_result) is False
            assert await repository.record_storage_ready(old_claim, storage_result) is False
            assert (
                await repository.record_default_policy_ready(
                    old_claim,
                    policy_result,
                )
                is False
            )
            assert await repository.record_schema_ready(reclaimed, schema_result) is True
            assert await repository.record_storage_ready(reclaimed, storage_result) is True
            assert (
                await repository.record_default_policy_ready(
                    reclaimed,
                    policy_result,
                )
                is True
            )
            assert (
                await repository.record_failure(
                    old_claim,
                    category="worker",
                    code="unexpected_error",
                    retryable=False,
                    backoff_seconds=5,
                )
                is False
            )
            assert await repository.activate(old_claim) is False
            assert await repository.activate(reclaimed) is True

            async with database_module.platform_session() as session:
                state = await session.get(TenantSchemaState, "worker-stale")
                assert state is not None
                assert state.revision == TENANT_SCHEMA_REVISION
                job = await session.get(TenantProvisioningJob, "job-worker-stale")
                assert job is not None
                assert job.status == "completed"
                assert job.lease_owner is None
                assert job.lease_token is None
                assert job.lease_expires_at is None
                assert job.heartbeat_at is None
        finally:
            await database_module.dispose_platform_engine()

    asyncio.run(exercise())


def test_stale_provisioning_claims_stop_at_max_attempts_without_deactivating_upgrade_tenant(
    migration_database,
    monkeypatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from deeptutor.teaching.models import (
        Tenant,
        TenantProvisioningJob,
        TenantSchemaState,
    )
    from deeptutor.teaching.repositories.provisioning import (
        SqlAlchemyProvisioningRepository,
    )

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _settings, database_module = _install_source_runtime_database(
        monkeypatch,
        migration_database,
    )

    async def exercise() -> None:
        await database_module.dispose_platform_engine()
        expired_at = datetime.now(UTC) - timedelta(minutes=1)
        repository = SqlAlchemyProvisioningRepository()
        try:
            async with database_module.platform_session() as session:
                async with session.begin():
                    session.add_all(
                        [
                            Tenant(
                                id="stale-provision-limit",
                                name="Stale provision limit",
                                status="provisioning",
                            ),
                            Tenant(
                                id="stale-upgrade-limit",
                                name="Stale upgrade limit",
                                status="active",
                            ),
                        ]
                    )
                    session.add(
                        TenantSchemaState(
                            tenant_id="stale-upgrade-limit",
                            schema_name=tenant_schema_name("stale-upgrade-limit"),
                            revision="20260801_0007",
                            status="active",
                        )
                    )
                    for tenant_id, operation in (
                        ("stale-provision-limit", "provision"),
                        ("stale-upgrade-limit", "upgrade_schema"),
                    ):
                        session.add(
                            TenantProvisioningJob(
                                id=f"job-{tenant_id}",
                                tenant_id=tenant_id,
                                operation=operation,
                                target_revision=(
                                    "20260802_0008" if operation == "upgrade_schema" else None
                                ),
                                status="running",
                                attempt_count=4,
                                max_attempts=5,
                                lease_owner="crashed-worker",
                                lease_token=f"token-{tenant_id}",
                                lease_expires_at=expired_at,
                                heartbeat_at=expired_at,
                            )
                        )

            assert await repository.claim_next("replacement-worker", lease_seconds=60) is None
            assert await repository.claim_next("replacement-worker", lease_seconds=60) is None

            async with database_module.platform_session() as session:
                provision_tenant = await session.get(Tenant, "stale-provision-limit")
                upgrade_tenant = await session.get(Tenant, "stale-upgrade-limit")
                provision_job = await session.get(
                    TenantProvisioningJob,
                    "job-stale-provision-limit",
                )
                upgrade_job = await session.get(
                    TenantProvisioningJob,
                    "job-stale-upgrade-limit",
                )
                assert provision_tenant is not None
                assert upgrade_tenant is not None
                assert provision_job is not None
                assert upgrade_job is not None
                assert provision_tenant.status == "failed"
                assert upgrade_tenant.status == "active"
                for job in (provision_job, upgrade_job):
                    assert job.status == "failed"
                    assert job.attempt_count == 4
                    assert job.lease_owner is None
                    assert job.lease_token is None
                    assert job.lease_expires_at is None
                    assert job.heartbeat_at is None
        finally:
            await database_module.dispose_platform_engine()

    asyncio.run(exercise())


def test_future_tenant_schema_revision_is_not_enqueued_or_claimed(
    migration_database,
    monkeypatch,
) -> None:
    from deeptutor.teaching.models import (
        Tenant,
        TenantProvisioningJob,
        TenantSchemaState,
    )
    from deeptutor.teaching.repositories.provisioning import (
        SqlAlchemyProvisioningRepository,
    )

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _settings, database_module = _install_source_runtime_database(
        monkeypatch,
        migration_database,
    )

    async def exercise() -> None:
        await database_module.dispose_platform_engine()
        repository = SqlAlchemyProvisioningRepository()
        try:
            async with database_module.platform_session() as session:
                async with session.begin():
                    session.add(
                        Tenant(
                            id="future-schema-tenant",
                            name="Future schema tenant",
                            status="active",
                        )
                    )
                    session.add(
                        TenantSchemaState(
                            tenant_id="future-schema-tenant",
                            schema_name=tenant_schema_name("future-schema-tenant"),
                            revision="20260802_0009",
                            status="active",
                        )
                    )
                    session.add(
                        TenantProvisioningJob(
                            id="future-schema-old-worker-job",
                            tenant_id="future-schema-tenant",
                            operation="upgrade_schema",
                            target_revision="20260802_0008",
                            status="pending",
                        )
                    )

            assert await repository.enqueue_next_schema_upgrade() is False
            assert await repository.claim_next("old-worker", lease_seconds=60) is None
            async with database_module.platform_session() as session:
                job = await session.get(
                    TenantProvisioningJob,
                    "future-schema-old-worker-job",
                )
                assert job is not None and job.status == "pending"
        finally:
            await database_module.dispose_platform_engine()

    asyncio.run(exercise())


def test_postgres_activation_requires_all_states_and_s3_credential_binding(
    migration_database,
    monkeypatch,
) -> None:
    from dataclasses import replace

    from deeptutor.teaching.models import (
        TenantDefaultPolicyState,
        TenantProvisioningJob,
        TenantStorageCredential,
        TenantStorageState,
    )
    from deeptutor.teaching.provisioning_worker import (
        TENANT_SCHEMA_REVISION,
        ProvisioningStepError,
        SchemaProvisioningResult,
        StorageProvisioningResult,
        build_default_policy_result,
    )
    from deeptutor.teaching.repositories.provisioning import (
        SqlAlchemyProvisioningRepository,
    )
    from deeptutor.teaching.repositories.tenants import TenantRepository

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _settings, database_module = _install_source_runtime_database(
        monkeypatch,
        migration_database,
    )

    async def exercise() -> None:
        await database_module.dispose_platform_engine()
        repository = SqlAlchemyProvisioningRepository()
        try:
            await TenantRepository().create_provisioning(
                tenant_id="activation-prerequisites",
                job_id="job-activation-prerequisites",
                name="Activation Prerequisites",
            )
            claim = await repository.claim_next(
                "worker-activation",
                lease_seconds=60,
            )
            assert claim is not None
            assert claim.tenant_id == "activation-prerequisites"
            assert await repository.activate(claim) is False

            schema_result = SchemaProvisioningResult(
                schema_name=tenant_schema_name(claim.tenant_id),
                revision=TENANT_SCHEMA_REVISION,
            )
            assert await repository.record_schema_ready(claim, schema_result) is True
            assert await repository.activate(claim) is False

            local_storage = StorageProvisioningResult.local(claim.tenant_id)
            cross_tenant_storage = replace(
                local_storage,
                mode="s3",
                secret_ref="another-tenant/object-store",
                access_key_fingerprint="a" * 64,
            )
            with pytest.raises(ProvisioningStepError):
                await repository.record_storage_ready(
                    claim,
                    cross_tenant_storage,
                )
            assert await repository.activate(claim) is False
            async with database_module.platform_session() as session:
                assert await session.get(TenantStorageCredential, claim.tenant_id) is None
                assert await session.get(TenantStorageState, claim.tenant_id) is None

            storage_result = replace(
                local_storage,
                mode="s3",
                secret_ref="activation-prerequisites/object-store",
                access_key_fingerprint="a" * 64,
            )
            assert await repository.record_storage_ready(claim, storage_result) is True
            assert await repository.activate(claim) is False

            policy_result = build_default_policy_result()
            assert await repository.record_default_policy_ready(claim, policy_result) is True
            async with database_module.platform_session() as session:
                async with session.begin():
                    credential = await session.get(
                        TenantStorageCredential,
                        claim.tenant_id,
                    )
                    assert credential is not None
                    credential.access_key_fingerprint = "b" * 64
            assert await repository.activate(claim) is False

            async with database_module.platform_session() as session:
                async with session.begin():
                    credential = await session.get(
                        TenantStorageCredential,
                        claim.tenant_id,
                    )
                    assert credential is not None
                    credential.access_key_fingerprint = "a" * 64
            assert await repository.activate(claim) is True

            async with database_module.platform_session() as session:
                job = await session.get(
                    TenantProvisioningJob,
                    "job-activation-prerequisites",
                )
                policy = await session.get(
                    TenantDefaultPolicyState,
                    "activation-prerequisites",
                )
                assert job is not None
                assert job.status == "completed"
                assert job.completed_at is not None
                assert job.lease_owner is None
                assert policy is not None
                payload = json.loads(policy.policy_payload)
                assert payload["network_access_enabled"] is False
                assert payload["open_creation_enabled"] is False
                assert payload["external_media_enabled"] is False
        finally:
            await database_module.dispose_platform_engine()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    (
        "failed_step",
        "category",
        "code",
        "retryable",
        "expected_tenant_status",
        "expected_job_status",
        "expected_attempt_count",
        "max_attempts",
    ),
    [
        (
            "schema",
            "schema",
            "migration_unavailable",
            True,
            "provisioning",
            "pending",
            1,
            5,
        ),
        (
            "schema-exhausted",
            "schema",
            "migration_unavailable",
            True,
            "failed",
            "failed",
            0,
            1,
        ),
        (
            "storage",
            "storage",
            "admin_unavailable",
            False,
            "failed",
            "failed",
            0,
            5,
        ),
        (
            "policy",
            "policy",
            "invalid_default",
            False,
            "failed",
            "failed",
            0,
            5,
        ),
    ],
)
def test_postgres_step_failures_never_activate_and_persist_fixed_retry_state(
    migration_database,
    monkeypatch,
    failed_step,
    category,
    code,
    retryable,
    expected_tenant_status,
    expected_job_status,
    expected_attempt_count,
    max_attempts,
) -> None:
    from deeptutor.teaching.models import Tenant, TenantProvisioningJob
    from deeptutor.teaching.provisioning_worker import (
        TENANT_SCHEMA_REVISION,
        ProvisioningStepError,
        ProvisioningWorker,
        SchemaProvisioningResult,
        StorageProvisioningResult,
        build_default_policy_result,
    )
    from deeptutor.teaching.repositories.provisioning import (
        SqlAlchemyProvisioningRepository,
    )
    from deeptutor.teaching.repositories.tenants import TenantRepository

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    _settings, database_module = _install_source_runtime_database(
        monkeypatch,
        migration_database,
    )
    suffix = f"{failed_step}-{code}".replace("_", "-")
    tenant_id = f"failure-{suffix}"
    job_id = f"job-failure-{suffix}"

    class Schema:
        async def provision(self, requested_tenant_id):
            if failed_step.startswith("schema"):
                raise ProvisioningStepError(
                    category=category,
                    code=code,
                    retryable=retryable,
                )
            return SchemaProvisioningResult(
                schema_name=tenant_schema_name(requested_tenant_id),
                revision=TENANT_SCHEMA_REVISION,
            )

    class Storage:
        async def provision(self, requested_tenant_id):
            if failed_step == "storage":
                raise ProvisioningStepError(
                    category=category,
                    code=code,
                    retryable=retryable,
                )
            return StorageProvisioningResult.local(requested_tenant_id)

    class Policy:
        async def provision(self, requested_tenant_id):
            if failed_step == "policy":
                raise ProvisioningStepError(
                    category=category,
                    code=code,
                    retryable=retryable,
                )
            return build_default_policy_result()

    async def exercise() -> None:
        await database_module.dispose_platform_engine()
        try:
            await TenantRepository().create_provisioning(
                tenant_id=tenant_id,
                job_id=job_id,
                name=f"Failure {failed_step}",
            )
            if max_attempts != 5:
                async with database_module.platform_session() as session:
                    async with session.begin():
                        job = await session.get(TenantProvisioningJob, job_id)
                        assert job is not None
                        job.max_attempts = max_attempts
            worker = ProvisioningWorker(
                enabled=True,
                worker_id=f"worker-failure-{failed_step}",
                repository=SqlAlchemyProvisioningRepository(),
                schema_provisioner=Schema(),
                storage_provisioner=Storage(),
                policy_provisioner=Policy(),
                lease_seconds=60,
            )
            assert await worker.run_once() is True
            async with database_module.platform_session() as session:
                tenant = await session.get(Tenant, tenant_id)
                job = await session.get(TenantProvisioningJob, job_id)
                assert tenant is not None
                assert job is not None
                assert tenant.status == expected_tenant_status
                assert job.status == expected_job_status
                assert job.attempt_count == expected_attempt_count
                assert job.error_category == category
                assert job.error_code == code
                assert job.lease_owner is None
                if expected_job_status == "pending":
                    assert job.completed_at is None
                else:
                    assert job.completed_at is not None

            if expected_job_status == "pending":
                async with database_module.platform_session() as session:
                    async with session.begin():
                        tenant = await session.get(Tenant, tenant_id)
                        job = await session.get(TenantProvisioningJob, job_id)
                        assert tenant is not None
                        assert job is not None
                        tenant.status = "failed"
                        job.status = "failed"
        finally:
            await database_module.dispose_platform_engine()

    asyncio.run(exercise())


def test_create_intent_runs_to_active_with_real_schema_revision_and_local_storage(
    migration_database,
    monkeypatch,
    tmp_path,
) -> None:
    from deeptutor.services import config as config_module
    from deeptutor.teaching.models import (
        AuditLog,
        Tenant,
        TenantDefaultPolicyState,
        TenantProvisioningJob,
        TenantSchemaState,
        TenantStorageState,
    )
    from deeptutor.teaching.provisioning_worker import (
        TENANT_SCHEMA_REVISION,
        AlembicTenantSchemaProvisioner,
        FixedTenantPolicyProvisioner,
        LocalTenantStorageProvisioner,
        ProvisioningWorker,
    )
    from deeptutor.teaching.repositories.provisioning import (
        SqlAlchemyProvisioningRepository,
    )
    from deeptutor.teaching.repositories.tenants import TenantRepository
    from deeptutor.teaching.services.tenant_provisioning import (
        TenantProvisioningService,
    )

    _assert_migration_succeeded(
        migration_database,
        _run_alembic(migration_database, "scope=platform"),
    )
    settings, database_module = _install_source_runtime_database(
        monkeypatch,
        migration_database,
    )
    monkeypatch.setattr(
        config_module,
        "load_platform_settings",
        lambda: settings,
    )

    async def exercise() -> None:
        await database_module.dispose_platform_engine()
        try:
            service = TenantProvisioningService(TenantRepository())
            intent = await service.create(
                actor_id="worker-e2e-admin",
                name="Worker E2E Tenant",
                idempotency_key="worker-e2e-intent",
            )
            assert intent.status == "provisioning"
            worker = ProvisioningWorker(
                enabled=True,
                worker_id="worker-e2e",
                repository=SqlAlchemyProvisioningRepository(),
                schema_provisioner=AlembicTenantSchemaProvisioner(),
                storage_provisioner=LocalTenantStorageProvisioner(
                    tmp_path / "objects",
                ),
                policy_provisioner=FixedTenantPolicyProvisioner(),
                lease_seconds=60,
                heartbeat_interval_seconds=0.5,
            )
            safe_state = None
            for attempt_index in range(3):
                assert await worker.run_once() is True
                async with database_module.platform_session() as session:
                    tenant = await session.get(Tenant, intent.tenant_id)
                    job = await session.get(TenantProvisioningJob, intent.job_id)
                    database_now = await session.scalar(select(func.now()))
                    assert tenant is not None
                    assert job is not None
                    safe_state = (
                        tenant.status,
                        job.status,
                        job.error_category,
                        job.error_code,
                    )
                    if tenant.status == "active" and job.status == "completed":
                        break
                    assert job.status == "pending", (
                        f"provisioning stopped in safe state {safe_state!r}"
                    )
                    assert (job.error_category, job.error_code) in {
                        ("infrastructure", "temporarily_unavailable"),
                        ("schema", "migration_unavailable"),
                    }
                    assert database_now is not None
                    delay_seconds = max(
                        0.0,
                        (job.next_attempt_at - database_now).total_seconds(),
                    )
                if attempt_index == 2:
                    pytest.fail(
                        f"provisioning did not activate: {safe_state!r}",
                    )
                await asyncio.sleep(min(delay_seconds + 0.05, 10.05))
            else:
                pytest.fail(f"provisioning did not activate: {safe_state!r}")

            schema_name = tenant_schema_name(intent.tenant_id)
            async with database_module.platform_session() as session:
                tenant = await session.get(Tenant, intent.tenant_id)
                job = await session.get(TenantProvisioningJob, intent.job_id)
                schema_state = await session.get(TenantSchemaState, intent.tenant_id)
                storage_state = await session.get(TenantStorageState, intent.tenant_id)
                policy_state = await session.get(
                    TenantDefaultPolicyState,
                    intent.tenant_id,
                )
                audits = (
                    (
                        await session.execute(
                            select(AuditLog.action)
                            .where(AuditLog.tenant_id == intent.tenant_id)
                            .order_by(AuditLog.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert tenant is not None and tenant.status == "active"
                assert job is not None and job.status == "completed"
                assert schema_state is not None
                assert schema_state.schema_name == schema_name
                assert schema_state.revision == TENANT_SCHEMA_REVISION
                assert storage_state is not None
                assert storage_state.mode == "local"
                assert storage_state.credential_secret_ref is None
                assert storage_state.credential_fingerprint is None
                assert policy_state is not None
                policy_payload = json.loads(policy_state.policy_payload)
                assert policy_payload == {
                    "classroom_visibility": "tenant_members",
                    "external_media_enabled": False,
                    "generation_concurrency_limit": 2,
                    "membership_management": "tenant_admins",
                    "network_access_enabled": False,
                    "open_creation_enabled": False,
                }
                assert audits == [
                    "tenant.provisioning.attempt_started",
                    "tenant.provisioning.schema_ready",
                    "tenant.provisioning.storage_ready",
                    "tenant.provisioning.default_policy_ready",
                    "tenant.provisioning.completed",
                ]

            engine = create_async_engine(migration_database.url)
            try:
                async with engine.connect() as connection:
                    revision = await connection.scalar(
                        text(f'SELECT version_num FROM "{schema_name}".alembic_version')
                    )
                    assert revision == TENANT_SCHEMA_REVISION
            finally:
                await engine.dispose()
            assert (tmp_path / "objects" / "tenants" / intent.tenant_id).is_dir()
        finally:
            await database_module.dispose_platform_engine()

    asyncio.run(exercise())


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
