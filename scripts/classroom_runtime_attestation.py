#!/usr/bin/env python
"""Produce a fixed, read-only Docker runtime attestation for one release candidate."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
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
import time
import uuid

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from docker_compose import platform_compose_topology_arguments  # noqa: E402
from render_platform_compose import load_image_lock  # noqa: E402

PROJECT = "yfeistai-platform"
SCHEMA_VERSION = 1
COMMAND_TIMEOUT_SECONDS = 300
SINGLE_COMMAND_TIMEOUT_SECONDS = 30
DOCKER_CONTEXT = "default"
DOCKER_CONFIG_LOGICAL = "<isolated-docker-config>"
WINDOWS_DOCKER_PATH = Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe")
POSIX_DOCKER_PATHS = (Path("/usr/local/bin/docker"), Path("/usr/bin/docker"))
DOCKER_LOGICAL_PREFIX = (
    "docker",
    "--config",
    DOCKER_CONFIG_LOGICAL,
    "--context",
    DOCKER_CONTEXT,
)
DOCKER_CONTEXT_ARGUMENTS = (
    "context",
    "inspect",
    DOCKER_CONTEXT,
    "--format",
    "{{json .Endpoints.docker.Host}}",
)
DOCKER_INFO_ARGUMENTS = (
    "info",
    "--format",
    '{"serverId":{{json .ID}},"osType":{{json .OSType}}}',
)
PS_FORMAT = "{{json .ID}}"
CONTAINER_INSPECT_FORMAT = (
    '{"containerId":{{json .Id}},"localImageId":{{json .Image}},'
    '"configImage":{{json .Config.Image}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"configHash":{{json (index .Config.Labels "com.docker.compose.config-hash")}},'
    '"privileged":{{json .HostConfig.Privileged}},"mounts":{{json .Mounts}},'
    '"capAdd":{{json .HostConfig.CapAdd}},"capDrop":{{json .HostConfig.CapDrop}},'
    '"command":{{json .Config.Cmd}},"entrypoint":{{json .Config.Entrypoint}},'
    '"user":{{json .Config.User}},"environment":{{json .Config.Env}},'
    '"state":{{json .State.Status}},"running":{{json .State.Running}},'
    '"restarting":{{json .State.Restarting}},"exitCode":{{json .State.ExitCode}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}}'
)
IMAGE_INSPECT_FORMAT = (
    '{"imageId":{{json .Id}},"repoDigests":{{json .RepoDigests}},'
    '"command":{{json .Config.Cmd}},"entrypoint":{{json .Config.Entrypoint}},'
    '"user":{{json .Config.User}},"environment":{{json .Config.Env}},'
    '"volumes":{{json .Config.Volumes}}}'
)
_DEPLOYMENT_ROOT_LOGICAL = "<deployment-root>"
_CANDIDATE_ROOT_LOGICAL = "<candidate-root>"
_COMPOSE_CONFIG_TAIL = ("config", "--format", "json")
_COMPOSE_HASH_TAIL = ("config", "--hash", "*")
PS_ARGUMENTS = (
    "ps",
    "-a",
    "--no-trunc",
    "--filter",
    f"label=com.docker.compose.project={PROJECT}",
    "--format",
    PS_FORMAT,
)
_OS_ENVIRONMENT = frozenset(("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"))
_DOCKER_HOST_IDENTITY_ENV = "YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_REQUIRED_HEALTHY_SERVICES = frozenset(
    ("deeptutor", "postgres", "minio", "openmaic", "openmaic-render")
)


class _ComposeLoader(yaml.SafeLoader):
    """Safe loader for Docker Compose value tags."""


def _construct_compose_value(
    loader: yaml.SafeLoader,
    node: ScalarNode | SequenceNode | MappingNode,
) -> object:
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
        return None if value in {"null", "~"} else value
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for _compose_tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(_compose_tag, _construct_compose_value)


def _valid_observed_at(value: object) -> bool:
    if not isinstance(value, str) or _OBSERVED_AT.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _release_run(raw: Mapping[str, object]) -> dict[str, str]:
    if set(raw) != {"runId", "environmentId"}:
        raise ValueError("release run identity is invalid")
    result: dict[str, str] = {}
    for name in ("runId", "environmentId"):
        value = raw.get(name)
        if not isinstance(value, str) or _RELEASE_ID.fullmatch(value) is None:
            raise ValueError("release run identity is invalid")
        result[name] = value
    return result


def _base_url(raw: object) -> str:
    from urllib.parse import urlsplit

    if not isinstance(raw, str):
        raise ValueError("runtime base URL is invalid")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("runtime base URL is invalid")
    return raw.rstrip("/")


def _trusted_regular_file(path: Path) -> bool:
    candidate = Path(path)
    if not candidate.is_absolute():
        return False
    try:
        candidate.resolve(strict=True)
    except OSError:
        return False
    cursor = candidate
    while cursor != Path(cursor.anchor):
        try:
            if _is_link_or_reparse(cursor):
                return False
        except ValueError:
            return False
        if cursor == candidate:
            if not cursor.is_file():
                return False
        elif not cursor.is_dir():
            return False
        cursor = cursor.parent
    return True


def _is_link_or_reparse(path: Path) -> bool:
    candidate = Path(path)
    try:
        details = candidate.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("runtime output boundary cannot be inspected") from exc
    if stat.S_ISLNK(details.st_mode):
        return True
    is_junction = getattr(candidate, "is_junction", None)
    try:
        if callable(is_junction) and is_junction():
            return True
    except OSError as exc:
        raise ValueError("runtime output boundary cannot be inspected") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _assert_no_link_ancestors(path: Path) -> None:
    cursor = Path(path)
    anchor = Path(cursor.anchor)
    while cursor != anchor:
        if _is_link_or_reparse(cursor):
            raise ValueError("runtime output boundary uses a symlink, junction, or reparse point")
        cursor = cursor.parent


def _assert_runtime_boundary(bundle_root: Path, runtime_root: Path) -> None:
    bundle = Path(bundle_root)
    runtime = Path(runtime_root)
    _assert_no_link_ancestors(bundle)
    _assert_no_link_ancestors(runtime)
    if not bundle.is_dir() or not runtime.is_dir():
        raise ValueError("runtime output boundary is not a directory")
    try:
        resolved_bundle = bundle.resolve(strict=True)
        resolved_runtime = runtime.resolve(strict=True)
    except OSError as exc:
        raise ValueError("runtime output boundary cannot be resolved") from exc
    if resolved_runtime.parent != resolved_bundle or runtime.parent != bundle:
        raise ValueError("runtime output boundary escaped the evidence bundle")


def _prepare_runtime_root(bundle_root: Path) -> tuple[Path, Path]:
    bundle = Path(os.path.abspath(bundle_root))
    _assert_no_link_ancestors(bundle)
    try:
        bundle.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError("runtime output boundary cannot be created") from exc
    _assert_no_link_ancestors(bundle)
    runtime = bundle / "runtime"
    if _is_link_or_reparse(runtime):
        raise ValueError("runtime output boundary uses a symlink, junction, or reparse point")
    try:
        runtime.mkdir(exist_ok=True)
    except OSError as exc:
        raise ValueError("runtime output boundary cannot be created") from exc
    _assert_runtime_boundary(bundle, runtime)
    return bundle, runtime


def resolve_fixed_docker(
    *,
    platform: str = sys.platform,
    trusted_paths: Sequence[Path] | None = None,
) -> Path:
    candidates = (
        tuple(trusted_paths)
        if trusted_paths is not None
        else ((WINDOWS_DOCKER_PATH,) if platform == "win32" else POSIX_DOCKER_PATHS)
    )
    for candidate in candidates:
        path = Path(candidate)
        if _trusted_regular_file(path):
            return path.resolve(strict=True)
    raise ValueError("trusted Docker CLI is unavailable")


def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    by_upper = {name.upper(): value for name, value in environ.items()}
    return {name: by_upper[name] for name in _OS_ENVIRONMENT if name in by_upper and by_upper[name]}


def _docker_host_identity_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None or value == "0" * 64:
        raise ValueError("Docker host identity is unavailable or invalid")
    return value


def _expected_docker_host_identity_sha256(
    value: object,
    environ: Mapping[str, str],
) -> str | None:
    by_upper = {name.upper(): item for name, item in environ.items()}
    environment_value = by_upper.get(_DOCKER_HOST_IDENTITY_ENV)
    supplied = [item for item in (value, environment_value) if item is not None]
    if not supplied:
        return None
    validated = [_docker_host_identity_sha256(item) for item in supplied]
    if len(set(validated)) != 1:
        raise ValueError("Docker host identity inputs do not match")
    return validated[0]


def _run_docker(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    check: bool,
    capture_output: bool,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
        capture_output=capture_output,
    )


class _ImmutableTextFile:
    """Minimal read-only path adapter for the shared strict lock parser."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read_text(self, *, encoding: str) -> str:
        return self._body.decode(encoding)


