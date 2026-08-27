from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy.dialects import postgresql

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.models import TenantProvisioningJob
from deeptutor.teaching.provisioning_worker import (
    DEFAULT_POLICY_PAYLOAD,
    DEFAULT_POLICY_VERSION,
    TENANT_SCHEMA_REVISION,
    ProvisioningClaim,
    ProvisioningStepError,
    ProvisioningWorker,
    SchemaProvisioningResult,
    StorageProvisioningResult,
    TenantPolicyProvisioningResult,
    build_default_policy_result,
    build_storage_provisioner,
    retry_backoff_seconds,
)
from deeptutor.teaching.repositories.provisioning import (
    build_claim_statement,
    build_schema_upgrade_candidate_statement,
    schema_upgrade_job_id,
)


class FakeProvisioningRepository:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.claim = ProvisioningClaim(
            tenant_id="tenant-a",
            job_id="job-a",
            attempt_count=0,
            lease_owner="worker-a",
            lease_token="lease-a",
        )
        self.failures: list[tuple[str, str, bool, int]] = []

    async def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> ProvisioningClaim | None:
        self.trace.append("claim")
        claim, self.claim = self.claim, None
        return claim

    async def record_schema_ready(
        self,
        claim: ProvisioningClaim,
        result: SchemaProvisioningResult,
    ) -> bool:
        self.trace.append(f"persist:schema:{result.revision}")
        return True

    async def heartbeat(
        self,
        claim: ProvisioningClaim,
        *,
        lease_seconds: int,
    ) -> bool:
        return True

    async def finalize_provisioning(
        self,
        claim: ProvisioningClaim,
        storage: StorageProvisioningResult,
        policy: TenantPolicyProvisioningResult,
    ) -> bool:
        self.trace.append(f"finalize:{storage.mode}:{policy.policy_version}:activation")
        return True

    async def record_failure(
        self,
        claim: ProvisioningClaim,
        *,
        category: str,
        code: str,
        retryable: bool,
        backoff_seconds: int,
    ) -> bool:
        self.trace.append("failure")
        self.failures.append((category, code, retryable, backoff_seconds))
        return True


@dataclass
class FakeSchemaProvisioner:
    trace: list[str]
    failure: Exception | None = None

    async def provision(self, tenant_id: str) -> SchemaProvisioningResult:
        self.trace.append("schema")
        if self.failure is not None:
            raise self.failure
        return SchemaProvisioningResult(
            schema_name="tenant_9e4714402f06d0f7",
            revision="20260730_0005",
        )


@dataclass
class FakeStorageProvisioner:
    trace: list[str]
    failure: Exception | None = None

    async def provision(self, tenant_id: str) -> StorageProvisioningResult:
        self.trace.append("storage")
        if self.failure is not None:
            raise self.failure
        return StorageProvisioningResult.local(tenant_id)


@dataclass
class FakePolicyProvisioner:
    trace: list[str]
    failure: Exception | None = None

    async def provision(self, tenant_id: str) -> TenantPolicyProvisioningResult:
        self.trace.append("policy")
        if self.failure is not None:
            raise self.failure
        return build_default_policy_result()


def _worker(
    repository: FakeProvisioningRepository,
    trace: list[str],
    *,
    schema_failure: Exception | None = None,
    storage_failure: Exception | None = None,
    policy_failure: Exception | None = None,
) -> ProvisioningWorker:
    return ProvisioningWorker(
        enabled=True,
        worker_id="worker-a",
        repository=repository,
        schema_provisioner=FakeSchemaProvisioner(trace, schema_failure),
        storage_provisioner=FakeStorageProvisioner(trace, storage_failure),
        policy_provisioner=FakePolicyProvisioner(trace, policy_failure),
        lease_seconds=30,
    )


def test_run_once_finalizes_storage_policy_and_activation_in_one_repository_call() -> None:
    trace: list[str] = []
    repository = FakeProvisioningRepository(trace)

    handled = asyncio.run(_worker(repository, trace).run_once())

    assert handled is True
    assert trace == [
        "claim",
        "schema",
        "persist:schema:20260730_0005",
        "storage",
        "policy",
        f"finalize:local:{DEFAULT_POLICY_VERSION}:activation",
    ]
    assert repository.failures == []


def test_transient_finalization_database_failure_is_retryable() -> None:
    trace: list[str] = []

    class FailingFinalizeRepository(FakeProvisioningRepository):
        async def finalize_provisioning(
            self,
            claim: ProvisioningClaim,
            storage: StorageProvisioningResult,
            policy: TenantPolicyProvisioningResult,
        ) -> bool:
            raise sqlalchemy_exc.OperationalError(
                "COMMIT",
                {},
                RuntimeError("deterministic database outage"),
            )

    repository = FailingFinalizeRepository(trace)

    handled = asyncio.run(_worker(repository, trace).run_once())

    assert handled is True
    assert trace == [
        "claim",
        "schema",
        "persist:schema:20260730_0005",
        "storage",
        "policy",
        "failure",
    ]
    assert repository.failures == [
        ("infrastructure", "temporarily_unavailable", True, retry_backoff_seconds(0)),
    ]


def test_run_once_does_not_retry_connection_invalidated_programming_error() -> None:
    trace: list[str] = []

    class BrokenFinalizeRepository(FakeProvisioningRepository):
        async def finalize_provisioning(
            self,
            claim: ProvisioningClaim,
            storage: StorageProvisioningResult,
            policy: TenantPolicyProvisioningResult,
        ) -> bool:
            raise sqlalchemy_exc.ProgrammingError(
                "COMMIT",
                {},
                RuntimeError("deterministic query defect"),
                connection_invalidated=True,
            )

    repository = BrokenFinalizeRepository(trace)

    with pytest.raises(sqlalchemy_exc.ProgrammingError):
        asyncio.run(_worker(repository, trace).run_once())

    assert "failure" not in trace
    assert repository.failures == []


