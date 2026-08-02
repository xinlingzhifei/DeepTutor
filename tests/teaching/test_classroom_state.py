from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint

from deeptutor.teaching.models.classrooms import (
    ALLOWED_TRANSITIONS,
    Assignment,
    ClassroomAsset,
    ClassroomVersion,
    InvalidClassroomTransition,
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
    assert {
        foreign_key.target_fullname
        for foreign_key in ClassroomVersion.__table__.foreign_keys
        if foreign_key.parent.name == "classroom_id"
    } == {"tenant.classroom_assets.id"}


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
    assert {
        foreign_key.target_fullname
        for foreign_key in table.c.source_version_id.foreign_keys
    } == {"tenant.classroom_versions.id"}
    assert provenance_constraints == {
        "(generation_job_id IS NOT NULL AND source_version_id IS NULL) OR "
        "(generation_job_id IS NULL AND source_version_id IS NOT NULL)"
    }


def test_assignment_directly_references_an_immutable_version() -> None:
    assert {
        foreign_key.target_fullname for foreign_key in Assignment.__table__.foreign_keys
    } >= {"tenant.classroom_versions.id"}


def test_generation_status_is_separate_from_classroom_lifecycle() -> None:
    assert "status" in GenerationJob.__table__.columns
    assert "lifecycle_state" not in GenerationJob.__table__.columns
    assert "lifecycle_state" in ClassroomAsset.__table__.columns
