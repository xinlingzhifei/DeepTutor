"""Authorized, immutable teaching-source snapshots.

This module is the only bridge from tenant-bound sources to the generation
data plane.  Filesystem paths and object-store keys stop at this boundary;
only authorized text fragments and opaque provenance identifiers cross it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import multiprocessing
import os
from pathlib import PurePosixPath
import re
import tempfile
from typing import Any, Literal, Protocol

from fastapi import HTTPException

from deeptutor.multi_user.knowledge_access import (
    AuthorizedKnowledgeSource,
    resolve_authorized_source,
)
from deeptutor.services.rag.retrieval_view import (
    bounded_retrieval_view,
    canonical_retrieval_fragments,
    canonical_retrieval_view_signature,
)
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.repositories.sources import (
    BoundSourceRecord,
    NewAuthorizedSnapshot,
    SavedSourceSnapshot,
    SourceEntitlementDeniedError,
    SourceNotFoundError,
)
from deeptutor.teaching.tenant_context import TenantContext

_MAX_FRAGMENTS = 20
_MAX_FRAGMENT_CHARS = 20_000
_MAX_PDF_FRAGMENTS = 8
_MAX_PDF_FRAGMENT_CHARS = 2_000
_MAX_PDF_TOTAL_CHARS = 8_000
_MAX_PDF_CANDIDATES = _MAX_PDF_FRAGMENTS * 4
_MAX_PDF_BYTES = 100 * 1024 * 1024
_PDF_EXTRACTION_TIMEOUT_SECONDS = 30.0
_PAGE_HEADING = re.compile(r"^--- Page (\d+) ---\s*$", re.MULTILINE)
_PDF_SEGMENT_BOUNDARY = re.compile(r"\n\s*\n+|(?<=[.!?。！？])\s+")
_QUERY_WORD = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)
_QUERY_STOP_WORDS = {
    "about",
    "and",
    "describe",
    "explain",
    "for",
    "how",
    "into",
    "lesson",
    "the",
    "this",
    "what",
    "with",
}


class SourceAccessDenied(PermissionError):
    """The source is not visible and bound to this actor's tenant scope."""


class SourceSnapshotUnavailable(RuntimeError):
    """A source was authorized but did not yield safe grounded fragments."""


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    course_id: str
    class_id: str | None
    query: str

    def __post_init__(self) -> None:
        for value in (self.course_id, self.query):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("source snapshot request is incomplete")
        if self.class_id is not None and not self.class_id.strip():
            raise ValueError("source snapshot class is invalid")


@dataclass(frozen=True, slots=True)
class AuthorizedFragment:
    fragment_id: str
    source_id: str
    text: str
    content_sha256: str
    permission: Literal["source.use"]
    document_id: str
    page: int | None
    section: str | None

    @classmethod
    def create(
        cls,
        *,
        stable_source_id: str,
        provider_fragment_id: str,
        text: str,
        document_id: str,
        page: int | None,
        section: str | None,
    ) -> AuthorizedFragment:
        normalized = text.strip()
        if not normalized:
            raise ValueError("authorized fragment text is empty")
        content_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        identity = _digest(
            "fragment-v1",
            stable_source_id,
            provider_fragment_id,
            document_id,
            str(page or ""),
            section or "",
            content_sha256,
        )
        return cls(
            fragment_id=f"fragment-{identity}",
            source_id=stable_source_id,
            text=normalized,
            content_sha256=content_sha256,
            permission="source.use",
            document_id=document_id,
            page=page,
            section=section,
        )


@dataclass(frozen=True, slots=True)
class AuthorizedSourceReference:
    citation_id: str
    source_id: str
    fragment_id: str
    document_id: str
    page: int | None
    section: str | None


