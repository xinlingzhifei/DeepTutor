from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _load_backup_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "backup_teaching.py"
    spec = importlib.util.spec_from_file_location("backup_teaching_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BACKUP = _load_backup_module()
ObjectInventoryEntry = _BACKUP.ObjectInventoryEntry
TeachingBackupFacts = _BACKUP.TeachingBackupFacts
create_teaching_backup = _BACKUP.create_teaching_backup
load_backup_manifest = _BACKUP.load_backup_manifest
load_verified_backup = _BACKUP.load_verified_backup
write_backup_manifest = _BACKUP.write_backup_manifest


def test_backup_manifest_binds_database_and_objects(tmp_path) -> None:
    output = tmp_path / "backup"
    output.mkdir()
    database_dump = output / "database.dump"
    database_dump.write_bytes(b"consistent-postgres-dump")
    inventory = (
        ObjectInventoryEntry(
            tenant_id="tenant-a",
            key="tenants/tenant-a/classrooms/a/document.json",
            sha256=hashlib.sha256(b"document-a").hexdigest(),
            size=len(b"document-a"),
        ),
        ObjectInventoryEntry(
            tenant_id="tenant-b",
            key="tenants/tenant-b/classrooms/b/media/image.png",
            sha256=hashlib.sha256(b"image-b").hexdigest(),
            size=len(b"image-b"),
        ),
    )

    manifest = write_backup_manifest(
        output,
        database_dump=database_dump,
        database_identity_sha256="a" * 64,
        object_inventory=inventory,
        schema_revisions={"tenant-a": "20260810_0017", "tenant-b": "20260810_0017"},
        classroom_versions_count=3,
        learning_events_count=8,
        created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
    )

    assert manifest.database.sha256 == hashlib.sha256(database_dump.read_bytes()).hexdigest()
    assert manifest.database.size == len(database_dump.read_bytes())
    assert manifest.object_inventory_sha256
    assert manifest.classroom_versions_count == 3
    assert manifest.learning_events_count == 8
    loaded = load_backup_manifest(output / "manifest.json")
    assert loaded == manifest
    inventory_bytes = (output / manifest.object_inventory_file).read_bytes()
    assert hashlib.sha256(inventory_bytes).hexdigest() == manifest.object_inventory_sha256
    assert json.loads((output / "manifest.sha256").read_text(encoding="utf-8")) == {
        "manifestSha256": hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    }


def test_backup_manifest_refuses_to_overwrite_existing_backup_files(tmp_path) -> None:
    output = tmp_path / "backup"
    output.mkdir()
    database_dump = output / "database.dump"
    database_dump.write_bytes(b"dump")
    (output / "manifest.json").write_text("operator-owned", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_backup_manifest(
            output,
            database_dump=database_dump,
            database_identity_sha256="b" * 64,
            object_inventory=(),
            schema_revisions={},
            classroom_versions_count=0,
            learning_events_count=0,
            created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
        )

    assert (output / "manifest.json").read_text(encoding="utf-8") == "operator-owned"
    assert not (output / "objects.json").exists()
    assert not (output / "manifest.sha256").exists()


def test_backup_entrypoint_creates_new_explicit_backup_and_verifies_every_file(tmp_path) -> None:
    output = tmp_path / "backup-20260821"
    older = tmp_path / "backup-older"
    older.mkdir()
    marker = older / "manifest.json"
    marker.write_text("operator-owned", encoding="utf-8")
    calls: list[tuple[str, object]] = []
    inventory = (
        ObjectInventoryEntry(
            tenant_id="tenant-a",
            key="tenants/tenant-a/classrooms/a/versions/1/document.json",
            sha256=hashlib.sha256(b"document-a").hexdigest(),
            size=len(b"document-a"),
        ),
    )

    async def dump_database(path: Path) -> TeachingBackupFacts:
        calls.append(("dump", path))
        path.write_bytes(b"consistent-postgres-dump")
        return TeachingBackupFacts(
            database_identity_sha256="d" * 64,
            schema_revisions={"tenant-a": "20260810_0017"},
            classroom_versions_count=2,
            learning_events_count=5,
        )

    async def inventory_objects() -> tuple[ObjectInventoryEntry, ...]:
        calls.append(("inventory", None))
        return inventory

    manifest = asyncio.run(
        create_teaching_backup(
            output,
            dump_database=dump_database,
            inventory_objects=inventory_objects,
            created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
        )
    )

    assert [name for name, _ in calls] == ["dump", "inventory"]
    assert calls[0][1] == output / "database.dump"
    assert marker.read_text(encoding="utf-8") == "operator-owned"
    verified = load_verified_backup(output)
    assert verified.manifest == manifest
    assert verified.database_dump == output / "database.dump"
    assert verified.object_inventory == inventory


def test_verified_backup_rejects_tampering_before_it_can_be_restored(tmp_path) -> None:
    output = tmp_path / "backup"
    output.mkdir()
    database_dump = output / "database.dump"
    database_dump.write_bytes(b"dump")
    write_backup_manifest(
        output,
        database_dump=database_dump,
        database_identity_sha256="e" * 64,
        object_inventory=(),
        schema_revisions={},
        classroom_versions_count=0,
        learning_events_count=0,
        created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
    )
    (output / "objects.json").write_text("[]\n ", encoding="utf-8")

    with pytest.raises(ValueError, match="object inventory checksum"):
        load_verified_backup(output)


def test_object_inventory_rejects_cross_tenant_keys() -> None:
    with pytest.raises(ValueError, match="tenant prefix"):
        ObjectInventoryEntry(
            tenant_id="tenant-a",
            key="tenants/tenant-b/classrooms/a/versions/1/document.json",
            sha256="a" * 64,
            size=1,
        )


def test_backup_manifest_rejects_duplicate_object_keys_before_writing(tmp_path) -> None:
    output = tmp_path / "backup"
    output.mkdir()
    database_dump = output / "database.dump"
    database_dump.write_bytes(b"dump")
    entry = ObjectInventoryEntry(
        tenant_id="tenant-a",
        key="tenants/tenant-a/classrooms/a/versions/1/document.json",
        sha256="a" * 64,
        size=1,
    )

    with pytest.raises(ValueError, match="duplicate object key"):
        write_backup_manifest(
            output,
            database_dump=database_dump,
            database_identity_sha256="b" * 64,
            object_inventory=(entry, entry),
            schema_revisions={"tenant-a": "20260810_0017"},
            classroom_versions_count=1,
            learning_events_count=1,
            created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
        )

    assert not (output / "objects.json").exists()
    assert not (output / "manifest.json").exists()
    assert not (output / "manifest.sha256").exists()


def test_backup_manifest_rejects_symlinked_output_directory(tmp_path) -> None:
    real_output = tmp_path / "real-backup"
    real_output.mkdir()
    database_dump = real_output / "database.dump"
    database_dump.write_bytes(b"dump")
    linked_output = tmp_path / "linked-backup"
    linked_output.symlink_to(real_output, target_is_directory=True)

    with pytest.raises(ValueError, match="output directory is unsafe"):
        write_backup_manifest(
            linked_output,
            database_dump=database_dump,
            database_identity_sha256="b" * 64,
            object_inventory=(),
            schema_revisions={},
            classroom_versions_count=0,
            learning_events_count=0,
            created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
        )

    assert not (real_output / "manifest.json").exists()
