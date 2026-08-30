from __future__ import annotations

import copy
from functools import cache
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]

MANIFEST_SHA256 = "a" * 64
ARCHIVE_SHA256 = "b" * 64
SOURCE_DATABASE_IDENTITY_SHA256 = "c" * 64
SOURCE_DATABASE_SHA256 = "d" * 64
SOURCE_OBJECT_IDENTITY_SHA256 = "e" * 64
INVENTORY_SHA256 = "f" * 64
TARGET_DATABASE_IDENTITY_SHA256 = "1" * 64
RESTORE_REPORT_SHA256 = "3" * 64
PERMISSIONS_FINDING_SHA256 = "4" * 64
TARGET_OBJECT_OWNER_ID_SHA256 = "8" * 64


@cache
def _module():
    path = ROOT / "scripts" / "backup_restore_contract.py"
    assert path.is_file(), "backup/restore evidence contract is missing"
    spec = importlib.util.spec_from_file_location("backup_restore_contract_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate() -> dict[str, object]:
    return {
        "sourceRepository": "xinlingzhifei/DeepTutor",
        "sourceHead": "c" * 40,
        "releaseTag": "yfeistai-first-release-20260830-cccccccc",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": "sha256:" + "d" * 64,
            "openmaic": "sha256:" + "e" * 64,
            "openmaic_render": "sha256:" + "f" * 64,
        },
    }


def _release_run() -> dict[str, str]:
    return {
        "runId": "run-backup-restore-01",
        "environmentId": "environment-backup-restore-01",
    }


