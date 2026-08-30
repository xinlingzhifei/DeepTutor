"""Strict candidate-bound contract for backup/restore release evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import hashlib
import json
import math
from pathlib import PurePath
import re
from urllib.parse import urlsplit

BACKUP_RESTORE_SCHEMA_VERSION = 1
BACKUP_RESTORE_PRODUCER = "backup-restore-probe"
MAX_BACKUP_RESTORE_REPORT_BYTES = 128 * 1024
MAX_RESTORE_VALIDATION_ARTIFACT_BYTES = 64 * 1024
MAX_TARGET_PROVISIONING_RECEIPT_BYTES = 64 * 1024
TARGET_PROVISIONING_RECEIPT_PRODUCER = "backup-restore-target-provisioner"

_TOP_LEVEL_FIELDS = {
    "schemaVersion",
    "producer",
    "candidate",
    "releaseRun",
    "observedAt",
    "consistency",
    "source",
    "target",
    "execution",
    "findings",
    "retention",
}
_SOURCE_FIELDS = {
    "manifestSha256",
    "archiveFingerprintSha256",
    "databaseIdentitySha256",
    "databaseSha256",
    "objectStoreIdentitySha256",
    "objectInventorySha256",
    "platformSchemaRevision",
    "schemaRevisions",
    "classroomVersionsCount",
    "learningEventsCount",
    "objectCount",
    "provenanceSha256",
}
_LEGACY_TARGET_FIELDS = {
    "databaseId",
    "databaseIdentitySha256",
    "databaseOwnership",
    "databaseWasEmpty",
    "databaseDistinctFromSource",
    "objectNamespaceId",
    "objectStoreIdentitySha256",
    "objectNamespaceOwnership",
    "objectNamespaceWasEmpty",
    "objectNamespaceDistinctFromSource",
    "objectVersioningEnabled",
    "targetConfigSha256",
}
_TARGET_FIELDS = _LEGACY_TARGET_FIELDS | {
    "databaseHost",
    "databasePort",
    "databaseCurrentRole",
    "databaseOwner",
    "databasePreRestoreUserObjectCount",
    "databasePostRestoreUserObjectCount",
    "objectEndpoint",
    "objectRegion",
    "objectNamespace",
    "objectBucket",
    "objectOwnerIdSha256",
    "objectPreRestoreObjectCount",
    "objectPostRestoreObjectCount",
    "objectPreRestoreVersionCount",
    "objectPostRestoreVersionCount",
    "objectPreRestoreDeleteMarkerCount",
    "objectPostRestoreDeleteMarkerCount",
    "concurrencyExclusionMode",
    "concurrencyExclusionIdentitySha256",
    "concurrencyExclusionHeldThroughPostValidation",
    "operatorTargetObservationsSha256",
    "provisioningReceiptSha256",
}
_COMMAND_FIELDS = {
    "sequence",
    "name",
    "argv",
    "nativeExit",
    "startedAt",
    "finishedAt",
    "durationMs",
    "stdoutSha256",
    "stderrSha256",
    "artifact",
    "artifactSha256",
}
_RESTORE_VALIDATION_ARTIFACT = "restore-validation.json"
_EXPECTED_RESTORE_VALIDATIONS = [
    "platform_schema_revision",
    "schema_revisions",
    "classroom_versions",
    "learning_events",
    "database_object_references",
    "source_snapshots",
    "media",
    "quota",
    "audit",
    "app_role_access",
]
_OWNERSHIP_VALUES = {"runner-owned-disposable", "retained-audit"}
_RESTORE_DATABASE_USER = "yfeistai_migrator"
_EXPECTED_CONSISTENCY = {
    "databaseSnapshot": "postgresql-consistent-dump",
    "objectSnapshot": "version-pinned-inventory",
    "crossSystemAtomic": False,
    "partialBackupArtifacts": "retained",
}
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secretvalue",
    "ticket",
    "token",
)
_FORBIDDEN_VALUE_OPTIONS = {
    "--authorization",
    "--cookie",
    "--credential",
    "--password",
    "--secret",
    "--token",
}

_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def backup_restore_command_record() -> dict[str, object]:
    """Return the secret-free logical command used by the release verifier."""

    return {
        "runner": "python",
        "script": "scripts/backup_restore_probe.py",
        "arguments": ["--profile", "first-release"],
    }


def canonical_backup_restore_report(report: Mapping[str, object]) -> bytes:
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


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None and value != "0" * 64


def _valid_public_id(value: object) -> bool:
    return isinstance(value, str) and _PUBLIC_ID.fullmatch(value) is not None


def canonical_object_store_endpoint(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2048
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("object store endpoint is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("object store endpoint is invalid") from None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or not isinstance(hostname, str)
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("object store endpoint is invalid")
    canonical_host = hostname.lower()
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        canonical_host = f"{canonical_host}:{port}"
    return f"{scheme}://{canonical_host}"


def physical_object_store_identity_sha256(
    endpoint: object,
    region: object,
    bucket: object,
    owner_id_sha256: object,
) -> str:
    if (
        not isinstance(region, str)
        or not region
        or region != region.strip()
        or len(region) > 191
        or any(ord(character) < 0x20 for character in region)
        or not _valid_public_id(bucket)
        or not _valid_sha256(owner_id_sha256)
    ):
        raise ValueError("object store physical identity is invalid")
    return hashlib.sha256(
        canonical_backup_restore_report(
            {
                "bucket": bucket,
                "endpoint": canonical_object_store_endpoint(endpoint),
                "ownerIdSha256": owner_id_sha256,
                "region": region.lower(),
            }
        )
    ).hexdigest()


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or _OBSERVED_AT.fullmatch(value) is None:
        raise ValueError(f"backup restore {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError(f"backup restore {field} is invalid") from None
    return parsed


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"backup restore {field} is invalid")
    return value


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


def _reject_forbidden_secret_values(body: bytes, secrets: Sequence[bytes]) -> None:
    for secret in secrets:
        if not isinstance(secret, bytes) or not secret:
            continue
        if secret in body:
            raise ValueError("backup restore report contains a forbidden secret value")


def _json_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _json_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _json_strings(nested)


def _reject_forbidden_decoded_secret_values(
    value: object,
    secrets: Sequence[bytes],
) -> None:
    decoded_secrets: list[str] = []
    for secret in secrets:
        if not isinstance(secret, bytes) or not secret:
            continue
        try:
            decoded_secrets.append(secret.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    if any(secret in text for text in _json_strings(value) for secret in decoded_secrets):
        raise ValueError("backup restore report contains a forbidden secret value")


def _reject_forbidden_artifact_secret_values(
    body: object,
    secrets: Sequence[bytes],
) -> None:
    if not isinstance(body, bytes):
        return
    _reject_forbidden_secret_values(body, secrets)
    try:
        decoded = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return
    _reject_forbidden_decoded_secret_values(decoded, secrets)


def _validate_candidate(candidate: object) -> None:
    if not isinstance(candidate, dict) or set(candidate) != {
        "sourceRepository",
        "sourceHead",
        "releaseTag",
        "openmaicHead",
        "imageDigests",
    }:
        raise ValueError("backup restore candidate schema is invalid")
    if not _valid_public_id(candidate.get("sourceRepository")) and not (
        isinstance(candidate.get("sourceRepository"), str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate["sourceRepository"])
    ):
        raise ValueError("backup restore candidate repository is invalid")
    if _GIT_SHA.fullmatch(str(candidate.get("sourceHead", ""))) is None:
        raise ValueError("backup restore candidate source revision is invalid")
    if _GIT_SHA.fullmatch(str(candidate.get("openmaicHead", ""))) is None:
        raise ValueError("backup restore candidate OpenMAIC revision is invalid")
    if not _valid_public_id(candidate.get("releaseTag")):
        raise ValueError("backup restore candidate release tag is invalid")
    image_digests = candidate.get("imageDigests")
    if not isinstance(image_digests, dict) or set(image_digests) != {
        "deeptutor",
        "openmaic",
        "openmaic_render",
    }:
        raise ValueError("backup restore candidate image digest schema is invalid")
    if any(
        not isinstance(value, str) or _IMAGE_DIGEST.fullmatch(value) is None
        for value in image_digests.values()
    ):
        raise ValueError("backup restore candidate image digest is invalid")


def _validate_release_run(release_run: object) -> None:
    if (
        not isinstance(release_run, dict)
        or set(release_run) != {"runId", "environmentId"}
        or any(not _valid_public_id(value) for value in release_run.values())
    ):
        raise ValueError("backup restore release run is invalid")


def validate_backup_restore_identity(
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate candidate/run identity before any live restore side effect."""

    bound_candidate = dict(candidate) if isinstance(candidate, Mapping) else candidate
    bound_run = dict(release_run) if isinstance(release_run, Mapping) else release_run
    _validate_candidate(bound_candidate)
    _validate_release_run(bound_run)
    return bound_candidate, bound_run


