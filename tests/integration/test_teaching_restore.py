from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from deeptutor.teaching.secret_permissions import secret_file_is_restricted

_ROOT = Path(__file__).resolve().parents[2]


def _load_script(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BACKUP = _load_script("backup_teaching_restore_test", "backup_teaching.py")
_RESTORE = _load_script("restore_teaching_validation_under_test", "restore_teaching_validation.py")
_SOURCE_OBJECT_STORE_NAMESPACE_ID = "source-objects-primary"
_SOURCE_OBJECT_STORE_BUCKET = "source-teaching"
_SOURCE_OBJECT_STORE_IDENTITY = _BACKUP.object_store_identity_sha256(
    _SOURCE_OBJECT_STORE_NAMESPACE_ID,
    _SOURCE_OBJECT_STORE_BUCKET,
)
BackupManifest = _BACKUP.BackupManifest
DatabaseBackup = _BACKUP.DatabaseBackup
ObjectInventoryEntry = _BACKUP.ObjectInventoryEntry
RestorableObjectInventoryEntry = _BACKUP.RestorableObjectInventoryEntry
inventory_sha256 = _BACKUP.inventory_sha256
write_backup_manifest = _BACKUP.write_backup_manifest
RestoredTeachingFacts = _RESTORE.RestoredTeachingFacts
DatabaseObjectReference = _RESTORE.DatabaseObjectReference
validate_teaching_restore = _RESTORE.validate_teaching_restore


def _inventory() -> tuple[ObjectInventoryEntry, ...]:
    return (
        ObjectInventoryEntry(
            tenant_id="tenant-a",
            key="tenants/tenant-a/classrooms/a/document.json",
            sha256="c" * 64,
            size=10,
        ),
    )


def _manifest(
    inventory: tuple[ObjectInventoryEntry, ...] | None = None,
) -> BackupManifest:
    entries = _inventory() if inventory is None else inventory
    return BackupManifest(
        schema_version=3,
        created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
        database=DatabaseBackup(
            file="database.dump",
            sha256=hashlib.sha256(b"dump").hexdigest(),
            size=4,
            identity_sha256="a" * 64,
        ),
        object_inventory_file="objects.json",
        object_inventory_sha256=inventory_sha256(entries),
        object_count=len(entries),
        source_object_store_identity_sha256=_SOURCE_OBJECT_STORE_IDENTITY,
        platform_schema_revision="platform-revision",
        schema_revisions={"tenant-a": "20260810_0017"},
        classroom_versions_count=2,
        learning_events_count=5,
    )


def _restorable_backup(
    tmp_path: Path,
    *,
    source_object_store_namespace_id: str = _SOURCE_OBJECT_STORE_NAMESPACE_ID,
    source_object_store_bucket: str = _SOURCE_OBJECT_STORE_BUCKET,
    source_object_store_identity_sha256: str | None = None,
) -> tuple[Path, tuple[RestorableObjectInventoryEntry, ...], bytes]:
    payload = b"restored classroom object"
    object_key = "tenants/tenant-a/classrooms/a/document.json"
    payload_file = f"objects/{hashlib.sha256(object_key.encode('utf-8')).hexdigest()}.blob"
    inventory = (
        RestorableObjectInventoryEntry(
            tenant_id="tenant-a",
            key=object_key,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            version_id="object-version-1",
            payload_file=payload_file,
            content_type="application/json",
            owner_token="1" * 32,
            source_revision="source-etag-1",
        ),
    )
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    database_dump = backup_dir / "database.dump"
    database_dump.write_bytes(b"dump")
    object_payload = backup_dir / payload_file
    object_payload.parent.mkdir()
    object_payload.write_bytes(payload)
    write_backup_manifest(
        backup_dir,
        database_dump=database_dump,
        database_identity_sha256="a" * 64,
        object_inventory=inventory,
        source_object_store_namespace_id=source_object_store_namespace_id,
        source_object_store_bucket=source_object_store_bucket,
        source_object_store_identity_sha256=source_object_store_identity_sha256,
        platform_schema_revision="platform-revision",
        schema_revisions={"tenant-a": "20260810_0017"},
        classroom_versions_count=2,
        learning_events_count=5,
        created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
    )
    return backup_dir, inventory, payload


def _restore_target():
    return _RESTORE.RestoreTarget(
        database_url=("postgresql+asyncpg://yfeistai_migrator:restore-password@db/restore_target"),
        app_database_url=("postgresql+asyncpg://yfeistai_app:app-password@db/restore_target"),
        database_host="db",
        database_port=5432,
        database_name="restore_target",
        database_user="yfeistai_migrator",
        database_password="restore-password",
        object_endpoint="http://objects:9000",
        object_namespace_id="restore-objects-primary",
        object_bucket="restore-bucket",
        object_region="us-east-1",
        object_access_key="restore-access",
        object_secret_key="restore-secret",
    )


_TARGET_DATABASE_IDENTITY_SHA256 = "d" * 64
_TARGET_OBJECT_IDENTITY_SHA256 = "e" * 64
_TARGET_OBJECT_OWNER_ID_SHA256 = "f" * 64


def _measured_restore_arguments(
    tmp_path: Path,
    *,
    run_id: str,
    target_config: Path | None = None,
) -> dict[str, object]:
    config = target_config or (tmp_path / "target.json")
    config.write_bytes(b"{}\n")
    config_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
    candidate_sha256 = "c" * 64
    environment_id = "restore-environment-01"
    receipt = tmp_path / f"{run_id}-target-provisioning-receipt.json"
    receipt.write_bytes(
        _BACKUP._canonical_json(
            {
                "schemaVersion": 1,
                "producer": "backup-restore-target-provisioner",
                "candidateSha256": candidate_sha256,
                "releaseRun": {
                    "runId": run_id,
                    "environmentId": environment_id,
                },
                "resources": {
                    "database": {
                        "identitySha256": _TARGET_DATABASE_IDENTITY_SHA256,
                        "ownerRunId": run_id,
                        "disposition": "runner-owned-disposable",
                    },
                    "objectStore": {
                        "identitySha256": _TARGET_OBJECT_IDENTITY_SHA256,
                        "ownerRunId": run_id,
                        "disposition": "runner-owned-disposable",
                    },
                },
            }
        )
    )
    return {
        "target_config": config,
        "target_config_sha256": config_sha256,
        "provisioning_receipt": receipt,
        "provisioning_receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "run_id": run_id,
        "environment_id": environment_id,
        "candidate_sha256": candidate_sha256,
        "database_ownership": "runner-owned-disposable",
        "object_namespace_ownership": "runner-owned-disposable",
    }


class _RuntimeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _RuntimeObjectClient:
    def __init__(
        self,
        calls: list[tuple[str, object]],
        *,
        prefix_count: int = 0,
        version_pages: list[dict[str, object]] | None = None,
        concurrent_key: str | None = None,
        versioning_status: str = "Enabled",
        put_version_id: str = "target-version-1",
    ) -> None:
        self.calls = calls
        self.meta = SimpleNamespace(
            service_model=SimpleNamespace(
                operation_model=lambda _name: SimpleNamespace(
                    input_shape=SimpleNamespace(members={"IfNoneMatch": object()})
                )
            )
        )
        self.prefix_count = prefix_count
        self.version_pages = list(version_pages or [])
        self.concurrent_key = concurrent_key
        self.versioning_status = versioning_status
        self.put_version_id = put_version_id
        self.objects: dict[str, bytes] = {}
        self.object_metadata: dict[str, dict[str, object]] = {}
        self.closed = False

    def get_bucket_versioning(self, **arguments):
        self.calls.append(("versioning", dict(arguments)))
        return {"Status": self.versioning_status}

    def list_object_versions(self, **arguments):
        self.calls.append(("list", dict(arguments)))
        if self.version_pages:
            return self.version_pages.pop(0)
        prefix = arguments["Prefix"]
        matching = [key for key in self.objects if key.startswith(prefix)]
        if self.prefix_count:
            matching.append(prefix + "occupied")
        return {
            "IsTruncated": False,
            "Versions": [
                {
                    "Key": key,
                    "VersionId": self.object_metadata.get(key, {}).get(
                        "VersionId",
                        "version",
                    ),
                    "IsLatest": True,
                }
                for key in matching
            ],
            "DeleteMarkers": [],
        }

    def get_object(self, **arguments):
        self.calls.append(("get", dict(arguments)))
        metadata = self.object_metadata[arguments["Key"]]
        assert arguments["VersionId"] == metadata["VersionId"]
        body = self.objects[arguments["Key"]]
        if self.concurrent_key is not None:
            self.objects[self.concurrent_key] = b"concurrent object"
        return {"Body": io.BytesIO(body)}

    def head_object(self, **arguments):
        self.calls.append(("head", dict(arguments)))
        metadata = self.object_metadata[arguments["Key"]]
        assert arguments["VersionId"] == metadata["VersionId"]
        return dict(metadata)

    def put_object(self, **arguments):
        captured = dict(arguments)
        body = captured.pop("Body")
        self.calls.append(("put", captured))
        assert captured["IfNoneMatch"] == "*"
        assert captured["Key"] not in self.objects
        payload = body if isinstance(body, bytes) else body.read()
        version_id = (
            "control-version-1"
            if captured["Key"] == _RESTORE._OBJECT_RESTORE_CONTROL_KEY
            else self.put_version_id
        )
        self.objects[captured["Key"]] = payload
        self.object_metadata[captured["Key"]] = {
            "ContentLength": captured.get("ContentLength", len(payload)),
            "ContentType": captured["ContentType"],
            "Metadata": dict(captured.get("Metadata", {})),
            "ETag": "target-etag-1",
            "VersionId": version_id,
        }
        return {"ETag": "target-etag-1", "VersionId": version_id}

    def close(self) -> None:
        self.closed = True


def test_restore_objects_use_canonical_keys_and_target_version_receipts(tmp_path: Path) -> None:
    backup_dir, inventory, payload = _restorable_backup(tmp_path)
    calls: list[tuple[str, object]] = []
    client = _RuntimeObjectClient(calls)

    receipts = asyncio.run(
        _RESTORE._restore_inventory_objects(
            client,
            bucket="restore-bucket",
            prefix="",
            inventory=inventory,
            payloads=(backup_dir / inventory[0].payload_file,),
        )
    )

    assert tuple(
        (
            receipt.tenant_id,
            receipt.key,
            receipt.sha256,
            receipt.size,
            receipt.content_type,
            receipt.owner_token,
            receipt.revision,
            receipt.version_id,
        )
        for receipt in receipts
    ) == (
        (
            "tenant-a",
            inventory[0].key,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "application/json",
            "1" * 32,
            "target-etag-1",
            "target-version-1",
        ),
    )
    assert client.objects == {inventory[0].key: payload}
    assert (
        "put",
        {
            "Bucket": "restore-bucket",
            "Key": inventory[0].key,
            "ContentLength": len(payload),
            "ContentType": "application/json",
            "Metadata": {
                "owner": "1" * 32,
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            "IfNoneMatch": "*",
        },
    ) in calls
    assert (
        "head",
        {
            "Bucket": "restore-bucket",
            "Key": inventory[0].key,
            "VersionId": "target-version-1",
        },
    ) in calls
    assert (
        "get",
        {
            "Bucket": "restore-bucket",
            "Key": inventory[0].key,
            "VersionId": "target-version-1",
        },
    ) in calls


def _successful_operator_runtime(
    calls: list[tuple[str, object]],
    engine: _RuntimeEngine,
    client: _RuntimeObjectClient,
):
    target = _restore_target()
    restored_receipts: list[object] = []

    def load_target(config: Path, secret_dir: Path):
        calls.append(("load_target", (config, secret_dir)))
        return target

    async def probe_database(_engine: _RuntimeEngine):
        calls.append(("probe_database", None))
        return _RESTORE.TargetDatabaseState(
            identity_sha256=_TARGET_DATABASE_IDENTITY_SHA256,
            user_object_count=(1 if any(name == "pg_restore" for name, _value in calls) else 0),
            current_role=target.database_user,
            database_owner=target.database_user,
        )

    async def probe_objects(_client, _target):
        restored_keys = {
            key for key in client.objects if key != _RESTORE._OBJECT_RESTORE_CONTROL_KEY
        }
        return _RESTORE.TargetObjectState(
            identity_sha256=_TARGET_OBJECT_IDENTITY_SHA256,
            versioning_enabled=True,
            object_count=len(restored_keys),
            version_count=len(restored_keys),
            delete_marker_count=0,
            owner_id_sha256=_TARGET_OBJECT_OWNER_ID_SHA256,
        )

    class Exclusion:
        async def __aenter__(self) -> None:
            calls.append(("target_exclusion_enter", None))

        async def __aexit__(self, *_args: object) -> None:
            calls.append(("target_exclusion_exit", None))

    def exclude_target(_engine, identity_sha256: str):
        assert identity_sha256 == _TARGET_DATABASE_IDENTITY_SHA256
        return Exclusion()

    async def inspect_facts(_engine: _RuntimeEngine) -> RestoredTeachingFacts:
        calls.append(("inspect", None))
        receipt = restored_receipts[0]
        return RestoredTeachingFacts(
            platform_schema_revision="platform-revision",
            schema_revisions={"tenant-a": "20260810_0017"},
            classroom_versions_count=2,
            learning_events_count=5,
            source_snapshot_links_valid=True,
            media_links_valid=True,
            quota_links_valid=True,
            audit_links_valid=True,
            database_object_references=(
                DatabaseObjectReference(
                    tenant_id=receipt.tenant_id,
                    key=receipt.key,
                    sha256=receipt.sha256,
                    size=receipt.size,
                    version_id=receipt.version_id,
                    content_type=receipt.content_type,
                    owner_token=receipt.owner_token,
                    source_revision=receipt.revision,
                ),
            ),
        )

    async def run_process(argv: tuple[str, ...], environment: dict[str, str]) -> int:
        calls.append(("pg_restore", (argv, dict(environment))))
        return 0

    async def rebind_receipts(_engine, receipts) -> None:
        restored_receipts.extend(receipts)
        calls.append(("rebind", receipts))

    async def grant_app_access(_engine) -> None:
        calls.append(("grant_app", None))

    def create_app_engine(database_url: str):
        app_engine = _RuntimeEngine()
        calls.append(("app_engine", (database_url, app_engine)))
        return app_engine

    async def probe_app_access(_engine) -> bool:
        calls.append(("probe_app", None))
        return True

    return _RESTORE.RestoreOperatorRuntime(
        target_loader=load_target,
        engine_factory=lambda _database_url: engine,
        object_client_factory=lambda _loaded_target: client,
        database_probe=probe_database,
        facts_inspector=inspect_facts,
        process_runner=run_process,
        receipt_rebinder=rebind_receipts,
        app_access_granter=grant_app_access,
        app_engine_factory=create_app_engine,
        app_access_probe=probe_app_access,
        target_exclusion=exclude_target,
        target_exclusion_mode="test-exclusive-target",
        object_state_probe=probe_objects,
    )


@dataclass
class RestoreHarness:
    calls: list[tuple[str, object]]

    async def restore_database(self) -> None:
        self.calls.append(("database", None))

    async def restore_objects(
        self,
        prefix: str,
        inventory: tuple[ObjectInventoryEntry, ...],
    ) -> None:
        self.calls.append(("objects", (prefix, inventory)))

    async def inspect(self) -> RestoredTeachingFacts:
        self.calls.append(("inspect", None))
        return RestoredTeachingFacts(
            platform_schema_revision="platform-revision",
            schema_revisions={"tenant-a": "20260810_0017"},
            classroom_versions_count=2,
            learning_events_count=5,
            source_snapshot_links_valid=True,
            media_links_valid=True,
            quota_links_valid=True,
            audit_links_valid=True,
        )


def test_restore_validation_uses_new_database_and_empty_bucket_without_mutating_source() -> None:
    harness = RestoreHarness(calls=[])
    inventory = _inventory()

    report = asyncio.run(
        validate_teaching_restore(
            _manifest(),
            target_database_identity_sha256="d" * 64,
            object_prefix="",
            object_inventory=inventory,
            restore_database=harness.restore_database,
            restore_objects=harness.restore_objects,
            inspect_restored_facts=harness.inspect,
        )
    )

    assert report.ok is True
    assert report.object_prefix == ""
    assert report.validated == (
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
    )
    assert [name for name, _ in harness.calls] == ["database", "objects", "inspect"]


@pytest.mark.parametrize(
    ("target_identity", "prefix", "message"),
    [
        ("a" * 64, "", "new database"),
        ("d" * 64, "tenants/tenant-a/", "object prefix must be empty"),
        ("d" * 64, "restore-validation/run-a/", "object prefix must be empty"),
    ],
)
def test_restore_rejects_destructive_targets_before_any_write(
    target_identity: str,
    prefix: str,
    message: str,
) -> None:
    harness = RestoreHarness(calls=[])

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            validate_teaching_restore(
                _manifest(),
                target_database_identity_sha256=target_identity,
                object_prefix=prefix,
                object_inventory=_inventory(),
                restore_database=harness.restore_database,
                restore_objects=harness.restore_objects,
                inspect_restored_facts=harness.inspect,
            )
        )

    assert harness.calls == []


def test_restore_report_fails_when_any_required_relationship_is_invalid() -> None:
    harness = RestoreHarness(calls=[])

    async def inspect() -> RestoredTeachingFacts:
        return RestoredTeachingFacts(
            platform_schema_revision="platform-revision",
            schema_revisions={"tenant-a": "20260810_0017"},
            classroom_versions_count=2,
            learning_events_count=5,
            source_snapshot_links_valid=True,
            media_links_valid=False,
            quota_links_valid=True,
            audit_links_valid=True,
        )

    report = asyncio.run(
        validate_teaching_restore(
            _manifest(),
            target_database_identity_sha256="d" * 64,
            object_prefix="",
            object_inventory=_inventory(),
            restore_database=harness.restore_database,
            restore_objects=harness.restore_objects,
            inspect_restored_facts=inspect,
        )
    )

    assert report.ok is False
    assert report.failures == ("media",)


def test_restore_report_rejects_database_object_reference_missing_from_inventory() -> None:
    harness = RestoreHarness(calls=[])

    async def inspect() -> RestoredTeachingFacts:
        return RestoredTeachingFacts(
            platform_schema_revision="platform-revision",
            schema_revisions={"tenant-a": "20260810_0017"},
            classroom_versions_count=2,
            learning_events_count=5,
            source_snapshot_links_valid=True,
            media_links_valid=True,
            quota_links_valid=True,
            audit_links_valid=True,
            database_object_references=(
                DatabaseObjectReference(
                    tenant_id="tenant-a",
                    key="tenants/tenant-a/missing.bin",
                    sha256="b" * 64,
                    size=1,
                    version_id="v1",
                ),
            ),
        )

    report = asyncio.run(
        validate_teaching_restore(
            _manifest(),
            target_database_identity_sha256="d" * 64,
            object_prefix="",
            object_inventory=_inventory(),
            restore_database=harness.restore_database,
            restore_objects=harness.restore_objects,
            inspect_restored_facts=inspect,
        )
    )

    assert report.ok is False
    assert report.failures == ("database_object_references",)


@pytest.mark.parametrize(
    (
        "sha256",
        "size",
        "version_id",
        "content_type",
        "owner_token",
        "source_revision",
    ),
    [
        pytest.param(
            "d" * 64,
            10,
            "version-1",
            "application/json",
            "1" * 32,
            "source-etag-1",
            id="sha256",
        ),
        pytest.param(
            "c" * 64,
            11,
            "version-1",
            "application/json",
            "1" * 32,
            "source-etag-1",
            id="size",
        ),
        pytest.param(
            "c" * 64,
            10,
            "wrong-version",
            "application/json",
            "1" * 32,
            "source-etag-1",
            id="version-id",
        ),
        pytest.param(
            "c" * 64,
            10,
            "version-1",
            "application/pdf",
            "1" * 32,
            "source-etag-1",
            id="content-type",
        ),
        pytest.param(
            "c" * 64,
            10,
            "version-1",
            "application/json",
            "2" * 32,
            "source-etag-1",
            id="owner-token",
        ),
        pytest.param(
            "c" * 64,
            10,
            "version-1",
            "application/json",
            "1" * 32,
            "wrong-revision",
            id="source-revision",
        ),
    ],
)
def test_restore_report_rejects_database_object_receipt_mismatch(
    sha256: str,
    size: int,
    version_id: str,
    content_type: str,
    owner_token: str,
    source_revision: str,
) -> None:
    harness = RestoreHarness(calls=[])
    inventory = (
        RestorableObjectInventoryEntry(
            tenant_id="tenant-a",
            key="tenants/tenant-a/classrooms/a/document.json",
            sha256="c" * 64,
            size=10,
            version_id="version-1",
            payload_file=(
                "objects/"
                + hashlib.sha256(b"tenants/tenant-a/classrooms/a/document.json").hexdigest()
                + ".blob"
            ),
            content_type="application/json",
            owner_token="1" * 32,
            source_revision="source-etag-1",
        ),
    )

    async def inspect() -> RestoredTeachingFacts:
        return RestoredTeachingFacts(
            platform_schema_revision="platform-revision",
            schema_revisions={"tenant-a": "20260810_0017"},
            classroom_versions_count=2,
            learning_events_count=5,
            source_snapshot_links_valid=True,
            media_links_valid=True,
            quota_links_valid=True,
            audit_links_valid=True,
            database_object_references=(
                DatabaseObjectReference(
                    tenant_id="tenant-a",
                    key="tenants/tenant-a/classrooms/a/document.json",
                    sha256=sha256,
                    size=size,
                    version_id=version_id,
                    content_type=content_type,
                    owner_token=owner_token,
                    source_revision=source_revision,
                ),
            ),
        )

    report = asyncio.run(
        validate_teaching_restore(
            _manifest(inventory),
            target_database_identity_sha256="d" * 64,
            object_prefix="",
            object_inventory=inventory,
            restore_database=harness.restore_database,
            restore_objects=harness.restore_objects,
            inspect_restored_facts=inspect,
        )
    )

    assert report.ok is False
    assert report.failures == ("database_object_references",)


def test_restore_rejects_tampered_inventory_before_any_write() -> None:
    harness = RestoreHarness(calls=[])
    tampered = (
        ObjectInventoryEntry(
            tenant_id="tenant-a",
            key="tenants/tenant-a/classrooms/a/document.json",
            sha256="f" * 64,
            size=10,
        ),
    )

    with pytest.raises(ValueError, match="object inventory checksum"):
        asyncio.run(
            validate_teaching_restore(
                _manifest(),
                target_database_identity_sha256="d" * 64,
                object_prefix="",
                object_inventory=tampered,
                restore_database=harness.restore_database,
                restore_objects=harness.restore_objects,
                inspect_restored_facts=harness.inspect,
            )
        )

    assert harness.calls == []


def test_restore_rejects_inventory_tenant_missing_from_manifest_before_any_write() -> None:
    harness = RestoreHarness(calls=[])
    foreign_inventory = (
        ObjectInventoryEntry(
            tenant_id="tenant-b",
            key="tenants/tenant-b/classrooms/b/document.json",
            sha256="d" * 64,
            size=4,
        ),
    )

    with pytest.raises(ValueError, match="tenant inventory"):
        asyncio.run(
            validate_teaching_restore(
                _manifest(foreign_inventory),
                target_database_identity_sha256="d" * 64,
                object_prefix="",
                object_inventory=foreign_inventory,
                restore_database=harness.restore_database,
                restore_objects=harness.restore_objects,
                inspect_restored_facts=harness.inspect,
            )
        )

    assert harness.calls == []


def test_restore_target_loader_reuses_backup_config_and_secret_contract(tmp_path) -> None:
    config = tmp_path / "platform.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "database_host": "restore-db.internal",
                "database_port": 5432,
                "database_name": "restore_validation",
                "database_user": "yfeistai_app",
                "object_store_mode": "s3",
                "object_store_endpoint": "https://restore-minio.internal",
                "object_store_namespace_id": "restore-objects-primary",
                "object_store_bucket": "restore-classrooms",
                "object_store_region": "us-east-1",
            }
        ),
        encoding="utf-8",
    )
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "platform_database_app_password").write_text(
        "APP_DATABASE_SECRET", encoding="utf-8"
    )
    (secret_dir / "platform_database_migration_password").write_text(
        "MIGRATION_DATABASE_SECRET", encoding="utf-8"
    )
    (secret_dir / "minio_bootstrap_access_key").write_text("MINIO_ACCESS", encoding="utf-8")
    (secret_dir / "minio_bootstrap_secret_key").write_text("MINIO_SECRET", encoding="utf-8")

    target = _RESTORE.load_restore_target(config, secret_dir)

    from sqlalchemy.engine import make_url

    database_url = make_url(target.database_url)
    app_database_url = make_url(target.app_database_url)
    assert target.database_host == "restore-db.internal"
    assert target.database_name == "restore_validation"
    assert target.database_user == "yfeistai_migrator"
    assert target.database_password == "MIGRATION_DATABASE_SECRET"
    assert database_url.username == "yfeistai_migrator"
    assert database_url.password == "MIGRATION_DATABASE_SECRET"
    assert app_database_url.username == "yfeistai_app"
    assert app_database_url.password == "APP_DATABASE_SECRET"
    assert target.object_endpoint == "https://restore-minio.internal"
    assert target.object_namespace_id == "restore-objects-primary"
    assert target.object_access_key == "MINIO_ACCESS"
    assert target.object_secret_key == "MINIO_SECRET"
    assert "APP_DATABASE_SECRET" not in repr(target)
    assert "MIGRATION_DATABASE_SECRET" not in repr(target)
    assert "MINIO_SECRET" not in repr(target)


