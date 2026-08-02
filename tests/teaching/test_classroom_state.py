from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from deeptutor.teaching.models.classrooms import (
    ALLOWED_TRANSITIONS,
    Approval,
    Assignment,
    ClassroomAsset,
    ClassroomDraft,
    ClassroomVersion,
    InvalidClassroomTransition,
    Publication,
    SourceSnapshot,
    transition,
)
from deeptutor.teaching.models.jobs import GenerationJob
from deeptutor.teaching.models.tenant import TenantBase


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
        "tenant.classroom_versions",
        "tenant.classroom_exports",
        "tenant.approvals",
        "tenant.publications",
        "tenant.assignments",
        "tenant.batch_jobs",
        "tenant.batch_items",
    }.issubset(TenantBase.metadata.tables)


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
    ) in unique_columns


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
