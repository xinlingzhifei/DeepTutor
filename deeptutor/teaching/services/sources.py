"""Secure validation, authorization, and storage for teaching sources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
import hashlib
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import tempfile
import threading
from typing import Any, BinaryIO, Protocol
from urllib.parse import urlparse
import uuid

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

from deeptutor.knowledge.kb_types import (
    LIGHTRAG_SERVER_KB_TYPE,
    LINKED_KB_TYPE,
    OBSIDIAN_KB_TYPE,
    SUBAGENT_KB_TYPE,
    external_root_of,
)
from deeptutor.multi_user.knowledge_access import manager_for_resource
from deeptutor.multi_user.models import ADMIN_KNOWLEDGE_OWNER_ID, KnowledgeResource
from deeptutor.teaching.artifacts import StoredArtifact, source_upload_key
from deeptutor.teaching.object_store import ClassroomArtifactStore
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.repositories.sources import (
    NewKnowledgeSnapshot,
    NewPdfSnapshot,
    NewUploadReceipt,
    SourceEntitlementDeniedError,
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
_PDF_INSPECTION_TIMEOUT_SECONDS = 30.0
_PDF_INSPECTION_MEMORY_BYTES = 384 * 1024 * 1024
_PDF_INSPECTION_SLOTS = threading.BoundedSemaphore(value=2)
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
        "/Movie",
        "/Sound",
        "/3D",
        "/3DA",
        "/3DD",
        "/JS",
        "/JavaScript",
        "/OpenAction",
        "/RichMediaContent",
        "/XFA",
    }
)
_FORBIDDEN_TYPES = frozenset({"/EmbeddedFile", "/Filespec"})
_FORBIDDEN_SUBTYPES = frozenset(
    {"/3D", "/FileAttachment", "/Movie", "/RichMedia", "/Screen", "/Sound"}
)


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

    async def list_bindings(
        self,
        course_ids: frozenset[str] | None,
        class_ids: frozenset[str] | None,
    ) -> tuple[SourceRecord, ...]: ...

    async def get_binding(self, binding_id: str) -> SourceRecord: ...

    async def find_upload_by_sha256(self, sha256: str) -> UploadRecord | None: ...

    async def reserve_upload(
        self,
        upload: NewUploadReceipt,
    ) -> UploadRecord: ...

    async def complete_upload(
        self,
        upload_id: str,
        artifact: StoredArtifact,
    ) -> UploadRecord: ...

    async def mark_upload_failed(
        self,
        upload_id: str,
        error_code: str,
        *,
        cleanup_pending: bool = False,
    ) -> None: ...

    async def list_reconcilable_uploads(self, limit: int) -> tuple[UploadRecord, ...]: ...

    async def delete_reconciled_upload(self, upload_id: str) -> None: ...

    async def bind_uploaded_pdf(
        self,
        upload: UploadRecord,
        snapshot: NewPdfSnapshot,
        *,
        binding_id: str,
        course_id: str,
        class_id: str | None,
        actor_id: str,
    ) -> SourceRecord: ...

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
    path: str
    filename: str
    sha256: str
    size_bytes: int

    def close(self) -> None:
        self.handle.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


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


def _has_stable_knowledge_generation(resource: KnowledgeResource) -> bool:
    generation = resource.generation_id
    if not isinstance(generation, str):
        return False
    try:
        canonical_generation = str(uuid.UUID(generation))
    except (ValueError, AttributeError):
        return False
    return (
        generation == canonical_generation
        and resource.source in {"admin", "user"}
        and resource.id == f"{resource.source}:kb:{generation}"
    )


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

    if not _has_stable_knowledge_generation(resource):
        return False
    generation = resource.generation_id
    manager = manager_for_resource(resource)
    entry = manager.get_kb_entry(resource.name)
    if entry is None or entry.get("generation_id") != generation:
        return False

    kb_type = entry.get("type")
    if kb_type in {LINKED_KB_TYPE, OBSIDIAN_KB_TYPE}:
        external = external_root_of(entry)
        return bool(external and Path(external).expanduser().is_dir())
    if kb_type == SUBAGENT_KB_TYPE:
        agent_kind = str(entry.get("agent_kind") or "").strip()
        if not agent_kind:
            return False
        if agent_kind == "partner":
            return bool(str(entry.get("partner_id") or "").strip())
        cwd = str(entry.get("cwd") or "").strip()
        return not cwd or Path(cwd).expanduser().is_dir()
    if kb_type == LIGHTRAG_SERVER_KB_TYPE:
        parsed = urlparse(str(entry.get("server_url") or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    if kb_type:
        return False

    relative = Path(str(entry.get("path") or resource.name))
    if relative.is_absolute():
        return False
    base_dir = manager.base_dir.resolve()
    candidate = (base_dir / relative).resolve()
    try:
        candidate.relative_to(base_dir)
    except ValueError:
        return False
    return candidate.is_dir()


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


def _bounded_page_count(reader: PdfReader, max_pages: int) -> int:
    root = _resolve_pdf_object(reader.trailer["/Root"])
    if not isinstance(root, DictionaryObject):
        raise InvalidPdfSourceError("PDF catalog is invalid")
    pages = root.get("/Pages")
    if pages is None:
        raise InvalidPdfSourceError("PDF page tree is missing")
    pending: list[Any] = [pages]
    seen_indirect: set[tuple[int, int]] = set()
    seen_direct: set[int] = set()
    page_count = 0
    inspected = 0
    while pending:
        item = pending.pop()
        if isinstance(item, IndirectObject):
            reference = (item.idnum, item.generation)
            if reference in seen_indirect:
                raise InvalidPdfSourceError("PDF page tree is cyclic")
            seen_indirect.add(reference)
            item = _resolve_pdf_object(item)
        if not isinstance(item, DictionaryObject):
            raise InvalidPdfSourceError("PDF page tree is invalid")
        identity = id(item)
        if identity in seen_direct:
            raise InvalidPdfSourceError("PDF page tree is cyclic")
        seen_direct.add(identity)
        inspected += 1
        if inspected > _MAX_OBJECTS_INSPECTED:
            raise InvalidPdfSourceError("PDF page tree is too complex")
        item_type = str(item.get("/Type", ""))
        kids = item.get("/Kids")
        if item_type == "/Page" or kids is None:
            page_count += 1
            if page_count > max_pages:
                raise InvalidPdfSourceError("PDF exceeds the 2,000 page limit")
            continue
        if item_type not in {"", "/Pages"}:
            raise InvalidPdfSourceError("PDF page tree is invalid")
        declared_count = item.get("/Count")
        if not isinstance(declared_count, int) or declared_count < 0:
            raise InvalidPdfSourceError("PDF page count is invalid")
        if declared_count > max_pages:
            raise InvalidPdfSourceError("PDF exceeds the 2,000 page limit")
        children = _resolve_pdf_object(kids)
        if not isinstance(children, ArrayObject):
            raise InvalidPdfSourceError("PDF page tree is invalid")
        if len(pending) + len(children) > _MAX_OBJECTS_INSPECTED:
            raise InvalidPdfSourceError("PDF page tree is too complex")
        pending.extend(children)
    return page_count


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


def _validate_pdf_structure(path: str, max_pages: int, max_objects: int) -> None:
    global _MAX_OBJECTS_INSPECTED
    previous_max_objects = _MAX_OBJECTS_INSPECTED
    _MAX_OBJECTS_INSPECTED = max_objects
    try:
        with open(path, "rb") as handle:
            reader = PdfReader(handle, strict=False)
            if reader.is_encrypted:
                raise InvalidPdfSourceError("encrypted PDFs are not allowed")
            _bounded_page_count(reader, max_pages)
            _reject_active_or_embedded_content(reader)
    except InvalidPdfSourceError:
        raise
    except Exception as exc:
        raise InvalidPdfSourceError("PDF could not be safely parsed") from exc
    finally:
        _MAX_OBJECTS_INSPECTED = previous_max_objects


_WINDOWS_PDF_JOB_HANDLE: int | None = None


def _apply_windows_pdf_memory_limit(limit_bytes: int) -> None:
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "could not create PDF inspection job")
    information = _ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00000100
    information.ProcessMemoryLimit = limit_bytes
    if not kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ) or not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "could not constrain PDF inspection memory")
    global _WINDOWS_PDF_JOB_HANDLE
    _WINDOWS_PDF_JOB_HANDLE = int(job)


def _apply_pdf_worker_memory_limit(limit_bytes: int) -> None:
    if os.name == "nt":
        _apply_windows_pdf_memory_limit(limit_bytes)
        return
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    del soft
    bounded = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
    resource.setrlimit(resource.RLIMIT_AS, (bounded, bounded))


def _pdf_inspection_worker(
    path: str,
    connection,
    max_pages: int,
    max_objects: int,
    memory_limit_bytes: int,
) -> None:
    try:
        _apply_pdf_worker_memory_limit(memory_limit_bytes)
        _validate_pdf_structure(path, max_pages, max_objects)
        connection.send(("ok", ""))
    except InvalidPdfSourceError as exc:
        connection.send(("invalid", str(exc)))
    except BaseException:
        try:
            connection.send(("invalid", "PDF could not be safely parsed"))
        except BaseException:
            pass
    finally:
        connection.close()


def _terminate_and_reap_pdf_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
    process.join(timeout=0.5)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.5)
    if process.is_alive():
        raise RuntimeError("PDF inspection child could not be terminated")
    process.close()


async def _acquire_pdf_inspection_slot(deadline: float) -> None:
    loop = asyncio.get_running_loop()
    while not _PDF_INSPECTION_SLOTS.acquire(blocking=False):
        if loop.time() >= deadline:
            raise InvalidPdfSourceError("PDF inspection timed out")
        await asyncio.sleep(0.01)


async def _run_pdf_inspection_process(
    path: str | os.PathLike[str],
    *,
    timeout_seconds: float = _PDF_INSPECTION_TIMEOUT_SECONDS,
    worker: Callable[..., None] = _pdf_inspection_worker,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    await _acquire_pdf_inspection_slot(deadline)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        name="yfeistai-pdf-inspector",
        target=worker,
        args=(
            os.fspath(path),
            sender,
            MAX_PDF_PAGES,
            _MAX_OBJECTS_INSPECTED,
            _PDF_INSPECTION_MEMORY_BYTES,
        ),
    )
    try:
        process.start()
        sender.close()
        message: tuple[str, str] | None = None
        while True:
            try:
                if receiver.poll():
                    message = receiver.recv()
                    break
            except (EOFError, OSError):
                break
            if not process.is_alive():
                try:
                    if receiver.poll():
                        message = receiver.recv()
                except (EOFError, OSError):
                    pass
                break
            if loop.time() >= deadline:
                raise InvalidPdfSourceError("PDF inspection timed out")
            await asyncio.sleep(0.01)
        if message is None or message[0] != "ok":
            detail = message[1] if message is not None else "PDF could not be safely parsed"
            raise InvalidPdfSourceError(detail)
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            _terminate_and_reap_pdf_process(process)
        else:
            process.close()
        _PDF_INSPECTION_SLOTS.release()


async def _stage_pdf(upload: UploadFileLike) -> _StagedPdf:
    if (upload.content_type or "").split(";", 1)[0].strip().lower() != "application/pdf":
        try:
            raise UnsupportedSourceMediaError("only application/pdf is accepted")
        finally:
            await upload.close()
    handle = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix="yfeistai-pdf-",
        suffix=".pdf",
        delete=False,
    )
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
        handle.flush()
        await _run_pdf_inspection_process(handle.name)
        handle.seek(0)
        return _StagedPdf(
            handle=handle,
            path=handle.name,
            filename=_safe_filename(upload.filename),
            sha256=digest.hexdigest(),
            size_bytes=total,
        )
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except FileNotFoundError:
            pass
        raise
    finally:
        await upload.close()


async def _file_chunks(handle: BinaryIO) -> AsyncIterator[bytes]:
    handle.seek(0)
    while chunk := await asyncio.to_thread(handle.read, _READ_CHUNK_BYTES):
        yield chunk


async def _await_upload_completion(task: asyncio.Task[UploadRecord]) -> UploadRecord:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        result = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


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

    async def _materialize_upload(
        self,
        store: ClassroomArtifactStore,
        receipt: UploadRecord,
        staged: _StagedPdf,
    ) -> UploadRecord:
        try:
            artifact = await store.reconcile_verified(
                receipt.object_key,
                receipt.sha256,
                receipt.size_bytes,
                content_type="application/pdf",
                ownership_token=receipt.ownership_token,
            )
        except BaseException:
            await self._repository.mark_upload_failed(
                receipt.upload_id,
                "object_reconcile_failed",
            )
            raise
        if artifact is None:
            try:
                artifact = await store.put_verified(
                    receipt.object_key,
                    _file_chunks(staged.handle),
                    receipt.sha256,
                    receipt.size_bytes,
                    content_type="application/pdf",
                    ownership_token=receipt.ownership_token,
                )
            except BaseException as write_error:
                try:
                    artifact = await store.reconcile_verified(
                        receipt.object_key,
                        receipt.sha256,
                        receipt.size_bytes,
                        content_type="application/pdf",
                        ownership_token=receipt.ownership_token,
                    )
                except BaseException:
                    artifact = None
                if artifact is None:
                    await self._repository.mark_upload_failed(
                        receipt.upload_id,
                        "object_write_failed",
                    )
                    raise write_error
        try:
            return await self._repository.complete_upload(receipt.upload_id, artifact)
        except BaseException as finalize_error:
            refreshed = await self._repository.find_upload_by_sha256(receipt.sha256)
            if (
                refreshed is not None
                and refreshed.status == "uploaded"
                and refreshed.upload_id == receipt.upload_id
                and refreshed.object_key == artifact.key
                and refreshed.sha256 == artifact.sha256
                and refreshed.size_bytes == artifact.size
                and refreshed.ownership_token == artifact.ownership_token
                and refreshed.object_revision == artifact.revision
                and refreshed.object_version_id == artifact.version_id
            ):
                return refreshed
            await self._repository.mark_upload_failed(
                receipt.upload_id,
                "receipt_finalize_failed",
            )
            raise finalize_error

    async def _reconcile_pending_with_store(
        self,
        store: ClassroomArtifactStore,
        *,
        limit: int,
    ) -> int:
        receipts = await self._repository.list_reconcilable_uploads(limit)
        for receipt in receipts:
            try:
                artifact = await store.reconcile_verified(
                    receipt.object_key,
                    receipt.sha256,
                    receipt.size_bytes,
                    content_type="application/pdf",
                    ownership_token=receipt.ownership_token,
                )
                if receipt.status == "cleanup_pending":
                    cleanup_artifact = artifact or StoredArtifact(
                        key=receipt.object_key,
                        sha256=receipt.sha256,
                        size=receipt.size_bytes,
                        content_type="application/pdf",
                        ownership_token=receipt.ownership_token,
                        revision=receipt.object_revision,
                        version_id=receipt.object_version_id,
                    )
                    await store.delete_owned(cleanup_artifact)
                    await self._repository.delete_reconciled_upload(receipt.upload_id)
                elif artifact is not None:
                    await self._repository.complete_upload(receipt.upload_id, artifact)
                else:
                    await self._repository.mark_upload_failed(
                        receipt.upload_id,
                        "object_missing",
                    )
            except Exception:
                try:
                    await self._repository.mark_upload_failed(
                        receipt.upload_id,
                        "cleanup_failed"
                        if receipt.status == "cleanup_pending"
                        else "reconcile_failed",
                        cleanup_pending=receipt.status == "cleanup_pending",
                    )
                except Exception:
                    pass
        return len(receipts)

    async def reconcile_pending_uploads(
        self,
        context: TenantContext,
        *,
        limit: int = 8,
    ) -> int:
        store = await self._store_provider.store_for_tenant(context.tenant_id)
        return await self._reconcile_pending_with_store(store, limit=limit)

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
            or not _has_stable_knowledge_generation(resource)
        ):
            raise InvalidSourceBindingError("knowledge resource identity is invalid")
        if not self._knowledge_exists(resource):
            raise SourceNotFoundError("knowledge resource not found")
        resource_owner_id = _knowledge_resource_owner_id(context, resource)
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
        try:
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
        except SourceEntitlementDeniedError as exc:
            raise SourceAccessDeniedError(
                "knowledge resource is not entitled to this tenant"
            ) from exc

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
        try:
            store = await self._store_provider.store_for_tenant(context.tenant_id)
            await self._reconcile_pending_with_store(store, limit=8)
        except BaseException:
            await upload.close()
            raise
        staged = await _stage_pdf(upload)
        try:
            upload_id = _digest_id(
                "upload",
                context.tenant_id,
                staged.sha256,
            )
            object_key = source_upload_key(context.tenant_id, upload_id)
            receipt = await self._repository.reserve_upload(
                NewUploadReceipt(
                    upload_id=upload_id,
                    object_key=object_key,
                    sha256=staged.sha256,
                    size_bytes=staged.size_bytes,
                    uploaded_by=context.user_id,
                    ownership_token=uuid.uuid4().hex,
                )
            )
            completed = await _await_upload_completion(
                asyncio.create_task(self._materialize_upload(store, receipt, staged))
            )
            snapshot_id = _digest_id(
                "pdf-source",
                context.tenant_id,
                completed.upload_id,
                course_id,
                class_id or "",
                staged.filename,
            )
            binding_id = source_binding_id(
                context.tenant_id,
                snapshot_id,
                course_id,
                class_id,
            )
            return await self._repository.bind_uploaded_pdf(
                completed,
                NewPdfSnapshot(
                    snapshot_id=snapshot_id,
                    upload_id=completed.upload_id,
                    display_name=staged.filename,
                    permission_sha256=_target_permission_sha256(
                        context.tenant_id,
                        course_id,
                        class_id,
                    ),
                ),
                binding_id=binding_id,
                course_id=course_id,
                class_id=class_id,
                actor_id=context.user_id,
            )
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
