"""Supported installed-artifact entry point for teaching migrations."""

from __future__ import annotations

from argparse import Namespace
from importlib import resources
from pathlib import Path
import re
from typing import Literal, NamedTuple

from alembic import command, context
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import TextClause

from deeptutor.services.config import load_platform_settings

_TENANT_SCHEMA_PATTERN = re.compile(r"tenant_[0-9a-f]{16}")
_SUPPORTED_ACTIONS = frozenset({"upgrade", "downgrade"})
MIGRATION_LOCK_TIMEOUT_MS = 30_000
MIGRATION_STATEMENT_TIMEOUT_MS = 900_000
TEACHING_MIGRATION_HEAD_REVISION = "20260830_0023"


class MigrationUnavailableError(CommandError):
    """Fixed machine-readable transient migration failure."""

    code = "migration_unavailable"

    def __init__(self) -> None:
        super().__init__("database migration is temporarily unavailable")


def load_migration_database_url() -> str:
    """Load one fail-closed database URL without exposing its value."""

    try:
        settings = load_platform_settings()
    except Exception:
        raise CommandError("platform database settings are invalid") from None
    if not settings.enabled:
        raise CommandError("platform database is disabled")
    if settings.database_url is None:
        raise CommandError("platform database URL is unavailable")
    return settings.database_url.get_secret_value()


def is_transient_database_error(exc: Exception) -> bool:
    """Classify database availability failures by exception type."""

    if isinstance(
        exc,
        (
            sqlalchemy_exc.DataError,
            sqlalchemy_exc.IntegrityError,
            sqlalchemy_exc.NotSupportedError,
            sqlalchemy_exc.ProgrammingError,
        ),
    ):
        return False
    return isinstance(
        exc,
        (
            ConnectionError,
            sqlalchemy_exc.DisconnectionError,
            sqlalchemy_exc.InterfaceError,
            sqlalchemy_exc.OperationalError,
            OSError,
            sqlalchemy_exc.TimeoutError,
            TimeoutError,
        ),
    ) or (isinstance(exc, sqlalchemy_exc.DBAPIError) and exc.connection_invalidated)


def translate_migration_runtime_error(exc: Exception) -> CommandError:
    """Preserve transient connection failures without parsing messages."""

    if isinstance(exc, MigrationUnavailableError):
        return exc
    if is_transient_database_error(exc):
        return MigrationUnavailableError()
    if isinstance(exc, CommandError):
        return exc
    return CommandError(f"database migration failed ({type(exc).__name__})")


class MigrationScope(NamedTuple):
    name: Literal["platform", "tenant"]
    schema: str


def validate_migration_scope(
    scope: str,
    tenant_schema: str | None,
) -> MigrationScope:
    """Validate one exact platform or tenant migration target."""

    if scope == "platform" and tenant_schema is None:
        return MigrationScope("platform", "platform")
    if scope == "tenant":
        if tenant_schema is None:
            raise CommandError("scope must be exactly platform or tenant")
        if _TENANT_SCHEMA_PATTERN.fullmatch(tenant_schema):
            return MigrationScope("tenant", tenant_schema)
        raise CommandError("tenant_schema must match tenant_[0-9a-f]{16}")
    raise CommandError("scope must be exactly platform or tenant")


def _migration_resource_root() -> Path:
    traversable = resources.files("deeptutor.teaching.migrations")
    try:
        root = Path(traversable)
    except TypeError:
        raise CommandError("packaged migration resources are unavailable") from None
    required_paths = (
        root / "env.py",
        root / "script.py.mako",
        root / "versions",
    )
    if (
        not root.is_dir()
        or not required_paths[0].is_file()
        or not required_paths[1].is_file()
        or not required_paths[2].is_dir()
        or not any(
            path.is_file() and path.name != "__init__.py" and path.suffix == ".py"
            for path in required_paths[2].iterdir()
        )
    ):
        raise CommandError("packaged migration resources are unavailable")
    return root


def build_alembic_config(
    *,
    action: str,
    scope: str,
    tenant_schema: str | None = None,
) -> Config:
    """Build a resource-backed Alembic config without a checkout-local ini."""

    if action not in _SUPPORTED_ACTIONS:
        raise CommandError("teaching migrations support only upgrade and downgrade")
    migration_scope = validate_migration_scope(scope, tenant_schema)
    operation = getattr(command, action)
    x_arguments = [f"scope={migration_scope.name}"]
    if migration_scope.name == "tenant":
        x_arguments.append(f"tenant_schema={migration_scope.schema}")
    config = Config(
        cmd_opts=Namespace(
            cmd=(operation,),
            x=x_arguments,
        )
    )
    config.set_main_option("script_location", str(_migration_resource_root()))
    return config


def _run_migrations_in_transaction(
    connection: Connection,
    migration_scope: MigrationScope,
) -> None:
    """Configure one bounded Alembic transaction on its sync connection."""

    context.configure(
        connection=connection,
        version_table="alembic_version",
        version_table_schema=migration_scope.schema,
    )
    with context.begin_transaction():
        connection.execute(*_migration_timeout_command())
        context.run_migrations()


def _migration_timeout_command() -> tuple[TextClause, dict[str, str]]:
    return (
        text(
            "SELECT set_config('lock_timeout', :lock_timeout, true), "
            "set_config('statement_timeout', :statement_timeout, true)"
        ),
        {
            "lock_timeout": f"{MIGRATION_LOCK_TIMEOUT_MS}ms",
            "statement_timeout": f"{MIGRATION_STATEMENT_TIMEOUT_MS}ms",
        },
    )


def _run_migration_unlocked(
    *,
    action: str,
    scope: str,
    tenant_schema: str | None = None,
) -> None:
    """Run Alembic after the caller has acquired the required session locks."""

    config = build_alembic_config(
        action=action,
        scope=scope,
        tenant_schema=tenant_schema,
    )
    revision = "head" if action == "upgrade" else "base"
    getattr(command, action)(config, revision)
