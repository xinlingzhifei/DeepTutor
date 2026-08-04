from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from deeptutor.teaching.contracts import (
    OutlineBundle,
    OutlineConfirmationMetadata,
    TeachingBrief,
    canonical_outline_sha256,
)
from deeptutor.teaching.job_route_binding import DataPlaneBindingUnavailable
from deeptutor.teaching.permissions import ResourceScope, permissions_for_roles
from deeptutor.teaching.quota import InsufficientQuota
from deeptutor.teaching.services.batches import (
    BatchIdempotencyConflict,
    BatchItemInput,
    BatchItemRecord,
    BatchItemRejected,
    BatchJobRecord,
    BatchNotFound,
    BatchOutlineConflict,
    BatchRetryResult,
    BatchService,
    InvalidBatchRequest,
    SqlAlchemyBatchClassroomGateway,
)
from deeptutor.teaching.services.classrooms import (
    ClassroomPreflightRejected,
    InvalidClassroomState,
    SqlAlchemyClassroomGeneration,
)
from deeptutor.teaching.source_snapshots import SourceSnapshotUnavailable
from deeptutor.teaching.tenant_context import TenantContext


def _context(
    *,
    user_id: str = "author-a",
    roles: set[str] | None = None,
) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant_a",
        user_id=user_id,
        permissions=permissions_for_roles(
            roles or {"content_author"},
            scope_type="tenant",
            scope_id="tenant-a",
        ),
    )


def _request(title: str) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        course_id="course-a",
        class_id="class-a",
        objective="Explain motion",
        grade_band="grade-8",
        audience="intermediate",
        duration_minutes=45,
        classroom_mode="full",
        web_policy="disabled",
        allowed_web_domains=(),
        template_id="template-a",
        template_version="1",
        knowledge_points=(
            SimpleNamespace(
                knowledge_point_id="kp-motion",
                title="Motion",
                description="Describe displacement and velocity",
            ),
        ),
        content_mode="open_creation",
        open_creation_acknowledged=True,
        source_type=None,
        source_ref=None,
        requested_exports=("classroom_zip",),
    )


class _BatchRepository:
    def __init__(self) -> None:
        self.batch: BatchJobRecord | None = None
        self.job_statuses: dict[str, str] = {}
        self.list_calls: list[tuple[int, int]] = []
        self.list_access_scopes: list[object | None] = []

    async def create(self, batch_id, actor_id, item_ids):
        if self.batch is None:
            self.batch = BatchJobRecord(
                id=batch_id,
                tenant_id="tenant-a",
                actor_id=actor_id,
                status="queued",
                item_count=len(item_ids),
                succeeded_count=0,
                failed_count=0,
                items=tuple(
                    BatchItemRecord(
                        id=item_id,
                        batch_id=batch_id,
                        status="queued",
                    )
                    for item_id in item_ids
                ),
            )
        return self.batch

    async def get(self, batch_id):
        return self.batch if self.batch is not None and self.batch.id == batch_id else None

    async def list(self, *, access_scope=None, limit=50, offset=0):
        self.list_calls.append((limit, offset))
        self.list_access_scopes.append(access_scope)
        records = (self.batch,) if self.batch is not None else ()
        return records[offset : offset + limit]

    async def bind_item(self, batch_id, item_id, *, generation_job_id, classroom_draft_id, classroom_asset_id, status):
        assert self.batch is not None
        self.batch = replace(
            self.batch,
            items=tuple(
                replace(
                    item,
                    generation_job_id=generation_job_id,
                    classroom_draft_id=classroom_draft_id,
                    classroom_asset_id=classroom_asset_id,
                    resource_course_id="course-a",
                    resource_class_id="class-a",
                    status=status,
                )
                if item.id == item_id
                else item
                for item in self.batch.items
            ),
        )
        return await self.set_item_status(batch_id, item_id, status)

    async def set_item_status(self, batch_id, item_id, status):
        assert self.batch is not None
        items = tuple(
            replace(item, status=status) if item.id == item_id else item
            for item in self.batch.items
        )
        succeeded = sum(item.status == "succeeded" for item in items)
        failed = sum(item.status == "failed" for item in items)
        canceled = sum(item.status == "canceled" for item in items)
        terminal = succeeded + failed + canceled == len(items)
        waiting = any(item.status == "awaiting_confirmation" for item in items)
        batch_status = (
            "partially_succeeded"
            if terminal and succeeded and (failed or canceled)
            else "succeeded"
            if terminal and succeeded == len(items)
            else "failed"
            if terminal and failed
            else "canceled"
            if terminal
            else "awaiting_confirmation"
            if waiting
            else "running"
        )
        self.batch = replace(
            self.batch,
            status=batch_status,
            succeeded_count=succeeded,
            failed_count=failed,
            items=items,
        )
        return next(item for item in items if item.id == item_id)

    async def bind_rejected_item(self, batch_id, item_id, *, generation_job_id):
        assert self.batch is not None
        self.job_statuses[generation_job_id] = "failed"
        self.batch = replace(
            self.batch,
            items=tuple(
                replace(
                    candidate,
                    generation_job_id=generation_job_id,
                    classroom_draft_id=None,
                    classroom_asset_id=None,
                    resource_course_id="course-a",
                    resource_class_id="class-a",
                    status="failed",
                )
                if candidate.id == item_id
                else candidate
                for candidate in self.batch.items
            ),
        )
        return await self.set_item_status(batch_id, item_id, "failed")

    async def rebind_failed_item(self, batch_id, item_id, *, expected_job_id, new_job_id):
        assert self.batch is not None
        item = next(candidate for candidate in self.batch.items if candidate.id == item_id)
        assert item.status == "failed"
        assert item.generation_job_id == expected_job_id
        new_status = self.job_statuses.get(
            new_job_id,
            "failed" if "-rejected-" in new_job_id else "queued",
        )
        self.batch = replace(
            self.batch,
            items=tuple(
                replace(
                    candidate,
                    generation_job_id=new_job_id,
                    classroom_draft_id=(
                        None if new_status == "failed" else f"draft-{item_id}-retry"
                    ),
                    classroom_asset_id=(
                        None if new_status == "failed" else f"asset-{item_id}"
                    ),
                    status=new_status,
                )
                if candidate.id == item_id
                else candidate
                for candidate in self.batch.items
            ),
        )
        await self.set_item_status(batch_id, item_id, new_status)
        return next(candidate for candidate in self.batch.items if candidate.id == item_id)


