from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest

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
BackupManifest = _BACKUP.BackupManifest
DatabaseBackup = _BACKUP.DatabaseBackup
ObjectInventoryEntry = _BACKUP.ObjectInventoryEntry
inventory_sha256 = _BACKUP.inventory_sha256
RestoredTeachingFacts = _RESTORE.RestoredTeachingFacts
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
        schema_version=1,
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
        schema_revisions={"tenant-a": "20260810_0017"},
        classroom_versions_count=2,
        learning_events_count=5,
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
            schema_revisions={"tenant-a": "20260810_0017"},
            classroom_versions_count=2,
            learning_events_count=5,
            source_snapshot_links_valid=True,
            media_links_valid=True,
            quota_links_valid=True,
            audit_links_valid=True,
        )


def test_restore_validation_uses_new_database_and_prefix_without_mutating_source() -> None:
    harness = RestoreHarness(calls=[])
    inventory = _inventory()

    report = asyncio.run(
        validate_teaching_restore(
            _manifest(),
            target_database_identity_sha256="d" * 64,
            object_prefix="restore-validation/run-20260821/",
            object_inventory=inventory,
            restore_database=harness.restore_database,
            restore_objects=harness.restore_objects,
            inspect_restored_facts=harness.inspect,
        )
    )

    assert report.ok is True
    assert report.object_prefix == "restore-validation/run-20260821/"
    assert report.validated == (
        "schema_revisions",
        "classroom_versions",
        "learning_events",
        "source_snapshots",
        "media",
        "quota",
        "audit",
    )
    assert [name for name, _ in harness.calls] == ["database", "objects", "inspect"]


@pytest.mark.parametrize(
    ("target_identity", "prefix", "message"),
    [
        ("a" * 64, "restore-validation/run-a/", "new database"),
        ("d" * 64, "tenants/tenant-a/", "restore-validation"),
        ("d" * 64, "restore-validation/../current/", "restore-validation"),
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
            object_prefix="restore-validation/run-b/",
            object_inventory=_inventory(),
            restore_database=harness.restore_database,
            restore_objects=harness.restore_objects,
            inspect_restored_facts=inspect,
        )
    )

    assert report.ok is False
    assert report.failures == ("media",)


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
                object_prefix="restore-validation/run-c/",
                object_inventory=tampered,
                restore_database=harness.restore_database,
                restore_objects=harness.restore_objects,
                inspect_restored_facts=harness.inspect,
            )
        )

    assert harness.calls == []
