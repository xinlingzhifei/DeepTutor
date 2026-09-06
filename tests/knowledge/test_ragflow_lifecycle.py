from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pytest


def setup_bound(tmp_path, *, write=True):
    from deeptutor.knowledge.ragflow.operations import OperationScope, OperationStore

    scope = OperationScope("owner-a", "workspace-a", "generation-a", "binding-a")
    bound = SimpleNamespace(
        binding={
            "dataset_id": "dataset-a",
            "ownership": "managed",
            "access_mode": "manage_documents" if write else "read_only",
        },
        scope=scope,
        store=OperationStore(tmp_path / "operations.sqlite3"),
        config=None,
        resource=SimpleNamespace(id="user:kb:generation-a", name="Course", read_only=False),
        entry={"generation_id": "generation-a"},
        recheck=lambda: None,
        recovery_snapshot=lambda: {"actor_id": "owner-a", "kb_ref": "user:kb:generation-a"},
    )
    return bound


class Remote:
    def __init__(self):
        self.docs = [
            {
                "id": "doc-a",
                "dataset_id": "dataset-a",
                "name": "book.pdf",
                "run": "RUNNING",
                "chunk_count": 0,
            }
        ]
        self.sent = []

    async def list_documents(self, dataset_id, **params):
        docs = self.docs
        if params.get("id"):
            docs = [d for d in docs if d["id"] == params["id"]]
        if params.get("name"):
            docs = [d for d in docs if d["name"] == params["name"]]
        return {"docs": docs, "total": len(docs)}

    async def parse_documents(self, dataset_id, ids):
        self.sent.append(("parse", ids))

    async def create_document(self, dataset_id, *, name, url=None):
        self.sent.append(("create", name))
        from deeptutor.services.rag.pipelines.ragflow.client import RagflowError

        raise RagflowError("upstream_unavailable", outcome_unknown=True)

    async def delete_documents(self, dataset_id, ids):
        self.sent.append(("delete", ids))
        self.docs = [doc for doc in self.docs if doc["id"] not in ids]

    async def list_chunks(self, dataset_id, document_id, **params):
        return {
            "chunks": [
                {
                    "id": "chunk-a",
                    "document_id": document_id,
                    "available": True,
                    "content": "lesson",
                }
            ],
            "total": 1,
        }


def operation(bound, action, request):
    from deeptutor.knowledge.ragflow.lifecycle import record_operation

    return record_operation(bound, action, request, idempotency_key="click-1")


