from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deeptutor.api.routers import classroom_batches
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.openmaic.data_planes import DataPlaneUnavailable
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.services.batches import (
    BatchAccessDenied,
    BatchItemRecord,
    BatchItemRejected,
    BatchJobRecord,
    BatchOutlineConflict,
    BatchRetryResult,
    InvalidBatchRequest,
)
from deeptutor.teaching.services.classrooms import InvalidClassroomState
from deeptutor.teaching.source_snapshots import SourceAccessDenied
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


def _context(*, roles: set[str] | None = None) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant_a",
        user_id="author-a",
        permissions=permissions_for_roles(
            roles or {"content_author"},
            scope_type="tenant",
            scope_id="tenant-a",
        ),
    )


def _batch() -> BatchJobRecord:
    return BatchJobRecord(
        id="batch-a",
        tenant_id="tenant-a",
        actor_id="author-a",
        status="awaiting_confirmation",
        item_count=2,
        succeeded_count=0,
        failed_count=0,
        items=(
            BatchItemRecord(
                id="a",
                batch_id="batch-a",
                status="awaiting_confirmation",
                generation_job_id="job-a",
                classroom_draft_id="draft-a",
                classroom_asset_id="asset-a",
            ),
            BatchItemRecord(
                id="b",
                batch_id="batch-a",
                status="awaiting_confirmation",
                generation_job_id="job-b",
                classroom_draft_id="draft-b",
                classroom_asset_id="asset-b",
            ),
        ),
    )


class _Service:
    def __init__(self) -> None:
        self.batch = _batch()
        self.created = None
        self.confirmed = None
        self.listed = None

    async def create(self, context, items, *, idempotency_key):
        self.created = (context, items, idempotency_key)
        return self.batch

    async def list(self, context, *, limit=50, offset=0):
        self.listed = (context, limit, offset)
        return (self.batch,)

    async def get(self, context, batch_id):
        return self.batch if batch_id == self.batch.id else None

    async def confirm_outline(self, context, batch_id, item_id, *, revision, outline_sha256):
        self.confirmed = ((item_id, revision, outline_sha256),)
        return self.batch

    async def confirm_outlines(self, context, batch_id, confirmations):
        self.confirmed = confirmations
        return self.batch

    async def retry_item(self, context, batch_id, item_id):
        item = replace(self.batch.items[1], status="queued", generation_job_id="job-b-retry")
        return BatchRetryResult(parent_item_id=item_id, item=item)

    async def cancel(self, context, batch_id):
        return replace(self.batch, status="canceled")


def _client(
    service: _Service,
    context: TenantContext | None = None,
    *,
    raise_server_exceptions: bool = True,
) -> TestClient:
    application = FastAPI()
    application.include_router(classroom_batches.router, prefix="/api/v1")
    application.dependency_overrides[require_tenant] = lambda: context or _context()
    application.dependency_overrides[classroom_batches.get_batch_service] = lambda: service
    return TestClient(
        application,
        raise_server_exceptions=raise_server_exceptions,
    )


def _classroom_item(item_id: str) -> dict[str, object]:
    return {
        "itemId": item_id,
        "title": f"Motion {item_id}",
        "courseId": "course-a",
        "classId": "class-a",
        "objective": "Explain motion",
        "gradeBand": "grade-8",
        "audience": "intermediate",
        "durationMinutes": 45,
        "classroomMode": "full",
        "webPolicy": "disabled",
        "templateId": "template-a",
        "templateVersion": "1",
        "knowledgePoints": [
            {
                "knowledgePointId": "kp-motion",
                "title": "Motion",
                "description": "Describe displacement and velocity",
            }
        ],
        "contentMode": "open_creation",
        "openCreationAcknowledged": True,
        "requestedExports": ["classroom_zip"],
    }


def test_content_author_can_create_a_durable_batch() -> None:
    service = _Service()

    response = _client(service).post(
        "/api/v1/classroom-batches",
        headers={"Idempotency-Key": "batch-request-1"},
        json={"items": [_classroom_item("a"), _classroom_item("b")]},
    )

    assert response.status_code == 202
    assert response.json()["id"] == "batch-a"
    assert [item["id"] for item in response.json()["items"]] == ["a", "b"]
    assert "resourceCourseId" not in response.json()["items"][0]
    assert "resourceClassId" not in response.json()["items"][0]
    assert service.created is not None
    assert service.created[2] == "batch-request-1"
    assert [item.id for item in service.created[1]] == ["a", "b"]


def test_selected_outline_confirmations_forward_each_revision_and_hash() -> None:
    service = _Service()

    response = _client(service).post(
        "/api/v1/classroom-batches/batch-a/confirm-outlines",
        json={
            "items": [
                {"itemId": "a", "revision": 3, "outlineSha256": "a" * 64},
            ]
        },
    )

    assert response.status_code == 202
    assert service.confirmed == (("a", 3, "a" * 64),)


