from __future__ import annotations

import importlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def api(monkeypatch, tmp_path):
    module = importlib.import_module("deeptutor.api.routers.ragflow_knowledge")
    from deeptutor.knowledge.ragflow.operations import OperationScope, OperationStore

    scope = OperationScope("owner-a", "workspace-a", "generation-a", "binding-a")
    bound = SimpleNamespace(
        scope=scope,
        store=OperationStore(tmp_path / "operations.sqlite3"),
        config=None,
        resource=SimpleNamespace(id="user:kb:generation-a", name="Course", read_only=False),
        binding={
            "dataset_id": "private-dataset",
            "ownership": "attached",
            "access_mode": "read_only",
        },
        recheck=lambda: None,
        recovery_snapshot=lambda: {"actor_id": "owner-a"},
    )
    remote = SimpleNamespace()

    async def docs(dataset_id, **params):
        return {
            "docs": [
                {
                    "id": "private-doc",
                    "dataset_id": "private-dataset",
                    "name": "C:\\secret\\book.pdf",
                    "run": "DONE",
                    "chunk_count": 1,
                    "progress": 1,
                    "progress_msg": "secret http://internal/?api_key=password",
                    "location": "private-store",
                }
            ],
            "total": 1,
        }

    remote.list_documents = docs

    async def chunks(*args, **kwargs):
        return {
            "chunks": [
                {
                    "id": "chunk-a",
                    "document_id": "private-doc",
                    "available": True,
                    "content": "lesson",
                }
            ],
            "total": 1,
        }

    remote.list_chunks = chunks
    monkeypatch.setattr(module, "resolve_binding", lambda *args, **kwargs: bound)
    monkeypatch.setattr(module, "assert_writable", lambda *args, **kwargs: bound.resource)
    monkeypatch.setattr(module, "RagflowClient", lambda _: remote)
    app = FastAPI()
    app.include_router(module.router, prefix="/knowledge")
    return module, bound, remote, TestClient(app)


def test_document_list_uses_scoped_refs_and_sanitizes_remote_output(api):
    _, bound, _, client = api
    response = client.get("/knowledge/Course/documents")
    assert response.status_code == 200
    item = response.json()["documents"][0]
    assert item["document_ref"].startswith("doc_")
    assert item["name"] == "book.pdf"
    assert item["visibility"] == "draft"
    assert bound.store.resolve_document(bound.scope, item["document_ref"]) == "private-doc"
    assert "private-dataset" not in response.text
    assert "private-doc" not in response.text
    assert "password" not in response.text
    assert "private-store" not in response.text


@pytest.mark.parametrize(
    "read_only,access_mode,can_manage,can_write",
    [
        (False, "read_only", True, False),
        (False, "manage_documents", True, True),
        (True, "manage_documents", False, False),
    ],
)
def test_binding_view_exposes_capabilities_without_remote_identity(
    api, read_only, access_mode, can_manage, can_write
):
    _, bound, _, client = api
    bound.resource.read_only = read_only
    bound.binding.update(access_mode=access_mode, lifecycle_state="active")
    response = client.get("/knowledge/Course/ragflow/binding")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == bound.resource.id
    assert body["ownership"] == "attached"
    assert body["can_manage"] is can_manage
    assert body["can_write"] is can_write
    assert body["can_reconcile"] is can_write
    assert body["lifecycle_state"] == "active"
    assert "private-dataset" not in response.text
    assert "profile" not in response.text
    assert "api_key" not in response.text


def test_provisioning_binding_can_reconcile_but_cannot_edit_remote_content(api):
    _, bound, _, client = api
    bound.binding.update(access_mode="manage_documents", lifecycle_state="provisioning")
    response = client.get("/knowledge/Course/ragflow/binding")
    assert response.status_code == 200
    assert response.json()["can_reconcile"] is True
    assert response.json()["can_write"] is False


def test_binding_capabilities_obey_fresh_write_resolution(api, monkeypatch):
    from fastapi import HTTPException

    module, bound, _, client = api
    bound.binding.update(access_mode="manage_documents", lifecycle_state="active")

    def resolve(*args, require_write=False):
        if require_write:
            raise HTTPException(409, "duplicate binding")
        return bound

    monkeypatch.setattr(module, "resolve_binding", resolve)
    response = client.get("/knowledge/Course/ragflow/binding")
    assert response.status_code == 200
    assert response.json()["can_reconcile"] is False
    assert response.json()["can_write"] is False


