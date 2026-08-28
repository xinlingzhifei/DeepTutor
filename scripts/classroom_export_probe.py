"""Create one isolated teacher fixture and stage four controlled classroom exports."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from classroom_export_contract import (  # noqa: E402
    CLASSROOM_EXPORT_CONTENT_TYPES,
    CLASSROOM_EXPORT_KINDS,
    CLASSROOM_EXPORT_PATHS,
    CLASSROOM_EXPORT_PRODUCER,
    CLASSROOM_EXPORT_SCHEMA_VERSION,
    MAX_EXPORT_BYTES,
    canonical_classroom_export_report,
    derive_classroom_export_checks,
)
from render_platform_compose import validate_image_lock_bindings  # noqa: E402

_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CLEANUP_TIMEOUT_SECONDS = 30.0
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)

EXPORT_SPECS: dict[str, tuple[str, str, int]] = {
    kind: (
        CLASSROOM_EXPORT_PATHS[kind],
        CLASSROOM_EXPORT_CONTENT_TYPES[kind],
        MAX_EXPORT_BYTES[kind],
    )
    for kind in CLASSROOM_EXPORT_KINDS
}
_CONTROLLED_EXPORT_BASENAMES = frozenset(CLASSROOM_EXPORT_PATHS.values())


class ExportProbeError(RuntimeError):
    """A stable, secret-free probe error safe to emit on stderr."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ExportProbeError("classroom_export_arguments_invalid")


class ProbeConfig:
    __slots__ = (
        "admin_token",
        "base_url",
        "candidate",
        "candidate_root",
        "release_run",
        "staging_dir",
        "staging_identity",
        "tenant_id",
        "timeout_seconds",
    )

    def __init__(
        self,
        *,
        admin_token: SecretStr,
        base_url: str,
        candidate: Mapping[str, object],
        candidate_root: Path,
        release_run: Mapping[str, str],
        staging_dir: Path,
        timeout_seconds: int,
        tenant_id: str = "tenant-acceptance",
        staging_identity: tuple[int, int] | None = None,
    ) -> None:
        self.admin_token = admin_token
        self.base_url = base_url
        self.candidate = dict(candidate)
        self.candidate_root = candidate_root
        self.release_run = dict(release_run)
        self.staging_dir = staging_dir
        if staging_identity is None:
            stat = staging_dir.stat()
            staging_identity = (stat.st_dev, stat.st_ino)
        self.staging_identity = staging_identity
        self.tenant_id = tenant_id
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            "ProbeConfig(admin_token=SecretStr('**********'), "
            f"base_url={self.base_url!r}, candidate_root={self.candidate_root!r}, "
            f"release_run={self.release_run!r}, staging_dir={self.staging_dir!r}, "
            f"tenant_id={self.tenant_id!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


class IdentityCredential(NamedTuple):
    username: str
    user_id: str
    token: SecretStr


class FixtureMaterial(NamedTuple):
    run_key: str
    teacher_username: str
    teacher_password: SecretStr


class PublishedFixture(NamedTuple):
    tenant_id: str
    classroom_version_id: str
    document_sha256: str
    teacher: IdentityCredential


class ProbeState:
    __slots__ = (
        "mp4_policy_cleanup_revision",
        "mp4_policy_configured",
        "mp4_policy_enable_operation_id",
        "mp4_policy_original_exists",
        "mp4_policy_original",
        "mp4_policy_original_operation_id",
        "mp4_policy_original_revision",
        "mp4_policy_restore_operation_id",
        "teacher_identity",
        "teacher_user_id",
        "teacher_username",
        "tenant_id",
    )

    def __init__(self) -> None:
        self.mp4_policy_cleanup_revision: str | None = None
        self.mp4_policy_configured = False
        self.mp4_policy_enable_operation_id: str | None = None
        self.mp4_policy_original_exists: bool | None = None
        self.mp4_policy_original: bool | None = None
        self.mp4_policy_original_operation_id: str | None = None
        self.mp4_policy_original_revision: str | None = None
        self.mp4_policy_restore_operation_id: str | None = None
        self.teacher_identity: IdentityCredential | None = None
        self.teacher_user_id: str | None = None
        self.teacher_username: str | None = None
        self.tenant_id: str | None = None


CandidateLoader = Any


def _default_candidate_loader(candidate_root: Path) -> dict[str, object]:
    lock = validate_image_lock_bindings(
        candidate_root / "deploy" / "image-lock.json",
        compose_paths=(
            candidate_root / "docker-compose.platform.yml",
            candidate_root / "docker-compose.data-plane.yml",
        ),
        require_candidate=True,
    )
    candidate = lock.get("candidate")
    if not isinstance(candidate, dict):
        raise ExportProbeError("candidate_invalid")
    return json.loads(json.dumps(candidate))


def _valid_base_url(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.rstrip("/"):
        return False
    parsed = urlsplit(value)
    if parsed.scheme == "http":
        hostname = parsed.hostname
        if hostname != "localhost":
            try:
                if hostname is None or not ipaddress.ip_address(hostname).is_loopback:
                    return False
            except ValueError:
                return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )


