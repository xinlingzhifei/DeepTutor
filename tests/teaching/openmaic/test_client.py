from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
import hashlib
import json
import logging
import math
import traceback
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from deeptutor.teaching.contracts import (
    ExportPolicy,
    ExportRequest,
    GenerationRequest,
    canonical_json_bytes,
)
from deeptutor.teaching.export_worker import (
    ExportInputCommitReceipt,
    ExportInputDeclaration,
    ExportInputFileDeclaration,
)
import deeptutor.teaching.openmaic as openmaic_package
from deeptutor.teaching.openmaic.auth import (
    PrehashedServiceRequest,
    ServiceRequest,
    sign_prehashed_service_request,
    sign_service_request,
)
from deeptutor.teaching.openmaic.client import (
    ClientTimeouts,
    IncompatibleOpenMAIC,
    InvalidOpenMAICResponse,
    OpenMAICClient,
    OpenMAICClientFactory,
    OpenMAICPollingExhausted,
    OpenMAICRequestFailed,
    OpenMAICTimeout,
    OpenMAICUnavailable,
    PollRetryPolicy,
    UnsafeArtifactPath,
)
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneRouteRecord,
    DataPlaneSelection,
    DataPlaneUnavailable,
)

NOW = 1_770_000_000
SECRET = "SERVICE_SECRET_SENTINEL"
BASE_URL = "http://openmaic-shared:3000"


def _generation_request(*, job_id: str = "job-outline") -> GenerationRequest:
    return GenerationRequest.model_construct(
        schema_version="1.0",
        tenant_id="tenant-a",
        job_id=job_id,
        idempotency_key=f"idem-{job_id}",
        phase="outline",
    )


def _export_request(*, job_id: str = "job-export") -> ExportRequest:
    return ExportRequest(
        schema_version="1.0",
        tenant_id="tenant-a",
        job_id=job_id,
        idempotency_key=f"idem-{job_id}",
        classroom_document_sha256="a" * 64,
        media_manifest_sha256="b" * 64,
        format="pptx",
        language="zh-CN",
        export_policy=ExportPolicy(
            include_source_attribution=True,
            allow_external_links=False,
        ),
    )


def _export_input_declaration() -> ExportInputDeclaration:
    document_sha256 = hashlib.sha256(b"{}").hexdigest()
    return ExportInputDeclaration(
        tenant_id="tenant-a",
        job_id="job-export",
        idempotency_key="idem-job-export",
        classroom_document_sha256=document_sha256,
        media_manifest_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        files=(
            ExportInputFileDeclaration(
                file_id="file-document",
                kind="document",
                media_id=None,
                relative_name="classroom.json",
                sha256=document_sha256,
                size_bytes=2,
                mime_type="application/json",
            ),
        ),
    )


def _assert_valid_signature(request: httpx.Request, *, job_id: str) -> None:
    body = request.content
    timestamp = int(request.headers["x-yfeistai-timestamp"])
    signed = ServiceRequest(
        method=request.method,
        path=request.url.path,
        tenant_id=request.headers["x-yfeistai-tenant-id"],
        job_id=request.headers["x-yfeistai-job-id"],
        timestamp=timestamp,
        idempotency_key=request.headers["x-yfeistai-idempotency-key"],
        body=body,
    )
    assert timestamp == NOW
    assert signed.tenant_id == "tenant-a"
    assert signed.job_id == job_id
    assert request.headers["x-yfeistai-signature"] == sign_service_request(
        signed,
        SecretStr(SECRET),
    )


def _client(
    handler,
    *,
    retry_policy: PollRetryPolicy | None = None,
    sleep=None,
    jitter=None,
    known_job_kinds=None,
    follow_redirects: bool = False,
    timeouts: ClientTimeouts | None = None,
) -> tuple[OpenMAICClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=follow_redirects,
    )
    client = OpenMAICClient(
        http,
        base_url=BASE_URL,
        tenant_id="tenant-a",
        route_id="shared-primary",
        service_secret=SecretStr(SECRET),
        timeouts=timeouts or ClientTimeouts(connect=1.5, read=3.5, total=5.0),
        retry_policy=retry_policy,
        now_seconds=lambda: NOW,
        sleep=sleep,
        jitter=jitter,
        known_job_kinds=known_job_kinds,
    )
    return client, http