def test_web_import_is_disabled_without_remote_egress_control(api):
    _, _, _, client = api
    response = client.post(
        "/knowledge/Course/documents",
        json={"name": "web", "type": "web", "url": "http://127.0.0.1/admin"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "remote_web_ingestion_disabled"


def test_unknown_document_reference_is_not_forwarded(api):
    _, _, _, client = api
    response = client.get("/knowledge/Course/documents/private-doc")
    assert response.status_code == 404


def test_document_detail_includes_editable_fields_but_not_internal_metadata(api):
    module, bound, remote, client = api
    row = bound.store.register_document(bound.scope, "private-doc")

    async def details(*args, **kwargs):
        return {
            "docs": [
                {
                    "id": "private-doc",
                    "name": "lesson.txt",
                    "run": "DONE",
                    "meta_fields": {"subject": "math", "yfeistai_generation": "private-generation"},
                    "chunk_method": "naive",
                    "parser_config": {"chunk_token_num": 512},
                    "location": "private-store",
                }
            ],
            "total": 1,
        }

    remote.list_documents = details
    response = client.get(f"/knowledge/Course/documents/{row['document_ref']}")
    assert response.status_code == 200
    assert response.json()["meta_fields"] == {"subject": "math"}
    assert response.json()["chunk_method"] == "naive"
    assert response.json()["parser_config"] == {"chunk_token_num": 512}
    assert "private-generation" not in response.text
    assert "private-store" not in response.text


def test_visibility_management_does_not_require_remote_write(api):
    _, bound, _, client = api
    row = bound.store.register_document(bound.scope, "private-doc")
    response = client.put(
        f"/knowledge/Course/documents/{row['document_ref']}/visibility",
        json={"visibility": "active", "expected_revision": row["revision"]},
    )
    assert response.status_code == 200
    assert bound.store.visibility_snapshot(bound.scope)[1] == ["private-doc"]


@pytest.mark.parametrize("visibility", ["draft", "disabled", "archived"])
def test_emergency_unpublish_does_not_depend_on_remote_availability(api, visibility):
    _, bound, remote, client = api
    from deeptutor.services.rag.pipelines.ragflow.client import RagflowError

    row = bound.store.register_document(bound.scope, "private-doc", visibility="active")
    calls = []

    async def offline(*args, **kwargs):
        calls.append("remote")
        raise RagflowError("transport_error")

    remote.list_documents = offline
    response = client.put(
        f"/knowledge/Course/documents/{row['document_ref']}/visibility",
        json={"visibility": visibility, "expected_revision": row["revision"]},
    )
    assert response.status_code == 200
    assert bound.store.visibility_snapshot(bound.scope)[1] == []
    assert calls == []


def test_visibility_requires_local_management_permission(api, monkeypatch):
    module, bound, _, client = api
    from fastapi import HTTPException

    row = bound.store.register_document(bound.scope, "private-doc")

    def denied(*args, **kwargs):
        raise HTTPException(403, "read only")

    monkeypatch.setattr(module, "assert_writable", denied)
    response = client.put(
        f"/knowledge/Course/documents/{row['document_ref']}/visibility",
        json={"visibility": "active", "expected_revision": row["revision"]},
    )
    assert response.status_code == 403
    assert bound.store.visibility_snapshot(bound.scope)[1] == []


def test_operation_status_never_returns_request_or_remote_receipt(api):
    _, bound, _, client = api
    op = bound.store.create_operation(
        bound.scope,
        action="upload",
        idempotency_key="k",
        payload={"spool_path": "C:/private", "document_id": "private-doc"},
    )
    response = client.get(f"/knowledge/Course/operations/{op['operation_id']}")
    assert response.status_code == 200
    assert "spool_path" not in response.text and "private-doc" not in response.text


def test_read_only_attached_cannot_delete_remote_dataset(api):
    _, _, _, client = api
    response = client.request(
        "DELETE", "/knowledge/Course/ragflow/dataset", json={"confirm_name": "Course"}
    )
    assert response.status_code == 403


def test_activation_checks_available_content_not_just_document_count(api):
    _, bound, remote, client = api
    row = bound.store.register_document(bound.scope, "private-doc")

    async def unavailable(*args, **kwargs):
        return {
            "chunks": [
                {
                    "id": "chunk-a",
                    "document_id": "private-doc",
                    "available": False,
                    "content": "lesson",
                }
            ],
            "total": 1,
        }

    remote.list_chunks = unavailable
    response = client.put(
        f"/knowledge/Course/documents/{row['document_ref']}/visibility",
        json={"visibility": "active", "expected_revision": 1},
    )
    assert response.status_code == 409
    assert bound.store.visibility_snapshot(bound.scope)[1] == []


def test_retrieval_test_rejects_scope_override(api):
    _, _, _, client = api
    response = client.post(
        "/knowledge/Course/retrieval-test", json={"query": "lesson", "dataset_ids": ["another"]}
    )
    assert response.status_code == 422


def test_retrieval_test_supports_explicit_draft_preview(api, monkeypatch):
    _, bound, _, client = api
    from deeptutor.services.rag.pipelines.ragflow.pipeline import RagflowPipeline

    called = []

    async def search(self, current, query, *, preview_document_refs=None):
        called.append((current.scope, query, preview_document_refs))
        return {"sources": [], "content": ""}

    monkeypatch.setattr(RagflowPipeline, "search_bound", search)
    response = client.post(
        "/knowledge/Course/retrieval-test",
        json={"query": "lesson", "preview_document_refs": ["doc_selected"]},
    )
    assert response.status_code == 200
    assert called == [(bound.scope, "lesson", ["doc_selected"])]


def test_upload_rejects_executable_before_accepting_operation(api):
    _, bound, _, client = api
    bound.binding["access_mode"] = "manage_documents"
    response = client.post(
        "/knowledge/Course/documents/upload",
        files={"files": ("run.exe", b"payload", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert bound.store.list_operations(bound.scope) == []


def test_retrieval_settings_rejects_unsafe_provider_switches(api):
    _, _, _, client = api
    response = client.put("/knowledge/Course/retrieval-settings", json={"use_kg": True})
    assert response.status_code == 422


def test_retrieval_settings_updates_only_current_binding(api):
    _, bound, _, client = api
    bound.entry = {"generation_id": "generation-a", "ragflow": bound.binding}
    bound.resource.generation_id = "generation-a"
    data = {"knowledge_bases": {"Course": bound.entry}}
    bound.manager = SimpleNamespace(_mutate_config=lambda mutate: mutate(data))
    response = client.put(
        "/knowledge/Course/retrieval-settings", json={"page_size": 5, "similarity_threshold": 0.4}
    )
    assert response.status_code == 200
    assert data["knowledge_bases"]["Course"]["ragflow"]["retrieval"]["page_size"] == 5
    assert data["knowledge_bases"]["Course"]["ragflow"]["dataset_id"] == "private-dataset"


def test_manual_reconciliation_requires_revision_and_refuses_foreign_operation(api):
    _, bound, _, client = api
    bound.binding["access_mode"] = "manage_documents"
    response = client.post(
        "/knowledge/Course/operations/missing/reconcile", json={"expected_revision": 1}
    )
    assert response.status_code == 404
    response = client.post("/knowledge/Course/operations/missing/reconcile", json={})
    assert response.status_code == 422


def test_manual_reconciliation_only_authorizes_readback(api, monkeypatch):
    module, bound, _, client = api
    bound.binding["access_mode"] = "manage_documents"
    snapshot = {
        "actor_id": "owner-a",
        "actor_role": "user",
        "kb_ref": "user:kb:generation-a",
        "generation_id": "generation-a",
        "resource_owner_id": "owner-a",
        "workspace_scope": "workspace-a",
        "binding_fingerprint": "fresh-binding",
        "require_write": True,
    }
    bound.recovery_snapshot = lambda: snapshot
    op = bound.store.create_operation(
        bound.scope,
        action="delete_document",
        idempotency_key="repair",
        payload={"binding_snapshot": snapshot, "request": {"document_id": "private-doc"}},
    )
    op = bound.store.claim(bound.scope, op["operation_id"], "old-worker")
    for state in ("SEND_STARTED", "NEEDS_ATTENTION"):
        op = bound.store.transition(
            bound.scope,
            op["operation_id"],
            worker_id="old-worker",
            expected_revision=op["revision"],
            state=state,
        )
    calls = []

    async def readback(current, operation_id):
        calls.append(current.store.get_operation(current.scope, operation_id)["state"])

    monkeypatch.setattr(module, "reconcile_operation", readback)
    response = client.post(
        f"/knowledge/Course/operations/{op['operation_id']}/reconcile",
        json={"expected_revision": op["revision"]},
    )
    assert response.status_code == 202
    assert calls == ["RESULT_UNKNOWN"]
    assert bound.store.get_operation(bound.scope, op["operation_id"])["payload"]["request"] == {
        "document_id": "private-doc"
    }
