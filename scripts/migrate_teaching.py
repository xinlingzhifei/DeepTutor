"""Bootstrap fixed database roles and migrate platform then tenant schemas."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from deeptutor.services.config import PlatformSettings, load_platform_settings
from deeptutor.teaching.migrations.runner import run_migration


@dataclass(frozen=True, slots=True)
class DatabaseRoleStatement:
    sql: str
    parameters: dict[str, str]
    renders_ddl: bool = False


def build_database_role_statements(
    *,
    app_password: str,
    migration_password: str,
) -> tuple[DatabaseRoleStatement, ...]:
    """Return fixed-identifier DDL with passwords kept in bind parameters."""

    return (
        DatabaseRoleStatement(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
            "'yfeistai_app') THEN CREATE ROLE yfeistai_app LOGIN; END IF; END $$",
            {},
        ),
        DatabaseRoleStatement(
            "SELECT format('ALTER ROLE %I LOGIN NOCREATEDB NOCREATEROLE "
            "NOREPLICATION PASSWORD %L', CAST(:role AS text), "
            "CAST(:password AS text))",
            {"role": "yfeistai_app", "password": app_password},
            renders_ddl=True,
        ),
        DatabaseRoleStatement(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
            "'yfeistai_migrator') THEN CREATE ROLE yfeistai_migrator LOGIN; END IF; END $$",
            {},
        ),
        DatabaseRoleStatement(
            "SELECT format('ALTER ROLE %I LOGIN NOCREATEDB NOCREATEROLE "
            "NOREPLICATION PASSWORD %L', CAST(:role AS text), "
            "CAST(:password AS text))",
            {"role": "yfeistai_migrator", "password": migration_password},
            renders_ddl=True,
        ),
    )


class TenantMigrationError(RuntimeError):
    def __init__(self, tenant_id: str, schema_name: str, revision: str | None) -> None:
        safe_revision = revision or "unknown"
        super().__init__(
            f"tenant migration failed: tenant={tenant_id} "
            f"schema={schema_name} revision={safe_revision}"
        )


async def migrate_tenant_schemas(
    tenants: Iterable[tuple[str, str, str | None]],
    *,
    migrate: Callable[[str], Awaitable[None]],
) -> None:
    for tenant_id, schema_name, revision in tenants:
        try:
            await migrate(schema_name)
        except Exception:
            raise TenantMigrationError(tenant_id, schema_name, revision) from None


def _read_secret(path: Path, label: str) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} secret is unavailable")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError):
        raise ValueError(f"{label} secret could not be read") from None
    if not value or "\x00" in value:
        raise ValueError(f"{label} secret is empty")
    return value


def _database_url(settings: PlatformSettings, *, user: str, password: str) -> str:
    return URL.create(
        drivername="postgresql+asyncpg",
        username=user,
        password=password,
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
    ).render_as_string(hide_password=False)


async def _execute_role_bootstrap(
    engine: AsyncEngine,
    *,
    app_password: str,
    migration_password: str,
) -> None:
    async with engine.begin() as connection:
        for statement in build_database_role_statements(
            app_password=app_password,
            migration_password=migration_password,
        ):
            result = await connection.execute(text(statement.sql), statement.parameters)
            if statement.renders_ddl:
                await connection.execute(text(result.scalar_one()))
        await connection.execute(
            text(
                "GRANT CONNECT ON DATABASE "
                '"' + engine.url.database.replace('"', '""') + '" '
                "TO yfeistai_app, yfeistai_migrator"
            )
        )
        await connection.execute(
            text(
                "GRANT CREATE ON DATABASE "
                '"' + engine.url.database.replace('"', '""') + '" '
                "TO yfeistai_migrator"
            )
        )


async def _tenant_rows(engine: AsyncEngine) -> tuple[tuple[str, str, str | None], ...]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT tenants.id, states.schema_name, states.revision "
                    "FROM platform.tenants AS tenants "
                    "JOIN platform.tenant_schema_states AS states "
                    "ON states.tenant_id = tenants.id "
                    "ORDER BY tenants.id"
                )
            )
        ).all()
    return tuple((str(row[0]), str(row[1]), row[2]) for row in rows)


async def _grant_app_access(engine: AsyncEngine, schemas: Sequence[str]) -> None:
    async with engine.begin() as connection:
        for schema in ("platform", *schemas):
            if not (schema == "platform" or schema.startswith("tenant_")):
                raise ValueError("database schema is unsafe")
            quoted = '"' + schema.replace('"', '""') + '"'
            await connection.execute(text(f"GRANT USAGE ON SCHEMA {quoted} TO yfeistai_app"))
            await connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                    f"{quoted} TO yfeistai_app"
                )
            )
            await connection.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {quoted} TO yfeistai_app")
            )
            await connection.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE yfeistai_migrator IN SCHEMA "
                    f"{quoted} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO yfeistai_app"
                )
            )
            await connection.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE yfeistai_migrator IN SCHEMA "
                    f"{quoted} GRANT USAGE, SELECT ON SEQUENCES TO yfeistai_app"
                )
            )


@contextmanager
def _migration_database_url(url: str):
    previous = os.environ.get("DEEPTUTOR_PLATFORM_DATABASE_URL")
    os.environ["DEEPTUTOR_PLATFORM_DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DEEPTUTOR_PLATFORM_DATABASE_URL", None)
        else:
            os.environ["DEEPTUTOR_PLATFORM_DATABASE_URL"] = previous


async def migrate_platform_and_tenants(settings: PlatformSettings) -> None:
    admin_password_file = Path(
        os.environ.get(
            "YFEISTAI_PLATFORM_ADMIN_PASSWORD_FILE",
            "/run/secrets/platform_database_password",
        )
    )
    app_password_file = Path(
        os.environ.get(
            "YFEISTAI_PLATFORM_APP_PASSWORD_FILE",
            "/run/secrets/platform_database_app_password",
        )
    )
    migration_password_file = Path(
        os.environ.get(
            "YFEISTAI_PLATFORM_MIGRATION_PASSWORD_FILE",
            "/run/secrets/platform_database_migration_password",
        )
    )
    admin_password = _read_secret(admin_password_file, "platform database admin")
    app_password = _read_secret(app_password_file, "platform database app")
    migration_password = _read_secret(
        migration_password_file,
        "platform database migration",
    )
    admin_url = _database_url(settings, user="yfeistai_admin", password=admin_password)
    migration_url = _database_url(
        settings,
        user="yfeistai_migrator",
        password=migration_password,
    )
    admin_engine = create_async_engine(admin_url)
    try:
        await _execute_role_bootstrap(
            admin_engine,
            app_password=app_password,
            migration_password=migration_password,
        )
    finally:
        await admin_engine.dispose()

    with _migration_database_url(migration_url):
        await asyncio.to_thread(run_migration, action="upgrade", scope="platform")
        migration_engine = create_async_engine(migration_url)
        try:
            tenants = await _tenant_rows(migration_engine)

            async def migrate(schema_name: str) -> None:
                await asyncio.to_thread(
                    run_migration,
                    action="upgrade",
                    scope="tenant",
                    tenant_schema=schema_name,
                )

            await migrate_tenant_schemas(tenants, migrate=migrate)
            await _grant_app_access(
                migration_engine,
                tuple(schema for _tenant, schema, _revision in tenants),
            )
        finally:
            await migration_engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate teaching platform and tenants")
    parser.add_argument("--config", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    settings = load_platform_settings(arguments.config)
    if not settings.enabled:
        raise SystemExit("teaching migration requires an enabled platform")
    asyncio.run(migrate_platform_and_tenants(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
