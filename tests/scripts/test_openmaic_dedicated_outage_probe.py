from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "openmaic_dedicated_outage_probe.py"
SOURCE_HEAD = "a" * 40
RUNTIME_SHA256 = "b" * 64
BASE_URL = "https://candidate.example.test"
OBSERVER_ID = "shared-ingress-observer-environment-dedicated-01"
OBSERVER_URL = "https://observer.example.test"
SHARED_CONTROL_URL = "https://shared-ingress.example.test/v1/control-canaries"
DOCKER_HOST_IDENTITY_SHA256 = hashlib.sha256(
    b'{"scheme":"unix","socketPath":"/var/run/docker.sock"}\n'
).hexdigest()


def _module():
    name = "openmaic_dedicated_outage_probe_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _candidate() -> dict[str, object]:
    return {
        "sourceRepository": "xinlingzhifei/DeepTutor",
        "sourceHead": SOURCE_HEAD,
        "releaseTag": f"yfeistai-first-release-20260830-{SOURCE_HEAD[:8]}",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": "sha256:" + "c" * 64,
            "openmaic": "sha256:" + "d" * 64,
            "openmaic_render": "sha256:" + "e" * 64,
        },
    }


def _observer_attestation() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "producer": "openmaic-shared-ingress-observer",
        "releaseRun": {
            "runId": "run-dedicated-01",
            "environmentId": "environment-dedicated-01",
        },
        "observedAt": "2026-08-30T00:00:00Z",
        "observer": {
            "observerId": OBSERVER_ID,
            "observerUrl": OBSERVER_URL,
            "sharedIngressControlUrl": SHARED_CONTROL_URL,
        },
    }


