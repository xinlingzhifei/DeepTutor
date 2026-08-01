from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from deeptutor.services.config import PlatformSettings, load_platform_settings
from deeptutor.services.config import platform_settings as platform_settings_module


@pytest.fixture(autouse=True)
def _clear_platform_database_url(monkeypatch) -> None:
    monkeypatch.delenv("DEEPTUTOR_PLATFORM_DATABASE_URL", raising=False)


def _write_settings(tmp_path: Path, payload: dict[str, object]) -> Path:
    settings = tmp_path / "platform.json"
    settings.write_text(json.dumps(payload), encoding="utf-8")
    return settings


def _assert_error_does_not_expose(
    error: BaseException,
    *secret_values: str,
) -> None:
    rendered = [str(error), repr(error)]
    errors = getattr(error, "errors", None)
    if callable(errors):
        rendered.append(repr(errors()))
    as_json = getattr(error, "json", None)
    if callable(as_json):
        rendered.append(as_json())
    for chained in (error.__cause__, error.__context__):
        if chained is not None:
            rendered.extend((str(chained), repr(chained)))
    error_text = "\n".join(rendered)

    for secret_value in secret_values:
        assert secret_value not in error_text
    assert error.__cause__ is None
    assert error.__context__ is None


def test_validation_error_summary_is_compatible_with_pydantic_2_0() -> None:
    sentinel = "PYDANTIC_2_0_INPUT_MUST_NOT_LEAK"

    class Pydantic20ValidationError:
        def __init__(self) -> None:
            self.calls: list[tuple[bool, bool]] = []

        def errors(
            self,
            *,
            include_url: bool,
            include_context: bool,
        ) -> list[dict[str, object]]:
            self.calls.append((include_url, include_context))
            return [
                {
                    "loc": ("database_port",),
                    "msg": "Input should be a valid integer",
                    "input": sentinel,
                }
            ]

    error = Pydantic20ValidationError()

    message = platform_settings_module._safe_validation_error_message(error)

    assert error.calls == [(False, False)]
    assert message == "invalid platform settings: database_port"
    assert sentinel not in message


def test_disabled_platform_keeps_local_mode(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path, {"enabled": False})

    loaded = load_platform_settings(settings)

    assert loaded.enabled is False
    assert loaded.database_url is None


def test_enabled_platform_requires_database_url(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path, {"enabled": True})

    with pytest.raises(ValueError, match="database_url"):
        load_platform_settings(settings)


