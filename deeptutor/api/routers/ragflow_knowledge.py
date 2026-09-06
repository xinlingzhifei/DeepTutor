"""Authenticated RAGFlow knowledge-management routes (mounted by api.main)."""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from deeptutor.knowledge.naming import validate_knowledge_base_name
from deeptutor.knowledge.ragflow.binding import (
    activate_managed_binding,
    reserve_managed_binding,
    resolve_binding,
)
from deeptutor.knowledge.ragflow.lifecycle import (
    RESERVED_METADATA,
    assert_remote_write,
    checked_call,
    client_context,
    digest,
    document_status,
    ensure_available_content,
    execute_operation,
    public_operation,
    reconcile_operation,
    record_operation,
    remote_chunk,
    remote_document,
    safe_name,
)
from deeptutor.knowledge.ragflow.operations import OperationConflict, OperationScope
from deeptutor.knowledge.ragflow.profiles import (
    ProfileStore,
    get_profile_config,
    profile_fingerprint,
)
from deeptutor.multi_user.context import get_current_user
from deeptutor.multi_user.knowledge_access import assert_writable, current_kb_manager
from deeptutor.multi_user.paths import current_owner_id
from deeptutor.services.rag.pipelines.ragflow.client import RagflowClient, RagflowError
from deeptutor.services.rag.pipelines.ragflow.probe import probe_server
from deeptutor.utils.document_validator import DocumentValidator


class SafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RagflowError as exc:
                # Upstream 401 is a provider fault, never a platform logout response.
                return JSONResponse(status_code=502, content={"detail": exc.error_type})
            except OperationConflict:
                return JSONResponse(
                    status_code=409, content={"detail": "ragflow_operation_conflict"}
                )
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid_ragflow_request"})

        return safe_handler


router = APIRouter(route_class=SafeRoute)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_SPOOL_BYTES = 1024 * 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileInput(StrictModel):
    base_url: str
    api_key: str | None = None
    allow_http: bool = False
    connect_timeout: float = Field(5, gt=0, le=60)
    read_timeout: float = Field(60, gt=0, le=300)
    upload_timeout: float = Field(300, gt=0, le=300)


class ProfileSelection(StrictModel):
    profile_id: str


class ConnectInput(ProfileSelection):
    name: str
    dataset_id: str
    selected_document_ids: list[str] = Field(max_length=1000)
    access_mode: Literal["read_only", "manage_documents"] = "read_only"
    description: str = Field("", max_length=1000)


class CreateInput(ProfileSelection):
    name: str
    description: str = Field("", max_length=1000)
    chunk_method: str | None = None
    embedding_model: str | None = None


class DocumentInput(StrictModel):
    name: str = Field(min_length=1, max_length=180)
    type: Literal["empty", "web"] = "empty"
    url: str | None = None


class DocumentPatch(StrictModel):
    name: str | None = Field(None, min_length=1, max_length=180)
    meta_fields: dict | None = None
    chunk_method: str | None = None
    parser_config: dict | None = None


class ParseInput(StrictModel):
    use_ingestion_pipeline: bool = False
    rebuild: bool = False
    confirm_rebuild: bool = False


class ChunkInput(StrictModel):
    content: str = Field(min_length=1, max_length=200000)
    important_keywords: list[str] | None = Field(None, max_length=100)
    questions: list[str] | None = Field(None, max_length=100)
    tag_kwd: list[str] | None = Field(None, max_length=100)


class ChunkPatch(StrictModel):
    content: str | None = Field(None, min_length=1, max_length=200000)
    important_keywords: list[str] | None = Field(None, max_length=100)
    questions: list[str] | None = Field(None, max_length=100)
    tag_kwd: list[str] | None = Field(None, max_length=100)
    available: bool | None = None


class AvailabilityInput(StrictModel):
    available: bool


class VisibilityInput(StrictModel):
    visibility: Literal["draft", "active", "disabled", "archived"]
    expected_revision: int = Field(ge=1)


class DeleteDatasetInput(StrictModel):
    confirm_name: str


class RetrievalTestInput(StrictModel):
    query: str = Field(min_length=1, max_length=20000)
    preview_document_refs: list[str] | None = Field(None, min_length=1, max_length=1000)


class RetrievalSettingsInput(StrictModel):
    page_size: int = Field(8, ge=1, le=20)
    similarity_threshold: float = Field(0.2, ge=0, le=1)
    vector_similarity_weight: float = Field(0.3, ge=0, le=1)
    rerank_id: str | None = Field(None, max_length=256)


