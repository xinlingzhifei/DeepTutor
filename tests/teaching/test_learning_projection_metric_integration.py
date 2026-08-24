from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from deeptutor.teaching.projector_worker import (
    ProjectionClaim,
    ProjectionLeaseLost,
    SqlAlchemyProjectionQueueRepository,
)
from deeptutor.teaching.projectors.mastery import ProjectionEvent
from deeptutor.teaching.schema_names import tenant_schema_name


def test_projector_scan_requires_metric_backlog_revision() -> None:
    import deeptutor.teaching.projector_worker as worker_module

    assert worker_module._MINIMUM_SCHEMA_REVISION == "20260825_0019"


class _Context:
    def __init__(self, value=None) -> None:
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_args) -> None:
        return None


class _Session:
    def __init__(self, rows=()) -> None:
        self._rows = rows

    def begin(self):
        return _Context()

    async def scalar(self, _statement):
        return datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC)

    async def execute(self, _statement):
        rows = self._rows

        class Result:
            def all(self):
                return rows

        return Result()


class _Factory:
    def __init__(self, session: _Session) -> None:
        self._session = session

    def __call__(self):
        return _Context(self._session)


def _claim(event_id: str) -> ProjectionClaim:
    return ProjectionClaim(
        event=ProjectionEvent(
            event_id=event_id,
            tenant_id="tenant-a",
            session_id="session-a",
            user_id="student-a",
            classroom_version_id="version-a",
            seq=1,
            event_type="classroom.started",
            occurred_at=datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC),
            scene_id=None,
            knowledge_point_id=None,
            payload={"schema_version": "1.0"},
        ),
        lease_owner="worker-a",
        lease_token="token-a",
    )


def _item(*, attempt_count: int = 1, max_attempts: int = 5):
    return SimpleNamespace(
        status="running",
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        available_at=None,
        last_error_code=None,
        lease_owner="worker-a",
        lease_token="token-a",
        lease_expires_at=datetime(2026, 8, 25, 1, 3, 3, tzinfo=UTC),
        heartbeat_at=datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC),
    )


def _repository(session: _Session) -> SqlAlchemyProjectionQueueRepository:
    repository = object.__new__(SqlAlchemyProjectionQueueRepository)
    repository._tenant_sessions = lambda _tenant_id: _Factory(session)
    return repository


@pytest.mark.asyncio
async def test_active_tenant_ids_excludes_0018_and_accepts_0019() -> None:
    candidates = (
        ("tenant-0018", tenant_schema_name("tenant-0018"), "20260824_0018"),
        ("tenant-0019", tenant_schema_name("tenant-0019"), "20260825_0019"),
    )

    class PlatformSession:
        async def execute(self, statement):
            compiled = statement.compile()
            sql = " ".join(str(compiled).lower().split())
            assert "tenant_schema_states.revision >=" in sql
            minimum = next(value for value in compiled.params.values() if value == "20260825_0019")
            rows = [
                (tenant_id, schema_name)
                for tenant_id, schema_name, revision in candidates
                if revision >= minimum
            ]

            class Result:
                def all(self):
                    return rows

            return Result()

    repository = object.__new__(SqlAlchemyProjectionQueueRepository)
    repository._platform_sessions = _Factory(PlatformSession())

    assert await repository.active_tenant_ids() == ("tenant-0019",)


