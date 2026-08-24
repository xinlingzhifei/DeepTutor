from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from deeptutor.logging import bind_log_context
from deeptutor.logging.formatters import ContextFilter, JsonlFormatter
from deeptutor.services.config import PlatformSettings
from deeptutor.services.config.platform_settings import (
    validate_object_store_endpoint,
    validate_render_health_url,
)
from deeptutor.teaching.health_logging import redact_health_transport_logs
from deeptutor.teaching.health_probes import (
    ACTIVE_PROBE_COMPONENTS,
    TEACHING_SCHEMA_REVISION,
    ActiveHealthProbeService,
    DatabaseHealthProbe,
    HealthProbeFailure,
    MigrationHealthProbe,
    MigrationHealthSnapshot,
    ObjectStoreHealthProbe,
    OpenMAICDataPlaneHealthProbes,
    RenderHealthProbe,
    RuntimeOpenMAICHealthClientFactory,
    SqlAlchemyMigrationHealthRepository,
    build_migration_health_statement,
)
import deeptutor.teaching.object_store as object_store_module
from deeptutor.teaching.object_store import (
    OBJECT_STORE_HEALTH_CONNECT_TIMEOUT_SECONDS,
    OBJECT_STORE_HEALTH_READ_TIMEOUT_SECONDS,
    OBJECT_STORE_HEALTH_THREAD_WORKERS,
    LocalClassroomArtifactStore,
    ObjectStoreConfigurationError,
    ObjectStoreError,
    S3ClassroomArtifactStore,
    check_s3_object_store_health,
    run_s3_health_sync,
)
from deeptutor.teaching.openmaic.client import (
    EXPECTED_APP_VERSION,
    EXPECTED_UPSTREAM_COMMIT,
    MAX_HEALTH_RESPONSE_BYTES,
    REQUIRED_CAPABILITIES,
    REQUIRED_EXPORT_FORMATS,
    SUPPORTED_CONTRACT_VERSION,
    IncompatibleOpenMAIC,
    InvalidOpenMAICResponse,
    OpenMAICContractHealthClient,
)
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneRouteRecord,
    DedicatedDataPlaneHealthInventory,
)
from deeptutor.teaching.repositories.data_planes import (
    SqlAlchemyDataPlaneRepository,
    build_dedicated_health_inventory_statement,
    build_shared_health_route_statement,
)
from deeptutor.teaching.storage_credentials import (
    ResolvedStorageCredentials,
    SqlAlchemyStorageCredentialRepository,
    StorageHealthInventory,
    TenantStorageCredentialRecord,
    build_storage_health_inventory_statement,
)


def _healthy_probes(calls: list[str]):
    async def probe(component: str) -> None:
        calls.append(component)

    return {
        component: lambda component=component: probe(component)
        for component in ACTIVE_PROBE_COMPONENTS
    }


@pytest.mark.parametrize(
    ("validator", "value"),
    [
        (validate_object_store_endpoint, "https://private-store／bucket"),
        (validate_render_health_url, "https://private-render／health"),
    ],
)
def test_health_endpoint_validator_redacts_urlsplit_errors(validator, value: str) -> None:
    with pytest.raises(ValueError) as error:
        validator(value)

    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    ("validator", "value", "expected_error"),
    [
        (
            validate_object_store_endpoint,
            "http://bad host:9000",
            "object store endpoint is invalid",
        ),
        (
            validate_object_store_endpoint,
            "http://bad\u00a0host:9000",
            "object store endpoint is invalid",
        ),
        (
            validate_render_health_url,
            "http://bad host:9000/health",
            "render health URL is invalid",
        ),
        (
            validate_render_health_url,
            "http://bad\u2003host:9000/health",
            "render health URL is invalid",
        ),
    ],
)
def test_health_endpoint_validator_rejects_all_whitespace_without_echoing_url(
    validator,
    value: str,
    expected_error: str,
) -> None:
    with pytest.raises(ValueError) as error:
        validator(value)

    assert str(error.value) == expected_error
    assert value not in str(error.value)


@pytest.mark.asyncio
async def test_active_probe_service_reports_every_dependency_healthy() -> None:
    calls: list[str] = []
    service = ActiveHealthProbeService(
        _healthy_probes(calls),
        probe_timeout_seconds=0.1,
        request_timeout_seconds=0.5,
    )

    results = await service.probe()

    assert set(results) == set(ACTIVE_PROBE_COMPONENTS)
    assert {result.status for result in results.values()} == {"healthy"}
    assert {result.reason for result in results.values()} == {None}
    assert set(calls) == set(ACTIVE_PROBE_COMPONENTS)


@pytest.mark.asyncio
async def test_active_probe_service_maps_one_safe_failure_without_leaking_exception() -> None:
    sentinel = "postgresql://user:password@secret-host/private"
    probes = _healthy_probes([])

    async def fail() -> None:
        raise RuntimeError(sentinel)

    probes["database"] = fail
    service = ActiveHealthProbeService(probes)

    results = await service.probe()

    assert results["database"].status == "unhealthy"
    assert results["database"].reason == "probe_failed"
    assert sentinel not in repr(results)


@pytest.mark.asyncio
async def test_active_probe_service_preserves_allowlisted_failure_reason() -> None:
    probes = _healthy_probes([])

    async def fail() -> None:
        raise HealthProbeFailure("contract_mismatch")

    probes["openmaic_shared"] = fail
    service = ActiveHealthProbeService(probes)

    results = await service.probe()

    assert results["openmaic_shared"].status == "unhealthy"
    assert results["openmaic_shared"].reason == "contract_mismatch"


@pytest.mark.asyncio
async def test_active_probe_service_times_out_one_dependency() -> None:
    probes = _healthy_probes([])

    async def block() -> None:
        await asyncio.Event().wait()

    probes["object_store"] = block
    service = ActiveHealthProbeService(
        probes,
        probe_timeout_seconds=0.01,
        request_timeout_seconds=0.2,
    )

    results = await service.probe()

    assert results["object_store"].status == "unhealthy"
    assert results["object_store"].reason == "probe_timeout"


