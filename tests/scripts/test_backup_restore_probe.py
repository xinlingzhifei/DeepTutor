from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
import copy
from dataclasses import replace
from datetime import datetime, timezone
from functools import cache
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

from deeptutor.teaching.secret_permissions import secret_file_is_restricted

ROOT = Path(__file__).resolve().parents[2]
SECRET_VALUE = b"restore-password-must-never-appear"
TARGET_SECRET_NAMES = (
    "platform_database_app_password",
    "platform_database_migration_password",
    "minio_bootstrap_access_key",
    "minio_bootstrap_secret_key",
)

MANIFEST_SHA256 = "a" * 64
ARCHIVE_SHA256 = "b" * 64
SOURCE_DATABASE_IDENTITY_SHA256 = "c" * 64
SOURCE_DATABASE_SHA256 = "d" * 64
SOURCE_OBJECT_IDENTITY_SHA256 = "e" * 64
INVENTORY_SHA256 = "f" * 64
TARGET_DATABASE_IDENTITY_SHA256 = "1" * 64


def _write_target_secrets(directory: Path) -> None:
    for name in TARGET_SECRET_NAMES:
        path = directory / name
        path.write_bytes(SECRET_VALUE)
        path.chmod(0o600)


@cache
def _module():
    path = ROOT / "scripts" / "backup_restore_probe.py"
    assert path.is_file(), "backup/restore evidence probe is missing"
    spec = importlib.util.spec_from_file_location("backup_restore_probe_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@cache
def _contract():
    path = ROOT / "scripts" / "backup_restore_contract.py"
    spec = importlib.util.spec_from_file_location("backup_restore_contract_for_probe_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@cache
def _restore_module():
    path = ROOT / "scripts" / "restore_teaching_validation.py"
    spec = importlib.util.spec_from_file_location("restore_teaching_for_probe_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@cache
def _backup_module():
    path = ROOT / "scripts" / "backup_teaching.py"
    spec = importlib.util.spec_from_file_location("backup_teaching_for_restore_probe_test", path)
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


def _provisioning_receipt(
    database_identity_sha256: str,
    object_store_identity_sha256: str,
) -> dict[str, object]:
    candidate_sha256 = hashlib.sha256(_canonical(_candidate())).hexdigest()
    release_run = _release_run()
    return {
        "schemaVersion": 1,
        "producer": "backup-restore-target-provisioner",
        "candidateSha256": candidate_sha256,
        "releaseRun": release_run,
        "resources": {
            "database": {
                "identitySha256": database_identity_sha256,
                "ownerRunId": release_run["runId"],
                "disposition": "runner-owned-disposable",
            },
            "objectStore": {
                "identitySha256": object_store_identity_sha256,
                "ownerRunId": release_run["runId"],
                "disposition": "runner-owned-disposable",
            },
        },
    }


def _source_provenance() -> dict[str, object]:
    return {
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


def _fake_backup(backup_dir: Path) -> SimpleNamespace:
    database_dump = backup_dir / "database.dump"
    database_dump.write_bytes(b"database dump")
    manifest = SimpleNamespace(
        database=SimpleNamespace(identity_sha256=SOURCE_DATABASE_IDENTITY_SHA256),
        source_object_store_identity_sha256=SOURCE_OBJECT_IDENTITY_SHA256,
        object_inventory_sha256=INVENTORY_SHA256,
        platform_schema_revision="20260830_0023",
        schema_revisions={"tenant-a": "20260830_0023"},
        classroom_versions_count=3,
        learning_events_count=7,
        object_count=2,
    )
    return SimpleNamespace(
        directory=backup_dir,
        database_dump=database_dump,
        manifest=manifest,
        manifest_sha256=MANIFEST_SHA256,
        archive_fingerprint_sha256=ARCHIVE_SHA256,
        object_inventory_sha256=INVENTORY_SHA256,
        database_sha256=SOURCE_DATABASE_SHA256,
    )


def _snapshot_backup(backup: SimpleNamespace, snapshot_directory: Path) -> SimpleNamespace:
    current = copy.deepcopy(backup)
    current.directory = Path(snapshot_directory).resolve()
    current.database_dump = current.directory / "database.dump"
    return current


def _canonical(payload: object) -> bytes:
    return (
        json.dumps(
            payload, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        + "\n"
    ).encode()


def _restore_payload(*, target_bucket: str = "restore-bucket") -> dict[str, object]:
    target_config_sha256 = hashlib.sha256(b"{}\n").hexdigest()
    target_namespace = "restore-objects-run-01"
    target_object_identity = _module().physical_object_store_identity_sha256(
        "https://restore-objects.internal",
        "us-east-1",
        target_bucket,
        "8" * 64,
    )
    provisioning_receipt_sha256 = hashlib.sha256(
        _canonical(
            _provisioning_receipt(
                TARGET_DATABASE_IDENTITY_SHA256,
                target_object_identity,
            )
        )
    ).hexdigest()
    return {
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
            "targetBucket": target_bucket,
        },
        "crossSystemAtomic": False,
        "target": {
            "targetConfigSha256": target_config_sha256,
            "provisioningReceiptSha256": provisioning_receipt_sha256,
            "database": {
                "host": "restore-db.internal",
                "port": 5432,
                "name": "restore-db-run-01",
                "identitySha256": TARGET_DATABASE_IDENTITY_SHA256,
                "ownership": "runner-owned-disposable",
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
                "namespaceId": target_namespace,
                "bucket": target_bucket,
                "identitySha256": target_object_identity,
                "ownership": "runner-owned-disposable",
                "pre": {
                    "identitySha256": target_object_identity,
                    "versioningEnabled": True,
                    "objectCount": 0,
                    "versionCount": 0,
                    "deleteMarkerCount": 0,
                    "ownerIdSha256": "8" * 64,
                },
                "post": {
                    "identitySha256": target_object_identity,
                    "versioningEnabled": True,
                    "objectCount": 2,
                    "versionCount": 2,
                    "deleteMarkerCount": 0,
                    "ownerIdSha256": "8" * 64,
                },
            },
            "concurrencyExclusion": {
                "mode": "postgresql-session-advisory-lock",
                "identitySha256": TARGET_DATABASE_IDENTITY_SHA256,
                "heldThroughPostValidation": True,
            },
        },
    }


def _fixture(tmp_path: Path):
    backup_dir = tmp_path / "source-backup"
    backup_dir.mkdir()
    backup = _fake_backup(backup_dir)
    target_config = tmp_path / "target.json"
    target_config.write_text("{}", encoding="utf-8")
    target_secrets = tmp_path / "target-secrets"
    target_secrets.mkdir()
    _write_target_secrets(target_secrets)
    pg_restore = tmp_path / "pg_restore"
    pg_restore.write_bytes(b"operator binary")
    output_dir = tmp_path / "evidence" / "backup-restore-run-01"
    output_dir.parent.mkdir()
    target = SimpleNamespace(
        database_host="restore-db.internal",
        database_port=5432,
        database_name="restore-db-run-01",
        database_user="yfeistai_migrator",
        object_store_endpoint="https://restore-objects.internal",
        object_store_region="us-east-1",
        object_store_namespace_id="restore-objects-run-01",
        object_store_bucket="restore-bucket",
    )
    target_object_identity = _module().physical_object_store_identity_sha256(
        target.object_store_endpoint,
        target.object_store_region,
        target.object_store_bucket,
        "8" * 64,
    )
    provisioning_receipt = tmp_path / "target-provisioning-receipt.json"
    provisioning_receipt.write_bytes(
        _canonical(
            _provisioning_receipt(
                TARGET_DATABASE_IDENTITY_SHA256,
                target_object_identity,
            )
        )
    )
    config = _module().BackupRestoreProbeConfig(
        candidate=_candidate(),
        release_run=_release_run(),
        source_provenance=_source_provenance(),
        backup_directory=backup_dir,
        target_config_path=target_config,
        provisioning_receipt_path=provisioning_receipt,
        target_secret_directory=target_secrets,
        output_directory=output_dir,
        python_executable=Path(sys.executable),
        pg_restore_executable=pg_restore,
        database_ownership="runner-owned-disposable",
        object_namespace_ownership="runner-owned-disposable",
        timeout_seconds=300,
        forbidden_secret_values=(SECRET_VALUE,),
    )
    return config, backup, target


def write_release_probe_fixture(
    bundle_root: Path,
    *,
    candidate: dict[str, object],
    release_run: dict[str, str],
) -> Path:
    """Create one real-archive, mock-operator backup/restore proof bundle."""

    probe = _module()
    backup_module = _backup_module()
    root = Path(bundle_root)
    attempt = uuid.uuid4().hex
    inputs = root / "test-inputs" / f"backup-restore-{attempt}"
    inputs.mkdir(parents=True)

    backup_directory = inputs / "source-backup"
    backup_directory.mkdir()
    database_dump = backup_directory / "database.dump"
    database_dump.write_bytes(b"release backup database")
    object_key = "tenants/tenant-a/classrooms/release/document.json"
    object_body = b"release backup object"
    payload_file = f"objects/{hashlib.sha256(object_key.encode()).hexdigest()}.blob"
    payload_path = backup_directory / payload_file
    payload_path.parent.mkdir()
    payload_path.write_bytes(object_body)
    inventory = (
        backup_module.RestorableObjectInventoryEntry(
            tenant_id="tenant-a",
            key=object_key,
            sha256=hashlib.sha256(object_body).hexdigest(),
            size=len(object_body),
            version_id="release-object-version-1",
            payload_file=payload_file,
            content_type="application/json",
            owner_token="1" * 32,
            source_revision="release-source-revision-1",
        ),
    )
    backup_module.write_backup_manifest(
        backup_directory,
        database_dump=database_dump,
        database_identity_sha256="a" * 64,
        object_inventory=inventory,
        source_object_store_namespace_id="release-source-objects",
        source_object_store_bucket="release-source-bucket",
        source_object_store_identity_sha256="b" * 64,
        platform_schema_revision="20260830_0023",
        schema_revisions={"tenant-a": "20260830_0023"},
        classroom_versions_count=3,
        learning_events_count=7,
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    verified_backup = backup_module.load_verified_backup(backup_directory)

    target_config = inputs / "target.json"
    target_config.write_bytes(b"{}\n")
    target_secrets = inputs / "target-secrets"
    target_secrets.mkdir(mode=0o700)
    _write_target_secrets(target_secrets)
    pg_restore = inputs / "pg_restore"
    pg_restore.write_bytes(b"operator binary")
    pg_restore.chmod(0o700)

    target = SimpleNamespace(
        database_host="restore-db.internal",
        database_port=5432,
        database_name="restore-db-run-01",
        database_user="yfeistai_migrator",
        object_store_endpoint="https://restore-objects.internal",
        object_store_region="us-east-1",
        object_store_namespace_id="restore-objects-run-01",
        object_store_bucket="restore-bucket",
    )
    target_database_identity = "c" * 64
    target_owner_id_sha256 = "8" * 64
    target_object_identity = probe.physical_object_store_identity_sha256(
        target.object_store_endpoint,
        target.object_store_region,
        target.object_store_bucket,
        target_owner_id_sha256,
    )
    provisioning_receipt = {
        "schemaVersion": 1,
        "producer": "backup-restore-target-provisioner",
        "candidateSha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
        "releaseRun": release_run,
        "resources": {
            "database": {
                "identitySha256": target_database_identity,
                "ownerRunId": release_run["runId"],
                "disposition": "runner-owned-disposable",
            },
            "objectStore": {
                "identitySha256": target_object_identity,
                "ownerRunId": release_run["runId"],
                "disposition": "runner-owned-disposable",
            },
        },
    }
    provisioning_receipt_path = inputs / "target-provisioning-receipt.json"
    provisioning_receipt_path.write_bytes(_canonical(provisioning_receipt))
    source_provenance = {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": release_run,
        "source": {
            "manifestSha256": verified_backup.manifest_sha256,
            "archiveFingerprintSha256": verified_backup.archive_fingerprint_sha256,
            "databaseIdentitySha256": verified_backup.manifest.database.identity_sha256,
            "objectStoreIdentitySha256": (
                verified_backup.manifest.source_object_store_identity_sha256
            ),
        },
    }

    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    output_directory = runtime / "backup-restore"
    if output_directory.exists():
        output_directory.replace(runtime / f"backup-restore.previous-{attempt}")
    config = probe.BackupRestoreProbeConfig(
        candidate=candidate,
        release_run=release_run,
        source_provenance=source_provenance,
        backup_directory=backup_directory,
        target_config_path=target_config,
        provisioning_receipt_path=provisioning_receipt_path,
        target_secret_directory=target_secrets,
        output_directory=output_directory,
        python_executable=Path(sys.executable),
        pg_restore_executable=pg_restore,
        database_ownership="runner-owned-disposable",
        object_namespace_ownership="runner-owned-disposable",
        timeout_seconds=300,
        forbidden_secret_values=(SECRET_VALUE,),
    )
    target_config_sha256 = hashlib.sha256(target_config.read_bytes()).hexdigest()
    provisioning_receipt_sha256 = hashlib.sha256(provisioning_receipt_path.read_bytes()).hexdigest()
    object_count = verified_backup.manifest.object_count

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        payload = {
            "schemaVersion": 3,
            "runId": release_run["runId"],
            "ok": True,
            "targetDatabaseIdentitySha256": target_database_identity,
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
                "archiveFingerprintSha256": verified_backup.archive_fingerprint_sha256,
                "manifestSha256": verified_backup.manifest_sha256,
            },
            "database": {
                "dumpRestoreSingleTransaction": True,
                "postRestoreMutationsAtomic": False,
            },
            "objects": {
                "createOnly": True,
                "isolation": "empty_target_bucket",
                "readbackVerified": True,
                "restoredCount": object_count,
                "targetBucket": target.object_store_bucket,
            },
            "crossSystemAtomic": False,
            "target": {
                "targetConfigSha256": target_config_sha256,
                "provisioningReceiptSha256": provisioning_receipt_sha256,
                "database": {
                    "host": target.database_host,
                    "port": target.database_port,
                    "name": target.database_name,
                    "identitySha256": target_database_identity,
                    "ownership": "runner-owned-disposable",
                    "pre": {
                        "identitySha256": target_database_identity,
                        "userObjectCount": 0,
                        "currentRole": "yfeistai_migrator",
                        "owner": "yfeistai_migrator",
                    },
                    "post": {
                        "identitySha256": target_database_identity,
                        "userObjectCount": 17,
                        "currentRole": "yfeistai_migrator",
                        "owner": "yfeistai_migrator",
                    },
                },
                "objects": {
                    "endpoint": target.object_store_endpoint,
                    "region": target.object_store_region,
                    "namespaceId": target.object_store_namespace_id,
                    "bucket": target.object_store_bucket,
                    "identitySha256": target_object_identity,
                    "ownership": "runner-owned-disposable",
                    "pre": {
                        "identitySha256": target_object_identity,
                        "versioningEnabled": True,
                        "objectCount": 0,
                        "versionCount": 0,
                        "deleteMarkerCount": 0,
                        "ownerIdSha256": target_owner_id_sha256,
                    },
                    "post": {
                        "identitySha256": target_object_identity,
                        "versioningEnabled": True,
                        "objectCount": object_count,
                        "versionCount": object_count,
                        "deleteMarkerCount": 0,
                        "ownerIdSha256": target_owner_id_sha256,
                    },
                },
                "concurrencyExclusion": {
                    "mode": "postgresql-session-advisory-lock",
                    "identitySha256": target_database_identity,
                    "heldThroughPostValidation": True,
                },
            },
        }
        _write_restore_report(arguments, payload)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"restore ok\n", stderr=b"")

    times = iter(
        [
            datetime(2026, 8, 23, 23, 59, 58, tzinfo=timezone.utc),
            datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc),
        ]
    )
    monotonic = iter([10.0, 12.0])
    return probe.run_backup_restore_probe(
        config,
        target_config_loader=lambda path: target,
        command_runner=runner,
        now=lambda: next(times),
        monotonic=lambda: next(monotonic),
    )


def _write_restore_report(arguments: list[str], payload: dict[str, object]) -> Path:
    report_path = Path(arguments[arguments.index("--report") + 1])
    report_path.write_bytes(_canonical(payload))
    return report_path


def test_probe_emits_canonical_candidate_bound_report_without_secret_or_cleanup(
    tmp_path: Path,
) -> None:
    module = _module()
    config, backup, target = _fixture(tmp_path)
    calls: list[list[str]] = []

    def runner(arguments: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        assert options["check"] is False
        assert options["capture_output"] is True
        assert options["deadline_monotonic"] == 310.0
        deadline_index = arguments.index("--deadline-monotonic")
        assert float(arguments[deadline_index + 1]) == options["deadline_monotonic"]
        assert SECRET_VALUE.decode() not in "\n".join(arguments)
        _write_restore_report(arguments, _restore_payload())
        return subprocess.CompletedProcess(arguments, 0, stdout=b"restore ok\n", stderr=b"")

    times = iter(
        [
            datetime(2026, 8, 30, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 30, 0, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 30, 0, 0, 3, tzinfo=timezone.utc),
        ]
    )
    monotonic = iter([10.0, 12.0])

    report_path = module.run_backup_restore_probe(
        config,
        verified_backup_loader=lambda path: _snapshot_backup(backup, path),
        verified_backup_rechecker=lambda current: current,
        target_config_loader=lambda path: target,
        command_runner=runner,
        now=lambda: next(times),
        monotonic=lambda: next(monotonic),
    )

    assert len(calls) == 1
    body = report_path.read_bytes()
    assert SECRET_VALUE not in body
    parsed = _contract().parse_backup_restore_report(
        body,
        candidate=_candidate(),
        release_run=_release_run(),
        expected_source_manifest_sha256=MANIFEST_SHA256,
        expected_source_archive_fingerprint_sha256=ARCHIVE_SHA256,
        expected_database_ownership="runner-owned-disposable",
        expected_object_namespace_ownership="runner-owned-disposable",
        operator_artifact_body=(config.output_directory / "restore-validation.json").read_bytes(),
        verified_backup=backup,
        source_provenance_body=_canonical(_source_provenance()),
        target_config_body=b"{}\n",
        provisioning_receipt_body=config.provisioning_receipt_path.read_bytes(),
        forbidden_secret_values=(SECRET_VALUE,),
    )
    assert parsed["consistency"]["crossSystemAtomic"] is False
    assert parsed["retention"]["cleanupAttempted"] is False
    assert parsed["retention"]["fullCleanupClaimed"] is False
    assert parsed["findings"]["database"]["classroomVersionsCount"] == 3
    assert parsed["findings"]["objects"]["sourceRevisionsVerified"] is True
    assert parsed["findings"]["permissions"]["verified"] is True
    command = parsed["execution"]["commands"][0]
    assert command["argv"] == calls[0]
    assert command["nativeExit"] == 0
    assert command["durationMs"] == 2000
    assert command["stdoutSha256"] == hashlib.sha256(b"restore ok\n").hexdigest()
    assert command["stderrSha256"] == hashlib.sha256(b"").hexdigest()


def test_probe_retains_partial_output_and_does_not_publish_success_after_command_failure(
    tmp_path: Path,
) -> None:
    config, backup, target = _fixture(tmp_path)

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        partial = config.output_directory / "operator.partial"
        partial.write_bytes(b"retained partial")
        return subprocess.CompletedProcess(arguments, 17, stdout=b"", stderr=b"safe failure")

    with pytest.raises(RuntimeError, match="restore command failed"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=runner,
        )

    assert (config.output_directory / "operator.partial").read_bytes() == b"retained partial"
    assert not (config.output_directory / "backup-restore-report.json").exists()


def test_probe_fails_closed_on_noncanonical_or_mismatched_restore_provenance(
    tmp_path: Path,
) -> None:
    config, backup, target = _fixture(tmp_path)

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        payload = _restore_payload()
        payload["sourceArchive"]["manifestSha256"] = "9" * 64
        report_path = Path(arguments[arguments.index("--report") + 1])
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    with pytest.raises(ValueError, match="canonical|provenance"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=runner,
        )

    assert (config.output_directory / "restore-validation.json").exists()
    assert not (config.output_directory / "backup-restore-report.json").exists()


def test_probe_rejects_secret_output_without_writing_it_to_report(tmp_path: Path) -> None:
    config, backup, target = _fixture(tmp_path)

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        _write_restore_report(arguments, _restore_payload())
        return subprocess.CompletedProcess(arguments, 0, stdout=SECRET_VALUE, stderr=b"")

    with pytest.raises(RuntimeError, match="secret"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=runner,
        )

    assert not (config.output_directory / "backup-restore-report.json").exists()
    assert SECRET_VALUE not in b"".join(
        path.read_bytes() for path in config.output_directory.iterdir() if path.is_file()
    )


def test_probe_rejects_contract_sensitive_target_config_fields_before_output(
    tmp_path: Path,
) -> None:
    contract_sensitive_parts = tuple(_contract()._SENSITIVE_KEY_PARTS)
    sensitive_field_names = {
        "authorization": "requestAuthorization",
        "cookie": "session_cookie",
        "credential": "clientCredential",
        "password": "database-password",
        "secretvalue": "secretValue",
        "ticket": "supportTicket",
        "token": "apiToken",
    }
    assert set(sensitive_field_names) == set(contract_sensitive_parts)

    for index, part in enumerate(contract_sensitive_parts):
        case_root = tmp_path / f"sensitive-target-{index}"
        case_root.mkdir()
        config, backup, target = _fixture(case_root)
        target_config_body = _canonical(
            {
                "metadata": {
                    "ordinaryEvidence": {
                        "secretSha256": "1" * 64,
                        "publicKeySha256": "2" * 64,
                    },
                    "nested": [{sensitive_field_names[part]: "redacted"}],
                }
            }
        )
        config.target_config_path.write_bytes(target_config_body)
        runner_calls: list[list[str]] = []

        def runner(
            arguments: list[str],
            **_options: object,
        ) -> subprocess.CompletedProcess[bytes]:
            runner_calls.append(arguments)
            payload = _restore_payload()
            payload_target = payload["target"]
            assert isinstance(payload_target, dict)
            payload_target["targetConfigSha256"] = hashlib.sha256(target_config_body).hexdigest()
            _write_restore_report(arguments, payload)
            return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

        with pytest.raises((RuntimeError, ValueError), match="sensitive field"):
            _module().run_backup_restore_probe(
                config,
                verified_backup_loader=lambda path: _snapshot_backup(backup, path),
                verified_backup_rechecker=lambda current: current,
                target_config_loader=lambda path: target,
                command_runner=runner,
            )

        assert runner_calls == []
        assert not config.output_directory.exists()

    safe_root = tmp_path / "ordinary-target-fields"
    safe_root.mkdir()
    config, backup, target = _fixture(safe_root)
    target_config_body = _canonical(
        {
            "metadata": {
                "accessMode": "isolated",
                "objectKeySha256": "3" * 64,
                "publicKeySha256": "4" * 64,
                "secretSha256": "5" * 64,
            }
        }
    )
    config.target_config_path.write_bytes(target_config_body)

    def safe_runner(
        arguments: list[str],
        **_options: object,
    ) -> subprocess.CompletedProcess[bytes]:
        payload = _restore_payload()
        payload_target = payload["target"]
        assert isinstance(payload_target, dict)
        payload_target["targetConfigSha256"] = hashlib.sha256(target_config_body).hexdigest()
        _write_restore_report(arguments, payload)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    report_path = _module().run_backup_restore_probe(
        config,
        verified_backup_loader=lambda path: _snapshot_backup(backup, path),
        verified_backup_rechecker=lambda current: current,
        target_config_loader=lambda path: target,
        command_runner=safe_runner,
    )
    assert report_path.is_file()


def test_probe_recursively_scans_json_shaped_stdout_and_stderr(tmp_path: Path) -> None:
    cases = (
        ("stdout", "decoded-secret"),
        ("stderr", "decoded-secret"),
        ("stdout", "sensitive-key"),
        ("stderr", "sensitive-key"),
    )
    for index, (stream_name, finding) in enumerate(cases):
        case_root = tmp_path / f"{stream_name}-{finding}-{index}"
        case_root.mkdir()
        config, backup, target = _fixture(case_root)
        if finding == "decoded-secret":
            escaped_secret = b'operator"secret\\value'
            (config.target_secret_directory / TARGET_SECRET_NAMES[0]).write_bytes(escaped_secret)
            config = replace(config, forbidden_secret_values=())
            output = _canonical({"diagnostics": [{"message": escaped_secret.decode("utf-8")}]})
            assert escaped_secret not in output
        else:
            output = _canonical(
                {
                    "diagnostics": [
                        {
                            "publicKeySha256": "6" * 64,
                            "apiToken": "redacted",
                        }
                    ]
                }
            )

        def runner(
            arguments: list[str],
            **_options: object,
        ) -> subprocess.CompletedProcess[bytes]:
            _write_restore_report(arguments, _restore_payload())
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=output if stream_name == "stdout" else b"",
                stderr=output if stream_name == "stderr" else b"",
            )

        with pytest.raises(RuntimeError, match="secret|sensitive field"):
            _module().run_backup_restore_probe(
                config,
                verified_backup_loader=lambda path: _snapshot_backup(backup, path),
                verified_backup_rechecker=lambda current: current,
                target_config_loader=lambda path: target,
                command_runner=runner,
            )
        assert not (config.output_directory / "backup-restore-report.json").exists()


def test_probe_scans_inputs_with_required_secret_snapshot_before_any_persistent_write(
    tmp_path: Path,
) -> None:
    config, backup, target = _fixture(tmp_path)
    escaped_secret = b'operator"secret\\value'
    (config.target_secret_directory / TARGET_SECRET_NAMES[0]).write_bytes(escaped_secret)
    config = replace(config, forbidden_secret_values=())
    target_config_body = _canonical({"description": escaped_secret.decode("utf-8")})
    assert escaped_secret not in target_config_body
    config.target_config_path.write_bytes(target_config_body)
    runner_calls: list[list[str]] = []

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        runner_calls.append(arguments)
        pytest.fail("runner must not be called before input secret scanning")

    with pytest.raises(RuntimeError, match="secret"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=runner,
        )

    assert runner_calls == []
    assert not config.output_directory.exists()


@pytest.mark.parametrize("outcome", ["success", "baseexception"])
def test_probe_passes_owned_private_secret_snapshot_and_reconciles_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    module = _module()
    config, backup, target = _fixture(tmp_path)
    live_secret_directory = config.target_secret_directory.resolve()
    live_secret = live_secret_directory / TARGET_SECRET_NAMES[0]
    original_secret = live_secret.read_bytes()
    unrelated_secret = live_secret_directory / "classroom_ticket_secret"
    unrelated_body = b"unrelated-platform-secret-must-not-be-snapshotted"
    unrelated_secret.write_bytes(unrelated_body)
    opened_live_secrets: list[tuple[str, int]] = []
    handed_off_snapshots: list[Path] = []
    real_open = module.os.open

    def tracking_open(path: object, flags: int, *args: object) -> int:
        candidate = Path(path)
        if candidate.parent == live_secret_directory:
            opened_live_secrets.append((candidate.name, flags))
        return real_open(path, flags, *args)

    monkeypatch.setattr(module.os, "open", tracking_open)

    class OperatorAbort(BaseException):
        pass

    abort = OperatorAbort("operator interrupted after secret handoff")

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        snapshot = Path(arguments[arguments.index("--target-secret-dir") + 1]).resolve()
        handed_off_snapshots.append(snapshot)
        assert snapshot != live_secret_directory
        assert snapshot.is_dir()
        assert not snapshot.is_symlink()
        assert secret_file_is_restricted(snapshot)
        assert {entry.name for entry in snapshot.iterdir()} == set(TARGET_SECRET_NAMES)
        for secret_name in TARGET_SECRET_NAMES:
            snapshot_secret = snapshot / secret_name
            assert snapshot_secret.is_file()
            assert not snapshot_secret.is_symlink()
            assert secret_file_is_restricted(snapshot_secret)
            assert snapshot_secret.read_bytes() == original_secret
        assert not (snapshot / unrelated_secret.name).exists()

        live_secret.write_bytes(b"rotated-after-scan")
        assert (snapshot / live_secret.name).read_bytes() == original_secret
        if outcome == "baseexception":
            raise abort
        _write_restore_report(arguments, _restore_payload())
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    if outcome == "baseexception":
        with pytest.raises(OperatorAbort) as exc_info:
            module.run_backup_restore_probe(
                config,
                verified_backup_loader=lambda path: _snapshot_backup(backup, path),
                verified_backup_rechecker=lambda current: current,
                target_config_loader=lambda path: target,
                command_runner=runner,
            )
        assert exc_info.value is abort
        assert not (config.output_directory / "backup-restore-report.json").exists()
    else:
        report_path = module.run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=runner,
        )
        assert report_path.is_file()

    assert len(handed_off_snapshots) == 1
    assert not handed_off_snapshots[0].exists()
    assert unrelated_secret.read_bytes() == unrelated_body
    no_follow = getattr(module.os, "O_NOFOLLOW", 0)
    if no_follow:
        assert {name for name, _flags in opened_live_secrets} == set(TARGET_SECRET_NAMES)
        assert all(flags & no_follow for _name, flags in opened_live_secrets)


def test_probe_keeps_ephemeral_target_secret_snapshot_outside_evidence_tree(
    tmp_path: Path,
) -> None:
    module = _module()
    config, backup, target = _fixture(tmp_path)
    evidence_root = config.output_directory.parent.resolve()
    handed_off_snapshots: list[Path] = []

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        snapshot = Path(arguments[arguments.index("--target-secret-dir") + 1]).resolve()
        handed_off_snapshots.append(snapshot)
        assert not snapshot.is_relative_to(evidence_root), (
            "target secret snapshot must stay outside the evidence tree"
        )
        assert {entry.name for entry in snapshot.iterdir()} == set(TARGET_SECRET_NAMES)
        _write_restore_report(arguments, _restore_payload())
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    report_path = module.run_backup_restore_probe(
        config,
        verified_backup_loader=lambda path: _snapshot_backup(backup, path),
        verified_backup_rechecker=lambda current: current,
        target_config_loader=lambda path: target,
        command_runner=runner,
    )

    assert report_path.is_file()
    assert len(handed_off_snapshots) == 1
    assert not handed_off_snapshots[0].exists()
    assert not tuple(evidence_root.rglob(f"{module._TARGET_SECRET_SNAPSHOT_PREFIX}*"))


def test_probe_rechecks_source_archive_after_restore_and_fails_closed_on_change(
    tmp_path: Path,
) -> None:
    config, backup, target = _fixture(tmp_path)

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        _write_restore_report(arguments, _restore_payload())
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    def changed(_backup: object) -> object:
        raise ValueError("source archive changed")

    with pytest.raises(ValueError, match="source archive changed"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=changed,
            target_config_loader=lambda path: target,
            command_runner=runner,
        )

    assert not (config.output_directory / "backup-restore-report.json").exists()


def test_probe_requires_caller_supplied_fresh_output_directory(tmp_path: Path) -> None:
    config, backup, target = _fixture(tmp_path)
    config.output_directory.mkdir()

    with pytest.raises(FileExistsError, match="output directory"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=lambda *args, **kwargs: pytest.fail("runner must not be called"),
        )


def test_outer_report_publication_is_proof_last_atomic_and_no_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    report = {"proof": {"verified": True}}
    expected_body = module.canonical_backup_restore_report(report)

    existing_case = tmp_path / "existing"
    existing_case.mkdir()
    existing_target = existing_case / "backup-restore-report.json"
    existing_target.write_bytes(b"existing-proof\n")
    unrelated = existing_case / f".{existing_target.name}.unrelated.tmp"
    unrelated.write_bytes(b"unrelated-staging-inode\n")
    with pytest.raises(FileExistsError, match="already exists"):
        module._write_report(existing_target, report)
    assert existing_target.read_bytes() == b"existing-proof\n"
    assert unrelated.read_bytes() == b"unrelated-staging-inode\n"
    assert {entry.name for entry in existing_case.iterdir()} == {
        existing_target.name,
        unrelated.name,
    }

    class PublicationAbort(BaseException):
        pass

    real_fdopen = module.os.fdopen
    real_link = module._link_report_file_descriptor

    class LateFailingHandle:
        def __init__(self, handle: object, phase: str) -> None:
            self._handle = handle
            self._phase = phase

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *arguments: object):
            return self._handle.__exit__(*arguments)

        def write(self, body: bytes) -> int:
            written = self._handle.write(body)
            if self._phase == "write":
                raise PublicationAbort("late write failure")
            return written

        def flush(self) -> None:
            self._handle.flush()
            if self._phase == "flush":
                raise PublicationAbort("late flush failure")

        def fileno(self) -> int:
            return self._handle.fileno()

    for phase in ("write", "flush", "publish"):
        case = tmp_path / phase
        case.mkdir()
        target = case / "backup-restore-report.json"
        unrelated = case / f".{target.name}.unrelated.tmp"
        unrelated.write_bytes(b"unrelated-staging-inode\n")
        with monkeypatch.context() as scoped:
            if phase in {"write", "flush"}:
                scoped.setattr(
                    module.os,
                    "fdopen",
                    lambda descriptor, mode, *, closefd=True, _phase=phase: LateFailingHandle(
                        real_fdopen(descriptor, mode, closefd=closefd),
                        _phase,
                    ),
                )
            else:
                scoped.setattr(
                    module,
                    "_link_report_file_descriptor",
                    lambda *_arguments: (_ for _ in ()).throw(
                        PublicationAbort("late publish failure")
                    ),
                )
            with pytest.raises(PublicationAbort, match=f"late {phase} failure"):
                module._write_report(target, report)
        assert not target.exists()
        assert unrelated.read_bytes() == b"unrelated-staging-inode\n"
        assert {entry.name for entry in case.iterdir()} == {unrelated.name}

    success_case = tmp_path / "success"
    success_case.mkdir()
    success_target = success_case / "backup-restore-report.json"
    unrelated = success_case / f".{success_target.name}.unrelated.tmp"
    unrelated.write_bytes(b"unrelated-staging-inode\n")
    publication_events: list[str] = []
    real_fsync = module.os.fsync

    def observed_fsync(descriptor: int) -> None:
        publication_events.append("fsync")
        real_fsync(descriptor)

    def observed_link(
        descriptor: int,
        directory_handle: object,
        target_name: str,
    ) -> None:
        assert publication_events == ["fsync"]
        offset = module.os.lseek(descriptor, 0, module.os.SEEK_CUR)
        module.os.lseek(descriptor, 0, module.os.SEEK_SET)
        descriptor_body = module.os.read(descriptor, len(expected_body) + 1)
        module.os.lseek(descriptor, offset, module.os.SEEK_SET)
        assert descriptor_body == expected_body
        assert target_name == success_target.name
        assert not success_target.exists()
        publication_events.append("publish")
        real_link(descriptor, directory_handle, target_name)

    with monkeypatch.context() as scoped:
        scoped.setattr(module.os, "fsync", observed_fsync)
        scoped.setattr(module, "_link_report_file_descriptor", observed_link)
        module._write_report(success_target, report)
    assert publication_events[:2] == ["fsync", "publish"]
    assert success_target.read_bytes() == expected_body
    assert unrelated.read_bytes() == b"unrelated-staging-inode\n"
    assert {entry.name for entry in success_case.iterdir()} == {
        success_target.name,
        unrelated.name,
    }


