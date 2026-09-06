from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from deeptutor.knowledge.ragflow.operations import (
    OperationConflict,
    OperationScope,
    OperationStore,
)


@pytest.fixture
def scope() -> OperationScope:
    return OperationScope("owner-a", "workspace-a", "generation-a", "binding-a")


@pytest.fixture
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "operations.sqlite3")


def create(store: OperationStore, scope: OperationScope, key: str = "request-1") -> dict:
    return store.create_operation(
        scope, action="upload", idempotency_key=key, payload={"name": "lecture.pdf"}
    )


def test_idempotency_survives_reopening_and_payload_key_order(
    tmp_path: Path, scope: OperationScope
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = OperationStore(path)
    first = store.create_operation(
        scope, action="upload", idempotency_key="same", payload={"a": 1, "b": 2}
    )
    restarted = OperationStore(path)
    repeated = restarted.create_operation(
        scope, action="upload", idempotency_key="same", payload={"b": 2, "a": 1}
    )
    assert first["operation_id"] == repeated["operation_id"]
    assert repeated["state"] == "INTENT_RECORDED"
    assert len(restarted.list_operations(scope)) == 1
    with pytest.raises(OperationConflict):
        restarted.create_operation(scope, action="upload", idempotency_key="same", payload={"a": 3})
    with pytest.raises(OperationConflict):
        restarted.create_operation(
            scope, action="delete", idempotency_key="same", payload={"a": 1, "b": 2}
        )


@pytest.mark.parametrize(
    "field", ["owner_id", "workspace_scope", "generation_id", "binding_fingerprint"]
)
def test_every_scope_boundary_is_enforced(
    store: OperationStore, scope: OperationScope, field: str
) -> None:
    op = create(store, scope)
    doc = store.register_document(scope, "remote-doc", visibility="active", title="same.pdf")
    chunk = store.register_chunk(scope, doc["document_ref"], "remote-chunk")
    other = replace(scope, **{field: "different"})
    assert store.get_operation(other, op["operation_id"]) is None
    assert store.list_operations(other) == []
    assert store.claim(other, op["operation_id"], "worker") is None
    assert store.events(other, op["operation_id"]) == []
    assert store.list_documents(other) == []
    assert store.visibility_snapshot(other) == (0, [])
    with pytest.raises(ValueError):
        store.resolve_document(other, doc["document_ref"])
    with pytest.raises(ValueError):
        store.resolve_chunk(other, doc["document_ref"], chunk)
    with pytest.raises(ValueError):
        store.set_visibility(other, doc["document_ref"], "active")
    assert create(store, other)["operation_id"] != op["operation_id"]


def test_claim_and_transition_require_exclusive_lease_and_revision(
    store: OperationStore, scope: OperationScope
) -> None:
    op = create(store, scope)
    claimed = store.claim(scope, op["operation_id"], "worker-a")
    assert claimed is not None
    assert claimed["state"] == "INTENT_RECORDED"
    assert claimed["lease_owner"] == "worker-a"
    assert claimed["lease_expires_at"] > claimed["updated_at"]
    assert store.claim(scope, op["operation_id"], "worker-b") is None
    with pytest.raises(OperationConflict):
        store.transition(
            scope,
            op["operation_id"],
            worker_id="worker-b",
            expected_revision=claimed["revision"],
            state="SEND_STARTED",
        )
    sent = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker-a",
        expected_revision=claimed["revision"],
        state="SEND_STARTED",
    )
    with pytest.raises(OperationConflict):
        store.transition(
            scope,
            op["operation_id"],
            worker_id="worker-a",
            expected_revision=claimed["revision"],
            state="ACKNOWLEDGED",
        )
    acknowledged = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker-a",
        expected_revision=sent["revision"],
        state="ACKNOWLEDGED",
        result={"remote_id": "doc-1"},
    )
    done = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker-a",
        expected_revision=acknowledged["revision"],
        state="RECONCILED",
    )
    assert done["result"] == {"remote_id": "doc-1"}
    assert done["lease_owner"] is None
    assert store.claim(scope, op["operation_id"], "worker-c") is None
    assert store.pending_operations() == []