def _is_link_or_reparse(path_stat: object) -> bool:
    return stat.S_ISLNK(int(getattr(path_stat, "st_mode"))) or bool(
        _REPARSE_POINT_ATTRIBUTE
        and int(getattr(path_stat, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE
    )


def _assert_no_link_or_reparse_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    for component in (*reversed(absolute.parents), absolute):
        try:
            component_stat = component.lstat()
        except OSError as exc:
            raise ExportProbeError("staging_directory_invalid") from exc
        if _is_link_or_reparse(component_stat):
            raise ExportProbeError("staging_directory_invalid")


def _staging_directory_identity(path: Path) -> tuple[Path, tuple[int, int]]:
    _assert_no_link_or_reparse_components(path)
    try:
        resolved = path.resolve(strict=True)
        path_stat = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise ExportProbeError("staging_directory_invalid") from exc
    if resolved != path or _is_link_or_reparse(path_stat) or not stat.S_ISDIR(path_stat.st_mode):
        raise ExportProbeError("staging_directory_invalid")
    return resolved, (path_stat.st_dev, path_stat.st_ino)


def _assert_staging_directory_identity(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    resolved, observed_identity = _staging_directory_identity(path)
    if resolved != path or observed_identity != expected_identity:
        raise ExportProbeError("staging_directory_invalid")


def _open_staging_directory_anchor(
    path: Path,
    expected_identity: tuple[int, int],
) -> int | None:
    """Anchor POSIX opens; Windows uses fail-closed pre/post identity checks.

    Python does not expose a portable Windows ``openat`` equivalent.  On that
    platform the caller therefore verifies both the directory identity and the
    final file-handle identity before and after publication.
    """

    if os.open not in os.supports_dir_fd:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        path_stat = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ExportProbeError("staging_directory_invalid") from exc
    if (path_stat.st_dev, path_stat.st_ino) != expected_identity:
        os.close(descriptor)
        raise ExportProbeError("staging_directory_invalid")
    return descriptor


def _assert_published_file_identity(
    target: Path,
    expected_identity: tuple[int, int],
) -> None:
    try:
        target_stat = target.lstat()
    except OSError as exc:
        raise ExportProbeError("staging_directory_invalid") from exc
    if (
        _is_link_or_reparse(target_stat)
        or not stat.S_ISREG(target_stat.st_mode)
        or (target_stat.st_dev, target_stat.st_ino) != expected_identity
    ):
        raise ExportProbeError("staging_directory_invalid")


def _load_config(
    environ: Mapping[str, str],
    *,
    cwd: Path,
    candidate_loader: CandidateLoader = _default_candidate_loader,
) -> ProbeConfig:
    raw_root = environ.get("YFEISTAI_CANDIDATE_ROOT")
    if not isinstance(raw_root, str) or not raw_root:
        raise ExportProbeError("candidate_root_invalid")
    try:
        candidate_root = Path(raw_root).resolve(strict=True)
        current_root = cwd.resolve(strict=True)
    except OSError as exc:
        raise ExportProbeError("candidate_root_invalid") from exc
    if candidate_root != current_root:
        raise ExportProbeError("candidate_root_invalid")

    raw_staging = environ.get("YFEISTAI_CLASSROOM_EXPORT_STAGING_DIR")
    if not isinstance(raw_staging, str) or not raw_staging:
        raise ExportProbeError("staging_directory_invalid")
    staging_path = Path(raw_staging)
    if not staging_path.is_absolute():
        raise ExportProbeError("staging_directory_invalid")
    try:
        _assert_no_link_or_reparse_components(staging_path)
        staging_dir = staging_path.resolve(strict=True)
        _assert_no_link_or_reparse_components(staging_dir)
        staging_stat = staging_dir.lstat()
        entries = tuple(staging_dir.iterdir())
    except ExportProbeError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ExportProbeError("staging_directory_invalid") from exc
    if (
        _is_link_or_reparse(staging_stat)
        or not stat.S_ISDIR(staging_stat.st_mode)
        or staging_dir == candidate_root
        or candidate_root in staging_dir.parents
        or entries
    ):
        raise ExportProbeError("staging_directory_invalid")

    token = environ.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ExportProbeError("fixture_token_unavailable")

    release_run: dict[str, str] = {}
    for field, name in (
        ("runId", "YFEISTAI_RELEASE_RUN_ID"),
        ("environmentId", "YFEISTAI_ENVIRONMENT_ID"),
    ):
        value = environ.get(name)
        if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
            raise ExportProbeError("release_identity_invalid")
        release_run[field] = value

    base_url = environ.get("WEB_BASE_URL")
    if not _valid_base_url(base_url):
        raise ExportProbeError("base_url_invalid")
    assert isinstance(base_url, str)

    tenant_id = environ.get("YFEISTAI_ACCEPTANCE_TENANT_ID")
    if not isinstance(tenant_id, str) or _PUBLIC_ID.fullmatch(tenant_id) is None:
        raise ExportProbeError("acceptance_tenant_invalid")

    raw_timeout = environ.get("YFEISTAI_CLASSROOM_EXPORT_TIMEOUT_SECONDS")
    try:
        timeout_seconds = int(raw_timeout or "")
    except ValueError as exc:
        raise ExportProbeError("timeout_invalid") from exc
    if timeout_seconds < 60 or timeout_seconds > 86_400:
        raise ExportProbeError("timeout_invalid")

    try:
        candidate = dict(candidate_loader(candidate_root))
    except ExportProbeError:
        raise
    except Exception as exc:
        raise ExportProbeError("candidate_invalid") from exc
    if not candidate:
        raise ExportProbeError("candidate_invalid")
    return ProbeConfig(
        admin_token=SecretStr(token.strip()),
        base_url=base_url,
        candidate=candidate,
        candidate_root=candidate_root,
        release_run=release_run,
        staging_dir=staging_dir,
        tenant_id=tenant_id,
        staging_identity=(staging_stat.st_dev, staging_stat.st_ino),
        timeout_seconds=timeout_seconds,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("first-release",))
    return parser.parse_args(argv)


class ExportApi:
    """Minimal, redirect-free HTTP boundary for the candidate under test."""

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url
        self._transport = transport
        timeout = httpx.Timeout(timeout_seconds)
        self._admin_client = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )
        self._identity_client = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> ExportApi:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._admin_client.aclose()
        await self._identity_client.aclose()

    @staticmethod
    def _path(path: str) -> str:
        if not path.startswith("/api/v1/") or path.startswith("//"):
            raise ExportProbeError("request_path_invalid")
        return path

    @staticmethod
    def _response_json(
        response: httpx.Response,
        *,
        expected_statuses: frozenset[int],
    ) -> dict[str, Any]:
        if response.status_code not in expected_statuses:
            raise ExportProbeError("candidate_request_rejected")
        try:
            body = response.json()
        except (UnicodeError, ValueError) as exc:
            raise ExportProbeError("candidate_response_invalid") from exc
        if not isinstance(body, dict):
            raise ExportProbeError("candidate_response_invalid")
        return body

    @staticmethod
    def _response_json_list(
        response: httpx.Response,
        *,
        expected_statuses: frozenset[int],
    ) -> list[Any]:
        if response.status_code not in expected_statuses:
            raise ExportProbeError("candidate_request_rejected")
        try:
            body = response.json()
        except (UnicodeError, ValueError) as exc:
            raise ExportProbeError("candidate_response_invalid") from exc
        if not isinstance(body, list):
            raise ExportProbeError("candidate_response_invalid")
        return body

    async def _json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        kwargs: dict[str, object] = {"headers": dict(headers or {})}
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            response = await client.request(method, self._path(path), **kwargs)
        except httpx.HTTPError as exc:
            raise ExportProbeError("candidate_request_failed") from exc
        return self._response_json(response, expected_statuses=expected_statuses)

    async def admin_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        return await self._json(
            self._admin_client,
            method,
            path,
            headers=headers,
            json_body=json_body,
            expected_statuses=expected_statuses,
        )

    async def admin_list_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        expected_statuses: frozenset[int] = frozenset({200}),
    ) -> list[Any]:
        try:
            response = await self._admin_client.request(
                method,
                self._path(path),
                headers=dict(headers or {}),
            )
        except httpx.HTTPError as exc:
            raise ExportProbeError("candidate_request_failed") from exc
        return self._response_json_list(
            response,
            expected_statuses=expected_statuses,
        )

    @staticmethod
    def _tenant_headers(tenant_id: str) -> dict[str, str]:
        if _PUBLIC_ID.fullmatch(tenant_id) is None:
            raise ExportProbeError("tenant_id_invalid")
        return {"X-Tenant-ID": tenant_id, "Cookie": f"dt_tenant={tenant_id}"}

    async def tenant_admin_json(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        bound = {**dict(headers or {}), **self._tenant_headers(tenant_id)}
        return await self.admin_json(
            method,
            path,
            headers=bound,
            json_body=json_body,
            expected_statuses=expected_statuses,
        )

    @staticmethod
    def _identity_headers(
        identity: IdentityCredential,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        bound = {
            **dict(headers or {}),
            "X-Tenant-ID": tenant_id,
            "Cookie": (f"dt_token={identity.token.get_secret_value()}; dt_tenant={tenant_id}"),
        }
        bound.pop("Authorization", None)
        return bound

    async def login_identity(
        self,
        username: str,
        password: SecretStr,
    ) -> IdentityCredential:
        if _PUBLIC_ID.fullmatch(username) is None:
            raise ExportProbeError("identity_invalid")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                follow_redirects=False,
                timeout=self._identity_client.timeout,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": username,
                        "password": password.get_secret_value(),
                    },
                )
        except httpx.HTTPError as exc:
            raise ExportProbeError("identity_login_failed") from exc
        body = self._response_json(response, expected_statuses=frozenset({200}))
        token = response.cookies.get("dt_token")
        user_id = body.get("user_id")
        if (
            body.get("ok") is not True
            or body.get("username") != username
            or body.get("role") != "user"
            or body.get("is_admin") is not False
            or not isinstance(user_id, str)
            or _PUBLIC_ID.fullmatch(user_id) is None
            or not isinstance(token, str)
            or not token
        ):
            raise ExportProbeError("identity_login_failed")
        return IdentityCredential(username, user_id, SecretStr(token))

    async def tenant_identity_json(
        self,
        method: str,
        path: str,
        *,
        identity: IdentityCredential,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        if _PUBLIC_ID.fullmatch(tenant_id) is None:
            raise ExportProbeError("tenant_id_invalid")
        return await self._json(
            self._identity_client,
            method,
            path,
            headers=self._identity_headers(identity, tenant_id, headers),
            json_body=json_body,
            expected_statuses=expected_statuses,
        )

    async def tenant_identity_download(
        self,
        path: str,
        *,
        identity: IdentityCredential,
        tenant_id: str,
        target: Path,
        expected_filename: str,
        expected_content_type: str,
        max_bytes: int,
    ) -> dict[str, object]:
        expected_disposition = f"attachment; filename*=UTF-8''{expected_filename}"
        if expected_filename not in _CONTROLLED_EXPORT_BASENAMES:
            raise ExportProbeError("staging_directory_invalid")
        parent, parent_identity = _staging_directory_identity(target.parent)
        if target.parent != parent or target != parent / expected_filename:
            raise ExportProbeError("staging_directory_invalid")
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ExportProbeError("staging_directory_invalid") from exc
        else:
            raise ExportProbeError("export_staging_conflict")

        directory_descriptor: int | None = None
        try:
            directory_descriptor = _open_staging_directory_anchor(
                parent,
                parent_identity,
            )
            with tempfile.TemporaryFile(mode="w+b") as spool:
                async with self._identity_client.stream(
                    "GET",
                    self._path(path),
                    headers=self._identity_headers(identity, tenant_id),
                ) as response:
                    if response.status_code != 200:
                        raise ExportProbeError("export_download_rejected")
                    observed_content_type = response.headers.get("content-type")
                    allowed_content_types = {expected_content_type}
                    if expected_content_type == "text/html":
                        allowed_content_types.add("text/html; charset=utf-8")
                    if (
                        observed_content_type not in allowed_content_types
                        or response.headers.get("content-disposition") != expected_disposition
                        or response.headers.get("content-encoding") not in {None, "identity"}
                    ):
                        raise ExportProbeError("export_download_invalid")
                    raw_length = response.headers.get("content-length")
                    if raw_length is not None:
                        try:
                            declared_length = int(raw_length)
                        except ValueError as exc:
                            raise ExportProbeError("export_download_invalid") from exc
                        if declared_length <= 0:
                            raise ExportProbeError("export_download_invalid")
                        if declared_length > max_bytes:
                            raise ExportProbeError("export_download_too_large")
                    else:
                        declared_length = None
                    byte_length = 0
                    digest = hashlib.sha256()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        byte_length += len(chunk)
                        if byte_length > max_bytes:
                            raise ExportProbeError("export_download_too_large")
                        spool.write(chunk)
                        digest.update(chunk)
                if not byte_length or (
                    declared_length is not None and byte_length != declared_length
                ):
                    raise ExportProbeError("export_download_invalid")
                _assert_staging_directory_identity(parent, parent_identity)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    if directory_descriptor is None:
                        descriptor = os.open(target, flags, 0o600)
                    else:
                        descriptor = os.open(
                            expected_filename,
                            flags,
                            0o600,
                            dir_fd=directory_descriptor,
                        )
                except FileExistsError as exc:
                    raise ExportProbeError("export_staging_conflict") from exc
                spool.seek(0)
                with os.fdopen(descriptor, "wb") as output:
                    output_stat = os.fstat(output.fileno())
                    if not stat.S_ISREG(output_stat.st_mode):
                        raise ExportProbeError("staging_directory_invalid")
                    output_identity = (output_stat.st_dev, output_stat.st_ino)
                    while chunk := spool.read(64 * 1024):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                    _assert_staging_directory_identity(parent, parent_identity)
                    _assert_published_file_identity(target, output_identity)
                _assert_staging_directory_identity(parent, parent_identity)
                _assert_published_file_identity(target, output_identity)
        except ExportProbeError:
            raise
        except httpx.HTTPError as exc:
            raise ExportProbeError("candidate_request_failed") from exc
        except OSError as exc:
            raise ExportProbeError("export_staging_failed") from exc
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
        return {
            "contentType": expected_content_type,
            "contentDisposition": expected_disposition,
            "byteLength": byte_length,
            "sha256": digest.hexdigest(),
        }


def _exact_keys(value: object, expected: frozenset[str], error: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ExportProbeError(error)
    return value


def _public_id(value: object, error: str) -> str:
    if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
        raise ExportProbeError(error)
    return value


def _remaining(end_time: float) -> float:
    remaining = end_time - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fixture_material(config: ProbeConfig) -> FixtureMaterial:
    nonce = secrets.token_bytes(32)
    binding = json.dumps(
        {"candidate": config.candidate, "releaseRun": config.release_run},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    token = config.admin_token.get_secret_value().encode("utf-8")
    digest = hmac.new(token, binding + nonce, hashlib.sha256).hexdigest()
    password = hmac.new(token, f"{digest}:teacher".encode(), hashlib.sha256).hexdigest()
    suffix = digest[:16]
    return FixtureMaterial(
        run_key=f"export-{suffix}",
        teacher_username=f"export-teacher-{suffix}",
        teacher_password=SecretStr(f"Exp9!{password}"),
    )


async def _create_identity(
    api: ExportApi,
    *,
    username: str,
    password: SecretStr,
) -> str:
    body = _exact_keys(
        await api.admin_json(
            "POST",
            "/api/v1/auth/users",
            json_body={"username": username, "password": password.get_secret_value()},
            expected_statuses=frozenset({201}),
        ),
        frozenset({"ok", "user_id", "username", "role", "is_admin"}),
        "identity_create_invalid",
    )
    user_id = _public_id(body.get("user_id"), "identity_create_invalid")
    if (
        body.get("ok") is not True
        or body.get("username") != username
        or body.get("role") != "user"
        or body.get("is_admin") is not False
    ):
        raise ExportProbeError("identity_create_invalid")
    return user_id


_CLASSROOM_KEYS = frozenset(
    {
        "assetId",
        "draftId",
        "jobId",
        "lifecycleState",
        "status",
        "title",
        "courseId",
        "classId",
        "ownerId",
        "revision",
        "outline",
        "document",
        "classroomVersionId",
        "confirmedOutlineSha256",
        "validationReport",
        "idempotencyKey",
    }
)


def _classroom_response(raw: object) -> dict[str, Any]:
    body = _exact_keys(raw, _CLASSROOM_KEYS, "classroom_fixture_invalid")
    _public_id(body.get("assetId"), "classroom_fixture_invalid")
    _public_id(body.get("draftId"), "classroom_fixture_invalid")
    if isinstance(body.get("revision"), bool) or not isinstance(body.get("revision"), int):
        raise ExportProbeError("classroom_fixture_invalid")
    return body


def _confirmed_classroom_response(
    raw: object,
    *,
    asset_id: str,
    draft_id: str,
    owner_id: str,
) -> dict[str, Any]:
    body = _classroom_response(raw)
    if (
        body.get("assetId") != asset_id
        or body.get("draftId") != draft_id
        or body.get("ownerId") != owner_id
        or body.get("lifecycleState") != "generating_content"
    ):
        raise ExportProbeError("classroom_fixture_invalid")
    return body


async def _wait_for_generated_classroom(
    api: ExportApi,
    *,
    asset_id: str,
    identity: IdentityCredential,
    tenant_id: str,
    end_time: float,
) -> dict[str, Any]:
    while True:
        body = _classroom_response(
            await api.tenant_identity_json(
                "GET",
                f"/api/v1/classrooms/{asset_id}",
                identity=identity,
                tenant_id=tenant_id,
                expected_statuses=frozenset({200}),
            )
        )
        if body.get("status") in {"failed", "canceled"}:
            raise ExportProbeError("classroom_generation_failed")
        if (
            body.get("assetId") == asset_id
            and body.get("status") == "succeeded"
            and body.get("lifecycleState") == "editing"
            and isinstance(body.get("document"), dict)
        ):
            return body
        await asyncio.sleep(min(0.25, _remaining(end_time)))


def _policy_response(
    body: Mapping[str, object],
    *,
    tenant_id: str,
) -> tuple[bool, bool, str, str | None]:
    if set(body) != {
        "tenant_id",
        "allow_mp4",
        "exists",
        "revision",
        "operation_id",
    }:
        raise ExportProbeError("export_policy_invalid")
    allow_mp4 = body.get("allow_mp4")
    exists = body.get("exists")
    revision = body.get("revision")
    operation_id = body.get("operation_id")
    if (
        body.get("tenant_id") != tenant_id
        or not isinstance(allow_mp4, bool)
        or not isinstance(exists, bool)
        or not isinstance(revision, str)
        or (revision != "absent" and _SHA256.fullmatch(revision) is None)
        or (
            operation_id is not None
            and (
                not isinstance(operation_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None
            )
        )
        or (exists and revision == "absent")
        or (not exists and (allow_mp4 or ((revision == "absent") != (operation_id is None))))
    ):
        raise ExportProbeError("export_policy_invalid")
    return allow_mp4, exists, revision, operation_id


def _clear_mp4_policy_mutation(state: ProbeState) -> None:
    state.mp4_policy_cleanup_revision = None
    state.mp4_policy_configured = False
    state.mp4_policy_enable_operation_id = None
    state.mp4_policy_restore_operation_id = None


def _matches_original_policy(
    current: tuple[bool, bool, str, str | None],
    state: ProbeState,
) -> bool:
    return current == (
        state.mp4_policy_original,
        state.mp4_policy_original_exists,
        state.mp4_policy_original_revision,
        state.mp4_policy_original_operation_id,
    )


async def _configure_mp4_policy(api: ExportApi, *, state: ProbeState) -> None:
    if state.tenant_id is None:
        raise ExportProbeError("export_policy_invalid")
    allow_mp4, exists, revision, operation_id = _policy_response(
        await api.tenant_admin_json(
            "GET",
            "/api/v1/classroom-export-policy",
            tenant_id=state.tenant_id,
            expected_statuses=frozenset({200}),
        ),
        tenant_id=state.tenant_id,
    )
    state.mp4_policy_original = allow_mp4
    state.mp4_policy_original_exists = exists
    state.mp4_policy_original_revision = revision
    state.mp4_policy_original_operation_id = operation_id
    if allow_mp4:
        return
    enable_operation_id = secrets.token_hex(16)
    state.mp4_policy_enable_operation_id = enable_operation_id
    try:
        enabled_state = _policy_response(
            await api.tenant_admin_json(
                "PUT",
                "/api/v1/classroom-export-policy",
                tenant_id=state.tenant_id,
                json_body={
                    "allow_mp4": True,
                    "expected_revision": revision,
                    "operation_id": enable_operation_id,
                },
                expected_statuses=frozenset({200}),
            ),
            tenant_id=state.tenant_id,
        )
    except ExportProbeError:
        enabled_state = _policy_response(
            await api.tenant_admin_json(
                "GET",
                "/api/v1/classroom-export-policy",
                tenant_id=state.tenant_id,
                expected_statuses=frozenset({200}),
            ),
            tenant_id=state.tenant_id,
        )
        if enabled_state[3] != enable_operation_id:
            raise
    enabled, enabled_exists, enabled_revision, enabled_operation_id = enabled_state
    if (
        not enabled
        or not enabled_exists
        or enabled_revision == revision
        or enabled_operation_id != enable_operation_id
    ):
        raise ExportProbeError("export_policy_invalid")
    state.mp4_policy_cleanup_revision = enabled_revision
    state.mp4_policy_configured = True


async def _create_fixture(
    config: ProbeConfig,
    api: ExportApi,
    *,
    material: FixtureMaterial,
    state: ProbeState,
    end_time: float,
) -> PublishedFixture:
    state.teacher_username = material.teacher_username
    teacher_user_id = await _create_identity(
        api,
        username=material.teacher_username,
        password=material.teacher_password,
    )
    state.teacher_user_id = teacher_user_id
    teacher = await api.login_identity(material.teacher_username, material.teacher_password)
    if teacher.user_id != teacher_user_id:
        raise ExportProbeError("identity_login_failed")
    state.teacher_identity = teacher
    tenant_id = config.tenant_id
    state.tenant_id = tenant_id

    member = _exact_keys(
        await api.tenant_admin_json(
            "POST",
            f"/api/v1/tenants/{tenant_id}/members",
            tenant_id=tenant_id,
            json_body={"user_id": teacher_user_id, "role": "teacher"},
            expected_statuses=frozenset({200}),
        ),
        frozenset({"tenant_id", "user_id", "roles", "grants"}),
        "tenant_fixture_invalid",
    )
    if (
        member.get("tenant_id") != tenant_id
        or member.get("user_id") != teacher_user_id
        or member.get("roles") != ["teacher"]
        or member.get("grants")
        != [{"role": "teacher", "scope_type": "tenant", "scope_id": tenant_id}]
    ):
        raise ExportProbeError("tenant_fixture_invalid")

    await _configure_mp4_policy(api, state=state)

    suffix = material.run_key.removeprefix("export-")
    course_id = f"course-{suffix}"
    class_id = f"class-{suffix}"
    course = _exact_keys(
        await api.tenant_admin_json(
            "POST",
            "/api/v1/teaching/courses",
            tenant_id=tenant_id,
            json_body={"id": course_id, "title": "Classroom export acceptance"},
            expected_statuses=frozenset({201}),
        ),
        frozenset({"id", "title", "status", "createdAt"}),
        "tenant_fixture_invalid",
    )
    if course.get("id") != course_id or course.get("status") != "active":
        raise ExportProbeError("tenant_fixture_invalid")
    classroom = _exact_keys(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/teaching/courses/{course_id}/classes",
            identity=teacher,
            tenant_id=tenant_id,
            json_body={"id": class_id, "name": "Classroom export acceptance"},
            expected_statuses=frozenset({201}),
        ),
        frozenset({"id", "courseId", "name", "status", "createdAt"}),
        "tenant_fixture_invalid",
    )
    if (
        classroom.get("id") != class_id
        or classroom.get("courseId") != course_id
        or classroom.get("status") != "active"
    ):
        raise ExportProbeError("tenant_fixture_invalid")
    enrollment = _exact_keys(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/teaching/classes/{class_id}/enrollments",
            identity=teacher,
            tenant_id=tenant_id,
            json_body={"userId": teacher_user_id},
            expected_statuses=frozenset({201}),
        ),
        frozenset({"classId", "userId", "status", "createdAt"}),
        "tenant_fixture_invalid",
    )
    if (
        enrollment.get("classId") != class_id
        or enrollment.get("userId") != teacher_user_id
        or enrollment.get("status") != "active"
    ):
        raise ExportProbeError("tenant_fixture_invalid")

    quota = _exact_keys(
        await api.tenant_admin_json(
            "POST",
            "/api/v1/teaching/generation-quota-grants",
            tenant_id=tenant_id,
            headers={"Idempotency-Key": f"{material.run_key}-quota"},
            json_body={"units": 20},
            expected_statuses=frozenset({200}),
        ),
        frozenset({"grantId", "tenantId", "units", "balance", "created"}),
        "generation_prerequisites_invalid",
    )
    if (
        quota.get("tenantId") != tenant_id
        or quota.get("units") != 20
        or isinstance(quota.get("balance"), bool)
        or not isinstance(quota.get("balance"), int)
        or int(quota["balance"]) < 20
        or not isinstance(quota.get("created"), bool)
    ):
        raise ExportProbeError("generation_prerequisites_invalid")

    created = _classroom_response(
        await api.tenant_identity_json(
            "POST",
            "/api/v1/classrooms",
            identity=teacher,
            tenant_id=tenant_id,
            headers={"Idempotency-Key": f"{material.run_key}-classroom"},
            json_body={
                "title": "Classroom export acceptance",
                "courseId": course_id,
                "classId": class_id,
                "objective": "Verify all controlled classroom export formats",
                "gradeBand": "grade-8",
                "audience": "intermediate",
                "durationMinutes": 15,
                "classroomMode": "full",
                "webPolicy": "disabled",
                "mediaPolicy": "text_only",
                "templateId": "first-release-acceptance",
                "templateVersion": "1",
                "knowledgePoints": [
                    {
                        "knowledgePointId": "kp-export-acceptance",
                        "title": "Controlled classroom exports",
                        "description": "Explain how immutable classroom exports are verified",
                    }
                ],
                "contentMode": "open_creation",
                "openCreationAcknowledged": True,
                "requestedExports": list(CLASSROOM_EXPORT_KINDS),
            },
            expected_statuses=frozenset({202}),
        )
    )
    asset_id = _public_id(created.get("assetId"), "classroom_fixture_invalid")
    draft_id = _public_id(created.get("draftId"), "classroom_fixture_invalid")
    if created.get("ownerId") != teacher_user_id or created.get("outline") is None:
        raise ExportProbeError("classroom_fixture_invalid")
    _confirmed_classroom_response(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/classrooms/{asset_id}/confirm-outline",
            identity=teacher,
            tenant_id=tenant_id,
            expected_statuses=frozenset({202}),
        ),
        asset_id=asset_id,
        draft_id=draft_id,
        owner_id=teacher_user_id,
    )
    generated = await _wait_for_generated_classroom(
        api,
        asset_id=asset_id,
        identity=teacher,
        tenant_id=tenant_id,
        end_time=end_time,
    )
    validated = _classroom_response(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/classrooms/{asset_id}/validate",
            identity=teacher,
            tenant_id=tenant_id,
            expected_statuses=frozenset({200}),
        )
    )
    validation = validated.get("validationReport")
    if (
        validated.get("revision") != generated.get("revision")
        or not isinstance(validation, dict)
        or validation.get("valid") is not True
    ):
        raise ExportProbeError("classroom_validation_failed")

    review = _exact_keys(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/classrooms/{asset_id}/submit",
            identity=teacher,
            tenant_id=tenant_id,
            headers={"Idempotency-Key": f"{material.run_key}-review"},
            json_body={"scope": "class", "classId": class_id},
            expected_statuses=frozenset({201}),
        ),
        frozenset(
            {
                "id",
                "assetId",
                "draftId",
                "draftRevision",
                "documentSha256",
                "validationReportSha256",
                "submittedBy",
                "scope",
                "classId",
                "status",
                "warnings",
                "reviewerId",
                "comment",
            }
        ),
        "classroom_review_invalid",
    )
    review_id = _public_id(review.get("id"), "classroom_review_invalid")
    if review.get("assetId") != asset_id or review.get("status") != "pending":
        raise ExportProbeError("classroom_review_invalid")
    approved = _exact_keys(
        await api.tenant_admin_json(
            "POST",
            f"/api/v1/classroom-reviews/{review_id}/approve",
            tenant_id=tenant_id,
            json_body={"comment": "First-release export acceptance"},
            expected_statuses=frozenset({200}),
        ),
        frozenset(review),
        "classroom_review_invalid",
    )
    if approved.get("id") != review_id or approved.get("status") != "approved":
        raise ExportProbeError("classroom_review_invalid")

    published = _exact_keys(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/classrooms/{asset_id}/publish",
            identity=teacher,
            tenant_id=tenant_id,
            headers={"Idempotency-Key": f"{material.run_key}-publish"},
            json_body={"scope": "class", "classId": class_id},
            expected_statuses=frozenset({201}),
        ),
        frozenset(
            {
                "versionId",
                "assetId",
                "versionNumber",
                "documentSha256",
                "publicationScope",
                "classId",
                "idempotencyKey",
            }
        ),
        "classroom_publish_invalid",
    )
    version_id = _public_id(published.get("versionId"), "classroom_publish_invalid")
    document_sha256 = published.get("documentSha256")
    if (
        published.get("assetId") != asset_id
        or published.get("publicationScope") != "class"
        or published.get("classId") != class_id
        or not isinstance(document_sha256, str)
        or _SHA256.fullmatch(document_sha256) is None
    ):
        raise ExportProbeError("classroom_publish_invalid")
    return PublishedFixture(tenant_id, version_id, document_sha256, teacher)


