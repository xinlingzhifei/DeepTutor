from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.openmaic.data_planes import DataPlaneUnavailable
from deeptutor.teaching.repositories.jobs import CancellationRequest


def test_process_module_exposes_exact_lifecycle_commands() -> None:
    from deeptutor.teaching import processes

    assert processes.PROCESS_NAMES == (
        "dispatcher",
        "worker",
        "export-worker",
        "reaper",
        "learning-projector",
        "tenant-provisioner",
    )
    assert getattr(processes, "PROCESS_HEALTH_ROLES", None) == {
        "dispatcher": "dispatcher",
        "worker": "generation_worker",
        "export-worker": "export_worker",
        "reaper": "reaper",
        "learning-projector": "projector",
        "tenant-provisioner": "tenant_provisioner",
    }
    assert "heartbeat_repository" in inspect.signature(processes.run_process).parameters


def test_process_module_help_does_not_touch_external_services() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "deeptutor.teaching.processes", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    for name in (
        "dispatcher",
        "worker",
        "export-worker",
        "reaper",
        "learning-projector",
        "tenant-provisioner",
    ):
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
    (
        "dispatcher",
        "worker",
        "export-worker",
        "reaper",
        "learning-projector",
        "tenant-provisioner",
    ),
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
async def test_worker_client_records_job_bound_dedicated_route_attempt() -> None:
    from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection
    from deeptutor.teaching.processes import (
        RuntimeOpenMAICClients,
        WorkerRuntimeBoundary,
    )

    attempts: list[dict[str, object]] = []

    class Repository:
        async def record_job_route_attempt(self, **kwargs) -> None:
            attempts.append(kwargs)

    clients = RuntimeOpenMAICClients.__new__(RuntimeOpenMAICClients)
    clients._boundary = WorkerRuntimeBoundary(
        mode="dedicated",
        route_id="dedicated-tenant-1",
        tenant_id="tenant-1",
    )
    clients._repository = Repository()

    async def selection(**_kwargs):
        return DataPlaneSelection(
            tenant_id="tenant-1",
            route_ref="dedicated-tenant-1",
            provider_profile_ref="provider-tenant-1",
            mode="dedicated",
            worker_pool_ref="generation-tenant-1",
            queue_ref="openmaic.tenant-1",
            config_revision="route-binding-v1",
            route_config_digest="a" * 64,
            provider_config_digest="b" * 64,
        )

    sentinel_client = object()

    async def client(**_kwargs):
        return sentinel_client

    clients._selection = selection
    clients._client = client
    claim = SimpleNamespace(
        tenant_id="tenant-1",
        job_id="job-1",
        phase="content",
        attempt_count=2,
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        lease_owner="dedicated-worker-1",
        lease_token="lease-token-1",
    )

    assert await clients.client_for_claim(claim) is sentinel_client
    assert attempts == [
        {
            "tenant_id": "tenant-1",
            "job_id": "job-1",
            "phase": "content",
            "attempt_count": 2,
            "mode": "dedicated",
            "data_plane_route_id": "dedicated-tenant-1",
            "provider_profile_id": "provider-tenant-1",
            "worker_pool_ref": "generation-tenant-1",
            "queue_ref": "openmaic.tenant-1",
            "worker_id": "dedicated-worker-1",
            "lease_token": "lease-token-1",
            "outcome": "selected",
            "config_revision": "route-binding-v1",
            "route_config_digest": "a" * 64,
            "provider_config_digest": "b" * 64,
        }
    ]