@pytest.mark.asyncio
async def test_every_claim_terminal_path_deletes_the_exact_backlog_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeptutor.teaching.projector_worker as worker_module

    deleted: list[tuple[object, str, str]] = []

    async def delete_backlog(session, *, tenant_id: str, event_id: str) -> None:
        deleted.append((session, tenant_id, event_id))

    monkeypatch.setattr(
        worker_module,
        "delete_learning_projection_backlog",
        delete_backlog,
        raising=False,
    )

    async def run_terminal(event_id: str, operation: str) -> _Session:
        session = _Session()
        repository = _repository(session)
        item = _item(attempt_count=2, max_attempts=2)
        stored_event = SimpleNamespace(tenant_id="tenant-a", event_id=event_id)

        async def locked(_session, _claim_value):
            return item, stored_event

        async def apply_projection(_session, _claim_value, *, document):
            assert document is None
            return item

        async def store_quarantine(_session, _event, _reason_code) -> None:
            return None

        monkeypatch.setattr(repository, "_locked_claim", locked)
        monkeypatch.setattr(repository, "_apply_projection", apply_projection)
        monkeypatch.setattr(repository, "_store_quarantine", store_quarantine)
        claim = _claim(event_id)
        if operation == "project":
            await repository.project(claim, document=None)
        elif operation == "complete":
            await repository.complete(claim)
        elif operation == "quarantine":
            await repository.quarantine(claim, reason_code="invalid")
        else:
            await repository.retry(claim, error_code="transient")
        return session

    sessions = [
        await run_terminal("event-project", "project"),
        await run_terminal("event-complete", "complete"),
        await run_terminal("event-quarantine", "quarantine"),
        await run_terminal("event-exhausted", "retry"),
    ]

    assert deleted == [
        (sessions[0], "tenant-a", "event-project"),
        (sessions[1], "tenant-a", "event-complete"),
        (sessions[2], "tenant-a", "event-quarantine"),
        (sessions[3], "tenant-a", "event-exhausted"),
    ]


@pytest.mark.asyncio
async def test_retry_retains_backlog_and_lost_lease_cannot_delete_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeptutor.teaching.projector_worker as worker_module

    async def unexpected_delete(*_args, **_kwargs) -> None:
        raise AssertionError("nonterminal or lease-lost work must retain backlog")

    monkeypatch.setattr(
        worker_module,
        "delete_learning_projection_backlog",
        unexpected_delete,
        raising=False,
    )
    session = _Session()
    repository = _repository(session)
    item = _item(attempt_count=1, max_attempts=2)
    event = SimpleNamespace(tenant_id="tenant-a", event_id="event-retry")

    async def locked(_session, _claim_value):
        return item, event

    monkeypatch.setattr(repository, "_locked_claim", locked)
    await repository.retry(_claim("event-retry"), error_code="transient")
    assert item.status == "failed"

    async def lease_lost(_session, _claim_value):
        raise ProjectionLeaseLost("reclaimed")

    monkeypatch.setattr(repository, "_locked_claim", lease_lost)
    with pytest.raises(ProjectionLeaseLost, match="reclaimed"):
        await repository.complete(_claim("event-stale"))


@pytest.mark.asyncio
async def test_expired_exhausted_lease_sweep_deletes_each_terminal_backlog_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeptutor.teaching.projector_worker as worker_module

    first_item = _item(attempt_count=5, max_attempts=5)
    second_item = _item(attempt_count=5, max_attempts=5)
    first_event = SimpleNamespace(tenant_id="tenant-a", event_id="event-a")
    second_event = SimpleNamespace(tenant_id="tenant-a", event_id="event-b")
    session = _Session(((first_item, first_event), (second_item, second_event)))
    repository = _repository(session)
    deleted: list[tuple[object, str, str]] = []

    async def store_quarantine(_session, _event, _reason_code) -> None:
        return None

    async def delete_backlog(delete_session, *, tenant_id: str, event_id: str) -> None:
        deleted.append((delete_session, tenant_id, event_id))

    monkeypatch.setattr(repository, "_store_quarantine", store_quarantine)
    monkeypatch.setattr(
        worker_module,
        "delete_learning_projection_backlog",
        delete_backlog,
        raising=False,
    )

    await repository._quarantine_exhausted_leases(session, "tenant-a")

    assert deleted == [
        (session, "tenant-a", "event-a"),
        (session, "tenant-a", "event-b"),
    ]
    assert first_item.status == second_item.status == "quarantined"
