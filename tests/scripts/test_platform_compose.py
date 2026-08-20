from __future__ import annotations

from functools import cache
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIRED_PLATFORM_SERVICES = {
    "gateway",
    "postgres",
    "minio",
    "minio-bootstrap",
    "teaching-migrate",
    "tenant-provisioner",
    "openmaic",
    "openmaic-render",
    "teaching-dispatcher",
    "teaching-worker",
    "teaching-export-worker",
    "teaching-reaper",
    "learning-projector",
}


def _load_renderer():
    module_path = ROOT / "scripts" / "render_platform_compose.py"
    assert module_path.is_file(), "platform compose renderer has not been implemented"
    spec = importlib.util.spec_from_file_location(
        "render_platform_compose_under_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


@cache
def _platform_compose() -> dict[str, Any]:
    renderer = _load_renderer()
    return renderer.load_rendered_compose(
        "docker-compose.yml",
        "docker-compose.platform.yml",
        project_root=ROOT,
    )


@cache
def _data_plane_compose() -> dict[str, Any]:
    renderer = _load_renderer()
    tenant_hash = hashlib.sha256(b"tenant-acme").hexdigest()[:16]
    return renderer.load_rendered_compose(
        "docker-compose.data-plane.yml",
        project_root=ROOT,
        profiles=("mp4-export",),
        environment={
            "YFEISTAI_DATA_PLANE_ID": "tenant-acme",
            "YFEISTAI_DATA_PLANE_SECRET_DIR": (
                f"./data/system/secrets/data-planes/tenant_{tenant_hash}"
            ),
        },
    )


def _service_secret_sources(service: dict[str, Any]) -> set[str]:
    sources: set[str] = set()
    for secret in service.get("secrets", []):
        if isinstance(secret, str):
            sources.add(secret)
        elif isinstance(secret, dict):
            source = secret.get("source")
            if source:
                sources.add(str(source))
    return sources


def _writable_volume_targets(service: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    for volume in service.get("volumes", []):
        if not isinstance(volume, dict):
            continue
        if not volume.get("read_only", False) and volume.get("target"):
            targets.add(str(volume["target"]))
    return targets


def _volume_at_target(service: dict[str, Any], target: str) -> dict[str, Any]:
    matches = [
        volume
        for volume in service.get("volumes", [])
        if isinstance(volume, dict) and volume.get("target") == target
    ]
    assert len(matches) == 1, (target, matches)
    return matches[0]


def test_platform_merge_exposes_only_gateway_ports() -> None:
    compose = _platform_compose()
    assert REQUIRED_PLATFORM_SERVICES.issubset(compose["services"])

    for name, service in compose["services"].items():
        if name != "gateway":
            assert not service.get("ports"), name

    published = {str(port["published"]) for port in compose["services"]["gateway"]["ports"]}
    assert published == {"80", "443"}


def test_api_waits_for_migration_and_storage_on_the_private_network() -> None:
    compose = _platform_compose()
    dependencies = compose["services"]["deeptutor"]["depends_on"]
    assert dependencies["teaching-migrate"]["condition"] == ("service_completed_successfully")
    assert dependencies["minio-bootstrap"]["condition"] == ("service_completed_successfully")

    assert compose["networks"]["platform-internal"]["internal"] is True
    for name in REQUIRED_PLATFORM_SERVICES | {"deeptutor"}:
        assert "platform-internal" in compose["services"][name]["networks"], name


def test_only_openmaic_can_reach_the_shared_provider_egress() -> None:
    compose = _platform_compose()
    services = compose["services"]

    assert compose["networks"]["shared-provider-egress"].get("internal", False) is False
    assert not compose["networks"]["shared-provider-egress"].get("external", False)
    assert set(services["openmaic"]["networks"]) == {
        "platform-internal",
        "shared-provider-egress",
    }
    for name, service in services.items():
        if name == "openmaic":
            continue
        assert "shared-provider-egress" not in service.get("networks", []), name


def test_deeptutor_retains_application_egress_without_joining_provider_network() -> None:
    compose = _platform_compose()
    services = compose["services"]

    assert compose["networks"]["platform-service-egress"].get("internal", False) is False
    assert not compose["networks"]["platform-service-egress"].get("external", False)
    assert set(services["deeptutor"]["networks"]) == {
        "platform-internal",
        "platform-service-egress",
    }
    for name, service in services.items():
        if name == "deeptutor":
            continue
        assert "platform-service-egress" not in service.get("networks", []), name


def test_only_provisioner_combines_migration_storage_bootstrap_and_secret_write() -> None:
    compose = _platform_compose()
    services = compose["services"]
    provisioner = services["tenant-provisioner"]
    privileged_secrets = {
        "platform_database_migration_password",
        "minio_bootstrap_access_key",
        "minio_bootstrap_secret_key",
    }
    tenant_secret_directory = "/run/yfeistai/tenant-secrets"

    assert privileged_secrets.issubset(_service_secret_sources(provisioner))
    assert tenant_secret_directory in _writable_volume_targets(provisioner)

    ordinary_services = {
        "deeptutor",
        "openmaic",
        "teaching-dispatcher",
        "teaching-worker",
        "teaching-export-worker",
        "teaching-reaper",
        "learning-projector",
    }
    for name in ordinary_services:
        service = services[name]
        assert _service_secret_sources(service).isdisjoint(privileged_secrets), name
        assert tenant_secret_directory not in _writable_volume_targets(service), name


def test_data_mounts_mask_host_secret_tree_from_runtime_services() -> None:
    compose = _platform_compose()
    services = compose["services"]
    data_services = {
        "deeptutor",
        "teaching-migrate",
        "tenant-provisioner",
        "teaching-dispatcher",
        "teaching-worker",
        "teaching-export-worker",
        "teaching-reaper",
        "learning-projector",
    }

    assert not compose["volumes"]["platform-secret-mask"].get("external", False)
    for name in data_services:
        mask = _volume_at_target(services[name], "/app/data/system/secrets")
        assert mask["type"] == "volume", name
        assert mask["read_only"] is True, name
        assert mask["volume"]["nocopy"] is True, name


def test_teaching_processes_override_the_monolithic_image_entrypoint() -> None:
    services = _platform_compose()["services"]
    process_names = {
        "teaching-dispatcher": "dispatcher",
        "teaching-worker": "worker",
        "teaching-export-worker": "export-worker",
        "teaching-reaper": "reaper",
        "learning-projector": "learning-projector",
    }

    for service_name, process_name in process_names.items():
        service = services[service_name]
        assert service["entrypoint"] == [
            "python",
            "-m",
            "deeptutor.teaching.processes",
        ]
        assert service["command"] == [process_name]
        assert service["user"] == "1000:1000"
        assert service["healthcheck"]["disable"] is True

    provisioner = services["tenant-provisioner"]
    assert provisioner["entrypoint"] == [
        "python",
        "-m",
        "deeptutor.teaching.processes",
    ]
    assert provisioner["command"] == ["tenant-provisioner"]
    assert provisioner["user"] == "1000:1000"
    assert provisioner["healthcheck"]["disable"] is True

    migration = services["teaching-migrate"]
    assert migration["entrypoint"] == ["python"]
    assert migration["command"] == ["scripts/migrate_teaching.py"]
    assert migration["user"] == "1000:1000"
    assert migration["healthcheck"]["disable"] is True


def test_openmaic_images_and_worker_pools_are_separate_and_pinned() -> None:
    compose = _platform_compose()
    services = compose["services"]

    assert services["deeptutor"]["image"].startswith(
        "ghcr.io/xinlingzhifei/deeptutor:first-release@sha256:"
    )
    assert services["openmaic"]["image"].startswith(
        "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330@sha256:"
    )
    assert services["openmaic-render"]["image"].startswith(
        "ghcr.io/xinlingzhifei/openmaic-render:0.3.1-0cf2a330@sha256:"
    )
    lock = json.loads((ROOT / "deploy" / "image-lock.json").read_text(encoding="utf-8"))
    render_source = lock["images"]["openmaic_render"]["source"]
    assert render_source == {
        "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
        "revision": "0cf2a330411681190e89f48e20f305345ff99f87",
        "dockerfile": "render-service/Dockerfile",
    }
    assert "build" not in services["openmaic-render"]
    assert (
        services["openmaic"]["environment"]["YFEISTAI_OPENMAIC_RENDER_ENDPOINT"]
        == "http://openmaic-render:9000"
    )

    assert services["teaching-worker"]["command"][-1] == "worker"
    assert services["teaching-export-worker"]["command"][-1] == "export-worker"
    settings = json.loads((ROOT / "deploy" / "platform.example.json").read_text(encoding="utf-8"))
    assert settings["shared_generation_limit"] == 20
    assert settings["default_tenant_generation_limit"] == 2


def test_image_lock_contains_actual_digest_references_used_by_platform() -> None:
    lock = json.loads((ROOT / "deploy" / "image-lock.json").read_text(encoding="utf-8"))
    expected = {
        "deeptutor",
        "openmaic",
        "openmaic_render",
        "nginx",
        "postgres",
        "minio",
        "minio_client",
    }
    assert expected == set(lock["images"])

    references: dict[str, str] = {}
    for name, record in lock["images"].items():
        assert record["repository"]
        assert record["tag"]
        assert IMAGE_DIGEST_PATTERN.fullmatch(record["digest"]), name
        assert record["digest"] != "sha256:" + ("0" * 64), name
        reference = f"{record['repository']}:{record['tag']}@{record['digest']}"
        assert record["reference"] == reference
        references[name] = reference

    compose = _platform_compose()
    expected_images = {
        "gateway": "nginx",
        "postgres": "postgres",
        "minio": "minio",
        "minio-bootstrap": "minio_client",
        "deeptutor": "deeptutor",
        "teaching-migrate": "deeptutor",
        "tenant-provisioner": "deeptutor",
        "teaching-dispatcher": "deeptutor",
        "teaching-worker": "deeptutor",
        "teaching-export-worker": "deeptutor",
        "teaching-reaper": "deeptutor",
        "learning-projector": "deeptutor",
        "openmaic": "openmaic",
        "openmaic-render": "openmaic_render",
    }
    for service, image_name in expected_images.items():
        assert compose["services"][service]["image"] == references[image_name]
    for name, service in compose["services"].items():
        if service.get("profiles"):
            continue
        assert "@sha256:" in service["image"], name


def test_private_platform_workflow_builds_all_images_and_exports_digest_lock() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "private-platform-images.yml"
    assert workflow_path.is_file(), "private platform image workflow is missing"

    source = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    assert isinstance(workflow, dict)
    assert workflow["permissions"] == {
        "contents": "read",
        "packages": "write",
    }

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and len(jobs) == 1
    job = next(iter(jobs.values()))
    steps = job["steps"]

    checkout = next(step for step in steps if step.get("name") == "Checkout OpenMAIC")
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {
        "repository": "xinlingzhifei/OpenMAIC",
        "ref": "0cf2a330411681190e89f48e20f305345ff99f87",
        "path": "openmaic-upstream",
    }

    build_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == "docker/build-push-action@v6"
    ]
    assert len(build_steps) == 3
    builds_by_tag: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, step in build_steps:
        settings = step["with"]
        tags = [line.strip() for line in str(settings["tags"]).splitlines() if line.strip()]
        assert len(tags) == 1
        assert settings["platforms"] == "linux/amd64"
        assert settings["push"] is True
        builds_by_tag[tags[0]] = (index, settings)

    expected_builds = {
        "ghcr.io/xinlingzhifei/deeptutor:first-release": {
            "context": ".",
            "file": "./Dockerfile",
            "target": "production",
        },
        "ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330": {
            "context": ".",
            "file": "./integrations/openmaic/Dockerfile",
        },
        "ghcr.io/xinlingzhifei/openmaic-render:0.3.1-0cf2a330": {
            "context": "./openmaic-upstream/render-service",
            "file": "./openmaic-upstream/render-service/Dockerfile",
        },
    }
    assert set(builds_by_tag) == set(expected_builds)
    for tag, expected in expected_builds.items():
        settings = builds_by_tag[tag][1]
        for key, value in expected.items():
            assert settings[key] == value, tag

    write_index, write_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if "render_platform_compose.py --write-image-lock" in str(step.get("run", ""))
    )
    assert all(index < write_index for index, _step in build_steps)

    verify_index, verify_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if "load_image_lock" in str(step.get("run", ""))
    )
    assert verify_index > write_index
    verify_command = str(verify_step["run"])
    assert "from scripts.render_platform_compose import load_image_lock" in verify_command
    assert "load_image_lock()" in verify_command

    upload_index, upload_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == "actions/upload-artifact@v4"
    )
    assert upload_index > verify_index
    artifact_paths = {
        line.strip() for line in str(upload_step["with"]["path"]).splitlines() if line.strip()
    }
    assert artifact_paths == {
        "deploy/image-lock.json",
        "docker-compose.platform.yml",
        "docker-compose.data-plane.yml",
    }