@pytest.mark.asyncio
async def test_worker_client_records_claim_mode_when_route_attempt_is_unavailable() -> None:
    from deeptutor.teaching.processes import (
        RuntimeOpenMAICClients,
        WorkerRuntimeBoundary,
    )

    attempts: list[dict[str, object]] = []

    class Repository:
        async def record_job_route_attempt(self, **kwargs) -> None:
            attempts.append(kwargs)

    clients = RuntimeOpenMAICClients.__new__(RuntimeOpenMAICClients)
    clients._boundary = WorkerRuntimeBoundary(
        mode="shared",
        route_id="shared-primary",
        tenant_id=None,
    )
    clients._repository = Repository()

    async def unavailable(**_kwargs):
        raise DataPlaneUnavailable()

    clients._selection = unavailable
    claim = SimpleNamespace(
        tenant_id="tenant-1",
        job_id="job-1",
        phase="content",
        attempt_count=3,
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        lease_owner="shared-worker-1",
        lease_token="lease-token-1",
    )

    with pytest.raises(DataPlaneUnavailable):
        await clients.client_for_claim(claim)
    assert attempts == [
        {
            "tenant_id": "tenant-1",
            "job_id": "job-1",
            "phase": "content",
            "attempt_count": 3,
            "mode": "dedicated",
            "data_plane_route_id": "dedicated-tenant-1",
            "provider_profile_id": "provider-tenant-1",
            "worker_pool_ref": "generation-tenant-1",
            "queue_ref": "openmaic.tenant-1",
            "worker_id": "shared-worker-1",
            "lease_token": "lease-token-1",
            "outcome": "unavailable",
        }
    ]


@pytest.mark.asyncio
async def test_worker_client_records_service_secret_unavailable_before_reraising() -> None:
    from deeptutor.teaching.openmaic.auth import ServiceSecretUnavailable
    from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection
    from deeptutor.teaching.processes import (
        RuntimeOpenMAICClients,
        WorkerRuntimeBoundary,
    )

    attempts: list[dict[str, object]] = []

    class Repository:
        async def record_job_route_attempt(self, **kwargs) -> None:
            attempts.append(kwargs)

    clients = RuntimeOpenMAICClients.__new__(RuntimeOpenMAICClients)
    clients._boundary = WorkerRuntimeBoundary(
        mode="dedicated",
        route_id="dedicated-tenant-1",
        tenant_id="tenant-1",
    )
    clients._repository = Repository()

    async def selection(**_kwargs):
        return DataPlaneSelection(
            tenant_id="tenant-1",
            route_ref="dedicated-tenant-1",
            provider_profile_ref="provider-tenant-1",
            mode="dedicated",
            worker_pool_ref="generation-tenant-1",
            queue_ref="openmaic.tenant-1",
        )

    unavailable = ServiceSecretUnavailable()

    async def client(**_kwargs):
        raise unavailable

    clients._selection = selection
    clients._client = client
    claim = SimpleNamespace(
        tenant_id="tenant-1",
        job_id="job-1",
        phase="content",
        attempt_count=1,
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        lease_owner="dedicated-worker-1",
        lease_token="lease-token-1",
    )

    with pytest.raises(ServiceSecretUnavailable) as captured:
        await clients.client_for_claim(claim)

    assert captured.value is unavailable
    assert attempts == [
        {
            "tenant_id": "tenant-1",
            "job_id": "job-1",
            "phase": "content",
            "attempt_count": 1,
            "mode": "dedicated",
            "data_plane_route_id": "dedicated-tenant-1",
            "provider_profile_id": "provider-tenant-1",
            "worker_pool_ref": "generation-tenant-1",
            "queue_ref": "openmaic.tenant-1",
            "worker_id": "dedicated-worker-1",
            "lease_token": "lease-token-1",
            "outcome": "unavailable",
        }
    ]


