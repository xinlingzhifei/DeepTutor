from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import ModuleType, SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
import pytest
import yaml

from tests.scripts.platform_candidate_test_support import write_schema3_candidate_root

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "provision_openmaic_observer_secrets.py"
PREFLIGHT_PATH = ROOT / "scripts" / "platform_preflight.py"
OVERLAY_PATH = ROOT / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml"
RUN_ID = "first-release-observer-run-01"
CANDIDATE_SOURCE_HEAD = "a" * 40
CANDIDATE_RELEASE_TAG = "yfeistai-first-release-20260831-aaaaaaaa"
OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
SIGNER_REPOSITORY = "ghcr.io/xinlingzhifei/yfeistai-observer-signer"
SIGNER_IMAGE = f"{SIGNER_REPOSITORY}:{CANDIDATE_RELEASE_TAG}@sha256:" + "d" * 64
IMAGE_SPECS = {
    "deeptutor": ("ghcr.io/xinlingzhifei/deeptutor", None, "1"),
    "openmaic": ("ghcr.io/xinlingzhifei/openmaic", None, "2"),
    "openmaic_render": ("ghcr.io/xinlingzhifei/openmaic-render", None, "3"),
    "nginx": ("nginx", "1.29.8-alpine3.23", "4"),
    "postgres": ("postgres", "16.14-alpine3.24", "5"),
    "minio": ("minio/minio", "RELEASE.2025-04-22T22-12-26Z", "6"),
    "minio_client": ("minio/mc", "RELEASE.2025-04-16T18-13-26Z", "7"),
    "pocketbase": ("ghcr.io/muchobien/pocketbase", "latest", "8"),
}
MANIFEST_FILE = "observer-release-manifest.json"
OWNER_MARKER_FILE = ".yfeistai-observer-provisioner-owner.json"
OWNER_MARKER_KIND = "yfeistai-openmaic-observer-secret-provisioner"
PRIVATE_TARGETS = {
    "observerAuthToken": ("observer-auth.token", 0o400),
    "candidateHealthToken": ("candidate-health.token", 0o400),
    "attestationSigningKey": ("attestation-signing-key.pem", 0o600),
}
DEPLOYMENT_INPUTS = {
    "environment_id": "preflight-environment",
    "observer_id": "preflight-observer",
    "observer_url": "https://observer.example.test",
    "control_url": "https://control.example.test/v1/control-canaries",
    "upstream_url": "https://upstream.example.test",
    "candidate_health_url": (
        "https://candidate.example.test/api/v1/system/teaching-health/observer-canary"
    ),
    "auth_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
}


def _deployment_environment(inputs=None):
    return {
        "YFEISTAI_ENVIRONMENT_ID"
        if name == "environment_id"
        else "YFEISTAI_SHARED_INGRESS_" + name.upper(): value
        for name, value in (DEPLOYMENT_INPUTS if inputs is None else inputs).items()
    }


def _load(path: Path, name: str, *, missing: str) -> ModuleType:
    assert path.is_file(), missing
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _provisioner() -> ModuleType:
    return _load(
        SCRIPT_PATH,
        "provision_openmaic_observer_secrets_under_test",
        missing="observer secret provisioner is missing",
    )


def _candidate_lock_document(
    *,
    source_head: str = CANDIDATE_SOURCE_HEAD,
    release_tag: str = CANDIDATE_RELEASE_TAG,
) -> dict[str, object]:
    images: dict[str, dict[str, str]] = {}
    for name, (repository, fixed_tag, digest_digit) in IMAGE_SPECS.items():
        tag = fixed_tag or release_tag
        digest = "sha256:" + digest_digit * 64
        images[name] = {
            "repository": repository,
            "tag": tag,
            "digest": digest,
            "reference": f"{repository}:{tag}@{digest}",
        }
    signer_digest = "sha256:" + "d" * 64
    return {
        "schemaVersion": 3,
        "candidate": {
            "sourceRepository": "xinlingzhifei/DeepTutor",
            "sourceHead": source_head,
            "releaseTag": release_tag,
            "openmaicHead": OPENMAIC_HEAD,
            "imageDigests": {
                "deeptutor": "sha256:" + "1" * 64,
                "openmaic": "sha256:" + "2" * 64,
                "openmaic_render": "sha256:" + "3" * 64,
            },
        },
        "images": images,
        "supportImages": {
            "observer_signer": {
                "repository": SIGNER_REPOSITORY,
                "tag": release_tag,
                "digest": signer_digest,
                "reference": f"{SIGNER_REPOSITORY}:{release_tag}@{signer_digest}",
            }
        },
    }


def _write_candidate_lock(
    candidate_root: Path,
    *,
    source_head: str = CANDIDATE_SOURCE_HEAD,
    release_tag: str = CANDIDATE_RELEASE_TAG,
) -> Path:
    lock_path = candidate_root / "deploy" / "image-lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            _candidate_lock_document(source_head=source_head, release_tag=release_tag),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return lock_path


def _candidate_binding(candidate_root: Path) -> dict[str, object]:
    lock_path = candidate_root / "deploy" / "image-lock.json"
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    return {
        "imageLockSha256": hashlib.sha256(lock_bytes).hexdigest(),
        "candidate": lock["candidate"],
        "observerSignerImage": lock["supportImages"]["observer_signer"]["reference"],
    }


