from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import inspect
import json
import multiprocessing
from pathlib import Path
import time

from fastapi import HTTPException
import pytest

from deeptutor.multi_user.knowledge_access import AuthorizedKnowledgeSource
from deeptutor.services.rag.retrieval_view import stamp_retrieval_view_signature
from deeptutor.teaching.object_store import (
    LocalClassroomArtifactStore,
    S3ClassroomArtifactStore,
)
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.repositories.sources import (
    BoundSourceRecord,
    SavedSourceSnapshot,
    SourceEntitlementDeniedError,
    SourceNotFoundError,
)
import deeptutor.teaching.source_snapshots as source_snapshots_module
from deeptutor.teaching.source_snapshots import (
    SnapshotRequest,
    SourceAccessDenied,
    SourceSnapshotBuilder,
    SourceSnapshotUnavailable,
    _pdf_fragments,
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


def _resource(
    tmp_path: Path,
    *,
    provider: str = "llamaindex",
) -> AuthorizedKnowledgeSource:
    del tmp_path
    return AuthorizedKnowledgeSource(
        resource_id=RESOURCE_ID,
        generation_id=GENERATION,
        name="mechanics",
        source="user",
        resource_owner_id="teacher-a",
        read_only=True,
        retrieval_provider=provider,
    )


def test_authorized_knowledge_source_rejects_alias_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="generation identity"):
        AuthorizedKnowledgeSource(
            resource_id="user:kb:course-a",
            generation_id=GENERATION,
            name="mechanics",
            source="user",
            resource_owner_id="teacher-a",
            read_only=True,
            retrieval_provider="llamaindex",
        )


def test_local_and_s3_open_contracts_are_coroutines_returning_streams() -> None:
    assert inspect.iscoroutinefunction(LocalClassroomArtifactStore.open)
    assert inspect.iscoroutinefunction(S3ClassroomArtifactStore.open)


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


def _sleeping_pdf_extraction_worker(
    path,
    connection,
    max_pages,
    max_objects,
    memory_limit,
    query,
):
    del path, connection, max_pages, max_objects, memory_limit, query
    time.sleep(10)


def _memory_bomb_pdf_extraction_worker(
    path,
    connection,
    max_pages,
    max_objects,
    memory_limit,
    query,
):
    del path, max_pages, max_objects, query
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
        self.authorization_requests: list[dict[str, object]] = []

    async def require_authorized_source(self, **kwargs) -> BoundSourceRecord:
        self.authorization_requests.append(kwargs)
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
        if "retrieval_view_signature" not in self.result:
            stamp_retrieval_view_signature(self.result)
        return self.result


def _context_view_signature(result: dict[str, object]) -> str:
    payload = {
        "content_kind": "retrieval_context",
        "fragments": [
            {
                "content": str(result["content"]).strip(),
                "provenance": result.get("retrieval_provenance") or result.get("sources") or [],
            }
        ],
        "mode": str(result.get("mode") or ""),
        "provider": str(result["provider"]),
        "schema_version": 1,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


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
async def test_kb_preflight_resolves_logical_identity_and_exact_target_without_rag(
    tmp_path: Path,
) -> None:
    repository = _Repository()
    resolved_refs: list[str] = []
    source = _resource(tmp_path)

    def resolve(kb_ref: str) -> AuthorizedKnowledgeSource:
        resolved_refs.append(kb_ref)
        return source

    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        knowledge_resolver=resolve,
        rag_service_factory=lambda _source: pytest.fail("RAG must not run"),
    )

    bound = await builder.require_authorized_kb(
        "kb-visible-alias",
        course_id="course-a",
        class_id="class-a",
    )

    assert bound == _bound_source()
    assert resolved_refs == ["kb-visible-alias"]
    assert repository.authorization_requests == [
        {
            "source_type": "knowledge_base",
            "source_id": RESOURCE_ID,
            "resource_owner_id": "teacher-a",
            "course_id": "course-a",
            "class_id": "class-a",
            "binding_id": None,
        }
    ]


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
    assert len(first.retrieval_view_signature) == 64
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
            return stamp_retrieval_view_signature(next(results))

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

            async def chunks():
                yield pdf_bytes

            return chunks()

    class _StoreProvider:
        async def store_for_tenant(self, tenant_id: str):
            assert tenant_id == "tenant-a"
            return _Store()

    async def extract(_path: str, filename: str) -> str:
        assert filename == "mechanics.pdf"
        return "--- Page 1 ---\nNewton's second law explains force and motion."

    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        store_provider=_StoreProvider(),
        pdf_extractor=extract,
    )

    snapshot = await builder.from_pdf("pdf-binding-a", _request())

    payload = json.dumps(snapshot.to_generation_payload(), sort_keys=True)
    assert snapshot.fragments[0].text == "Newton's second law explains force and motion."
    assert snapshot.source_refs[0].page == 1
    assert bound.object_key not in payload
    assert "object_key" not in payload


