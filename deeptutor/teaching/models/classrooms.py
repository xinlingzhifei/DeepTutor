"""Tenant classroom lifecycle, versioning, publication, and batch models."""

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .tenant import TenantBase

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"generating_outline", "generating_content", "canceled"}),
    "generating_outline": frozenset({"awaiting_outline", "failed", "canceled"}),
    "awaiting_outline": frozenset({"generating_content", "canceled"}),
    "generating_content": frozenset({"editing", "failed", "canceled"}),
    "editing": frozenset({"submitted", "validated", "canceled"}),
    "submitted": frozenset({"approved", "rejected"}),
    "rejected": frozenset({"editing"}),
    "validated": frozenset({"approved"}),
    "approved": frozenset({"published"}),
    "published": frozenset(),
    "failed": frozenset({"draft"}),
    "canceled": frozenset(),
}
CLASSROOM_STATES = frozenset(ALLOWED_TRANSITIONS)


class InvalidClassroomTransition(ValueError):
    """The requested transition is outside the classroom lifecycle."""


def transition(current_state: str, target_state: str) -> str:
    """Return a valid target state or reject an illegal lifecycle change."""

    if current_state not in ALLOWED_TRANSITIONS:
        raise InvalidClassroomTransition("unknown classroom state")
    if target_state not in ALLOWED_TRANSITIONS[current_state]:
        raise InvalidClassroomTransition("classroom transition is not allowed")
    return target_state


class SourceSnapshot(TenantBase):
    """Immutable source revision and authorization evidence used for generation."""

    __tablename__ = "source_snapshots"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(128))
    resource_owner_id: Mapped[str] = mapped_column(String(128))
    source_upload_id: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(512))
    source_revision: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64))
    permission_sha256: Mapped[str] = mapped_column(String(64))
    citation_manifest: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "(source_type = 'pdf' AND source_upload_id IS NOT NULL "
            "AND display_name IS NOT NULL) OR "
            "(source_type <> 'pdf' AND source_upload_id IS NULL)",
            name="pdf_upload",
        ),
        CheckConstraint(
            "source_type <> 'knowledge_base' OR source_id ~ "
            "'^(admin|user):kb:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            "[0-9a-f]{4}-[0-9a-f]{12}$'",
            name="knowledge_generation",
        ),
        ForeignKeyConstraint(
            ["source_upload_id", "tenant_id"],
            ["tenant.source_uploads.id", "tenant.source_uploads.tenant_id"],
            name="fk_source_snapshots_upload_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_source_snapshots_id_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "source_type",
            "source_id",
            "resource_owner_id",
            "source_revision",
            "permission_sha256",
            name="uq_source_snapshots_tenant_source_revision",
        ),
    )


class TenantSourceBinding(TenantBase):
    """Tenant course/class authorization binding for one source snapshot."""

    __tablename__ = "tenant_source_bindings"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_snapshot_id: Mapped[str] = mapped_column(
        String(128),
    )
    course_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tenant.courses.id", ondelete="CASCADE"),
    )
    class_id: Mapped[str | None] = mapped_column(String(64))
    bound_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "course_id IS NOT NULL OR class_id IS NOT NULL",
            name="resource_scope",
        ),
        CheckConstraint(
            "class_id IS NULL OR course_id IS NOT NULL",
            name="class_requires_course",
        ),
        ForeignKeyConstraint(
            ["source_snapshot_id", "tenant_id"],
            ["tenant.source_snapshots.id", "tenant.source_snapshots.tenant_id"],
            name="fk_tenant_source_bindings_snapshot_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["class_id", "course_id"],
            ["tenant.classes.id", "tenant.classes.course_id"],
            name="fk_tenant_source_bindings_class_course",
            ondelete="CASCADE",
        ),
        Index("ix_tenant_source_bindings_snapshot", "source_snapshot_id"),
    )


