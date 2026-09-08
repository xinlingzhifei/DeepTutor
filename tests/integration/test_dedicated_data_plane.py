from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from deeptutor.teaching.models import AuditLog, DataPlaneRoute, ProviderProfile, Tenant
from deeptutor.teaching.openmaic.client import OpenMAICRequestFailed
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneResolution,
    DataPlaneRouteRecord,
    DataPlaneSelector,
    DataPlaneUnavailable,
    ProviderProfileRecord,
)
from deeptutor.teaching.secret_permissions import restrict_secret_file


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
        self.tenant_calls: list[str] = []
        self.health_calls: list[DedicatedDataPlaneRegistration] = []
        self.secret_calls: list[tuple[str, str, str]] = []

    async def verify_tenant(self, tenant_id: str) -> None:
        self.tenant_calls.append(tenant_id)

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


def test_repository_injected_sessions_stay_in_the_selected_database(tmp_path, monkeypatch):
    import inspect

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import deeptutor.teaching.repositories.data_planes as module

    assert "session_factory" in inspect.signature(module.SqlAlchemyDataPlaneRepository).parameters

    def forbidden_default():
        raise AssertionError("the default database must not be opened")

    monkeypatch.setattr(module, "platform_session", forbidden_default)

    async def scenario():
        engines = [
            create_async_engine(
                f"sqlite+aiosqlite:///{tmp_path / (name + '.db')}",
                execution_options={"schema_translate_map": {"platform": None}},
            )
            for name in ("selected", "other")
        ]
        try:
            repositories = []
            for index, engine in enumerate(engines):
                async with engine.begin() as connection:
                    for table in (
                        Tenant.__table__,
                        ProviderProfile.__table__,
                        DataPlaneRoute.__table__,
                    ):
                        await connection.run_sync(table.create)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory.begin() as session:
                    session.add(
                        Tenant(
                            id="tenant-selected",
                            name=str(index),
                            status="active",
                            data_plane_mode="shared" if index == 0 else "dedicated",
                        )
                    )
                    if index == 0:
                        session.add(
                            ProviderProfile(
                                id="platform-default",
                                scope="shared",
                                tenant_id=None,
                                owner_key="shared",
                                provider_type="openai",
                                model_name="configured-model",
                                api_base_url="https://llm.example.test/v1",
                                secret_ref="shared/providers/platform-default",
                                status="active",
                            )
                        )
                        session.add(
                            DataPlaneRoute(
                                id="shared",
                                mode="shared",
                                tenant_id=None,
                                owner_key="shared",
                                base_url="http://openmaic:3000",
                                worker_pool="shared",
                                queue_name="shared",
                                provider_profile_id="platform-default",
                                status="active",
                                health_status="healthy",
                            )
                        )
                repositories.append(module.SqlAlchemyDataPlaneRepository(session_factory=factory))
            assert (await repositories[0].resolve("tenant-selected")).tenant_mode == "shared"
            assert (await repositories[1].resolve("tenant-selected")).tenant_mode == "dedicated"
            assert (
                await repositories[0].resolve_shared_health_route()
            ).provider_profile_id == "platform-default"
            assert (await repositories[1].resolve_dedicated_health_inventory()).active_tenants == 1
            selection = await repositories[0].resolve_worker_selection(
                tenant_id="tenant-selected",
                route_id="shared",
                provider_profile_id="platform-default",
                worker_pool_ref="shared",
                queue_ref="shared",
            )
            assert selection is not None
            assert await repositories[0].resolve_bound_route(selection) is not None
            assert await repositories[0].resolve_bound_profile(selection) is not None
            assert await repositories[1].resolve_bound_profile(selection) is None
            assert await repositories[0].set_health("shared", "unhealthy")
            assert await repositories[0].resolve_bound_profile(selection) is None
        finally:
            for engine in engines:
                await engine.dispose()

    asyncio.run(scenario())


