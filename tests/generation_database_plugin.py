from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config

from deeptutor.teaching.schema_names import tenant_schema_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_TIMEOUT_SECONDS = 240


@dataclass
class GenerationDatabase:
    url: str
    environment: dict[str, str]
    migrated_tenants: set[str] = field(default_factory=set)

    def migrate_tenant(self, tenant_id: str) -> None:
        schema_name = tenant_schema_name(tenant_id)
        if schema_name in self.migrated_tenants:
            return
        completed = subprocess.run(
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
            cwd=PROJECT_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=MIGRATION_TIMEOUT_SECONDS,
        )
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
        self.migrated_tenants.add(schema_name)


def _clean_python_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _wait_for_host_database(async_url: str, *, timeout_seconds: float = 30.0) -> None:
    """Wait for the published host port, not only the in-container socket."""

    async def probe() -> None:
        engine = create_async_engine(async_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            asyncio.run(asyncio.wait_for(probe(), timeout=5.0))
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError("published PostgreSQL port did not become ready") from last_error


@pytest.fixture(scope="session")
def generation_database(tmp_path_factory) -> GenerationDatabase:
    password = "GENERATION_PASSWORD_SENTINEL_7d3c1"
    postgres = PostgresContainer(
        "postgres:16-alpine",
        username="generation_user",
        password=password,
        dbname="teaching_jobs",
    )
    previous_max_tries = testcontainers_config.max_tries
    previous_sleep_time = testcontainers_config.sleep_time
    try:
        testcontainers_config.max_tries = MIGRATION_TIMEOUT_SECONDS
        testcontainers_config.sleep_time = 1
        with postgres:
            testcontainers_config.max_tries = previous_max_tries
            testcontainers_config.sleep_time = previous_sleep_time
            sync_url = make_url(postgres.get_connection_url())
            async_url = sync_url.set(drivername="postgresql+asyncpg").render_as_string(
                hide_password=False
            )
            _wait_for_host_database(async_url)
            runtime_home = tmp_path_factory.mktemp("generation-runtime")
            settings_dir = runtime_home / "data" / "user" / "settings"
            settings_dir.mkdir(parents=True)
            (settings_dir / "platform.json").write_text(
                json.dumps({"enabled": True}),
                encoding="utf-8",
            )
            environment = _clean_python_environment()
            environment["DEEPTUTOR_HOME"] = str(runtime_home)
            environment["DEEPTUTOR_PLATFORM_DATABASE_URL"] = async_url
            platform_migration = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-x",
                    "scope=platform",
                    "upgrade",
                    "head",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=MIGRATION_TIMEOUT_SECONDS,
            )
            assert platform_migration.returncode == 0, (
                f"{platform_migration.stdout}\n{platform_migration.stderr}"
            )
            yield GenerationDatabase(
                url=async_url,
                environment=environment,
            )
    finally:
        testcontainers_config.max_tries = previous_max_tries
        testcontainers_config.sleep_time = previous_sleep_time


@pytest.fixture
def clean_generation_runtime_state(generation_database: GenerationDatabase):
    async def reset() -> None:
        engine = create_async_engine(generation_database.url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE TABLE "
                        "platform.generation_slots, "
                        "platform.generation_queue, "
                        "platform.outbox_messages, "
                        "platform.tenant_scheduler_state "
                        "RESTART IDENTITY"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(reset())
    yield
    asyncio.run(reset())
