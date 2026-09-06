from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from tests.scripts.gateway_public_test_support import canonical_inputs

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = {
    "sourceRepository": "xinlingzhifei/DeepTutor",
    "sourceHead": "a" * 40,
    "releaseTag": "yfeistai-first-release-20260831-aaaaaaaa",
    "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
    "imageDigests": {
        "deeptutor": "sha256:" + "1" * 64,
        "openmaic": "sha256:" + "2" * 64,
        "openmaic_render": "sha256:" + "3" * 64,
    },
}
RELEASE_RUN = {
    "runId": "first-release-run-20260831",
    "environmentId": "acceptance-external-01",
}
MACHINE_ID = "0123456789ABCDEF0123456789abcdef"
MACHINE_ID_DOMAIN = b"yfeistai.gateway-physical-host-id.v1\0"
CHALLENGES = {
    "schemaVersion": 1,
    "observerChallenge": "8" * 64,
    "hostChallenge": "9" * 64,
}
_CANDIDATE_ARTIFACT_RELATIVE_PATHS = (
    "deploy/image-lock.json",
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.data-plane.yml",
    "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
)


def _load_source_module(filename: str, module_name: str):
    path = ROOT / "scripts" / filename
    assert path.is_file(), f"{filename} is missing"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_module():
    return _load_source_module(
        "gateway_trust_producer.py",
        "gateway_trust_producer_under_test",
    )


def _write_key_pair(root: Path, name: str) -> tuple[Path, bytes]:
    private_key = Ed25519PrivateKey.generate()
    private_path = root / f"{name}.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_body = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_path, public_body


def _authorize_test_private_key(monkeypatch: pytest.MonkeyPatch, producer) -> None:
    monkeypatch.setattr(
        producer,
        "_assert_private_key_permissions",
        lambda *_args, **_kwargs: None,
    )


def _authorize_test_publication(monkeypatch: pytest.MonkeyPatch, producer) -> None:
    monkeypatch.setattr(producer, "_assert_formal_publication_authority", lambda: None)


def _fix_signing_clock(monkeypatch: pytest.MonkeyPatch, producer) -> None:
    monkeypatch.setattr(
        producer,
        "_current_utc",
        lambda: datetime(2026, 8, 30, 4, 5, tzinfo=timezone.utc),
    )


def _empty_candidate_root(tmp_path: Path, *, name: str = "candidate") -> Path:
    candidate_root = tmp_path / name
    candidate_root.mkdir()
    return candidate_root


def _write_candidate_contract(
    tmp_path: Path,
    *,
    name: str = "candidate",
    candidate: dict[str, object] = CANDIDATE,
    fallback_digest_index: int = 4,
) -> Path:
    renderer = _load_source_module(
        "render_platform_compose.py",
        f"render_platform_compose_for_gateway_trust_{name.replace('-', '_')}",
    )
    candidate_root = tmp_path / name
    deploy_root = candidate_root / "deploy"
    deploy_root.mkdir(parents=True)
    compose_relative_paths = _CANDIDATE_ARTIFACT_RELATIVE_PATHS[1:]
    compose_paths = tuple(candidate_root / relative for relative in compose_relative_paths)
    for relative, destination in zip(compose_relative_paths, compose_paths, strict=True):
        source = ROOT / relative
        destination.write_bytes(source.read_bytes())

    custom_digests = candidate["imageDigests"]
    custom_digest_by_repository = {
        renderer.IMAGE_SPECS[name]["repository"]: custom_digests[name]
        for name in renderer.CUSTOM_IMAGE_NAMES
    }

    def resolve_digest(reference: str) -> str:
        nonlocal fallback_digest_index
        repository = reference.split(":", 1)[0]
        custom_digest = custom_digest_by_repository.get(repository)
        if custom_digest is not None:
            return custom_digest
        digest = "sha256:" + f"{fallback_digest_index:064x}"
        fallback_digest_index += 1
        return digest

    lock = renderer.write_image_lock(
        deploy_root / "image-lock.json",
        digest_resolver=resolve_digest,
        compose_paths=compose_paths,
        source_repository=candidate["sourceRepository"],
        source_head=candidate["sourceHead"],
        release_tag=candidate["releaseTag"],
        openmaic_head=candidate["openmaicHead"],
    )
    assert lock["candidate"] == candidate
    return candidate_root


def _candidate_contract_claim(candidate_root: Path) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "artifacts": {
            relative: {
                "sha256": hashlib.sha256((candidate_root / relative).read_bytes()).hexdigest()
            }
            for relative in _CANDIDATE_ARTIFACT_RELATIVE_PATHS
        },
    }


def _candidate_descriptor_is_closed(producer, lease: object) -> bool:
    try:
        opened = producer.os.fstat(lease.descriptor)
    except OSError:
        return True
    return producer._file_identity(opened) != lease.identity


def _write_canonical_mapping(producer, path: Path, value: dict[str, object]) -> Path:
    path.write_bytes(producer._canonical_json(value))
    return path


def _publish_test_body(path: Path, body: bytes) -> None:
    Path(path).write_bytes(body)


def test_host_receipt_uses_normalized_domain_separated_linux_machine_id(
    tmp_path: Path,
) -> None:
    producer = _load_module()
    candidate_root = _write_candidate_contract(tmp_path)

    body = producer.build_gateway_host_receipt(
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        docker_endpoint="unix:///var/run/docker.sock",
        docker_server_id="docker-server-01",
        machine_id_reader=lambda: f"  {MACHINE_ID}\n".encode(),
    )

    expected_host_id = hashlib.sha256(
        MACHINE_ID_DOMAIN + MACHINE_ID.lower().encode("ascii")
    ).hexdigest()
    assert body.endswith(b"\n")
    assert json.loads(body) == {
        "schemaVersion": 2,
        "producer": "gateway-docker-host-provisioner",
        "candidate": CANDIDATE,
        "candidateContract": _candidate_contract_claim(candidate_root),
        "releaseRun": RELEASE_RUN,
        "environmentId": RELEASE_RUN["environmentId"],
        "host": {
            "physicalHostIdSha256": expected_host_id,
            "dockerContext": "default",
            "dockerEndpoint": "unix:///var/run/docker.sock",
            "dockerServerId": "docker-server-01",
            "osType": "linux",
        },
    }


def test_host_receipt_rejects_whole_candidate_set_aba_during_semantic_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    candidate_root = _write_candidate_contract(tmp_path)
    alternate_root = _write_candidate_contract(
        tmp_path,
        name="candidate-aba-b",
        fallback_digest_index=40,
    )
    candidate_claim = _candidate_contract_claim(candidate_root)
    alternate_claim = _candidate_contract_claim(alternate_root)
    assert alternate_claim != candidate_claim
    renderer = _load_source_module(
        "render_platform_compose.py",
        "render_platform_compose_for_host_cached_candidate_validation",
    )
    expected_cached_artifacts = tuple(
        (Path(relative), (candidate_root / relative).read_bytes())
        for relative in _CANDIDATE_ARTIFACT_RELATIVE_PATHS
    )
    original_opener = producer._open_candidate_contract_artifact
    original_validator = producer.validate_image_lock_bindings
    opened_leases: list[object] = []
    semantic_lease_counts: list[int] = []
    semantic_claims: list[dict[str, object]] = []
    cached_semantic_calls: list[tuple[bytes, ...]] = []

    def track_candidate_lease(path: Path) -> object:
        lease = original_opener(path)
        opened_leases.append(lease)
        return lease

    def validate_alternate_semantic_bytes(
        _image_lock_path: Path,
        *,
        compose_paths: tuple[Path, ...],
        require_candidate: bool,
    ) -> dict[str, object]:
        active_leases = 0
        for lease in opened_leases:
            try:
                opened = producer.os.fstat(lease.descriptor)
                current = lease.path.lstat()
            except OSError:
                continue
            if (
                producer._file_identity(opened)
                == lease.identity
                == producer._file_identity(current)
            ):
                active_leases += 1
        semantic_lease_counts.append(active_leases)
        semantic_claims.append(alternate_claim)
        alternate_paths = producer.candidate_artifact_paths(alternate_root)
        assert len(compose_paths) == 4
        return original_validator(
            alternate_paths.image_lock,
            compose_paths=(
                alternate_paths.base_compose,
                alternate_paths.platform_compose,
                alternate_paths.data_plane_compose,
                alternate_paths.observer_overlay,
            ),
            require_candidate=require_candidate,
        )

    def validate_cached_candidate_bytes(
        artifact_contents: tuple[tuple[Path, bytes], ...],
        *,
        require_candidate: bool,
    ) -> dict[str, object]:
        def relative_artifact_path(path: Path) -> Path:
            source = Path(path)
            return source.relative_to(candidate_root) if source.is_absolute() else source

        normalized = tuple(
            (relative_artifact_path(path), content) for path, content in artifact_contents
        )
        assert normalized == expected_cached_artifacts
        assert len({path for path, _content in normalized}) == 5
        cached_semantic_calls.append(tuple(content for _path, content in normalized))
        lock = renderer.load_image_lock_bytes(
            normalized[0][1],
            require_candidate=require_candidate,
        )
        renderer._validate_image_lock_binding_bytes(lock, normalized[1:])
        return {
            "candidate": json.loads(json.dumps(lock["candidate"])),
            "candidateContract": json.loads(json.dumps(candidate_claim)),
        }

    monkeypatch.setattr(producer, "_open_candidate_contract_artifact", track_candidate_lease)
    monkeypatch.setattr(
        producer,
        "validate_image_lock_bindings",
        validate_alternate_semantic_bytes,
    )
    monkeypatch.setattr(
        producer,
        "validate_gateway_candidate_contract_bytes",
        validate_cached_candidate_bytes,
        raising=False,
    )

    receipt_body: bytes | None = None
    error: ValueError | None = None
    try:
        receipt_body = producer.build_gateway_host_receipt(
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            docker_endpoint="unix:///var/run/docker.sock",
            docker_server_id="docker-server-01",
            machine_id_reader=lambda: MACHINE_ID.encode(),
        )
    except ValueError as caught:
        error = caught

    assert all(_candidate_descriptor_is_closed(producer, lease) for lease in opened_leases)
    assert not (semantic_claims and cached_semantic_calls)
    if cached_semantic_calls:
        assert cached_semantic_calls == [
            tuple(content for _path, content in expected_cached_artifacts)
        ]
        assert error is None
        assert receipt_body is not None
        receipt = json.loads(receipt_body)
        assert receipt["candidate"] == CANDIDATE
        assert receipt["candidateContract"] == candidate_claim
        return

    assert semantic_claims, "candidate semantic validation was bypassed"
    assert semantic_claims == [alternate_claim]
    assert semantic_lease_counts == [5]
    assert error is not None
    assert "candidate contract" in str(error)