class _Classrooms:
    def __init__(self, jobs=None) -> None:
        self.records: dict[str, SimpleNamespace] = {}
        self.jobs = jobs
        self.edit_on_confirm: set[str] = set()
        self.confirmation_bindings: list[tuple[str, int, str]] = []

    async def create(
        self,
        context,
        request,
        *,
        batch_id,
        item_id,
        retry_of_job_id=None,
    ):
        record = SimpleNamespace(
            asset_id=f"asset-{item_id}",
            draft_id=f"draft-{item_id}",
            job_id=f"job-{item_id}",
            status="failed" if request.title == "invalid" else "succeeded",
        )
        self.records[record.asset_id] = record
        if self.jobs is not None:
            self.jobs.created(item_id)
            if retry_of_job_id is not None:
                record.job_id = f"job-{item_id}-real-retry-{self.jobs.counts[item_id]}"
                self.jobs.lineage[record.job_id] = retry_of_job_id
        return record

    async def get(self, context, asset_id):
        return self.records.get(asset_id)

    async def confirm_outline(
        self,
        context,
        asset_id,
        *,
        expected_revision,
        expected_outline_sha256,
    ):
        record = self.records[asset_id]
        self.confirmation_bindings.append(
            (asset_id, expected_revision, expected_outline_sha256)
        )
        if asset_id in self.edit_on_confirm:
            record.revision += 1
            raise BatchOutlineConflict("outline changed while confirming")
        record.status = "queued"
        return record


class _Jobs:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.lineage: dict[str, str | None] = {}
        self.replay: dict[str, object] = {}
        self.cancel_calls: list[str] = []

    def created(self, item_id: str) -> None:
        self.counts[item_id] = self.counts.get(item_id, 0) + 1

    async def retry(self, context, *, batch_id, item_id, job_id):
        self.created(item_id)
        retried = f"{job_id}-retry-{self.counts[item_id]}"
        self.lineage[retried] = job_id
        return retried

    async def record_rejected(
        self,
        context,
        *,
        batch_id,
        item_id,
        request,
        retry_of_job_id=None,
    ):
        self.created(item_id)
        job_id = f"job-{item_id}-rejected-{self.counts[item_id]}"
        self.lineage[job_id] = retry_of_job_id
        self.replay[job_id] = request
        return job_id

    async def rejected_input(self, context, *, job_id):
        return self.replay[job_id]

    async def cancel_unstarted(self, context, *, job_id):
        self.cancel_calls.append(job_id)
        return not job_id.endswith("running")


