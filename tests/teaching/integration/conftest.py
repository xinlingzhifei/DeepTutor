from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from deeptutor.teaching.schema_names import tenant_schema_name

PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
            timeout=120,
        )
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
        self.migrated_tenants.add(schema_name)


def _clean_python_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


@pytest.fixture(scope="session")
def generation_database(tmp_path_factory) -> GenerationDatabase:
    password = "GENERATION_PASSWORD_SENTINEL_7d3c1"
    with PostgresContainer(
        "postgres:16-alpine",
        username="generation_user",
        password=password,
        dbname="teaching_jobs",
    ) as postgres:
        sync_url = make_url(postgres.get_connection_url())
        async_url = sync_url.set(drivername="postgresql+asyncpg").render_as_string(
            hide_password=False
        )
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
            timeout=120,
        )
        assert platform_migration.returncode == 0, (
            f"{platform_migration.stdout}\n{platform_migration.stderr}"
        )
        yield GenerationDatabase(
            url=async_url,
            environment=environment,
        )


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
