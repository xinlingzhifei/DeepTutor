from __future__ import annotations

from functools import cache
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://candidate.example.test"
OWNER_TENANT_ID = "t_" + "1" * 32
FOREIGN_TENANT_ID = "t_" + "2" * 32
OWNER_ACTOR_ID = "u_" + "3" * 32
FOREIGN_ACTOR_ID = "u_" + "4" * 32
CAPACITY_REPORT_SHA256 = "5" * 64
CAPACITY_TENANT_IDS = (OWNER_TENANT_ID, FOREIGN_TENANT_ID)

COURSE_ID = "course-" + "6" * 32
SOURCE_BINDING_ID = "source-" + "7" * 32
CLASSROOM_VERSION_ID = "version-" + "8" * 32
EXPORT_ID = "export-" + "9" * 40
SESSION_ID = "a" * 32
EVENT_ID = "event-" + "b" * 32

LAYERS = ("database", "objects", "exports", "events")
OPERATION_NAMES = {
    "database": (
        "owner-policy-before",
        "foreign-list",
        "foreign-read",
        "foreign-write",
        "owner-policy-after",
    ),
    "objects": (
        "owner-source-list-before",
        "owner-document-before",
        "foreign-source-list",
        "foreign-document-read",
        "foreign-source-delete",
        "owner-source-list-after",
        "owner-document-after",
    ),
    "exports": (
        "owner-status-before",
        "owner-download-before",
        "foreign-status-read",
        "foreign-download-read",
        "owner-status-after",
        "owner-download-after",
    ),
    "events": (
        "owner-session-before",
        "owner-projection-before",
        "foreign-session-read",
        "foreign-ticket-issue",
        "foreign-event-ingest",
        "owner-session-after",
        "owner-projection-after",
    ),
}


@cache
def _module():
    path = ROOT / "scripts" / "tenant_isolation_contract.py"
    spec = importlib.util.spec_from_file_location("tenant_isolation_contract_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate() -> dict[str, object]:
    return {
        "sourceRepository": "xinlingzhifei/DeepTutor",
        "sourceHead": "c" * 40,
        "releaseTag": "yfeistai-first-release-20260828-cccccccc",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": "sha256:" + "d" * 64,
            "openmaic": "sha256:" + "e" * 64,
            "openmaic_render": "sha256:" + "f" * 64,
        },
    }


def _release_run() -> dict[str, str]:
    return {
        "runId": "run-tenant-isolation",
        "environmentId": "environment-tenant-isolation",
    }


def _operation(
    name: str,
    phase: str,
    method: str,
    path: str,
    observed_target_id: str,
    *,
    owner: bool,
    actor_owner: bool | None = None,
    status_code: int,
    state_sha256: str | None = None,
    request_sha256: str | None = None,
    error_code: str | None = None,
    target_omitted: bool | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "phase": phase,
        "method": method,
        "path": path,
        "tenantId": OWNER_TENANT_ID if owner else FOREIGN_TENANT_ID,
        "actorId": OWNER_ACTOR_ID
        if (actor_owner if actor_owner is not None else owner)
        else FOREIGN_ACTOR_ID,
        "statusCode": status_code,
        "observedTargetId": observed_target_id,
        "requestSha256": request_sha256,
        "stateSha256": state_sha256,
        "errorCode": error_code,
        "targetOmitted": target_omitted,
    }


def _database_observation() -> dict[str, object]:
    policy_path = f"/api/v1/teaching/courses/{COURSE_ID}/generation-policy"
    state_sha256 = "1" * 64
    return {
        "sequence": 0,
        "layer": "database",
        "target": {
            "targetType": "course",
            "resourceIds": {"courseId": COURSE_ID},
            "ownerTenantId": OWNER_TENANT_ID,
            "ownerActorId": OWNER_ACTOR_ID,
        },
        "operations": [
            _operation(
                "owner-policy-before",
                "owner-before",
                "GET",
                policy_path,
                COURSE_ID,
                owner=True,
                status_code=200,
                state_sha256=state_sha256,
            ),
            _operation(
                "foreign-list",
                "foreign-check",
                "GET",
                "/api/v1/teaching/courses",
                COURSE_ID,
                owner=False,
                actor_owner=True,
                status_code=200,
                state_sha256="2" * 64,
                target_omitted=True,
            ),
            _operation(
                "foreign-read",
                "foreign-check",
                "GET",
                policy_path,
                COURSE_ID,
                owner=False,
                status_code=404,
                error_code="not_found",
            ),
            _operation(
                "foreign-write",
                "foreign-check",
                "PUT",
                policy_path,
                COURSE_ID,
                owner=False,
                status_code=404,
                request_sha256="3" * 64,
                error_code="not_found",
            ),
            _operation(
                "owner-policy-after",
                "owner-after",
                "GET",
                policy_path,
                COURSE_ID,
                owner=True,
                status_code=200,
                state_sha256=state_sha256,
            ),
        ],
    }