class SourceUpload(TenantBase):
    """Durable object-store receipt for an uploaded teaching source."""

    __tablename__ = "source_uploads"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), server_default="writing")
    ownership_token: Mapped[str] = mapped_column(String(32))
    object_revision: Mapped[str | None] = mapped_column(String(256))
    object_version_id: Mapped[str | None] = mapped_column(String(256))
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
        CheckConstraint("size_bytes >= 0", name="size_bytes"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint(
            "ownership_token ~ '^[0-9a-f]{32}$'",
            name="ownership_token",
        ),
        CheckConstraint(
            "status IN ('writing', 'uploaded', 'cleanup_pending', 'failed')",
            name="status",
        ),
        CheckConstraint(
            "(status = 'writing' AND object_revision IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'uploaded' AND object_revision IS NOT NULL "
            "AND last_error_code IS NULL) OR "
            "(status IN ('cleanup_pending', 'failed') "
            "AND last_error_code IS NOT NULL)",
            name="receipt_state",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_source_uploads_id_tenant"),
        UniqueConstraint("tenant_id", "sha256", name="uq_source_uploads_tenant_sha256"),
        UniqueConstraint(
            "tenant_id",
            "object_key",
            name="uq_source_uploads_tenant_object_key",
        ),
    )


class TeachingBrief(TenantBase):
    """Stable, hashed input contract for staged classroom generation."""

    __tablename__ = "teaching_briefs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_snapshot_id: Mapped[str | None] = mapped_column(
        String(128),
    )
    course_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tenant.courses.id", ondelete="RESTRICT"),
    )
    class_id: Mapped[str | None] = mapped_column(String(64))
    brief_version: Mapped[int] = mapped_column(Integer)
    document: Mapped[str] = mapped_column(Text)
    document_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("brief_version > 0", name="brief_version"),
        CheckConstraint(
            "class_id IS NULL OR course_id IS NOT NULL",
            name="class_requires_course",
        ),
        ForeignKeyConstraint(
            ["source_snapshot_id", "tenant_id"],
            ["tenant.source_snapshots.id", "tenant.source_snapshots.tenant_id"],
            name="fk_teaching_briefs_snapshot_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["class_id", "course_id"],
            ["tenant.classes.id", "tenant.classes.course_id"],
            name="fk_teaching_briefs_class_course",
            ondelete="RESTRICT",
        ),
    )


class ClassroomAsset(TenantBase):
    """Stable logical classroom identity with publication lifecycle state."""

    __tablename__ = "classroom_assets"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(255))
    lifecycle_state: Mapped[str] = mapped_column(String(32), server_default="draft")
    current_published_version_id: Mapped[str | None] = mapped_column(String(128))
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
            "lifecycle_state IN ("
            "'draft', 'generating_outline', 'awaiting_outline', "
            "'generating_content', 'editing', 'submitted', 'rejected', "
            "'validated', 'approved', 'published', 'failed', 'canceled'"
            ")",
            name="lifecycle_state",
        ),
        ForeignKeyConstraint(
            ["current_published_version_id", "id", "tenant_id"],
            [
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ],
            name="fk_classroom_assets_current_version_classroom_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_classroom_assets_id_tenant",
        ),
        Index(
            "ix_classroom_assets_tenant_owner_state",
            "tenant_id",
            "owner_id",
            "lifecycle_state",
        ),
    )


class ClassroomVersion(TenantBase):
    """Immutable classroom document and media reference."""

    __tablename__ = "classroom_versions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_id: Mapped[str] = mapped_column(String(128))
    version_number: Mapped[int] = mapped_column(Integer)
    generation_job_id: Mapped[str | None] = mapped_column(String(64))
    source_version_id: Mapped[str | None] = mapped_column(String(128))
    document_sha256: Mapped[str] = mapped_column(String(64))
    media_manifest_sha256: Mapped[str] = mapped_column(String(64))
    document_object_key: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_number"),
        CheckConstraint(
            "(generation_job_id IS NOT NULL AND source_version_id IS NULL) OR "
            "(generation_job_id IS NULL AND source_version_id IS NOT NULL)",
            name="provenance",
        ),
        ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            [
                "tenant.classroom_assets.id",
                "tenant.classroom_assets.tenant_id",
            ],
            name="fk_classroom_versions_asset_tenant_classroom_assets",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["generation_job_id", "tenant_id"],
            [
                "tenant.generation_jobs.id",
                "tenant.generation_jobs.tenant_id",
            ],
            name="fk_classroom_versions_job_tenant_generation_jobs",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ],
            name="fk_classroom_versions_source_classroom_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "classroom_id",
            "tenant_id",
            name="uq_classroom_versions_id_classroom_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "classroom_id",
            "version_number",
            name="uq_classroom_versions_tenant_classroom_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "generation_job_id",
            name="uq_classroom_versions_tenant_generation_job",
        ),
    )


