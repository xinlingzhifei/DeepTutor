from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.provisioning_worker import (
    DEFAULT_POLICY_PAYLOAD,
    DEFAULT_POLICY_VERSION,
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

    async def record_storage_ready(
        self,
        claim: ProvisioningClaim,
        result: StorageProvisioningResult,
    ) -> bool:
        self.trace.append(f"persist:storage:{result.mode}")
        return True

    async def record_default_policy_ready(
        self,
        claim: ProvisioningClaim,
        result: TenantPolicyProvisioningResult,
    ) -> bool:
        self.trace.append(f"persist:policy:{result.policy_version}")
        return True

    async def activate(self, claim: ProvisioningClaim) -> bool:
        self.trace.append("activate")
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
            revision="20260730_0003",
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


def test_run_once_executes_and_persists_steps_in_fixed_order() -> None:
    trace: list[str] = []
    repository = FakeProvisioningRepository(trace)

    handled = asyncio.run(_worker(repository, trace).run_once())

    assert handled is True
    assert trace == [
        "claim",
        "schema",
        "persist:schema:20260730_0003",
        "storage",
        "persist:storage:local",
        "policy",
        f"persist:policy:{DEFAULT_POLICY_VERSION}",
        "activate",
    ]
    assert repository.failures == []


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
                "persist:schema:20260730_0003",
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
                "persist:schema:20260730_0003",
                "storage",
                "persist:storage:local",
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
        object_store_tenant_credentials_dir=tmp_path,
    )
    provisioner = build_storage_provisioner(settings)

    with pytest.raises(ProvisioningStepError) as captured:
        asyncio.run(provisioner.provision("tenant-a"))

    assert captured.value.category == "storage"
    assert captured.value.code == "admin_unavailable"
    assert captured.value.retryable is False
    assert "password" not in repr(captured.value).lower()


def test_schema_migration_unavailable_error_is_retryable_and_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching import provisioning_worker as worker_module
    from deeptutor.teaching.migrations.runner import MigrationUnavailableError

    def unavailable(**_kwargs: Any) -> None:
        raise MigrationUnavailableError()

    monkeypatch.setattr(worker_module, "run_migration", unavailable)

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

    monkeypatch.setattr(worker_module, "run_migration", deterministic)

    with pytest.raises(ProvisioningStepError) as captured:
        asyncio.run(worker_module.AlembicTenantSchemaProvisioner().provision("tenant-a"))

    assert captured.value.category == "schema"
    assert captured.value.code == "migration_failed"
    assert captured.value.retryable is False


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
                    revision="20260730_0003",
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
    assert trace[-1] == "activate"


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


def test_claim_query_is_due_stale_and_skip_locked() -> None:
    from deeptutor.teaching.repositories.provisioning import build_claim_statement

    sql = _compiled_sql(
        build_claim_statement(datetime(2026, 7, 30, 12, tzinfo=UTC))
    )

    for fragment in (
        "tenants.status = 'provisioning'",
        "tenant_provisioning_jobs.operation = 'provision'",
        "tenant_provisioning_jobs.status = 'pending'",
        "tenant_provisioning_jobs.next_attempt_at <=",
        "tenant_provisioning_jobs.status = 'running'",
        "tenant_provisioning_jobs.lease_expires_at <=",
        "for update of tenant_provisioning_jobs skip locked",
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


def test_worker_activation_requires_schema_policy_and_mode_specific_storage() -> None:
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
        "tenant_schema_states.status = 'active'",
        "tenant_schema_states.revision = '20260730_0003'",
        "tenant_storage_states.status = 'active'",
        "tenant_storage_states.mode = 'local'",
        "tenant_storage_states.mode = 's3'",
        "tenant_storage_credentials.status = 'active'",
        "tenant_storage_states.credential_secret_ref = "
        "platform.tenant_storage_credentials.secret_ref",
        "tenant_storage_states.credential_fingerprint = "
        "platform.tenant_storage_credentials.access_key_fingerprint",
        "tenant_default_policy_states.status = 'active'",
        f"tenant_default_policy_states.policy_version = '{DEFAULT_POLICY_VERSION}'",
        f"tenant_default_policy_states.policy_payload = '{DEFAULT_POLICY_PAYLOAD}'",
        "for update",
    ):
        assert fragment in sql
