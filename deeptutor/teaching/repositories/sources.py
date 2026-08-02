"""Tenant-scoped source upload and binding persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.artifacts import StoredArtifact, source_upload_key
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.models.classrooms import (
    SourceSnapshot,
    SourceUpload,
    TenantSourceBinding,
)
from deeptutor.teaching.models.platform import Tenant, TenantKnowledgeEntitlement
from deeptutor.teaching.models.tenant import Course, TeachingClass
from deeptutor.teaching.schema_names import tenant_schema_name

TENANT_SOURCE_OWNER_ID = "tenant-workspace"


class SourceRepositoryError(RuntimeError):
    """Base class for safe source persistence errors."""


class SourceNotFoundError(SourceRepositoryError):
    """A source or target scope does not exist in this tenant."""


class SourceConflictError(SourceRepositoryError):
    """A source write conflicts with existing tenant state."""


class SourceEntitlementDeniedError(SourceRepositoryError):
    """The tenant has no active entitlement for a knowledge resource."""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    binding_id: str
    source_type: str
    source_id: str
    filename: str | None
    sha256: str
    size_bytes: int | None
    course_id: str | None
    class_id: str | None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UploadRecord:
    upload_id: str
    object_key: str
    sha256: str
    size_bytes: int
    status: str
    ownership_token: str
    object_revision: str | None
    object_version_id: str | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class NewUploadReceipt:
    upload_id: str
    object_key: str
    sha256: str
    size_bytes: int
    uploaded_by: str
    ownership_token: str


@dataclass(frozen=True, slots=True)
class NewPdfSnapshot:
    snapshot_id: str
    upload_id: str
    display_name: str
    permission_sha256: str


@dataclass(frozen=True, slots=True)
class NewKnowledgeSnapshot:
    snapshot_id: str
    resource_id: str
    resource_owner_id: str
    revision: str
    content_sha256: str
    permission_sha256: str


def _lock_key(tenant_id: str, digest: str) -> int:
    raw = hashlib.sha256(f"source-upload\0{tenant_id}\0{digest}".encode()).digest()[:8]
    return int.from_bytes(raw, "big", signed=True)


def source_binding_id(
    tenant_id: str,
    snapshot_id: str,
    course_id: str,
    class_id: str | None,
) -> str:
    payload = "\0".join((tenant_id, snapshot_id, course_id, class_id or "")).encode()
    return f"source-binding-{hashlib.sha256(payload).hexdigest()}"


def _pdf_source_revision(content_sha256: str, display_name: str) -> str:
    payload = "\0".join(("pdf-name-v1", content_sha256, display_name)).encode()
    return hashlib.sha256(payload).hexdigest()


def _require_upload_key(tenant_id: str, upload_id: str, object_key: str) -> None:
    try:
        expected = source_upload_key(tenant_id, upload_id)
    except ValueError as exc:
        raise SourceConflictError("source upload identity is invalid") from exc
    if object_key != expected:
        raise SourceConflictError("source upload object is outside the tenant")


class SqlAlchemySourceRepository:
    """Source repository whose connection can address only one tenant schema."""

    def __init__(self, tenant_id: str, engine: AsyncEngine | None = None) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        translated = (engine or get_platform_engine()).execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(translated, expire_on_commit=False)

    async def _validate_target(
        self,
        session: AsyncSession,
        *,
        course_id: str,
        class_id: str | None,
    ) -> None:
        course = await session.scalar(
            select(Course.id).where(Course.id == course_id, Course.status == "active")
        )
        if course is None:
            raise SourceNotFoundError("course not found")
        if class_id is not None:
            teaching_class = await session.scalar(
                select(TeachingClass.id).where(
                    TeachingClass.id == class_id,
                    TeachingClass.course_id == course_id,
                    TeachingClass.status == "active",
                )
            )
            if teaching_class is None:
                raise SourceNotFoundError("class not found in course")

    async def validate_target(self, course_id: str, class_id: str | None) -> None:
        """Validate trusted course/class ancestry inside the selected tenant."""

        async with self._session_factory() as session:
            await self._validate_target(
                session,
                course_id=course_id,
                class_id=class_id,
            )

    async def is_knowledge_resource_entitled(
        self,
        resource_id: str,
        resource_owner_id: str,
    ) -> bool:
        async with self._session_factory() as session:
            entitled = await session.scalar(
                select(TenantKnowledgeEntitlement.knowledge_resource_id)
                .join(Tenant, Tenant.id == TenantKnowledgeEntitlement.tenant_id)
                .where(
                    TenantKnowledgeEntitlement.tenant_id == self._tenant_id,
                    TenantKnowledgeEntitlement.knowledge_resource_id == resource_id,
                    TenantKnowledgeEntitlement.resource_owner_id == resource_owner_id,
                    TenantKnowledgeEntitlement.status == "active",
                    Tenant.status == "active",
                )
            )
            return entitled is not None

    async def _lock_knowledge_entitlement(
        self,
        session: AsyncSession,
        *,
        resource_id: str,
        resource_owner_id: str,
    ) -> None:
        tenant = await session.scalar(
            select(Tenant.id)
            .where(
                Tenant.id == self._tenant_id,
                Tenant.status == "active",
            )
            .with_for_update(read=True)
        )
        if tenant is None:
            raise SourceEntitlementDeniedError(
                "knowledge resource is not entitled to this tenant"
            )
        entitlement = await session.scalar(
            select(TenantKnowledgeEntitlement.knowledge_resource_id)
            .where(
                TenantKnowledgeEntitlement.tenant_id == self._tenant_id,
                TenantKnowledgeEntitlement.knowledge_resource_id == resource_id,
                TenantKnowledgeEntitlement.resource_owner_id == resource_owner_id,
                TenantKnowledgeEntitlement.status == "active",
            )
            .with_for_update(read=True)
        )
        if entitlement is None:
            raise SourceEntitlementDeniedError(
                "knowledge resource is not entitled to this tenant"
            )

    async def _ensure_binding(
        self,
        session: AsyncSession,
        *,
        binding_id: str,
        snapshot_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
    ) -> None:
        await self._validate_target(session, course_id=course_id, class_id=class_id)
        await session.execute(
            insert(TenantSourceBinding)
            .values(
                id=binding_id,
                tenant_id=self._tenant_id,
                source_snapshot_id=snapshot_id,
                course_id=course_id,
                class_id=class_id,
                bound_by=actor_id,
            )
            .on_conflict_do_nothing(index_elements=[TenantSourceBinding.id])
        )
        existing = await session.get(TenantSourceBinding, binding_id)
        if existing is None:
            raise SourceConflictError("source binding could not be created")
        if (
            existing.tenant_id != self._tenant_id
            or existing.source_snapshot_id != snapshot_id
            or existing.course_id != course_id
            or existing.class_id != class_id
        ):
            raise SourceConflictError("source binding identity conflict")

    def _source_record_statement(self, binding_id: str):
        return (
            select(
                TenantSourceBinding.id,
                SourceSnapshot.source_type,
                SourceSnapshot.source_id,
                SourceSnapshot.display_name,
                SourceSnapshot.content_sha256,
                SourceUpload.size_bytes,
                TenantSourceBinding.course_id,
                TenantSourceBinding.class_id,
                TenantSourceBinding.created_at,
            )
            .join(
                SourceSnapshot,
                SourceSnapshot.id == TenantSourceBinding.source_snapshot_id,
            )
            .outerjoin(
                SourceUpload,
                and_(
                    SourceUpload.id == SourceSnapshot.source_upload_id,
                    SourceUpload.tenant_id == SourceSnapshot.tenant_id,
                    SourceUpload.tenant_id == self._tenant_id,
                ),
            )
            .where(
                TenantSourceBinding.id == binding_id,
                TenantSourceBinding.tenant_id == self._tenant_id,
                SourceSnapshot.tenant_id == self._tenant_id,
            )
        )

    async def _source_record_in_session(
        self,
        session: AsyncSession,
        binding_id: str,
    ) -> SourceRecord:
        row = (await session.execute(self._source_record_statement(binding_id))).one_or_none()
        if row is None:
            raise SourceNotFoundError("source binding not found")
        return SourceRecord(*row)

    async def _source_record(self, binding_id: str) -> SourceRecord:
        async with self._session_factory() as session:
            return await self._source_record_in_session(session, binding_id)

    async def list_bindings(
        self,
        course_ids: frozenset[str] | None,
        class_ids: frozenset[str] | None,
    ) -> tuple[SourceRecord, ...]:
        if course_ids == frozenset() and class_ids == frozenset():
            return ()
        statement = (
            select(
                TenantSourceBinding.id,
                SourceSnapshot.source_type,
                SourceSnapshot.source_id,
                SourceSnapshot.display_name,
                SourceSnapshot.content_sha256,
                SourceUpload.size_bytes,
                TenantSourceBinding.course_id,
                TenantSourceBinding.class_id,
                TenantSourceBinding.created_at,
            )
            .join(
                SourceSnapshot,
                SourceSnapshot.id == TenantSourceBinding.source_snapshot_id,
            )
            .outerjoin(
                SourceUpload,
                and_(
                    SourceUpload.id == SourceSnapshot.source_upload_id,
                    SourceUpload.tenant_id == SourceSnapshot.tenant_id,
                    SourceUpload.tenant_id == self._tenant_id,
                ),
            )
            .where(
                TenantSourceBinding.tenant_id == self._tenant_id,
                SourceSnapshot.tenant_id == self._tenant_id,
            )
        )
        if course_ids is not None or class_ids is not None:
            predicates = []
            if course_ids:
                predicates.append(TenantSourceBinding.course_id.in_(course_ids))
            if class_ids:
                predicates.append(TenantSourceBinding.class_id.in_(class_ids))
            if not predicates:
                return ()
            statement = statement.where(or_(*predicates))
        async with self._session_factory() as session:
            rows = await session.execute(
                statement.order_by(TenantSourceBinding.created_at, TenantSourceBinding.id)
            )
            return tuple(SourceRecord(*row) for row in rows)

    async def get_binding(self, binding_id: str) -> SourceRecord:
        return await self._source_record(binding_id)

    @staticmethod
    def _upload_record(row) -> UploadRecord:
        return UploadRecord(
            upload_id=row.id,
            object_key=row.object_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            status=row.status,
            ownership_token=row.ownership_token,
            object_revision=row.object_revision,
            object_version_id=row.object_version_id,
            last_error_code=row.last_error_code,
        )

    async def find_upload_by_sha256(self, sha256: str) -> UploadRecord | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SourceUpload).where(
                    SourceUpload.tenant_id == self._tenant_id,
                    SourceUpload.sha256 == sha256,
                )
            )
            if row is None:
                return None
            record = self._upload_record(row)
            _require_upload_key(self._tenant_id, record.upload_id, record.object_key)
            return record

    async def reserve_upload(
        self,
        upload: NewUploadReceipt,
    ) -> UploadRecord:
        _require_upload_key(self._tenant_id, upload.upload_id, upload.object_key)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": _lock_key(self._tenant_id, upload.sha256)},
                    )
                    existing = await session.scalar(
                        select(SourceUpload)
                        .where(
                            SourceUpload.tenant_id == self._tenant_id,
                            SourceUpload.sha256 == upload.sha256,
                        )
                        .with_for_update()
                    )
                    if existing is None:
                        existing = SourceUpload(
                            id=upload.upload_id,
                            tenant_id=self._tenant_id,
                            uploaded_by=upload.uploaded_by,
                            object_key=upload.object_key,
                            sha256=upload.sha256,
                            size_bytes=upload.size_bytes,
                            status="writing",
                            ownership_token=upload.ownership_token,
                        )
                        session.add(existing)
                    await session.flush()
                    if (
                        existing.id != upload.upload_id
                        or existing.tenant_id != self._tenant_id
                        or existing.object_key != upload.object_key
                        or existing.sha256 != upload.sha256
                        or existing.size_bytes != upload.size_bytes
                    ):
                        raise SourceConflictError("source upload identity conflict")
                    record = self._upload_record(existing)
        except IntegrityError as exc:
            raise SourceConflictError("source upload receipt conflicts with existing state") from exc
        return record

    async def complete_upload(
        self,
        upload_id: str,
        artifact: StoredArtifact,
    ) -> UploadRecord:
        if artifact.content_type != "application/pdf" or artifact.revision is None:
            raise SourceConflictError("source upload artifact is incomplete")
        async with self._session_factory() as session:
            async with session.begin():
                upload = await session.scalar(
                    select(SourceUpload)
                    .where(
                        SourceUpload.id == upload_id,
                        SourceUpload.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if upload is None:
                    raise SourceNotFoundError("source upload receipt not found")
                if (
                    upload.object_key != artifact.key
                    or upload.sha256 != artifact.sha256
                    or upload.size_bytes != artifact.size
                    or upload.ownership_token != artifact.ownership_token
                ):
                    raise SourceConflictError("source upload artifact conflicts with receipt")
                upload.status = "uploaded"
                upload.object_revision = artifact.revision
                upload.object_version_id = artifact.version_id
                upload.last_error_code = None
                upload.updated_at = func.now()
                await session.flush()
                record = self._upload_record(upload)
        return record

    async def mark_upload_failed(
        self,
        upload_id: str,
        error_code: str,
        *,
        cleanup_pending: bool = False,
    ) -> None:
        if (
            not error_code
            or len(error_code) > 64
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in error_code)
        ):
            raise ValueError("upload error code is invalid")
        async with self._session_factory() as session:
            async with session.begin():
                upload = await session.scalar(
                    select(SourceUpload)
                    .where(
                        SourceUpload.id == upload_id,
                        SourceUpload.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if upload is None:
                    raise SourceNotFoundError("source upload receipt not found")
                if upload.status != "uploaded":
                    upload.status = "cleanup_pending" if cleanup_pending else "failed"
                    upload.last_error_code = error_code
                    upload.updated_at = func.now()

    async def list_reconcilable_uploads(self, limit: int) -> tuple[UploadRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("reconciliation limit is invalid")
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(SourceUpload)
                .where(
                    SourceUpload.tenant_id == self._tenant_id,
                    SourceUpload.status.in_(("writing", "cleanup_pending", "failed")),
                )
                .order_by(SourceUpload.updated_at, SourceUpload.id)
                .limit(limit)
            )
            return tuple(self._upload_record(row) for row in rows)

    async def delete_reconciled_upload(self, upload_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                referenced = await session.scalar(
                    select(SourceSnapshot.id).where(
                        SourceSnapshot.tenant_id == self._tenant_id,
                        SourceSnapshot.source_upload_id == upload_id,
                    )
                )
                if referenced is not None:
                    raise SourceConflictError("source upload is still referenced")
                await session.execute(
                    delete(SourceUpload).where(
                        SourceUpload.id == upload_id,
                        SourceUpload.tenant_id == self._tenant_id,
                        SourceUpload.status.in_(("cleanup_pending", "failed")),
                    )
                )

    async def bind_uploaded_pdf(
        self,
        upload: UploadRecord,
        snapshot: NewPdfSnapshot,
        *,
        binding_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
    ) -> SourceRecord:
        _require_upload_key(self._tenant_id, upload.upload_id, upload.object_key)
        source_revision = _pdf_source_revision(upload.sha256, snapshot.display_name)
        expected_binding_id = source_binding_id(
            self._tenant_id,
            snapshot.snapshot_id,
            course_id,
            class_id,
        )
        if binding_id != expected_binding_id or snapshot.upload_id != upload.upload_id:
            raise SourceConflictError("source binding identity conflict")
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    stored_upload = await session.scalar(
                        select(SourceUpload)
                        .where(
                            SourceUpload.id == upload.upload_id,
                            SourceUpload.tenant_id == self._tenant_id,
                            SourceUpload.status == "uploaded",
                        )
                        .with_for_update(read=True)
                    )
                    if stored_upload is None or (
                        stored_upload.object_key != upload.object_key
                        or stored_upload.sha256 != upload.sha256
                        or stored_upload.size_bytes != upload.size_bytes
                        or stored_upload.ownership_token != upload.ownership_token
                        or stored_upload.object_revision != upload.object_revision
                        or stored_upload.object_version_id != upload.object_version_id
                    ):
                        raise SourceConflictError("source upload is not complete")
                    await session.execute(
                        insert(SourceSnapshot)
                        .values(
                            id=snapshot.snapshot_id,
                            tenant_id=self._tenant_id,
                            source_type="pdf",
                            source_id=upload.upload_id,
                            resource_owner_id=TENANT_SOURCE_OWNER_ID,
                            source_upload_id=upload.upload_id,
                            display_name=snapshot.display_name,
                            source_revision=source_revision,
                            content_sha256=upload.sha256,
                            permission_sha256=snapshot.permission_sha256,
                            citation_manifest="[]",
                            created_by=actor_id,
                        )
                        .on_conflict_do_nothing(index_elements=[SourceSnapshot.id])
                    )
                    existing = await session.get(SourceSnapshot, snapshot.snapshot_id)
                    if existing is None or (
                        existing.tenant_id != self._tenant_id
                        or existing.source_type != "pdf"
                        or existing.source_id != upload.upload_id
                        or existing.resource_owner_id != TENANT_SOURCE_OWNER_ID
                        or existing.source_upload_id != upload.upload_id
                        or existing.display_name != snapshot.display_name
                        or existing.source_revision != source_revision
                        or existing.content_sha256 != upload.sha256
                        or existing.permission_sha256 != snapshot.permission_sha256
                        or existing.citation_manifest != "[]"
                    ):
                        raise SourceConflictError("PDF source identity conflict")
                    await self._ensure_binding(
                        session,
                        binding_id=binding_id,
                        snapshot_id=snapshot.snapshot_id,
                        course_id=course_id,
                        class_id=class_id,
                        actor_id=actor_id,
                    )
                    await session.flush()
                    record = await self._source_record_in_session(session, binding_id)
        except IntegrityError as exc:
            raise SourceConflictError("PDF source conflicts with existing state") from exc
        return record

    async def bind_knowledge_resource(
        self,
        snapshot: NewKnowledgeSnapshot,
        *,
        binding_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
    ) -> SourceRecord:
        expected_binding_id = source_binding_id(
            self._tenant_id,
            snapshot.snapshot_id,
            course_id,
            class_id,
        )
        if binding_id != expected_binding_id:
            raise SourceConflictError("source binding identity conflict")
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._lock_knowledge_entitlement(
                        session,
                        resource_id=snapshot.resource_id,
                        resource_owner_id=snapshot.resource_owner_id,
                    )
                    await session.execute(
                        insert(SourceSnapshot)
                        .values(
                            id=snapshot.snapshot_id,
                            tenant_id=self._tenant_id,
                            source_type="knowledge_base",
                            source_id=snapshot.resource_id,
                            resource_owner_id=snapshot.resource_owner_id,
                            source_revision=snapshot.revision,
                            content_sha256=snapshot.content_sha256,
                            permission_sha256=snapshot.permission_sha256,
                            citation_manifest="[]",
                            created_by=actor_id,
                        )
                        .on_conflict_do_nothing(index_elements=[SourceSnapshot.id])
                    )
                    existing = await session.get(SourceSnapshot, snapshot.snapshot_id)
                    if existing is None or (
                        existing.tenant_id != self._tenant_id
                        or existing.source_type != "knowledge_base"
                        or existing.source_id != snapshot.resource_id
                        or existing.resource_owner_id != snapshot.resource_owner_id
                        or existing.source_revision != snapshot.revision
                        or existing.content_sha256 != snapshot.content_sha256
                        or existing.permission_sha256 != snapshot.permission_sha256
                        or existing.citation_manifest != "[]"
                    ):
                        raise SourceConflictError("knowledge source identity conflict")
                    await self._ensure_binding(
                        session,
                        binding_id=binding_id,
                        snapshot_id=snapshot.snapshot_id,
                        course_id=course_id,
                        class_id=class_id,
                        actor_id=actor_id,
                    )
        except IntegrityError as exc:
            raise SourceConflictError("knowledge source conflicts with existing state") from exc
        return await self._source_record(binding_id)

    async def delete_binding(self, binding_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    delete(TenantSourceBinding).where(
                        TenantSourceBinding.id == binding_id,
                        TenantSourceBinding.tenant_id == self._tenant_id,
                    )
                )
                if result.rowcount != 1:
                    raise SourceNotFoundError("source binding not found")


__all__ = [
    "NewKnowledgeSnapshot",
    "NewPdfSnapshot",
    "NewUploadReceipt",
    "SourceConflictError",
    "SourceEntitlementDeniedError",
    "SourceNotFoundError",
    "SourceRecord",
    "SqlAlchemySourceRepository",
    "UploadRecord",
    "source_binding_id",
]
