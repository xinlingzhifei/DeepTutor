from __future__ import annotations

from dataclasses import replace
import importlib

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint

from deeptutor.teaching.models.student_generation import (
    CourseGenerationPolicyRecord,
    StudentGenerationApprovalRecord,
    StudentGenerationRequestRecord,
)
from deeptutor.teaching.policies.student_generation import (
    CourseGenerationPolicy,
    StudentGenerationEvaluationContext,
    StudentGenerationQuota,
    StudentGenerationRequest,
    estimate_student_request,
    evaluate_student_request,
)


def policy(**changes: object) -> CourseGenerationPolicy:
    values: dict[str, object] = {
        "allowed_content_modes": frozenset({"source_grounded", "open_creation"}),
        "daily_student_units": 20,
        "monthly_student_units": 200,
        "allow_student_micro": True,
        "allow_student_full": True,
        "allow_web_search": True,
        "require_approval_for_restricted_topics": True,
        "minor_safety_mode": True,
        "micro_scene_limit": 5,
        "full_scene_limit": 24,
    }
    values.update(changes)
    return CourseGenerationPolicy(**values)  # type: ignore[arg-type]


def request(**changes: object) -> StudentGenerationRequest:
    values: dict[str, object] = {
        "course_id": "course-1",
        "class_id": "class-1",
        "mode": "micro",
        "content_mode": "source_grounded",
        "web_search_requested": False,
    }
    values.update(changes)
    return StudentGenerationRequest(**values)  # type: ignore[arg-type]


def context(**changes: object) -> StudentGenerationEvaluationContext:
    values: dict[str, object] = {
        "enrolled": True,
        "has_generation_permission": True,
        "source_permitted": True,
        "generally_safe": True,
        "minor_safe": True,
        "restricted_topic": False,
        "approval_granted": False,
    }
    values.update(changes)
    return StudentGenerationEvaluationContext(**values)  # type: ignore[arg-type]


def quota(**changes: int) -> StudentGenerationQuota:
    values = {"daily_used_units": 0, "monthly_used_units": 0}
    values.update(changes)
    return StudentGenerationQuota(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("micro_scene_limit", 0),
        ("micro_scene_limit", 6),
        ("full_scene_limit", 0),
        ("full_scene_limit", 25),
        ("daily_student_units", -1),
        ("monthly_student_units", -1),
    ],
)
def test_policy_rejects_out_of_bounds_limits(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=field):
        policy(**{field: value})


@pytest.mark.parametrize(
    "allowed_content_modes",
    [frozenset(), frozenset({"unknown"}), frozenset({"source_grounded", "unknown"})],
)
def test_policy_rejects_empty_or_unknown_content_modes(
    allowed_content_modes: frozenset[str],
) -> None:
    with pytest.raises(ValueError, match="allowed_content_modes"):
        policy(allowed_content_modes=allowed_content_modes)


def test_policy_requires_an_immutable_content_mode_set() -> None:
    with pytest.raises(ValueError, match="allowed_content_modes"):
        policy(allowed_content_modes={"source_grounded"})