def _observer_attestation_body() -> bytes:
    return (
        json.dumps(
            _observer_attestation(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _docker_boundary() -> dict[str, str]:
    return {
        "dockerHostIdentitySha256": DOCKER_HOST_IDENTITY_SHA256,
        "daemonIdentityBeforeSha256": "2" * 64,
        "daemonIdentityAfterSha256": "2" * 64,
        "inventoryBeforeSha256": "3" * 64,
        "inventoryAfterSha256": "3" * 64,
    }


def _docker_daemon_document() -> dict[str, str]:
    return {
        "ID": "daemon-01",
        "Name": "docker-host-01",
        "ServerVersion": "24.0.9",
        "Driver": "overlay2",
        "OperatingSystem": "Ubuntu 22.04",
        "KernelVersion": "6.8.0",
        "DockerRootDir": "/var/lib/docker",
    }


def _docker_inventory_document(config) -> dict[str, str]:
    return {
        "ID": config.dedicated_container_id,
        "Names": "openmaic",
        "Image": config.openmaic_image_reference,
        "State": "running",
    }


def _docker_command(config, *arguments: str) -> list[str]:
    return [
        str(config.docker_path),
        "--config",
        str(config.docker_config_dir),
        "--host",
        config.docker_host,
        *arguments,
    ]


def _docker_preflight_response(config, command: list[str]):
    payload = command[5:]
    if payload == ["info", "--format", "{{json .}}"]:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_docker_daemon_document()).encode(),
            stderr=b"",
        )
    if payload == ["ps", "-a", "--no-trunc", "--format", "{{json .}}"]:
        return SimpleNamespace(
            returncode=0,
            stdout=(json.dumps(_docker_inventory_document(config)) + "\n").encode(),
            stderr=b"",
        )
    return None


def _config(module, tmp_path: Path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(exist_ok=True)
    observer_path = runtime_root / "openmaic-shared-ingress-observer-attestation.json"
    observer_body = _observer_attestation_body()
    observer_path.write_bytes(observer_body)
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir(exist_ok=True)
    return module.OutageProbeConfig(
        admin_token=SecretStr("admin-token-never-serialize"),
        attempt_marker_path=runtime_root / "openmaic-dedicated-outage-attempt.json",
        base_url=BASE_URL,
        candidate=_candidate(),
        candidate_root=tmp_path,
        docker_path=tmp_path / "docker",
        dedicated_container_id="f" * 64,
        dedicated_project="yfeistai-outage-run-dedicated-01",
        dedicated_route_id="dedicated-tenant-dedicated-01",
        dedicated_tenant_id="tenant-dedicated-01",
        openmaic_image_reference=(
            "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:" + "d" * 64
        ),
        release_run={
            "runId": "run-dedicated-01",
            "environmentId": "environment-dedicated-01",
        },
        observer_attestation_path=observer_path,
        observer_attestation_sha256=hashlib.sha256(observer_body).hexdigest(),
        observer_id=OBSERVER_ID,
        observer_url=OBSERVER_URL,
        observer_origin="https://observer.example.test",
        shared_ingress_control_url=SHARED_CONTROL_URL,
        shared_ingress_control_origin="https://shared-ingress.example.test",
        output_path=runtime_root / "openmaic-dedicated-outage-attestation.json",
        runtime_attestation_sha256=RUNTIME_SHA256,
        timeout_seconds=300,
        docker_config_dir=docker_config,
        docker_host="unix:///var/run/docker.sock",
        docker_host_identity_sha256=DOCKER_HOST_IDENTITY_SHA256,
    )


class _Runtime:
    def __init__(self, module) -> None:
        self.module = module
        self.events: list[str] = []
        self.control_count = 0

    async def __aenter__(self):
        self.events.append("enter")
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.events.append("exit")

    async def verify_disposable_plane(self):
        self.events.append("verify")
        return self.module.DedicatedPlaneIdentity(
            container_id="f" * 64,
            project="yfeistai-outage-run-dedicated-01",
            route_id="dedicated-tenant-dedicated-01",
            tenant_id="tenant-dedicated-01",
        )

    async def prepare_outage_fixture(self) -> None:
        self.events.append("prepare")

    async def control_shared_ingress(self):
        self.control_count += 1
        self.events.append(f"control-{self.control_count}")
        return self.module.SharedIngressObservation(
            observation_id="shared-ingress-run-dedicated-01",
            request_count=7 if self.control_count == 1 else 8,
        )

    async def read_shared_ingress(self):
        self.events.append("observe-critical-after")
        return self.module.SharedIngressObservation(
            observation_id="shared-ingress-run-dedicated-01",
            request_count=7,
        )

    async def stop_dedicated_plane(self, identity) -> None:
        assert identity.container_id == "f" * 64
        self.events.append("stop")

    async def submit_outage_job(self) -> str:
        self.events.append("submit")
        return "job-dedicated-outage-01"

    async def wait_for_terminal_job(self, job_id: str):
        assert job_id == "job-dedicated-outage-01"
        self.events.append("terminal")
        return self.module.TerminalJob(
            job_id=job_id,
            status="failed",
            error_code="dedicated_data_plane_unavailable",
        )

    async def read_job_route_evidence(self, job_id: str):
        assert job_id == "job-dedicated-outage-01"
        self.events.append("outage-binding")
        return self.module.RouteAttemptEvidence(
            route_id="dedicated-tenant-dedicated-01",
            job_id=job_id,
            job_status="failed",
            attempt_count=2,
            shared_attempt_count=0,
            dedicated_attempt_count=2,
            selected_attempt_count=2,
            unavailable_attempt_count=0,
            history_complete=True,
        )

    async def start_dedicated_plane(self, identity) -> None:
        assert identity.container_id == "f" * 64
        self.events.append("start")

    async def wait_dedicated_ready(self, identity) -> None:
        assert identity.route_id == "dedicated-tenant-dedicated-01"
        self.events.append("ready")

    async def run_restoration_canary(self):
        self.events.append("canary")
        return self.module.RouteAttemptEvidence(
            route_id="dedicated-tenant-dedicated-01",
            job_id="job-dedicated-canary-01",
            job_status="succeeded",
            attempt_count=1,
            shared_attempt_count=0,
            dedicated_attempt_count=1,
            selected_attempt_count=1,
            unavailable_attempt_count=0,
            history_complete=True,
        )

    def fixture_audit_inventory(self):
        return self.module.FixtureAuditInventory(
            reversible_resources_deleted=(
                "classEnrollment",
                "tenantMembership",
                "teacherIdentity",
            ),
            retained_resources=(
                self.module.RetainedAuditResource("course", "course-dedicated-01"),
                self.module.RetainedAuditResource("class", "class-dedicated-01"),
                self.module.RetainedAuditResource("generationQuotaGrant", "tenant-dedicated-01"),
                self.module.RetainedAuditResource("classroomAsset", "asset-outage-01"),
                self.module.RetainedAuditResource("generationJob", "job-dedicated-outage-01"),
                self.module.RetainedAuditResource("classroomAsset", "asset-canary-01"),
                self.module.RetainedAuditResource("generationJob", "job-dedicated-canary-01"),
            ),
        )

    def docker_boundary_attestation(self) -> dict[str, str]:
        return _docker_boundary()


def test_outage_probe_proves_failure_without_shared_ingress_and_restores_before_canary(
    tmp_path: Path,
) -> None:
    module = _module()
    runtime = _Runtime(module)
    config = _config(module, tmp_path)

    body = asyncio.run(
        module.run_dedicated_outage_probe(
            config,
            runtime=runtime,
            observed_at=lambda: "2026-08-30T00:00:01Z",
        )
    )

    report = json.loads(body)
    assert body == module.canonical_openmaic_dedicated_outage_attestation(report)
    assert report == {
        "schemaVersion": 1,
        "producer": "openmaic-dedicated-outage",
        "candidate": _candidate(),
        "releaseRun": {
            "runId": "run-dedicated-01",
            "environmentId": "environment-dedicated-01",
        },
        "observedAt": "2026-08-30T00:00:01Z",
        "baseUrl": BASE_URL,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": RUNTIME_SHA256,
        },
        "observerAttestation": {
            "artifact": "runtime/openmaic-shared-ingress-observer-attestation.json",
            "sha256": config.observer_attestation_sha256,
            "observerId": OBSERVER_ID,
            "observerOrigin": "https://observer.example.test",
            "sharedIngressControlOrigin": "https://shared-ingress.example.test",
        },
        "fixture": {
            "tenantId": "tenant-dedicated-01",
            "attemptMarker": {
                "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
                "sha256": hashlib.sha256(config.attempt_marker_path.read_bytes()).hexdigest(),
            },
            "cleanupBoundary": {
                "reason": "formal-delete-api-unavailable",
                "reversibleResourcesDeleted": [
                    "classEnrollment",
                    "tenantMembership",
                    "teacherIdentity",
                ],
                "retainedAuditResources": [
                    {"resourceType": "course", "resourceId": "course-dedicated-01"},
                    {"resourceType": "class", "resourceId": "class-dedicated-01"},
                    {
                        "resourceType": "generationQuotaGrant",
                        "resourceId": "tenant-dedicated-01",
                    },
                    {"resourceType": "classroomAsset", "resourceId": "asset-outage-01"},
                    {
                        "resourceType": "generationJob",
                        "resourceId": "job-dedicated-outage-01",
                    },
                    {"resourceType": "classroomAsset", "resourceId": "asset-canary-01"},
                    {
                        "resourceType": "generationJob",
                        "resourceId": "job-dedicated-canary-01",
                    },
                ],
            },
        },
        "provenance": {
            "attemptMarker": {
                "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
                "sha256": hashlib.sha256(config.attempt_marker_path.read_bytes()).hexdigest(),
            },
            "observerTrustAnchor": {
                "sha256": config.observer_attestation_sha256,
                "observerId": OBSERVER_ID,
                "observerOrigin": "https://observer.example.test",
                "sharedIngressControlOrigin": "https://shared-ingress.example.test",
            },
            "dockerBoundary": _docker_boundary(),
        },
        "outage": {
            "dedicatedPlaneStopped": True,
            "routeId": "dedicated-tenant-dedicated-01",
            "jobId": "job-dedicated-outage-01",
            "jobStatus": "failed",
            "errorCode": "dedicated_data_plane_unavailable",
            "attemptCount": 2,
            "sharedRouteAttemptCount": 0,
            "dedicatedRouteAttemptCount": 2,
            "selectedRouteAttemptCount": 2,
            "unavailableRouteAttemptCount": 0,
            "routeAttemptHistoryComplete": True,
        },
        "sharedIngress": {
            "observationId": "shared-ingress-run-dedicated-01",
            "requestCountBefore": 7,
            "requestCountAfter": 7,
        },
        "restoration": {
            "dedicatedPlaneRestored": True,
            "routeId": "dedicated-tenant-dedicated-01",
            "canaryJobId": "job-dedicated-canary-01",
            "canaryJobStatus": "succeeded",
            "attemptCount": 1,
            "sharedRouteAttemptCount": 0,
            "dedicatedRouteAttemptCount": 1,
            "selectedRouteAttemptCount": 1,
            "unavailableRouteAttemptCount": 0,
            "routeAttemptHistoryComplete": True,
        },
    }
    attempt_marker_body = config.attempt_marker_path.read_bytes()
    contract_module = sys.modules["openmaic_smoke_contract"]
    assert contract_module.parse_openmaic_dedicated_outage_attempt_marker(
        attempt_marker_body,
        candidate=config.candidate,
        release_run=config.release_run,
        expected_observer_attestation_sha256=config.observer_attestation_sha256,
        expected_observer_id=config.observer_id,
        expected_observer_origin=config.observer_origin,
        expected_shared_ingress_control_origin=config.shared_ingress_control_origin,
        expected_tenant_id=config.dedicated_tenant_id,
        expected_route_id=config.dedicated_route_id,
    ) == json.loads(attempt_marker_body)
    assert "admin-token-never-serialize" not in body.decode("utf-8")
    assert runtime.events == [
        "enter",
        "verify",
        "prepare",
        "control-1",
        "stop",
        "submit",
        "terminal",
        "outage-binding",
        "observe-critical-after",
        "start",
        "ready",
        "canary",
        "control-2",
        "exit",
    ]


def test_inner_outage_probe_never_self_reports_native_exit_or_publishes_success(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)

    body = asyncio.run(
        module.run_dedicated_outage_probe(
            config,
            runtime=_Runtime(module),
            observed_at=lambda: "2026-08-30T00:00:01Z",
        )
    )

    assert "execution" not in json.loads(body)
    assert not config.output_path.exists()


def test_inner_outage_probe_binds_actual_marker_observer_anchor_and_docker_boundary(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)

    report = json.loads(
        asyncio.run(
            module.run_dedicated_outage_probe(
                config,
                runtime=_Runtime(module),
                observed_at=lambda: "2026-08-30T00:00:01Z",
            )
        )
    )
    marker_body = config.attempt_marker_path.read_bytes()

    assert report["provenance"] == {
        "attemptMarker": {
            "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
            "sha256": hashlib.sha256(marker_body).hexdigest(),
        },
        "observerTrustAnchor": {
            "sha256": config.observer_attestation_sha256,
            "observerId": config.observer_id,
            "observerOrigin": config.observer_origin,
            "sharedIngressControlOrigin": config.shared_ingress_control_origin,
        },
        "dockerBoundary": _docker_boundary(),
    }


def test_outage_probe_restores_and_canaries_before_reporting_shared_ingress_drift(
    tmp_path: Path,
) -> None:
    module = _module()

    class DriftRuntime(_Runtime):
        async def read_shared_ingress(self):
            self.events.append("observe-critical-after")
            return self.module.SharedIngressObservation(
                observation_id="shared-ingress-run-dedicated-01",
                request_count=8,
            )

        async def control_shared_ingress(self):
            self.control_count += 1
            self.events.append(f"control-{self.control_count}")
            return self.module.SharedIngressObservation(
                observation_id="shared-ingress-run-dedicated-01",
                request_count=7 if self.control_count == 1 else 9,
            )

    runtime = DriftRuntime(module)

    with pytest.raises(module.OpenMAICDedicatedOutageProbeError, match="shared_ingress_changed"):
        asyncio.run(module.run_dedicated_outage_probe(_config(module, tmp_path), runtime=runtime))

    assert runtime.events[-5:] == ["start", "ready", "canary", "control-2", "exit"]


def test_outage_probe_restores_and_canaries_when_the_outage_task_is_cancelled(
    tmp_path: Path,
) -> None:
    module = _module()

    class CancelledRuntime(_Runtime):
        async def submit_outage_job(self) -> str:
            self.events.append("submit")
            raise asyncio.CancelledError

    runtime = CancelledRuntime(module)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module.run_dedicated_outage_probe(_config(module, tmp_path), runtime=runtime))

    assert runtime.events[-5:] == ["start", "ready", "canary", "control-2", "exit"]