def test_default_database_probe_counts_namespaced_and_database_level_user_objects() -> None:
    statements: list[str] = []
    values = iter(
        (
            SimpleNamespace(
                system_identifier="7449553557289146937",
                database_oid="42",
                database_name="restore_target",
            ),
            3,
        )
    )

    class Result:
        def __init__(self, value) -> None:
            self.value = value

        def scalar_one(self):
            return self.value

        def one(self):
            return self.value

    class Connection:
        async def execute(self, statement):
            statements.append(str(statement))
            return Result(next(values))

    class ConnectionContext:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args) -> None:
            return None

    engine = SimpleNamespace(
        url=SimpleNamespace(host="restore-db", port=5432, database="restore_target"),
        connect=lambda: ConnectionContext(),
    )

    state = asyncio.run(_RESTORE._default_database_probe(engine))

    assert state.user_object_count == 3
    object_sql = statements[1]
    for catalog in (
        "pg_namespace",
        "pg_class",
        "pg_proc",
        "pg_type",
        "pg_operator",
        "pg_collation",
        "pg_conversion",
        "pg_ts_config",
        "pg_ts_dict",
        "pg_ts_parser",
        "pg_ts_template",
        "pg_opclass",
        "pg_opfamily",
        "pg_statistic_ext",
        "pg_event_trigger",
        "pg_foreign_data_wrapper",
        "pg_foreign_server",
        "pg_user_mapping",
        "pg_publication",
        "pg_subscription",
        "pg_extension",
        "pg_cast",
        "pg_transform",
        "pg_language",
        "pg_largeobject_metadata",
        "pg_default_acl",
        "pg_depend",
    ):
        assert catalog in object_sql
    assert "deptype = 'e'" in object_sql
    assert "types.typtype IN ('b', 'c', 'd', 'e', 'r', 'm')" in object_sql


