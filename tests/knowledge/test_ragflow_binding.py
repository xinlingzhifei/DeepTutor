from __future__ import annotations

import json

import pytest

from deeptutor.knowledge.manager import KnowledgeBaseManager
from deeptutor.services.rag.factory import normalize_provider_name


def test_ragflow_provider_and_remote_upload_capabilities(tmp_path):
    from deeptutor.knowledge import kb_types

    assert normalize_provider_name("ragflow") == "ragflow"
    assert hasattr(KnowledgeBaseManager, "register_ragflow_kb")
    manager = KnowledgeBaseManager(base_dir=tmp_path / "kb")
    entry = manager.register_ragflow_kb(
        "course", "default", "dataset-1", credential_owner_id="alice"
    )
    assert kb_types.is_connected_kb(entry)
    assert not kb_types.supports_local_raw_files(entry)
    assert not kb_types.supports_document_upload(entry)
    assert "course" in manager.list_knowledge_bases()
    assert entry["generation_id"]
    assert not (tmp_path / "kb" / "course").exists()
    metadata = manager.get_metadata("course")
    assert metadata["type"] == "ragflow"
    assert "dataset-1" not in json.dumps(metadata)
    writable = manager.register_ragflow_kb(
        "editable",
        "default",
        "dataset-2",
        credential_owner_id="alice",
        ownership="managed",
        access_mode="manage_documents",
    )
    assert kb_types.supports_document_upload(writable)


def test_profile_secrets_are_separate_and_rotation_changes_identity(tmp_path):
    from deeptutor.knowledge.ragflow.profiles import ProfileStore, profile_fingerprint

    store = ProfileStore(root=tmp_path)
    public = store.save(
        "alice",
        "default",
        {
            "base_url": "https://ragflow.example",
            "api_key": "private-key-1",
        },
    )
    assert public["has_api_key"] is True
    assert "private-key-1" not in json.dumps(public)
    assert "private-key-1" not in (tmp_path / "profiles" / "alice" / "default.json").read_text()
    before = profile_fingerprint(store.load("alice", "default"))
    store.save("alice", "default", {"api_key": "private-key-2"})
    loaded = store.load("alice", "default")
    assert loaded["api_key"] == "private-key-2"
    assert profile_fingerprint(loaded) != before
    assert "private-key-2" not in repr(store.config("alice", "default"))
    with pytest.raises(ValueError):
        store.load("bob", "default")


@pytest.mark.parametrize("owner,profile", [("../alice", "default"), ("alice", "../key")])
def test_profile_paths_are_not_caller_controlled(tmp_path, owner, profile):
    from deeptutor.knowledge.ragflow.profiles import ProfileStore

    with pytest.raises(ValueError):
        ProfileStore(root=tmp_path).save(
            owner,
            profile,
            {
                "base_url": "https://ragflow.example",
                "api_key": "secret",
            },
        )


def test_profile_rejects_embedded_credentials_and_insecure_default(tmp_path):
    from deeptutor.knowledge.ragflow.profiles import ProfileStore

    store = ProfileStore(root=tmp_path)
    for url in (
        "http://ragflow.example",
        "https://user:secret@ragflow.example",
        "https://ragflow.example/?secret=x",
    ):
        with pytest.raises(ValueError):
            store.save("alice", "default", {"base_url": url, "api_key": "secret"})


def test_profile_requires_initial_credentials_with_a_validation_error(tmp_path):
    from deeptutor.knowledge.ragflow.profiles import ProfileStore

    with pytest.raises(ValueError):
        ProfileStore(root=tmp_path).save(
            "alice", "default", {"base_url": "https://ragflow.example"}
        )


def test_duplicate_remote_dataset_registration_is_rejected_atomically(ragflow_account):
    manager, _ = ragflow_account
    with pytest.raises(ValueError, match="already bound"):
        manager.register_ragflow_kb(
            "alias", "default", "dataset-1", credential_owner_id="local-admin"
        )
    assert manager.get_kb_entry("alias") is None


