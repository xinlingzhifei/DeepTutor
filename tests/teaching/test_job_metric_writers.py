from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from deeptutor.teaching.models.jobs import InvalidJobTransition, QuotaLedger
from deeptutor.teaching.repositories import jobs as jobs_module
from deeptutor.teaching.repositories.jobs import (
    GenerationJobRequest,
    MaterializedArtifactInput,
    SqlAlchemyGenerationJobRepository,
    require_repository_transition,
)
from deeptutor.teaching.scheduler import ClaimedGenerationJob

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _SessionOwner(AbstractAsyncContextManager["_Session"]):
    def __init__(self, session: "_Session") -> None:
        self._session = session

    async def __aenter__(self) -> "_Session":
        return self._session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _ScalarRows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(
        self,
        *,
        rowcount: int = 0,
        scalar=None,
        mapping=None,
    ) -> None:
        self.rowcount = rowcount
        self._scalar = scalar
        self._mapping = mapping

    def scalar_one_or_none(self):
        return self._scalar

    def mappings(self):
        return self

    def one_or_none(self):
        return self._mapping


class _Session:
    def __init__(
        self,
        *,
        scalar_results=(),
        scalars_results=(),
        execute_results=(),
    ) -> None:
        self.events: list[object] = []
        self._scalar_results = iter(scalar_results)
        self._scalars_results = iter(scalars_results)
        self._execute_results = iter(execute_results)

    def begin(self) -> _Transaction:
        self.events.append("begin")
        return _Transaction()

    def add(self, value) -> None:
        self.events.append(("add", value))

    async def delete(self, value) -> None:
        self.events.append(("delete", value))

    async def flush(self) -> None:
        self.events.append("flush")

    async def scalar(self, statement):
        self.events.append(("scalar", statement))
        return next(self._scalar_results)

    async def scalars(self, statement) -> _ScalarRows:
        self.events.append(("scalars", statement))
        return _ScalarRows(next(self._scalars_results))

    async def execute(self, statement, parameters=None) -> _Result:
        self.events.append(("execute", statement, parameters))
        return next(self._execute_results, _Result())


def _factory(session: _Session):
    return lambda: _SessionOwner(session)


def _repository(session: _Session) -> SqlAlchemyGenerationJobRepository:
    repository = SqlAlchemyGenerationJobRepository()
    repository._session_factory = lambda _tenant_id: _factory(session)  # type: ignore[method-assign]
    return repository


