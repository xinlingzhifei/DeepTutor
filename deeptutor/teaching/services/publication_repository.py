"""PostgreSQL publication, assignment, and explicit migration repository."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import json

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.artifacts import classroom_artifact_key
from deeptutor.teaching.contracts import canonical_json_bytes
from deeptutor.teaching.models.classrooms import (
    Assignment,
    AssignmentMigration,
    ClassLearningState,
    ClassroomAsset,
    ClassroomDraft,
    ClassroomDraftMedia,
    ClassroomPublicationMaterialization,
    ClassroomReviewPolicy,
    ClassroomReviewRequest,
    ClassroomVersion,
    Publication,
    TeachingBrief,
    transition,
)
from deeptutor.teaching.models.jobs import ArtifactPromotionState, ClassroomArtifact
from deeptutor.teaching.models.platform import AuditLog
from deeptutor.teaching.models.tenant import TeachingClass
from deeptutor.teaching.repositories.classroom_version_allocation import (
    allocate_classroom_version_number,
    raise_for_classroom_version_allocation_conflict,
)
from deeptutor.teaching.repositories.student_visibility import teacher_asset_visible
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.classrooms import (
    ClassroomMediaBinding,
    InvalidDraftDocument,
    validate_draft_document_references,
)
from deeptutor.teaching.services.publication_materializer import (
    publication_manifest,
    publication_manifest_document,
    publication_manifest_sha256,
)
from deeptutor.teaching.services.publications import (
    AssignCommand,
    AssignmentRecord,
    AssignmentTarget,
    ConfirmedPublicationMaterialization,
    MaterializedPublicationArtifact,
    MigrateAssignmentCommand,
    MigrationRecord,
    PublicationConflict,
    PublicationMaterializationPlan,
    PublicationMaterializer,
    PublicationMediaSource,
    PublicationPersistenceError,
    PublicationTarget,
    PublicationValidationStale,
    PublishCommand,
    PublishedVersionRecord,
    TenantPublicationCandidate,
    TenantPublicationItem,
    VersionTarget,
    publication_media_manifest_sha256,
    validated_publication_document,
)
from deeptutor.teaching.services.reviews import ReviewPolicy


def _digest(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:48]}"


def _decode_report(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        raise PublicationPersistenceError("stored classroom validation is invalid") from None
    if not isinstance(decoded, dict):
        raise PublicationPersistenceError("stored classroom validation is invalid")
    return decoded


_MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


class SqlAlchemyPublicationRepository:
    """Commit version, publication, pointer, and assignment changes atomically."""

    def __init__(self, engine: AsyncEngine, tenant_id: str) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(translated, expire_on_commit=False)

    @staticmethod
    def _policy(model: ClassroomReviewPolicy | None) -> ReviewPolicy:
        if model is None:
            return ReviewPolicy()
        return ReviewPolicy(
            teacher_self_publish=model.teacher_self_publish,
            org_content_requires_review=model.org_content_requires_review,
            platform_template_requires_review=model.platform_template_requires_review,
            prohibit_self_review=model.prohibit_self_review,
        )

    async def get_policy(self) -> ReviewPolicy:
        async with self._session_factory() as session:
            return self._policy(await session.get(ClassroomReviewPolicy, self._tenant_id))

    def _publication_target_statement(self, asset_id: str):
        return (
            select(
                ClassroomAsset,
                ClassroomDraft,
                TeachingBrief,
                ClassroomReviewRequest,
            )
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
            .join(
                ClassroomReviewRequest,
                and_(
                    ClassroomReviewRequest.classroom_id == ClassroomAsset.id,
                    ClassroomReviewRequest.classroom_draft_id == ClassroomDraft.id,
                    ClassroomReviewRequest.tenant_id == ClassroomAsset.tenant_id,
                ),
            )
            .where(
                ClassroomAsset.id == asset_id,
                ClassroomAsset.tenant_id == self._tenant_id,
                teacher_asset_visible(ClassroomAsset.id, ClassroomAsset.tenant_id),
            )
            .order_by(
                ClassroomReviewRequest.created_at.desc(),
                ClassroomReviewRequest.id.desc(),
            )
            .limit(1)
        )

    @staticmethod
    def _publication_target(row) -> PublicationTarget | None:
        asset, _, brief, review = row
        if brief.course_id is None or brief.class_id is None:
            return None
        return PublicationTarget(
            tenant_id=asset.tenant_id,
            asset_id=asset.id,
            owner_id=asset.owner_id,
            course_id=brief.course_id,
            class_id=brief.class_id,
            review_id=review.id,
            review_scope=review.scope,
            review_status=review.status,
            submitted_by=review.submitted_by,
            draft_revision=review.draft_revision,
            document_sha256=review.document_sha256,
        )

    async def get_publication_target(
        self,
        asset_id: str,
    ) -> PublicationTarget | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(self._publication_target_statement(asset_id))
            ).one_or_none()
            return self._publication_target(row) if row is not None else None

    async def list_tenant_library(
        self,
    ) -> tuple[
        tuple[TenantPublicationItem, ...],
        tuple[TenantPublicationCandidate, ...],
    ]:
        async with self._session_factory() as session:
            item_rows = (
                await session.execute(
                    select(
                        Publication,
                        ClassroomVersion,
                        ClassroomAsset,
                        ClassroomReviewRequest,
                        ClassroomDraft,
                        TeachingBrief,
                    )
                    .join(
                        ClassroomVersion,
                        and_(
                            ClassroomVersion.id == Publication.classroom_version_id,
                            ClassroomVersion.classroom_id == Publication.classroom_id,
                            ClassroomVersion.tenant_id == Publication.tenant_id,
                        ),
                    )
                    .join(
                        ClassroomAsset,
                        and_(
                            ClassroomAsset.id == Publication.classroom_id,
                            ClassroomAsset.tenant_id == Publication.tenant_id,
                        ),
                    )
                    .join(
                        ClassroomReviewRequest,
                        and_(
                            ClassroomReviewRequest.id == Publication.review_request_id,
                            ClassroomReviewRequest.classroom_id == ClassroomAsset.id,
                            ClassroomReviewRequest.tenant_id == Publication.tenant_id,
                        ),
                    )
                    .join(
                        ClassroomDraft,
                        and_(
                            ClassroomDraft.id
                            == ClassroomReviewRequest.classroom_draft_id,
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
                        Publication.tenant_id == self._tenant_id,
                        Publication.scope == "tenant",
                        ClassroomReviewRequest.scope == "tenant",
                        ClassroomReviewRequest.status == "approved",
                        teacher_asset_visible(
                            ClassroomAsset.id,
                            ClassroomAsset.tenant_id,
                        ),
                    )
                    .order_by(Publication.created_at.desc(), Publication.id.desc())
                )
            ).all()
            items: list[TenantPublicationItem] = []
            for publication, version, asset, review, draft, brief in item_rows:
                if (
                    any(
                        model.tenant_id != self._tenant_id
                        for model in (publication, version, asset, review, draft, brief)
                    )
                    or publication.scope != "tenant"
                    or publication.classroom_id != asset.id
                    or version.classroom_id != asset.id
                    or publication.review_request_id != review.id
                    or review.scope != "tenant"
                    or review.status != "approved"
                    or review.classroom_draft_id != draft.id
                    or draft.classroom_id != asset.id
                    or draft.teaching_brief_id != brief.id
                    or asset.title is None
                    or brief.course_id is None
                ):
                    raise PublicationPersistenceError(
                        "stored tenant publication is invalid"
                    )
                items.append(
                    TenantPublicationItem(
                        tenant_id=self._tenant_id,
                        publication_id=publication.id,
                        version_id=version.id,
                        asset_id=asset.id,
                        version_number=version.version_number,
                        title=asset.title,
                        course_id=brief.course_id,
                        document_sha256=version.document_sha256,
                        published_by=publication.actor_id,
                        created_at=publication.created_at,
                        scope="tenant",
                    )
                )
            published_review = (
                select(Publication.id)
                .where(
                    Publication.tenant_id == self._tenant_id,
                    Publication.scope == "tenant",
                    Publication.review_request_id == ClassroomReviewRequest.id,
                )
                .correlate(ClassroomReviewRequest)
                .exists()
            )
            candidate_rows = (
                await session.execute(
                    select(
                        ClassroomReviewRequest,
                        ClassroomAsset,
                        ClassroomDraft,
                        TeachingBrief,
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
                    .where(
                        ClassroomReviewRequest.tenant_id == self._tenant_id,
                        ClassroomReviewRequest.status == "approved",
                        ClassroomReviewRequest.scope == "tenant",
                        ClassroomAsset.lifecycle_state == "approved",
                        teacher_asset_visible(
                            ClassroomAsset.id,
                            ClassroomAsset.tenant_id,
                        ),
                        ~published_review,
                    )
                    .order_by(
                        ClassroomReviewRequest.created_at,
                        ClassroomReviewRequest.id,
                    )
                )
            ).all()
            candidates: list[TenantPublicationCandidate] = []
            for review, asset, draft, brief in candidate_rows:
                if (
                    any(
                        model.tenant_id != self._tenant_id
                        for model in (review, asset, draft, brief)
                    )
                    or review.scope != "tenant"
                    or review.status != "approved"
                    or asset.lifecycle_state != "approved"
                    or review.classroom_id != asset.id
                    or review.classroom_draft_id != draft.id
                    or draft.classroom_id != asset.id
                    or draft.revision != review.draft_revision
                    or not hmac.compare_digest(
                        draft.document_sha256,
                        review.document_sha256,
                    )
                    or asset.title is None
                    or brief.course_id is None
                    or brief.class_id is None
                ):
                    raise PublicationPersistenceError(
                        "stored tenant publication candidate is invalid"
                    )
                try:
                    self._validate_review_binding(asset, draft, review)
                except PublicationValidationStale:
                    raise PublicationPersistenceError(
                        "stored tenant publication candidate is invalid"
                    ) from None
                candidates.append(
                    TenantPublicationCandidate(
                        tenant_id=self._tenant_id,
                        review_id=review.id,
                        asset_id=asset.id,
                        title=asset.title,
                        course_id=brief.course_id,
                        target_class_id=brief.class_id,
                        draft_revision=review.draft_revision,
                        document_sha256=review.document_sha256,
                        submitted_by=review.submitted_by,
                        review_scope="tenant",
                        review_status="approved",
                    )
                )
            return tuple(items), tuple(candidates)

    @staticmethod
    def _validate_review_binding(
        asset: ClassroomAsset,
        draft: ClassroomDraft,
        review: ClassroomReviewRequest,
    ) -> None:
        if (
            draft.revision != review.draft_revision
            or not hmac.compare_digest(draft.document_sha256, review.document_sha256)
            or draft.validation_report is None
            or draft.validation_report_sha256 is None
            or draft.validation_revision != draft.revision
            or draft.validation_document_sha256 is None
            or not hmac.compare_digest(
                draft.validation_document_sha256,
                draft.document_sha256,
            )
            or not hmac.compare_digest(
                draft.validation_report_sha256,
                review.validation_report_sha256,
            )
        ):
            raise PublicationValidationStale("classroom validation is stale")
        report = _decode_report(draft.validation_report)
        if (
            not hmac.compare_digest(_digest(report), draft.validation_report_sha256)
            or report.get("draftRevision") != draft.revision
            or report.get("documentSha256") != draft.document_sha256
            or report.get("valid") is not True
            or not isinstance(report.get("severeFindings"), list)
            or report.get("severeFindings")
        ):
            raise PublicationValidationStale("classroom validation is stale")
        if asset.id != review.classroom_id or draft.id != review.classroom_draft_id:
            raise PublicationValidationStale("classroom review binding is stale")

    @staticmethod
    def _published_record(
        version: ClassroomVersion,
        publication: Publication,
    ) -> PublishedVersionRecord:
        if publication.idempotency_key is None:
            raise PublicationPersistenceError("stored publication binding is invalid")
        return PublishedVersionRecord(
            version_id=version.id,
            asset_id=version.classroom_id,
            version_number=version.version_number,
            document_sha256=version.document_sha256,
            publication_scope=publication.scope,  # type: ignore[arg-type]
            class_id=publication.class_id,
            idempotency_key=publication.idempotency_key,
        )

    async def _get_publication_by_key(
        self,
        idempotency_key: str,
    ) -> tuple[ClassroomVersion, Publication] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ClassroomVersion, Publication)
                    .join(
                        Publication,
                        and_(
                            Publication.classroom_version_id == ClassroomVersion.id,
                            Publication.tenant_id == ClassroomVersion.tenant_id,
                        ),
                    )
                    .where(
                        Publication.tenant_id == self._tenant_id,
                        Publication.idempotency_key == idempotency_key,
                    )
                )
            ).one_or_none()
            return (row[0], row[1]) if row is not None else None

    @staticmethod
    def _verify_publication_retry(
        row: tuple[ClassroomVersion, Publication],
        *,
        request_sha256: str,
    ) -> PublishedVersionRecord:
        version, publication = row
        if publication.request_sha256 is None or not hmac.compare_digest(
            publication.request_sha256, request_sha256
        ):
            raise PublicationConflict("publication idempotency key conflicts")
        return SqlAlchemyPublicationRepository._published_record(version, publication)

    @staticmethod
    def _publication_request_sha256(command: PublishCommand) -> str:
        return _digest(
            {
                "tenantId": command.tenant_id,
                "assetId": command.asset_id,
                "actorId": command.actor_id,
                "scope": command.scope,
                "classId": command.class_id,
                "reviewId": command.review_id,
                "draftRevision": command.draft_revision,
                "documentSha256": command.document_sha256,
            }
        )

    async def _locked_publication_target(self, session, command: PublishCommand):
        return (
            await session.execute(
                select(
                    ClassroomAsset,
                    ClassroomDraft,
                    TeachingBrief,
                    ClassroomReviewRequest,
                )
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
                .join(
                    ClassroomReviewRequest,
                    and_(
                        ClassroomReviewRequest.id == command.review_id,
                        ClassroomReviewRequest.classroom_id == ClassroomAsset.id,
                        ClassroomReviewRequest.classroom_draft_id == ClassroomDraft.id,
                        ClassroomReviewRequest.tenant_id == ClassroomAsset.tenant_id,
                    ),
                )
                .where(
                    ClassroomAsset.id == command.asset_id,
                    ClassroomAsset.tenant_id == self._tenant_id,
                )
                .with_for_update()
            )
        ).one_or_none()

    async def _validate_locked_publication_target(
        self,
        session,
        command: PublishCommand,
        row,
    ):
        if row is None:
            raise PublicationConflict("publication target is unavailable")
        asset, draft, brief, review = row
        if brief.class_id is None or brief.course_id is None:
            raise PublicationConflict("publication target is unavailable")
        if (
            review.scope != command.scope
            or review.class_id != command.class_id
            or review.draft_revision != command.draft_revision
            or not hmac.compare_digest(review.document_sha256, command.document_sha256)
        ):
            raise PublicationConflict("publication review binding conflicts")
        self._validate_review_binding(asset, draft, review)
        policy = self._policy(
            await session.scalar(
                select(ClassroomReviewPolicy)
                .where(ClassroomReviewPolicy.tenant_id == self._tenant_id)
                .with_for_update()
            )
        )
        self_publish = (
            command.allow_self_publish
            and command.scope == "class"
            and policy.teacher_self_publish
            and asset.owner_id == command.actor_id
            and review.submitted_by == command.actor_id
            and review.class_id == brief.class_id
            and review.status == "pending"
        )
        if review.status != "approved" and not self_publish:
            raise PublicationConflict("publication approval is unavailable")
        if review.status == "approved":
            if asset.lifecycle_state not in {"approved", "published"}:
                raise PublicationConflict("publication lifecycle conflicts")
        elif asset.lifecycle_state != "submitted":
            raise PublicationConflict("publication lifecycle conflicts")
        return asset, draft, brief, review

    @staticmethod
    def _media_receipts_document(media: tuple[PublicationMediaSource, ...]) -> str:
        return canonical_json_bytes(
            [
                {
                    "mediaId": item.media_id,
                    "relativeName": item.relative_name,
                    "mimeType": item.mime_type,
                    "sha256": item.sha256,
                    "sizeBytes": item.size_bytes,
                    "sourceKind": item.source_kind,
                    "objectKey": item.object_key,
                    "ownershipToken": item.ownership_token,
                    "objectRevision": item.object_revision,
                }
                for item in media
            ]
        ).decode()

    @staticmethod
    def _decode_media_receipts(value: str) -> tuple[PublicationMediaSource, ...]:
        try:
            rows = json.loads(value)
            media = tuple(
                PublicationMediaSource(
                    media_id=row["mediaId"],
                    relative_name=row["relativeName"],
                    mime_type=row["mimeType"],
                    sha256=row["sha256"],
                    size_bytes=row["sizeBytes"],
                    source_kind=row["sourceKind"],
                    object_key=row["objectKey"],
                    ownership_token=row.get("ownershipToken"),
                    object_revision=row.get("objectRevision"),
                )
                for row in rows
            )
        except (KeyError, TypeError, ValueError):
            raise PublicationPersistenceError(
                "stored publication media receipts are invalid"
            ) from None
        if SqlAlchemyPublicationRepository._media_receipts_document(media) != value:
            raise PublicationPersistenceError("stored publication media receipts are invalid")
        return media

    @staticmethod
    def _plan(
        model: ClassroomPublicationMaterialization,
        draft: ClassroomDraft,
    ) -> PublicationMaterializationPlan:
        document = draft.document.encode()
        parsed = validated_publication_document(document)
        if (
            model.classroom_draft_id != draft.id
            or model.draft_revision != draft.revision
            or not hmac.compare_digest(model.document_sha256, draft.document_sha256)
            or hashlib.sha256(document).hexdigest() != model.document_sha256
        ):
            raise PublicationValidationStale("classroom validation is stale")
        media = SqlAlchemyPublicationRepository._decode_media_receipts(model.source_media_receipts)
        expected_media = tuple(
            (
                item.media_id,
                item.relative_path,
                item.mime_type,
                item.sha256,
                item.size_bytes,
            )
            for item in parsed.media_manifest
        )
        actual_media = tuple(
            (
                item.media_id,
                item.relative_name,
                item.mime_type,
                item.sha256,
                item.size_bytes,
            )
            for item in media
        )
        if (
            actual_media != expected_media
            or publication_media_manifest_sha256(document) != model.media_manifest_sha256
        ):
            raise PublicationPersistenceError("stored media manifest is invalid")
        plan = PublicationMaterializationPlan(
            reservation_id=model.id,
            tenant_id=model.tenant_id,
            asset_id=model.classroom_id,
            review_id=model.review_request_id,
            draft_id=model.classroom_draft_id,
            draft_revision=model.draft_revision,
            source_version_id=model.source_version_id,
            version_id=model.version_id,
            version_number=model.version_number,
            document=document,
            document_sha256=model.document_sha256,
            validation_report_sha256=model.validation_report_sha256,
            media_manifest_sha256=model.media_manifest_sha256,
            manifest_sha256=model.manifest_sha256,
            media=media,
            status=model.status,  # type: ignore[arg-type]
        )
        manifest = publication_manifest(plan)
        if (
            publication_manifest_document(manifest).decode() != model.manifest_document
            or publication_manifest_sha256(manifest) != model.manifest_sha256
        ):
            raise PublicationPersistenceError("stored publication manifest is invalid")
        return plan

    async def _source_version(self, session, draft: ClassroomDraft, asset_id: str):
        materialized = (
            await session.execute(
                select(ClassroomVersion, ArtifactPromotionState, ClassroomArtifact)
                .join(
                    ArtifactPromotionState,
                    and_(
                        ArtifactPromotionState.job_id == ClassroomVersion.generation_job_id,
                        ArtifactPromotionState.tenant_id == ClassroomVersion.tenant_id,
                        ArtifactPromotionState.classroom_id == ClassroomVersion.classroom_id,
                    ),
                )
                .join(
                    ClassroomArtifact,
                    and_(
                        ClassroomArtifact.classroom_version_id == ClassroomVersion.id,
                        ClassroomArtifact.source_job_id == ClassroomVersion.generation_job_id,
                        ClassroomArtifact.tenant_id == ClassroomVersion.tenant_id,
                    ),
                )
                .where(
                    ClassroomVersion.tenant_id == self._tenant_id,
                    ClassroomVersion.classroom_id == asset_id,
                    ClassroomVersion.id == draft.base_version_id,
                    ClassroomVersion.generation_job_id == draft.generation_job_id,
                    ClassroomArtifact.artifact_kind == "dsl_json",
                    ClassroomArtifact.mime_type == "application/json",
                )
                .with_for_update()
            )
        ).one_or_none()
        if materialized is None:
            raise PublicationConflict("materialized classroom is unavailable")
        source, promotion, artifact = materialized
        if (
            source.generation_job_id is None
            or promotion.status != "finalized"
            or promotion.manifest_sha256 is None
            or not hmac.compare_digest(artifact.sha256, source.document_sha256)
            or artifact.object_key != source.document_object_key
            or artifact.size_bytes <= 0
        ):
            raise PublicationConflict("materialized classroom binding is invalid")
        return source

    async def _media_sources(
        self,
        session,
        draft: ClassroomDraft,
        source: ClassroomVersion,
    ) -> tuple[PublicationMediaSource, ...]:
        try:
            document = validated_publication_document(draft.document.encode())
        except PublicationPersistenceError:
            raise PublicationValidationStale("classroom validation is stale") from None
        if (
            document.classroom_id != draft.classroom_id
            or document.classroom_version_id != source.id
        ):
            raise PublicationValidationStale("classroom validation is stale")
        if not document.media_manifest:
            try:
                validate_draft_document_references(
                    document.model_dump(mode="json", by_alias=True, exclude_none=True),
                    available_media_bindings=(),
                )
            except InvalidDraftDocument:
                raise PublicationValidationStale("classroom validation is stale") from None
            return ()
        media_ids = tuple(item.media_id for item in document.media_manifest)
        uploads = (
            await session.scalars(
                select(ClassroomDraftMedia)
                .where(
                    ClassroomDraftMedia.tenant_id == self._tenant_id,
                    ClassroomDraftMedia.classroom_id == draft.classroom_id,
                    ClassroomDraftMedia.id.in_(media_ids),
                    ClassroomDraftMedia.status == "uploaded",
                )
                .order_by(ClassroomDraftMedia.id)
                .with_for_update()
            )
        ).all()
        artifacts = (
            await session.scalars(
                select(ClassroomArtifact).where(
                    ClassroomArtifact.tenant_id == self._tenant_id,
                    ClassroomArtifact.classroom_version_id == source.id,
                    ClassroomArtifact.source_job_id == source.generation_job_id,
                    ClassroomArtifact.artifact_kind == "media",
                )
            )
        ).all()
        uploads_by_id = {item.id: item for item in uploads}
        artifacts_by_name = {item.relative_name: item for item in artifacts}
        media: list[PublicationMediaSource] = []
        for item in document.media_manifest:
            matches: list[PublicationMediaSource] = []
            artifact = artifacts_by_name.get(item.relative_path)
            if artifact is not None and (
                artifact.object_key
                == classroom_artifact_key(
                    self._tenant_id,
                    draft.classroom_id,
                    source.version_number,
                    item.relative_path,
                )
                and (
                    artifact.mime_type,
                    artifact.sha256,
                    artifact.size_bytes,
                )
                == (item.mime_type, item.sha256, item.size_bytes)
            ):
                matches.append(
                    PublicationMediaSource(
                        media_id=item.media_id,
                        relative_name=item.relative_path,
                        mime_type=item.mime_type,
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                        source_kind="version_artifact",
                        object_key=artifact.object_key,
                    )
                )
            upload = uploads_by_id.get(item.media_id)
            suffix = _MEDIA_SUFFIXES.get(upload.mime_type) if upload is not None else None
            if (
                upload is not None
                and suffix is not None
                and upload.object_revision is not None
                and (
                    f"media/{upload.id}{suffix}",
                    upload.mime_type,
                    upload.sha256,
                    upload.size_bytes,
                )
                == (
                    item.relative_path,
                    item.mime_type,
                    item.sha256,
                    item.size_bytes,
                )
            ):
                matches.append(
                    PublicationMediaSource(
                        media_id=item.media_id,
                        relative_name=item.relative_path,
                        mime_type=item.mime_type,
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                        source_kind="draft_upload",
                        object_key=upload.object_key,
                        ownership_token=upload.ownership_token,
                        object_revision=upload.object_revision,
                    )
                )
            if len(matches) != 1:
                raise PublicationValidationStale("classroom draft media is stale")
            media.append(matches[0])
        try:
            validate_draft_document_references(
                document.model_dump(mode="json", by_alias=True, exclude_none=True),
                available_media_bindings=tuple(
                    ClassroomMediaBinding(
                        media_id=item.media_id,
                        relative_name=item.relative_name,
                        mime_type=item.mime_type,
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                    )
                    for item in media
                ),
            )
        except InvalidDraftDocument:
            raise PublicationValidationStale("classroom validation is stale") from None
        return tuple(media)

    async def _prepare_publication(
        self,
        command: PublishCommand,
        request_sha256: str,
    ) -> PublicationMaterializationPlan:
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._locked_publication_target(session, command)
                asset, draft, _, review = await self._validate_locked_publication_target(
                    session,
                    command,
                    row,
                )
                by_key = await session.scalar(
                    select(ClassroomPublicationMaterialization)
                    .where(
                        ClassroomPublicationMaterialization.tenant_id == self._tenant_id,
                        ClassroomPublicationMaterialization.idempotency_key
                        == command.idempotency_key,
                    )
                    .with_for_update()
                )
                by_review = await session.scalar(
                    select(ClassroomPublicationMaterialization)
                    .where(
                        ClassroomPublicationMaterialization.tenant_id == self._tenant_id,
                        ClassroomPublicationMaterialization.review_request_id == review.id,
                    )
                    .with_for_update()
                )
                existing = by_key or by_review
                if existing is not None:
                    if (
                        (by_key is not None and by_review is not None and by_key.id != by_review.id)
                        or existing.idempotency_key != command.idempotency_key
                        or not hmac.compare_digest(
                            existing.request_sha256,
                            request_sha256,
                        )
                    ):
                        raise PublicationConflict("publication idempotency key conflicts")
                    return self._plan(existing, draft)

                source = await self._source_version(session, draft, asset.id)
                media = await self._media_sources(session, draft, source)
                version_number = await allocate_classroom_version_number(
                    session,
                    tenant_id=self._tenant_id,
                    classroom_id=asset.id,
                )
                reservation_id = (
                    "pm-"
                    + hashlib.sha256(f"{self._tenant_id}\0{review.id}".encode()).hexdigest()[:24]
                )
                version_id = _identifier("version", self._tenant_id, review.id)
                plan = PublicationMaterializationPlan(
                    reservation_id=reservation_id,
                    tenant_id=self._tenant_id,
                    asset_id=asset.id,
                    review_id=review.id,
                    draft_id=draft.id,
                    draft_revision=draft.revision,
                    source_version_id=source.id,
                    version_id=version_id,
                    version_number=version_number,
                    document=draft.document.encode(),
                    document_sha256=draft.document_sha256,
                    validation_report_sha256=review.validation_report_sha256,
                    media_manifest_sha256=publication_media_manifest_sha256(
                        draft.document.encode()
                    ),
                    manifest_sha256="",
                    media=media,
                    status="prepared",
                )
                manifest = publication_manifest(plan)
                plan = replace(
                    plan,
                    manifest_sha256=publication_manifest_sha256(manifest),
                )
                model = ClassroomPublicationMaterialization(
                    id=reservation_id,
                    tenant_id=self._tenant_id,
                    review_request_id=review.id,
                    classroom_id=asset.id,
                    classroom_draft_id=draft.id,
                    source_version_id=source.id,
                    version_id=version_id,
                    version_number=version_number,
                    draft_revision=draft.revision,
                    document_sha256=draft.document_sha256,
                    validation_report_sha256=review.validation_report_sha256,
                    media_manifest_sha256=plan.media_manifest_sha256,
                    manifest_sha256=plan.manifest_sha256,
                    manifest_document=publication_manifest_document(manifest).decode(),
                    source_media_receipts=self._media_receipts_document(media),
                    confirmed_artifacts=None,
                    status="prepared",
                    scope=command.scope,
                    class_id=command.class_id,
                    idempotency_key=command.idempotency_key,
                    request_sha256=request_sha256,
                    actor_id=command.actor_id,
                )
                session.add(model)
                await session.flush()
                return plan

    @staticmethod
    def _confirmed_document(
        confirmed: ConfirmedPublicationMaterialization,
    ) -> str:
        return canonical_json_bytes(
            [
                {
                    "relativeName": artifact.relative_name,
                    "objectKey": artifact.object_key,
                    "sha256": artifact.sha256,
                    "sizeBytes": artifact.size_bytes,
                    "mimeType": artifact.mime_type,
                    "artifactKind": artifact.artifact_kind,
                    "mediaId": artifact.media_id,
                }
                for artifact in confirmed.artifacts
            ]
        ).decode()

    @staticmethod
    def _validate_confirmed(
        plan: PublicationMaterializationPlan,
        confirmed: ConfirmedPublicationMaterialization,
    ) -> None:
        manifest = publication_manifest(plan)
        if (
            confirmed.manifest_sha256 != plan.manifest_sha256
            or confirmed.media_manifest_sha256 != plan.media_manifest_sha256
            or len(confirmed.artifacts) != len(manifest.entries)
        ):
            raise PublicationPersistenceError("confirmed publication is invalid")
        media_by_name = {item.relative_name: item for item in plan.media}
        for entry, artifact in zip(manifest.entries, confirmed.artifacts, strict=True):
            media = media_by_name.get(entry.relative_name)
            if (
                artifact.relative_name != entry.relative_name
                or artifact.object_key
                != classroom_artifact_key(
                    plan.tenant_id,
                    plan.asset_id,
                    plan.version_number,
                    entry.relative_name,
                )
                or artifact.sha256 != entry.sha256
                or artifact.size_bytes != entry.size
                or artifact.mime_type != entry.content_type
                or artifact.artifact_kind != ("dsl_json" if media is None else "media")
                or artifact.media_id != (media.media_id if media is not None else None)
            ):
                raise PublicationPersistenceError("confirmed publication is invalid")

    @staticmethod
    def _decode_confirmed(value: str) -> ConfirmedPublicationMaterialization:
        try:
            rows = json.loads(value)
            artifacts = tuple(
                MaterializedPublicationArtifact(
                    relative_name=row["relativeName"],
                    object_key=row["objectKey"],
                    sha256=row["sha256"],
                    size_bytes=row["sizeBytes"],
                    mime_type=row["mimeType"],
                    artifact_kind=row["artifactKind"],
                    media_id=row["mediaId"],
                )
                for row in rows
            )
        except (KeyError, TypeError, ValueError):
            raise PublicationPersistenceError("stored confirmed publication is invalid") from None
        return ConfirmedPublicationMaterialization(
            manifest_sha256="",
            media_manifest_sha256="",
            artifacts=artifacts,
        )

    async def _mark_object_committed(
        self,
        plan: PublicationMaterializationPlan,
        confirmed: ConfirmedPublicationMaterialization,
    ) -> None:
        self._validate_confirmed(plan, confirmed)
        document = self._confirmed_document(confirmed)
        async with self._session_factory() as session:
            async with session.begin():
                model = await session.scalar(
                    select(ClassroomPublicationMaterialization)
                    .where(
                        ClassroomPublicationMaterialization.id == plan.reservation_id,
                        ClassroomPublicationMaterialization.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if model is None or model.manifest_sha256 != plan.manifest_sha256:
                    raise PublicationConflict("publication reservation conflicts")
                if model.status == "prepared":
                    model.confirmed_artifacts = document
                    model.status = "object_committed"
                    model.updated_at = func.now()
                elif model.confirmed_artifacts != document:
                    raise PublicationConflict("publication confirmation conflicts")
                await session.flush()

    async def _finalize_publication(
        self,
        command: PublishCommand,
        request_sha256: str,
        plan: PublicationMaterializationPlan,
        confirmed: ConfirmedPublicationMaterialization,
    ) -> PublishedVersionRecord:
        self._validate_confirmed(plan, confirmed)
        confirmed_document = self._confirmed_document(confirmed)
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._locked_publication_target(session, command)
                asset, draft, _, review = await self._validate_locked_publication_target(
                    session,
                    command,
                    row,
                )
                model = await session.scalar(
                    select(ClassroomPublicationMaterialization)
                    .where(
                        ClassroomPublicationMaterialization.id == plan.reservation_id,
                        ClassroomPublicationMaterialization.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if (
                    model is None
                    or model.review_request_id != review.id
                    or model.classroom_draft_id != draft.id
                    or model.idempotency_key != command.idempotency_key
                    or not hmac.compare_digest(model.request_sha256, request_sha256)
                    or model.status not in {"object_committed", "finalized"}
                    or model.confirmed_artifacts != confirmed_document
                ):
                    raise PublicationConflict("publication reservation conflicts")
                persisted_plan = self._plan(model, draft)
                if persisted_plan != replace(plan, status=model.status):
                    raise PublicationConflict("publication reservation conflicts")
                existing = (
                    await session.execute(
                        select(ClassroomVersion, Publication)
                        .join(
                            Publication,
                            and_(
                                Publication.classroom_version_id == ClassroomVersion.id,
                                Publication.tenant_id == ClassroomVersion.tenant_id,
                            ),
                        )
                        .where(
                            Publication.tenant_id == self._tenant_id,
                            Publication.review_request_id == review.id,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if existing is not None:
                    return self._verify_publication_retry(
                        (existing[0], existing[1]),
                        request_sha256=request_sha256,
                    )

                document = confirmed.document
                version = ClassroomVersion(
                    id=model.version_id,
                    tenant_id=self._tenant_id,
                    classroom_id=asset.id,
                    version_number=model.version_number,
                    generation_job_id=None,
                    source_version_id=model.source_version_id,
                    document_sha256=document.sha256,
                    media_manifest_sha256=confirmed.media_manifest_sha256,
                    document_object_key=document.object_key,
                )
                publication = Publication(
                    id=_identifier("publication", self._tenant_id, review.id),
                    tenant_id=self._tenant_id,
                    classroom_id=asset.id,
                    classroom_version_id=model.version_id,
                    actor_id=command.actor_id,
                    scope=command.scope,
                    class_id=command.class_id,
                    review_request_id=review.id,
                    idempotency_key=command.idempotency_key,
                    request_sha256=request_sha256,
                )
                session.add(version)
                await session.flush([version])
                session.add(publication)
                if asset.lifecycle_state == "approved":
                    asset.lifecycle_state = transition("approved", "published")
                elif asset.lifecycle_state == "submitted":
                    asset.lifecycle_state = transition("submitted", "approved")
                    asset.lifecycle_state = transition("approved", "published")
                asset.current_published_version_id = version.id
                asset.updated_at = func.now()
                model.status = "finalized"
                model.updated_at = func.now()
                session.add(
                    AuditLog(
                        tenant_id=self._tenant_id,
                        actor_id=command.actor_id,
                        action="teaching.classroom.published",
                        resource_type="classroom_version",
                        resource_id=version.id,
                    )
                )
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise_for_classroom_version_allocation_conflict(exc)
                    raise
                return self._published_record(version, publication)

    async def publish(
        self,
        command: PublishCommand,
        materializer: PublicationMaterializer,
    ) -> PublishedVersionRecord:
        if command.tenant_id != self._tenant_id:
            raise PublicationConflict("publication tenant conflicts")
        request_sha256 = self._publication_request_sha256(command)
        existing = await self._get_publication_by_key(command.idempotency_key)
        if existing is not None:
            return self._verify_publication_retry(
                existing,
                request_sha256=request_sha256,
            )
        try:
            plan = await self._prepare_publication(command, request_sha256)
        except IntegrityError:
            plan = await self._prepare_publication(command, request_sha256)
        confirmed = await materializer.materialize(plan)
        await self._mark_object_committed(plan, confirmed)
        try:
            return await self._finalize_publication(
                command,
                request_sha256,
                plan,
                confirmed,
            )
        except IntegrityError as exc:
            existing = await self._get_publication_by_key(command.idempotency_key)
            if existing is not None:
                return self._verify_publication_retry(
                    existing,
                    request_sha256=request_sha256,
                )
            raise PublicationConflict("classroom publication conflicts") from exc

    def _version_target_statement(self, version_id: str):
        return (
            select(ClassroomVersion, Publication, TeachingBrief)
            .join(
                Publication,
                and_(
                    Publication.classroom_version_id == ClassroomVersion.id,
                    Publication.tenant_id == ClassroomVersion.tenant_id,
                ),
            )
            .join(
                ClassroomDraft,
                and_(
                    ClassroomDraft.classroom_id == ClassroomVersion.classroom_id,
                    ClassroomDraft.tenant_id == ClassroomVersion.tenant_id,
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
                ClassroomVersion.id == version_id,
                ClassroomVersion.tenant_id == self._tenant_id,
                teacher_asset_visible(
                    ClassroomVersion.classroom_id,
                    ClassroomVersion.tenant_id,
                ),
            )
            .order_by(ClassroomDraft.updated_at.desc(), ClassroomDraft.id)
            .limit(1)
        )

    async def get_version_target(self, version_id: str) -> VersionTarget | None:
        async with self._session_factory() as session:
            row = (await session.execute(self._version_target_statement(version_id))).one_or_none()
            if row is None or row[2].course_id is None:
                return None
            version, publication, brief = row
            return VersionTarget(
                tenant_id=version.tenant_id,
                version_id=version.id,
                asset_id=version.classroom_id,
                course_id=brief.course_id,
                publication_scope=publication.scope,  # type: ignore[arg-type]
                publication_class_id=publication.class_id,
            )

    @staticmethod
    def _assignment_record(
        assignment: Assignment,
        *,
        asset_id: str,
    ) -> AssignmentRecord:
        if assignment.idempotency_key is None:
            raise PublicationPersistenceError("stored assignment binding is invalid")
        return AssignmentRecord(
            assignment_id=assignment.id,
            tenant_id=assignment.tenant_id,
            asset_id=asset_id,
            version_id=assignment.classroom_version_id,
            class_id=assignment.class_id,
            assigned_by=assignment.assigned_by,
            idempotency_key=assignment.idempotency_key,
            revoked_at=assignment.revoked_at,
        )

    async def assign(self, command: AssignCommand) -> AssignmentRecord:
        if command.tenant_id != self._tenant_id:
            raise PublicationConflict("assignment tenant conflicts")
        request_sha256 = _digest(
            {
                "tenantId": command.tenant_id,
                "assetId": command.asset_id,
                "versionId": command.version_id,
                "classId": command.class_id,
                "actorId": command.actor_id,
            }
        )
        assignment_id = _identifier("assignment", self._tenant_id, command.idempotency_key)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    row = (
                        await session.execute(
                            self._version_target_statement(command.version_id).with_for_update()
                        )
                    ).one_or_none()
                    if row is None:
                        raise PublicationConflict("assignment target is unavailable")
                    version, publication, brief = row
                    teaching_class = await session.scalar(
                        select(TeachingClass)
                        .where(TeachingClass.id == command.class_id)
                        .with_for_update()
                    )
                    if teaching_class is None:
                        raise PublicationConflict("assignment target is unavailable")
                    existing = await session.scalar(
                        select(Assignment)
                        .where(
                            Assignment.tenant_id == self._tenant_id,
                            Assignment.idempotency_key == command.idempotency_key,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if existing.request_sha256 is None or not hmac.compare_digest(
                            existing.request_sha256,
                            request_sha256,
                        ):
                            raise PublicationConflict("assignment idempotency key conflicts")
                        existing_version = await session.get(
                            ClassroomVersion,
                            existing.classroom_version_id,
                        )
                        if existing_version is None:
                            raise PublicationPersistenceError(
                                "stored assignment version is unavailable"
                            )
                        return self._assignment_record(
                            existing,
                            asset_id=existing_version.classroom_id,
                        )
                    if version.classroom_id != command.asset_id:
                        raise PublicationConflict("assignment version binding conflicts")
                    if brief.course_id != teaching_class.course_id:
                        raise PublicationConflict("assignment course binding conflicts")
                    if publication.scope == "private" or (
                        publication.scope == "class" and publication.class_id != command.class_id
                    ):
                        raise PublicationConflict("assignment publication scope conflicts")
                    active = await session.scalar(
                        select(Assignment)
                        .join(
                            ClassroomVersion,
                            ClassroomVersion.id == Assignment.classroom_version_id,
                        )
                        .where(
                            Assignment.tenant_id == self._tenant_id,
                            Assignment.class_id == teaching_class.id,
                            Assignment.revoked_at.is_(None),
                            ClassroomVersion.classroom_id == command.asset_id,
                        )
                        .with_for_update()
                    )
                    if active is not None:
                        raise PublicationConflict("explicit assignment migration is required")
                    model = Assignment(
                        id=assignment_id,
                        tenant_id=self._tenant_id,
                        classroom_version_id=version.id,
                        class_id=teaching_class.id,
                        assigned_by=command.actor_id,
                        idempotency_key=command.idempotency_key,
                        request_sha256=request_sha256,
                        revoked_at=None,
                    )
                    session.add(model)
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=command.actor_id,
                            action="teaching.classroom.assigned",
                            resource_type="classroom_assignment",
                            resource_id=assignment_id,
                        )
                    )
                    await session.flush()
                    return self._assignment_record(model, asset_id=version.classroom_id)
        except IntegrityError as exc:
            async with self._session_factory() as session:
                existing = await session.scalar(
                    select(Assignment).where(
                        Assignment.tenant_id == self._tenant_id,
                        Assignment.idempotency_key == command.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_sha256 is None or not hmac.compare_digest(
                        existing.request_sha256,
                        request_sha256,
                    ):
                        raise PublicationConflict("assignment idempotency key conflicts") from exc
                    version = await session.get(
                        ClassroomVersion,
                        existing.classroom_version_id,
                    )
                    if version is None:
                        raise PublicationPersistenceError(
                            "stored assignment version is unavailable"
                        ) from exc
                    return self._assignment_record(
                        existing,
                        asset_id=version.classroom_id,
                    )
            raise PublicationConflict("classroom assignment conflicts") from exc

    async def get_assignment_target(
        self,
        assignment_id: str,
    ) -> AssignmentTarget | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Assignment, ClassroomVersion, TeachingClass)
                    .join(
                        ClassroomVersion,
                        ClassroomVersion.id == Assignment.classroom_version_id,
                    )
                    .join(TeachingClass, TeachingClass.id == Assignment.class_id)
                    .where(
                        Assignment.id == assignment_id,
                        Assignment.tenant_id == self._tenant_id,
                        teacher_asset_visible(
                            ClassroomVersion.classroom_id,
                            ClassroomVersion.tenant_id,
                        ),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            assignment, version, teaching_class = row
            return AssignmentTarget(
                tenant_id=assignment.tenant_id,
                assignment_id=assignment.id,
                asset_id=version.classroom_id,
                version_id=version.id,
                course_id=teaching_class.course_id,
                class_id=assignment.class_id,
                revoked_at=assignment.revoked_at,
            )

    @staticmethod
    def _migration_record(model: AssignmentMigration) -> MigrationRecord:
        return MigrationRecord(
            migration_id=model.id,
            tenant_id=model.tenant_id,
            old_assignment_id=model.old_assignment_id,
            old_version_id=model.old_version_id,
            new_version_id=model.new_version_id,
            new_assignment_id=model.new_assignment_id,
            class_id=model.class_id,
            actor_id=model.actor_id,
            reason=model.reason,
            outcome=model.outcome,  # type: ignore[arg-type]
            idempotency_key=model.idempotency_key,
        )

    async def get_migration(self, idempotency_key: str) -> MigrationRecord | None:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(AssignmentMigration).where(
                    AssignmentMigration.tenant_id == self._tenant_id,
                    AssignmentMigration.idempotency_key == idempotency_key,
                )
            )
            return self._migration_record(model) if model is not None else None

    async def set_learning_state(
        self,
        *,
        class_id: str,
        state: str,
        active_session_count: int,
        actor_id: str,
    ) -> None:
        if (
            state not in {"unknown", "idle", "active"}
            or not isinstance(active_session_count, int)
            or isinstance(active_session_count, bool)
            or active_session_count < 0
            or (state == "active") != (active_session_count > 0)
        ):
            raise ValueError("class learning state is invalid")
        async with self._session_factory() as session:
            async with session.begin():
                teaching_class = await session.get(TeachingClass, class_id)
                if teaching_class is None:
                    raise PublicationConflict("class learning state target is unavailable")
                model = await session.scalar(
                    select(ClassLearningState)
                    .where(ClassLearningState.class_id == class_id)
                    .with_for_update()
                )
                if model is None:
                    model = ClassLearningState(
                        class_id=class_id,
                        tenant_id=self._tenant_id,
                        updated_by=actor_id,
                    )
                    session.add(model)
                model.state = state
                model.active_session_count = active_session_count
                model.updated_by = actor_id
                model.updated_at = func.now()
                session.add(
                    AuditLog(
                        tenant_id=self._tenant_id,
                        actor_id=actor_id,
                        action="teaching.class_learning_state.updated",
                        resource_type="teaching_class",
                        resource_id=class_id,
                    )
                )
                await session.flush()

    async def migrate(self, command: MigrateAssignmentCommand) -> MigrationRecord:
        if command.tenant_id != self._tenant_id:
            raise PublicationConflict("migration tenant conflicts")
        request_sha256 = _digest(
            {
                "tenantId": command.tenant_id,
                "assignmentId": command.assignment_id,
                "oldVersionId": command.old_version_id,
                "newVersionId": command.new_version_id,
                "classId": command.class_id,
                "actorId": command.actor_id,
                "reason": command.reason,
            }
        )
        migration_id = _identifier("migration", self._tenant_id, command.idempotency_key)
        new_assignment_id = _identifier(
            "assignment",
            self._tenant_id,
            f"migration:{command.idempotency_key}",
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    old_row = (
                        await session.execute(
                            select(Assignment, ClassroomVersion)
                            .join(
                                ClassroomVersion,
                                ClassroomVersion.id == Assignment.classroom_version_id,
                            )
                            .where(
                                Assignment.id == command.assignment_id,
                                Assignment.tenant_id == self._tenant_id,
                            )
                            .with_for_update()
                        )
                    ).one_or_none()
                    if old_row is None:
                        raise PublicationConflict("migration assignment is unavailable")
                    old_assignment, old_version = old_row
                    existing = await session.scalar(
                        select(AssignmentMigration)
                        .where(
                            AssignmentMigration.tenant_id == self._tenant_id,
                            AssignmentMigration.idempotency_key == command.idempotency_key,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if not hmac.compare_digest(
                            existing.request_sha256,
                            request_sha256,
                        ):
                            raise PublicationConflict("migration idempotency key conflicts")
                        return self._migration_record(existing)
                    if (
                        old_assignment.revoked_at is not None
                        or old_assignment.class_id != command.class_id
                        or old_version.id != command.old_version_id
                    ):
                        raise PublicationConflict("migration assignment binding conflicts")
                    target_row = (
                        await session.execute(
                            select(ClassroomVersion, Publication)
                            .join(
                                Publication,
                                and_(
                                    Publication.classroom_version_id == ClassroomVersion.id,
                                    Publication.tenant_id == ClassroomVersion.tenant_id,
                                ),
                            )
                            .where(
                                ClassroomVersion.id == command.new_version_id,
                                ClassroomVersion.tenant_id == self._tenant_id,
                            )
                            .with_for_update()
                        )
                    ).one_or_none()
                    if target_row is None:
                        raise PublicationConflict("migration version is unavailable")
                    new_version, publication = target_row
                    if (
                        new_version.classroom_id != old_version.classroom_id
                        or publication.scope == "private"
                        or (
                            publication.scope == "class"
                            and publication.class_id != command.class_id
                        )
                    ):
                        raise PublicationConflict("migration version binding conflicts")
                    learning = await session.scalar(
                        select(ClassLearningState)
                        .where(
                            ClassLearningState.class_id == command.class_id,
                            ClassLearningState.tenant_id == self._tenant_id,
                        )
                        .with_for_update()
                    )
                    if learning is None or learning.state == "unknown":
                        outcome = "refused_guard_unavailable"
                    elif learning.state == "active" or learning.active_session_count > 0:
                        outcome = "refused_active_learning"
                    elif learning.state == "idle" and learning.active_session_count == 0:
                        outcome = "succeeded"
                    else:
                        raise PublicationPersistenceError("stored class learning state is invalid")
                    created_assignment_id: str | None = None
                    if outcome == "succeeded":
                        if new_version.id == old_version.id:
                            raise PublicationConflict("migration target is already assigned")
                        active_target = await session.scalar(
                            select(Assignment)
                            .where(
                                Assignment.tenant_id == self._tenant_id,
                                Assignment.class_id == command.class_id,
                                Assignment.classroom_version_id == new_version.id,
                                Assignment.revoked_at.is_(None),
                            )
                            .with_for_update()
                        )
                        if active_target is not None:
                            raise PublicationConflict("migration target is already assigned")
                        migrated_assignment = Assignment(
                            id=new_assignment_id,
                            tenant_id=self._tenant_id,
                            classroom_version_id=new_version.id,
                            class_id=command.class_id,
                            assigned_by=command.actor_id,
                            idempotency_key=f"migration:{command.idempotency_key}",
                            request_sha256=request_sha256,
                            revoked_at=None,
                        )
                        session.add(migrated_assignment)
                        await session.flush()
                        old_assignment.revoked_at = func.now()
                        created_assignment_id = new_assignment_id
                    migration = AssignmentMigration(
                        id=migration_id,
                        tenant_id=self._tenant_id,
                        old_assignment_id=old_assignment.id,
                        old_version_id=old_version.id,
                        new_version_id=new_version.id,
                        new_assignment_id=created_assignment_id,
                        class_id=command.class_id,
                        actor_id=command.actor_id,
                        reason=command.reason,
                        outcome=outcome,
                        idempotency_key=command.idempotency_key,
                        request_sha256=request_sha256,
                    )
                    session.add(migration)
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=command.actor_id,
                            action=f"teaching.assignment_migration.{outcome}",
                            resource_type="assignment_migration",
                            resource_id=migration_id,
                        )
                    )
                    await session.flush()
                    return self._migration_record(migration)
        except IntegrityError as exc:
            async with self._session_factory() as session:
                existing = await session.scalar(
                    select(AssignmentMigration).where(
                        AssignmentMigration.tenant_id == self._tenant_id,
                        AssignmentMigration.idempotency_key == command.idempotency_key,
                    )
                )
                if existing is not None:
                    if not hmac.compare_digest(
                        existing.request_sha256,
                        request_sha256,
                    ):
                        raise PublicationConflict("migration idempotency key conflicts") from exc
                    return self._migration_record(existing)
            raise PublicationConflict("classroom migration conflicts") from exc


__all__ = ["SqlAlchemyPublicationRepository"]
