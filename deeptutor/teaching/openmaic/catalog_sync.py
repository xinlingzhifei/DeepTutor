"""Project an administrator's selected model into the shared OpenMAIC runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from deeptutor.services.provider_registry import find_by_name
from deeptutor.teaching.openmaic.provider_config import (
    parse_provider_configuration,
    unconfigured_provider_document,
)


class CatalogSyncError(ValueError):
    """Public, credential-free synchronization failure."""


async def shared_model_status(catalog_service: Any) -> dict[str, Any]:
    """Read saved selection and its published projection, without calling a model.

    A successful sync is reconstructed from the actual file and registration so
    it survives restarts and cannot report an older model as ready after a save.
    """
    from sqlalchemy import select

    from deeptutor.runtime.home import get_runtime_data_root
    from deeptutor.services.config import load_platform_settings
    from deeptutor.teaching.models import ProviderProfile
    from deeptutor.teaching.openmaic.provider_config import read_provider_configuration

    result: dict[str, Any] = {"state": "not_configured", "ready": False}
    try:
        if not load_platform_settings().enabled:
            return {"state": "not_enabled", "ready": False}
        document = provider_document_from_catalog(catalog_service.load())
        if document is None:
            return result
        expected = parse_provider_configuration(
            json.dumps(document).encode(),
            scope="shared",
            tenant_id=None,
            profile_id="platform-default",
        )
        result.update(provider=expected.provider_id, model=expected.model_name)
        root = get_runtime_data_root() / "system/secrets/openmaic-providers"
        published = read_provider_configuration(
            root,
            scope="shared",
            tenant_id=None,
            profile_id="platform-default",
        )
        if published != expected:
            raise CatalogSyncError("synchronization_required")
        async with _shared_update_session() as session:
            profile = await session.scalar(
                select(ProviderProfile).where(ProviderProfile.id == "platform-default")
            )
            if profile is None:
                raise CatalogSyncError("platform_not_initialized")
            if (
                profile.scope,
                profile.tenant_id,
                profile.owner_key,
                profile.secret_ref,
                profile.status,
                profile.provider_type,
                profile.model_name,
                profile.api_base_url,
            ) != (
                "shared",
                None,
                "shared",
                expected.secret_ref,
                "active",
                expected.provider_id,
                expected.model_name,
                expected.base_url,
            ):
                raise CatalogSyncError("synchronization_required")
        updated_at = (root / expected.secret_ref).stat().st_mtime
        return {
            **result,
            "state": "synced",
            "ready": True,
            "last_synced_at": datetime.fromtimestamp(updated_at, timezone.utc).isoformat(),
        }
    except CatalogSyncError as error:
        return {**result, "state": "error", "reason": str(error)}
    except Exception:
        return {**result, "state": "error", "reason": "synchronization_unavailable"}


def require_shared_course_model() -> None:
    from deeptutor.runtime.home import get_runtime_data_root
    from deeptutor.teaching.openmaic.data_planes import SharedModelNotConfigured
    from deeptutor.teaching.openmaic.provider_config import (
        ProviderConfigurationError,
        read_provider_configuration,
    )

    try:
        read_provider_configuration(
            get_runtime_data_root() / "system/secrets/openmaic-providers",
            scope="shared",
            tenant_id=None,
            profile_id="platform-default",
        )
    except ProviderConfigurationError:
        raise SharedModelNotConfigured() from None


@asynccontextmanager
async def _shared_update_session():
    from sqlalchemy.ext.asyncio import AsyncSession

    from deeptutor.teaching.database import get_platform_engine

    # Keep the same connection across commits while holding a session-level lock.
    async with get_platform_engine().connect() as connection:
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session


def provider_document_from_catalog(catalog: dict[str, Any]) -> dict[str, Any] | None:
    """Use only the explicitly selected admin profile, never personal fallback keys."""
    llm = catalog.get("services", {}).get("llm", {})
    profile = next(
        (p for p in llm.get("profiles", []) if p.get("id") == llm.get("active_profile_id")), None
    )
    if profile is None or not llm.get("active_model_id"):
        return None
    model = next(
        (m for m in profile.get("models", []) if m.get("id") == llm["active_model_id"]), None
    )
    if model is None or not model.get("model"):
        return None
    spec = find_by_name(profile.get("binding", "openai"))
    # Match the catalog's wire protocol, including custom and gateway endpoints.
    provider_id = {"openai_compat": "openai", "anthropic": "anthropic"}.get(
        spec.backend if spec else ""
    )
    if provider_id is None or spec.is_oauth:
        raise CatalogSyncError("unsupported_provider")
    # These options cannot be silently dropped when creating the course model.
    if profile.get("extra_headers") or profile.get("api_version"):
        raise CatalogSyncError("unsupported_provider_options")
    api_key = profile.get("api_key", "")
    if not api_key:
        if spec.is_local:
            # The compatible SDK requires a token even for unauthenticated local servers.
            api_key = "local-no-auth"
        else:
            return None
    base_url = profile.get("base_url") or (spec.default_api_base if spec else None)
    if not base_url:
        raise CatalogSyncError("provider_endpoint_required")
    document = {
        "scope": "shared",
        "tenantId": None,
        "profileId": "platform-default",
        "defaultModel": f"{provider_id}:{model['model']}",
        "providers": {provider_id: {"apiKey": api_key, "baseUrl": base_url.rstrip("/")}},
    }
    try:
        parse_provider_configuration(
            json.dumps(document).encode(),
            scope="shared",
            tenant_id=None,
            profile_id="platform-default",
        )
    except ValueError:
        raise CatalogSyncError("invalid_provider_configuration") from None
    return document


def publish_provider_document(path: Path, document: dict[str, Any] | None) -> None:
    """Replace one shared file atomically; a directory mount sees every revision."""
    from deeptutor.teaching.secret_permissions import restrict_secret_file

    path = Path(path)
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise CatalogSyncError("unsafe_provider_path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".provider-", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document or unconfigured_provider_document(), stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        restrict_secret_file(staged)
        if os.name == "posix" and os.geteuid() == 0:
            os.chown(staged, 1000, 1000)
            os.chown(path.parent, 1000, 1000)
        os.replace(staged, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if staged.exists():
            # Preserve failed staging material for diagnosis without exposing it.
            staged.rename(staged.with_suffix(".failed"))


async def synchronize_shared_catalog(catalog_service: Any) -> dict[str, str]:
    """Called only after the settings endpoint has authorized an administrator.

    Clear generation credentials before changing the registered model. Export and
    health routes remain active without an LLM; subsequent save/apply retries from
    the persisted catalog after failures.
    A PostgreSQL session lock serializes saves across API processes.
    """
    from sqlalchemy import select, text

    from deeptutor.runtime.home import get_runtime_data_root
    from deeptutor.services.config import load_platform_settings
    from deeptutor.teaching.models import ProviderProfile

    target = (
        get_runtime_data_root()
        / "system/secrets/openmaic-providers"
        / "shared/providers/platform-default"
    )
    try:
        if not load_platform_settings().enabled:
            return {"state": "not_enabled"}
        async with _shared_update_session() as session:
            await session.execute(text("SELECT pg_advisory_lock(1937006964, 1)"))
            await session.commit()
            try:
                async with session.begin():
                    profile = await session.scalar(
                        select(ProviderProfile)
                        .where(ProviderProfile.id == "platform-default")
                        .with_for_update()
                    )
                    if profile is None:
                        raise CatalogSyncError("platform_not_initialized")
                    if (
                        profile.scope,
                        profile.tenant_id,
                        profile.owner_key,
                        profile.secret_ref,
                    ) != ("shared", None, "shared", "shared/providers/platform-default"):
                        raise CatalogSyncError("provider_scope_mismatch")
                publish_provider_document(target, None)
                document = provider_document_from_catalog(catalog_service.load())
                if document is None:
                    return {"state": "not_configured"}
                config = parse_provider_configuration(
                    json.dumps(document).encode(),
                    scope="shared",
                    tenant_id=None,
                    profile_id="platform-default",
                )
                async with session.begin():
                    profile.provider_type = config.provider_id
                    profile.model_name = config.model_name
                    profile.api_base_url = config.base_url
                    profile.status = "active"
                publish_provider_document(target, document)
                return {"state": "synced"}
            finally:
                await session.execute(text("SELECT pg_advisory_unlock(1937006964, 1)"))
                await session.commit()
    except CatalogSyncError as error:
        return {"state": "error", "reason": str(error)}
    except Exception:
        return {"state": "error", "reason": "synchronization_failed"}
