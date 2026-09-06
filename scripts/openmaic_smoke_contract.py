"""Strict contract for one candidate-bound live OpenMAIC smoke report."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

OPENMAIC_SMOKE_SCHEMA_VERSION = 1
OPENMAIC_SMOKE_PRODUCER = "openmaic-smoke"
OPENMAIC_DEDICATED_OUTAGE_PRODUCER = "openmaic-dedicated-outage"
OPENMAIC_SHARED_INGRESS_OBSERVER_PRODUCER = "openmaic-shared-ingress-observer"
OPENMAIC_SHARED_INGRESS_OBSERVER_SCHEMA_VERSION = 2
OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION = 3
MAX_OPENMAIC_SMOKE_REPORT_BYTES = 64 * 1024
MAX_OPENMAIC_IMAGE_LOCK_BYTES = 1024 * 1024

_SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
_OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
_CUSTOM_IMAGE_NAMES = {"deeptutor", "openmaic", "openmaic_render"}
_RUNTIME_ATTESTATION_ARTIFACT = "runtime/runtime-attestation.json"
_SHARED_INGRESS_OBSERVER_ATTESTATION_ARTIFACT = (
    "runtime/openmaic-shared-ingress-observer-attestation.json"
)
_SHARED_INGRESS_OBSERVER_SIGNATURE_ARTIFACT = (
    "runtime/openmaic-shared-ingress-observer-attestation.sig"
)
_SHARED_BINDING = {
    "routeId": "shared-primary",
    "providerProfileId": "platform-default",
    "workerPoolRef": "shared-generation",
    "queueRef": "openmaic.shared",
}
_DEDICATED_BINDING_FIELDS = {
    "routeId",
    "routeTenantId",
    "routeOwnerKey",
    "providerProfileId",
    "providerScope",
    "providerTenantId",
    "providerOwnerKey",
    "workerPoolRef",
    "queueRef",
    "attemptCount",
    "sharedRouteAttemptCount",
    "dedicatedRouteAttemptCount",
    "selectedRouteAttemptCount",
    "unavailableRouteAttemptCount",
    "routeAttemptHistoryComplete",
}

_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_RELEASE_TAG = re.compile(r"^yfeistai-first-release-[0-9]{8}-([0-9a-f]{8})$")
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "ticket",
    "token",
)


def openmaic_shared_plane_command_record() -> dict[str, object]:
    """Return the secret-free fixed command recorded in release evidence."""

    return {
        "runner": "python",
        "script": "scripts/openmaic_smoke_probe.py",
        "arguments": ["--plane", "shared", "--profile", "first-release"],
    }


def openmaic_dedicated_plane_command_record() -> dict[str, object]:
    """Return the secret-free fixed dedicated-plane command record."""

    return {
        "runner": "python",
        "script": "scripts/openmaic_smoke_probe.py",
        "arguments": ["--plane", "dedicated", "--profile", "first-release"],
    }


def openmaic_dedicated_outage_command_record() -> dict[str, object]:
    """Return the fixed command record reserved for the outage producer."""

    return {
        "runner": "python",
        "script": "scripts/openmaic_dedicated_outage_probe.py",
        "arguments": ["--profile", "first-release"],
    }


def canonical_openmaic_smoke_report(report: Mapping[str, object]) -> bytes:
    """Serialize an OpenMAIC smoke report using the only accepted JSON form."""

    return (
        json.dumps(
            dict(report),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def canonical_openmaic_dedicated_outage_attestation(
    report: Mapping[str, object],
) -> bytes:
    """Serialize one dedicated outage attestation in its only accepted form."""

    return canonical_openmaic_smoke_report(report)


def canonical_openmaic_dedicated_outage_attempt_marker(
    marker: Mapping[str, object],
) -> bytes:
    """Serialize one durable outage-attempt marker in its only accepted form."""

    return canonical_openmaic_smoke_report(marker)


def canonical_openmaic_shared_ingress_observer_attestation(
    report: Mapping[str, object],
) -> bytes:
    """Serialize shared-ingress observer provenance in its only accepted form."""

    return canonical_openmaic_smoke_report(report)


def openmaic_observer_attestation_challenge(public_key_spki_sha256: str) -> str:
    """Derive one release-key-scoped, non-secret attestation challenge."""

    if not _valid_sha256(public_key_spki_sha256):
        raise ValueError("OpenMAIC observer attestation challenge is invalid")
    return hashlib.sha256(
        b"yfeistai-openmaic-observer-attestation-v1\0" + bytes.fromhex(public_key_spki_sha256)
    ).hexdigest()


def current_openmaic_observer_candidate_binding(candidate_root: Path) -> dict[str, str]:
    """Hash the canonical candidate and exact current image-lock bytes."""

    descriptor: int | None = None
    try:
        supplied_root = Path(candidate_root)
        if not supplied_root.is_absolute():
            raise OSError
        root = supplied_root.resolve(strict=True)
        if supplied_root != root or not root.is_dir():
            raise OSError
        lock_path = root / "deploy" / "image-lock.json"
        if any(item.is_symlink() for item in (lock_path, *lock_path.parents)):
            raise OSError
        resolved_lock = lock_path.resolve(strict=True)
        if resolved_lock != lock_path:
            raise OSError
        before = resolved_lock.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_OPENMAIC_IMAGE_LOCK_BYTES:
            raise OSError
        descriptor = os.open(
            resolved_lock,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        body = os.read(descriptor, MAX_OPENMAIC_IMAGE_LOCK_BYTES + 1)
        after = os.fstat(descriptor)
        final = resolved_lock.lstat()

        def identity(details: os.stat_result) -> tuple[int, ...]:
            return (
                details.st_dev,
                details.st_ino,
                details.st_size,
                details.st_mtime_ns,
                details.st_ctime_ns,
                details.st_mode,
                details.st_uid,
            )

        if (
            not body
            or len(body) > MAX_OPENMAIC_IMAGE_LOCK_BYTES
            or identity(before) != identity(opened)
            or identity(opened) != identity(after)
            or identity(opened) != identity(final)
        ):
            raise OSError
        lock = json.loads(body)
        if (
            not isinstance(lock, dict)
            or set(lock) != {"schemaVersion", "candidate", "images", "supportImages"}
            or lock.get("schemaVersion") != 3
            or not isinstance(lock.get("images"), dict)
            or not isinstance(lock.get("supportImages"), dict)
        ):
            raise ValueError
        candidate = lock.get("candidate")
        _validate_candidate(candidate)
        assert isinstance(candidate, dict)
        candidate_body = json.dumps(
            candidate,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC observer candidate binding is invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {
        "candidateSha256": hashlib.sha256(candidate_body).hexdigest(),
        "imageLockSha256": hashlib.sha256(body).hexdigest(),
    }


def _exact_json_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _exact_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _exact_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _contains_sensitive_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                return True
            if _contains_sensitive_field(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_field(item) for item in value)
    return False


def _contains_forbidden_secret_value(value: object, secrets: tuple[bytes, ...]) -> bool:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return any(secret in encoded for secret in secrets)
    if isinstance(value, dict):
        return any(_contains_forbidden_secret_value(item, secrets) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_secret_value(item, secrets) for item in value)
    return False


def _valid_public_id(raw: object) -> bool:
    return isinstance(raw, str) and _PUBLIC_ID.fullmatch(raw) is not None


def _valid_sha256(raw: object) -> bool:
    return isinstance(raw, str) and _SHA256.fullmatch(raw) is not None and raw != "0" * 64


def _valid_observed_at(raw: object) -> bool:
    if not isinstance(raw, str) or _OBSERVED_AT.fullmatch(raw) is None:
        return False
    try:
        datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _valid_base_url(raw: object) -> bool:
    if not isinstance(raw, str) or not raw or raw != raw.rstrip("/"):
        return False
    parsed = urlsplit(raw)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _valid_absolute_url(raw: object) -> bool:
    if not isinstance(raw, str) or not raw or raw != raw.rstrip("/"):
        return False
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/")
        and parsed.path != "/"
        and not parsed.query
        and not parsed.fragment
    )


def _canonical_origin(raw: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
        if hostname is None:
            raise ValueError
        canonical_host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not canonical_host:
            raise ValueError
    except (UnicodeError, ValueError) as exc:
        raise ValueError("OpenMAIC origin is invalid") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("OpenMAIC origin is invalid")
    return scheme, canonical_host, port or (443 if scheme == "https" else 80)


def _url_origin(raw: str) -> str:
    scheme, hostname, port = _canonical_origin(raw)
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{rendered_host}{suffix}"


def _canonical_internal_proxy_url(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("OpenMAIC internal proxy URL is invalid")
    try:
        parsed = urlsplit(raw)
        explicit_port = parsed.port
        canonical = _url_origin(raw)
    except ValueError as exc:
        raise ValueError("OpenMAIC internal proxy URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or explicit_port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or raw != canonical
    ):
        raise ValueError("OpenMAIC internal proxy URL is invalid")
    return canonical


def parse_openmaic_shared_ingress_observer_attestation(
    body: bytes,
    *,
    release_run: Mapping[str, str],
    signature: bytes | None = None,
    public_key_path: Path | None = None,
    candidate_root: Path | None = None,
    expected_public_key_spki_sha256: str | None = None,
) -> dict[str, object]:
    """Parse observer provenance, retaining an explicit offline unsigned seam.

    Live evidence must use ``verify_openmaic_shared_ingress_observer_attestation``.
    The optional signature arguments remain only for offline compatibility and
    are deliberately all-or-nothing.
    """

    if not isinstance(body, bytes) or not body or len(body) > MAX_OPENMAIC_SMOKE_REPORT_BYTES:
        raise ValueError("OpenMAIC shared-ingress observer attestation size is invalid")
    signature_inputs = (
        signature,
        public_key_path,
        candidate_root,
        expected_public_key_spki_sha256,
    )
    if any(value is not None for value in signature_inputs):
        if any(value is None for value in signature_inputs):
            raise ValueError("OpenMAIC shared-ingress observer signature inputs are incomplete")
        assert signature is not None
        assert public_key_path is not None
        assert candidate_root is not None
        assert expected_public_key_spki_sha256 is not None
        descriptor: int | None = None
        try:
            supplied_root = Path(candidate_root)
            if not supplied_root.is_absolute():
                raise OSError
            root = supplied_root.resolve(strict=True)
            if supplied_root != root or not root.is_dir():
                raise OSError
            anchor = Path(public_key_path)
            if not anchor.is_absolute():
                raise OSError
            resolved_anchor = anchor.resolve(strict=True)
            if anchor != resolved_anchor or any(
                parent.is_symlink() for parent in (resolved_anchor, *resolved_anchor.parents)
            ):
                raise OSError
            try:
                resolved_anchor.relative_to(root)
            except ValueError:
                pass
            else:
                raise OSError
            before = resolved_anchor.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > 16 * 1024:
                raise OSError
            descriptor = os.open(
                resolved_anchor,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            public_key_body = os.read(descriptor, 16 * 1024 + 1)
            after = os.fstat(descriptor)
            final = resolved_anchor.lstat()

            def identity(details: os.stat_result) -> tuple[int, ...]:
                return (
                    details.st_dev,
                    details.st_ino,
                    details.st_size,
                    details.st_mtime_ns,
                    details.st_ctime_ns,
                    details.st_mode,
                    details.st_uid,
                )

            if (
                len(public_key_body) > 16 * 1024
                or identity(before) != identity(opened)
                or identity(opened) != identity(after)
                or identity(opened) != identity(final)
            ):
                raise OSError
            public_key = serialization.load_pem_public_key(public_key_body)
            if not isinstance(public_key, Ed25519PublicKey):
                raise ValueError
            public_key_spki = public_key.public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if (
                _SHA256.fullmatch(expected_public_key_spki_sha256) is None
                or expected_public_key_spki_sha256 == "0" * 64
                or hashlib.sha256(public_key_spki).hexdigest() != expected_public_key_spki_sha256
            ):
                raise ValueError
            encoded_signature = signature.decode("ascii").strip()
            signature_bytes = base64.b64decode(encoded_signature, validate=True)
            if len(signature_bytes) != 64 or signature not in {
                encoded_signature.encode("ascii"),
                encoded_signature.encode("ascii") + b"\n",
                encoded_signature.encode("ascii") + b"\r\n",
            }:
                raise ValueError
            public_key.verify(signature_bytes, body)
        except (InvalidSignature, OSError, TypeError, UnicodeError, ValueError) as exc:
            raise ValueError("OpenMAIC shared-ingress observer signature is invalid") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
    try:
        report = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC shared-ingress observer attestation is invalid") from exc
    if not isinstance(report, dict):
        raise ValueError("OpenMAIC shared-ingress observer attestation schema is invalid")
    schema_version = report.get("schemaVersion")
    expected_report_fields = {
        "schemaVersion",
        "producer",
        "releaseRun",
        "observedAt",
        "observer",
    }
    if schema_version == OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION:
        expected_report_fields.update(("candidateBinding", "attestationChallenge"))
    if set(report) != expected_report_fields:
        raise ValueError("OpenMAIC shared-ingress observer attestation schema is invalid")
    if canonical_openmaic_shared_ingress_observer_attestation(report) != body:
        raise ValueError("OpenMAIC shared-ingress observer attestation is invalid")
    expected_run = dict(release_run)
    _validate_release_run(expected_run)
    observer = report.get("observer")
    legacy_fields = {
        "observerId",
        "observerUrl",
        "sharedIngressControlUrl",
    }
    proxy_fields = {
        *legacy_fields,
        "candidateHealthUrl",
        "sharedIngressUpstreamUrl",
        "internalProxyUrl",
        "authExpiresAt",
    }
    expected_fields = (
        legacy_fields
        if schema_version == OPENMAIC_SMOKE_SCHEMA_VERSION
        else proxy_fields
        if schema_version
        in {
            OPENMAIC_SHARED_INGRESS_OBSERVER_SCHEMA_VERSION,
            OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION,
        }
        else None
    )
    if (
        not isinstance(observer, dict)
        or expected_fields is None
        or set(observer) != expected_fields
    ):
        raise ValueError("OpenMAIC shared-ingress observer identity is invalid")
    observer_url = observer.get("observerUrl")
    control_url = observer.get("sharedIngressControlUrl")
    if (
        report.get("producer") != OPENMAIC_SHARED_INGRESS_OBSERVER_PRODUCER
        or not _exact_json_equal(report.get("releaseRun"), expected_run)
        or not _valid_observed_at(report.get("observedAt"))
        or not _valid_public_id(observer.get("observerId"))
        or not _valid_base_url(observer_url)
        or not _valid_absolute_url(control_url)
        or _url_origin(observer_url) == _url_origin(control_url)
        or _contains_sensitive_field(report)
    ):
        raise ValueError("OpenMAIC shared-ingress observer binding is invalid")
    if schema_version in {
        OPENMAIC_SHARED_INGRESS_OBSERVER_SCHEMA_VERSION,
        OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION,
    }:
        candidate_health_url = observer.get("candidateHealthUrl")
        upstream_url = observer.get("sharedIngressUpstreamUrl")
        internal_proxy_url = observer.get("internalProxyUrl")
        expires_at = observer.get("authExpiresAt")
        observed_at = report.get("observedAt")
        try:
            normalized_internal_proxy_url = _canonical_internal_proxy_url(internal_proxy_url)
        except ValueError as exc:
            raise ValueError("OpenMAIC shared-ingress observer binding is invalid") from exc
        if (
            not isinstance(control_url, str)
            or urlsplit(control_url).path != "/v1/control-canaries"
            or bool(urlsplit(control_url).query or urlsplit(control_url).fragment)
            or not _valid_absolute_url(candidate_health_url)
            or not isinstance(candidate_health_url, str)
            or urlsplit(candidate_health_url).path
            != "/api/v1/system/teaching-health/observer-canary"
            or not _valid_base_url(upstream_url)
            or not isinstance(upstream_url, str)
            or internal_proxy_url != normalized_internal_proxy_url
            or not _valid_observed_at(expires_at)
            or not isinstance(expires_at, str)
            or not isinstance(observed_at, str)
            or datetime.fromisoformat(expires_at.removesuffix("Z") + "+00:00")
            <= datetime.fromisoformat(observed_at.removesuffix("Z") + "+00:00")
            or (
                datetime.fromisoformat(expires_at.removesuffix("Z") + "+00:00")
                - datetime.fromisoformat(observed_at.removesuffix("Z") + "+00:00")
            ).total_seconds()
            > 7_200
            or len(
                {
                    _canonical_origin(observer_url),
                    _canonical_origin(control_url),
                    _canonical_origin(candidate_health_url),
                    _canonical_origin(upstream_url),
                    _canonical_origin(normalized_internal_proxy_url),
                }
            )
            != 5
            or len(
                {
                    _canonical_origin(observer_url)[1:],
                    _canonical_origin(control_url)[1:],
                    _canonical_origin(candidate_health_url)[1:],
                    _canonical_origin(upstream_url)[1:],
                    _canonical_origin(normalized_internal_proxy_url)[1:],
                }
            )
            != 5
        ):
            raise ValueError("OpenMAIC shared-ingress observer binding is invalid")
    if schema_version == OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION:
        candidate_binding = report.get("candidateBinding")
        challenge = report.get("attestationChallenge")
        if (
            not isinstance(candidate_binding, dict)
            or set(candidate_binding) != {"candidateSha256", "imageLockSha256"}
            or not _valid_sha256(candidate_binding.get("candidateSha256"))
            or not _valid_sha256(candidate_binding.get("imageLockSha256"))
            or not _valid_sha256(challenge)
        ):
            raise ValueError("OpenMAIC shared-ingress observer candidate binding is invalid")
    return report


def validate_openmaic_shared_ingress_observer_bound_context(
    report: Mapping[str, object],
    *,
    expected_candidate_binding: Mapping[str, str],
    expected_public_key_spki_sha256: str,
    trusted_now: datetime | None = None,
) -> dict[str, object]:
    """Bind one signed observer report to the current candidate and trust clock."""

    expected_binding = dict(expected_candidate_binding)
    try:
        expected_challenge = openmaic_observer_attestation_challenge(
            expected_public_key_spki_sha256
        )
    except ValueError as exc:
        raise ValueError("OpenMAIC shared-ingress observer candidate binding is invalid") from exc
    if (
        report.get("schemaVersion") != OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION
        or set(expected_binding) != {"candidateSha256", "imageLockSha256"}
        or not all(_valid_sha256(value) for value in expected_binding.values())
        or not _exact_json_equal(report.get("candidateBinding"), expected_binding)
        or report.get("attestationChallenge") != expected_challenge
    ):
        raise ValueError("OpenMAIC shared-ingress observer candidate binding is invalid")
    if trusted_now is not None:
        observer = report.get("observer")
        observed_at = report.get("observedAt")
        expires_at = observer.get("authExpiresAt") if isinstance(observer, dict) else None
        if (
            trusted_now.tzinfo is None
            or trusted_now.utcoffset() != timezone.utc.utcoffset(trusted_now)
            or not isinstance(observed_at, str)
            or not isinstance(expires_at, str)
        ):
            raise ValueError("OpenMAIC shared-ingress observer auth window is invalid")
        observed = datetime.fromisoformat(observed_at.removesuffix("Z") + "+00:00")
        expires = datetime.fromisoformat(expires_at.removesuffix("Z") + "+00:00")
        current = trusted_now.astimezone(timezone.utc)
        if current < observed or current >= expires:
            raise ValueError("OpenMAIC shared-ingress observer auth window is invalid")
    return dict(report)


def verify_openmaic_shared_ingress_observer_attestation(
    body: bytes,
    *,
    release_run: Mapping[str, str],
    signature: bytes,
    public_key_path: Path,
    candidate_root: Path,
    expected_public_key_spki_sha256: str,
    trusted_now: datetime,
) -> dict[str, object]:
    """Verify the detached Ed25519 signature before accepting live evidence."""

    report = parse_openmaic_shared_ingress_observer_attestation(
        body,
        release_run=release_run,
        signature=signature,
        public_key_path=public_key_path,
        candidate_root=candidate_root,
        expected_public_key_spki_sha256=expected_public_key_spki_sha256,
    )
    return validate_openmaic_shared_ingress_observer_bound_context(
        report,
        expected_candidate_binding=current_openmaic_observer_candidate_binding(candidate_root),
        expected_public_key_spki_sha256=expected_public_key_spki_sha256,
        trusted_now=trusted_now,
    )


def verify_archived_openmaic_shared_ingress_observer_attestation(
    body: bytes,
    *,
    release_run: Mapping[str, str],
    signature: bytes,
    public_key_path: Path,
    candidate_root: Path,
    expected_public_key_spki_sha256: str,
) -> dict[str, object]:
    """Replay a signed observer report at its signed observation time."""

    report = parse_openmaic_shared_ingress_observer_attestation(
        body,
        release_run=release_run,
        signature=signature,
        public_key_path=public_key_path,
        candidate_root=candidate_root,
        expected_public_key_spki_sha256=expected_public_key_spki_sha256,
    )
    observed_at = report.get("observedAt")
    if not isinstance(observed_at, str):
        raise ValueError("OpenMAIC shared-ingress observer auth window is invalid")
    try:
        signed_observation_time = datetime.fromisoformat(observed_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("OpenMAIC shared-ingress observer auth window is invalid") from exc
    return validate_openmaic_shared_ingress_observer_bound_context(
        report,
        expected_candidate_binding=current_openmaic_observer_candidate_binding(candidate_root),
        expected_public_key_spki_sha256=expected_public_key_spki_sha256,
        trusted_now=signed_observation_time,
    )


def _validate_candidate(raw: object) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "sourceRepository",
        "sourceHead",
        "releaseTag",
        "openmaicHead",
        "imageDigests",
    }:
        raise ValueError("OpenMAIC smoke candidate schema is invalid")
    source_head = raw.get("sourceHead")
    release_tag = raw.get("releaseTag")
    release_match = _RELEASE_TAG.fullmatch(release_tag) if isinstance(release_tag, str) else None
    if (
        raw.get("sourceRepository") != _SOURCE_REPOSITORY
        or not isinstance(source_head, str)
        or _COMMIT.fullmatch(source_head) is None
        or source_head == "0" * 40
        or release_match is None
        or release_match.group(1) != source_head[:8]
        or raw.get("openmaicHead") != _OPENMAIC_HEAD
    ):
        raise ValueError("OpenMAIC smoke candidate identity is invalid")
    image_digests = raw.get("imageDigests")
    if not isinstance(image_digests, dict) or set(image_digests) != _CUSTOM_IMAGE_NAMES:
        raise ValueError("OpenMAIC smoke candidate image digests are invalid")
    for digest in image_digests.values():
        match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if match is None or match.group(1) == "0" * 64:
            raise ValueError("OpenMAIC smoke candidate image digests are invalid")


def _validate_release_run(raw: object) -> None:
    if not isinstance(raw, dict) or set(raw) != {"runId", "environmentId"}:
        raise ValueError("OpenMAIC smoke release run schema is invalid")
    if any(not _valid_public_id(raw.get(field)) for field in ("runId", "environmentId")):
        raise ValueError("OpenMAIC smoke release run binding is invalid")


def _validate_runtime_attestation(raw: object, *, expected_sha256: str) -> None:
    if not isinstance(raw, dict) or set(raw) != {"artifact", "sha256"}:
        raise ValueError("OpenMAIC smoke runtime attestation schema is invalid")
    if (
        raw.get("artifact") != _RUNTIME_ATTESTATION_ARTIFACT
        or not _valid_sha256(raw.get("sha256"))
        or raw.get("sha256") != expected_sha256
    ):
        raise ValueError("OpenMAIC smoke runtime attestation binding is invalid")


def _validate_fixture(raw: object) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "tenantId",
        "teacherUserId",
        "courseId",
        "classId",
    }:
        raise ValueError("OpenMAIC smoke fixture schema is invalid")
    if any(
        not _valid_public_id(raw.get(field))
        for field in ("tenantId", "teacherUserId", "courseId", "classId")
    ):
        raise ValueError("OpenMAIC smoke fixture binding is invalid")


def _validate_binding(
    raw: object,
    *,
    expected_plane: str,
    fixture: object,
) -> None:
    if expected_plane == "shared":
        if not isinstance(raw, dict) or not _exact_json_equal(raw, _SHARED_BINDING):
            raise ValueError("OpenMAIC smoke shared-plane binding is invalid")
        return

    if expected_plane != "dedicated":
        raise ValueError("OpenMAIC smoke expected plane is invalid")
    if (
        not isinstance(raw, dict)
        or set(raw) != _DEDICATED_BINDING_FIELDS
        or not isinstance(fixture, dict)
    ):
        raise ValueError("OpenMAIC smoke dedicated-plane binding is invalid")
    tenant_id = fixture.get("tenantId")
    attempt_count = raw.get("attemptCount")
    shared_attempt_count = raw.get("sharedRouteAttemptCount")
    dedicated_attempt_count = raw.get("dedicatedRouteAttemptCount")
    selected_attempt_count = raw.get("selectedRouteAttemptCount")
    unavailable_attempt_count = raw.get("unavailableRouteAttemptCount")
    if (
        not _valid_public_id(tenant_id)
        or any(
            not _valid_public_id(raw.get(field))
            for field in (
                "routeId",
                "routeTenantId",
                "routeOwnerKey",
                "providerProfileId",
                "providerTenantId",
                "providerOwnerKey",
                "workerPoolRef",
                "queueRef",
            )
        )
        or raw.get("routeTenantId") != tenant_id
        or raw.get("routeOwnerKey") != tenant_id
        or raw.get("providerScope") != "dedicated"
        or raw.get("providerTenantId") != tenant_id
        or raw.get("providerOwnerKey") != tenant_id
        or any(raw.get(field) == shared_value for field, shared_value in _SHARED_BINDING.items())
        or any(
            type(value) is not int
            for value in (
                attempt_count,
                shared_attempt_count,
                dedicated_attempt_count,
                selected_attempt_count,
                unavailable_attempt_count,
            )
        )
        or attempt_count <= 0
        or shared_attempt_count != 0
        or dedicated_attempt_count != attempt_count
        or selected_attempt_count <= 0
        or unavailable_attempt_count < 0
        or selected_attempt_count + unavailable_attempt_count != attempt_count
        or raw.get("routeAttemptHistoryComplete") is not True
    ):
        raise ValueError("OpenMAIC smoke dedicated-plane binding is invalid")


def _validate_generation(raw: object) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "jobId",
        "jobStatus",
        "assetId",
        "classroomStatus",
        "classroomVersionId",
        "documentSha256",
        "documentSizeBytes",
        "documentEtag",
    }:
        raise ValueError("OpenMAIC smoke generation schema is invalid")
    document_sha256 = raw.get("documentSha256")
    if (
        raw.get("jobStatus") != "succeeded"
        or raw.get("classroomStatus") != "succeeded"
        or any(
            not _valid_public_id(raw.get(field))
            for field in ("jobId", "assetId", "classroomVersionId")
        )
        or not _valid_sha256(document_sha256)
        or raw.get("documentEtag") != f'"sha256-{document_sha256}"'
        or type(raw.get("documentSizeBytes")) is not int
        or not 0 < raw["documentSizeBytes"] <= (2**63 - 1)
    ):
        raise ValueError("OpenMAIC smoke generation was not materialized successfully")


def _validate_route_attempt_counts(
    raw: Mapping[str, object],
    *,
    require_selected: bool | None,
) -> None:
    attempt_count = raw.get("attemptCount")
    shared_count = raw.get("sharedRouteAttemptCount")
    dedicated_count = raw.get("dedicatedRouteAttemptCount")
    selected_count = raw.get("selectedRouteAttemptCount")
    unavailable_count = raw.get("unavailableRouteAttemptCount")
    if (
        any(
            type(value) is not int
            for value in (
                attempt_count,
                shared_count,
                dedicated_count,
                selected_count,
                unavailable_count,
            )
        )
        or attempt_count <= 0
        or shared_count != 0
        or dedicated_count != attempt_count
        or selected_count < 0
        or unavailable_count < 0
        or selected_count + unavailable_count != attempt_count
        or (require_selected is True and selected_count <= 0)
        or (require_selected is False and selected_count != 0)
        or raw.get("routeAttemptHistoryComplete") is not True
    ):
        raise ValueError("OpenMAIC dedicated route-attempt history is invalid")


def _validate_dedicated_outage_facts(raw: Mapping[str, object]) -> None:
    fixture = raw.get("fixture")
    outage = raw.get("outage")
    shared_ingress = raw.get("sharedIngress")
    restoration = raw.get("restoration")
    if not isinstance(fixture, dict) or set(fixture) != {
        "tenantId",
        "attemptMarker",
        "cleanupBoundary",
    }:
        raise ValueError("OpenMAIC dedicated outage fixture is invalid")
    if not _valid_public_id(fixture.get("tenantId")):
        raise ValueError("OpenMAIC dedicated outage fixture is invalid")
    marker = fixture.get("attemptMarker")
    boundary = fixture.get("cleanupBoundary")
    if (
        not isinstance(marker, dict)
        or set(marker) != {"artifact", "sha256"}
        or marker.get("artifact") != "runtime/openmaic-dedicated-outage-attempt.json"
        or not _valid_sha256(marker.get("sha256"))
        or not isinstance(boundary, dict)
        or set(boundary) != {"reason", "reversibleResourcesDeleted", "retainedAuditResources"}
        or boundary.get("reason") != "formal-delete-api-unavailable"
        or boundary.get("reversibleResourcesDeleted")
        != ["classEnrollment", "tenantMembership", "teacherIdentity"]
    ):
        raise ValueError("OpenMAIC dedicated outage fixture is invalid")
    retained = boundary.get("retainedAuditResources")
    expected_types = [
        "course",
        "class",
        "generationQuotaGrant",
        "classroomAsset",
        "generationJob",
        "classroomAsset",
        "generationJob",
    ]
    if (
        not isinstance(retained, list)
        or len(retained) != len(expected_types)
        or any(
            not isinstance(item, dict)
            or set(item) != {"resourceType", "resourceId"}
            or item.get("resourceType") != resource_type
            or not _valid_public_id(item.get("resourceId"))
            for item, resource_type in zip(retained, expected_types, strict=True)
        )
    ):
        raise ValueError("OpenMAIC dedicated outage fixture is invalid")
    if not isinstance(outage, dict) or set(outage) != {
        "dedicatedPlaneStopped",
        "routeId",
        "jobId",
        "jobStatus",
        "errorCode",
        "attemptCount",
        "sharedRouteAttemptCount",
        "dedicatedRouteAttemptCount",
        "selectedRouteAttemptCount",
        "unavailableRouteAttemptCount",
        "routeAttemptHistoryComplete",
    }:
        raise ValueError("OpenMAIC dedicated outage failure facts are invalid")
    if (
        outage.get("dedicatedPlaneStopped") is not True
        or not _valid_public_id(outage.get("routeId"))
        or outage.get("routeId") == _SHARED_BINDING["routeId"]
        or not _valid_public_id(outage.get("jobId"))
        or outage.get("jobStatus") != "failed"
        or outage.get("errorCode") != "dedicated_data_plane_unavailable"
    ):
        raise ValueError("OpenMAIC dedicated outage failure facts are invalid")
    _validate_route_attempt_counts(outage, require_selected=None)
    if not isinstance(shared_ingress, dict) or set(shared_ingress) != {
        "observationId",
        "requestCountBefore",
        "requestCountAfter",
    }:
        raise ValueError("OpenMAIC dedicated outage shared-ingress facts are invalid")
    request_count_before = shared_ingress.get("requestCountBefore")
    request_count_after = shared_ingress.get("requestCountAfter")
    if (
        not _valid_public_id(shared_ingress.get("observationId"))
        or type(request_count_before) is not int
        or type(request_count_after) is not int
        or request_count_before < 0
        or request_count_after != request_count_before
    ):
        raise ValueError("OpenMAIC dedicated outage shared-ingress facts are invalid")
    if not isinstance(restoration, dict) or set(restoration) != {
        "dedicatedPlaneRestored",
        "routeId",
        "canaryJobId",
        "canaryJobStatus",
        "attemptCount",
        "sharedRouteAttemptCount",
        "dedicatedRouteAttemptCount",
        "selectedRouteAttemptCount",
        "unavailableRouteAttemptCount",
        "routeAttemptHistoryComplete",
    }:
        raise ValueError("OpenMAIC dedicated outage restoration facts are invalid")
    if (
        restoration.get("dedicatedPlaneRestored") is not True
        or restoration.get("routeId") != outage.get("routeId")
        or not _valid_public_id(restoration.get("canaryJobId"))
        or restoration.get("canaryJobStatus") != "succeeded"
    ):
        raise ValueError("OpenMAIC dedicated outage restoration facts are invalid")
    _validate_route_attempt_counts(restoration, require_selected=True)


def _validate_observer_trust_anchor(
    raw: object,
    *,
    expected_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
    expected_control_origin: str,
    expected_public_key_spki_sha256: str | None = None,
) -> None:
    expected_fields = {
        "sha256",
        "observerId",
        "observerOrigin",
        "sharedIngressControlOrigin",
    }
    if expected_public_key_spki_sha256 is not None:
        expected_fields.update(("signatureArtifact", "publicKeySpkiSha256"))
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ValueError("OpenMAIC shared-ingress observer trust anchor is invalid")
    try:
        observer_identity = _canonical_origin(expected_observer_origin)
        control_identity = _canonical_origin(expected_control_origin)
    except ValueError as exc:
        raise ValueError("OpenMAIC shared-ingress observer trust anchor is invalid") from exc
    if (
        not _valid_sha256(expected_sha256)
        or raw.get("sha256") != expected_sha256
        or not _valid_public_id(expected_observer_id)
        or raw.get("observerId") != expected_observer_id
        or not _valid_base_url(expected_observer_origin)
        or raw.get("observerOrigin") != expected_observer_origin
        or _url_origin(expected_observer_origin) != expected_observer_origin
        or not _valid_base_url(expected_control_origin)
        or raw.get("sharedIngressControlOrigin") != expected_control_origin
        or _url_origin(expected_control_origin) != expected_control_origin
        or observer_identity == control_identity
        or (
            expected_public_key_spki_sha256 is not None
            and (
                not _valid_sha256(expected_public_key_spki_sha256)
                or raw.get("signatureArtifact") != _SHARED_INGRESS_OBSERVER_SIGNATURE_ARTIFACT
                or raw.get("publicKeySpkiSha256") != expected_public_key_spki_sha256
            )
        )
    ):
        raise ValueError("OpenMAIC shared-ingress observer trust anchor is invalid")


def validate_openmaic_shared_ingress_observer_trust_anchor(
    *,
    expected_observer_attestation_sha256: object,
    expected_observer_id: object,
    expected_observer_origin: object,
    expected_shared_ingress_control_origin: object,
    expected_public_key_spki_sha256: object | None = None,
) -> dict[str, str]:
    """Validate caller-supplied observer identity without consulting an evidence bundle."""

    if not all(
        isinstance(value, str)
        for value in (
            expected_observer_attestation_sha256,
            expected_observer_id,
            expected_observer_origin,
            expected_shared_ingress_control_origin,
        )
    ):
        raise ValueError("OpenMAIC external observer trust anchor is unavailable or invalid")
    anchor = {
        "sha256": expected_observer_attestation_sha256,
        "observerId": expected_observer_id,
        "observerOrigin": expected_observer_origin,
        "sharedIngressControlOrigin": expected_shared_ingress_control_origin,
    }
    if expected_public_key_spki_sha256 is not None:
        if not isinstance(expected_public_key_spki_sha256, str):
            raise ValueError("OpenMAIC external observer trust anchor is unavailable or invalid")
        anchor.update(
            {
                "signatureArtifact": _SHARED_INGRESS_OBSERVER_SIGNATURE_ARTIFACT,
                "publicKeySpkiSha256": expected_public_key_spki_sha256,
            }
        )
    try:
        _validate_observer_trust_anchor(
            anchor,
            expected_sha256=expected_observer_attestation_sha256,
            expected_observer_id=expected_observer_id,
            expected_observer_origin=expected_observer_origin,
            expected_control_origin=expected_shared_ingress_control_origin,
            expected_public_key_spki_sha256=(expected_public_key_spki_sha256),
        )
    except ValueError as exc:
        raise ValueError(
            "OpenMAIC external observer trust anchor is unavailable or invalid"
        ) from exc
    return dict(anchor)


def parse_openmaic_dedicated_outage_attempt_marker(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_observer_attestation_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
    expected_shared_ingress_control_origin: str,
    expected_tenant_id: str,
    expected_route_id: str,
    expected_observer_public_key_spki_sha256: str | None = None,
) -> dict[str, object]:
    """Parse the durable pre-mutation marker against external trusted bindings."""

    if not isinstance(body, bytes) or not body or len(body) > MAX_OPENMAIC_SMOKE_REPORT_BYTES:
        raise ValueError("OpenMAIC dedicated outage attempt marker size is invalid")
    try:
        marker = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC dedicated outage attempt marker is invalid") from exc
    if not isinstance(marker, dict) or set(marker) != {
        "schemaVersion",
        "producer",
        "candidate",
        "releaseRun",
        "observerTrustAnchor",
        "fixturePlan",
    }:
        raise ValueError("OpenMAIC dedicated outage attempt marker is invalid")
    if canonical_openmaic_dedicated_outage_attempt_marker(
        marker
    ) != body or _contains_sensitive_field(marker):
        raise ValueError("OpenMAIC dedicated outage attempt marker is invalid")
    expected_candidate = dict(candidate)
    expected_run = dict(release_run)
    _validate_candidate(expected_candidate)
    _validate_release_run(expected_run)
    if (
        marker.get("schemaVersion") != OPENMAIC_SMOKE_SCHEMA_VERSION
        or marker.get("producer") != "openmaic-dedicated-outage-attempt"
        or not _exact_json_equal(marker.get("candidate"), expected_candidate)
        or not _exact_json_equal(marker.get("releaseRun"), expected_run)
    ):
        raise ValueError("OpenMAIC dedicated outage attempt marker binding is invalid")
    _validate_observer_trust_anchor(
        marker.get("observerTrustAnchor"),
        expected_sha256=expected_observer_attestation_sha256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_control_origin=expected_shared_ingress_control_origin,
        expected_public_key_spki_sha256=(expected_observer_public_key_spki_sha256),
    )
    plan = marker.get("fixturePlan")
    if (
        not isinstance(plan, dict)
        or set(plan)
        != {
            "tenantId",
            "routeId",
            "cleanupBoundary",
            "retainedResourceTypes",
        }
        or not _valid_public_id(expected_tenant_id)
        or plan.get("tenantId") != expected_tenant_id
        or not _valid_public_id(expected_route_id)
        or expected_route_id == _SHARED_BINDING["routeId"]
        or plan.get("routeId") != expected_route_id
        or plan.get("cleanupBoundary") != "identity-membership-enrollment-only"
        or plan.get("retainedResourceTypes")
        != [
            "course",
            "class",
            "generationQuotaGrant",
            "classroomAsset",
            "generationJob",
        ]
    ):
        raise ValueError("OpenMAIC dedicated outage attempt marker fixture is invalid")
    return marker


def _validate_docker_boundary(
    raw: object,
    *,
    expected_host_identity_sha256: str,
) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "dockerHostIdentitySha256",
        "daemonIdentityBeforeSha256",
        "daemonIdentityAfterSha256",
        "inventoryBeforeSha256",
        "inventoryAfterSha256",
    }:
        raise ValueError("OpenMAIC dedicated outage Docker boundary is invalid")
    if (
        not _valid_sha256(expected_host_identity_sha256)
        or raw.get("dockerHostIdentitySha256") != expected_host_identity_sha256
        or any(not _valid_sha256(raw.get(name)) for name in set(raw) - {"dockerHostIdentitySha256"})
        or raw.get("daemonIdentityBeforeSha256") != raw.get("daemonIdentityAfterSha256")
        or raw.get("inventoryBeforeSha256") != raw.get("inventoryAfterSha256")
    ):
        raise ValueError("OpenMAIC dedicated outage Docker boundary is invalid")


def _validate_observer_attestation_reference(
    raw: object,
    *,
    expected_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
    expected_control_origin: str,
    candidate_origin: str,
    expected_public_key_spki_sha256: str | None = None,
) -> None:
    expected_fields = {
        "artifact",
        "sha256",
        "observerId",
        "observerOrigin",
        "sharedIngressControlOrigin",
    }
    if expected_public_key_spki_sha256 is not None:
        expected_fields.update(("signatureArtifact", "publicKeySpkiSha256"))
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ValueError("OpenMAIC shared-ingress observer reference is invalid")
    try:
        observer_identity = _canonical_origin(expected_observer_origin)
        control_identity = _canonical_origin(expected_control_origin)
        candidate_identity = _canonical_origin(candidate_origin)
    except ValueError as exc:
        raise ValueError("OpenMAIC shared-ingress observer reference is invalid") from exc
    if (
        raw.get("artifact") != _SHARED_INGRESS_OBSERVER_ATTESTATION_ARTIFACT
        or not _valid_sha256(raw.get("sha256"))
        or raw.get("sha256") != expected_sha256
        or not _valid_public_id(raw.get("observerId"))
        or raw.get("observerId") != expected_observer_id
        or not _valid_base_url(raw.get("observerOrigin"))
        or raw.get("observerOrigin") != expected_observer_origin
        or _url_origin(expected_observer_origin) != expected_observer_origin
        or not _valid_base_url(raw.get("sharedIngressControlOrigin"))
        or raw.get("sharedIngressControlOrigin") != expected_control_origin
        or _url_origin(expected_control_origin) != expected_control_origin
        or observer_identity in {candidate_identity, control_identity}
        or (
            expected_public_key_spki_sha256 is not None
            and (
                not _valid_sha256(expected_public_key_spki_sha256)
                or raw.get("signatureArtifact") != _SHARED_INGRESS_OBSERVER_SIGNATURE_ARTIFACT
                or raw.get("publicKeySpkiSha256") != expected_public_key_spki_sha256
            )
        )
    ):
        raise ValueError("OpenMAIC shared-ingress observer reference is invalid")


def parse_openmaic_dedicated_outage_attestation(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
    expected_runtime_attestation_sha256: str,
    expected_observer_attestation_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
    expected_shared_ingress_control_origin: str,
    expected_tenant_id: str,
    attempt_marker_body: bytes,
    expected_docker_host_identity_sha256: str,
    expected_observer_public_key_spki_sha256: str | None = None,
) -> dict[str, object]:
    """Parse a candidate-bound outage/zero-shared-ingress/restore attestation."""

    if not isinstance(body, bytes) or not body or len(body) > MAX_OPENMAIC_SMOKE_REPORT_BYTES:
        raise ValueError("OpenMAIC dedicated outage attestation size is invalid")
    try:
        report = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC dedicated outage attestation is invalid") from exc
    if not isinstance(report, dict) or set(report) != {
        "schemaVersion",
        "producer",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "runtimeAttestation",
        "observerAttestation",
        "fixture",
        "provenance",
        "execution",
        "outage",
        "sharedIngress",
        "restoration",
    }:
        raise ValueError("OpenMAIC dedicated outage attestation schema is invalid")
    try:
        canonical = canonical_openmaic_dedicated_outage_attestation(report)
    except (TypeError, ValueError) as exc:
        raise ValueError("OpenMAIC dedicated outage attestation is invalid") from exc
    if canonical != body or _contains_sensitive_field(report):
        raise ValueError("OpenMAIC dedicated outage attestation is invalid")
    expected_candidate = dict(candidate)
    expected_run = dict(release_run)
    _validate_candidate(expected_candidate)
    _validate_release_run(expected_run)
    if (
        type(report.get("schemaVersion")) is not int
        or report.get("schemaVersion") != OPENMAIC_SMOKE_SCHEMA_VERSION
        or report.get("producer") != OPENMAIC_DEDICATED_OUTAGE_PRODUCER
        or not _exact_json_equal(report.get("candidate"), expected_candidate)
        or not _exact_json_equal(report.get("releaseRun"), expected_run)
        or not _valid_observed_at(report.get("observedAt"))
        or not _valid_base_url(expected_base_url)
        or report.get("baseUrl") != expected_base_url
    ):
        raise ValueError("OpenMAIC dedicated outage attestation binding is invalid")
    _validate_runtime_attestation(
        report.get("runtimeAttestation"),
        expected_sha256=expected_runtime_attestation_sha256,
    )
    _validate_observer_attestation_reference(
        report.get("observerAttestation"),
        expected_sha256=expected_observer_attestation_sha256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_control_origin=expected_shared_ingress_control_origin,
        candidate_origin=_url_origin(expected_base_url),
        expected_public_key_spki_sha256=(expected_observer_public_key_spki_sha256),
    )
    _validate_dedicated_outage_facts(report)
    fixture = report["fixture"]
    if not _valid_public_id(expected_tenant_id) or fixture.get("tenantId") != expected_tenant_id:
        raise ValueError("OpenMAIC dedicated outage tenant binding is invalid")
    outage = report["outage"]
    assert isinstance(outage, dict)
    marker_reference = fixture.get("attemptMarker")
    provenance = report.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "attemptMarker",
        "observerTrustAnchor",
        "dockerBoundary",
    }:
        raise ValueError("OpenMAIC dedicated outage provenance is invalid")
    if (
        not _exact_json_equal(provenance.get("attemptMarker"), marker_reference)
        or not isinstance(marker_reference, dict)
        or hashlib.sha256(attempt_marker_body).hexdigest() != marker_reference.get("sha256")
    ):
        raise ValueError("OpenMAIC dedicated outage attempt marker reference is invalid")
    _validate_observer_trust_anchor(
        provenance.get("observerTrustAnchor"),
        expected_sha256=expected_observer_attestation_sha256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_control_origin=expected_shared_ingress_control_origin,
        expected_public_key_spki_sha256=(expected_observer_public_key_spki_sha256),
    )
    _validate_docker_boundary(
        provenance.get("dockerBoundary"),
        expected_host_identity_sha256=expected_docker_host_identity_sha256,
    )
    parse_openmaic_dedicated_outage_attempt_marker(
        attempt_marker_body,
        candidate=expected_candidate,
        release_run=expected_run,
        expected_observer_attestation_sha256=expected_observer_attestation_sha256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_shared_ingress_control_origin=expected_shared_ingress_control_origin,
        expected_tenant_id=expected_tenant_id,
        expected_route_id=str(outage["routeId"]),
        expected_observer_public_key_spki_sha256=(expected_observer_public_key_spki_sha256),
    )
    execution = report.get("execution")
    if (
        not isinstance(execution, dict)
        or set(execution) != {"command", "nativeExit", "stdoutSha256", "stderrSha256"}
        or not _exact_json_equal(
            execution.get("command"), openmaic_dedicated_outage_command_record()
        )
        or type(execution.get("nativeExit")) is not int
        or execution.get("nativeExit") != 0
        or not _valid_sha256(execution.get("stdoutSha256"))
        or execution.get("stderrSha256") != hashlib.sha256(b"").hexdigest()
    ):
        raise ValueError("OpenMAIC dedicated outage execution is invalid")
    inner_report = dict(report)
    inner_report.pop("execution")
    child_stdout = canonical_openmaic_dedicated_outage_attestation(inner_report)
    if hashlib.sha256(child_stdout).hexdigest() != execution.get("stdoutSha256"):
        raise ValueError("OpenMAIC dedicated outage execution is invalid")
    return report


def derive_openmaic_dedicated_outage_checks(
    report: Mapping[str, object],
) -> dict[str, bool]:
    """Derive only the independent outage no-fallback result."""

    passed = False
    try:
        if report.get("producer") != OPENMAIC_DEDICATED_OUTAGE_PRODUCER:
            raise ValueError("OpenMAIC dedicated outage identity is invalid")
        _validate_dedicated_outage_facts(report)
        passed = True
    except (KeyError, TypeError, ValueError):
        passed = False
    return {"noSharedFallback": passed}


def parse_openmaic_smoke_report(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
    expected_runtime_attestation_sha256: str,
    forbidden_secret_values: Sequence[bytes],
    expected_plane: str = "shared",
) -> dict[str, object]:
    """Parse and bind one canonical live OpenMAIC smoke report."""

    if not isinstance(body, bytes) or not body or len(body) > MAX_OPENMAIC_SMOKE_REPORT_BYTES:
        raise ValueError("OpenMAIC smoke report size is too large or invalid")
    secrets = tuple(
        secret for secret in forbidden_secret_values if isinstance(secret, bytes) and secret
    )
    if any(secret in body for secret in secrets):
        raise ValueError("OpenMAIC smoke report contains a forbidden secret value")
    try:
        report = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC smoke report is invalid") from exc
    if _contains_forbidden_secret_value(report, secrets):
        raise ValueError("OpenMAIC smoke report contains a forbidden secret value")
    if _contains_sensitive_field(report):
        raise ValueError("OpenMAIC smoke report contains a forbidden sensitive field")
    if not isinstance(report, dict) or set(report) != {
        "schemaVersion",
        "producer",
        "plane",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "runtimeAttestation",
        "fixture",
        "binding",
        "generation",
    }:
        raise ValueError("OpenMAIC smoke report schema fields are invalid")
    try:
        canonical = canonical_openmaic_smoke_report(report)
    except (TypeError, ValueError) as exc:
        raise ValueError("OpenMAIC smoke report is invalid") from exc
    if canonical != body:
        raise ValueError("OpenMAIC smoke report is not canonical")
    if expected_plane not in {"shared", "dedicated"}:
        raise ValueError("OpenMAIC smoke expected plane is invalid")
    if (
        type(report.get("schemaVersion")) is not int
        or report.get("schemaVersion") != OPENMAIC_SMOKE_SCHEMA_VERSION
        or report.get("producer") != OPENMAIC_SMOKE_PRODUCER
        or report.get("plane") != expected_plane
    ):
        raise ValueError("OpenMAIC smoke report identity is invalid")

    expected_candidate = dict(candidate)
    expected_run = dict(release_run)
    _validate_candidate(expected_candidate)
    _validate_release_run(expected_run)
    if not _exact_json_equal(report.get("candidate"), expected_candidate):
        raise ValueError("OpenMAIC smoke candidate binding is invalid")
    if not _exact_json_equal(report.get("releaseRun"), expected_run):
        raise ValueError("OpenMAIC smoke release run binding is invalid")
    if not _valid_observed_at(report.get("observedAt")):
        raise ValueError("OpenMAIC smoke timestamp is invalid")
    if not _valid_base_url(expected_base_url):
        raise ValueError("OpenMAIC smoke expected base URL is invalid")
    if not _valid_base_url(report.get("baseUrl")) or report.get("baseUrl") != expected_base_url:
        raise ValueError("OpenMAIC smoke base URL binding is invalid")
    if not _valid_sha256(expected_runtime_attestation_sha256):
        raise ValueError("OpenMAIC smoke expected runtime attestation digest is invalid")
    _validate_runtime_attestation(
        report.get("runtimeAttestation"),
        expected_sha256=expected_runtime_attestation_sha256,
    )
    _validate_fixture(report.get("fixture"))
    _validate_binding(
        report.get("binding"),
        expected_plane=expected_plane,
        fixture=report.get("fixture"),
    )
    _validate_generation(report.get("generation"))
    return report


def derive_openmaic_shared_plane_checks(report: Mapping[str, object]) -> dict[str, bool]:
    """Derive the shared-plane result from binding and materialization facts."""

    passed = False
    try:
        if report.get("producer") != OPENMAIC_SMOKE_PRODUCER or report.get("plane") != "shared":
            raise ValueError("OpenMAIC smoke report identity is invalid")
        _validate_fixture(report.get("fixture"))
        _validate_binding(
            report.get("binding"),
            expected_plane="shared",
            fixture=report.get("fixture"),
        )
        _validate_generation(report.get("generation"))
        passed = True
    except (KeyError, TypeError, ValueError):
        passed = False
    return {"sharedGenerationPassed": passed}


def derive_openmaic_dedicated_plane_checks(
    report: Mapping[str, object],
) -> dict[str, bool]:
    """Derive dedicated success and the narrower no-shared-client fact."""

    dedicated_generation_passed = False
    no_shared_client_issued = False
    try:
        if report.get("producer") != OPENMAIC_SMOKE_PRODUCER or report.get("plane") != "dedicated":
            raise ValueError("OpenMAIC smoke report identity is invalid")
        _validate_fixture(report.get("fixture"))
        _validate_binding(
            report.get("binding"),
            expected_plane="dedicated",
            fixture=report.get("fixture"),
        )
        no_shared_client_issued = True
        _validate_generation(report.get("generation"))
        dedicated_generation_passed = True
    except (KeyError, TypeError, ValueError):
        pass
    return {
        "dedicatedGenerationPassed": dedicated_generation_passed,
        "noSharedClientIssued": no_shared_client_issued,
    }


__all__ = [
    "MAX_OPENMAIC_SMOKE_REPORT_BYTES",
    "OPENMAIC_DEDICATED_OUTAGE_PRODUCER",
    "OPENMAIC_SHARED_INGRESS_OBSERVER_PRODUCER",
    "OPENMAIC_SHARED_INGRESS_OBSERVER_BOUND_SCHEMA_VERSION",
    "OPENMAIC_SHARED_INGRESS_OBSERVER_SCHEMA_VERSION",
    "OPENMAIC_SMOKE_PRODUCER",
    "OPENMAIC_SMOKE_SCHEMA_VERSION",
    "canonical_openmaic_smoke_report",
    "canonical_openmaic_dedicated_outage_attestation",
    "canonical_openmaic_dedicated_outage_attempt_marker",
    "canonical_openmaic_shared_ingress_observer_attestation",
    "current_openmaic_observer_candidate_binding",
    "derive_openmaic_dedicated_outage_checks",
    "derive_openmaic_dedicated_plane_checks",
    "derive_openmaic_shared_plane_checks",
    "openmaic_dedicated_plane_command_record",
    "openmaic_dedicated_outage_command_record",
    "openmaic_shared_plane_command_record",
    "openmaic_observer_attestation_challenge",
    "parse_openmaic_smoke_report",
    "parse_openmaic_dedicated_outage_attestation",
    "parse_openmaic_dedicated_outage_attempt_marker",
    "parse_openmaic_shared_ingress_observer_attestation",
    "validate_openmaic_shared_ingress_observer_bound_context",
    "verify_openmaic_shared_ingress_observer_attestation",
]