class ControlledByteStream(httpx.AsyncByteStream):
    def __init__(self, *, first_delay: float = 0.0) -> None:
        self.first_delay = first_delay
        self.closed = False

    async def __aiter__(self):
        if self.first_delay:
            await asyncio.sleep(self.first_delay)
        yield b"first"
        yield b"second"

    async def aclose(self) -> None:
        self.closed = True


class FailingCloseByteStream(ControlledByteStream):
    async def aclose(self) -> None:
        self.closed = True
        raise httpx.ReadError(
            "close failed",
            request=httpx.Request(
                "GET",
                BASE_URL,
                headers={"x-yfeistai-signature": "SIGNATURE_SENTINEL"},
            ),
        )


def _selection(*, mode: str = "shared") -> DataPlaneSelection:
    tenant_id = "tenant-a"
    return DataPlaneSelection(
        tenant_id=tenant_id,
        route_ref="shared-primary" if mode == "shared" else "dedicated-tenant-a",
        provider_profile_ref=("platform-default" if mode == "shared" else "provider-tenant-a"),
        mode=mode,
        worker_pool_ref=("shared-generation" if mode == "shared" else "generation-tenant-a"),
        queue_ref="openmaic.shared" if mode == "shared" else "openmaic.tenant-a",
    )


def _route(*, mode: str = "shared") -> DataPlaneRouteRecord:
    tenant_id = "tenant-a"
    return DataPlaneRouteRecord(
        route_id="shared-primary" if mode == "shared" else "dedicated-tenant-a",
        tenant_id=None if mode == "shared" else tenant_id,
        owner_key="shared" if mode == "shared" else tenant_id,
        mode=mode,
        base_url=BASE_URL if mode == "shared" else "http://openmaic-tenant-a:3000",
        worker_pool="shared-generation" if mode == "shared" else "generation-tenant-a",
        queue_name="openmaic.shared" if mode == "shared" else "openmaic.tenant-a",
        provider_profile_id=("platform-default" if mode == "shared" else "provider-tenant-a"),
        status="active",
        health_status="healthy",
    )


class RouteBindingRepository:
    def __init__(self, route: DataPlaneRouteRecord | None) -> None:
        self.route = route
        self.calls: list[DataPlaneSelection] = []

    async def resolve_bound_route(
        self,
        selection: DataPlaneSelection,
    ) -> DataPlaneRouteRecord | None:
        self.calls.append(selection)
        return self.route


class RecordingServiceSecretResolver:
    def __init__(self) -> None:
        self.calls: list[DataPlaneSelection] = []

    def resolve(self, selection: DataPlaneSelection) -> SecretStr:
        self.calls.append(selection)
        return SecretStr(SECRET)


