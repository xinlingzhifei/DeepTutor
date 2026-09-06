#!/usr/bin/env python
"""Run Docker Compose with port mappings rendered from JSON settings.

Docker Compose cannot read ``data/user/settings/system.json`` directly for
host port interpolation. This wrapper renders a tiny compose env file from the
JSON settings and then invokes ``docker compose --env-file``. It intentionally
does not read or migrate the project-root ``.env`` file.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
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
_OBSERVER_RUNTIME_ENVIRONMENT = (
    "YFEISTAI_OBSERVER_SECRET_DIR",
    "YFEISTAI_RELEASE_RUN_ID",
    "YFEISTAI_OBSERVER_ATTESTATION_DIR",
    "YFEISTAI_OBSERVER_TRUST_ROOT",
    "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256",
    "YFEISTAI_CANDIDATE_SHA256",
    "YFEISTAI_IMAGE_LOCK_SHA256",
    "YFEISTAI_OBSERVER_SIGNER_IMAGE",
    "YFEISTAI_ENVIRONMENT_ID",
    "YFEISTAI_SHARED_INGRESS_OBSERVER_ID",
    "YFEISTAI_SHARED_INGRESS_OBSERVER_URL",
    "YFEISTAI_SHARED_INGRESS_CONTROL_URL",
    "YFEISTAI_SHARED_INGRESS_UPSTREAM_URL",
    "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL",
    "YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT",
)


def _assert_no_link_ancestors(path: Path) -> None:
    absolute_path = Path(os.path.abspath(path))
    current = Path(absolute_path.anchor)
    for part in absolute_path.parts[1:]:
        current /= part
        try:
            details = os.lstat(current)
        except OSError as exc:
            raise OSError(f"production Compose path could not be inspected: {current}") from exc
        attributes = int(getattr(details, "st_file_attributes", 0))
        if stat.S_ISLNK(details.st_mode) or attributes & 0x00000400:
            raise RuntimeError(
                f"production Compose path contains a link or reparse point: {current}"
            )


def _windows_file_api() -> tuple[Any, ...]:
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
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
    read_file = kernel32.ReadFile
    read_file.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    read_file.restype = wintypes.BOOL
    get_file_information = kernel32.GetFileInformationByHandle
    get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_file_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return (
        ctypes,
        wintypes,
        _ByHandleFileInformation,
        create_file,
        read_file,
        get_file_information,
        close_handle,
    )


def _open_windows_read_handle(path: Path) -> Any:
    ctypes, wintypes, _info, create_file, _read, _get_info, _close = _windows_file_api()
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ; deny write and delete sharing
        None,
        3,  # OPEN_EXISTING
        0x00000080 | 0x00200000,  # FILE_ATTRIBUTE_NORMAL | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(path))
    return handle


def _checked_close_windows_handle(handle: Any, path: Path) -> None:
    ctypes, _wintypes, _info, _create, _read, _get_info, close_handle = _windows_file_api()
    if not close_handle(handle):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(path))


def _windows_handle_identity(handle: Any, path: Path) -> tuple[int, int, int]:
    ctypes, _wintypes, information_type, _create, _read, get_info, _close = _windows_file_api()
    information = information_type()
    if not get_info(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(path))
    if information.file_attributes & (0x00000010 | 0x00000400):
        raise RuntimeError(f"production Compose artifact is not a regular file: {path.name}")
    file_index = (int(information.file_index_high) << 32) | int(information.file_index_low)
    file_size = (int(information.file_size_high) << 32) | int(information.file_size_low)
    return int(information.volume_serial_number), file_index, file_size


def _read_windows_handle(handle: Any, path: Path) -> bytes:
    ctypes, wintypes, _info, _create, read_file, _get_info, _close = _windows_file_api()
    chunks: list[bytes] = []
    while True:
        buffer = ctypes.create_string_buffer(1024 * 1024)
        read_count = wintypes.DWORD()
        if not read_file(handle, buffer, len(buffer), ctypes.byref(read_count), None):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(path))
        if read_count.value == 0:
            return b"".join(chunks)
        chunks.append(buffer.raw[: read_count.value])


def _windows_path_identity(path: Path) -> tuple[int, int, int]:
    handle = _open_windows_read_handle(path)
    error: BaseException | None = None
    identity: tuple[int, int, int] | None = None
    try:
        identity = _windows_handle_identity(handle, path)
    except BaseException as exc:
        error = exc
    try:
        _checked_close_windows_handle(handle, path)
    except BaseException as close_error:
        if error is not None:
            raise BaseExceptionGroup(
                "Windows Compose identity and handle cleanup both failed",
                [error, close_error],
            ) from None
        raise
    if error is not None:
        raise error.with_traceback(error.__traceback__)
    assert identity is not None
    return identity


class _ComposeArtifactLease:
    """Keep one verified artifact immutable and readable through Compose execution."""

    def __init__(
        self,
        *,
        original_path: Path,
        execution_path: Path,
        content: bytes,
        close: Any,
        assert_unchanged: Any | None = None,
    ) -> None:
        self.original_path = original_path
        self.execution_path = execution_path
        self.content = content
        self._close = close
        self._assert_unchanged = assert_unchanged

    @classmethod
    def acquire(cls, path: Path) -> _ComposeArtifactLease:
        absolute_path = Path(os.path.abspath(path))
        _assert_no_link_ancestors(absolute_path)
        if os.name == "nt":
            return cls._acquire_windows(absolute_path)
        if os.name == "posix":
            return cls._acquire_linux_snapshot(absolute_path)
        raise RuntimeError("production Compose execution leases are unsupported")

    @classmethod
    def _acquire_windows(cls, path: Path) -> _ComposeArtifactLease:
        handle = _open_windows_read_handle(path)
        try:
            identity = _windows_handle_identity(handle, path)
            content = _read_windows_handle(handle, path)
            if len(content) != identity[2]:
                raise RuntimeError(
                    f"production Compose artifact size changed while leased: {path.name}"
                )
        except BaseException as exc:
            try:
                _checked_close_windows_handle(handle, path)
            except BaseException as close_error:
                raise BaseExceptionGroup(
                    "Windows Compose acquisition and handle cleanup both failed",
                    [exc, close_error],
                ) from None
            raise

        def assert_unchanged() -> None:
            _assert_no_link_ancestors(path)
            if _windows_path_identity(path) != identity:
                raise RuntimeError(
                    f"production Compose artifact identity changed while leased: {path.name}"
                )

        return cls(
            original_path=path,
            execution_path=path,
            content=content,
            close=lambda: _checked_close_windows_handle(handle, path),
            assert_unchanged=assert_unchanged,
        )

    @classmethod
    def _acquire_linux_snapshot(cls, path: Path) -> _ComposeArtifactLease:
        if not hasattr(os, "memfd_create") or not Path("/proc/self/fd").is_dir():
            raise RuntimeError("production Compose execution leases require Linux procfs")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise RuntimeError(f"production Compose artifact is not a file: {path.name}")
            chunks: list[bytes] = []
            while chunk := os.read(source_fd, 1024 * 1024):
                chunks.append(chunk)
            content = b"".join(chunks)
        finally:
            os.close(source_fd)

        import fcntl

        snapshot_fd = os.memfd_create(
            f"yfeistai-{path.name}",
            getattr(os, "MFD_CLOEXEC", 0) | getattr(os, "MFD_ALLOW_SEALING", 0),
        )
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(snapshot_fd, remaining)
                if written <= 0:
                    raise OSError("production Compose snapshot write was incomplete")
                remaining = remaining[written:]
            os.lseek(snapshot_fd, 0, os.SEEK_SET)
            required_seals = (
                fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
            )
            fcntl.fcntl(snapshot_fd, fcntl.F_ADD_SEALS, required_seals)
            applied_seals = fcntl.fcntl(snapshot_fd, fcntl.F_GET_SEALS)
            if applied_seals & required_seals != required_seals:
                raise RuntimeError("production Compose snapshot could not be sealed")
            execution_path = Path(f"/proc/{os.getpid()}/fd/{snapshot_fd}")
            if execution_path.read_bytes() != content:
                raise RuntimeError("production Compose snapshot could not be verified")
        except BaseException:
            os.close(snapshot_fd)
            raise

        def assert_unchanged() -> None:
            if execution_path.read_bytes() != content:
                raise RuntimeError(f"production Compose snapshot changed while leased: {path.name}")

        return cls(
            original_path=path,
            execution_path=execution_path,
            content=content,
            close=lambda: os.close(snapshot_fd),
            assert_unchanged=assert_unchanged,
        )

    def assert_unchanged(self) -> None:
        if self._assert_unchanged is not None:
            self._assert_unchanged()

    def close(self) -> None:
        close = self._close
        if close is not None:
            close()
            self._close = None


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


def _load_neighbor_script(file_name: str, module_name: str, error_message: str) -> Any:
    module_path = Path(__file__).resolve().with_name(file_name)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(error_message)
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


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _close_artifact_leases(leases: dict[str, Any]) -> None:
    errors: list[BaseException] = []
    for lease in reversed(tuple(leases.values())):
        try:
            lease.close()
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise BaseExceptionGroup("production Compose lease cleanup failed", errors)


@contextmanager
def _production_artifact_leases(
    *,
    platform: bool,
    data_plane: str | None,
    candidate_root: Path,
):
    if not platform and data_plane is None:
        yield {}
        return
    _assert_no_link_ancestors(candidate_root)
    renderer = _load_platform_renderer()
    with renderer.validated_candidate_artifact_lease(candidate_root) as artifact:
        artifacts = artifact.paths
        required_paths = [
            artifacts.image_lock,
            artifacts.base_compose,
            artifacts.platform_compose,
            artifacts.data_plane_compose,
            artifacts.observer_overlay,
        ]
        if platform:
            required_paths.append(PROJECT_ROOT / "docker-compose.yml")
        expected_candidate_bytes = {
            _path_key(artifacts.image_lock): artifact.image_lock_bytes,
            **{_path_key(path): content for path, content in artifact.compose_bytes},
        }
        leases: dict[str, _ComposeArtifactLease] = {}
        body_error: BaseException | None = None
        try:
            for path in required_paths:
                key = _path_key(path)
                if key not in leases:
                    leases[key] = _ComposeArtifactLease.acquire(path)
            for key, expected in expected_candidate_bytes.items():
                if key not in leases or leases[key].content != expected:
                    raise RuntimeError(
                        "production candidate artifact changed between validation and execution lease"
                    )
            yield leases
        except BaseException as exc:
            body_error = exc
        cleanup_error: BaseException | None = None
        try:
            _close_artifact_leases(leases)
        except BaseException as exc:
            cleanup_error = exc
        if body_error is not None:
            if cleanup_error is not None:
                raise BaseExceptionGroup(
                    "production Compose execution and lease cleanup both failed",
                    [body_error, cleanup_error],
                ) from None
            raise body_error.with_traceback(body_error.__traceback__)
        if cleanup_error is not None:
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)


def _lease_for(
    leases: dict[str, _ComposeArtifactLease],
    path: Path,
) -> _ComposeArtifactLease:
    try:
        return leases[_path_key(path)]
    except KeyError as exc:
        raise RuntimeError(f"production Compose artifact was not leased: {path.name}") from exc


def platform_compose_topology_arguments(
    *,
    deployment_root: Path,
    candidate_root: Path,
    env_file: Path,
) -> list[str]:
    """Return the one fixed platform topology shared by launch and attestation."""
    deployment = Path(os.path.abspath(deployment_root))
    candidate = Path(os.path.abspath(candidate_root))
    artifacts = _candidate_paths(candidate)
    return [
        "--env-file",
        str(Path(os.path.abspath(env_file))),
        "--project-directory",
        str(deployment),
        "--project-name",
        "yfeistai-platform",
        "-f",
        str(candidate / "docker-compose.yml"),
        "-f",
        str(artifacts.platform_compose),
        "-f",
        str(artifacts.observer_overlay),
    ]


def _validate_production_image_lock(
    candidate_root: Path,
    leases: dict[str, _ComposeArtifactLease] | None = None,
) -> None:
    renderer = _load_platform_renderer()
    paths = renderer.candidate_artifact_paths(candidate_root)
    compose_paths = (
        paths.base_compose,
        paths.platform_compose,
        paths.data_plane_compose,
        paths.observer_overlay,
    )
    if leases is None:
        renderer.validate_image_lock_bindings(
            paths.image_lock,
            require_candidate=True,
            compose_paths=compose_paths,
        )
        return

    lock = renderer.load_image_lock_bytes(
        _lease_for(leases, paths.image_lock).content,
        require_candidate=True,
    )
    images = lock["images"]
    support_images = lock["supportImages"]
    for compose_path in compose_paths:
        template_path = (
            renderer.PROJECT_ROOT / "deploy" / compose_path.name
            if compose_path.name == "docker-compose.openmaic-shared-ingress-observer.yml"
            else renderer.PROJECT_ROOT / compose_path.name
        )
        try:
            current = _lease_for(leases, compose_path).content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"production Compose file could not be read: {compose_path.name}"
            ) from exc
        expected = renderer._render_compose_references(
            template_path,
            images,
            support_images,
        )
        if current != expected:
            raise ValueError("production Compose image references do not match image lock")


def _observer_release_file_snapshot(admission: dict[str, object]) -> tuple[tuple[str, str], ...]:
    paths = admission.get("paths")
    required = (
        "observerAuthToken",
        "candidateHealthToken",
        "attestationSigningKey",
        "attestationPublicKey",
    )
    if not isinstance(paths, dict):
        raise ValueError("observer release manifest paths are invalid")
    snapshot: list[tuple[str, str]] = []
    for name in required:
        raw_path = paths.get(name)
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise ValueError("observer release manifest paths are invalid")
        lease = _ComposeArtifactLease.acquire(Path(raw_path))
        try:
            if not lease.content or len(lease.content) > 1024 * 1024:
                raise ValueError("observer release file is invalid")
            snapshot.append((name, hashlib.sha256(lease.content).hexdigest()))
        finally:
            lease.close()
    return tuple(snapshot)


def _load_observer_release_admission(
    *,
    manifest_path: Path,
    candidate_root: Path,
    leases: dict[str, _ComposeArtifactLease],
) -> dict[str, object]:
    renderer = _load_platform_renderer()
    artifact_paths = renderer.candidate_artifact_paths(candidate_root)
    image_lock_bytes = _lease_for(leases, artifact_paths.image_lock).content
    try:
        return load_observer_release_admission(
            manifest_path=manifest_path,
            candidate_root=candidate_root,
            image_lock_bytes=image_lock_bytes,
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SystemExit("observer release manifest is unavailable or invalid") from exc


def load_observer_release_admission(
    *,
    manifest_path: Path,
    candidate_root: Path,
    image_lock_bytes: bytes,
) -> dict[str, object]:
    """Bind launcher or host preflight admission to already-leased candidate bytes."""
    renderer = _load_platform_renderer()
    lock = renderer.load_image_lock_bytes(
        image_lock_bytes,
        require_candidate=True,
    )
    signer = lock["supportImages"]["observer_signer"]["reference"]
    if not isinstance(signer, str):
        raise ValueError("observer signer image is unavailable")
    provisioner = _load_neighbor_script(
        "provision_openmaic_observer_secrets.py",
        "_yfeistai_provision_openmaic_observer_secrets",
        "observer release manifest consumer could not be loaded",
    )
    preflight = _load_neighbor_script(
        "platform_preflight.py",
        "_yfeistai_platform_preflight_for_observer_admission",
        "observer release preflight could not be loaded",
    )

    def validate_release() -> tuple[dict[str, object], object, tuple[tuple[str, str], ...]]:
        current_admission = provisioner.load_committed_observer_release_manifest(
            manifest_path=manifest_path,
            candidate_root=candidate_root,
            observer_signer_image=signer,
            image_lock_bytes=image_lock_bytes,
        )
        inputs = current_admission["platformPreflightInputs"]
        errors, current_proof = preflight._inspect_observer_secret_preflight(
            Path(inputs["observer_secret_dir"]),
            inputs["release_run_id"],
            inputs["observer_signer_image"],
        )
        if errors or current_proof is None:
            raise ValueError("observer release manifest preflight failed")
        return current_admission, current_proof, _observer_release_file_snapshot(current_admission)

    admission, proof, release_file_snapshot = validate_release()

    baseline_admission = dict(admission)
    baseline_proof = proof
    baseline_release_file_snapshot = release_file_snapshot

    def revalidate() -> None:
        try:
            current_admission, current_proof, current_release_file_snapshot = validate_release()
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            raise ValueError("observer release files changed after admission") from None
        if (
            current_admission != baseline_admission
            or current_proof != baseline_proof
            or current_release_file_snapshot != baseline_release_file_snapshot
        ):
            raise ValueError("observer release files changed after admission")

    admission["observerSecretPreflight"] = proof
    admission["_revalidate"] = revalidate
    return admission


def _bind_compose_command_to_leases(
    command: list[str],
    leases: dict[str, _ComposeArtifactLease],
) -> list[str]:
    bound_command = list(command)
    compose_count = 0
    for index, argument in enumerate(bound_command[:-1]):
        if argument != "-f":
            continue
        compose_path = Path(bound_command[index + 1])
        lease = _lease_for(leases, compose_path)
        bound_command[index + 1] = str(lease.execution_path)
        compose_count += 1
    if compose_count == 0:
        raise RuntimeError("production Compose command has no leased files")
    return bound_command


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
            candidate_root = Path(os.path.abspath(candidate_root))
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


def _extract_observer_release_manifest(
    args: list[str],
) -> tuple[Path | None, list[str]]:
    manifest: Path | None = None
    compose_args: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        value: str | None = None
        if argument == "--observer-release-manifest":
            if index + 1 >= len(args):
                raise SystemExit("--observer-release-manifest requires a value")
            value = args[index + 1]
            index += 2
        elif argument.startswith("--observer-release-manifest="):
            value = argument.partition("=")[2]
            index += 1
        else:
            compose_args.append(argument)
            index += 1
            continue
        if manifest is not None:
            raise SystemExit("--observer-release-manifest may only be provided once")
        manifest = Path(value)
    if manifest is None:
        environment_value = os.environ.get("YFEISTAI_OBSERVER_RELEASE_MANIFEST")
        if environment_value:
            manifest = Path(environment_value)
    if manifest is not None and not manifest.is_absolute():
        raise SystemExit("observer release manifest path must be absolute")
    return manifest, compose_args


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
            or argument == "--scale"
            or argument.startswith("--scale=")
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
    command = [docker, "compose"]
    if platform:
        command.extend(
            platform_compose_topology_arguments(
                deployment_root=PROJECT_ROOT,
                candidate_root=candidate_root or PROJECT_ROOT,
                env_file=DOCKER_ENV_PATH,
            )
        )
    elif data_plane is not None:
        command.extend(("--env-file", str(DOCKER_ENV_PATH)))
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
    else:
        command.extend(("--env-file", str(DOCKER_ENV_PATH)))
    command.extend(compose_args)
    return command


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        args = ["up", "-d"]

    observer_manifest, args = _extract_observer_release_manifest(args)
    platform, data_plane, candidate_root, _ = _parse_topology_args(args)
    if platform and observer_manifest is None:
        raise SystemExit("a committed observer release manifest is required for platform Compose")
    if not platform and observer_manifest is not None:
        raise SystemExit("observer release manifest requires --platform")
    production_root = candidate_root or PROJECT_ROOT
    with _production_artifact_leases(
        platform=platform,
        data_plane=data_plane,
        candidate_root=production_root,
    ) as leases:
        if platform or data_plane is not None:
            _validate_production_image_lock(production_root, leases)
        observer_admission: dict[str, object] | None = None
        if platform:
            assert observer_manifest is not None
            observer_admission = _load_observer_release_admission(
                manifest_path=observer_manifest,
                candidate_root=production_root,
                leases=leases,
            )
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
            "YFEISTAI_CANDIDATE_ROOT",
            "YFEISTAI_OBSERVER_RELEASE_MANIFEST",
            *_OBSERVER_RUNTIME_ENVIRONMENT,
        ):
            env.pop(key, None)
        if platform or data_plane is not None:
            env["YFEISTAI_CANDIDATE_ROOT"] = str(production_root)
        if observer_admission is not None:
            environment = observer_admission.get("environment")
            if not isinstance(environment, dict) or set(environment) != set(
                _OBSERVER_RUNTIME_ENVIRONMENT
            ):
                raise SystemExit("observer release manifest environment is invalid")
            if any(not isinstance(value, str) or not value for value in environment.values()):
                raise SystemExit("observer release manifest environment is invalid")
            env.update(environment)

        command = _compose_command(args)
        if leases:
            command = _bind_compose_command_to_leases(command, leases)
            for lease in leases.values():
                lease.assert_unchanged()
        if observer_admission is not None:
            revalidate = observer_admission.get("_revalidate")
            if not callable(revalidate):
                raise SystemExit("observer release manifest revalidation is unavailable")
            try:
                revalidate()
            except (KeyError, TypeError, ValueError) as exc:
                raise SystemExit("observer release manifest changed before subprocess") from exc
        result = subprocess.run(command, cwd=str(PROJECT_ROOT), env=env, check=False)
        for lease in leases.values():
            lease.assert_unchanged()
        return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