def test_expired_send_is_reconciled_without_resending_after_restart(
    tmp_path: Path, scope: OperationScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeptutor.knowledge.ragflow import operations

    monkeypatch.setattr(operations.time, "time", lambda: 1000.0)
    path = tmp_path / "operations.sqlite3"
    store = OperationStore(path)
    op = create(store, scope)
    first = store.claim(scope, op["operation_id"], "old-worker", lease_seconds=10)
    sent = store.transition(
        scope,
        op["operation_id"],
        worker_id="old-worker",
        expected_revision=first["revision"],
        state="SEND_STARTED",
    )
    monkeypatch.setattr(operations.time, "time", lambda: 1011.0)
    restarted = OperationStore(path)
    pending = restarted.pending_operations()
    assert pending[0]["scope"] == {
        "owner_id": scope.owner_id,
        "workspace_scope": scope.workspace_scope,
        "generation_id": scope.generation_id,
        "binding_fingerprint": scope.binding_fingerprint,
    }
    claimed = restarted.claim(scope, op["operation_id"], "new-worker")
    assert claimed["state"] == "RESULT_UNKNOWN"
    with pytest.raises(OperationConflict):
        restarted.transition(
            scope,
            op["operation_id"],
            worker_id="new-worker",
            expected_revision=claimed["revision"],
            state="SEND_STARTED",
        )
    with pytest.raises(OperationConflict):
        restarted.transition(
            scope,
            op["operation_id"],
            worker_id="old-worker",
            expected_revision=sent["revision"],
            state="ACKNOWLEDGED",
        )
    reconciling = restarted.transition(
        scope,
        op["operation_id"],
        worker_id="new-worker",
        expected_revision=claimed["revision"],
        state="RECONCILING",
    )
    restarted.transition(
        scope,
        op["operation_id"],
        worker_id="new-worker",
        expected_revision=reconciling["revision"],
        state="RECONCILED",
        result={"remote_id": "doc"},
    )
    assert any(
        event["state"] == "RESULT_UNKNOWN" for event in restarted.events(scope, op["operation_id"])
    )


def test_lease_expiry_blocks_old_worker_without_new_claim(
    store: OperationStore, scope: OperationScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeptutor.knowledge.ragflow import operations

    monkeypatch.setattr(operations.time, "time", lambda: 100.0)
    op = create(store, scope)
    claimed = store.claim(scope, op["operation_id"], "worker", lease_seconds=1)
    monkeypatch.setattr(operations.time, "time", lambda: 102.0)
    with pytest.raises(OperationConflict):
        store.transition(
            scope,
            op["operation_id"],
            worker_id="worker",
            expected_revision=claimed["revision"],
            state="SEND_STARTED",
        )


def test_concurrent_workers_have_one_claim_winner(
    store: OperationStore, scope: OperationScope
) -> None:
    op = create(store, scope)
    with ThreadPoolExecutor(max_workers=4) as workers:
        claims = list(
            workers.map(lambda i: store.claim(scope, op["operation_id"], f"worker-{i}"), range(4))
        )
    assert sum(claim is not None for claim in claims) == 1


def test_document_refs_chunk_refs_and_visibility_persist(
    tmp_path: Path, scope: OperationScope
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = OperationStore(path)
    draft = store.register_document(scope, "remote-doc", title="lecture.pdf")
    assert draft["document_ref"] != "remote-doc"
    assert draft["visibility"] == "draft"
    before_revision, active_ids = store.visibility_snapshot(scope)
    assert active_ids == []
    assert store.register_document(scope, "remote-doc")["document_ref"] == draft["document_ref"]
    active = store.set_visibility(
        scope, draft["document_ref"], "active", expected_revision=draft["revision"]
    )
    with pytest.raises(OperationConflict):
        store.set_visibility(
            scope, draft["document_ref"], "disabled", expected_revision=draft["revision"]
        )
    after_revision, active_ids = store.visibility_snapshot(scope)
    assert after_revision > before_revision
    assert active_ids == ["remote-doc"]
    chunk_ref = store.register_chunk(scope, draft["document_ref"], "remote-chunk")
    assert chunk_ref != "remote-chunk"
    assert store.register_chunk(scope, draft["document_ref"], "remote-chunk") == chunk_ref
    restarted = OperationStore(path)
    assert restarted.resolve_document(scope, draft["document_ref"]) == "remote-doc"
    assert restarted.resolve_chunk(scope, draft["document_ref"], chunk_ref) == "remote-chunk"
    second_doc = restarted.register_document(scope, "other-doc")
    with pytest.raises(ValueError):
        restarted.resolve_chunk(scope, second_doc["document_ref"], chunk_ref)
    with pytest.raises(ValueError):
        restarted.resolve_chunk(scope, draft["document_ref"], "remote-chunk")
    for visibility in ("disabled", "archived", "deleted"):
        active = restarted.set_visibility(
            scope, draft["document_ref"], visibility, expected_revision=active["revision"]
        )
        assert restarted.visibility_snapshot(scope)[1] == []
    assert restarted.list_documents(scope)[0]["title"] == "lecture.pdf"


def test_operation_events_are_ordered_and_resumable(
    store: OperationStore, scope: OperationScope
) -> None:
    op = create(store, scope)
    claimed = store.claim(scope, op["operation_id"], "worker")
    store.transition(
        scope,
        op["operation_id"],
        worker_id="worker",
        expected_revision=claimed["revision"],
        state="FAILED",
        error_type="validation_error",
    )
    events = store.events(scope, op["operation_id"])
    assert events[0]["state"] == "INTENT_RECORDED"
    assert events[-1]["state"] == "FAILED"
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    assert store.events(scope, op["operation_id"], after_id=events[0]["id"]) == events[1:]


@pytest.mark.parametrize(
    "credential", ["api_key", "Authorization", "access-token", "password", "clientSecret"]
)
def test_credentials_cannot_enter_operation_json(
    store: OperationStore, scope: OperationScope, credential: str
) -> None:
    with pytest.raises(ValueError, match="credential"):
        store.create_operation(
            scope,
            action="upload",
            idempotency_key="secret",
            payload={"nested": [{credential: "private"}]},
        )
    assert store.list_operations(scope) == []


def test_result_credentials_rejected_before_write(
    store: OperationStore, scope: OperationScope
) -> None:
    op = create(store, scope)
    claimed = store.claim(scope, op["operation_id"], "worker")
    with pytest.raises(ValueError, match="credential"):
        store.transition(
            scope,
            op["operation_id"],
            worker_id="worker",
            expected_revision=claimed["revision"],
            state="FAILED",
            result={"api_key": "private"},
        )
    assert store.get_operation(scope, op["operation_id"])["revision"] == claimed["revision"]


def test_default_path_uses_current_system_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: OperationScope
) -> None:
    from deeptutor.multi_user import paths

    monkeypatch.setattr(paths, "SYSTEM_ROOT", tmp_path / "new-system")
    store = OperationStore()
    create(store, scope)
    database = tmp_path / "new-system" / "ragflow" / "operations.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_invalid_visibility_and_scope_fail_closed(
    store: OperationStore, scope: OperationScope
) -> None:
    with pytest.raises(ValueError):
        OperationScope("", "workspace", "generation", "binding")
    with pytest.raises(ValueError):
        store.register_document(scope, "doc", visibility="public")
    doc = store.register_document(scope, "doc")
    with pytest.raises(ValueError):
        store.set_visibility(scope, doc["document_ref"], "unknown")


def test_resource_lock_blocks_overlapping_document_writes(
    store: OperationStore, scope: OperationScope
) -> None:
    payload = {"resource_key": "document:remote-doc"}
    first = store.create_operation(scope, action="parse", idempotency_key="parse", payload=payload)
    with pytest.raises(OperationConflict):
        store.create_operation(scope, action="edit", idempotency_key="edit", payload=payload)
    claimed = store.claim(scope, first["operation_id"], "worker")
    store.transition(
        scope,
        first["operation_id"],
        worker_id="worker",
        expected_revision=claimed["revision"],
        state="FAILED",
    )
    assert (
        store.create_operation(scope, action="edit", idempotency_key="edit", payload=payload)[
            "operation_id"
        ]
        != first["operation_id"]
    )


def test_chunk_invalidation_replaces_refs_without_resurrecting_old_tokens(
    store: OperationStore, scope: OperationScope
) -> None:
    document = store.register_document(scope, "doc")
    ref = document["document_ref"]
    old = store.register_chunk(scope, ref, "chunk")
    store.invalidate_chunks(scope, ref)
    with pytest.raises(ValueError):
        store.resolve_chunk(scope, ref, old)
    new = store.register_chunk(scope, ref, "chunk")
    assert new != old
    assert store.resolve_chunk(scope, ref, new) == "chunk"


def test_history_lookup_only_relaxes_binding_filter(
    store: OperationStore, scope: OperationScope
) -> None:
    op = create(store, scope)
    rebound = replace(scope, binding_fingerprint="final-binding")
    assert store.get_operation(rebound, op["operation_id"]) is None
    assert (
        store.get_operation_for_generation(rebound, op["operation_id"])["operation_id"]
        == op["operation_id"]
    )
    assert len(store.list_operations_for_generation(rebound)) == 1
    for field in ("owner_id", "workspace_scope", "generation_id"):
        alien = replace(rebound, **{field: "other"})
        assert store.get_operation_for_generation(alien, op["operation_id"]) is None
        assert store.list_operations_for_generation(alien) == []


def test_reconciliation_receipt_updates_renew_a_live_lease(
    store: OperationStore, scope: OperationScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeptutor.knowledge.ragflow import operations

    monkeypatch.setattr(operations.time, "time", lambda: 100.0)
    op = create(store, scope)
    claimed = store.claim(scope, op["operation_id"], "worker", lease_seconds=10)
    sent = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker",
        expected_revision=claimed["revision"],
        state="SEND_STARTED",
    )
    ack = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker",
        expected_revision=sent["revision"],
        state="ACKNOWLEDGED",
    )
    monkeypatch.setattr(operations.time, "time", lambda: 105.0)
    renewed = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker",
        expected_revision=ack["revision"],
        state="ACKNOWLEDGED",
        result={"phase": "metadata"},
    )
    assert renewed["lease_expires_at"] == 115.0