def test_outage_probe_fails_closed_when_the_dedicated_plane_cannot_be_restored(
    tmp_path: Path,
) -> None:
    module = _module()

    class BrokenRestoreRuntime(_Runtime):
        async def wait_dedicated_ready(self, identity) -> None:
            assert identity.route_id == "dedicated-tenant-dedicated-01"
            self.events.append("ready")
            raise RuntimeError("unhealthy")

    runtime = BrokenRestoreRuntime(module)

    with pytest.raises(
        module.OpenMAICDedicatedOutageProbeError,
        match="dedicated_plane_restoration_failed",
    ):
        asyncio.run(module.run_dedicated_outage_probe(_config(module, tmp_path), runtime=runtime))

    assert "canary" not in runtime.events
    assert runtime.events[-3:] == ["start", "ready", "exit"]


def test_cancellation_during_inflight_docker_stop_reconciles_before_restore(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)
    stop_started = threading.Event()
    finish_stop = threading.Event()
    stop_finished = threading.Event()
    events: list[str] = []

    class Controller:
        def verify_disposable_plane(self):
            events.append("verify")
            return module.DedicatedPlaneIdentity(
                container_id="f" * 64,
                project="yfeistai-outage-run-dedicated-01",
                route_id="dedicated-tenant-dedicated-01",
                tenant_id="tenant-dedicated-01",
            )

        def stop(self, _identity) -> None:
            events.append("stop-started")
            stop_started.set()
            assert finish_stop.wait(timeout=5)
            events.append("stop-finished")
            stop_finished.set()

        def start(self, _identity) -> None:
            assert stop_finished.is_set()
            events.append("start")

        def wait_ready(self, _identity) -> None:
            events.append("ready")

    class Observer:
        count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def control(self):
            self.count += 1
            events.append(f"control-{self.count}")
            return module.SharedIngressObservation(
                observation_id="shared-ingress-run-dedicated-01",
                request_count=6 + self.count,
                last_canary_id=f"shared-control-canary-{self.count}",
            )

        async def read(self):
            events.append("critical-read")
            return module.SharedIngressObservation(
                observation_id="shared-ingress-run-dedicated-01",
                request_count=7,
                last_canary_id="shared-control-canary-1",
            )

    class CandidateApi:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def prepare_fixture(self) -> None:
            events.append("prepare")

        async def submit_outage_job(self) -> str:
            pytest.fail("submission must not start after cancellation during stop")

        async def wait_for_terminal_job(self, _job_id: str):
            pytest.fail("terminal wait must not start")

        async def read_route_evidence(self, _job_id: str, *, expected_status: str):
            pytest.fail(f"route evidence must not start: {expected_status}")

        async def run_canary(self):
            events.append("canary")
            return module.RouteAttemptEvidence(
                route_id="dedicated-tenant-dedicated-01",
                job_id="job-dedicated-canary-01",
                job_status="succeeded",
                attempt_count=1,
                shared_attempt_count=0,
                dedicated_attempt_count=1,
                selected_attempt_count=1,
                unavailable_attempt_count=0,
                history_complete=True,
            )

    runtime = module.LiveDedicatedOutageRuntime(
        config,
        controller=Controller(),
        observer=Observer(),
        candidate_api=CandidateApi(),
    )

    async def scenario() -> None:
        task = asyncio.create_task(module.run_dedicated_outage_probe(config, runtime=runtime))
        assert await asyncio.to_thread(stop_started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert "start" not in events
        finish_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert events.index("stop-finished") < events.index("start")
    assert events[-4:] == ["start", "ready", "canary", "control-2"]


def test_repeated_cancellation_cannot_interrupt_restoration_sequence(tmp_path: Path) -> None:
    module = _module()

    class RepeatedCancellationRuntime(_Runtime):
        def __init__(self, loaded_module) -> None:
            super().__init__(loaded_module)
            self.restore_started = asyncio.Event()
            self.finish_start = asyncio.Event()

        async def submit_outage_job(self) -> str:
            self.events.append("submit")
            raise asyncio.CancelledError

        async def start_dedicated_plane(self, identity) -> None:
            assert identity.container_id == "f" * 64
            self.events.append("start")
            self.restore_started.set()
            await self.finish_start.wait()

    runtime = RepeatedCancellationRuntime(module)

    async def scenario() -> None:
        task = asyncio.create_task(
            module.run_dedicated_outage_probe(_config(module, tmp_path), runtime=runtime)
        )
        await runtime.restore_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        runtime.finish_start.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert runtime.events[-5:] == ["start", "ready", "canary", "control-2", "exit"]


def test_repeated_cancellation_cannot_interrupt_runtime_context_cleanup(tmp_path: Path) -> None:
    module = _module()
    events: list[str] = []

    class Context:
        def __init__(self, name: str, *, blocking: bool = False) -> None:
            self.name = name
            self.blocking = blocking
            self.started = asyncio.Event()
            self.finish = asyncio.Event()

        async def __aenter__(self):
            events.append(f"{self.name}-enter")
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append(f"{self.name}-exit-start")
            self.started.set()
            if self.blocking:
                await self.finish.wait()
            events.append(f"{self.name}-exit-done")

    observer = Context("observer")
    candidate_api = Context("candidate", blocking=True)
    runtime = module.LiveDedicatedOutageRuntime(
        _config(module, tmp_path),
        controller=object(),
        observer=observer,
        candidate_api=candidate_api,
    )

    async def scenario() -> None:
        await runtime.__aenter__()
        task = asyncio.create_task(runtime.__aexit__(None, None, None))
        await candidate_api.started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        candidate_api.finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert events[-4:] == [
        "candidate-exit-start",
        "candidate-exit-done",
        "observer-exit-start",
        "observer-exit-done",
    ]


def test_repeated_cancellation_cannot_interrupt_fixture_cleanup_and_client_close(
    tmp_path: Path,
) -> None:
    module = _module()
    events: list[str] = []

    class Api:
        def __init__(self) -> None:
            self.cleanup_started = asyncio.Event()
            self.finish_cleanup = asyncio.Event()

        async def cleanup_fixture(self, _state) -> None:
            events.append("cleanup-start")
            self.cleanup_started.set()
            await self.finish_cleanup.wait()
            events.append("cleanup-done")

        async def __aexit__(self, *_args: object) -> None:
            events.append("client-close")

    api = Api()
    candidate = module._LiveCandidateApi.__new__(module._LiveCandidateApi)
    candidate._api = api
    candidate._cleanup = SimpleNamespace(identity_attempted=True)
    candidate._cleanup_completed = False
    candidate._entered = True

    async def scenario() -> None:
        task = asyncio.create_task(candidate.__aexit__(None, None, None))
        await api.cleanup_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        api.finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert events == ["cleanup-start", "cleanup-done", "client-close"]
    assert candidate._cleanup_completed is True
    assert candidate._entered is False


def test_config_is_candidate_run_container_and_observer_bound_without_serializing_token(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    runtime_root = candidate_root / "runtime"
    runtime_root.mkdir()
    observer_body = _observer_attestation_body()
    observer_path = tmp_path / "external-observer-attestation.json"
    observer_path.write_bytes(observer_body)
    docker_path = tmp_path / "docker"
    docker_path.write_bytes(b"docker")
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    environment = {
        "YFEISTAI_CANDIDATE_ROOT": str(candidate_root),
        "YFEISTAI_LIVE_FIXTURE_TOKEN": "admin-token-never-serialize",
        "YFEISTAI_RELEASE_RUN_ID": "run-dedicated-01",
        "YFEISTAI_ENVIRONMENT_ID": "environment-dedicated-01",
        "YFEISTAI_RUNTIME_ATTESTATION_SHA256": RUNTIME_SHA256,
        "YFEISTAI_OPENMAIC_OUTAGE_TIMEOUT_SECONDS": "300",
        "YFEISTAI_DEDICATED_TENANT_ID": "tenant-dedicated-01",
        "YFEISTAI_DEDICATED_OUTAGE_CONTAINER_ID": "f" * 64,
        "YFEISTAI_DEDICATED_OUTAGE_PROJECT": "yfeistai-outage-run-dedicated-01",
        "YFEISTAI_DEDICATED_ROUTE_ID": "dedicated-tenant-dedicated-01",
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ID": OBSERVER_ID,
        "YFEISTAI_SHARED_INGRESS_OBSERVER_URL": OBSERVER_URL,
        "YFEISTAI_SHARED_INGRESS_CONTROL_URL": SHARED_CONTROL_URL,
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_SHA256": hashlib.sha256(
            observer_body
        ).hexdigest(),
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PATH": str(observer_path),
        "YFEISTAI_OUTAGE_DOCKER_CONFIG": str(docker_config),
        "YFEISTAI_OUTAGE_DOCKER_HOST": "unix:///var/run/docker.sock",
        "WEB_BASE_URL": BASE_URL,
    }

    config = module._load_config(
        environment,
        cwd=candidate_root,
        candidate_loader=lambda _root: (
            _candidate(),
            "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:" + "d" * 64,
        ),
        docker_resolver=lambda: docker_path,
    )

    assert config.candidate_root == candidate_root.resolve()
    assert (
        config.output_path
        == (candidate_root / "runtime" / "openmaic-dedicated-outage-attestation.json").resolve()
    )
    assert (
        config.attempt_marker_path
        == (candidate_root / "runtime" / "openmaic-dedicated-outage-attempt.json").resolve()
    )
    assert config.docker_path == docker_path.resolve()
    assert config.release_run == {
        "runId": "run-dedicated-01",
        "environmentId": "environment-dedicated-01",
    }
    assert "admin-token-never-serialize" not in repr(config)

    for name, value in (
        ("YFEISTAI_DEDICATED_OUTAGE_CONTAINER_ID", "short"),
        ("YFEISTAI_DEDICATED_OUTAGE_PROJECT", "other-project"),
        ("YFEISTAI_SHARED_INGRESS_OBSERVER_URL", "http://remote-plaintext.example.test"),
    ):
        changed = dict(environment)
        changed[name] = value
        with pytest.raises(module.OpenMAICDedicatedOutageProbeError):
            module._load_config(
                changed,
                cwd=candidate_root,
                candidate_loader=lambda _root: (
                    _candidate(),
                    "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:" + "d" * 64,
                ),
                docker_resolver=lambda: docker_path,
            )


def test_config_requires_exact_candidate_independent_observer_attestation_before_mutation(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = tmp_path / "candidate"
    runtime_root = candidate_root / "runtime"
    runtime_root.mkdir(parents=True)
    observer_body = _observer_attestation_body()
    observer_path = tmp_path / "external-observer-attestation.json"
    observer_path.write_bytes(observer_body)
    docker_path = tmp_path / "docker"
    docker_path.write_bytes(b"docker")
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    environment = {
        "YFEISTAI_CANDIDATE_ROOT": str(candidate_root),
        "YFEISTAI_LIVE_FIXTURE_TOKEN": "admin-token-never-serialize",
        "YFEISTAI_RELEASE_RUN_ID": "run-dedicated-01",
        "YFEISTAI_ENVIRONMENT_ID": "environment-dedicated-01",
        "YFEISTAI_RUNTIME_ATTESTATION_SHA256": RUNTIME_SHA256,
        "YFEISTAI_OPENMAIC_OUTAGE_TIMEOUT_SECONDS": "300",
        "YFEISTAI_DEDICATED_TENANT_ID": "tenant-dedicated-01",
        "YFEISTAI_DEDICATED_OUTAGE_CONTAINER_ID": "f" * 64,
        "YFEISTAI_DEDICATED_OUTAGE_PROJECT": "yfeistai-outage-run-dedicated-01",
        "YFEISTAI_DEDICATED_ROUTE_ID": "dedicated-tenant-dedicated-01",
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ID": OBSERVER_ID,
        "YFEISTAI_SHARED_INGRESS_OBSERVER_URL": OBSERVER_URL,
        "YFEISTAI_SHARED_INGRESS_CONTROL_URL": SHARED_CONTROL_URL,
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_SHA256": hashlib.sha256(
            observer_body
        ).hexdigest(),
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PATH": str(observer_path),
        "YFEISTAI_OUTAGE_DOCKER_CONFIG": str(docker_config),
        "YFEISTAI_OUTAGE_DOCKER_HOST": "unix:///var/run/docker.sock",
        "WEB_BASE_URL": BASE_URL,
    }

    config = module._load_config(
        environment,
        cwd=candidate_root,
        candidate_loader=lambda _root: (
            _candidate(),
            "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:" + "d" * 64,
        ),
        docker_resolver=lambda: docker_path,
    )

    assert config.observer_attestation_path == observer_path.resolve()
    assert config.observer_attestation_sha256 == hashlib.sha256(observer_body).hexdigest()
    assert config.observer_id == OBSERVER_ID
    assert config.observer_url == OBSERVER_URL
    assert config.shared_ingress_control_url == SHARED_CONTROL_URL
    assert config.observer_origin == "https://observer.example.test"
    assert config.shared_ingress_control_origin == "https://shared-ingress.example.test"

    for name, value in (
        ("YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_SHA256", "a" * 64),
        ("YFEISTAI_SHARED_INGRESS_OBSERVER_ID", "observer-other"),
        ("YFEISTAI_SHARED_INGRESS_OBSERVER_URL", BASE_URL),
    ):
        changed = dict(environment)
        changed[name] = value
        with pytest.raises(module.OpenMAICDedicatedOutageProbeError):
            module._load_config(
                changed,
                cwd=candidate_root,
                candidate_loader=lambda _root: (
                    _candidate(),
                    "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:" + "d" * 64,
                ),
                docker_resolver=lambda: docker_path,
            )


def test_origin_identity_uses_lowercase_idna_host_and_effective_port() -> None:
    module = _module()

    assert module._canonical_origin("https://BÜCHER.example.test:443") == (
        "https",
        "xn--bcher-kva.example.test",
        443,
    )
    assert module._canonical_origin("http://EXAMPLE.test") == (
        "http",
        "example.test",
        80,
    )


def test_config_requires_observer_trust_anchor_outside_candidate_and_rejects_aliases(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = tmp_path / "candidate"
    (candidate_root / "runtime").mkdir(parents=True)
    observer_body = _observer_attestation_body()
    observer_path = tmp_path / "external-observer-attestation.json"
    observer_path.write_bytes(observer_body)
    docker_path = tmp_path / "docker"
    docker_path.write_bytes(b"docker")
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    environment = {
        "YFEISTAI_CANDIDATE_ROOT": str(candidate_root),
        "YFEISTAI_LIVE_FIXTURE_TOKEN": "admin-token-never-serialize",
        "YFEISTAI_RELEASE_RUN_ID": "run-dedicated-01",
        "YFEISTAI_ENVIRONMENT_ID": "environment-dedicated-01",
        "YFEISTAI_RUNTIME_ATTESTATION_SHA256": RUNTIME_SHA256,
        "YFEISTAI_OPENMAIC_OUTAGE_TIMEOUT_SECONDS": "300",
        "YFEISTAI_DEDICATED_TENANT_ID": "tenant-dedicated-01",
        "YFEISTAI_DEDICATED_OUTAGE_CONTAINER_ID": "f" * 64,
        "YFEISTAI_DEDICATED_OUTAGE_PROJECT": "yfeistai-outage-run-dedicated-01",
        "YFEISTAI_DEDICATED_ROUTE_ID": "dedicated-tenant-dedicated-01",
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ID": OBSERVER_ID,
        "YFEISTAI_SHARED_INGRESS_OBSERVER_URL": OBSERVER_URL,
        "YFEISTAI_SHARED_INGRESS_CONTROL_URL": SHARED_CONTROL_URL,
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PATH": str(observer_path),
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_SHA256": hashlib.sha256(
            observer_body
        ).hexdigest(),
        "YFEISTAI_OUTAGE_DOCKER_CONFIG": str(docker_config),
        "YFEISTAI_OUTAGE_DOCKER_HOST": "unix:///var/run/docker.sock",
        "WEB_BASE_URL": BASE_URL,
    }

    config = module._load_config(
        environment,
        cwd=candidate_root,
        candidate_loader=lambda _root: (
            _candidate(),
            "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:" + "d" * 64,
        ),
        docker_resolver=lambda: docker_path,
    )

    assert config.observer_attestation_path == observer_path.resolve()
    assert candidate_root.resolve() not in config.observer_attestation_path.parents
    changed = dict(environment)
    changed["YFEISTAI_SHARED_INGRESS_OBSERVER_URL"] = "https://CANDIDATE.example.test:443"
    with pytest.raises(
        module.OpenMAICDedicatedOutageProbeError,
        match="shared_ingress_observer_invalid",
    ):
        module._load_config(
            changed,
            cwd=candidate_root,
            candidate_loader=lambda _root: (
                _candidate(),
                "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:" + "d" * 64,
            ),
            docker_resolver=lambda: docker_path,
        )


def _container_document(*, running: bool = True, run_id: str = "run-dedicated-01"):
    return {
        "Id": "f" * 64,
        "Config": {
            "Image": "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:" + "d" * 64,
            "Labels": {
                "com.docker.compose.project": "yfeistai-outage-run-dedicated-01",
                "com.docker.compose.service": "openmaic",
                "com.yfeistai.acceptance.disposable": "true",
                "com.yfeistai.acceptance.environment-id": "environment-dedicated-01",
                "com.yfeistai.acceptance.purpose": "openmaic-dedicated-outage",
                "com.yfeistai.acceptance.route-id": "dedicated-tenant-dedicated-01",
                "com.yfeistai.acceptance.run-id": run_id,
                "com.yfeistai.acceptance.tenant-id": "tenant-dedicated-01",
            },
        },
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "NetworkSettings": {"Ports": {}},
        "State": {
            "Running": running,
            "Health": {"Status": "healthy" if running else "none"},
        },
    }


def test_docker_controller_targets_only_the_exact_run_owned_disposable_container(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)
    calls: list[list[str]] = []
    running = True

    def runner(arguments, *, cwd, env, timeout):
        nonlocal running
        command = [str(value) for value in arguments]
        calls.append(command)
        assert cwd == tmp_path
        assert timeout <= config.timeout_seconds
        assert "COMPOSE_FILE" not in env
        preflight = _docker_preflight_response(config, command)
        if preflight is not None:
            return preflight
        payload = command[5:]
        if payload[:2] == ["container", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([_container_document(running=running)]).encode(),
                stderr=b"",
            )
        if payload[:1] == ["stop"]:
            running = False
            return SimpleNamespace(returncode=0, stdout=("f" * 64).encode(), stderr=b"")
        if payload[:1] == ["start"]:
            running = True
            return SimpleNamespace(returncode=0, stdout=("f" * 64).encode(), stderr=b"")
        pytest.fail(f"unexpected Docker command: {arguments}")

    controller = module.DockerDedicatedPlaneController(config, runner=runner)
    identity = controller.verify_disposable_plane()
    controller.stop(identity)
    controller.start(identity)
    controller.wait_ready(identity)

    assert identity == module.DedicatedPlaneIdentity(
        container_id="f" * 64,
        project="yfeistai-outage-run-dedicated-01",
        route_id="dedicated-tenant-dedicated-01",
        tenant_id="tenant-dedicated-01",
    )
    assert calls == [
        _docker_command(config, "info", "--format", "{{json .}}"),
        _docker_command(config, "ps", "-a", "--no-trunc", "--format", "{{json .}}"),
        _docker_command(config, "ps", "-a", "--no-trunc", "--format", "{{json .}}"),
        _docker_command(config, "container", "inspect", "f" * 64),
        _docker_command(config, "stop", "--time", "30", "f" * 64),
        _docker_command(config, "container", "inspect", "f" * 64),
        _docker_command(config, "start", "f" * 64),
        _docker_command(config, "container", "inspect", "f" * 64),
        _docker_command(config, "container", "inspect", "f" * 64),
    ]


def test_docker_controller_uses_isolated_host_and_replays_stable_daemon_inventory(
    tmp_path: Path,
) -> None:
    module = _module()
    base_config = _config(module, tmp_path)
    docker_config = tmp_path / "isolated-docker-config"
    docker_config.mkdir()
    docker_host = "unix:///var/run/docker.sock"
    host_digest = hashlib.sha256(
        b'{"scheme":"unix","socketPath":"/var/run/docker.sock"}\n'
    ).hexdigest()
    config = SimpleNamespace(
        **{
            name: getattr(base_config, name)
            for name in base_config.__slots__
            if name not in {"docker_config_dir", "docker_host", "docker_host_identity_sha256"}
        },
        docker_config_dir=docker_config,
        docker_host=docker_host,
        docker_host_identity_sha256=host_digest,
    )
    calls: list[tuple[list[str], dict[str, str]]] = []
    running = True
    daemon = _docker_daemon_document()
    inventory = {
        "ID": "f" * 64,
        "Names": "openmaic",
        "Image": config.openmaic_image_reference,
        "State": "running",
        "Status": "Up 1 hour (healthy)",
    }

    def runner(arguments, *, cwd, env, timeout):
        nonlocal running
        command = [str(value) for value in arguments]
        calls.append((command, dict(env)))
        assert cwd == tmp_path
        assert timeout <= config.timeout_seconds
        payload = command[5:]
        if payload[:1] == ["info"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(daemon).encode(), stderr=b"")
        if payload[:3] == ["ps", "-a", "--no-trunc"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(json.dumps(inventory) + "\n").encode(),
                stderr=b"",
            )
        if payload[:2] == ["container", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([_container_document(running=running)]).encode(),
                stderr=b"",
            )
        if payload[:1] == ["stop"]:
            running = False
            return SimpleNamespace(returncode=0, stdout=("f" * 64).encode(), stderr=b"")
        if payload[:1] == ["start"]:
            running = True
            return SimpleNamespace(returncode=0, stdout=("f" * 64).encode(), stderr=b"")
        pytest.fail(f"unexpected Docker command: {payload}")

    controller = module.DockerDedicatedPlaneController(
        config,
        runner=runner,
        environment={
            "HOME": "C:/should-not-leak",
            "USERPROFILE": "C:/should-not-leak",
            "DOCKER_CONTEXT": "foreign",
            "DOCKER_HOST": "tcp://foreign:2375",
            "PATH": "C:/trusted-bin",
        },
    )
    identity = controller.verify_disposable_plane()
    controller.stop(identity)
    controller.start(identity)
    controller.wait_ready(identity)
    boundary = controller.boundary_attestation()

    expected_prefix = [
        str(config.docker_path),
        "--config",
        str(docker_config.resolve()),
        "--host",
        docker_host,
    ]
    assert all(command[:5] == expected_prefix for command, _env in calls)
    assert all(
        not {"HOME", "USERPROFILE", "DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_CONFIG"} & set(env)
        for _command, env in calls
    )
    assert boundary["dockerHostIdentitySha256"] == host_digest
    assert boundary["daemonIdentityBeforeSha256"] == boundary["daemonIdentityAfterSha256"]
    assert boundary["inventoryBeforeSha256"] == boundary["inventoryAfterSha256"]
    assert list(docker_config.iterdir()) == []


def test_docker_controller_rejects_a_foreign_run_before_any_mutating_command(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        command = [str(value) for value in arguments]
        calls.append(command)
        preflight = _docker_preflight_response(config, command)
        if preflight is not None:
            return preflight
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([_container_document(run_id="other-run")]).encode(),
            stderr=b"",
        )

    controller = module.DockerDedicatedPlaneController(config, runner=runner)
    with pytest.raises(
        module.OpenMAICDedicatedOutageProbeError,
        match="dedicated_plane_identity_invalid",
    ):
        controller.verify_disposable_plane()

    assert calls == [
        _docker_command(config, "info", "--format", "{{json .}}"),
        _docker_command(config, "ps", "-a", "--no-trunc", "--format", "{{json .}}"),
        _docker_command(config, "ps", "-a", "--no-trunc", "--format", "{{json .}}"),
        _docker_command(config, "container", "inspect", "f" * 64),
    ]


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("Id",), "e" * 64),
        (("Config", "Image"), "ghcr.io/xinlingzhifei/openmaic@sha256:" + "e" * 64),
        (("Config", "Labels", "com.docker.compose.project"), "foreign-project"),
        (("Config", "Labels", "com.docker.compose.service"), "foreign-service"),
        (("Config", "Labels", "com.yfeistai.acceptance.disposable"), "false"),
        (
            ("Config", "Labels", "com.yfeistai.acceptance.environment-id"),
            "foreign-environment",
        ),
        (("Config", "Labels", "com.yfeistai.acceptance.run-id"), "foreign-run"),
        (("Config", "Labels", "com.yfeistai.acceptance.tenant-id"), "foreign-tenant"),
        (("Config", "Labels", "com.yfeistai.acceptance.route-id"), "foreign-route"),
        (("Config", "Labels", "com.yfeistai.acceptance.purpose"), "foreign-purpose"),
        (("HostConfig", "RestartPolicy", "Name"), "unless-stopped"),
        (("NetworkSettings", "Ports"), {"3000/tcp": [{"HostPort": "3000"}]}),
        (("State", "Health", "Status"), "unhealthy"),
    ),
)
def test_docker_controller_rejects_every_exact_container_fence_before_mutation(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    module = _module()
    config = _config(module, tmp_path)
    document = copy.deepcopy(_container_document())
    target = document
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value
    calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        command = [str(item) for item in arguments]
        calls.append(command)
        preflight = _docker_preflight_response(config, command)
        if preflight is not None:
            return preflight
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([document]).encode(),
            stderr=b"",
        )

    controller = module.DockerDedicatedPlaneController(config, runner=runner)
    with pytest.raises(module.OpenMAICDedicatedOutageProbeError):
        controller.verify_disposable_plane()

    assert calls == [
        _docker_command(config, "info", "--format", "{{json .}}"),
        _docker_command(config, "ps", "-a", "--no-trunc", "--format", "{{json .}}"),
        _docker_command(config, "ps", "-a", "--no-trunc", "--format", "{{json .}}"),
        _docker_command(config, "container", "inspect", "f" * 64),
    ]


