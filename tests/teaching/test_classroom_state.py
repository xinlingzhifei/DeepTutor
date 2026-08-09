from __future__ import annotations

import importlib

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from deeptutor.teaching.models.classrooms import (
    ALLOWED_TRANSITIONS,
    Approval,
    Assignment,
    AssignmentMigration,
    ClassLearningState,
    ClassroomAsset,
    ClassroomDraft,
    ClassroomDraftMedia,
    ClassroomExport,
    ClassroomExportPolicy,
    ClassroomPublicationMaterialization,
    ClassroomReviewPolicy,
    ClassroomReviewRequest,
    ClassroomVersion,
    InvalidClassroomTransition,
    Publication,
    SourceSnapshot,
    SourceUpload,
    TeachingBrief,
    TenantSourceBinding,
    transition,
)
from deeptutor.teaching.models.jobs import GenerationJob
from deeptutor.teaching.models.tenant import TeachingClass, TenantBase


def test_draft_cannot_publish_before_validation() -> None:
    with pytest.raises(InvalidClassroomTransition):
        transition("editing", "published")


@pytest.mark.parametrize(
    ("current_state", "target_state"),
    [
        ("draft", "generating_outline"),
        ("generating_outline", "awaiting_outline"),
        ("awaiting_outline", "generating_content"),
        ("generating_content", "editing"),
        ("editing", "submitted"),
        ("editing", "validated"),
        ("submitted", "approved"),
        ("submitted", "rejected"),
        ("rejected", "editing"),
        ("validated", "approved"),
        ("approved", "published"),
        ("failed", "draft"),
    ],
)
def test_allowed_classroom_transition_returns_target_state(
    current_state: str,
    target_state: str,
) -> None:
    assert transition(current_state, target_state) == target_state


@pytest.mark.parametrize("terminal_state", ["published", "canceled"])
def test_terminal_classroom_state_cannot_transition(terminal_state: str) -> None:
    with pytest.raises(InvalidClassroomTransition):
        transition(terminal_state, "draft")


