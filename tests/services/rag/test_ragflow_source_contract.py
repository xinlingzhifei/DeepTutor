"""Integration review: raw source provenance, profile snapshots, and tool failures."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
import httpx
import pytest

from deeptutor.knowledge.manager import KnowledgeBaseManager
from deeptutor.knowledge.ragflow.binding import resolve_binding
from deeptutor.knowledge.ragflow.profiles import ProfileStore
from deeptutor.multi_user import knowledge_access, paths
from deeptutor.multi_user.models import LOCAL_ADMIN_ID, CurrentUser, UserScope
from deeptutor.services.rag.pipelines.ragflow.client import RagflowClient
from deeptutor.services.rag.pipelines.ragflow.pipeline import RagflowPipeline


@pytest.fixture
def source_binding(tmp_path, monkeypatch):
    from deeptutor.services import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(paths, "SYSTEM_ROOT", tmp_path / "system")
    manager = KnowledgeBaseManager(base_dir=tmp_path / "kb")
    monkeypatch.setattr(knowledge_access, "admin_kb_manager", lambda: manager)
    monkeypatch.setattr(knowledge_access, "admin_kb_base_dir", lambda: manager.base_dir)
    actor = CurrentUser(
        LOCAL_ADMIN_ID, "admin", "admin", UserScope("admin", LOCAL_ADMIN_ID, tmp_path)
    )
    with paths.user_context(actor):
        profiles = ProfileStore()
        profiles.save(
            LOCAL_ADMIN_ID,
            "default",
            {"base_url": "https://ragflow.example", "api_key": "old-secret"},
        )
        manager.register_ragflow_kb(
            "course",
            "default",
            "dataset1",
            credential_owner_id=LOCAL_ADMIN_ID,
            ownership="managed",
            access_mode="manage_documents",
        )
        bound = resolve_binding("course")
        document = bound.store.register_document(
            bound.scope, "document1", visibility="active", title="course.pdf"
        )
        try:
            yield bound, document, profiles
        finally:
            knowledge_access._manager_for.cache_clear()


def test_profile_rotation_cannot_mix_fingerprint_and_client_configuration(
    source_binding,
    monkeypatch,
):
    _, _, profiles = source_binding
    before = profiles.load(LOCAL_ADMIN_ID, "default")
    profiles.save(LOCAL_ADMIN_ID, "default", {"api_key": "new-secret"})
    after = profiles.load(LOCAL_ADMIN_ID, "default")
    reads = []

    def rotating_load(self, owner_id, profile_id):
        reads.append((owner_id, profile_id))
        return dict(before if len(reads) == 1 else after)

    monkeypatch.setattr(ProfileStore, "load", rotating_load)
    # One bound object must represent one atomic profile version. A rotation
    # between two reads must not pair old identity with the new credential.
    bound = resolve_binding("course")
    assert bound.config.api_key == before["api_key"]
    assert len(reads) == 1


def test_ragflow_failure_is_not_reported_as_successful_rag_tool(monkeypatch):
    from deeptutor.tools import rag_tool
    from deeptutor.tools.builtin import RAGTool

    async def failed_search(**kwargs):
        return {
            "provider": "ragflow",
            "error_type": "auth_error",
            "sources": [],
            "answer": "RAGFlow authentication or access was rejected.",
            "content": "",
        }

    monkeypatch.setattr(rag_tool, "rag_search", failed_search)
    result = asyncio.run(RAGTool().execute(query="question", kb_name="course"))
    assert result.success is False
    assert result.sources == []


def test_ragflow_error_propagation_preserves_other_provider_tool_behavior(monkeypatch):
    from deeptutor.tools import rag_tool
    from deeptutor.tools.builtin import RAGTool

    async def legacy_search(**kwargs):
        return {"provider": "llamaindex", "error_type": "legacy_notice", "answer": "legacy"}

    monkeypatch.setattr(rag_tool, "rag_search", legacy_search)
    result = asyncio.run(RAGTool().execute(query="question", kb_name="course"))
    assert result.success is True
    assert result.sources == [{"type": "rag", "query": "question", "kb_name": "course"}]


@pytest.mark.parametrize(
    "override",
    [
        {"doc_id": "foreign_document"},
        {"kb_id": "foreign_dataset"},
        {"id": "different_chunk"},
        {"compile_kwd": "compiled-secret"},
    ],
)
def test_fresh_chunk_lookup_cannot_substitute_foreign_or_compiled_content(
    source_binding,
    override,
):
    bound, _, _ = source_binding

    def handler(request):
        if request.url.path.endswith("/retrieval"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "total": 1,
                        "chunks": [
                            {
                                "id": "chunk1",
                                "document_id": "document1",
                                "dataset_id": "dataset1",
                                "content": "candidate",
                                "similarity": 0.8,
                            }
                        ],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": "chunk1",
                    "doc_id": "document1",
                    "kb_id": ["dataset1"],
                    "available_int": 1,
                    "content_with_weight": "must never be disclosed",
                    **override,
                },
            },
        )

    pipeline = RagflowPipeline(
        client_factory=lambda config: RagflowClient(config, transport=httpx.MockTransport(handler))
    )
    result = asyncio.run(pipeline.search_bound(bound, "question"))
    assert result["error_type"] == "provider_incompatible"
    assert result["sources"] == []
    assert "must never" not in result["content"]


def test_v027_raw_chunk_fields_and_list_dataset_identity_are_accepted(source_binding):
    bound, document, _ = source_binding

    def handler(request):
        if request.url.path.endswith("/retrieval"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "total": 1,
                        "chunks": [
                            {
                                "id": "chunk1",
                                "document_id": "document1",
                                "dataset_id": ["dataset1"],
                                "content": "candidate",
                                "similarity": 0.8,
                            }
                        ],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": "chunk1",
                    "doc_id": "document1",
                    "kb_id": ["dataset1"],
                    "available_int": 1,
                    "content_with_weight": "verified current content",
                },
            },
        )

    pipeline = RagflowPipeline(
        client_factory=lambda config: RagflowClient(config, transport=httpx.MockTransport(handler))
    )
    result = asyncio.run(pipeline.search_bound(bound, "question"))
    assert result["content"] == "verified current content"
    assert result["sources"][0]["document_id"] == document["document_ref"]


def test_profile_rotation_between_retrieval_and_source_fetch_discards_content(source_binding):
    bound, _, profiles = source_binding
    requests = []

    def handler(request):
        requests.append(request.url.path)
        profiles.save(LOCAL_ADMIN_ID, "default", {"api_key": "new-secret"})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 1,
                    "chunks": [
                        {
                            "id": "chunk1",
                            "document_id": "document1",
                            "dataset_id": "dataset1",
                            "content": "stale secret",
                            "similarity": 0.8,
                        }
                    ],
                },
            },
        )

    pipeline = RagflowPipeline(
        client_factory=lambda config: RagflowClient(config, transport=httpx.MockTransport(handler))
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(pipeline.search_bound(bound, "question"))
    assert error.value.status_code == 409
    assert len(requests) == 1
