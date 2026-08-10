"""Server-authorized reads for immutable classroom version resources."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal, Protocol

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.models import (
    ClassroomArtifact,
    ClassroomAsset,
    ClassroomExport,
    ClassroomPublicationMaterialization,
    ClassroomVersion,
    GenerationJob,
    LearningSession,
)
from deeptutor.teaching.object_store import (
    ObjectStoreAccessDenied,
    ObjectStoreError,
    ObjectStoreIntegrityError,
    ObjectStoreNotFound,
)
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext
from deeptutor.teaching.tickets import ClassroomTicketService

ReadAction = Literal[
    "classroom.document.read",
    "classroom.media.read",
    "classroom.export.read",
]
_TEACHER_READ_PERMISSIONS = (
    "classroom.edit",
    "classroom.approve",
    "classroom.publish",
    "tenant.manage",
)


class ClassroomContentError(RuntimeError):
    """Base error for classroom content delivery."""


class ClassroomContentNotFound(ClassroomContentError):
    """The requested version resource does not exist."""


class ClassroomContentAccessDenied(ClassroomContentError):
    """The current authenticated user cannot read the resource."""


class ClassroomContentIntegrityError(ClassroomContentError):
    """The database receipt and immutable object do not agree."""


@dataclass(frozen=True, slots=True)
class ContentArtifactReceipt:
    artifact_kind: Literal["dsl_json", "media", "export"]
    media_id: str | None
    relative_name: str
    object_key: str
    sha256: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class VersionContentRecord:
    tenant_id: str
    version_id: str
    source_version_id: str | None
    classroom_id: str
    owner_id: str
    course_id: str | None
    class_id: str | None
    document: ContentArtifactReceipt
    media: tuple[ContentArtifactReceipt, ...]


@dataclass(frozen=True, slots=True)
class SessionContentAccess:
    session_id: str
    tenant_id: str
    user_id: str
    classroom_version_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ExportContentRecord:
    export_id: str
    tenant_id: str
    classroom_version_id: str
    status: str
    receipt: ContentArtifactReceipt


@dataclass(frozen=True, slots=True)
class ClassroomContent:
    body: bytes
    mime_type: str
    sha256: str
    size_bytes: int
    filename: str | None = None


class ClassroomContentRepository(Protocol):
    async def get_version(
        self,
        context: TenantContext,
        version_id: str,
    ) -> VersionContentRecord | None: ...

    async def get_session(
        self,
        context: TenantContext,
        session_id: str,
    ) -> SessionContentAccess | None: ...

    async def get_export(
        self,
        context: TenantContext,
        export_id: str,
    ) -> ExportContentRecord | None: ...


class StoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str): ...


class SqlAlchemyClassroomContentRepository:
    """Resolve immutable content receipts strictly inside the selected tenant schema."""

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    def _session_factory(
        self,
        context: TenantContext,
    ) -> async_sessionmaker[AsyncSession]:
        expected_schema = tenant_schema_name(context.tenant_id)
        if context.schema_name != expected_schema:
            raise ClassroomContentAccessDenied("tenant schema binding is invalid")
        translated = self._engine.execution_options(
            schema_translate_map={"tenant": expected_schema}
        )
        return async_sessionmaker(translated, expire_on_commit=False)

    async def get_session(
        self,
        context: TenantContext,
        session_id: str,
    ) -> SessionContentAccess | None:
        session_factory = self._session_factory(context)
        async with session_factory() as database_session:
            model = await database_session.scalar(
                select(LearningSession).where(
                    LearningSession.id == session_id,
                    LearningSession.tenant_id == context.tenant_id,
                )
            )
        if model is None:
            return None
        return SessionContentAccess(
            session_id=model.id,
            tenant_id=model.tenant_id,
            user_id=model.user_id,
            classroom_version_id=model.classroom_version_id,
            status=model.status,
        )

    @staticmethod
    def _artifact_receipt(model: ClassroomArtifact) -> ContentArtifactReceipt:
        return ContentArtifactReceipt(
            artifact_kind=model.artifact_kind,  # type: ignore[arg-type]
            media_id=None,
            relative_name=model.relative_name,
            object_key=model.object_key,
            sha256=model.sha256,
            size_bytes=model.size_bytes,
            mime_type=model.mime_type,
        )

    @staticmethod
    def _publication_receipts(value: str) -> tuple[ContentArtifactReceipt, ...]:
        try:
            rows = json.loads(value)
            if not isinstance(rows, list) or canonical_json_bytes(rows).decode() != value:
                raise ValueError
            receipts = tuple(
                ContentArtifactReceipt(
                    artifact_kind=row["artifactKind"],
                    media_id=row.get("mediaId"),
                    relative_name=row["relativeName"],
                    object_key=row["objectKey"],
                    sha256=row["sha256"],
                    size_bytes=row["sizeBytes"],
                    mime_type=row["mimeType"],
                )
                for row in rows
            )
        except (KeyError, TypeError, ValueError):
            raise ClassroomContentIntegrityError(
                "published classroom receipts are invalid"
            ) from None
        if any(
            receipt.artifact_kind not in {"dsl_json", "media"}
            or not receipt.object_key
            or len(receipt.sha256) != 64
            or receipt.size_bytes < 0
            for receipt in receipts
        ):
            raise ClassroomContentIntegrityError("published classroom receipts are invalid")
        return receipts

    async def get_version(
        self,
        context: TenantContext,
        version_id: str,
    ) -> VersionContentRecord | None:
        session_factory = self._session_factory(context)
        source_version = aliased(ClassroomVersion)
        source_job = aliased(GenerationJob)
        async with session_factory() as database_session:
            row = (
                await database_session.execute(
                    select(ClassroomVersion, ClassroomAsset, source_job)
                    .join(
                        ClassroomAsset,
                        and_(
                            ClassroomAsset.id == ClassroomVersion.classroom_id,
                            ClassroomAsset.tenant_id == ClassroomVersion.tenant_id,
                        ),
                    )
                    .outerjoin(
                        source_version,
                        and_(
                            source_version.id == ClassroomVersion.source_version_id,
                            source_version.tenant_id == ClassroomVersion.tenant_id,
                        ),
                    )
                    .outerjoin(
                        source_job,
                        and_(
                            source_job.id
                            == func.coalesce(
                                ClassroomVersion.generation_job_id,
                                source_version.generation_job_id,
                            ),
                            source_job.tenant_id == ClassroomVersion.tenant_id,
                        ),
                    )
                    .where(
                        ClassroomVersion.id == version_id,
                        ClassroomVersion.tenant_id == context.tenant_id,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            version, asset, job = row
            if version.generation_job_id is not None:
                artifact_models = (
                    await database_session.scalars(
                        select(ClassroomArtifact).where(
                            ClassroomArtifact.tenant_id == context.tenant_id,
                            ClassroomArtifact.classroom_version_id == version.id,
                        )
                    )
                ).all()
                receipts = tuple(self._artifact_receipt(item) for item in artifact_models)
            else:
                publication = await database_session.scalar(
                    select(ClassroomPublicationMaterialization).where(
                        ClassroomPublicationMaterialization.tenant_id == context.tenant_id,
                        ClassroomPublicationMaterialization.version_id == version.id,
                        ClassroomPublicationMaterialization.classroom_id == version.classroom_id,
                        ClassroomPublicationMaterialization.status == "finalized",
                    )
                )
                if publication is None or publication.confirmed_artifacts is None:
                    raise ClassroomContentIntegrityError(
                        "published classroom receipts are unavailable"
                    )
                receipts = self._publication_receipts(publication.confirmed_artifacts)

        documents = tuple(item for item in receipts if item.artifact_kind == "dsl_json")
        media = tuple(item for item in receipts if item.artifact_kind == "media")
        if (
            len(documents) != 1
            or documents[0].object_key != version.document_object_key
            or documents[0].sha256 != version.document_sha256
        ):
            raise ClassroomContentIntegrityError("classroom document receipt is invalid")
        return VersionContentRecord(
            tenant_id=version.tenant_id,
            version_id=version.id,
            source_version_id=version.source_version_id,
            classroom_id=version.classroom_id,
            owner_id=asset.owner_id,
            course_id=(job.resource_course_id if job is not None else None),
            class_id=(job.resource_class_id if job is not None else None),
            document=documents[0],
            media=media,
        )

    async def get_export(
        self,
        context: TenantContext,
        export_id: str,
    ) -> ExportContentRecord | None:
        session_factory = self._session_factory(context)
        async with session_factory() as database_session:
            model = await database_session.scalar(
                select(ClassroomExport).where(
                    ClassroomExport.id == export_id,
                    ClassroomExport.tenant_id == context.tenant_id,
                    ClassroomExport.classroom_version_id.is_not(None),
                )
            )
        if (
            model is None
            or model.classroom_version_id is None
            or model.object_key is None
            or model.relative_name is None
            or model.sha256 is None
            or model.size_bytes is None
            or model.mime_type is None
        ):
            return None
        return ExportContentRecord(
            export_id=model.id,
            tenant_id=model.tenant_id,
            classroom_version_id=model.classroom_version_id,
            status=model.status,
            receipt=ContentArtifactReceipt(
                artifact_kind="export",
                media_id=None,
                relative_name=model.relative_name,
                object_key=model.object_key,
                sha256=model.sha256,
                size_bytes=model.size_bytes,
                mime_type=model.mime_type,
            ),
        )


class ClassroomContentService:
    def __init__(
        self,
        *,
        repository: ClassroomContentRepository,
        stores: StoreProvider,
        ticket_service: ClassroomTicketService | None,
    ) -> None:
        self._repository = repository
        self._stores = stores
        self._ticket_service = ticket_service

    async def _version(
        self,
        context: TenantContext,
        version_id: str,
    ) -> VersionContentRecord:
        record = await self._repository.get_version(context, version_id)
        if record is None or record.tenant_id != context.tenant_id:
            raise ClassroomContentNotFound("classroom version is unavailable")
        return record

    @staticmethod
    def _teacher_can_read(context: TenantContext, version: VersionContentRecord) -> bool:
        if context.user_id == version.owner_id:
            return True
        resource = ResourceScope(
            tenant_id=version.tenant_id,
            course_id=version.course_id,
            class_id=version.class_id,
        )
        return any(
            grant.allows_resource(permission, resource)
            for grant in context.permissions
            for permission in _TEACHER_READ_PERMISSIONS
        )

    async def _authorize(
        self,
        context: TenantContext,
        version: VersionContentRecord,
        *,
        token: str | None,
        action: ReadAction,
        resource_id: str,
    ) -> None:
        if token is None:
            if not self._teacher_can_read(context, version):
                raise ClassroomContentAccessDenied("classroom content access denied")
            return
        if self._ticket_service is None:
            raise ClassroomContentAccessDenied("classroom ticket verification is unavailable")
        claims = self._ticket_service.verify_read(
            token,
            expected_tenant_id=context.tenant_id,
            expected_user_id=context.user_id,
            expected_version_id=version.version_id,
            expected_action=action,
            expected_resource_id=resource_id,
        )
        session = await self._repository.get_session(context, claims.session_id)
        if (
            session is None
            or session.status != "active"
            or session.tenant_id != context.tenant_id
            or session.user_id != context.user_id
            or session.classroom_version_id != version.version_id
        ):
            raise ClassroomContentAccessDenied("classroom learning session is unavailable")

    async def _read(self, tenant_id: str, receipt: ContentArtifactReceipt) -> bytes:
        try:
            store = await self._stores.store_for_tenant(tenant_id)
            chunks = await store.open(receipt.object_key)
            body = bytearray()
            async for chunk in chunks:
                body.extend(chunk)
                if len(body) > receipt.size_bytes:
                    raise ClassroomContentIntegrityError("classroom object size is invalid")
        except ClassroomContentIntegrityError:
            raise
        except (ObjectStoreNotFound, ObjectStoreAccessDenied):
            raise ClassroomContentNotFound("classroom object is unavailable") from None
        except (ObjectStoreIntegrityError, ObjectStoreError):
            raise ClassroomContentIntegrityError("classroom object is invalid") from None
        materialized = bytes(body)
        if (
            len(materialized) != receipt.size_bytes
            or hashlib.sha256(materialized).hexdigest() != receipt.sha256
        ):
            raise ClassroomContentIntegrityError("classroom object receipt is invalid")
        return materialized

    @staticmethod
    def _validate_document_bindings(
        version: VersionContentRecord,
        document: ClassroomDocument,
    ) -> None:
        expected_document_version = version.source_version_id or version.version_id
        if document.classroom_version_id != expected_document_version:
            raise ClassroomContentIntegrityError("classroom document version is invalid")
        declared = {
            item.relative_path: (
                item.mime_type,
                item.sha256,
                item.size_bytes,
            )
            for item in document.media_manifest
        }
        receipts = {
            item.relative_name: (
                item.mime_type,
                item.sha256,
                item.size_bytes,
            )
            for item in version.media
            if item.artifact_kind == "media"
        }
        if declared != receipts:
            raise ClassroomContentIntegrityError("classroom media receipts are invalid")

    async def _load_version(
        self,
        context: TenantContext,
        version: VersionContentRecord,
    ) -> tuple[ClassroomDocument, bytes]:
        body = await self._read(context.tenant_id, version.document)
        try:
            document = ClassroomDocument.model_validate_json(body)
        except ValueError:
            raise ClassroomContentIntegrityError("classroom document is invalid") from None
        self._validate_document_bindings(version, document)
        return document, body

    async def load_version_document(
        self,
        context: TenantContext,
        version_id: str,
    ) -> ClassroomDocument:
        version = await self._version(context, version_id)
        document, _ = await self._load_version(context, version)
        return document

    async def issue_read_ticket(
        self,
        context: TenantContext,
        *,
        session_id: str,
        action: ReadAction,
        resource_id: str,
        ttl_seconds: int = 60,
    ) -> str:
        session = await self._repository.get_session(context, session_id)
        if (
            session is None
            or session.status != "active"
            or session.tenant_id != context.tenant_id
            or session.user_id != context.user_id
        ):
            raise ClassroomContentAccessDenied("classroom learning session is unavailable")
        version = await self._version(context, session.classroom_version_id)
        if action == "classroom.document.read":
            if resource_id != version.version_id:
                raise ClassroomContentAccessDenied("document resource is outside session")
        elif action == "classroom.media.read":
            document, _ = await self._load_version(context, version)
            if resource_id not in {item.media_id for item in document.media_manifest}:
                raise ClassroomContentAccessDenied("media resource is outside session")
        else:
            export = await self._repository.get_export(context, resource_id)
            if (
                export is None
                or export.tenant_id != context.tenant_id
                or export.classroom_version_id != version.version_id
                or export.status not in {"ready", "succeeded"}
            ):
                raise ClassroomContentAccessDenied("export resource is outside session")
        if self._ticket_service is None:
            raise ClassroomContentAccessDenied("classroom ticket issuance is unavailable")
        return self._ticket_service.issue(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=session.session_id,
            classroom_version_id=version.version_id,
            allowed_action=action,
            resource_id=resource_id,
            ttl_seconds=ttl_seconds,
        )

    async def open_document(
        self,
        context: TenantContext,
        *,
        version_id: str,
        token: str | None,
    ) -> ClassroomContent:
        version = await self._version(context, version_id)
        await self._authorize(
            context,
            version,
            token=token,
            action="classroom.document.read",
            resource_id=version_id,
        )
        document, _ = await self._load_version(context, version)
        rendered = document.model_dump(mode="json", by_alias=True, exclude_none=True)
        rendered["exportManifest"] = []
        for item in rendered.get("mediaManifest", []):
            if isinstance(item, dict):
                item["temporaryDownloadPath"] = (
                    f"/api/v1/classroom-versions/{version.version_id}/media/{item['mediaId']}"
                )
                item.pop("expiresAt", None)
        body = canonical_json_bytes(rendered)
        return ClassroomContent(
            body=body,
            mime_type="application/json",
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )

    async def open_media(
        self,
        context: TenantContext,
        *,
        version_id: str,
        media_id: str,
        token: str | None,
    ) -> ClassroomContent:
        version = await self._version(context, version_id)
        await self._authorize(
            context,
            version,
            token=token,
            action="classroom.media.read",
            resource_id=media_id,
        )
        document, _ = await self._load_version(context, version)
        declared = next(
            (item for item in document.media_manifest if item.media_id == media_id),
            None,
        )
        receipt = next((item for item in version.media if item.media_id == media_id), None)
        if receipt is None and declared is not None:
            receipt = next(
                (item for item in version.media if item.relative_name == declared.relative_path),
                None,
            )
        if declared is None or receipt is None:
            raise ClassroomContentNotFound("classroom media is unavailable")
        body = await self._read(context.tenant_id, receipt)
        return ClassroomContent(
            body=body,
            mime_type=receipt.mime_type,
            sha256=receipt.sha256,
            size_bytes=receipt.size_bytes,
        )

    async def open_export(
        self,
        context: TenantContext,
        *,
        export_id: str,
        token: str,
    ) -> ClassroomContent:
        export = await self._repository.get_export(context, export_id)
        if (
            export is None
            or export.tenant_id != context.tenant_id
            or export.status not in {"ready", "succeeded"}
        ):
            raise ClassroomContentNotFound("classroom export is unavailable")
        version = await self._version(context, export.classroom_version_id)
        await self._authorize(
            context,
            version,
            token=token,
            action="classroom.export.read",
            resource_id=export_id,
        )
        body = await self._read(context.tenant_id, export.receipt)
        return ClassroomContent(
            body=body,
            mime_type=export.receipt.mime_type,
            sha256=export.receipt.sha256,
            size_bytes=export.receipt.size_bytes,
            filename=PurePosixPath(export.receipt.relative_name).name,
        )


__all__ = [
    "ClassroomContent",
    "ClassroomContentAccessDenied",
    "ClassroomContentError",
    "ClassroomContentIntegrityError",
    "ClassroomContentNotFound",
    "ClassroomContentService",
    "ContentArtifactReceipt",
    "ExportContentRecord",
    "SessionContentAccess",
    "SqlAlchemyClassroomContentRepository",
    "VersionContentRecord",
]