@pytest.mark.parametrize(
    "machine_id",
    [b"0" * 32, b"short", b"0123456789abcdef-0123456789abcdef", b"\xff" * 32],
)
def test_host_receipt_rejects_noncanonical_machine_identity(
    tmp_path: Path,
    machine_id: bytes,
) -> None:
    producer = _load_module()
    candidate_root = _write_candidate_contract(tmp_path)

    with pytest.raises(ValueError, match="machine-id is invalid"):
        producer.build_gateway_host_receipt(
            candidate_root=candidate_root,
            release_run=RELEASE_RUN,
            docker_endpoint="unix:///var/run/docker.sock",
            docker_server_id="docker-server-01",
            machine_id_reader=lambda: machine_id,
        )


def test_observer_attestation_is_derived_from_canonical_raw_probe() -> None:
    producer = _load_module()
    gateway_public_contract = _load_source_module(
        "gateway_public_contract.py",
        "gateway_public_contract_for_producer_test",
    )
    raw_body, expected_attestation, _digest = canonical_inputs(
        gateway_public_contract,
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        runtime_sha256="4" * 64,
    )

    actual = producer.build_gateway_observer_attestation(
        raw_body,
        observed_at="2026-08-30T04:00:02Z",
    )

    assert actual == expected_attestation


def test_role_signatures_and_host_receipt_bind_exact_schema3_candidate_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    contract = _load_source_module(
        "gateway_trust_contract.py",
        "gateway_trust_contract_for_producer_test",
    )
    public_contract = _load_source_module(
        "gateway_public_contract.py",
        "gateway_public_contract_for_pair_test",
    )
    raw_body, observer_artifact, _digest = canonical_inputs(
        public_contract,
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        runtime_sha256="4" * 64,
    )
    assert raw_body
    candidate_root = _write_candidate_contract(tmp_path)
    expected_candidate_contract = _candidate_contract_claim(candidate_root)
    release_run_path = _write_canonical_mapping(
        producer,
        tmp_path / "release-run.json",
        RELEASE_RUN,
    )
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    host_receipt_path = trusted_root / "gateway-docker-host-provisioning-receipt.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    monkeypatch.setattr(producer, "_publish_no_replace", _publish_test_body)
    assert (
        producer.main(
            [
                "host-receipt",
                "--candidate-root",
                str(candidate_root),
                "--release-run",
                str(release_run_path),
                "--output",
                str(host_receipt_path),
            ]
        )
        == 0
    )
    host_receipt = host_receipt_path.read_bytes()
    observer_private, observer_public = _write_key_pair(tmp_path, "observer")
    host_private, host_public = _write_key_pair(tmp_path, "host")
    observer_envelope = producer.GatewayObserverSigner.from_private_key_file(
        observer_private,
        candidate_root=candidate_root,
    ).sign(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        challenges=CHALLENGES,
        artifact_body=observer_artifact,
        issued_at="2026-08-30T04:00:00Z",
        expires_at="2026-08-30T04:10:00Z",
    )
    host_envelope = producer.GatewayHostProvisionerSigner.from_private_key_file(
        host_private,
        candidate_root=candidate_root,
    ).sign(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        challenges=CHALLENGES,
        artifact_body=host_receipt,
        issued_at="2026-08-30T04:00:00Z",
        expires_at="2026-08-30T04:10:00Z",
    )
    keyring_body = producer.build_gateway_trust_keyring(
        observer_public_key_body=observer_public,
        host_public_key_body=host_public,
    )
    keyring_path = trusted_root / "gateway-trust-keyring.json"
    keyring_path.write_bytes(keyring_body)
    verified = contract._verify_gateway_trust_pair(
        observer_envelope_body=observer_envelope,
        host_envelope_body=host_envelope,
        observer_artifact_body=observer_artifact,
        host_receipt_body=host_receipt,
        trusted_keyring_path=keyring_path,
        expected_trusted_keyring_sha256=hashlib.sha256(keyring_body).hexdigest(),
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        expected_environment_id=RELEASE_RUN["environmentId"],
        expected_observer_challenge=CHALLENGES["observerChallenge"],
        expected_host_challenge=CHALLENGES["hostChallenge"],
        trusted_now="2026-08-30T04:05:00Z",
        trusted_keyring_platform_name=None,
        trusted_owner_uid=None,
    )

    assert (
        verified["observer"]["claims"]["artifactSha256"]
        == hashlib.sha256(observer_artifact).hexdigest()
    )
    assert verified["hostReceipt"] == json.loads(host_receipt)
    observer_document = json.loads(observer_envelope)
    host_document = json.loads(host_envelope)
    host_receipt_document = json.loads(host_receipt)
    assert [
        observer_document["payload"].get("candidateContract"),
        host_document["payload"].get("candidateContract"),
        host_receipt_document.get("candidateContract"),
    ] == [expected_candidate_contract] * 3
    assert json.loads(keyring_body)["keys"][0]["publicKeySpkiBase64"] == base64.b64encode(
        serialization.load_pem_public_key(observer_public).public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")


@pytest.mark.parametrize(
    "challenges",
    [
        {**CHALLENGES, "observerChallenge": "0" * 64},
        {**CHALLENGES, "hostChallenge": CHALLENGES["observerChallenge"]},
    ],
)
def test_single_role_signer_rejects_zero_or_shared_challenges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    challenges: dict[str, object],
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _empty_candidate_root(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "observer")
    signer = producer.GatewayObserverSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    )

    with pytest.raises(ValueError, match="independent challenges"):
        signer.sign(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            challenges=challenges,
            artifact_body=b"artifact",
            issued_at="2026-08-30T04:00:00Z",
            expires_at="2026-08-30T04:10:00Z",
        )


def test_signed_envelope_is_role_fixed_and_tamper_evident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    contract = _load_source_module(
        "gateway_trust_contract.py",
        "gateway_trust_contract_for_tamper_test",
    )
    private_path, public_body = _write_key_pair(tmp_path, "observer")
    public_contract = _load_source_module(
        "gateway_public_contract.py",
        "gateway_public_contract_for_tamper_artifact",
    )
    _raw, artifact, _digest = canonical_inputs(
        public_contract,
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        runtime_sha256="4" * 64,
    )
    envelope = producer.GatewayObserverSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    ).sign(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        challenges=CHALLENGES,
        artifact_body=artifact,
        issued_at="2026-08-30T04:00:00Z",
        expires_at="2026-08-30T04:10:00Z",
    )
    document = json.loads(envelope)
    assert document["payloadType"] == "gateway-observer"
    assert document["payload"]["issuer"] == "external-observer-authority"
    assert document["payload"]["claims"]["artifact"] == (
        "runtime/gateway-external-observer-attestation.json"
    )
    document["payload"]["environmentId"] = "tampered-environment"
    tampered = producer._canonical_json(document)
    keyring = producer.build_gateway_trust_keyring(
        observer_public_key_body=public_body,
        host_public_key_body=_write_key_pair(tmp_path, "host")[1],
    )
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    keyring_path = trusted / "keyring.json"
    keyring_path.write_bytes(keyring)
    with pytest.raises(ValueError, match="signature is invalid"):
        contract._parse_gateway_trust_envelope(
            tampered,
            expected_payload_type="gateway-observer",
            trusted_keyring_path=keyring_path,
            candidate_root=candidate_root,
            candidate=CANDIDATE,
            candidate_contract=_candidate_contract_claim(candidate_root),
            release_run=RELEASE_RUN,
            expected_environment_id=RELEASE_RUN["environmentId"],
            expected_challenge=CHALLENGES["observerChallenge"],
            trusted_now="2026-08-30T04:05:00Z",
        )