def test_openmaic_package_exports_the_client_boundary() -> None:
    assert openmaic_package.OpenMAICClient is OpenMAICClient
    assert openmaic_package.OpenMAICClientFactory is OpenMAICClientFactory
    assert openmaic_package.MountedServiceSecretResolver is not None
    assert openmaic_package.ServiceRequest is ServiceRequest


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_client_timeouts_reject_nonfinite_values(value: float) -> None:
    with pytest.raises(ValueError):
        ClientTimeouts(connect=value, read=1.0, total=1.0)
    with pytest.raises(ValueError):
        ClientTimeouts(connect=1.0, read=value, total=1.0)
    with pytest.raises(ValueError):
        ClientTimeouts(connect=1.0, read=1.0, total=value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_poll_policy_rejects_nonfinite_values(value: float) -> None:
    with pytest.raises(ValueError):
        PollRetryPolicy(initial_delay=value)
    with pytest.raises(ValueError):
        PollRetryPolicy(max_delay=value)
    with pytest.raises(ValueError):
        PollRetryPolicy(jitter_ratio=value)


def test_factory_rebinds_the_selection_before_building_a_client() -> None:
    selection = _selection()
    repository = RouteBindingRepository(_route())
    secret_resolver = RecordingServiceSecretResolver()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openmaic-shared"
        return httpx.Response(
            200,
            json={
                "service": "openmaic",
                "upstreamCommit": "0cf2a330411681190e89f48e20f305345ff99f87",
                "appVersion": "0.3.1",
                "contractVersions": ["1.0"],
                "capabilities": [
                    "outline",
                    "content",
                    "micro",
                    "export",
                    "cancel",
                    "artifact-manifest",
                ],
                "exportFormats": [
                    "classroom_zip",
                    "pptx",
                    "offline_html",
                    "mp4",
                ],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    factory = OpenMAICClientFactory(
        settings=SimpleNamespace(enabled=True),
        binding_repository=repository,
        service_secret_resolver=secret_resolver,
    )

    client = asyncio.run(factory.create(selection=selection, http_client=http))
    assert client is not None
    asyncio.run(client.assert_compatible())
    asyncio.run(http.aclose())

    assert repository.calls == [selection]
    assert secret_resolver.calls == [selection]


@pytest.mark.parametrize(
    "route",
    [
        None,
        _route(mode="shared"),
        replace(_route(mode="dedicated"), tenant_id="tenant-b", owner_key="tenant-b"),
    ],
)
def test_factory_fails_closed_for_stale_or_cross_boundary_routes(
    route: DataPlaneRouteRecord | None,
) -> None:
    selection = _selection(mode="dedicated")
    repository = RouteBindingRepository(route)
    secret_resolver = RecordingServiceSecretResolver()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid route must not reach HTTP")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    factory = OpenMAICClientFactory(
        settings=SimpleNamespace(enabled=True),
        binding_repository=repository,
        service_secret_resolver=secret_resolver,
    )

    with pytest.raises(DataPlaneUnavailable):
        asyncio.run(factory.create(selection=selection, http_client=http))
    asyncio.run(http.aclose())

    assert repository.calls == [selection]
    assert secret_resolver.calls == []


def test_factory_preserves_disabled_legacy_mode_without_any_boundary_access() -> None:
    selection = _selection()
    repository = RouteBindingRepository(_route())
    secret_resolver = RecordingServiceSecretResolver()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled teaching must not reach HTTP")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    factory = OpenMAICClientFactory(
        settings=SimpleNamespace(enabled=False),
        binding_repository=repository,
        service_secret_resolver=secret_resolver,
    )

    assert asyncio.run(factory.create(selection=selection, http_client=http)) is None
    asyncio.run(http.aclose())
    assert repository.calls == []
    assert secret_resolver.calls == []


def test_client_rejects_incompatible_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_valid_signature(request, job_id="health")
        return httpx.Response(
            200,
            json={
                "service": "openmaic",
                "upstreamCommit": "0cf2a330411681190e89f48e20f305345ff99f87",
                "appVersion": "0.3.1",
                "contractVersions": ["2.0"],
                "capabilities": ["outline"],
                "exportFormats": [],
            },
        )

    client, http = _client(handler)
    with pytest.raises(IncompatibleOpenMAIC):
        asyncio.run(client.assert_compatible())
    asyncio.run(http.aclose())


@pytest.mark.parametrize(
    ("upstream_commit", "app_version"),
    [
        ("f" * 40, "0.3.1"),
        ("0cf2a330411681190e89f48e20f305345ff99f87", "0.3.2"),
    ],
)
def test_client_rejects_an_unpinned_engine_release(
    upstream_commit: str,
    app_version: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "service": "openmaic",
                "upstreamCommit": upstream_commit,
                "appVersion": app_version,
                "contractVersions": ["1.0"],
                "capabilities": [
                    "outline",
                    "content",
                    "micro",
                    "export",
                    "cancel",
                    "artifact-manifest",
                ],
                "exportFormats": [
                    "classroom_zip",
                    "pptx",
                    "offline_html",
                    "mp4",
                ],
            },
        )

    client, http = _client(handler)
    with pytest.raises(IncompatibleOpenMAIC):
        asyncio.run(client.assert_compatible())
    asyncio.run(http.aclose())


@pytest.mark.parametrize(
    ("capabilities", "export_formats"),
    [
        (
            ["outline", "content", "micro", "export", "cancel"],
            ["classroom_zip", "pptx", "offline_html", "mp4"],
        ),
        (
            [
                "outline",
                "content",
                "micro",
                "export",
                "cancel",
                "artifact-manifest",
            ],
            ["classroom_zip", "pptx", "offline_html"],
        ),
    ],
)
def test_client_rejects_an_incomplete_engine_capability_surface(
    capabilities: list[str],
    export_formats: list[str],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "service": "openmaic",
                "upstreamCommit": "0cf2a330411681190e89f48e20f305345ff99f87",
                "appVersion": "0.3.1",
                "contractVersions": ["1.0"],
                "capabilities": capabilities,
                "exportFormats": export_formats,
            },
        )

    client, http = _client(handler)
    with pytest.raises(IncompatibleOpenMAIC):
        asyncio.run(client.assert_compatible())
    asyncio.run(http.aclose())


