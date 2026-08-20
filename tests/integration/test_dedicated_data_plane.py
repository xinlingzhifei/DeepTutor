from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys

import pytest

from deeptutor.teaching.models import AuditLog, DataPlaneRoute, ProviderProfile, Tenant
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneResolution,
    DataPlaneRouteRecord,
    DataPlaneSelector,
    DataPlaneUnavailable,
    ProviderProfileRecord,
)


def _load_registration_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "register_data_plane.py"
    spec = importlib.util.spec_from_file_location("register_data_plane_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_REGISTER = _load_registration_module()
DedicatedDataPlaneRegistration = _REGISTER.DedicatedDataPlaneRegistration
VerifiedDataPlaneHealth = _REGISTER.VerifiedDataPlaneHealth
provider_secret_reference_exists = _REGISTER.provider_secret_reference_exists
register_dedicated_data_plane = _REGISTER.register_dedicated_data_plane


@dataclass
class RegistrationHarness:
    health: VerifiedDataPlaneHealth
    secret_valid: bool = True

    def __post_init__(self) -> None:
        self.registered: list[DedicatedDataPlaneRegistration] = []
        self.health_calls: list[DedicatedDataPlaneRegistration] = []
        self.secret_calls: list[tuple[str, str, str]] = []

    async def verify_health(
        self,
        registration: DedicatedDataPlaneRegistration,
    ) -> VerifiedDataPlaneHealth:
        self.health_calls.append(registration)
        return self.health

    def verify_secret(self, tenant_id: str, profile_id: str, secret_ref: str) -> bool:
        self.secret_calls.append((tenant_id, profile_id, secret_ref))
        return self.secret_valid

    async def register(self, registration: DedicatedDataPlaneRegistration) -> None:
        self.registered.append(registration)


def _registration() -> DedicatedDataPlaneRegistration:
    return DedicatedDataPlaneRegistration(
        tenant_id="tenant-private",
        route_id="dedicated-tenant-private",
        base_url="https://openmaic.tenant-private.internal",
        worker_pool="generation-tenant-private",
        queue_name="openmaic.tenant-private",
        provider_profile_id="provider-tenant-private",
        provider_type="openai-compatible",
        model_name="private-model",
        provider_api_base_url="https://provider.internal/v1",
        secret_ref="tenants/tenant-private/providers/provider-tenant-private",
    )


def _healthy() -> VerifiedDataPlaneHealth:
    return VerifiedDataPlaneHealth(
        service="openmaic",
        upstream_commit="0cf2a330411681190e89f48e20f305345ff99f87",
        app_version="0.3.1",
        contract_versions=("1.0",),
        capabilities=("outline", "content", "micro", "export", "cancel", "artifact-manifest"),
        export_formats=("classroom_zip", "pptx", "offline_html", "mp4"),
    )


def test_registration_verifies_health_contract_and_secret_before_persisting() -> None:
    harness = RegistrationHarness(_healthy())
    registration = _registration()

    asyncio.run(
        register_dedicated_data_plane(
            registration,
            verify_health=harness.verify_health,
            verify_secret=harness.verify_secret,
            persist=harness.register,
        )
    )

    assert harness.secret_calls == [
        (
            "tenant-private",
            "provider-tenant-private",
            "tenants/tenant-private/providers/provider-tenant-private",
        )
    ]
    assert harness.health_calls == [registration]
    assert harness.registered == [registration]


@pytest.mark.parametrize(
    "health",
    [
        VerifiedDataPlaneHealth(
            service="other",
            upstream_commit="0cf2a330411681190e89f48e20f305345ff99f87",
            app_version="0.3.1",
            contract_versions=("1.0",),
            capabilities=("outline", "content", "micro", "export", "cancel", "artifact-manifest"),
            export_formats=("classroom_zip", "pptx", "offline_html", "mp4"),
        ),
        VerifiedDataPlaneHealth(
            service="openmaic",
            upstream_commit="0cf2a330411681190e89f48e20f305345ff99f87",
            app_version="0.3.1",
            contract_versions=("0.9",),
            capabilities=("outline", "content", "micro", "export", "cancel", "artifact-manifest"),
            export_formats=("classroom_zip", "pptx", "offline_html", "mp4"),
        ),
        VerifiedDataPlaneHealth(
            service="openmaic",
            upstream_commit="wrong",
            app_version="0.3.1",
            contract_versions=("1.0",),
            capabilities=("outline",),
            export_formats=("classroom_zip",),
        ),
    ],
)
def test_registration_fails_closed_before_persisting_on_contract_mismatch(
    health: VerifiedDataPlaneHealth,
) -> None:
    harness = RegistrationHarness(health)

    with pytest.raises(ValueError, match="contract"):
        asyncio.run(
            register_dedicated_data_plane(
                _registration(),
                verify_health=harness.verify_health,
                verify_secret=harness.verify_secret,
                persist=harness.register,
            )
        )

    assert harness.registered == []


def test_private_tenant_never_uses_shared_provider_when_dedicated_plane_is_down() -> None:
    shared_requests: list[str] = []

    class Repository:
        async def resolve(self, tenant_id: str) -> DataPlaneResolution:
            return DataPlaneResolution(
                tenant_id=tenant_id,
                tenant_mode="dedicated",
                route=DataPlaneRouteRecord(
                    route_id="shared-primary",
                    tenant_id=None,
                    owner_key="shared",
                    mode="shared",
                    base_url="http://openmaic-shared:3000",
                    worker_pool="shared-generation",
                    queue_name="openmaic.shared",
                    provider_profile_id="platform-default",
                    status="active",
                    health_status="healthy",
                ),
                provider_profile=ProviderProfileRecord(
                    profile_id="platform-default",
                    scope="shared",
                    tenant_id=None,
                    owner_key="shared",
                    provider_type="openai-compatible",
                    model_name="platform-model",
                    api_base_url=None,
                    secret_ref="shared/providers/platform-default",
                    status="active",
                ),
            )

        async def record_decision(self, decision) -> None:
            if decision.decision == "selected":
                shared_requests.append(decision.tenant_id)

    selector = DataPlaneSelector(
        settings=type("Settings", (), {"enabled": True})(), repository=Repository()
    )

    with pytest.raises(DataPlaneUnavailable):
        asyncio.run(selector.resolve("tenant-private"))

    assert shared_requests == []


def test_registration_secret_check_uses_exact_tenant_file_without_exposing_value(tmp_path) -> None:
    root = tmp_path / "provider-secrets"
    secret = root / "tenants" / "tenant-private" / "providers" / "provider-tenant-private"
    secret.parent.mkdir(parents=True)
    secret.write_text("PROVIDER_SECRET_SENTINEL", encoding="utf-8")

    assert provider_secret_reference_exists(
        root,
        "tenant-private",
        "provider-tenant-private",
        "tenants/tenant-private/providers/provider-tenant-private",
    )
    assert not provider_secret_reference_exists(
        root,
        "tenant-other",
        "provider-tenant-private",
        "tenants/tenant-private/providers/provider-tenant-private",
    )
    assert "PROVIDER_SECRET_SENTINEL" not in repr(_registration())


def test_default_registration_persists_exact_dedicated_binding_in_one_transaction(
    monkeypatch,
) -> None:
    tenant = Tenant(
        id="tenant-private",
        name="Private Tenant",
        status="active",
        data_plane_mode="shared",
    )

    class Session:
        def __init__(self) -> None:
            self.scalar_results = [tenant, None, None]
            self.added: list[object] = []
            self.transactions = 0
            self.flushes = 0

        @asynccontextmanager
        async def begin(self):
            self.transactions += 1
            yield

        async def scalar(self, _statement):
            return self.scalar_results.pop(0)

        def add(self, value: object) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            self.flushes += 1

    session = Session()

    @asynccontextmanager
    async def fake_platform_session():
        yield session

    monkeypatch.setattr(_REGISTER, "platform_session", fake_platform_session)
    harness = RegistrationHarness(_healthy())

    asyncio.run(
        register_dedicated_data_plane(
            _registration(),
            verify_health=harness.verify_health,
            verify_secret=harness.verify_secret,
        )
    )

    assert session.transactions == 1
    assert session.flushes == 1
    assert tenant.data_plane_mode == "dedicated"
    profile = next(value for value in session.added if isinstance(value, ProviderProfile))
    route = next(value for value in session.added if isinstance(value, DataPlaneRoute))
    audit = next(value for value in session.added if isinstance(value, AuditLog))
    assert (profile.scope, profile.tenant_id, profile.owner_key) == (
        "dedicated",
        "tenant-private",
        "tenant-private",
    )
    assert profile.secret_ref == "tenants/tenant-private/providers/provider-tenant-private"
    assert (route.mode, route.tenant_id, route.owner_key, route.health_status) == (
        "dedicated",
        "tenant-private",
        "tenant-private",
        "healthy",
    )
    assert route.provider_profile_id == profile.id
    assert audit.action == "teaching.data_plane.registered"