def test_password_file_builds_encoded_async_database_url(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "database-password"
    password_file.write_text("p@ ss:/?#[]\n", encoding="utf-8")
    settings = _write_settings(
        tmp_path,
        {
            "enabled": True,
            "database_host": "db.internal",
            "database_port": 6543,
            "database_name": "classrooms/main",
            "database_user": "tenant@owner",
            "database_password_file": str(password_file),
        },
    )
    loaded = load_platform_settings(settings)

    assert loaded.database_url is not None
    parsed = make_url(loaded.database_url.get_secret_value())
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.username == "tenant@owner"
    assert parsed.password == "p@ ss:/?#[]"
    assert parsed.host == "db.internal"
    assert parsed.port == 6543
    assert parsed.database == "classrooms/main"


def test_platform_settings_are_exported_from_config_package(
    tmp_path: Path,
) -> None:
    settings = _write_settings(tmp_path, {})

    loaded = load_platform_settings(settings)

    assert PlatformSettings().enabled is False
    assert isinstance(loaded, PlatformSettings)


def test_missing_default_settings_file_returns_disabled_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_dir = tmp_path / "data" / "user" / "settings"
    monkeypatch.setattr(
        platform_settings_module,
        "get_runtime_settings_dir",
        lambda: settings_dir,
    )
    loaded = platform_settings_module.load_platform_settings()

    assert loaded.enabled is False
    assert loaded.database_url is None
    assert loaded.database_host == "postgres"
    assert loaded.database_port == 5432
    assert loaded.database_name == "yfeistai"
    assert loaded.database_user == "yfeistai"
    assert loaded.object_store_mode == "local"
    assert loaded.object_store_bucket == "yfeistai-classrooms"
    assert loaded.object_store_region == "us-east-1"
    assert loaded.shared_generation_limit == 20
    assert loaded.default_tenant_generation_limit == 2
    assert not (settings_dir / "platform.json").exists()


@pytest.mark.parametrize(
    ("contents", "sentinel"),
    [
        (
            b'{"database_url":"postgresql+asyncpg://user:'
            b'PLATFORM_FILE_DECODE_LEAK\xff@db/yfeistai"}',
            "PLATFORM_FILE_DECODE_LEAK",
        ),
        (
            b'{"database_url":"postgresql+asyncpg://user:PLATFORM_JSON_PARSE_LEAK@db/yfeistai"',
            "PLATFORM_JSON_PARSE_LEAK",
        ),
    ],
)
def test_invalid_platform_file_does_not_expose_contents(
    tmp_path: Path,
    contents: bytes,
    sentinel: str,
) -> None:
    settings = tmp_path / "platform.json"
    settings.write_bytes(contents)

    with pytest.raises(ValueError, match="invalid JSON or encoding") as exc_info:
        load_platform_settings(settings)

    _assert_error_does_not_expose(exc_info.value, sentinel)


def test_process_environment_database_url_overrides_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _write_settings(
        tmp_path,
        {
            "enabled": True,
            "database_url": "postgresql+asyncpg://json.invalid/yfeistai",
        },
    )
    expected = "postgresql+asyncpg://runtime.invalid/yfeistai"
    monkeypatch.setenv("DEEPTUTOR_PLATFORM_DATABASE_URL", expected)

    loaded = load_platform_settings(settings)

    assert loaded.database_url is not None
    assert loaded.database_url.get_secret_value() == expected


@pytest.mark.parametrize(
    "invalid_url",
    [
        "",
        "   ",
        "not-a-url",
        "mysql+asyncpg://user:INVALID_DATABASE_URL_LEAK@db/yfeistai",
        "postgresql+asyncpg:///yfeistai",
        "postgresql+asyncpg://not a host/yfeistai",
        "postgresql+asyncpg://db/",
        "postgresql+asyncpg://db:not-a-port/yfeistai",
    ],
)
def test_invalid_environment_database_url_fails_closed(
    tmp_path: Path,
    monkeypatch,
    invalid_url: str,
) -> None:
    json_sentinel = "JSON_URL_MUST_NOT_LEAK"
    json_database_url = f"postgresql+asyncpg://user:{json_sentinel}@db/yfeistai"
    settings = _write_settings(
        tmp_path,
        {
            "enabled": True,
            "database_url": json_database_url,
        },
    )
    monkeypatch.setenv("DEEPTUTOR_PLATFORM_DATABASE_URL", invalid_url)

    with pytest.raises(ValueError, match="valid postgresql\\+asyncpg URL") as exc_info:
        load_platform_settings(settings)

    _assert_error_does_not_expose(
        exc_info.value,
        "INVALID_DATABASE_URL_LEAK",
        json_sentinel,
        json_database_url,
    )


def test_invalid_json_database_url_fails_closed_without_exposing_input(
    tmp_path: Path,
) -> None:
    sentinel = "INVALID_JSON_DATABASE_URL_LEAK"
    database_url = f"postgresql+asyncpg://user:{sentinel}@/yfeistai"
    settings = _write_settings(
        tmp_path,
        {
            "enabled": True,
            "database_url": database_url,
        },
    )
    with pytest.raises(ValueError, match="valid postgresql\\+asyncpg URL") as exc_info:
        load_platform_settings(settings)

    _assert_error_does_not_expose(exc_info.value, sentinel, database_url)


@pytest.mark.parametrize("source", ["environment", "json"])
def test_database_url_is_redacted_from_validation_errors(
    tmp_path: Path,
    monkeypatch,
    source: str,
) -> None:
    sentinel = f"{source.upper()}_TOPSECRET_SENTINEL"
    database_url = f"postgresql+asyncpg://user:{sentinel}@db/yfeistai"
    payload: dict[str, object] = {
        "enabled": True,
        "object_store_mode": "s3",
    }
    if source == "environment":
        monkeypatch.setenv("DEEPTUTOR_PLATFORM_DATABASE_URL", database_url)
    else:
        payload["database_url"] = database_url
    settings = _write_settings(tmp_path, payload)

    with pytest.raises(ValueError, match="S3 endpoint") as exc_info:
        load_platform_settings(settings)

    _assert_error_does_not_expose(exc_info.value, sentinel, database_url)


@pytest.mark.parametrize(
    ("payload", "error_match", "sentinel"),
    [
        (
            {
                "enabled": False,
                "databse_url": ("postgresql+asyncpg://user:TYPO_DATABASE_URL_LEAK@db/yfeistai"),
            },
            "databse_url",
            "TYPO_DATABASE_URL_LEAK",
        ),
        (
            {
                "enabled": True,
                "database_url": {"password": "DATABASE_URL_TYPE_LEAK"},
            },
            "database_url",
            "DATABASE_URL_TYPE_LEAK",
        ),
        (
            {
                "enabled": False,
                "database_port": {"password": "OTHER_FIELD_TYPE_LEAK"},
            },
            "database_port",
            "OTHER_FIELD_TYPE_LEAK",
        ),
    ],
)
def test_invalid_setting_is_rejected_without_exposing_raw_input(
    tmp_path: Path,
    payload: dict[str, object],
    error_match: str,
    sentinel: str,
) -> None:
    settings = _write_settings(tmp_path, payload)

    with pytest.raises(ValueError, match=error_match) as exc_info:
        load_platform_settings(settings)

    _assert_error_does_not_expose(exc_info.value, sentinel)


@pytest.mark.parametrize(
    ("invalid_settings", "error_match"),
    [
        ({"object_store_endpoint": None}, "S3 endpoint"),
        ({"object_store_tenant_credentials_dir": None}, "tenant credentials directory"),
        ({"object_store_tenant_credentials_dir": ""}, "tenant credentials directory"),
        ({"object_store_tenant_credentials_dir": "."}, "tenant credentials directory"),
        (
            {"object_store_tenant_credentials_dir": "relative/tenant-secrets"},
            "tenant credentials directory",
        ),
        ({"object_store_endpoint": " \t "}, "S3 endpoint"),
        ({"object_store_bucket": " \t "}, "bucket"),
    ],
)
def test_enabled_s3_rejects_unusable_storage_settings_without_exposing_input(
    tmp_path: Path,
    invalid_settings: dict[str, object],
    error_match: str,
) -> None:
    sentinel = "S3_ERROR_INPUT_LEAK"
    settings = _write_settings(
        tmp_path,
        {
            "enabled": True,
            "database_url": "postgresql+asyncpg://db/yfeistai",
            "object_store_mode": "s3",
            "object_store_endpoint": "http://minio:9000",
            "object_store_bucket": "yfeistai-classrooms",
            "object_store_tenant_credentials_dir": str(tmp_path / "tenant-secrets"),
            "openmaic_service_secret_file": sentinel,
            **invalid_settings,
        },
    )
    with pytest.raises(ValueError, match=error_match) as exc_info:
        load_platform_settings(settings)

    _assert_error_does_not_expose(exc_info.value, sentinel)


def test_enabled_s3_accepts_tenant_credentials_root(tmp_path: Path) -> None:
    credentials_dir = tmp_path / "tenant-secrets"
    settings = _write_settings(
        tmp_path,
        {
            "enabled": True,
            "database_url": "postgresql+asyncpg://db/yfeistai",
            "object_store_mode": "s3",
            "object_store_endpoint": "http://minio:9000",
            "object_store_tenant_credentials_dir": str(credentials_dir),
        },
    )
    loaded = load_platform_settings(settings)

    assert loaded.object_store_endpoint == "http://minio:9000"
    assert loaded.object_store_tenant_credentials_dir == credentials_dir


@pytest.mark.parametrize(
    "origin",
    [
        "http://downloads.example",
        "https://user@downloads.example",
        "https://downloads.example/path",
    ],
)
def test_public_download_origin_must_be_a_bare_https_origin(
    tmp_path: Path,
    origin: str,
) -> None:
    settings = _write_settings(
        tmp_path,
        {"object_store_public_download_origins": [origin]},
    )

    with pytest.raises(ValueError, match="invalid platform settings"):
        load_platform_settings(settings)


def test_disabled_platform_ignores_incomplete_s3_settings(tmp_path: Path) -> None:
    settings = _write_settings(
        tmp_path,
        {
            "enabled": False,
            "object_store_mode": "s3",
            "object_store_endpoint": " ",
            "object_store_bucket": "",
            "object_store_tenant_credentials_dir": "",
        },
    )
    loaded = load_platform_settings(settings)

    assert loaded.enabled is False
    assert loaded.database_url is None


@pytest.mark.parametrize(
    ("contents", "sentinel"),
    [
        (None, "MISSING_PASSWORD_PATH_LEAK"),
        (b"\xffPASSWORD_FILE_CONTENT_LEAK", "PASSWORD_FILE_CONTENT_LEAK"),
    ],
)
def test_password_file_read_failure_is_secret_safe(
    tmp_path: Path,
    contents: bytes | None,
    sentinel: str,
) -> None:
    password_file = tmp_path / (sentinel if contents is None else "database-password")
    if contents is not None:
        password_file.write_bytes(contents)
    settings = _write_settings(
        tmp_path,
        {
            "enabled": True,
            "database_password_file": str(password_file),
        },
    )
    with pytest.raises(ValueError, match="password secret could not be read") as exc_info:
        load_platform_settings(settings)

    _assert_error_does_not_expose(exc_info.value, sentinel)


@pytest.mark.parametrize(
    ("overrides", "error_match", "sentinel"),
    [
        (
            {"database_host": ""},
            "valid postgresql\\+asyncpg URL",
            "CONSTRUCTED_DATABASE_URL_PASSWORD_LEAK",
        ),
        (
            {"database_name": "q?ssl=require"},
            "without changing connection fields",
            "DATABASE_NAME_INJECTION_PASSWORD_LEAK",
        ),
    ],
)
def test_invalid_constructed_database_url_does_not_expose_password(
    tmp_path: Path,
    overrides: dict[str, object],
    error_match: str,
    sentinel: str,
) -> None:
    password_file = tmp_path / "database-password"
    password_file.write_text(sentinel, encoding="utf-8")
    settings = _write_settings(
        tmp_path,
        {
            "enabled": True,
            "database_password_file": str(password_file),
            **overrides,
        },
    )
    with pytest.raises(ValueError, match=error_match) as exc_info:
        load_platform_settings(settings)

    _assert_error_does_not_expose(exc_info.value, sentinel)
