"""Independent lifecycle processes for durable teaching jobs."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import socket
from typing import Literal

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from deeptutor.services.config import PlatformSettings, load_platform_settings
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.dispatcher import OutboxDispatcher
from deeptutor.teaching.models import DataPlaneRoute, Tenant
from deeptutor.teaching.models.jobs import GenerationQueue
from deeptutor.teaching.object_store import (
    ClassroomArtifactStore,
    ClassroomArtifactStoreFactory,
)
from deeptutor.teaching.openmaic.auth import (
    SERVICE_SECRET_PATH,
    MountedServiceSecretResolver,
)
from deeptutor.teaching.openmaic.client import OpenMAICClient, OpenMAICClientFactory
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection, DataPlaneUnavailable
from deeptutor.teaching.provisioning_worker import build_provisioning_worker
from deeptutor.teaching.repositories.data_planes import SqlAlchemyDataPlaneRepository
from deeptutor.teaching.repositories.jobs import (
    CancellationRequest,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.scheduler import FairScheduler
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext
from deeptutor.teaching.worker import GenerationLeaseReaper, GenerationWorker

PROCESS_NAMES = (
    "dispatcher",
    "worker",
    "export-worker",
    "reaper",
    "learning-projector",
    "tenant-provisioner",
)
_IDLE_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class QueueBinding:
    data_plane_route_id: str
    provider_profile_id: str
    worker_pool_ref: str
    queue_ref: str
    slot_pool: str
    tenant_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerRuntimeBoundary:
    """One process-wide service-secret boundary for a worker route."""

    mode: Literal["shared", "dedicated"]
    route_id: str | None
    tenant_id: str | None

    def __post_init__(self) -> None:
        if self.mode not in {"shared", "dedicated"}:
            raise ValueError("worker runtime mode is invalid")
        for value in (self.route_id, self.tenant_id):
            if value is not None and (not value or "\n" in value or "\r" in value):
                raise ValueError("worker runtime boundary is invalid")
        if self.mode == "shared" and self.tenant_id is not None:
            raise ValueError("shared worker boundary cannot select a tenant")
        if self.mode == "dedicated" and (self.route_id is None or self.tenant_id is None):
            raise ValueError("dedicated worker boundary requires route and tenant")

    def bind_route(self, route_id: str) -> WorkerRuntimeBoundary:
        if self.route_id is not None and self.route_id != route_id:
            raise ValueError("worker route is already bound")
        return WorkerRuntimeBoundary(
            mode=self.mode,
            route_id=route_id,
            tenant_id=self.tenant_id,
        )

    def allows_binding(self, *, tenant_id: str, route_id: str) -> bool:
        return self.route_id == route_id and (self.mode == "shared" or self.tenant_id == tenant_id)

    def allows_selection(self, selection: DataPlaneSelection) -> bool:
        return selection.mode == self.mode and self.allows_binding(
            tenant_id=selection.tenant_id,
            route_id=selection.route_ref,
        )


def _worker_runtime_boundary() -> WorkerRuntimeBoundary:
    raw_mode = os.environ.get("DEEPTUTOR_TEACHING_WORKER_MODE", "shared").strip()
    if raw_mode not in {"shared", "dedicated"}:
        raise ValueError("teaching worker mode is invalid")
    route_id = os.environ.get("DEEPTUTOR_TEACHING_WORKER_ROUTE_ID") or None
    tenant_id = os.environ.get("DEEPTUTOR_TEACHING_WORKER_TENANT_ID") or None
    return WorkerRuntimeBoundary(
        mode=raw_mode,
        route_id=route_id,
        tenant_id=tenant_id,
    )


class RuntimeOpenMAICClients:
    """Build a signed client only from a revalidated durable job binding."""

    def __init__(
        self,
        settings: PlatformSettings,
        http_client: httpx.AsyncClient,
        *,
        boundary: WorkerRuntimeBoundary,
        repository: SqlAlchemyDataPlaneRepository | None = None,
    ) -> None:
        if boundary.route_id is None:
            raise ValueError("worker client requires a fixed route boundary")
        self._settings = settings
        self._http = http_client
        self._boundary = boundary
        self._repository = repository or SqlAlchemyDataPlaneRepository()
        secret_path = self._settings.openmaic_service_secret_file or SERVICE_SECRET_PATH
        resolver = MountedServiceSecretResolver(
            Path(secret_path),
            runtime_mode=boundary.mode,
            runtime_route_id=boundary.route_id,
            runtime_tenant_id=boundary.tenant_id,
        )
        self._factory = OpenMAICClientFactory(
            settings=self._settings,
            binding_repository=self._repository,
            service_secret_resolver=resolver,
        )

    async def _selection(
        self,
        *,
        tenant_id: str,
        data_plane_route_id: str,
        provider_profile_id: str,
        worker_pool_ref: str,
        queue_ref: str,
    ) -> DataPlaneSelection:
        if not self._boundary.allows_binding(
            tenant_id=tenant_id,
            route_id=data_plane_route_id,
        ):
            raise DataPlaneUnavailable()
        selection = await self._repository.resolve_worker_selection(
            tenant_id=tenant_id,
            route_id=data_plane_route_id,
            provider_profile_id=provider_profile_id,
            worker_pool_ref=worker_pool_ref,
            queue_ref=queue_ref,
        )
        if selection is None or not self._boundary.allows_selection(selection):
            raise DataPlaneUnavailable()
        return selection

    async def _client(
        self,
        *,
        selection: DataPlaneSelection,
        job_id: str,
        phase: str,
    ) -> OpenMAICClient:
        kind = "export" if phase == "export" else "outline" if phase == "outline" else "content"
        client = await self._factory.create(
            selection=selection,
            http_client=self._http,
            known_job_kinds={job_id: kind},
        )
        if client is None:
            raise DataPlaneUnavailable()
        return client

    async def client_for_claim(self, claim) -> OpenMAICClient:
        selection = await self._selection(
            tenant_id=claim.tenant_id,
            data_plane_route_id=claim.data_plane_route_id,
            provider_profile_id=claim.provider_profile_id,
            worker_pool_ref=claim.worker_pool_ref,
            queue_ref=claim.queue_ref,
        )
        return await self._client(
            selection=selection,
            job_id=claim.job_id,
            phase=claim.phase,
        )

    async def client_for_cancellation(
        self,
        request: CancellationRequest,
    ) -> OpenMAICClient:
        selection = await self._selection(
            tenant_id=request.tenant_id,
            data_plane_route_id=request.data_plane_route_id,
            provider_profile_id=request.provider_profile_id,
            worker_pool_ref=request.worker_pool_ref,
            queue_ref=request.queue_ref,
        )
        return await self._client(
            selection=selection,
            job_id=request.job_id,
            phase=request.phase,
        )


class RuntimeCancellationGateway:
    def __init__(self, settings: PlatformSettings) -> None:
        self._settings = settings

    async def cancel(self, request: CancellationRequest) -> None:
        async with httpx.AsyncClient() as http_client:
            repository = SqlAlchemyDataPlaneRepository()
            selection = await repository.resolve_worker_selection(
                tenant_id=request.tenant_id,
                route_id=request.data_plane_route_id,
                provider_profile_id=request.provider_profile_id,
                worker_pool_ref=request.worker_pool_ref,
                queue_ref=request.queue_ref,
            )
            if selection is None:
                raise DataPlaneUnavailable()
            provider = RuntimeOpenMAICClients(
                self._settings,
                http_client,
                boundary=WorkerRuntimeBoundary(
                    mode=selection.mode,
                    route_id=selection.route_ref,
                    tenant_id=(selection.tenant_id if selection.mode == "dedicated" else None),
                ),
                repository=repository,
            )
            client = await provider.client_for_cancellation(request)
            await client.cancel(request.job_id)


class RuntimeStoreProvider:
    """Create a tenant-bound store under an internal, non-user context."""

    def __init__(
        self,
        settings: PlatformSettings,
        *,
        local_root: Path | None = None,
    ) -> None:
        self._settings = settings
        if local_root is None:
            from deeptutor.runtime.home import get_runtime_data_root

            local_root = get_runtime_data_root() / "teaching" / "object-store"
        self._local_root = local_root

    async def store_for_tenant(self, tenant_id: str) -> ClassroomArtifactStore:
        from deeptutor.multi_user.context import reset_current_tenant, set_current_tenant

        token = set_current_tenant(
            TenantContext(
                tenant_id=tenant_id,
                schema_name=tenant_schema_name(tenant_id),
                user_id="teaching-worker",
                permissions=frozenset(),
            )
        )
        try:
            factory = ClassroomArtifactStoreFactory(
                self._settings,
                local_root=self._local_root,
                allow_local=self._settings.object_store_mode == "local",
            )
            return await factory.create(tenant_id)
        finally:
            reset_current_tenant(token)


class RuntimeProjectionDocuments:
    """Load immutable version documents for the internal projector identity."""

    def __init__(self, settings: PlatformSettings) -> None:
        from deeptutor.teaching.services.classroom_content import (
            ClassroomContentService,
            SqlAlchemyClassroomContentRepository,
        )

        engine = get_platform_engine()
        self._service = ClassroomContentService(
            repository=SqlAlchemyClassroomContentRepository(engine=engine),
            stores=RuntimeStoreProvider(settings),
            ticket_service=None,
        )

    async def load_version_document(self, tenant_id: str, version_id: str) -> object:
        from deeptutor.teaching.projectors.mastery import DeterministicProjectionError
        from deeptutor.teaching.services.classroom_content import (
            ClassroomContentAccessDenied,
            ClassroomContentIntegrityError,
            ClassroomContentNotFound,
        )

        context = TenantContext(
            tenant_id=tenant_id,
            schema_name=tenant_schema_name(tenant_id),
            user_id="learning-projector",
            permissions=frozenset(),
        )
        try:
            return await self._service.load_version_document(context, version_id)
        except ClassroomContentNotFound:
            raise DeterministicProjectionError("classroom_document_unavailable") from None
        except ClassroomContentIntegrityError:
            raise DeterministicProjectionError("classroom_document_integrity_invalid") from None
        except ClassroomContentAccessDenied:
            raise DeterministicProjectionError("classroom_document_binding_invalid") from None


async def _queued_bindings(
    job_kind: str,
    boundary: WorkerRuntimeBoundary,
) -> tuple[QueueBinding, ...]:
    engine = get_platform_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        conditions = [
            Tenant.status == "active",
            GenerationQueue.status == "queued",
            GenerationQueue.job_kind == job_kind,
            Tenant.data_plane_mode == DataPlaneRoute.mode,
            GenerationQueue.provider_profile_id == DataPlaneRoute.provider_profile_id,
            GenerationQueue.worker_pool_ref == DataPlaneRoute.worker_pool,
            GenerationQueue.queue_ref == DataPlaneRoute.queue_name,
            DataPlaneRoute.mode == boundary.mode,
            DataPlaneRoute.status == "active",
            DataPlaneRoute.health_status == "healthy",
            or_(
                and_(
                    DataPlaneRoute.mode == "shared",
                    DataPlaneRoute.tenant_id.is_(None),
                    DataPlaneRoute.owner_key == "shared",
                ),
                and_(
                    DataPlaneRoute.mode == "dedicated",
                    DataPlaneRoute.tenant_id == Tenant.id,
                    DataPlaneRoute.owner_key == Tenant.id,
                ),
            ),
        ]
        if boundary.route_id is not None:
            conditions.append(DataPlaneRoute.id == boundary.route_id)
        if boundary.mode == "dedicated":
            conditions.append(Tenant.id == boundary.tenant_id)
        rows = (
            await session.execute(
                select(
                    GenerationQueue.data_plane_route_id,
                    GenerationQueue.provider_profile_id,
                    GenerationQueue.worker_pool_ref,
                    GenerationQueue.queue_ref,
                    GenerationQueue.slot_pool,
                    GenerationQueue.tenant_id,
                )
                .select_from(GenerationQueue)
                .join(Tenant, Tenant.id == GenerationQueue.tenant_id)
                .join(
                    DataPlaneRoute,
                    DataPlaneRoute.id == GenerationQueue.data_plane_route_id,
                )
                .where(*conditions)
                .distinct()
            )
        ).all()
    grouped: dict[tuple[str, str, str, str, str], set[str]] = {}
    for row in rows:
        key = (
            row.data_plane_route_id,
            row.provider_profile_id,
            row.worker_pool_ref,
            row.queue_ref,
            row.slot_pool,
        )
        grouped.setdefault(key, set()).add(row.tenant_id)
    return tuple(
        QueueBinding(*key, tenant_ids=tuple(sorted(tenant_ids)))
        for key, tenant_ids in sorted(grouped.items())
    )


async def _run_loop(
    step: Callable[[], Awaitable[bool]],
    *,
    once: bool,
) -> bool:
    worked = await step()
    if once:
        return worked
    while True:
        if not worked:
            await asyncio.sleep(_IDLE_SECONDS)
        worked = await step()


async def _run_dispatcher(*, once: bool) -> bool:
    dispatcher = OutboxDispatcher()

    async def step() -> bool:
        return await dispatcher.dispatch_next() is not None

    return await _run_loop(step, once=once)


async def _run_reaper(*, once: bool) -> bool:
    reaper = GenerationLeaseReaper(SqlAlchemyGenerationJobRepository())
    return await _run_loop(reaper.run_once, once=once)


async def _run_tenant_provisioner(
    settings: PlatformSettings,
    *,
    once: bool,
) -> bool:
    worker = build_provisioning_worker(
        settings=settings,
        worker_id=f"{socket.gethostname()}-tenant-provisioner-{os.getpid()}",
    )
    return await _run_loop(worker.run_once, once=once)


async def _run_worker(
    settings: PlatformSettings,
    *,
    job_kind: str,
    once: bool,
) -> bool:
    scheduler = FairScheduler()
    repository = SqlAlchemyGenerationJobRepository()
    boundary = _worker_runtime_boundary()
    process_name = "worker" if job_kind == "generation" else "export-worker"
    worker_id = f"{socket.gethostname()}-{process_name}-{os.getpid()}"
    stores = RuntimeStoreProvider(settings)
    async with httpx.AsyncClient() as http_client:
        worker: GenerationWorker | None = None

        async def step() -> bool:
            nonlocal boundary, worker
            bindings = await _queued_bindings(job_kind, boundary)
            if boundary.route_id is None:
                if not bindings:
                    return False
                boundary = boundary.bind_route(bindings[0].data_plane_route_id)
                bindings = tuple(
                    binding
                    for binding in bindings
                    if binding.data_plane_route_id == boundary.route_id
                )
            if worker is None:
                worker = GenerationWorker(
                    scheduler=scheduler,
                    repository=repository,
                    clients=RuntimeOpenMAICClients(
                        settings,
                        http_client,
                        boundary=boundary,
                    ),
                    stores=stores,
                    worker_id=worker_id,
                    job_kind=job_kind,
                )
            for binding in bindings:
                global_limit = (
                    settings.shared_generation_limit
                    if binding.slot_pool == "generation" and boundary.mode == "shared"
                    else max(1, settings.default_tenant_generation_limit)
                )
                await scheduler.ensure_slots(
                    binding.tenant_ids,
                    worker_pool_ref=binding.worker_pool_ref,
                    slot_pool=binding.slot_pool,
                    global_limit=global_limit,
                    tenant_limit=settings.default_tenant_generation_limit,
                )
                if await worker.run_once(
                    slot_pool=binding.slot_pool,
                    data_plane_route_id=binding.data_plane_route_id,
                    provider_profile_id=binding.provider_profile_id,
                    worker_pool_ref=binding.worker_pool_ref,
                    queue_ref=binding.queue_ref,
                ):
                    return True
            return False

        return await _run_loop(step, once=once)


async def _run_learning_projector(
    settings: PlatformSettings,
    *,
    once: bool,
) -> bool:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker
    from deeptutor.teaching.projectors.memory import (
        ClassroomMemoryProjector,
        ClassroomMemoryTargetResolver,
    )

    worker = LearningProjectionWorker(
        engine=get_platform_engine(),
        documents=RuntimeProjectionDocuments(settings),
        worker_id=f"{socket.gethostname()}-learning-projector-{os.getpid()}",
        memory_projector=ClassroomMemoryProjector(),
        memory_targets=ClassroomMemoryTargetResolver(),
    )
    return await _run_loop(worker.run_once, once=once)


async def run_process(
    process_name: str,
    *,
    once: bool = False,
    settings: PlatformSettings | None = None,
) -> bool:
    if process_name not in PROCESS_NAMES:
        raise ValueError("unknown teaching process")
    resolved = settings or load_platform_settings()
    if not resolved.enabled:
        return False
    if process_name == "dispatcher":
        return await _run_dispatcher(once=once)
    if process_name == "reaper":
        return await _run_reaper(once=once)
    if process_name == "learning-projector":
        return await _run_learning_projector(resolved, once=once)
    if process_name == "tenant-provisioner":
        return await _run_tenant_provisioner(resolved, once=once)
    return await _run_worker(
        resolved,
        job_kind="generation" if process_name == "worker" else "export",
        once=once,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one teaching lifecycle process")
    parser.add_argument("process", choices=PROCESS_NAMES)
    parser.add_argument("--once", action="store_true", help="Run at most one polling cycle")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        asyncio.run(run_process(arguments.process, once=arguments.once))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PROCESS_NAMES",
    "RuntimeCancellationGateway",
    "RuntimeOpenMAICClients",
    "RuntimeProjectionDocuments",
    "RuntimeStoreProvider",
    "WorkerRuntimeBoundary",
    "main",
    "run_process",
]