def _request(
    *,
    job_id: str = "job-a",
    phase: str = "outline",
    priority: str = "teacher",
    batch_id: str | None = None,
    public_request_sha256: str | None = None,
) -> GenerationJobRequest:
    payload = json.dumps(
        {
            "dataPlaneRouteId": "shared-primary",
            "idempotencyKey": f"idem-{job_id}",
            "jobId": job_id,
            "phase": phase,
            "requestId": f"request-{job_id}",
            "tenantId": "tenant-a",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return GenerationJobRequest(
        tenant_id="tenant-a",
        job_id=job_id,
        job_kind="generation",
        phase=phase,
        export_format=None,
        priority=priority,
        quota_units=7,
        actor_id="teacher-a",
        owner_id="teacher-a",
        visibility="private",
        request_id=f"request-{job_id}",
        idempotency_key=f"idem-{job_id}",
        request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        request_payload=payload,
        batch_id=batch_id,
        public_request_sha256=public_request_sha256,
    )


def _claim(*, phase: str = "content", attempt_count: int = 2) -> ClaimedGenerationJob:
    return ClaimedGenerationJob(
        tenant_id="tenant-a",
        job_id="job-a",
        job_kind="generation" if phase != "export" else "export",
        phase=phase,
        status=(
            "generating_outline"
            if phase == "outline"
            else "generating_content"
            if phase == "content"
            else "exporting"
        ),
        slot_pool="generation",
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        attempt_count=attempt_count,
        lease_owner="worker-a",
        lease_token="lease-a",
        lease_expires_at=NOW + timedelta(seconds=60),
        global_slot_id=1,
        tenant_slot_id=2,
    )


@pytest.mark.asyncio
async def test_claim_fence_reads_database_wall_clock_after_locking_all_rows() -> None:
    job, queue, slots = _leased_state()

    class Session:
        def __init__(self) -> None:
            self.sql: list[str] = []

        async def scalar(self, statement):
            sql = str(statement).lower()
            self.sql.append(sql)
            if "generation_jobs" in sql:
                return job
            if "generation_queue" in sql:
                return queue
            if "clock_timestamp" in sql:
                return NOW
            raise AssertionError(f"unexpected scalar statement: {sql}")

        async def scalars(self, statement):
            self.sql.append(str(statement).lower())
            return _ScalarRows(slots)

    session = Session()

    _job, _queue, _slots, now = await SqlAlchemyGenerationJobRepository._lock_claim(
        session,  # type: ignore[arg-type]
        _claim(),
    )

    assert now == NOW
    assert "generation_jobs" in session.sql[0]
    assert "generation_queue" in session.sql[1]
    assert "generation_slots" in session.sql[2]
    assert "clock_timestamp" in session.sql[3]


def _leased_state(
    *,
    phase: str = "content",
    attempt_count: int = 2,
    status: str | None = None,
    cancel_requested: bool = False,
):
    job = SimpleNamespace(
        id="job-a",
        tenant_id="tenant-a",
        job_kind="generation" if phase != "export" else "export",
        phase=phase,
        status=status
        or (
            "generating_outline"
            if phase == "outline"
            else "generating_content"
            if phase == "content"
            else "exporting"
        ),
        quota_units=7,
        attempt_count=attempt_count,
        max_attempts=5,
        cancel_requested=cancel_requested,
        progress_percent=10,
        error_category=None,
        error_code=None,
        completed_at=None,
        canceled_at=None,
        waiting_reason=None,
        updated_at=NOW,
        lease_owner="worker-a",
        lease_token="lease-a",
        lease_expires_at=NOW + timedelta(seconds=60),
        heartbeat_at=NOW,
        classroom_draft_id=None,
        owner_id="teacher-a",
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        result_ref=None,
        artifact_manifest_ref=None,
    )
    queue = SimpleNamespace(
        tenant_id="tenant-a",
        job_id="job-a",
        job_kind=job.job_kind,
        phase=phase,
        status="claimed",
        claimed_at=NOW - timedelta(seconds=4.25),
        available_at=NOW - timedelta(seconds=10),
        lease_owner="worker-a",
        lease_token="lease-a",
        lease_expires_at=NOW + timedelta(seconds=60),
        heartbeat_at=NOW,
    )
    slots = (
        SimpleNamespace(
            id=1,
            claimed_tenant_id="tenant-a",
            claimed_job_id="job-a",
            lease_owner="worker-a",
            lease_token="lease-a",
            lease_expires_at=NOW + timedelta(seconds=60),
            heartbeat_at=NOW,
        ),
        SimpleNamespace(
            id=2,
            claimed_tenant_id="tenant-a",
            claimed_job_id="job-a",
            lease_owner="worker-a",
            lease_token="lease-a",
            lease_expires_at=NOW + timedelta(seconds=60),
            heartbeat_at=NOW,
        ),
    )
    return job, queue, slots


def _install_claim(repository, job, queue, slots) -> None:
    async def lock_claim(_session, _claim, *, require_unexpired=True):
        assert require_unexpired
        return job, queue, slots, NOW

    repository._lock_claim = lock_claim  # type: ignore[method-assign]


@pytest.fixture
def metric_recorder(monkeypatch):
    async def counter(session, **kwargs) -> None:
        session.events.append(("counter", kwargs))

    async def histogram(session, **kwargs) -> None:
        session.events.append(("histogram", kwargs))

    monkeypatch.setattr(jobs_module, "increment_counter_rollup", counter, raising=False)
    monkeypatch.setattr(jobs_module, "observe_histogram_rollup", histogram, raising=False)


def _metric_events(session: _Session) -> list[tuple[str, dict[str, object]]]:
    return [
        event
        for event in session.events
        if isinstance(event, tuple) and event[0] in {"counter", "histogram"}
    ]


def _assert_metrics_after_last_flush(session: _Session) -> None:
    flush_index = max(index for index, event in enumerate(session.events) if event == "flush")
    assert all(session.events.index(event) > flush_index for event in _metric_events(session))


@pytest.mark.parametrize(
    ("job_kind", "current_status"),
    (
        ("generation", "quota_reserved"),
        ("generation", "awaiting_confirmation"),
        ("export", "quota_reserved"),
    ),
)
def test_repository_transition_rejects_every_generic_queued_target(
    job_kind: str,
    current_status: str,
) -> None:
    with pytest.raises(InvalidJobTransition, match="queued transitions require lifecycle API"):
        require_repository_transition(job_kind, current_status, "queued")


@pytest.mark.asyncio
async def test_transition_rejects_queued_target_before_opening_a_session() -> None:
    repository = SqlAlchemyGenerationJobRepository()

    def forbidden_factory(_tenant_id):
        raise AssertionError("queued target must fail before opening a session")

    repository._session_factory = forbidden_factory  # type: ignore[method-assign]

    with pytest.raises(InvalidJobTransition, match="queued transitions require lifecycle API"):
        await repository.transition(
            "tenant-a",
            "job-a",
            expected_status="quota_reserved",
            target_status="queued",
        )


@pytest.mark.asyncio
async def test_new_reservation_records_quota_only_after_unique_ledger_flush(
    monkeypatch,
    metric_recorder,
) -> None:
    request = _request()
    first_session = _Session(
        scalar_results=(None,),
        execute_results=(_Result(), _Result(rowcount=1)),
    )
    repository = _repository(first_session)
    monkeypatch.setattr(jobs_module, "lock_active_job_binding", _true_binding)
    monkeypatch.setattr(jobs_module, "_database_now", _fixed_now)

    async def quota_balance(_session, _tenant_id):
        return 100

    repository._quota_balance = quota_balance  # type: ignore[method-assign]

    record = await repository.create_job_and_reserve(request)

    assert record.status == "quota_reserved"
    metrics = _metric_events(first_session)
    assert metrics == [
        (
            "counter",
            {
                "metric": "quota_units_total",
                "category": "reserved",
                "fact_key": jobs_module._reservation_id(request.job_id),
                "amount": request.quota_units,
            },
        )
    ]
    reserve_add = next(
        event
        for event in first_session.events
        if isinstance(event, tuple) and event[0] == "add" and isinstance(event[1], QuotaLedger)
    )
    assert reserve_add[1].entry_type == "reserve"
    _assert_metrics_after_last_flush(first_session)

    inserted_job = next(
        event[1]
        for event in first_session.events
        if isinstance(event, tuple)
        and event[0] == "add"
        and getattr(event[1], "id", None) == request.job_id
    )
    idempotent_session = _Session(scalar_results=(inserted_job,), execute_results=(_Result(),))
    repository._session_factory = lambda _tenant_id: _factory(idempotent_session)  # type: ignore[method-assign]

    idempotent = await repository.create_job_and_reserve(request)

    assert idempotent.status == "created"
    assert _metric_events(idempotent_session) == []


async def _true_binding(*_args, **_kwargs) -> bool:
    return True


async def _fixed_now(_session) -> datetime:
    return NOW


@pytest.mark.asyncio
async def test_rejected_batch_job_records_failed_only_for_first_insert(
    monkeypatch,
    metric_recorder,
) -> None:
    request = _request(
        job_id="batch-job",
        priority="batch",
        batch_id="batch-a",
        public_request_sha256="a" * 64,
    )
    first_session = _Session(scalar_results=(None,), execute_results=(_Result(),))
    repository = _repository(first_session)
    monkeypatch.setattr(jobs_module, "_database_now", _fixed_now)

    async def lock_tenant(_session, _tenant_id) -> None:
        return None

    repository._lock_active_tenant = lock_tenant  # type: ignore[method-assign]

    first = await repository.create_rejected_batch_job(request)
    inserted_job = next(
        event[1]
        for event in first_session.events
        if isinstance(event, tuple)
        and event[0] == "add"
        and getattr(event[1], "id", None) == request.job_id
    )

    second_session = _Session(
        scalar_results=(inserted_job,),
        execute_results=(_Result(),),
    )
    repository._session_factory = lambda _tenant_id: _factory(second_session)  # type: ignore[method-assign]
    second = await repository.create_rejected_batch_job(request)

    assert first.status == second.status == "failed"
    assert _metric_events(first_session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "failed",
                "fact_key": "tenant-a/batch-job/outline/0",
                "amount": 1,
            },
        )
    ]
    assert _metric_events(second_session) == []
    _assert_metrics_after_last_flush(first_session)


