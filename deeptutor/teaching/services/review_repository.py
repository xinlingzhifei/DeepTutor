"""PostgreSQL repository for exact-bound classroom review decisions."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import json

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.contracts import (
    ClassroomDocument,
    canonical_json_bytes,
    canonical_teaching_brief_sha256,
)
from deeptutor.teaching.contracts import (
    TeachingBrief as TeachingBriefContract,
)
from deeptutor.teaching.models.classrooms import (
    Approval,
    ClassroomAsset,
    ClassroomDraft,
    ClassroomReviewPolicy,
    ClassroomReviewRequest,
    ClassroomVersion,
    Publication,
    SourceSnapshot,
    TeachingBrief,
    transition,
)
from deeptutor.teaching.models.platform import AuditLog
from deeptutor.teaching.repositories.student_visibility import teacher_asset_visible
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.reviews import (
    DecideReviewCommand,
    ReviewAccessDenied,
    ReviewBaseline,
    ReviewBlocked,
    ReviewConflict,
    ReviewDetailEvidence,
    ReviewPersistenceError,
    ReviewPolicy,
    ReviewRecord,
    ReviewSourceFragment,
    ReviewTarget,
    ReviewValidationStale,
    SubmitReviewCommand,
)

_LOWER_HEX = frozenset("0123456789abcdef")


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

    @staticmethod
    def _reviewed_document(
        asset: ClassroomAsset,
        draft: ClassroomDraft,
        review: ClassroomReviewRequest,
    ) -> dict[str, object]:
        payload = _decode_object(draft.document, field="document")
        try:
            document = ClassroomDocument.model_validate(payload)
        except Exception:
            raise ReviewPersistenceError("stored classroom document is invalid") from None
        canonical = canonical_json_bytes(document)
        raw = document.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        file_sha256 = raw.pop("fileSha256")
        if (
            canonical_json_bytes(payload) != canonical
            or draft.revision != review.draft_revision
            or draft.id != review.classroom_draft_id
            or draft.classroom_id != asset.id
            or review.classroom_id != asset.id
            or document.classroom_id != asset.id
            or draft.base_version_id is None
            or document.classroom_version_id != draft.base_version_id
            or not hmac.compare_digest(
                hashlib.sha256(canonical).hexdigest(),
                draft.document_sha256,
            )
            or not hmac.compare_digest(draft.document_sha256, review.document_sha256)
            or not hmac.compare_digest(
                hashlib.sha256(canonical_json_bytes(raw)).hexdigest(),
                file_sha256,
            )
        ):
            raise ReviewValidationStale("classroom review binding is stale")
        return document.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )

    @staticmethod
    def _brief_contract(
        brief: TeachingBrief,
        asset: ClassroomAsset,
    ) -> TeachingBriefContract:
        payload = _decode_object(brief.document, field="teaching brief")
        try:
            contract = TeachingBriefContract.model_validate(payload)
        except Exception:
            raise ReviewPersistenceError("stored classroom teaching brief is invalid") from None
        if (
            brief.tenant_id != asset.tenant_id
            or contract.tenant_id != asset.tenant_id
            or contract.brief_id != brief.id
            or contract.course_id != brief.course_id
            or contract.target_class_id != brief.class_id
            or not hmac.compare_digest(
                canonical_teaching_brief_sha256(contract),
                contract.content_sha256,
            )
            or not hmac.compare_digest(contract.content_sha256, brief.document_sha256)
        ):
            raise ReviewPersistenceError("stored classroom teaching brief is invalid")
        return contract

    @staticmethod
    def _source_fragments(
        snapshot: SourceSnapshot | None,
        brief: TeachingBrief,
        contract: TeachingBriefContract,
    ) -> tuple[ReviewSourceFragment, ...]:
        if brief.source_snapshot_id is None:
            if snapshot is not None or contract.source_snapshot is not None or contract.source_fragments:
                raise ReviewPersistenceError("stored review source evidence is invalid")
            return ()
        if (
            snapshot is None
            or snapshot.tenant_id != brief.tenant_id
            or snapshot.id != brief.source_snapshot_id
            or contract.source_snapshot is None
            or contract.source_snapshot.snapshot_id != snapshot.id
            or not hmac.compare_digest(
                contract.source_snapshot.content_sha256,
                snapshot.content_sha256,
            )
        ):
            raise ReviewPersistenceError("stored review source evidence is invalid")
        try:
            manifest = json.loads(snapshot.citation_manifest)
        except (TypeError, ValueError):
            raise ReviewPersistenceError("stored review source evidence is invalid") from None
        expected_keys = {
            "schema_version",
            "snapshot_id",
            "source_kind",
            "source_id",
            "source_snapshot_sha256",
            "fragments",
            "source_refs",
            "permission_summary",
            "query_sha256",
            "retrieval",
            "created_by",
        }
        if (
            not isinstance(manifest, dict)
            or set(manifest) != expected_keys
            or manifest.get("schema_version") != 1
            or manifest.get("snapshot_id") != snapshot.id
            or manifest.get("source_id") != snapshot.source_id
            or manifest.get("source_snapshot_sha256") != snapshot.content_sha256
            or not isinstance(manifest.get("fragments"), list)
        ):
            raise ReviewPersistenceError("stored review source evidence is invalid")
        fragments: list[ReviewSourceFragment] = []
        seen: set[str] = set()
        for raw in manifest["fragments"]:
            if not isinstance(raw, dict):
                raise ReviewPersistenceError("stored review source evidence is invalid")
            fragment_id = raw.get("fragment_id")
            source_id = raw.get("source_id")
            text = raw.get("text")
            content_sha256 = raw.get("content_sha256")
            if (
                set(raw)
                != {
                    "fragment_id",
                    "source_id",
                    "text",
                    "content_sha256",
                    "permission",
                    "document_id",
                    "page",
                    "section",
                }
                or not isinstance(fragment_id, str)
                or not fragment_id
                or fragment_id in seen
                or source_id != snapshot.source_id
                or not isinstance(text, str)
                or not text
                or text != text.strip()
                or not isinstance(content_sha256, str)
                or len(content_sha256) != 64
                or any(character not in _LOWER_HEX for character in content_sha256)
                or not hmac.compare_digest(
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    content_sha256,
                )
                or raw.get("permission") != "source.use"
            ):
                raise ReviewPersistenceError("stored review source evidence is invalid")
            seen.add(fragment_id)
            fragments.append(
                ReviewSourceFragment(
                    fragment_id=fragment_id,
                    source_id=source_id,
                    text=text,
                    content_sha256=content_sha256,
                )
            )
        contract_fragments = tuple(
            (item.fragment_id, item.source_id, item.text, item.content_sha256)
            for item in contract.source_fragments
        )
        persisted_fragments = tuple(
            (item.fragment_id, item.source_id, item.text, item.content_sha256)
            for item in fragments
        )
        if persisted_fragments != contract_fragments:
            raise ReviewPersistenceError("stored review source evidence is invalid")
        raw_refs = manifest.get("source_refs")
        if not isinstance(raw_refs, list):
            raise ReviewPersistenceError("stored review source evidence is invalid")
        persisted_refs: list[tuple[str, str, str]] = []
        for raw in raw_refs:
            if (
                not isinstance(raw, dict)
                or set(raw)
                != {
                    "citation_id",
                    "source_id",
                    "fragment_id",
                    "document_id",
                    "page",
                    "section",
                }
                or not isinstance(raw.get("citation_id"), str)
                or raw.get("source_id") != snapshot.source_id
                or raw.get("fragment_id") not in seen
            ):
                raise ReviewPersistenceError("stored review source evidence is invalid")
            persisted_refs.append(
                (
                    raw["citation_id"],
                    raw["source_id"],
                    raw["fragment_id"],
                )
            )
        contract_refs = [
            (item.citation_id, item.source_id, item.fragment_id)
            for item in contract.source_refs
        ]
        permission = manifest.get("permission_summary")
        expected_scope_ids = {
            "tenant": brief.tenant_id,
            "course": brief.course_id,
            "class": brief.class_id,
        }
        if (
            persisted_refs != contract_refs
            or not isinstance(permission, dict)
            or set(permission) != {"permissions", "scope_type", "scope_id"}
            or permission.get("permissions") != ["source.use"]
            or permission.get("scope_id")
            != expected_scope_ids.get(permission.get("scope_type"))
            or set(contract.permission_summary.allowed_fragment_ids) != seen
            or set(contract.permission_summary.allowed_source_ids) != {snapshot.source_id}
        ):
            raise ReviewPersistenceError("stored review source evidence is invalid")
        return tuple(fragments)

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
                teacher_asset_visible(ClassroomAsset.id, ClassroomAsset.tenant_id),
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

    async def get_detail(self, review_id: str) -> ReviewDetailEvidence | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ClassroomReviewRequest,
                        ClassroomAsset,
                        ClassroomDraft,
                        TeachingBrief,
                        SourceSnapshot,
                    )
                    .join(
                        ClassroomAsset,
                        and_(
                            ClassroomAsset.id == ClassroomReviewRequest.classroom_id,
                            ClassroomAsset.tenant_id == ClassroomReviewRequest.tenant_id,
                        ),
                    )
                    .join(
                        ClassroomDraft,
                        and_(
                            ClassroomDraft.id
                            == ClassroomReviewRequest.classroom_draft_id,
                            ClassroomDraft.classroom_id
                            == ClassroomReviewRequest.classroom_id,
                            ClassroomDraft.tenant_id
                            == ClassroomReviewRequest.tenant_id,
                        ),
                    )
                    .join(
                        TeachingBrief,
                        and_(
                            TeachingBrief.id == ClassroomDraft.teaching_brief_id,
                            TeachingBrief.tenant_id == ClassroomDraft.tenant_id,
                        ),
                    )
                    .outerjoin(
                        SourceSnapshot,
                        and_(
                            SourceSnapshot.id == TeachingBrief.source_snapshot_id,
                            SourceSnapshot.tenant_id == TeachingBrief.tenant_id,
                        ),
                    )
                    .where(
                        ClassroomReviewRequest.id == review_id,
                        ClassroomReviewRequest.tenant_id == self._tenant_id,
                        teacher_asset_visible(
                            ClassroomAsset.id,
                            ClassroomAsset.tenant_id,
                        ),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            review, asset, draft, brief, snapshot = row
            if (
                any(
                    item.tenant_id != self._tenant_id
                    for item in (review, asset, draft, brief)
                )
                or asset.title is None
                or not asset.title.strip()
                or brief.course_id is None
                or brief.class_id is None
            ):
                raise ReviewPersistenceError("stored classroom review binding is invalid")
            document = self._reviewed_document(asset, draft, review)
            validation_sha256, _ = self._validated_report(draft)
            if not hmac.compare_digest(
                validation_sha256,
                review.validation_report_sha256,
            ):
                raise ReviewValidationStale("classroom validation is stale")
            validation_report = _decode_object(
                draft.validation_report or "",
                field="validation report",
            )
            brief_contract = self._brief_contract(brief, asset)
            source_fragments = self._source_fragments(snapshot, brief, brief_contract)
            baseline_row = (
                await session.execute(
                    select(Publication, ClassroomVersion)
                    .join(
                        ClassroomVersion,
                        and_(
                            ClassroomVersion.id == Publication.classroom_version_id,
                            ClassroomVersion.classroom_id == Publication.classroom_id,
                            ClassroomVersion.tenant_id == Publication.tenant_id,
                        ),
                    )
                    .where(
                        Publication.tenant_id == self._tenant_id,
                        Publication.classroom_id == asset.id,
                        Publication.scope == review.scope,
                        or_(
                            Publication.review_request_id.is_(None),
                            Publication.review_request_id != review.id,
                        ),
                        *(
                            (Publication.class_id == review.class_id,)
                            if review.scope == "class"
                            else (Publication.class_id.is_(None),)
                        ),
                    )
                    .order_by(
                        Publication.created_at.desc(),
                        ClassroomVersion.version_number.desc(),
                        Publication.id.desc(),
                    )
                    .limit(1)
                )
            ).one_or_none()
            baseline = None
            if baseline_row is not None:
                publication, version = baseline_row
                if (
                    publication.tenant_id != self._tenant_id
                    or version.tenant_id != self._tenant_id
                    or version.classroom_id != asset.id
                    or publication.classroom_id != asset.id
                ):
                    raise ReviewPersistenceError("stored review baseline is invalid")
                baseline = ReviewBaseline(
                    version_id=version.id,
                    version_number=version.version_number,
                    document_sha256=version.document_sha256,
                    document_object_key=version.document_object_key,
                )
            target = ReviewTarget(
                tenant_id=asset.tenant_id,
                asset_id=asset.id,
                owner_id=asset.owner_id,
                course_id=brief.course_id,
                class_id=brief.class_id,
            )
            return ReviewDetailEvidence(
                review=self._record(review),
                target=target,
                title=asset.title,
                course_id=brief.course_id,
                target_class_id=brief.class_id,
                document=document,
                validation_report=validation_report,
                source_fragments=source_fragments,
                baseline=baseline,
            )

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
                    row = (
                        await session.execute(
                            self._target_statement(command.asset_id).with_for_update()
                        )
                    ).one_or_none()
                    if row is None:
                        raise ReviewConflict("classroom review target is unavailable")
                    asset, draft, brief = row
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
                    policy = await session.scalar(
                        select(ClassroomReviewPolicy)
                        .where(ClassroomReviewPolicy.tenant_id == self._tenant_id)
                        .with_for_update()
                    )
                    if (
                        policy is None or policy.prohibit_self_review
                    ) and model.submitted_by == command.actor_id:
                        raise ReviewAccessDenied("classroom self-review is prohibited")
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
