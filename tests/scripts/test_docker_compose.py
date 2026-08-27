from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest
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


def test_render_docker_env_adds_only_non_sensitive_platform_values(
    tmp_path: Path,
) -> None:
    module = _load_module()
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    sensitive_database_url = "postgresql+asyncpg://platform:do-not-render@postgres/yfeistai"
    (settings_dir / "platform.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "database_url": sensitive_database_url,
                "database_host": "postgres",
                "database_port": 5432,
                "database_name": "yfeistai",
                "database_user": "yfeistai_app",
                "database_password_file": "/run/secrets/platform_database_password",
                "object_store_mode": "s3",
                "object_store_endpoint": "http://minio:9000",
                "object_store_namespace_id": "test-minio-primary",
                "object_store_bucket": "yfeistai-classrooms",
                "object_store_region": "us-east-1",
                "shared_generation_limit": 20,
                "default_tenant_generation_limit": 2,
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "docker.env"

    values = module.render_docker_env(settings_dir, output_path)

    assert values["YFEISTAI_PLATFORM_DATABASE_HOST"] == "postgres"
    assert values["YFEISTAI_PLATFORM_DATABASE_NAME"] == "yfeistai"
    assert values["YFEISTAI_PLATFORM_DATABASE_USER"] == "yfeistai_app"
    assert values["YFEISTAI_OBJECT_STORE_ENDPOINT"] == "http://minio:9000"
    assert values["YFEISTAI_SHARED_GENERATION_LIMIT"] == "20"
    saved = output_path.read_text(encoding="utf-8")
    assert sensitive_database_url not in saved
    assert "do-not-render" not in saved
    assert "database_password_file" not in saved


def test_render_docker_env_rejects_platform_newline_injection(tmp_path: Path) -> None:
    module = _load_module()
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "platform.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "database_host": "postgres\nEXPOSE_SECRET=value",
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "docker.env"

    with pytest.raises(ValueError, match="platform compose setting"):
        module.render_docker_env(settings_dir, output_path)
    assert not output_path.exists()


def test_render_docker_env_rejects_credentials_inside_endpoint(tmp_path: Path) -> None:
    module = _load_module()
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "platform.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "object_store_endpoint": "http://access:do-not-render@minio:9000",
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "docker.env"

    with pytest.raises(ValueError, match="object_store_endpoint"):
        module.render_docker_env(settings_dir, output_path)
    assert not output_path.exists()


def test_compose_command_selects_platform_or_isolated_data_plane(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module.shutil, "which", lambda executable: "docker.exe")

    platform = module._compose_command(["--platform", "up", "-d"])
    assert platform[-6:] == [
        "-f",
        str(module.PROJECT_ROOT / "docker-compose.yml"),
        "-f",
        str(module.PROJECT_ROOT / "docker-compose.platform.yml"),
        "up",
        "-d",
    ]

    data_plane = module._compose_command(["--data-plane", "tenant-acme", "up", "-d"])
    assert data_plane[-6:] == [
        "--project-name",
        "yfeistai-tenant-acme",
        "-f",
        str(module.PROJECT_ROOT / "docker-compose.data-plane.yml"),
        "up",
        "-d",
    ]

    data_plane_with_render = module._compose_command(
        [
            "--data-plane",
            "tenant-acme",
            "--profile",
            "mp4-export",
            "up",
            "-d",
        ]
    )
    assert data_plane_with_render[-4:] == [
        "--profile",
        "mp4-export",
        "up",
        "-d",
    ]


def test_platform_compose_command_pins_attested_project_name(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module.shutil, "which", lambda executable: "docker.exe")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "attacker-project")

    assert module._compose_command(["--platform", "config"]) == [
        "docker.exe",
        "compose",
        "--env-file",
        str(module.DOCKER_ENV_PATH),
        "--project-name",
        "yfeistai-platform",
        "-f",
        str(module.PROJECT_ROOT / "docker-compose.yml"),
        "-f",
        str(module.PROJECT_ROOT / "docker-compose.platform.yml"),
        "config",
    ]


def test_default_compose_command_does_not_load_candidate_artifacts(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module.shutil, "which", lambda executable: "docker.exe")
    monkeypatch.setattr(
        module,
        "_candidate_paths",
        lambda _root: (_ for _ in ()).throw(AssertionError("candidate artifacts loaded")),
    )

    assert module._compose_command(["config"]) == [
        "docker.exe",
        "compose",
        "--env-file",
        str(module.DOCKER_ENV_PATH),
        "config",
    ]