@pytest.mark.asyncio
async def test_content_requeue_records_one_queued_transition_after_outbox_flush(
    monkeypatch,
    metric_recorder,
) -> None:
    job = SimpleNamespace(
        id="job-a",
        tenant_id="tenant-a",
        job_kind="generation",
        phase="outline",
        status="awaiting_confirmation",
        request_id="request-job-a",
        idempotency_key="idem-job-a",
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        priority=300,
        attempt_count=1,
        progress_percent=50,
    )
    request = _request(phase="content")
    session = _Session(scalar_results=(job, None, None))
    repository = _repository(session)
    monkeypatch.setattr(jobs_module, "lock_active_job_binding", _true_binding)
    monkeypatch.setattr(jobs_module, "_database_now", _fixed_now)

    assert await repository.requeue_confirmed_content(
        "tenant-a",
        "job-a",
        request_payload=request.request_payload,
        request_sha256=request.request_sha256,
    )

    assert _metric_events(session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "queued",
                "fact_key": jobs_module._event_id("tenant-a", "job-a", "content"),
                "amount": 1,
            },
        )
    ]
    _assert_metrics_after_last_flush(session)


@pytest.mark.asyncio
async def test_outline_completion_records_only_claimed_stage_duration(
    metric_recorder,
) -> None:
    session = _Session()
    repository = _repository(session)
    job, queue, slots = _leased_state(phase="outline", attempt_count=1)
    _install_claim(repository, job, queue, slots)

    await repository.complete_outline(_claim(phase="outline", attempt_count=1), result_payload="{}")

    assert _metric_events(session) == [
        (
            "histogram",
            {
                "metric": "generation_stage_seconds",
                "category": "outline",
                "fact_key": "tenant-a/job-a/outline/1",
                "seconds": 4.25,
            },
        )
    ]
    _assert_metrics_after_last_flush(session)


