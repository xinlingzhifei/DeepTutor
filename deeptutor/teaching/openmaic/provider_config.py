"""One scoped OpenMAIC provider configuration, shared with the upstream YAML reader."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit

from pydantic import SecretStr

from deeptutor.teaching.openmaic.data_planes import ProviderProfileRecord
from deeptutor.teaching.secret_permissions import secret_file_is_restricted

MAX_PROVIDER_CONFIGURATION_BYTES = 64 * 1024
PROVIDER_CONFIGURATION_DIRECTORY = "openmaic-providers"
PROVIDER_IDS = frozenset(
    (
        "openai",
        "azure",
        "anthropic",
        "google",
        "glm",
        "qwen",
        "deepseek",
        "kimi",
        "minimax",
        "siliconflow",
        "doubao",
        "openrouter",
        "grok",
        "tencent-hunyuan",
        "xiaomi",
        "ollama",
        "lemonade",
    )
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", re.ASCII)


class ProviderConfigurationError(ValueError):
    """A configuration or its registered binding is unusable; never include values."""

    def __init__(self) -> None:
        super().__init__("openmaic_provider_configuration_invalid")


class ProviderNotConfigured(ProviderConfigurationError):
    """The shared runtime is healthy but no course model has been selected."""


def unconfigured_provider_document() -> dict[str, object]:
    return {
        "scope": "shared",
        "tenantId": None,
        "profileId": "platform-default",
        "defaultModel": None,
        "providers": {},
    }


def provider_secret_ref(*, scope: str, tenant_id: str | None, profile_id: str) -> str:
    if not isinstance(profile_id, str) or _IDENTIFIER.fullmatch(profile_id) is None:
        raise ProviderConfigurationError()
    if scope == "shared" and tenant_id is None and profile_id == "platform-default":
        return f"shared/providers/{profile_id}"
    if (
        scope == "dedicated"
        and isinstance(tenant_id, str)
        and _IDENTIFIER.fullmatch(tenant_id) is not None
    ):
        return f"tenants/{tenant_id}/providers/{profile_id}"
    raise ProviderConfigurationError()


def valid_provider_model_name(value: object) -> bool:
    return isinstance(value, str) and _MODEL.fullmatch(value) is not None


def _valid_base_url(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or not value.startswith(("http://", "https://"))
        or value.endswith("/")
        or any(ord(character) <= 32 or ord(character) >= 127 for character in value)
        or any(character in value for character in "\\?#")
    ):
        return False
    try:
        parsed = urlsplit(value)
        if "%" in parsed.netloc:
            return False
        if parsed.netloc.startswith("["):
            ipaddress.IPv6Address(parsed.hostname)
        if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
            return False
        decoded_path = unquote(parsed.path, errors="strict")
        if any(
            ord(character) <= 32 or ord(character) == 127 or character == "\\"
            for character in decoded_path
        ):
            return False
        return bool(
            parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and not parsed.netloc.endswith(":")
            and not any(part in {".", ".."} for part in decoded_path.split("/"))
        )
    except (UnicodeError, ValueError):
        return False


def validate_provider_endpoint(value: str) -> None:
    if not _valid_base_url(value):
        raise ProviderConfigurationError()


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    scope: str
    tenant_id: str | None
    profile_id: str
    provider_id: str
    model_name: str
    base_url: str
    api_key: SecretStr = field(repr=False)

    @property
    def default_model(self) -> str:
        return f"{self.provider_id}:{self.model_name}"

    @property
    def secret_ref(self) -> str:
        return provider_secret_ref(
            scope=self.scope, tenant_id=self.tenant_id, profile_id=self.profile_id
        )

    def descriptor(self) -> dict[str, str | None]:
        return {
            "scope": self.scope,
            "tenantId": self.tenant_id,
            "profileId": self.profile_id,
            "providerId": self.provider_id,
            "modelName": self.model_name,
            "baseUrl": self.base_url,
            "secretRef": self.secret_ref,
        }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderConfigurationError()
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ProviderConfigurationError()


def parse_provider_configuration(
    body: bytes,
    *,
    scope: str,
    tenant_id: str | None,
    profile_id: str,
) -> ProviderConfiguration:
    provider_secret_ref(scope=scope, tenant_id=tenant_id, profile_id=profile_id)
    try:
        if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_PROVIDER_CONFIGURATION_BYTES:
            raise ProviderConfigurationError()
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        if scope == "shared" and tenant_id is None and profile_id == "platform-default":
            if document == unconfigured_provider_document():
                raise ProviderNotConfigured()
        if (
            not isinstance(document, dict)
            or set(document) != {"scope", "tenantId", "profileId", "defaultModel", "providers"}
            or document["scope"] != scope
            or document["tenantId"] != tenant_id
            or document["profileId"] != profile_id
            or not isinstance(document["defaultModel"], str)
        ):
            raise ProviderConfigurationError()
        provider_id, separator, model_name = document["defaultModel"].partition(":")
        if (
            not separator
            or provider_id not in PROVIDER_IDS
            or not valid_provider_model_name(model_name)
        ):
            raise ProviderConfigurationError()
        providers = document["providers"]
        if not isinstance(providers, dict) or set(providers) != {provider_id}:
            raise ProviderConfigurationError()
        entry = providers[provider_id]
        if not isinstance(entry, dict) or set(entry) != {"apiKey", "baseUrl"}:
            raise ProviderConfigurationError()
        api_key = entry["apiKey"]
        if (
            not isinstance(api_key, str)
            or api_key != api_key.strip()
            or api_key.startswith("\ufeff")
            or api_key.endswith("\ufeff")
            or any(ord(character) < 32 or ord(character) == 127 for character in api_key)
            or (not api_key and provider_id not in {"ollama", "lemonade"})
            or not _valid_base_url(entry["baseUrl"])
        ):
            raise ProviderConfigurationError()
        return ProviderConfiguration(
            scope=scope,
            tenant_id=tenant_id,
            profile_id=profile_id,
            provider_id=provider_id,
            model_name=model_name,
            base_url=entry["baseUrl"],
            api_key=SecretStr(api_key),
        )
    except ProviderNotConfigured:
        raise
    except (KeyError, TypeError, UnicodeError, ValueError, RecursionError):
        raise ProviderConfigurationError() from None


def provider_configuration_path(
    secrets_root: Path, *, scope: str, tenant_id: str | None, profile_id: str
) -> Path:
    root = Path(secrets_root)
    if not root.is_absolute():
        raise ProviderConfigurationError()
    return root.joinpath(
        *provider_secret_ref(scope=scope, tenant_id=tenant_id, profile_id=profile_id).split("/")
    )


def read_provider_configuration(
    secrets_root: Path, *, scope: str, tenant_id: str | None, profile_id: str
) -> ProviderConfiguration:
    path = provider_configuration_path(
        secrets_root, scope=scope, tenant_id=tenant_id, profile_id=profile_id
    )
    descriptor = None
    try:
        for current in (*reversed(path.parents), path):
            try:
                details = current.lstat()
            except FileNotFoundError:
                if scope == "shared" and tenant_id is None and profile_id == "platform-default":
                    raise ProviderNotConfigured() from None
                raise
            if stat.S_ISLNK(details.st_mode) or (
                getattr(details, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ProviderConfigurationError()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProviderConfigurationError()
        if os.name == "posix":
            if (
                before.st_uid != 1000
                or before.st_gid != 1000
                or stat.S_IMODE(before.st_mode) not in {0o400, 0o600}
            ):
                raise ProviderConfigurationError()
        elif not secret_file_is_restricted(path):
            raise ProviderConfigurationError()
        body = b""
        while len(body) <= MAX_PROVIDER_CONFIGURATION_BYTES:
            chunk = os.read(
                descriptor, min(65536, MAX_PROVIDER_CONFIGURATION_BYTES + 1 - len(body))
            )
            if not chunk:
                break
            body += chunk
        after = os.fstat(descriptor)

        def signature(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
                value.st_mode,
                value.st_uid,
                value.st_gid,
            )

        if signature(before) != signature(after) or signature(after) != signature(
            path.stat(follow_symlinks=False)
        ):
            raise ProviderConfigurationError()
        return parse_provider_configuration(
            body, scope=scope, tenant_id=tenant_id, profile_id=profile_id
        )
    except ProviderNotConfigured:
        raise
    except (OSError, ValueError):
        raise ProviderConfigurationError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def validate_registered_provider_configuration(
    configuration: ProviderConfiguration, profile: ProviderProfileRecord
) -> None:
    if (
        profile.profile_id != configuration.profile_id
        or profile.scope != configuration.scope
        or profile.tenant_id != configuration.tenant_id
        or profile.owner_key
        != ("shared" if configuration.scope == "shared" else configuration.tenant_id)
        or profile.status != "active"
        or profile.provider_type != configuration.provider_id
        or profile.model_name != configuration.model_name
        or profile.api_base_url != configuration.base_url
        or profile.secret_ref != configuration.secret_ref
    ):
        raise ProviderConfigurationError()