@pytest.mark.asyncio
async def test_initial_kb_resolver_error_is_mapped_without_path() -> None:
    leaked_path = "C:/private/kb_config.json denied"

    def fail_resolution(_kb_ref: str) -> AuthorizedKnowledgeSource:
        raise OSError(leaked_path)

    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(),
        knowledge_resolver=fail_resolution,
    )

    with pytest.raises(SourceSnapshotUnavailable) as captured:
        await builder.from_kb(RESOURCE_ID, _request())

    assert str(captured.value) == "knowledge source could not be resolved"
    assert leaked_path not in str(captured.value)


@pytest.mark.asyncio
async def test_post_retrieval_resolver_error_is_mapped_without_path(
    tmp_path: Path,
) -> None:
    leaked_path = "C:/private/kb_config.json denied"
    resource = _resource(tmp_path)
    resolution_count = 0

    def resolve(_kb_ref: str) -> AuthorizedKnowledgeSource:
        nonlocal resolution_count
        resolution_count += 1
        if resolution_count == 1:
            return resource
        raise OSError(leaked_path)

    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(),
        knowledge_resolver=resolve,
        rag_service_factory=lambda _source: _RagService(
            {
                "provider": "llamaindex",
                "sources": [
                    {
                        "chunk_id": "chunk",
                        "content": "Grounded context.",
                        "source": "book.pdf",
                    }
                ],
            }
        ),
    )

    with pytest.raises(SourceAccessDenied) as captured:
        await builder.from_kb(RESOURCE_ID, _request())

    assert str(captured.value) == "knowledge source changed during retrieval"
    assert leaked_path not in str(captured.value)


