"""OpenMAIC service-to-service request signing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection

MAX_CLOCK_SKEW_SECONDS = 60
SERVICE_SECRET_PATH = Path("/run/secrets/openmaic_service_secret")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ServiceSecretUnavailable(RuntimeError):
    """The fixed OpenMAIC service-secret mount cannot be read safely."""

    def __init__(self) -> None:
        super().__init__("OpenMAIC service secret is unavailable")


class ServiceSecretAccessDenied(ServiceSecretUnavailable):
    """The selected route is outside this worker's service-secret boundary."""


@dataclass(frozen=True, slots=True)
class ServiceRequest:
    """The seven fields bound by the OpenMAIC overlay signature."""

    method: str
    path: str
    tenant_id: str
    job_id: str
    timestamp: int
    idempotency_key: str
    body: str | bytes


@dataclass(frozen=True, slots=True)
class PrehashedServiceRequest:
    """A signed request whose streamed body is bound by a declared SHA-256."""

    method: str
    path: str
    tenant_id: str
    job_id: str
    timestamp: int
    idempotency_key: str
    body_sha256: str


def _canonical_line(
    name: str,
    value: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} cannot contain a newline")
    return value


def _normalized_request(request: ServiceRequest) -> ServiceRequest:
    method = _canonical_line("method", request.method).upper()
    if not method.isascii() or not method.isalpha():
        raise ValueError("method must contain only ASCII letters")
    path = _canonical_line("path", request.path)
    if not path.startswith("/"):
        raise ValueError("path must be absolute")
    tenant_id = _canonical_line("tenant_id", request.tenant_id)
    job_id = _canonical_line("job_id", request.job_id)
    if (
        isinstance(request.timestamp, bool)
        or not isinstance(request.timestamp, int)
        or request.timestamp < 0
        or request.timestamp > _MAX_SAFE_INTEGER
    ):
        raise ValueError("timestamp must be a non-negative safe integer")
    idempotency_key = _canonical_line(
        "idempotency_key",
        request.idempotency_key,
        allow_empty=method in _SAFE_METHODS,
    )
    if not isinstance(request.body, (str, bytes)):
        raise TypeError("body must be str or bytes")
    return ServiceRequest(
        method=method,
        path=path,
        tenant_id=tenant_id,
        job_id=job_id,
        timestamp=request.timestamp,
        idempotency_key=idempotency_key,
        body=request.body,
    )


def _body_bytes(body: str | bytes) -> bytes:
    return body.encode("utf-8") if isinstance(body, str) else body


def canonical_service_request(request: ServiceRequest) -> str:
    """Return the exact seven-line string verified by the TypeScript overlay."""

    normalized = _normalized_request(request)
    digest = hashlib.sha256(_body_bytes(normalized.body)).hexdigest()
    return "\n".join(
        (
            normalized.method,
            normalized.path,
            normalized.tenant_id,
            normalized.job_id,
            str(normalized.timestamp),
            normalized.idempotency_key,
            digest,
        )
    )


def _normalized_prehashed_request(
    request: PrehashedServiceRequest,
) -> PrehashedServiceRequest:
    normalized = _normalized_request(
        ServiceRequest(
            method=request.method,
            path=request.path,
            tenant_id=request.tenant_id,
            job_id=request.job_id,
            timestamp=request.timestamp,
            idempotency_key=request.idempotency_key,
            body=b"",
        )
    )
    digest = request.body_sha256
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("body_sha256 must be a lowercase SHA-256 hex digest")
    return PrehashedServiceRequest(
        method=normalized.method,
        path=normalized.path,
        tenant_id=normalized.tenant_id,
        job_id=normalized.job_id,
        timestamp=normalized.timestamp,
        idempotency_key=normalized.idempotency_key,
        body_sha256=digest,
    )


def canonical_prehashed_service_request(request: PrehashedServiceRequest) -> str:
    """Return the seven-line signature input for a bounded streamed body."""

    normalized = _normalized_prehashed_request(request)
    return "\n".join(
        (
            normalized.method,
            normalized.path,
            normalized.tenant_id,
            normalized.job_id,
            str(normalized.timestamp),
            normalized.idempotency_key,
            normalized.body_sha256,
        )
    )


def _secret_value(secret: SecretStr | str) -> str:
    value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
    return _canonical_line("secret", value)


