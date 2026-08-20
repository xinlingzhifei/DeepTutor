"""Create tamper-evident metadata for one explicit teaching backup directory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _require_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_count(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True, order=True)
class ObjectInventoryEntry:
    tenant_id: str
    key: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        if _TENANT_ID.fullmatch(self.tenant_id) is None:
            raise ValueError("tenant_id is invalid")
        if not self.key or self.key.startswith("/") or "\\" in self.key or "\x00" in self.key:
            raise ValueError("object key is invalid")
        if any(part in {"", ".", ".."} for part in self.key.split("/")):
            raise ValueError("object key is invalid")
        if not self.key.startswith(f"tenants/{self.tenant_id}/"):
            raise ValueError("object key is outside its tenant prefix")
        _require_digest(self.sha256, "object sha256")
        _require_count(self.size, "object size")


@dataclass(frozen=True, slots=True)
class DatabaseBackup:
    file: str
    sha256: str
    size: int
    identity_sha256: str

    def __post_init__(self) -> None:
        if not self.file or Path(self.file).name != self.file:
            raise ValueError("database backup file is invalid")
        _require_digest(self.sha256, "database sha256")
        _require_digest(self.identity_sha256, "database identity sha256")
        _require_count(self.size, "database size")


@dataclass(frozen=True, slots=True)
class BackupManifest:
    schema_version: int
    created_at: datetime
    database: DatabaseBackup
    object_inventory_file: str
    object_inventory_sha256: str
    object_count: int
    schema_revisions: dict[str, str]
    classroom_versions_count: int
    learning_events_count: int

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("backup manifest schema version is unsupported")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("backup manifest time must be timezone-aware")
        if Path(self.object_inventory_file).name != self.object_inventory_file:
            raise ValueError("object inventory file is invalid")
        _require_digest(self.object_inventory_sha256, "object inventory sha256")
        _require_count(self.object_count, "object count")
        _require_count(self.classroom_versions_count, "classroom versions count")
        _require_count(self.learning_events_count, "learning events count")
        for tenant_id, revision in self.schema_revisions.items():
            if not tenant_id or not revision:
                raise ValueError("schema revision entry is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "createdAt": self.created_at.isoformat(),
            "database": {
                "file": self.database.file,
                "sha256": self.database.sha256,
                "size": self.database.size,
                "identitySha256": self.database.identity_sha256,
            },
            "objectInventoryFile": self.object_inventory_file,
            "objectInventorySha256": self.object_inventory_sha256,
            "objectCount": self.object_count,
            "schemaRevisions": dict(sorted(self.schema_revisions.items())),
            "classroomVersionsCount": self.classroom_versions_count,
            "learningEventsCount": self.learning_events_count,
        }


@dataclass(frozen=True, slots=True)
class TeachingBackupFacts:
    database_identity_sha256: str
    schema_revisions: dict[str, str]
    classroom_versions_count: int
    learning_events_count: int

    def __post_init__(self) -> None:
        _require_digest(self.database_identity_sha256, "database identity sha256")
        _require_count(self.classroom_versions_count, "classroom versions count")
        _require_count(self.learning_events_count, "learning events count")
        for tenant_id, revision in self.schema_revisions.items():
            if not tenant_id or not revision:
                raise ValueError("schema revision entry is invalid")


@dataclass(frozen=True, slots=True)
class VerifiedTeachingBackup:
    directory: Path
    database_dump: Path
    manifest: BackupManifest
    object_inventory: tuple[ObjectInventoryEntry, ...]


def _inventory_payload(entries: Iterable[ObjectInventoryEntry]) -> list[dict[str, object]]:
    ordered = tuple(sorted(entries))
    keys = tuple((entry.tenant_id, entry.key) for entry in ordered)
    if len(set(keys)) != len(keys):
        raise ValueError("object inventory contains a duplicate object key")
    return [asdict(entry) for entry in ordered]


def inventory_sha256(entries: Iterable[ObjectInventoryEntry]) -> str:
    return hashlib.sha256(_canonical_json(_inventory_payload(entries))).hexdigest()


def write_backup_manifest(
    output_dir: Path,
    *,
    database_dump: Path,
    database_identity_sha256: str,
    object_inventory: Iterable[ObjectInventoryEntry],
    schema_revisions: dict[str, str],
    classroom_versions_count: int,
    learning_events_count: int,
    created_at: datetime,
) -> BackupManifest:
    requested_output = Path(output_dir)
    if requested_output.is_symlink() or not requested_output.is_dir():
        raise ValueError("backup output directory is unsafe")
    requested_dump = Path(database_dump)
    if requested_dump.is_symlink() or not requested_dump.is_file():
        raise ValueError("database dump must be a regular file in the backup directory")
    try:
        output = requested_output.resolve(strict=True)
        dump = requested_dump.resolve(strict=True)
    except OSError:
        raise ValueError("backup inputs are unavailable") from None
    if dump.parent != output:
        raise ValueError("database dump must be a regular file in the backup directory")

    inventory_path = output / "objects.json"
    manifest_path = output / "manifest.json"
    checksum_path = output / "manifest.sha256"
    for target in (inventory_path, manifest_path, checksum_path):
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)

    entries = tuple(sorted(object_inventory))
    inventory_bytes = _canonical_json(_inventory_payload(entries))
    database_sha256, database_size = _digest_file(dump)
    manifest = BackupManifest(
        schema_version=1,
        created_at=created_at,
        database=DatabaseBackup(
            file=dump.name,
            sha256=database_sha256,
            size=database_size,
            identity_sha256=_require_digest(
                database_identity_sha256,
                "database identity sha256",
            ),
        ),
        object_inventory_file=inventory_path.name,
        object_inventory_sha256=hashlib.sha256(inventory_bytes).hexdigest(),
        object_count=len(entries),
        schema_revisions=dict(sorted(schema_revisions.items())),
        classroom_versions_count=classroom_versions_count,
        learning_events_count=learning_events_count,
    )
    manifest_bytes = _canonical_json(manifest.to_payload())
    checksum_bytes = _canonical_json({"manifestSha256": hashlib.sha256(manifest_bytes).hexdigest()})

    with inventory_path.open("xb") as handle:
        handle.write(inventory_bytes)
    with manifest_path.open("xb") as handle:
        handle.write(manifest_bytes)
    with checksum_path.open("xb") as handle:
        handle.write(checksum_bytes)
    return manifest


async def create_teaching_backup(
    output_dir: Path,
    *,
    dump_database: Callable[[Path], Awaitable[TeachingBackupFacts]],
    inventory_objects: Callable[[], Awaitable[Iterable[ObjectInventoryEntry]]],
    created_at: datetime | None = None,
) -> BackupManifest:
    """Create one new backup without inspecting or deleting sibling backups."""

    requested = Path(output_dir)
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError:
        raise ValueError("backup output parent is unavailable") from None
    output = parent / requested.name
    if requested.name in {"", ".", ".."} or output.exists() or output.is_symlink():
        raise FileExistsError(output)
    output.mkdir()

    database_dump = output / "database.dump"
    facts = await dump_database(database_dump)
    if database_dump.is_symlink() or not database_dump.is_file():
        raise ValueError("database dump was not created")
    inventory = tuple(await inventory_objects())
    return write_backup_manifest(
        output,
        database_dump=database_dump,
        database_identity_sha256=facts.database_identity_sha256,
        object_inventory=inventory,
        schema_revisions=facts.schema_revisions,
        classroom_versions_count=facts.classroom_versions_count,
        learning_events_count=facts.learning_events_count,
        created_at=created_at or datetime.now(timezone.utc),
    )


def load_backup_manifest(path: Path) -> BackupManifest:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("backup manifest is unavailable")
    payload = json.loads(source.read_text(encoding="utf-8"))
    database = payload["database"]
    return BackupManifest(
        schema_version=payload["schemaVersion"],
        created_at=datetime.fromisoformat(payload["createdAt"]),
        database=DatabaseBackup(
            file=database["file"],
            sha256=database["sha256"],
            size=database["size"],
            identity_sha256=database["identitySha256"],
        ),
        object_inventory_file=payload["objectInventoryFile"],
        object_inventory_sha256=payload["objectInventorySha256"],
        object_count=payload["objectCount"],
        schema_revisions=dict(payload["schemaRevisions"]),
        classroom_versions_count=payload["classroomVersionsCount"],
        learning_events_count=payload["learningEventsCount"],
    )


def _load_object_inventory(path: Path) -> tuple[ObjectInventoryEntry, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("object inventory is invalid") from None
    if not isinstance(payload, list):
        raise ValueError("object inventory is invalid")
    entries: list[ObjectInventoryEntry] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"tenant_id", "key", "sha256", "size"}:
            raise ValueError("object inventory is invalid")
        try:
            entries.append(ObjectInventoryEntry(**item))
        except (TypeError, ValueError):
            raise ValueError("object inventory is invalid") from None
    inventory = tuple(entries)
    _inventory_payload(inventory)
    return inventory


def load_verified_backup(directory: Path) -> VerifiedTeachingBackup:
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("backup directory is unavailable")
    try:
        root = root.resolve(strict=True)
    except OSError:
        raise ValueError("backup directory is unavailable") from None

    manifest_path = root / "manifest.json"
    checksum_path = root / "manifest.sha256"
    if manifest_path.is_symlink() or checksum_path.is_symlink():
        raise ValueError("backup manifest is unsafe")
    try:
        manifest_bytes = manifest_path.read_bytes()
        checksum_payload = json.loads(checksum_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("backup manifest checksum is invalid") from None
    if (
        not isinstance(checksum_payload, dict)
        or set(checksum_payload) != {"manifestSha256"}
        or checksum_payload["manifestSha256"] != hashlib.sha256(manifest_bytes).hexdigest()
    ):
        raise ValueError("backup manifest checksum is invalid")

    manifest = load_backup_manifest(manifest_path)
    inventory_path = root / manifest.object_inventory_file
    if inventory_path.is_symlink() or not inventory_path.is_file():
        raise ValueError("object inventory is unavailable")
    inventory_bytes = inventory_path.read_bytes()
    if hashlib.sha256(inventory_bytes).hexdigest() != manifest.object_inventory_sha256:
        raise ValueError("object inventory checksum does not match")
    inventory = _load_object_inventory(inventory_path)
    if len(inventory) != manifest.object_count:
        raise ValueError("object inventory count does not match")

    database_dump = root / manifest.database.file
    if database_dump.is_symlink() or not database_dump.is_file():
        raise ValueError("database dump is unavailable")
    database_sha256, database_size = _digest_file(database_dump)
    if database_sha256 != manifest.database.sha256 or database_size != manifest.database.size:
        raise ValueError("database dump checksum does not match")
    return VerifiedTeachingBackup(
        directory=root,
        database_dump=database_dump,
        manifest=manifest,
        object_inventory=inventory,
    )
