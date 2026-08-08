"""Resource-scoped classroom review policy and orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
from typing import AsyncIterator, Literal, Mapping, Protocol

from deeptutor.teaching.artifacts import classroom_artifact_key
from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
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
class ReviewSourceFragment:
    fragment_id: str
    source_id: str
    text: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ReviewBaseline:
    version_id: str
    version_number: int
    document_sha256: str
    document_object_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReviewDetailEvidence:
    review: ReviewRecord
    target: ReviewTarget
    title: str
    course_id: str
    target_class_id: str
    document: dict[str, object]
    validation_report: dict[str, object]
    source_fragments: tuple[ReviewSourceFragment, ...]
    baseline: ReviewBaseline | None


@dataclass(frozen=True, slots=True)
class ReviewDetail:
    review: ReviewRecord
    title: str
    course_id: str
    target_class_id: str
    document: dict[str, object]
    validation_report: dict[str, object]
    source_fragments: tuple[ReviewSourceFragment, ...]
    baseline: ReviewBaseline | None
    changed_paths: tuple[str, ...]


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

    async def get_detail(self, review_id: str) -> ReviewDetailEvidence | None: ...

    async def decide(self, command: DecideReviewCommand) -> ReviewRecord: ...


class _ReviewDocumentStore(Protocol):
    async def open(self, object_key: str) -> AsyncIterator[bytes]: ...


class ReviewDocumentStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> _ReviewDocumentStore: ...


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


def _content_document(document: Mapping[str, object]) -> dict[str, object]:
    content = dict(document)
    content.pop("fileSha256", None)
    return content


def _pointer(path: tuple[str, ...]) -> str:
    if not path:
        return "/"
    return "/" + "/".join(
        segment.replace("~", "~0").replace("/", "~1") for segment in path
    )


def _changed_json_pointers(
    baseline: object,
    submitted: object,
    *,
    max_paths: int = 256,
    max_depth: int = 64,
) -> tuple[str, ...]:
    changed: list[str] = []
    overflow = False

    def add(path: tuple[str, ...]) -> None:
        nonlocal overflow
        if len(changed) >= max_paths:
            overflow = True
            return
        changed.append(_pointer(path))

    def walk(before: object, after: object, path: tuple[str, ...], depth: int) -> None:
        if overflow or before == after:
            return
        if depth >= max_depth:
            add(path)
            return
        if isinstance(before, Mapping) and isinstance(after, Mapping):
            for key in sorted(set(before) | set(after)):
                child = (*path, str(key))
                if key not in before or key not in after:
                    add(child)
                else:
                    walk(before[key], after[key], child, depth + 1)
            return
        if isinstance(before, list) and isinstance(after, list):
            for index in range(max(len(before), len(after))):
                child = (*path, str(index))
                if index >= len(before) or index >= len(after):
                    add(child)
                else:
                    walk(before[index], after[index], child, depth + 1)
            return
        add(path)

    walk(baseline, submitted, (), 0)
    if overflow:
        return ("/",)
    return tuple(changed)


class ReviewService:
    """Apply actor policy before atomic repository submission/decision writes."""

    def __init__(
        self,
        repository: ReviewRepository,
        store_provider: ReviewDocumentStoreProvider | None = None,
    ) -> None:
        self._repository = repository
        self._store_provider = store_provider

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

    async def detail(
        self,
        context: TenantContext,
        review_id: str,
    ) -> ReviewDetail:
        evidence = await self._repository.get_detail(review_id)
        if (
            evidence is None
            or evidence.review.tenant_id != context.tenant_id
            or evidence.target.tenant_id != context.tenant_id
            or evidence.review.asset_id != evidence.target.asset_id
        ):
            raise ReviewNotFound("classroom review was not found")
        if not await self._can_review(context, evidence.review, evidence.target):
            if evidence.review.submitted_by == context.user_id:
                raise ReviewAccessDenied("classroom self-review is prohibited")
            raise ReviewAccessDenied("classroom review is denied")
        if evidence.baseline is None:
            changed_paths = ("/",)
        else:
            baseline_document = await self._load_baseline_document(
                context,
                evidence,
            )
            changed_paths = _changed_json_pointers(
                _content_document(baseline_document),
                _content_document(evidence.document),
            )
        return ReviewDetail(
            review=evidence.review,
            title=evidence.title,
            course_id=evidence.course_id,
            target_class_id=evidence.target_class_id,
            document=evidence.document,
            validation_report=evidence.validation_report,
            source_fragments=evidence.source_fragments,
            baseline=evidence.baseline,
            changed_paths=changed_paths,
        )

    async def _load_baseline_document(
        self,
        context: TenantContext,
        evidence: ReviewDetailEvidence,
    ) -> dict[str, object]:
        baseline = evidence.baseline
        if baseline is None or self._store_provider is None:
            raise ReviewPersistenceError("review baseline is unavailable")
        try:
            expected_key = classroom_artifact_key(
                context.tenant_id,
                evidence.target.asset_id,
                baseline.version_number,
                "classroom.json",
            )
        except ValueError:
            raise ReviewPersistenceError("review baseline is invalid") from None
        if not hmac.compare_digest(expected_key, baseline.document_object_key):
            raise ReviewPersistenceError("review baseline is invalid")
        chunks: list[bytes] = []
        size = 0
        try:
            store = await self._store_provider.store_for_tenant(context.tenant_id)
            async for chunk in await store.open(baseline.document_object_key):
                if not isinstance(chunk, bytes):
                    raise ReviewPersistenceError("review baseline is invalid")
                size += len(chunk)
                if size > 16 * 1024 * 1024:
                    raise ReviewPersistenceError("review baseline is invalid")
                chunks.append(chunk)
        except asyncio.CancelledError:
            raise
        except ReviewPersistenceError:
            raise
        except Exception:
            raise ReviewPersistenceError("review baseline is unavailable") from None
        body = b"".join(chunks)
        if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), baseline.document_sha256):
            raise ReviewPersistenceError("review baseline is invalid")
        try:
            parsed = ClassroomDocument.model_validate_json(body)
        except Exception:
            raise ReviewPersistenceError("review baseline is invalid") from None
        if canonical_json_bytes(parsed) != body:
            raise ReviewPersistenceError("review baseline is invalid")
        if (
            parsed.classroom_id != evidence.target.asset_id
            or parsed.classroom_version_id != baseline.version_id
        ):
            raise ReviewPersistenceError("review baseline is invalid")
        raw = parsed.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        file_sha256 = raw.pop("fileSha256")
        if not hmac.compare_digest(
            hashlib.sha256(canonical_json_bytes(raw)).hexdigest(),
            file_sha256,
        ):
            raise ReviewPersistenceError("review baseline is invalid")
        return parsed.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )

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
    "ReviewBaseline",
    "ReviewBlocked",
    "ReviewConflict",
    "ReviewDetail",
    "ReviewDetailEvidence",
    "ReviewDocumentStoreProvider",
    "ReviewError",
    "ReviewNotFound",
    "ReviewPersistenceError",
    "ReviewPolicy",
    "ReviewRecord",
    "ReviewRepository",
    "ReviewService",
    "ReviewSourceFragment",
    "ReviewTarget",
    "ReviewValidationStale",
    "SubmitReviewCommand",
]