def test_keyring_writer_accepts_only_public_keys_and_never_replaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_publication(monkeypatch, producer)
    observer_private, observer_public = _write_key_pair(tmp_path, "observer")
    _host_private, host_public = _write_key_pair(tmp_path, "host")
    output = tmp_path / "trusted" / "gateway-keyring.json"
    output.parent.mkdir()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    observer_public_path = tmp_path / "observer-public.pem"
    host_public_path = tmp_path / "host-public.pem"
    observer_public_path.write_bytes(observer_public)
    host_public_path.write_bytes(host_public)

    written = producer.write_gateway_trust_keyring(
        output_path=output,
        candidate_root=candidate_root,
        observer_public_key_path=observer_public_path,
        host_public_key_path=host_public_path,
    )

    assert written == output
    first = output.read_bytes()
    assert first == producer.build_gateway_trust_keyring(
        observer_public_key_body=observer_public,
        host_public_key_body=host_public,
    )
    if sys.platform != "win32":
        assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        producer.write_gateway_trust_keyring(
            output_path=output,
            candidate_root=candidate_root,
            observer_public_key_path=observer_public_path,
            host_public_key_path=host_public_path,
        )
    assert output.read_bytes() == first
    with pytest.raises(ValueError, match="public key"):
        producer.build_gateway_trust_keyring(
            observer_public_key_body=observer_private.read_bytes(),
            host_public_key_body=host_public,
        )

    with pytest.raises(ValueError, match="outside candidate root"):
        producer.write_gateway_trust_keyring(
            output_path=candidate_root / "gateway-keyring.json",
            candidate_root=candidate_root,
            observer_public_key_path=observer_public_path,
            host_public_key_path=host_public_path,
        )


def test_failed_keyring_publication_cleans_private_staging_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_publication(monkeypatch, producer)
    _observer_private, observer_public = _write_key_pair(tmp_path, "observer")
    _host_private, host_public = _write_key_pair(tmp_path, "host")
    output_dir = tmp_path / "trusted"
    output_dir.mkdir()
    output = output_dir / "gateway-keyring.json"
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    observer_public_path = tmp_path / "observer-public.pem"
    host_public_path = tmp_path / "host-public.pem"
    observer_public_path.write_bytes(observer_public)
    host_public_path.write_bytes(host_public)

    def fail_link(_source: Path, _target: Path) -> None:
        raise PermissionError("synthetic publish failure")

    monkeypatch.setattr(producer.os, "link", fail_link)
    with pytest.raises(PermissionError, match="synthetic publish failure"):
        producer.write_gateway_trust_keyring(
            output_path=output,
            candidate_root=candidate_root,
            observer_public_key_path=observer_public_path,
            host_public_key_path=host_public_path,
        )

    assert not output.exists()
    assert list(output_dir.iterdir()) == []


def test_keyring_writer_reports_candidate_change_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    moved_candidate = tmp_path / "candidate-original"
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    output = trusted_root / "gateway-keyring.json"
    _observer_private, observer_public = _write_key_pair(tmp_path, "observer")
    _host_private, host_public = _write_key_pair(tmp_path, "host")
    observer_public_path = trusted_root / "observer-public.pem"
    host_public_path = trusted_root / "host-public.pem"
    observer_public_path.write_bytes(observer_public)
    host_public_path.write_bytes(host_public)

    def publish_then_exchange(path: Path, body: bytes) -> None:
        path.write_bytes(body)
        candidate_root.rename(moved_candidate)
        candidate_root.mkdir()

    monkeypatch.setattr(producer, "_publish_no_replace", publish_then_exchange)
    with pytest.raises(producer.GatewayTrustCommittedError) as raised:
        producer.write_gateway_trust_keyring(
            output_path=output,
            candidate_root=candidate_root,
            observer_public_key_path=observer_public_path,
            host_public_key_path=host_public_path,
        )

    assert raised.value.output_path == output
    assert output.is_file()


def test_private_key_permission_policy_is_explicit_for_posix_and_windows() -> None:
    producer = _load_module()
    restricted = SimpleNamespace(st_mode=0o100600, st_uid=1000)
    exposed = SimpleNamespace(st_mode=0o100640, st_uid=1000)

    producer._assert_private_key_permissions(
        restricted,
        platform_name="posix",
        effective_user_id=1000,
    )
    with pytest.raises(ValueError, match="permissions are not restricted"):
        producer._assert_private_key_permissions(
            exposed,
            platform_name="posix",
            effective_user_id=1000,
        )
    with pytest.raises(ValueError, match="Windows private key DACL cannot be verified"):
        producer._assert_private_key_permissions(
            exposed,
            platform_name="nt",
            effective_user_id=None,
        )


def test_private_key_reader_rejects_symlink_or_reparse_input(tmp_path: Path) -> None:
    producer = _load_module()
    candidate_root = _write_candidate_contract(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "observer")
    alias = tmp_path / "observer-alias.pem"
    try:
        alias.symlink_to(private_path)
    except OSError:
        pytest.skip("test host cannot create a file symlink")

    with pytest.raises(ValueError, match="path is unsafe"):
        producer.GatewayObserverSigner.from_private_key_file(
            alias,
            candidate_root=candidate_root,
        )


def test_cli_has_separate_fixed_role_commands_and_no_secret_value_options() -> None:
    producer = _load_module()

    help_text = producer._parser().format_help()

    assert "sign-observer" in help_text
    assert "sign-host" in help_text
    assert "--role" not in help_text
    assert "--token" not in help_text
    assert "--secret" not in help_text
    assert "--allow-insecure" not in help_text