def test_poll_survives_finalize_and_failure_recording_outage_then_reclaims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching import provisioning_worker as worker_module

    trace: list[str] = []
    reclaimed = asyncio.Event()

    class DoubleOutageRepository(FakeProvisioningRepository):
        def __init__(self) -> None:
            super().__init__(trace)
            self.claim_count = 0
            self.backoff_seen = False

        async def claim_next(
            self,
            worker_id: str,
            *,
            lease_seconds: int,
        ) -> ProvisioningClaim | None:
            trace.append(f"claim:{self.claim_count}")
            if self.claim_count == 0:
                self.claim_count += 1
                return ProvisioningClaim(
                    tenant_id="tenant-a",
                    job_id="job-a",
                    attempt_count=0,
                    lease_owner=worker_id,
                    lease_token="lease-a",
                )
            if self.claim_count == 1 and self.backoff_seen:
                self.claim_count += 1
                return ProvisioningClaim(
                    tenant_id="tenant-a",
                    job_id="job-a",
                    attempt_count=1,
                    lease_owner=worker_id,
                    lease_token="lease-b",
                )
            return None

        async def finalize_provisioning(
            self,
            claim: ProvisioningClaim,
            storage: StorageProvisioningResult,
            policy: TenantPolicyProvisioningResult,
        ) -> bool:
            trace.append(f"finalize:{claim.attempt_count}")
            if claim.attempt_count == 0:
                raise sqlalchemy_exc.OperationalError(
                    "COMMIT",
                    {},
                    RuntimeError("deterministic finalization outage"),
                )
            reclaimed.set()
            return True

        async def record_failure(
            self,
            claim: ProvisioningClaim,
            *,
            category: str,
            code: str,
            retryable: bool,
            backoff_seconds: int,
        ) -> bool:
            trace.append(f"record-failure:{category}:{code}:{retryable}")
            assert category == "infrastructure"
            assert code == "temporarily_unavailable"
            assert retryable is True
            raise sqlalchemy_exc.OperationalError(
                "UPDATE",
                {},
                RuntimeError("deterministic failure-recording outage"),
            )

    repository = DoubleOutageRepository()
    worker = _worker(repository, trace)
    real_sleep = asyncio.sleep

    async def record_backoff(delay: float) -> None:
        trace.append(f"sleep:{delay}")
        repository.backoff_seen = True
        await real_sleep(0)

    monkeypatch.setattr(worker_module.asyncio, "sleep", record_backoff)

    async def exercise() -> None:
        poll_task = asyncio.create_task(worker.poll(poll_interval_seconds=0.25))
        reclaimed_task = asyncio.create_task(reclaimed.wait())
        try:
            done, _pending = await asyncio.wait(
                {poll_task, reclaimed_task},
                timeout=1,
                return_when=asyncio.FIRST_COMPLETED,
            )
            failure = poll_task.exception() if poll_task.done() else None
            assert reclaimed_task in done, (
                "poll terminated before the expired lease was reclaimed: "
                f"{type(failure).__name__ if failure else 'timeout'}"
            )
        finally:
            reclaimed_task.cancel()
            poll_task.cancel()
            for task in (reclaimed_task, poll_task):
                try:
                    await task
                except (asyncio.CancelledError, sqlalchemy_exc.OperationalError):
                    pass

    asyncio.run(exercise())

    assert repository.claim_count == 2
    assert "record-failure:infrastructure:temporarily_unavailable:True" in trace
    assert trace.index("sleep:0.25") < trace.index("claim:1")
    assert "finalize:1" in trace


def test_poll_backs_off_and_continues_after_top_level_transient_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching import provisioning_worker as worker_module

    trace: list[str] = []
    continued = asyncio.Event()

    class FlakyClaimRepository(FakeProvisioningRepository):
        def __init__(self) -> None:
            super().__init__(trace)
            self.claim_count = 0

        async def claim_next(
            self,
            worker_id: str,
            *,
            lease_seconds: int,
        ) -> ProvisioningClaim | None:
            self.claim_count += 1
            trace.append(f"claim:{self.claim_count}")
            if self.claim_count == 1:
                raise sqlalchemy_exc.OperationalError(
                    "SELECT",
                    {},
                    RuntimeError("deterministic claim outage"),
                )
            continued.set()
            return None

    repository = FlakyClaimRepository()
    worker = _worker(repository, trace)
    real_sleep = asyncio.sleep

    async def record_backoff(delay: float) -> None:
        trace.append(f"sleep:{delay}")
        await real_sleep(0)

    monkeypatch.setattr(worker_module.asyncio, "sleep", record_backoff)

    async def exercise() -> None:
        poll_task = asyncio.create_task(worker.poll(poll_interval_seconds=0.5))
        continued_task = asyncio.create_task(continued.wait())
        try:
            done, _pending = await asyncio.wait(
                {poll_task, continued_task},
                timeout=1,
                return_when=asyncio.FIRST_COMPLETED,
            )
            failure = poll_task.exception() if poll_task.done() else None
            assert continued_task in done, (
                "poll terminated before retrying the claim: "
                f"{type(failure).__name__ if failure else 'timeout'}"
            )
        finally:
            continued_task.cancel()
            poll_task.cancel()
            for task in (continued_task, poll_task):
                try:
                    await task
                except (asyncio.CancelledError, sqlalchemy_exc.OperationalError):
                    pass

    asyncio.run(exercise())

    assert trace[:3] == ["claim:1", "sleep:0.5", "claim:2"]


def test_poll_does_not_swallow_connection_invalidated_programming_error() -> None:
    trace: list[str] = []

    class BrokenClaimRepository(FakeProvisioningRepository):
        async def claim_next(
            self,
            worker_id: str,
            *,
            lease_seconds: int,
        ) -> ProvisioningClaim | None:
            raise sqlalchemy_exc.ProgrammingError(
                "SELECT",
                {},
                RuntimeError("deterministic query defect"),
                connection_invalidated=True,
            )

    worker = _worker(BrokenClaimRepository(trace), trace)

    async def exercise() -> None:
        await asyncio.wait_for(worker.poll(poll_interval_seconds=0.01), timeout=0.2)

    with pytest.raises(sqlalchemy_exc.ProgrammingError):
        asyncio.run(exercise())


