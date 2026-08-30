"""Durable generation job, queue, quota, and scheduler models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .classrooms import ClassroomVersion as ClassroomVersion
from .platform import PlatformBase, Tenant
from .tenant import TenantBase

JOB_KINDS = frozenset({"generation", "export"})
JOB_PHASES = frozenset({"outline", "content", "export"})
EXPORT_FORMATS = frozenset({"classroom_zip", "pptx", "offline_html", "mp4"})
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "canceled"})
LEASED_JOB_STATUSES = frozenset(
    {
        "generating_outline",
        "generating_content",
        "exporting",
        "validating",
        "materializing",
    }
)
JOB_STATUSES = frozenset(
    {
        "created",
        "quota_reserved",
        "queued",
        "generating_outline",
        "awaiting_confirmation",
        "generating_content",
        "exporting",
        "validating",
        "materializing",
        *TERMINAL_JOB_STATUSES,
    }
)

_GENERATION_TRANSITIONS = {
    "created": {"quota_reserved"},
    "quota_reserved": {"queued"},
    "queued": {"generating_outline", "generating_content"},
    "generating_outline": {"awaiting_confirmation"},
    "awaiting_confirmation": {"queued"},
    "generating_content": {"validating"},
    "validating": {"materializing"},
    "materializing": {"succeeded"},
}
_EXPORT_TRANSITIONS = {
    "created": {"quota_reserved"},
    "quota_reserved": {"queued"},
    "queued": {"exporting"},
    "exporting": {"validating"},
    "validating": {"materializing"},
    "materializing": {"succeeded"},
}


class InvalidJobTransition(ValueError):
    """The requested transition is outside the persisted job state machine."""


def require_job_transition(
    job_kind: str,
    current_status: str,
    target_status: str,
) -> None:
    """Validate one conditional job transition before issuing its UPDATE."""

    transitions = {
        "generation": _GENERATION_TRANSITIONS,
        "export": _EXPORT_TRANSITIONS,
    }.get(job_kind)
    if transitions is None:
        raise InvalidJobTransition("unknown job kind")
    if current_status in TERMINAL_JOB_STATUSES:
        raise InvalidJobTransition("terminal jobs cannot transition")
    if current_status not in transitions:
        raise InvalidJobTransition("unknown job status")
    allowed = set(transitions.get(current_status, ()))
    allowed.update({"failed", "canceled"})
    if target_status not in allowed:
        raise InvalidJobTransition("job transition is not allowed")


class GenerationJob(TenantBase):
    """Tenant-owned source of truth for one generation or export lifecycle."""

    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"),
    )
    job_kind: Mapped[str] = mapped_column(String(16))
    phase: Mapped[str] = mapped_column(String(16))
    export_format: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), server_default="created")
    priority: Mapped[int] = mapped_column(Integer)
    quota_units: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[str] = mapped_column(String(128))
    visibility: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    classroom_draft_id: Mapped[str | None] = mapped_column(String(64))
    batch_id: Mapped[str | None] = mapped_column(String(64))
    resource_course_id: Mapped[str | None] = mapped_column(String(64))
    resource_class_id: Mapped[str | None] = mapped_column(String(64))
    public_request_sha256: Mapped[str | None] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64))
    data_plane_mode: Mapped[str | None] = mapped_column(String(16))
    data_plane_route_id: Mapped[str] = mapped_column(String(63))
    provider_profile_id: Mapped[str] = mapped_column(String(63))
    worker_pool_ref: Mapped[str] = mapped_column(String(128))
    queue_ref: Mapped[str] = mapped_column(String(128))
    request_payload: Mapped[str] = mapped_column(Text)
    progress_percent: Mapped[int] = mapped_column(Integer, server_default="0")
    waiting_reason: Mapped[str | None] = mapped_column(String(64))
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
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default="false")
    error_category: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    result_ref: Mapped[str | None] = mapped_column(String(512))
    artifact_manifest_ref: Mapped[str | None] = mapped_column(String(512))
    result_payload: Mapped[str | None] = mapped_column(Text)
    retry_of_job_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tenant.generation_jobs.id", ondelete="RESTRICT"),
    )
    dsl_repair_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
            "job_kind IN ('generation', 'export')",
            name="job_kind",
        ),
        CheckConstraint(
            "(job_kind = 'generation' AND phase = 'outline' "
            "AND export_format IS NULL AND status IN ("
            "'created', 'quota_reserved', 'queued', 'generating_outline', "
            "'awaiting_confirmation', 'failed', 'canceled'"
            ")) OR ("
            "job_kind = 'generation' AND phase = 'content' "
            "AND export_format IS NULL AND status IN ("
            "'created', 'quota_reserved', 'queued', 'generating_content', "
            "'validating', 'materializing', 'succeeded', 'failed', 'canceled'"
            ")) OR ("
            "job_kind = 'export' AND phase = 'export' "
            "AND export_format IN ('classroom_zip', 'pptx', 'offline_html', 'mp4') "
            "AND status IN ("
            "'created', 'quota_reserved', 'queued', 'exporting', 'validating', "
            "'materializing', 'succeeded', 'failed', 'canceled'"
            "))",
            name="kind_phase_format_status",
        ),
        CheckConstraint(
            "visibility IN ('private', 'class', 'tenant')",
            name="visibility",
        ),
        CheckConstraint(
            "data_plane_mode IS NULL OR data_plane_mode IN ('shared', 'dedicated')",
            name="data_plane_mode",
        ),
        CheckConstraint("priority >= 0", name="priority"),
        CheckConstraint("quota_units > 0", name="quota_units"),
        CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="progress_percent",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="attempts",
        ),
        CheckConstraint(
            "dsl_repair_attempts >= 0 AND dsl_repair_attempts <= 2",
            name="dsl_repair_attempts",
        ),
        CheckConstraint(
            "("
            "status IN ("
            "'generating_outline', 'generating_content', 'exporting', "
            "'validating', 'materializing'"
            ") AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL"
            ") OR ("
            "status NOT IN ("
            "'generating_outline', 'generating_content', 'exporting', "
            "'validating', 'materializing'"
            ") AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL"
            ")",
            name="status_lease_fence",
        ),
        UniqueConstraint(
            "tenant_id",
            "request_id",
            name="uq_generation_jobs_tenant_request",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_generation_jobs_tenant_idempotency",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_generation_jobs_id_tenant",
        ),
        Index(
            "ix_generation_jobs_status_available",
            "status",
            "next_attempt_at",
        ),
    )


class ArtifactPromotionState(TenantBase):
    """Recoverable bridge between an object-store commit and the DB commit."""

    __tablename__ = "artifact_promotion_states"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_id: Mapped[str] = mapped_column(String(128))
    version_number: Mapped[int] = mapped_column(Integer)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="prepared")
    object_committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_number"),
        CheckConstraint(
            "status IN ('prepared', 'object_committed', 'finalized')",
            name="status",
        ),
        ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            [
                "tenant.generation_jobs.id",
                "tenant.generation_jobs.tenant_id",
            ],
            name="fk_artifact_promotion_job_tenant_generation_jobs",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "classroom_id",
            "version_number",
            name="uq_artifact_promotion_tenant_classroom_version",
        ),
    )


class ClassroomArtifact(TenantBase):
    """Immutable integrity record for one promoted generation/export file."""

    __tablename__ = "classroom_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_job_id: Mapped[str] = mapped_column(String(64))
    classroom_version_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("tenant.classroom_versions.id", ondelete="RESTRICT"),
    )
    artifact_kind: Mapped[str] = mapped_column(String(16))
    relative_name: Mapped[str] = mapped_column(String(512))
    object_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(160))
    input_document_sha256: Mapped[str | None] = mapped_column(String(64))
    input_media_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "artifact_kind IN ('dsl_json', 'media', 'export')",
            name="artifact_kind",
        ),
        CheckConstraint("size_bytes >= 0", name="size_bytes"),
        ForeignKeyConstraint(
            ["source_job_id", "tenant_id"],
            [
                "tenant.generation_jobs.id",
                "tenant.generation_jobs.tenant_id",
            ],
            name="fk_classroom_artifacts_job_tenant_generation_jobs",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "source_job_id",
            "relative_name",
            name="uq_classroom_artifacts_tenant_job_name",
        ),
        UniqueConstraint(
            "tenant_id",
            "object_key",
            name="uq_classroom_artifacts_tenant_object_key",
        ),
    )


class QuotaLedger(TenantBase):
    """Append-only grant/reservation lifecycle entries for tenant quota."""

    __tablename__ = "quota_ledger"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str | None] = mapped_column(String(64))
    entry_type: Mapped[str] = mapped_column(String(16))
    units: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('grant', 'reserve', 'release', 'settle')",
            name="entry_type",
        ),
        CheckConstraint(
            "("
            "entry_type = 'grant' AND job_id IS NULL AND units > 0"
            ") OR ("
            "entry_type = 'reserve' AND job_id IS NOT NULL AND units < 0"
            ") OR ("
            "entry_type = 'release' AND job_id IS NOT NULL AND units > 0"
            ") OR ("
            "entry_type = 'settle' AND job_id IS NOT NULL AND units = 0"
            ")",
            name="entry_units",
        ),
        ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            [
                "tenant.generation_jobs.id",
                "tenant.generation_jobs.tenant_id",
            ],
            name="fk_quota_ledger_job_tenant_generation_jobs",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "job_id",
            "entry_type",
            name="uq_quota_ledger_job_entry",
        ),
        Index("ix_quota_ledger_tenant_created", "tenant_id", "created_at"),
    )


class OutboxMessage(PlatformBase):
    """One immutable queue-delivery event for a tenant job phase."""

    __tablename__ = "outbox_messages"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
    )
    job_id: Mapped[str] = mapped_column(String(64))
    job_kind: Mapped[str] = mapped_column(String(16))
    phase: Mapped[str] = mapped_column(String(16))
    data_plane_route_id: Mapped[str] = mapped_column(String(63))
    provider_profile_id: Mapped[str] = mapped_column(String(63))
    worker_pool_ref: Mapped[str] = mapped_column(String(128))
    queue_ref: Mapped[str] = mapped_column(String(128))
    slot_pool: Mapped[str] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "job_kind IN ('generation', 'export')",
            name="job_kind",
        ),
        CheckConstraint(
            "("
            "job_kind = 'generation' AND phase IN ('outline', 'content')"
            ") OR ("
            "job_kind = 'export' AND phase = 'export'"
            ")",
            name="kind_phase",
        ),
        CheckConstraint(
            "slot_pool IN ('generation', 'mp4_export')",
            name="slot_pool",
        ),
        CheckConstraint("priority >= 0", name="priority"),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "phase",
            name="uq_outbox_messages_tenant_job_phase",
        ),
        Index(
            "ix_outbox_messages_undelivered",
            "available_at",
            "created_at",
            postgresql_where=delivered_at.is_(None),
        ),
    )


class GenerationQueue(PlatformBase):
    """Scheduling-only projection of a tenant job."""

    __tablename__ = "generation_queue"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_kind: Mapped[str] = mapped_column(String(16))
    phase: Mapped[str] = mapped_column(String(16))
    data_plane_route_id: Mapped[str] = mapped_column(String(63))
    provider_profile_id: Mapped[str] = mapped_column(String(63))
    worker_pool_ref: Mapped[str] = mapped_column(String(128))
    queue_ref: Mapped[str] = mapped_column(String(128))
    slot_pool: Mapped[str] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "job_kind IN ('generation', 'export')",
            name="job_kind",
        ),
        CheckConstraint(
            "("
            "job_kind = 'generation' AND phase IN ('outline', 'content')"
            ") OR ("
            "job_kind = 'export' AND phase = 'export'"
            ")",
            name="kind_phase",
        ),
        CheckConstraint(
            "slot_pool IN ('generation', 'mp4_export')",
            name="slot_pool",
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed')",
            name="status",
        ),
        CheckConstraint("priority >= 0", name="priority"),
        CheckConstraint(
            "("
            "status = 'queued' AND claimed_at IS NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL"
            ") OR ("
            "status = 'claimed' AND claimed_at IS NOT NULL "
            "AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL"
            ")",
            name="lease_fence",
        ),
        Index(
            "ix_generation_queue_claim",
            "worker_pool_ref",
            "slot_pool",
            "status",
            "available_at",
            "priority",
            "enqueued_at",
        ),
    )


class GenerationSlot(PlatformBase):
    """A global or tenant-scoped concurrency slot with the worker fence."""

    __tablename__ = "generation_slots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    worker_pool_ref: Mapped[str] = mapped_column(String(128))
    slot_pool: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(16))
    owner_key: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    claimed_tenant_id: Mapped[str | None] = mapped_column(String(64))
    claimed_job_id: Mapped[str | None] = mapped_column(String(64))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "slot_pool IN ('generation', 'mp4_export')",
            name="slot_pool",
        ),
        CheckConstraint(
            "("
            "scope = 'global' AND tenant_id IS NULL AND owner_key = 'shared'"
            ") OR ("
            "scope = 'tenant' AND tenant_id IS NOT NULL "
            "AND owner_key = tenant_id"
            ")",
            name="scope_owner",
        ),
        CheckConstraint("ordinal >= 0", name="ordinal"),
        CheckConstraint(
            "("
            "claimed_tenant_id IS NULL AND claimed_job_id IS NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL"
            ") OR ("
            "claimed_tenant_id IS NOT NULL AND claimed_job_id IS NOT NULL "
            "AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL"
            ")",
            name="claim_fence",
        ),
        ForeignKeyConstraint(
            ["claimed_tenant_id", "claimed_job_id"],
            [
                "platform.generation_queue.tenant_id",
                "platform.generation_queue.job_id",
            ],
            name="fk_generation_slots_claimed_job_generation_queue",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "worker_pool_ref",
            "slot_pool",
            "scope",
            "owner_key",
            "ordinal",
            name="uq_generation_slots_worker_pool_scope_owner_ordinal",
        ),
        Index(
            "ix_generation_slots_available",
            "worker_pool_ref",
            "slot_pool",
            "scope",
            "owner_key",
            "claimed_job_id",
        ),
    )


class TenantSchedulerState(PlatformBase):
    """Fairness cursor independently maintained for each slot pool."""

    __tablename__ = "tenant_scheduler_state"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    worker_pool_ref: Mapped[str] = mapped_column(String(128), primary_key=True)
    slot_pool: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "slot_pool IN ('generation', 'mp4_export')",
            name="slot_pool",
        ),
        Index(
            "ix_tenant_scheduler_state_fairness",
            "worker_pool_ref",
            "slot_pool",
            "last_dispatched_at",
        ),
    )