def test_cli_does_not_echo_invalid_private_key_material(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    secret_marker = "DO-NOT-ECHO-PRIVATE-KEY-MATERIAL"
    private_path = tmp_path / "observer-private.pem"
    private_path.write_text(secret_marker, encoding="utf-8")
    private_path.chmod(0o600)
    candidate_path = tmp_path / "candidate.json"
    release_run_path = tmp_path / "release-run.json"
    challenges_path = tmp_path / "challenges.json"
    artifact_path = tmp_path / "observer-attestation.json"
    candidate_root = _write_candidate_contract(tmp_path)
    for path, value in (
        (candidate_path, CANDIDATE),
        (release_run_path, RELEASE_RUN),
        (challenges_path, CHALLENGES),
    ):
        path.write_bytes(producer._canonical_json(value))
    artifact_path.write_bytes(b"artifact\n")

    exit_code = producer.main(
        [
            "sign-observer",
            "--candidate-root",
            str(candidate_root),
            "--candidate",
            str(candidate_path),
            "--release-run",
            str(release_run_path),
            "--challenges",
            str(challenges_path),
            "--artifact",
            str(artifact_path),
            "--private-key",
            str(private_path),
            "--issued-at",
            "2026-08-30T04:00:00Z",
            "--expires-at",
            "2026-08-30T04:10:00Z",
            "--output",
            str(tmp_path / "observer-envelope.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "gateway private key is invalid" in captured.err
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err
    assert not (tmp_path / "observer-envelope.json").exists()


@pytest.mark.parametrize("inside_role", ("observer", "host"))
def test_keyring_writer_rejects_public_key_sources_inside_candidate(
    tmp_path: Path,
    inside_role: str,
) -> None:
    producer = _load_module()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    _observer_private, observer_public = _write_key_pair(tmp_path, "observer")
    _host_private, host_public = _write_key_pair(tmp_path, "host")
    observer_public_path = (
        candidate_root if inside_role == "observer" else trusted_root
    ) / "observer-public.pem"
    host_public_path = (
        candidate_root if inside_role == "host" else trusted_root
    ) / "host-public.pem"
    observer_public_path.write_bytes(observer_public)
    host_public_path.write_bytes(host_public)

    with pytest.raises(ValueError, match="public key source must be outside candidate root"):
        producer.write_gateway_trust_keyring(
            output_path=trusted_root / "gateway-keyring.json",
            candidate_root=candidate_root,
            observer_public_key_path=observer_public_path,
            host_public_key_path=host_public_path,
        )


def test_windows_private_key_authority_fails_closed() -> None:
    producer = _load_module()
    file_stat = SimpleNamespace(st_mode=0o100600, st_uid=1000)

    with pytest.raises(ValueError, match="Windows private key DACL cannot be verified"):
        producer._assert_private_key_permissions(
            file_stat,
            platform_name="nt",
            effective_user_id=None,
        )


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "message"),
    [
        ("2026-08-30T04:00:00Z", "2036-08-30T04:00:00Z", "lifetime"),
        ("2026-08-30T03:59:00Z", "2026-08-30T04:06:00Z", "too old"),
    ],
    ids=("ten-year-lifetime", "stale-but-unexpired"),
)
def test_signer_rejects_excessive_or_stale_envelope_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issued_at: str,
    expires_at: str,
    message: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    candidate_root = _empty_candidate_root(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "observer")
    monkeypatch.setattr(
        producer,
        "_current_utc",
        lambda: datetime(2026, 8, 30, 4, 5, tzinfo=timezone.utc),
        raising=False,
    )

    with pytest.raises(ValueError, match=message):
        producer.GatewayObserverSigner.from_private_key_file(
            private_path,
            candidate_root=candidate_root,
        ).sign(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            challenges=CHALLENGES,
            artifact_body=b"observer-attestation\n",
            issued_at=issued_at,
            expires_at=expires_at,
        )


def test_signer_allows_small_future_clock_skew(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "observer")
    public_contract = _load_source_module(
        "gateway_public_contract.py",
        "gateway_public_contract_for_future_skew",
    )
    _raw, artifact, _digest = canonical_inputs(
        public_contract,
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        runtime_sha256="4" * 64,
    )

    envelope = producer.GatewayObserverSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    ).sign(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        challenges=CHALLENGES,
        artifact_body=artifact,
        issued_at="2026-08-30T04:05:04Z",
        expires_at="2026-08-30T04:10:00Z",
    )

    assert json.loads(envelope)["payload"]["issuedAt"] == "2026-08-30T04:05:04Z"


@pytest.mark.parametrize("race", ("ctime", "mode"))
def test_private_key_read_rejects_restored_mtime_or_chmod_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    private_path, _public = _write_key_pair(tmp_path, "observer")
    real_fstat = producer.os.fstat
    calls = 0

    def raced_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        raw = real_fstat(descriptor)
        values = {
            "st_dev": raw.st_dev,
            "st_ino": raw.st_ino,
            "st_size": raw.st_size,
            "st_mtime_ns": raw.st_mtime_ns,
            "st_ctime_ns": raw.st_ctime_ns,
            "st_mode": raw.st_mode,
            "st_uid": getattr(raw, "st_uid", 0),
            "st_file_attributes": getattr(raw, "st_file_attributes", 0),
        }
        if calls == 2:
            if race == "ctime":
                values["st_ctime_ns"] += 1
            else:
                values["st_mode"] ^= 0o040
        return SimpleNamespace(**values)

    monkeypatch.setattr(producer.os, "fstat", raced_fstat)
    with pytest.raises(ValueError, match="changed while reading"):
        producer._read_regular_file_no_follow(
            private_path,
            label="gateway private key",
            private=True,
        )


def test_publication_detects_parent_directory_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_publication(monkeypatch, producer)
    output_parent = tmp_path / "trusted"
    output_parent.mkdir()
    moved_parent = tmp_path / "trusted-original"
    output = output_parent / "gateway-keyring.json"
    real_link = producer.os.link

    def exchange_then_link(source: Path, target: Path, **_kwargs: object) -> None:
        output_parent.rename(moved_parent)
        output_parent.mkdir()
        real_link(moved_parent / Path(source).name, target)

    monkeypatch.setattr(producer.os, "link", exchange_then_link)
    with pytest.raises(RuntimeError, match="parent identity"):
        producer._publish_no_replace(output, b"evidence\n")
    assert not output.exists()


def test_publication_reports_committed_error_after_link_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_publication(monkeypatch, producer)
    output_parent = tmp_path / "trusted"
    output_parent.mkdir()
    output = output_parent / "gateway-keyring.json"

    def fail_fsync(_path: Path) -> None:
        raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(producer, "_fsync_directory", fail_fsync)
    with pytest.raises(RuntimeError, match="committed") as raised:
        producer._publish_no_replace(output, b"evidence\n")
    assert raised.value.output_path == output
    assert output.read_bytes() == b"evidence\n"


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("observer-candidate", "candidate contract"),
        ("observer-run", "artifact"),
        ("observer-role", "artifact"),
        ("host-role", "artifact"),
    ),
)
def test_signer_parses_role_artifact_and_candidate_run_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    public_contract = _load_source_module(
        "gateway_public_contract.py",
        f"gateway_public_contract_for_{case}",
    )
    _raw, observer_artifact, _digest = canonical_inputs(
        public_contract,
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        runtime_sha256="4" * 64,
    )
    host_artifact = producer.build_gateway_host_receipt(
        candidate_root=candidate_root,
        release_run=RELEASE_RUN,
        docker_endpoint="unix:///var/run/docker.sock",
        docker_server_id="docker-server-01",
        machine_id_reader=lambda: MACHINE_ID.encode(),
    )
    private_path, _public = _write_key_pair(tmp_path, "role")
    candidate = dict(CANDIDATE)
    release_run = dict(RELEASE_RUN)
    signer_type = producer.GatewayObserverSigner
    artifact = observer_artifact
    if case == "observer-candidate":
        candidate["sourceHead"] = "b" * 40
    elif case == "observer-run":
        release_run["runId"] = "different-run"
    elif case == "observer-role":
        artifact = host_artifact
    else:
        signer_type = producer.GatewayHostProvisionerSigner
        artifact = observer_artifact

    with pytest.raises(ValueError, match=message):
        signer_type.from_private_key_file(
            private_path,
            candidate_root=candidate_root,
        ).sign(
            candidate=candidate,
            release_run=release_run,
            challenges=CHALLENGES,
            artifact_body=artifact,
            issued_at="2026-08-30T04:00:00Z",
            expires_at="2026-08-30T04:10:00Z",
        )


def _host_receipt_with_facts(
    producer,
    facts: dict[str, object],
    *,
    candidate_contract: dict[str, object],
) -> bytes:
    return producer._canonical_json(
        {
            "schemaVersion": 2,
            "producer": "gateway-docker-host-provisioner",
            "candidate": CANDIDATE,
            "candidateContract": candidate_contract,
            "releaseRun": RELEASE_RUN,
            "environmentId": RELEASE_RUN["environmentId"],
            "host": facts,
        }
    )


def _authority_facts() -> dict[str, object]:
    return {
        "physicalHostIdSha256": hashlib.sha256(
            MACHINE_ID_DOMAIN + MACHINE_ID.lower().encode("ascii")
        ).hexdigest(),
        "dockerContext": "default",
        "dockerEndpoint": "unix:///var/run/docker.sock",
        "dockerServerId": "docker-server-01",
        "osType": "linux",
    }


def _host_authority_runner(
    endpoint: str = "unix:///var/run/docker.sock",
    *,
    on_call: Callable[[int], None] | None = None,
) -> Callable[..., SimpleNamespace]:
    calls = 0

    def runner(
        _arguments: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: int,
    ) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        assert env
        assert timeout == 10
        stdout = (
            json.dumps(endpoint) + "\n"
            if calls % 2 == 1
            else '{"ID":"docker-server-01","OSType":"linux"}\n'
        )
        if on_call is not None:
            on_call(calls)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return runner


def _write_host_sign_inputs(
    producer,
    tmp_path: Path,
    *,
    candidate_root: Path,
) -> dict[str, Path]:
    inputs = {
        "release_run": _write_canonical_mapping(
            producer,
            tmp_path / "release-run.json",
            RELEASE_RUN,
        ),
        "challenges": _write_canonical_mapping(
            producer,
            tmp_path / "challenges.json",
            CHALLENGES,
        ),
        "artifact": tmp_path / "host-receipt.json",
    }
    inputs["artifact"].write_bytes(
        _host_receipt_with_facts(
            producer,
            _authority_facts(),
            candidate_contract=_candidate_contract_claim(candidate_root),
        )
    )
    inputs["private_key"], _public = _write_key_pair(tmp_path, "host")
    return inputs


def _sign_host_argv(
    *,
    candidate_root: Path,
    inputs: dict[str, Path],
    output: Path,
) -> list[str]:
    return [
        "sign-host",
        "--candidate-root",
        str(candidate_root),
        "--release-run",
        str(inputs["release_run"]),
        "--challenges",
        str(inputs["challenges"]),
        "--artifact",
        str(inputs["artifact"]),
        "--private-key",
        str(inputs["private_key"]),
        "--issued-at",
        "2026-08-30T04:00:00Z",
        "--expires-at",
        "2026-08-30T04:10:00Z",
        "--output",
        str(output),
    ]


def _host_receipt_argv(
    *,
    candidate_root: Path,
    release_run_path: Path,
    output: Path,
) -> list[str]:
    return [
        "host-receipt",
        "--candidate-root",
        str(candidate_root),
        "--release-run",
        str(release_run_path),
        "--output",
        str(output),
    ]


def test_host_authority_cli_exposes_no_fact_or_path_overrides() -> None:
    producer = _load_module()
    parser = producer._parser()
    subcommands = next(
        action.choices
        for action in parser._actions
        if isinstance(getattr(action, "choices", None), dict)
    )
    host_parser = subcommands["host-receipt"]
    sign_parser = subcommands["sign-host"]
    host_help = host_parser.format_help()
    sign_help = sign_parser.format_help()
    host_options = {
        option
        for action in host_parser._actions
        for option in getattr(action, "option_strings", ())
    }
    sign_options = {
        option
        for action in sign_parser._actions
        for option in getattr(action, "option_strings", ())
    }

    assert "--candidate" not in host_options
    assert "--candidate" not in sign_options

    for forbidden in (
        "--machine-id-file",
        "--docker-endpoint",
        "--docker-server-id",
        "--docker-cli",
        "--docker-config",
        "--skip-host-fact-check",
        "--allow-insecure",
    ):
        assert forbidden not in host_help
        assert forbidden not in sign_help
    assert "--candidate-root" in host_help
    assert "--candidate-root" in sign_help