def test_parse_waits_for_remote_terminal_state(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import execute_operation, reconcile_operation

    bound, remote = setup_bound(tmp_path), Remote()
    op = operation(bound, "parse", {"document_id": "doc-a"})
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    current = bound.store.get_operation(bound.scope, op["operation_id"])
    assert current["state"] == "RECONCILING"
    assert remote.sent == [("parse", ["doc-a"])]
    remote.docs[0].update(run="DONE", chunk_count=0)
    asyncio.run(reconcile_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    current = bound.store.get_operation(bound.scope, op["operation_id"])
    assert current["state"] == "FAILED"
    assert current["error_type"] == "empty_document"


def test_unknown_create_is_not_replayed(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import execute_operation

    bound, remote = setup_bound(tmp_path), Remote()
    op = operation(bound, "create_document", {"name": "draft.txt"})
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    assert len(remote.sent) == 1
    assert bound.store.get_operation(bound.scope, op["operation_id"])["state"] in {
        "RESULT_UNKNOWN",
        "RECONCILING",
    }


def test_target_must_belong_to_bound_dataset(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import execute_operation

    bound, remote = setup_bound(tmp_path), Remote()
    op = operation(bound, "delete_document", {"document_id": "someone-elses-doc"})
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    assert remote.sent == []
    assert bound.store.get_operation(bound.scope, op["operation_id"])["state"] == "FAILED"


def test_revoke_after_read_prevents_mutation(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import execute_operation

    bound, remote = setup_bound(tmp_path), Remote()
    original = remote.list_documents

    async def revoked_read(*args, **kwargs):
        result = await original(*args, **kwargs)

        def revoked():
            raise HTTPException(403, "revoked")

        bound.recheck = revoked
        return result

    remote.list_documents = revoked_read
    op = operation(bound, "delete_document", {"document_id": "doc-a"})
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    assert not remote.sent
    assert bound.store.get_operation(bound.scope, op["operation_id"])["state"] == "FAILED"


def test_delete_success_requires_remote_absence(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import execute_operation

    bound, remote = setup_bound(tmp_path), Remote()
    op = operation(bound, "delete_document", {"document_id": "doc-a"})
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    assert bound.store.get_operation(bound.scope, op["operation_id"])["state"] == "RECONCILED"
    assert remote.sent == [("delete", ["doc-a"])]


def test_read_only_binding_rejects_operation_before_intent(tmp_path):
    bound = setup_bound(tmp_path, write=False)
    with pytest.raises(HTTPException) as exc:
        operation(bound, "delete_document", {"document_id": "doc-a"})
    assert exc.value.status_code == 403
    assert bound.store.list_operations(bound.scope) == []


def test_sdk_does_not_report_parse_acceptance_as_completed(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import RagflowOperationPending, ingest_files

    bound, remote = setup_bound(tmp_path), Remote()
    source = tmp_path / "sample.txt"
    source.write_text("lesson", encoding="utf-8")

    async def uploaded(dataset_id, path, *, filename):
        remote.docs[0].update(name=filename, run="UNSTART")
        return [dict(remote.docs[0])]

    async def updated(dataset_id, document_id, patch):
        remote.docs[0].update(patch)

    remote.upload_document, remote.update_document = uploaded, updated
    with pytest.raises(RagflowOperationPending, match="[a-f0-9]") as exc:
        asyncio.run(
            ingest_files(bound, [str(source)], client_factory=lambda _: remote, wait_timeout=0)
        )
    assert exc.value.operation_id
    assert any(op["action"] == "parse" for op in bound.store.list_operations(bound.scope))


def test_recovery_does_not_fall_back_to_admin_for_missing_actor(tmp_path, monkeypatch):
    from deeptutor.knowledge.ragflow import recovery

    bound = setup_bound(tmp_path)
    op = operation(bound, "create_document", {"name": "draft.txt"})

    def unavailable(snapshot):
        raise HTTPException(403, "deleted actor")

    monkeypatch.setattr(recovery, "restore_binding", unavailable)
    asyncio.run(recovery.recover_once(store=bound.store))
    current = bound.store.get_operation(bound.scope, op["operation_id"])
    assert current["state"] == "NEEDS_ATTENTION"
    assert current["error_type"] == "recovery_authorization_unavailable"


def test_cancel_can_be_accepted_while_parse_is_running(tmp_path):
    bound = setup_bound(tmp_path)
    operation(bound, "parse", {"document_id": "doc-a"})
    from deeptutor.knowledge.ragflow.lifecycle import record_operation

    cancelled = record_operation(
        bound, "cancel", {"document_id": "doc-a"}, idempotency_key="cancel-1"
    )
    assert cancelled["state"] == "INTENT_RECORDED"


def test_parse_done_with_only_disabled_chunks_is_not_ready(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import execute_operation

    bound, remote = setup_bound(tmp_path), Remote()
    remote.docs[0].update(run="DONE", chunk_count=1)

    async def disabled(*args, **kwargs):
        return {
            "chunks": [
                {"id": "chunk-a", "document_id": "doc-a", "available": False, "content": "lesson"}
            ],
            "total": 1,
        }

    remote.list_chunks = disabled
    op = operation(bound, "parse", {"document_id": "doc-a"})
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    current = bound.store.get_operation(bound.scope, op["operation_id"])
    assert current["state"] == "FAILED"
    assert current["error_type"] == "no_available_chunks"


def test_recovery_finishes_binding_activation_after_remote_create_was_recorded(
    tmp_path, monkeypatch
):
    from deeptutor.knowledge.ragflow import recovery

    bound = setup_bound(tmp_path)
    op = operation(bound, "create_dataset", {"name": "Course"})
    op = bound.store.claim(bound.scope, op["operation_id"], "worker")
    for state in ("SEND_STARTED", "ACKNOWLEDGED", "RECONCILED"):
        op = bound.store.transition(
            bound.scope,
            op["operation_id"],
            worker_id="worker",
            expected_revision=op["revision"],
            state=state,
            result={"dataset_id": "dataset-a"},
        )
    monkeypatch.setattr(recovery, "restore_activation_binding", lambda snapshot, dataset_id: bound)
    asyncio.run(recovery.recover_once(store=bound.store))
    assert (
        bound.store.get_operation(bound.scope, op["operation_id"])["result"]["binding_activated"]
        is True
    )


def test_sdk_repeat_idempotency_key_keeps_same_remote_upload(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import ingest_files

    bound, remote = setup_bound(tmp_path), Remote()
    source = tmp_path / "sample.txt"
    source.write_text("lesson", encoding="utf-8")

    async def uploaded(dataset_id, path, *, filename):
        remote.sent.append(("upload", filename))
        remote.docs[0].update(name=filename, run="UNSTART")
        return [dict(remote.docs[0])]

    async def updated(dataset_id, document_id, patch):
        remote.docs[0].update(patch)

    async def parsed(dataset_id, ids):
        remote.sent.append(("parse", ids))
        remote.docs[0].update(run="DONE", chunk_count=1)

    remote.upload_document, remote.update_document, remote.parse_documents = (
        uploaded,
        updated,
        parsed,
    )
    for _ in range(2):
        assert (
            asyncio.run(
                ingest_files(
                    bound,
                    [str(source)],
                    client_factory=lambda _: remote,
                    idempotency_key="same-file",
                )
            )
            is True
        )
    assert len([call for call in remote.sent if call[0] == "upload"]) == 1


def test_lost_create_receipt_does_not_skip_metadata_finalization(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import execute_operation
    from deeptutor.services.rag.pipelines.ragflow.client import RagflowError

    bound, remote = setup_bound(tmp_path), Remote()

    async def accepted_then_lost(dataset_id, *, name, url=None):
        remote.docs[0].update(name=name)
        remote.sent.append(("create", name))
        raise RagflowError("transport_error", outcome_unknown=True)

    remote.create_document = accepted_then_lost
    op = operation(bound, "create_document", {"name": "draft.txt"})
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    asyncio.run(execute_operation(bound, op["operation_id"], client_factory=lambda _: remote))
    current = bound.store.get_operation(bound.scope, op["operation_id"])
    assert current["state"] == "NEEDS_ATTENTION"
    assert current["error_type"] == "document_finalization_incomplete"
    assert len(remote.sent) == 1


def test_confirmed_parse_failure_releases_lock_for_an_explicit_retry(tmp_path):
    from deeptutor.knowledge.ragflow.lifecycle import execute_operation, record_operation

    bound, remote = setup_bound(tmp_path), Remote()
    remote.docs[0]["run"] = "FAIL"
    first = operation(bound, "parse", {"document_id": "doc-a"})
    asyncio.run(execute_operation(bound, first["operation_id"], client_factory=lambda _: remote))
    result = bound.store.get_operation(bound.scope, first["operation_id"])
    assert result["state"] == "FAILED"
    retry = record_operation(
        bound, "parse", {"document_id": "doc-a"}, idempotency_key="explicit-retry"
    )
    assert retry["operation_id"] != first["operation_id"]