class ClassroomDraft(TenantBase):
    """Mutable working document kept separate from immutable versions."""

    __tablename__ = "classroom_drafts"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_id: Mapped[str] = mapped_column(String(128))
    generation_job_id: Mapped[str | None] = mapped_column(String(64))
    teaching_brief_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("tenant.teaching_briefs.id", ondelete="RESTRICT"),
    )
    base_version_id: Mapped[str | None] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    document: Mapped[str] = mapped_column(Text)
    document_sha256: Mapped[str] = mapped_column(String(64))
    outline_document: Mapped[str | None] = mapped_column(Text)
    outline_sha256: Mapped[str | None] = mapped_column(String(64))
    confirmed_outline_sha256: Mapped[str | None] = mapped_column(String(64))
    validation_report: Mapped[str | None] = mapped_column(Text)
    validation_report_sha256: Mapped[str | None] = mapped_column(String(64))
    validation_revision: Mapped[int | None] = mapped_column(Integer)
    validation_document_sha256: Mapped[str | None] = mapped_column(String(64))
    creation_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    creation_request_sha256: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    updated_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint(
            "(validation_report IS NULL AND validation_report_sha256 IS NULL "
            "AND validation_revision IS NULL AND validation_document_sha256 IS NULL) OR "
            "(validation_report IS NOT NULL AND validation_report_sha256 IS NOT NULL "
            "AND validation_revision IS NOT NULL AND validation_revision > 0 "
            "AND validation_document_sha256 IS NOT NULL)",
            name="validation_binding",
        ),
        CheckConstraint(
            "(creation_idempotency_key IS NULL AND creation_request_sha256 IS NULL) OR "
            "(creation_idempotency_key IS NOT NULL "
            "AND creation_request_sha256 IS NOT NULL)",
            name="creation_binding",
        ),
        ForeignKeyConstraint(
            ["generation_job_id", "tenant_id"],
            [
                "tenant.generation_jobs.id",
                "tenant.generation_jobs.tenant_id",
            ],
            name="fk_classroom_drafts_job_tenant_generation_jobs",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["base_version_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ],
            name="fk_classroom_drafts_base_version_classroom_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            [
                "tenant.classroom_assets.id",
                "tenant.classroom_assets.tenant_id",
            ],
            name="fk_classroom_drafts_asset_tenant_classroom_assets",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id",
            "classroom_id",
            "tenant_id",
            name="uq_classroom_drafts_id_classroom_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "creation_idempotency_key",
            name="uq_classroom_drafts_tenant_creation_idempotency",
        ),
        Index("ix_classroom_drafts_classroom_updated", "classroom_id", "updated_at"),
    )


class ClassroomDraftMedia(TenantBase):
    """Integrity receipt for one asset-scoped temporary editor upload."""

    __tablename__ = "classroom_draft_media"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_id: Mapped[str] = mapped_column(String(128))
    uploaded_by: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), server_default="writing")
    ownership_token: Mapped[str] = mapped_column(String(32))
    object_revision: Mapped[str | None] = mapped_column(String(256))
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
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="sha256",
        ),
        CheckConstraint(
            "size_bytes >= 0 AND size_bytes <= 104857600",
            name="size_bytes",
        ),
        CheckConstraint(
            "status IN ('writing', 'uploaded', 'cleanup_pending', 'failed')",
            name="status",
        ),
        CheckConstraint(
            "ownership_token ~ '^[0-9a-f]{32}$'",
            name="ownership_token",
        ),
        CheckConstraint(
            "(status = 'writing' AND object_revision IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'uploaded' AND object_revision IS NOT NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'cleanup_pending' AND last_error_code IS NOT NULL) OR "
            "(status = 'failed' AND last_error_code IS NOT NULL)",
            name="receipt_state",
        ),
        ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            [
                "tenant.classroom_assets.id",
                "tenant.classroom_assets.tenant_id",
            ],
            name="fk_classroom_draft_media_asset_tenant_classroom_assets",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id",
            "classroom_id",
            "tenant_id",
            name="uq_classroom_draft_media_id_classroom_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "object_key",
            name="uq_classroom_draft_media_tenant_object_key",
        ),
        Index(
            "ix_classroom_draft_media_asset_created",
            "classroom_id",
            "created_at",
        ),
    )