def test_shared_ingress_observer_requires_an_independent_control_increment(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)
    count = 6
    last_canary_id: str | None = None
    observer_seen: list[tuple[str, str]] = []
    control_seen: list[dict[str, object]] = []

    def observer_handler(request: httpx.Request) -> httpx.Response:
        observer_seen.append((request.method, request.url.path))
        assert request.headers.get("authorization") is None
        path = "/v1/observations/run-dedicated-01"
        if request.method == "GET" and request.url.path == path:
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 1,
                    "runId": "run-dedicated-01",
                    "environmentId": "environment-dedicated-01",
                    "observationId": "shared-ingress-run-dedicated-01",
                    "requestCount": count,
                    "lastCanaryId": last_canary_id,
                },
            )
        return httpx.Response(404)

    def control_handler(request: httpx.Request) -> httpx.Response:
        nonlocal count, last_canary_id
        assert request.method == "POST"
        assert str(request.url) == SHARED_CONTROL_URL
        payload = json.loads(request.content)
        assert payload == {
            "schemaVersion": 1,
            "runId": "run-dedicated-01",
            "environmentId": "environment-dedicated-01",
            "canaryId": "shared-control-canary-01",
            "kind": "openmaic-shared-ingress-control",
        }
        control_seen.append(payload)
        count += 1
        last_canary_id = str(payload["canaryId"])
        return httpx.Response(
            202,
            json={"accepted": True, "canaryId": last_canary_id},
        )

    async def scenario():
        async with module.SharedIngressObserver(
            config,
            observer_transport=httpx.MockTransport(observer_handler),
            control_transport=httpx.MockTransport(control_handler),
            canary_id_factory=lambda: "shared-control-canary-01",
        ) as observer:
            controlled = await observer.control()
            critical = await observer.read()
            return controlled, critical

    controlled, critical = asyncio.run(scenario())

    assert controlled == module.SharedIngressObservation(
        observation_id="shared-ingress-run-dedicated-01",
        request_count=7,
        last_canary_id="shared-control-canary-01",
    )
    assert critical == controlled
    assert observer_seen == [
        ("GET", "/v1/observations/run-dedicated-01"),
        ("GET", "/v1/observations/run-dedicated-01"),
        ("GET", "/v1/observations/run-dedicated-01"),
    ]
    assert control_seen == [
        {
            "schemaVersion": 1,
            "runId": "run-dedicated-01",
            "environmentId": "environment-dedicated-01",
            "canaryId": "shared-control-canary-01",
            "kind": "openmaic-shared-ingress-control",
        }
    ]


