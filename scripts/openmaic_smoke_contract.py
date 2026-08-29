"""Strict contract for one candidate-bound live OpenMAIC smoke report."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
import re
from urllib.parse import urlsplit

OPENMAIC_SMOKE_SCHEMA_VERSION = 1
OPENMAIC_SMOKE_PRODUCER = "openmaic-smoke"
MAX_OPENMAIC_SMOKE_REPORT_BYTES = 64 * 1024

_SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
_OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
_CUSTOM_IMAGE_NAMES = {"deeptutor", "openmaic", "openmaic_render"}
_RUNTIME_ATTESTATION_ARTIFACT = "runtime/runtime-attestation.json"
_SHARED_BINDING = {
    "routeId": "shared-primary",
    "providerProfileId": "platform-default",
    "workerPoolRef": "shared-generation",
    "queueRef": "openmaic.shared",
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


def _validate_binding(raw: object) -> None:
    if not isinstance(raw, dict) or not _exact_json_equal(raw, _SHARED_BINDING):
        raise ValueError("OpenMAIC smoke shared-plane binding is invalid")


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


def parse_openmaic_smoke_report(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
    expected_runtime_attestation_sha256: str,
    forbidden_secret_values: Sequence[bytes],
) -> dict[str, object]:
    """Parse and bind one canonical live OpenMAIC shared-plane report."""

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
    if (
        type(report.get("schemaVersion")) is not int
        or report.get("schemaVersion") != OPENMAIC_SMOKE_SCHEMA_VERSION
        or report.get("producer") != OPENMAIC_SMOKE_PRODUCER
        or report.get("plane") != "shared"
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
    _validate_binding(report.get("binding"))
    _validate_generation(report.get("generation"))
    return report


def derive_openmaic_shared_plane_checks(report: Mapping[str, object]) -> dict[str, bool]:
    """Derive the shared-plane result from binding and materialization facts."""

    passed = False
    try:
        if report.get("producer") != OPENMAIC_SMOKE_PRODUCER or report.get("plane") != "shared":
            raise ValueError("OpenMAIC smoke report identity is invalid")
        _validate_fixture(report.get("fixture"))
        _validate_binding(report.get("binding"))
        _validate_generation(report.get("generation"))
        passed = True
    except (KeyError, TypeError, ValueError):
        passed = False
    return {"sharedGenerationPassed": passed}


__all__ = [
    "MAX_OPENMAIC_SMOKE_REPORT_BYTES",
    "OPENMAIC_SMOKE_PRODUCER",
    "OPENMAIC_SMOKE_SCHEMA_VERSION",
    "canonical_openmaic_smoke_report",
    "derive_openmaic_shared_plane_checks",
    "openmaic_shared_plane_command_record",
    "parse_openmaic_smoke_report",
]
