"""Non-destructive validation rules shared by teaching restore drills."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.backup_teaching import BackupManifest, ObjectInventoryEntry


_RESTORE_PREFIX = re.compile(r"^restore-validation/[A-Za-z0-9][A-Za-z0-9._-]{0,62}/$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _inventory_sha256(entries: tuple[ObjectInventoryEntry, ...]) -> str:
    payload = [asdict(entry) for entry in sorted(entries)]
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RestoredTeachingFacts:
    schema_revisions: dict[str, str]
    classroom_versions_count: int
    learning_events_count: int
    source_snapshot_links_valid: bool
    media_links_valid: bool
    quota_links_valid: bool
    audit_links_valid: bool


@dataclass(frozen=True, slots=True)
class RestoreValidationReport:
    ok: bool
    target_database_identity_sha256: str
    object_prefix: str
    validated: tuple[str, ...]
    failures: tuple[str, ...]


async def validate_teaching_restore(
    manifest: BackupManifest,
    *,
    target_database_identity_sha256: str,
    object_prefix: str,
    object_inventory: tuple[ObjectInventoryEntry, ...],
    restore_database: Callable[[], Awaitable[None]],
    restore_objects: Callable[[str, tuple[ObjectInventoryEntry, ...]], Awaitable[None]],
    inspect_restored_facts: Callable[[], Awaitable[RestoredTeachingFacts]],
) -> RestoreValidationReport:
    if _SHA256.fullmatch(target_database_identity_sha256) is None:
        raise ValueError("target database identity must be a SHA-256 digest")
    if target_database_identity_sha256 == manifest.database.identity_sha256:
        raise ValueError("restore validation requires a new database")
    if _RESTORE_PREFIX.fullmatch(object_prefix) is None:
        raise ValueError("object prefix must be restore-validation/<run-id>/")
    if len(object_inventory) != manifest.object_count:
        raise ValueError("object inventory count does not match")
    if _inventory_sha256(object_inventory) != manifest.object_inventory_sha256:
        raise ValueError("object inventory checksum does not match")

    await restore_database()
    await restore_objects(object_prefix, object_inventory)
    facts = await inspect_restored_facts()

    failures: list[str] = []
    if facts.schema_revisions != manifest.schema_revisions:
        failures.append("schema_revisions")
    if facts.classroom_versions_count != manifest.classroom_versions_count:
        failures.append("classroom_versions")
    if facts.learning_events_count != manifest.learning_events_count:
        failures.append("learning_events")
    for name, valid in (
        ("source_snapshots", facts.source_snapshot_links_valid),
        ("media", facts.media_links_valid),
        ("quota", facts.quota_links_valid),
        ("audit", facts.audit_links_valid),
    ):
        if not valid:
            failures.append(name)
    validated = (
        "schema_revisions",
        "classroom_versions",
        "learning_events",
        "source_snapshots",
        "media",
        "quota",
        "audit",
    )
    return RestoreValidationReport(
        ok=not failures,
        target_database_identity_sha256=target_database_identity_sha256,
        object_prefix=object_prefix,
        validated=validated,
        failures=tuple(failures),
    )