@pytest.mark.parametrize(
    "field",
    [
        "allow_student_micro",
        "allow_student_full",
        "allow_web_search",
        "require_approval_for_restricted_topics",
        "minor_safety_mode",
    ],
)
def test_policy_requires_strict_boolean_flags(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        policy(**{field: 1})


def test_zero_quota_is_a_fail_closed_policy_that_requires_approval() -> None:
    selected_policy = policy(daily_student_units=0, monthly_student_units=0)

    decision = evaluate_student_request(
        policy=selected_policy,
        request=request(),
        context=context(),
        quota=quota(),
    )

    assert decision.outcome == "approval_required"
    assert decision.reason == "quota_exceeded"
    assert decision.estimated_units > 0


def test_user_request_cannot_supply_trusted_policy_facts() -> None:
    with pytest.raises(TypeError):
        StudentGenerationRequest(
            course_id="course-1",
            class_id="class-1",
            mode="micro",
            content_mode="source_grounded",
            web_search_requested=False,
            enrolled=True,  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    "field",
    [
        "enrolled",
        "has_generation_permission",
        "source_permitted",
        "generally_safe",
        "minor_safe",
        "restricted_topic",
        "approval_granted",
    ],
)
def test_trusted_evaluation_context_requires_strict_booleans(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        context(**{field: "false"})


def test_full_classroom_requires_explicit_course_permission() -> None:
    decision = evaluate_student_request(
        policy=policy(allow_student_full=False),
        request=request(mode="full"),
        context=context(),
        quota=quota(),
    )

    assert decision.outcome == "denied"
    assert decision.reason == "full_classroom_disabled"


@pytest.mark.parametrize(
    ("selected_request", "expected_reason"),
    [
        (request(content_mode="open_creation"), "content_mode_disabled"),
        (request(web_search_requested=True), "web_search_disabled"),
    ],
)
def test_course_policy_enforces_content_and_web_modes(
    selected_request: StudentGenerationRequest,
    expected_reason: str,
) -> None:
    decision = evaluate_student_request(
        policy=policy(
            allowed_content_modes=frozenset({"source_grounded"}),
            allow_web_search=False,
        ),
        request=selected_request,
        context=context(),
        quota=quota(),
    )

    assert decision.outcome == "denied"
    assert decision.reason == expected_reason


@pytest.mark.parametrize(
    (
        "selected_request",
        "selected_policy",
        "selected_context",
        "selected_quota",
        "reason",
        "checks",
    ),
    [
        (
            request(mode="full", content_mode="open_creation", web_search_requested=True),
            policy(
                allow_student_full=False,
                allowed_content_modes=frozenset({"source_grounded"}),
                allow_web_search=False,
                daily_student_units=0,
                monthly_student_units=0,
            ),
            context(
                enrolled=False,
                has_generation_permission=False,
                source_permitted=False,
                generally_safe=False,
                minor_safe=False,
                restricted_topic=True,
            ),
            quota(daily_used_units=10, monthly_used_units=10),
            "not_enrolled",
            ("enrollment",),
        ),
        (
            request(mode="full", content_mode="open_creation", web_search_requested=True),
            policy(
                allow_student_full=False,
                allowed_content_modes=frozenset({"source_grounded"}),
                allow_web_search=False,
                daily_student_units=0,
                monthly_student_units=0,
            ),
            context(
                has_generation_permission=False,
                source_permitted=False,
                generally_safe=False,
                minor_safe=False,
                restricted_topic=True,
            ),
            quota(daily_used_units=10, monthly_used_units=10),
            "generation_permission_denied",
            ("enrollment", "permission"),
        ),
        (
            request(mode="full", content_mode="open_creation", web_search_requested=True),
            policy(
                allow_student_full=False,
                allowed_content_modes=frozenset({"source_grounded"}),
                allow_web_search=False,
                daily_student_units=0,
                monthly_student_units=0,
            ),
            context(
                source_permitted=False,
                generally_safe=False,
                minor_safe=False,
                restricted_topic=True,
            ),
            quota(daily_used_units=10, monthly_used_units=10),
            "full_classroom_disabled",
            ("enrollment", "permission", "course_mode"),
        ),
        (
            request(content_mode="open_creation", web_search_requested=True),
            policy(
                allowed_content_modes=frozenset({"source_grounded"}),
                allow_web_search=False,
                daily_student_units=0,
                monthly_student_units=0,
            ),
            context(
                source_permitted=False,
                generally_safe=False,
                minor_safe=False,
                restricted_topic=True,
            ),
            quota(daily_used_units=10, monthly_used_units=10),
            "content_mode_disabled",
            ("enrollment", "permission", "course_mode", "tenant_policy"),
        ),
        (
            request(),
            policy(daily_student_units=0, monthly_student_units=0),
            context(
                source_permitted=False,
                generally_safe=False,
                minor_safe=False,
                restricted_topic=True,
            ),
            quota(daily_used_units=10, monthly_used_units=10),
            "source_permission_denied",
            (
                "enrollment",
                "permission",
                "course_mode",
                "tenant_policy",
                "source_permission",
            ),
        ),
        (
            request(),
            policy(daily_student_units=0, monthly_student_units=0),
            context(generally_safe=False, minor_safe=False, restricted_topic=True),
            quota(daily_used_units=10, monthly_used_units=10),
            "safety_restriction",
            (
                "enrollment",
                "permission",
                "course_mode",
                "tenant_policy",
                "source_permission",
                "safety",
            ),
        ),
        (
            request(),
            policy(daily_student_units=1, monthly_student_units=1),
            context(restricted_topic=True),
            quota(daily_used_units=1, monthly_used_units=1),
            "quota_exceeded",
            (
                "enrollment",
                "permission",
                "course_mode",
                "tenant_policy",
                "source_permission",
                "safety",
                "quota",
                "approval",
            ),
        ),
        (
            request(),
            policy(),
            context(restricted_topic=True),
            quota(),
            "restricted_topic_requires_approval",
            (
                "enrollment",
                "permission",
                "course_mode",
                "tenant_policy",
                "source_permission",
                "safety",
                "quota",
                "approval",
            ),
        ),
        (
            request(),
            policy(),
            context(),
            quota(),
            "accepted",
            (
                "enrollment",
                "permission",
                "course_mode",
                "tenant_policy",
                "source_permission",
                "safety",
                "quota",
                "approval",
                "accepted",
            ),
        ),
    ],
)
def test_policy_checks_fail_closed_in_the_fixed_order(
    selected_request: StudentGenerationRequest,
    selected_policy: CourseGenerationPolicy,
    selected_context: StudentGenerationEvaluationContext,
    selected_quota: StudentGenerationQuota,
    reason: str,
    checks: tuple[str, ...],
) -> None:
    decision = evaluate_student_request(
        policy=selected_policy,
        request=selected_request,
        context=selected_context,
        quota=selected_quota,
    )

    assert decision.reason == reason
    assert decision.evaluated_checks == checks


def test_minor_safety_mode_requires_minor_safe_evidence() -> None:
    decision = evaluate_student_request(
        policy=policy(minor_safety_mode=True),
        request=request(),
        context=context(minor_safe=False),
        quota=quota(),
    )

    assert decision.outcome == "denied"
    assert decision.reason == "minor_safety_restriction"


@pytest.mark.parametrize("mode", ["micro", "full"])
def test_estimation_is_positive_deterministic_and_bounded(mode: str) -> None:
    selected_policy = policy(micro_scene_limit=4, full_scene_limit=19)
    selected_request = request(mode=mode)

    first = estimate_student_request(
        policy=selected_policy,
        request=selected_request,
        context=context(),
        quota=quota(),
    )
    second = estimate_student_request(
        policy=selected_policy,
        request=replace(selected_request),
        context=replace(context()),
        quota=replace(quota()),
    )

    assert first == second
    assert 1 <= first.scene_range[0] <= first.scene_range[1]
    assert 1 <= first.duration_minutes_range[0] <= first.duration_minutes_range[1]
    assert first.quota_units > 0
    if mode == "micro":
        assert first.scene_range[1] == 4
        assert first.scene_range[1] <= 5
        assert first.requires_outline_confirmation is False
    else:
        assert first.scene_range[1] == 19
        assert first.scene_range[1] <= 24
        assert first.requires_outline_confirmation is True


def test_student_generation_orm_tables_have_strict_constraints_and_links() -> None:
    policy_table = CourseGenerationPolicyRecord.__table__
    request_table = StudentGenerationRequestRecord.__table__
    approval_table = StudentGenerationApprovalRecord.__table__

    policy_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in policy_table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert (
        "micro_scene_limit >= 1" in policy_checks["ck_course_generation_policies_micro_scene_limit"]
    )
    assert (
        "micro_scene_limit <= 5" in policy_checks["ck_course_generation_policies_micro_scene_limit"]
    )
    assert (
        "full_scene_limit <= 24" in policy_checks["ck_course_generation_policies_full_scene_limit"]
    )
    assert (
        "daily_student_units >= 0"
        in policy_checks["ck_course_generation_policies_daily_student_units"]
    )

    request_foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in request_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("class_id", "course_id"),
        ("tenant.classes.id", "tenant.classes.course_id"),
    ) in request_foreign_keys
    request_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in request_table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert (
        "quota_state IN ('none', 'reserved', 'settled', 'released')"
        in request_checks["ck_student_generation_requests_quota_state"]
    )
    assert (
        "decision_outcome = 'accepted'"
        in request_checks["ck_student_generation_requests_quota_lifecycle"]
    )
    assert (
        ("course_id", "tenant_id"),
        (
            "tenant.course_generation_policies.course_id",
            "tenant.course_generation_policies.tenant_id",
        ),
    ) in request_foreign_keys

    approval_foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in approval_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("request_id", "tenant_id"),
        (
            "tenant.student_generation_requests.id",
            "tenant.student_generation_requests.tenant_id",
        ),
    ) in approval_foreign_keys
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("id", "tenant_id")
        for constraint in request_table.constraints
    )
    assert any(
        isinstance(index, Index)
        and index.name == "uq_student_generation_approvals_pending_request"
        and index.unique
        for index in approval_table.indexes
    )


def test_student_generation_migration_is_the_tenant_head() -> None:
    migration = importlib.import_module(
        "deeptutor.teaching.migrations.versions.20260809_0013_student_generation"
    )

    assert migration.revision == "20260809_0013"
    assert migration.down_revision == "20260804_0012"