class _FailingClassroomService:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def create(self, *args, **kwargs):
        raise self._error


@pytest.mark.asyncio
async def test_batch_gateway_terminalizes_quota_failure_but_preserves_data_plane_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SqlAlchemyBatchClassroomGateway(None, None, None, None, None)
    monkeypatch.setattr(
        gateway,
        "_service",
        lambda **kwargs: _FailingClassroomService(InsufficientQuota()),
    )

    with pytest.raises(BatchItemRejected):
        await gateway.create(
            _context(),
            _request("quota exhausted"),
            batch_id=f"batch-{'a' * 20}-{'b' * 32}",
            item_id="a",
        )

    monkeypatch.setattr(
        gateway,
        "_service",
        lambda **kwargs: _FailingClassroomService(
            ClassroomPreflightRejected("invalid brief")
        ),
    )
    with pytest.raises(BatchItemRejected):
        await gateway.create(
            _context(),
            _request("invalid brief"),
            batch_id=f"batch-{'a' * 20}-{'b' * 32}",
            item_id="a",
        )

    for invariant_error in (
        InvalidClassroomState("live job binding is invalid"),
        ValueError("post-workflow value error"),
        PermissionError("post-workflow permission error"),
    ):
        monkeypatch.setattr(
            gateway,
            "_service",
            lambda **kwargs: _FailingClassroomService(invariant_error),
        )
        with pytest.raises(type(invariant_error)):
            await gateway.create(
                _context(),
                _request("post-workflow failure"),
                batch_id=f"batch-{'a' * 20}-{'b' * 32}",
                item_id="a",
            )

    for unavailable in (
        DataPlaneBindingUnavailable(),
        SourceSnapshotUnavailable(),
    ):
        monkeypatch.setattr(
            gateway,
            "_service",
            lambda **kwargs: _FailingClassroomService(unavailable),
        )
        with pytest.raises(type(unavailable)):
            await gateway.create(
                _context(),
                _request("dependency unavailable"),
                batch_id=f"batch-{'a' * 20}-{'b' * 32}",
                item_id="a",
            )


@pytest.mark.asyncio
async def test_post_job_invariant_does_not_create_or_bind_a_rejected_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LiveJobThenInvariantService:
        live_jobs = 0

        async def create(self, *args, **kwargs):
            self.live_jobs += 1
            raise InvalidClassroomState("outline job binding is invalid")

    live_service = _LiveJobThenInvariantService()
    gateway = SqlAlchemyBatchClassroomGateway(None, None, None, None, None)
    monkeypatch.setattr(gateway, "_service", lambda **kwargs: live_service)
    repository = _BatchRepository()
    jobs = _Jobs()
    service = BatchService(repository, gateway, jobs)

    with pytest.raises(InvalidClassroomState, match="binding"):
        await service.create(
            _context(),
            (BatchItemInput("a", _request("post-job invariant")),),
            idempotency_key="batch-post-job-invariant",
        )

    assert live_service.live_jobs == 1
    assert jobs.counts == {}
    assert repository.batch is not None
    assert repository.batch.items[0].generation_job_id is None
    assert repository.batch.items[0].status == "queued"


def test_invalid_batch_item_identity_is_an_explicit_request_error() -> None:
    for item_id in ("bad item id", None):
        with pytest.raises(InvalidBatchRequest):
            BatchItemInput(item_id, _request("invalid identity"))