def parse_target_provisioning_receipt(
    body: bytes,
    *,
    provisioning_receipt_sha256: str,
    candidate_sha256: str,
    release_run: Mapping[str, object],
    database_disposition: str,
    object_store_disposition: str,
    database_identity_sha256: str | None = None,
    object_store_identity_sha256: str | None = None,
) -> dict[str, object]:
    if (
        not isinstance(body, bytes)
        or not body
        or len(body) > MAX_TARGET_PROVISIONING_RECEIPT_BYTES
        or not _valid_sha256(provisioning_receipt_sha256)
        or not _valid_sha256(candidate_sha256)
    ):
        raise ValueError("target provisioning receipt is invalid")
    if hashlib.sha256(body).hexdigest() != provisioning_receipt_sha256:
        raise ValueError("target provisioning receipt digest is invalid")
    _validate_release_run(release_run)
    if (
        database_disposition not in _OWNERSHIP_VALUES
        or object_store_disposition not in _OWNERSHIP_VALUES
    ):
        raise ValueError("target provisioning receipt disposition is invalid")
    try:
        receipt = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("target provisioning receipt JSON is invalid") from None
    if (
        not isinstance(receipt, dict)
        or canonical_backup_restore_report(receipt) != body
        or set(receipt)
        != {"schemaVersion", "producer", "candidateSha256", "releaseRun", "resources"}
        or receipt.get("schemaVersion") != 1
        or receipt.get("producer") != TARGET_PROVISIONING_RECEIPT_PRODUCER
        or receipt.get("candidateSha256") != candidate_sha256
        or not _exact_json_equal(receipt.get("releaseRun"), dict(release_run))
    ):
        raise ValueError("target provisioning receipt binding is invalid")
    resources = receipt.get("resources")
    if not isinstance(resources, dict) or set(resources) != {"database", "objectStore"}:
        raise ValueError("target provisioning receipt resources are invalid")
    expected = (
        ("database", database_disposition, database_identity_sha256),
        ("objectStore", object_store_disposition, object_store_identity_sha256),
    )
    for name, disposition, expected_identity in expected:
        resource = resources.get(name)
        if (
            not isinstance(resource, dict)
            or set(resource) != {"identitySha256", "ownerRunId", "disposition"}
            or not _valid_sha256(resource.get("identitySha256"))
            or resource.get("ownerRunId") != release_run["runId"]
            or resource.get("disposition") != disposition
            or (
                expected_identity is not None
                and resource.get("identitySha256") != expected_identity
            )
        ):
            raise ValueError("target provisioning receipt resource binding is invalid")
    return receipt


