"""Platform runtime settings backed by ``data/user/settings/platform.json``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
    model_validator,
)

from .loader import get_runtime_settings_dir


class PlatformSettings(BaseModel):
    """Typed configuration for the optional multi-tenant platform runtime.

    Direct construction and Pydantic's native validation APIs are intended for
    trusted, already-typed data. Untrusted JSON or environment payloads must go
    through :func:`load_platform_settings`, which provides the secret-safe
    validation boundary.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    database_url: SecretStr | None = None
    database_host: str = "postgres"
    database_port: int = 5432
    database_name: str = "yfeistai"
    database_user: str = "yfeistai"
    database_password_file: Path | None = None
    object_store_mode: Literal["local", "s3"] = "local"
    object_store_endpoint: str | None = None
    object_store_namespace_id: str | None = None
    object_store_bucket: str = "yfeistai-classrooms"
    object_store_region: str = "us-east-1"
    object_store_tenant_credentials_dir: Path | None = None
    object_store_public_download_origins: tuple[str, ...] = ()
    classroom_ticket_secret_file: Path | None = None
    openmaic_service_secret_file: Path | None = None
    shared_generation_limit: int = 20
    default_tenant_generation_limit: int = 2

    @model_validator(mode="after")
    def validate_enabled_runtime(self) -> "PlatformSettings":
        if self.enabled and self.database_url is None and self.database_password_file is None:
            raise ValueError(
                "platform database_url or database_password_file is required when enabled"
            )
        namespace_id = self.object_store_namespace_id
        if self.enabled and self.object_store_mode == "s3":
            endpoint = self.object_store_endpoint
            bucket = self.object_store_bucket
            credentials_dir = self.object_store_tenant_credentials_dir
            if (
                endpoint is None
                or not endpoint.strip()
                or namespace_id is None
                or not namespace_id
                or not bucket.strip()
                or credentials_dir is None
                or not credentials_dir.is_absolute()
            ):
                raise ValueError(
                    "S3 endpoint, stable namespace ID, bucket, and absolute tenant credentials "
                    "directory are required"
                )
        for origin in self.object_store_public_download_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("object store public download origins must be HTTPS origins")
        if namespace_id is not None and (
            not namespace_id
            or len(namespace_id) > 128
            or not namespace_id.isascii()
            or not namespace_id[0].isalnum()
            or any(not (character.isalnum() or character in "._:-") for character in namespace_id)
        ):
            raise ValueError("object_store_namespace_id is invalid")
        if (
            self.classroom_ticket_secret_file is not None
            and not self.classroom_ticket_secret_file.is_absolute()
        ):
            raise ValueError("classroom_ticket_secret_file must be an absolute path")
        return self


def _database_url_from_password_file(settings: PlatformSettings) -> SecretStr:
    from sqlalchemy.engine import URL, make_url
    from sqlalchemy.exc import ArgumentError

    password_file = settings.database_password_file
    if password_file is None:
        raise ValueError("platform database password secret file is required")
    password = ""
    read_failed = False
    try:
        password = password_file.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError):
        read_failed = True
    if read_failed:
        raise ValueError("platform database password secret could not be read")
    if not password:
        raise ValueError("platform database password secret must not be empty")

    rendered_url: str | None = None
    round_trip_ok = False
    try:
        constructed = URL.create(
            drivername="postgresql+asyncpg",
            username=settings.database_user,
            password=password,
            host=settings.database_host,
            port=settings.database_port,
            database=settings.database_name,
        )
        rendered_url = constructed.render_as_string(hide_password=False)
        parsed = make_url(rendered_url)
        round_trip_ok = (
            parsed.drivername == "postgresql+asyncpg"
            and parsed.username == settings.database_user
            and parsed.password == password
            and parsed.host == settings.database_host
            and parsed.port == settings.database_port
            and parsed.database == settings.database_name
            and not parsed.query
        )
    except (ArgumentError, TypeError, UnicodeError, ValueError):
        rendered_url = None
    if rendered_url is None or not round_trip_ok:
        raise ValueError(
            "platform database_url could not be constructed as a valid "
            "postgresql+asyncpg URL without changing connection fields"
        )
    return SecretStr(rendered_url)


