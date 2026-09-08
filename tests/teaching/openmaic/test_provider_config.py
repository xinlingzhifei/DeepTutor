from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import os

import pytest

from deeptutor.teaching.openmaic.data_planes import ProviderProfileRecord

CANARY_KEY = "provider-config-test-canary-not-a-live-key"


def _module():
    name = "deeptutor.teaching.openmaic.provider_config"
    assert importlib.util.find_spec(name) is not None, "provider configuration boundary is missing"
    return importlib.import_module(name)


def _document(*, scope="shared", tenant_id=None, profile_id="platform-default"):
    return {
        "scope": scope,
        "tenantId": tenant_id,
        "profileId": profile_id,
        "defaultModel": "openai:configured-model",
        "providers": {
            "openai": {"apiKey": CANARY_KEY, "baseUrl": "https://llm.example.test/v1"},
        },
    }


def _parse(document=None, **identity):
    module = _module()
    body = json.dumps(_document() if document is None else document).encode()
    return module.parse_provider_configuration(
        body,
        scope=identity.get("scope", "shared"),
        tenant_id=identity.get("tenant_id"),
        profile_id=identity.get("profile_id", "platform-default"),
    )


def test_provider_config_preserves_one_key_and_returns_only_redacted_descriptor():
    config = _parse()
    assert config.api_key.get_secret_value() == CANARY_KEY
    assert config.descriptor() == {
        "scope": "shared",
        "tenantId": None,
        "profileId": "platform-default",
        "providerId": "openai",
        "modelName": "configured-model",
        "baseUrl": "https://llm.example.test/v1",
        "secretRef": "shared/providers/platform-default",
    }
    assert CANARY_KEY not in repr(config)
    assert CANARY_KEY not in json.dumps(config.descriptor())


@pytest.mark.parametrize("url", ("https://exa%mple.invalid/v1", "http://[v1.a]/v1"))
def test_provider_config_rejects_nonportable_url_hosts(url):
    document = _document()
    document["providers"]["openai"]["baseUrl"] = url
    with pytest.raises(_module().ProviderConfigurationError):
        _parse(document)


@pytest.mark.parametrize("key", ("\ufeffcanary", "canary\ufeff", "\u0085canary", "canary\u0085"))
def test_provider_config_rejects_cross_runtime_boundary_whitespace(key):
    document = _document()
    document["providers"]["openai"]["apiKey"] = key
    with pytest.raises(_module().ProviderConfigurationError):
        _parse(document)


@pytest.mark.skipif(
    os.name != "posix", reason="Linux UID/GID and mode proof is not provided by Windows"
)
def test_provider_file_reader_checks_real_owner_mode_and_bytes(tmp_path):
    if os.geteuid() not in {0, 1000} or (os.geteuid() == 1000 and os.getegid() != 1000):
        pytest.skip("requires root to provision UID1000 file, or UID/GID1000 runtime")
    module = _module()
    source = tmp_path / "shared/providers/platform-default"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(_document()), encoding="utf-8")
    if os.geteuid() == 0:
        os.chown(source, 1000, 1000)
    source.chmod(0o400)
    assert (
        module.read_provider_configuration(
            tmp_path, scope="shared", tenant_id=None, profile_id="platform-default"
        ).api_key.get_secret_value()
        == CANARY_KEY
    )
    source.chmod(0o644)
    with pytest.raises(module.ProviderConfigurationError):
        module.read_provider_configuration(
            tmp_path, scope="shared", tenant_id=None, profile_id="platform-default"
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update(scope="dedicated"),
        lambda value: value.update(tenantId="private-tenant"),
        lambda value: value.update(profileId="private-profile"),
        lambda value: value.update(defaultModel=""),
        lambda value: value.update(defaultModel="openai/model"),
        lambda value: value.update(defaultModel="openai:" + "x" * 129),
        lambda value: value.update(defaultModel="openai:model\nprivate"),
        lambda value: value.update(defaultModel="unknown:model"),
        lambda value: value.update(unexpected=CANARY_KEY),
        lambda value: value["providers"].update(private={"apiKey": CANARY_KEY}),
        lambda value: value["providers"]["openai"].update(apiKey=""),
        lambda value: value["providers"]["openai"].update(apiKey=True),
        lambda value: value["providers"]["openai"].update(apiKey="null\x00key"),
        lambda value: value["providers"]["openai"].update(baseUrl="https://llm.example.test/v1/"),
        lambda value: value["providers"]["openai"].update(baseUrl="https://key:secret@llm.test"),
        lambda value: value["providers"]["openai"].update(baseUrl="https://llm.test?key=secret"),
        lambda value: value["providers"]["openai"].update(baseUrl="https://llm.test/a/../b"),
        lambda value: value["providers"]["openai"].update(models=["different-model"]),
    ),
)
def test_provider_config_rejects_ambiguous_or_foreign_configuration_without_key_leak(mutation):
    module = _module()
    document = _document()
    mutation(document)
    with pytest.raises(module.ProviderConfigurationError) as captured:
        _parse(document)
    assert CANARY_KEY not in str(captured.value)
    assert CANARY_KEY not in repr(captured.value)