def test_outer_report_prelink_name_replacement_cannot_change_published_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    report = {"proof": {"verified": True}}
    expected_body = module.canonical_backup_restore_report(report)
    target = tmp_path / "backup-restore-report.json"
    sentinel_body = b"prelink-replacement-must-not-be-published\n"
    real_link = module._link_report_file_descriptor
    state: dict[str, object] = {}

    def attempt_replacement_then_link(
        descriptor: int,
        directory_handle: object,
        target_name: str,
    ) -> None:
        candidates = tuple(tmp_path.glob(f".{target.name}.*.tmp"))
        if not candidates:
            state["unnamed"] = True
        else:
            assert len(candidates) == 1
            staging = candidates[0]
            try:
                staging.unlink()
            except PermissionError:
                state["replacement_blocked"] = True
            else:
                staging.write_bytes(sentinel_body)
                state["replacement_succeeded"] = True
        real_link(descriptor, directory_handle, target_name)

    monkeypatch.setattr(
        module,
        "_link_report_file_descriptor",
        attempt_replacement_then_link,
    )

    module._write_report(target, report)

    assert target.read_bytes() == expected_body
    assert set(state) in ({"unnamed"}, {"replacement_blocked"}, {"replacement_succeeded"})
    if state.get("replacement_succeeded"):
        sentinel = next(tmp_path.glob(f".{target.name}.*.tmp"))
        assert sentinel.read_bytes() == sentinel_body