def test_data_plane_env_uses_a_hashed_dedicated_secret_directory(tmp_path: Path) -> None:
    module = _load_module()
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    output_path = tmp_path / "docker.env"

    values = module.render_docker_env(
        settings_dir,
        output_path,
        data_plane="tenant-acme",
    )

    tenant_hash = hashlib.sha256(b"tenant-acme").hexdigest()[:16]
    assert values["YFEISTAI_DATA_PLANE_ID"] == "tenant-acme"
    assert values["YFEISTAI_DATA_PLANE_SECRET_DIR"] == (
        f"./data/system/secrets/data-planes/tenant_{tenant_hash}"
    )


def test_compose_command_rejects_ambiguous_or_unsafe_topology(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module.shutil, "which", lambda executable: "docker.exe")

    for arguments in (
        ["--platform", "--data-plane", "tenant-acme", "up"],
        ["--data-plane", "../tenant-acme", "up"],
        ["--data-plane", "Tenant_Acme", "up"],
    ):
        try:
            module._compose_command(arguments)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"unsafe compose arguments were accepted: {arguments}")


def _isolate_production_wrapper_subprocess(module, monkeypatch):
    subprocess_calls: list[tuple[list[str], dict[str, object]]] = []

    def record_subprocess(command: list[str], **kwargs) -> SimpleNamespace:
        subprocess_calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module, "_validate_production_image_lock", lambda _candidate_root: None)
    monkeypatch.setattr(
        module,
        "render_docker_env",
        lambda **_kwargs: {
            "DEEPTUTOR_DOCKER_BACKEND_PORT": "8001",
            "DEEPTUTOR_DOCKER_FRONTEND_PORT": "3782",
            "DEEPTUTOR_DOCKER_POCKETBASE_PORT": "8090",
        },
    )
    monkeypatch.setattr(module.shutil, "which", lambda executable: "docker.exe")
    monkeypatch.setattr(module.subprocess, "run", record_subprocess)
    return subprocess_calls


def test_platform_wrapper_removes_ambient_compose_project_name(monkeypatch) -> None:
    module = _load_module()
    subprocess_calls = _isolate_production_wrapper_subprocess(module, monkeypatch)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "attacker-project")

    assert module.main(["--platform", "config"]) == 0

    assert len(subprocess_calls) == 1
    _command, options = subprocess_calls[0]
    assert "COMPOSE_PROJECT_NAME" not in options["env"]


@pytest.mark.parametrize(
    "topology_arguments",
    [
        pytest.param(["--platform"], id="platform"),
        pytest.param(["--data-plane", "tenant-acme"], id="data-plane"),
    ],
)
def test_production_wrapper_rejects_user_topology_overrides_before_subprocess(
    topology_arguments: list[str],
    monkeypatch,
) -> None:
    module = _load_module()
    subprocess_calls = _isolate_production_wrapper_subprocess(module, monkeypatch)
    cases = (
        ("file-short-separated", ["-f", "compose.extra.yml", "config"]),
        ("file-short-equals", ["-f=compose.extra.yml", "config"]),
        ("file-short-attached", ["-fcompose.extra.yml", "config"]),
        ("file-long-separated", ["--file", "compose.extra.yml", "config"]),
        ("file-long-equals", ["--file=compose.extra.yml", "config"]),
        ("env-file-separated", ["--env-file", "compose.env", "config"]),
        ("env-file-equals", ["--env-file=compose.env", "config"]),
        (
            "project-directory-separated",
            ["--project-directory", "alternate-root", "config"],
        ),
        (
            "project-directory-equals",
            ["--project-directory=alternate-root", "config"],
        ),
        ("project-name-short-separated", ["-p", "alternate", "config"]),
        ("project-name-short-equals", ["-p=alternate", "config"]),
        ("project-name-short-attached", ["-palternate", "config"]),
        (
            "project-name-long-separated",
            ["--project-name", "alternate", "config"],
        ),
        ("project-name-long-equals", ["--project-name=alternate", "config"]),
    )
    accepted: list[str] = []
    reached_subprocess: list[str] = []

    for case_name, compose_arguments in cases:
        calls_before = len(subprocess_calls)
        try:
            module.main([*topology_arguments, *compose_arguments])
        except SystemExit:
            pass
        else:
            accepted.append(case_name)
        if len(subprocess_calls) != calls_before:
            reached_subprocess.append(case_name)

    assert accepted == []
    assert reached_subprocess == []


