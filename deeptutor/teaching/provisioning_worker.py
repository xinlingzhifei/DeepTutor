"""Lease-fenced tenant provisioning worker and infrastructure boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PureWindowsPath
import socket
from types import MappingProxyType
from typing import Awaitable, Mapping, Protocol, TypeVar
import uuid

from deeptutor.runtime.home import get_runtime_data_root
from deeptutor.services.config import PlatformSettings, load_platform_settings
from deeptutor.teaching.artifacts import tenant_artifact_prefix
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.migrations.facade import (
    TenantMigrationRevisionMismatchError,
    TenantMigrationVerificationFailedError,
    TenantMigrationVerificationUnavailableError,
    migration_lock_scope,
)
from deeptutor.teaching.migrations.runner import (
    TEACHING_MIGRATION_HEAD_REVISION,
    MigrationUnavailableError,
    is_transient_database_error,
)
from deeptutor.teaching.schema_names import tenant_schema_name

TENANT_SCHEMA_REVISION = TEACHING_MIGRATION_HEAD_REVISION
OBJECT_STORAGE_POLICY_VERSION = "20260730"
DEFAULT_POLICY_VERSION = "20260730"
DEFAULT_POLICY_PAYLOAD = (
    '{"classroom_visibility":"tenant_members",'
    '"external_media_enabled":false,'
    '"generation_concurrency_limit":2,'
    '"membership_management":"tenant_admins",'
    '"network_access_enabled":false,'
    '"open_creation_enabled":false}'
)

_BASE_BACKOFF_SECONDS = 5
_MAX_BACKOFF_SECONDS = 300
FAILURE_CLASSIFICATIONS: Mapping[tuple[str, str], bool] = MappingProxyType(
    {
        ("schema", "migration_unavailable"): True,
        ("schema", "migration_failed"): False,
        ("schema", "verification_unavailable"): True,
        ("schema", "verification_failed"): False,
        ("schema", "revision_mismatch"): False,
        ("schema", "verification_mismatch"): False,
        ("storage", "admin_temporarily_unavailable"): True,
        ("storage", "admin_unavailable"): False,
        ("storage", "admin_result_mode"): False,
        ("storage", "bootstrap_secret_invalid"): False,
        ("storage", "bootstrap_secret_unavailable"): False,
        ("storage", "credential_root_unavailable"): False,
        ("storage", "cross_prefix_access_allowed"): False,
        ("storage", "cross_prefix_probe_inconclusive"): False,
        ("storage", "endpoint_invalid"): False,
        ("storage", "invalid_credential_metadata"): False,
        ("storage", "invalid_local_credential"): False,
        ("storage", "invalid_policy"): False,
        ("storage", "local_prefix_unsafe"): False,
        ("storage", "local_unavailable"): True,
        ("storage", "minio_admin_sdk_unavailable"): False,
        ("storage", "own_prefix_probe_failed"): False,
        ("storage", "probe_unavailable"): True,
        ("storage", "service_account_create_failed"): True,
        ("storage", "service_account_revoke_failed"): True,
        ("storage", "tenant_binding_invalid"): False,
        ("policy", "invalid_default"): False,
        ("infrastructure", "temporarily_unavailable"): True,
        ("worker", "unexpected_error"): False,
    }
)
_StepResult = TypeVar("_StepResult")


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


DEFAULT_POLICY_HASH = _payload_hash(DEFAULT_POLICY_PAYLOAD)


@dataclass(frozen=True, slots=True)
class ProvisioningClaim:
    tenant_id: str
    job_id: str
    attempt_count: int
    lease_owner: str
    lease_token: str = field(repr=False)
    operation: str = "provision"


@dataclass(frozen=True, slots=True)
class SchemaProvisioningResult:
    schema_name: str
    revision: str


@dataclass(frozen=True, slots=True)
class StorageProvisioningResult:
    mode: str
    policy_version: str
    policy_payload: str
    policy_hash: str
    secret_ref: str | None = None
    access_key_fingerprint: str | None = None

    @classmethod
    def local(cls, tenant_id: str) -> "StorageProvisioningResult":
        payload = _canonical_json(
            {
                "allowed_actions": ["delete", "get", "list", "put"],
                "prefix": tenant_artifact_prefix(tenant_id),
            }
        )
        return cls(
            mode="local",
            policy_version=OBJECT_STORAGE_POLICY_VERSION,
            policy_payload=payload,
            policy_hash=_payload_hash(payload),
        )

    def validate(self, tenant_id: str) -> None:
        expected = type(self).local(tenant_id)
        if (
            self.mode not in {"local", "s3"}
            or self.policy_version != OBJECT_STORAGE_POLICY_VERSION
            or self.policy_payload != expected.policy_payload
            or self.policy_hash != expected.policy_hash
        ):
            raise ProvisioningStepError(
                category="storage",
                code="invalid_policy",
                retryable=False,
            )
        if self.mode == "local":
            if self.secret_ref is not None or self.access_key_fingerprint is not None:
                raise ProvisioningStepError(
                    category="storage",
                    code="invalid_local_credential",
                    retryable=False,
                )
            return
        from deeptutor.teaching.minio_tenant_storage import (
            secret_ref_is_bound_to_tenant,
        )

        fingerprint = self.access_key_fingerprint
        secret_ref = self.secret_ref
        if (
            not secret_ref
            or len(secret_ref) > 512
            or secret_ref.startswith("/")
            or secret_ref.endswith("/")
            or "\\" in secret_ref
            or ":" in secret_ref
            or "\x00" in secret_ref
            or PureWindowsPath(secret_ref).drive
            or any(part in {"", ".", ".."} for part in secret_ref.split("/"))
            or not secret_ref_is_bound_to_tenant(secret_ref, tenant_id)
            or fingerprint is None
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ProvisioningStepError(
                category="storage",
                code="invalid_credential_metadata",
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class TenantPolicyProvisioningResult:
    policy_version: str
    policy_payload: str
    policy_hash: str

    def validate(self) -> None:
        if (
            self.policy_version != DEFAULT_POLICY_VERSION
            or self.policy_payload != DEFAULT_POLICY_PAYLOAD
            or self.policy_hash != DEFAULT_POLICY_HASH
        ):
            raise ProvisioningStepError(
                category="policy",
                code="invalid_default",
                retryable=False,
            )


def build_default_policy_result() -> TenantPolicyProvisioningResult:
    """Return the fixed, canonical, queryable default tenant policy."""

    return TenantPolicyProvisioningResult(
        policy_version=DEFAULT_POLICY_VERSION,
        policy_payload=DEFAULT_POLICY_PAYLOAD,
        policy_hash=DEFAULT_POLICY_HASH,
    )


class ProvisioningStepError(RuntimeError):
    """Safe fixed failure classification crossing the worker boundary."""

    def __init__(self, *, category: str, code: str, retryable: bool) -> None:
        validate_failure_classification(category, code, retryable)
        self.category = category
        self.code = code
        self.retryable = retryable
        super().__init__(f"{category}:{code}")


def validate_failure_classification(
    category: str,
    code: str,
    retryable: bool,
) -> None:
    """Reject any failure identifier outside the fixed safe registry."""

    expected_retryable = FAILURE_CLASSIFICATIONS.get((category, code))
    if expected_retryable is None or expected_retryable is not retryable:
        raise ValueError("unknown provisioning failure classification")


class ProvisioningRepository(Protocol):
    async def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> ProvisioningClaim | None: ...

    async def heartbeat(
        self,
        claim: ProvisioningClaim,
        *,
        lease_seconds: int,
    ) -> bool: ...

    async def record_schema_ready(
        self,
        claim: ProvisioningClaim,
        result: SchemaProvisioningResult,
    ) -> bool: ...

    async def finalize_provisioning(
        self,
        claim: ProvisioningClaim,
        storage: StorageProvisioningResult,
        policy: TenantPolicyProvisioningResult,
    ) -> bool: ...

    async def record_failure(
        self,
        claim: ProvisioningClaim,
        *,
        category: str,
        code: str,
        retryable: bool,
        backoff_seconds: int,
    ) -> bool: ...


class TenantSchemaUpgradeReconciler(Protocol):
    async def enqueue_next_schema_upgrade(self) -> bool: ...


class TenantSchemaProvisioner(Protocol):
    async def provision(self, tenant_id: str) -> SchemaProvisioningResult: ...


class TenantStorageProvisioner(Protocol):
    async def provision(self, tenant_id: str) -> StorageProvisioningResult: ...


class TenantPolicyProvisioner(Protocol):
    async def provision(self, tenant_id: str) -> TenantPolicyProvisioningResult: ...


class S3TenantStorageAdmin(Protocol):
    """Production boundary that must create and verify tenant-only S3 access."""

    async def provision_and_verify(
        self,
        *,
        settings: PlatformSettings,
        tenant_id: str,
        tenant_prefix: str,
    ) -> StorageProvisioningResult: ...


class UnavailableS3TenantStorageAdmin:
    """Fail-closed default until an operator supplies a real admin adapter."""

    async def provision_and_verify(
        self,
        *,
        settings: PlatformSettings,
        tenant_id: str,
        tenant_prefix: str,
    ) -> StorageProvisioningResult:
        raise ProvisioningStepError(
            category="storage",
            code="admin_unavailable",
            retryable=False,
        )


class AlembicTenantSchemaProvisioner:
    """Run the packaged tenant migration and verify its actual revision."""

    async def provision(self, tenant_id: str) -> SchemaProvisioningResult:
        schema_name = tenant_schema_name(tenant_id)
        try:
            engine = get_platform_engine()
            async with migration_lock_scope(
                engine,
                scope="tenant",
                tenant_schema=schema_name,
            ) as lease:
                revision = await lease.run(
                    action="upgrade",
                    scope="tenant",
                    tenant_schema=schema_name,
                )
        except TenantMigrationVerificationUnavailableError:
            raise ProvisioningStepError(
                category="schema",
                code="verification_unavailable",
                retryable=True,
            ) from None
        except TenantMigrationVerificationFailedError:
            raise ProvisioningStepError(
                category="schema",
                code="verification_failed",
                retryable=False,
            ) from None
        except TenantMigrationRevisionMismatchError:
            raise ProvisioningStepError(
                category="schema",
                code="revision_mismatch",
                retryable=False,
            ) from None
        except ProvisioningStepError:
            raise
        except Exception as exc:
            if isinstance(exc, MigrationUnavailableError) or is_transient_database_error(exc):
                raise ProvisioningStepError(
                    category="schema",
                    code="migration_unavailable",
                    retryable=True,
                ) from None
            raise ProvisioningStepError(
                category="schema",
                code="migration_failed",
                retryable=False,
            ) from None
        return SchemaProvisioningResult(
            schema_name=schema_name,
            revision=str(revision),
        )


class LocalTenantStorageProvisioner:
    """Create and verify the sole tenant prefix in explicit local mode."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    async def provision(self, tenant_id: str) -> StorageProvisioningResult:
        prefix = tenant_artifact_prefix(tenant_id)

        def create_and_verify() -> None:
            self._root.mkdir(parents=True, exist_ok=True)
            resolved_root = self._root.resolve(strict=True)
            target = resolved_root.joinpath(*prefix.rstrip("/").split("/"))
            target.mkdir(parents=True, exist_ok=True)
            resolved_target = target.resolve(strict=True)
            try:
                resolved_target.relative_to(resolved_root)
            except ValueError:
                raise ProvisioningStepError(
                    category="storage",
                    code="local_prefix_unsafe",
                    retryable=False,
                ) from None
            if target.is_symlink() or not resolved_target.is_dir():
                raise ProvisioningStepError(
                    category="storage",
                    code="local_prefix_unsafe",
                    retryable=False,
                )

        try:
            await asyncio.to_thread(create_and_verify)
        except ProvisioningStepError:
            raise
        except OSError:
            raise ProvisioningStepError(
                category="storage",
                code="local_unavailable",
                retryable=True,
            ) from None
        return StorageProvisioningResult.local(tenant_id)


