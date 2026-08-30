"""Run a non-destructive teaching restore drill against an explicit target."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Any, TypeVar

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if TYPE_CHECKING:
    from backup_teaching import (
        BackupManifest,
        ObjectInventoryEntry,
        VerifiedTeachingBackup,
    )


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_ENVIRONMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_RESTORE_DATABASE_USER = "yfeistai_migrator"
_OWNERSHIP_VALUES = {"runner-owned-disposable", "retained-audit"}
_OBJECT_RESTORE_CONTROL_KEY = ".yfeistai-backup-restore-control/claim.json"
_PROCESS_CLEANUP_GRACE_SECONDS = 10.0
_OBJECT_CLIENT_CLEANUP_GRACE_SECONDS = _PROCESS_CLEANUP_GRACE_SECONDS
_OwnedResult = TypeVar("_OwnedResult")


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _inventory_sha256(entries: tuple[ObjectInventoryEntry, ...]) -> str:
    payload = [asdict(entry) for entry in sorted(entries)]
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class DatabaseObjectReference:
    tenant_id: str
    key: str
    sha256: str
    size: int | None = None
    version_id: str | None = None
    content_type: str | None = None
    owner_token: str | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        parts = self.key.split("/") if isinstance(self.key, str) else ()
        if (
            not isinstance(self.tenant_id, str)
            or _TENANT_ID.fullmatch(self.tenant_id) is None
            or len(parts) < 3
            or parts[0] != "tenants"
            or parts[1] != self.tenant_id
            or "\\" in self.key
            or "\x00" in self.key
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("restored database object reference is invalid")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("restored database object receipt is invalid")
        if self.size is not None and (
            isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0
        ):
            raise ValueError("restored database object receipt is invalid")
        if self.version_id is not None and (
            not isinstance(self.version_id, str)
            or not self.version_id
            or len(self.version_id) > 1024
            or any(ord(character) < 0x20 for character in self.version_id)
        ):
            raise ValueError("restored database object receipt is invalid")
        if self.content_type is not None and (
            not isinstance(self.content_type, str)
            or not self.content_type
            or self.content_type != self.content_type.strip().lower()
            or ";" in self.content_type
            or any(ord(character) < 0x20 for character in self.content_type)
        ):
            raise ValueError("restored database object receipt is invalid")
        if self.owner_token is not None and (
            not isinstance(self.owner_token, str)
            or len(self.owner_token) != 32
            or any(character not in "0123456789abcdef" for character in self.owner_token)
        ):
            raise ValueError("restored database object receipt is invalid")
        if self.source_revision is not None and (
            not isinstance(self.source_revision, str)
            or not self.source_revision
            or len(self.source_revision) > 1024
            or any(ord(character) < 0x20 for character in self.source_revision)
        ):
            raise ValueError("restored database object receipt is invalid")


@dataclass(frozen=True, slots=True, order=True)
class RestoredObjectReceipt:
    tenant_id: str
    key: str
    sha256: str
    size: int
    content_type: str
    owner_token: str
    revision: str
    version_id: str

    def __post_init__(self) -> None:
        DatabaseObjectReference(
            tenant_id=self.tenant_id,
            key=self.key,
            sha256=self.sha256,
            size=self.size,
            version_id=self.version_id,
            content_type=self.content_type,
            owner_token=self.owner_token,
            source_revision=self.revision,
        )
        if self.version_id == "null":
            raise ValueError("restored object receipt is not versioned")
        if len(self.revision) > 256 or len(self.version_id) > 256:
            raise ValueError("restored object receipt exceeds database limits")


@dataclass(frozen=True, slots=True)
class _ObjectRestoreControlClaim:
    version_id: str
    body: bytes = field(repr=False)
    body_sha256: str


@dataclass(frozen=True, slots=True)
class RestoredTeachingFacts:
    platform_schema_revision: str
    schema_revisions: dict[str, str]
    classroom_versions_count: int
    learning_events_count: int
    source_snapshot_links_valid: bool
    media_links_valid: bool
    quota_links_valid: bool
    audit_links_valid: bool
    database_object_references: tuple[DatabaseObjectReference, ...] = ()

    @property
    def referenced_object_keys(self) -> tuple[str, ...]:
        return tuple(reference.key for reference in self.database_object_references)


@dataclass(frozen=True, slots=True)
class RestoreValidationReport:
    ok: bool
    target_database_identity_sha256: str
    object_prefix: str
    validated: tuple[str, ...]
    failures: tuple[str, ...]
    target_evidence: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class TargetDatabaseState:
    identity_sha256: str
    user_object_count: int
    current_role: str | None = None
    database_owner: str | None = None


@dataclass(frozen=True, slots=True)
class TargetObjectState:
    identity_sha256: str
    versioning_enabled: bool
    object_count: int
    version_count: int
    delete_marker_count: int
    owner_id_sha256: str


@dataclass(frozen=True, slots=True)
class TargetObservation:
    database: TargetDatabaseState
    objects: TargetObjectState


@dataclass(frozen=True, slots=True)
class RestoreTarget:
    database_url: str = field(repr=False)
    app_database_url: str = field(repr=False)
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    database_password: str = field(repr=False)
    object_endpoint: str
    object_namespace_id: str
    object_bucket: str
    object_region: str
    object_access_key: str = field(repr=False)
    object_secret_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RestoreOperatorRuntime:
    target_loader: Callable[[Path, Path], RestoreTarget]
    engine_factory: Callable[[str], Any]
    object_client_factory: Callable[[RestoreTarget], Any]
    database_probe: Callable[[Any], Awaitable[TargetDatabaseState]]
    facts_inspector: Callable[[Any], Awaitable[RestoredTeachingFacts]]
    process_runner: Callable[[tuple[str, ...], dict[str, str]], Awaitable[int]]
    receipt_rebinder: Callable[
        [Any, tuple[RestoredObjectReceipt, ...]],
        Awaitable[None],
    ]
    app_access_granter: Callable[[Any], Awaitable[None]]
    app_engine_factory: Callable[[str], Any]
    app_access_probe: Callable[[Any], Awaitable[bool]]
    target_exclusion: Callable[[Any, str], Any] | None = None
    target_exclusion_mode: str | None = None
    object_state_probe: Callable[[Any, RestoreTarget], Awaitable[TargetObjectState]] | None = None


async def _execute_measured_target_operation(
    *,
    exclusion: Callable[[], Any],
    observe: Callable[[], Awaitable[Any]],
    mutate: Callable[[], Awaitable[Any]],
    validate: Callable[[object, object, object], Any],
) -> Any:
    async with exclusion():
        before = await observe()
        mutation_result = await mutate()
        after = await observe()
        return validate(before, after, mutation_result)


def _validate_restore_inputs(
    manifest: BackupManifest,
    *,
    target_database_identity_sha256: str,
    object_prefix: str,
    object_inventory: tuple[ObjectInventoryEntry, ...],
) -> None:
    if _SHA256.fullmatch(target_database_identity_sha256) is None:
        raise ValueError("target database identity must be a SHA-256 digest")
    if target_database_identity_sha256 == manifest.database.identity_sha256:
        raise ValueError("restore validation requires a new database")
    if object_prefix != "":
        raise ValueError("object prefix must be empty for an isolated target bucket")
    if len(object_inventory) != manifest.object_count:
        raise ValueError("object inventory count does not match")
    if _inventory_sha256(object_inventory) != manifest.object_inventory_sha256:
        raise ValueError("object inventory checksum does not match")
    if any(entry.tenant_id not in manifest.schema_revisions for entry in object_inventory):
        raise ValueError("object inventory is outside the manifest tenant inventory")


async def validate_teaching_restore(
    manifest: BackupManifest,
    *,
    target_database_identity_sha256: str,
    object_prefix: str,
    object_inventory: tuple[ObjectInventoryEntry, ...],
    restore_database: Callable[[], Awaitable[None]],
    restore_objects: Callable[
        [str, tuple[ObjectInventoryEntry, ...]],
        Awaitable[tuple[RestoredObjectReceipt, ...] | None],
    ],
    inspect_restored_facts: Callable[[], Awaitable[RestoredTeachingFacts]],
) -> RestoreValidationReport:
    _validate_restore_inputs(
        manifest,
        target_database_identity_sha256=target_database_identity_sha256,
        object_prefix=object_prefix,
        object_inventory=object_inventory,
    )

    await restore_database()
    restored_receipts = await restore_objects(object_prefix, object_inventory)
    facts = await inspect_restored_facts()

    failures: list[str] = []
    if facts.platform_schema_revision != manifest.platform_schema_revision:
        failures.append("platform_schema_revision")
    if facts.schema_revisions != manifest.schema_revisions:
        failures.append("schema_revisions")
    if facts.classroom_versions_count != manifest.classroom_versions_count:
        failures.append("classroom_versions")
    if facts.learning_events_count != manifest.learning_events_count:
        failures.append("learning_events")
    if restored_receipts is None:
        expected_objects_by_key = {entry.key: entry for entry in object_inventory}
    else:
        if any(not isinstance(receipt, RestoredObjectReceipt) for receipt in restored_receipts):
            raise ValueError("restored object receipt is invalid")
        expected_objects_by_key = {receipt.key: receipt for receipt in restored_receipts}
        if len(expected_objects_by_key) != len(restored_receipts):
            raise ValueError("restored object receipts contain duplicate keys")
    if any(
        reference.key not in expected_objects_by_key
        or expected_objects_by_key[reference.key].sha256 != reference.sha256
        or (
            reference.size is not None
            and expected_objects_by_key[reference.key].size != reference.size
        )
        or (
            reference.version_id is not None
            and getattr(expected_objects_by_key[reference.key], "version_id", None)
            != reference.version_id
        )
        or (
            reference.content_type is not None
            and getattr(expected_objects_by_key[reference.key], "content_type", None)
            != reference.content_type
        )
        or (
            reference.owner_token is not None
            and getattr(expected_objects_by_key[reference.key], "owner_token", None)
            != reference.owner_token
        )
        or (
            reference.source_revision is not None
            and getattr(
                expected_objects_by_key[reference.key],
                "source_revision",
                getattr(expected_objects_by_key[reference.key], "revision", None),
            )
            != reference.source_revision
        )
        for reference in facts.database_object_references
    ):
        failures.append("database_object_references")
    for name, valid in (
        ("source_snapshots", facts.source_snapshot_links_valid),
        ("media", facts.media_links_valid),
        ("quota", facts.quota_links_valid),
        ("audit", facts.audit_links_valid),
    ):
        if not valid:
            failures.append(name)
    validated = (
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
    return RestoreValidationReport(
        ok=not failures,
        target_database_identity_sha256=target_database_identity_sha256,
        object_prefix=object_prefix,
        validated=validated,
        failures=tuple(failures),
    )


def _load_verified_backup(directory: Path) -> VerifiedTeachingBackup:
    from backup_teaching import load_verified_backup

    return load_verified_backup(directory)


def _reverify_verified_backup(backup: VerifiedTeachingBackup) -> VerifiedTeachingBackup:
    from backup_teaching import reverify_verified_backup

    try:
        return reverify_verified_backup(backup)
    except Exception:
        raise ValueError("backup changed after verification") from None


def load_restore_target(config_path: Path, secret_dir: Path) -> RestoreTarget:
    """Load the target through the same platform config/secret contract as backup."""

    from backup_teaching import (
        load_operator_backup_config,
        load_operator_backup_secrets,
    )

    try:
        config = load_operator_backup_config(Path(config_path))
        secrets = load_operator_backup_secrets(Path(secret_dir), config)
    except Exception:
        raise ValueError("restore target configuration is invalid") from None

    try:
        from sqlalchemy.engine import URL

        database_url = URL.create(
            drivername="postgresql+asyncpg",
            username=_RESTORE_DATABASE_USER,
            password=secrets.database_migration_password,
            host=config.database_host,
            port=config.database_port,
            database=config.database_name,
        ).render_as_string(hide_password=False)
        app_database_url = URL.create(
            drivername="postgresql+asyncpg",
            username="yfeistai_app",
            password=secrets.database_password,
            host=config.database_host,
            port=config.database_port,
            database=config.database_name,
        ).render_as_string(hide_password=False)
    except Exception:
        raise ValueError("restore target database configuration is invalid") from None
    return RestoreTarget(
        database_url=database_url,
        app_database_url=app_database_url,
        database_host=config.database_host,
        database_port=config.database_port,
        database_name=config.database_name,
        database_user=_RESTORE_DATABASE_USER,
        database_password=secrets.database_migration_password,
        object_endpoint=config.object_store_endpoint,
        object_namespace_id=config.object_store_namespace_id,
        object_bucket=config.object_store_bucket,
        object_region=config.object_store_region,
        object_access_key=secrets.minio_access_key,
        object_secret_key=secrets.minio_secret_key,
    )


def _default_engine_factory(database_url: str) -> Any:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(database_url, poolclass=NullPool)


def _default_object_client_factory(target: RestoreTarget) -> Any:
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=target.object_endpoint,
        region_name=target.object_region,
        aws_access_key_id=target.object_access_key,
        aws_secret_access_key=target.object_secret_key,
    )


async def _default_database_probe(engine: Any) -> TargetDatabaseState:
    from sqlalchemy import text

    async with engine.connect() as connection:
        identity_row = (
            await connection.execute(
                text(
                    "SELECT controls.system_identifier::text AS system_identifier, "
                    "databases.oid::text AS database_oid, "
                    "databases.datname::text AS database_name "
                    "FROM pg_control_system() AS controls CROSS JOIN pg_database AS databases "
                    "WHERE databases.datname = current_database()"
                )
            )
        ).one()
        object_count = (
            await connection.execute(
                text(
                    "WITH candidate_namespaces AS ("
                    "SELECT oid, nspname FROM pg_namespace "
                    "WHERE nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'"
                    "), catalog_objects AS ("
                    "SELECT 'pg_namespace'::regclass AS classid, namespaces.oid AS objid "
                    "FROM candidate_namespaces AS namespaces "
                    "WHERE namespaces.nspname <> 'public' "
                    "UNION ALL SELECT 'pg_class'::regclass, classes.oid FROM pg_class AS classes "
                    "JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = classes.relnamespace "
                    "WHERE classes.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') "
                    "UNION ALL SELECT 'pg_proc'::regclass, routines.oid FROM pg_proc AS routines "
                    "JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = routines.pronamespace "
                    "UNION ALL SELECT 'pg_type'::regclass, types.oid FROM pg_type AS types "
                    "JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = types.typnamespace "
                    "WHERE types.typtype IN ('b', 'c', 'd', 'e', 'r', 'm') "
                    "UNION ALL SELECT 'pg_operator'::regclass, operators.oid "
                    "FROM pg_operator AS operators JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = operators.oprnamespace "
                    "UNION ALL SELECT 'pg_collation'::regclass, collations.oid "
                    "FROM pg_collation AS collations JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = collations.collnamespace "
                    "UNION ALL SELECT 'pg_conversion'::regclass, conversions.oid "
                    "FROM pg_conversion AS conversions JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = conversions.connamespace "
                    "UNION ALL SELECT 'pg_ts_config'::regclass, configurations.oid "
                    "FROM pg_ts_config AS configurations JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = configurations.cfgnamespace "
                    "UNION ALL SELECT 'pg_ts_dict'::regclass, dictionaries.oid "
                    "FROM pg_ts_dict AS dictionaries JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = dictionaries.dictnamespace "
                    "UNION ALL SELECT 'pg_ts_parser'::regclass, parsers.oid "
                    "FROM pg_ts_parser AS parsers JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = parsers.prsnamespace "
                    "UNION ALL SELECT 'pg_ts_template'::regclass, templates.oid "
                    "FROM pg_ts_template AS templates JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = templates.tmplnamespace "
                    "UNION ALL SELECT 'pg_opclass'::regclass, operator_classes.oid "
                    "FROM pg_opclass AS operator_classes JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = operator_classes.opcnamespace "
                    "UNION ALL SELECT 'pg_opfamily'::regclass, operator_families.oid "
                    "FROM pg_opfamily AS operator_families JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = operator_families.opfnamespace "
                    "UNION ALL SELECT 'pg_statistic_ext'::regclass, statistics.oid "
                    "FROM pg_statistic_ext AS statistics JOIN candidate_namespaces AS namespaces "
                    "ON namespaces.oid = statistics.stxnamespace "
                    "UNION ALL SELECT 'pg_event_trigger'::regclass, oid FROM pg_event_trigger "
                    "UNION ALL SELECT 'pg_foreign_data_wrapper'::regclass, oid "
                    "FROM pg_foreign_data_wrapper "
                    "UNION ALL SELECT 'pg_foreign_server'::regclass, oid FROM pg_foreign_server "
                    "UNION ALL SELECT 'pg_user_mapping'::regclass, oid FROM pg_user_mapping "
                    "UNION ALL SELECT 'pg_publication'::regclass, oid FROM pg_publication "
                    "UNION ALL SELECT 'pg_subscription'::regclass, oid FROM pg_subscription "
                    "UNION ALL SELECT 'pg_extension'::regclass, oid FROM pg_extension "
                    "WHERE extname <> 'plpgsql' "
                    "UNION ALL SELECT 'pg_cast'::regclass, oid FROM pg_cast WHERE oid >= 16384 "
                    "UNION ALL SELECT 'pg_transform'::regclass, oid "
                    "FROM pg_transform WHERE oid >= 16384 "
                    "UNION ALL SELECT 'pg_language'::regclass, oid "
                    "FROM pg_language WHERE oid >= 16384 "
                    "UNION ALL SELECT 'pg_largeobject_metadata'::regclass, oid "
                    "FROM pg_largeobject_metadata WHERE oid >= 16384 "
                    "UNION ALL SELECT 'pg_default_acl'::regclass, oid "
                    "FROM pg_default_acl WHERE oid >= 16384"
                    "), user_objects AS ("
                    "SELECT objects.objid FROM catalog_objects AS objects "
                    "WHERE NOT EXISTS (SELECT 1 FROM pg_depend AS dependencies "
                    "WHERE dependencies.classid = objects.classid "
                    "AND dependencies.objid = objects.objid AND dependencies.deptype = 'e')"
                    ") SELECT count(*) FROM user_objects"
                )
            )
        ).scalar_one()
    system_identifier = identity_row.system_identifier
    database_oid = identity_row.database_oid
    database_name = identity_row.database_name
    if any(
        not isinstance(value, str) or not value
        for value in (system_identifier, database_oid, database_name)
    ):
        raise RuntimeError("restore target physical database identity is unavailable")
    identity_payload = {
        "systemIdentifier": system_identifier,
        "databaseOid": database_oid,
        "databaseName": database_name,
    }
    identity = hashlib.sha256(_canonical_json(identity_payload)).hexdigest()
    return TargetDatabaseState(
        identity_sha256=identity,
        user_object_count=int(object_count),
    )


async def _default_measured_database_probe(engine: Any) -> TargetDatabaseState:
    from sqlalchemy import text

    state = await _default_database_probe(engine)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT current_user::text AS current_role, roles.rolname::text AS owner "
                    "FROM pg_database AS databases JOIN pg_roles AS roles "
                    "ON roles.oid = databases.datdba WHERE databases.datname = current_database()"
                )
            )
        ).one()
    current_role = str(row[0])
    database_owner = str(row[1])
    if not current_role or not database_owner:
        raise RuntimeError("restore target database ownership could not be inspected")
    return replace(
        state,
        current_role=current_role,
        database_owner=database_owner,
    )


async def _default_rebind_database_object_receipts(
    engine: Any,
    receipts: tuple[RestoredObjectReceipt, ...],
) -> None:
    from sqlalchemy import text

    async with engine.begin() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT tenant_id, schema_name FROM platform.tenant_schema_states "
                    "WHERE tenant_schema_states.status = 'active' ORDER BY tenant_id"
                )
            )
        ).all()
        schemas: dict[str, str] = {}
        for tenant_id, schema_name in rows:
            tenant = str(tenant_id)
            schema = str(schema_name)
            if (
                _TENANT_ID.fullmatch(tenant) is None
                or _SAFE_SCHEMA.fullmatch(schema) is None
                or tenant in schemas
            ):
                raise ValueError("restored tenant schema metadata is invalid")
            schemas[tenant] = schema
        if any(receipt.tenant_id not in schemas for receipt in receipts):
            raise ValueError("restored object receipt tenant is not active")

        for receipt in sorted(receipts):
            quoted_schema = '"' + schemas[receipt.tenant_id].replace('"', '""') + '"'
            parameters = {
                "key": receipt.key,
                "owner_token": receipt.owner_token,
                "revision": receipt.revision,
                "sha256": receipt.sha256,
                "version_id": receipt.version_id,
            }
            await connection.execute(
                text(
                    f"UPDATE {quoted_schema}.source_uploads "
                    "SET object_revision = :revision, object_version_id = :version_id "
                    "WHERE object_key = :key AND sha256 = :sha256 "
                    "AND ownership_token = :owner_token AND status = 'uploaded'"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    f"UPDATE {quoted_schema}.classroom_draft_media "
                    "SET object_revision = :revision "
                    "WHERE object_key = :key AND sha256 = :sha256 "
                    "AND ownership_token = :owner_token AND status = 'uploaded'"
                ),
                parameters,
            )


async def _default_grant_app_access(engine: Any) -> None:
    from sqlalchemy import text

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT schema_name FROM platform.tenant_schema_states "
                    "WHERE status = 'active' ORDER BY tenant_id"
                )
            )
        ).all()
    schemas = tuple(str(row[0]) for row in rows)
    if len(set(schemas)) != len(schemas) or any(
        _SAFE_SCHEMA.fullmatch(schema) is None for schema in schemas
    ):
        raise ValueError("restored tenant schema metadata is invalid")
    from migrate_teaching import _grant_app_access

    await _grant_app_access(engine, schemas)


async def _default_app_access_probe(engine: Any) -> bool:
    from sqlalchemy import text

    async with engine.connect() as connection:
        role_valid = (
            await connection.execute(text("SELECT current_user = 'yfeistai_app'"))
        ).scalar_one()
        if role_valid is not True:
            return False
        rows = (
            await connection.execute(
                text(
                    "SELECT schema_name FROM platform.tenant_schema_states "
                    "WHERE status = 'active' ORDER BY tenant_id"
                )
            )
        ).all()
        schemas = ("platform", *(str(row[0]) for row in rows))
        if len(set(schemas)) != len(schemas) or any(
            schema != "platform" and _SAFE_SCHEMA.fullmatch(schema) is None for schema in schemas
        ):
            raise ValueError("restored tenant schema metadata is invalid")
        route_attempt_access_valid = (
            await connection.execute(
                text(
                    "SELECT NOT has_table_privilege(current_user, "
                    "'platform.generation_route_attempts', 'INSERT') "
                    "AND NOT has_table_privilege(current_user, "
                    "'platform.generation_route_attempts', 'SELECT') "
                    "AND NOT has_table_privilege(current_user, "
                    "'platform.generation_route_attempts', 'UPDATE') "
                    "AND NOT has_table_privilege(current_user, "
                    "'platform.generation_route_attempts', 'DELETE') "
                    "AND NOT has_table_privilege(current_user, "
                    "'platform.generation_route_attempts', 'TRUNCATE') "
                    "AND has_function_privilege(current_user, "
                    "'platform.record_generation_route_attempt(text, text, text, "
                    "integer, text, text, text, text, text, text, text, text, "
                    "text, text, text)', "
                    "'EXECUTE') "
                    "AND has_function_privilege(current_user, "
                    "'platform.read_generation_route_attempts(text, text, text, "
                    "text, text, text, text)', 'EXECUTE')"
                )
            )
        ).scalar_one()
        if route_attempt_access_valid is not True:
            return False
        for schema in schemas:
            privileges_valid = (
                await connection.execute(
                    text(
                        "SELECT has_schema_privilege(current_user, :schema, 'USAGE') "
                        "AND COALESCE((SELECT bool_and(has_table_privilege("
                        "current_user, format('%I.%I', schemaname, tablename), "
                        "'SELECT') AND has_table_privilege(current_user, "
                        "format('%I.%I', schemaname, tablename), 'INSERT') "
                        "AND has_table_privilege(current_user, format('%I.%I', "
                        "schemaname, tablename), 'UPDATE') AND has_table_privilege("
                        "current_user, format('%I.%I', schemaname, tablename), "
                        "'DELETE')) FROM pg_tables "
                        "WHERE schemaname = :schema "
                        "AND NOT (schemaname = 'platform' "
                        "AND tablename = 'generation_route_attempts')), TRUE) "
                        "AND COALESCE((SELECT bool_and(has_sequence_privilege("
                        "current_user, format('%I.%I', schemaname, sequencename), "
                        "'USAGE') AND has_sequence_privilege(current_user, "
                        "format('%I.%I', schemaname, sequencename), 'SELECT')) "
                        "FROM pg_sequences "
                        "WHERE schemaname = :schema), TRUE)"
                    ),
                    {"schema": schema},
                )
            ).scalar_one()
            if privileges_valid is not True:
                return False
            quoted_schema = '"' + schema.replace('"', '""') + '"'
            relation = "tenant_schema_states" if schema == "platform" else "alembic_version"
            await connection.execute(text(f"SELECT count(*) FROM {quoted_schema}.{relation}"))
    return True


_ForeignKeyBinding = tuple[str, tuple[str, ...], str, tuple[str, ...]]

_SOURCE_RELATIONSHIP_CONSTRAINTS: dict[str, _ForeignKeyBinding] = {
    "fk_source_snapshots_upload_tenant": (
        "source_snapshots",
        ("source_upload_id", "tenant_id"),
        "source_uploads",
        ("id", "tenant_id"),
    ),
    "fk_tenant_source_bindings_snapshot_tenant": (
        "tenant_source_bindings",
        ("source_snapshot_id", "tenant_id"),
        "source_snapshots",
        ("id", "tenant_id"),
    ),
    "fk_teaching_briefs_snapshot_tenant": (
        "teaching_briefs",
        ("source_snapshot_id", "tenant_id"),
        "source_snapshots",
        ("id", "tenant_id"),
    ),
}
_CLASSROOM_RELATIONSHIP_CONSTRAINTS: dict[str, _ForeignKeyBinding] = {
    "fk_classroom_assets_current_version_classroom_tenant": (
        "classroom_assets",
        ("current_published_version_id", "id", "tenant_id"),
        "classroom_versions",
        ("id", "classroom_id", "tenant_id"),
    ),
    "fk_classroom_versions_asset_tenant_classroom_assets": (
        "classroom_versions",
        ("classroom_id", "tenant_id"),
        "classroom_assets",
        ("id", "tenant_id"),
    ),
    "fk_classroom_versions_job_tenant_generation_jobs": (
        "classroom_versions",
        ("generation_job_id", "tenant_id"),
        "generation_jobs",
        ("id", "tenant_id"),
    ),
    "fk_classroom_versions_source_classroom_tenant": (
        "classroom_versions",
        ("source_version_id", "classroom_id", "tenant_id"),
        "classroom_versions",
        ("id", "classroom_id", "tenant_id"),
    ),
}
_MEDIA_RELATIONSHIP_CONSTRAINTS: dict[str, _ForeignKeyBinding] = {
    "fk_classroom_draft_media_asset_tenant_classroom_assets": (
        "classroom_draft_media",
        ("classroom_id", "tenant_id"),
        "classroom_assets",
        ("id", "tenant_id"),
    )
}
_EXPORT_RELATIONSHIP_CONSTRAINTS: dict[str, _ForeignKeyBinding] = {
    "fk_classroom_exports_asset_tenant_classroom_assets": (
        "classroom_exports",
        ("classroom_id", "tenant_id"),
        "classroom_assets",
        ("id", "tenant_id"),
    ),
    "fk_classroom_exports_version_classroom_tenant": (
        "classroom_exports",
        ("classroom_version_id", "classroom_id", "tenant_id"),
        "classroom_versions",
        ("id", "classroom_id", "tenant_id"),
    ),
    "fk_classroom_exports_draft_classroom_tenant": (
        "classroom_exports",
        ("classroom_draft_id", "classroom_id", "tenant_id"),
        "classroom_drafts",
        ("id", "classroom_id", "tenant_id"),
    ),
    "fk_classroom_exports_job_tenant_generation_jobs": (
        "classroom_exports",
        ("generation_job_id", "tenant_id"),
        "generation_jobs",
        ("id", "tenant_id"),
    ),
}
_QUOTA_RELATIONSHIP_CONSTRAINTS: dict[str, _ForeignKeyBinding] = {
    "fk_quota_ledger_job_tenant_generation_jobs": (
        "quota_ledger",
        ("job_id", "tenant_id"),
        "generation_jobs",
        ("id", "tenant_id"),
    )
}
_AUDIT_RELATIONSHIP_CONSTRAINTS: dict[str, _ForeignKeyBinding] = {
    "fk_audit_log_tenant_id_tenants": (
        "audit_log",
        ("tenant_id",),
        "tenants",
        ("id",),
    )
}
_FOREIGN_KEY_DELETE_ACTIONS = {
    "fk_classroom_draft_media_asset_tenant_classroom_assets": "c",
    "fk_quota_ledger_job_tenant_generation_jobs": "c",
    "fk_audit_log_tenant_id_tenants": "n",
}


async def _constraint_group_valid(
    connection: Any,
    schema: str,
    expected: dict[str, _ForeignKeyBinding],
) -> bool:
    from sqlalchemy import text

    rows = (
        await connection.execute(
            text(
                "SELECT constraints.conname, source_relations.relname, "
                "ARRAY(SELECT source_attributes.attname::text "
                "FROM unnest(constraints.conkey) WITH ORDINALITY "
                "AS source_keys(attnum, position) "
                "JOIN pg_attribute AS source_attributes "
                "ON source_attributes.attrelid = constraints.conrelid "
                "AND source_attributes.attnum = source_keys.attnum "
                "ORDER BY source_keys.position), "
                "target_relations.relname, "
                "ARRAY(SELECT target_attributes.attname::text "
                "FROM unnest(constraints.confkey) WITH ORDINALITY "
                "AS target_keys(attnum, position) "
                "JOIN pg_attribute AS target_attributes "
                "ON target_attributes.attrelid = constraints.confrelid "
                "AND target_attributes.attnum = target_keys.attnum "
                "ORDER BY target_keys.position), constraints.convalidated, "
                "constraints.confdeltype::text, constraints.confupdtype::text, "
                "constraints.confmatchtype::text, constraints.condeferrable, "
                "constraints.condeferred "
                "FROM pg_constraint AS constraints "
                "JOIN pg_class AS source_relations "
                "ON source_relations.oid = constraints.conrelid "
                "JOIN pg_namespace AS source_namespaces "
                "ON source_namespaces.oid = source_relations.relnamespace "
                "JOIN pg_class AS target_relations "
                "ON target_relations.oid = constraints.confrelid "
                "JOIN pg_namespace AS target_namespaces "
                "ON target_namespaces.oid = target_relations.relnamespace "
                "WHERE constraints.contype = 'f' "
                "AND source_namespaces.nspname = :schema "
                "AND target_namespaces.nspname = :schema "
                "AND constraints.conname::text = "
                "ANY(CAST(:constraint_names AS text[]))"
            ),
            {"schema": schema, "constraint_names": list(expected)},
        )
    ).all()
    try:
        actual = {
            str(name): (
                str(source_table),
                tuple(str(column) for column in source_columns),
                str(target_table),
                tuple(str(column) for column in target_columns),
                str(delete_action),
                str(update_action),
                str(match_type),
                deferrable,
                deferred,
            )
            for (
                name,
                source_table,
                source_columns,
                target_table,
                target_columns,
                validated,
                delete_action,
                update_action,
                match_type,
                deferrable,
                deferred,
            ) in rows
            if validated is True and isinstance(deferrable, bool) and isinstance(deferred, bool)
        }
    except (TypeError, ValueError):
        return False
    expected_with_actions = {
        name: (
            *binding,
            _FOREIGN_KEY_DELETE_ACTIONS.get(name, "r"),
            "a",
            "s",
            False,
            False,
        )
        for name, binding in expected.items()
    }
    return (
        len(rows) == len(expected_with_actions) == len(actual) and actual == expected_with_actions
    )


async def _default_facts_inspector(engine: Any) -> RestoredTeachingFacts:
    from sqlalchemy import text

    schema_revisions: dict[str, str] = {}
    database_object_references: dict[str, DatabaseObjectReference] = {}
    classroom_versions_count = 0
    learning_events_count = 0
    async with engine.connect() as connection:
        platform_revision = (
            await connection.execute(text("SELECT version_num FROM platform.alembic_version"))
        ).scalar_one()
        if not isinstance(platform_revision, str) or not platform_revision:
            raise ValueError("restored platform revision is invalid")
        tenant_rows = (
            await connection.execute(
                text(
                    "SELECT tenant_id, schema_name, revision "
                    ", status "
                    "FROM platform.tenant_schema_states ORDER BY tenant_id"
                )
            )
        ).all()
        relationships = {name: True for name in ("source", "classroom", "media", "export", "quota")}
        seen_schemas: set[str] = set()
        for tenant_id, schema, revision, status in tenant_rows:
            schema_name = str(schema)
            tenant_key = str(tenant_id)
            if (
                _SAFE_SCHEMA.fullmatch(schema_name) is None
                or schema_name in seen_schemas
                or not isinstance(status, str)
            ):
                raise ValueError("restored tenant schema metadata is invalid")
            seen_schemas.add(schema_name)
            if status != "active":
                continue
            if not isinstance(revision, str) or not revision or tenant_key in schema_revisions:
                raise ValueError("restored active tenant schema metadata is invalid")
            quoted_schema = '"' + schema_name.replace('"', '""') + '"'
            actual_revision = (
                await connection.execute(
                    text(f"SELECT version_num FROM {quoted_schema}.alembic_version")
                )
            ).scalar_one()
            if actual_revision != revision:
                raise ValueError("restored tenant revision does not match platform state")
            schema_revisions[tenant_key] = revision
            classroom_versions_count += int(
                (
                    await connection.execute(
                        text(f"SELECT count(*) FROM {quoted_schema}.classroom_versions")
                    )
                ).scalar_one()
            )
            learning_events_count += int(
                (
                    await connection.execute(
                        text(f"SELECT count(*) FROM {quoted_schema}.learning_events")
                    )
                ).scalar_one()
            )
            object_rows = (
                await connection.execute(
                    text(
                        f"SELECT object_key::text AS object_key, sha256::text AS sha256, "
                        "size_bytes::bigint AS size_bytes, "
                        "object_version_id::text AS version_id, "
                        "NULL::text AS content_type, ownership_token::text AS owner_token, "
                        "object_revision::text AS source_revision "
                        f"FROM {quoted_schema}.source_uploads "
                        "WHERE object_key IS NOT NULL "
                        "UNION ALL SELECT document_object_key::text AS object_key, "
                        "document_sha256::text AS sha256, NULL::bigint AS size_bytes, "
                        "NULL::text AS version_id, NULL::text AS content_type, "
                        "NULL::text AS owner_token, NULL::text AS source_revision "
                        f"FROM {quoted_schema}.classroom_versions "
                        "WHERE document_object_key IS NOT NULL "
                        "UNION ALL SELECT object_key::text AS object_key, "
                        "sha256::text AS sha256, size_bytes::bigint AS size_bytes, "
                        "NULL::text AS version_id, mime_type::text AS content_type, "
                        "NULL::text AS owner_token, NULL::text AS source_revision "
                        f"FROM {quoted_schema}.classroom_artifacts "
                        "WHERE object_key IS NOT NULL "
                        "UNION ALL SELECT object_key::text AS object_key, "
                        "sha256::text AS sha256, size_bytes::bigint AS size_bytes, "
                        "NULL::text AS version_id, mime_type::text AS content_type, "
                        "ownership_token::text AS owner_token, "
                        "object_revision::text AS source_revision "
                        f"FROM {quoted_schema}.classroom_draft_media "
                        "WHERE object_key IS NOT NULL "
                        "UNION ALL SELECT input_manifest_object_key::text AS object_key, "
                        "input_manifest_sha256::text AS sha256, "
                        "NULL::bigint AS size_bytes, NULL::text AS version_id, "
                        "NULL::text AS content_type, NULL::text AS owner_token, "
                        "NULL::text AS source_revision "
                        f"FROM {quoted_schema}.classroom_exports "
                        "WHERE input_manifest_object_key IS NOT NULL "
                        "UNION ALL SELECT object_key::text AS object_key, "
                        "sha256::text AS sha256, size_bytes::bigint AS size_bytes, "
                        "NULL::text AS version_id, mime_type::text AS content_type, "
                        "NULL::text AS owner_token, NULL::text AS source_revision "
                        f"FROM {quoted_schema}.classroom_exports "
                        "WHERE object_key IS NOT NULL"
                    )
                )
            ).all()
            for (
                object_key,
                sha256,
                size_bytes,
                version_id,
                content_type,
                owner_token,
                source_revision,
            ) in object_rows:
                reference = DatabaseObjectReference(
                    tenant_id=tenant_key,
                    key=object_key,
                    sha256=sha256,
                    size=size_bytes,
                    version_id=version_id,
                    content_type=content_type,
                    owner_token=owner_token,
                    source_revision=source_revision,
                )
                existing = database_object_references.get(object_key)
                if existing is not None:
                    if (
                        existing.sha256 != reference.sha256
                        or (
                            existing.size is not None
                            and reference.size is not None
                            and existing.size != reference.size
                        )
                        or (
                            existing.version_id is not None
                            and reference.version_id is not None
                            and existing.version_id != reference.version_id
                        )
                        or (
                            existing.content_type is not None
                            and reference.content_type is not None
                            and existing.content_type != reference.content_type
                        )
                        or (
                            existing.owner_token is not None
                            and reference.owner_token is not None
                            and existing.owner_token != reference.owner_token
                        )
                        or (
                            existing.source_revision is not None
                            and reference.source_revision is not None
                            and existing.source_revision != reference.source_revision
                        )
                    ):
                        raise ValueError("restored database object receipts conflict")
                    reference = DatabaseObjectReference(
                        tenant_id=tenant_key,
                        key=object_key,
                        sha256=reference.sha256,
                        size=existing.size if existing.size is not None else reference.size,
                        version_id=(
                            existing.version_id
                            if existing.version_id is not None
                            else reference.version_id
                        ),
                        content_type=(
                            existing.content_type
                            if existing.content_type is not None
                            else reference.content_type
                        ),
                        owner_token=(
                            existing.owner_token
                            if existing.owner_token is not None
                            else reference.owner_token
                        ),
                        source_revision=(
                            existing.source_revision
                            if existing.source_revision is not None
                            else reference.source_revision
                        ),
                    )
                database_object_references[object_key] = reference
            for name, constraints in (
                ("source", _SOURCE_RELATIONSHIP_CONSTRAINTS),
                ("classroom", _CLASSROOM_RELATIONSHIP_CONSTRAINTS),
                ("media", _MEDIA_RELATIONSHIP_CONSTRAINTS),
                ("export", _EXPORT_RELATIONSHIP_CONSTRAINTS),
                ("quota", _QUOTA_RELATIONSHIP_CONSTRAINTS),
            ):
                valid = await _constraint_group_valid(
                    connection,
                    schema_name,
                    constraints,
                )
                relationships[name] = relationships[name] and valid
        audit_constraint_valid = await _constraint_group_valid(
            connection,
            "platform",
            _AUDIT_RELATIONSHIP_CONSTRAINTS,
        )
        audit_rows_valid = bool(
            (
                await connection.execute(
                    text(
                        "SELECT NOT EXISTS (SELECT 1 FROM platform.audit_log AS audit "
                        "LEFT JOIN platform.tenants AS tenants ON tenants.id = audit.tenant_id "
                        "WHERE btrim(audit.resource_type) = '' "
                        "OR (audit.resource_id IS NOT NULL "
                        "AND btrim(audit.resource_id) = '') "
                        "OR (audit.tenant_id IS NOT NULL AND tenants.id IS NULL))"
                    )
                )
            ).scalar_one()
        )
    return RestoredTeachingFacts(
        platform_schema_revision=platform_revision,
        schema_revisions=schema_revisions,
        classroom_versions_count=classroom_versions_count,
        learning_events_count=learning_events_count,
        source_snapshot_links_valid=relationships["source"],
        media_links_valid=(
            relationships["classroom"] and relationships["media"] and relationships["export"]
        ),
        quota_links_valid=relationships["quota"],
        audit_links_valid=audit_constraint_valid and audit_rows_valid,
        database_object_references=tuple(
            database_object_references[key] for key in sorted(database_object_references)
        ),
    )


async def _default_process_runner(
    argv: tuple[str, ...],
    environment: dict[str, str],
    *,
    deadline_monotonic: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    try:
        return await _await_owned_operation(
            asyncio.to_thread(
                _run_process_with_deadline,
                argv,
                environment,
                deadline_monotonic=deadline_monotonic,
                monotonic=monotonic,
            )
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("pg_restore could not be executed") from None


def _run_process_with_deadline(
    argv: tuple[str, ...],
    environment: dict[str, str],
    *,
    deadline_monotonic: float,
    monotonic: Callable[[], float],
    cleanup_grace_seconds: float = _PROCESS_CLEANUP_GRACE_SECONDS,
) -> int:
    remaining = deadline_monotonic - monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise subprocess.TimeoutExpired(argv, max(0.0, remaining))
    if (
        isinstance(cleanup_grace_seconds, bool)
        or not isinstance(cleanup_grace_seconds, (int, float))
        or not math.isfinite(cleanup_grace_seconds)
        or cleanup_grace_seconds <= 0
    ):
        raise ValueError("pg_restore process cleanup grace is invalid")
    cleanup_grace_seconds = float(cleanup_grace_seconds)
    options: dict[str, object] = {
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "shell": False,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        )
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(argv, **options)
    try:
        return int(process.wait(timeout=remaining))
    except BaseException as primary_failure:
        cleanup_failures: list[str] = []
        try:
            cleanup_deadline = monotonic() + cleanup_grace_seconds
        except BaseException:
            cleanup_deadline = None
            cleanup_failures.append("cleanup clock")
        try:
            _terminate_process_tree(process)
        except BaseException:
            cleanup_failures.append("process tree termination")
            try:
                process.kill()
            except BaseException:
                cleanup_failures.append("process fallback termination")
        try:
            cleanup_remaining = (
                max(0.0, cleanup_deadline - monotonic()) if cleanup_deadline is not None else 0.0
            )
        except BaseException:
            cleanup_remaining = 0.0
            cleanup_failures.append("cleanup clock")
        if cleanup_remaining > 0:
            try:
                process.wait(timeout=cleanup_remaining)
            except BaseException:
                cleanup_failures.append("process reap")
        else:
            cleanup_failures.append("process reap deadline")
        if cleanup_failures:
            primary_failure.add_note(
                "pg_restore cleanup incomplete: " + ", ".join(cleanup_failures)
            )
        raise


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        return
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        raise OSError("Windows system root is unavailable")
    taskkill = Path(system_root) / "System32" / "taskkill.exe"
    result = subprocess.run(
        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        timeout=10,
        env={"SystemRoot": system_root, "WINDIR": system_root},
    )
    if result.returncode != 0 and process.poll() is None:
        raise subprocess.SubprocessError("taskkill did not terminate the process tree")


@asynccontextmanager
async def _default_target_exclusion(engine: Any, identity_sha256: str):
    from sqlalchemy import text

    if not isinstance(identity_sha256, str) or _SHA256.fullmatch(identity_sha256) is None:
        raise ValueError("restore target concurrency exclusion identity is invalid")
    lock_key = int.from_bytes(bytes.fromhex(identity_sha256)[:8], "big", signed=True)
    connection_context = engine.connect()
    connection = await connection_context.__aenter__()
    try:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
        ).scalar_one()
        if acquired is not True:
            raise RuntimeError("restore target concurrency exclusion is already held")
        try:
            yield
        finally:
            primary_failure = sys.exception()

            async def release_exclusion() -> None:
                released = (
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": lock_key},
                    )
                ).scalar_one()
                if released is not True:
                    raise RuntimeError("restore target concurrency exclusion release failed")

            try:
                await _await_owned_cleanup(release_exclusion())
            except asyncio.CancelledError:
                if primary_failure is None:
                    raise
                primary_failure.add_note(
                    "restore target concurrency exclusion release was repeatedly cancelled"
                )
            except BaseException as error:
                if primary_failure is not None:
                    primary_failure.add_note("restore target concurrency exclusion release failed")
                else:
                    raise RuntimeError(
                        "restore target concurrency exclusion release failed"
                    ) from error
    finally:
        primary_failure = sys.exception()
        error_info = (
            (type(primary_failure), primary_failure, primary_failure.__traceback__)
            if primary_failure is not None
            else (None, None, None)
        )
        try:
            await _await_owned_operation(connection_context.__aexit__(*error_info))
        except asyncio.CancelledError:
            if primary_failure is None:
                raise
            primary_failure.add_note(
                "restore target concurrency exclusion connection exit was repeatedly cancelled"
            )
        except BaseException as error:
            if primary_failure is not None:
                primary_failure.add_note(
                    "restore target concurrency exclusion connection exit failed"
                )
            else:
                raise RuntimeError(
                    "restore target concurrency exclusion connection exit failed"
                ) from error


def _default_runtime(
    *,
    deadline_monotonic: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> RestoreOperatorRuntime:
    async def process_runner(argv: tuple[str, ...], environment: dict[str, str]) -> int:
        return await _default_process_runner(
            argv,
            environment,
            deadline_monotonic=deadline_monotonic,
            monotonic=monotonic,
        )

    return RestoreOperatorRuntime(
        target_loader=load_restore_target,
        engine_factory=_default_engine_factory,
        object_client_factory=_default_object_client_factory,
        database_probe=_default_measured_database_probe,
        facts_inspector=_default_facts_inspector,
        process_runner=process_runner,
        receipt_rebinder=_default_rebind_database_object_receipts,
        app_access_granter=_default_grant_app_access,
        app_engine_factory=_default_engine_factory,
        app_access_probe=_default_app_access_probe,
        target_exclusion=_default_target_exclusion,
        target_exclusion_mode="postgresql-session-advisory-lock",
        object_state_probe=_default_object_state_probe,
    )


def _object_prefix(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run ID must contain only safe path characters")
    return ""


def _report_target(path: Path) -> Path:
    requested = Path(path)
    if requested.name in {"", ".", ".."}:
        raise ValueError("restore report path is invalid")
    if requested.exists() or requested.is_symlink():
        raise FileExistsError("restore report already exists")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError:
        raise ValueError("restore report parent is unavailable") from None
    if requested.parent.is_symlink() or not parent.is_dir():
        raise ValueError("restore report parent is unsafe")
    return parent / requested.name


def _validate_target_config_snapshot(path: Path, expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        return
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("restore target config digest is invalid")
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("restore target config snapshot is unavailable")
    try:
        body = source.read_bytes()
        payload = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("restore target config snapshot is invalid") from None
    if (
        not isinstance(payload, dict)
        or _canonical_json(payload) != body
        or hashlib.sha256(body).hexdigest() != expected_sha256
    ):
        raise ValueError("restore target config snapshot is invalid")


def _load_target_provisioning_receipt(
    path: Path,
    expected_sha256: str,
    *,
    candidate_sha256: str,
    run_id: str,
    environment_id: str,
    database_disposition: str,
    object_store_disposition: str,
) -> bytes:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("restore target provisioning receipt digest is invalid")
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("restore target provisioning receipt is unavailable")
    try:
        body = source.read_bytes()
    except OSError:
        raise ValueError("restore target provisioning receipt is unavailable") from None
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise ValueError("restore target provisioning receipt digest is invalid")
    from backup_restore_contract import parse_target_provisioning_receipt

    parse_target_provisioning_receipt(
        body,
        provisioning_receipt_sha256=expected_sha256,
        candidate_sha256=candidate_sha256,
        release_run={"runId": run_id, "environmentId": environment_id},
        database_disposition=database_disposition,
        object_store_disposition=object_store_disposition,
    )
    return body


def _pg_restore_argv(
    executable: Path,
    *,
    target: RestoreTarget,
    database_dump: Path,
) -> tuple[str, ...]:
    program = str(executable)
    if not program or "\x00" in program:
        raise ValueError("pg_restore executable is invalid")
    return (
        program,
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
        "--no-password",
        "--host",
        target.database_host,
        "--port",
        str(target.database_port),
        "--username",
        _RESTORE_DATABASE_USER,
        "--dbname",
        target.database_name,
        str(database_dump),
    )


def _pg_environment(password: str) -> dict[str, str]:
    allowed = (
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["PGPASSWORD"] = password
    return environment


async def _client_call(client: Any, method: str, **arguments: object) -> Any:
    operation = getattr(client, method)
    return await _await_owned_operation(
        asyncio.to_thread(operation, **arguments),
        cleanup_grace_seconds=_OBJECT_CLIENT_CLEANUP_GRACE_SECONDS,
        incomplete_cleanup_note="object client operation cleanup incomplete",
    )


async def _await_owned_operation(
    operation: Awaitable[_OwnedResult],
    *,
    cleanup_grace_seconds: float | None = None,
    incomplete_cleanup_note: str | None = None,
) -> _OwnedResult:
    if cleanup_grace_seconds is not None:
        if (
            isinstance(cleanup_grace_seconds, bool)
            or not isinstance(cleanup_grace_seconds, (int, float))
            or not math.isfinite(cleanup_grace_seconds)
            or cleanup_grace_seconds <= 0
            or not isinstance(incomplete_cleanup_note, str)
            or not incomplete_cleanup_note
        ):
            raise ValueError("owned operation cleanup grace is invalid")
        cleanup_grace_seconds = float(cleanup_grace_seconds)
    elif incomplete_cleanup_note is not None:
        raise ValueError("owned operation cleanup grace is invalid")

    async def run_operation() -> _OwnedResult:
        return await operation

    operation_task = asyncio.create_task(run_operation())
    first_cancellation: asyncio.CancelledError | None = None
    cleanup_deadline: float | None = None
    cleanup_incomplete_noted = False
    loop = asyncio.get_running_loop()
    while not operation_task.done():
        try:
            if cleanup_deadline is not None and not cleanup_incomplete_noted:
                remaining = cleanup_deadline - loop.time()
                if remaining <= 0:
                    if first_cancellation is None or incomplete_cleanup_note is None:
                        raise RuntimeError("owned operation cleanup state is invalid")
                    first_cancellation.add_note(incomplete_cleanup_note)
                    cleanup_incomplete_noted = True
                    continue
                await asyncio.wait((operation_task,), timeout=remaining)
                if not operation_task.done():
                    if first_cancellation is None or incomplete_cleanup_note is None:
                        raise RuntimeError("owned operation cleanup state is invalid")
                    first_cancellation.add_note(incomplete_cleanup_note)
                    cleanup_incomplete_noted = True
            else:
                await asyncio.shield(operation_task)
        except asyncio.CancelledError as cancellation:
            if first_cancellation is None:
                first_cancellation = cancellation
                if cleanup_grace_seconds is not None:
                    cleanup_deadline = loop.time() + cleanup_grace_seconds
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
    try:
        result = operation_task.result()
    except BaseException as operation_failure:
        if first_cancellation is not None:
            first_cancellation.add_note(
                f"owned operation failed: {type(operation_failure).__name__}"
            )
            raise first_cancellation
        raise
    if first_cancellation is not None:
        raise first_cancellation
    return result


async def _await_owned_cleanup(cleanup: Awaitable[None]) -> None:
    await _await_owned_operation(cleanup)


def _digest_body(body: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := body.read(1024 * 1024):
        if not isinstance(chunk, bytes):
            raise ValueError("object body is invalid")
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _read_bounded_body(body: Any, maximum_size: int) -> bytes:
    payload = bytearray()
    while len(payload) <= maximum_size:
        remaining = maximum_size + 1 - len(payload)
        chunk = body.read(min(1024 * 1024, remaining))
        if not isinstance(chunk, bytes):
            raise ValueError("object body is invalid")
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)


async def _close_resource(resource: object) -> None:
    close = getattr(resource, "close", None)
    if close is not None:
        await _await_owned_cleanup(asyncio.to_thread(close))


async def _list_object_versions(
    client: Any,
    *,
    bucket: str,
    prefix: str,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    arguments: dict[str, object] = {
        "Bucket": bucket,
        "Prefix": prefix,
        "MaxKeys": 1000,
    }
    seen_markers: set[tuple[str, str | None]] = set()
    versions: list[object] = []
    delete_markers: list[object] = []
    while True:
        try:
            response = await _client_call(
                client,
                "list_object_versions",
                **arguments,
            )
            page_versions = response.get("Versions", [])
            page_delete_markers = response.get("DeleteMarkers", [])
            if not isinstance(page_versions, list) or not isinstance(
                page_delete_markers,
                list,
            ):
                raise ValueError
            versions.extend(page_versions)
            delete_markers.extend(page_delete_markers)
            truncated = response.get("IsTruncated")
            if not isinstance(truncated, bool):
                raise ValueError
            if not truncated:
                return tuple(versions), tuple(delete_markers)
            key_marker = response.get("NextKeyMarker")
            version_marker = response.get("NextVersionIdMarker")
            if not isinstance(key_marker, str) or not key_marker:
                raise ValueError
            if version_marker is not None and not isinstance(version_marker, str):
                raise ValueError
            marker = (key_marker, version_marker)
            if marker in seen_markers:
                raise ValueError
            seen_markers.add(marker)
            arguments["KeyMarker"] = key_marker
            if version_marker:
                arguments["VersionIdMarker"] = version_marker
            else:
                arguments.pop("VersionIdMarker", None)
        except Exception:
            raise RuntimeError("restore target object prefix could not be inspected") from None


def _separate_object_restore_control(
    versions: tuple[object, ...],
    delete_markers: tuple[object, ...],
) -> tuple[tuple[object, ...], tuple[object, ...], tuple[dict[str, object], ...]]:
    business_versions: list[object] = []
    control_versions: list[dict[str, object]] = []
    for version in versions:
        if isinstance(version, dict) and version.get("Key") == _OBJECT_RESTORE_CONTROL_KEY:
            if not isinstance(version.get("VersionId"), str) or not version["VersionId"]:
                raise RuntimeError("restore target object control claim is invalid")
            control_versions.append(version)
        else:
            business_versions.append(version)
    business_delete_markers: list[object] = []
    for marker in delete_markers:
        if isinstance(marker, dict) and marker.get("Key") == _OBJECT_RESTORE_CONTROL_KEY:
            raise RuntimeError("restore target object control claim was deleted")
        business_delete_markers.append(marker)
    return (
        tuple(business_versions),
        tuple(business_delete_markers),
        tuple(control_versions),
    )


async def _object_prefix_is_empty(
    client: Any,
    *,
    bucket: str,
    prefix: str,
) -> bool:
    versions, delete_markers = await _list_object_versions(
        client,
        bucket=bucket,
        prefix=prefix,
    )
    versions, delete_markers, _control_versions = _separate_object_restore_control(
        versions,
        delete_markers,
    )
    return not versions and not delete_markers


async def _object_bucket_versioning_is_enabled(client: Any, *, bucket: str) -> bool:
    try:
        response = await _client_call(
            client,
            "get_bucket_versioning",
            Bucket=bucket,
        )
    except Exception:
        raise RuntimeError(
            "restore target object bucket versioning could not be inspected"
        ) from None
    return isinstance(response, dict) and response.get("Status") == "Enabled"


async def _claim_object_restore_control(
    client: Any,
    *,
    bucket: str,
    candidate_sha256: str,
    provisioning_receipt_sha256: str,
    run_id: str,
    environment_id: str,
    database_identity_sha256: str,
    object_store_identity_sha256: str,
) -> _ObjectRestoreControlClaim:
    if (
        _SHA256.fullmatch(candidate_sha256) is None
        or _SHA256.fullmatch(provisioning_receipt_sha256) is None
        or _RUN_ID.fullmatch(run_id) is None
        or _ENVIRONMENT_ID.fullmatch(environment_id) is None
        or _SHA256.fullmatch(database_identity_sha256) is None
        or _SHA256.fullmatch(object_store_identity_sha256) is None
    ):
        raise ValueError("restore target object control claim identity is invalid")
    marker = {
        "schemaVersion": 1,
        "candidateSha256": candidate_sha256,
        "provisioningReceiptSha256": provisioning_receipt_sha256,
        "releaseRun": {
            "runId": run_id,
            "environmentId": environment_id,
        },
        "target": {
            "databaseIdentitySha256": database_identity_sha256,
            "objectStoreIdentitySha256": object_store_identity_sha256,
        },
    }
    marker_body = _canonical_json(marker)
    try:
        service_model = client.meta.service_model
        operation_model = service_model.operation_model("PutObject")
        input_members = operation_model.input_shape.members
    except Exception:
        raise RuntimeError(
            "restore target object storage does not support atomic conditional create"
        ) from None
    if not isinstance(input_members, Mapping) or "IfNoneMatch" not in input_members:
        raise RuntimeError(
            "restore target object storage does not support atomic conditional create"
        )
    try:
        response = await _client_call(
            client,
            "put_object",
            Bucket=bucket,
            Key=_OBJECT_RESTORE_CONTROL_KEY,
            Body=marker_body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except Exception as error:
        response = getattr(error, "response", None)
        error_details = response.get("Error") if isinstance(response, Mapping) else None
        response_metadata = (
            response.get("ResponseMetadata") if isinstance(response, Mapping) else None
        )
        if (
            isinstance(error_details, Mapping) and error_details.get("Code") == "PreconditionFailed"
        ) or (
            isinstance(response_metadata, Mapping)
            and response_metadata.get("HTTPStatusCode") == 412
        ):
            raise RuntimeError("restore target object control key is already claimed") from None
        raise RuntimeError("restore target object control claim failed") from None
    if not isinstance(response, dict):
        raise RuntimeError("restore target object control claim returned no receipt")
    etag = response.get("ETag")
    version_id = response.get("VersionId")
    if (
        not isinstance(etag, str)
        or not etag
        or not etag.strip()
        or etag != etag.strip()
        or len(etag) > 1024
        or not isinstance(version_id, str)
        or not version_id
        or not version_id.strip()
        or version_id != version_id.strip()
        or version_id == "null"
        or len(version_id) > 1024
    ):
        raise RuntimeError("restore target object control claim returned no receipt")
    return _ObjectRestoreControlClaim(
        version_id=version_id,
        body=marker_body,
        body_sha256=hashlib.sha256(marker_body).hexdigest(),
    )


async def _default_object_state_probe(
    client: Any,
    target: RestoreTarget,
) -> TargetObjectState:
    from backup_restore_contract import physical_object_store_identity_sha256

    versioning_enabled = await _object_bucket_versioning_is_enabled(
        client,
        bucket=target.object_bucket,
    )
    versions, delete_markers = await _list_object_versions(
        client,
        bucket=target.object_bucket,
        prefix="",
    )
    versions, delete_markers, _control_versions = _separate_object_restore_control(
        versions,
        delete_markers,
    )
    try:
        owner_response = await _client_call(
            client,
            "get_bucket_acl",
            Bucket=target.object_bucket,
        )
        owner = owner_response["Owner"]
        owner_id = owner["ID"]
    except Exception:
        raise RuntimeError("restore target object ownership could not be inspected") from None
    if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 1024:
        raise RuntimeError("restore target object ownership could not be inspected")
    keys: set[str] = set()
    for version in versions:
        if (
            not isinstance(version, dict)
            or not isinstance(version.get("Key"), str)
            or not isinstance(version.get("VersionId"), str)
            or not version["VersionId"]
        ):
            raise RuntimeError("restore target object versions could not be inspected")
        keys.add(version["Key"])
    if any(
        not isinstance(marker, dict)
        or not isinstance(marker.get("Key"), str)
        or not isinstance(marker.get("VersionId"), str)
        or not marker["VersionId"]
        for marker in delete_markers
    ):
        raise RuntimeError("restore target object versions could not be inspected")
    owner_id_sha256 = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    return TargetObjectState(
        identity_sha256=physical_object_store_identity_sha256(
            target.object_endpoint,
            target.object_region,
            target.object_bucket,
            owner_id_sha256,
        ),
        versioning_enabled=versioning_enabled,
        object_count=len(keys),
        version_count=len(versions),
        delete_marker_count=len(delete_markers),
        owner_id_sha256=owner_id_sha256,
    )


async def _restored_object_prefix_is_exact(
    client: Any,
    *,
    bucket: str,
    prefix: str,
    expected_receipts: tuple[RestoredObjectReceipt, ...],
    required_control_claim: _ObjectRestoreControlClaim | None = None,
    required_control_version_id: str | None = None,
) -> bool:
    versions, delete_markers = await _list_object_versions(
        client,
        bucket=bucket,
        prefix=prefix,
    )
    versions, delete_markers, control_versions = _separate_object_restore_control(
        versions,
        delete_markers,
    )
    if required_control_claim is not None and required_control_version_id is not None:
        return False
    if required_control_version_id is not None:
        return False
    if required_control_claim is not None:
        if (
            not isinstance(required_control_claim, _ObjectRestoreControlClaim)
            or not isinstance(required_control_claim.body, bytes)
            or not required_control_claim.body
            or _SHA256.fullmatch(required_control_claim.body_sha256) is None
            or hashlib.sha256(required_control_claim.body).hexdigest()
            != required_control_claim.body_sha256
        ):
            return False
        try:
            marker = json.loads(required_control_claim.body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(marker, dict) or _canonical_json(marker) != required_control_claim.body:
            return False
        if len(control_versions) != 1:
            return False
        control_version = control_versions[0]
        if (
            control_version.get("VersionId") != required_control_claim.version_id
            or control_version.get("IsLatest") is not True
        ):
            return False
        control_body: object | None = None
        try:
            try:
                response = await _client_call(
                    client,
                    "get_object",
                    Bucket=bucket,
                    Key=_OBJECT_RESTORE_CONTROL_KEY,
                    VersionId=required_control_claim.version_id,
                )
            except Exception:
                raise RuntimeError("restore target object control readback failed") from None
            if not isinstance(response, Mapping):
                return False
            control_body = response.get("Body")
            if control_body is None:
                return False
            try:
                stored_body = await _await_owned_operation(
                    asyncio.to_thread(
                        _read_bounded_body,
                        control_body,
                        len(required_control_claim.body),
                    ),
                    cleanup_grace_seconds=_OBJECT_CLIENT_CLEANUP_GRACE_SECONDS,
                    incomplete_cleanup_note="object client operation cleanup incomplete",
                )
            except Exception:
                raise RuntimeError("restore target object control readback failed") from None
            if (
                stored_body != required_control_claim.body
                or hashlib.sha256(stored_body).hexdigest() != required_control_claim.body_sha256
            ):
                return False
        finally:
            if control_body is not None:
                primary_failure = sys.exception()
                try:
                    await _close_resource(control_body)
                except BaseException as close_failure:
                    if primary_failure is not None:
                        primary_failure.add_note(
                            "restore target object control readback cleanup failed"
                        )
                    else:
                        raise RuntimeError(
                            "restore target object control readback cleanup failed"
                        ) from close_failure
    expected = {receipt.key: receipt.version_id for receipt in expected_receipts}
    if delete_markers or len(expected) != len(expected_receipts) or len(versions) != len(expected):
        return False
    actual: set[str] = set()
    for version in versions:
        if not isinstance(version, dict):
            return False
        key = version.get("Key")
        version_id = version.get("VersionId")
        if (
            not isinstance(key, str)
            or key not in expected
            or key in actual
            or version.get("IsLatest") is not True
            or not isinstance(version_id, str)
            or not version_id
            or version_id != expected[key]
        ):
            return False
        actual.add(key)
    return actual == set(expected)


async def _restore_inventory_objects(
    client: Any,
    *,
    bucket: str,
    prefix: str,
    inventory: tuple[ObjectInventoryEntry, ...],
    payloads: tuple[Path, ...],
) -> tuple[RestoredObjectReceipt, ...]:
    if prefix != "":
        raise ValueError("restore object prefix must be empty")
    receipts: list[RestoredObjectReceipt] = []
    for entry, payload in zip(inventory, payloads, strict=True):
        destination_key = entry.key
        content_type = getattr(entry, "content_type", None)
        owner_token = getattr(entry, "owner_token", None)
        if not isinstance(content_type, str) or not isinstance(owner_token, str):
            raise ValueError("restore object metadata is incomplete")
        try:
            with Path(payload).open("rb") as source_body:
                put_response = await _client_call(
                    client,
                    "put_object",
                    Bucket=bucket,
                    Key=destination_key,
                    Body=source_body,
                    ContentLength=entry.size,
                    ContentType=content_type,
                    Metadata={"owner": owner_token, "sha256": entry.sha256},
                    IfNoneMatch="*",
                )
        except Exception:
            raise RuntimeError("restore object create-only write failed") from None
        if not isinstance(put_response, dict):
            raise RuntimeError("restore object create-only write returned no receipt")
        revision = put_response.get("ETag")
        version_id = put_response.get("VersionId")
        try:
            receipt = RestoredObjectReceipt(
                tenant_id=entry.tenant_id,
                key=destination_key,
                sha256=entry.sha256,
                size=entry.size,
                content_type=content_type,
                owner_token=owner_token,
                revision=revision,
                version_id=version_id,
            )
        except (TypeError, ValueError):
            raise RuntimeError("restore object create-only write returned no receipt") from None

        restored_body: object | None = None
        try:
            head = await _client_call(
                client,
                "head_object",
                Bucket=bucket,
                Key=destination_key,
                VersionId=receipt.version_id,
            )
            metadata = head.get("Metadata") if isinstance(head, dict) else None
            if not isinstance(metadata, dict):
                raise ValueError
            normalized_metadata = {str(name).lower(): value for name, value in metadata.items()}
            if (
                head.get("ContentLength") != receipt.size
                or head.get("ContentType") != receipt.content_type
                or head.get("ETag") != receipt.revision
                or head.get("VersionId") != receipt.version_id
                or normalized_metadata.get("owner") != receipt.owner_token
                or normalized_metadata.get("sha256") != receipt.sha256
            ):
                raise ValueError
            restored_response = await _client_call(
                client,
                "get_object",
                Bucket=bucket,
                Key=destination_key,
                VersionId=receipt.version_id,
            )
            restored_body = restored_response["Body"]
            restored_sha256, restored_size = await _await_owned_operation(
                asyncio.to_thread(
                    _digest_body,
                    restored_body,
                ),
                cleanup_grace_seconds=_OBJECT_CLIENT_CLEANUP_GRACE_SECONDS,
                incomplete_cleanup_note="object client operation cleanup incomplete",
            )
        except Exception:
            raise RuntimeError("restored object could not be verified") from None
        finally:
            if restored_body is not None:
                await _close_resource(restored_body)
        if restored_sha256 != entry.sha256 or restored_size != entry.size:
            raise ValueError("restored object checksum does not match backup inventory")
        receipts.append(receipt)
    return tuple(receipts)


async def _restore_database_dump(
    runtime: RestoreOperatorRuntime,
    *,
    target: RestoreTarget,
    database_dump: Path,
    pg_restore: Path,
) -> None:
    argv = _pg_restore_argv(
        pg_restore,
        target=target,
        database_dump=database_dump,
    )
    try:
        returncode = await runtime.process_runner(argv, _pg_environment(target.database_password))
    except Exception:
        raise RuntimeError("pg_restore failed") from None
    if isinstance(returncode, bool) or returncode != 0:
        raise RuntimeError("pg_restore failed")


def _validate_measured_target_transition(
    before: TargetObservation,
    after: TargetObservation,
    restored_count: object,
    *,
    target: RestoreTarget,
    target_config_sha256: str,
    provisioning_receipt_sha256: str,
    database_ownership: str,
    object_namespace_ownership: str,
    exclusion_mode: str,
    exclusion_identity_sha256: str,
) -> dict[str, object]:
    if isinstance(restored_count, bool) or not isinstance(restored_count, int):
        raise RuntimeError("restore target mutation result is invalid")
    if restored_count < 0:
        raise RuntimeError("restore target mutation result is invalid")
    database_before = before.database
    database_after = after.database
    objects_before = before.objects
    objects_after = after.objects
    if (
        database_before.identity_sha256 != database_after.identity_sha256
        or database_before.user_object_count != 0
        or database_after.user_object_count <= 0
        or database_before.current_role != target.database_user
        or database_after.current_role != target.database_user
        or database_before.database_owner != target.database_user
        or database_after.database_owner != target.database_user
    ):
        raise RuntimeError("restore target database observations are invalid")
    if (
        objects_before.identity_sha256 != objects_after.identity_sha256
        or objects_before.versioning_enabled is not True
        or objects_after.versioning_enabled is not True
        or objects_before.object_count != 0
        or objects_before.version_count != 0
        or objects_before.delete_marker_count != 0
        or objects_after.object_count != restored_count
        or objects_after.version_count != restored_count
        or objects_after.delete_marker_count != 0
        or not isinstance(objects_before.owner_id_sha256, str)
        or _SHA256.fullmatch(objects_before.owner_id_sha256) is None
        or objects_before.owner_id_sha256 != objects_after.owner_id_sha256
    ):
        raise RuntimeError("restore target object observations are invalid")
    if (
        database_ownership not in _OWNERSHIP_VALUES
        or object_namespace_ownership not in _OWNERSHIP_VALUES
        or not isinstance(exclusion_mode, str)
        or not exclusion_mode
        or _SHA256.fullmatch(exclusion_identity_sha256) is None
    ):
        raise RuntimeError("restore target ownership or exclusion evidence is invalid")
    return {
        "targetConfigSha256": target_config_sha256,
        "provisioningReceiptSha256": provisioning_receipt_sha256,
        "database": {
            "host": target.database_host,
            "port": target.database_port,
            "name": target.database_name,
            "identitySha256": database_before.identity_sha256,
            "ownership": database_ownership,
            "pre": {
                "identitySha256": database_before.identity_sha256,
                "userObjectCount": database_before.user_object_count,
                "currentRole": database_before.current_role,
                "owner": database_before.database_owner,
            },
            "post": {
                "identitySha256": database_after.identity_sha256,
                "userObjectCount": database_after.user_object_count,
                "currentRole": database_after.current_role,
                "owner": database_after.database_owner,
            },
        },
        "objects": {
            "endpoint": target.object_endpoint,
            "region": target.object_region,
            "namespaceId": target.object_namespace_id,
            "bucket": target.object_bucket,
            "identitySha256": objects_before.identity_sha256,
            "ownership": object_namespace_ownership,
            "pre": {
                "identitySha256": objects_before.identity_sha256,
                "versioningEnabled": objects_before.versioning_enabled,
                "objectCount": objects_before.object_count,
                "versionCount": objects_before.version_count,
                "deleteMarkerCount": objects_before.delete_marker_count,
                "ownerIdSha256": objects_before.owner_id_sha256,
            },
            "post": {
                "identitySha256": objects_after.identity_sha256,
                "versioningEnabled": objects_after.versioning_enabled,
                "objectCount": objects_after.object_count,
                "versionCount": objects_after.version_count,
                "deleteMarkerCount": objects_after.delete_marker_count,
                "ownerIdSha256": objects_after.owner_id_sha256,
            },
        },
        "concurrencyExclusion": {
            "mode": exclusion_mode,
            "identitySha256": exclusion_identity_sha256,
            "heldThroughPostValidation": True,
        },
    }


def _validate_measured_target_precondition(
    observation: TargetObservation,
    *,
    target: RestoreTarget,
    source_database_identity_sha256: str,
    source_object_identity_sha256: str,
) -> None:
    database = observation.database
    objects = observation.objects
    if (
        _SHA256.fullmatch(str(database.identity_sha256)) is None
        or database.identity_sha256 == source_database_identity_sha256
        or database.user_object_count != 0
        or database.current_role != target.database_user
        or database.database_owner != target.database_user
    ):
        raise ValueError("restore target database pre-observation is invalid")
    if (
        _SHA256.fullmatch(str(objects.identity_sha256)) is None
        or objects.identity_sha256 == source_object_identity_sha256
        or objects.versioning_enabled is not True
        or objects.object_count != 0
        or objects.version_count != 0
        or objects.delete_marker_count != 0
        or _SHA256.fullmatch(str(objects.owner_id_sha256)) is None
    ):
        raise ValueError("restore target object pre-observation is invalid")


def restore_report_payload(
    report: RestoreValidationReport,
    *,
    run_id: str,
    restored_object_count: int,
    archive_fingerprint_sha256: str,
    manifest_sha256: str,
    target_object_bucket: str,
) -> dict[str, object]:
    if _SHA256.fullmatch(archive_fingerprint_sha256) is None:
        raise ValueError("archive fingerprint is invalid")
    if _SHA256.fullmatch(manifest_sha256) is None:
        raise ValueError("manifest digest is invalid")
    if not isinstance(target_object_bucket, str) or not target_object_bucket:
        raise ValueError("target object bucket is invalid")
    payload = {
        "schemaVersion": 3 if report.target_evidence is not None else 2,
        "runId": run_id,
        "ok": report.ok,
        "targetDatabaseIdentitySha256": report.target_database_identity_sha256,
        "objectPrefix": report.object_prefix,
        "validated": list(report.validated),
        "failures": list(report.failures),
        "sourceArchive": {
            "archiveFingerprintSha256": archive_fingerprint_sha256,
            "manifestSha256": manifest_sha256,
        },
        "database": {
            "dumpRestoreSingleTransaction": True,
            "postRestoreMutationsAtomic": False,
        },
        "objects": {
            "createOnly": True,
            "isolation": "empty_target_bucket",
            "readbackVerified": True,
            "restoredCount": restored_object_count,
            "targetBucket": target_object_bucket,
        },
        "crossSystemAtomic": False,
    }
    if report.target_evidence is not None:
        payload["target"] = report.target_evidence
    return payload


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_restore_report(
    path: Path,
    report: RestoreValidationReport,
    *,
    run_id: str,
    restored_object_count: int,
    archive_fingerprint_sha256: str,
    manifest_sha256: str,
    target_object_bucket: str,
) -> None:
    from deeptutor.teaching.secret_permissions import restrict_secret_file

    report_bytes = _canonical_json(
        restore_report_payload(
            report,
            run_id=run_id,
            restored_object_count=restored_object_count,
            archive_fingerprint_sha256=archive_fingerprint_sha256,
            manifest_sha256=manifest_sha256,
            target_object_bucket=target_object_bucket,
        )
    )
    try:
        descriptor, staging_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
    except OSError:
        raise RuntimeError("restore report staging could not be created") from None
    staging = Path(staging_name)
    descriptor_open = True
    committed = False
    try:
        handle = os.fdopen(descriptor, "wb")
        descriptor_open = False
        with handle:
            restrict_secret_file(staging)
            handle.write(report_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staging, path)
        except FileExistsError:
            raise FileExistsError("restore report already exists") from None
        committed = True
        # The no-replace hard link is the publication commit point. Once the
        # formal report exists, later durability/cleanup attempts cannot turn
        # the operator result into a contradictory failure.
        try:
            _fsync_directory(path.parent)
        except BaseException:
            pass
    except FileExistsError:
        raise
    except OSError:
        raise RuntimeError("restore report could not be published") from None
    finally:
        primary_failure = sys.exception()
        if descriptor_open:
            try:
                os.close(descriptor)
            except BaseException:
                if primary_failure is not None and not committed:
                    primary_failure.add_note("restore report staging descriptor cleanup failed")
                elif not committed:
                    raise
        try:
            staging.unlink(missing_ok=True)
        except BaseException:
            if primary_failure is not None and not committed:
                primary_failure.add_note("restore report staging cleanup failed")
            elif not committed:
                raise


async def run_restore_operator(
    *,
    backup_dir: Path,
    target_config: Path,
    provisioning_receipt: Path | None = None,
    provisioning_receipt_sha256: str | None = None,
    target_secret_dir: Path,
    run_id: str,
    report_path: Path,
    pg_restore: Path = Path("pg_restore"),
    target_config_sha256: str | None = None,
    database_ownership: str | None = None,
    object_namespace_ownership: str | None = None,
    candidate_sha256: str | None = None,
    environment_id: str | None = None,
    deadline_monotonic: float | None = None,
    runtime: RestoreOperatorRuntime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> RestoreValidationReport:
    """Restore into one empty target DB and one empty versioned target bucket.

    The pg_restore dump phase uses one transaction. Receipt rebinding, grants,
    and object writes are post-restore mutations and are not atomic with that
    dump transaction or with one another across systems.
    """

    current_monotonic = monotonic()
    if deadline_monotonic is None:
        deadline_monotonic = current_monotonic + 60 * 60
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(deadline_monotonic)
        or deadline_monotonic <= current_monotonic
    ):
        raise ValueError("restore deadline is invalid or expired")
    deadline_monotonic = float(deadline_monotonic)
    prefix = _object_prefix(run_id)
    backup = _load_verified_backup(Path(backup_dir))
    if len(backup.object_payloads) != len(backup.object_inventory) or any(
        not hasattr(entry, "version_id") or not hasattr(entry, "payload_file")
        for entry in backup.object_inventory
    ):
        raise ValueError("verified backup does not contain every object payload")
    report_target = _report_target(Path(report_path))
    _validate_target_config_snapshot(Path(target_config), target_config_sha256)
    selected_runtime = runtime or _default_runtime(
        deadline_monotonic=deadline_monotonic,
        monotonic=monotonic,
    )
    measured_values = (
        target_config_sha256,
        provisioning_receipt,
        provisioning_receipt_sha256,
        database_ownership,
        object_namespace_ownership,
        candidate_sha256,
        environment_id,
        selected_runtime.target_exclusion,
        selected_runtime.target_exclusion_mode,
        selected_runtime.object_state_probe,
    )
    measured_execution = all(value is not None for value in measured_values)
    if not measured_execution:
        raise ValueError("restore measured target evidence is required")
    try:
        target = selected_runtime.target_loader(
            Path(target_config),
            Path(target_secret_dir),
        )
    except Exception:
        raise ValueError("restore target configuration is invalid") from None
    if target.database_user != _RESTORE_DATABASE_USER:
        raise ValueError("restore target database role must be yfeistai_migrator")
    provisioning_receipt_body: bytes | None = None
    if measured_execution:
        if (
            provisioning_receipt is None
            or provisioning_receipt_sha256 is None
            or candidate_sha256 is None
            or environment_id is None
            or database_ownership is None
            or object_namespace_ownership is None
        ):
            raise RuntimeError("restore target provisioning receipt is unavailable")
        provisioning_receipt_body = _load_target_provisioning_receipt(
            provisioning_receipt,
            provisioning_receipt_sha256,
            candidate_sha256=candidate_sha256,
            run_id=run_id,
            environment_id=environment_id,
            database_disposition=database_ownership,
            object_store_disposition=object_namespace_ownership,
        )

    try:
        engine = selected_runtime.engine_factory(target.database_url)
    except Exception:
        raise RuntimeError("restore target database is unavailable") from None
    object_client: Any | None = None
    app_engine: Any | None = None
    restored_receipts: tuple[RestoredObjectReceipt, ...] | None = None
    object_control_claim: _ObjectRestoreControlClaim | None = None
    report: RestoreValidationReport | None = None
    try:
        try:
            object_client = selected_runtime.object_client_factory(target)
        except Exception:
            raise RuntimeError("restore target object storage is unavailable") from None

        async def restore_database() -> None:
            current_backup = _reverify_verified_backup(backup)
            await _restore_database_dump(
                selected_runtime,
                target=target,
                database_dump=current_backup.database_dump,
                pg_restore=Path(pg_restore),
            )

        async def restore_objects(
            object_prefix: str,
            object_inventory: tuple[ObjectInventoryEntry, ...],
        ) -> tuple[RestoredObjectReceipt, ...]:
            nonlocal restored_receipts
            if object_client is None:
                raise RuntimeError("restore target object storage is unavailable")
            current_backup = _reverify_verified_backup(backup)
            restored_receipts = await _restore_inventory_objects(
                object_client,
                bucket=target.object_bucket,
                prefix=object_prefix,
                inventory=current_backup.object_inventory,
                payloads=current_backup.object_payloads,
            )
            return restored_receipts

        async def inspect_restored_facts() -> RestoredTeachingFacts:
            nonlocal app_engine
            if restored_receipts is None:
                raise RuntimeError("restored object receipts are unavailable")
            try:
                await selected_runtime.receipt_rebinder(engine, restored_receipts)
            except Exception:
                raise RuntimeError(
                    "restored database object receipts could not be rebound"
                ) from None
            try:
                await selected_runtime.app_access_granter(engine)
            except Exception:
                raise RuntimeError("restored app role grants could not be applied") from None
            try:
                app_engine = selected_runtime.app_engine_factory(target.app_database_url)
            except Exception:
                raise RuntimeError("restored app role database is unavailable") from None
            try:
                app_role_access = await selected_runtime.app_access_probe(app_engine)
            except Exception:
                raise RuntimeError("restored app role access could not be validated") from None
            if app_role_access is not True:
                raise RuntimeError("restored app role access could not be validated")
            try:
                return await selected_runtime.facts_inspector(engine)
            except Exception:
                raise RuntimeError("restored database validation failed") from None

        async def perform_mutations(database_identity_sha256: str) -> RestoreValidationReport:
            return await validate_teaching_restore(
                backup.manifest,
                target_database_identity_sha256=database_identity_sha256,
                object_prefix=prefix,
                object_inventory=backup.object_inventory,
                restore_database=restore_database,
                restore_objects=restore_objects,
                inspect_restored_facts=inspect_restored_facts,
            )

        async def verify_restored_object_prefix() -> None:
            if object_client is None or restored_receipts is None or object_control_claim is None:
                raise RuntimeError("restored object bucket verification failed")
            try:
                exact_prefix = await _restored_object_prefix_is_exact(
                    object_client,
                    bucket=target.object_bucket,
                    prefix=prefix,
                    expected_receipts=restored_receipts,
                    required_control_claim=object_control_claim,
                )
            except Exception:
                raise RuntimeError("restored object bucket verification failed") from None
            if not exact_prefix:
                raise RuntimeError("restored object bucket verification failed")

        if measured_execution:
            target_exclusion = selected_runtime.target_exclusion
            object_state_probe = selected_runtime.object_state_probe
            exclusion_mode = selected_runtime.target_exclusion_mode
            if (
                target_exclusion is None
                or object_state_probe is None
                or exclusion_mode is None
                or target_config_sha256 is None
                or database_ownership is None
                or object_namespace_ownership is None
                or candidate_sha256 is None
                or environment_id is None
                or provisioning_receipt_body is None
                or provisioning_receipt_sha256 is None
            ):
                raise RuntimeError("restore measured target evidence is unavailable")
            try:
                lock_database_state = await selected_runtime.database_probe(engine)
            except Exception:
                raise RuntimeError(
                    "restore target physical database identity could not be inspected"
                ) from None
            exclusion_identity_sha256 = getattr(
                lock_database_state,
                "identity_sha256",
                None,
            )
            if (
                not isinstance(exclusion_identity_sha256, str)
                or _SHA256.fullmatch(exclusion_identity_sha256) is None
                or exclusion_identity_sha256 == backup.manifest.database.identity_sha256
            ):
                raise ValueError("restore target physical database identity is invalid")
            observations: list[TargetObservation] = []

            async def observe_target() -> TargetObservation:
                try:
                    database_state = await selected_runtime.database_probe(engine)
                    object_state = await object_state_probe(object_client, target)
                except Exception:
                    raise RuntimeError("restore target state could not be inspected") from None
                if database_state.identity_sha256 != exclusion_identity_sha256:
                    raise RuntimeError("restore target physical database identity changed")
                observation = TargetObservation(
                    database=database_state,
                    objects=object_state,
                )
                observations.append(observation)
                if len(observations) == 2:
                    await verify_restored_object_prefix()
                return observation

            async def mutate_target() -> RestoreValidationReport:
                nonlocal object_control_claim
                if len(observations) != 1:
                    raise RuntimeError("restore target pre-observation is unavailable")
                before = observations[0]
                _validate_measured_target_precondition(
                    before,
                    target=target,
                    source_database_identity_sha256=backup.manifest.database.identity_sha256,
                    source_object_identity_sha256=(
                        backup.manifest.source_object_store_identity_sha256
                    ),
                )
                _validate_restore_inputs(
                    backup.manifest,
                    target_database_identity_sha256=before.database.identity_sha256,
                    object_prefix=prefix,
                    object_inventory=backup.object_inventory,
                )
                from backup_restore_contract import parse_target_provisioning_receipt

                parse_target_provisioning_receipt(
                    provisioning_receipt_body,
                    provisioning_receipt_sha256=provisioning_receipt_sha256,
                    candidate_sha256=candidate_sha256,
                    release_run={"runId": run_id, "environmentId": environment_id},
                    database_disposition=database_ownership,
                    object_store_disposition=object_namespace_ownership,
                    database_identity_sha256=before.database.identity_sha256,
                    object_store_identity_sha256=before.objects.identity_sha256,
                )
                object_control_claim = await _claim_object_restore_control(
                    object_client,
                    bucket=target.object_bucket,
                    candidate_sha256=candidate_sha256,
                    provisioning_receipt_sha256=provisioning_receipt_sha256,
                    run_id=run_id,
                    environment_id=environment_id,
                    database_identity_sha256=before.database.identity_sha256,
                    object_store_identity_sha256=before.objects.identity_sha256,
                )
                return await perform_mutations(before.database.identity_sha256)

            def validate_target_transition(
                before: object,
                after: object,
                current_report: object,
            ) -> RestoreValidationReport:
                if (
                    not isinstance(before, TargetObservation)
                    or not isinstance(after, TargetObservation)
                    or not isinstance(current_report, RestoreValidationReport)
                ):
                    raise RuntimeError("restore target observations are invalid")
                target_evidence = _validate_measured_target_transition(
                    before,
                    after,
                    len(backup.object_inventory),
                    target=target,
                    target_config_sha256=target_config_sha256,
                    provisioning_receipt_sha256=provisioning_receipt_sha256,
                    database_ownership=database_ownership,
                    object_namespace_ownership=object_namespace_ownership,
                    exclusion_mode=exclusion_mode,
                    exclusion_identity_sha256=exclusion_identity_sha256,
                )
                return replace(current_report, target_evidence=target_evidence)

            report = await _execute_measured_target_operation(
                exclusion=lambda: target_exclusion(engine, exclusion_identity_sha256),
                observe=observe_target,
                mutate=mutate_target,
                validate=validate_target_transition,
            )
    finally:
        primary_failure = sys.exception()
        cleanup_failures: list[str] = []
        cleanup_cancellation: asyncio.CancelledError | None = None

        async def reconcile_cleanup(label: str, cleanup: Awaitable[None]) -> None:
            nonlocal cleanup_cancellation
            try:
                await cleanup
            except asyncio.CancelledError as cancellation:
                if primary_failure is not None:
                    primary_failure.add_note(
                        f"restore {label} cleanup was repeatedly cancelled after reconciliation"
                    )
                elif cleanup_cancellation is None:
                    cleanup_cancellation = cancellation
            except BaseException:
                cleanup_failures.append(label)

        if app_engine is not None:
            await reconcile_cleanup(
                "app role database",
                _await_owned_cleanup(app_engine.dispose()),
            )
        if object_client is not None:
            await reconcile_cleanup("object client", _close_resource(object_client))
        await reconcile_cleanup(
            "target database",
            _await_owned_cleanup(engine.dispose()),
        )
        if cleanup_failures:
            summary = "restore resource cleanup failed: " + ", ".join(cleanup_failures)
            if primary_failure is not None:
                primary_failure.add_note(summary)
            elif cleanup_cancellation is not None:
                cleanup_cancellation.add_note(summary)
            else:
                raise RuntimeError(summary) from None
        if primary_failure is None and cleanup_cancellation is not None:
            raise cleanup_cancellation

    if report is None:
        raise RuntimeError("restore validation did not produce a report")
    _write_restore_report(
        report_target,
        report,
        run_id=run_id,
        restored_object_count=len(backup.object_inventory),
        archive_fingerprint_sha256=backup.archive_fingerprint_sha256,
        manifest_sha256=backup.manifest_sha256,
        target_object_bucket=target.object_bucket,
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--target-config", required=True, type=Path)
    parser.add_argument("--target-config-sha256", required=True)
    parser.add_argument("--provisioning-receipt", required=True, type=Path)
    parser.add_argument("--provisioning-receipt-sha256", required=True)
    parser.add_argument("--target-secret-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--pg-restore", default=Path("pg_restore"), type=Path)
    parser.add_argument(
        "--database-ownership",
        required=True,
        choices=tuple(sorted(_OWNERSHIP_VALUES)),
    )
    parser.add_argument(
        "--object-namespace-ownership",
        required=True,
        choices=tuple(sorted(_OWNERSHIP_VALUES)),
    )
    parser.add_argument("--deadline-monotonic", required=True, type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = asyncio.run(
            run_restore_operator(
                backup_dir=arguments.backup_dir,
                target_config=arguments.target_config,
                provisioning_receipt=arguments.provisioning_receipt,
                provisioning_receipt_sha256=arguments.provisioning_receipt_sha256,
                target_secret_dir=arguments.target_secret_dir,
                run_id=arguments.run_id,
                report_path=arguments.report,
                pg_restore=arguments.pg_restore,
                target_config_sha256=arguments.target_config_sha256,
                database_ownership=arguments.database_ownership,
                object_namespace_ownership=arguments.object_namespace_ownership,
                candidate_sha256=arguments.candidate_sha256,
                environment_id=arguments.environment_id,
                deadline_monotonic=arguments.deadline_monotonic,
            )
        )
    except Exception:
        print("teaching restore validation failed", file=sys.stderr)
        return 1
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