def test_active_tenant_schema_upgrade_runs_only_the_schema_step() -> None:
    trace: list[str] = []
    repository = FakeProvisioningRepository(trace)
    repository.claim = SimpleNamespace(
        tenant_id="tenant-a",
        job_id="upgrade-a",
        attempt_count=0,
        lease_owner="worker-a",
        lease_token="lease-a",
        operation="upgrade_schema",
    )

    handled = asyncio.run(_worker(repository, trace).run_once())

    assert handled is True
    assert trace == [
        "claim",
        "schema",
        "persist:schema:20260730_0005",
    ]


def test_claim_query_keeps_active_schema_upgrades_separate_from_provisioning() -> None:
    sql = str(
        build_claim_statement(datetime(2026, 7, 30, tzinfo=UTC)).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).replace("platform.", "")

    assert "operation = 'provision'" in sql
    assert "tenants.status = 'provisioning'" in sql
    assert "operation = 'upgrade_schema'" in sql
    assert "tenants.status = 'active'" in sql
    assert (
        "tenant_schema_states.revision IN ('20260801_0007', '20260802_0008', '20260803_0009', '20260803_0010', '20260803_0011', '20260804_0012', '20260809_0013', '20260809_0014', '20260809_0015', '20260810_0016', '20260810_0017', '20260824_0018', '20260825_0019', '20260825_0020', '20260827_0021')"
    ) in sql
    assert "FOR UPDATE OF tenant_provisioning_jobs, tenants" in sql


def test_provisioning_job_operation_is_database_constrained() -> None:
    operation_constraints = {
        str(constraint.sqltext)
        for constraint in TenantProvisioningJob.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_tenant_provisioning_jobs_operation"
    }

    assert operation_constraints == {
        "operation IN ('provision', 'upgrade_schema')",
    }


def test_schema_upgrade_reconciliation_is_locked_targeted_and_idempotent() -> None:
    sql = str(
        build_schema_upgrade_candidate_statement().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).replace("platform.", "")

    assert "tenants.status = 'active'" in sql
    assert "tenant_schema_states.status = 'active'" in sql
    assert (
        "tenant_schema_states.revision IN ('20260801_0007', '20260802_0008', '20260803_0009', '20260803_0010', '20260803_0011', '20260804_0012', '20260809_0013', '20260809_0014', '20260809_0015', '20260810_0016', '20260810_0017', '20260824_0018', '20260825_0019', '20260825_0020')"
        in sql
    )
    assert "tenant_schema_states.revision != '20260827_0021'" not in sql
    assert "tenant_provisioning_jobs.operation = 'upgrade_schema'" in sql
    assert "tenant_provisioning_jobs.target_revision = '20260827_0021'" in sql
    assert "NOT (EXISTS" in sql
    assert "FOR UPDATE OF tenants, tenant_schema_states SKIP LOCKED" in sql
    assert schema_upgrade_job_id("tenant-a") == schema_upgrade_job_id("tenant-a")
    assert schema_upgrade_job_id("tenant-a") != schema_upgrade_job_id("tenant-b")
    assert len(schema_upgrade_job_id("tenant-a")) == 64


def test_upgrade_claim_accepts_only_supported_or_current_revisions() -> None:
    sql = str(
        build_claim_statement(datetime(2026, 7, 30, tzinfo=UTC)).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).replace("platform.", "")

    assert (
        "tenant_schema_states.revision IN ('20260801_0007', '20260802_0008', '20260803_0009', '20260803_0010', '20260803_0011', '20260804_0012', '20260809_0013', '20260809_0014', '20260809_0015', '20260810_0016', '20260810_0017', '20260824_0018', '20260825_0019', '20260825_0020', '20260827_0021')"
    ) in sql
    assert "tenant_schema_states.revision != '20260827_0021'" not in sql


def test_provisioning_job_attempt_and_lease_state_are_database_constrained() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in TenantProvisioningJob.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert constraints["ck_tenant_provisioning_jobs_attempts"] == (
        "attempt_count >= 0 AND max_attempts > 0 AND attempt_count < max_attempts"
    )
    assert constraints["ck_tenant_provisioning_jobs_status_lease_fence"] == (
        "(status = 'running' AND lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL) OR (status != 'running' "
        "AND lease_owner IS NULL AND lease_token IS NULL "
        "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)"
    )


@pytest.mark.parametrize(
    ("failed_step", "expected_trace", "expected_category", "expected_code"),
    [
        (
            "schema",
            ["claim", "schema", "failure"],
            "schema",
            "migration_unavailable",
        ),
        (
            "storage",
            [
                "claim",
                "schema",
                "persist:schema:20260730_0005",
                "storage",
                "failure",
            ],
            "storage",
            "admin_temporarily_unavailable",
        ),
        (
            "policy",
            [
                "claim",
                "schema",
                "persist:schema:20260730_0005",
                "storage",
                "policy",
                "failure",
            ],
            "policy",
            "invalid_default",
        ),
    ],
)
def test_step_failure_never_activates_and_records_only_safe_classification(
    failed_step: str,
    expected_trace: list[str],
    expected_category: str,
    expected_code: str,
) -> None:
    trace: list[str] = []
    repository = FakeProvisioningRepository(trace)
    failure = ProvisioningStepError(
        category=expected_category,
        code=expected_code,
        retryable=failed_step != "policy",
    )
    worker = _worker(
        repository,
        trace,
        schema_failure=failure if failed_step == "schema" else None,
        storage_failure=failure if failed_step == "storage" else None,
        policy_failure=failure if failed_step == "policy" else None,
    )

    handled = asyncio.run(worker.run_once())

    assert handled is True
    assert trace == expected_trace
    assert repository.failures == [
        (
            expected_category,
            expected_code,
            failed_step != "policy",
            retry_backoff_seconds(0),
        )
    ]