def test_same_worker_can_reclaim_reconciliation_but_not_an_active_send(
    store: OperationStore, scope: OperationScope
) -> None:
    op = create(store, scope)
    claimed = store.claim(scope, op["operation_id"], "worker")
    assert store.claim(scope, op["operation_id"], "worker") is None
    sent = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker",
        expected_revision=claimed["revision"],
        state="SEND_STARTED",
    )
    assert store.claim(scope, op["operation_id"], "worker") is None
    unknown = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker",
        expected_revision=sent["revision"],
        state="RESULT_UNKNOWN",
    )
    reclaimed = store.claim(scope, op["operation_id"], "worker")
    assert reclaimed is not None
    assert reclaimed["revision"] > unknown["revision"]
    assert reclaimed["state"] == "RESULT_UNKNOWN"
    assert store.claim(scope, op["operation_id"], "different-worker") is None
    reconciling = store.transition(
        scope,
        op["operation_id"],
        worker_id="worker",
        expected_revision=reclaimed["revision"],
        state="RECONCILING",
    )
    assert store.claim(scope, op["operation_id"], "worker")["state"] == "RECONCILING"


def _reconciled_create(store: OperationStore, scope: OperationScope) -> dict:
    operation = store.create_operation(
        scope, action="create_dataset", idempotency_key="create", payload={"name": "course"}
    )
    current = store.claim(scope, operation["operation_id"], "worker")
    for state in ("SEND_STARTED", "ACKNOWLEDGED", "RECONCILED"):
        current = store.transition(
            scope,
            operation["operation_id"],
            worker_id="worker",
            expected_revision=current["revision"],
            state=state,
            result={"dataset_id": "remote-dataset"},
        )
    return current