_EXPORT_STATUS_KEYS = frozenset(
    {
        "job_id",
        "job_kind",
        "phase",
        "status",
        "progress_percent",
        "waiting_reason",
        "cancellable",
        "retryable",
        "outline",
        "error_category",
        "error_code",
        "retry_of_job_id",
        "export_format",
        "download_ready",
    }
)


def _export_status(
    raw: object,
    *,
    export_id: str | None,
    export_format: str,
) -> dict[str, Any]:
    body = _exact_keys(raw, _EXPORT_STATUS_KEYS, "export_status_invalid")
    job_id = _public_id(body.get("job_id"), "export_status_invalid")
    status = body.get("status")
    progress = body.get("progress_percent")
    if (
        (export_id is not None and job_id != export_id)
        or body.get("job_kind") != "export"
        or body.get("phase") != "export"
        or body.get("export_format") != export_format
        or status
        not in {
            "created",
            "quota_reserved",
            "queued",
            "claimed",
            "exporting",
            "succeeded",
            "failed",
            "canceled",
        }
        or isinstance(progress, bool)
        or not isinstance(progress, int)
        or not 0 <= progress <= 100
    ):
        raise ExportProbeError("export_status_invalid")
    if status == "succeeded" and (
        progress != 100
        or body.get("download_ready") is not True
        or body.get("cancellable") is not False
    ):
        raise ExportProbeError("export_status_invalid")
    if status != "succeeded" and body.get("download_ready") is not False:
        raise ExportProbeError("export_status_invalid")
    return body


