"""RAGFlow authorization regressions using real isolated user and KB stores."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

from fastapi import HTTPException
import pytest

from deeptutor.knowledge.ragflow.binding import resolve_binding, restore_binding
from deeptutor.knowledge.ragflow.profiles import ProfileStore
from deeptutor.multi_user import grants, identity, knowledge_access, paths
from deeptutor.multi_user.models import ADMIN_KNOWLEDGE_OWNER_ID, LOCAL_ADMIN_ID
from deeptutor.services.file_io import atomic_write_json


@pytest.fixture
def accounts(mu_isolated_root: Path, monkeypatch: pytest.MonkeyPatch):
    from deeptutor.services import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    knowledge_access._manager_for.cache_clear()
    users = {
        name: identity.create_user(name, "unused-test-password-hash", role=role)
        for name, role in (("admin", "admin"), ("alice", "user"), ("bob", "user"))
    }
    assert paths.SYSTEM_ROOT == mu_isolated_root / "data" / "system"
    yield users
    knowledge_access._manager_for.cache_clear()


def _profile(owner_id: str, *, profile_id: str = "default", key: str = "fixture-secret") -> dict:
    return ProfileStore().save(
        owner_id, profile_id, {"base_url": "https://ragflow.example", "api_key": key}
    )


def _register(
    *,
    name: str = "course",
    credential_owner: str | None = None,
    access_mode: str = "manage_documents",
    dataset_id: str = "dataset-1",
) -> dict:
    return knowledge_access.current_kb_manager().register_ragflow_kb(
        name,
        "default",
        dataset_id,
        credential_owner_id=credential_owner or paths.current_owner_id(),
        ownership="attached",
        access_mode=access_mode,
    )


def test_profiles_live_under_system_root_and_never_expose_credentials(
    accounts,
    as_user,
    mu_isolated_root: Path,
) -> None:
    alice = accounts["alice"]["id"]
    bob = accounts["bob"]["id"]
    _profile(alice, key="alice-secret")
    _profile(bob, key="bob-secret")
    _profile(alice, profile_id="alice-only", key="alice-only-secret")
    with as_user(bob):
        public = ProfileStore().list(paths.current_owner_id())
        assert [item["profile_id"] for item in public] == ["default"]
        assert public[0]["has_api_key"] is True
        assert "bob-secret" not in json.dumps(public)
        assert "api_key" not in public[0]
        assert "secret_ref" not in public[0]
        with pytest.raises(ValueError):
            ProfileStore().load(paths.current_owner_id(), "alice-only")
    metadata = paths.SYSTEM_ROOT / "ragflow" / "profiles" / alice / "default.json"
    assert metadata.is_file()
    assert "alice-secret" not in metadata.read_text(encoding="utf-8")
    assert list((paths.SYSTEM_ROOT / "user-secrets" / alice / "ragflow").glob("*.json"))
    assert not list((mu_isolated_root / "data" / "users").rglob("*secret*"))


def test_user_cannot_resolve_other_users_kb_or_borrow_their_profile(accounts, as_user) -> None:
    alice, bob = accounts["alice"]["id"], accounts["bob"]["id"]
    _profile(alice, key="alice-secret")
    with as_user(alice):
        _register()
        alice_kb = resolve_binding("course")
    with as_user(bob):
        with pytest.raises(HTTPException) as stale:
            resolve_binding(alice_kb.resource.id)
        assert stale.value.status_code == 404
        _register(credential_owner=alice)
        with pytest.raises(HTTPException) as borrowed:
            resolve_binding("course")
        assert borrowed.value.status_code == 403


def test_identical_kb_names_and_remote_ids_never_share_operations_or_documents(
    accounts, as_user
) -> None:
    bound_by_user = {}
    for name in ("alice", "bob"):
        user_id = accounts[name]["id"]
        _profile(user_id, key=f"{name}-secret")
        with as_user(user_id):
            _register()
            bound = resolve_binding("course", require_write=True)
            operation = bound.store.create_operation(
                bound.scope,
                action="parse",
                idempotency_key="same-request",
                payload={"document_id": "same-remote-document"},
                actor_id=user_id,
            )
            document = bound.store.register_document(
                bound.scope, "same-remote-document", visibility="active", title="course.pdf"
            )
            bound_by_user[name] = (bound, operation, document)
    alice, alice_op, alice_doc = bound_by_user["alice"]
    bob, bob_op, bob_doc = bound_by_user["bob"]
    assert alice.scope != bob.scope
    assert alice_op["operation_id"] != bob_op["operation_id"]
    assert alice_doc["document_ref"] != bob_doc["document_ref"]
    assert bob.store.get_operation(bob.scope, alice_op["operation_id"]) is None
    assert bob.store.get_operation_for_generation(bob.scope, alice_op["operation_id"]) is None
    assert bob.store.events(bob.scope, alice_op["operation_id"]) == []
    assert bob.store.claim(bob.scope, alice_op["operation_id"], "bob-worker") is None
    with pytest.raises(ValueError):
        bob.store.resolve_document(bob.scope, alice_doc["document_ref"])


def test_shared_admin_kb_uses_admin_credentials_and_one_document_scope(accounts, as_user) -> None:
    _profile(LOCAL_ADMIN_ID, key="admin-only-secret")
    with as_user(accounts["admin"]["id"], role="admin"):
        _register()
        admin_bound = resolve_binding("course", require_write=True)
        doc = admin_bound.store.register_document(
            admin_bound.scope, "shared-document", visibility="active"
        )
    for name in ("alice", "bob"):
        user_id = accounts[name]["id"]
        _profile(user_id, key=f"{name}-private-secret")
        grants.save_grant(user_id, {"knowledge_bases": [{"resource_id": admin_bound.resource.id}]})
        with as_user(user_id):
            reader = resolve_binding(admin_bound.resource.id)
            assert reader.config.api_key == "admin-only-secret"
            assert reader.binding["credential_owner_id"] == LOCAL_ADMIN_ID
            assert reader.scope.owner_id == ADMIN_KNOWLEDGE_OWNER_ID
            assert reader.scope == admin_bound.scope
            assert reader.resource.read_only is True
            assert (
                reader.store.resolve_document(reader.scope, doc["document_ref"])
                == "shared-document"
            )
            with pytest.raises(HTTPException) as denied:
                resolve_binding(admin_bound.resource.id, require_write=True)
            assert denied.value.status_code == 403


def test_shared_admin_binding_cannot_borrow_an_ordinary_users_credential_owner(
    accounts, as_user
) -> None:
    alice = accounts["alice"]["id"]
    _profile(alice, key="alice-private-secret")
    with as_user(accounts["admin"]["id"], role="admin"):
        _register(credential_owner=alice)
        with pytest.raises(HTTPException) as denied:
            resolve_binding("course")
        assert denied.value.status_code == 403


def test_attached_read_only_binding_rejects_remote_write_even_for_its_owner(
    accounts, as_user
) -> None:
    alice = accounts["alice"]["id"]
    _profile(alice)
    with as_user(alice):
        _register(access_mode="read_only")
        assert resolve_binding("course").binding["access_mode"] == "read_only"
        with pytest.raises(HTTPException) as denied:
            resolve_binding("course", require_write=True)
        assert denied.value.status_code == 403


@pytest.mark.parametrize("change", ["generation", "dataset", "profile", "read_only"])
def test_recheck_rejects_stale_generation_binding_profile_or_write_mode(
    accounts, as_user, change: str
) -> None:
    alice = accounts["alice"]["id"]
    _profile(alice)
    with as_user(alice):
        _register()
        old = resolve_binding("course", require_write=True)
        if change == "profile":
            _profile(alice, key="rotated-secret")
        else:

            def mutate(config):
                entry = config["knowledge_bases"]["course"]
                if change == "generation":
                    entry["generation_id"] = str(uuid.uuid4())
                elif change == "dataset":
                    entry["ragflow"]["dataset_id"] = "new-dataset"
                    entry["ragflow"]["binding_version"] += 1
                else:
                    entry["ragflow"]["access_mode"] = "read_only"

            old.manager._mutate_config(mutate)
        with pytest.raises(HTTPException) as stale:
            old.recheck()
        assert stale.value.status_code in {403, 404, 409}


def test_grant_revocation_invalidates_an_already_resolved_shared_binding(accounts, as_user) -> None:
    _profile(LOCAL_ADMIN_ID)
    with as_user(accounts["admin"]["id"], role="admin"):
        _register()
        shared = resolve_binding("course")
    alice = accounts["alice"]["id"]
    grants.save_grant(alice, {"knowledge_bases": [{"resource_id": shared.resource.id}]})
    with as_user(alice):
        old = resolve_binding(shared.resource.id)
    grants.save_grant(alice, {"knowledge_bases": []})
    with pytest.raises(HTTPException) as revoked:
        old.recheck()
    assert revoked.value.status_code == 403


def test_credential_rotation_invalidates_inflight_binding_without_losing_active_documents(
    accounts, as_user
) -> None:
    alice = accounts["alice"]["id"]
    _profile(alice, key="old-secret")
    with as_user(alice):
        _register()
        old = resolve_binding("course", require_write=True)
        document = old.store.register_document(old.scope, "document-1", visibility="active")
        revision, active_ids = old.store.visibility_snapshot(old.scope)
        snapshot = old.recovery_snapshot()
        _profile(alice, key="new-secret")
        current = resolve_binding("course", require_write=True)
        assert current.config.api_key == "new-secret"
        assert current.fingerprint != old.fingerprint
        assert current.scope == old.scope
        assert current.store.visibility_snapshot(current.scope) == (revision, active_ids)
        assert (
            current.store.resolve_document(current.scope, document["document_ref"]) == "document-1"
        )
        with pytest.raises(HTTPException) as stale:
            old.recheck()
        assert stale.value.status_code == 409
        with pytest.raises(HTTPException):
            restore_binding(snapshot)


@pytest.mark.parametrize("actor_id", [None, "", "nonexistent-user"])
def test_recovery_never_uses_ambient_admin_when_actor_is_missing(
    accounts, as_user, actor_id
) -> None:
    _profile(LOCAL_ADMIN_ID)
    with as_user(accounts["admin"]["id"], role="admin"):
        _register()
        snapshot = resolve_binding("course", require_write=True).recovery_snapshot()
        snapshot["actor_id"] = actor_id
        with pytest.raises(HTTPException) as denied:
            restore_binding(snapshot)
        assert denied.value.status_code == 403


def test_recovery_rejects_disabled_actor(accounts, as_user) -> None:
    alice = accounts["alice"]["id"]
    _profile(alice)
    with as_user(alice):
        _register()
        snapshot = resolve_binding("course", require_write=True).recovery_snapshot()
    users = identity.load_users()
    users["alice"]["disabled"] = True
    atomic_write_json(identity.USERS_FILE, users)
    with as_user(accounts["admin"]["id"], role="admin"):
        with pytest.raises(HTTPException) as denied:
            restore_binding(snapshot)
    assert denied.value.status_code == 403


def test_recovery_rejects_changed_actor_role(accounts, as_user) -> None:
    _profile(LOCAL_ADMIN_ID)
    with as_user(accounts["admin"]["id"], role="admin"):
        _register()
        snapshot = resolve_binding("course", require_write=True).recovery_snapshot()
    assert identity.set_role("admin", "user")
    with pytest.raises(HTTPException) as denied:
        restore_binding(snapshot)
    assert denied.value.status_code == 403


@pytest.mark.parametrize("revocation", ["demoted", "disabled"])
def test_inflight_admin_binding_recheck_rejects_revoked_actor(
    accounts, as_user, revocation: str
) -> None:
    _profile(LOCAL_ADMIN_ID)
    with as_user(accounts["admin"]["id"], role="admin"):
        _register()
        old = resolve_binding("course", require_write=True)
    if revocation == "demoted":
        assert identity.set_role("admin", "user")
    else:
        users = identity.load_users()
        users["admin"]["disabled"] = True
        atomic_write_json(identity.USERS_FILE, users)
    with pytest.raises(HTTPException) as denied:
        old.recheck()
    assert denied.value.status_code == 403


@pytest.mark.parametrize("actor_role", [None, "user"])
def test_auth_disabled_recovery_requires_explicit_admin_role(
    accounts,
    as_user,
    monkeypatch: pytest.MonkeyPatch,
    actor_role,
) -> None:
    from deeptutor.services import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    _profile(LOCAL_ADMIN_ID)
    with as_user(LOCAL_ADMIN_ID, role="admin"):
        _register()
        snapshot = resolve_binding("course", require_write=True).recovery_snapshot()
    if actor_role is None:
        snapshot.pop("actor_role")
    else:
        snapshot["actor_role"] = actor_role
    with pytest.raises(HTTPException) as denied:
        restore_binding(snapshot)
    assert denied.value.status_code == 403


def test_recovery_rebuilds_the_original_user_not_the_ambient_administrator(
    accounts, as_user
) -> None:
    alice = accounts["alice"]["id"]
    _profile(alice, key="alice-secret")
    with as_user(alice):
        _register()
        original = resolve_binding("course", require_write=True)
        snapshot = original.recovery_snapshot()
    with as_user(accounts["admin"]["id"], role="admin"):
        restored = restore_binding(snapshot)
    assert restored.actor.id == alice
    assert restored.actor.role == "user"
    assert restored.scope == original.scope
    assert restored.config.api_key == "alice-secret"
