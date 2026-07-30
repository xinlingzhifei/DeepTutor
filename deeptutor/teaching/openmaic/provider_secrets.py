"""Route-bound Provider secret-file resolution."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Literal, Protocol

from pydantic import SecretStr

from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneSelection,
    ProviderProfileRecord,
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class ProviderSecretError(ValueError):
    """A Provider secret reference or file is unsafe."""


class ProviderSecretAccessDenied(ProviderSecretError):
    """The current data-plane runtime cannot access this Provider secret."""


class ProviderSecretUnavailable(ProviderSecretError):
    """The authorized Provider secret file is unavailable."""


class ProviderSecretBindingRepository(Protocol):
    """Re-read the active route/profile binding before secret access."""

    async def resolve_bound_profile(
        self,
        selection: DataPlaneSelection,
    ) -> ProviderProfileRecord | None: ...


class ProviderSecretResolver:
    """Resolve only secrets authorized for one runtime pool boundary."""

    def __init__(
        self,
        secrets_root: Path,
        *,
        runtime_mode: Literal["shared", "dedicated"],
        runtime_tenant_id: str | None = None,
        binding_repository: ProviderSecretBindingRepository,
    ) -> None:
        root = Path(secrets_root)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ProviderSecretUnavailable("provider secrets root is unavailable")
        try:
            self._root = root.resolve(strict=True)
        except OSError as exc:
            raise ProviderSecretUnavailable("provider secrets root is unavailable") from exc
        if runtime_mode == "shared" and runtime_tenant_id is not None:
            raise ProviderSecretAccessDenied(
                "shared provider runtime cannot bind a tenant secret scope"
            )
        if runtime_mode == "dedicated" and not runtime_tenant_id:
            raise ProviderSecretAccessDenied("dedicated provider runtime requires a tenant scope")
        self._runtime_mode = runtime_mode
        self._runtime_tenant_id = runtime_tenant_id
        self._binding_repository = binding_repository

    async def resolve(
        self,
        *,
        selection: DataPlaneSelection,
    ) -> SecretStr:
        if selection.mode != self._runtime_mode:
            raise ProviderSecretAccessDenied(
                "provider secret is outside the current data-plane boundary"
            )
        if (
            not _IDENTIFIER_PATTERN.fullmatch(selection.tenant_id)
            or not _IDENTIFIER_PATTERN.fullmatch(selection.route_ref)
            or not _IDENTIFIER_PATTERN.fullmatch(selection.provider_profile_ref)
        ):
            raise ProviderSecretAccessDenied("provider route context is invalid")
        expected_tenant_id = self._runtime_tenant_id if self._runtime_mode == "dedicated" else None
        if self._runtime_mode == "dedicated" and selection.tenant_id != expected_tenant_id:
            raise ProviderSecretAccessDenied("provider route tenant is outside the current runtime")
        provider_profile = await self._binding_repository.resolve_bound_profile(selection)
        if provider_profile is None:
            raise ProviderSecretAccessDenied(
                "provider profile is outside the selected route boundary"
            )
        expected_owner_key = "shared" if self._runtime_mode == "shared" else selection.tenant_id
        if (
            provider_profile.profile_id != selection.provider_profile_ref
            or provider_profile.scope != selection.mode
            or provider_profile.tenant_id != expected_tenant_id
            or provider_profile.owner_key != expected_owner_key
            or provider_profile.status != "active"
        ):
            raise ProviderSecretAccessDenied(
                "provider profile is outside the selected route boundary"
            )
        if self._runtime_mode == "shared":
            expected_secret_ref = f"shared/providers/{provider_profile.profile_id}"
        else:
            expected_secret_ref = (
                f"tenants/{selection.tenant_id}/providers/{provider_profile.profile_id}"
            )
        if provider_profile.secret_ref != expected_secret_ref:
            raise ProviderSecretAccessDenied(
                "provider secret reference is outside the selected route"
            )

        current = self._root
        for part in expected_secret_ref.split("/"):
            current = current / part
            if current.is_symlink():
                raise ProviderSecretUnavailable("provider secret file is unavailable")
        try:
            secret_file = current.resolve(strict=True)
            secret_file.relative_to(self._root)
            if secret_file.is_symlink() or not secret_file.is_file():
                raise ProviderSecretUnavailable("provider secret file is unavailable")
            secret_value = secret_file.read_text(encoding="utf-8").rstrip("\r\n")
        except ProviderSecretUnavailable:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProviderSecretUnavailable("provider secret file is unavailable") from exc
        if not secret_value or "\x00" in secret_value:
            raise ProviderSecretUnavailable("provider secret file is unavailable")
        return SecretStr(secret_value)
