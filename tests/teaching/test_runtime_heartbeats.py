from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from types import SimpleNamespace

from pydantic import SecretStr
import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.health import REQUIRED_HEALTH_COMPONENTS, TeachingHealthService

NOW = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)


class RecordingRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.snapshots: tuple[object, ...] = ()
        self.heartbeat_result = True
        self.stop_result = True
        self.heartbeat_seen = asyncio.Event()

    async def register(self, role: str, instance_id: str) -> None:
        self.events.append(("register", role, instance_id))

    async def heartbeat(self, role: str, instance_id: str) -> bool:
        self.events.append(("heartbeat", role, instance_id))
        self.heartbeat_seen.set()
        return self.heartbeat_result

    async def mark_stopped(self, role: str, instance_id: str) -> bool:
        self.events.append(("stop", role, instance_id))
        return self.stop_result

    async def latest_running_heartbeats(self, roles):
        return self.snapshots


def _assert_no_runtime_heartbeat_tasks() -> None:
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("teaching-runtime-heartbeat:")
    ]


async def _wait_for_all_tasks(tasks, **_kwargs):
    await asyncio.gather(*tasks, return_exceptions=True)
    return set(tasks), set()


@pytest.mark.asyncio
async def test_durable_report_uses_snapshot_instead_of_fresh_process_memory() -> None:
    from deeptutor.teaching.runtime_heartbeat import RuntimeHeartbeatSnapshot

    repository = RecordingRepository()
    repository.snapshots = (
        RuntimeHeartbeatSnapshot(
            role="dispatcher",
            age_seconds=91,
        ),
    )
    service = TeachingHealthService(now=lambda: NOW, stale_after_seconds=90)
    for component in REQUIRED_HEALTH_COMPONENTS:
        service.set_status(component, "healthy")
    service.set_heartbeat("dispatcher")

    report = await service.report_durable(repository)

    assert report.status == "degraded"
    assert report.components["dispatcher"].status == "stale"
    assert report.components["dispatcher"].age_seconds == 91
    assert report.components["generation_worker"].status == "unknown"


@pytest.mark.asyncio
async def test_durable_report_redacts_repository_failure_details() -> None:
    class FailingRepository(RecordingRepository):
        async def latest_running_heartbeats(self, roles):
            raise RuntimeError("postgresql://user:secret@example.invalid/private")

    service = TeachingHealthService(now=lambda: NOW)

    report = await service.report_durable(FailingRepository())

    serialized = repr(report)
    assert report.status == "degraded"
    assert "secret" not in serialized
    assert report.components["dispatcher"].reason == "heartbeat_repository_unavailable"


@pytest.mark.asyncio
async def test_durable_report_uses_freshest_running_replica() -> None:
    from deeptutor.teaching.runtime_heartbeat import (
        RUNTIME_PROCESS_ROLES,
        RuntimeHeartbeatSnapshot,
    )

    repository = RecordingRepository()
    repository.snapshots = (
        *(
            RuntimeHeartbeatSnapshot(
                role=role,
                age_seconds=1,
            )
            for role in RUNTIME_PROCESS_ROLES
            if role != "dispatcher"
        ),
        RuntimeHeartbeatSnapshot(
            role="dispatcher",
            age_seconds=120,
        ),
        RuntimeHeartbeatSnapshot(
            role="dispatcher",
            age_seconds=1,
        ),
    )
    service = TeachingHealthService(now=lambda: NOW, stale_after_seconds=90)
    for component in REQUIRED_HEALTH_COMPONENTS:
        service.set_status(component, "healthy")
    service.set_heartbeat("dispatcher", age_seconds=120)

    report = await service.report_durable(repository)

    assert report.status == "healthy"
    assert report.components["dispatcher"].status == "healthy"
    assert report.components["dispatcher"].age_seconds == 1