def canonical_source_provenance(provenance: Mapping[str, object]) -> bytes:
    return canonical_backup_restore_report(provenance)


def validate_backup_restore_source_provenance(
    provenance: Mapping[str, object],
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
    source: Mapping[str, object],
) -> bytes:
    """Bind one independently supplied source record to this archive and run."""

    bound_candidate, bound_run = validate_backup_restore_identity(candidate, release_run)
    expected = {
        "schemaVersion": 1,
        "candidate": bound_candidate,
        "releaseRun": bound_run,
        "source": {
            "manifestSha256": source.get("manifestSha256"),
            "archiveFingerprintSha256": source.get("archiveFingerprintSha256"),
            "databaseIdentitySha256": source.get("databaseIdentitySha256"),
            "objectStoreIdentitySha256": source.get("objectStoreIdentitySha256"),
        },
    }
    if not isinstance(provenance, Mapping) or not _exact_json_equal(dict(provenance), expected):
        raise ValueError("backup restore source provenance is invalid")
    body = canonical_source_provenance(dict(provenance))
    if len(body) > MAX_RESTORE_VALIDATION_ARTIFACT_BYTES:
        raise ValueError("backup restore source provenance is invalid")
    return body


def _validate_source(
    source: object,
    *,
    expected_manifest_sha256: str,
    expected_archive_fingerprint_sha256: str,
    expected_provenance_sha256: str | None = None,
) -> dict[str, object]:
    expected_fields = (
        _SOURCE_FIELDS
        if expected_provenance_sha256 is not None
        else _SOURCE_FIELDS - {"provenanceSha256"}
    )
    if not isinstance(source, dict) or set(source) != expected_fields:
        raise ValueError("backup restore source schema is invalid")
    digest_fields = (
        "manifestSha256",
        "archiveFingerprintSha256",
        "databaseIdentitySha256",
        "databaseSha256",
        "objectStoreIdentitySha256",
        "objectInventorySha256",
    )
    if any(not _valid_sha256(source.get(field)) for field in digest_fields):
        raise ValueError("backup restore source digest is invalid")
    if source["manifestSha256"] != expected_manifest_sha256:
        raise ValueError("backup restore source manifest provenance is invalid")
    if source["archiveFingerprintSha256"] != expected_archive_fingerprint_sha256:
        raise ValueError("backup restore source archive provenance is invalid")
    if (
        expected_provenance_sha256 is not None
        and source.get("provenanceSha256") != expected_provenance_sha256
    ):
        raise ValueError("backup restore source provenance artifact is invalid")
    revision = source.get("platformSchemaRevision")
    revisions = source.get("schemaRevisions")
    if not _valid_public_id(revision):
        raise ValueError("backup restore source platform revision is invalid")
    if not isinstance(revisions, dict) or not revisions:
        raise ValueError("backup restore source schema revisions are invalid")
    for tenant_id, tenant_revision in revisions.items():
        if not _valid_public_id(tenant_id) or not _valid_public_id(tenant_revision):
            raise ValueError("backup restore source schema revisions are invalid")
    for field in ("classroomVersionsCount", "learningEventsCount", "objectCount"):
        _nonnegative_integer(source.get(field), f"source {field}")
    return source


def _source_from_verified_backup(
    backup: object,
    *,
    provenance_sha256: str | None = None,
) -> dict[str, object]:
    try:
        manifest = backup.manifest
        source = {
            "manifestSha256": backup.manifest_sha256,
            "archiveFingerprintSha256": backup.archive_fingerprint_sha256,
            "databaseIdentitySha256": manifest.database.identity_sha256,
            "databaseSha256": backup.database_sha256,
            "objectStoreIdentitySha256": manifest.source_object_store_identity_sha256,
            "objectInventorySha256": backup.object_inventory_sha256,
            "platformSchemaRevision": manifest.platform_schema_revision,
            "schemaRevisions": dict(sorted(manifest.schema_revisions.items())),
            "classroomVersionsCount": manifest.classroom_versions_count,
            "learningEventsCount": manifest.learning_events_count,
            "objectCount": manifest.object_count,
        }
        if provenance_sha256 is not None:
            source["provenanceSha256"] = provenance_sha256
    except (AttributeError, TypeError, ValueError):
        raise ValueError("backup restore verified backup is invalid") from None
    try:
        return _validate_source(
            source,
            expected_manifest_sha256=str(source["manifestSha256"]),
            expected_archive_fingerprint_sha256=str(source["archiveFingerprintSha256"]),
            expected_provenance_sha256=provenance_sha256,
        )
    except ValueError:
        raise ValueError("backup restore verified backup is invalid") from None


