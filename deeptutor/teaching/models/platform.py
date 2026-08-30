"""ORM models stored in the fixed ``platform`` schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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
    data_plane_mode: Mapped[str] = mapped_column(
        String(16),
        server_default="shared",
    )

    __table_args__ = (
        CheckConstraint(
            "data_plane_mode IN ('shared', 'dedicated')",
            name="data_plane_mode",
        ),
        Index("ix_tenants_status", "status"),
    )


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


class TenantKnowledgeEntitlement(PlatformBase):
    __tablename__ = "tenant_knowledge_entitlements"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    knowledge_resource_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    resource_owner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    granted_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        CheckConstraint(
            "knowledge_resource_id ~ "
            "'^(admin|user):kb:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            "[0-9a-f]{4}-[0-9a-f]{12}$'",
            name="resource_id",
        ),
        Index(
            "ix_tenant_knowledge_entitlements_resource_owner_status",
            "knowledge_resource_id",
            "resource_owner_id",
            "status",
        ),
    )


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


class ProviderProfile(PlatformBase):
    __tablename__ = "provider_profiles"

    id: Mapped[str] = mapped_column(String(63), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))
    tenant_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
    )
    owner_key: Mapped[str] = mapped_column(String(64))
    provider_type: Mapped[str] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(128))
    api_base_url: Mapped[str | None] = mapped_column(String(512))
    secret_ref: Mapped[str] = mapped_column(String(512), unique=True)
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "scope IN ('shared', 'dedicated')",
            name="scope",
        ),
        CheckConstraint(
            "("
            "scope = 'shared' AND tenant_id IS NULL AND owner_key = 'shared'"
            ") OR ("
            "scope = 'dedicated' AND tenant_id IS NOT NULL "
            "AND owner_key = tenant_id"
            ")",
            name="owner_scope",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="status",
        ),
        UniqueConstraint(
            "id",
            "scope",
            "owner_key",
            name="uq_provider_profiles_route_binding",
        ),
        Index(
            "ix_provider_profiles_tenant_status",
            "tenant_id",
            "status",
        ),
    )


class DataPlaneRoute(PlatformBase):
    __tablename__ = "data_plane_routes"

    id: Mapped[str] = mapped_column(String(63), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
    )
    owner_key: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    base_url: Mapped[str] = mapped_column(String(512))
    worker_pool: Mapped[str] = mapped_column(String(128), unique=True)
    queue_name: Mapped[str] = mapped_column(String(128), unique=True)
    provider_profile_id: Mapped[str] = mapped_column(String(63))
    status: Mapped[str] = mapped_column(String(32), server_default="active")
    health_status: Mapped[str] = mapped_column(
        String(32),
        server_default="unknown",
    )
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_profile_id", "mode", "owner_key"],
            [
                "platform.provider_profiles.id",
                "platform.provider_profiles.scope",
                "platform.provider_profiles.owner_key",
            ],
            name="fk_data_plane_routes_provider_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "mode IN ('shared', 'dedicated')",
            name="mode",
        ),
        CheckConstraint(
            "("
            "mode = 'shared' AND tenant_id IS NULL AND owner_key = 'shared'"
            ") OR ("
            "mode = 'dedicated' AND tenant_id IS NOT NULL "
            "AND owner_key = tenant_id"
            ")",
            name="owner_mode",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="status",
        ),
        CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'unhealthy')",
            name="health_status",
        ),
        UniqueConstraint(
            "tenant_id",
            name="uq_data_plane_routes_tenant_id",
        ),
        Index(
            "uq_data_plane_routes_global_shared",
            "mode",
            unique=True,
            postgresql_where=text("mode = 'shared'"),
        ),
        Index(
            "ix_data_plane_routes_status_health",
            "status",
            "health_status",
        ),
    )


class GenerationRouteAttempt(PlatformBase):
    """Append-only worker routing fact for one claimed job attempt."""

    __tablename__ = "generation_route_attempts"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer, primary_key=True)
    phase: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(16))
    data_plane_mode: Mapped[str] = mapped_column(String(16))
    data_plane_route_id: Mapped[str] = mapped_column(String(63))
    provider_profile_id: Mapped[str] = mapped_column(String(63))
    worker_pool_ref: Mapped[str] = mapped_column(String(128))
    queue_ref: Mapped[str] = mapped_column(String(128))
    worker_id: Mapped[str] = mapped_column(String(128))
    config_revision: Mapped[str | None] = mapped_column(String(32))
    route_config_digest: Mapped[str | None] = mapped_column(String(64))
    provider_config_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("attempt_count > 0", name="attempt_count"),
        CheckConstraint(
            "phase IN ('outline', 'content', 'export')",
            name="phase",
        ),
        CheckConstraint(
            "decision IN ('selected', 'unavailable')",
            name="decision",
        ),
        CheckConstraint(
            "data_plane_mode IN ('shared', 'dedicated')",
            name="data_plane_mode",
        ),
        CheckConstraint(
            "length(btrim(tenant_id)) > 0 "
            "AND length(btrim(job_id)) > 0 "
            "AND length(btrim(data_plane_route_id)) > 0 "
            "AND length(btrim(provider_profile_id)) > 0 "
            "AND length(btrim(worker_pool_ref)) > 0 "
            "AND length(btrim(queue_ref)) > 0 "
            "AND length(btrim(worker_id)) > 0",
            name="bindings_not_empty",
        ),
        CheckConstraint(
            "(decision = 'selected' AND config_revision = 'route-binding-v1' "
            "AND route_config_digest ~ '^[0-9a-f]{64}$' "
            "AND route_config_digest <> "
            "'0000000000000000000000000000000000000000000000000000000000000000' "
            "AND provider_config_digest ~ '^[0-9a-f]{64}$' "
            "AND provider_config_digest <> "
            "'0000000000000000000000000000000000000000000000000000000000000000') OR "
            "(decision = 'unavailable' AND config_revision IS NULL "
            "AND route_config_digest IS NULL AND provider_config_digest IS NULL)",
            name="configuration_binding",
        ),
        Index(
            "ix_generation_route_attempts_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )


class TeachingMetricCounterRollup(PlatformBase):
    """Internally sharded absolute counters for the fixed public metric contract."""

    __tablename__ = "teaching_metric_counter_rollups"

    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), primary_key=True)
    shard: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    total: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("shard BETWEEN 0 AND 15", name="shard"),
        CheckConstraint("total >= 0", name="total"),
        CheckConstraint(
            "(metric = 'generation_jobs_total' "
            "AND category IN ('queued', 'running', 'completed', 'failed', 'canceled')) "
            "OR (metric = 'generation_retries_total' "
            "AND category IN "
            "('timeout', 'unavailable', 'lease_lost', 'rate_limited', 'unknown')) "
            "OR (metric = 'quota_units_total' "
            "AND category IN ('reserved', 'consumed', 'released')) "
            "OR (metric = 'learning_events_total' "
            "AND category IN "
            "('classroom.started', 'scene.completed', 'quiz.graded', 'hint.used', "
            "'pbl.milestone_completed', 'classroom.completed')) "
            "OR (metric = 'artifact_validation_failures_total' "
            "AND category IN "
            "('schema_invalid', 'receipt_mismatch', 'hash_mismatch', 'size_mismatch', "
            "'missing_artifact', 'unknown'))",
            name="metric_category",
        ),
    )


class TeachingMetricHistogramRollup(PlatformBase):
    """Noncumulative fixed-bucket bins summed into public histograms at scrape time."""

    __tablename__ = "teaching_metric_histogram_rollups"

    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), primary_key=True)
    shard: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, server_default="0")
    sum_seconds: Mapped[float] = mapped_column(Float, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("shard BETWEEN 0 AND 15", name="shard"),
        CheckConstraint("count >= 0", name="count"),
        CheckConstraint(
            "sum_seconds >= 0 AND sum_seconds < 'Infinity'::double precision",
            name="sum_seconds",
        ),
        CheckConstraint(
            "count > 0 OR sum_seconds = 0",
            name="count_sum",
        ),
        CheckConstraint(
            "(metric = 'generation_queue_seconds' AND category = '' "
            "AND bucket IN "
            "('0.1', '0.5', '1', '2', '5', '10', '30', '60', '120', '300', '+Inf')) "
            "OR (metric = 'generation_stage_seconds' "
            "AND category IN ('outline', 'content', 'export') "
            "AND bucket IN "
            "('0.5', '1', '2', '5', '10', '30', '60', '120', '300', '900', "
            "'1800', '+Inf'))",
            name="metric_category_bucket",
        ),
    )


class TeachingLearningProjectionBacklog(PlatformBase):
    """Platform mirror of current nonterminal tenant projection work."""

    __tablename__ = "teaching_learning_projection_backlog"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_teaching_projection_backlog_received_at", "received_at"),)


class TenantSchemaState(PlatformBase):
    """Authoritative tenant-migration ledger advanced only after successful migrations.

    Health checks use this ledger as migration evidence. Physical schema changes made
    out of band are outside that evidence boundary.
    """

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
    target_revision: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
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
        CheckConstraint(
            "operation IN ('provision', 'upgrade_schema')",
            name="operation",
        ),
        CheckConstraint(
            "(operation = 'provision' AND target_revision IS NULL) OR "
            "(operation = 'upgrade_schema' AND target_revision IS NOT NULL)",
            name="operation_target",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count < max_attempts",
            name="attempts",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL) OR (status != 'running' "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
            name="status_lease_fence",
        ),
        UniqueConstraint(
            "tenant_id",
            "operation",
            "target_revision",
            name="uq_tenant_provisioning_jobs_upgrade_target",
        ),
        Index("ix_tenant_provisioning_jobs_tenant_status", "tenant_id", "status"),
        Index(
            "ix_tenant_provisioning_jobs_claim",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )


class TeachingRuntimeProcessHeartbeat(PlatformBase):
    __tablename__ = "teaching_runtime_process_heartbeats"

    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    status: Mapped[str] = mapped_column(String(16), server_default="running")
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("length(btrim(role)) > 0", name="role_not_empty"),
        CheckConstraint(
            "role IN ('tenant_provisioner', 'dispatcher', 'generation_worker', "
            "'export_worker', 'projector', 'reaper')",
            name="role",
        ),
        CheckConstraint(
            "length(btrim(instance_id)) > 0",
            name="instance_id_not_empty",
        ),
        CheckConstraint("status IN ('running', 'stopped')", name="status"),
        CheckConstraint(
            "(status = 'running' AND stopped_at IS NULL) OR "
            "(status = 'stopped' AND stopped_at IS NOT NULL)",
            name="status_stopped_at",
        ),
        CheckConstraint(
            "heartbeat_at >= started_at AND updated_at >= started_at "
            "AND (stopped_at IS NULL OR stopped_at >= started_at)",
            name="timestamps",
        ),
        Index(
            "ix_teaching_runtime_process_heartbeats_role_heartbeat_running",
            "role",
            heartbeat_at.desc(),
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "ix_teaching_runtime_process_heartbeats_heartbeat_running_ttl",
            "heartbeat_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "ix_teaching_runtime_process_heartbeats_stopped_at_retention",
            "stopped_at",
            postgresql_where=text("status = 'stopped'"),
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
