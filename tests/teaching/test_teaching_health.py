from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import errno
import inspect
import os
from pathlib import Path

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
    from deeptutor.api.routers.teaching_health import (
        get_active_health_probe_service,
        require_observer_canary_token,
    )
    from deeptutor.teaching.health import get_teaching_health_service
    from deeptutor.teaching.metrics import TeachingMetricsSnapshot
    from deeptutor.teaching.repositories.capacity_scheduler import (
        get_capacity_scheduler_repository,
    )
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
    assert "/api/v1/system/teaching-health/observer-canary" not in disabled.openapi()["paths"]
    assert TestClient(disabled).get("/internal/metrics").status_code == 404

    enabled = FastAPI()
    assert _register_teaching_health_routes(enabled, enabled=True)
    assert "/api/v1/system/teaching-health" in enabled.openapi()["paths"]
    assert "/api/v1/system/teaching-health/observer-canary" in enabled.openapi()["paths"]
    assert "/api/v1/system/generation-scheduler-snapshot" in enabled.openapi()["paths"]
    enabled.dependency_overrides[get_teaching_metrics_repository] = MetricsRepository
    assert TestClient(enabled).get("/internal/metrics").status_code == 200
    routes = {
        route.path: route for route in teaching_health.router.routes if isinstance(route, APIRoute)
    }
    health_dependencies = {
        dependency.call
        for dependency in routes["/api/v1/system/teaching-health"].dependant.dependencies
    }
    observer_dependencies = {
        dependency.call
        for dependency in routes[
            "/api/v1/system/teaching-health/observer-canary"
        ].dependant.dependencies
    }
    metrics_dependencies = {
        dependency.call for dependency in routes["/internal/metrics"].dependant.dependencies
    }
    snapshot_dependencies = {
        dependency.call
        for dependency in routes[
            "/api/v1/system/generation-scheduler-snapshot"
        ].dependant.dependencies
    }
    assert require_platform_admin in health_dependencies
    assert require_observer_canary_token not in health_dependencies
    assert get_teaching_health_service in health_dependencies
    assert get_runtime_heartbeat_repository in health_dependencies
    assert get_active_health_probe_service in health_dependencies
    assert require_observer_canary_token in observer_dependencies
    assert require_platform_admin not in observer_dependencies
    assert get_active_health_probe_service in observer_dependencies
    assert get_teaching_health_service not in observer_dependencies
    assert get_runtime_heartbeat_repository not in observer_dependencies
    assert require_platform_admin not in metrics_dependencies
    assert get_teaching_metrics_repository in metrics_dependencies
    assert require_platform_admin in snapshot_dependencies
    assert get_capacity_scheduler_repository in snapshot_dependencies
    assert inspect.iscoroutinefunction(teaching_health.teaching_health)
    assert inspect.iscoroutinefunction(teaching_health.observer_canary_health)
    assert "observer_canary" not in inspect.signature(teaching_health.teaching_health).parameters
    assert "observer_canary" in inspect.signature(teaching_health.observer_canary_health).parameters
    assert inspect.iscoroutinefunction(teaching_health.teaching_metrics)
    assert inspect.iscoroutinefunction(teaching_health.generation_scheduler_snapshot)


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
async def test_health_route_forwards_one_valid_observer_canary_to_active_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers.teaching_health import observer_canary_health

    challenge = "observer-canary-" + "7" * 48
    candidate_sha256 = "a" * 64
    monkeypatch.setenv("YFEISTAI_CANDIDATE_SHA256", candidate_sha256)

    class ProbeService:
        received: str | None = None

        async def probe(self, *, openmaic_shared_observer_canary=None):
            self.received = openmaic_shared_observer_canary
            return {
                component: ActiveProbeResult(status="healthy")
                for component in ACTIVE_PROBE_COMPONENTS
            }

    probes = ProbeService()
    payload = await observer_canary_health(
        probes,
        observer_canary=[challenge],
    )

    assert probes.received == challenge
    assert payload == {
        "candidateSha256": candidate_sha256,
        "components": {
            "openmaic_shared": {
                "status": "healthy",
                "service": "openmaic",
                "mode": "shared",
            }
        },
    }


