from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import auth as auth_router
from deeptutor.api.routers import classroom_jobs as jobs_router
from deeptutor.teaching.contracts import (
    GenerationRequest,
    TeachingBrief,
    canonical_json_bytes,
    canonical_outline_sha256,
    canonical_teaching_brief_sha256,
)
from deeptutor.teaching.object_store import ObjectStoreConfigurationError
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection, DataPlaneUnavailable
from deeptutor.teaching.permissions import ResourceScope, permissions_for_roles
from deeptutor.teaching.repositories.jobs import GenerationJobRecord
from deeptutor.teaching.tenant_context import TenantContext, require_tenant
from tests.teaching.test_contracts import (
    valid_generation_request,
    valid_outline_bundle,
)


def _sealed_teaching_brief(brief: dict[str, object] | None = None) -> TeachingBrief:
    parsed = TeachingBrief.model_validate(
        brief if brief is not None else valid_generation_request()["teaching_brief"]
    )
    digest = canonical_teaching_brief_sha256(parsed)
    return TeachingBrief.model_validate(
        {**parsed.model_dump(mode="json", by_alias=False), "content_sha256": digest}
    )


def _public_request() -> dict[str, object]:
    request = valid_generation_request()
    brief = _sealed_teaching_brief(request["teaching_brief"])
    request["teaching_brief_sha256"] = brief.content_sha256
    request.pop("tenant_id")
    request.pop("job_id")
    request.pop("data_plane_route_id")
    request.pop("confirmed_outline")
    request.pop("confirmed_outline_sha256")
    request.pop("teaching_brief")
    request.pop("priority")
    return request


def _internal_request(*, tenant_id: str = "tenant-1", job_id: str = "job-existing"):
    request = valid_generation_request()
    request["tenant_id"] = tenant_id
    request["job_id"] = job_id
    request["data_plane_route_id"] = "route-trusted"
    return request


def _export_payload(job_id: str) -> str:
    return canonical_json_bytes(
        {
            "schemaVersion": "1.0",
            "tenantId": "tenant-1",
            "jobId": job_id,
            "idempotencyKey": f"{job_id}-key",
            "classroomDocumentSha256": "a" * 64,
            "mediaManifestSha256": "b" * 64,
            "format": "pptx",
            "language": "zh-CN",
            "exportPolicy": {
                "includeSourceAttribution": True,
                "allowExternalLinks": False,
            },
        }
    ).decode()


def _context(
    user_id: str,
    role: str,
    *,
    tenant_id: str = "tenant-1",
    scope_type: str = "class",
    scope_id: str = "class-1",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name="tenant_test",
        user_id=user_id,
        permissions=permissions_for_roles(
            {role},
            scope_type=scope_type,
            scope_id=scope_id,
            tenant_id=tenant_id,
        ),
    )


def _detail(
    *,
    job_id: str = "job-existing",
    owner_id: str = "teacher-1",
    status: str = "awaiting_confirmation",
    phase: str = "outline",
    visibility: str = "private",
    job_kind: str = "generation",
    request_payload: str | None = None,
    result_payload: str | None = None,
    result_ref: str | None = None,
    export_format: str | None = None,
    resource_course_id: str | None = "course-1",
    resource_class_id: str | None = "class-1",
    public_request_sha256: str | None = None,
    progress_percent: int = 50,
    error_category: str | None = None,
    error_code: str | None = None,
):
    if request_payload is None:
        request_payload = canonical_json_bytes(
            GenerationRequest.model_validate(_internal_request(job_id=job_id))
        ).decode()
    return SimpleNamespace(
        tenant_id="tenant-1",
        job_id=job_id,
        job_kind=job_kind,
        phase=phase,
        status=status,
        priority=300,
        quota_units=2,
        actor_id=owner_id,
        owner_id=owner_id,
        visibility=visibility,
        request_id="request-existing",
        idempotency_key="key-existing",
        classroom_draft_id=None,
        batch_id=None,
        resource_course_id=resource_course_id,
        resource_class_id=resource_class_id,
        public_request_sha256=public_request_sha256,
        request_sha256=hashlib.sha256(request_payload.encode()).hexdigest(),
        data_plane_route_id="route-trusted",
        provider_profile_id="provider-private",
        worker_pool_ref="workers-private",
        queue_ref="queue-private",
        request_payload=request_payload,
        progress_percent=progress_percent,
        waiting_reason="outline_confirmation" if status == "awaiting_confirmation" else None,
        cancel_requested=False,
        error_category=error_category,
        error_code=error_code,
        result_payload=result_payload,
        result_ref=result_ref,
        retry_of_job_id=None,
        export_format=export_format,
    )


