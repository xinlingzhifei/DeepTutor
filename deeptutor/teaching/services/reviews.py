"""Resource-scoped classroom review policy and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Protocol

from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.tenant_context import TenantContext

ReviewScope = Literal["class", "tenant", "platform"]
ReviewStatus = Literal["pending", "approved", "rejected"]


class ReviewError(RuntimeError):
    """Base class for fixed-safe review failures."""


class ReviewAccessDenied(ReviewError, PermissionError):
    """The actor cannot submit or decide this review."""


class ReviewNotFound(ReviewError, LookupError):
    """The review or classroom is not visible in the active tenant."""


class ReviewConflict(ReviewError):
    """The review state changed or was already decided."""


class ReviewValidationStale(ReviewConflict):
    """The validation report is not bound to the current draft."""


class ReviewBlocked(ReviewError):
    """The current validation report contains severe blockers."""


class ReviewPersistenceError(ReviewError):
    """Review persistence is unavailable or inconsistent."""


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    """Tenant review policy. Self-publish remains disabled unless persisted."""

    teacher_self_publish: bool = False
    org_content_requires_review: bool = True
    platform_template_requires_review: bool = True
    prohibit_self_review: bool = True


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    tenant_id: str
    asset_id: str
    owner_id: str
    course_id: str
    class_id: str


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    id: str
    tenant_id: str
    asset_id: str
    draft_id: str
    draft_revision: int
    document_sha256: str
    validation_report_sha256: str
    submitted_by: str
    scope: ReviewScope
    class_id: str | None
    status: ReviewStatus
    warnings: tuple[Mapping[str, object], ...]
    reviewer_id: str | None
    comment: str | None


@dataclass(frozen=True, slots=True)
class SubmitReviewCommand:
    tenant_id: str
    asset_id: str
    actor_id: str
    scope: ReviewScope
    class_id: str | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class DecideReviewCommand:
    tenant_id: str
    review_id: str
    actor_id: str
    decision: Literal["approved", "rejected"]
    comment: str


class ReviewRepository(Protocol):
    async def get_policy(self) -> ReviewPolicy: ...

    async def get_target(self, asset_id: str) -> ReviewTarget | None: ...

    async def submit(self, command: SubmitReviewCommand) -> ReviewRecord: ...

    async def list_pending(self) -> tuple[ReviewRecord, ...]: ...

    async def get_review(self, review_id: str) -> ReviewRecord | None: ...

    async def decide(self, command: DecideReviewCommand) -> ReviewRecord: ...


def _allows(
    context: TenantContext,
    permission: str,
    target: ReviewTarget,
) -> bool:
    resource = ResourceScope(
        tenant_id=context.tenant_id,
        course_id=target.course_id,
        class_id=target.class_id,
    )
    return any(
        grant.allows_resource(permission, resource)
        for grant in context.permissions
    )


def _platform_reviewer(context: TenantContext, target: ReviewTarget) -> bool:
    return _allows(context, "classroom.approve", target) and _allows(
        context,
        "template.manage",
        target,
    )


class ReviewService:
    """Apply actor policy before atomic repository submission/decision writes."""

    def __init__(self, repository: ReviewRepository) -> None:
        self._repository = repository

    async def submit(
        self,
        context: TenantContext,
        asset_id: str,
        *,
        scope: ReviewScope,
        class_id: str | None,
        idempotency_key: str,
    ) -> ReviewRecord:
        target = await self._repository.get_target(asset_id)
        if target is None or target.tenant_id != context.tenant_id:
            raise ReviewNotFound("classroom review target was not found")
        if not _allows(context, "classroom.submit", target):
            raise ReviewAccessDenied("classroom submission is denied")
        if scope == "class":
            if class_id != target.class_id:
                raise ReviewAccessDenied("classroom review scope is denied")
        elif class_id is not None:
            raise ReviewConflict("class_id is only valid for class review")
        if scope == "platform" and not _allows(context, "template.manage", target):
            raise ReviewAccessDenied("platform template submission is denied")
        return await self._repository.submit(
            SubmitReviewCommand(
                tenant_id=context.tenant_id,
                asset_id=asset_id,
                actor_id=context.user_id,
                scope=scope,
                class_id=class_id,
                idempotency_key=idempotency_key,
            )
        )

    async def list(self, context: TenantContext) -> tuple[ReviewRecord, ...]:
        visible: list[ReviewRecord] = []
        for review in await self._repository.list_pending():
            if review.tenant_id != context.tenant_id:
                continue
            target = await self._repository.get_target(review.asset_id)
            if target is None or target.tenant_id != context.tenant_id:
                continue
            if await self._can_review(context, review, target):
                visible.append(review)
        return tuple(visible)

    async def approve(
        self,
        context: TenantContext,
        review_id: str,
        comment: str,
    ) -> ReviewRecord:
        return await self._decide(context, review_id, "approved", comment)

    async def reject(
        self,
        context: TenantContext,
        review_id: str,
        comment: str,
    ) -> ReviewRecord:
        return await self._decide(context, review_id, "rejected", comment)

    async def _can_review(
        self,
        context: TenantContext,
        review: ReviewRecord,
        target: ReviewTarget,
    ) -> bool:
        policy = await self._repository.get_policy()
        if policy.prohibit_self_review and review.submitted_by == context.user_id:
            return False
        if review.scope == "platform":
            return _platform_reviewer(context, target)
        return _allows(context, "classroom.approve", target)

    async def _decide(
        self,
        context: TenantContext,
        review_id: str,
        decision: Literal["approved", "rejected"],
        comment: str,
    ) -> ReviewRecord:
        review = await self._repository.get_review(review_id)
        if review is None or review.tenant_id != context.tenant_id:
            raise ReviewNotFound("classroom review was not found")
        if review.status != "pending":
            raise ReviewConflict("classroom review was already decided")
        target = await self._repository.get_target(review.asset_id)
        if target is None or target.tenant_id != context.tenant_id:
            raise ReviewNotFound("classroom review was not found")
        policy = await self._repository.get_policy()
        if policy.prohibit_self_review and review.submitted_by == context.user_id:
            raise ReviewAccessDenied("classroom self-review is prohibited")
        if review.scope == "platform":
            allowed = _platform_reviewer(context, target)
        else:
            allowed = _allows(context, "classroom.approve", target)
        if not allowed:
            raise ReviewAccessDenied("classroom review is denied")
        normalized_comment = comment.strip()
        if not normalized_comment or len(normalized_comment) > 4000:
            raise ReviewConflict("review comment is invalid")
        return await self._repository.decide(
            DecideReviewCommand(
                tenant_id=context.tenant_id,
                review_id=review_id,
                actor_id=context.user_id,
                decision=decision,
                comment=normalized_comment,
            )
        )


__all__ = [
    "DecideReviewCommand",
    "ReviewAccessDenied",
    "ReviewBlocked",
    "ReviewConflict",
    "ReviewError",
    "ReviewNotFound",
    "ReviewPersistenceError",
    "ReviewPolicy",
    "ReviewRecord",
    "ReviewRepository",
    "ReviewService",
    "ReviewTarget",
    "ReviewValidationStale",
    "SubmitReviewCommand",
]
