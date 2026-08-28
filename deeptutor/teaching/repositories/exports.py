"""Tenant-scoped persistence for pinned classroom exports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
from typing import Protocol

from sqlalchemy import and_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.models.classrooms import (
    ClassroomAsset,
    ClassroomDraft,
    ClassroomDraftMedia,
    ClassroomExport,
    ClassroomExportPolicy,
    ClassroomExportPolicyOperation,
    ClassroomPublicationMaterialization,
    ClassroomVersion,
    TeachingBrief,
)
from deeptutor.teaching.models.jobs import ClassroomArtifact, GenerationJob
from deeptutor.teaching.object_store import (
    ClassroomArtifactStore,
    ObjectStoreNotFound,
)
from deeptutor.teaching.repositories.student_visibility import teacher_asset_visible
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.exports import (
    ExportCommand,
    ExportIdempotencyConflict,
    ExportInputReceipt,
    ExportRecord,
    ExportRevisionConflict,
    ExportSource,
    ExportSourceMedia,
)

_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_ABSENT_POLICY_REVISION = "absent"


class ExportPolicyConflict(RuntimeError):
    """The tenant export policy changed after the caller observed it."""


@dataclass(frozen=True)
class ClassroomExportPolicyState:
    tenant_id: str
    exists: bool
    allow_mp4: bool
    revision: str
    operation_id: str | None


def _policy_state(
    tenant_id: str,
    model: ClassroomExportPolicy | None,
) -> ClassroomExportPolicyState:
    if model is None:
        return ClassroomExportPolicyState(
            tenant_id=tenant_id,
            exists=False,
            allow_mp4=False,
            revision=_ABSENT_POLICY_REVISION,
            operation_id=None,
        )
    return ClassroomExportPolicyState(
        tenant_id=tenant_id,
        exists=model.exists,
        allow_mp4=model.allow_mp4,
        revision=model.revision,
        operation_id=model.operation_id,
    )


def _validate_policy_revision(value: str) -> None:
    if value == _ABSENT_POLICY_REVISION:
        return
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("export policy revision is invalid")


def _validate_policy_operation_id(value: str) -> None:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("export policy operation id is invalid")


def _is_policy_mutation_conflict(exc: IntegrityError) -> bool:
    original = exc.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate == "23505":
        constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
        return constraint in {
            None,
            "pk_classroom_export_policies",
            "pk_classroom_export_policy_operations",
        }
    message = str(original)
    return any(
        marker in message
        for marker in (
            "UNIQUE constraint failed: classroom_export_policies.tenant_id",
            "UNIQUE constraint failed: classroom_export_policy_operations.operation_id",
        )
    )


def _policy_operation_matches(
    operation: ClassroomExportPolicyOperation,
    *,
    mutation: str,
    expected_revision: str,
    allow_mp4: bool | None,
    operation_id: str,
    updated_by: str,
) -> bool:
    return (
        hmac.compare_digest(operation.operation_id, operation_id)
        and hmac.compare_digest(operation.mutation, mutation)
        and hmac.compare_digest(operation.expected_revision, expected_revision)
        and operation.allow_mp4 == allow_mp4
        and hmac.compare_digest(operation.updated_by, updated_by)
    )


def _policy_operation_state(
    tenant_id: str,
    operation: ClassroomExportPolicyOperation,
) -> ClassroomExportPolicyState:
    if not hmac.compare_digest(operation.tenant_id, tenant_id):
        raise ExportPolicyConflict
    return ClassroomExportPolicyState(
        tenant_id=tenant_id,
        exists=operation.result_exists,
        allow_mp4=operation.result_allow_mp4,
        revision=operation.result_revision,
        operation_id=operation.result_operation_id,
    )


class ExportStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> ClassroomArtifactStore: ...


async def _read_document(store: ClassroomArtifactStore, key: str) -> bytes:
    payload = bytearray()
    try:
        stream = await store.open(key)
    except ObjectStoreNotFound:
        raise ValueError("classroom document object is unavailable") from None
    async for chunk in stream:
        if not isinstance(chunk, bytes):
            raise ValueError("classroom document stream is invalid")
        payload.extend(chunk)
        if len(payload) > _MAX_DOCUMENT_BYTES:
            raise ValueError("classroom document exceeds the export limit")
    return bytes(payload)


def _document(value: bytes) -> ClassroomDocument:
    try:
        return ClassroomDocument.model_validate_json(value)
    except Exception:
        raise ValueError("classroom document is invalid") from None


def _source_media(
    document: ClassroomDocument,
    *,
    draft_media: tuple[ClassroomDraftMedia, ...] = (),
    artifacts: tuple[ClassroomArtifact, ...] = (),
    publication: ClassroomPublicationMaterialization | None = None,
) -> tuple[ExportSourceMedia, ...]:
    uploads = {item.id: item for item in draft_media if item.status == "uploaded"}
    version_files = {
        (item.relative_name, item.sha256): item
        for item in artifacts
        if item.artifact_kind == "media"
    }
    published_files = {
        (row["mediaId"], row["relativeName"], row["sha256"]): row
        for row in _confirmed_artifacts(publication)
        if row["artifactKind"] == "media"
    }
    result: list[ExportSourceMedia] = []
    for declared in document.media_manifest:
        upload = uploads.get(declared.media_id)
        if (
            upload is not None
            and upload.sha256 == declared.sha256
            and upload.size_bytes == declared.size_bytes
            and upload.mime_type == declared.mime_type
        ):
            result.append(
                ExportSourceMedia(
                    media_id=declared.media_id,
                    relative_name=declared.relative_path,
                    object_key=upload.object_key,
                    sha256=upload.sha256,
                    size_bytes=upload.size_bytes,
                    mime_type=upload.mime_type,
                )
            )
            continue
        artifact = version_files.get((declared.relative_path, declared.sha256))
        if (
            artifact is not None
            and artifact.size_bytes == declared.size_bytes
            and artifact.mime_type == declared.mime_type
        ):
            result.append(
                ExportSourceMedia(
                    media_id=declared.media_id,
                    relative_name=declared.relative_path,
                    object_key=artifact.object_key,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    mime_type=artifact.mime_type,
                )
            )
            continue
        published = published_files.get(
            (declared.media_id, declared.relative_path, declared.sha256)
        )
        if (
            published is not None
            and published["sizeBytes"] == declared.size_bytes
            and published["mimeType"] == declared.mime_type
        ):
            result.append(
                ExportSourceMedia(
                    media_id=declared.media_id,
                    relative_name=declared.relative_path,
                    object_key=published["objectKey"],
                    sha256=published["sha256"],
                    size_bytes=published["sizeBytes"],
                    mime_type=published["mimeType"],
                )
            )
    return tuple(result)


def _confirmed_artifacts(
    publication: ClassroomPublicationMaterialization | None,
) -> tuple[dict[str, object], ...]:
    if publication is None:
        return ()
    if publication.status != "finalized" or publication.confirmed_artifacts is None:
        raise ValueError("published classroom materialization is not finalized")
    try:
        decoded = json.loads(publication.confirmed_artifacts)
    except (TypeError, ValueError):
        raise ValueError("published classroom artifacts are invalid") from None
    expected = {
        "relativeName",
        "objectKey",
        "sha256",
        "sizeBytes",
        "mimeType",
        "artifactKind",
        "mediaId",
    }
    if (
        not isinstance(decoded, list)
        or canonical_json_bytes(decoded).decode("utf-8")
        != publication.confirmed_artifacts
    ):
        raise ValueError("published classroom artifacts are invalid")
    rows: list[dict[str, object]] = []
    for row in decoded:
        if (
            not isinstance(row, dict)
            or set(row) != expected
            or not isinstance(row.get("relativeName"), str)
            or not isinstance(row.get("objectKey"), str)
            or not isinstance(row.get("sha256"), str)
            or not isinstance(row.get("sizeBytes"), int)
            or isinstance(row.get("sizeBytes"), bool)
            or row["sizeBytes"] < 0
            or not isinstance(row.get("mimeType"), str)
            or row.get("artifactKind") not in {"dsl_json", "media"}
            or (
                row.get("artifactKind") == "dsl_json"
                and row.get("mediaId") is not None
            )
            or (
                row.get("artifactKind") == "media"
                and not isinstance(row.get("mediaId"), str)
            )
        ):
            raise ValueError("published classroom artifacts are invalid")
        rows.append(row)
    return tuple(rows)


class SqlAlchemyClassroomExportRepository:
    """Load exact draft/version inputs and persist their export lifecycle."""

    def __init__(
        self,
        engine: AsyncEngine,
        tenant_id: str,
        stores: ExportStoreProvider,
    ) -> None:
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._stores = stores
        self._session_factory = async_sessionmaker(translated, expire_on_commit=False)

    async def _draft_parts(
        self,
        session: AsyncSession,
        asset_id: str,
        *,
        lock: bool = False,
    ) -> tuple[
        ClassroomDraft,
        ClassroomAsset,
        TeachingBrief | None,
        tuple[ClassroomDraftMedia, ...],
        tuple[ClassroomArtifact, ...],
        ClassroomPublicationMaterialization | None,
    ] | None:
        statement = (
            select(ClassroomDraft, ClassroomAsset, TeachingBrief)
            .join(
                ClassroomAsset,
                and_(
                    ClassroomAsset.id == ClassroomDraft.classroom_id,
                    ClassroomAsset.tenant_id == ClassroomDraft.tenant_id,
                ),
            )
            .outerjoin(
                TeachingBrief,
                and_(
                    TeachingBrief.id == ClassroomDraft.teaching_brief_id,
                    TeachingBrief.tenant_id == ClassroomDraft.tenant_id,
                ),
            )
            .where(
                ClassroomDraft.classroom_id == asset_id,
                ClassroomDraft.tenant_id == self._tenant_id,
                teacher_asset_visible(
                    ClassroomDraft.classroom_id,
                    ClassroomDraft.tenant_id,
                ),
            )
        )
        if lock:
            statement = statement.with_for_update(of=ClassroomDraft)
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        draft, asset, brief = row
        uploads = tuple(
            (
                await session.scalars(
                    select(ClassroomDraftMedia).where(
                        ClassroomDraftMedia.tenant_id == self._tenant_id,
                        ClassroomDraftMedia.classroom_id == asset_id,
                        ClassroomDraftMedia.status == "uploaded",
                    )
                )
            ).all()
        )
        artifacts: tuple[ClassroomArtifact, ...] = ()
        publication = None
        if draft.base_version_id is not None:
            artifacts = tuple(
                (
                    await session.scalars(
                        select(ClassroomArtifact).where(
                            ClassroomArtifact.tenant_id == self._tenant_id,
                            ClassroomArtifact.classroom_version_id
                            == draft.base_version_id,
                            ClassroomArtifact.artifact_kind == "media",
                        )
                    )
                ).all()
            )
            publication = await session.scalar(
                select(ClassroomPublicationMaterialization).where(
                    ClassroomPublicationMaterialization.tenant_id
                    == self._tenant_id,
                    ClassroomPublicationMaterialization.classroom_id == asset_id,
                    ClassroomPublicationMaterialization.version_id
                    == draft.base_version_id,
                    ClassroomPublicationMaterialization.status == "finalized",
                )
            )
        return draft, asset, brief, uploads, artifacts, publication

    async def get_draft_source(self, asset_id: str) -> ExportSource | None:
        async with self._session_factory() as session:
            parts = await self._draft_parts(session, asset_id)
        if parts is None:
            return None
        draft, asset, brief, uploads, artifacts, publication = parts
        body = draft.document.encode("utf-8")
        document = _document(body)
        return ExportSource(
            tenant_id=self._tenant_id,
            asset_id=asset.id,
            owner_id=asset.owner_id,
            course_id=brief.course_id if brief is not None else None,
            class_id=brief.class_id if brief is not None else None,
            classroom_draft_id=draft.id,
            classroom_version_id=None,
            draft_revision=draft.revision,
            document=body,
            document_sha256=draft.document_sha256,
            media_manifest_sha256=hashlib.sha256(
                canonical_json_bytes(
                    document.model_dump(mode="json", by_alias=True, exclude_none=False)[
                        "mediaManifest"
                    ]
                )
            ).hexdigest(),
            media=_source_media(
                document,
                draft_media=uploads,
                artifacts=artifacts,
                publication=publication,
            ),
        )

    async def _version_resource(
        self,
        session: AsyncSession,
        version: ClassroomVersion,
    ) -> tuple[str | None, str | None]:
        generation_job_id = version.generation_job_id
        if generation_job_id is None and version.source_version_id is not None:
            source = await session.scalar(
                select(ClassroomVersion).where(
                    ClassroomVersion.id == version.source_version_id,
                    ClassroomVersion.tenant_id == self._tenant_id,
                )
            )
            generation_job_id = source.generation_job_id if source is not None else None
        if generation_job_id is None:
            return None, None
        job = await session.scalar(
            select(GenerationJob).where(
                GenerationJob.id == generation_job_id,
                GenerationJob.tenant_id == self._tenant_id,
            )
        )
        if job is None:
            return None, None
        return job.resource_course_id, job.resource_class_id

    async def _version_parts(
        self,
        session: AsyncSession,
        version_id: str,
        *,
        lock: bool = False,
    ) -> tuple[
        ClassroomVersion,
        ClassroomAsset,
        tuple[ClassroomArtifact, ...],
        ClassroomPublicationMaterialization | None,
        str | None,
        str | None,
    ] | None:
        statement = (
            select(ClassroomVersion, ClassroomAsset)
            .join(
                ClassroomAsset,
                and_(
                    ClassroomAsset.id == ClassroomVersion.classroom_id,
                    ClassroomAsset.tenant_id == ClassroomVersion.tenant_id,
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
        )
        if lock:
            statement = statement.with_for_update(of=ClassroomVersion)
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        version, asset = row
        artifacts = tuple(
            (
                await session.scalars(
                    select(ClassroomArtifact).where(
                        ClassroomArtifact.tenant_id == self._tenant_id,
                        ClassroomArtifact.classroom_version_id == version.id,
                        ClassroomArtifact.artifact_kind == "media",
                    )
                )
            ).all()
        )
        publication = await session.scalar(
            select(ClassroomPublicationMaterialization).where(
                ClassroomPublicationMaterialization.tenant_id == self._tenant_id,
                ClassroomPublicationMaterialization.classroom_id
                == version.classroom_id,
                ClassroomPublicationMaterialization.version_id == version.id,
                ClassroomPublicationMaterialization.status == "finalized",
            )
        )
        course_id, class_id = await self._version_resource(session, version)
        return version, asset, artifacts, publication, course_id, class_id

    async def get_version_source(self, version_id: str) -> ExportSource | None:
        async with self._session_factory() as session:
            parts = await self._version_parts(session, version_id)
        if parts is None:
            return None
        version, asset, artifacts, publication, course_id, class_id = parts
        store = await self._stores.store_for_tenant(self._tenant_id)
        body = await _read_document(store, version.document_object_key)
        document = _document(body)
        if publication is not None:
            rows = _confirmed_artifacts(publication)
            documents = [row for row in rows if row["artifactKind"] == "dsl_json"]
            if (
                publication.document_sha256 != version.document_sha256
                or publication.media_manifest_sha256
                != version.media_manifest_sha256
                or len(documents) != 1
                or documents[0]["relativeName"] != "classroom.json"
                or documents[0]["objectKey"] != version.document_object_key
                or documents[0]["sha256"] != version.document_sha256
                or documents[0]["sizeBytes"] != len(body)
                or documents[0]["mimeType"] != "application/json"
            ):
                raise ValueError("published classroom binding is invalid")
        return ExportSource(
            tenant_id=self._tenant_id,
            asset_id=asset.id,
            owner_id=asset.owner_id,
            course_id=course_id,
            class_id=class_id,
            classroom_draft_id=None,
            classroom_version_id=version.id,
            draft_revision=None,
            document=body,
            document_sha256=version.document_sha256,
            media_manifest_sha256=version.media_manifest_sha256,
            media=_source_media(
                document,
                artifacts=artifacts,
                publication=publication,
            ),
        )

    @staticmethod
    def _matches(model: ClassroomExport, command: ExportCommand) -> bool:
        return (
            model.id == command.export_id
            and model.classroom_id == command.asset_id
            and model.classroom_draft_id == command.classroom_draft_id
            and model.classroom_version_id == command.classroom_version_id
            and model.draft_revision == command.draft_revision
            and model.export_format == command.export_format
            and model.input_document_sha256 == command.document_sha256
            and model.input_media_manifest_sha256
            == command.media_manifest_sha256
            and model.idempotency_key == command.idempotency_key
            and model.request_sha256 == command.request_sha256
            and model.created_by == command.actor_id
        )

    @staticmethod
    def _status(model: ClassroomExport, job: GenerationJob | None) -> str:
        if model.status == "ready":
            return "succeeded"
        return job.status if job is not None else model.status

    async def _record(
        self,
        session: AsyncSession,
        model: ClassroomExport,
        *,
        command: ExportCommand | None = None,
    ) -> ExportRecord:
        asset = await session.scalar(
            select(ClassroomAsset).where(
                ClassroomAsset.id == model.classroom_id,
                ClassroomAsset.tenant_id == self._tenant_id,
            )
        )
        if asset is None:
            raise RuntimeError("classroom export asset is unavailable")
        job = None
        if model.generation_job_id is not None:
            job = await session.scalar(
                select(GenerationJob).where(
                    GenerationJob.id == model.generation_job_id,
                    GenerationJob.tenant_id == self._tenant_id,
                )
            )
        if command is not None:
            course_id, class_id = command.course_id, command.class_id
        elif job is not None:
            course_id, class_id = job.resource_course_id, job.resource_class_id
        elif model.classroom_draft_id is not None:
            parts = await self._draft_parts(session, asset.id)
            brief = parts[2] if parts is not None else None
            course_id = brief.course_id if brief is not None else None
            class_id = brief.class_id if brief is not None else None
        else:
            parts = await self._version_parts(
                session, model.classroom_version_id or ""
            )
            course_id = parts[4] if parts is not None else None
            class_id = parts[5] if parts is not None else None
        receipt = (
            ExportInputReceipt(
                manifest_object_key=model.input_manifest_object_key,
                manifest_sha256=model.input_manifest_sha256,
            )
            if model.input_manifest_object_key is not None
            and model.input_manifest_sha256 is not None
            else None
        )
        return ExportRecord(
            tenant_id=self._tenant_id,
            export_id=model.id,
            job_id=model.generation_job_id,
            idempotency_key=model.idempotency_key or "",
            request_sha256=model.request_sha256 or "",
            created_by=model.created_by,
            owner_id=asset.owner_id,
            course_id=course_id,
            class_id=class_id,
            asset_id=asset.id,
            export_format=model.export_format,  # type: ignore[arg-type]
            classroom_draft_id=model.classroom_draft_id,
            classroom_version_id=model.classroom_version_id,
            draft_revision=model.draft_revision,
            input_document_sha256=model.input_document_sha256 or "",
            input_media_manifest_sha256=model.input_media_manifest_sha256 or "",
            status=self._status(model, job),  # type: ignore[arg-type]
            progress_percent=(
                100
                if model.status == "ready"
                else job.progress_percent
                if job is not None
                else 0
            ),
            waiting_reason=(
                job.waiting_reason
                if job is not None
                and job.status not in {"succeeded", "failed", "canceled"}
                else None
            ),
            error_category=(
                job.error_category
                if job is not None and job.status in {"failed", "canceled"}
                else None
            ),
            error_code=(
                job.error_code
                if job is not None and job.status in {"failed", "canceled"}
                else None
            ),
            retry_of_job_id=job.retry_of_job_id if job is not None else None,
            input_receipt=receipt,
            relative_name=model.relative_name,
            object_key=model.object_key,
            sha256=model.sha256,
            size_bytes=model.size_bytes,
            mime_type=model.mime_type,
        )

    async def reserve(self, command: ExportCommand) -> ExportRecord:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:idempotency_lock_key, 0))"
                    ),
                    {
                        "idempotency_lock_key": hashlib.sha256(
                            (
                                "classroom-export-idempotency\0"
                                f"{self._tenant_id}\0{command.idempotency_key}"
                            ).encode()
                        ).hexdigest()
                    },
                )
                existing = await session.scalar(
                    select(ClassroomExport)
                    .where(
                        ClassroomExport.tenant_id == self._tenant_id,
                        ClassroomExport.idempotency_key == command.idempotency_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if not self._matches(existing, command):
                        raise ExportIdempotencyConflict(
                            "export idempotency key conflicts"
                        )
                    return await self._record(session, existing, command=command)
                if command.classroom_draft_id is not None:
                    parts = await self._draft_parts(
                        session, command.asset_id, lock=True
                    )
                    if parts is None:
                        raise ExportRevisionConflict("classroom draft is unavailable")
                    draft, _asset, _brief, uploads, artifacts, publication = parts
                    current_document = draft.document.encode("utf-8")
                    current_media = _source_media(
                        _document(current_document),
                        draft_media=uploads,
                        artifacts=artifacts,
                        publication=publication,
                    )
                    if (
                        draft.id != command.classroom_draft_id
                        or draft.revision != command.draft_revision
                        or draft.document_sha256 != command.document_sha256
                        or current_document != command.document
                        or current_media != command.media
                    ):
                        raise ExportRevisionConflict("classroom draft revision is stale")
                else:
                    parts = await self._version_parts(
                        session, command.classroom_version_id or "", lock=True
                    )
                    if parts is None:
                        raise ExportRevisionConflict("classroom version is unavailable")
                    version = parts[0]
                    if (
                        version.classroom_id != command.asset_id
                        or version.document_sha256 != command.document_sha256
                        or version.media_manifest_sha256
                        != command.media_manifest_sha256
                    ):
                        raise ExportRevisionConflict("classroom version binding changed")
                model = ClassroomExport(
                    id=command.export_id,
                    tenant_id=self._tenant_id,
                    classroom_id=command.asset_id,
                    classroom_version_id=command.classroom_version_id,
                    classroom_draft_id=command.classroom_draft_id,
                    draft_revision=command.draft_revision,
                    generation_job_id=None,
                    export_format=command.export_format,
                    input_document_sha256=command.document_sha256,
                    input_media_manifest_sha256=command.media_manifest_sha256,
                    idempotency_key=command.idempotency_key,
                    request_sha256=command.request_sha256,
                    input_manifest_object_key=None,
                    input_manifest_sha256=None,
                    status="preparing_input",
                    created_by=command.actor_id,
                )
                session.add(model)
                await session.flush()
                return await self._record(session, model, command=command)

    async def confirm_input(
        self,
        export_id: str,
        receipt: ExportInputReceipt,
    ) -> ExportRecord:
        async with self._session_factory() as session:
            async with session.begin():
                model = await session.scalar(
                    select(ClassroomExport)
                    .where(
                        ClassroomExport.id == export_id,
                        ClassroomExport.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if model is None:
                    raise RuntimeError("classroom export is unavailable")
                if model.input_manifest_object_key is not None:
                    if (
                        model.input_manifest_object_key
                        != receipt.manifest_object_key
                        or not hmac.compare_digest(
                            model.input_manifest_sha256 or "",
                            receipt.manifest_sha256,
                        )
                    ):
                        raise ExportIdempotencyConflict(
                            "export input receipt conflicts"
                        )
                else:
                    if model.status != "preparing_input":
                        raise RuntimeError("classroom export input cannot be confirmed")
                    model.input_manifest_object_key = receipt.manifest_object_key
                    model.input_manifest_sha256 = receipt.manifest_sha256
                    model.status = "input_ready"
                    await session.flush()
                return await self._record(session, model)

    async def get(self, export_id: str) -> ExportRecord | None:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(ClassroomExport).where(
                    ClassroomExport.id == export_id,
                    ClassroomExport.tenant_id == self._tenant_id,
                    ClassroomExport.classroom_id.is_not(None),
                    teacher_asset_visible(
                        ClassroomExport.classroom_id,
                        ClassroomExport.tenant_id,
                    ),
                )
            )
            return await self._record(session, model) if model is not None else None

    async def mp4_enabled(self) -> bool:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(ClassroomExportPolicy.allow_mp4).where(
                    ClassroomExportPolicy.tenant_id == self._tenant_id
                )
            )
            return value is True

    async def policy_state(self) -> ClassroomExportPolicyState:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(ClassroomExportPolicy).where(
                    ClassroomExportPolicy.tenant_id == self._tenant_id
                )
            )
            return _policy_state(self._tenant_id, model)

    async def _policy_operation(
        self,
        session: AsyncSession,
        operation_id: str,
    ) -> ClassroomExportPolicyOperation | None:
        return await session.scalar(
            select(ClassroomExportPolicyOperation).where(
                ClassroomExportPolicyOperation.operation_id == operation_id
            )
        )

    def _policy_operation_replay(
        self,
        operation: ClassroomExportPolicyOperation,
        *,
        mutation: str,
        expected_revision: str,
        allow_mp4: bool | None,
        operation_id: str,
        updated_by: str,
    ) -> ClassroomExportPolicyState:
        if not hmac.compare_digest(operation.tenant_id, self._tenant_id) or not (
            _policy_operation_matches(
                operation,
                mutation=mutation,
                expected_revision=expected_revision,
                allow_mp4=allow_mp4,
                operation_id=operation_id,
                updated_by=updated_by,
            )
        ):
            raise ExportPolicyConflict
        return _policy_operation_state(self._tenant_id, operation)

    async def _policy_replay_state(
        self,
        *,
        mutation: str,
        expected_revision: str,
        allow_mp4: bool | None,
        operation_id: str,
        updated_by: str,
    ) -> ClassroomExportPolicyState | None:
        async with self._session_factory() as session:
            operation = await self._policy_operation(session, operation_id)
            if operation is None:
                return None
            return self._policy_operation_replay(
                operation,
                mutation=mutation,
                expected_revision=expected_revision,
                allow_mp4=allow_mp4,
                operation_id=operation_id,
                updated_by=updated_by,
            )

    async def _mutate_mp4_policy(
        self,
        *,
        mutation: str,
        expected_revision: str,
        allow_mp4: bool | None,
        operation_id: str,
        updated_by: str,
    ) -> ClassroomExportPolicyState:
        _validate_policy_revision(expected_revision)
        _validate_policy_operation_id(operation_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    operation = await self._policy_operation(session, operation_id)
                    if operation is not None:
                        return self._policy_operation_replay(
                            operation,
                            mutation=mutation,
                            expected_revision=expected_revision,
                            allow_mp4=allow_mp4,
                            operation_id=operation_id,
                            updated_by=updated_by,
                        )
                    model = await session.scalar(
                        select(ClassroomExportPolicy)
                        .where(ClassroomExportPolicy.tenant_id == self._tenant_id)
                        .with_for_update()
                    )
                    operation = await self._policy_operation(session, operation_id)
                    if operation is not None:
                        return self._policy_operation_replay(
                            operation,
                            mutation=mutation,
                            expected_revision=expected_revision,
                            allow_mp4=allow_mp4,
                            operation_id=operation_id,
                            updated_by=updated_by,
                        )
                    current = _policy_state(self._tenant_id, model)
                    if not hmac.compare_digest(current.revision, expected_revision):
                        raise ExportPolicyConflict
                    result_exists = mutation == "replace"
                    result_allow_mp4 = bool(allow_mp4) if result_exists else False
                    now = datetime.now(timezone.utc)
                    next_revision = secrets.token_hex(32)
                    if model is None:
                        model = ClassroomExportPolicy(
                            tenant_id=self._tenant_id,
                            exists=result_exists,
                            allow_mp4=result_allow_mp4,
                            revision=next_revision,
                            operation_id=operation_id,
                            updated_by=updated_by,
                            updated_at=now,
                        )
                        session.add(model)
                    else:
                        model.exists = result_exists
                        model.allow_mp4 = result_allow_mp4
                        model.revision = next_revision
                        model.operation_id = operation_id
                        model.updated_by = updated_by
                        model.updated_at = now
                    result = _policy_state(self._tenant_id, model)
                    session.add(
                        ClassroomExportPolicyOperation(
                            operation_id=operation_id,
                            tenant_id=self._tenant_id,
                            mutation=mutation,
                            expected_revision=expected_revision,
                            allow_mp4=allow_mp4,
                            updated_by=updated_by,
                            result_exists=result.exists,
                            result_allow_mp4=result.allow_mp4,
                            result_revision=result.revision,
                            result_operation_id=operation_id,
                            created_at=now,
                        )
                    )
                    await session.flush()
                    return result
        except IntegrityError as exc:
            if not _is_policy_mutation_conflict(exc):
                raise
            replayed = await self._policy_replay_state(
                mutation=mutation,
                expected_revision=expected_revision,
                allow_mp4=allow_mp4,
                operation_id=operation_id,
                updated_by=updated_by,
            )
            if replayed is not None:
                return replayed
            raise ExportPolicyConflict from exc

    async def replace_mp4_policy(
        self,
        *,
        allow_mp4: bool,
        expected_revision: str,
        operation_id: str,
        updated_by: str,
    ) -> ClassroomExportPolicyState:
        return await self._mutate_mp4_policy(
            mutation="replace",
            expected_revision=expected_revision,
            allow_mp4=allow_mp4,
            operation_id=operation_id,
            updated_by=updated_by,
        )

    async def delete_mp4_policy(
        self,
        *,
        expected_revision: str,
        operation_id: str,
        updated_by: str,
    ) -> ClassroomExportPolicyState:
        return await self._mutate_mp4_policy(
            mutation="delete",
            expected_revision=expected_revision,
            allow_mp4=None,
            operation_id=operation_id,
            updated_by=updated_by,
        )


__all__ = [
    "ClassroomExportPolicyState",
    "ExportPolicyConflict",
    "SqlAlchemyClassroomExportRepository",
]
