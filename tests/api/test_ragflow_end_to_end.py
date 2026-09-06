"""HTTP composition contract; RAGFlow is simulated, local authorization is real."""

from __future__ import annotations

from collections import Counter
import json
import re

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from deeptutor.api.routers import auth as auth_router
from deeptutor.api.routers import ragflow_knowledge as router
from deeptutor.knowledge.manager import KnowledgeBaseManager
from deeptutor.knowledge.ragflow.binding import resolve_binding
from deeptutor.knowledge.ragflow.lifecycle import execute_operation
from deeptutor.knowledge.ragflow.operations import OperationStore
from deeptutor.knowledge.ragflow.profiles import ProfileStore
from deeptutor.multi_user import knowledge_access, paths
from deeptutor.multi_user.paths import local_admin_user, user_context
from deeptutor.services import auth
from deeptutor.services.rag.pipelines.ragflow.client import RagflowClient


@pytest.fixture
def local_ragflow(tmp_path, monkeypatch):
    """Redirect every owner/runtime store while preserving its implementation."""
    data_root = tmp_path / "data"
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(paths, "ADMIN_WORKSPACE_ROOT", data_root)
    monkeypatch.setattr(paths, "USERS_ROOT", data_root / "users")
    monkeypatch.setattr(paths, "SYSTEM_ROOT", data_root / "system")
    monkeypatch.setattr(paths, "LEGACY_MULTI_USER_ROOT", tmp_path / "legacy")
    monkeypatch.setattr(paths, "_legacy_migration_done", True)
    monkeypatch.setattr(paths, "_path_services", {})
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    knowledge_access._manager_for.cache_clear()
    manager = KnowledgeBaseManager(base_dir=data_root / "knowledge_bases")
    monkeypatch.setattr(knowledge_access, "admin_kb_manager", lambda: manager)
    monkeypatch.setattr(knowledge_access, "admin_kb_base_dir", lambda: manager.base_dir)
    monkeypatch.setattr(knowledge_access, "current_kb_manager", lambda: manager)
    monkeypatch.setattr(router, "current_kb_manager", lambda: manager)
    try:
        yield manager
    finally:
        knowledge_access._manager_for.cache_clear()


class SimulatedRagflow:
    dataset_id = "remote-dataset-1"
    document_id = "remote-document-1"
    chunk_id = "remote-chunk-1"
    text = "A triangle has three sides."
    api_key = "fixture-service-key-do-not-expose"

    def __init__(self):
        self.calls = Counter()
        self.doc = None
        self.chunks = []
        self.retrieval_payloads = []
        self.upload_bodies = []

    @staticmethod
    def response(data):
        return httpx.Response(200, json={"code": 0, "data": data})

    def __call__(self, request):
        assert request.url.host == "ragflow.example"
        assert request.headers["Authorization"] == f"Bearer {self.api_key}"
        method, path = request.method, request.url.path
        self.calls[(method, path)] += 1
        document_path = f"/api/v1/datasets/{self.dataset_id}/documents"
        chunk_path = f"{document_path}/{self.document_id}/chunks"
        if method == "GET" and path == "/api/v1/datasets":
            return self.response([{"id": self.dataset_id, "name": "Geometry"}])
        if method == "POST" and path == document_path:
            body = request.read()
            self.upload_bodies.append(body)
            assert self.text.encode() in body
            filename = re.search(rb'filename="([^"\r\n]+)"', body).group(1).decode()
            assert filename.startswith("yfs-") and filename.endswith("lesson.txt")
            self.doc = {
                "id": self.document_id,
                "dataset_id": self.dataset_id,
                "name": filename,
                "run": "UNSTART",
                "chunk_count": 0,
                "progress": 0,
                "meta_fields": {},
            }
            return self.response([self.doc])
        if method == "GET" and path == document_path:
            documents = [self.doc] if self.doc is not None else []
            for key in ("id", "name"):
                if request.url.params.get(key):
                    documents = [doc for doc in documents if doc[key] == request.url.params[key]]
            return self.response({"docs": documents, "total": len(documents)})
        if method == "PATCH" and path == f"{document_path}/{self.document_id}":
            patch = json.loads(request.read())
            assert patch["name"] == "lesson.txt"
            assert patch["meta_fields"]["yfeistai_source_kind"] == "local"
            assert len(patch["meta_fields"]["yfeistai_source_sha256"]) == 64
            self.doc.update(patch)
            return self.response(self.doc)
        if method == "POST" and path == f"/api/v1/datasets/{self.dataset_id}/chunks":
            assert json.loads(request.read()) == {"document_ids": [self.document_id]}
            self.doc.update(run="DONE", chunk_count=1, progress=1)
            self.chunks = [
                {
                    "id": self.chunk_id,
                    "dataset_id": self.dataset_id,
                    "document_id": self.document_id,
                    "available": True,
                    "content": self.text,
                }
            ]
            return self.response(None)
        if method == "GET" and path == chunk_path:
            return self.response({"chunks": self.chunks, "total": len(self.chunks)})
        if method == "GET" and path == f"{chunk_path}/{self.chunk_id}":
            return self.response(self.chunks[0])
        if method == "POST" and path == "/api/v1/retrieval":
            payload = json.loads(request.read())
            self.retrieval_payloads.append(payload)
            assert payload["dataset_ids"] == [self.dataset_id]
            assert payload["document_ids"] == [self.document_id]
            assert payload["use_kg"] is False
            assert payload["toc_enhance"] is False
            assert payload["include_knowledge_compilation"] is False
            return self.response(
                {
                    "chunks": [
                        {
                            "id": self.chunk_id,
                            "dataset_id": self.dataset_id,
                            "document_id": self.document_id,
                            "document_keyword": "lesson.txt",
                            "content": "Unverified aggregation must not become context.",
                            "similarity": 0.95,
                            "positions": [[1, 0, 10, 0, 10]],
                        }
                    ],
                    "total": 1,
                    "doc_aggs": [{"doc_id": self.document_id, "dataset_id": self.dataset_id}],
                }
            )
        raise AssertionError(f"Unexpected simulated RAGFlow request: {method} {path}")