@pytest.mark.parametrize(
    ("error_category", "reason"),
    (
        ("connect_timeout", "timeout"),
        ("read_timeout", "timeout"),
        ("provider_429", "rate_limited"),
        ("provider_5xx", "unavailable"),
        ("engine_unavailable", "unavailable"),
        ("worker_lost", "lease_lost"),
    ),
)
@pytest.mark.asyncio
async def test_retry_claim_records_queued_retry_reason_and_stage_after_fence(
    metric_recorder,
    error_category: str,
    reason: str,
) -> None:
    session = _Session()
    repository = _repository(session)
    job, queue, slots = _leased_state(attempt_count=2)
    _install_claim(repository, job, queue, slots)

    assert await repository.retry_claim(
        _claim(attempt_count=2),
        error_category=error_category,
        error_code="stable-code",
        delay_seconds=3,
    )

    assert _metric_events(session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "queued",
                "fact_key": "tenant-a/job-a/content/2",
                "amount": 1,
            },
        ),
        (
            "counter",
            {
                "metric": "generation_retries_total",
                "category": reason,
                "fact_key": "tenant-a/job-a/content/2",
                "amount": 1,
            },
        ),
        (
            "histogram",
            {
                "metric": "generation_stage_seconds",
                "category": "content",
                "fact_key": "tenant-a/job-a/content/2",
                "seconds": 4.25,
            },
        ),
    ]
    _assert_metrics_after_last_flush(session)


@pytest.mark.asyncio
async def test_retry_claim_rejects_unmapped_retryable_category_before_transaction() -> None:
    repository = SqlAlchemyGenerationJobRepository()

    def forbidden_factory(_tenant_id):
        raise AssertionError("invalid retry category must fail before opening a session")

    repository._session_factory = forbidden_factory  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="retry metric category is invalid"):
        await repository.retry_claim(
            _claim(),
            error_category="new_provider_error",
            error_code="private-code",
            delay_seconds=1,
        )


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    (("fail", "failed"), ("cancel", "canceled"), ("retry_exhausted", "failed")),
)
@pytest.mark.asyncio
async def test_terminal_claim_paths_record_status_stage_and_released_quota(
    metric_recorder,
    operation: str,
    expected_status: str,
) -> None:
    session = _Session()
    repository = _repository(session)
    job, queue, slots = _leased_state(attempt_count=2)
    if operation == "retry_exhausted":
        job.max_attempts = 2
    _install_claim(repository, job, queue, slots)
    claim = _claim(attempt_count=2)

    if operation == "fail":
        await repository.fail_claim(
            claim,
            error_category="contract_invalid",
            error_code="invalid-output",
        )
    elif operation == "cancel":
        await repository.cancel_claim(claim)
    else:
        assert not await repository.retry_claim(
            claim,
            error_category="read_timeout",
            error_code="timeout",
            delay_seconds=1,
        )

    terminal_fact = "tenant-a/job-a/content/2"
    release_id = jobs_module._quota_event_id("release", "job-a")
    assert job.status == expected_status
    assert _metric_events(session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": expected_status,
                "fact_key": terminal_fact,
                "amount": 1,
            },
        ),
        (
            "histogram",
            {
                "metric": "generation_stage_seconds",
                "category": "content",
                "fact_key": terminal_fact,
                "seconds": 4.25,
            },
        ),
        (
            "counter",
            {
                "metric": "quota_units_total",
                "category": "released",
                "fact_key": release_id,
                "amount": 7,
            },
        ),
    ]
    _assert_metrics_after_last_flush(session)