def test_restore_rebinds_database_object_receipts_to_target_versions() -> None:
    statements: list[tuple[str, object]] = []

    class Result:
        def __init__(self, *, rows=None) -> None:
            self.rows = rows

        def all(self):
            return self.rows

    results = iter((Result(rows=[("tenant-a", "tenant_a")]), Result(), Result()))

    class Connection:
        async def execute(self, statement, parameters=None):
            statements.append((str(statement), parameters))
            return next(results)

    class Context:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    receipt = _RESTORE.RestoredObjectReceipt(
        tenant_id="tenant-a",
        key="tenants/tenant-a/classrooms/a/document.json",
        sha256="c" * 64,
        size=10,
        content_type="application/json",
        owner_token="1" * 32,
        revision="target-etag-1",
        version_id="target-version-1",
    )

    asyncio.run(
        _RESTORE._default_rebind_database_object_receipts(
            SimpleNamespace(begin=lambda: Context()),
            (receipt,),
        )
    )

    sql = "\n".join(statement for statement, _parameters in statements)
    assert "tenant_schema_states.status = 'active'" in sql
    assert 'UPDATE "tenant_a".source_uploads' in sql
    assert "object_revision = :revision" in sql
    assert "object_version_id = :version_id" in sql
    assert 'UPDATE "tenant_a".classroom_draft_media' in sql
    assert statements[1][1] == {
        "key": receipt.key,
        "owner_token": receipt.owner_token,
        "revision": receipt.revision,
        "sha256": receipt.sha256,
        "version_id": receipt.version_id,
    }


