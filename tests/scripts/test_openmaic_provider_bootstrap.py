"""Exercise the production preload in a real, credential-free Node process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "integrations/openmaic/runtime/provider-bootstrap.cjs"
CANARY = "fixture-provider-key-never-log-this"


def _configuration():
    return {
        "scope": "shared",
        "tenantId": None,
        "profileId": "platform-default",
        "defaultModel": "openai:fixture-model",
        "providers": {"openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid/v1"}},
    }


def _environment():
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }
    return {
        **environment,
        "YFEISTAI_OPENMAIC_MODE": "shared",
        "YFEISTAI_OPENMAIC_PROVIDER_PROFILE_ID": "platform-default",
    }


def _run(tmp_path, document=None, *, raw=None, env=None, mode=0o400, missing=False):
    assert BOOTSTRAP.is_file(), "production provider bootstrap is missing"
    node = shutil.which("node")
    assert node, "This contract requires a real Node runtime"
    config = tmp_path / "server-providers.yml"
    if not missing:
        content = raw if raw is not None else json.dumps(document or _configuration())
        config.write_text(content, encoding="utf-8")
        config.chmod(mode)
    # No provider request, credential echo or application implementation mock:
    # this sentinel runs only after the real preload has admitted startup.
    return subprocess.run(
        [
            node,
            "--require",
            str(BOOTSTRAP),
            "-e",
            "console.log('STARTED:'+process.env.DEFAULT_MODEL)",
        ],
        cwd=tmp_path,
        env=_environment() if env is None else env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _assert_rejected(result):
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "YFEISTAI_PROVIDER_CONFIG_INVALID\n"
    assert CANARY not in result.stdout + result.stderr


def test_real_node_bootstrap_selects_model_from_one_protected_file(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "STARTED:openai:fixture-model\n"
    assert result.stderr == ""


def test_real_node_bootstrap_binds_dedicated_scope_and_preserves_model_colons(tmp_path):
    document = _configuration()
    document.update(scope="dedicated", tenantId="tenant-a", profileId="profile-a")
    document["defaultModel"] = "openai:vendor/model:version"
    environment = _environment()
    environment.update(
        YFEISTAI_OPENMAIC_MODE="dedicated",
        YFEISTAI_DATA_PLANE_ID="tenant-a",
        YFEISTAI_OPENMAIC_PROVIDER_PROFILE_ID="profile-a",
    )
    result = _run(tmp_path, document, env=environment)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "STARTED:openai:vendor/model:version\n"


@pytest.mark.parametrize("provider", ["ollama", "lemonade"])
def test_real_node_bootstrap_accepts_only_explicit_upstream_keyless_providers(tmp_path, provider):
    document = _configuration()
    document["defaultModel"] = f"{provider}:fixture-model"
    document["providers"] = {provider: {"apiKey": "", "baseUrl": "http://model.internal:11434"}}
    result = _run(tmp_path, document)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "dedicated"},
        {"tenantId": "foreign-tenant"},
        {"profileId": "another-profile"},
        {"defaultModel": ""},
        {"defaultModel": "openai/fixture-model"},
        {"defaultModel": "openai:" + "a" * 129},
        {"defaultModel": "unknown:fixture-model"},
        {"defaultModel": "openai:fixture\nmodel"},
        {"tts": {"another-provider": {"apiKey": CANARY}}},
        {"providers": {"openai": {"apiKey": "", "baseUrl": "https://provider.invalid/v1"}}},
        {
            "providers": {
                "openai": {"apiKey": CANARY + "\n", "baseUrl": "https://provider.invalid/v1"}
            }
        },
        {
            "providers": {
                "openai": {"apiKey": CANARY, "baseUrl": "https://user:pass@provider.invalid"}
            }
        },
        {"providers": {"openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid/v1/"}}},
        {
            "providers": {
                "openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid/a/../v1"}
            }
        },
        {
            "providers": {
                "openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid/%2e%2e/v1"}
            }
        },
        {
            "providers": {
                "openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid/v1?key=x"}
            }
        },
        {"providers": {"openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid:0/v1"}}},
        {"providers": {"openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid:/v1"}}},
        {"providers": {"openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid/%zz"}}},
        {"providers": {"openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid/%00"}}},
        {
            "providers": {
                "openai": {
                    "apiKey": CANARY,
                    "baseUrl": "https://provider.invalid/v1",
                    "proxy": "http://unbound.invalid",
                }
            }
        },
        {
            "providers": {
                "openai": {"apiKey": CANARY, "baseUrl": "https://provider.invalid/v1"},
                "deepseek": {"apiKey": CANARY, "baseUrl": "https://other.invalid"},
            }
        },
    ],
)
def test_real_node_bootstrap_fails_closed_without_logging_secret(tmp_path, change):
    document = _configuration()
    document.update(change)
    _assert_rejected(_run(tmp_path, document))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("YFEISTAI_OPENMAIC_MODE", ""),
        ("YFEISTAI_OPENMAIC_PROVIDER_PROFILE_ID", "foreign-profile"),
        ("YFEISTAI_DATA_PLANE_ID", "foreign-tenant"),
        ("DEFAULT_MODEL", "deepseek:other"),
        ("MODEL_ROUTES", '{"generate-classroom":"other"}'),
        ("OPENAI_API_KEY", CANARY),
        ("DEEPSEEK_API_KEY", CANARY),
        ("OPENAI_BASE_URL", "https://unbound.invalid"),
        ("OPENAI_MODELS", "other"),
        ("ALIDOCMIND_ACCESS_KEY_SECRET", CANARY),
    ],
)
def test_real_node_bootstrap_rejects_environment_overrides(tmp_path, name, value):
    environment = _environment()
    environment[name] = value
    _assert_rejected(_run(tmp_path, env=environment))


@pytest.mark.parametrize(
    "raw",
    [
        '{"apiKey":"' + CANARY + '",broken}',
        "\ufeff" + json.dumps(_configuration()),
        json.dumps(_configuration()).replace(
            '"scope": "shared"', '"scope":"shared","scope":"shared"'
        ),
        json.dumps(_configuration()).replace(
            '"scope": "shared"', '"scope":"shared","sco\\u0070e":"shared"'
        ),
        json.dumps(_configuration()).replace('"apiKey":', '"apiKey":"discarded","apiKey":'),
        " " * 65_537 + json.dumps(_configuration()),
    ],
    ids=["malformed", "bom", "duplicate", "escaped-duplicate", "nested-duplicate", "oversize"],
)
def test_real_node_bootstrap_rejects_ambiguous_json_before_upstream_yaml_loader(tmp_path, raw):
    _assert_rejected(_run(tmp_path, raw=raw))


def test_real_node_bootstrap_requires_the_config_file(tmp_path):
    _assert_rejected(_run(tmp_path, missing=True))


@pytest.mark.parametrize(
    "key",
    ["\u0085" + CANARY, CANARY + "\u0085", "\ufeff" + CANARY, CANARY + "\ufeff"],
    ids=["nel-prefix", "nel-suffix", "bom-prefix", "bom-suffix"],
)
def test_real_node_bootstrap_rejects_cross_language_trim_ambiguity(tmp_path, key):
    document = _configuration()
    document["providers"]["openai"]["apiKey"] = key
    _assert_rejected(_run(tmp_path, document))


@pytest.mark.skipif(sys.platform != "linux", reason="Requires native Linux DAC and symlinks")
@pytest.mark.parametrize("mode", [0o444, 0o640, 0o644, 0o700])
def test_real_node_bootstrap_rejects_unsafe_posix_permissions(tmp_path, mode):
    _assert_rejected(_run(tmp_path, mode=mode))


@pytest.mark.skipif(sys.platform != "linux", reason="Requires native Linux symlinks")
def test_real_node_bootstrap_rejects_symlink(tmp_path):
    target = tmp_path / "different-secret"
    target.write_text(json.dumps(_configuration()), encoding="utf-8")
    target.chmod(0o400)
    (tmp_path / "server-providers.yml").symlink_to(target)
    _assert_rejected(_run(tmp_path, missing=True))