@pytest.mark.asyncio
async def test_failed_claim_records_artifact_validation_after_terminal_fence(
    metric_recorder,
) -> None:
    session = _Session()
    repository = _repository(session)
    job, queue, slots = _leased_state(attempt_count=2)
    _install_claim(repository, job, queue, slots)

    await repository.fail_claim(
        _claim(attempt_count=2),
        error_category="contract_invalid",
        error_code="hash_invalid",
        artifact_validation_reason="hash_mismatch",
    )

    assert _metric_events(session)[-1] == (
        "counter",
        {
            "metric": "artifact_validation_failures_total",
            "category": "hash_mismatch",
            "fact_key": "tenant-a/job-a/content/2",
            "amount": 1,
        },
    )
    _assert_metrics_after_last_flush(session)


@pytest.mark.asyncio
async def test_artifact_validation_does_not_count_when_cancel_wins_terminal_race(
    metric_recorder,
) -> None:
    session = _Session()
    repository = _repository(session)
    job, queue, slots = _leased_state(attempt_count=2, cancel_requested=True)
    _install_claim(repository, job, queue, slots)

    await repository.fail_claim(
        _claim(attempt_count=2),
        error_category="contract_invalid",
        error_code="hash_invalid",
        artifact_validation_reason="hash_mismatch",
    )

    assert job.status == "canceled"
    assert not any(
        event[0] == "counter" and event[1]["metric"] == "artifact_validation_failures_total"
        for event in _metric_events(session)
    )


@pytest.mark.asyncio
async def test_failed_claim_rejects_unknown_artifact_reason_before_transaction() -> None:
    repository = SqlAlchemyGenerationJobRepository()

    def forbidden_factory(_tenant_id):
        raise AssertionError("invalid validation reason must fail before opening a session")

    repository._session_factory = forbidden_factory  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="artifact validation metric reason is invalid"):
        await repository.fail_claim(
            _claim(),
            error_category="contract_invalid",
            error_code="artifact_invalid",
            artifact_validation_reason="private_reason",
        )


@pytest.mark.asyncio
async def test_finalize_export_records_completed_stage_and_consumed_quota(
    metric_recorder,
) -> None:
    session = _Session()
    repository = _repository(session)
    job, queue, slots = _leased_state(
        phase="export",
        attempt_count=1,
        status="materializing",
    )
    job.export_format = "pptx"
    state = SimpleNamespace(
        status="object_committed",
        manifest_sha256="m" * 64,
        finalized_at=None,
        updated_at=NOW,
    )
    exported = SimpleNamespace(
        status="quota_reserved",
        input_document_sha256="d" * 64,
        input_media_manifest_sha256="e" * 64,
        export_format="pptx",
        input_manifest_object_key="input/manifest.json",
        input_manifest_sha256="f" * 64,
        classroom_version_id="version-a",
        relative_name=None,
        object_key=None,
        sha256=None,
        size_bytes=None,
        mime_type=None,
        updated_at=NOW,
    )
    session._scalar_results = iter((state, exported))
    _install_claim(repository, job, queue, slots)
    artifact = MaterializedArtifactInput(
        relative_name="lesson.pptx",
        object_key="exports/job-a/lesson.pptx",
        sha256="a" * 64,
        size_bytes=20,
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        artifact_kind="export",
    )

    await repository.finalize_export(
        _claim(phase="export", attempt_count=1),
        input_document_sha256="d" * 64,
        input_media_manifest_sha256="e" * 64,
        manifest_sha256="m" * 64,
        artifact=artifact,
    )

    fact_key = "tenant-a/job-a/export/1"
    settle_id = jobs_module._quota_event_id("settle", "job-a")
    assert _metric_events(session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "completed",
                "fact_key": fact_key,
                "amount": 1,
            },
        ),
        (
            "histogram",
            {
                "metric": "generation_stage_seconds",
                "category": "export",
                "fact_key": fact_key,
                "seconds": 4.25,
            },
        ),
        (
            "counter",
            {
                "metric": "quota_units_total",
                "category": "consumed",
                "fact_key": settle_id,
                "amount": 7,
            },
        ),
    ]
    _assert_metrics_after_last_flush(session)


