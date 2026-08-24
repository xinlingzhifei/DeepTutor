"""Cross-process PostgreSQL lock for tenant-directory migration safety."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

TENANT_DIRECTORY_LOCK_RESOURCE = "yfeistai:tenant-directory:v1"
TENANT_SCHEMA_MIGRATION_LOCK_NAMESPACE = "yfeistai:tenant-schema-migration:v1"
_ThreadResult = TypeVar("_ThreadResult")


def build_tenant_directory_transaction_lock_statement() -> TextClause:
    """Share-lock directory mutations for the lifetime of one transaction."""

    return text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:resource, 0))").bindparams(
        resource=TENANT_DIRECTORY_LOCK_RESOURCE
    )


def _tenant_schema_migration_lock_resource(schema_name: str) -> str:
    return f"{TENANT_SCHEMA_MIGRATION_LOCK_NAMESPACE}:{schema_name}"


def _session_lock_statement(*, resource: str, shared: bool) -> TextClause:
    function = "pg_advisory_lock_shared" if shared else "pg_advisory_lock"
    return text(f"SELECT {function}(hashtextextended(:resource, 0))").bindparams(resource=resource)


def _session_unlock_statement(*, resource: str, shared: bool) -> TextClause:
    function = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
    return text(f"SELECT {function}(hashtextextended(:resource, 0))").bindparams(resource=resource)


async def _await_cancellation_safe(awaitable: Awaitable[_ThreadResult]) -> _ThreadResult:
    task = asyncio.create_task(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except BaseException:
            break
    if cancellation is not None:
        if not task.cancelled():
            try:
                task.result()
            except BaseException:
                pass
        raise cancellation
    return task.result()


async def run_sync_in_thread_cancellation_safe(
    function: Callable[..., _ThreadResult],
    /,
    *args: Any,
    **kwargs: Any,
) -> _ThreadResult:
    """Wait for a sync thread after cancellation, then preserve cancellation."""

    return await _await_cancellation_safe(asyncio.to_thread(function, *args, **kwargs))


async def _release_session_lock(
    connection: AsyncConnection,
    *,
    resource: str,
    shared: bool,
) -> None:
    await connection.rollback()
    released = await connection.scalar(_session_unlock_statement(resource=resource, shared=shared))
    await connection.commit()
    if released is not True:
        raise RuntimeError("advisory session lock was not held")


async def _invalidate_connection(connection: AsyncConnection) -> None:
    await connection.invalidate()


@asynccontextmanager
async def _session_lock(
    connection: AsyncConnection,
    *,
    resource: str,
    shared: bool,
) -> AsyncIterator[None]:
    """Hold one advisory lock across work performed by other DB connections.

    The caller must pass a freshly opened connection because acquisition commits
    its implicit transaction while the session-level advisory lock remains held.
    """

    body_error: BaseException | None = None
    acquired = False
    try:
        await connection.execute(_session_lock_statement(resource=resource, shared=shared))
        acquired = True
        await connection.commit()
        yield
    except BaseException as exc:
        body_error = exc

    release_error: BaseException | None = None
    if acquired:
        try:
            await _await_cancellation_safe(
                _release_session_lock(
                    connection,
                    resource=resource,
                    shared=shared,
                )
            )
        except BaseException as exc:
            release_error = exc
            try:
                await _await_cancellation_safe(_invalidate_connection(connection))
            except BaseException:
                pass
    elif body_error is not None:
        try:
            await _await_cancellation_safe(_invalidate_connection(connection))
        except BaseException:
            pass

    if release_error is not None:
        if body_error is not None:
            raise release_error from body_error
        raise release_error
    if body_error is not None:
        raise body_error


@asynccontextmanager
async def tenant_directory_session_lock(
    connection: AsyncConnection,
    *,
    shared: bool,
) -> AsyncIterator[None]:
    async with _session_lock(
        connection,
        resource=TENANT_DIRECTORY_LOCK_RESOURCE,
        shared=shared,
    ):
        yield


@asynccontextmanager
async def tenant_schema_migration_session_lock(
    connection: AsyncConnection,
    *,
    schema_name: str,
) -> AsyncIterator[None]:
    """Serialize Alembic and revision verification for one tenant key."""

    async with _session_lock(
        connection,
        resource=_tenant_schema_migration_lock_resource(schema_name),
        shared=False,
    ):
        yield