def test_disabled_worker_is_a_noop_without_dependencies() -> None:
    worker = ProvisioningWorker(enabled=False, worker_id="disabled")

    assert asyncio.run(worker.run_once()) is False


def test_default_policy_is_fixed_queryable_canonical_data() -> None:
    result = build_default_policy_result()

    assert result.policy_version == DEFAULT_POLICY_VERSION
    assert result.policy_payload == DEFAULT_POLICY_PAYLOAD
    assert result.policy_payload == (
        '{"classroom_visibility":"tenant_members",'
        '"external_media_enabled":false,'
        '"generation_concurrency_limit":2,'
        '"membership_management":"tenant_admins",'
        '"network_access_enabled":false,'
        '"open_creation_enabled":false}'
    )
    assert len(result.policy_hash) == 64


def test_local_storage_provisioner_creates_and_verifies_tenant_prefix(
    tmp_path: Path,
) -> None:
    settings = PlatformSettings(enabled=False, object_store_mode="local")
    provisioner = build_storage_provisioner(
        settings,
        local_root=tmp_path,
    )

    first = asyncio.run(provisioner.provision("tenant-a"))
    second = asyncio.run(provisioner.provision("tenant-a"))

    assert first == second
    assert first.mode == "local"
    assert first.secret_ref is None
    assert first.access_key_fingerprint is None
    assert (tmp_path / "tenants" / "tenant-a").is_dir()


def test_s3_default_admin_boundary_fails_closed_without_credential_metadata(
    tmp_path: Path,
) -> None:
    settings = PlatformSettings(
        enabled=True,
        database_url="postgresql+asyncpg://user:password@db/test",
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_namespace_id="test-minio-primary",
        object_store_tenant_credentials_dir=tmp_path,
    )
    provisioner = build_storage_provisioner(settings)

    with pytest.raises(ProvisioningStepError) as captured:
        asyncio.run(provisioner.provision("tenant-a"))

    assert captured.value.category == "storage"
    assert captured.value.code == "admin_unavailable"
    assert captured.value.retryable is False
    assert "password" not in repr(captured.value).lower()


class _SchemaLockConnection:
    def __init__(self, *, revision_error: Exception | None = None) -> None:
        self.revision_error = revision_error
        self.directory_locked = False
        self.tenant_locked = False
        self.tenant_resource: str | None = None
        self.seen_tenant_resource: str | None = None
        self.trace: list[str] = []

    @property
    def locked(self) -> bool:
        return self.directory_locked

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: Any) -> None:
        assert self.directory_locked is False
        assert self.tenant_locked is False

    async def execute(self, statement):
        sql = str(statement)
        if "pg_advisory_lock_shared" in sql:
            self.directory_locked = True
            self.trace.append("lock:directory")
            return None
        assert "pg_advisory_lock(" in sql
        assert self.directory_locked is True
        resource = str(statement.compile().params["resource"])
        self.tenant_locked = True
        self.tenant_resource = resource
        self.seen_tenant_resource = resource
        self.trace.append("lock:tenant")

    async def scalar(self, statement):
        sql = str(statement)
        if "pg_advisory_unlock_shared" in sql:
            assert self.directory_locked is True
            assert self.tenant_locked is False
            self.directory_locked = False
            self.trace.append("unlock:directory")
            return True
        if "pg_advisory_unlock(" in sql:
            assert self.directory_locked is True
            assert self.tenant_locked is True
            self.tenant_locked = False
            self.tenant_resource = None
            self.trace.append("unlock:tenant")
            return True
        assert self.directory_locked is True
        assert self.tenant_locked is True
        self.trace.append("revision")
        if self.revision_error is not None:
            raise self.revision_error
        return TENANT_SCHEMA_REVISION

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _SchemaLockEngine:
    def __init__(self, connection: _SchemaLockConnection) -> None:
        self.connection = connection

    def connect(self) -> _SchemaLockConnection:
        return self.connection


def _install_schema_lock_engine(
    monkeypatch: pytest.MonkeyPatch,
    worker_module,
    *,
    revision_error: Exception | None = None,
) -> _SchemaLockConnection:
    connection = _SchemaLockConnection(revision_error=revision_error)
    monkeypatch.setattr(
        worker_module,
        "get_platform_engine",
        lambda: _SchemaLockEngine(connection),
    )
    return connection


def _install_unlocked_migration(
    monkeypatch: pytest.MonkeyPatch,
    migration: Any,
) -> None:
    from deeptutor.teaching.migrations import facade as migration_facade

    monkeypatch.setattr(migration_facade, "_run_migration_unlocked", migration)


def test_schema_migration_holds_shared_directory_lock_through_revision_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching import provisioning_worker as worker_module

    connection = _install_schema_lock_engine(monkeypatch, worker_module)

    def migrate(**_kwargs: Any) -> None:
        assert connection.directory_locked is True
        assert connection.tenant_locked is True
        connection.trace.append("migrate")

    _install_unlocked_migration(monkeypatch, migrate)

    result = asyncio.run(worker_module.AlembicTenantSchemaProvisioner().provision("tenant-a"))

    assert result.revision == TENANT_SCHEMA_REVISION
    assert connection.trace == [
        "lock:directory",
        "lock:tenant",
        "migrate",
        "revision",
        "unlock:tenant",
        "unlock:directory",
    ]