def test_shared_ingress_observer_fails_closed_when_control_is_not_observed(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)

    def observer_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "runId": "run-dedicated-01",
                "environmentId": "environment-dedicated-01",
                "observationId": "shared-ingress-run-dedicated-01",
                "requestCount": 6,
                "lastCanaryId": "an-earlier-canary",
            },
        )

    def control_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            202,
            json={"accepted": True, "canaryId": payload["canaryId"]},
        )

    async def no_sleep(_seconds: float) -> None:
        return None

    async def scenario() -> None:
        async with module.SharedIngressObserver(
            config,
            observer_transport=httpx.MockTransport(observer_handler),
            control_transport=httpx.MockTransport(control_handler),
            sleep=no_sleep,
            monotonic=iter((0.0, 21.0)).__next__,
        ) as observer:
            await observer.control()

    with pytest.raises(
        module.OpenMAICDedicatedOutageProbeError,
        match="shared_ingress_control_invalid",
    ):
        asyncio.run(scenario())


def test_attestation_publication_is_canonical_atomic_and_never_overwrites_drift(
    tmp_path: Path,
) -> None:
    module = _module()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    target = runtime_root / "openmaic-dedicated-outage-attestation.json"
    body = b'{"candidate":"bound"}\n'

    module._atomic_publish(target, body)
    module._atomic_publish(target, body)

    assert target.read_bytes() == body
    assert list(runtime_root.iterdir()) == [target]

    with pytest.raises(
        module.OpenMAICDedicatedOutageProbeError,
        match="attestation_already_exists",
    ):
        module._atomic_publish(target, b'{"candidate":"drift"}\n')

    assert target.read_bytes() == body

    race_target = runtime_root / "race-attestation.json"
    start = threading.Barrier(2)

    def publish(body_to_publish: bytes) -> str:
        start.wait(timeout=2)
        try:
            module._atomic_publish(race_target, body_to_publish)
        except module.OpenMAICDedicatedOutageProbeError as exc:
            return str(exc)
        return "published"

    competing = (b'{"candidate":"first"}\n', b'{"candidate":"second"}\n')
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, competing))

    assert sorted(outcomes) == ["attestation_already_exists", "published"]
    assert race_target.read_bytes() in competing
    assert not list(runtime_root.glob(".*.staged"))