def test_host_authority_collects_two_stable_fixed_linux_docker_rounds() -> None:
    producer = _load_module()
    calls: list[tuple[tuple[str, ...], dict[str, str], int]] = []
    outputs = iter(
        (
            '"unix:///var/run/docker.sock"\n',
            '{"ID":"docker-server-01","OSType":"linux"}\n',
            '"unix:///var/run/docker.sock"\n',
            '{"ID":"docker-server-01","OSType":"linux"}\n',
        )
    )

    def runner(
        arguments: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: int,
    ) -> SimpleNamespace:
        calls.append((arguments, env, timeout))
        return SimpleNamespace(returncode=0, stdout=next(outputs), stderr="")

    facts = producer._collect_gateway_host_authority_facts(
        machine_id_reader=lambda: MACHINE_ID.encode(),
        docker_resolver=lambda: Path("/usr/bin/docker"),
        runner=runner,
    )

    assert facts == _authority_facts()
    expected_round = [
        (
            "/usr/bin/docker",
            "context",
            "inspect",
            "default",
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ),
        (
            "/usr/bin/docker",
            "--context",
            "default",
            "info",
            "--format",
            "{{json .}}",
        ),
    ]
    assert [call[0] for call in calls] == [*expected_round, *expected_round]
    expected_env = {
        "DOCKER_CONFIG": "/etc/yfeistai/docker-authority",
        "LC_ALL": "C",
    }
    assert all(call[1] == expected_env for call in calls)
    assert all(call[2] == 10 for call in calls)


def test_host_authority_does_not_echo_docker_stderr() -> None:
    producer = _load_module()
    secret_marker = "DO-NOT-ECHO-DOCKER-CREDENTIAL"

    def runner(
        _arguments: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: int,
    ) -> SimpleNamespace:
        assert env
        assert timeout == 10
        return SimpleNamespace(returncode=1, stdout="", stderr=secret_marker)

    with pytest.raises(ValueError) as raised:
        producer._collect_gateway_host_authority_facts(
            machine_id_reader=lambda: MACHINE_ID.encode(),
            docker_resolver=lambda: Path("/usr/bin/docker"),
            runner=runner,
        )
    assert secret_marker not in str(raised.value)


def test_host_authority_rejects_unstable_docker_facts() -> None:
    producer = _load_module()
    outputs = iter(
        (
            '"unix:///var/run/docker.sock"\n',
            '{"ID":"docker-server-01","OSType":"linux"}\n',
            '"unix:///var/run/docker.sock"\n',
            '{"ID":"docker-server-02","OSType":"linux"}\n',
        )
    )

    def runner(
        _arguments: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: int,
    ) -> SimpleNamespace:
        assert env
        assert timeout == 10
        return SimpleNamespace(returncode=0, stdout=next(outputs), stderr="")

    with pytest.raises(ValueError, match="changed between authority observations"):
        producer._collect_gateway_host_authority_facts(
            machine_id_reader=lambda: MACHINE_ID.encode(),
            docker_resolver=lambda: Path("/usr/bin/docker"),
            runner=runner,
        )


@pytest.mark.parametrize(
    "drift",
    ("physicalHostIdSha256", "dockerEndpoint", "dockerServerId"),
)
def test_host_signer_recollects_and_rejects_candidate_host_fact_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    signed_facts = _authority_facts()
    observed_facts = dict(signed_facts)
    observed_facts[drift] = "f" * 64 if drift == "physicalHostIdSha256" else "different"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        lambda: observed_facts,
        raising=False,
    )
    private_path, _public = _write_key_pair(tmp_path, "host")

    with pytest.raises(ValueError, match="host authority facts do not match artifact"):
        producer.GatewayHostProvisionerSigner.from_private_key_file(
            private_path,
            candidate_root=candidate_root,
        ).sign(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            challenges=CHALLENGES,
            artifact_body=_host_receipt_with_facts(
                producer,
                signed_facts,
                candidate_contract=_candidate_contract_claim(candidate_root),
            ),
            issued_at="2026-08-30T04:00:00Z",
            expires_at="2026-08-30T04:10:00Z",
        )


@pytest.mark.parametrize(
    "signer_name",
    ("GatewayObserverSigner", "GatewayHostProvisionerSigner"),
)
def test_private_signing_key_source_must_be_outside_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signer_name: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    private_path, _public = _write_key_pair(candidate_root, "signer")
    signer_type = getattr(producer, signer_name)

    with pytest.raises(ValueError, match="private key source must be outside candidate root"):
        signer_type.from_private_key_file(
            private_path,
            candidate_root=candidate_root,
        )


@pytest.mark.parametrize(
    "signer_name",
    ("GatewayObserverSigner", "GatewayHostProvisionerSigner"),
)
def test_public_signer_requires_explicit_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signer_name: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    private_path, _public = _write_key_pair(tmp_path, "signer")
    signer_type = getattr(producer, signer_name)

    with pytest.raises(TypeError, match="candidate_root"):
        signer_type.from_private_key_file(private_path)