def test_classroom_lifecycle_matches_the_approved_state_machine() -> None:
    assert ALLOWED_TRANSITIONS == {
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


def test_tenant_metadata_contains_the_classroom_domain_tables() -> None:
    assert {
        "tenant.source_snapshots",
        "tenant.tenant_source_bindings",
        "tenant.source_uploads",
        "tenant.teaching_briefs",
        "tenant.classroom_assets",
        "tenant.classroom_drafts",
        "tenant.classroom_draft_media",
        "tenant.classroom_review_policies",
        "tenant.classroom_review_requests",
        "tenant.classroom_publication_materializations",
        "tenant.assignment_migrations",
        "tenant.class_learning_states",
        "tenant.classroom_versions",
        "tenant.classroom_exports",
        "tenant.classroom_export_policies",
        "tenant.approvals",
        "tenant.publications",
        "tenant.assignments",
        "tenant.batch_jobs",
        "tenant.batch_items",
    }.issubset(TenantBase.metadata.tables)


def test_review_publication_schema_has_durable_idempotency_and_guard_fences() -> None:
    review = ClassroomReviewRequest.__table__
    materialization = ClassroomPublicationMaterialization.__table__
    publication = Publication.__table__
    assignment = Assignment.__table__
    migration = AssignmentMigration.__table__
    learning = ClassLearningState.__table__

    def unique_columns(table) -> set[tuple[str, ...]]:
        return {
            tuple(constraint.columns.keys())
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }

    assert ("tenant_id", "idempotency_key") in unique_columns(review)
    assert ("tenant_id", "review_request_id") in unique_columns(materialization)
    assert ("tenant_id", "idempotency_key") in unique_columns(materialization)
    assert ("tenant_id", "version_id") in unique_columns(materialization)
    assert (
        "tenant_id",
        "classroom_id",
        "version_number",
    ) in unique_columns(materialization)
    assert {
        "source_version_id",
        "draft_revision",
        "document_sha256",
        "validation_report_sha256",
        "media_manifest_sha256",
        "manifest_sha256",
        "source_media_receipts",
        "confirmed_artifacts",
        "status",
    }.issubset(materialization.c.keys())
    assert ("tenant_id", "idempotency_key") in unique_columns(publication)
    assert ("tenant_id", "review_request_id") in unique_columns(publication)
    assert ("tenant_id", "idempotency_key") in unique_columns(assignment)
    assert ("tenant_id", "idempotency_key") in unique_columns(migration)
    assert {
        "old_assignment_id",
        "old_version_id",
        "new_version_id",
        "new_assignment_id",
        "class_id",
        "actor_id",
        "reason",
        "outcome",
    }.issubset(migration.c.keys())
    assert {"state", "active_session_count", "updated_by"}.issubset(
        learning.c.keys()
    )
    assert ClassroomReviewPolicy.__table__.c.teacher_self_publish.default is not None


def test_review_events_link_to_request_and_terminal_decision_is_unique() -> None:
    approval = Approval.__table__
    assert "review_request_id" in approval.c
    terminal_indexes = {
        index.name: index
        for index in approval.indexes
        if index.unique
    }
    assert "uq_approvals_terminal_review_decision" in terminal_indexes


def test_review_publication_migration_follows_classroom_authoring() -> None:
    migration = importlib.import_module(
        "deeptutor.teaching.migrations.versions.20260803_0011_review_publication"
    )
    assert migration.revision == "20260803_0011"
    assert migration.down_revision == "20260803_0010"


def test_classroom_export_schema_pins_one_source_and_durable_receipts() -> None:
    table = ClassroomExport.__table__
    columns = table.c
    assert {
        "classroom_id",
        "classroom_version_id",
        "classroom_draft_id",
        "draft_revision",
        "generation_job_id",
        "export_format",
        "input_document_sha256",
        "input_media_manifest_sha256",
        "idempotency_key",
        "request_sha256",
        "input_manifest_object_key",
        "input_manifest_sha256",
        "relative_name",
        "object_key",
        "sha256",
        "size_bytes",
        "mime_type",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    }.issubset(columns.keys())
    assert columns.classroom_version_id.nullable is True
    assert columns.classroom_draft_id.nullable is True
    assert columns.draft_revision.nullable is True
    assert columns.generation_job_id.nullable is True
    assert columns.object_key.nullable is True
    assert columns.sha256.nullable is True

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "classroom_id IS NULL OR" in checks[
        "ck_classroom_exports_target"
    ]
    assert "classroom_version_id IS NOT NULL" in checks[
        "ck_classroom_exports_target"
    ]
    assert "classroom_draft_id IS NOT NULL" in checks[
        "ck_classroom_exports_target"
    ]
    assert "draft_revision > 0" in checks[
        "ck_classroom_exports_draft_revision"
    ]
    assert "classroom_zip" in checks["ck_classroom_exports_format"]
    assert "mp4" in checks["ck_classroom_exports_format"]
    assert "input_manifest_object_key IS NULL" in checks[
        "ck_classroom_exports_input_receipt"
    ]
    assert "status = 'ready'" in checks[
        "ck_classroom_exports_output_receipt"
    ]

    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "idempotency_key") in unique_columns
    assert ("tenant_id", "generation_job_id") in unique_columns

    foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("classroom_id", "tenant_id"),
        ("tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"),
    ) in foreign_keys
    assert (
        ("classroom_version_id", "classroom_id", "tenant_id"),
        (
            "tenant.classroom_versions.id",
            "tenant.classroom_versions.classroom_id",
            "tenant.classroom_versions.tenant_id",
        ),
    ) in foreign_keys
    assert (
        ("classroom_draft_id", "classroom_id", "tenant_id"),
        (
            "tenant.classroom_drafts.id",
            "tenant.classroom_drafts.classroom_id",
            "tenant.classroom_drafts.tenant_id",
        ),
    ) in foreign_keys
    assert (
        ("generation_job_id", "tenant_id"),
        ("tenant.generation_jobs.id", "tenant.generation_jobs.tenant_id"),
    ) in foreign_keys


