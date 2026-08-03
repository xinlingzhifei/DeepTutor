from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
from pathlib import Path
import time

from fastapi import HTTPException
import pytest

from deeptutor.multi_user.knowledge_access import AuthorizedKnowledgeSource
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.repositories.sources import (
    BoundSourceRecord,
    SavedSourceSnapshot,
    SourceEntitlementDeniedError,
    SourceNotFoundError,
)
from deeptutor.teaching.source_snapshots import (
    SnapshotRequest,
    SourceAccessDenied,
    SourceSnapshotBuilder,
    SourceSnapshotUnavailable,
    _run_pdf_extraction_process,
)
from deeptutor.teaching.tenant_context import TenantContext

GENERATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RESOURCE_ID = f"user:kb:{GENERATION}"


def _context(*, tenant_id: str = "tenant-a") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=f"tenant_{tenant_id}",
        user_id="teacher-a",
        permissions=frozenset(
            {
                ScopedPermission(
                    permission="source.use",
                    scope_type="course",
                    scope_id="course-a",
                    tenant_id=tenant_id,
                )
            }
        ),
    )


def _request() -> SnapshotRequest:
    return SnapshotRequest(
        course_id="course-a",
        class_id="class-a",
        query="Explain Newton's second law",
    )


def _resource(tmp_path: Path) -> AuthorizedKnowledgeSource:
    return AuthorizedKnowledgeSource(
        resource_id=RESOURCE_ID,
        generation_id=GENERATION,
        name="mechanics",
        source="user",
        resource_owner_id="teacher-a",
        read_only=False,
        index_signature="idx-v1",
        _base_dir=tmp_path,
    )


def test_authorized_knowledge_source_rejects_alias_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="generation identity"):
        AuthorizedKnowledgeSource(
            resource_id="user:kb:course-a",
            generation_id=GENERATION,
            name="mechanics",
            source="user",
            resource_owner_id="teacher-a",
            read_only=False,
            index_signature="idx-v1",
            _base_dir=tmp_path,
        )


def _bound_source() -> BoundSourceRecord:
    return BoundSourceRecord(
        binding_id="binding-a",
        snapshot_id="bound-snapshot-a",
        source_type="knowledge_base",
        source_id=RESOURCE_ID,
        resource_owner_id="teacher-a",
        source_revision="binding-v1",
        content_sha256="1" * 64,
        permission_sha256="2" * 64,
        display_name=None,
        upload_id=None,
        object_key=None,
    )


def _sleeping_pdf_extraction_worker(path, connection, max_pages, max_objects, memory_limit):
    del path, connection, max_pages, max_objects, memory_limit
    time.sleep(10)


def _memory_bomb_pdf_extraction_worker(
    path,
    connection,
    max_pages,
    max_objects,
    memory_limit,
):
    del path, max_pages, max_objects
    from deeptutor.teaching.services.sources import _apply_pdf_worker_memory_limit

    _apply_pdf_worker_memory_limit(memory_limit)
    try:
        payload = bytearray(memory_limit * 4)
        connection.send(("ok", str(len(payload))))
    except BaseException:
        pass
    finally:
        connection.close()


class _Repository:
    def __init__(self, bound: BoundSourceRecord | Exception | None = None) -> None:
        self.bound = bound or _bound_source()
        self.persisted = []

    async def require_authorized_source(self, **kwargs) -> BoundSourceRecord:
        if isinstance(self.bound, Exception):
            raise self.bound
        return self.bound

    async def persist_authorized_snapshot(self, bound, snapshot, **kwargs):
        self.persisted.append((bound, snapshot, kwargs))
        return SavedSourceSnapshot(
            snapshot_id=snapshot.snapshot_id,
            created_at=datetime(2026, 8, 3, 8, tzinfo=timezone.utc),
        )


class _RagService:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def search(self, query: str, kb_name: str):
        self.calls.append((query, kb_name))
        return self.result


@pytest.mark.asyncio
async def test_snapshot_rejects_kb_not_visible_to_current_user(tmp_path: Path) -> None:
    repository = _Repository()

    def deny(_kb_ref: str) -> AuthorizedKnowledgeSource:
        raise HTTPException(status_code=403, detail="private")

    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        knowledge_resolver=deny,
        rag_service_factory=lambda _source: pytest.fail("RAG must not run"),
    )

    with pytest.raises(SourceAccessDenied, match="not available"):
        await builder.from_kb("admin:kb:private-b", _request())

    assert repository.persisted == []


@pytest.mark.asyncio
async def test_source_bound_to_other_tenant_is_rejected(tmp_path: Path) -> None:
    repository = _Repository(SourceNotFoundError("source binding not found"))
    source = _resource(tmp_path)
    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        knowledge_resolver=lambda _kb_ref: source,
        rag_service_factory=lambda _source: pytest.fail("RAG must not run"),
    )

    with pytest.raises(SourceAccessDenied, match="not bound"):
        await builder.from_kb(RESOURCE_ID, _request())