def test_schema_migration_cancellation_waits_for_real_thread_before_unlocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching import provisioning_worker as worker_module

    class BlockingUnlockConnection(_SchemaLockConnection):
        def __init__(self) -> None:
            super().__init__()
            self.unlock_started = asyncio.Event()
            self.allow_unlock = asyncio.Event()

        async def scalar(self, statement):
            sql = str(statement)
            if "pg_advisory_unlock(" in sql:
                self.unlock_started.set()
                await self.allow_unlock.wait()
            return await super().scalar(statement)

    connection = BlockingUnlockConnection()
    monkeypatch.setattr(
        worker_module,
        "get_platform_engine",
        lambda: _SchemaLockEngine(connection),
    )
    started = threading.Event()
    release = threading.Event()

    def migrate(**_kwargs: Any) -> None:
        started.set()
        if not release.wait(timeout=2):
            raise AssertionError("test did not release the migration thread")
        raise RuntimeError("late migration failure after cancellation")

    _install_unlocked_migration(monkeypatch, migrate)

    async def exercise() -> None:
        task = asyncio.create_task(
            worker_module.AlembicTenantSchemaProvisioner().provision("tenant-a")
        )
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert connection.directory_locked is True
        assert connection.tenant_locked is True
        release.set()
        await asyncio.wait_for(connection.unlock_started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert connection.directory_locked is True
        assert connection.tenant_locked is True
        connection.allow_unlock.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.directory_locked is False
        assert connection.tenant_locked is False

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_same_tenant_schema_migrations_are_serialized_through_revision_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching import provisioning_worker as worker_module

    class LockRegistry:
        def __init__(self) -> None:
            self.locks: dict[str, asyncio.Lock] = {}
            self.attempts = 0
            self.second_attempted = asyncio.Event()

        async def acquire(self, resource: str) -> None:
            self.attempts += 1
            if self.attempts == 2:
                self.second_attempted.set()
            await self.locks.setdefault(resource, asyncio.Lock()).acquire()

        def release(self, resource: str) -> None:
            self.locks[resource].release()

    class Connection:
        def __init__(self, registry: LockRegistry) -> None:
            self.registry = registry
            self.directory_locked = False
            self.tenant_resource: str | None = None
            self.revision_under_tenant_lock = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            assert self.directory_locked is False
            assert self.tenant_resource is None

        async def execute(self, statement):
            sql = str(statement)
            resource = str(statement.compile().params["resource"])
            if "pg_advisory_lock_shared" in sql:
                self.directory_locked = True
                return None
            assert "pg_advisory_lock(" in sql
            await self.registry.acquire(resource)
            self.tenant_resource = resource
            return None

        async def scalar(self, statement):
            sql = str(statement)
            if "pg_advisory_unlock_shared" in sql:
                self.directory_locked = False
                return True
            if "pg_advisory_unlock(" in sql:
                assert self.tenant_resource is not None
                self.registry.release(self.tenant_resource)
                self.tenant_resource = None
                return True
            self.revision_under_tenant_lock = self.tenant_resource is not None
            return TENANT_SCHEMA_REVISION

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class Engine:
        def __init__(self) -> None:
            self.registry = LockRegistry()
            self.connections: list[Connection] = []

        def connect(self) -> Connection:
            connection = Connection(self.registry)
            self.connections.append(connection)
            return connection

    engine = Engine()
    monkeypatch.setattr(worker_module, "get_platform_engine", lambda: engine)
    started = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    starts = 0
    active = 0
    max_active = 0

    def migrate(**_kwargs: Any) -> None:
        nonlocal starts, active, max_active
        with state_lock:
            starts += 1
            is_first = starts == 1
            active += 1
            max_active = max(max_active, active)
            if is_first:
                started.set()
        if is_first and not release.wait(timeout=2):
            raise AssertionError("test did not release the first migration")
        with state_lock:
            active -= 1

    _install_unlocked_migration(monkeypatch, migrate)

    async def exercise() -> None:
        first = asyncio.create_task(
            worker_module.AlembicTenantSchemaProvisioner().provision("tenant-a")
        )
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
        second = asyncio.create_task(
            worker_module.AlembicTenantSchemaProvisioner().provision("tenant-a")
        )
        try:
            await asyncio.wait_for(engine.registry.second_attempted.wait(), timeout=1)
            assert starts == 1
            release.set()
            await asyncio.gather(first, second)
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(exercise())

    assert starts == 2
    assert max_active == 1
    assert all(connection.revision_under_tenant_lock for connection in engine.connections)
    assert all(
        resource.startswith("yfeistai:tenant-schema-migration:v1:")
        for resource in engine.registry.locks
    )


def test_different_tenant_schema_migrations_can_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching import provisioning_worker as worker_module
    from deeptutor.teaching.schema_names import tenant_schema_name

    tenant_ids = ("tenant-a", "tenant-b")
    schemas = tuple(tenant_schema_name(tenant_id) for tenant_id in tenant_ids)
    started = {schema: threading.Event() for schema in schemas}
    release = threading.Event()

    class Connection(_SchemaLockConnection):
        pass

    class Engine:
        def __init__(self) -> None:
            self.connections: list[Connection] = []

        def connect(self) -> Connection:
            connection = Connection()
            self.connections.append(connection)
            return connection

    engine = Engine()
    monkeypatch.setattr(worker_module, "get_platform_engine", lambda: engine)

    def migrate(**kwargs: Any) -> None:
        schema = kwargs["tenant_schema"]
        started[schema].set()
        if not release.wait(timeout=2):
            raise AssertionError("different tenant migrations were serialized")

    _install_unlocked_migration(monkeypatch, migrate)

    async def exercise() -> None:
        tasks = [
            asyncio.create_task(worker_module.AlembicTenantSchemaProvisioner().provision(tenant_id))
            for tenant_id in tenant_ids
        ]
        try:
            both_started = await asyncio.gather(
                *(asyncio.to_thread(event.wait, 1) for event in started.values())
            )
            assert all(both_started)
            release.set()
            await asyncio.gather(*tasks)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())

    resources = {connection.seen_tenant_resource for connection in engine.connections}
    assert len(resources) == 2
    assert all(
        resource is not None and resource.startswith("yfeistai:tenant-schema-migration:v1:")
        for resource in resources
    )
    assert all(
        connection.trace.index("revision") > connection.trace.index("lock:tenant")
        for connection in engine.connections
    )


