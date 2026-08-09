"""Pure student classroom generation policy and estimation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ContentMode = Literal["source_grounded", "open_creation"]
GenerationMode = Literal["micro", "full"]
DecisionOutcome = Literal["denied", "approval_required", "accepted"]

_CONTENT_MODES = frozenset({"source_grounded", "open_creation"})


def _bounded_identifier(value: str, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field} is invalid")


def _nonnegative_integer(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be nonnegative")


@dataclass(frozen=True, slots=True)
class CourseGenerationPolicy:
    """Per-learner course allowance; zero fails closed into teacher approval."""

    allowed_content_modes: frozenset[ContentMode]
    daily_student_units: int
    monthly_student_units: int
    allow_student_micro: bool = True
    allow_student_full: bool = False
    allow_web_search: bool = False
    require_approval_for_restricted_topics: bool = True
    minor_safety_mode: bool = True
    micro_scene_limit: int = 5
    full_scene_limit: int = 24

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed_content_modes, frozenset)
            or not self.allowed_content_modes
            or not self.allowed_content_modes.issubset(_CONTENT_MODES)
        ):
            raise ValueError("allowed_content_modes is invalid")
        for field in (
            "allow_student_micro",
            "allow_student_full",
            "allow_web_search",
            "require_approval_for_restricted_topics",
            "minor_safety_mode",
        ):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"{field} must be boolean")
        if not 1 <= self.micro_scene_limit <= 5:
            raise ValueError("micro_scene_limit must be between 1 and 5")
        if not 1 <= self.full_scene_limit <= 24:
            raise ValueError("full_scene_limit must be between 1 and 24")
        _nonnegative_integer(self.daily_student_units, "daily_student_units")
        _nonnegative_integer(self.monthly_student_units, "monthly_student_units")


@dataclass(frozen=True, slots=True)
class StudentGenerationRequest:
    """Only user-controlled classroom choices; no trusted authorization facts."""

    course_id: str
    class_id: str
    mode: GenerationMode
    content_mode: ContentMode
    web_search_requested: bool

    def __post_init__(self) -> None:
        _bounded_identifier(self.course_id, "course_id", 64)
        _bounded_identifier(self.class_id, "class_id", 64)
        if self.mode not in {"micro", "full"}:
            raise ValueError("mode is invalid")
        if self.content_mode not in _CONTENT_MODES:
            raise ValueError("content_mode is invalid")
        if type(self.web_search_requested) is not bool:
            raise ValueError("web_search_requested is invalid")


@dataclass(frozen=True, slots=True)
class StudentGenerationEvaluationContext:
    """Trusted facts loaded independently of user-controlled request data."""

    enrolled: bool
    has_generation_permission: bool
    source_permitted: bool
    generally_safe: bool
    minor_safe: bool
    restricted_topic: bool
    approval_granted: bool

    def __post_init__(self) -> None:
        for field in (
            "enrolled",
            "has_generation_permission",
            "source_permitted",
            "generally_safe",
            "minor_safe",
            "restricted_topic",
            "approval_granted",
        ):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"{field} must be boolean")


@dataclass(frozen=True, slots=True)
class StudentGenerationQuota:
    daily_used_units: int
    monthly_used_units: int

    def __post_init__(self) -> None:
        _nonnegative_integer(self.daily_used_units, "daily_used_units")
        _nonnegative_integer(self.monthly_used_units, "monthly_used_units")


@dataclass(frozen=True, slots=True)
class StudentGenerationEstimate:
    scene_range: tuple[int, int]
    duration_minutes_range: tuple[int, int]
    quota_units: int
    requires_outline_confirmation: bool
    requires_approval: bool


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: DecisionOutcome
    reason: str
    estimated_units: int
    evaluated_checks: tuple[str, ...]


def estimate_student_request(
    *,
    policy: CourseGenerationPolicy,
    request: StudentGenerationRequest,
    context: StudentGenerationEvaluationContext,
    quota: StudentGenerationQuota,
) -> StudentGenerationEstimate:
    """Return a deterministic bounded estimate without mutating policy state."""

    scene_max = (
        min(policy.micro_scene_limit, 5)
        if request.mode == "micro"
        else min(policy.full_scene_limit, 24)
    )
    scene_min = 1 if request.mode == "micro" else min(6, scene_max)
    quota_units = scene_max
    quota_exceeded = (
        quota.daily_used_units + quota_units > policy.daily_student_units
        or quota.monthly_used_units + quota_units > policy.monthly_student_units
    )
    restricted_approval = context.restricted_topic and policy.require_approval_for_restricted_topics
    return StudentGenerationEstimate(
        scene_range=(scene_min, scene_max),
        duration_minutes_range=(scene_min * 3, scene_max * 5),
        quota_units=quota_units,
        requires_outline_confirmation=request.mode == "full",
        requires_approval=(quota_exceeded or restricted_approval) and not context.approval_granted,
    )


def evaluate_student_request(
    *,
    policy: CourseGenerationPolicy,
    request: StudentGenerationRequest,
    context: StudentGenerationEvaluationContext,
    quota: StudentGenerationQuota,
) -> PolicyDecision:
    """Evaluate the fixed fail-closed check sequence."""

    estimate = estimate_student_request(
        policy=policy,
        request=request,
        context=context,
        quota=quota,
    )
    checks: list[str] = []

    def decision(outcome: DecisionOutcome, reason: str) -> PolicyDecision:
        return PolicyDecision(
            outcome=outcome,
            reason=reason,
            estimated_units=estimate.quota_units,
            evaluated_checks=tuple(checks),
        )

    checks.append("enrollment")
    if not context.enrolled:
        return decision("denied", "not_enrolled")

    checks.append("permission")
    if not context.has_generation_permission:
        return decision("denied", "generation_permission_denied")

    checks.append("course_mode")
    if request.mode == "micro" and not policy.allow_student_micro:
        return decision("denied", "micro_classroom_disabled")
    if request.mode == "full" and not policy.allow_student_full:
        return decision("denied", "full_classroom_disabled")

    checks.append("tenant_policy")
    if request.content_mode not in policy.allowed_content_modes:
        return decision("denied", "content_mode_disabled")
    if request.web_search_requested and not policy.allow_web_search:
        return decision("denied", "web_search_disabled")

    checks.append("source_permission")
    if request.content_mode == "source_grounded" and not context.source_permitted:
        return decision("denied", "source_permission_denied")

    checks.append("safety")
    if not context.generally_safe:
        return decision("denied", "safety_restriction")
    if policy.minor_safety_mode and not context.minor_safe:
        return decision("denied", "minor_safety_restriction")

    checks.append("quota")
    quota_exceeded = (
        quota.daily_used_units + estimate.quota_units > policy.daily_student_units
        or quota.monthly_used_units + estimate.quota_units > policy.monthly_student_units
    )

    checks.append("approval")
    if quota_exceeded and not context.approval_granted:
        return decision("approval_required", "quota_exceeded")
    if (
        context.restricted_topic
        and policy.require_approval_for_restricted_topics
        and not context.approval_granted
    ):
        return decision(
            "approval_required",
            "restricted_topic_requires_approval",
        )

    checks.append("accepted")
    return decision("accepted", "accepted")