def _validate_target(
    target: object,
    *,
    source: Mapping[str, object],
    expected_database_ownership: str,
    expected_object_namespace_ownership: str,
) -> dict[str, object]:
    if not isinstance(target, dict):
        raise ValueError("backup restore target schema is invalid")
    target_fields = set(target)
    rich_target = target_fields == _TARGET_FIELDS
    if not rich_target:
        raise ValueError("backup restore rich target evidence is required")
    if expected_database_ownership not in _OWNERSHIP_VALUES:
        raise ValueError("backup restore target database ownership expectation is invalid")
    if expected_object_namespace_ownership not in _OWNERSHIP_VALUES:
        raise ValueError("backup restore target object ownership expectation is invalid")
    if (
        not _valid_public_id(target.get("databaseId"))
        or not _valid_public_id(target.get("objectNamespaceId"))
        or not _valid_sha256(target.get("databaseIdentitySha256"))
        or not _valid_sha256(target.get("objectStoreIdentitySha256"))
        or target.get("databaseOwnership") != expected_database_ownership
        or target.get("objectNamespaceOwnership") != expected_object_namespace_ownership
    ):
        raise ValueError("backup restore target identity is invalid")
    if "targetConfigSha256" in target and not _valid_sha256(target.get("targetConfigSha256")):
        raise ValueError("backup restore target config provenance is invalid")
    required_true = (
        "databaseWasEmpty",
        "databaseDistinctFromSource",
        "objectNamespaceWasEmpty",
        "objectNamespaceDistinctFromSource",
        "objectVersioningEnabled",
    )
    if any(target.get(field) is not True for field in required_true):
        raise ValueError("backup restore target isolation is invalid")
    if (
        target["databaseIdentitySha256"] == source["databaseIdentitySha256"]
        or target["objectStoreIdentitySha256"] == source["objectStoreIdentitySha256"]
    ):
        raise ValueError("backup restore target must be distinct from source")
    if rich_target:
        count_fields = (
            "databasePreRestoreUserObjectCount",
            "databasePostRestoreUserObjectCount",
            "objectPreRestoreObjectCount",
            "objectPostRestoreObjectCount",
            "objectPreRestoreVersionCount",
            "objectPostRestoreVersionCount",
            "objectPreRestoreDeleteMarkerCount",
            "objectPostRestoreDeleteMarkerCount",
        )
        if any(
            isinstance(target.get(field), bool)
            or not isinstance(target.get(field), int)
            or target[field] < 0
            for field in count_fields
        ):
            raise ValueError("backup restore target observations are invalid")
        text_fields = (
            "databaseHost",
            "databaseCurrentRole",
            "databaseOwner",
            "objectEndpoint",
            "objectRegion",
        )
        if any(
            not isinstance(target.get(field), str)
            or not target[field]
            or len(target[field]) > 2048
            or any(ord(character) < 0x20 for character in target[field])
            for field in text_fields
        ):
            raise ValueError("backup restore target observations are invalid")
        if (
            isinstance(target.get("databasePort"), bool)
            or not isinstance(target.get("databasePort"), int)
            or not 1 <= target["databasePort"] <= 65535
            or target.get("databasePreRestoreUserObjectCount") != 0
            or target.get("databasePostRestoreUserObjectCount") <= 0
            or target.get("objectPreRestoreObjectCount") != 0
            or target.get("objectPreRestoreVersionCount") != 0
            or target.get("objectPreRestoreDeleteMarkerCount") != 0
            or target.get("objectPostRestoreObjectCount") != source["objectCount"]
            or target.get("objectPostRestoreVersionCount") != source["objectCount"]
            or target.get("objectPostRestoreDeleteMarkerCount") != 0
            or target.get("databaseCurrentRole") != _RESTORE_DATABASE_USER
            or target.get("databaseOwner") != _RESTORE_DATABASE_USER
            or not _valid_public_id(target.get("objectNamespace"))
            or not _valid_public_id(target.get("objectBucket"))
            or target.get("objectNamespaceId")
            != f"{target.get('objectNamespace')}:{target.get('objectBucket')}"
            or target.get("concurrencyExclusionMode") != "postgresql-session-advisory-lock"
            or target.get("concurrencyExclusionHeldThroughPostValidation") is not True
            or not _valid_sha256(target.get("objectOwnerIdSha256"))
            or target.get("concurrencyExclusionIdentitySha256")
            != target.get("databaseIdentitySha256")
            or not _valid_sha256(target.get("operatorTargetObservationsSha256"))
        ):
            raise ValueError("backup restore target observations are invalid")
        try:
            expected_object_identity = physical_object_store_identity_sha256(
                target.get("objectEndpoint"),
                target.get("objectRegion"),
                target.get("objectBucket"),
                target.get("objectOwnerIdSha256"),
            )
        except ValueError:
            raise ValueError("backup restore target observations are invalid") from None
        if target.get("objectStoreIdentitySha256") != expected_object_identity:
            raise ValueError("backup restore target physical object identity is invalid")
    return target


