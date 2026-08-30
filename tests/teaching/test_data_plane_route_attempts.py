from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from deeptutor.teaching.openmaic.data_planes import JobRouteAttemptConflict
from deeptutor.teaching.repositories.data_planes import (
    SqlAlchemyDataPlaneRepository,
    _validated_route_attempt_counts,
)
from deeptutor.teaching.repositories.jobs import JobLeaseLost


def _attempt(attempt_count: int, phase: str):
    return SimpleNamespace(
        attempt_count=attempt_count,
        phase=phase,
        decision="selected",
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        worker_id=f"worker-{attempt_count}",
        config_revision="route-binding-v1",
        route_config_digest="a" * 64,
        provider_config_digest="b" * 64,
    )


def _counts(attempts):
    return _validated_route_attempt_counts(
        attempts,
        phase="content",
        expected_attempt_count=len(attempts),
        expected_data_plane_mode="dedicated",
        expected_route_id="dedicated-tenant-1",
        expected_provider_profile_id="provider-tenant-1",
        expected_worker_pool_ref="generation-tenant-1",
        expected_queue_ref="openmaic.tenant-1",
    )


def test_route_attempt_summary_accepts_only_forward_outline_to_content_lifecycle() -> None:
    assert _counts(
        [
            _attempt(1, "outline"),
            _attempt(2, "content"),
            _attempt(3, "content"),
        ]
    ) == (3, 0)
    assert _counts([_attempt(1, "content"), _attempt(2, "outline")]) is None
    assert _counts([_attempt(1, "outline"), _attempt(2, "outline")]) is None


@pytest.mark.parametrize(
    ("field", "digest"),
    [
        ("route_config_digest", "0" * 64),
        ("route_config_digest", "A" * 64),
        ("route_config_digest", "g" * 64),
        ("provider_config_digest", "0" * 64),
        ("provider_config_digest", "B" * 64),
        ("provider_config_digest", "z" * 64),
    ],
)
def test_route_attempt_summary_rejects_noncanonical_configuration_digest(
    field: str,
    digest: str,
) -> None:
    attempt = _attempt(1, "content")
    setattr(attempt, field, digest)

    assert _counts([attempt]) is None


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RouteAttemptSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[object, dict[str, object]]] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    async def scalar(self, statement, parameters=None):
        self.calls.append((statement, dict(parameters or {})))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _record_kwargs(**overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "job_id": "job-1",
        "phase": "content",
        "attempt_count": 2,
        "mode": "dedicated",
        "data_plane_route_id": "dedicated-tenant-1",
        "provider_profile_id": "provider-tenant-1",
        "worker_pool_ref": "generation-tenant-1",
        "queue_ref": "openmaic.tenant-1",
        "worker_id": "dedicated-worker-1",
        "lease_token": "lease-token-1",
        "outcome": "selected",
        "config_revision": "route-binding-v1",
        "route_config_digest": "a" * 64,
        "provider_config_digest": "b" * 64,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_route_attempt_uses_only_the_database_controlled_entrypoint(
    monkeypatch,
) -> None:
    session = _RouteAttemptSession([True])

    @asynccontextmanager
    async def sessions():
        yield session

    monkeypatch.setattr(
        "deeptutor.teaching.repositories.data_planes.platform_session",
        sessions,
    )

    await SqlAlchemyDataPlaneRepository().record_job_route_attempt(**_record_kwargs())

    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    sql = str(statement)
    assert "SELECT platform.record_generation_route_attempt(" in sql
    assert "INSERT INTO platform.generation_route_attempts" not in sql
    assert "lease-token-1" not in sql
    assert parameters["lease_token"] == "lease-token-1"
    assert parameters["config_revision"] == "route-binding-v1"
    assert parameters["route_config_digest"] == "a" * 64
    assert parameters["provider_config_digest"] == "b" * 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sqlstate", "expected_error"),
    [
        ("PGR02", JobLeaseLost),
        ("PGR03", JobRouteAttemptConflict),
        ("XX000", RuntimeError),
    ],
    ids=("stale-lease", "conflicting-fact", "unknown-database-error"),
)
async def test_route_attempt_maps_database_fence_failures_to_domain_errors(
    monkeypatch,
    sqlstate: str,
    expected_error: type[Exception],
) -> None:
    class ControlledEntryError(Exception):
        pass

    original = ControlledEntryError("controlled route attempt rejected")
    original.sqlstate = sqlstate
    database_error = DBAPIError(
        "SELECT controlled_entrypoint",
        {"lease_token": "lease-token-1"},
        original,
        False,
    )
    session = _RouteAttemptSession([database_error])

    @asynccontextmanager
    async def sessions():
        yield session

    monkeypatch.setattr(
        "deeptutor.teaching.repositories.data_planes.platform_session",
        sessions,
    )

    with pytest.raises(expected_error) as captured:
        await SqlAlchemyDataPlaneRepository().record_job_route_attempt(**_record_kwargs())

    assert len(session.calls) == 1
    assert "lease-token-1" not in str(captured.value)


@pytest.mark.asyncio
async def test_route_attempt_summary_exposes_last_and_final_selected_facts(
    monkeypatch,
) -> None:
    attempts = [
        _attempt(1, "outline"),
        _attempt(2, "content"),
        _attempt(3, "content"),
    ]

    class Mappings:
        def all(self):
            return [vars(attempt) for attempt in attempts]

    class Result:
        def mappings(self):
            return Mappings()

    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple[object, dict[str, object]]] = []

        async def execute(self, statement, parameters=None):
            self.calls.append((statement, dict(parameters or {})))
            return Result()

    session = Session()

    @asynccontextmanager
    async def sessions():
        yield session

    monkeypatch.setattr(
        "deeptutor.teaching.repositories.data_planes.platform_session",
        sessions,
    )

    summary = await SqlAlchemyDataPlaneRepository().resolve_job_route_audit(
        "tenant-1",
        "job-1",
        phase="content",
        expected_attempt_count=3,
        expected_data_plane_mode="dedicated",
        expected_route_id="dedicated-tenant-1",
        expected_provider_profile_id="provider-tenant-1",
        expected_worker_pool_ref="generation-tenant-1",
        expected_queue_ref="openmaic.tenant-1",
    )

    assert summary is not None
    assert summary.last_attempt_phase == "content"
    assert summary.last_attempt_decision == "selected"
    assert summary.final_phase_selected is True
    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    sql = str(statement)
    assert "SELECT * FROM platform.read_generation_route_attempts(" in sql
    assert "FROM platform.generation_route_attempts" not in sql
    assert parameters == {
        "tenant_id": "tenant-1",
        "job_id": "job-1",
        "data_plane_mode": "dedicated",
        "data_plane_route_id": "dedicated-tenant-1",
        "provider_profile_id": "provider-tenant-1",
        "worker_pool_ref": "generation-tenant-1",
        "queue_ref": "openmaic.tenant-1",
    }