@pytest.mark.parametrize(
    ("method_name", "expected_path", "request_factory"),
    [
        (
            "submit_outline",
            "/api/yfeistai/v1/outlines",
            _generation_request,
        ),
        (
            "submit_content",
            "/api/yfeistai/v1/classrooms",
            lambda: GenerationRequest.model_construct(
                schema_version="1.0",
                tenant_id="tenant-a",
                job_id="job-content",
                idempotency_key="idem-job-content",
                phase="content",
            ),
        ),
        (
            "submit_export",
            "/api/yfeistai/v1/exports",
            _export_request,
        ),
    ],
)
def test_submit_methods_sign_exact_canonical_json(
    method_name: str,
    expected_path: str,
    request_factory,
) -> None:
    submitted = request_factory()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        assert request.headers["content-type"] == "application/json"
        _assert_valid_signature(request, job_id=submitted.job_id)
        assert json.loads(request.content) == submitted.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        assert request.extensions["timeout"] == {
            "connect": 1.5,
            "read": 3.5,
            "write": 3.5,
            "pool": 1.5,
        }
        return httpx.Response(
            202,
            json={
                "tenantId": "tenant-a",
                "jobId": submitted.job_id,
                "status": "created",
            },
        )

    client, http = _client(handler)
    job = asyncio.run(getattr(client, method_name)(submitted))
    asyncio.run(http.aclose())

    assert job.tenant_id == "tenant-a"
    assert job.job_id == submitted.job_id
    assert job.status == "created"


def test_export_input_staging_uses_signed_logical_declarations_and_streams_bytes() -> (
    None
):
    declaration = _export_input_declaration()
    requests: list[str] = []
    consumed: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.method == "PUT":
            digest = request.headers["x-yfeistai-content-sha256"]
            signed = PrehashedServiceRequest(
                method=request.method,
                path=request.url.path,
                tenant_id=request.headers["x-yfeistai-tenant-id"],
                job_id=request.headers["x-yfeistai-job-id"],
                timestamp=int(request.headers["x-yfeistai-timestamp"]),
                idempotency_key=request.headers["x-yfeistai-idempotency-key"],
                body_sha256=digest,
            )
            assert request.headers["x-yfeistai-signature"] == (
                sign_prehashed_service_request(signed, SecretStr(SECRET))
            )
            body = await request.aread()
            consumed.append(body)
            assert hashlib.sha256(body).hexdigest() == digest
            return httpx.Response(
                200,
                json={
                    "tenantId": "tenant-a",
                    "jobId": "job-export",
                    "fileId": "file-document",
                    "sha256": digest,
                    "sizeBytes": 2,
                    "status": "uploaded",
                },
            )

        body = await request.aread()
        _assert_valid_signature(request, job_id="job-export")
        assert b"objectKey" not in body
        if request.url.path.endswith("/commit"):
            receipt_payload = {
                "schemaVersion": 1,
                "tenantId": "tenant-a",
                "jobId": "job-export",
                "idempotencyKey": "idem-job-export",
                "declarationSha256": declaration.declaration_sha256,
                "classroomDocumentSha256": declaration.classroom_document_sha256,
                "mediaManifestSha256": "b" * 64,
                "status": "committed",
            }
            return httpx.Response(
                200,
                json={
                    **receipt_payload,
                    "receiptSha256": hashlib.sha256(
                        canonical_json_bytes(receipt_payload)
                    ).hexdigest(),
                },
            )
        assert body == declaration.canonical_payload()
        return httpx.Response(
            200,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-export",
                "idempotencyKey": "idem-job-export",
                "declarationSha256": declaration.declaration_sha256,
                "status": "reserved",
            },
        )

    async def body() -> AsyncIterator[bytes]:
        yield b"{}"

    async def exercise() -> ExportInputCommitReceipt:
        client, http = _client(handler)
        try:
            await client.reserve_export_input(declaration)
            await client.upload_export_input_file(
                declaration,
                declaration.files[0],
                body(),
            )
            return await client.commit_export_input(declaration)
        finally:
            await http.aclose()

    receipt = asyncio.run(exercise())

    assert requests == [
        "/api/yfeistai/v1/export-inputs/job-export",
        "/api/yfeistai/v1/export-inputs/job-export/files/file-document",
        "/api/yfeistai/v1/export-inputs/job-export/commit",
    ]
    assert consumed == [b"{}"]
    receipt.validate(declaration)