def sign_service_request(
    request: ServiceRequest,
    secret: SecretStr | str,
) -> str:
    """Create the HMAC-SHA256 hex signature consumed by OpenMAIC."""

    return hmac.new(
        _secret_value(secret).encode("utf-8"),
        canonical_service_request(request).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_prehashed_service_request(
    request: PrehashedServiceRequest,
    secret: SecretStr | str,
) -> str:
    """Sign a streamed request without reading its body into memory."""

    return hmac.new(
        _secret_value(secret).encode("utf-8"),
        canonical_prehashed_service_request(request).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def signed_service_headers(
    request: ServiceRequest,
    secret: SecretStr | str,
) -> dict[str, str]:
    """Return the five trusted headers used by overlay route handlers."""

    normalized = _normalized_request(request)
    return {
        "x-yfeistai-tenant-id": normalized.tenant_id,
        "x-yfeistai-job-id": normalized.job_id,
        "x-yfeistai-timestamp": str(normalized.timestamp),
        "x-yfeistai-idempotency-key": normalized.idempotency_key,
        "x-yfeistai-signature": sign_service_request(normalized, secret),
    }


def signed_prehashed_service_headers(
    request: PrehashedServiceRequest,
    secret: SecretStr | str,
) -> dict[str, str]:
    """Return trusted headers for a streamed body with a declared digest."""

    normalized = _normalized_prehashed_request(request)
    return {
        "x-yfeistai-tenant-id": normalized.tenant_id,
        "x-yfeistai-job-id": normalized.job_id,
        "x-yfeistai-timestamp": str(normalized.timestamp),
        "x-yfeistai-idempotency-key": normalized.idempotency_key,
        "x-yfeistai-content-sha256": normalized.body_sha256,
        "x-yfeistai-signature": sign_prehashed_service_request(
            normalized,
            secret,
        ),
    }


def verify_service_request(
    request: ServiceRequest,
    signature: str,
    secret: SecretStr | str,
    *,
    now_seconds: int,
) -> bool:
    """Verify a signature with the overlay's inclusive 60-second window."""

    try:
        normalized = _normalized_request(request)
        if (
            isinstance(now_seconds, bool)
            or not isinstance(now_seconds, int)
            or now_seconds < 0
            or now_seconds > _MAX_SAFE_INTEGER
            or abs(now_seconds - normalized.timestamp) > MAX_CLOCK_SKEW_SECONDS
            or not isinstance(signature, str)
            or len(signature) != 64
        ):
            return False
        bytes.fromhex(signature)
        expected = sign_service_request(normalized, secret)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def read_service_secret(path: Path = SERVICE_SECRET_PATH) -> SecretStr:
    """Read the service HMAC secret from one explicit non-symlink mount."""

    secret_file = Path(path)
    if (
        not secret_file.is_absolute()
        or secret_file.is_symlink()
        or any(parent.is_symlink() for parent in secret_file.parents)
        or not secret_file.is_file()
    ):
        raise ServiceSecretUnavailable()
    read_error: ServiceSecretUnavailable | None = None
    try:
        value = secret_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        read_error = ServiceSecretUnavailable()
    if read_error is not None:
        raise read_error
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    validation_error: ServiceSecretUnavailable | None = None
    try:
        secret = SecretStr(_canonical_line("secret", value))
    except (TypeError, ValueError):
        validation_error = ServiceSecretUnavailable()
    else:
        return secret
    value = ""
    raise validation_error


class MountedServiceSecretResolver:
    """Resolve one service HMAC mount bound to a worker route.

    Provider API-key mounts are deliberately not accepted here. The file is
    re-read whenever the client factory builds a client, so rotation takes
    effect for the next job/client without retaining the old file contents.
    """

    def __init__(
        self,
        secret_path: Path = SERVICE_SECRET_PATH,
        *,
        runtime_mode: Literal["shared", "dedicated"],
        runtime_route_id: str,
        runtime_tenant_id: str | None = None,
    ) -> None:
        if not runtime_route_id or "\n" in runtime_route_id or "\r" in runtime_route_id:
            raise ServiceSecretAccessDenied()
        if runtime_mode == "shared" and runtime_tenant_id is not None:
            raise ServiceSecretAccessDenied()
        if runtime_mode == "dedicated" and not runtime_tenant_id:
            raise ServiceSecretAccessDenied()
        self._secret_path = Path(secret_path)
        self._runtime_mode = runtime_mode
        self._runtime_route_id = runtime_route_id
        self._runtime_tenant_id = runtime_tenant_id

    def resolve(self, selection: DataPlaneSelection) -> SecretStr:
        if selection.mode != self._runtime_mode or selection.route_ref != self._runtime_route_id:
            raise ServiceSecretAccessDenied()
        if self._runtime_mode == "dedicated" and selection.tenant_id != self._runtime_tenant_id:
            raise ServiceSecretAccessDenied()
        return read_service_secret(self._secret_path)