@pytest.mark.asyncio
async def test_generation_flip_during_retrieval_is_rejected(tmp_path: Path) -> None:
    original = _resource(tmp_path)
    replacement = AuthorizedKnowledgeSource(
        resource_id="user:kb:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        generation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        name=original.name,
        source="user",
        resource_owner_id="teacher-a",
        read_only=True,
        retrieval_provider="llamaindex",
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
async def test_provider_flip_during_retrieval_is_rejected(tmp_path: Path) -> None:
    resolutions = iter(
        (
            _resource(tmp_path, provider="llamaindex"),
            _resource(tmp_path, provider="lightrag"),
        )
    )
    repository = _Repository()
    rag = _RagService(
        {
            "provider": "llamaindex",
            "sources": [
                {
                    "chunk_id": "chunk",
                    "content": "Grounded context.",
                    "source": "book.pdf",
                }
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


@pytest.mark.asyncio
async def test_pdf_pipe_construction_failure_releases_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Slot:
        releases = 0

        def release(self) -> None:
            self.releases += 1

    class _Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            raise RuntimeError("C:/private/pipe failure")

    slot = _Slot()

    async def acquire(_deadline: float) -> None:
        return None

    monkeypatch.setattr(
        "deeptutor.teaching.services.sources._acquire_pdf_inspection_slot",
        acquire,
    )
    monkeypatch.setattr(
        "deeptutor.teaching.services.sources._PDF_INSPECTION_SLOTS",
        slot,
    )
    monkeypatch.setattr(
        source_snapshots_module.multiprocessing,
        "get_context",
        lambda _method: _Context(),
    )

    with pytest.raises(SourceSnapshotUnavailable, match="could not be started") as exc_info:
        await _run_pdf_extraction_process(tmp_path / "private.pdf")

    assert "private" not in str(exc_info.value)
    assert slot.releases == 1


@pytest.mark.asyncio
async def test_pdf_process_construction_failure_closes_pipe_and_releases_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Endpoint:
        closed = False

        def close(self) -> None:
            self.closed = True

    class _Slot:
        releases = 0

        def release(self) -> None:
            self.releases += 1

    receiver = _Endpoint()
    sender = _Endpoint()

    class _Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return receiver, sender

        def Process(self, **_kwargs):
            raise RuntimeError("E:/secret/process failure")

    slot = _Slot()

    async def acquire(_deadline: float) -> None:
        return None

    monkeypatch.setattr(
        "deeptutor.teaching.services.sources._acquire_pdf_inspection_slot",
        acquire,
    )
    monkeypatch.setattr(
        "deeptutor.teaching.services.sources._PDF_INSPECTION_SLOTS",
        slot,
    )
    monkeypatch.setattr(
        source_snapshots_module.multiprocessing,
        "get_context",
        lambda _method: _Context(),
    )

    with pytest.raises(SourceSnapshotUnavailable, match="could not be started") as exc_info:
        await _run_pdf_extraction_process(tmp_path / "private.pdf")

    assert "secret" not in str(exc_info.value)
    assert receiver.closed and sender.closed
    assert slot.releases == 1


@pytest.mark.asyncio
async def test_pdf_worker_selects_relevant_pages_after_old_400k_cutoff(
    tmp_path: Path,
) -> None:
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "late-relevant.pdf"
    document = fitz.open()
    try:
        filler_line = "administrative scheduling material " * 3
        for _ in range(210):
            page = document.new_page(width=612, height=792)
            for line in range(35):
                page.insert_text((20, 20 + line * 20), filler_line, fontsize=8)
        page = document.new_page(width=612, height=792)
        page.insert_text(
            (20, 40),
            "Photosynthesis uses chlorophyll to convert light into chemical energy.",
            fontsize=10,
        )
        document.save(pdf)
    finally:
        document.close()

    selected = await _run_pdf_extraction_process(
        pdf,
        query="Explain photosynthesis and chlorophyll",
    )

    assert "Photosynthesis uses chlorophyll" in selected
    assert "administrative scheduling" not in selected
    assert len(selected) <= 10_000


def test_pdf_fragments_are_query_selected_and_strictly_bounded() -> None:
    pages = [
        "--- Page 1 ---\nPRIVATE student medical notes must stay outside teaching context.",
        *(f"--- Page {page} ---\nUnrelated administrative material {page}." for page in range(2, 27)),
        (
            "--- Page 27 ---\nPhotosynthesis uses chlorophyll to convert light energy "
            "into chemical energy.\n\n" + "Relevant detail. " * 400
        ),
        "--- Page 28 ---\nUnrelated appendix.",
    ]

    fragments, references = _pdf_fragments(
        "pdf-source-a",
        "\n".join(pages),
        query="Explain photosynthesis and chlorophyll",
    )

    assert {reference.page for reference in references} == {27}
    assert all("PRIVATE" not in fragment.text for fragment in fragments)
    assert len(fragments) <= 8
    assert all(len(fragment.text) <= 2_000 for fragment in fragments)
    assert sum(len(fragment.text) for fragment in fragments) <= 8_000


@pytest.mark.asyncio
async def test_query_hash_changes_snapshot_identity_when_fragments_are_identical(
    tmp_path: Path,
) -> None:
    repository = _Repository()
    rag = _RagService(
        {
            "provider": "llamaindex",
            "index_signature": "idx-v1",
            "sources": [
                {
                    "chunk_id": "same",
                    "content": "The same grounded passage.",
                    "source": "book.pdf",
                }
            ],
        }
    )
    builder = SourceSnapshotBuilder(
        _context(),
        repository,
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path),
        rag_service_factory=lambda _source: rag,
    )

    first = await builder.from_kb(RESOURCE_ID, _request())
    second = await builder.from_kb(
        RESOURCE_ID,
        SnapshotRequest("course-a", "class-a", "Explain a different objective"),
    )

    assert first.snapshot_id != second.snapshot_id
    manifests = [json.loads(item[1].citation_manifest) for item in repository.persisted]
    assert manifests[0]["query_sha256"] != manifests[1]["query_sha256"]
    assert "Explain" not in repository.persisted[0][1].citation_manifest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,expected",
    (
        (
            {
                "provider": "lightrag",
                "content": "Local context-only passage.",
                "sources": [],
            },
            "Local context-only passage.",
        ),
        (
            {
                "provider": "lightrag-server",
                "content": "Remote context-only passage.",
                "sources": [{"id": "1", "file_path": "/docs/a.pdf"}],
            },
            "Remote context-only passage.",
        ),
    ),
)
async def test_real_lightrag_provider_shapes_produce_grounded_snapshots(
    tmp_path: Path,
    result: dict[str, object],
    expected: str,
) -> None:
    result["content_kind"] = "retrieval_context"
    result["retrieval_provenance"] = result.get("sources") or [
        {"kind": "local_retrieval_view", "storage_view": "version-7"}
    ]
    result["retrieval_view_signature"] = _context_view_signature(result)
    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(),
        knowledge_resolver=lambda _kb_ref: _resource(
            tmp_path,
            provider=str(result["provider"]),
        ),
        rag_service_factory=lambda _source: _RagService(result),
    )

    snapshot = await builder.from_kb(RESOURCE_ID, _request())

    assert snapshot.fragments[0].text == expected


@pytest.mark.asyncio
async def test_answer_shaped_lightrag_content_without_context_marker_fails_closed(
    tmp_path: Path,
) -> None:
    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(),
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path, provider="lightrag"),
        rag_service_factory=lambda _source: _RagService(
            {
                "provider": "lightrag",
                "answer": "Model-generated answer.",
                "content": "Model-generated answer.",
                "sources": [],
            }
        ),
    )

    with pytest.raises(SourceSnapshotUnavailable, match="grounded fragments"):
        await builder.from_kb(RESOURCE_ID, _request())