@dataclass(frozen=True, slots=True)
class PermissionEvidence:
    permissions: tuple[Literal["source.use"], ...]
    scope_type: Literal["tenant", "course", "class"]
    scope_id: str

    def __post_init__(self) -> None:
        if self.permissions != ("source.use",) or not self.scope_id:
            raise ValueError("source permission evidence is invalid")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    snapshot_id: str
    source_kind: Literal["knowledge_base", "pdf"]
    stable_source_id: str
    source_revision: str
    snapshot_sha256: str
    fragments: tuple[AuthorizedFragment, ...]
    source_refs: tuple[AuthorizedSourceReference, ...]
    permission_summary: PermissionEvidence
    query_sha256: str
    retrieval_provider: str
    retrieval_view_signature: str
    created_at: datetime
    created_by: str

    @classmethod
    def create(
        cls,
        *,
        source_kind: Literal["knowledge_base", "pdf"],
        stable_source_id: str,
        source_revision: str,
        fragments: tuple[AuthorizedFragment, ...],
        source_refs: tuple[AuthorizedSourceReference, ...],
        permission_summary: PermissionEvidence,
        query_sha256: str,
        retrieval_provider: str,
        retrieval_view_signature: str,
        created_at: datetime,
        created_by: str,
    ) -> SourceSnapshot:
        if not fragments or len(fragments) != len(source_refs):
            raise ValueError("source snapshot requires matched fragments and references")
        if created_at.utcoffset() is None:
            raise ValueError("source snapshot timestamp must be timezone-aware")
        for digest in (query_sha256, retrieval_view_signature):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("source snapshot digest is invalid")
        fragment_ids = {fragment.fragment_id for fragment in fragments}
        if len(fragment_ids) != len(fragments):
            raise ValueError("source snapshot fragment identities must be unique")
        for fragment, reference in zip(fragments, source_refs, strict=True):
            if (
                fragment.permission != "source.use"
                or fragment.source_id != stable_source_id
                or reference.source_id != stable_source_id
                or reference.fragment_id != fragment.fragment_id
                or reference.document_id != fragment.document_id
            ):
                raise ValueError("source snapshot contains unauthorized lineage")
        canonical = {
            "created_by": created_by,
            "fragments": [_fragment_payload(item) for item in fragments],
            "permission_summary": asdict(permission_summary),
            "query_sha256": query_sha256,
            "retrieval_provider": retrieval_provider,
            "retrieval_view_signature": retrieval_view_signature,
            "schema_version": 1,
            "source_kind": source_kind,
            "source_revision": source_revision,
            "stable_source_id": stable_source_id,
            "source_refs": [asdict(item) for item in source_refs],
        }
        digest = hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()
        return cls(
            snapshot_id=f"source-snapshot-{digest}",
            source_kind=source_kind,
            stable_source_id=stable_source_id,
            source_revision=source_revision,
            snapshot_sha256=digest,
            fragments=fragments,
            source_refs=source_refs,
            permission_summary=permission_summary,
            query_sha256=query_sha256,
            retrieval_provider=retrieval_provider,
            retrieval_view_signature=retrieval_view_signature,
            created_at=created_at,
            created_by=created_by,
        )

    def to_generation_payload(self) -> dict[str, object]:
        """Return the complete safe payload accepted by a shared data plane."""

        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "source_kind": self.source_kind,
            "source_id": self.stable_source_id,
            "source_snapshot_sha256": self.snapshot_sha256,
            "fragments": [_fragment_payload(item) for item in self.fragments],
            "source_refs": [asdict(item) for item in self.source_refs],
            "permission_summary": asdict(self.permission_summary),
            "query_sha256": self.query_sha256,
            "retrieval": {
                "provider": self.retrieval_provider,
                "retrieval_view_signature": self.retrieval_view_signature,
            },
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
        }


class SourceSnapshotRepository(Protocol):
    async def require_authorized_source(self, **kwargs: object) -> BoundSourceRecord: ...

    async def persist_authorized_snapshot(
        self,
        bound: BoundSourceRecord,
        snapshot: NewAuthorizedSnapshot,
        **kwargs: object,
    ) -> SavedSourceSnapshot: ...