def test_attestation_publication_cleans_owned_staging_when_no_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    target = runtime_root / "openmaic-dedicated-outage-attestation.json"

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated no-replace failure")

    monkeypatch.setattr(module._RuntimeDirectoryGuard, "replace", fail_replace)
    with pytest.raises(
        module.OpenMAICDedicatedOutageProbeError,
        match="attestation_publish_failed",
    ):
        module._atomic_publish(target, b'{"candidate":"bound"}\n')

    assert not target.exists()
    assert list(runtime_root.iterdir()) == []


def test_portable_posix_publication_fails_closed_before_competing_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    runtime_module = sys.modules[module._RuntimeDirectoryGuard.__module__]
    first_body = b'{"candidate":"first"}\n'
    second_body = b'{"candidate":"second"}\n'
    target_body: bytes | None = None
    rename_calls: list[tuple[str, str, int, int]] = []

    def stat_then_first_publisher_wins(
        path: str,
        *,
        dir_fd: int,
        follow_symlinks: bool,
    ) -> SimpleNamespace:
        nonlocal target_body
        assert dir_fd == 41
        assert follow_symlinks is False
        if path == ".outage-attestation.second.staged":
            return SimpleNamespace(st_mode=0o100600, st_dev=7, st_ino=11)
        assert path == "outage-attestation.json"
        assert target_body is None
        target_body = first_body
        raise FileNotFoundError

    def unavailable_atomic_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal target_body
        assert source == ".outage-attestation.second.staged"
        assert target == "outage-attestation.json"
        assert src_dir_fd == dst_dir_fd == 41
        assert follow_symlinks is False
        assert target_body is None
        target_body = first_body
        raise NotImplementedError("atomic hard links are unavailable")

    def overwrite_capable_rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal target_body
        rename_calls.append((source, target, src_dir_fd, dst_dir_fd))
        assert target_body == first_body
        target_body = second_body

    monkeypatch.setattr(runtime_module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        runtime_module,
        "os",
        SimpleNamespace(
            stat=stat_then_first_publisher_wins,
            link=unavailable_atomic_link,
            rename=overwrite_capable_rename,
        ),
    )

    with pytest.raises(OSError, match="atomic|no-replace|safe|unsupported|required"):
        runtime_module._rename_posix_no_replace(
            41,
            ".outage-attestation.second.staged",
            "outage-attestation.json",
        )

    assert target_body in (None, first_body)
    assert rename_calls == []