def test_restore_replays_app_role_grants_for_platform_and_active_tenants() -> None:
    statements: list[str] = []

    class Result:
        def all(self):
            return [("tenant_a",)]

    class Connection:
        async def execute(self, statement, _parameters=None):
            statements.append(str(statement))
            return Result()

    class Context:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    engine = SimpleNamespace(connect=lambda: Context(), begin=lambda: Context())
    asyncio.run(_RESTORE._default_grant_app_access(engine))

    sql = "\n".join(statements)
    for schema in ('"platform"', '"tenant_a"'):
        assert f"GRANT USAGE ON SCHEMA {schema} TO yfeistai_app" in sql
        assert f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema}" in sql
        assert f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema}" in sql
        assert ("ALTER DEFAULT PRIVILEGES FOR ROLE yfeistai_migrator IN SCHEMA " + schema) in sql
    assert (
        "REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE "
        "platform.generation_route_attempts FROM yfeistai_app"
    ) in sql
    assert (
        "ALTER FUNCTION platform.record_generation_route_attempt("
        "text, text, text, integer, text, text, text, text, text, text, text, text, "
        "text, text, text) "
        "OWNER TO yfeistai_migrator"
    ) in sql
    assert (
        "ALTER FUNCTION platform.read_generation_route_attempts("
        "text, text, text, text, text, text, text) OWNER TO yfeistai_migrator"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION platform.record_generation_route_attempt("
        "text, text, text, integer, text, text, text, text, text, text, text, text, "
        "text, text, text) "
        "TO yfeistai_app"
    ) in sql
    assert (
        "REVOKE ALL ON FUNCTION platform.record_generation_route_attempt("
        "text, text, text, integer, text, text, text, text, text, text, text, text, "
        "text, text, text) "
        "FROM PUBLIC"
    ) in sql
    assert (
        "record_generation_route_attempt(text, text, text, integer, text, text, text, "
        "text, text, text, text, text)"
    ) not in sql
    assert (
        "GRANT EXECUTE ON FUNCTION platform.read_generation_route_attempts("
        "text, text, text, text, text, text, text) TO yfeistai_app"
    ) in sql
    assert (
        "REVOKE ALL ON FUNCTION platform.read_generation_route_attempts("
        "text, text, text, text, text, text, text) FROM PUBLIC"
    ) in sql
    assert (
        sql.index("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA")
        < sql.index("ALTER FUNCTION platform.record_generation_route_attempt")
        < sql.index("ALTER FUNCTION platform.read_generation_route_attempts")
        < sql.index("REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE")
        < sql.index("REVOKE ALL ON FUNCTION platform.record_generation_route_attempt")
        < sql.index("GRANT EXECUTE ON FUNCTION platform.record_generation_route_attempt")
    )


def test_restore_app_role_probe_checks_privileges_and_reads_each_schema() -> None:
    statements: list[str] = []

    class Result:
        def __init__(self, *, scalar=None, rows=None) -> None:
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def all(self):
            return self.rows

    results = iter(
        (
            Result(scalar=True),
            Result(rows=[("tenant_a",)]),
            Result(scalar=True),
            Result(scalar=True),
            Result(scalar=1),
            Result(scalar=True),
            Result(scalar=1),
        )
    )

    class Connection:
        async def execute(self, statement, _parameters=None):
            statements.append(str(statement))
            return next(results)

    class Context:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    valid = asyncio.run(
        _RESTORE._default_app_access_probe(SimpleNamespace(connect=lambda: Context()))
    )

    assert valid is True
    sql = "\n".join(statements)
    assert "current_user = 'yfeistai_app'" in sql
    assert "has_schema_privilege" in sql
    assert "platform.generation_route_attempts" in sql
    assert "has_function_privilege" in sql
    assert "record_generation_route_attempt" in sql
    assert (
        "record_generation_route_attempt(text, text, text, integer, text, text, text, "
        "text, text, text, text, text, text, text, text)"
    ) in sql
    assert (
        "record_generation_route_attempt(text, text, text, integer, text, text, text, "
        "text, text, text, text, text)"
    ) not in sql
    assert "read_generation_route_attempts" in sql
    assert "NOT has_table_privilege" in sql
    assert sql.count("has_sequence_privilege") == 4
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "EXECUTE"):
        assert f"'{privilege}'" in sql
    for privilege in ("USAGE", "SELECT"):
        assert f"'{privilege}'" in sql
    assert "'SELECT,INSERT,UPDATE,DELETE'" not in sql
    assert "'USAGE,SELECT'" not in sql
    assert 'FROM "platform".tenant_schema_states' in sql
    assert 'FROM "tenant_a".alembic_version' in sql