class _RagService(Protocol):
    async def search(self, query: str, kb_name: str) -> dict[str, Any]: ...


class _PdfStore(Protocol):
    async def open(self, key: str): ...


class _PdfStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> _PdfStore: ...


def _pdf_extraction_worker(
    path: str,
    connection,
    max_pages: int,
    max_objects: int,
    memory_limit_bytes: int,
    query: str,
) -> None:
    """Revalidate and extract a PDF inside a killable memory-bounded child."""

    try:
        from deeptutor.teaching.services.sources import (
            _apply_pdf_worker_memory_limit,
            _validate_pdf_structure,
        )
        _apply_pdf_worker_memory_limit(memory_limit_bytes)
        _validate_pdf_structure(path, max_pages, max_objects)
        text = _extract_query_selected_pdf_text(path, query)
        connection.send(("ok", text))
    except BaseException:
        try:
            connection.send(("invalid", "PDF could not be safely extracted"))
        except BaseException:
            pass
    finally:
        connection.close()


async def _run_pdf_extraction_process(
    path: str | os.PathLike[str],
    *,
    timeout_seconds: float = _PDF_EXTRACTION_TIMEOUT_SECONDS,
    worker: Callable[..., None] = _pdf_extraction_worker,
    memory_limit_bytes: int | None = None,
    query: str = "document",
) -> str:
    """Extract text with the Task 2 process, timeout, and memory guardrails."""

    from deeptutor.teaching.services.sources import (
        _MAX_OBJECTS_INSPECTED,
        _PDF_INSPECTION_MEMORY_BYTES,
        _PDF_INSPECTION_SLOTS,
        MAX_PDF_PAGES,
        InvalidPdfSourceError,
        _acquire_pdf_inspection_slot,
        _terminate_and_reap_pdf_process,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    try:
        await _acquire_pdf_inspection_slot(deadline)
    except InvalidPdfSourceError as exc:
        raise SourceSnapshotUnavailable("PDF extraction timed out") from exc
    receiver = None
    sender = None
    process = None
    primary_error: BaseException | None = None
    try:
        try:
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                name="yfeistai-pdf-extractor",
                target=worker,
                args=(
                    os.fspath(path),
                    sender,
                    MAX_PDF_PAGES,
                    _MAX_OBJECTS_INSPECTED,
                    memory_limit_bytes or _PDF_INSPECTION_MEMORY_BYTES,
                    query,
                ),
            )
        except Exception:
            raise SourceSnapshotUnavailable("PDF extraction could not be started") from None
        try:
            process.start()
        except Exception:
            raise SourceSnapshotUnavailable("PDF extraction could not be started") from None
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
                raise SourceSnapshotUnavailable("PDF extraction timed out")
            await asyncio.sleep(0.01)
        if (
            message is None
            or message[0] != "ok"
            or not isinstance(message[1], str)
            or not message[1].strip()
        ):
            raise SourceSnapshotUnavailable("PDF could not be safely extracted")
        return message[1]
    except asyncio.CancelledError as exc:
        primary_error = exc
        raise
    except SourceSnapshotUnavailable as exc:
        primary_error = exc
        raise
    except Exception:
        primary_error = SourceSnapshotUnavailable("PDF extraction failed")
        raise primary_error from None
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_failed = False
        for endpoint in (receiver, sender):
            if endpoint is None:
                continue
            try:
                endpoint.close()
            except Exception:
                cleanup_failed = True
        if process is not None:
            try:
                if process.pid is not None:
                    _terminate_and_reap_pdf_process(process)
                else:
                    process.close()
            except Exception:
                cleanup_failed = True
        try:
            _PDF_INSPECTION_SLOTS.release()
        except Exception:
            cleanup_failed = True
        if cleanup_failed and primary_error is None:
            raise SourceSnapshotUnavailable("PDF extraction cleanup failed") from None


