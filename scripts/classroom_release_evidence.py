"""Write candidate-bound classroom release receipts and evidence manifests."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO, Protocol
from urllib.parse import urlsplit
import uuid

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from backup_restore_contract import derive_backup_restore_checks  # noqa: E402
from capacity_profile_contract import (  # noqa: E402
    MAX_CAPACITY_REPORT_BYTES,
    capacity_profile_command_record,
    derive_capacity_profile_summary,
    derive_learning_event_idempotency_checks,
    parse_capacity_profile_report,
)
from classroom_export_contract import (  # noqa: E402
    CLASSROOM_EXPORT_PATHS,
    MAX_CLASSROOM_EXPORT_REPORT_BYTES,
    MAX_EXPORT_BYTES,
    MAX_TOTAL_EXPORT_BYTES,
    classroom_export_archive_contains_forbidden_bytes,
    classroom_exports_command_record,
    derive_classroom_export_checks,
    parse_classroom_export_report,
)
from classroom_release_probe_contract import probe_command_record  # noqa: E402
from classroom_runtime_attestation import (  # noqa: E402
    _assert_no_link_ancestors,
    _close_windows_handle,
    _create_windows_directory_relative,
    _create_windows_staging_file,
    _delete_windows_file_on_close,
    _file_identity,
    _is_link_or_reparse,
    _open_posix_directory_path_no_follow,
    _open_windows_directory_handle,
    _open_windows_directory_relative,
    _open_windows_regular_file_relative,
    _rename_windows_file_relative,
    _windows_handle_identity,
    resolve_fixed_docker,
)
from gateway_public_contract import (  # noqa: E402
    derive_gateway_public_checks,
    parse_gateway_candidate_networks,
    parse_gateway_public_report,
    signed_gateway_observer_policy,
)
from gateway_trust_contract import verify_gateway_trust_pair  # noqa: E402
from openmaic_smoke_contract import (  # noqa: E402
    MAX_OPENMAIC_SMOKE_REPORT_BYTES,
    derive_openmaic_dedicated_outage_checks,
    derive_openmaic_dedicated_plane_checks,
    derive_openmaic_shared_plane_checks,
    openmaic_dedicated_plane_command_record,
    openmaic_shared_plane_command_record,
    parse_openmaic_dedicated_outage_attestation,
    parse_openmaic_shared_ingress_observer_attestation,
    parse_openmaic_smoke_report,
)
from platform_preflight_contract import (  # noqa: E402
    MAX_CANDIDATE_NETWORK_REPORT_BYTES,
    materialize_candidate_network_phase_command,
    parse_candidate_network_report,
)
from platform_preflight_contract import (
    PHASE_SERVICES as PREFLIGHT_PHASE_SERVICES,
)
from platform_preflight_contract import (
    PHASES as PREFLIGHT_PHASES,
)
from render_platform_compose import validate_image_lock_bindings  # noqa: E402
from tenant_isolation_contract import (  # noqa: E402
    MAX_TENANT_ISOLATION_REPORT_BYTES,
    derive_tenant_isolation_checks,
    parse_tenant_isolation_report,
    tenant_isolation_command_record,
)
from verify_classroom_release import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    PROBE_RECIPES,
    RECEIPT_CONTRACTS,
    _replay_openmaic_dedicated_outage_attempt_marker,
    derive_backup_restore_receipt_checks,
    derive_capacity_profile_receipt_checks,
    derive_capacity_profile_tenant_id,
    derive_capacity_profile_tenant_ids,
    derive_classroom_exports_receipt_checks,
    derive_platform_preflight_receipt_checks,
    derive_probe_checks,
    derive_tenant_isolation_receipt_checks,
    probe_provenance_error,
    read_backup_restore_report_artifact,
    read_capacity_profile_attestation_artifact,
    read_runtime_attestation_artifact,
    validate_runtime_attestation,
)

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class GitRunner(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]: ...


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


_DIRECT_RECEIPT_EVIDENCE = frozenset(("source_head", "image_digests"))
_PROBE_CLEANUP_MARGIN_SECONDS = 30
_CAPACITY_STDERR_LIMIT_BYTES = 64 * 1024
_CLASSROOM_EXPORT_STDERR_LIMIT_BYTES = 64 * 1024
_TENANT_ISOLATION_STDERR_LIMIT_BYTES = 64 * 1024
_OPENMAIC_SMOKE_STDERR_LIMIT_BYTES = 64 * 1024
_GATEWAY_DOCKER_PROJECT = "yfeistai-platform"
_GATEWAY_DOCKER_CONTEXT = "default"
_GATEWAY_DOCKER_CONTEXT_ARGUMENTS = [
    "context",
    "inspect",
    _GATEWAY_DOCKER_CONTEXT,
    "--format",
    "{{json .Endpoints.docker.Host}}",
]
_GATEWAY_DOCKER_INFO_ARGUMENTS = [
    "info",
    "--format",
    '{"serverId":{{json .ID}},"osType":{{json .OSType}}}',
]
_GATEWAY_DOCKER_PS_ARGUMENTS = [
    "ps",
    "-a",
    "--no-trunc",
    "--filter",
    f"label=com.docker.compose.project={_GATEWAY_DOCKER_PROJECT}",
    "--format",
    "{{json .ID}}",
]
_GATEWAY_DOCKER_INSPECT_FORMAT = (
    '{"containerId":{{json .Id}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"networkMode":{{json .HostConfig.NetworkMode}},'
    '"networks":{{json .NetworkSettings.Networks}},'
    '"publishedPorts":{{json .NetworkSettings.Ports}}}'
)
_GATEWAY_DOCKER_HOST_IDENTITY_ENV = "YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256"
_GATEWAY_MAX_INPUT_BYTES = 1024 * 1024
_GATEWAY_MAX_DOCKER_OUTPUT_BYTES = 1024 * 1024


def _candidate(candidate_root: Path) -> dict[str, Any]:
    root = Path(candidate_root)
    lock = validate_image_lock_bindings(
        root / "deploy" / "image-lock.json",
        compose_paths=(
            root / "docker-compose.platform.yml",
            root / "docker-compose.data-plane.yml",
        ),
        require_candidate=True,
    )
    candidate = lock.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("candidate image lock is invalid")
    return json.loads(json.dumps(candidate))


def _release_run(raw: Mapping[str, object]) -> dict[str, str]:
    if set(raw) != {"runId", "environmentId"}:
        raise ValueError("release run identity is invalid")
    values: dict[str, str] = {}
    for name in ("runId", "environmentId"):
        value = raw.get(name)
        if not isinstance(value, str) or _RELEASE_ID.fullmatch(value) is None:
            raise ValueError("release run identity is invalid")
        values[name] = value
    return values


def _valid_observed_at(raw: object) -> bool:
    if not isinstance(raw, str) or _OBSERVED_AT.fullmatch(raw) is None:
        return False
    try:
        datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _validate_pass_result(
    evidence: str,
    *,
    native_exit: object,
    checks: Mapping[str, object],
) -> tuple[str, dict[str, bool]]:
    contract = RECEIPT_CONTRACTS.get(evidence)
    if contract is None:
        raise ValueError("evidence layer is invalid")
    producer, required_checks = contract
    if not isinstance(native_exit, int) or isinstance(native_exit, bool) or native_exit != 0:
        raise ValueError("native exit does not prove passing evidence")
    if set(checks) != set(required_checks) or any(
        checks.get(name) is not True for name in required_checks
    ):
        raise ValueError("evidence checks must be explicit and passing")
    return producer, {name: True for name in required_checks}


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            staged = Path(handle.name)
            handle.write(_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _ManifestPublicationContext:
    boundary: Any
    source: Path
    source_handle: BinaryIO


_MANIFEST_PUBLICATION_CONTEXT: ContextVar[_ManifestPublicationContext | None] = ContextVar(
    "manifest_publication_context",
    default=None,
)


def _publish_no_replace(source: Path, target: Path) -> None:
    """Publish one staged regular file without replacing an existing target."""

    manifest_context = _MANIFEST_PUBLICATION_CONTEXT.get()
    if manifest_context is not None:
        if Path(source) != manifest_context.source:
            raise ValueError("manifest publication source is outside the held boundary")
        _publish_manifest_no_replace(
            manifest_context.boundary,
            Path(target),
            source_handle=manifest_context.source_handle,
        )
        return
    gateway_context = _GATEWAY_PUBLICATION_CONTEXT.get()
    if gateway_context is not None:
        source_handle = gateway_context.source_handles.get(Path(source))
        if source_handle is None:
            raise ValueError("gateway publication source is outside the held boundary")
        _publish_classroom_no_replace(
            gateway_context.boundary,
            Path(source),
            Path(target),
            source_handle=source_handle,
        )
        return
    os.link(source, target, follow_symlinks=False)


def _fsync_directory(path: Path) -> None:
    manifest_context = _MANIFEST_PUBLICATION_CONTEXT.get()
    if manifest_context is not None:
        boundary = manifest_context.boundary
        if Path(path) != boundary.root:
            raise ValueError("manifest publication directory is outside the held boundary")
        if os.name != "nt":
            os.fsync(int(boundary.leases["bundle"].handle))
        return
    if os.name == "nt":
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(Path(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _GatewayPublicationContext:
    boundary: Any
    source_handles: Mapping[Path, BinaryIO]


_GATEWAY_PUBLICATION_CONTEXT: ContextVar[_GatewayPublicationContext | None] = ContextVar(
    "gateway_publication_context",
    default=None,
)
_GATEWAY_STAGING_BOUNDARY: ContextVar[Any | None] = ContextVar(
    "gateway_staging_boundary",
    default=None,
)


@dataclass(frozen=True, slots=True)
class _GatewayPinnedInput:
    path: Path
    body: bytes
    identity: tuple[int, int]

    def assert_unchanged(self, *, label: str) -> None:
        current = _read_gateway_input(self.path, label=label)
        if current.identity != self.identity or current.body != self.body:
            raise ValueError(f"gateway {label} changed during observation")


def _read_gateway_input(path: Path, *, label: str) -> _GatewayPinnedInput:
    """Read one fixed regular file while rejecting link and replacement races."""

    candidate = Path(os.path.abspath(path))
    try:
        _assert_no_link_ancestors(candidate)
        if _is_link_or_reparse(candidate):
            raise ValueError("link")
        before = os.stat(candidate, follow_symlinks=False)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(opened.st_mode):
                raise ValueError("regular file")
            if _file_identity(before) != _file_identity(opened):
                raise ValueError("identity")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, _GATEWAY_MAX_INPUT_BYTES - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > _GATEWAY_MAX_INPUT_BYTES:
                    raise ValueError("size")
                chunks.append(chunk)
            body = b"".join(chunks)
        finally:
            os.close(descriptor)
        _assert_no_link_ancestors(candidate)
        after = os.stat(candidate, follow_symlinks=False)
        if _file_identity(after) != _file_identity(opened):
            raise ValueError("identity")
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"gateway {label} must use a stable regular no-follow boundary; "
            "symlink or reparse input is forbidden"
        ) from exc
    return _GatewayPinnedInput(candidate, body, _file_identity(opened))


def _gateway_docker_host_identity_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None or value == "0" * 64:
        raise ValueError("gateway Docker host identity is unavailable or invalid")
    return value


def _gateway_expected_docker_host_identity_sha256(value: object) -> str:
    by_upper = {name.upper(): item for name, item in os.environ.items()}
    environment_value = by_upper.get(_GATEWAY_DOCKER_HOST_IDENTITY_ENV)
    supplied = [item for item in (value, environment_value) if item is not None]
    if not supplied:
        raise ValueError("gateway Docker host identity is unavailable or invalid")
    validated = [_gateway_docker_host_identity_sha256(item) for item in supplied]
    if len(set(validated)) != 1:
        raise ValueError("gateway Docker host identity inputs do not match")
    return validated[0]


def _gateway_runtime_host_identity(
    runtime: Mapping[str, object],
    *,
    expected_sha256: str,
) -> dict[str, str]:
    raw = runtime.get("dockerHostIdentity")
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "context",
            "endpoint",
            "serverId",
            "dockerHostIdentitySha256",
        }
        or raw.get("context") != _GATEWAY_DOCKER_CONTEXT
        or not isinstance(raw.get("endpoint"), str)
        or not raw["endpoint"]
        or raw["endpoint"] != raw["endpoint"].strip()
        or not isinstance(raw.get("serverId"), str)
        or not raw["serverId"]
        or raw["serverId"] != raw["serverId"].strip()
        or raw.get("dockerHostIdentitySha256") != expected_sha256
    ):
        raise ValueError("gateway Docker host identity is invalid")
    return {
        "context": raw["context"],
        "endpoint": raw["endpoint"],
        "serverId": raw["serverId"],
        "dockerHostIdentitySha256": raw["dockerHostIdentitySha256"],
    }


def _gateway_child_environment() -> dict[str, str]:
    allowed = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "WINDIR",
    }
    by_upper = {name.upper(): value for name, value in os.environ.items()}
    return {name: by_upper[name] for name in allowed if name in by_upper}


def _run_gateway_docker(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=False,
        capture_output=True,
        text=True,
    )


def _gateway_command_stdout(
    completed: subprocess.CompletedProcess[str],
    *,
    arguments: list[str],
) -> str:
    if (
        type(completed.returncode) is not int
        or completed.returncode != 0
        or completed.args != arguments
        or not isinstance(completed.stdout, str)
        or completed.stderr != ""
    ):
        raise ValueError("gateway Docker observation command did not exit cleanly")
    try:
        body = completed.stdout.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("gateway Docker observation output is invalid") from exc
    if len(body) > _GATEWAY_MAX_DOCKER_OUTPUT_BYTES:
        raise ValueError("gateway Docker observation output is invalid")
    return completed.stdout


def _gateway_command_record(arguments: list[str], stdout: str) -> dict[str, object]:
    return {
        "argv": [
            "docker",
            "--config",
            "<isolated-docker-config>",
            "--context",
            _GATEWAY_DOCKER_CONTEXT,
            *arguments,
        ],
        "nativeExit": 0,
        "stdout": stdout,
        "stdoutSha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
    }


def _gateway_runtime_containers(runtime: Mapping[str, object]) -> dict[str, str]:
    rows = runtime.get("containers")
    if not isinstance(rows, list) or not rows:
        raise ValueError("gateway runtime container identity is invalid")
    containers: dict[str, str] = {}
    for row in rows:
        container_id = row.get("containerId") if isinstance(row, dict) else None
        service = row.get("service") if isinstance(row, dict) else None
        project = row.get("project") if isinstance(row, dict) else None
        if (
            not isinstance(container_id, str)
            or not container_id
            or not isinstance(service, str)
            or not service
            or project != _GATEWAY_DOCKER_PROJECT
            or container_id in containers
        ):
            raise ValueError("gateway runtime container identity is invalid")
        containers[container_id] = service
    if tuple(containers.values()).count("gateway") != 1:
        raise ValueError("gateway runtime container identity is invalid")
    return containers


def _gateway_published_ports(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        raise ValueError("gateway Docker published ports are invalid")
    published: list[dict[str, object]] = []
    for target, bindings in value.items():
        match = re.fullmatch(r"([1-9][0-9]{0,4})/(tcp|udp)", str(target))
        if match is None:
            raise ValueError("gateway Docker published ports are invalid")
        if bindings is None:
            continue
        if not isinstance(bindings, list):
            raise ValueError("gateway Docker published ports are invalid")
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"HostIp", "HostPort"}:
                raise ValueError("gateway Docker published ports are invalid")
            host_ip = binding.get("HostIp")
            host_port = binding.get("HostPort")
            try:
                parsed_host_port = int(host_port)
            except (TypeError, ValueError):
                raise ValueError("gateway Docker published ports are invalid") from None
            if not isinstance(host_ip, str) or not host_ip or not 1 <= parsed_host_port <= 65535:
                raise ValueError("gateway Docker published ports are invalid")
            published.append(
                {
                    "containerPort": int(match.group(1)),
                    "hostIp": host_ip,
                    "hostPort": parsed_host_port,
                    "protocol": match.group(2),
                }
            )
    return sorted(
        published,
        key=lambda row: (
            row["containerPort"],
            row["hostIp"],
            row["hostPort"],
            row["protocol"],
        ),
    )


def _gateway_attached_networks(value: object) -> list[str]:
    if not isinstance(value, dict) or not value:
        raise ValueError("gateway Docker network set is invalid")
    if any(
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or not isinstance(details, dict)
        for name, details in value.items()
    ):
        raise ValueError("gateway Docker network set is invalid")
    return sorted(value)


def _gateway_observation_round(
    *,
    docker: Path,
    docker_config: Path,
    candidate_root: Path,
    deadline_monotonic: float,
    runner: CommandRunner,
    runtime_containers: Mapping[str, str],
    expected_service_networks: Mapping[str, Sequence[str]],
) -> tuple[dict[str, str], list[dict[str, object]], list[dict[str, object]]]:
    prefix = [
        str(docker),
        "--config",
        str(docker_config),
        "--context",
        _GATEWAY_DOCKER_CONTEXT,
    ]
    environment = _gateway_child_environment()
    commands: list[dict[str, object]] = []

    def execute(arguments: list[str]) -> str:
        full_arguments = [*prefix, *arguments]
        remaining = deadline_monotonic - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ValueError("gateway Docker observation deadline expired")
        command_timeout = max(1, math.floor(remaining))
        try:
            completed = runner(
                full_arguments,
                cwd=candidate_root,
                env=environment,
                timeout=command_timeout,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise ValueError("gateway Docker observation command could not run") from exc
        stdout = _gateway_command_stdout(completed, arguments=full_arguments)
        commands.append(_gateway_command_record(arguments, stdout))
        return stdout

    def observe_daemon() -> dict[str, str]:
        try:
            endpoint = json.loads(execute(_GATEWAY_DOCKER_CONTEXT_ARGUMENTS))
            daemon = json.loads(execute(_GATEWAY_DOCKER_INFO_ARGUMENTS))
        except json.JSONDecodeError as exc:
            raise ValueError("gateway Docker daemon identity is invalid") from exc
        if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
            raise ValueError("gateway Docker daemon endpoint is invalid")
        if (
            not isinstance(daemon, dict)
            or set(daemon) != {"serverId", "osType"}
            or not isinstance(daemon.get("serverId"), str)
            or not daemon["serverId"]
            or daemon.get("osType") != "linux"
        ):
            raise ValueError("gateway Docker daemon identity is invalid")
        return {
            "context": _GATEWAY_DOCKER_CONTEXT,
            "endpoint": endpoint,
            "serverId": daemon["serverId"],
            "osType": daemon["osType"],
        }

    daemon_before = observe_daemon()

    ps_stdout = execute(_GATEWAY_DOCKER_PS_ARGUMENTS)
    try:
        observed_ids = [json.loads(line) for line in ps_stdout.splitlines()]
    except json.JSONDecodeError as exc:
        raise ValueError("gateway Docker observation commands are invalid") from exc
    expected_ids = sorted(runtime_containers)
    if observed_ids != expected_ids:
        raise ValueError("gateway Docker container identity is invalid")

    snapshot: list[dict[str, object]] = []
    for container_id in expected_ids:
        arguments = [
            "container",
            "inspect",
            "--format",
            _GATEWAY_DOCKER_INSPECT_FORMAT,
            container_id,
        ]
        try:
            row = json.loads(execute(arguments))
        except json.JSONDecodeError as exc:
            raise ValueError("gateway Docker container identity is invalid") from exc
        expected_service = runtime_containers[container_id]
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "containerId",
                "project",
                "service",
                "networkMode",
                "networks",
                "publishedPorts",
            }
            or row.get("containerId") != container_id
            or row.get("project") != _GATEWAY_DOCKER_PROJECT
            or row.get("service") != expected_service
        ):
            raise ValueError("gateway Docker container identity is invalid")
        expected_networks = expected_service_networks.get(expected_service)
        if not isinstance(expected_networks, Sequence) or isinstance(
            expected_networks, (str, bytes)
        ):
            raise ValueError("gateway candidate Compose network set is invalid")
        network_mode = row.get("networkMode")
        if not isinstance(network_mode, str) or network_mode not in expected_networks:
            raise ValueError("gateway Docker network mode is invalid")
        networks = _gateway_attached_networks(row.get("networks"))
        if networks != sorted(expected_networks):
            raise ValueError("gateway Docker network set is invalid")
        published_ports = _gateway_published_ports(row.get("publishedPorts"))
        expected_ports = (
            [
                {
                    "containerPort": port,
                    "hostIp": "0.0.0.0",
                    "hostPort": port,
                    "protocol": "tcp",
                }
                for port in (80, 443)
            ]
            if expected_service == "gateway"
            else []
        )
        if published_ports != expected_ports:
            raise ValueError("gateway Docker published ports are invalid")
        snapshot.append(
            {
                "containerId": container_id,
                "project": _GATEWAY_DOCKER_PROJECT,
                "service": expected_service,
                "networkMode": network_mode,
                "networks": networks,
                "publishedPorts": published_ports,
            }
        )
    daemon_after = observe_daemon()
    if daemon_after != daemon_before:
        raise ValueError("gateway Docker daemon identity changed during observation round")
    return (
        daemon_before,
        snapshot,
        commands,
    )


def _stage_gateway_json(
    parent: Path, document: Mapping[str, object]
) -> tuple[Path, bytes, tuple[int, int]]:
    gateway_boundary = _GATEWAY_STAGING_BOUNDARY.get()
    if gateway_boundary is not None:
        parent_path = Path(parent)
        if parent_path == gateway_boundary.root / "runtime":
            parent_key = "runtime"
        elif parent_path == gateway_boundary.root / "artifacts":
            parent_key = "artifacts"
        else:
            raise ValueError("gateway staging path is outside the held boundary")
        staged = parent_path / f".gateway-only-public-{uuid.uuid4().hex}.tmp"
        identity: tuple[int, int] | None = None
        handle: BinaryIO | None = None
        try:
            handle, identity, body = _create_classroom_json_staging(
                gateway_boundary,
                parent_key=parent_key,
                path=staged,
                document=document,
            )
            handle.close()
            handle = None
            return staged, body, identity
        except BaseException:
            if handle is not None:
                handle.close()
            try:
                _remove_classroom_entries(
                    gateway_boundary,
                    {staged: identity},
                    label="gateway staging evidence",
                )
            except Exception as cleanup_error:
                active_error = sys.exception()
                if active_error is not None:
                    active_error.add_note(f"gateway staging cleanup failed: {cleanup_error}")
            raise

    body = _json_bytes(document)
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".gateway-only-public-",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as handle:
            staged = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
            identity = _file_identity(os.fstat(handle.fileno()))
        return staged, body, identity
    except BaseException:
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise


def _stage_gateway_json_bound(
    boundary: _GatewayPublicationBoundary,
    parent: Path,
    document: Mapping[str, object],
) -> tuple[Path, bytes, tuple[int, int]]:
    token = _GATEWAY_STAGING_BOUNDARY.set(boundary)
    try:
        return _stage_gateway_json(parent, document)
    finally:
        _GATEWAY_STAGING_BOUNDARY.reset(token)


def _assert_gateway_published(
    path: Path,
    *,
    body: bytes,
    identity: tuple[int, int],
) -> None:
    pinned = _read_gateway_input(path, label="published evidence")
    if pinned.identity != identity or pinned.body != body:
        raise ValueError("gateway published evidence changed")


def _open_gateway_staged_input(
    boundary: _GatewayPublicationBoundary,
    path: Path,
    *,
    body: bytes,
    identity: tuple[int, int],
) -> BinaryIO:
    parent = _classroom_publication_parent(boundary, path)
    handle = _open_classroom_artifact_relative(parent, path.name)
    try:
        details = os.fstat(handle.fileno())
        staged_body = handle.read()
        handle.seek(0)
        if (
            not stat.S_ISREG(details.st_mode)
            or _file_identity(details) != identity
            or staged_body != body
        ):
            raise ValueError("gateway staged evidence changed")
        return handle
    except BaseException:
        handle.close()
        raise


def write_pass_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    release_run: Mapping[str, object],
    evidence: str,
    observed_at: str,
    native_exit: int,
    checks: Mapping[str, object],
) -> dict[str, object]:
    """Write one derived receipt; executable evidence must use a fresh probe."""
    if evidence not in _DIRECT_RECEIPT_EVIDENCE:
        raise ValueError("probe-backed evidence must be derived from an executed probe")
    return _write_pass_receipt_from_candidate(
        output_path,
        candidate=_candidate(candidate_root),
        release_run=release_run,
        evidence=evidence,
        observed_at=observed_at,
        native_exit=native_exit,
        checks=checks,
    )


def _write_pass_receipt_from_candidate(
    output_path: Path,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
    evidence: str,
    observed_at: str,
    native_exit: int,
    checks: Mapping[str, object],
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    document = _pass_receipt_from_candidate(
        candidate=candidate,
        release_run=release_run,
        evidence=evidence,
        observed_at=observed_at,
        native_exit=native_exit,
        checks=checks,
        provenance=provenance,
    )
    _atomic_write_json(Path(output_path), document)
    return document


def _pass_receipt_from_candidate(
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
    evidence: str,
    observed_at: str,
    native_exit: int,
    checks: Mapping[str, object],
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    bound_run = _release_run(release_run)
    if not _valid_observed_at(observed_at):
        raise ValueError("receipt observedAt is invalid")
    producer, bound_checks = _validate_pass_result(
        evidence,
        native_exit=native_exit,
        checks=checks,
    )
    document: dict[str, object] = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "candidate": json.loads(json.dumps(candidate)),
        "releaseRun": bound_run,
        "evidence": evidence,
        "receipt": {
            "producer": producer,
            "observedAt": observed_at,
            "result": {
                "outcome": "pass",
                "nativeExit": native_exit,
                "checks": bound_checks,
            },
        },
    }
    if provenance is not None:
        document["provenance"] = json.loads(json.dumps(provenance))
    return document


def _run_probe(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        check=False,
        timeout=timeout,
    )


def _run_platform_preflight_phase(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_buffer,
        tempfile.TemporaryFile(mode="w+b") as stderr_buffer,
    ):
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=env,
            stdout=stdout_buffer,
            stderr=stderr_buffer,
            check=False,
            timeout=timeout,
        )
        if any(
            os.fstat(buffer.fileno()).st_size > MAX_CANDIDATE_NETWORK_REPORT_BYTES
            for buffer in (stdout_buffer, stderr_buffer)
        ):
            raise subprocess.SubprocessError("platform preflight output is too large")
        stdout_buffer.seek(0)
        stderr_buffer.seek(0)
        return subprocess.CompletedProcess(
            arguments,
            completed.returncode,
            stdout_buffer.read().decode("utf-8", errors="strict"),
            stderr_buffer.read().decode("utf-8", errors="strict"),
        )


def _run_capacity_profile(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_buffer,
        tempfile.TemporaryFile(mode="w+b") as stderr_buffer,
    ):
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=env,
            stdout=stdout_buffer,
            stderr=stderr_buffer,
            check=False,
            timeout=timeout,
        )
        if (
            os.fstat(stdout_buffer.fileno()).st_size > MAX_CAPACITY_REPORT_BYTES
            or os.fstat(stderr_buffer.fileno()).st_size > _CAPACITY_STDERR_LIMIT_BYTES
        ):
            raise subprocess.SubprocessError("capacity profile output is too large")
        stdout_buffer.seek(0)
        stderr_buffer.seek(0)
        return subprocess.CompletedProcess(
            arguments,
            completed.returncode,
            stdout_buffer.read().decode("utf-8", errors="strict"),
            stderr_buffer.read().decode("utf-8", errors="strict"),
        )


def _run_tenant_isolation(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_buffer,
        tempfile.TemporaryFile(mode="w+b") as stderr_buffer,
    ):
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=env,
            stdout=stdout_buffer,
            stderr=stderr_buffer,
            check=False,
            timeout=timeout,
        )
        if (
            os.fstat(stdout_buffer.fileno()).st_size > MAX_TENANT_ISOLATION_REPORT_BYTES
            or os.fstat(stderr_buffer.fileno()).st_size > _TENANT_ISOLATION_STDERR_LIMIT_BYTES
        ):
            raise subprocess.SubprocessError("tenant isolation output is too large")
        stdout_buffer.seek(0)
        stderr_buffer.seek(0)
        return subprocess.CompletedProcess(
            arguments,
            completed.returncode,
            stdout_buffer.read().decode("utf-8", errors="strict"),
            stderr_buffer.read().decode("utf-8", errors="strict"),
        )


def _run_openmaic_smoke(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_buffer,
        tempfile.TemporaryFile(mode="w+b") as stderr_buffer,
    ):
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=env,
            stdout=stdout_buffer,
            stderr=stderr_buffer,
            check=False,
            timeout=timeout,
        )
        if (
            os.fstat(stdout_buffer.fileno()).st_size > MAX_OPENMAIC_SMOKE_REPORT_BYTES
            or os.fstat(stderr_buffer.fileno()).st_size > _OPENMAIC_SMOKE_STDERR_LIMIT_BYTES
        ):
            raise subprocess.SubprocessError("OpenMAIC smoke output is too large")
        stdout_buffer.seek(0)
        stderr_buffer.seek(0)
        return subprocess.CompletedProcess(
            arguments,
            completed.returncode,
            stdout_buffer.read().decode("utf-8", errors="strict"),
            stderr_buffer.read().decode("utf-8", errors="strict"),
        )


def _run_classroom_exports(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_buffer,
        tempfile.TemporaryFile(mode="w+b") as stderr_buffer,
    ):
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=env,
            stdout=stdout_buffer,
            stderr=stderr_buffer,
            check=False,
            timeout=timeout,
        )
        if (
            os.fstat(stdout_buffer.fileno()).st_size > MAX_CLASSROOM_EXPORT_REPORT_BYTES
            or os.fstat(stderr_buffer.fileno()).st_size > _CLASSROOM_EXPORT_STDERR_LIMIT_BYTES
        ):
            raise subprocess.SubprocessError("classroom exports output is too large")
        stdout_buffer.seek(0)
        stderr_buffer.seek(0)
        return subprocess.CompletedProcess(
            arguments,
            completed.returncode,
            stdout_buffer.read().decode("utf-8", errors="strict"),
            stderr_buffer.read().decode("utf-8", errors="strict"),
        )


@dataclass(slots=True)
class _ClassroomArtifactLease:
    kind: str
    name: str
    path: Path
    handle: Any
    identity: tuple[int, int]
    size: int
    sha256: str


def _classroom_file_digest(handle: Any) -> tuple[int, str]:
    handle.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    handle.seek(0)
    return size, digest.hexdigest()


def _assert_classroom_artifact_lease(lease: _ClassroomArtifactLease) -> None:
    try:
        path_details = os.stat(lease.path, follow_symlinks=False)
        opened_details = os.fstat(lease.handle.fileno())
    except OSError as exc:
        raise ValueError("classroom export staged artifact is unavailable") from exc
    size, sha256 = _classroom_file_digest(lease.handle)
    if (
        not stat.S_ISREG(path_details.st_mode)
        or not stat.S_ISREG(opened_details.st_mode)
        or _file_identity(path_details) != lease.identity
        or _file_identity(opened_details) != lease.identity
        or path_details.st_size != lease.size
        or opened_details.st_size != lease.size
        or size != lease.size
        or sha256 != lease.sha256
    ):
        raise ValueError("classroom export staged artifact changed")


def _classroom_artifact_contains_secret(
    lease: _ClassroomArtifactLease,
    secrets: set[bytes],
) -> bool:
    if not secrets:
        return False
    overlap = max(len(secret) for secret in secrets) - 1
    lease.handle.seek(0)
    previous = b""
    try:
        while chunk := lease.handle.read(1024 * 1024):
            window = previous + chunk
            if any(secret in window for secret in secrets):
                return True
            previous = window[-overlap:] if overlap > 0 else b""
        return classroom_export_archive_contains_forbidden_bytes(
            lease.handle,
            kind=lease.kind,
            forbidden=secrets,
        )
    finally:
        lease.handle.seek(0)


def _open_classroom_artifact_relative(
    parent: _ClassroomDirectoryLease,
    name: str,
) -> BinaryIO:
    if Path(name).name != name or not name:
        raise ValueError("classroom export staged artifact name is invalid")
    if os.name == "nt":
        import msvcrt

        native_handle, _native_identity = _open_windows_regular_file_relative(
            parent.handle,
            name,
            share_access=0x00000001 | 0x00000002 | 0x00000004,
            deletable=True,
        )
        descriptor: int | None = None
        try:
            handle_value = getattr(native_handle, "value", native_handle)
            descriptor = msvcrt.open_osfhandle(
                int(handle_value),
                os.O_RDONLY | os.O_BINARY,
            )
            return os.fdopen(descriptor, "rb")
        except BaseException:
            if descriptor is None:
                _close_windows_handle(native_handle)
            else:
                os.close(descriptor)
            raise

    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=int(parent.handle))
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _open_classroom_artifact_leases(
    boundary: _ClassroomPublicationBoundary,
) -> dict[str, _ClassroomArtifactLease]:
    staging = boundary.staging
    if staging is None:
        raise ValueError("classroom export staging is unavailable")
    staging_parent = boundary.leases["staging/attempt"]
    expected_names = set(CLASSROOM_EXPORT_PATHS.values())
    boundary.assert_unchanged()
    try:
        entries = {entry.name: entry for entry in os.scandir(staging)}
    except OSError as exc:
        raise ValueError("classroom export staging is unavailable") from exc
    boundary.assert_unchanged()
    if set(entries) != expected_names:
        raise ValueError("classroom export staging must contain exactly four artifacts")
    leases: dict[str, _ClassroomArtifactLease] = {}
    total_size = 0
    try:
        for kind, name in CLASSROOM_EXPORT_PATHS.items():
            path = staging / name
            handle = _open_classroom_artifact_relative(staging_parent, name)
            try:
                opened = os.fstat(handle.fileno())
            except BaseException:
                handle.close()
                raise
            if not stat.S_ISREG(opened.st_mode):
                handle.close()
                raise ValueError("classroom export staged artifact must be a regular file")
            if opened.st_size <= 0 or opened.st_size > MAX_EXPORT_BYTES[kind]:
                handle.close()
                raise ValueError("classroom export staged artifact size is invalid")
            total_size += opened.st_size
            if total_size > MAX_TOTAL_EXPORT_BYTES:
                handle.close()
                raise ValueError("classroom export staged artifact set is too large")
            size, sha256 = _classroom_file_digest(handle)
            if size != opened.st_size:
                handle.close()
                raise ValueError("classroom export staged artifact identity changed")
            lease = _ClassroomArtifactLease(
                kind=kind,
                name=name,
                path=path,
                handle=handle,
                identity=_file_identity(opened),
                size=size,
                sha256=sha256,
            )
            leases[kind] = lease
            _assert_classroom_artifact_lease(lease)
        boundary.assert_unchanged()
        return leases
    except BaseException:
        for lease in leases.values():
            lease.handle.close()
        raise


def _assert_published_classroom_artifact(
    lease: _ClassroomArtifactLease,
    target: Path,
) -> None:
    try:
        details = os.stat(target, follow_symlinks=False)
        with target.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            size, sha256 = _classroom_file_digest(handle)
    except OSError as exc:
        raise ValueError("published classroom export artifact is unavailable") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or _file_identity(details) != lease.identity
        or _file_identity(opened) != lease.identity
        or size != lease.size
        or sha256 != lease.sha256
    ):
        raise ValueError("published classroom export artifact changed")


@dataclass(slots=True)
class _ClassroomDirectoryLease:
    path: Path
    handle: object | int
    identity: tuple[int, int]


def _assert_classroom_fixed_parents(root: Path) -> None:
    _assert_no_link_ancestors(root)
    try:
        root_details = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("classroom export bundle boundary is unavailable") from exc
    if not stat.S_ISDIR(root_details.st_mode) or _is_link_or_reparse(root):
        raise ValueError("classroom export bundle boundary is not a plain directory")
    for name in ("runtime", "artifacts", "raw", "staging"):
        path = root / name
        if not os.path.lexists(path):
            continue
        try:
            details = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("classroom export publication boundary is unavailable") from exc
        if not stat.S_ISDIR(details.st_mode) or _is_link_or_reparse(path):
            raise ValueError(
                "classroom export publication boundary uses a symlink, junction, or reparse point"
            )


def _open_classroom_directory_relative(
    parent: _ClassroomDirectoryLease,
    *,
    name: str,
    path: Path,
    create: bool,
) -> _ClassroomDirectoryLease:
    handle: object | int | None = None
    try:
        if os.name == "nt":
            if create:
                created, created_identity = _create_windows_directory_relative(
                    parent.handle,
                    name,
                )
                _close_windows_handle(created)
                handle, identity = _open_windows_directory_relative(parent.handle, name)
                if identity != created_identity:
                    raise ValueError(
                        "classroom export publication directory identity changed during creation"
                    )
            else:
                handle, identity = _open_windows_directory_relative(parent.handle, name)
        else:
            parent_fd = int(parent.handle)
            if create:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            handle = os.open(name, flags, dir_fd=parent_fd)
            details = os.fstat(handle)
            if not stat.S_ISDIR(details.st_mode):
                raise ValueError("classroom export publication boundary is not a directory")
            identity = _file_identity(details)
        return _ClassroomDirectoryLease(path=path, handle=handle, identity=identity)
    except (OSError, ValueError) as exc:
        if handle is not None:
            if os.name == "nt":
                _close_windows_handle(handle)
            else:
                os.close(int(handle))
        action = "created" if create else "opened"
        raise ValueError(f"classroom export publication directory cannot be {action}") from exc


class _ClassroomPublicationBoundary:
    _FIXED_NAMES = ("runtime", "artifacts", "raw", "staging")

    def __init__(self, root: Path) -> None:
        self.root = root
        self.leases: dict[str, _ClassroomDirectoryLease] = {}
        self.staging: Path | None = None
        self.raw_root: Path | None = None

    @classmethod
    def open(cls, root: Path, attempt_id: str) -> _ClassroomPublicationBoundary:
        boundary = cls(Path(os.path.abspath(root)))
        try:
            _assert_classroom_fixed_parents(boundary.root)
            if os.name == "nt":
                root_handle, root_identity = _open_windows_directory_handle(
                    boundary.root,
                    deletable=True,
                )
            else:
                root_handle, root_identity = _open_posix_directory_path_no_follow(boundary.root)
            boundary.leases["bundle"] = _ClassroomDirectoryLease(
                path=boundary.root,
                handle=root_handle,
                identity=root_identity,
            )

            existing = {name: os.path.lexists(boundary.root / name) for name in cls._FIXED_NAMES}
            _assert_classroom_fixed_parents(boundary.root)
            for name in cls._FIXED_NAMES:
                boundary.leases[name] = _open_classroom_directory_relative(
                    boundary.leases["bundle"],
                    name=name,
                    path=boundary.root / name,
                    create=not existing[name],
                )

            raw_root = boundary.root / "raw" / "classroom-exports"
            if os.path.lexists(raw_root):
                if _is_link_or_reparse(raw_root):
                    raise ValueError("classroom exports raw boundary is redirected")
                raw_details = os.stat(raw_root, follow_symlinks=False)
                if not stat.S_ISDIR(raw_details.st_mode):
                    raise ValueError("classroom exports raw boundary is invalid")
                with os.scandir(raw_root) as entries:
                    is_empty = next(entries, None) is None
                if not is_empty:
                    raise ValueError("classroom exports raw boundary is invalid")
                create_raw_root = False
            else:
                create_raw_root = True
            boundary.leases["raw/classroom-exports"] = _open_classroom_directory_relative(
                boundary.leases["raw"],
                name="classroom-exports",
                path=raw_root,
                create=create_raw_root,
            )
            boundary.raw_root = raw_root

            staging_name = f"classroom-exports-{attempt_id}"
            staging = boundary.root / "staging" / staging_name
            if os.path.lexists(staging):
                raise ValueError("classroom exports staging already exists")
            boundary.leases["staging/attempt"] = _open_classroom_directory_relative(
                boundary.leases["staging"],
                name=staging_name,
                path=staging,
                create=True,
            )
            boundary.staging = staging
            boundary.assert_unchanged()
            return boundary
        except BaseException:
            boundary.close()
            raise

    def assert_unchanged(self) -> None:
        relative_parents = {
            "runtime": ("bundle", "runtime"),
            "artifacts": ("bundle", "artifacts"),
            "raw": ("bundle", "raw"),
            "staging": ("bundle", "staging"),
            "raw/classroom-exports": ("raw", "classroom-exports"),
        }
        for key, lease in self.leases.items():
            reopened: object | int | None = None
            try:
                if os.name == "nt":
                    if key == "bundle":
                        reopened, identity = _open_windows_directory_handle(
                            lease.path,
                            deletable=True,
                        )
                    else:
                        parent_key, name = relative_parents.get(
                            key,
                            ("staging", lease.path.name),
                        )
                        reopened, identity = _open_windows_directory_relative(
                            self.leases[parent_key].handle,
                            name,
                        )
                else:
                    details = os.fstat(int(lease.handle))
                    if not stat.S_ISDIR(details.st_mode):
                        raise ValueError("classroom export publication directory changed")
                    if _file_identity(details) != lease.identity:
                        raise ValueError("classroom export publication directory changed")
                    if key == "bundle":
                        reopened, identity = _open_posix_directory_path_no_follow(lease.path)
                    else:
                        parent_key, name = relative_parents.get(
                            key,
                            ("staging", lease.path.name),
                        )
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                        reopened = os.open(
                            name,
                            flags,
                            dir_fd=int(self.leases[parent_key].handle),
                        )
                        reopened_details = os.fstat(reopened)
                        identity = _file_identity(reopened_details)
                if identity != lease.identity:
                    raise ValueError("classroom export publication directory changed")
            except (OSError, ValueError) as exc:
                raise ValueError("classroom export publication directory changed") from exc
            finally:
                if reopened is not None:
                    if os.name == "nt":
                        _close_windows_handle(reopened)
                    else:
                        os.close(int(reopened))

    def close(self) -> None:
        for lease in reversed(tuple(self.leases.values())):
            try:
                if os.name == "nt":
                    _close_windows_handle(lease.handle)
                else:
                    os.close(int(lease.handle))
            except OSError:
                pass
        self.leases.clear()


class _GatewayPublicationBoundary:
    def __init__(self, root: Path, candidate_root: Path) -> None:
        self.root = Path(root)
        self.candidate_root = Path(candidate_root)
        self.leases: dict[str, _ClassroomDirectoryLease] = {}
        self.staging: Path | None = None
        self._committed = False

    @staticmethod
    def _open_root(path: Path) -> _ClassroomDirectoryLease:
        if os.name == "nt":
            if not path.is_absolute() or not path.anchor:
                raise ValueError("gateway directory lease path is invalid")
            handle, identity = _open_windows_directory_handle(
                Path(path.anchor),
                deletable=True,
            )
            try:
                for component in path.relative_to(path.anchor).parts:
                    opened, opened_identity = _open_windows_directory_relative(
                        handle,
                        component,
                    )
                    previous = handle
                    handle = opened
                    identity = opened_identity
                    _close_windows_handle(previous)
            except BaseException:
                _close_windows_handle(handle)
                raise
        else:
            handle, identity = _open_posix_directory_path_no_follow(path)
        return _ClassroomDirectoryLease(path=path, handle=handle, identity=identity)

    @classmethod
    def open(
        cls,
        root: Path,
        candidate_root: Path,
    ) -> _GatewayPublicationBoundary:
        boundary = cls(
            Path(os.path.abspath(root)),
            Path(os.path.abspath(candidate_root)),
        )
        try:
            boundary.leases["bundle"] = cls._open_root(boundary.root)
            boundary.leases["candidate"] = cls._open_root(boundary.candidate_root)
            for key, parent_key, name, path in (
                ("raw", "bundle", "raw", boundary.root / "raw"),
                ("runtime", "bundle", "runtime", boundary.root / "runtime"),
                (
                    "candidate/deploy",
                    "candidate",
                    "deploy",
                    boundary.candidate_root / "deploy",
                ),
            ):
                try:
                    boundary.leases[key] = _open_classroom_directory_relative(
                        boundary.leases[parent_key],
                        name=name,
                        path=path,
                        create=False,
                    )
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        "gateway no-follow directory boundary cannot be opened"
                    ) from exc
            boundary.assert_unchanged()
            return boundary
        except BaseException:
            boundary.close(suppress_errors=True)
            raise

    def __enter__(self) -> _GatewayPublicationBoundary:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.close()
        except BaseException as close_error:
            if exc_value is None and not self._committed:
                raise
            if exc_value is not None and hasattr(exc_value, "add_note"):
                exc_value.add_note(f"gateway directory lease cleanup failed: {close_error}")
        return False

    def mark_committed(self) -> None:
        self._committed = True

    def ensure_publication_parents(self) -> None:
        if "artifacts" in self.leases:
            self.assert_unchanged()
            return
        path = self.root / "artifacts"
        exists = os.path.lexists(path)
        if exists:
            try:
                details = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise ValueError("gateway publication boundary is unavailable") from exc
            if not stat.S_ISDIR(details.st_mode) or _is_link_or_reparse(path):
                raise ValueError("gateway publication boundary uses a reparse point")
        try:
            self.leases["artifacts"] = _open_classroom_directory_relative(
                self.leases["bundle"],
                name="artifacts",
                path=path,
                create=not exists,
            )
            self.assert_unchanged()
        except BaseException:
            lease = self.leases.pop("artifacts", None)
            if lease is not None:
                if os.name == "nt":
                    _close_windows_handle(lease.handle)
                else:
                    os.close(int(lease.handle))
            raise

    def assert_unchanged(self) -> None:
        relative_parents = {
            "raw": ("bundle", "raw"),
            "runtime": ("bundle", "runtime"),
            "artifacts": ("bundle", "artifacts"),
            "candidate/deploy": ("candidate", "deploy"),
        }
        for key, lease in self.leases.items():
            reopened: object | int | None = None
            try:
                if os.name == "nt":
                    if key in {"bundle", "candidate"}:
                        reopened_lease = self._open_root(lease.path)
                        reopened = reopened_lease.handle
                        identity = reopened_lease.identity
                    else:
                        parent_key, name = relative_parents[key]
                        reopened, identity = _open_windows_directory_relative(
                            self.leases[parent_key].handle,
                            name,
                        )
                else:
                    held_details = os.fstat(int(lease.handle))
                    if (
                        not stat.S_ISDIR(held_details.st_mode)
                        or _file_identity(held_details) != lease.identity
                    ):
                        raise ValueError("gateway directory lease changed")
                    if key in {"bundle", "candidate"}:
                        reopened, identity = _open_posix_directory_path_no_follow(lease.path)
                    else:
                        parent_key, name = relative_parents[key]
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                        reopened = os.open(
                            name,
                            flags,
                            dir_fd=int(self.leases[parent_key].handle),
                        )
                        details = os.fstat(reopened)
                        if not stat.S_ISDIR(details.st_mode):
                            raise ValueError("gateway directory lease changed")
                        identity = _file_identity(details)
                if identity != lease.identity:
                    raise ValueError("gateway directory lease changed")
            except (OSError, ValueError, KeyError) as exc:
                raise ValueError("gateway directory boundary changed") from exc
            finally:
                if reopened is not None:
                    if os.name == "nt":
                        _close_windows_handle(reopened)
                    else:
                        os.close(int(reopened))

    def close(self, *, suppress_errors: bool = False) -> None:
        errors: list[OSError] = []
        for lease in reversed(tuple(self.leases.values())):
            try:
                if os.name == "nt":
                    _close_windows_handle(lease.handle)
                else:
                    os.close(int(lease.handle))
            except OSError as exc:
                errors.append(exc)
        self.leases.clear()
        if errors and not suppress_errors:
            raise errors[0]


class _BackupRestorePublicationBoundary(_ClassroomPublicationBoundary):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._committed = False

    @classmethod
    def open(cls, root: Path) -> _BackupRestorePublicationBoundary:
        boundary = cls(Path(os.path.abspath(root)))
        try:
            boundary.leases["bundle"] = _GatewayPublicationBoundary._open_root(boundary.root)
            for key, parent_key, name, path, create in (
                (
                    "runtime",
                    "bundle",
                    "runtime",
                    boundary.root / "runtime",
                    False,
                ),
                (
                    "runtime/backup-restore",
                    "runtime",
                    "backup-restore",
                    boundary.root / "runtime" / "backup-restore",
                    False,
                ),
                (
                    "artifacts",
                    "bundle",
                    "artifacts",
                    boundary.root / "artifacts",
                    not os.path.lexists(boundary.root / "artifacts"),
                ),
            ):
                boundary.leases[key] = _open_classroom_directory_relative(
                    boundary.leases[parent_key],
                    name=name,
                    path=path,
                    create=create,
                )
            boundary.assert_unchanged()
            return boundary
        except BaseException:
            boundary.close()
            raise

    def assert_unchanged(self) -> None:
        relative_parents = {
            "runtime": ("bundle", "runtime"),
            "runtime/backup-restore": ("runtime", "backup-restore"),
            "artifacts": ("bundle", "artifacts"),
        }
        for key, lease in self.leases.items():
            reopened: _ClassroomDirectoryLease | object | int | None = None
            try:
                held_identity = (
                    _windows_handle_identity(lease.handle, directory=True)
                    if os.name == "nt"
                    else _file_identity(os.fstat(int(lease.handle)))
                )
                if held_identity != lease.identity:
                    raise ValueError("backup restore publication directory changed")
                if key == "bundle":
                    reopened = _GatewayPublicationBoundary._open_root(lease.path)
                    identity = reopened.identity
                else:
                    parent_key, name = relative_parents[key]
                    if os.name == "nt":
                        reopened, identity = _open_windows_directory_relative(
                            self.leases[parent_key].handle,
                            name,
                        )
                    else:
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                        reopened = os.open(
                            name,
                            flags,
                            dir_fd=int(self.leases[parent_key].handle),
                        )
                        details = os.fstat(reopened)
                        if not stat.S_ISDIR(details.st_mode):
                            raise ValueError("backup restore publication directory changed")
                        identity = _file_identity(details)
                if identity != lease.identity:
                    raise ValueError("backup restore publication directory changed")
            except (OSError, ValueError, KeyError) as exc:
                raise ValueError("backup restore publication directory changed") from exc
            finally:
                if isinstance(reopened, _ClassroomDirectoryLease):
                    if os.name == "nt":
                        _close_windows_handle(reopened.handle)
                    else:
                        os.close(int(reopened.handle))
                elif reopened is not None:
                    if os.name == "nt":
                        _close_windows_handle(reopened)
                    else:
                        os.close(int(reopened))

    def mark_committed(self) -> None:
        self._committed = True

    @property
    def committed(self) -> bool:
        return self._committed


class _TenantPublicationBoundary(_ClassroomPublicationBoundary):
    _FIXED_NAMES = ("runtime", "artifacts", "staging")

    @classmethod
    def open(
        cls,
        root: Path,
        attempt_id: str,
        *,
        staging_prefix: str = "tenant-isolation",
    ) -> _TenantPublicationBoundary:
        if (
            not staging_prefix
            or Path(staging_prefix).name != staging_prefix
            or not all(
                character.isascii() and (character.isalnum() or character == "-")
                for character in staging_prefix
            )
        ):
            raise ValueError("release evidence staging prefix is invalid")
        boundary = cls(Path(os.path.abspath(root)))
        try:
            _assert_classroom_fixed_parents(boundary.root)
            if os.name == "nt":
                root_handle, root_identity = _open_windows_directory_handle(
                    boundary.root,
                    deletable=True,
                )
            else:
                root_handle, root_identity = _open_posix_directory_path_no_follow(boundary.root)
            boundary.leases["bundle"] = _ClassroomDirectoryLease(
                path=boundary.root,
                handle=root_handle,
                identity=root_identity,
            )

            existing = {name: os.path.lexists(boundary.root / name) for name in cls._FIXED_NAMES}
            _assert_classroom_fixed_parents(boundary.root)
            for name in cls._FIXED_NAMES:
                boundary.leases[name] = _open_classroom_directory_relative(
                    boundary.leases["bundle"],
                    name=name,
                    path=boundary.root / name,
                    create=not existing[name],
                )

            staging_name = f"{staging_prefix}-{attempt_id}"
            staging = boundary.root / "staging" / staging_name
            if os.path.lexists(staging):
                raise ValueError("release evidence staging already exists")
            boundary.leases["staging/attempt"] = _open_classroom_directory_relative(
                boundary.leases["staging"],
                name=staging_name,
                path=staging,
                create=True,
            )
            boundary.staging = staging
            boundary.assert_unchanged()
            return boundary
        except BaseException:
            boundary.close()
            raise


def _create_classroom_json_staging(
    boundary: _ClassroomPublicationBoundary,
    *,
    parent_key: str,
    path: Path,
    document: Mapping[str, object],
) -> tuple[BinaryIO, tuple[int, int], bytes]:
    parent = boundary.leases[parent_key]
    if path.parent != parent.path or Path(path.name).name != path.name or not path.name:
        raise ValueError("classroom export staging path is invalid")
    body = _json_bytes(document)
    if os.name == "nt":
        import msvcrt

        native_handle, _native_identity = _create_windows_staging_file(
            parent.handle,
            path.name,
            body,
        )
        descriptor: int | None = None
        try:
            handle_value = getattr(native_handle, "value", native_handle)
            descriptor = msvcrt.open_osfhandle(
                int(handle_value),
                os.O_RDONLY | os.O_BINARY,
            )
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            if descriptor is None:
                _close_windows_handle(native_handle)
            else:
                os.close(descriptor)
            raise
    else:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(path.name, flags, 0o600, dir_fd=int(parent.handle))
        try:
            offset = 0
            while offset < len(body):
                offset += os.write(descriptor, body[offset:])
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
    try:
        details = os.fstat(handle.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("classroom export staging file is invalid")
        identity = _file_identity(details)
        handle.seek(0)
    except BaseException:
        handle.close()
        raise
    return handle, identity, body


def _link_windows_file_relative(
    file_handle: object,
    directory_handle: object,
    target_name: str,
) -> None:
    import ctypes
    from ctypes import wintypes

    if Path(target_name).name != target_name or not target_name:
        raise ValueError("classroom export publication target name is invalid")

    class _FileLinkInformation(ctypes.Structure):
        _fields_ = (
            ("replaceIfExists", wintypes.BOOLEAN),
            ("rootDirectory", wintypes.HANDLE),
            ("fileNameLength", wintypes.DWORD),
            ("fileName", wintypes.WCHAR * len(target_name)),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (("status", ctypes.c_void_p), ("information", ctypes.c_size_t))

    information = _FileLinkInformation()
    information.replaceIfExists = False
    information.rootDirectory = directory_handle
    information.fileNameLength = len(target_name.encode("utf-16-le"))
    information.fileName = target_name
    io_status = _IoStatusBlock()
    ntdll = ctypes.WinDLL("ntdll")
    set_information = ntdll.NtSetInformationFile
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
    )
    set_information.restype = wintypes.LONG
    status = set_information(
        file_handle,
        ctypes.byref(io_status),
        ctypes.byref(information),
        _FileLinkInformation.fileName.offset + information.fileNameLength,
        11,
    )
    if status < 0:
        status_to_error = ntdll.RtlNtStatusToDosError
        status_to_error.argtypes = (wintypes.LONG,)
        status_to_error.restype = wintypes.ULONG
        error = status_to_error(status)
        raise OSError(error, f"cannot publish classroom export as {target_name}")


def _link_posix_file_descriptor(
    file_descriptor: int,
    directory_descriptor: int,
    target_name: str,
) -> None:
    import ctypes

    if Path(target_name).name != target_name or not target_name:
        raise ValueError("classroom export publication target name is invalid")
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "descriptor-relative hard links require Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    linkat.restype = ctypes.c_int
    encoded_target = os.fsencode(target_name)
    if linkat(file_descriptor, b"", directory_descriptor, encoded_target, 0x1000) == 0:
        return
    first_error = ctypes.get_errno()
    if first_error not in {errno.EINVAL, errno.ENOENT, errno.EPERM}:
        raise OSError(first_error, f"cannot publish classroom export as {target_name}")
    proc_source = os.fsencode(f"/proc/self/fd/{file_descriptor}")
    if linkat(-100, proc_source, directory_descriptor, encoded_target, 0x400) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"cannot publish classroom export as {target_name}")


def _classroom_publication_parent(
    boundary: _ClassroomPublicationBoundary,
    path: Path,
) -> _ClassroomDirectoryLease:
    parents = {
        boundary.root / "runtime": "runtime",
        boundary.root / "artifacts": "artifacts",
    }
    if boundary.staging is not None and "staging/attempt" in boundary.leases:
        parents[boundary.staging] = "staging/attempt"
    if "raw/classroom-exports" in boundary.leases:
        parents[boundary.root / "raw" / "classroom-exports"] = "raw/classroom-exports"
    key = parents.get(path.parent)
    if key is None:
        raise ValueError("classroom export publication path is invalid")
    return boundary.leases[key]


def _publish_classroom_no_replace(
    boundary: _ClassroomPublicationBoundary,
    source: Path,
    target: Path,
    *,
    source_handle: BinaryIO,
) -> None:
    source_parent = _classroom_publication_parent(boundary, source)
    target_parent = _classroom_publication_parent(boundary, target)
    if os.name == "nt":
        import msvcrt

        _link_windows_file_relative(
            msvcrt.get_osfhandle(source_handle.fileno()),
            target_parent.handle,
            target.name,
        )
    else:
        del source_parent
        _link_posix_file_descriptor(
            source_handle.fileno(),
            int(target_parent.handle),
            target.name,
        )


def _assert_published_classroom_receipt(
    path: Path,
    *,
    expected_body: bytes,
    expected_identity: tuple[int, int] | None = None,
    label: str = "receipt",
) -> tuple[int, int]:
    message = f"published classroom exports {label} changed"
    try:
        details = os.stat(path, follow_symlinks=False)
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            body = handle.read()
    except OSError as exc:
        raise ValueError(message) from exc
    identity = _file_identity(opened)
    if (
        not stat.S_ISREG(details.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or _file_identity(details) != identity
        or (expected_identity is not None and identity != expected_identity)
        or body != expected_body
    ):
        raise ValueError(message)
    return identity


def _assert_published_classroom_receipt_relative(
    boundary: _ClassroomPublicationBoundary,
    path: Path,
    *,
    expected_body: bytes,
    expected_identity: tuple[int, int],
    label: str,
) -> None:
    message = f"published classroom exports {label} changed"
    parent = _classroom_publication_parent(boundary, path)
    descriptor: int | None = None
    native_handle: object | None = None
    try:
        if os.name == "nt":
            import msvcrt

            native_handle, _native_identity = _open_windows_regular_file_relative(
                parent.handle,
                path.name,
                share_access=0x00000001 | 0x00000002 | 0x00000004,
            )
            handle_value = getattr(native_handle, "value", native_handle)
            descriptor = msvcrt.open_osfhandle(
                int(handle_value),
                os.O_RDONLY | os.O_BINARY,
            )
            native_handle = None
        else:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=int(parent.handle),
            )
        details = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            body = handle.read()
        if (
            not stat.S_ISREG(details.st_mode)
            or _file_identity(details) != expected_identity
            or body != expected_body
        ):
            raise ValueError(message)
    except OSError as exc:
        raise ValueError(message) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if native_handle is not None:
            _close_windows_handle(native_handle)


def _remove_classroom_entries(
    boundary: _ClassroomPublicationBoundary,
    entries: Mapping[Path, tuple[int, int] | None],
    *,
    label: str,
) -> None:
    parent_leases = {
        boundary.root / "runtime": boundary.leases["runtime"],
        boundary.root / "artifacts": boundary.leases["artifacts"],
    }
    if boundary.staging is not None and "staging/attempt" in boundary.leases:
        parent_leases[boundary.staging] = boundary.leases["staging/attempt"]
    if "raw/classroom-exports" in boundary.leases:
        parent_leases[boundary.root / "raw" / "classroom-exports"] = boundary.leases[
            "raw/classroom-exports"
        ]
    failures: list[Exception] = []
    for path, expected_identity in entries.items():
        try:
            parent = parent_leases.get(path.parent)
            if parent is None or Path(path.name).name != path.name or not path.name:
                raise ValueError(f"{label} path is invalid")
            if os.name == "nt":
                import msvcrt

                descriptor: int | None = None
                try:
                    handle, _native_identity = _open_windows_regular_file_relative(
                        parent.handle,
                        path.name,
                        share_access=0x00000001 | 0x00000002 | 0x00000004,
                        deletable=True,
                    )
                except FileNotFoundError:
                    continue
                try:
                    handle_value = getattr(handle, "value", handle)
                    descriptor = msvcrt.open_osfhandle(
                        int(handle_value),
                        os.O_RDONLY | os.O_BINARY,
                    )
                    identity = _file_identity(os.fstat(descriptor))
                    if expected_identity is not None and identity != expected_identity:
                        raise ValueError(f"{label} identity changed")
                    _delete_windows_file_on_close(msvcrt.get_osfhandle(descriptor))
                finally:
                    if descriptor is None:
                        _close_windows_handle(handle)
                    else:
                        os.close(descriptor)
            else:
                try:
                    details = os.stat(
                        path.name,
                        dir_fd=int(parent.handle),
                        follow_symlinks=False,
                    )
                    identity = _file_identity(details)
                    if not stat.S_ISREG(details.st_mode) or (
                        expected_identity is not None and identity != expected_identity
                    ):
                        raise ValueError(f"{label} identity changed")
                    os.unlink(path.name, dir_fd=int(parent.handle))
                except FileNotFoundError:
                    continue
        except (OSError, ValueError) as exc:
            failures.append(exc)
    remaining: list[Path] = []
    for path, expected_identity in entries.items():
        parent = parent_leases.get(path.parent)
        if parent is None:
            continue
        try:
            if os.name == "nt":
                import msvcrt

                handle, _native_identity = _open_windows_regular_file_relative(
                    parent.handle,
                    path.name,
                    share_access=0x00000001 | 0x00000002 | 0x00000004,
                )
                descriptor = None
                try:
                    handle_value = getattr(handle, "value", handle)
                    descriptor = msvcrt.open_osfhandle(
                        int(handle_value),
                        os.O_RDONLY | os.O_BINARY,
                    )
                    identity = _file_identity(os.fstat(descriptor))
                finally:
                    if descriptor is None:
                        _close_windows_handle(handle)
                    else:
                        os.close(descriptor)
            else:
                details = os.stat(
                    path.name,
                    dir_fd=int(parent.handle),
                    follow_symlinks=False,
                )
                identity = _file_identity(details)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            failures.append(exc)
        else:
            if expected_identity is None or identity == expected_identity:
                remaining.append(path)
            else:
                failures.append(ValueError(f"{label} identity changed"))
    if remaining or failures:
        raise RuntimeError(f"{label} could not be removed") from (failures[0] if failures else None)


def _retract_classroom_formal_entries(
    boundary: _ClassroomPublicationBoundary,
    entries: Mapping[Path, tuple[int, int]],
) -> None:
    _remove_classroom_entries(
        boundary,
        entries,
        label="classroom exports formal evidence",
    )


def _current_observed_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bundle_artifact(path: Path, *, bundle_root: Path, label: str) -> tuple[Path, str]:
    resolved_root = Path(bundle_root).resolve()
    resolved_path = Path(path).resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside the evidence bundle") from exc
    if not relative.parts:
        raise ValueError(f"{label} is invalid")
    return resolved_path, relative.as_posix()


def _probe_report(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    evidence: str,
) -> tuple[dict[str, object], dict[str, bool]]:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("probe raw report is invalid") from exc
    if not isinstance(document, dict):
        raise ValueError("probe raw report is invalid")
    checks = derive_probe_checks(
        evidence,
        raw_report=body,
        candidate=candidate,
        release_run=release_run,
    )
    return document, checks


def _probe_command(evidence: str, recipe: str) -> list[str]:
    registered = PROBE_RECIPES.get(evidence)
    if registered is None or recipe != registered[0]:
        raise ValueError("probe recipe is invalid")
    return [
        sys.executable,
        str(SCRIPTS_ROOT / "classroom_release_probe.py"),
        evidence,
    ]


def _failure_archive_document(
    *,
    evidence: str,
    recipe: str,
    reason: str,
    native_exit: int | None,
    moved: Mapping[str, str],
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "evidence": evidence,
        "recipe": recipe,
        "reason": reason,
        "nativeExit": native_exit,
        "artifacts": dict(moved),
    }


def _bundle_relative_file(path: Path, *, bundle_root: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(bundle_root)
    except ValueError as exc:
        raise ValueError("failure archive source is outside the evidence bundle") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("failure archive source is invalid")
    return relative


def _open_or_create_windows_directory(
    parent_handle: object,
    name: str,
) -> tuple[object, tuple[int, int]]:
    try:
        return _open_windows_directory_relative(parent_handle, name)
    except FileNotFoundError:
        try:
            return _create_and_reopen_windows_directory(parent_handle, name)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {80, 183}:
                raise
            return _open_windows_directory_relative(parent_handle, name)


def _create_and_reopen_windows_directory(
    parent_handle: object,
    name: str,
) -> tuple[object, tuple[int, int]]:
    created_handle, created_identity = _create_windows_directory_relative(parent_handle, name)
    _close_windows_handle(created_handle)
    opened_handle, opened_identity = _open_windows_directory_relative(parent_handle, name)
    if opened_identity != created_identity:
        _close_windows_handle(opened_handle)
        raise ValueError("failure archive directory identity changed during creation")
    return opened_handle, opened_identity


def _relocate_windows_bundle_file(
    source: Path,
    *,
    bundle_root: Path,
    bundle_handle: object,
    target_handle: object,
    target_name: str,
) -> bool:
    relative = _bundle_relative_file(source, bundle_root=bundle_root)
    directory_handles: list[object] = []
    current = bundle_handle
    source_handle: object | None = None
    try:
        try:
            for component in relative.parts[:-1]:
                current, _identity = _open_windows_directory_relative(current, component)
                directory_handles.append(current)
            source_handle, _identity = _open_windows_regular_file_relative(
                current,
                relative.name,
                share_access=0x00000001 | 0x00000002 | 0x00000004,
                deletable=True,
            )
        except FileNotFoundError:
            return False
        _rename_windows_file_relative(
            source_handle,
            target_handle,
            target_name,
            replace_existing=False,
        )
        return True
    finally:
        if source_handle is not None:
            _close_windows_handle(source_handle)
        for handle in reversed(directory_handles):
            _close_windows_handle(handle)


def _discard_windows_bundle_file(
    source: Path,
    *,
    bundle_root: Path,
    bundle_handle: object,
) -> bool:
    import msvcrt

    relative = _bundle_relative_file(source, bundle_root=bundle_root)
    directory_handles: list[object] = []
    current = bundle_handle
    source_handle: object | None = None
    descriptor: int | None = None
    try:
        try:
            for component in relative.parts[:-1]:
                current, _identity = _open_windows_directory_relative(current, component)
                directory_handles.append(current)
            source_handle, _identity = _open_windows_regular_file_relative(
                current,
                relative.name,
                share_access=0x00000001 | 0x00000002 | 0x00000004,
                deletable=True,
            )
        except FileNotFoundError:
            return False
        handle_value = getattr(source_handle, "value", source_handle)
        descriptor = msvcrt.open_osfhandle(
            int(handle_value),
            os.O_RDONLY | os.O_BINARY,
        )
        source_handle = None
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("sensitive probe artifact is not a regular file")
        _delete_windows_file_on_close(msvcrt.get_osfhandle(descriptor))
        os.close(descriptor)
        descriptor = None
        try:
            recreated, _identity = _open_windows_regular_file_relative(
                current,
                relative.name,
                share_access=0x00000001 | 0x00000002 | 0x00000004,
            )
        except FileNotFoundError:
            return True
        _close_windows_handle(recreated)
        raise ValueError("sensitive probe artifact reappeared during cleanup")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if source_handle is not None:
            _close_windows_handle(source_handle)
        for handle in reversed(directory_handles):
            _close_windows_handle(handle)


def _record_probe_failure_windows(
    *,
    root: Path,
    evidence: str,
    recipe: str,
    attempt_id: str,
    reason: str,
    native_exit: int | None,
    artifacts: Mapping[str, Path],
) -> None:
    handles: list[object] = []
    try:
        bundle_handle, _identity = _open_windows_directory_handle(root, deletable=True)
        handles.append(bundle_handle)
        failure_root_handle, _identity = _open_or_create_windows_directory(
            bundle_handle,
            "failures",
        )
        handles.append(failure_root_handle)
        evidence_handle, _identity = _open_or_create_windows_directory(
            failure_root_handle,
            evidence,
        )
        handles.append(evidence_handle)
        failure_handle, _identity = _create_and_reopen_windows_directory(
            evidence_handle,
            attempt_id,
        )
        handles.append(failure_handle)
        moved: dict[str, str] = {}
        for name, source in artifacts.items():
            target_name = f"{name}.json"
            if _relocate_windows_bundle_file(
                Path(source),
                bundle_root=root,
                bundle_handle=bundle_handle,
                target_handle=failure_handle,
                target_name=target_name,
            ):
                moved[name] = f"failures/{evidence}/{attempt_id}/{target_name}"
        failure_file, _identity = _create_windows_staging_file(
            failure_handle,
            "failure.json",
            _json_bytes(
                _failure_archive_document(
                    evidence=evidence,
                    recipe=recipe,
                    reason=reason,
                    native_exit=native_exit,
                    moved=moved,
                )
            ),
        )
        _close_windows_handle(failure_file)
    finally:
        for handle in reversed(handles):
            _close_windows_handle(handle)


def _open_or_create_posix_directory(
    parent_fd: int,
    name: str,
    *,
    exclusive: bool,
) -> int:
    if Path(name).name != name or not name:
        raise ValueError("failure archive boundary is invalid")
    if exclusive:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    else:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_descriptor = os.open(name, flags, dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
        os.close(file_descriptor)
        raise ValueError("failure archive boundary is invalid")
    return file_descriptor


def _rename_posix_between_no_replace(
    source_fd: int,
    source: str,
    target_fd: int,
    target: str,
) -> None:
    if any(Path(name).name != name or not name for name in (source, target)):
        raise ValueError("failure archive entry name is invalid")
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2 is required for safe failure archival")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                source_fd,
                os.fsencode(source),
                target_fd,
                os.fsencode(target),
                1,
            )
            != 0
        ):
            error = ctypes.get_errno()
            raise OSError(error, f"cannot safely archive failure entry: {target}")
        return
    raise OSError(
        errno.ENOSYS,
        "atomic no-replace failure archival is unavailable on this POSIX platform",
    )


def _relocate_posix_bundle_file(
    source: Path,
    *,
    bundle_root: Path,
    bundle_fd: int,
    target_fd: int,
    target_name: str,
) -> bool:
    relative = _bundle_relative_file(source, bundle_root=bundle_root)
    directory_fds: list[int] = []
    current = bundle_fd
    source_file: int | None = None
    try:
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            for component in relative.parts[:-1]:
                current = os.open(component, directory_flags, dir_fd=current)
                directory_fds.append(current)
            source_file = os.open(
                relative.name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
        except FileNotFoundError:
            return False
        source_details = os.fstat(source_file)
        if not stat.S_ISREG(source_details.st_mode):
            raise ValueError("failure archive source is not a regular file")
        source_identity = _file_identity(source_details)
        _rename_posix_between_no_replace(current, relative.name, target_fd, target_name)
        target_details = os.stat(target_name, dir_fd=target_fd, follow_symlinks=False)
        if _file_identity(target_details) != source_identity or not stat.S_ISREG(
            target_details.st_mode
        ):
            raise ValueError("failure archive target identity changed")
        os.fsync(current)
        os.fsync(target_fd)
        return True
    finally:
        if source_file is not None:
            os.close(source_file)
        for file_descriptor in reversed(directory_fds):
            os.close(file_descriptor)


def _discard_posix_bundle_file(
    source: Path,
    *,
    bundle_root: Path,
    bundle_fd: int,
) -> bool:
    relative = _bundle_relative_file(source, bundle_root=bundle_root)
    directory_fds: list[int] = []
    current = bundle_fd
    source_file: int | None = None
    try:
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            for component in relative.parts[:-1]:
                current = os.open(component, directory_flags, dir_fd=current)
                directory_fds.append(current)
            source_file = os.open(
                relative.name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(os.fstat(source_file).st_mode):
            raise ValueError("sensitive probe artifact is not a regular file")
        os.unlink(relative.name, dir_fd=current)
        os.fsync(current)
        try:
            os.stat(relative.name, dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return True
        raise ValueError("sensitive probe artifact reappeared during cleanup")
    finally:
        if source_file is not None:
            os.close(source_file)
        for file_descriptor in reversed(directory_fds):
            os.close(file_descriptor)


def _discard_sensitive_probe_artifact(path: Path, *, bundle_root: Path) -> None:
    root = Path(bundle_root).resolve()
    if os.name == "nt":
        bundle_handle, _identity = _open_windows_directory_handle(root, deletable=True)
        try:
            _discard_windows_bundle_file(
                Path(path),
                bundle_root=root,
                bundle_handle=bundle_handle,
            )
        finally:
            _close_windows_handle(bundle_handle)
        return
    bundle_fd, _identity = _open_posix_directory_path_no_follow(root)
    try:
        _discard_posix_bundle_file(
            Path(path),
            bundle_root=root,
            bundle_fd=bundle_fd,
        )
    finally:
        os.close(bundle_fd)


def _write_posix_new_file(directory_fd: int, name: str, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    file_descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        offset = 0
        while offset < len(body):
            written = os.write(file_descriptor, body[offset:])
            if written <= 0:
                raise OSError("failure archive write made no progress")
            offset += written
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    os.fsync(directory_fd)


def _record_probe_failure_posix(
    *,
    root: Path,
    evidence: str,
    recipe: str,
    attempt_id: str,
    reason: str,
    native_exit: int | None,
    artifacts: Mapping[str, Path],
) -> None:
    handles: list[int] = []
    try:
        bundle_fd, _identity = _open_posix_directory_path_no_follow(root)
        handles.append(bundle_fd)
        failure_root_fd = _open_or_create_posix_directory(
            bundle_fd,
            "failures",
            exclusive=False,
        )
        handles.append(failure_root_fd)
        evidence_fd = _open_or_create_posix_directory(
            failure_root_fd,
            evidence,
            exclusive=False,
        )
        handles.append(evidence_fd)
        failure_fd = _open_or_create_posix_directory(
            evidence_fd,
            attempt_id,
            exclusive=True,
        )
        handles.append(failure_fd)
        moved: dict[str, str] = {}
        for name, source in artifacts.items():
            target_name = f"{name}.json"
            if _relocate_posix_bundle_file(
                Path(source),
                bundle_root=root,
                bundle_fd=bundle_fd,
                target_fd=failure_fd,
                target_name=target_name,
            ):
                moved[name] = f"failures/{evidence}/{attempt_id}/{target_name}"
        _write_posix_new_file(
            failure_fd,
            "failure.json",
            _json_bytes(
                _failure_archive_document(
                    evidence=evidence,
                    recipe=recipe,
                    reason=reason,
                    native_exit=native_exit,
                    moved=moved,
                )
            ),
        )
    finally:
        for file_descriptor in reversed(handles):
            os.close(file_descriptor)


def _record_probe_failure(
    *,
    bundle_root: Path,
    evidence: str,
    recipe: str,
    attempt_id: str,
    reason: str,
    native_exit: int | None,
    artifacts: Mapping[str, Path],
) -> Path:
    components = (evidence, attempt_id, *artifacts)
    if any(
        not isinstance(component, str)
        or not component
        or Path(component).name != component
        or component in {".", ".."}
        for component in components
    ):
        raise ValueError("failure archive boundary is invalid")
    root = Path(bundle_root).resolve()
    arguments = {
        "root": root,
        "evidence": evidence,
        "recipe": recipe,
        "attempt_id": attempt_id,
        "reason": reason,
        "native_exit": native_exit,
        "artifacts": artifacts,
    }
    try:
        if os.name == "nt":
            _record_probe_failure_windows(**arguments)
        else:
            _record_probe_failure_posix(**arguments)
    except ValueError as exc:
        if str(exc).startswith("failure archive"):
            raise
        raise ValueError("failure archive boundary is invalid") from exc
    return root / "failures" / evidence / attempt_id


def _record_playwright_probe_failure(
    *,
    bundle_root: Path,
    evidence: str,
    recipe: str,
    attempt_id: str,
    reason: str,
    native_exit: int | None,
    raw_report: Path,
    artifacts: Mapping[str, Path] | None = None,
) -> Path:
    _discard_sensitive_probe_artifact(raw_report, bundle_root=bundle_root)
    return _record_probe_failure(
        bundle_root=bundle_root,
        evidence=evidence,
        recipe=recipe,
        attempt_id=attempt_id,
        reason=reason,
        native_exit=native_exit,
        artifacts={} if artifacts is None else artifacts,
    )


def run_probe_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    evidence: str,
    observed_at: str,
    base_url: str,
    raw_report_path: Path,
    execution_record_path: Path,
    recipe: str,
    working_directory: Path,
    timeout_seconds: int,
    runner: CommandRunner = _run_probe,
) -> dict[str, object]:
    """Execute one probe and publish its receipt only from a fresh bound report."""
    if evidence in _DIRECT_RECEIPT_EVIDENCE or evidence not in RECEIPT_CONTRACTS:
        raise ValueError("probe evidence layer is invalid")
    candidate = _candidate(candidate_root)
    bound_run = _release_run(release_run)
    if not _valid_observed_at(observed_at):
        raise ValueError("receipt observedAt is invalid")
    resolved_output, _output_artifact = _bundle_artifact(
        output_path,
        bundle_root=bundle_root,
        label="receipt output",
    )
    resolved_report, report_artifact = _bundle_artifact(
        raw_report_path,
        bundle_root=bundle_root,
        label="probe raw report",
    )
    resolved_execution, execution_artifact = _bundle_artifact(
        execution_record_path,
        bundle_root=bundle_root,
        label="probe execution record",
    )
    resolved_attestation = Path(bundle_root).resolve() / "runtime" / "runtime-attestation.json"
    attestation_artifact = "runtime/runtime-attestation.json"
    if len({resolved_output, resolved_report, resolved_execution, resolved_attestation}) != 4:
        raise ValueError("probe proof files must use distinct paths")
    if resolved_output.exists() or resolved_report.exists() or resolved_execution.exists():
        raise ValueError("probe proof files must not already exist")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= _PROBE_CLEANUP_MARGIN_SECONDS
    ):
        raise ValueError("probe timeout is invalid")
    attestation_body, attestation_sha256 = read_runtime_attestation_artifact(
        resolved_attestation,
        bundle_root=bundle_root,
    )
    attestation = validate_runtime_attestation(
        resolved_attestation,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_sha256=attestation_sha256,
    )
    base_url = attestation["baseUrl"]
    assert isinstance(base_url, str)
    attestation_proof = {
        "artifact": attestation_artifact,
        "sha256": attestation_sha256,
    }
    arguments = _probe_command(evidence, recipe)
    cwd = Path(working_directory).resolve()
    if not cwd.is_dir():
        raise ValueError("probe working directory is invalid")
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    attempt_id = uuid.uuid4().hex
    staged_report = resolved_report.parent / f".{resolved_report.name}.{attempt_id}.staging"
    environment = os.environ.copy()
    environment.update(
        {
            "YFEISTAI_EVIDENCE_REPORT": str(staged_report),
            "YFEISTAI_CANDIDATE_ROOT": str(Path(candidate_root).resolve()),
            "YFEISTAI_RELEASE_RUN_ID": bound_run["runId"],
            "YFEISTAI_ENVIRONMENT_ID": bound_run["environmentId"],
            "YFEISTAI_EVIDENCE": evidence,
            "YFEISTAI_PROBE_TIMEOUT_SECONDS": str(timeout_seconds - _PROBE_CLEANUP_MARGIN_SECONDS),
            "WEB_BASE_URL": base_url,
        }
    )
    try:
        completed = runner(
            arguments,
            cwd=cwd,
            env=environment,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        _record_playwright_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="outer probe deadline expired",
            native_exit=None,
            raw_report=staged_report,
        )
        raise
    native_exit = completed.returncode
    if not isinstance(native_exit, int) or isinstance(native_exit, bool):
        _record_playwright_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe native exit is invalid",
            native_exit=None,
            raw_report=staged_report,
        )
        raise ValueError("probe native exit is invalid")
    if native_exit != 0:
        _record_playwright_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe native exit does not prove passing evidence",
            native_exit=native_exit,
            raw_report=staged_report,
        )
        raise ValueError(f"probe native exit {native_exit} does not prove passing evidence")
    try:
        raw_body = staged_report.read_bytes()
    except OSError as exc:
        _record_playwright_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe raw report is unavailable",
            native_exit=native_exit,
            raw_report=staged_report,
        )
        raise ValueError("probe raw report is unavailable") from exc
    try:
        _report, checks = _probe_report(
            raw_body,
            candidate=candidate,
            release_run=bound_run,
            evidence=evidence,
        )
        candidate_after = _candidate(candidate_root)
        if candidate_after != candidate:
            raise ValueError("candidate changed while the probe was running")
        try:
            attestation_after, attestation_after_sha256 = read_runtime_attestation_artifact(
                resolved_attestation,
                bundle_root=bundle_root,
            )
        except ValueError as exc:
            raise ValueError(
                "runtime attestation became unavailable while the probe was running"
            ) from exc
        if attestation_after != attestation_body or attestation_after_sha256 != attestation_sha256:
            raise ValueError("runtime attestation changed while the probe was running")
        validate_runtime_attestation(
            resolved_attestation,
            bundle_root=bundle_root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
            expected_sha256=attestation_sha256,
        )
    except ValueError:
        _record_playwright_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe raw report or candidate validation failed",
            native_exit=native_exit,
            raw_report=staged_report,
        )
        raise
    command_record = probe_command_record(evidence)
    raw_sha256 = hashlib.sha256(raw_body).hexdigest()
    execution = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": bound_run,
        "evidence": evidence,
        "recipe": recipe,
        "command": command_record,
        "observedAt": observed_at,
        "baseUrl": base_url,
        "nativeExit": native_exit,
        "rawReportSha256": raw_sha256,
        "runtimeAttestation": attestation_proof,
    }
    try:
        os.replace(staged_report, resolved_report)
        _atomic_write_json(resolved_execution, execution)
        execution_sha256 = hashlib.sha256(resolved_execution.read_bytes()).hexdigest()
        return _write_pass_receipt_from_candidate(
            resolved_output,
            candidate=candidate,
            release_run=bound_run,
            evidence=evidence,
            observed_at=observed_at,
            native_exit=native_exit,
            checks=checks,
            provenance={
                "recipe": recipe,
                "command": command_record,
                "rawReport": {
                    "artifact": report_artifact,
                    "sha256": raw_sha256,
                },
                "execution": {
                    "artifact": execution_artifact,
                    "sha256": execution_sha256,
                },
                "runtimeAttestation": attestation_proof,
            },
        )
    except Exception:
        _record_playwright_probe_failure(
            bundle_root=bundle_root,
            evidence=evidence,
            recipe=recipe,
            attempt_id=attempt_id,
            reason="probe proof publication failed",
            native_exit=native_exit,
            raw_report=resolved_report if resolved_report.exists() else staged_report,
            artifacts={
                "execution": resolved_execution,
                "receipt": resolved_output,
            },
        )
        raise


def _run_git(arguments: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"safe.directory={cwd.as_posix()}",
            *arguments,
        ],
        cwd=cwd,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )


def _git_stdout(
    runner: GitRunner,
    arguments: list[str],
    *,
    cwd: Path,
) -> str:
    try:
        result = runner(arguments, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Git probe could not run") from exc
    if (
        not isinstance(result.returncode, int)
        or isinstance(result.returncode, bool)
        or result.returncode != 0
        or not isinstance(result.stdout, str)
    ):
        raise ValueError("Git probe failed")
    return result.stdout.strip()


def _github_repository(remote: str) -> str | None:
    match = _GITHUB_REMOTE.fullmatch(remote)
    return match.group(1) if match is not None else None


def write_source_head_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    release_run: Mapping[str, object],
    source_root: Path,
    observed_at: str,
    git_runner: GitRunner = _run_git,
) -> dict[str, object]:
    """Probe one trusted, clean Git checkout and bind it to the candidate."""
    candidate = _candidate(candidate_root)
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise ValueError("Git source root is unavailable")
    head = _git_stdout(git_runner, ["rev-parse", "HEAD"], cwd=root)
    status = _git_stdout(
        git_runner,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
    )
    origin = _git_stdout(git_runner, ["remote", "get-url", "origin"], cwd=root)
    final_head = _git_stdout(git_runner, ["rev-parse", "HEAD"], cwd=root)
    try:
        final_candidate = _candidate(candidate_root)
    except ValueError as exc:
        raise ValueError("release candidate changed during Git probe") from exc
    if final_candidate != candidate:
        raise ValueError("release candidate changed during Git probe")
    if head != candidate["sourceHead"]:
        raise ValueError("Git HEAD does not match the release candidate")
    if final_head != head:
        raise ValueError("Git HEAD changed during release probe")
    if status:
        raise ValueError("Git worktree is not clean")
    if _github_repository(origin) != candidate["sourceRepository"]:
        raise ValueError("Git origin does not match the release candidate")
    return _write_pass_receipt_from_candidate(
        output_path,
        candidate=candidate,
        release_run=release_run,
        evidence="source_head",
        observed_at=observed_at,
        native_exit=0,
        checks={"headMatches": True, "worktreeClean": True},
    )


def write_image_digest_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    release_run: Mapping[str, object],
    observed_at: str,
) -> dict[str, object]:
    """Revalidate the candidate lock and both Compose files before receipt."""
    candidate = _candidate(candidate_root)
    return _write_pass_receipt_from_candidate(
        output_path,
        candidate=candidate,
        release_run=release_run,
        evidence="image_digests",
        observed_at=observed_at,
        native_exit=0,
        checks={"lockMatches": True, "composeMatches": True},
    )


def write_running_containers_receipt(
    output_path: Path,
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
) -> dict[str, object]:
    """Derive stable-container evidence from the fixed runtime attestation."""
    resolved_output, _output_artifact = _bundle_artifact(
        output_path,
        bundle_root=bundle_root,
        label="receipt output",
    )
    canonical_output = Path(bundle_root).resolve() / "artifacts" / "running_containers.json"
    if resolved_output != canonical_output:
        raise ValueError("running containers receipt must use its canonical output path")
    candidate = _candidate(candidate_root)
    bound_run = _release_run(release_run)
    resolved_attestation = Path(bundle_root).resolve() / "runtime" / "runtime-attestation.json"
    attestation_body, attestation_sha256 = read_runtime_attestation_artifact(
        resolved_attestation,
        bundle_root=bundle_root,
    )
    attestation = validate_runtime_attestation(
        resolved_attestation,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=attestation_sha256,
    )
    observed_at = attestation["observedAt"]
    assert isinstance(observed_at, str)
    try:
        candidate_after = _candidate(candidate_root)
    except ValueError as exc:
        raise ValueError("candidate changed while container evidence was derived") from exc
    if candidate_after != candidate:
        raise ValueError("candidate changed while container evidence was derived")
    try:
        attestation_after, attestation_after_sha256 = read_runtime_attestation_artifact(
            resolved_attestation,
            bundle_root=bundle_root,
        )
    except ValueError as exc:
        raise ValueError("runtime attestation changed while evidence was derived") from exc
    if attestation_after != attestation_body or attestation_after_sha256 != attestation_sha256:
        raise ValueError("runtime attestation changed while evidence was derived")
    validate_runtime_attestation(
        resolved_attestation,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=attestation_sha256,
    )
    return _write_pass_receipt_from_candidate(
        resolved_output,
        candidate=candidate,
        release_run=bound_run,
        evidence="running_containers",
        observed_at=observed_at,
        native_exit=0,
        checks={"stableContainerSet": True},
        provenance={
            "runtimeAttestation": {
                "artifact": "runtime/runtime-attestation.json",
                "sha256": attestation_sha256,
            }
        },
    )


def write_platform_preflight_receipts(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    runner: CommandRunner = _run_platform_preflight_phase,
    docker_resolver: Callable[[], Path] = resolve_fixed_docker,
) -> dict[str, dict[str, object]]:
    """Run both fixed candidate-network phases and publish their bound receipts."""

    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ValueError("platform preflight timeout is invalid")
    root = Path(bundle_root).resolve()
    candidate_path = Path(candidate_root).resolve()
    proof_path = root / "runtime" / "platform-preflight-attestation.json"
    receipt_paths = {
        evidence: root / "artifacts" / f"{evidence}.json"
        for evidence in ("database_revisions", "service_health")
    }
    targets = (proof_path, *receipt_paths.values())
    if any(path.exists() for path in targets):
        raise ValueError("platform preflight evidence already exists")

    candidate = _candidate(candidate_path)
    bound_run = _release_run(release_run)
    runtime_path = root / "runtime" / "runtime-attestation.json"
    runtime_body, runtime_sha256 = read_runtime_attestation_artifact(
        runtime_path,
        bundle_root=root,
    )
    runtime = validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=runtime_sha256,
    )
    containers = runtime.get("containers")
    if not isinstance(containers, list):
        raise ValueError("platform preflight runtime containers are invalid")
    container_ids = {
        container.get("service"): container.get("containerId")
        for container in containers
        if isinstance(container, dict)
    }
    base_url = runtime.get("baseUrl")
    if not isinstance(base_url, str):
        raise ValueError("platform preflight runtime URL is invalid")

    allowed_environment = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "WINDIR",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_environment
    }
    executions: list[dict[str, object]] = []
    docker = Path(docker_resolver())
    with tempfile.TemporaryDirectory(prefix="yfeistai-preflight-docker-") as config_dir:
        for phase in PREFLIGHT_PHASES:
            service = PREFLIGHT_PHASE_SERVICES[phase]
            container_id = container_ids.get(service)
            if not isinstance(container_id, str):
                raise ValueError("platform preflight container identity is invalid")
            actual_command, logical_command = materialize_candidate_network_phase_command(
                phase,
                container_id,
                docker_executable=docker,
                docker_config=Path(config_dir).resolve(),
            )
            try:
                completed = runner(
                    actual_command,
                    cwd=candidate_path,
                    env=environment,
                    timeout=timeout_seconds,
                )
            except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
                raise ValueError("platform preflight phase could not run") from exc
            if (
                not isinstance(completed.returncode, int)
                or isinstance(completed.returncode, bool)
                or completed.returncode != 0
                or completed.args != actual_command
                or not isinstance(completed.stdout, str)
                or completed.stderr != ""
            ):
                raise ValueError("platform preflight phase did not exit cleanly")
            try:
                stdout_body = completed.stdout.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("platform preflight phase stdout is invalid") from exc
            report = parse_candidate_network_report(stdout_body, expected_phase=phase)
            checks = report["checks"]
            errors = report["errors"]
            assert isinstance(checks, dict) and isinstance(errors, list)
            if errors or any(value is not True for value in checks.values()):
                raise ValueError("platform preflight phase did not prove passing evidence")
            executions.append(
                {
                    "phase": phase,
                    "service": service,
                    "containerId": container_id,
                    "command": logical_command,
                    "nativeExit": 0,
                    "stdout": completed.stdout,
                    "stdoutSha256": hashlib.sha256(stdout_body).hexdigest(),
                }
            )
        if any(Path(config_dir).iterdir()):
            raise ValueError("platform preflight isolated Docker config was modified")

    try:
        candidate_after = _candidate(candidate_path)
        runtime_after, runtime_after_sha256 = read_runtime_attestation_artifact(
            runtime_path,
            bundle_root=root,
        )
    except ValueError as exc:
        raise ValueError("platform preflight release binding changed") from exc
    if (
        candidate_after != candidate
        or runtime_after != runtime_body
        or runtime_after_sha256 != runtime_sha256
    ):
        raise ValueError("platform preflight release binding changed")
    validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_sha256=runtime_sha256,
    )

    observed_at = _current_observed_at()
    if not _valid_observed_at(observed_at):
        raise ValueError("platform preflight observedAt is invalid")
    proof = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": bound_run,
        "observedAt": observed_at,
        "baseUrl": base_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        },
        "executions": executions,
    }
    attempt_id = uuid.uuid4().hex
    staged_proof = proof_path.with_name(f".{proof_path.name}.{attempt_id}.staging")
    staged_receipts = {
        evidence: path.with_name(f".{path.name}.{attempt_id}.staging")
        for evidence, path in receipt_paths.items()
    }
    staged_paths = {
        "proof": staged_proof,
        **staged_receipts,
    }
    published_paths: dict[str, Path] = {}
    try:
        _atomic_write_json(staged_proof, proof)
        proof_body = staged_proof.read_bytes()
        proof_sha256 = hashlib.sha256(proof_body).hexdigest()
        derived_checks, replayed_observed_at = derive_platform_preflight_receipt_checks(
            proof_body,
            bundle_root=root,
            candidate_root=candidate_path,
            candidate=candidate,
            release_run=bound_run,
        )
        if replayed_observed_at != observed_at:
            raise ValueError("platform preflight replay timestamp changed")
        provenance = {
            "platformPreflightAttestation": {
                "artifact": "runtime/platform-preflight-attestation.json",
                "sha256": proof_sha256,
            }
        }
        documents = {
            "database_revisions": _write_pass_receipt_from_candidate(
                staged_receipts["database_revisions"],
                candidate=candidate,
                release_run=bound_run,
                evidence="database_revisions",
                observed_at=observed_at,
                native_exit=0,
                checks=derived_checks["database_revisions"],
                provenance=provenance,
            ),
            "service_health": _write_pass_receipt_from_candidate(
                staged_receipts["service_health"],
                candidate=candidate,
                release_run=bound_run,
                evidence="service_health",
                observed_at=observed_at,
                native_exit=0,
                checks=derived_checks["service_health"],
                provenance=provenance,
            ),
        }
        candidate_before_publish = _candidate(candidate_path)
        runtime_before_publish, sha_before_publish = read_runtime_attestation_artifact(
            runtime_path,
            bundle_root=root,
        )
        if (
            candidate_before_publish != candidate
            or runtime_before_publish != runtime_body
            or sha_before_publish != runtime_sha256
            or any(path.exists() for path in targets)
        ):
            raise ValueError("platform preflight release binding changed before publication")
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_paths["database_revisions"].parent.mkdir(parents=True, exist_ok=True)
        publication_order = (
            (
                "database_revisions",
                staged_receipts["database_revisions"],
                receipt_paths["database_revisions"],
            ),
            (
                "service_health",
                staged_receipts["service_health"],
                receipt_paths["service_health"],
            ),
            ("proof", staged_proof, proof_path),
        )
        for name, staged, target in publication_order:
            _publish_no_replace(staged, target)
            published_paths[f"published-{name.replace('_', '-')}"] = target
            if target.read_bytes() != staged.read_bytes():
                raise ValueError("platform preflight published evidence changed")
            staged.unlink()
        return documents
    except Exception:
        _record_probe_failure(
            bundle_root=root,
            evidence="platform-preflight",
            recipe="candidate-network-phases",
            attempt_id=attempt_id,
            reason="platform preflight proof publication failed",
            native_exit=0,
            artifacts={
                **staged_paths,
                **published_paths,
            },
        )
        raise


def write_capacity_profile_receipt(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    runner: CommandRunner = _run_capacity_profile,
) -> dict[str, object]:
    """Run the fixed live capacity probe and publish its proof marker last."""

    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= _PROBE_CLEANUP_MARGIN_SECONDS
    ):
        raise ValueError("capacity profile timeout is invalid")
    token = os.environ.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("capacity profile live fixture token is unavailable")
    root = Path(bundle_root).resolve()
    candidate_path = Path(candidate_root).resolve()
    proof_path = root / "runtime" / "capacity-profile-attestation.json"
    receipt_path = root / "artifacts" / "capacity_profile.json"
    idempotency_path = root / "artifacts" / "learning_event_idempotency.json"
    if any(os.path.lexists(path) for path in (proof_path, receipt_path, idempotency_path)):
        raise ValueError("capacity profile evidence already exists")

    candidate = _candidate(candidate_path)
    bound_run = _release_run(release_run)
    runtime_path = root / "runtime" / "runtime-attestation.json"
    runtime_body, runtime_sha256 = read_runtime_attestation_artifact(
        runtime_path,
        bundle_root=root,
    )
    runtime = validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=runtime_sha256,
    )
    base_url = runtime.get("baseUrl")
    if not isinstance(base_url, str):
        raise ValueError("capacity profile runtime URL is invalid")

    logical_command = capacity_profile_command_record()
    arguments = [
        sys.executable,
        str(SCRIPTS_ROOT / "classroom_capacity_probe.py"),
        "--profile",
        "first-release",
    ]
    allowed_environment = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "WINDIR",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_environment
    }
    environment.update(
        {
            "YFEISTAI_LIVE_FIXTURE_TOKEN": token,
            "YFEISTAI_CANDIDATE_ROOT": str(candidate_path),
            "YFEISTAI_RELEASE_RUN_ID": bound_run["runId"],
            "YFEISTAI_ENVIRONMENT_ID": bound_run["environmentId"],
            "YFEISTAI_CAPACITY_TIMEOUT_SECONDS": str(
                timeout_seconds - _PROBE_CLEANUP_MARGIN_SECONDS
            ),
            "WEB_BASE_URL": base_url,
        }
    )
    try:
        completed = runner(
            arguments,
            cwd=candidate_path,
            env=environment,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ValueError("capacity profile probe could not run") from exc
    if (
        not isinstance(completed.returncode, int)
        or isinstance(completed.returncode, bool)
        or completed.returncode != 0
        or completed.args != arguments
        or not isinstance(completed.stdout, str)
        or completed.stderr != ""
    ):
        raise ValueError("capacity profile probe did not exit cleanly")
    try:
        stdout = completed.stdout.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("capacity profile probe output is invalid") from exc
    if any(
        secret.encode("utf-8", errors="strict") in stdout
        for secret in {token, token.strip()}
        if secret
    ):
        raise ValueError("capacity profile serialized live fixture token")
    report = parse_capacity_profile_report(
        stdout,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
    )
    summary = derive_capacity_profile_summary(report)
    checks = summary.get("checks")
    if not isinstance(checks, dict) or any(value is not True for value in checks.values()):
        raise ValueError("capacity profile did not prove passing thresholds")
    idempotency_checks = derive_learning_event_idempotency_checks(report)
    if any(value is not True for value in idempotency_checks.values()):
        raise ValueError("capacity profile did not prove learning event idempotency")
    observed_at = report.get("observedAt")
    if not isinstance(observed_at, str) or not _valid_observed_at(observed_at):
        raise ValueError("capacity profile observedAt is invalid")

    try:
        candidate_after = _candidate(candidate_path)
        runtime_after, runtime_after_sha256 = read_runtime_attestation_artifact(
            runtime_path,
            bundle_root=root,
        )
    except ValueError as exc:
        raise ValueError("capacity profile release binding changed") from exc
    if (
        candidate_after != candidate
        or runtime_after != runtime_body
        or runtime_after_sha256 != runtime_sha256
    ):
        raise ValueError("capacity profile release binding changed")
    validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_sha256=runtime_sha256,
    )

    proof = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": bound_run,
        "observedAt": observed_at,
        "baseUrl": base_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        },
        "execution": {
            "command": logical_command,
            "nativeExit": 0,
            "stdout": completed.stdout,
            "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
            "stderr": "",
        },
        "summary": summary,
    }
    attempt_id = uuid.uuid4().hex
    staged_proof = proof_path.with_name(f".{proof_path.name}.{attempt_id}.staging")
    staged_receipt = receipt_path.with_name(f".{receipt_path.name}.{attempt_id}.staging")
    staged_idempotency = idempotency_path.with_name(
        f".{idempotency_path.name}.{attempt_id}.staging"
    )
    published: dict[str, Path] = {}
    try:
        _atomic_write_json(staged_proof, proof)
        proof_body = staged_proof.read_bytes()
        proof_sha256 = hashlib.sha256(proof_body).hexdigest()
        derived_checks, replayed_observed_at = derive_capacity_profile_receipt_checks(
            proof_body,
            bundle_root=root,
            candidate_root=candidate_path,
            candidate=candidate,
            release_run=bound_run,
        )
        if replayed_observed_at != observed_at or derived_checks != checks:
            raise ValueError("capacity profile replay changed")
        receipt = _write_pass_receipt_from_candidate(
            staged_receipt,
            candidate=candidate,
            release_run=bound_run,
            evidence="capacity_profile",
            observed_at=observed_at,
            native_exit=0,
            checks=derived_checks,
            provenance={
                "capacityAttestation": {
                    "artifact": "runtime/capacity-profile-attestation.json",
                    "sha256": proof_sha256,
                }
            },
        )
        _write_pass_receipt_from_candidate(
            staged_idempotency,
            candidate=candidate,
            release_run=bound_run,
            evidence="learning_event_idempotency",
            observed_at=observed_at,
            native_exit=0,
            checks=idempotency_checks,
            provenance={
                "capacityAttestation": {
                    "artifact": "runtime/capacity-profile-attestation.json",
                    "sha256": proof_sha256,
                }
            },
        )
        candidate_before_publish = _candidate(candidate_path)
        runtime_before_publish, runtime_before_sha256 = read_runtime_attestation_artifact(
            runtime_path,
            bundle_root=root,
        )
        if (
            candidate_before_publish != candidate
            or runtime_before_publish != runtime_body
            or runtime_before_sha256 != runtime_sha256
            or any(os.path.lexists(path) for path in (proof_path, receipt_path, idempotency_path))
        ):
            raise ValueError("capacity profile release binding changed before publication")
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        for name, staged, target in (
            ("capacity-receipt", staged_receipt, receipt_path),
            ("idempotency-receipt", staged_idempotency, idempotency_path),
            ("proof", staged_proof, proof_path),
        ):
            _publish_no_replace(staged, target)
            published[f"published-{name}"] = target
            if target.read_bytes() != staged.read_bytes():
                raise ValueError("capacity profile published evidence changed")
            staged.unlink()
        return receipt
    except Exception:
        _record_probe_failure(
            bundle_root=root,
            evidence="capacity-profile",
            recipe="live-first-release",
            attempt_id=attempt_id,
            reason="capacity profile proof publication failed",
            native_exit=0,
            artifacts={
                "proof": staged_proof,
                "capacity-receipt": staged_receipt,
                "idempotency-receipt": staged_idempotency,
                **published,
            },
        )
        raise


def _parse_bound_openmaic_dedicated_outage_attestation(
    body: bytes,
    *,
    bundle_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
    expected_runtime_attestation_sha256: str,
    expected_observer_attestation_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
    expected_shared_ingress_control_origin: str,
    expected_tenant_id: str,
    expected_docker_host_identity_sha256: str,
) -> dict[str, object]:
    """Replay the archived marker, then validate its bound outage receipt."""

    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC dedicated outage attestation is invalid") from exc
    fixture = document.get("fixture") if isinstance(document, dict) else None
    provenance = document.get("provenance") if isinstance(document, dict) else None
    outage = document.get("outage") if isinstance(document, dict) else None
    marker_reference = fixture.get("attemptMarker") if isinstance(fixture, dict) else None
    route_id = outage.get("routeId") if isinstance(outage, dict) else None
    if (
        not isinstance(provenance, dict)
        or provenance.get("attemptMarker") != marker_reference
        or not isinstance(route_id, str)
        or _SHA256.fullmatch(expected_docker_host_identity_sha256) is None
        or expected_docker_host_identity_sha256 == "0" * 64
    ):
        raise ValueError("OpenMAIC dedicated outage attestation is invalid")
    replayed = _replay_openmaic_dedicated_outage_attempt_marker(
        bundle_root,
        marker_reference,
        candidate=candidate,
        release_run=release_run,
        expected_observer_attestation_sha256=expected_observer_attestation_sha256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_shared_ingress_control_origin=expected_shared_ingress_control_origin,
        expected_tenant_id=expected_tenant_id,
        expected_route_id=route_id,
        return_body=True,
    )
    if not isinstance(replayed, tuple):
        raise ValueError("OpenMAIC dedicated outage attempt marker is invalid")
    _marker, marker_body = replayed
    return parse_openmaic_dedicated_outage_attestation(
        body,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=expected_base_url,
        expected_runtime_attestation_sha256=expected_runtime_attestation_sha256,
        expected_observer_attestation_sha256=expected_observer_attestation_sha256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_shared_ingress_control_origin=(expected_shared_ingress_control_origin),
        expected_tenant_id=expected_tenant_id,
        attempt_marker_body=marker_body,
        expected_docker_host_identity_sha256=(expected_docker_host_identity_sha256),
    )


def _write_openmaic_plane_receipt(
    *,
    plane: str,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    runner: CommandRunner = _run_openmaic_smoke,
    expected_outage_docker_host_identity_sha256: str | None = None,
) -> dict[str, object]:
    """Run one fixed OpenMAIC plane smoke and publish its proof marker last."""

    if plane == "shared":
        evidence = "openmaic_shared_plane"
        proof_name = "openmaic-shared-plane-attestation.json"
        receipt_name = "openmaic_shared_plane.json"
        provenance_key = "openmaicSharedPlaneAttestation"
        logical_command = openmaic_shared_plane_command_record()
        derive_checks = derive_openmaic_shared_plane_checks
        expected_smoke_checks = {"sharedGenerationPassed": True}
        expected_receipt_checks = expected_smoke_checks
    elif plane == "dedicated":
        evidence = "openmaic_dedicated_plane"
        proof_name = "openmaic-dedicated-plane-attestation.json"
        receipt_name = "openmaic_dedicated_plane.json"
        provenance_key = "openmaicDedicatedPlaneAttestation"
        logical_command = openmaic_dedicated_plane_command_record()
        derive_checks = derive_openmaic_dedicated_plane_checks
        expected_smoke_checks = {
            "dedicatedGenerationPassed": True,
            "noSharedClientIssued": True,
        }
        expected_receipt_checks = {
            **expected_smoke_checks,
            "noSharedFallback": True,
        }
    else:
        raise ValueError("OpenMAIC evidence plane is invalid")
    label = f"OpenMAIC {plane}-plane"
    staging_prefix = f"openmaic-{plane}-plane"

    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= _PROBE_CLEANUP_MARGIN_SECONDS
    ):
        raise ValueError(f"{label} timeout is invalid")
    token = os.environ.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError(f"{label} live fixture token is unavailable")
    dedicated_tenant_id: str | None = None
    if plane == "dedicated":
        raw_tenant_id = os.environ.get("YFEISTAI_DEDICATED_TENANT_ID")
        if not isinstance(raw_tenant_id, str) or _RELEASE_ID.fullmatch(raw_tenant_id) is None:
            raise ValueError("OpenMAIC dedicated-plane tenant ID is unavailable or invalid")
        dedicated_tenant_id = raw_tenant_id
    secrets = tuple(
        value.encode("utf-8", errors="strict") for value in {token, token.strip()} if value
    )
    root = Path(os.path.abspath(bundle_root))
    candidate_path = Path(candidate_root).resolve()
    proof_path = root / "runtime" / proof_name
    receipt_path = root / "artifacts" / receipt_name
    if any(os.path.lexists(path) for path in (proof_path, receipt_path)):
        raise ValueError(f"{label} evidence already exists")

    candidate = _candidate(candidate_path)
    bound_run = _release_run(release_run)
    runtime_path = root / "runtime" / "runtime-attestation.json"
    runtime_body, runtime_sha256 = read_runtime_attestation_artifact(
        runtime_path,
        bundle_root=root,
    )
    runtime = validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=runtime_sha256,
    )
    base_url = runtime.get("baseUrl")
    if not isinstance(base_url, str):
        raise ValueError(f"{label} runtime URL is invalid")

    outage_path: Path | None = None
    outage_body: bytes | None = None
    outage_sha256: str | None = None
    outage_checks: dict[str, bool] = {}
    observer_path: Path | None = None
    observer_body: bytes | None = None
    observer_sha256: str | None = None
    observer_id: str | None = None
    observer_origin: str | None = None
    control_origin: str | None = None
    if dedicated_tenant_id is not None:
        if (
            not isinstance(expected_outage_docker_host_identity_sha256, str)
            or _SHA256.fullmatch(expected_outage_docker_host_identity_sha256) is None
            or expected_outage_docker_host_identity_sha256 == "0" * 64
        ):
            raise ValueError(
                "OpenMAIC dedicated outage Docker host identity is unavailable or invalid"
            )
        observer_path = root / "runtime" / "openmaic-shared-ingress-observer-attestation.json"
        try:
            observer_body = observer_path.read_bytes()
            observer = parse_openmaic_shared_ingress_observer_attestation(
                observer_body,
                release_run=bound_run,
            )
        except (OSError, ValueError) as exc:
            raise ValueError("OpenMAIC shared-ingress observer attestation is invalid") from exc
        observer_sha256 = hashlib.sha256(observer_body).hexdigest()
        observer_details = observer.get("observer")
        if not isinstance(observer_details, dict):
            raise ValueError("OpenMAIC shared-ingress observer attestation is invalid")
        observer_id = observer_details.get("observerId")
        observer_url = observer_details.get("observerUrl")
        control_url = observer_details.get("sharedIngressControlUrl")
        if not all(isinstance(value, str) for value in (observer_id, observer_url, control_url)):
            raise ValueError("OpenMAIC shared-ingress observer attestation is invalid")
        parsed_observer_url = urlsplit(observer_url)
        parsed_control_url = urlsplit(control_url)
        observer_origin = f"{parsed_observer_url.scheme}://{parsed_observer_url.netloc}"
        control_origin = f"{parsed_control_url.scheme}://{parsed_control_url.netloc}"
        outage_path = root / "runtime" / "openmaic-dedicated-outage-attestation.json"
        try:
            outage_body = outage_path.read_bytes()
        except OSError as exc:
            raise ValueError("OpenMAIC dedicated outage attestation is unavailable") from exc
        try:
            outage_report = _parse_bound_openmaic_dedicated_outage_attestation(
                outage_body,
                bundle_root=root,
                candidate=candidate,
                release_run=bound_run,
                expected_base_url=base_url,
                expected_runtime_attestation_sha256=runtime_sha256,
                expected_observer_attestation_sha256=observer_sha256,
                expected_observer_id=observer_id,
                expected_observer_origin=observer_origin,
                expected_shared_ingress_control_origin=control_origin,
                expected_tenant_id=dedicated_tenant_id,
                expected_docker_host_identity_sha256=(expected_outage_docker_host_identity_sha256),
            )
        except ValueError as exc:
            raise ValueError("OpenMAIC dedicated outage attestation is invalid") from exc
        outage_checks = derive_openmaic_dedicated_outage_checks(outage_report)
        if outage_checks != {"noSharedFallback": True}:
            raise ValueError("OpenMAIC dedicated outage attestation did not prove no fallback")
        outage_sha256 = hashlib.sha256(outage_body).hexdigest()

    arguments = [
        sys.executable,
        str(SCRIPTS_ROOT / "openmaic_smoke_probe.py"),
        "--plane",
        plane,
        "--profile",
        "first-release",
    ]
    allowed_environment = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "WINDIR",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_environment
    }
    environment.update(
        {
            "YFEISTAI_LIVE_FIXTURE_TOKEN": token,
            "YFEISTAI_CANDIDATE_ROOT": str(candidate_path),
            "YFEISTAI_RELEASE_RUN_ID": bound_run["runId"],
            "YFEISTAI_ENVIRONMENT_ID": bound_run["environmentId"],
            "YFEISTAI_RUNTIME_ATTESTATION_SHA256": runtime_sha256,
            "YFEISTAI_OPENMAIC_SMOKE_TIMEOUT_SECONDS": str(
                timeout_seconds - _PROBE_CLEANUP_MARGIN_SECONDS
            ),
            "WEB_BASE_URL": base_url,
        }
    )
    if dedicated_tenant_id is not None:
        environment["YFEISTAI_DEDICATED_TENANT_ID"] = dedicated_tenant_id
    try:
        completed = runner(
            arguments,
            cwd=candidate_path,
            env=environment,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ValueError(f"{label} probe could not run") from exc
    if (
        not isinstance(completed.returncode, int)
        or isinstance(completed.returncode, bool)
        or completed.returncode != 0
        or completed.args != arguments
        or not isinstance(completed.stdout, str)
        or completed.stderr != ""
    ):
        raise ValueError(f"{label} probe did not exit cleanly")
    try:
        stdout = completed.stdout.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} probe output is invalid") from exc
    if len(stdout) > MAX_OPENMAIC_SMOKE_REPORT_BYTES or any(secret in stdout for secret in secrets):
        raise ValueError(f"{label} probe output is invalid or contains a secret")
    report = parse_openmaic_smoke_report(
        stdout,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_runtime_attestation_sha256=runtime_sha256,
        forbidden_secret_values=secrets,
        expected_plane=plane,
    )
    smoke_checks = derive_checks(report)
    if smoke_checks != expected_smoke_checks:
        raise ValueError(f"{label} probe did not prove materialized generation")
    checks = {**smoke_checks, **outage_checks}
    if checks != expected_receipt_checks:
        raise ValueError(f"{label} evidence checks are incomplete")
    observed_at = report.get("observedAt")
    fixture = report.get("fixture")
    binding = report.get("binding")
    generation = report.get("generation")
    if (
        not isinstance(observed_at, str)
        or not _valid_observed_at(observed_at)
        or not isinstance(fixture, dict)
        or not isinstance(binding, dict)
        or not isinstance(generation, dict)
    ):
        raise ValueError(f"{label} report summary is invalid")
    if dedicated_tenant_id is not None and fixture.get("tenantId") != dedicated_tenant_id:
        raise ValueError(
            "OpenMAIC dedicated-plane report tenant does not match the requested tenant"
        )

    def assert_release_binding() -> None:
        try:
            candidate_after = _candidate(candidate_path)
            runtime_after, runtime_after_sha256 = read_runtime_attestation_artifact(
                runtime_path,
                bundle_root=root,
            )
        except ValueError as exc:
            raise ValueError(f"{label} release binding changed") from exc
        if (
            candidate_after != candidate
            or runtime_after != runtime_body
            or runtime_after_sha256 != runtime_sha256
        ):
            raise ValueError(f"{label} release binding changed")
        validate_runtime_attestation(
            runtime_path,
            bundle_root=root,
            candidate_root=candidate_path,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
            expected_sha256=runtime_sha256,
        )
        if outage_path is not None:
            assert outage_body is not None
            assert observer_path is not None
            assert observer_body is not None
            assert observer_sha256 is not None
            assert observer_id is not None
            assert observer_origin is not None
            assert control_origin is not None
            try:
                outage_after = outage_path.read_bytes()
                observer_after = observer_path.read_bytes()
            except OSError as exc:
                raise ValueError("OpenMAIC dedicated outage release binding changed") from exc
            if outage_after != outage_body or observer_after != observer_body:
                raise ValueError("OpenMAIC dedicated outage release binding changed")
            try:
                parse_openmaic_shared_ingress_observer_attestation(
                    observer_after,
                    release_run=bound_run,
                )
                replayed_outage = _parse_bound_openmaic_dedicated_outage_attestation(
                    outage_after,
                    bundle_root=root,
                    candidate=candidate,
                    release_run=bound_run,
                    expected_base_url=base_url,
                    expected_runtime_attestation_sha256=runtime_sha256,
                    expected_observer_attestation_sha256=observer_sha256,
                    expected_observer_id=observer_id,
                    expected_observer_origin=observer_origin,
                    expected_shared_ingress_control_origin=control_origin,
                    expected_tenant_id=dedicated_tenant_id,
                    expected_docker_host_identity_sha256=(
                        expected_outage_docker_host_identity_sha256
                    ),
                )
            except ValueError as exc:
                raise ValueError("OpenMAIC dedicated outage release binding changed") from exc
            if derive_openmaic_dedicated_outage_checks(replayed_outage) != outage_checks:
                raise ValueError("OpenMAIC dedicated outage release binding changed")

    assert_release_binding()
    proof = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": bound_run,
        "observedAt": observed_at,
        "baseUrl": base_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        },
        "execution": {
            "command": logical_command,
            "nativeExit": 0,
            "stdout": completed.stdout,
            "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
            "stderr": "",
        },
        "summary": {
            "fixture": json.loads(json.dumps(fixture)),
            "binding": json.loads(json.dumps(binding)),
            "generation": json.loads(json.dumps(generation)),
            "checks": smoke_checks,
        },
    }
    attempt_id = uuid.uuid4().hex
    boundary = _TenantPublicationBoundary.open(
        root,
        attempt_id,
        staging_prefix=staging_prefix,
    )
    staging = boundary.staging
    if staging is None:
        boundary.close()
        raise ValueError(f"{label} staging is unavailable")
    staged_proof = staging / proof_name
    staged_receipt = staging / receipt_name
    proof_handle: BinaryIO | None = None
    receipt_handle: BinaryIO | None = None
    proof_identity: tuple[int, int] | None = None
    receipt_identity: tuple[int, int] | None = None
    published: dict[str, tuple[Path, tuple[int, int]]] = {}
    archive_artifacts = {
        "proof": staged_proof,
        "receipt": staged_receipt,
    }
    try:
        boundary.assert_unchanged()
        proof_handle, proof_identity, proof_body = _create_classroom_json_staging(
            boundary,
            parent_key="staging/attempt",
            path=staged_proof,
            document=proof,
        )
        proof_sha256 = hashlib.sha256(proof_body).hexdigest()
        provenance = {
            provenance_key: {
                "artifact": f"runtime/{proof_name}",
                "sha256": proof_sha256,
            }
        }
        if outage_path is not None:
            assert outage_sha256 is not None
            assert observer_sha256 is not None
            provenance["openmaicDedicatedOutageAttestation"] = {
                "artifact": "runtime/openmaic-dedicated-outage-attestation.json",
                "sha256": outage_sha256,
            }
            provenance["openmaicSharedIngressObserverAttestation"] = {
                "artifact": "runtime/openmaic-shared-ingress-observer-attestation.json",
                "sha256": observer_sha256,
            }
        receipt = _pass_receipt_from_candidate(
            candidate=candidate,
            release_run=bound_run,
            evidence=evidence,
            observed_at=observed_at,
            native_exit=0,
            checks=checks,
            provenance=provenance,
        )
        receipt_handle, receipt_identity, receipt_body = _create_classroom_json_staging(
            boundary,
            parent_key="staging/attempt",
            path=staged_receipt,
            document=receipt,
        )
        if any(secret in proof_body or secret in receipt_body for secret in secrets):
            raise ValueError(f"{label} evidence contains a live fixture token")
        replayed_report = parse_openmaic_smoke_report(
            stdout,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
            expected_runtime_attestation_sha256=runtime_sha256,
            forbidden_secret_values=secrets,
            expected_plane=plane,
        )
        if replayed_report != report or derive_checks(replayed_report) != smoke_checks:
            raise ValueError(f"{label} proof replay changed")
        assert_release_binding()
        boundary.assert_unchanged()
        if any(os.path.lexists(path) for path in (proof_path, receipt_path)):
            raise ValueError(f"{label} publication target appeared concurrently")
        for name, staged, target, source_handle, expected_body, expected_identity in (
            (
                "receipt",
                staged_receipt,
                receipt_path,
                receipt_handle,
                receipt_body,
                receipt_identity,
            ),
            (
                "proof",
                staged_proof,
                proof_path,
                proof_handle,
                proof_body,
                proof_identity,
            ),
        ):
            boundary.assert_unchanged()
            assert source_handle is not None
            assert expected_identity is not None
            published[f"published-{name}"] = (target, expected_identity)
            _publish_classroom_no_replace(
                boundary,
                staged,
                target,
                source_handle=source_handle,
            )
            boundary.assert_unchanged()
            _assert_published_classroom_receipt_relative(
                boundary,
                target,
                expected_body=expected_body,
                expected_identity=expected_identity,
                label=f"{label} {name}",
            )
            assert_release_binding()
        boundary.assert_unchanged()
        assert_release_binding()
        return receipt
    except BaseException as original_error:
        if proof_handle is not None:
            proof_handle.close()
            proof_handle = None
        if receipt_handle is not None:
            receipt_handle.close()
            receipt_handle = None
        cleanup_error: Exception | None = None
        try:
            _remove_classroom_entries(
                boundary,
                {path: identity for path, identity in published.values()},
                label=f"{label} formal evidence",
            )
        except Exception as exc:
            cleanup_error = exc
        archive_error: Exception | None = None
        if isinstance(original_error, Exception):
            try:
                boundary.assert_unchanged()
                _record_probe_failure(
                    bundle_root=root,
                    evidence=staging_prefix,
                    recipe="live-first-release",
                    attempt_id=attempt_id,
                    reason=f"{label} execution or publication failed",
                    native_exit=0,
                    artifacts=archive_artifacts,
                )
            except Exception as exc:
                archive_error = exc
        if archive_error is not None:
            if cleanup_error is not None:
                archive_error.add_note(f"formal evidence retraction also failed: {cleanup_error}")
            raise original_error from archive_error
        if cleanup_error is not None:
            raise original_error from cleanup_error
        raise
    finally:
        if proof_handle is not None:
            proof_handle.close()
        if receipt_handle is not None:
            receipt_handle.close()
        active_error = sys.exception()
        staging_cleanup_error: Exception | None = None
        try:
            _remove_classroom_entries(
                boundary,
                {
                    staged_proof: proof_identity,
                    staged_receipt: receipt_identity,
                },
                label=f"{label} staging evidence",
            )
        except Exception as exc:
            staging_cleanup_error = exc
            if active_error is not None:
                active_error.add_note(f"staging evidence cleanup failed: {exc}")
        finally:
            boundary.close()
            try:
                staging.rmdir()
            except OSError:
                pass
        if staging_cleanup_error is not None and active_error is None:
            raise staging_cleanup_error


def write_openmaic_shared_plane_receipt(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    runner: CommandRunner = _run_openmaic_smoke,
) -> dict[str, object]:
    """Run the fixed shared-plane smoke and publish its proof marker last."""

    return _write_openmaic_plane_receipt(
        plane="shared",
        candidate_root=candidate_root,
        bundle_root=bundle_root,
        release_run=release_run,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )


def _publish_backup_restore_receipt(
    boundary: _BackupRestorePublicationBoundary,
    document: Mapping[str, object],
) -> dict[str, object]:
    target = boundary.root / "artifacts" / "backup_restore.json"
    staged = target.parent / f".backup-restore-{uuid.uuid4().hex}.tmp"
    staged_handle: BinaryIO | None = None
    staged_identity: tuple[int, int] | None = None
    published: dict[Path, tuple[int, int]] = {}
    committed = False
    primary_error: BaseException | None = None
    try:
        boundary.assert_unchanged()
        staged_handle, staged_identity, body = _create_classroom_json_staging(
            boundary,
            parent_key="artifacts",
            path=staged,
            document=document,
        )
        boundary.assert_unchanged()
        try:
            _publish_classroom_no_replace(
                boundary,
                staged,
                target,
                source_handle=staged_handle,
            )
        except OSError as exc:
            if (
                isinstance(exc, FileExistsError)
                or exc.errno in {errno.EEXIST, 80, 183}
                or getattr(exc, "winerror", None) in {80, 183}
            ):
                raise FileExistsError("backup restore receipt already exists") from exc
            raise
        published[target] = staged_identity
        _assert_published_classroom_receipt_relative(
            boundary,
            target,
            expected_body=body,
            expected_identity=staged_identity,
            label="backup restore receipt",
        )
        boundary.assert_unchanged()
        if os.name != "nt":
            os.fsync(int(boundary.leases["artifacts"].handle))
        boundary.assert_unchanged()
        boundary.mark_committed()
        committed = True
        return dict(document)
    except BaseException as error:
        primary_error = error
        if staged_handle is not None:
            try:
                staged_handle.close()
            except BaseException as cleanup_error:
                error.add_note(f"backup restore staging cleanup failed: {cleanup_error}")
            staged_handle = None
        try:
            _remove_classroom_entries(
                boundary,
                published,
                label="backup restore formal evidence",
            )
        except BaseException as cleanup_error:
            error.add_note(f"backup restore formal evidence cleanup failed: {cleanup_error}")
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if staged_handle is not None:
            try:
                staged_handle.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if staged_identity is not None:
            try:
                _remove_classroom_entries(
                    boundary,
                    {staged: staged_identity},
                    label="backup restore staging evidence",
                )
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            if primary_error is not None:
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"backup restore staging cleanup failed: {cleanup_error}"
                    )
            elif not committed:
                raise cleanup_errors[0]


def write_backup_restore_receipt(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    database_ownership: str,
    object_namespace_ownership: str,
) -> dict[str, object]:
    """Wrap one completed fixed backup/restore probe in a formal receipt."""

    if (
        database_ownership != "runner-owned-disposable"
        or object_namespace_ownership != "runner-owned-disposable"
    ):
        raise ValueError("backup restore release targets must be runner-owned disposable")
    candidate = _candidate(candidate_root)
    bound_run = _release_run(release_run)
    root = Path(os.path.abspath(bundle_root))
    boundary = _BackupRestorePublicationBoundary.open(root)
    try:
        boundary.assert_unchanged()
        report_path = root / "runtime" / "backup-restore" / "backup-restore-report.json"
        report_body, report_sha256 = read_backup_restore_report_artifact(
            report_path,
            bundle_root=root,
        )
        checks, observed_at = derive_backup_restore_receipt_checks(
            report_body,
            bundle_root=root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=bound_run,
            expected_database_ownership=database_ownership,
            expected_object_namespace_ownership=object_namespace_ownership,
        )
        boundary.assert_unchanged()
        if checks != derive_backup_restore_checks(json.loads(report_body)):
            raise ValueError("backup restore receipt checks are inconsistent")
        document = _pass_receipt_from_candidate(
            candidate=candidate,
            release_run=bound_run,
            evidence="backup_restore",
            observed_at=observed_at,
            native_exit=0,
            checks=checks,
            provenance={
                "backupRestoreReport": {
                    "artifact": "runtime/backup-restore/backup-restore-report.json",
                    "sha256": report_sha256,
                }
            },
        )
        return _publish_backup_restore_receipt(boundary, document)
    finally:
        active_error = sys.exception()
        try:
            boundary.close()
        except BaseException as cleanup_error:
            if active_error is not None:
                active_error.add_note(
                    f"backup restore publication boundary cleanup failed: {cleanup_error}"
                )
            elif not boundary.committed:
                raise


def write_openmaic_dedicated_plane_receipt(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    expected_outage_docker_host_identity_sha256: str,
    runner: CommandRunner = _run_openmaic_smoke,
) -> dict[str, object]:
    """Run the fixed dedicated-plane smoke and publish its proof marker last."""

    return _write_openmaic_plane_receipt(
        plane="dedicated",
        candidate_root=candidate_root,
        bundle_root=bundle_root,
        release_run=release_run,
        timeout_seconds=timeout_seconds,
        runner=runner,
        expected_outage_docker_host_identity_sha256=(expected_outage_docker_host_identity_sha256),
    )


def write_gateway_only_public_receipt(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    expected_docker_host_identity_sha256: str | None = None,
    trusted_keyring_path: Path | None = None,
    expected_trusted_keyring_sha256: str | None = None,
    expected_observer_challenge: str | None = None,
    expected_host_challenge: str | None = None,
    trusted_now: str | None = None,
    runner: CommandRunner = _run_gateway_docker,
    docker_resolver: Callable[[], Path] = resolve_fixed_docker,
) -> dict[str, object]:
    """Replay the external probe and bind it to two fixed Docker observations."""

    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ValueError("gateway observation timeout is invalid")
    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + timeout_seconds
    if not math.isfinite(started_monotonic) or not math.isfinite(deadline_monotonic):
        raise ValueError("gateway observation deadline is invalid")
    expected_host_identity_sha256 = _gateway_expected_docker_host_identity_sha256(
        expected_docker_host_identity_sha256
    )
    if not isinstance(trusted_keyring_path, (str, os.PathLike)):
        raise ValueError("gateway trust keyring path is unavailable or invalid")
    normalized_trusted_keyring_path = Path(trusted_keyring_path)
    root = Path(os.path.abspath(bundle_root))
    candidate_path = Path(os.path.abspath(candidate_root))
    with _GatewayPublicationBoundary.open(root, candidate_path) as boundary:
        return _write_gateway_only_public_receipt_bound(
            root=root,
            candidate_path=candidate_path,
            release_run=release_run,
            deadline_monotonic=deadline_monotonic,
            expected_host_identity_sha256=expected_host_identity_sha256,
            trusted_keyring_path=normalized_trusted_keyring_path,
            expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
            expected_observer_challenge=expected_observer_challenge,
            expected_host_challenge=expected_host_challenge,
            trusted_now=trusted_now,
            boundary=boundary,
            runner=runner,
            docker_resolver=docker_resolver,
        )


def _write_gateway_only_public_receipt_bound(
    *,
    root: Path,
    candidate_path: Path,
    release_run: Mapping[str, object],
    deadline_monotonic: float,
    expected_host_identity_sha256: str,
    trusted_keyring_path: Path,
    expected_trusted_keyring_sha256: str | None,
    expected_observer_challenge: str | None,
    expected_host_challenge: str | None,
    trusted_now: str | None,
    boundary: _GatewayPublicationBoundary,
    runner: CommandRunner,
    docker_resolver: Callable[[], Path],
) -> dict[str, object]:
    bound_run = _release_run(release_run)
    proof_path = root / "runtime" / "gateway-only-public-attestation.json"
    receipt_path = root / "artifacts" / "gateway_only_public.json"
    report_path = root / "raw" / "gateway-public-observation.json"
    observer_path = root / "runtime" / "gateway-external-observer-attestation.json"
    observer_envelope_path = root / "runtime" / "gateway-observer-trust-envelope.json"
    host_envelope_path = root / "runtime" / "gateway-host-provisioner-trust-envelope.json"
    host_receipt_path = root / "runtime" / "gateway-docker-host-provisioning-receipt.json"
    runtime_path = root / "runtime" / "runtime-attestation.json"
    candidate_inputs = (
        candidate_path / "deploy" / "image-lock.json",
        candidate_path / "docker-compose.platform.yml",
        candidate_path / "docker-compose.data-plane.yml",
    )
    if any(os.path.lexists(path) for path in (proof_path, receipt_path)):
        raise ValueError("gateway-only-public evidence already exists")

    pinned_inputs = {
        "external observation": _read_gateway_input(
            report_path,
            label="external observation",
        ),
        "observer attestation": _read_gateway_input(
            observer_path,
            label="observer attestation",
        ),
        "trust observer envelope": _read_gateway_input(
            observer_envelope_path,
            label="trust observer envelope",
        ),
        "trust host-provisioner envelope": _read_gateway_input(
            host_envelope_path,
            label="trust host-provisioner envelope",
        ),
        "trust host provisioning receipt": _read_gateway_input(
            host_receipt_path,
            label="trust host provisioning receipt",
        ),
        "runtime attestation": _read_gateway_input(
            runtime_path,
            label="runtime attestation",
        ),
        "candidate image lock": _read_gateway_input(
            candidate_inputs[0],
            label="candidate image lock",
        ),
        "candidate platform Compose": _read_gateway_input(
            candidate_inputs[1],
            label="candidate platform Compose",
        ),
        "candidate data-plane Compose": _read_gateway_input(
            candidate_inputs[2],
            label="candidate data-plane Compose",
        ),
    }
    boundary.assert_unchanged()
    candidate = _candidate(candidate_path)
    verified_trust = verify_gateway_trust_pair(
        observer_envelope_body=pinned_inputs["trust observer envelope"].body,
        host_envelope_body=pinned_inputs["trust host-provisioner envelope"].body,
        observer_artifact_body=pinned_inputs["observer attestation"].body,
        host_receipt_body=pinned_inputs["trust host provisioning receipt"].body,
        trusted_keyring_path=trusted_keyring_path,
        expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_environment_id=bound_run["environmentId"],
        expected_observer_challenge=expected_observer_challenge,
        expected_host_challenge=expected_host_challenge,
        trusted_now=trusted_now,
    )
    observer_trust_payload = verified_trust.get("observer")
    if not isinstance(observer_trust_payload, Mapping):
        raise ValueError("gateway signed observer policy is invalid")
    observer_policy = signed_gateway_observer_policy(
        pinned_inputs["observer attestation"].body,
        observer_trust_payload,
    )
    host_receipt = verified_trust.get("hostReceipt")
    trusted_host = host_receipt.get("host") if isinstance(host_receipt, dict) else None
    host_receipt_sha256 = hashlib.sha256(
        pinned_inputs["trust host provisioning receipt"].body
    ).hexdigest()
    if host_receipt_sha256 != expected_host_identity_sha256:
        raise ValueError(
            "gateway trust host receipt digest does not match expected Docker host identity"
        )
    if not isinstance(trusted_host, dict):
        raise ValueError("gateway trust host receipt identity is invalid")
    trusted_daemon = {
        "context": trusted_host.get("dockerContext"),
        "endpoint": trusted_host.get("dockerEndpoint"),
        "serverId": trusted_host.get("dockerServerId"),
        "osType": trusted_host.get("osType"),
    }
    runtime_body, runtime_sha256 = read_runtime_attestation_artifact(
        runtime_path,
        bundle_root=root,
    )
    if runtime_body != pinned_inputs["runtime attestation"].body:
        raise ValueError("gateway runtime attestation identity changed")
    runtime = validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=runtime_sha256,
        expected_docker_host_identity_sha256=expected_host_identity_sha256,
    )
    runtime_host_identity = _gateway_runtime_host_identity(
        runtime,
        expected_sha256=expected_host_identity_sha256,
    )
    if runtime_host_identity != {
        "context": trusted_daemon["context"],
        "endpoint": trusted_daemon["endpoint"],
        "serverId": trusted_daemon["serverId"],
        "dockerHostIdentitySha256": host_receipt_sha256,
    }:
        raise ValueError("gateway trust host receipt does not match runtime identity")
    base_url = runtime.get("baseUrl") if isinstance(runtime, dict) else None
    if not isinstance(base_url, str):
        raise ValueError("gateway runtime base URL is invalid")
    report = parse_gateway_public_report(
        pinned_inputs["external observation"].body,
        observer_attestation_body=pinned_inputs["observer attestation"].body,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_runtime_attestation_sha256=runtime_sha256,
        expected_observer_id=observer_policy["expected_observer_id"],
        expected_observer_origin=observer_policy["expected_observer_origin"],
        expected_attestation_sha256=observer_policy["expected_attestation_sha256"],
        trusted_now=trusted_now,
        run_started_at=observer_policy["run_started_at"],
        run_ended_at=observer_policy["run_ended_at"],
    )
    checks = derive_gateway_public_checks(report)
    if checks != {"gatewayPublic": True, "internalPortsClosed": True}:
        raise ValueError("gateway external observation did not prove the fixed policy")
    runtime_containers = _gateway_runtime_containers(runtime)
    expected_service_networks = parse_gateway_candidate_networks(
        pinned_inputs["candidate platform Compose"].body,
        docker_project=_GATEWAY_DOCKER_PROJECT,
        expected_services=tuple(runtime_containers.values()),
    )
    docker = Path(docker_resolver())
    if not docker.is_absolute():
        raise ValueError("gateway trusted Docker CLI is invalid")

    with tempfile.TemporaryDirectory(prefix="yfeistai-gateway-docker-") as raw_config:
        docker_config = Path(raw_config)
        before_daemon, before_snapshot, before_commands = _gateway_observation_round(
            docker=docker,
            docker_config=docker_config,
            candidate_root=candidate_path,
            deadline_monotonic=deadline_monotonic,
            runner=runner,
            runtime_containers=runtime_containers,
            expected_service_networks=expected_service_networks,
        )
        after_daemon, after_snapshot, after_commands = _gateway_observation_round(
            docker=docker,
            docker_config=docker_config,
            candidate_root=candidate_path,
            deadline_monotonic=deadline_monotonic,
            runner=runner,
            runtime_containers=runtime_containers,
            expected_service_networks=expected_service_networks,
        )
    if before_daemon["endpoint"] != after_daemon["endpoint"]:
        raise ValueError("gateway Docker daemon endpoint changed")
    if (
        before_daemon["serverId"] != after_daemon["serverId"]
        or before_daemon["osType"] != after_daemon["osType"]
    ):
        raise ValueError("gateway Docker daemon identity changed")
    if before_daemon != trusted_daemon:
        raise ValueError("gateway Docker host identity is invalid")
    if before_snapshot != after_snapshot:
        raise ValueError("gateway Docker container identity changed")

    boundary.assert_unchanged()
    for label, pinned in pinned_inputs.items():
        pinned.assert_unchanged(label=label)
        boundary.assert_unchanged()
    if _candidate(candidate_path) != candidate:
        raise ValueError("gateway candidate binding changed during observation")
    runtime_after, runtime_after_sha256 = read_runtime_attestation_artifact(
        runtime_path,
        bundle_root=root,
    )
    if runtime_after != runtime_body or runtime_after_sha256 != runtime_sha256:
        raise ValueError("gateway runtime attestation changed during observation")
    validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_sha256=runtime_sha256,
        expected_docker_host_identity_sha256=expected_host_identity_sha256,
    )
    replayed = parse_gateway_public_report(
        pinned_inputs["external observation"].body,
        observer_attestation_body=pinned_inputs["observer attestation"].body,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_runtime_attestation_sha256=runtime_sha256,
        expected_observer_id=observer_policy["expected_observer_id"],
        expected_observer_origin=observer_policy["expected_observer_origin"],
        expected_attestation_sha256=observer_policy["expected_attestation_sha256"],
        trusted_now=trusted_now,
        run_started_at=observer_policy["run_started_at"],
        run_ended_at=observer_policy["run_ended_at"],
    )
    if replayed != report or derive_gateway_public_checks(replayed) != checks:
        raise ValueError("gateway external observation replay changed")
    boundary.assert_unchanged()

    observed_at = report.get("observedAt")
    if not isinstance(observed_at, str):
        raise ValueError("gateway external observation timestamp is invalid")
    proof: dict[str, object] = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": bound_run,
        "observedAt": observed_at,
        "baseUrl": base_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        },
        "observerAttestation": {
            "artifact": "runtime/gateway-external-observer-attestation.json",
            "sha256": hashlib.sha256(pinned_inputs["observer attestation"].body).hexdigest(),
        },
        "externalObservation": {
            "artifact": "raw/gateway-public-observation.json",
            "sha256": hashlib.sha256(pinned_inputs["external observation"].body).hexdigest(),
        },
        "trustPair": {
            "observerEnvelope": {
                "artifact": "runtime/gateway-observer-trust-envelope.json",
                "sha256": hashlib.sha256(pinned_inputs["trust observer envelope"].body).hexdigest(),
            },
            "hostProvisionerEnvelope": {
                "artifact": "runtime/gateway-host-provisioner-trust-envelope.json",
                "sha256": hashlib.sha256(
                    pinned_inputs["trust host-provisioner envelope"].body
                ).hexdigest(),
            },
            "hostProvisioningReceipt": {
                "artifact": "runtime/gateway-docker-host-provisioning-receipt.json",
                "sha256": host_receipt_sha256,
            },
        },
        "docker": {
            "project": _GATEWAY_DOCKER_PROJECT,
            "daemon": {
                **before_daemon,
                "dockerHostIdentitySha256": expected_host_identity_sha256,
            },
            "beforeSnapshot": before_snapshot,
            "afterSnapshot": after_snapshot,
            "commands": [*before_commands, *after_commands],
        },
        "summary": {"checks": checks},
    }
    boundary.ensure_publication_parents()
    boundary.assert_unchanged()
    staged_receipt: Path | None = None
    staged_proof: Path | None = None
    receipt_handle: BinaryIO | None = None
    proof_handle: BinaryIO | None = None
    receipt_identity: tuple[int, int] | None = None
    proof_identity: tuple[int, int] | None = None
    published: dict[Path, tuple[int, int]] = {}
    committed = False
    try:
        staged_proof, proof_body, proof_identity = _stage_gateway_json_bound(
            boundary,
            proof_path.parent,
            proof,
        )
        boundary.assert_unchanged()
        proof_handle = _open_gateway_staged_input(
            boundary,
            staged_proof,
            body=proof_body,
            identity=proof_identity,
        )
        receipt = _pass_receipt_from_candidate(
            candidate=candidate,
            release_run=bound_run,
            evidence="gateway_only_public",
            observed_at=observed_at,
            native_exit=0,
            checks=checks,
            provenance={
                "gatewayOnlyPublicAttestation": {
                    "artifact": "runtime/gateway-only-public-attestation.json",
                    "sha256": hashlib.sha256(proof_body).hexdigest(),
                }
            },
        )
        staged_receipt, receipt_body, receipt_identity = _stage_gateway_json_bound(
            boundary,
            receipt_path.parent,
            receipt,
        )
        boundary.assert_unchanged()
        receipt_handle = _open_gateway_staged_input(
            boundary,
            staged_receipt,
            body=receipt_body,
            identity=receipt_identity,
        )
        publication_context = _GATEWAY_PUBLICATION_CONTEXT.set(
            _GatewayPublicationContext(
                boundary=boundary,
                source_handles={
                    staged_receipt: receipt_handle,
                    staged_proof: proof_handle,
                },
            )
        )
        try:
            for staged, target, body, identity in (
                (staged_receipt, receipt_path, receipt_body, receipt_identity),
                (staged_proof, proof_path, proof_body, proof_identity),
            ):
                boundary.assert_unchanged()
                if os.path.lexists(target):
                    raise ValueError("gateway-only-public publication target appeared concurrently")
                published[target] = identity
                _publish_no_replace(staged, target)
                _assert_gateway_published(target, body=body, identity=identity)
                _assert_published_classroom_receipt_relative(
                    boundary,
                    target,
                    expected_body=body,
                    expected_identity=identity,
                    label="gateway-only-public evidence",
                )
                boundary.assert_unchanged()
        finally:
            _GATEWAY_PUBLICATION_CONTEXT.reset(publication_context)
        boundary.assert_unchanged()
        boundary.mark_committed()
        committed = True
        return receipt
    except BaseException as original_error:
        cleanup_errors: list[Exception] = []
        if receipt_handle is not None:
            try:
                receipt_handle.close()
            except Exception as exc:
                cleanup_errors.append(exc)
            receipt_handle = None
        if proof_handle is not None:
            try:
                proof_handle.close()
            except Exception as exc:
                cleanup_errors.append(exc)
            proof_handle = None
        try:
            _remove_classroom_entries(
                boundary,
                published,
                label="gateway formal evidence",
            )
        except Exception as cleanup_error:
            cleanup_errors.append(cleanup_error)
        for cleanup_error in cleanup_errors:
            original_error.add_note(f"gateway formal evidence cleanup failed: {cleanup_error}")
        raise
    finally:
        active_error = sys.exception()
        staging_cleanup_errors: list[Exception] = []
        for handle in (receipt_handle, proof_handle):
            if handle is not None:
                try:
                    handle.close()
                except Exception as exc:
                    staging_cleanup_errors.append(exc)
        staged_entries = {
            staged: identity
            for staged, identity in (
                (staged_receipt, receipt_identity),
                (staged_proof, proof_identity),
            )
            if staged is not None
        }
        try:
            _remove_classroom_entries(
                boundary,
                staged_entries,
                label="gateway staging evidence",
            )
        except Exception as exc:
            staging_cleanup_errors.append(exc)
        if staging_cleanup_errors:
            if active_error is not None:
                for cleanup_error in staging_cleanup_errors:
                    active_error.add_note(f"gateway staging cleanup failed: {cleanup_error}")
            elif not committed:
                raise staging_cleanup_errors[0]


def write_tenant_isolation_receipt(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    runner: CommandRunner = _run_tenant_isolation,
) -> dict[str, object]:
    """Run the fixed tenant-isolation probe and publish its proof marker last."""

    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= _PROBE_CLEANUP_MARGIN_SECONDS
    ):
        raise ValueError("tenant isolation timeout is invalid")
    token = os.environ.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("tenant isolation live fixture token is unavailable")
    secrets = tuple(
        value.encode("utf-8", errors="strict") for value in {token, token.strip()} if value
    )
    root = Path(os.path.abspath(bundle_root))
    candidate_path = Path(os.path.abspath(candidate_root))
    _assert_no_link_ancestors(root)
    _assert_no_link_ancestors(candidate_path)
    proof_path = root / "runtime" / "tenant-isolation-attestation.json"
    receipt_path = root / "artifacts" / "tenant_isolation.json"
    if any(os.path.lexists(path) for path in (proof_path, receipt_path)):
        raise ValueError("tenant isolation evidence already exists")

    candidate = _candidate(candidate_path)
    bound_run = _release_run(release_run)
    runtime_path = root / "runtime" / "runtime-attestation.json"
    runtime_body, runtime_sha256 = read_runtime_attestation_artifact(
        runtime_path,
        bundle_root=root,
    )
    runtime = validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=runtime_sha256,
    )
    base_url = runtime.get("baseUrl")
    if not isinstance(base_url, str):
        raise ValueError("tenant isolation runtime URL is invalid")

    capacity_path = root / "runtime" / "capacity-profile-attestation.json"
    capacity_body, capacity_sha256 = read_capacity_profile_attestation_artifact(
        capacity_path,
        bundle_root=root,
    )
    capacity_tenant_ids = derive_capacity_profile_tenant_ids(
        capacity_body,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
    )
    if len(capacity_tenant_ids) < 2:
        raise ValueError("tenant isolation capacity tenant pair is unavailable")
    selected_tenant_ids = capacity_tenant_ids[:2]
    if len(set(selected_tenant_ids)) != 2 or selected_tenant_ids != tuple(
        sorted(selected_tenant_ids)
    ):
        raise ValueError("tenant isolation capacity tenant pair is invalid")

    logical_command = tenant_isolation_command_record()
    arguments = [
        sys.executable,
        str(SCRIPTS_ROOT / "tenant_isolation_probe.py"),
        "--profile",
        "first-release",
    ]
    allowed_environment = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "WINDIR",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_environment
    }
    environment.update(
        {
            "YFEISTAI_LIVE_FIXTURE_TOKEN": token,
            "YFEISTAI_CANDIDATE_ROOT": str(candidate_path),
            "YFEISTAI_RELEASE_RUN_ID": bound_run["runId"],
            "YFEISTAI_ENVIRONMENT_ID": bound_run["environmentId"],
            "YFEISTAI_TENANT_ISOLATION_TIMEOUT_SECONDS": str(
                timeout_seconds - _PROBE_CLEANUP_MARGIN_SECONDS
            ),
            "YFEISTAI_CAPACITY_ATTESTATION_PATH": str(capacity_path),
            "YFEISTAI_CAPACITY_ATTESTATION_SHA256": capacity_sha256,
            "YFEISTAI_CAPACITY_TENANT_IDS": json.dumps(
                selected_tenant_ids,
                separators=(",", ":"),
            ),
            "WEB_BASE_URL": base_url,
        }
    )
    try:
        completed = runner(
            arguments,
            cwd=candidate_path,
            env=environment,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ValueError("tenant isolation probe could not run") from exc
    if (
        not isinstance(completed.returncode, int)
        or isinstance(completed.returncode, bool)
        or completed.returncode != 0
        or completed.args != arguments
        or not isinstance(completed.stdout, str)
        or completed.stderr != ""
    ):
        raise ValueError("tenant isolation probe did not exit cleanly")
    try:
        stdout = completed.stdout.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("tenant isolation probe output is invalid") from exc
    if any(secret in stdout for secret in secrets):
        raise ValueError("tenant isolation probe serialized a live fixture secret")
    report = parse_tenant_isolation_report(
        stdout,
        candidate=candidate,
        release_run=bound_run,
        expected_base_url=base_url,
        expected_capacity_report_sha256=capacity_sha256,
        expected_capacity_tenant_ids=selected_tenant_ids,
        forbidden_secret_values=secrets,
    )
    checks = derive_tenant_isolation_checks(report)
    if any(value is not True for value in checks.values()):
        raise ValueError("tenant isolation probe did not prove isolation")
    observed_at = report.get("observedAt")
    if not isinstance(observed_at, str) or not _valid_observed_at(observed_at):
        raise ValueError("tenant isolation observedAt is invalid")

    def assert_release_binding() -> None:
        try:
            candidate_after = _candidate(candidate_path)
            runtime_after, runtime_after_sha256 = read_runtime_attestation_artifact(
                runtime_path,
                bundle_root=root,
            )
            capacity_after, capacity_after_sha256 = read_capacity_profile_attestation_artifact(
                capacity_path,
                bundle_root=root,
            )
        except ValueError as exc:
            raise ValueError("tenant isolation release binding changed") from exc
        if (
            candidate_after != candidate
            or runtime_after != runtime_body
            or runtime_after_sha256 != runtime_sha256
            or capacity_after != capacity_body
            or capacity_after_sha256 != capacity_sha256
        ):
            raise ValueError("tenant isolation release binding changed")
        validate_runtime_attestation(
            runtime_path,
            bundle_root=root,
            candidate_root=candidate_path,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
            expected_sha256=runtime_sha256,
        )
        replayed_tenant_ids = derive_capacity_profile_tenant_ids(
            capacity_after,
            bundle_root=root,
            candidate_root=candidate_path,
            candidate=candidate,
            release_run=bound_run,
        )
        if replayed_tenant_ids[:2] != selected_tenant_ids:
            raise ValueError("tenant isolation capacity tenant pair changed")

    assert_release_binding()
    proof = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": bound_run,
        "observedAt": observed_at,
        "baseUrl": base_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        },
        "capacityAttestation": {
            "artifact": "runtime/capacity-profile-attestation.json",
            "sha256": capacity_sha256,
        },
        "execution": {
            "command": logical_command,
            "nativeExit": 0,
            "stdout": completed.stdout,
            "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
            "stderr": "",
        },
        "summary": {"checks": checks},
    }
    attempt_id = uuid.uuid4().hex
    boundary = _TenantPublicationBoundary.open(root, attempt_id)
    staging = boundary.staging
    staged_proof = staging / "tenant-isolation-attestation.json"
    staged_receipt = staging / "tenant_isolation.json"
    proof_handle: BinaryIO | None = None
    receipt_handle: BinaryIO | None = None
    proof_identity: tuple[int, int] | None = None
    receipt_identity: tuple[int, int] | None = None
    published: dict[str, tuple[Path, tuple[int, int]]] = {}
    archive_artifacts: dict[str, Path] = {}
    try:
        boundary.assert_unchanged()
        proof_handle, proof_identity, proof_body = _create_classroom_json_staging(
            boundary,
            parent_key="staging/attempt",
            path=staged_proof,
            document=proof,
        )
        if any(secret in proof_body for secret in secrets):
            raise ValueError("tenant isolation proof contains a live fixture secret")
        proof_sha256 = hashlib.sha256(proof_body).hexdigest()
        replayed_checks, replayed_observed_at = derive_tenant_isolation_receipt_checks(
            proof_body,
            bundle_root=root,
            candidate_root=candidate_path,
            candidate=candidate,
            release_run=bound_run,
        )
        if replayed_checks != checks or replayed_observed_at != observed_at:
            raise ValueError("tenant isolation proof replay changed")
        receipt = _pass_receipt_from_candidate(
            candidate=candidate,
            release_run=bound_run,
            evidence="tenant_isolation",
            observed_at=observed_at,
            native_exit=0,
            checks=replayed_checks,
            provenance={
                "tenantIsolationAttestation": {
                    "artifact": "runtime/tenant-isolation-attestation.json",
                    "sha256": proof_sha256,
                }
            },
        )
        receipt_handle, receipt_identity, receipt_body = _create_classroom_json_staging(
            boundary,
            parent_key="staging/attempt",
            path=staged_receipt,
            document=receipt,
        )
        if any(secret in receipt_body for secret in secrets):
            raise ValueError("tenant isolation receipt contains a live fixture secret")
        archive_artifacts = {
            "proof": staged_proof,
            "receipt": staged_receipt,
        }
        assert_release_binding()
        boundary.assert_unchanged()
        if any(os.path.lexists(path) for path in (proof_path, receipt_path)):
            raise ValueError("tenant isolation publication target appeared concurrently")
        for name, staged, target, source_handle, expected_body, expected_identity in (
            (
                "receipt",
                staged_receipt,
                receipt_path,
                receipt_handle,
                receipt_body,
                receipt_identity,
            ),
            (
                "proof",
                staged_proof,
                proof_path,
                proof_handle,
                proof_body,
                proof_identity,
            ),
        ):
            boundary.assert_unchanged()
            assert source_handle is not None
            assert expected_identity is not None
            published[f"published-{name}"] = (target, expected_identity)
            _publish_classroom_no_replace(
                boundary,
                staged,
                target,
                source_handle=source_handle,
            )
            boundary.assert_unchanged()
            _assert_published_classroom_receipt(
                target,
                expected_body=expected_body,
                expected_identity=expected_identity,
                label=f"tenant isolation {name}",
            )
            assert_release_binding()
        boundary.assert_unchanged()
        return receipt
    except BaseException as original_error:
        if proof_handle is not None:
            proof_handle.close()
            proof_handle = None
        if receipt_handle is not None:
            receipt_handle.close()
            receipt_handle = None
        cleanup_error: Exception | None = None
        try:
            _remove_classroom_entries(
                boundary,
                {path: identity for path, identity in published.values()},
                label="tenant isolation formal evidence",
            )
        except Exception as exc:
            cleanup_error = exc
        archive_error: Exception | None = None
        if isinstance(original_error, Exception):
            try:
                boundary.assert_unchanged()
                _record_probe_failure(
                    bundle_root=root,
                    evidence="tenant-isolation",
                    recipe="live-first-release",
                    attempt_id=attempt_id,
                    reason="tenant isolation execution or publication failed",
                    native_exit=0,
                    artifacts=archive_artifacts,
                )
            except Exception as exc:
                archive_error = exc
        if archive_error is not None:
            if cleanup_error is not None:
                archive_error.add_note(f"formal evidence retraction also failed: {cleanup_error}")
            raise original_error from archive_error
        if cleanup_error is not None:
            raise original_error from cleanup_error
        raise
    finally:
        if proof_handle is not None:
            proof_handle.close()
        if receipt_handle is not None:
            receipt_handle.close()
        active_error = sys.exception()
        staging_cleanup_error: Exception | None = None
        try:
            _remove_classroom_entries(
                boundary,
                {
                    staged_proof: proof_identity,
                    staged_receipt: receipt_identity,
                },
                label="tenant isolation staging evidence",
            )
        except Exception as exc:
            staging_cleanup_error = exc
            if active_error is not None:
                active_error.add_note(f"staging evidence cleanup failed: {exc}")
        finally:
            boundary.close()
            try:
                staging.rmdir()
            except OSError:
                pass
        if staging_cleanup_error is not None and active_error is None:
            raise staging_cleanup_error


def write_classroom_exports_receipt(
    *,
    candidate_root: Path,
    bundle_root: Path,
    release_run: Mapping[str, object],
    timeout_seconds: int,
    runner: CommandRunner = _run_classroom_exports,
) -> dict[str, object]:
    """Run the fixed classroom export probe and publish its proof marker last."""

    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= _PROBE_CLEANUP_MARGIN_SECONDS
    ):
        raise ValueError("classroom exports timeout is invalid")
    token = os.environ.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("classroom exports live fixture token is unavailable")
    secrets = {value.encode("utf-8", errors="strict") for value in {token, token.strip()} if value}
    root = Path(os.path.abspath(bundle_root))
    _assert_classroom_fixed_parents(root)
    candidate_path = Path(candidate_root).resolve()
    proof_path = root / "runtime" / "classroom-exports-attestation.json"
    receipt_path = root / "artifacts" / "classroom_exports.json"
    raw_root = root / "raw" / "classroom-exports"
    raw_targets = {kind: raw_root / name for kind, name in CLASSROOM_EXPORT_PATHS.items()}
    if any(os.path.lexists(path) for path in (proof_path, receipt_path)):
        raise ValueError("classroom exports evidence already exists")

    candidate = _candidate(candidate_path)
    bound_run = _release_run(release_run)
    runtime_path = root / "runtime" / "runtime-attestation.json"
    runtime_body, runtime_sha256 = read_runtime_attestation_artifact(
        runtime_path,
        bundle_root=root,
    )
    runtime = validate_runtime_attestation(
        runtime_path,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
        expected_sha256=runtime_sha256,
    )
    base_url = runtime.get("baseUrl")
    if not isinstance(base_url, str):
        raise ValueError("classroom exports runtime URL is invalid")
    capacity_path = root / "runtime" / "capacity-profile-attestation.json"
    capacity_body, capacity_sha256 = read_capacity_profile_attestation_artifact(
        capacity_path,
        bundle_root=root,
    )
    tenant_id = derive_capacity_profile_tenant_id(
        capacity_body,
        bundle_root=root,
        candidate_root=candidate_path,
        candidate=candidate,
        release_run=bound_run,
    )

    attempt_id = uuid.uuid4().hex
    boundary = _ClassroomPublicationBoundary.open(root, attempt_id)
    if boundary.staging is None or boundary.raw_root is None:
        boundary.close()
        raise RuntimeError("classroom exports publication boundary is incomplete")
    staging = boundary.staging
    raw_root = boundary.raw_root
    raw_targets = {kind: raw_root / name for kind, name in CLASSROOM_EXPORT_PATHS.items()}
    logical_command = classroom_exports_command_record()
    arguments = [
        sys.executable,
        str(SCRIPTS_ROOT / "classroom_export_probe.py"),
        "--profile",
        "first-release",
    ]
    allowed_environment = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "WINDIR",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_environment
    }
    environment.update(
        {
            "YFEISTAI_LIVE_FIXTURE_TOKEN": token,
            "YFEISTAI_CANDIDATE_ROOT": str(candidate_path),
            "YFEISTAI_RELEASE_RUN_ID": bound_run["runId"],
            "YFEISTAI_ENVIRONMENT_ID": bound_run["environmentId"],
            "YFEISTAI_ACCEPTANCE_TENANT_ID": tenant_id,
            "YFEISTAI_CLASSROOM_EXPORT_TIMEOUT_SECONDS": str(
                timeout_seconds - _PROBE_CLEANUP_MARGIN_SECONDS
            ),
            "YFEISTAI_CLASSROOM_EXPORT_STAGING_DIR": str(staging),
            "WEB_BASE_URL": base_url,
        }
    )
    staged_proof = proof_path.with_name(f".{proof_path.name}.{attempt_id}.staging")
    staged_receipt = receipt_path.with_name(f".{receipt_path.name}.{attempt_id}.staging")
    leases: dict[str, _ClassroomArtifactLease] = {}
    proof_handle: BinaryIO | None = None
    receipt_handle: BinaryIO | None = None
    proof_identity: tuple[int, int] | None = None
    receipt_identity: tuple[int, int] | None = None
    published: dict[str, tuple[Path, tuple[int, int]]] = {}
    archive_artifacts: dict[str, Path] = {}
    formal_paths = (proof_path, receipt_path, *raw_targets.values())
    native_exit: int | None = None
    try:
        boundary.assert_unchanged()
        if any(os.path.lexists(path) for path in (proof_path, receipt_path, *raw_targets.values())):
            raise ValueError("classroom exports evidence already exists")
        try:
            completed = runner(
                arguments,
                cwd=candidate_path,
                env=environment,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise ValueError("classroom exports probe could not run") from exc
        if isinstance(completed.returncode, int) and not isinstance(completed.returncode, bool):
            native_exit = completed.returncode
        if (
            native_exit != 0
            or completed.args != arguments
            or not isinstance(completed.stdout, str)
            or completed.stderr != ""
        ):
            raise ValueError("classroom exports probe did not exit cleanly")
        try:
            stdout = completed.stdout.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("classroom exports probe output is invalid") from exc
        if len(stdout) > MAX_CLASSROOM_EXPORT_REPORT_BYTES or any(
            secret in stdout for secret in secrets
        ):
            raise ValueError("classroom exports probe output is invalid or contains a secret")

        leases = _open_classroom_artifact_leases(boundary)
        artifact_handles = {kind: lease.handle for kind, lease in leases.items()}
        if any(_classroom_artifact_contains_secret(lease, secrets) for lease in leases.values()):
            raise ValueError("classroom exports raw artifact contains a live fixture token")
        report = parse_classroom_export_report(
            stdout,
            artifact_root=staging,
            artifact_handles=artifact_handles,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
        )
        if report.get("tenantId") != tenant_id:
            raise ValueError("classroom exports tenant does not match capacity proof")
        observed_at = report.get("observedAt")
        if not isinstance(observed_at, str) or not _valid_observed_at(observed_at):
            raise ValueError("classroom exports observedAt is invalid")
        checks = derive_classroom_export_checks(
            stdout,
            artifact_root=staging,
            artifact_handles=artifact_handles,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
        )
        if any(value is not True for value in checks.values()):
            raise ValueError("classroom exports did not prove all four artifacts")

        def assert_release_binding() -> None:
            candidate_after = _candidate(candidate_path)
            runtime_after, runtime_after_sha256 = read_runtime_attestation_artifact(
                runtime_path,
                bundle_root=root,
            )
            capacity_after, capacity_after_sha256 = read_capacity_profile_attestation_artifact(
                capacity_path,
                bundle_root=root,
            )
            if (
                candidate_after != candidate
                or runtime_after != runtime_body
                or runtime_after_sha256 != runtime_sha256
                or capacity_after != capacity_body
                or capacity_after_sha256 != capacity_sha256
            ):
                raise ValueError("classroom exports release binding changed")
            validate_runtime_attestation(
                runtime_path,
                bundle_root=root,
                candidate_root=candidate_path,
                candidate=candidate,
                release_run=bound_run,
                expected_base_url=base_url,
                expected_sha256=runtime_sha256,
            )
            if (
                derive_capacity_profile_tenant_id(
                    capacity_after,
                    bundle_root=root,
                    candidate_root=candidate_path,
                    candidate=candidate,
                    release_run=bound_run,
                )
                != tenant_id
            ):
                raise ValueError("classroom exports capacity tenant changed")

        assert_release_binding()
        for lease in leases.values():
            _assert_classroom_artifact_lease(lease)
        replayed_report = parse_classroom_export_report(
            stdout,
            artifact_root=staging,
            artifact_handles=artifact_handles,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
        )
        replayed_checks = derive_classroom_export_checks(
            stdout,
            artifact_root=staging,
            artifact_handles=artifact_handles,
            candidate=candidate,
            release_run=bound_run,
            expected_base_url=base_url,
        )
        if replayed_report != report or replayed_checks != checks:
            raise ValueError("classroom exports replay changed before publication")

        raw_records = {
            kind: {
                "artifact": f"raw/classroom-exports/{lease.name}",
                "sha256": lease.sha256,
                "sizeBytes": lease.size,
            }
            for kind, lease in leases.items()
        }
        proof = {
            "schemaVersion": 1,
            "candidate": candidate,
            "releaseRun": bound_run,
            "observedAt": observed_at,
            "baseUrl": base_url,
            "tenantId": tenant_id,
            "runtimeAttestation": {
                "artifact": "runtime/runtime-attestation.json",
                "sha256": runtime_sha256,
            },
            "capacityAttestation": {
                "artifact": "runtime/capacity-profile-attestation.json",
                "sha256": capacity_sha256,
            },
            "execution": {
                "command": logical_command,
                "nativeExit": 0,
                "stdout": completed.stdout,
                "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
                "stderr": "",
            },
            "rawArtifacts": raw_records,
        }
        boundary.assert_unchanged()
        proof_handle, proof_identity, proof_body = _create_classroom_json_staging(
            boundary,
            parent_key="runtime",
            path=staged_proof,
            document=proof,
        )
        boundary.assert_unchanged()
        proof_details = os.fstat(proof_handle.fileno())
        if not stat.S_ISREG(proof_details.st_mode):
            raise ValueError("classroom exports staged proof changed")
        proof_sha256 = hashlib.sha256(proof_body).hexdigest()
        boundary.assert_unchanged()
        receipt = _pass_receipt_from_candidate(
            candidate=candidate,
            release_run=bound_run,
            evidence="classroom_exports",
            observed_at=observed_at,
            native_exit=0,
            checks=checks,
            provenance={
                "classroomExportsAttestation": {
                    "artifact": "runtime/classroom-exports-attestation.json",
                    "sha256": proof_sha256,
                }
            },
        )
        receipt_handle, receipt_identity, receipt_body = _create_classroom_json_staging(
            boundary,
            parent_key="artifacts",
            path=staged_receipt,
            document=receipt,
        )
        boundary.assert_unchanged()
        receipt_details = os.fstat(receipt_handle.fileno())
        if not stat.S_ISREG(receipt_details.st_mode):
            raise ValueError("classroom exports staged receipt changed")
        if any(secret in proof_body or secret in receipt_body for secret in secrets):
            raise ValueError("classroom exports evidence contains a live fixture token")
        archive_artifacts = {
            "proof": staged_proof,
            "receipt": staged_receipt,
        }

        assert_release_binding()
        boundary.assert_unchanged()
        if any(os.path.lexists(path) for path in formal_paths):
            raise ValueError("classroom exports publication target appeared concurrently")
        for kind in CLASSROOM_EXPORT_PATHS:
            lease = leases[kind]
            target = raw_targets[kind]
            boundary.assert_unchanged()
            published[f"published-raw-{kind}"] = (target, lease.identity)
            _publish_classroom_no_replace(
                boundary,
                lease.path,
                target,
                source_handle=lease.handle,
            )
            boundary.assert_unchanged()
            _assert_classroom_artifact_lease(lease)
            _assert_published_classroom_artifact(lease, target)
        boundary.assert_unchanged()
        assert receipt_identity is not None
        published["published-receipt"] = (receipt_path, receipt_identity)
        _publish_classroom_no_replace(
            boundary,
            staged_receipt,
            receipt_path,
            source_handle=receipt_handle,
        )
        boundary.assert_unchanged()
        _assert_published_classroom_receipt(
            receipt_path,
            expected_body=receipt_body,
            expected_identity=receipt_identity,
        )

        derived_checks, replayed_observed_at = derive_classroom_exports_receipt_checks(
            proof_body,
            bundle_root=root,
            candidate_root=candidate_path,
            candidate=candidate,
            release_run=bound_run,
        )
        if derived_checks != checks or replayed_observed_at != observed_at:
            raise ValueError("classroom exports proof replay changed")
        assert_release_binding()
        for kind, lease in leases.items():
            _assert_classroom_artifact_lease(lease)
            _assert_published_classroom_artifact(lease, raw_targets[kind])
        _assert_published_classroom_receipt(
            receipt_path,
            expected_body=receipt_body,
            expected_identity=receipt_identity,
        )
        boundary.assert_unchanged()
        assert proof_identity is not None
        published["published-proof"] = (proof_path, proof_identity)
        _publish_classroom_no_replace(
            boundary,
            staged_proof,
            proof_path,
            source_handle=proof_handle,
        )
        boundary.assert_unchanged()
        _assert_published_classroom_receipt(
            proof_path,
            expected_body=proof_body,
            expected_identity=proof_identity,
            label="proof",
        )
        assert_release_binding()
        for kind, lease in leases.items():
            _assert_classroom_artifact_lease(lease)
            _assert_published_classroom_artifact(lease, raw_targets[kind])
        _assert_published_classroom_receipt(
            receipt_path,
            expected_body=receipt_body,
            expected_identity=receipt_identity,
        )
        _assert_published_classroom_receipt(
            proof_path,
            expected_body=proof_body,
            expected_identity=proof_identity,
            label="proof",
        )
        boundary.assert_unchanged()
        return receipt
    except BaseException as original_error:
        for lease in leases.values():
            lease.handle.close()
        leases = {}
        if proof_handle is not None:
            proof_handle.close()
            proof_handle = None
        if receipt_handle is not None:
            receipt_handle.close()
            receipt_handle = None
        cleanup_error: Exception | None = None
        try:
            _retract_classroom_formal_entries(
                boundary,
                {path: identity for path, identity in published.values()},
            )
        except Exception as exc:
            cleanup_error = exc
        archive_error: Exception | None = None
        if isinstance(original_error, Exception):
            try:
                boundary.assert_unchanged()
                _record_probe_failure(
                    bundle_root=root,
                    evidence="classroom-exports",
                    recipe="live-first-release",
                    attempt_id=attempt_id,
                    reason="classroom exports execution or publication failed",
                    native_exit=native_exit,
                    artifacts=archive_artifacts,
                )
            except Exception as exc:
                archive_error = exc
        if archive_error is not None:
            if cleanup_error is not None:
                archive_error.add_note(f"formal evidence retraction also failed: {cleanup_error}")
            raise original_error from archive_error
        if cleanup_error is not None:
            raise original_error from cleanup_error
        raise
    finally:
        for lease in leases.values():
            lease.handle.close()
        if proof_handle is not None:
            proof_handle.close()
        if receipt_handle is not None:
            receipt_handle.close()
        staging_entries: dict[Path, tuple[int, int] | None] = {
            staging / name: leases.get(kind).identity if kind in leases else None
            for kind, name in CLASSROOM_EXPORT_PATHS.items()
        }
        staging_entries[staged_proof] = proof_identity
        staging_entries[staged_receipt] = receipt_identity
        active_error = sys.exception()
        try:
            _remove_classroom_entries(
                boundary,
                staging_entries,
                label="classroom exports staging evidence",
            )
        except Exception as exc:
            if active_error is None:
                raise
            active_error.add_note(f"staging evidence cleanup failed: {exc}")
        boundary.close()
        try:
            staging.rmdir()
        except OSError:
            pass


class _ManifestPublicationBoundary:
    def __init__(self, root: Path, lease: _ClassroomDirectoryLease) -> None:
        self.root = root
        self.leases = {"bundle": lease}

    @classmethod
    def open(cls, root: Path) -> _ManifestPublicationBoundary:
        path = Path(os.path.abspath(root))
        try:
            lease = _GatewayPublicationBoundary._open_root(path)
        except (OSError, ValueError) as exc:
            raise ValueError("release manifest bundle boundary cannot be opened") from exc
        boundary = cls(path, lease)
        try:
            boundary.assert_unchanged()
            return boundary
        except BaseException:
            boundary.close()
            raise

    def assert_unchanged(self) -> None:
        lease = self.leases["bundle"]
        reopened: _ClassroomDirectoryLease | None = None
        try:
            held_identity = (
                _windows_handle_identity(lease.handle, directory=True)
                if os.name == "nt"
                else _file_identity(os.fstat(int(lease.handle)))
            )
            reopened = _GatewayPublicationBoundary._open_root(self.root)
            if held_identity != lease.identity or reopened.identity != lease.identity:
                raise ValueError("release manifest bundle boundary changed")
        except (OSError, ValueError) as exc:
            raise ValueError("release manifest bundle boundary changed") from exc
        finally:
            if reopened is not None:
                if os.name == "nt":
                    _close_windows_handle(reopened.handle)
                else:
                    os.close(int(reopened.handle))

    def close(self) -> None:
        lease = self.leases.pop("bundle", None)
        if lease is None:
            return
        if os.name == "nt":
            _close_windows_handle(lease.handle)
        else:
            os.close(int(lease.handle))


def _publish_manifest_no_replace(
    boundary: _ManifestPublicationBoundary,
    target: Path,
    *,
    source_handle: BinaryIO,
) -> None:
    if target.parent != boundary.root or Path(target.name).name != target.name or not target.name:
        raise ValueError("release manifest publication target is invalid")
    if os.name == "nt":
        import msvcrt

        _link_windows_file_relative(
            msvcrt.get_osfhandle(source_handle.fileno()),
            boundary.leases["bundle"].handle,
            target.name,
        )
    else:
        _link_posix_file_descriptor(
            source_handle.fileno(),
            int(boundary.leases["bundle"].handle),
            target.name,
        )


def _assert_manifest_published(
    boundary: _ManifestPublicationBoundary,
    target: Path,
    *,
    expected_body: bytes,
    expected_identity: tuple[int, int],
) -> None:
    if target.parent != boundary.root or Path(target.name).name != target.name or not target.name:
        raise ValueError("published release manifest target is invalid")
    handle: object | int | None = None
    descriptor: int | None = None
    try:
        if os.name == "nt":
            import msvcrt

            handle, _native_identity = _open_windows_regular_file_relative(
                boundary.leases["bundle"].handle,
                target.name,
                share_access=0x00000001 | 0x00000002 | 0x00000004,
            )
            handle_value = getattr(handle, "value", handle)
            descriptor = msvcrt.open_osfhandle(
                int(handle_value),
                os.O_RDONLY | os.O_BINARY,
            )
            handle = None
            details = os.fstat(descriptor)
            identity = _file_identity(details)
            with os.fdopen(descriptor, "rb", closefd=True) as opened:
                descriptor = None
                body = opened.read(len(expected_body) + 1)
        else:
            descriptor = os.open(
                target.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=int(boundary.leases["bundle"].handle),
            )
            handle = descriptor
            details = os.fstat(descriptor)
            identity = _file_identity(details)
            with os.fdopen(descriptor, "rb", closefd=False) as opened:
                body = opened.read(len(expected_body) + 1)
        if identity != expected_identity or body != expected_body:
            raise ValueError("published release manifest changed")
    except (OSError, ValueError) as exc:
        raise ValueError("published release manifest changed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if handle is not None:
            if os.name == "nt":
                _close_windows_handle(handle)
            else:
                os.close(int(handle))


def _remove_manifest_staging(
    boundary: _ManifestPublicationBoundary,
    path: Path,
    *,
    expected_identity: tuple[int, int],
) -> None:
    if path.parent != boundary.root or Path(path.name).name != path.name or not path.name:
        raise ValueError("release manifest staging path is invalid")
    if os.name == "nt":
        import msvcrt

        handle: object | None = None
        descriptor: int | None = None
        try:
            handle, _identity = _open_windows_regular_file_relative(
                boundary.leases["bundle"].handle,
                path.name,
                share_access=0x00000001 | 0x00000002 | 0x00000004,
                deletable=True,
            )
            handle_value = getattr(handle, "value", handle)
            descriptor = msvcrt.open_osfhandle(
                int(handle_value),
                os.O_RDONLY | os.O_BINARY,
            )
            handle = None
            if _file_identity(os.fstat(descriptor)) != expected_identity:
                raise ValueError("release manifest staging file changed")
            _delete_windows_file_on_close(msvcrt.get_osfhandle(descriptor))
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if handle is not None:
                _close_windows_handle(handle)
        return
    details = os.stat(
        path.name,
        dir_fd=int(boundary.leases["bundle"].handle),
        follow_symlinks=False,
    )
    if not stat.S_ISREG(details.st_mode) or _file_identity(details) != expected_identity:
        raise ValueError("release manifest staging file changed")
    os.unlink(path.name, dir_fd=int(boundary.leases["bundle"].handle))


def _validated_receipt(
    path: Path,
    *,
    evidence: str,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    bundle_root: Path,
    candidate_root: Path,
    expected_outage_docker_host_identity_sha256: str | None = None,
    expected_gateway_docker_host_identity_sha256: str | None = None,
    trusted_keyring_path: Path | None = None,
    expected_trusted_keyring_sha256: str | None = None,
    expected_observer_challenge: str | None = None,
    expected_host_challenge: str | None = None,
    trusted_now: str | None = None,
    expected_openmaic_observer_attestation_sha256: str | None = None,
    expected_openmaic_observer_id: str | None = None,
    expected_openmaic_observer_origin: str | None = None,
    expected_openmaic_shared_ingress_control_origin: str | None = None,
) -> tuple[dict[str, object], bytes, str]:
    try:
        body = path.read_bytes()
        document = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt is unavailable or invalid") from exc
    if (
        not isinstance(document, dict)
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != ARTIFACT_SCHEMA_VERSION
        or document.get("candidate") != candidate
        or document.get("releaseRun") != release_run
        or document.get("evidence") != evidence
    ):
        raise ValueError("receipt envelope does not match the evidence bundle")
    receipt = document.get("receipt")
    contract = RECEIPT_CONTRACTS.get(evidence)
    if not isinstance(receipt, dict) or contract is None:
        raise ValueError("receipt is invalid")
    producer, required_checks = contract
    result = receipt.get("result")
    if (
        set(receipt) != {"producer", "observedAt", "result"}
        or receipt.get("producer") != producer
        or not _valid_observed_at(receipt.get("observedAt"))
        or not isinstance(result, dict)
        or set(result) != {"outcome", "nativeExit", "checks"}
        or result.get("outcome") != "pass"
    ):
        raise ValueError("receipt is invalid")
    native_exit = result.get("nativeExit")
    checks = result.get("checks")
    if not isinstance(checks, dict):
        raise ValueError("receipt is invalid")
    _validate_pass_result(evidence, native_exit=native_exit, checks=checks)
    gateway_trust_arguments: dict[str, object] = {}
    if evidence == "gateway_only_public":
        gateway_trust_arguments = {
            "trusted_keyring_path": trusted_keyring_path,
            "expected_trusted_keyring_sha256": expected_trusted_keyring_sha256,
            "expected_observer_challenge": expected_observer_challenge,
            "expected_host_challenge": expected_host_challenge,
            "trusted_now": trusted_now,
        }
    provenance_error = probe_provenance_error(
        document,
        evidence=evidence,
        candidate=candidate,
        release_run=release_run,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        expected_outage_docker_host_identity_sha256=(expected_outage_docker_host_identity_sha256),
        expected_gateway_docker_host_identity_sha256=(expected_gateway_docker_host_identity_sha256),
        expected_openmaic_observer_attestation_sha256=(
            expected_openmaic_observer_attestation_sha256
        ),
        expected_openmaic_observer_id=expected_openmaic_observer_id,
        expected_openmaic_observer_origin=expected_openmaic_observer_origin,
        expected_openmaic_shared_ingress_control_origin=(
            expected_openmaic_shared_ingress_control_origin
        ),
        **gateway_trust_arguments,
    )
    if provenance_error is not None:
        raise ValueError(provenance_error)
    return document, body, producer


def assemble_manifest(
    output_path: Path,
    *,
    candidate_root: Path,
    release_run: Mapping[str, object],
    receipt_paths: Mapping[str, Path],
    expected_outage_docker_host_identity_sha256: str | None = None,
    expected_gateway_docker_host_identity_sha256: str | None = None,
    trusted_keyring_path: Path | None = None,
    expected_trusted_keyring_sha256: str | None = None,
    expected_observer_challenge: str | None = None,
    expected_host_challenge: str | None = None,
    trusted_now: str | None = None,
    expected_openmaic_observer_attestation_sha256: str | None = None,
    expected_openmaic_observer_id: str | None = None,
    expected_openmaic_observer_origin: str | None = None,
    expected_openmaic_shared_ingress_control_origin: str | None = None,
) -> dict[str, object]:
    """Validate receipt bytes and publish their schema-v3 manifest last."""
    target = Path(os.path.abspath(output_path))
    if Path(target.name).name != target.name or not target.name:
        raise ValueError("release manifest output path is invalid")
    bundle_root = target.parent
    candidate_path = Path(os.path.abspath(candidate_root))
    boundary = _ManifestPublicationBoundary.open(bundle_root)
    staged: Path | None = None
    staged_handle: BinaryIO | None = None
    staged_identity: tuple[int, int] | None = None
    committed = False
    primary_error: BaseException | None = None
    try:
        boundary.assert_unchanged()
        if any(Path(os.path.abspath(raw_path)) == target for raw_path in receipt_paths.values()):
            raise ValueError("receipt path must not be the manifest output path")
        if os.path.lexists(target):
            raise FileExistsError("release manifest already exists")
        candidate = _candidate(candidate_path)
        boundary.assert_unchanged()
        bound_run = _release_run(release_run)
        expected_gateway_identity = expected_gateway_docker_host_identity_sha256
        if "gateway_only_public" in receipt_paths:
            expected_gateway_identity = _gateway_expected_docker_host_identity_sha256(
                expected_gateway_identity
            )
        evidence_entries: dict[str, object] = {}
        for evidence, raw_path in receipt_paths.items():
            boundary.assert_unchanged()
            receipt_path = Path(os.path.abspath(raw_path))
            if receipt_path == target:
                raise ValueError("receipt path must not be the manifest output path")
            try:
                relative_path = receipt_path.relative_to(bundle_root)
            except ValueError as exc:
                raise ValueError("receipt is outside the evidence bundle") from exc
            _document, body, producer = _validated_receipt(
                receipt_path,
                evidence=evidence,
                candidate=candidate,
                release_run=bound_run,
                bundle_root=bundle_root,
                candidate_root=candidate_path,
                expected_outage_docker_host_identity_sha256=(
                    expected_outage_docker_host_identity_sha256
                ),
                expected_gateway_docker_host_identity_sha256=expected_gateway_identity,
                trusted_keyring_path=trusted_keyring_path,
                expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
                expected_observer_challenge=expected_observer_challenge,
                expected_host_challenge=expected_host_challenge,
                trusted_now=trusted_now,
                expected_openmaic_observer_attestation_sha256=(
                    expected_openmaic_observer_attestation_sha256
                ),
                expected_openmaic_observer_id=expected_openmaic_observer_id,
                expected_openmaic_observer_origin=expected_openmaic_observer_origin,
                expected_openmaic_shared_ingress_control_origin=(
                    expected_openmaic_shared_ingress_control_origin
                ),
            )
            boundary.assert_unchanged()
            evidence_entries[evidence] = {
                "status": "pass",
                "detail": f"{evidence} verified by {producer}",
                "artifact": relative_path.as_posix(),
                "artifactSha256": hashlib.sha256(body).hexdigest(),
            }
        manifest: dict[str, object] = {
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "candidate": candidate,
            "releaseRun": bound_run,
            "evidence": evidence_entries,
        }
        boundary.assert_unchanged()
        staged = bundle_root / f".{target.name}.{uuid.uuid4().hex}.tmp"
        staged_handle, staged_identity, staged_body = _create_classroom_json_staging(
            boundary,
            parent_key="bundle",
            path=staged,
            document=manifest,
        )
        if os.name == "nt":
            import msvcrt

            staged_handle.close()
            native_handle: object | None = None
            descriptor: int | None = None
            try:
                native_handle, _reopened_identity = _open_windows_regular_file_relative(
                    boundary.leases["bundle"].handle,
                    staged.name,
                    share_access=0x00000001 | 0x00000002 | 0x00000004,
                )
                handle_value = getattr(native_handle, "value", native_handle)
                descriptor = msvcrt.open_osfhandle(
                    int(handle_value),
                    os.O_RDONLY | os.O_BINARY,
                )
                native_handle = None
                staged_handle = os.fdopen(descriptor, "rb")
                descriptor = None
                if _file_identity(os.fstat(staged_handle.fileno())) != staged_identity:
                    raise ValueError("release manifest staging file changed")
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if native_handle is not None:
                    _close_windows_handle(native_handle)
        boundary.assert_unchanged()
        publication_context = _MANIFEST_PUBLICATION_CONTEXT.set(
            _ManifestPublicationContext(
                boundary=boundary,
                source=staged,
                source_handle=staged_handle,
            )
        )
        try:
            if os.path.lexists(target):
                raise FileExistsError("release manifest already exists")
            _publish_no_replace(staged, target)
            committed = True
            _assert_manifest_published(
                boundary,
                target,
                expected_body=staged_body,
                expected_identity=staged_identity,
            )
            boundary.assert_unchanged()
            _fsync_directory(bundle_root)
        finally:
            _MANIFEST_PUBLICATION_CONTEXT.reset(publication_context)
        return manifest
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if staged_handle is not None:
            try:
                staged_handle.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if staged is not None and staged_identity is not None:
            try:
                _remove_manifest_staging(
                    boundary,
                    staged,
                    expected_identity=staged_identity,
                )
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            boundary.close()
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if primary_error is not None:
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"release manifest staging cleanup failed: {cleanup_error}"
                    )
            elif not committed:
                raise cleanup_errors[0]


def _add_common_receipt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--observed-at", required=True)


def _add_gateway_trust_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    parser.add_argument("--gateway-trust-keyring", type=Path, required=required)
    parser.add_argument("--gateway-trust-keyring-sha256", required=required)
    parser.add_argument("--gateway-observer-challenge", required=required)
    parser.add_argument("--gateway-host-challenge", required=required)
    parser.add_argument("--gateway-trusted-now", required=required)


def _add_openmaic_observer_trust_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    parser.add_argument("--openmaic-observer-attestation-sha256", required=required)
    parser.add_argument("--openmaic-observer-id", required=required)
    parser.add_argument("--openmaic-observer-origin", required=required)
    parser.add_argument("--openmaic-shared-ingress-control-origin", required=required)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    source_head = commands.add_parser("source-head")
    _add_common_receipt_arguments(source_head)
    source_head.add_argument("--source-root", type=Path, required=True)

    image_digests = commands.add_parser("image-digests")
    _add_common_receipt_arguments(image_digests)

    running_containers = commands.add_parser("running-containers")
    running_containers.add_argument("--output", type=Path, required=True)
    running_containers.add_argument("--candidate-root", type=Path, required=True)
    running_containers.add_argument("--bundle-root", type=Path, required=True)
    running_containers.add_argument("--run-id", required=True)
    running_containers.add_argument("--environment-id", required=True)

    platform_preflight = commands.add_parser("platform-preflight")
    platform_preflight.add_argument("--candidate-root", type=Path, required=True)
    platform_preflight.add_argument("--bundle-root", type=Path, required=True)
    platform_preflight.add_argument("--run-id", required=True)
    platform_preflight.add_argument("--environment-id", required=True)
    platform_preflight.add_argument("--timeout-seconds", type=int, required=True)

    capacity_profile = commands.add_parser("capacity-profile")
    capacity_profile.add_argument("--candidate-root", type=Path, required=True)
    capacity_profile.add_argument("--bundle-root", type=Path, required=True)
    capacity_profile.add_argument("--run-id", required=True)
    capacity_profile.add_argument("--environment-id", required=True)
    capacity_profile.add_argument("--timeout-seconds", type=int, required=True)

    tenant_isolation = commands.add_parser("tenant-isolation")
    tenant_isolation.add_argument("--candidate-root", type=Path, required=True)
    tenant_isolation.add_argument("--bundle-root", type=Path, required=True)
    tenant_isolation.add_argument("--run-id", required=True)
    tenant_isolation.add_argument("--environment-id", required=True)
    tenant_isolation.add_argument("--timeout-seconds", type=int, required=True)

    openmaic_shared_plane = commands.add_parser("openmaic-shared-plane")
    openmaic_shared_plane.add_argument("--candidate-root", type=Path, required=True)
    openmaic_shared_plane.add_argument("--bundle-root", type=Path, required=True)
    openmaic_shared_plane.add_argument("--run-id", required=True)
    openmaic_shared_plane.add_argument("--environment-id", required=True)
    openmaic_shared_plane.add_argument("--timeout-seconds", type=int, required=True)

    openmaic_dedicated_plane = commands.add_parser("openmaic-dedicated-plane")
    openmaic_dedicated_plane.add_argument("--candidate-root", type=Path, required=True)
    openmaic_dedicated_plane.add_argument("--bundle-root", type=Path, required=True)
    openmaic_dedicated_plane.add_argument("--run-id", required=True)
    openmaic_dedicated_plane.add_argument("--environment-id", required=True)
    openmaic_dedicated_plane.add_argument("--timeout-seconds", type=int, required=True)
    openmaic_dedicated_plane.add_argument(
        "--outage-docker-host-identity-sha256",
        required=True,
    )

    gateway_only_public = commands.add_parser("gateway-only-public")
    gateway_only_public.add_argument("--candidate-root", type=Path, required=True)
    gateway_only_public.add_argument("--bundle-root", type=Path, required=True)
    gateway_only_public.add_argument("--run-id", required=True)
    gateway_only_public.add_argument("--environment-id", required=True)
    gateway_only_public.add_argument("--timeout-seconds", type=int, required=True)
    gateway_only_public.add_argument(
        "--docker-host-identity-sha256",
        required=True,
    )
    _add_gateway_trust_arguments(gateway_only_public, required=True)

    classroom_exports = commands.add_parser("classroom-exports")
    classroom_exports.add_argument("--candidate-root", type=Path, required=True)
    classroom_exports.add_argument("--bundle-root", type=Path, required=True)
    classroom_exports.add_argument("--run-id", required=True)
    classroom_exports.add_argument("--environment-id", required=True)
    classroom_exports.add_argument("--timeout-seconds", type=int, required=True)

    backup_restore = commands.add_parser("backup-restore")
    backup_restore.add_argument("--candidate-root", type=Path, required=True)
    backup_restore.add_argument("--bundle-root", type=Path, required=True)
    backup_restore.add_argument("--run-id", required=True)
    backup_restore.add_argument("--environment-id", required=True)
    backup_restore.add_argument(
        "--database-ownership",
        choices=("runner-owned-disposable",),
        required=True,
    )
    backup_restore.add_argument(
        "--object-namespace-ownership",
        choices=("runner-owned-disposable",),
        required=True,
    )

    produce = commands.add_parser("produce")
    _add_common_receipt_arguments(produce)
    produce.add_argument("--evidence", choices=tuple(sorted(PROBE_RECIPES)), required=True)
    produce.add_argument("--bundle-root", type=Path, required=True)
    produce.add_argument("--working-directory", type=Path, required=True)
    produce.add_argument("--timeout-seconds", type=int, required=True)
    produce.add_argument("--base-url", required=True)

    assemble = commands.add_parser("assemble")
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("--candidate-root", type=Path, required=True)
    assemble.add_argument("--run-id", required=True)
    assemble.add_argument("--environment-id", required=True)
    assemble.add_argument("--receipt", action="append", required=True)
    assemble.add_argument("--outage-docker-host-identity-sha256")
    assemble.add_argument("--gateway-docker-host-identity-sha256")
    _add_gateway_trust_arguments(assemble, required=False)
    _add_openmaic_observer_trust_arguments(assemble, required=False)
    args = parser.parse_args(argv)
    if args.command == "assemble":
        try:
            receipt_paths = _receipt_paths(args.receipt)
        except ValueError as exc:
            parser.error(str(exc))
        if "gateway_only_public" in receipt_paths:
            required_trust_arguments = {
                "--gateway-trust-keyring": args.gateway_trust_keyring,
                "--gateway-trust-keyring-sha256": args.gateway_trust_keyring_sha256,
                "--gateway-observer-challenge": args.gateway_observer_challenge,
                "--gateway-host-challenge": args.gateway_host_challenge,
                "--gateway-trusted-now": args.gateway_trusted_now,
            }
            missing = [
                option for option, value in required_trust_arguments.items() if value is None
            ]
            if missing:
                parser.error(
                    "assemble with gateway_only_public receipt requires " + ", ".join(missing)
                )
        if "openmaic_dedicated_plane" in receipt_paths:
            required_observer_arguments = {
                "--openmaic-observer-attestation-sha256": (
                    args.openmaic_observer_attestation_sha256
                ),
                "--openmaic-observer-id": args.openmaic_observer_id,
                "--openmaic-observer-origin": args.openmaic_observer_origin,
                "--openmaic-shared-ingress-control-origin": (
                    args.openmaic_shared_ingress_control_origin
                ),
            }
            missing = [
                option for option, value in required_observer_arguments.items() if value is None
            ]
            if missing:
                parser.error(
                    "assemble with openmaic_dedicated_plane receipt requires " + ", ".join(missing)
                )
    return args


def _receipt_paths(values: Sequence[str]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for value in values:
        evidence, separator, raw_path = value.partition("=")
        if not separator or evidence not in RECEIPT_CONTRACTS or evidence in paths or not raw_path:
            raise ValueError("--receipt must contain unique evidence=path values")
        paths[evidence] = Path(raw_path)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    release_run = {
        "runId": args.run_id,
        "environmentId": args.environment_id,
    }
    if args.command == "source-head":
        write_source_head_receipt(
            args.output,
            candidate_root=args.candidate_root,
            release_run=release_run,
            source_root=args.source_root,
            observed_at=args.observed_at,
        )
    elif args.command == "image-digests":
        write_image_digest_receipt(
            args.output,
            candidate_root=args.candidate_root,
            release_run=release_run,
            observed_at=args.observed_at,
        )
    elif args.command == "running-containers":
        write_running_containers_receipt(
            args.output,
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
        )
    elif args.command == "platform-preflight":
        write_platform_preflight_receipts(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            timeout_seconds=args.timeout_seconds,
        )
        print(args.bundle_root / "runtime" / "platform-preflight-attestation.json")
        return 0
    elif args.command == "capacity-profile":
        write_capacity_profile_receipt(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            timeout_seconds=args.timeout_seconds,
        )
        print(args.bundle_root / "runtime" / "capacity-profile-attestation.json")
        return 0
    elif args.command == "tenant-isolation":
        write_tenant_isolation_receipt(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            timeout_seconds=args.timeout_seconds,
        )
        print(args.bundle_root / "runtime" / "tenant-isolation-attestation.json")
        return 0
    elif args.command == "openmaic-shared-plane":
        write_openmaic_shared_plane_receipt(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            timeout_seconds=args.timeout_seconds,
        )
        print(args.bundle_root / "runtime" / "openmaic-shared-plane-attestation.json")
        return 0
    elif args.command == "openmaic-dedicated-plane":
        write_openmaic_dedicated_plane_receipt(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            timeout_seconds=args.timeout_seconds,
            expected_outage_docker_host_identity_sha256=(args.outage_docker_host_identity_sha256),
        )
        print(args.bundle_root / "runtime" / "openmaic-dedicated-plane-attestation.json")
        return 0
    elif args.command == "gateway-only-public":
        write_gateway_only_public_receipt(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            timeout_seconds=args.timeout_seconds,
            expected_docker_host_identity_sha256=(args.docker_host_identity_sha256),
            trusted_keyring_path=args.gateway_trust_keyring,
            expected_trusted_keyring_sha256=(args.gateway_trust_keyring_sha256),
            expected_observer_challenge=args.gateway_observer_challenge,
            expected_host_challenge=args.gateway_host_challenge,
            trusted_now=args.gateway_trusted_now,
        )
        print(args.bundle_root / "runtime" / "gateway-only-public-attestation.json")
        return 0
    elif args.command == "classroom-exports":
        write_classroom_exports_receipt(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            timeout_seconds=args.timeout_seconds,
        )
        print(args.bundle_root / "runtime" / "classroom-exports-attestation.json")
        return 0
    elif args.command == "backup-restore":
        write_backup_restore_receipt(
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            database_ownership=args.database_ownership,
            object_namespace_ownership=args.object_namespace_ownership,
        )
        print(args.bundle_root / "artifacts" / "backup_restore.json")
        return 0
    elif args.command == "produce":
        recipe, _expected_count = PROBE_RECIPES[args.evidence]
        run_probe_receipt(
            args.output,
            candidate_root=args.candidate_root,
            bundle_root=args.bundle_root,
            release_run=release_run,
            evidence=args.evidence,
            observed_at=args.observed_at,
            base_url=args.base_url,
            raw_report_path=args.bundle_root / "raw" / f"{args.evidence}.json",
            execution_record_path=args.bundle_root / "executions" / f"{args.evidence}.json",
            recipe=recipe,
            working_directory=args.working_directory,
            timeout_seconds=args.timeout_seconds,
        )
    else:
        receipt_paths = _receipt_paths(args.receipt)
        assemble_arguments: dict[str, object] = {
            "candidate_root": args.candidate_root,
            "release_run": release_run,
            "receipt_paths": receipt_paths,
        }
        if args.outage_docker_host_identity_sha256 is not None:
            assemble_arguments["expected_outage_docker_host_identity_sha256"] = (
                args.outage_docker_host_identity_sha256
            )
        if args.gateway_docker_host_identity_sha256 is not None:
            assemble_arguments["expected_gateway_docker_host_identity_sha256"] = (
                args.gateway_docker_host_identity_sha256
            )
        if "gateway_only_public" in receipt_paths:
            assemble_arguments.update(
                {
                    "trusted_keyring_path": args.gateway_trust_keyring,
                    "expected_trusted_keyring_sha256": args.gateway_trust_keyring_sha256,
                    "expected_observer_challenge": args.gateway_observer_challenge,
                    "expected_host_challenge": args.gateway_host_challenge,
                    "trusted_now": args.gateway_trusted_now,
                }
            )
        if "openmaic_dedicated_plane" in receipt_paths:
            assemble_arguments.update(
                {
                    "expected_openmaic_observer_attestation_sha256": (
                        args.openmaic_observer_attestation_sha256
                    ),
                    "expected_openmaic_observer_id": args.openmaic_observer_id,
                    "expected_openmaic_observer_origin": args.openmaic_observer_origin,
                    "expected_openmaic_shared_ingress_control_origin": (
                        args.openmaic_shared_ingress_control_origin
                    ),
                }
            )
        assemble_manifest(
            args.output,
            **assemble_arguments,
        )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
