#!/usr/bin/env python
"""Provision one retained, run-owned backup/restore validation target.

The provisioner deliberately has no teardown path.  A failed or successful run
leaves its uniquely named resources in place for operator inspection.  It never
discovers or reuses platform storage.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from urllib.parse import quote

SCRIPTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_ROOT.parent
for import_root in (PROJECT_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from backup_restore_contract import (  # noqa: E402
    TARGET_PROVISIONING_RECEIPT_PRODUCER,
    canonical_backup_restore_report,
    parse_target_provisioning_receipt,
    physical_object_store_identity_sha256,
    validate_backup_restore_identity,
)

from deeptutor.teaching.secret_permissions import (  # noqa: E402
    restrict_secret_file,
    secret_file_is_restricted,
)

_DATABASE_USER = "yfeistai_migrator"
_APP_DATABASE_USER = "yfeistai_app"
_OBJECT_REGION = "us-east-1"
_SECRET_NAMES = (
    "platform_database_app_password",
    "platform_database_migration_password",
    "minio_bootstrap_access_key",
    "minio_bootstrap_secret_key",
)
_IMAGE_REFERENCE = re.compile(
    r"^(?!-)[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?"
    r"@sha256:[0-9a-f]{64}$"
)
_RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_ANONYMOUS_VOLUME_NAME = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_REPOSITORY_COMPONENT = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_APPROVED_IMAGE_REPOSITORIES = {
    "postgres": "docker.io/library/postgres",
    "minio": "docker.io/minio/minio",
}
_MAX_SECRET_BYTES = 64 * 1024
_MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
_SAFE_ENVIRONMENT_NAMES = (
    "HOME",
    "PATH",
    "SystemRoot",
    "TEMP",
    "TMP",
)
_LABEL_PURPOSE = "com.yfeistai.purpose=backup-restore-target"


@dataclass(frozen=True, slots=True)
class TargetProvisioningConfig:
    candidate: Mapping[str, object]
    release_run: Mapping[str, object]
    target_secret_directory: Path
    runtime_staging_root: Path
    output_directory: Path
    docker_executable: str
    postgres_image: str
    minio_image: str
    postgres_host_port: int
    minio_host_port: int
    timeout_seconds: int
    docker_host: str = "unix:///var/run/docker.sock"


@dataclass(frozen=True, slots=True)
class TargetResourceNames:
    network_name: str
    postgres_container: str
    minio_container: str
    database_name: str
    object_namespace_id: str
    object_bucket: str


@dataclass(frozen=True, slots=True)
class ObjectStoreObservation:
    owner_id: str
    versioning_enabled: bool
    object_count: int
    version_count: int
    delete_marker_count: int


@dataclass(frozen=True, slots=True)
class ProvisionedTarget:
    target_config_path: Path
    provisioning_receipt_path: Path
    candidate_sha256: str
    database_identity_sha256: str
    object_store_identity_sha256: str
    staged_secret_directory: Path
    network_name: str
    postgres_volume: str
    minio_volume: str
    postgres_container: str
    minio_container: str
    database_name: str
    object_namespace_id: str
    object_bucket: str


CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]
ObjectStoreObserver = Callable[..., ObjectStoreObservation]


def _canonical(value: Mapping[str, object]) -> bytes:
    return canonical_backup_restore_report(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_port(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{field} is invalid")
    return value


def _normalized_image_repository(value: str) -> str:
    tagged_name = value.split("@", 1)[0]
    repository = tagged_name.rsplit(":", 1)[0] if ":" in tagged_name else tagged_name
    components = repository.split("/")
    if any(_IMAGE_REPOSITORY_COMPONENT.fullmatch(component) is None for component in components):
        raise ValueError("target provisioning image repository is invalid")
    first = components[0]
    if "." in first or ":" in first or first == "localhost":
        registry = first
        path = components[1:]
    else:
        registry = "docker.io"
        path = components
    if registry == "docker.io" and len(path) == 1:
        path.insert(0, "library")
    if not path:
        raise ValueError("target provisioning image repository is invalid")
    return f"{registry}/{'/'.join(path)}"


def _validate_image(value: object, image_kind: str) -> None:
    if not isinstance(value, str) or _IMAGE_REFERENCE.fullmatch(value) is None:
        raise ValueError("target provisioning image must be digest pinned")
    if value.endswith("sha256:" + "0" * 64):
        raise ValueError("target provisioning image digest is invalid")
    repository = _normalized_image_repository(value)
    if repository != _APPROVED_IMAGE_REPOSITORIES[image_kind]:
        raise ValueError(f"target provisioning {image_kind} image repository is invalid")


def _validate_config(
    config: TargetProvisioningConfig,
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(config, TargetProvisioningConfig):
        raise ValueError("target provisioning config is invalid")
    candidate, release_run = validate_backup_restore_identity(
        config.candidate,
        config.release_run,
    )
    if (
        not isinstance(config.docker_executable, str)
        or not config.docker_executable
        or config.docker_executable != config.docker_executable.strip()
        or any(ord(character) < 0x20 for character in config.docker_executable)
    ):
        raise ValueError("target provisioning Docker executable is invalid")
    _validate_image(config.postgres_image, "postgres")
    _validate_image(config.minio_image, "minio")
    if config.docker_host != "unix:///var/run/docker.sock":
        raise ValueError("target provisioning requires the local Linux Docker socket")
    postgres_port = _validate_port(config.postgres_host_port, "PostgreSQL host port")
    minio_port = _validate_port(config.minio_host_port, "MinIO host port")
    if postgres_port == minio_port:
        raise ValueError("target provisioning host ports must be distinct")
    if (
        isinstance(config.timeout_seconds, bool)
        or not isinstance(config.timeout_seconds, int)
        or not 1 <= config.timeout_seconds <= 3600
    ):
        raise ValueError("target provisioning timeout is invalid")
    secret_directory = Path(config.target_secret_directory)
    runtime_staging_root = Path(config.runtime_staging_root)
    output_directory = Path(config.output_directory)
    if (
        not secret_directory.is_absolute()
        or not runtime_staging_root.is_absolute()
        or not output_directory.is_absolute()
    ):
        raise ValueError("target provisioning paths must be absolute")
    if output_directory.exists() or output_directory.is_symlink():
        raise ValueError("target provisioning output already exists")
    output_parent = output_directory.parent
    if output_parent.is_symlink() or not output_parent.is_dir():
        raise ValueError("target provisioning output parent is unavailable")
    if secret_directory.is_symlink() or not secret_directory.is_dir():
        raise ValueError("target provisioning secret directory is unavailable")
    if runtime_staging_root.is_symlink() or not runtime_staging_root.is_dir():
        raise ValueError("target provisioning runtime staging root is unavailable")
    try:
        resolved_secret_directory = secret_directory.resolve(strict=True)
        resolved_staging_root = runtime_staging_root.resolve(strict=True)
        resolved_output_parent = output_parent.resolve(strict=True)
    except OSError:
        raise ValueError("target provisioning paths are unavailable") from None
    if (
        resolved_staging_root == resolved_output_parent
        or resolved_staging_root.is_relative_to(resolved_output_parent)
        or resolved_output_parent.is_relative_to(resolved_staging_root)
    ):
        raise ValueError("target provisioning runtime and evidence paths must be disjoint")
    if (
        resolved_secret_directory == resolved_output_parent
        or resolved_secret_directory.is_relative_to(resolved_output_parent)
        or resolved_output_parent.is_relative_to(resolved_secret_directory)
    ):
        raise ValueError("target provisioning secret and evidence paths must be disjoint")
    staging_stat = resolved_staging_root.stat()
    if os.name == "nt":
        staging_is_restricted = secret_file_is_restricted(resolved_staging_root)
    else:
        staging_is_restricted = (
            staging_stat.st_uid == os.geteuid()
            and stat.S_IMODE(staging_stat.st_mode) & 0o077 == 0
            and stat.S_IMODE(staging_stat.st_mode) & 0o700 == 0o700
        )
    if not staging_is_restricted:
        raise ValueError("target provisioning runtime staging root is not restricted")
    return candidate, release_run


def target_resource_names(config: TargetProvisioningConfig) -> TargetResourceNames:
    """Return deterministic names which make a run collision fail closed."""

    candidate, release_run = _validate_config(config)
    candidate_sha256 = _sha256(_canonical(candidate))
    binding = _canonical(
        {
            "candidateSha256": candidate_sha256,
            "releaseRun": release_run,
        }
    )
    suffix = _sha256(binding)[:20]
    prefix = f"yfeistai-br-{suffix}"
    names = TargetResourceNames(
        network_name=f"{prefix}-network",
        postgres_container=f"{prefix}-postgres",
        minio_container=f"{prefix}-minio",
        database_name=f"br_{suffix}",
        object_namespace_id=f"br-{suffix}",
        object_bucket=f"br-{suffix}",
    )
    if any(
        _RESOURCE_NAME.fullmatch(value) is None
        for value in (
            names.network_name,
            names.postgres_container,
            names.minio_container,
            names.database_name,
            names.object_namespace_id,
            names.object_bucket,
        )
    ):
        raise ValueError("target provisioning resource identity is invalid")
    staging_target = Path(config.runtime_staging_root) / f".{names.network_name}-secrets"
    if staging_target.exists() or staging_target.is_symlink():
        raise ValueError("target provisioning secret staging already exists")
    return names


def _read_secret_file(path: Path, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError(f"target provisioning secret {name} is unavailable") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= _MAX_SECRET_BYTES
            or (os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077)
        ):
            raise ValueError(f"target provisioning secret {name} is not restricted")
        body = b""
        while chunk := os.read(descriptor, min(8192, _MAX_SECRET_BYTES + 1 - len(body))):
            body += chunk
            if len(body) > _MAX_SECRET_BYTES:
                raise ValueError(f"target provisioning secret {name} is invalid")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(body) != before.st_size:
        raise ValueError(f"target provisioning secret {name} changed while reading")
    return body


def _read_secrets(directory: Path) -> dict[str, str]:
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("target provisioning secret directory is unavailable")
    try:
        root = root.resolve(strict=True)
    except OSError:
        raise ValueError("target provisioning secret directory is unavailable") from None
    root_stat = root.stat()
    if os.name != "nt" and (
        root_stat.st_uid != os.geteuid() or stat.S_IMODE(root_stat.st_mode) & 0o077
    ):
        raise ValueError("target provisioning secret directory is not restricted")
    values: dict[str, str] = {}
    for name in _SECRET_NAMES:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.parent.resolve() != root:
            raise ValueError(f"target provisioning secret {name} is unavailable")
        body = _read_secret_file(path, name)
        if not secret_file_is_restricted(path):
            raise ValueError(f"target provisioning secret {name} is not restricted")
        try:
            value = body.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            raise ValueError(f"target provisioning secret {name} is invalid") from None
        if not value or "\x00" in value:
            raise ValueError(f"target provisioning secret {name} is invalid")
        values[name] = value
    if len(set(values.values())) != len(values):
        raise ValueError("target provisioning secrets must be distinct")
    return values


def _stage_secrets(
    config: TargetProvisioningConfig,
    names: TargetResourceNames,
    secrets: Mapping[str, str],
) -> Path:
    directory = config.runtime_staging_root / f".{names.network_name}-secrets"
    if directory.exists() or directory.is_symlink():
        raise ValueError("target provisioning secret staging already exists")
    directory.mkdir(mode=0o700)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    for name in _SECRET_NAMES:
        target = directory / name
        _private_write_new(target, (secrets[name] + "\n").encode("utf-8"))
        restrict_secret_file(target)
        if not secret_file_is_restricted(target):
            raise RuntimeError("target provisioning staged secret is not restricted")
    _fsync_directory(directory)
    return directory.resolve(strict=True)


def _safe_environment(docker_host: str) -> dict[str, str]:
    environment = {name: os.environ[name] for name in _SAFE_ENVIRONMENT_NAMES if name in os.environ}
    environment["DOCKER_HOST"] = docker_host
    return environment


def _default_runner(
    arguments: Sequence[str],
    **options: object,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(arguments), **options)  # type: ignore[arg-type]


def _command_output_bytes(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    raise RuntimeError("target provisioning command returned invalid output")


def _invoke(
    runner: CommandRunner,
    arguments: Sequence[str],
    *,
    timeout: float,
    secrets: Mapping[str, str],
    docker_host: str,
    input_body: bytes | None = None,
) -> bytes:
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("target provisioning deadline expired")
    argv = [str(argument) for argument in arguments]
    secret_bytes = tuple(value.encode("utf-8") for value in secrets.values())
    if any(secret.decode("utf-8") in argument for secret in secret_bytes for argument in argv):
        raise RuntimeError("target provisioning command contains secret material")
    try:
        completed = runner(
            argv,
            input=input_body,
            timeout=timeout,
            check=False,
            capture_output=True,
            env=_safe_environment(docker_host),
            shell=False,
        )
    except Exception:
        raise RuntimeError("target provisioning command could not be executed") from None
    stdout = _command_output_bytes(completed.stdout)
    stderr = _command_output_bytes(completed.stderr)
    if len(stdout) > _MAX_COMMAND_OUTPUT_BYTES or len(stderr) > _MAX_COMMAND_OUTPUT_BYTES:
        raise RuntimeError("target provisioning command output is too large")
    if any(secret in output for secret in secret_bytes for output in (stdout, stderr)):
        raise RuntimeError("target provisioning command exposed secret material")
    if isinstance(completed.returncode, bool) or not isinstance(completed.returncode, int):
        raise RuntimeError("target provisioning command returned invalid status")
    if completed.returncode != 0:
        raise RuntimeError(f"target provisioning command failed with exit {completed.returncode}")
    return stdout


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise RuntimeError("target provisioning deadline expired")
    return remaining


def _docker(
    config: TargetProvisioningConfig,
    runner: CommandRunner,
    command: Sequence[str],
    *,
    deadline: float,
    secrets: Mapping[str, str],
    input_body: bytes | None = None,
) -> bytes:
    return _invoke(
        runner,
        [config.docker_executable, *command],
        timeout=_remaining(deadline),
        secrets=secrets,
        docker_host=config.docker_host,
        input_body=input_body,
    )


def _assert_local_linux_docker(
    config: TargetProvisioningConfig,
    *,
    runner: CommandRunner,
    deadline: float,
    secrets: Mapping[str, str],
) -> None:
    body = _docker(
        config,
        runner,
        ["info", "--format", "{{json .}}"],
        deadline=deadline,
        secrets=secrets,
    )
    try:
        info = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("target provisioning Docker identity is invalid") from None
    if (
        not isinstance(info, dict)
        or info.get("OSType") != "linux"
        or not isinstance(info.get("ID"), str)
        or not info["ID"]
    ):
        raise RuntimeError("target provisioning requires a local Linux Docker daemon")


def _listed_names(body: bytes) -> set[str]:
    try:
        names = {line.strip() for line in body.decode("utf-8").splitlines() if line.strip()}
    except UnicodeDecodeError:
        raise RuntimeError("target provisioning resource listing is invalid") from None
    if any(any(ord(character) < 0x20 for character in name) for name in names):
        raise RuntimeError("target provisioning resource listing is invalid")
    return names


def _ensure_resources_absent(
    config: TargetProvisioningConfig,
    names: TargetResourceNames,
    *,
    runner: CommandRunner,
    deadline: float,
    secrets: Mapping[str, str],
) -> None:
    checks = (
        ("container", names.postgres_container, ("container", "ls", "--all")),
        ("container", names.minio_container, ("container", "ls", "--all")),
        ("network", names.network_name, ("network", "ls")),
    )
    for _kind, name, command in checks:
        output = _docker(
            config,
            runner,
            [*command, "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
            deadline=deadline,
            secrets=secrets,
        )
        if name in _listed_names(output):
            raise ValueError(f"target provisioning resource already exists: {name}")


def _labels(candidate_sha256: str, run_id: str) -> tuple[str, ...]:
    return (
        "--label",
        _LABEL_PURPOSE,
        "--label",
        f"com.yfeistai.owner-run={run_id}",
        "--label",
        f"com.yfeistai.candidate-sha256={candidate_sha256}",
    )


def _secret_mount(path: Path, name: str) -> str:
    source = str(path.resolve(strict=True))
    if "," in source or any(ord(character) < 0x20 for character in source):
        raise ValueError("target provisioning secret path is not Docker-mount-safe")
    return f"type=bind,src={source},dst=/run/secrets/{name},readonly"


def _create_anonymous_volume(
    config: TargetProvisioningConfig,
    *,
    runner: CommandRunner,
    deadline: float,
    secrets: Mapping[str, str],
    labels: tuple[str, ...],
) -> str:
    body = _docker(
        config,
        runner,
        ["volume", "create", "--driver", "local", *labels],
        deadline=deadline,
        secrets=secrets,
    )
    try:
        name = body.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeError("target provisioning volume identity is invalid") from None
    if _ANONYMOUS_VOLUME_NAME.fullmatch(name) is None:
        raise RuntimeError("target provisioning volume identity is invalid")
    inspection_body = _docker(
        config,
        runner,
        ["volume", "inspect", "--format", "{{json .}}", name],
        deadline=deadline,
        secrets=secrets,
    )
    try:
        inspection = json.loads(inspection_body)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("target provisioning volume inspection is invalid") from None
    expected_labels = {
        labels[index + 1].split("=", 1)[0]: labels[index + 1].split("=", 1)[1]
        for index, argument in enumerate(labels)
        if argument == "--label"
    }
    if (
        not isinstance(inspection, dict)
        or inspection.get("Name") != name
        or inspection.get("Driver") != "local"
        or inspection.get("Scope") != "local"
        or inspection.get("Options") not in (None, {})
        or inspection.get("Labels") != expected_labels
    ):
        raise RuntimeError("target provisioning volume ownership is invalid")
    return name


def _create_resources(
    config: TargetProvisioningConfig,
    names: TargetResourceNames,
    *,
    runner: CommandRunner,
    deadline: float,
    secrets: Mapping[str, str],
    candidate_sha256: str,
    run_id: str,
    secret_root: Path,
) -> tuple[str, str]:
    labels = _labels(candidate_sha256, run_id)
    _docker(
        config,
        runner,
        ["network", "create", "--internal", *labels, names.network_name],
        deadline=deadline,
        secrets=secrets,
    )
    postgres_volume = _create_anonymous_volume(
        config,
        runner=runner,
        deadline=deadline,
        secrets=secrets,
        labels=labels,
    )
    minio_volume = _create_anonymous_volume(
        config,
        runner=runner,
        deadline=deadline,
        secrets=secrets,
        labels=labels,
    )
    migration_secret = secret_root / "platform_database_migration_password"
    access_secret = secret_root / "minio_bootstrap_access_key"
    object_secret = secret_root / "minio_bootstrap_secret_key"
    _docker(
        config,
        runner,
        [
            "run",
            "--detach",
            "--name",
            names.postgres_container,
            "--network",
            names.network_name,
            "--restart=no",
            *labels,
            "--publish",
            f"127.0.0.1:{config.postgres_host_port}:5432",
            "--mount",
            f"type=volume,src={postgres_volume},dst=/var/lib/postgresql/data",
            "--mount",
            _secret_mount(migration_secret, migration_secret.name),
            "--env",
            f"POSTGRES_USER={_DATABASE_USER}",
            "--env",
            f"POSTGRES_DB={names.database_name}",
            "--env",
            "POSTGRES_PASSWORD_FILE=/run/secrets/platform_database_migration_password",
            "--health-cmd",
            f"pg_isready --username {_DATABASE_USER} --dbname {names.database_name}",
            "--health-interval",
            "2s",
            "--health-timeout",
            "2s",
            "--health-retries",
            "60",
            config.postgres_image,
        ],
        deadline=deadline,
        secrets=secrets,
    )
    _docker(
        config,
        runner,
        [
            "run",
            "--detach",
            "--name",
            names.minio_container,
            "--network",
            names.network_name,
            "--restart=no",
            *labels,
            "--publish",
            f"127.0.0.1:{config.minio_host_port}:9000",
            "--mount",
            f"type=volume,src={minio_volume},dst=/data",
            "--mount",
            _secret_mount(access_secret, access_secret.name),
            "--mount",
            _secret_mount(object_secret, object_secret.name),
            "--env",
            "MINIO_ROOT_USER_FILE=/run/secrets/minio_bootstrap_access_key",
            "--env",
            "MINIO_ROOT_PASSWORD_FILE=/run/secrets/minio_bootstrap_secret_key",
            "--health-cmd",
            "mc ready local",
            "--health-interval",
            "2s",
            "--health-timeout",
            "2s",
            "--health-retries",
            "60",
            config.minio_image,
            "server",
            "/data",
            "--console-address",
            ":9001",
        ],
        deadline=deadline,
        secrets=secrets,
    )
    return postgres_volume, minio_volume


def _wait_for_healthy(
    config: TargetProvisioningConfig,
    container: str,
    *,
    runner: CommandRunner,
    deadline: float,
    secrets: Mapping[str, str],
    sleeper: Callable[[float], None],
) -> None:
    while True:
        status = (
            _docker(
                config,
                runner,
                ["inspect", "--format={{.State.Health.Status}}", container],
                deadline=deadline,
                secrets=secrets,
            )
            .decode("utf-8", errors="strict")
            .strip()
        )
        if status == "healthy":
            return
        if status == "unhealthy":
            raise RuntimeError("target provisioning container became unhealthy")
        remaining = _remaining(deadline)
        sleeper(min(1.0, remaining))


def _sql_literal(value: str, tag_seed: str) -> str:
    suffix = tag_seed[:16]
    tag = f"$yfeistai_{suffix}$"
    if tag in value:
        raise ValueError("target provisioning secret cannot be safely encoded")
    return f"{tag}{value}{tag}"


def _create_app_role(
    config: TargetProvisioningConfig,
    names: TargetResourceNames,
    *,
    runner: CommandRunner,
    deadline: float,
    secrets: Mapping[str, str],
    candidate_sha256: str,
) -> None:
    password = _sql_literal(
        secrets["platform_database_app_password"],
        candidate_sha256,
    )
    sql = (
        "\\set ON_ERROR_STOP on\n"
        "BEGIN;\n"
        f"CREATE ROLE {_APP_DATABASE_USER} LOGIN PASSWORD {password};\n"
        f"GRANT CONNECT ON DATABASE {names.database_name} TO {_APP_DATABASE_USER};\n"
        "COMMIT;\n"
    ).encode("utf-8")
    _docker(
        config,
        runner,
        [
            "exec",
            "-i",
            names.postgres_container,
            "psql",
            "--username",
            _DATABASE_USER,
            "--dbname",
            names.database_name,
            "--no-psqlrc",
        ],
        deadline=deadline,
        secrets=secrets,
        input_body=sql,
    )


_DATABASE_OBSERVATION_SQL = """
WITH candidate_namespaces AS (
    SELECT oid, nspname FROM pg_namespace
    WHERE nspname NOT IN ('pg_catalog', 'information_schema')
      AND nspname NOT LIKE 'pg_toast%'
      AND nspname NOT LIKE 'pg_temp_%'
), catalog_objects AS (
    SELECT 'pg_namespace'::regclass AS classid, oid AS objid
    FROM candidate_namespaces WHERE nspname <> 'public'
    UNION ALL SELECT 'pg_class'::regclass, c.oid FROM pg_class c
      JOIN candidate_namespaces n ON n.oid = c.relnamespace
      WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
    UNION ALL SELECT 'pg_proc'::regclass, p.oid FROM pg_proc p
      JOIN candidate_namespaces n ON n.oid = p.pronamespace
    UNION ALL SELECT 'pg_type'::regclass, t.oid FROM pg_type t
      JOIN candidate_namespaces n ON n.oid = t.typnamespace
      WHERE t.typtype IN ('b', 'c', 'd', 'e', 'r', 'm')
    UNION ALL SELECT 'pg_operator'::regclass, o.oid FROM pg_operator o
      JOIN candidate_namespaces n ON n.oid = o.oprnamespace
    UNION ALL SELECT 'pg_collation'::regclass, c.oid FROM pg_collation c
      JOIN candidate_namespaces n ON n.oid = c.collnamespace
    UNION ALL SELECT 'pg_conversion'::regclass, c.oid FROM pg_conversion c
      JOIN candidate_namespaces n ON n.oid = c.connamespace
    UNION ALL SELECT 'pg_ts_config'::regclass, c.oid FROM pg_ts_config c
      JOIN candidate_namespaces n ON n.oid = c.cfgnamespace
    UNION ALL SELECT 'pg_ts_dict'::regclass, d.oid FROM pg_ts_dict d
      JOIN candidate_namespaces n ON n.oid = d.dictnamespace
    UNION ALL SELECT 'pg_ts_parser'::regclass, p.oid FROM pg_ts_parser p
      JOIN candidate_namespaces n ON n.oid = p.prsnamespace
    UNION ALL SELECT 'pg_ts_template'::regclass, t.oid FROM pg_ts_template t
      JOIN candidate_namespaces n ON n.oid = t.tmplnamespace
    UNION ALL SELECT 'pg_opclass'::regclass, o.oid FROM pg_opclass o
      JOIN candidate_namespaces n ON n.oid = o.opcnamespace
    UNION ALL SELECT 'pg_opfamily'::regclass, o.oid FROM pg_opfamily o
      JOIN candidate_namespaces n ON n.oid = o.opfnamespace
    UNION ALL SELECT 'pg_statistic_ext'::regclass, s.oid FROM pg_statistic_ext s
      JOIN candidate_namespaces n ON n.oid = s.stxnamespace
    UNION ALL SELECT 'pg_event_trigger'::regclass, oid FROM pg_event_trigger
    UNION ALL SELECT 'pg_foreign_data_wrapper'::regclass, oid FROM pg_foreign_data_wrapper
    UNION ALL SELECT 'pg_foreign_server'::regclass, oid FROM pg_foreign_server
    UNION ALL SELECT 'pg_user_mapping'::regclass, oid FROM pg_user_mapping
    UNION ALL SELECT 'pg_publication'::regclass, oid FROM pg_publication
    UNION ALL SELECT 'pg_subscription'::regclass, oid FROM pg_subscription
    UNION ALL SELECT 'pg_extension'::regclass, oid FROM pg_extension WHERE extname <> 'plpgsql'
    UNION ALL SELECT 'pg_cast'::regclass, oid FROM pg_cast WHERE oid >= 16384
    UNION ALL SELECT 'pg_transform'::regclass, oid FROM pg_transform WHERE oid >= 16384
    UNION ALL SELECT 'pg_language'::regclass, oid FROM pg_language WHERE oid >= 16384
    UNION ALL SELECT 'pg_largeobject_metadata'::regclass, oid
      FROM pg_largeobject_metadata WHERE oid >= 16384
    UNION ALL SELECT 'pg_default_acl'::regclass, oid FROM pg_default_acl WHERE oid >= 16384
), user_objects AS (
    SELECT objid FROM catalog_objects o
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = o.classid AND d.objid = o.objid AND d.deptype = 'e'
    )
)
SELECT json_build_object(
    'databaseName', d.datname,
    'databaseOid', d.oid::text,
    'databaseOwner', r.rolname,
    'currentRole', current_user,
    'systemIdentifier', c.system_identifier::text,
    'userObjectCount', (SELECT count(*)::integer FROM user_objects)
)::text
FROM pg_control_system() c
CROSS JOIN pg_database d
JOIN pg_roles r ON r.oid = d.datdba
WHERE d.datname = current_database()
""".strip()


def _observe_database(
    config: TargetProvisioningConfig,
    names: TargetResourceNames,
    *,
    runner: CommandRunner,
    deadline: float,
    secrets: Mapping[str, str],
) -> str:
    body = _docker(
        config,
        runner,
        [
            "exec",
            names.postgres_container,
            "psql",
            "--username",
            _DATABASE_USER,
            "--dbname",
            names.database_name,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--command",
            _DATABASE_OBSERVATION_SQL,
        ],
        deadline=deadline,
        secrets=secrets,
    )
    try:
        observation = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("target provisioning database observation is invalid") from None
    expected_fields = {
        "databaseName",
        "databaseOid",
        "databaseOwner",
        "currentRole",
        "systemIdentifier",
        "userObjectCount",
    }
    if (
        not isinstance(observation, dict)
        or set(observation) != expected_fields
        or observation.get("databaseName") != names.database_name
        or observation.get("databaseOwner") != _DATABASE_USER
        or observation.get("currentRole") != _DATABASE_USER
        or not isinstance(observation.get("databaseOid"), str)
        or not observation["databaseOid"].isdigit()
        or not isinstance(observation.get("systemIdentifier"), str)
        or not observation["systemIdentifier"].isdigit()
        or observation.get("userObjectCount") != 0
    ):
        raise RuntimeError("target provisioning database is not fresh and isolated")
    identity = {
        "databaseName": observation["databaseName"],
        "databaseOid": observation["databaseOid"],
        "systemIdentifier": observation["systemIdentifier"],
    }
    return _sha256(_canonical(identity))


def _shell_literal(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _minio_initialization_body(
    names: TargetResourceNames,
    secrets: Mapping[str, str],
) -> bytes:
    access_key = quote(secrets["minio_bootstrap_access_key"], safe="")
    secret_key = quote(secrets["minio_bootstrap_secret_key"], safe="")
    endpoint = f"http://{access_key}:{secret_key}@127.0.0.1:9000"
    return (
        "set -eu\n"
        f"export MC_HOST_target={_shell_literal(endpoint)}\n"
        f"bucket={_shell_literal(names.object_bucket)}\n"
        'mc mb "target/$bucket"\n'
        'mc version enable "target/$bucket"\n'
        'mc version info "target/$bucket" | grep -F "versioning is enabled" >/dev/null\n'
    ).encode("utf-8")


def _initialize_object_store(
    config: TargetProvisioningConfig,
    names: TargetResourceNames,
    *,
    runner: CommandRunner,
    deadline: float,
    secrets: Mapping[str, str],
) -> None:
    _docker(
        config,
        runner,
        [
            "exec",
            "-i",
            names.minio_container,
            "/bin/sh",
            "-s",
            "--",
        ],
        deadline=deadline,
        secrets=secrets,
        input_body=_minio_initialization_body(names, secrets),
    )


def _default_object_store_observer(
    *,
    endpoint: str,
    region: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    timeout: float,
) -> ObjectStoreObservation:
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                connect_timeout=timeout,
                read_timeout=timeout,
                retries={"max_attempts": 0},
            ),
        )
        owner = client.get_bucket_acl(Bucket=bucket).get("Owner", {}).get("ID")
        versioning = client.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
        object_count = 0
        version_count = 0
        delete_marker_count = 0
        paginator = client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            versions = page.get("Versions", [])
            delete_markers = page.get("DeleteMarkers", [])
            if not isinstance(versions, list) or not isinstance(delete_markers, list):
                raise RuntimeError
            version_count += len(versions)
            delete_marker_count += len(delete_markers)
            object_count += sum(1 for item in versions if item.get("IsLatest") is True)
    except Exception:
        raise RuntimeError("target provisioning object store observation failed") from None
    finally:
        if "client" in locals():
            close = getattr(client, "close", None)
            if callable(close):
                close()
    if not isinstance(owner, str) or not owner:
        raise RuntimeError("target provisioning object store owner is unavailable")
    return ObjectStoreObservation(
        owner_id=owner,
        versioning_enabled=versioning,
        object_count=object_count,
        version_count=version_count,
        delete_marker_count=delete_marker_count,
    )


def _observe_object_store(
    config: TargetProvisioningConfig,
    names: TargetResourceNames,
    secrets: Mapping[str, str],
    *,
    deadline: float,
    observer: ObjectStoreObserver,
) -> str:
    try:
        observation = observer(
            endpoint=f"http://127.0.0.1:{config.minio_host_port}",
            region=_OBJECT_REGION,
            bucket=names.object_bucket,
            access_key=secrets["minio_bootstrap_access_key"],
            secret_key=secrets["minio_bootstrap_secret_key"],
            timeout=_remaining(deadline),
        )
    except Exception:
        raise RuntimeError("target provisioning object store observation failed") from None
    if (
        not isinstance(observation, ObjectStoreObservation)
        or not isinstance(observation.owner_id, str)
        or not observation.owner_id
        or observation.versioning_enabled is not True
        or observation.object_count != 0
        or observation.version_count != 0
        or observation.delete_marker_count != 0
    ):
        raise RuntimeError("target provisioning object store is not fresh and versioned")
    owner_id_sha256 = _sha256(observation.owner_id.encode("utf-8"))
    return physical_object_store_identity_sha256(
        f"http://127.0.0.1:{config.minio_host_port}",
        _OBJECT_REGION,
        names.object_bucket,
        owner_id_sha256,
    )


def _private_write_new(path: Path, body: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_artifacts(
    config: TargetProvisioningConfig,
    names: TargetResourceNames,
    *,
    candidate_sha256: str,
    release_run: Mapping[str, object],
    database_identity_sha256: str,
    object_store_identity_sha256: str,
    secrets: Mapping[str, str],
) -> tuple[Path, Path]:
    target = {
        "database_host": "127.0.0.1",
        "database_name": names.database_name,
        "database_port": config.postgres_host_port,
        "database_url": None,
        "database_user": _DATABASE_USER,
        "enabled": True,
        "object_store_bucket": names.object_bucket,
        "object_store_endpoint": f"http://127.0.0.1:{config.minio_host_port}",
        "object_store_mode": "s3",
        "object_store_namespace_id": names.object_namespace_id,
        "object_store_region": _OBJECT_REGION,
    }
    receipt = {
        "schemaVersion": 1,
        "producer": TARGET_PROVISIONING_RECEIPT_PRODUCER,
        "candidateSha256": candidate_sha256,
        "releaseRun": dict(release_run),
        "resources": {
            "database": {
                "identitySha256": database_identity_sha256,
                "ownerRunId": release_run["runId"],
                "disposition": "runner-owned-disposable",
            },
            "objectStore": {
                "identitySha256": object_store_identity_sha256,
                "ownerRunId": release_run["runId"],
                "disposition": "runner-owned-disposable",
            },
        },
    }
    target_body = _canonical(target)
    receipt_body = _canonical(receipt)
    secret_bytes = tuple(value.encode("utf-8") for value in secrets.values())
    if any(secret in body for secret in secret_bytes for body in (target_body, receipt_body)):
        raise RuntimeError("target provisioning artifact contains secret material")
    receipt_sha256 = _sha256(receipt_body)
    parse_target_provisioning_receipt(
        receipt_body,
        provisioning_receipt_sha256=receipt_sha256,
        candidate_sha256=candidate_sha256,
        release_run=release_run,
        database_disposition="runner-owned-disposable",
        object_store_disposition="runner-owned-disposable",
        database_identity_sha256=database_identity_sha256,
        object_store_identity_sha256=object_store_identity_sha256,
    )
    output = Path(config.output_directory)
    output.mkdir(mode=0o700)
    if os.name != "nt":
        os.chmod(output, 0o700)
    target_path = output / "target-config.json"
    receipt_path = output / "target-provisioning-receipt.json"
    _private_write_new(target_path, target_body)
    _private_write_new(receipt_path, receipt_body)
    _fsync_directory(output)
    return target_path, receipt_path


def provision_backup_restore_target(
    config: TargetProvisioningConfig,
    *,
    runner: CommandRunner = _default_runner,
    object_store_observer: ObjectStoreObserver = _default_object_store_observer,
    sleeper: Callable[[float], None] = time.sleep,
) -> ProvisionedTarget:
    """Provision and attest one new target; retain every resource for inspection."""

    candidate, release_run = _validate_config(config)
    names = target_resource_names(config)
    secrets = _read_secrets(Path(config.target_secret_directory))
    candidate_sha256 = _sha256(_canonical(candidate))
    deadline = time.monotonic() + config.timeout_seconds
    _assert_local_linux_docker(
        config,
        runner=runner,
        deadline=deadline,
        secrets=secrets,
    )
    _ensure_resources_absent(
        config,
        names,
        runner=runner,
        deadline=deadline,
        secrets=secrets,
    )
    staged_secret_directory = _stage_secrets(config, names, secrets)
    postgres_volume, minio_volume = _create_resources(
        config,
        names,
        runner=runner,
        deadline=deadline,
        secrets=secrets,
        candidate_sha256=candidate_sha256,
        run_id=str(release_run["runId"]),
        secret_root=staged_secret_directory,
    )
    for container in (names.postgres_container, names.minio_container):
        _wait_for_healthy(
            config,
            container,
            runner=runner,
            deadline=deadline,
            secrets=secrets,
            sleeper=sleeper,
        )
    _create_app_role(
        config,
        names,
        runner=runner,
        deadline=deadline,
        secrets=secrets,
        candidate_sha256=candidate_sha256,
    )
    _initialize_object_store(
        config,
        names,
        runner=runner,
        deadline=deadline,
        secrets=secrets,
    )
    database_identity_sha256 = _observe_database(
        config,
        names,
        runner=runner,
        deadline=deadline,
        secrets=secrets,
    )
    object_store_identity_sha256 = _observe_object_store(
        config,
        names,
        secrets,
        deadline=deadline,
        observer=object_store_observer,
    )
    target_config_path, provisioning_receipt_path = _write_artifacts(
        config,
        names,
        candidate_sha256=candidate_sha256,
        release_run=release_run,
        database_identity_sha256=database_identity_sha256,
        object_store_identity_sha256=object_store_identity_sha256,
        secrets=secrets,
    )
    return ProvisionedTarget(
        target_config_path=target_config_path,
        provisioning_receipt_path=provisioning_receipt_path,
        candidate_sha256=candidate_sha256,
        database_identity_sha256=database_identity_sha256,
        object_store_identity_sha256=object_store_identity_sha256,
        staged_secret_directory=staged_secret_directory,
        network_name=names.network_name,
        postgres_volume=postgres_volume,
        minio_volume=minio_volume,
        postgres_container=names.postgres_container,
        minio_container=names.minio_container,
        database_name=names.database_name,
        object_namespace_id=names.object_namespace_id,
        object_bucket=names.object_bucket,
    )


def _load_canonical_json(path: Path, field: str) -> dict[str, object]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"{field} is unavailable")
    try:
        body = source.read_bytes()
        payload = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError(f"{field} is invalid") from None
    if not isinstance(payload, dict) or body != _canonical(payload):
        raise ValueError(f"{field} must be canonical JSON")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--release-run", required=True, type=Path)
    parser.add_argument("--target-secret-dir", required=True, type=Path)
    parser.add_argument("--runtime-staging-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--minio-image", required=True)
    parser.add_argument("--postgres-host-port", required=True, type=int)
    parser.add_argument("--minio-host-port", required=True, type=int)
    parser.add_argument("--timeout-seconds", default=900, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = provision_backup_restore_target(
            TargetProvisioningConfig(
                candidate=_load_canonical_json(arguments.candidate, "candidate"),
                release_run=_load_canonical_json(arguments.release_run, "release run"),
                target_secret_directory=arguments.target_secret_dir,
                runtime_staging_root=arguments.runtime_staging_root,
                output_directory=arguments.output_dir,
                docker_executable=arguments.docker,
                postgres_image=arguments.postgres_image,
                minio_image=arguments.minio_image,
                postgres_host_port=arguments.postgres_host_port,
                minio_host_port=arguments.minio_host_port,
                timeout_seconds=arguments.timeout_seconds,
            )
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        _canonical(
            {
                "database": result.database_name,
                "objectBucket": result.object_bucket,
                "outputDirectory": str(result.target_config_path.parent),
                "secretStagingDirectory": str(result.staged_secret_directory),
                "network": result.network_name,
                "postgresContainer": result.postgres_container,
                "postgresVolume": result.postgres_volume,
                "minioContainer": result.minio_container,
                "minioVolume": result.minio_volume,
                "retained": True,
            }
        ).decode("utf-8"),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
