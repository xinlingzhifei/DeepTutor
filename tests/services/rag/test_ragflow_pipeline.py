from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from deeptutor.knowledge.manager import KnowledgeBaseManager
from deeptutor.multi_user import knowledge_access, paths
from deeptutor.multi_user.context import reset_current_user, set_current_user
from deeptutor.multi_user.models import CurrentUser, UserScope


@pytest.fixture
def binding(tmp_path, monkeypatch):
    from deeptutor.knowledge.ragflow.binding import resolve_binding
    from deeptutor.knowledge.ragflow.profiles import ProfileStore

    monkeypatch.setattr(paths, "SYSTEM_ROOT", tmp_path / "system")
    manager = KnowledgeBaseManager(base_dir=tmp_path / "kb")
    monkeypatch.setattr(knowledge_access, "admin_kb_manager", lambda: manager)
    monkeypatch.setattr(knowledge_access, "admin_kb_base_dir", lambda: manager.base_dir)
    from deeptutor.services import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    actor = CurrentUser(
        "local-admin", "admin", "admin", UserScope("admin", "local-admin", tmp_path)
    )
    token = set_current_user(actor)
    ProfileStore().save(
        "local-admin", "default", {"base_url": "https://ragflow.example", "api_key": "private-key"}
    )
    manager.register_ragflow_kb("course", "default", "dataset1", credential_owner_id="local-admin")
    bound = resolve_binding("course")
    doc = bound.store.register_document(
        bound.scope, "doc1", visibility="active", title="algebra.pdf"
    )
    try:
        yield bound, doc
    finally:
        reset_current_user(token)
        knowledge_access._manager_for.cache_clear()


def pipeline(binding, handler):
    from deeptutor.services.rag.pipelines.ragflow.client import RagflowClient
    from deeptutor.services.rag.pipelines.ragflow.pipeline import RagflowPipeline

    bound, _ = binding
    return RagflowPipeline(
        kb_base_dir=str(bound.resource.base_dir),
        client_factory=lambda config: RagflowClient(config, transport=httpx.MockTransport(handler)),
    )


def response_chunk(**overrides):
    return {
        "id": "chunk1",
        "document_id": "doc1",
        "dataset_id": "dataset1",
        "content": "x plus two",
        "document_keyword": "/private/algebra.pdf",
        "similarity": 0.8,
        "positions": [[2, 0, 1, 0, 1]],
        **overrides,
    }


def chunk_response():
    return httpx.Response(
        200,
        json={
            "code": 0,
            "data": {
                "id": "chunk1",
                "doc_id": "doc1",
                "kb_id": "dataset1",
                "available_int": 1,
                "content_with_weight": "x plus two",
            },
        },
    )


def test_retrieval_filters_active_docs_and_returns_only_opaque_sources(binding):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/retrieval"):
            body = json.loads(request.content)
            assert body["dataset_ids"] == ["dataset1"]
            assert body["document_ids"] == ["doc1"]
            assert (
                body["use_kg"]
                is body["toc_enhance"]
                is body["include_knowledge_compilation"]
                is False
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "total": 1,
                        "chunks": [response_chunk()],
                        "doc_aggs": [{"doc_id": "doc1", "count": 1}],
                    },
                },
            )
        return chunk_response()

    result = asyncio.run(pipeline(binding, handler).search("what", "course"))
    assert result["content"] == "x plus two"
    assert result["sources"][0]["document_id"] == binding[1]["document_ref"]
    assert result["sources"][0]["chunk_id"].startswith("chunk_")
    assert result["sources"][0]["title"] == "algebra.pdf"
    assert result["sources"][0]["page"] == 2
    serialized = json.dumps(result)
    assert all(
        secret not in serialized
        for secret in ("dataset1", "doc1", "chunk1", "private-key", "ragflow.example", "/private")
    )
    assert len(calls) == 2


def test_disabled_collection_does_not_issue_unfiltered_retrieval(binding):
    bound, doc = binding
    bound.store.set_visibility(bound.scope, doc["document_ref"], "disabled")

    def handler(request):
        pytest.fail("No active document must not call the remote service")

    result = asyncio.run(pipeline(binding, handler).search("what", "course"))
    assert result["sources"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"document_id": "draftdoc"},
        {"dataset_id": "otherdataset"},
        {"document_id": None},
        {"similarity": "invalid"},
    ],
)
def test_retrieval_rejects_unproven_or_foreign_sources(binding, overrides):
    def handler(request):
        return httpx.Response(
            200, json={"code": 0, "data": {"total": 1, "chunks": [response_chunk(**overrides)]}}
        )

    result = asyncio.run(pipeline(binding, handler).search("what", "course"))
    assert result["error_type"] == "provider_incompatible"
    assert result["sources"] == []


def test_visibility_change_while_retrieving_discards_late_content(binding):
    bound, doc = binding

    def handler(request):
        if request.url.path.endswith("/retrieval"):
            bound.store.set_visibility(bound.scope, doc["document_ref"], "disabled")
            return httpx.Response(
                200, json={"code": 0, "data": {"total": 1, "chunks": [response_chunk()]}}
            )
        return chunk_response()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        asyncio.run(pipeline(binding, handler).search("what", "course"))
    assert error.value.status_code == 409


def test_parent_chunk_availability_is_verified(binding):
    def handler(request):
        if request.url.path.endswith("/retrieval"):
            return httpx.Response(
                200, json={"code": 0, "data": {"total": 1, "chunks": [response_chunk()]}}
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": "chunk1",
                    "doc_id": "doc1",
                    "kb_id": "dataset1",
                    "available_int": 0,
                    "content_with_weight": "disabled parent secret",
                },
            },
        )

    result = asyncio.run(pipeline(binding, handler).search("what", "course"))
    assert result["sources"] == []
    assert "secret" not in result["content"]
    assert result["error_type"] == "unavailable_chunk"


def test_caller_cannot_override_dataset_or_enable_expansion(binding):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        asyncio.run(
            pipeline(binding, lambda req: pytest.fail("must reject before network")).search(
                "what", "course", use_kg=True
            )
        )
