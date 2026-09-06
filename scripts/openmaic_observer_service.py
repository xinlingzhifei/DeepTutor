"""Candidate-bound shared-ingress observer/control ASGI bridge."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
from typing import Any
from urllib.parse import urlsplit
import uuid

import httpx
from pydantic import SecretStr

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from openmaic_smoke_contract import (  # noqa: E402
    OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION,
    OPENMAIC_SHARED_INGRESS_OBSERVER_PRODUCER,
    canonical_openmaic_shared_ingress_observer_attestation,
    openmaic_observer_attestation_challenge,
    parse_openmaic_shared_ingress_observer_attestation,
)

_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_REQUEST_BYTES = 4096
_MAX_AUTH_TOKEN_BYTES = 129
_MAX_PROXY_REQUEST_BYTES = 128 * 1024 * 1024
_MAX_PROXY_RESPONSE_BYTES = 256 * 1024 * 1024
_MAX_HEALTH_RESPONSE_BYTES = 64 * 1024
_RUN_AUTH_TOKEN = re.compile(r"^[A-Za-z0-9_-]{48,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBSERVER_CANARY_HEADER = b"x-yfeistai-observer-canary"
_CANDIDATE_OBSERVER_HEALTH_PATH = "/api/v1/system/teaching-health/observer-canary"
_SIGNED_HEADER_NAMES = {
    b"x-yfeistai-tenant-id",
    b"x-yfeistai-job-id",
    b"x-yfeistai-timestamp",
    b"x-yfeistai-idempotency-key",
    b"x-yfeistai-content-sha256",
    b"x-yfeistai-signature",
}
_REQUIRED_SIGNED_HEADERS = _SIGNED_HEADER_NAMES - {b"x-yfeistai-content-sha256"}


class OpenMAICObserverServiceError(RuntimeError):
    """Stable, secret-free observer service failure."""


@dataclass(frozen=True, slots=True)
class ObserverServiceConfig:
    auth_token: SecretStr
    auth_expires_at: datetime
    candidate_health_token: SecretStr
    candidate_health_url: str
    candidate_sha256: str
    environment_id: str
    image_lock_sha256: str
    observer_id: str
    observer_public_key_spki_sha256: str
    observer_url: str
    release_run_id: str
    shared_ingress_control_url: str
    shared_ingress_upstream_url: str
    internal_proxy_url: str = "http://openmaic-shared-ingress-observer:8000"
    state_limit: int = 1024


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes | AsyncIterator[bytes]


@dataclass(frozen=True, slots=True)
class _LoadedAuthSecret:
    identity: tuple[int, ...]
    path: Path
    secret: SecretStr


Requester = Callable[..., Awaitable[HTTPResponse]]


def _origin(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise OpenMAICObserverServiceError("service_config_invalid") from exc
    if parsed.scheme != "https" or hostname is None or parsed.username or parsed.password:
        raise OpenMAICObserverServiceError("service_config_invalid")
    authority = hostname.lower().rstrip(".")
    if not authority:
        raise OpenMAICObserverServiceError("service_config_invalid")
    if port not in {None, 443}:
        authority = f"{authority}:{port}"
    return f"https://{authority}", authority


def _internal_proxy_origin(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise OpenMAICObserverServiceError("service_config_invalid") from exc
    if (
        parsed.scheme != "http"
        or hostname is None
        or parsed.username
        or parsed.password
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise OpenMAICObserverServiceError("service_config_invalid")
    authority = f"{hostname.lower().rstrip('.')}:{port}"
    if authority.startswith(":"):
        raise OpenMAICObserverServiceError("service_config_invalid")
    return f"http://{authority}", authority


def _authority_key(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if hostname is None:
            raise ValueError
        canonical_host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not canonical_host:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise OpenMAICObserverServiceError("service_config_invalid") from exc
    return canonical_host, port


def validate_observer_endpoints(
    *,
    environment_id: str,
    observer_id: str,
    release_run_id: str,
    observer_url: str,
    shared_ingress_control_url: str,
    candidate_health_url: str,
    shared_ingress_upstream_url: str,
    internal_proxy_url: str,
) -> tuple[str, str, str]:
    """Validate public deployment inputs without loading credentials or making requests."""
    if any(
        not isinstance(value, str)
        for value in (
            environment_id,
            observer_id,
            release_run_id,
            observer_url,
            shared_ingress_control_url,
            candidate_health_url,
            shared_ingress_upstream_url,
            internal_proxy_url,
        )
    ):
        raise OpenMAICObserverServiceError("service_config_invalid")
    _, observer_authority = _origin(observer_url)
    _, control_authority = _origin(shared_ingress_control_url)
    _origin(candidate_health_url)
    _origin(shared_ingress_upstream_url)
    _, internal_authority = _internal_proxy_origin(internal_proxy_url)
    observer_parts = urlsplit(observer_url)
    control_parts = urlsplit(shared_ingress_control_url)
    candidate_parts = urlsplit(candidate_health_url)
    upstream_parts = urlsplit(shared_ingress_upstream_url)
    if (
        any(
            _PUBLIC_ID.fullmatch(value) is None
            for value in (environment_id, observer_id, release_run_id)
        )
        or len(
            {
                _authority_key(observer_url),
                _authority_key(shared_ingress_control_url),
                _authority_key(candidate_health_url),
                _authority_key(shared_ingress_upstream_url),
                _authority_key(internal_proxy_url),
            }
        )
        != 5
        or observer_parts.path not in {"", "/"}
        or observer_parts.query
        or observer_parts.fragment
        or control_parts.path != "/v1/control-canaries"
        or control_parts.query
        or control_parts.fragment
        or candidate_parts.path != _CANDIDATE_OBSERVER_HEALTH_PATH
        or candidate_parts.query
        or candidate_parts.fragment
        or upstream_parts.path not in {"", "/"}
        or upstream_parts.query
        or upstream_parts.fragment
    ):
        raise OpenMAICObserverServiceError("service_config_invalid")
    return observer_authority, control_authority, internal_authority


def _validate_config(config: ObserverServiceConfig) -> tuple[str, str, str]:
    authorities = validate_observer_endpoints(
        environment_id=config.environment_id,
        observer_id=config.observer_id,
        release_run_id=config.release_run_id,
        observer_url=config.observer_url,
        shared_ingress_control_url=config.shared_ingress_control_url,
        candidate_health_url=config.candidate_health_url,
        shared_ingress_upstream_url=config.shared_ingress_upstream_url,
        internal_proxy_url=config.internal_proxy_url,
    )
    token = config.auth_token.get_secret_value()
    candidate_token = config.candidate_health_token.get_secret_value()
    if (
        config.auth_expires_at.tzinfo is None
        or _RUN_AUTH_TOKEN.fullmatch(token) is None
        or _RUN_AUTH_TOKEN.fullmatch(candidate_token) is None
        or _SHA256.fullmatch(config.candidate_sha256) is None
        or config.candidate_sha256 == "0" * 64
        or _SHA256.fullmatch(config.image_lock_sha256) is None
        or config.image_lock_sha256 == "0" * 64
        or _SHA256.fullmatch(config.observer_public_key_spki_sha256) is None
        or config.observer_public_key_spki_sha256 == "0" * 64
        or hmac.compare_digest(token, candidate_token)
        or type(config.state_limit) is not int
        or config.state_limit < 1
        or config.state_limit > 4096
    ):
        raise OpenMAICObserverServiceError("service_config_invalid")
    return authorities


async def _default_requester(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    content: bytes | AsyncIterator[bytes],
    timeout_seconds: float,
    max_response_bytes: int,
) -> HTTPResponse:
    client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    response: httpx.Response | None = None
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        request = client.build_request(method, url, headers=dict(headers), content=content)
        async with asyncio.timeout_at(deadline):
            response = await client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
        content_lengths = response.headers.get_list("content-length")
        if (
            response.headers.get_list("content-encoding")
            or content_lengths
            and (
                len(content_lengths) != 1
                or re.fullmatch(r"[0-9]+", content_lengths[0]) is None
                or int(content_lengths[0]) > max_response_bytes
            )
        ):
            raise OpenMAICObserverServiceError("upstream_invalid")
    except OpenMAICObserverServiceError:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise
    except (TimeoutError, httpx.HTTPError) as exc:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise OpenMAICObserverServiceError("upstream_unavailable") from exc

    async def response_body() -> AsyncIterator[bytes]:
        assert response is not None
        consumed = 0
        try:
            async with asyncio.timeout_at(deadline):
                async for chunk in response.aiter_raw():
                    consumed += len(chunk)
                    if consumed > max_response_bytes:
                        raise OpenMAICObserverServiceError("upstream_invalid")
                    if chunk:
                        yield chunk
        except OpenMAICObserverServiceError:
            raise
        except (TimeoutError, httpx.HTTPError) as exc:
            raise OpenMAICObserverServiceError("upstream_unavailable") from exc
        finally:
            await response.aclose()
            await client.aclose()

    return HTTPResponse(
        status_code=response.status_code,
        headers={key.lower(): value for key, value in response.headers.items()},
        body=response_body(),
    )


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _is_reparse_point(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def _path_has_reparse_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            return True
    return False


def _file_identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        details.st_mode,
        details.st_uid,
    )


def _load_auth_secret_record(raw_path: object) -> _LoadedAuthSecret:
    if not isinstance(raw_path, str) or not raw_path:
        raise OpenMAICObserverServiceError("auth_secret_invalid")
    supplied = Path(raw_path)
    descriptor: int | None = None
    try:
        if not supplied.is_absolute() or _path_has_reparse_component(supplied):
            raise OSError
        resolved = supplied.resolve(strict=True)
        if supplied != resolved:
            raise OSError
        before = resolved.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise OSError
        if os.name == "posix" and (
            before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise OSError
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise OSError
        body = os.read(descriptor, _MAX_AUTH_TOKEN_BYTES)
        if os.read(descriptor, 1):
            raise OSError
        after = os.fstat(descriptor)
        final = resolved.lstat()
        identity = _file_identity(opened)
        if identity != _file_identity(after) or identity != _file_identity(final):
            raise OSError
        token = body.decode("ascii").strip()
        encoded = token.encode("ascii")
        if (
            len(token) < 48
            or len(token) > 128
            or any(character.isspace() for character in token)
            or body not in {encoded, encoded + b"\n", encoded + b"\r\n"}
        ):
            raise OSError
    except (OSError, UnicodeError) as exc:
        raise OpenMAICObserverServiceError("auth_secret_invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _LoadedAuthSecret(
        identity=identity,
        path=resolved,
        secret=SecretStr(token),
    )


def _load_auth_secret(raw_path: object) -> SecretStr:
    return _load_auth_secret_record(raw_path).secret


def _required_environment_id(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
        raise OpenMAICObserverServiceError("service_config_invalid")
    return value


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise OpenMAICObserverServiceError("service_config_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise OpenMAICObserverServiceError("service_config_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise OpenMAICObserverServiceError("service_config_invalid")
    return parsed


def parse_observer_auth_expiry(value: object, *, now: datetime) -> datetime:
    """Apply the same short-lived UTC admission window on the host and in the service."""
    expires_at = _parse_utc(value)
    if now.tzinfo is None or not 30 <= (expires_at - now).total_seconds() <= 7200:
        raise OpenMAICObserverServiceError("service_config_invalid")
    return expires_at


def load_config(
    environment: Mapping[str, str],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ObserverServiceConfig:
    """Load one short-lived, run-bound service configuration."""

    current = now()
    expires_at = parse_observer_auth_expiry(
        environment.get("YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT"), now=current
    )
    observer_secret = _load_auth_secret_record(
        environment.get("YFEISTAI_SHARED_INGRESS_AUTH_TOKEN_FILE")
    )
    candidate_secret = _load_auth_secret_record(
        environment.get("YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE")
    )
    if (
        observer_secret.path == candidate_secret.path
        or observer_secret.identity == candidate_secret.identity
        or hmac.compare_digest(
            observer_secret.secret.get_secret_value(),
            candidate_secret.secret.get_secret_value(),
        )
    ):
        raise OpenMAICObserverServiceError("service_config_invalid")
    config = ObserverServiceConfig(
        auth_token=observer_secret.secret,
        auth_expires_at=expires_at,
        candidate_health_token=candidate_secret.secret,
        candidate_health_url=environment.get("YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL", ""),
        candidate_sha256=environment.get("YFEISTAI_CANDIDATE_SHA256", ""),
        environment_id=_required_environment_id(environment, "YFEISTAI_ENVIRONMENT_ID"),
        image_lock_sha256=environment.get("YFEISTAI_IMAGE_LOCK_SHA256", ""),
        observer_id=_required_environment_id(
            environment,
            "YFEISTAI_SHARED_INGRESS_OBSERVER_ID",
        ),
        observer_url=environment.get("YFEISTAI_SHARED_INGRESS_OBSERVER_URL", ""),
        observer_public_key_spki_sha256=environment.get(
            "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256",
            "",
        ),
        release_run_id=_required_environment_id(environment, "YFEISTAI_RELEASE_RUN_ID"),
        shared_ingress_control_url=environment.get(
            "YFEISTAI_SHARED_INGRESS_CONTROL_URL",
            "",
        ),
        shared_ingress_upstream_url=environment.get(
            "YFEISTAI_SHARED_INGRESS_UPSTREAM_URL",
            "",
        ),
        internal_proxy_url=environment.get(
            "YFEISTAI_SHARED_INGRESS_INTERNAL_PROXY_URL",
            "http://openmaic-shared-ingress-observer:8000",
        ),
    )
    _validate_config(config)
    return config


def canonical_observer_attestation(
    config: ObserverServiceConfig,
    *,
    observed_at: datetime,
) -> bytes:
    if observed_at.tzinfo is None or observed_at.utcoffset() != timezone.utc.utcoffset(observed_at):
        raise OpenMAICObserverServiceError("attestation_invalid")
    rendered_at = observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    report = {
        "schemaVersion": OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION,
        "producer": OPENMAIC_SHARED_INGRESS_OBSERVER_PRODUCER,
        "releaseRun": {
            "runId": config.release_run_id,
            "environmentId": config.environment_id,
        },
        "candidateBinding": {
            "candidateSha256": config.candidate_sha256,
            "imageLockSha256": config.image_lock_sha256,
        },
        "attestationChallenge": openmaic_observer_attestation_challenge(
            config.observer_public_key_spki_sha256
        ),
        "observedAt": rendered_at,
        "observer": {
            "observerId": config.observer_id,
            "observerUrl": config.observer_url,
            "sharedIngressControlUrl": config.shared_ingress_control_url,
            "candidateHealthUrl": config.candidate_health_url,
            "sharedIngressUpstreamUrl": config.shared_ingress_upstream_url,
            "internalProxyUrl": _internal_proxy_origin(config.internal_proxy_url)[0],
            "authExpiresAt": config.auth_expires_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    }
    body = canonical_openmaic_shared_ingress_observer_attestation(report)
    secrets = (
        config.auth_token.get_secret_value().encode("utf-8"),
        config.candidate_health_token.get_secret_value().encode("utf-8"),
    )
    if any(secret in body for secret in secrets):
        raise OpenMAICObserverServiceError("attestation_contains_secret")
    try:
        parse_openmaic_shared_ingress_observer_attestation(
            body,
            release_run=report["releaseRun"],
        )
    except ValueError as exc:
        raise OpenMAICObserverServiceError("attestation_invalid") from exc
    return body


def _close_parent_descriptor(descriptor: int | None) -> None:
    if descriptor is not None:
        os.close(descriptor)


def publish_observer_attestation(
    config: ObserverServiceConfig,
    target: Path,
    *,
    observed_at: datetime,
) -> bytes:
    """Atomically publish a canonical attestation without replacing a path."""

    body = canonical_observer_attestation(config, observed_at=observed_at)
    path = Path(target)
    staged: Path | None = None
    staged_name = ""
    linked = False
    parent_descriptor: int | None = None
    publish_failure: tuple[str, OSError] | None = None
    cleanup_failure: OSError | None = None
    try:
        if not path.is_absolute():
            raise OSError
        parent = path.parent.resolve(strict=True)
        if path.parent != parent or _path_has_reparse_component(parent):
            raise OSError
        parent_before = parent.lstat()
        parent_identity = (
            parent_before.st_dev,
            parent_before.st_ino,
            parent_before.st_mode,
            parent_before.st_uid,
        )
        if path.exists() or path.is_symlink():
            raise FileExistsError
        staged_name = f".{path.name}.{uuid.uuid4().hex}.staged"
        staged = parent / staged_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if os.name == "posix":
            parent_descriptor = os.open(
                parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            parent_opened = os.fstat(parent_descriptor)
            if parent_identity != (
                parent_opened.st_dev,
                parent_opened.st_ino,
                parent_opened.st_mode,
                parent_opened.st_uid,
            ):
                raise OSError
            descriptor = os.open(staged_name, flags, 0o600, dir_fd=parent_descriptor)
        else:
            descriptor = os.open(staged, flags, 0o600)
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            staged_details = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if os.name == "posix":
            assert parent_descriptor is not None
            os.link(
                staged_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            linked = True
            published = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        else:
            os.link(staged, path)
            linked = True
            published = path.lstat()
        if (
            _is_reparse_point(path)
            or not stat.S_ISREG(published.st_mode)
            or (published.st_dev, published.st_ino)
            != (staged_details.st_dev, staged_details.st_ino)
            or (os.name == "posix" and stat.S_IMODE(published.st_mode) != 0o600)
            or path.read_bytes() != body
        ):
            raise OSError
        if os.name == "posix":
            assert parent_descriptor is not None
            os.fsync(parent_descriptor)
        parent_after = parent.lstat()
        if parent_identity != (
            parent_after.st_dev,
            parent_after.st_ino,
            parent_after.st_mode,
            parent_after.st_uid,
        ):
            raise OSError
    except FileExistsError as exc:
        publish_failure = ("attestation_exists", exc)
    except OSError as exc:
        if linked:
            publish_failure = ("attestation_committed_but_unverified", exc)
        else:
            publish_failure = ("attestation_publish_failed", exc)
    finally:
        if staged is not None:
            try:
                if os.name == "posix" and parent_descriptor is not None:
                    try:
                        os.unlink(staged_name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
                else:
                    staged.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_failure = exc
        descriptor_to_close = parent_descriptor
        parent_descriptor = None
        try:
            _close_parent_descriptor(descriptor_to_close)
        except OSError as exc:
            # The canonical target is already verified and the parent directory
            # fsynced once linked; a close error cannot revoke that durable commit.
            if not linked and cleanup_failure is None:
                cleanup_failure = exc

    primary_error: OpenMAICObserverServiceError | None = None
    if publish_failure is not None:
        code, cause = publish_failure
        primary_error = OpenMAICObserverServiceError(code)
        primary_error.__cause__ = cause
        primary_error.__suppress_context__ = True

    if cleanup_failure is not None and linked:
        message = "attestation_committed_but_cleanup_incomplete"
        assert staged is not None
        try:
            staged.lstat()
        except OSError:
            pass
        else:
            message = f"{message}: {staged.absolute()}"
        committed_error = OpenMAICObserverServiceError(message)
        if primary_error is None:
            raise committed_error from cleanup_failure
        raise committed_error from BaseExceptionGroup(
            "attestation publication and cleanup both failed",
            (primary_error, cleanup_failure),
        )

    if cleanup_failure is not None:
        cleanup_error = OpenMAICObserverServiceError("attestation_cleanup_failed")
        cleanup_error.__cause__ = cleanup_failure
        cleanup_error.__suppress_context__ = True
        if primary_error is not None:
            raise BaseExceptionGroup(
                "attestation publication and cleanup both failed",
                (primary_error, cleanup_error),
            )
        raise cleanup_error

    if primary_error is not None:
        raise primary_error
    return body


class OpenMAICObserverService:
    """Minimal ASGI application for one release-run-scoped observer."""

    def __init__(
        self,
        config: ObserverServiceConfig,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        requester: Requester = _default_requester,
    ) -> None:
        self._config = config
        (
            self._observer_authority,
            self._control_authority,
            self._internal_proxy_authority,
        ) = _validate_config(config)
        self._now = now
        self._requester = requester
        self._lock = threading.Lock()
        self._control_lock = asyncio.Lock()
        self._request_count = 0
        self._last_canary_id: str | None = None
        self._event_keys: set[str] = set()
        self._completed_canaries: set[str] = set()
        self._pending_canary: tuple[str, str, int] | None = None

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            raise OpenMAICObserverServiceError("scope_invalid")
        raw_headers = tuple(
            (bytes(name).lower(), bytes(value)) for name, value in scope.get("headers", ())
        )
        by_name: dict[bytes, list[bytes]] = {}
        for name, value in raw_headers:
            by_name.setdefault(name, []).append(value)
        if (
            len(by_name.get(b"host", ())) != 1
            or len(by_name.get(b"authorization", ())) > 1
            or len(by_name.get(b"content-type", ())) > 1
            or len(by_name.get(_OBSERVER_CANARY_HEADER, ())) > 1
            or any(len(by_name.get(name, ())) > 1 for name in _SIGNED_HEADER_NAMES)
        ):
            await self._response(send, 400, {"error": "ambiguous_headers"})
            return
        method = scope.get("method")
        path = scope.get("path")
        query = scope.get("query_string", b"")
        try:
            host = _origin("https://" + by_name[b"host"][0].decode("ascii", "strict"))[1]
        except (UnicodeError, OpenMAICObserverServiceError):
            await self._response(send, 400, {"error": "request_invalid"})
            return
        if host == self._internal_proxy_authority:
            if method == "GET" and path == "/healthz" and not query:
                await self._response(send, 200, {"status": "healthy"})
            elif isinstance(path, str) and path.startswith("/api/yfeistai/v1/"):
                await self._proxy(method, path, query, by_name, receive, send)
            else:
                await self._response(send, 404, {"error": "not_found"})
            return
        if (
            host == self._control_authority
            and isinstance(path, str)
            and path.startswith("/api/yfeistai/v1/")
        ):
            await self._proxy(method, path, query, by_name, receive, send)
            return

        expected = f"Bearer {self._config.auth_token.get_secret_value()}".encode("ascii")
        authorization = by_name.get(b"authorization", ())
        current = self._now()
        if (
            current.tzinfo is None
            or current >= self._config.auth_expires_at
            or len(authorization) != 1
            or not hmac.compare_digest(authorization[0], expected)
        ):
            await self._response(send, 401, {"error": "unauthorized"})
            return

        observation_path = f"/v1/observations/{self._config.release_run_id}"
        if query:
            await self._response(send, 400, {"error": "request_invalid"})
        elif path == observation_path and host == self._observer_authority:
            if method != "GET":
                await self._response(
                    send,
                    405,
                    {"error": "method_not_allowed"},
                    extra_headers=((b"allow", b"GET"),),
                )
            else:
                await self._read(send)
        elif path == "/v1/control-canaries" and host == self._control_authority:
            if method != "POST":
                await self._response(
                    send,
                    405,
                    {"error": "method_not_allowed"},
                    extra_headers=((b"allow", b"POST"),),
                )
            elif by_name.get(b"content-type") != [b"application/json"]:
                await self._response(send, 415, {"error": "content_type_invalid"})
            else:
                await self._control(receive, send)
        else:
            await self._response(send, 404, {"error": "not_found"})

    async def _read(self, send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        with self._lock:
            document = {
                "schemaVersion": 1,
                "runId": self._config.release_run_id,
                "environmentId": self._config.environment_id,
                "observationId": f"shared-ingress-{self._config.release_run_id}",
                "requestCount": self._request_count,
                "lastCanaryId": self._last_canary_id,
            }
        await self._response(send, 200, document)

    async def _control(
        self,
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        body = await self._read_request_body(receive, limit=_MAX_REQUEST_BYTES)
        if body is None:
            await self._response(send, 413, {"error": "request_too_large"})
            return
        if self._token_expired():
            await self._response(send, 401, {"error": "unauthorized"})
            return
        try:
            document = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            await self._response(send, 400, {"error": "request_invalid"})
            return
        canary_id = document.get("canaryId") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or set(document) != {"schemaVersion", "runId", "environmentId", "canaryId", "kind"}
            or document.get("schemaVersion") != 1
            or document.get("runId") != self._config.release_run_id
            or document.get("environmentId") != self._config.environment_id
            or document.get("kind") != "openmaic-shared-ingress-control"
            or not isinstance(canary_id, str)
            or _PUBLIC_ID.fullmatch(canary_id) is None
        ):
            await self._response(send, 400, {"error": "control_binding_invalid"})
            return

        async with self._control_lock:
            if self._token_expired():
                await self._response(send, 401, {"error": "unauthorized"})
                return
            with self._lock:
                if canary_id in self._completed_canaries:
                    accepted = True
                elif len(self._event_keys) >= self._config.state_limit:
                    accepted = None
                else:
                    accepted = False
                    challenge = "c" + secrets.token_urlsafe(48)
                    baseline = self._request_count
                    self._pending_canary = (canary_id, challenge, baseline)
            if accepted is None:
                await self._response(send, 429, {"error": "state_limit_reached"})
                return
            if accepted is True:
                await self._response(send, 202, {"accepted": True, "canaryId": canary_id})
                return
            healthy = False
            try:
                candidate = await self._requester(
                    method="GET",
                    url=self._config.candidate_health_url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": (
                            "Bearer " + self._config.candidate_health_token.get_secret_value()
                        ),
                        "X-YFeiSTAI-Observer-Canary": challenge,
                    },
                    content=b"",
                    timeout_seconds=20.0,
                    max_response_bytes=_MAX_HEALTH_RESPONSE_BYTES,
                )
                healthy = await self._valid_candidate_health(candidate)
            except (OpenMAICObserverServiceError, OSError, ValueError):
                pass
            finally:
                with self._lock:
                    event_key = f"canary:{canary_id}"
                    exact_increment = (
                        self._pending_canary == (canary_id, challenge, baseline)
                        and event_key in self._event_keys
                        and self._request_count == baseline + 1
                        and self._last_canary_id == canary_id
                    )
                    if (not healthy or not exact_increment) and event_key in self._event_keys:
                        self._event_keys.remove(event_key)
                        self._request_count -= 1
                        if self._last_canary_id == canary_id:
                            self._last_canary_id = None
                    self._pending_canary = None
            with self._lock:
                if healthy and exact_increment:
                    self._completed_canaries.add(canary_id)
            if not healthy or not exact_increment:
                await self._response(send, 502, {"error": "control_canary_failed"})
                return
            await self._response(send, 202, {"accepted": True, "canaryId": canary_id})

    def _token_expired(self) -> bool:
        current = self._now()
        return current.tzinfo is None or current >= self._config.auth_expires_at

    @staticmethod
    async def _response_body_bytes(response: HTTPResponse, *, limit: int) -> bytes | None:
        if isinstance(response.body, bytes):
            return response.body if len(response.body) <= limit else None
        body = bytearray()
        try:
            async for chunk in response.body:
                if not isinstance(chunk, bytes) or len(body) + len(chunk) > limit:
                    return None
                body.extend(chunk)
        except (OpenMAICObserverServiceError, OSError, ValueError):
            return None
        return bytes(body)

    async def _valid_candidate_health(self, response: HTTPResponse) -> bool:
        body = await self._response_body_bytes(response, limit=_MAX_HEALTH_RESPONSE_BYTES)
        if (
            response.status_code != 200
            or body is None
            or response.headers.get("content-type", "").split(";", 1)[0] != "application/json"
        ):
            return False
        try:
            document = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            return False
        components = document.get("components") if isinstance(document, dict) else None
        shared = components.get("openmaic_shared") if isinstance(components, dict) else None
        return (
            document.get("candidateSha256") == self._config.candidate_sha256
            and isinstance(shared, dict)
            and all(
                shared.get(name) == expected
                for name, expected in {
                    "status": "healthy",
                    "service": "openmaic",
                    "mode": "shared",
                }.items()
            )
        )

    async def _proxy(
        self,
        method: object,
        path: str,
        query: object,
        headers: Mapping[bytes, list[bytes]],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if not isinstance(method, str) or method not in {"GET", "POST", "PUT", "DELETE"}:
            await self._response(send, 405, {"error": "method_not_allowed"})
            return
        if not isinstance(query, bytes) or query:
            await self._response(send, 400, {"error": "request_invalid"})
            return
        segments = path.split("/")
        if "//" in path or any(segment in {".", ".."} for segment in segments):
            await self._response(send, 400, {"error": "request_invalid"})
            return
        supplied_signed = {name for name in _SIGNED_HEADER_NAMES if headers.get(name)}
        unknown_signed = (
            {name for name in headers if name.startswith(b"x-yfeistai-")}
            - _SIGNED_HEADER_NAMES
            - {_OBSERVER_CANARY_HEADER}
        )
        health_path = path == "/api/yfeistai/v1/health"
        if unknown_signed or (not health_path and not _REQUIRED_SIGNED_HEADERS <= supplied_signed):
            await self._response(send, 400, {"error": "signature_headers_invalid"})
            return
        stream_upload = method == "PUT" and path.startswith("/api/yfeistai/v1/export-inputs/")
        if stream_upload:
            body: bytes | AsyncIterator[bytes] = self._stream_request_body(
                receive,
                limit=_MAX_PROXY_REQUEST_BYTES,
            )
        else:
            buffered = await self._read_request_body(receive, limit=_MAX_PROXY_REQUEST_BYTES)
            if buffered is None:
                await self._response(send, 413, {"error": "request_too_large"})
                return
            body = buffered
        forwarded_headers: dict[str, str] = {}
        for name in (b"accept", b"content-type", *_SIGNED_HEADER_NAMES):
            values = headers.get(name, ())
            if values:
                try:
                    forwarded_headers[name.decode("ascii")] = values[0].decode("ascii", "strict")
                except UnicodeError:
                    await self._response(send, 400, {"error": "request_invalid"})
                    return
        url = self._config.shared_ingress_upstream_url.rstrip("/") + path

        challenge_values = headers.get(_OBSERVER_CANARY_HEADER, ())
        with self._lock:
            pending = self._pending_canary
            is_canary = (
                pending is not None
                and health_path
                and method == "GET"
                and body == b""
                and not supplied_signed
                and len(challenge_values) == 1
                and hmac.compare_digest(challenge_values[0], pending[1].encode("ascii"))
            )
            if is_canary:
                assert pending is not None
                event_key = f"canary:{pending[0]}"
            else:
                idempotency = forwarded_headers.get("x-yfeistai-idempotency-key")
                signature = forwarded_headers.get("x-yfeistai-signature")
                event_key = (
                    f"request:{method}:{path}:{idempotency}:{signature}"
                    if idempotency is not None and signature is not None
                    else None
                )
        try:
            response = await self._requester(
                method=method,
                url=url,
                headers=forwarded_headers,
                content=body,
                timeout_seconds=20.0,
                max_response_bytes=_MAX_PROXY_RESPONSE_BYTES,
            )
        except OpenMAICObserverServiceError as exc:
            if str(exc) == "request_too_large":
                await self._response(send, 413, {"error": "request_too_large"})
                return
            if str(exc) == "request_invalid":
                await self._response(send, 400, {"error": "request_invalid"})
                return
            await self._response(send, 502, {"error": "upstream_unavailable"})
            return
        except (OSError, ValueError):
            await self._response(send, 502, {"error": "upstream_unavailable"})
            return
        if isinstance(response.body, bytes) and len(response.body) > _MAX_PROXY_RESPONSE_BYTES:
            await self._response(send, 502, {"error": "upstream_invalid"})
            return
        content_length = response.headers.get("content-length")
        if content_length is not None and (
            re.fullmatch(r"[0-9]+", content_length) is None
            or int(content_length) > _MAX_PROXY_RESPONSE_BYTES
        ):
            await self._response(send, 502, {"error": "upstream_invalid"})
            return
        content_type = response.headers.get("content-type", "application/octet-stream")
        try:
            content_type_bytes = content_type.encode("ascii", "strict")
        except UnicodeError:
            await self._response(send, 502, {"error": "upstream_invalid"})
            return
        if isinstance(response.body, bytes):
            await self._raw_response(send, response.status_code, response.body, content_type_bytes)
        else:
            await self._stream_raw_response(
                send,
                response.status_code,
                response.body,
                content_type_bytes,
                limit=_MAX_PROXY_RESPONSE_BYTES,
            )
        if 200 <= response.status_code < 300:
            self._commit_observation(
                event_key=event_key,
                is_canary=is_canary,
                pending=pending,
            )

    def _commit_observation(
        self,
        *,
        event_key: str | None,
        is_canary: bool,
        pending: tuple[str, str, int] | None,
    ) -> None:
        with self._lock:
            can_commit = (
                not is_canary
                or pending is not None
                and self._pending_canary == pending
                and self._request_count == pending[2]
            )
            if (
                can_commit
                and (event_key is None or event_key not in self._event_keys)
                and (event_key is None or len(self._event_keys) < self._config.state_limit)
            ):
                if event_key is not None:
                    self._event_keys.add(event_key)
                self._request_count += 1
                self._last_canary_id = pending[0] if is_canary and pending is not None else None

    @staticmethod
    async def _stream_request_body(
        receive: Callable[[], Awaitable[dict[str, Any]]],
        *,
        limit: int,
    ) -> AsyncIterator[bytes]:
        consumed = 0
        while True:
            event = await receive()
            if event.get("type") != "http.request":
                raise OpenMAICObserverServiceError("request_invalid")
            chunk = event.get("body", b"")
            if not isinstance(chunk, bytes):
                raise OpenMAICObserverServiceError("request_invalid")
            consumed += len(chunk)
            if consumed > limit:
                raise OpenMAICObserverServiceError("request_too_large")
            if chunk:
                yield chunk
            if not event.get("more_body", False):
                return

    @staticmethod
    async def _read_request_body(
        receive: Callable[[], Awaitable[dict[str, Any]]],
        *,
        limit: int,
    ) -> bytes | None:
        body = bytearray()
        while True:
            event = await receive()
            if event.get("type") != "http.request":
                return None
            body.extend(event.get("body", b""))
            if len(body) > limit:
                return None
            if not event.get("more_body", False):
                return bytes(body)

    @staticmethod
    async def _raw_response(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
        body: bytes,
        content_type: bytes,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": (
                    (b"content-type", content_type),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ),
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @staticmethod
    async def _stream_raw_response(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
        body: AsyncIterator[bytes],
        content_type: bytes,
        *,
        limit: int,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": (
                    (b"content-type", content_type),
                    (b"cache-control", b"no-store"),
                ),
            }
        )
        pending: bytes | None = None
        consumed = 0
        async for chunk in body:
            if not isinstance(chunk, bytes):
                raise OpenMAICObserverServiceError("upstream_invalid")
            consumed += len(chunk)
            if consumed > limit:
                raise OpenMAICObserverServiceError("upstream_invalid")
            if not chunk:
                continue
            if pending is not None:
                await send({"type": "http.response.body", "body": pending, "more_body": True})
            pending = chunk
        await send(
            {
                "type": "http.response.body",
                "body": pending or b"",
                "more_body": False,
            }
        )

    @staticmethod
    async def _response(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
        document: object,
        *,
        extra_headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> None:
        body = _canonical_json(document)
        headers = (
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
            *extra_headers,
        )
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-attestation", metavar="PATH", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        config = load_config(os.environ)
        body = publish_observer_attestation(
            config,
            Path(arguments.write_attestation),
            observed_at=datetime.now(timezone.utc),
        )
        if config.auth_token.get_secret_value().encode("utf-8") in body:
            raise OpenMAICObserverServiceError("attestation_contains_secret")
        sys.stdout.write(str(Path(arguments.write_attestation).resolve()) + "\n")
        return 0
    except OpenMAICObserverServiceError:
        sys.stderr.write("openmaic_observer_service_failed\n")
        return 1


def create_app() -> OpenMAICObserverService:
    """ASGI factory for a reverse-proxy deployment."""

    return OpenMAICObserverService(load_config(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HTTPResponse",
    "OpenMAICObserverService",
    "OpenMAICObserverServiceError",
    "ObserverServiceConfig",
    "SecretStr",
    "canonical_observer_attestation",
    "create_app",
    "load_config",
    "publish_observer_attestation",
]
