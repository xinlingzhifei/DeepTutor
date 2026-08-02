"""Tenant-scoped source upload and binding persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.artifacts import source_upload_key
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
    snapshot_id: str
    filename: str
    object_key: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class NewUpload:
    upload_id: str
    snapshot_id: str
    filename: str
    object_key: str
    sha256: str
    size_bytes: int


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
                SourceUpload.filename,
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
                    SourceUpload.source_snapshot_id == SourceSnapshot.id,
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
                SourceUpload.filename,
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
                    SourceUpload.source_snapshot_id == SourceSnapshot.id,
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

    async def find_upload_by_sha256(self, sha256: str) -> UploadRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        SourceUpload.id,
                        SourceUpload.source_snapshot_id,
                        SourceUpload.filename,
                        SourceUpload.object_key,
                        SourceUpload.sha256,
                        SourceUpload.size_bytes,
                    ).where(
                        SourceUpload.tenant_id == self._tenant_id,
                        SourceUpload.sha256 == sha256,
                        SourceUpload.status == "uploaded",
                    )
                )
            ).first()
            if row is None or row.source_snapshot_id is None:
                return None
            record = UploadRecord(*row)
            _require_upload_key(self._tenant_id, record.upload_id, record.object_key)
            return record

    async def bind_existing_upload(
        self,
        upload: UploadRecord,
        *,
        binding_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
    ) -> SourceRecord:
        _require_upload_key(self._tenant_id, upload.upload_id, upload.object_key)
        expected_binding_id = source_binding_id(
            self._tenant_id,
            upload.snapshot_id,
            course_id,
            class_id,
        )
        if binding_id != expected_binding_id:
            raise SourceConflictError("source binding identity conflict")
        async with self._session_factory() as session:
            async with session.begin():
                await self._ensure_binding(
                    session,
                    binding_id=binding_id,
                    snapshot_id=upload.snapshot_id,
                    course_id=course_id,
                    class_id=class_id,
                    actor_id=actor_id,
                )
        return await self._source_record(binding_id)

    async def create_upload_binding(
        self,
        upload: NewUpload,
        *,
        binding_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
        permission_sha256: str,
    ) -> tuple[SourceRecord, bool]:
        """Create a deduplicated upload and return whether its new object was retained."""

        retained = False
        _require_upload_key(self._tenant_id, upload.upload_id, upload.object_key)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": _lock_key(self._tenant_id, upload.sha256)},
                    )
                    existing_row = (
                        await session.execute(
                            select(
                                SourceUpload.id,
                                SourceUpload.source_snapshot_id,
                                SourceUpload.filename,
                                SourceUpload.object_key,
                                SourceUpload.sha256,
                                SourceUpload.size_bytes,
                            ).where(
                                SourceUpload.tenant_id == self._tenant_id,
                                SourceUpload.sha256 == upload.sha256,
                                SourceUpload.status == "uploaded",
                            )
                        )
                    ).first()
                    if existing_row is None:
                        snapshot = SourceSnapshot(
                            id=upload.snapshot_id,
                            tenant_id=self._tenant_id,
                            source_type="pdf",
                            source_id=upload.upload_id,
                            resource_owner_id=TENANT_SOURCE_OWNER_ID,
                            source_revision=upload.sha256,
                            content_sha256=upload.sha256,
                            permission_sha256=permission_sha256,
                            citation_manifest="[]",
                            created_by=actor_id,
                        )
                        session.add(snapshot)
                        session.add(
                            SourceUpload(
                                id=upload.upload_id,
                                tenant_id=self._tenant_id,
                                source_snapshot_id=upload.snapshot_id,
                                uploaded_by=actor_id,
                                filename=upload.filename,
                                object_key=upload.object_key,
                                sha256=upload.sha256,
                                size_bytes=upload.size_bytes,
                            )
                        )
                        retained = True
                        snapshot_id = upload.snapshot_id
                    else:
                        _require_upload_key(
                            self._tenant_id,
                            existing_row.id,
                            existing_row.object_key,
                        )
                        snapshot_id = existing_row.source_snapshot_id
                        if snapshot_id is None:
                            raise SourceConflictError("existing upload is incomplete")
                        binding_id = source_binding_id(
                            self._tenant_id,
                            snapshot_id,
                            course_id,
                            class_id,
                        )
                    await self._ensure_binding(
                        session,
                        binding_id=binding_id,
                        snapshot_id=snapshot_id,
                        course_id=course_id,
                        class_id=class_id,
                        actor_id=actor_id,
                    )
                    await session.flush()
                    record = await self._source_record_in_session(session, binding_id)
        except IntegrityError as exc:
            raise SourceConflictError("source upload conflicts with existing state") from exc
        return record, retained

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
    "NewUpload",
    "SourceConflictError",
    "SourceEntitlementDeniedError",
    "SourceNotFoundError",
    "SourceRecord",
    "SqlAlchemySourceRepository",
    "UploadRecord",
    "source_binding_id",
]
