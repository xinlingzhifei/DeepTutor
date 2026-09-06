from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import cache
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr
import pytest

import deeptutor.services.config as config_module
from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.secret_permissions import restrict_secret_file
from tests.scripts.platform_candidate_test_support import (
    POCKETBASE_BASE_COMPOSE_DRIFTS,
    drift_pocketbase_base_compose,
    write_schema3_candidate_root,
)

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


def test_observer_runtime_secret_metadata_requires_container_uid_and_gid() -> None:
    module = _module()

    for owner_id, group_id in ((0, 1000), (1000, 0), (1001, 1000), (1000, 1001)):
        details = SimpleNamespace(
            st_uid=owner_id,
            st_gid=group_id,
            st_mode=stat.S_IFREG | 0o400,
            st_size=32,
        )

        with pytest.raises(ValueError, match="observer container identity"):
            module._observer_runtime_secret_proof(
                details,
                file_name="observer-auth.token",
                platform_name="posix",
            )


def test_observer_runtime_secret_proof_replays_owner_group_and_mode() -> None:
    module = _module()

    for mode in (0o400, 0o600):
        details = SimpleNamespace(
            st_uid=1000,
            st_gid=1000,
            st_mode=stat.S_IFREG | mode,
            st_size=32,
        )

        assert module._observer_runtime_secret_proof(
            details,
            file_name="observer-auth.token",
            platform_name="posix",
        ) == {
            "fileName": "observer-auth.token",
            "mode": f"{mode:04o}",
            "ownerId": 1000,
            "groupId": 1000,
            "size": 32,
        }

    with pytest.raises(ValueError, match="POSIX ownership metadata"):
        module._observer_runtime_secret_proof(
            SimpleNamespace(
                st_uid=1000,
                st_gid=1000,
                st_mode=stat.S_IFREG | 0o400,
                st_size=32,
            ),
            file_name="observer-auth.token",
            platform_name="nt",
        )

    with pytest.raises(ValueError, match="mode is not restricted"):
        module._observer_runtime_secret_proof(
            SimpleNamespace(
                st_uid=1000,
                st_gid=1000,
                st_mode=stat.S_IFREG | 0o640,
                st_size=32,
            ),
            file_name="observer-auth.token",
            platform_name="posix",
        )


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


