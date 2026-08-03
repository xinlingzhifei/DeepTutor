"""Knowledge-base visibility and write guards for the multi-user layer."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
import uuid

from fastapi import HTTPException

from deeptutor.knowledge.manager import KnowledgeBaseManager
from deeptutor.knowledge.manifest import MANIFEST_NOTE_LIMIT, KbManifest, build_manifest

from .context import get_current_user
from .grants import load_grant
from .models import ADMIN_KNOWLEDGE_OWNER_ID, KnowledgeResource
from .paths import get_admin_path_service, get_current_path_service

ADMIN_PREFIX = "admin:kb:"
USER_PREFIX = "user:kb:"


@dataclass(frozen=True, slots=True)
class AuthorizedKnowledgeSource:
    """Generation-pinned identity exposing only read-only grounded search."""

    resource_id: str
    generation_id: str
    name: str
    source: Literal["admin", "user"]
    resource_owner_id: str
    read_only: bool
    retrieval_provider: str

    def __post_init__(self) -> None:
        if (
            self.source not in {"admin", "user"}
            or not _is_generation_id(self.generation_id)
            or self.resource_id != f"{self.source}:kb:{self.generation_id}"
        ):
            raise ValueError("knowledge source requires a stable generation identity")
        for value in (self.name, self.resource_owner_id, self.retrieval_provider):
            if not isinstance(value, str) or not value.strip() or any(
                character in value for character in "\x00\r\n"
            ):
                raise ValueError("knowledge source descriptor is invalid")
        if self.read_only is not True:
            raise ValueError("knowledge source descriptor is invalid")

    async def search(self, query: str) -> dict[str, Any]:
        """Search the pinned generation through the access-checked read seam."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("knowledge search query is empty")
        return await _search_authorized_source(self, query)


DEFAULT_KB_ALIASES = {"", "default", "current", "selected", "默认", "默认知识库", "当前知识库"}


@lru_cache(maxsize=128)
def _manager_for(base_dir: str) -> KnowledgeBaseManager:
    return KnowledgeBaseManager(base_dir=base_dir)


def current_kb_base_dir() -> Path:
    return get_current_path_service().get_knowledge_bases_root()


def admin_kb_base_dir() -> Path:
    return get_admin_path_service().get_knowledge_bases_root()


def current_kb_manager() -> KnowledgeBaseManager:
    return _manager_for(str(current_kb_base_dir().resolve()))


def admin_kb_manager() -> KnowledgeBaseManager:
    return _manager_for(str(admin_kb_base_dir().resolve()))


def _strip_resource_prefix(value: str) -> tuple[str | None, str]:
    raw = str(value or "").strip()
    if raw.startswith(ADMIN_PREFIX):
        return "admin", raw[len(ADMIN_PREFIX) :]
    if raw.startswith(USER_PREFIX):
        return "user", raw[len(USER_PREFIX) :]
    return None, raw


