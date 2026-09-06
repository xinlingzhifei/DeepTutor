"""Private metrics and administrator teaching-health endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
import hmac
import os
from pathlib import Path
import re
import stat
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response
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
_OBSERVER_CANARY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBSERVER_HEALTH_TOKEN = re.compile(r"^[A-Za-z0-9_-]{48,128}$")
_OBSERVER_HEALTH_TOKEN_FILE = "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE"
_CANDIDATE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_SHA256_ENV = "YFEISTAI_CANDIDATE_SHA256"


def _observer_candidate_sha256() -> str:
    value = os.environ.get(_CANDIDATE_SHA256_ENV, "")
    if _CANDIDATE_SHA256.fullmatch(value) is None or value == "0" * 64:
        raise HTTPException(status_code=503, detail="observer_candidate_unavailable")
    return value


def _observer_health_token() -> str:
    raw_path = os.environ.get(_OBSERVER_HEALTH_TOKEN_FILE)
    if not raw_path:
        raise HTTPException(status_code=503, detail="observer_credential_unavailable")
    supplied = Path(raw_path)
    descriptor: int | None = None
    try:
        if not supplied.is_absolute():
            raise OSError
        resolved = supplied.resolve(strict=True)
        if supplied != resolved or any(
            parent.is_symlink() for parent in (resolved, *resolved.parents)
        ):
            raise OSError
        before = resolved.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise OSError
        if os.name == "posix" and (
            before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise OSError
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        body = os.read(descriptor, 130)
        if os.read(descriptor, 1):
            raise OSError
        after = os.fstat(descriptor)
        final = resolved.lstat()

        def identity(details: os.stat_result) -> tuple[int, ...]:
            return (
                details.st_dev,
                details.st_ino,
                details.st_size,
                details.st_mtime_ns,
                details.st_ctime_ns,
                details.st_mode,
                details.st_uid,
            )

        if identity(before) != identity(opened) or identity(opened) != identity(after):
            raise OSError
        if identity(opened) != identity(final):
            raise OSError
        token = body.decode("ascii").strip()
        encoded = token.encode("ascii")
        if _OBSERVER_HEALTH_TOKEN.fullmatch(token) is None or body not in {
            encoded,
            encoded + b"\n",
            encoded + b"\r\n",
        }:
            raise OSError
        return token
    except (OSError, UnicodeError) as exc:
        raise HTTPException(status_code=503, detail="observer_credential_unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def require_observer_canary_token(
    authorization: Annotated[list[str] | None, Header(alias="Authorization")] = None,
) -> None:
    if authorization is None or len(authorization) != 1:
        raise HTTPException(status_code=401, detail="observer_credential_invalid")
    scheme, separator, supplied = authorization[0].partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or _OBSERVER_HEALTH_TOKEN.fullmatch(supplied) is None
        or not hmac.compare_digest(supplied, _observer_health_token())
    ):
        raise HTTPException(status_code=401, detail="observer_credential_invalid")


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


@router.get(
    "/api/v1/system/teaching-health/observer-canary",
    dependencies=[Depends(require_observer_canary_token)],
)
async def observer_canary_health(
    active_probes: ActiveHealthProbeService = Depends(get_active_health_probe_service),
    observer_canary: Annotated[
        list[str] | None,
        Header(alias="X-YFeiSTAI-Observer-Canary"),
    ] = None,
) -> dict[str, object]:
    if (
        observer_canary is None
        or len(observer_canary) != 1
        or _OBSERVER_CANARY.fullmatch(observer_canary[0]) is None
    ):
        raise HTTPException(status_code=400, detail="observer_canary_invalid")
    active_results = await active_probes.probe(openmaic_shared_observer_canary=observer_canary[0])
    shared = active_results.get("openmaic_shared")
    if shared is None:
        raise HTTPException(status_code=503, detail="observer_canary_unavailable")
    return {
        "candidateSha256": _observer_candidate_sha256(),
        "components": {
            "openmaic_shared": {
                "status": shared.status,
                "service": "openmaic",
                "mode": "shared",
            }
        },
    }


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
