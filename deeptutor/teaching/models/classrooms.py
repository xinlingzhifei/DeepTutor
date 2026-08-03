"""Tenant classroom lifecycle, versioning, publication, and batch models."""

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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .tenant import TenantBase

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"generating_outline", "canceled"}),
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
            "status IN ('writing', 'uploaded', 'failed')",
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
    """Export receipt pinned to one immutable classroom version."""

    __tablename__ = "classroom_exports"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    classroom_version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenant.classroom_versions.id", ondelete="RESTRICT"),
    )
    generation_job_id: Mapped[str | None] = mapped_column(String(64))
    export_format: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="ready")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
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
            "object_key",
            name="uq_classroom_exports_tenant_object_key",
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
        Index("ix_approvals_classroom_created", "classroom_id", "created_at"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "(scope = 'class' AND class_id IS NOT NULL) OR "
            "(scope IN ('private', 'tenant') AND class_id IS NULL)",
            name="scope_class",
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "class_id",
            "classroom_version_id",
            name="uq_assignments_tenant_class_version",
        ),
        Index("ix_assignments_class_active", "class_id", "revoked_at"),
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
    "BatchItem",
    "BatchJob",
    "CLASSROOM_STATES",
    "ClassroomAsset",
    "ClassroomDraft",
    "ClassroomDraftMedia",
    "ClassroomExport",
    "ClassroomVersion",
    "InvalidClassroomTransition",
    "Publication",
    "SourceSnapshot",
    "SourceUpload",
    "TeachingBrief",
    "TenantSourceBinding",
    "transition",
]