def test_dedicated_registration_checks_the_same_json_provider_descriptor(tmp_path, monkeypatch):
    from dataclasses import replace
    import json

    from deeptutor.teaching.openmaic.provider_config import parse_provider_configuration

    assert hasattr(_REGISTER, "dedicated_provider_configuration_matches"), (
        "JSON registration binding is missing"
    )
    registration = replace(
        _registration(), provider_type="openai", model_name="vendor/model:version"
    )
    document = {
        "scope": "dedicated",
        "tenantId": registration.tenant_id,
        "profileId": registration.provider_profile_id,
        "defaultModel": "openai:vendor/model:version",
        "providers": {
            "openai": {
                "apiKey": "scoped-registration-test-canary",
                "baseUrl": registration.provider_api_base_url,
            }
        },
    }
    configuration = parse_provider_configuration(
        json.dumps(document).encode(),
        scope="dedicated",
        tenant_id=registration.tenant_id,
        profile_id=registration.provider_profile_id,
    )
    calls = []

    def read(root, **identity):
        calls.append((root, identity))
        return configuration

    monkeypatch.setattr(_REGISTER, "read_provider_configuration", read)
    assert _REGISTER.dedicated_provider_configuration_matches(registration, tmp_path)
    assert calls == [
        (
            tmp_path,
            {
                "scope": "dedicated",
                "tenant_id": registration.tenant_id,
                "profile_id": registration.provider_profile_id,
            },
        )
    ]
    assert not _REGISTER.dedicated_provider_configuration_matches(
        replace(registration, model_name="wrong-model"), tmp_path
    )


def test_registration_verifies_health_contract_and_secret_before_persisting() -> None:
    harness = RegistrationHarness(_healthy())
    registration = _registration()

    asyncio.run(
        register_dedicated_data_plane(
            registration,
            verify_tenant=harness.verify_tenant,
            verify_health=harness.verify_health,
            verify_secret=harness.verify_secret,
            persist=harness.register,
        )
    )

    assert harness.tenant_calls == ["tenant-private"]
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
                verify_tenant=harness.verify_tenant,
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


def test_explicit_writer_persists_exact_dedicated_binding_in_one_transaction(
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

    monkeypatch.setattr(
        _REGISTER,
        "async_sessionmaker",
        lambda _engine, *, expire_on_commit: fake_platform_session,
    )
    writer = _REGISTER.SqlAlchemyDedicatedDataPlaneWriter(object())

    asyncio.run(writer.persist(_registration()))

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


def test_explicit_writer_is_exactly_idempotent_without_duplicate_audit(
    monkeypatch,
) -> None:
    registration = _registration()
    tenant = Tenant(
        id=registration.tenant_id,
        name="Private Tenant",
        status="active",
        data_plane_mode="dedicated",
    )
    route = DataPlaneRoute(
        id=registration.route_id,
        tenant_id=registration.tenant_id,
        owner_key=registration.tenant_id,
        mode="dedicated",
        base_url=registration.base_url,
        worker_pool=registration.worker_pool,
        queue_name=registration.queue_name,
        provider_profile_id=registration.provider_profile_id,
        status="active",
        health_status="unknown",
    )
    profile = ProviderProfile(
        id=registration.provider_profile_id,
        scope="dedicated",
        tenant_id=registration.tenant_id,
        owner_key=registration.tenant_id,
        provider_type=registration.provider_type,
        model_name=registration.model_name,
        api_base_url=registration.provider_api_base_url,
        secret_ref=registration.secret_ref,
        status="active",
    )

    class Session:
        def __init__(self) -> None:
            self.scalar_results = [tenant, route, profile]
            self.added: list[object] = []
            self.transactions = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info) -> None:
            return None

        @asynccontextmanager
        async def begin(self):
            self.transactions += 1
            yield

        async def scalar(self, _statement):
            return self.scalar_results.pop(0)

        def add(self, value: object) -> None:
            self.added.append(value)

    session = Session()
    monkeypatch.setattr(
        _REGISTER,
        "async_sessionmaker",
        lambda _engine, *, expire_on_commit: lambda: session,
    )

    writer = _REGISTER.SqlAlchemyDedicatedDataPlaneWriter(object())
    asyncio.run(writer.persist(registration))

    assert session.transactions == 1
    assert session.added == []
    assert route.health_status == "healthy"
    assert route.health_checked_at is not None