class ConfiguredS3TenantStorageProvisioner:
    def __init__(
        self,
        settings: PlatformSettings,
        admin: S3TenantStorageAdmin,
    ) -> None:
        self._settings = settings
        self._admin = admin

    async def provision(self, tenant_id: str) -> StorageProvisioningResult:
        try:
            result = await self._admin.provision_and_verify(
                settings=self._settings,
                tenant_id=tenant_id,
                tenant_prefix=tenant_artifact_prefix(tenant_id),
            )
        except ProvisioningStepError:
            raise
        except (ConnectionError, OSError, TimeoutError):
            raise ProvisioningStepError(
                category="storage",
                code="admin_temporarily_unavailable",
                retryable=True,
            ) from None
        result.validate(tenant_id)
        if result.mode != "s3":
            raise ProvisioningStepError(
                category="storage",
                code="admin_result_mode",
                retryable=False,
            )
        return result


class FixedTenantPolicyProvisioner:
    async def provision(self, tenant_id: str) -> TenantPolicyProvisioningResult:
        return build_default_policy_result()


def build_storage_provisioner(
    settings: PlatformSettings,
    *,
    local_root: Path | None = None,
    s3_admin: S3TenantStorageAdmin | None = None,
) -> TenantStorageProvisioner:
    if settings.object_store_mode == "local":
        root = local_root or get_runtime_data_root() / "teaching" / "object-store"
        return LocalTenantStorageProvisioner(root)
    return ConfiguredS3TenantStorageProvisioner(
        settings,
        s3_admin or UnavailableS3TenantStorageAdmin(),
    )


