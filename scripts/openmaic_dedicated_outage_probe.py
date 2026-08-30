"""Prove that one isolated dedicated tenant never falls back to the shared plane."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Protocol, Self, TypeAlias
from urllib.parse import quote, urlsplit
import uuid

import httpx
from pydantic import SecretStr

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from classroom_runtime_attestation import (  # noqa: E402
    _RuntimeDirectoryGuard,
    resolve_fixed_docker,
)
from openmaic_smoke_contract import (  # noqa: E402
    OPENMAIC_DEDICATED_OUTAGE_PRODUCER,
    OPENMAIC_SMOKE_SCHEMA_VERSION,
    canonical_openmaic_dedicated_outage_attempt_marker,
    canonical_openmaic_dedicated_outage_attestation,
    parse_openmaic_shared_ingress_observer_attestation,
)
from openmaic_smoke_probe import (  # noqa: E402
    _BINDING_RESPONSE_FIELDS,
    OpenMAICSmokeProbeError,
    _fixture_material,
    _FixtureCleanupState,
    _local_user_id,
    _OpenMAICSmokeApi,
    _wait_for_content_job,
    _wait_for_generated_classroom,
    _wait_for_outline_classroom,
    _wait_for_outline_job,
)
from openmaic_smoke_probe import (
    ProbeConfig as SmokeProbeConfig,
)
from openmaic_smoke_probe import (
    _public_id as _smoke_public_id,
)
from render_platform_compose import validate_image_lock_bindings  # noqa: E402

_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_PROJECT = re.compile(r"^yfeistai-outage-[a-z0-9][a-z0-9_-]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OpenMAICDedicatedOutageProbeError(RuntimeError):
    """Stable, secret-free failure raised by the outage producer."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise OpenMAICDedicatedOutageProbeError("arguments_invalid")


@dataclass(frozen=True, slots=True)
class OutageProbeConfig:
    admin_token: SecretStr
    attempt_marker_path: Path
    base_url: str
    candidate: Mapping[str, object]
    candidate_root: Path
    docker_path: Path
    dedicated_container_id: str
    dedicated_project: str
    dedicated_route_id: str
    dedicated_tenant_id: str
    openmaic_image_reference: str
    release_run: Mapping[str, str]
    observer_attestation_path: Path
    observer_attestation_sha256: str
    observer_id: str
    observer_url: str
    observer_origin: str
    shared_ingress_control_url: str
    shared_ingress_control_origin: str
    output_path: Path
    runtime_attestation_sha256: str
    timeout_seconds: int
    docker_config_dir: Path | None = None
    docker_host: str = ""
    docker_host_identity_sha256: str = ""


@dataclass(frozen=True, slots=True)
class DedicatedPlaneIdentity:
    container_id: str
    project: str
    route_id: str
    tenant_id: str


@dataclass(frozen=True, slots=True)
class SharedIngressObservation:
    observation_id: str
    request_count: int
    last_canary_id: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalJob:
    job_id: str
    status: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class RouteAttemptEvidence:
    route_id: str
    job_id: str
    job_status: str
    attempt_count: int
    shared_attempt_count: int
    dedicated_attempt_count: int
    selected_attempt_count: int
    unavailable_attempt_count: int
    history_complete: bool


@dataclass(frozen=True, slots=True)
class RetainedAuditResource:
    resource_type: str
    resource_id: str


@dataclass(frozen=True, slots=True)
class FixtureAuditInventory:
    reversible_resources_deleted: tuple[str, ...]
    retained_resources: tuple[RetainedAuditResource, ...]


class DedicatedOutageRuntime(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def verify_disposable_plane(self) -> DedicatedPlaneIdentity: ...

    async def prepare_outage_fixture(self) -> None: ...

    async def control_shared_ingress(self) -> SharedIngressObservation: ...

    async def read_shared_ingress(self) -> SharedIngressObservation: ...

    async def stop_dedicated_plane(self, identity: DedicatedPlaneIdentity) -> None: ...

    async def submit_outage_job(self) -> str: ...

    async def wait_for_terminal_job(self, job_id: str) -> TerminalJob: ...

    async def read_job_route_evidence(self, job_id: str) -> RouteAttemptEvidence: ...

    async def start_dedicated_plane(self, identity: DedicatedPlaneIdentity) -> None: ...

    async def wait_dedicated_ready(self, identity: DedicatedPlaneIdentity) -> None: ...

    async def run_restoration_canary(self) -> RouteAttemptEvidence: ...

    def fixture_audit_inventory(self) -> FixtureAuditInventory: ...

    def docker_boundary_attestation(self) -> Mapping[str, str]: ...


async def _await_task_deferring_cancellation(
    task: asyncio.Task[Any],
) -> tuple[Any, bool]:
    """Wait for an owned task to finish while retaining caller cancellation."""

    cancellation_seen = False
    current = asyncio.current_task()
    while True:
        try:
            return await asyncio.shield(task), cancellation_seen
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_seen = True
            if current is not None:
                current.uncancel()


def _public_id(value: object, error: str) -> str:
    if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
        raise OpenMAICDedicatedOutageProbeError(error)
    return value


def _validate_identity(
    config: OutageProbeConfig,
    identity: DedicatedPlaneIdentity,
) -> None:
    if (
        _CONTAINER_ID.fullmatch(identity.container_id) is None
        or identity.container_id != config.dedicated_container_id
        or identity.project != config.dedicated_project
        or identity.route_id != config.dedicated_route_id
        or identity.tenant_id != config.dedicated_tenant_id
        or not _PUBLIC_ID.fullmatch(identity.route_id)
        or identity.route_id == "shared-primary"
    ):
        raise OpenMAICDedicatedOutageProbeError("dedicated_plane_identity_invalid")


def _validate_observation(observation: SharedIngressObservation) -> None:
    if (
        _PUBLIC_ID.fullmatch(observation.observation_id) is None
        or type(observation.request_count) is not int
        or observation.request_count < 0
        or (
            observation.last_canary_id is not None
            and _PUBLIC_ID.fullmatch(observation.last_canary_id) is None
        )
    ):
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_observation_invalid")


def _validate_route_evidence(
    evidence: RouteAttemptEvidence,
    *,
    identity: DedicatedPlaneIdentity,
    expected_job_id: str,
    expected_status: str,
    require_selected: bool | None,
) -> None:
    if (
        evidence.route_id != identity.route_id
        or evidence.job_id != expected_job_id
        or evidence.job_status != expected_status
        or type(evidence.attempt_count) is not int
        or evidence.attempt_count <= 0
        or type(evidence.shared_attempt_count) is not int
        or evidence.shared_attempt_count != 0
        or type(evidence.dedicated_attempt_count) is not int
        or evidence.dedicated_attempt_count != evidence.attempt_count
        or type(evidence.selected_attempt_count) is not int
        or evidence.selected_attempt_count < 0
        or type(evidence.unavailable_attempt_count) is not int
        or evidence.unavailable_attempt_count < 0
        or evidence.selected_attempt_count + evidence.unavailable_attempt_count
        != evidence.attempt_count
        or (require_selected is True and evidence.selected_attempt_count <= 0)
        or (require_selected is False and evidence.selected_attempt_count != 0)
        or evidence.history_complete is not True
    ):
        raise OpenMAICDedicatedOutageProbeError("dedicated_route_evidence_invalid")


def _validate_fixture_audit_inventory(inventory: FixtureAuditInventory) -> None:
    expected_reversible = (
        "classEnrollment",
        "tenantMembership",
        "teacherIdentity",
    )
    expected_retained_types = (
        "course",
        "class",
        "generationQuotaGrant",
        "classroomAsset",
        "generationJob",
        "classroomAsset",
        "generationJob",
    )
    if (
        inventory.reversible_resources_deleted != expected_reversible
        or tuple(item.resource_type for item in inventory.retained_resources)
        != expected_retained_types
        or any(
            _PUBLIC_ID.fullmatch(item.resource_id) is None for item in inventory.retained_resources
        )
    ):
        raise OpenMAICDedicatedOutageProbeError("fixture_audit_inventory_invalid")


def _validated_docker_boundary(
    raw: Mapping[str, str],
    *,
    expected_host_identity_sha256: str,
) -> dict[str, str]:
    expected_fields = {
        "dockerHostIdentitySha256",
        "daemonIdentityBeforeSha256",
        "daemonIdentityAfterSha256",
        "inventoryBeforeSha256",
        "inventoryAfterSha256",
    }
    boundary = dict(raw)
    if (
        set(boundary) != expected_fields
        or boundary.get("dockerHostIdentitySha256") != expected_host_identity_sha256
        or any(_SHA256.fullmatch(boundary.get(name, "")) is None for name in expected_fields)
        or any(boundary.get(name) == "0" * 64 for name in expected_fields)
        or boundary.get("daemonIdentityBeforeSha256") != boundary.get("daemonIdentityAfterSha256")
        or boundary.get("inventoryBeforeSha256") != boundary.get("inventoryAfterSha256")
    ):
        raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
    return boundary


def _utc_observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


CandidateLoader: TypeAlias = Callable[[Path], tuple[Mapping[str, object], str]]
DockerResolver: TypeAlias = Callable[[], Path]
DockerRunner: TypeAlias = Callable[..., subprocess.CompletedProcess[bytes]]


def _default_candidate_loader(candidate_root: Path) -> tuple[dict[str, object], str]:
    lock = validate_image_lock_bindings(
        candidate_root / "deploy" / "image-lock.json",
        compose_paths=(
            candidate_root / "docker-compose.platform.yml",
            candidate_root / "docker-compose.data-plane.yml",
        ),
        require_candidate=True,
    )
    candidate = lock.get("candidate")
    images = lock.get("images")
    openmaic = images.get("openmaic") if isinstance(images, dict) else None
    reference = openmaic.get("reference") if isinstance(openmaic, dict) else None
    if not isinstance(candidate, dict) or not isinstance(reference, str):
        raise OpenMAICDedicatedOutageProbeError("candidate_invalid")
    return dict(candidate), reference


def _valid_base_url(value: object, *, allow_remote_http: bool = False) -> bool:
    if not isinstance(value, str) or not value or value != value.rstrip("/"):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    if parsed.scheme == "http" and not allow_remote_http:
        try:
            if parsed.hostname is None or not ipaddress.ip_address(parsed.hostname).is_loopback:
                return False
        except ValueError:
            return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _valid_endpoint_url(value: object, *, allow_remote_http: bool = False) -> bool:
    if not isinstance(value, str) or not value or value != value.rstrip("/"):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    if parsed.scheme == "http" and not allow_remote_http:
        try:
            if parsed.hostname is None or not ipaddress.ip_address(parsed.hostname).is_loopback:
                return False
        except ValueError:
            return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/")
        and parsed.path != "/"
        and not parsed.query
        and not parsed.fragment
    )


