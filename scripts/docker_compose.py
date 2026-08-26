#!/usr/bin/env python
"""Run Docker Compose with port mappings rendered from JSON settings.

Docker Compose cannot read ``data/user/settings/system.json`` directly for
host port interpolation. This wrapper renders a tiny compose env file from the
JSON settings and then invokes ``docker compose --env-file``. It intentionally
does not read or migrate the project-root ``.env`` file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_DIR = PROJECT_ROOT / "data" / "user" / "settings"
DOCKER_ENV_PATH = SETTINGS_DIR / "docker.env"

DEFAULT_BACKEND_PORT = 8001
DEFAULT_FRONTEND_PORT = 3782
DEFAULT_POCKETBASE_PORT = 8090
DATA_PLANE_ID_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PRODUCTION_TOPOLOGY_OPTIONS = (
    "--env-file",
    "--file",
    "--project-directory",
    "--project-name",
)


def _load_platform_renderer() -> Any:
    module_path = Path(__file__).resolve().with_name("render_platform_compose.py")
    spec = importlib.util.spec_from_file_location(
        "_yfeistai_render_platform_compose",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("platform image-lock validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _candidate_paths(candidate_root: Path) -> Any:
    renderer = _load_platform_renderer()
    return renderer.candidate_artifact_paths(candidate_root)


def _validate_production_image_lock(candidate_root: Path) -> None:
    renderer = _load_platform_renderer()
    paths = renderer.candidate_artifact_paths(candidate_root)
    renderer.validate_image_lock_bindings(
        paths.image_lock,
        require_candidate=True,
        compose_paths=(
            paths.platform_compose,
            paths.data_plane_compose,
        ),
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _coerce_port(value: Any, default: int) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _read_platform_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("platform compose setting file is invalid") from exc
    if not isinstance(loaded, dict):
        raise ValueError("platform compose setting file must contain an object")
    return loaded


def _safe_compose_string(value: Any, *, name: str, default: str) -> str:
    candidate = default if value is None else value
    if not isinstance(candidate, str):
        raise ValueError(f"platform compose setting {name} must be a string")
    candidate = candidate.strip()
    if (
        not candidate
        or len(candidate) > 2048
        or "\x00" in candidate
        or "\r" in candidate
        or "\n" in candidate
    ):
        raise ValueError(f"platform compose setting {name} is invalid")
    return candidate


def _safe_compose_integer(
    value: Any,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> str:
    candidate = default if value is None else value
    if isinstance(candidate, bool):
        raise ValueError(f"platform compose setting {name} must be an integer")
    try:
        number = int(str(candidate).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"platform compose setting {name} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"platform compose setting {name} is out of range")
    return str(number)


def _safe_object_store_endpoint(value: Any) -> str:
    endpoint = _safe_compose_string(
        value,
        name="object_store_endpoint",
        default="http://minio:9000",
    )
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("platform compose setting object_store_endpoint is invalid")
    return endpoint.rstrip("/")


def _validate_data_plane_id(value: str) -> str:
    if not DATA_PLANE_ID_PATTERN.fullmatch(value):
        raise SystemExit("data-plane id must be a lowercase platform identifier")
    return value


def render_docker_env(
    settings_dir: Path = SETTINGS_DIR,
    output_path: Path = DOCKER_ENV_PATH,
    *,
    data_plane: str | None = None,
) -> dict[str, str]:
    """Render compose interpolation vars from JSON settings only."""
    system = _read_json_object(settings_dir / "system.json")
    integrations = _read_json_object(settings_dir / "integrations.json")
    values = {
        "DEEPTUTOR_DOCKER_BACKEND_PORT": str(
            _coerce_port(system.get("backend_port"), DEFAULT_BACKEND_PORT)
        ),
        "DEEPTUTOR_DOCKER_FRONTEND_PORT": str(
            _coerce_port(system.get("frontend_port"), DEFAULT_FRONTEND_PORT)
        ),
        "DEEPTUTOR_DOCKER_POCKETBASE_PORT": str(
            _coerce_port(integrations.get("pocketbase_port"), DEFAULT_POCKETBASE_PORT)
        ),
    }
    platform = _read_platform_object(settings_dir / "platform.json")
    if platform is not None:
        values.update(
            {
                "YFEISTAI_PLATFORM_DATABASE_HOST": _safe_compose_string(
                    platform.get("database_host"),
                    name="database_host",
                    default="postgres",
                ),
                "YFEISTAI_PLATFORM_DATABASE_PORT": _safe_compose_integer(
                    platform.get("database_port"),
                    name="database_port",
                    default=5432,
                    minimum=1,
                    maximum=65535,
                ),
                "YFEISTAI_PLATFORM_DATABASE_NAME": _safe_compose_string(
                    platform.get("database_name"),
                    name="database_name",
                    default="yfeistai",
                ),
                "YFEISTAI_PLATFORM_DATABASE_USER": _safe_compose_string(
                    platform.get("database_user"),
                    name="database_user",
                    default="yfeistai_app",
                ),
                "YFEISTAI_OBJECT_STORE_ENDPOINT": _safe_object_store_endpoint(
                    platform.get("object_store_endpoint")
                ),
                "YFEISTAI_OBJECT_STORE_BUCKET": _safe_compose_string(
                    platform.get("object_store_bucket"),
                    name="object_store_bucket",
                    default="yfeistai-classrooms",
                ),
                "YFEISTAI_OBJECT_STORE_REGION": _safe_compose_string(
                    platform.get("object_store_region"),
                    name="object_store_region",
                    default="us-east-1",
                ),
                "YFEISTAI_SHARED_GENERATION_LIMIT": _safe_compose_integer(
                    platform.get("shared_generation_limit"),
                    name="shared_generation_limit",
                    default=20,
                    minimum=1,
                    maximum=10000,
                ),
                "YFEISTAI_DEFAULT_TENANT_GENERATION_LIMIT": _safe_compose_integer(
                    platform.get("default_tenant_generation_limit"),
                    name="default_tenant_generation_limit",
                    default=2,
                    minimum=1,
                    maximum=1000,
                ),
            }
        )
    if data_plane is not None:
        tenant_id = _validate_data_plane_id(data_plane)
        tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
        values.update(
            {
                "YFEISTAI_DATA_PLANE_ID": tenant_id,
                "YFEISTAI_DATA_PLANE_SECRET_DIR": (
                    f"./data/system/secrets/data-planes/tenant_{tenant_hash}"
                ),
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Auto-generated by scripts/docker_compose.py from data/user/settings/*.json.",
        "# Contains non-sensitive Compose interpolation only; secret values stay in files.",
    ]
    lines.extend(f"{key}={value}" for key, value in values.items())
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return values


def _parse_topology_args(
    args: list[str],
) -> tuple[bool, str | None, Path | None, list[str]]:
    platform = False
    data_plane: str | None = None
    candidate_root: Path | None = None
    compose_args: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--platform":
            if platform:
                raise SystemExit("--platform may only be provided once")
            platform = True
            index += 1
            continue
        if argument == "--data-plane":
            if data_plane is not None or index + 1 >= len(args):
                raise SystemExit("--data-plane requires exactly one identifier")
            data_plane = _validate_data_plane_id(args[index + 1])
            index += 2
            continue
        if argument == "--candidate-root":
            if candidate_root is not None or index + 1 >= len(args):
                raise SystemExit("--candidate-root requires exactly one path")
            candidate_root = Path(args[index + 1])
            if not candidate_root.is_absolute():
                raise SystemExit("--candidate-root must be an absolute path")
            candidate_root = candidate_root.resolve()
            index += 2
            continue
        compose_args.append(argument)
        index += 1
    if platform and data_plane is not None:
        raise SystemExit("--platform and --data-plane are mutually exclusive")
    if candidate_root is not None and not (platform or data_plane is not None):
        raise SystemExit("--candidate-root requires --platform or --data-plane")
    if platform or data_plane is not None:
        _validate_production_compose_args(
            platform=platform,
            data_plane=data_plane,
            compose_args=compose_args,
        )
    return platform, data_plane, candidate_root, compose_args


def _validate_production_compose_args(
    *,
    platform: bool,
    data_plane: str | None,
    compose_args: list[str],
) -> None:
    index = 0
    while index < len(compose_args):
        argument = compose_args[index]
        if (
            argument == "-f"
            or argument.startswith("-f=")
            or (argument.startswith("-f") and len(argument) > 2)
            or argument == "-p"
            or argument.startswith("-p=")
            or (argument.startswith("-p") and len(argument) > 2)
            or any(
                argument == option or argument.startswith(f"{option}=")
                for option in _PRODUCTION_TOPOLOGY_OPTIONS
            )
        ):
            raise SystemExit("production Compose topology overrides are not allowed")
        if argument == "--profile":
            if index + 1 >= len(compose_args):
                raise SystemExit("--profile requires a value")
            profile = compose_args[index + 1]
            index += 2
        elif argument.startswith("--profile="):
            profile = argument.partition("=")[2]
            index += 1
        else:
            index += 1
            continue
        if platform or data_plane is None or profile != "mp4-export":
            raise SystemExit("the requested Compose profile is not allowed")


def _compose_command(args: list[str]) -> list[str]:
    docker = shutil.which("docker")
    if not docker:
        raise SystemExit("docker was not found on PATH")
    platform, data_plane, candidate_root, compose_args = _parse_topology_args(args)
    command = [docker, "compose", "--env-file", str(DOCKER_ENV_PATH)]
    if platform:
        artifact_paths = _candidate_paths(candidate_root or PROJECT_ROOT)
        command.extend(
            (
                "--project-name",
                "yfeistai-platform",
                "-f",
                str(PROJECT_ROOT / "docker-compose.yml"),
                "-f",
                str(artifact_paths.platform_compose),
            )
        )
    elif data_plane is not None:
        artifact_paths = _candidate_paths(candidate_root or PROJECT_ROOT)
        command.extend(
            (
                "--project-directory",
                str(PROJECT_ROOT),
                "--project-name",
                f"yfeistai-{data_plane}",
                "-f",
                str(artifact_paths.data_plane_compose),
            )
        )
    command.extend(compose_args)
    return command


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        args = ["up", "-d"]

    platform, data_plane, candidate_root, _ = _parse_topology_args(args)
    if platform or data_plane is not None:
        _validate_production_image_lock(candidate_root or PROJECT_ROOT)
    values = render_docker_env(data_plane=data_plane)
    print(
        "Docker settings: "
        f"backend={values['DEEPTUTOR_DOCKER_BACKEND_PORT']} "
        f"frontend={values['DEEPTUTOR_DOCKER_FRONTEND_PORT']} "
        f"pocketbase={values['DEEPTUTOR_DOCKER_POCKETBASE_PORT']}",
        file=sys.stderr,
    )

    env = os.environ.copy()
    # Keep Docker execution detached from host process overrides.
    for key in (
        "BACKEND_PORT",
        "COMPOSE_FILE",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "FRONTEND_PORT",
        "POCKETBASE_PORT",
        "AUTH_ENABLED",
        "POCKETBASE_URL",
        "NEXT_PUBLIC_API_BASE",
        "NEXT_PUBLIC_API_BASE_EXTERNAL",
    ):
        env.pop(key, None)

    result = subprocess.run(_compose_command(args), cwd=str(PROJECT_ROOT), env=env, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