def _validate_argv(
    argv: object,
    release_run: Mapping[str, object],
    candidate: Mapping[str, object],
) -> list[str]:
    if (
        not isinstance(argv, list)
        or len(argv) not in {14, 16, 18, 22, 26, 30}
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 4096
            or any(ord(character) < 0x20 for character in item)
            for item in argv
        )
    ):
        raise ValueError("backup restore command argv is invalid")
    if PurePath(argv[1]).name != "restore_teaching_validation.py":
        raise ValueError("backup restore command script is invalid")
    expected_options = ["--backup-dir", "--target-config"]
    if len(argv) >= 16:
        expected_options.append("--target-config-sha256")
    if len(argv) == 30:
        expected_options.extend(["--provisioning-receipt", "--provisioning-receipt-sha256"])
    expected_options.extend(["--target-secret-dir", "--run-id"])
    if len(argv) in {26, 30}:
        expected_options.extend(["--environment-id", "--candidate-sha256"])
    expected_options.extend(["--report", "--pg-restore"])
    if len(argv) in {22, 26, 30}:
        expected_options.extend(["--database-ownership", "--object-namespace-ownership"])
    if len(argv) in {18, 22, 26, 30}:
        expected_options.append("--deadline-monotonic")
    if tuple(argv[2::2]) != tuple(expected_options):
        raise ValueError("backup restore command argv is invalid")
    values = dict(zip(argv[2::2], argv[3::2], strict=True))
    if values["--run-id"] != release_run["runId"]:
        raise ValueError("backup restore command release run is invalid")
    if len(argv) in {26, 30}:
        candidate_sha256 = hashlib.sha256(
            canonical_backup_restore_report(dict(candidate))
        ).hexdigest()
        if (
            values.get("--environment-id") != release_run["environmentId"]
            or values.get("--candidate-sha256") != candidate_sha256
        ):
            raise ValueError("backup restore command release identity is invalid")
    if PurePath(values["--report"]).name != _RESTORE_VALIDATION_ARTIFACT:
        raise ValueError("backup restore command artifact path is invalid")
    if "--target-config-sha256" in values and not _valid_sha256(values["--target-config-sha256"]):
        raise ValueError("backup restore command target config digest is invalid")
    if len(argv) == 30 and (
        PurePath(values["--provisioning-receipt"]).name != "target-provisioning-receipt.json"
        or not _valid_sha256(values["--provisioning-receipt-sha256"])
    ):
        raise ValueError("backup restore command provisioning receipt is invalid")
    if "--deadline-monotonic" in values:
        try:
            deadline_monotonic = float(values["--deadline-monotonic"])
        except ValueError:
            raise ValueError("backup restore command deadline is invalid") from None
        if not math.isfinite(deadline_monotonic) or deadline_monotonic <= 0:
            raise ValueError("backup restore command deadline is invalid")
    if len(argv) in {22, 26, 30} and (
        values.get("--database-ownership") not in _OWNERSHIP_VALUES
        or values.get("--object-namespace-ownership") not in _OWNERSHIP_VALUES
    ):
        raise ValueError("backup restore command ownership is invalid")
    if any(item.lower() in _FORBIDDEN_VALUE_OPTIONS for item in argv):
        raise ValueError("backup restore command must not accept secret values")
    return argv


