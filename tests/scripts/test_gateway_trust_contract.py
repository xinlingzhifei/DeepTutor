from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

ROOT = Path(__file__).resolve().parents[2]

OBSERVER_PAYLOAD_TYPE = "gateway-observer"
HOST_PAYLOAD_TYPE = "gateway-host-provisioner"
OBSERVER_ARTIFACT = "runtime/gateway-external-observer-attestation.json"
HOST_RECEIPT_ARTIFACT = "runtime/gateway-docker-host-provisioning-receipt.json"
CANDIDATE = {
    "sourceRepository": "xinlingzhifei/DeepTutor",
    "sourceHead": "a" * 40,
    "releaseTag": "yfeistai-first-release-20260830-aaaaaaaa",
    "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
    "imageDigests": {
        "deeptutor": "sha256:" + "1" * 64,
        "openmaic": "sha256:" + "2" * 64,
        "openmaic_render": "sha256:" + "3" * 64,
    },
}
RELEASE_RUN = {
    "runId": "first-release-run-20260830",
    "environmentId": "acceptance-external-01",
}
CHALLENGE = "4" * 64
HOST_CHALLENGE = "6" * 64
TRUSTED_NOW = "2026-08-30T10:05:00Z"
ISSUED_AT = "2026-08-30T10:00:00Z"
EXPIRES_AT = "2026-08-30T10:10:00Z"
SIGNATURE_DOMAIN = b"yfeistai.gateway-trust-envelope.v2\0"
_CANDIDATE_ARTIFACT_RELATIVE_PATHS = (
    "deploy/image-lock.json",
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.data-plane.yml",
    "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
)
CANDIDATE_CONTRACT = {
    "schemaVersion": 1,
    "artifacts": {
        relative: {"sha256": character * 64}
        for relative, character in zip(
            _CANDIDATE_ARTIFACT_RELATIVE_PATHS,
            "abcde",
            strict=True,
        )
    },
}