def test_platform_wrapper_rejects_all_profiles_before_subprocess(monkeypatch) -> None:
    module = _load_module()
    subprocess_calls = _isolate_production_wrapper_subprocess(module, monkeypatch)
    cases = (
        ("legacy-local-separated", ["--profile", "legacy-local", "config"]),
        ("local-sandbox-equals", ["--profile=local-sandbox", "config"]),
        ("mp4-export-separated", ["--profile", "mp4-export", "config"]),
        ("arbitrary-equals", ["--profile=arbitrary", "config"]),
        ("wildcard-separated", ["--profile", "*", "config"]),
        ("wildcard-equals", ["--profile=*", "config"]),
    )
    accepted: list[str] = []
    reached_subprocess: list[str] = []

    for case_name, compose_arguments in cases:
        calls_before = len(subprocess_calls)
        try:
            module.main(["--platform", *compose_arguments])
        except SystemExit:
            pass
        else:
            accepted.append(case_name)
        if len(subprocess_calls) != calls_before:
            reached_subprocess.append(case_name)

    assert accepted == []
    assert reached_subprocess == []


def test_data_plane_wrapper_rejects_profiles_other_than_mp4_export_before_subprocess(
    monkeypatch,
) -> None:
    module = _load_module()
    subprocess_calls = _isolate_production_wrapper_subprocess(module, monkeypatch)
    cases = (
        ("legacy-local-separated", ["--profile", "legacy-local", "config"]),
        ("local-sandbox-equals", ["--profile=local-sandbox", "config"]),
        ("wildcard-separated", ["--profile", "*", "config"]),
        ("wildcard-equals", ["--profile=*", "config"]),
        (
            "allowed-then-wildcard",
            ["--profile", "mp4-export", "--profile", "*", "config"],
        ),
    )
    accepted: list[str] = []
    reached_subprocess: list[str] = []

    for case_name, compose_arguments in cases:
        calls_before = len(subprocess_calls)
        try:
            module.main(["--data-plane", "tenant-acme", *compose_arguments])
        except SystemExit:
            pass
        else:
            accepted.append(case_name)
        if len(subprocess_calls) != calls_before:
            reached_subprocess.append(case_name)

    assert accepted == []
    assert reached_subprocess == []


def test_platform_wrapper_removes_host_compose_topology_environment(
    monkeypatch,
) -> None:
    module = _load_module()
    subprocess_calls = _isolate_production_wrapper_subprocess(module, monkeypatch)
    monkeypatch.setenv("COMPOSE_PROFILES", "legacy-local,local-sandbox")
    monkeypatch.setenv("COMPOSE_FILE", "compose.extra.yml")

    assert module.main(["--platform", "config"]) == 0

    assert len(subprocess_calls) == 1
    command, options = subprocess_calls[0]
    assert command[-5:] == [
        "-f",
        str(module.PROJECT_ROOT / "docker-compose.yml"),
        "-f",
        str(module.PROJECT_ROOT / "docker-compose.platform.yml"),
        "config",
    ]
    assert "COMPOSE_PROFILES" not in options["env"]
    assert "COMPOSE_FILE" not in options["env"]


def test_data_plane_wrapper_allows_only_mp4_export_and_removes_host_topology_environment(
    monkeypatch,
) -> None:
    module = _load_module()
    subprocess_calls = _isolate_production_wrapper_subprocess(module, monkeypatch)
    monkeypatch.setenv("COMPOSE_PROFILES", "*")
    monkeypatch.setenv("COMPOSE_FILE", "compose.extra.yml")

    assert (
        module.main(
            [
                "--data-plane",
                "tenant-acme",
                "--profile",
                "mp4-export",
                "config",
            ]
        )
        == 0
    )

    assert len(subprocess_calls) == 1
    command, options = subprocess_calls[0]
    assert command[-5:] == [
        "-f",
        str(module.PROJECT_ROOT / "docker-compose.data-plane.yml"),
        "--profile",
        "mp4-export",
        "config",
    ]
    assert "COMPOSE_PROFILES" not in options["env"]
    assert "COMPOSE_FILE" not in options["env"]


