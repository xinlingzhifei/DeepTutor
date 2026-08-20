from __future__ import annotations

from functools import cache
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from pydantic import SecretStr
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[3]


def _database_url(request: pytest.FixtureRequest) -> str:
    external = os.environ.get("YFEISTAI_TEST_POSTGRES_URL")
    if external is not None:
        return external
    return request.getfixturevalue("generation_database").url


@cache
def _migration_script():
    path = ROOT / "scripts" / "migrate_teaching.py"
    spec = importlib.util.spec_from_file_location("migrate_teaching_role_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_database_roles_authenticate_with_least_privilege(
    request: pytest.FixtureRequest,
) -> None:
    database_url = _database_url(request)
    app_password = "APP_ROLE_PASSWORD_SENTINEL_2026"
    migration_password = "MIGRATION_ROLE_PASSWORD_SENTINEL_2026"
    admin_engine = create_async_engine(database_url)
    try:
        await _migration_script()._execute_role_bootstrap(
            admin_engine,
            app_password=app_password,
            migration_password=migration_password,
        )
        async with admin_engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole "
                        "FROM pg_roles WHERE rolname IN "
                        "('yfeistai_app', 'yfeistai_migrator') ORDER BY rolname"
                    )
                )
            ).all()
        assert rows == [
            ("yfeistai_app", False, False, False),
            ("yfeistai_migrator", False, False, False),
        ]
    finally:
        await admin_engine.dispose()

    base_url = make_url(database_url)
    app_url = base_url.set(
        username="yfeistai_app",
        password=app_password,
    ).render_as_string(hide_password=False)
    migrator_url = base_url.set(
        username="yfeistai_migrator",
        password=migration_password,
    ).render_as_string(hide_password=False)

    app_engine = create_async_engine(app_url)
    try:
        async with app_engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            with pytest.raises(DBAPIError):
                await connection.execute(text("CREATE SCHEMA app_must_not_create"))
            await connection.rollback()
    finally:
        await app_engine.dispose()

    migrator_engine = create_async_engine(migrator_url)
    try:
        async with migrator_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("CREATE SCHEMA migrator_can_create"))
            await transaction.rollback()
    finally:
        await migrator_engine.dispose()


@pytest.mark.asyncio
async def test_preflight_requires_current_platform_and_tenant_migrations(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    from deeptutor.services.config import PlatformSettings
    from deeptutor.teaching.provisioning_worker import TENANT_SCHEMA_REVISION
    from deeptutor.teaching.schema_names import tenant_schema_name
    from scripts.platform_preflight import _inspect_database_runtime

    database_url = _database_url(request)
    settings_dir = tmp_path / "data" / "user" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "platform.json").write_text(
        json.dumps({"enabled": True}),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["DEEPTUTOR_HOME"] = str(tmp_path)
    environment["DEEPTUTOR_PLATFORM_DATABASE_URL"] = database_url
    platform_migration = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", "scope=platform", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert platform_migration.returncode == 0, (
        f"{platform_migration.stdout}\n{platform_migration.stderr}"
    )

    tenant_id = "preflight-tenant"
    schema_name = tenant_schema_name(tenant_id)
    tenant_migration = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            "scope=tenant",
            "-x",
            f"tenant_schema={schema_name}",
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert tenant_migration.returncode == 0, f"{tenant_migration.stdout}\n{tenant_migration.stderr}"

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform.tenants (id, name, status, data_plane_mode) "
                    "VALUES (:tenant_id, 'Preflight', 'active', 'shared') "
                    "ON CONFLICT (id) DO UPDATE SET status = 'active'"
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.tenant_schema_states "
                    "(tenant_id, schema_name, revision, status) "
                    "VALUES (:tenant_id, :schema_name, :revision, 'active') "
                    "ON CONFLICT (tenant_id) DO UPDATE SET "
                    "schema_name = EXCLUDED.schema_name, revision = EXCLUDED.revision, "
                    "status = 'active'"
                ),
                {
                    "tenant_id": tenant_id,
                    "schema_name": schema_name,
                    "revision": TENANT_SCHEMA_REVISION,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.tenant_storage_credentials "
                    "(tenant_id, secret_ref, access_key_fingerprint, status) "
                    "VALUES (:tenant_id, 'tenant_preflight', :fingerprint, 'pending') "
                    "ON CONFLICT (tenant_id) DO UPDATE SET "
                    "secret_ref = EXCLUDED.secret_ref, "
                    "access_key_fingerprint = EXCLUDED.access_key_fingerprint, "
                    "status = 'pending'"
                ),
                {"tenant_id": tenant_id, "fingerprint": "a" * 64},
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.provider_profiles "
                    "(id, scope, tenant_id, owner_key, provider_type, model_name, "
                    "api_base_url, secret_ref, status) VALUES "
                    "('preflight-shared-profile', 'shared', NULL, 'shared', "
                    "'openmaic', 'openmaic-0.3.1', NULL, "
                    "'providers/shared/preflight', 'active') "
                    "ON CONFLICT (id) DO NOTHING"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO platform.data_plane_routes "
                    "(id, tenant_id, owner_key, mode, base_url, worker_pool, "
                    "queue_name, provider_profile_id, status, health_status) VALUES "
                    "('preflight-shared-route', NULL, 'shared', 'shared', "
                    "'http://openmaic:3000', 'preflight-shared-pool', "
                    "'preflight-shared-queue', 'preflight-shared-profile', "
                    "'active', 'healthy') ON CONFLICT (id) DO NOTHING"
                )
            )
    finally:
        await engine.dispose()

    inspection = await _inspect_database_runtime(
        PlatformSettings(
            enabled=True,
            database_url=SecretStr(database_url),
        )
    )

    assert inspection.errors == ()
    assert inspection.active_tenants[0].secret_ref is None

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE platform.tenant_storage_credentials "
                    "SET status = 'active' WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
    finally:
        await engine.dispose()

    inspection = await _inspect_database_runtime(
        PlatformSettings(
            enabled=True,
            database_url=SecretStr(database_url),
        )
    )

    assert inspection.errors == ()
    assert inspection.active_tenants == (
        type(inspection.active_tenants[0])(
            tenant_id=tenant_id,
            schema_name=schema_name,
            secret_ref="tenant_preflight",
        ),
    )
    assert inspection.shared_route == (
        "preflight-shared-route",
        "http://openmaic:3000",
    )
