from __future__ import annotations

from dataclasses import dataclass, fields, replace
from functools import cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from deeptutor.teaching.secret_permissions import restrict_secret_file

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = "postgres:16@sha256:" + "1" * 64
MINIO_IMAGE = "minio/minio:release@sha256:" + "2" * 64
CANDIDATE = {
    "sourceRepository": "xinlingzhifei/DeepTutor",
    "sourceHead": "3" * 40,
    "releaseTag": "yfeistai-first-release-20260831-33333333",
    "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
    "imageDigests": {
        "deeptutor": "sha256:" + "4" * 64,
        "openmaic": "sha256:" + "5" * 64,
        "openmaic_render": "sha256:" + "6" * 64,
    },
}
RELEASE_RUN = {
    "runId": "backup-restore-run-01",
    "environmentId": "first-release-server-01",
}
SECRET_VALUES = {
    "platform_database_app_password": "app-secret-7f2ef478",
    "platform_database_migration_password": "migration-secret-70299f4e",
    "minio_bootstrap_access_key": "object-access-bd05c0d1",
    "minio_bootstrap_secret_key": "object-secret-cc56e706",
}


@cache
def _module():
    path = ROOT / "scripts" / "provision_backup_restore_target.py"
    spec = importlib.util.spec_from_file_location(
        "provision_backup_restore_target_under_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_secrets(root: Path) -> None:
    root.mkdir()
    if os.name != "nt":
        root.chmod(0o700)
    for name, value in SECRET_VALUES.items():
        target = root / name
        target.write_text(value + "\n", encoding="utf-8")
        restrict_secret_file(target)


@dataclass(frozen=True)
class _Call:
    argv: tuple[str, ...]
    input: bytes | None
    timeout: float | None
    environment: dict[str, str]
    check: bool | None
    capture_output: bool | None
    shell: bool | None


class _RecordingRunner:
    def __init__(
        self,
        *,
        existing_network: str | None = None,
        fail_minio_start_with: bytes | None = None,
        volume_driver: str = "local",
    ) -> None:
        self.calls: list[_Call] = []
        self.existing_network = existing_network
        self.fail_minio_start_with = fail_minio_start_with
        self.volume_driver = volume_driver
        self.volume_labels: dict[str, dict[str, str]] = {}

    def __call__(
        self,
        arguments: list[str] | tuple[str, ...],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        check: bool | None = None,
        capture_output: bool | None = None,
        shell: bool | None = None,
        **_options: object,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = tuple(arguments)
        self.calls.append(
            _Call(
                argv=argv,
                input=input,
                timeout=timeout,
                environment=dict(env or {}),
                check=check,
                capture_output=capture_output,
                shell=shell,
            )
        )
        command = argv[1:]
        if command and command[0] == "info":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=_canonical({"ID": "local-daemon-id", "OSType": "linux"}),
                stderr=b"",
            )
        if command[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if command[:2] == ("network", "ls"):
            stdout = b""
            if self.existing_network is not None:
                stdout = (self.existing_network + "\n").encode()
                self.existing_network = None
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
        if command[:2] == ("volume", "ls"):
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if command[:2] == ("network", "create"):
            return subprocess.CompletedProcess(argv, 0, stdout=b"network-id\n", stderr=b"")
        if command[:2] == ("volume", "create"):
            volume_name = ("a" if not self.volume_labels else "b") * 64
            labels = {
                argv[index + 1].split("=", 1)[0]: argv[index + 1].split("=", 1)[1]
                for index, argument in enumerate(argv)
                if argument == "--label"
            }
            self.volume_labels[volume_name] = labels
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(volume_name + "\n").encode(),
                stderr=b"",
            )
        if command[:2] == ("volume", "inspect"):
            name = argv[-1]
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=_canonical(
                    {
                        "Driver": self.volume_driver,
                        "Labels": self.volume_labels[name],
                        "Name": name,
                        "Options": None,
                        "Scope": "local",
                    }
                ),
                stderr=b"",
            )
        if command and command[0] == "run":
            name = argv[argv.index("--name") + 1]
            if name.endswith("-minio") and self.fail_minio_start_with is not None:
                return subprocess.CompletedProcess(
                    argv,
                    17,
                    stdout=b"",
                    stderr=self.fail_minio_start_with,
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=hashlib.sha256(name.encode()).hexdigest().encode() + b"\n",
                stderr=b"",
            )
        if command and command[0] == "inspect":
            return subprocess.CompletedProcess(argv, 0, stdout=b"healthy\n", stderr=b"")
        if command and command[0] == "exec":
            if "psql" in argv and input is not None:
                return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
            if "psql" in argv:
                database_name = argv[argv.index("--dbname") + 1]
                body = _canonical(
                    {
                        "databaseName": database_name,
                        "databaseOid": "16384",
                        "databaseOwner": "yfeistai_migrator",
                        "currentRole": "yfeistai_migrator",
                        "systemIdentifier": "7557862732681765314",
                        "userObjectCount": 0,
                    }
                )
                return subprocess.CompletedProcess(argv, 0, stdout=body, stderr=b"")
            if "/bin/sh" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected command: {argv!r}")