def _body(report: dict[str, object]) -> bytes:
    return (
        json.dumps(
            report, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        + "\n"
    ).encode()


def _target_object_identity_sha256() -> str:
    return _module().physical_object_store_identity_sha256(
        "https://restore-objects.internal",
        "us-east-1",
        "restore-bucket",
        TARGET_OBJECT_OWNER_ID_SHA256,
    )


def _source_provenance_body() -> bytes:
    return _body(
        {
            "schemaVersion": 1,
            "candidate": _candidate(),
            "releaseRun": _release_run(),
            "source": {
                "manifestSha256": MANIFEST_SHA256,
                "archiveFingerprintSha256": ARCHIVE_SHA256,
                "databaseIdentitySha256": SOURCE_DATABASE_IDENTITY_SHA256,
                "objectStoreIdentitySha256": SOURCE_OBJECT_IDENTITY_SHA256,
            },
        }
    )


def _target_config_body() -> bytes:
    return b"{}\n"


def _provisioning_receipt_body(
    *,
    database_ownership: str = "runner-owned-disposable",
    object_ownership: str = "runner-owned-disposable",
) -> bytes:
    release_run = _release_run()
    return _body(
        {
            "schemaVersion": 1,
            "producer": "backup-restore-target-provisioner",
            "candidateSha256": hashlib.sha256(_body(_candidate())).hexdigest(),
            "releaseRun": release_run,
            "resources": {
                "database": {
                    "identitySha256": TARGET_DATABASE_IDENTITY_SHA256,
                    "ownerRunId": release_run["runId"],
                    "disposition": database_ownership,
                },
                "objectStore": {
                    "identitySha256": _target_object_identity_sha256(),
                    "ownerRunId": release_run["runId"],
                    "disposition": object_ownership,
                },
            },
        }
    )


def _operator_target(
    *,
    database_ownership: str = "runner-owned-disposable",
    object_ownership: str = "runner-owned-disposable",
) -> dict[str, object]:
    target_config_sha256 = hashlib.sha256(_target_config_body()).hexdigest()
    provisioning_receipt_sha256 = hashlib.sha256(
        _provisioning_receipt_body(
            database_ownership=database_ownership,
            object_ownership=object_ownership,
        )
    ).hexdigest()
    object_identity = _target_object_identity_sha256()
    return {
        "targetConfigSha256": target_config_sha256,
        "provisioningReceiptSha256": provisioning_receipt_sha256,
        "database": {
            "host": "restore-db.internal",
            "port": 5432,
            "name": "restore-db-run-01",
            "identitySha256": TARGET_DATABASE_IDENTITY_SHA256,
            "ownership": database_ownership,
            "pre": {
                "identitySha256": TARGET_DATABASE_IDENTITY_SHA256,
                "userObjectCount": 0,
                "currentRole": "yfeistai_migrator",
                "owner": "yfeistai_migrator",
            },
            "post": {
                "identitySha256": TARGET_DATABASE_IDENTITY_SHA256,
                "userObjectCount": 17,
                "currentRole": "yfeistai_migrator",
                "owner": "yfeistai_migrator",
            },
        },
        "objects": {
            "endpoint": "https://restore-objects.internal",
            "region": "us-east-1",
            "namespaceId": "restore-objects-run-01",
            "bucket": "restore-bucket",
            "identitySha256": object_identity,
            "ownership": object_ownership,
            "pre": {
                "identitySha256": object_identity,
                "versioningEnabled": True,
                "objectCount": 0,
                "versionCount": 0,
                "deleteMarkerCount": 0,
                "ownerIdSha256": TARGET_OBJECT_OWNER_ID_SHA256,
            },
            "post": {
                "identitySha256": object_identity,
                "versioningEnabled": True,
                "objectCount": 2,
                "versionCount": 2,
                "deleteMarkerCount": 0,
                "ownerIdSha256": TARGET_OBJECT_OWNER_ID_SHA256,
            },
        },
        "concurrencyExclusion": {
            "mode": "postgresql-session-advisory-lock",
            "identitySha256": TARGET_DATABASE_IDENTITY_SHA256,
            "heldThroughPostValidation": True,
        },
    }


def _command(
    *,
    database_ownership: str = "runner-owned-disposable",
    object_ownership: str = "runner-owned-disposable",
) -> dict[str, object]:
    target_config_sha256 = hashlib.sha256(_target_config_body()).hexdigest()
    provisioning_receipt_sha256 = hashlib.sha256(
        _provisioning_receipt_body(
            database_ownership=database_ownership,
            object_ownership=object_ownership,
        )
    ).hexdigest()
    return {
        "sequence": 1,
        "name": "restore-and-verify",
        "argv": [
            "python",
            "scripts/restore_teaching_validation.py",
            "--backup-dir",
            "C:/evidence/source-backup",
            "--target-config",
            "C:/evidence/run/target-config.snapshot.json",
            "--target-config-sha256",
            target_config_sha256,
            "--provisioning-receipt",
            "C:/evidence/run/target-provisioning-receipt.json",
            "--provisioning-receipt-sha256",
            provisioning_receipt_sha256,
            "--target-secret-dir",
            "C:/secrets/restore",
            "--run-id",
            "run-backup-restore-01",
            "--environment-id",
            "environment-backup-restore-01",
            "--candidate-sha256",
            hashlib.sha256(_body(_candidate())).hexdigest(),
            "--report",
            "C:/evidence/run/restore-validation.json",
            "--pg-restore",
            "C:/PostgreSQL/bin/pg_restore.exe",
            "--database-ownership",
            database_ownership,
            "--object-namespace-ownership",
            object_ownership,
            "--deadline-monotonic",
            "1000.0",
        ],
        "nativeExit": 0,
        "startedAt": "2026-08-30T00:00:00Z",
        "finishedAt": "2026-08-30T00:00:02Z",
        "durationMs": 2000,
        "stdoutSha256": "5" * 64,
        "stderrSha256": "6" * 64,
        "artifact": "restore-validation.json",
        "artifactSha256": RESTORE_REPORT_SHA256,
    }


def _report(
    *,
    database_ownership: str = "runner-owned-disposable",
    object_ownership: str = "runner-owned-disposable",
) -> dict[str, object]:
    source_provenance_sha256 = hashlib.sha256(_source_provenance_body()).hexdigest()
    target_config_sha256 = hashlib.sha256(_target_config_body()).hexdigest()
    provisioning_receipt_sha256 = hashlib.sha256(
        _provisioning_receipt_body(
            database_ownership=database_ownership,
            object_ownership=object_ownership,
        )
    ).hexdigest()
    operator_target = _operator_target(
        database_ownership=database_ownership,
        object_ownership=object_ownership,
    )
    object_identity_sha256 = _target_object_identity_sha256()
    report = {
        "schemaVersion": 1,
        "producer": "backup-restore-probe",
        "candidate": _candidate(),
        "releaseRun": _release_run(),
        "observedAt": "2026-08-30T00:00:03Z",
        "consistency": {
            "databaseSnapshot": "postgresql-consistent-dump",
            "objectSnapshot": "version-pinned-inventory",
            "crossSystemAtomic": False,
            "partialBackupArtifacts": "retained",
        },
        "source": {
            "manifestSha256": MANIFEST_SHA256,
            "archiveFingerprintSha256": ARCHIVE_SHA256,
            "databaseIdentitySha256": SOURCE_DATABASE_IDENTITY_SHA256,
            "databaseSha256": SOURCE_DATABASE_SHA256,
            "objectStoreIdentitySha256": SOURCE_OBJECT_IDENTITY_SHA256,
            "objectInventorySha256": INVENTORY_SHA256,
            "platformSchemaRevision": "20260830_0023",
            "schemaRevisions": {"tenant-a": "20260830_0023"},
            "classroomVersionsCount": 3,
            "learningEventsCount": 7,
            "objectCount": 2,
            "provenanceSha256": source_provenance_sha256,
        },
        "target": {
            "databaseId": "restore-db-run-01",
            "databaseHost": "restore-db.internal",
            "databasePort": 5432,
            "databaseIdentitySha256": TARGET_DATABASE_IDENTITY_SHA256,
            "databaseOwnership": database_ownership,
            "databaseWasEmpty": True,
            "databaseDistinctFromSource": True,
            "databaseCurrentRole": "yfeistai_migrator",
            "databaseOwner": "yfeistai_migrator",
            "databasePreRestoreUserObjectCount": 0,
            "databasePostRestoreUserObjectCount": 17,
            "objectNamespaceId": "restore-objects-run-01:restore-bucket",
            "objectEndpoint": "https://restore-objects.internal",
            "objectRegion": "us-east-1",
            "objectNamespace": "restore-objects-run-01",
            "objectBucket": "restore-bucket",
            "objectOwnerIdSha256": TARGET_OBJECT_OWNER_ID_SHA256,
            "objectStoreIdentitySha256": object_identity_sha256,
            "objectNamespaceOwnership": object_ownership,
            "objectNamespaceWasEmpty": True,
            "objectNamespaceDistinctFromSource": True,
            "objectVersioningEnabled": True,
            "objectPreRestoreObjectCount": 0,
            "objectPostRestoreObjectCount": 2,
            "objectPreRestoreVersionCount": 0,
            "objectPostRestoreVersionCount": 2,
            "objectPreRestoreDeleteMarkerCount": 0,
            "objectPostRestoreDeleteMarkerCount": 0,
            "concurrencyExclusionMode": "postgresql-session-advisory-lock",
            "concurrencyExclusionIdentitySha256": TARGET_DATABASE_IDENTITY_SHA256,
            "concurrencyExclusionHeldThroughPostValidation": True,
            "operatorTargetObservationsSha256": hashlib.sha256(_body(operator_target)).hexdigest(),
            "targetConfigSha256": target_config_sha256,
            "provisioningReceiptSha256": provisioning_receipt_sha256,
        },
        "execution": {
            "commands": [
                _command(
                    database_ownership=database_ownership,
                    object_ownership=object_ownership,
                )
            ],
            "artifactSha256s": {
                "sourceManifest": MANIFEST_SHA256,
                "sourceObjectInventory": INVENTORY_SHA256,
                "sourceDatabaseDump": SOURCE_DATABASE_SHA256,
                "sourceProvenance": source_provenance_sha256,
                "targetConfigSnapshot": target_config_sha256,
                "targetProvisioningReceipt": provisioning_receipt_sha256,
                "restoreValidation": RESTORE_REPORT_SHA256,
            },
        },
        "findings": {
            "database": {
                "restored": True,
                "dumpRestoreSingleTransaction": True,
                "postRestoreMutationsAtomic": False,
                "sourceDatabaseSha256": SOURCE_DATABASE_SHA256,
                "platformSchemaRevision": "20260830_0023",
                "schemaRevisions": {"tenant-a": "20260830_0023"},
                "classroomVersionsCount": 3,
                "learningEventsCount": 7,
                "preRestoreUserObjectCount": 0,
                "postRestoreUserObjectCount": 17,
            },
            "objects": {
                "restored": True,
                "createOnly": True,
                "readbackVerified": True,
                "objectCount": 2,
                "inventorySha256": INVENTORY_SHA256,
                "contentHashesVerified": True,
                "sourceRevisionsVerified": True,
                "versionIdsVerified": True,
                "preRestoreObjectCount": 0,
                "postRestoreObjectCount": 2,
                "preRestoreVersionCount": 0,
                "postRestoreVersionCount": 2,
                "preRestoreDeleteMarkerCount": 0,
                "postRestoreDeleteMarkerCount": 0,
            },
            "permissions": {
                "role": "yfeistai_app",
                "verified": True,
                "findingSha256": PERMISSIONS_FINDING_SHA256,
            },
        },
        "retention": {
            "policy": "no-destructive-cleanup",
            "cleanupAttempted": False,
            "fullCleanupClaimed": False,
            "targets": [
                {
                    "kind": "source-backup",
                    "id": MANIFEST_SHA256,
                    "ownership": "retained-audit",
                },
                {
                    "kind": "database",
                    "id": "restore-db-run-01",
                    "ownership": database_ownership,
                },
                {
                    "kind": "object-namespace",
                    "id": "restore-objects-run-01:restore-bucket",
                    "ownership": object_ownership,
                },
                {
                    "kind": "report",
                    "id": "run-backup-restore-01",
                    "ownership": "retained-audit",
                },
            ],
        },
    }
    _bind_operator_artifact(
        report,
        _operator_artifact_body(
            database_ownership=database_ownership,
            object_ownership=object_ownership,
        ),
    )
    return report


def _operator_artifact_body(
    *,
    database_ownership: str = "runner-owned-disposable",
    object_ownership: str = "runner-owned-disposable",
) -> bytes:
    return _body(
        {
            "schemaVersion": 3,
            "runId": "run-backup-restore-01",
            "ok": True,
            "targetDatabaseIdentitySha256": TARGET_DATABASE_IDENTITY_SHA256,
            "objectPrefix": "",
            "validated": [
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
            ],
            "failures": [],
            "sourceArchive": {
                "archiveFingerprintSha256": ARCHIVE_SHA256,
                "manifestSha256": MANIFEST_SHA256,
            },
            "database": {
                "dumpRestoreSingleTransaction": True,
                "postRestoreMutationsAtomic": False,
            },
            "objects": {
                "createOnly": True,
                "isolation": "empty_target_bucket",
                "readbackVerified": True,
                "restoredCount": 2,
                "targetBucket": "restore-bucket",
            },
            "crossSystemAtomic": False,
            "target": _operator_target(
                database_ownership=database_ownership,
                object_ownership=object_ownership,
            ),
        }
    )


def _verified_backup() -> SimpleNamespace:
    return SimpleNamespace(
        manifest=SimpleNamespace(
            database=SimpleNamespace(identity_sha256=SOURCE_DATABASE_IDENTITY_SHA256),
            source_object_store_identity_sha256=SOURCE_OBJECT_IDENTITY_SHA256,
            platform_schema_revision="20260830_0023",
            schema_revisions={"tenant-a": "20260830_0023"},
            classroom_versions_count=3,
            learning_events_count=7,
            object_count=2,
        ),
        manifest_sha256=MANIFEST_SHA256,
        archive_fingerprint_sha256=ARCHIVE_SHA256,
        object_inventory_sha256=INVENTORY_SHA256,
        database_sha256=SOURCE_DATABASE_SHA256,
    )


def _bind_operator_artifact(report: dict[str, object], body: bytes) -> None:
    digest = hashlib.sha256(body).hexdigest()
    report["execution"]["commands"][0]["artifactSha256"] = digest
    report["execution"]["artifactSha256s"]["restoreValidation"] = digest


def _parse(report: dict[str, object], **overrides: object) -> dict[str, object]:
    database_ownership = report["target"]["databaseOwnership"]
    object_ownership = report["target"]["objectNamespaceOwnership"]
    arguments: dict[str, object] = {
        "candidate": _candidate(),
        "release_run": _release_run(),
        "expected_source_manifest_sha256": MANIFEST_SHA256,
        "expected_source_archive_fingerprint_sha256": ARCHIVE_SHA256,
        "expected_database_ownership": database_ownership,
        "expected_object_namespace_ownership": object_ownership,
        "operator_artifact_body": _operator_artifact_body(
            database_ownership=database_ownership,
            object_ownership=object_ownership,
        ),
        "verified_backup": _verified_backup(),
        "source_provenance_body": _source_provenance_body(),
        "target_config_body": _target_config_body(),
        "provisioning_receipt_body": _provisioning_receipt_body(
            database_ownership=database_ownership,
            object_ownership=object_ownership,
        ),
        "forbidden_secret_values": (b"platform-admin-secret",),
    }
    arguments.update(overrides)
    return _module().parse_backup_restore_report(_body(report), **arguments)


def test_accepts_candidate_bound_non_atomic_restore_with_exact_provenance() -> None:
    report = _report()
    operator_artifact = _operator_artifact_body()
    _bind_operator_artifact(report, operator_artifact)

    parsed = _parse(report, operator_artifact_body=operator_artifact)

    assert parsed == report
    assert _module().backup_restore_command_record() == {
        "runner": "python",
        "script": "scripts/backup_restore_probe.py",
        "arguments": ["--profile", "first-release"],
    }


def test_rejects_legacy_transaction_and_atomicity_field_names() -> None:
    report = _report()
    report["consistency"]["globallyAtomic"] = report["consistency"].pop("crossSystemAtomic")
    with pytest.raises(ValueError, match="consistency"):
        _parse(report)

    report = _report()
    database_findings = report["findings"]["database"]
    database_findings["singleTransaction"] = database_findings.pop("dumpRestoreSingleTransaction")
    with pytest.raises(ValueError, match="database"):
        _parse(report)

    report = _report()
    operator_artifact = json.loads(_operator_artifact_body())
    operator_artifact["database"] = {"singleTransaction": True}
    operator_body = _body(operator_artifact)
    _bind_operator_artifact(report, operator_body)
    with pytest.raises(ValueError, match="operator.*findings"):
        _parse(report, operator_artifact_body=operator_body)


def test_rejects_schema_v2_operator_with_rich_outer_target() -> None:
    report = _report()
    operator_artifact = json.loads(_operator_artifact_body())
    operator_artifact["schemaVersion"] = 2
    operator_artifact.pop("target")
    operator_body = _body(operator_artifact)
    _bind_operator_artifact(report, operator_body)

    with pytest.raises(ValueError, match="operator artifact schema version"):
        _parse(report, operator_artifact_body=operator_body)


def test_rejects_rehashed_operator_artifact_with_mismatched_target_identity() -> None:
    report = _report()
    operator_artifact = json.loads(_operator_artifact_body())
    operator_artifact["targetDatabaseIdentitySha256"] = "9" * 64
    operator_body = _body(operator_artifact)
    _bind_operator_artifact(report, operator_body)

    with pytest.raises(ValueError, match="operator.*target|target.*operator"):
        _parse(report, operator_artifact_body=operator_body)


def test_rejects_self_consistent_source_claim_that_disagrees_with_verified_backup() -> None:
    report = _report()
    report["source"]["databaseSha256"] = "9" * 64
    report["execution"]["artifactSha256s"]["sourceDatabaseDump"] = "9" * 64
    report["findings"]["database"]["sourceDatabaseSha256"] = "9" * 64
    operator_body = _operator_artifact_body()
    _bind_operator_artifact(report, operator_body)

    with pytest.raises(ValueError, match="verified backup"):
        _parse(
            report,
            operator_artifact_body=operator_body,
            verified_backup=_verified_backup(),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda report: report["candidate"].__setitem__("sourceHead", "9" * 40),
            "candidate",
        ),
        (
            lambda report: report["releaseRun"].__setitem__("runId", "other-run"),
            "release run",
        ),
        (
            lambda report: report["source"].__setitem__("manifestSha256", "9" * 64),
            "manifest",
        ),
        (
            lambda report: report["source"].__setitem__("archiveFingerprintSha256", "9" * 64),
            "archive",
        ),
    ],
)
def test_rejects_missing_or_mismatched_candidate_and_source_provenance(
    mutation,
    message: str,
) -> None:
    report = _report()
    mutation(report)

    with pytest.raises(ValueError, match=message):
        _parse(report)