@pytest.mark.asyncio
async def test_queued_cancel_locks_queue_before_job_to_match_scheduler_claim_order(
    monkeypatch,
    metric_recorder,
) -> None:
    job = SimpleNamespace(
        id="job-a",
        tenant_id="tenant-a",
        job_kind="generation",
        phase="content",
        status="queued",
        quota_units=7,
        attempt_count=1,
        cancel_requested=False,
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        updated_at=NOW,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        heartbeat_at=None,
    )
    queue = SimpleNamespace(status="queued", claimed_at=None)
    session = _Session(scalars_results=([],))

    async def scalar_by_table(statement):
        session.events.append(("scalar", statement))
        sql = str(statement)
        if "generation_queue" in sql:
            return queue
        if "generation_jobs" in sql:
            return job
        raise AssertionError(f"unexpected scalar statement: {sql}")

    session.scalar = scalar_by_table  # type: ignore[method-assign]
    repository = _repository(session)
    monkeypatch.setattr(jobs_module, "_database_now", _fixed_now)

    cancellation = await repository.request_cancel("tenant-a", "job-a")

    lock_sql = [
        str(event[1])
        for event in session.events
        if isinstance(event, tuple) and event[0] == "scalar"
    ]
    assert cancellation is not None and not cancellation.running
    assert "generation_queue" in lock_sql[0]
    assert "generation_jobs" in lock_sql[1]


@pytest.mark.asyncio
async def test_running_cancel_does_not_lock_claimed_queue_before_job(
    monkeypatch,
    metric_recorder,
) -> None:
    job, claimed_queue, _ = _leased_state()
    session = _Session(scalars_results=([],))

    async def scalar_by_table(statement):
        session.events.append(("scalar", statement))
        sql = str(statement)
        if "generation_queue" in sql:
            return None if "generation_queue.status =" in sql else claimed_queue
        if "generation_jobs" in sql:
            return job
        raise AssertionError(f"unexpected scalar statement: {sql}")

    session.scalar = scalar_by_table  # type: ignore[method-assign]
    repository = _repository(session)
    monkeypatch.setattr(jobs_module, "_database_now", _fixed_now)

    cancellation = await repository.request_cancel("tenant-a", "job-a")

    queue_lock_sql = next(
        str(event[1])
        for event in session.events
        if isinstance(event, tuple) and event[0] == "scalar" and "generation_queue" in str(event[1])
    )
    assert cancellation is not None and cancellation.running
    assert "generation_queue.status =" in queue_lock_sql


@pytest.mark.asyncio
async def test_unstarted_cancellation_records_status_and_release_without_stage(
    monkeypatch,
    metric_recorder,
) -> None:
    job = SimpleNamespace(
        id="job-a",
        tenant_id="tenant-a",
        job_kind="generation",
        phase="content",
        status="queued",
        quota_units=7,
        attempt_count=1,
        cancel_requested=False,
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        updated_at=NOW,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        heartbeat_at=None,
    )
    queue = SimpleNamespace(status="queued", claimed_at=None)
    message = SimpleNamespace(delivered_at=None)
    session = _Session(
        scalar_results=(queue, job),
        scalars_results=([message],),
    )
    repository = _repository(session)

    async def ordered_now(current_session) -> datetime:
        current_session.events.append("clock")
        return NOW

    monkeypatch.setattr(jobs_module, "_database_now", ordered_now)

    cancellation = await repository.request_cancel("tenant-a", "job-a")

    assert cancellation is not None and not cancellation.running
    assert session.events.index("clock") > max(
        index
        for index, event in enumerate(session.events)
        if isinstance(event, tuple) and event[0] in {"scalar", "scalars"}
    )
    assert _metric_events(session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "canceled",
                "fact_key": "tenant-a/job-a/content/1",
                "amount": 1,
            },
        ),
        (
            "counter",
            {
                "metric": "quota_units_total",
                "category": "released",
                "fact_key": jobs_module._quota_event_id("release", "job-a"),
                "amount": 7,
            },
        ),
    ]
    _assert_metrics_after_last_flush(session)


@pytest.mark.asyncio
async def test_running_cancel_request_waits_for_terminal_fence_before_metrics(
    monkeypatch,
    metric_recorder,
) -> None:
    job, queue, _ = _leased_state()
    session = _Session(scalar_results=(None, job), scalars_results=([],))
    repository = _repository(session)

    async def ordered_now(current_session) -> datetime:
        current_session.events.append("clock")
        return NOW

    monkeypatch.setattr(jobs_module, "_database_now", ordered_now)

    cancellation = await repository.request_cancel("tenant-a", "job-a")

    assert cancellation is not None and cancellation.running
    assert session.events.index("clock") > max(
        index
        for index, event in enumerate(session.events)
        if isinstance(event, tuple) and event[0] in {"scalar", "scalars"}
    )
    assert _metric_events(session) == []


