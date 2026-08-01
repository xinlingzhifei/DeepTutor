from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import SecretStr
import pytest

from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneSelection,
    ProviderProfileRecord,
)
from deeptutor.teaching.openmaic.provider_secrets import (
    ProviderSecretAccessDenied,
    ProviderSecretResolver,
)


class BindingRepository:
    def __init__(self, profile: ProviderProfileRecord | None) -> None:
        self.profile = profile
        self.selections: list[DataPlaneSelection] = []

    async def resolve_bound_profile(
        self,
        selection: DataPlaneSelection,
    ) -> ProviderProfileRecord | None:
        self.selections.append(selection)
        return self.profile


def test_stale_selection_is_rebound_before_any_secret_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[Path] = []

    def tracked_read_text(path: Path, *args, **kwargs):
        reads.append(path)
        raise AssertionError("secret file must not be read")

    monkeypatch.setattr(Path, "read_text", tracked_read_text)
    repository = BindingRepository(None)
    resolver = ProviderSecretResolver(
        tmp_path,
        runtime_mode="shared",
        binding_repository=repository,
    )
    selection = DataPlaneSelection(
        tenant_id="tenant-standard",
        route_ref="shared-primary",
        provider_profile_ref="provider-old",
        mode="shared",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
    )

    with pytest.raises(ProviderSecretAccessDenied):
        asyncio.run(resolver.resolve(selection=selection))

    assert repository.selections == [selection]
    assert reads == []


def test_shared_pool_rejects_dedicated_provider_before_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_file = tmp_path / "tenants" / "tenant-private" / "providers" / "provider-private"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("DEDICATED_PROVIDER_SECRET_SENTINEL", encoding="utf-8")
    reads: list[Path] = []
    original_read_text = Path.read_text

    def tracked_read_text(path: Path, *args, **kwargs):
        reads.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read_text)
    selection = DataPlaneSelection(
        tenant_id="tenant-private",
        route_ref="dedicated-tenant-private",
        provider_profile_ref="provider-private",
        mode="dedicated",
        worker_pool_ref="generation-tenant-private",
        queue_ref="openmaic.tenant-private",
    )
    profile = ProviderProfileRecord(
        profile_id="provider-private",
        scope="dedicated",
        tenant_id="tenant-private",
        owner_key="tenant-private",
        provider_type="openai-compatible",
        model_name="private-model",
        api_base_url=None,
        secret_ref="tenants/tenant-private/providers/provider-private",
        status="active",
    )
    repository = BindingRepository(profile)
    resolver = ProviderSecretResolver(
        tmp_path,
        runtime_mode="shared",
        binding_repository=repository,
    )

    with pytest.raises(ProviderSecretAccessDenied) as captured:
        asyncio.run(resolver.resolve(selection=selection))

    assert repository.selections == []
    assert reads == []
    assert "DEDICATED_PROVIDER_SECRET_SENTINEL" not in repr(captured.value)
    assert profile.secret_ref not in repr(captured.value)


@pytest.mark.parametrize(
    ("mode", "tenant_id", "profile_id", "secret_ref"),
    [
        (
            "shared",
            "tenant-standard",
            "provider-shared",
            "shared/providers/provider-shared",
        ),
        (
            "dedicated",
            "tenant-private",
            "provider-private",
            "tenants/tenant-private/providers/provider-private",
        ),
    ],
)
def test_authorized_route_context_resolves_redacted_provider_secret(
    tmp_path: Path,
    mode: str,
    tenant_id: str,
    profile_id: str,
    secret_ref: str,
) -> None:
    secret_value = f"{mode.upper()}_PROVIDER_SECRET_SENTINEL"
    secret_file = tmp_path.joinpath(*secret_ref.split("/"))
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text(secret_value, encoding="utf-8")
    runtime_tenant_id = tenant_id if mode == "dedicated" else None
    selection = DataPlaneSelection(
        tenant_id=tenant_id,
        route_ref=f"route-{mode}",
        provider_profile_ref=profile_id,
        mode=mode,
        worker_pool_ref=f"pool-{mode}",
        queue_ref=f"queue-{mode}",
    )
    owner_key = tenant_id if mode == "dedicated" else "shared"
    profile = ProviderProfileRecord(
        profile_id=profile_id,
        scope=mode,
        tenant_id=runtime_tenant_id,
        owner_key=owner_key,
        provider_type="openai-compatible",
        model_name="configured-model",
        api_base_url=None,
        secret_ref=secret_ref,
        status="active",
    )
    resolver = ProviderSecretResolver(
        tmp_path,
        runtime_mode=mode,
        runtime_tenant_id=runtime_tenant_id,
        binding_repository=BindingRepository(profile),
    )

    secret = asyncio.run(resolver.resolve(selection=selection))

    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == secret_value
    assert secret_value not in repr(secret)