def test_main_rejects_explicit_image_lock_compatibility_path_before_any_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "run_preflight",
        lambda **_kwargs: calls.append("static") or module.PreflightResult(()),
    )
    monkeypatch.setattr(
        module,
        "validated_candidate_artifact_lease",
        lambda *_args, **_kwargs: calls.append("candidate"),
    )

    async def record_runtime(**_kwargs) -> tuple[str, ...]:
        calls.append("runtime")
        return ()

    monkeypatch.setattr(
        module,
        "run_runtime_preflight",
        record_runtime,
    )
    monkeypatch.setattr(config_module, "load_platform_settings", lambda _path: object())

    exit_code = module.main(
        [
            "--config",
            str(ROOT / "deploy" / "platform.example.json"),
            "--secret-dir",
            str(tmp_path),
            "--image-lock",
            str(tmp_path / "detached-image-lock.json"),
            "--hostname",
            "classroom.example.com",
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().out.splitlines() == ["image lock candidate"]
    assert calls == []


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
        candidate_root / "docker-compose.yml",
        candidate_root / "docker-compose.platform.yml",
        candidate_root / "docker-compose.data-plane.yml",
        deploy_dir / "docker-compose.openmaic-shared-ingress-observer.yml",
    )
    for source, destination in zip(
        (
            ROOT / "docker-compose.yml",
            ROOT / "docker-compose.platform.yml",
            ROOT / "docker-compose.data-plane.yml",
            ROOT / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml",
        ),
        compose_paths,
        strict=True,
    ):
        destination.write_bytes(source.read_bytes())
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


def _write_preflight_observer_manifest(candidate_root: Path, tmp_path: Path, monkeypatch):
    import docker_compose

    from tests.scripts import test_provision_openmaic_observer_secrets as support

    module = _module()
    roots = (
        candidate_root,
        tmp_path / "observer-secrets",
        tmp_path / "observer-trust",
        tmp_path / "observer-attestations",
    )
    for root in roots[1:]:
        root.mkdir(mode=0o700)
    provisioner = support._provisioner()
    support._install_metadata_seam(provisioner, monkeypatch, roots)
    support._provision(provisioner, roots)
    manifest_path = support._manifest_path(roots[2])
    lock = json.loads((candidate_root / "deploy" / "image-lock.json").read_bytes())
    admission = provisioner.load_committed_observer_release_manifest(
        manifest_path=manifest_path,
        candidate_root=candidate_root,
        observer_signer_image=lock["supportImages"]["observer_signer"]["reference"],
    )
    load_neighbor = docker_compose._load_neighbor_script

    def load_with_permission_seam(filename, *args):
        if filename == "provision_openmaic_observer_secrets.py":
            return provisioner
        if filename == "platform_preflight.py":
            return module
        return load_neighbor(filename, *args)

    monkeypatch.setattr(docker_compose, "_load_neighbor_script", load_with_permission_seam)
    # Keep the real consumer, secret snapshots and revalidation; POSIX DAC is separate.
    monkeypatch.setattr(
        module, "_inspect_observer_secret_preflight", lambda *_args: ((), {"ownerOnly": True})
    )
    return manifest_path, admission


@pytest.mark.parametrize("change_after", (None, "static", "database", "version", "config"))
def test_host_preflight_binds_committed_observer_manifest_through_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change_after: str | None,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    manifest, admission = _write_preflight_observer_manifest(candidate_root, tmp_path, monkeypatch)
    expected_environment = admission["environment"]
    secret = Path(admission["paths"]["observerAuthToken"])
    deployment_root = tmp_path / "deployment"
    deployment_root.mkdir()
    monkeypatch.setattr(module, "PROJECT_ROOT", deployment_root)
    for name in (*expected_environment, "YFEISTAI_CANDIDATE_ROOT"):
        monkeypatch.setenv(name, "ambient-wrong-value")
    monkeypatch.setenv("YFEISTAI_OBSERVER_RELEASE_MANIFEST", "ambient-wrong-manifest")
    commands: list[list[str]] = []
    events: list[str] = []
    static_inputs: list[dict[str, object]] = []
    lock = json.loads((candidate_root / "deploy" / "image-lock.json").read_bytes())
    services = {
        name: {"image": lock["images"][image]["reference"]}
        for name, image in (
            ("deeptutor", "deeptutor"),
            ("gateway", "nginx"),
            ("minio", "minio"),
            ("minio-bootstrap", "minio_client"),
            ("openmaic", "openmaic"),
            ("openmaic-render", "openmaic_render"),
            ("pocketbase", "pocketbase"),
            ("postgres", "postgres"),
        )
    }
    services["gateway"]["ports"] = [{"published": "80"}, {"published": "443"}]
    for name in ("openmaic-observer-attestation-signer", "openmaic-shared-ingress-observer"):
        services[name] = {"image": lock["supportImages"]["observer_signer"]["reference"]}
    services["openmaic-shared-ingress-observer"]["environment"] = _observer_compose_environment(
        admission
    )

    def mark_event(name):
        events.append(name)
        if change_after == name:
            secret.write_bytes(b"replacement-that-must-not-be-admitted\n")

    def static(**kwargs):
        static_inputs.append(kwargs)
        mark_event("static")
        return module.PreflightResult(())

    async def database(_settings):
        mark_event("database")
        return module._DatabaseInspection((), (), None)

    async def successful_dependency(*_args):
        return ()

    def runner(command, **kwargs):
        commands.append(command)
        assert {
            name: kwargs["env"].get(name) for name in expected_environment
        } == expected_environment
        assert kwargs["env"]["YFEISTAI_CANDIDATE_ROOT"] == str(candidate_root)
        assert "YFEISTAI_OBSERVER_RELEASE_MANIFEST" not in kwargs["env"]
        version = command[-2:] == ["version", "--short"]
        mark_event("version" if version else "config")
        return subprocess.CompletedProcess(
            command, 0, "2.24.4\n" if version else json.dumps({"services": services}), ""
        )

    compose_probe = module._inspect_compose_runtime
    monkeypatch.setattr(module, "run_preflight", static)
    monkeypatch.setattr(module, "_inspect_database_runtime", database)
    monkeypatch.setattr(module, "_inspect_object_store_runtime", successful_dependency)
    monkeypatch.setattr(module, "_inspect_openmaic_runtime", successful_dependency)
    monkeypatch.setattr(
        module,
        "_inspect_compose_runtime",
        lambda *args, **kwargs: compose_probe(*args, **kwargs, runner=runner),
    )
    monkeypatch.setattr(config_module, "load_platform_settings", lambda _path: object())
    try:
        result = module.main(
            [
                "--candidate-root",
                str(candidate_root),
                "--observer-release-manifest",
                str(manifest),
                "--config",
                str(ROOT / "deploy" / "platform.example.json"),
                "--secret-dir",
                str(tmp_path / "platform-secrets"),
                "--hostname",
                "classroom.example.com",
            ]
        )
    except SystemExit as exc:
        result = exc.code

    assert result == (0 if change_after is None else 1)
    assert {name: static_inputs[0][name] for name in admission["platformPreflightInputs"]} == {
        name: Path(value) if name == "observer_secret_dir" else value
        for name, value in admission["platformPreflightInputs"].items()
    }
    assert (
        len(commands)
        == {None: 2, "static": 0, "database": 0, "version": 1, "config": 2}[change_after]
    )
    if change_after is not None:
        assert "observer release" in capsys.readouterr().out
    assert os.environ["YFEISTAI_CANDIDATE_ROOT"] == "ambient-wrong-value"


def test_host_preflight_requires_manifest_before_runtime_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(module, "run_preflight", lambda **_kwargs: module.PreflightResult(()))
    monkeypatch.setattr(config_module, "load_platform_settings", lambda _path: object())

    async def runtime(**_kwargs):
        calls.append("runtime")
        return ()

    monkeypatch.setattr(module, "run_runtime_preflight", runtime)
    result = module.main(
        ["--candidate-root", str(candidate_root), "--hostname", "classroom.example.com"]
    )
    assert result == 1
    assert "observer release manifest required" in capsys.readouterr().out
    assert calls == []


def test_main_uses_external_candidate_root_for_lock_and_runtime_compose(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    manifest, _admission = _write_preflight_observer_manifest(candidate_root, tmp_path, monkeypatch)
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
                "--observer-release-manifest",
                str(manifest),
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


def test_main_rejects_candidate_drift_after_static_preflight_before_runtime_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    platform_compose = candidate_root / "docker-compose.platform.yml"
    original_compose = platform_compose.read_bytes()
    drifted_compose = original_compose + b"\n# Candidate changed after static checks.\n"
    runtime_calls: list[str] = []

    def complete_static_preflight(**_kwargs) -> object:
        platform_compose.write_bytes(drifted_compose)
        return module.PreflightResult(())

    async def record_runtime(**_kwargs) -> tuple[str, ...]:
        runtime_calls.append("runtime")
        return ()

    monkeypatch.setattr(module, "run_preflight", complete_static_preflight)
    monkeypatch.setattr(module, "run_runtime_preflight", record_runtime)
    monkeypatch.setattr(config_module, "load_platform_settings", lambda _path: object())

    result = module.main(
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

    assert platform_compose.read_bytes() == drifted_compose
    assert result == 1
    assert capsys.readouterr().out.splitlines() == ["image lock candidate"]
    assert runtime_calls == []


@pytest.mark.parametrize("drift", POCKETBASE_BASE_COMPOSE_DRIFTS)
def test_main_rejects_schema3_pocketbase_base_compose_drift_before_runtime_probes(
    tmp_path: Path,
    monkeypatch,
    capsys,
    drift: str,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    lock = json.loads((candidate_root / "deploy" / "image-lock.json").read_text(encoding="utf-8"))
    assert lock["schemaVersion"] == 3
    assert "pocketbase" in lock["images"]
    drift_pocketbase_base_compose(candidate_root, lock, drift)
    runtime_calls: list[str] = []

    async def record_runtime(**_kwargs) -> tuple[str, ...]:
        runtime_calls.append("runtime")
        return ()

    monkeypatch.setattr(
        module,
        "run_preflight",
        lambda **_kwargs: module.PreflightResult(()),
    )
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
                str(candidate_root),
                "--hostname",
                "classroom.example.com",
            ]
        )
        == 1
    )

    assert "image lock candidate" in capsys.readouterr().out
    assert runtime_calls == []


def test_main_rejects_schema3_pocketbase_base_compose_identity_drift_after_runtime_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    candidate_root, lock = write_schema3_candidate_root(tmp_path / "stable")
    manifest, _admission = _write_preflight_observer_manifest(candidate_root, tmp_path, monkeypatch)
    drift_root, drift_lock = write_schema3_candidate_root(tmp_path / "identity-drift")
    drift_pocketbase_base_compose(drift_root, drift_lock, "different-digest")
    base_compose = candidate_root / "docker-compose.yml"
    drifted_base_compose = drift_root / "docker-compose.yml"
    assert lock["schemaVersion"] == drift_lock["schemaVersion"] == 3
    assert base_compose.read_bytes() != drifted_base_compose.read_bytes()

    original_stat = os.stat
    original_lstat = os.lstat
    base_key = os.path.normcase(os.path.abspath(base_compose))
    drift_active = False
    runtime_calls: list[str] = []

    def redirected(path: object) -> bool:
        try:
            return os.path.normcase(os.path.abspath(os.fspath(path))) == base_key
        except TypeError:
            return False

    def redirected_stat(path, *args, **kwargs):
        target = drifted_base_compose if drift_active and redirected(path) else path
        return original_stat(target, *args, **kwargs)

    def redirected_lstat(path, *args, **kwargs):
        target = drifted_base_compose if drift_active and redirected(path) else path
        return original_lstat(target, *args, **kwargs)

    async def complete_runtime_probe(**_kwargs) -> tuple[str, ...]:
        nonlocal drift_active
        runtime_calls.append("runtime")
        drift_active = True
        return ()

    monkeypatch.setattr(os, "stat", redirected_stat)
    monkeypatch.setattr(os, "lstat", redirected_lstat)
    monkeypatch.setattr(
        module,
        "run_preflight",
        lambda **_kwargs: module.PreflightResult(()),
    )
    monkeypatch.setattr(module, "run_runtime_preflight", complete_runtime_probe)
    monkeypatch.setattr(config_module, "load_platform_settings", lambda _path: object())

    result = module.main(
        [
            "--config",
            str(ROOT / "deploy" / "platform.example.json"),
            "--secret-dir",
            str(tmp_path / "secrets"),
            "--candidate-root",
            str(candidate_root),
            "--observer-release-manifest",
            str(manifest),
            "--hostname",
            "classroom.example.com",
        ]
    )

    assert result == 1
    assert capsys.readouterr().out.splitlines() == ["image lock candidate"]
    assert runtime_calls == ["runtime"]


def _stub_admitted_observer_for_compose_unit_probe():
    # Compose output/version units isolate admission; the host test uses the real consumer.
    from tests.scripts.test_provision_openmaic_observer_secrets import _deployment_environment

    return {
        "environment": {
            **dict.fromkeys(_module()._OBSERVER_RUNTIME_ENVIRONMENT, "admitted-unit-value"),
            **_deployment_environment(),
        },
        "_revalidate": lambda: None,
    }


def _observer_compose_environment(admission=None):
    admitted = (admission or _stub_admitted_observer_for_compose_unit_probe())["environment"]
    from tests.scripts.test_provision_openmaic_observer_secrets import _deployment_environment

    return {
        **{
            name: admitted[name]
            for name in (
                "YFEISTAI_RELEASE_RUN_ID",
                "YFEISTAI_CANDIDATE_SHA256",
                "YFEISTAI_IMAGE_LOCK_SHA256",
                "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256",
            )
        },
        **{name: admitted[name] for name in _deployment_environment()},
        "YFEISTAI_SHARED_INGRESS_INTERNAL_PROXY_URL": "http://openmaic-shared-ingress-observer:8000",
    }


def test_runtime_compose_probe_uses_schema3_observer_signer_from_external_candidate_root(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    deployment_root = tmp_path / "deployment"
    env_file = deployment_root / "data" / "user" / "settings" / "docker.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_bytes(b"DEEPTUTOR_DOCKER_BACKEND_PORT=8999\n")
    original_inputs = {
        path: path.read_bytes()
        for path in (
            env_file,
            candidate_root / "docker-compose.yml",
            candidate_root / "docker-compose.platform.yml",
            candidate_root / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml",
        )
    }
    lock_path = candidate_root / "deploy" / "image-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    images = lock["images"]
    observer_signer = lock["supportImages"]["observer_signer"]
    pocketbase = images["pocketbase"]
    assert lock["schemaVersion"] == 3
    assert pocketbase["digest"] != "sha256:" + "0" * 64
    assert pocketbase["reference"].endswith(f"@{pocketbase['digest']}")
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
            "openmaic-observer-attestation-signer": {"image": observer_signer["reference"]},
            "openmaic-shared-ingress-observer": {
                "image": observer_signer["reference"],
                "environment": _observer_compose_environment(),
            },
            "pocketbase": {"image": pocketbase["reference"]},
            "postgres": {"image": images["postgres"]["reference"]},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps({"services": services}), "")

    assert (
        module._inspect_compose_runtime(
            deployment_root,
            candidate_root=candidate_root,
            image_lock_path=lock_path,
            observer_admission=_stub_admitted_observer_for_compose_unit_probe(),
            runner=runner,
        )
        == ()
    )
    assert commands == [
        ["docker", "compose", "version", "--short"],
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "--project-directory",
            str(deployment_root),
            "--project-name",
            "yfeistai-platform",
            "-f",
            str(candidate_root / "docker-compose.yml"),
            "-f",
            str(candidate_root / "docker-compose.platform.yml"),
            "-f",
            str(candidate_root / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml"),
            "--profile",
            "legacy-local",
            "config",
            "--format",
            "json",
        ],
    ]
    assert {path: path.read_bytes() for path in original_inputs} == original_inputs


@pytest.mark.parametrize(
    ("service_name", "lock_name"),
    (
        pytest.param("deeptutor", "deeptutor", id="deeptutor"),
        pytest.param("pocketbase", "pocketbase", id="pocketbase"),
    ),
)
def test_runtime_compose_probe_reports_locked_service_drift_with_matching_observer_signer(
    tmp_path: Path,
    service_name: str,
    lock_name: str,
) -> None:
    module = _module()
    candidate_root = _write_preflight_candidate_root(tmp_path)
    lock_path = candidate_root / "deploy" / "image-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    images = lock["images"]
    observer_signer = lock["supportImages"]["observer_signer"]

    def runner(command, **_kwargs):
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
            "openmaic-observer-attestation-signer": {"image": observer_signer["reference"]},
            "openmaic-shared-ingress-observer": {
                "image": observer_signer["reference"],
                "environment": _observer_compose_environment(),
            },
            "pocketbase": {"image": images["pocketbase"]["reference"]},
            "postgres": {"image": images["postgres"]["reference"]},
        }
        services[service_name]["image"] = f"{images[lock_name]['repository']}@sha256:" + "f" * 64
        return subprocess.CompletedProcess(command, 0, json.dumps({"services": services}), "")

    assert module._inspect_compose_runtime(
        ROOT,
        candidate_root=candidate_root,
        observer_admission=_stub_admitted_observer_for_compose_unit_probe(),
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
            observer_admission: dict[str, object] | None = None,
        ) -> tuple[str, ...]:
            assert settings.enabled is True
            assert observer_admission is None
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
    monkeypatch.setattr(module, "validated_candidate_artifact_lease", forbidden)

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
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "foreign-project")
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
                        "openmaic-shared-ingress-observer": {
                            "environment": _observer_compose_environment(),
                        },
                    }
                }
            ),
            "",
        )

    errors = module._inspect_compose_runtime(
        ROOT, runner=runner, observer_admission=_stub_admitted_observer_for_compose_unit_probe()
    )

    assert errors == ("Docker Compose version", "gateway-only public ports")
    assert len(calls) == 2
    assert all("COMPOSE_FILE" not in environment for _command, environment in calls)
    assert all("COMPOSE_PROFILES" not in environment for _command, environment in calls)
    assert all("COMPOSE_PROJECT_NAME" not in environment for _command, environment in calls)


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
                        },
                        "openmaic-shared-ingress-observer": {
                            "environment": _observer_compose_environment(),
                        },
                    }
                }
            ),
            "",
        )

    assert (
        module._inspect_compose_runtime(
            ROOT, runner=runner, observer_admission=_stub_admitted_observer_for_compose_unit_probe()
        )
        == ()
    )