class ReconcileInput(StrictModel):
    expected_revision: int = Field(ge=1)


def _managed(kb_ref: str, *, remote_write=False):
    # Local management is separate from the permission to write remote content.
    assert_writable(kb_ref)
    bound = resolve_binding(kb_ref, require_write=remote_write)
    bound.recheck()
    return bound


def _document_id(bound, document_ref):
    try:
        return bound.store.resolve_document(bound.scope, document_ref)
    except ValueError:
        raise HTTPException(404, "document_reference_not_found") from None


def _chunk_id(bound, document_ref, chunk_ref):
    try:
        return bound.store.resolve_chunk(bound.scope, document_ref, chunk_ref)
    except ValueError:
        raise HTTPException(404, "chunk_reference_not_found") from None


def _enqueue(bound, action, request, background_tasks, key=None):
    op = record_operation(bound, action, request, idempotency_key=key or uuid4().hex)
    background_tasks.add_task(execute_operation, bound, op["operation_id"])
    return public_operation(op)


def _document_view(bound, document):
    if document.get("dataset_id", bound.binding["dataset_id"]) != bound.binding["dataset_id"]:
        raise HTTPException(502, "ragflow_document_scope_mismatch")
    row = bound.store.register_document(
        bound.scope, document["id"], title=safe_name(document.get("name"))
    )
    return {
        "document_ref": row["document_ref"],
        "name": safe_name(document.get("name")),
        "state": document_status(document),
        "visibility": row["visibility"],
        "revision": row["revision"],
        "chunk_count": max(0, int(document.get("chunk_count", 0) or 0)),
        "progress": max(0, min(1, float(document.get("progress", 0) or 0))),
    }


def _chunk_view(bound, document_ref, document_id, chunk):
    if (
        chunk.get("document_id", document_id) != document_id
        or chunk.get("dataset_id", bound.binding["dataset_id"]) != bound.binding["dataset_id"]
    ):
        raise HTTPException(502, "ragflow_chunk_scope_mismatch")
    ref = bound.store.register_chunk(bound.scope, document_ref, chunk["id"])
    return {
        "chunk_ref": ref,
        "document_ref": document_ref,
        **{
            key: chunk[key]
            for key in (
                "content",
                "available",
                "important_keywords",
                "questions",
                "tag_kwd",
                "positions",
            )
            if key in chunk
        },
    }


@router.get("/rag-pipelines/ragflow/profiles")
async def list_profiles():
    return {"profiles": ProfileStore().list(current_owner_id())}


@router.get("/rag-pipelines/ragflow/profiles/{profile_id}")
async def get_profile(profile_id: str):
    return ProfileStore().public(current_owner_id(), profile_id)


@router.put("/rag-pipelines/ragflow/profiles/{profile_id}")
async def save_profile(profile_id: str, request: ProfileInput):
    if not get_current_user().is_admin:
        raise HTTPException(403, "ragflow_profile_admin_required")
    return ProfileStore().save(
        current_owner_id(), profile_id, request.model_dump(exclude_none=True)
    )


@router.post("/ragflow/probe")
async def probe(request: ProfileSelection):
    owner = current_owner_id()
    before = profile_fingerprint(ProfileStore().load(owner, request.profile_id))
    result = await probe_server(get_profile_config(owner, request.profile_id))
    if before != profile_fingerprint(ProfileStore().load(owner, request.profile_id)):
        raise HTTPException(409, "ragflow_profile_changed")
    return result


@router.get("/ragflow/datasets")
async def list_datasets(
    profile_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)
):
    owner = current_owner_id()
    before = profile_fingerprint(ProfileStore().load(owner, profile_id))
    datasets = await RagflowClient(get_profile_config(owner, profile_id)).list_datasets(
        page=page, page_size=page_size
    )
    if before != profile_fingerprint(ProfileStore().load(owner, profile_id)):
        raise HTTPException(409, "ragflow_profile_changed")
    return {
        "datasets": [{"id": item["id"], "name": safe_name(item.get("name"))} for item in datasets]
    }