def _load_module():
    renderer = _load_support_module(
        "render_platform_compose.py",
        "render_platform_compose_for_gateway_trust_contract",
    )
    previous_renderer = sys.modules.get("render_platform_compose")
    sys.modules["render_platform_compose"] = renderer
    path = ROOT / "scripts" / "gateway_trust_contract.py"
    assert path.is_file(), "gateway trust contract is missing"
    spec = importlib.util.spec_from_file_location("gateway_trust_contract_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)
        if previous_renderer is None:
            sys.modules.pop("render_platform_compose", None)
        else:
            sys.modules["render_platform_compose"] = previous_renderer


def _load_support_module(filename: str, module_name: str):
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


def _canonical(document: object) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _spki_der(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _key_id(private_key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_spki_der(private_key)).hexdigest()


def _payload(
    payload_type: str,
    *,
    candidate: dict[str, object] | None = None,
    candidate_contract: dict[str, object] | None = None,
    release_run: dict[str, str] | None = None,
    environment_id: str = RELEASE_RUN["environmentId"],
    challenge: str = CHALLENGE,
    issued_at: str = ISSUED_AT,
    expires_at: str = EXPIRES_AT,
) -> dict[str, object]:
    issuer = (
        "external-observer-authority"
        if payload_type == OBSERVER_PAYLOAD_TYPE
        else "deployment-authority"
    )
    artifact_name = (
        OBSERVER_ARTIFACT if payload_type == OBSERVER_PAYLOAD_TYPE else HOST_RECEIPT_ARTIFACT
    )
    return {
        "schemaVersion": 2,
        "issuer": issuer,
        "issuedAt": issued_at,
        "expiresAt": expires_at,
        "challenge": challenge,
        "candidate": copy.deepcopy(candidate if candidate is not None else CANDIDATE),
        "candidateContract": copy.deepcopy(
            candidate_contract if candidate_contract is not None else CANDIDATE_CONTRACT
        ),
        "releaseRun": copy.deepcopy(release_run if release_run is not None else RELEASE_RUN),
        "environmentId": environment_id,
        "claims": {
            "artifact": artifact_name,
            "artifactSha256": "5" * 64,
        },
    }


def _signature_input(payload_type: str, payload: dict[str, object]) -> bytes:
    return SIGNATURE_DOMAIN + payload_type.encode("ascii") + b"\0" + _canonical(payload)


def _envelope(
    private_key: Ed25519PrivateKey,
    payload_type: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    bound_payload = copy.deepcopy(payload if payload is not None else _payload(payload_type))
    signature = private_key.sign(_signature_input(payload_type, bound_payload))
    return {
        "schemaVersion": 2,
        "payloadType": payload_type,
        "algorithm": "ed25519",
        "keyId": _key_id(private_key),
        "payload": bound_payload,
        "signatureBase64": base64.b64encode(signature).decode("ascii"),
    }


def _write_keyring(
    path: Path,
    entries: list[tuple[Ed25519PrivateKey, tuple[str, ...]]],
    *,
    key_id_overrides: dict[int, str] | None = None,
) -> Path:
    key_id_overrides = key_id_overrides or {}
    document = {
        "schemaVersion": 1,
        "keys": [
            {
                "algorithm": "ed25519",
                "keyId": key_id_overrides.get(index, _key_id(private_key)),
                "payloadTypes": list(payload_types),
                "publicKeySpkiBase64": base64.b64encode(_spki_der(private_key)).decode("ascii"),
            }
            for index, (private_key, payload_types) in enumerate(entries)
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(document))
    return path


def _case_roots(tmp_path: Path) -> tuple[Path, Path]:
    candidate_root = tmp_path / "candidate"
    trusted_root = tmp_path / "trusted-controller"
    candidate_root.mkdir()
    trusted_root.mkdir()
    return candidate_root, trusted_root


def _write_candidate_contract(
    tmp_path: Path,
    *,
    candidate: dict[str, object] = CANDIDATE,
    name: str = "candidate-contract",
    fallback_digest_index: int = 4,
) -> Path:
    renderer = _load_support_module(
        "render_platform_compose.py",
        f"render_platform_compose_for_gateway_contract_{name.replace('-', '_')}",
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
    assert isinstance(custom_digests, dict)
    custom_digest_by_repository = {
        renderer.IMAGE_SPECS[image_name]["repository"]: custom_digests[image_name]
        for image_name in renderer.CUSTOM_IMAGE_NAMES
    }

    def resolve_digest(reference: str) -> str:
        nonlocal fallback_digest_index
        repository = reference.split(":", 1)[0]
        digest = custom_digest_by_repository.get(repository)
        if isinstance(digest, str):
            return digest
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


def _candidate_descriptor_is_closed(module, lease: object) -> bool:
    try:
        opened = module.os.fstat(lease.descriptor)
    except OSError:
        return True
    return module._file_signature(opened) != lease.signature


def _parse(
    module,
    body: bytes,
    *,
    payload_type: str,
    keyring_path: Path,
    candidate_root: Path,
    candidate: dict[str, object] | None = None,
    candidate_contract: dict[str, object] | None = None,
    release_run: dict[str, str] | None = None,
    environment_id: str = RELEASE_RUN["environmentId"],
    challenge: str = CHALLENGE,
    trusted_now: str = TRUSTED_NOW,
):
    return module._parse_gateway_trust_envelope(
        body,
        expected_payload_type=payload_type,
        trusted_keyring_path=keyring_path,
        candidate_root=candidate_root,
        candidate=candidate if candidate is not None else CANDIDATE,
        candidate_contract=(
            candidate_contract if candidate_contract is not None else CANDIDATE_CONTRACT
        ),
        release_run=release_run if release_run is not None else RELEASE_RUN,
        expected_environment_id=environment_id,
        expected_challenge=challenge,
        trusted_now=trusted_now,
    )


@pytest.mark.parametrize("payload_type", (OBSERVER_PAYLOAD_TYPE, HOST_PAYLOAD_TYPE))
def test_valid_signed_envelope_is_accepted(tmp_path: Path, payload_type: str) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (payload_type,))],
    )
    payload = _payload(payload_type)

    parsed = _parse(
        module,
        _canonical(_envelope(private_key, payload_type, payload)),
        payload_type=payload_type,
        keyring_path=keyring,
        candidate_root=candidate_root,
    )

    assert parsed == payload


@pytest.mark.parametrize("invalid_form", ("noncanonical", "oversized"))
def test_envelope_is_strictly_bounded_and_canonical(tmp_path: Path, invalid_form: str) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    envelope = _envelope(private_key, OBSERVER_PAYLOAD_TYPE)
    if invalid_form == "noncanonical":
        body = (json.dumps(envelope, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        message = "canonical"
    else:
        body = b" " * (module.MAX_GATEWAY_TRUST_ENVELOPE_BYTES + 1)
        message = "size"

    with pytest.raises(ValueError, match=message):
        _parse(
            module,
            body,
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


@pytest.mark.parametrize("invalid_form", ("unsigned", "embedded-public-key"))
def test_unsigned_or_self_embedded_key_cannot_establish_trust(
    tmp_path: Path,
    invalid_form: str,
) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    envelope = _envelope(private_key, OBSERVER_PAYLOAD_TYPE)
    if invalid_form == "unsigned":
        envelope.pop("signatureBase64")
        message = "signature"
    else:
        envelope["publicKeySpkiBase64"] = base64.b64encode(_spki_der(private_key)).decode("ascii")
        message = "schema"

    with pytest.raises(ValueError, match=message):
        _parse(
            module,
            _canonical(envelope),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


def test_trust_keyring_inside_candidate_root_cannot_establish_trust(tmp_path: Path) -> None:
    module = _load_module()
    candidate_root, _trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    candidate_keyring = _write_keyring(
        candidate_root / "runtime" / "self-issued-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )

    with pytest.raises(ValueError, match="outside candidate"):
        _parse(
            module,
            _canonical(_envelope(private_key, OBSERVER_PAYLOAD_TYPE)),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=candidate_keyring,
            candidate_root=candidate_root,
        )


def test_keyring_key_id_must_be_sha256_of_spki_der(tmp_path: Path) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    false_key_id = "f" * 64
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
        key_id_overrides={0: false_key_id},
    )
    envelope = _envelope(private_key, OBSERVER_PAYLOAD_TYPE)
    envelope["keyId"] = false_key_id

    with pytest.raises(ValueError, match="key id"):
        _parse(
            module,
            _canonical(envelope),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


def test_key_cannot_cross_payload_roles(tmp_path: Path) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )

    with pytest.raises(ValueError, match="role"):
        _parse(
            module,
            _canonical(_envelope(private_key, HOST_PAYLOAD_TYPE)),
            payload_type=HOST_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


def test_payload_type_is_bound_into_the_signature_domain(tmp_path: Path) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE, HOST_PAYLOAD_TYPE))],
    )
    envelope = _envelope(private_key, OBSERVER_PAYLOAD_TYPE, _payload(HOST_PAYLOAD_TYPE))
    envelope["payloadType"] = HOST_PAYLOAD_TYPE

    with pytest.raises(ValueError, match="signature"):
        _parse(
            module,
            _canonical(envelope),
            payload_type=HOST_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


def test_envelope_signed_by_wrong_key_is_rejected(tmp_path: Path) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    trusted_key = Ed25519PrivateKey.generate()
    wrong_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(trusted_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    envelope = _envelope(wrong_key, OBSERVER_PAYLOAD_TYPE)
    envelope["keyId"] = _key_id(trusted_key)

    with pytest.raises(ValueError, match="signature"):
        _parse(
            module,
            _canonical(envelope),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


def test_signed_payload_tamper_is_rejected(tmp_path: Path) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    envelope = _envelope(private_key, OBSERVER_PAYLOAD_TYPE)
    claims = envelope["payload"]["claims"]
    assert isinstance(claims, dict)
    claims["artifactSha256"] = "6" * 64

    with pytest.raises(ValueError, match="signature"):
        _parse(
            module,
            _canonical(envelope),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "message"),
    (
        ("2026-08-30T10:06:00Z", EXPIRES_AT, "issued"),
        (ISSUED_AT, "2026-08-30T10:04:59Z", "expired"),
    ),
    ids=("not-yet-valid", "expired"),
)
def test_envelope_time_window_is_enforced(
    tmp_path: Path,
    issued_at: str,
    expires_at: str,
    message: str,
) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    payload = _payload(OBSERVER_PAYLOAD_TYPE, issued_at=issued_at, expires_at=expires_at)

    with pytest.raises(ValueError, match=message):
        _parse(
            module,
            _canonical(_envelope(private_key, OBSERVER_PAYLOAD_TYPE, payload)),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "message"),
    (
        ("2026-08-30T04:00:00Z", "2036-08-30T04:00:00Z", "lifetime"),
        ("2026-08-30T03:59:00Z", "2026-08-30T04:06:00Z", "too old"),
    ),
    ids=("ten-year-lifetime", "stale-but-unexpired"),
)
def test_envelope_rejects_excessive_ttl_or_stale_issuance(
    tmp_path: Path,
    issued_at: str,
    expires_at: str,
    message: str,
) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    payload = _payload(OBSERVER_PAYLOAD_TYPE, issued_at=issued_at, expires_at=expires_at)

    with pytest.raises(ValueError, match=message):
        _parse(
            module,
            _canonical(_envelope(private_key, OBSERVER_PAYLOAD_TYPE, payload)),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
            trusted_now="2026-08-30T04:05:00Z",
        )


def test_envelope_allows_small_future_clock_skew(tmp_path: Path) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    payload = _payload(
        OBSERVER_PAYLOAD_TYPE,
        issued_at="2026-08-30T10:05:04Z",
        expires_at="2026-08-30T10:10:00Z",
    )

    parsed = _parse(
        module,
        _canonical(_envelope(private_key, OBSERVER_PAYLOAD_TYPE, payload)),
        payload_type=OBSERVER_PAYLOAD_TYPE,
        keyring_path=keyring,
        candidate_root=candidate_root,
        trusted_now="2026-08-30T10:05:00Z",
    )

    assert parsed["issuedAt"] == "2026-08-30T10:05:04Z"


def test_envelope_challenge_must_match_the_external_verifier(tmp_path: Path) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )

    with pytest.raises(ValueError, match="challenge"):
        _parse(
            module,
            _canonical(_envelope(private_key, OBSERVER_PAYLOAD_TYPE)),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
            challenge="7" * 64,
        )


@pytest.mark.parametrize("binding", ("candidate", "release-run", "environment"))
def test_envelope_is_bound_to_candidate_release_run_and_environment(
    tmp_path: Path,
    binding: str,
) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    payload_candidate = copy.deepcopy(CANDIDATE)
    payload_release_run = copy.deepcopy(RELEASE_RUN)
    payload_environment = RELEASE_RUN["environmentId"]
    if binding == "candidate":
        payload_candidate["sourceHead"] = "b" * 40
        message = "candidate"
    elif binding == "release-run":
        payload_release_run["runId"] = "other-release-run"
        message = "release run"
    else:
        payload_environment = "other-environment"
        message = "environment"
    payload = _payload(
        OBSERVER_PAYLOAD_TYPE,
        candidate=payload_candidate,
        release_run=payload_release_run,
        environment_id=payload_environment,
    )

    with pytest.raises(ValueError, match=message):
        _parse(
            module,
            _canonical(_envelope(private_key, OBSERVER_PAYLOAD_TYPE, payload)),
            payload_type=OBSERVER_PAYLOAD_TYPE,
            keyring_path=keyring,
            candidate_root=candidate_root,
        )


@pytest.fixture
def gateway_trust_pair(tmp_path: Path) -> dict[str, object]:
    candidate_root = _write_candidate_contract(tmp_path)
    candidate_contract = _candidate_contract_claim(candidate_root)
    trusted_root = tmp_path / "trusted-controller"
    trusted_root.mkdir()
    observer_key = Ed25519PrivateKey.generate()
    host_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [
            (observer_key, (OBSERVER_PAYLOAD_TYPE,)),
            (host_key, (HOST_PAYLOAD_TYPE,)),
        ],
    )
    observer_artifact = {
        "schemaVersion": 1,
        "producer": "gateway-external-observer-attestation",
    }
    observer_artifact_body = _canonical(observer_artifact)
    host_receipt = {
        "schemaVersion": 2,
        "producer": "gateway-docker-host-provisioner",
        "candidate": copy.deepcopy(CANDIDATE),
        "candidateContract": copy.deepcopy(candidate_contract),
        "releaseRun": copy.deepcopy(RELEASE_RUN),
        "environmentId": RELEASE_RUN["environmentId"],
        "host": {
            "physicalHostIdSha256": "7" * 64,
            "dockerContext": "default",
            "dockerEndpoint": "npipe:////./pipe/docker_engine",
            "dockerServerId": "trusted-daemon-01",
            "osType": "linux",
        },
    }
    host_receipt_body = _canonical(host_receipt)
    observer_payload = _payload(
        OBSERVER_PAYLOAD_TYPE,
        candidate_contract=candidate_contract,
        challenge=CHALLENGE,
    )
    observer_payload["claims"] = {
        "artifact": OBSERVER_ARTIFACT,
        "artifactSha256": hashlib.sha256(observer_artifact_body).hexdigest(),
    }
    host_payload = _payload(
        HOST_PAYLOAD_TYPE,
        candidate_contract=candidate_contract,
        challenge=HOST_CHALLENGE,
    )
    host_payload["claims"] = {
        "artifact": HOST_RECEIPT_ARTIFACT,
        "artifactSha256": hashlib.sha256(host_receipt_body).hexdigest(),
    }
    return {
        "candidate_root": candidate_root,
        "observer_key": observer_key,
        "host_key": host_key,
        "keyring": keyring,
        "keyring_sha256": hashlib.sha256(keyring.read_bytes()).hexdigest(),
        "observer_artifact_body": observer_artifact_body,
        "host_receipt": host_receipt,
        "host_receipt_body": host_receipt_body,
        "observer_payload": observer_payload,
        "host_payload": host_payload,
        "observer_envelope_body": _canonical(
            _envelope(observer_key, OBSERVER_PAYLOAD_TYPE, observer_payload)
        ),
        "host_envelope_body": _canonical(_envelope(host_key, HOST_PAYLOAD_TYPE, host_payload)),
    }


def _verify_pair(
    module,
    pair: dict[str, object],
    *,
    public: bool = False,
    **overrides: object,
):
    arguments = {
        "observer_envelope_body": pair["observer_envelope_body"],
        "host_envelope_body": pair["host_envelope_body"],
        "observer_artifact_body": pair["observer_artifact_body"],
        "host_receipt_body": pair["host_receipt_body"],
        "trusted_keyring_path": pair["keyring"],
        "expected_trusted_keyring_sha256": pair["keyring_sha256"],
        "candidate_root": pair["candidate_root"],
        "release_run": RELEASE_RUN,
        "expected_environment_id": RELEASE_RUN["environmentId"],
        "expected_observer_challenge": CHALLENGE,
        "expected_host_challenge": HOST_CHALLENGE,
        "trusted_now": TRUSTED_NOW,
    }
    arguments.update(overrides)
    if public:
        return module.verify_gateway_trust_pair(**arguments)
    arguments.setdefault("trusted_keyring_platform_name", None)
    arguments.setdefault("trusted_owner_uid", None)
    return module._verify_gateway_trust_pair(**arguments)


def _trust_keyring_policy_stat(
    path: Path,
    *,
    keyring_path: Path,
    overrides: dict[Path, tuple[int, int]] | None = None,
) -> SimpleNamespace:
    resolved_path = Path(path)
    actual = resolved_path.stat()
    default_mode = stat.S_IFREG | 0o600 if resolved_path == keyring_path else stat.S_IFDIR | 0o755
    mode, owner_uid = (overrides or {}).get(resolved_path, (default_mode, 0))
    return SimpleNamespace(
        st_dev=actual.st_dev,
        st_ino=actual.st_ino,
        st_mode=mode,
        st_mtime_ns=actual.st_mtime_ns,
        st_size=actual.st_size,
        st_uid=owner_uid,
    )


def test_public_gateway_trust_pair_owns_the_runtime_keyring_os_policy(
    monkeypatch: pytest.MonkeyPatch,
    gateway_trust_pair: dict[str, object],
) -> None:
    module = _load_module()
    public_parameters = inspect.signature(module.verify_gateway_trust_pair).parameters

    assert "trusted_keyring_platform_name" not in public_parameters
    assert "trusted_owner_uid" not in public_parameters

    captured: dict[str, object] = {}
    verified = {"verifiedBy": "private gateway trust seam"}

    def private_verifier(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return verified

    monkeypatch.setattr(module, "_verify_gateway_trust_pair", private_verifier, raising=False)

    result = _verify_pair(module, gateway_trust_pair, public=True)

    assert result is verified
    assert captured["trusted_keyring_platform_name"] == module.os.name
    expected_owner_uid = (
        module.os.geteuid() if module.os.name == "posix" and hasattr(module.os, "geteuid") else None
    )
    assert captured["trusted_owner_uid"] == expected_owner_uid


def test_gateway_trust_contract_does_not_export_single_envelope_authority() -> None:
    module = _load_module()

    assert "parse_gateway_trust_envelope" not in module.__all__
    assert not hasattr(module, "parse_gateway_trust_envelope")


def test_candidate_contract_signature_detects_ctime_mode_and_owner_drift() -> None:
    module = _load_module()
    baseline = SimpleNamespace(
        st_dev=11,
        st_ino=22,
        st_size=33,
        st_mtime_ns=44,
        st_ctime_ns=55,
        st_mode=stat.S_IFREG | 0o600,
        st_uid=1000,
    )
    drifted_values = {
        "st_ctime_ns": baseline.st_ctime_ns + 1,
        "st_mode": stat.S_IFREG | 0o640,
        "st_uid": baseline.st_uid + 1,
    }

    undetected = []
    for field, value in drifted_values.items():
        changed = SimpleNamespace(**vars(baseline))
        setattr(changed, field, value)
        if module._file_signature(changed) == module._file_signature(baseline):
            undetected.append(field)

    assert undetected == []


def test_gateway_trust_pair_derives_candidate_from_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_trust_pair: dict[str, object],
) -> None:
    module = _load_module()
    candidate_root = gateway_trust_pair["candidate_root"]
    assert isinstance(candidate_root, Path)
    monkeypatch.setattr(
        module,
        "_read_trusted_keyring",
        lambda path, *, candidate_root, **_options: Path(path).read_bytes(),
    )

    verified = _verify_pair(module, gateway_trust_pair)

    assert verified["observer"]["candidate"] == CANDIDATE
    assert verified["hostProvisioner"]["candidate"] == CANDIDATE
    assert verified["hostReceipt"]["candidate"] == CANDIDATE


def test_gateway_trust_pair_rejects_caller_supplied_candidate_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_trust_pair: dict[str, object],
) -> None:
    module = _load_module()
    root_candidate = copy.deepcopy(CANDIDATE)
    root_candidate["sourceHead"] = "b" * 40
    root_candidate["releaseTag"] = "yfeistai-first-release-20260830-bbbbbbbb"
    gateway_trust_pair["candidate_root"] = _write_candidate_contract(
        tmp_path,
        candidate=root_candidate,
        name="candidate-contract-b",
    )
    monkeypatch.setattr(
        module,
        "_read_trusted_keyring",
        lambda path, *, candidate_root, **_options: Path(path).read_bytes(),
    )

    with pytest.raises(TypeError, match="candidate"):
        _verify_pair(
            module,
            gateway_trust_pair,
            candidate=CANDIDATE,
        )


@pytest.mark.parametrize(
    "drift",
    ("image-lock-candidate", "platform-compose"),
)
def test_gateway_trust_pair_rejects_candidate_root_contract_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_trust_pair: dict[str, object],
    drift: str,
) -> None:
    module = _load_module()
    candidate_root = gateway_trust_pair["candidate_root"]
    assert isinstance(candidate_root, Path)
    monkeypatch.setattr(
        module,
        "_read_trusted_keyring",
        lambda path, *, candidate_root, **_options: Path(path).read_bytes(),
    )
    if drift == "image-lock-candidate":
        lock_path = candidate_root / "deploy" / "image-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["candidate"]["sourceHead"] = "b" * 40
        lock_path.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        message = "candidate"
    else:
        compose_path = candidate_root / "docker-compose.platform.yml"
        compose_path.write_text(
            compose_path.read_text(encoding="utf-8") + "\n# untrusted topology drift\n",
            encoding="utf-8",
        )
        message = "Compose"

    with pytest.raises(ValueError, match=message):
        _verify_pair(module, gateway_trust_pair)


