"""PostgreSQL repository for exact-bound classroom review decisions."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import json

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.contracts import canonical_json_bytes
from deeptutor.teaching.models.classrooms import (
    Approval,
    ClassroomAsset,
    ClassroomDraft,
    ClassroomReviewPolicy,
    ClassroomReviewRequest,
    TeachingBrief,
    transition,
)
from deeptutor.teaching.models.platform import AuditLog
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.reviews import (
    DecideReviewCommand,
    ReviewBlocked,
    ReviewConflict,
    ReviewPersistenceError,
    ReviewPolicy,
    ReviewRecord,
    ReviewTarget,
    ReviewValidationStale,
    SubmitReviewCommand,
)


def _digest(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:48]}"


def _decode_object(value: str, *, field: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        raise ReviewPersistenceError(f"stored classroom {field} is invalid") from None
    if not isinstance(decoded, dict):
        raise ReviewPersistenceError(f"stored classroom {field} is invalid")
    return decoded


def _warnings(value: str) -> tuple[dict[str, object], ...]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        raise ReviewPersistenceError("stored review warnings are invalid") from None
    if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
        raise ReviewPersistenceError("stored review warnings are invalid")
    return tuple(dict(item) for item in decoded)


class SqlAlchemyReviewRepository:
    """Write review requests and append-only decision events in one tenant."""

    def __init__(self, engine: AsyncEngine, tenant_id: str) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(translated, expire_on_commit=False)

    @staticmethod
    def _record(model: ClassroomReviewRequest) -> ReviewRecord:
        return ReviewRecord(
            id=model.id,
            tenant_id=model.tenant_id,
            asset_id=model.classroom_id,
            draft_id=model.classroom_draft_id,
            draft_revision=model.draft_revision,
            document_sha256=model.document_sha256,
            validation_report_sha256=model.validation_report_sha256,
            submitted_by=model.submitted_by,
            scope=model.scope,  # type: ignore[arg-type]
            class_id=model.class_id,
            status=model.status,  # type: ignore[arg-type]
            warnings=_warnings(model.warnings),
            reviewer_id=model.decided_by,
            comment=model.decision_comment,
        )

    def _target_statement(self, asset_id: str):
        return (
            select(ClassroomAsset, ClassroomDraft, TeachingBrief)
            .join(
                ClassroomDraft,
                and_(
                    ClassroomDraft.classroom_id == ClassroomAsset.id,
                    ClassroomDraft.tenant_id == ClassroomAsset.tenant_id,
                ),
            )
            .join(
                TeachingBrief,
                and_(
                    TeachingBrief.id == ClassroomDraft.teaching_brief_id,
                    TeachingBrief.tenant_id == ClassroomDraft.tenant_id,
                ),
            )
            .where(
                ClassroomAsset.id == asset_id,
                ClassroomAsset.tenant_id == self._tenant_id,
            )
            .order_by(ClassroomDraft.updated_at.desc(), ClassroomDraft.id)
            .limit(1)
        )

    async def get_policy(self) -> ReviewPolicy:
        async with self._session_factory() as session:
            policy = await session.get(ClassroomReviewPolicy, self._tenant_id)
            if policy is None:
                return ReviewPolicy()
            return ReviewPolicy(
                teacher_self_publish=policy.teacher_self_publish,
                org_content_requires_review=policy.org_content_requires_review,
                platform_template_requires_review=policy.platform_template_requires_review,
                prohibit_self_review=policy.prohibit_self_review,
            )

    async def set_policy(self, policy: ReviewPolicy, *, actor_id: str) -> ReviewPolicy:
        if not actor_id or len(actor_id) > 128:
            raise ValueError("actor_id is invalid")
        async with self._session_factory() as session:
            async with session.begin():
                current = await session.scalar(
                    select(ClassroomReviewPolicy)
                    .where(ClassroomReviewPolicy.tenant_id == self._tenant_id)
                    .with_for_update()
                )
                if current is None:
                    current = ClassroomReviewPolicy(
                        tenant_id=self._tenant_id,
                        updated_by=actor_id,
                    )
                    session.add(current)
                current.teacher_self_publish = policy.teacher_self_publish
                current.org_content_requires_review = policy.org_content_requires_review
                current.platform_template_requires_review = (
                    policy.platform_template_requires_review
                )
                current.prohibit_self_review = policy.prohibit_self_review
                current.updated_by = actor_id
                current.updated_at = func.now()
                session.add(
                    AuditLog(
                        tenant_id=self._tenant_id,
                        actor_id=actor_id,
                        action="teaching.review_policy.updated",
                        resource_type="classroom_review_policy",
                        resource_id=self._tenant_id,
                    )
                )
                await session.flush()
        return policy

    async def get_target(self, asset_id: str) -> ReviewTarget | None:
        async with self._session_factory() as session:
            row = (await session.execute(self._target_statement(asset_id))).one_or_none()
            if row is None:
                return None
            asset, _, brief = row
            if brief.course_id is None or brief.class_id is None:
                return None
            return ReviewTarget(
                tenant_id=asset.tenant_id,
                asset_id=asset.id,
                owner_id=asset.owner_id,
                course_id=brief.course_id,
                class_id=brief.class_id,
            )

    async def get_review(self, review_id: str) -> ReviewRecord | None:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(ClassroomReviewRequest).where(
                    ClassroomReviewRequest.id == review_id,
                    ClassroomReviewRequest.tenant_id == self._tenant_id,
                )
            )
            return self._record(model) if model is not None else None

    async def list_pending(self) -> tuple[ReviewRecord, ...]:
        async with self._session_factory() as session:
            models = (
                await session.scalars(
                    select(ClassroomReviewRequest)
                    .where(
                        ClassroomReviewRequest.tenant_id == self._tenant_id,
                        ClassroomReviewRequest.status == "pending",
                    )
                    .order_by(ClassroomReviewRequest.created_at, ClassroomReviewRequest.id)
                )
            ).all()
            return tuple(self._record(model) for model in models)

    @staticmethod
    def _validated_report(draft: ClassroomDraft) -> tuple[str, tuple[dict[str, object], ...]]:
        if (
            draft.validation_report is None
            or draft.validation_report_sha256 is None
            or draft.validation_revision != draft.revision
            or draft.validation_document_sha256 is None
            or not hmac.compare_digest(
                draft.validation_document_sha256,
                draft.document_sha256,
            )
        ):
            raise ReviewValidationStale("classroom validation is stale")
        report = _decode_object(draft.validation_report, field="validation report")
        canonical_sha256 = _digest(report)
        if (
            not hmac.compare_digest(canonical_sha256, draft.validation_report_sha256)
            or report.get("draftRevision") != draft.revision
            or report.get("documentSha256") != draft.document_sha256
        ):
            raise ReviewValidationStale("classroom validation is stale")
        severe = report.get("severeFindings")
        if not isinstance(severe, list):
            raise ReviewPersistenceError("stored classroom validation report is invalid")
        if report.get("valid") is not True or severe:
            raise ReviewBlocked("classroom validation has blockers")
        raw_warnings = report.get("warnings")
        if not isinstance(raw_warnings, list) or any(
            not isinstance(item, dict) for item in raw_warnings
        ):
            raise ReviewPersistenceError("stored classroom validation report is invalid")
        return canonical_sha256, tuple(dict(item) for item in raw_warnings)

    @staticmethod
    def _verify_idempotent(
        model: ClassroomReviewRequest,
        *,
        request_sha256: str,
    ) -> ReviewRecord:
        if not hmac.compare_digest(model.request_sha256, request_sha256):
            raise ReviewConflict("review idempotency key conflicts")
        return SqlAlchemyReviewRepository._record(model)

    async def submit(self, command: SubmitReviewCommand) -> ReviewRecord:
        if command.tenant_id != self._tenant_id:
            raise ReviewConflict("review tenant conflicts")
        request_sha256 = _digest(
            {
                "tenantId": command.tenant_id,
                "assetId": command.asset_id,
                "actorId": command.actor_id,
                "scope": command.scope,
                "classId": command.class_id,
            }
        )
        review_id = _identifier(
            "review",
            self._tenant_id,
            command.idempotency_key,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(ClassroomReviewRequest)
                        .where(
                            ClassroomReviewRequest.tenant_id == self._tenant_id,
                            ClassroomReviewRequest.idempotency_key
                            == command.idempotency_key,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        return self._verify_idempotent(
                            existing,
                            request_sha256=request_sha256,
                        )
                    row = (
                        await session.execute(
                            self._target_statement(command.asset_id).with_for_update()
                        )
                    ).one_or_none()
                    if row is None:
                        raise ReviewConflict("classroom review target is unavailable")
                    asset, draft, brief = row
                    if brief.course_id is None or brief.class_id is None:
                        raise ReviewConflict("classroom review target is unavailable")
                    if asset.lifecycle_state != "editing":
                        raise ReviewConflict("classroom cannot be submitted")
                    if command.scope == "class":
                        if command.class_id != brief.class_id:
                            raise ReviewConflict("classroom review scope conflicts")
                    elif command.class_id is not None:
                        raise ReviewConflict("classroom review scope conflicts")
                    validation_sha256, warnings = self._validated_report(draft)
                    warnings_payload = canonical_json_bytes(list(warnings)).decode()
                    model = ClassroomReviewRequest(
                        id=review_id,
                        tenant_id=self._tenant_id,
                        classroom_id=asset.id,
                        classroom_draft_id=draft.id,
                        draft_revision=draft.revision,
                        document_sha256=draft.document_sha256,
                        validation_report_sha256=validation_sha256,
                        submitted_by=command.actor_id,
                        scope=command.scope,
                        class_id=command.class_id,
                        status="pending",
                        warnings=warnings_payload,
                        idempotency_key=command.idempotency_key,
                        request_sha256=request_sha256,
                        decided_by=None,
                        decision_comment=None,
                        decided_at=None,
                    )
                    session.add(model)
                    # No ORM relationship links the durable request to its audit
                    # event, so make the foreign-key order explicit.
                    await session.flush([model])
                    session.add(
                        Approval(
                            id=_identifier("approval", review_id, "submitted"),
                            tenant_id=self._tenant_id,
                            classroom_id=asset.id,
                            classroom_draft_id=draft.id,
                            submitted_by=command.actor_id,
                            reviewer_id=None,
                            decision="submitted",
                            reason=None,
                            review_request_id=review_id,
                        )
                    )
                    asset.lifecycle_state = transition("editing", "submitted")
                    asset.updated_at = func.now()
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=command.actor_id,
                            action="teaching.classroom.submitted",
                            resource_type="classroom_review",
                            resource_id=review_id,
                        )
                    )
                    await session.flush()
                    return self._record(model)
        except IntegrityError as exc:
            existing = await self.get_review(review_id)
            if existing is not None:
                async with self._session_factory() as session:
                    model = await session.scalar(
                        select(ClassroomReviewRequest).where(
                            ClassroomReviewRequest.id == review_id,
                            ClassroomReviewRequest.tenant_id == self._tenant_id,
                        )
                    )
                    if model is not None:
                        return self._verify_idempotent(
                            model,
                            request_sha256=request_sha256,
                        )
            raise ReviewConflict("classroom submission conflicts") from exc

    async def decide(self, command: DecideReviewCommand) -> ReviewRecord:
        if command.tenant_id != self._tenant_id:
            raise ReviewConflict("review tenant conflicts")
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    model = await session.scalar(
                        select(ClassroomReviewRequest)
                        .where(
                            ClassroomReviewRequest.id == command.review_id,
                            ClassroomReviewRequest.tenant_id == self._tenant_id,
                        )
                        .with_for_update()
                    )
                    if model is None:
                        raise ReviewConflict("classroom review is unavailable")
                    if model.status != "pending":
                        raise ReviewConflict("classroom review was already decided")
                    row = (
                        await session.execute(
                            select(ClassroomAsset, ClassroomDraft)
                            .join(
                                ClassroomDraft,
                                and_(
                                    ClassroomDraft.classroom_id == ClassroomAsset.id,
                                    ClassroomDraft.tenant_id == ClassroomAsset.tenant_id,
                                ),
                            )
                            .where(
                                ClassroomAsset.id == model.classroom_id,
                                ClassroomAsset.tenant_id == self._tenant_id,
                                ClassroomDraft.id == model.classroom_draft_id,
                            )
                            .with_for_update()
                        )
                    ).one_or_none()
                    if row is None:
                        raise ReviewConflict("classroom review target is unavailable")
                    asset, draft = row
                    if (
                        asset.lifecycle_state != "submitted"
                        or draft.revision != model.draft_revision
                        or not hmac.compare_digest(
                            draft.document_sha256,
                            model.document_sha256,
                        )
                        or draft.validation_report_sha256 is None
                        or not hmac.compare_digest(
                            draft.validation_report_sha256,
                            model.validation_report_sha256,
                        )
                    ):
                        raise ReviewValidationStale("classroom validation is stale")
                    self._validated_report(draft)
                    session.add(
                        Approval(
                            id=_identifier(
                                "approval",
                                command.review_id,
                                command.decision,
                            ),
                            tenant_id=self._tenant_id,
                            classroom_id=model.classroom_id,
                            classroom_draft_id=model.classroom_draft_id,
                            submitted_by=model.submitted_by,
                            reviewer_id=command.actor_id,
                            decision=command.decision,
                            reason=command.comment,
                            review_request_id=model.id,
                        )
                    )
                    model.status = command.decision
                    model.decided_by = command.actor_id
                    model.decision_comment = command.comment
                    model.decided_at = func.now()
                    if command.decision == "approved":
                        asset.lifecycle_state = transition("submitted", "approved")
                    else:
                        asset.lifecycle_state = transition("submitted", "rejected")
                        asset.lifecycle_state = transition("rejected", "editing")
                        draft.revision += 1
                        draft.validation_report = None
                        draft.validation_report_sha256 = None
                        draft.validation_revision = None
                        draft.validation_document_sha256 = None
                        draft.updated_by = command.actor_id
                        draft.updated_at = func.now()
                    asset.updated_at = func.now()
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=command.actor_id,
                            action=f"teaching.classroom.{command.decision}",
                            resource_type="classroom_review",
                            resource_id=model.id,
                        )
                    )
                    await session.flush()
                    record = self._record(model)
                    if command.decision == "rejected":
                        record = replace(record, draft_revision=draft.revision)
                    return record
        except IntegrityError as exc:
            raise ReviewConflict("classroom review was already decided") from exc


__all__ = ["SqlAlchemyReviewRepository"]