def test_host_receipt_cli_derives_candidate_from_strict_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    candidate_root = _write_candidate_contract(tmp_path)
    release_run_path = _write_canonical_mapping(
        producer,
        tmp_path / "release-run.json",
        RELEASE_RUN,
    )
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    output = trusted_root / "host-receipt.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    monkeypatch.setattr(producer, "_publish_no_replace", _publish_test_body)

    exit_code = producer.main(
        [
            "host-receipt",
            "--candidate-root",
            str(candidate_root),
            "--release-run",
            str(release_run_path),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert json.loads(output.read_bytes())["candidate"] == CANDIDATE


def test_host_receipt_cli_rejects_unrelated_candidate_override(tmp_path: Path) -> None:
    producer = _load_module()
    candidate_root = _write_candidate_contract(tmp_path)
    unrelated_candidate = _write_canonical_mapping(
        producer,
        tmp_path / "unrelated-candidate.json",
        CANDIDATE,
    )
    release_run_path = _write_canonical_mapping(
        producer,
        tmp_path / "release-run.json",
        RELEASE_RUN,
    )

    with pytest.raises(SystemExit) as raised:
        producer._parser().parse_args(
            [
                "host-receipt",
                "--candidate-root",
                str(candidate_root),
                "--candidate",
                str(unrelated_candidate),
                "--release-run",
                str(release_run_path),
                "--output",
                str(tmp_path / "host-receipt.json"),
            ]
        )

    assert raised.value.code == 2


@pytest.mark.parametrize(
    "compose_name",
    ("docker-compose.platform.yml", "docker-compose.data-plane.yml"),
)
def test_host_receipt_cli_rejects_candidate_compose_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compose_name: str,
) -> None:
    producer = _load_module()
    candidate_root = _write_candidate_contract(tmp_path)
    compose_path = candidate_root / compose_name
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8") + "# candidate drift\n",
        encoding="utf-8",
    )
    release_run_path = _write_canonical_mapping(
        producer,
        tmp_path / "release-run.json",
        RELEASE_RUN,
    )
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    output = trusted_root / "host-receipt.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    monkeypatch.setattr(producer, "_publish_no_replace", _publish_test_body)

    exit_code = producer.main(
        [
            "host-receipt",
            "--candidate-root",
            str(candidate_root),
            "--release-run",
            str(release_run_path),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert not output.exists()


def test_host_receipt_cli_rejects_decoy_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    candidate_root = _empty_candidate_root(tmp_path)
    release_run_path = _write_canonical_mapping(
        producer,
        tmp_path / "release-run.json",
        RELEASE_RUN,
    )
    output = tmp_path / "host-receipt.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    monkeypatch.setattr(producer, "_publish_no_replace", _publish_test_body)

    exit_code = producer.main(
        [
            "host-receipt",
            "--candidate-root",
            str(candidate_root),
            "--release-run",
            str(release_run_path),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert not output.exists()


def test_sign_host_cli_derives_candidate_from_strict_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    inputs = _write_host_sign_inputs(
        producer,
        tmp_path,
        candidate_root=candidate_root,
    )
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    output = trusted_root / "host-envelope.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    monkeypatch.setattr(producer, "_publish_no_replace", _publish_test_body)

    exit_code = producer.main(
        _sign_host_argv(
            candidate_root=candidate_root,
            inputs=inputs,
            output=output,
        )
    )

    assert exit_code == 0
    assert json.loads(output.read_bytes())["payload"]["candidate"] == CANDIDATE


def test_sign_host_cli_rejects_decoy_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _empty_candidate_root(tmp_path)
    receipt_candidate_root = _write_candidate_contract(tmp_path, name="receipt-candidate")
    inputs = _write_host_sign_inputs(
        producer,
        tmp_path,
        candidate_root=receipt_candidate_root,
    )
    output = tmp_path / "host-envelope.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    monkeypatch.setattr(producer, "_publish_no_replace", _publish_test_body)

    exit_code = producer.main(
        _sign_host_argv(
            candidate_root=candidate_root,
            inputs=inputs,
            output=output,
        )
    )

    assert exit_code == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "compose_name",
    ("docker-compose.platform.yml", "docker-compose.data-plane.yml"),
)
def test_sign_host_cli_rejects_candidate_compose_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compose_name: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    compose_path = candidate_root / compose_name
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8") + "# candidate drift\n",
        encoding="utf-8",
    )
    inputs = _write_host_sign_inputs(
        producer,
        tmp_path,
        candidate_root=candidate_root,
    )
    output = tmp_path / "host-envelope.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    monkeypatch.setattr(producer, "_publish_no_replace", _publish_test_body)

    exit_code = producer.main(
        _sign_host_argv(
            candidate_root=candidate_root,
            inputs=inputs,
            output=output,
        )
    )

    assert exit_code == 1
    assert not output.exists()


def test_sign_host_cli_rejects_receipt_candidate_not_matching_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    inputs = _write_host_sign_inputs(
        producer,
        tmp_path,
        candidate_root=candidate_root,
    )
    mismatched_candidate = dict(CANDIDATE)
    mismatched_candidate["sourceHead"] = "b" * 40
    mismatched_candidate["releaseTag"] = "yfeistai-first-release-20260831-bbbbbbbb"
    inputs["artifact"].write_bytes(
        producer._build_gateway_host_receipt_from_facts(
            candidate=mismatched_candidate,
            candidate_contract=_candidate_contract_claim(candidate_root),
            release_run=RELEASE_RUN,
            host=_authority_facts(),
        )
    )
    output = tmp_path / "host-envelope.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    monkeypatch.setattr(producer, "_publish_no_replace", _publish_test_body)

    exit_code = producer.main(
        _sign_host_argv(
            candidate_root=candidate_root,
            inputs=inputs,
            output=output,
        )
    )

    assert exit_code == 1
    assert not output.exists()


@pytest.mark.parametrize("command", ("host-receipt", "sign-host"))
def test_host_authority_cli_rejects_output_inside_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    runtime_root = candidate_root / "runtime"
    runtime_root.mkdir()
    output = runtime_root / f"{command}.json"
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )

    def fail_publish(_path: Path, _body: bytes) -> None:
        pytest.fail("candidate-contained host authority output reached publication")

    monkeypatch.setattr(producer, "_publish_no_replace", fail_publish)
    if command == "host-receipt":
        release_run_path = _write_canonical_mapping(
            producer,
            tmp_path / "release-run.json",
            RELEASE_RUN,
        )
        argv = _host_receipt_argv(
            candidate_root=candidate_root,
            release_run_path=release_run_path,
            output=output,
        )
    else:
        inputs = _write_host_sign_inputs(
            producer,
            tmp_path,
            candidate_root=candidate_root,
        )
        argv = _sign_host_argv(
            candidate_root=candidate_root,
            inputs=inputs,
            output=output,
        )

    assert producer.main(argv) == 1
    assert not output.exists()


def _simulate_candidate_root_identity_change(
    monkeypatch: pytest.MonkeyPatch,
    candidate_root: Path,
):
    original_lstat = Path.lstat
    changed = False

    class ChangedRootStat:
        def __init__(self, details: os.stat_result) -> None:
            self._details = details
            self.st_ino = details.st_ino + 1

        def __getattr__(self, name: str):
            return getattr(self._details, name)

    def observed_lstat(path: Path, *args, **kwargs):
        details = original_lstat(path, *args, **kwargs)
        if changed and Path(path) == candidate_root:
            return ChangedRootStat(details)
        return details

    def change_identity() -> None:
        nonlocal changed
        changed = True

    monkeypatch.setattr(Path, "lstat", observed_lstat)
    return change_identity


@pytest.mark.parametrize("command", ("host-receipt", "sign-host"))
def test_host_authority_cli_reports_candidate_root_exchange_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    moved_root = tmp_path / "candidate-original"
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    output = trusted_root / f"{command}.json"
    change_root_identity = (
        _simulate_candidate_root_identity_change(monkeypatch, candidate_root)
        if os.name == "nt"
        else None
    )
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )

    def publish_then_exchange(path: Path, body: bytes) -> None:
        Path(path).write_bytes(body)
        if change_root_identity is not None:
            change_root_identity()
            return
        candidate_root.rename(moved_root)
        candidate_root.mkdir()

    monkeypatch.setattr(producer, "_publish_no_replace", publish_then_exchange)
    if command == "host-receipt":
        release_run_path = _write_canonical_mapping(
            producer,
            tmp_path / "release-run.json",
            RELEASE_RUN,
        )
        argv = _host_receipt_argv(
            candidate_root=candidate_root,
            release_run_path=release_run_path,
            output=output,
        )
    else:
        inputs = _write_host_sign_inputs(
            producer,
            tmp_path,
            candidate_root=candidate_root,
        )
        argv = _sign_host_argv(
            candidate_root=candidate_root,
            inputs=inputs,
            output=output,
        )

    assert producer.main(argv) == 2
    assert output.is_file()


def test_host_authority_rejects_remote_docker_endpoint() -> None:
    producer = _load_module()

    with pytest.raises(ValueError, match="local Docker endpoint"):
        producer._collect_gateway_host_authority_facts(
            machine_id_reader=lambda: MACHINE_ID.encode(),
            docker_resolver=lambda: Path("/usr/bin/docker"),
            runner=_host_authority_runner("tcp://198.51.100.10:2376"),
        )


def test_host_authority_rejects_machine_id_drift_between_rounds() -> None:
    producer = _load_module()
    machine_id_reads = 0

    def machine_id_reader() -> bytes:
        nonlocal machine_id_reads
        machine_id_reads += 1
        return MACHINE_ID.encode() if machine_id_reads == 1 else b"f" * 32

    with pytest.raises(ValueError, match="authority.*changed"):
        producer._collect_gateway_host_authority_facts(
            machine_id_reader=machine_id_reader,
            docker_resolver=lambda: Path("/usr/bin/docker"),
            runner=_host_authority_runner(),
        )


def test_host_authority_rejects_docker_executable_drift_between_rounds() -> None:
    producer = _load_module()
    resolver_calls = 0

    def docker_resolver() -> Path:
        nonlocal resolver_calls
        resolver_calls += 1
        return Path("/usr/bin/docker") if resolver_calls == 1 else Path("/opt/candidate/docker")

    with pytest.raises(ValueError, match="authority.*changed"):
        producer._collect_gateway_host_authority_facts(
            machine_id_reader=lambda: MACHINE_ID.encode(),
            docker_resolver=docker_resolver,
            runner=_host_authority_runner(),
        )


def test_host_authority_rejects_docker_config_drift_between_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()

    def mutate_config_after_first_round(call: int) -> None:
        if call == 2:
            monkeypatch.setattr(
                producer,
                "_GATEWAY_DOCKER_CONFIG",
                "/etc/yfeistai/docker-authority-exchanged",
            )

    with pytest.raises(ValueError, match="authority.*changed"):
        producer._collect_gateway_host_authority_facts(
            machine_id_reader=lambda: MACHINE_ID.encode(),
            docker_resolver=lambda: Path("/usr/bin/docker"),
            runner=_host_authority_runner(on_call=mutate_config_after_first_round),
        )


def test_host_signer_factory_requires_strict_candidate_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    candidate_root = _empty_candidate_root(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "host")

    with pytest.raises(ValueError, match="candidate contract"):
        producer.GatewayHostProvisionerSigner.from_private_key_file(
            private_path,
            candidate_root=candidate_root,
        )


def test_host_signer_binds_candidate_to_factory_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "host")
    mismatched_candidate = dict(CANDIDATE)
    mismatched_candidate["sourceHead"] = "b" * 40
    mismatched_candidate["releaseTag"] = "yfeistai-first-release-20260831-bbbbbbbb"
    artifact = producer._build_gateway_host_receipt_from_facts(
        candidate=mismatched_candidate,
        candidate_contract=_candidate_contract_claim(candidate_root),
        release_run=RELEASE_RUN,
        host=_authority_facts(),
    )
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    signer = producer.GatewayHostProvisionerSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    )

    with pytest.raises(ValueError, match="candidate.*contract"):
        signer.sign(
            candidate=mismatched_candidate,
            release_run=RELEASE_RUN,
            challenges=CHALLENGES,
            artifact_body=artifact,
            issued_at="2026-08-30T04:00:00Z",
            expires_at="2026-08-30T04:10:00Z",
        )


