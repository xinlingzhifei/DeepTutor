"""Async control-plane repository for tenant selection and provisioning."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql import Select

from deeptutor.teaching.database import platform_session, tenant_session
from deeptutor.teaching.models.platform import (
    AuditLog,
    Tenant,
    TenantDefaultPolicyState,
    TenantMembership,
    TenantProvisioningJob,
    TenantSchemaState,
    TenantStorageCredential,
    TenantStorageState,
)
from deeptutor.teaching.models.platform import (
    RoleGrant as RoleGrantModel,
)
from deeptutor.teaching.models.tenant import Course, TeachingClass
from deeptutor.teaching.permissions import (
    DEFAULT_ROLE_PERMISSIONS,
    KNOWN_SCOPE_TYPES,
    RoleGrant,
)
from deeptutor.teaching.provisioning_worker import (
    DEFAULT_POLICY_HASH,
    DEFAULT_POLICY_PAYLOAD,
    DEFAULT_POLICY_VERSION,
    OBJECT_STORAGE_POLICY_VERSION,
    TENANT_SCHEMA_REVISION,
    StorageProvisioningResult,
)
from deeptutor.teaching.schema_names import tenant_schema_name

_POLICY_VERIFIED_ACTION = "tenant.provisioning.policy_verified"
_PROVISIONING_JOB_RESOURCE = "provisioning_job"


class TenantRepositoryError(Exception):
    """Base class for safe tenant-domain failures."""


class TenantNotFoundError(TenantRepositoryError):
    """The requested tenant or provisioning record does not exist."""


class TenantAccessDeniedError(TenantRepositoryError):
    """The user has no active membership for the requested tenant."""


class TenantNotActiveError(TenantRepositoryError):
    """The tenant exists but is not selectable."""


class TenantSelectionRequiredError(TenantRepositoryError):
    """A request cannot infer one unambiguous active tenant."""


class TenantConflictError(TenantRepositoryError):
    """A tenant write conflicts with existing control-plane state."""


class UnknownRoleError(TenantRepositoryError):
    """At least one requested role is not a fixed role template."""


class InvalidGrantScopeError(TenantRepositoryError):
    """A requested grant has an invalid or cross-tenant scope."""


class GrantResourceNotFoundError(TenantRepositoryError):
    """A requested course or class is not active in the path tenant."""


@dataclass(frozen=True, slots=True)
class TenantSummary:
    tenant_id: str
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class TenantAccess:
    summary: TenantSummary
    schema_name: str
    roles: frozenset[str] = frozenset()
    grants: frozenset[RoleGrant] = frozenset()

    def __post_init__(self) -> None:
        if self.grants:
            object.__setattr__(
                self,
                "roles",
                frozenset(grant.role for grant in self.grants),
            )
        elif self.roles:
            object.__setattr__(
                self,
                "grants",
                frozenset(
                    RoleGrant(
                        role=role,
                        scope_type="tenant",
                        scope_id=self.summary.tenant_id,
                    )
                    for role in self.roles
                ),
            )


@dataclass(frozen=True, slots=True)
class ProvisioningSummary:
    tenant_id: str
    status: str
    job_id: str
    job_status: str
    attempt_count: int


def build_accessible_tenants_statement(
    user_id: str,
    *,
    is_platform_admin: bool,
) -> Select[Any]:
    """Build the active-tenant list query with explicit security filters."""

    statement = select(
        Tenant.id.label("tenant_id"),
        Tenant.name,
        Tenant.status,
    ).where(Tenant.status == "active")
    if not is_platform_admin:
        statement = statement.join(
            TenantMembership,
            TenantMembership.tenant_id == Tenant.id,
        ).where(
            TenantMembership.user_id == user_id,
            TenantMembership.status == "active",
        )
    return statement.order_by(Tenant.name, Tenant.id)


def build_tenant_access_statement(
    tenant_id: str,
    user_id: str,
    *,
    is_platform_admin: bool,
) -> Select[Any]:
    """Build one selectable-tenant query without accepting a raw schema."""

    statement = (
        select(
            Tenant.id.label("tenant_id"),
            Tenant.name,
            Tenant.status,
            TenantSchemaState.schema_name,
            RoleGrantModel.role.label("grant_role"),
            RoleGrantModel.scope_type.label("grant_scope_type"),
            RoleGrantModel.scope_id.label("grant_scope_id"),
        )
        .join(
            TenantSchemaState,
            (TenantSchemaState.tenant_id == Tenant.id)
            & (TenantSchemaState.status == "active"),
        )
        .where(
            Tenant.id == tenant_id,
            Tenant.status == "active",
        )
    )
    if is_platform_admin:
        statement = statement.outerjoin(
            TenantMembership,
            (TenantMembership.tenant_id == Tenant.id)
            & (TenantMembership.user_id == user_id)
            & (TenantMembership.status == "active"),
        )
    else:
        statement = statement.join(
            TenantMembership,
            TenantMembership.tenant_id == Tenant.id,
        ).where(
            TenantMembership.user_id == user_id,
            TenantMembership.status == "active",
        )
    return statement.outerjoin(
        RoleGrantModel,
        (RoleGrantModel.tenant_id == TenantMembership.tenant_id)
        & (RoleGrantModel.user_id == TenantMembership.user_id),
    )


def _summary_from_row(row: Any) -> TenantSummary:
    return TenantSummary(
        tenant_id=str(row["tenant_id"]),
        name=str(row["name"]),
        status=str(row["status"]),
    )


def _validate_roles(roles: frozenset[str]) -> None:
    if not roles or not roles.issubset(DEFAULT_ROLE_PERMISSIONS):
        raise UnknownRoleError("unknown role")


def _tenant_role_grants(
    tenant_id: str,
    roles: frozenset[str],
) -> frozenset[RoleGrant]:
    return frozenset(
        RoleGrant(
            role=role,
            scope_type="tenant",
            scope_id=tenant_id,
        )
        for role in roles
    )


def _validate_scoped_grants(
    tenant_id: str,
    grants: frozenset[RoleGrant],
) -> None:
    if not grants:
        raise InvalidGrantScopeError("at least one grant is required")
    _validate_roles(frozenset(grant.role for grant in grants))
    for grant in grants:
        if grant.scope_type not in KNOWN_SCOPE_TYPES or not grant.scope_id:
            raise InvalidGrantScopeError("invalid grant scope")
        if grant.scope_type == "tenant" and grant.scope_id != tenant_id:
            raise InvalidGrantScopeError("tenant grant scope does not match path tenant")


def _advisory_lock_key(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def build_active_tenant_lock_statement(tenant_id: str) -> Select[Any]:
    return (
        select(Tenant.id)
        .where(
            Tenant.id == tenant_id,
            Tenant.status == "active",
        )
        .with_for_update()
    )


def build_membership_upsert_statement(
    tenant_id: str,
    user_id: str,
) -> Any:
    return (
        insert(TenantMembership)
        .values(
            tenant_id=tenant_id,
            user_id=user_id,
            status="active",
        )
        .on_conflict_do_update(
            index_elements=[
                TenantMembership.tenant_id,
                TenantMembership.user_id,
            ],
            set_={"status": "active", "updated_at": func.now()},
        )
    )


def build_role_delete_statement(tenant_id: str, user_id: str) -> Any:
    return delete(RoleGrantModel).where(
        RoleGrantModel.tenant_id == tenant_id,
        RoleGrantModel.user_id == user_id,
    )


def build_scoped_role_insert_statement(
    tenant_id: str,
    user_id: str,
    grants: frozenset[RoleGrant],
) -> Any:
    return insert(RoleGrantModel).values(
        [
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "role": grant.role,
                "scope_type": grant.scope_type,
                "scope_id": grant.scope_id,
            }
            for grant in sorted(
                grants,
                key=lambda item: (item.role, item.scope_type, item.scope_id),
            )
        ]
    )


def build_role_insert_statement(
    tenant_id: str,
    user_id: str,
    roles: frozenset[str],
) -> Any:
    return build_scoped_role_insert_statement(
        tenant_id,
        user_id,
        _tenant_role_grants(tenant_id, roles),
    )


def build_active_membership_lock_statement(
    tenant_id: str,
    user_id: str,
) -> Select[Any]:
    return (
        select(TenantMembership.user_id)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .where(
            Tenant.id == tenant_id,
            Tenant.status == "active",
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.user_id == user_id,
            TenantMembership.status == "active",
        )
        .with_for_update()
    )


def build_course_scope_validation_statement(
    course_ids: frozenset[str],
) -> Select[Any]:
    return select(Course.id).where(
        Course.id.in_(sorted(course_ids)),
        Course.status == "active",
    )


def build_class_scope_validation_statement(
    class_ids: frozenset[str],
) -> Select[Any]:
    return (
        select(TeachingClass.id, TeachingClass.course_id)
        .join(Course, Course.id == TeachingClass.course_id)
        .where(
            TeachingClass.id.in_(sorted(class_ids)),
            TeachingClass.status == "active",
            Course.status == "active",
        )
    )


def build_provisioning_advisory_lock_statement(
    tenant_id: str,
) -> Select[Any]:
    return select(func.pg_advisory_xact_lock(_advisory_lock_key(tenant_id)))


def build_failed_tenant_retry_statement(tenant_id: str) -> Any:
    return (
        update(Tenant)
        .where(
            Tenant.id == tenant_id,
            Tenant.status == "failed",
        )
        .values(
            status="provisioning",
            updated_at=func.now(),
        )
    )


def build_failed_job_retry_statement(
    tenant_id: str,
    job_id: str,
    expected_attempt_count: int,
) -> Any:
    return (
        update(TenantProvisioningJob)
        .where(
            TenantProvisioningJob.id == job_id,
            TenantProvisioningJob.tenant_id == tenant_id,
            TenantProvisioningJob.operation == "provision",
            TenantProvisioningJob.status == "failed",
            TenantProvisioningJob.attempt_count == expected_attempt_count,
        )
        .values(
            status="pending",
            attempt_count=TenantProvisioningJob.attempt_count + 1,
            next_attempt_at=func.now(),
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            error_category=None,
            error_code=None,
            started_at=None,
            completed_at=None,
            updated_at=func.now(),
        )
    )


def _policy_resource_id(job_id: str, attempt_count: int) -> str:
    return f"{job_id}:{attempt_count}"


def build_worker_attempt_lock_statement(
    tenant_id: str,
    job_id: str,
    expected_attempt_count: int,
) -> Select[Any]:
    return (
        select(Tenant, TenantProvisioningJob)
        .join(
            TenantProvisioningJob,
            TenantProvisioningJob.tenant_id == Tenant.id,
        )
        .where(
            Tenant.id == tenant_id,
            Tenant.status == "provisioning",
            TenantProvisioningJob.id == job_id,
            TenantProvisioningJob.tenant_id == tenant_id,
            TenantProvisioningJob.operation == "provision",
            TenantProvisioningJob.attempt_count == expected_attempt_count,
            TenantProvisioningJob.status.in_(("pending", "running")),
        )
        .with_for_update()
    )


def build_activation_lock_statement(
    tenant_id: str,
    job_id: str,
    expected_attempt_count: int,
) -> Select[Any]:
    expected_storage = StorageProvisioningResult.local(tenant_id)
    return (
        select(Tenant, TenantProvisioningJob)
        .join(
            TenantProvisioningJob,
            TenantProvisioningJob.tenant_id == Tenant.id,
        )
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
            Tenant.id == tenant_id,
            Tenant.status == "provisioning",
            TenantProvisioningJob.id == job_id,
            TenantProvisioningJob.tenant_id == tenant_id,
            TenantProvisioningJob.operation == "provision",
            TenantProvisioningJob.status.in_(("pending", "running")),
            TenantProvisioningJob.attempt_count == expected_attempt_count,
            TenantSchemaState.tenant_id == tenant_id,
            TenantSchemaState.status == "active",
            TenantSchemaState.revision == TENANT_SCHEMA_REVISION,
            TenantSchemaState.schema_name == tenant_schema_name(tenant_id),
            TenantStorageState.tenant_id == tenant_id,
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
                    TenantStorageState.credential_secret_ref
                    == TenantStorageCredential.secret_ref,
                    TenantStorageState.credential_fingerprint
                    == TenantStorageCredential.access_key_fingerprint,
                ),
            ),
            TenantDefaultPolicyState.tenant_id == tenant_id,
            TenantDefaultPolicyState.status == "active",
            TenantDefaultPolicyState.policy_version == DEFAULT_POLICY_VERSION,
            TenantDefaultPolicyState.policy_payload == DEFAULT_POLICY_PAYLOAD,
            TenantDefaultPolicyState.policy_hash == DEFAULT_POLICY_HASH,
        )
        .limit(1)
        .with_for_update(of=(Tenant, TenantProvisioningJob))
    )


def _expect_single_update(result: Any, operation: str) -> None:
    if result.rowcount != 1:
        raise TenantConflictError(f"{operation} state changed concurrently")


class TenantRepository:
    """Small request-stateless repository backed by ``platform_session``."""

    async def list_tenants(
        self,
        user_id: str,
        *,
        is_platform_admin: bool,
    ) -> tuple[TenantSummary, ...]:
        async with platform_session() as session:
            result = await session.execute(
                build_accessible_tenants_statement(
                    user_id,
                    is_platform_admin=is_platform_admin,
                )
            )
            return tuple(_summary_from_row(row) for row in result.mappings().all())

    async def get_tenant_access(
        self,
        tenant_id: str,
        user_id: str,
        *,
        is_platform_admin: bool,
    ) -> TenantAccess:
        async with platform_session() as session:
            result = await session.execute(
                build_tenant_access_statement(
                    tenant_id,
                    user_id,
                    is_platform_admin=is_platform_admin,
                )
            )
            rows = result.mappings().all()
            if not rows:
                inactive_statement = (
                    select(Tenant.id)
                    .outerjoin(
                        TenantSchemaState,
                        TenantSchemaState.tenant_id == Tenant.id,
                    )
                    .where(
                        Tenant.id == tenant_id,
                        or_(
                            Tenant.status != "active",
                            TenantSchemaState.tenant_id.is_(None),
                            TenantSchemaState.status != "active",
                        ),
                    )
                )
                if not is_platform_admin:
                    inactive_statement = inactive_statement.join(
                        TenantMembership,
                        TenantMembership.tenant_id == Tenant.id,
                    ).where(
                        TenantMembership.user_id == user_id,
                        TenantMembership.status == "active",
                    )
                inactive = await session.scalar(inactive_statement)
                if inactive is not None:
                    raise TenantNotActiveError(tenant_id)
                if is_platform_admin:
                    raise TenantNotFoundError(tenant_id)
                raise TenantAccessDeniedError(tenant_id)

            grants = frozenset(
                RoleGrant(
                    role=str(row["grant_role"]),
                    scope_type=str(row["grant_scope_type"]),
                    scope_id=str(row["grant_scope_id"]),
                )
                for row in rows
                if row["grant_role"] is not None
            )
            row = rows[0]
            schema_name = str(row["schema_name"])
            return TenantAccess(
                summary=_summary_from_row(row),
                schema_name=schema_name,
                grants=grants,
            )

    async def create_provisioning(
        self,
        *,
        tenant_id: str,
        job_id: str,
        name: str,
    ) -> ProvisioningSummary:
        """Create one tenant/job pair, serialized by its opaque tenant ID."""

        async with platform_session() as session:
            async with session.begin():
                await session.execute(build_provisioning_advisory_lock_statement(tenant_id))
                existing_result = await session.execute(
                    select(
                        Tenant.id.label("tenant_id"),
                        Tenant.name,
                        Tenant.status,
                        TenantProvisioningJob.id.label("job_id"),
                        TenantProvisioningJob.status.label("job_status"),
                        TenantProvisioningJob.attempt_count,
                    )
                    .join(
                        TenantProvisioningJob,
                        TenantProvisioningJob.tenant_id == Tenant.id,
                    )
                    .where(
                        Tenant.id == tenant_id,
                        Tenant.status.in_(("provisioning", "active", "failed")),
                        TenantProvisioningJob.id == job_id,
                        TenantProvisioningJob.tenant_id == tenant_id,
                        TenantProvisioningJob.operation == "provision",
                        TenantProvisioningJob.status.in_(
                            ("pending", "running", "completed", "failed")
                        ),
                    )
                )
                existing = existing_result.mappings().one_or_none()
                if existing is not None:
                    if str(existing["name"]) != name:
                        raise TenantConflictError("idempotency payload conflict")
                    tenant_status = str(existing["status"])
                    job_status = str(existing["job_status"])
                    if "failed" in {tenant_status, job_status}:
                        if (tenant_status, job_status) != ("failed", "failed"):
                            raise TenantConflictError("provisioning retry state is inconsistent")
                        tenant_update = await session.execute(
                            build_failed_tenant_retry_statement(tenant_id)
                        )
                        job_update = await session.execute(
                            build_failed_job_retry_statement(
                                tenant_id,
                                job_id,
                                int(existing["attempt_count"]),
                            )
                        )
                        _expect_single_update(
                            tenant_update,
                            "tenant provisioning retry",
                        )
                        _expect_single_update(
                            job_update,
                            "job provisioning retry",
                        )
                        await session.flush()
                        return ProvisioningSummary(
                            tenant_id=str(existing["tenant_id"]),
                            status="provisioning",
                            job_id=str(existing["job_id"]),
                            job_status="pending",
                            attempt_count=int(existing["attempt_count"]) + 1,
                        )
                    return ProvisioningSummary(
                        tenant_id=str(existing["tenant_id"]),
                        status=tenant_status,
                        job_id=str(existing["job_id"]),
                        job_status=job_status,
                        attempt_count=int(existing["attempt_count"]),
                    )

                collision = await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id))
                if collision is not None:
                    raise TenantConflictError("tenant identifier conflict")

                session.add(
                    Tenant(
                        id=tenant_id,
                        name=name,
                        status="provisioning",
                    )
                )
                session.add(
                    TenantProvisioningJob(
                        id=job_id,
                        tenant_id=tenant_id,
                        operation="provision",
                        status="pending",
                        attempt_count=0,
                    )
                )
                await session.flush()
                return ProvisioningSummary(
                    tenant_id=tenant_id,
                    status="provisioning",
                    job_id=job_id,
                    job_status="pending",
                    attempt_count=0,
                )

    async def get_provisioning(self, tenant_id: str) -> ProvisioningSummary:
        async with platform_session() as session:
            result = await session.execute(
                select(
                    Tenant.id.label("tenant_id"),
                    Tenant.status,
                    TenantProvisioningJob.id.label("job_id"),
                    TenantProvisioningJob.status.label("job_status"),
                    TenantProvisioningJob.attempt_count,
                )
                .join(
                    TenantProvisioningJob,
                    TenantProvisioningJob.tenant_id == Tenant.id,
                )
                .where(
                    Tenant.id == tenant_id,
                    Tenant.status.in_(("provisioning", "active", "failed")),
                    TenantProvisioningJob.tenant_id == tenant_id,
                    TenantProvisioningJob.operation == "provision",
                    TenantProvisioningJob.status.in_(("pending", "running", "completed", "failed")),
                )
                .order_by(TenantProvisioningJob.created_at.desc())
                .limit(1)
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise TenantNotFoundError(tenant_id)
            return ProvisioningSummary(
                tenant_id=str(row["tenant_id"]),
                status=str(row["status"]),
                job_id=str(row["job_id"]),
                job_status=str(row["job_status"]),
                attempt_count=int(row["attempt_count"]),
            )

    async def activate_if_ready(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        """Activate one exact current attempt with persisted prerequisites."""

        async with platform_session() as session:
            async with session.begin():
                result = await session.execute(
                    build_activation_lock_statement(
                        tenant_id,
                        job_id,
                        expected_attempt_count,
                    )
                )
                row = result.one_or_none()
                if row is None:
                    return False
                tenant, job = row
                tenant.status = "active"
                job.status = "completed"
                await session.flush()
                return True

    async def mark_provisioning_failed(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        """Atomically fail one current attempt without changing its count."""

        async with platform_session() as session:
            async with session.begin():
                result = await session.execute(
                    build_worker_attempt_lock_statement(
                        tenant_id,
                        job_id,
                        expected_attempt_count,
                    )
                )
                row = result.one_or_none()
                if row is None:
                    return False
                tenant, job = row
                tenant.status = "failed"
                job.status = "failed"
                await session.flush()
                return True

    async def record_policy_verified(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        """Persist the fixed policy event for one exact current attempt."""

        async with platform_session() as session:
            async with session.begin():
                result = await session.execute(
                    build_worker_attempt_lock_statement(
                        tenant_id,
                        job_id,
                        expected_attempt_count,
                    )
                )
                if result.one_or_none() is None:
                    return False
                session.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        actor_id=None,
                        action=_POLICY_VERIFIED_ACTION,
                        resource_type=_PROVISIONING_JOB_RESOURCE,
                        resource_id=_policy_resource_id(
                            job_id,
                            expected_attempt_count,
                        ),
                    )
                )
                await session.flush()
                return True

    async def upsert_member(
        self,
        tenant_id: str,
        user_id: str,
        roles: frozenset[str],
    ) -> None:
        """Transactionally upsert an active membership and its initial roles."""

        _validate_roles(roles)
        await self.upsert_member_with_scoped_grants(
            tenant_id,
            user_id,
            _tenant_role_grants(tenant_id, roles),
        )

    async def upsert_member_with_scoped_grants(
        self,
        tenant_id: str,
        user_id: str,
        grants: frozenset[RoleGrant],
    ) -> None:
        """Validate resources before atomically activating membership and grants."""

        _validate_scoped_grants(tenant_id, grants)
        async with tenant_session(tenant_id) as session:
            async with session.begin():
                active_tenant = await session.scalar(build_active_tenant_lock_statement(tenant_id))
                if active_tenant is None:
                    raise TenantNotFoundError(tenant_id)
                await self._validate_grant_resources(session, grants)
                await session.execute(build_membership_upsert_statement(tenant_id, user_id))
                await self._replace_scoped_roles(
                    session,
                    tenant_id,
                    user_id,
                    grants,
                )

    async def replace_grants(
        self,
        tenant_id: str,
        user_id: str,
        roles: frozenset[str],
    ) -> None:
        """Replace one active member's role set inside one transaction."""

        _validate_roles(roles)
        await self.replace_scoped_grants(
            tenant_id,
            user_id,
            _tenant_role_grants(tenant_id, roles),
        )

    async def replace_scoped_grants(
        self,
        tenant_id: str,
        user_id: str,
        grants: frozenset[RoleGrant],
    ) -> None:
        """Validate and atomically replace one active member's scoped grants."""

        _validate_scoped_grants(tenant_id, grants)
        async with tenant_session(tenant_id) as session:
            async with session.begin():
                membership = await session.scalar(
                    build_active_membership_lock_statement(tenant_id, user_id)
                )
                if membership is None:
                    raise TenantAccessDeniedError(tenant_id)
                await self._validate_grant_resources(session, grants)
                await self._replace_scoped_roles(
                    session,
                    tenant_id,
                    user_id,
                    grants,
                )

    @staticmethod
    async def _validate_grant_resources(
        session: Any,
        grants: frozenset[RoleGrant],
    ) -> None:
        course_ids = frozenset(grant.scope_id for grant in grants if grant.scope_type == "course")
        if course_ids:
            result = await session.execute(build_course_scope_validation_statement(course_ids))
            found_courses = frozenset(str(value) for value in result.scalars().all())
            if found_courses != course_ids:
                raise GrantResourceNotFoundError("course is not active in tenant")

        class_ids = frozenset(grant.scope_id for grant in grants if grant.scope_type == "class")
        if class_ids:
            result = await session.execute(build_class_scope_validation_statement(class_ids))
            found_classes = frozenset(str(row[0]) for row in result.all())
            if found_classes != class_ids:
                raise GrantResourceNotFoundError("class is not active in tenant")

    @staticmethod
    async def _replace_scoped_roles(
        session: Any,
        tenant_id: str,
        user_id: str,
        grants: frozenset[RoleGrant],
    ) -> None:
        await session.execute(build_role_delete_statement(tenant_id, user_id))
        await session.execute(
            build_scoped_role_insert_statement(
                tenant_id,
                user_id,
                grants,
            )
        )
        await session.flush()


def get_tenant_repository() -> TenantRepository:
    """FastAPI-replaceable repository dependency."""

    return TenantRepository()