@pytest.mark.asyncio
async def test_observer_canary_health_returns_the_runtime_candidate_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers import teaching_health

    candidate_sha256 = "a" * 64
    monkeypatch.setenv("YFEISTAI_CANDIDATE_SHA256", candidate_sha256)

    class ProbeService:
        async def probe(self, *, openmaic_shared_observer_canary=None):
            assert openmaic_shared_observer_canary == "observer-canary-" + "7" * 48
            return {
                component: ActiveProbeResult(status="healthy")
                for component in ACTIVE_PROBE_COMPONENTS
            }

    payload = await teaching_health.observer_canary_health(
        ProbeService(),
        observer_canary=["observer-canary-" + "7" * 48],
    )

    assert payload["candidateSha256"] == candidate_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observer_canary",
    [None, [], ["bad canary"], ["valid-canary", "duplicate-canary"]],
)
async def test_health_route_rejects_invalid_or_duplicate_observer_canary(
    observer_canary: list[str] | None,
) -> None:
    from deeptutor.api.routers.teaching_health import observer_canary_health

    class ProbeService:
        async def probe(self, **_kwargs):
            raise AssertionError("invalid observer challenge must not start probes")

    with pytest.raises(HTTPException) as error:
        await observer_canary_health(
            ProbeService(),
            observer_canary=observer_canary,
        )

    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "supplied",
    [
        pytest.param("\u00e9" + "a" * 47, id="latin1-non-ascii"),
        pytest.param("\u89c2\u5bdf" + "a" * 46, id="unicode"),
        pytest.param("a" * 47, id="short-token"),
        pytest.param("a" * 129, id="long-token"),
        pytest.param("/" + "a" * 47, id="invalid-character"),
    ],
)
async def test_observer_health_dependency_rejects_malformed_bearer_before_secret_read(
    supplied: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers import teaching_health

    observer_token = "8" * 64
    secret_reads = 0

    def read_observer_token() -> str:
        nonlocal secret_reads
        secret_reads += 1
        return observer_token

    monkeypatch.setattr(teaching_health, "_observer_health_token", read_observer_token)

    with pytest.raises(HTTPException) as rejected:
        await teaching_health.require_observer_canary_token(authorization=[f"Bearer {supplied}"])

    assert rejected.value.status_code == 401
    assert rejected.value.detail == "observer_credential_invalid"
    assert supplied not in str(rejected.value)
    assert observer_token not in str(rejected.value)
    assert secret_reads == 0


@pytest.mark.asyncio
async def test_observer_health_dependency_rejects_a_distinct_platform_admin_bearer(
    monkeypatch,
) -> None:
    from deeptutor.api.routers import teaching_health

    observer_token = "observer-health-token-" + "8" * 48
    platform_admin_token = "platform-admin-token-" + "9" * 48
    monkeypatch.setattr(teaching_health, "_observer_health_token", lambda: observer_token)

    await teaching_health.require_observer_canary_token(authorization=[f"Bearer {observer_token}"])
    with pytest.raises(HTTPException) as rejected:
        await teaching_health.require_observer_canary_token(
            authorization=[f"Bearer {platform_admin_token}"]
        )

    assert rejected.value.status_code == 401


@pytest.mark.parametrize(
    ("token", "newline"),
    [
        pytest.param("a" * 48, b"", id="no-newline-minimum-length"),
        pytest.param("b" * 64, b"\n", id="lf"),
        pytest.param("c" * 128, b"\r\n", id="crlf-maximum-length"),
    ],
)
def test_observer_health_token_reads_valid_private_file(
    token: str,
    newline: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers import teaching_health

    token_file = tmp_path / "private-observer-health.token"
    token_file.write_bytes(token.encode("ascii") + newline)
    token_file.chmod(0o600)
    monkeypatch.setenv("YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE", str(token_file))

    assert teaching_health._observer_health_token() == token


@pytest.mark.parametrize("location", ["missing-env", "relative-path", "missing-file"])
def test_observer_health_token_rejects_unavailable_file_with_static_error(
    location: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers import teaching_health

    token_file = tmp_path / "private-observer-health.token"
    if location == "missing-env":
        monkeypatch.delenv("YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE", raising=False)
    else:
        supplied = token_file.name if location == "relative-path" else str(token_file)
        monkeypatch.setenv("YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE", supplied)

    with pytest.raises(HTTPException) as rejected:
        teaching_health._observer_health_token()

    assert rejected.value.status_code == 503
    assert rejected.value.detail == "observer_credential_unavailable"
    assert str(token_file) not in str(rejected.value)
    assert token_file.name not in str(rejected.value)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"/" + b"a" * 63, id="invalid-ascii-character"),
        pytest.param(b"\xc3\xa9" + b"a" * 62, id="non-ascii"),
        pytest.param(b"a" * 129, id="token-exceeds-maximum-length"),
        pytest.param(b"a" * 131, id="file-exceeds-read-bound"),
        pytest.param(b"a" * 64 + b"\n\n", id="extra-newline"),
        pytest.param(b"", id="empty-file"),
    ],
)
def test_observer_health_token_rejects_invalid_file_contents_with_static_error(
    body: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers import teaching_health

    token_file = tmp_path / "private-observer-health.token"
    token_file.write_bytes(body)
    token_file.chmod(0o600)
    monkeypatch.setenv("YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE", str(token_file))

    with pytest.raises(HTTPException) as rejected:
        teaching_health._observer_health_token()

    assert rejected.value.status_code == 503
    assert rejected.value.detail == "observer_credential_unavailable"
    assert str(token_file) not in str(rejected.value)
    if body:
        assert body.decode("ascii", errors="replace") not in str(rejected.value)


@pytest.mark.parametrize("valid", [True, False], ids=["success", "rejection"])
def test_observer_health_token_closes_opened_descriptor(
    valid: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers import teaching_health

    token = "a" * 64
    token_file = tmp_path / "private-observer-health.token"
    token_file.write_bytes(token.encode("ascii") if valid else b"/" + b"a" * 63)
    token_file.chmod(0o600)
    monkeypatch.setenv("YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE", str(token_file))
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def record_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def record_close(descriptor: int) -> None:
        real_close(descriptor)
        closed.append(descriptor)

    with monkeypatch.context() as descriptor_spy:
        descriptor_spy.setattr(teaching_health.os, "open", record_open)
        descriptor_spy.setattr(teaching_health.os, "close", record_close)
        if valid:
            assert teaching_health._observer_health_token() == token
        else:
            with pytest.raises(HTTPException) as rejected:
                teaching_health._observer_health_token()
            assert rejected.value.status_code == 503
            assert rejected.value.detail == "observer_credential_unavailable"

    assert len(opened) == 1
    assert closed == opened
    with pytest.raises(OSError) as invalid_descriptor:
        os.fstat(opened[0])
    assert invalid_descriptor.value.errno == errno.EBADF


@pytest.mark.parametrize(
    "candidate_sha256",
    [
        pytest.param(None, id="missing-env"),
        pytest.param("0" * 64, id="zero-digest"),
        pytest.param("g" * 64, id="malformed-digest"),
    ],
)
def test_observer_candidate_sha256_rejects_unavailable_digest_with_static_error(
    candidate_sha256: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.api.routers import teaching_health

    if candidate_sha256 is None:
        monkeypatch.delenv("YFEISTAI_CANDIDATE_SHA256", raising=False)
    else:
        monkeypatch.setenv("YFEISTAI_CANDIDATE_SHA256", candidate_sha256)

    with pytest.raises(HTTPException) as rejected:
        teaching_health._observer_candidate_sha256()

    assert rejected.value.status_code == 503
    assert rejected.value.detail == "observer_candidate_unavailable"
    if candidate_sha256 is not None:
        assert candidate_sha256 not in str(rejected.value)


def test_observer_candidate_sha256_returns_valid_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    from deeptutor.api.routers import teaching_health

    candidate_sha256 = "a1" * 32
    monkeypatch.setenv("YFEISTAI_CANDIDATE_SHA256", candidate_sha256)

    assert teaching_health._observer_candidate_sha256() == candidate_sha256


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
