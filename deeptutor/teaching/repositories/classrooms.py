"""Transactional tenant repository for immutable classroom publication."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.contracts import TeachingBrief, canonical_json_bytes
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
from deeptutor.teaching.models.jobs import GenerationJob
from deeptutor.teaching.models.platform import AuditLog
from deeptutor.teaching.repositories.classroom_version_allocation import (
    ClassroomVersionAllocationError,
    allocate_classroom_version_number,
    raise_for_classroom_version_allocation_conflict,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.classrooms import (
    ClassroomRecord,
    DraftMediaRecord,
    NewClassroomWorkflow,
    NewDraftMedia,
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
        )

    async def create_workflow(
        self,
        workflow: NewClassroomWorkflow,
    ) -> ClassroomRecord:
        if workflow.tenant_id != self._tenant_id:
            raise ValueError("workflow tenant does not match repository")
        brief = workflow.teaching_brief
        brief_document = canonical_json_bytes(brief).decode("utf-8")
        empty_document = canonical_json_bytes({}).decode("utf-8")
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
                try:
                    await session.flush()
                except IntegrityError as exc:
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
    ) -> ClassroomRecord:
        payload = canonical_json_bytes(outline).decode("utf-8")
        _sha256(confirmed_outline_sha256, "confirmed_outline_sha256")
        async with self._session_factory() as session:
            async with session.begin():
                asset, draft = await self._lock_draft(session, asset_id)
                if asset.lifecycle_state == "awaiting_outline":
                    asset.lifecycle_state = transition("awaiting_outline", "generating_content")
                elif asset.lifecycle_state != "generating_content":
                    raise ClassroomPersistenceError("outline confirmation state is invalid")
                if draft.confirmed_outline_sha256 not in {
                    None,
                    confirmed_outline_sha256,
                }:
                    raise ClassroomPersistenceError("confirmed outline conflicts")
                if draft.confirmed_outline_sha256 is None:
                    draft.outline_document = payload
                    draft.outline_sha256 = confirmed_outline_sha256
                    draft.confirmed_outline_sha256 = confirmed_outline_sha256
                    draft.revision += 1
                elif draft.outline_document != payload:
                    raise ClassroomPersistenceError("confirmed outline conflicts")
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
                version_id = await session.scalar(
                    select(ClassroomVersion.id).where(
                        ClassroomVersion.generation_job_id == job_id,
                        ClassroomVersion.classroom_id == asset_id,
                        ClassroomVersion.tenant_id == self._tenant_id,
                    )
                )
                if draft.generation_job_id != job_id or version_id is None:
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
                draft.revision += 1
                draft.updated_at = func.now()
                asset.updated_at = func.now()
                await session.flush()
        return await self.get_workflow(asset_id)

    async def available_media_ids(self, asset_id: str) -> frozenset[str]:
        async with self._session_factory() as session:
            values = await session.scalars(
                select(ClassroomDraftMedia.id).where(
                    ClassroomDraftMedia.tenant_id == self._tenant_id,
                    ClassroomDraftMedia.classroom_id == asset_id,
                    ClassroomDraftMedia.status == "uploaded",
                )
            )
            return frozenset(values)

    async def save_validation_report(
        self,
        asset_id: str,
        report: dict[str, object],
        report_sha256: str,
    ) -> ClassroomRecord:
        payload = canonical_json_bytes(report).decode("utf-8")
        _sha256(report_sha256, "report_sha256")
        async with self._session_factory() as session:
            async with session.begin():
                asset, draft = await self._lock_draft(session, asset_id)
                if asset.lifecycle_state != "editing":
                    raise ClassroomPersistenceError("validation state is invalid")
                draft.validation_report = payload
                draft.validation_report_sha256 = report_sha256
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