@pytest.mark.asyncio
async def test_grounded_snapshot_contains_only_authorized_fragments_and_stable_digest(
    tmp_path: Path,
) -> None:
    repository = _Repository()
    rag = _RagService(
        {
            "provider": "llamaindex",
            "index_signature": "idx-v1",
            "sources": [
                {
                    "chunk_id": "chunk-7",
                    "content": "A net force changes an object's momentum.",
                    "source": "C:/private/kbs/mechanics/book.pdf",
                    "page": 4,
                    "title": "Newton's second law",
                },
                {"chunk_id": "empty", "content": "", "source": "ignored.pdf"},
            ],
        }
    )
    instants = iter(
        (
            datetime(2026, 8, 3, 8, tzinfo=timezone.utc),
            datetime(2026, 8, 3, 9, tzinfo=timezone.utc),
        )
    )
    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path),
        rag_service_factory=lambda _source: rag,
        clock=lambda: next(instants),
    )

    first = await builder.from_kb(RESOURCE_ID, _request())
    second = await builder.from_kb(RESOURCE_ID, _request())

    assert first.snapshot_sha256 == second.snapshot_sha256
    # Idempotent persistence returns the original immutable creation timestamp.
    assert first.created_at == second.created_at
    assert first.retrieval_provider == "llamaindex"
    assert first.index_signature == "idx-v1"
    assert len(first.fragments) == 1
    assert all(fragment.permission == "source.use" for fragment in first.fragments)
    assert all(reference.document_id for reference in first.source_refs)
    assert first.source_refs[0].page == 4
    assert first.source_refs[0].section == "Newton's second law"
    assert first.permission_summary.permissions == ("source.use",)
    assert len(repository.persisted) == 2
    assert (
        repository.persisted[0][1].citation_manifest
        == repository.persisted[1][1].citation_manifest
    )

    payload = first.to_generation_payload()
    encoded = json.dumps(payload, sort_keys=True)
    assert "C:/private" not in encoded
    assert "base_dir" not in encoded
    assert "object_key" not in encoded
    assert payload["fragments"][0]["text"] == "A net force changes an object's momentum."


@pytest.mark.asyncio
async def test_different_queries_materialize_distinct_immutable_manifests(
    tmp_path: Path,
) -> None:
    repository = _Repository()
    results = iter(
        (
            {
                "provider": "llamaindex",
                "index_signature": "idx-v1",
                "sources": [
                    {"chunk_id": "force", "content": "Force changes momentum.", "source": "book.pdf"}
                ],
            },
            {
                "provider": "llamaindex",
                "index_signature": "idx-v1",
                "sources": [
                    {"chunk_id": "energy", "content": "Energy is conserved.", "source": "book.pdf"}
                ],
            },
        )
    )

    class _SequentialRag:
        async def search(self, query: str, kb_name: str):
            return next(results)

    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path),
        rag_service_factory=lambda _source: _SequentialRag(),
    )

    force = await builder.from_kb(RESOURCE_ID, _request())
    energy = await builder.from_kb(
        RESOURCE_ID,
        SnapshotRequest(
            course_id="course-a",
            class_id="class-a",
            query="Explain energy conservation",
        ),
    )

    assert force.snapshot_id != energy.snapshot_id
    assert force.fragments[0].text == "Force changes momentum."
    assert energy.fragments[0].text == "Energy is conserved."
    assert repository.persisted[0][1].citation_manifest != repository.persisted[1][1].citation_manifest
    assert repository.persisted[0][1].source_revision != repository.persisted[1][1].source_revision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persistence_error",
    (
        SourceNotFoundError("binding was deleted"),
        SourceEntitlementDeniedError("entitlement was revoked"),
    ),
)
async def test_snapshot_fails_closed_when_binding_is_revoked_during_retrieval(
    tmp_path: Path,
    persistence_error: Exception,
) -> None:
    class _RevokedRepository(_Repository):
        async def persist_authorized_snapshot(self, bound, snapshot, **kwargs):
            raise persistence_error

    rag = _RagService(
        {
            "provider": "llamaindex",
            "index_signature": "idx-v1",
            "sources": [
                {"chunk_id": "chunk", "content": "Grounded text", "source": "book.pdf"}
            ],
        }
    )
    builder = SourceSnapshotBuilder(
        _context(),
        _RevokedRepository(),
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path),
        rag_service_factory=lambda _source: rag,
    )

    with pytest.raises(SourceAccessDenied, match="changed or was revoked"):
        await builder.from_kb(RESOURCE_ID, _request())