def test_gateway_trust_pair_rejects_stale_candidate_from_late_snapshot_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    gateway_trust_pair: dict[str, object],
) -> None:
    module = _load_module()
    candidate_root = gateway_trust_pair["candidate_root"]
    assert isinstance(candidate_root, Path)
    lock_path = candidate_root / "deploy" / "image-lock.json"
    overlay_path = candidate_root / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml"
    original_reader = module._read_candidate_contract_artifact
    overlay_reads = 0

    def read_then_rewrite_early_lock(lease) -> bytes:
        nonlocal overlay_reads
        result = original_reader(lease)
        if lease.path == overlay_path:
            overlay_reads += 1
            if overlay_reads == 3:
                with lock_path.open("r+b") as stream:
                    stream.seek(-1, 2)
                    assert stream.read(1) == b"\n"
                    stream.seek(-1, 2)
                    stream.write(b" ")
        return result

    monkeypatch.setattr(
        module,
        "_read_candidate_contract_artifact",
        read_then_rewrite_early_lock,
    )

    with pytest.raises(ValueError, match="candidate contract"):
        _verify_pair(module, gateway_trust_pair)

    assert overlay_reads == 4


def test_gateway_trust_pair_rejects_whole_candidate_set_aba_during_semantic_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_trust_pair: dict[str, object],
) -> None:
    module = _load_module()
    candidate_root = gateway_trust_pair["candidate_root"]
    assert isinstance(candidate_root, Path)
    alternate_root = _write_candidate_contract(
        tmp_path,
        name="candidate-contract-aba-b",
        fallback_digest_index=40,
    )
    candidate_claim = _candidate_contract_claim(candidate_root)
    alternate_claim = _candidate_contract_claim(alternate_root)
    assert alternate_claim != candidate_claim
    renderer = _load_support_module(
        "render_platform_compose.py",
        "render_platform_compose_for_gateway_cached_candidate_validation",
    )
    expected_cached_artifacts = tuple(
        (Path(relative), (candidate_root / relative).read_bytes())
        for relative in _CANDIDATE_ARTIFACT_RELATIVE_PATHS
    )
    original_opener = module._open_candidate_contract_artifact
    original_validator = module.validate_image_lock_bindings
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
                opened = module.os.fstat(lease.descriptor)
                current = lease.path.lstat()
            except OSError:
                continue
            if module._file_signature(opened) == lease.signature == module._file_signature(current):
                active_leases += 1
        semantic_lease_counts.append(active_leases)
        semantic_claims.append(alternate_claim)
        alternate_paths = module.candidate_artifact_paths(alternate_root)
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
            "candidate": copy.deepcopy(lock["candidate"]),
            "candidateContract": copy.deepcopy(candidate_claim),
        }

    monkeypatch.setattr(module, "_open_candidate_contract_artifact", track_candidate_lease)
    monkeypatch.setattr(module, "validate_image_lock_bindings", validate_alternate_semantic_bytes)
    monkeypatch.setattr(
        module,
        "validate_gateway_candidate_contract_bytes",
        validate_cached_candidate_bytes,
        raising=False,
    )

    verified: dict[str, object] | None = None
    error: ValueError | None = None
    try:
        verified = _verify_pair(module, gateway_trust_pair)
    except ValueError as caught:
        error = caught

    assert all(_candidate_descriptor_is_closed(module, lease) for lease in opened_leases)
    assert not (semantic_claims and cached_semantic_calls)
    if cached_semantic_calls:
        assert cached_semantic_calls == [
            tuple(content for _path, content in expected_cached_artifacts)
        ]
        assert error is None
        assert verified is not None
        assert verified["observer"]["candidate"] == CANDIDATE
        assert verified["observer"]["candidateContract"] == candidate_claim
        assert verified["hostProvisioner"]["candidate"] == CANDIDATE
        assert verified["hostProvisioner"]["candidateContract"] == candidate_claim
        assert verified["hostReceipt"]["candidate"] == CANDIDATE
        assert verified["hostReceipt"]["candidateContract"] == candidate_claim
        return

    assert semantic_claims, "candidate semantic validation was bypassed"
    assert semantic_claims == [alternate_claim]
    assert semantic_lease_counts == [5]
    assert error is not None
    assert "candidate contract" in str(error)


