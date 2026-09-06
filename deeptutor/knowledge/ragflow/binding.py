"""Resolve RAGFlow resources through existing ACLs and pin their remote identity."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from fastapi import HTTPException

from deeptutor.knowledge.manager import KnowledgeBaseManager
from deeptutor.multi_user import knowledge_access
from deeptutor.multi_user.context import get_current_user
from deeptutor.multi_user.models import (
    ADMIN_KNOWLEDGE_OWNER_ID,
    LOCAL_ADMIN_ID,
    CurrentUser,
    KnowledgeResource,
)
from deeptutor.multi_user.paths import current_owner_id, user_context
from deeptutor.services.rag.pipelines.ragflow.config import RagflowConfig

from .operations import OperationScope, OperationStore
from .profiles import ProfileStore, profile_fingerprint, safe_component


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class BoundRagflow:
    resource: KnowledgeResource
    entry: dict
    binding: dict
    config: object = field(repr=False)
    scope: OperationScope
    store: OperationStore = field(repr=False)
    manager: KnowledgeBaseManager = field(repr=False)
    fingerprint: str
    profile_digest: str
    actor: CurrentUser = field(repr=False)
    require_write: bool = False

    def recheck(self) -> None:
        actor = _restore_actor(self.recovery_snapshot())
        with user_context(actor):
            fresh = resolve_binding(self.resource.id, require_write=self.require_write)
        if fresh.fingerprint != self.fingerprint or fresh.scope != self.scope:
            raise HTTPException(409, "RAGFlow binding changed during the operation")

    def recovery_snapshot(self) -> dict:
        return {
            "actor_id": self.actor.id,
            "actor_role": self.actor.role,
            "kb_ref": self.resource.id,
            "generation_id": self.resource.generation_id,
            "binding_fingerprint": self.fingerprint,
            "require_write": self.require_write,
            "resource_owner_id": self.scope.owner_id,
            "workspace_scope": self.scope.workspace_scope,
        }


def resolve_binding(kb_ref: str, *, require_write: bool = False) -> BoundRagflow:
    resource = knowledge_access.resolve_kb(kb_ref, require_write=require_write)
    manager = knowledge_access.manager_for_resource(resource)
    entry = manager.get_kb_entry(resource.name)
    if not entry or entry.get("type") != "ragflow" or entry.get("rag_provider") != "ragflow":
        raise HTTPException(400, "Knowledge base is not a RAGFlow binding")
    binding = entry.get("ragflow")
    if not isinstance(binding, dict) or binding.get("lifecycle_state") not in {
        "active",
        "provisioning",
    }:
        raise HTTPException(409, "RAGFlow binding is unavailable")
    if require_write and binding.get("access_mode") != "manage_documents":
        raise HTTPException(403, "RAGFlow binding is read-only")
    if require_write and binding.get("dataset_id"):
        for name, other in manager._load_config().get("knowledge_bases", {}).items():
            if (
                name != resource.name
                and other.get("type") == "ragflow"
                and other.get("ragflow", {}).get("dataset_id") == binding["dataset_id"]
            ):
                raise HTTPException(409, "RAGFlow Dataset has multiple workspace bindings")
    actor = get_current_user()
    credential_owner = binding.get("credential_owner_id", "")
    if resource.source == "admin" and credential_owner != LOCAL_ADMIN_ID:
        raise HTTPException(403, "RAGFlow administrator credentials have an invalid owner")
    if resource.source != "admin" and credential_owner != current_owner_id():
        raise HTTPException(403, "RAGFlow credentials do not belong to this resource")
    try:
        profiles = ProfileStore()
        profile = profiles.load(credential_owner, binding["profile_id"])
        config = RagflowConfig(
            **{
                key: profile[key]
                for key in (
                    "base_url",
                    "api_key",
                    "connect_timeout",
                    "read_timeout",
                    "upload_timeout",
                    "allow_http",
                )
            }
        )
    except (ValueError, KeyError):
        raise HTTPException(503, "RAGFlow connection is not configured") from None
    owner_id = ADMIN_KNOWLEDGE_OWNER_ID if resource.source == "admin" else actor.id
    profile_digest = profile_fingerprint(profile)
    scope, fingerprint = _binding_identity(
        resource, binding, config.base_url, profile_digest, owner_id
    )
    return BoundRagflow(
        resource=resource,
        entry=entry,
        binding=binding,
        config=config,
        scope=scope,
        store=OperationStore(),
        manager=manager,
        fingerprint=fingerprint,
        profile_digest=profile_digest,
        actor=actor,
        require_write=require_write,
    )


def _binding_identity(resource, binding, base_url, profile_digest, owner_id):
    data_identity = {
        "owner": owner_id,
        "generation": resource.generation_id,
        "binding": {
            key: binding.get(key)
            for key in (
                "profile_id",
                "credential_owner_id",
                "dataset_id",
                "binding_version",
            )
        },
        "base_url": base_url,
    }
    scope = OperationScope(
        owner_id=owner_id,
        workspace_scope=_digest(str(resource.base_dir.resolve())),
        generation_id=resource.generation_id,
        binding_fingerprint=_digest(data_identity),
    )
    return scope, _digest({"data": data_identity, "binding": binding, "profile": profile_digest})


def resolve_pipeline_binding(kb_name: str, kb_base_dir: str | None) -> BoundRagflow:
    if kb_base_dir is None:
        return resolve_binding(kb_name)
    base = Path(kb_base_dir).resolve()
    entry = KnowledgeBaseManager(base_dir=base).get_kb_entry(kb_name)
    if not entry:
        raise HTTPException(404, "Knowledge base not found")
    if base == knowledge_access.admin_kb_base_dir().resolve():
        prefix = "admin"
    elif base == knowledge_access.current_kb_base_dir().resolve():
        prefix = "user"
    else:
        raise HTTPException(403, "Knowledge base is outside the authorized workspace")
    bound = resolve_binding(f"{prefix}:kb:{entry.get('generation_id', '')}")
    if bound.resource.base_dir.resolve() != base:
        raise HTTPException(409, "RAGFlow workspace changed")
    return bound


def reserve_managed_binding(name: str, profile_id: str, *, description: str = "") -> BoundRagflow:
    owner = current_owner_id()
    ProfileStore().load(owner, profile_id)
    manager = knowledge_access.current_kb_manager()
    if get_current_user().is_admin:
        manager = knowledge_access.admin_kb_manager()
    existing = manager.get_kb_entry(name)
    if existing:
        binding = existing.get("ragflow", {})
        if (
            existing.get("type") != "ragflow"
            or binding.get("ownership") != "managed"
            or binding.get("profile_id") != profile_id
            or binding.get("credential_owner_id") != owner
        ):
            raise HTTPException(409, "Knowledge base name is already in use")
    else:
        try:
            manager.register_ragflow_kb(
                name,
                profile_id,
                "",
                credential_owner_id=owner,
                ownership="managed",
                access_mode="manage_documents",
                description=description,
            )
        except ValueError:
            raise HTTPException(409, "Knowledge base could not be reserved") from None
    return resolve_binding(name, require_write=True)


def activate_managed_binding(bound: BoundRagflow, dataset_id: str) -> BoundRagflow:
    safe_component(dataset_id)
    bound.recheck()
    if bound.binding.get("ownership") != "managed" or bound.binding.get("dataset_id"):
        raise HTTPException(409, "RAGFlow binding is already active")

    def activate(config: dict):
        entry = config.get("knowledge_bases", {}).get(bound.resource.name)
        if (
            not entry
            or entry.get("generation_id") != bound.resource.generation_id
            or entry.get("ragflow") != bound.binding
        ):
            raise HTTPException(409, "RAGFlow reservation changed")
        entry["ragflow"] = {**bound.binding, "dataset_id": dataset_id, "lifecycle_state": "active"}
        entry["status"] = "ready"

    bound.manager._mutate_config(activate)
    with user_context(bound.actor):
        return resolve_binding(bound.resource.id, require_write=bound.require_write)


def _restore_actor(snapshot: dict) -> CurrentUser:
    from deeptutor.multi_user.identity import get_user_by_id
    from deeptutor.multi_user.models import LOCAL_ADMIN_ID
    from deeptutor.multi_user.paths import local_admin_user, scope_for_user
    from deeptutor.services import auth

    actor_id = snapshot.get("actor_id")
    if actor_id == LOCAL_ADMIN_ID and not auth.AUTH_ENABLED:
        if snapshot.get("actor_role") != "admin":
            raise HTTPException(403, "RAGFlow local operation actor is invalid")
        actor = local_admin_user()
    else:
        record = get_user_by_id(actor_id) if isinstance(actor_id, str) else None
        if not record or record[1].get("disabled"):
            raise HTTPException(403, "RAGFlow operation actor is unavailable")
        username, data = record
        if data.get("role") != snapshot.get("actor_role"):
            raise HTTPException(403, "RAGFlow operation actor permissions changed")
        actor = CurrentUser(
            actor_id,
            username,
            data["role"],
            scope_for_user(actor_id, is_admin=data["role"] == "admin"),
        )
    return actor


def restore_binding(snapshot: dict) -> BoundRagflow:
    """Recover an explicitly recorded actor; never fall back to the current admin."""
    actor = _restore_actor(snapshot)
    with user_context(actor):
        bound = resolve_binding(
            snapshot.get("kb_ref", ""), require_write=snapshot.get("require_write") is True
        )
    if (
        bound.fingerprint != snapshot.get("binding_fingerprint")
        or bound.scope.generation_id != snapshot.get("generation_id")
        or bound.scope.owner_id != snapshot.get("resource_owner_id")
        or bound.scope.workspace_scope != snapshot.get("workspace_scope")
    ):
        raise HTTPException(409, "RAGFlow operation binding is stale")
    return bound


def restore_activation_binding(snapshot: dict, dataset_id: str) -> BoundRagflow:
    """Accept only the original reservation or its exact one-time activation."""
    safe_component(dataset_id)
    actor = _restore_actor(snapshot)
    with user_context(actor):
        bound = resolve_binding(snapshot.get("kb_ref", ""), require_write=True)
    if not bound.binding.get("dataset_id"):
        return restore_binding(snapshot)
    if (
        bound.binding.get("ownership") != "managed"
        or bound.binding.get("dataset_id") != dataset_id
        or bound.binding.get("lifecycle_state") != "active"
    ):
        raise HTTPException(409, "RAGFlow activation target changed")
    original_binding = {**bound.binding, "dataset_id": "", "lifecycle_state": "provisioning"}
    scope, fingerprint = _binding_identity(
        bound.resource,
        original_binding,
        bound.config.base_url,
        bound.profile_digest,
        bound.scope.owner_id,
    )
    if (
        fingerprint != snapshot.get("binding_fingerprint")
        or scope.generation_id != snapshot.get("generation_id")
        or scope.owner_id != snapshot.get("resource_owner_id")
        or scope.workspace_scope != snapshot.get("workspace_scope")
    ):
        raise HTTPException(409, "RAGFlow activation binding is stale")
    return bound