def test_client_never_follows_a_redirect_with_service_signature_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                307,
                headers={"location": "https://attacker.invalid/collect"},
            )
        return httpx.Response(
            202,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-outline",
                "status": "created",
            },
        )

    client, http = _client(handler, follow_redirects=True)

    with pytest.raises(OpenMAICRequestFailed) as captured:
        asyncio.run(client.submit_outline(_generation_request()))
    asyncio.run(http.aclose())

    assert captured.value.status_code == 307
    assert len(requests) == 1
    assert requests[0].url.host == "openmaic-shared"


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("connect", OpenMAICUnavailable),
        ("timeout", OpenMAICTimeout),
        ("status", OpenMAICRequestFailed),
    ],
)
def test_transport_errors_drop_signed_request_and_body_from_the_exception_chain(
    failure: str,
    expected_error: type[Exception],
) -> None:
    source_sentinel = "SOURCE_BODY_SENTINEL"
    signature: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal signature
        signature = request.headers["x-yfeistai-signature"]
        if failure == "connect":
            raise httpx.ConnectError("transport failed", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("read failed", request=request)
        return httpx.Response(503, text="private-upstream-detail")

    request = _export_request()
    request = request.model_copy(update={"language": source_sentinel})
    client, http = _client(handler)

    with pytest.raises(expected_error) as captured:
        asyncio.run(client.submit_export(request))
    asyncio.run(http.aclose())

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert signature is not None and signature not in rendered
    assert source_sentinel not in rendered
    assert BASE_URL not in rendered
    assert SECRET not in rendered


def test_invalid_json_drops_the_response_body_from_the_exception_chain() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="SOURCE_BODY_SENTINEL")

    client, http = _client(handler)

    with pytest.raises(InvalidOpenMAICResponse) as captured:
        asyncio.run(client.health())
    asyncio.run(http.aclose())

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert "SOURCE_BODY_SENTINEL" not in rendered
    assert BASE_URL not in rendered


def test_poll_uses_bounded_exponential_backoff_and_jitter() -> None:
    responses = iter(["queued", "running", "succeeded"])
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "tenantId": "tenant-a",
                    "jobId": "job-content",
                    "status": "created",
                },
            )
        assert request.url.path == "/api/yfeistai/v1/classrooms/job-content"
        _assert_valid_signature(request, job_id="job-content")
        return httpx.Response(
            200,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-content",
                "status": next(responses),
            },
        )

    client, http = _client(
        handler,
        retry_policy=PollRetryPolicy(
            max_attempts=3,
            initial_delay=1.0,
            max_delay=2.0,
            jitter_ratio=0.25,
        ),
        sleep=record_sleep,
        jitter=lambda: 1.0,
    )
    asyncio.run(
        client.submit_content(
            GenerationRequest.model_construct(
                schema_version="1.0",
                tenant_id="tenant-a",
                job_id="job-content",
                idempotency_key="idem-job-content",
                phase="content",
            )
        )
    )

    job = asyncio.run(client.poll("job-content"))
    asyncio.run(http.aclose())

    assert job.status == "succeeded"
    assert delays == [1.25, 2.0]


def test_poll_stops_after_the_configured_attempt_limit() -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "tenantId": "tenant-a",
                    "jobId": "job-outline",
                    "status": "created",
                },
            )
        return httpx.Response(
            200,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-outline",
                "status": "queued",
            },
        )

    client, http = _client(
        handler,
        retry_policy=PollRetryPolicy(max_attempts=2),
        sleep=no_sleep,
    )
    asyncio.run(client.submit_outline(_generation_request()))

    with pytest.raises(OpenMAICPollingExhausted):
        asyncio.run(client.poll("job-outline"))
    asyncio.run(http.aclose())


