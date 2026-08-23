from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from deeptutor.teaching.secret_permissions import secret_file_is_restricted


def _load_backup_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "backup_teaching.py"
    spec = importlib.util.spec_from_file_location("backup_teaching_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BACKUP = _load_backup_module()
_SOURCE_OBJECT_STORE_NAMESPACE_ID = "source-minio-primary"
_SOURCE_OBJECT_STORE_BUCKET = "source-teaching"
_SOURCE_OBJECT_STORE_IDENTITY = _BACKUP.object_store_identity_sha256(
    _SOURCE_OBJECT_STORE_NAMESPACE_ID,
    _SOURCE_OBJECT_STORE_BUCKET,
)
RuntimeConfig = _BACKUP._RuntimeConfig
BackupManifest = _BACKUP.BackupManifest
DatabaseBackup = _BACKUP.DatabaseBackup
ObjectInventoryEntry = _BACKUP.ObjectInventoryEntry
DatabaseObjectReference = _BACKUP.DatabaseObjectReference
MinioVersionedObjectStore = _BACKUP.MinioVersionedObjectStore
OperatorBackupSecrets = _BACKUP.OperatorBackupSecrets
RestorableObjectInventoryEntry = _BACKUP.RestorableObjectInventoryEntry
TeachingBackupFacts = _BACKUP.TeachingBackupFacts
VersionedObject = _BACKUP.VersionedObject
create_restorable_teaching_backup = _BACKUP.create_restorable_teaching_backup
create_teaching_backup = _BACKUP.create_teaching_backup
load_backup_manifest = _BACKUP.load_backup_manifest
load_operator_backup_config = _BACKUP.load_operator_backup_config
load_operator_backup_secrets = _BACKUP.load_operator_backup_secrets
load_verified_backup = _BACKUP.load_verified_backup
parse_backup_arguments = _BACKUP.parse_backup_arguments
reverify_verified_backup = _BACKUP.reverify_verified_backup
run_pg_dump = _BACKUP.run_pg_dump
write_backup_manifest = _BACKUP.write_backup_manifest
dump_postgres_snapshot = _BACKUP._dump_postgres_snapshot


def _facts() -> TeachingBackupFacts:
    return TeachingBackupFacts(
        database_identity_sha256="a" * 64,
        platform_schema_revision="platform-revision",
        schema_revisions={"tenant-a": "20260810_0017"},
        classroom_versions_count=2,
        learning_events_count=5,
    )


def _versioned_object(
    tenant_id: str,
    key: str,
    version_id: str,
    payload: bytes,
    *,
    content_type: str = "application/json",
    owner_token: str = "1" * 32,
    source_revision: str = "source-etag-1",
) -> VersionedObject:
    return VersionedObject(
        tenant_id=tenant_id,
        key=key,
        version_id=version_id,
        content_type=content_type,
        owner_token=owner_token,
        source_revision=source_revision,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


def test_backup_receipt_types_expose_exact_source_metadata_contract() -> None:
    assert {
        "content_type",
        "owner_token",
        "source_revision",
        "sha256",
        "size",
    } <= set(VersionedObject.__dataclass_fields__)
    assert {"content_type", "owner_token", "source_revision"} <= set(
        RestorableObjectInventoryEntry.__dataclass_fields__
    )
    assert {"content_type", "owner_token", "source_revision"} <= set(
        DatabaseObjectReference.__dataclass_fields__
    )
    assert "source_object_store_identity_sha256" in BackupManifest.__dataclass_fields__


def test_object_store_identity_binds_stable_namespace_and_bucket() -> None:
    first = _BACKUP.object_store_identity_sha256("minio-primary", "teaching")
    second = _BACKUP.object_store_identity_sha256("minio-primary", "teaching")

    assert first == second
    assert first != _BACKUP.object_store_identity_sha256("minio-secondary", "teaching")
    assert first != _BACKUP.object_store_identity_sha256("minio-primary", "other")
    with pytest.raises(ValueError, match="object store identity"):
        _BACKUP.object_store_identity_sha256("同一存储", "teaching")


def test_backup_manifest_schema_v2_is_rejected() -> None:
    with pytest.raises(ValueError, match="schema version"):
        BackupManifest(
            schema_version=2,
            created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
            database=DatabaseBackup(
                file="database.dump",
                sha256="a" * 64,
                size=1,
                identity_sha256="b" * 64,
            ),
            object_inventory_file="objects.json",
            object_inventory_sha256="c" * 64,
            object_count=0,
            source_object_store_identity_sha256=_SOURCE_OBJECT_STORE_IDENTITY,
            platform_schema_revision="platform-revision",
            schema_revisions={},
            classroom_versions_count=0,
            learning_events_count=0,
        )


def test_restorable_backup_does_not_expose_an_unsafe_publish_override() -> None:
    assert "publish_backup" not in inspect.signature(create_restorable_teaching_backup).parameters


def _create_restorable_fixture(tmp_path):
    output = tmp_path / "backup"
    payload = b"version-pinned-document"
    source = _versioned_object(
        "tenant-a",
        "tenants/tenant-a/classrooms/a/document.json",
        "version-2",
        payload,
    )
    calls: list[tuple[str, object]] = []

    async def dump(path: Path) -> TeachingBackupFacts:
        calls.append(("dump", path))
        path.write_bytes(b"database")
        return _facts()

    async def enumerate_versions():
        calls.append(("enumerate", None))
        return (source,)

    async def read_version(item: VersionedObject, path: Path) -> None:
        calls.append(("read", item.version_id))
        path.write_bytes(payload)

    manifest = asyncio.run(
        create_restorable_teaching_backup(
            output,
            dump_database=dump,
            enumerate_object_versions=enumerate_versions,
            read_object_version=read_version,
            source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
            source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
            created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
    )
    return output, source, payload, manifest, calls


def test_restorable_backup_copies_versioned_object_payloads_before_publishing(tmp_path) -> None:
    output, source, payload, manifest, calls = _create_restorable_fixture(tmp_path)
    relative = f"objects/{hashlib.sha256(source.key.encode()).hexdigest()}.blob"
    entry = RestorableObjectInventoryEntry(
        source.tenant_id,
        source.key,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        source.version_id,
        relative,
        source.content_type,
        source.owner_token,
        source.source_revision,
    )

    assert calls == [
        ("dump", output.with_name("backup.partial") / "database.dump"),
        ("enumerate", None),
        ("read", source.version_id),
    ]
    assert not output.with_name("backup.partial").exists()
    assert (output / relative).read_bytes() == payload
    assert json.loads((output / manifest.object_inventory_file).read_text()) == [
        {
            "key": entry.key,
            "content_type": entry.content_type,
            "owner_token": entry.owner_token,
            "payload_file": entry.payload_file,
            "sha256": entry.sha256,
            "size": entry.size,
            "source_revision": entry.source_revision,
            "tenant_id": entry.tenant_id,
            "version_id": entry.version_id,
        }
    ]
    verified = load_verified_backup(output)
    assert verified.object_inventory == (entry,)
    assert verified.object_payloads == (output / relative,)


def test_restorable_backup_explicitly_sets_private_archive_modes(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    original = _BACKUP._set_private_mode

    def record(path: Path, mode: int) -> None:
        calls.append((Path(path).relative_to(tmp_path).as_posix(), mode))
        original(path, mode)

    monkeypatch.setattr(_BACKUP, "_set_private_mode", record)
    output, source, _payload, _manifest, _calls = _create_restorable_fixture(tmp_path)
    blob = f"backup.partial/objects/{hashlib.sha256(source.key.encode()).hexdigest()}.blob"

    assert {path for path, mode in calls if mode == 0o700} >= {
        "backup.partial",
        "backup.partial/objects",
    }
    assert {path for path, mode in calls if mode == 0o600} >= {
        "backup.partial/database.dump",
        blob,
        "backup.partial/objects.json",
        "backup.partial/manifest.json",
        "backup.partial/manifest.sha256",
    }
    assert output.is_dir()
    restricted_paths = (
        output,
        output / "objects",
        output / "database.dump",
        output / "objects.json",
        output / "manifest.json",
        output / "manifest.sha256",
        output / blob.removeprefix("backup.partial/"),
    )
    assert all(secret_file_is_restricted(path) for path in restricted_paths)


def test_restorable_backup_fsyncs_complete_staging_tree_before_single_publish(
    tmp_path, monkeypatch
) -> None:
    events: list[tuple[str, str]] = []

    def record_file(path: Path) -> None:
        events.append(("file", Path(path).relative_to(tmp_path).as_posix()))

    def record_directory(path: Path) -> None:
        events.append(("directory", Path(path).relative_to(tmp_path).as_posix()))

    def publish(staging: Path, output: Path) -> None:
        events.append(("publish", f"{staging.name}->{output.name}"))
        staging.rename(output)

    monkeypatch.setattr(_BACKUP, "_fsync_file", record_file)
    monkeypatch.setattr(_BACKUP, "_fsync_directory", record_directory)
    monkeypatch.setattr(_BACKUP, "_publish_directory_no_replace", publish)
    _output, source, _payload, _manifest, _calls = _create_restorable_fixture(tmp_path)
    blob = f"backup.partial/objects/{hashlib.sha256(source.key.encode()).hexdigest()}.blob"

    assert events == [
        ("file", "backup.partial/database.dump"),
        ("file", blob),
        ("file", "backup.partial/objects.json"),
        ("file", "backup.partial/manifest.json"),
        ("file", "backup.partial/manifest.sha256"),
        ("directory", "backup.partial/objects"),
        ("directory", "backup.partial"),
        ("publish", "backup.partial->backup"),
        ("directory", "."),
    ]


def test_backup_directory_publish_never_replaces_a_concurrent_destination(tmp_path) -> None:
    staging = tmp_path / "backup.partial"
    staging.mkdir()
    (staging / "manifest.json").write_text("staged", encoding="utf-8")
    destination = tmp_path / "backup"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        _BACKUP._publish_directory_no_replace(staging, destination)

    assert (staging / "manifest.json").read_text(encoding="utf-8") == "staged"
    assert list(destination.iterdir()) == []


def test_restorable_backup_rejects_unknown_object_tenant_but_allows_zero_objects(
    tmp_path,
) -> None:
    async def dump(path: Path) -> TeachingBackupFacts:
        path.write_bytes(b"database")
        return _facts()

    async def no_objects():
        return ()

    async def unexpected_read(_source, _path):
        raise AssertionError("zero-object backup must not read a payload")

    empty_manifest = asyncio.run(
        create_restorable_teaching_backup(
            tmp_path / "empty-backup",
            dump_database=dump,
            enumerate_object_versions=no_objects,
            read_object_version=unexpected_read,
            source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
            source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
        )
    )
    assert empty_manifest.object_count == 0

    async def outside_tenant():
        return (
            _versioned_object(
                "tenant-b",
                "tenants/tenant-b/classrooms/b/document.json",
                "v1",
                b"outside",
            ),
        )

    with pytest.raises(ValueError, match="tenant outside the database snapshot"):
        asyncio.run(
            create_restorable_teaching_backup(
                tmp_path / "outside-backup",
                dump_database=dump,
                enumerate_object_versions=outside_tenant,
                read_object_version=unexpected_read,
                source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
                source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
            )
        )


def test_restorable_backup_rejects_missing_database_referenced_object_key(tmp_path) -> None:
    facts = TeachingBackupFacts(
        database_identity_sha256="a" * 64,
        platform_schema_revision="platform-revision",
        schema_revisions={"tenant-a": "20260810_0017"},
        classroom_versions_count=2,
        learning_events_count=5,
        database_object_references=(
            DatabaseObjectReference(
                tenant_id="tenant-a",
                key="tenants/tenant-a/required.bin",
                sha256="b" * 64,
                size=1,
                version_id="v1",
            ),
        ),
    )

    async def dump(path: Path) -> TeachingBackupFacts:
        path.write_bytes(b"database")
        return facts

    async def incomplete_objects():
        return (
            _versioned_object(
                "tenant-a",
                "tenants/tenant-a/classrooms/a/document.json",
                "v1",
                b"incomplete",
            ),
        )

    async def unexpected_read(_source, _path):
        raise AssertionError("missing database reference must fail before payload reads")

    with pytest.raises(ValueError, match="missing database-referenced object keys"):
        asyncio.run(
            create_restorable_teaching_backup(
                tmp_path / "backup",
                dump_database=dump,
                enumerate_object_versions=incomplete_objects,
                read_object_version=unexpected_read,
                source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
                source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
            )
        )


@pytest.mark.parametrize(
    (
        "expected_sha256",
        "expected_size",
        "expected_version_id",
        "expected_content_type",
        "expected_owner_token",
        "expected_source_revision",
    ),
    [
        pytest.param(
            "f" * 64,
            len(b"actual-payload"),
            "actual-version",
            "application/json",
            "1" * 32,
            "source-etag-1",
            id="sha256",
        ),
        pytest.param(
            hashlib.sha256(b"actual-payload").hexdigest(),
            99,
            "actual-version",
            "application/json",
            "1" * 32,
            "source-etag-1",
            id="size",
        ),
        pytest.param(
            hashlib.sha256(b"actual-payload").hexdigest(),
            len(b"actual-payload"),
            "wrong-version",
            "application/json",
            "1" * 32,
            "source-etag-1",
            id="version-id",
        ),
        pytest.param(
            hashlib.sha256(b"actual-payload").hexdigest(),
            len(b"actual-payload"),
            "actual-version",
            "application/pdf",
            "1" * 32,
            "source-etag-1",
            id="content-type",
        ),
        pytest.param(
            hashlib.sha256(b"actual-payload").hexdigest(),
            len(b"actual-payload"),
            "actual-version",
            "application/json",
            "2" * 32,
            "source-etag-1",
            id="owner-token",
        ),
        pytest.param(
            hashlib.sha256(b"actual-payload").hexdigest(),
            len(b"actual-payload"),
            "actual-version",
            "application/json",
            "1" * 32,
            "wrong-revision",
            id="source-revision",
        ),
    ],
)
def test_restorable_backup_rejects_database_object_receipt_mismatch(
    tmp_path,
    expected_sha256: str,
    expected_size: int,
    expected_version_id: str,
    expected_content_type: str,
    expected_owner_token: str,
    expected_source_revision: str,
) -> None:
    key = "tenants/tenant-a/required.bin"
    facts = TeachingBackupFacts(
        database_identity_sha256="a" * 64,
        platform_schema_revision="platform-revision",
        schema_revisions={"tenant-a": "20260810_0017"},
        classroom_versions_count=2,
        learning_events_count=5,
        database_object_references=(
            DatabaseObjectReference(
                tenant_id="tenant-a",
                key=key,
                sha256=expected_sha256,
                size=expected_size,
                version_id=expected_version_id,
                content_type=expected_content_type,
                owner_token=expected_owner_token,
                source_revision=expected_source_revision,
            ),
        ),
    )

    async def dump(path: Path) -> TeachingBackupFacts:
        path.write_bytes(b"database")
        return facts

    async def enumerate_objects():
        return (_versioned_object("tenant-a", key, "actual-version", b"actual-payload"),)

    async def read_object(_source, path: Path) -> None:
        path.write_bytes(b"actual-payload")

    with pytest.raises(ValueError, match="database object receipt"):
        asyncio.run(
            create_restorable_teaching_backup(
                tmp_path / "backup",
                dump_database=dump,
                enumerate_object_versions=enumerate_objects,
                read_object_version=read_object,
                source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
                source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
            )
        )


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_verified_backup_rejects_damaged_object_payload_before_restore(
    tmp_path, damage: str
) -> None:
    output, source, _payload, _manifest, _calls = _create_restorable_fixture(tmp_path)
    path = output / f"objects/{hashlib.sha256(source.key.encode()).hexdigest()}.blob"
    if damage == "missing":
        path.rename(output / "held.blob")
    else:
        path.write_bytes(b"tampered")
    external_calls: list[str] = []

    with pytest.raises(ValueError, match="object payload"):
        load_verified_backup(output)
        external_calls.append("restore")

    assert external_calls == []


def test_verified_backup_parses_checked_bytes_once_and_rechecks_fingerprints(
    tmp_path, monkeypatch
) -> None:
    output, _source, _payload, _manifest, _calls = _create_restorable_fixture(tmp_path)
    original = Path.read_bytes
    reads = {"manifest.json": 0, "objects.json": 0}

    def count_reads(path: Path) -> bytes:
        if path.name in reads:
            reads[path.name] += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", count_reads)
    verified = load_verified_backup(output)
    assert reads == {"manifest.json": 1, "objects.json": 1}
    assert verified.archive_fingerprint_sha256
    assert verified.database_sha256 == verified.manifest.database.sha256

    manifest_path = output / "manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["createdAt"] = "2026-08-24T00:00:00+00:00"
    manifest_bytes = _BACKUP._canonical_json(manifest_payload)
    manifest_path.write_bytes(manifest_bytes)
    (output / "manifest.sha256").write_bytes(
        _BACKUP._canonical_json({"manifestSha256": hashlib.sha256(manifest_bytes).hexdigest()})
    )

    with pytest.raises(ValueError, match="changed after verification"):
        reverify_verified_backup(verified)
    assert reads == {"manifest.json": 2, "objects.json": 2}


def test_pg_dump_keeps_password_out_of_argv_output_and_errors(tmp_path, monkeypatch) -> None:
    password = "PG_SECRET_MUST_NOT_LEAK"
    monkeypatch.setenv("PGHOST", "attacker.invalid")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/untrusted/library")
    monkeypatch.setenv("SERVICE_TOKEN", "TOKEN_MUST_NOT_LEAK")
    captured: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(argv, **kwargs):
        captured.append((tuple(argv), kwargs))
        output = next(
            argument.removeprefix("--file=") for argument in argv if argument.startswith("--file=")
        )
        Path(output).write_bytes(b"PGDMP\x01")
        return subprocess.CompletedProcess(argv, 0)

    options = dict(
        pg_dump=Path("/operator/pg_dump"),
        destination=tmp_path / "database.dump",
        host="postgres",
        port=5432,
        database="yfeistai",
        user="operator",
        password=password,
        snapshot_id="00000003-0000001B-1",
    )
    run_pg_dump(**options, runner=runner)
    argv, call = captured[0]
    assert password not in " ".join(argv)
    assert call["env"]["PGPASSWORD"] == password
    assert not {"PGHOST", "LD_LIBRARY_PATH", "SERVICE_TOKEN"}.intersection(call["env"])
    assert call["stdout"] is call["stderr"] is subprocess.DEVNULL

    with pytest.raises(RuntimeError) as exc_info:
        run_pg_dump(
            **options,
            runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 2),
        )
    assert password not in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    (None, b"", b"PGDMP", b"not-custom"),
    ids=("no-write", "empty", "truncated-magic", "wrong-magic"),
)
def test_pg_dump_rejects_exit_zero_without_custom_format_archive(
    tmp_path: Path,
    payload: bytes | None,
) -> None:
    destination = tmp_path / "database.dump"

    def runner(argv, **_kwargs):
        if payload is not None:
            destination.write_bytes(payload)
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(RuntimeError, match="valid custom-format archive"):
        run_pg_dump(
            pg_dump=Path("pg_dump"),
            destination=destination,
            host="postgres",
            port=5432,
            database="yfeistai",
            user="operator",
            password="APP_PASSWORD",
            snapshot_id="snapshot-1",
            runner=runner,
        )


def test_postgres_snapshot_uses_app_secret_and_closes_transaction(tmp_path) -> None:
    events: list[object] = []

    class Transaction:
        async def start(self):
            events.append("start")

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    class Connection:
        tenant_revision = "20260810_0017"

        def transaction(self, **options):
            events.append(("transaction", options))
            return Transaction()

        async def fetchval(self, query):
            if "pg_export_snapshot" in query:
                return "snapshot-1"
            if "pg_database" in query:
                return "42"
            if "platform.alembic_version" in query:
                return "platform-revision"
            if "alembic_version" in query:
                return self.tenant_revision
            if "classroom_versions" in query:
                return 2
            if "learning_events" in query:
                return 5
            raise AssertionError(query)

        async def fetch(self, query):
            if "source_uploads" in query:
                assert all(
                    fragment in query
                    for fragment in (
                        "source_uploads",
                        "classroom_versions",
                        "document_object_key",
                        "classroom_artifacts",
                        "classroom_draft_media",
                        "classroom_exports",
                        "input_manifest_object_key",
                    )
                )
                return (
                    {
                        "object_key": "tenants/tenant-a/source.bin",
                        "sha256": "b" * 64,
                        "size_bytes": 17,
                        "version_id": "source-version-1",
                        "content_type": None,
                        "owner_token": "2" * 32,
                        "source_revision": "source-etag-2",
                    },
                )
            return (
                {
                    "tenant_id": "tenant-a",
                    "schema_name": "tenant_a",
                    "revision": "20260810_0017",
                    "status": "active",
                },
                {
                    "tenant_id": "tenant-pending",
                    "schema_name": "tenant_pending",
                    "revision": None,
                    "status": "pending",
                },
            )

        async def close(self):
            events.append("close")

    async def connect(**options):
        events.append(("connect", options))
        return Connection()

    config = RuntimeConfig(
        "postgres",
        5432,
        "yfeistai",
        "operator",
        "http://minio:9000",
        _SOURCE_OBJECT_STORE_NAMESPACE_ID,
        "teaching",
        "us-east-1",
    )
    destination = tmp_path / "database.dump"
    _BACKUP._write_private_new_file(destination)

    def runner(argv, **_options):
        assert destination.is_file()
        destination.write_bytes(b"PGDMP\x01")
        return subprocess.CompletedProcess(argv, 0)

    facts = asyncio.run(
        dump_postgres_snapshot(
            destination,
            config=config,
            password="APP_PASSWORD",
            pg_dump=Path("pg_dump"),
            connect=connect,
            runner=runner,
        )
    )

    connect_options = next(value for name, value in events if name == "connect")
    assert connect_options["password"] == "APP_PASSWORD"
    assert events[-3:] == ["start", "commit", "close"]
    assert facts.platform_schema_revision == "platform-revision"
    assert facts.schema_revisions == {"tenant-a": "20260810_0017"}
    assert facts.classroom_versions_count == 2
    assert facts.learning_events_count == 5
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
            "2" * 32,
            "source-etag-2",
        ),
    )

    events.clear()
    with pytest.raises(RuntimeError, match="pg_dump"):
        asyncio.run(
            dump_postgres_snapshot(
                destination,
                config=config,
                password="APP_PASSWORD",
                pg_dump=Path("pg_dump"),
                connect=connect,
                runner=lambda argv, **_options: subprocess.CompletedProcess(argv, 2),
            )
        )
    assert events[-3:] == ["start", "rollback", "close"]

    Connection.tenant_revision = "drifted-revision"
    events.clear()
    with pytest.raises(ValueError, match="revision drift"):
        asyncio.run(
            dump_postgres_snapshot(
                destination,
                config=config,
                password="APP_PASSWORD",
                pg_dump=Path("pg_dump"),
                connect=connect,
                runner=lambda *_args, **_options: (_ for _ in ()).throw(
                    AssertionError("pg_dump must not run after schema drift")
                ),
            )
        )
    assert events[-3:] == ["start", "rollback", "close"]