@pytest.mark.asyncio
async def test_context_only_provider_without_provenance_fails_actionably(
    tmp_path: Path,
) -> None:
    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(),
        knowledge_resolver=lambda _kb_ref: _resource(
            tmp_path,
            provider="lightrag-server",
        ),
        rag_service_factory=lambda _source: _RagService(
            {
                "provider": "lightrag-server",
                "content_kind": "retrieval_context",
                "content": "Context from an old server without references.",
                "sources": [],
            }
        ),
    )

    with pytest.raises(SourceSnapshotUnavailable, match="traceable provenance"):
        await builder.from_kb(RESOURCE_ID, _request())


@pytest.mark.asyncio
async def test_pageindex_title_only_shape_builds_grounded_snapshot(tmp_path: Path) -> None:
    result = {
        "provider": "pageindex",
        "content_kind": "retrieval_context",
        "content": "## a.pdf\n- Title without summary (p.7)",
        "sources": [
            {
                "title": "Title without summary",
                "content": "Title without summary",
                "source": "a.pdf",
                "page": 7,
                "chunk_id": "n1",
            }
        ],
    }
    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(),
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path, provider="pageindex"),
        rag_service_factory=lambda _source: _RagService(result),
    )

    snapshot = await builder.from_kb(RESOURCE_ID, _request())

    assert snapshot.retrieval_provider == "pageindex"
    assert snapshot.fragments[0].text == "Title without summary"
    assert snapshot.retrieval_view_signature


@pytest.mark.asyncio
async def test_claimed_retrieval_view_signature_cannot_hide_content_flip(
    tmp_path: Path,
) -> None:
    result = {
        "provider": "llamaindex",
        "retrieval_view_signature": hashlib.sha256(b"original context").hexdigest(),
        "sources": [
            {
                "chunk_id": "chunk-a",
                "content": "Tampered replacement context.",
                "source": "book.pdf",
            }
        ],
    }
    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(),
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path),
        rag_service_factory=lambda _source: _RagService(result),
    )

    with pytest.raises(SourceAccessDenied, match="retrieval view changed"):
        await builder.from_kb(RESOURCE_ID, _request())


@pytest.mark.asyncio
async def test_kb_search_error_is_mapped_without_leaking_local_path(tmp_path: Path) -> None:
    class _FailingRag:
        async def search(self, query: str, kb_name: str):
            raise RuntimeError("failed at C:/private/kb/index.json")

    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(),
        knowledge_resolver=lambda _kb_ref: _resource(tmp_path),
        rag_service_factory=lambda _source: _FailingRag(),
    )

    with pytest.raises(SourceSnapshotUnavailable, match="knowledge retrieval failed") as exc_info:
        await builder.from_kb(RESOURCE_ID, _request())

    assert "private" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_pdf_store_error_and_cleanup_error_do_not_leak_or_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = BoundSourceRecord(
        binding_id="pdf-binding-a",
        snapshot_id="pdf-source-a",
        source_type="pdf",
        source_id="upload-a",
        resource_owner_id="tenant-workspace",
        source_revision="pdf-v1",
        content_sha256="1" * 64,
        permission_sha256="2" * 64,
        display_name="mechanics.pdf",
        upload_id="upload-a",
        object_key="tenants/tenant-a/sources/upload-a/private.pdf",
    )

    class _Store:
        async def open(self, _key: str):
            raise RuntimeError("S3 tenants/tenant-a/sources/upload-a/private.pdf")

    class _StoreProvider:
        async def store_for_tenant(self, _tenant_id: str):
            return _Store()

    async def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("C:/private/temp.pdf")

    monkeypatch.setattr(source_snapshots_module.asyncio, "to_thread", fail_cleanup)
    builder = SourceSnapshotBuilder(
        _context(),
        _Repository(bound),
        store_provider=_StoreProvider(),
    )

    with pytest.raises(SourceSnapshotUnavailable, match="PDF source could not be read") as exc_info:
        await builder.from_pdf("pdf-binding-a", _request())

    assert "private" not in str(exc_info.value)
