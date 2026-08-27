"""Private metrics and administrator teaching-health endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Response
import httpx
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict, Field, field_validator

from deeptutor.api.routers.auth import require_platform_admin
from deeptutor.services.config import PlatformSettings, load_platform_settings
from deeptutor.teaching.health import (
    TeachingHealthService,
    get_teaching_health_service,
)
from deeptutor.teaching.health_probes import (
    ActiveHealthProbeService,
    DatabaseHealthProbe,
    HealthProbeFailure,
    MigrationHealthProbe,
    ObjectStoreHealthProbe,
    OpenMAICDataPlaneHealthProbes,
    RenderHealthProbe,
    RuntimeOpenMAICHealthClientFactory,
    SqlAlchemyMigrationHealthRepository,
)
from deeptutor.teaching.metrics import TeachingMetrics
from deeptutor.teaching.repositories.capacity_scheduler import (
    MAX_CAPACITY_SNAPSHOT_JOBS,
    SqlAlchemyCapacitySchedulerRepository,
    get_capacity_scheduler_repository,
)
from deeptutor.teaching.repositories.data_planes import SqlAlchemyDataPlaneRepository
from deeptutor.teaching.repositories.metrics import (
    SqlAlchemyTeachingMetricsRepository,
    get_teaching_metrics_repository,
)
from deeptutor.teaching.repositories.runtime_heartbeats import (
    get_runtime_heartbeat_repository,
)
from deeptutor.teaching.runtime_heartbeat import RuntimeHeartbeatRepository
from deeptutor.teaching.storage_credentials import (
    SqlAlchemyStorageCredentialRepository,
    TenantStorageCredentialResolver,
)

router = APIRouter()


class GenerationSchedulerSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    job_ids: list[str] = Field(
        alias="jobIds",
        min_length=1,
        max_length=MAX_CAPACITY_SNAPSHOT_JOBS,
    )

    @field_validator("job_ids")
    @classmethod
    def validate_job_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("jobIds must be unique")
        return value


def _unavailable_probe(reason: str):
    async def probe() -> None:
        raise HealthProbeFailure(reason)

    return probe


def build_active_health_probe_service(
    settings: PlatformSettings,
    http_client: httpx.AsyncClient,
) -> ActiveHealthProbeService:
    """Compose request-local probes without resolving provider credentials."""

    if not settings.enabled:
        raise RuntimeError("teaching platform is disabled")
    data_plane_repository = SqlAlchemyDataPlaneRepository()
    data_plane_probes = OpenMAICDataPlaneHealthProbes(
        repository=data_plane_repository,
        client_factory=RuntimeOpenMAICHealthClientFactory(settings, http_client),
    )
    try:
        if settings.object_store_mode == "s3":
            credentials_dir = settings.object_store_tenant_credentials_dir
            if credentials_dir is None:
                raise RuntimeError("S3 credential root is unavailable")
            object_store_probe = ObjectStoreHealthProbe(
                settings,
                s3_inventory_repository=SqlAlchemyStorageCredentialRepository(),
                s3_credential_resolver=TenantStorageCredentialResolver(credentials_dir),
            )
        else:
            object_store_probe = ObjectStoreHealthProbe(settings)
    except Exception:
        object_store_probe = _unavailable_probe("object_store_unavailable")

    render_health_url = settings.openmaic_render_health_url
    try:
        render_probe = (
            RenderHealthProbe(
                health_url=render_health_url,
                http_client=http_client,
            )
            if render_health_url is not None
            else _unavailable_probe("render_unhealthy")
        )
    except Exception:
        render_probe = _unavailable_probe("render_unhealthy")

    return ActiveHealthProbeService(
        {
            "database": DatabaseHealthProbe(),
            "migrations": MigrationHealthProbe(SqlAlchemyMigrationHealthRepository()),
            "object_store": object_store_probe,
            "openmaic_shared": data_plane_probes.probe_shared,
            "render_shared": render_probe,
            "dedicated_data_planes": data_plane_probes.probe_dedicated,
        }
    )


async def get_active_health_probe_service() -> AsyncIterator[ActiveHealthProbeService]:
    settings = load_platform_settings()
    if not settings.enabled:
        raise RuntimeError("teaching platform is disabled")
    async with httpx.AsyncClient(trust_env=False) as http_client:
        yield build_active_health_probe_service(settings, http_client)


@router.get(
    "/api/v1/system/teaching-health",
    dependencies=[Depends(require_platform_admin)],
)
async def teaching_health(
    service: TeachingHealthService = Depends(get_teaching_health_service),
    runtime_repository: RuntimeHeartbeatRepository = Depends(get_runtime_heartbeat_repository),
    active_probes: ActiveHealthProbeService = Depends(get_active_health_probe_service),
) -> dict[str, object]:
    active_results = await active_probes.probe()
    return asdict(await service.report_active(runtime_repository, active_results))


def _iso_utc(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


@router.post(
    "/api/v1/system/generation-scheduler-snapshot",
    dependencies=[Depends(require_platform_admin)],
)
async def generation_scheduler_snapshot(
    request: GenerationSchedulerSnapshotRequest,
    repository: SqlAlchemyCapacitySchedulerRepository = Depends(get_capacity_scheduler_repository),
) -> dict[str, object]:
    try:
        snapshot = await repository.fetch_snapshot(tuple(request.job_ids))
    except ValueError:
        raise HTTPException(
            status_code=503,
            detail="generation_scheduler_snapshot_unavailable",
        ) from None
    return {
        "schemaVersion": 1,
        "observedAt": _iso_utc(snapshot.observed_at),
        "jobs": [
            {
                "jobId": job.job_id,
                "tenantId": job.tenant_id,
                "workerPoolRef": job.worker_pool_ref,
                "status": job.status,
                "claimedAt": _iso_utc(job.claimed_at) if job.claimed_at else None,
            }
            for job in snapshot.jobs
        ],
        "claimEvents": [
            {
                "cursor": event.cursor,
                "jobId": event.job_id,
                "tenantId": event.tenant_id,
                "claimedAt": _iso_utc(event.claimed_at),
            }
            for event in snapshot.claim_events
        ],
        "missingJobIds": list(snapshot.missing_job_ids),
        "pools": [
            {
                "workerPoolRef": pool.worker_pool_ref,
                "globalSlotCapacity": pool.global_slot_capacity,
                "tenantSlotCapacities": [
                    {"tenantId": item.tenant_id, "capacity": item.capacity}
                    for item in pool.tenant_capacities
                ],
                "active": [
                    {
                        "jobId": claim.job_id,
                        "tenantId": claim.tenant_id,
                        "ordinal": claim.ordinal,
                    }
                    for claim in pool.active
                ],
            }
            for pool in snapshot.pools
        ],
    }


@router.get("/internal/metrics", include_in_schema=False)
async def teaching_metrics(
    repository: SqlAlchemyTeachingMetricsRepository = Depends(get_teaching_metrics_repository),
) -> Response:
    try:
        snapshot = await repository.fetch_snapshot()
        content = TeachingMetrics().render(snapshot)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="metrics_unavailable") from None
    return Response(content=content, media_type=CONTENT_TYPE_LATEST)