class ClassroomExport(TenantBase):
    """Durable export request pinned to exactly one draft or version."""

    __tablename__ = "classroom_exports"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    # Nullable at the storage layer only so pre-0012 rows remain representable.
    # New records are required to set this through ``record_shape`` below.
    classroom_id: Mapped[str] = mapped_column(String(128), nullable=True)
    classroom_version_id: Mapped[str | None] = mapped_column(String(128))
    classroom_draft_id: Mapped[str | None] = mapped_column(String(128))
    draft_revision: Mapped[int | None] = mapped_column(Integer)
    generation_job_id: Mapped[str | None] = mapped_column(String(64))
    export_format: Mapped[str] = mapped_column(String(32))
    input_document_sha256: Mapped[str] = mapped_column(String(64), nullable=True)
    input_media_manifest_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=True)
    input_manifest_object_key: Mapped[str | None] = mapped_column(String(512))
    input_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    relative_name: Mapped[str | None] = mapped_column(String(512))
    object_key: Mapped[str | None] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32),
        server_default="preparing_input",
    )
    created_by: Mapped[str] = mapped_column(String(128))
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
            "(classroom_id IS NULL AND classroom_version_id IS NOT NULL "
            "AND classroom_draft_id IS NULL AND draft_revision IS NULL "
            "AND input_document_sha256 IS NULL "
            "AND input_media_manifest_sha256 IS NULL "
            "AND idempotency_key IS NULL AND request_sha256 IS NULL "
            "AND input_manifest_object_key IS NULL "
            "AND input_manifest_sha256 IS NULL "
            "AND relative_name IS NULL AND size_bytes IS NULL "
            "AND mime_type IS NULL) OR "
            "(classroom_id IS NOT NULL "
            "AND input_document_sha256 IS NOT NULL "
            "AND input_media_manifest_sha256 IS NOT NULL "
            "AND idempotency_key IS NOT NULL AND request_sha256 IS NOT NULL)",
            name="record_shape",
        ),
        CheckConstraint(
            "classroom_id IS NULL OR ("
            "(classroom_version_id IS NOT NULL AND classroom_draft_id IS NULL) OR "
            "(classroom_version_id IS NULL AND classroom_draft_id IS NOT NULL))",
            name="target",
        ),
        CheckConstraint(
            "classroom_id IS NULL OR ("
            "(classroom_draft_id IS NULL AND draft_revision IS NULL) OR "
            "(classroom_draft_id IS NOT NULL AND draft_revision IS NOT NULL "
            "AND draft_revision > 0))",
            name="draft_revision",
        ),
        CheckConstraint(
            "classroom_id IS NULL OR export_format IN "
            "('classroom_zip', 'pptx', 'offline_html', 'mp4')",
            name="format",
        ),
        CheckConstraint(
            "classroom_id IS NULL OR ("
            "input_document_sha256 ~ '^[0-9a-f]{64}$' AND "
            "input_media_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "request_sha256 ~ '^[0-9a-f]{64}$' AND "
            "(input_manifest_sha256 IS NULL OR "
            "input_manifest_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'))",
            name="hashes",
        ),
        CheckConstraint(
            "(input_manifest_object_key IS NULL "
            "AND input_manifest_sha256 IS NULL) OR "
            "(input_manifest_object_key IS NOT NULL "
            "AND input_manifest_sha256 IS NOT NULL)",
            name="input_receipt",
        ),
        CheckConstraint(
            "classroom_id IS NULL OR ("
            "(relative_name IS NULL AND object_key IS NULL AND sha256 IS NULL "
            "AND size_bytes IS NULL AND mime_type IS NULL AND status <> 'ready') OR "
            "(relative_name IS NOT NULL AND object_key IS NOT NULL "
            "AND sha256 IS NOT NULL AND size_bytes IS NOT NULL "
            "AND size_bytes >= 0 AND mime_type IS NOT NULL "
            "AND status = 'ready'))",
            name="output_receipt",
        ),
        ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            [
                "tenant.classroom_assets.id",
                "tenant.classroom_assets.tenant_id",
            ],
            name="fk_classroom_exports_asset_tenant_classroom_assets",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["classroom_version_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ],
            name="fk_classroom_exports_version_classroom_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["classroom_draft_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_drafts.id",
                "tenant.classroom_drafts.classroom_id",
                "tenant.classroom_drafts.tenant_id",
            ],
            name="fk_classroom_exports_draft_classroom_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["generation_job_id", "tenant_id"],
            [
                "tenant.generation_jobs.id",
                "tenant.generation_jobs.tenant_id",
            ],
            name="fk_classroom_exports_job_tenant_generation_jobs",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_classroom_exports_tenant_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "generation_job_id",
            name="uq_classroom_exports_tenant_generation_job",
        ),
        UniqueConstraint(
            "tenant_id",
            "object_key",
            name="uq_classroom_exports_tenant_object_key",
        ),
    )