class FakeSelector:
    resolve_calls: list[str] = []

    async def resolve(self, tenant_id: str):
        self.resolve_calls.append(tenant_id)
        return DataPlaneSelection(
            tenant_id=tenant_id,
            route_ref="route-trusted",
            provider_profile_ref="provider-private",
            mode="shared",
            worker_pool_ref="workers-private",
            queue_ref="queue-private",
        )


class FakeTrustedTeachingBriefResolver:
    def __init__(
        self,
        *,
        brief: TeachingBrief | None = None,
        priority: str = "teacher",
        quota_units: int = 2,
        visibility: str = "private",
    ) -> None:
        self.brief = brief or _sealed_teaching_brief()
        self.priority = priority
        self.quota_units = quota_units
        self.visibility = visibility

    async def resolve(self, **_kwargs):
        return jobs_router.TrustedTeachingBrief(
            brief=self.brief,
            resource=ResourceScope(
                tenant_id=self.brief.tenant_id,
                course_id=self.brief.course_id,
                class_id=self.brief.target_class_id,
            ),
            priority=self.priority,
            quota_units=self.quota_units,
            visibility=self.visibility,
        )


class FakeRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}
        self.by_idempotency: dict[str, str] = {}
        self.created_requests: list[object] = []
        self.confirmed_payloads: list[tuple[str, str]] = []

    async def create_job_and_reserve(self, request):
        self.created_requests.append(request)
        existing_id = self.by_idempotency.get(request.idempotency_key)
        if existing_id is not None:
            job = self.jobs[existing_id]
            return GenerationJobRecord(
                tenant_id=job.tenant_id,
                job_id=job.job_id,
                job_kind=job.job_kind,
                phase=job.phase,
                status=job.status,
                priority=job.priority,
                quota_units=job.quota_units,
            )
        detail = _detail(
            job_id=request.job_id,
            owner_id=request.owner_id,
            status="quota_reserved",
            phase=request.phase,
            visibility=request.visibility,
            job_kind=request.job_kind,
            request_payload=request.request_payload,
            export_format=request.export_format,
            resource_course_id=request.resource_course_id,
            resource_class_id=request.resource_class_id,
            public_request_sha256=request.public_request_sha256,
        )
        detail.idempotency_key = request.idempotency_key
        detail.request_id = request.request_id
        detail.retry_of_job_id = request.retry_of_job_id
        self.jobs[request.job_id] = detail
        self.by_idempotency[request.idempotency_key] = request.job_id
        return GenerationJobRecord(
            tenant_id=request.tenant_id,
            job_id=request.job_id,
            job_kind=request.job_kind,
            phase=request.phase,
            status="quota_reserved",
            priority=request.priority_rank,
            quota_units=request.quota_units,
        )

    async def get_job_details(self, tenant_id: str, job_id: str):
        job = self.jobs.get(job_id)
        return job if job is not None and job.tenant_id == tenant_id else None

    async def requeue_confirmed_content(
        self,
        tenant_id: str,
        job_id: str,
        *,
        request_payload: str,
        request_sha256: str,
    ) -> bool:
        job = await self.get_job_details(tenant_id, job_id)
        if job is None or job.status != "awaiting_confirmation":
            return False
        self.confirmed_payloads.append((request_payload, request_sha256))
        job.phase = "content"
        job.status = "queued"
        job.request_payload = request_payload
        job.request_sha256 = request_sha256
        job.waiting_reason = None
        return True

    async def request_cancel(self, tenant_id: str, job_id: str):
        job = await self.get_job_details(tenant_id, job_id)
        if job is None or job.status in {"succeeded", "failed", "canceled"}:
            return None
        running = job.status in {
            "generating_outline",
            "generating_content",
            "exporting",
            "validating",
            "materializing",
        }
        if not running:
            job.status = "canceled"
        return SimpleNamespace(
            tenant_id=tenant_id,
            job_id=job_id,
            running=running,
            phase=job.phase,
            data_plane_route_id=job.data_plane_route_id,
            provider_profile_id=job.provider_profile_id,
            worker_pool_ref=job.worker_pool_ref,
            queue_ref=job.queue_ref,
        )

    async def finish_requested_cancellation(self, tenant_id: str, job_id: str) -> bool:
        job = await self.get_job_details(tenant_id, job_id)
        if job is None:
            return False
        job.status = "canceled"
        return True

    async def get_export_artifact(self, tenant_id: str, job_id: str):
        job = await self.get_job_details(tenant_id, job_id)
        if job is None or job.job_kind != "export" or job.status != "succeeded":
            return None
        return SimpleNamespace(
            relative_name="exports/classroom.pptx",
            object_key=job.result_ref,
            mime_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        )


class FakeCancellationGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def cancel(self, request) -> None:
        self.calls.append(request.job_id)


class FakeStore:
    def __init__(self) -> None:
        self.presign_calls: list[tuple[str, int]] = []
        self.open_calls: list[str] = []

    async def presign_download(self, key: str, expires_seconds: int) -> str:
        self.presign_calls.append((key, expires_seconds))
        return "https://signed.example/download-token"

    async def open(self, key: str):
        self.open_calls.append(key)

        async def chunks():
            yield b"verified-export"

        return chunks()


class FakeStores:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.tenants: list[str] = []

    async def store_for_tenant(self, tenant_id: str):
        self.tenants.append(tenant_id)
        return self.store


@pytest.fixture
def api_harness():
    FakeSelector.resolve_calls = []
    repository = FakeRepository()
    cancellation = FakeCancellationGateway()
    store = FakeStore()
    stores = FakeStores(store)
    app = FastAPI()
    app.include_router(jobs_router.router, prefix="/api/v1")
    app.dependency_overrides[jobs_router.get_job_repository] = lambda: repository
    app.dependency_overrides[jobs_router.get_data_plane_selector] = FakeSelector
    app.dependency_overrides[jobs_router.get_trusted_teaching_brief_resolver] = lambda: (
        FakeTrustedTeachingBriefResolver()
    )
    app.dependency_overrides[jobs_router.get_cancellation_gateway] = lambda: cancellation
    app.dependency_overrides[jobs_router.get_download_store_provider] = lambda: stores
    app.dependency_overrides[jobs_router.get_public_download_origins] = lambda: frozenset()
    app.dependency_overrides[auth_router.require_platform_enabled] = lambda: None
    app.dependency_overrides[require_tenant] = lambda: _context("teacher-1", "teacher")
    return app, repository, cancellation, stores, store


