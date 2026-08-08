"""Transactional tenant repository for immutable classroom publication."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from typing import Any

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.artifacts import classroom_artifact_key
from deeptutor.teaching.contracts import (
    ClassroomDocument,
    OutlineBundle,
    OutlineConfirmationMetadata,
    TeachingBrief,
    canonical_json_bytes,
    canonical_outline_sha256,
)
from deeptutor.teaching.models.classrooms import (
    ClassroomAsset,
    ClassroomDraft,
    ClassroomDraftMedia,
    ClassroomVersion,
    Publication,
    transition,
)
from deeptutor.teaching.models.classrooms import (
    TeachingBrief as TeachingBriefModel,
)
from deeptutor.teaching.models.jobs import ClassroomArtifact, GenerationJob
from deeptutor.teaching.models.platform import AuditLog
from deeptutor.teaching.repositories.classroom_version_allocation import (
    ClassroomVersionAllocationError,
    allocate_classroom_version_number,
    raise_for_classroom_version_allocation_conflict,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.classrooms import (
    BoundClassroomMedia,
    ClassroomConfirmationConflict,
    ClassroomIdempotencyConflict,
    ClassroomMediaBinding,
    ClassroomRecord,
    DraftMediaRecord,
    InvalidDraftMedia,
    NewClassroomWorkflow,
    NewDraftMedia,
    draft_media_relative_path,
    matches_reviewed_outline_binding,
)

_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")
_IMMUTABLE_TRIGGER_MESSAGE = "immutable classroom record"


def _required(value: str, name: str, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _sha256(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in _LOWER_HEX_DIGITS for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class ClassroomDocumentReference:
    """Immutable object-store identity for one classroom document."""

    sha256: str
    media_manifest_sha256: str
    object_key: str

    def __post_init__(self) -> None:
        _sha256(self.sha256, "sha256")
        _sha256(self.media_manifest_sha256, "media_manifest_sha256")
        _required(self.object_key, "object_key", 512)


@dataclass(frozen=True, slots=True)
class PublishedClassroomVersion:
    """Inputs committed together when an approved classroom is published."""

    id: str
    classroom_id: str
    source_version_id: str
    document: ClassroomDocumentReference
    publication_id: str
    actor_id: str
    scope: str
    class_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.id, "id", 128)
        _required(self.classroom_id, "classroom_id", 128)
        _required(self.source_version_id, "source_version_id", 128)
        _required(self.publication_id, "publication_id", 128)
        _required(self.actor_id, "actor_id", 128)
        if self.scope not in {"private", "class", "tenant"}:
            raise ValueError("scope is invalid")
        if self.scope == "class":
            if self.class_id is None:
                raise ValueError("class scope requires class_id")
            _required(self.class_id, "class_id", 64)
        elif self.class_id is not None:
            raise ValueError("class_id is only valid for class scope")


class ImmutableVersionError(RuntimeError):
    """A caller attempted to mutate an immutable classroom version."""


class ClassroomVersionNotFoundError(LookupError):
    """The requested version does not exist in the active tenant."""


class ClassroomAssetNotFoundError(LookupError):
    """The requested classroom asset does not exist in the active tenant."""


class ClassroomPersistenceError(RuntimeError):
    """Stored classroom authoring state is unavailable or inconsistent."""


def _is_immutable_trigger_error(exc: DBAPIError) -> bool:
    return _IMMUTABLE_TRIGGER_MESSAGE in str(exc.orig).lower()


class SqlAlchemyClassroomRepository:
    """Publish and protect classroom versions inside one tenant schema."""

    def __init__(self, engine: AsyncEngine, tenant_id: str) -> None:
        _required(tenant_id, "tenant_id", 64)
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(
            translated,
            expire_on_commit=False,
        )

    @staticmethod
    def _decode_object(value: str | None, *, field: str) -> dict[str, object] | None:
        if value is None:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            raise ClassroomPersistenceError(f"stored classroom {field} is invalid") from None
        if not isinstance(decoded, dict):
            raise ClassroomPersistenceError(f"stored classroom {field} is invalid")
        return decoded

    @staticmethod
    def _verified_draft_document(draft: ClassroomDraft) -> ClassroomDocument | None:
        try:
            payload = json.loads(draft.document)
            document = ClassroomDocument.model_validate(payload)
            canonical_payload = canonical_json_bytes(document)
            raw = document.model_dump(mode="json", by_alias=True, exclude_none=True)
            file_sha256 = raw.pop("fileSha256")
            if (
                draft.base_version_id is None
                or document.classroom_id != draft.classroom_id
                or document.classroom_version_id != draft.base_version_id
                or canonical_json_bytes(payload) != canonical_payload
                or not hmac.compare_digest(
                    hashlib.sha256(canonical_json_bytes(raw)).hexdigest(),
                    file_sha256,
                )
                or not hmac.compare_digest(
                    hashlib.sha256(canonical_payload).hexdigest(),
                    draft.document_sha256,
                )
            ):
                return None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return document

    def _verified_bound_version_media(
        self,
        asset_id: str,
        draft: ClassroomDraft,
        version: ClassroomVersion,
        artifacts: tuple[ClassroomArtifact, ...],
    ) -> tuple[BoundClassroomMedia, ...] | None:
        document = self._verified_draft_document(draft)
        if (
            document is None
            or draft.tenant_id != self._tenant_id
            or draft.classroom_id != asset_id
            or version.tenant_id != self._tenant_id
            or version.id != draft.base_version_id
            or version.classroom_id != asset_id
        ):
            return None
        manifest_ids = [item.media_id for item in document.media_manifest]
        manifest_paths = [item.relative_path for item in document.media_manifest]
        if (
            len(set(manifest_ids)) != len(manifest_ids)
            or len(set(manifest_paths)) != len(manifest_paths)
        ):
            return None
        artifacts_by_path: dict[str, list[ClassroomArtifact]] = {}
        for artifact in artifacts:
            if (
                artifact.tenant_id != self._tenant_id
                or artifact.classroom_version_id != version.id
                or artifact.artifact_kind != "media"
            ):
                return None
            artifacts_by_path.setdefault(artifact.relative_name, []).append(artifact)
        resolved: list[BoundClassroomMedia] = []
        for item in document.media_manifest:
            matches = artifacts_by_path.get(item.relative_path, [])
            if len(matches) != 1:
                return None
            artifact = matches[0]
            if (
                artifact.mime_type,
                artifact.sha256,
                artifact.size_bytes,
            ) != (item.mime_type, item.sha256, item.size_bytes):
                return None
            try:
                expected_object_key = classroom_artifact_key(
                    self._tenant_id,
                    asset_id,
                    version.version_number,
                    item.relative_path,
                )
            except ValueError:
                return None
            if not hmac.compare_digest(artifact.object_key, expected_object_key):
                return None
            resolved.append(
                BoundClassroomMedia(
                    id=item.media_id,
                    classroom_id=asset_id,
                    relative_path=item.relative_path,
                    mime_type=item.mime_type,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    object_key=artifact.object_key,
                )
            )
        return tuple(resolved)

    def _workflow_statement(self):
        return (
            select(
                ClassroomAsset,
                ClassroomDraft,
                TeachingBriefModel,
                GenerationJob,
                ClassroomVersion.id.label("classroom_version_id"),
            )
            .join(
                ClassroomDraft,
                and_(
                    ClassroomDraft.classroom_id == ClassroomAsset.id,
                    ClassroomDraft.tenant_id == ClassroomAsset.tenant_id,
                ),
            )
            .outerjoin(
                TeachingBriefModel,
                and_(
                    TeachingBriefModel.id == ClassroomDraft.teaching_brief_id,
                    TeachingBriefModel.tenant_id == ClassroomDraft.tenant_id,
                ),
            )
            .outerjoin(
                GenerationJob,
                and_(
                    GenerationJob.id == ClassroomDraft.generation_job_id,
                    GenerationJob.tenant_id == ClassroomDraft.tenant_id,
                ),
            )
            .outerjoin(
                ClassroomVersion,
                and_(
                    ClassroomVersion.generation_job_id == ClassroomDraft.generation_job_id,
                    ClassroomVersion.tenant_id == ClassroomDraft.tenant_id,
                    ClassroomVersion.classroom_id == ClassroomAsset.id,
                ),
            )
            .where(ClassroomAsset.tenant_id == self._tenant_id)
        )

    def _workflow_record(self, row) -> ClassroomRecord:
        asset: ClassroomAsset = row[0]
        draft: ClassroomDraft = row[1]
        brief_model: TeachingBriefModel | None = row[2]
        job: GenerationJob | None = row[3]
        version_id: str | None = row[4]
        brief: TeachingBrief | None = None
        if brief_model is not None:
            try:
                brief = TeachingBrief.model_validate_json(brief_model.document)
            except Exception:
                raise ClassroomPersistenceError("stored teaching brief is invalid") from None
            if (
                brief.brief_id != brief_model.id
                or brief.tenant_id != self._tenant_id
                or brief.content_sha256 != brief_model.document_sha256
            ):
                raise ClassroomPersistenceError("stored teaching brief is invalid")
        if brief is None:
            raise ClassroomPersistenceError("stored teaching brief is unavailable")
        document = self._decode_object(draft.document, field="document")
        if document is None:
            raise ClassroomPersistenceError("stored classroom document is invalid")
        outline = self._decode_object(draft.outline_document, field="outline")
        validation_report = self._decode_object(
            draft.validation_report,
            field="validation report",
        )
        if validation_report is not None and (
            draft.validation_revision != draft.revision
            or draft.validation_document_sha256 != draft.document_sha256
            or validation_report.get("draftRevision") != draft.validation_revision
            or validation_report.get("documentSha256") != draft.validation_document_sha256
        ):
            raise ClassroomPersistenceError("stored validation report binding is invalid")
        status = job.status if job is not None else asset.lifecycle_state
        if asset.lifecycle_state == "awaiting_outline":
            status = "awaiting_confirmation"
        return ClassroomRecord(
            tenant_id=asset.tenant_id,
            asset_id=asset.id,
            draft_id=draft.id,
            job_id=draft.generation_job_id,
            lifecycle_state=asset.lifecycle_state,
            status=status,
            title=asset.title or "Untitled classroom",
            course_id=brief.course_id,
            class_id=brief.target_class_id,
            owner_id=asset.owner_id,
            teaching_brief=brief,
            revision=draft.revision,
            outline=outline,
            document=document,
            classroom_version_id=version_id,
            confirmed_outline_sha256=draft.confirmed_outline_sha256,
            validation_report=validation_report,
            validation_revision=draft.validation_revision,
            validation_document_sha256=draft.validation_document_sha256,
            creation_idempotency_key=draft.creation_idempotency_key,
            creation_request_sha256=draft.creation_request_sha256,
        )

    async def get_creation(self, idempotency_key: str) -> ClassroomRecord | None:
        _required(idempotency_key, "idempotency_key", 128)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    self._workflow_statement().where(
                        ClassroomDraft.creation_idempotency_key == idempotency_key
                    )
                )
            ).one_or_none()
            return self._workflow_record(row) if row is not None else None

    @staticmethod
    def _verify_creation_binding(
        record: ClassroomRecord,
        workflow: NewClassroomWorkflow,
    ) -> ClassroomRecord:
        if (
            record.tenant_id != workflow.tenant_id
            or record.asset_id != workflow.asset_id
            or record.draft_id != workflow.draft_id
            or record.owner_id != workflow.owner_id
            or record.creation_idempotency_key != workflow.creation_idempotency_key
            or record.creation_request_sha256 is None
            or not hmac.compare_digest(
                record.creation_request_sha256,
                workflow.creation_request_sha256,
            )
        ):
            raise ClassroomIdempotencyConflict("classroom idempotency key conflicts")
        return record

    async def create_workflow(
        self,
        workflow: NewClassroomWorkflow,
    ) -> ClassroomRecord:
        if workflow.tenant_id != self._tenant_id:
            raise ValueError("workflow tenant does not match repository")
        _required(
            workflow.creation_idempotency_key,
            "creation_idempotency_key",
            128,
        )
        _sha256(workflow.creation_request_sha256, "creation_request_sha256")
        existing = await self.get_creation(workflow.creation_idempotency_key)
        if existing is not None:
            return self._verify_creation_binding(existing, workflow)
        brief = workflow.teaching_brief
        brief_document = json.dumps(
            brief.model_dump(mode="json", by_alias=True, exclude_none=False),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        empty_document = canonical_json_bytes({}).decode("utf-8")
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    existing_brief = await session.scalar(
                        select(TeachingBriefModel)
                        .where(
                            TeachingBriefModel.id == brief.brief_id,
                            TeachingBriefModel.tenant_id == self._tenant_id,
                        )
                        .with_for_update()
                    )
                    if existing_brief is None:
                        session.add(
                            TeachingBriefModel(
                                id=brief.brief_id,
                                tenant_id=self._tenant_id,
                                source_snapshot_id=(
                                    brief.source_snapshot.snapshot_id
                                    if brief.source_snapshot is not None
                                    else None
                                ),
                                course_id=brief.course_id,
                                class_id=brief.target_class_id,
                                brief_version=brief.brief_version,
                                document=brief_document,
                                document_sha256=brief.content_sha256,
                                created_by=workflow.owner_id,
                            )
                        )
                    elif (
                        existing_brief.document != brief_document
                        or existing_brief.document_sha256 != brief.content_sha256
                    ):
                        raise ClassroomPersistenceError("teaching brief identity conflicts")
                    await session.flush()
                    session.add(
                        ClassroomAsset(
                            id=workflow.asset_id,
                            tenant_id=self._tenant_id,
                            owner_id=workflow.owner_id,
                            title=workflow.title,
                            lifecycle_state="generating_outline",
                        )
                    )
                    session.add(
                        ClassroomDraft(
                            id=workflow.draft_id,
                            tenant_id=self._tenant_id,
                            classroom_id=workflow.asset_id,
                            generation_job_id=None,
                            teaching_brief_id=brief.brief_id,
                            base_version_id=None,
                            revision=1,
                            document=empty_document,
                            document_sha256=hashlib.sha256(empty_document.encode()).hexdigest(),
                            outline_document=None,
                            outline_sha256=None,
                            confirmed_outline_sha256=None,
                            validation_report=None,
                            validation_report_sha256=None,
                            validation_revision=None,
                            validation_document_sha256=None,
                            creation_idempotency_key=(workflow.creation_idempotency_key),
                            creation_request_sha256=(workflow.creation_request_sha256),
                            created_by=workflow.owner_id,
                            updated_by=workflow.owner_id,
                        )
                    )
                    session.add(
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=workflow.owner_id,
                            action="teaching.classroom.created",
                            resource_type="classroom_asset",
                            resource_id=workflow.asset_id,
                        )
                    )
                    await session.flush()
        except IntegrityError as exc:
            existing = await self.get_creation(workflow.creation_idempotency_key)
            if existing is not None:
                return self._verify_creation_binding(existing, workflow)
            raise ClassroomPersistenceError("classroom workflow conflicts") from exc
        record = await self.get_workflow(workflow.asset_id)
        if record is None:
            raise ClassroomPersistenceError("classroom workflow was not persisted")
        return record

    async def list_workflows(self) -> tuple[ClassroomRecord, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    self._workflow_statement().order_by(
                        ClassroomAsset.updated_at.desc(),
                        ClassroomAsset.id,
                    )
                )
            ).all()
            return tuple(self._workflow_record(row) for row in rows)

    async def get_workflow(self, asset_id: str) -> ClassroomRecord | None:
        _required(asset_id, "asset_id", 128)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    self._workflow_statement().where(ClassroomAsset.id == asset_id)
                )
            ).one_or_none()
            return self._workflow_record(row) if row is not None else None

    async def _lock_draft(self, session, asset_id: str):
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
                    ClassroomAsset.id == asset_id,
                    ClassroomAsset.tenant_id == self._tenant_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ClassroomAssetNotFoundError(asset_id)
        return row[0], row[1]

    async def attach_outline_job(self, asset_id: str, job_id: str) -> ClassroomRecord:
        _required(job_id, "job_id", 64)
        async with self._session_factory() as session:
            async with session.begin():
                _, draft = await self._lock_draft(session, asset_id)
                job = await session.scalar(
                    select(GenerationJob).where(
                        GenerationJob.id == job_id,
                        GenerationJob.tenant_id == self._tenant_id,
                        GenerationJob.classroom_draft_id == draft.id,
                        GenerationJob.phase == "outline",
                    )
                )
                if job is None:
                    raise ClassroomPersistenceError("outline job binding is unavailable")
                if draft.generation_job_id not in {None, job_id}:
                    raise ClassroomPersistenceError("classroom already has a generation job")
                draft.generation_job_id = job_id
                await session.flush()
        record = await self.get_workflow(asset_id)
        if record is None:
            raise ClassroomPersistenceError("classroom workflow is unavailable")
        return record

    async def save_outline(
        self,
        asset_id: str,
        outline: dict[str, Any],
        outline_sha256: str,
    ) -> ClassroomRecord:
        payload = canonical_json_bytes(outline).decode("utf-8")
        _sha256(outline_sha256, "outline_sha256")
        async with self._session_factory() as session:
            async with session.begin():
                asset, draft = await self._lock_draft(session, asset_id)
                if asset.lifecycle_state == "generating_outline":
                    asset.lifecycle_state = transition("generating_outline", "awaiting_outline")
                elif asset.lifecycle_state != "awaiting_outline":
                    raise ClassroomPersistenceError("outline state is invalid")
                if draft.outline_document != payload:
                    draft.outline_document = payload
                    draft.outline_sha256 = outline_sha256
                    draft.revision += 1
                asset.updated_at = func.now()
                draft.updated_at = func.now()
                await session.flush()
        record = await self.get_workflow(asset_id)
        if record is None:
            raise ClassroomPersistenceError("classroom workflow is unavailable")
        return record

    async def update_outline(
        self,
        asset_id: str,
        outline: dict[str, Any],
        outline_sha256: str,
        expected_revision: int,
    ) -> ClassroomRecord | None:
        payload = canonical_json_bytes(outline).decode("utf-8")
        _sha256(outline_sha256, "outline_sha256")
        async with self._session_factory() as session:
            async with session.begin():
                asset, draft = await self._lock_draft(session, asset_id)
                if asset.lifecycle_state != "awaiting_outline":
                    raise ClassroomPersistenceError("outline state is invalid")
                if draft.revision != expected_revision:
                    return None
                draft.outline_document = payload
                draft.outline_sha256 = outline_sha256
                draft.revision += 1
                draft.updated_at = func.now()
                asset.updated_at = func.now()
                await session.flush()
        return await self.get_workflow(asset_id)

    async def confirm_outline(
        self,
        asset_id: str,
        outline: dict[str, Any],
        confirmed_outline_sha256: str,
        source_outline_sha256: str,
        *,
        expected_revision: int | None = None,
        expected_outline_sha256: str | None = None,
    ) -> ClassroomRecord:
        payload = canonical_json_bytes(outline).decode("utf-8")
        _sha256(confirmed_outline_sha256, "confirmed_outline_sha256")
        _sha256(source_outline_sha256, "source_outline_sha256")
        if (expected_revision is None) != (expected_outline_sha256 is None):
            raise ValueError("outline review binding is incomplete")
        if expected_revision is not None:
            if expected_revision < 1:
                raise ValueError("expected_revision is invalid")
            assert expected_outline_sha256 is not None
            _sha256(expected_outline_sha256, "expected_outline_sha256")
        try:
            proposed = OutlineBundle.model_validate(outline)
            proposed_source_sha256 = canonical_outline_sha256(
                proposed.model_copy(
                    update={"confirmation_metadata": OutlineConfirmationMetadata(status="draft")}
                )
            )
        except Exception:
            raise ClassroomConfirmationConflict("confirmed outline conflicts") from None
        if (
            proposed.confirmation_metadata.status != "confirmed"
            or proposed.confirmation_metadata.confirmed_by is None
            or not hmac.compare_digest(proposed_source_sha256, source_outline_sha256)
            or not hmac.compare_digest(
                canonical_outline_sha256(proposed),
                confirmed_outline_sha256,
            )
        ):
            raise ClassroomConfirmationConflict("confirmed outline conflicts")
        async with self._session_factory() as session:
            async with session.begin():
                asset, draft = await self._lock_draft(session, asset_id)
                locked_outline = None
                if expected_revision is not None:
                    assert expected_outline_sha256 is not None
                    try:
                        locked_outline = OutlineBundle.model_validate_json(
                            draft.outline_document
                        )
                    except Exception:
                        raise ClassroomConfirmationConflict(
                            "confirmed outline conflicts"
                        ) from None
                    if (
                        draft.outline_sha256 is None
                        or not hmac.compare_digest(
                            draft.outline_sha256,
                            expected_outline_sha256,
                        )
                        or not hmac.compare_digest(
                            source_outline_sha256,
                            expected_outline_sha256,
                        )
                        or not matches_reviewed_outline_binding(
                            lifecycle_state=asset.lifecycle_state,
                            revision=draft.revision,
                            outline=locked_outline,
                            confirmed_outline_sha256=draft.confirmed_outline_sha256,
                            expected_revision=expected_revision,
                            expected_outline_sha256=expected_outline_sha256,
                        )
                    ):
                        raise ClassroomConfirmationConflict(
                            "confirmed outline conflicts"
                        )
                if draft.generation_job_id is None:
                    raise ClassroomConfirmationConflict(
                        "confirmed outline job binding is unavailable"
                    )
                job = await session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.id == draft.generation_job_id,
                        GenerationJob.tenant_id == self._tenant_id,
                        GenerationJob.classroom_draft_id == draft.id,
                        GenerationJob.job_kind == "generation",
                    )
                    .with_for_update()
                )
                if job is None:
                    raise ClassroomConfirmationConflict(
                        "confirmed outline job binding is unavailable"
                    )
                if asset.lifecycle_state == "awaiting_outline":
                    if (
                        draft.outline_sha256 is None
                        or not hmac.compare_digest(
                            draft.outline_sha256,
                            source_outline_sha256,
                        )
                        or job.phase != "outline"
                        or job.status != "awaiting_confirmation"
                    ):
                        raise ClassroomConfirmationConflict("confirmed outline conflicts")
                    asset.lifecycle_state = transition("awaiting_outline", "generating_content")
                elif asset.lifecycle_state != "generating_content":
                    raise ClassroomPersistenceError("outline confirmation state is invalid")
                if draft.confirmed_outline_sha256 is None:
                    draft.outline_document = payload
                    draft.confirmed_outline_sha256 = confirmed_outline_sha256
                    draft.revision += 1
                else:
                    try:
                        persisted = locked_outline or OutlineBundle.model_validate_json(
                            draft.outline_document
                        )
                        persisted_source_sha256 = canonical_outline_sha256(
                            persisted.model_copy(
                                update={
                                    "confirmation_metadata": OutlineConfirmationMetadata(
                                        status="draft"
                                    )
                                }
                            )
                        )
                    except Exception:
                        raise ClassroomConfirmationConflict("confirmed outline conflicts") from None
                    if (
                        draft.outline_sha256 is None
                        or not hmac.compare_digest(
                            draft.outline_sha256,
                            source_outline_sha256,
                        )
                        or not hmac.compare_digest(
                            persisted_source_sha256,
                            source_outline_sha256,
                        )
                        or not hmac.compare_digest(
                            draft.confirmed_outline_sha256,
                            canonical_outline_sha256(persisted),
                        )
                        or persisted.confirmation_metadata.confirmed_by
                        != proposed.confirmation_metadata.confirmed_by
                    ):
                        raise ClassroomConfirmationConflict("confirmed outline conflicts")
                draft.updated_at = func.now()
                asset.updated_at = func.now()
                await session.flush()
        record = await self.get_workflow(asset_id)
        if record is None:
            raise ClassroomPersistenceError("classroom workflow is unavailable")
        return record

    async def mark_generation_succeeded(
        self,
        asset_id: str,
        job_id: str,
    ) -> ClassroomRecord:
        async with self._session_factory() as session:
            async with session.begin():
                asset, draft = await self._lock_draft(session, asset_id)
                version = await session.scalar(
                    select(ClassroomVersion).where(
                        ClassroomVersion.generation_job_id == job_id,
                        ClassroomVersion.classroom_id == asset_id,
                        ClassroomVersion.tenant_id == self._tenant_id,
                    )
                )
                if (
                    draft.generation_job_id != job_id
                    or version is None
                    or draft.base_version_id != version.id
                    or not hmac.compare_digest(
                        draft.document_sha256,
                        version.document_sha256,
                    )
                ):
                    raise ClassroomPersistenceError("generated classroom version is unavailable")
                if asset.lifecycle_state == "generating_content":
                    asset.lifecycle_state = transition("generating_content", "editing")
                    asset.updated_at = func.now()
                elif asset.lifecycle_state != "editing":
                    raise ClassroomPersistenceError("generated classroom state is invalid")
                await session.flush()
        record = await self.get_workflow(asset_id)
        if record is None:
            raise ClassroomPersistenceError("classroom workflow is unavailable")
        return record

    async def update_document(
        self,
        asset_id: str,
        document: dict[str, Any],
        document_sha256: str,
        expected_revision: int,
    ) -> ClassroomRecord | None:
        payload = canonical_json_bytes(document).decode("utf-8")
        _sha256(document_sha256, "document_sha256")
        async with self._session_factory() as session:
            async with session.begin():
                asset, draft = await self._lock_draft(session, asset_id)
                if asset.lifecycle_state != "editing":
                    raise ClassroomPersistenceError("draft state is invalid")
                if draft.revision != expected_revision:
                    return None
                draft.document = payload
                draft.document_sha256 = document_sha256
                draft.validation_report = None
                draft.validation_report_sha256 = None
                draft.validation_revision = None
                draft.validation_document_sha256 = None
                draft.revision += 1
                draft.updated_at = func.now()
                asset.updated_at = func.now()
                await session.flush()
        return await self.get_workflow(asset_id)

    async def available_media_bindings(
        self,
        asset_id: str,
    ) -> tuple[ClassroomMediaBinding, ...]:
        async with self._session_factory() as session:
            draft = await session.scalar(
                select(ClassroomDraft).where(
                    ClassroomDraft.classroom_id == asset_id,
                    ClassroomDraft.tenant_id == self._tenant_id,
                )
            )
            if draft is None:
                return ()
            uploads = tuple(
                await session.scalars(
                    select(ClassroomDraftMedia)
                    .where(
                        ClassroomDraftMedia.tenant_id == self._tenant_id,
                        ClassroomDraftMedia.classroom_id == asset_id,
                        ClassroomDraftMedia.status == "uploaded",
                    )
                    .order_by(ClassroomDraftMedia.id)
                )
            )
            bindings: list[ClassroomMediaBinding] = []
            for item in uploads:
                if item.object_revision is None:
                    continue
                try:
                    relative_name = draft_media_relative_path(item.id, item.mime_type)
                except InvalidDraftMedia:
                    continue
                bindings.append(
                    ClassroomMediaBinding(
                        media_id=item.id,
                        relative_name=relative_name,
                        mime_type=item.mime_type,
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                    )
                )
            if draft.base_version_id is None:
                return tuple(bindings)
            version = await session.scalar(
                select(ClassroomVersion).where(
                    ClassroomVersion.id == draft.base_version_id,
                    ClassroomVersion.classroom_id == asset_id,
                    ClassroomVersion.tenant_id == self._tenant_id,
                )
            )
            if version is None:
                return tuple(bindings)
            artifacts = tuple(
                await session.scalars(
                    select(ClassroomArtifact)
                    .where(
                        ClassroomArtifact.tenant_id == self._tenant_id,
                        ClassroomArtifact.classroom_version_id == draft.base_version_id,
                        ClassroomArtifact.artifact_kind == "media",
                    )
                    .order_by(ClassroomArtifact.relative_name)
                )
            )
            resolved = self._verified_bound_version_media(
                asset_id,
                draft,
                version,
                artifacts,
            )
            if resolved is None:
                return tuple(bindings)
            bindings.extend(
                ClassroomMediaBinding(
                    media_id=item.id,
                    relative_name=item.relative_path,
                    mime_type=item.mime_type,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                )
                for item in resolved
            )
            return tuple(bindings)

    async def save_validation_report(
        self,
        asset_id: str,
        report: dict[str, object],
        report_sha256: str,
        expected_revision: int,
        expected_document_sha256: str,
    ) -> ClassroomRecord | None:
        payload = canonical_json_bytes(report).decode("utf-8")
        _sha256(report_sha256, "report_sha256")
        _sha256(expected_document_sha256, "expected_document_sha256")
        async with self._session_factory() as session:
            async with session.begin():
                asset, draft = await self._lock_draft(session, asset_id)
                if asset.lifecycle_state != "editing":
                    raise ClassroomPersistenceError("validation state is invalid")
                if (
                    draft.revision != expected_revision
                    or not hmac.compare_digest(
                        draft.document_sha256,
                        expected_document_sha256,
                    )
                    or report.get("draftRevision") != expected_revision
                    or report.get("documentSha256") != expected_document_sha256
                ):
                    return None
                draft.validation_report = payload
                draft.validation_report_sha256 = report_sha256
                draft.validation_revision = expected_revision
                draft.validation_document_sha256 = expected_document_sha256
                draft.updated_at = func.now()
                asset.updated_at = func.now()
                await session.flush()
        record = await self.get_workflow(asset_id)
        if record is None:
            raise ClassroomPersistenceError("classroom workflow is unavailable")
        return record

    @staticmethod
    def _media_record(model: ClassroomDraftMedia) -> DraftMediaRecord:
        return DraftMediaRecord(
            id=model.id,
            classroom_id=model.classroom_id,
            mime_type=model.mime_type,
            sha256=model.sha256,
            size_bytes=model.size_bytes,
            object_key=model.object_key,
            ownership_token=model.ownership_token,
            object_revision=model.object_revision,
            status=model.status,
            last_error_code=model.last_error_code,
        )

    async def reserve_media(self, media: NewDraftMedia) -> DraftMediaRecord:
        async with self._session_factory() as session:
            async with session.begin():
                asset = await session.scalar(
                    select(ClassroomAsset).where(
                        ClassroomAsset.id == media.classroom_id,
                        ClassroomAsset.tenant_id == self._tenant_id,
                    )
                )
                if asset is None:
                    raise ClassroomAssetNotFoundError(media.classroom_id)
                model = ClassroomDraftMedia(
                    id=media.id,
                    tenant_id=self._tenant_id,
                    classroom_id=media.classroom_id,
                    uploaded_by=media.uploaded_by,
                    object_key=media.object_key,
                    mime_type=media.mime_type,
                    sha256=media.sha256,
                    size_bytes=media.size_bytes,
                    status="writing",
                    ownership_token=media.ownership_token,
                    object_revision=None,
                    last_error_code=None,
                )
                session.add(model)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise ClassroomPersistenceError("draft media conflicts") from exc
                return self._media_record(model)

    async def complete_media(
        self,
        asset_id: str,
        media_id: str,
        object_revision: str,
    ) -> DraftMediaRecord:
        _required(object_revision, "object_revision", 256)
        async with self._session_factory() as session:
            async with session.begin():
                model = await session.scalar(
                    select(ClassroomDraftMedia)
                    .where(
                        ClassroomDraftMedia.id == media_id,
                        ClassroomDraftMedia.classroom_id == asset_id,
                        ClassroomDraftMedia.tenant_id == self._tenant_id,
                        ClassroomDraftMedia.status == "writing",
                    )
                    .with_for_update()
                )
                if model is None:
                    raise ClassroomPersistenceError("draft media receipt is unavailable")
                model.status = "uploaded"
                model.object_revision = object_revision
                model.last_error_code = None
                model.updated_at = func.now()
                await session.flush()
                return self._media_record(model)

    async def fail_media(
        self,
        asset_id: str,
        media_id: str,
        error_code: str,
    ) -> None:
        _required(error_code, "error_code", 64)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ClassroomDraftMedia)
                    .where(
                        ClassroomDraftMedia.id == media_id,
                        ClassroomDraftMedia.classroom_id == asset_id,
                        ClassroomDraftMedia.tenant_id == self._tenant_id,
                        ClassroomDraftMedia.status == "writing",
                    )
                    .values(
                        status="failed",
                        last_error_code=error_code,
                        updated_at=func.now(),
                    )
                )

    async def mark_media_cleanup_pending(
        self,
        asset_id: str,
        media_id: str,
        error_code: str,
    ) -> DraftMediaRecord:
        _required(error_code, "error_code", 64)
        async with self._session_factory() as session:
            async with session.begin():
                model = await session.scalar(
                    select(ClassroomDraftMedia)
                    .where(
                        ClassroomDraftMedia.id == media_id,
                        ClassroomDraftMedia.classroom_id == asset_id,
                        ClassroomDraftMedia.tenant_id == self._tenant_id,
                        ClassroomDraftMedia.status.in_(("writing", "cleanup_pending")),
                    )
                    .with_for_update()
                )
                if model is None:
                    raise ClassroomPersistenceError("draft media receipt is unavailable")
                model.status = "cleanup_pending"
                model.last_error_code = error_code
                model.updated_at = func.now()
                await session.flush()
                return self._media_record(model)

    async def finish_media_cleanup(
        self,
        asset_id: str,
        media_id: str,
        error_code: str,
    ) -> None:
        _required(error_code, "error_code", 64)
        async with self._session_factory() as session:
            async with session.begin():
                model = await session.scalar(
                    select(ClassroomDraftMedia)
                    .where(
                        ClassroomDraftMedia.id == media_id,
                        ClassroomDraftMedia.classroom_id == asset_id,
                        ClassroomDraftMedia.tenant_id == self._tenant_id,
                        ClassroomDraftMedia.status.in_(("cleanup_pending", "failed")),
                    )
                    .with_for_update()
                )
                if model is None:
                    raise ClassroomPersistenceError("draft media receipt is unavailable")
                model.status = "failed"
                model.object_revision = None
                model.last_error_code = error_code
                model.updated_at = func.now()
                await session.flush()

    async def get_media_receipt(
        self,
        asset_id: str,
        media_id: str,
    ) -> DraftMediaRecord | None:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(ClassroomDraftMedia).where(
                    ClassroomDraftMedia.id == media_id,
                    ClassroomDraftMedia.classroom_id == asset_id,
                    ClassroomDraftMedia.tenant_id == self._tenant_id,
                )
            )
            return self._media_record(model) if model is not None else None

    async def list_cleanup_pending(
        self,
        asset_id: str,
        *,
        limit: int = 8,
    ) -> tuple[DraftMediaRecord, ...]:
        _required(asset_id, "asset_id", 128)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 8:
            raise ValueError("limit must be between 1 and 8")
        async with self._session_factory() as session:
            models = (
                await session.scalars(
                    select(ClassroomDraftMedia)
                    .where(
                        ClassroomDraftMedia.classroom_id == asset_id,
                        ClassroomDraftMedia.tenant_id == self._tenant_id,
                        ClassroomDraftMedia.status == "cleanup_pending",
                    )
                    .order_by(
                        ClassroomDraftMedia.created_at,
                        ClassroomDraftMedia.id,
                    )
                    .limit(limit)
                )
            ).all()
            return tuple(self._media_record(model) for model in models)

    async def get_media(
        self,
        asset_id: str,
        media_id: str,
    ) -> DraftMediaRecord | None:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(ClassroomDraftMedia).where(
                    ClassroomDraftMedia.id == media_id,
                    ClassroomDraftMedia.classroom_id == asset_id,
                    ClassroomDraftMedia.tenant_id == self._tenant_id,
                    ClassroomDraftMedia.status == "uploaded",
                )
            )
            return self._media_record(model) if model is not None else None

    async def get_bound_version_media(
        self,
        asset_id: str,
        media_id: str,
    ) -> BoundClassroomMedia | None:
        _required(asset_id, "asset_id", 128)
        _required(media_id, "media_id", 128)
        async with self._session_factory() as session:
            draft = await session.scalar(
                select(ClassroomDraft)
                .where(
                    ClassroomDraft.classroom_id == asset_id,
                    ClassroomDraft.tenant_id == self._tenant_id,
                )
                .with_for_update()
            )
            if draft is None or draft.base_version_id is None:
                return None
            version = await session.scalar(
                select(ClassroomVersion).where(
                    ClassroomVersion.id == draft.base_version_id,
                    ClassroomVersion.classroom_id == asset_id,
                    ClassroomVersion.tenant_id == self._tenant_id,
                )
            )
            if version is None:
                return None
            artifacts = tuple(
                await session.scalars(
                    select(ClassroomArtifact)
                    .where(
                        ClassroomArtifact.tenant_id == self._tenant_id,
                        ClassroomArtifact.classroom_version_id == draft.base_version_id,
                        ClassroomArtifact.artifact_kind == "media",
                    )
                    .order_by(ClassroomArtifact.relative_name, ClassroomArtifact.id)
                )
            )
            resolved = self._verified_bound_version_media(
                asset_id,
                draft,
                version,
                artifacts,
            )
            if resolved is None:
                return None
            return next((item for item in resolved if item.id == media_id), None)

    async def insert_published_version(
        self,
        published: PublishedClassroomVersion,
    ) -> ClassroomVersion:
        """Atomically freeze a version, record publication/audit, and advance its asset."""

        async with self._session_factory() as session:
            async with session.begin():
                version_number = await allocate_classroom_version_number(
                    session,
                    tenant_id=self._tenant_id,
                    classroom_id=published.classroom_id,
                )
                asset = await session.scalar(
                    select(ClassroomAsset)
                    .where(
                        ClassroomAsset.id == published.classroom_id,
                        ClassroomAsset.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if asset is None:
                    raise ClassroomAssetNotFoundError(published.classroom_id)

                next_state = transition(asset.lifecycle_state, "published")
                source_version = await session.scalar(
                    select(ClassroomVersion)
                    .where(
                        ClassroomVersion.id == published.source_version_id,
                        ClassroomVersion.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if source_version is None:
                    raise ValueError("source classroom version does not exist")
                if source_version.classroom_id != published.classroom_id:
                    raise ValueError("source classroom version belongs to another asset")
                if source_version.generation_job_id is None:
                    raise ValueError("source classroom version is not a materialized result")
                version = ClassroomVersion(
                    id=published.id,
                    tenant_id=self._tenant_id,
                    classroom_id=published.classroom_id,
                    version_number=version_number,
                    generation_job_id=None,
                    source_version_id=source_version.id,
                    document_sha256=published.document.sha256,
                    media_manifest_sha256=published.document.media_manifest_sha256,
                    document_object_key=published.document.object_key,
                )
                session.add(version)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise_for_classroom_version_allocation_conflict(exc)
                    raise

                session.add(
                    Publication(
                        id=published.publication_id,
                        tenant_id=self._tenant_id,
                        classroom_id=published.classroom_id,
                        classroom_version_id=published.id,
                        actor_id=published.actor_id,
                        scope=published.scope,
                        class_id=published.class_id,
                    )
                )
                session.add(
                    AuditLog(
                        tenant_id=self._tenant_id,
                        actor_id=published.actor_id,
                        action="teaching.classroom.published",
                        resource_type="classroom_version",
                        resource_id=published.id,
                    )
                )
                asset.lifecycle_state = next_state
                asset.current_published_version_id = published.id
                asset.updated_at = func.now()
                await session.flush()
                return version

    async def replace_document(
        self,
        version_id: str,
        document: ClassroomDocumentReference,
    ) -> None:
        """Attempt a replacement while mapping the database immutability fence."""

        _required(version_id, "version_id", 128)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await session.execute(
                        update(ClassroomVersion)
                        .where(
                            ClassroomVersion.id == version_id,
                            ClassroomVersion.tenant_id == self._tenant_id,
                        )
                        .values(
                            document_sha256=document.sha256,
                            media_manifest_sha256=document.media_manifest_sha256,
                            document_object_key=document.object_key,
                        )
                    )
                    if result.rowcount != 1:
                        raise ClassroomVersionNotFoundError(version_id)
        except DBAPIError as exc:
            if _is_immutable_trigger_error(exc):
                raise ImmutableVersionError(version_id) from None
            raise

    async def delete_version(self, version_id: str) -> None:
        """Attempt deletion while mapping the database immutability fence."""

        _required(version_id, "version_id", 128)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await session.execute(
                        delete(ClassroomVersion).where(
                            ClassroomVersion.id == version_id,
                            ClassroomVersion.tenant_id == self._tenant_id,
                        )
                    )
                    if result.rowcount != 1:
                        raise ClassroomVersionNotFoundError(version_id)
        except DBAPIError as exc:
            if _is_immutable_trigger_error(exc):
                raise ImmutableVersionError(version_id) from None
            raise


__all__ = [
    "ClassroomAssetNotFoundError",
    "ClassroomDocumentReference",
    "ClassroomPersistenceError",
    "ClassroomVersionAllocationError",
    "ClassroomVersionNotFoundError",
    "ImmutableVersionError",
    "PublishedClassroomVersion",
    "SqlAlchemyClassroomRepository",
]