def test_image_lock_writer_updates_compose_atomically_from_registry_digests(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    platform_compose = tmp_path / "docker-compose.platform.yml"
    data_plane_compose = tmp_path / "docker-compose.data-plane.yml"
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    platform_compose.write_bytes((ROOT / "docker-compose.platform.yml").read_bytes())
    data_plane_compose.write_bytes((ROOT / "docker-compose.data-plane.yml").read_bytes())
    platform_before = platform_compose.read_bytes()
    data_plane_before = data_plane_compose.read_bytes()

    partial_calls: list[str] = []

    def resolve_until_missing(reference: str) -> str | None:
        partial_calls.append(reference)
        if len(partial_calls) == 4:
            return None
        return "sha256:" + f"{len(partial_calls):064x}"

    with pytest.raises(ValueError, match="registry digest"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve_until_missing,
            compose_paths=(platform_compose, data_plane_compose),
        )
    assert len(partial_calls) == 4
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"sentinel": True}
    assert platform_compose.read_bytes() == platform_before
    assert data_plane_compose.read_bytes() == data_plane_before

    calls: list[str] = []

    def resolve(reference: str) -> str:
        calls.append(reference)
        return "sha256:" + f"{len(calls):064x}"

    written = renderer.write_image_lock(
        output_path,
        digest_resolver=resolve,
        compose_paths=(platform_compose, data_plane_compose),
    )
    assert set(written["images"]) == {
        "deeptutor",
        "openmaic",
        "openmaic_render",
        "nginx",
        "postgres",
        "minio",
        "minio_client",
    }
    assert len(calls) == 7
    assert json.loads(output_path.read_text(encoding="utf-8")) == written
    assert (
        renderer.image_reference("nginx", lock_path=output_path)
        == (written["images"]["nginx"]["reference"])
    )

    platform_text = platform_compose.read_text(encoding="utf-8")
    data_plane_text = data_plane_compose.read_text(encoding="utf-8")
    for name, record in written["images"].items():
        assert record["reference"] in platform_text, name
    for name in ("openmaic", "openmaic_render"):
        assert written["images"][name]["reference"] in data_plane_text
    assert "sha256:" + ("0" * 64) not in platform_text
    assert "sha256:" + ("0" * 64) not in data_plane_text


