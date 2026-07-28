"""Async database connections and sessions for teaching data."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from deeptutor.services.config import load_platform_settings

from .schema_names import tenant_schema_name

_platform_engine: AsyncEngine | None = None


def get_platform_engine() -> AsyncEngine:
    """Return the platform engine, failing closed when it is not configured."""

    global _platform_engine
    if _platform_engine is not None:
        return _platform_engine

    settings = load_platform_settings()
    if not settings.enabled:
        raise RuntimeError("platform database is disabled")
    if settings.database_url is None:
        raise RuntimeError("platform database URL is unavailable")

    _platform_engine = create_async_engine(
        settings.database_url.get_secret_value(),
        poolclass=NullPool,
    )
    return _platform_engine


async def dispose_platform_engine() -> None:
    """Dispose and clear the cached platform engine."""

    global _platform_engine
    engine = _platform_engine
    _platform_engine = None
    if engine is not None:
        await engine.dispose()


@asynccontextmanager
async def tenant_connection(
    engine: AsyncEngine,
    tenant_id: str,
) -> AsyncIterator[AsyncConnection]:
    """Open a connection with the logical tenant schema translated safely."""

    tenant_engine = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    async with tenant_engine.connect() as connection:
        yield connection


@asynccontextmanager
async def platform_session() -> AsyncIterator[AsyncSession]:
    """Open a session for fixed-schema platform repositories."""

    session_factory = async_sessionmaker(
        get_platform_engine(),
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@asynccontextmanager
async def tenant_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """Open a session whose logical tenant schema maps to one tenant."""

    tenant_engine = get_platform_engine().execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    session_factory = async_sessionmaker(
        tenant_engine,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session