def _replace_report_staging_when_owned_handle_closes(
    module: object,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: Path,
    sentinel_body: bytes,
) -> dict[str, object]:
    real_close = module._close_report_staging_handle
    state: dict[str, object] = {}

    def close_and_replace_once(descriptor: int, *arguments: object) -> None:
        if state:
            real_close(descriptor, *arguments)
            return
        staging = next(target.parent.glob(f".{target.name}.*.tmp"))
        original = staging.stat(follow_symlinks=False)
        real_close(descriptor, *arguments)
        assert not staging.exists()
        staging.write_bytes(sentinel_body)
        replacement = staging.stat(follow_symlinks=False)
        state.update(
            staging=staging,
            original_identity=(original.st_dev, original.st_ino),
            replacement_identity=(replacement.st_dev, replacement.st_ino),
        )

    monkeypatch.setattr(
        module,
        "_close_report_staging_handle",
        close_and_replace_once,
    )
    return state


def test_outer_report_never_publishes_replaced_staging_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    if module.os.name != "nt":
        pytest.skip("POSIX report staging is unnamed")
    report = {"proof": {"verified": True}}
    expected_body = module.canonical_backup_restore_report(report)
    target = tmp_path / "backup-restore-report.json"
    sentinel_body = b"replacement-sentinel-must-not-be-published\n"
    state = _replace_report_staging_when_owned_handle_closes(
        module,
        monkeypatch,
        target=target,
        sentinel_body=sentinel_body,
    )

    module._write_report(target, report)

    staging = state["staging"]
    assert isinstance(staging, Path)
    assert target.read_bytes() == expected_body
    assert staging.read_bytes() == sentinel_body
    assert state["original_identity"] != state["replacement_identity"]
    assert all(
        (entry.stat(follow_symlinks=False).st_dev, entry.stat(follow_symlinks=False).st_ino)
        != state["original_identity"]
        for entry in tmp_path.glob(f".{target.name}.*.tmp")
    )


def test_outer_report_precommit_cleanup_preserves_replacement_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    if module.os.name != "nt":
        pytest.skip("POSIX report staging is unnamed")
    report = {"proof": {"verified": True}}
    target = tmp_path / "backup-restore-report.json"
    sentinel_body = b"replacement-sentinel-must-not-be-cleaned\n"
    state = _replace_report_staging_when_owned_handle_closes(
        module,
        monkeypatch,
        target=target,
        sentinel_body=sentinel_body,
    )

    class PublicationAbort(BaseException):
        pass

    def abort_publication(*_arguments: object, **_kwargs: object) -> None:
        raise PublicationAbort("precommit publication aborted")

    monkeypatch.setattr(module.os, "link", abort_publication)
    monkeypatch.setattr(
        module,
        "_link_report_file_descriptor",
        abort_publication,
        raising=False,
    )

    with pytest.raises(PublicationAbort, match="precommit publication aborted"):
        module._write_report(target, report)

    staging = state["staging"]
    assert isinstance(staging, Path)
    assert not target.exists()
    assert staging.read_bytes() == sentinel_body
    assert state["original_identity"] != state["replacement_identity"]
    assert all(
        (entry.stat(follow_symlinks=False).st_dev, entry.stat(follow_symlinks=False).st_ino)
        != state["original_identity"]
        for entry in tmp_path.glob(f".{target.name}.*.tmp")
    )


@pytest.mark.parametrize(
    "postcommit_phase",
    ("directory-fsync", "readback", "staging-close"),
)
def test_outer_report_postcommit_baseexception_cannot_reverse_published_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    postcommit_phase: str,
) -> None:
    module = _module()
    report = {"proof": {"verified": True}}
    expected_body = module.canonical_backup_restore_report(report)
    target = tmp_path / "backup-restore-report.json"
    real_link = module._link_report_file_descriptor
    committed: list[Path] = []

    class PostCommitAbort(BaseException):
        pass

    def observed_link(
        descriptor: int,
        directory_handle: object,
        target_name: str,
    ) -> None:
        real_link(descriptor, directory_handle, target_name)
        assert target_name == target.name
        committed.append(target)

    def abort_directory_fsync(_directory: object) -> None:
        assert committed == [target]
        raise PostCommitAbort("post-commit directory fsync interrupted")

    def abort_readback(*_arguments: object) -> None:
        nonlocal readback_calls
        assert committed == [target]
        readback_calls += 1
        if readback_calls == 1:
            raise PostCommitAbort("post-commit readback interrupted")
        return real_readback(*_arguments)

    real_close = module._close_report_staging_handle
    real_readback = module._read_report_relative
    readback_calls = 0

    def abort_staging_close(descriptor: int, *arguments: object) -> None:
        assert committed == [target]
        real_close(descriptor, *arguments)
        raise PostCommitAbort("post-commit staging close interrupted")

    monkeypatch.setattr(module, "_link_report_file_descriptor", observed_link)
    if postcommit_phase == "directory-fsync":
        monkeypatch.setattr(module, "_fsync_report_directory", abort_directory_fsync)
    elif postcommit_phase == "readback":
        monkeypatch.setattr(module, "_read_report_relative", abort_readback)
    else:
        monkeypatch.setattr(
            module,
            "_close_report_staging_handle",
            abort_staging_close,
        )

    if postcommit_phase == "staging-close":
        with pytest.warns(RuntimeWarning, match="staging residual"):
            module._write_report(target, report)
    else:
        module._write_report(target, report)

    assert committed == [target]
    assert readback_calls == (2 if postcommit_phase == "readback" else 0)
    assert target.read_bytes() == expected_body
    owned_staging = tuple(tmp_path.glob(f".{target.name}.*.tmp"))
    assert owned_staging == ()