class ClassroomExportPolicy(TenantBase):
    """Tenant policy for costly or privileged classroom export formats."""

    __tablename__ = "classroom_export_policies"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    allow_mp4: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    updated_by: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ClassroomReviewPolicy(TenantBase):
    """Explicit per-tenant review policy; self-publish defaults fail closed."""

    __tablename__ = "classroom_review_policies"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    teacher_self_publish: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    org_content_requires_review: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )
    platform_template_requires_review: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )
    prohibit_self_review: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )
    updated_by: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ClassroomReviewRequest(TenantBase):
    """Mutable decision fence bound to one exact validated draft revision."""

    __tablename__ = "classroom_review_requests"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_id: Mapped[str] = mapped_column(String(128))
    classroom_draft_id: Mapped[str] = mapped_column(String(128))
    draft_revision: Mapped[int] = mapped_column(Integer)
    document_sha256: Mapped[str] = mapped_column(String(64))
    validation_report_sha256: Mapped[str] = mapped_column(String(64))
    submitted_by: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(16))
    class_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    warnings: Mapped[str] = mapped_column(Text, server_default="[]")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decision_comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("draft_revision > 0", name="draft_revision"),
        CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$' AND "
            "validation_report_sha256 ~ '^[0-9a-f]{64}$' AND "
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="hashes",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="status",
        ),
        CheckConstraint(
            "(scope = 'class' AND class_id IS NOT NULL) OR "
            "(scope IN ('tenant', 'platform') AND class_id IS NULL)",
            name="scope_class",
        ),
        CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decided_at IS NULL "
            "AND decision_comment IS NULL) OR "
            "(status IN ('approved', 'rejected') AND decided_by IS NOT NULL "
            "AND decided_at IS NOT NULL AND decision_comment IS NOT NULL)",
            name="decision_binding",
        ),
        ForeignKeyConstraint(
            ["classroom_draft_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_drafts.id",
                "tenant.classroom_drafts.classroom_id",
                "tenant.classroom_drafts.tenant_id",
            ],
            name="fk_classroom_review_requests_draft_classroom_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            [
                "tenant.classroom_assets.id",
                "tenant.classroom_assets.tenant_id",
            ],
            name="fk_classroom_review_requests_asset_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["class_id"],
            ["tenant.classes.id"],
            name="fk_classroom_review_requests_class",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_classroom_review_requests_id_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_classroom_review_requests_tenant_idempotency",
        ),
        Index(
            "ix_classroom_review_requests_pending",
            "status",
            "created_at",
        ),
    )