def test_attestation_publication_rejects_runtime_parent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    displaced_runtime = tmp_path / "displaced-runtime"
    target = runtime_root / "openmaic-dedicated-outage-attestation.json"
    real_replace = module._RuntimeDirectoryGuard.replace
    replacement_attempted = False
    replacement_blocked = False
    swapped = False

    def replace_after_parent_swap(guard, source: str, destination: str) -> None:
        nonlocal replacement_attempted, replacement_blocked, swapped
        replacement_attempted = True
        try:
            runtime_root.rename(displaced_runtime)
        except PermissionError:
            replacement_blocked = True
            raise
        try:
            runtime_root.symlink_to(displaced_runtime, target_is_directory=True)
        except OSError:
            displaced_runtime.rename(runtime_root)
            pytest.skip("directory symlinks are unavailable on this test host")
        swapped = True
        real_replace(guard, source, destination)

    monkeypatch.setattr(
        module._RuntimeDirectoryGuard,
        "replace",
        replace_after_parent_swap,
    )

    with pytest.raises(
        module.OpenMAICDedicatedOutageProbeError,
        match="attestation_publish_failed",
    ) as published_error:
        module._atomic_publish(target, b'{"candidate":"bound"}\n')

    assert replacement_attempted, str(published_error.value)
    assert replacement_blocked or swapped
    asserted_runtime = displaced_runtime if swapped else runtime_root
    assert not (asserted_runtime / target.name).exists()
    assert not list(asserted_runtime.glob(".*.staged"))


def test_run_attempt_marker_is_durable_no_replace_and_blocks_retry_before_runtime_entry(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)
    runtime = _Runtime(module)

    asyncio.run(
        module.run_dedicated_outage_probe(
            config,
            runtime=runtime,
            observed_at=lambda: "2026-08-30T00:00:01Z",
        )
    )
    marker = config.attempt_marker_path
    marker_body = marker.read_bytes()
    marker_document = json.loads(marker_body)
    assert marker_document["candidate"] == _candidate()
    assert marker_document["releaseRun"] == config.release_run
    assert marker_document["fixturePlan"] == {
        "tenantId": "tenant-dedicated-01",
        "routeId": "dedicated-tenant-dedicated-01",
        "cleanupBoundary": "identity-membership-enrollment-only",
        "retainedResourceTypes": [
            "course",
            "class",
            "generationQuotaGrant",
            "classroomAsset",
            "generationJob",
        ],
    }
    assert module.canonical_openmaic_dedicated_outage_attestation(marker_document) == marker_body

    retry_runtime = _Runtime(module)
    with pytest.raises(
        module.OpenMAICDedicatedOutageProbeError,
        match="outage_attempt_already_exists",
    ):
        asyncio.run(module.run_dedicated_outage_probe(config, runtime=retry_runtime))

    assert retry_runtime.events == []
    assert marker.read_bytes() == marker_body


def test_outage_compose_overlay_marks_only_the_run_owned_openmaic_service_disposable() -> None:
    overlay_path = PROJECT_ROOT / "deploy" / "docker-compose.openmaic-dedicated-outage.yml"
    overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))

    assert overlay["name"] == (
        "${YFEISTAI_DEDICATED_OUTAGE_PROJECT:?set the run-scoped outage project}"
    )
    assert set(overlay["services"]) == {"openmaic"}
    service = overlay["services"]["openmaic"]
    assert service["restart"] == "no"
    assert service["labels"] == {
        "com.yfeistai.acceptance.disposable": "true",
        "com.yfeistai.acceptance.environment-id": (
            "${YFEISTAI_ENVIRONMENT_ID:?set environment id}"
        ),
        "com.yfeistai.acceptance.purpose": "openmaic-dedicated-outage",
        "com.yfeistai.acceptance.route-id": (
            "${YFEISTAI_DEDICATED_ROUTE_ID:?set dedicated route id}"
        ),
        "com.yfeistai.acceptance.run-id": ("${YFEISTAI_RELEASE_RUN_ID:?set release run id}"),
        "com.yfeistai.acceptance.tenant-id": (
            "${YFEISTAI_DEDICATED_TENANT_ID:?set dedicated tenant id}"
        ),
    }
    assert "ports" not in service
    assert "volumes" not in service


def test_offline_merged_outage_compose_preserves_plane_and_adds_only_safety_fences() -> None:
    base = yaml.safe_load((PROJECT_ROOT / "docker-compose.data-plane.yml").read_text("utf-8"))
    overlay = yaml.safe_load(
        (PROJECT_ROOT / "deploy" / "docker-compose.openmaic-dedicated-outage.yml").read_text(
            "utf-8"
        )
    )

    def merge(left: object, right: object) -> object:
        if isinstance(left, dict) and isinstance(right, dict):
            merged = copy.deepcopy(left)
            for key, value in right.items():
                merged[key] = merge(merged[key], value) if key in merged else copy.deepcopy(value)
            return merged
        return copy.deepcopy(right)

    merged = merge(base, overlay)
    assert isinstance(merged, dict)
    services = merged["services"]
    assert isinstance(services, dict)
    openmaic = services["openmaic"]
    original_openmaic = base["services"]["openmaic"]
    assert openmaic["image"] == original_openmaic["image"]
    assert openmaic["healthcheck"] == original_openmaic["healthcheck"]
    assert openmaic["networks"] == original_openmaic["networks"]
    assert openmaic["secrets"] == original_openmaic["secrets"]
    assert openmaic["volumes"] == original_openmaic["volumes"]
    assert openmaic["restart"] == "no"
    assert "ports" not in openmaic
    assert openmaic["labels"] == overlay["services"]["openmaic"]["labels"]
    assert services["openmaic-render"] == base["services"]["openmaic-render"]