def test_reconciled_dataset_create_survives_restart_until_binding_is_activated(
    tmp_path: Path,
    scope: OperationScope,
) -> None:
    path = tmp_path / "operations.sqlite3"
    store = OperationStore(path)
    operation = _reconciled_create(store, scope)
    assert store.pending_operations() == []
    restarted = OperationStore(path)
    pending = restarted.pending_binding_activations()
    assert [row["operation_id"] for row in pending] == [operation["operation_id"]]
    assert pending[0]["scope"] == operation["scope"]
    done = restarted.mark_binding_activated(scope, operation["operation_id"])
    assert done["state"] == "RECONCILED"
    assert done["result"] == {"dataset_id": "remote-dataset", "binding_activated": True}
    assert done["revision"] == operation["revision"] + 1
    assert (
        restarted.events(scope, operation["operation_id"])[-1]["event_type"] == "binding_activated"
    )
    again = OperationStore(path)
    assert again.pending_binding_activations() == []
    event_count = len(again.events(scope, operation["operation_id"]))
    assert again.mark_binding_activated(scope, operation["operation_id"]) == done
    assert len(again.events(scope, operation["operation_id"])) == event_count


@pytest.mark.parametrize(
    "field", ["owner_id", "workspace_scope", "generation_id", "binding_fingerprint"]
)
def test_mark_binding_activated_enforces_original_operation_scope(
    store: OperationStore,
    scope: OperationScope,
    field: str,
) -> None:
    operation = _reconciled_create(store, scope)
    foreign = replace(scope, **{field: "other"})
    with pytest.raises(OperationConflict):
        store.mark_binding_activated(foreign, operation["operation_id"])
    assert store.get_operation(scope, operation["operation_id"]) == operation