def test_classroom_export_policy_defaults_mp4_to_denied() -> None:
    policy = ClassroomExportPolicy.__table__
    assert policy.c.tenant_id.primary_key is True
    assert policy.c.allow_mp4.nullable is False
    assert policy.c.allow_mp4.server_default is not None
    assert str(policy.c.allow_mp4.server_default.arg) == "false"
    assert {"updated_by", "updated_at"}.issubset(policy.c.keys())


def test_classroom_export_migration_follows_review_publication() -> None:
    migration = importlib.import_module(
        "deeptutor.teaching.migrations.versions.20260804_0012_classroom_exports"
    )
    assert migration.revision == "20260804_0012"
    assert migration.down_revision == "20260803_0011"


def test_draft_media_is_integrity_checked_and_bound_to_tenant_asset() -> None:
    table = ClassroomDraftMedia.__table__
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert ("id", "classroom_id", "tenant_id") in unique_columns
    assert ("tenant_id", "object_key") in unique_columns
    assert (
        ("classroom_id", "tenant_id"),
        ("tenant.classroom_assets.id", "tenant.classroom_assets.tenant_id"),
    ) in foreign_keys
    assert checks["ck_classroom_draft_media_sha256"] == (
        "sha256 ~ '^[0-9a-f]{64}$'"
    )
    assert checks["ck_classroom_draft_media_size_bytes"] == (
        "size_bytes >= 0 AND size_bytes <= 104857600"
    )
    assert checks["ck_classroom_draft_media_status"] == (
        "status IN ('writing', 'uploaded', 'cleanup_pending', 'failed')"
    )
    assert checks["ck_classroom_draft_media_ownership_token"] == (
        "ownership_token ~ '^[0-9a-f]{32}$'"
    )


def test_classroom_draft_persists_outline_confirmation_and_validation_report() -> None:
    columns = ClassroomDraft.__table__.c

    assert columns.generation_job_id.nullable is True
    assert columns.outline_document.nullable is True
    assert columns.outline_sha256.nullable is True
    assert columns.confirmed_outline_sha256.nullable is True
    assert columns.validation_report.nullable is True
    assert columns.validation_report_sha256.nullable is True
    assert columns.validation_revision.nullable is True
    assert columns.validation_document_sha256.nullable is True
    assert columns.creation_idempotency_key.nullable is True
    assert columns.creation_request_sha256.nullable is True
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in ClassroomDraft.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "validation_report IS NULL" in checks["ck_classroom_drafts_validation_binding"]
    assert "validation_revision IS NOT NULL" in checks[
        "ck_classroom_drafts_validation_binding"
    ]
    assert "creation_idempotency_key IS NULL" in checks[
        "ck_classroom_drafts_creation_binding"
    ]
    assert "creation_request_sha256 IS NOT NULL" in checks[
        "ck_classroom_drafts_creation_binding"
    ]
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in ClassroomDraft.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "creation_idempotency_key") in unique_columns
    foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in ClassroomDraft.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("generation_job_id", "tenant_id"),
        ("tenant.generation_jobs.id", "tenant.generation_jobs.tenant_id"),
    ) in foreign_keys


def test_task4_migration_follows_knowledge_entitlements_revision() -> None:
    migration = importlib.import_module(
        "deeptutor.teaching.migrations.versions.20260803_0010_classroom_authoring"
    )

    assert migration.revision == "20260803_0010"
    assert migration.down_revision == "20260803_0009"