class ClassroomPublicationMaterialization(TenantBase):
    """Durable object-store reservation for one reviewed draft publication."""

    __tablename__ = "classroom_publication_materializations"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    review_request_id: Mapped[str] = mapped_column(String(128))
    classroom_id: Mapped[str] = mapped_column(String(128))
    classroom_draft_id: Mapped[str] = mapped_column(String(128))
    source_version_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[str] = mapped_column(String(128))
    version_number: Mapped[int] = mapped_column(Integer)
    draft_revision: Mapped[int] = mapped_column(Integer)
    document_sha256: Mapped[str] = mapped_column(String(64))
    validation_report_sha256: Mapped[str] = mapped_column(String(64))
    media_manifest_sha256: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    manifest_document: Mapped[str] = mapped_column(Text)
    source_media_receipts: Mapped[str] = mapped_column(Text)
    confirmed_artifacts: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), server_default="prepared")
    scope: Mapped[str] = mapped_column(String(16))
    class_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(128))
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
        CheckConstraint("version_number > 0", name="version_number"),
        CheckConstraint("draft_revision > 0", name="draft_revision"),
        CheckConstraint(
            "status IN ('prepared', 'object_committed', 'finalized')",
            name="status",
        ),
        CheckConstraint(
            "(status = 'prepared' AND confirmed_artifacts IS NULL) OR "
            "(status IN ('object_committed', 'finalized') "
            "AND confirmed_artifacts IS NOT NULL)",
            name="confirmed_binding",
        ),
        CheckConstraint(
            "(scope = 'class' AND class_id IS NOT NULL) OR "
            "(scope IN ('tenant', 'platform') AND class_id IS NULL)",
            name="scope_class",
        ),
        ForeignKeyConstraint(
            ["review_request_id", "tenant_id"],
            [
                "tenant.classroom_review_requests.id",
                "tenant.classroom_review_requests.tenant_id",
            ],
            name="fk_publish_materializations_review_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["classroom_draft_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_drafts.id",
                "tenant.classroom_drafts.classroom_id",
                "tenant.classroom_drafts.tenant_id",
            ],
            name="fk_publish_materializations_draft_classroom_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ],
            name="fk_publish_materializations_source_classroom_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "review_request_id",
            name="uq_publish_materializations_tenant_review",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_publish_materializations_tenant_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "version_id",
            name="uq_publish_materializations_tenant_version_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "classroom_id",
            "version_number",
            name="uq_publish_materializations_tenant_classroom_version",
        ),
    )