def _validate_execution(
    execution: object,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
    source: Mapping[str, object],
    target: Mapping[str, object],
    operator_artifact_body: bytes | None = None,
    source_provenance_sha256: str | None = None,
    target_config_body: bytes | None = None,
    provisioning_receipt_body: bytes | None = None,
) -> dict[str, object]:
    if not isinstance(execution, dict) or set(execution) != {
        "commands",
        "artifactSha256s",
    }:
        raise ValueError("backup restore execution schema is invalid")
    commands = execution.get("commands")
    if not isinstance(commands, list) or len(commands) != 1:
        raise ValueError("backup restore command evidence is incomplete")
    command = commands[0]
    if not isinstance(command, dict) or set(command) != _COMMAND_FIELDS:
        raise ValueError("backup restore command schema is invalid")
    command_argv = _validate_argv(command.get("argv"), release_run, candidate)
    if command.get("sequence") != 1 or command.get("name") != "restore-and-verify":
        raise ValueError("backup restore command identity is invalid")
    if isinstance(command.get("nativeExit"), bool) or command.get("nativeExit") != 0:
        raise ValueError("backup restore command native exit is invalid")
    if (
        isinstance(command.get("durationMs"), bool)
        or not isinstance(command.get("durationMs"), int)
        or command["durationMs"] <= 0
    ):
        raise ValueError("backup restore command timing is invalid")
    started = _parse_utc(command.get("startedAt"), "command start time")
    finished = _parse_utc(command.get("finishedAt"), "command finish time")
    if finished < started:
        raise ValueError("backup restore command timing is invalid")
    if any(
        not _valid_sha256(command.get(field))
        for field in ("stdoutSha256", "stderrSha256", "artifactSha256")
    ):
        raise ValueError("backup restore command digest is invalid")
    if command.get("artifact") != _RESTORE_VALIDATION_ARTIFACT:
        raise ValueError("backup restore command artifact is invalid")
    if operator_artifact_body is not None:
        if (
            not isinstance(operator_artifact_body, bytes)
            or not operator_artifact_body
            or len(operator_artifact_body) > MAX_RESTORE_VALIDATION_ARTIFACT_BYTES
        ):
            raise ValueError("backup restore operator artifact byte length is invalid")
        try:
            operator_artifact = json.loads(operator_artifact_body)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("backup restore operator artifact JSON is invalid") from None
        if (
            not isinstance(operator_artifact, dict)
            or canonical_backup_restore_report(operator_artifact) != operator_artifact_body
        ):
            raise ValueError("backup restore operator artifact must be canonical JSON")
        if hashlib.sha256(operator_artifact_body).hexdigest() != command["artifactSha256"]:
            raise ValueError("backup restore operator artifact digest is invalid")
        expected_operator_fields = {
            "schemaVersion",
            "runId",
            "ok",
            "targetDatabaseIdentitySha256",
            "objectPrefix",
            "validated",
            "failures",
            "sourceArchive",
            "database",
            "objects",
            "crossSystemAtomic",
        }
        operator_schema_version = operator_artifact.get("schemaVersion")
        if operator_schema_version != 3:
            raise ValueError("backup restore operator artifact schema version is invalid")
        expected_operator_fields.add("target")
        if set(operator_artifact) != expected_operator_fields:
            raise ValueError("backup restore operator artifact schema is invalid")
        if (
            operator_artifact.get("runId") != release_run["runId"]
            or operator_artifact.get("ok") is not True
            or operator_artifact.get("objectPrefix") != ""
            or operator_artifact.get("validated") != _EXPECTED_RESTORE_VALIDATIONS
            or operator_artifact.get("failures") != []
            or operator_artifact.get("crossSystemAtomic") is not False
        ):
            raise ValueError("backup restore operator artifact findings are invalid")
        if operator_artifact.get("sourceArchive") != {
            "archiveFingerprintSha256": source["archiveFingerprintSha256"],
            "manifestSha256": source["manifestSha256"],
        }:
            raise ValueError("backup restore operator artifact source provenance is invalid")
        if (
            operator_artifact.get("targetDatabaseIdentitySha256")
            != target["databaseIdentitySha256"]
        ):
            raise ValueError("backup restore operator target identity is invalid")
        target_namespace = str(target["objectNamespaceId"])
        _separator, _marker, target_bucket = target_namespace.rpartition(":")
        if operator_artifact.get("database") != {
            "dumpRestoreSingleTransaction": True,
            "postRestoreMutationsAtomic": False,
        } or operator_artifact.get("objects") != {
            "createOnly": True,
            "isolation": "empty_target_bucket",
            "readbackVerified": True,
            "restoredCount": source["objectCount"],
            "targetBucket": target_bucket,
        }:
            raise ValueError("backup restore operator artifact findings are invalid")
        if operator_schema_version == 3:
            if set(target) != _TARGET_FIELDS:
                raise ValueError("backup restore measured target evidence is incomplete")
            target_namespace = target["objectNamespace"]
            target_bucket = target["objectBucket"]
            expected_operator_target = {
                "targetConfigSha256": target["targetConfigSha256"],
                "provisioningReceiptSha256": target["provisioningReceiptSha256"],
                "database": {
                    "host": target["databaseHost"],
                    "port": target["databasePort"],
                    "name": target["databaseId"],
                    "identitySha256": target["databaseIdentitySha256"],
                    "ownership": target["databaseOwnership"],
                    "pre": {
                        "identitySha256": target["databaseIdentitySha256"],
                        "userObjectCount": target["databasePreRestoreUserObjectCount"],
                        "currentRole": target["databaseCurrentRole"],
                        "owner": target["databaseOwner"],
                    },
                    "post": {
                        "identitySha256": target["databaseIdentitySha256"],
                        "userObjectCount": target["databasePostRestoreUserObjectCount"],
                        "currentRole": target["databaseCurrentRole"],
                        "owner": target["databaseOwner"],
                    },
                },
                "objects": {
                    "endpoint": target["objectEndpoint"],
                    "region": target["objectRegion"],
                    "namespaceId": target_namespace,
                    "bucket": target_bucket,
                    "identitySha256": target["objectStoreIdentitySha256"],
                    "ownership": target["objectNamespaceOwnership"],
                    "pre": {
                        "identitySha256": target["objectStoreIdentitySha256"],
                        "versioningEnabled": target["objectVersioningEnabled"],
                        "objectCount": target["objectPreRestoreObjectCount"],
                        "versionCount": target["objectPreRestoreVersionCount"],
                        "deleteMarkerCount": target["objectPreRestoreDeleteMarkerCount"],
                        "ownerIdSha256": target["objectOwnerIdSha256"],
                    },
                    "post": {
                        "identitySha256": target["objectStoreIdentitySha256"],
                        "versioningEnabled": target["objectVersioningEnabled"],
                        "objectCount": target["objectPostRestoreObjectCount"],
                        "versionCount": target["objectPostRestoreVersionCount"],
                        "deleteMarkerCount": target["objectPostRestoreDeleteMarkerCount"],
                        "ownerIdSha256": target["objectOwnerIdSha256"],
                    },
                },
                "concurrencyExclusion": {
                    "mode": target["concurrencyExclusionMode"],
                    "identitySha256": target["concurrencyExclusionIdentitySha256"],
                    "heldThroughPostValidation": target[
                        "concurrencyExclusionHeldThroughPostValidation"
                    ],
                },
            }
            operator_target = operator_artifact.get("target")
            if not _exact_json_equal(operator_target, expected_operator_target):
                raise ValueError("backup restore operator target observations are invalid")
            if (
                hashlib.sha256(canonical_backup_restore_report(operator_target)).hexdigest()
                != target["operatorTargetObservationsSha256"]
            ):
                raise ValueError("backup restore operator target observation digest is invalid")

    artifacts = execution.get("artifactSha256s")
    expected_artifacts = {
        "sourceManifest": source["manifestSha256"],
        "sourceObjectInventory": source["objectInventorySha256"],
        "sourceDatabaseDump": source["databaseSha256"],
        "restoreValidation": command["artifactSha256"],
    }
    if source_provenance_sha256 is not None:
        expected_artifacts["sourceProvenance"] = source_provenance_sha256
    if target_config_body is not None:
        try:
            target_config = json.loads(target_config_body)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("backup restore target config artifact is invalid") from None
        if (
            not isinstance(target_config, dict)
            or canonical_backup_restore_report(target_config) != target_config_body
        ):
            raise ValueError("backup restore target config artifact must be canonical JSON")
        target_config_sha256 = hashlib.sha256(target_config_body).hexdigest()
        if (
            target.get("targetConfigSha256") != target_config_sha256
            or "--target-config-sha256" not in command_argv
            or command_argv[command_argv.index("--target-config-sha256") + 1]
            != target_config_sha256
        ):
            raise ValueError("backup restore target config provenance is invalid")
        expected_artifacts["targetConfigSnapshot"] = target_config_sha256
    if provisioning_receipt_body is not None:
        provisioning_receipt_sha256 = hashlib.sha256(provisioning_receipt_body).hexdigest()
        if (
            target.get("provisioningReceiptSha256") != provisioning_receipt_sha256
            or "--provisioning-receipt-sha256" not in command_argv
            or command_argv[command_argv.index("--provisioning-receipt-sha256") + 1]
            != provisioning_receipt_sha256
        ):
            raise ValueError("backup restore provisioning receipt provenance is invalid")
        expected_artifacts["targetProvisioningReceipt"] = provisioning_receipt_sha256
    if not _exact_json_equal(artifacts, expected_artifacts):
        raise ValueError("backup restore command artifact hashes are invalid")
    return execution


