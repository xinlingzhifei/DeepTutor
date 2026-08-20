from __future__ import annotations

import asyncio
from functools import cache
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from deeptutor.teaching.secret_permissions import secret_file_is_restricted

ROOT = Path(__file__).resolve().parents[2]


@cache
def _script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "script_name",
    (
        "init_platform_secrets.py",
        "migrate_teaching.py",
        "platform_preflight.py",
        "provision_tenant_storage.py",
    ),
)
def test_platform_scripts_start_directly_from_repository_root(
    script_name: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_secret_initializer_never_overwrites_existing_secret(tmp_path: Path) -> None:
    initialize_secret = _script("init_platform_secrets").initialize_secret

    target = tmp_path / "openmaic_service_secret"
    target.write_text("keep-me", encoding="utf-8")

    created = initialize_secret(target, bytes_count=32)

    assert created is False
    assert target.read_text(encoding="utf-8") == "keep-me"


def test_secret_initializer_never_publishes_partial_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _script("init_platform_secrets")
    target = tmp_path / "openmaic_service_secret"
    original_write = module.os.write
    writes = 0

    def interrupted_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(descriptor, payload[:3])
        raise OSError("simulated interrupted secret write")

    monkeypatch.setattr(module.os, "write", interrupted_write)

    with pytest.raises(OSError, match="simulated interrupted secret write"):
        module.initialize_secret(target, bytes_count=32)

    assert not target.exists()
    assert list(tmp_path.glob(".openmaic_service_secret.*.tmp")) == []


def test_platform_secret_initializer_creates_only_generated_secrets(
    tmp_path: Path,
    capsys,
) -> None:
    module = _script("init_platform_secrets")
    GENERATED_SECRET_SPECS = module.GENERATED_SECRET_SPECS
    initialize_platform_secrets = module.initialize_platform_secrets

    created = initialize_platform_secrets(tmp_path)

    assert {path.name for path in created} == set(GENERATED_SECRET_SPECS)
    assert not (tmp_path / "gateway_fullchain.pem").exists()
    assert not (tmp_path / "gateway_private_key.pem").exists()
    assert capsys.readouterr().out == ""
    for path in created:
        assert path.read_text(encoding="utf-8").strip()
        assert secret_file_is_restricted(path)
    minio_secret = (tmp_path / "minio_bootstrap_secret_key").read_text(encoding="utf-8").strip()
    assert 8 <= len(minio_secret) <= 40


def test_database_role_bootstrap_keeps_passwords_out_of_sql() -> None:
    build_database_role_statements = _script("migrate_teaching").build_database_role_statements

    app_password = "APP_PASSWORD_SENTINEL"
    migration_password = "MIGRATION_PASSWORD_SENTINEL"
    statements = build_database_role_statements(
        app_password=app_password,
        migration_password=migration_password,
    )

    rendered = "\n".join(statement.sql for statement in statements)
    assert app_password not in rendered
    assert migration_password not in rendered
    assert "yfeistai_app" in rendered
    assert "yfeistai_migrator" in rendered
    assert all("SUPERUSER" not in statement.sql for statement in statements)
    assert any(statement.parameters.get("password") == app_password for statement in statements)
    assert any(
        statement.parameters.get("password") == migration_password for statement in statements
    )


def test_tenant_migration_failure_is_safe_and_stops_later_tenants() -> None:
    module = _script("migrate_teaching")
    TenantMigrationError = module.TenantMigrationError
    migrate_tenant_schemas = module.migrate_tenant_schemas

    calls: list[str] = []

    async def migrate(schema_name: str) -> None:
        calls.append(schema_name)
        if schema_name.endswith("2" * 16):
            raise RuntimeError("PASSWORD_SENTINEL")

    try:
        asyncio.run(
            migrate_tenant_schemas(
                (
                    ("tenant-a", "tenant_" + "1" * 16, "old-a"),
                    ("tenant-b", "tenant_" + "2" * 16, "old-b"),
                    ("tenant-c", "tenant_" + "3" * 16, "old-c"),
                ),
                migrate=migrate,
            )
        )
    except TenantMigrationError as exc:
        message = str(exc)
    else:
        raise AssertionError("tenant migration failure was not propagated")

    assert calls == ["tenant_" + "1" * 16, "tenant_" + "2" * 16]
    assert "tenant-b" in message
    assert "tenant_" + "2" * 16 in message
    assert "old-b" in message
    assert "PASSWORD_SENTINEL" not in message
