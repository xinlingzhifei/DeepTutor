import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from deeptutor.teaching.openmaic.catalog_sync import (
    CatalogSyncError,
    provider_document_from_catalog,
)
from deeptutor.teaching.openmaic.provider_config import parse_provider_configuration


def catalog():
    return {
        "services": {
            "llm": {
                "active_profile_id": "selected",
                "active_model_id": "model",
                "profiles": [
                    {
                        "id": "selected",
                        "binding": "openai",
                        "base_url": "https://provider.example/v1",
                        "api_key": "secret-canary",
                        "models": [{"id": "model", "model": "course-model"}],
                    }
                ],
            }
        }
    }


def test_selected_model_round_trips_through_openmaic_contract():
    value = catalog()
    before = deepcopy(value)
    document = provider_document_from_catalog(value)
    result = parse_provider_configuration(
        json.dumps(document).encode(), scope="shared", tenant_id=None, profile_id="platform-default"
    )
    assert result.model_name == "course-model"
    assert result.api_key.get_secret_value() == "secret-canary"
    assert value == before


def test_empty_or_cleared_selection_is_unconfigured():
    assert provider_document_from_catalog({}) is None
    value = catalog()
    value["services"]["llm"]["active_model_id"] = None
    assert provider_document_from_catalog(value) is None


def test_switch_uses_only_selected_profile_credentials():
    value = catalog()
    other = deepcopy(value["services"]["llm"]["profiles"][0])
    other.update(id="other", api_key="other-secret")
    value["services"]["llm"]["profiles"].append(other)
    value["services"]["llm"]["active_profile_id"] = "other"
    assert provider_document_from_catalog(value)["providers"]["openai"]["apiKey"] == "other-secret"


def test_missing_key_does_not_reuse_another_profile():
    value = catalog()
    value["services"]["llm"]["profiles"][0]["api_key"] = ""
    assert provider_document_from_catalog(value) is None


def test_unsupported_binding_is_explicit_and_secret_safe():
    value = catalog()
    value["services"]["llm"]["profiles"][0]["binding"] = "openai_codex"
    with pytest.raises(CatalogSyncError, match="unsupported_provider") as error:
        provider_document_from_catalog(value)
    assert "secret-canary" not in str(error.value)


def test_atomic_publication_rotation_clear_and_restart(tmp_path):
    from deeptutor.teaching.openmaic.catalog_sync import publish_provider_document
    from deeptutor.teaching.openmaic.provider_config import (
        ProviderNotConfigured,
        read_provider_configuration,
    )

    target = tmp_path / "shared/providers/platform-default"
    first = provider_document_from_catalog(catalog())
    publish_provider_document(target, first)

    def read():
        return read_provider_configuration(
            tmp_path, scope="shared", tenant_id=None, profile_id="platform-default"
        )

    assert read().model_name == "course-model"
    second = deepcopy(first)
    second["defaultModel"] = "openai:replacement"
    second["providers"]["openai"]["apiKey"] = "replacement-key"
    publish_provider_document(target, second)
    assert read().model_name == "replacement"
    assert read().api_key.get_secret_value() == "replacement-key"
    publish_provider_document(target, None)
    with pytest.raises(ProviderNotConfigured):
        read()


@pytest.fixture
def sync_runtime(tmp_path, monkeypatch):
    import deeptutor.runtime.home
    import deeptutor.services.config
    from deeptutor.teaching.openmaic import catalog_sync

    profile = SimpleNamespace(
        id="platform-default",
        scope="shared",
        tenant_id=None,
        owner_key="shared",
        secret_ref="shared/providers/platform-default",
        status="active",
        provider_type="openai",
        model_name="old",
        api_base_url="https://old.example/v1",
    )
    events = []

    class Session:
        @asynccontextmanager
        async def begin(self):
            yield
            events.append(("commit", profile.model_name))

        async def scalar(self, _statement):
            return profile

        async def execute(self, statement):
            events.append(("sql", str(statement)))

        async def commit(self):
            pass

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(catalog_sync, "_shared_update_session", session)
    monkeypatch.setattr(
        deeptutor.services.config, "load_platform_settings", lambda: SimpleNamespace(enabled=True)
    )
    monkeypatch.setattr(deeptutor.runtime.home, "get_runtime_data_root", lambda: tmp_path)
    return (
        catalog_sync,
        profile,
        events,
        tmp_path / "system/secrets/openmaic-providers/shared/providers/platform-default",
    )


def test_sync_updates_registration_and_publishes_latest_saved_selection(sync_runtime):
    module, profile, events, target = sync_runtime
    result = asyncio.run(module.synchronize_shared_catalog(SimpleNamespace(load=catalog)))
    assert result == {"state": "synced"}
    assert profile.model_name == "course-model"
    assert profile.owner_key == "shared"
    assert json.loads(target.read_text())["defaultModel"] == "openai:course-model"
    assert any("pg_advisory_unlock" in entry[1] for entry in events)
    assert "secret-canary" not in json.dumps(result)


def test_clear_keeps_export_route_profile_available(sync_runtime):
    module, profile, _events, target = sync_runtime
    result = asyncio.run(module.synchronize_shared_catalog(SimpleNamespace(load=lambda: {})))
    assert result == {"state": "not_configured"}
    assert profile.status == "active"
    assert json.loads(target.read_text())["providers"] == {}


def test_sync_refuses_tenant_profile_without_writing_credentials(sync_runtime):
    module, profile, _events, target = sync_runtime
    profile.tenant_id = "private-tenant"
    result = asyncio.run(module.synchronize_shared_catalog(SimpleNamespace(load=catalog)))
    assert result == {"state": "error", "reason": "provider_scope_mismatch"}
    assert not target.exists()


def test_failed_publish_leaves_generation_unconfigured(sync_runtime, monkeypatch):
    module, profile, _events, target = sync_runtime
    publish = module.publish_provider_document

    def fail(path, document):
        if document is not None:
            raise OSError("secret-canary must not escape")
        return publish(path, document)

    monkeypatch.setattr(module, "publish_provider_document", fail)
    result = asyncio.run(module.synchronize_shared_catalog(SimpleNamespace(load=catalog)))
    assert result == {"state": "error", "reason": "synchronization_failed"}
    assert json.loads(target.read_text())["providers"] == {}


@pytest.mark.parametrize(
    "binding,expected",
    [
        ("custom", "openai"),
        ("dashscope", "openai"),
        ("zhipu", "openai"),
        ("gemini", "openai"),
        ("custom_anthropic", "anthropic"),
    ],
)
def test_catalog_binding_preserves_wire_protocol(binding, expected):
    value = catalog()
    value["services"]["llm"]["profiles"][0]["binding"] = binding
    result = provider_document_from_catalog(value)
    assert result["defaultModel"] == f"{expected}:course-model"
    assert result["providers"][expected]["baseUrl"] == "https://provider.example/v1"


def test_shared_generation_guard_tracks_clear_and_configuration(sync_runtime):
    from deeptutor.teaching.openmaic.data_planes import SharedModelNotConfigured

    module, _, _, target = sync_runtime
    with pytest.raises(SharedModelNotConfigured):
        module.require_shared_course_model()
    module.publish_provider_document(target, provider_document_from_catalog(catalog()))
    module.require_shared_course_model()
    module.publish_provider_document(target, None)
    with pytest.raises(SharedModelNotConfigured):
        module.require_shared_course_model()


def test_missing_model_terminal_error_is_not_retried():
    from deeptutor.teaching.job_errors import classify_engine_error_code
    failure = classify_engine_error_code("model_not_configured")
    assert failure.category == "model_not_configured"
    assert not failure.retryable
