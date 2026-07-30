"""ORM models stored in the fixed ``platform`` schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class PlatformBase(DeclarativeBase):
    """Declarative base for control-plane data."""

    metadata = MetaData(schema="platform", naming_convention=_NAMING_CONVENTION)


class Tenant(PlatformBase):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32),
        server_default="provisioning",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_tenants_status", "status"),)


class TenantMembership(PlatformBase):
    __tablename__ = "tenant_memberships"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_tenant_memberships_user_id", "user_id"),)


class RoleGrant(PlatformBase):
    __tablename__ = "role_grants"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            [
                "platform.tenant_memberships.tenant_id",
                "platform.tenant_memberships.user_id",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "scope_type IN ('tenant', 'course', 'class')",
            name="scope_type",
        ),
        Index("ix_role_grants_user_id", "user_id"),
        Index(
            "ix_role_grants_tenant_user_scope",
            "tenant_id",
            "user_id",
            "scope_type",
            "scope_id",
        ),
    )


class DataPlaneRoute(PlatformBase):
    __tablename__ = "data_plane_routes"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    schema_name: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_data_plane_routes_status", "status"),)


class TenantSchemaState(PlatformBase):
    __tablename__ = "tenant_schema_states"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    schema_name: Mapped[str] = mapped_column(String(64), unique=True)
    revision: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_tenant_schema_states_status", "status"),)


class TenantStorageCredential(PlatformBase):
    __tablename__ = "tenant_storage_credentials"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    secret_ref: Mapped[str] = mapped_column(String(512), unique=True)
    access_key_fingerprint: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class TenantStorageState(PlatformBase):
    __tablename__ = "tenant_storage_states"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mode: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_payload: Mapped[str] = mapped_column(Text)
    policy_hash: Mapped[str] = mapped_column(String(64))
    credential_secret_ref: Mapped[str | None] = mapped_column(String(512))
    credential_fingerprint: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_tenant_storage_states_status", "status"),)


class TenantDefaultPolicyState(PlatformBase):
    __tablename__ = "tenant_default_policy_states"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_payload: Mapped[str] = mapped_column(Text)
    policy_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_tenant_default_policy_states_status", "status"),)


class TenantProvisioningJob(PlatformBase):
    __tablename__ = "tenant_provisioning_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
    )
    operation: Mapped[str] = mapped_column(String(32), server_default="provision")
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_category: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_tenant_provisioning_jobs_tenant_status", "tenant_id", "status"),
        Index(
            "ix_tenant_provisioning_jobs_claim",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )


class AuditLog(PlatformBase):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="SET NULL"),
    )
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),)