def test_classroom_version_is_unique_per_asset_version_number() -> None:
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in ClassroomVersion.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("tenant_id", "classroom_id", "version_number") in unique_columns
    assert ("id", "classroom_id", "tenant_id") in unique_columns


def test_source_snapshot_identity_includes_resource_owner() -> None:
    table = SourceSnapshot.__table__
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert table.c.resource_owner_id.nullable is False
    assert (
        "tenant_id",
        "source_type",
        "source_id",
        "resource_owner_id",
        "source_revision",
        "permission_sha256",
    ) in unique_columns


def test_source_storage_identity_and_foreign_keys_are_tenant_composite() -> None:
    snapshot = SourceSnapshot.__table__
    upload = SourceUpload.__table__
    binding = TenantSourceBinding.__table__
    teaching_class = TeachingClass.__table__

    def unique_columns(table) -> set[tuple[str, ...]]:
        return {
            tuple(constraint.columns.keys())
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }

    def foreign_key_columns(table) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
        return {
            (
                tuple(constraint.columns.keys()),
                tuple(element.target_fullname for element in constraint.elements),
            )
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }

    assert ("id", "tenant_id") in unique_columns(snapshot)
    assert ("id", "tenant_id") in unique_columns(upload)
    assert ("tenant_id", "sha256") in unique_columns(upload)
    assert ("id", "course_id") in unique_columns(teaching_class)
    assert (
        ("source_upload_id", "tenant_id"),
        ("tenant.source_uploads.id", "tenant.source_uploads.tenant_id"),
    ) in foreign_key_columns(snapshot)
    assert (
        ("source_snapshot_id", "tenant_id"),
        ("tenant.source_snapshots.id", "tenant.source_snapshots.tenant_id"),
    ) in foreign_key_columns(binding)
    assert (
        ("class_id", "course_id"),
        ("tenant.classes.id", "tenant.classes.course_id"),
    ) in foreign_key_columns(binding)

    assert "source_snapshot_id" not in upload.c
    assert "filename" not in upload.c
    assert snapshot.c.source_upload_id.nullable is True
    assert snapshot.c.display_name.nullable is True
    assert {
        "ownership_token",
        "object_revision",
        "object_version_id",
        "last_error_code",
        "updated_at",
    }.issubset(upload.c.keys())
    status_constraints = {
        str(constraint.sqltext)
        for constraint in upload.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_source_uploads_status"
    }
    assert status_constraints == {
        "status IN ('writing', 'uploaded', 'cleanup_pending', 'failed')"
    }


def test_source_and_brief_scope_columns_are_constrained_as_tuples() -> None:
    snapshot = SourceSnapshot.__table__
    binding = TenantSourceBinding.__table__
    brief = TeachingBrief.__table__

    def foreign_key_columns(table) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
        return {
            (
                tuple(constraint.columns.keys()),
                tuple(element.target_fullname for element in constraint.elements),
            )
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }

    snapshot_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in snapshot.constraints
        if isinstance(constraint, CheckConstraint)
    }
    binding_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in binding.constraints
        if isinstance(constraint, CheckConstraint)
    }
    upload_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in SourceUpload.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert (
        ("source_snapshot_id", "tenant_id"),
        ("tenant.source_snapshots.id", "tenant.source_snapshots.tenant_id"),
    ) in foreign_key_columns(brief)
    assert (
        ("class_id", "course_id"),
        ("tenant.classes.id", "tenant.classes.course_id"),
    ) in foreign_key_columns(brief)
    assert binding_checks["ck_tenant_source_bindings_class_requires_course"] == (
        "class_id IS NULL OR course_id IS NOT NULL"
    )
    brief_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in brief.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert brief_checks["ck_teaching_briefs_class_requires_course"] == (
        "class_id IS NULL OR course_id IS NOT NULL"
    )
    assert snapshot_checks["ck_source_snapshots_pdf_upload"] == (
        "(source_type = 'pdf' AND source_upload_id IS NOT NULL AND display_name IS NOT NULL) "
        "OR (source_type <> 'pdf' AND source_upload_id IS NULL)"
    )
    assert snapshot_checks["ck_source_snapshots_knowledge_generation"] == (
        "source_type <> 'knowledge_base' OR source_id ~ "
        "'^(admin|user):kb:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        "[0-9a-f]{4}-[0-9a-f]{12}$'"
    )
    assert upload_checks["ck_source_uploads_sha256"] == (
        "sha256 ~ '^[0-9a-f]{64}$'"
    )
    assert upload_checks["ck_source_uploads_ownership_token"] == (
        "ownership_token ~ '^[0-9a-f]{32}$'"
    )
    assert upload_checks["ck_source_uploads_receipt_state"] == (
        "(status = 'writing' AND object_revision IS NULL AND last_error_code IS NULL) OR "
        "(status = 'uploaded' AND object_revision IS NOT NULL AND last_error_code IS NULL) OR "
        "(status IN ('cleanup_pending', 'failed') AND last_error_code IS NOT NULL)"
    )