def test_explicit_writer_rolls_back_conflicting_registration(monkeypatch) -> None:
    registration = _registration()
    tenant = Tenant(
        id=registration.tenant_id,
        name="Private Tenant",
        status="active",
        data_plane_mode="shared",
    )
    conflicting_route = DataPlaneRoute(
        id=registration.route_id,
        tenant_id=registration.tenant_id,
        owner_key=registration.tenant_id,
        mode="dedicated",
        base_url="https://different.internal",
        worker_pool=registration.worker_pool,
        queue_name=registration.queue_name,
        provider_profile_id=registration.provider_profile_id,
        status="active",
        health_status="unknown",
    )

    class Session:
        def __init__(self) -> None:
            self.scalar_results = [tenant, conflicting_route, None]
            self.added: list[object] = []
            self.transactions = 0
            self.rollbacks = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info) -> None:
            return None

        @asynccontextmanager
        async def begin(self):
            self.transactions += 1
            try:
                yield
            except BaseException:
                self.rollbacks += 1
                raise

        async def scalar(self, _statement):
            return self.scalar_results.pop(0)

        def add(self, value: object) -> None:
            self.added.append(value)

    session = Session()
    monkeypatch.setattr(
        _REGISTER,
        "async_sessionmaker",
        lambda _engine, *, expire_on_commit: lambda: session,
    )
    writer = _REGISTER.SqlAlchemyDedicatedDataPlaneWriter(object())

    with pytest.raises(ValueError, match="conflicts"):
        asyncio.run(writer.persist(registration))

    assert session.transactions == 1
    assert session.rollbacks == 1
    assert session.added == []
    assert tenant.data_plane_mode == "shared"


def _operator_arguments(
    *,
    config: Path,
    provider_secrets_root: Path,
    service_secret_file: Path,
) -> list[str]:
    return [
        "--config",
        str(config),
        "--provider-secrets-root",
        str(provider_secrets_root),
        "--service-secret-file",
        str(service_secret_file),
        "--tenant-id",
        "tenant-private",
        "--route-id",
        "dedicated-tenant-private",
        "--base-url",
        "https://openmaic.tenant-private.internal",
        "--worker-pool",
        "generation-tenant-private",
        "--queue-name",
        "openmaic.tenant-private",
        "--provider-profile-id",
        "provider-tenant-private",
        "--provider-type",
        "openai",
        "--model-name",
        "private-model",
        "--provider-api-base-url",
        "https://provider.internal/v1",
    ]


