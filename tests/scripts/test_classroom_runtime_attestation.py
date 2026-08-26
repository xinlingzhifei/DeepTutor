from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE_RUN = {"runId": "release-run-1", "environmentId": "staging-1"}
CANDIDATE = {
    "sourceRepository": "xinlingzhifei/DeepTutor",
    "sourceHead": "a" * 40,
    "releaseTag": "yfeistai-first-release-20260825-aaaaaaaa",
    "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
    "imageDigests": {
        "deeptutor": "sha256:" + "1" * 64,
        "openmaic": "sha256:" + "2" * 64,
        "openmaic_render": "sha256:" + "3" * 64,
    },
}
SERVICES = {
    "deeptutor": {"restart": "unless-stopped", "health": "healthy"},
    "postgres": {"restart": "unless-stopped", "health": "healthy"},
    "minio": {"restart": "unless-stopped", "health": "healthy"},
    "openmaic": {"restart": "unless-stopped", "health": "healthy"},
    "openmaic-render": {"restart": "unless-stopped", "health": "healthy"},
    "teaching-migrate": {"restart": "no", "health": "none"},
}


def _load_module():
    path = ROOT / "scripts" / "classroom_runtime_attestation.py"
    assert path.is_file(), "fixed classroom runtime attestation producer is missing"
    spec = importlib.util.spec_from_file_location("classroom_runtime_attestation_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_verifier():
    path = ROOT / "scripts" / "verify_classroom_release.py"
    spec = importlib.util.spec_from_file_location("runtime_attestation_verifier_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _candidate_root(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "candidate"
    deploy = root / "deploy"
    deploy.mkdir(parents=True)
    release_tag = CANDIDATE["releaseTag"]
    candidate_digests = CANDIDATE["imageDigests"]
    assert isinstance(release_tag, str) and isinstance(candidate_digests, dict)
    specifications = {
        "deeptutor": (
            "ghcr.io/xinlingzhifei/deeptutor",
            release_tag,
            candidate_digests["deeptutor"],
        ),
        "openmaic": (
            "ghcr.io/xinlingzhifei/openmaic",
            release_tag,
            candidate_digests["openmaic"],
        ),
        "openmaic_render": (
            "ghcr.io/xinlingzhifei/openmaic-render",
            release_tag,
            candidate_digests["openmaic_render"],
        ),
        "nginx": ("nginx", "1.29.8-alpine3.23", "sha256:" + "4" * 64),
        "postgres": ("postgres", "16.14-alpine3.24", "sha256:" + "5" * 64),
        "minio": (
            "minio/minio",
            "RELEASE.2025-04-22T22-12-26Z",
            "sha256:" + "6" * 64,
        ),
        "minio_client": (
            "minio/mc",
            "RELEASE.2025-04-16T18-13-26Z",
            "sha256:" + "7" * 64,
        ),
    }
    images = {
        name: {
            "repository": repository,
            "tag": tag,
            "digest": digest,
            "reference": f"{repository}:{tag}@{digest}",
        }
        for name, (repository, tag, digest) in specifications.items()
    }
    (deploy / "image-lock.json").write_text(
        json.dumps({"schemaVersion": 2, "candidate": CANDIDATE, "images": images}),
        encoding="utf-8",
    )
    image_references = {name: record["reference"] for name, record in images.items()}
    service_images = {
        "deeptutor": image_references["deeptutor"],
        "postgres": image_references["postgres"],
        "minio": image_references["minio"],
        "openmaic": image_references["openmaic"],
        "openmaic-render": image_references["openmaic_render"],
        "teaching-migrate": image_references["deeptutor"],
    }
    compose_services = {
        service: {"image": service_images[service], "restart": settings["restart"]}
        for service, settings in SERVICES.items()
    }
    compose_services["profiled-debug"] = {
        "image": image_references["deeptutor"],
        "profiles": ["debug"],
    }
    (root / "docker-compose.platform.yml").write_text(
        json.dumps({"services": compose_services}), encoding="utf-8"
    )
    return root, service_images


def _repo_digest(reference: str) -> str:
    tagged, digest = reference.rsplit("@", 1)
    return f"{tagged.rsplit(':', 1)[0]}@{digest}"


def _local_image_id(reference: str) -> str:
    return "sha256:local-" + hashlib.sha256(reference.encode()).hexdigest()


def _container(service: str, reference: str) -> dict[str, object]:
    running = SERVICES[service]["restart"] != "no"
    state: dict[str, object] = {
        "Status": "running" if running else "exited",
        "Running": running,
        "Restarting": False,
        "ExitCode": 0,
    }
    if SERVICES[service]["health"] != "none":
        state["Health"] = {"Status": SERVICES[service]["health"]}
    return {
        "Id": f"container-{service}",
        "Image": _local_image_id(reference),
        "Config": {
            "Image": reference,
            "Labels": {
                "com.docker.compose.project": "yfeistai-platform",
                "com.docker.compose.service": service,
            },
        },
        "State": state,
    }


def _minimal_container(raw: dict[str, object]) -> dict[str, object]:
    config = raw["Config"]
    state = raw["State"]
    assert isinstance(config, dict) and isinstance(state, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    health_record = state.get("Health")
    return {
        "containerId": raw["Id"],
        "localImageId": raw["Image"],
        "configImage": config["Image"],
        "project": labels["com.docker.compose.project"],
        "service": labels["com.docker.compose.service"],
        "state": state["Status"],
        "running": state["Running"],
        "restarting": state["Restarting"],
        "exitCode": state["ExitCode"],
        "health": (health_record["Status"] if isinstance(health_record, dict) else "none"),
    }


class FakeDocker:
    def __init__(self, references: dict[str, str]) -> None:
        self.references = references
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.by_id = {
            f"container-{service}": _container(service, reference)
            for service, reference in references.items()
        }
        self.after_by_id: dict[str, dict[str, object]] | None = None
        self.ps_count = 0
        self.repo_digest_overrides: dict[str, list[str]] = {}
        self.image_id_overrides: dict[str, str] = {}
        self.fail_prefix: list[str] | None = None
        self.on_call: Callable[[int], None] | None = None

    def __call__(self, arguments: list[str], **options: object):
        docker_config = Path(arguments[2])
        assert docker_config.is_dir()
        assert list(docker_config.iterdir()) == []
        self.calls.append((arguments, options))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        docker_arguments = arguments[5:]
        if (
            self.fail_prefix is not None
            and docker_arguments[: len(self.fail_prefix)] == self.fail_prefix
        ):
            return subprocess.CompletedProcess(arguments, 17, stdout=b"native failure", stderr=b"")
        if docker_arguments[0] == "ps":
            self.ps_count += 1
            containers = (
                self.after_by_id
                if self.ps_count == 2 and self.after_by_id is not None
                else self.by_id
            )
            body = b"\n".join(
                json.dumps(container_id).encode() for container_id in sorted(containers)
            )
        elif docker_arguments[:2] == ["container", "inspect"]:
            containers = (
                self.after_by_id
                if self.ps_count == 2 and self.after_by_id is not None
                else self.by_id
            )
            body = json.dumps(_minimal_container(containers[docker_arguments[-1]])).encode()
        else:
            assert docker_arguments[:2] == ["image", "inspect"]
            reference = docker_arguments[-1]
            body = json.dumps(
                [
                    {
                        "imageId": self.image_id_overrides.get(
                            reference, _local_image_id(reference)
                        ),
                        "repoDigests": self.repo_digest_overrides.get(
                            reference, [_repo_digest(reference)]
                        ),
                    }
                ]
            )[1:-1].encode()
        return subprocess.CompletedProcess(arguments, 0, stdout=body, stderr=b"")


def test_stable_runtime_is_attested_with_only_fixed_read_only_docker_commands(
    tmp_path: Path,
) -> None:
    module = _load_module()
    candidate_root, references = _candidate_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    malicious_runtime_config = bundle_root / "runtime" / "docker-config"
    malicious_runtime_config.mkdir(parents=True)
    (malicious_runtime_config / "config.json").write_text(
        json.dumps({"currentContext": "attacker"}), encoding="utf-8"
    )
    attacker_home = tmp_path / "attacker-home"
    (attacker_home / ".docker").mkdir(parents=True)
    (attacker_home / ".docker" / "config.json").write_text(
        json.dumps({"currentContext": "attacker"}), encoding="utf-8"
    )
    docker = tmp_path / "trusted" / "docker.exe"
    docker.parent.mkdir()
    docker.write_bytes(b"docker")
    runner = FakeDocker(references)

    report = module.produce_runtime_attestation(
        candidate_root=candidate_root,
        bundle_root=bundle_root,
        release_run=RELEASE_RUN,
        observed_at="2026-08-25T00:00:00Z",
        base_url="https://candidate.example.test",
        runner=runner,
        docker_resolver=lambda: docker,
        environ={
            "SystemRoot": "C:/Windows",
            "TEMP": "C:/Temp",
            "DOCKER_HOST": "tcp://attacker:2375",
            "DOCKER_CONTEXT": "attacker",
            "DOCKER_CONFIG": str(attacker_home / ".docker"),
            "HOME": str(attacker_home),
            "USERPROFILE": str(attacker_home),
            "PATH": "C:/attacker",
        },
    )

    output = bundle_root / "runtime" / "runtime-attestation.json"
    assert report == json.loads(output.read_text(encoding="utf-8"))
    assert report["schemaVersion"] == 1
    assert report["candidate"] == CANDIDATE
    assert report["releaseRun"] == RELEASE_RUN
    assert report["project"] == "yfeistai-platform"
    assert report["beforeSnapshot"] == report["afterSnapshot"]
    assert [item["service"] for item in report["containers"]] == sorted(SERVICES)
    assert "profiled-debug" not in json.dumps(report)
    for command in report["commands"]:
        assert set(command) == {"argv", "nativeExit", "stdout", "stdoutSha256"}
        raw_stdout = command["stdout"].encode("utf-8")
        assert command["stdoutSha256"] == hashlib.sha256(raw_stdout).hexdigest()

    commands = [call[0][5:] for call in runner.calls]
    assert (
        commands.count(
            [
                "ps",
                "-a",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=yfeistai-platform",
                "--format",
                "{{json .ID}}",
            ]
        )
        == 2
    )
    assert all(
        command[:2] in (["container", "inspect"], ["image", "inspect"]) or command[0] == "ps"
        for command in commands
    )
    assert not any(
        forbidden in command
        for command in commands
        for forbidden in (
            "build",
            "pull",
            "push",
            "up",
            "run",
            "start",
            "stop",
            "restart",
            "rm",
            "prune",
        )
    )
    for _arguments, options in runner.calls:
        assert options["cwd"] == candidate_root.resolve()
        assert options["timeout"] == 30
        assert options["check"] is False
        assert options["capture_output"] is True
        assert options["env"] == {"SYSTEMROOT": "C:/Windows", "TEMP": "C:/Temp"}
    for arguments, _options in runner.calls:
        assert arguments[1] == "--config"
        isolated_config = Path(arguments[2])
        assert isolated_config.parent == bundle_root / "runtime"
        assert isolated_config.name.startswith(".docker-config-")
        assert isolated_config != malicious_runtime_config
        assert arguments[3:5] == ["--context", "default"]
    isolated_configs = {Path(arguments[2]) for arguments, _options in runner.calls}
    assert isolated_configs
    assert all(not isolated_config.exists() for isolated_config in isolated_configs)
    assert json.loads((malicious_runtime_config / "config.json").read_text(encoding="utf-8")) == {
        "currentContext": "attacker"
    }
    for command in report["commands"]:
        serialized = json.dumps(command["argv"])
        assert "<isolated-docker-config>" in serialized
        assert str(tmp_path) not in serialized


def _produce(tmp_path: Path, runner: FakeDocker, *, module=None) -> Path:
    candidate_root = tmp_path / "candidate"
    bundle_root = tmp_path / "bundle"
    docker = tmp_path / "trusted" / "docker.exe"
    docker.parent.mkdir(exist_ok=True)
    docker.write_bytes(b"docker")
    module = _load_module() if module is None else module
    module.produce_runtime_attestation(
        candidate_root=candidate_root,
        bundle_root=bundle_root,
        release_run=RELEASE_RUN,
        observed_at="2026-08-25T00:00:00Z",
        base_url="https://candidate.example.test",
        runner=runner,
        docker_resolver=lambda: docker,
        environ={"SystemRoot": "C:/Windows"},
    )
    return bundle_root / "runtime" / "runtime-attestation.json"


def test_attestation_does_not_remove_preexisting_config_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    collision = runtime_root / ".docker-config-fixed"
    collision.mkdir(parents=True)
    sentinel = collision / "config.json"
    sentinel.write_bytes(b"preexisting sentinel")
    module = _load_module()

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(module.uuid, "uuid4", lambda: FixedUuid())
    runner = FakeDocker(references)

    with pytest.raises(ValueError, match="config|directory"):
        _produce(tmp_path, runner, module=module)

    assert runner.calls == []
    assert sentinel.read_bytes() == b"preexisting sentinel"


@pytest.mark.parametrize("prior_canonical", (False, True))
def test_last_docker_call_config_pollution_blocks_publication(
    tmp_path: Path, prior_canonical: bool
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runner = FakeDocker(references)
    final_call = 2 + (2 * len(runner.by_id)) + len(set(references.values()))

    def pollute_config(call_count: int) -> None:
        if call_count == final_call:
            config = Path(runner.calls[-1][0][2])
            (config / "pollution.json").write_bytes(b"runner pollution")

    runner.on_call = pollute_config
    canonical = tmp_path / "bundle" / "runtime" / "runtime-attestation.json"
    canonical.parent.mkdir(parents=True)
    if prior_canonical:
        canonical.write_bytes(b"existing canonical")

    with pytest.raises(ValueError, match="config|cleanup|recovery"):
        _produce(tmp_path, runner)

    if prior_canonical:
        assert canonical.read_bytes() == b"existing canonical"
    else:
        assert not canonical.exists()
    recovery_configs = list(canonical.parent.glob(".docker-config-*"))
    assert len(recovery_configs) == 1
    assert (recovery_configs[0] / "pollution.json").read_bytes() == b"runner pollution"


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows relative create handle")
def test_config_create_after_swap_does_not_own_or_delete_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    replacement = runtime_root / ".docker-config-fixed"
    recovery = runtime_root / "config-created-recovery"
    module = _load_module()
    real_create = module._create_windows_directory_relative
    attack_attempted = False
    attack_blocked = False

    def create_after_swap(directory_handle: object, name: str):
        nonlocal attack_attempted, attack_blocked
        result = real_create(directory_handle, name)
        if name == replacement.name and not attack_attempted:
            attack_attempted = True
            try:
                replacement.rename(recovery)
            except OSError as exc:
                attack_blocked = True
                raise ValueError("config creation swap was blocked") from exc
            replacement.mkdir()
        return result

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(module.uuid, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(module, "_create_windows_directory_relative", create_after_swap)

    with pytest.raises(ValueError, match="config|identity|boundary|recovery"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert attack_attempted
    assert attack_blocked
    assert replacement.is_dir()
    assert not recovery.exists()
    assert not (runtime_root / "runtime-attestation.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows config handle cleanup")
def test_windows_config_identity_failure_disposes_created_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    bundle_root, runtime_root = module._prepare_runtime_root(tmp_path / "bundle")
    guard = module._RuntimeDirectoryGuard.open(bundle_root, runtime_root)
    config_name = ".docker-config-identity-failure"

    def fail_identity(_handle: object, *, directory: bool) -> tuple[int, int]:
        assert directory
        raise OSError("injected config identity failure")

    monkeypatch.setattr(module, "_windows_handle_identity", fail_identity)
    try:
        with pytest.raises(OSError, match="injected config identity failure"):
            module._create_windows_directory_relative(guard._runtime_handle, config_name)
    finally:
        guard.close()

    assert not (runtime_root / config_name).exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX config initialization cleanup")
@pytest.mark.parametrize(
    ("error_type", "message"),
    (
        (OSError, "injected replacement config OSError"),
        (KeyboardInterrupt, "injected replacement config KeyboardInterrupt"),
        (SystemExit, "injected replacement config SystemExit"),
    ),
    ids=("os-error", "keyboard-interrupt", "system-exit"),
)
def test_posix_config_initial_identity_failure_preserves_unknown_replacement_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    message: str,
) -> None:
    module = _load_module()
    bundle_root, runtime_root = module._prepare_runtime_root(tmp_path / "bundle")
    guard = module._RuntimeDirectoryGuard.open(bundle_root, runtime_root)
    config_name = ".docker-config-initial-identity-replacement"
    recovery_name = ".docker-config-initial-identity-recovery"
    real_entry_details = guard._entry_details
    replaced = False

    def replace_then_fail_identity(name: str) -> os.stat_result:
        nonlocal replaced
        if name == config_name and not replaced:
            replaced = True
            (runtime_root / config_name).rename(runtime_root / recovery_name)
            (runtime_root / config_name).mkdir()
            raise error_type(message)
        return real_entry_details(name)

    monkeypatch.setattr(guard, "_entry_details", replace_then_fail_identity)
    try:
        with pytest.raises(error_type, match=message) as exception_info:
            guard.create_empty_directory(config_name)
    finally:
        guard.close()

    assert replaced
    assert (runtime_root / config_name).is_dir()
    assert (runtime_root / recovery_name).is_dir()
    if hasattr(exception_info.value, "add_note"):
        assert "isolated Docker config creation recovery required" in getattr(
            exception_info.value, "__notes__", ()
        )


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX config initialization cleanup")
@pytest.mark.parametrize(
    ("error_type", "message"),
    (
        (OSError, "injected initial config OSError"),
        (KeyboardInterrupt, "injected initial config KeyboardInterrupt"),
        (SystemExit, "injected initial config SystemExit"),
    ),
    ids=("os-error", "keyboard-interrupt", "system-exit"),
)
def test_posix_config_create_removes_owned_directory_when_after_identity_capture_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    message: str,
) -> None:
    module = _load_module()
    bundle_root, runtime_root = module._prepare_runtime_root(tmp_path / "bundle")
    guard = module._RuntimeDirectoryGuard.open(bundle_root, runtime_root)
    config_name = ".docker-config-after-identity-failure"
    real_entry_details = guard._entry_details
    entry_details_calls = 0

    def fail_after_initial_identity(name: str) -> os.stat_result:
        nonlocal entry_details_calls
        if name == config_name:
            entry_details_calls += 1
        if name == config_name and entry_details_calls == 1:
            assert (runtime_root / config_name).is_dir()
        if name == config_name and entry_details_calls == 2:
            raise error_type(message)
        return real_entry_details(name)

    monkeypatch.setattr(guard, "_entry_details", fail_after_initial_identity)
    try:
        with pytest.raises(error_type, match=message):
            guard.create_empty_directory(config_name)
    finally:
        guard.close()

    assert entry_details_calls >= 2
    assert not (runtime_root / config_name).exists()


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows owned directory handle")
def test_config_cleanup_swap_does_not_delete_replacement_or_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runner = FakeDocker(references)
    module = _load_module()
    real_delete = module._delete_windows_file_on_close
    attack_attempted = False
    attack_blocked = False
    replacement: Path | None = None

    def delete_after_swap(handle: object) -> None:
        nonlocal attack_attempted, attack_blocked, replacement
        if not attack_attempted and runner.calls:
            replacement = Path(runner.calls[-1][0][2])
            recovery = replacement.with_name("config-cleanup-recovery")
            attack_attempted = True
            try:
                replacement.rename(recovery)
            except OSError as exc:
                attack_blocked = True
                raise ValueError("config cleanup swap was blocked") from exc
            replacement.mkdir()
        real_delete(handle)

    monkeypatch.setattr(module, "_delete_windows_file_on_close", delete_after_swap)

    with pytest.raises(ValueError, match="config|cleanup|recovery"):
        _produce(tmp_path, runner, module=module)

    assert attack_attempted and attack_blocked
    assert replacement is not None and replacement.is_dir()
    assert not (tmp_path / "bundle" / "runtime" / "runtime-attestation.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX mkdirat/openat identity checks")
def test_posix_config_create_swap_between_stat_and_open_is_not_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    replacement = runtime_root / ".docker-config-fixed"
    recovery = runtime_root / "config-create-recovery"
    module = _load_module()
    real_open = module.os.open
    swapped = False

    def open_after_swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == replacement.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            replacement.rename(recovery)
            replacement.mkdir()
        return real_open(path, flags, *args, **kwargs)

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(module.uuid, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(module.os, "open", open_after_swap)

    with pytest.raises(ValueError, match="config|identity|boundary|recovery"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert swapped
    assert replacement.is_dir()
    assert recovery.is_dir()
    assert not (runtime_root / "runtime-attestation.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX provisional fd cleanup")
def test_posix_config_create_closes_fd_when_post_open_identity_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    module = _load_module()
    real_open = module.os.open
    real_fstat = module.os.fstat
    config_fd: int | None = None
    failure_injected = False

    def record_open(path, flags, *args, **kwargs):
        nonlocal config_fd
        file_descriptor = real_open(path, flags, *args, **kwargs)
        if path == ".docker-config-fixed" and kwargs.get("dir_fd") is not None:
            config_fd = file_descriptor
        return file_descriptor

    def fail_config_fstat(file_descriptor: int):
        nonlocal failure_injected
        if file_descriptor == config_fd and not failure_injected:
            failure_injected = True
            raise OSError("injected config identity failure")
        return real_fstat(file_descriptor)

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(module.uuid, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(module.os, "open", record_open)
    monkeypatch.setattr(module.os, "fstat", fail_config_fstat)

    with pytest.raises(OSError, match="injected config identity failure"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert failure_injected and config_fd is not None
    with pytest.raises(OSError):
        real_fstat(config_fd)
    assert not (tmp_path / "bundle" / "runtime" / ".docker-config-fixed").exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX quarantine identity checks")
def test_posix_config_cleanup_swap_before_final_identity_does_not_rmdir_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    module = _load_module()
    real_stat = module.os.stat
    quarantine_checks = 0
    swapped = False
    replacement: Path | None = None
    recovery: Path | None = None

    def stat_after_swap(path, *args, **kwargs):
        nonlocal quarantine_checks, swapped, replacement, recovery
        name = Path(path).name
        if name.endswith(".cleanup") and kwargs.get("dir_fd") is not None:
            quarantine_checks += 1
            if quarantine_checks == 2 and not swapped:
                replacement = runtime_root / name
                recovery = runtime_root / "config-cleanup-recovery"
                replacement.rename(recovery)
                replacement.mkdir()
                swapped = True
        return real_stat(path, *args, **kwargs)

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(module.uuid, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(module.os, "stat", stat_after_swap)

    with pytest.raises(ValueError, match="config|identity|cleanup|recovery"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert quarantine_checks >= 2 and swapped
    assert replacement is not None and replacement.is_dir()
    assert recovery is not None and recovery.is_dir()
    assert not (runtime_root / "runtime-attestation.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX no-replace quarantine")
@pytest.mark.parametrize("collision_kind", ("directory", "symlink"))
def test_posix_config_cleanup_quarantine_collision_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision_kind: str,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    quarantine = runtime_root / "..docker-config-fixed.fixed.cleanup"
    external = tmp_path / "external-config-collision"
    external.mkdir()
    sentinel = external / "sentinel.json"
    sentinel.write_bytes(b"collision sentinel")
    if collision_kind == "directory":
        quarantine.mkdir()
        collision_sentinel = quarantine / "sentinel.json"
        collision_sentinel.write_bytes(b"collision sentinel")
    else:
        quarantine.symlink_to(external, target_is_directory=True)
        collision_sentinel = sentinel
    module = _load_module()

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(module.uuid, "uuid4", lambda: FixedUuid())

    with pytest.raises(ValueError, match="config|cleanup|recovery|exist"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert collision_sentinel.read_bytes() == b"collision sentinel"
    assert quarantine.exists() or quarantine.is_symlink()
    assert (runtime_root / ".docker-config-fixed").is_dir()
    assert not (runtime_root / "runtime-attestation.json").exists()


@pytest.mark.parametrize("case", ("missing", "extra"))
def test_attestation_rejects_missing_or_extra_enabled_service(tmp_path: Path, case: str) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runner = FakeDocker(references)
    if case == "missing":
        runner.by_id.pop("container-postgres")
    else:
        extra = _container("deeptutor", references["deeptutor"])
        extra["Id"] = "container-extra"
        config = extra["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels["com.docker.compose.service"] = "extra"
        runner.by_id["container-extra"] = extra

    with pytest.raises(ValueError, match="service"):
        _produce(tmp_path, runner)

    assert not (tmp_path / "bundle" / "runtime" / "runtime-attestation.json").exists()


@pytest.mark.parametrize(
    "case",
    (
        "wrong-config-image",
        "wrong-local-image",
        "wrong-repo-digest",
        "shared-image-ref-inconsistent",
        "wrong-project-label",
        "unhealthy",
        "restarting",
        "one-shot-nonzero",
    ),
)
def test_attestation_rejects_container_or_image_mismatch(tmp_path: Path, case: str) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runner = FakeDocker(references)
    container = runner.by_id["container-deeptutor"]
    state = container["State"]
    config = container["Config"]
    assert isinstance(state, dict) and isinstance(config, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    if case == "wrong-config-image":
        config["Image"] = references["postgres"]
    elif case == "wrong-local-image":
        runner.image_id_overrides[references["deeptutor"]] = "sha256:attacker"
    elif case == "wrong-repo-digest":
        runner.repo_digest_overrides[references["deeptutor"]] = [
            "registry.example/deeptutor@sha256:" + "f" * 64
        ]
    elif case == "shared-image-ref-inconsistent":
        runner.by_id["container-teaching-migrate"]["Image"] = "sha256:other-local-image"
    elif case == "wrong-project-label":
        labels["com.docker.compose.project"] = "attacker"
    elif case == "unhealthy":
        health = state["Health"]
        assert isinstance(health, dict)
        health["Status"] = "unhealthy"
    elif case == "restarting":
        state["Restarting"] = True
    else:
        migrate_state = runner.by_id["container-teaching-migrate"]["State"]
        assert isinstance(migrate_state, dict)
        migrate_state["ExitCode"] = 9

    with pytest.raises(ValueError):
        _produce(tmp_path, runner)

    assert not (tmp_path / "bundle" / "runtime" / "runtime-attestation.json").exists()


def test_attestation_rejects_snapshot_drift(tmp_path: Path) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runner = FakeDocker(references)
    runner.after_by_id = {
        container_id: json.loads(json.dumps(container))
        for container_id, container in runner.by_id.items()
    }
    state = runner.after_by_id["container-deeptutor"]["State"]
    assert isinstance(state, dict)
    health = state["Health"]
    assert isinstance(health, dict)
    health["Status"] = "unhealthy"

    with pytest.raises(ValueError, match="snapshot"):
        _produce(tmp_path, runner)

    assert not (tmp_path / "bundle" / "runtime" / "runtime-attestation.json").exists()


def test_attestation_rejects_nonzero_native_exit(tmp_path: Path) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runner = FakeDocker(references)
    runner.fail_prefix = ["image", "inspect"]

    with pytest.raises(ValueError, match="native"):
        _produce(tmp_path, runner)

    assert not (tmp_path / "bundle" / "runtime" / "runtime-attestation.json").exists()
    assert list((tmp_path / "bundle" / "runtime").glob(".docker-config-*")) == []


@pytest.mark.parametrize(
    "case",
    (
        "extra-candidate-field",
        "wrong-source-repository",
        "zero-candidate-digest",
        "candidate-image-digest-mismatch",
        "missing-required-image",
        "wrong-fixed-image-tag",
    ),
)
def test_attestation_rejects_invalid_schema_v2_candidate_lock(tmp_path: Path, case: str) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    lock_path = candidate_root / "deploy" / "image-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if case == "extra-candidate-field":
        lock["candidate"]["attackerControlled"] = True
    elif case == "wrong-source-repository":
        lock["candidate"]["sourceRepository"] = "attacker/DeepTutor"
    elif case == "zero-candidate-digest":
        lock["candidate"]["imageDigests"]["deeptutor"] = "sha256:" + "0" * 64
    elif case == "candidate-image-digest-mismatch":
        lock["candidate"]["imageDigests"]["deeptutor"] = "sha256:" + "f" * 64
    elif case == "missing-required-image":
        lock["images"].pop("nginx")
    else:
        postgres = lock["images"]["postgres"]
        postgres["tag"] = "latest"
        postgres["reference"] = f"{postgres['repository']}:{postgres['tag']}@{postgres['digest']}"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    runner = FakeDocker(references)

    with pytest.raises(ValueError, match="candidate|image lock"):
        _produce(tmp_path, runner)

    assert runner.calls == []
    assert not (tmp_path / "bundle" / "runtime" / "runtime-attestation.json").exists()


@pytest.mark.parametrize("link_location", ("root", "ancestor"))
def test_producer_rejects_candidate_root_or_ancestor_link(
    tmp_path: Path,
    link_location: str,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real_candidate, references = _candidate_root(real_parent)
    if link_location == "root":
        candidate_argument = tmp_path / "linked-candidate"
        link_target = real_candidate
    else:
        linked_parent = tmp_path / "linked-parent"
        candidate_argument = linked_parent / "candidate"
        link_target = real_parent
    try:
        if link_location == "root":
            candidate_argument.symlink_to(link_target, target_is_directory=True)
        else:
            candidate_argument.parent.symlink_to(link_target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this test host")
    docker = tmp_path / "trusted" / "docker.exe"
    docker.parent.mkdir()
    docker.write_bytes(b"docker")
    runner = FakeDocker(references)
    module = _load_module()

    with pytest.raises(ValueError, match="candidate|symlink|junction|reparse|lease"):
        module.produce_runtime_attestation(
            candidate_root=candidate_argument,
            bundle_root=tmp_path / "bundle",
            release_run=RELEASE_RUN,
            observed_at="2026-08-25T00:00:00Z",
            base_url="https://candidate.example.test",
            runner=runner,
            docker_resolver=lambda: docker,
            environ={"SystemRoot": "C:/Windows"},
        )

    assert runner.calls == []
    assert not (tmp_path / "bundle" / "runtime" / "runtime-attestation.json").exists()


@pytest.mark.parametrize("link_location", ("root", "ancestor"))
def test_verifier_rejects_candidate_root_or_ancestor_link(
    tmp_path: Path,
    link_location: str,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    candidate_root, references = _candidate_root(real_parent)
    docker = tmp_path / "trusted" / "docker.exe"
    docker.parent.mkdir()
    docker.write_bytes(b"docker")
    producer = _load_module()
    producer.produce_runtime_attestation(
        candidate_root=candidate_root,
        bundle_root=tmp_path / "bundle",
        release_run=RELEASE_RUN,
        observed_at="2026-08-25T00:00:00Z",
        base_url="https://candidate.example.test",
        runner=FakeDocker(references),
        docker_resolver=lambda: docker,
        environ={"SystemRoot": "C:/Windows"},
    )
    path = tmp_path / "bundle" / "runtime" / "runtime-attestation.json"
    if link_location == "root":
        linked_candidate = tmp_path / "linked-candidate"
        link = linked_candidate
        target = candidate_root
    else:
        linked_parent = tmp_path / "linked-parent"
        linked_candidate = linked_parent / "candidate"
        link = linked_parent
        target = real_parent
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this test host")
    verifier = _load_verifier()

    with pytest.raises(ValueError, match="candidate|symlink|junction|reparse|lease"):
        verifier.validate_runtime_attestation(
            path,
            bundle_root=tmp_path / "bundle",
            candidate_root=linked_candidate,
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            expected_base_url="https://candidate.example.test",
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX nonblocking contract opens")
@pytest.mark.parametrize("contract_file", ("lock", "compose"))
def test_producer_rejects_candidate_contract_fifo_without_running_docker(
    tmp_path: Path,
    contract_file: str,
) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    contract_path = (
        candidate_root / "deploy" / "image-lock.json"
        if contract_file == "lock"
        else candidate_root / "docker-compose.platform.yml"
    )
    contract_path.rename(contract_path.with_name(f"{contract_path.name}.real"))
    os.mkfifo(contract_path)
    runner = FakeDocker(references)

    with pytest.raises(ValueError, match="candidate|contract|regular|lease"):
        _produce(tmp_path, runner)

    assert runner.calls == []
    assert not (tmp_path / "bundle" / "runtime" / "runtime-attestation.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX nonblocking canonical opens")
def test_producer_rejects_canonical_fifo_without_leaving_staging(
    tmp_path: Path,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    os.mkfifo(canonical)

    with pytest.raises(ValueError, match="canonical|regular|plain"):
        _produce(tmp_path, FakeDocker(references))

    assert stat.S_ISFIFO(canonical.lstat().st_mode)
    assert list(runtime_root.glob(".runtime-attestation.json.*")) == []
    assert list(runtime_root.glob(".docker-config-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX nonblocking artifact opens")
def test_verifier_rejects_runtime_attestation_fifo_without_blocking(tmp_path: Path) -> None:
    candidate_root, _references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    attestation = runtime_root / "runtime-attestation.json"
    os.mkfifo(attestation)
    verifier = _load_verifier()

    with pytest.raises(ValueError, match="runtime attestation|fixed boundary|regular"):
        verifier.validate_runtime_attestation(
            attestation,
            bundle_root=tmp_path / "bundle",
            candidate_root=candidate_root,
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            expected_base_url="https://candidate.example.test",
            expected_sha256="0" * 64,
        )


@pytest.mark.parametrize("contract_file", ("lock", "compose"))
def test_attestation_rejects_candidate_contract_byte_drift_without_overwriting_canonical(
    tmp_path: Path, contract_file: str
) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    runner = FakeDocker(references)
    path = (
        candidate_root / "deploy" / "image-lock.json"
        if contract_file == "lock"
        else candidate_root / "docker-compose.platform.yml"
    )
    original_contract = path.read_bytes()
    mutation_attempted = False
    mutation_blocked = False

    def mutate(call_count: int) -> None:
        nonlocal mutation_attempted, mutation_blocked
        if call_count == 2:
            mutation_attempted = True
            try:
                path.write_bytes(original_contract + b"\n")
            except OSError as exc:
                mutation_blocked = True
                raise ValueError("candidate contract mutation was blocked") from exc

    runner.on_call = mutate
    canonical = tmp_path / "bundle" / "runtime" / "runtime-attestation.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"existing canonical")

    with pytest.raises(ValueError, match="changed|candidate contract"):
        _produce(tmp_path, runner)

    assert mutation_attempted
    if os.name == "nt":
        assert mutation_blocked
        assert path.read_bytes() == original_contract
    else:
        assert not mutation_blocked
        assert path.read_bytes() == original_contract + b"\n"
    assert canonical.read_bytes() == b"existing canonical"


def test_attestation_rejects_symlink_runtime_without_touching_external_directory(
    tmp_path: Path,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "runtime-attestation.json"
    sentinel.write_bytes(b"external sentinel")
    try:
        (bundle_root / "runtime").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this test host")
    runner = FakeDocker(references)

    with pytest.raises(ValueError, match="symlink|junction|reparse|boundary"):
        _produce(tmp_path, runner)

    assert runner.calls == []
    assert sentinel.read_bytes() == b"external sentinel"
    assert sorted(path.name for path in external.iterdir()) == [sentinel.name]


def test_attestation_rejects_runtime_reparse_boundary_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    module = _load_module()
    original = module._is_link_or_reparse
    monkeypatch.setattr(
        module,
        "_is_link_or_reparse",
        lambda path: Path(path) == runtime_root or original(Path(path)),
    )
    runner = FakeDocker(references)

    with pytest.raises(ValueError, match="symlink|junction|reparse|boundary"):
        _produce(tmp_path, runner, module=module)

    assert runner.calls == []


@pytest.mark.parametrize("prior_canonical", (False, True))
def test_attestation_replace_boundary_race_cannot_publish_in_moved_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_canonical: bool
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    bundle_root = tmp_path / "bundle"
    runtime_root = bundle_root / "runtime"
    runtime_root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    canonical = runtime_root / "runtime-attestation.json"
    if prior_canonical:
        canonical.write_bytes(b"external sentinel")
    module = _load_module()
    real_replace = module._RuntimeDirectoryGuard.replace
    swapped = False

    def replace_after_boundary_swap(guard, source: str, target: str) -> None:
        nonlocal swapped
        if target != canonical.name or swapped:
            real_replace(guard, source, target)
            return
        swapped = True
        staged_body, staged_identity = guard.read_optional_regular_file(source)
        assert staged_body is not None and staged_identity is not None
        moved_runtime = external / "runtime"
        runtime_root.rename(moved_runtime)
        runtime_root.symlink_to(moved_runtime, target_is_directory=True)
        real_replace(guard, source, target)

    monkeypatch.setattr(module._RuntimeDirectoryGuard, "replace", replace_after_boundary_swap)

    with pytest.raises((OSError, ValueError)):
        _produce(tmp_path, FakeDocker(references), module=module)

    moved_canonical = external / "runtime" / canonical.name
    assert swapped
    if prior_canonical:
        retained_canonical = moved_canonical if moved_canonical.exists() else canonical
        assert retained_canonical.read_bytes() == b"external sentinel"
    else:
        assert not moved_canonical.exists()
        assert not canonical.exists()
    runtime_after_race = external / "runtime" if (external / "runtime").exists() else runtime_root
    assert list(runtime_after_race.glob(".docker-config-*")) == []


@pytest.mark.parametrize("prior_canonical", (False, True))
@pytest.mark.parametrize("contract_file", ("lock", "compose"))
def test_attestation_publish_time_contract_drift_rolls_back_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_canonical: bool,
    contract_file: str,
) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    if prior_canonical:
        canonical.write_bytes(b"existing canonical")
    contract_path = (
        candidate_root / "deploy" / "image-lock.json"
        if contract_file == "lock"
        else candidate_root / "docker-compose.platform.yml"
    )
    module = _load_module()
    real_replace = module._RuntimeDirectoryGuard.replace
    original_contract = contract_path.read_bytes()
    mutation_attempted = False
    mutation_blocked = False

    def replace_and_mutate_contract(guard, source: str, target: str) -> None:
        nonlocal mutation_attempted, mutation_blocked
        target_name = Path(target).name
        if target_name == canonical.name and not mutation_attempted:
            mutation_attempted = True
            try:
                contract_path.write_bytes(original_contract + b"\n")
            except OSError as exc:
                mutation_blocked = True
                raise ValueError("candidate contract mutation was blocked") from exc
        real_replace(guard, source, target)

    monkeypatch.setattr(module._RuntimeDirectoryGuard, "replace", replace_and_mutate_contract)

    with pytest.raises(ValueError, match="contract|changed"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert mutation_attempted
    if os.name == "nt":
        assert mutation_blocked
        assert contract_path.read_bytes() == original_contract
    else:
        assert not mutation_blocked
        assert contract_path.read_bytes() == original_contract + b"\n"
    if prior_canonical:
        assert canonical.read_bytes() == b"existing canonical"
    else:
        assert not canonical.exists()
    assert list(runtime_root.glob(".docker-config-*")) == []


@pytest.mark.parametrize("prior_canonical", (False, True))
def test_candidate_change_after_published_target_read_still_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_canonical: bool
) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    if prior_canonical:
        canonical.write_bytes(b"existing canonical")
    module = _load_module()
    real_read = module._RuntimeDirectoryGuard.read_optional_regular_file
    real_assert = module._CandidateContractLease.assert_unchanged
    published_target_observed = False
    injected_postcheck_failure = False

    def observe_published_target(guard, name: str):
        nonlocal published_target_observed
        result = real_read(guard, name)
        body, _identity = result
        if name == canonical.name and isinstance(body, bytes) and b'"schemaVersion": 1' in body:
            published_target_observed = True
        return result

    def fail_candidate_guard_after_publish(lease) -> None:
        nonlocal injected_postcheck_failure
        real_assert(lease)
        if published_target_observed:
            injected_postcheck_failure = True
            raise ValueError("candidate contract changed during publication")

    monkeypatch.setattr(
        module._RuntimeDirectoryGuard,
        "read_optional_regular_file",
        observe_published_target,
    )
    monkeypatch.setattr(
        module._CandidateContractLease,
        "assert_unchanged",
        fail_candidate_guard_after_publish,
    )

    with pytest.raises(ValueError, match="candidate contract changed"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert published_target_observed
    assert injected_postcheck_failure
    if prior_canonical:
        assert canonical.read_bytes() == b"existing canonical"
    else:
        assert not canonical.exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX commit cleanup transaction")
@pytest.mark.parametrize("prior_canonical", (False, True))
def test_posix_candidate_change_during_commit_cleanup_still_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_canonical: bool
) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    if prior_canonical:
        canonical.write_bytes(b"existing canonical")
    compose_path = candidate_root / "docker-compose.platform.yml"
    module = _load_module()
    real_remove = module._RuntimeDirectoryGuard.remove_file_if_identity
    mutation_injected = False

    def mutate_during_commit_cleanup(guard, name: str, identity: tuple[int, int]) -> bool:
        nonlocal mutation_injected
        trigger = name.endswith(".rollback") if prior_canonical else name.endswith(".tmp")
        if trigger and not mutation_injected:
            mutation_injected = True
            compose_path.write_bytes(compose_path.read_bytes() + b"\n")
        return real_remove(guard, name, identity)

    monkeypatch.setattr(
        module._RuntimeDirectoryGuard,
        "remove_file_if_identity",
        mutate_during_commit_cleanup,
    )

    with pytest.raises(ValueError, match="candidate|contract|changed"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert mutation_injected
    if prior_canonical:
        assert canonical.read_bytes() == b"existing canonical"
    else:
        assert not canonical.exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX candidate path re-open binding")
@pytest.mark.parametrize("contract_file", ("lock", "compose"))
def test_posix_same_byte_contract_replacement_during_final_assert_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_file: str,
) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    canonical.write_bytes(b"existing canonical")
    contract_path = (
        candidate_root / "deploy" / "image-lock.json"
        if contract_file == "lock"
        else candidate_root / "docker-compose.platform.yml"
    )
    recovery = contract_path.with_name(f"{contract_path.name}.identity-recovery")
    contract_body = contract_path.read_bytes()
    module = _load_module()
    real_assert = module._CandidateContractLease.assert_unchanged
    real_read = module._read_posix_file_descriptor
    final_assert_active = False
    published_asserts = 0
    matching_reads = 0
    replaced = False

    def track_final_assert(lease) -> None:
        nonlocal final_assert_active, published_asserts
        try:
            published = canonical.read_bytes()
        except OSError:
            published = b""
        if b'"schemaVersion": 1' in published:
            published_asserts += 1
        final_assert_active = published_asserts == 3
        try:
            real_assert(lease)
        finally:
            final_assert_active = False

    def replace_after_current_read(file_descriptor: int) -> bytes:
        nonlocal matching_reads, replaced
        body = real_read(file_descriptor)
        if final_assert_active and body == contract_body and not replaced:
            matching_reads += 1
            if matching_reads == 2:
                contract_path.rename(recovery)
                contract_path.write_bytes(contract_body)
                replaced = True
        return body

    monkeypatch.setattr(
        module._CandidateContractLease,
        "assert_unchanged",
        track_final_assert,
    )
    monkeypatch.setattr(module, "_read_posix_file_descriptor", replace_after_current_read)

    with pytest.raises(ValueError, match="candidate|contract|lease|changed"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert replaced
    assert published_asserts == 3
    assert canonical.read_bytes() == b"existing canonical"
    assert contract_path.read_bytes() == contract_body
    assert recovery.read_bytes() == contract_body


def test_published_target_same_name_swap_never_accepts_or_deletes_attacker_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    displaced = tmp_path / "attacker" / "published-recovery.json"
    displaced.parent.mkdir()
    malicious = b"attacker same-name target"
    module = _load_module()
    real_replace = module._RuntimeDirectoryGuard.replace
    attack_attempted = False

    def replace_then_swap_target(guard, source: str, target: str) -> None:
        nonlocal attack_attempted
        real_replace(guard, source, target)
        if target == canonical.name and not attack_attempted:
            attack_attempted = True
            canonical.rename(displaced)
            canonical.write_bytes(malicious)

    monkeypatch.setattr(module._RuntimeDirectoryGuard, "replace", replace_then_swap_target)

    with pytest.raises((OSError, ValueError)):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert attack_attempted
    if canonical.exists():
        assert canonical.read_bytes() == malicious


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows no-replace publication")
@pytest.mark.parametrize("prior_canonical", (False, True))
def test_pre_replace_same_name_collision_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_canonical: bool,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    original = b"existing canonical"
    if prior_canonical:
        canonical.write_bytes(original)
    attacker_recovery = tmp_path / "attacker" / "original.json"
    attacker_recovery.parent.mkdir()
    malicious = b"pre-replace attacker target"
    module = _load_module()
    real_replace = module._RuntimeDirectoryGuard.replace
    attack_injected = False

    def collide_before_stage_publish(guard, source: str, target: str) -> None:
        nonlocal attack_injected
        if target == canonical.name and source.endswith(".tmp") and not attack_injected:
            attack_injected = True
            if canonical.exists():
                canonical.rename(attacker_recovery)
            canonical.write_bytes(malicious)
        real_replace(guard, source, target)

    monkeypatch.setattr(
        module._RuntimeDirectoryGuard,
        "replace",
        collide_before_stage_publish,
    )

    with pytest.raises((OSError, ValueError), match="publish|replace|rollback|canonical"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert attack_injected
    assert canonical.read_bytes() == malicious
    if attacker_recovery.exists():
        assert attacker_recovery.read_bytes() == original
    owned_recovery = list(runtime_root.glob(".runtime-attestation.json.*.original-recovery"))
    if prior_canonical:
        assert len(owned_recovery) == 1
        assert owned_recovery[0].read_bytes() == original
    else:
        assert owned_recovery == []


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX no-replace publication")
@pytest.mark.parametrize("prior_canonical", (False, True))
def test_posix_pre_replace_same_name_collision_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_canonical: bool,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    original = b"existing canonical"
    if prior_canonical:
        canonical.write_bytes(original)
    attacker_recovery = tmp_path / "attacker" / "original.json"
    attacker_recovery.parent.mkdir()
    malicious = b"POSIX pre-replace attacker target"
    module = _load_module()
    real_replace = module._RuntimeDirectoryGuard.replace
    attack_injected = False

    def collide_before_stage_publish(guard, source: str, target: str) -> None:
        nonlocal attack_injected
        if target == canonical.name and source.endswith(".tmp") and not attack_injected:
            attack_injected = True
            if canonical.exists():
                canonical.rename(attacker_recovery)
            canonical.write_bytes(malicious)
        real_replace(guard, source, target)

    monkeypatch.setattr(
        module._RuntimeDirectoryGuard,
        "replace",
        collide_before_stage_publish,
    )

    with pytest.raises((OSError, ValueError), match="publish|replace|rollback|canonical|recovery"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert attack_injected
    assert canonical.read_bytes() == malicious
    if attacker_recovery.exists():
        assert attacker_recovery.read_bytes() == original


@pytest.mark.parametrize("prior_canonical", (False, True))
def test_rename_then_keyboard_interrupt_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_canonical: bool,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    if prior_canonical:
        canonical.write_bytes(b"existing canonical")
    module = _load_module()
    real_replace = module._RuntimeDirectoryGuard.replace
    interrupted = False

    def interrupt_after_rename(guard, source: str, target: str) -> None:
        nonlocal interrupted
        real_replace(guard, source, target)
        if target == canonical.name and source.endswith(".tmp") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected after runtime rename")

    monkeypatch.setattr(
        module._RuntimeDirectoryGuard,
        "replace",
        interrupt_after_rename,
    )

    with pytest.raises(KeyboardInterrupt, match="injected after runtime rename"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert interrupted
    if prior_canonical:
        assert canonical.read_bytes() == b"existing canonical"
    else:
        assert not canonical.exists()
    assert list(runtime_root.glob(".runtime-attestation.json.*.tmp")) == []
    assert list(runtime_root.glob(".runtime-attestation.json.*.rollback")) == []
    assert list(runtime_root.glob(".runtime-attestation.json.*.original-recovery")) == []


def test_stage_registered_before_initialization_interrupt_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    canonical.write_bytes(b"existing canonical")
    module = _load_module()
    real_write = module._RuntimeDirectoryGuard.write_new_file
    interrupted = False

    def interrupt_after_stage_create(guard, name: str, body: bytes) -> tuple[int, int]:
        nonlocal interrupted
        identity = real_write(guard, name, body)
        if name.endswith(".tmp") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected after stage initialization")
        return identity

    monkeypatch.setattr(
        module._RuntimeDirectoryGuard,
        "write_new_file",
        interrupt_after_stage_create,
    )

    with pytest.raises(KeyboardInterrupt, match="injected after stage initialization"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert interrupted
    assert canonical.read_bytes() == b"existing canonical"
    assert list(runtime_root.glob(".runtime-attestation.json.*")) == []
    assert list(runtime_root.glob(".docker-config-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows staging handle cleanup")
def test_windows_staging_identity_failure_disposes_created_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    bundle_root, runtime_root = module._prepare_runtime_root(tmp_path / "bundle")
    guard = module._RuntimeDirectoryGuard.open(bundle_root, runtime_root)
    staging_name = ".runtime-attestation.json.identity-failure.tmp"

    def fail_identity(_handle: object, *, directory: bool) -> tuple[int, int]:
        assert not directory
        raise OSError("injected staging identity failure")

    monkeypatch.setattr(module, "_windows_handle_identity", fail_identity)
    try:
        with pytest.raises(OSError, match="injected staging identity failure"):
            module._create_windows_staging_file(
                guard._runtime_handle,
                staging_name,
                b"staged body",
            )
    finally:
        guard.close()

    assert not (runtime_root / staging_name).exists()


@pytest.mark.parametrize("canonical_kind", ("directory", "symlink"))
def test_preexisting_nonregular_canonical_is_not_touched_and_leaves_no_stage(
    tmp_path: Path,
    canonical_kind: str,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    external = tmp_path / "external-canonical.json"
    external.write_bytes(b"external sentinel")
    if canonical_kind == "directory":
        canonical.mkdir()
    else:
        try:
            canonical.symlink_to(external)
        except OSError:
            pytest.skip("symlinks are unavailable on this test host")

    with pytest.raises(ValueError, match="canonical|plain|regular|reparse"):
        _produce(tmp_path, FakeDocker(references))

    if canonical_kind == "directory":
        assert canonical.is_dir()
    else:
        assert canonical.is_symlink()
    assert external.read_bytes() == b"external sentinel"
    assert list(runtime_root.glob(".runtime-attestation.json.*.tmp")) == []
    assert list(runtime_root.glob(".docker-config-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX staging initialization cleanup")
def test_posix_staging_fsync_failure_removes_only_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    canonical.write_bytes(b"existing canonical")
    module = _load_module()
    real_fsync = module.os.fsync
    injected = False

    def fail_regular_file_fsync(file_descriptor: int) -> None:
        nonlocal injected
        if stat.S_ISREG(os.fstat(file_descriptor).st_mode) and not injected:
            injected = True
            raise OSError("injected staging fsync failure")
        real_fsync(file_descriptor)

    monkeypatch.setattr(module.os, "fsync", fail_regular_file_fsync)

    with pytest.raises(OSError, match="injected staging fsync failure"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert injected
    assert canonical.read_bytes() == b"existing canonical"
    assert list(runtime_root.glob(".runtime-attestation.json.*")) == []
    assert list(runtime_root.glob(".docker-config-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX staging identity cleanup")
def test_posix_staging_identity_failure_removes_only_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate, references = _candidate_root(tmp_path)
    runtime_root = tmp_path / "bundle" / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    canonical.write_bytes(b"existing canonical")
    module = _load_module()
    real_write = module._RuntimeDirectoryGuard.write_new_file
    real_fstat = module.os.fstat
    staging_write_active = False
    injected = False

    def mark_staging_write(guard, name: str, body: bytes) -> tuple[int, int]:
        nonlocal staging_write_active
        staging_write_active = True
        try:
            return real_write(guard, name, body)
        finally:
            staging_write_active = False

    def fail_staging_fstat(file_descriptor: int):
        nonlocal injected
        details = real_fstat(file_descriptor)
        if staging_write_active and stat.S_ISREG(details.st_mode) and not injected:
            injected = True
            raise OSError("injected staging identity failure")
        return details

    monkeypatch.setattr(module._RuntimeDirectoryGuard, "write_new_file", mark_staging_write)
    monkeypatch.setattr(module.os, "fstat", fail_staging_fstat)

    with pytest.raises(OSError, match="injected staging identity failure"):
        _produce(tmp_path, FakeDocker(references), module=module)

    assert injected
    assert canonical.read_bytes() == b"existing canonical"
    assert list(runtime_root.glob(".runtime-attestation.json.*")) == []
    assert list(runtime_root.glob(".docker-config-*")) == []


@pytest.mark.parametrize(
    "forbidden_option",
    ("--command", "--project", "--service", "--image", "--check", "--pass"),
)
def test_cli_has_no_runtime_or_result_override(forbidden_option: str) -> None:
    module = _load_module()
    with pytest.raises(SystemExit):
        module._parse_args(
            [
                "--candidate-root",
                str(ROOT),
                "--bundle-root",
                str(ROOT),
                "--run-id",
                "run-1",
                "--environment-id",
                "env-1",
                "--observed-at",
                "2026-08-25T00:00:00Z",
                "--base-url",
                "https://candidate.example.test",
                forbidden_option,
                "attacker",
            ]
        )


def test_fixed_docker_resolver_rejects_symlink_boundary(tmp_path: Path) -> None:
    module = _load_module()
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    real = trusted / "docker-real.exe"
    real.write_bytes(b"docker")
    linked = trusted / "docker.exe"
    try:
        linked.symlink_to(real)
    except OSError:
        pytest.skip("symlinks are unavailable on this Windows test host")

    with pytest.raises(ValueError, match="trusted Docker"):
        module.resolve_fixed_docker(platform="win32", trusted_paths=(linked,))


def test_shared_verifier_recomputes_runtime_attestation_from_raw_facts(
    tmp_path: Path,
) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    path = _produce(tmp_path, FakeDocker(references))
    verifier = _load_verifier()

    document = verifier.validate_runtime_attestation(
        path,
        bundle_root=tmp_path / "bundle",
        candidate_root=candidate_root,
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        expected_base_url="https://candidate.example.test",
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    assert document["candidate"] == CANDIDATE
    assert document["beforeSnapshot"] == document["afterSnapshot"]


@pytest.mark.parametrize("swap", ("file", "runtime-ancestor"))
def test_verifier_rejects_same_body_file_or_ancestor_swap_during_handle_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    verifier = _load_verifier()
    bundle_root = tmp_path / "bundle"
    runtime_root = bundle_root / "runtime"
    runtime_root.mkdir(parents=True)
    canonical = runtime_root / "runtime-attestation.json"
    canonical.write_bytes(b"trusted artifact")
    external = tmp_path / "external"
    external.mkdir()
    attacker = external / canonical.name
    attacker.write_bytes(b"trusted artifact")
    original_read = verifier._read_runtime_artifact_handle
    swapped = False

    def read_after_swap(handle, *, windows: bool) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            if swap == "file":
                canonical.rename(runtime_root / "trusted-recovery.json")
                canonical.symlink_to(attacker)
            else:
                runtime_root.rename(bundle_root / "runtime-recovery")
                runtime_root.symlink_to(external, target_is_directory=True)
        return original_read(handle, windows=windows)

    monkeypatch.setattr(verifier, "_read_runtime_artifact_handle", read_after_swap)

    with pytest.raises(ValueError, match="runtime attestation"):
        verifier._runtime_artifact_body(
            canonical,
            bundle_root=bundle_root,
            expected_sha256=hashlib.sha256(b"trusted artifact").hexdigest(),
        )

    assert swapped


@pytest.mark.parametrize(
    "case",
    (
        "candidate",
        "release-run",
        "base-url",
        "config-image",
        "repo-digest",
        "unhealthy",
        "snapshot",
        "command",
        "native-exit",
        "fictional-hash",
        "raw-mutation",
        "rehashed-raw-fact",
        "shared-image-ref-inconsistent",
    ),
)
def test_shared_verifier_rejects_attestation_fact_tampering(tmp_path: Path, case: str) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    path = _produce(tmp_path, FakeDocker(references))
    document = json.loads(path.read_text(encoding="utf-8"))
    containers = document["containers"]
    assert isinstance(containers, list)
    first = containers[0]
    assert isinstance(first, dict)
    if case == "candidate":
        candidate = document["candidate"]
        assert isinstance(candidate, dict)
        candidate["sourceHead"] = "b" * 40
    elif case == "release-run":
        release_run = document["releaseRun"]
        assert isinstance(release_run, dict)
        release_run["runId"] = "attacker-run"
    elif case == "base-url":
        document["baseUrl"] = "https://attacker.example.test"
    elif case == "config-image":
        first["configImage"] = references["postgres"]
    elif case == "repo-digest":
        first["repoDigests"] = ["registry.example/attacker@sha256:" + "f" * 64]
    elif case == "unhealthy":
        first["health"] = "unhealthy"
    elif case == "snapshot":
        before = document["beforeSnapshot"]
        assert isinstance(before, list) and isinstance(before[0], dict)
        before[0]["exitCode"] = 7
    elif case == "command":
        commands = document["commands"]
        assert isinstance(commands, list) and isinstance(commands[0], dict)
        commands[0]["argv"] = ["docker", "build", "."]
    elif case == "native-exit":
        commands = document["commands"]
        assert isinstance(commands, list) and isinstance(commands[0], dict)
        commands[0]["nativeExit"] = 1
    elif case == "fictional-hash":
        commands = document["commands"]
        assert isinstance(commands, list) and isinstance(commands[0], dict)
        commands[0]["stdoutSha256"] = "b" * 64
    elif case == "raw-mutation":
        commands = document["commands"]
        assert isinstance(commands, list) and isinstance(commands[0], dict)
        commands[0]["stdout"] += "\n"
    elif case == "rehashed-raw-fact":
        commands = document["commands"]
        assert isinstance(commands, list)
        record = next(
            command
            for command in commands
            if isinstance(command, dict)
            and isinstance(command.get("argv"), list)
            and "container" in command["argv"]
        )
        raw_fact = json.loads(record["stdout"])
        raw_fact["project"] = "attacker"
        record["stdout"] = json.dumps(raw_fact)
        record["stdoutSha256"] = hashlib.sha256(record["stdout"].encode()).hexdigest()
    else:
        commands = document["commands"]
        assert isinstance(commands, list)
        for record in commands:
            if not isinstance(record, dict) or not isinstance(record.get("stdout"), str):
                continue
            try:
                raw_fact = json.loads(record["stdout"])
            except json.JSONDecodeError:
                continue
            if (
                isinstance(raw_fact, dict)
                and raw_fact.get("containerId") == "container-teaching-migrate"
            ):
                raw_fact["localImageId"] = "sha256:other-local-image"
                record["stdout"] = json.dumps(raw_fact)
                record["stdoutSha256"] = hashlib.sha256(record["stdout"].encode()).hexdigest()
        shared = next(
            container
            for container in containers
            if isinstance(container, dict)
            and container.get("containerId") == "container-teaching-migrate"
        )
        shared["localImageId"] = "sha256:other-local-image"
    path.write_text(json.dumps(document), encoding="utf-8")
    verifier = _load_verifier()

    with pytest.raises(ValueError):
        verifier.validate_runtime_attestation(
            path,
            bundle_root=tmp_path / "bundle",
            candidate_root=candidate_root,
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            expected_base_url="https://candidate.example.test",
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize("case", ("missing", "digest", "symlink"))
def test_shared_verifier_rejects_missing_tampered_or_symlink_attestation(
    tmp_path: Path, case: str
) -> None:
    candidate_root, references = _candidate_root(tmp_path)
    path = _produce(tmp_path, FakeDocker(references))
    expected_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if case == "missing":
        path = path.parent / "missing.json"
    elif case == "digest":
        expected_sha256 = "0" * 64
    else:
        linked = path.parent / "linked.json"
        try:
            linked.symlink_to(path)
        except OSError:
            pytest.skip("symlinks are unavailable on this Windows test host")
        path = linked
    verifier = _load_verifier()

    with pytest.raises(ValueError, match="runtime attestation"):
        verifier.validate_runtime_attestation(
            path,
            bundle_root=tmp_path / "bundle",
            candidate_root=candidate_root,
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            expected_base_url="https://candidate.example.test",
            expected_sha256=expected_sha256,
        )