def _validate_findings(
    findings: object,
    *,
    source: Mapping[str, object],
    target: Mapping[str, object],
) -> None:
    if not isinstance(findings, dict) or set(findings) != {
        "database",
        "objects",
        "permissions",
    }:
        raise ValueError("backup restore findings schema is invalid")
    expected_database = {
        "restored": True,
        "dumpRestoreSingleTransaction": True,
        "postRestoreMutationsAtomic": False,
        "sourceDatabaseSha256": source["databaseSha256"],
        "platformSchemaRevision": source["platformSchemaRevision"],
        "schemaRevisions": source["schemaRevisions"],
        "classroomVersionsCount": source["classroomVersionsCount"],
        "learningEventsCount": source["learningEventsCount"],
    }
    if set(target) == _TARGET_FIELDS:
        expected_database.update(
            {
                "preRestoreUserObjectCount": target["databasePreRestoreUserObjectCount"],
                "postRestoreUserObjectCount": target["databasePostRestoreUserObjectCount"],
            }
        )
    if not _exact_json_equal(findings.get("database"), expected_database):
        raise ValueError("backup restore database findings are invalid")
    expected_objects = {
        "restored": True,
        "createOnly": True,
        "readbackVerified": True,
        "objectCount": source["objectCount"],
        "inventorySha256": source["objectInventorySha256"],
        "contentHashesVerified": True,
        "sourceRevisionsVerified": True,
        "versionIdsVerified": True,
    }
    if set(target) == _TARGET_FIELDS:
        expected_objects.update(
            {
                "preRestoreObjectCount": target["objectPreRestoreObjectCount"],
                "postRestoreObjectCount": target["objectPostRestoreObjectCount"],
                "preRestoreVersionCount": target["objectPreRestoreVersionCount"],
                "postRestoreVersionCount": target["objectPostRestoreVersionCount"],
                "preRestoreDeleteMarkerCount": target["objectPreRestoreDeleteMarkerCount"],
                "postRestoreDeleteMarkerCount": target["objectPostRestoreDeleteMarkerCount"],
            }
        )
    if not _exact_json_equal(findings.get("objects"), expected_objects):
        raise ValueError("backup restore objects findings are invalid")
    permissions = findings.get("permissions")
    if (
        not isinstance(permissions, dict)
        or set(permissions) != {"role", "verified", "findingSha256"}
        or permissions.get("role") != "yfeistai_app"
        or permissions.get("verified") is not True
        or not _valid_sha256(permissions.get("findingSha256"))
    ):
        raise ValueError("backup restore permissions findings are invalid")


def _validate_retention(
    retention: object,
    *,
    release_run: Mapping[str, object],
    source: Mapping[str, object],
    target: Mapping[str, object],
) -> None:
    expected = {
        "policy": "no-destructive-cleanup",
        "cleanupAttempted": False,
        "fullCleanupClaimed": False,
        "targets": [
            {
                "kind": "source-backup",
                "id": source["manifestSha256"],
                "ownership": "retained-audit",
            },
            {
                "kind": "database",
                "id": target["databaseId"],
                "ownership": target["databaseOwnership"],
            },
            {
                "kind": "object-namespace",
                "id": target["objectNamespaceId"],
                "ownership": target["objectNamespaceOwnership"],
            },
            {
                "kind": "report",
                "id": release_run["runId"],
                "ownership": "retained-audit",
            },
        ],
    }
    if not _exact_json_equal(retention, expected):
        raise ValueError("backup restore retention evidence is invalid")


