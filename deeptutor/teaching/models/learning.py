"""Tenant-scoped append-only classroom learning records and projections."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .tenant import TenantBase


class LearningSession(TenantBase):
    """Server-owned learner session pinned to one immutable classroom version."""

    __tablename__ = "learning_sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.classroom_versions.id", ondelete="RESTRICT"),
    )
    assignment_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("tenant.assignments.id", ondelete="RESTRICT"),
    )
    student_asset_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    next_seq: Mapped[int] = mapped_column(Integer, server_default="1")
    last_cursor: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "(assignment_id IS NOT NULL AND student_asset_id IS NULL) OR "
            "(assignment_id IS NULL AND student_asset_id IS NOT NULL)",
            name="authority",
        ),
        CheckConstraint(
            "status IN ('active', 'completed', 'abandoned')",
            name="status",
        ),
        CheckConstraint("next_seq > 0", name="next_seq"),
        CheckConstraint(
            "(status = 'active' AND completed_at IS NULL) OR "
            "(status IN ('completed', 'abandoned') AND completed_at IS NOT NULL)",
            name="completion",
        ),
        ForeignKeyConstraint(
            ["student_asset_id", "tenant_id"],
            [
                "tenant.student_classroom_assets.asset_id",
                "tenant.student_classroom_assets.tenant_id",
            ],
            name="fk_learning_sessions_student_asset_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_learning_sessions_id_tenant"),
        Index("ix_learning_sessions_user_status", "user_id", "status"),
        Index("ix_learning_sessions_classroom_version", "classroom_version_id"),
    )


class ClassroomTicketConsumption(TenantBase):
    """Durable single-use ticket fact committed with its protected action."""

    __tablename__ = "classroom_ticket_consumptions"

    jti: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(String(128))
    allowed_action: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["tenant.learning_sessions.id", "tenant.learning_sessions.tenant_id"],
            name="fk_classroom_ticket_consumptions_session_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_classroom_ticket_consumptions_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("expires_at > issued_at", name="validity"),
        CheckConstraint(
            "allowed_action = 'learning_event.append'",
            name="allowed_action",
        ),
        Index("ix_classroom_ticket_consumptions_expires_at", "expires_at"),
    )


class LearningEvent(TenantBase):
    """Immutable, server-bound classroom event with a per-session sequence."""

    __tablename__ = "learning_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.classroom_versions.id", ondelete="RESTRICT"),
    )
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scene_id: Mapped[str | None] = mapped_column(String(128))
    knowledge_point_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("seq > 0", name="seq"),
        CheckConstraint("length(event_id) > 0", name="event_id"),
        CheckConstraint("length(event_type) > 0", name="event_type"),
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["tenant.learning_sessions.id", "tenant.learning_sessions.tenant_id"],
            name="fk_learning_events_session_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("event_id", name="uq_learning_events_event_id"),
        UniqueConstraint(
            "session_id",
            "seq",
            name="uq_learning_events_session_seq",
        ),
        Index("ix_learning_events_event_type", "event_type"),
        Index("ix_learning_events_occurred_at", "occurred_at"),
        Index("ix_learning_events_session_id", "session_id"),
        Index(
            "ix_learning_events_classroom_version_id",
            "classroom_version_id",
        ),
        Index("ix_learning_events_knowledge_point_id", "knowledge_point_id"),
    )


class LearningProjectionQueueItem(TenantBase):
    """Leaseable projection work item inserted atomically with its event."""

    __tablename__ = "learning_projection_queue"

    event_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.learning_events.event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="8")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'quarantined')",
            name="status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="attempts",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL "
            "AND heartbeat_at IS NULL)",
            name="lease_fence",
        ),
        Index(
            "ix_learning_projection_queue_available",
            "status",
            "available_at",
            "created_at",
        ),
        Index("ix_learning_projection_queue_session", "session_id"),
    )


class QuizAttempt(TenantBase):
    """Idempotent trusted quiz projection derived from one raw event."""

    __tablename__ = "quiz_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.learning_events.event_id", ondelete="RESTRICT"),
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(String(128))
    assessment_id: Mapped[str] = mapped_column(String(128))
    question_id: Mapped[str] = mapped_column(String(128))
    knowledge_point_id: Mapped[str] = mapped_column(String(128))
    answer_payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    is_correct: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[float] = mapped_column(Float)
    grading_source: Mapped[str] = mapped_column(String(32))
    graded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 1", name="score"),
        UniqueConstraint("event_id", name="uq_quiz_attempts_event_id"),
        Index("ix_quiz_attempts_user_knowledge", "user_id", "knowledge_point_id"),
        Index("ix_quiz_attempts_session", "session_id"),
    )


class PblGradingResult(TenantBase):
    """Immutable trusted teacher grading fact for one PBL completion event."""

    __tablename__ = "pbl_grading_results"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.learning_events.event_id", ondelete="RESTRICT"),
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(String(128))
    document_version_id: Mapped[str] = mapped_column(String(128))
    scene_id: Mapped[str] = mapped_column(String(128))
    milestone_id: Mapped[str] = mapped_column(String(128))
    knowledge_point_id: Mapped[str] = mapped_column(String(128))
    rubric_sha256: Mapped[str] = mapped_column(String(64))
    correctness: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[float | None] = mapped_column(Float)
    grading_source: Mapped[str] = mapped_column(String(32))
    source_reference: Mapped[str] = mapped_column(String(256))
    graded_by: Mapped[str] = mapped_column(String(128))
    graded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["classroom_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_pbl_grading_results_classroom_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_version_id"],
            ["tenant.classroom_versions.id"],
            name="fk_pbl_grading_results_document_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["session_id", "tenant_id"],
            ["tenant.learning_sessions.id", "tenant.learning_sessions.tenant_id"],
            name="fk_pbl_grading_results_session_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)", name="score"),
        CheckConstraint(
            "grading_source = 'teacher_review'",
            name="grading_source",
        ),
        CheckConstraint("length(source_reference) > 0", name="source_reference"),
        CheckConstraint("length(graded_by) > 0", name="graded_by"),
        CheckConstraint("length(scene_id) > 0", name="scene_id"),
        CheckConstraint("length(milestone_id) > 0", name="milestone_id"),
        CheckConstraint("length(knowledge_point_id) > 0", name="knowledge_point_id"),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key"),
        CheckConstraint(
            "rubric_sha256 ~ '^[0-9a-f]{64}$'",
            name="rubric_sha256",
        ),
        CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="request_sha256",
        ),
        UniqueConstraint("event_id", name="uq_pbl_grading_results_event_id"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_pbl_grading_results_tenant_idempotency",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "event_id",
            "request_sha256",
            name="uq_pbl_grading_results_alias_binding",
        ),
        Index(
            "ix_pbl_grading_results_user_knowledge",
            "user_id",
            "knowledge_point_id",
            "graded_at",
        ),
        Index("ix_pbl_grading_results_session", "session_id"),
    )


class PblGradingIdempotencyKey(TenantBase):
    """Permanent tenant-scoped key alias for one immutable PBL result."""

    __tablename__ = "pbl_grading_idempotency_keys"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["result_id", "tenant_id", "event_id", "request_sha256"],
            [
                "tenant.pbl_grading_results.id",
                "tenant.pbl_grading_results.tenant_id",
                "tenant.pbl_grading_results.event_id",
                "tenant.pbl_grading_results.request_sha256",
            ],
            name="fk_pbl_grading_idempotency_keys_result",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key"),
        CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="request_sha256",
        ),
        Index("ix_pbl_grading_idempotency_keys_result", "result_id"),
    )


class MasteryEvidence(TenantBase):
    """Trusted graded evidence eligible to change learner mastery."""

    __tablename__ = "mastery_evidence"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.learning_events.event_id", ondelete="RESTRICT"),
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(String(128))
    knowledge_point_id: Mapped[str] = mapped_column(String(128))
    evidence_type: Mapped[str] = mapped_column(String(32))
    correctness: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[float | None] = mapped_column(Float)
    grading_source: Mapped[str] = mapped_column(String(32))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)", name="score"),
        UniqueConstraint("event_id", name="uq_mastery_evidence_event_id"),
        Index(
            "ix_mastery_evidence_user_knowledge",
            "user_id",
            "knowledge_point_id",
            "recorded_at",
        ),
    )


class MasteryLevel(TenantBase):
    """Current mastery projection for one learner and knowledge point."""

    __tablename__ = "mastery_levels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(128))
    knowledge_point_id: Mapped[str] = mapped_column(String(128))
    level: Mapped[float] = mapped_column(Float, server_default="0")
    evidence_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_evidence_event_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("tenant.learning_events.event_id", ondelete="RESTRICT"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint("level >= 0 AND level <= 1", name="level"),
        CheckConstraint("evidence_count >= 0", name="evidence_count"),
        UniqueConstraint(
            "user_id",
            "knowledge_point_id",
            name="uq_mastery_levels_user_knowledge",
        ),
    )


class LearningProgress(TenantBase):
    """Idempotent per-session progress projection."""

    __tablename__ = "learning_progress"

    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.learning_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    last_event_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("tenant.learning_events.event_id", ondelete="RESTRICT"),
    )
    last_event_seq: Mapped[int] = mapped_column(Integer, server_default="0")
    completed_scene_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_scene_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'completed')", name="status"),
        CheckConstraint(
            "last_event_seq >= 0 AND completed_scene_count >= 0",
            name="counts",
        ),
        CheckConstraint(
            "(status = 'active' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name="completion",
        ),
        Index("ix_learning_progress_user_status", "user_id", "status"),
        Index("ix_learning_progress_classroom_version", "classroom_version_id"),
    )


class LearningEventQuarantine(TenantBase):
    """Rejected event fact retained for safe diagnosis without projection."""

    __tablename__ = "learning_event_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.learning_sessions.id", ondelete="RESTRICT"),
    )
    user_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    knowledge_point_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    reason_code: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    quarantined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("length(reason_code) > 0", name="reason_code"),
        Index("ix_learning_event_quarantine_event", "event_id"),
        Index(
            "ix_learning_event_quarantine_reason",
            "reason_code",
            "quarantined_at",
        ),
    )


__all__ = [
    "ClassroomTicketConsumption",
    "LearningEvent",
    "LearningEventQuarantine",
    "LearningProgress",
    "LearningProjectionQueueItem",
    "LearningSession",
    "MasteryEvidence",
    "MasteryLevel",
    "PblGradingResult",
    "QuizAttempt",
]
