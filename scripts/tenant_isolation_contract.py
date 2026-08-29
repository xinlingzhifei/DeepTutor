"""Strict contract for one candidate-bound live tenant-isolation probe."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
import re
from urllib.parse import urlsplit

TENANT_ISOLATION_SCHEMA_VERSION = 1
TENANT_ISOLATION_PRODUCER = "tenant-isolation-probe"
MAX_TENANT_ISOLATION_REPORT_BYTES = 64 * 1024

TENANT_ISOLATION_LAYERS = ("database", "objects", "exports", "events")
_CHECK_KEYS = {
    "database": "databaseIsolated",
    "objects": "objectsIsolated",
    "exports": "exportsIsolated",
    "events": "eventsIsolated",
}
_TARGET_TYPES = {
    "database": "course",
    "objects": "classroom_source_document",
    "exports": "classroom_export",
    "events": "learning_session_event",
}
_TARGET_RESOURCE_FIELDS = {
    "database": ("courseId",),
    "objects": ("bindingId", "classroomVersionId"),
    "exports": ("exportId",),
    "events": ("sessionId", "eventId", "classroomVersionId"),
}

_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "objectkey",
    "password",
    "secret",
    "ticket",
    "token",
)


def tenant_isolation_command_record() -> dict[str, object]:
    """Return the secret-free logical command recorded in release evidence."""

    return {
        "runner": "python",
        "script": "scripts/tenant_isolation_probe.py",
        "arguments": ["--profile", "first-release"],
    }


def canonical_tenant_isolation_report(report: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            report,
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
        return value.encode("utf-8") in secrets
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


def _parse_capacity_binding(
    raw: object,
    *,
    expected_report_sha256: str,
    expected_tenant_ids: tuple[str, str],
) -> None:
    if not isinstance(raw, dict) or set(raw) != {"reportSha256", "tenantIds"}:
        raise ValueError("tenant isolation capacity proof schema is invalid")
    if (
        not _valid_sha256(raw.get("reportSha256"))
        or raw.get("reportSha256") != expected_report_sha256
    ):
        raise ValueError("tenant isolation capacity proof SHA digest is invalid")
    tenant_ids = raw.get("tenantIds")
    if (
        not isinstance(tenant_ids, list)
        or len(tenant_ids) != 2
        or tuple(tenant_ids) != expected_tenant_ids
        or len(set(tenant_ids)) != 2
        or any(not _valid_public_id(tenant_id) for tenant_id in tenant_ids)
    ):
        raise ValueError("tenant isolation capacity tenant binding is invalid")


def _parse_principals(
    raw: object,
    *,
    expected_tenant_ids: tuple[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("tenant isolation principals must contain two entries")
    principals: list[dict[str, str]] = []
    for sequence, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {
            "tenantId",
            "actorId",
            "role",
            "membershipStatus",
        }:
            raise ValueError("tenant isolation principal schema is invalid")
        if item.get("role") != "user":
            raise ValueError("tenant isolation principal role must not be admin")
        if item.get("membershipStatus") != "active":
            raise ValueError("tenant isolation principal membership must be active")
        if item.get("tenantId") != expected_tenant_ids[sequence] or not _valid_public_id(
            item.get("actorId")
        ):
            raise ValueError("tenant isolation capacity principal binding is invalid")
        principals.append({key: str(value) for key, value in item.items()})
    if principals[0]["actorId"] == principals[1]["actorId"]:
        raise ValueError("tenant isolation principal actors must be distinct")
    return principals[0], principals[1]


def _parse_cross_tenant_principal(
    raw: object,
    *,
    owner: Mapping[str, str],
    foreign: Mapping[str, str],
) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "tenantId",
        "actorId",
        "role",
        "membershipStatus",
    }:
        raise ValueError("tenant isolation cross-tenant principal schema is invalid")
    if (
        raw.get("tenantId") != foreign["tenantId"]
        or raw.get("actorId") != owner["actorId"]
        or raw.get("role") != "student"
        or raw.get("membershipStatus") != "active"
    ):
        raise ValueError("tenant isolation cross-tenant principal binding is invalid")


def _parse_resource_ids(layer: str, raw: object) -> dict[str, str]:
    expected_fields = _TARGET_RESOURCE_FIELDS[layer]
    if not isinstance(raw, dict) or set(raw) != set(expected_fields):
        raise ValueError(f"tenant isolation {layer} resource schema is invalid")
    resources: dict[str, str] = {}
    for field in expected_fields:
        value = raw.get(field)
        if not _valid_public_id(value):
            if layer == "objects":
                raise ValueError("tenant isolation object binding public resource is invalid")
            raise ValueError(f"tenant isolation {layer} public resource is invalid")
        resources[field] = value
    return resources


def _operation_specs(
    layer: str,
    resources: Mapping[str, str],
) -> tuple[dict[str, object], ...]:
    if layer == "database":
        course_id = resources["courseId"]
        policy = f"/api/v1/teaching/courses/{course_id}/generation-policy"
        return (
            _spec("owner-policy-before", "owner-before", "GET", policy, course_id, "owner-state"),
            _spec(
                "foreign-list",
                "foreign-check",
                "GET",
                "/api/v1/teaching/courses",
                course_id,
                "foreign-list",
                actor="owner",
            ),
            _spec("foreign-read", "foreign-check", "GET", policy, course_id, "foreign-deny"),
            _spec(
                "foreign-write",
                "foreign-check",
                "PUT",
                policy,
                course_id,
                "foreign-deny",
                request=True,
            ),
            _spec("owner-policy-after", "owner-after", "GET", policy, course_id, "owner-state"),
        )
    if layer == "objects":
        binding_id = resources["bindingId"]
        version_id = resources["classroomVersionId"]
        document = f"/api/v1/classroom-versions/{version_id}/document"
        return (
            _spec(
                "owner-source-list-before",
                "owner-before",
                "GET",
                "/api/v1/teaching/sources",
                binding_id,
                "owner-list",
            ),
            _spec(
                "owner-document-before", "owner-before", "GET", document, version_id, "owner-state"
            ),
            _spec(
                "foreign-source-list",
                "foreign-check",
                "GET",
                "/api/v1/teaching/sources",
                binding_id,
                "foreign-list",
            ),
            _spec(
                "foreign-document-read",
                "foreign-check",
                "GET",
                document,
                version_id,
                "foreign-deny",
            ),
            _spec(
                "foreign-source-delete",
                "foreign-check",
                "DELETE",
                f"/api/v1/teaching/sources/{binding_id}",
                binding_id,
                "foreign-deny",
            ),
            _spec(
                "owner-source-list-after",
                "owner-after",
                "GET",
                "/api/v1/teaching/sources",
                binding_id,
                "owner-list",
            ),
            _spec(
                "owner-document-after", "owner-after", "GET", document, version_id, "owner-state"
            ),
        )
    if layer == "exports":
        export_id = resources["exportId"]
        status = f"/api/v1/classroom-exports/{export_id}"
        download = f"{status}/download"
        return (
            _spec("owner-status-before", "owner-before", "GET", status, export_id, "owner-state"),
            _spec(
                "owner-download-before",
                "owner-before",
                "GET",
                download,
                export_id,
                "owner-state",
            ),
            _spec(
                "foreign-status-read",
                "foreign-check",
                "GET",
                status,
                export_id,
                "foreign-deny",
            ),
            _spec(
                "foreign-download-read",
                "foreign-check",
                "GET",
                download,
                export_id,
                "foreign-deny",
            ),
            _spec("owner-status-after", "owner-after", "GET", status, export_id, "owner-state"),
            _spec(
                "owner-download-after",
                "owner-after",
                "GET",
                download,
                export_id,
                "owner-state",
            ),
        )
    session_id = resources["sessionId"]
    event_id = resources["eventId"]
    version_id = resources["classroomVersionId"]
    session = f"/api/v1/classroom-sessions/{session_id}"
    projection = f"/api/v1/teaching-reports/classrooms/{version_id}"
    return (
        _spec("owner-session-before", "owner-before", "GET", session, session_id, "owner-state"),
        _spec(
            "owner-projection-before",
            "owner-before",
            "GET",
            projection,
            version_id,
            "owner-state",
        ),
        _spec(
            "foreign-session-read",
            "foreign-check",
            "GET",
            session,
            session_id,
            "foreign-deny",
            actor="owner",
        ),
        _spec(
            "foreign-ticket-issue",
            "foreign-check",
            "POST",
            f"{session}/event-ticket",
            session_id,
            "foreign-deny",
            actor="owner",
        ),
        _spec(
            "foreign-event-ingest",
            "foreign-check",
            "POST",
            f"{session}/events",
            event_id,
            "foreign-deny",
            request=True,
            actor="owner",
        ),
        _spec("owner-session-after", "owner-after", "GET", session, session_id, "owner-state"),
        _spec(
            "owner-projection-after",
            "owner-after",
            "GET",
            projection,
            version_id,
            "owner-state",
        ),
    )


def _spec(
    name: str,
    phase: str,
    method: str,
    path: str,
    target_id: str,
    kind: str,
    *,
    request: bool = False,
    actor: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "phase": phase,
        "method": method,
        "path": path,
        "targetId": target_id,
        "kind": kind,
        "request": request,
        "actor": actor,
    }


def _parse_operation(
    raw: object,
    *,
    spec: Mapping[str, object],
    owner: Mapping[str, str],
    foreign: Mapping[str, str],
) -> dict[str, object]:
    fields = {
        "name",
        "phase",
        "method",
        "path",
        "tenantId",
        "actorId",
        "statusCode",
        "observedTargetId",
        "requestSha256",
        "stateSha256",
        "errorCode",
        "targetOmitted",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError("tenant isolation operation schema is invalid")
    if any(raw.get(field) != spec[field] for field in ("name", "phase", "method", "path")):
        raise ValueError("tenant isolation operation matrix path is invalid")
    principal = owner if str(spec["kind"]).startswith("owner") else foreign
    actor = owner if spec.get("actor") == "owner" else principal
    if (
        raw.get("tenantId") != principal["tenantId"]
        or raw.get("actorId") != actor["actorId"]
        or raw.get("observedTargetId") != spec["targetId"]
    ):
        raise ValueError("tenant isolation operation target principal is invalid")
    status_code = raw.get("statusCode")
    if type(status_code) is not int:
        raise ValueError("tenant isolation operation status must be an integer, not a boolean")
    if not 100 <= status_code <= 599:
        raise ValueError("tenant isolation operation status is invalid")

    request_sha256 = raw.get("requestSha256")
    if bool(spec["request"]):
        if not _valid_sha256(request_sha256):
            raise ValueError("tenant isolation operation request digest is invalid")
    elif request_sha256 is not None:
        raise ValueError("tenant isolation operation request digest is unexpected")

    kind = str(spec["kind"])
    state_sha256 = raw.get("stateSha256")
    if kind in {"owner-state", "owner-list", "foreign-list"}:
        if not _valid_sha256(state_sha256):
            raise ValueError("tenant isolation operation state digest is invalid")
    elif state_sha256 is not None and not _valid_sha256(state_sha256):
        raise ValueError("tenant isolation operation state digest is invalid")

    error_code = raw.get("errorCode")
    if kind == "foreign-deny":
        if error_code is not None and (
            not isinstance(error_code, str) or not error_code or len(error_code) > 64
        ):
            raise ValueError("tenant isolation operation error code is invalid")
    elif error_code is not None:
        raise ValueError("tenant isolation operation error code is unexpected")

    target_omitted = raw.get("targetOmitted")
    if kind in {"owner-list", "foreign-list"}:
        if type(target_omitted) is not bool:
            raise ValueError("tenant isolation operation list result is invalid")
    elif target_omitted is not None:
        raise ValueError("tenant isolation operation list result is unexpected")
    return raw


def _parse_observation(
    raw: object,
    *,
    layer: str,
    sequence: int,
    owner: Mapping[str, str],
    foreign: Mapping[str, str],
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {"sequence", "layer", "target", "operations"}:
        raise ValueError("tenant isolation observation schema is invalid")
    if type(raw.get("sequence")) is not int or raw.get("sequence") != sequence:
        raise ValueError("tenant isolation observation sequence is invalid")
    if raw.get("layer") != layer:
        raise ValueError("tenant isolation observation layer is invalid")
    target = raw.get("target")
    if not isinstance(target, dict) or set(target) != {
        "targetType",
        "resourceIds",
        "ownerTenantId",
        "ownerActorId",
    }:
        raise ValueError("tenant isolation target schema is invalid")
    if (
        target.get("targetType") != _TARGET_TYPES[layer]
        or target.get("ownerTenantId") != owner["tenantId"]
        or target.get("ownerActorId") != owner["actorId"]
    ):
        raise ValueError("tenant isolation target owner is invalid")
    resources = _parse_resource_ids(layer, target.get("resourceIds"))
    specs = _operation_specs(layer, resources)
    operations = raw.get("operations")
    if not isinstance(operations, list) or len(operations) != len(specs):
        raise ValueError("tenant isolation operation matrix is incomplete")
    for operation, spec in zip(operations, specs, strict=True):
        _parse_operation(operation, spec=spec, owner=owner, foreign=foreign)
    return raw


def parse_tenant_isolation_report(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
    expected_capacity_report_sha256: str,
    expected_capacity_tenant_ids: Sequence[str],
    forbidden_secret_values: Sequence[bytes],
) -> dict[str, object]:
    """Parse and bind one canonical live tenant-isolation report."""

    if not isinstance(body, bytes) or not body or len(body) > MAX_TENANT_ISOLATION_REPORT_BYTES:
        raise ValueError("tenant isolation report size is too large or invalid")
    secrets = tuple(
        secret for secret in forbidden_secret_values if isinstance(secret, bytes) and secret
    )
    try:
        report = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("tenant isolation report is invalid") from exc
    if _contains_forbidden_secret_value(report, secrets):
        raise ValueError("tenant isolation report contains a forbidden secret value")
    if _contains_sensitive_field(report):
        raise ValueError("tenant isolation report contains a forbidden sensitive field")
    if not isinstance(report, dict) or set(report) != {
        "schemaVersion",
        "producer",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "capacityProof",
        "principals",
        "crossTenantPrincipal",
        "observations",
    }:
        raise ValueError("tenant isolation report schema fields are invalid")
    try:
        canonical = canonical_tenant_isolation_report(report)
    except (TypeError, ValueError) as exc:
        raise ValueError("tenant isolation report is invalid") from exc
    if canonical != body:
        raise ValueError("tenant isolation report is not canonical")
    if (
        type(report.get("schemaVersion")) is not int
        or report.get("schemaVersion") != TENANT_ISOLATION_SCHEMA_VERSION
    ):
        raise ValueError("tenant isolation report schema is invalid")
    if report.get("producer") != TENANT_ISOLATION_PRODUCER:
        raise ValueError("tenant isolation report producer is invalid")
    if not _exact_json_equal(report.get("candidate"), dict(candidate)):
        raise ValueError("tenant isolation candidate binding is invalid")
    if not _exact_json_equal(report.get("releaseRun"), dict(release_run)):
        raise ValueError("tenant isolation run binding is invalid")
    if not _valid_observed_at(report.get("observedAt")):
        raise ValueError("tenant isolation report timestamp is invalid")
    if not _valid_base_url(report.get("baseUrl")) or report.get("baseUrl") != expected_base_url:
        raise ValueError("tenant isolation report URL binding is invalid")
    if (
        not isinstance(expected_capacity_tenant_ids, Sequence)
        or isinstance(expected_capacity_tenant_ids, (str, bytes))
        or len(expected_capacity_tenant_ids) != 2
    ):
        raise ValueError("tenant isolation expected capacity tenants are invalid")
    expected_tenants = (
        str(expected_capacity_tenant_ids[0]),
        str(expected_capacity_tenant_ids[1]),
    )
    _parse_capacity_binding(
        report.get("capacityProof"),
        expected_report_sha256=expected_capacity_report_sha256,
        expected_tenant_ids=expected_tenants,
    )
    owner, foreign = _parse_principals(
        report.get("principals"),
        expected_tenant_ids=expected_tenants,
    )
    _parse_cross_tenant_principal(
        report.get("crossTenantPrincipal"),
        owner=owner,
        foreign=foreign,
    )
    observations = report.get("observations")
    if not isinstance(observations, list) or len(observations) != len(TENANT_ISOLATION_LAYERS):
        raise ValueError("tenant isolation observation layer count is invalid")
    for sequence, layer in enumerate(TENANT_ISOLATION_LAYERS):
        _parse_observation(
            observations[sequence],
            layer=layer,
            sequence=sequence,
            owner=owner,
            foreign=foreign,
        )
    return report


def _exact_status(operation: Mapping[str, object], expected: int) -> bool:
    return type(operation.get("statusCode")) is int and operation.get("statusCode") == expected


def _denied(operation: Mapping[str, object]) -> bool:
    status_code = operation.get("statusCode")
    return (
        type(status_code) is int
        and status_code in {403, 404}
        and operation.get("errorCode") == {403: "forbidden", 404: "not_found"}[status_code]
    )


def _layer_isolated(observation: Mapping[str, object]) -> bool:
    operations = observation.get("operations")
    if not isinstance(operations, list):
        return False
    by_name = {
        operation.get("name"): operation
        for operation in operations
        if isinstance(operation, dict) and isinstance(operation.get("name"), str)
    }
    layer = observation.get("layer")
    if layer == "database":
        return (
            _exact_status(by_name.get("owner-policy-before", {}), 200)
            and _exact_status(by_name.get("foreign-list", {}), 200)
            and by_name.get("foreign-list", {}).get("targetOmitted") is True
            and _denied(by_name.get("foreign-read", {}))
            and _denied(by_name.get("foreign-write", {}))
            and _exact_status(by_name.get("owner-policy-after", {}), 200)
            and by_name.get("owner-policy-before", {}).get("stateSha256")
            == by_name.get("owner-policy-after", {}).get("stateSha256")
        )
    if layer == "objects":
        return (
            _exact_status(by_name.get("owner-source-list-before", {}), 200)
            and by_name.get("owner-source-list-before", {}).get("targetOmitted") is False
            and _exact_status(by_name.get("owner-document-before", {}), 200)
            and _exact_status(by_name.get("foreign-source-list", {}), 200)
            and by_name.get("foreign-source-list", {}).get("targetOmitted") is True
            and _denied(by_name.get("foreign-document-read", {}))
            and _denied(by_name.get("foreign-source-delete", {}))
            and _exact_status(by_name.get("owner-source-list-after", {}), 200)
            and by_name.get("owner-source-list-after", {}).get("targetOmitted") is False
            and _exact_status(by_name.get("owner-document-after", {}), 200)
            and by_name.get("owner-source-list-before", {}).get("stateSha256")
            == by_name.get("owner-source-list-after", {}).get("stateSha256")
            and by_name.get("owner-document-before", {}).get("stateSha256")
            == by_name.get("owner-document-after", {}).get("stateSha256")
        )
    if layer == "exports":
        return (
            _exact_status(by_name.get("owner-status-before", {}), 200)
            and _exact_status(by_name.get("owner-download-before", {}), 200)
            and _denied(by_name.get("foreign-status-read", {}))
            and _denied(by_name.get("foreign-download-read", {}))
            and _exact_status(by_name.get("owner-status-after", {}), 200)
            and _exact_status(by_name.get("owner-download-after", {}), 200)
            and by_name.get("owner-status-before", {}).get("stateSha256")
            == by_name.get("owner-status-after", {}).get("stateSha256")
            and by_name.get("owner-download-before", {}).get("stateSha256")
            == by_name.get("owner-download-after", {}).get("stateSha256")
        )
    if layer == "events":
        return (
            _exact_status(by_name.get("owner-session-before", {}), 200)
            and _exact_status(by_name.get("owner-projection-before", {}), 200)
            and _denied(by_name.get("foreign-session-read", {}))
            and _denied(by_name.get("foreign-ticket-issue", {}))
            and _denied(by_name.get("foreign-event-ingest", {}))
            and _exact_status(by_name.get("owner-session-after", {}), 200)
            and _exact_status(by_name.get("owner-projection-after", {}), 200)
            and by_name.get("owner-session-before", {}).get("stateSha256")
            == by_name.get("owner-session-after", {}).get("stateSha256")
            and by_name.get("owner-projection-before", {}).get("stateSha256")
            == by_name.get("owner-projection-after", {}).get("stateSha256")
        )
    return False


def derive_tenant_isolation_checks(report: Mapping[str, object]) -> dict[str, bool]:
    """Derive all isolation results from the fixed operation matrix."""

    failed = {key: False for key in _CHECK_KEYS.values()}
    capacity = report.get("capacityProof")
    tenant_ids = capacity.get("tenantIds") if isinstance(capacity, dict) else None
    observations = report.get("observations")
    if (
        not isinstance(tenant_ids, list)
        or len(tenant_ids) != 2
        or not isinstance(observations, list)
        or len(observations) != len(TENANT_ISOLATION_LAYERS)
    ):
        return failed
    try:
        owner, foreign = _parse_principals(
            report.get("principals"),
            expected_tenant_ids=(str(tenant_ids[0]), str(tenant_ids[1])),
        )
        _parse_cross_tenant_principal(
            report.get("crossTenantPrincipal"),
            owner=owner,
            foreign=foreign,
        )
        for sequence, layer in enumerate(TENANT_ISOLATION_LAYERS):
            _parse_observation(
                observations[sequence],
                layer=layer,
                sequence=sequence,
                owner=owner,
                foreign=foreign,
            )
    except (KeyError, TypeError, ValueError):
        return failed
    by_layer = {
        observation.get("layer"): observation
        for observation in observations
        if isinstance(observation, dict)
    }
    return {check: _layer_isolated(by_layer.get(layer, {})) for layer, check in _CHECK_KEYS.items()}


__all__ = [
    "MAX_TENANT_ISOLATION_REPORT_BYTES",
    "TENANT_ISOLATION_PRODUCER",
    "canonical_tenant_isolation_report",
    "derive_tenant_isolation_checks",
    "parse_tenant_isolation_report",
    "tenant_isolation_command_record",
]