def test_live_candidate_api_uses_formal_fixture_endpoints_and_reads_persisted_route_evidence(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(module, tmp_path)
    tenant_id = config.dedicated_tenant_id
    teacher_user_id = "teacher-user-outage-01"
    confirmed = False
    created_username = ""
    course_id = ""
    class_id = ""
    cleanup: list[str] = []

    def response_binding(job_id: str, *, succeeded: bool) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "tenantId": tenant_id,
            "jobId": job_id,
            "jobKind": "generation",
            "phase": "content" if succeeded else "outline",
            "status": "succeeded" if succeeded else "failed",
            "progressPercent": 100 if succeeded else 0,
            "classroomVersionId": "version-canary-01" if succeeded else None,
            "dataPlaneMode": "dedicated",
            "dataPlaneRouteId": "dedicated-tenant-dedicated-01",
            "routeTenantId": tenant_id,
            "routeOwnerKey": tenant_id,
            "providerProfileId": "provider-tenant-dedicated-01",
            "providerScope": "dedicated",
            "providerTenantId": tenant_id,
            "providerOwnerKey": tenant_id,
            "workerPoolRef": "generation-tenant-dedicated-01",
            "queueRef": "openmaic.tenant-dedicated-01",
            "attemptCount": 2 if succeeded else 1,
            "sharedRouteAttemptCount": 0,
            "dedicatedRouteAttemptCount": 2 if succeeded else 1,
            "selectedRouteAttemptCount": 2 if succeeded else 1,
            "unavailableRouteAttemptCount": 0,
            "routeAttemptHistoryComplete": True,
        }

    def request_json(request: httpx.Request) -> dict[str, object]:
        value = json.loads(request.content)
        assert isinstance(value, dict)
        return value

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal class_id, confirmed, course_id, created_username
        path = request.url.path
        is_admin = request.headers.get("authorization") == "Bearer admin-token-never-serialize"
        if request.method == "PUT" and path == "/api/v1/tenants/active":
            assert request_json(request) == {"tenant_id": tenant_id}
            return httpx.Response(
                200,
                json={"active_tenant_id": tenant_id},
                headers={"Set-Cookie": f"dt_tenant={tenant_id}; Path=/; SameSite=Lax"},
            )
        if request.method == "GET" and path == "/api/v1/auth/users":
            assert is_admin
            return httpx.Response(200, json=[])
        if request.method == "POST" and path == "/api/v1/auth/users":
            assert is_admin
            payload = request_json(request)
            created_username = str(payload["username"])
            assert payload["password"]
            return httpx.Response(
                201,
                json={
                    "ok": True,
                    "user_id": teacher_user_id,
                    "username": created_username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if request.method == "POST" and path == f"/api/v1/tenants/{tenant_id}/members":
            assert is_admin
            return httpx.Response(
                200,
                json={
                    "tenant_id": tenant_id,
                    "user_id": teacher_user_id,
                    "roles": ["teacher"],
                    "grants": [
                        {
                            "role": "teacher",
                            "scope_type": "tenant",
                            "scope_id": tenant_id,
                        }
                    ],
                },
            )
        if request.method == "POST" and path == "/api/v1/auth/login":
            payload = request_json(request)
            assert payload["username"] == created_username
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "user_id": teacher_user_id,
                    "username": created_username,
                    "role": "user",
                    "is_admin": False,
                },
                headers={"Set-Cookie": "dt_token=teacher-session; Path=/; HttpOnly"},
            )
        if request.method == "POST" and path == "/api/v1/teaching/courses":
            assert is_admin
            payload = request_json(request)
            course_id = str(payload["id"])
            return httpx.Response(201, json={"id": course_id, "status": "active"})
        if (
            request.method == "POST"
            and course_id
            and path == f"/api/v1/teaching/courses/{course_id}/classes"
        ):
            payload = request_json(request)
            class_id = str(payload["id"])
            return httpx.Response(
                201,
                json={"id": class_id, "courseId": course_id, "status": "active"},
            )
        if (
            request.method == "POST"
            and class_id
            and path == f"/api/v1/teaching/classes/{class_id}/enrollments"
        ):
            return httpx.Response(
                201,
                json={
                    "classId": class_id,
                    "userId": teacher_user_id,
                    "status": "active",
                },
            )
        if request.method == "POST" and path == "/api/v1/teaching/generation-quota-grants":
            assert is_admin
            return httpx.Response(
                200,
                json={"tenantId": tenant_id, "units": 40, "balance": 40},
            )
        if request.method == "POST" and path == "/api/v1/classrooms":
            payload = request_json(request)
            purpose = "canary" if "restore-canary" in str(payload["title"]) else "outage"
            return httpx.Response(
                202,
                json={
                    "assetId": f"asset-{purpose}-01",
                    "jobId": f"job-{purpose}-01",
                    "ownerId": teacher_user_id,
                    "status": "created",
                    "lifecycleState": "generating_outline",
                    "outline": None,
                },
            )
        if request.method == "GET" and path == "/api/v1/classroom-jobs/job-outage-01":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-outage-01",
                    "job_kind": "generation",
                    "phase": "outline",
                    "status": "failed",
                    "progress_percent": 0,
                    "error_code": "dedicated_data_plane_unavailable",
                },
            )
        if request.method == "GET" and path == "/api/v1/classroom-jobs/job-canary-01":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-canary-01",
                    "job_kind": "generation",
                    "phase": "content" if confirmed else "outline",
                    "status": "succeeded" if confirmed else "awaiting_confirmation",
                    "progress_percent": 100,
                    "outline": None if confirmed else {"sections": []},
                },
            )
        if request.method == "GET" and path == "/api/v1/classrooms/asset-canary-01":
            return httpx.Response(
                200,
                json={
                    "assetId": "asset-canary-01",
                    "jobId": "job-canary-01",
                    "ownerId": teacher_user_id,
                    "status": "succeeded" if confirmed else "awaiting_confirmation",
                    "lifecycleState": "editing" if confirmed else "awaiting_outline",
                    "outline": None if confirmed else {"sections": []},
                    "document": {"schemaVersion": "1.0"} if confirmed else None,
                    "classroomVersionId": "version-canary-01" if confirmed else None,
                },
            )
        if (
            request.method == "POST"
            and path == "/api/v1/classrooms/asset-canary-01/confirm-outline"
        ):
            confirmed = True
            return httpx.Response(
                202,
                json={
                    "assetId": "asset-canary-01",
                    "jobId": "job-canary-01",
                    "ownerId": teacher_user_id,
                    "lifecycleState": "generating_content",
                },
            )
        if request.method == "GET" and path.endswith("/job-outage-01/binding"):
            assert is_admin
            return httpx.Response(200, json=response_binding("job-outage-01", succeeded=False))
        if request.method == "GET" and path.endswith("/job-canary-01/binding"):
            assert is_admin
            return httpx.Response(200, json=response_binding("job-canary-01", succeeded=True))
        if request.method == "DELETE" and "/enrollments/" in path:
            cleanup.append("enrollment")
            return httpx.Response(204)
        if request.method == "DELETE" and path.endswith(f"/members/{teacher_user_id}"):
            cleanup.append("membership")
            return httpx.Response(204)
        if request.method == "DELETE" and path.startswith("/api/v1/auth/users/"):
            cleanup.append("identity")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(500, json={"unexpected": f"{request.method} {path}"})

    async def scenario():
        async with module._LiveCandidateApi(
            config,
            transport=httpx.MockTransport(handler),
        ) as api:
            await api.prepare_fixture()
            outage_job_id = await api.submit_outage_job()
            terminal = await api.wait_for_terminal_job(outage_job_id)
            outage = await api.read_route_evidence(outage_job_id, expected_status="failed")
            canary = await api.run_canary()
        inventory = api.fixture_audit_inventory()
        return terminal, outage, canary, inventory

    terminal, outage, canary, inventory = asyncio.run(scenario())

    assert terminal == module.TerminalJob(
        job_id="job-outage-01",
        status="failed",
        error_code="dedicated_data_plane_unavailable",
    )
    assert outage.selected_attempt_count == 1
    assert outage.shared_attempt_count == 0
    assert canary.job_status == "succeeded"
    assert canary.selected_attempt_count == 2
    assert canary.shared_attempt_count == 0
    assert cleanup == ["enrollment", "membership", "identity"]
    assert inventory.reversible_resources_deleted == (
        "classEnrollment",
        "tenantMembership",
        "teacherIdentity",
    )
    assert [(item.resource_type, item.resource_id) for item in inventory.retained_resources] == [
        ("course", course_id),
        ("class", class_id),
        ("generationQuotaGrant", tenant_id),
        ("classroomAsset", "asset-outage-01"),
        ("generationJob", "job-outage-01"),
        ("classroomAsset", "asset-canary-01"),
        ("generationJob", "job-canary-01"),
    ]