def test_compose_files_do_not_consume_legacy_env_names() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("docker-compose.yml", "docker-compose.ghcr.yml"):
        content = (root / name).read_text(encoding="utf-8")
        assert "${BACKEND_PORT" not in content
        assert "${FRONTEND_PORT" not in content
        assert "\n      - BACKEND_PORT" not in content
        assert "\n      - AUTH_ENABLED" not in content
        assert "DEEPTUTOR_DOCKER_BACKEND_PORT" in content


def test_platform_wrapper_rejects_invalid_image_lock_before_subprocess(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    root = Path(__file__).resolve().parents[2]
    source_lock = json.loads((root / "deploy" / "image-lock.json").read_text(encoding="utf-8"))
    subprocess_calls: list[list[str]] = []
    outcomes: dict[str, str | None] = {}

    def record_subprocess(command: list[str], **_kwargs) -> SimpleNamespace:
        subprocess_calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.shutil, "which", lambda executable: "docker.exe")
    monkeypatch.setattr(module.subprocess, "run", record_subprocess)

    for scenario in ("zero-digest", "compose-drift"):
        case_root = tmp_path / scenario
        settings_dir = case_root / "data" / "user" / "settings"
        settings_dir.mkdir(parents=True)
        deploy_dir = case_root / "deploy"
        deploy_dir.mkdir()

        lock = json.loads(json.dumps(source_lock))
        source_head = "a" * 40
        release_tag = f"yfeistai-first-release-20260825-{source_head[:8]}"
        lock["schemaVersion"] = 2
        replacements: dict[str, str] = {}
        for index, (name, record) in enumerate(lock["images"].items(), start=1):
            previous = source_lock["images"][name]["reference"]
            if name in {"deeptutor", "openmaic", "openmaic_render"}:
                record["tag"] = release_tag
            digest = "sha256:" + f"{index:064x}"
            record["digest"] = digest
            record["reference"] = f"{record['repository']}:{record['tag']}@{digest}"
            replacements[previous] = record["reference"]
        lock["candidate"] = {
            "sourceRepository": "xinlingzhifei/DeepTutor",
            "sourceHead": source_head,
            "releaseTag": release_tag,
            "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
            "imageDigests": {
                name: lock["images"][name]["digest"]
                for name in ("deeptutor", "openmaic", "openmaic_render")
            },
        }

        if scenario == "zero-digest":
            record = lock["images"]["deeptutor"]
            zero_digest = "sha256:" + ("0" * 64)
            record["digest"] = zero_digest
            record["reference"] = f"{record['repository']}:{record['tag']}@{zero_digest}"
            replacements[source_lock["images"]["deeptutor"]["reference"]] = record["reference"]

        compose_paths = (
            case_root / "docker-compose.platform.yml",
            case_root / "docker-compose.data-plane.yml",
        )
        for source_name, destination in zip(
            ("docker-compose.platform.yml", "docker-compose.data-plane.yml"),
            compose_paths,
            strict=True,
        ):
            rendered = (root / source_name).read_text(encoding="utf-8")
            for previous, replacement in replacements.items():
                rendered = rendered.replace(previous, replacement)
            destination.write_text(rendered, encoding="utf-8")

        if scenario == "compose-drift":
            record = lock["images"]["deeptutor"]
            drifted = record["reference"].replace(
                record["digest"],
                "sha256:" + ("f" * 64),
            )
            compose_paths[0].write_text(
                compose_paths[0].read_text(encoding="utf-8").replace(record["reference"], drifted),
                encoding="utf-8",
            )

        (deploy_dir / "image-lock.json").write_text(
            json.dumps(lock),
            encoding="utf-8",
        )
        monkeypatch.setattr(module, "PROJECT_ROOT", case_root)
        monkeypatch.setattr(module, "SETTINGS_DIR", settings_dir)
        monkeypatch.setattr(
            module,
            "DOCKER_ENV_PATH",
            settings_dir / "docker.env",
        )

        try:
            module.main(["--platform", "config"])
        except ValueError as exc:
            outcomes[scenario] = str(exc)
        else:
            outcomes[scenario] = None

    assert subprocess_calls == []
    assert outcomes == {
        "zero-digest": "image lock entry is invalid for deeptutor",
        "compose-drift": "production Compose image references do not match image lock",
    }


