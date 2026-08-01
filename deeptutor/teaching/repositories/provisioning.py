"""PostgreSQL repository for the lease-fenced provisioning worker."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import secrets
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql import Select

from deeptutor.teaching.database import platform_session
from deeptutor.teaching.models import (
    AuditLog,
    Tenant,
    TenantDefaultPolicyState,
    TenantProvisioningJob,
    TenantSchemaState,
    TenantStorageCredential,
    TenantStorageState,
)
from deeptutor.teaching.provisioning_worker import (
    DEFAULT_POLICY_HASH,
    DEFAULT_POLICY_PAYLOAD,
    DEFAULT_POLICY_VERSION,
    OBJECT_STORAGE_POLICY_VERSION,
    TENANT_SCHEMA_REVISION,
    ProvisioningClaim,
    ProvisioningStepError,
    SchemaProvisioningResult,
    StorageProvisioningResult,
    TenantPolicyProvisioningResult,
    validate_failure_classification,
)
from deeptutor.teaching.schema_names import tenant_schema_name

_RESOURCE_TYPE = "provisioning_job"
_AUDIT_ATTEMPT_STARTED = "tenant.provisioning.attempt_started"
_AUDIT_SCHEMA_READY = "tenant.provisioning.schema_ready"
_AUDIT_STORAGE_READY = "tenant.provisioning.storage_ready"
_AUDIT_DEFAULT_POLICY_READY = "tenant.provisioning.default_policy_ready"
_AUDIT_COMPLETED = "tenant.provisioning.completed"
_AUDIT_RETRY_SCHEDULED = "tenant.provisioning.retry_scheduled"
_AUDIT_FAILED = "tenant.provisioning.failed"
_AUDIT_SCHEMA_UPGRADE_COMPLETED = "tenant.schema_upgrade.completed"
_PREVIOUS_TENANT_SCHEMA_REVISION = "20260801_0006"


def schema_upgrade_job_id(tenant_id: str) -> str:
    """Return the stable idempotency fence for the current target revision."""

    digest = hashlib.sha256(f"{tenant_id}\0{TENANT_SCHEMA_REVISION}".encode("utf-8")).hexdigest()
    return f"upg-{digest[:60]}"


def build_schema_upgrade_candidate_statement() -> Select[Any]:
    """Lock one active tenant whose persisted schema revision is behind."""

    target_job_exists = (
        select(TenantProvisioningJob.id).where(
            TenantProvisioningJob.tenant_id == Tenant.id,
            TenantProvisioningJob.operation == "upgrade_schema",
            TenantProvisioningJob.target_revision == TENANT_SCHEMA_REVISION,
        )
    ).exists()
    return (
        select(Tenant, TenantSchemaState)
        .join(TenantSchemaState, TenantSchemaState.tenant_id == Tenant.id)
        .where(
            Tenant.status == "active",
            TenantSchemaState.status == "active",
            TenantSchemaState.revision.is_not(None),
            TenantSchemaState.revision == _PREVIOUS_TENANT_SCHEMA_REVISION,
            ~target_job_exists,
        )
        .order_by(TenantSchemaState.updated_at, Tenant.id)
        .limit(1)
        .with_for_update(of=(Tenant, TenantSchemaState), skip_locked=True)
    )


async def _database_now(session: Any) -> datetime:
    """Use PostgreSQL time so worker-host clock skew cannot steal leases."""

    value = await session.scalar(select(func.now()))
    if not isinstance(value, datetime):
        raise RuntimeError("database clock is unavailable")
    return value


def _resource_id(claim: ProvisioningClaim) -> str:
    return f"{claim.job_id}:{claim.attempt_count}"


def build_claim_statement(reference_time: datetime) -> Select[Any]:
    """Select one due or stale job with a PostgreSQL skip-locked claim."""

    upgradeable_schema = (
        select(TenantSchemaState.tenant_id).where(
            TenantSchemaState.tenant_id == Tenant.id,
            TenantSchemaState.status == "active",
            TenantSchemaState.revision.in_(
                (_PREVIOUS_TENANT_SCHEMA_REVISION, TENANT_SCHEMA_REVISION)
            ),
        )
    ).exists()
    return (
        select(TenantProvisioningJob, Tenant)
        .join(Tenant, Tenant.id == TenantProvisioningJob.tenant_id)
        .where(
            or_(
                and_(
                    Tenant.status == "provisioning",
                    TenantProvisioningJob.operation == "provision",
                ),
                and_(
                    Tenant.status == "active",
                    TenantProvisioningJob.operation == "upgrade_schema",
                    TenantProvisioningJob.target_revision == TENANT_SCHEMA_REVISION,
                    upgradeable_schema,
                ),
            ),
            or_(
                and_(
                    TenantProvisioningJob.status == "pending",
                    TenantProvisioningJob.next_attempt_at <= reference_time,
                ),
                and_(
                    TenantProvisioningJob.status == "running",
                    TenantProvisioningJob.lease_expires_at.is_not(None),
                    TenantProvisioningJob.lease_expires_at <= reference_time,
                ),
            ),
        )
        .order_by(
            TenantProvisioningJob.next_attempt_at,
            TenantProvisioningJob.created_at,
            TenantProvisioningJob.id,
        )
        .limit(1)
        .with_for_update(
            of=(TenantProvisioningJob, Tenant),
            skip_locked=True,
        )
    )


def build_fenced_attempt_statement(
    claim: ProvisioningClaim,
    reference_time: datetime,
) -> Select[Any]:
    """Lock the exact live lease owner for one tenant/job/attempt."""

    tenant_operation = or_(
        and_(
            Tenant.status == "provisioning",
            TenantProvisioningJob.operation == "provision",
            claim.operation == "provision",
        ),
        and_(
            Tenant.status == "active",
            TenantProvisioningJob.operation == "upgrade_schema",
            TenantProvisioningJob.target_revision == TENANT_SCHEMA_REVISION,
            claim.operation == "upgrade_schema",
        ),
    )
    statement = (
        select(Tenant, TenantProvisioningJob)
        .join(
            TenantProvisioningJob,
            TenantProvisioningJob.tenant_id == Tenant.id,
        )
        .where(
            Tenant.id == claim.tenant_id,
            tenant_operation,
            TenantProvisioningJob.id == claim.job_id,
            TenantProvisioningJob.tenant_id == claim.tenant_id,
            TenantProvisioningJob.status == "running",
            TenantProvisioningJob.attempt_count == claim.attempt_count,
            TenantProvisioningJob.lease_owner == claim.lease_owner,
            TenantProvisioningJob.lease_token == claim.lease_token,
            TenantProvisioningJob.lease_expires_at.is_not(None),
            TenantProvisioningJob.lease_expires_at > reference_time,
        )
        .with_for_update(of=(Tenant, TenantProvisioningJob))
    )
    if claim.operation == "upgrade_schema":
        statement = (
            statement.join(
                TenantSchemaState,
                TenantSchemaState.tenant_id == Tenant.id,
            )
            .where(
                TenantSchemaState.status == "active",
                TenantSchemaState.revision.in_(
                    (_PREVIOUS_TENANT_SCHEMA_REVISION, TENANT_SCHEMA_REVISION)
                ),
            )
            .with_for_update(of=(Tenant, TenantProvisioningJob, TenantSchemaState))
        )
    return statement


def build_worker_activation_statement(
    claim: ProvisioningClaim,
    reference_time: datetime,
) -> Select[Any]:
    """Lock a live attempt only when every persisted prerequisite is exact."""

    expected_storage = StorageProvisioningResult.local(claim.tenant_id)
    return (
        build_fenced_attempt_statement(claim, reference_time)
        .join(
            TenantSchemaState,
            TenantSchemaState.tenant_id == Tenant.id,
        )
        .join(
            TenantStorageState,
            TenantStorageState.tenant_id == Tenant.id,
        )
        .outerjoin(
            TenantStorageCredential,
            TenantStorageCredential.tenant_id == Tenant.id,
        )
        .join(
            TenantDefaultPolicyState,
            TenantDefaultPolicyState.tenant_id == Tenant.id,
        )
        .where(
            TenantProvisioningJob.operation == "provision",
            TenantSchemaState.schema_name == tenant_schema_name(claim.tenant_id),
            TenantSchemaState.revision == TENANT_SCHEMA_REVISION,
            TenantSchemaState.status == "active",
            TenantStorageState.status == "active",
            TenantStorageState.policy_version == OBJECT_STORAGE_POLICY_VERSION,
            TenantStorageState.policy_payload == expected_storage.policy_payload,
            TenantStorageState.policy_hash == expected_storage.policy_hash,
            or_(
                and_(
                    TenantStorageState.mode == "local",
                    TenantStorageState.credential_secret_ref.is_(None),
                    TenantStorageState.credential_fingerprint.is_(None),
                ),
                and_(
                    TenantStorageState.mode == "s3",
                    TenantStorageCredential.status == "active",
                    TenantStorageCredential.secret_ref != "",
                    TenantStorageCredential.access_key_fingerprint != "",
                    TenantStorageState.credential_secret_ref == TenantStorageCredential.secret_ref,
                    TenantStorageState.credential_fingerprint
                    == TenantStorageCredential.access_key_fingerprint,
                ),
            ),
            TenantDefaultPolicyState.status == "active",
            TenantDefaultPolicyState.policy_version == DEFAULT_POLICY_VERSION,
            TenantDefaultPolicyState.policy_payload == DEFAULT_POLICY_PAYLOAD,
            TenantDefaultPolicyState.policy_hash == DEFAULT_POLICY_HASH,
        )
    )


async def _record_audit_once(
    session: Any,
    claim: ProvisioningClaim,
    action: str,
) -> None:
    resource_id = _resource_id(claim)
    existing = await session.scalar(
        select(AuditLog.id).where(
            AuditLog.tenant_id == claim.tenant_id,
            AuditLog.action == action,
            AuditLog.resource_type == _RESOURCE_TYPE,
            AuditLog.resource_id == resource_id,
        )
    )
    if existing is None:
        session.add(
            AuditLog(
                tenant_id=claim.tenant_id,
                actor_id=None,
                action=action,
                resource_type=_RESOURCE_TYPE,
                resource_id=resource_id,
            )
        )


class SqlAlchemyProvisioningRepository:
    """Persist worker claims, prerequisites, retries, and final state."""

    async def enqueue_next_schema_upgrade(self) -> bool:
        """Idempotently materialize one upgrade job without deactivating its tenant."""

        async with platform_session() as session:
            async with session.begin():
                row = (
                    await session.execute(build_schema_upgrade_candidate_statement())
                ).one_or_none()
                if row is None:
                    return False
                tenant, _schema_state = row
                result = await session.execute(
                    insert(TenantProvisioningJob)
                    .values(
                        id=schema_upgrade_job_id(tenant.id),
                        tenant_id=tenant.id,
                        operation="upgrade_schema",
                        target_revision=TENANT_SCHEMA_REVISION,
                        status="pending",
                    )
                    .on_conflict_do_nothing(
                        index_elements=[TenantProvisioningJob.id],
                    )
                )
                return result.rowcount == 1

    async def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> ProvisioningClaim | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be between 1 and 128 characters")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        async with platform_session() as session:
            async with session.begin():
                now = await _database_now(session)
                result = await session.execute(build_claim_statement(now))
                row = result.one_or_none()
                if row is None:
                    return None
                job, _tenant = row
                if job.operation == "upgrade_schema":
                    schema_state = await session.scalar(
                        select(TenantSchemaState)
                        .where(
                            TenantSchemaState.tenant_id == job.tenant_id,
                            TenantSchemaState.status == "active",
                            TenantSchemaState.revision.in_(
                                (
                                    _PREVIOUS_TENANT_SCHEMA_REVISION,
                                    TENANT_SCHEMA_REVISION,
                                )
                            ),
                        )
                        .with_for_update()
                    )
                    if schema_state is None:
                        return None
                if job.status == "running":
                    if job.attempt_count + 1 >= job.max_attempts:
                        exhausted_claim = ProvisioningClaim(
                            tenant_id=job.tenant_id,
                            job_id=job.id,
                            attempt_count=job.attempt_count,
                            lease_owner=job.lease_owner or "expired-worker",
                            lease_token=job.lease_token or "expired-lease",
                            operation=job.operation,
                        )
                        if job.operation == "provision":
                            _tenant.status = "failed"
                            _tenant.updated_at = now
                        job.status = "failed"
                        job.error_category = "worker"
                        job.error_code = "stale_lease_exhausted"
                        job.completed_at = now
                        job.lease_owner = None
                        job.lease_token = None
                        job.lease_expires_at = None
                        job.heartbeat_at = None
                        job.updated_at = now
                        await _record_audit_once(
                            session,
                            exhausted_claim,
                            _AUDIT_FAILED,
                        )
                        await session.flush()
                        return None
                    job.attempt_count += 1
                lease_token = secrets.token_hex(32)
                job.status = "running"
                job.lease_owner = worker_id
                job.lease_token = lease_token
                job.lease_expires_at = now + timedelta(seconds=lease_seconds)
                job.heartbeat_at = now
                job.started_at = job.started_at or now
                job.error_category = None
                job.error_code = None
                job.updated_at = now
                claim = ProvisioningClaim(
                    tenant_id=job.tenant_id,
                    job_id=job.id,
                    attempt_count=job.attempt_count,
                    lease_owner=worker_id,
                    lease_token=lease_token,
                    operation=job.operation,
                )
                await _record_audit_once(
                    session,
                    claim,
                    _AUDIT_ATTEMPT_STARTED,
                )
                await session.flush()
                return claim

    async def heartbeat(
        self,
        claim: ProvisioningClaim,
        *,
        lease_seconds: int,
    ) -> bool:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        async with platform_session() as session:
            async with session.begin():
                now = await _database_now(session)
                locked = await self._lock_claim(session, claim, now)
                if locked is None:
                    return False
                _tenant, job = locked
                job.heartbeat_at = now
                job.lease_expires_at = now + timedelta(seconds=lease_seconds)
                job.updated_at = now
                await session.flush()
                return True

    async def _lock_claim(
        self,
        session: Any,
        claim: ProvisioningClaim,
        now: datetime,
    ) -> tuple[Tenant, TenantProvisioningJob] | None:
        result = await session.execute(build_fenced_attempt_statement(claim, now))
        return result.one_or_none()

    async def record_schema_ready(
        self,
        claim: ProvisioningClaim,
        result: SchemaProvisioningResult,
    ) -> bool:
        if (
            result.schema_name != tenant_schema_name(claim.tenant_id)
            or result.revision != TENANT_SCHEMA_REVISION
        ):
            raise ProvisioningStepError(
                category="schema",
                code="verification_mismatch",
                retryable=False,
            )
        async with platform_session() as session:
            async with session.begin():
                now = await _database_now(session)
                locked = await self._lock_claim(session, claim, now)
                if locked is None:
                    return False
                _tenant, job = locked
                statement = (
                    insert(TenantSchemaState)
                    .values(
                        tenant_id=claim.tenant_id,
                        schema_name=result.schema_name,
                        revision=result.revision,
                        status="active",
                        verified_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[TenantSchemaState.tenant_id],
                        set_={
                            "schema_name": result.schema_name,
                            "revision": result.revision,
                            "status": "active",
                            "verified_at": now,
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(statement)
                await _record_audit_once(session, claim, _AUDIT_SCHEMA_READY)
                if claim.operation == "upgrade_schema":
                    job.status = "completed"
                    job.completed_at = now
                    job.lease_owner = None
                    job.lease_token = None
                    job.lease_expires_at = None
                    job.heartbeat_at = None
                    job.updated_at = now
                    await _record_audit_once(
                        session,
                        claim,
                        _AUDIT_SCHEMA_UPGRADE_COMPLETED,
                    )
                await session.flush()
                return True

    async def record_storage_ready(
        self,
        claim: ProvisioningClaim,
        result: StorageProvisioningResult,
    ) -> bool:
        if claim.operation != "provision":
            return False
        result.validate(claim.tenant_id)
        async with platform_session() as session:
            async with session.begin():
                now = await _database_now(session)
                if await self._lock_claim(session, claim, now) is None:
                    return False
                if result.mode == "s3":
                    credential_statement = (
                        insert(TenantStorageCredential)
                        .values(
                            tenant_id=claim.tenant_id,
                            secret_ref=result.secret_ref,
                            access_key_fingerprint=result.access_key_fingerprint,
                            status="active",
                            rotated_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=[TenantStorageCredential.tenant_id],
                            set_={
                                "secret_ref": result.secret_ref,
                                "access_key_fingerprint": result.access_key_fingerprint,
                                "status": "active",
                                "rotated_at": now,
                                "updated_at": now,
                            },
                        )
                    )
                    await session.execute(credential_statement)
                state_statement = (
                    insert(TenantStorageState)
                    .values(
                        tenant_id=claim.tenant_id,
                        mode=result.mode,
                        policy_version=result.policy_version,
                        policy_payload=result.policy_payload,
                        policy_hash=result.policy_hash,
                        credential_secret_ref=result.secret_ref,
                        credential_fingerprint=result.access_key_fingerprint,
                        status="active",
                        verified_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[TenantStorageState.tenant_id],
                        set_={
                            "mode": result.mode,
                            "policy_version": result.policy_version,
                            "policy_payload": result.policy_payload,
                            "policy_hash": result.policy_hash,
                            "credential_secret_ref": result.secret_ref,
                            "credential_fingerprint": result.access_key_fingerprint,
                            "status": "active",
                            "verified_at": now,
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(state_statement)
                await _record_audit_once(session, claim, _AUDIT_STORAGE_READY)
                await session.flush()
                return True

    async def record_default_policy_ready(
        self,
        claim: ProvisioningClaim,
        result: TenantPolicyProvisioningResult,
    ) -> bool:
        if claim.operation != "provision":
            return False
        result.validate()
        async with platform_session() as session:
            async with session.begin():
                now = await _database_now(session)
                if await self._lock_claim(session, claim, now) is None:
                    return False
                statement = (
                    insert(TenantDefaultPolicyState)
                    .values(
                        tenant_id=claim.tenant_id,
                        policy_version=result.policy_version,
                        policy_payload=result.policy_payload,
                        policy_hash=result.policy_hash,
                        status="active",
                        verified_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[TenantDefaultPolicyState.tenant_id],
                        set_={
                            "policy_version": result.policy_version,
                            "policy_payload": result.policy_payload,
                            "policy_hash": result.policy_hash,
                            "status": "active",
                            "verified_at": now,
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(statement)
                await _record_audit_once(
                    session,
                    claim,
                    _AUDIT_DEFAULT_POLICY_READY,
                )
                await session.flush()
                return True

    async def activate(self, claim: ProvisioningClaim) -> bool:
        if claim.operation != "provision":
            return False
        async with platform_session() as session:
            async with session.begin():
                now = await _database_now(session)
                result = await session.execute(build_worker_activation_statement(claim, now))
                row = result.one_or_none()
                if row is None:
                    return False
                tenant, job = row
                tenant.status = "active"
                tenant.updated_at = now
                job.status = "completed"
                job.completed_at = now
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.updated_at = now
                await _record_audit_once(session, claim, _AUDIT_COMPLETED)
                await session.flush()
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
        validate_failure_classification(category, code, retryable)
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")
        async with platform_session() as session:
            async with session.begin():
                now = await _database_now(session)
                locked = await self._lock_claim(session, claim, now)
                if locked is None:
                    return False
                tenant, job = locked
                job.error_category = category
                job.error_code = code
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.updated_at = now
                can_retry = retryable and job.attempt_count + 1 < job.max_attempts
                if can_retry:
                    job.status = "pending"
                    job.attempt_count += 1
                    job.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                    job.started_at = None
                    job.completed_at = None
                    action = _AUDIT_RETRY_SCHEDULED
                else:
                    if claim.operation == "provision":
                        tenant.status = "failed"
                        tenant.updated_at = now
                    job.status = "failed"
                    job.completed_at = now
                    action = _AUDIT_FAILED
                await _record_audit_once(session, claim, action)
                await session.flush()
                return True
