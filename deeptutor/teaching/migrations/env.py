"""Alembic environment for isolated platform and tenant migrations."""

from __future__ import annotations

import asyncio
import re
from typing import Literal, NamedTuple

from alembic import context
from alembic.util import CommandError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema

from deeptutor.services.config import load_platform_settings

_TENANT_SCHEMA_PATTERN = re.compile(r"tenant_[0-9a-f]{16}")
_SUPPORTED_COMMANDS = {"upgrade", "downgrade"}


class MigrationScope(NamedTuple):
    name: Literal["platform", "tenant"]
    schema: str


def _validate_cli_command() -> None:
    command_options = context.config.cmd_opts
    command_tuple = getattr(command_options, "cmd", None)
    command_function = (
        command_tuple[0]
        if isinstance(command_tuple, tuple) and command_tuple
        else None
    )
    command_name = getattr(command_function, "__name__", None)
    if command_name not in _SUPPORTED_COMMANDS:
        raise CommandError(
            "teaching migrations support only upgrade and downgrade"
        )


def _parse_x_arguments() -> MigrationScope:
    parsed: dict[str, str] = {}
    for raw_argument in context.get_x_argument():
        key, separator, value = raw_argument.partition("=")
        if not separator or not key or key in parsed:
            raise CommandError("migration -x arguments are invalid")
        parsed[key] = value

    scope = parsed.get("scope")
    if scope == "platform" and set(parsed) == {"scope"}:
        return MigrationScope("platform", "platform")

    if scope == "tenant" and set(parsed) == {"scope", "tenant_schema"}:
        tenant_schema = parsed["tenant_schema"]
        if _TENANT_SCHEMA_PATTERN.fullmatch(tenant_schema):
            return MigrationScope("tenant", tenant_schema)
        raise CommandError("tenant_schema must match tenant_[0-9a-f]{16}")

    raise CommandError("scope must be exactly platform or tenant")


def _load_database_url() -> str:
    try:
        settings = load_platform_settings()
    except Exception:
        raise CommandError("platform database settings are invalid") from None
    if not settings.enabled:
        raise CommandError("platform database is disabled")
    if settings.database_url is None:
        raise CommandError("platform database URL is unavailable")
    return settings.database_url.get_secret_value()


def _run_migrations(
    connection,
    migration_scope: MigrationScope,
) -> None:
    context.configure(
        connection=connection,
        version_table="alembic_version",
        version_table_schema=migration_scope.schema,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online_migrations(migration_scope: MigrationScope) -> None:
    engine = create_async_engine(
        _load_database_url(),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(
                CreateSchema(migration_scope.schema, if_not_exists=True)
            )
            await connection.commit()

            migration_connection: AsyncConnection = connection
            if migration_scope.name == "tenant":
                migration_connection = await connection.execution_options(
                    schema_translate_map={"tenant": migration_scope.schema}
                )
            await migration_connection.run_sync(
                _run_migrations,
                migration_scope,
            )
    finally:
        await engine.dispose()


_validate_cli_command()

if context.is_offline_mode():
    raise CommandError("offline teaching migrations are not supported")

migration_scope = _parse_x_arguments()
try:
    asyncio.run(_run_online_migrations(migration_scope))
except CommandError:
    raise
except Exception as exc:
    raise CommandError(
        f"database migration failed ({type(exc).__name__})"
    ) from None