def test_minio_runtime_seam_reads_the_enumerated_version(tmp_path) -> None:
    key = "tenants/tenant-a/classrooms/a/document.json"
    payload = b"latest"
    calls: list[tuple[str, object]] = []

    class Response:
        remaining = payload

        def read(self, _size):
            chunk, self.remaining = self.remaining, b""
            return chunk

        def close(self):
            calls.append(("close", None))

        def release_conn(self):
            calls.append(("release", None))

    class Client:
        def get_bucket_versioning(self, bucket):
            calls.append(("versioning", bucket))
            return SimpleNamespace(status="Enabled")

        def list_objects(self, bucket, **kwargs):
            calls.append(("list", (bucket, kwargs)))
            return (
                SimpleNamespace(
                    object_name=key,
                    version_id="v2",
                    is_latest="true",
                    is_delete_marker=False,
                ),
                SimpleNamespace(
                    object_name="tenants/tenant-a/older.json",
                    version_id="v1",
                    is_latest="false",
                    is_delete_marker=False,
                ),
            )

        def stat_object(self, bucket, name, *, version_id):
            calls.append(("stat", (bucket, name, version_id)))
            return SimpleNamespace(
                content_type="application/json",
                metadata={
                    "x-amz-meta-owner": "1" * 32,
                    "x-amz-meta-sha256": hashlib.sha256(payload).hexdigest(),
                },
                etag="source-etag-1",
                version_id=version_id,
                size=len(payload),
            )

        def get_object(self, bucket, name, *, version_id):
            calls.append(("get", (bucket, name, version_id)))
            return Response()

    store = MinioVersionedObjectStore(Client(), bucket="teaching")
    versions = asyncio.run(store.enumerate_object_versions())
    destination = tmp_path / "payload.blob"
    asyncio.run(store.read_object_version(versions[0], destination))

    assert versions == (
        _versioned_object(
            "tenant-a",
            key,
            "v2",
            payload,
            source_revision='"source-etag-1"',
        ),
    )
    assert destination.read_bytes() == payload
    assert secret_file_is_restricted(destination)
    assert calls[3] == ("get", ("teaching", key, "v2"))
    assert calls[-2:] == [("close", None), ("release", None)]