def test_schema_migration_unavailable_error_is_retryable_and_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching import provisioning_worker as worker_module
    from deeptutor.teaching.migrations.runner import MigrationUnavailableError

    def unavailable(**_kwargs: Any) -> None:
        raise MigrationUnavailableError()

    _install_schema_lock_engine(monkeypatch, worker_module)
    _install_unlocked_migration(monkeypatch, unavailable)

    with pytest.raises(ProvisioningStepError) as captured:
        asyncio.run(worker_module.AlembicTenantSchemaProvisioner().provision("tenant-a"))

    assert captured.value.category == "schema"
    assert captured.value.code == "migration_unavailable"
    assert captured.value.retryable is True


def test_schema_deterministic_command_error_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.util import CommandError

    from deeptutor.teaching import provisioning_worker as worker_module

    def deterministic(**_kwargs: Any) -> None:
        raise CommandError("deterministic migration failure")

    _install_schema_lock_engine(monkeypatch, worker_module)
    _install_unlocked_migration(monkeypatch, deterministic)

    with pytest.raises(ProvisioningStepError) as captured:
        asyncio.run(worker_module.AlembicTenantSchemaProvisioner().provision("tenant-a"))

    assert captured.value.category == "schema"
    assert captured.value.code == "migration_failed"
    assert captured.value.retryable is False


def _capture_schema_revision_query_failure(
    monkeypatch: pytest.MonkeyPatch,
    revision_error: Exception,
) -> ProvisioningStepError:
    from deeptutor.teaching import provisioning_worker as worker_module

    _install_schema_lock_engine(
        monkeypatch,
        worker_module,
        revision_error=revision_error,
    )
    _install_unlocked_migration(monkeypatch, lambda **_kwargs: None)

    with pytest.raises(ProvisioningStepError) as captured:
        asyncio.run(worker_module.AlembicTenantSchemaProvisioner().provision("tenant-a"))
    return captured.value


@pytest.mark.parametrize(
    "revision_error",
    [
        pytest.param(
            sqlalchemy_exc.OperationalError(
                "SELECT",
                {},
                RuntimeError("deterministic missing table"),
            ),
            id="operational-error",
        ),
        pytest.param(
            sqlalchemy_exc.InterfaceError(
                "SELECT",
                {},
                RuntimeError("deterministic driver response"),
            ),
            id="interface-error",
        ),
        pytest.param(
            sqlalchemy_exc.TimeoutError("deterministic pool response"),
            id="sqlalchemy-timeout",
        ),
        pytest.param(
            sqlalchemy_exc.DBAPIError(
                "SELECT",
                {},
                RuntimeError("deterministic invalidation response"),
                connection_invalidated=True,
            ),
            id="invalidated-dbapi-error",
        ),
    ],
)
def test_schema_revision_query_transient_errors_are_retryable(
    monkeypatch: pytest.MonkeyPatch,
    revision_error: Exception,
) -> None:
    failure = _capture_schema_revision_query_failure(
        monkeypatch,
        revision_error,
    )

    assert failure.category == "schema"
    assert failure.code == "verification_unavailable"
    assert failure.retryable is True


@pytest.mark.parametrize(
    "revision_error",
    [
        pytest.param(
            sqlalchemy_exc.ProgrammingError(
                "SELECT",
                {},
                RuntimeError("connection reset and timed out"),
            ),
            id="programming-error",
        ),
        pytest.param(
            ValueError("deterministic verification failure"),
            id="deterministic-error",
        ),
    ],
)
def test_schema_revision_query_deterministic_errors_are_terminal(
    monkeypatch: pytest.MonkeyPatch,
    revision_error: Exception,
) -> None:
    failure = _capture_schema_revision_query_failure(
        monkeypatch,
        revision_error,
    )

    assert failure.category == "schema"
    assert failure.code == "verification_failed"
    assert failure.retryable is False


def test_migration_runtime_error_translation_preserves_transient_types() -> None:
    from alembic.util import CommandError
    from sqlalchemy.exc import OperationalError

    from deeptutor.teaching.migrations.runner import (
        MigrationUnavailableError,
        translate_migration_runtime_error,
    )

    transients = (
        ConnectionError("connection message must not be parsed"),
        TimeoutError("timeout message must not be parsed"),
        OperationalError(
            "connect",
            {},
            RuntimeError("SQL message must not be parsed"),
        ),
    )
    deterministic = CommandError("same words: temporarily unavailable")

    for transient in transients:
        translated = translate_migration_runtime_error(transient)
        assert isinstance(translated, MigrationUnavailableError)
        assert translated.code == "migration_unavailable"
    assert translate_migration_runtime_error(deterministic) is deterministic