def test_activation_inventory_excludes_incomplete_and_other_action_operations(
    store: OperationStore,
    scope: OperationScope,
) -> None:
    uploading = create(store, scope)
    incomplete = store.create_operation(
        scope, action="create_dataset", idempotency_key="incomplete", payload={"name": "pending"}
    )
    claimed = store.claim(scope, uploading["operation_id"], "worker")
    for state in ("SEND_STARTED", "ACKNOWLEDGED", "RECONCILED"):
        claimed = store.transition(
            scope,
            uploading["operation_id"],
            worker_id="worker",
            expected_revision=claimed["revision"],
            state=state,
            result={"dataset_id": "not-a-create"},
        )
    assert store.pending_binding_activations() == []
    for operation in (uploading, incomplete):
        with pytest.raises(OperationConflict):
            store.mark_binding_activated(scope, operation["operation_id"])


@pytest.mark.parametrize(
    "other_payload", [{}, {"resource_key": "document:doc"}, {"resource_key": "cancel:doc"}]
)
@pytest.mark.parametrize("delete_first", [True, False])
def test_dataset_deletion_excludes_every_other_unfinished_write_in_both_directions(
    store: OperationStore,
    scope: OperationScope,
    other_payload: dict,
    delete_first: bool,
) -> None:
    actions = [
        ("delete_dataset", {"resource_key": "dataset"}),
        ("document_write", other_payload),
    ]
    if not delete_first:
        actions.reverse()
    store.create_operation(
        scope, action=actions[0][0], idempotency_key="first", payload=actions[0][1]
    )
    with pytest.raises(OperationConflict):
        store.create_operation(
            scope, action=actions[1][0], idempotency_key="second", payload=actions[1][1]
        )
    other_scope = replace(scope, owner_id="other-owner")
    assert store.create_operation(
        other_scope, action=actions[1][0], idempotency_key="second", payload=actions[1][1]
    )