def test_outer_report_does_not_report_success_when_committed_name_cannot_be_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    report = {"proof": {"verified": True}}
    expected_body = module.canonical_backup_restore_report(report)
    target = tmp_path / "backup-restore-report.json"
    calls = 0

    def missing_report(*_arguments: object) -> None:
        nonlocal calls
        calls += 1
        raise FileNotFoundError("committed report name is unavailable")

    monkeypatch.setattr(module, "_read_report_relative", missing_report)

    with pytest.raises(FileNotFoundError, match="committed report name is unavailable"):
        module._write_report(target, report)

    assert calls == 2
    assert target.read_bytes() == expected_body
    assert tuple(tmp_path.glob(f".{target.name}.*.tmp")) == ()


def test_outer_report_retries_exact_windows_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    if module.os.name != "nt":
        pytest.skip("Windows delete-on-close retry contract")
    report = {"proof": {"verified": True}}
    expected_body = module.canonical_backup_restore_report(report)
    target = tmp_path / "backup-restore-report.json"
    real_delete = module._delete_windows_file_on_close
    calls = 0

    def fail_once(handle: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient delete-on-close failure")
        real_delete(handle)

    monkeypatch.setattr(module, "_delete_windows_file_on_close", fail_once)

    module._write_report(target, report)

    assert calls == 2
    assert target.read_bytes() == expected_body
    assert tuple(tmp_path.glob(f".{target.name}.*.tmp")) == ()


def test_outer_report_warns_when_windows_staging_cleanup_remains_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    if module.os.name != "nt":
        pytest.skip("Windows delete-on-close residual contract")
    report = {"proof": {"verified": True}}
    expected_body = module.canonical_backup_restore_report(report)
    target = tmp_path / "backup-restore-report.json"

    monkeypatch.setattr(
        module,
        "_delete_windows_file_on_close",
        lambda _handle: (_ for _ in ()).throw(OSError("persistent cleanup failure")),
    )

    with pytest.warns(RuntimeWarning, match="staging residual"):
        module._write_report(target, report)

    assert target.read_bytes() == expected_body
    residuals = tuple(tmp_path.glob(f".{target.name}.*.tmp"))
    assert len(residuals) == 1
    assert residuals[0].read_bytes() == expected_body


def test_outer_report_warning_policy_cannot_reverse_committed_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    if module.os.name != "nt":
        pytest.skip("Windows delete-on-close residual contract")
    report = {"proof": {"verified": True}}
    expected_body = module.canonical_backup_restore_report(report)
    target = tmp_path / "backup-restore-report.json"

    monkeypatch.setattr(
        module,
        "_delete_windows_file_on_close",
        lambda _handle: (_ for _ in ()).throw(OSError("persistent cleanup failure")),
    )
    monkeypatch.setattr(
        module.warnings,
        "warn",
        lambda *_arguments, **_options: (_ for _ in ()).throw(
            RuntimeWarning("warnings are errors")
        ),
    )

    module._write_report(target, report)

    assert target.read_bytes() == expected_body
    assert "retained an owned staging residual" in capsys.readouterr().err


@pytest.mark.parametrize("postcommit_phase", ("directory-fsync", "staging-unlink"))
def test_operator_report_postcommit_baseexception_cannot_reverse_published_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    postcommit_phase: str,
) -> None:
    restore = _restore_module()
    report = restore.RestoreValidationReport(
        ok=True,
        target_database_identity_sha256=TARGET_DATABASE_IDENTITY_SHA256,
        object_prefix="",
        validated=tuple(_restore_payload()["validated"]),
        failures=(),
    )
    payload_options = {
        "run_id": "run-backup-restore-01",
        "restored_object_count": 2,
        "archive_fingerprint_sha256": ARCHIVE_SHA256,
        "manifest_sha256": MANIFEST_SHA256,
        "target_object_bucket": "restore-bucket",
    }
    expected_body = _canonical(restore.restore_report_payload(report, **payload_options))
    target = tmp_path / "restore-validation.json"
    real_link = restore.os.link
    real_unlink = restore.Path.unlink
    committed: list[Path] = []

    class PostCommitAbort(BaseException):
        pass

    def observed_link(source: object, destination: object) -> None:
        real_link(source, destination)
        committed.append(Path(destination))

    def abort_directory_fsync(_directory: Path) -> None:
        assert committed == [target]
        raise PostCommitAbort("operator post-commit directory fsync interrupted")

    def abort_staging_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.parent == tmp_path and path.name.startswith(f".{target.name}."):
            assert committed == [target]
            raise PostCommitAbort("operator post-commit staging cleanup interrupted")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(restore.os, "link", observed_link)
    if postcommit_phase == "directory-fsync":
        monkeypatch.setattr(restore, "_fsync_directory", abort_directory_fsync)
    else:
        monkeypatch.setattr(restore.Path, "unlink", abort_staging_unlink)

    restore._write_restore_report(target, report, **payload_options)

    assert committed == [target]
    assert target.read_bytes() == expected_body
    owned_staging = tuple(tmp_path.glob(f".{target.name}.*.tmp"))
    assert len(owned_staging) == (1 if postcommit_phase == "staging-unlink" else 0)


def test_probe_rejects_invalid_candidate_before_output_or_restore(tmp_path: Path) -> None:
    config, backup, target = _fixture(tmp_path)
    config.candidate["sourceHead"] = "not-a-commit"

    with pytest.raises(ValueError, match="candidate"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=lambda *args, **kwargs: pytest.fail("runner must not be called"),
        )

    assert not config.output_directory.exists()


def test_probe_rejects_source_provenance_from_another_candidate_before_restore(
    tmp_path: Path,
) -> None:
    config, backup, target = _fixture(tmp_path)
    config.source_provenance["candidate"]["sourceHead"] = "9" * 40

    with pytest.raises(ValueError, match="source provenance"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=lambda *args, **kwargs: pytest.fail("runner must not be called"),
        )

    assert (config.output_directory / "source-backup.snapshot" / "database.dump").is_file()
    assert not (config.output_directory / "backup-restore-report.json").exists()


def test_probe_binds_source_provenance_and_uses_immutable_target_config_snapshot(
    tmp_path: Path,
) -> None:
    config, backup, target = _fixture(tmp_path)

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        snapshot = Path(arguments[arguments.index("--target-config") + 1])
        snapshot_sha256 = arguments[arguments.index("--target-config-sha256") + 1]
        assert snapshot != config.target_config_path.resolve()
        assert snapshot.name == "target-config.snapshot.json"
        assert snapshot.read_bytes() == b"{}\n"
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == snapshot_sha256
        _write_restore_report(arguments, _restore_payload())
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    report_path = _module().run_backup_restore_probe(
        config,
        verified_backup_loader=lambda path: _snapshot_backup(backup, path),
        verified_backup_rechecker=lambda current: current,
        target_config_loader=lambda path: target,
        command_runner=runner,
    )

    report = json.loads(report_path.read_bytes())
    provenance_body = _canonical(_source_provenance())
    assert (config.output_directory / "source-provenance.json").read_bytes() == provenance_body
    assert report["source"]["provenanceSha256"] == hashlib.sha256(provenance_body).hexdigest()
    assert report["target"]["targetConfigSha256"] == hashlib.sha256(b"{}\n").hexdigest()


def test_probe_validates_and_restores_from_same_owned_source_archive_snapshot(
    tmp_path: Path,
) -> None:
    config, backup, target = _fixture(tmp_path)
    original_directory = config.backup_directory.resolve()
    original_database = original_directory / "database.dump"
    original_body = original_database.read_bytes()
    original_sha256 = hashlib.sha256(original_body).hexdigest()
    validated: list[tuple[Path, tuple[int, int]]] = []
    rechecked: list[tuple[Path, tuple[int, int]]] = []

    def archive_at(path: Path) -> object:
        directory = Path(path).resolve()
        database = directory / "database.dump"
        file_stat = database.stat()
        identity = (file_stat.st_dev, file_stat.st_ino)
        current = copy.deepcopy(backup)
        current.directory = directory
        current.database_dump = database
        validated.append((directory, identity))
        return current

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        snapshot = Path(arguments[arguments.index("--backup-dir") + 1]).resolve()
        assert snapshot != original_directory
        assert snapshot.parent == config.output_directory.resolve()
        snapshot_database = snapshot / "database.dump"
        snapshot_stat = snapshot_database.stat()
        assert validated == [(snapshot, (snapshot_stat.st_dev, snapshot_stat.st_ino))]
        assert (snapshot_stat.st_dev, snapshot_stat.st_ino) != (
            original_database.stat().st_dev,
            original_database.stat().st_ino,
        )
        original_database.write_bytes(b"source changed after owned snapshot")
        assert hashlib.sha256(snapshot_database.read_bytes()).hexdigest() == original_sha256
        assert (snapshot_database.stat().st_dev, snapshot_database.stat().st_ino) == validated[0][1]
        _write_restore_report(arguments, _restore_payload())
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    def recheck(current: object) -> object:
        directory = Path(current.directory).resolve()
        database = Path(current.database_dump)
        identity = (database.stat().st_dev, database.stat().st_ino)
        rechecked.append((directory, identity))
        assert validated == [(directory, identity)]
        assert hashlib.sha256(database.read_bytes()).hexdigest() == original_sha256
        return current

    report_path = _module().run_backup_restore_probe(
        config,
        verified_backup_loader=archive_at,
        verified_backup_rechecker=recheck,
        target_config_loader=lambda path: target,
        command_runner=runner,
    )

    assert report_path.is_file()
    assert rechecked == validated


def test_shared_deadline_terminates_and_reaps_timed_out_operator_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    config, _backup, _target = _fixture(tmp_path)
    deadline_monotonic = 400.0
    arguments = module._command_arguments(
        config,
        backup_directory=config.backup_directory,
        target_config=config.target_config_path,
        target_config_sha256="7" * 64,
        provisioning_receipt=config.provisioning_receipt_path,
        provisioning_receipt_sha256=hashlib.sha256(
            config.provisioning_receipt_path.read_bytes()
        ).hexdigest(),
        target_secret_directory=config.target_secret_directory,
        output_directory=config.output_directory,
        python_executable=config.python_executable,
        pg_restore_executable=config.pg_restore_executable,
        deadline_monotonic=deadline_monotonic,
        secret_values=(SECRET_VALUE,),
    )
    deadline_index = arguments.index("--deadline-monotonic")
    assert float(arguments[deadline_index + 1]) == deadline_monotonic

    events: list[tuple[str, object]] = []

    class TimedOutProcess:
        pid = 4242
        returncode: int | None = None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            events.append(("communicate", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired(arguments, timeout)
            return b"", b""

    process = TimedOutProcess()
    popen_options: dict[str, object] = {}

    def popen(argv: list[str], **options: object) -> TimedOutProcess:
        assert argv == arguments
        popen_options.update(options)
        return process

    def terminate_tree(current: TimedOutProcess) -> None:
        events.append(("terminate-tree", current.pid))
        current.returncode = 137

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("runner must own and reap its Popen process tree"),
    )
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "_terminate_process_tree", terminate_tree, raising=False)

    with pytest.raises(subprocess.TimeoutExpired):
        module._default_command_runner(
            arguments,
            cwd=tmp_path,
            env={},
            deadline_monotonic=deadline_monotonic,
            monotonic=lambda: 100.0,
            check=False,
            capture_output=True,
        )

    assert events == [
        ("communicate", 300.0),
        ("terminate-tree", 4242),
        ("communicate", 10.0),
    ]
    assert popen_options["shell"] is False
    if module.os.name == "nt":
        assert popen_options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert popen_options["start_new_session"] is True


def test_command_runner_reaps_tree_with_cleanup_grace_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    arguments = ["python", "restore-operator"]
    interrupt = KeyboardInterrupt("operator interrupted")
    events: list[tuple[str, object]] = []

    class InterruptedProcess:
        pid = 4343
        returncode: int | None = None
        communicate_calls = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            events.append(("communicate", timeout))
            if self.communicate_calls == 1:
                raise interrupt
            self.returncode = 130
            return b"", b""

    process = InterruptedProcess()

    def terminate_tree(current: InterruptedProcess) -> None:
        events.append(("terminate-tree", current.pid))

    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(module, "_terminate_process_tree", terminate_tree)

    with pytest.raises(KeyboardInterrupt) as captured:
        module._default_command_runner(
            arguments,
            cwd=tmp_path,
            env={},
            deadline_monotonic=110.0,
            cleanup_grace_seconds=7.0,
            monotonic=lambda: 100.0,
            check=False,
            capture_output=True,
        )

    assert captured.value is interrupt
    assert events == [
        ("communicate", 10.0),
        ("terminate-tree", 4343),
        ("communicate", 7.0),
    ]


def test_object_client_cleanup_is_shielded_and_reconciled_under_repeated_cancellation() -> None:
    restore = _restore_module()
    close_started = threading.Event()
    allow_close = threading.Event()
    close_finished = threading.Event()
    events: list[str] = []

    class Resource:
        def close(self) -> None:
            events.append("close-start")
            close_started.set()
            if not allow_close.wait(timeout=5):
                raise AssertionError("close was not released")
            events.append("close-finish")
            close_finished.set()

    async def scenario() -> None:
        task = asyncio.create_task(restore._close_resource(Resource()))
        try:
            for _ in range(1000):
                if close_started.is_set():
                    break
                await asyncio.sleep(0)
            assert close_started.is_set()
            task.cancel("first cleanup cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel("second cleanup cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            allow_close.set()
            with pytest.raises(asyncio.CancelledError) as captured:
                await task
            assert captured.value.args == ("first cleanup cancellation",)
        finally:
            allow_close.set()
            if not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())

    assert close_finished.is_set()
    assert events == ["close-start", "close-finish"]