class Approval(TenantBase):
    """Append-only submission or review decision for one classroom draft."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_id: Mapped[str] = mapped_column(String(128))
    classroom_draft_id: Mapped[str] = mapped_column(String(128))
    submitted_by: Mapped[str] = mapped_column(String(128))
    reviewer_id: Mapped[str | None] = mapped_column(String(128))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    review_request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN ('submitted', 'approved', 'rejected')",
            name="decision",
        ),
        ForeignKeyConstraint(
            ["classroom_draft_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_drafts.id",
                "tenant.classroom_drafts.classroom_id",
                "tenant.classroom_drafts.tenant_id",
            ],
            name="fk_approvals_draft_classroom_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            [
                "tenant.classroom_assets.id",
                "tenant.classroom_assets.tenant_id",
            ],
            name="fk_approvals_asset_tenant_classroom_assets",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["review_request_id", "tenant_id"],
            [
                "tenant.classroom_review_requests.id",
                "tenant.classroom_review_requests.tenant_id",
            ],
            name="fk_approvals_review_request_tenant",
            ondelete="RESTRICT",
        ),
        Index("ix_approvals_classroom_created", "classroom_id", "created_at"),
        Index(
            "uq_approvals_terminal_review_decision",
            "tenant_id",
            "review_request_id",
            unique=True,
            postgresql_where=text("decision IN ('approved', 'rejected')"),
        ),
    )


class Publication(TenantBase):
    """Audit-relevant publication scope pinned to one immutable version."""

    __tablename__ = "publications"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_id: Mapped[str] = mapped_column(String(128))
    classroom_version_id: Mapped[str] = mapped_column(String(128))
    actor_id: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(16))
    class_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tenant.classes.id", ondelete="RESTRICT"),
    )
    review_request_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "(scope = 'class' AND class_id IS NOT NULL) OR "
            "(scope IN ('private', 'tenant', 'platform') AND class_id IS NULL)",
            name="scope_class",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL AND request_sha256 IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_sha256 IS NOT NULL)",
            name="idempotency_binding",
        ),
        ForeignKeyConstraint(
            ["classroom_id", "tenant_id"],
            [
                "tenant.classroom_assets.id",
                "tenant.classroom_assets.tenant_id",
            ],
            name="fk_publications_asset_tenant_classroom_assets",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["review_request_id", "tenant_id"],
            [
                "tenant.classroom_review_requests.id",
                "tenant.classroom_review_requests.tenant_id",
            ],
            name="fk_publications_review_request_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["classroom_version_id", "classroom_id", "tenant_id"],
            [
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ],
            name="fk_publications_version_classroom_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "classroom_version_id",
            "scope",
            "class_id",
            name="uq_publications_tenant_version_scope_class",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_publications_tenant_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "review_request_id",
            name="uq_publications_tenant_review_request",
        ),
    )


class Assignment(TenantBase):
    """Class assignment pinned directly to an immutable classroom version."""

    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.classroom_versions.id", ondelete="RESTRICT"),
    )
    class_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant.classes.id", ondelete="CASCADE"),
    )
    assigned_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(idempotency_key IS NULL AND request_sha256 IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_sha256 IS NOT NULL)",
            name="idempotency_binding",
        ),
        UniqueConstraint(
            "tenant_id",
            "class_id",
            "classroom_version_id",
            name="uq_assignments_tenant_class_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_assignments_tenant_idempotency",
        ),
        Index("ix_assignments_class_active", "class_id", "revoked_at"),
    )


class ClassLearningState(TenantBase):
    """Plan-06 integration seam used to fail closed during migration."""

    __tablename__ = "class_learning_states"

    class_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant.classes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), server_default="unknown")
    active_session_count: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_by: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "(state IN ('unknown', 'idle') AND active_session_count = 0) OR "
            "(state = 'active' AND active_session_count > 0)",
            name="state_count",
        ),
        UniqueConstraint(
            "class_id",
            "tenant_id",
            name="uq_class_learning_states_class_tenant",
        ),
    )


class AssignmentMigration(TenantBase):
    """Append-only audit of one explicit pinned assignment migration."""

    __tablename__ = "assignment_migrations"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    old_assignment_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.assignments.id", ondelete="RESTRICT"),
    )
    old_version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.classroom_versions.id", ondelete="RESTRICT"),
    )
    new_version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.classroom_versions.id", ondelete="RESTRICT"),
    )
    new_assignment_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("tenant.assignments.id", ondelete="RESTRICT"),
    )
    class_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant.classes.id", ondelete="RESTRICT"),
    )
    actor_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('succeeded', 'refused_active_learning', "
            "'refused_guard_unavailable')",
            name="outcome",
        ),
        CheckConstraint(
            "(outcome = 'succeeded' AND new_assignment_id IS NOT NULL) OR "
            "(outcome <> 'succeeded' AND new_assignment_id IS NULL)",
            name="outcome_assignment",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_assignment_migrations_tenant_idempotency",
        ),
        Index(
            "ix_assignment_migrations_class_created",
            "class_id",
            "created_at",
        ),
    )


class BatchJob(TenantBase):
    """Tenant batch aggregate kept separate from individual generation jobs."""

    __tablename__ = "batch_jobs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), server_default="created")
    item_count: Mapped[int] = mapped_column(Integer, server_default="0")
    succeeded_count: Mapped[int] = mapped_column(Integer, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, server_default="0")
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
            "item_count >= 0 AND succeeded_count >= 0 AND failed_count >= 0 "
            "AND succeeded_count + failed_count <= item_count",
            name="counts",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_batch_jobs_id_tenant"),
    )


class BatchItem(TenantBase):
    """One independently retryable item within a tenant batch."""

    __tablename__ = "batch_items"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    batch_job_id: Mapped[str] = mapped_column(String(128))
    generation_job_id: Mapped[str | None] = mapped_column(String(64))
    classroom_draft_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("tenant.classroom_drafts.id", ondelete="SET NULL"),
    )
    status: Mapped[str] = mapped_column(String(32), server_default="created")
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
            ["batch_job_id", "tenant_id"],
            ["tenant.batch_jobs.id", "tenant.batch_jobs.tenant_id"],
            name="fk_batch_items_batch_tenant_batch_jobs",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["generation_job_id", "tenant_id"],
            [
                "tenant.generation_jobs.id",
                "tenant.generation_jobs.tenant_id",
            ],
            name="fk_batch_items_job_tenant_generation_jobs",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "batch_job_id",
            "id",
            name="uq_batch_items_tenant_batch_item",
        ),
        Index("ix_batch_items_batch_status", "batch_job_id", "status"),
    )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "Approval",
    "Assignment",
    "AssignmentMigration",
    "BatchItem",
    "BatchJob",
    "CLASSROOM_STATES",
    "ClassLearningState",
    "ClassroomAsset",
    "ClassroomDraft",
    "ClassroomDraftMedia",
    "ClassroomExport",
    "ClassroomExportPolicy",
    "ClassroomPublicationMaterialization",
    "ClassroomReviewPolicy",
    "ClassroomReviewRequest",
    "ClassroomVersion",
    "InvalidClassroomTransition",
    "Publication",
    "SourceSnapshot",
    "SourceUpload",
    "TeachingBrief",
    "TenantSourceBinding",
    "transition",
]