@pytest.mark.parametrize("is_latest", [None, "TRUE", 1])
def test_minio_runtime_rejects_unknown_latest_markers(is_latest) -> None:
    class Client:
        def get_bucket_versioning(self, _bucket):
            return SimpleNamespace(status="Enabled")

        def list_objects(self, *_args, **_kwargs):
            return (
                SimpleNamespace(
                    object_name="tenants/tenant-a/document.json",
                    version_id="v1",
                    is_latest=is_latest,
                    is_delete_marker=False,
                ),
            )

    with pytest.raises(ValueError, match="is_latest"):
        asyncio.run(
            MinioVersionedObjectStore(
                Client(),
                bucket="teaching",
            ).enumerate_object_versions()
        )


def test_minio_runtime_rejects_bucket_without_enabled_versioning() -> None:
    calls: list[str] = []

    class Client:
        def get_bucket_versioning(self, bucket):
            calls.append(f"versioning:{bucket}")
            return SimpleNamespace(status="Suspended")

        def list_objects(self, *_args, **_kwargs):
            calls.append("list")
            return ()

    store = MinioVersionedObjectStore(Client(), bucket="teaching")

    with pytest.raises(ValueError, match="versioning"):
        asyncio.run(store.enumerate_object_versions())

    assert calls == ["versioning:teaching"]


