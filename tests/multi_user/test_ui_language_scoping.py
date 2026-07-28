"""M1 regression — interface_settings.py must resolve per-user, not at import."""

from __future__ import annotations

import json

from deeptutor.services.settings.interface_settings import (
    get_ui_language,
    get_ui_settings,
)


def test_get_ui_language_reads_per_user_interface_json(mu_isolated_root, as_user):
    # Admin's interface.json says English…
    admin_settings = mu_isolated_root / "data" / "user" / "settings" / "interface.json"
    admin_settings.parent.mkdir(parents=True, exist_ok=True)
    admin_settings.write_text(json.dumps({"theme": "light", "language": "en"}))

    # …while alice has chosen Chinese in her own scope.
    alice_settings = (
        mu_isolated_root / "data" / "users" / "u_alice" / "user" / "settings" / "interface.json"
    )
    alice_settings.parent.mkdir(parents=True, exist_ok=True)
    alice_settings.write_text(json.dumps({"theme": "dark", "language": "zh"}))

    with as_user("u_admin", role="admin"):
        assert get_ui_language() == "en"
        assert get_ui_settings()["theme"] == "light"

    with as_user("u_alice", role="user"):
        assert get_ui_language() == "zh"
        assert get_ui_settings()["theme"] == "dark"


def test_get_ui_language_defaults_when_no_file(mu_isolated_root, as_user):
    with as_user("u_alice", role="user"):
        # Alice has nothing on disk yet — the product default is Chinese.
        assert get_ui_language() == "zh"


def test_get_ui_language_normalizes_invalid_saved_value_to_chinese(
    mu_isolated_root, as_user
):
    settings_file = (
        mu_isolated_root
        / "data"
        / "users"
        / "u_alice"
        / "user"
        / "settings"
        / "interface.json"
    )
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({"language": "unsupported"}))

    with as_user("u_alice", role="user"):
        assert get_ui_language() == "zh"
