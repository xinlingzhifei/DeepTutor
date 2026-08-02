from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
import pytest

from deeptutor.knowledge.manager import KnowledgeBaseManager
from deeptutor.multi_user import knowledge_access
from deeptutor.multi_user.context import reset_current_user, set_current_user
from deeptutor.multi_user.models import CurrentUser, UserScope
from deeptutor.teaching.services.sources import knowledge_resource_exists


def _admin(tmp_path: Path) -> CurrentUser:
    return CurrentUser(
        id="admin",
        username="admin",
        role="admin",
        scope=UserScope(kind="admin", user_id="admin", root=tmp_path),
    )


def _user(tmp_path: Path) -> CurrentUser:
    return CurrentUser(
        id="alice",
        username="alice",
        role="user",
        scope=UserScope(kind="user", user_id="alice", root=tmp_path),
    )


def test_resolved_resource_uses_generation_and_stale_identity_does_not_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb_dir = tmp_path / "math"
    kb_dir.mkdir()
    manager = KnowledgeBaseManager(base_dir=tmp_path)
    manager.register_knowledge_base("math")
    monkeypatch.setattr(knowledge_access, "admin_kb_manager", lambda: manager)
    monkeypatch.setattr(knowledge_access, "admin_kb_base_dir", lambda: tmp_path)
    knowledge_access._manager_for.cache_clear()
    token = set_current_user(_admin(tmp_path))
    try:
        first = knowledge_access.resolve_kb("math")
        assert first.id == f"admin:kb:{first.generation_id}"
        assert knowledge_access.resolve_kb(first.id) == first
        assert knowledge_resource_exists(first)

        manager.config["knowledge_bases"].pop("math")
        manager._save_config()
        manager.register_knowledge_base("math")
        second = knowledge_access.resolve_kb("math")

        assert second.generation_id != first.generation_id
        assert not knowledge_resource_exists(first)
        with pytest.raises(HTTPException) as exc:
            knowledge_access.resolve_kb(first.id)
        assert exc.value.status_code == 404
    finally:
        reset_current_user(token)
        knowledge_access._manager_for.cache_clear()


def test_prefixed_user_name_is_an_input_alias_but_output_identity_is_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_root = tmp_path / "user"
    admin_root = tmp_path / "admin"
    (user_root / "course-a").mkdir(parents=True)
    admin_root.mkdir()
    user_manager = KnowledgeBaseManager(base_dir=user_root)
    user_manager.register_knowledge_base("course-a")
    admin_manager = KnowledgeBaseManager(base_dir=admin_root)
    monkeypatch.setattr(knowledge_access, "current_kb_manager", lambda: user_manager)
    monkeypatch.setattr(knowledge_access, "admin_kb_manager", lambda: admin_manager)
    monkeypatch.setattr(knowledge_access, "current_kb_base_dir", lambda: user_root)
    monkeypatch.setattr(knowledge_access, "admin_kb_base_dir", lambda: admin_root)
    monkeypatch.setattr(knowledge_access, "load_grant", lambda _user_id: {})
    token = set_current_user(_user(tmp_path))
    try:
        first = knowledge_access.resolve_kb("user:kb:course-a")
        assert first.name == "course-a"
        assert first.id == f"user:kb:{first.generation_id}"

        user_manager.config["knowledge_bases"].pop("course-a")
        user_manager._save_config()
        user_manager.register_knowledge_base("course-a")
        current = knowledge_access.resolve_kb("user:kb:course-a")

        assert current.generation_id != first.generation_id
        with pytest.raises(HTTPException, match="stale"):
            knowledge_access.resolve_kb(first.id)
    finally:
        reset_current_user(token)


def test_legacy_name_grant_fails_closed_and_generation_grant_does_not_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_root = tmp_path / "admin"
    user_root = tmp_path / "user"
    (admin_root / "math").mkdir(parents=True)
    user_root.mkdir()
    admin_manager = KnowledgeBaseManager(base_dir=admin_root)
    admin_manager.register_knowledge_base("math")
    user_manager = KnowledgeBaseManager(base_dir=user_root)
    monkeypatch.setattr(knowledge_access, "current_kb_manager", lambda: user_manager)
    monkeypatch.setattr(knowledge_access, "admin_kb_manager", lambda: admin_manager)
    monkeypatch.setattr(knowledge_access, "current_kb_base_dir", lambda: user_root)
    monkeypatch.setattr(knowledge_access, "admin_kb_base_dir", lambda: admin_root)
    grant: dict[str, object] = {
        "knowledge_bases": [{"resource_id": "admin:kb:math", "name": "math"}]
    }
    monkeypatch.setattr(knowledge_access, "load_grant", lambda _user_id: grant)
    token = set_current_user(_user(tmp_path))
    try:
        with pytest.raises(HTTPException) as legacy_error:
            knowledge_access.resolve_kb("admin:kb:math")
        assert legacy_error.value.status_code == 403

        first_generation = admin_manager.get_kb_entry("math")["generation_id"]
        first_id = f"admin:kb:{first_generation}"
        grant["knowledge_bases"] = [{"resource_id": first_id, "name": "math"}]
        assert knowledge_access.resolve_kb("admin:kb:math").id == first_id

        admin_manager.config["knowledge_bases"].pop("math")
        admin_manager._save_config()
        admin_manager.register_knowledge_base("math")
        with pytest.raises(HTTPException):
            knowledge_access.resolve_kb(first_id)
        with pytest.raises(HTTPException) as recreated_error:
            knowledge_access.resolve_kb("admin:kb:math")
        assert recreated_error.value.status_code == 403
    finally:
        reset_current_user(token)


@pytest.mark.parametrize(
    ("entry", "backing_exists"),
    [
        ({"type": "linked", "external_path": "{path}"}, True),
        ({"type": "linked", "external_path": "{missing}"}, False),
        ({"type": "obsidian", "vault_path": "{path}"}, True),
        ({"type": "subagent", "agent_kind": "codex", "cwd": "{path}"}, True),
        ({"type": "subagent", "agent_kind": "codex", "cwd": "{missing}"}, False),
        ({"type": "subagent", "agent_kind": "partner", "partner_id": "p-1"}, True),
        ({"type": "lightrag_server", "server_url": "https://kb.invalid"}, True),
        ({"type": "lightrag_server", "server_url": ""}, False),
    ],
)
def test_connected_resource_requires_exact_generation_and_backing_target(
    tmp_path: Path,
    entry: dict[str, str],
    backing_exists: bool,
) -> None:
    backing = tmp_path / "backing"
    backing.mkdir()
    manager = KnowledgeBaseManager(base_dir=tmp_path / "managed")
    rendered = {
        key: value.format(path=backing, missing=tmp_path / "missing")
        for key, value in entry.items()
    }
    manager.config["knowledge_bases"]["connected"] = rendered
    manager._save_config()
    generation = manager.get_kb_entry("connected")["generation_id"]
    resource = knowledge_access.KnowledgeResource(
        id=f"admin:kb:{generation}",
        name="connected",
        base_dir=manager.base_dir,
        source="admin",
        generation_id=generation,
    )

    assert knowledge_resource_exists(resource) is backing_exists

    stale = knowledge_access.KnowledgeResource(
        id="admin:kb:00000000-0000-0000-0000-000000000000",
        name="connected",
        base_dir=manager.base_dir,
        source="admin",
        generation_id="00000000-0000-0000-0000-000000000000",
    )
    assert not knowledge_resource_exists(stale)