def test_injected_s3_admin_must_execute_and_return_verified_tenant_binding(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeS3Admin:
        async def provision_and_verify(
            self,
            *,
            settings: PlatformSettings,
            tenant_id: str,
            tenant_prefix: str,
        ) -> StorageProvisioningResult:
            calls.append((tenant_id, tenant_prefix))
            local = StorageProvisioningResult.local(tenant_id)
            return StorageProvisioningResult(
                mode="s3",
                policy_version=local.policy_version,
                policy_payload=local.policy_payload,
                policy_hash=local.policy_hash,
                secret_ref=f"{tenant_id}/object-store",
                access_key_fingerprint="a" * 64,
            )

    settings = PlatformSettings(
        enabled=True,
        database_url="postgresql+asyncpg://user:password@db/test",
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_namespace_id="test-minio-primary",
        object_store_tenant_credentials_dir=tmp_path,
    )
    provisioner = build_storage_provisioner(
        settings,
        s3_admin=FakeS3Admin(),
    )

    result = asyncio.run(provisioner.provision("tenant-a"))

    assert calls == [("tenant-a", "tenants/tenant-a/")]
    assert result.mode == "s3"
    assert result.secret_ref == "tenant-a/object-store"
    assert result.access_key_fingerprint == "a" * 64


def test_backoff_is_exponential_and_bounded() -> None:
    assert [retry_backoff_seconds(attempt) for attempt in range(7)] == [
        5,
        10,
        20,
        40,
        80,
        160,
        300,
    ]
    assert retry_backoff_seconds(1_000_000) == 300


def test_s3_credential_reference_must_be_resolver_safe() -> None:
    local = StorageProvisioningResult.local("tenant-a")
    unsafe = StorageProvisioningResult(
        mode="s3",
        policy_version=local.policy_version,
        policy_payload=local.policy_payload,
        policy_hash=local.policy_hash,
        secret_ref="../outside",
        access_key_fingerprint="a" * 64,
    )

    with pytest.raises(ProvisioningStepError) as captured:
        unsafe.validate("tenant-a")

    assert captured.value.code == "invalid_credential_metadata"


def test_s3_credential_reference_must_be_bound_to_current_tenant() -> None:
    local = StorageProvisioningResult.local("tenant-a")
    cross_tenant = StorageProvisioningResult(
        mode="s3",
        policy_version=local.policy_version,
        policy_payload=local.policy_payload,
        policy_hash=local.policy_hash,
        secret_ref="tenant-b/object-store",
        access_key_fingerprint="a" * 64,
    )

    with pytest.raises(ProvisioningStepError) as captured:
        cross_tenant.validate("tenant-a")

    assert captured.value.code == "invalid_credential_metadata"


def test_unknown_failure_classification_is_rejected_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching.repositories import provisioning as repository_module

    claim = ProvisioningClaim(
        tenant_id="tenant-a",
        job_id="job-a",
        attempt_count=0,
        lease_owner="worker-a",
        lease_token="lease-a",
    )
    repository = repository_module.SqlAlchemyProvisioningRepository()
    opened = False

    def unexpected_session() -> Any:
        nonlocal opened
        opened = True
        raise AssertionError("database session must not open")

    monkeypatch.setattr(repository_module, "platform_session", unexpected_session)

    with pytest.raises(ValueError, match="unknown provisioning failure"):
        asyncio.run(
            repository.record_failure(
                claim,
                category="storage",
                code="password=leaked",
                retryable=True,
                backoff_seconds=5,
            )
        )

    assert opened is False


def test_failure_classification_cannot_forge_retryability() -> None:
    with pytest.raises(ValueError, match="unknown provisioning failure"):
        ProvisioningStepError(
            category="storage",
            code="admin_unavailable",
            retryable=True,
        )


def test_blocked_step_is_heartbeated_before_another_worker_can_reclaim() -> None:
    async def exercise() -> tuple[ProvisioningClaim | None, list[str], int]:
        trace: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()
        heartbeat_seen = asyncio.Event()

        class LeaseRepository(FakeProvisioningRepository):
            def __init__(self) -> None:
                super().__init__(trace)
                self.initial_claimed = False
                self.heartbeat_count = 0

            async def claim_next(
                self,
                worker_id: str,
                *,
                lease_seconds: int,
            ) -> ProvisioningClaim | None:
                if not self.initial_claimed:
                    self.initial_claimed = True
                    return self.claim
                if worker_id == "worker-b" and self.heartbeat_count == 0:
                    return ProvisioningClaim(
                        tenant_id="tenant-a",
                        job_id="job-a",
                        attempt_count=0,
                        lease_owner="worker-b",
                        lease_token="lease-b",
                    )
                return None

            async def heartbeat(
                self,
                claim: ProvisioningClaim,
                *,
                lease_seconds: int,
            ) -> bool:
                self.heartbeat_count += 1
                heartbeat_seen.set()
                return True

        class BlockingSchema:
            async def provision(self, tenant_id: str) -> SchemaProvisioningResult:
                trace.append("schema")
                started.set()
                await release.wait()
                return SchemaProvisioningResult(
                    schema_name="tenant_9e4714402f06d0f7",
                    revision="20260730_0005",
                )

        repository = LeaseRepository()
        worker = ProvisioningWorker(
            enabled=True,
            worker_id="worker-a",
            repository=repository,
            schema_provisioner=BlockingSchema(),
            storage_provisioner=FakeStorageProvisioner(trace),
            policy_provisioner=FakePolicyProvisioner(trace),
            lease_seconds=3,
            heartbeat_interval_seconds=0.01,
        )
        task = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=1)
        competing_claim = await repository.claim_next(
            "worker-b",
            lease_seconds=3,
        )
        release.set()
        await asyncio.wait_for(task, timeout=1)
        return competing_claim, trace, repository.heartbeat_count

    competing_claim, trace, heartbeat_count = asyncio.run(exercise())

    assert competing_claim is None
    assert heartbeat_count >= 1
    assert trace[-1] == f"finalize:local:{DEFAULT_POLICY_VERSION}:activation"


def test_lost_lease_cancels_blocked_step_and_old_worker_writes_no_state() -> None:
    async def exercise() -> tuple[list[str], bool]:
        trace: list[str] = []
        cancelled = asyncio.Event()

        class LostLeaseRepository(FakeProvisioningRepository):
            async def heartbeat(
                self,
                claim: ProvisioningClaim,
                *,
                lease_seconds: int,
            ) -> bool:
                trace.append("lease-lost")
                return False

        class BlockingSchema:
            async def provision(self, tenant_id: str) -> SchemaProvisioningResult:
                trace.append("schema")
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        repository = LostLeaseRepository(trace)
        worker = ProvisioningWorker(
            enabled=True,
            worker_id="worker-a",
            repository=repository,
            schema_provisioner=BlockingSchema(),
            storage_provisioner=FakeStorageProvisioner(trace),
            policy_provisioner=FakePolicyProvisioner(trace),
            lease_seconds=3,
            heartbeat_interval_seconds=0.01,
        )

        handled = await asyncio.wait_for(worker.run_once(), timeout=1)
        return trace, handled and cancelled.is_set()

    trace, cancelled = asyncio.run(exercise())

    assert cancelled is True
    assert trace == ["claim", "schema", "lease-lost"]


def _compiled_sql(statement: Any) -> str:
    return " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    ).lower()


