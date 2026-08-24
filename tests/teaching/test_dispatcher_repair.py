from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from deeptutor.teaching import dispatcher as dispatcher_module
from deeptutor.teaching.dispatcher import OutboxDispatcher
from deeptutor.teaching.models.jobs import GenerationQueue


class _Context:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Result:
    def __init__(self, row: dict[str, object] | None = None, *, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class _Session:
    def __init__(self, message: SimpleNamespace, now: datetime) -> None:
        self._scalars = iter((message, now))
        self._results = iter(
            (
                _Result(),
                _Result(
                    {
                        "status": "queued",
                        "job_kind": "generation",
                        "phase": "content",
                        "export_format": None,
                        "priority": 500,
                        "data_plane_route_id": "authoritative-route",
                        "provider_profile_id": "authoritative-provider",
                        "worker_pool_ref": "authoritative-pool",
                        "queue_ref": "authoritative-queue",
                    }
                ),
                _Result(rowcount=1),
                _Result(rowcount=1),
            )
        )

    def begin(self) -> _Context:
        return _Context(None)

    async def scalar(self, _statement: object) -> object:
        return next(self._scalars)

    async def execute(self, _statement: object, _parameters: object = None) -> _Result:
        return next(self._results)

    async def flush(self) -> None:
        return None


class _InsertRecorder:
    def __init__(self, model: type[object], repairs: list[dict[str, object]]) -> None:
        self._model = model
        self._repairs = repairs

    def values(self, **_values: object) -> _InsertRecorder:
        return self

    def on_conflict_do_update(
        self,
        *,
        index_elements: object,
        set_: dict[str, object],
        where: object,
    ) -> _InsertRecorder:
        del index_elements, where
        if self._model is GenerationQueue:
            self._repairs.append(set_)
        return self

    def on_conflict_do_nothing(self, *, index_elements: object) -> _InsertRecorder:
        del index_elements
        return self


def test_queued_outbox_repair_preserves_schedule_while_rebinding_authoritatively(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)
    message = SimpleNamespace(
        tenant_id="tenant-a",
        job_id="job-a",
        event_id="event-a",
        job_kind="generation",
        phase="content",
        data_plane_route_id="stale-route",
        provider_profile_id="stale-provider",
        worker_pool_ref="stale-pool",
        queue_ref="stale-queue",
        slot_pool="generation",
        available_at=now - timedelta(minutes=5),
        delivered_at=None,
    )
    session = _Session(message, now)
    repairs: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatcher_module,
        "async_sessionmaker",
        lambda *_args, **_kwargs: lambda: _Context(session),
    )
    monkeypatch.setattr(
        dispatcher_module,
        "insert",
        lambda model: _InsertRecorder(model, repairs),
    )

    dispatched = asyncio.run(OutboxDispatcher(object()).dispatch_next())

    assert dispatched is not None
    assert len(repairs) == 1
    repair = repairs[0]
    assert "available_at" not in repair
    assert "enqueued_at" not in repair
    assert repair["priority"] == 500
    assert repair["data_plane_route_id"] == "authoritative-route"
    assert repair["provider_profile_id"] == "authoritative-provider"
    assert repair["worker_pool_ref"] == "authoritative-pool"
    assert repair["queue_ref"] == "authoritative-queue"
