from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import yaml

from deeptutor.services.config import PlatformSettings


def _load_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "docker_compose.py"
    spec = importlib.util.spec_from_file_location("docker_compose_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def test_render_docker_env_reads_json_only(tmp_path: Path) -> None:
    module = _load_module()
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "system.json").write_text(
        json.dumps({"backend_port": 9001, "frontend_port": 4000}),
        encoding="utf-8",
    )
    (settings_dir / "integrations.json").write_text(
        json.dumps({"pocketbase_port": 19090}),
        encoding="utf-8",
    )
    output_path = tmp_path / "docker.env"

    values = module.render_docker_env(settings_dir, output_path)

    assert values == {
        "DEEPTUTOR_DOCKER_BACKEND_PORT": "9001",
        "DEEPTUTOR_DOCKER_FRONTEND_PORT": "4000",
        "DEEPTUTOR_DOCKER_POCKETBASE_PORT": "19090",
    }
    saved = output_path.read_text(encoding="utf-8")
    assert "\nBACKEND_PORT=" not in saved
    assert "DEEPTUTOR_DOCKER_BACKEND_PORT=9001" in saved


def test_render_docker_env_uses_defaults_for_missing_or_invalid_json(tmp_path: Path) -> None:
    module = _load_module()
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "system.json").write_text(
        json.dumps({"backend_port": "bad", "frontend_port": 70000}),
        encoding="utf-8",
    )
    output_path = tmp_path / "docker.env"

    values = module.render_docker_env(settings_dir, output_path)

    assert values["DEEPTUTOR_DOCKER_BACKEND_PORT"] == "8001"
    assert values["DEEPTUTOR_DOCKER_FRONTEND_PORT"] == "3782"
    assert values["DEEPTUTOR_DOCKER_POCKETBASE_PORT"] == "8090"


def test_compose_files_do_not_consume_legacy_env_names() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("docker-compose.yml", "docker-compose.ghcr.yml"):
        content = (root / name).read_text(encoding="utf-8")
        assert "${BACKEND_PORT" not in content
        assert "${FRONTEND_PORT" not in content
        assert "\n      - BACKEND_PORT" not in content
        assert "\n      - AUTH_ENABLED" not in content
        assert "DEEPTUTOR_DOCKER_BACKEND_PORT" in content


def test_dockerfile_is_json_driven_without_bundle_sed() -> None:
    """The image no longer rewrites the built bundle at startup (the runtime
    ``sed -i`` broke under a read-only rootfs). URL/auth knowledge is JSON-driven:
    the entrypoint re-exports runtime settings from data/user/settings/*.json
    (including DEEPTUTOR_API_BASE_URL / DEEPTUTOR_AUTH_ENABLED) and web/proxy.ts
    forwards /api/* and /ws/* to the backend at request time."""
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    # The build-time placeholder + runtime bundle sed mechanism is gone.
    assert "__NEXT_PUBLIC_API_BASE_PLACEHOLDER__" not in content
    assert "__NEXT_PUBLIC_AUTH_ENABLED_PLACEHOLDER__" not in content
    # Still JSON-driven: stale runtime env names are ignored and re-exported
    # from the settings JSON on every start.
    assert "DEEPTUTOR_IGNORE_PROCESS_ENV_OVERRIDES=1" in content
    assert 'unset "$key"' in content
    assert "export_runtime_settings_to_env" in content


def test_supervisord_runs_as_root_with_unprivileged_children() -> None:
    """supervisord itself must run as root so it can open the container's
    stdout/stderr (``/dev/fd/1,2`` — root-owned pipes under a rootful daemon
    such as Docker Desktop) and write its pidfile under ``/var/run``. Dropping
    supervisord to the unprivileged ``deeptutor`` user via ``gosu`` made child
    spawning fail with ``EACCES`` ("making dispatchers ... EACCES"), so neither
    the backend nor the frontend started under rootful Docker (it only worked
    under rootless podman). The app processes stay non-root via the per-program
    ``user=deeptutor`` directive instead, which keeps them unprivileged in both
    runtimes. This guards against reintroducing the ``gosu`` privilege drop.
    """
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    # supervisord is launched directly (as root), not behind a gosu priv-drop.
    assert "exec /usr/bin/supervisord" in content
    assert "gosu deeptutor /usr/bin/supervisord" not in content
    # Every supervisord program drops to the unprivileged deeptutor user, so the
    # backend/frontend processes never run as root. Each config heredoc closes
    # with ``EOF``; slice to it so a program's section is bounded correctly.
    program_blocks = content.split("[program:")[1:]
    assert program_blocks, "expected supervisord [program:*] sections in the Dockerfile"
    for block in program_blocks:
        name = block.splitlines()[0].rstrip("]")
        section = block.split("EOF")[0]
        assert "user=deeptutor" in section, (
            f"supervisord program '{name}' must run as deeptutor (user=deeptutor)"
        )


def test_frontend_api_is_url_agnostic_passthrough() -> None:
    """web/lib/api.ts no longer carries a build-time API base or a placeholder
    token; apiUrl/wsUrl are pass-throughs and the Next.js middleware
    (web/proxy.ts) performs the forwarding at request time."""
    root = Path(__file__).resolve().parents[2]
    api_ts = (root / "web" / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "NEXT_PUBLIC_API_BASE_PLACEHOLDER" not in api_ts
    assert "process.env.NEXT_PUBLIC_API_BASE" not in api_ts
    proxy_ts = (root / "web" / "proxy.ts").read_text(encoding="utf-8")
    assert "DEEPTUTOR_API_BASE_URL" in proxy_ts
    assert "NextResponse.rewrite" in proxy_ts


def test_platform_container_contract_keeps_storage_credentials_tenant_scoped() -> None:
    root = Path(__file__).resolve().parents[2]
    forbidden_environment = {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
    }
    infrastructure_names = ("postgres", "postgresql", "minio", "s3")

    for name in ("docker-compose.yml", "docker-compose.ghcr.yml"):
        document = yaml.safe_load((root / name).read_text(encoding="utf-8"))
        service = document["services"]["deeptutor"]
        environment = service.get("environment", {})
        if isinstance(environment, dict):
            environment_keys = set(environment)
        else:
            environment_keys = {str(entry).partition("=")[0] for entry in environment}
        assert environment_keys.isdisjoint(forbidden_environment)

        depends_on = service.get("depends_on", {})
        dependency_names = set(depends_on) if isinstance(depends_on, dict) else set(depends_on)
        assert not any(
            infrastructure in dependency.lower()
            for dependency in dependency_names
            for infrastructure in infrastructure_names
        )

    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert not any(name in dockerfile for name in forbidden_environment)

    disabled_platform = PlatformSettings(enabled=False, object_store_mode="s3")
    assert disabled_platform.database_url is None
    assert {
        "object_store_access_key",
        "object_store_secret_key",
        "object_store_session_token",
    }.isdisjoint(PlatformSettings.model_fields)


def test_production_image_installs_supported_migration_entrypoint() -> None:
    root = Path(__file__).resolve().parents[2]
    wrapper = root / "scripts" / "deeptutor-migrate"
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    attributes = (root / ".gitattributes").read_text(encoding="utf-8")

    assert wrapper.is_file()
    wrapper_content = wrapper.read_text(encoding="utf-8")
    assert "cd /app" in wrapper_content
    assert "python -m deeptutor.teaching.migrations.cli" in wrapper_content
    assert "scripts/deeptutor-migrate text eol=lf" in attributes
    assert "COPY scripts/deeptutor-migrate /usr/local/bin/deeptutor-migrate" in dockerfile
    assert "deeptutor-migrate --help" in dockerfile
