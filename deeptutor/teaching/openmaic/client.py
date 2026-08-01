"""Async client for the trusted yFeiSTAI OpenMAIC overlay."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
import logging
import math
import random
import re
import time
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, SecretStr

from deeptutor.teaching.contracts import ExportRequest, GenerationRequest, canonical_json_bytes
from deeptutor.teaching.openmaic.auth import (
    ServiceRequest,
    signed_service_headers,
)
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneRouteRecord,
    DataPlaneSelection,
    DataPlaneUnavailable,
)

logger = logging.getLogger(__name__)

SUPPORTED_CONTRACT_VERSION = "1.0"
EXPECTED_UPSTREAM_COMMIT = "0cf2a330411681190e89f48e20f305345ff99f87"
EXPECTED_APP_VERSION = "0.3.1"
REQUIRED_CAPABILITIES = frozenset(
    {"outline", "content", "micro", "export", "cancel", "artifact-manifest"}
)
REQUIRED_EXPORT_FORMATS = frozenset({"classroom_zip", "pptx", "offline_html", "mp4"})
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_JOB_STATUSES = {
    "outline": frozenset(
        {
            "created",
            "queued",
            "running",
            "generating_outline",
            *_TERMINAL_STATUSES,
        }
    ),
    "content": frozenset(
        {
            "created",
            "queued",
            "running",
            "generating_content",
            "validating",
            "materializing",
            *_TERMINAL_STATUSES,
        }
    ),
    "export": frozenset(
        {
            "created",
            "queued",
            "running",
            "exporting",
            "validating",
            "materializing",
            *_TERMINAL_STATUSES,
        }
    ),
}
_JOB_ID = re.compile(r"^[A-Za-z0-9._~-]+$")
_ARTIFACT_PATH = re.compile(
    r"^/api/yfeistai/v1/artifacts/(?P<job_id>[^/\s:]+)/(?P<relative>[A-Za-z0-9._~/-]+)$"
)


class OpenMAICError(RuntimeError):
    """Base class for stable, secret-free OpenMAIC client failures."""


class IncompatibleOpenMAIC(OpenMAICError):
    def __init__(self) -> None:
        super().__init__("OpenMAIC contract is incompatible")


class OpenMAICRequestFailed(OpenMAICError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"OpenMAIC request failed with status {status_code}")


class OpenMAICUnavailable(OpenMAICError):
    def __init__(self) -> None:
        super().__init__("OpenMAIC is unavailable")


class OpenMAICTimeout(OpenMAICError):
    def __init__(self) -> None:
        super().__init__("OpenMAIC request timed out")


class OpenMAICPollingExhausted(OpenMAICError):
    def __init__(self) -> None:
        super().__init__("OpenMAIC polling attempt limit reached")


class UnknownEngineJob(OpenMAICError):
    def __init__(self) -> None:
        super().__init__("OpenMAIC job route is unknown")


class UnsafeArtifactPath(OpenMAICError):
    def __init__(self) -> None:
        super().__init__("OpenMAIC artifact path is invalid")


class InvalidOpenMAICResponse(OpenMAICError):
    def __init__(self) -> None:
        super().__init__("OpenMAIC returned an invalid response")


@dataclass(frozen=True, slots=True)
class ClientTimeouts:
    connect: float = 5.0
    read: float = 30.0
    total: float = 60.0

    def __post_init__(self) -> None:
        values = (self.connect, self.read, self.total)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("OpenMAIC timeouts must be positive")

    def httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.read,
            pool=self.connect,
        )


@dataclass(frozen=True, slots=True)
class PollRetryPolicy:
    max_attempts: int = 8
    initial_delay: float = 0.25
    max_delay: float = 4.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("poll max_attempts must be positive")
        values = (self.initial_delay, self.max_delay, self.jitter_ratio)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("poll retry values must be finite")
        if self.initial_delay < 0 or self.max_delay < self.initial_delay:
            raise ValueError("poll retry delays are invalid")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("poll jitter_ratio must be between zero and one")


@dataclass(frozen=True, slots=True)
class OpenMAICHealth:
    service: str
    upstream_commit: str
    app_version: str
    contract_versions: tuple[str, ...]
    capabilities: tuple[str, ...]
    export_formats: tuple[str, ...]


EngineJobKind = Literal["outline", "content", "export"]


@dataclass(frozen=True, slots=True)
class EngineJob:
    tenant_id: str
    job_id: str
    kind: EngineJobKind
    status: str
    payload: Mapping[str, Any]


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidOpenMAICResponse()
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidOpenMAICResponse()
    return tuple(value)


class OpenMAICClient:
    """Signed, timeout-bounded client for one resolved tenant data plane."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        tenant_id: str,
        route_id: str,
        service_secret: SecretStr,
        timeouts: ClientTimeouts | None = None,
        retry_policy: PollRetryPolicy | None = None,
        now_seconds: Callable[[], int | float] = time.time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[], float] | None = None,
        known_job_kinds: Mapping[str, EngineJobKind] | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("OpenMAIC base URL is invalid")
        if not tenant_id or "\n" in tenant_id or "\r" in tenant_id:
            raise ValueError("tenant_id is invalid")
        if not route_id or "\n" in route_id or "\r" in route_id:
            raise ValueError("route_id is invalid")
        if not isinstance(service_secret, SecretStr):
            raise TypeError("service_secret must be a SecretStr")
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        self._route_id = route_id
        self._service_secret = service_secret
        self._timeouts = timeouts or ClientTimeouts()
        self._retry_policy = retry_policy or PollRetryPolicy()
        self._now_seconds = now_seconds
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter or random.random
        self._job_kinds: dict[str, EngineJobKind] = {}
        for job_id, kind in (known_job_kinds or {}).items():
            self.register_job(job_id, kind)

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        if (
            not isinstance(job_id, str)
            or job_id in {".", ".."}
            or _JOB_ID.fullmatch(job_id) is None
        ):
            raise ValueError("OpenMAIC job_id is invalid")
        return job_id

    def register_job(self, job_id: str, kind: EngineJobKind) -> None:
        """Register a job kind loaded from the durable yFeiSTAI job record."""

        job_id = self._validate_job_id(job_id)
        if kind not in {"outline", "content", "export"}:
            raise ValueError("OpenMAIC job kind is invalid")
        existing = self._job_kinds.get(job_id)
        if existing is not None and existing != kind:
            raise ValueError("OpenMAIC job kind conflicts with an existing registration")
        self._job_kinds[job_id] = kind

    @staticmethod
    def _status_path(job_id: str, kind: EngineJobKind) -> str:
        resource = {
            "outline": "outlines",
            "content": "classrooms",
            "export": "exports",
        }[kind]
        return f"/api/yfeistai/v1/{resource}/{job_id}"

    def _headers(
        self,
        *,
        method: str,
        path: str,
        job_id: str,
        idempotency_key: str,
        body: bytes,
    ) -> dict[str, str]:
        timestamp = int(self._now_seconds())
        return signed_service_headers(
            ServiceRequest(
                method=method,
                path=path,
                tenant_id=self._tenant_id,
                job_id=job_id,
                timestamp=timestamp,
                idempotency_key=idempotency_key,
                body=body,
            ),
            self._service_secret,
        )

    def _log_request(self, job_id: str) -> None:
        logger.info(
            "OpenMAIC request",
            extra={
                "tenant_id": self._tenant_id,
                "job_id": job_id,
                "route_id": self._route_id,
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        job_id: str,
        idempotency_key: str = "",
        body: bytes = b"",
        content_type: str | None = None,
    ) -> httpx.Response:
        headers = self._headers(
            method=method,
            path=path,
            job_id=job_id,
            idempotency_key=idempotency_key,
            body=body,
        )
        if content_type is not None:
            headers["content-type"] = content_type
        self._log_request(job_id)
        mapped_error: OpenMAICError
        try:
            async with asyncio.timeout(self._timeouts.total):
                response = await self._http.request(
                    method,
                    f"{self._base_url}{path}",
                    content=body,
                    headers=headers,
                    timeout=self._timeouts.httpx_timeout(),
                    follow_redirects=False,
                )
                response.raise_for_status()
        except (TimeoutError, httpx.TimeoutException):
            mapped_error = OpenMAICTimeout()
        except httpx.HTTPStatusError as exc:
            mapped_error = OpenMAICRequestFailed(exc.response.status_code)
        except httpx.RequestError:
            mapped_error = OpenMAICUnavailable()
        else:
            return response
        raise mapped_error

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        mapped_error: InvalidOpenMAICResponse
        try:
            value = response.json()
        except (json.JSONDecodeError, UnicodeError):
            mapped_error = InvalidOpenMAICResponse()
        else:
            if not isinstance(value, dict):
                raise InvalidOpenMAICResponse()
            return value
        raise mapped_error

    def _engine_job(
        self,
        response: httpx.Response,
        *,
        expected_job_id: str,
        kind: EngineJobKind,
    ) -> EngineJob:
        payload = self._json_object(response)
        tenant_id = _required_string(payload.get("tenantId"))
        job_id = _required_string(payload.get("jobId"))
        status = _required_string(payload.get("status"))
        if (
            tenant_id != self._tenant_id
            or job_id != expected_job_id
            or status not in _JOB_STATUSES[kind]
        ):
            raise InvalidOpenMAICResponse()
        return EngineJob(
            tenant_id=tenant_id,
            job_id=job_id,
            kind=kind,
            status=status,
            payload=dict(payload),
        )

    async def health(self) -> OpenMAICHealth:
        response = await self._request(
            "GET",
            "/api/yfeistai/v1/health",
            job_id="health",
        )
        payload = self._json_object(response)
        return OpenMAICHealth(
            service=_required_string(payload.get("service")),
            upstream_commit=_required_string(payload.get("upstreamCommit")),
            app_version=_required_string(payload.get("appVersion")),
            contract_versions=_string_tuple(payload.get("contractVersions")),
            capabilities=_string_tuple(payload.get("capabilities")),
            export_formats=_string_tuple(payload.get("exportFormats")),
        )

    async def assert_compatible(self) -> OpenMAICHealth:
        health = await self.health()
        if (
            health.service != "openmaic"
            or health.upstream_commit != EXPECTED_UPSTREAM_COMMIT
            or health.app_version != EXPECTED_APP_VERSION
            or SUPPORTED_CONTRACT_VERSION not in health.contract_versions
            or not REQUIRED_CAPABILITIES.issubset(health.capabilities)
            or not REQUIRED_EXPORT_FORMATS.issubset(health.export_formats)
        ):
            raise IncompatibleOpenMAIC()
        return health

    async def _submit(
        self,
        *,
        request: BaseModel,
        path: str,
        kind: EngineJobKind,
    ) -> EngineJob:
        tenant_id = _required_string(getattr(request, "tenant_id", None))
        job_id = self._validate_job_id(_required_string(getattr(request, "job_id", None)))
        idempotency_key = _required_string(getattr(request, "idempotency_key", None))
        if tenant_id != self._tenant_id:
            raise ValueError("request tenant does not match the resolved data plane")
        request_route_id = getattr(request, "data_plane_route_id", None)
        if request_route_id is not None and request_route_id != self._route_id:
            raise ValueError("request route does not match the resolved data plane")
        body = canonical_json_bytes(request)
        response = await self._request(
            "POST",
            path,
            job_id=job_id,
            idempotency_key=idempotency_key,
            body=body,
            content_type="application/json",
        )
        job = self._engine_job(response, expected_job_id=job_id, kind=kind)
        self.register_job(job_id, kind)
        return job

    async def submit_outline(self, request: GenerationRequest) -> EngineJob:
        if request.phase != "outline":
            raise ValueError("outline submission requires outline phase")
        return await self._submit(
            request=request,
            path="/api/yfeistai/v1/outlines",
            kind="outline",
        )

    async def submit_content(self, request: GenerationRequest) -> EngineJob:
        if request.phase not in {"content", "micro"}:
            raise ValueError("content submission requires content or micro phase")
        return await self._submit(
            request=request,
            path="/api/yfeistai/v1/classrooms",
            kind="content",
        )

    async def submit_export(self, request: ExportRequest) -> EngineJob:
        return await self._submit(
            request=request,
            path="/api/yfeistai/v1/exports",
            kind="export",
        )

    async def poll(self, engine_job_id: str) -> EngineJob:
        kind = self._job_kinds.get(engine_job_id)
        if kind is None:
            raise UnknownEngineJob()
        path = self._status_path(engine_job_id, kind)
        policy = self._retry_policy
        for attempt in range(policy.max_attempts):
            try:
                response = await self._request("GET", path, job_id=engine_job_id)
            except (OpenMAICTimeout, OpenMAICUnavailable):
                pass
            except OpenMAICRequestFailed as exc:
                if exc.status_code not in {429, 502, 503, 504}:
                    raise
            else:
                job = self._engine_job(
                    response,
                    expected_job_id=engine_job_id,
                    kind=kind,
                )
                if job.status in _TERMINAL_STATUSES:
                    return job
            if attempt + 1 < policy.max_attempts:
                base_delay = min(
                    policy.max_delay,
                    policy.initial_delay * (2**attempt),
                )
                delay = min(
                    policy.max_delay,
                    base_delay * (1 + policy.jitter_ratio * self._jitter()),
                )
                await self._sleep(delay)
        raise OpenMAICPollingExhausted()

    async def cancel(self, engine_job_id: str) -> None:
        engine_job_id = self._validate_job_id(engine_job_id)
        await self._request(
            "POST",
            f"/api/yfeistai/v1/jobs/{engine_job_id}/cancel",
            job_id=engine_job_id,
            idempotency_key=f"cancel-{engine_job_id}",
        )

    async def stream_artifact(self, path: str) -> AsyncIterator[bytes]:
        parsed = urlsplit(path)
        match = _ARTIFACT_PATH.fullmatch(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or match is None
            or any(part in {"", ".", ".."} for part in match.group("relative").split("/"))
        ):
            raise UnsafeArtifactPath()
        try:
            job_id = self._validate_job_id(match.group("job_id"))
        except ValueError:
            mapped_error = UnsafeArtifactPath()
        else:
            mapped_error = None
        if mapped_error is not None:
            raise mapped_error
        headers = self._headers(
            method="GET",
            path=path,
            job_id=job_id,
            idempotency_key="",
            body=b"",
        )
        self._log_request(job_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeouts.total
        stream_context = self._http.stream(
            "GET",
            f"{self._base_url}{path}",
            headers=headers,
            timeout=self._timeouts.httpx_timeout(),
            follow_redirects=False,
        )
        entered = False
        mapped_error: OpenMAICError | None = None

        def remaining() -> float:
            value = deadline - loop.time()
            if value <= 0:
                raise OpenMAICTimeout()
            return value

        try:
            async with asyncio.timeout(remaining()):
                response = await stream_context.__aenter__()
            entered = True
            response.raise_for_status()
            iterator = response.aiter_bytes().__aiter__()
            while True:
                try:
                    async with asyncio.timeout(remaining()):
                        chunk = await anext(iterator)
                except StopAsyncIteration:
                    break
                yield chunk
        except (TimeoutError, httpx.TimeoutException):
            mapped_error = OpenMAICTimeout()
        except httpx.HTTPStatusError as exc:
            mapped_error = OpenMAICRequestFailed(exc.response.status_code)
        except httpx.RequestError:
            mapped_error = OpenMAICUnavailable()
        finally:
            if entered:
                try:
                    await stream_context.__aexit__(None, None, None)
                except (TimeoutError, httpx.TimeoutException):
                    if mapped_error is None:
                        mapped_error = OpenMAICTimeout()
                except httpx.RequestError:
                    if mapped_error is None:
                        mapped_error = OpenMAICUnavailable()
        if mapped_error is not None:
            raise mapped_error


class ClientRouteBindingRepository(Protocol):
    """Re-read the route bound to an opaque selector result."""

    async def resolve_bound_route(
        self,
        selection: DataPlaneSelection,
    ) -> DataPlaneRouteRecord | None: ...


class ServiceSecretResolver(Protocol):
    """Resolve only the service-auth secret for the selected worker route."""

    def resolve(self, selection: DataPlaneSelection) -> SecretStr: ...


class _TeachingSettings(Protocol):
    enabled: bool


class OpenMAICClientFactory:
    """Build clients only after revalidating the persisted route binding."""

    def __init__(
        self,
        *,
        settings: _TeachingSettings,
        binding_repository: ClientRouteBindingRepository,
        service_secret_resolver: ServiceSecretResolver,
        timeouts: ClientTimeouts | None = None,
        retry_policy: PollRetryPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._binding_repository = binding_repository
        self._service_secret_resolver = service_secret_resolver
        self._timeouts = timeouts
        self._retry_policy = retry_policy

    @staticmethod
    def _is_current_binding(
        selection: DataPlaneSelection,
        route: DataPlaneRouteRecord,
    ) -> bool:
        expected_tenant_id = None if selection.mode == "shared" else selection.tenant_id
        expected_owner_key = "shared" if selection.mode == "shared" else selection.tenant_id
        return (
            route.route_id == selection.route_ref
            and route.tenant_id == expected_tenant_id
            and route.owner_key == expected_owner_key
            and route.mode == selection.mode
            and route.worker_pool == selection.worker_pool_ref
            and route.queue_name == selection.queue_ref
            and route.provider_profile_id == selection.provider_profile_ref
            and route.status == "active"
            and route.health_status == "healthy"
        )

    async def create(
        self,
        *,
        selection: DataPlaneSelection,
        http_client: httpx.AsyncClient,
        known_job_kinds: Mapping[str, EngineJobKind] | None = None,
    ) -> OpenMAICClient | None:
        if not self._settings.enabled:
            return None
        route = await self._binding_repository.resolve_bound_route(selection)
        if route is None or not self._is_current_binding(selection, route):
            raise DataPlaneUnavailable()
        service_secret = self._service_secret_resolver.resolve(selection)
        return OpenMAICClient(
            http_client,
            base_url=route.base_url,
            tenant_id=selection.tenant_id,
            route_id=selection.route_ref,
            service_secret=service_secret,
            timeouts=self._timeouts,
            retry_policy=self._retry_policy,
            known_job_kinds=known_job_kinds,
        )