def test_default_poll_policy_covers_the_longest_supported_render_window() -> None:
    policy = PollRetryPolicy()
    minimum_delay_budget = sum(
        min(policy.max_delay, policy.initial_delay * (2**attempt))
        for attempt in range(policy.max_attempts - 1)
    )

    assert minimum_delay_budget >= 120


def test_poll_retries_a_transient_connection_failure_with_the_same_bound() -> None:
    attempts = 0
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "tenantId": "tenant-a",
                    "jobId": "job-outline",
                    "status": "created",
                },
            )
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("transient", request=request)
        return httpx.Response(
            200,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-outline",
                "status": "succeeded",
            },
        )

    client, http = _client(
        handler,
        retry_policy=PollRetryPolicy(
            max_attempts=2,
            initial_delay=0.5,
            max_delay=1.0,
            jitter_ratio=0.0,
        ),
        sleep=record_sleep,
    )
    asyncio.run(client.submit_outline(_generation_request()))

    job = asyncio.run(client.poll("job-outline"))
    asyncio.run(http.aclose())

    assert job.status == "succeeded"
    assert attempts == 2
    assert delays == [0.5]


def test_poll_resumes_a_trusted_registered_job_without_prior_submit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/yfeistai/v1/classrooms/job-existing"
        _assert_valid_signature(request, job_id="job-existing")
        return httpx.Response(
            200,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-existing",
                "status": "succeeded",
            },
        )

    client, http = _client(
        handler,
        known_job_kinds={"job-existing": "content"},
    )

    job = asyncio.run(client.poll("job-existing"))
    asyncio.run(http.aclose())

    assert job.kind == "content"
    assert job.status == "succeeded"


def test_client_rejects_an_unknown_engine_job_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-outline",
                "status": "invented-state",
            },
        )

    client, http = _client(handler)

    with pytest.raises(InvalidOpenMAICResponse):
        asyncio.run(client.submit_outline(_generation_request()))
    asyncio.run(http.aclose())


def test_submit_rejects_a_known_status_from_the_wrong_job_kind() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-outline",
                "status": "exporting",
            },
        )

    client, http = _client(handler)

    with pytest.raises(InvalidOpenMAICResponse):
        asyncio.run(client.submit_outline(_generation_request()))
    asyncio.run(http.aclose())


def test_poll_rejects_a_known_status_from_the_wrong_job_kind() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-existing",
                "status": "generating_outline",
            },
        )

    client, http = _client(
        handler,
        known_job_kinds={"job-existing": "content"},
        retry_policy=PollRetryPolicy(max_attempts=1),
    )

    with pytest.raises(InvalidOpenMAICResponse):
        asyncio.run(client.poll("job-existing"))
    asyncio.run(http.aclose())


def test_cancel_uses_a_stable_idempotency_key() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-yfeistai-idempotency-key"])
        _assert_valid_signature(request, job_id="job-1")
        return httpx.Response(204)

    client, http = _client(handler)
    asyncio.run(client.cancel("job-1"))
    asyncio.run(client.cancel("job-1"))
    asyncio.run(http.aclose())

    assert seen == ["cancel-job-1", "cancel-job-1"]