def test_backup_cli_requires_explicit_runtime_boundaries(tmp_path) -> None:
    values = {
        "--config": tmp_path / "platform.json",
        "--secret-dir": tmp_path / "secrets",
        "--output": tmp_path / "backup",
        "--pg-dump": tmp_path / "pg_dump",
    }
    argv = [part for flag, value in values.items() for part in (flag, str(value))]
    arguments = parse_backup_arguments(argv)
    assert (arguments.config, arguments.secret_dir, arguments.output, arguments.pg_dump) == tuple(
        values.values()
    )
    for omitted in values:
        with pytest.raises(SystemExit):
            parse_backup_arguments(
                [
                    part
                    for flag, value in values.items()
                    if flag != omitted
                    for part in (flag, str(value))
                ]
            )


def test_operator_secret_loader_includes_migration_password_without_repr_leaks(tmp_path) -> None:
    config_path = tmp_path / "platform.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "database_host": "postgres",
                "database_port": 5432,
                "database_name": "yfeistai",
                "database_user": "operator",
                "object_store_mode": "s3",
                "object_store_endpoint": "http://minio:9000",
                "object_store_namespace_id": _SOURCE_OBJECT_STORE_NAMESPACE_ID,
                "object_store_bucket": "teaching",
                "object_store_region": "us-east-1",
            }
        ),
        encoding="utf-8",
    )
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    values = {
        "platform_database_app_password": "APP_SECRET",
        "platform_database_migration_password": "MIGRATION_SECRET",
        "minio_bootstrap_access_key": "ACCESS_SECRET",
        "minio_bootstrap_secret_key": "MINIO_SECRET",
    }
    for name, value in values.items():
        (secret_dir / name).write_text(value, encoding="utf-8")

    secrets = load_operator_backup_secrets(
        secret_dir,
        load_operator_backup_config(config_path),
    )

    assert secrets.database_password == values["platform_database_app_password"]
    assert secrets.database_migration_password == values["platform_database_migration_password"]
    assert not any(value in repr(secrets) for value in values.values())