@pytest.mark.asyncio
async def test_batch_request_validation_finishes_before_repository_side_effects() -> None:
    malformed = SimpleNamespace(course_id="course-a", class_id="class-a")
    cases = (
        ((BatchItemInput("a", _request("valid")),), "bad"),
        ((), "batch-request-empty"),
        (
            (
                BatchItemInput("a", _request("valid-a")),
                BatchItemInput("a", _request("valid-b")),
            ),
            "batch-request-duplicate",
        ),
        (
            tuple(
                BatchItemInput(f"item-{index}", _request(f"valid-{index}"))
                for index in range(101)
            ),
            "batch-request-too-many",
        ),
        ((BatchItemInput("a", malformed),), "batch-request-malformed"),
    )

    for items, idempotency_key in cases:
        repository = _BatchRepository()
        service = BatchService(repository, _Classrooms())

        with pytest.raises(InvalidBatchRequest):
            await service.create(
                _context(),
                items,
                idempotency_key=idempotency_key,
            )

        assert repository.batch is None


@pytest.mark.asyncio
async def test_batch_list_uses_bounded_explicit_pagination() -> None:
    repository = _BatchRepository()
    service = BatchService(repository, _Classrooms())

    assert await service.list(_context(), limit=17, offset=4) == ()
    assert repository.list_calls == [(17, 4)]
    assert repository.list_access_scopes[0].tenant_wide is True
    for limit, offset in ((0, 0), (101, 0), (50, -1)):
        with pytest.raises(InvalidBatchRequest):
            await service.list(_context(), limit=limit, offset=offset)


@pytest.mark.asyncio
async def test_batch_list_compiles_current_edit_grants_before_pagination() -> None:
    repository = _BatchRepository()
    service = BatchService(repository, _Classrooms())
    scoped = TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant_a",
        user_id="author-a",
        permissions=(
            permissions_for_roles(
                {"content_author"},
                scope_type="course",
                scope_id="course-a",
                tenant_id="tenant-a",
            )
            | permissions_for_roles(
                {"content_author"},
                scope_type="class",
                scope_id="class-b",
                tenant_id="tenant-a",
            )
        ),
    )

    assert await service.list(scoped, limit=1, offset=0) == ()
    access_scope = repository.list_access_scopes[0]
    assert access_scope is not None
    assert access_scope.tenant_wide is False
    assert access_scope.course_ids == ("course-a",)
    assert access_scope.class_ids == ("class-b",)

    no_scope_repository = _BatchRepository()
    no_scope_service = BatchService(no_scope_repository, _Classrooms())
    assert await no_scope_service.list(_context(roles={"student"})) == ()
    assert no_scope_repository.list_calls == []


@pytest.mark.asyncio
async def test_confirmation_collection_request_errors_are_explicit() -> None:
    service = BatchService(_BatchRepository(), _Classrooms())

    with pytest.raises(InvalidBatchRequest):
        await service.confirm_outlines(_context(), "batch-a", ())
    with pytest.raises(InvalidBatchRequest):
        await service.confirm_outlines(
            _context(),
            "batch-a",
            (("a", 1, "a" * 64), ("a", 2, "b" * 64)),
        )


@pytest.mark.asyncio
async def test_batch_preserves_success_when_one_item_fails() -> None:
    repository = _BatchRepository()
    service = BatchService(repository, _Classrooms())

    batch = await service.create(
        _context(),
        (
            BatchItemInput("a", _request("valid-a")),
            BatchItemInput("b", _request("invalid")),
            BatchItemInput("c", _request("valid-c")),
        ),
        idempotency_key="batch-request-1",
    )

    assert batch.status == "partially_succeeded"
    assert [item.status for item in batch.items] == ["succeeded", "failed", "succeeded"]


@pytest.mark.asyncio
async def test_outline_confirmation_requires_the_seen_revision() -> None:
    from tests.teaching_contract_fixtures import valid_outline_bundle

    repository = _BatchRepository()
    classrooms = _Classrooms()
    service = BatchService(repository, classrooms)
    classroom = _request("valid")
    batch = await service.create(
        _context(),
        (BatchItemInput("a", classroom),),
        idempotency_key="batch-request-2",
    )
    record = classrooms.records["asset-a"]
    record.status = "awaiting_confirmation"
    record.revision = 3
    record.outline = valid_outline_bundle()
    await repository.set_item_status(batch.id, "a", "awaiting_confirmation")

    with pytest.raises(BatchOutlineConflict, match="revision"):
        await service.confirm_outline(
            _context(),
            batch.id,
            "a",
            revision=2,
            outline_sha256="a" * 64,
        )