@pytest.mark.parametrize(
    ("probe_timeout", "request_timeout", "expected_reason"),
    [
        (0.03, 0.3, "probe_timeout"),
        (1.0, 0.03, "request_timeout"),
    ],
)
@pytest.mark.asyncio
async def test_active_probe_service_deadline_does_not_drain_s3_thread(
    probe_timeout: float,
    request_timeout: float,
    expected_reason: str,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def block() -> None:
        entered.set()
        try:
            release.wait(timeout=1)
        finally:
            finished.set()

    probes = _healthy_probes([])

    async def s3_probe() -> None:
        await run_s3_health_sync(block)

    probes["object_store"] = s3_probe
    service = ActiveHealthProbeService(
        probes,
        probe_timeout_seconds=probe_timeout,
        request_timeout_seconds=request_timeout,
    )
    started_at = asyncio.get_running_loop().time()
    try:
        results = await service.probe()
        elapsed = asyncio.get_running_loop().time() - started_at

        assert entered.is_set()
        assert elapsed < 0.2
        assert results["object_store"].reason == expected_reason
        assert finished.is_set() is False
        assert not [
            child
            for child in asyncio.all_tasks()
            if child is not asyncio.current_task()
            and not child.done()
            and (
                child.get_name() == "object-store-health-call"
                or child.get_name() == "object-store-tenant-health"
                or child.get_name().startswith("teaching-health-probe:")
            )
        ]
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.asyncio
async def test_active_probe_service_propagates_cancellation_and_reaps_children() -> None:
    started = asyncio.Event()
    stopped: set[str] = set()

    async def block(component: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add(component)

    probes = {
        component: lambda component=component: block(component)
        for component in ACTIVE_PROBE_COMPONENTS
    }
    service = ActiveHealthProbeService(
        probes,
        probe_timeout_seconds=60,
        request_timeout_seconds=60,
    )
    task = asyncio.create_task(service.probe())
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped == set(ACTIVE_PROBE_COMPONENTS)
    assert not [
        child
        for child in asyncio.all_tasks()
        if child is not asyncio.current_task()
        and not child.done()
        and child.get_name().startswith("teaching-health-probe:")
    ]


@pytest.mark.asyncio
async def test_database_probe_executes_select_one_on_injected_platform_session() -> None:
    statements: list[str] = []

    class Session:
        async def scalar(self, statement) -> int:
            statements.append(str(statement))
            return 1

    @asynccontextmanager
    async def sessions():
        yield Session()

    await DatabaseHealthProbe(session_factory=sessions)()

    assert statements == ["SELECT 1"]


@pytest.mark.asyncio
async def test_database_probe_fails_closed_when_select_one_is_not_one() -> None:
    class Session:
        async def scalar(self, _statement) -> None:
            return None

    @asynccontextmanager
    async def sessions():
        yield Session()

    with pytest.raises(HealthProbeFailure, match="database_unavailable"):
        await DatabaseHealthProbe(session_factory=sessions)()


class _MigrationRepository:
    def __init__(self, snapshot: MigrationHealthSnapshot) -> None:
        self.snapshot = snapshot

    async def fetch_snapshot(self) -> MigrationHealthSnapshot:
        return self.snapshot


def _current_migrations() -> MigrationHealthSnapshot:
    return MigrationHealthSnapshot(
        platform_revision=TEACHING_SCHEMA_REVISION,
        active_tenants=2,
        current_tenants=2,
        missing_tenants=0,
        outdated_tenants=0,
    )


@pytest.mark.asyncio
async def test_migration_probe_accepts_current_platform_and_all_active_tenants() -> None:
    await MigrationHealthProbe(_MigrationRepository(_current_migrations()))()


@pytest.mark.asyncio
async def test_migration_probe_rejects_platform_revision_mismatch() -> None:
    snapshot = replace(_current_migrations(), platform_revision="older")

    with pytest.raises(HealthProbeFailure, match="platform_revision_mismatch"):
        await MigrationHealthProbe(_MigrationRepository(snapshot))()


@pytest.mark.asyncio
async def test_migration_probe_rejects_missing_active_tenant_schema_state() -> None:
    snapshot = replace(
        _current_migrations(),
        current_tenants=1,
        missing_tenants=1,
    )

    with pytest.raises(HealthProbeFailure, match="tenant_revision_missing"):
        await MigrationHealthProbe(_MigrationRepository(snapshot))()


@pytest.mark.asyncio
async def test_migration_probe_rejects_outdated_active_tenant_schema_state() -> None:
    snapshot = replace(
        _current_migrations(),
        current_tenants=1,
        outdated_tenants=1,
    )

    with pytest.raises(HealthProbeFailure, match="tenant_revision_outdated"):
        await MigrationHealthProbe(_MigrationRepository(snapshot))()


def test_migration_health_query_uses_active_tenants_and_schema_state_revision() -> None:
    sql = str(build_migration_health_statement().compile(compile_kwargs={"literal_binds": True}))

    assert "SELECT version_num FROM platform.alembic_version" in sql
    assert "platform.tenants.status = 'active'" in sql
    assert "LEFT OUTER JOIN platform.tenant_schema_states" in sql
    assert f"tenant_schema_states.revision = '{TEACHING_SCHEMA_REVISION}'" in sql
    assert "tenant_schema_states.status = 'active'" in sql


@pytest.mark.asyncio
async def test_migration_repository_reads_one_database_snapshot_statement() -> None:
    statements: list[str] = []

    class Row:
        platform_revision = TEACHING_SCHEMA_REVISION
        active_tenants = 1
        current_tenants = 1
        missing_tenants = 0
        outdated_tenants = 0

    class Result:
        def one(self) -> Row:
            return Row()

    class Session:
        async def execute(self, statement) -> Result:
            statements.append(str(statement))
            return Result()

        async def scalar(self, _statement):
            raise AssertionError("migration health must use one snapshot statement")

    @asynccontextmanager
    async def sessions():
        yield Session()

    snapshot = await SqlAlchemyMigrationHealthRepository(sessions).fetch_snapshot()

    assert snapshot == replace(_current_migrations(), active_tenants=1, current_tenants=1)
    assert len(statements) == 1


@pytest.mark.asyncio
async def test_local_object_store_health_check_is_read_only(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = tuple(
        sorted((path.relative_to(tmp_path), path.stat().st_mtime_ns) for path in tmp_path.iterdir())
    )

    await LocalClassroomArtifactStore.health_check_root(tmp_path)

    after = tuple(
        sorted((path.relative_to(tmp_path), path.stat().st_mtime_ns) for path in tmp_path.iterdir())
    )
    assert after == before
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.asyncio
async def test_local_object_store_health_check_closes_directory_iterator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    closed = False

    class Entries:
        def __enter__(self):
            return iter(())

        def __exit__(self, *_args):
            nonlocal closed
            closed = True

    monkeypatch.setattr("deeptutor.teaching.object_store.os.scandir", lambda _path: Entries())

    await LocalClassroomArtifactStore.health_check_root(tmp_path)

    assert closed


@pytest.mark.asyncio
async def test_local_object_store_health_check_fails_when_root_is_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ObjectStoreConfigurationError):
        await LocalClassroomArtifactStore.health_check_root(tmp_path / "missing")


@pytest.mark.asyncio
async def test_s3_object_store_health_check_uses_only_bounded_list(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    configs = []
    clients = []

    class Client:
        closed = False

        def list_objects_v2(self, **kwargs):
            calls.append(("list", kwargs))
            return {"Contents": [], "IsTruncated": False}

        def put_object(self, **_kwargs):
            raise AssertionError("health probe must not write objects")

        def delete_object(self, **_kwargs):
            raise AssertionError("health probe must not delete objects")

        def close(self):
            self.closed = True

    def client_factory(*_args, **kwargs):
        configs.append(kwargs["config"])
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr("deeptutor.teaching.object_store.boto3.client", client_factory)
    store = S3ClassroomArtifactStore(
        tenant_id="tenant-a",
        endpoint="http://minio:9000",
        bucket="classrooms",
        region="us-east-1",
        access_key="access",
        secret_key="secret",
    )

    assert "access" not in repr(vars(store))
    assert "secret" not in repr(vars(store))
    await check_s3_object_store_health(
        tenant_id="tenant-a",
        endpoint="http://minio:9000",
        bucket="classrooms",
        region="us-east-1",
        credentials=ResolvedStorageCredentials(
            tenant_id="tenant-a",
            access_key="access",
            secret_key="secret",
        ),
    )

    assert calls == [
        (
            "list",
            {
                "Bucket": "classrooms",
                "Prefix": "tenants/tenant-a/",
                "MaxKeys": 1,
            },
        )
    ]
    assert len(configs) == 2
    ordinary_config, health_config = configs
    assert ordinary_config.connect_timeout != OBJECT_STORE_HEALTH_CONNECT_TIMEOUT_SECONDS
    assert ordinary_config.read_timeout != OBJECT_STORE_HEALTH_READ_TIMEOUT_SECONDS
    assert health_config.connect_timeout == OBJECT_STORE_HEALTH_CONNECT_TIMEOUT_SECONDS
    assert health_config.read_timeout == OBJECT_STORE_HEALTH_READ_TIMEOUT_SECONDS
    assert health_config.retries == {"total_max_attempts": 1}
    assert health_config.proxies == {}
    assert clients[1].closed


@pytest.mark.asyncio
async def test_s3_health_transport_logs_redact_all_private_context(
    monkeypatch,
    caplog,
) -> None:
    tenant_id = "private-s3-tenant"
    endpoint = "http://private-minio.internal:9000"
    bucket = "private-classrooms-bucket"
    access_key = "AKIA_PRIVATE_HEALTH_ACCESS"
    secret_key = "private-health-secret-key"

    class Client:
        def list_objects_v2(self, **kwargs):
            logging.getLogger("botocore.endpoint").debug(
                "endpoint=%s bucket=%s prefix=%s Credential=%s secret=%s",
                endpoint,
                kwargs["Bucket"],
                kwargs["Prefix"],
                access_key,
                secret_key,
            )
            return {"Contents": [], "IsTruncated": False}

        def close(self):
            return None

    monkeypatch.setattr(
        "deeptutor.teaching.object_store.boto3.client",
        lambda *_args, **_kwargs: Client(),
    )
    caplog.set_level(logging.DEBUG)

    await check_s3_object_store_health(
        tenant_id=tenant_id,
        endpoint=endpoint,
        bucket=bucket,
        region="us-east-1",
        credentials=ResolvedStorageCredentials(
            tenant_id=tenant_id,
            access_key=access_key,
            secret_key=secret_key,
        ),
    )

    rendered = " ".join(repr(record.__dict__) for record in caplog.records)
    assert endpoint not in rendered
    assert bucket not in rendered
    assert tenant_id not in rendered
    assert access_key not in rendered
    assert secret_key not in rendered
    credentials_repr = repr(
        ResolvedStorageCredentials(
            tenant_id=tenant_id,
            access_key=access_key,
            secret_key=secret_key,
        )
    )
    assert tenant_id not in credentials_repr
    assert access_key not in credentials_repr
    assert secret_key not in credentials_repr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:password@minio:9000",
        "http://minio:9000/private-bucket",
        "http://minio:9000?tenant=private",
        "http://minio:9000#private",
        "http://minio:9000\\@attacker.invalid",
    ],
)
async def test_s3_health_rejects_unsafe_endpoint_before_client_creation(
    endpoint: str,
    monkeypatch,
) -> None:
    called = False
    access_key = "private-access"
    secret_key = "private-secret"

    def client_factory(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unsafe endpoint must fail before boto client creation")

    monkeypatch.setattr("deeptutor.teaching.object_store.boto3.client", client_factory)

    with pytest.raises(ObjectStoreConfigurationError) as exc_info:
        await check_s3_object_store_health(
            tenant_id="private-tenant",
            endpoint=endpoint,
            bucket="private-bucket",
            region="us-east-1",
            credentials=ResolvedStorageCredentials(
                tenant_id="private-tenant",
                access_key=access_key,
                secret_key=secret_key,
            ),
        )

    assert called is False
    rendered = str(exc_info.value)
    assert endpoint not in rendered
    assert access_key not in rendered
    assert secret_key not in rendered


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:password@minio:9000",
        "http://minio:9000/private-bucket",
        "http://minio:9000?tenant=private",
    ],
)
def test_platform_settings_rejects_unsafe_s3_health_endpoint(
    endpoint: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        PlatformSettings(
            enabled=True,
            database_url=SecretStr("postgresql+asyncpg://app:db@postgres/platform"),
            object_store_mode="s3",
            object_store_endpoint=endpoint,
            object_store_namespace_id="health-namespace",
            object_store_tenant_credentials_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_s3_object_store_health_cancellation_returns_before_thread_finishes(
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class Client:
        closed = False

        def list_objects_v2(self, **_kwargs):
            entered.set()
            release.wait(timeout=1)
            return {"Contents": [], "IsTruncated": False}

        def close(self):
            self.closed = True
            closed.set()

    client = Client()

    monkeypatch.setattr(
        "deeptutor.teaching.object_store.boto3.client",
        lambda *_a, **_k: client,
    )
    task = asyncio.create_task(
        check_s3_object_store_health(
            tenant_id="tenant-a",
            endpoint="http://minio:9000",
            bucket="classrooms",
            region="us-east-1",
            credentials=ResolvedStorageCredentials(
                tenant_id="tenant-a",
                access_key="access",
                secret_key="secret",
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)

    task.cancel()
    started_at = asyncio.get_running_loop().time()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = asyncio.get_running_loop().time() - started_at

        assert elapsed < 0.2
        assert client.closed is False
        assert not [
            child
            for child in asyncio.all_tasks()
            if child is not asyncio.current_task()
            and not child.done()
            and child.get_name() == "object-store-health-call"
        ]
    finally:
        release.set()
        assert await asyncio.to_thread(closed.wait, 1)

    assert client.closed

    reuse_deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < reuse_deadline:
        entered_count = 0
        entered_all = threading.Event()
        release_slots = threading.Event()
        counter_lock = threading.Lock()

        def occupy_slot() -> None:
            nonlocal entered_count
            with counter_lock:
                entered_count += 1
                if entered_count == OBJECT_STORE_HEALTH_THREAD_WORKERS:
                    entered_all.set()
            release_slots.wait(timeout=1)

        reuse_tasks = [
            asyncio.create_task(run_s3_health_sync(occupy_slot))
            for _ in range(OBJECT_STORE_HEALTH_THREAD_WORKERS)
        ]
        try:
            all_slots_admitted = await asyncio.to_thread(entered_all.wait, 0.1)
        finally:
            release_slots.set()
            reuse_results = await asyncio.gather(*reuse_tasks, return_exceptions=True)

        unexpected = [
            result
            for result in reuse_results
            if result is not None and not isinstance(result, ObjectStoreError)
        ]
        assert unexpected == []
        if all_slots_admitted:
            assert entered_count == OBJECT_STORE_HEALTH_THREAD_WORKERS
            assert reuse_results == [None] * OBJECT_STORE_HEALTH_THREAD_WORKERS
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("all S3 health executor slots were not released before the deadline")


@pytest.mark.asyncio
async def test_s3_health_executor_rejects_work_beyond_global_bound() -> None:
    entered_count = 0
    entered_all = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    fifth_called = False

    def block() -> None:
        nonlocal entered_count
        with counter_lock:
            entered_count += 1
            if entered_count == OBJECT_STORE_HEALTH_THREAD_WORKERS:
                entered_all.set()
        release.wait(timeout=1)

    tasks = [
        asyncio.create_task(run_s3_health_sync(block))
        for _ in range(OBJECT_STORE_HEALTH_THREAD_WORKERS)
    ]
    assert await asyncio.to_thread(entered_all.wait, 1)

    def fifth() -> None:
        nonlocal fifth_called
        fifth_called = True

    async def release_later() -> None:
        await asyncio.sleep(0.05)
        release.set()

    releaser = asyncio.create_task(release_later())
    with pytest.raises(ObjectStoreError):
        await run_s3_health_sync(fifth)
    await releaser
    await asyncio.gather(*tasks)

    assert fifth_called is False
    await run_s3_health_sync(lambda: None)


@pytest.mark.asyncio
async def test_s3_health_executor_releases_slot_when_context_setup_fails(
    monkeypatch,
) -> None:
    original_copy_context = object_store_module.copy_context
    setup_calls = 0

    def fail_once():
        nonlocal setup_calls
        setup_calls += 1
        if setup_calls == 1:
            raise RuntimeError("health context setup failed")
        return original_copy_context()

    monkeypatch.setattr(object_store_module, "copy_context", fail_once)
    with pytest.raises(RuntimeError, match="context setup"):
        await run_s3_health_sync(lambda: None)

    entered_count = 0
    entered_all = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()

    def block() -> None:
        nonlocal entered_count
        with counter_lock:
            entered_count += 1
            if entered_count == OBJECT_STORE_HEALTH_THREAD_WORKERS:
                entered_all.set()
        release.wait(timeout=1)

    tasks = [
        asyncio.create_task(run_s3_health_sync(block))
        for _ in range(OBJECT_STORE_HEALTH_THREAD_WORKERS)
    ]
    try:
        assert await asyncio.to_thread(entered_all.wait, 1)
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

    assert results == [None] * OBJECT_STORE_HEALTH_THREAD_WORKERS


@pytest.mark.asyncio
async def test_object_store_probe_maps_adapter_failure_to_fixed_reason(tmp_path: Path) -> None:
    probe = ObjectStoreHealthProbe(
        PlatformSettings(enabled=False, object_store_mode="local"),
        local_root=tmp_path / "missing",
    )

    with pytest.raises(HealthProbeFailure, match="object_store_unavailable"):
        await probe()


class _StorageHealthRepository:
    def __init__(self, inventory: StorageHealthInventory) -> None:
        self.inventory = inventory

    async def fetch_health_inventory(self) -> StorageHealthInventory:
        return self.inventory


class _StorageCredentialResolver:
    def __init__(self) -> None:
        self.tenants: list[str] = []

    def resolve(
        self,
        record: TenantStorageCredentialRecord,
        *,
        tenant_id: str,
    ) -> ResolvedStorageCredentials:
        self.tenants.append(tenant_id)
        assert record.tenant_id == tenant_id
        return ResolvedStorageCredentials(
            tenant_id=tenant_id,
            access_key=f"access-{tenant_id}",
            secret_key=f"secret-{tenant_id}",
        )


def _s3_health_settings(tmp_path: Path) -> PlatformSettings:
    return PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://app:db@postgres/platform"),
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_namespace_id="health-namespace",
        object_store_bucket="classrooms",
        object_store_region="us-east-1",
        object_store_tenant_credentials_dir=tmp_path,
    )


def _storage_record(tenant_id: str) -> TenantStorageCredentialRecord:
    return TenantStorageCredentialRecord(
        tenant_id=tenant_id,
        secret_ref=f"{tenant_id}/object-store",
        access_key_fingerprint="a" * 64,
        status="active",
    )


@pytest.mark.asyncio
async def test_s3_probe_zero_active_tenants_is_not_applicable_healthy(
    tmp_path: Path,
) -> None:
    resolver = _StorageCredentialResolver()
    checks: list[str] = []

    async def check(**kwargs) -> None:
        checks.append(kwargs["tenant_id"])

    probe = ObjectStoreHealthProbe(
        _s3_health_settings(tmp_path),
        s3_inventory_repository=_StorageHealthRepository(
            StorageHealthInventory(
                active_tenants=0,
                credentials=(),
                unavailable_tenants=0,
            )
        ),
        s3_credential_resolver=resolver,
        s3_health_check=check,
    )

    await probe()

    assert resolver.tenants == []
    assert checks == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inventory",
    [
        StorageHealthInventory(-1, (), 0),
        StorageHealthInventory(0, (_storage_record("tenant-a"),), 0),
        StorageHealthInventory(0, (), 1),
        StorageHealthInventory(
            2,
            (_storage_record("tenant-a"), _storage_record("tenant-a")),
            0,
        ),
        StorageHealthInventory(1, (_storage_record("tenant-a"),), 1),
    ],
)
async def test_s3_probe_fails_closed_for_malformed_inventory(
    inventory: StorageHealthInventory,
    tmp_path: Path,
) -> None:
    probe = ObjectStoreHealthProbe(
        _s3_health_settings(tmp_path),
        s3_inventory_repository=_StorageHealthRepository(inventory),
        s3_credential_resolver=_StorageCredentialResolver(),
        s3_health_check=lambda **_kwargs: None,
    )

    with pytest.raises(HealthProbeFailure, match="object_store_unavailable"):
        await probe()


@pytest.mark.asyncio
async def test_s3_probe_resolves_every_active_tenant_just_in_time(
    tmp_path: Path,
) -> None:
    records = (_storage_record("tenant-a"), _storage_record("tenant-b"))
    resolver = _StorageCredentialResolver()
    checks: list[tuple[str, str]] = []

    async def check(**kwargs) -> None:
        checks.append((kwargs["tenant_id"], kwargs["credentials"].access_key))

    probe = ObjectStoreHealthProbe(
        _s3_health_settings(tmp_path),
        s3_inventory_repository=_StorageHealthRepository(
            StorageHealthInventory(
                active_tenants=2,
                credentials=records,
                unavailable_tenants=0,
            )
        ),
        s3_credential_resolver=resolver,
        s3_health_check=check,
    )

    await probe()

    assert set(resolver.tenants) == {"tenant-a", "tenant-b"}
    assert set(checks) == {
        ("tenant-a", "access-tenant-a"),
        ("tenant-b", "access-tenant-b"),
    }


@pytest.mark.asyncio
async def test_s3_probe_fails_closed_for_missing_active_tenant_credential(
    tmp_path: Path,
) -> None:
    resolver = _StorageCredentialResolver()
    probe = ObjectStoreHealthProbe(
        _s3_health_settings(tmp_path),
        s3_inventory_repository=_StorageHealthRepository(
            StorageHealthInventory(
                active_tenants=2,
                credentials=(_storage_record("tenant-a"),),
                unavailable_tenants=1,
            )
        ),
        s3_credential_resolver=resolver,
        s3_health_check=lambda **_kwargs: None,
    )

    with pytest.raises(HealthProbeFailure, match="object_store_unavailable"):
        await probe()

    assert resolver.tenants == []


@pytest.mark.asyncio
async def test_s3_probe_rejects_unsafe_endpoint_before_secret_resolution(
    tmp_path: Path,
) -> None:
    resolver = _StorageCredentialResolver()
    checks: list[str] = []

    async def check(**kwargs) -> None:
        checks.append(kwargs["tenant_id"])

    unsafe_settings = _s3_health_settings(tmp_path).model_copy(
        update={"object_store_endpoint": "http://user:password@attacker.invalid"}
    )
    probe = ObjectStoreHealthProbe(
        unsafe_settings,
        s3_inventory_repository=_StorageHealthRepository(
            StorageHealthInventory(
                active_tenants=1,
                credentials=(_storage_record("private-tenant"),),
                unavailable_tenants=0,
            )
        ),
        s3_credential_resolver=resolver,
        s3_health_check=check,
    )

    with pytest.raises(HealthProbeFailure, match="object_store_unavailable"):
        await probe()

    assert resolver.tenants == []
    assert checks == []


@pytest.mark.asyncio
async def test_s3_probe_bounds_tenant_concurrency_and_reaps_on_cancel(
    tmp_path: Path,
) -> None:
    records = tuple(_storage_record(f"tenant-{index}") for index in range(4))
    active = 0
    maximum = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def check(**_kwargs) -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            entered.set()
        try:
            await release.wait()
        finally:
            active -= 1

    probe = ObjectStoreHealthProbe(
        _s3_health_settings(tmp_path),
        s3_inventory_repository=_StorageHealthRepository(
            StorageHealthInventory(
                active_tenants=4,
                credentials=records,
                unavailable_tenants=0,
            )
        ),
        s3_credential_resolver=_StorageCredentialResolver(),
        s3_health_check=check,
        s3_concurrency=2,
    )
    task = asyncio.create_task(probe())
    await entered.wait()
    await asyncio.sleep(0)

    assert maximum == 2
    owned_before_cancel = [
        child
        for child in asyncio.all_tasks()
        if not child.done() and child.get_name() == "object-store-tenant-health"
    ]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(owned_before_cancel) == 2
    assert active == 0
    assert not [
        child
        for child in asyncio.all_tasks()
        if child is not asyncio.current_task()
        and not child.done()
        and child.get_name() == "object-store-tenant-health"
    ]


def test_storage_health_inventory_query_is_active_and_secret_value_free() -> None:
    sql = str(
        build_storage_health_inventory_statement().compile(compile_kwargs={"literal_binds": True})
    )

    assert "tenants.status = 'active'" in sql
    assert sql.index("JOIN platform.tenant_storage_credentials") < sql.index(
        "JOIN platform.tenant_storage_states"
    )
    assert "tenant_storage_credentials.status = 'active'" in sql
    assert "JOIN platform.tenant_storage_states" in sql
    assert "tenant_storage_states.mode = 's3'" in sql
    assert "tenant_storage_states.status = 'active'" in sql
    assert (
        "tenant_storage_states.credential_secret_ref = "
        "platform.tenant_storage_credentials.secret_ref" in sql
    )
    assert (
        "tenant_storage_states.credential_fingerprint = "
        "platform.tenant_storage_credentials.access_key_fingerprint" in sql
    )
    assert "tenant_storage_credentials.secret_ref" in sql
    assert "tenant_storage_credentials.access_key_fingerprint" in sql
    assert "aws_access_key_id" not in sql
    assert "secret_key" not in sql


def _route(route_id: str, *, mode: str = "dedicated") -> DataPlaneRouteRecord:
    tenant_id = None if mode == "shared" else f"tenant-{route_id}"
    return DataPlaneRouteRecord(
        route_id=route_id,
        tenant_id=tenant_id,
        owner_key="shared" if mode == "shared" else tenant_id or "",
        mode=mode,
        base_url=f"http://{route_id}.internal:3000",
        worker_pool=f"worker-{route_id}",
        queue_name=f"queue-{route_id}",
        provider_profile_id=f"provider-{route_id}",
        status="active",
        health_status="unhealthy",
    )


class _DataPlaneHealthRepository:
    def __init__(
        self,
        *,
        shared: DataPlaneRouteRecord | None = None,
        dedicated: DedicatedDataPlaneHealthInventory | None = None,
    ) -> None:
        self.shared = shared
        self.dedicated = dedicated or DedicatedDataPlaneHealthInventory(
            active_tenants=0,
            routes=(),
            unavailable_tenants=0,
        )

    async def resolve_shared_health_route(self) -> DataPlaneRouteRecord | None:
        return self.shared

    async def resolve_dedicated_health_inventory(self) -> DedicatedDataPlaneHealthInventory:
        return self.dedicated


class _CompatibleClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def assert_compatible(self):
        if self.error is not None:
            raise self.error


class _HealthClientFactory:
    def __init__(self, errors: dict[str, Exception] | None = None) -> None:
        self.errors = errors or {}

    async def create(self, route: DataPlaneRouteRecord) -> _CompatibleClient:
        return _CompatibleClient(self.errors.get(route.route_id))


@pytest.mark.asyncio
async def test_shared_openmaic_probe_uses_active_contract_not_cached_health() -> None:
    probes = OpenMAICDataPlaneHealthProbes(
        repository=_DataPlaneHealthRepository(shared=_route("shared", mode="shared")),
        client_factory=_HealthClientFactory(),
    )

    await probes.probe_shared()


@pytest.mark.asyncio
async def test_shared_openmaic_probe_maps_contract_mismatch() -> None:
    probes = OpenMAICDataPlaneHealthProbes(
        repository=_DataPlaneHealthRepository(shared=_route("shared", mode="shared")),
        client_factory=_HealthClientFactory({"shared": IncompatibleOpenMAIC()}),
    )

    with pytest.raises(HealthProbeFailure, match="contract_mismatch"):
        await probes.probe_shared()


@pytest.mark.asyncio
async def test_render_probe_uses_explicit_health_url_and_maps_unhealthy() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        probe = RenderHealthProbe(
            health_url="http://openmaic-render:9000/health",
            http_client=client,
        )
        with pytest.raises(HealthProbeFailure, match="render_unhealthy"):
            await probe()

    assert [str(request.url) for request in requests] == ["http://openmaic-render:9000/health"]


@pytest.mark.asyncio
async def test_dedicated_probe_zero_active_tenants_is_not_applicable_healthy() -> None:
    probes = OpenMAICDataPlaneHealthProbes(
        repository=_DataPlaneHealthRepository(),
        client_factory=_HealthClientFactory(),
    )

    await probes.probe_dedicated()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inventory",
    [
        DedicatedDataPlaneHealthInventory(-1, (), 0),
        DedicatedDataPlaneHealthInventory(0, (_route("unexpected"),), 0),
        DedicatedDataPlaneHealthInventory(0, (), 1),
        DedicatedDataPlaneHealthInventory(
            2,
            (_route("duplicate"), _route("duplicate")),
            0,
        ),
        DedicatedDataPlaneHealthInventory(1, (_route("route"),), 1),
        DedicatedDataPlaneHealthInventory(1, (_route("shared", mode="shared"),), 0),
        DedicatedDataPlaneHealthInventory(
            1,
            (replace(_route("wrong-owner"), owner_key="another-tenant"),),
            0,
        ),
        DedicatedDataPlaneHealthInventory(
            1,
            (replace(_route("disabled"), status="disabled"),),
            0,
        ),
    ],
)
async def test_dedicated_probe_fails_closed_for_malformed_inventory(
    inventory: DedicatedDataPlaneHealthInventory,
) -> None:
    probes = OpenMAICDataPlaneHealthProbes(
        repository=_DataPlaneHealthRepository(dedicated=inventory),
        client_factory=_HealthClientFactory(),
    )

    with pytest.raises(HealthProbeFailure, match="route_unavailable"):
        await probes.probe_dedicated()


@pytest.mark.asyncio
async def test_dedicated_probe_active_tenant_without_valid_route_fails_closed() -> None:
    inventory = DedicatedDataPlaneHealthInventory(
        active_tenants=1,
        routes=(),
        unavailable_tenants=1,
    )
    probes = OpenMAICDataPlaneHealthProbes(
        repository=_DataPlaneHealthRepository(dedicated=inventory),
        client_factory=_HealthClientFactory(),
    )

    with pytest.raises(HealthProbeFailure, match="route_unavailable"):
        await probes.probe_dedicated()


@pytest.mark.asyncio
async def test_dedicated_probe_all_routes_compatible_with_bounded_concurrency() -> None:
    routes = tuple(_route(f"dedicated-{index}") for index in range(5))
    inventory = DedicatedDataPlaneHealthInventory(
        active_tenants=len(routes),
        routes=routes,
        unavailable_tenants=0,
    )
    active = 0
    maximum = 0

    class Client:
        async def assert_compatible(self):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

    class Factory:
        async def create(self, _route):
            return Client()

    probes = OpenMAICDataPlaneHealthProbes(
        repository=_DataPlaneHealthRepository(dedicated=inventory),
        client_factory=Factory(),
        dedicated_concurrency=2,
    )

    await probes.probe_dedicated()

    assert maximum == 2


@pytest.mark.asyncio
async def test_dedicated_probe_cancellation_reaps_owned_route_tasks() -> None:
    routes = tuple(_route(f"route-{index}") for index in range(10))
    inventory = DedicatedDataPlaneHealthInventory(
        active_tenants=len(routes),
        routes=routes,
        unavailable_tenants=0,
    )
    active = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    class Client:
        async def assert_compatible(self) -> None:
            nonlocal active
            active += 1
            if active == 2:
                entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

    class Factory:
        async def create(self, _route: DataPlaneRouteRecord) -> Client:
            return Client()

    probes = OpenMAICDataPlaneHealthProbes(
        repository=_DataPlaneHealthRepository(dedicated=inventory),
        client_factory=Factory(),
        dedicated_concurrency=2,
    )
    task = asyncio.create_task(probes.probe_dedicated())
    await entered.wait()
    owned_before_cancel = [
        child
        for child in asyncio.all_tasks()
        if not child.done() and child.get_name() == "dedicated-data-plane-health"
    ]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(owned_before_cancel) == 2
    assert active == 0
    assert not [
        child
        for child in asyncio.all_tasks()
        if child is not asyncio.current_task()
        and not child.done()
        and child.get_name() == "dedicated-data-plane-health"
    ]


@pytest.mark.asyncio
async def test_dedicated_probe_one_contract_mismatch_degrades_aggregate() -> None:
    routes = (_route("good"), _route("bad"))
    inventory = DedicatedDataPlaneHealthInventory(
        active_tenants=2,
        routes=routes,
        unavailable_tenants=0,
    )
    probes = OpenMAICDataPlaneHealthProbes(
        repository=_DataPlaneHealthRepository(dedicated=inventory),
        client_factory=_HealthClientFactory({"bad": IncompatibleOpenMAIC()}),
    )

    with pytest.raises(HealthProbeFailure, match="contract_mismatch"):
        await probes.probe_dedicated()


@pytest.mark.asyncio
async def test_credential_free_health_client_redacts_endpoint_at_info(caplog) -> None:
    payload = {
        "service": "openmaic",
        "upstreamCommit": EXPECTED_UPSTREAM_COMMIT,
        "appVersion": EXPECTED_APP_VERSION,
        "contractVersions": [SUPPORTED_CONTRACT_VERSION],
        "capabilities": sorted(REQUIRED_CAPABILITIES),
        "exportFormats": sorted(REQUIRED_EXPORT_FORMATS),
    }
    endpoint = "http://private-route.internal:3000"
    tenant = "private-tenant"
    route = "private-route"
    ordinary_message = "ordinary-non-health-http-log"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        logging.getLogger("httpcore.connection").debug(
            "connect_tcp host=%s",
            endpoint,
            extra={
                "health_url": endpoint,
                "tenant_id": tenant,
                "route_id": route,
                "provider_secret": "private-provider-secret",
            },
        )
        return httpx.Response(200, json=payload)

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenMAICContractHealthClient(
            http_client,
            base_url=endpoint,
        )
        health_task = asyncio.create_task(client.assert_compatible())
        await entered.wait()
        logging.getLogger("httpx").info(ordinary_message)
        release.set()
        await health_task

    rendered = " ".join(repr(record.__dict__) for record in caplog.records)
    assert ordinary_message in rendered
    assert endpoint not in rendered
    assert tenant not in rendered
    assert route not in rendered


def test_health_json_logging_cannot_reintroduce_private_context() -> None:
    endpoint = "http://private-plane.internal:3000"
    tenant = "private-tenant"
    route = "private-route"
    secret = "private-provider-secret"

    with bind_log_context(tenant_id=tenant, route_id=route):
        with redact_health_transport_logs():
            record = logging.getLogger("httpx").makeRecord(
                "httpx",
                logging.INFO,
                __file__,
                1,
                "request endpoint=%s secret=%s",
                (endpoint, secret),
                None,
                extra={"health_url": endpoint, "provider_secret": secret},
            )
        assert ContextFilter().filter(record)
        record.log_context = {"tenant_id": tenant, "route_id": route}
        private_payload = json.loads(JsonlFormatter().format(record))

    with bind_log_context(tenant_id="ordinary-tenant"):
        ordinary = logging.getLogger("ordinary").makeRecord(
            "ordinary",
            logging.INFO,
            __file__,
            1,
            "ordinary-log",
            (),
            None,
        )
        assert ContextFilter().filter(ordinary)
        ordinary_payload = json.loads(JsonlFormatter().format(ordinary))

    rendered = json.dumps(private_payload)
    assert endpoint not in rendered
    assert tenant not in rendered
    assert route not in rendered
    assert secret not in rendered
    assert private_payload["context"] == {}
    assert ordinary_payload["context"] == {"tenant_id": "ordinary-tenant"}


@pytest.mark.asyncio
async def test_credential_free_health_client_stops_at_response_size_limit() -> None:
    class Stream(httpx.AsyncByteStream):
        consumed = 0

        async def __aiter__(self):
            for chunk in (
                b"x" * MAX_HEALTH_RESPONSE_BYTES,
                b"y",
                b"must-not-be-read",
            ):
                self.consumed += 1
                yield chunk

    stream = Stream()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenMAICContractHealthClient(
            http_client,
            base_url="http://dedicated.internal:3000",
        )
        with pytest.raises(InvalidOpenMAICResponse):
            await client.health()

    assert stream.consumed == 2


@pytest.mark.parametrize(
    "base_url",
    [
        "http://:9000",
        "http://private.internal:not-a-port",
        " http://private.internal:3000",
        "http://private.internal:3000\\@attacker.invalid",
        "http://bad host:3000",
        "http://bad\u00a0host:3000",
    ],
)
def test_credential_free_health_client_rejects_unsafe_base_url_before_http(
    base_url: str,
) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None))
    try:
        with pytest.raises(ValueError) as error:
            OpenMAICContractHealthClient(client, base_url=base_url)
        assert str(error.value) == "OpenMAIC base URL is invalid"
        assert base_url not in str(error.value)
    finally:
        asyncio.run(client.aclose())


