"""PostgreSQL publication, assignment, and explicit migration repository."""

from __future__ import annotations

import hashlib
import hmac
import json

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.contracts import canonical_json_bytes
from deeptutor.teaching.models.classrooms import (
    Assignment,
    AssignmentMigration,
    ClassLearningState,
    ClassroomAsset,
    ClassroomDraft,
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
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.publications import (
    AssignCommand,
    AssignmentRecord,
    AssignmentTarget,
    MigrateAssignmentCommand,
    MigrationRecord,
    PublicationConflict,
    PublicationPersistenceError,
    PublicationTarget,
    PublicationValidationStale,
    PublishCommand,
    PublishedVersionRecord,
    VersionTarget,
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
            return self._policy(
                await session.get(ClassroomReviewPolicy, self._tenant_id)
            )

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
        if (
            publication.request_sha256 is None
            or not hmac.compare_digest(publication.request_sha256, request_sha256)
        ):
            raise PublicationConflict("publication idempotency key conflicts")
        return SqlAlchemyPublicationRepository._published_record(version, publication)

    async def publish(self, command: PublishCommand) -> PublishedVersionRecord:
        if command.tenant_id != self._tenant_id:
            raise PublicationConflict("publication tenant conflicts")
        request_sha256 = _digest(
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
        existing = await self._get_publication_by_key(command.idempotency_key)
        if existing is not None:
            return self._verify_publication_retry(existing, request_sha256=request_sha256)
        version_id = _identifier("version", self._tenant_id, command.idempotency_key)
        publication_id = _identifier(
            "publication",
            self._tenant_id,
            command.idempotency_key,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    row = (
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
                                    ClassroomReviewRequest.classroom_draft_id
                                    == ClassroomDraft.id,
                                    ClassroomReviewRequest.tenant_id
                                    == ClassroomAsset.tenant_id,
                                ),
                            )
                            .where(
                                ClassroomAsset.id == command.asset_id,
                                ClassroomAsset.tenant_id == self._tenant_id,
                            )
                            .with_for_update()
                        )
                    ).one_or_none()
                    if row is None:
                        raise PublicationConflict("publication target is unavailable")
                    asset, draft, brief, review = row
                    if brief.class_id is None or brief.course_id is None:
                        raise PublicationConflict("publication target is unavailable")
                    if (
                        review.scope != command.scope
                        or review.class_id != command.class_id
                        or review.draft_revision != command.draft_revision
                        or not hmac.compare_digest(
                            review.document_sha256,
                            command.document_sha256,
                        )
                    ):
                        raise PublicationConflict("publication review binding conflicts")
                    self._validate_review_binding(asset, draft, review)
                    policy = self._policy(
                        await session.scalar(
                            select(ClassroomReviewPolicy)
                            .where(
                                ClassroomReviewPolicy.tenant_id == self._tenant_id
                            )
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

                    materialized = (
                        await session.execute(
                            select(
                                ClassroomVersion,
                                ArtifactPromotionState,
                                ClassroomArtifact,
                            )
                            .join(
                                ArtifactPromotionState,
                                and_(
                                    ArtifactPromotionState.job_id
                                    == ClassroomVersion.generation_job_id,
                                    ArtifactPromotionState.tenant_id
                                    == ClassroomVersion.tenant_id,
                                    ArtifactPromotionState.classroom_id
                                    == ClassroomVersion.classroom_id,
                                ),
                            )
                            .join(
                                ClassroomArtifact,
                                and_(
                                    ClassroomArtifact.classroom_version_id
                                    == ClassroomVersion.id,
                                    ClassroomArtifact.source_job_id
                                    == ClassroomVersion.generation_job_id,
                                    ClassroomArtifact.tenant_id
                                    == ClassroomVersion.tenant_id,
                                ),
                            )
                            .where(
                                ClassroomVersion.tenant_id == self._tenant_id,
                                ClassroomVersion.classroom_id == asset.id,
                                ClassroomVersion.generation_job_id
                                == draft.generation_job_id,
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
                        or not hmac.compare_digest(
                            source.document_sha256,
                            draft.document_sha256,
                        )
                        or not hmac.compare_digest(
                            artifact.sha256,
                            source.document_sha256,
                        )
                        or artifact.object_key != source.document_object_key
                        or artifact.size_bytes <= 0
                    ):
                        raise PublicationConflict("materialized classroom binding is invalid")
                    version_number = await allocate_classroom_version_number(
                        session,
                        tenant_id=self._tenant_id,
                        classroom_id=asset.id,
                    )
                    version = ClassroomVersion(
                        id=version_id,
                        tenant_id=self._tenant_id,
                        classroom_id=asset.id,
                        version_number=version_number,
                        generation_job_id=None,
                        source_version_id=source.id,
                        document_sha256=source.document_sha256,
                        media_manifest_sha256=source.media_manifest_sha256,
                        document_object_key=source.document_object_key,
                    )
                    publication = Publication(
                        id=publication_id,
                        tenant_id=self._tenant_id,
                        classroom_id=asset.id,
                        classroom_version_id=version_id,
                        actor_id=command.actor_id,
                        scope=command.scope,
                        class_id=command.class_id,
                        review_request_id=review.id,
                        idempotency_key=command.idempotency_key,
                        request_sha256=request_sha256,
                    )
                    session.add(version)
                    # Persist the immutable target before advancing the asset's
                    # foreign-key pointer; the models intentionally have no ORM
                    # relationship that would otherwise establish this order.
                    await session.flush([version])
                    session.add(publication)
                    if asset.lifecycle_state == "approved":
                        asset.lifecycle_state = transition("approved", "published")
                    elif asset.lifecycle_state == "submitted":
                        asset.lifecycle_state = transition("submitted", "approved")
                        asset.lifecycle_state = transition("approved", "published")
                    asset.current_published_version_id = version_id
                    asset.updated_at = func.now()
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=command.actor_id,
                            action="teaching.classroom.published",
                            resource_type="classroom_version",
                            resource_id=version_id,
                        )
                    )
                    try:
                        await session.flush()
                    except IntegrityError as exc:
                        raise_for_classroom_version_allocation_conflict(exc)
                        raise
                    return self._published_record(version, publication)
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
                    existing = await session.scalar(
                        select(Assignment)
                        .where(
                            Assignment.tenant_id == self._tenant_id,
                            Assignment.idempotency_key == command.idempotency_key,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if (
                            existing.request_sha256 is None
                            or not hmac.compare_digest(
                                existing.request_sha256,
                                request_sha256,
                            )
                        ):
                            raise PublicationConflict("assignment idempotency key conflicts")
                        version = await session.get(
                            ClassroomVersion,
                            existing.classroom_version_id,
                        )
                        if version is None:
                            raise PublicationPersistenceError(
                                "stored assignment version is unavailable"
                            )
                        return self._assignment_record(existing, asset_id=version.classroom_id)
                    row = (
                        await session.execute(
                            self._version_target_statement(command.version_id)
                            .with_for_update()
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
                    if version.classroom_id != command.asset_id:
                        raise PublicationConflict("assignment version binding conflicts")
                    if brief.course_id != teaching_class.course_id:
                        raise PublicationConflict("assignment course binding conflicts")
                    if publication.scope == "private" or (
                        publication.scope == "class"
                        and publication.class_id != command.class_id
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
                    existing = await session.scalar(
                        select(AssignmentMigration)
                        .where(
                            AssignmentMigration.tenant_id == self._tenant_id,
                            AssignmentMigration.idempotency_key
                            == command.idempotency_key,
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
                                    Publication.classroom_version_id
                                    == ClassroomVersion.id,
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
                        raise PublicationPersistenceError(
                            "stored class learning state is invalid"
                        )
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
            existing = await self.get_migration(command.idempotency_key)
            if existing is not None:
                return existing
            raise PublicationConflict("classroom migration conflicts") from exc


__all__ = ["SqlAlchemyPublicationRepository"]