@pytest.mark.parametrize(
    "field",
    [
        "databaseWasEmpty",
        "databaseDistinctFromSource",
        "objectNamespaceWasEmpty",
        "objectNamespaceDistinctFromSource",
        "objectVersioningEnabled",
    ],
)
def test_rejects_target_that_is_not_distinct_empty_and_versioned(field: str) -> None:
    report = _report()
    report["target"][field] = False

    with pytest.raises(ValueError, match="target"):
        _parse(report)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("database", "classroomVersionsCount", 4),
        ("database", "learningEventsCount", 8),
        ("database", "platformSchemaRevision", "wrong"),
        ("objects", "objectCount", 3),
        ("objects", "inventorySha256", "9" * 64),
        ("objects", "contentHashesVerified", False),
        ("objects", "sourceRevisionsVerified", False),
        ("objects", "versionIdsVerified", False),
        ("permissions", "verified", False),
    ],
)
def test_rejects_mismatched_database_object_count_hash_revision_or_permission_finding(
    section: str,
    field: str,
    value: object,
) -> None:
    report = _report()
    report["findings"][section][field] = value

    with pytest.raises(ValueError, match=section):
        _parse(report)


def test_rejects_global_atomicity_and_cleanup_claims_for_retained_targets() -> None:
    report = _report(
        database_ownership="retained-audit",
        object_ownership="retained-audit",
    )
    report["consistency"]["crossSystemAtomic"] = True

    with pytest.raises(ValueError, match="consistency"):
        _parse(report)

    report = _report(
        database_ownership="retained-audit",
        object_ownership="retained-audit",
    )
    report["retention"]["fullCleanupClaimed"] = True

    with pytest.raises(ValueError, match="retention"):
        _parse(report)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nativeExit", 1),
        ("durationMs", 0),
        ("artifactSha256", "9" * 64),
        ("argv", ["python", "restore.py", "--password", "platform-admin-secret"]),
    ],
)
def test_rejects_failed_incomplete_or_secret_bearing_command_evidence(
    field: str,
    value: object,
) -> None:
    report = _report()
    report["execution"]["commands"][0][field] = value

    with pytest.raises(ValueError, match="command|operator|secret"):
        _parse(report)