@pytest.mark.asyncio
async def test_retry_creates_a_job_only_for_the_failed_item() -> None:
    jobs = _Jobs()
    repository = _BatchRepository()
    service = BatchService(repository, _Classrooms(jobs), jobs)
    batch = await service.create(
        _context(),
        (
            BatchItemInput("a", _request("valid-a")),
            BatchItemInput("b", _request("invalid")),
            BatchItemInput("c", _request("valid-c")),
        ),
        idempotency_key="batch-request-3",
    )

    retried = await service.retry_item(_context(), batch.id, "b")

    assert isinstance(retried, BatchRetryResult)
    assert retried.parent_item_id == "b"
    assert retried.item.status == "queued"
    assert jobs.counts == {"a": 1, "b": 2, "c": 1}


@pytest.mark.asyncio
async def test_bulk_confirmation_only_advances_selected_reviewed_outlines() -> None:
    from tests.teaching_contract_fixtures import valid_outline_bundle

    repository = _BatchRepository()
    classrooms = _Classrooms()
    service = BatchService(repository, classrooms)
    batch = await service.create(
        _context(),
        (
            BatchItemInput("a", _request("valid-a")),
            BatchItemInput("b", _request("valid-b")),
        ),
        idempotency_key="batch-request-4",
    )
    outline = valid_outline_bundle()
    for item_id in ("a", "b"):
        record = classrooms.records[f"asset-{item_id}"]
        record.status = "awaiting_confirmation"
        record.revision = 3
        record.outline = outline
        await repository.set_item_status(batch.id, item_id, "awaiting_confirmation")

    updated = await service.confirm_outlines(
        _context(),
        batch.id,
        (("a", 3, canonical_outline_sha256(outline)),),
    )

    assert [item.status for item in updated.items] == ["queued", "awaiting_confirmation"]
    assert classrooms.confirmation_bindings == [
        ("asset-a", 3, canonical_outline_sha256(outline))
    ]


@pytest.mark.asyncio
async def test_single_confirmation_rejects_an_outline_edited_after_review() -> None:
    from tests.teaching_contract_fixtures import valid_outline_bundle

    repository = _BatchRepository()
    classrooms = _Classrooms()
    service = BatchService(repository, classrooms)
    batch = await service.create(
        _context(),
        (BatchItemInput("a", _request("valid-a")),),
        idempotency_key="batch-request-concurrent-single",
    )
    outline = valid_outline_bundle()
    record = classrooms.records["asset-a"]
    record.status = "awaiting_confirmation"
    record.revision = 3
    record.outline = outline
    classrooms.edit_on_confirm.add("asset-a")
    await repository.set_item_status(batch.id, "a", "awaiting_confirmation")

    with pytest.raises(BatchOutlineConflict):
        await service.confirm_outline(
            _context(),
            batch.id,
            "a",
            revision=3,
            outline_sha256=canonical_outline_sha256(outline),
        )

    assert (await repository.get(batch.id)).items[0].status == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_batch_confirmation_recovers_the_same_review_after_content_queue_failure() -> None:
    from tests.teaching_contract_fixtures import valid_outline_bundle

    repository = _BatchRepository()
    classrooms = _Classrooms()
    service = BatchService(repository, classrooms)
    batch = await service.create(
        _context(),
        (BatchItemInput("a", _request("valid-a")),),
        idempotency_key="batch-request-confirm-recovery",
    )
    reviewed = OutlineBundle.model_validate(valid_outline_bundle()).model_copy(
        update={"confirmation_metadata": OutlineConfirmationMetadata(status="draft")}
    )
    reviewed_revision = 3
    reviewed_sha256 = canonical_outline_sha256(reviewed)
    confirmed = reviewed.model_copy(
        update={
            "confirmation_metadata": OutlineConfirmationMetadata(
                status="confirmed",
                confirmed_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
                confirmed_by="author-a",
            )
        }
    )
    record = classrooms.records["asset-a"]
    record.status = "awaiting_confirmation"
    record.lifecycle_state = "generating_content"
    record.revision = reviewed_revision + 1
    record.outline = confirmed
    record.confirmed_outline_sha256 = canonical_outline_sha256(confirmed)
    await repository.set_item_status(batch.id, "a", "awaiting_confirmation")

    recovered = await service.confirm_outline(
        _context(),
        batch.id,
        "a",
        revision=reviewed_revision,
        outline_sha256=reviewed_sha256,
    )

    assert recovered.items[0].status == "queued"
    assert classrooms.confirmation_bindings == [
        ("asset-a", reviewed_revision, reviewed_sha256)
    ]


