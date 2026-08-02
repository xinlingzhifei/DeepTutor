from __future__ import annotations

import asyncio
import subprocess
import sys

import httpx
from pydantic import SecretStr
import pytest

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.openmaic.data_planes import DataPlaneUnavailable
from deeptutor.teaching.repositories.jobs import CancellationRequest


def test_process_module_exposes_exact_lifecycle_commands() -> None:
    from deeptutor.teaching.processes import PROCESS_NAMES

    assert PROCESS_NAMES == (
        "dispatcher",
        "worker",
        "export-worker",
        "reaper",
    )


def test_process_module_help_does_not_touch_external_services() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "deeptutor.teaching.processes", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    for name in ("dispatcher", "worker", "export-worker", "reaper"):
        assert name in completed.stdout


@pytest.mark.parametrize("process_name", ("dispatcher", "worker", "export-worker", "reaper"))
def test_disabled_platform_processes_return_without_external_work(process_name: str) -> None:
    from deeptutor.teaching.processes import run_process

    assert not asyncio.run(
        run_process(
            process_name,
            once=True,
            settings=PlatformSettings(enabled=False),
        )
    )


def test_dedicated_worker_boundary_requires_route_and_tenant() -> None:
    from deeptutor.teaching.processes import WorkerRuntimeBoundary

    with pytest.raises(ValueError, match="route and tenant"):
        WorkerRuntimeBoundary(mode="dedicated", route_id=None, tenant_id=None)


def test_worker_client_rejects_cross_route_before_repository_access() -> None:
    from deeptutor.teaching.processes import (
        RuntimeOpenMAICClients,
        WorkerRuntimeBoundary,
    )

    class Repository:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve_worker_selection(self, **kwargs):
            self.calls += 1
            raise AssertionError("cross-route work must fail before a database lookup")

    async def run() -> int:
        repository = Repository()
        settings = PlatformSettings(
            enabled=True,
            database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        )
        async with httpx.AsyncClient() as http_client:
            clients = RuntimeOpenMAICClients(
                settings,
                http_client,
                boundary=WorkerRuntimeBoundary(
                    mode="shared",
                    route_id="shared-primary",
                    tenant_id=None,
                ),
                repository=repository,
            )
            with pytest.raises(DataPlaneUnavailable):
                await clients.client_for_cancellation(
                    CancellationRequest(
                        tenant_id="tenant-1",
                        job_id="job-1",
                        running=True,
                        phase="outline",
                        data_plane_route_id="dedicated-other",
                        provider_profile_id="provider-other",
                        worker_pool_ref="pool-other",
                        queue_ref="queue-other",
                    )
                )
        return repository.calls

    assert asyncio.run(run()) == 0
