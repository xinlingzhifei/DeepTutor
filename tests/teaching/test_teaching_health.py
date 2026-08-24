from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from deeptutor.api.routers.auth import require_platform_admin
from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.health import (
    REQUIRED_HEALTH_COMPONENTS,
    TeachingHealthService,
)
from deeptutor.teaching.health_probes import (
    ACTIVE_PROBE_COMPONENTS,
    ActiveHealthProbeService,
    ActiveProbeResult,
)
from deeptutor.teaching.runtime_heartbeat import (
    RUNTIME_PROCESS_ROLES,
    RuntimeHeartbeatSnapshot,
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


class _HealthyRuntimeRepository:
    async def latest_running_heartbeats(self, roles):
        assert tuple(roles) == RUNTIME_PROCESS_ROLES
        return tuple(
            RuntimeHeartbeatSnapshot(role=role, age_seconds=0) for role in RUNTIME_PROCESS_ROLES
        )


@pytest.mark.asyncio
async def test_active_health_report_is_fixed_shape_and_overrides_memory() -> None:
    service = _service()
    service.set_status("database", "unhealthy", reason="old_memory_signal")
    service.set_data_plane_health(
        route_id="private-route-id",
        mode="dedicated",
        service="openmaic",
        status="unhealthy",
    )
    active = {
        component: ActiveProbeResult(status="healthy") for component in ACTIVE_PROBE_COMPONENTS
    }

    report = await service.report_active(_HealthyRuntimeRepository(), active)

    assert report.status == "healthy"
    assert set(report.components) == set(REQUIRED_HEALTH_COMPONENTS)
    assert "data_plane:private-route-id" not in report.components
    assert report.components["database"].status == "healthy"
    assert report.components["openmaic_shared"].service == "openmaic"
    assert report.components["openmaic_shared"].mode == "shared"
    assert report.components["render_shared"].service == "render"
    assert report.components["render_shared"].mode == "shared"
    assert report.components["dedicated_data_planes"].service == "openmaic"
    assert report.components["dedicated_data_planes"].mode == "dedicated"


@pytest.mark.asyncio
async def test_active_results_survive_runtime_repository_failure() -> None:
    class FailingRepository:
        async def latest_running_heartbeats(self, _roles):
            raise RuntimeError("postgresql://private-dsn")

    service = _service()
    active = {
        component: ActiveProbeResult(status="healthy") for component in ACTIVE_PROBE_COMPONENTS
    }
    active["object_store"] = ActiveProbeResult(
        status="unhealthy",
        reason="object_store_unavailable",
    )

    report = await service.report_active(FailingRepository(), active)

    assert report.components["object_store"].reason == "object_store_unavailable"
    assert report.components["database"].status == "healthy"
    for role in RUNTIME_PROCESS_ROLES:
        assert report.components[role].reason == "heartbeat_repository_unavailable"
    assert "private-dsn" not in repr(report)


@pytest.mark.asyncio
async def test_active_health_uses_one_report_timestamp() -> None:
    later = datetime(2026, 8, 21, 1, 0, 1, tzinfo=timezone.utc)
    times = iter((NOW, later))
    service = TeachingHealthService(now=lambda: next(times), stale_after_seconds=90)
    active = {
        component: ActiveProbeResult(status="healthy") for component in ACTIVE_PROBE_COMPONENTS
    }

    report = await service.report_active(_HealthyRuntimeRepository(), active)

    assert report.generated_at == NOW
    assert report.components["database"].checked_at == NOW
    assert report.components["database"].age_seconds == 0


@pytest.mark.asyncio
async def test_heartbeat_repository_timeout_is_fixed_and_cleans_up() -> None:
    cleaned = False

    class BlockingRepository:
        async def latest_running_heartbeats(self, _roles):
            nonlocal cleaned
            try:
                await asyncio.Event().wait()
            finally:
                cleaned = True

    service = TeachingHealthService(
        now=lambda: NOW,
        stale_after_seconds=90,
        heartbeat_timeout_seconds=0.01,
    )
    active = {
        component: ActiveProbeResult(status="healthy") for component in ACTIVE_PROBE_COMPONENTS
    }

    report = await service.report_active(BlockingRepository(), active)

    assert cleaned
    for role in RUNTIME_PROCESS_ROLES:
        assert report.components[role].reason == "heartbeat_repository_unavailable"


@pytest.mark.asyncio
async def test_heartbeat_repository_external_cancellation_propagates() -> None:
    entered = asyncio.Event()
    cleaned = False

    class BlockingRepository:
        async def latest_running_heartbeats(self, _roles):
            nonlocal cleaned
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned = True

    service = _service()
    active = {
        component: ActiveProbeResult(status="healthy") for component in ACTIVE_PROBE_COMPONENTS
    }
    task = asyncio.create_task(service.report_active(BlockingRepository(), active))
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleaned


def test_teaching_health_routes_register_only_for_enabled_platform() -> None:
    from deeptutor.api.main import _register_teaching_health_routes
    from deeptutor.api.routers import teaching_health
    from deeptutor.api.routers.teaching_health import get_active_health_probe_service
    from deeptutor.teaching.metrics import TeachingMetricsSnapshot
    from deeptutor.teaching.repositories.metrics import get_teaching_metrics_repository
    from deeptutor.teaching.repositories.runtime_heartbeats import (
        get_runtime_heartbeat_repository,
    )

    class MetricsRepository:
        async def fetch_snapshot(self):
            return TeachingMetricsSnapshot()

    disabled = FastAPI()
    assert not _register_teaching_health_routes(disabled, enabled=False)
    assert "/api/v1/system/teaching-health" not in disabled.openapi()["paths"]
    assert TestClient(disabled).get("/internal/metrics").status_code == 404

    enabled = FastAPI()
    assert _register_teaching_health_routes(enabled, enabled=True)
    assert "/api/v1/system/teaching-health" in enabled.openapi()["paths"]
    enabled.dependency_overrides[get_teaching_metrics_repository] = MetricsRepository
    assert TestClient(enabled).get("/internal/metrics").status_code == 200
    routes = {
        route.path: route for route in teaching_health.router.routes if isinstance(route, APIRoute)
    }
    health_dependencies = {
        dependency.call
        for dependency in routes["/api/v1/system/teaching-health"].dependant.dependencies
    }
    metrics_dependencies = {
        dependency.call for dependency in routes["/internal/metrics"].dependant.dependencies
    }
    assert require_platform_admin in health_dependencies
    assert get_runtime_heartbeat_repository in health_dependencies
    assert get_active_health_probe_service in health_dependencies
    assert require_platform_admin not in metrics_dependencies
    assert get_teaching_metrics_repository in metrics_dependencies
    assert inspect.iscoroutinefunction(teaching_health.teaching_health)
    assert inspect.iscoroutinefunction(teaching_health.teaching_metrics)


@pytest.mark.asyncio
async def test_internal_metrics_renders_one_durable_absolute_snapshot() -> None:
    from prometheus_client import CONTENT_TYPE_LATEST

    from deeptutor.api.routers.teaching_health import teaching_metrics
    from deeptutor.teaching.metrics import (
        CounterRollup,
        GaugeValue,
        TeachingMetricsSnapshot,
    )

    class Repository:
        calls = 0

        async def fetch_snapshot(self):
            self.calls += 1
            return TeachingMetricsSnapshot(
                counters=(CounterRollup("generation_retries_total", "lease_lost", 9),),
                gauges=(
                    GaugeValue("openmaic_health", "shared", 1),
                    GaugeValue("openmaic_health", "dedicated", 1),
                ),
            )

    repository = Repository()
    response = await teaching_metrics(repository)

    assert repository.calls == 1
    assert response.status_code == 200
    assert response.media_type == CONTENT_TYPE_LATEST
    assert b'yfeistai_generation_retries_total{reason="lease_lost"} 9.0' in response.body


@pytest.mark.asyncio
async def test_internal_metrics_database_or_snapshot_failure_returns_fixed_503() -> None:
    from deeptutor.api.routers.teaching_health import teaching_metrics

    class FailingRepository:
        async def fetch_snapshot(self):
            raise RuntimeError("postgresql://private-user:private-secret@private-host/db")

    with pytest.raises(HTTPException) as raised:
        await teaching_metrics(FailingRepository())

    assert raised.value.status_code == 503
    assert raised.value.detail == "metrics_unavailable"
    assert "private" not in repr(raised.value).lower()


@pytest.mark.asyncio
async def test_internal_metrics_external_cancellation_propagates() -> None:
    from deeptutor.api.routers.teaching_health import teaching_metrics

    class CancelledRepository:
        async def fetch_snapshot(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await teaching_metrics(CancelledRepository())


@pytest.mark.asyncio
async def test_active_probe_dependency_owns_proxy_free_http_client(monkeypatch) -> None:
    from deeptutor.api.routers import teaching_health

    created: list[dict[str, object]] = []

    class HttpClient:
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.closed = True

    client = HttpClient()

    def client_factory(**kwargs):
        created.append(kwargs)
        return client

    monkeypatch.setattr(
        teaching_health,
        "load_platform_settings",
        lambda: PlatformSettings(
            enabled=True,
            database_url=SecretStr("postgresql+asyncpg://app:db@postgres/platform"),
            openmaic_render_health_url="http://openmaic-render:9000/health",
        ),
    )
    monkeypatch.setattr(teaching_health.httpx, "AsyncClient", client_factory)

    dependency = teaching_health.get_active_health_probe_service()
    probe_service = await anext(dependency)

    assert isinstance(probe_service, ActiveHealthProbeService)
    assert created == [{"trust_env": False}]
    assert client.closed is False

    await dependency.aclose()
    assert client.closed is True


@pytest.mark.asyncio
async def test_disabled_active_probe_dependency_builds_no_http_client(monkeypatch) -> None:
    from deeptutor.api.routers import teaching_health

    called = False

    def client_factory(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("disabled teaching must not build health dependencies")

    monkeypatch.setattr(
        teaching_health,
        "load_platform_settings",
        lambda: PlatformSettings(enabled=False),
    )
    monkeypatch.setattr(teaching_health.httpx, "AsyncClient", client_factory)
    dependency = teaching_health.get_active_health_probe_service()

    with pytest.raises(RuntimeError, match="disabled"):
        await anext(dependency)

    assert called is False


@pytest.mark.asyncio
async def test_health_route_runs_active_probes_once_and_uses_fixed_report() -> None:
    from deeptutor.api.routers.teaching_health import teaching_health

    class ProbeService:
        calls = 0

        async def probe(self):
            self.calls += 1
            return {
                component: ActiveProbeResult(status="healthy")
                for component in ACTIVE_PROBE_COMPONENTS
            }

    probes = ProbeService()
    payload = await teaching_health(_service(), _HealthyRuntimeRepository(), probes)

    assert probes.calls == 1
    assert set(payload["components"]) == set(REQUIRED_HEALTH_COMPONENTS)
    assert payload["components"]["dedicated_data_planes"]["mode"] == "dedicated"
    assert not any(key.startswith("data_plane:") for key in payload["components"])


@pytest.mark.asyncio
async def test_health_route_propagates_active_probe_cancellation() -> None:
    from deeptutor.api.routers.teaching_health import teaching_health

    class CancelledProbeService:
        async def probe(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await teaching_health(
            _service(),
            _HealthyRuntimeRepository(),
            CancelledProbeService(),
        )


@pytest.mark.asyncio
async def test_probe_factory_maps_missing_s3_and_render_dependencies(
    monkeypatch,
    tmp_path,
) -> None:
    from deeptutor.api.routers import teaching_health

    async def healthy() -> None:
        return None

    class DataPlaneProbes:
        probe_shared = staticmethod(healthy)
        probe_dedicated = staticmethod(healthy)

    monkeypatch.setattr(teaching_health, "DatabaseHealthProbe", lambda: healthy)
    monkeypatch.setattr(
        teaching_health,
        "MigrationHealthProbe",
        lambda _repository: healthy,
    )
    monkeypatch.setattr(
        teaching_health,
        "OpenMAICDataPlaneHealthProbes",
        lambda **_kwargs: DataPlaneProbes(),
    )
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://app:db@postgres/platform"),
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_namespace_id="health-namespace",
        object_store_tenant_credentials_dir=tmp_path / "missing",
        openmaic_render_health_url=None,
    )

    service = teaching_health.build_active_health_probe_service(settings, object())
    results = await service.probe()

    assert results["object_store"] == ActiveProbeResult(
        status="unhealthy",
        reason="object_store_unavailable",
    )
    assert results["render_shared"] == ActiveProbeResult(
        status="unhealthy",
        reason="render_unhealthy",
    )
