"""Secure validation, authorization, and storage for teaching sources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
import tempfile
from typing import Any, BinaryIO, Protocol
import uuid

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

from deeptutor.multi_user.knowledge_access import manager_for_resource
from deeptutor.multi_user.models import ADMIN_KNOWLEDGE_OWNER_ID, KnowledgeResource
from deeptutor.teaching.artifacts import StoredArtifact, source_upload_key
from deeptutor.teaching.object_store import ClassroomArtifactStore
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.repositories.sources import (
    NewKnowledgeSnapshot,
    NewUpload,
    SourceConflictError,
    SourceNotFoundError,
    SourceRecord,
    UploadRecord,
    source_binding_id,
)
from deeptutor.teaching.tenant_context import TenantContext

MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 2_000
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_OBJECTS_INSPECTED = 250_000
_FORBIDDEN_ACTION_TYPES = frozenset(
    {
        "/GoToR",
        "/ImportData",
        "/JavaScript",
        "/Launch",
        "/Movie",
        "/Rendition",
        "/RichMediaExecute",
        "/Sound",
        "/SubmitForm",
        "/URI",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {
        "/AA",
        "/AF",
        "/EmbeddedFiles",
        "/EF",
        "/JS",
        "/JavaScript",
        "/OpenAction",
        "/RichMediaContent",
        "/XFA",
    }
)
_FORBIDDEN_TYPES = frozenset({"/EmbeddedFile", "/Filespec"})
_FORBIDDEN_SUBTYPES = frozenset({"/FileAttachment", "/RichMedia", "/Screen"})


class SourceAccessDeniedError(PermissionError):
    """The current tenant principal cannot use the requested source or scope."""


class SourceUploadTooLargeError(ValueError):
    """The upload exceeds the bounded streaming limit."""


class UnsupportedSourceMediaError(ValueError):
    """The upload is not an application/pdf with PDF magic."""


class InvalidPdfSourceError(ValueError):
    """The PDF is malformed or contains unsupported active content."""


class InvalidSourceBindingError(ValueError):
    """A resolved source identity cannot be safely persisted."""


class UploadFileLike(Protocol):
    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> None: ...


class SourceRepository(Protocol):
    async def validate_target(self, course_id: str, class_id: str | None) -> None: ...

    async def is_knowledge_resource_entitled(
        self,
        resource_id: str,
        resource_owner_id: str,
    ) -> bool: ...

    async def list_bindings(
        self,
        course_ids: frozenset[str] | None,
        class_ids: frozenset[str] | None,
    ) -> tuple[SourceRecord, ...]: ...

    async def get_binding(self, binding_id: str) -> SourceRecord: ...

    async def find_upload_by_sha256(self, sha256: str) -> UploadRecord | None: ...

    async def bind_existing_upload(
        self,
        upload: UploadRecord,
        *,
        binding_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
    ) -> SourceRecord: ...

    async def create_upload_binding(
        self,
        upload: NewUpload,
        *,
        binding_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
        permission_sha256: str,
    ) -> tuple[SourceRecord, bool]: ...

    async def bind_knowledge_resource(
        self,
        snapshot: NewKnowledgeSnapshot,
        *,
        binding_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
    ) -> SourceRecord: ...

    async def delete_binding(self, binding_id: str) -> None: ...


class SourceStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> ClassroomArtifactStore: ...


@dataclass(slots=True)
class _StagedPdf:
    handle: BinaryIO
    filename: str
    sha256: str
    size_bytes: int

    def close(self) -> None:
        self.handle.close()


def _digest_id(prefix: str, *values: str) -> str:
    payload = "\0".join(values).encode()
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()}"


def _knowledge_resource_owner_id(
    context: TenantContext,
    resource: KnowledgeResource,
) -> str:
    if resource.source == "admin":
        owner_id = ADMIN_KNOWLEDGE_OWNER_ID
    elif resource.source == "user":
        owner_id = context.user_id
    else:
        raise InvalidSourceBindingError("knowledge resource owner is invalid")
    if (
        not owner_id
        or len(owner_id) > 128
        or any(character in owner_id for character in "\x00\r\n")
    ):
        raise InvalidSourceBindingError("knowledge resource owner is invalid")
    return owner_id


def _target_permission_sha256(tenant_id: str, course_id: str, class_id: str | None) -> str:
    return hashlib.sha256(
        f"{tenant_id}\0{course_id}\0{class_id or ''}\0source.use".encode()
    ).hexdigest()


def _safe_filename(value: str | None) -> str:
    name = PurePosixPath((value or "source.pdf").replace("\\", "/")).name.strip()
    if not name or any(character in name for character in "\x00\r\n"):
        return "source.pdf"
    return name[:512]


def knowledge_resource_exists(resource: KnowledgeResource) -> bool:
    """Check the resolved resource against its authoritative KB manager."""

    return resource.name in manager_for_resource(resource).list_knowledge_bases()


def _has_permission(
    context: TenantContext,
    permission: str,
    *,
    course_id: str | None,
    class_id: str | None,
) -> bool:
    resource = ResourceScope(
        tenant_id=context.tenant_id,
        course_id=course_id,
        class_id=class_id,
    )
    return any(grant.allows_resource(permission, resource) for grant in context.permissions)


def _is_admin(context: TenantContext) -> bool:
    resource = ResourceScope(tenant_id=context.tenant_id)
    return any(grant.allows_resource("tenant.manage", resource) for grant in context.permissions)


def _assert_source_target(
    context: TenantContext,
    *,
    course_id: str | None,
    class_id: str | None,
) -> None:
    if _is_admin(context):
        return
    if not _has_permission(
        context,
        "source.use",
        course_id=course_id,
        class_id=class_id,
    ):
        raise SourceAccessDeniedError("source scope access denied")


def _scopes_for_source_list(
    context: TenantContext,
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    if _is_admin(context):
        return None, None
    course_ids = {
        grant.scope_id
        for grant in context.permissions
        if grant.permission == "source.use" and grant.scope_type == "course"
    }
    class_ids = {
        grant.scope_id
        for grant in context.permissions
        if grant.permission == "source.use" and grant.scope_type == "class"
    }
    tenant_access = any(
        grant.permission == "source.use"
        and grant.scope_type == "tenant"
        and grant.scope_id == context.tenant_id
        for grant in context.permissions
    )
    if tenant_access:
        return None, None
    if not course_ids and not class_ids:
        raise SourceAccessDeniedError("source access denied")
    return frozenset(course_ids), frozenset(class_ids)


def _resolve_pdf_object(value: Any) -> Any:
    while isinstance(value, IndirectObject):
        value = value.get_object()
    return value


def _action_dictionary(value: Any) -> bool:
    try:
        resolved = _resolve_pdf_object(value)
    except Exception:
        return True
    return isinstance(resolved, DictionaryObject) and str(resolved.get("/S", "")) != ""


def _reject_active_or_embedded_content(reader: PdfReader) -> None:
    root = _resolve_pdf_object(reader.trailer["/Root"])
    if not isinstance(root, DictionaryObject):
        raise InvalidPdfSourceError("PDF catalog is invalid")
    pending: list[Any] = [root]
    seen_indirect: set[tuple[int, int]] = set()
    seen_direct: set[int] = set()
    inspected = 0
    while pending:
        item = pending.pop()
        if isinstance(item, IndirectObject):
            reference = (item.idnum, item.generation)
            if reference in seen_indirect:
                continue
            seen_indirect.add(reference)
            try:
                item = item.get_object()
            except Exception as exc:
                raise InvalidPdfSourceError("PDF contains an unreadable object") from exc
        elif isinstance(item, (DictionaryObject, ArrayObject)):
            identity = id(item)
            if identity in seen_direct:
                continue
            seen_direct.add(identity)
        else:
            continue
        inspected += 1
        if inspected > _MAX_OBJECTS_INSPECTED:
            raise InvalidPdfSourceError("PDF object graph is too complex")
        if isinstance(item, ArrayObject):
            if len(pending) + len(item) > _MAX_OBJECTS_INSPECTED:
                raise InvalidPdfSourceError("PDF object graph is too complex")
            pending.extend(item)
            continue
        item_type = str(item.get("/Type", ""))
        subtype = str(item.get("/Subtype", ""))
        action_type = str(item.get("/S", ""))
        if (
            item_type in _FORBIDDEN_TYPES
            or subtype in _FORBIDDEN_SUBTYPES
            or action_type in _FORBIDDEN_ACTION_TYPES
        ):
            raise InvalidPdfSourceError("PDF active or embedded content is not allowed")
        if len(pending) + len(item) > _MAX_OBJECTS_INSPECTED:
            raise InvalidPdfSourceError("PDF object graph is too complex")
        for key, value in item.items():
            key_name = str(key)
            if key_name in _FORBIDDEN_KEYS:
                raise InvalidPdfSourceError("PDF active or embedded content is not allowed")
            if key_name == "/A" and _action_dictionary(value):
                raise InvalidPdfSourceError("PDF actions are not allowed")
            pending.append(value)


def _validate_pdf_structure(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        reader = PdfReader(handle, strict=False)
        if reader.is_encrypted:
            raise InvalidPdfSourceError("encrypted PDFs are not allowed")
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise InvalidPdfSourceError("PDF exceeds the 2,000 page limit")
        _reject_active_or_embedded_content(reader)
    except InvalidPdfSourceError:
        raise
    except Exception as exc:
        raise InvalidPdfSourceError("PDF could not be safely parsed") from exc
    finally:
        handle.seek(0)


async def _stage_pdf(upload: UploadFileLike) -> _StagedPdf:
    if (upload.content_type or "").split(";", 1)[0].strip().lower() != "application/pdf":
        try:
            raise UnsupportedSourceMediaError("only application/pdf is accepted")
        finally:
            await upload.close()
    handle = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    digest = hashlib.sha256()
    total = 0
    magic = b""
    try:
        while chunk := await upload.read(_READ_CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise SourceUploadTooLargeError("PDF exceeds the 100 MiB limit")
            if len(magic) < 5:
                magic = (magic + chunk)[:5]
            digest.update(chunk)
            handle.write(chunk)
        if magic != b"%PDF-":
            raise UnsupportedSourceMediaError("file content is not a PDF")
        await asyncio.to_thread(_validate_pdf_structure, handle)
        return _StagedPdf(
            handle=handle,
            filename=_safe_filename(upload.filename),
            sha256=digest.hexdigest(),
            size_bytes=total,
        )
    except BaseException:
        handle.close()
        raise
    finally:
        await upload.close()


async def _file_chunks(handle: BinaryIO) -> AsyncIterator[bytes]:
    handle.seek(0)
    while chunk := await asyncio.to_thread(handle.read, _READ_CHUNK_BYTES):
        yield chunk


class SourceService:
    def __init__(
        self,
        repository: SourceRepository,
        store_provider: SourceStoreProvider,
        knowledge_resolver: Callable[..., KnowledgeResource],
        knowledge_exists: Callable[[KnowledgeResource], bool],
    ) -> None:
        self._repository = repository
        self._store_provider = store_provider
        self._knowledge_resolver = knowledge_resolver
        self._knowledge_exists = knowledge_exists

    async def list_sources(self, context: TenantContext) -> tuple[SourceRecord, ...]:
        course_ids, class_ids = _scopes_for_source_list(context)
        return await self._repository.list_bindings(course_ids, class_ids)

    async def bind_knowledge_resource(
        self,
        context: TenantContext,
        *,
        knowledge_resource_id: str,
        course_id: str,
        class_id: str | None,
    ) -> SourceRecord:
        _assert_source_target(context, course_id=course_id, class_id=class_id)
        await self._repository.validate_target(course_id, class_id)
        # resolve_kb is the authoritative current-user access check. Persist only its stable ID.
        resource = self._knowledge_resolver(knowledge_resource_id, require_write=False)
        stable_id = resource.id
        if (
            not isinstance(stable_id, str)
            or not stable_id
            or len(stable_id) > 128
            or any(character in stable_id for character in "\x00\r\n")
        ):
            raise InvalidSourceBindingError("knowledge resource identity is invalid")
        if not self._knowledge_exists(resource):
            raise SourceNotFoundError("knowledge resource not found")
        resource_owner_id = _knowledge_resource_owner_id(context, resource)
        if not await self._repository.is_knowledge_resource_entitled(
            stable_id,
            resource_owner_id,
        ):
            raise SourceAccessDeniedError("knowledge resource is not entitled to this tenant")
        content_sha256 = hashlib.sha256(f"{resource_owner_id}\0{stable_id}".encode()).hexdigest()
        snapshot_id = _digest_id(
            "kb-source",
            context.tenant_id,
            resource_owner_id,
            stable_id,
            "binding-v1",
        )
        binding_id = source_binding_id(
            context.tenant_id,
            snapshot_id,
            course_id,
            class_id,
        )
        permission_sha256 = hashlib.sha256(
            f"{context.tenant_id}\0{resource_owner_id}\0{stable_id}\0source.use".encode()
        ).hexdigest()
        return await self._repository.bind_knowledge_resource(
            NewKnowledgeSnapshot(
                snapshot_id=snapshot_id,
                resource_id=stable_id,
                resource_owner_id=resource_owner_id,
                revision="binding-v1",
                content_sha256=content_sha256,
                permission_sha256=permission_sha256,
            ),
            binding_id=binding_id,
            course_id=course_id,
            class_id=class_id,
            actor_id=context.user_id,
        )

    async def upload_pdf(
        self,
        context: TenantContext,
        *,
        upload: UploadFileLike,
        course_id: str,
        class_id: str | None,
    ) -> SourceRecord:
        try:
            _assert_source_target(context, course_id=course_id, class_id=class_id)
            await self._repository.validate_target(course_id, class_id)
        except BaseException:
            await upload.close()
            raise
        staged = await _stage_pdf(upload)
        try:
            existing = await self._repository.find_upload_by_sha256(staged.sha256)
            if existing is not None:
                binding_id = source_binding_id(
                    context.tenant_id,
                    existing.snapshot_id,
                    course_id,
                    class_id,
                )
                return await self._repository.bind_existing_upload(
                    existing,
                    binding_id=binding_id,
                    course_id=course_id,
                    class_id=class_id,
                    actor_id=context.user_id,
                )

            upload_id = f"upload-{uuid.uuid4().hex}"
            snapshot_id = f"pdf-source-{uuid.uuid4().hex}"
            object_key = source_upload_key(context.tenant_id, upload_id)
            store = await self._store_provider.store_for_tenant(context.tenant_id)
            artifact = await store.put_verified(
                object_key,
                _file_chunks(staged.handle),
                staged.sha256,
                staged.size_bytes,
                content_type="application/pdf",
            )
            try:
                binding_id = source_binding_id(
                    context.tenant_id,
                    snapshot_id,
                    course_id,
                    class_id,
                )
                record, retained = await self._repository.create_upload_binding(
                    NewUpload(
                        upload_id=upload_id,
                        snapshot_id=snapshot_id,
                        filename=staged.filename,
                        object_key=artifact.key,
                        sha256=staged.sha256,
                        size_bytes=staged.size_bytes,
                    ),
                    binding_id=binding_id,
                    course_id=course_id,
                    class_id=class_id,
                    actor_id=context.user_id,
                    permission_sha256=_target_permission_sha256(
                        context.tenant_id,
                        course_id,
                        class_id,
                    ),
                )
                if not retained:
                    await asyncio.shield(_cleanup_owned(store, artifact))
                return record
            except (SourceConflictError, SourceNotFoundError):
                await asyncio.shield(_cleanup_owned(store, artifact))
                raise
        finally:
            staged.close()

    async def delete_source(self, context: TenantContext, *, binding_id: str) -> None:
        record = await self._repository.get_binding(binding_id)
        _assert_source_target(
            context,
            course_id=record.course_id,
            class_id=record.class_id,
        )
        await self._repository.delete_binding(binding_id)


async def _cleanup_owned(store: ClassroomArtifactStore, artifact: StoredArtifact) -> None:
    try:
        await store.delete_owned(artifact)
    except Exception:
        # The original domain/storage failure is authoritative; ownership-bound cleanup is best effort.
        return


__all__ = [
    "InvalidPdfSourceError",
    "InvalidSourceBindingError",
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "SourceAccessDeniedError",
    "SourceService",
    "SourceUploadTooLargeError",
    "UnsupportedSourceMediaError",
    "knowledge_resource_exists",
]