@router.post("/connect-ragflow")
async def connect(request: ConnectInput):
    name = validate_knowledge_base_name(request.name)
    owner = current_owner_id()
    before = profile_fingerprint(ProfileStore().load(owner, request.profile_id))
    client = RagflowClient(get_profile_config(owner, request.profile_id))
    datasets = await client.list_datasets(id=request.dataset_id, page=1, page_size=2)
    if len([item for item in datasets if item.get("id") == request.dataset_id]) != 1:
        raise HTTPException(404, "ragflow_dataset_not_found")
    selected = []
    for document_id in dict.fromkeys(request.selected_document_ids):
        page = await client.list_documents(request.dataset_id, id=document_id, page=1, page_size=2)
        docs = [
            doc
            for doc in page["docs"]
            if doc.get("id") == document_id
            and doc.get("dataset_id", request.dataset_id) == request.dataset_id
        ]
        if (
            len(docs) != 1
            or document_status(docs[0]) != "ready"
            or int(docs[0].get("chunk_count", 0) or 0) < 1
        ):
            raise HTTPException(409, "selected_document_not_ready")
        selected.append(docs[0])
    if before != profile_fingerprint(ProfileStore().load(owner, request.profile_id)):
        raise HTTPException(409, "ragflow_profile_changed")
    current_kb_manager().register_ragflow_kb(
        name,
        request.profile_id,
        request.dataset_id,
        credential_owner_id=owner,
        ownership="attached",
        access_mode=request.access_mode,
        description=request.description,
    )
    bound = resolve_binding(name)
    for document in selected:
        bound.store.register_document(
            bound.scope, document["id"], visibility="active", title=safe_name(document.get("name"))
        )
    bound.recheck()
    return {
        "id": bound.resource.id,
        "name": name,
        "type": "ragflow",
        "ownership": "attached",
        "access_mode": request.access_mode,
        "active_documents": len(selected),
    }


@router.post("/ragflow/create")
async def create_managed(request: CreateInput, idempotency_key: str | None = Header(None)):
    bound = reserve_managed_binding(
        validate_knowledge_base_name(request.name),
        request.profile_id,
        description=request.description,
    )
    if bound.binding.get("dataset_id"):
        async with client_context(bound, RagflowClient) as client:
            datasets = await checked_call(
                bound, client.list_datasets, id=bound.binding["dataset_id"], page=1, page_size=2
            )
        if not any(item.get("id") == bound.binding["dataset_id"] for item in datasets):
            raise HTTPException(410, "ragflow_dataset_deleted")
        return {"id": bound.resource.id, "name": bound.resource.name, "status": "ready"}
    config = request.model_dump(include={"chunk_method", "embedding_model"}, exclude_none=True)
    op = record_operation(
        bound,
        "create_dataset",
        {"name": request.name, "config": config},
        idempotency_key=idempotency_key or "create-dataset",
    )
    op = await execute_operation(bound, op["operation_id"])
    if op["state"] == "RECONCILED":
        original_scope = bound.scope
        bound = activate_managed_binding(bound, op["result"]["dataset_id"])
        op = bound.store.mark_binding_activated(original_scope, op["operation_id"])
    response = {**public_operation(op), "id": bound.resource.id, "name": bound.resource.name}
    return JSONResponse(status_code=201 if op["state"] == "RECONCILED" else 202, content=response)


@router.get("/{kb_ref}/documents")
async def list_documents(
    kb_ref: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    keywords: str = Query("", max_length=200),
):
    bound = _managed(kb_ref)
    async with client_context(bound, RagflowClient) as client:
        data = await checked_call(
            bound,
            client.list_documents,
            bound.binding["dataset_id"],
            page=page,
            page_size=page_size,
            keywords=keywords,
        )
    return {
        "documents": [_document_view(bound, doc) for doc in data["docs"]],
        "total": data["total"],
    }