@pytest.mark.parametrize("health_status", [200, 503])
def test_operator_cli_uses_explicit_inputs_signs_health_and_closes_resources(
    monkeypatch,
    tmp_path,
    capsys,
    health_status: int,
) -> None:
    config = tmp_path / "platform.json"
    config.write_text("{}", encoding="utf-8")
    provider_secrets_root = tmp_path / "provider-secrets"
    provider_secret = (
        provider_secrets_root
        / "tenants"
        / "tenant-private"
        / "providers"
        / "provider-tenant-private"
    )
    provider_secret.parent.mkdir(parents=True)
    provider_secret.write_text(
        json.dumps(
            {
                "scope": "dedicated",
                "tenantId": "tenant-private",
                "profileId": "provider-tenant-private",
                "defaultModel": "openai:private-model",
                "providers": {
                    "openai": {
                        "apiKey": "PROVIDER_SECRET_SENTINEL",
                        "baseUrl": "https://provider.internal/v1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    if os.name == "posix":
        if os.geteuid() == 0:
            os.chown(provider_secret, 1000, 1000)
        elif os.geteuid() != 1000 or os.getegid() != 1000:
            pytest.skip("requires root to provision UID1000 file, or UID/GID1000 runtime")
    restrict_secret_file(provider_secret)
    service_secret_file = tmp_path / "openmaic_service_secret"
    service_secret_file.write_text("SERVICE_SECRET_SENTINEL_0123456789", encoding="utf-8")
    arguments = _operator_arguments(
        config=config,
        provider_secrets_root=provider_secrets_root,
        service_secret_file=service_secret_file,
    )
    events: list[str] = []
    loaded_configs: list[Path] = []

    def load_settings(path: Path):
        loaded_configs.append(path)
        return SimpleNamespace(
            enabled=True,
            database_url=SecretStr("postgresql+asyncpg://operator@db/platform"),
        )

    class Engine:
        disposed = False

        async def dispose(self) -> None:
            events.append("dispose-engine")
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(_REGISTER, "load_platform_settings", load_settings)
    monkeypatch.setattr(_REGISTER, "create_async_engine", lambda *_args, **_kwargs: engine)

    class Writer:
        def __init__(self, received_engine) -> None:
            assert received_engine is engine

        async def require_active_tenant(self, tenant_id: str) -> None:
            events.append(f"active:{tenant_id}")

        async def persist(self, registration: DedicatedDataPlaneRegistration) -> None:
            assert registration.provider_type == "openai"
            assert registration.model_name == "private-model"
            assert registration.provider_api_base_url == "https://provider.internal/v1"
            events.append(f"persist:{registration.secret_ref}")

    monkeypatch.setattr(_REGISTER, "SqlAlchemyDedicatedDataPlaneWriter", Writer)
    original_provider_check = _REGISTER.dedicated_provider_configuration_matches

    def provider_check(registration, root) -> bool:
        events.append("provider-secret")
        assert root == provider_secrets_root
        assert registration.secret_ref == "tenants/tenant-private/providers/provider-tenant-private"
        return original_provider_check(registration, root)

    monkeypatch.setattr(_REGISTER, "dedicated_provider_configuration_matches", provider_check)
    original_read_service_secret = _REGISTER.read_service_secret

    def read_secret(path: Path):
        events.append("service-secret")
        return original_read_service_secret(path)

    monkeypatch.setattr(_REGISTER, "read_service_secret", read_secret)
    created_clients: list[httpx.AsyncClient] = []
    real_async_client = httpx.AsyncClient

    def handle_health(request: httpx.Request) -> httpx.Response:
        events.append("signed-health")
        assert "PROVIDER_SECRET_SENTINEL" not in repr(request.headers)
        assert b"PROVIDER_SECRET_SENTINEL" not in request.content
        assert request.url.path == "/api/yfeistai/v1/health"
        assert request.headers["x-yfeistai-tenant-id"] == "tenant-private"
        assert request.headers["x-yfeistai-job-id"] == "health"
        assert len(request.headers["x-yfeistai-signature"]) == 64
        payload = {
            "service": "openmaic",
            "upstreamCommit": "0cf2a330411681190e89f48e20f305345ff99f87",
            "appVersion": "0.3.1",
            "contractVersions": ["1.0"],
            "capabilities": [
                "outline",
                "content",
                "micro",
                "export",
                "cancel",
                "artifact-manifest",
            ],
            "exportFormats": ["classroom_zip", "pptx", "offline_html", "mp4"],
        }
        return httpx.Response(health_status, json=payload, request=request)

    def async_client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle_health)
        client = real_async_client(*args, **kwargs)
        created_clients.append(client)
        return client

    monkeypatch.setattr(_REGISTER.httpx, "AsyncClient", async_client_factory)

    if health_status == 200:
        assert _REGISTER.main(arguments) == 0
    else:
        with pytest.raises(OpenMAICRequestFailed):
            _REGISTER.main(arguments)

    assert loaded_configs == [config]
    assert events[:-1] == [
        "active:tenant-private",
        "provider-secret",
        "service-secret",
        "signed-health",
        *(
            ["persist:tenants/tenant-private/providers/provider-tenant-private"]
            if health_status == 200
            else []
        ),
    ]
    assert events[-1] == "dispose-engine"
    assert engine.disposed
    assert len(created_clients) == 1 and created_clients[0].is_closed
    output = capsys.readouterr()
    rendered_output = output.out + output.err
    assert "PROVIDER_SECRET_SENTINEL" not in rendered_output
    assert "SERVICE_SECRET_SENTINEL" not in rendered_output

    parsed = _REGISTER._parser().parse_args(arguments)
    registration = _REGISTER._registration_from_arguments(parsed)
    assert registration.secret_ref == ("tenants/tenant-private/providers/provider-tenant-private")
    assert not hasattr(parsed, "secret_ref")