def _roots(base: Path) -> tuple[Path, Path, Path, Path]:
    roots = tuple(
        base / name
        for name in (
            "candidate",
            "observer-secrets",
            "observer-trust",
            "observer-attestations",
        )
    )
    for root in roots:
        root.mkdir(parents=True)
    _write_candidate_lock(roots[0])
    return tuple(root.resolve() for root in roots)


def _paths(secret_root: Path, trust_root: Path, attestation_root: Path) -> dict[str, Path]:
    secret_run = secret_root / RUN_ID
    trust_run = trust_root / RUN_ID
    attestation_run = attestation_root / RUN_ID
    return {
        "secretRoot": secret_root,
        "runDirectory": secret_run,
        "observerAuthToken": secret_run / "observer-auth.token",
        "candidateHealthToken": secret_run / "candidate-health.token",
        "attestationSigningKey": secret_run / "attestation-signing-key.pem",
        "trustRoot": trust_root,
        "trustDirectory": trust_run,
        "attestationPublicKey": trust_run / "openmaic-observer-attestation-public-key.pem",
        "attestationDirectory": attestation_run,
        "observerAttestation": (
            attestation_run / "openmaic-shared-ingress-observer-attestation.json"
        ),
        "observerAttestationSignature": (
            attestation_run / "openmaic-shared-ingress-observer-attestation.sig"
        ),
    }


def _operator_identity(path: Path) -> tuple[int, int]:
    details = path.stat()
    uid = os.geteuid() if hasattr(os, "geteuid") else int(details.st_uid)
    gid = os.getegid() if hasattr(os, "getegid") else int(details.st_gid)
    return uid, gid


def _install_metadata_seam(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, ...],
) -> dict[Path, SimpleNamespace]:
    records: dict[Path, SimpleNamespace] = {}
    operator_uid, operator_gid = _operator_identity(roots[0])
    inode = 100

    def record(path: Path, *, uid: int, gid: int, mode: int) -> None:
        nonlocal inode
        target = Path(path).resolve(strict=True)
        details = target.stat()
        inode += 1
        records[target] = SimpleNamespace(
            st_dev=1,
            st_ino=inode,
            st_size=details.st_size,
            st_mode=(stat.S_IFDIR if target.is_dir() else stat.S_IFREG) | mode,
            st_uid=uid,
            st_gid=gid,
            st_file_attributes=0,
        )

    for root in roots:
        record(root, uid=operator_uid, gid=operator_gid, mode=0o700)

    def apply_private_metadata(path: Path, *, uid: int, gid: int, mode: int) -> None:
        record(path, uid=uid, gid=gid, mode=mode)

    def private_metadata(path: Path) -> SimpleNamespace:
        target = Path(path).resolve(strict=True)
        if target not in records:
            record(
                target,
                uid=operator_uid,
                gid=operator_gid,
                mode=0o700 if target.is_dir() else 0o600,
            )
        return records[target]

    monkeypatch.setattr(module, "_apply_private_metadata", apply_private_metadata)
    monkeypatch.setattr(module, "_private_metadata", private_metadata)
    return records


def _provision(
    module: ModuleType,
    roots: tuple[Path, Path, Path, Path],
    **deployment_overrides,
) -> dict[str, object]:
    candidate_root, secret_root, trust_root, attestation_root = roots
    return module.provision_observer_release_secrets(
        candidate_root=candidate_root,
        secret_root=secret_root,
        trust_root=trust_root,
        attestation_root=attestation_root,
        release_run_id=RUN_ID,
        container_uid=1000,
        container_gid=1000,
        **{**DEPLOYMENT_INPUTS, **deployment_overrides},
    )


def _cli_arguments(roots: tuple[Path, Path, Path, Path]) -> list[str]:
    candidate_root, secret_root, trust_root, attestation_root = roots
    return [
        "--candidate-root",
        str(candidate_root),
        "--secret-root",
        str(secret_root),
        "--trust-root",
        str(trust_root),
        "--attestation-root",
        str(attestation_root),
        "--release-run-id",
        RUN_ID,
        "--container-uid",
        "1000",
        "--container-gid",
        "1000",
        *(
            argument
            for name, value in DEPLOYMENT_INPUTS.items()
            for argument in ("--" + name.replace("_", "-"), value)
        ),
    ]


def _manifest_path(trust_root: Path) -> Path:
    return trust_root / RUN_ID / MANIFEST_FILE


def _manifest_document(
    roots: tuple[Path, Path, Path, Path],
    *,
    status: str,
    spki_sha256: str = "a" * 64,
) -> dict[str, object]:
    candidate_root, secret_root, trust_root, attestation_root = roots
    paths = _paths(secret_root, trust_root, attestation_root)
    manifest_path = _manifest_path(trust_root)
    return {
        "schemaVersion": 2,
        "status": status,
        "releaseRunId": RUN_ID,
        "roots": {
            "candidateRoot": str(candidate_root),
            "secretRoot": str(secret_root),
            "trustRoot": str(trust_root),
            "attestationRoot": str(attestation_root),
        },
        "paths": {
            **{name: str(path) for name, path in paths.items()},
            "manifest": str(manifest_path),
        },
        "candidateBinding": _candidate_binding(candidate_root),
        "deploymentEnvironment": _deployment_environment(),
        "publicKeySpkiSha256": spki_sha256,
    }


