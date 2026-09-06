"""Owner-private RAGFlow credentials and atomically published connection settings."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid

from deeptutor.knowledge.manager import _exclusive_config_lock
from deeptutor.services.file_io import atomic_write_json


def safe_component(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value):
        raise ValueError("Invalid RAGFlow identifier")
    return value


def profile_fingerprint(profile: dict) -> str:
    identity = {key: value for key, value in profile.items() if key != "api_key"}
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ProfileStore:
    """Connection metadata and versioned secrets never enter KB config or the sandbox."""

    def __init__(self, root: Path | None = None):
        from deeptutor.multi_user import paths

        self.root = Path(root) if root is not None else paths.SYSTEM_ROOT / "ragflow"
        self._custom_root = root is not None

    def _path(self, owner_id: str, profile_id: str) -> Path:
        return (
            self.root / "profiles" / safe_component(owner_id) / f"{safe_component(profile_id)}.json"
        )

    def _secret_path(self, owner_id: str, secret_ref: str) -> Path:
        from deeptutor.multi_user.paths import owner_secrets_dir

        safe_component(owner_id)
        safe_component(secret_ref)
        root = (
            self.root / "secrets" / owner_id
            if self._custom_root
            else owner_secrets_dir(owner_id) / "ragflow"
        )
        return root / f"{secret_ref}.json"

    def load(self, owner_id: str, profile_id: str) -> dict[str, Any]:
        try:
            profile = json.loads(self._path(owner_id, profile_id).read_text(encoding="utf-8"))
            if (
                not isinstance(profile, dict)
                or profile.get("credential_owner_id") != owner_id
                or profile.get("profile_id") != profile_id
            ):
                raise ValueError("Invalid RAGFlow profile")
            secret_path = self._secret_path(owner_id, profile["secret_ref"])
            secret = json.loads(secret_path.read_text(encoding="utf-8"))
            if not isinstance(secret.get("api_key"), str) or not secret["api_key"]:
                raise ValueError("Invalid RAGFlow profile")
            return {**profile, "api_key": secret["api_key"]}
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("RAGFlow profile is unavailable") from None

    def config(self, owner_id: str, profile_id: str):
        from deeptutor.services.rag.pipelines.ragflow.config import RagflowConfig

        profile = self.load(owner_id, profile_id)
        return RagflowConfig(
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

    def public(self, owner_id: str, profile_id: str) -> dict:
        profile = self.load(owner_id, profile_id)
        return {
            key: profile[key]
            for key in (
                "profile_id",
                "base_url",
                "connect_timeout",
                "read_timeout",
                "upload_timeout",
                "allow_http",
                "credential_revision",
                "api_contract",
            )
        } | {"has_api_key": True}

    def list(self, owner_id: str) -> list[dict]:
        folder = self.root / "profiles" / safe_component(owner_id)
        if not folder.is_dir():
            return []
        return [self.public(owner_id, path.stem) for path in sorted(folder.glob("*.json"))]

    def save(self, owner_id: str, profile_id: str, payload: dict) -> dict:
        from deeptutor.services.rag.pipelines.ragflow.config import RagflowConfig

        path = self._path(owner_id, profile_id)
        allowed = {
            "base_url",
            "api_key",
            "connect_timeout",
            "read_timeout",
            "upload_timeout",
            "allow_http",
        }
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("Unsupported RAGFlow profile fields")
        with _exclusive_config_lock(path.with_suffix(".lock")):
            previous = self.load(owner_id, profile_id) if path.exists() else {}
            values = {
                "connect_timeout": 5,
                "read_timeout": 60,
                "upload_timeout": 300,
                "allow_http": False,
            }
            values.update({key: value for key, value in previous.items() if key in allowed})
            values.update(payload)
            if not values.get("base_url") or not values.get("api_key"):
                raise ValueError("RAGFlow connection requires a base URL and API key")
            config = RagflowConfig(**values)
            revision = int(previous.get("credential_revision", 0)) + 1
            secret_ref = f"{profile_id}-{uuid.uuid4().hex}"
            secret_path = self._secret_path(owner_id, secret_ref)
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(secret_path.parent, 0o700)
            atomic_write_json(secret_path, {"api_key": config.api_key})
            os.chmod(secret_path, 0o600)
            metadata = {key: getattr(config, key) for key in allowed if key != "api_key"}
            metadata.update(
                {
                    "schema_version": 1,
                    "profile_id": profile_id,
                    "credential_owner_id": owner_id,
                    "credential_revision": revision,
                    "secret_ref": secret_ref,
                    "api_contract": "ragflow-v0.27.1",
                }
            )
            atomic_write_json(path, metadata)
            return self.public(owner_id, profile_id)


def get_profile_config(owner_id: str, profile_id: str):
    return ProfileStore().config(owner_id, profile_id)