def _is_generation_id(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value.lower()


def _entry_generation(manager: KnowledgeBaseManager, name: str) -> str:
    entry = manager.get_kb_entry(name)
    generation = str((entry or {}).get("generation_id") or "")
    if not _is_generation_id(generation):
        raise HTTPException(status_code=404, detail=f"Knowledge base '{name}' not found")
    return generation


def _name_for_generation(manager: KnowledgeBaseManager, generation: str) -> str | None:
    for name in manager.list_knowledge_bases():
        if _entry_generation(manager, name) == generation:
            return name
    return None


def _resolve_reference(
    manager: KnowledgeBaseManager,
    value: str,
    *,
    prefixed: bool,
) -> tuple[str, str]:
    if prefixed and _is_generation_id(value):
        name = _name_for_generation(manager, value)
        if name is None:
            raise HTTPException(status_code=404, detail="Knowledge base identity is stale")
        return name, value
    name = _resolve_default_or_name(manager, value)
    return name, _entry_generation(manager, name)


def _resource(
    manager: KnowledgeBaseManager,
    *,
    name: str,
    generation_id: str,
    base_dir: Path,
    source: Literal["admin", "user"],
    assigned: bool,
    read_only: bool,
) -> KnowledgeResource:
    return KnowledgeResource(
        id=f"{source}:kb:{generation_id}",
        name=name,
        base_dir=base_dir,
        source=source,
        assigned=assigned,
        read_only=read_only,
        metadata=manager.get_metadata(name),
        generation_id=generation_id,
    )


def _assigned_admin_resources(manager: KnowledgeBaseManager) -> dict[str, str]:
    user = get_current_user()
    if user.is_admin:
        return {}
    by_generation = {
        _entry_generation(manager, name): name for name in manager.list_knowledge_bases()
    }
    out: dict[str, str] = {}
    for item in load_grant(user.id).get("knowledge_bases", []) or []:
        resource_id = str(item.get("resource_id") or item.get("id") or "")
        if not resource_id.startswith(ADMIN_PREFIX):
            continue
        generation = resource_id[len(ADMIN_PREFIX) :]
        name = by_generation.get(generation)
        if name is not None:
            out[name] = generation
    return out


def resolve_kb(kb_ref: str, *, require_write: bool = False) -> KnowledgeResource:
    user = get_current_user()
    requested_source, name = _strip_resource_prefix(kb_ref)

    if user.is_admin:
        if requested_source == "user":
            raise HTTPException(status_code=404, detail="Knowledge base not found")
        manager = admin_kb_manager()
        resolved, generation = _resolve_reference(
            manager,
            name,
            prefixed=requested_source is not None,
        )
        return _resource(
            manager,
            name=resolved,
            generation_id=generation,
            base_dir=admin_kb_base_dir(),
            source="admin",
            assigned=False,
            read_only=False,
        )

    user_manager = current_kb_manager()
    admin_manager = admin_kb_manager()
    assigned_resources = _assigned_admin_resources(admin_manager)

    if requested_source == "admin":
        resolved, generation = _resolve_reference(admin_manager, name, prefixed=True)
        if assigned_resources.get(resolved) != generation:
            raise HTTPException(status_code=403, detail="Knowledge base is not assigned to you")
        if require_write:
            raise HTTPException(
                status_code=403, detail="Assigned admin knowledge bases are read-only"
            )
        return _resource(
            admin_manager,
            name=resolved,
            generation_id=generation,
            base_dir=admin_kb_base_dir(),
            source="admin",
            assigned=True,
            read_only=True,
        )

    if requested_source == "user":
        resolved, generation = _resolve_reference(user_manager, name, prefixed=True)
        return _resource(
            user_manager,
            name=resolved,
            generation_id=generation,
            base_dir=current_kb_base_dir(),
            source="user",
            assigned=False,
            read_only=False,
        )

    if name.lower() in DEFAULT_KB_ALIASES:
        resolved, generation = _resolve_reference(user_manager, name, prefixed=False)
        return _resource(
            user_manager,
            name=resolved,
            generation_id=generation,
            base_dir=current_kb_base_dir(),
            source="user",
            assigned=False,
            read_only=False,
        )

    user_names = set(user_manager.list_knowledge_bases())
    if name in user_names:
        return _resource(
            user_manager,
            name=name,
            generation_id=_entry_generation(user_manager, name),
            base_dir=current_kb_base_dir(),
            source="user",
            assigned=False,
            read_only=False,
        )

    if name in assigned_resources:
        if require_write:
            raise HTTPException(
                status_code=403, detail="Assigned admin knowledge bases are read-only"
            )
        return _resource(
            admin_manager,
            name=name,
            generation_id=assigned_resources[name],
            base_dir=admin_kb_base_dir(),
            source="admin",
            assigned=True,
            read_only=True,
        )

    raise HTTPException(status_code=404, detail=f"Knowledge base '{name}' not found")


def _resolve_default_or_name(manager: KnowledgeBaseManager, name: str) -> str:
    requested = str(name or "").strip()
    names = manager.list_knowledge_bases()
    if requested and requested in names:
        return requested
    if requested.lower() in DEFAULT_KB_ALIASES:
        default_kb = manager.get_default()
        if default_kb and default_kb in names:
            return default_kb
        raise HTTPException(status_code=404, detail="No default knowledge base is configured")
    raise HTTPException(status_code=404, detail=f"Knowledge base '{requested}' not found")


def manager_for_resource(resource: KnowledgeResource) -> KnowledgeBaseManager:
    return _manager_for(str(resource.base_dir.resolve()))


def list_visible_knowledge_bases() -> list[dict[str, Any]]:
    user = get_current_user()
    manager = current_kb_manager()
    items: list[dict[str, Any]] = []
    for name in manager.list_knowledge_bases():
        generation = _entry_generation(manager, name)
        items.append(
            {
                "id": (
                    f"admin:kb:{generation}" if user.is_admin else f"user:kb:{generation}"
                ),
                "name": name,
                "generation_id": generation,
                "source": "admin" if user.is_admin else "user",
                "assigned": False,
                "read_only": False,
                "provenance_label": "Created by you" if not user.is_admin else "Admin workspace",
            }
        )

    if user.is_admin:
        return items

    admin_manager = admin_kb_manager()
    admin_by_generation = {
        _entry_generation(admin_manager, name): name
        for name in admin_manager.list_knowledge_bases()
    }
    existing_ids = {item["id"] for item in items}
    for item in load_grant(user.id).get("knowledge_bases", []) or []:
        name = str(item.get("name") or item.get("kb_name") or "").strip()
        resource_id = str(item.get("resource_id") or item.get("id") or "")
        if not resource_id.startswith(ADMIN_PREFIX):
            continue
        generation = resource_id[len(ADMIN_PREFIX) :]
        current_name = admin_by_generation.get(generation)
        if current_name is not None:
            name = current_name
        if not name or resource_id in existing_ids:
            continue
        items.append(
            {
                "id": resource_id,
                "name": name,
                "generation_id": generation,
                "source": "admin",
                "assigned": True,
                "read_only": True,
                "available": current_name is not None,
                "provenance_label": "Assigned by admin",
                "needs_admin_reindex": bool(item.get("needs_admin_reindex", False)),
                "embedding_signature": item.get("embedding_signature", ""),
            }
        )
    return items


def assert_writable(kb_ref: str) -> KnowledgeResource:
    return resolve_kb(kb_ref, require_write=True)


def resolve_for_rag(kb_ref: str | None) -> KnowledgeResource | None:
    if not kb_ref:
        return None
    resource = resolve_kb(kb_ref, require_write=False)
    if resource.assigned:
        from .audit import log_usage

        log_usage("knowledge_base", resource.id, "rag_query")
    return resource


def _has_stable_resource_identity(resource: KnowledgeResource) -> bool:
    generation = resource.generation_id
    return _is_generation_id(generation) and resource.id == (
        f"{resource.source}:kb:{generation}"
    )


def resolve_authorized_source(kb_ref: str) -> AuthorizedKnowledgeSource:
    """Resolve a visible KB to a generation-pinned, read-only descriptor.

    Human-readable aliases remain an input convenience only. Callers receive
    the stable generation identity and cannot serialize the internal KB root.
    """

    resource = resolve_for_rag(kb_ref)
    if resource is None or not _has_stable_resource_identity(resource):
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    user = get_current_user()
    owner_id = ADMIN_KNOWLEDGE_OWNER_ID if resource.source == "admin" else user.id
    base_dir = resource.base_dir.resolve()
    manager = manager_for_resource(resource)
    if manager.base_dir.resolve() != base_dir:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    entry = manager.get_kb_entry(resource.name)
    if entry is None or entry.get("generation_id") != resource.generation_id:
        raise HTTPException(status_code=404, detail="Knowledge base identity is stale")
    from deeptutor.services.rag.provider_binding import resolve_bound_provider

    provider = resolve_bound_provider(base_dir, resource.name)
    return AuthorizedKnowledgeSource(
        resource_id=resource.id,
        generation_id=resource.generation_id,
        name=resource.name,
        source=resource.source,
        resource_owner_id=owner_id,
        read_only=True,
        retrieval_provider=provider,
    )


async def _search_authorized_source(
    descriptor: AuthorizedKnowledgeSource,
    query: str,
) -> dict[str, Any]:
    """Resolve the physical service internally without putting it on the descriptor."""

    from deeptutor.services.rag.provider_binding import resolve_bound_provider
    from deeptutor.services.rag.service import RAGService

    resource = resolve_for_rag(descriptor.resource_id)
    if resource is None or not _has_stable_resource_identity(resource):
        raise HTTPException(status_code=404, detail="Knowledge base identity is stale")
    user = get_current_user()
    owner_id = ADMIN_KNOWLEDGE_OWNER_ID if resource.source == "admin" else user.id
    provider = resolve_bound_provider(resource.base_dir.resolve(), resource.name)
    if (
        resource.id != descriptor.resource_id
        or resource.generation_id != descriptor.generation_id
        or resource.name != descriptor.name
        or resource.source != descriptor.source
        or owner_id != descriptor.resource_owner_id
        or provider != descriptor.retrieval_provider
    ):
        raise HTTPException(status_code=409, detail="Knowledge base changed during retrieval")
    service = RAGService(kb_base_dir=str(resource.base_dir.resolve()))
    return await service.search_grounded(query, resource.name)


def resolve_kb_metadata(kb_ref: str | None) -> dict[str, Any] | None:
    """Access-checked KB metadata (``type`` / ``vault_path`` / …) for ``kb_ref``.

    Returns ``None`` when the reference is empty or not accessible to the
    current user. A pure read with no usage audit (unlike
    :func:`resolve_for_rag`) — safe to call while resolving capability bindings.
    """
    if not kb_ref:
        return None
    try:
        resource = resolve_kb(str(kb_ref), require_write=False)
    except HTTPException:
        return None
    manager = _manager_for(str(resource.base_dir.resolve()))
    return manager.get_metadata(resource.name)


def resolve_kb_manifest(
    kb_ref: str | None,
    *,
    limit: int = MANIFEST_NOTE_LIMIT,
    pattern: str = "",
) -> KbManifest | None:
    """Access-checked document inventory for ``kb_ref`` (``None`` if inaccessible).

    The one seam through which the chat manifest and the ``kb_files`` tool read
    a KB's document set, so neither can bypass the per-user visibility rules
    :func:`resolve_kb` enforces. A pure read with no usage audit, mirroring
    :func:`resolve_kb_metadata`; it touches the filesystem, so async callers
    should hand it to a worker thread.
    """
    if not kb_ref:
        return None
    try:
        resource = resolve_kb(str(kb_ref), require_write=False)
    except HTTPException:
        return None
    manager = _manager_for(str(resource.base_dir.resolve()))
    entry = manager.get_kb_entry(resource.name)
    if entry is None:
        return None
    return build_manifest(
        name=resource.name,
        kb_dir=resource.base_dir / resource.name,
        entry=entry,
        limit=limit,
        pattern=pattern,
    )