async def _wait_for_export(
    api: ExportApi,
    *,
    export_id: str,
    export_format: str,
    identity: IdentityCredential,
    tenant_id: str,
    end_time: float,
) -> dict[str, Any]:
    while True:
        body = _export_status(
            await api.tenant_identity_json(
                "GET",
                f"/api/v1/classroom-exports/{export_id}",
                identity=identity,
                tenant_id=tenant_id,
                expected_statuses=frozenset({200}),
            ),
            export_id=export_id,
            export_format=export_format,
        )
        if body.get("status") in {"failed", "canceled"}:
            raise ExportProbeError("export_job_failed")
        if body.get("status") == "succeeded":
            return body
        await asyncio.sleep(min(0.25, _remaining(end_time)))


def _assert_staging_identity(
    config: ProbeConfig,
    *,
    expected_names: frozenset[str] = frozenset(),
) -> None:
    _assert_staging_directory_identity(config.staging_dir, config.staging_identity)
    try:
        entries = tuple(config.staging_dir.iterdir())
    except OSError as exc:
        raise ExportProbeError("staging_directory_invalid") from exc
    if {entry.name for entry in entries} != expected_names:
        raise ExportProbeError("staging_directory_invalid")
    for entry in entries:
        try:
            entry_stat = entry.lstat()
            if _is_link_or_reparse(entry_stat) or not stat.S_ISREG(entry_stat.st_mode):
                raise ExportProbeError("staging_directory_invalid")
        except OSError as exc:
            raise ExportProbeError("staging_directory_invalid") from exc