def test_http_lifecycle_from_attachment_to_disabled_retrieval(local_ragflow, monkeypatch):
    remote = SimulatedRagflow()
    transport = httpx.MockTransport(remote)

    def factory(config):
        return RagflowClient(config, transport=transport)

    async def execute_with_transport(bound, operation_id):
        return await execute_operation(bound, operation_id, client_factory=factory)

    monkeypatch.setattr(router, "RagflowClient", factory)
    monkeypatch.setattr(router, "execute_operation", execute_with_transport)
    app = FastAPI()
    app.include_router(
        router.router, prefix="/knowledge", dependencies=[Depends(auth_router.require_auth)]
    )
    with TestClient(app) as client:
        configured = client.put(
            "/knowledge/rag-pipelines/ragflow/profiles/default",
            json={
                "base_url": "https://ragflow.example",
                "api_key": remote.api_key,
            },
        )
        assert configured.status_code == 200
        assert configured.json()["has_api_key"] is True
        assert remote.api_key not in configured.text
        listed = client.get("/knowledge/ragflow/datasets", params={"profile_id": "default"})
        assert listed.status_code == 200
        assert listed.json()["datasets"][0]["id"] == remote.dataset_id
        connected = client.post(
            "/knowledge/connect-ragflow",
            json={
                "name": "Geometry",
                "profile_id": "default",
                "dataset_id": remote.dataset_id,
                "selected_document_ids": [],
                "access_mode": "manage_documents",
            },
        )
        assert connected.status_code == 200, connected.text
        kb_ref = connected.json()["id"]
        assert kb_ref.startswith("admin:kb:")
        base = f"/knowledge/{kb_ref}"
        uploaded = client.post(
            base + "/documents/upload",
            headers={"Idempotency-Key": "upload-click"},
            files={"files": ("lesson.txt", remote.text.encode(), "text/plain")},
        )
        assert uploaded.status_code == 202, uploaded.text
        operation_id = uploaded.json()["operation_id"]
        upload_result = client.get(base + f"/operations/{operation_id}")
        assert upload_result.status_code == 200
        assert upload_result.json()["state"] == "RECONCILED", upload_result.text
        document_ref = upload_result.json()["result"]["document_ref"]
        assert document_ref.startswith("doc_")
        assert remote.document_id not in upload_result.text
        assert "spool_path" not in upload_result.text
        assert remote.api_key not in upload_result.text
        repeated = client.post(
            base + "/documents/upload",
            headers={"Idempotency-Key": "upload-click"},
            files={"files": ("lesson.txt", remote.text.encode(), "text/plain")},
        )
        assert repeated.status_code == 202
        assert repeated.json()["operation_id"] == operation_id
        assert remote.calls[("POST", f"/api/v1/datasets/{remote.dataset_id}/documents")] == 1

        document_url = base + f"/documents/{document_ref}"
        document = client.get(document_url).json()
        assert document["visibility"] == "draft"
        parsed = client.post(
            document_url + "/parse", json={}, headers={"Idempotency-Key": "parse-click"}
        )
        assert parsed.status_code == 202, parsed.text
        parse_id = parsed.json()["operation_id"]
        parse_result = client.get(base + f"/operations/{parse_id}")
        assert parse_result.status_code == 200
        assert parse_result.json()["state"] == "RECONCILED", parse_result.text
        assert parse_result.json()["result"]["document_status"] == "ready"
        assert remote.calls[("POST", f"/api/v1/datasets/{remote.dataset_id}/chunks")] == 1
        document = client.get(document_url).json()
        published = client.put(
            document_url + "/visibility",
            json={
                "visibility": "active",
                "expected_revision": document["revision"],
            },
        )
        assert published.status_code == 200, published.text
        retrieved = client.post(base + "/retrieval-test", json={"query": "How many sides?"})
        assert retrieved.status_code == 200, retrieved.text
        assert retrieved.json()["content"] == remote.text
        source = retrieved.json()["sources"][0]
        assert source["document_id"] == document_ref
        assert source["chunk_id"].startswith("chunk_")
        assert source["page"] == 1
        for private in (
            remote.api_key,
            remote.dataset_id,
            remote.document_id,
            remote.chunk_id,
            "https://ragflow.example",
        ):
            assert private not in retrieved.text
        requests_before_unpublish = remote.calls.copy()
        disabled = client.put(
            document_url + "/visibility",
            json={
                "visibility": "disabled",
                "expected_revision": published.json()["revision"],
            },
        )
        assert disabled.status_code == 200
        assert remote.calls == requests_before_unpublish
        empty = client.post(base + "/retrieval-test", json={"query": "How many sides?"})
        assert empty.status_code == 200
        assert empty.json()["sources"] == [] and empty.json()["content"] == ""
        assert remote.calls[("POST", "/api/v1/retrieval")] == 1
        assert remote.calls == requests_before_unpublish

    with user_context(local_admin_user()):
        bound = resolve_binding(kb_ref, require_write=True)
        assert isinstance(bound.manager, KnowledgeBaseManager)
        assert ProfileStore().load("local-admin", "default")["api_key"] == remote.api_key
        durable = OperationStore().get_operation(bound.scope, operation_id)
        assert durable["state"] == "RECONCILED"
        assert bound.store.visibility_snapshot(bound.scope)[1] == []
        assert bound.store.resolve_document(bound.scope, document_ref) == remote.document_id
