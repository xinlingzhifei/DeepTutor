"""Strict contract for one candidate-bound live OpenMAIC smoke report."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import hashlib
import json
import re
from urllib.parse import urlsplit

OPENMAIC_SMOKE_SCHEMA_VERSION = 1
OPENMAIC_SMOKE_PRODUCER = "openmaic-smoke"
OPENMAIC_DEDICATED_OUTAGE_PRODUCER = "openmaic-dedicated-outage"
OPENMAIC_SHARED_INGRESS_OBSERVER_PRODUCER = "openmaic-shared-ingress-observer"
MAX_OPENMAIC_SMOKE_REPORT_BYTES = 64 * 1024

_SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
_OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
_CUSTOM_IMAGE_NAMES = {"deeptutor", "openmaic", "openmaic_render"}
_RUNTIME_ATTESTATION_ARTIFACT = "runtime/runtime-attestation.json"
_SHARED_INGRESS_OBSERVER_ATTESTATION_ARTIFACT = (
    "runtime/openmaic-shared-ingress-observer-attestation.json"
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
    """Serialize candidate-independent shared-ingress observer provenance."""

    return canonical_openmaic_smoke_report(report)


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
        canonical_host = hostname.encode("idna").decode("ascii").lower()
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


def parse_openmaic_shared_ingress_observer_attestation(
    body: bytes,
    *,
    release_run: Mapping[str, str],
) -> dict[str, object]:
    """Parse canonical candidate-independent observer provenance for one run."""

    if not isinstance(body, bytes) or not body or len(body) > MAX_OPENMAIC_SMOKE_REPORT_BYTES:
        raise ValueError("OpenMAIC shared-ingress observer attestation size is invalid")
    try:
        report = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC shared-ingress observer attestation is invalid") from exc
    if not isinstance(report, dict) or set(report) != {
        "schemaVersion",
        "producer",
        "releaseRun",
        "observedAt",
        "observer",
    }:
        raise ValueError("OpenMAIC shared-ingress observer attestation schema is invalid")
    if canonical_openmaic_shared_ingress_observer_attestation(report) != body:
        raise ValueError("OpenMAIC shared-ingress observer attestation is invalid")
    expected_run = dict(release_run)
    _validate_release_run(expected_run)
    observer = report.get("observer")
    if not isinstance(observer, dict) or set(observer) != {
        "observerId",
        "observerUrl",
        "sharedIngressControlUrl",
    }:
        raise ValueError("OpenMAIC shared-ingress observer identity is invalid")
    observer_url = observer.get("observerUrl")
    control_url = observer.get("sharedIngressControlUrl")
    if (
        report.get("schemaVersion") != OPENMAIC_SMOKE_SCHEMA_VERSION
        or report.get("producer") != OPENMAIC_SHARED_INGRESS_OBSERVER_PRODUCER
        or not _exact_json_equal(report.get("releaseRun"), expected_run)
        or not _valid_observed_at(report.get("observedAt"))
        or not _valid_public_id(observer.get("observerId"))
        or not _valid_base_url(observer_url)
        or not _valid_absolute_url(control_url)
        or _url_origin(observer_url) == _url_origin(control_url)
        or _contains_sensitive_field(report)
    ):
        raise ValueError("OpenMAIC shared-ingress observer binding is invalid")
    return report


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
) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "sha256",
        "observerId",
        "observerOrigin",
        "sharedIngressControlOrigin",
    }:
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
    ):
        raise ValueError("OpenMAIC shared-ingress observer trust anchor is invalid")


def validate_openmaic_shared_ingress_observer_trust_anchor(
    *,
    expected_observer_attestation_sha256: object,
    expected_observer_id: object,
    expected_observer_origin: object,
    expected_shared_ingress_control_origin: object,
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
    try:
        _validate_observer_trust_anchor(
            anchor,
            expected_sha256=expected_observer_attestation_sha256,
            expected_observer_id=expected_observer_id,
            expected_observer_origin=expected_observer_origin,
            expected_control_origin=expected_shared_ingress_control_origin,
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
) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "artifact",
        "sha256",
        "observerId",
        "observerOrigin",
        "sharedIngressControlOrigin",
    }:
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
    "OPENMAIC_SMOKE_PRODUCER",
    "OPENMAIC_SMOKE_SCHEMA_VERSION",
    "canonical_openmaic_smoke_report",
    "canonical_openmaic_dedicated_outage_attestation",
    "canonical_openmaic_dedicated_outage_attempt_marker",
    "canonical_openmaic_shared_ingress_observer_attestation",
    "derive_openmaic_dedicated_outage_checks",
    "derive_openmaic_dedicated_plane_checks",
    "derive_openmaic_shared_plane_checks",
    "openmaic_dedicated_plane_command_record",
    "openmaic_dedicated_outage_command_record",
    "openmaic_shared_plane_command_record",
    "parse_openmaic_smoke_report",
    "parse_openmaic_dedicated_outage_attestation",
    "parse_openmaic_dedicated_outage_attempt_marker",
    "parse_openmaic_shared_ingress_observer_attestation",
]