@pytest.mark.parametrize(
    "topology_arguments",
    [
        pytest.param(["--platform"], id="platform"),
        pytest.param(["--data-plane", "tenant-acme"], id="data-plane"),
    ],
)
def test_production_wrapper_rejects_legacy_image_lock_before_subprocess(
    tmp_path: Path,
    monkeypatch,
    topology_arguments: list[str],
) -> None:
    module = _load_module()
    root = Path(__file__).resolve().parents[2]
    settings_dir = tmp_path / "data" / "user" / "settings"
    settings_dir.mkdir(parents=True)
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "image-lock.json").write_bytes((root / "deploy" / "image-lock.json").read_bytes())
    for name in ("docker-compose.platform.yml", "docker-compose.data-plane.yml"):
        (tmp_path / name).write_bytes((root / name).read_bytes())

    downstream_calls: list[str] = []
    subprocess_calls: list[list[str]] = []

    def record_render(**_kwargs) -> dict[str, str]:
        downstream_calls.append("render")
        return {}

    def record_subprocess(command: list[str], **_kwargs) -> SimpleNamespace:
        subprocess_calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "SETTINGS_DIR", settings_dir)
    monkeypatch.setattr(module, "DOCKER_ENV_PATH", settings_dir / "docker.env")
    monkeypatch.setattr(module, "render_docker_env", record_render)
    monkeypatch.setattr(module.subprocess, "run", record_subprocess)

    with pytest.raises(ValueError, match="candidate"):
        module.main([*topology_arguments, "config"])

    assert downstream_calls == []
    assert subprocess_calls == []


def _write_external_candidate_root(module, tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[2]
    candidate_root = tmp_path / "candidate"
    deploy_dir = candidate_root / "deploy"
    deploy_dir.mkdir(parents=True)
    compose_paths = (
        candidate_root / "docker-compose.platform.yml",
        candidate_root / "docker-compose.data-plane.yml",
    )
    for source_name, destination in zip(
        ("docker-compose.platform.yml", "docker-compose.data-plane.yml"),
        compose_paths,
        strict=True,
    ):
        destination.write_bytes((source_root / source_name).read_bytes())
    digest_index = 0

    def resolve_digest(_reference: str) -> str:
        nonlocal digest_index
        digest_index += 1
        return "sha256:" + f"{digest_index:064x}"

    source_head = "a" * 40
    module._load_platform_renderer().write_image_lock(
        deploy_dir / "image-lock.json",
        digest_resolver=resolve_digest,
        compose_paths=compose_paths,
        source_repository="xinlingzhifei/DeepTutor",
        source_head=source_head,
        release_tag=f"yfeistai-first-release-20260825-{source_head[:8]}",
        openmaic_head="0cf2a330411681190e89f48e20f305345ff99f87",
    )
    return candidate_root


@pytest.mark.parametrize(
    ("topology_arguments", "compose_name"),
    [
        pytest.param(["--platform"], "docker-compose.platform.yml", id="platform"),
        pytest.param(
            ["--data-plane", "tenant-acme"],
            "docker-compose.data-plane.yml",
            id="data-plane",
        ),
    ],
)
def test_production_wrapper_uses_external_candidate_root_without_source_mutation(
    tmp_path: Path,
    monkeypatch,
    topology_arguments: list[str],
    compose_name: str,
) -> None:
    module = _load_module()
    candidate_root = _write_external_candidate_root(module, tmp_path)
    source_root = module.PROJECT_ROOT
    protected_paths = (
        source_root / "deploy" / "image-lock.json",
        source_root / "docker-compose.platform.yml",
        source_root / "docker-compose.data-plane.yml",
    )
    before = {path: path.read_bytes() for path in protected_paths}
    subprocess_calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(module, "SETTINGS_DIR", tmp_path / "settings")
    monkeypatch.setattr(module, "DOCKER_ENV_PATH", tmp_path / "settings" / "docker.env")
    monkeypatch.setattr(
        module,
        "render_docker_env",
        lambda **_kwargs: {
            "DEEPTUTOR_DOCKER_BACKEND_PORT": "8001",
            "DEEPTUTOR_DOCKER_FRONTEND_PORT": "3782",
            "DEEPTUTOR_DOCKER_POCKETBASE_PORT": "8090",
        },
    )
    monkeypatch.setattr(module.shutil, "which", lambda _executable: "docker.exe")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: (
            subprocess_calls.append((command, kwargs)) or SimpleNamespace(returncode=0)
        ),
    )

    assert (
        module.main(
            [
                *topology_arguments,
                "--candidate-root",
                str(candidate_root.resolve()),
                "config",
            ]
        )
        == 0
    )

    assert len(subprocess_calls) == 1
    command, options = subprocess_calls[0]
    assert str(candidate_root / compose_name) in command
    assert "--candidate-root" not in command
    assert options["cwd"] == str(source_root)
    if compose_name == "docker-compose.data-plane.yml":
        project_directory_index = command.index("--project-directory")
        assert command[project_directory_index + 1] == str(source_root)
    assert {path: path.read_bytes() for path in protected_paths} == before


