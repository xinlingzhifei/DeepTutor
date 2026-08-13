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
        "learning-projector",
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
    for name in ("dispatcher", "worker", "export-worker", "reaper", "learning-projector"):
        assert name in completed.stdout


def test_process_module_lazy_loads_learning_projector_dependencies() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import deeptutor.teaching.processes; "
            "print('deeptutor.teaching.projector_worker' in sys.modules); "
            "print('deeptutor.teaching.services.classroom_content' in sys.modules)",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["False", "False"]


@pytest.mark.parametrize(
    "process_name",
    ("dispatcher", "worker", "export-worker", "reaper", "learning-projector"),
)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_error", "reason_code"),
    [
        ("not_found", "classroom_document_unavailable"),
        ("integrity", "classroom_document_integrity_invalid"),
        ("access_denied", "classroom_document_binding_invalid"),
    ],
)
async def test_runtime_projection_documents_classifies_permanent_content_failures(
    content_error: str,
    reason_code: str,
) -> None:
    from deeptutor.teaching.processes import RuntimeProjectionDocuments
    from deeptutor.teaching.projectors.mastery import DeterministicProjectionError
    from deeptutor.teaching.services.classroom_content import (
        ClassroomContentAccessDenied,
        ClassroomContentIntegrityError,
        ClassroomContentNotFound,
    )

    errors = {
        "not_found": ClassroomContentNotFound("private detail"),
        "integrity": ClassroomContentIntegrityError("private detail"),
        "access_denied": ClassroomContentAccessDenied("private detail"),
    }

    class Service:
        async def load_version_document(self, context, version_id):
            raise errors[content_error]

    documents = RuntimeProjectionDocuments.__new__(RuntimeProjectionDocuments)
    documents._service = Service()

    with pytest.raises(DeterministicProjectionError, match=reason_code):
        await documents.load_version_document("tenant-a", "version-a")


@pytest.mark.asyncio
async def test_runtime_projection_documents_preserves_operational_unavailability() -> None:
    from deeptutor.teaching.processes import RuntimeProjectionDocuments
    from deeptutor.teaching.services.classroom_content import ClassroomContentUnavailable

    sentinel = ClassroomContentUnavailable("temporary storage outage")

    class Service:
        async def load_version_document(self, context, version_id):
            raise sentinel

    documents = RuntimeProjectionDocuments.__new__(RuntimeProjectionDocuments)
    documents._service = Service()

    with pytest.raises(ClassroomContentUnavailable) as caught:
        await documents.load_version_document("tenant-a", "version-a")

    assert caught.value is sentinel


@pytest.mark.asyncio
async def test_learning_projector_process_installs_memory_projection(monkeypatch) -> None:
    from deeptutor.teaching import processes

    captured: dict[str, object] = {}

    class Worker:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_once(self):
            return False

    monkeypatch.setattr(
        "deeptutor.teaching.projector_worker.LearningProjectionWorker",
        Worker,
    )
    monkeypatch.setattr(processes, "get_platform_engine", lambda: object())

    assert not await processes._run_learning_projector(
        PlatformSettings(
            enabled=True,
            database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        ),
        once=True,
    )
    assert captured["memory_projector"].__class__.__name__ == "ClassroomMemoryProjector"
    assert captured["memory_targets"].__class__.__name__ == "ClassroomMemoryTargetResolver"