def _write_recovery_batch(
    roots: tuple[Path, Path, Path, Path],
    *,
    status: str,
    owner_markers: bool,
) -> dict[Path, bytes]:
    _candidate_root, secret_root, trust_root, attestation_root = roots
    run_directories = {
        "secret": secret_root / RUN_ID,
        "trust": trust_root / RUN_ID,
        "attestation": attestation_root / RUN_ID,
    }
    for role, directory in run_directories.items():
        directory.mkdir()
        (directory / f"{role}-payload.bin").write_bytes(f"preserve-{status}-{role}".encode("ascii"))
        if owner_markers:
            (directory / OWNER_MARKER_FILE).write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "owner": OWNER_MARKER_KIND,
                        "releaseRunId": RUN_ID,
                        "rootRole": role,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
    _manifest_path(trust_root).write_text(
        json.dumps(_manifest_document(roots, status=status), sort_keys=True),
        encoding="utf-8",
    )
    return {
        path: path.read_bytes()
        for directory in run_directories.values()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _recover(
    module: ModuleType,
    roots: tuple[Path, Path, Path, Path],
) -> dict[str, str]:
    candidate_root, secret_root, trust_root, attestation_root = roots
    return module.recover_observer_release_batch(
        candidate_root=candidate_root,
        secret_root=secret_root,
        trust_root=trust_root,
        attestation_root=attestation_root,
        release_run_id=RUN_ID,
    )


def _render_compose_path(template: str, environment: dict[str, str]) -> Path:
    def replace(match: re.Match[str]) -> str:
        return environment[match.group(1)]

    rendered = re.sub(r"\$\{([A-Z0-9_]+):\?[^}]+\}", replace, template)
    assert "${" not in rendered
    return Path(rendered).resolve()


def _argument_mapping(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    assert isinstance(value, list) and len(value) % 2 == 0
    return {str(value[index]): str(value[index + 1]) for index in range(0, len(value), 2)}


def test_observer_secret_candidate_binding_accepts_complete_schema3_image_specs() -> None:
    module = _provisioner()
    lock = _candidate_lock_document()
    body = (json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    assert module._candidate_binding_from_image_lock_bytes(body) == {
        "imageLockSha256": hashlib.sha256(body).hexdigest(),
        "candidate": lock["candidate"],
        "observerSignerImage": lock["supportImages"]["observer_signer"]["reference"],
    }


@pytest.mark.parametrize("mutation", ("missing", "extra", "zero", "drift"))
def test_observer_secret_candidate_binding_rejects_incomplete_or_drifted_schema3_images(
    mutation: str,
) -> None:
    module = _provisioner()
    lock = _candidate_lock_document()
    images = lock["images"]
    assert isinstance(images, dict)
    if mutation == "missing":
        images.pop("pocketbase")
    elif mutation == "extra":
        images["unexpected"] = {
            "repository": "ghcr.io/example/unexpected",
            "tag": "latest",
            "digest": "sha256:" + "9" * 64,
            "reference": "ghcr.io/example/unexpected:latest@sha256:" + "9" * 64,
        }
    else:
        pocketbase = images["pocketbase"]
        assert isinstance(pocketbase, dict)
        if mutation == "zero":
            pocketbase["digest"] = "sha256:" + "0" * 64
        else:
            pocketbase["repository"] = "ghcr.io/example/pocketbase"
        pocketbase["reference"] = (
            f"{pocketbase['repository']}:{pocketbase['tag']}@{pocketbase['digest']}"
        )
    body = (json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    with pytest.raises(ValueError, match="observer release manifest candidate binding"):
        module._candidate_binding_from_image_lock_bytes(body)


def test_observer_secret_provisioner_cli_publishes_non_secret_committed_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    _candidate_root, secret_root, trust_root, attestation_root = roots
    _install_metadata_seam(module, monkeypatch, roots)
    main = getattr(module, "main", None)
    assert callable(main), "observer secret provisioner CLI is missing"

    assert main(_cli_arguments(roots)) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    manifest_path = _manifest_path(trust_root)
    manifest_body = manifest_path.read_bytes()
    manifest = json.loads(manifest_body)
    assert json.loads(captured.out) == manifest
    assert manifest == _manifest_document(
        roots,
        status="committed",
        spki_sha256=manifest["publicKeySpkiSha256"],
    )
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["publicKeySpkiSha256"])

    paths = _paths(secret_root, trust_root, attestation_root)
    observer_body = paths["observerAuthToken"].read_bytes()
    health_body = paths["candidateHealthToken"].read_bytes()
    private_body = paths["attestationSigningKey"].read_bytes()
    public_body = paths["attestationPublicKey"].read_bytes()
    public_key = serialization.load_pem_public_key(public_body)
    assert isinstance(public_key, Ed25519PublicKey)
    public_spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert manifest["publicKeySpkiSha256"] == hashlib.sha256(public_spki).hexdigest()

    emitted = captured.out.encode("utf-8")
    for secret_body in (observer_body.strip(), health_body.strip(), private_body):
        assert secret_body not in manifest_body
        assert secret_body not in emitted
    for private_line in private_body.splitlines()[1:-1]:
        assert private_line not in manifest_body
        assert private_line not in emitted


def test_observer_secret_provisioner_recovers_only_its_uncommitted_release_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    recover = getattr(module, "recover_observer_release_batch", None)
    assert callable(recover), "observer secret provisioner recovery is missing"

    owned_roots = _roots(tmp_path / "owned-uncommitted")
    _install_metadata_seam(module, monkeypatch, owned_roots)
    owned_snapshot = _write_recovery_batch(
        owned_roots,
        status="uncommitted",
        owner_markers=True,
    )
    recovered = _recover(module, owned_roots)
    assert all(not (root / RUN_ID).exists() for root in owned_roots[1:])
    for role, root in zip(("secret", "trust", "attestation"), owned_roots[1:], strict=True):
        quarantine = Path(recovered[role])
        assert quarantine == module._quarantine_path(root, RUN_ID)
        original_run = root / RUN_ID
        original_files = {
            path: body for path, body in owned_snapshot.items() if path.is_relative_to(original_run)
        }
        assert original_files
        for original, body in original_files.items():
            assert (quarantine / original.relative_to(original_run)).read_bytes() == body

    committed_roots = _roots(tmp_path / "committed")
    _install_metadata_seam(module, monkeypatch, committed_roots)
    committed_snapshot = _write_recovery_batch(
        committed_roots,
        status="committed",
        owner_markers=True,
    )
    with pytest.raises(ValueError, match="committed"):
        _recover(module, committed_roots)
    assert all(path.read_bytes() == body for path, body in committed_snapshot.items())

    unowned_roots = _roots(tmp_path / "unowned")
    _install_metadata_seam(module, monkeypatch, unowned_roots)
    unowned_snapshot = _write_recovery_batch(
        unowned_roots,
        status="uncommitted",
        owner_markers=False,
    )
    with pytest.raises(ValueError, match="owner"):
        _recover(module, unowned_roots)
    assert all(path.read_bytes() == body for path, body in unowned_snapshot.items())


def test_observer_secret_provisioner_recovers_crash_after_manifest_before_other_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    _candidate_root, _secret_root, trust_root, _attestation_root = roots
    _install_metadata_seam(module, monkeypatch, roots)
    trust_run = trust_root / RUN_ID
    trust_run.mkdir()
    marker_path = trust_run / OWNER_MARKER_FILE
    marker_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "owner": OWNER_MARKER_KIND,
                "releaseRunId": RUN_ID,
                "rootRole": "trust",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest_path = _manifest_path(trust_root)
    manifest_path.write_text(
        json.dumps(_manifest_document(roots, status="uncommitted"), sort_keys=True),
        encoding="utf-8",
    )
    before = {
        marker_path.name: marker_path.read_bytes(),
        manifest_path.name: manifest_path.read_bytes(),
    }

    recovered = _recover(module, roots)

    assert set(recovered) == {"trust"}
    quarantine = Path(recovered["trust"])
    assert not trust_run.exists()
    assert {name: (quarantine / name).read_bytes() for name in before} == before


def test_observer_secret_provisioner_rejects_parent_identity_replacement_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    _candidate_root, secret_root, _trust_root, _attestation_root = roots
    records = _install_metadata_seam(module, monkeypatch, roots)
    original_write_owner_marker = module._write_owner_marker
    displaced_secret_root = tmp_path / "displaced-observer-secret-root"

    def replace_secret_root_after_manifest_directory(*args, **kwargs):
        original_write_owner_marker(*args, **kwargs)
        if kwargs["root_role"] != "trust":
            return
        secret_root.rename(displaced_secret_root)
        secret_root.mkdir()
        records.pop(secret_root, None)

    monkeypatch.setattr(module, "_write_owner_marker", replace_secret_root_after_manifest_directory)

    with pytest.raises(ValueError, match="observer secret root changed"):
        _provision(module, roots)

    assert not (secret_root / RUN_ID).exists()


def test_observer_secret_provisioner_refuses_committed_release_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    _install_metadata_seam(module, monkeypatch, roots)
    committed_snapshot = _write_recovery_batch(
        roots,
        status="committed",
        owner_markers=True,
    )
    main = getattr(module, "main", None)
    assert callable(main), "observer secret provisioner CLI is missing"

    with pytest.raises(ValueError, match="committed"):
        main(_cli_arguments(roots))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert all(path.read_bytes() == body for path, body in committed_snapshot.items())
    assert not any(
        path.is_file() and path not in committed_snapshot
        for root in roots[1:]
        for path in (root / RUN_ID).rglob("*")
    )


def test_observer_secret_provisioner_publishes_run_bound_tokens_keypair_and_spki(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    candidate_root, secret_root, trust_root, attestation_root = roots
    records = _install_metadata_seam(module, monkeypatch, roots)

    result = _provision(module, roots)
    paths = _paths(secret_root, trust_root, attestation_root)

    observer_body = paths["observerAuthToken"].read_bytes()
    health_body = paths["candidateHealthToken"].read_bytes()
    observer_token = observer_body.decode("ascii").strip()
    health_token = health_body.decode("ascii").strip()
    assert len(observer_token) >= 32
    assert len(health_token) >= 32
    assert observer_token != health_token

    private_body = paths["attestationSigningKey"].read_bytes()
    public_body = paths["attestationPublicKey"].read_bytes()
    private_key = serialization.load_pem_private_key(private_body, password=None)
    public_key = serialization.load_pem_public_key(public_body)
    assert isinstance(private_key, Ed25519PrivateKey)
    assert isinstance(public_key, Ed25519PublicKey)
    assert private_body.startswith(b"-----BEGIN PRIVATE KEY-----")
    public_spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert public_spki == private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    spki_sha256 = hashlib.sha256(public_spki).hexdigest()
    assert spki_sha256 != "0" * 64
    assert result["environment"]["YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256"] == spki_sha256

    for path_name, (_file_name, mode) in PRIVATE_TARGETS.items():
        details = records[paths[path_name].resolve()]
        assert (details.st_uid, details.st_gid) == (1000, 1000)
        assert stat.S_IMODE(details.st_mode) == mode
    run_details = records[paths["runDirectory"].resolve()]
    assert (run_details.st_uid, run_details.st_gid) == _operator_identity(secret_root)
    assert stat.S_IMODE(run_details.st_mode) == 0o700

    assert paths["attestationPublicKey"].parent == trust_root / RUN_ID
    assert candidate_root not in paths["attestationPublicKey"].parents
    rendered_result = json.dumps(result, sort_keys=True)
    for private_value in (observer_token, health_token, private_body.decode("ascii")):
        assert private_value not in rendered_result
    for private_line in private_body.decode("ascii").splitlines()[1:-1]:
        assert private_line not in rendered_result


def test_observer_public_key_metadata_preserves_operator_and_bootstrap_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    records = _install_metadata_seam(module, monkeypatch, roots)

    _provision(module, roots)

    public_key = _paths(*roots[1:])["attestationPublicKey"]
    details = records[public_key.resolve()]
    operator_uid, _operator_gid = _operator_identity(roots[1])
    assert (details.st_uid, details.st_gid, stat.S_IMODE(details.st_mode)) == (
        operator_uid,
        1000,
        0o440,
    )


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="real numeric UID/GID access checks require a root POSIX test process",
)
@pytest.mark.parametrize("operator_uid", (0, 2001), ids=("root-operator", "nonroot-operator"))
def test_observer_public_key_real_posix_access_preserves_private_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_uid: int,
) -> None:
    module = _provisioner()
    candidate_root, _lock = write_schema3_candidate_root(tmp_path)
    external_roots = tuple(
        tmp_path / name for name in ("observer-secrets", "observer-trust", "observer-attestations")
    )
    for root in external_roots:
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    candidate_root.chmod(0o700)
    operator_gid = os.getegid() if operator_uid == 0 else operator_uid
    if operator_uid != 0:
        # Only identity selection is injected; chown/chmod/stat remain real.
        monkeypatch.setattr(
            module, "_operator_identity", lambda _path: (operator_uid, operator_gid)
        )
    _provision(module, (candidate_root, *external_roots))
    paths = _paths(*external_roots)

    synthetic_run = tmp_path / "synthetic-run"
    synthetic_run.mkdir(mode=0o700)
    synthetic_secrets = synthetic_run / "secrets"
    synthetic_secrets.mkdir(mode=0o755)
    synthetic_secrets.chmod(0o755)
    for name in (*PRIVATE_TARGETS, "attestationPublicKey"):
        target = synthetic_secrets / paths[name].name
        os.link(paths[name], target)
        source_details, target_details = paths[name].stat(), target.stat()
        assert (source_details.st_dev, source_details.st_ino) == (
            target_details.st_dev,
            target_details.st_ino,
        )

    def access_as(directory: Path, name: str, uid: int, gid: int) -> dict[str, bool]:
        # A directory FD bypasses pytest's private ancestors, not this directory's
        # search permission or the file's DAC. No key FD is inherited. Same-inode
        # access simulates a file-backed secret; this is not a Docker mount test.
        expected_sha256 = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    """
import hashlib
import json
import os
import sys

directory_fd, name, uid, gid, expected_sha256 = sys.argv[1:]
uid, gid = int(uid), int(gid)
os.setgroups([])
os.setgid(gid)
os.setuid(uid)
assert (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (uid, uid, gid, gid)
assert os.getgroups() == []
access = {}
for operation, flags in (("read", os.O_RDONLY), ("write", os.O_WRONLY)):
    try:
        descriptor = os.open(name, flags | os.O_NOFOLLOW, dir_fd=int(directory_fd))
    except PermissionError:
        access[operation] = False
    else:
        try:
            if operation == "read":
                body = os.read(descriptor, 64 * 1024)
                access[operation] = hashlib.sha256(body).hexdigest() == expected_sha256
            else:
                access[operation] = True
        finally:
            os.close(descriptor)
print(json.dumps(access))
""",
                    str(directory_fd),
                    name,
                    str(uid),
                    str(gid),
                    expected_sha256,
                ],
                cwd="/",
                pass_fds=(directory_fd,),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        finally:
            os.close(directory_fd)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    for directory in (
        *external_roots,
        paths["trustDirectory"],
        paths["runDirectory"],
        paths["attestationDirectory"],
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name, (_filename, mode) in PRIVATE_TARGETS.items():
        details = paths[name].stat()
        assert (details.st_uid, details.st_gid, stat.S_IMODE(details.st_mode)) == (1000, 1000, mode)
        assert access_as(synthetic_secrets, paths[name].name, 2002, 2002) == {
            "read": False,
            "write": False,
        }
        assert access_as(synthetic_secrets, paths[name].name, 1000, 1000) == {
            "read": True,
            "write": bool(mode & 0o200),
        }

    public_key = paths["attestationPublicKey"]
    assert access_as(public_key.parent, public_key.name, operator_uid, operator_gid)["read"]
    assert access_as(synthetic_secrets, public_key.name, 2002, 2002) == {
        "read": False,
        "write": False,
    }
    assert access_as(synthetic_secrets, public_key.name, 1000, 1000) == {
        "read": True,
        "write": False,
    }
    public_details = public_key.stat()
    assert (public_details.st_uid, public_details.st_gid, stat.S_IMODE(public_details.st_mode)) == (
        operator_uid,
        1000,
        0o440,
    )


@pytest.mark.parametrize(
    "unsafe_case",
    ("relative", "root-in-candidate", "symlink", "existing-target", "publish-failure"),
)
def test_observer_secret_provisioner_rejects_partial_or_unsafe_targets_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_case: str,
) -> None:
    module = _provisioner()

    def setup(base: Path):
        roots = _roots(base)
        _install_metadata_seam(module, monkeypatch, roots)
        guards = []
        for index, root in enumerate(roots):
            guard = root / f"existing-guard-{index}.bin"
            guard.write_bytes(f"keep-{index}".encode("ascii"))
            guards.append((guard, guard.read_bytes()))
        return roots, guards

    if unsafe_case == "existing-target":
        for index, path_name in enumerate((*PRIVATE_TARGETS, "attestationPublicKey")):
            roots, guards = setup(tmp_path / f"existing-{index}")
            paths = _paths(roots[1], roots[2], roots[3])
            target = paths[path_name]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"do-not-overwrite")

            with pytest.raises(ValueError):
                _provision(module, roots)

            assert target.read_bytes() == b"do-not-overwrite"
            assert all(path.read_bytes() == body for path, body in guards)
            for other_name in (*PRIVATE_TARGETS, "attestationPublicKey"):
                if other_name != path_name:
                    assert not paths[other_name].exists()
        return

    roots, guards = setup(tmp_path / unsafe_case)
    candidate_root, secret_root, trust_root, attestation_root = roots
    attempted_roots = roots
    expected_error: type[BaseException] | tuple[type[BaseException], ...] = ValueError
    publish_count = 0

    if unsafe_case == "relative":
        attempted_roots = (
            candidate_root,
            Path(os.path.relpath(secret_root, Path.cwd())),
            trust_root,
            attestation_root,
        )
    elif unsafe_case == "root-in-candidate":
        unsafe_trust = candidate_root / "observer-trust"
        unsafe_trust.mkdir()
        attempted_roots = candidate_root, secret_root, unsafe_trust, attestation_root
    elif unsafe_case == "symlink":
        linked_trust = tmp_path / "observer-trust-link"
        try:
            linked_trust.symlink_to(trust_root, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks are unavailable: {exc}")
        attempted_roots = candidate_root, secret_root, linked_trust, attestation_root
    else:
        original_link = module.os.link

        def interrupted_link(*args, **kwargs):
            nonlocal publish_count
            publish_count += 1
            if publish_count == 3:
                raise OSError("simulated observer secret publication failure")
            return original_link(*args, **kwargs)

        monkeypatch.setattr(module.os, "link", interrupted_link)
        expected_error = (OSError, RuntimeError)

    attempted_paths = _paths(attempted_roots[1], attempted_roots[2], attempted_roots[3])
    with pytest.raises(expected_error):
        _provision(module, attempted_roots)

    assert all(path.read_bytes() == body for path, body in guards)
    for path_name in (*PRIVATE_TARGETS, "attestationPublicKey"):
        assert not attempted_paths[path_name].exists()
    if unsafe_case == "publish-failure":
        assert publish_count == 3


def test_observer_secret_provisioner_output_maps_to_compose_preflight_and_verifier_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    _candidate_root, secret_root, trust_root, attestation_root = roots
    _install_metadata_seam(module, monkeypatch, roots)
    result = _provision(module, roots)
    paths = _paths(secret_root, trust_root, attestation_root)
    spki_sha256 = result["environment"]["YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256"]

    expected_environment = {
        "YFEISTAI_OBSERVER_SECRET_DIR": str(secret_root),
        "YFEISTAI_RELEASE_RUN_ID": RUN_ID,
        "YFEISTAI_OBSERVER_ATTESTATION_DIR": str(paths["attestationDirectory"]),
        "YFEISTAI_OBSERVER_TRUST_ROOT": str(paths["trustDirectory"]),
        "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256": spki_sha256,
    }
    for name, value in expected_environment.items():
        assert result["environment"][name] == value
    for name, path in paths.items():
        assert result["paths"][name] == str(path)
    assert result["platformPreflightInputs"]["observer_secret_dir"] == str(secret_root)
    assert result["platformPreflightInputs"]["release_run_id"] == RUN_ID

    verifier_arguments = _argument_mapping(result["verifierArguments"])
    assert verifier_arguments["--openmaic-observer-public-key"] == str(
        paths["attestationPublicKey"]
    )
    assert verifier_arguments["--openmaic-observer-public-key-spki-sha256"] == spki_sha256
    expected_outage_environment = {
        "YFEISTAI_SHARED_INGRESS_AUTH_TOKEN_FILE": str(paths["observerAuthToken"]),
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PATH": str(paths["observerAttestation"]),
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_SIGNATURE_PATH": str(
            paths["observerAttestationSignature"]
        ),
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PUBLIC_KEY_PATH": str(
            paths["attestationPublicKey"]
        ),
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PUBLIC_KEY_SPKI_SHA256": spki_sha256,
    }
    for name, value in expected_outage_environment.items():
        assert result["outageProbeEnvironment"][name] == value

    overlay = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    compose_secret_paths = {
        "openmaic_observer_auth_token": paths["observerAuthToken"],
        "openmaic_observer_health_token": paths["candidateHealthToken"],
        "openmaic_observer_attestation_signing_key": paths["attestationSigningKey"],
        "openmaic_observer_attestation_public_key": paths["attestationPublicKey"],
    }
    for name, expected_path in compose_secret_paths.items():
        assert (
            _render_compose_path(overlay["secrets"][name]["file"], expected_environment)
            == expected_path
        )
    observer_mounts = [
        volume
        for service in overlay["services"].values()
        for volume in service.get("volumes", ())
        if isinstance(volume, dict) and volume.get("target") == "/run/yfeistai-observer"
    ]
    assert observer_mounts
    assert all(
        _render_compose_path(volume["source"], expected_environment)
        == paths["attestationDirectory"]
        for volume in observer_mounts
    )

    preflight = _load(
        PREFLIGHT_PATH,
        "platform_preflight_for_observer_provisioning_test",
        missing="platform preflight is missing",
    )
    expected_proof = {
        "schemaVersion": 1,
        "releaseRunId": RUN_ID,
        "runDirectory": str(paths["runDirectory"]),
        "ownerOnly": True,
    }

    def inspect_metadata(*args, **kwargs):
        inputs = args or (
            kwargs["observer_secret_dir"],
            kwargs["release_run_id"],
            kwargs["observer_signer_image"],
        )
        assert inputs == (secret_root, RUN_ID, SIGNER_IMAGE)
        return (), expected_proof

    monkeypatch.setattr(preflight, "_inspect_observer_secret_preflight", inspect_metadata)
    preflight_result = preflight.run_preflight(
        secret_dir=tmp_path / "unused-platform-secrets",
        required_secret_names=(),
        observer_secret_dir=secret_root,
        release_run_id=RUN_ID,
        observer_signer_image=SIGNER_IMAGE,
    )
    assert preflight_result.errors == ()
    assert preflight_result.observer_secret_preflight == expected_proof