@pytest.mark.parametrize(
    "rebinding",
    ("image-lock-raw-bytes", "observer-signer-and-overlay"),
    ids=("image-lock-raw-bytes", "observer-signer-and-overlay"),
)
def test_gateway_trust_pair_rejects_unsigned_candidate_artifact_rebinding(
    gateway_trust_pair: dict[str, object],
    rebinding: str,
) -> None:
    module = _load_module()
    candidate_root = gateway_trust_pair["candidate_root"]
    assert isinstance(candidate_root, Path)

    baseline = _verify_pair(module, gateway_trust_pair)
    assert baseline["hostReceipt"]["candidate"] == CANDIDATE

    lock_path = candidate_root / "deploy" / "image-lock.json"
    original_lock_body = lock_path.read_bytes()
    lock = json.loads(original_lock_body)
    if rebinding == "image-lock-raw-bytes":
        rebound_lock_body = (
            json.dumps(
                lock,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        assert rebound_lock_body != original_lock_body
        lock_path.write_bytes(rebound_lock_body)
    else:
        signer = lock["supportImages"]["observer_signer"]
        original_reference = signer["reference"]
        rebound_digest = "sha256:" + "f" * 64
        assert rebound_digest not in lock["candidate"]["imageDigests"].values()
        signer["digest"] = rebound_digest
        signer["reference"] = f"{signer['repository']}:{signer['tag']}@{rebound_digest}"
        lock_path.write_text(
            json.dumps(lock, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        observer_overlay = (
            candidate_root / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml"
        )
        observer_body = observer_overlay.read_text(encoding="utf-8")
        assert observer_body.count(original_reference) == 2
        observer_overlay.write_text(
            observer_body.replace(original_reference, signer["reference"]),
            encoding="utf-8",
        )

    with pytest.raises(
        ValueError,
        match="candidate contract|production Compose image references",
    ):
        _verify_pair(module, gateway_trust_pair)


def test_trusted_keyring_reader_walks_every_absolute_component_without_following(
    tmp_path: Path,
) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    visited: list[Path] = []

    def path_lstat(path: Path) -> SimpleNamespace:
        visited.append(Path(path))
        return _trust_keyring_policy_stat(Path(path), keyring_path=keyring)

    body = module._read_trusted_keyring(
        keyring,
        candidate_root=candidate_root,
        platform_name="posix",
        trusted_owner_uid=0,
        path_lstat=path_lstat,
    )

    assert body == keyring.read_bytes()
    assert visited == [*reversed(keyring.parents), keyring]


@pytest.mark.parametrize(
    ("unsafe_path", "message"),
    (
        ("relative", "absolute"),
        ("ancestor-symlink", "symlink"),
        ("directory-owner", "owner"),
        ("directory-mode", "permissions|mode"),
        ("file-owner", "owner"),
        ("file-mode", "permissions|mode"),
    ),
    ids=(
        "relative",
        "ancestor-symlink",
        "directory-owner",
        "directory-mode",
        "file-owner",
        "file-mode",
    ),
)
def test_trusted_keyring_reader_rejects_unsafe_path_chain(
    tmp_path: Path,
    unsafe_path: str,
    message: str,
) -> None:
    module = _load_module()
    candidate_root, trusted_root = _case_roots(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    keyring = _write_keyring(
        trusted_root / "gateway-trust-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    requested = keyring
    overrides: dict[Path, tuple[int, int]] = {}
    if unsafe_path == "relative":
        requested = Path("trusted-controller/gateway-trust-keyring.json")
    elif unsafe_path == "ancestor-symlink":
        overrides[trusted_root] = (stat.S_IFLNK | 0o777, 0)
    elif unsafe_path == "directory-owner":
        overrides[trusted_root] = (stat.S_IFDIR | 0o755, 1000)
    elif unsafe_path == "directory-mode":
        overrides[trusted_root] = (stat.S_IFDIR | 0o777, 0)
    elif unsafe_path == "file-owner":
        overrides[keyring] = (stat.S_IFREG | 0o600, 1000)
    else:
        overrides[keyring] = (stat.S_IFREG | 0o644, 0)

    def path_lstat(path: Path) -> SimpleNamespace:
        return _trust_keyring_policy_stat(
            Path(path),
            keyring_path=keyring,
            overrides=overrides,
        )

    with pytest.raises(ValueError, match=message):
        module._read_trusted_keyring(
            requested,
            candidate_root=candidate_root,
            platform_name="posix",
            trusted_owner_uid=0,
            path_lstat=path_lstat,
        )


@pytest.mark.parametrize(
    "document_kind",
    ("trusted-keyring", "envelope", "payload", "challenge"),
)
def test_gateway_trust_schema_version_must_be_a_real_integer(
    tmp_path: Path,
    document_kind: str,
) -> None:
    if document_kind == "challenge":
        producer = _load_support_module(
            "gateway_trust_producer.py",
            "gateway_trust_producer_for_schema_version_contract",
        )
        with pytest.raises(ValueError, match="challenge.*schema|challenge document"):
            producer._challenge_for(
                {
                    "schemaVersion": True,
                    "observerChallenge": CHALLENGE,
                    "hostChallenge": HOST_CHALLENGE,
                },
                payload_type=OBSERVER_PAYLOAD_TYPE,
            )
        return

    module = _load_module()
    private_key = Ed25519PrivateKey.generate()
    keyring_path = _write_keyring(
        tmp_path / "trusted-keyring.json",
        [(private_key, (OBSERVER_PAYLOAD_TYPE,))],
    )
    keyring = json.loads(keyring_path.read_text(encoding="utf-8"))
    if document_kind == "trusted-keyring":
        keyring["schemaVersion"] = True
        with pytest.raises(ValueError, match="keyring.*schema"):
            module._trusted_keys(_canonical(keyring))
        return

    trusted_keys = module._trusted_keys(_canonical(keyring))
    payload = _payload(OBSERVER_PAYLOAD_TYPE)
    if document_kind == "payload":
        payload["schemaVersion"] = True
    envelope = _envelope(private_key, OBSERVER_PAYLOAD_TYPE, payload)
    if document_kind == "envelope":
        envelope["schemaVersion"] = True
    with pytest.raises(ValueError, match=f"{document_kind}.*schema"):
        module._parse_gateway_trust_envelope_from_keys(
            _canonical(envelope),
            expected_payload_type=OBSERVER_PAYLOAD_TYPE,
            trusted_keys=trusted_keys,
            candidate=CANDIDATE,
            candidate_contract=CANDIDATE_CONTRACT,
            release_run=RELEASE_RUN,
            expected_environment_id=RELEASE_RUN["environmentId"],
            expected_challenge=CHALLENGE,
            trusted_now=TRUSTED_NOW,
        )


def test_gateway_trust_pair_binds_external_observer_and_host_provisioner_artifacts(
    gateway_trust_pair: dict[str, object],
) -> None:
    module = _load_module()

    verified = _verify_pair(module, gateway_trust_pair)

    assert verified == {
        "observer": gateway_trust_pair["observer_payload"],
        "hostProvisioner": gateway_trust_pair["host_payload"],
        "hostReceipt": gateway_trust_pair["host_receipt"],
    }


@pytest.mark.parametrize(
    "invalid_input",
    (
        "observer-claim-swapped",
        "host-claim-swapped",
        "host-receipt-noncanonical",
        "host-receipt-schema",
        "keyring-inside-candidate",
        "keyring-digest-mismatch",
        "shared-challenge",
        "same-key",
        "multi-role-key",
        "observer-artifact-digest-mismatch",
        "host-receipt-digest-mismatch",
        "candidate-mismatch",
        "release-run-mismatch",
        "environment-mismatch",
        "expired",
    ),
)
def test_gateway_trust_pair_rejects_untrusted_or_swapped_inputs(
    gateway_trust_pair: dict[str, object],
    invalid_input: str,
) -> None:
    module = _load_module()
    observer_key = gateway_trust_pair["observer_key"]
    host_key = gateway_trust_pair["host_key"]
    overrides: dict[str, object] = {}
    message = "gateway trust"

    if invalid_input == "observer-claim-swapped":
        payload = copy.deepcopy(gateway_trust_pair["observer_payload"])
        payload["claims"]["artifact"] = HOST_RECEIPT_ARTIFACT
        gateway_trust_pair["observer_envelope_body"] = _canonical(
            _envelope(observer_key, OBSERVER_PAYLOAD_TYPE, payload)
        )
        message = "observer.*artifact"
    elif invalid_input == "host-claim-swapped":
        payload = copy.deepcopy(gateway_trust_pair["host_payload"])
        payload["claims"]["artifact"] = OBSERVER_ARTIFACT
        gateway_trust_pair["host_envelope_body"] = _canonical(
            _envelope(host_key, HOST_PAYLOAD_TYPE, payload)
        )
        message = "host.*artifact"
    elif invalid_input == "host-receipt-noncanonical":
        receipt_body = (
            json.dumps(gateway_trust_pair["host_receipt"], ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        payload = copy.deepcopy(gateway_trust_pair["host_payload"])
        payload["claims"]["artifactSha256"] = hashlib.sha256(receipt_body).hexdigest()
        gateway_trust_pair["host_receipt_body"] = receipt_body
        gateway_trust_pair["host_envelope_body"] = _canonical(
            _envelope(host_key, HOST_PAYLOAD_TYPE, payload)
        )
        message = "canonical"
    elif invalid_input == "host-receipt-schema":
        receipt = copy.deepcopy(gateway_trust_pair["host_receipt"])
        receipt["host"].pop("osType")
        receipt["host"]["unexpected"] = True
        receipt_body = _canonical(receipt)
        payload = copy.deepcopy(gateway_trust_pair["host_payload"])
        payload["claims"]["artifactSha256"] = hashlib.sha256(receipt_body).hexdigest()
        gateway_trust_pair["host_receipt_body"] = receipt_body
        gateway_trust_pair["host_envelope_body"] = _canonical(
            _envelope(host_key, HOST_PAYLOAD_TYPE, payload)
        )
        message = "schema"
    elif invalid_input == "keyring-inside-candidate":
        keyring = _write_keyring(
            gateway_trust_pair["candidate_root"] / "self-issued-keyring.json",
            [
                (observer_key, (OBSERVER_PAYLOAD_TYPE,)),
                (host_key, (HOST_PAYLOAD_TYPE,)),
            ],
        )
        overrides["trusted_keyring_path"] = keyring
        overrides["expected_trusted_keyring_sha256"] = hashlib.sha256(
            keyring.read_bytes()
        ).hexdigest()
        message = "outside candidate"
    elif invalid_input == "keyring-digest-mismatch":
        overrides["expected_trusted_keyring_sha256"] = "f" * 64
        message = "keyring.*digest"
    elif invalid_input == "shared-challenge":
        payload = copy.deepcopy(gateway_trust_pair["host_payload"])
        payload["challenge"] = CHALLENGE
        gateway_trust_pair["host_envelope_body"] = _canonical(
            _envelope(host_key, HOST_PAYLOAD_TYPE, payload)
        )
        overrides["expected_host_challenge"] = CHALLENGE
        message = "independent.*challenge"
    elif invalid_input == "same-key":
        gateway_trust_pair["host_envelope_body"] = _canonical(
            _envelope(observer_key, HOST_PAYLOAD_TYPE, gateway_trust_pair["host_payload"])
        )
        keyring = _write_keyring(
            gateway_trust_pair["keyring"],
            [(observer_key, (OBSERVER_PAYLOAD_TYPE, HOST_PAYLOAD_TYPE))],
        )
        overrides["expected_trusted_keyring_sha256"] = hashlib.sha256(
            keyring.read_bytes()
        ).hexdigest()
        message = "different.*key|role"
    elif invalid_input == "multi-role-key":
        keyring = _write_keyring(
            gateway_trust_pair["keyring"],
            [
                (observer_key, (OBSERVER_PAYLOAD_TYPE, HOST_PAYLOAD_TYPE)),
                (host_key, (HOST_PAYLOAD_TYPE,)),
            ],
        )
        overrides["expected_trusted_keyring_sha256"] = hashlib.sha256(
            keyring.read_bytes()
        ).hexdigest()
        message = "single.*role|role"
    elif invalid_input == "observer-artifact-digest-mismatch":
        gateway_trust_pair["observer_artifact_body"] = _canonical(
            {"schemaVersion": 1, "producer": "tampered-observer"}
        )
        message = "observer.*digest"
    elif invalid_input == "host-receipt-digest-mismatch":
        receipt = copy.deepcopy(gateway_trust_pair["host_receipt"])
        receipt["host"]["physicalHostIdSha256"] = "8" * 64
        gateway_trust_pair["host_receipt_body"] = _canonical(receipt)
        message = "host.*digest"
    elif invalid_input == "candidate-mismatch":
        payload = copy.deepcopy(gateway_trust_pair["observer_payload"])
        payload["candidate"]["sourceHead"] = "b" * 40
        gateway_trust_pair["observer_envelope_body"] = _canonical(
            _envelope(observer_key, OBSERVER_PAYLOAD_TYPE, payload)
        )
        message = "candidate"
    elif invalid_input == "release-run-mismatch":
        release_run = copy.deepcopy(RELEASE_RUN)
        release_run["runId"] = "replayed-run"
        overrides["release_run"] = release_run
        message = "release run"
    elif invalid_input == "environment-mismatch":
        overrides["expected_environment_id"] = "other-environment"
        message = "environment"
    else:
        overrides["trusted_now"] = EXPIRES_AT
        message = "expired"

    with pytest.raises(ValueError, match=message):
        _verify_pair(module, gateway_trust_pair, **overrides)
