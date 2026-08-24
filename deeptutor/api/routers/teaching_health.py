"""Private metrics and administrator teaching-health endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST

from deeptutor.api.routers.auth import require_platform_admin
from deeptutor.teaching.health import (
    TeachingHealthService,
    get_teaching_health_service,
)
from deeptutor.teaching.metrics import TeachingMetrics, get_teaching_metrics
from deeptutor.teaching.repositories.runtime_heartbeats import (
    get_runtime_heartbeat_repository,
)
from deeptutor.teaching.runtime_heartbeat import RuntimeHeartbeatRepository

router = APIRouter()


@router.get(
    "/api/v1/system/teaching-health",
    dependencies=[Depends(require_platform_admin)],
)
async def teaching_health(
    service: TeachingHealthService = Depends(get_teaching_health_service),
    runtime_repository: RuntimeHeartbeatRepository = Depends(get_runtime_heartbeat_repository),
) -> dict[str, object]:
    return asdict(await service.report_durable(runtime_repository))


@router.get("/internal/metrics", include_in_schema=False)
def teaching_metrics(
    metrics: TeachingMetrics = Depends(get_teaching_metrics),
) -> Response:
    return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)
