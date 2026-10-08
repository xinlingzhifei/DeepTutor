from pathlib import Path

from alembic.util import CommandError
from pydantic import SecretStr
import pytest
from sqlalchemy.engine import make_url

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.migrations import runner


@pytest.fixture
def app_settings(monkeypatch):
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://yfeistai_app:app-secret@db:5432/platform"),
    )
    monkeypatch.setattr(runner, "load_platform_settings", lambda: settings)
    monkeypatch.delenv("YFEISTAI_PLATFORM_MIGRATION_USER", raising=False)
    monkeypatch.delenv("YFEISTAI_PLATFORM_MIGRATION_PASSWORD_FILE", raising=False)
    return settings


def test_migration_uses_dedicated_credentials_without_changing_app_settings(
    app_settings, monkeypatch, tmp_path: Path
):
    password = "migration:@/ secret!"
    secret = tmp_path / "migration-password"
    secret.write_text(password + "\n", encoding="utf-8")
    monkeypatch.setenv("YFEISTAI_PLATFORM_MIGRATION_USER", "yfeistai_migrator")
    monkeypatch.setenv("YFEISTAI_PLATFORM_MIGRATION_PASSWORD_FILE", str(secret))

    url = make_url(runner.load_migration_database_url())

    assert url.username == "yfeistai_migrator"
    assert url.password == password
    assert (url.host, url.port, url.database) == ("db", 5432, "platform")
    assert make_url(app_settings.database_url.get_secret_value()).username == "yfeistai_app"


def test_migration_retains_explicit_database_url_without_dedicated_credentials(app_settings):
    assert runner.load_migration_database_url() == app_settings.database_url.get_secret_value()


@pytest.mark.parametrize("mode", ["missing_user", "missing_file", "unreadable", "empty"])
def test_invalid_dedicated_credentials_fail_closed(app_settings, monkeypatch, tmp_path, mode):
    secret = tmp_path / "migration-password"
    if mode != "missing_user":
        monkeypatch.setenv("YFEISTAI_PLATFORM_MIGRATION_USER", "yfeistai_migrator")
    if mode != "missing_file":
        monkeypatch.setenv("YFEISTAI_PLATFORM_MIGRATION_PASSWORD_FILE", str(secret))
    if mode != "unreadable":
        secret.write_text("\n" if mode == "empty" else "migration-secret", encoding="utf-8")

    with pytest.raises(CommandError) as error:
        runner.load_migration_database_url()
    assert "migration-secret" not in str(error.value)
    assert "app-secret" not in str(error.value)
    assert str(secret) not in str(error.value)