def parse_backup_restore_report(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
    expected_source_manifest_sha256: str,
    expected_source_archive_fingerprint_sha256: str,
    expected_database_ownership: str,
    expected_object_namespace_ownership: str,
    operator_artifact_body: bytes | None = None,
    verified_backup: object | None = None,
    source_provenance_body: bytes | None = None,
    target_config_body: bytes | None = None,
    provisioning_receipt_body: bytes | None = None,
    forbidden_secret_values: Sequence[bytes] = (),
) -> dict[str, object]:
    """Parse a canonical probe report and reject unbound or self-inconsistent evidence."""

    if not isinstance(body, bytes) or not body or len(body) > MAX_BACKUP_RESTORE_REPORT_BYTES:
        raise ValueError("backup restore report byte length is invalid")
    if operator_artifact_body is None:
        raise ValueError("backup restore operator artifact is required")
    if verified_backup is None:
        raise ValueError("backup restore verified backup is required")
    if source_provenance_body is None:
        raise ValueError("backup restore source provenance artifact is required")
    if target_config_body is None:
        raise ValueError("backup restore target config artifact is required")
    if provisioning_receipt_body is None:
        raise ValueError("backup restore target provisioning receipt is required")
    _reject_forbidden_secret_values(body, forbidden_secret_values)
    try:
        report = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("backup restore report JSON is invalid") from None
    if not isinstance(report, dict):
        raise ValueError("backup restore report schema is invalid")
    _reject_forbidden_decoded_secret_values(report, forbidden_secret_values)
    for artifact_body in (
        operator_artifact_body,
        source_provenance_body,
        target_config_body,
        provisioning_receipt_body,
    ):
        _reject_forbidden_artifact_secret_values(
            artifact_body,
            forbidden_secret_values,
        )
    try:
        canonical = canonical_backup_restore_report(report)
    except (TypeError, ValueError):
        raise ValueError("backup restore report JSON is invalid") from None
    if body != canonical:
        raise ValueError("backup restore report must be canonical JSON")
    if set(report) != _TOP_LEVEL_FIELDS:
        raise ValueError("backup restore report schema is invalid")
    if _contains_sensitive_field(report):
        raise ValueError("backup restore report contains a sensitive field")
    if report.get("schemaVersion") != BACKUP_RESTORE_SCHEMA_VERSION:
        raise ValueError("backup restore report schema version is invalid")
    if report.get("producer") != BACKUP_RESTORE_PRODUCER:
        raise ValueError("backup restore report producer is invalid")

    _validate_candidate(candidate)
    _validate_release_run(release_run)
    if not _exact_json_equal(report.get("candidate"), dict(candidate)):
        raise ValueError("backup restore candidate provenance is invalid")
    if not _exact_json_equal(report.get("releaseRun"), dict(release_run)):
        raise ValueError("backup restore release run provenance is invalid")
    _parse_utc(report.get("observedAt"), "observation time")
    if not _exact_json_equal(report.get("consistency"), _EXPECTED_CONSISTENCY):
        raise ValueError("backup restore consistency evidence is invalid")
    if not _valid_sha256(expected_source_manifest_sha256):
        raise ValueError("backup restore expected manifest provenance is invalid")
    if not _valid_sha256(expected_source_archive_fingerprint_sha256):
        raise ValueError("backup restore expected archive provenance is invalid")

    source_provenance: dict[str, object] | None = None
    source_provenance_sha256: str | None = None
    if source_provenance_body is not None:
        try:
            source_provenance = json.loads(source_provenance_body)
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("backup restore source provenance is invalid") from None
        if (
            not isinstance(source_provenance, dict)
            or canonical_source_provenance(source_provenance) != source_provenance_body
        ):
            raise ValueError("backup restore source provenance must be canonical JSON")
        source_provenance_sha256 = hashlib.sha256(source_provenance_body).hexdigest()

    source = _validate_source(
        report.get("source"),
        expected_manifest_sha256=expected_source_manifest_sha256,
        expected_archive_fingerprint_sha256=expected_source_archive_fingerprint_sha256,
        expected_provenance_sha256=source_provenance_sha256,
    )
    if source_provenance is not None:
        validate_backup_restore_source_provenance(
            source_provenance,
            candidate=candidate,
            release_run=release_run,
            source=source,
        )
    if verified_backup is not None and not _exact_json_equal(
        source,
        _source_from_verified_backup(
            verified_backup,
            provenance_sha256=source_provenance_sha256,
        ),
    ):
        raise ValueError("backup restore source disagrees with verified backup")
    target = _validate_target(
        report.get("target"),
        source=source,
        expected_database_ownership=expected_database_ownership,
        expected_object_namespace_ownership=expected_object_namespace_ownership,
    )
    candidate_sha256 = hashlib.sha256(canonical_backup_restore_report(dict(candidate))).hexdigest()
    parse_target_provisioning_receipt(
        provisioning_receipt_body,
        provisioning_receipt_sha256=target["provisioningReceiptSha256"],
        candidate_sha256=candidate_sha256,
        release_run=release_run,
        database_disposition=expected_database_ownership,
        object_store_disposition=expected_object_namespace_ownership,
        database_identity_sha256=target["databaseIdentitySha256"],
        object_store_identity_sha256=target["objectStoreIdentitySha256"],
    )
    _validate_execution(
        report.get("execution"),
        candidate=candidate,
        release_run=release_run,
        source=source,
        target=target,
        operator_artifact_body=operator_artifact_body,
        source_provenance_sha256=source_provenance_sha256,
        target_config_body=target_config_body,
        provisioning_receipt_body=provisioning_receipt_body,
    )
    _validate_findings(report.get("findings"), source=source, target=target)
    _validate_retention(
        report.get("retention"),
        release_run=release_run,
        source=source,
        target=target,
    )
    return report


def derive_backup_restore_checks(
    report: Mapping[str, object],
) -> dict[str, bool]:
    """Derive release checks from a report that passed strict contract parsing."""

    target = report.get("target")
    execution = report.get("execution")
    findings = report.get("findings")
    if not isinstance(target, Mapping) or not isinstance(execution, Mapping):
        raise ValueError("backup restore report schema is invalid")
    if not isinstance(findings, Mapping):
        raise ValueError("backup restore report schema is invalid")
    database = findings.get("database")
    objects = findings.get("objects")
    permissions = findings.get("permissions")
    artifact_sha256s = execution.get("artifactSha256s")
    if (
        not isinstance(database, Mapping)
        or not isinstance(objects, Mapping)
        or not isinstance(permissions, Mapping)
        or not isinstance(artifact_sha256s, Mapping)
    ):
        raise ValueError("backup restore report schema is invalid")

    receipt_artifacts = (
        "sourceProvenance",
        "targetConfigSnapshot",
        "targetProvisioningReceipt",
        "restoreValidation",
    )
    return {
        "newDatabaseRestored": (
            database.get("restored") is True
            and target.get("databaseWasEmpty") is True
            and target.get("databaseDistinctFromSource") is True
            and target.get("databasePreRestoreUserObjectCount") == 0
            and isinstance(target.get("databasePostRestoreUserObjectCount"), int)
            and not isinstance(target.get("databasePostRestoreUserObjectCount"), bool)
            and target["databasePostRestoreUserObjectCount"] > 0
        ),
        "distinctVersionedBucketRestored": (
            objects.get("restored") is True
            and objects.get("createOnly") is True
            and objects.get("readbackVerified") is True
            and objects.get("contentHashesVerified") is True
            and objects.get("sourceRevisionsVerified") is True
            and objects.get("versionIdsVerified") is True
            and target.get("objectNamespaceWasEmpty") is True
            and target.get("objectNamespaceDistinctFromSource") is True
            and target.get("objectVersioningEnabled") is True
        ),
        "receiptsVerified": (
            permissions.get("verified") is True
            and all(_valid_sha256(artifact_sha256s.get(name)) for name in receipt_artifacts)
        ),
    }