@pytest.mark.parametrize(
    "topology_arguments",
    [
        pytest.param(["--platform"], id="platform"),
        pytest.param(["--data-plane", "tenant-acme"], id="data-plane"),
    ],
)
def test_production_wrapper_rejects_external_candidate_compose_drift_before_subprocess(
    tmp_path: Path,
    monkeypatch,
    topology_arguments: list[str],
) -> None:
    module = _load_module()
    candidate_root = _write_external_candidate_root(module, tmp_path)
    compose_path = candidate_root / "docker-compose.platform.yml"
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8").replace(
            "ghcr.io/xinlingzhifei/deeptutor:",
            "ghcr.io/xinlingzhifei/deeptutor-drifted:",
            1,
        ),
        encoding="utf-8",
    )
    downstream_calls: list[str] = []

    monkeypatch.setattr(
        module,
        "render_docker_env",
        lambda **_kwargs: downstream_calls.append("render") or {},
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: downstream_calls.append("subprocess"),
    )

    with pytest.raises(ValueError, match="production Compose"):
        module.main(
            [
                *topology_arguments,
                "--candidate-root",
                str(candidate_root.resolve()),
                "config",
            ]
        )

    assert downstream_calls == []


def _compose_service(root: Path, name: str) -> dict:
    content = yaml.safe_load((root / name).read_text(encoding="utf-8"))
    return content["services"]["deeptutor"]


def _volume_targets(root: Path, name: str) -> set[str | None]:
    volumes = _compose_service(root, name).get("volumes", [])
    return {
        (volume.get("target") if isinstance(volume, dict) else str(volume).split(":")[1])
        for volume in volumes
    }


def test_default_compose_files_do_not_reserve_codex_callback_ports() -> None:
    """The fixed Codex callback ports are opt-in because other clients use them."""
    root = Path(__file__).resolve().parents[2]
    for name in ("docker-compose.yml", "docker-compose.ghcr.yml", "compose.yaml"):
        ports = _compose_service(root, name).get("ports", [])
        assert not any(re.search(r"(^|:)145[57]:", str(port)) for port in ports), name


def test_codex_oauth_overlay_forwards_loopback_callbacks_to_frontend() -> None:
    """The temporary overlay routes browser callbacks through the Web broker."""
    root = Path(__file__).resolve().parents[2]
    ports = _compose_service(root, "compose.codex-oauth.yaml")["ports"]

    assert set(ports) == {
        "127.0.0.1:1455:${DEEPTUTOR_DOCKER_FRONTEND_PORT:-3782}",
        "127.0.0.1:1457:${DEEPTUTOR_DOCKER_FRONTEND_PORT:-3782}",
    }


def test_official_container_manifests_persist_codex_credentials() -> None:
    """Container recreation must retain data/system, where Codex tokens live."""
    root = Path(__file__).resolve().parents[2]
    for name in ("docker-compose.yml", "docker-compose.ghcr.yml", "compose.yaml"):
        targets = _volume_targets(root, name)
        assert "/app/data" in targets or "/app/data/system" in targets, name


def test_ghcr_compose_persists_complete_data_tree() -> None:
    """Forced recreation must retain every workspace as well as OAuth tokens."""
    root = Path(__file__).resolve().parents[2]
    volumes = _compose_service(root, "docker-compose.ghcr.yml")["volumes"]

    assert "./data:/app/data" in volumes


