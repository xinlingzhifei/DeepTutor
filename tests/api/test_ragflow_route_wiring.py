from __future__ import annotations

import importlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def app_modules(monkeypatch, tmp_path):
    knowledge = importlib.import_module("deeptutor.api.routers.knowledge")
    ragflow = importlib.import_module("deeptutor.api.routers.ragflow_knowledge")
    from deeptutor.knowledge.manager import KnowledgeBaseManager
    from deeptutor.multi_user import knowledge_access, paths
    from deeptutor.multi_user.context import reset_current_user, set_current_user
    from deeptutor.multi_user.models import CurrentUser, UserScope
    from deeptutor.services import auth
    from deeptutor.services import config as config_module
    from deeptutor.services.config.knowledge_base_config import KnowledgeBaseConfigService

    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(paths, "SYSTEM_ROOT", tmp_path / "system")
    manager = KnowledgeBaseManager(base_dir=tmp_path / "kb")
    config_service = KnowledgeBaseConfigService(config_path=manager.config_file)
    monkeypatch.setattr(config_module, "get_kb_config_service", lambda: config_service)
    monkeypatch.setattr(knowledge, "get_kb_manager", lambda: manager)
    monkeypatch.setattr(knowledge, "_current_kb_base_dir", lambda: manager.base_dir)
    monkeypatch.setattr(knowledge_access, "admin_kb_manager", lambda: manager)
    monkeypatch.setattr(knowledge_access, "admin_kb_base_dir", lambda: manager.base_dir)
    token = set_current_user(
        CurrentUser("local-admin", "admin", "admin", UserScope("admin", "local-admin", tmp_path))
    )
    app = FastAPI()
    app.include_router(ragflow.router, prefix="/api/v1/knowledge")
    app.include_router(knowledge.router, prefix="/api/v1/knowledge")
    try:
        yield TestClient(app), knowledge, ragflow
    finally:
        reset_current_user(token)
        knowledge_access._manager_for.cache_clear()


def test_existing_upload_delegates_ragflow_without_local_raw_directory(
    app_modules, monkeypatch, tmp_path
):
    client, knowledge, ragflow = app_modules
    manager = SimpleNamespace(
        _load_config=lambda: {
            "knowledge_bases": {"course": {"type": "ragflow", "rag_provider": "ragflow"}}
        }
    )
    monkeypatch.setattr(knowledge, "_writable_kb", lambda _: (manager, "course", tmp_path))
    seen = []

    async def upload(kb_ref, files, background_tasks, idempotency_key=None):
        seen.append((kb_ref, await files[0].read(), idempotency_key))
        return {"operation_id": "op-safe", "state": "INTENT_RECORDED"}

    monkeypatch.setattr(ragflow, "upload_files", upload)
    response = client.post(
        "/api/v1/knowledge/course/upload",
        files={"files": ("book.txt", b"lesson")},
        headers={"Idempotency-Key": "upload-1"},
    )
    assert response.status_code == 202, response.text
    assert seen == [("course", b"lesson", "upload-1")]
    assert not (tmp_path / "course").exists()


def test_existing_create_delegates_empty_managed_ragflow(app_modules, monkeypatch):
    client, _, ragflow = app_modules
    seen = []

    async def create(request, idempotency_key=None):
        seen.append((request.name, request.profile_id, idempotency_key))
        return JSONResponse(
            status_code=201, content={"id": "admin:kb:generation", "status": "ready"}
        )

    monkeypatch.setattr(ragflow, "create_managed", create)
    response = client.post(
        "/api/v1/knowledge/create",
        data={"name": "course", "rag_provider": "ragflow", "profile_id": "default"},
        headers={"Idempotency-Key": "create-1"},
    )
    assert response.status_code == 201
    assert seen == [("course", "default", "create-1")]


def test_new_routes_do_not_shadow_existing_upload(app_modules):
    client, _, _ = app_modules
    matches = [
        route for route in client.app.routes if getattr(route, "path", "").endswith("/upload")
    ]
    assert [route.path for route in matches] == [
        "/api/v1/knowledge/{kb_ref}/documents/upload",
        "/api/v1/knowledge/{kb_name}/upload",
    ]


def test_ragflow_create_rejects_files_before_creating_remote_resources(app_modules, monkeypatch):
    client, _, ragflow = app_modules

    async def unexpected(*args, **kwargs):
        pytest.fail("Dataset must not be created when the request cannot preserve its uploads")

    monkeypatch.setattr(ragflow, "create_managed", unexpected)
    response = client.post(
        "/api/v1/knowledge/create",
        data={"name": "course", "rag_provider": "ragflow", "profile_id": "default"},
        files={"files": ("book.txt", b"lesson")},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "ragflow_create_then_upload"


def test_main_mounts_ragflow_routes_with_auth_dependencies():
    from deeptutor.api.main import app

    routes = [
        route
        for route in app.routes
        if getattr(route, "path", "") == "/api/v1/knowledge/ragflow/create"
    ]
    assert len(routes) == 1
    assert routes[0].dependencies


@pytest.mark.parametrize(
    "payload",
    [
        {"ragflow": {"ownership": "managed"}},
        {"rag_provider": "ragflow", "type": "ragflow"},
    ],
)
def test_legacy_config_cannot_forge_ragflow_ownership(app_modules, payload):
    client, _, _ = app_modules
    response = client.put("/api/v1/knowledge/course/config", json=payload)
    assert response.status_code == 409