def _canonical_origin(value: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
        if hostname is None:
            raise ValueError
        canonical_host = hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        raise OpenMAICDedicatedOutageProbeError("endpoint_origin_invalid") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise OpenMAICDedicatedOutageProbeError("endpoint_origin_invalid")
    return scheme, canonical_host, port or (443 if scheme == "https" else 80)


def _url_origin(value: str) -> str:
    scheme, hostname, port = _canonical_origin(value)
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{rendered_host}{suffix}"


def _explicit_regular_file_outside_candidate(
    raw_path: object,
    *,
    candidate_root: Path,
) -> tuple[Path, bytes]:
    if not isinstance(raw_path, str) or not raw_path:
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid")
    supplied = Path(raw_path)
    try:
        if not supplied.is_absolute():
            raise OSError
        resolved = supplied.resolve(strict=True)
        if supplied != resolved or resolved == candidate_root or candidate_root in resolved.parents:
            raise OSError
        body = _existing_regular_body(resolved)
    except (OSError, OpenMAICDedicatedOutageProbeError) as exc:
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid") from exc
    return resolved, body


def _isolated_docker_config(raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
    supplied = Path(raw_path)
    try:
        if not supplied.is_absolute():
            raise OSError
        resolved = supplied.resolve(strict=True)
        details = supplied.lstat()
        if supplied != resolved or not stat.S_ISDIR(details.st_mode) or any(resolved.iterdir()):
            raise OSError
    except OSError as exc:
        raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid") from exc
    return resolved


def _canonical_docker_host(raw_host: object) -> tuple[str, str]:
    if not isinstance(raw_host, str) or not raw_host:
        raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
    if raw_host == "npipe:////./pipe/docker_engine":
        identity = {"scheme": "npipe", "pipePath": "//./pipe/docker_engine"}
        canonical = raw_host
    else:
        parsed = urlsplit(raw_host)
        if (
            parsed.scheme != "unix"
            or parsed.netloc
            or not parsed.path.startswith("/")
            or parsed.path == "/"
            or parsed.query
            or parsed.fragment
        ):
            raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
        canonical = f"unix://{parsed.path}"
        if canonical != raw_host:
            raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
        identity = {"scheme": "unix", "socketPath": parsed.path}
    canonical_identity = (
        json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    return canonical, hashlib.sha256(canonical_identity).hexdigest()


def _required_environment_id(environment: Mapping[str, str], name: str, error: str) -> str:
    return _public_id(environment.get(name), error)


def _load_config(
    environment: Mapping[str, str],
    *,
    cwd: Path,
    candidate_loader: CandidateLoader = _default_candidate_loader,
    docker_resolver: DockerResolver = resolve_fixed_docker,
) -> OutageProbeConfig:
    raw_root = environment.get("YFEISTAI_CANDIDATE_ROOT")
    if not isinstance(raw_root, str) or not raw_root:
        raise OpenMAICDedicatedOutageProbeError("candidate_root_invalid")
    try:
        candidate_root = Path(raw_root).resolve(strict=True)
        current_root = Path(cwd).resolve(strict=True)
        runtime_root = (candidate_root / "runtime").resolve(strict=True)
    except OSError as exc:
        raise OpenMAICDedicatedOutageProbeError("candidate_root_invalid") from exc
    if candidate_root != current_root or runtime_root.parent != candidate_root:
        raise OpenMAICDedicatedOutageProbeError("candidate_root_invalid")

    token = environment.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise OpenMAICDedicatedOutageProbeError("fixture_token_unavailable")
    token = token.strip()

    release_run = {
        "runId": _required_environment_id(
            environment,
            "YFEISTAI_RELEASE_RUN_ID",
            "release_identity_invalid",
        ),
        "environmentId": _required_environment_id(
            environment,
            "YFEISTAI_ENVIRONMENT_ID",
            "release_identity_invalid",
        ),
    }
    tenant_id = _required_environment_id(
        environment,
        "YFEISTAI_DEDICATED_TENANT_ID",
        "dedicated_tenant_invalid",
    )
    container_id = environment.get("YFEISTAI_DEDICATED_OUTAGE_CONTAINER_ID")
    if not isinstance(container_id, str) or _CONTAINER_ID.fullmatch(container_id) is None:
        raise OpenMAICDedicatedOutageProbeError("dedicated_container_invalid")
    project = environment.get("YFEISTAI_DEDICATED_OUTAGE_PROJECT")
    if not isinstance(project, str) or _PROJECT.fullmatch(project) is None:
        raise OpenMAICDedicatedOutageProbeError("dedicated_project_invalid")
    route_id = _required_environment_id(
        environment,
        "YFEISTAI_DEDICATED_ROUTE_ID",
        "dedicated_route_invalid",
    )
    if route_id == "shared-primary":
        raise OpenMAICDedicatedOutageProbeError("dedicated_route_invalid")
    base_url = environment.get("WEB_BASE_URL")
    if not _valid_base_url(base_url, allow_remote_http=False):
        raise OpenMAICDedicatedOutageProbeError("base_url_invalid")
    assert isinstance(base_url, str)
    observer_id = environment.get("YFEISTAI_SHARED_INGRESS_OBSERVER_ID")
    observer_url = environment.get("YFEISTAI_SHARED_INGRESS_OBSERVER_URL")
    control_url = environment.get("YFEISTAI_SHARED_INGRESS_CONTROL_URL")
    expected_observer_sha256 = environment.get(
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_SHA256"
    )
    if (
        not isinstance(observer_id, str)
        or _PUBLIC_ID.fullmatch(observer_id) is None
        or not _valid_base_url(observer_url)
        or not _valid_endpoint_url(control_url)
        or not isinstance(expected_observer_sha256, str)
        or _SHA256.fullmatch(expected_observer_sha256) is None
        or expected_observer_sha256 == "0" * 64
    ):
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid")
    assert isinstance(observer_url, str)
    assert isinstance(control_url, str)
    observer_origin_identity = _canonical_origin(observer_url)
    control_origin_identity = _canonical_origin(control_url)
    if observer_origin_identity in {
        control_origin_identity,
        _canonical_origin(base_url),
    }:
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid")
    observer_origin = _url_origin(observer_url)
    control_origin = _url_origin(control_url)
    observer_attestation_path, observer_body = _explicit_regular_file_outside_candidate(
        environment.get("YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PATH"),
        candidate_root=candidate_root,
    )
    observer_sha256 = hashlib.sha256(observer_body).hexdigest()
    if observer_sha256 != expected_observer_sha256:
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid")
    try:
        observer_report = parse_openmaic_shared_ingress_observer_attestation(
            observer_body,
            release_run=release_run,
        )
    except ValueError as exc:
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid") from exc
    observer_binding = observer_report.get("observer")
    if (
        not isinstance(observer_binding, dict)
        or observer_binding.get("observerId") != observer_id
        or observer_binding.get("observerUrl") != observer_url
        or observer_binding.get("sharedIngressControlUrl") != control_url
    ):
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid")
    runtime_sha256 = environment.get("YFEISTAI_RUNTIME_ATTESTATION_SHA256")
    if (
        not isinstance(runtime_sha256, str)
        or _SHA256.fullmatch(runtime_sha256) is None
        or runtime_sha256 == "0" * 64
    ):
        raise OpenMAICDedicatedOutageProbeError("runtime_attestation_invalid")
    try:
        timeout_seconds = int(environment.get("YFEISTAI_OPENMAIC_OUTAGE_TIMEOUT_SECONDS", ""))
    except ValueError as exc:
        raise OpenMAICDedicatedOutageProbeError("timeout_invalid") from exc
    if timeout_seconds < 60 or timeout_seconds > 3_600:
        raise OpenMAICDedicatedOutageProbeError("timeout_invalid")
    docker_config_dir = _isolated_docker_config(environment.get("YFEISTAI_OUTAGE_DOCKER_CONFIG"))
    docker_host, docker_host_identity_sha256 = _canonical_docker_host(
        environment.get("YFEISTAI_OUTAGE_DOCKER_HOST")
    )
    try:
        candidate, openmaic_reference = candidate_loader(candidate_root)
        docker_path = Path(docker_resolver()).resolve(strict=True)
    except OpenMAICDedicatedOutageProbeError:
        raise
    except Exception as exc:
        raise OpenMAICDedicatedOutageProbeError("candidate_invalid") from exc
    if not candidate or "@sha256:" not in openmaic_reference:
        raise OpenMAICDedicatedOutageProbeError("candidate_invalid")

    return OutageProbeConfig(
        admin_token=SecretStr(token),
        attempt_marker_path=runtime_root / "openmaic-dedicated-outage-attempt.json",
        base_url=base_url,
        candidate=dict(candidate),
        candidate_root=candidate_root,
        docker_path=docker_path,
        dedicated_container_id=container_id,
        dedicated_project=project,
        dedicated_route_id=route_id,
        dedicated_tenant_id=tenant_id,
        openmaic_image_reference=openmaic_reference,
        release_run=release_run,
        observer_attestation_path=observer_attestation_path,
        observer_attestation_sha256=observer_sha256,
        observer_id=observer_id,
        observer_url=observer_url,
        observer_origin=observer_origin,
        shared_ingress_control_url=control_url,
        shared_ingress_control_origin=control_origin,
        output_path=runtime_root / "openmaic-dedicated-outage-attestation.json",
        runtime_attestation_sha256=runtime_sha256,
        timeout_seconds=timeout_seconds,
        docker_config_dir=docker_config_dir,
        docker_host=docker_host,
        docker_host_identity_sha256=docker_host_identity_sha256,
    )


def _docker_environment(environment: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    by_upper = {name.upper(): value for name, value in environment.items()}
    return {
        name: by_upper[name]
        for name in allowed
        if isinstance(by_upper.get(name), str) and by_upper[name]
    }


def _default_docker_runner(
    arguments: Sequence[str | Path],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(value) for value in arguments],
        cwd=cwd,
        env=dict(env),
        timeout=timeout,
        check=False,
        capture_output=True,
    )


class DockerDedicatedPlaneController:
    """Exact-container Docker boundary for one disposable outage fixture."""

    def __init__(
        self,
        config: OutageProbeConfig,
        *,
        runner: DockerRunner = _default_docker_runner,
        environment: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._runner = runner
        self._environment = _docker_environment(environment or os.environ)
        self._sleep = sleep
        self._monotonic = monotonic
        docker_config_dir = _isolated_docker_config(str(config.docker_config_dir or ""))
        docker_host, docker_host_identity_sha256 = _canonical_docker_host(config.docker_host)
        if (
            docker_config_dir != config.docker_config_dir
            or docker_host != config.docker_host
            or docker_host_identity_sha256 != config.docker_host_identity_sha256
        ):
            raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
        self._daemon_identity_before: bytes | None = None
        self._inventory_before: bytes | None = None

    def _run(self, *arguments: str, timeout: int | None = None) -> bytes:
        docker_config_dir = _isolated_docker_config(str(self._config.docker_config_dir or ""))
        try:
            completed = self._runner(
                [
                    self._config.docker_path,
                    "--config",
                    str(docker_config_dir),
                    "--host",
                    self._config.docker_host,
                    *arguments,
                ],
                cwd=self._config.candidate_root,
                env=self._environment,
                timeout=min(timeout or 60, self._config.timeout_seconds),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OpenMAICDedicatedOutageProbeError("docker_command_failed") from exc
        stdout = bytes(completed.stdout or b"")
        stderr = bytes(completed.stderr or b"")
        if completed.returncode != 0 or len(stdout) > 1024 * 1024 or len(stderr) > 1024 * 1024:
            raise OpenMAICDedicatedOutageProbeError("docker_command_failed")
        return stdout

    @staticmethod
    def _canonical_json_bytes(value: object) -> bytes:
        return (
            json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        )

    def _daemon_identity(self) -> bytes:
        raw = self._run("info", "--format", "{{json .}}")
        try:
            document = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OpenMAICDedicatedOutageProbeError("docker_daemon_identity_invalid") from exc
        fields = (
            "ID",
            "Name",
            "ServerVersion",
            "Driver",
            "OperatingSystem",
            "KernelVersion",
            "DockerRootDir",
        )
        if not isinstance(document, dict) or any(
            not isinstance(document.get(name), str) or not document[name] for name in fields
        ):
            raise OpenMAICDedicatedOutageProbeError("docker_daemon_identity_invalid")
        return self._canonical_json_bytes({name: document[name] for name in fields})

    def _inventory_snapshot(self) -> bytes:
        raw = self._run("ps", "-a", "--no-trunc", "--format", "{{json .}}")
        documents: list[dict[str, str]] = []
        try:
            for raw_line in raw.splitlines():
                if not raw_line:
                    continue
                document = json.loads(raw_line)
                if not isinstance(document, dict):
                    raise ValueError
                fields = ("ID", "Names", "Image", "State")
                if any(
                    not isinstance(document.get(name), str) or not document[name] for name in fields
                ):
                    raise ValueError
                documents.append({name: document[name] for name in fields})
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise OpenMAICDedicatedOutageProbeError("docker_inventory_invalid") from exc
        documents.sort(key=lambda item: (item["ID"], item["Names"]))
        return self._canonical_json_bytes(documents)

    def _stable_inventory(self) -> bytes:
        first = self._inventory_snapshot()
        second = self._inventory_snapshot()
        if first != second:
            raise OpenMAICDedicatedOutageProbeError("docker_inventory_unstable")
        return first

    def _capture_boundary_before(self) -> None:
        if self._daemon_identity_before is not None or self._inventory_before is not None:
            return
        self._daemon_identity_before = self._daemon_identity()
        self._inventory_before = self._stable_inventory()

    def boundary_attestation(self) -> dict[str, str]:
        if self._daemon_identity_before is None or self._inventory_before is None:
            raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
        daemon_after = self._daemon_identity()
        inventory_after = self._stable_inventory()
        if (
            daemon_after != self._daemon_identity_before
            or inventory_after != self._inventory_before
        ):
            raise OpenMAICDedicatedOutageProbeError("docker_boundary_changed")
        return {
            "dockerHostIdentitySha256": self._config.docker_host_identity_sha256,
            "daemonIdentityBeforeSha256": hashlib.sha256(self._daemon_identity_before).hexdigest(),
            "daemonIdentityAfterSha256": hashlib.sha256(daemon_after).hexdigest(),
            "inventoryBeforeSha256": hashlib.sha256(self._inventory_before).hexdigest(),
            "inventoryAfterSha256": hashlib.sha256(inventory_after).hexdigest(),
        }

    def _inspect(self) -> dict[str, Any]:
        raw = self._run(
            "container",
            "inspect",
            self._config.dedicated_container_id,
        )
        try:
            documents = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_identity_invalid") from exc
        if (
            not isinstance(documents, list)
            or len(documents) != 1
            or not isinstance(documents[0], dict)
        ):
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_identity_invalid")
        return documents[0]

    def _identity(self, document: Mapping[str, object]) -> DedicatedPlaneIdentity:
        config_document = document.get("Config")
        host_config = document.get("HostConfig")
        network = document.get("NetworkSettings")
        state = document.get("State")
        if not all(
            isinstance(value, dict) for value in (config_document, host_config, network, state)
        ):
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_identity_invalid")
        assert isinstance(config_document, dict)
        assert isinstance(host_config, dict)
        assert isinstance(network, dict)
        labels = config_document.get("Labels")
        restart_policy = host_config.get("RestartPolicy")
        expected_labels = {
            "com.docker.compose.project": self._config.dedicated_project,
            "com.docker.compose.service": "openmaic",
            "com.yfeistai.acceptance.disposable": "true",
            "com.yfeistai.acceptance.environment-id": self._config.release_run["environmentId"],
            "com.yfeistai.acceptance.purpose": "openmaic-dedicated-outage",
            "com.yfeistai.acceptance.run-id": self._config.release_run["runId"],
            "com.yfeistai.acceptance.tenant-id": self._config.dedicated_tenant_id,
        }
        if (
            document.get("Id") != self._config.dedicated_container_id
            or config_document.get("Image") != self._config.openmaic_image_reference
            or not isinstance(labels, dict)
            or any(labels.get(name) != value for name, value in expected_labels.items())
            or not isinstance(restart_policy, dict)
            or restart_policy.get("Name") != "no"
            or network.get("Ports") not in ({}, None)
        ):
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_identity_invalid")
        route_id = labels.get("com.yfeistai.acceptance.route-id")
        if not isinstance(route_id, str):
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_identity_invalid")
        identity = DedicatedPlaneIdentity(
            container_id=self._config.dedicated_container_id,
            project=self._config.dedicated_project,
            route_id=route_id,
            tenant_id=self._config.dedicated_tenant_id,
        )
        _validate_identity(self._config, identity)
        return identity

    @staticmethod
    def _running(document: Mapping[str, object]) -> tuple[bool, str | None]:
        state = document.get("State")
        if not isinstance(state, dict) or type(state.get("Running")) is not bool:
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_state_invalid")
        health = state.get("Health")
        health_status = health.get("Status") if isinstance(health, dict) else None
        return state["Running"], health_status if isinstance(health_status, str) else None

    def verify_disposable_plane(self) -> DedicatedPlaneIdentity:
        self._capture_boundary_before()
        document = self._inspect()
        identity = self._identity(document)
        running, health = self._running(document)
        if not running or health != "healthy":
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_not_ready")
        return identity

    def stop(self, identity: DedicatedPlaneIdentity) -> None:
        _validate_identity(self._config, identity)
        self._run("stop", "--time", "30", identity.container_id, timeout=45)
        document = self._inspect()
        self._identity(document)
        running, _health = self._running(document)
        if running:
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_stop_failed")

    def start(self, identity: DedicatedPlaneIdentity) -> None:
        _validate_identity(self._config, identity)
        self._run("start", identity.container_id, timeout=45)
        document = self._inspect()
        self._identity(document)
        running, _health = self._running(document)
        if not running:
            raise OpenMAICDedicatedOutageProbeError("dedicated_plane_start_failed")

    def wait_ready(self, identity: DedicatedPlaneIdentity) -> None:
        _validate_identity(self._config, identity)
        deadline = self._monotonic() + min(120, self._config.timeout_seconds)
        while True:
            document = self._inspect()
            self._identity(document)
            running, health = self._running(document)
            if running and health == "healthy":
                return
            if not running or self._monotonic() >= deadline:
                raise OpenMAICDedicatedOutageProbeError("dedicated_plane_restore_unhealthy")
            self._sleep(0.25)


class SharedIngressObserver:
    """Read and control-test one independent shared-ingress counter."""

    def __init__(
        self,
        config: OutageProbeConfig,
        *,
        observer_transport: httpx.AsyncBaseTransport | None = None,
        control_transport: httpx.AsyncBaseTransport | None = None,
        canary_id_factory: Callable[[], str] = lambda: f"shared-control-{uuid.uuid4().hex}",
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._observer_client = httpx.AsyncClient(
            base_url=config.observer_url,
            follow_redirects=False,
            timeout=httpx.Timeout(min(float(config.timeout_seconds), 20.0)),
            transport=observer_transport,
            trust_env=False,
            headers={"Accept": "application/json"},
        )
        self._control_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(min(float(config.timeout_seconds), 20.0)),
            transport=control_transport,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self._canary_id_factory = canary_id_factory
        self._sleep = sleep
        self._monotonic = monotonic
        run_id = quote(config.release_run["runId"], safe="")
        self._path = f"/v1/observations/{run_id}"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        try:
            await self._observer_client.aclose()
        finally:
            await self._control_client.aclose()

    async def _request(self, method: str, path: str, *, status: int) -> httpx.Response:
        try:
            response = await self._observer_client.request(method, path)
        except httpx.HTTPError as exc:
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_unavailable") from exc
        if (
            response.is_redirect
            or response.status_code != status
            or len(response.content) > 64 * 1024
        ):
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid")
        return response

    async def read(self) -> SharedIngressObservation:
        response = await self._request("GET", self._path, status=200)
        try:
            document = response.json()
        except (UnicodeError, ValueError) as exc:
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid") from exc
        if not isinstance(document, dict) or set(document) != {
            "schemaVersion",
            "runId",
            "environmentId",
            "observationId",
            "requestCount",
            "lastCanaryId",
        }:
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid")
        observation = SharedIngressObservation(
            observation_id=document.get("observationId"),
            request_count=document.get("requestCount"),
            last_canary_id=document.get("lastCanaryId"),
        )
        if (
            document.get("schemaVersion") != 1
            or document.get("runId") != self._config.release_run["runId"]
            or document.get("environmentId") != self._config.release_run["environmentId"]
        ):
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_observer_invalid")
        _validate_observation(observation)
        return observation

    async def control(self) -> SharedIngressObservation:
        before = await self.read()
        canary_id = _public_id(
            self._canary_id_factory(),
            "shared_ingress_control_invalid",
        )
        if canary_id == before.last_canary_id:
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_control_invalid")
        try:
            response = await self._control_client.post(
                self._config.shared_ingress_control_url,
                json={
                    "schemaVersion": 1,
                    "runId": self._config.release_run["runId"],
                    "environmentId": self._config.release_run["environmentId"],
                    "canaryId": canary_id,
                    "kind": "openmaic-shared-ingress-control",
                },
            )
        except httpx.HTTPError as exc:
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_control_invalid") from exc
        if response.is_redirect or response.status_code != 202 or len(response.content) > 4096:
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_control_invalid")
        try:
            accepted = response.json()
        except (UnicodeError, ValueError) as exc:
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_control_invalid") from exc
        if accepted != {"accepted": True, "canaryId": canary_id}:
            raise OpenMAICDedicatedOutageProbeError("shared_ingress_control_invalid")

        deadline = self._monotonic() + min(float(self._config.timeout_seconds), 20.0)
        while True:
            after = await self.read()
            if after.request_count != before.request_count:
                if (
                    after.observation_id != before.observation_id
                    or after.request_count != before.request_count + 1
                    or after.last_canary_id != canary_id
                ):
                    raise OpenMAICDedicatedOutageProbeError("shared_ingress_control_invalid")
                return after
            if after.last_canary_id == canary_id or self._monotonic() >= deadline:
                raise OpenMAICDedicatedOutageProbeError("shared_ingress_control_invalid")
            await self._sleep(0.1)


def _route_evidence_from_binding(
    binding: Mapping[str, object],
    *,
    tenant_id: str,
    job_id: str,
    expected_status: str,
) -> RouteAttemptEvidence:
    if set(binding) != _BINDING_RESPONSE_FIELDS:
        raise OpenMAICDedicatedOutageProbeError("route_evidence_invalid")
    if (
        binding.get("schemaVersion") != 1
        or binding.get("tenantId") != tenant_id
        or binding.get("jobId") != job_id
        or binding.get("jobKind") != "generation"
        or binding.get("status") != expected_status
        or binding.get("dataPlaneMode") != "dedicated"
        or binding.get("routeTenantId") != tenant_id
        or binding.get("routeOwnerKey") != tenant_id
        or binding.get("providerScope") != "dedicated"
        or binding.get("providerTenantId") != tenant_id
        or binding.get("providerOwnerKey") != tenant_id
    ):
        raise OpenMAICDedicatedOutageProbeError("route_evidence_invalid")
    route_id = _public_id(binding.get("dataPlaneRouteId"), "route_evidence_invalid")
    evidence = RouteAttemptEvidence(
        route_id=route_id,
        job_id=job_id,
        job_status=expected_status,
        attempt_count=binding.get("attemptCount"),
        shared_attempt_count=binding.get("sharedRouteAttemptCount"),
        dedicated_attempt_count=binding.get("dedicatedRouteAttemptCount"),
        selected_attempt_count=binding.get("selectedRouteAttemptCount"),
        unavailable_attempt_count=binding.get("unavailableRouteAttemptCount"),
        history_complete=binding.get("routeAttemptHistoryComplete"),
    )
    return evidence


class _LiveCandidateApi:
    """Formal candidate API fixture used on both sides of the outage window."""

    def __init__(
        self,
        config: OutageProbeConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._api = _OpenMAICSmokeApi(
            base_url=config.base_url,
            admin_token=config.admin_token.get_secret_value(),
            timeout_seconds=config.timeout_seconds,
            transport=transport,
        )
        material_config = SmokeProbeConfig(
            admin_token=config.admin_token,
            base_url=config.base_url,
            candidate=config.candidate,
            candidate_root=config.candidate_root,
            dedicated_tenant_id=config.dedicated_tenant_id,
            plane="dedicated",
            release_run={
                **dict(config.release_run),
                "runId": f"{config.release_run['runId']}-outage-fixture",
            },
            runtime_attestation_sha256=config.runtime_attestation_sha256,
            timeout_seconds=config.timeout_seconds,
        )
        self._material = _fixture_material(material_config)
        self._cleanup = _FixtureCleanupState(
            tenant_id=config.dedicated_tenant_id,
            teacher_username=self._material.teacher_username,
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._teacher_user_id: str | None = None
        self._course_id: str | None = None
        self._class_id: str | None = None
        self._outage_asset_id: str | None = None
        self._outage_job_id: str | None = None
        self._canary_asset_id: str | None = None
        self._canary_job_id: str | None = None
        self._cleanup_completed = False
        self._entered = False

    async def __aenter__(self) -> Self:
        await self._api.__aenter__()
        self._entered = True
        return self

    async def _reconcile_exit(self, args: tuple[object, ...]) -> None:
        cleanup_error: Exception | None = None
        try:
            if self._cleanup.identity_attempted:
                await self._api.cleanup_fixture(self._cleanup)
            self._cleanup_completed = True
        except Exception as exc:
            cleanup_error = exc
        try:
            if self._entered:
                await self._api.__aexit__(*args)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        finally:
            self._entered = False
        if cleanup_error is not None:
            raise OpenMAICDedicatedOutageProbeError("fixture_cleanup_failed") from cleanup_error

    async def __aexit__(self, *args: object) -> None:
        cleanup_task = asyncio.create_task(self._reconcile_exit(tuple(args)))
        _result, cancellation_seen = await _await_task_deferring_cancellation(cleanup_task)
        if cancellation_seen:
            raise asyncio.CancelledError

    async def prepare_fixture(self) -> None:
        tenant_id = self._config.dedicated_tenant_id
        try:
            await self._api.select_admin_tenant(tenant_id)
            await self._api.require_identity_absent(self._material.teacher_username)
            self._cleanup.identity_attempted = True
            created_user = await self._api.admin_json(
                "POST",
                "/api/v1/auth/users",
                expected_status=201,
                json_body={
                    "username": self._material.teacher_username,
                    "password": self._material.teacher_password.get_secret_value(),
                },
            )
            teacher_user_id = _local_user_id(
                created_user.get("user_id"),
                "teacher_create_invalid",
            )
            if (
                created_user.get("ok") is not True
                or created_user.get("username") != self._material.teacher_username
                or created_user.get("role") != "user"
                or created_user.get("is_admin") is not False
            ):
                raise OpenMAICDedicatedOutageProbeError("teacher_create_invalid")
            self._cleanup.teacher_user_id = teacher_user_id
            self._cleanup.membership_attempted = True
            membership = await self._api.admin_json(
                "POST",
                f"/api/v1/tenants/{tenant_id}/members",
                expected_status=200,
                tenant_id=tenant_id,
                json_body={"user_id": teacher_user_id, "role": "teacher"},
            )
            if (
                membership.get("tenant_id") != tenant_id
                or membership.get("user_id") != teacher_user_id
                or membership.get("roles") != ["teacher"]
            ):
                raise OpenMAICDedicatedOutageProbeError("teacher_membership_invalid")
            logged_in_user_id, _session = await self._api.login(
                username=self._material.teacher_username,
                password=self._material.teacher_password,
            )
            if logged_in_user_id != teacher_user_id:
                raise OpenMAICDedicatedOutageProbeError("teacher_login_invalid")
            await self._api.select_teacher_tenant(tenant_id)

            course_id = f"course-{self._material.resource_suffix}"
            class_id = f"class-{self._material.resource_suffix}"
            course = await self._api.admin_json(
                "POST",
                "/api/v1/teaching/courses",
                expected_status=201,
                tenant_id=tenant_id,
                json_body={"id": course_id, "title": "Dedicated outage acceptance"},
            )
            if course.get("id") != course_id or course.get("status") != "active":
                raise OpenMAICDedicatedOutageProbeError("course_create_invalid")
            classroom = await self._api.teacher_json(
                "POST",
                f"/api/v1/teaching/courses/{course_id}/classes",
                expected_status=201,
                tenant_id=tenant_id,
                json_body={"id": class_id, "name": "Dedicated outage acceptance"},
            )
            if (
                classroom.get("id") != class_id
                or classroom.get("courseId") != course_id
                or classroom.get("status") != "active"
            ):
                raise OpenMAICDedicatedOutageProbeError("class_create_invalid")
            self._cleanup.class_id = class_id
            self._cleanup.enrollment_attempted = True
            enrollment = await self._api.teacher_json(
                "POST",
                f"/api/v1/teaching/classes/{class_id}/enrollments",
                expected_status=201,
                tenant_id=tenant_id,
                json_body={"userId": teacher_user_id},
            )
            if (
                enrollment.get("classId") != class_id
                or enrollment.get("userId") != teacher_user_id
                or enrollment.get("status") != "active"
            ):
                raise OpenMAICDedicatedOutageProbeError("enrollment_create_invalid")
            quota = await self._api.admin_json(
                "POST",
                "/api/v1/teaching/generation-quota-grants",
                expected_status=200,
                tenant_id=tenant_id,
                headers={"Idempotency-Key": self._material.quota_idempotency_key},
                json_body={"units": 40},
            )
            if (
                quota.get("tenantId") != tenant_id
                or quota.get("units") != 40
                or type(quota.get("balance")) is not int
                or quota["balance"] < 40
            ):
                raise OpenMAICDedicatedOutageProbeError("generation_quota_invalid")
            self._teacher_user_id = teacher_user_id
            self._course_id = course_id
            self._class_id = class_id
        except OpenMAICDedicatedOutageProbeError:
            raise
        except (OpenMAICSmokeProbeError, KeyError, TypeError, ValueError) as exc:
            raise OpenMAICDedicatedOutageProbeError("fixture_prepare_failed") from exc

    async def _create_classroom(self, purpose: str) -> tuple[str, str]:
        if self._teacher_user_id is None or self._course_id is None or self._class_id is None:
            raise OpenMAICDedicatedOutageProbeError("fixture_not_prepared")
        tenant_id = self._config.dedicated_tenant_id
        created = await self._api.teacher_json(
            "POST",
            "/api/v1/classrooms",
            expected_status=202,
            tenant_id=tenant_id,
            headers={"Idempotency-Key": f"{self._material.classroom_idempotency_key}-{purpose}"},
            json_body={
                "title": f"Dedicated {purpose} acceptance",
                "courseId": self._course_id,
                "classId": self._class_id,
                "objective": f"Prove dedicated plane {purpose}",
                "gradeBand": "grade-8",
                "audience": "intermediate",
                "durationMinutes": 15,
                "classroomMode": "full",
                "webPolicy": "disabled",
                "mediaPolicy": "text_only",
                "templateId": "first-release-acceptance",
                "templateVersion": "1",
                "knowledgePoints": [
                    {
                        "knowledgePointId": f"kp-dedicated-{purpose}",
                        "title": f"Dedicated {purpose}",
                        "description": f"Verify dedicated {purpose} behavior",
                    }
                ],
                "contentMode": "open_creation",
                "openCreationAcknowledged": True,
                "requestedExports": ["offline_html"],
            },
        )
        asset_id = _smoke_public_id(created.get("assetId"), "classroom_create_invalid")
        job_id = _smoke_public_id(created.get("jobId"), "classroom_create_invalid")
        if created.get("ownerId") != self._teacher_user_id:
            raise OpenMAICDedicatedOutageProbeError("classroom_create_invalid")
        return asset_id, job_id

    async def submit_outage_job(self) -> str:
        asset_id, job_id = await self._create_classroom("outage")
        self._outage_asset_id = asset_id
        self._outage_job_id = job_id
        return job_id

    async def wait_for_terminal_job(self, job_id: str) -> TerminalJob:
        deadline = self._monotonic() + self._config.timeout_seconds
        tenant_id = self._config.dedicated_tenant_id
        while True:
            body = await self._api.teacher_json(
                "GET",
                f"/api/v1/classroom-jobs/{job_id}",
                expected_status=200,
                tenant_id=tenant_id,
            )
            if (
                body.get("job_id") != job_id
                or body.get("job_kind") != "generation"
                or body.get("phase") not in {"outline", "content"}
            ):
                raise OpenMAICDedicatedOutageProbeError("outage_terminal_invalid")
            status = body.get("status")
            if status in {"failed", "canceled", "succeeded"}:
                return TerminalJob(
                    job_id=job_id,
                    status=status,
                    error_code=body.get("error_code"),
                )
            if self._monotonic() >= deadline:
                raise OpenMAICDedicatedOutageProbeError("outage_timeout")
            await self._sleep(0.25)

    async def read_route_evidence(
        self,
        job_id: str,
        *,
        expected_status: str,
    ) -> RouteAttemptEvidence:
        binding = await self._api.admin_json(
            "GET",
            f"/api/v1/system/classroom-jobs/{self._config.dedicated_tenant_id}/{job_id}/binding",
            expected_status=200,
        )
        return _route_evidence_from_binding(
            binding,
            tenant_id=self._config.dedicated_tenant_id,
            job_id=job_id,
            expected_status=expected_status,
        )

    async def run_canary(self) -> RouteAttemptEvidence:
        assert self._teacher_user_id is not None
        asset_id, job_id = await self._create_classroom("restore-canary")
        self._canary_asset_id = asset_id
        self._canary_job_id = job_id
        deadline = self._monotonic() + self._config.timeout_seconds
        tenant_id = self._config.dedicated_tenant_id
        try:
            await _wait_for_outline_job(
                self._api,
                tenant_id=tenant_id,
                job_id=job_id,
                deadline=deadline,
            )
            await _wait_for_outline_classroom(
                self._api,
                tenant_id=tenant_id,
                asset_id=asset_id,
                job_id=job_id,
                owner_id=self._teacher_user_id,
                deadline=deadline,
            )
            confirmed = await self._api.teacher_json(
                "POST",
                f"/api/v1/classrooms/{asset_id}/confirm-outline",
                expected_status=202,
                tenant_id=tenant_id,
            )
            if confirmed.get("jobId") != job_id:
                raise OpenMAICDedicatedOutageProbeError("restore_canary_invalid")
            await _wait_for_content_job(
                self._api,
                tenant_id=tenant_id,
                job_id=job_id,
                deadline=deadline,
            )
            await _wait_for_generated_classroom(
                self._api,
                tenant_id=tenant_id,
                asset_id=asset_id,
                job_id=job_id,
                owner_id=self._teacher_user_id,
                deadline=deadline,
            )
        except OpenMAICSmokeProbeError as exc:
            raise OpenMAICDedicatedOutageProbeError("restore_canary_failed") from exc
        return await self.read_route_evidence(job_id, expected_status="succeeded")

    def fixture_audit_inventory(self) -> FixtureAuditInventory:
        if (
            not self._cleanup_completed
            or self._course_id is None
            or self._class_id is None
            or self._outage_asset_id is None
            or self._outage_job_id is None
            or self._canary_asset_id is None
            or self._canary_job_id is None
        ):
            raise OpenMAICDedicatedOutageProbeError("fixture_audit_inventory_invalid")
        inventory = FixtureAuditInventory(
            reversible_resources_deleted=(
                "classEnrollment",
                "tenantMembership",
                "teacherIdentity",
            ),
            retained_resources=(
                RetainedAuditResource("course", self._course_id),
                RetainedAuditResource("class", self._class_id),
                RetainedAuditResource(
                    "generationQuotaGrant",
                    self._config.dedicated_tenant_id,
                ),
                RetainedAuditResource("classroomAsset", self._outage_asset_id),
                RetainedAuditResource("generationJob", self._outage_job_id),
                RetainedAuditResource("classroomAsset", self._canary_asset_id),
                RetainedAuditResource("generationJob", self._canary_job_id),
            ),
        )
        _validate_fixture_audit_inventory(inventory)
        return inventory


class LiveDedicatedOutageRuntime:
    """Compose exact Docker, candidate API, and observer boundaries."""

    def __init__(
        self,
        config: OutageProbeConfig,
        *,
        controller: DockerDedicatedPlaneController | None = None,
        observer: SharedIngressObserver | None = None,
        candidate_api: _LiveCandidateApi | None = None,
    ) -> None:
        self._controller = controller or DockerDedicatedPlaneController(config)
        self._observer = observer or SharedIngressObserver(config)
        self._candidate_api = candidate_api or _LiveCandidateApi(config)
        self._stack = AsyncExitStack()
        self._docker_tasks: set[asyncio.Task[Any]] = set()
        self._docker_boundary: dict[str, str] | None = None

    async def __aenter__(self) -> Self:
        await self._stack.__aenter__()
        await self._stack.enter_async_context(self._observer)
        await self._stack.enter_async_context(self._candidate_api)
        return self

    async def __aexit__(self, *args: object) -> None:
        cancellation_seen = False
        first_error: BaseException | None = None
        for task in tuple(self._docker_tasks):
            try:
                _result, cancelled = await _await_task_deferring_cancellation(task)
                cancellation_seen = cancellation_seen or cancelled
            except BaseException as exc:
                first_error = first_error or exc
        stack_cleanup = asyncio.create_task(self._stack.__aexit__(*args))
        try:
            _result, cancelled = await _await_task_deferring_cancellation(stack_cleanup)
            cancellation_seen = cancellation_seen or cancelled
        except BaseException as exc:
            first_error = first_error or exc
        boundary_operation = getattr(self._controller, "boundary_attestation", None)
        if callable(boundary_operation):
            boundary_task = asyncio.create_task(asyncio.to_thread(boundary_operation))
            try:
                boundary, cancelled = await _await_task_deferring_cancellation(boundary_task)
                cancellation_seen = cancellation_seen or cancelled
                if not isinstance(boundary, dict):
                    raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
                self._docker_boundary = dict(boundary)
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
        if cancellation_seen:
            raise asyncio.CancelledError

    async def _run_docker(self, operation: Callable[..., object], *args: object) -> object:
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        self._docker_tasks.add(task)
        try:
            result, cancellation_seen = await _await_task_deferring_cancellation(task)
        finally:
            if task.done():
                self._docker_tasks.discard(task)
        if cancellation_seen:
            raise asyncio.CancelledError
        return result

    async def verify_disposable_plane(self) -> DedicatedPlaneIdentity:
        identity = await self._run_docker(self._controller.verify_disposable_plane)
        assert isinstance(identity, DedicatedPlaneIdentity)
        return identity

    async def prepare_outage_fixture(self) -> None:
        await self._candidate_api.prepare_fixture()

    async def control_shared_ingress(self) -> SharedIngressObservation:
        return await self._observer.control()

    async def read_shared_ingress(self) -> SharedIngressObservation:
        return await self._observer.read()

    async def stop_dedicated_plane(self, identity: DedicatedPlaneIdentity) -> None:
        await self._run_docker(self._controller.stop, identity)

    async def submit_outage_job(self) -> str:
        return await self._candidate_api.submit_outage_job()

    async def wait_for_terminal_job(self, job_id: str) -> TerminalJob:
        return await self._candidate_api.wait_for_terminal_job(job_id)

    async def read_job_route_evidence(self, job_id: str) -> RouteAttemptEvidence:
        return await self._candidate_api.read_route_evidence(job_id, expected_status="failed")

    async def start_dedicated_plane(self, identity: DedicatedPlaneIdentity) -> None:
        await self._run_docker(self._controller.start, identity)

    async def wait_dedicated_ready(self, identity: DedicatedPlaneIdentity) -> None:
        await self._run_docker(self._controller.wait_ready, identity)

    async def run_restoration_canary(self) -> RouteAttemptEvidence:
        return await self._candidate_api.run_canary()

    def fixture_audit_inventory(self) -> FixtureAuditInventory:
        return self._candidate_api.fixture_audit_inventory()

    def docker_boundary_attestation(self) -> Mapping[str, str]:
        if self._docker_boundary is None:
            raise OpenMAICDedicatedOutageProbeError("docker_boundary_invalid")
        return dict(self._docker_boundary)


def _existing_regular_body(path: Path) -> bytes:
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode):
            raise OpenMAICDedicatedOutageProbeError("attestation_path_invalid")
        body = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise OpenMAICDedicatedOutageProbeError("attestation_path_invalid") from exc
    if (details.st_dev, details.st_ino, details.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise OpenMAICDedicatedOutageProbeError("attestation_path_invalid")
    return body


def _publish_no_replace(
    target: Path,
    body: bytes,
    *,
    allow_identical: bool,
    existing_error: str,
) -> None:
    """Publish through a pinned candidate/runtime directory, never replace."""

    path = Path(target)
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise OpenMAICDedicatedOutageProbeError("attestation_path_invalid") from exc
    if (
        path.parent != parent
        or parent.name != "runtime"
        or path.name != str(path.relative_to(parent))
        or path.is_symlink()
    ):
        raise OpenMAICDedicatedOutageProbeError("attestation_path_invalid")

    staged_name = f".{path.name}.{uuid.uuid4().hex}.staged"
    staged_identity: tuple[int, int] | None = None
    published = False
    try:
        with _RuntimeDirectoryGuard.open(parent.parent, parent) as guard:
            guard.assert_bound()
            existing_body, existing_identity = guard.read_optional_regular_file(path.name)
            if existing_identity is not None:
                if allow_identical and existing_body == body:
                    return
                raise OpenMAICDedicatedOutageProbeError(existing_error)
            staged_identity = guard.write_new_file(staged_name, body)
            try:
                guard.assert_bound()
                existing_body, existing_identity = guard.read_optional_regular_file(path.name)
                if existing_identity is not None:
                    if allow_identical and existing_body == body:
                        return
                    raise OpenMAICDedicatedOutageProbeError(existing_error)
                try:
                    guard.replace(staged_name, path.name)
                except (FileExistsError, OSError) as exc:
                    existing_body, existing_identity = guard.read_optional_regular_file(path.name)
                    if existing_identity is not None:
                        if allow_identical and existing_body == body:
                            return
                        raise OpenMAICDedicatedOutageProbeError(existing_error) from exc
                    raise OpenMAICDedicatedOutageProbeError("attestation_publish_failed") from exc
                guard.assert_bound()
                published_body, published_identity = guard.read_optional_regular_file(path.name)
                if published_body != body or published_identity != staged_identity:
                    raise OpenMAICDedicatedOutageProbeError("attestation_publish_failed")
                guard.release_owned_file(staged_identity)
                published = True
            finally:
                if staged_identity is not None and not published:
                    removed = guard.remove_file_if_identity(path.name, staged_identity)
                    if not removed:
                        removed = guard.remove_file_if_identity(staged_name, staged_identity)
                    if not removed:
                        raise OpenMAICDedicatedOutageProbeError(
                            "attestation_staging_cleanup_failed"
                        )
    except OpenMAICDedicatedOutageProbeError:
        raise
    except (OSError, ValueError) as exc:
        if staged_identity is None:
            raise OpenMAICDedicatedOutageProbeError("attestation_path_invalid") from exc
        raise OpenMAICDedicatedOutageProbeError("attestation_publish_failed") from exc


def _atomic_publish(target: Path, body: bytes) -> None:
    _publish_no_replace(
        target,
        body,
        allow_identical=True,
        existing_error="attestation_already_exists",
    )


def _claim_run_attempt(config: OutageProbeConfig) -> str:
    marker = {
        "schemaVersion": OPENMAIC_SMOKE_SCHEMA_VERSION,
        "producer": "openmaic-dedicated-outage-attempt",
        "candidate": dict(config.candidate),
        "releaseRun": dict(config.release_run),
        "observerTrustAnchor": {
            "sha256": config.observer_attestation_sha256,
            "observerId": config.observer_id,
            "observerOrigin": config.observer_origin,
            "sharedIngressControlOrigin": config.shared_ingress_control_origin,
        },
        "fixturePlan": {
            "tenantId": config.dedicated_tenant_id,
            "routeId": config.dedicated_route_id,
            "cleanupBoundary": "identity-membership-enrollment-only",
            "retainedResourceTypes": [
                "course",
                "class",
                "generationQuotaGrant",
                "classroomAsset",
                "generationJob",
            ],
        },
    }
    body = canonical_openmaic_dedicated_outage_attempt_marker(marker)
    _publish_no_replace(
        config.attempt_marker_path,
        body,
        allow_identical=False,
        existing_error="outage_attempt_already_exists",
    )
    return hashlib.sha256(body).hexdigest()


async def _restore_dedicated_plane(
    runtime: DedicatedOutageRuntime,
    *,
    identity: DedicatedPlaneIdentity,
    before: SharedIngressObservation,
    after: SharedIngressObservation | None,
) -> RouteAttemptEvidence:
    await runtime.start_dedicated_plane(identity)
    await runtime.wait_dedicated_ready(identity)
    canary = await runtime.run_restoration_canary()
    _validate_route_evidence(
        canary,
        identity=identity,
        expected_job_id=canary.job_id,
        expected_status="succeeded",
        require_selected=True,
    )
    control_after = await runtime.control_shared_ingress()
    _validate_observation(control_after)
    expected_count = after.request_count + 1 if after is not None else before.request_count + 1
    if (
        control_after.observation_id != before.observation_id
        or control_after.request_count != expected_count
    ):
        raise OpenMAICDedicatedOutageProbeError("shared_ingress_control_after_invalid")
    return canary


async def run_dedicated_outage_probe(
    config: OutageProbeConfig,
    *,
    runtime: DedicatedOutageRuntime,
    observed_at: Callable[[], str] = _utc_observed_at,
) -> bytes:
    """Run the outage window and return one candidate-bound canonical attestation."""

    attempt_marker_sha256 = _claim_run_attempt(config)
    async with runtime:
        identity = await runtime.verify_disposable_plane()
        _validate_identity(config, identity)
        await runtime.prepare_outage_fixture()
        before = await runtime.control_shared_ingress()
        _validate_observation(before)

        stopped = False
        outage_job_id: str | None = None
        terminal: TerminalJob | None = None
        outage_evidence: RouteAttemptEvidence | None = None
        after: SharedIngressObservation | None = None
        primary_error: BaseException | None = None
        canary: RouteAttemptEvidence | None = None
        try:
            stopped = True
            await runtime.stop_dedicated_plane(identity)
            outage_job_id = _public_id(
                await runtime.submit_outage_job(),
                "outage_job_invalid",
            )
            terminal = await runtime.wait_for_terminal_job(outage_job_id)
            if (
                terminal.job_id != outage_job_id
                or terminal.status != "failed"
                or terminal.error_code != "dedicated_data_plane_unavailable"
            ):
                raise OpenMAICDedicatedOutageProbeError("outage_terminal_invalid")
            outage_evidence = await runtime.read_job_route_evidence(outage_job_id)
            _validate_route_evidence(
                outage_evidence,
                identity=identity,
                expected_job_id=outage_job_id,
                expected_status="failed",
                require_selected=None,
            )
            after = await runtime.read_shared_ingress()
            _validate_observation(after)
            if (
                after.observation_id != before.observation_id
                or after.request_count != before.request_count
            ):
                raise OpenMAICDedicatedOutageProbeError("shared_ingress_changed")
        except BaseException as exc:
            primary_error = exc
        finally:
            if stopped:
                restoration_task = asyncio.create_task(
                    _restore_dedicated_plane(
                        runtime,
                        identity=identity,
                        before=before,
                        after=after,
                    )
                )
                try:
                    restored, cancellation_seen = await _await_task_deferring_cancellation(
                        restoration_task
                    )
                    assert isinstance(restored, RouteAttemptEvidence)
                    canary = restored
                    if cancellation_seen and primary_error is None:
                        primary_error = asyncio.CancelledError()
                except BaseException as exc:
                    raise OpenMAICDedicatedOutageProbeError(
                        "dedicated_plane_restoration_failed"
                    ) from exc

        if primary_error is not None:
            raise primary_error
        assert outage_job_id is not None
        assert terminal is not None
        assert outage_evidence is not None
        assert after is not None
        assert canary is not None

    fixture_audit = runtime.fixture_audit_inventory()
    _validate_fixture_audit_inventory(fixture_audit)
    docker_boundary = _validated_docker_boundary(
        runtime.docker_boundary_attestation(),
        expected_host_identity_sha256=config.docker_host_identity_sha256,
    )
    attempt_marker_reference = {
        "artifact": "runtime/openmaic-dedicated-outage-attempt.json",
        "sha256": attempt_marker_sha256,
    }
    observer_trust_anchor = {
        "sha256": config.observer_attestation_sha256,
        "observerId": config.observer_id,
        "observerOrigin": config.observer_origin,
        "sharedIngressControlOrigin": config.shared_ingress_control_origin,
    }
    report = {
        "schemaVersion": OPENMAIC_SMOKE_SCHEMA_VERSION,
        "producer": OPENMAIC_DEDICATED_OUTAGE_PRODUCER,
        "candidate": dict(config.candidate),
        "releaseRun": dict(config.release_run),
        "observedAt": observed_at(),
        "baseUrl": config.base_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": config.runtime_attestation_sha256,
        },
        "observerAttestation": {
            "artifact": "runtime/openmaic-shared-ingress-observer-attestation.json",
            "sha256": config.observer_attestation_sha256,
            "observerId": config.observer_id,
            "observerOrigin": config.observer_origin,
            "sharedIngressControlOrigin": config.shared_ingress_control_origin,
        },
        "fixture": {
            "tenantId": config.dedicated_tenant_id,
            "attemptMarker": attempt_marker_reference,
            "cleanupBoundary": {
                "reason": "formal-delete-api-unavailable",
                "reversibleResourcesDeleted": list(fixture_audit.reversible_resources_deleted),
                "retainedAuditResources": [
                    {
                        "resourceType": item.resource_type,
                        "resourceId": item.resource_id,
                    }
                    for item in fixture_audit.retained_resources
                ],
            },
        },
        "provenance": {
            "attemptMarker": attempt_marker_reference,
            "observerTrustAnchor": observer_trust_anchor,
            "dockerBoundary": docker_boundary,
        },
        "outage": {
            "dedicatedPlaneStopped": True,
            "routeId": identity.route_id,
            "jobId": outage_job_id,
            "jobStatus": terminal.status,
            "errorCode": terminal.error_code,
            "attemptCount": outage_evidence.attempt_count,
            "sharedRouteAttemptCount": outage_evidence.shared_attempt_count,
            "dedicatedRouteAttemptCount": outage_evidence.dedicated_attempt_count,
            "selectedRouteAttemptCount": outage_evidence.selected_attempt_count,
            "unavailableRouteAttemptCount": outage_evidence.unavailable_attempt_count,
            "routeAttemptHistoryComplete": outage_evidence.history_complete,
        },
        "sharedIngress": {
            "observationId": before.observation_id,
            "requestCountBefore": before.request_count,
            "requestCountAfter": after.request_count,
        },
        "restoration": {
            "dedicatedPlaneRestored": True,
            "routeId": identity.route_id,
            "canaryJobId": canary.job_id,
            "canaryJobStatus": canary.job_status,
            "attemptCount": canary.attempt_count,
            "sharedRouteAttemptCount": canary.shared_attempt_count,
            "dedicatedRouteAttemptCount": canary.dedicated_attempt_count,
            "selectedRouteAttemptCount": canary.selected_attempt_count,
            "unavailableRouteAttemptCount": canary.unavailable_attempt_count,
            "routeAttemptHistoryComplete": canary.history_complete,
        },
    }
    return canonical_openmaic_dedicated_outage_attestation(report)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("first-release",))
    return parser.parse_args(argv)


async def _run_main(config: OutageProbeConfig) -> bytes:
    task = asyncio.create_task(
        run_dedicated_outage_probe(
            config,
            runtime=LiveDedicatedOutageRuntime(config),
        )
    )
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for selected_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected_signal, task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(selected_signal)
    try:
        return await task
    finally:
        for selected_signal in installed:
            loop.remove_signal_handler(selected_signal)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _parse_args(argv)
        config = _load_config(os.environ, cwd=Path.cwd())
        body = asyncio.run(_run_main(config))
        token = config.admin_token.get_secret_value().encode("utf-8")
        if token in body:
            raise OpenMAICDedicatedOutageProbeError("attestation_contains_secret")
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()
        return 0
    except (
        OpenMAICDedicatedOutageProbeError,
        OpenMAICSmokeProbeError,
        asyncio.CancelledError,
        KeyboardInterrupt,
        OSError,
        ValueError,
    ):
        sys.stderr.write("openmaic_dedicated_outage_probe_failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DedicatedPlaneIdentity",
    "OpenMAICDedicatedOutageProbeError",
    "OutageProbeConfig",
    "RouteAttemptEvidence",
    "SharedIngressObservation",
    "TerminalJob",
    "canonical_openmaic_dedicated_outage_attestation",
    "run_dedicated_outage_probe",
]
