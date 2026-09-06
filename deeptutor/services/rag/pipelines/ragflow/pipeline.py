"""RAGFlow retrieval constrained by platform document visibility and resource ACLs."""

from __future__ import annotations

import math
from typing import Any

from fastapi import HTTPException

from deeptutor.knowledge.ragflow.binding import BoundRagflow, resolve_pipeline_binding
from deeptutor.multi_user import knowledge_access
from deeptutor.services.rag.retrieval_view import stamp_retrieval_view_signature

from .client import RagflowClient, RagflowError

_UNSAFE_OVERRIDES = {
    "dataset_ids",
    "document_ids",
    "use_kg",
    "toc_enhance",
    "include_knowledge_compilation",
    "metadata_condition",
    "preview_document_refs",
}
_MAX_ACTIVE_DOCUMENTS = 1000


def _incompatible() -> RagflowError:
    return RagflowError("provider_incompatible")


def _number(value: Any, default: float, low: float, high: float) -> float:
    value = default if value is None else value
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise _incompatible()
    return value


def _dataset_matches(value: Any, dataset_id: str) -> bool:
    return value == dataset_id or value == [dataset_id]


class RagflowPipeline:
    def __init__(self, kb_base_dir: str | None = None, *, client_factory=None, **_: Any):
        self.kb_base_dir = kb_base_dir
        self._client_factory = client_factory or RagflowClient

    async def search(self, query: str, kb_name: str, **kwargs) -> dict:
        if _UNSAFE_OVERRIDES.intersection(kwargs):
            raise HTTPException(400, "RAGFlow retrieval scope cannot be overridden")
        bound = resolve_pipeline_binding(kb_name, self.kb_base_dir)
        return await self.search_bound(bound, query)

    async def search_bound(
        self, bound: BoundRagflow, query: str, *, preview_document_refs: list[str] | None = None
    ) -> dict:
        """Management preview is explicit and never accepted from the ordinary rag Tool."""
        if not isinstance(query, str) or not query.strip() or len(query) > 20_000:
            raise HTTPException(400, "RAGFlow query must contain 1 to 20000 characters")
        bound.recheck()
        if not bound.binding.get("dataset_id"):
            raise HTTPException(409, "RAGFlow dataset is still being provisioned")
        revision, active = bound.store.visibility_snapshot(bound.scope)
        if preview_document_refs is not None:
            knowledge_access.resolve_kb(bound.resource.id, require_write=True)
            if not preview_document_refs or len(preview_document_refs) > _MAX_ACTIVE_DOCUMENTS:
                raise HTTPException(400, "Preview requires an explicit bounded document list")
            try:
                active = [
                    bound.store.resolve_document(bound.scope, ref) for ref in preview_document_refs
                ]
            except ValueError:
                raise HTTPException(404, "Preview document was not found") from None

        def recheck() -> None:
            bound.recheck()
            if bound.store.visibility_snapshot(bound.scope)[0] != revision:
                raise HTTPException(409, "RAGFlow document visibility changed during retrieval")

        empty = {
            "query": query,
            "content": "",
            "answer": "",
            "sources": [],
            "provider": "ragflow",
            "content_kind": "retrieval_context",
            "mode": "hybrid",
        }
        if not active:
            return empty
        if len(active) > _MAX_ACTIVE_DOCUMENTS:
            raise HTTPException(422, "RAGFlow active document scope exceeds the safe request limit")
        try:
            settings = bound.binding.get("retrieval") or {}
            page_size = settings.get("page_size", 8)
            if type(page_size) is not int or not 1 <= page_size <= 20:
                raise _incompatible()
            dataset_id = bound.binding["dataset_id"]
            payload = {
                "question": query,
                "dataset_ids": [dataset_id],
                "document_ids": active,
                "page": 1,
                "page_size": page_size,
                "similarity_threshold": _number(settings.get("similarity_threshold"), 0.2, 0, 1),
                "vector_similarity_weight": _number(
                    settings.get("vector_similarity_weight"), 0.3, 0, 1
                ),
                "use_kg": False,
                "toc_enhance": False,
                "include_knowledge_compilation": False,
                "keyword": False,
                "highlight": False,
            }
            if settings.get("rerank_id"):
                if not isinstance(settings["rerank_id"], str) or len(settings["rerank_id"]) > 256:
                    raise _incompatible()
                payload["rerank_id"] = settings["rerank_id"]
            client = self._client_factory(bound.config)
            recheck()
            result = await client.retrieve(payload)
            recheck()
            sources = await self._sources(bound, client, result, set(active), page_size, recheck)
            recheck()
            content = "\n\n".join(source["content"] for source in sources)
            return stamp_retrieval_view_signature(
                {**empty, "content": content, "answer": content, "sources": sources}
            )
        except RagflowError as error:
            recheck()
            return {**empty, "error_type": error.error_type, "answer": str(error)}

    async def _sources(self, bound, client, result, active, limit, recheck) -> list[dict]:
        dataset_id = bound.binding["dataset_id"]
        aggregations = result.get("doc_aggs", [])
        if not isinstance(aggregations, list):
            raise _incompatible()
        for aggregate in aggregations:
            if (
                not isinstance(aggregate, dict)
                or aggregate.get("doc_id", aggregate.get("document_id")) not in active
            ):
                raise _incompatible()
            if "dataset_id" in aggregate and not _dataset_matches(
                aggregate["dataset_id"], dataset_id
            ):
                raise _incompatible()
        chunks = result.get("chunks")
        if not isinstance(chunks, list):
            raise _incompatible()
        # Validate every candidate, including ones outside the displayed top-N.
        for chunk in chunks:
            if (
                not isinstance(chunk, dict)
                or chunk.get("document_id") not in active
                or not _dataset_matches(chunk.get("dataset_id"), dataset_id)
                or not isinstance(chunk.get("id"), str)
            ):
                raise _incompatible()
            _number(chunk.get("similarity"), 0, -1, 100)
        documents = {doc["remote_id"]: doc for doc in bound.store.list_documents(bound.scope)}
        sources = []
        for chunk in chunks[:limit]:
            recheck()
            remote = await client.get_chunk(dataset_id, chunk["document_id"], chunk["id"])
            recheck()
            if (
                remote.get("doc_id", remote.get("document_id")) != chunk["document_id"]
                or not _dataset_matches(remote.get("kb_id", remote.get("dataset_id")), dataset_id)
                or remote.get("id") != chunk["id"]
                or remote.get("compile_kwd")
            ):
                raise _incompatible()
            available = remote.get("available_int", remote.get("available"))
            if type(available) not in (int, bool) or available not in (0, 1):
                raise _incompatible()
            if not available:
                raise RagflowError("unavailable_chunk")
            text = remote.get("content_with_weight", remote.get("content"))
            if not isinstance(text, str) or "\x00" in text:
                raise _incompatible()
            if not text.strip():
                continue
            document = documents[chunk["document_id"]]
            title = str(document.get("title") or chunk.get("document_keyword") or "Document")
            title = (
                title.replace("\\", "/")
                .rsplit("/", 1)[-1]
                .replace("\r", " ")
                .replace("\n", " ")[:512]
            )
            source = {
                "document_id": document["document_ref"],
                "chunk_id": bound.store.register_chunk(
                    bound.scope, document["document_ref"], chunk["id"]
                ),
                "title": title,
                "content": text[:20_000],
                "score": _number(chunk.get("similarity"), 0, -1, 100),
            }
            positions = chunk.get("positions")
            if (
                isinstance(positions, list)
                and positions
                and isinstance(positions[0], (list, tuple))
                and positions[0]
            ):
                page = positions[0][0]
                if type(page) is int and page >= 1:
                    source["page"] = page
            if len(text) > 20_000:
                source["truncated"] = True
            sources.append(source)
        return sources

    async def initialize(self, kb_name: str, file_paths: list[str], **kwargs) -> bool:
        return await self.add_documents(kb_name, file_paths, **kwargs)

    async def add_documents(self, kb_name: str, file_paths: list[str], **kwargs) -> bool:
        from deeptutor.knowledge.ragflow.lifecycle import ingest_files

        bound = resolve_pipeline_binding(kb_name, self.kb_base_dir)
        return await ingest_files(bound, file_paths, client_factory=self._client_factory, **kwargs)

    async def delete(self, kb_name: str, **kwargs) -> bool:
        # The existing manager owns local pointer removal. Remote deletion is a
        # separate explicit, audited operation; generic cleanup cannot select it.
        bound = resolve_pipeline_binding(kb_name, self.kb_base_dir)
        knowledge_access.resolve_kb(bound.resource.id, require_write=True)
        bound.recheck()
        return True