def test_accepts_only_canonical_json_and_exact_schema() -> None:
    report = _report()
    noncanonical = (json.dumps(report, indent=2) + "\n").encode()

    with pytest.raises(ValueError, match="canonical"):
        _module().parse_backup_restore_report(
            noncanonical,
            candidate=_candidate(),
            release_run=_release_run(),
            expected_source_manifest_sha256=MANIFEST_SHA256,
            expected_source_archive_fingerprint_sha256=ARCHIVE_SHA256,
            expected_database_ownership="runner-owned-disposable",
            expected_object_namespace_ownership="runner-owned-disposable",
            operator_artifact_body=_operator_artifact_body(),
            verified_backup=_verified_backup(),
            source_provenance_body=_source_provenance_body(),
            target_config_body=_target_config_body(),
            provisioning_receipt_body=_provisioning_receipt_body(),
            forbidden_secret_values=(b"platform-admin-secret",),
        )

    extra = copy.deepcopy(report)
    extra["unexpected"] = True
    with pytest.raises(ValueError, match="schema"):
        _parse(extra)


def test_rejects_secret_values_anywhere_in_the_report() -> None:
    report = _report()
    report["execution"]["commands"][0]["argv"].append("platform-admin-secret")

    with pytest.raises(ValueError, match="secret"):
        _parse(report)


def test_rejects_escaped_secret_in_decoded_json_string() -> None:
    secret_text = 'operator-"quoted"-secret'
    secret_value = secret_text.encode("utf-8")
    report = _report()
    report["execution"]["commands"][0]["argv"][3] = f"C:/evidence/{secret_text}"
    assert secret_value not in _body(report)

    with pytest.raises(ValueError, match="secret"):
        _parse(report, forbidden_secret_values=(secret_value,))