def test_image_lock_cli_updates_both_production_compose_files(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    captured: dict[str, Any] = {}

    def write_image_lock(
        path: Path,
        *,
        compose_paths: tuple[Path, Path],
    ) -> dict[str, Any]:
        captured["path"] = path
        captured["compose_paths"] = compose_paths
        return {}

    monkeypatch.setattr(renderer, "write_image_lock", write_image_lock)

    assert renderer.main(["--write-image-lock", "--lock-path", str(output_path)]) == 0
    assert captured == {
        "path": output_path,
        "compose_paths": (
            ROOT / "docker-compose.platform.yml",
            ROOT / "docker-compose.data-plane.yml",
        ),
    }
    assert capsys.readouterr().out == f"{output_path}\n"


def test_image_lock_writer_cleans_staged_files_when_publish_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    output_path = tmp_path / "image-lock.json"
    platform_compose = tmp_path / "docker-compose.platform.yml"
    data_plane_compose = tmp_path / "docker-compose.data-plane.yml"
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    platform_compose.write_bytes((ROOT / "docker-compose.platform.yml").read_bytes())
    data_plane_compose.write_bytes((ROOT / "docker-compose.data-plane.yml").read_bytes())
    originals = {
        path: path.read_bytes() for path in (output_path, platform_compose, data_plane_compose)
    }

    calls = 0

    def resolve(reference: str) -> str:
        nonlocal calls
        calls += 1
        return "sha256:" + f"{calls:064x}"

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(renderer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated publish failure"):
        renderer.write_image_lock(
            output_path,
            digest_resolver=resolve,
            compose_paths=(platform_compose, data_plane_compose),
        )
    assert calls == 7
    for path, original in originals.items():
        assert path.read_bytes() == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_image_lock_writer_rolls_back_every_file_after_late_publish_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    real_replace = renderer.os.replace
    partial_publications: list[str] = []

    for failure_index in (2, 3):
        case_dir = tmp_path / f"replace-{failure_index}"
        case_dir.mkdir()
        output_path = case_dir / "image-lock.json"
        platform_compose = case_dir / "docker-compose.platform.yml"
        data_plane_compose = case_dir / "docker-compose.data-plane.yml"
        output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
        platform_compose.write_bytes((ROOT / "docker-compose.platform.yml").read_bytes())
        data_plane_compose.write_bytes((ROOT / "docker-compose.data-plane.yml").read_bytes())
        originals = {
            path: path.read_bytes() for path in (output_path, platform_compose, data_plane_compose)
        }

        digest_calls = 0
        replace_calls = 0

        def resolve(reference: str) -> str:
            nonlocal digest_calls
            digest_calls += 1
            return "sha256:" + f"{digest_calls:064x}"

        def fail_late_replace(source: Path, target: Path) -> None:
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == failure_index:
                raise OSError(f"simulated publish failure {failure_index}")
            real_replace(source, target)

        monkeypatch.setattr(renderer.os, "replace", fail_late_replace)

        with pytest.raises(
            OSError,
            match=f"simulated publish failure {failure_index}",
        ):
            renderer.write_image_lock(
                output_path,
                digest_resolver=resolve,
                compose_paths=(platform_compose, data_plane_compose),
            )

        assert digest_calls == 7
        assert replace_calls == failure_index
        for path, original in originals.items():
            if path.read_bytes() != original:
                partial_publications.append(
                    f"replace {failure_index} partially published {path.name}"
                )
        assert list(case_dir.glob(".*.tmp")) == []

    assert partial_publications == []


def test_registry_digest_hashes_raw_remote_manifest_bytes_without_local_image(
    monkeypatch,
) -> None:
    renderer = _load_renderer()
    raw_manifest = (
        b'{\n  "schemaVersion": 2,\n'
        b'  "mediaType": "application/vnd.oci.image.index.v1+json",\n'
        b'  "manifests": []\n}\n'
    )
    digest = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
    commands: list[list[str]] = []
    subprocess_options: list[dict[str, Any]] = []

    def inspect_remote_manifest(command: list[str], **_kwargs) -> SimpleNamespace:
        commands.append(command)
        subprocess_options.append(_kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=raw_manifest,
            stderr=b"",
        )

    monkeypatch.setattr(renderer.shutil, "which", lambda executable: "docker.exe")
    monkeypatch.setattr(renderer.subprocess, "run", inspect_remote_manifest)

    reference = "registry.example/yfeistai/deeptutor:first-release"
    resolved = renderer._registry_digest(reference)
    assert commands == [
        [
            "docker.exe",
            "buildx",
            "imagetools",
            "inspect",
            "--raw",
            reference,
        ]
    ]
    assert not subprocess_options[0].get("text", False)
    assert resolved == digest


def test_data_plane_is_project_scoped_private_and_uses_dedicated_secrets() -> None:
    compose = _data_plane_compose()
    assert set(compose["services"]) == {"openmaic", "openmaic-render"}
    for service in compose["services"].values():
        assert not service.get("ports")
    assert set(compose["services"]["openmaic"]["networks"]) == {
        "tenant-data-plane",
        "tenant-provider-egress",
    }
    assert set(compose["services"]["openmaic-render"]["networks"]) == {"tenant-data-plane"}
    assert compose["services"]["openmaic-render"]["profiles"] == ["mp4-export"]
    assert compose["networks"]["tenant-data-plane"]["internal"] is True
    assert not compose["networks"]["tenant-data-plane"].get("external", False)
    assert compose["networks"]["tenant-provider-egress"].get("internal", False) is False
    assert not compose["networks"]["tenant-provider-egress"].get("external", False)

    secret = compose["secrets"]["openmaic_service_secret"]
    tenant_hash = hashlib.sha256(b"tenant-acme").hexdigest()[:16]
    assert f"tenant_{tenant_hash}" in secret["file"]
    assert _service_secret_sources(compose["services"]["openmaic"]) == {"openmaic_service_secret"}