@pytest.mark.asyncio
async def test_durable_report_is_stale_when_every_running_replica_is_stale() -> None:
    from deeptutor.teaching.runtime_heartbeat import (
        RUNTIME_PROCESS_ROLES,
        RuntimeHeartbeatSnapshot,
    )

    repository = RecordingRepository()
    repository.snapshots = (
        *(
            RuntimeHeartbeatSnapshot(
                role=role,
                age_seconds=1,
            )
            for role in RUNTIME_PROCESS_ROLES
            if role != "dispatcher"
        ),
        RuntimeHeartbeatSnapshot(
            role="dispatcher",
            age_seconds=120,
        ),
        RuntimeHeartbeatSnapshot(
            role="dispatcher",
            age_seconds=91,
        ),
    )
    service = TeachingHealthService(now=lambda: NOW, stale_after_seconds=90)
    for component in REQUIRED_HEALTH_COMPONENTS:
        service.set_status(component, "healthy")
    service.set_heartbeat("dispatcher")

    report = await service.report_durable(repository)

    assert report.status == "degraded"
    assert report.components["dispatcher"].status == "stale"
    assert report.components["dispatcher"].age_seconds == 91


@pytest.mark.asyncio
async def test_durable_report_is_unknown_when_no_replica_is_running() -> None:
    from deeptutor.teaching.runtime_heartbeat import (
        RUNTIME_PROCESS_ROLES,
        RuntimeHeartbeatSnapshot,
    )

    repository = RecordingRepository()
    repository.snapshots = tuple(
        RuntimeHeartbeatSnapshot(
            role=role,
            age_seconds=1,
        )
        for role in RUNTIME_PROCESS_ROLES
        if role != "dispatcher"
    )
    service = TeachingHealthService(now=lambda: NOW, stale_after_seconds=90)
    for component in REQUIRED_HEALTH_COMPONENTS:
        service.set_status(component, "healthy")
    service.set_heartbeat("dispatcher")

    report = await service.report_durable(repository)

    assert report.status == "degraded"
    assert report.components["dispatcher"].status == "unknown"
    assert report.components["dispatcher"].reason == "heartbeat_missing"


@pytest.mark.asyncio
async def test_runtime_supervisor_registers_before_work_and_stops_without_task_leak() -> None:
    from deeptutor.teaching.runtime_heartbeat import RuntimeHeartbeatSupervisor

    repository = RecordingRepository()
    supervisor = RuntimeHeartbeatSupervisor(
        repository,
        role="dispatcher",
        heartbeat_interval_seconds=0.01,
        instance_id="dispatcher:0123456789abcdef0123456789abcdef",
    )

    async def work() -> bool:
        assert [event[0] for event in repository.events] == ["register"]
        return True

    assert await supervisor.run(work)
    assert [event[0] for event in repository.events] == ["register", "stop"]
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("teaching-runtime-heartbeat:")
    ]


@pytest.mark.asyncio
async def test_runtime_supervisor_propagates_cancel_and_marks_stopped() -> None:
    from deeptutor.teaching.runtime_heartbeat import RuntimeHeartbeatSupervisor

    repository = RecordingRepository()
    started = asyncio.Event()
    supervisor = RuntimeHeartbeatSupervisor(
        repository,
        role="projector",
        heartbeat_interval_seconds=60,
        instance_id="projector:0123456789abcdef0123456789abcdef",
    )

    async def work() -> bool:
        started.set()
        await asyncio.Event().wait()
        return False

    task = asyncio.create_task(supervisor.run(work))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert [event[0] for event in repository.events] == ["register", "stop"]


@pytest.mark.asyncio
async def test_runtime_supervisor_fails_closed_when_fenced_heartbeat_is_lost() -> None:
    from deeptutor.teaching.runtime_heartbeat import (
        RuntimeHeartbeatSupervisor,
        RuntimeHeartbeatUnavailable,
    )

    repository = RecordingRepository()
    repository.heartbeat_result = False
    cancelled = asyncio.Event()
    supervisor = RuntimeHeartbeatSupervisor(
        repository,
        role="generation_worker",
        heartbeat_interval_seconds=0.001,
        instance_id="generation_worker:0123456789abcdef0123456789abcdef",
    )

    async def work() -> bool:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return False

    with pytest.raises(RuntimeHeartbeatUnavailable, match="heartbeat unavailable"):
        await asyncio.wait_for(supervisor.run(work), timeout=1)

    assert cancelled.is_set()
    assert [event[0] for event in repository.events] == [
        "register",
        "heartbeat",
        "stop",
    ]