@pytest.mark.asyncio
async def test_batch_confirmation_recovery_rejects_a_tampered_confirmed_outline() -> None:
    from tests.teaching_contract_fixtures import valid_outline_bundle

    repository = _BatchRepository()
    classrooms = _Classrooms()
    service = BatchService(repository, classrooms)
    batch = await service.create(
        _context(),
        (BatchItemInput("a", _request("valid-a")),),
        idempotency_key="batch-request-confirm-tamper",
    )
    reviewed = OutlineBundle.model_validate(valid_outline_bundle()).model_copy(
        update={"confirmation_metadata": OutlineConfirmationMetadata(status="draft")}
    )
    reviewed_revision = 3
    reviewed_sha256 = canonical_outline_sha256(reviewed)
    confirmed = reviewed.model_copy(
        update={
            "confirmation_metadata": OutlineConfirmationMetadata(
                status="confirmed",
                confirmed_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
                confirmed_by="author-a",
            )
        }
    ).model_copy(update={"title": "Tampered after confirmation"})
    record = classrooms.records["asset-a"]
    record.status = "awaiting_confirmation"
    record.lifecycle_state = "generating_content"
    record.revision = reviewed_revision + 1
    record.outline = confirmed
    record.confirmed_outline_sha256 = canonical_outline_sha256(confirmed)
    await repository.set_item_status(batch.id, "a", "awaiting_confirmation")

    with pytest.raises(BatchOutlineConflict):
        await service.confirm_outline(
            _context(),
            batch.id,
            "a",
            revision=reviewed_revision,
            outline_sha256=reviewed_sha256,
        )


@pytest.mark.asyncio
async def test_bulk_confirmation_rechecks_each_item_at_its_locked_write() -> None:
    from tests.teaching_contract_fixtures import valid_outline_bundle

    repository = _BatchRepository()
    classrooms = _Classrooms()
    service = BatchService(repository, classrooms)
    batch = await service.create(
        _context(),
        (
            BatchItemInput("a", _request("valid-a")),
            BatchItemInput("b", _request("valid-b")),
        ),
        idempotency_key="batch-request-concurrent-bulk",
    )
    outline = valid_outline_bundle()
    digest = canonical_outline_sha256(outline)
    for item_id in ("a", "b"):
        record = classrooms.records[f"asset-{item_id}"]
        record.status = "awaiting_confirmation"
        record.revision = 3
        record.outline = outline
        await repository.set_item_status(batch.id, item_id, "awaiting_confirmation")
    classrooms.edit_on_confirm.add("asset-b")

    with pytest.raises(BatchOutlineConflict):
        await service.confirm_outlines(
            _context(),
            batch.id,
            (("a", 3, digest), ("b", 3, digest)),
        )

    assert [item.status for item in (await repository.get(batch.id)).items] == [
        "queued",
        "awaiting_confirmation",
    ]


@pytest.mark.asyncio
async def test_cancel_only_affects_unstarted_items_and_preserves_success() -> None:
    repository = _BatchRepository()
    jobs = _Jobs()
    classrooms = _Classrooms(jobs)
    service = BatchService(repository, classrooms, jobs)
    batch = await service.create(
        _context(),
        (
            BatchItemInput("a", _request("valid-a")),
            BatchItemInput("b", _request("valid-b")),
            BatchItemInput("c", _request("valid-c")),
        ),
        idempotency_key="batch-request-5",
    )
    await repository.set_item_status(batch.id, "a", "succeeded")
    await repository.set_item_status(batch.id, "b", "queued")
    await repository.bind_item(
        batch.id,
        "c",
        generation_job_id="job-c-running",
        classroom_draft_id="draft-c",
        classroom_asset_id="asset-c",
        status="running",
    )

    canceled = await service.cancel(_context(), batch.id)

    assert [item.status for item in canceled.items] == ["succeeded", "canceled", "running"]