@pytest.mark.asyncio
async def test_requested_cancellation_reads_database_clock_after_all_row_locks(
    monkeypatch,
    metric_recorder,
) -> None:
    job, queue, slots = _leased_state(cancel_requested=True)
    session = _Session(
        scalar_results=(job, queue),
        scalars_results=(list(slots),),
    )
    repository = _repository(session)

    async def ordered_now(current_session) -> datetime:
        current_session.events.append("clock")
        return NOW

    monkeypatch.setattr(jobs_module, "_database_now", ordered_now)

    assert await repository.finish_requested_cancellation("tenant-a", "job-a")

    clock_index = session.events.index("clock")
    lock_indexes = [
        index
        for index, event in enumerate(session.events)
        if isinstance(event, tuple) and event[0] in {"scalar", "scalars"}
    ]
    assert len(lock_indexes) == 3
    assert clock_index > max(lock_indexes)


@pytest.mark.asyncio
async def test_requested_running_cancellation_records_status_stage_and_release(
    monkeypatch,
    metric_recorder,
) -> None:
    job, queue, slots = _leased_state(cancel_requested=True)
    session = _Session(
        scalar_results=(job, queue),
        scalars_results=(list(slots),),
    )
    repository = _repository(session)
    monkeypatch.setattr(jobs_module, "_database_now", _fixed_now)

    assert await repository.finish_requested_cancellation("tenant-a", "job-a")

    fact_key = "tenant-a/job-a/content/2"
    assert _metric_events(session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "canceled",
                "fact_key": fact_key,
                "amount": 1,
            },
        ),
        (
            "histogram",
            {
                "metric": "generation_stage_seconds",
                "category": "content",
                "fact_key": fact_key,
                "seconds": 4.25,
            },
        ),
        (
            "counter",
            {
                "metric": "quota_units_total",
                "category": "released",
                "fact_key": jobs_module._quota_event_id("release", "job-a"),
                "amount": 7,
            },
        ),
    ]
    _assert_metrics_after_last_flush(session)


@pytest.mark.asyncio
async def test_finalize_generation_records_completed_stage_and_consumed_quota(
    monkeypatch,
    metric_recorder,
) -> None:
    job, queue, slots = _leased_state(status="materializing")
    state = SimpleNamespace(
        status="object_committed",
        manifest_sha256="m" * 64,
        classroom_id="classroom-a",
        version_number=1,
        finalized_at=None,
        updated_at=NOW,
    )
    session = _Session(scalar_results=(state,))
    repository = _repository(session)
    _install_claim(repository, job, queue, slots)

    async def lock_asset(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(jobs_module, "_lock_or_create_classroom_asset", lock_asset)
    monkeypatch.setattr(
        jobs_module,
        "_validate_generation_document_payload",
        lambda *_args, **_kwargs: SimpleNamespace(classroom_id="classroom-a"),
    )
    document_payload = "{}"
    artifact = MaterializedArtifactInput(
        relative_name="classroom.json",
        object_key="classrooms/classroom-a/classroom.json",
        sha256="d" * 64,
        size_bytes=len(document_payload.encode()),
        mime_type="application/json",
        artifact_kind="dsl_json",
    )

    await repository.finalize_generation(
        _claim(attempt_count=2),
        classroom_version_id="version-a",
        document_payload=document_payload,
        document_sha256="d" * 64,
        media_manifest_sha256="e" * 64,
        manifest_sha256="m" * 64,
        artifacts=(artifact,),
    )

    fact_key = "tenant-a/job-a/content/2"
    assert _metric_events(session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "completed",
                "fact_key": fact_key,
                "amount": 1,
            },
        ),
        (
            "histogram",
            {
                "metric": "generation_stage_seconds",
                "category": "content",
                "fact_key": fact_key,
                "seconds": 4.25,
            },
        ),
        (
            "counter",
            {
                "metric": "quota_units_total",
                "category": "consumed",
                "fact_key": jobs_module._quota_event_id("settle", "job-a"),
                "amount": 7,
            },
        ),
    ]
    _assert_metrics_after_last_flush(session)


def _install_reaper_session(monkeypatch, repository, session: _Session) -> None:
    repository._engine = lambda: object()  # type: ignore[method-assign]
    monkeypatch.setattr(
        jobs_module,
        "async_sessionmaker",
        lambda *_args, **_kwargs: _factory(session),
    )
    monkeypatch.setattr(jobs_module, "_database_now", _fixed_now)