def _objects_observation() -> dict[str, object]:
    document_path = f"/api/v1/classroom-versions/{CLASSROOM_VERSION_ID}/document"
    return {
        "sequence": 1,
        "layer": "objects",
        "target": {
            "targetType": "classroom_source_document",
            "resourceIds": {
                "bindingId": SOURCE_BINDING_ID,
                "classroomVersionId": CLASSROOM_VERSION_ID,
            },
            "ownerTenantId": OWNER_TENANT_ID,
            "ownerActorId": OWNER_ACTOR_ID,
        },
        "operations": [
            _operation(
                "owner-source-list-before",
                "owner-before",
                "GET",
                "/api/v1/teaching/sources",
                SOURCE_BINDING_ID,
                owner=True,
                status_code=200,
                state_sha256="4" * 64,
                target_omitted=False,
            ),
            _operation(
                "owner-document-before",
                "owner-before",
                "GET",
                document_path,
                CLASSROOM_VERSION_ID,
                owner=True,
                status_code=200,
                state_sha256="5" * 64,
            ),
            _operation(
                "foreign-source-list",
                "foreign-check",
                "GET",
                "/api/v1/teaching/sources",
                SOURCE_BINDING_ID,
                owner=False,
                status_code=200,
                state_sha256="6" * 64,
                target_omitted=True,
            ),
            _operation(
                "foreign-document-read",
                "foreign-check",
                "GET",
                document_path,
                CLASSROOM_VERSION_ID,
                owner=False,
                status_code=404,
                error_code="not_found",
            ),
            _operation(
                "foreign-source-delete",
                "foreign-check",
                "DELETE",
                f"/api/v1/teaching/sources/{SOURCE_BINDING_ID}",
                SOURCE_BINDING_ID,
                owner=False,
                status_code=404,
                error_code="not_found",
            ),
            _operation(
                "owner-source-list-after",
                "owner-after",
                "GET",
                "/api/v1/teaching/sources",
                SOURCE_BINDING_ID,
                owner=True,
                status_code=200,
                state_sha256="4" * 64,
                target_omitted=False,
            ),
            _operation(
                "owner-document-after",
                "owner-after",
                "GET",
                document_path,
                CLASSROOM_VERSION_ID,
                owner=True,
                status_code=200,
                state_sha256="5" * 64,
            ),
        ],
    }


def _exports_observation() -> dict[str, object]:
    status_path = f"/api/v1/classroom-exports/{EXPORT_ID}"
    download_path = f"{status_path}/download"
    return {
        "sequence": 2,
        "layer": "exports",
        "target": {
            "targetType": "classroom_export",
            "resourceIds": {"exportId": EXPORT_ID},
            "ownerTenantId": OWNER_TENANT_ID,
            "ownerActorId": OWNER_ACTOR_ID,
        },
        "operations": [
            _operation(
                "owner-status-before",
                "owner-before",
                "GET",
                status_path,
                EXPORT_ID,
                owner=True,
                status_code=200,
                state_sha256="7" * 64,
            ),
            _operation(
                "owner-download-before",
                "owner-before",
                "GET",
                download_path,
                EXPORT_ID,
                owner=True,
                status_code=200,
                state_sha256="8" * 64,
            ),
            _operation(
                "foreign-status-read",
                "foreign-check",
                "GET",
                status_path,
                EXPORT_ID,
                owner=False,
                status_code=404,
                error_code="not_found",
            ),
            _operation(
                "foreign-download-read",
                "foreign-check",
                "GET",
                download_path,
                EXPORT_ID,
                owner=False,
                status_code=404,
                error_code="not_found",
            ),
            _operation(
                "owner-status-after",
                "owner-after",
                "GET",
                status_path,
                EXPORT_ID,
                owner=True,
                status_code=200,
                state_sha256="7" * 64,
            ),
            _operation(
                "owner-download-after",
                "owner-after",
                "GET",
                download_path,
                EXPORT_ID,
                owner=True,
                status_code=200,
                state_sha256="8" * 64,
            ),
        ],
    }