def test_outline_conflict_is_an_explicit_409_without_hash_leakage() -> None:
    class _ConflictService(_Service):
        async def confirm_outline(self, *args, **kwargs):
            raise BatchOutlineConflict("stored hash was secret")

    response = _client(_ConflictService()).post(
        "/api/v1/classroom-batches/batch-a/items/a/confirm-outline",
        json={"revision": 3, "outlineSha256": "a" * 64},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Batch outline confirmation conflicts"}
    assert "secret" not in response.text


def test_batch_access_denial_is_an_explicit_403() -> None:
    class _DeniedService(_Service):
        async def create(self, *args, **kwargs):
            raise BatchAccessDenied("private scope")

    response = _client(_DeniedService(), _context(roles={"student"})).post(
        "/api/v1/classroom-batches",
        headers={"Idempotency-Key": "batch-request-1"},
        json={"items": [_classroom_item("a")]},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Batch access denied"}


def test_batch_list_forwards_bounded_pagination() -> None:
    service = _Service()
    client = _client(service)

    response = client.get("/api/v1/classroom-batches?limit=17&offset=4")

    assert response.status_code == 200
    assert service.listed is not None
    assert service.listed[1:] == (17, 4)
    assert client.get("/api/v1/classroom-batches?limit=101").status_code == 422
    assert client.get("/api/v1/classroom-batches?offset=-1").status_code == 422


def test_explicit_invalid_batch_request_is_the_only_service_request_422() -> None:
    class _InvalidService(_Service):
        def __init__(self, error: Exception) -> None:
            super().__init__()
            self.error = error

        async def create(self, *args, **kwargs):
            raise self.error

    for error in (
        InvalidBatchRequest("secret invalid input"),
        BatchItemRejected("secret rejected item"),
    ):
        response = _client(_InvalidService(error)).post(
            "/api/v1/classroom-batches",
            headers={"Idempotency-Key": "batch-request-1"},
            json={"items": [_classroom_item("a")]},
        )

        assert response.status_code == 422
        assert response.json() == {"detail": "Batch request is invalid"}
        assert "secret" not in response.text


def test_post_workflow_failures_are_stable_503_without_detail_leakage() -> None:
    class _SideEffectFailureService(_Service):
        def __init__(self, error: Exception) -> None:
            super().__init__()
            self.error = error
            self.side_effects = 0

        async def create(self, *args, **kwargs):
            self.side_effects += 1
            raise self.error

    request = {
        "headers": {"Idempotency-Key": "batch-request-1"},
        "json": {"items": [_classroom_item("a")]},
    }
    for error in (
        ValueError("secret post-workflow value"),
        InvalidClassroomState("secret live job invariant"),
        PermissionError("secret unexpected permission"),
    ):
        service = _SideEffectFailureService(error)
        response = _client(
            service,
            raise_server_exceptions=False,
        ).post("/api/v1/classroom-batches", **request)

        assert response.status_code == 503
        assert response.json() == {"detail": "Batch processing is unavailable"}
        assert "secret" not in response.text
        assert service.side_effects == 1


def test_unrelated_member_gets_empty_list_and_opaque_404() -> None:
    class _RestrictedService(_Service):
        async def list(self, context, *, limit=50, offset=0):
            return ()

        async def get(self, context, batch_id):
            return None

    client = _client(
        _RestrictedService(),
        _context(roles={"student"}),
    )

    listed = client.get("/api/v1/classroom-batches")
    fetched = client.get("/api/v1/classroom-batches/batch-a")

    assert listed.status_code == 200
    assert listed.json() == {"items": []}
    assert fetched.status_code == 404
    assert fetched.json() == {"detail": "Batch not found"}
    assert "asset-a" not in fetched.text


def test_source_access_denial_and_data_plane_failure_have_stable_statuses() -> None:
    class _SourceDeniedService(_Service):
        async def create(self, *args, **kwargs):
            raise SourceAccessDenied("private source details")

    class _UnavailableService(_Service):
        async def create(self, *args, **kwargs):
            raise DataPlaneBindingUnavailable()

    class _SelectorUnavailableService(_Service):
        async def create(self, *args, **kwargs):
            raise DataPlaneUnavailable()

    request = {
        "headers": {"Idempotency-Key": "batch-request-1"},
        "json": {"items": [_classroom_item("a")]},
    }
    denied = _client(_SourceDeniedService()).post(
        "/api/v1/classroom-batches",
        **request,
    )
    unavailable = _client(_UnavailableService()).post(
        "/api/v1/classroom-batches",
        **request,
    )
    selector_unavailable = _client(
        _SelectorUnavailableService(),
        raise_server_exceptions=False,
    ).post("/api/v1/classroom-batches", **request)

    assert denied.status_code == 403
    assert denied.json() == {"detail": "Batch access denied"}
    assert "private source" not in denied.text
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Batch processing is unavailable"}
    assert selector_unavailable.status_code == 503
    assert selector_unavailable.json() == {
        "detail": "Batch processing is unavailable"
    }
    assert "data plane is unavailable" not in selector_unavailable.text


def test_list_get_retry_and_cancel_routes_expose_batch_state() -> None:
    client = _client(_Service())

    listed = client.get("/api/v1/classroom-batches")
    fetched = client.get("/api/v1/classroom-batches/batch-a")
    retried = client.post("/api/v1/classroom-batches/batch-a/items/b/retry")
    canceled = client.post("/api/v1/classroom-batches/batch-a/cancel")

    assert listed.status_code == 200
    assert [batch["id"] for batch in listed.json()["items"]] == ["batch-a"]
    assert fetched.status_code == 200
    assert retried.status_code == 202
    assert retried.json()["parentItemId"] == "b"
    assert retried.json()["item"]["generationJobId"] == "job-b-retry"
    assert canceled.status_code == 202
    assert canceled.json()["status"] == "canceled"


def test_disabled_teaching_does_not_register_classroom_batch_routes() -> None:
    from deeptutor.api.main import _register_classroom_batch_routes

    application = FastAPI()
    registered = _register_classroom_batch_routes(
        application,
        enabled=False,
        dependencies=[],
    )

    assert registered is False
    assert all("/classroom-batches" not in route.path for route in application.routes)