def _expired_reaper_state(*, max_attempts: int = 5, cancel_requested: bool = False):
    queue = SimpleNamespace(
        tenant_id="tenant-a",
        job_id="job-a",
        phase="content",
        status="claimed",
        claimed_at=NOW - timedelta(seconds=6),
        available_at=NOW - timedelta(seconds=20),
        lease_owner="worker-a",
        lease_token="lease-a",
        lease_expires_at=NOW - timedelta(seconds=1),
        heartbeat_at=NOW - timedelta(seconds=2),
    )
    _, _, slots = _leased_state()
    for slot in slots:
        slot.lease_expires_at = NOW - timedelta(seconds=1)
    job_row = {
        "status": "generating_content",
        "attempt_count": 2,
        "max_attempts": max_attempts,
        "cancel_requested": cancel_requested,
        "quota_units": 7,
        "lease_token": "lease-a",
        "lease_expires_at": NOW - timedelta(seconds=1),
    }
    return queue, slots, job_row


@pytest.mark.asyncio
async def test_reaper_retry_records_queued_lease_lost_and_stage_after_update_fence(
    monkeypatch,
    metric_recorder,
) -> None:
    queue, slots, job_row = _expired_reaper_state()
    session = _Session(
        scalar_results=(queue,),
        scalars_results=(list(slots),),
        execute_results=(
            _Result(mapping={"tenant_id": queue.tenant_id, "job_id": queue.job_id}),
            _Result(mapping=job_row),
            _Result(rowcount=1),
        ),
    )
    repository = SqlAlchemyGenerationJobRepository()
    _install_reaper_session(monkeypatch, repository, session)

    reaped = await repository.reap_one_expired()

    assert reaped is not None and reaped.terminal_status is None
    lock_sql = [
        str(event[1]).lower()
        for event in session.events
        if isinstance(event, tuple)
        and event[0] in {"scalar", "scalars", "execute"}
        and "for update" in str(event[1]).lower()
    ]
    assert "generation_jobs" in lock_sql[0]
    assert "generation_queue" in lock_sql[1]
    assert "generation_slots" in lock_sql[2]
    fact_key = "tenant-a/job-a/content/2"
    assert _metric_events(session) == [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "queued",
                "fact_key": fact_key,
                "amount": 1,
            },
        ),
        (
            "counter",
            {
                "metric": "generation_retries_total",
                "category": "lease_lost",
                "fact_key": fact_key,
                "amount": 1,
            },
        ),
        (
            "histogram",
            {
                "metric": "generation_stage_seconds",
                "category": "content",
                "fact_key": fact_key,
                "seconds": 6.0,
            },
        ),
    ]
    _assert_metrics_after_last_flush(session)


@pytest.mark.parametrize("release_inserted", (False, True))
@pytest.mark.asyncio
async def test_reaper_terminal_release_counts_only_returning_insert(
    monkeypatch,
    metric_recorder,
    release_inserted: bool,
) -> None:
    queue, slots, job_row = _expired_reaper_state(max_attempts=2)
    release_id = jobs_module._quota_event_id("release", "job-a")
    session = _Session(
        scalar_results=(queue,),
        scalars_results=(list(slots),),
        execute_results=(
            _Result(mapping={"tenant_id": queue.tenant_id, "job_id": queue.job_id}),
            _Result(mapping=job_row),
            _Result(rowcount=1),
            _Result(scalar=release_id if release_inserted else None),
        ),
    )
    repository = SqlAlchemyGenerationJobRepository()
    _install_reaper_session(monkeypatch, repository, session)

    reaped = await repository.reap_one_expired()

    assert reaped is not None and reaped.terminal_status == "failed"
    release_statement = [
        event[1]
        for event in session.events
        if isinstance(event, tuple)
        and event[0] == "execute"
        and "INSERT INTO" in str(event[1])
        and "quota_ledger" in str(event[1])
    ][0]
    assert "RETURNING id" in str(release_statement)
    fact_key = "tenant-a/job-a/content/2"
    expected = [
        (
            "counter",
            {
                "metric": "generation_jobs_total",
                "category": "failed",
                "fact_key": fact_key,
                "amount": 1,
            },
        ),
        (
            "histogram",
            {
                "metric": "generation_stage_seconds",
                "category": "content",
                "fact_key": fact_key,
                "seconds": 6.0,
            },
        ),
    ]
    if release_inserted:
        expected.append(
            (
                "counter",
                {
                    "metric": "quota_units_total",
                    "category": "released",
                    "fact_key": release_id,
                    "amount": 7,
                },
            )
        )
    assert _metric_events(session) == expected
    _assert_metrics_after_last_flush(session)