@pytest.mark.asyncio
async def test_production_batch_generation_binds_low_priority_and_batch_id() -> None:
    from tests.teaching_contract_fixtures import valid_generation_request

    class _Repository:
        request = None

        async def create_job_and_reserve(self, request):
            self.request = request

        async def get_job_details(self, tenant_id, job_id):
            return SimpleNamespace(
                job_id=job_id,
                status="quota_reserved",
                result_payload=None,
                phase="outline",
            )

    class _Selector:
        async def resolve(self, tenant_id):
            return SimpleNamespace(
                route_ref="route-a",
                provider_profile_ref="provider-a",
                worker_pool_ref="worker-a",
                queue_ref="queue-a",
            )

    repository = _Repository()
    generation = SqlAlchemyClassroomGeneration(
        repository,
        _Selector(),
        priority="batch",
        batch_id="batch-0123456789abcdef0123456789abcdef0123456789abcdef",
    )
    request = valid_generation_request()
    brief = TeachingBrief.model_validate(request["teaching_brief"])

    await generation.start_outline(
        context=_context(),
        asset_id="asset-a",
        draft_id="draft-a",
        teaching_brief=brief,
        requested_exports=("classroom_zip",),
    )

    assert repository.request.priority == "batch"
    assert repository.request.batch_id == "batch-0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.mark.asyncio
async def test_same_idempotency_key_rejects_changed_classroom_input() -> None:
    service = BatchService(_BatchRepository(), _Classrooms())
    await service.create(
        _context(),
        (BatchItemInput("a", _request("original")),),
        idempotency_key="batch-request-6",
    )

    with pytest.raises(BatchIdempotencyConflict):
        await service.create(
            _context(),
            (BatchItemInput("a", _request("changed")),),
            idempotency_key="batch-request-6",
        )


@pytest.mark.asyncio
async def test_unrelated_same_tenant_member_cannot_list_or_get_batch_ids() -> None:
    class _ResourceScopedClassrooms(_Classrooms):
        async def get(self, context, asset_id):
            if context.user_id != "author-a":
                return None
            return await super().get(context, asset_id)

    repository = _BatchRepository()
    service = BatchService(repository, _ResourceScopedClassrooms())
    batch = await service.create(
        _context(),
        (BatchItemInput("a", _request("valid-a")),),
        idempotency_key="batch-private-1",
    )
    unrelated = _context(user_id="student-a", roles={"student"})

    assert await service.list(unrelated) == ()
    assert await service.get(unrelated, batch.id) is None


class _PermissionCheckingClassrooms(_Classrooms):
    async def get(self, context, asset_id):
        resource = ResourceScope(
            tenant_id=context.tenant_id,
            course_id="course-a",
            class_id="class-a",
        )
        if not any(
            grant.allows_resource("classroom.edit", resource)
            for grant in context.permissions
        ):
            return None
        return await super().get(context, asset_id)


@pytest.mark.asyncio
async def test_batch_actor_loses_visibility_and_mutation_after_edit_revocation() -> None:
    jobs = _Jobs()
    repository = _BatchRepository()
    service = BatchService(repository, _PermissionCheckingClassrooms(jobs), jobs)
    batch = await service.create(
        _context(),
        (BatchItemInput("a", _request("valid-a")),),
        idempotency_key="batch-owner-revoked-asset",
    )
    await repository.set_item_status(batch.id, "a", "failed")
    revoked = _context(user_id="author-a", roles={"student"})
    counts_before = dict(jobs.counts)

    assert await service.list(revoked) == ()
    assert await service.get(revoked, batch.id) is None
    with pytest.raises(BatchNotFound):
        await service.cancel(revoked, batch.id)
    with pytest.raises(BatchNotFound):
        await service.retry_item(revoked, batch.id, "a")

    assert jobs.counts == counts_before
    assert jobs.cancel_calls == []


class _PreflightRejectingClassrooms(_Classrooms):
    def __init__(self, jobs: _Jobs) -> None:
        super().__init__(jobs)
        self.rejected_items = {"b"}

    async def create(self, context, request, *, batch_id, item_id, retry_of_job_id=None):
        if item_id in self.rejected_items:
            raise BatchItemRejected("source is not ready")
        return await super().create(
            context,
            request,
            batch_id=batch_id,
            item_id=item_id,
            retry_of_job_id=retry_of_job_id,
        )