def _events_observation() -> dict[str, object]:
    session_path = f"/api/v1/classroom-sessions/{SESSION_ID}"
    projection_path = f"/api/v1/teaching-reports/classrooms/{CLASSROOM_VERSION_ID}"
    return {
        "sequence": 3,
        "layer": "events",
        "target": {
            "targetType": "learning_session_event",
            "resourceIds": {
                "sessionId": SESSION_ID,
                "eventId": EVENT_ID,
                "classroomVersionId": CLASSROOM_VERSION_ID,
            },
            "ownerTenantId": OWNER_TENANT_ID,
            "ownerActorId": OWNER_ACTOR_ID,
        },
        "operations": [
            _operation(
                "owner-session-before",
                "owner-before",
                "GET",
                session_path,
                SESSION_ID,
                owner=True,
                status_code=200,
                state_sha256="9" * 64,
            ),
            _operation(
                "owner-projection-before",
                "owner-before",
                "GET",
                projection_path,
                CLASSROOM_VERSION_ID,
                owner=True,
                status_code=200,
                state_sha256="a" * 64,
            ),
            _operation(
                "foreign-session-read",
                "foreign-check",
                "GET",
                session_path,
                SESSION_ID,
                owner=False,
                actor_owner=True,
                status_code=404,
                error_code="not_found",
            ),
            _operation(
                "foreign-ticket-issue",
                "foreign-check",
                "POST",
                f"{session_path}/event-ticket",
                SESSION_ID,
                owner=False,
                actor_owner=True,
                status_code=404,
                error_code="not_found",
            ),
            _operation(
                "foreign-event-ingest",
                "foreign-check",
                "POST",
                f"{session_path}/events",
                EVENT_ID,
                owner=False,
                actor_owner=True,
                status_code=404,
                request_sha256="b" * 64,
                error_code="not_found",
            ),
            _operation(
                "owner-session-after",
                "owner-after",
                "GET",
                session_path,
                SESSION_ID,
                owner=True,
                status_code=200,
                state_sha256="9" * 64,
            ),
            _operation(
                "owner-projection-after",
                "owner-after",
                "GET",
                projection_path,
                CLASSROOM_VERSION_ID,
                owner=True,
                status_code=200,
                state_sha256="a" * 64,
            ),
        ],
    }


def _report() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "producer": "tenant-isolation-probe",
        "candidate": _candidate(),
        "releaseRun": _release_run(),
        "observedAt": "2026-08-28T00:00:00Z",
        "baseUrl": BASE_URL,
        "capacityProof": {
            "reportSha256": CAPACITY_REPORT_SHA256,
            "tenantIds": list(CAPACITY_TENANT_IDS),
        },
        "principals": [
            {
                "tenantId": OWNER_TENANT_ID,
                "actorId": OWNER_ACTOR_ID,
                "role": "user",
                "membershipStatus": "active",
            },
            {
                "tenantId": FOREIGN_TENANT_ID,
                "actorId": FOREIGN_ACTOR_ID,
                "role": "user",
                "membershipStatus": "active",
            },
        ],
        "crossTenantPrincipal": {
            "tenantId": FOREIGN_TENANT_ID,
            "actorId": OWNER_ACTOR_ID,
            "role": "student",
            "membershipStatus": "active",
        },
        "observations": [
            _database_observation(),
            _objects_observation(),
            _exports_observation(),
            _events_observation(),
        ],
    }