def test_host_signer_direct_construction_cannot_bypass_candidate_contract() -> None:
    producer = _load_module()
    forged_contract = SimpleNamespace(
        candidate=CANDIDATE,
        assert_unchanged=lambda: None,
    )

    with pytest.raises((TypeError, ValueError), match="candidate contract"):
        producer.GatewayHostProvisionerSigner(
            Ed25519PrivateKey.generate(),
            forged_contract,
        )


def test_host_signer_factory_candidate_binding_is_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "host")
    signer = producer.GatewayHostProvisionerSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    )
    bound_candidate = signer._candidate_contract.candidate
    bound_candidate["sourceHead"] = "b" * 40

    assert signer._candidate_contract.candidate == CANDIDATE
    with pytest.raises(AttributeError):
        signer._candidate_contract = SimpleNamespace(
            candidate=CANDIDATE,
            assert_unchanged=lambda: None,
        )
    with pytest.raises(AttributeError):
        signer._candidate_contract.candidate_canonical = producer._canonical_json(
            {**CANDIDATE, "sourceHead": "b" * 40}
        )


def test_host_signer_uses_one_fresh_whole_candidate_lease_per_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "host")
    signer = producer.GatewayHostProvisionerSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    )
    original_loader = producer._load_candidate_contract
    original_contracts = [original_loader(candidate_root) for _ in range(2)]
    marked_claims: list[dict[str, object]] = []
    for index, contract in enumerate(original_contracts):
        claim = json.loads(json.dumps(contract.claim))
        artifacts = claim["artifacts"]
        assert isinstance(artifacts, dict)
        marker = artifacts["docker-compose.platform.yml"]
        assert isinstance(marker, dict)
        marker["sha256"] = ("a" if index == 0 else "b") * 64
        marked_claims.append(claim)

    lifecycle: list[tuple[str, int]] = []

    class ObservableSignatureLease:
        def __init__(
            self,
            contract: object,
            claim: dict[str, object],
            marker: int,
        ) -> None:
            self.root = contract.root
            self._candidate = contract.candidate
            self._claim = claim
            self.marker = marker
            self.active = False
            self.activations = 0
            self.releases = 0
            self.accesses: list[str] = []

        def activate(self) -> None:
            assert not self.active
            self.active = True
            self.activations += 1
            lifecycle.append(("activate", self.marker))

        def __enter__(self):
            assert self.active
            lifecycle.append(("enter", self.marker))
            return self

        def __exit__(self, *_error: object) -> None:
            self.close()

        def close(self) -> None:
            assert self.active
            self.active = False
            self.releases += 1
            lifecycle.append(("release", self.marker))

        @property
        def candidate(self) -> dict[str, object]:
            assert self.active
            self.accesses.append("candidate")
            lifecycle.append(("candidate", self.marker))
            return json.loads(json.dumps(self._candidate))

        @property
        def claim(self) -> dict[str, object]:
            assert self.active
            self.accesses.append("claim")
            lifecycle.append(("claim", self.marker))
            return json.loads(json.dumps(self._claim))

        def assert_current(self) -> None:
            assert self.active
            self.accesses.append("assert_current")
            lifecycle.append(("assert_current", self.marker))

        def assert_unchanged(self) -> None:
            self.assert_current()

    signature_leases = [
        ObservableSignatureLease(contract, claim, marker)
        for marker, (contract, claim) in enumerate(
            zip(original_contracts, marked_claims, strict=True)
        )
    ]
    available_leases = iter(signature_leases)
    load_calls: list[Path] = []

    def load_fresh_signature_lease(root: Path) -> object:
        load_calls.append(Path(root))
        try:
            lease = next(available_leases)
        except StopIteration:
            pytest.fail("host signer loaded more than one candidate lease per signature")
        lease.activate()
        return lease

    artifacts = [
        _host_receipt_with_facts(
            producer,
            _authority_facts(),
            candidate_contract=claim,
        )
        for claim in marked_claims
    ]
    original_parser = producer._parse_gateway_host_receipt
    original_canonical = producer._canonical_json
    artifact_validation_active: list[tuple[int, ...]] = []
    payload_construction_active: list[tuple[int, ...]] = []
    top_level_construction_active: list[tuple[int, ...]] = []

    def active_signature_leases() -> tuple[int, ...]:
        return tuple(lease.marker for lease in signature_leases if lease.active)

    def parse_with_active_signature_lease(*args: object, **kwargs: object) -> object:
        active = active_signature_leases()
        artifact_validation_active.append(active)
        lifecycle.extend(("artifact_parse", marker) for marker in active)
        return original_parser(*args, **kwargs)

    def canonical_with_active_signature_lease(document: dict[str, object]) -> bytes:
        if "claims" in document and "candidateContract" in document:
            active = active_signature_leases()
            payload_construction_active.append(active)
            lifecycle.extend(("payload_canonical", marker) for marker in active)
        if "signatureBase64" in document:
            active = active_signature_leases()
            top_level_construction_active.append(active)
            lifecycle.extend(("top_level_canonical", marker) for marker in active)
        return original_canonical(document)

    monkeypatch.setattr(producer, "_load_candidate_contract", load_fresh_signature_lease)
    monkeypatch.setattr(
        producer,
        "_parse_gateway_host_receipt",
        parse_with_active_signature_lease,
    )
    monkeypatch.setattr(producer, "_canonical_json", canonical_with_active_signature_lease)
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )
    envelopes: list[bytes] = []
    errors: list[str] = []
    for artifact in artifacts:
        try:
            envelopes.append(
                signer.sign(
                    candidate=CANDIDATE,
                    release_run=RELEASE_RUN,
                    challenges=CHALLENGES,
                    artifact_body=artifact,
                    issued_at="2026-08-30T04:00:00Z",
                    expires_at="2026-08-30T04:10:00Z",
                )
            )
        except ValueError as error:
            errors.append(str(error))

    assert all(not lease.active for lease in signature_leases)
    assert errors == []
    assert len(envelopes) == 2
    assert load_calls == [candidate_root, candidate_root]
    assert signature_leases[0] is not signature_leases[1]
    assert lifecycle.index(("release", 0)) < lifecycle.index(("activate", 1))
    envelope_claims = [json.loads(body)["payload"]["candidateContract"] for body in envelopes]
    assert envelope_claims == marked_claims
    assert artifact_validation_active == [(0,), (1,)]
    assert payload_construction_active == [(0,), (1,)]
    assert top_level_construction_active == [(0,), (1,)]
    for lease in signature_leases:
        assert lease.activations == 1
        assert lease.releases == 1
        assert "candidate" in lease.accesses
        assert "claim" in lease.accesses
        assert "assert_current" in lease.accesses
        events = [event for event, marker in lifecycle if marker == lease.marker]
        assert events[0] == "activate"
        assert events[-1] == "release"
        assert events[-2] == "assert_current"
        assert events.count("release") == 1
        assert events.index("artifact_parse") < events.index("payload_canonical")
        assert events.index("payload_canonical") < events.index("top_level_canonical")
        assert events.index("top_level_canonical") < len(events) - 2


def _mutate_candidate_contract(
    candidate_root: Path,
    tmp_path: Path,
    target: str,
) -> None:
    if target == "root":
        candidate_root.rename(tmp_path / "candidate-before-exchange")
        candidate_root.mkdir()
        return
    path = (
        candidate_root / "deploy" / "image-lock.json"
        if target == "image-lock"
        else candidate_root / target
    )
    path.write_bytes(path.read_bytes() + b"\n")


@pytest.mark.parametrize(
    "target",
    (
        "root",
        "image-lock",
        "docker-compose.platform.yml",
        "docker-compose.data-plane.yml",
    ),
)
def test_host_signer_rejects_candidate_contract_drift_before_signing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "host")
    signer = producer.GatewayHostProvisionerSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    )
    candidate_contract = _candidate_contract_claim(candidate_root)
    _mutate_candidate_contract(candidate_root, tmp_path, target)
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )

    with pytest.raises(ValueError, match="candidate contract changed"):
        signer.sign(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            challenges=CHALLENGES,
            artifact_body=_host_receipt_with_facts(
                producer,
                _authority_facts(),
                candidate_contract=candidate_contract,
            ),
            issued_at="2026-08-30T04:00:00Z",
            expires_at="2026-08-30T04:10:00Z",
        )


@pytest.mark.parametrize(
    "target",
    (
        "root",
        "image-lock",
        "docker-compose.platform.yml",
        "docker-compose.data-plane.yml",
    ),
)
def test_host_signer_rejects_candidate_contract_drift_during_signing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    change_root_identity = (
        _simulate_candidate_root_identity_change(monkeypatch, candidate_root)
        if target == "root" and os.name == "nt"
        else None
    )
    private_path, _public = _write_key_pair(tmp_path, "host")
    signer = producer.GatewayHostProvisionerSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    )
    candidate_contract = _candidate_contract_claim(candidate_root)

    def collect_then_mutate_candidate() -> dict[str, object]:
        if change_root_identity is not None:
            change_root_identity()
        else:
            _mutate_candidate_contract(candidate_root, tmp_path, target)
        return _authority_facts()

    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        collect_then_mutate_candidate,
    )

    with pytest.raises(ValueError, match="candidate contract changed"):
        signer.sign(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            challenges=CHALLENGES,
            artifact_body=_host_receipt_with_facts(
                producer,
                _authority_facts(),
                candidate_contract=candidate_contract,
            ),
            issued_at="2026-08-30T04:00:00Z",
            expires_at="2026-08-30T04:10:00Z",
        )