@pytest.mark.asyncio
async def test_runtime_supervisor_fails_closed_when_stop_fence_is_lost() -> None:
    from deeptutor.teaching.runtime_heartbeat import (
        RuntimeHeartbeatSupervisor,
        RuntimeHeartbeatUnavailable,
    )

    repository = RecordingRepository()
    repository.stop_result = False
    supervisor = RuntimeHeartbeatSupervisor(
        repository,
        role="dispatcher",
        heartbeat_interval_seconds=60,
        instance_id="dispatcher:0123456789abcdef0123456789abcdef",
    )

    with pytest.raises(RuntimeHeartbeatUnavailable, match="heartbeat unavailable"):
        await supervisor.run(lambda: asyncio.sleep(0, result=True))
    assert [event[0] for event in repository.events] == ["register", "stop"]


@pytest.mark.asyncio
async def test_runtime_supervisor_prioritizes_workload_error_when_both_tasks_finish(
    monkeypatch,
) -> None:
    from deeptutor.teaching import runtime_heartbeat

    repository = RecordingRepository()
    repository.heartbeat_result = False
    supervisor = runtime_heartbeat.RuntimeHeartbeatSupervisor(
        repository,
        role="dispatcher",
        heartbeat_interval_seconds=0.001,
        instance_id="dispatcher:0123456789abcdef0123456789abcdef",
    )

    async def work() -> bool:
        raise RuntimeError("workload failed")

    monkeypatch.setattr(runtime_heartbeat.asyncio, "wait", _wait_for_all_tasks)

    with pytest.raises(RuntimeError, match="workload failed") as caught:
        await supervisor.run(work)
    assert not isinstance(caught.value, runtime_heartbeat.RuntimeHeartbeatUnavailable)
    assert [event[0] for event in repository.events] == [
        "register",
        "heartbeat",
        "stop",
    ]
    _assert_no_runtime_heartbeat_tasks()


@pytest.mark.asyncio
async def test_runtime_supervisor_prioritizes_workload_cancel_when_both_tasks_finish(
    monkeypatch,
) -> None:
    from deeptutor.teaching import runtime_heartbeat

    repository = RecordingRepository()
    repository.heartbeat_result = False
    supervisor = runtime_heartbeat.RuntimeHeartbeatSupervisor(
        repository,
        role="dispatcher",
        heartbeat_interval_seconds=0.001,
        instance_id="dispatcher:0123456789abcdef0123456789abcdef",
    )

    async def work() -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime_heartbeat.asyncio, "wait", _wait_for_all_tasks)

    with pytest.raises(asyncio.CancelledError):
        await supervisor.run(work)
    assert [event[0] for event in repository.events] == [
        "register",
        "heartbeat",
        "stop",
    ]
    _assert_no_runtime_heartbeat_tasks()


@pytest.mark.asyncio
async def test_runtime_supervisor_fails_closed_when_normal_work_and_heartbeat_finish(
    monkeypatch,
) -> None:
    from deeptutor.teaching import runtime_heartbeat

    repository = RecordingRepository()
    repository.heartbeat_result = False
    supervisor = runtime_heartbeat.RuntimeHeartbeatSupervisor(
        repository,
        role="dispatcher",
        heartbeat_interval_seconds=0.001,
        instance_id="dispatcher:0123456789abcdef0123456789abcdef",
    )

    monkeypatch.setattr(runtime_heartbeat.asyncio, "wait", _wait_for_all_tasks)

    with pytest.raises(
        runtime_heartbeat.RuntimeHeartbeatUnavailable,
        match="heartbeat unavailable",
    ):
        await supervisor.run(lambda: asyncio.sleep(0, result=True))
    assert [event[0] for event in repository.events] == [
        "register",
        "heartbeat",
        "stop",
    ]
    _assert_no_runtime_heartbeat_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process_name", "role"),
    (
        ("dispatcher", "dispatcher"),
        ("worker", "generation_worker"),
        ("export-worker", "export_worker"),
        ("reaper", "reaper"),
        ("learning-projector", "projector"),
        ("tenant-provisioner", "tenant_provisioner"),
    ),
)
async def test_run_process_wraps_every_enabled_role_with_durable_heartbeat(
    monkeypatch,
    process_name: str,
    role: str,
) -> None:
    from deeptutor.teaching import processes

    repository = RecordingRepository()
    calls: list[str] = []

    async def workload(name, settings, *, once):
        calls.append(name)
        assert repository.events[0][:2] == ("register", role)
        return True

    monkeypatch.setattr(processes, "_run_process_workload", workload)

    assert await processes.run_process(
        process_name,
        once=True,
        settings=PlatformSettings(
            enabled=True,
            database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        ),
        heartbeat_repository=repository,
        heartbeat_interval_seconds=0.01,
    )
    assert calls == [process_name]
    assert [event[0] for event in repository.events] == ["register", "stop"]
    assert repository.events[0][2] == repository.events[-1][2]