def _digest(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _fragment_payload(fragment: AuthorizedFragment) -> dict[str, object]:
    return {
        "fragment_id": fragment.fragment_id,
        "source_id": fragment.source_id,
        "text": fragment.text,
        "content_sha256": fragment.content_sha256,
        "permission": fragment.permission,
        "document_id": fragment.document_id,
        "page": fragment.page,
        "section": fragment.section,
    }


def _authorized_view_signature(
    provider: str,
    fragments: tuple[AuthorizedFragment, ...],
) -> str:
    return canonical_retrieval_view_signature(
        {
            "provider": provider,
            "sources": [
                {
                    "content": fragment.text,
                    "document_id": fragment.document_id,
                    "id": fragment.fragment_id,
                    "page": fragment.page,
                    "section": fragment.section,
                }
                for fragment in fragments
            ],
        }
    )


def _scope_permission(context: TenantContext, request: SnapshotRequest) -> PermissionEvidence:
    scope = ResourceScope(
        tenant_id=context.tenant_id,
        course_id=request.course_id,
        class_id=request.class_id,
    )
    if not any(
        grant.allows_resource("source.use", scope) for grant in context.permissions
    ):
        raise SourceAccessDenied("source use is not allowed for this scope")
    if request.class_id is not None and any(
        grant.permission == "source.use"
        and grant.scope_type == "class"
        and grant.allows_resource("source.use", scope)
        for grant in context.permissions
    ):
        return PermissionEvidence(("source.use",), "class", request.class_id)
    if any(
        grant.permission == "source.use"
        and grant.scope_type == "course"
        and grant.allows_resource("source.use", scope)
        for grant in context.permissions
    ):
        return PermissionEvidence(("source.use",), "course", request.course_id)
    return PermissionEvidence(("source.use",), "tenant", context.tenant_id)


def _safe_label(value: object) -> str:
    raw = str(value or "document").replace("\\", "/")
    label = PurePosixPath(raw).name.strip()
    if not label or any(character in label for character in "\x00\r\n"):
        return "document"
    return label[:256]


def _page_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def _fragments_from_rag(
    stable_source_id: str,
    result: dict[str, Any],
) -> tuple[tuple[AuthorizedFragment, ...], tuple[AuthorizedSourceReference, ...]]:
    grounded = canonical_retrieval_fragments(result)
    fragments: list[AuthorizedFragment] = []
    references: list[AuthorizedSourceReference] = []
    for position, raw in enumerate(grounded[:_MAX_FRAGMENTS]):
        text = str(raw.get("content") or "").strip()
        if not text:
            continue
        text = text[:_MAX_FRAGMENT_CHARS]
        provenance = raw.get("provenance")
        provenance_rows = provenance if isinstance(provenance, list) else []
        primary = provenance_rows[0] if provenance_rows else {}
        provider_id = str(
            primary.get("chunk_id")
            or primary.get("id")
            or _digest(_canonical_json(provenance_rows), str(position))
        )
        raw_origin = _canonical_json(provenance_rows) or str(position)
        document_id = f"document-{_digest(stable_source_id, raw_origin)}"
        section_value = primary.get("title") or primary.get("section")
        section = _safe_label(section_value) if section_value else None
        page = _page_number(primary.get("page"))
        fragment = AuthorizedFragment.create(
            stable_source_id=stable_source_id,
            provider_fragment_id=provider_id,
            text=text,
            document_id=document_id,
            page=page,
            section=section,
        )
        if fragment.fragment_id in {item.fragment_id for item in fragments}:
            continue
        citation_id = f"citation-{_digest(fragment.fragment_id)}"
        fragments.append(fragment)
        references.append(
            AuthorizedSourceReference(
                citation_id=citation_id,
                source_id=stable_source_id,
                fragment_id=fragment.fragment_id,
                document_id=document_id,
                page=page,
                section=section,
            )
        )
    if not fragments:
        raise SourceSnapshotUnavailable("knowledge retrieval returned no grounded fragments")
    return tuple(fragments), tuple(references)


def _pdf_fragments(
    stable_source_id: str,
    text: str,
    *,
    query: str,
) -> tuple[tuple[AuthorizedFragment, ...], tuple[AuthorizedSourceReference, ...]]:
    matches = list(_PAGE_HEADING.finditer(text))
    sections: list[tuple[int | None, str]] = []
    if not matches:
        sections.append((None, text))
    else:
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections.append((int(match.group(1)), text[match.end() : end]))
    document_id = f"document-{_digest(stable_source_id)}"
    fragments: list[AuthorizedFragment] = []
    references: list[AuthorizedSourceReference] = []
    for page, position, normalized in _select_pdf_segments(sections, query):
        fragment = AuthorizedFragment.create(
            stable_source_id=stable_source_id,
            provider_fragment_id=f"page-{page or 0}-segment-{position}",
            text=normalized,
            document_id=document_id,
            page=page,
            section=None,
        )
        citation_id = f"citation-{_digest(fragment.fragment_id)}"
        fragments.append(fragment)
        references.append(
            AuthorizedSourceReference(
                citation_id=citation_id,
                source_id=stable_source_id,
                fragment_id=fragment.fragment_id,
                document_id=document_id,
                page=page,
                section=None,
            )
        )
    if not fragments:
        raise SourceSnapshotUnavailable("PDF source contains no extractable text")
    return tuple(fragments), tuple(references)


def _query_terms(query: str) -> tuple[str, ...]:
    terms: set[str] = set()
    for match in _QUERY_WORD.finditer(query.casefold()):
        value = match.group(0)
        if value.isascii():
            if len(value) >= 3 and value not in _QUERY_STOP_WORDS:
                terms.add(value)
            continue
        if len(value) == 1:
            terms.add(value)
        else:
            terms.update(value[index : index + 2] for index in range(len(value) - 1))
    return tuple(sorted(terms, key=lambda item: (-len(item), item)))


def _pdf_segments(raw: str) -> tuple[str, ...]:
    segments: list[str] = []
    for part in _PDF_SEGMENT_BOUNDARY.split(raw):
        normalized = " ".join(part.split())
        if not normalized:
            continue
        while len(normalized) > _MAX_PDF_FRAGMENT_CHARS:
            split_at = normalized.rfind(" ", 0, _MAX_PDF_FRAGMENT_CHARS + 1)
            if split_at < _MAX_PDF_FRAGMENT_CHARS // 2:
                split_at = _MAX_PDF_FRAGMENT_CHARS
            segments.append(normalized[:split_at].strip())
            normalized = normalized[split_at:].strip()
        if normalized:
            segments.append(normalized)
    return tuple(segments)


def _pdf_relevance(text: str, query_terms: tuple[str, ...]) -> int:
    lowered = text.casefold()
    return sum(min(lowered.count(term), 3) * len(term) for term in query_terms)


def _select_pdf_segments(
    sections: Any,
    query: str,
) -> tuple[tuple[int | None, int, str], ...]:
    query_terms = _query_terms(query)
    candidates: list[tuple[int, int | None, int, str]] = []
    for page_position, (page, raw) in enumerate(sections):
        for segment_position, segment in enumerate(_pdf_segments(raw)):
            score = _pdf_relevance(segment, query_terms)
            if score <= 0:
                continue
            candidates.append(
                (score, page, page_position * 10_000 + segment_position, segment)
            )
            if len(candidates) > _MAX_PDF_CANDIDATES:
                candidates.sort(key=lambda item: (-item[0], item[2]))
                del candidates[_MAX_PDF_CANDIDATES:]
    if not candidates:
        raise SourceSnapshotUnavailable("PDF source has no query-relevant text")
    candidates.sort(key=lambda item: (-item[0], item[2]))

    selected: list[tuple[int | None, int, str]] = []
    total_chars = 0
    for _score, page, position, raw in candidates:
        if len(selected) >= _MAX_PDF_FRAGMENTS or total_chars >= _MAX_PDF_TOTAL_CHARS:
            break
        remaining = _MAX_PDF_TOTAL_CHARS - total_chars
        normalized = raw.strip()[: min(_MAX_PDF_FRAGMENT_CHARS, remaining)]
        if not normalized:
            continue
        selected.append((page, position, normalized))
        total_chars += len(normalized)
    return tuple(selected)


def _extract_query_selected_pdf_text(path: str, query: str) -> str:
    """Scan every page but retain only the bounded query-relevant view."""

    try:
        import fitz
    except ImportError:  # pragma: no cover - production installs PyMuPDF
        fitz = None

    if fitz is not None:
        with fitz.open(path) as document:
            if document.is_encrypted and not document.authenticate(""):
                raise SourceSnapshotUnavailable("PDF is encrypted")
            selected = _select_pdf_segments(
                (
                    (page_number, page.get_text() or "")
                    for page_number, page in enumerate(document, 1)
                ),
                query,
            )
    else:  # pragma: no cover - fallback for minimal installations
        from pypdf import PdfReader

        reader = PdfReader(path)
        if getattr(reader, "is_encrypted", False):
            raise SourceSnapshotUnavailable("PDF is encrypted")
        selected = _select_pdf_segments(
            (
                (page_number, page.extract_text() or "")
                for page_number, page in enumerate(reader.pages, 1)
            ),
            query,
        )
    return "\n\n".join(
        f"--- Page {page or 0} ---\n{text}" for page, _position, text in selected
    )


class SourceSnapshotBuilder:
    """Build and persist query-specific snapshots after two-layer authorization."""

    def __init__(
        self,
        context: TenantContext,
        repository: SourceSnapshotRepository,
        *,
        knowledge_resolver: Callable[[str], AuthorizedKnowledgeSource] = (
            resolve_authorized_source
        ),
        rag_service_factory: Callable[[AuthorizedKnowledgeSource], _RagService] | None = None,
        store_provider: _PdfStoreProvider | None = None,
        pdf_extractor: Callable[[str, str], Awaitable[str]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._context = context
        self._repository = repository
        self._knowledge_resolver = knowledge_resolver
        self._rag_service_factory = rag_service_factory
        self._store_provider = store_provider
        self._pdf_extractor = pdf_extractor
        self._clock = clock

    async def _persist(
        self,
        bound: BoundSourceRecord,
        snapshot: SourceSnapshot,
        request: SnapshotRequest,
    ) -> SourceSnapshot:
        manifest_payload = snapshot.to_generation_payload()
        # ``created_at`` is the database row's server timestamp. Excluding the
        # attempted time keeps idempotent retries byte-identical while the row
        # still records the authoritative creation time.
        manifest_payload.pop("created_at")
        manifest = _canonical_json(manifest_payload)
        try:
            saved = await self._repository.persist_authorized_snapshot(
                bound,
                NewAuthorizedSnapshot(
                    snapshot_id=snapshot.snapshot_id,
                    source_revision=f"retrieval-v1-{snapshot.snapshot_sha256}",
                    content_sha256=snapshot.snapshot_sha256,
                    permission_sha256=_digest(
                        self._context.tenant_id,
                        snapshot.permission_summary.scope_type,
                        snapshot.permission_summary.scope_id,
                        "source.use",
                    ),
                    citation_manifest=manifest,
                ),
                course_id=request.course_id,
                class_id=request.class_id,
                actor_id=self._context.user_id,
            )
        except (SourceNotFoundError, SourceEntitlementDeniedError) as exc:
            raise SourceAccessDenied(
                "source binding changed or was revoked during retrieval"
            ) from exc
        return replace(snapshot, snapshot_id=saved.snapshot_id, created_at=saved.created_at)

    async def from_kb(self, kb_ref: str, request: SnapshotRequest) -> SourceSnapshot:
        permission = _scope_permission(self._context, request)
        try:
            resource = self._knowledge_resolver(kb_ref)
        except asyncio.CancelledError:
            raise
        except HTTPException as exc:
            raise SourceAccessDenied("knowledge source is not available to this user") from exc
        except Exception:
            raise SourceSnapshotUnavailable(
                "knowledge source could not be resolved"
            ) from None
        try:
            bound = await self._repository.require_authorized_source(
                source_type="knowledge_base",
                source_id=resource.resource_id,
                resource_owner_id=resource.resource_owner_id,
                course_id=request.course_id,
                class_id=request.class_id,
                binding_id=None,
            )
        except (SourceNotFoundError, SourceEntitlementDeniedError) as exc:
            raise SourceAccessDenied("knowledge source is not bound to this tenant scope") from exc
        if (
            bound.source_id != resource.resource_id
            or bound.resource_owner_id != resource.resource_owner_id
        ):
            raise SourceAccessDenied("knowledge source identity does not match its binding")
        try:
            if self._rag_service_factory is None:
                result = await resource.search(request.query)
            else:
                result = await self._rag_service_factory(resource).search(
                    request.query,
                    resource.name,
                )
        except asyncio.CancelledError:
            raise
        except HTTPException as exc:
            raise SourceAccessDenied("knowledge source changed during retrieval") from exc
        except Exception:
            raise SourceSnapshotUnavailable("knowledge retrieval failed") from None
        if result.get("error_type") or result.get("needs_reindex"):
            raise SourceSnapshotUnavailable("knowledge retrieval is unavailable")
        provider = str(result.get("provider") or "").strip()
        if not provider:
            raise SourceSnapshotUnavailable("knowledge retrieval provider is missing")
        if provider != resource.retrieval_provider:
            raise SourceAccessDenied("knowledge source changed during retrieval")
        try:
            computed_view_signature = canonical_retrieval_view_signature(result)
            bounded_result = bounded_retrieval_view(result)
        except ValueError:
            raise SourceSnapshotUnavailable("knowledge retrieval view is invalid") from None
        if not computed_view_signature:
            if (
                result.get("content_kind") == "retrieval_context"
                and str(result.get("content") or "").strip()
            ):
                raise SourceSnapshotUnavailable(
                    "knowledge provider returned context without traceable provenance; "
                    "update or reconnect the provider"
                )
            raise SourceSnapshotUnavailable(
                "knowledge retrieval returned no grounded fragments"
            )
        claimed_view_signature = str(
            result.get("retrieval_view_signature") or ""
        ).strip()
        if not claimed_view_signature or not hmac.compare_digest(
            claimed_view_signature,
            computed_view_signature,
        ):
            raise SourceAccessDenied("knowledge retrieval view changed during retrieval")
        result = bounded_result
        computed_view_signature = str(result["retrieval_view_signature"])
        try:
            current_resource = self._knowledge_resolver(resource.resource_id)
        except asyncio.CancelledError:
            raise
        except HTTPException as exc:
            raise SourceAccessDenied("knowledge source changed during retrieval") from exc
        except Exception:
            raise SourceAccessDenied(
                "knowledge source changed during retrieval"
            ) from None
        if (
            current_resource.resource_id != resource.resource_id
            or current_resource.generation_id != resource.generation_id
            or current_resource.name != resource.name
            or current_resource.source != resource.source
            or current_resource.resource_owner_id != resource.resource_owner_id
            or current_resource.retrieval_provider != resource.retrieval_provider
        ):
            raise SourceAccessDenied("knowledge source changed during retrieval")
        fragments, references = _fragments_from_rag(resource.resource_id, result)
        authorized_view_signature = _authorized_view_signature(provider, fragments)
        snapshot = SourceSnapshot.create(
            source_kind="knowledge_base",
            stable_source_id=resource.resource_id,
            source_revision=bound.source_revision,
            fragments=fragments,
            source_refs=references,
            permission_summary=permission,
            query_sha256=hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
            retrieval_provider=provider,
            retrieval_view_signature=authorized_view_signature,
            created_at=self._clock(),
            created_by=self._context.user_id,
        )
        return await self._persist(bound, snapshot, request)

    async def from_pdf(self, binding_id: str, request: SnapshotRequest) -> SourceSnapshot:
        permission = _scope_permission(self._context, request)
        try:
            bound = await self._repository.require_authorized_source(
                source_type="pdf",
                source_id=None,
                resource_owner_id=None,
                course_id=request.course_id,
                class_id=request.class_id,
                binding_id=binding_id,
            )
        except SourceNotFoundError as exc:
            raise SourceAccessDenied("PDF source is not bound to this tenant scope") from exc
        if self._store_provider is None or bound.object_key is None:
            raise SourceSnapshotUnavailable("PDF source reader is not configured")
        handle = None
        operation_error: BaseException | None = None
        received = 0
        digest = hashlib.sha256()
        try:
            store = await self._store_provider.store_for_tenant(self._context.tenant_id)
            stream = await store.open(bound.object_key)
            handle = tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix="yfeistai-source-extract-",
                suffix=".pdf",
                delete=False,
            )
            async for chunk in stream:
                if not isinstance(chunk, bytes):
                    raise SourceSnapshotUnavailable("PDF source stream is invalid")
                received += len(chunk)
                if received > _MAX_PDF_BYTES:
                    raise SourceSnapshotUnavailable("PDF source exceeds extraction limit")
                digest.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
            await asyncio.to_thread(handle.flush)
            await asyncio.to_thread(handle.close)
            if digest.hexdigest() != bound.content_sha256:
                raise SourceSnapshotUnavailable("PDF source integrity check failed")
            filename = bound.display_name or "source.pdf"
            if self._pdf_extractor is None:
                text = await _run_pdf_extraction_process(
                    handle.name,
                    query=request.query,
                )
            else:
                text = await self._pdf_extractor(handle.name, filename)
        except asyncio.CancelledError as exc:
            operation_error = exc
            raise
        except SourceSnapshotUnavailable as exc:
            operation_error = exc
            raise
        except Exception as exc:
            operation_error = exc
            raise SourceSnapshotUnavailable("PDF source could not be read") from None
        finally:
            cleanup_failed = False
            if handle is not None:
                if not handle.closed:
                    try:
                        await asyncio.to_thread(handle.close)
                    except Exception:
                        cleanup_failed = True
                try:
                    await asyncio.to_thread(os.unlink, handle.name)
                except FileNotFoundError:
                    pass
                except Exception:
                    cleanup_failed = True
            if cleanup_failed and operation_error is None:
                raise SourceSnapshotUnavailable("PDF source cleanup failed") from None
        fragments, references = _pdf_fragments(
            bound.source_id,
            text,
            query=request.query,
        )
        retrieval_view_signature = _authorized_view_signature(
            "document_extractor",
            fragments,
        )
        snapshot = SourceSnapshot.create(
            source_kind="pdf",
            stable_source_id=bound.source_id,
            source_revision=bound.source_revision,
            fragments=fragments,
            source_refs=references,
            permission_summary=permission,
            query_sha256=hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
            retrieval_provider="document_extractor",
            retrieval_view_signature=retrieval_view_signature,
            created_at=self._clock(),
            created_by=self._context.user_id,
        )
        return await self._persist(bound, snapshot, request)


__all__ = [
    "AuthorizedFragment",
    "AuthorizedSourceReference",
    "PermissionEvidence",
    "SnapshotRequest",
    "SourceAccessDenied",
    "SourceSnapshot",
    "SourceSnapshotBuilder",
    "SourceSnapshotUnavailable",
]