def test_container_docs_use_temporary_codex_oauth_bridge() -> None:
    """README links to the canonical guide, which owns the exact commands."""
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    guide = (root / "CONTAINERIZATION.md").read_text(encoding="utf-8")
    heading = "### Temporary local Codex OAuth bridge"
    assert heading in guide, f"{heading} was renamed; update this test with it"
    section = guide.split(heading, 1)[1].split("\n### ", 1)[0]
    normalized_section = " ".join(section.replace("\\\n", " ").split())

    assert "CONTAINERIZATION.md#temporary-local-codex-oauth-bridge" in readme
    assert "127.0.0.1:1455:3782" in section
    assert "127.0.0.1:1457:3782" in section
    for base_file in ("docker-compose.yml", "docker-compose.ghcr.yml"):
        assert (
            f"-f {base_file} -f compose.codex-oauth.yaml up -d --force-recreate deeptutor"
        ) in normalized_section
    assert (
        "podman compose -f compose.yaml -f compose.codex-oauth.yaml "
        "up -d --force-recreate deeptutor"
    ) in normalized_section


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


def test_production_dockerfile_can_use_ephemeral_signed_debian_sources() -> None:
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    mount = (
        "--mount=type=secret,id=debian_sources,"
        "target=/etc/apt/sources.list.d/debian.sources,required=false"
    )

    assert content.count(f"RUN {mount} \\") == 2


def test_production_dockerfile_installs_postgresql_16_backup_clients() -> None:
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    production = content.split("FROM python:3.11-slim AS production", 1)[1].split(
        "FROM production AS development", 1
    )[0]
    mount = (
        "RUN --mount=type=secret,id=debian_sources,"
        "target=/etc/apt/sources.list.d/debian.sources,required=false \\\n"
    )

    assert mount in production
    assert "https://www.postgresql.org/media/keys/ACCC4CF8.asc" in production
    assert "https://apt.postgresql.org/pub/repos/apt" in production
    assert "Suites: ${VERSION_CODENAME}-pgdg" in production
    assert "Architectures: $(dpkg --print-architecture)" in production
    assert "$${VERSION_CODENAME}" not in production
    assert "$$(dpkg --print-architecture)" not in production
    assert "Signed-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc" in production
    assert "    postgresql-client-16 \\\n" in production
    assert "pg_dump --version | grep -E '[(]PostgreSQL[)] 16[.]'" in production
    assert "pg_restore --version | grep -E '[(]PostgreSQL[)] 16[.]'" in production


def test_production_dockerfile_installs_rust_from_the_signed_debian_source() -> None:
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    python_base = content.split("FROM python:3.11-slim AS python-base", 1)[1].split(
        "FROM python:3.11-slim AS production", 1
    )[0]

    assert "    rustc \\\n" in python_base
    assert "    cargo \\\n" in python_base
    assert "sh.rustup.rs" not in python_base


def test_production_dockerfile_accepts_ephemeral_pip_configuration() -> None:
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    python_base = content.split("FROM python:3.11-slim AS python-base", 1)[1].split(
        "FROM python:3.11-slim AS production", 1
    )[0]
    mount = "--mount=type=secret,id=pip_config,target=/etc/pip.conf,required=false"

    assert f"RUN {mount} \\\n" in python_base
    assert "pip install --upgrade pip" not in python_base


def test_production_dockerfile_reuses_pip_downloads_after_interruption() -> None:
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    python_base = content.split("FROM python:3.11-slim AS python-base", 1)[1].split(
        "FROM python:3.11-slim AS production", 1
    )[0]

    assert "--mount=type=cache,target=/root/.cache/pip,sharing=locked" in python_base
    assert "PIP_NO_CACHE_DIR=1" not in python_base


def test_production_dockerfile_skips_build_time_python_bytecode() -> None:
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    python_base = content.split("FROM python:3.11-slim AS python-base", 1)[1].split(
        "FROM python:3.11-slim AS production", 1
    )[0]

    assert "python -m pip install --no-compile -r requirements.txt" in python_base


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


def test_production_backend_exports_supervisor_pid_for_complete_memory_accounting() -> None:
    root = Path(__file__).resolve().parents[2]
    content = (root / "Dockerfile").read_text(encoding="utf-8")
    backend = content.split("[program:backend]", 1)[1].split("[program:frontend]", 1)[0]

    assert 'DEEPTUTOR_SUPERVISOR_PID="1"' in backend


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