def _config(tmp_path: Path):
    secret_directory = tmp_path / "target-secrets"
    _write_secrets(secret_directory)
    runtime_staging_root = tmp_path / "runtime-staging"
    runtime_staging_root.mkdir()
    if os.name == "nt":
        restrict_secret_file(runtime_staging_root)
    else:
        runtime_staging_root.chmod(0o700)
    output_parent = tmp_path / "evidence-inputs"
    output_parent.mkdir()
    return _module().TargetProvisioningConfig(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        target_secret_directory=secret_directory,
        runtime_staging_root=runtime_staging_root,
        output_directory=output_parent / "target",
        docker_executable="docker",
        postgres_image=POSTGRES_IMAGE,
        minio_image=MINIO_IMAGE,
        postgres_host_port=15432,
        minio_host_port=19000,
        timeout_seconds=300,
    )


def _object_observer(**arguments: Any):
    assert arguments["access_key"] == SECRET_VALUES["minio_bootstrap_access_key"]
    assert arguments["secret_key"] == SECRET_VALUES["minio_bootstrap_secret_key"]
    assert arguments["endpoint"].startswith("http://127.0.0.1:")
    return _module().ObjectStoreObservation(
        owner_id="minio-owner-id-01",
        versioning_enabled=True,
        object_count=0,
        version_count=0,
        delete_marker_count=0,
    )


def _argument_after(argv: tuple[str, ...], option: str) -> str:
    return argv[argv.index(option) + 1]


def test_provisioner_creates_only_run_owned_resources_and_writes_bound_artifacts(
    tmp_path: Path,
) -> None:
    module = _module()
    runner = _RecordingRunner()
    config = _config(tmp_path)

    result = module.provision_backup_restore_target(
        config,
        runner=runner,
        object_store_observer=_object_observer,
        sleeper=lambda _seconds: None,
    )

    target_config_body = result.target_config_path.read_bytes()
    target_config = json.loads(target_config_body)
    assert target_config_body == _canonical(target_config)
    assert target_config == {
        "database_host": "127.0.0.1",
        "database_name": result.database_name,
        "database_port": 15432,
        "database_url": None,
        "database_user": "yfeistai_migrator",
        "enabled": True,
        "object_store_bucket": result.object_bucket,
        "object_store_endpoint": "http://127.0.0.1:19000",
        "object_store_mode": "s3",
        "object_store_namespace_id": result.object_namespace_id,
        "object_store_region": "us-east-1",
    }

    receipt_body = result.provisioning_receipt_path.read_bytes()
    receipt = json.loads(receipt_body)
    assert receipt_body == _canonical(receipt)
    assert receipt == {
        "schemaVersion": 1,
        "producer": "backup-restore-target-provisioner",
        "candidateSha256": result.candidate_sha256,
        "releaseRun": RELEASE_RUN,
        "resources": {
            "database": {
                "identitySha256": result.database_identity_sha256,
                "ownerRunId": RELEASE_RUN["runId"],
                "disposition": "runner-owned-disposable",
            },
            "objectStore": {
                "identitySha256": result.object_store_identity_sha256,
                "ownerRunId": RELEASE_RUN["runId"],
                "disposition": "runner-owned-disposable",
            },
        },
    }

    all_arguments = [argument for call in runner.calls for argument in call.argv]
    for secret in SECRET_VALUES.values():
        assert all(secret not in argument for argument in all_arguments)
        assert secret not in target_config_body.decode()
        assert secret not in receipt_body.decode()
    assert "--rm" not in all_arguments
    assert "platform-postgres" not in all_arguments
    assert "platform-minio" not in all_arguments
    assert all(call.timeout is not None and call.timeout > 0 for call in runner.calls)
    assert any(result.postgres_volume in call.argv for call in runner.calls)
    assert any(result.minio_volume in call.argv for call in runner.calls)
    assert any(result.network_name in call.argv for call in runner.calls)
    assert result.postgres_volume == "a" * 64
    assert result.minio_volume == "b" * 64
    assert all(
        result.postgres_volume not in call.argv and result.minio_volume not in call.argv
        for call in runner.calls
        if call.argv[1:3] == ("volume", "create")
    )
    assert all(
        call.environment.get("DOCKER_HOST") == "unix:///var/run/docker.sock"
        for call in runner.calls
    )
    assert all(
        call.check is False and call.capture_output is True and call.shell is False
        for call in runner.calls
    )
    assert all(
        str(config.target_secret_directory) not in argument
        for call in runner.calls
        for argument in call.argv
    )
    assert result.staged_secret_directory.is_dir()
    assert result.staged_secret_directory.parent != config.output_directory.parent
    for name, value in SECRET_VALUES.items():
        staged = result.staged_secret_directory / name
        assert staged.read_text(encoding="utf-8") == value + "\n"
        assert not staged.is_symlink()
        assert all(
            value.encode() not in path.read_bytes()
            for path in config.output_directory.parent.rglob("*")
            if path.is_file()
        )
    minio_init = next(call for call in runner.calls if "/bin/sh" in call.argv)
    assert minio_init.input is not None
    assert b"MC_HOST_target" in minio_init.input
    assert b"mc alias set" not in minio_init.input
    assert SECRET_VALUES["minio_bootstrap_access_key"].encode() in minio_init.input
    assert SECRET_VALUES["minio_bootstrap_secret_key"].encode() in minio_init.input
    run_calls = [call for call in runner.calls if call.argv[1] == "run"]
    assert len(run_calls) == 2
    postgres_run = next(
        call for call in run_calls if _argument_after(call.argv, "--name").endswith("-postgres")
    )
    minio_run = next(
        call for call in run_calls if _argument_after(call.argv, "--name").endswith("-minio")
    )
    expected_labels = {
        "com.yfeistai.purpose=backup-restore-target",
        f"com.yfeistai.owner-run={RELEASE_RUN['runId']}",
        f"com.yfeistai.candidate-sha256={result.candidate_sha256}",
    }
    for call, port, image in (
        (postgres_run, "127.0.0.1:15432:5432", config.postgres_image),
        (minio_run, "127.0.0.1:19000:9000", config.minio_image),
    ):
        assert _argument_after(call.argv, "--network") == result.network_name
        assert _argument_after(call.argv, "--publish") == port
        assert image in call.argv
        assert {
            call.argv[index + 1]
            for index, argument in enumerate(call.argv)
            if argument == "--label"
        } == expected_labels
    assert (
        f"type=volume,src={result.postgres_volume},dst=/var/lib/postgresql/data"
        in postgres_run.argv
    )
    assert f"type=volume,src={result.minio_volume},dst=/data" in minio_run.argv
    assert any(
        str(result.staged_secret_directory) in argument
        and "platform_database_migration_password" in argument
        for argument in postgres_run.argv
    )
    for secret_name in ("minio_bootstrap_access_key", "minio_bootstrap_secret_key"):
        assert any(
            str(result.staged_secret_directory) in argument and secret_name in argument
            for argument in minio_run.argv
        )
    assert any(
        call.input and SECRET_VALUES["platform_database_app_password"].encode() in call.input
        for call in runner.calls
    )