def test_finished_write_releases_dataset_delete_lock(
    store: OperationStore, scope: OperationScope
) -> None:
    upload = create(store, scope)
    claimed = store.claim(scope, upload["operation_id"], "worker")
    store.transition(
        scope,
        upload["operation_id"],
        worker_id="worker",
        expected_revision=claimed["revision"],
        state="FAILED",
    )
    deletion = store.create_operation(
        scope,
        action="delete_dataset",
        idempotency_key="delete",
        payload={"resource_key": "dataset"},
    )
    claimed = store.claim(scope, deletion["operation_id"], "worker")
    store.transition(
        scope,
        deletion["operation_id"],
        worker_id="worker",
        expected_revision=claimed["revision"],
        state="FAILED",
    )
    assert create(store, scope, key="another-upload")


def test_recording_document_write_atomically_unpublishes_active_content_once(
    store: OperationStore,
    scope: OperationScope,
) -> None:
    document = store.register_document(scope, "doc", visibility="active")
    before_revision = store.visibility_snapshot(scope)[0]
    payload = {"resource_key": "document:doc"}
    operation = store.create_operation(
        scope,
        action="parse",
        idempotency_key="parse",
        payload=payload,
        document_ref_to_unpublish=document["document_ref"],
    )
    assert store.visibility_snapshot(scope)[1] == []
    assert store.visibility_snapshot(scope)[0] > before_revision
    unpublished = store.list_documents(scope)[0]
    assert unpublished["visibility"] == "draft"
    assert unpublished["revision"] > document["revision"]
    claimed = store.claim(scope, operation["operation_id"], "worker")
    store.transition(
        scope,
        operation["operation_id"],
        worker_id="worker",
        expected_revision=claimed["revision"],
        state="FAILED",
    )
    store.set_visibility(scope, document["document_ref"], "active")
    republished_snapshot = store.visibility_snapshot(scope)
    repeated = store.create_operation(
        scope,
        action="parse",
        idempotency_key="parse",
        payload=payload,
        document_ref_to_unpublish=document["document_ref"],
    )
    assert repeated["operation_id"] == operation["operation_id"]
    assert store.visibility_snapshot(scope) == republished_snapshot


def test_unpublish_target_must_belong_to_scope_and_failure_rolls_back_intent(
    store: OperationStore,
    scope: OperationScope,
) -> None:
    other = replace(scope, owner_id="other-owner")
    document = store.register_document(other, "doc", visibility="active")
    with pytest.raises(ValueError):
        store.create_operation(
            scope,
            action="parse",
            idempotency_key="parse",
            payload={"resource_key": "document:doc"},
            document_ref_to_unpublish=document["document_ref"],
        )
    assert store.list_operations(scope) == []
    assert store.visibility_snapshot(other)[1] == ["doc"]


@pytest.mark.parametrize("resource_key", ["document:doc", "cancel:doc", "dataset"])
def test_publication_rejects_pending_document_cancel_or_dataset_operation(
    store: OperationStore,
    scope: OperationScope,
    resource_key: str,
) -> None:
    document = store.register_document(scope, "doc")
    store.create_operation(
        scope, action="mutation", idempotency_key="write", payload={"resource_key": resource_key}
    )
    with pytest.raises(OperationConflict):
        store.set_visibility(scope, document["document_ref"], "active")
    for visibility in ("disabled", "archived", "draft"):
        assert (
            store.set_visibility(scope, document["document_ref"], visibility)["visibility"]
            == visibility
        )


def test_publication_is_not_blocked_by_other_document_or_other_scope_operations(
    store: OperationStore,
    scope: OperationScope,
) -> None:
    document = store.register_document(scope, "doc")
    store.create_operation(
        scope,
        action="parse",
        idempotency_key="other-doc",
        payload={"resource_key": "document:other-doc"},
    )
    other = replace(scope, owner_id="other-owner")
    store.create_operation(
        other,
        action="delete_dataset",
        idempotency_key="delete",
        payload={"resource_key": "dataset"},
    )
    assert store.set_visibility(scope, document["document_ref"], "active")["visibility"] == "active"