def test_runtime_update_statements_are_instance_fenced() -> None:
    from deeptutor.teaching.repositories.runtime_heartbeats import (
        build_heartbeat_statement,
        build_mark_stopped_statement,
    )

    heartbeat_sql = str(
        build_heartbeat_statement("dispatcher", "dispatcher:old").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    stopped_sql = str(
        build_mark_stopped_statement("dispatcher", "dispatcher:old").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    for statement in (heartbeat_sql, stopped_sql):
        assert "role = 'dispatcher'" in statement
        assert "instance_id = 'dispatcher:old'" in statement
        assert "status = 'running'" in statement
    assert "instance_id" not in heartbeat_sql.partition(" SET ")[0]


def test_runtime_instance_id_is_unique_role_scoped_and_opaque() -> None:
    from deeptutor.teaching.runtime_heartbeat import new_runtime_instance_id

    first = new_runtime_instance_id("dispatcher")
    second = new_runtime_instance_id("dispatcher")

    assert first != second
    assert re.fullmatch(r"dispatcher:[0-9a-f]{32}", first)
    assert "@" not in first


@pytest.mark.asyncio
async def test_durable_report_uses_database_age_when_api_clock_is_ahead() -> None:
    repository = RecordingRepository()
    repository.snapshots = (SimpleNamespace(role="dispatcher", age_seconds=1.0),)
    service = TeachingHealthService(
        now=lambda: NOW + timedelta(days=3650),
        stale_after_seconds=90,
    )
    service.set_heartbeat("dispatcher", age_seconds=120)

    report = await service.report_durable(repository)

    assert report.components["dispatcher"].status == "healthy"
    assert report.components["dispatcher"].age_seconds == 1


@pytest.mark.asyncio
async def test_durable_report_uses_database_age_when_api_clock_is_behind() -> None:
    repository = RecordingRepository()
    repository.snapshots = (SimpleNamespace(role="dispatcher", age_seconds=91.0),)
    service = TeachingHealthService(
        now=lambda: NOW - timedelta(days=3650),
        stale_after_seconds=90,
    )
    service.set_heartbeat("dispatcher")

    report = await service.report_durable(repository)

    assert report.components["dispatcher"].status == "stale"
    assert report.components["dispatcher"].age_seconds == 91


@pytest.mark.asyncio
async def test_durable_report_clamps_negative_database_age() -> None:
    repository = RecordingRepository()
    repository.snapshots = (SimpleNamespace(role="dispatcher", age_seconds=-5.0),)
    service = TeachingHealthService(now=lambda: NOW, stale_after_seconds=90)

    report = await service.report_durable(repository)

    assert report.components["dispatcher"].status == "healthy"
    assert report.components["dispatcher"].age_seconds == 0


def test_latest_running_query_uses_database_clock_and_retention_window() -> None:
    from deeptutor.teaching.repositories.runtime_heartbeats import (
        build_latest_running_heartbeats_statement,
    )
    from deeptutor.teaching.runtime_heartbeat import RUNTIME_PROCESS_ROLES

    sql = str(
        build_latest_running_heartbeats_statement(RUNTIME_PROCESS_ROLES).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "EXTRACT(epoch FROM now() - max(" in sql
    assert "heartbeat_at >= now() - INTERVAL '7 days'" in sql
    assert "GROUP BY" in sql


def test_runtime_retention_constants_and_prune_statement_are_bounded() -> None:
    from deeptutor.teaching.repositories.runtime_heartbeats import (
        build_retention_prune_statement,
    )
    from deeptutor.teaching.runtime_heartbeat import (
        RUNTIME_HEARTBEAT_PRUNE_BATCH_SIZE,
        RUNTIME_HEARTBEAT_RETENTION_SECONDS,
    )

    assert RUNTIME_HEARTBEAT_RETENTION_SECONDS == 7 * 24 * 60 * 60
    assert RUNTIME_HEARTBEAT_PRUNE_BATCH_SIZE == 500
    sql = str(
        build_retention_prune_statement().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert sql.startswith("DELETE FROM platform.teaching_runtime_process_heartbeats")
    assert "SELECT platform.teaching_runtime_process_heartbeats.role" in sql
    assert "platform.teaching_runtime_process_heartbeats.instance_id" in sql
    assert "stopped_at < now() - INTERVAL '7 days'" in sql
    assert "heartbeat_at < now() - INTERVAL '7 days'" in sql
    assert "LIMIT 500 FOR UPDATE SKIP LOCKED" in sql


@pytest.mark.asyncio
async def test_register_prunes_before_inserting_the_new_instance() -> None:
    from deeptutor.teaching.repositories.runtime_heartbeats import (
        SqlAlchemyRuntimeHeartbeatRepository,
    )

    events: list[str] = []

    class Session:
        async def execute(self, statement):
            events.append(statement.__class__.__name__)

        def add(self, record) -> None:
            events.append("add")

        async def commit(self) -> None:
            events.append("commit")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    repository = SqlAlchemyRuntimeHeartbeatRepository(SessionContext)

    await repository.register(
        "dispatcher",
        "dispatcher:0123456789abcdef0123456789abcdef",
    )

    assert events == ["Delete", "add", "commit"]


def test_runtime_heartbeat_indexes_support_latest_and_retention() -> None:
    from deeptutor.teaching.models import TeachingRuntimeProcessHeartbeat

    indexes = {index.name for index in TeachingRuntimeProcessHeartbeat.__table__.indexes}

    assert indexes == {
        "ix_teaching_runtime_process_heartbeats_role_heartbeat_running",
        "ix_teaching_runtime_process_heartbeats_heartbeat_running_ttl",
        "ix_teaching_runtime_process_heartbeats_stopped_at_retention",
    }


def test_runtime_heartbeat_schema_names_fit_postgresql_and_match_migration() -> None:
    from deeptutor.teaching.models import TeachingRuntimeProcessHeartbeat

    table = TeachingRuntimeProcessHeartbeat.__table__
    orm_names = {
        str(item.name) for item in (*table.constraints, *table.indexes) if item.name is not None
    }
    migration = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260824_0018_teaching_runtime_heartbeats.py"
    ).read_text(encoding="utf-8")
    migration_names = set(
        re.findall(
            r'"((?:ck|pk|ix)_teaching_runtime_process_heartbeats[^"]*)"',
            migration,
        )
    )

    assert migration_names == orm_names
    assert {name: len(name.encode("utf-8")) for name in orm_names if len(name.encode()) > 63} == {}


@pytest.mark.asyncio
async def test_health_route_reads_the_durable_repository() -> None:
    from deeptutor.api.routers.teaching_health import teaching_health
    from deeptutor.teaching.health_probes import (
        ACTIVE_PROBE_COMPONENTS,
        ActiveProbeResult,
    )
    from deeptutor.teaching.runtime_heartbeat import RuntimeHeartbeatSnapshot

    class HealthyActiveProbes:
        async def probe(self):
            return {
                component: ActiveProbeResult(status="healthy")
                for component in ACTIVE_PROBE_COMPONENTS
            }

    repository = RecordingRepository()
    repository.snapshots = (
        RuntimeHeartbeatSnapshot(
            role="dispatcher",
            age_seconds=91,
        ),
    )
    service = TeachingHealthService(now=lambda: NOW, stale_after_seconds=90)
    service.set_heartbeat("dispatcher")

    payload = await teaching_health(service, repository, HealthyActiveProbes())

    assert payload["components"]["dispatcher"]["status"] == "stale"
    assert "instance_id" not in repr(payload)


def test_runtime_heartbeat_migration_is_packaged_before_the_declared_head() -> None:
    from deeptutor.teaching.migrations.runner import TEACHING_MIGRATION_HEAD_REVISION

    root = Path(__file__).resolve().parents[2]
    migration = (
        root
        / "deeptutor"
        / "teaching"
        / "migrations"
        / "versions"
        / "20260824_0018_teaching_runtime_heartbeats.py"
    )

    assert TEACHING_MIGRATION_HEAD_REVISION == "20260830_0023"
    assert migration.is_file()