def _load_candidate_token(
    token: tuple[bytes, bytes],
    *,
    expected_candidate: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    try:
        lock = load_image_lock(
            _ImmutableTextFile(token[0]),  # type: ignore[arg-type]
            require_candidate=True,
            expected_candidate=expected_candidate,
        )
        compose = yaml.load(token[1].decode("utf-8"), Loader=_ComposeLoader)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError("candidate runtime contract is unavailable or invalid") from exc
    candidate = lock.get("candidate") if isinstance(lock, dict) else None
    images = lock.get("images") if isinstance(lock, dict) else None
    services = compose.get("services") if isinstance(compose, dict) else None
    if (
        not isinstance(lock, dict)
        or lock.get("schemaVersion") != 2
        or not isinstance(candidate, dict)
        or not isinstance(images, dict)
        or not isinstance(services, dict)
    ):
        raise ValueError("candidate runtime contract is unavailable or invalid")
    locked_references = {
        record["reference"] for record in images.values() if isinstance(record, dict)
    }
    expected: dict[str, dict[str, object]] = {}
    for service_name, raw_service in services.items():
        if not isinstance(service_name, str) or not isinstance(raw_service, dict):
            raise ValueError("candidate runtime Compose services are invalid")
        if "profiles" in raw_service:
            continue
        image = raw_service.get("image")
        restart = raw_service.get("restart", "")
        if (
            not isinstance(image, str)
            or image not in locked_references
            or not isinstance(restart, str)
        ):
            raise ValueError("candidate runtime Compose service is invalid")
        expected[service_name] = {"image": image, "restart": restart}
    if not expected:
        raise ValueError("candidate runtime Compose services are invalid")
    if not _REQUIRED_HEALTHY_SERVICES.issubset(expected):
        raise ValueError("candidate runtime Compose health services are invalid")
    return json.loads(json.dumps(candidate)), expected


def _load_candidate(candidate_root: Path) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    root = Path(os.path.abspath(candidate_root))
    try:
        with _CandidateContractLease.open(root) as lease:
            result = _load_candidate_token(lease.token)
            lease.assert_unchanged()
            return result
    except (OSError, ValueError) as exc:
        raise ValueError("candidate runtime contract is unavailable or invalid") from exc


def _json_object(body: bytes, *, label: str) -> dict[str, object]:
    try:
        raw = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Docker {label} output is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Docker {label} output is invalid")
    return raw


def _ps_ids(body: bytes) -> list[str]:
    ids: list[str] = []
    try:
        for line in body.splitlines():
            item = json.loads(line)
            if not isinstance(item, str) or not item:
                raise ValueError
            ids.append(item)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Docker ps output is invalid") from exc
    if len(ids) != len(set(ids)):
        raise ValueError("Docker ps output contains duplicate containers")
    return sorted(ids)


def _compose_hashes(body: bytes) -> dict[str, str]:
    hashes: dict[str, str] = {}
    try:
        for line in body.decode("utf-8").splitlines():
            parts = line.split()
            if len(parts) != 2:
                raise ValueError
            service, config_hash = parts
            if not service or service in hashes or _SHA256.fullmatch(config_hash) is None:
                raise ValueError
            hashes[service] = config_hash
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Docker Compose config hash output is invalid") from exc
    if not hashes:
        raise ValueError("Docker Compose config hash output is invalid")
    return hashes


def _runtime_compose_arguments(
    *,
    deployment_root: Path,
    candidate_root: Path,
    tail: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    actual = (
        "compose",
        *platform_compose_topology_arguments(
            deployment_root=deployment_root,
            candidate_root=candidate_root,
            env_file=deployment_root / "data" / "user" / "settings" / "docker.env",
        ),
        *tail,
    )
    logical = (
        "compose",
        "--env-file",
        f"{_DEPLOYMENT_ROOT_LOGICAL}/data/user/settings/docker.env",
        "--project-directory",
        _DEPLOYMENT_ROOT_LOGICAL,
        "--project-name",
        PROJECT,
        "-f",
        f"{_CANDIDATE_ROOT_LOGICAL}/docker-compose.yml",
        "-f",
        f"{_CANDIDATE_ROOT_LOGICAL}/docker-compose.platform.yml",
        *tail,
    )
    return actual, logical


def _canonical_json_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _value_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _docker_environment_hashes(raw: object, *, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"Docker {label} environment is invalid")
    values: dict[str, str] = {}
    for item in raw:
        name, separator, value = item.partition("=")
        if not separator or not name or name in values:
            raise ValueError(f"Docker {label} environment is invalid")
        values[name] = _value_sha256(value)
    return dict(sorted(values.items()))


def _compose_environment_hashes(raw: object) -> dict[str, str | None]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Docker Compose service environment is invalid")
    values: dict[str, str | None] = {}
    for name, value in raw.items():
        if (
            not isinstance(name, str)
            or not name
            or (value is not None and not isinstance(value, str))
        ):
            raise ValueError("Docker Compose service environment is invalid")
        values[name] = None if value is None else _value_sha256(value)
    return dict(sorted(values.items()))


def _environment_hash_digest(raw: object, *, label: str) -> str:
    if not isinstance(raw, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(value, str)
        and _SHA256.fullmatch(value) is not None
        for name, value in raw.items()
    ):
        raise ValueError(f"Docker {label} environment hashes are invalid")
    return hashlib.sha256(_canonical_json_bytes(dict(sorted(raw.items())))).hexdigest()


def _redacted_inspect_output(body: bytes, *, label: str) -> bytes:
    document = _json_object(body, label=label)
    if "environment" not in document or "environmentHashes" in document:
        raise ValueError(f"Docker {label} output is invalid")
    environment = _docker_environment_hashes(document.pop("environment"), label=label)
    document["environmentHashes"] = environment
    return _canonical_json_bytes(document)


def _compose_security_projection(body: bytes) -> bytes:
    document = _json_object(body, label="Compose config")
    raw_services = document.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise ValueError("Docker Compose config services are invalid")
    services: dict[str, dict[str, object]] = {}
    for name, raw_service in raw_services.items():
        if not isinstance(name, str) or not name or not isinstance(raw_service, dict):
            raise ValueError("Docker Compose config services are invalid")
        services[name] = {
            "image": raw_service.get("image"),
            "restart": raw_service.get("restart", "no"),
            "profiles": raw_service.get("profiles") or [],
            "privileged": raw_service.get("privileged", False),
            "capAdd": raw_service.get("cap_add") or [],
            "capDrop": raw_service.get("cap_drop") or [],
            "command": raw_service.get("command"),
            "entrypoint": raw_service.get("entrypoint"),
            "user": raw_service.get("user"),
            "environmentHashes": _compose_environment_hashes(raw_service.get("environment")),
            "volumes": raw_service.get("volumes") or [],
            "secrets": raw_service.get("secrets") or [],
            "configs": raw_service.get("configs") or [],
        }
    projection: dict[str, object] = {"services": services}
    for name in ("volumes", "secrets", "configs"):
        raw = document.get(name) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Docker Compose config {name} are invalid")
        projection[name] = raw
    return _canonical_json_bytes(projection)


def _merged_expected_services(
    compose_security: Mapping[str, object],
    candidate_services: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    raw_services = compose_security.get("services")
    if not isinstance(raw_services, dict):
        raise ValueError("Docker Compose config services are invalid")
    active: dict[str, dict[str, object]] = {}
    for name, raw in raw_services.items():
        if not isinstance(name, str) or not name or not isinstance(raw, dict):
            raise ValueError("Docker Compose config services are invalid")
        profiles = raw.get("profiles")
        if not isinstance(profiles, list) or not all(
            isinstance(profile, str) and profile for profile in profiles
        ):
            raise ValueError("Docker Compose service profiles are invalid")
        if profiles:
            continue
        image = raw.get("image")
        restart = raw.get("restart")
        if not isinstance(image, str) or not image or not isinstance(restart, str) or not restart:
            raise ValueError("Docker Compose active service is invalid")
        active[name] = {"image": image, "restart": restart}
    if set(active) != set(candidate_services):
        raise ValueError("merged Docker Compose active services do not match candidate")
    for name, expected in candidate_services.items():
        if active[name]["image"] != expected.get("image"):
            raise ValueError("merged Docker Compose images do not match candidate")
    return active


def _string_list(raw: object, *, label: str) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        raise ValueError(f"Docker container inspect {label} is invalid")
    return list(raw)


def _mount_facts(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise ValueError("Docker container inspect mounts are invalid")
    mounts: list[dict[str, object]] = []
    for mount in raw:
        if not isinstance(mount, dict):
            raise ValueError("Docker container inspect mounts are invalid")
        mount_type = mount.get("Type")
        source = mount.get("Source")
        destination = mount.get("Destination")
        read_write = mount.get("RW")
        propagation = mount.get("Propagation", "")
        if (
            mount_type not in {"bind", "volume", "tmpfs"}
            or not isinstance(source, str)
            or not isinstance(destination, str)
            or not destination.startswith("/")
            or not isinstance(read_write, bool)
            or not isinstance(propagation, str)
        ):
            raise ValueError("Docker container inspect mounts are invalid")
        mounts.append(
            {
                "type": mount_type,
                "source": source,
                "destination": destination,
                "readOnly": not read_write,
                "propagation": propagation,
            }
        )
    return sorted(mounts, key=lambda item: (str(item["destination"]), str(item["source"])))


def _container_fact(raw: Mapping[str, object]) -> dict[str, object]:
    if set(raw) != {
        "containerId",
        "localImageId",
        "configImage",
        "project",
        "service",
        "configHash",
        "privileged",
        "mounts",
        "capAdd",
        "capDrop",
        "command",
        "entrypoint",
        "user",
        "environmentHashes",
        "state",
        "running",
        "restarting",
        "exitCode",
        "health",
    }:
        raise ValueError("Docker container inspect output is invalid")
    container_id = raw.get("containerId")
    service = raw.get("service")
    project = raw.get("project")
    image = raw.get("configImage")
    local_image_id = raw.get("localImageId")
    status = raw.get("state")
    running = raw.get("running")
    restarting = raw.get("restarting")
    exit_code = raw.get("exitCode")
    health = raw.get("health")
    config_hash = raw.get("configHash")
    privileged = raw.get("privileged")
    user = raw.get("user")
    if (
        not all(
            isinstance(value, str) and value
            for value in (container_id, service, project, image, local_image_id, status, health)
        )
        or not isinstance(running, bool)
        or not isinstance(restarting, bool)
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not isinstance(config_hash, str)
        or _SHA256.fullmatch(config_hash) is None
        or not isinstance(privileged, bool)
        or not isinstance(user, str)
    ):
        raise ValueError("Docker container inspect output is invalid")
    security = {
        "configHash": config_hash,
        "privileged": privileged,
        "mounts": _mount_facts(raw.get("mounts")),
        "capAdd": _capabilities(raw.get("capAdd"), label="container capAdd"),
        "capDrop": _capabilities(raw.get("capDrop"), label="container capDrop"),
        "command": _string_list(raw.get("command"), label="command"),
        "entrypoint": _string_list(raw.get("entrypoint"), label="entrypoint"),
        "user": user,
        "environmentSha256": _environment_hash_digest(
            raw.get("environmentHashes"), label="container inspect"
        ),
    }
    return {
        "containerId": container_id,
        "service": service,
        "project": project,
        "configImage": image,
        "localImageId": local_image_id,
        "state": status,
        "running": running,
        "restarting": restarting,
        "health": health,
        "exitCode": exit_code,
        "security": security,
    }


def _snapshot(facts: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "containerId": fact["containerId"],
            "service": fact["service"],
            "image": fact["configImage"],
            "state": fact["state"],
            "health": fact["health"],
            "exitCode": fact["exitCode"],
            "securitySha256": hashlib.sha256(
                json.dumps(
                    fact["security"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        for fact in sorted(facts, key=lambda item: str(item["service"]))
    ]


def _normalized_repo_digest(reference: str) -> str:
    tagged, separator, digest = reference.rpartition("@")
    if not separator:
        raise ValueError("candidate image reference is invalid")
    last_slash = tagged.rfind("/")
    tag_separator = tagged.rfind(":")
    repository = tagged[:tag_separator] if tag_separator > last_slash else tagged
    return f"{repository}@{digest}"


def _capabilities(raw: object, *, label: str) -> list[str]:
    values = _string_list(raw, label=label) or []
    normalized: list[str] = []
    for value in values:
        capability = value.upper().removeprefix("CAP_")
        if not capability or not re.fullmatch(r"[A-Z0-9_]+", capability):
            raise ValueError(f"Docker {label} capabilities are invalid")
        normalized.append(capability)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"Docker {label} capabilities are invalid")
    return sorted(normalized)


def _compose_mounts(
    service: Mapping[str, object],
    compose_security: Mapping[str, object],
    *,
    image: Mapping[str, object],
) -> list[dict[str, object]]:
    raw_volumes = service.get("volumes")
    if not isinstance(raw_volumes, list):
        raise ValueError("Docker Compose service volumes are invalid")
    top_volumes = compose_security.get("volumes")
    top_secrets = compose_security.get("secrets")
    top_configs = compose_security.get("configs")
    if not all(isinstance(item, dict) for item in (top_volumes, top_secrets, top_configs)):
        raise ValueError("Docker Compose mount declarations are invalid")
    mounts: list[dict[str, object]] = []
    for raw in raw_volumes:
        if not isinstance(raw, dict):
            raise ValueError("Docker Compose service volumes are invalid")
        mount_type = raw.get("type")
        source = raw.get("source", "")
        target = raw.get("target")
        read_only = raw.get("read_only", False)
        if (
            mount_type not in {"bind", "volume", "tmpfs"}
            or not isinstance(source, str)
            or not isinstance(target, str)
            or not target.startswith("/")
            or not isinstance(read_only, bool)
        ):
            raise ValueError("Docker Compose service volumes are invalid")
        propagation = ""
        expected_source: str | None = source
        if mount_type == "bind":
            bind = raw.get("bind") or {}
            if not isinstance(bind, dict):
                raise ValueError("Docker Compose bind mount is invalid")
            propagation = bind.get("propagation", "rprivate")
            if not isinstance(propagation, str):
                raise ValueError("Docker Compose bind mount is invalid")
        elif mount_type == "volume":
            declaration = top_volumes.get(source)
            if declaration is None:
                expected_source = source
            elif isinstance(declaration, dict):
                default_name = (
                    source if declaration.get("external") is True else f"{PROJECT}_{source}"
                )
                name = declaration.get("name", default_name)
                if not isinstance(name, str) or not name:
                    raise ValueError("Docker Compose named volume is invalid")
                expected_source = name
            else:
                raise ValueError("Docker Compose named volume is invalid")
        else:
            expected_source = ""
        mounts.append(
            {
                "type": mount_type,
                "source": expected_source,
                "destination": target,
                "readOnly": read_only,
                "propagation": propagation,
            }
        )

    for key, default_prefix, declarations in (
        ("secrets", "/run/secrets", top_secrets),
        ("configs", "/", top_configs),
    ):
        raw_items = service.get(key)
        if not isinstance(raw_items, list):
            raise ValueError(f"Docker Compose service {key} are invalid")
        for raw in raw_items:
            if isinstance(raw, str):
                source = raw
                target = f"{default_prefix.rstrip('/')}/{raw}"
            elif isinstance(raw, dict):
                source = raw.get("source")
                configured_target = raw.get("target")
                if configured_target is None:
                    target = f"{default_prefix.rstrip('/')}/{source}"
                else:
                    if not isinstance(configured_target, str) or not configured_target:
                        raise ValueError(f"Docker Compose service {key} are invalid")
                    target = (
                        configured_target
                        if configured_target.startswith("/")
                        else f"{default_prefix.rstrip('/')}/{configured_target}"
                    )
            else:
                raise ValueError(f"Docker Compose service {key} are invalid")
            declaration = declarations.get(source) if isinstance(source, str) else None
            file_path = declaration.get("file") if isinstance(declaration, dict) else None
            if (
                not isinstance(source, str)
                or not source
                or not isinstance(target, str)
                or not target.startswith("/")
                or not isinstance(file_path, str)
                or not file_path
            ):
                raise ValueError(f"Docker Compose service {key} are invalid")
            mounts.append(
                {
                    "type": "bind",
                    "source": file_path,
                    "destination": target,
                    "readOnly": True,
                    "propagation": "rprivate",
                }
            )

    image_volumes = image.get("volumes")
    if image_volumes is not None and not isinstance(image_volumes, dict):
        raise ValueError("Docker image declared volumes are invalid")
    occupied = {str(item["destination"]) for item in mounts}
    for target in sorted(image_volumes or {}):
        if not isinstance(target, str) or not target.startswith("/"):
            raise ValueError("Docker image declared volumes are invalid")
        if target not in occupied:
            raise ValueError("Docker image declared volumes must be explicitly bound by Compose")
    destinations = [str(item["destination"]) for item in mounts]
    if len(destinations) != len(set(destinations)):
        raise ValueError("Docker Compose mount destinations are duplicated")
    return sorted(mounts, key=lambda item: str(item["destination"]))


def _expected_security(
    service_name: str,
    *,
    compose_security: Mapping[str, object],
    image: Mapping[str, object],
    compose_hash: str,
) -> dict[str, object]:
    services = compose_security.get("services")
    service = services.get(service_name) if isinstance(services, dict) else None
    if not isinstance(service, dict) or service.get("image") != image.get("reference"):
        raise ValueError("Docker Compose security projection is invalid")
    privileged = service.get("privileged")
    user = service.get("user")
    if not isinstance(privileged, bool) or (user is not None and not isinstance(user, str)):
        raise ValueError("Docker Compose security projection is invalid")
    command = service.get("command")
    entrypoint = service.get("entrypoint")
    if command is not None:
        command = _string_list(command, label="Compose command")
    else:
        command = image.get("command")
    if entrypoint is not None:
        entrypoint = _string_list(entrypoint, label="Compose entrypoint")
    else:
        entrypoint = image.get("entrypoint")
    expected_user = image.get("user") if user is None else user
    if not isinstance(expected_user, str):
        raise ValueError("Docker Compose security projection is invalid")
    image_environment = image.get("environmentHashes")
    compose_environment = service.get("environmentHashes")
    if not isinstance(image_environment, dict) or not isinstance(compose_environment, dict):
        raise ValueError("Docker Compose environment projection is invalid")
    environment = dict(image_environment)
    for name, value in compose_environment.items():
        if (
            not isinstance(name, str)
            or not name
            or (
                value is not None
                and (not isinstance(value, str) or _SHA256.fullmatch(value) is None)
            )
        ):
            raise ValueError("Docker Compose environment projection is invalid")
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return {
        "configHash": compose_hash,
        "privileged": privileged,
        "mounts": _compose_mounts(service, compose_security, image=image),
        "capAdd": _capabilities(service.get("capAdd"), label="Compose capAdd"),
        "capDrop": _capabilities(service.get("capDrop"), label="Compose capDrop"),
        "command": command,
        "entrypoint": entrypoint,
        "user": expected_user,
        "environmentSha256": _environment_hash_digest(environment, label="expected container"),
    }


def _security_matches(observed: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    if set(observed) != set(expected):
        return False
    observed_mounts = observed.get("mounts")
    expected_mounts = expected.get("mounts")
    if not isinstance(observed_mounts, list) or not isinstance(expected_mounts, list):
        return False
    observed_without_mounts = {key: value for key, value in observed.items() if key != "mounts"}
    expected_without_mounts = {key: value for key, value in expected.items() if key != "mounts"}
    if observed_without_mounts != expected_without_mounts or len(observed_mounts) != len(
        expected_mounts
    ):
        return False
    for actual, wanted in zip(observed_mounts, expected_mounts, strict=True):
        if not isinstance(actual, dict) or not isinstance(wanted, dict):
            return False
        if any(
            actual.get(key) != wanted.get(key)
            for key in ("type", "destination", "readOnly", "propagation")
        ):
            return False
        expected_source = wanted.get("source")
        if expected_source is None:
            if not isinstance(actual.get("source"), str) or not actual["source"]:
                return False
        elif actual.get("source") != expected_source:
            return False
    return True


def _validate_container_facts(
    facts: Sequence[Mapping[str, object]],
    *,
    expected_services: Mapping[str, Mapping[str, object]],
    image_facts: Mapping[str, Mapping[str, object]],
    compose_hashes: Mapping[str, str],
    compose_security: Mapping[str, object],
) -> None:
    observed_services = [str(fact["service"]) for fact in facts]
    if len(observed_services) != len(set(observed_services)) or set(observed_services) != set(
        expected_services
    ):
        raise ValueError("runtime service set does not match candidate Compose")
    for fact in facts:
        service = str(fact["service"])
        expected = expected_services[service]
        reference = expected["image"]
        if fact["project"] != PROJECT or fact["configImage"] != reference:
            raise ValueError("runtime service labels or image do not match candidate Compose")
        security = fact.get("security")
        if not isinstance(security, dict):
            raise ValueError("runtime security-sensitive configuration is invalid")
        image = image_facts.get(str(reference))
        if not isinstance(image, Mapping):
            raise ValueError("runtime image identity does not match the candidate digest")
        expected_security = _expected_security(
            service,
            compose_security=compose_security,
            image=image,
            compose_hash=str(compose_hashes.get(service, "")),
        )
        if not _security_matches(security, expected_security):
            raise ValueError("runtime security-sensitive configuration does not match candidate")
        one_shot = expected["restart"] == "no"
        if one_shot:
            state_is_valid = (
                fact["state"] == "exited"
                and fact["running"] is False
                and fact["restarting"] is False
                and fact["exitCode"] == 0
            )
        else:
            state_is_valid = (
                fact["state"] == "running"
                and fact["running"] is True
                and fact["restarting"] is False
            )
        if not state_is_valid:
            raise ValueError("runtime container state does not match candidate Compose")
        if service in _REQUIRED_HEALTHY_SERVICES and fact["health"] != "healthy":
            raise ValueError("required runtime service is not healthy")
        if fact["localImageId"] != image.get("id") or _normalized_repo_digest(
            reference
        ) not in image.get("repoDigests", []):
            raise ValueError("runtime image identity does not match the candidate digest")


def _file_identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _open_windows_directory_handle(
    path: Path, *, deletable: bool = False
) -> tuple[object, tuple[int, int]]:
    import ctypes
    from ctypes import wintypes

    class _FileAttributeTagInformation(ctypes.Structure):
        _fields_ = (
            ("fileAttributes", wintypes.DWORD),
            ("reparseTag", wintypes.DWORD),
        )

    class _FileId128(ctypes.Structure):
        _fields_ = (("identifier", ctypes.c_ubyte * 16),)

    class _FileIdInformation(ctypes.Structure):
        _fields_ = (
            ("volumeSerialNumber", ctypes.c_ulonglong),
            ("fileId", _FileId128),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x00100000 | 0x00000001 | 0x00000080 | (0x00010000 if deletable else 0),
        0x00000001 | 0x00000002 | (0x00000004 if deletable else 0),
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, f"cannot secure runtime directory handle: {path}")
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    attributes = _FileAttributeTagInformation()
    if not get_information(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
        error = ctypes.get_last_error()
        _close_windows_handle(handle)
        raise OSError(error, f"cannot inspect runtime directory handle: {path}")
    if not attributes.fileAttributes & 0x00000010 or attributes.fileAttributes & 0x00000400:
        _close_windows_handle(handle)
        raise ValueError("runtime directory handle is not a plain directory")
    file_id = _FileIdInformation()
    if not get_information(handle, 18, ctypes.byref(file_id), ctypes.sizeof(file_id)):
        error = ctypes.get_last_error()
        _close_windows_handle(handle)
        raise OSError(error, f"cannot identify runtime directory handle: {path}")
    identity = (
        file_id.volumeSerialNumber,
        int.from_bytes(bytes(file_id.fileId.identifier), byteorder="little"),
    )
    return handle, identity


def _close_windows_handle(handle: object) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(handle):
        error = ctypes.get_last_error()
        raise OSError(error, "cannot close runtime directory handle")


def _windows_handle_identity(
    handle: object,
    *,
    directory: bool,
) -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes

    class _FileAttributeTagInformation(ctypes.Structure):
        _fields_ = (
            ("fileAttributes", wintypes.DWORD),
            ("reparseTag", wintypes.DWORD),
        )

    class _FileId128(ctypes.Structure):
        _fields_ = (("identifier", ctypes.c_ubyte * 16),)

    class _FileIdInformation(ctypes.Structure):
        _fields_ = (
            ("volumeSerialNumber", ctypes.c_ulonglong),
            ("fileId", _FileId128),
        )

    get_information = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    attributes = _FileAttributeTagInformation()
    if not get_information(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
        error = ctypes.get_last_error()
        raise OSError(error, "cannot inspect secured Windows handle")
    is_directory = bool(attributes.fileAttributes & 0x00000010)
    if attributes.fileAttributes & 0x00000400 or is_directory != directory:
        raise ValueError("secured Windows handle has an invalid type or reparse boundary")
    file_id = _FileIdInformation()
    if not get_information(handle, 18, ctypes.byref(file_id), ctypes.sizeof(file_id)):
        error = ctypes.get_last_error()
        raise OSError(error, "cannot identify secured Windows handle")
    return (
        file_id.volumeSerialNumber,
        int.from_bytes(bytes(file_id.fileId.identifier), byteorder="little"),
    )


def _nt_create_relative(
    directory_handle: object,
    name: str,
    *,
    desired_access: int,
    share_access: int,
    disposition: int,
    create_options: int,
) -> object:
    import ctypes
    from ctypes import wintypes

    class _UnicodeString(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.USHORT),
            ("maximumLength", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        )

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.ULONG),
            ("rootDirectory", wintypes.HANDLE),
            ("objectName", ctypes.POINTER(_UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("securityDescriptor", wintypes.LPVOID),
            ("securityQualityOfService", wintypes.LPVOID),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (("status", ctypes.c_void_p), ("information", ctypes.c_size_t))

    if Path(name).name != name or not name:
        raise ValueError("secured Windows relative entry name is invalid")
    name_buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(
        encoded_length,
        encoded_length + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    object_attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        directory_handle,
        ctypes.pointer(unicode_name),
        0x00000040,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll")
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    )
    nt_create_file.restype = wintypes.LONG
    status = nt_create_file(
        ctypes.byref(handle),
        desired_access,
        ctypes.byref(object_attributes),
        ctypes.byref(io_status),
        None,
        0,
        share_access,
        disposition,
        create_options,
        None,
        0,
    )
    if status < 0:
        status_to_error = ntdll.RtlNtStatusToDosError
        status_to_error.argtypes = (wintypes.LONG,)
        status_to_error.restype = wintypes.ULONG
        error = status_to_error(status)
        raise OSError(error, f"cannot open secured Windows relative entry: {name}")
    return handle


def _open_windows_regular_file_relative(
    directory_handle: object,
    name: str,
    *,
    share_access: int,
    deletable: bool = False,
) -> tuple[object, tuple[int, int]]:
    handle = _nt_create_relative(
        directory_handle,
        name,
        desired_access=(0x00100000 | 0x00000001 | 0x00000080 | (0x00010000 if deletable else 0)),
        share_access=share_access,
        disposition=1,
        create_options=0x00000020 | 0x00000040 | 0x00200000,
    )
    try:
        return handle, _windows_handle_identity(handle, directory=False)
    except BaseException:
        _close_windows_handle(handle)
        raise


def _open_windows_regular_file_path(
    path: Path,
    *,
    share_access: int,
) -> tuple[object, tuple[int, int]]:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000 | 0x00000080,
        share_access,
        None,
        3,
        0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, f"cannot open secured Windows regular file: {path}")
    try:
        return handle, _windows_handle_identity(handle, directory=False)
    except BaseException:
        _close_windows_handle(handle)
        raise


def _create_windows_directory_relative(
    directory_handle: object,
    name: str,
) -> tuple[object, tuple[int, int]]:
    handle = _nt_create_relative(
        directory_handle,
        name,
        desired_access=0x00100000 | 0x00010000 | 0x00000001 | 0x00000080,
        share_access=0x00000001 | 0x00000002,
        disposition=2,
        create_options=0x00000001 | 0x00000020 | 0x00200000,
    )
    try:
        return handle, _windows_handle_identity(handle, directory=True)
    except BaseException:
        try:
            _delete_windows_file_on_close(handle)
        finally:
            _close_windows_handle(handle)
        raise


def _open_windows_directory_relative(
    directory_handle: object,
    name: str,
) -> tuple[object, tuple[int, int]]:
    handle = _nt_create_relative(
        directory_handle,
        name,
        desired_access=0x00100000 | 0x00000001 | 0x00000080,
        share_access=0x00000001 | 0x00000002 | 0x00000004,
        disposition=1,
        create_options=0x00000001 | 0x00000020 | 0x00200000,
    )
    try:
        return handle, _windows_handle_identity(handle, directory=True)
    except BaseException:
        _close_windows_handle(handle)
        raise


def _create_windows_staging_file(
    directory_handle: object,
    name: str,
    body: bytes,
) -> tuple[object, tuple[int, int]]:
    import ctypes
    from ctypes import wintypes

    handle = _nt_create_relative(
        directory_handle,
        name,
        desired_access=0x80000000 | 0x40000000 | 0x00100000 | 0x00010000 | 0x00000080,
        share_access=0x00000001,
        disposition=2,
        create_options=0x00000020 | 0x00000040 | 0x00200000,
    )
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        write_file = kernel32.WriteFile
        write_file.argtypes = (
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        write_file.restype = wintypes.BOOL
        flush_file = kernel32.FlushFileBuffers
        flush_file.argtypes = (wintypes.HANDLE,)
        flush_file.restype = wintypes.BOOL
        offset = 0
        while offset < len(body):
            written = wintypes.DWORD()
            chunk = body[offset : offset + 1024 * 1024]
            buffer = ctypes.create_string_buffer(chunk)
            if not write_file(
                handle,
                buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                error = ctypes.get_last_error()
                raise OSError(error, f"cannot write runtime staging file: {name}")
            if written.value == 0:
                raise OSError("runtime staging write made no progress")
            offset += written.value
        if not flush_file(handle):
            error = ctypes.get_last_error()
            raise OSError(error, f"cannot flush runtime staging file: {name}")
        return handle, _windows_handle_identity(handle, directory=False)
    except BaseException:
        try:
            _delete_windows_file_on_close(handle)
        finally:
            _close_windows_handle(handle)
        raise


def _rename_windows_file_relative(
    file_handle: object,
    directory_handle: object,
    target_name: str,
    *,
    replace_existing: bool,
) -> None:
    import ctypes
    from ctypes import wintypes

    class _FileRenameInformation(ctypes.Structure):
        _fields_ = (
            ("replaceIfExists", wintypes.BOOLEAN),
            ("rootDirectory", wintypes.HANDLE),
            ("fileNameLength", wintypes.DWORD),
            ("fileName", wintypes.WCHAR * len(target_name)),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (("status", ctypes.c_void_p), ("information", ctypes.c_size_t))

    information = _FileRenameInformation()
    information.replaceIfExists = replace_existing
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
        _FileRenameInformation.fileName.offset + information.fileNameLength,
        10,
    )
    if status < 0:
        status_to_error = ntdll.RtlNtStatusToDosError
        status_to_error.argtypes = (wintypes.LONG,)
        status_to_error.restype = wintypes.ULONG
        error = status_to_error(status)
        raise OSError(error, f"cannot publish runtime staging file as {target_name}")


def _delete_windows_file_on_close(file_handle: object) -> None:
    import ctypes
    from ctypes import wintypes

    class _FileDispositionInformation(ctypes.Structure):
        _fields_ = (("deleteFile", wintypes.BOOLEAN),)

    information = _FileDispositionInformation(True)
    set_information = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    if not set_information(
        file_handle,
        4,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "cannot safely remove runtime staging file")


def _read_windows_file_handle(file_handle: object) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_size = kernel32.GetFileSizeEx
    get_size.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong))
    get_size.restype = wintypes.BOOL
    set_pointer = kernel32.SetFilePointerEx
    set_pointer.argtypes = (
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    )
    set_pointer.restype = wintypes.BOOL
    read_file = kernel32.ReadFile
    read_file.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    read_file.restype = wintypes.BOOL
    size = ctypes.c_longlong()
    if not get_size(file_handle, ctypes.byref(size)):
        error = ctypes.get_last_error()
        raise OSError(error, "cannot inspect runtime staging file size")
    position = ctypes.c_longlong()
    if not set_pointer(file_handle, 0, ctypes.byref(position), 0):
        error = ctypes.get_last_error()
        raise OSError(error, "cannot rewind runtime staging file")
    remaining = size.value
    chunks: list[bytes] = []
    while remaining:
        requested = min(remaining, 1024 * 1024)
        buffer = ctypes.create_string_buffer(requested)
        read = wintypes.DWORD()
        if not read_file(
            file_handle,
            buffer,
            requested,
            ctypes.byref(read),
            None,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "cannot read runtime staging file")
        if read.value == 0:
            raise OSError("runtime staging read ended before the recorded size")
        chunks.append(buffer.raw[: read.value])
        remaining -= read.value
    return b"".join(chunks)


def _read_posix_file_descriptor(file_descriptor: int) -> bytes:
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _rename_posix_no_replace(
    directory_fd: int,
    source: str,
    target: str,
) -> None:
    if any(Path(name).name != name or not name for name in (source, target)):
        raise ValueError("POSIX runtime relative rename name is invalid")
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2 is required for safe runtime rename")
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
                directory_fd,
                os.fsencode(source),
                directory_fd,
                os.fsencode(target),
                1,
            )
            != 0
        ):
            error = ctypes.get_errno()
            raise OSError(error, f"cannot safely rename runtime entry: {target}")
        return
    # Portable POSIX lacks renameat2(RENAME_NOREPLACE), but linkat-style hard
    # link creation is still atomic and refuses an existing target. This is
    # valid only for the regular files published by the runtime guards;
    # directories and platforms without dir-fd hard links fail closed.
    try:
        source_details = os.stat(
            source,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError:
        raise
    if not stat.S_ISREG(source_details.st_mode):
        raise OSError(
            errno.ENOTSUP,
            "atomic hard-link no-replace requires a regular file",
            source,
        )
    source_identity = _file_identity(source_details)

    try:
        os.link(
            source,
            target,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except (AttributeError, NotImplementedError, TypeError) as exc:
        raise OSError(
            errno.ENOTSUP,
            "atomic hard-link no-replace is unavailable",
            target,
        ) from exc

    def remove_linked_target() -> None:
        try:
            target_details = os.stat(
                target,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(target_details.st_mode)
            or _file_identity(target_details) != source_identity
        ):
            raise OSError(
                errno.EIO,
                "atomic hard-link no-replace cleanup identity changed",
                target,
            )
        os.unlink(target, dir_fd=directory_fd)

    try:
        linked_source = os.stat(
            source,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked_target = os.stat(
            target,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(linked_source.st_mode)
            or not stat.S_ISREG(linked_target.st_mode)
            or _file_identity(linked_source) != source_identity
            or _file_identity(linked_target) != source_identity
        ):
            raise OSError(
                errno.EIO,
                "atomic hard-link no-replace identity changed",
                target,
            )
    except OSError as publication_error:
        try:
            remove_linked_target()
        except OSError as cleanup_error:
            raise OSError(
                errno.EIO,
                "atomic hard-link no-replace validation cleanup failed",
                target,
            ) from cleanup_error
        raise publication_error

    try:
        os.unlink(source, dir_fd=directory_fd)
    except (AttributeError, NotImplementedError, OSError, TypeError) as unlink_error:
        try:
            remove_linked_target()
        except OSError as cleanup_error:
            raise OSError(
                errno.EIO,
                "atomic hard-link no-replace source cleanup failed",
                target,
            ) from cleanup_error
        raise OSError(
            errno.EIO,
            "atomic hard-link no-replace could not remove the staging name",
            source,
        ) from unlink_error


def _open_posix_directory_path_no_follow(path: Path) -> tuple[int, tuple[int, int]]:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.anchor:
        raise ValueError("candidate directory lease path is invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open(candidate.anchor, flags)
    try:
        for component in candidate.relative_to(candidate.anchor).parts:
            opened = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = opened
        details = os.fstat(current)
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError("candidate directory lease is not a directory")
        return current, _file_identity(details)
    except BaseException:
        os.close(current)
        raise


class _DeploymentContractLease:
    """Pin the base Compose and env-file used by the real deployment wrapper."""

    def __init__(self, deployment_root: Path) -> None:
        self.deployment_root = Path(deployment_root)
        self.paths = (self.deployment_root / "data" / "user" / "settings" / "docker.env",)
        self._handles: list[object | int] = []
        self._identities: list[tuple[int, int]] = []
        self._bodies: list[bytes] = []
        self._directory_handles: list[int] = []
        self._directory_identities: list[tuple[int, int]] = []

    @classmethod
    def open(cls, deployment_root: Path) -> _DeploymentContractLease:
        lease = cls(deployment_root)
        try:
            for path in lease.paths:
                if os.name == "nt":
                    _assert_no_link_ancestors(path)
                    handle, identity = _open_windows_regular_file_path(
                        path, share_access=0x00000001
                    )
                else:
                    directory_handle, directory_identity = _open_posix_directory_path_no_follow(
                        path.parent
                    )
                    lease._directory_handles.append(directory_handle)
                    lease._directory_identities.append(directory_identity)
                    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                    handle = os.open(path.name, flags, dir_fd=directory_handle)
                    details = os.fstat(handle)
                    if not stat.S_ISREG(details.st_mode):
                        raise ValueError("deployment contract is not a regular file")
                    identity = _file_identity(details)
                lease._handles.append(handle)
                lease._identities.append(identity)
                lease._bodies.append(
                    _read_windows_file_handle(handle)
                    if os.name == "nt"
                    else _read_posix_file_descriptor(int(handle))
                )
            lease.assert_unchanged()
        except BaseException:
            lease.close(suppress_errors=True)
            raise
        return lease

    def __enter__(self) -> _DeploymentContractLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def close(self, *, suppress_errors: bool = False) -> None:
        errors: list[OSError] = []
        for handle in reversed(self._handles):
            try:
                if os.name == "nt":
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            except OSError as exc:
                errors.append(exc)
        self._handles.clear()
        for handle in reversed(self._directory_handles):
            try:
                os.close(handle)
            except OSError as exc:
                errors.append(exc)
        self._directory_handles.clear()
        if errors and not suppress_errors:
            raise errors[0]

    def assert_unchanged(self) -> None:
        if not (
            len(self._handles) == len(self._identities) == len(self._bodies) == len(self.paths)
        ):
            raise ValueError("deployment contract lease is incomplete")
        if os.name != "nt" and not (
            len(self._directory_handles) == len(self._directory_identities) == len(self.paths)
        ):
            raise ValueError("deployment contract directory lease is incomplete")
        for index, (path, handle, expected_identity, expected_body) in enumerate(
            zip(
                self.paths,
                self._handles,
                self._identities,
                self._bodies,
                strict=True,
            )
        ):
            held_identity = (
                _windows_handle_identity(handle, directory=False)
                if os.name == "nt"
                else _file_identity(os.fstat(int(handle)))
            )
            held_body = (
                _read_windows_file_handle(handle)
                if os.name == "nt"
                else _read_posix_file_descriptor(int(handle))
            )
            if os.name == "nt":
                _assert_no_link_ancestors(path)
                current_handle, current_identity = _open_windows_regular_file_path(
                    path, share_access=0x00000001
                )
                try:
                    current_body = _read_windows_file_handle(current_handle)
                finally:
                    _close_windows_handle(current_handle)
            else:
                directory_handle = self._directory_handles[index]
                if _file_identity(os.fstat(directory_handle)) != self._directory_identities[index]:
                    raise ValueError("deployment contract directory lease changed")
                current_directory, current_directory_identity = (
                    _open_posix_directory_path_no_follow(path.parent)
                )
                current_handle = None
                try:
                    if current_directory_identity != self._directory_identities[index]:
                        raise ValueError("deployment contract directory path changed")
                    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                    current_handle = os.open(path.name, flags, dir_fd=current_directory)
                    details = os.fstat(current_handle)
                    if not stat.S_ISREG(details.st_mode):
                        raise ValueError("deployment contract path is not a regular file")
                    current_identity = _file_identity(details)
                    current_body = _read_posix_file_descriptor(current_handle)
                finally:
                    if current_handle is not None:
                        os.close(current_handle)
                    os.close(current_directory)
            if (
                held_identity != expected_identity
                or current_identity != expected_identity
                or held_body != expected_body
                or current_body != expected_body
            ):
                raise ValueError("deployment runtime contract lease changed")


class _CandidateContractLease:
    def __init__(self, candidate_root: Path) -> None:
        self.candidate_root = Path(candidate_root)
        self.paths = (
            self.candidate_root / "deploy" / "image-lock.json",
            self.candidate_root / "docker-compose.platform.yml",
            self.candidate_root / "docker-compose.yml",
        )
        self._handles: list[object | int] = []
        self._identities: list[tuple[int, int]] = []
        self._bodies: list[bytes] = []
        self._candidate_directory_handle: int | None = None
        self._candidate_directory_identity: tuple[int, int] | None = None
        self._deploy_directory_handle: int | None = None
        self._deploy_directory_identity: tuple[int, int] | None = None

    @classmethod
    def open(cls, candidate_root: Path) -> _CandidateContractLease:
        lease = cls(candidate_root)
        try:
            if os.name == "nt":
                for path in lease.paths:
                    _assert_no_link_ancestors(path)
                    handle, identity = _open_windows_regular_file_path(
                        path,
                        share_access=0x00000001,
                    )
                    lease._handles.append(handle)
                    body = _read_windows_file_handle(handle)
                    lease._identities.append(identity)
                    lease._bodies.append(body)
            else:
                (
                    lease._candidate_directory_handle,
                    lease._candidate_directory_identity,
                ) = _open_posix_directory_path_no_follow(lease.candidate_root)
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                lease._deploy_directory_handle = os.open(
                    "deploy",
                    directory_flags,
                    dir_fd=lease._candidate_directory_handle,
                )
                deploy_details = os.fstat(lease._deploy_directory_handle)
                lease._deploy_directory_identity = _file_identity(deploy_details)
                flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                for directory_handle, name in (
                    (lease._deploy_directory_handle, "image-lock.json"),
                    (lease._candidate_directory_handle, "docker-compose.platform.yml"),
                    (lease._candidate_directory_handle, "docker-compose.yml"),
                ):
                    handle = os.open(name, flags, dir_fd=directory_handle)
                    lease._handles.append(handle)
                    details = os.fstat(handle)
                    if not stat.S_ISREG(details.st_mode):
                        raise ValueError("candidate contract lease is not a regular file")
                    identity = _file_identity(details)
                    body = _read_posix_file_descriptor(handle)
                    lease._identities.append(identity)
                    lease._bodies.append(body)
            lease.assert_unchanged()
        except BaseException:
            lease.close(suppress_errors=True)
            raise
        return lease

    def __enter__(self) -> _CandidateContractLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    @property
    def token(self) -> tuple[bytes, bytes]:
        if len(self._bodies) != 3:
            raise ValueError("candidate contract lease is incomplete")
        return self._bodies[0], self._bodies[1]

    def close(self, *, suppress_errors: bool = False) -> None:
        errors: list[OSError] = []
        for handle in reversed(self._handles):
            try:
                if os.name == "nt":
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            except OSError as exc:
                errors.append(exc)
        self._handles.clear()
        for attribute in ("_deploy_directory_handle", "_candidate_directory_handle"):
            handle = getattr(self, attribute)
            if handle is None:
                continue
            try:
                os.close(handle)
            except OSError as exc:
                errors.append(exc)
            setattr(self, attribute, None)
        if errors and not suppress_errors:
            raise errors[0]

    def assert_unchanged(self) -> None:
        if not (
            len(self._handles) == len(self._identities) == len(self._bodies) == len(self.paths)
        ):
            raise ValueError("candidate contract lease is incomplete")
        if os.name != "nt":
            self._assert_posix_unchanged()
            return
        for path, handle, expected_identity, expected_body in zip(
            self.paths,
            self._handles,
            self._identities,
            self._bodies,
            strict=True,
        ):
            handle_identity = _windows_handle_identity(handle, directory=False)
            handle_body = _read_windows_file_handle(handle)
            current_handle, current_identity = _open_windows_regular_file_path(
                path,
                share_access=0x00000001,
            )
            try:
                current_body = _read_windows_file_handle(current_handle)
            finally:
                _close_windows_handle(current_handle)
            if (
                handle_identity != expected_identity
                or current_identity != expected_identity
                or handle_body != expected_body
                or current_body != expected_body
            ):
                raise ValueError("candidate runtime contract lease changed")

    def _assert_posix_unchanged(self) -> None:
        if (
            self._candidate_directory_handle is None
            or self._candidate_directory_identity is None
            or self._deploy_directory_handle is None
            or self._deploy_directory_identity is None
        ):
            raise ValueError("candidate directory lease is incomplete")
        if (
            _file_identity(os.fstat(self._candidate_directory_handle))
            != self._candidate_directory_identity
            or _file_identity(os.fstat(self._deploy_directory_handle))
            != self._deploy_directory_identity
        ):
            raise ValueError("candidate directory lease changed")
        current_candidate: int | None = None
        current_deploy: int | None = None
        try:
            current_candidate, candidate_identity = _open_posix_directory_path_no_follow(
                self.candidate_root
            )
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            current_deploy = os.open("deploy", directory_flags, dir_fd=current_candidate)
            deploy_identity = _file_identity(os.fstat(current_deploy))
            if (
                candidate_identity != self._candidate_directory_identity
                or deploy_identity != self._deploy_directory_identity
            ):
                raise ValueError("candidate directory path binding changed")
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            locations = (
                (current_deploy, "image-lock.json"),
                (current_candidate, "docker-compose.platform.yml"),
                (current_candidate, "docker-compose.yml"),
            )
            for (
                held_handle,
                expected_identity,
                expected_body,
                (directory_handle, name),
            ) in zip(
                self._handles,
                self._identities,
                self._bodies,
                locations,
                strict=True,
            ):
                held_details = os.fstat(int(held_handle))
                held_identity = _file_identity(held_details)
                held_body = _read_posix_file_descriptor(int(held_handle))
                current_handle = os.open(name, flags, dir_fd=directory_handle)
                confirmation_handle: int | None = None
                try:
                    current_details = os.fstat(current_handle)
                    if not stat.S_ISREG(current_details.st_mode):
                        raise ValueError("candidate contract path is not a regular file")
                    current_identity = _file_identity(current_details)
                    current_body = _read_posix_file_descriptor(current_handle)
                    confirmation_handle = os.open(name, flags, dir_fd=directory_handle)
                    confirmation_details = os.fstat(confirmation_handle)
                    if not stat.S_ISREG(confirmation_details.st_mode):
                        raise ValueError("candidate contract confirmation is not a regular file")
                    confirmation_identity = _file_identity(confirmation_details)
                    confirmation_body = _read_posix_file_descriptor(confirmation_handle)
                finally:
                    if confirmation_handle is not None:
                        os.close(confirmation_handle)
                    os.close(current_handle)
                if (
                    held_identity != expected_identity
                    or current_identity != expected_identity
                    or confirmation_identity != expected_identity
                    or held_body != expected_body
                    or current_body != expected_body
                    or confirmation_body != expected_body
                ):
                    raise ValueError("candidate runtime contract lease changed")
            confirmed_candidate, confirmed_candidate_identity = (
                _open_posix_directory_path_no_follow(self.candidate_root)
            )
            try:
                confirmed_deploy = os.open(
                    "deploy",
                    directory_flags,
                    dir_fd=confirmed_candidate,
                )
                try:
                    confirmed_deploy_identity = _file_identity(os.fstat(confirmed_deploy))
                finally:
                    os.close(confirmed_deploy)
            finally:
                os.close(confirmed_candidate)
            if (
                confirmed_candidate_identity != self._candidate_directory_identity
                or confirmed_deploy_identity != self._deploy_directory_identity
            ):
                raise ValueError("candidate directory path binding changed during validation")
        finally:
            if current_deploy is not None:
                os.close(current_deploy)
            if current_candidate is not None:
                os.close(current_candidate)


class _PublicationContractLease:
    def __init__(
        self,
        candidate: _CandidateContractLease,
        deployment: _DeploymentContractLease,
    ) -> None:
        self._candidate = candidate
        self._deployment = deployment

    def assert_unchanged(self) -> None:
        self._candidate.assert_unchanged()
        self._deployment.assert_unchanged()


class _RuntimeDirectoryGuard:
    def __init__(self, bundle_root: Path, runtime_root: Path) -> None:
        self.bundle_root = Path(bundle_root)
        self.runtime_root = Path(runtime_root)
        self._bundle_handle: object | int | None = None
        self._runtime_handle: object | int | None = None
        self._bundle_identity: tuple[int, int] | None = None
        self._runtime_identity: tuple[int, int] | None = None
        self._owned_directories: dict[str, tuple[int, int]] = {}
        self._owned_directory_handles: dict[str, object] = {}
        self._owned_file_handles: dict[tuple[int, int], object] = {}
        self._owned_file_names: dict[tuple[int, int], str] = {}

    @classmethod
    def open(cls, bundle_root: Path, runtime_root: Path) -> _RuntimeDirectoryGuard:
        guard = cls(bundle_root, runtime_root)
        try:
            if os.name == "nt":
                guard._bundle_handle, guard._bundle_identity = _open_windows_directory_handle(
                    guard.bundle_root
                )
                guard._runtime_handle, guard._runtime_identity = _open_windows_directory_handle(
                    guard.runtime_root
                )
            else:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                guard._bundle_handle = os.open(guard.bundle_root, flags)
                guard._runtime_handle = os.open(
                    "runtime",
                    flags,
                    dir_fd=int(guard._bundle_handle),
                )
                guard._bundle_identity = _file_identity(os.fstat(int(guard._bundle_handle)))
                guard._runtime_identity = _file_identity(os.fstat(int(guard._runtime_handle)))
            guard.assert_bound()
        except BaseException:
            guard.close(suppress_errors=True)
            raise
        return guard

    def __enter__(self) -> _RuntimeDirectoryGuard:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        cleanup_error: BaseException | None = None
        for name in reversed(tuple(self._owned_directories)):
            try:
                self.remove_owned_empty_directory(name)
            except BaseException as error:
                cleanup_error = error
        try:
            self.close()
        except BaseException as error:
            cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            if exc_value is not None and hasattr(exc_value, "add_note"):
                exc_value.add_note(f"runtime cleanup also failed: {cleanup_error}")
            elif exc_value is None:
                raise cleanup_error
        return False

    @property
    def _runtime_fd(self) -> int:
        if os.name == "nt" or not isinstance(self._runtime_handle, int):
            raise ValueError("POSIX runtime directory handle is unavailable")
        return self._runtime_handle

    def close(self, *, suppress_errors: bool = False) -> None:
        errors: list[BaseException] = []
        for name, handle in tuple(self._owned_directory_handles.items()):
            try:
                if os.name == "nt":
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            except OSError as exc:
                errors.append(exc)
            self._owned_directory_handles.pop(name, None)
        for identity, handle in tuple(self._owned_file_handles.items()):
            try:
                try:
                    _delete_windows_file_on_close(handle)
                finally:
                    _close_windows_handle(handle)
            except BaseException as exc:
                errors.append(exc)
            self._owned_file_handles.pop(identity, None)
            self._owned_file_names.pop(identity, None)
        if os.name != "nt":
            for identity, name in tuple(self._owned_file_names.items()):
                try:
                    if not name or not self.remove_file_if_identity(name, identity):
                        raise ValueError("owned runtime staging file requires recovery")
                except BaseException as exc:
                    errors.append(exc)
        for attribute in ("_runtime_handle", "_bundle_handle"):
            handle = getattr(self, attribute)
            if handle is None:
                continue
            try:
                if os.name == "nt":
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            except OSError as exc:
                errors.append(exc)
            setattr(self, attribute, None)
        if errors and not suppress_errors:
            raise errors[0]

    def assert_bound(self) -> None:
        _assert_runtime_boundary(self.bundle_root, self.runtime_root)
        if os.name == "nt":
            bundle_handle, bundle_identity = _open_windows_directory_handle(self.bundle_root)
            runtime_handle, runtime_identity = _open_windows_directory_handle(self.runtime_root)
            try:
                if (
                    bundle_identity != self._bundle_identity
                    or runtime_identity != self._runtime_identity
                ):
                    raise ValueError("runtime directory handle is no longer bundle-bound")
            finally:
                _close_windows_handle(runtime_handle)
                _close_windows_handle(bundle_handle)
            return
        bundle_details = os.stat(self.bundle_root, follow_symlinks=False)
        runtime_details = os.stat(self.runtime_root, follow_symlinks=False)
        linked_runtime = os.stat(
            "runtime",
            dir_fd=int(self._bundle_handle),
            follow_symlinks=False,
        )
        if (
            _file_identity(bundle_details) != self._bundle_identity
            or _file_identity(runtime_details) != self._runtime_identity
            or _file_identity(linked_runtime) != self._runtime_identity
        ):
            raise ValueError("runtime directory handle is no longer bundle-bound")

    def _entry_details(self, name: str) -> os.stat_result:
        if Path(name).name != name:
            raise ValueError("runtime directory entry name is invalid")
        if os.name == "nt":
            return (self.runtime_root / name).lstat()
        return os.stat(name, dir_fd=self._runtime_fd, follow_symlinks=False)

    def create_empty_directory(self, name: str) -> Path:
        self.assert_bound()
        if Path(name).name != name or name in self._owned_directories:
            raise ValueError("isolated Docker config name is invalid")
        if os.name == "nt":
            try:
                config_handle, config_identity = _create_windows_directory_relative(
                    self._runtime_handle,
                    name,
                )
            except OSError as exc:
                raise ValueError("isolated Docker config directory cannot be created") from exc
            try:
                check_handle, check_identity = _open_windows_directory_relative(
                    self._runtime_handle,
                    name,
                )
                _close_windows_handle(check_handle)
                if check_identity != config_identity:
                    raise ValueError("isolated Docker config identity changed during creation")
            except BaseException:
                try:
                    _delete_windows_file_on_close(config_handle)
                finally:
                    _close_windows_handle(config_handle)
                raise
            self._owned_directory_handles[name] = config_handle
            self._owned_directories[name] = config_identity
        else:
            config_fd: int | None = None
            created_identity: tuple[int, int] | None = None
            created = False
            try:
                # POSIX mkdirat does not return the created directory's fd. The
                # checks on both sides of openat reject every observable swap;
                # a non-cooperating same-UID swap before the first stat is not
                # distinguishable with portable POSIX APIs.
                os.mkdir(name, mode=0o700, dir_fd=self._runtime_fd)
                created = True
                created_details = self._entry_details(name)
                created_identity = _file_identity(created_details)
                if not stat.S_ISDIR(created_details.st_mode):
                    raise ValueError("isolated Docker config creation is not a directory")
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                config_fd = os.open(name, flags, dir_fd=self._runtime_fd)
                opened_details = os.fstat(config_fd)
                linked_details = self._entry_details(name)
                if (
                    not stat.S_ISDIR(opened_details.st_mode)
                    or _file_identity(opened_details) != created_identity
                    or _file_identity(linked_details) != created_identity
                ):
                    raise ValueError("isolated Docker config identity changed during creation")
            except BaseException as creation_error:
                if config_fd is not None:
                    os.close(config_fd)
                if created and created_identity is None:
                    if hasattr(creation_error, "add_note"):
                        creation_error.add_note("isolated Docker config creation recovery required")
                    raise
                if created:
                    cleanup_fd: int | None = None
                    try:
                        current_details = self._entry_details(name)
                        if (
                            not stat.S_ISDIR(current_details.st_mode)
                            or _file_identity(current_details) != created_identity
                        ):
                            raise ValueError(
                                "isolated Docker config creation left a replacement; recovery required"
                            )
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                        cleanup_fd = os.open(name, flags, dir_fd=self._runtime_fd)
                        if _file_identity(os.fstat(cleanup_fd)) != created_identity or os.listdir(
                            cleanup_fd
                        ):
                            raise ValueError(
                                "isolated Docker config creation cleanup requires recovery"
                            )
                        final_details = self._entry_details(name)
                        if _file_identity(final_details) != created_identity:
                            raise ValueError(
                                "isolated Docker config creation cleanup identity changed"
                            )
                        # POSIX has no rmdir-by-fd; every observable drift is
                        # rejected, with the final same-UID stat/rmdir interval
                        # retained as the explicit portable platform limit.
                        os.rmdir(name, dir_fd=self._runtime_fd)
                    except BaseException as cleanup_error:
                        raise ValueError(
                            f"isolated Docker config creation requires recovery: {cleanup_error}"
                        ) from creation_error
                    finally:
                        if cleanup_fd is not None:
                            os.close(cleanup_fd)
                if created or isinstance(creation_error, ValueError):
                    raise
                raise ValueError(
                    "isolated Docker config directory cannot be created"
                ) from creation_error
            assert config_fd is not None and created_identity is not None
            self._owned_directory_handles[name] = config_fd
            self._owned_directories[name] = created_identity
        self.assert_owned_directory_empty(name)
        return self.runtime_root / name

    def assert_owned_directory_empty(self, name: str) -> None:
        expected_identity = self._owned_directories.get(name)
        if expected_identity is None:
            raise ValueError("isolated Docker config boundary is invalid")
        if os.name == "nt":
            check_handle, check_identity = _open_windows_directory_relative(
                self._runtime_handle,
                name,
            )
            try:
                if check_identity != expected_identity:
                    raise ValueError("isolated Docker config boundary is invalid")
                entries = list((self.runtime_root / name).iterdir())
            finally:
                _close_windows_handle(check_handle)
        else:
            details = self._entry_details(name)
            if _file_identity(details) != expected_identity or not stat.S_ISDIR(details.st_mode):
                raise ValueError("isolated Docker config boundary is invalid")
            config_fd = self._owned_directory_handles.get(name)
            if (
                not isinstance(config_fd, int)
                or _file_identity(os.fstat(config_fd)) != expected_identity
            ):
                raise ValueError("isolated Docker config boundary is invalid")
            entries = os.listdir(config_fd)
        if entries:
            raise ValueError("isolated Docker config directory is not empty")

    def remove_owned_empty_directory(self, name: str) -> None:
        self.assert_owned_directory_empty(name)
        expected_identity = self._owned_directories[name]
        if os.name == "nt":
            handle = self._owned_directory_handles.pop(name)
            try:
                _delete_windows_file_on_close(handle)
            finally:
                _close_windows_handle(handle)
            try:
                replacement_handle, _replacement_identity = _open_windows_directory_relative(
                    self._runtime_handle,
                    name,
                )
            except FileNotFoundError:
                pass
            else:
                _close_windows_handle(replacement_handle)
                self._owned_directories.pop(name, None)
                raise ValueError("isolated Docker config cleanup left a replacement name")
        else:
            quarantine = f".{name}.{uuid.uuid4().hex}.cleanup"
            _rename_posix_no_replace(
                self._runtime_fd,
                name,
                quarantine,
            )
            quarantined = os.stat(
                quarantine,
                dir_fd=self._runtime_fd,
                follow_symlinks=False,
            )
            if _file_identity(quarantined) != expected_identity:
                raise ValueError(
                    "isolated Docker config cleanup quarantined a replacement; recovery required"
                )
            config_fd = self._owned_directory_handles.get(name)
            if not isinstance(config_fd, int) or os.listdir(config_fd):
                raise ValueError("isolated Docker config recovery directory is not empty")
            final_details = os.stat(
                quarantine,
                dir_fd=self._runtime_fd,
                follow_symlinks=False,
            )
            if _file_identity(final_details) != expected_identity or not stat.S_ISDIR(
                final_details.st_mode
            ):
                raise ValueError("isolated Docker config cleanup target identity changed")
            # Portable POSIX has no rmdir-by-fd operation. This last identity
            # check fails closed on observable drift; a non-cooperating same-UID
            # swap between this stat and rmdir is the remaining platform limit.
            os.rmdir(quarantine, dir_fd=self._runtime_fd)
            self._owned_directory_handles.pop(name, None)
            os.close(config_fd)
            try:
                os.stat(name, dir_fd=self._runtime_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("isolated Docker config cleanup left a replacement name")
        self._owned_directories.pop(name, None)

    def write_new_file(self, name: str, body: bytes) -> tuple[int, int]:
        if Path(name).name != name:
            raise ValueError("runtime staging name is invalid")
        if os.name == "nt":
            handle, identity = _create_windows_staging_file(self._runtime_handle, name, body)
            self._owned_file_handles[identity] = handle
            self._owned_file_names[identity] = name
            return identity
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        file_descriptor = os.open(name, flags, 0o600, dir_fd=self._runtime_fd)
        identity: tuple[int, int] | None = None
        try:
            details = os.fstat(file_descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("runtime staging file is not a plain file")
            identity = _file_identity(details)
            self._owned_file_names[identity] = name
            with os.fdopen(file_descriptor, "wb", closefd=False) as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException as write_error:
            if identity is None:
                try:
                    fallback_details = os.stat(file_descriptor)
                    if stat.S_ISREG(fallback_details.st_mode):
                        identity = _file_identity(fallback_details)
                        self._owned_file_names[identity] = name
                except OSError:
                    pass
            os.close(file_descriptor)
            if identity is not None:
                try:
                    if not self.remove_file_if_identity(name, identity):
                        raise ValueError("runtime staging initialization requires recovery")
                except BaseException as cleanup_error:
                    raise ValueError(
                        f"runtime staging initialization requires recovery: {cleanup_error}"
                    ) from write_error
            else:
                raise ValueError(
                    "runtime staging initialization requires recovery"
                ) from write_error
            raise
        os.close(file_descriptor)
        assert identity is not None
        return identity

    def read_optional_regular_file(self, name: str) -> tuple[bytes | None, tuple[int, int] | None]:
        try:
            if os.name == "nt":
                file_handle, identity = _open_windows_regular_file_relative(
                    self._runtime_handle,
                    name,
                    share_access=0x00000001 | 0x00000002 | 0x00000004,
                )
                try:
                    return _read_windows_file_handle(file_handle), identity
                finally:
                    _close_windows_handle(file_handle)
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            file_descriptor = os.open(name, flags, dir_fd=self._runtime_fd)
        except FileNotFoundError:
            return None, None
        try:
            details = os.fstat(file_descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("runtime canonical is not a plain file")
            with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
                return handle.read(), _file_identity(details)
        finally:
            os.close(file_descriptor)

    def pin_windows_regular_file(
        self,
        name: str,
    ) -> tuple[bytes | None, tuple[int, int] | None, object | None]:
        if os.name != "nt":
            raise ValueError("Windows runtime target pinning is unavailable")
        try:
            handle, identity = _open_windows_regular_file_relative(
                self._runtime_handle,
                name,
                share_access=0x00000001,
                deletable=True,
            )
        except FileNotFoundError:
            return None, None, None
        try:
            return _read_windows_file_handle(handle), identity, handle
        except BaseException:
            _close_windows_handle(handle)
            raise

    def rename_windows_pinned_file(self, handle: object, target: str) -> None:
        if os.name != "nt":
            raise ValueError("Windows runtime target pinning is unavailable")
        _rename_windows_file_relative(
            handle,
            self._runtime_handle,
            target,
            replace_existing=False,
        )

    def replace(self, source: str, target: str) -> None:
        if os.name == "nt":
            identity = next(
                (
                    owned_identity
                    for owned_identity, owned_name in self._owned_file_names.items()
                    if owned_name == source
                ),
                None,
            )
            handle = self._owned_file_handles.get(identity) if identity is not None else None
            if handle is None:
                raise ValueError("runtime replacement source is not an owned staging file")
            _rename_windows_file_relative(
                handle,
                self._runtime_handle,
                target,
                replace_existing=False,
            )
            for owned_identity, owned_name in tuple(self._owned_file_names.items()):
                if owned_identity != identity and owned_name == target:
                    self._owned_file_names[owned_identity] = ""
            self._owned_file_names[identity] = target
        else:
            identity = next(
                (
                    owned_identity
                    for owned_identity, owned_name in self._owned_file_names.items()
                    if owned_name == source
                ),
                None,
            )
            if identity is None:
                raise ValueError("runtime replacement source is not an owned staging file")
            _rename_posix_no_replace(self._runtime_fd, source, target)
            self._owned_file_names[identity] = target
            os.fsync(self._runtime_fd)

    def rename_existing_no_replace(self, source: str, target: str) -> None:
        if os.name == "nt":
            raise ValueError("POSIX existing-file relocation is unavailable")
        if Path(source).name != source or Path(target).name != target:
            raise ValueError("runtime relocation name is invalid")
        _rename_posix_no_replace(self._runtime_fd, source, target)
        os.fsync(self._runtime_fd)

    def remove_file_if_identity(self, name: str, identity: tuple[int, int]) -> bool:
        if os.name == "nt" and self._owned_file_names.get(identity) == name:
            handle = self._owned_file_handles.pop(identity)
            self._owned_file_names.pop(identity, None)
            try:
                _delete_windows_file_on_close(handle)
            finally:
                _close_windows_handle(handle)
            return True
        if os.name == "nt":
            return False
        try:
            details = self._entry_details(name)
        except FileNotFoundError:
            self._owned_file_names.pop(identity, None)
            return False
        if _file_identity(details) != identity or not stat.S_ISREG(details.st_mode):
            return False
        if os.name == "nt":
            (self.runtime_root / name).unlink()
        else:
            os.unlink(name, dir_fd=self._runtime_fd)
            os.fsync(self._runtime_fd)
            self._owned_file_names.pop(identity, None)
        return True

    def release_owned_file(self, identity: tuple[int, int]) -> None:
        handle = self._owned_file_handles.pop(identity, None)
        self._owned_file_names.pop(identity, None)
        if handle is not None:
            _close_windows_handle(handle)


def _atomic_write_json_windows(
    *,
    guard: _RuntimeDirectoryGuard,
    candidate_lease: _PublicationContractLease,
    body: bytes,
) -> None:
    target_name = "runtime-attestation.json"
    staged_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
    staged_identity = guard.write_new_file(staged_name, body)
    original_body: bytes | None = None
    original_identity: tuple[int, int] | None = None
    original_handle: object | None = None
    original_recovery_name: str | None = None
    original_relocated = False
    preserve_original_recovery = False

    def rollback() -> None:
        nonlocal original_relocated, preserve_original_recovery
        current_body, current_identity = guard.read_optional_regular_file(target_name)
        if current_identity == staged_identity:
            if not guard.remove_file_if_identity(target_name, staged_identity):
                raise ValueError("runtime published target could not be removed for rollback")
            current_body, current_identity = None, None
        if original_relocated:
            if current_body is not None or current_identity is not None:
                preserve_original_recovery = True
                raise ValueError(
                    "runtime canonical rollback requires the preserved original recovery file"
                )
            if original_handle is None:
                preserve_original_recovery = True
                raise ValueError("runtime canonical rollback handle is unavailable")
            guard.rename_windows_pinned_file(original_handle, target_name)
            original_relocated = False
        restored_body, restored_identity = guard.read_optional_regular_file(target_name)
        if restored_body != original_body or restored_identity != original_identity:
            raise ValueError("runtime canonical rollback did not restore prior identity and bytes")

    try:
        original_body, original_identity, original_handle = guard.pin_windows_regular_file(
            target_name
        )
        candidate_lease.assert_unchanged()
        guard.assert_bound()
        current_body, current_identity = guard.read_optional_regular_file(target_name)
        if current_body != original_body or current_identity != original_identity:
            raise ValueError("runtime canonical changed before publication")
        candidate_lease.assert_unchanged()
        if original_handle is not None:
            original_recovery_name = f".{target_name}.{uuid.uuid4().hex}.original-recovery"
            guard.rename_windows_pinned_file(original_handle, original_recovery_name)
            original_relocated = True
        guard.replace(staged_name, target_name)
        guard.assert_bound()
        published_body, published_identity = guard.read_optional_regular_file(target_name)
        if published_body != body or published_identity != staged_identity:
            raise ValueError("runtime attestation publication identity is invalid")
        candidate_lease.assert_unchanged()
        if original_handle is not None:
            _delete_windows_file_on_close(original_handle)
            _close_windows_handle(original_handle)
            original_handle = None
            original_relocated = False
        candidate_lease.assert_unchanged()
        guard.release_owned_file(staged_identity)
    except BaseException as publication_error:
        try:
            rollback()
        except BaseException as rollback_error:
            if original_relocated:
                preserve_original_recovery = True
            recovery = (
                f" at {original_recovery_name}"
                if preserve_original_recovery and original_recovery_name is not None
                else ""
            )
            raise ValueError(
                f"runtime canonical rollback requires recovery{recovery}: {rollback_error}"
            ) from publication_error
        raise
    finally:
        guard.remove_file_if_identity(staged_name, staged_identity)
        if original_handle is not None:
            _close_windows_handle(original_handle)


def _atomic_write_json_posix(
    *,
    guard: _RuntimeDirectoryGuard,
    candidate_lease: _PublicationContractLease,
    body: bytes,
) -> None:
    target_name = "runtime-attestation.json"
    staged_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
    staged_identity: tuple[int, int] | None = None
    original_body: bytes | None = None
    original_identity: tuple[int, int] | None = None
    recovery_name: str | None = None
    recovery_body: bytes | None = None
    recovery_identity: tuple[int, int] | None = None
    preserve_recovery = False
    committed = False

    def rollback() -> None:
        nonlocal recovery_name, recovery_body, recovery_identity, preserve_recovery
        assert staged_identity is not None
        current_body, current_identity = guard.read_optional_regular_file(target_name)
        if current_identity == staged_identity:
            if not guard.remove_file_if_identity(target_name, staged_identity):
                raise ValueError("runtime published target could not be removed for rollback")
            current_body, current_identity = None, None
        elif current_body is not None or current_identity is not None:
            if current_body == original_body and current_identity == original_identity:
                return
            if recovery_name is not None:
                preserve_recovery = True
            raise ValueError("runtime canonical could not be safely rolled back")

        if original_body is None:
            return

        if recovery_name is not None:
            actual_body, actual_identity = guard.read_optional_regular_file(recovery_name)
            if actual_body is not None or actual_identity is not None:
                expected_body = recovery_body if recovery_body is not None else original_body
                expected_identity = (
                    recovery_identity if recovery_identity is not None else original_identity
                )
                if actual_body != expected_body or actual_identity != expected_identity:
                    preserve_recovery = True
                    raise ValueError("runtime canonical recovery identity changed")
                try:
                    guard.rename_existing_no_replace(recovery_name, target_name)
                except BaseException:
                    preserve_recovery = True
                    raise
                restored_body, restored_identity = guard.read_optional_regular_file(target_name)
                if restored_body != expected_body or restored_identity != expected_identity:
                    raise ValueError("runtime canonical recovery was not restored exactly")
                recovery_name = None
                recovery_body = None
                recovery_identity = None
                return

        rollback_name = f".{target_name}.{uuid.uuid4().hex}.rollback"
        rollback_identity = guard.write_new_file(rollback_name, original_body)
        try:
            guard.replace(rollback_name, target_name)
            restored_body, restored_identity = guard.read_optional_regular_file(target_name)
            if restored_body != original_body or restored_identity != rollback_identity:
                raise ValueError("runtime canonical rollback did not restore prior bytes")
            guard.release_owned_file(rollback_identity)
        except BaseException:
            preserve_recovery = True
            raise

    try:
        staged_identity = guard.write_new_file(staged_name, body)
        original_body, original_identity = guard.read_optional_regular_file(target_name)
        candidate_lease.assert_unchanged()
        guard.assert_bound()
        current_body, current_identity = guard.read_optional_regular_file(target_name)
        if current_body != original_body or current_identity != original_identity:
            raise ValueError("runtime canonical changed before publication")
        candidate_lease.assert_unchanged()

        if original_body is not None:
            recovery_name = f".{target_name}.{uuid.uuid4().hex}.rollback"
            guard.rename_existing_no_replace(target_name, recovery_name)
            recovery_body, recovery_identity = guard.read_optional_regular_file(recovery_name)
            if recovery_body != original_body or recovery_identity != original_identity:
                raise ValueError("runtime canonical changed while it was preserved")

        candidate_lease.assert_unchanged()
        guard.replace(staged_name, target_name)
        guard.assert_bound()
        published_body, published_identity = guard.read_optional_regular_file(target_name)
        if published_body != body or published_identity != staged_identity:
            raise ValueError("runtime attestation publication identity is invalid")
        candidate_lease.assert_unchanged()

        # Retain these anchored no-op/removal boundaries so candidate validation
        # also covers every observable commit-cleanup step.
        guard.remove_file_if_identity(staged_name, staged_identity)
        candidate_lease.assert_unchanged()
        if recovery_name is not None and recovery_identity is not None:
            if not guard.remove_file_if_identity(recovery_name, recovery_identity):
                preserve_recovery = True
                raise ValueError("runtime canonical recovery cleanup requires recovery")
            recovery_name = None
            recovery_body = None
            recovery_identity = None
        candidate_lease.assert_unchanged()
        guard.release_owned_file(staged_identity)
        committed = True
    except BaseException as publication_error:
        if staged_identity is not None:
            try:
                rollback()
            except BaseException as rollback_error:
                recovery = f" at {recovery_name}" if recovery_name is not None else ""
                raise ValueError(
                    f"runtime canonical rollback requires recovery{recovery}: {rollback_error}"
                ) from publication_error
        raise
    finally:
        if staged_identity is not None and not committed:
            guard.remove_file_if_identity(staged_name, staged_identity)
        if not preserve_recovery and recovery_name is not None and recovery_identity is not None:
            guard.remove_file_if_identity(recovery_name, recovery_identity)


def _atomic_write_json(
    *,
    guard: _RuntimeDirectoryGuard,
    candidate_lease: _PublicationContractLease,
    document: Mapping[str, object],
) -> None:
    body = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if os.name == "nt":
        _atomic_write_json_windows(
            guard=guard,
            candidate_lease=candidate_lease,
            body=body,
        )
        return
    _atomic_write_json_posix(
        guard=guard,
        candidate_lease=candidate_lease,
        body=body,
    )


def _collect_runtime_attestation(
    *,
    root: Path,
    deployment_root: Path,
    candidate: dict[str, object],
    expected_services: dict[str, dict[str, object]],
    bound_run: dict[str, str],
    observed_at: str,
    bound_base_url: str,
    docker: Path,
    child_environment: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
    guard: _RuntimeDirectoryGuard,
    docker_config: Path,
    candidate_lease: _CandidateContractLease,
    deployment_lease: _DeploymentContractLease,
    docker_host_identity_sha256: str | None,
    deadline_monotonic: float,
) -> dict[str, object]:
    docker_config_name = docker_config.name
    command_records: list[dict[str, object]] = []

    def invoke(
        arguments: Sequence[str],
        *,
        logical_arguments: Sequence[str] | None = None,
        stdout_transform: Callable[[bytes], bytes] | None = None,
    ) -> bytes:
        candidate_lease.assert_unchanged()
        deployment_lease.assert_unchanged()
        guard.assert_bound()
        guard.assert_owned_directory_empty(docker_config_name)
        argv = [
            str(docker),
            "--config",
            str(docker_config),
            "--context",
            "default",
            *arguments,
        ]
        remaining = deadline_monotonic - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ValueError("runtime attestation Docker deadline expired")
        command_timeout = max(1, min(SINGLE_COMMAND_TIMEOUT_SECONDS, math.ceil(remaining)))
        completed = runner(
            argv,
            cwd=deployment_root,
            env=child_environment,
            timeout=command_timeout,
            check=False,
            capture_output=True,
        )
        candidate_lease.assert_unchanged()
        deployment_lease.assert_unchanged()
        native_exit = completed.returncode
        stdout = completed.stdout
        if (
            not isinstance(native_exit, int)
            or isinstance(native_exit, bool)
            or not isinstance(stdout, bytes)
        ):
            raise ValueError("Docker native result is invalid")
        if native_exit != 0:
            raise ValueError("Docker native command failed")
        recorded_stdout = stdout_transform(stdout) if stdout_transform is not None else stdout
        if not isinstance(recorded_stdout, bytes):
            raise ValueError("Docker stdout sanitizer result is invalid")
        try:
            safe_stdout = recorded_stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Docker stdout is not valid UTF-8") from exc
        command_records.append(
            {
                "argv": [
                    *DOCKER_LOGICAL_PREFIX,
                    *(logical_arguments if logical_arguments is not None else arguments),
                ],
                "nativeExit": native_exit,
                "stdout": safe_stdout,
                "stdoutSha256": hashlib.sha256(recorded_stdout).hexdigest(),
            }
        )
        return stdout

    def inspect_docker_host() -> dict[str, str]:
        try:
            endpoint = json.loads(invoke(DOCKER_CONTEXT_ARGUMENTS))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Docker daemon endpoint is invalid") from exc
        daemon = _json_object(invoke(DOCKER_INFO_ARGUMENTS), label="info")
        if (
            not isinstance(endpoint, str)
            or not endpoint
            or endpoint != endpoint.strip()
            or set(daemon) != {"serverId", "osType"}
            or not isinstance(daemon.get("serverId"), str)
            or not daemon["serverId"]
            or daemon["serverId"] != daemon["serverId"].strip()
            or not isinstance(daemon.get("osType"), str)
            or not daemon["osType"]
            or daemon["osType"] != daemon["osType"].strip()
        ):
            raise ValueError("Docker daemon identity is invalid")
        return {
            "context": DOCKER_CONTEXT,
            "endpoint": endpoint,
            "serverId": daemon["serverId"],
            "osType": daemon["osType"],
        }

    def inspect_round() -> list[dict[str, object]]:
        ids = _ps_ids(invoke(PS_ARGUMENTS))
        facts: list[dict[str, object]] = []
        for container_id in ids:
            raw_inspect = invoke(
                (
                    "container",
                    "inspect",
                    "--format",
                    CONTAINER_INSPECT_FORMAT,
                    container_id,
                ),
                stdout_transform=lambda body: _redacted_inspect_output(
                    body, label="container inspect"
                ),
            )
            fact = _container_fact(
                _json_object(
                    _redacted_inspect_output(raw_inspect, label="container inspect"),
                    label="container inspect",
                )
            )
            if fact["containerId"] != container_id:
                raise ValueError("Docker container inspect identity does not match Docker ps")
            facts.append(fact)
        return facts

    candidate_expected_services = expected_services
    compose_config_arguments, compose_config_logical = _runtime_compose_arguments(
        deployment_root=deployment_root,
        candidate_root=root,
        tail=_COMPOSE_CONFIG_TAIL,
    )
    raw_compose_config = invoke(
        compose_config_arguments,
        logical_arguments=compose_config_logical,
        stdout_transform=_compose_security_projection,
    )
    compose_security = _json_object(
        _compose_security_projection(raw_compose_config), label="Compose security projection"
    )
    expected_services = _merged_expected_services(compose_security, candidate_expected_services)
    compose_hash_arguments, compose_hash_logical = _runtime_compose_arguments(
        deployment_root=deployment_root,
        candidate_root=root,
        tail=_COMPOSE_HASH_TAIL,
    )
    compose_hashes = _compose_hashes(
        invoke(compose_hash_arguments, logical_arguments=compose_hash_logical)
    )
    if not set(expected_services).issubset(compose_hashes):
        raise ValueError("Docker Compose config hashes do not cover candidate services")
    before_host = inspect_docker_host() if docker_host_identity_sha256 is not None else None
    before_facts = inspect_round()
    image_facts: dict[str, dict[str, object]] = {}
    references = {service["image"] for service in expected_services.values()}
    if not all(isinstance(reference, str) for reference in references):
        raise ValueError("candidate runtime image references are invalid")
    for reference in sorted(str(reference) for reference in references):
        raw_image = invoke(
            ("image", "inspect", "--format", IMAGE_INSPECT_FORMAT, reference),
            stdout_transform=lambda body: _redacted_inspect_output(body, label="image inspect"),
        )
        image = _json_object(
            _redacted_inspect_output(raw_image, label="image inspect"),
            label="image inspect",
        )
        if set(image) != {
            "imageId",
            "repoDigests",
            "command",
            "entrypoint",
            "user",
            "environmentHashes",
            "volumes",
        }:
            raise ValueError("Docker image inspect output is invalid")
        image_id = image.get("imageId")
        repo_digests = image.get("repoDigests")
        image_command = _string_list(image.get("command"), label="image command")
        image_entrypoint = _string_list(image.get("entrypoint"), label="image entrypoint")
        image_user = image.get("user")
        image_environment = image.get("environmentHashes")
        image_volumes = image.get("volumes")
        if (
            not isinstance(image_id, str)
            or not image_id
            or not isinstance(repo_digests, list)
            or not all(isinstance(value, str) for value in repo_digests)
            or not isinstance(image_user, str)
            or not isinstance(image_environment, dict)
            or not all(
                isinstance(name, str)
                and name
                and isinstance(value, str)
                and _SHA256.fullmatch(value) is not None
                for name, value in image_environment.items()
            )
            or (image_volumes is not None and not isinstance(image_volumes, dict))
        ):
            raise ValueError("Docker image inspect output is invalid")
        image_facts[reference] = {
            "id": image_id,
            "repoDigests": repo_digests,
            "command": image_command,
            "entrypoint": image_entrypoint,
            "user": image_user,
            "environmentHashes": dict(sorted(image_environment.items())),
            "volumes": image_volumes,
            "reference": reference,
        }
    after_facts = inspect_round()
    after_host = inspect_docker_host() if docker_host_identity_sha256 is not None else None
    candidate_lease.assert_unchanged()
    deployment_lease.assert_unchanged()
    final_candidate, final_expected_services = _load_candidate_token(candidate_lease.token)
    if final_candidate != candidate or final_expected_services != candidate_expected_services:
        raise ValueError("candidate runtime contract changed during final validation")
    before_snapshot = _snapshot(before_facts)
    after_snapshot = _snapshot(after_facts)
    if before_snapshot != after_snapshot:
        raise ValueError("runtime container snapshot changed during attestation")
    if before_host != after_host:
        raise ValueError("Docker host identity changed during runtime attestation")
    _validate_container_facts(
        before_facts,
        expected_services=expected_services,
        image_facts=image_facts,
        compose_hashes=compose_hashes,
        compose_security=compose_security,
    )
    _validate_container_facts(
        after_facts,
        expected_services=expected_services,
        image_facts=image_facts,
        compose_hashes=compose_hashes,
        compose_security=compose_security,
    )
    containers = []
    for fact in sorted(after_facts, key=lambda item: str(item["service"])):
        image = image_facts.get(str(fact["configImage"]), {})
        containers.append(
            {
                **fact,
                "imageId": image.get("id"),
                "repoDigests": image.get("repoDigests", []),
            }
        )
    report: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "candidate": candidate,
        "releaseRun": bound_run,
        "observedAt": observed_at,
        "baseUrl": bound_base_url,
        "project": PROJECT,
        "beforeSnapshot": before_snapshot,
        "afterSnapshot": after_snapshot,
        "containers": containers,
        "commands": command_records,
    }
    if before_host is not None and docker_host_identity_sha256 is not None:
        report["dockerHostIdentity"] = {
            "context": before_host["context"],
            "endpoint": before_host["endpoint"],
            "serverId": before_host["serverId"],
            "dockerHostIdentitySha256": docker_host_identity_sha256,
        }
    candidate_lease.assert_unchanged()
    publish_candidate, publish_expected_services = _load_candidate_token(candidate_lease.token)
    if publish_candidate != candidate or publish_expected_services != candidate_expected_services:
        raise ValueError("candidate runtime contract changed before publication")
    return report


def produce_runtime_attestation(
    *,
    candidate_root: Path,
    deployment_root: Path | None = None,
    bundle_root: Path,
    release_run: Mapping[str, object],
    observed_at: str,
    base_url: str,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = _run_docker,
    docker_resolver: Callable[[], Path] = resolve_fixed_docker,
    environ: Mapping[str, str] | None = None,
    docker_host_identity_sha256: str | None = None,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
) -> dict[str, object]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("runtime attestation timeout is invalid")
    deadline_monotonic = time.monotonic() + float(timeout_seconds)
    source_environment = os.environ if environ is None else environ
    expected_docker_host_identity_sha256 = _expected_docker_host_identity_sha256(
        docker_host_identity_sha256,
        source_environment,
    )
    root = Path(os.path.abspath(candidate_root))
    deployment = Path(os.path.abspath(deployment_root or candidate_root))
    with (
        _CandidateContractLease.open(root) as candidate_lease,
        _DeploymentContractLease.open(deployment) as deployment_lease,
    ):
        candidate, expected_services = _load_candidate_token(candidate_lease.token)
        candidate_lease.assert_unchanged()
        bound_run = _release_run(release_run)
        if not _valid_observed_at(observed_at):
            raise ValueError("runtime observedAt is invalid")
        bound_base_url = _base_url(base_url)
        docker = Path(docker_resolver())
        child_environment = _child_environment(source_environment)
        safe_bundle_root, runtime_root = _prepare_runtime_root(bundle_root)
        with _RuntimeDirectoryGuard.open(safe_bundle_root, runtime_root) as guard:
            docker_config = guard.create_empty_directory(f".docker-config-{uuid.uuid4().hex}")
            try:
                report = _collect_runtime_attestation(
                    root=root,
                    deployment_root=deployment,
                    candidate=candidate,
                    expected_services=expected_services,
                    bound_run=bound_run,
                    observed_at=observed_at,
                    bound_base_url=bound_base_url,
                    docker=docker,
                    child_environment=child_environment,
                    runner=runner,
                    guard=guard,
                    docker_config=docker_config,
                    candidate_lease=candidate_lease,
                    deployment_lease=deployment_lease,
                    docker_host_identity_sha256=expected_docker_host_identity_sha256,
                    deadline_monotonic=deadline_monotonic,
                )
            except BaseException as error:
                try:
                    guard.assert_owned_directory_empty(docker_config.name)
                    guard.remove_owned_empty_directory(docker_config.name)
                except BaseException as cleanup_error:
                    if hasattr(error, "add_note"):
                        error.add_note(f"isolated Docker config recovery required: {cleanup_error}")
                raise
            try:
                guard.assert_owned_directory_empty(docker_config.name)
                guard.remove_owned_empty_directory(docker_config.name)
            except BaseException as exc:
                raise ValueError("isolated Docker config cleanup requires recovery") from exc
            _atomic_write_json(
                guard=guard,
                candidate_lease=_PublicationContractLease(candidate_lease, deployment_lease),
                document=report,
            )
            return report


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument(
        "--deployment-root",
        type=Path,
        default=SCRIPTS_ROOT.parent,
        help="deployed source root holding docker-compose.yml and docker.env",
    )
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--docker-host-identity-sha256")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    produce_runtime_attestation(
        candidate_root=args.candidate_root,
        deployment_root=args.deployment_root,
        bundle_root=args.bundle_root,
        release_run={"runId": args.run_id, "environmentId": args.environment_id},
        observed_at=args.observed_at,
        base_url=args.base_url,
        docker_host_identity_sha256=args.docker_host_identity_sha256,
    )
    print(Path(args.bundle_root).resolve() / "runtime" / "runtime-attestation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