async def _stage_exports(
    config: ProbeConfig,
    api: ExportApi,
    *,
    material: FixtureMaterial,
    fixture: PublishedFixture,
    end_time: float,
) -> dict[str, dict[str, object]]:
    exports: dict[str, dict[str, object]] = {}
    for export_format, (filename, content_type, max_bytes) in EXPORT_SPECS.items():
        _assert_staging_identity(
            config,
            expected_names=frozenset(str(item["relativePath"]) for item in exports.values()),
        )
        target = config.staging_dir / filename
        if target.exists() or target.is_symlink():
            raise ExportProbeError("export_staging_conflict")
        created = _export_status(
            await api.tenant_identity_json(
                "POST",
                f"/api/v1/classroom-versions/{fixture.classroom_version_id}/exports",
                identity=fixture.teacher,
                tenant_id=fixture.tenant_id,
                headers={"Idempotency-Key": f"{material.run_key}-{export_format}"},
                json_body={"format": export_format},
                expected_statuses=frozenset({202}),
            ),
            export_id=None,
            export_format=export_format,
        )
        if created.get("status") in {"failed", "canceled"}:
            raise ExportProbeError("export_job_failed")
        export_id = str(created["job_id"])
        terminal = (
            created
            if created.get("status") == "succeeded"
            else await _wait_for_export(
                api,
                export_id=export_id,
                export_format=export_format,
                identity=fixture.teacher,
                tenant_id=fixture.tenant_id,
                end_time=end_time,
            )
        )
        download = await api.tenant_identity_download(
            f"/api/v1/classroom-exports/{export_id}/download",
            identity=fixture.teacher,
            tenant_id=fixture.tenant_id,
            target=target,
            expected_filename=filename,
            expected_content_type=content_type,
            max_bytes=max_bytes,
        )
        exports[export_format] = {
            "exportId": export_id,
            "jobId": str(terminal["job_id"]),
            "status": str(terminal["status"]),
            "progressPercent": int(terminal["progress_percent"]),
            "downloadReady": bool(terminal["download_ready"]),
            "relativePath": filename,
            **download,
        }
    _assert_staging_identity(
        config,
        expected_names=frozenset(CLASSROOM_EXPORT_PATHS.values()),
    )
    return exports