def test_failed_restorable_backup_publishes_with_one_directory_rename(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "backup"
    source = _versioned_object(
        "tenant-a",
        "tenants/tenant-a/classrooms/a/document.json",
        "v1",
        b"payload",
    )

    async def dump(path):
        path.write_bytes(b"database")
        return _facts()

    async def enumerate_versions():
        return (source,)

    async def read(_source, path):
        path.write_bytes(b"payload")

    publish_calls: list[tuple[Path, Path]] = []

    def fail_publish(staging: Path, final: Path) -> None:
        publish_calls.append((staging, final))
        assert (staging / "manifest.sha256").is_file()
        assert not (staging / ".manifest.sha256.pending").exists()
        raise RuntimeError("publish failed")

    monkeypatch.setattr(_BACKUP, "_publish_directory_no_replace", fail_publish)
    with pytest.raises(RuntimeError):
        asyncio.run(
            create_restorable_teaching_backup(
                output,
                dump_database=dump,
                enumerate_object_versions=enumerate_versions,
                read_object_version=read,
                source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
                source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
            )
        )
    partial = output.with_name("backup.partial")
    assert not output.exists()
    assert publish_calls == [(partial, output)]
    assert (partial / "manifest.sha256").is_file()
    with pytest.raises(ValueError, match="incomplete"):
        load_verified_backup(partial)


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
        source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
        source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
        platform_schema_revision="platform-revision",
        schema_revisions={"tenant-a": "20260810_0017", "tenant-b": "20260810_0017"},
        classroom_versions_count=3,
        learning_events_count=8,
        created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
    )

    assert manifest.schema_version == 3
    assert manifest.database.sha256 == hashlib.sha256(database_dump.read_bytes()).hexdigest()
    assert manifest.database.size == len(database_dump.read_bytes())
    assert manifest.object_inventory_sha256
    assert manifest.source_object_store_identity_sha256 == _SOURCE_OBJECT_STORE_IDENTITY
    assert manifest.classroom_versions_count == 3
    assert manifest.learning_events_count == 8
    loaded = load_backup_manifest(output / "manifest.json")
    assert loaded == manifest
    inventory_bytes = (output / manifest.object_inventory_file).read_bytes()
    assert hashlib.sha256(inventory_bytes).hexdigest() == manifest.object_inventory_sha256
    assert json.loads((output / "manifest.sha256").read_text(encoding="utf-8")) == {
        "manifestSha256": hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    }
    assert (
        json.loads((output / "manifest.json").read_text(encoding="utf-8"))[
            "sourceObjectStoreIdentitySha256"
        ]
        == _SOURCE_OBJECT_STORE_IDENTITY
    )


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
            source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
            source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
            platform_schema_revision="platform-revision",
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
            platform_schema_revision="platform-revision",
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
            source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
            source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
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
        source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
        source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
        platform_schema_revision="platform-revision",
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
            source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
            source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
            platform_schema_revision="platform-revision",
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
    try:
        linked_output.symlink_to(real_output, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("current Windows token cannot create directory symlinks")
        raise

    with pytest.raises(ValueError, match="output directory is unsafe"):
        write_backup_manifest(
            linked_output,
            database_dump=database_dump,
            database_identity_sha256="b" * 64,
            object_inventory=(),
            source_object_store_namespace_id=_SOURCE_OBJECT_STORE_NAMESPACE_ID,
            source_object_store_bucket=_SOURCE_OBJECT_STORE_BUCKET,
            platform_schema_revision="platform-revision",
            schema_revisions={},
            classroom_versions_count=0,
            learning_events_count=0,
            created_at=datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
        )

    assert not (real_output / "manifest.json").exists()