@pytest.mark.asyncio
async def test_pdf_snapshot_uses_controlled_object_reader_without_leaking_key() -> None:
    pdf_bytes = b"%PDF-safe-test"
    bound = BoundSourceRecord(
        binding_id="pdf-binding-a",
        snapshot_id="pdf-source-a",
        source_type="pdf",
        source_id="upload-a",
        resource_owner_id="tenant-workspace",
        source_revision="pdf-v1",
        content_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        permission_sha256="2" * 64,
        display_name="mechanics.pdf",
        upload_id="upload-a",
        object_key="tenants/tenant-a/sources/upload-a/source.pdf",
    )
    repository = _Repository(bound)

    class _Store:
        async def open(self, key: str):
            assert key == bound.object_key
            yield pdf_bytes

    class _StoreProvider:
        async def store_for_tenant(self, tenant_id: str):
            assert tenant_id == "tenant-a"
            return _Store()

    async def extract(_path: str, filename: str) -> str:
        assert filename == "mechanics.pdf"
        return "--- Page 1 ---\nForce and motion."

    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        store_provider=_StoreProvider(),
        pdf_extractor=extract,
    )

    snapshot = await builder.from_pdf("pdf-binding-a", _request())

    payload = json.dumps(snapshot.to_generation_payload(), sort_keys=True)
    assert snapshot.fragments[0].text == "Force and motion."
    assert snapshot.source_refs[0].page == 1
    assert bound.object_key not in payload
    assert "object_key" not in payload


@pytest.mark.asyncio
async def test_generation_flip_during_retrieval_is_rejected(tmp_path: Path) -> None:
    original = _resource(tmp_path)
    replacement = AuthorizedKnowledgeSource(
        resource_id="user:kb:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        generation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        name=original.name,
        source="user",
        resource_owner_id="teacher-a",
        read_only=False,
        index_signature="idx-v2",
        _base_dir=tmp_path,
    )
    resolutions = iter((original, replacement))
    repository = _Repository()
    rag = _RagService(
        {
            "provider": "llamaindex",
            "index_signature": "idx-v1",
            "sources": [
                {"chunk_id": "new", "content": "Replacement content", "source": "book.pdf"}
            ],
        }
    )
    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        knowledge_resolver=lambda _kb_ref: next(resolutions),
        rag_service_factory=lambda _source: rag,
    )

    with pytest.raises(SourceAccessDenied, match="changed during retrieval"):
        await builder.from_kb(RESOURCE_ID, _request())

    assert repository.persisted == []


@pytest.mark.asyncio
async def test_opaque_document_ids_distinguish_same_basename_sources(tmp_path: Path) -> None:
    repository = _Repository()
    rag = _RagService(
        {
            "provider": "llamaindex",
            "index_signature": "idx-v1",
            "sources": [
                {"chunk_id": "a", "content": "First", "source": "/private/a/book.pdf"},
                {"chunk_id": "b", "content": "Second", "source": "/private/b/book.pdf"},
            ],
        }
    )
    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path),
        rag_service_factory=lambda _source: rag,
    )

    snapshot = await builder.from_kb(RESOURCE_ID, _request())

    assert snapshot.source_refs[0].document_id != snapshot.source_refs[1].document_id
    payload = json.dumps(snapshot.to_generation_payload(), sort_keys=True)
    assert "/private/a" not in payload
    assert "/private/b" not in payload


def _active_pdf_extractors() -> set[int | None]:
    return {
        process.pid
        for process in multiprocessing.active_children()
        if process.name == "yfeistai-pdf-extractor"
    }


@pytest.mark.asyncio
async def test_pdf_extraction_timeout_reaps_child(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-test")
    before = _active_pdf_extractors()

    with pytest.raises(SourceSnapshotUnavailable, match="timed out"):
        await _run_pdf_extraction_process(
            pdf,
            timeout_seconds=0.1,
            worker=_sleeping_pdf_extraction_worker,
            memory_limit_bytes=64 * 1024 * 1024,
        )

    assert _active_pdf_extractors() == before


@pytest.mark.asyncio
async def test_pdf_extraction_memory_limit_reaps_child(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-test")
    before = _active_pdf_extractors()

    with pytest.raises(
        SourceSnapshotUnavailable,
        match="could not be safely extracted|timed out",
    ):
        await _run_pdf_extraction_process(
            pdf,
            timeout_seconds=5,
            worker=_memory_bomb_pdf_extraction_worker,
            memory_limit_bytes=64 * 1024 * 1024,
        )

    assert _active_pdf_extractors() == before


@pytest.mark.asyncio
async def test_pdf_extraction_cancellation_reaps_child(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-test")
    before = _active_pdf_extractors()
    task = asyncio.create_task(
        _run_pdf_extraction_process(
            pdf,
            timeout_seconds=10,
            worker=_sleeping_pdf_extraction_worker,
            memory_limit_bytes=64 * 1024 * 1024,
        )
    )
    await asyncio.sleep(0.1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _active_pdf_extractors() == before