async def _execute_export_probe(
    config: ProbeConfig,
    api: ExportApi,
    *,
    material: FixtureMaterial,
    state: ProbeState,
    end_time: float,
) -> bytes:
    fixture = await _create_fixture(
        config,
        api,
        material=material,
        state=state,
        end_time=end_time,
    )
    exports = await _stage_exports(
        config,
        api,
        material=material,
        fixture=fixture,
        end_time=end_time,
    )
    report = {
        "schemaVersion": CLASSROOM_EXPORT_SCHEMA_VERSION,
        "producer": CLASSROOM_EXPORT_PRODUCER,
        "candidate": config.candidate,
        "releaseRun": config.release_run,
        "observedAt": _observed_at(),
        "baseUrl": config.base_url,
        "tenantId": fixture.tenant_id,
        "classroomVersionId": fixture.classroom_version_id,
        "documentSha256": fixture.document_sha256,
        "exports": exports,
    }
    body = canonical_classroom_export_report(report)
    checks = derive_classroom_export_checks(
        body,
        artifact_root=config.staging_dir,
        candidate=config.candidate,
        release_run=config.release_run,
        expected_base_url=config.base_url,
    )
    if not checks or any(value is not True for value in checks.values()):
        raise ExportProbeError("classroom_exports_failed")
    return body