@pytest.mark.asyncio
async def test_render_probe_does_not_buffer_health_response_body() -> None:
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("render health must not consume the response body")
            yield b"unreachable"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await RenderHealthProbe(
            health_url="http://openmaic-render:9000/health",
            http_client=client,
        )()


@pytest.mark.asyncio
async def test_render_health_transport_logs_redact_private_endpoint(caplog) -> None:
    endpoint = "http://private-render.internal:9000/health"

    async def handler(_request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug("render endpoint=%s", endpoint)
        return httpx.Response(200, content=b"ignored-private-body")

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await RenderHealthProbe(health_url=endpoint, http_client=client)()

    rendered = " ".join(repr(record.__dict__) for record in caplog.records)
    assert endpoint not in rendered


@pytest.mark.parametrize(
    "health_url",
    [
        "http://user:password@openmaic-render:9000/health",
        "http://openmaic-render:9000/not-health",
        "http://openmaic-render:9000/health?tenant=private",
    ],
)
def test_platform_settings_rejects_unsafe_render_health_url(
    health_url: str,
) -> None:
    with pytest.raises(ValueError):
        PlatformSettings(openmaic_render_health_url=health_url)


def test_platform_settings_accepts_controlled_render_health_url() -> None:
    settings = PlatformSettings(openmaic_render_health_url="http://openmaic-render:9000/health")

    assert settings.openmaic_render_health_url == "http://openmaic-render:9000/health"


def test_data_plane_health_queries_require_active_provider_binding_without_secrets() -> None:
    shared_sql = str(
        build_shared_health_route_statement().compile(compile_kwargs={"literal_binds": True})
    )
    dedicated_sql = str(
        build_dedicated_health_inventory_statement().compile(compile_kwargs={"literal_binds": True})
    )

    assert "data_plane_routes.status = 'active'" in shared_sql
    assert "health_status = 'healthy'" not in shared_sql
    assert "JOIN platform.provider_profiles" in shared_sql
    assert "provider_profiles.id = platform.data_plane_routes.provider_profile_id" in shared_sql
    assert "provider_profiles.scope = platform.data_plane_routes.mode" in shared_sql
    assert "provider_profiles.owner_key = platform.data_plane_routes.owner_key" in shared_sql
    assert "provider_profiles.status = 'active'" in shared_sql
    assert "secret_ref" not in shared_sql
    assert "tenants.status = 'active'" in dedicated_sql
    assert "tenants.data_plane_mode = 'dedicated'" in dedicated_sql
    assert "health_status = 'healthy'" not in dedicated_sql
    assert "JOIN platform.provider_profiles" in dedicated_sql
    assert "provider_profiles.id = platform.data_plane_routes.provider_profile_id" in dedicated_sql
    assert "provider_profiles.scope = platform.data_plane_routes.mode" in dedicated_sql
    assert "provider_profiles.tenant_id = platform.tenants.id" in dedicated_sql
    assert "provider_profiles.owner_key = platform.tenants.id" in dedicated_sql
    assert "provider_profiles.status = 'active'" in dedicated_sql
    assert "secret_ref" not in dedicated_sql


def _route_model(route_id: str, tenant_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=route_id,
        tenant_id=tenant_id,
        owner_key=tenant_id,
        mode="dedicated",
        base_url=f"http://{route_id}.internal:3000",
        worker_pool=f"worker-{route_id}",
        queue_name=f"queue-{route_id}",
        provider_profile_id=f"profile-{route_id}",
        status="active",
        health_status="unhealthy",
    )


@pytest.mark.asyncio
async def test_data_plane_repository_marks_missing_or_invalid_profile_unavailable(
    monkeypatch,
) -> None:
    valid_tenant = SimpleNamespace(id="tenant-valid")
    valid_route = _route_model("valid", valid_tenant.id)
    missing_route_tenant = SimpleNamespace(id="tenant-missing-route")
    missing_profile_tenant = SimpleNamespace(id="tenant-missing-profile")
    missing_profile_route = _route_model("missing-profile", missing_profile_tenant.id)
    misbound_tenant = SimpleNamespace(id="tenant-misbound")
    misbound_route = _route_model("misbound", misbound_tenant.id)
    misbound_route.owner_key = "another-tenant"
    rows = [
        (valid_tenant, valid_route, valid_route.provider_profile_id),
        (missing_route_tenant, None, None),
        (missing_profile_tenant, missing_profile_route, None),
        (misbound_tenant, misbound_route, misbound_route.provider_profile_id),
    ]

    class Result:
        def all(self):
            return rows

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def sessions():
        yield Session()

    monkeypatch.setattr(
        "deeptutor.teaching.repositories.data_planes.platform_session",
        sessions,
    )

    inventory = await SqlAlchemyDataPlaneRepository().resolve_dedicated_health_inventory()

    assert inventory.active_tenants == 4
    assert [route.route_id for route in inventory.routes] == ["valid"]
    assert inventory.unavailable_tenants == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_state", ["missing", "disabled", "misbound"])
async def test_shared_health_repository_returns_none_without_active_bound_profile(
    profile_state: str,
    monkeypatch,
) -> None:
    class Session:
        async def scalar(self, statement):
            sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
            assert "provider_profiles.status = 'active'" in sql
            assert "provider_profiles.scope = platform.data_plane_routes.mode" in sql
            assert profile_state in {"missing", "disabled", "misbound"}
            return None

    @asynccontextmanager
    async def sessions():
        yield Session()

    monkeypatch.setattr(
        "deeptutor.teaching.repositories.data_planes.platform_session",
        sessions,
    )

    assert await SqlAlchemyDataPlaneRepository().resolve_shared_health_route() is None


@pytest.mark.asyncio
async def test_storage_repository_counts_missing_inactive_and_misbound_state(
    monkeypatch,
) -> None:
    rows = [
        SimpleNamespace(
            tenant_id="tenant-valid",
            secret_ref="tenant-valid/object-store",
            access_key_fingerprint="a" * 64,
            credential_status="active",
            storage_state_tenant_id="tenant-valid",
        ),
        SimpleNamespace(
            tenant_id="tenant-missing",
            secret_ref=None,
            access_key_fingerprint=None,
            credential_status=None,
            storage_state_tenant_id=None,
        ),
        SimpleNamespace(
            tenant_id="tenant-pending",
            secret_ref="tenant-pending/object-store",
            access_key_fingerprint="b" * 64,
            credential_status="active",
            storage_state_tenant_id=None,
        ),
        SimpleNamespace(
            tenant_id="tenant-misbound",
            secret_ref="tenant-misbound/object-store",
            access_key_fingerprint="c" * 64,
            credential_status="active",
            storage_state_tenant_id="another-tenant",
        ),
    ]

    class Result:
        def all(self):
            return rows

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def sessions():
        yield Session()

    monkeypatch.setattr(
        "deeptutor.teaching.storage_credentials.platform_session",
        sessions,
    )

    inventory = await SqlAlchemyStorageCredentialRepository().fetch_health_inventory()

    assert inventory.active_tenants == 4
    assert [record.tenant_id for record in inventory.credentials] == ["tenant-valid"]
    assert inventory.unavailable_tenants == 3


@pytest.mark.asyncio
async def test_runtime_openmaic_health_factory_uses_credential_free_health_boundary(
    tmp_path: Path,
    caplog,
) -> None:
    secret_path = (tmp_path / "must-not-be-read").resolve()
    secret_value = "service-health-secret"
    payload = {
        "service": "openmaic",
        "upstreamCommit": EXPECTED_UPSTREAM_COMMIT,
        "appVersion": EXPECTED_APP_VERSION,
        "contractVersions": [SUPPORTED_CONTRACT_VERSION],
        "capabilities": sorted(REQUIRED_CAPABILITIES),
        "exportFormats": sorted(REQUIRED_EXPORT_FORMATS),
    }

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://app:db@postgres/platform"),
        openmaic_service_secret_file=secret_path,
    )
    route = _route("dedicated-health")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        factory = RuntimeOpenMAICHealthClientFactory(settings, http_client)
        client = await factory.create(route)
        await client.assert_compatible()

    assert len(requests) == 1
    identity_headers = {
        name.lower(): value
        for name, value in requests[0].headers.items()
        if name.lower().startswith("x-yfeistai-") or name.lower() == "authorization"
    }
    assert identity_headers == {}
    rendered = " ".join(repr(record.__dict__) for record in caplog.records)
    assert route.base_url not in rendered
    assert route.route_id not in rendered
    assert route.tenant_id not in rendered
    assert secret_value not in rendered