def test_restore_app_role_probe_rejects_incomplete_privilege_matrix() -> None:
    statements: list[str] = []

    class Result:
        def __init__(self, *, scalar=None, rows=None) -> None:
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def all(self):
            return self.rows

    results = iter(
        (
            Result(scalar=True),
            Result(rows=[]),
            Result(scalar=False),
        )
    )

    class Connection:
        async def execute(self, statement, _parameters=None):
            statements.append(str(statement))
            return next(results)

    class Context:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    valid = asyncio.run(
        _RESTORE._default_app_access_probe(SimpleNamespace(connect=lambda: Context()))
    )

    assert valid is False
    assert len(statements) == 3


def test_default_facts_inspector_checks_revisions_and_distinct_relationships() -> None:
    statements: list[tuple[str, object]] = []

    def fk_row(
        name: str,
        source_table: str,
        source_columns: tuple[str, ...],
        target_table: str,
        target_columns: tuple[str, ...],
        *,
        delete_action: str = "r",
    ) -> tuple[object, ...]:
        return (
            name,
            source_table,
            list(source_columns),
            target_table,
            list(target_columns),
            True,
            delete_action,
            "a",
            "s",
            False,
            False,
        )

    class Result:
        def __init__(self, *, scalar=None, rows=None, one=None) -> None:
            self.scalar = scalar
            self.rows = rows
            self.row = one

        def scalar_one(self):
            return self.scalar

        def all(self):
            return self.rows

        def one(self):
            return self.row

    results = iter(
        (
            Result(scalar="platform-revision"),
            Result(
                rows=[
                    ("tenant-a", "tenant_a", "tenant-revision", "active"),
                    ("tenant-pending", "tenant_pending", None, "pending"),
                ]
            ),
            Result(scalar="tenant-revision"),
            Result(scalar=2),
            Result(scalar=5),
            Result(
                rows=[
                    (
                        "tenants/tenant-a/source.bin",
                        "b" * 64,
                        17,
                        "source-version-1",
                        None,
                        "1" * 32,
                        "source-etag-1",
                    )
                ]
            ),
            Result(
                rows=[
                    fk_row(
                        "fk_source_snapshots_upload_tenant",
                        "source_snapshots",
                        ("source_upload_id", "tenant_id"),
                        "source_uploads",
                        ("id", "tenant_id"),
                    ),
                    fk_row(
                        "fk_tenant_source_bindings_snapshot_tenant",
                        "tenant_source_bindings",
                        ("source_snapshot_id", "tenant_id"),
                        "source_snapshots",
                        ("id", "tenant_id"),
                    ),
                    fk_row(
                        "fk_teaching_briefs_snapshot_tenant",
                        "teaching_briefs",
                        ("source_snapshot_id", "tenant_id"),
                        "source_snapshots",
                        ("id", "tenant_id"),
                    ),
                ]
            ),
            Result(
                rows=[
                    fk_row(
                        "fk_classroom_assets_current_version_classroom_tenant",
                        "classroom_assets",
                        ("current_published_version_id", "id", "tenant_id"),
                        "classroom_versions",
                        ("id", "classroom_id", "tenant_id"),
                    ),
                    fk_row(
                        "fk_classroom_versions_asset_tenant_classroom_assets",
                        "classroom_versions",
                        ("classroom_id", "tenant_id"),
                        "classroom_assets",
                        ("id", "tenant_id"),
                    ),
                    fk_row(
                        "fk_classroom_versions_job_tenant_generation_jobs",
                        "classroom_versions",
                        ("generation_job_id", "tenant_id"),
                        "generation_jobs",
                        ("id", "tenant_id"),
                    ),
                    fk_row(
                        "fk_classroom_versions_source_classroom_tenant",
                        "classroom_versions",
                        ("source_version_id", "classroom_id", "tenant_id"),
                        "classroom_versions",
                        ("id", "classroom_id", "tenant_id"),
                    ),
                ]
            ),
            Result(
                rows=[
                    fk_row(
                        "fk_classroom_draft_media_asset_tenant_classroom_assets",
                        "classroom_draft_media",
                        ("classroom_id", "tenant_id"),
                        "wrong_target_table",
                        ("id", "tenant_id"),
                        delete_action="c",
                    )
                ]
            ),
            Result(
                rows=[
                    fk_row(
                        "fk_classroom_exports_asset_tenant_classroom_assets",
                        "classroom_exports",
                        ("classroom_id", "tenant_id"),
                        "classroom_assets",
                        ("id", "tenant_id"),
                    ),
                    fk_row(
                        "fk_classroom_exports_version_classroom_tenant",
                        "classroom_exports",
                        ("classroom_version_id", "classroom_id", "tenant_id"),
                        "classroom_versions",
                        ("id", "classroom_id", "tenant_id"),
                    ),
                    fk_row(
                        "fk_classroom_exports_draft_classroom_tenant",
                        "classroom_exports",
                        ("classroom_draft_id", "classroom_id", "tenant_id"),
                        "classroom_drafts",
                        ("id", "classroom_id", "tenant_id"),
                    ),
                    fk_row(
                        "fk_classroom_exports_job_tenant_generation_jobs",
                        "classroom_exports",
                        ("generation_job_id", "tenant_id"),
                        "generation_jobs",
                        ("id", "tenant_id"),
                    ),
                ]
            ),
            Result(
                rows=[
                    fk_row(
                        "fk_quota_ledger_job_tenant_generation_jobs",
                        "quota_ledger",
                        ("job_id", "tenant_id"),
                        "generation_jobs",
                        ("id", "tenant_id"),
                        delete_action="c",
                    )
                ]
            ),
            Result(
                rows=[
                    fk_row(
                        "fk_audit_log_tenant_id_tenants",
                        "audit_log",
                        ("tenant_id",),
                        "tenants",
                        ("id",),
                        delete_action="n",
                    )
                ]
            ),
            Result(scalar=True),
        )
    )

    class Connection:
        async def execute(self, statement, parameters=None):
            statements.append((str(statement), parameters))
            return next(results)

    class ConnectionContext:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args) -> None:
            return None

    engine = SimpleNamespace(connect=lambda: ConnectionContext())

    facts = asyncio.run(_RESTORE._default_facts_inspector(engine))

    assert facts.platform_schema_revision == "platform-revision"
    assert facts.schema_revisions == {"tenant-a": "tenant-revision"}
    assert facts.referenced_object_keys == ("tenants/tenant-a/source.bin",)
    assert tuple(
        (
            reference.key,
            reference.sha256,
            reference.size,
            reference.version_id,
            reference.content_type,
            reference.owner_token,
            reference.source_revision,
        )
        for reference in facts.database_object_references
    ) == (
        (
            "tenants/tenant-a/source.bin",
            "b" * 64,
            17,
            "source-version-1",
            None,
            "1" * 32,
            "source-etag-1",
        ),
    )
    assert facts.source_snapshot_links_valid is True
    assert facts.media_links_valid is False
    assert facts.quota_links_valid is True
    assert facts.audit_links_valid is True
    sql = "\n".join(f"{statement}\n{parameters!r}" for statement, parameters in statements)
    for relation in (
        "platform.alembic_version",
        "alembic_version",
        "source_snapshots",
        "classroom_versions",
        "classroom_draft_media",
        "classroom_exports",
        "quota_ledger",
        "platform.audit_log",
    ):
        assert relation in sql
    for catalog_binding in (
        "constraints.contype = 'f'",
        "constraints.conrelid",
        "constraints.confrelid",
        "constraints.confdeltype",
        "constraints.confupdtype",
        "constraints.confmatchtype",
        "constraints.condeferrable",
        "constraints.condeferred",
        "unnest(constraints.conkey)",
        "unnest(constraints.confkey)",
    ):
        assert catalog_binding in sql
    assert "audit.resource_id IS NULL" not in sql
    assert "audit.resource_id IS NOT NULL AND btrim(audit.resource_id) = ''" in sql