async def _cleanup_resources(api: ExportApi, state: ProbeState) -> None:
    if not state.mp4_policy_configured and state.mp4_policy_enable_operation_id is None:
        return
    if (
        state.tenant_id is None
        or state.mp4_policy_original is None
        or state.mp4_policy_original_exists is None
        or state.mp4_policy_original_revision is None
    ):
        raise ExportProbeError("resource_cleanup_failed")
    if state.mp4_policy_cleanup_revision is None:
        current = _policy_response(
            await api.tenant_admin_json(
                "GET",
                "/api/v1/classroom-export-policy",
                tenant_id=state.tenant_id,
                expected_statuses=frozenset({200}),
            ),
            tenant_id=state.tenant_id,
        )
        if _matches_original_policy(current, state):
            _clear_mp4_policy_mutation(state)
            return
        if (
            current[0] is not True
            or current[1] is not True
            or current[3] != state.mp4_policy_enable_operation_id
        ):
            raise ExportProbeError("resource_cleanup_failed")
        state.mp4_policy_cleanup_revision = current[2]
        state.mp4_policy_configured = True
    method = "PUT" if state.mp4_policy_original_exists else "DELETE"
    json_body: dict[str, object] = {"expected_revision": state.mp4_policy_cleanup_revision}
    restore_operation_id = secrets.token_hex(16)
    state.mp4_policy_restore_operation_id = restore_operation_id
    json_body["operation_id"] = restore_operation_id
    if method == "PUT":
        json_body["allow_mp4"] = state.mp4_policy_original
    try:
        restored_state = _policy_response(
            await api.tenant_admin_json(
                method,
                "/api/v1/classroom-export-policy",
                tenant_id=state.tenant_id,
                json_body=json_body,
                expected_statuses=frozenset({200}),
            ),
            tenant_id=state.tenant_id,
        )
    except ExportProbeError:
        restored_state = _policy_response(
            await api.tenant_admin_json(
                "GET",
                "/api/v1/classroom-export-policy",
                tenant_id=state.tenant_id,
                expected_statuses=frozenset({200}),
            ),
            tenant_id=state.tenant_id,
        )
    allow_mp4, exists, revision, operation_id = restored_state
    if state.mp4_policy_original_exists and (
        allow_mp4 != state.mp4_policy_original
        or not exists
        or revision == state.mp4_policy_cleanup_revision
        or operation_id != state.mp4_policy_restore_operation_id
    ):
        raise ExportProbeError("resource_cleanup_failed")
    if not state.mp4_policy_original_exists and (
        allow_mp4
        or exists
        or revision == "absent"
        or revision == state.mp4_policy_cleanup_revision
        or operation_id != state.mp4_policy_restore_operation_id
    ):
        raise ExportProbeError("resource_cleanup_failed")
    _clear_mp4_policy_mutation(state)