@pytest.mark.asyncio
async def test_worker_client_records_route_configuration_unavailable_before_reraising() -> None:
    from deeptutor.teaching.openmaic.data_planes import (
        DataPlaneConfigurationUnavailable,
        DataPlaneSelection,
    )
    from deeptutor.teaching.processes import (
        RuntimeOpenMAICClients,
        WorkerRuntimeBoundary,
    )

    attempts: list[dict[str, object]] = []

    class Repository:
        async def record_job_route_attempt(self, **kwargs) -> None:
            attempts.append(kwargs)

    clients = RuntimeOpenMAICClients.__new__(RuntimeOpenMAICClients)
    clients._boundary = WorkerRuntimeBoundary(
        mode="dedicated",
        route_id="dedicated-tenant-1",
        tenant_id="tenant-1",
    )
    clients._repository = Repository()

    async def selection(**_kwargs):
        return DataPlaneSelection(
            tenant_id="tenant-1",
            route_ref="dedicated-tenant-1",
            provider_profile_ref="provider-tenant-1",
            mode="dedicated",
            worker_pool_ref="generation-tenant-1",
            queue_ref="openmaic.tenant-1",
        )

    unavailable = DataPlaneConfigurationUnavailable()

    async def client(**_kwargs):
        raise unavailable

    clients._selection = selection
    clients._client = client
    claim = SimpleNamespace(
        tenant_id="tenant-1",
        job_id="job-1",
        phase="content",
        attempt_count=1,
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        lease_owner="dedicated-worker-1",
        lease_token="lease-token-1",
    )

    with pytest.raises(DataPlaneConfigurationUnavailable) as captured:
        await clients.client_for_claim(claim)

    assert captured.value is unavailable
    assert attempts == [
        {
            "tenant_id": "tenant-1",
            "job_id": "job-1",
            "phase": "content",
            "attempt_count": 1,
            "mode": "dedicated",
            "data_plane_route_id": "dedicated-tenant-1",
            "provider_profile_id": "provider-tenant-1",
            "worker_pool_ref": "generation-tenant-1",
            "queue_ref": "openmaic.tenant-1",
            "worker_id": "dedicated-worker-1",
            "lease_token": "lease-token-1",
            "outcome": "unavailable",
        }
    ]


@pytest.mark.asyncio
async def test_worker_client_does_not_record_programming_value_error_as_unavailable() -> None:
    from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection
    from deeptutor.teaching.processes import (
        RuntimeOpenMAICClients,
        WorkerRuntimeBoundary,
    )

    attempts: list[dict[str, object]] = []

    class Repository:
        async def record_job_route_attempt(self, **kwargs) -> None:
            attempts.append(kwargs)

    clients = RuntimeOpenMAICClients.__new__(RuntimeOpenMAICClients)
    clients._boundary = WorkerRuntimeBoundary(
        mode="dedicated",
        route_id="dedicated-tenant-1",
        tenant_id="tenant-1",
    )
    clients._repository = Repository()

    async def selection(**_kwargs):
        return DataPlaneSelection(
            tenant_id="tenant-1",
            route_ref="dedicated-tenant-1",
            provider_profile_ref="provider-tenant-1",
            mode="dedicated",
            worker_pool_ref="generation-tenant-1",
            queue_ref="openmaic.tenant-1",
        )

    programming_error = ValueError("unrelated programming defect")

    async def client(**_kwargs):
        raise programming_error

    clients._selection = selection
    clients._client = client
    claim = SimpleNamespace(
        tenant_id="tenant-1",
        job_id="job-1",
        phase="content",
        attempt_count=1,
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        lease_owner="dedicated-worker-1",
        lease_token="lease-token-1",
    )

    with pytest.raises(ValueError) as captured:
        await clients.client_for_claim(claim)

    assert captured.value is programming_error
    assert attempts == []


@pytest.mark.asyncio
async def test_worker_client_does_not_return_client_when_route_attempt_audit_fails() -> None:
    from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection
    from deeptutor.teaching.processes import (
        RuntimeOpenMAICClients,
        WorkerRuntimeBoundary,
    )

    class Repository:
        async def record_job_route_attempt(self, **_kwargs) -> None:
            raise RuntimeError("route attempt audit unavailable")

    clients = RuntimeOpenMAICClients.__new__(RuntimeOpenMAICClients)
    clients._boundary = WorkerRuntimeBoundary(
        mode="dedicated",
        route_id="dedicated-tenant-1",
        tenant_id="tenant-1",
    )
    clients._repository = Repository()

    async def selection(**_kwargs):
        return DataPlaneSelection(
            tenant_id="tenant-1",
            route_ref="dedicated-tenant-1",
            provider_profile_ref="provider-tenant-1",
            mode="dedicated",
            worker_pool_ref="generation-tenant-1",
            queue_ref="openmaic.tenant-1",
        )

    sentinel_client = object()

    async def client(**_kwargs):
        return sentinel_client

    clients._selection = selection
    clients._client = client
    claim = SimpleNamespace(
        tenant_id="tenant-1",
        job_id="job-1",
        phase="content",
        attempt_count=1,
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        lease_owner="dedicated-worker-1",
        lease_token="lease-token-1",
    )

    with pytest.raises(RuntimeError, match="route attempt audit unavailable"):
        await clients.client_for_claim(claim)


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
