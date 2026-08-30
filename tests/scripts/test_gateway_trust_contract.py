from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

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
SIGNATURE_DOMAIN = b"yfeistai.gateway-trust-envelope.v1\0"


def _load_module():
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
        "schemaVersion": 1,
        "issuer": issuer,
        "issuedAt": issued_at,
        "expiresAt": expires_at,
        "challenge": challenge,
        "candidate": copy.deepcopy(candidate if candidate is not None else CANDIDATE),
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
        "schemaVersion": 1,
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


def _parse(
    module,
    body: bytes,
    *,
    payload_type: str,
    keyring_path: Path,
    candidate_root: Path,
    candidate: dict[str, object] | None = None,
    release_run: dict[str, str] | None = None,
    environment_id: str = RELEASE_RUN["environmentId"],
    challenge: str = CHALLENGE,
    trusted_now: str = TRUSTED_NOW,
):
    return module.parse_gateway_trust_envelope(
        body,
        expected_payload_type=payload_type,
        trusted_keyring_path=keyring_path,
        candidate_root=candidate_root,
        candidate=candidate if candidate is not None else CANDIDATE,
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
    candidate_root, trusted_root = _case_roots(tmp_path)
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
        "schemaVersion": 1,
        "producer": "gateway-docker-host-provisioner",
        "candidate": copy.deepcopy(CANDIDATE),
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
        challenge=CHALLENGE,
    )
    observer_payload["claims"] = {
        "artifact": OBSERVER_ARTIFACT,
        "artifactSha256": hashlib.sha256(observer_artifact_body).hexdigest(),
    }
    host_payload = _payload(
        HOST_PAYLOAD_TYPE,
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


def _verify_pair(module, pair: dict[str, object], **overrides: object):
    arguments = {
        "observer_envelope_body": pair["observer_envelope_body"],
        "host_envelope_body": pair["host_envelope_body"],
        "observer_artifact_body": pair["observer_artifact_body"],
        "host_receipt_body": pair["host_receipt_body"],
        "trusted_keyring_path": pair["keyring"],
        "expected_trusted_keyring_sha256": pair["keyring_sha256"],
        "candidate_root": pair["candidate_root"],
        "candidate": CANDIDATE,
        "release_run": RELEASE_RUN,
        "expected_environment_id": RELEASE_RUN["environmentId"],
        "expected_observer_challenge": CHALLENGE,
        "expected_host_challenge": HOST_CHALLENGE,
        "trusted_now": TRUSTED_NOW,
    }
    arguments.update(overrides)
    return module.verify_gateway_trust_pair(**arguments)


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
        candidate = copy.deepcopy(CANDIDATE)
        candidate["sourceHead"] = "b" * 40
        overrides["candidate"] = candidate
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
