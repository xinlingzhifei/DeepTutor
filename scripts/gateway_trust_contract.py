"""Verify externally signed Gateway observation and host trust envelopes."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_GATEWAY_TRUST_ENVELOPE_BYTES = 64 * 1024
MAX_GATEWAY_TRUST_KEYRING_BYTES = 64 * 1024

_SIGNATURE_DOMAIN = b"yfeistai.gateway-trust-envelope.v1\0"
_PAYLOAD_TYPES = {"gateway-observer", "gateway-host-provisioner"}
_ISSUERS = {
    "gateway-observer": "external-observer-authority",
    "gateway-host-provisioner": "deployment-authority",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_FIELDS = {
    "schemaVersion",
    "payloadType",
    "algorithm",
    "keyId",
    "payload",
    "signatureBase64",
}
_PAYLOAD_FIELDS = {
    "schemaVersion",
    "issuer",
    "issuedAt",
    "expiresAt",
    "challenge",
    "candidate",
    "releaseRun",
    "environmentId",
    "claims",
}
_KEY_FIELDS = {
    "algorithm",
    "keyId",
    "payloadTypes",
    "publicKeySpkiBase64",
}
_OBSERVER_ARTIFACT = "runtime/gateway-external-observer-attestation.json"
_HOST_RECEIPT_ARTIFACT = "runtime/gateway-docker-host-provisioning-receipt.json"
_HOST_RECEIPT_FIELDS = {
    "schemaVersion",
    "producer",
    "candidate",
    "releaseRun",
    "environmentId",
    "host",
}
_HOST_FIELDS = {
    "physicalHostIdSha256",
    "dockerContext",
    "dockerEndpoint",
    "dockerServerId",
    "osType",
}


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_document(body: bytes, *, limit: int, label: str) -> dict[str, object]:
    if not isinstance(body, bytes) or not body or len(body) > limit:
        raise ValueError(f"{label} size is invalid")
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError(f"{label} JSON is invalid") from None
    if not isinstance(document, dict) or _canonical_json(document) != body:
        raise ValueError(f"{label} is not canonical")
    return document


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )


def _read_trusted_keyring(path: Path, *, candidate_root: Path) -> bytes:
    try:
        candidate = Path(candidate_root).resolve(strict=True)
        requested = Path(path)
        requested_stat = requested.lstat()
        resolved = requested.resolve(strict=True)
    except OSError:
        raise ValueError("gateway trust keyring is unavailable") from None
    if requested.is_symlink() or not stat.S_ISREG(requested_stat.st_mode):
        raise ValueError("gateway trust keyring is unsafe")
    if resolved == candidate or candidate in resolved.parents:
        raise ValueError("gateway trust keyring must be outside candidate root")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError:
        raise ValueError("gateway trust keyring is unavailable") from None
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_size <= 0
            or opened_stat.st_size > MAX_GATEWAY_TRUST_KEYRING_BYTES
        ):
            raise ValueError("gateway trust keyring size is invalid")
        chunks: list[bytes] = []
        remaining = opened_stat.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ValueError("gateway trust keyring changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("gateway trust keyring changed while being read")
        final_stat = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current_stat = resolved.stat()
    except OSError:
        raise ValueError("gateway trust keyring changed while being read") from None
    if _file_signature(opened_stat) != _file_signature(final_stat) or _file_signature(
        opened_stat
    ) != _file_signature(current_stat):
        raise ValueError("gateway trust keyring changed while being read")
    return b"".join(chunks)


def _trusted_keys(
    body: bytes,
) -> dict[str, tuple[Ed25519PublicKey, frozenset[str]]]:
    document = _canonical_document(
        body,
        limit=MAX_GATEWAY_TRUST_KEYRING_BYTES,
        label="gateway trust keyring",
    )
    keys = document.get("keys")
    if set(document) != {"schemaVersion", "keys"} or document.get("schemaVersion") != 1:
        raise ValueError("gateway trust keyring schema is invalid")
    if not isinstance(keys, list) or not keys:
        raise ValueError("gateway trust keyring schema is invalid")
    trusted: dict[str, tuple[Ed25519PublicKey, frozenset[str]]] = {}
    for raw in keys:
        if not isinstance(raw, dict) or set(raw) != _KEY_FIELDS:
            raise ValueError("gateway trust keyring schema is invalid")
        key_id = raw.get("keyId")
        payload_types = raw.get("payloadTypes")
        encoded_key = raw.get("publicKeySpkiBase64")
        if (
            raw.get("algorithm") != "ed25519"
            or not isinstance(key_id, str)
            or _SHA256.fullmatch(key_id) is None
            or not isinstance(payload_types, list)
            or not payload_types
            or any(item not in _PAYLOAD_TYPES for item in payload_types)
            or len(set(payload_types)) != len(payload_types)
            or not isinstance(encoded_key, str)
            or key_id in trusted
        ):
            raise ValueError("gateway trust keyring schema is invalid")
        try:
            spki = base64.b64decode(encoded_key, validate=True)
            public_key = serialization.load_der_public_key(spki)
        except (ValueError, TypeError):
            raise ValueError("gateway trust keyring public key is invalid") from None
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("gateway trust keyring public key is invalid")
        canonical_spki = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if spki != canonical_spki or hashlib.sha256(spki).hexdigest() != key_id:
            raise ValueError("gateway trust key id is invalid")
        trusted[key_id] = (public_key, frozenset(payload_types))
    return trusted


def _utc_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"gateway trust {label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError(f"gateway trust {label} timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"gateway trust {label} timestamp is invalid")
    return parsed


def _parse_gateway_trust_envelope_from_keys(
    body: bytes,
    *,
    expected_payload_type: str,
    trusted_keys: Mapping[str, tuple[Ed25519PublicKey, frozenset[str]]],
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_environment_id: str,
    expected_challenge: str,
    trusted_now: str,
) -> tuple[dict[str, object], str, frozenset[str]]:

    if expected_payload_type not in _PAYLOAD_TYPES:
        raise ValueError("gateway trust payload role is invalid")
    document = _canonical_document(
        body,
        limit=MAX_GATEWAY_TRUST_ENVELOPE_BYTES,
        label="gateway trust envelope",
    )
    if set(document) != _ENVELOPE_FIELDS:
        if "signatureBase64" not in document:
            raise ValueError("gateway trust signature is missing")
        raise ValueError("gateway trust envelope schema is invalid")
    payload_type = document.get("payloadType")
    key_id = document.get("keyId")
    payload = document.get("payload")
    encoded_signature = document.get("signatureBase64")
    if (
        document.get("schemaVersion") != 1
        or payload_type != expected_payload_type
        or document.get("algorithm") != "ed25519"
        or not isinstance(key_id, str)
        or _SHA256.fullmatch(key_id) is None
        or not isinstance(payload, dict)
        or not isinstance(encoded_signature, str)
    ):
        raise ValueError("gateway trust envelope schema is invalid")

    trusted_entry = trusted_keys.get(key_id)
    if trusted_entry is None:
        raise ValueError("gateway trust signature key is not trusted")
    public_key, authorized_payload_types = trusted_entry
    if payload_type not in authorized_payload_types:
        raise ValueError("gateway trust key role is invalid")
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, TypeError):
        raise ValueError("gateway trust signature is invalid") from None
    if len(signature) != 64:
        raise ValueError("gateway trust signature is invalid")
    signature_input = (
        _SIGNATURE_DOMAIN + payload_type.encode("ascii") + b"\0" + _canonical_json(payload)
    )
    try:
        public_key.verify(signature, signature_input)
    except (InvalidSignature, ValueError):
        raise ValueError("gateway trust signature is invalid") from None

    if set(payload) != _PAYLOAD_FIELDS or payload.get("schemaVersion") != 1:
        raise ValueError("gateway trust payload schema is invalid")
    if payload.get("issuer") != _ISSUERS[payload_type]:
        raise ValueError("gateway trust payload issuer is invalid")
    issued_at = _utc_timestamp(payload.get("issuedAt"), label="issued")
    expires_at = _utc_timestamp(payload.get("expiresAt"), label="expiry")
    now = _utc_timestamp(trusted_now, label="trusted now")
    if issued_at > now:
        raise ValueError("gateway trust envelope was issued in the future")
    if expires_at <= issued_at or now >= expires_at:
        raise ValueError("gateway trust envelope is expired")
    if (
        not isinstance(expected_challenge, str)
        or _SHA256.fullmatch(expected_challenge) is None
        or payload.get("challenge") != expected_challenge
    ):
        raise ValueError("gateway trust challenge does not match")
    if payload.get("candidate") != dict(candidate):
        raise ValueError("gateway trust candidate does not match")
    if payload.get("releaseRun") != dict(release_run):
        raise ValueError("gateway trust release run does not match")
    if payload.get("environmentId") != expected_environment_id:
        raise ValueError("gateway trust environment does not match")
    claims = payload.get("claims")
    if (
        not isinstance(claims, dict)
        or set(claims) != {"artifact", "artifactSha256"}
        or not isinstance(claims.get("artifact"), str)
        or not claims["artifact"]
        or not isinstance(claims.get("artifactSha256"), str)
        or _SHA256.fullmatch(claims["artifactSha256"]) is None
        or claims["artifactSha256"] == "0" * 64
    ):
        raise ValueError("gateway trust payload claims are invalid")
    return payload, key_id, authorized_payload_types


def parse_gateway_trust_envelope(
    body: bytes,
    *,
    expected_payload_type: str,
    trusted_keyring_path: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_environment_id: str,
    expected_challenge: str,
    trusted_now: str,
) -> dict[str, object]:
    """Return a verified payload rooted in a keyring outside the candidate."""

    keyring_body = _read_trusted_keyring(
        Path(trusted_keyring_path),
        candidate_root=Path(candidate_root),
    )
    payload, _key_id, _authorized_payload_types = _parse_gateway_trust_envelope_from_keys(
        body,
        expected_payload_type=expected_payload_type,
        trusted_keys=_trusted_keys(keyring_body),
        candidate=candidate,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
        expected_challenge=expected_challenge,
        trusted_now=trusted_now,
    )
    return payload


def _gateway_trust_claim(
    payload: Mapping[str, object],
    *,
    expected_artifact: str,
    artifact_body: bytes,
    label: str,
) -> None:
    claims = payload.get("claims")
    if not isinstance(claims, Mapping) or claims.get("artifact") != expected_artifact:
        raise ValueError(f"gateway trust {label} artifact is invalid")
    if not isinstance(artifact_body, bytes) or not artifact_body:
        raise ValueError(f"gateway trust {label} artifact bytes are invalid")
    if claims.get("artifactSha256") != hashlib.sha256(artifact_body).hexdigest():
        raise ValueError(f"gateway trust {label} artifact digest does not match")


def _parse_gateway_host_receipt(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_environment_id: str,
) -> dict[str, object]:
    receipt = _canonical_document(
        body,
        limit=MAX_GATEWAY_TRUST_ENVELOPE_BYTES,
        label="gateway host provisioning receipt",
    )
    if (
        set(receipt) != _HOST_RECEIPT_FIELDS
        or type(receipt.get("schemaVersion")) is not int
        or receipt.get("schemaVersion") != 1
        or receipt.get("producer") != "gateway-docker-host-provisioner"
        or receipt.get("candidate") != dict(candidate)
        or receipt.get("releaseRun") != dict(release_run)
        or receipt.get("environmentId") != expected_environment_id
    ):
        raise ValueError("gateway trust host receipt schema or binding is invalid")
    host = receipt.get("host")
    if not isinstance(host, dict) or set(host) != _HOST_FIELDS:
        raise ValueError("gateway trust host receipt schema is invalid")
    physical_host_id = host.get("physicalHostIdSha256")
    endpoint = host.get("dockerEndpoint")
    server_id = host.get("dockerServerId")
    if (
        not isinstance(physical_host_id, str)
        or _SHA256.fullmatch(physical_host_id) is None
        or physical_host_id == "0" * 64
        or host.get("dockerContext") != "default"
        or host.get("osType") != "linux"
        or not isinstance(endpoint, str)
        or not endpoint
        or endpoint != endpoint.strip()
        or not isinstance(server_id, str)
        or not server_id
        or server_id != server_id.strip()
    ):
        raise ValueError("gateway trust host receipt host identity is invalid")
    return receipt


def verify_gateway_trust_pair(
    *,
    observer_envelope_body: bytes,
    host_envelope_body: bytes,
    observer_artifact_body: bytes,
    host_receipt_body: bytes,
    trusted_keyring_path: Path,
    expected_trusted_keyring_sha256: str,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_environment_id: str,
    expected_observer_challenge: str,
    expected_host_challenge: str,
    trusted_now: str,
) -> dict[str, object]:
    """Verify the role-separated observer and host provisioning trust roots."""

    if (
        not isinstance(expected_observer_challenge, str)
        or _SHA256.fullmatch(expected_observer_challenge) is None
        or expected_observer_challenge == "0" * 64
        or not isinstance(expected_host_challenge, str)
        or _SHA256.fullmatch(expected_host_challenge) is None
        or expected_host_challenge == "0" * 64
        or expected_observer_challenge == expected_host_challenge
    ):
        raise ValueError("gateway trust independent challenges are required")
    if (
        not isinstance(expected_trusted_keyring_sha256, str)
        or _SHA256.fullmatch(expected_trusted_keyring_sha256) is None
        or expected_trusted_keyring_sha256 == "0" * 64
    ):
        raise ValueError("gateway trust keyring digest is invalid")
    keyring_body = _read_trusted_keyring(
        Path(trusted_keyring_path),
        candidate_root=Path(candidate_root),
    )
    if hashlib.sha256(keyring_body).hexdigest() != expected_trusted_keyring_sha256:
        raise ValueError("gateway trust keyring digest does not match")
    trusted_keys = _trusted_keys(keyring_body)
    observer, observer_key_id, observer_roles = _parse_gateway_trust_envelope_from_keys(
        observer_envelope_body,
        expected_payload_type="gateway-observer",
        trusted_keys=trusted_keys,
        candidate=candidate,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
        expected_challenge=expected_observer_challenge,
        trusted_now=trusted_now,
    )
    host, host_key_id, host_roles = _parse_gateway_trust_envelope_from_keys(
        host_envelope_body,
        expected_payload_type="gateway-host-provisioner",
        trusted_keys=trusted_keys,
        candidate=candidate,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
        expected_challenge=expected_host_challenge,
        trusted_now=trusted_now,
    )
    if observer_key_id == host_key_id:
        raise ValueError("gateway trust envelopes must use different keys")
    if observer_roles != frozenset({"gateway-observer"}) or host_roles != frozenset(
        {"gateway-host-provisioner"}
    ):
        raise ValueError("gateway trust keys must each have a single role")
    _gateway_trust_claim(
        observer,
        expected_artifact=_OBSERVER_ARTIFACT,
        artifact_body=observer_artifact_body,
        label="observer",
    )
    _gateway_trust_claim(
        host,
        expected_artifact=_HOST_RECEIPT_ARTIFACT,
        artifact_body=host_receipt_body,
        label="host",
    )
    host_receipt = _parse_gateway_host_receipt(
        host_receipt_body,
        candidate=candidate,
        release_run=release_run,
        expected_environment_id=expected_environment_id,
    )
    return {
        "observer": observer,
        "hostProvisioner": host,
        "hostReceipt": host_receipt,
    }


__all__ = [
    "MAX_GATEWAY_TRUST_ENVELOPE_BYTES",
    "MAX_GATEWAY_TRUST_KEYRING_BYTES",
    "parse_gateway_trust_envelope",
    "verify_gateway_trust_pair",
]