@pytest.mark.asyncio
async def test_batch_authorization_uses_projected_scopes_without_gateway_reads() -> None:
    class _NoAuthorizationReads(_PreflightRejectingClassrooms):
        def __init__(self, jobs: _Jobs) -> None:
            super().__init__(jobs)
            self.get_calls = 0

        async def get(self, context, asset_id):
            self.get_calls += 1
            raise AssertionError("batch authorization must not read classrooms")

    jobs = _Jobs()
    repository = _BatchRepository()
    classrooms = _NoAuthorizationReads(jobs)
    service = BatchService(repository, classrooms, jobs)
    batch = await service.create(
        _context(),
        (
            BatchItemInput("a", _request("valid-a")),
            BatchItemInput("b", _request("valid-b")),
        ),
        idempotency_key="batch-projected-scope",
    )

    assert await service.get(_context(), batch.id) == batch
    assert await service.list(_context()) == (batch,)
    assert await service.get(_context(roles={"student"}), batch.id) is None
    assert classrooms.get_calls == 0


@pytest.mark.asyncio
async def test_rejected_batch_actor_revocation_is_opaque_and_blocks_job_mutation() -> None:
    jobs = _Jobs()
    repository = _BatchRepository()
    classrooms = _PreflightRejectingClassrooms(jobs)
    service = BatchService(repository, classrooms, jobs)
    batch = await service.create(
        _context(),
        (BatchItemInput("b", _request("valid-b")),),
        idempotency_key="batch-owner-revoked-rejected",
    )
    revoked = _context(user_id="author-a", roles={"student"})
    counts_before = dict(jobs.counts)

    assert await service.list(revoked) == ()
    assert await service.get(revoked, batch.id) is None
    with pytest.raises(BatchNotFound):
        await service.cancel(revoked, batch.id)
    with pytest.raises(BatchNotFound):
        await service.retry_item(revoked, batch.id, "b")

    assert jobs.counts == counts_before
    assert jobs.cancel_calls == []


@pytest.mark.asyncio
async def test_rejected_item_gets_durable_job_then_source_fix_retries_only_it() -> None:
    jobs = _Jobs()
    repository = _BatchRepository()
    classrooms = _PreflightRejectingClassrooms(jobs)
    service = BatchService(repository, classrooms, jobs)
    batch = await service.create(
        _context(),
        (
            BatchItemInput("a", _request("valid-a")),
            BatchItemInput("b", _request("valid-b")),
        ),
        idempotency_key="batch-rejected-fix-1",
    )
    rejected = batch.items[1]

    assert rejected.status == "failed"
    assert rejected.generation_job_id == "job-b-rejected-1"
    assert rejected.classroom_draft_id is None

    classrooms.rejected_items.clear()
    retried = await service.retry_item(_context(), batch.id, "b")

    assert retried.item.status == "queued"
    assert retried.item.generation_job_id == "job-b-real-retry-2"
    assert jobs.lineage[retried.item.generation_job_id] == rejected.generation_job_id
    assert jobs.counts == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_rejected_item_retry_records_new_failed_lineage_if_still_rejected() -> None:
    jobs = _Jobs()
    repository = _BatchRepository()
    classrooms = _PreflightRejectingClassrooms(jobs)
    service = BatchService(repository, classrooms, jobs)
    batch = await service.create(
        _context(),
        (BatchItemInput("b", _request("valid-b")),),
        idempotency_key="batch-rejected-still-1",
    )
    first_job_id = batch.items[0].generation_job_id

    retried = await service.retry_item(_context(), batch.id, "b")

    assert retried.item.status == "failed"
    assert retried.item.generation_job_id == "job-b-rejected-2"
    assert jobs.lineage[retried.item.generation_job_id] == first_job_id
    assert jobs.counts == {"b": 2}


@pytest.mark.asyncio
async def test_replaying_create_does_not_auto_retry_terminal_rejected_item() -> None:
    jobs = _Jobs()
    repository = _BatchRepository()
    classrooms = _PreflightRejectingClassrooms(jobs)
    service = BatchService(repository, classrooms, jobs)
    item = BatchItemInput("b", _request("valid-b"))
    first = await service.create(
        _context(),
        (item,),
        idempotency_key="batch-rejected-replay-1",
    )
    classrooms.rejected_items.clear()

    replayed = await service.create(
        _context(),
        (item,),
        idempotency_key="batch-rejected-replay-1",
    )

    assert replayed.items[0].status == "failed"
    assert replayed.items[0].generation_job_id == first.items[0].generation_job_id
    assert jobs.counts == {"b": 1}