def test_host_signer_rejects_stale_contract_from_late_snapshot_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _load_module()
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    private_path, _public = _write_key_pair(tmp_path, "host")
    signer = producer.GatewayHostProvisionerSigner.from_private_key_file(
        private_path,
        candidate_root=candidate_root,
    )
    candidate_contract = _candidate_contract_claim(candidate_root)
    compose_path = candidate_root / "docker-compose.platform.yml"
    overlay_path = candidate_root / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml"
    original_reader = producer._read_candidate_contract_artifact
    overlay_reads = 0

    def read_then_rewrite_early_compose(lease) -> bytes:
        nonlocal overlay_reads
        result = original_reader(lease)
        if lease.path == overlay_path:
            overlay_reads += 1
            if overlay_reads == 4:
                with compose_path.open("r+b") as stream:
                    stream.seek(-1, 2)
                    assert stream.read(1) == b"\n"
                    stream.seek(-1, 2)
                    stream.write(b" ")
        return result

    monkeypatch.setattr(
        producer,
        "_read_candidate_contract_artifact",
        read_then_rewrite_early_compose,
    )
    monkeypatch.setattr(
        producer,
        "_collect_gateway_host_authority_facts",
        _authority_facts,
    )

    with pytest.raises(ValueError, match="candidate contract changed"):
        signer.sign(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            challenges=CHALLENGES,
            artifact_body=_host_receipt_with_facts(
                producer,
                _authority_facts(),
                candidate_contract=candidate_contract,
            ),
            issued_at="2026-08-30T04:00:00Z",
            expires_at="2026-08-30T04:10:00Z",
        )

    assert overlay_reads == 4


def test_gateway_challenge_rejects_boolean_schema_version() -> None:
    producer = _load_module()

    with pytest.raises(ValueError, match="challenge document is invalid"):
        producer._challenge_for(
            {**CHALLENGES, "schemaVersion": True},
            payload_type="gateway-host-provisioner",
        )


def test_gateway_trust_keyring_rejects_boolean_schema_version(tmp_path: Path) -> None:
    producer = _load_module()
    contract = _load_source_module(
        "gateway_trust_contract.py",
        "gateway_trust_contract_for_boolean_keyring_test",
    )
    _observer_private, observer_public = _write_key_pair(tmp_path, "observer")
    _host_private, host_public = _write_key_pair(tmp_path, "host")
    keyring = json.loads(
        producer.build_gateway_trust_keyring(
            observer_public_key_body=observer_public,
            host_public_key_body=host_public,
        )
    )
    keyring["schemaVersion"] = True

    with pytest.raises(ValueError, match="keyring schema is invalid"):
        contract._trusted_keys(producer._canonical_json(keyring))


@pytest.mark.parametrize("location", ("envelope", "payload"))
def test_gateway_trust_envelope_rejects_boolean_schema_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    producer = _load_module()
    contract = _load_source_module(
        "gateway_trust_contract.py",
        f"gateway_trust_contract_for_boolean_{location}_test",
    )
    public_contract = _load_source_module(
        "gateway_public_contract.py",
        f"gateway_public_contract_for_boolean_{location}_test",
    )
    _authorize_test_private_key(monkeypatch, producer)
    _fix_signing_clock(monkeypatch, producer)
    candidate_root = _write_candidate_contract(tmp_path)
    private_path, public_body = _write_key_pair(tmp_path, "observer")
    _raw, artifact, _digest = canonical_inputs(
        public_contract,
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        runtime_sha256="4" * 64,
    )
    document = json.loads(
        producer.GatewayObserverSigner.from_private_key_file(
            private_path,
            candidate_root=candidate_root,
        ).sign(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            challenges=CHALLENGES,
            artifact_body=artifact,
            issued_at="2026-08-30T04:00:00Z",
            expires_at="2026-08-30T04:10:00Z",
        )
    )
    document["schemaVersion"] = True
    if location == "payload":
        document["schemaVersion"] = 2
        document["payload"]["schemaVersion"] = True
        private_key = serialization.load_pem_private_key(
            private_path.read_bytes(),
            password=None,
        )
        assert isinstance(private_key, Ed25519PrivateKey)
        signature_input = (
            producer._SIGNATURE_DOMAIN
            + b"gateway-observer\0"
            + producer._canonical_json(document["payload"])
        )
        document["signatureBase64"] = base64.b64encode(private_key.sign(signature_input)).decode(
            "ascii"
        )
    keyring_body = producer.build_gateway_trust_keyring(
        observer_public_key_body=public_body,
        host_public_key_body=_write_key_pair(tmp_path, "host")[1],
    )
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    keyring_path = trusted_root / "gateway-keyring.json"
    keyring_path.write_bytes(keyring_body)

    with pytest.raises(ValueError, match="(?:envelope|payload) schema is invalid"):
        contract._parse_gateway_trust_envelope(
            producer._canonical_json(document),
            expected_payload_type="gateway-observer",
            trusted_keyring_path=keyring_path,
            candidate_root=candidate_root,
            candidate=CANDIDATE,
            candidate_contract=_candidate_contract_claim(candidate_root),
            release_run=RELEASE_RUN,
            expected_environment_id=RELEASE_RUN["environmentId"],
            expected_challenge=CHALLENGES["observerChallenge"],
            trusted_now="2026-08-30T04:05:00Z",
        )


def _fake_docker_socket_stat(
    *,
    inode: int = 101,
    owner: int = 0,
    mode: int = stat.S_IFSOCK | 0o660,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=7,
        st_ino=inode,
        st_size=0,
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_mode=mode,
        st_uid=owner,
    )


def test_host_authority_binds_stable_root_owned_fixed_unix_socket() -> None:
    producer = _load_module()
    observed_paths: list[Path] = []

    def docker_socket_stat_reader(path: Path) -> SimpleNamespace:
        observed_paths.append(path)
        return _fake_docker_socket_stat()

    facts = producer._collect_gateway_host_authority_facts(
        machine_id_reader=lambda: MACHINE_ID.encode(),
        docker_resolver=lambda: Path("/usr/bin/docker"),
        docker_socket_stat_reader=docker_socket_stat_reader,
        runner=_host_authority_runner(),
    )

    assert facts == _authority_facts()
    assert observed_paths == [Path("/var/run/docker.sock")] * 4


@pytest.mark.parametrize("case", ("non-root-owner", "not-socket", "identity-drift"))
def test_host_authority_rejects_unsafe_or_drifting_fixed_unix_socket(case: str) -> None:
    producer = _load_module()
    reads = 0

    def docker_socket_stat_reader(path: Path) -> SimpleNamespace:
        nonlocal reads
        reads += 1
        assert path == Path("/var/run/docker.sock")
        if case == "non-root-owner":
            return _fake_docker_socket_stat(owner=1000)
        if case == "not-socket":
            return _fake_docker_socket_stat(mode=stat.S_IFREG | 0o660)
        return _fake_docker_socket_stat(inode=101 if reads < 4 else 202)

    with pytest.raises(ValueError, match="Docker socket"):
        producer._collect_gateway_host_authority_facts(
            machine_id_reader=lambda: MACHINE_ID.encode(),
            docker_resolver=lambda: Path("/usr/bin/docker"),
            docker_socket_stat_reader=docker_socket_stat_reader,
            runner=_host_authority_runner(),
        )


def test_host_authority_rejects_docker_command_output_over_hard_byte_limit() -> None:
    producer = _load_module()
    oversized_json = json.dumps("x" * (128 * 1024))

    def runner(
        _arguments: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: int,
    ) -> SimpleNamespace:
        assert env
        assert timeout == 10
        return SimpleNamespace(returncode=0, stdout=oversized_json, stderr="")

    with pytest.raises(ValueError, match="output size is invalid"):
        producer._gateway_host_authority_json(("/usr/bin/docker", "info"), runner=runner)


def test_real_host_authority_runner_stops_combined_output_before_materializing_it(
    tmp_path: Path,
) -> None:
    producer = _load_module()
    completed_marker = tmp_path / "child-completed"
    child = "\n".join(
        (
            "from pathlib import Path",
            "import sys",
            "import time",
            "payload = b'x' * (65 * 1024)",
            "sys.stdout.buffer.write(payload)",
            "sys.stdout.buffer.flush()",
            "sys.stderr.buffer.write(payload)",
            "sys.stderr.buffer.flush()",
            "time.sleep(30)",
            "Path(sys.argv[1]).write_text('completed', encoding='utf-8')",
        )
    )

    with pytest.raises(ValueError, match="output size is invalid"):
        producer._gateway_host_authority_json(
            (sys.executable, "-c", child, str(completed_marker)),
            runner=producer._run_gateway_host_authority_command,
        )

    assert not completed_marker.exists(), "runner materialized all child output before rejecting it"