def test_tenant_directory_writers_use_the_shared_transaction_lock_contract() -> None:
    from deeptutor.teaching.tenant_directory_lock import (
        TENANT_DIRECTORY_LOCK_RESOURCE,
        build_tenant_directory_transaction_lock_statement,
    )

    sql = _compiled_sql(build_tenant_directory_transaction_lock_statement())

    assert "pg_advisory_xact_lock_shared" in sql
    assert TENANT_DIRECTORY_LOCK_RESOURCE in sql


def test_record_schema_ready_locks_directory_before_reading_or_writing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching.repositories import provisioning as repository_module
    from deeptutor.teaching.schema_names import tenant_schema_name

    trace: list[str] = []

    class Transaction:
        async def __aenter__(self) -> None:
            trace.append("transaction-enter")

        async def __aexit__(
            self,
            exc_type: Any,
            exc: BaseException | None,
            traceback: Any,
        ) -> None:
            trace.append("transaction-exit")

    class Session:
        async def __aenter__(self) -> Session:
            trace.append("session-enter")
            return self

        async def __aexit__(
            self,
            exc_type: Any,
            exc: BaseException | None,
            traceback: Any,
        ) -> None:
            trace.append("session-exit")

        def begin(self) -> Transaction:
            return Transaction()

        async def execute(self, statement: Any) -> None:
            sql = str(statement)
            if "pg_advisory_xact_lock_shared" in sql:
                trace.append("directory-lock")
                return
            assert "tenant_schema_states" in sql
            trace.append("state-write")

        async def flush(self) -> None:
            trace.append("flush")

    session = Session()

    async def database_now(candidate: Any) -> datetime:
        assert candidate is session
        trace.append("database-now")
        return datetime(2026, 8, 24, tzinfo=UTC)

    async def lock_claim(
        candidate: Any,
        claim: ProvisioningClaim,
        now: datetime,
    ) -> tuple[object, SimpleNamespace]:
        assert candidate is session
        trace.append("claim-read")
        return object(), SimpleNamespace()

    async def record_audit_once(
        candidate: Any,
        claim: ProvisioningClaim,
        action: str,
    ) -> None:
        assert candidate is session
        trace.append("audit-write")

    repository = repository_module.SqlAlchemyProvisioningRepository()
    monkeypatch.setattr(repository_module, "platform_session", lambda: session)
    monkeypatch.setattr(repository_module, "_database_now", database_now)
    monkeypatch.setattr(repository, "_lock_claim", lock_claim)
    monkeypatch.setattr(repository_module, "_record_audit_once", record_audit_once)
    claim = ProvisioningClaim(
        tenant_id="tenant-a",
        job_id="job-a",
        attempt_count=0,
        lease_owner="worker-a",
        lease_token="lease-a",
    )

    recorded = asyncio.run(
        repository.record_schema_ready(
            claim,
            SchemaProvisioningResult(
                schema_name=tenant_schema_name(claim.tenant_id),
                revision=TENANT_SCHEMA_REVISION,
            ),
        )
    )

    assert recorded is True
    assert trace == [
        "session-enter",
        "transaction-enter",
        "directory-lock",
        "database-now",
        "claim-read",
        "state-write",
        "audit-write",
        "flush",
        "transaction-exit",
        "session-exit",
    ]


def test_claim_query_is_due_stale_and_skip_locked() -> None:
    from deeptutor.teaching.repositories.provisioning import build_claim_statement

    sql = _compiled_sql(build_claim_statement(datetime(2026, 7, 30, 12, tzinfo=UTC)))

    for fragment in (
        "tenants.status = 'provisioning'",
        "tenant_provisioning_jobs.operation = 'provision'",
        "tenant_provisioning_jobs.status = 'pending'",
        "tenant_provisioning_jobs.next_attempt_at <=",
        "tenant_provisioning_jobs.status = 'running'",
        "tenant_provisioning_jobs.lease_expires_at <=",
        "for update of tenant_provisioning_jobs, tenants skip locked",
    ):
        assert fragment in sql


def test_fenced_query_binds_tenant_job_attempt_owner_and_live_lease() -> None:
    from deeptutor.teaching.repositories.provisioning import (
        build_fenced_attempt_statement,
    )

    claim = ProvisioningClaim(
        tenant_id="tenant-a",
        job_id="job-a",
        attempt_count=3,
        lease_owner="worker-a",
        lease_token="lease-new",
    )
    sql = _compiled_sql(
        build_fenced_attempt_statement(
            claim,
            datetime(2026, 7, 30, 12, tzinfo=UTC),
        )
    )

    for fragment in (
        "tenants.id = 'tenant-a'",
        "tenants.status = 'provisioning'",
        "tenant_provisioning_jobs.id = 'job-a'",
        "tenant_provisioning_jobs.tenant_id = 'tenant-a'",
        "tenant_provisioning_jobs.attempt_count = 3",
        "tenant_provisioning_jobs.lease_owner = 'worker-a'",
        "tenant_provisioning_jobs.lease_token = 'lease-new'",
        "tenant_provisioning_jobs.status = 'running'",
        "tenant_provisioning_jobs.lease_expires_at >",
        "for update",
    ):
        assert fragment in sql


def test_worker_finalization_locks_the_exact_schema_prerequisite() -> None:
    from deeptutor.teaching.repositories.provisioning import (
        build_worker_activation_statement,
    )

    claim = ProvisioningClaim(
        tenant_id="tenant-a",
        job_id="job-a",
        attempt_count=3,
        lease_owner="worker-a",
        lease_token="lease-a",
    )
    sql = _compiled_sql(
        build_worker_activation_statement(
            claim,
            datetime(2026, 7, 30, 12, tzinfo=UTC),
        )
    )

    for fragment in (
        "tenant_provisioning_jobs.operation = 'provision'",
        "tenant_schema_states.status = 'active'",
        "tenant_schema_states.revision = '20260827_0021'",
        "for update",
    ):
        assert fragment in sql
    assert "tenant_storage_states" not in sql
    assert "tenant_storage_credentials" not in sql
    assert "tenant_default_policy_states" not in sql