def test_classroom_version_has_exactly_one_immutable_provenance() -> None:
    table = ClassroomVersion.__table__
    provenance_constraints = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_classroom_versions_provenance"
    }

    assert table.c.generation_job_id.nullable is True
    assert table.c.source_version_id.nullable is True
    assert provenance_constraints == {
        "(generation_job_id IS NOT NULL AND source_version_id IS NULL) OR "
        "(generation_job_id IS NULL AND source_version_id IS NOT NULL)"
    }


@pytest.mark.parametrize(
    ("table", "constraint_name", "local_columns", "target_columns"),
    [
        (
            ClassroomVersion.__table__,
            "fk_classroom_versions_source_classroom_tenant",
            ("source_version_id", "classroom_id", "tenant_id"),
            (
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ),
        ),
        (
            ClassroomAsset.__table__,
            "fk_classroom_assets_current_version_classroom_tenant",
            ("current_published_version_id", "id", "tenant_id"),
            (
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ),
        ),
        (
            Publication.__table__,
            "fk_publications_version_classroom_tenant",
            ("classroom_version_id", "classroom_id", "tenant_id"),
            (
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ),
        ),
        (
            ClassroomDraft.__table__,
            "fk_classroom_drafts_base_version_classroom_tenant",
            ("base_version_id", "classroom_id", "tenant_id"),
            (
                "tenant.classroom_versions.id",
                "tenant.classroom_versions.classroom_id",
                "tenant.classroom_versions.tenant_id",
            ),
        ),
        (
            Approval.__table__,
            "fk_approvals_draft_classroom_tenant",
            ("classroom_draft_id", "classroom_id", "tenant_id"),
            (
                "tenant.classroom_drafts.id",
                "tenant.classroom_drafts.classroom_id",
                "tenant.classroom_drafts.tenant_id",
            ),
        ),
    ],
)
def test_redundant_classroom_identity_uses_composite_foreign_keys(
    table,
    constraint_name: str,
    local_columns: tuple[str, ...],
    target_columns: tuple[str, ...],
) -> None:
    constraint = next(
        item
        for item in table.constraints
        if isinstance(item, ForeignKeyConstraint) and item.name == constraint_name
    )

    assert tuple(constraint.columns.keys()) == local_columns
    assert tuple(element.target_fullname for element in constraint.elements) == target_columns


def test_assignment_directly_references_an_immutable_version() -> None:
    assert {
        foreign_key.target_fullname for foreign_key in Assignment.__table__.foreign_keys
    } >= {"tenant.classroom_versions.id"}


def test_generation_status_is_separate_from_classroom_lifecycle() -> None:
    assert "status" in GenerationJob.__table__.columns
    assert "lifecycle_state" not in GenerationJob.__table__.columns
    assert "lifecycle_state" in ClassroomAsset.__table__.columns