def retry_backoff_seconds(attempt_count: int) -> int:
    bounded_attempt = min(max(0, int(attempt_count)), 6)
    return min(_BASE_BACKOFF_SECONDS * (2**bounded_attempt), _MAX_BACKOFF_SECONDS)


class ProvisioningWorker:
    """Claim and execute one tenant provisioning job at a time."""

    def __init__(
        self,
        *,
        enabled: bool,
        worker_id: str,
        repository: ProvisioningRepository | None = None,
        schema_upgrade_reconciler: TenantSchemaUpgradeReconciler | None = None,
        schema_provisioner: TenantSchemaProvisioner | None = None,
        storage_provisioner: TenantStorageProvisioner | None = None,
        policy_provisioner: TenantPolicyProvisioner | None = None,
        lease_seconds: int = 300,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_interval_seconds is not None and heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self._enabled = enabled
        self._worker_id = worker_id
        self._repository = repository
        self._schema_upgrade_reconciler = schema_upgrade_reconciler
        self._schema_provisioner = schema_provisioner
        self._storage_provisioner = storage_provisioner
        self._policy_provisioner = policy_provisioner
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = (
            heartbeat_interval_seconds
            if heartbeat_interval_seconds is not None
            else min(lease_seconds / 3, 30.0)
        )

    def _require_dependencies(
        self,
    ) -> tuple[
        ProvisioningRepository,
        TenantSchemaProvisioner,
        TenantStorageProvisioner,
        TenantPolicyProvisioner,
    ]:
        dependencies = (
            self._repository,
            self._schema_provisioner,
            self._storage_provisioner,
            self._policy_provisioner,
        )
        if any(dependency is None for dependency in dependencies):
            raise RuntimeError("enabled provisioning worker dependencies are incomplete")
        repository, schema, storage, policy = dependencies
        return repository, schema, storage, policy  # type: ignore[return-value]

    async def _refresh_lease(
        self,
        repository: ProvisioningRepository,
        claim: ProvisioningClaim,
    ) -> bool:
        return await repository.heartbeat(
            claim,
            lease_seconds=self._lease_seconds,
        )

    async def _record_failure_best_effort(
        self,
        repository: ProvisioningRepository,
        claim: ProvisioningClaim,
        *,
        category: str,
        code: str,
        retryable: bool,
    ) -> bool:
        try:
            await repository.record_failure(
                claim,
                category=category,
                code=code,
                retryable=retryable,
                backoff_seconds=retry_backoff_seconds(claim.attempt_count),
            )
        except Exception as exc:
            if is_transient_database_error(exc):
                return False
            raise
        return True

    async def _run_step_with_heartbeat(
        self,
        repository: ProvisioningRepository,
        claim: ProvisioningClaim,
        operation: Awaitable[_StepResult],
    ) -> tuple[bool, _StepResult | None]:
        """Keep a lease live while an adapter runs and stop on fence loss."""

        task = asyncio.create_task(operation)
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=self._heartbeat_interval_seconds,
                )
                if task in done:
                    return True, await task
                if not await asyncio.wait_for(
                    self._refresh_lease(repository, claim),
                    timeout=self._heartbeat_interval_seconds,
                ):
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    return False, None
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def run_once(self) -> bool:
        """Handle at most one claim; disabled mode performs no work."""

        if not self._enabled:
            return False
        repository, schema, storage, policy = self._require_dependencies()
        if self._schema_upgrade_reconciler is not None:
            await self._schema_upgrade_reconciler.enqueue_next_schema_upgrade()
        claim = await repository.claim_next(
            self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if claim is None:
            return False

        try:
            lease_live, schema_result = await self._run_step_with_heartbeat(
                repository,
                claim,
                schema.provision(claim.tenant_id),
            )
            if not lease_live or schema_result is None:
                return True
            if not await self._refresh_lease(repository, claim):
                return True
            if not await repository.record_schema_ready(claim, schema_result):
                return True
            if claim.operation == "upgrade_schema":
                return True
            if claim.operation != "provision":
                raise RuntimeError("unknown provisioning operation")

            lease_live, storage_result = await self._run_step_with_heartbeat(
                repository,
                claim,
                storage.provision(claim.tenant_id),
            )
            if not lease_live or storage_result is None:
                return True
            storage_result.validate(claim.tenant_id)
            if not await self._refresh_lease(repository, claim):
                return True

            lease_live, policy_result = await self._run_step_with_heartbeat(
                repository,
                claim,
                policy.provision(claim.tenant_id),
            )
            if not lease_live or policy_result is None:
                return True
            policy_result.validate()
            if not await self._refresh_lease(repository, claim):
                return True
            await repository.finalize_provisioning(claim, storage_result, policy_result)
        except ProvisioningStepError as exc:
            return await self._record_failure_best_effort(
                repository,
                claim,
                category=exc.category,
                code=exc.code,
                retryable=exc.retryable,
            )
        except Exception as exc:
            if not is_transient_database_error(exc):
                raise
            return await self._record_failure_best_effort(
                repository,
                claim,
                category="infrastructure",
                code="temporarily_unavailable",
                retryable=True,
            )
        return True

    async def poll(
        self,
        *,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        """Poll until cancelled; disabled mode exits immediately."""

        if not self._enabled:
            return
        while True:
            try:
                handled = await self.run_once()
            except Exception as exc:
                if not is_transient_database_error(exc):
                    raise
                handled = False
            if not handled:
                await asyncio.sleep(poll_interval_seconds)


def build_provisioning_worker(
    *,
    settings: PlatformSettings | None = None,
    worker_id: str | None = None,
    s3_admin: S3TenantStorageAdmin | None = None,
    local_root: Path | None = None,
) -> ProvisioningWorker:
    runtime_settings = settings or load_platform_settings()
    resolved_worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex}"
    if not runtime_settings.enabled:
        return ProvisioningWorker(
            enabled=False,
            worker_id=resolved_worker_id,
        )

    from deeptutor.teaching.repositories.provisioning import (
        SqlAlchemyProvisioningRepository,
    )

    if s3_admin is None and runtime_settings.object_store_mode == "s3":
        from deeptutor.teaching.minio_tenant_storage import (
            RuntimeMinioTenantStorageAdmin,
        )

        s3_admin = RuntimeMinioTenantStorageAdmin.from_settings(runtime_settings)

    repository = SqlAlchemyProvisioningRepository()
    return ProvisioningWorker(
        enabled=True,
        worker_id=resolved_worker_id,
        repository=repository,
        schema_upgrade_reconciler=repository,
        schema_provisioner=AlembicTenantSchemaProvisioner(),
        storage_provisioner=build_storage_provisioner(
            runtime_settings,
            local_root=local_root,
            s3_admin=s3_admin,
        ),
        policy_provisioner=FixedTenantPolicyProvisioner(),
    )