def test_duplicate_idempotency_key_returns_same_server_job(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    client = TestClient(app)

    first = client.post("/api/v1/classroom-jobs", json=_public_request())
    second = client.post("/api/v1/classroom-jobs", json=_public_request())

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert repository.created_requests[0].tenant_id == "tenant-1"
    assert repository.created_requests[0].data_plane_route_id == "route-trusted"
    rendered = json.dumps(first.json(), sort_keys=True)
    assert "provider-private" not in rendered
    assert "queue-private" not in rendered


def test_idempotent_replay_returns_before_data_plane_selection(api_harness) -> None:
    app, _repository, _cancellation, _stores, _store = api_harness

    class OneShotSelector:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve(self, tenant_id: str):
            self.calls += 1
            if self.calls > 1:
                raise DataPlaneUnavailable()
            return await FakeSelector().resolve(tenant_id)

    selector = OneShotSelector()
    app.dependency_overrides[jobs_router.get_data_plane_selector] = lambda: selector
    client = TestClient(app)

    first = client.post("/api/v1/classroom-jobs", json=_public_request())
    replay = client.post("/api/v1/classroom-jobs", json=_public_request())

    assert first.status_code == replay.status_code == 202
    assert first.json()["job_id"] == replay.json()["job_id"]
    assert selector.calls == 1


def test_idempotency_key_reuse_with_changed_public_request_is_rejected(
    api_harness,
) -> None:
    app, _repository, _cancellation, _stores, _store = api_harness
    client = TestClient(app)
    changed = _public_request()
    changed["duration_minutes"] = int(changed["duration_minutes"]) + 1

    first = client.post("/api/v1/classroom-jobs", json=_public_request())
    conflict = client.post("/api/v1/classroom-jobs", json=changed)

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Idempotency conflict"
    assert FakeSelector.resolve_calls == ["tenant-1"]


def test_create_rejects_client_selected_tenant_job_and_route(api_harness) -> None:
    app, _repository, _cancellation, _stores, _store = api_harness
    body = _public_request()
    body.update(
        tenantId="tenant-other",
        jobId="chosen-job",
        dataPlaneRouteId="chosen-route",
    )

    response = TestClient(app).post("/api/v1/classroom-jobs", json=body)

    assert response.status_code == 422


def test_create_rejects_client_selected_policy_and_embedded_brief(api_harness) -> None:
    app, _repository, _cancellation, _stores, _store = api_harness
    body = _public_request()
    body.update(
        teaching_brief=valid_generation_request()["teaching_brief"],
        priority="student_micro",
        quota_units=1,
        visibility="tenant",
        classroom_draft_id="draft-client-selected",
    )

    response = TestClient(app).post("/api/v1/classroom-jobs", json=body)

    assert response.status_code == 422


def test_create_rejects_trusted_brief_when_canonical_hash_is_stale(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    brief = _sealed_teaching_brief().model_dump(mode="json", by_alias=False)
    brief["network_policy"] = {
        "allow_web_access": True,
        "allowed_domains": ["untrusted.example"],
    }
    brief["safety_policy"] = {"policy_id": "disabled", "blocked_categories": []}
    app.dependency_overrides[jobs_router.get_trusted_teaching_brief_resolver] = lambda: (
        FakeTrustedTeachingBriefResolver(brief=TeachingBrief.model_validate(brief))
    )

    response = TestClient(app).post("/api/v1/classroom-jobs", json=_public_request())

    assert response.status_code == 409
    assert repository.created_requests == []
    assert FakeSelector.resolve_calls == []


def test_create_fails_closed_when_trusted_brief_crosses_tenant_boundary(
    api_harness,
) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    raw_brief = _sealed_teaching_brief().model_dump(mode="json", by_alias=False)
    raw_brief["tenant_id"] = "tenant-other"
    brief = _sealed_teaching_brief(raw_brief)

    class CrossTenantResolver:
        async def resolve(self, **_kwargs):
            return jobs_router.TrustedTeachingBrief(
                brief=brief,
                resource=ResourceScope(
                    tenant_id="tenant-1",
                    course_id=brief.course_id,
                    class_id=brief.target_class_id,
                ),
                priority="teacher",
                quota_units=2,
                visibility="private",
            )

    body = _public_request()
    body["teaching_brief_sha256"] = brief.content_sha256
    app.dependency_overrides[jobs_router.get_trusted_teaching_brief_resolver] = CrossTenantResolver

    response = TestClient(app).post("/api/v1/classroom-jobs", json=body)

    assert response.status_code == 503
    assert response.json()["detail"] == "Trusted teaching brief unavailable"
    assert repository.created_requests == []
    assert FakeSelector.resolve_calls == []


def test_create_fails_closed_without_a_trusted_brief_resolver(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    del app.dependency_overrides[jobs_router.get_trusted_teaching_brief_resolver]

    response = TestClient(app).post("/api/v1/classroom-jobs", json=_public_request())

    assert response.status_code == 503
    assert response.json()["detail"] == "Trusted teaching brief unavailable"
    assert repository.created_requests == []
    assert FakeSelector.resolve_calls == []


def test_content_phase_can_only_be_entered_through_outline_confirmation(
    api_harness,
) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    body = _public_request()
    body["phase"] = "content"

    response = TestClient(app).post("/api/v1/classroom-jobs", json=body)

    assert response.status_code == 422
    assert repository.created_requests == []


def test_create_requires_permission_covering_the_brief_resource(api_harness) -> None:
    app, _repository, _cancellation, _stores, _store = api_harness
    app.dependency_overrides[require_tenant] = lambda: _context(
        "teacher-1",
        "teacher",
        scope_id="class-other",
    )

    response = TestClient(app).post(
        "/api/v1/classroom-jobs",
        json=_public_request(),
    )

    assert response.status_code == 403
    assert FakeSelector.resolve_calls == []


def test_micro_generation_keeps_micro_contract_but_uses_content_queue_phase(
    api_harness,
) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    app.dependency_overrides[require_tenant] = lambda: _context("student-1", "student")
    body = _public_request()
    body["phase"] = "micro"
    body["classroom_mode"] = "micro"
    brief = _sealed_teaching_brief().model_dump(mode="json", by_alias=False)
    brief["classroom_mode"] = "micro"
    brief["content_mode"] = "open_creation"
    brief["source_snapshot"] = None
    brief["source_fragments"] = []
    brief["citations"] = []
    brief["source_refs"] = []
    brief["permission_summary"]["allowed_source_ids"] = []
    brief["permission_summary"]["allowed_fragment_ids"] = []
    sealed_brief = _sealed_teaching_brief(brief)
    body["teaching_brief_sha256"] = sealed_brief.content_sha256
    app.dependency_overrides[jobs_router.get_trusted_teaching_brief_resolver] = lambda: (
        FakeTrustedTeachingBriefResolver(brief=sealed_brief)
    )

    response = TestClient(app).post("/api/v1/classroom-jobs", json=body)

    assert response.status_code == 202
    created = repository.created_requests[-1]
    assert created.phase == "content"
    assert json.loads(created.request_payload)["phase"] == "micro"


def test_disabled_platform_rejects_before_job_repository_access(monkeypatch) -> None:
    class UnexpectedRepository:
        async def get_job_details(self, tenant_id: str, job_id: str):
            raise AssertionError("disabled platform must not access job repository")

    monkeypatch.setattr(
        auth_router,
        "load_platform_settings",
        lambda: SimpleNamespace(enabled=False),
    )
    app = FastAPI()
    app.include_router(jobs_router.router, prefix="/api/v1")
    app.dependency_overrides[jobs_router.get_job_repository] = UnexpectedRepository
    app.dependency_overrides[require_tenant] = lambda: _context("teacher-1", "teacher")

    response = TestClient(app).get("/api/v1/classroom-jobs/job-disabled")

    assert response.status_code == 409
    assert response.json()["detail"] == "Tenant platform is disabled"


def test_disabled_teaching_does_not_register_classroom_routes() -> None:
    from deeptutor.api.main import _register_classroom_job_routes

    app = FastAPI()

    registered = _register_classroom_job_routes(
        app,
        enabled=False,
        dependencies=[],
    )

    assert registered is False
    assert all("classroom-jobs" not in route.path for route in app.routes)
    assert all("classroom-exports" not in route.path for route in app.routes)


def test_student_cannot_read_another_users_private_job(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    repository.jobs["job-other"] = _detail(job_id="job-other", owner_id="teacher-1")
    app.dependency_overrides[require_tenant] = lambda: _context("student-1", "student")

    response = TestClient(app).get("/api/v1/classroom-jobs/job-other")

    assert response.status_code == 404


def test_non_private_job_still_requires_matching_resource_scope(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    repository.jobs["job-class"] = _detail(
        job_id="job-class",
        owner_id="teacher-other",
        visibility="class",
    )
    app.dependency_overrides[require_tenant] = lambda: _context(
        "teacher-1",
        "teacher",
        scope_id="class-other",
    )
    denied = TestClient(app).get("/api/v1/classroom-jobs/job-class")
    app.dependency_overrides[require_tenant] = lambda: _context("teacher-1", "teacher")
    allowed = TestClient(app).get("/api/v1/classroom-jobs/job-class")

    assert denied.status_code == 404
    assert allowed.status_code == 200


def test_job_access_uses_persisted_resource_binding_not_request_ancestry(
    api_harness,
) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    request = _internal_request(job_id="job-forged-course")
    request["teaching_brief"]["course_id"] = "course-forged"
    repository.jobs["job-forged-course"] = _detail(
        job_id="job-forged-course",
        owner_id="teacher-other",
        visibility="class",
        request_payload=canonical_json_bytes(GenerationRequest.model_validate(request)).decode(),
        resource_course_id="course-real",
        resource_class_id="class-1",
    )
    app.dependency_overrides[require_tenant] = lambda: _context(
        "teacher-forged",
        "teacher",
        scope_type="course",
        scope_id="course-forged",
    )

    denied = TestClient(app).get("/api/v1/classroom-jobs/job-forged-course")

    assert denied.status_code == 404


def test_retry_attributes_the_new_job_to_the_current_actor(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    repository.jobs["job-failed-other"] = _detail(
        job_id="job-failed-other",
        owner_id="teacher-other",
        status="failed",
        phase="content",
        visibility="class",
    )
    app.dependency_overrides[require_tenant] = lambda: _context("teacher-current", "teacher")

    response = TestClient(app).post(
        "/api/v1/classroom-jobs/job-failed-other/retry",
        json={"requestId": "request-retry-current", "idempotencyKey": "retry-current"},
    )

    assert response.status_code == 202
    retried = repository.created_requests[-1]
    assert retried.actor_id == "teacher-current"
    assert retried.owner_id == "teacher-other"


def test_unvalidated_outline_fields_are_not_exposed(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    untrusted_outline = valid_outline_bundle()
    untrusted_outline["providerSecret"] = "must-not-leak"
    repository.jobs["job-outline"] = _detail(
        job_id="job-outline",
        result_payload=canonical_json_bytes(untrusted_outline).decode(),
    )

    response = TestClient(app).get("/api/v1/classroom-jobs/job-outline")

    assert response.status_code == 200
    assert response.json()["outline"] is None
    assert "must-not-leak" not in response.text


def test_outline_confirmation_atomically_freezes_content_request(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    outline = valid_outline_bundle()
    repository.jobs["job-outline"] = _detail(
        job_id="job-outline",
        result_payload=canonical_json_bytes(outline).decode(),
    )

    response = TestClient(app).post(
        "/api/v1/classroom-jobs/job-outline/confirm-outline",
        json={
            "confirmedOutline": outline,
            "confirmedOutlineSha256": canonical_outline_sha256(outline),
        },
    )

    assert response.status_code == 202
    assert response.json()["phase"] == "content"
    payload, payload_sha256 = repository.confirmed_payloads[-1]
    assert hashlib.sha256(payload.encode()).hexdigest() == payload_sha256
    frozen = json.loads(payload)
    assert frozen["phase"] == "content"
    assert frozen["confirmedOutlineSha256"] == canonical_outline_sha256(outline)
    assert frozen["dataPlaneRouteId"] == "route-trusted"


def test_outline_confirmation_rejects_a_changed_hash_without_requeue(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    repository.jobs["job-outline"] = _detail(job_id="job-outline")

    response = TestClient(app).post(
        "/api/v1/classroom-jobs/job-outline/confirm-outline",
        json={
            "confirmedOutline": valid_outline_bundle(),
            "confirmedOutlineSha256": "0" * 64,
        },
    )

    assert response.status_code == 422
    assert repository.confirmed_payloads == []


def test_running_cancel_uses_gateway_and_returns_only_safe_state(api_harness) -> None:
    app, repository, cancellation, _stores, _store = api_harness
    repository.jobs["job-running"] = _detail(
        job_id="job-running",
        status="generating_outline",
    )

    response = TestClient(app).post("/api/v1/classroom-jobs/job-running/cancel")

    assert response.status_code == 202
    assert cancellation.calls == ["job-running"]
    assert response.json()["status"] == "canceled"
    assert "provider" not in json.dumps(response.json())


def test_explicit_retry_creates_a_new_linked_job(api_harness) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    repository.jobs["job-failed"] = _detail(
        job_id="job-failed",
        status="failed",
    )

    response = TestClient(app).post(
        "/api/v1/classroom-jobs/job-failed/retry",
        json={"requestId": "request-retry", "idempotencyKey": "key-retry"},
    )

    assert response.status_code == 202
    assert response.json()["job_id"] != "job-failed"
    retried = repository.created_requests[-1]
    assert retried.retry_of_job_id == "job-failed"
    assert json.loads(retried.request_payload)["jobId"] == retried.job_id


def test_export_download_streams_by_default_and_hides_internal_object_key(
    api_harness,
) -> None:
    app, repository, _cancellation, stores, store = api_harness
    object_key = "tenants/tenant-1/classrooms/export-export-1/versions/1/output.pptx"
    repository.jobs["export-1"] = _detail(
        job_id="export-1",
        status="succeeded",
        phase="export",
        job_kind="export",
        request_payload=_export_payload("export-1"),
        result_ref=object_key,
        export_format="pptx",
    )
    client = TestClient(app)

    status_response = client.get("/api/v1/classroom-exports/export-1")
    download_response = client.get(
        "/api/v1/classroom-exports/export-1/download?expiresSeconds=3600",
    )

    assert status_response.status_code == 200
    assert status_response.json()["download_ready"] is True
    assert object_key not in json.dumps(status_response.json())
    assert download_response.status_code == 200
    assert download_response.content == b"verified-export"
    assert stores.tenants == ["tenant-1"]
    assert store.presign_calls == []
    assert store.open_calls == [object_key]
    assert object_key not in download_response.text


def test_export_download_redirects_only_to_an_allowlisted_public_https_origin(
    api_harness,
) -> None:
    app, repository, _cancellation, _stores, store = api_harness
    object_key = "tenants/tenant-1/classrooms/export-public/versions/1/output.pptx"
    repository.jobs["export-public"] = _detail(
        job_id="export-public",
        status="succeeded",
        phase="export",
        job_kind="export",
        request_payload=_export_payload("export-public"),
        result_ref=object_key,
        export_format="pptx",
    )
    app.dependency_overrides[jobs_router.get_public_download_origins] = lambda: frozenset(
        {"https://signed.example"}
    )

    response = TestClient(app).get(
        "/api/v1/classroom-exports/export-public/download",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://signed.example/download-token"
    assert store.presign_calls == [(object_key, 60)]
    assert store.open_calls == []


def test_export_download_streams_when_signed_url_is_not_public_https(
    api_harness,
) -> None:
    app, repository, _cancellation, _stores, store = api_harness
    object_key = "tenants/tenant-1/classrooms/export-http/versions/1/output.pptx"
    repository.jobs["export-http"] = _detail(
        job_id="export-http",
        status="succeeded",
        phase="export",
        job_kind="export",
        request_payload=_export_payload("export-http"),
        result_ref=object_key,
        export_format="pptx",
    )

    async def internal_presign(key: str, expires_seconds: int) -> str:
        store.presign_calls.append((key, expires_seconds))
        return "http://minio:9000/private-download-token"

    store.presign_download = internal_presign
    app.dependency_overrides[jobs_router.get_public_download_origins] = lambda: frozenset(
        {"https://downloads.example"}
    )

    response = TestClient(app).get("/api/v1/classroom-exports/export-http/download")

    assert response.status_code == 200
    assert response.content == b"verified-export"
    assert store.presign_calls == [(object_key, 60)]
    assert store.open_calls == [object_key]


def test_local_export_download_uses_verified_streaming(api_harness) -> None:
    app, repository, _cancellation, stores, _store = api_harness
    object_key = "tenants/tenant-1/classrooms/export-local/versions/1/output.pptx"
    repository.jobs["export-local"] = _detail(
        job_id="export-local",
        status="succeeded",
        phase="export",
        job_kind="export",
        request_payload=_export_payload("export-local"),
        result_ref=object_key,
        export_format="pptx",
    )

    class LocalStore(FakeStore):
        async def presign_download(self, key: str, expires_seconds: int) -> str:
            self.presign_calls.append((key, expires_seconds))
            raise ObjectStoreConfigurationError("local verified stream required")

        async def open(self, key: str):
            assert key == object_key

            async def chunks():
                yield b"verified-local-export"

            return chunks()

    local_store = LocalStore()
    stores.store = local_store

    response = TestClient(app).get("/api/v1/classroom-exports/export-local/download")

    assert response.status_code == 200
    assert response.content == b"verified-local-export"
    assert response.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''classroom.pptx"
    )
    assert local_store.presign_calls == []


def test_other_tenant_or_owner_cannot_download_private_export(api_harness) -> None:
    app, repository, _cancellation, stores, _store = api_harness
    repository.jobs["export-private"] = _detail(
        job_id="export-private",
        owner_id="teacher-other",
        status="succeeded",
        phase="export",
        job_kind="export",
        request_payload=_export_payload("export-private"),
        result_ref="tenants/tenant-1/classrooms/export/versions/1/output.pptx",
        export_format="pptx",
    )

    wrong_owner = TestClient(app).get(
        "/api/v1/classroom-exports/export-private/download",
        follow_redirects=False,
    )
    app.dependency_overrides[require_tenant] = lambda: _context(
        "teacher-1",
        "teacher",
        tenant_id="tenant-other",
    )
    wrong_tenant = TestClient(app).get(
        "/api/v1/classroom-exports/export-private/download",
        follow_redirects=False,
    )

    assert wrong_owner.status_code == 404
    assert wrong_tenant.status_code == 404
    assert stores.tenants == []


def test_non_owner_cannot_access_class_visible_export_without_resource_binding(
    api_harness,
) -> None:
    app, repository, _cancellation, stores, _store = api_harness
    repository.jobs["export-class"] = _detail(
        job_id="export-class",
        owner_id="teacher-other",
        status="succeeded",
        phase="export",
        visibility="class",
        job_kind="export",
        request_payload=_export_payload("export-class"),
        result_ref="tenants/tenant-1/classrooms/export-class/versions/1/output.pptx",
        export_format="pptx",
    )

    status_response = TestClient(app).get("/api/v1/classroom-exports/export-class")
    download_response = TestClient(app).get("/api/v1/classroom-exports/export-class/download")

    assert status_response.status_code == 404
    assert download_response.status_code == 404
    assert stores.tenants == []


@pytest.mark.parametrize(
    (
        "job_status",
        "stored_progress",
        "stored_error",
        "expected_progress",
        "expected_cancellable",
        "expected_retryable",
        "expected_download",
        "expected_error",
    ),
    [
        ("succeeded", 90, "stale_error", 100, False, False, True, None),
        ("failed", 80, "engine_failed", 80, False, True, False, "engine_failed"),
        ("canceled", 20, "job_canceled", 20, False, True, False, "job_canceled"),
        ("queued", 10, "stale_error", 10, True, False, False, None),
    ],
)
def test_export_status_response_has_one_stable_lifecycle_contract(
    api_harness,
    job_status: str,
    stored_progress: int,
    stored_error: str,
    expected_progress: int,
    expected_cancellable: bool,
    expected_retryable: bool,
    expected_download: bool,
    expected_error: str | None,
) -> None:
    app, repository, _cancellation, _stores, _store = api_harness
    job_id = f"export-contract-{job_status}"
    repository.jobs[job_id] = _detail(
        job_id=job_id,
        status=job_status,
        phase="export",
        job_kind="export",
        request_payload=_export_payload(job_id),
        result_ref=(
            f"tenants/tenant-1/classrooms/{job_id}/versions/1/output.pptx"
            if job_status == "succeeded"
            else None
        ),
        export_format="pptx",
        progress_percent=stored_progress,
        error_category="engine" if stored_error else None,
        error_code=stored_error,
    )

    response = TestClient(app).get(f"/api/v1/classroom-exports/{job_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "export"
    assert payload["status"] == job_status
    assert payload["progress_percent"] == expected_progress
    assert payload["cancellable"] is expected_cancellable
    assert payload["retryable"] is expected_retryable
    assert payload["download_ready"] is expected_download
    assert payload["error_code"] == expected_error
    assert payload["error_category"] == ("engine" if expected_error else None)
