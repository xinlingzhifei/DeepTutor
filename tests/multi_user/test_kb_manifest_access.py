"""``resolve_kb_manifest`` is the one seam that reads a KB's document list.

Both consumers (the chat system-prompt inventory and the ``kb_files`` tool) go
through it, so per-user visibility has to hold here: a user's own KBs resolve,
an admin KB resolves only while it is granted, and anything else yields
``None`` rather than a listing.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
import pytest

from deeptutor.knowledge.manager import KnowledgeBaseManager
import deeptutor.multi_user.knowledge_access as knowledge_access_module
from deeptutor.multi_user.knowledge_access import (
    AuthorizedKnowledgeSource,
    resolve_authorized_source,
    resolve_authorized_source_descriptor,
    resolve_kb_manifest,
)
from deeptutor.multi_user.models import CurrentUser, KnowledgeResource, UserScope
from deeptutor.services.rag.retrieval_view import stamp_retrieval_view_signature


def _make_kb(manager: KnowledgeBaseManager, name: str, *files: str) -> None:
    """Register a KB and stage documents into it, as an upload would."""
    raw = Path(manager.base_dir) / name / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for filename in files:
        (raw / filename).write_bytes(b"x" * 512)
    manager.register_knowledge_base(name, description=f"test KB {name}")


def test_user_sees_their_own_kb(mu_isolated_root, as_user) -> None:
    from deeptutor.multi_user.knowledge_access import current_kb_manager

    with as_user("u_alice", role="user"):
        _make_kb(current_kb_manager(), "alice-kb", "a.pdf", "b.pdf")

        manifest = resolve_kb_manifest("alice-kb")

        assert manifest is not None
        assert manifest.total == 2
        assert [document.name for document in manifest.documents] == ["a.pdf", "b.pdf"]


def test_pattern_and_limit_reach_the_filesystem(mu_isolated_root, as_user) -> None:
    from deeptutor.multi_user.knowledge_access import current_kb_manager

    with as_user("u_alice", role="user"):
        _make_kb(current_kb_manager(), "alice-kb", "a.pdf", "b.pdf", "notes.md")

        manifest = resolve_kb_manifest("alice-kb", pattern="*.pdf", limit=1)

        assert manifest is not None
        assert (manifest.total, manifest.matched, manifest.omitted) == (3, 2, 1)


def test_unknown_kb_yields_no_manifest(mu_isolated_root, as_user) -> None:
    with as_user("u_alice", role="user"):
        assert resolve_kb_manifest("does-not-exist") is None


def test_ungranted_admin_kb_yields_no_manifest(mu_isolated_root, as_user) -> None:
    """Naming an admin KB directly must not leak its file list (403 → None)."""
    from deeptutor.multi_user.knowledge_access import admin_kb_manager

    with as_user("u_admin", role="admin"):
        _make_kb(admin_kb_manager(), "admin-kb", "secret.pdf")

    with as_user("u_alice", role="user"):
        assert resolve_kb_manifest("admin:kb:admin-kb") is None


def test_empty_reference_yields_no_manifest(mu_isolated_root, as_user) -> None:
    with as_user("u_alice", role="user"):
        assert resolve_kb_manifest("") is None
        assert resolve_kb_manifest(None) is None


def test_authorized_source_exposes_only_generation_pinned_identity(
    mu_isolated_root,
    as_user,
) -> None:
    from deeptutor.multi_user.knowledge_access import current_kb_manager

    with as_user("u_alice", role="user"):
        _make_kb(current_kb_manager(), "alice-kb", "a.pdf")

        source = resolve_authorized_source("alice-kb")

        assert source.resource_id == f"user:kb:{source.generation_id}"
        assert source.resource_owner_id == "u_alice"
        assert source.name == "alice-kb"
        assert source.read_only is True
        assert not hasattr(source, "base_dir")
        assert not hasattr(source, "_base_dir")
        assert not hasattr(source, "create_rag_service")
        assert not hasattr(source, "initialize")
        assert not hasattr(source, "add_documents")
        assert not hasattr(source, "delete")
        assert str(mu_isolated_root.resolve()) not in repr(source)


def test_authorized_source_descriptor_does_not_audit_preflight_but_search_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    resource = KnowledgeResource(
        id=f"admin:kb:{generation}",
        name="assigned-kb",
        base_dir=tmp_path,
        source="admin",
        assigned=True,
        read_only=True,
        generation_id=generation,
    )

    class _Manager:
        base_dir = tmp_path

        def get_kb_entry(self, _name: str) -> dict[str, str]:
            return {"generation_id": generation}

    audits: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        knowledge_access_module,
        "resolve_kb",
        lambda _ref, *, require_write=False: resource,
    )
    monkeypatch.setattr(
        knowledge_access_module,
        "manager_for_resource",
        lambda _resource: _Manager(),
    )
    monkeypatch.setattr(
        knowledge_access_module,
        "get_current_user",
        lambda: _current_user(tmp_path),
    )
    monkeypatch.setattr(
        "deeptutor.services.rag.provider_binding.resolve_bound_provider",
        lambda *_args: "llamaindex",
    )
    monkeypatch.setattr(
        "deeptutor.multi_user.audit.log_usage",
        lambda resource_type, resource_id, action: audits.append(
            (resource_type, resource_id, action)
        ),
    )

    preflight = resolve_authorized_source_descriptor("assigned-kb")

    assert preflight.resource_id == resource.id
    assert audits == []

    searched = resolve_authorized_source("assigned-kb")

    assert searched == preflight
    assert audits == [("knowledge_base", resource.id, "rag_query")]


def _current_user(tmp_path: Path, user_id: str = "u_alice") -> CurrentUser:
    return CurrentUser(
        id=user_id,
        username=user_id,
        role="user",
        scope=UserScope("user", user_id, tmp_path),
    )


def _knowledge_resource(
    tmp_path: Path,
    generation: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
) -> KnowledgeResource:
    return KnowledgeResource(
        id=f"user:kb:{generation}",
        name="alice-kb",
        base_dir=tmp_path,
        source="user",
        generation_id=generation,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("revoke", "generation", "owner", "provider"))
async def test_authorized_search_revalidates_identity_after_await(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    original = _knowledge_resource(tmp_path)
    replacement = (
        None
        if change == "revoke"
        else _knowledge_resource(
            tmp_path,
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            if change == "generation"
            else original.generation_id,
        )
    )
    resources = iter((original, replacement))
    users = iter(
        (
            _current_user(tmp_path),
            _current_user(tmp_path, "u_bob") if change == "owner" else _current_user(tmp_path),
        )
    )
    providers = iter(("llamaindex", "pageindex" if change == "provider" else "llamaindex"))

    class _Service:
        def __init__(self, **_kwargs) -> None:
            pass

        async def search_grounded(self, _query: str, _kb_name: str):
            return stamp_retrieval_view_signature(
                {
                    "provider": "llamaindex",
                    "sources": [
                        {
                            "chunk_id": "chunk",
                            "content": "grounded",
                            "file_path": "C:/private/book.pdf",
                        }
                    ],
                }
            )

    monkeypatch.setattr(knowledge_access_module, "resolve_for_rag", lambda _ref: next(resources))
    monkeypatch.setattr(knowledge_access_module, "get_current_user", lambda: next(users))
    monkeypatch.setattr(
        "deeptutor.services.rag.provider_binding.resolve_bound_provider",
        lambda *_args: next(providers),
    )
    monkeypatch.setattr("deeptutor.services.rag.service.RAGService", _Service)
    descriptor = AuthorizedKnowledgeSource(
        resource_id=original.id,
        generation_id=original.generation_id,
        name=original.name,
        source="user",
        resource_owner_id="u_alice",
        read_only=True,
        retrieval_provider="llamaindex",
    )

    with pytest.raises(HTTPException, match="changed|stale"):
        await descriptor.search("query")


@pytest.mark.asyncio
async def test_authorized_search_returns_bounded_path_free_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _knowledge_resource(tmp_path)

    class _Service:
        def __init__(self, **_kwargs) -> None:
            pass

        async def search_grounded(self, _query: str, _kb_name: str):
            return stamp_retrieval_view_signature(
                {
                    "provider": "llamaindex",
                    "sources": [
                        {
                            "chunk_id": "chunk",
                            "content": "grounded",
                            "file_path": "C:/private/book.pdf",
                        }
                    ],
                }
            )

    monkeypatch.setattr(knowledge_access_module, "resolve_for_rag", lambda _ref: resource)
    monkeypatch.setattr(knowledge_access_module, "get_current_user", lambda: _current_user(tmp_path))
    monkeypatch.setattr(
        "deeptutor.services.rag.provider_binding.resolve_bound_provider",
        lambda *_args: "llamaindex",
    )
    monkeypatch.setattr("deeptutor.services.rag.service.RAGService", _Service)
    descriptor = AuthorizedKnowledgeSource(
        resource_id=resource.id,
        generation_id=resource.generation_id,
        name=resource.name,
        source="user",
        resource_owner_id="u_alice",
        read_only=True,
        retrieval_provider="llamaindex",
    )

    result = await descriptor.search("query")
    encoded = str(result)

    assert result["retrieval_view_signature"]
    assert "file_path" not in encoded
    assert "C:/private" not in encoded
    assert len(result["sources"]) <= 20