def test_provisioner_rejects_existing_resource_before_any_mutation(tmp_path: Path) -> None:
    module = _module()
    config = _config(tmp_path)
    expected = module.target_resource_names(config)
    runner = _RecordingRunner(existing_network=expected.network_name)

    with pytest.raises(ValueError, match="already exists"):
        module.provision_backup_restore_target(
            config,
            runner=runner,
            object_store_observer=lambda **_arguments: pytest.fail("observer must not run"),
            sleeper=lambda _seconds: None,
        )

    assert not [
        call
        for call in runner.calls
        if call.argv[1] in {"exec", "run"} or (len(call.argv) > 2 and call.argv[2] == "create")
    ]
    assert not config.output_directory.exists()
    assert not list(config.runtime_staging_root.iterdir())


def test_provisioner_rejects_unowned_volume_before_container_start(tmp_path: Path) -> None:
    module = _module()
    config = _config(tmp_path)
    runner = _RecordingRunner(volume_driver="nfs")

    with pytest.raises(RuntimeError, match="volume ownership is invalid"):
        module.provision_backup_restore_target(
            config,
            runner=runner,
            object_store_observer=lambda **_arguments: pytest.fail("observer must not run"),
            sleeper=lambda _seconds: None,
        )

    assert not [call for call in runner.calls if call.argv[1] == "run"]
    assert not config.output_directory.exists()


def test_provisioner_rejects_nonlocal_docker_socket_before_secret_use(
    tmp_path: Path,
) -> None:
    module = _module()
    config = replace(_config(tmp_path), docker_host="tcp://attacker.invalid:2375")

    with pytest.raises(ValueError, match="local Linux Docker socket"):
        module.target_resource_names(config)