def test_concurrent_publication_and_document_write_cannot_leave_content_active(
    store: OperationStore,
    scope: OperationScope,
) -> None:
    document = store.register_document(scope, "doc")

    def publish():
        try:
            return store.set_visibility(scope, document["document_ref"], "active")
        except OperationConflict:
            return None

    def record():
        return store.create_operation(
            scope,
            action="parse",
            idempotency_key="parse",
            payload={"resource_key": "document:doc"},
            document_ref_to_unpublish=document["document_ref"],
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        publication = workers.submit(publish)
        mutation = workers.submit(record)
        publication.result()
        assert mutation.result()["state"] == "INTENT_RECORDED"
    assert store.visibility_snapshot(scope)[1] == []


def test_dataset_delete_intent_unpublishes_all_active_documents_only_in_its_scope(
    store: OperationStore,
    scope: OperationScope,
) -> None:
    for remote_id, visibility in (("one", "active"), ("two", "active"), ("disabled", "disabled")):
        store.register_document(scope, remote_id, visibility=visibility)
    other = replace(scope, owner_id="other-owner")
    store.register_document(other, "one", visibility="active")
    before = store.visibility_snapshot(scope)[0]
    store.create_operation(
        scope,
        action="delete_dataset",
        idempotency_key="delete",
        payload={"resource_key": "dataset"},
    )
    assert store.visibility_snapshot(scope)[0] > before
    assert store.visibility_snapshot(scope)[1] == []
    assert {row["remote_id"]: row["visibility"] for row in store.list_documents(scope)} == {
        "one": "draft",
        "two": "draft",
        "disabled": "disabled",
    }
    assert store.visibility_snapshot(other)[1] == ["one"]


def _binding_snapshot(scope: OperationScope, actor_id: str = "old-actor") -> dict:
    return {
        "actor_id": actor_id,
        "actor_role": "user",
        "kb_ref": "user:kb:" + scope.generation_id,
        "generation_id": scope.generation_id,
        "binding_fingerprint": "old-full-fingerprint",
        "require_write": True,
        "resource_owner_id": scope.owner_id,
        "workspace_scope": scope.workspace_scope,
    }


def _unknown_operation(
    store: OperationStore, scope: OperationScope, *, attention: bool = True
) -> dict:
    operation = store.create_operation(
        scope,
        action="update_document",
        idempotency_key="update",
        actor_id="old-actor",
        payload={
            "request": {"document_id": "doc", "patch": {"name": "new-name"}},
            "resource_key": "document:doc",
            "binding_snapshot": _binding_snapshot(scope),
        },
    )
    current = store.claim(scope, operation["operation_id"], "worker", lease_seconds=10)
    for state in ("SEND_STARTED", "RESULT_UNKNOWN"):
        current = store.transition(
            scope,
            operation["operation_id"],
            worker_id="worker",
            expected_revision=current["revision"],
            state=state,
            result={"remote_id": "doc"},
        )
    if attention:
        current = store.transition(
            scope,
            operation["operation_id"],
            worker_id="worker",
            expected_revision=current["revision"],
            state="NEEDS_ATTENTION",
            error_type="recovery_authorization_unavailable",
        )
    return current


def test_reauthorization_preserves_request_receipt_and_never_allows_resending(
    store: OperationStore,
    scope: OperationScope,
) -> None:
    old = _unknown_operation(store, scope)
    snapshot = {
        **_binding_snapshot(scope, "new-actor"),
        "binding_fingerprint": "new-full-fingerprint",
    }
    updated = store.reauthorize_reconciliation(
        scope,
        old["operation_id"],
        expected_revision=old["revision"],
        binding_snapshot=snapshot,
        actor_id="new-actor",
    )
    assert updated["state"] == "RESULT_UNKNOWN"
    assert updated["actor_id"] == "new-actor"
    assert updated["payload"]["binding_snapshot"] == snapshot
    assert updated["payload"]["request"] == old["payload"]["request"]
    assert updated["result"] == old["result"]
    assert updated["resource_key"] == old["resource_key"]
    assert updated["idempotency_key"] == old["idempotency_key"]
    assert updated["payload_digest"] != old["payload_digest"]
    assert updated["revision"] == old["revision"] + 1
    assert updated["error_type"] is None
    assert updated["lease_owner"] is None
    assert updated["lease_expires_at"] is None
    assert OperationStore(store.path).get_operation(scope, old["operation_id"]) == updated
    events = store.events(scope, old["operation_id"])
    assert events[0]["actor_id"] == "old-actor"
    assert events[-1]["actor_id"] == "new-actor"
    assert events[-1]["event_type"] == "reconciliation_reauthorized"
    claimed = store.claim(scope, old["operation_id"], "new-worker")
    with pytest.raises(OperationConflict):
        store.transition(
            scope,
            old["operation_id"],
            worker_id="new-worker",
            expected_revision=claimed["revision"],
            state="SEND_STARTED",
        )


@pytest.mark.parametrize(
    "field", ["owner_id", "workspace_scope", "generation_id", "binding_fingerprint"]
)
def test_reauthorization_cannot_cross_original_scope(
    store: OperationStore, scope: OperationScope, field: str
) -> None:
    operation = _unknown_operation(store, scope)
    other = replace(scope, **{field: "other"})
    with pytest.raises(OperationConflict):
        store.reauthorize_reconciliation(
            other,
            operation["operation_id"],
            expected_revision=operation["revision"],
            binding_snapshot=_binding_snapshot(other),
            actor_id="old-actor",
        )


@pytest.mark.parametrize(
    "field", ["resource_owner_id", "workspace_scope", "generation_id", "kb_ref", "actor_id"]
)
def test_reauthorization_rejects_snapshot_identity_change(
    store: OperationStore, scope: OperationScope, field: str
) -> None:
    operation = _unknown_operation(store, scope)
    snapshot = {**_binding_snapshot(scope), field: "different"}
    with pytest.raises(OperationConflict):
        store.reauthorize_reconciliation(
            scope,
            operation["operation_id"],
            expected_revision=operation["revision"],
            binding_snapshot=snapshot,
            actor_id="old-actor",
        )
    assert store.get_operation(scope, operation["operation_id"]) == operation


def test_reauthorization_respects_existing_lease_and_revision(
    store: OperationStore, scope: OperationScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeptutor.knowledge.ragflow import operations

    monkeypatch.setattr(operations.time, "time", lambda: 100.0)
    operation = _unknown_operation(store, scope, attention=False)
    with pytest.raises(OperationConflict):
        store.reauthorize_reconciliation(
            scope,
            operation["operation_id"],
            expected_revision=operation["revision"],
            binding_snapshot=_binding_snapshot(scope),
            actor_id="old-actor",
        )
    monkeypatch.setattr(operations.time, "time", lambda: 111.0)
    updated = store.reauthorize_reconciliation(
        scope,
        operation["operation_id"],
        expected_revision=operation["revision"],
        binding_snapshot=_binding_snapshot(scope),
        actor_id="old-actor",
    )
    assert updated["revision"] > operation["revision"]
    with pytest.raises(OperationConflict):
        store.reauthorize_reconciliation(
            scope,
            operation["operation_id"],
            expected_revision=operation["revision"],
            binding_snapshot=_binding_snapshot(scope),
            actor_id="old-actor",
        )


def test_reauthorization_rejects_terminal_and_unsent_operations(
    store: OperationStore, scope: OperationScope
) -> None:
    unsent = create(store, scope)
    complete = _reconciled_create(store, scope)
    for operation in (unsent, complete):
        with pytest.raises(OperationConflict):
            store.reauthorize_reconciliation(
                scope,
                operation["operation_id"],
                expected_revision=operation["revision"],
                binding_snapshot=_binding_snapshot(scope),
                actor_id="old-actor",
            )


def test_operation_event_actor_column_migrates_existing_database(
    tmp_path: Path, scope: OperationScope
) -> None:
    path = tmp_path / "operations.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE operation_events (id INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,event_type TEXT NOT NULL,error_type TEXT,created_at REAL NOT NULL)"
        )
    store = OperationStore(path)
    operation = store.create_operation(
        scope, action="parse", idempotency_key="parse", payload={}, actor_id="recorded-actor"
    )
    assert store.events(scope, operation["operation_id"])[0]["actor_id"] == "recorded-actor"