@pytest.mark.parametrize(
    "job_id",
    [
        "job/foreign",
        "..",
        "job:foreign",
        "job%2Fforeign",
        "job?foreign",
        "job#foreign",
        r"job\foreign",
        "作业",
    ],
)
def test_cancel_rejects_a_non_segment_job_id_before_http(job_id: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid job id must not reach HTTP")

    client, http = _client(handler)

    with pytest.raises(ValueError, match="job_id"):
        asyncio.run(client.cancel(job_id))
    asyncio.run(http.aclose())


def test_stream_artifact_only_reads_the_signed_internal_artifact_path() -> None:
    async def consume(stream: AsyncIterator[bytes]) -> bytes:
        return b"".join([chunk async for chunk in stream])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/yfeistai/v1/artifacts/job-1/slides/deck.pptx"
        _assert_valid_signature(request, job_id="job-1")
        return httpx.Response(200, content=b"chunked-artifact")

    client, http = _client(handler)
    payload = asyncio.run(
        consume(client.stream_artifact("/api/yfeistai/v1/artifacts/job-1/slides/deck.pptx"))
    )
    asyncio.run(http.aclose())

    assert payload == b"chunked-artifact"


def test_stream_timeout_only_wraps_internal_reads_and_always_closes() -> None:
    stream = ControlledByteStream()
    unhandled: list[dict[str, object]] = []
    total_timeout = 0.5

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client, http = _client(
        handler,
        timeouts=ClientTimeouts(connect=1.0, read=1.0, total=total_timeout),
    )

    async def consume() -> tuple[bytes, bool]:
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        generator = client.stream_artifact("/api/yfeistai/v1/artifacts/job-1/media/file.bin")
        try:
            first = await anext(generator)
            await asyncio.sleep(total_timeout + 0.05)
            consumer_sleep_completed = True
            with pytest.raises(OpenMAICTimeout):
                await anext(generator)
            return first, consumer_sleep_completed
        finally:
            await generator.aclose()
            await asyncio.sleep(0)
            loop.set_exception_handler(previous_handler)

    first, consumer_sleep_completed = asyncio.run(consume())
    asyncio.run(http.aclose())

    assert first == b"first"
    assert consumer_sleep_completed
    assert stream.closed
    assert unhandled == []


def test_slow_artifact_stream_read_uses_the_remaining_total_deadline() -> None:
    stream = ControlledByteStream(first_delay=0.05)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client, http = _client(
        handler,
        timeouts=ClientTimeouts(connect=1.0, read=1.0, total=0.01),
    )

    with pytest.raises(OpenMAICTimeout):
        asyncio.run(
            anext(client.stream_artifact("/api/yfeistai/v1/artifacts/job-1/media/file.bin"))
        )
    asyncio.run(http.aclose())

    assert stream.closed


def test_stream_close_failure_is_mapped_without_a_signed_request_cause() -> None:
    stream = FailingCloseByteStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client, http = _client(handler)

    async def consume() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in client.stream_artifact(
                    "/api/yfeistai/v1/artifacts/job-1/media/file.bin"
                )
            ]
        )

    with pytest.raises(OpenMAICUnavailable) as captured:
        asyncio.run(consume())
    asyncio.run(http.aclose())

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SIGNATURE_SENTINEL" not in repr(captured.value)
    assert stream.closed


def test_stream_close_failure_cannot_override_a_stable_http_error() -> None:
    stream = FailingCloseByteStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=stream)

    client, http = _client(handler)

    with pytest.raises(OpenMAICRequestFailed) as captured:
        asyncio.run(
            anext(client.stream_artifact("/api/yfeistai/v1/artifacts/job-1/media/file.bin"))
        )
    asyncio.run(http.aclose())

    assert captured.value.status_code == 503
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SIGNATURE_SENTINEL" not in repr(captured.value)
    assert stream.closed


@pytest.mark.parametrize(
    "path",
    [
        "https://attacker.invalid/steal",
        "/api/yfeistai/v1/artifacts/job-1/../secret",
        "/api/yfeistai/v1/artifacts/job-1/%2e%2e/secret",
        "/api/yfeistai/v1/artifacts/../secret",
        "/api/yfeistai/v1/artifacts/./secret",
    ],
)
def test_stream_artifact_rejects_urls_and_traversal_before_http(
    path: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsafe artifact path must not reach HTTP")

    client, http = _client(handler)

    with pytest.raises(UnsafeArtifactPath):
        asyncio.run(anext(client.stream_artifact(path)))
    asyncio.run(http.aclose())


def test_logs_are_limited_to_tenant_job_and_route_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "tenantId": "tenant-a",
                "jobId": "job-outline",
                "status": "created",
            },
        )

    client, http = _client(handler)
    with caplog.at_level(logging.INFO, logger="deeptutor.teaching.openmaic.client"):
        asyncio.run(client.submit_outline(_generation_request()))
    asyncio.run(http.aclose())

    client_records = [
        record
        for record in caplog.records
        if record.name == "deeptutor.teaching.openmaic.client"
    ]
    record = client_records[-1]
    assert record.getMessage() == "OpenMAIC request"
    assert record.tenant_id == "tenant-a"
    assert record.job_id == "job-outline"
    assert record.route_id == "shared-primary"
    client_text = "\n".join(record.getMessage() for record in client_records)
    assert SECRET not in client_text
    assert "openmaic-shared" not in client_text