@pytest.mark.parametrize(
    ("field", "reference"),
    (
        (
            "postgres_image",
            "attacker.invalid/library/postgres:16@sha256:" + "7" * 64,
        ),
        (
            "postgres_image",
            "docker.io/attacker/postgres:16@sha256:" + "7" * 64,
        ),
        (
            "minio_image",
            "attacker.invalid/minio/minio:release@sha256:" + "8" * 64,
        ),
        (
            "minio_image",
            "minio/attacker/minio:release@sha256:" + "8" * 64,
        ),
    ),
)
def test_provisioner_rejects_unapproved_image_repository_before_secret_use(
    tmp_path: Path,
    field: str,
    reference: str,
) -> None:
    module = _module()
    config = replace(_config(tmp_path), **{field: reference})

    with pytest.raises(ValueError, match="image repository is invalid"):
        module.target_resource_names(config)

    assert not config.output_directory.exists()


def test_provisioner_accepts_only_normalized_image_lock_repositories(tmp_path: Path) -> None:
    module = _module()
    config = replace(
        _config(tmp_path),
        postgres_image="docker.io/library/postgres:16@sha256:" + "7" * 64,
        minio_image="docker.io/minio/minio:release@sha256:" + "8" * 64,
    )

    assert module.target_resource_names(config).database_name.startswith("br_")


@pytest.mark.parametrize(
    ("field", "reference"),
    (
        ("postgres_image", "postgres:16@sha256:" + "0" * 64),
        ("minio_image", "minio/minio:release@sha256:" + "0" * 64),
    ),
)
def test_provisioner_rejects_zero_image_digest(
    tmp_path: Path,
    field: str,
    reference: str,
) -> None:
    module = _module()
    config = replace(_config(tmp_path), **{field: reference})

    with pytest.raises(ValueError, match="image digest is invalid"):
        module.target_resource_names(config)


def test_provisioner_requires_disjoint_private_runtime_staging_root(tmp_path: Path) -> None:
    module = _module()
    assert "runtime_staging_root" in {
        field.name for field in fields(module.TargetProvisioningConfig)
    }
    config = _config(tmp_path)

    with pytest.raises(ValueError, match="must be disjoint"):
        module.target_resource_names(
            replace(config, runtime_staging_root=config.output_directory.parent)
        )

    nested_evidence_parent = config.runtime_staging_root / "evidence"
    nested_evidence_parent.mkdir()
    with pytest.raises(ValueError, match="must be disjoint"):
        module.target_resource_names(
            replace(config, output_directory=nested_evidence_parent / "target")
        )

    names = module.target_resource_names(config)
    (config.runtime_staging_root / f".{names.network_name}-secrets").mkdir()
    with pytest.raises(ValueError, match="staging already exists"):
        module.target_resource_names(config)


def test_provisioner_requires_source_secrets_disjoint_from_evidence_tree(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(tmp_path)

    nested_secret_directory = config.output_directory.parent / "source-secrets"
    _write_secrets(nested_secret_directory)
    with pytest.raises(ValueError, match="secret and evidence paths must be disjoint"):
        module.target_resource_names(
            replace(config, target_secret_directory=nested_secret_directory)
        )

    nested_evidence_parent = config.target_secret_directory / "evidence"
    nested_evidence_parent.mkdir()
    with pytest.raises(ValueError, match="secret and evidence paths must be disjoint"):
        module.target_resource_names(
            replace(config, output_directory=nested_evidence_parent / "target")
        )


def test_provisioner_retains_partial_resources_without_receipt_or_cleanup_on_failure(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _config(tmp_path)
    names = module.target_resource_names(config)
    secret = SECRET_VALUES["minio_bootstrap_secret_key"].encode()
    runner = _RecordingRunner(fail_minio_start_with=secret)

    with pytest.raises(RuntimeError) as failure:
        module.provision_backup_restore_target(
            config,
            runner=runner,
            object_store_observer=lambda **_arguments: pytest.fail("observer must not run"),
            sleeper=lambda _seconds: None,
        )

    assert SECRET_VALUES["minio_bootstrap_secret_key"] not in str(failure.value)
    assert any(call.argv[1:3] == ("volume", "create") for call in runner.calls)
    forbidden = {"down", "kill", "prune", "rm", "stop"}
    assert not [
        argument for call in runner.calls for argument in call.argv if argument in forbidden
    ]
    assert not config.output_directory.exists()
    expected_staging = config.runtime_staging_root / f".{names.network_name}-secrets"
    assert expected_staging.is_dir()
    assert not list(config.output_directory.parent.rglob("target-provisioning-receipt.json"))


def test_provisioner_source_has_no_cleanup_or_shell_execution_primitive() -> None:
    source = (ROOT / "scripts" / "provision_backup_restore_target.py").read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert "docker volume rm" not in source
    assert "docker container rm" not in source
    assert "docker system prune" not in source
    assert "docker volume prune" not in source
    assert "docker stop" not in source
    assert "docker kill" not in source
    assert "shutil.rmtree" not in source
    assert "os.remove(" not in source
    assert "mc alias set" not in source