def test_committed_observer_release_manifest_is_the_single_runtime_wiring_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    candidate_root, secret_root, trust_root, attestation_root = roots
    _install_metadata_seam(module, monkeypatch, roots)
    _provision(module, roots)
    paths = _paths(secret_root, trust_root, attestation_root)

    admission = module.load_committed_observer_release_manifest(
        manifest_path=_manifest_path(trust_root),
        candidate_root=candidate_root,
        observer_signer_image=SIGNER_IMAGE,
    )

    assert admission["environment"] == {
        **_deployment_environment(),
        "YFEISTAI_OBSERVER_SECRET_DIR": str(secret_root),
        "YFEISTAI_RELEASE_RUN_ID": RUN_ID,
        "YFEISTAI_OBSERVER_ATTESTATION_DIR": str(paths["attestationDirectory"]),
        "YFEISTAI_OBSERVER_TRUST_ROOT": str(paths["trustDirectory"]),
        "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256": admission["releaseManifest"][
            "publicKeySpkiSha256"
        ],
        "YFEISTAI_CANDIDATE_SHA256": hashlib.sha256(
            json.dumps(
                admission["releaseManifest"]["candidateBinding"]["candidate"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "YFEISTAI_IMAGE_LOCK_SHA256": admission["releaseManifest"]["candidateBinding"][
            "imageLockSha256"
        ],
        "YFEISTAI_OBSERVER_SIGNER_IMAGE": SIGNER_IMAGE,
    }
    assert admission["platformPreflightInputs"] == {
        "observer_secret_dir": str(secret_root),
        "release_run_id": RUN_ID,
        "observer_signer_image": SIGNER_IMAGE,
    }
    assert admission["releaseManifest"]["status"] == "committed"
    assert admission["paths"] == {
        **{name: str(path) for name, path in paths.items()},
        "manifest": str(_manifest_path(trust_root)),
    }


def test_committed_observer_release_manifest_rejects_same_path_candidate_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    candidate_root, _secret_root, trust_root, _attestation_root = roots
    _install_metadata_seam(module, monkeypatch, roots)
    _provision(module, roots)
    manifest_path = _manifest_path(trust_root)
    manifest_before = manifest_path.read_bytes()

    _write_candidate_lock(
        candidate_root,
        source_head="b" * 40,
        release_tag="yfeistai-first-release-20260831-bbbbbbbb",
    )

    with pytest.raises(ValueError, match="observer release manifest candidate binding"):
        module.load_committed_observer_release_manifest(
            manifest_path=manifest_path,
            candidate_root=candidate_root,
            observer_signer_image=SIGNER_IMAGE,
        )
    assert manifest_path.read_bytes() == manifest_before


@pytest.mark.parametrize(
    "mutation",
    ("missing-image", "workload-digest-drift", "tagless-signer"),
)
def test_observer_secret_provisioner_rejects_invalid_candidate_image_lock_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    candidate_root, _secret_root, _trust_root, _attestation_root = roots
    _install_metadata_seam(module, monkeypatch, roots)
    lock_path = candidate_root / "deploy" / "image-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if mutation == "missing-image":
        lock["images"].pop("nginx")
    elif mutation == "workload-digest-drift":
        record = lock["images"]["deeptutor"]
        record["digest"] = "sha256:" + "9" * 64
        record["reference"] = f"{record['repository']}:{record['tag']}@{record['digest']}"
    else:
        signer = lock["supportImages"]["observer_signer"]
        signer["reference"] = f"{signer['repository']}@{signer['digest']}"
    lock_path.write_text(
        json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="observer release manifest candidate binding"):
        _provision(module, roots)


def test_observer_secret_provisioner_rejects_zero_source_head_before_key_material_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    candidate_root, secret_root, trust_root, attestation_root = roots
    _write_candidate_lock(
        candidate_root,
        source_head="0" * 40,
        release_tag="yfeistai-first-release-20260831-00000000",
    )
    _install_metadata_seam(module, monkeypatch, roots)

    def reject_key_material_generation() -> tuple[bytes, bytes, bytes, bytes, str]:
        pytest.fail("key material generation started for invalid candidate")

    monkeypatch.setattr(module, "_new_material", reject_key_material_generation)

    with pytest.raises(ValueError, match="observer release manifest candidate binding"):
        _provision(module, roots)

    paths = _paths(secret_root, trust_root, attestation_root)
    assert not paths["runDirectory"].exists()
    assert not paths["trustDirectory"].exists()
    assert not paths["attestationDirectory"].exists()


@pytest.mark.parametrize(
    "mutation",
    ("uncommitted", "path-drift", "candidate-drift", "invalid-signer"),
)
def test_observer_release_manifest_consumer_rejects_uncommitted_or_drifted_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    candidate_root, secret_root, trust_root, attestation_root = roots
    _install_metadata_seam(module, monkeypatch, roots)
    _provision(module, roots)
    paths = _paths(secret_root, trust_root, attestation_root)
    manifest_path = _manifest_path(trust_root)
    signer_image = SIGNER_IMAGE

    if mutation == "invalid-signer":
        signer_image = "ghcr.io/xinlingzhifei/yfeistai-observer-signer:first-release"
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if mutation == "uncommitted":
            manifest["status"] = "uncommitted"
        elif mutation == "path-drift":
            manifest["paths"]["observerAuthToken"] = str(tmp_path / "foreign-token")
        else:
            manifest["roots"]["candidateRoot"] = str(tmp_path / "other-candidate")
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="observer release manifest|observer signer image"):
        module.load_committed_observer_release_manifest(
            manifest_path=manifest_path,
            candidate_root=candidate_root,
            observer_signer_image=signer_image,
        )


@pytest.mark.parametrize("root_role", ("secret", "trust", "attestation"))
def test_observer_secret_provisioner_rejects_owned_child_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_role: str,
) -> None:
    module = _provisioner()
    roots = _roots(tmp_path)
    _candidate_root, secret_root, trust_root, attestation_root = roots
    records = _install_metadata_seam(module, monkeypatch, roots)
    paths = _paths(secret_root, trust_root, attestation_root)
    child_paths = {
        "secret": paths["runDirectory"],
        "trust": paths["trustDirectory"],
        "attestation": paths["attestationDirectory"],
    }
    original_write_owner_marker = module._write_owner_marker
    replaced = False

    def replace_child_after_all_directories_exist(*args, **kwargs):
        nonlocal replaced
        original_write_owner_marker(*args, **kwargs)
        if replaced or kwargs["root_role"] != "attestation":
            return
        replaced = True
        target = child_paths[root_role]
        displaced = tmp_path / f"displaced-{root_role}-run"
        original_files = {
            path.relative_to(target): path.read_bytes()
            for path in target.rglob("*")
            if path.is_file()
        }
        target.rename(displaced)
        target.mkdir()
        for relative_path, body in original_files.items():
            restored = target / relative_path
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(body)
        for recorded_path in tuple(records):
            if recorded_path == target or recorded_path.is_relative_to(target):
                records.pop(recorded_path)

    monkeypatch.setattr(
        module,
        "_write_owner_marker",
        replace_child_after_all_directories_exist,
    )

    with pytest.raises(ValueError, match=f"observer {root_role} release directory changed"):
        _provision(module, roots)