_USER_LIST_KEYS = frozenset({"id", "username", "role", "created_at", "disabled", "avatar"})


async def _listed_identity_id(
    api: ExportApi,
    *,
    username: str,
) -> str | None:
    try:
        raw_users = await api.admin_list_json(
            "GET",
            "/api/v1/auth/users",
            expected_statuses=frozenset({200}),
        )
    except ExportProbeError as exc:
        raise ExportProbeError("identity_cleanup_failed") from exc
    match: str | None = None
    for raw_user in raw_users:
        user = _exact_keys(raw_user, _USER_LIST_KEYS, "identity_cleanup_failed")
        user_id = user.get("id")
        observed_username = user.get("username")
        role = user.get("role")
        if (
            not isinstance(user_id, str)
            or not isinstance(observed_username, str)
            or not observed_username
            or role not in {"admin", "user"}
            or not isinstance(user.get("created_at"), str)
            or not isinstance(user.get("disabled"), bool)
            or not isinstance(user.get("avatar"), str)
        ):
            raise ExportProbeError("identity_cleanup_failed")
        if observed_username != username:
            continue
        if _PUBLIC_ID.fullmatch(user_id) is None or match is not None:
            raise ExportProbeError("identity_cleanup_failed")
        match = user_id
    return match


async def _delete_identity_with_reconciliation(
    api: ExportApi,
    *,
    username: str,
    expected_user_id: str,
) -> None:
    for attempt in range(2):
        try:
            body = await api.admin_json(
                "DELETE",
                f"/api/v1/auth/users/{username}",
                json_body={"expected_user_id": expected_user_id},
                expected_statuses=frozenset({200}),
            )
        except ExportProbeError:
            body = None
        if body == {"ok": True}:
            return
        observed_user_id = await _listed_identity_id(api, username=username)
        if observed_user_id is None:
            return
        if not hmac.compare_digest(observed_user_id, expected_user_id) or attempt == 1:
            raise ExportProbeError("identity_cleanup_failed")


async def _cleanup_identity(
    api: ExportApi,
    state: ProbeState,
    *,
    material: FixtureMaterial,
) -> None:
    if state.teacher_username is None:
        return
    if state.teacher_username != material.teacher_username:
        raise ExportProbeError("identity_cleanup_failed")
    if state.teacher_user_id is None:
        recovered = await api.login_identity(
            material.teacher_username,
            material.teacher_password,
        )
        if recovered.username != state.teacher_username:
            raise ExportProbeError("identity_cleanup_failed")
        state.teacher_user_id = recovered.user_id
    await _delete_identity_with_reconciliation(
        api,
        username=state.teacher_username,
        expected_user_id=state.teacher_user_id,
    )
    state.teacher_identity = None
    state.teacher_user_id = None
    state.teacher_username = None


async def _run_export_probe(config: ProbeConfig) -> bytes:
    state = ProbeState()
    state.tenant_id = config.tenant_id
    material = _fixture_material(config)
    failure: BaseException | None = None
    body: bytes | None = None
    resource_cleanup_failed = False
    identity_cleanup_failed = False
    end_time = time.monotonic() + config.timeout_seconds
    async with ExportApi(
        config.base_url,
        config.admin_token.get_secret_value(),
    ) as api:
        try:
            async with asyncio.timeout(_remaining(end_time)):
                body = await _execute_export_probe(
                    config,
                    api,
                    material=material,
                    state=state,
                    end_time=end_time,
                )
        except TimeoutError:
            failure = ExportProbeError("classroom_export_probe_timeout")
        except KeyboardInterrupt as exc:
            failure = exc
        except Exception as exc:
            failure = exc
        try:
            async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
                await _cleanup_resources(api, state)
        except Exception:
            resource_cleanup_failed = True
        try:
            async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
                await _cleanup_identity(api, state, material=material)
        except Exception:
            identity_cleanup_failed = True
    cleanup_failed = resource_cleanup_failed or identity_cleanup_failed
    if failure is not None:
        if isinstance(failure, KeyboardInterrupt):
            raise failure
        if cleanup_failed:
            raise ExportProbeError("export_probe_and_cleanup_failed") from failure
        raise failure
    if cleanup_failed:
        raise ExportProbeError("classroom_export_cleanup_failed")
    if body is None:
        raise ExportProbeError("classroom_export_probe_failed")
    return body


def _write_stdout_report(body: bytes) -> None:
    try:
        written = sys.stdout.buffer.write(body)
        if written != len(body):
            raise OSError("short stdout write")
        sys.stdout.buffer.flush()
    except OSError as exc:
        raise ExportProbeError("classroom_export_stdout_failed") from exc


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _parse_args(argv)
        config = _load_config(os.environ, cwd=Path.cwd())
        body = asyncio.run(_run_export_probe(config))
        _write_stdout_report(body)
    except ExportProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("classroom_export_probe_interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("classroom_export_probe_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