@router.post("/{kb_ref}/documents", status_code=202)
async def create_document(
    kb_ref: str,
    request: DocumentInput,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    if request.type == "web" or request.url:
        raise HTTPException(400, "remote_web_ingestion_disabled")
    bound = _managed(kb_ref, remote_write=True)
    return _enqueue(
        bound,
        "create_document",
        {"name": safe_name(request.name)},
        background_tasks,
        idempotency_key,
    )


async def upload_files(
    kb_ref: str,
    files: list[UploadFile],
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = None,
):
    bound = _managed(kb_ref, remote_write=True)
    assert_remote_write(bound)
    if not files or len(files) > 20:
        raise HTTPException(400, "invalid_upload_count")
    key = idempotency_key or uuid4().hex
    root = (
        bound.store.path.parent
        / "spool"
        / digest({"owner": bound.scope.owner_id, "workspace": bound.scope.workspace_scope})[:24]
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    used = sum(path.stat().st_size for path in root.glob("*") if path.is_file())
    if used >= MAX_SPOOL_BYTES:
        raise HTTPException(413, "ragflow_spool_capacity_reached")
    prepared, batch_size = [], 0
    for index, upload in enumerate(files):
        filename = DocumentValidator.validate_upload_safety(safe_name(upload.filename), None)
        staging = root / (uuid4().hex + ".part")
        hasher, size = hashlib.sha256(), 0
        with staging.open("xb") as handle:
            while block := await upload.read(1024 * 1024):
                bound.recheck()
                size += len(block)
                batch_size += len(block)
                if batch_size > MAX_UPLOAD_BYTES or used + batch_size * 2 > MAX_SPOOL_BYTES:
                    raise HTTPException(413, "ragflow_upload_size_limit")
                handle.write(block)
                hasher.update(block)
            handle.flush()
            os.fsync(handle.fileno())
        if not size:
            raise HTTPException(400, "empty_upload")
        sha = hasher.hexdigest()
        item_key = f"{key}:{index}"
        destination = root / (digest({"key": item_key, "sha": sha}) + ".upload")
        try:
            # Exclusive creation keeps retry payload paths stable without overwriting.
            with destination.open("xb") as target, staging.open("rb") as source:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        except FileExistsError:
            pass
        prepared.append(
            (
                item_key,
                {
                    "name": filename,
                    "spool_path": str(destination),
                    "spool_root": str(root),
                    "source_sha256": sha,
                    "size": size,
                },
            )
        )
    bound.recheck()
    operations = [
        _enqueue(bound, "upload", request, background_tasks, item_key)
        for item_key, request in prepared
    ]
    if len(operations) == 1:
        return operations[0]
    return {"accepted": True, "operations": operations}


@router.post("/{kb_ref}/documents/upload", status_code=202)
async def upload(
    kb_ref: str,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    idempotency_key: str | None = Header(None),
):
    return await upload_files(kb_ref, files, background_tasks, idempotency_key)


@router.get("/{kb_ref}/ragflow/binding")
async def get_binding(kb_ref: str):
    """Expose effective UI capabilities without connection credentials or remote IDs."""
    bound = resolve_binding(kb_ref)
    bound.recheck()
    can_manage = not bound.resource.read_only
    can_reconcile = False
    if can_manage and bound.binding["access_mode"] == "manage_documents":
        try:
            writable = resolve_binding(kb_ref, require_write=True)
            writable.recheck()
            can_reconcile = True
        except HTTPException as exc:
            if exc.status_code not in {403, 409}:
                raise
    return {
        "id": bound.resource.id,
        "name": bound.resource.name,
        "ownership": bound.binding["ownership"],
        "access_mode": bound.binding["access_mode"],
        "lifecycle_state": bound.binding["lifecycle_state"],
        "read_only": bound.resource.read_only,
        "can_manage": can_manage,
        "can_reconcile": can_reconcile,
        "can_write": can_reconcile and bound.binding["lifecycle_state"] == "active",
    }


@router.get("/{kb_ref}/documents/{document_ref}")
async def get_document(kb_ref: str, document_ref: str):
    bound = _managed(kb_ref)
    document_id = _document_id(bound, document_ref)
    async with client_context(bound, RagflowClient) as client:
        doc = await remote_document(bound, client, document_id)
    return {
        **_document_view(bound, doc),
        "meta_fields": {
            key: value
            for key, value in (doc.get("meta_fields") or {}).items()
            if key not in RESERVED_METADATA
        },
        "chunk_method": doc.get("chunk_method"),
        "parser_config": doc.get("parser_config") or {},
    }


@router.get("/{kb_ref}/documents/{document_ref}/download")
async def download_document(kb_ref: str, document_ref: str):
    bound = _managed(kb_ref)
    document_id = _document_id(bound, document_ref)
    async with client_context(bound, RagflowClient) as client:
        doc = await remote_document(bound, client, document_id)
        body, mime = await checked_call(
            bound, client.download_document, bound.binding["dataset_id"], document_id
        )
    return Response(
        body,
        media_type=mime,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(safe_name(doc.get("name"))),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.patch("/{kb_ref}/documents/{document_ref}", status_code=202)
async def update_document(
    kb_ref: str,
    document_ref: str,
    request: DocumentPatch,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    patch = request.model_dump(exclude_none=True)
    if not patch or RESERVED_METADATA.intersection(patch.get("meta_fields", {})):
        raise HTTPException(400, "invalid_document_patch")
    if "name" in patch:
        patch["name"] = safe_name(patch["name"])
    return _enqueue(
        bound,
        "update_document",
        {"document_id": _document_id(bound, document_ref), "patch": patch},
        background_tasks,
        idempotency_key,
    )


@router.delete("/{kb_ref}/documents/{document_ref}", status_code=202)
async def delete_document(
    kb_ref: str,
    document_ref: str,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    return _enqueue(
        bound,
        "delete_document",
        {"document_id": _document_id(bound, document_ref)},
        background_tasks,
        idempotency_key,
    )


@router.post("/{kb_ref}/documents/{document_ref}/parse", status_code=202)
async def parse_document(
    kb_ref: str,
    document_ref: str,
    request: ParseInput,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    return _enqueue(
        bound,
        "parse",
        {"document_id": _document_id(bound, document_ref), **request.model_dump()},
        background_tasks,
        idempotency_key,
    )


@router.post("/{kb_ref}/documents/{document_ref}/cancel", status_code=202)
async def cancel_document(
    kb_ref: str,
    document_ref: str,
    request: ParseInput,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    return _enqueue(
        bound,
        "cancel",
        {"document_id": _document_id(bound, document_ref), **request.model_dump()},
        background_tasks,
        idempotency_key,
    )


@router.get("/{kb_ref}/documents/{document_ref}/chunks")
async def list_chunks(
    kb_ref: str,
    document_ref: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
):
    bound = _managed(kb_ref)
    document_id = _document_id(bound, document_ref)
    async with client_context(bound, RagflowClient) as client:
        await remote_document(bound, client, document_id)
        data = await checked_call(
            bound,
            client.list_chunks,
            bound.binding["dataset_id"],
            document_id,
            page=page,
            page_size=page_size,
        )
    return {
        "chunks": [
            _chunk_view(bound, document_ref, document_id, chunk) for chunk in data["chunks"]
        ],
        "total": data["total"],
    }


@router.post("/{kb_ref}/documents/{document_ref}/chunks", status_code=202)
async def add_chunk(
    kb_ref: str,
    document_ref: str,
    request: ChunkInput,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    return _enqueue(
        bound,
        "add_chunk",
        {
            "document_id": _document_id(bound, document_ref),
            "patch": request.model_dump(exclude_none=True),
        },
        background_tasks,
        idempotency_key,
    )


@router.get("/{kb_ref}/documents/{document_ref}/chunks/{chunk_ref}")
async def get_chunk(kb_ref: str, document_ref: str, chunk_ref: str):
    bound = _managed(kb_ref)
    document_id = _document_id(bound, document_ref)
    async with client_context(bound, RagflowClient) as client:
        chunk = await remote_chunk(
            bound, client, document_id, _chunk_id(bound, document_ref, chunk_ref)
        )
    return _chunk_view(bound, document_ref, document_id, chunk)


@router.patch("/{kb_ref}/documents/{document_ref}/chunks/{chunk_ref}", status_code=202)
async def update_chunk(
    kb_ref: str,
    document_ref: str,
    chunk_ref: str,
    request: ChunkPatch,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    patch = request.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(400, "empty_chunk_patch")
    return _enqueue(
        bound,
        "update_chunk",
        {
            "document_id": _document_id(bound, document_ref),
            "chunk_id": _chunk_id(bound, document_ref, chunk_ref),
            "patch": patch,
        },
        background_tasks,
        idempotency_key,
    )


@router.delete("/{kb_ref}/documents/{document_ref}/chunks/{chunk_ref}", status_code=202)
async def delete_chunk(
    kb_ref: str,
    document_ref: str,
    chunk_ref: str,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    return _enqueue(
        bound,
        "delete_chunk",
        {
            "document_id": _document_id(bound, document_ref),
            "chunk_id": _chunk_id(bound, document_ref, chunk_ref),
        },
        background_tasks,
        idempotency_key,
    )


@router.put("/{kb_ref}/documents/{document_ref}/chunks/{chunk_ref}/availability", status_code=202)
async def enable_chunk(
    kb_ref: str,
    document_ref: str,
    chunk_ref: str,
    request: AvailabilityInput,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    return _enqueue(
        bound,
        "enable_chunk",
        {
            "document_id": _document_id(bound, document_ref),
            "chunk_id": _chunk_id(bound, document_ref, chunk_ref),
            "available": request.available,
        },
        background_tasks,
        idempotency_key,
    )


@router.put("/{kb_ref}/documents/{document_ref}/visibility")
async def set_visibility(kb_ref: str, document_ref: str, request: VisibilityInput):
    bound = _managed(kb_ref)
    document_id = _document_id(bound, document_ref)
    if request.visibility == "active":
        async with client_context(bound, RagflowClient) as client:
            doc = await remote_document(bound, client, document_id)
            if document_status(doc) != "ready" or int(doc.get("chunk_count", 0) or 0) < 1:
                raise HTTPException(409, "document_not_ready_for_activation")
            await ensure_available_content(bound, client, document_id)
    bound.recheck()
    row = bound.store.set_visibility(
        bound.scope, document_ref, request.visibility, expected_revision=request.expected_revision
    )
    return {
        "document_ref": document_ref,
        "visibility": row["visibility"],
        "revision": row["revision"],
    }


@router.get("/{kb_ref}/operations")
async def list_operations(kb_ref: str):
    bound = _managed(kb_ref)
    return {
        "operations": [
            public_operation(op) for op in bound.store.list_operations_for_generation(bound.scope)
        ]
    }


@router.post("/{kb_ref}/retrieval-test")
async def retrieval_test(kb_ref: str, request: RetrievalTestInput):
    from deeptutor.services.rag.pipelines.ragflow.pipeline import RagflowPipeline

    bound = _managed(kb_ref)
    return await RagflowPipeline(client_factory=RagflowClient).search_bound(
        bound, request.query, preview_document_refs=request.preview_document_refs
    )


@router.get("/{kb_ref}/retrieval-settings")
async def get_retrieval_settings(kb_ref: str):
    bound = _managed(kb_ref)
    return RetrievalSettingsInput(**(bound.binding.get("retrieval") or {})).model_dump()


@router.put("/{kb_ref}/retrieval-settings")
async def set_retrieval_settings(kb_ref: str, request: RetrievalSettingsInput):
    bound = _managed(kb_ref)
    settings = request.model_dump(exclude_none=True)
    bound.recheck()

    def update(config):
        entry = config.get("knowledge_bases", {}).get(bound.resource.name)
        if (
            not entry
            or entry.get("generation_id") != bound.resource.generation_id
            or entry.get("ragflow") != bound.binding
        ):
            raise HTTPException(409, "ragflow_binding_changed")
        entry["ragflow"] = {**bound.binding, "retrieval": settings}

    bound.manager._mutate_config(update)
    return settings


@router.get("/{kb_ref}/operations/{operation_id}")
async def get_operation(kb_ref: str, operation_id: str):
    bound = _managed(kb_ref)
    op = bound.store.get_operation_for_generation(bound.scope, operation_id)
    if op is None:
        raise HTTPException(404, "operation_not_found")
    bound.recheck()
    return public_operation(op)


@router.get("/{kb_ref}/operations/{operation_id}/events")
async def get_events(kb_ref: str, operation_id: str, after_id: int = Query(0, ge=0)):
    bound = _managed(kb_ref)
    op = bound.store.get_operation_for_generation(bound.scope, operation_id)
    if op is None:
        raise HTTPException(404, "operation_not_found")
    scope = OperationScope(**op["scope"])
    events = bound.store.events(scope, operation_id, after_id=after_id)
    bound.recheck()
    return {
        "events": [
            {
                key: event[key]
                for key in ("id", "state", "revision", "event_type", "error_type", "created_at")
                if key in event
            }
            for event in events
        ]
    }


@router.post("/{kb_ref}/operations/{operation_id}/reconcile", status_code=202)
async def authorize_reconciliation(
    kb_ref: str, operation_id: str, request: ReconcileInput, background_tasks: BackgroundTasks
):
    bound = _managed(kb_ref, remote_write=True)
    if bound.store.get_operation(bound.scope, operation_id) is None:
        raise HTTPException(404, "operation_not_found")
    snapshot = bound.recovery_snapshot()
    bound.recheck()
    op = bound.store.reauthorize_reconciliation(
        bound.scope,
        operation_id,
        expected_revision=request.expected_revision,
        binding_snapshot=snapshot,
        actor_id=snapshot["actor_id"],
    )
    background_tasks.add_task(reconcile_operation, bound, operation_id)
    return public_operation(op)


@router.delete("/{kb_ref}/ragflow/dataset", status_code=202)
async def delete_dataset(
    kb_ref: str,
    request: DeleteDatasetInput,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(None),
):
    bound = _managed(kb_ref, remote_write=True)
    if request.confirm_name != bound.resource.name:
        raise HTTPException(400, "dataset_delete_confirmation_mismatch")
    return _enqueue(bound, "delete_dataset", {}, background_tasks, idempotency_key)
