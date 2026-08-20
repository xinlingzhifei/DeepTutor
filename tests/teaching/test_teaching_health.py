from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.routing import APIRoute

from deeptutor.api.routers.auth import require_platform_admin
from deeptutor.teaching.health import (
    REQUIRED_HEALTH_COMPONENTS,
    TeachingHealthService,
)

NOW = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)


def _service() -> TeachingHealthService:
    return TeachingHealthService(now=lambda: NOW, stale_after_seconds=90)


def test_health_reports_degraded_when_dispatcher_heartbeat_is_stale() -> None:
    service = _service()
    for component in REQUIRED_HEALTH_COMPONENTS:
        service.set_status(component, "healthy")
    service.set_heartbeat("dispatcher", age_seconds=91)

    report = service.report()

    assert report.status == "degraded"
    assert report.components["dispatcher"].status == "stale"
    assert report.components["dispatcher"].age_seconds == 91


def test_health_is_fail_closed_and_lists_every_required_component() -> None:
    service = _service()

    report = service.report()

    assert report.status == "degraded"
    assert set(report.components) == set(REQUIRED_HEALTH_COMPONENTS)
    assert {item.status for item in report.components.values()} == {"unknown"}


def test_health_includes_registered_shared_and_dedicated_data_planes() -> None:
    service = _service()
    for component in REQUIRED_HEALTH_COMPONENTS:
        service.set_status(component, "healthy")
    service.set_data_plane_health(
        route_id="shared-openmaic",
        mode="shared",
        service="openmaic",
        status="healthy",
    )
    service.set_data_plane_health(
        route_id="tenant-a-render",
        mode="dedicated",
        service="render",
        status="unhealthy",
        reason="contract_mismatch",
    )

    report = service.report()

    assert report.status == "degraded"
    assert report.components["data_plane:shared-openmaic"].mode == "shared"
    dedicated = report.components["data_plane:tenant-a-render"]
    assert dedicated.mode == "dedicated"
    assert dedicated.service == "render"
    assert dedicated.status == "unhealthy"
    assert dedicated.reason == "contract_mismatch"


def test_teaching_health_routes_register_only_for_enabled_platform() -> None:
    from deeptutor.api.main import _register_teaching_health_routes

    disabled = FastAPI()
    assert not _register_teaching_health_routes(disabled, enabled=False)
    assert "/api/v1/system/teaching-health" not in {route.path for route in disabled.routes}
    assert "/internal/metrics" not in {route.path for route in disabled.routes}

    enabled = FastAPI()
    assert _register_teaching_health_routes(enabled, enabled=True)
    paths = {route.path for route in enabled.routes}
    assert "/api/v1/system/teaching-health" in paths
    assert "/internal/metrics" in paths
    routes = {route.path: route for route in enabled.routes if isinstance(route, APIRoute)}
    health_dependencies = {
        dependency.call
        for dependency in routes["/api/v1/system/teaching-health"].dependant.dependencies
    }
    metrics_dependencies = {
        dependency.call for dependency in routes["/internal/metrics"].dependant.dependencies
    }
    assert require_platform_admin in health_dependencies
    assert require_platform_admin not in metrics_dependencies
