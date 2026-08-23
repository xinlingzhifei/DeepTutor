from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import cache
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.secret_permissions import restrict_secret_file

ROOT = Path(__file__).resolve().parents[2]


@cache
def _module():
    path = ROOT / "scripts" / "platform_preflight.py"
    spec = importlib.util.spec_from_file_location("platform_preflight_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_tls_pair(
    root: Path,
    *,
    hostname: str,
    expires_in: timedelta,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + expires_in)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = root / "gateway_fullchain.pem"
    private_key_path = root / "gateway_private_key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    restrict_secret_file(certificate_path)
    restrict_secret_file(private_key_path)


def test_preflight_rejects_world_readable_secret(tmp_path: Path) -> None:
    run_preflight = _module().run_preflight

    target = tmp_path / "classroom_ticket_secret"
    target.write_text("secret", encoding="utf-8")
    if os.name == "nt":
        import ntsecuritycon
        import win32security

        everyone = win32security.ConvertStringSidToSid("S-1-1-0")
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_GENERIC_READ,
            everyone,
        )
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorDacl(True, dacl, False)
        win32security.SetFileSecurity(
            str(target),
            win32security.DACL_SECURITY_INFORMATION,
            descriptor,
        )
    else:
        target.chmod(0o644)

    result = run_preflight(
        secret_dir=tmp_path,
        required_secret_names=("classroom_ticket_secret",),
    )

    assert "classroom_ticket_secret permissions" in result.errors


def test_preflight_rejects_missing_operator_tls_instead_of_generating_it(
    tmp_path: Path,
) -> None:
    run_preflight = _module().run_preflight

    result = run_preflight(
        secret_dir=tmp_path,
        required_secret_names=(),
        tls_hostname="classroom.example.com",
    )

    assert "gateway TLS certificate" in result.errors
    assert "gateway TLS private key" in result.errors
    assert not (tmp_path / "gateway_fullchain.pem").exists()
    assert not (tmp_path / "gateway_private_key.pem").exists()


def test_preflight_accepts_matching_operator_tls_with_safe_validity(
    tmp_path: Path,
) -> None:
    _write_tls_pair(
        tmp_path,
        hostname="classroom.example.com",
        expires_in=timedelta(days=30),
    )

    result = _module().run_preflight(
        secret_dir=tmp_path,
        required_secret_names=(),
        tls_hostname="classroom.example.com",
    )

    assert not [error for error in result.errors if error.startswith("gateway TLS")]


def test_preflight_rejects_tls_with_less_than_fourteen_days_remaining(
    tmp_path: Path,
) -> None:
    _write_tls_pair(
        tmp_path,
        hostname="classroom.example.com",
        expires_in=timedelta(days=13),
    )

    result = _module().run_preflight(
        secret_dir=tmp_path,
        required_secret_names=(),
        tls_hostname="classroom.example.com",
    )

    assert "gateway TLS expiry" in result.errors


def test_preflight_rejects_zero_or_unbound_image_digests(tmp_path: Path) -> None:
    run_preflight = _module().run_preflight

    image_lock = tmp_path / "image-lock.json"
    image_lock.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "images": {
                    "deeptutor": {
                        "digest": "sha256:" + "0" * 64,
                        "reference": "ghcr.io/example/deeptutor:first-release",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = run_preflight(
        secret_dir=tmp_path,
        required_secret_names=(),
        image_lock_path=image_lock,
    )

    assert "image lock digest" in result.errors
    assert "image lock reference" in result.errors


def test_preflight_rejects_capacity_settings_outside_release_contract(
    tmp_path: Path,
) -> None:
    run_preflight = _module().run_preflight

    platform = tmp_path / "platform.json"
    platform.write_text(
        json.dumps(
            {
                "enabled": False,
                "shared_generation_limit": 19,
                "default_tenant_generation_limit": 3,
            }
        ),
        encoding="utf-8",
    )

    result = run_preflight(
        secret_dir=tmp_path,
        required_secret_names=(),
        platform_config_path=platform,
    )

    assert "shared generation slots" in result.errors
    assert "tenant generation slots" in result.errors


def test_runtime_preflight_fails_closed_for_every_required_dependency(
    tmp_path: Path,
) -> None:
    module = _module()
    expected = (
        "database connectivity",
        "database migrations",
        "object store bucket probe",
        "active tenant credential",
        "tenant own-prefix probe",
        "tenant cross-prefix denial probe",
        "OpenMAIC health and contract 1.0",
        "Docker Compose version",
        "gateway-only public ports",
    )
    calls: list[tuple[Path, Path, Path]] = []

    class Probe:
        async def inspect(
            self,
            *,
            settings: PlatformSettings,
            secret_dir: Path,
            image_lock_path: Path,
            project_root: Path,
        ) -> tuple[str, ...]:
            assert settings.enabled is True
            calls.append((secret_dir, image_lock_path, project_root))
            return expected

    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://app:pass@db/platform"),
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_namespace_id="test-minio-primary",
        object_store_bucket="classrooms",
        object_store_tenant_credentials_dir=tmp_path / "tenant-secrets",
    )
    lock_path = tmp_path / "image-lock.json"

    errors = asyncio.run(
        module.run_runtime_preflight(
            settings=settings,
            secret_dir=tmp_path,
            image_lock_path=lock_path,
            project_root=ROOT,
            probe=Probe(),
        )
    )

    assert errors == expected
    assert calls == [(tmp_path, lock_path, ROOT)]


def test_runtime_compose_probe_rejects_old_cli_and_non_gateway_ports(
    monkeypatch,
) -> None:
    module = _module()
    monkeypatch.setenv("COMPOSE_FILE", "host-compose.yml")
    monkeypatch.setenv("COMPOSE_PROFILES", "dangerous")
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def runner(command, **kwargs):
        calls.append((tuple(command), dict(kwargs["env"])))
        if command[-2:] == ["version", "--short"]:
            return subprocess.CompletedProcess(command, 0, "2.23.0\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "services": {
                        "gateway": {"ports": [{"published": "443"}]},
                        "deeptutor": {"ports": [{"published": "8001"}]},
                    }
                }
            ),
            "",
        )

    errors = module._inspect_compose_runtime(ROOT, runner=runner)

    assert errors == ("Docker Compose version", "gateway-only public ports")
    assert len(calls) == 2
    assert all("COMPOSE_FILE" not in environment for _command, environment in calls)
    assert all("COMPOSE_PROFILES" not in environment for _command, environment in calls)


def test_runtime_compose_probe_does_not_require_packaging_dependency(
    monkeypatch,
) -> None:
    module = _module()
    monkeypatch.setitem(sys.modules, "packaging", None)
    monkeypatch.setitem(sys.modules, "packaging.version", None)

    def runner(command, **kwargs):
        del kwargs
        if command[-2:] == ["version", "--short"]:
            return subprocess.CompletedProcess(command, 0, "v2.24.4\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "services": {
                        "gateway": {
                            "ports": [
                                {"published": "80"},
                                {"published": "443"},
                            ]
                        }
                    }
                }
            ),
            "",
        )

    assert module._inspect_compose_runtime(ROOT, runner=runner) == ()