def _safe_validation_error_message(error: ValidationError) -> str:
    issues: list[str] = []
    for detail in error.errors(
        include_url=False,
        include_context=False,
    ):
        location = detail.get("loc", ())
        if location:
            issues.append(".".join(str(part) for part in location))
            continue

        message = str(detail.get("msg", ""))
        if "database_url or database_password_file" in message:
            issues.append("database_url or database_password_file")
        elif "S3 endpoint" in message:
            issues.append(
                "S3 endpoint, stable namespace ID, bucket, and absolute tenant credentials "
                "directory"
            )
        elif "classroom_ticket_secret_file" in message:
            issues.append("classroom_ticket_secret_file")
        else:
            issues.append("model")

    unique_issues = list(dict.fromkeys(issues))
    if not unique_issues:
        return "invalid platform settings"
    return f"invalid platform settings: {', '.join(unique_issues)}"


def _validate_platform_settings_payload(
    payload: dict[str, Any],
) -> PlatformSettings:
    prepared = dict(payload)
    unknown_keys = set(prepared).difference(PlatformSettings.model_fields)
    if unknown_keys:
        names = ", ".join(sorted(unknown_keys))
        raise ValueError(f"unknown platform settings: {names}")

    raw_database_url = prepared.get("database_url")
    if raw_database_url is not None and not isinstance(
        raw_database_url,
        (str, SecretStr),
    ):
        raise ValueError("platform database_url must be a string")
    if isinstance(raw_database_url, str):
        prepared["database_url"] = SecretStr(raw_database_url)

    settings: PlatformSettings | None = None
    validation_error_message: str | None = None
    try:
        settings = PlatformSettings.model_validate(prepared)
    except ValidationError as exc:
        validation_error_message = _safe_validation_error_message(exc)
    if validation_error_message is not None:
        raise ValueError(validation_error_message)
    if settings is None:
        raise ValueError("invalid platform settings")
    return settings


def _is_valid_database_url(database_url: SecretStr) -> bool:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        parsed = make_url(database_url.get_secret_value())
        port = parsed.port
    except (ArgumentError, TypeError, UnicodeError, ValueError):
        return False

    host = parsed.host
    database = parsed.database
    return (
        parsed.drivername == "postgresql+asyncpg"
        and host is not None
        and bool(host.strip())
        and not any(character.isspace() for character in host)
        and database is not None
        and bool(database.strip())
        and (port is None or 1 <= port <= 65535)
    )


def _read_platform_payload(path: Path) -> dict[str, Any]:
    contents: str | None = None
    read_error: str | None = None
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        read_error = "platform settings file could not be read"
    except UnicodeError:
        read_error = "platform settings file contains invalid JSON or encoding"
    if read_error is not None:
        raise ValueError(read_error)
    if contents is None:
        raise ValueError("platform settings file could not be read")

    loaded: Any = None
    invalid_json = False
    try:
        loaded = json.loads(contents)
    except json.JSONDecodeError:
        invalid_json = True
    if invalid_json:
        raise ValueError("platform settings file contains invalid JSON or encoding")
    if not isinstance(loaded, dict):
        raise ValueError("platform settings must be a JSON object")
    return loaded


def load_platform_settings(path: Path | None = None) -> PlatformSettings:
    """Safely load untrusted platform settings without reading root ``.env``.

    This loader and its private validation factory are the supported
    secret-safe boundary for raw JSON and process-environment configuration.
    """

    settings_path = path or get_runtime_settings_dir() / "platform.json"
    payload = _read_platform_payload(settings_path)

    database_url = os.environ.get("DEEPTUTOR_PLATFORM_DATABASE_URL")
    if database_url is not None:
        payload["database_url"] = database_url
    settings = _validate_platform_settings_payload(payload)

    if (
        settings.enabled
        and settings.database_url is None
        and settings.database_password_file is not None
    ):
        settings = settings.model_copy(
            update={"database_url": _database_url_from_password_file(settings)}
        )
    if (
        settings.enabled
        and settings.database_url is not None
        and not _is_valid_database_url(settings.database_url)
    ):
        raise ValueError("platform database_url must be a valid postgresql+asyncpg URL")
    return settings
