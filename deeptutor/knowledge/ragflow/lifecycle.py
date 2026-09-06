"""Durable, access-checked RAGFlow mutations and read-only reconciliation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any
from uuid import uuid4
from weakref import WeakValueDictionary

from fastapi import HTTPException

from deeptutor.services.rag.pipelines.ragflow.client import RagflowClient, RagflowError
from deeptutor.utils.document_validator import DocumentValidator

WORKER_ID = "ragflow-" + uuid4().hex
LEASE_SECONDS = 360
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
TERMINAL = {"RECONCILED", "FAILED", "NEEDS_ATTENTION"}
ACTIONS = {
    "create_dataset",
    "create_document",
    "upload",
    "update_document",
    "delete_document",
    "parse",
    "cancel",
    "add_chunk",
    "update_chunk",
    "delete_chunk",
    "enable_chunk",
    "delete_dataset",
}
RESERVED_METADATA = {
    "yfeistai_generation",
    "yfeistai_operation",
    "yfeistai_source_sha256",
    "yfeistai_source_kind",
}


def safe_name(value: object) -> str:
    name = str(value or "document").replace("\\", "/").split("/")[-1]
    return re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", name).strip(" .")[:180] or "document"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def document_status(document: dict) -> str:
    value = str(document.get("run", "")).upper()
    return {
        "0": "unstart",
        "UNSTART": "unstart",
        "1": "parsing",
        "RUNNING": "parsing",
        "RUN": "parsing",
        "2": "cancelled",
        "CANCEL": "cancelled",
        "CANCELED": "cancelled",
        "CANCELLED": "cancelled",
        "3": "ready",
        "DONE": "ready",
        "4": "failed",
        "FAIL": "failed",
        "FAILED": "failed",
    }.get(value, "unknown")


def assert_remote_write(bound: Any) -> None:
    bound.recheck()
    if bound.resource.read_only or bound.binding.get("access_mode") != "manage_documents":
        raise HTTPException(403, "ragflow_read_only")


def record_operation(bound: Any, action: str, request: dict, *, idempotency_key: str) -> dict:
    assert_remote_write(bound)
    if action not in ACTIONS:
        raise HTTPException(400, "unsupported_ragflow_action")
    if action == "delete_dataset" and bound.binding.get("ownership") != "managed":
        raise HTTPException(403, "attached_dataset_delete_forbidden")
    if not idempotency_key or len(idempotency_key) > 200:
        raise HTTPException(400, "invalid_idempotency_key")
    snapshot = bound.recovery_snapshot()
    payload = {"request": request, "binding_snapshot": snapshot}
    document_id = request.get("document_id")
    if document_id:
        payload["resource_key"] = ("cancel:" if action == "cancel" else "document:") + document_id
    elif action == "delete_dataset":
        payload["resource_key"] = "dataset"
    document_ref = (
        next(
            (
                row["document_ref"]
                for row in bound.store.list_documents(bound.scope)
                if row["remote_id"] == document_id
            ),
            None,
        )
        if document_id
        else None
    )
    return bound.store.create_operation(
        bound.scope,
        action=action,
        idempotency_key=idempotency_key,
        payload=payload,
        actor_id=snapshot.get("actor_id", ""),
        document_ref_to_unpublish=document_ref,
    )


def _assert_snapshot(bound: Any, op: dict) -> None:
    if op["payload"].get("binding_snapshot") != bound.recovery_snapshot():
        raise HTTPException(409, "operation_binding_changed")


@asynccontextmanager
async def client_context(bound: Any, client_factory=RagflowClient):
    client = client_factory(bound.config)
    try:
        yield client
    finally:
        close = getattr(client, "aclose", None)
        if close:
            await close()


async def checked_call(bound: Any, call, *args, **kwargs):
    bound.recheck()
    result = await call(*args, **kwargs)
    bound.recheck()
    return result


async def remote_document(
    bound: Any, client: Any, document_id: str, *, allow_absent=False
) -> dict | None:
    data = await checked_call(
        bound,
        client.list_documents,
        bound.binding["dataset_id"],
        id=document_id,
        page=1,
        page_size=2,
    )
    matches = [doc for doc in data["docs"] if doc.get("id") == document_id]
    if not matches and allow_absent:
        return None
    if len(matches) != 1:
        raise HTTPException(404, "ragflow_document_not_found")
    document = matches[0]
    if document.get("dataset_id", bound.binding["dataset_id"]) != bound.binding["dataset_id"]:
        raise HTTPException(502, "ragflow_document_scope_mismatch")
    return document


async def remote_chunk(
    bound: Any, client: Any, document_id: str, chunk_id: str, *, allow_absent=False
) -> dict | None:
    await remote_document(bound, client, document_id)
    data = await checked_call(
        bound,
        client.list_chunks,
        bound.binding["dataset_id"],
        document_id,
        id=chunk_id,
        page=1,
        page_size=2,
    )
    matches = [chunk for chunk in data["chunks"] if chunk.get("id") == chunk_id]
    if not matches and allow_absent:
        return None
    if len(matches) != 1:
        raise HTTPException(404, "ragflow_chunk_not_found")
    chunk = matches[0]
    if (
        chunk.get("document_id", document_id) != document_id
        or chunk.get("dataset_id", bound.binding["dataset_id"]) != bound.binding["dataset_id"]
    ):
        raise HTTPException(502, "ragflow_chunk_scope_mismatch")
    return chunk


async def ensure_available_content(bound: Any, client: Any, document_id: str) -> None:
    """Publication checks actual scoped, enabled content rather than stale counters."""
    for page in range(1, 11):
        data = await checked_call(
            bound,
            client.list_chunks,
            bound.binding["dataset_id"],
            document_id,
            page=page,
            page_size=100,
        )
        for chunk in data["chunks"]:
            if (
                chunk.get("document_id", document_id) != document_id
                or chunk.get("dataset_id", bound.binding["dataset_id"])
                != bound.binding["dataset_id"]
            ):
                raise HTTPException(502, "ragflow_chunk_scope_mismatch")
            if (
                chunk.get("available") in (True, 1, "1")
                and isinstance(chunk.get("content"), str)
                and chunk["content"].strip()
            ):
                return
        if page * 100 >= data["total"] or len(data["chunks"]) < 100:
            raise HTTPException(409, "no_available_chunks")
    raise HTTPException(409, "publication_sample_limit")


def _advance(bound: Any, op: dict, state: str, *, result=None, error_type=None) -> dict:
    return bound.store.transition(
        bound.scope,
        op["operation_id"],
        worker_id=WORKER_ID,
        expected_revision=op["revision"],
        state=state,
        result=result if result is not None else op.get("result"),
        error_type=error_type,
    )


def _temporary_name(op: dict) -> str:
    return (
        "yfs-"
        + op["operation_id"]
        + "-"
        + safe_name(op["payload"]["request"].get("name", "document"))
    )


def _finalization_patch(bound, op, request):
    metadata = {
        "yfeistai_generation": bound.scope.generation_id,
        "yfeistai_operation": op["operation_id"],
        "yfeistai_source_kind": "local" if op["action"] == "upload" else "empty",
    }
    if request.get("source_sha256"):
        metadata["yfeistai_source_sha256"] = request["source_sha256"]
    return {"name": safe_name(request["name"]), "meta_fields": metadata}


def _receipt_id(data: Any, *, kind="document") -> str:
    if isinstance(data, list):
        if len(data) != 1:
            raise HTTPException(502, "ragflow_ambiguous_receipt")
        data = data[0]
    if isinstance(data, dict) and kind == "chunk" and isinstance(data.get("chunk"), dict):
        data = data["chunk"]
    remote_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(remote_id, str) or not remote_id:
        raise HTTPException(502, "ragflow_missing_receipt")
    return remote_id


async def _preflight(bound: Any, client: Any, op: dict) -> dict:
    request = dict(op["payload"]["request"])
    action, document_id = op["action"], request.get("document_id")
    assert_remote_write(bound)
    _assert_snapshot(bound, op)
    if document_id:
        document = await remote_document(bound, client, document_id)
        if action == "update_document" and "meta_fields" in request.get("patch", {}):
            patch = dict(request["patch"])
            metadata = dict(document.get("meta_fields") or {})
            if RESERVED_METADATA.intersection(patch["meta_fields"]):
                raise HTTPException(400, "reserved_metadata")
            metadata.update(patch["meta_fields"])
            patch["meta_fields"] = metadata
            request["patch"] = patch
        if action == "parse":
            edits = [
                item
                for item in bound.store.list_operations(bound.scope, limit=1000)
                if item["action"] in {"add_chunk", "update_chunk", "delete_chunk"}
                and item["payload"]["request"].get("document_id") == document_id
                and item["state"] != "FAILED"
            ]
            if edits and not request.get("confirm_rebuild"):
                raise HTTPException(409, "chunk_edits_require_rebuild_confirmation")
            for row in bound.store.list_documents(bound.scope):
                if row["remote_id"] == document_id:
                    bound.store.invalidate_chunks(bound.scope, row["document_ref"])
        if request.get("chunk_id"):
            await remote_chunk(bound, client, document_id, request["chunk_id"])
    if action == "upload":
        path = Path(request["spool_path"])
        expected_root = Path(request["spool_root"]).resolve()
        if not path.resolve().is_relative_to(expected_root) or not path.is_file():
            raise HTTPException(409, "ragflow_spool_unavailable")
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        if hasher.hexdigest() != request["source_sha256"]:
            raise HTTPException(409, "ragflow_spool_changed")
    if action == "delete_dataset" and bound.binding.get("ownership") != "managed":
        raise HTTPException(403, "attached_dataset_delete_forbidden")
    return request


async def _send(bound: Any, client: Any, op: dict, request: dict):
    dataset_id, action = bound.binding["dataset_id"], op["action"]
    document_id, chunk_id = request.get("document_id"), request.get("chunk_id")
    if action == "create_dataset":
        return await checked_call(
            bound,
            client.create_dataset,
            {
                **request.get("config", {}),
                "name": _temporary_name(op),
                "description": "yFeiSTAI operation " + op["operation_id"],
            },
        )
    if action == "create_document":
        return await checked_call(
            bound, client.create_document, dataset_id, name=_temporary_name(op)
        )
    if action == "upload":
        return await checked_call(
            bound,
            client.upload_document,
            dataset_id,
            Path(request["spool_path"]),
            filename=_temporary_name(op),
        )
    if action == "update_document":
        return await checked_call(
            bound, client.update_document, dataset_id, document_id, request["patch"]
        )
    if action == "delete_document":
        return await checked_call(bound, client.delete_documents, dataset_id, [document_id])
    if action in {"parse", "cancel"}:
        if request.get("use_ingestion_pipeline"):
            return await checked_call(
                bound,
                client.ingest_documents,
                [document_id],
                run="1" if action == "parse" else "2",
                delete=bool(request.get("rebuild", False)),
            )
        method = client.parse_documents if action == "parse" else client.cancel_parse
        return await checked_call(bound, method, dataset_id, [document_id])
    if action == "add_chunk":
        return await checked_call(
            bound, client.add_chunk, dataset_id, document_id, request["patch"]
        )
    if action == "update_chunk":
        return await checked_call(
            bound, client.update_chunk, dataset_id, document_id, chunk_id, request["patch"]
        )
    if action == "delete_chunk":
        return await checked_call(bound, client.delete_chunks, dataset_id, document_id, [chunk_id])
    if action == "enable_chunk":
        return await checked_call(
            bound,
            client.set_chunk_availability,
            dataset_id,
            document_id,
            [chunk_id],
            request["available"],
        )
    if action == "delete_dataset":
        return await checked_call(bound, client.delete_datasets, [dataset_id])
    raise HTTPException(400, "unsupported_ragflow_action")


async def execute_operation(
    bound: Any, operation_id: str, *, client_factory=RagflowClient
) -> dict | None:
    lock = _locks.setdefault(operation_id, asyncio.Lock())
    async with lock:
        op = bound.store.claim(bound.scope, operation_id, WORKER_ID, lease_seconds=LEASE_SECONDS)
        if op is None:
            return bound.store.get_operation(bound.scope, operation_id)
        if op["state"] in TERMINAL:
            return op
        async with client_context(bound, client_factory) as client:
            if op["state"] != "INTENT_RECORDED":
                return await _reconcile(bound, client, op)
            sent = False
            try:
                request = await _preflight(bound, client, op)
                bound.recheck()
                expected_patch = (
                    _finalization_patch(bound, op, request)
                    if op["action"] in {"create_document", "upload"}
                    else request.get("patch", {})
                )
                op = _advance(bound, op, "SEND_STARTED", result={"expected_patch": expected_patch})
                sent = True
                data = await _send(bound, client, op, request)
                receipt = dict(op.get("result") or {})
                if op["action"] in {"create_document", "upload", "create_dataset", "add_chunk"}:
                    receipt["remote_id"] = _receipt_id(
                        data, kind="chunk" if op["action"] == "add_chunk" else "document"
                    )
                op = _advance(bound, op, "ACKNOWLEDGED", result=receipt)
                if op["action"] in {"create_document", "upload"}:
                    document_id = receipt["remote_id"]
                    patch = receipt["expected_patch"]
                    await remote_document(bound, client, document_id)
                    # A lease/revision check immediately precedes each subsequent write.
                    op = _advance(bound, op, "ACKNOWLEDGED", result=receipt)
                    await checked_call(
                        bound,
                        client.update_document,
                        bound.binding["dataset_id"],
                        document_id,
                        patch,
                    )
                return await _reconcile(bound, client, op)
            except Exception as exc:
                unknown = sent and (
                    op["state"] != "SEND_STARTED"
                    or not (isinstance(exc, RagflowError) and not exc.outcome_unknown)
                )
                error_type = (
                    exc.error_type
                    if isinstance(exc, RagflowError)
                    else (str(exc.detail) if isinstance(exc, HTTPException) else "operation_error")
                )
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", error_type):
                    error_type = "authorization_or_operation_changed"
                return _advance(
                    bound, op, "RESULT_UNKNOWN" if unknown else "FAILED", error_type=error_type
                )


def _matches(document: dict, expected: dict) -> bool:
    return all(document.get(key) == value for key, value in expected.items())


async def _find_by_name(bound: Any, client: Any, op: dict) -> list[dict]:
    name = _temporary_name(op)
    candidates = []
    for page in range(1, 101):
        if op["action"] == "create_dataset":
            items = await checked_call(
                bound, client.list_datasets, name=name, page=page, page_size=100
            )
            total = None
        else:
            response = await checked_call(
                bound,
                client.list_documents,
                bound.binding["dataset_id"],
                name=name,
                page=page,
                page_size=100,
            )
            items, total = response["docs"], response["total"]
        candidates.extend(item for item in items if item.get("name") == name)
        if len(items) < 100 or (total is not None and page * 100 >= total):
            return candidates
    raise HTTPException(409, "reconciliation_candidate_limit")


async def _reconcile(bound: Any, client: Any, op: dict) -> dict:
    if op["state"] in TERMINAL:
        return op
    try:
        bound.recheck()
        _assert_snapshot(bound, op)
        op = _advance(bound, op, "RECONCILING")
        action, request = op["action"], op["payload"]["request"]
        receipt = dict(op.get("result") or {})
        document_id, chunk_id = request.get("document_id"), request.get("chunk_id")
        if action in {"upload", "create_document", "create_dataset"}:
            if not receipt.get("remote_id"):
                candidates = await _find_by_name(bound, client, op)
                if not candidates:
                    return op  # Empty search is not proof the POST was never applied.
                if len(candidates) != 1:
                    return _advance(
                        bound, op, "NEEDS_ATTENTION", error_type="ambiguous_remote_result"
                    )
                receipt["remote_id"] = _receipt_id(candidates[0])
                op = _advance(bound, op, "RECONCILING", result=receipt)
            if action == "create_dataset":
                datasets = await checked_call(
                    bound, client.list_datasets, id=receipt["remote_id"], page=1, page_size=2
                )
                if len([item for item in datasets if item.get("id") == receipt["remote_id"]]) != 1:
                    return op
                receipt["dataset_id"] = receipt["remote_id"]
            else:
                document = await remote_document(bound, client, receipt["remote_id"])
                if receipt.get("expected_patch") and not _matches(
                    document, receipt["expected_patch"]
                ):
                    return _advance(
                        bound,
                        op,
                        "NEEDS_ATTENTION",
                        result=receipt,
                        error_type="document_finalization_incomplete",
                    )
                row = bound.store.register_document(
                    bound.scope,
                    receipt["remote_id"],
                    title=safe_name(request["name"]),
                    visibility="draft",
                )
                receipt["document_ref"] = row["document_ref"]
            return _advance(bound, op, "RECONCILED", result=receipt)
        if action in {"parse", "cancel"}:
            document = await remote_document(bound, client, document_id)
            status = document_status(document)
            receipt.update(
                document_status=status,
                progress=max(0.0, min(1.0, float(document.get("progress", 0) or 0))),
            )
            if status in {"unstart", "parsing", "unknown"}:
                return _advance(bound, op, "RECONCILING", result=receipt)
            if status == "failed":
                return _advance(bound, op, "FAILED", result=receipt, error_type="parse_failed")
            if status == "ready" and int(document.get("chunk_count", 0) or 0) <= 0:
                return _advance(bound, op, "FAILED", result=receipt, error_type="empty_document")
            if status == "ready":
                try:
                    await ensure_available_content(bound, client, document_id)
                except HTTPException as exc:
                    if exc.status_code == 409:
                        return _advance(bound, op, "FAILED", result=receipt, error_type=exc.detail)
                    raise
            return _advance(bound, op, "RECONCILED", result=receipt)
        if action == "delete_document":
            if await remote_document(bound, client, document_id, allow_absent=True) is not None:
                return op
            for row in bound.store.list_documents(bound.scope):
                if row["remote_id"] == document_id:
                    bound.store.set_visibility(bound.scope, row["document_ref"], "deleted")
            return _advance(bound, op, "RECONCILED")
        if action == "update_document":
            document = await remote_document(bound, client, document_id)
            expected = receipt.get("expected_patch") or request["patch"]
            if not _matches(document, expected):
                return _advance(
                    bound, op, "NEEDS_ATTENTION", error_type="partial_or_conflicting_update"
                )
            return _advance(bound, op, "RECONCILED")
        if action == "add_chunk":
            chunk_id = receipt.get("remote_id")
            if not chunk_id:
                return _advance(
                    bound, op, "NEEDS_ATTENTION", error_type="chunk_creation_result_unknown"
                )
        if action in {"add_chunk", "update_chunk", "delete_chunk", "enable_chunk"}:
            chunk = await remote_chunk(
                bound, client, document_id, chunk_id, allow_absent=action == "delete_chunk"
            )
            if action == "delete_chunk":
                return _advance(bound, op, "RECONCILED") if chunk is None else op
            expected = (
                {"available": request["available"]}
                if action == "enable_chunk"
                else request["patch"]
            )
            if not _matches(chunk, expected):
                return _advance(
                    bound, op, "NEEDS_ATTENTION", error_type="partial_or_conflicting_update"
                )
            row = bound.store.register_document(bound.scope, document_id)
            receipt["chunk_ref"] = bound.store.register_chunk(
                bound.scope, row["document_ref"], chunk_id
            )
            return _advance(bound, op, "RECONCILED", result=receipt)
        if action == "delete_dataset":
            datasets = await checked_call(
                bound, client.list_datasets, id=bound.binding["dataset_id"], page=1, page_size=2
            )
            if any(item.get("id") == bound.binding["dataset_id"] for item in datasets):
                return op
            return _advance(bound, op, "RECONCILED")
        return _advance(bound, op, "NEEDS_ATTENTION", error_type="unsupported_ragflow_action")
    except Exception as exc:
        error_type = (
            exc.error_type if isinstance(exc, RagflowError) else "reconciliation_unavailable"
        )
        return _advance(bound, op, "RESULT_UNKNOWN", error_type=error_type)


async def reconcile_operation(
    bound: Any, operation_id: str, *, client_factory=RagflowClient
) -> dict | None:
    lock = _locks.setdefault(operation_id, asyncio.Lock())
    async with lock:
        current = bound.store.get_operation(bound.scope, operation_id)
        if current is None or current["state"] == "INTENT_RECORDED":
            return current
        op = bound.store.claim(bound.scope, operation_id, WORKER_ID, lease_seconds=LEASE_SECONDS)
        if op is None or op["state"] in TERMINAL:
            return op or bound.store.get_operation(bound.scope, operation_id)
        async with client_context(bound, client_factory) as client:
            return await _reconcile(bound, client, op)


def public_operation(op: dict) -> dict:
    """Only allowlisted receipts cross the HTTP/event boundary."""
    result = op.get("result") or {}
    return {
        "operation_id": op["operation_id"],
        "task_id": op["operation_id"],
        "action": op["action"],
        "state": op["state"],
        "revision": op["revision"],
        "accepted": True,
        "error_type": op.get("error_type"),
        "result": {
            key: result[key]
            for key in ("document_ref", "chunk_ref", "document_status", "progress")
            if key in result
        },
    }


class RagflowOperationPending(RuntimeError):
    def __init__(self, operation_id: str, state: str):
        self.operation_id = operation_id
        self.state = state
        super().__init__(
            f"RAGFlow operation {operation_id} is {state}; completion has not been verified."
        )


async def _wait_for_operation(bound, operation_id, *, client_factory, wait_timeout):
    deadline = time.monotonic() + max(0, wait_timeout)
    op = await execute_operation(bound, operation_id, client_factory=client_factory)
    while op["state"] not in TERMINAL and time.monotonic() < deadline:
        await asyncio.sleep(min(1.0, max(0, deadline - time.monotonic())))
        method = execute_operation if op["state"] == "INTENT_RECORDED" else reconcile_operation
        op = await method(bound, operation_id, client_factory=client_factory)
    if op["state"] != "RECONCILED":
        raise RagflowOperationPending(operation_id, op["state"])
    return op


async def ingest_files(
    bound, file_paths: list[str], *, client_factory=RagflowClient, **kwargs
) -> bool:
    """SDK/CLI uploads wait for verified upload and parse, never coerce 202 to True."""
    assert_remote_write(bound)
    if not file_paths or len(file_paths) > 20:
        raise ValueError("Expected between one and twenty files")
    root = (
        bound.store.path.parent
        / "spool"
        / digest({"owner": bound.scope.owner_id, "workspace": bound.scope.workspace_scope})[:24]
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    request_key = kwargs.get("idempotency_key") or uuid4().hex
    wait_timeout = float(kwargs.get("wait_timeout", 60))
    for index, value in enumerate(file_paths):
        path = Path(value)
        filename = DocumentValidator.validate_upload_safety(safe_name(path.name), None)
        if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("Upload file is unavailable or exceeds 100 MiB")
        if (
            sum(item.stat().st_size for item in root.glob("*") if item.is_file())
            + path.stat().st_size * 2
            > 1024 * 1024 * 1024
        ):
            raise ValueError("RAGFlow spool capacity reached")
        staging = root / (uuid4().hex + ".upload")
        hasher, size = hashlib.sha256(), 0
        with path.open("rb") as source, staging.open("xb") as target:
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > 100 * 1024 * 1024:
                    raise ValueError("Upload exceeds 100 MiB")
                hasher.update(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        if not size:
            raise ValueError("Upload file is empty")
        sha = hasher.hexdigest()
        item_key = f"{request_key}:{index}:upload"
        stable_path = root / (digest({"key": item_key, "sha": sha}) + ".upload")
        try:
            with stable_path.open("xb") as target, staging.open("rb") as source:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        except FileExistsError:
            pass
        op = record_operation(
            bound,
            "upload",
            {
                "name": filename,
                "spool_path": str(stable_path),
                "spool_root": str(root),
                "source_sha256": sha,
                "size": size,
            },
            idempotency_key=item_key,
        )
        completed = await _wait_for_operation(
            bound, op["operation_id"], client_factory=client_factory, wait_timeout=wait_timeout
        )
        document_id = bound.store.resolve_document(bound.scope, completed["result"]["document_ref"])
        parse = record_operation(
            bound,
            "parse",
            {
                "document_id": document_id,
                "use_ingestion_pipeline": bool(kwargs.get("use_ingestion_pipeline", False)),
            },
            idempotency_key=f"{request_key}:{index}:parse",
        )
        await _wait_for_operation(
            bound, parse["operation_id"], client_factory=client_factory, wait_timeout=wait_timeout
        )
    return True