@pytest.mark.parametrize(
    "body",
    (
        b'{"scope":"shared","scope":"dedicated"}',
        b'{"scope":"shared","providers":{"openai":{"apiKey":"first","apiKey":"second"}}}',
        b"{not-json}",
        b"[]",
        b"null",
        b"\xef\xbb\xbf{}",
        b"\xff",
        b" " * (64 * 1024 + 1),
        b"[" * 2000 + b"0" + b"]" * 2000,
        b"NaN",
        b"Infinity",
    ),
    ids=(
        "duplicate-root",
        "duplicate-provider-key",
        "invalid-json",
        "array",
        "null",
        "bom",
        "invalid-utf8",
        "oversized",
        "deep-json",
        "nan",
        "infinity",
    ),
)
def test_provider_config_rejects_json_yaml_parser_disagreement(body):
    module = _module()
    with pytest.raises(module.ProviderConfigurationError):
        module.parse_provider_configuration(
            body, scope="shared", tenant_id=None, profile_id="platform-default"
        )


@pytest.mark.parametrize("provider", ("ollama", "lemonade"))
def test_provider_config_accepts_existing_keyless_provider_with_explicit_endpoint(provider):
    document = _document()
    document["defaultModel"] = f"{provider}:vendor/model:version"
    document["providers"] = {provider: {"apiKey": "", "baseUrl": "http://model.internal:11434/v1"}}
    config = _parse(document)
    assert config.model_name == "vendor/model:version"
    assert config.api_key.get_secret_value() == ""


def test_dedicated_provider_configuration_requires_exact_tenant_and_profile():
    document = _document(scope="dedicated", tenant_id="tenant-acme", profile_id="provider-acme")
    config = _parse(
        document, scope="dedicated", tenant_id="tenant-acme", profile_id="provider-acme"
    )
    assert config.secret_ref == "tenants/tenant-acme/providers/provider-acme"
    with pytest.raises(_module().ProviderConfigurationError):
        _parse(document, scope="dedicated", tenant_id="tenant-foreign", profile_id="provider-acme")


@pytest.mark.parametrize("suffix", ("/%", "/%xx", "/%FF", "/%20", "/%00", "/%7f", "/%5c"))
def test_provider_config_rejects_url_parser_disagreement(suffix):
    document = _document()
    document["providers"]["openai"]["baseUrl"] = "https://llm.example.test" + suffix
    with pytest.raises(_module().ProviderConfigurationError):
        _parse(document)


def test_registered_provider_binding_rejects_old_shared_sentinel_without_mutation():
    module = _module()
    config = _parse()
    profile = ProviderProfileRecord(
        profile_id="platform-default",
        scope="shared",
        tenant_id=None,
        owner_key="shared",
        provider_type="openmaic-server-configured",
        model_name="server-selected-model",
        api_base_url=None,
        secret_ref=config.secret_ref,
        status="active",
    )
    before = copy.deepcopy(profile)
    with pytest.raises(module.ProviderConfigurationError):
        module.validate_registered_provider_configuration(config, profile)
    assert profile == before


@pytest.mark.parametrize(
    "changed",
    (
        "scope",
        "tenant_id",
        "owner_key",
        "profile_id",
        "status",
        "model_name",
        "api_base_url",
        "secret_ref",
    ),
)
def test_registered_provider_binding_rejects_stale_or_foreign_profile(changed):
    from dataclasses import replace

    module = _module()
    config = _parse()
    profile = ProviderProfileRecord(
        profile_id="platform-default",
        scope="shared",
        tenant_id=None,
        owner_key="shared",
        provider_type="openai",
        model_name="configured-model",
        api_base_url="https://llm.example.test/v1",
        secret_ref=config.secret_ref,
        status="active",
    )
    module.validate_registered_provider_configuration(config, profile)
    with pytest.raises(module.ProviderConfigurationError):
        module.validate_registered_provider_configuration(
            config, replace(profile, **{changed: "foreign"})
        )
