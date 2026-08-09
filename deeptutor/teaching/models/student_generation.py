"""SQLAlchemy records for durable student generation policy decisions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .tenant import TenantBase


class CourseGenerationPolicyRecord(TenantBase):
    __tablename__ = "course_generation_policies"

    course_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant.courses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    allow_student_micro: Mapped[bool] = mapped_column(Boolean, server_default="true")
    allow_student_full: Mapped[bool] = mapped_column(Boolean, server_default="false")
    allowed_content_modes: Mapped[str] = mapped_column(String(64))
    allow_web_search: Mapped[bool] = mapped_column(Boolean, server_default="false")
    require_approval_for_restricted_topics: Mapped[bool] = mapped_column(
        Boolean,
        server_default="true",
    )
    minor_safety_mode: Mapped[bool] = mapped_column(Boolean, server_default="true")
    micro_scene_limit: Mapped[int] = mapped_column(Integer, server_default="5")
    full_scene_limit: Mapped[int] = mapped_column(Integer, server_default="24")
    daily_student_units: Mapped[int] = mapped_column(Integer)
    monthly_student_units: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "course_id",
            "tenant_id",
            name="uq_course_generation_policies_course_tenant",
        ),
        CheckConstraint(
            "allowed_content_modes IN ('source_grounded', 'open_creation', "
            "'source_grounded,open_creation')",
            name="allowed_content_modes",
        ),
        CheckConstraint(
            "micro_scene_limit >= 1 AND micro_scene_limit <= 5",
            name="micro_scene_limit",
        ),
        CheckConstraint(
            "full_scene_limit >= 1 AND full_scene_limit <= 24",
            name="full_scene_limit",
        ),
        CheckConstraint("daily_student_units >= 0", name="daily_student_units"),
        CheckConstraint("monthly_student_units >= 0", name="monthly_student_units"),
        Index("ix_course_generation_policies_tenant", "tenant_id"),
    )


class StudentGenerationRequestRecord(TenantBase):
    __tablename__ = "student_generation_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    learner_id: Mapped[str] = mapped_column(String(128))
    course_id: Mapped[str] = mapped_column(String(64))
    class_id: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    content_mode: Mapped[str] = mapped_column(String(32))
    web_search_requested: Mapped[bool] = mapped_column(Boolean)
    scene_min: Mapped[int] = mapped_column(Integer)
    scene_max: Mapped[int] = mapped_column(Integer)
    duration_minutes_min: Mapped[int] = mapped_column(Integer)
    duration_minutes_max: Mapped[int] = mapped_column(Integer)
    estimated_units: Mapped[int] = mapped_column(Integer)
    quota_state: Mapped[str] = mapped_column(String(16))
    requires_outline_confirmation: Mapped[bool] = mapped_column(Boolean)
    decision_outcome: Mapped[str] = mapped_column(String(32))
    decision_reason: Mapped[str] = mapped_column(String(64))
    evaluated_checks: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["class_id", "course_id"],
            ["tenant.classes.id", "tenant.classes.course_id"],
            name="fk_student_generation_requests_class_course_classes",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["course_id", "tenant_id"],
            [
                "tenant.course_generation_policies.course_id",
                "tenant.course_generation_policies.tenant_id",
            ],
            name="fk_student_generation_requests_policy_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_student_generation_requests_id_tenant",
        ),
        CheckConstraint("mode IN ('micro', 'full')", name="mode"),
        CheckConstraint(
            "content_mode IN ('source_grounded', 'open_creation')",
            name="content_mode",
        ),
        CheckConstraint(
            "scene_min >= 1 AND scene_max >= scene_min AND scene_max <= 24 "
            "AND (mode <> 'micro' OR scene_max <= 5)",
            name="scene_range",
        ),
        CheckConstraint(
            "duration_minutes_min >= 1 AND duration_minutes_max >= duration_minutes_min",
            name="duration_range",
        ),
        CheckConstraint("estimated_units > 0", name="estimated_units"),
        CheckConstraint(
            "quota_state IN ('none', 'reserved', 'settled', 'released')",
            name="quota_state",
        ),
        CheckConstraint(
            "(decision_outcome = 'accepted' AND quota_state IN "
            "('reserved', 'settled', 'released')) OR "
            "(decision_outcome <> 'accepted' AND quota_state = 'none')",
            name="quota_lifecycle",
        ),
        CheckConstraint(
            "(mode = 'micro' AND requires_outline_confirmation = false) OR "
            "(mode = 'full' AND requires_outline_confirmation = true)",
            name="outline_confirmation",
        ),
        CheckConstraint(
            "decision_outcome IN ('denied', 'approval_required', 'accepted')",
            name="decision_outcome",
        ),
        CheckConstraint(
            "length(decision_reason) > 0 AND length(evaluated_checks) > 0",
            name="decision_evidence",
        ),
        Index(
            "ix_student_generation_requests_quota_usage",
            "tenant_id",
            "learner_id",
            "course_id",
            "quota_state",
            "created_at",
        ),
    )


class StudentGenerationApprovalRecord(TenantBase):
    __tablename__ = "student_generation_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            [
                "tenant.student_generation_requests.id",
                "tenant.student_generation_requests.tenant_id",
            ],
            name="fk_student_generation_approvals_request_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_student_generation_approvals_id_tenant",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="status",
        ),
        CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(status <> 'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decision_shape",
        ),
        Index(
            "uq_student_generation_approvals_pending_request",
            "tenant_id",
            "request_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_student_generation_approvals_status_requested",
            "tenant_id",
            "status",
            "requested_at",
        ),
    )


class StudentClassroomAssetRecord(TenantBase):
    """Strong link from one policy request to its existing classroom asset."""

    __tablename__ = "student_classroom_assets"

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["asset_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_student_classroom_assets_asset_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            [
                "tenant.student_generation_requests.id",
                "tenant.student_generation_requests.tenant_id",
            ],
            name="fk_student_classroom_assets_request_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "asset_id",
            "tenant_id",
            name="uq_student_classroom_assets_asset_tenant",
        ),
        UniqueConstraint(
            "request_id",
            "tenant_id",
            name="uq_student_classroom_assets_request_tenant",
        ),
        Index(
            "ix_student_classroom_assets_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )


class StudentClassroomCopyRecord(TenantBase):
    """Immutable audit relation for a teacher draft copied from student work."""

    __tablename__ = "student_classroom_copies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_asset_id: Mapped[str] = mapped_column(String(128))
    teacher_asset_id: Mapped[str] = mapped_column(String(128))
    copied_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_asset_id", "tenant_id"],
            [
                "tenant.student_classroom_assets.asset_id",
                "tenant.student_classroom_assets.tenant_id",
            ],
            name="fk_student_classroom_copies_source_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["teacher_asset_id", "tenant_id"],
            ["tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"],
            name="fk_student_classroom_copies_teacher_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_student_classroom_copies_id_tenant",
        ),
        UniqueConstraint(
            "teacher_asset_id",
            "tenant_id",
            name="uq_student_classroom_copies_teacher_tenant",
        ),
        Index(
            "ix_student_classroom_copies_source_created",
            "tenant_id",
            "source_asset_id",
            "created_at",
        ),
    )
