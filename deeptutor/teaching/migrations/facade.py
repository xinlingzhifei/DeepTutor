"""Lock-aware facade for every formal teaching migration path."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from alembic.util import CommandError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.migrations.runner import (
    TEACHING_MIGRATION_HEAD_REVISION,
    MigrationScope,
    MigrationUnavailableError,
    _run_migration_unlocked,
    is_transient_database_error,
    load_migration_database_url,
    translate_migration_runtime_error,
    validate_migration_scope,
)
from deeptutor.teaching.tenant_directory_lock import (
    run_sync_in_thread_cancellation_safe,
    tenant_directory_session_lock,
    tenant_schema_migration_session_lock,
)


class TenantMigrationVerificationUnavailableError(MigrationUnavailableError):
    """The migrated tenant revision could not be read temporarily."""

    code = "verification_unavailable"

    def __init__(self) -> None:
        CommandError.__init__(
            self,
            "tenant migration revision is temporarily unavailable",
        )


class TenantMigrationVerificationFailedError(CommandError):
    """The migrated tenant revision could not be read deterministically."""

    def __init__(self) -> None:
        super().__init__("tenant migration revision verification failed")


class TenantMigrationRevisionMismatchError(CommandError):
    """The tenant revision does not match the requested migration target."""

    def __init__(self) -> None:
        super().__init__("tenant migration revision does not match its target")


@dataclass(frozen=True, slots=True)
class LockedMigrationLease:
    """A migration connection with an explicit advisory-lock coverage scope."""

    connection: AsyncConnection
    scope: MigrationScope

    async def run(
        self,
        *,
        action: str,
        scope: str,
        tenant_schema: str | None = None,
    ) -> str | None:
        migration_scope = validate_migration_scope(scope, tenant_schema)
        if self.scope.name == "tenant" and migration_scope != self.scope:
            raise CommandError("migration target is outside the held lock scope")

        await run_sync_in_thread_cancellation_safe(
            _run_migration_unlocked,
            action=action,
            scope=migration_scope.name,
            tenant_schema=(migration_scope.schema if migration_scope.name == "tenant" else None),
        )
        if migration_scope.name == "platform":
            return None

        try:
            revision = await self.connection.scalar(
                text(f'SELECT version_num FROM "{migration_scope.schema}".alembic_version')
            )
            await self.connection.commit()
        except Exception as exc:
            if is_transient_database_error(exc):
                raise TenantMigrationVerificationUnavailableError() from None
            raise TenantMigrationVerificationFailedError() from None

        expected_revision = TEACHING_MIGRATION_HEAD_REVISION if action == "upgrade" else None
        if revision != expected_revision:
            raise TenantMigrationRevisionMismatchError()
        return str(revision) if revision is not None else None


@asynccontextmanager
async def migration_lock_scope(
    engine: AsyncEngine,
    *,
    scope: str,
    tenant_schema: str | None = None,
) -> AsyncIterator[LockedMigrationLease]:
    """Acquire the complete advisory-lock protocol for one migration scope."""

    migration_scope = validate_migration_scope(scope, tenant_schema)
    async with engine.connect() as connection:
        if migration_scope.name == "platform":
            async with tenant_directory_session_lock(connection, shared=False):
                yield LockedMigrationLease(connection, migration_scope)
            return

        async with tenant_directory_session_lock(connection, shared=True):
            async with tenant_schema_migration_session_lock(
                connection,
                schema_name=migration_scope.schema,
            ):
                yield LockedMigrationLease(connection, migration_scope)


async def run_lock_aware_migration(
    engine: AsyncEngine | None = None,
    *,
    action: str,
    scope: str,
    tenant_schema: str | None = None,
) -> str | None:
    """Run one formal migration with locks, cancellation safety, and verification."""

    migration_scope = validate_migration_scope(scope, tenant_schema)
    owned_engine = engine is None
    try:
        if engine is None:
            engine = create_async_engine(
                load_migration_database_url(),
                poolclass=NullPool,
            )
        async with migration_lock_scope(
            engine,
            scope=migration_scope.name,
            tenant_schema=(migration_scope.schema if migration_scope.name == "tenant" else None),
        ) as lease:
            return await lease.run(
                action=action,
                scope=migration_scope.name,
                tenant_schema=(
                    migration_scope.schema if migration_scope.name == "tenant" else None
                ),
            )
    except CommandError:
        raise
    except Exception as exc:
        raise translate_migration_runtime_error(exc) from None
    finally:
        if owned_engine and engine is not None:
            await engine.dispose()