@pytest.mark.parametrize(
    "exceeds_cleanup_grace",
    [False, True],
    ids=["within-grace", "grace-exceeded"],
)
def test_object_claim_client_call_stays_owned_until_thread_terminal_before_unlock_and_close(
    monkeypatch: pytest.MonkeyPatch,
    exceeds_cleanup_grace: bool,
) -> None:
    restore = _restore_module()
    cleanup_grace_seconds = 0.02 if exceeds_cleanup_grace else 5.0
    monkeypatch.setattr(
        restore,
        "_OBJECT_CLIENT_CLEANUP_GRACE_SECONDS",
        cleanup_grace_seconds,
        raising=False,
    )
    put_started = threading.Event()
    allow_put = threading.Event()
    put_finished = threading.Event()
    close_finished = threading.Event()
    events: list[str] = []

    class ServiceModel:
        def operation_model(self, name: str) -> object:
            assert name == "PutObject"
            return SimpleNamespace(input_shape=SimpleNamespace(members={"IfNoneMatch": object()}))

    class ObjectClient:
        meta = SimpleNamespace(service_model=ServiceModel())

        def put_object(self, **arguments: object) -> dict[str, str]:
            assert arguments["IfNoneMatch"] == "*"
            events.append("put-start")
            put_started.set()
            if not allow_put.wait(timeout=5):
                raise AssertionError("object mutation was not released")
            events.append("put-finish")
            put_finished.set()
            return {"ETag": "restore-control-etag", "VersionId": "restore-control-version"}

        def close(self) -> None:
            if not put_finished.is_set():
                events.append("close-before-put-finish")
            events.append("close")
            close_finished.set()

    client = ObjectClient()

    @asynccontextmanager
    async def held_exclusion():
        events.append("lock")
        try:
            yield
        finally:
            if not put_finished.is_set():
                events.append("unlock-before-put-finish")
            events.append("unlock")

    async def operation() -> None:
        try:
            async with held_exclusion():
                await restore._claim_object_restore_control(
                    client,
                    bucket="restore-bucket",
                    candidate_sha256="1" * 64,
                    provisioning_receipt_sha256="2" * 64,
                    run_id="run-object-client-cancel",
                    environment_id="environment-object-client-cancel",
                    database_identity_sha256="3" * 64,
                    object_store_identity_sha256="4" * 64,
                )
        finally:
            await restore._close_resource(client)

    async def scenario() -> None:
        task = asyncio.create_task(operation())
        try:
            for _ in range(1000):
                if put_started.is_set():
                    break
                await asyncio.sleep(0)
            assert put_started.is_set()
            task.cancel("first object client cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            assert "unlock" not in events
            assert "close" not in events
            task.cancel("second object client cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            assert "unlock" not in events
            assert "close" not in events
            if exceeds_cleanup_grace:
                await asyncio.sleep(cleanup_grace_seconds * 3)
                assert not task.done()
                assert "unlock" not in events
                assert "close" not in events
            allow_put.set()
            with pytest.raises(asyncio.CancelledError) as captured:
                await task
            assert captured.value.args == ("first object client cancellation",)
            notes = tuple(getattr(captured.value, "__notes__", ()))
            if exceeds_cleanup_grace:
                assert any("object client operation cleanup incomplete" in note for note in notes)
            else:
                assert not any("cleanup incomplete" in note for note in notes)
        finally:
            allow_put.set()
            if not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
            assert await asyncio.to_thread(put_finished.wait, 5)

    asyncio.run(scenario())

    assert put_finished.is_set()
    assert close_finished.is_set()
    assert events == ["lock", "put-start", "put-finish", "unlock", "close"]


def test_process_runner_is_shielded_and_reconciled_under_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore = _restore_module()
    runner_started = threading.Event()
    allow_runner = threading.Event()
    runner_finished = threading.Event()
    events: list[str] = []

    def blocking_runner(
        argv: tuple[str, ...],
        environment: dict[str, str],
        *,
        deadline_monotonic: float,
        monotonic,
    ) -> int:
        assert argv == ("pg_restore", "--clean")
        assert environment == {"PGPASSWORD": "secret"}
        assert deadline_monotonic == 50.0
        assert monotonic() == 10.0
        events.append("runner-start")
        runner_started.set()
        if not allow_runner.wait(timeout=5):
            raise AssertionError("runner was not released")
        events.append("runner-finish")
        runner_finished.set()
        return 0

    monkeypatch.setattr(restore, "_run_process_with_deadline", blocking_runner)

    async def scenario() -> None:
        task = asyncio.create_task(
            restore._default_process_runner(
                ("pg_restore", "--clean"),
                {"PGPASSWORD": "secret"},
                deadline_monotonic=50.0,
                monotonic=lambda: 10.0,
            )
        )
        try:
            for _ in range(1000):
                if runner_started.is_set():
                    break
                await asyncio.sleep(0)
            assert runner_started.is_set()
            task.cancel("first runner cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel("second runner cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            allow_runner.set()
            with pytest.raises(asyncio.CancelledError) as captured:
                await task
            assert captured.value.args == ("first runner cancellation",)
        finally:
            allow_runner.set()
            if not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())

    assert runner_finished.is_set()
    assert events == ["runner-start", "runner-finish"]


def test_child_process_runner_terminates_and_reaps_tree_on_baseexception_with_cleanup_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore = _restore_module()
    interrupt = KeyboardInterrupt("operator interrupted")
    events: list[tuple[str, object]] = []

    class InterruptedProcess:
        pid = 5454
        returncode: int | None = None
        wait_calls = 0

        def wait(self, timeout: float) -> int:
            self.wait_calls += 1
            events.append(("wait", timeout))
            if self.wait_calls == 1:
                raise interrupt
            self.returncode = 130
            return self.returncode

        def kill(self) -> None:
            pytest.fail("tree termination succeeds; fallback kill must not run")

    process = InterruptedProcess()
    monkeypatch.setattr(restore.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        restore,
        "_terminate_process_tree",
        lambda current: events.append(("terminate-tree", current.pid)),
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        restore._run_process_with_deadline(
            ("pg_restore", "--clean"),
            {"PGPASSWORD": "secret"},
            deadline_monotonic=110.0,
            cleanup_grace_seconds=7.0,
            monotonic=lambda: 100.0,
        )

    assert captured.value is interrupt
    assert events == [
        ("wait", 10.0),
        ("terminate-tree", 5454),
        ("wait", 7.0),
    ]


def test_target_exclusion_unlock_is_shielded_under_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore = _restore_module()
    monkeypatch.setitem(sys.modules, "sqlalchemy", SimpleNamespace(text=lambda value: value))
    events: list[str] = []

    async def scenario() -> None:
        held = asyncio.Event()
        unlock_started = asyncio.Event()
        allow_unlock = asyncio.Event()

        class Result:
            def __init__(self, value: bool) -> None:
                self._value = value

            def scalar_one(self) -> bool:
                return self._value

        class Connection:
            async def __aenter__(self) -> Connection:
                events.append("connection-enter")
                return self

            async def __aexit__(self, *_error: object) -> None:
                events.append("connection-exit")

            async def execute(self, statement: str, _parameters: object) -> Result:
                if "pg_try_advisory_lock" in statement:
                    events.append("lock-acquire")
                    return Result(True)
                assert "pg_advisory_unlock" in statement
                events.append("unlock-start")
                unlock_started.set()
                await allow_unlock.wait()
                events.append("unlock-finish")
                return Result(True)

        class Engine:
            def connect(self) -> Connection:
                return Connection()

        async def holder() -> None:
            async with restore._default_target_exclusion(Engine(), "1" * 64):
                events.append("held")
                held.set()
                await asyncio.Future()

        task = asyncio.create_task(holder())
        try:
            await held.wait()
            task.cancel("first exclusion cancellation")
            await unlock_started.wait()
            assert not task.done()
            task.cancel("second exclusion cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            allow_unlock.set()
            with pytest.raises(asyncio.CancelledError) as captured:
                await task
            assert captured.value.args == ("first exclusion cancellation",)
        finally:
            allow_unlock.set()
            if not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())

    assert events == [
        "connection-enter",
        "lock-acquire",
        "held",
        "unlock-start",
        "unlock-finish",
        "connection-exit",
    ]


def test_target_exclusion_connection_exit_is_shielded_under_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore = _restore_module()
    monkeypatch.setitem(sys.modules, "sqlalchemy", SimpleNamespace(text=lambda value: value))
    events: list[str] = []

    async def scenario() -> None:
        held = asyncio.Event()
        connection_exit_started = asyncio.Event()
        allow_connection_exit = asyncio.Event()

        class Result:
            def scalar_one(self) -> bool:
                return True

        class Connection:
            async def __aenter__(self) -> Connection:
                events.append("connection-enter")
                return self

            async def __aexit__(self, *_error: object) -> None:
                events.append("connection-exit-start")
                connection_exit_started.set()
                await allow_connection_exit.wait()
                events.append("connection-exit-finish")

            async def execute(self, statement: str, _parameters: object) -> Result:
                if "pg_try_advisory_lock" in statement:
                    events.append("lock-acquire")
                else:
                    assert "pg_advisory_unlock" in statement
                    events.append("unlock")
                return Result()

        class Engine:
            def connect(self) -> Connection:
                return Connection()

        async def holder() -> None:
            async with restore._default_target_exclusion(Engine(), "1" * 64):
                events.append("held")
                held.set()
                await asyncio.Future()

        task = asyncio.create_task(holder())
        try:
            await held.wait()
            task.cancel("first connection cancellation")
            await connection_exit_started.wait()
            assert not task.done()
            task.cancel("second connection cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            allow_connection_exit.set()
            with pytest.raises(asyncio.CancelledError) as captured:
                await task
            assert captured.value.args == ("first connection cancellation",)
        finally:
            allow_connection_exit.set()
            if not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())

    assert events == [
        "connection-enter",
        "lock-acquire",
        "held",
        "unlock",
        "connection-exit-start",
        "connection-exit-finish",
    ]


def test_restore_operator_target_engine_dispose_is_shielded_under_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore = _restore_module()
    dispose_started = threading.Event()
    allow_dispose = threading.Event()
    dispose_finished = threading.Event()
    events: list[str] = []

    class Engine:
        async def dispose(self) -> None:
            events.append("dispose-start")
            dispose_started.set()
            while not allow_dispose.is_set():
                await asyncio.sleep(0)
            events.append("dispose-finish")
            dispose_finished.set()

    engine = Engine()
    backup = SimpleNamespace(object_payloads=(), object_inventory=())
    target = SimpleNamespace(
        database_user="yfeistai_migrator",
        database_url="postgresql+asyncpg://target",
    )

    async def unused(*_args: object, **_kwargs: object):
        raise AssertionError("operation must not be reached")

    def unavailable_object_client(_target: object) -> object:
        raise RuntimeError("object service unavailable")

    runtime = restore.RestoreOperatorRuntime(
        target_loader=lambda _config, _secrets: target,
        engine_factory=lambda _url: engine,
        object_client_factory=unavailable_object_client,
        database_probe=unused,
        facts_inspector=unused,
        process_runner=unused,
        receipt_rebinder=unused,
        app_access_granter=unused,
        app_engine_factory=lambda _url: pytest.fail("app engine must not be created"),
        app_access_probe=unused,
        target_exclusion=lambda *_args: pytest.fail("target exclusion must not be entered"),
        target_exclusion_mode="postgresql-session-advisory-lock",
        object_state_probe=unused,
    )
    monkeypatch.setattr(restore, "_load_verified_backup", lambda _path: backup)
    monkeypatch.setattr(restore, "_validate_target_config_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        restore,
        "_load_target_provisioning_receipt",
        lambda *_args, **_kwargs: b"measured receipt",
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            restore.run_restore_operator(
                backup_dir=tmp_path / "source-backup",
                target_config=tmp_path / "target-config.json",
                target_secret_dir=tmp_path / "target-secrets",
                run_id="run-engine-cleanup",
                report_path=tmp_path / "restore-validation.json",
                runtime=runtime,
                target_config_sha256="1" * 64,
                provisioning_receipt=tmp_path / "provisioning-receipt.json",
                provisioning_receipt_sha256="2" * 64,
                database_ownership="created",
                object_namespace_ownership="created",
                candidate_sha256="3" * 64,
                environment_id="environment-engine-cleanup",
            )
        )
        try:
            for _ in range(1000):
                if dispose_started.is_set():
                    break
                await asyncio.sleep(0)
            assert dispose_started.is_set()
            task.cancel("first engine cleanup cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel("second engine cleanup cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            allow_dispose.set()
            with pytest.raises(
                RuntimeError,
                match="restore target object storage is unavailable",
            ):
                await task
        finally:
            allow_dispose.set()
            if not task.done():
                try:
                    await asyncio.shield(task)
                except (asyncio.CancelledError, RuntimeError):
                    pass

    asyncio.run(scenario())

    assert dispose_finished.is_set()
    assert events == ["dispose-start", "dispose-finish"]


def test_target_observations_and_mutations_share_one_held_concurrency_exclusion() -> None:
    module = _restore_module()
    events: list[str] = []
    exclusion_active = False
    target_mutated = False
    mutation_started = asyncio.Event()
    allow_post_observation = asyncio.Event()

    @asynccontextmanager
    async def exclusion():
        nonlocal exclusion_active
        events.append("acquire")
        if exclusion_active:
            events.append("blocked")
            raise RuntimeError("target concurrency exclusion is already held")
        exclusion_active = True
        try:
            yield
        finally:
            exclusion_active = False
            events.append("release")

    async def observe() -> str:
        assert exclusion_active is True
        state = "post" if target_mutated else "pre"
        events.append(f"observe-{state}")
        return state

    async def mutate() -> int:
        nonlocal target_mutated
        assert exclusion_active is True
        events.append("mutate")
        target_mutated = True
        mutation_started.set()
        await allow_post_observation.wait()
        return 2

    def validate(pre: object, post: object, restored_count: object) -> dict[str, object]:
        assert exclusion_active is True
        events.append("validate")
        assert (pre, post, restored_count) == ("pre", "post", 2)
        return {"pre": pre, "post": post, "restoredCount": restored_count}

    async def scenario() -> dict[str, object]:
        first = asyncio.create_task(
            module._execute_measured_target_operation(
                exclusion=exclusion,
                observe=observe,
                mutate=mutate,
                validate=validate,
            )
        )
        await mutation_started.wait()
        with pytest.raises(RuntimeError, match="concurrency exclusion"):
            await module._execute_measured_target_operation(
                exclusion=exclusion,
                observe=observe,
                mutate=mutate,
                validate=validate,
            )
        allow_post_observation.set()
        return await first

    evidence = asyncio.run(scenario())

    assert evidence == {"pre": "pre", "post": "post", "restoredCount": 2}
    assert events == [
        "acquire",
        "observe-pre",
        "mutate",
        "acquire",
        "blocked",
        "observe-post",
        "validate",
        "release",
    ]


def test_default_database_probe_uses_physical_cluster_and_database_identity() -> None:
    restore = _restore_module()
    system_identifier = "7418529630741852963"
    database_oid = "16401"
    database_name = "restore-db-run-01"
    queries: list[str] = []

    class Result:
        def __init__(self, scalar: object) -> None:
            self.scalar = scalar

        def scalar_one(self) -> object:
            return self.scalar

        def one(self) -> object:
            return SimpleNamespace(
                system_identifier=system_identifier,
                database_oid=database_oid,
                database_name=database_name,
            )

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_arguments: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            query = str(statement)
            queries.append(query)
            if "count(*)" in query:
                return Result(0)
            return Result(database_oid)

    class Engine:
        def __init__(self, host_alias: str) -> None:
            self.url = SimpleNamespace(
                host=host_alias,
                port=5432,
                database=database_name,
            )

        def connect(self) -> Connection:
            return Connection()

    first = asyncio.run(restore._default_database_probe(Engine("alias-a.internal")))
    second = asyncio.run(restore._default_database_probe(Engine("alias-b.internal")))
    expected_identity = hashlib.sha256(
        _canonical(
            {
                "databaseName": database_name,
                "databaseOid": database_oid,
                "systemIdentifier": system_identifier,
            }
        )
    ).hexdigest()

    assert first.identity_sha256 == expected_identity
    assert second.identity_sha256 == expected_identity
    assert first.user_object_count == second.user_object_count == 0
    assert sum("pg_control_system" in query for query in queries) == 2


def test_source_database_snapshot_uses_physical_identity_across_host_aliases(
    tmp_path: Path,
) -> None:
    backup = _backup_module()
    system_identifier = "7418529630741852963"
    database_oid = "16401"
    database_name = "source-db"
    queries: list[str] = []

    class Transaction:
        async def start(self) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class Connection:
        def transaction(self, **_options: object) -> Transaction:
            return Transaction()

        async def fetchval(self, query: str) -> object:
            queries.append(query)
            if "pg_export_snapshot" in query:
                return "snapshot-physical-identity"
            if "pg_control_system" in query:
                return system_identifier
            if "pg_database" in query:
                return database_oid
            if "platform.alembic_version" in query:
                return "20260830_0023"
            raise AssertionError(query)

        async def fetch(self, query: str) -> tuple[object, ...]:
            queries.append(query)
            return ()

        async def close(self) -> None:
            return None

    async def connect(**_options: object) -> Connection:
        return Connection()

    def runner(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
        destination = Path(
            next(
                argument.removeprefix("--file=")
                for argument in arguments
                if argument.startswith("--file=")
            )
        )
        destination.write_bytes(b"PGDMP\x01")
        return subprocess.CompletedProcess(arguments, 0)

    async def snapshot(host_alias: str, destination: Path) -> object:
        destination.write_bytes(b"")
        config = backup._RuntimeConfig(
            host_alias,
            5432,
            database_name,
            "backup_operator",
            "https://objects.example.test",
            "source-alias",
            "source-bucket",
            "us-east-1",
        )
        return await backup._dump_postgres_snapshot(
            destination,
            config=config,
            password="database-secret",
            pg_dump=Path("pg_dump"),
            connect=connect,
            runner=runner,
        )

    first = asyncio.run(snapshot("alias-a.internal", tmp_path / "first.dump"))
    second = asyncio.run(snapshot("alias-b.internal", tmp_path / "second.dump"))
    expected_identity = hashlib.sha256(
        _canonical(
            {
                "databaseName": database_name,
                "databaseOid": database_oid,
                "systemIdentifier": system_identifier,
            }
        )
    ).hexdigest()

    assert first.database_identity_sha256 == expected_identity
    assert second.database_identity_sha256 == expected_identity
    assert sum("pg_control_system" in query for query in queries) == 2


def test_source_object_snapshot_uses_authoritative_owner_physical_identity(
    tmp_path: Path,
) -> None:
    backup = _backup_module()
    owner_id = "source-owner-physical-01"
    owner_observations: list[str] = []

    async def dump_database(destination: Path) -> object:
        destination.write_bytes(b"database snapshot")
        return backup.TeachingBackupFacts(
            database_identity_sha256="a" * 64,
            platform_schema_revision="20260830_0023",
            schema_revisions={},
            classroom_versions_count=0,
            learning_events_count=0,
        )

    async def enumerate_object_versions() -> tuple[object, ...]:
        return ()

    async def read_object_version(_source: object, _destination: Path) -> None:
        raise AssertionError("an empty object snapshot must not read a payload")

    async def observe_owner_id() -> str:
        owner_observations.append(owner_id)
        return owner_id

    async def snapshot(
        output: Path,
        *,
        endpoint: str,
        region: str,
        namespace_alias: str,
    ) -> object:
        return await backup.create_restorable_teaching_backup(
            output,
            dump_database=dump_database,
            enumerate_object_versions=enumerate_object_versions,
            read_object_version=read_object_version,
            observe_source_object_store_owner_id=observe_owner_id,
            source_object_store_endpoint=endpoint,
            source_object_store_region=region,
            source_object_store_namespace_id=namespace_alias,
            source_object_store_bucket="source-bucket",
            created_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        )

    first = asyncio.run(
        snapshot(
            tmp_path / "first",
            endpoint="HTTPS://OBJECTS.EXAMPLE.TEST:443/",
            region="US-EAST-1",
            namespace_alias="source-alias-a",
        )
    )
    second = asyncio.run(
        snapshot(
            tmp_path / "second",
            endpoint="https://objects.example.test",
            region="us-east-1",
            namespace_alias="source-alias-b",
        )
    )
    owner_id_sha256 = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    expected_identity = hashlib.sha256(
        _canonical(
            {
                "bucket": "source-bucket",
                "endpoint": "https://objects.example.test",
                "ownerIdSha256": owner_id_sha256,
                "region": "us-east-1",
            }
        )
    ).hexdigest()

    assert first.source_object_store_identity_sha256 == expected_identity
    assert second.source_object_store_identity_sha256 == expected_identity
    assert owner_observations == [owner_id, owner_id, owner_id, owner_id]


def test_default_object_probe_uses_canonical_physical_identity() -> None:
    restore = _restore_module()
    owner_id = "owner-physical-01"

    class ObjectClient:
        def get_bucket_versioning(self, **_arguments: object) -> dict[str, object]:
            return {"Status": "Enabled"}

        def list_object_versions(self, **_arguments: object) -> dict[str, object]:
            return {"IsTruncated": False, "Versions": [], "DeleteMarkers": []}

        def get_bucket_acl(self, **_arguments: object) -> dict[str, object]:
            return {"Owner": {"ID": owner_id}}

    def target(endpoint: str, region: str, namespace_alias: str) -> object:
        return restore.RestoreTarget(
            database_url="postgresql+asyncpg://restore-target",
            app_database_url="postgresql+asyncpg://restore-app",
            database_host="restore-db.internal",
            database_port=5432,
            database_name="restore-db-run-01",
            database_user="yfeistai_migrator",
            database_password="database-secret",
            object_endpoint=endpoint,
            object_namespace_id=namespace_alias,
            object_bucket="restore-bucket",
            object_region=region,
            object_access_key="object-access-secret",
            object_secret_key="object-secret",
        )

    first = asyncio.run(
        restore._default_object_state_probe(
            ObjectClient(),
            target("HTTPS://OBJECTS.EXAMPLE.TEST:443/", "US-EAST-1", "alias-a"),
        )
    )
    second = asyncio.run(
        restore._default_object_state_probe(
            ObjectClient(),
            target("https://objects.example.test", "us-east-1", "alias-b"),
        )
    )
    owner_id_sha256 = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    expected_identity = hashlib.sha256(
        _canonical(
            {
                "bucket": "restore-bucket",
                "endpoint": "https://objects.example.test",
                "ownerIdSha256": owner_id_sha256,
                "region": "us-east-1",
            }
        )
    ).hexdigest()

    assert first.identity_sha256 == expected_identity
    assert second.identity_sha256 == expected_identity
    assert first.owner_id_sha256 == second.owner_id_sha256 == owner_id_sha256


@pytest.mark.parametrize(
    "mismatch",
    (
        "candidate",
        "run",
        "environment",
        "disposition",
        "database-identity",
        "object-identity",
        "digest",
    ),
)
def test_target_provisioning_receipt_rejects_tamper_or_binding_mismatch(
    mismatch: str,
) -> None:
    contract = _contract()
    candidate_sha256 = hashlib.sha256(_canonical(_candidate())).hexdigest()
    database_identity_sha256 = TARGET_DATABASE_IDENTITY_SHA256
    object_store_identity_sha256 = "2" * 64
    receipt = _provisioning_receipt(
        database_identity_sha256,
        object_store_identity_sha256,
    )
    if mismatch == "candidate":
        receipt["candidateSha256"] = "9" * 64
    elif mismatch == "run":
        receipt["releaseRun"]["runId"] = "other-run"
        receipt["resources"]["database"]["ownerRunId"] = "other-run"
        receipt["resources"]["objectStore"]["ownerRunId"] = "other-run"
    elif mismatch == "environment":
        receipt["releaseRun"]["environmentId"] = "other-environment"
    elif mismatch == "disposition":
        receipt["resources"]["database"]["disposition"] = "retained-audit"
    elif mismatch == "database-identity":
        receipt["resources"]["database"]["identitySha256"] = "3" * 64
    elif mismatch == "object-identity":
        receipt["resources"]["objectStore"]["identitySha256"] = "4" * 64
    body = _canonical(receipt)
    receipt_sha256 = hashlib.sha256(body).hexdigest()
    if mismatch == "digest":
        receipt_sha256 = "9" * 64

    with pytest.raises(ValueError, match="provisioning receipt"):
        contract.parse_target_provisioning_receipt(
            body,
            provisioning_receipt_sha256=receipt_sha256,
            candidate_sha256=candidate_sha256,
            release_run=_release_run(),
            database_disposition="runner-owned-disposable",
            object_store_disposition="runner-owned-disposable",
            database_identity_sha256=database_identity_sha256,
            object_store_identity_sha256=object_store_identity_sha256,
        )


def test_direct_restore_operator_rejects_omitted_measured_evidence_before_target_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore = _restore_module()
    target_access: list[str] = []
    backup = SimpleNamespace(object_payloads=(), object_inventory=())
    target = SimpleNamespace(
        database_user="yfeistai_migrator",
        database_url="postgresql+asyncpg://restore-target",
    )

    def forbidden_engine_factory(_url: str) -> object:
        target_access.append("database-engine")
        raise AssertionError("target database engine must not be created")

    def forbidden_object_client_factory(_target: object) -> object:
        target_access.append("object-client")
        raise AssertionError("target object client must not be created")

    async def unused(*_args: object, **_kwargs: object):
        raise AssertionError("restore mutation must not be reached")

    runtime = restore.RestoreOperatorRuntime(
        target_loader=lambda _config, _secrets: target,
        engine_factory=forbidden_engine_factory,
        object_client_factory=forbidden_object_client_factory,
        database_probe=unused,
        facts_inspector=unused,
        process_runner=unused,
        receipt_rebinder=unused,
        app_access_granter=unused,
        app_engine_factory=forbidden_engine_factory,
        app_access_probe=unused,
    )
    monkeypatch.setattr(restore, "_load_verified_backup", lambda _path: backup)

    with pytest.raises(ValueError, match="measured target evidence.*required"):
        asyncio.run(
            restore.run_restore_operator(
                backup_dir=tmp_path / "source-backup",
                target_config=tmp_path / "target-config.json",
                target_secret_dir=tmp_path / "target-secrets",
                run_id="run-unmeasured-direct-operator",
                report_path=tmp_path / "restore-validation.json",
                runtime=runtime,
            )
        )

    assert target_access == []
    assert not (tmp_path / "restore-validation.json").exists()


def test_operator_and_probe_replay_measured_target_observations_under_held_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore = _restore_module()
    target_config_body = b"{}\n"
    target_config_sha256 = hashlib.sha256(target_config_body).hexdigest()
    candidate_sha256 = hashlib.sha256(_canonical(_candidate())).hexdigest()
    environment_id = _release_run()["environmentId"]
    target_object_identity = _module().physical_object_store_identity_sha256(
        "https://restore-objects.internal",
        "us-east-1",
        "restore-bucket",
        "8" * 64,
    )
    provisioning_receipt_body = _canonical(
        _provisioning_receipt(
            TARGET_DATABASE_IDENTITY_SHA256,
            target_object_identity,
        )
    )
    provisioning_receipt_sha256 = hashlib.sha256(provisioning_receipt_body).hexdigest()
    provisioning_receipt = tmp_path / "target-provisioning-receipt.json"
    provisioning_receipt.write_bytes(provisioning_receipt_body)
    target = restore.RestoreTarget(
        database_url="postgresql+asyncpg://restore-target",
        app_database_url="postgresql+asyncpg://restore-app",
        database_host="restore-db.internal",
        database_port=5432,
        database_name="restore-db-run-01",
        database_user="yfeistai_migrator",
        database_password="database-secret",
        object_endpoint="https://restore-objects.internal",
        object_namespace_id="restore-objects-run-01",
        object_bucket="restore-bucket",
        object_region="us-east-1",
        object_access_key="object-access-secret",
        object_secret_key="object-secret",
    )
    database_states = iter(
        [
            SimpleNamespace(
                identity_sha256=TARGET_DATABASE_IDENTITY_SHA256,
                user_object_count=0,
                current_role="yfeistai_migrator",
                database_owner="yfeistai_migrator",
            ),
            SimpleNamespace(
                identity_sha256=TARGET_DATABASE_IDENTITY_SHA256,
                user_object_count=0,
                current_role="yfeistai_migrator",
                database_owner="yfeistai_migrator",
            ),
            SimpleNamespace(
                identity_sha256=TARGET_DATABASE_IDENTITY_SHA256,
                user_object_count=17,
                current_role="yfeistai_migrator",
                database_owner="yfeistai_migrator",
            ),
        ]
    )
    object_states = iter(
        [
            SimpleNamespace(
                identity_sha256=target_object_identity,
                versioning_enabled=True,
                object_count=0,
                version_count=0,
                delete_marker_count=0,
                owner_id_sha256="8" * 64,
            ),
            SimpleNamespace(
                identity_sha256=target_object_identity,
                versioning_enabled=True,
                object_count=2,
                version_count=2,
                delete_marker_count=0,
                owner_id_sha256="8" * 64,
            ),
        ]
    )
    inventory = (
        SimpleNamespace(version_id="source-v1", payload_file="objects/one"),
        SimpleNamespace(version_id="source-v2", payload_file="objects/two"),
    )
    backup = SimpleNamespace(
        manifest=SimpleNamespace(
            database=SimpleNamespace(identity_sha256=SOURCE_DATABASE_IDENTITY_SHA256),
            source_object_store_identity_sha256=SOURCE_OBJECT_IDENTITY_SHA256,
        ),
        object_inventory=inventory,
        object_payloads=(object(), object()),
        database_dump=tmp_path / "database.dump",
        archive_fingerprint_sha256=ARCHIVE_SHA256,
        manifest_sha256=MANIFEST_SHA256,
    )
    events: list[str] = []
    exclusion_active = False
    exclusion_identity: list[str] = []
    object_claim_calls: list[dict[str, object]] = []
    database_probe_calls = 0

    @asynccontextmanager
    async def target_exclusion(_engine: object, identity_sha256: str):
        nonlocal exclusion_active
        assert exclusion_active is False
        exclusion_active = True
        exclusion_identity.append(identity_sha256)
        events.append("lock-enter")
        try:
            yield
        finally:
            exclusion_active = False
            events.append("lock-exit")

    async def database_probe(_engine: object) -> object:
        nonlocal database_probe_calls
        state = next(database_states)
        if database_probe_calls == 0:
            assert exclusion_active is False
            events.append("database-identity")
        else:
            assert exclusion_active is True
            events.append("database-pre" if state.user_object_count == 0 else "database-post")
        database_probe_calls += 1
        return state

    async def object_state_probe(_client: object, _target: object) -> object:
        assert exclusion_active is True
        state = next(object_states)
        events.append("objects-pre" if state.object_count == 0 else "objects-post")
        return state

    async def restore_database(*_args: object, **_kwargs: object) -> None:
        assert exclusion_active is True
        events.append("database-mutate")

    restored_receipts = (SimpleNamespace(), SimpleNamespace())

    async def restore_objects(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        assert exclusion_active is True
        events.append("objects-mutate")
        return restored_receipts

    async def validate_restore(
        _manifest: object,
        *,
        restore_database,
        restore_objects,
        inspect_restored_facts,
        object_prefix: str,
        object_inventory: tuple[object, ...],
        target_database_identity_sha256: str,
    ):
        await restore_database()
        await restore_objects(object_prefix, object_inventory)
        await inspect_restored_facts()
        return restore.RestoreValidationReport(
            ok=True,
            target_database_identity_sha256=target_database_identity_sha256,
            object_prefix=object_prefix,
            validated=tuple(_restore_payload()["validated"]),
            failures=(),
        )

    class Resource:
        async def dispose(self) -> None:
            return None

    class ObjectServiceModel:
        def operation_model(self, name: str) -> object:
            assert name == "PutObject"
            return SimpleNamespace(input_shape=SimpleNamespace(members={"IfNoneMatch": object()}))

    class ObjectClient:
        meta = SimpleNamespace(service_model=ObjectServiceModel())

        def get_bucket_versioning(self, **_arguments: object) -> dict[str, object]:
            assert exclusion_active is True
            return {"Status": "Enabled"}

        def list_object_versions(self, **_arguments: object) -> dict[str, object]:
            assert exclusion_active is True
            return {"IsTruncated": False, "Versions": [], "DeleteMarkers": []}

        def put_object(self, **arguments: object) -> dict[str, object]:
            assert exclusion_active is True
            events.append("object-claim")
            object_claim_calls.append(arguments)
            return {"ETag": '"claim-etag"', "VersionId": "claim-version-1"}

        def close(self) -> None:
            assert exclusion_active is False
            events.append("client-close")

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    async def facts(_engine: object) -> object:
        assert exclusion_active is True
        events.append("facts")
        return SimpleNamespace()

    async def access(_engine: object) -> bool:
        return True

    runtime = restore.RestoreOperatorRuntime(
        target_loader=lambda _config, _secrets: target,
        engine_factory=lambda _url: Resource(),
        object_client_factory=lambda _target: ObjectClient(),
        database_probe=database_probe,
        facts_inspector=facts,
        process_runner=noop,
        receipt_rebinder=noop,
        app_access_granter=noop,
        app_engine_factory=lambda _url: Resource(),
        app_access_probe=access,
        target_exclusion=target_exclusion,
        target_exclusion_mode="postgresql-session-advisory-lock",
        object_state_probe=object_state_probe,
    )
    target_config = tmp_path / "target-config.snapshot.json"
    target_config.write_bytes(target_config_body)
    operator_report = tmp_path / "restore-validation.json"
    monkeypatch.setattr(restore, "_load_verified_backup", lambda _path: backup)
    monkeypatch.setattr(restore, "_reverify_verified_backup", lambda current: current)
    monkeypatch.setattr(restore, "_validate_restore_inputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(restore, "_restore_database_dump", restore_database)
    monkeypatch.setattr(restore, "_restore_inventory_objects", restore_objects)
    monkeypatch.setattr(restore, "validate_teaching_restore", validate_restore)

    async def exact_prefix(*_args: object, **kwargs: object) -> bool:
        assert exclusion_active is True
        control_claim = kwargs.get("required_control_claim")
        assert control_claim is not None
        assert getattr(control_claim, "version_id", None) == "claim-version-1"
        marker_body = object_claim_calls[0]["Body"]
        assert getattr(control_claim, "body", None) == marker_body
        assert (
            getattr(control_claim, "body_sha256", None) == hashlib.sha256(marker_body).hexdigest()
        )
        events.append("control-readback")
        return True

    monkeypatch.setattr(restore, "_restored_object_prefix_is_exact", exact_prefix)

    asyncio.run(
        restore.run_restore_operator(
            backup_dir=tmp_path / "source-backup",
            target_config=target_config,
            provisioning_receipt=provisioning_receipt,
            provisioning_receipt_sha256=provisioning_receipt_sha256,
            target_secret_dir=tmp_path / "target-secrets",
            run_id="run-backup-restore-01",
            report_path=operator_report,
            target_config_sha256=target_config_sha256,
            database_ownership="runner-owned-disposable",
            object_namespace_ownership="runner-owned-disposable",
            candidate_sha256=candidate_sha256,
            environment_id=environment_id,
            runtime=runtime,
        )
    )

    assert len(object_claim_calls) == 1
    object_claim = object_claim_calls[0]
    assert set(object_claim) == {
        "Body",
        "Bucket",
        "ContentType",
        "IfNoneMatch",
        "Key",
    }
    assert object_claim["Bucket"] == "restore-bucket"
    assert object_claim["Key"] == ".yfeistai-backup-restore-control/claim.json"
    assert object_claim["ContentType"] == "application/json"
    assert object_claim["IfNoneMatch"] == "*"
    marker_body = object_claim["Body"]
    assert isinstance(marker_body, bytes)
    marker = json.loads(marker_body)
    assert marker_body == _canonical(marker)
    assert marker == {
        "candidateSha256": candidate_sha256,
        "provisioningReceiptSha256": provisioning_receipt_sha256,
        "releaseRun": {
            "environmentId": environment_id,
            "runId": "run-backup-restore-01",
        },
        "schemaVersion": 1,
        "target": {
            "databaseIdentitySha256": TARGET_DATABASE_IDENTITY_SHA256,
            "objectStoreIdentitySha256": target_object_identity,
        },
    }

    operator_body = operator_report.read_bytes()
    operator_payload = json.loads(operator_body)
    assert operator_body == _canonical(operator_payload)
    assert operator_payload["database"] == {
        "dumpRestoreSingleTransaction": True,
        "postRestoreMutationsAtomic": False,
    }
    assert operator_payload["crossSystemAtomic"] is False
    target_evidence = operator_payload["target"]
    assert target_evidence["targetConfigSha256"] == target_config_sha256
    assert target_evidence["provisioningReceiptSha256"] == provisioning_receipt_sha256
    assert target_evidence["database"]["pre"]["userObjectCount"] == 0
    assert target_evidence["database"]["post"]["userObjectCount"] == 17
    assert target_evidence["objects"]["pre"]["versionCount"] == 0
    assert target_evidence["objects"]["pre"]["deleteMarkerCount"] == 0
    assert target_evidence["objects"]["post"]["objectCount"] == 2
    assert target_evidence["objects"]["post"]["versionCount"] == 2
    assert target_evidence["objects"]["post"]["deleteMarkerCount"] == 0
    assert target_evidence["objects"]["pre"]["ownerIdSha256"] == "8" * 64
    assert target_evidence["objects"]["post"]["ownerIdSha256"] == "8" * 64
    assert target_evidence["concurrencyExclusion"] == {
        "heldThroughPostValidation": True,
        "identitySha256": exclusion_identity[0],
        "mode": "postgresql-session-advisory-lock",
    }
    assert exclusion_identity == [TARGET_DATABASE_IDENTITY_SHA256]
    assert events == [
        "database-identity",
        "lock-enter",
        "database-pre",
        "objects-pre",
        "object-claim",
        "database-mutate",
        "objects-mutate",
        "facts",
        "database-post",
        "objects-post",
        "control-readback",
        "lock-exit",
        "client-close",
    ]

    probe_root = tmp_path / "probe"
    probe_root.mkdir()
    config, probe_backup, _probe_target = _fixture(probe_root)

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        Path(arguments[arguments.index("--report") + 1]).write_bytes(operator_body)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    outer_report_path = _module().run_backup_restore_probe(
        config,
        verified_backup_loader=lambda path: _snapshot_backup(probe_backup, path),
        verified_backup_rechecker=lambda current: current,
        target_config_loader=lambda _path: target,
        command_runner=runner,
    )
    outer_report = json.loads(outer_report_path.read_bytes())
    assert outer_report["consistency"] == {
        "databaseSnapshot": "postgresql-consistent-dump",
        "objectSnapshot": "version-pinned-inventory",
        "crossSystemAtomic": False,
        "partialBackupArtifacts": "retained",
    }
    assert outer_report["findings"]["database"]["dumpRestoreSingleTransaction"] is True
    assert outer_report["findings"]["database"]["postRestoreMutationsAtomic"] is False
    assert "singleTransaction" not in outer_report["findings"]["database"]
    assert (
        outer_report["target"]["operatorTargetObservationsSha256"]
        == hashlib.sha256(_canonical(target_evidence)).hexdigest()
    )
    assert outer_report["target"]["provisioningReceiptSha256"] == provisioning_receipt_sha256
    assert (
        outer_report["execution"]["artifactSha256s"]["targetProvisioningReceipt"]
        == provisioning_receipt_sha256
    )
    assert outer_report["findings"]["database"]["postRestoreUserObjectCount"] == 17
    assert outer_report["findings"]["objects"]["postRestoreVersionCount"] == 2
    with pytest.raises(ValueError, match="operator artifact.*required"):
        _contract().parse_backup_restore_report(
            outer_report_path.read_bytes(),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_source_manifest_sha256=MANIFEST_SHA256,
            expected_source_archive_fingerprint_sha256=ARCHIVE_SHA256,
            expected_database_ownership="runner-owned-disposable",
            expected_object_namespace_ownership="runner-owned-disposable",
            verified_backup=probe_backup,
            source_provenance_body=_canonical(_source_provenance()),
            target_config_body=target_config_body,
            provisioning_receipt_body=provisioning_receipt_body,
            forbidden_secret_values=(SECRET_VALUE,),
        )
    unbound_config_report = copy.deepcopy(outer_report)
    unbound_config_report["execution"]["artifactSha256s"].pop("targetConfigSnapshot")
    with pytest.raises(ValueError, match="target config.*required"):
        _contract().parse_backup_restore_report(
            _canonical(unbound_config_report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_source_manifest_sha256=MANIFEST_SHA256,
            expected_source_archive_fingerprint_sha256=ARCHIVE_SHA256,
            expected_database_ownership="runner-owned-disposable",
            expected_object_namespace_ownership="runner-owned-disposable",
            operator_artifact_body=operator_body,
            verified_backup=probe_backup,
            source_provenance_body=_canonical(_source_provenance()),
            provisioning_receipt_body=provisioning_receipt_body,
            forbidden_secret_values=(SECRET_VALUE,),
        )
    with pytest.raises(ValueError, match="verified backup.*required"):
        _contract().parse_backup_restore_report(
            outer_report_path.read_bytes(),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_source_manifest_sha256=MANIFEST_SHA256,
            expected_source_archive_fingerprint_sha256=ARCHIVE_SHA256,
            expected_database_ownership="runner-owned-disposable",
            expected_object_namespace_ownership="runner-owned-disposable",
            operator_artifact_body=operator_body,
            source_provenance_body=_canonical(_source_provenance()),
            target_config_body=target_config_body,
            provisioning_receipt_body=provisioning_receipt_body,
            forbidden_secret_values=(SECRET_VALUE,),
        )
    unbound_report = copy.deepcopy(outer_report)
    unbound_report["source"].pop("provenanceSha256")
    unbound_report["execution"]["artifactSha256s"].pop("sourceProvenance")
    with pytest.raises(ValueError, match="source provenance.*required"):
        _contract().parse_backup_restore_report(
            _canonical(unbound_report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_source_manifest_sha256=MANIFEST_SHA256,
            expected_source_archive_fingerprint_sha256=ARCHIVE_SHA256,
            expected_database_ownership="runner-owned-disposable",
            expected_object_namespace_ownership="runner-owned-disposable",
            operator_artifact_body=operator_body,
            verified_backup=probe_backup,
            target_config_body=target_config_body,
            provisioning_receipt_body=provisioning_receipt_body,
            forbidden_secret_values=(SECRET_VALUE,),
        )
    legacy_report = copy.deepcopy(outer_report)
    legacy_target_fields = {
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
    legacy_report["target"] = {
        field: legacy_report["target"][field] for field in legacy_target_fields
    }
    for field in ("preRestoreUserObjectCount", "postRestoreUserObjectCount"):
        legacy_report["findings"]["database"].pop(field)
    for field in (
        "preRestoreObjectCount",
        "postRestoreObjectCount",
        "preRestoreVersionCount",
        "postRestoreVersionCount",
        "preRestoreDeleteMarkerCount",
        "postRestoreDeleteMarkerCount",
    ):
        legacy_report["findings"]["objects"].pop(field)
    legacy_operator = copy.deepcopy(operator_payload)
    legacy_operator["schemaVersion"] = 2
    legacy_operator.pop("target")
    legacy_operator_body = _canonical(legacy_operator)
    legacy_operator_sha256 = hashlib.sha256(legacy_operator_body).hexdigest()
    legacy_report["execution"]["commands"][0]["artifactSha256"] = legacy_operator_sha256
    legacy_report["execution"]["artifactSha256s"]["restoreValidation"] = legacy_operator_sha256
    with pytest.raises(ValueError, match="rich target.*required"):
        _contract().parse_backup_restore_report(
            _canonical(legacy_report),
            candidate=_candidate(),
            release_run=_release_run(),
            expected_source_manifest_sha256=MANIFEST_SHA256,
            expected_source_archive_fingerprint_sha256=ARCHIVE_SHA256,
            expected_database_ownership="runner-owned-disposable",
            expected_object_namespace_ownership="runner-owned-disposable",
            operator_artifact_body=legacy_operator_body,
            verified_backup=probe_backup,
            source_provenance_body=_canonical(_source_provenance()),
            target_config_body=target_config_body,
            provisioning_receipt_body=provisioning_receipt_body,
            forbidden_secret_values=(SECRET_VALUE,),
        )


def test_object_restore_claim_fails_closed_without_conditional_create_support() -> None:
    restore = _restore_module()
    put_calls: list[dict[str, object]] = []

    class UnsupportedServiceModel:
        def operation_model(self, name: str) -> object:
            assert name == "PutObject"
            return SimpleNamespace(
                input_shape=SimpleNamespace(
                    members={"Body": object(), "Bucket": object(), "Key": object()}
                )
            )

    class ObjectClient:
        meta = SimpleNamespace(service_model=UnsupportedServiceModel())

        def put_object(self, **arguments: object) -> dict[str, object]:
            put_calls.append(arguments)
            return {"ETag": '"ignored-condition"', "VersionId": "claim-version-1"}

    with pytest.raises(RuntimeError, match="atomic conditional create"):
        asyncio.run(
            restore._claim_object_restore_control(
                ObjectClient(),
                bucket="restore-bucket",
                candidate_sha256=hashlib.sha256(_canonical(_candidate())).hexdigest(),
                provisioning_receipt_sha256="3" * 64,
                run_id=_release_run()["runId"],
                environment_id=_release_run()["environmentId"],
                database_identity_sha256=TARGET_DATABASE_IDENTITY_SHA256,
                object_store_identity_sha256="2" * 64,
            )
        )
    assert put_calls == []


def test_object_restore_claim_classifies_existing_key_before_mutation() -> None:
    restore = _restore_module()
    events: list[str] = []

    class ServiceModel:
        def operation_model(self, name: str) -> object:
            assert name == "PutObject"
            return SimpleNamespace(input_shape=SimpleNamespace(members={"IfNoneMatch": object()}))

    class ClaimCollision(Exception):
        response = {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        }

    class ObjectClient:
        meta = SimpleNamespace(service_model=ServiceModel())

        def put_object(self, **_arguments: object) -> dict[str, object]:
            events.append("claim")
            raise ClaimCollision

    async def claim_then_mutate() -> None:
        await restore._claim_object_restore_control(
            ObjectClient(),
            bucket="restore-bucket",
            candidate_sha256=hashlib.sha256(_canonical(_candidate())).hexdigest(),
            provisioning_receipt_sha256="3" * 64,
            run_id=_release_run()["runId"],
            environment_id=_release_run()["environmentId"],
            database_identity_sha256=TARGET_DATABASE_IDENTITY_SHA256,
            object_store_identity_sha256="2" * 64,
        )
        events.append("mutation")

    with pytest.raises(RuntimeError, match="already claimed"):
        asyncio.run(claim_then_mutate())
    assert events == ["claim"]


def test_reserved_object_claim_version_history_drift_is_rejected() -> None:
    restore = _restore_module()
    marker_body = _canonical(
        {
            "candidateSha256": "1" * 64,
            "provisioningReceiptSha256": "2" * 64,
            "releaseRun": {
                "environmentId": "environment-control-history",
                "runId": "run-control-history",
            },
            "schemaVersion": 1,
            "target": {
                "databaseIdentitySha256": "3" * 64,
                "objectStoreIdentitySha256": "4" * 64,
            },
        }
    )
    control_claim = restore._ObjectRestoreControlClaim(
        version_id="claim-version-1",
        body=marker_body,
        body_sha256=hashlib.sha256(marker_body).hexdigest(),
    )

    class ObjectClient:
        include_older_version = False
        readback_calls = 0

        def list_object_versions(self, **_arguments: object) -> dict[str, object]:
            versions = [
                {
                    "Key": ".yfeistai-backup-restore-control/claim.json",
                    "VersionId": "claim-version-1",
                    "IsLatest": True,
                }
            ]
            if self.include_older_version:
                versions.append(
                    {
                        "Key": ".yfeistai-backup-restore-control/claim.json",
                        "VersionId": "unexpected-older-version",
                        "IsLatest": False,
                    }
                )
            return {
                "IsTruncated": False,
                "Versions": versions,
                "DeleteMarkers": [],
            }

        def get_object(self, **arguments: object) -> dict[str, object]:
            assert arguments == {
                "Bucket": "restore-bucket",
                "Key": ".yfeistai-backup-restore-control/claim.json",
                "VersionId": "claim-version-1",
            }
            self.readback_calls += 1
            return {"Body": io.BytesIO(marker_body)}

    client = ObjectClient()

    async def exact() -> bool:
        return await restore._restored_object_prefix_is_exact(
            client,
            bucket="restore-bucket",
            prefix="",
            expected_receipts=(),
            required_control_claim=control_claim,
        )

    assert asyncio.run(exact()) is True
    assert client.readback_calls == 1

    client.include_older_version = True
    assert asyncio.run(exact()) is False
    assert client.readback_calls == 1


@pytest.mark.parametrize(
    "readback_outcome",
    ["matching", "missing-body", "replaced-body", "read-error"],
)
def test_reserved_object_claim_reads_back_exact_version_and_body_before_unlock_and_close(
    readback_outcome: str,
) -> None:
    restore = _restore_module()
    events: list[str] = []
    claim_bodies: list[bytes] = []
    exclusion_active = False

    class ServiceModel:
        def operation_model(self, name: str) -> object:
            assert name == "PutObject"
            return SimpleNamespace(input_shape=SimpleNamespace(members={"IfNoneMatch": object()}))

    class StoredBody(io.BytesIO):
        read_started = False

        def read(self, size: int = -1) -> bytes:
            assert exclusion_active is True
            if not self.read_started:
                events.append("body-read")
                self.read_started = True
            return super().read(size)

        def close(self) -> None:
            assert exclusion_active is True
            events.append("body-close")
            super().close()

    class ObjectClient:
        meta = SimpleNamespace(service_model=ServiceModel())

        def put_object(self, **arguments: object) -> dict[str, str]:
            assert exclusion_active is True
            assert arguments["IfNoneMatch"] == "*"
            body = arguments["Body"]
            assert isinstance(body, bytes)
            claim_bodies.append(body)
            events.append("claim")
            return {"ETag": '"claim-etag"', "VersionId": "claim-version-1"}

        def list_object_versions(self, **arguments: object) -> dict[str, object]:
            assert exclusion_active is True
            assert arguments["Bucket"] == "restore-bucket"
            events.append("history")
            return {
                "IsTruncated": False,
                "Versions": [
                    {
                        "Key": ".yfeistai-backup-restore-control/claim.json",
                        "VersionId": "claim-version-1",
                        "IsLatest": True,
                    }
                ],
                "DeleteMarkers": [],
            }

        def get_object(self, **arguments: object) -> dict[str, object]:
            assert exclusion_active is True
            assert arguments == {
                "Bucket": "restore-bucket",
                "Key": ".yfeistai-backup-restore-control/claim.json",
                "VersionId": "claim-version-1",
            }
            events.append("readback")
            if readback_outcome == "read-error":
                raise OSError("stored control marker is unavailable")
            if readback_outcome == "missing-body":
                return {}
            stored_body = claim_bodies[0] if readback_outcome == "matching" else b"{}\n"
            return {"Body": StoredBody(stored_body)}

        def close(self) -> None:
            assert exclusion_active is False
            events.append("client-close")

    client = ObjectClient()

    @asynccontextmanager
    async def held_exclusion():
        nonlocal exclusion_active
        exclusion_active = True
        events.append("lock")
        try:
            yield
        finally:
            exclusion_active = False
            events.append("unlock")

    async def scenario() -> None:
        try:
            async with held_exclusion():
                control_claim = await restore._claim_object_restore_control(
                    client,
                    bucket="restore-bucket",
                    candidate_sha256="1" * 64,
                    provisioning_receipt_sha256="2" * 64,
                    run_id="run-object-control-readback",
                    environment_id="environment-object-control-readback",
                    database_identity_sha256="3" * 64,
                    object_store_identity_sha256="4" * 64,
                )
                claim_body = claim_bodies[0]
                assert claim_body == _canonical(json.loads(claim_body))
                try:
                    exact = await restore._restored_object_prefix_is_exact(
                        client,
                        bucket="restore-bucket",
                        prefix="",
                        expected_receipts=(),
                        required_control_claim=control_claim,
                    )
                except RuntimeError:
                    exact = False
                assert exact is (readback_outcome == "matching")
        finally:
            await restore._close_resource(client)

    asyncio.run(scenario())

    assert events.index("claim") < events.index("history") < events.index("readback")
    assert events.index("readback") < events.index("unlock") < events.index("client-close")
    if readback_outcome in {"matching", "replaced-body"}:
        assert events.index("body-read") < events.index("body-close") < events.index("unlock")
    else:
        assert "body-read" not in events
        assert "body-close" not in events


def test_object_restore_claim_rejects_blank_receipt_before_mutation() -> None:
    restore = _restore_module()
    events: list[str] = []

    class ServiceModel:
        def operation_model(self, name: str) -> object:
            assert name == "PutObject"
            return SimpleNamespace(input_shape=SimpleNamespace(members={"IfNoneMatch": object()}))

    class ObjectClient:
        meta = SimpleNamespace(service_model=ServiceModel())

        def put_object(self, **_arguments: object) -> dict[str, object]:
            events.append("claim")
            return {"ETag": " \t", "VersionId": " \n"}

    async def claim_then_mutate() -> None:
        await restore._claim_object_restore_control(
            ObjectClient(),
            bucket="restore-bucket",
            candidate_sha256=hashlib.sha256(_canonical(_candidate())).hexdigest(),
            provisioning_receipt_sha256="3" * 64,
            run_id=_release_run()["runId"],
            environment_id=_release_run()["environmentId"],
            database_identity_sha256=TARGET_DATABASE_IDENTITY_SHA256,
            object_store_identity_sha256="2" * 64,
        )
        events.append("mutation")

    with pytest.raises(RuntimeError, match="returned no receipt"):
        asyncio.run(claim_then_mutate())
    assert events == ["claim"]


def test_probe_rejects_source_target_object_namespace_identity_reuse(tmp_path: Path) -> None:
    config, backup, target = _fixture(tmp_path)
    source_identity = _module().physical_object_store_identity_sha256(
        target.object_store_endpoint,
        target.object_store_region,
        target.object_store_bucket,
        "8" * 64,
    )
    backup.manifest.source_object_store_identity_sha256 = source_identity
    config.source_provenance["source"]["objectStoreIdentitySha256"] = source_identity
    restore_payload = _restore_payload()
    assert restore_payload["target"]["objects"]["identitySha256"] == source_identity
    receipt = json.loads(config.provisioning_receipt_path.read_bytes())
    assert receipt["resources"]["objectStore"]["identitySha256"] == source_identity
    runner_calls: list[list[str]] = []

    def runner(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        runner_calls.append(arguments)
        _write_restore_report(arguments, restore_payload)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    with pytest.raises(ValueError, match="operator target observations"):
        _module().run_backup_restore_probe(
            config,
            verified_backup_loader=lambda path: _snapshot_backup(backup, path),
            verified_backup_rechecker=lambda current: current,
            target_config_loader=lambda path: target,
            command_runner=runner,
        )

    assert len(runner_calls) == 1
    assert not (config.output_directory / "backup-restore-report.json").exists()


def test_probe_source_has_no_docker_or_destructive_cleanup_primitive() -> None:
    source = (ROOT / "scripts" / "backup_restore_probe.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    unlink_calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "unlink"
    ]
    unlink_receivers = [ast.unparse(call.func.value) for call in unlink_calls]

    assert "import docker" not in source
    assert "shutil.rmtree" not in source
    assert unlink_receivers == ["snapshot_directory / item.name"]
    assert "os.remove(" not in source
    assert "os.unlink(" not in source
    assert "Remove-Item" not in source