def _body(report: dict[str, object]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _parse(
    report: dict[str, object],
    *,
    forbidden_secret_values: tuple[bytes, ...] = (),
) -> dict[str, object]:
    return _module().parse_tenant_isolation_report(
        _body(report),
        candidate=_candidate(),
        release_run=_release_run(),
        expected_base_url=BASE_URL,
        expected_capacity_report_sha256=CAPACITY_REPORT_SHA256,
        expected_capacity_tenant_ids=CAPACITY_TENANT_IDS,
        forbidden_secret_values=forbidden_secret_values,
    )


def _layer(report: dict[str, object], layer: str) -> dict[str, object]:
    return next(item for item in report["observations"] if item["layer"] == layer)


def _operation_named(
    report: dict[str, object],
    layer: str,
    name: str,
) -> dict[str, object]:
    observation = _layer(report, layer)
    return next(item for item in observation["operations"] if item["name"] == name)


def _replace_string(value: object, old: str, new: str) -> object:
    if isinstance(value, dict):
        return {key: _replace_string(nested, old, new) for key, nested in value.items()}
    if isinstance(value, list):
        return [_replace_string(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def test_tenant_isolation_contract_accepts_capacity_bound_real_api_matrix() -> None:
    module = _module()
    report = _report()

    assert module.TENANT_ISOLATION_PRODUCER == "tenant-isolation-probe"
    assert module.MAX_TENANT_ISOLATION_REPORT_BYTES >= len(_body(report))
    assert module.tenant_isolation_command_record() == {
        "runner": "python",
        "script": "scripts/tenant_isolation_probe.py",
        "arguments": ["--profile", "first-release"],
    }
    assert module.canonical_tenant_isolation_report(report) == _body(report)
    assert _parse(report) == report

    for layer in LAYERS:
        names = tuple(item["name"] for item in _layer(report, layer)["operations"])
        assert names == OPERATION_NAMES[layer]
    body = _body(report)
    assert b"/api/v1/classroom-versions/" in body
    assert b"/api/v1/classroom-exports/" in body
    assert b"/api/v1/classroom-sessions/" in body
    assert b"/api/v1/classrooms/versions/" not in body
    assert b"/api/v1/classrooms/exports/" not in body
    assert b"/api/v1/classrooms/events/" not in body
    assert b"objectKey" not in body
    assert b"/classrooms/" not in SOURCE_BINDING_ID.encode()


def test_event_isolation_keeps_the_actor_constant_while_switching_tenants() -> None:
    report = _report()
    events = report["observations"][3]
    foreign_session = events["operations"][2]
    assert foreign_session["tenantId"] == FOREIGN_TENANT_ID
    assert foreign_session["actorId"] == OWNER_ACTOR_ID
    assert report["crossTenantPrincipal"] == {
        "tenantId": FOREIGN_TENANT_ID,
        "actorId": OWNER_ACTOR_ID,
        "role": "student",
        "membershipStatus": "active",
    }

    foreign_session["actorId"] = FOREIGN_ACTOR_ID
    with pytest.raises(ValueError, match="target principal"):
        _parse(report)

    unattested = _report()
    unattested["crossTenantPrincipal"]["membershipStatus"] = "inactive"
    with pytest.raises(ValueError, match="cross-tenant principal"):
        _parse(unattested)


def test_tenant_isolation_checks_require_every_exact_operation_and_unchanged_owner() -> None:
    module = _module()
    report = _report()
    assert module.derive_tenant_isolation_checks(_parse(report)) == {
        "databaseIsolated": True,
        "objectsIsolated": True,
        "exportsIsolated": True,
        "eventsIsolated": True,
    }

    foreign_write_succeeded = _report()
    operation = _operation_named(foreign_write_succeeded, "database", "foreign-write")
    operation.update(statusCode=200, errorCode=None, stateSha256="c" * 64)
    assert module.derive_tenant_isolation_checks(_parse(foreign_write_succeeded)) == {
        "databaseIsolated": False,
        "objectsIsolated": True,
        "exportsIsolated": True,
        "eventsIsolated": True,
    }

    changed_projection = _report()
    _operation_named(changed_projection, "events", "owner-projection-after")["stateSha256"] = (
        "d" * 64
    )
    assert module.derive_tenant_isolation_checks(_parse(changed_projection)) == {
        "databaseIsolated": True,
        "objectsIsolated": True,
        "exportsIsolated": True,
        "eventsIsolated": False,
    }


def test_tenant_isolation_report_rejects_admin_inactive_or_unattested_principals() -> None:
    admin = _report()
    admin["principals"][1]["role"] = "admin"

    inactive = _report()
    inactive["principals"][0]["membershipStatus"] = "inactive"

    wrong_capacity_tenant = _report()
    wrong_capacity_tenant["capacityProof"]["tenantIds"][1] = "t_" + "e" * 32

    wrong_capacity_sha = _report()
    wrong_capacity_sha["capacityProof"]["reportSha256"] = "f" * 64

    for report, message in (
        (admin, "role|admin|principal"),
        (inactive, "membership|active|principal"),
        (wrong_capacity_tenant, "capacity|tenant|principal"),
        (wrong_capacity_sha, "capacity|sha|digest|proof"),
    ):
        with pytest.raises(ValueError, match=message):
            _parse(report)


def test_tenant_isolation_report_rejects_unrelated_404_and_incomplete_matrix() -> None:
    unrelated = _report()
    operation = _operation_named(unrelated, "database", "foreign-read")
    operation["path"] = "/api/v1/definitely-not-a-target"
    operation["observedTargetId"] = "unrelated-target"

    missing_delete = _report()
    object_operations = _layer(missing_delete, "objects")["operations"]
    object_operations[:] = [
        item for item in object_operations if item["name"] != "foreign-source-delete"
    ]

    wrong_legacy_route = _report()
    operation = _operation_named(wrong_legacy_route, "objects", "foreign-document-read")
    operation["path"] = f"/api/v1/classrooms/versions/{CLASSROOM_VERSION_ID}/document"

    for report in (unrelated, missing_delete, wrong_legacy_route):
        with pytest.raises(ValueError, match="operation|matrix|path|target"):
            _parse(report)


def test_tenant_isolation_report_rejects_object_keys_and_secret_values() -> None:
    object_key = f"{OWNER_TENANT_ID}/classrooms/{CLASSROOM_VERSION_ID}/classroom.json"
    raw_object_key = _replace_string(_report(), SOURCE_BINDING_ID, object_key)
    assert isinstance(raw_object_key, dict)
    with pytest.raises(ValueError, match="object|binding|resource|public"):
        _parse(raw_object_key)

    secret_value = "ghp_" + "s" * 36
    secret_bearing = _replace_string(_report(), COURSE_ID, secret_value)
    assert isinstance(secret_bearing, dict)
    with pytest.raises(ValueError, match="secret|sensitive|forbidden"):
        _parse(
            secret_bearing,
            forbidden_secret_values=(secret_value.encode("utf-8"),),
        )


def test_tenant_isolation_report_rejects_binding_schema_and_numeric_confusion() -> None:
    extra = _report()
    extra["checks"] = {f"{layer}Isolated": True for layer in LAYERS}

    wrong_candidate = _report()
    wrong_candidate["candidate"] = {**_candidate(), "sourceHead": "0" * 40}

    wrong_run = _report()
    wrong_run["releaseRun"] = {**_release_run(), "runId": "different-run"}

    wrong_url = _report()
    wrong_url["baseUrl"] = "https://other.example.test"

    boolean_status = _report()
    _operation_named(boolean_status, "database", "owner-policy-before")["statusCode"] = True

    for report, message in (
        (extra, "schema|field"),
        (wrong_candidate, "candidate|binding"),
        (wrong_run, "run|binding"),
        (wrong_url, "URL|binding"),
        (boolean_status, "status|integer|boolean"),
    ):
        with pytest.raises(ValueError, match=message):
            _parse(report)


def test_tenant_isolation_report_rejects_noncanonical_and_oversized_json() -> None:
    module = _module()
    report = _report()
    noncanonical = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    kwargs = {
        "candidate": _candidate(),
        "release_run": _release_run(),
        "expected_base_url": BASE_URL,
        "expected_capacity_report_sha256": CAPACITY_REPORT_SHA256,
        "expected_capacity_tenant_ids": CAPACITY_TENANT_IDS,
        "forbidden_secret_values": (),
    }

    with pytest.raises(ValueError, match="canonical"):
        module.parse_tenant_isolation_report(noncanonical, **kwargs)

    with pytest.raises(ValueError, match="large|size"):
        module.parse_tenant_isolation_report(
            b" " * (module.MAX_TENANT_ISOLATION_REPORT_BYTES + 1),
            **kwargs,
        )
