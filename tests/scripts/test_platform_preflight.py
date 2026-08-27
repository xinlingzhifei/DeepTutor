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
import pytest

import deeptutor.services.config as config_module
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


def test_preflight_rejects_legacy_image_lock_candidate(tmp_path: Path) -> None:
    image_lock = tmp_path / "image-lock.json"
    digest = "sha256:" + "1" * 64
    image_lock.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "images": {
                    "deeptutor": {
                        "repository": "ghcr.io/xinlingzhifei/deeptutor",
                        "tag": "first-release",
                        "digest": digest,
                        "reference": ("ghcr.io/xinlingzhifei/deeptutor:first-release@" + digest),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _module().run_preflight(
        secret_dir=tmp_path,
        required_secret_names=(),
        image_lock_path=image_lock,
    )

    assert "image lock candidate" in result.errors


def test_main_rejects_legacy_image_lock_before_runtime_probes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _module()
    image_lock = tmp_path / "image-lock.json"
    image_lock.write_bytes((ROOT / "deploy" / "image-lock.json").read_bytes())
    runtime_calls: list[str] = []

    async def record_runtime_probe(**_kwargs) -> tuple[str, ...]:
        runtime_calls.append("runtime")
        return ()

    monkeypatch.setattr(module, "DEFAULT_REQUIRED_SECRETS", ())
    monkeypatch.setattr(module, "_inspect_tls", lambda *_args: ())
    monkeypatch.setattr(module, "run_runtime_preflight", record_runtime_probe)
    monkeypatch.setattr(config_module, "load_platform_settings", lambda _path: object())

    exit_code = module.main(
        [
            "--config",
            str(ROOT / "deploy" / "platform.example.json"),
            "--secret-dir",
            str(tmp_path),
            "--image-lock",
            str(image_lock),
            "--hostname",
            "classroom.example.com",
        ]
    )

    assert exit_code == 1
    assert "image lock candidate" in capsys.readouterr().out
    assert runtime_calls == []


def _write_preflight_candidate_root(tmp_path: Path) -> Path:
    renderer_path = ROOT / "scripts" / "render_platform_compose.py"
    spec = importlib.util.spec_from_file_location(
        "render_platform_compose_for_preflight_candidate_test",
        renderer_path,
    )
    assert spec and spec.loader
    renderer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = renderer
    try:
        spec.loader.exec_module(renderer)
    finally:
        sys.modules.pop(spec.name, None)
    candidate_root = tmp_path / "candidate"
    deploy_dir = candidate_root / "deploy"
    deploy_dir.mkdir(parents=True)
    compose_paths = (
        candidate_root / "docker-compose.platform.yml",
        candidate_root / "docker-compose.data-plane.yml",
    )
    for source_name, destination in zip(
        ("docker-compose.platform.yml", "docker-compose.data-plane.yml"),
        compose_paths,
        strict=True,
    ):
        destination.write_bytes((ROOT / source_name).read_bytes())
    digest_index = 0

    def resolve_digest(_reference: str) -> str:
        nonlocal digest_index
        digest_index += 1
        return "sha256:" + f"{digest_index:064x}"

    source_head = "a" * 40
    renderer.write_image_lock(
        deploy_dir / "image-lock.json",
        digest_resolver=resolve_digest,
        compose_paths=compose_paths,
        source_repository="xinlingzhifei/DeepTutor",
        source_head=source_head,
        release_tag=f"yfeistai-first-release-20260825-{source_head[:8]}",
        openmaic_head="0cf2a330411681190e89f48e20f305345ff99f87",
    )
    return candidate_root


def test_main_uses_external_candidate_root_for_lock_and_runtime_compose(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    calls: dict[str, dict[str, object]] = {}

    def record_static(**kwargs) -> object:
        calls["static"] = kwargs
        return module.PreflightResult(())

    async def record_runtime(**kwargs) -> tuple[str, ...]:
        calls["runtime"] = kwargs
        return ()

    monkeypatch.setattr(module, "run_preflight", record_static)
    monkeypatch.setattr(module, "run_runtime_preflight", record_runtime)
    monkeypatch.setattr(config_module, "load_platform_settings", lambda _path: object())

    assert (
        module.main(
            [
                "--config",
                str(ROOT / "deploy" / "platform.example.json"),
                "--secret-dir",
                str(tmp_path / "secrets"),
                "--candidate-root",
                str(candidate_root.resolve()),
                "--hostname",
                "classroom.example.com",
            ]
        )
        == 0
    )

    expected_lock = candidate_root / "deploy" / "image-lock.json"
    assert calls["static"]["image_lock_path"] == expected_lock
    assert calls["runtime"]["image_lock_path"] == expected_lock
    assert calls["runtime"]["candidate_root"] == candidate_root
    assert calls["runtime"]["project_root"] == module.PROJECT_ROOT


def test_main_rejects_mixed_candidate_root_before_runtime_probes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "run_preflight",
        lambda **_kwargs: calls.append("static") or module.PreflightResult(()),
    )
    monkeypatch.setattr(
        module,
        "run_runtime_preflight",
        lambda **_kwargs: calls.append("runtime") or (),
    )

    with pytest.raises(SystemExit):
        module.main(
            [
                "--candidate-root",
                str(candidate_root),
                "--image-lock",
                str(tmp_path / "another-lock.json"),
                "--offline-contract-check",
            ]
        )

    assert "--candidate-root cannot be combined with --image-lock" in capsys.readouterr().err
    assert calls == []


def test_main_rejects_external_candidate_compose_drift_before_runtime_probes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    data_plane_compose = candidate_root / "docker-compose.data-plane.yml"
    data_plane_compose.write_text(
        data_plane_compose.read_text(encoding="utf-8").replace(
            "    restart: unless-stopped",
            "    restart: always",
            1,
        ),
        encoding="utf-8",
    )
    runtime_calls: list[str] = []

    async def record_runtime(**_kwargs) -> tuple[str, ...]:
        runtime_calls.append("runtime")
        return ()

    monkeypatch.setattr(
        module,
        "run_preflight",
        lambda **_kwargs: module.PreflightResult(()),
    )
    monkeypatch.setattr(
        module,
        "run_runtime_preflight",
        record_runtime,
    )
    monkeypatch.setattr(config_module, "load_platform_settings", lambda _path: object())

    assert (
        module.main(
            [
                "--config",
                str(ROOT / "deploy" / "platform.example.json"),
                "--secret-dir",
                str(tmp_path / "secrets"),
                "--candidate-root",
                str(candidate_root),
                "--hostname",
                "classroom.example.com",
            ]
        )
        == 1
    )

    assert "image lock candidate" in capsys.readouterr().out
    assert runtime_calls == []


def test_runtime_compose_probe_uses_external_candidate_root(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    lock_path = candidate_root / "deploy" / "image-lock.json"
    images = json.loads(lock_path.read_text(encoding="utf-8"))["images"]
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        commands.append(command)
        if command[-2:] == ["version", "--short"]:
            return subprocess.CompletedProcess(command, 0, "2.24.4\n", "")
        services = {
            "gateway": {
                "image": images["nginx"]["reference"],
                "ports": [{"published": "80"}, {"published": "443"}],
            },
            "deeptutor": {"image": images["deeptutor"]["reference"]},
            "minio": {"image": images["minio"]["reference"]},
            "minio-bootstrap": {"image": images["minio_client"]["reference"]},
            "openmaic": {"image": images["openmaic"]["reference"]},
            "openmaic-render": {"image": images["openmaic_render"]["reference"]},
            "postgres": {"image": images["postgres"]["reference"]},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps({"services": services}), "")

    assert (
        module._inspect_compose_runtime(
            ROOT,
            candidate_root=candidate_root,
            image_lock_path=lock_path,
            runner=runner,
        )
        == ()
    )
    assert str(candidate_root / "docker-compose.platform.yml") in commands[1]


def test_runtime_compose_probe_defaults_to_external_candidate_lock(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    lock_path = candidate_root / "deploy" / "image-lock.json"
    images = json.loads(lock_path.read_text(encoding="utf-8"))["images"]

    def runner(command, **_kwargs):
        if command[-2:] == ["version", "--short"]:
            return subprocess.CompletedProcess(command, 0, "2.24.4\n", "")
        services = {
            "gateway": {
                "image": images["nginx"]["reference"],
                "ports": [{"published": "80"}, {"published": "443"}],
            },
            "deeptutor": {"image": "ghcr.io/xinlingzhifei/deeptutor@sha256:" + "f" * 64},
            "minio": {"image": images["minio"]["reference"]},
            "minio-bootstrap": {"image": images["minio_client"]["reference"]},
            "openmaic": {"image": images["openmaic"]["reference"]},
            "openmaic-render": {"image": images["openmaic_render"]["reference"]},
            "postgres": {"image": images["postgres"]["reference"]},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps({"services": services}), "")

    assert module._inspect_compose_runtime(
        ROOT,
        candidate_root=candidate_root,
        runner=runner,
    ) == ("image lock match",)


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
    calls: list[tuple[Path, Path, Path, Path]] = []

    class Probe:
        async def inspect(
            self,
            *,
            settings: PlatformSettings,
            secret_dir: Path,
            image_lock_path: Path,
            project_root: Path,
            candidate_root: Path,
        ) -> tuple[str, ...]:
            assert settings.enabled is True
            calls.append((secret_dir, image_lock_path, project_root, candidate_root))
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
            candidate_root=ROOT,
            probe=Probe(),
        )
    )

    assert errors == expected
    assert calls == [(tmp_path, lock_path, ROOT, ROOT)]


def test_database_object_store_phase_never_calls_openmaic_or_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    calls: list[str] = []

    async def database(_settings):
        calls.append("database")
        return module._DatabaseInspection(
            (),
            (module._ActiveTenant("tenant-1", "tenant_one", "tenant-1.json"),),
            ("shared", "http://openmaic:3000"),
        )

    async def object_store(_settings, _secret_dir, _active_tenants):
        calls.append("object-store")
        return ()

    async def forbidden_openmaic(*_args):
        pytest.fail("database/object-store phase must not call OpenMAIC")

    def forbidden_compose(*_args, **_kwargs):
        pytest.fail("candidate-network phase must not call Docker Compose")

    monkeypatch.setattr(module, "_inspect_database_runtime", database)
    monkeypatch.setattr(module, "_inspect_object_store_runtime", object_store)
    monkeypatch.setattr(module, "_inspect_openmaic_runtime", forbidden_openmaic)
    monkeypatch.setattr(module, "_inspect_compose_runtime", forbidden_compose)

    report = asyncio.run(
        module.inspect_candidate_network_phase(
            phase="database-object-store",
            settings=object(),
            secret_dir=tmp_path,
        )
    )

    assert calls == ["database", "object-store"]
    assert report == {
        "schemaVersion": 1,
        "producer": "platform-preflight",
        "phase": "database-object-store",
        "checks": {
            "activeTenantCredentialsValid": True,
            "databaseConnected": True,
            "objectStoreRoundTrip": True,
            "revisionsMatch": True,
            "tenantCrossPrefixDenied": True,
            "tenantOwnPrefixAccessible": True,
        },
        "errors": [],
    }


def test_database_object_store_phase_does_not_pass_unexecuted_tenant_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    async def database(_settings):
        return module._DatabaseInspection((), (), ("shared", "http://openmaic:3000"))

    async def object_store(_settings, _secret_dir, _active_tenants):
        return ()

    monkeypatch.setattr(module, "_inspect_database_runtime", database)
    monkeypatch.setattr(module, "_inspect_object_store_runtime", object_store)

    report = asyncio.run(
        module.inspect_candidate_network_phase(
            phase="database-object-store",
            settings=object(),
            secret_dir=tmp_path,
        )
    )

    assert report["checks"] == {
        "activeTenantCredentialsValid": False,
        "databaseConnected": True,
        "objectStoreRoundTrip": True,
        "revisionsMatch": True,
        "tenantCrossPrefixDenied": False,
        "tenantOwnPrefixAccessible": False,
    }
    assert report["errors"] == ["active tenant inventory"]


def test_openmaic_phase_fails_when_database_dependency_is_not_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    async def database(_settings):
        return module._DatabaseInspection(
            ("database migrations",),
            (),
            ("shared", "http://openmaic:3000"),
        )

    async def openmaic(_route, _secret_dir):
        return ()

    monkeypatch.setattr(module, "_inspect_database_runtime", database)
    monkeypatch.setattr(module, "_inspect_openmaic_runtime", openmaic)

    report = asyncio.run(
        module.inspect_candidate_network_phase(
            phase="openmaic",
            settings=object(),
            secret_dir=tmp_path,
        )
    )

    assert report["checks"] == {"openmaicContractCompatible": False}
    assert report["errors"] == ["database migrations"]


def test_openmaic_phase_never_calls_object_store_or_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    calls: list[str] = []

    async def database(_settings):
        calls.append("database")
        return module._DatabaseInspection((), (), ("shared", "http://openmaic:3000"))

    async def forbidden_object_store(*_args):
        pytest.fail("OpenMAIC phase must not call object-store probes")

    async def openmaic(_route, _secret_dir):
        calls.append("openmaic")
        return ()

    def forbidden_compose(*_args, **_kwargs):
        pytest.fail("candidate-network phase must not call Docker Compose")

    monkeypatch.setattr(module, "_inspect_database_runtime", database)
    monkeypatch.setattr(module, "_inspect_object_store_runtime", forbidden_object_store)
    monkeypatch.setattr(module, "_inspect_openmaic_runtime", openmaic)
    monkeypatch.setattr(module, "_inspect_compose_runtime", forbidden_compose)

    report = asyncio.run(
        module.inspect_candidate_network_phase(
            phase="openmaic",
            settings=object(),
            secret_dir=tmp_path,
        )
    )

    assert calls == ["database", "openmaic"]
    assert report == {
        "schemaVersion": 1,
        "producer": "platform-preflight",
        "phase": "openmaic",
        "checks": {"openmaicContractCompatible": True},
        "errors": [],
    }


def test_candidate_network_report_json_is_canonical() -> None:
    module = _module()
    report = {
        "schemaVersion": 1,
        "producer": "platform-preflight",
        "phase": "openmaic",
        "checks": {"openmaicContractCompatible": True},
        "errors": [],
    }

    body = module.canonical_candidate_network_report(report)

    assert body == (
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def test_candidate_network_report_rejects_boolean_schema_version() -> None:
    module = _module()
    report = {
        "schemaVersion": True,
        "producer": "platform-preflight",
        "phase": "openmaic",
        "checks": {"openmaicContractCompatible": True},
        "errors": [],
    }
    body = module.canonical_candidate_network_report(report)

    with pytest.raises(ValueError, match="report is invalid"):
        module.parse_candidate_network_report(body, expected_phase="openmaic")


def test_candidate_network_report_rejects_oversized_body_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    parser_json = module.parse_candidate_network_report.__globals__["json"]

    def forbidden_loads(_body: bytes):
        pytest.fail("oversized reports must be rejected before JSON decoding")

    monkeypatch.setattr(parser_json, "loads", forbidden_loads)

    with pytest.raises(ValueError, match="report is too large"):
        module.parse_candidate_network_report(
            b" " * ((16 * 1024) + 1),
            expected_phase="openmaic",
        )


def test_runtime_phase_cli_skips_host_candidate_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    settings = object()
    calls: list[tuple[str, object, Path]] = []

    async def inspect(*, phase, settings, secret_dir):
        calls.append((phase, settings, secret_dir))
        return {
            "schemaVersion": 1,
            "producer": "platform-preflight",
            "phase": phase,
            "checks": {"openmaicContractCompatible": True},
            "errors": [],
        }

    def forbidden(*_args, **_kwargs):
        pytest.fail("candidate-network phase must not run host preflight checks")

    import deeptutor.services.config as config_module

    monkeypatch.setattr(config_module, "load_platform_settings", lambda path: settings)
    monkeypatch.setattr(module, "inspect_candidate_network_phase", inspect)
    monkeypatch.setattr(module, "run_preflight", forbidden)
    monkeypatch.setattr(module, "validate_image_lock_bindings", forbidden)

    exit_code = module.main(
        [
            "--runtime-phase",
            "openmaic",
            "--config",
            str(tmp_path / "platform.json"),
            "--secret-dir",
            str(tmp_path / "secrets"),
        ]
    )

    assert exit_code == 0
    assert calls == [("openmaic", settings, tmp_path / "secrets")]
    assert capsys.readouterr().out == (
        '{"checks":{"openmaicContractCompatible":true},"errors":[],'
        '"phase":"openmaic","producer":"platform-preflight","schemaVersion":1}\n'
    )


@pytest.mark.parametrize("case", ("raises", "invalid-report"))
def test_runtime_phase_cli_emits_canonical_failure_for_invalid_probe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    module = _module()

    async def inspect(**_kwargs):
        if case == "raises":
            raise RuntimeError("secret detail must not escape")
        return {"attacker": True}

    import deeptutor.services.config as config_module

    monkeypatch.setattr(config_module, "load_platform_settings", lambda path: object())
    monkeypatch.setattr(module, "inspect_candidate_network_phase", inspect)

    exit_code = module.main(
        [
            "--runtime-phase",
            "openmaic",
            "--config",
            str(tmp_path / "platform.json"),
            "--secret-dir",
            str(tmp_path / "secrets"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert captured.out == (
        '{"checks":{"openmaicContractCompatible":false},'
        '"errors":["candidate network preflight"],"phase":"openmaic",'
        '"producer":"platform-preflight","schemaVersion":1}\n'
    )


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