def test_restore_operator_uses_safe_database_and_object_restore_boundaries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LD_LIBRARY_PATH", "/untrusted/restore-library")
    backup_dir, inventory, payload = _restorable_backup(tmp_path)

    calls: list[tuple[str, object]] = []
    actual_reverify = _RESTORE._reverify_verified_backup

    def record_reverify(backup):
        calls.append(("reverify", backup.archive_fingerprint_sha256))
        return actual_reverify(backup)

    monkeypatch.setattr(_RESTORE, "_reverify_verified_backup", record_reverify)

    target_config = tmp_path / "restore-target.json"
    target_secret_dir = tmp_path / "restore-secrets"
    report_path = tmp_path / "restore-report.json"
    engine = _RuntimeEngine()
    client = _RuntimeObjectClient(calls)
    runtime = _successful_operator_runtime(calls, engine, client)

    report = asyncio.run(
        _RESTORE.run_restore_operator(
            backup_dir=backup_dir,
            target_secret_dir=target_secret_dir,
            report_path=report_path,
            pg_restore=Path("pg_restore-safe"),
            runtime=runtime,
            **_measured_restore_arguments(
                tmp_path,
                run_id="run-20260821",
                target_config=target_config,
            ),
        )
    )

    assert report.ok is True
    pg_argv, pg_environment = next(value for name, value in calls if name == "pg_restore")
    for required in (
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
    ):
        assert required in pg_argv
    assert "restore-password" not in " ".join(pg_argv)
    assert pg_environment["PGPASSWORD"] == "restore-password"
    assert "LD_LIBRARY_PATH" not in pg_environment
    assert "--clean" not in pg_argv
    event_names = [name for name, _value in calls]
    reverify_indices = [index for index, name in enumerate(event_names) if name == "reverify"]
    restored_key = inventory[0].key
    restored_put_index = next(
        index
        for index, (name, value) in enumerate(calls)
        if name == "put" and value["Key"] == restored_key
    )
    assert len(reverify_indices) == 2
    assert reverify_indices[0] < event_names.index("pg_restore")
    assert event_names.index("pg_restore") < reverify_indices[1]
    assert reverify_indices[1] < restored_put_index
    assert restored_put_index < event_names.index("rebind")
    assert event_names.index("rebind") < event_names.index("grant_app")
    assert event_names.index("grant_app") < event_names.index("probe_app")
    assert event_names.index("probe_app") < event_names.index("inspect")
    assert client.objects[restored_key] == payload
    assert (
        "put",
        {
            "Bucket": "restore-bucket",
            "Key": restored_key,
            "ContentLength": len(payload),
            "ContentType": "application/json",
            "Metadata": {
                "owner": "1" * 32,
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            "IfNoneMatch": "*",
        },
    ) in calls
    report_bytes = report_path.read_bytes()
    report_payload = json.loads(report_bytes)
    assert report_bytes == (
        json.dumps(
            report_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    assert report_payload["crossSystemAtomic"] is False
    assert report_payload["objects"] == {
        "createOnly": True,
        "isolation": "empty_target_bucket",
        "readbackVerified": True,
        "restoredCount": 1,
        "targetBucket": "restore-bucket",
    }
    assert "app_role_access" in report_payload["validated"]
    assert secret_file_is_restricted(report_path)
    assert engine.disposed is True
    assert client.closed is True
    app_engine = next(value[1] for name, value in calls if name == "app_engine")
    assert app_engine.disposed is True


def test_restore_operator_does_not_publish_when_app_role_probe_fails(tmp_path: Path) -> None:
    backup_dir, _inventory_entries, _payload = _restorable_backup(tmp_path)
    calls: list[tuple[str, object]] = []
    engine = _RuntimeEngine()
    client = _RuntimeObjectClient(calls)
    runtime = _successful_operator_runtime(calls, engine, client)

    async def deny_app_access(_engine) -> bool:
        calls.append(("probe_app_denied", None))
        return False

    report_path = tmp_path / "restore-report.json"
    with pytest.raises(RuntimeError, match="app role access"):
        asyncio.run(
            _RESTORE.run_restore_operator(
                backup_dir=backup_dir,
                target_secret_dir=tmp_path / "secrets",
                report_path=report_path,
                runtime=replace(runtime, app_access_probe=deny_app_access),
                **_measured_restore_arguments(tmp_path, run_id="app-role-denied"),
            )
        )

    assert report_path.exists() is False
    assert engine.disposed is True
    assert client.closed is True
    app_engine = next(value[1] for name, value in calls if name == "app_engine")
    assert app_engine.disposed is True


def test_restore_operator_rejects_null_target_version_before_database_rebind(
    tmp_path: Path,
) -> None:
    backup_dir, _inventory_entries, _payload = _restorable_backup(tmp_path)
    calls: list[tuple[str, object]] = []
    engine = _RuntimeEngine()
    client = _RuntimeObjectClient(calls, put_version_id="null")
    report_path = tmp_path / "restore-report.json"

    with pytest.raises(RuntimeError, match="create-only write returned no receipt"):
        asyncio.run(
            _RESTORE.run_restore_operator(
                backup_dir=backup_dir,
                target_secret_dir=tmp_path / "secrets",
                report_path=report_path,
                runtime=_successful_operator_runtime(calls, engine, client),
                **_measured_restore_arguments(tmp_path, run_id="null-target-version"),
            )
        )

    event_names = [name for name, _value in calls]
    assert "put" in event_names
    for forbidden in ("head", "get", "rebind", "grant_app", "probe_app"):
        assert forbidden not in event_names
    assert report_path.exists() is False
    assert engine.disposed is True
    assert client.closed is True


@pytest.mark.parametrize("failing_resource", ("app", "object", "database"))
def test_restore_operator_does_not_publish_when_resource_cleanup_fails(
    tmp_path: Path,
    failing_resource: str,
) -> None:
    backup_dir, _inventory_entries, _payload = _restorable_backup(tmp_path)
    calls: list[tuple[str, object]] = []

    class Engine(_RuntimeEngine):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

        async def dispose(self) -> None:
            self.disposed = True
            calls.append((f"dispose_{self.name}", None))
            if failing_resource == self.name:
                raise RuntimeError(f"{self.name.upper()}_CLEANUP_SECRET")

    class Client(_RuntimeObjectClient):
        def close(self) -> None:
            self.closed = True
            calls.append(("close_object", None))
            if failing_resource == "object":
                raise RuntimeError("OBJECT_CLEANUP_SECRET")

    database_engine = Engine("database")
    app_engine = Engine("app")
    client = Client(calls)
    runtime = _successful_operator_runtime(calls, database_engine, client)

    def create_app_engine(database_url: str):
        calls.append(("app_engine", (database_url, app_engine)))
        return app_engine

    report_path = tmp_path / "restore-report.json"
    with pytest.raises(RuntimeError, match="restore resource cleanup failed") as exc_info:
        asyncio.run(
            _RESTORE.run_restore_operator(
                backup_dir=backup_dir,
                target_secret_dir=tmp_path / "secrets",
                report_path=report_path,
                runtime=replace(runtime, app_engine_factory=create_app_engine),
                **_measured_restore_arguments(
                    tmp_path,
                    run_id=f"cleanup-{failing_resource}",
                ),
            )
        )

    assert report_path.exists() is False
    assert app_engine.disposed is True
    assert client.closed is True
    assert database_engine.disposed is True
    assert "CLEANUP_SECRET" not in str(exc_info.value)
    event_names = [name for name, _value in calls]
    assert event_names.index("dispose_app") < event_names.index("close_object")
    assert event_names.index("close_object") < event_names.index("dispose_database")


def test_restore_operator_preserves_primary_failure_when_cleanup_also_fails(
    tmp_path: Path,
) -> None:
    backup_dir, _inventory_entries, _payload = _restorable_backup(tmp_path)
    calls: list[tuple[str, object]] = []

    class FailingEngine(_RuntimeEngine):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

        async def dispose(self) -> None:
            self.disposed = True
            calls.append((f"dispose_{self.name}", None))
            raise RuntimeError(f"{self.name.upper()}_CLEANUP_SECRET")

    class FailingClient(_RuntimeObjectClient):
        def close(self) -> None:
            self.closed = True
            calls.append(("close_object", None))
            raise RuntimeError("OBJECT_CLEANUP_SECRET")

    database_engine = FailingEngine("database")
    app_engine = FailingEngine("app")
    client = FailingClient(calls)
    runtime = _successful_operator_runtime(calls, database_engine, client)

    async def fail_facts(_engine):
        raise RuntimeError("FACTS_SECRET")

    report_path = tmp_path / "restore-report.json"
    with pytest.raises(RuntimeError, match="restored database validation failed") as exc_info:
        asyncio.run(
            _RESTORE.run_restore_operator(
                backup_dir=backup_dir,
                target_secret_dir=tmp_path / "secrets",
                report_path=report_path,
                runtime=replace(
                    runtime,
                    facts_inspector=fail_facts,
                    app_engine_factory=lambda _database_url: app_engine,
                ),
                **_measured_restore_arguments(tmp_path, run_id="primary-plus-cleanup"),
            )
        )

    assert report_path.exists() is False
    assert app_engine.disposed is True
    assert client.closed is True
    assert database_engine.disposed is True
    rendered = str(exc_info.value) + "\n" + "\n".join(getattr(exc_info.value, "__notes__", ()))
    for secret in (
        "FACTS_SECRET",
        "APP_CLEANUP_SECRET",
        "OBJECT_CLEANUP_SECRET",
        "DATABASE_CLEANUP_SECRET",
    ):
        assert secret not in rendered
    assert "app role database, object client, target database" in rendered


def test_restore_operator_rejects_concurrent_object_injection_before_report(
    tmp_path: Path,
) -> None:
    backup_dir, _inventory_entries, _payload = _restorable_backup(tmp_path)
    calls: list[tuple[str, object]] = []
    engine = _RuntimeEngine()
    prefix = ""
    client = _RuntimeObjectClient(
        calls,
        concurrent_key=prefix + "concurrent-extra-object",
    )
    report_path = tmp_path / "restore-report.json"

    with pytest.raises(RuntimeError, match="restored object bucket verification failed"):
        asyncio.run(
            _RESTORE.run_restore_operator(
                backup_dir=backup_dir,
                target_secret_dir=tmp_path / "secrets",
                report_path=report_path,
                runtime=_successful_operator_runtime(calls, engine, client),
                **_measured_restore_arguments(tmp_path, run_id="concurrent-run"),
            )
        )

    assert report_path.exists() is False
    assert engine.disposed is True
    assert client.closed is True


def test_restore_report_publish_failure_leaves_no_formal_or_staging_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_path = tmp_path / "restore-report.json"
    report = _RESTORE.RestoreValidationReport(
        ok=True,
        target_database_identity_sha256="d" * 64,
        object_prefix="",
        validated=("schema_revisions",),
        failures=(),
    )

    def fail_publish(_source, _target) -> None:
        raise OSError("REPORT_PUBLISH_SECRET")

    monkeypatch.setattr(_RESTORE.os, "link", fail_publish)
    with pytest.raises(RuntimeError, match="could not be published") as exc_info:
        _RESTORE._write_restore_report(
            report_path,
            report,
            run_id="report-run",
            restored_object_count=1,
            archive_fingerprint_sha256="a" * 64,
            manifest_sha256="b" * 64,
            target_object_bucket="restore-bucket",
        )

    assert "REPORT_PUBLISH_SECRET" not in str(exc_info.value)
    assert report_path.exists() is False
    assert tuple(tmp_path.iterdir()) == ()


def test_restore_report_binds_the_verified_source_archive() -> None:
    payload = _RESTORE.restore_report_payload(
        _RESTORE.RestoreValidationReport(
            ok=True,
            target_database_identity_sha256="d" * 64,
            object_prefix="",
            validated=("schema_revisions",),
            failures=(),
        ),
        run_id="report-run",
        restored_object_count=1,
        archive_fingerprint_sha256="a" * 64,
        manifest_sha256="b" * 64,
        target_object_bucket="restore-bucket",
    )

    assert payload["sourceArchive"] == {
        "archiveFingerprintSha256": "a" * 64,
        "manifestSha256": "b" * 64,
    }
    assert payload["objects"]["targetBucket"] == "restore-bucket"


def test_restore_report_fsyncs_parent_after_atomic_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_path = tmp_path / "restore-report.json"
    events: list[tuple[str, Path]] = []
    actual_link = _RESTORE.os.link

    def record_link(source: Path, target: Path) -> None:
        actual_link(source, target)
        events.append(("link", Path(target)))

    def record_directory_fsync(parent: Path) -> None:
        events.append(("fsync", Path(parent)))

    monkeypatch.setattr(_RESTORE.os, "link", record_link)
    monkeypatch.setattr(_RESTORE, "_fsync_directory", record_directory_fsync)
    _RESTORE._write_restore_report(
        report_path,
        _RESTORE.RestoreValidationReport(
            ok=True,
            target_database_identity_sha256="d" * 64,
            object_prefix="",
            validated=("schema_revisions",),
            failures=(),
        ),
        run_id="report-run",
        restored_object_count=1,
        archive_fingerprint_sha256="a" * 64,
        manifest_sha256="b" * 64,
        target_object_bucket="restore-bucket",
    )

    assert events == [("link", report_path), ("fsync", tmp_path)]


def test_restore_report_does_not_fail_after_link_commit_when_parent_fsync_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_path = tmp_path / "restore-report.json"

    def fail_directory_fsync(_parent: Path) -> None:
        raise OSError("REPORT_FSYNC_SECRET")

    monkeypatch.setattr(_RESTORE, "_fsync_directory", fail_directory_fsync)
    _RESTORE._write_restore_report(
        report_path,
        _RESTORE.RestoreValidationReport(
            ok=True,
            target_database_identity_sha256="d" * 64,
            object_prefix="",
            validated=("schema_revisions",),
            failures=(),
        ),
        run_id="report-run",
        restored_object_count=1,
        archive_fingerprint_sha256="a" * 64,
        manifest_sha256="b" * 64,
        target_object_bucket="restore-bucket",
    )

    assert report_path.is_file()


def test_restore_report_does_not_fail_after_link_commit_when_staging_cleanup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_path = tmp_path / "restore-report.json"
    actual_unlink = Path.unlink

    def fail_staging_unlink(path: Path, *args, **kwargs) -> None:
        if path.name.startswith(f".{report_path.name}."):
            raise OSError("REPORT_CLEANUP_SECRET")
        actual_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_staging_unlink)
    _RESTORE._write_restore_report(
        report_path,
        _RESTORE.RestoreValidationReport(
            ok=True,
            target_database_identity_sha256="d" * 64,
            object_prefix="",
            validated=("schema_revisions",),
            failures=(),
        ),
        run_id="report-run",
        restored_object_count=1,
        archive_fingerprint_sha256="a" * 64,
        manifest_sha256="b" * 64,
        target_object_bucket="restore-bucket",
    )

    assert report_path.is_file()
    assert any(path.name.startswith(f".{report_path.name}.") for path in tmp_path.iterdir())


@pytest.mark.parametrize(
    ("object_count", "bucket_count", "versioning_status", "message"),
    [
        (1, 0, "Enabled", "restore target database pre-observation is invalid"),
        (0, 1, "Enabled", "restore target object pre-observation is invalid"),
        (0, 0, "Suspended", "restore target object pre-observation is invalid"),
    ],
    ids=("nonempty-database", "nonempty-object-bucket", "versioning-suspended"),
)
def test_restore_operator_finishes_all_target_preflight_before_writing(
    tmp_path: Path,
    object_count: int,
    bucket_count: int,
    versioning_status: str,
    message: str,
) -> None:
    backup_dir, _inventory_entries, _payload = _restorable_backup(tmp_path)
    writes: list[str] = []

    class Engine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    class Client:
        closed = False

        def get_bucket_versioning(self, **_arguments):
            return {"Status": versioning_status}

        def list_object_versions(self, **_arguments):
            return {
                "IsTruncated": False,
                "Versions": ([{"Key": "occupied", "VersionId": "v1"}] if bucket_count else []),
                "DeleteMarkers": [],
            }

        def close(self) -> None:
            self.closed = True

    engine = Engine()
    clients: list[Client] = []

    def create_client(_target):
        client = Client()
        clients.append(client)
        return client

    async def probe(_engine):
        target = _restore_target()
        return _RESTORE.TargetDatabaseState(
            identity_sha256=_TARGET_DATABASE_IDENTITY_SHA256,
            user_object_count=object_count,
            current_role=target.database_user,
            database_owner=target.database_user,
        )

    async def probe_objects(_client, _target):
        return _RESTORE.TargetObjectState(
            identity_sha256=_TARGET_OBJECT_IDENTITY_SHA256,
            versioning_enabled=versioning_status == "Enabled",
            object_count=bucket_count,
            version_count=bucket_count,
            delete_marker_count=0,
            owner_id_sha256=_TARGET_OBJECT_OWNER_ID_SHA256,
        )

    class Exclusion:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def process(_argv, _environment):
        writes.append("database")
        return 0

    runtime = _RESTORE.RestoreOperatorRuntime(
        target_loader=lambda _config, _secrets: _restore_target(),
        engine_factory=lambda _url: engine,
        object_client_factory=create_client,
        database_probe=probe,
        facts_inspector=lambda _engine: None,
        process_runner=process,
        receipt_rebinder=lambda _engine, _receipts: None,
        app_access_granter=lambda _engine: None,
        app_engine_factory=lambda _url: None,
        app_access_probe=lambda _engine: None,
        target_exclusion=lambda _engine, _identity: Exclusion(),
        target_exclusion_mode="test-exclusive-target",
        object_state_probe=probe_objects,
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            _RESTORE.run_restore_operator(
                backup_dir=backup_dir,
                target_secret_dir=tmp_path / "secrets",
                report_path=tmp_path / "report.json",
                runtime=runtime,
                **_measured_restore_arguments(tmp_path, run_id="preflight-run"),
            )
        )

    assert writes == []
    assert engine.disposed is True
    assert all(client.closed for client in clients)


def test_restore_operator_rejects_source_object_store_as_target_before_mutation(
    tmp_path: Path,
) -> None:
    target = _restore_target()
    owner_id_sha256 = hashlib.sha256(b"restore-owner-01").hexdigest()
    source_object_store_identity_sha256 = _BACKUP.physical_object_store_identity_sha256(
        target.object_endpoint,
        target.object_region,
        target.object_bucket,
        owner_id_sha256,
    )
    backup_dir, _inventory_entries, _payload = _restorable_backup(
        tmp_path,
        source_object_store_identity_sha256=source_object_store_identity_sha256,
    )
    database_identity_sha256 = "d" * 64
    candidate_sha256 = "c" * 64
    run_id = "same-object-store"
    environment_id = "restore-environment-01"
    target_config = tmp_path / "target.json"
    target_config.write_bytes(b"{}\n")
    target_config_sha256 = hashlib.sha256(target_config.read_bytes()).hexdigest()
    provisioning_receipt = tmp_path / "target-provisioning-receipt.json"
    provisioning_receipt.write_bytes(
        _BACKUP._canonical_json(
            {
                "schemaVersion": 1,
                "producer": "backup-restore-target-provisioner",
                "candidateSha256": candidate_sha256,
                "releaseRun": {
                    "runId": run_id,
                    "environmentId": environment_id,
                },
                "resources": {
                    "database": {
                        "identitySha256": database_identity_sha256,
                        "ownerRunId": run_id,
                        "disposition": "runner-owned-disposable",
                    },
                    "objectStore": {
                        "identitySha256": source_object_store_identity_sha256,
                        "ownerRunId": run_id,
                        "disposition": "runner-owned-disposable",
                    },
                },
            }
        )
    )
    provisioning_receipt_sha256 = hashlib.sha256(provisioning_receipt.read_bytes()).hexdigest()
    engines: list[_RuntimeEngine] = []
    clients: list[_RuntimeObjectClient] = []
    events: list[str] = []

    def create_engine(_database_url: str) -> _RuntimeEngine:
        engine = _RuntimeEngine()
        engines.append(engine)
        return engine

    def create_object_client(_target: _RESTORE.RestoreTarget) -> _RuntimeObjectClient:
        client = _RuntimeObjectClient([])
        clients.append(client)
        return client

    async def probe_database(_engine: object) -> _RESTORE.TargetDatabaseState:
        events.append("observe-database")
        return _RESTORE.TargetDatabaseState(
            identity_sha256=database_identity_sha256,
            user_object_count=0,
            current_role=target.database_user,
            database_owner=target.database_user,
        )

    async def probe_objects(
        _client: object,
        _target: _RESTORE.RestoreTarget,
    ) -> _RESTORE.TargetObjectState:
        events.append("observe-objects")
        return _RESTORE.TargetObjectState(
            identity_sha256=source_object_store_identity_sha256,
            versioning_enabled=True,
            object_count=0,
            version_count=0,
            delete_marker_count=0,
            owner_id_sha256=owner_id_sha256,
        )

    class Exclusion:
        async def __aenter__(self) -> None:
            events.append("lock-enter")

        async def __aexit__(self, *_args: object) -> None:
            events.append("lock-exit")

    def exclude_target(_engine: object, identity_sha256: str) -> Exclusion:
        assert identity_sha256 == database_identity_sha256
        return Exclusion()

    async def unexpected_mutation(*_args: object, **_kwargs: object) -> None:
        pytest.fail("target mutation must not run")

    runtime = _RESTORE.RestoreOperatorRuntime(
        target_loader=lambda _config, _secrets: target,
        engine_factory=create_engine,
        object_client_factory=create_object_client,
        database_probe=probe_database,
        facts_inspector=unexpected_mutation,
        process_runner=unexpected_mutation,
        receipt_rebinder=unexpected_mutation,
        app_access_granter=unexpected_mutation,
        app_engine_factory=lambda _url: pytest.fail("target mutation must not run"),
        app_access_probe=unexpected_mutation,
        target_exclusion=exclude_target,
        target_exclusion_mode="postgresql-session-advisory-lock",
        object_state_probe=probe_objects,
    )

    with pytest.raises(ValueError, match="object pre-observation"):
        asyncio.run(
            _RESTORE.run_restore_operator(
                backup_dir=backup_dir,
                target_config=target_config,
                target_config_sha256=target_config_sha256,
                provisioning_receipt=provisioning_receipt,
                provisioning_receipt_sha256=provisioning_receipt_sha256,
                target_secret_dir=tmp_path / "secrets",
                run_id=run_id,
                environment_id=environment_id,
                candidate_sha256=candidate_sha256,
                database_ownership="runner-owned-disposable",
                object_namespace_ownership="runner-owned-disposable",
                report_path=tmp_path / "report.json",
                runtime=runtime,
            )
        )

    assert events == [
        "observe-database",
        "lock-enter",
        "observe-database",
        "observe-objects",
        "lock-exit",
    ]
    assert len(engines) == len(clients) == 1
    assert engines[0].disposed is True
    assert clients[0].closed is True
    assert not (tmp_path / "report.json").exists()


def test_restore_prefix_inspection_paginates_and_rejects_delete_markers() -> None:
    calls: list[tuple[str, object]] = []
    client = _RuntimeObjectClient(
        calls,
        version_pages=[
            {
                "IsTruncated": True,
                "Versions": [],
                "DeleteMarkers": [],
                "NextKeyMarker": "next-key",
                "NextVersionIdMarker": "next-version",
            },
            {
                "IsTruncated": False,
                "Versions": [],
                "DeleteMarkers": [{"Key": "deleted", "VersionId": "old-version"}],
            },
        ],
    )

    is_empty = asyncio.run(
        _RESTORE._object_prefix_is_empty(
            client,
            bucket="restore-bucket",
            prefix="restore-validation/versioned-run/",
        )
    )

    assert is_empty is False
    list_calls = [arguments for name, arguments in calls if name == "list"]
    assert list_calls[1]["KeyMarker"] == "next-key"
    assert list_calls[1]["VersionIdMarker"] == "next-version"


def test_restore_operator_verifies_local_payloads_before_loading_target(tmp_path: Path) -> None:
    backup_dir, inventory, _payload = _restorable_backup(tmp_path)
    (backup_dir / inventory[0].payload_file).write_bytes(b"tampered")
    target_loads: list[str] = []
    runtime = _RESTORE.RestoreOperatorRuntime(
        target_loader=lambda _config, _secrets: target_loads.append("target"),
        engine_factory=lambda _url: None,
        object_client_factory=lambda _target: None,
        database_probe=lambda _engine: None,
        facts_inspector=lambda _engine: None,
        process_runner=lambda _argv, _environment: None,
        receipt_rebinder=lambda _engine, _receipts: None,
        app_access_granter=lambda _engine: None,
        app_engine_factory=lambda _url: None,
        app_access_probe=lambda _engine: None,
    )

    with pytest.raises(ValueError, match="object payload checksum"):
        asyncio.run(
            _RESTORE.run_restore_operator(
                backup_dir=backup_dir,
                target_config=tmp_path / "target.json",
                target_secret_dir=tmp_path / "secrets",
                run_id="tampered-backup",
                report_path=tmp_path / "report.json",
                runtime=runtime,
            )
        )

    assert target_loads == []