def test_activation_recovery_accepts_only_the_recorded_activation(ragflow_account):
    from fastapi import HTTPException

    from deeptutor.knowledge.ragflow.binding import (
        activate_managed_binding,
        reserve_managed_binding,
        restore_activation_binding,
    )

    _, profiles = ragflow_account
    reserved = reserve_managed_binding("new", "default")
    snapshot = reserved.recovery_snapshot()
    assert (
        restore_activation_binding(snapshot, "created-dataset").fingerprint == reserved.fingerprint
    )
    active = activate_managed_binding(reserved, "created-dataset")
    assert restore_activation_binding(snapshot, "created-dataset").fingerprint == active.fingerprint
    with pytest.raises(HTTPException):
        restore_activation_binding(snapshot, "foreign-dataset")
    profiles.save("local-admin", "default", {"api_key": "rotated"})
    with pytest.raises(HTTPException) as changed:
        restore_activation_binding(snapshot, "created-dataset")
    assert changed.value.status_code == 409


@pytest.fixture
def ragflow_account(tmp_path, monkeypatch):
    from deeptutor.knowledge.ragflow.profiles import ProfileStore
    from deeptutor.multi_user import knowledge_access, paths
    from deeptutor.multi_user.context import reset_current_user, set_current_user
    from deeptutor.multi_user.models import CurrentUser, UserScope

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
    store = ProfileStore()
    store.save(
        "local-admin", "default", {"base_url": "https://ragflow.example", "api_key": "secret"}
    )
    manager.register_ragflow_kb("course", "default", "dataset-1", credential_owner_id="local-admin")
    try:
        yield manager, store
    finally:
        reset_current_user(token)
        knowledge_access._manager_for.cache_clear()


def test_binding_rejects_credential_rotation_and_dataset_replacement(ragflow_account):
    from fastapi import HTTPException

    from deeptutor.knowledge.ragflow.binding import resolve_binding

    manager, profiles = ragflow_account
    bound = resolve_binding("course")
    assert bound.config.api_key == "secret"
    profiles.save("local-admin", "default", {"api_key": "rotated"})
    with pytest.raises(HTTPException) as error:
        bound.recheck()
    assert error.value.status_code == 409
    fresh = resolve_binding("course")
    manager.config["knowledge_bases"]["course"]["ragflow"]["dataset_id"] = "dataset-other"
    manager._save_config()
    with pytest.raises(HTTPException):
        fresh.recheck()


def test_read_only_binding_rejects_remote_write(ragflow_account):
    from fastapi import HTTPException

    from deeptutor.knowledge.ragflow.binding import resolve_binding

    with pytest.raises(HTTPException) as error:
        resolve_binding("course", require_write=True)
    assert error.value.status_code == 403


def test_managed_reservation_has_stable_generation_and_single_activation(ragflow_account):
    from fastapi import HTTPException

    from deeptutor.knowledge.ragflow.binding import (
        activate_managed_binding,
        reserve_managed_binding,
    )

    first = reserve_managed_binding("new-course", "default")
    second = reserve_managed_binding("new-course", "default")
    assert first.scope == second.scope
    active = activate_managed_binding(first, "new-dataset")
    assert active.binding["dataset_id"] == "new-dataset"
    with pytest.raises(HTTPException):
        activate_managed_binding(first, "another-dataset")


def test_authorized_descriptor_pins_remote_binding_before_search(ragflow_account):
    from deeptutor.multi_user.knowledge_access import resolve_authorized_source_descriptor

    _, profiles = ragflow_account
    before = resolve_authorized_source_descriptor("course")
    assert getattr(before, "retrieval_binding_fingerprint", "")
    profiles.save("local-admin", "default", {"api_key": "rotated-key"})
    after = resolve_authorized_source_descriptor("course")
    assert before.retrieval_binding_fingerprint != after.retrieval_binding_fingerprint
