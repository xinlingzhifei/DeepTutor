"""Scoped durable intents, leases and opaque references for RAGFlow writes.

Transactions never span a network call. Workers claim an intent, persist
``SEND_STARTED`` before sending, then compare-and-swap the receipt. An expired
send may only enter reconciliation; claiming it never authorizes a second send.
Returned dictionaries contain server-only identifiers and must be projected by
the API before being returned to a caller.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import astuple, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterator
import uuid


class OperationConflict(ValueError):
    """An idempotency key, resource lock, revision or worker lease conflicts."""


@dataclass(frozen=True)
class OperationScope:
    owner_id: str
    workspace_scope: str
    generation_id: str
    binding_fingerprint: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in astuple(self)):
            raise ValueError("Operation scope fields must be nonempty strings")


_SCOPE = "owner_id=? AND workspace_scope=? AND generation_id=? AND binding_fingerprint=?"
_GENERATION = "owner_id=? AND workspace_scope=? AND generation_id=?"
_TERMINAL = {"RECONCILED", "FAILED", "NEEDS_ATTENTION"}
_VISIBILITY = {"active", "draft", "disabled", "archived", "deleted"}
_TRANSITIONS = {
    "INTENT_RECORDED": {"SEND_STARTED", "FAILED", "NEEDS_ATTENTION"},
    "SEND_STARTED": {"ACKNOWLEDGED", "RESULT_UNKNOWN", "FAILED", "NEEDS_ATTENTION"},
    "ACKNOWLEDGED": {
        "ACKNOWLEDGED",
        "RECONCILING",
        "RECONCILED",
        "RESULT_UNKNOWN",
        "NEEDS_ATTENTION",
    },
    "RESULT_UNKNOWN": {"RECONCILING", "NEEDS_ATTENTION"},
    "RECONCILING": {"RECONCILING", "RECONCILED", "RESULT_UNKNOWN", "FAILED", "NEEDS_ATTENTION"},
}
_CREDENTIAL_KEYS = {
    "apikey",
    "authorization",
    "proxyauthorization",
    "accesstoken",
    "refreshtoken",
    "password",
    "passwd",
    "clientsecret",
    "secret",
    "credential",
    "credentials",
    "bearertoken",
    "cookie",
    "setcookie",
}


def _json(value: dict) -> str:
    if not isinstance(value, dict):
        raise ValueError("Operation payload and result must be JSON objects")

    def check(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                if re.sub(r"[^a-z0-9]", "", key.lower()) in _CREDENTIAL_KEYS:
                    raise ValueError("Operation JSON must not contain credentials")
                check(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                check(nested)

    check(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


class OperationStore:
    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            from deeptutor.multi_user import paths

            path = paths.SYSTEM_ROOT / "ragflow" / "operations.sqlite3"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL, workspace_scope TEXT NOT NULL,
                    generation_id TEXT NOT NULL, binding_fingerprint TEXT NOT NULL,
                    action TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    payload TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    actor_id TEXT NOT NULL, resource_key TEXT,
                    state TEXT NOT NULL, revision INTEGER NOT NULL,
                    result TEXT, error_type TEXT,
                    lease_owner TEXT, lease_expires_at REAL, lease_seconds REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(owner_id, workspace_scope, generation_id, binding_fingerprint, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS operation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                    state TEXT NOT NULL, revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL, error_type TEXT, created_at REAL NOT NULL,
                    actor_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS events_operation ON operation_events(operation_id, id);
                CREATE TABLE IF NOT EXISTS visibility_revisions (
                    owner_id TEXT NOT NULL, workspace_scope TEXT NOT NULL,
                    generation_id TEXT NOT NULL, binding_fingerprint TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(owner_id, workspace_scope, generation_id, binding_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS documents (
                    document_ref TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL, workspace_scope TEXT NOT NULL,
                    generation_id TEXT NOT NULL, binding_fingerprint TEXT NOT NULL,
                    remote_id TEXT NOT NULL, title TEXT NOT NULL,
                    visibility TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(owner_id, workspace_scope, generation_id, binding_fingerprint, remote_id)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_ref TEXT PRIMARY KEY,
                    document_ref TEXT NOT NULL REFERENCES documents(document_ref),
                    remote_id TEXT NOT NULL, invalidated INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_chunk_identity
                    ON chunks(document_ref, remote_id) WHERE invalidated=0;
            """)
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(operation_events)")
            }
            if "actor_id" not in columns:
                connection.execute(
                    "ALTER TABLE operation_events ADD COLUMN actor_id TEXT NOT NULL DEFAULT ''"
                )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _operation(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] is not None else None
        result["scope"] = {key: result[key] for key in OperationScope.__dataclass_fields__}
        return result

    @staticmethod
    def _event(connection: sqlite3.Connection, operation: dict, event_type: str) -> None:
        connection.execute(
            "INSERT INTO operation_events(operation_id,state,revision,event_type,error_type,created_at,actor_id) VALUES(?,?,?,?,?,?,?)",
            (
                operation["operation_id"],
                operation["state"],
                operation["revision"],
                event_type,
                operation["error_type"],
                operation["updated_at"],
                operation["actor_id"],
            ),
        )

    def _get(
        self, connection: sqlite3.Connection, scope: OperationScope, operation_id: str
    ) -> dict | None:
        return self._operation(
            connection.execute(
                f"SELECT * FROM operations WHERE {_SCOPE} AND operation_id=?",
                (*astuple(scope), operation_id),
            ).fetchone()
        )

    def create_operation(
        self,
        scope: OperationScope,
        *,
        action: str,
        idempotency_key: str,
        payload: dict,
        actor_id: str = "",
        document_ref_to_unpublish: str | None = None,
    ) -> dict:
        _nonempty(action, "action")
        _nonempty(idempotency_key, "idempotency_key")
        serialized = _json(payload)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        resource_key = payload.get("resource_key")
        if resource_key is not None:
            _nonempty(resource_key, "resource_key")
        with self._connection(write=True) as connection:
            existing = self._operation(
                connection.execute(
                    f"SELECT * FROM operations WHERE {_SCOPE} AND idempotency_key=?",
                    (*astuple(scope), idempotency_key),
                ).fetchone()
            )
            if existing:
                if existing["action"] != action or existing["payload_digest"] != digest:
                    raise OperationConflict(
                        "Idempotency key already identifies a different request"
                    )
                return existing
            if connection.execute(
                f"""SELECT 1 FROM operations WHERE {_SCOPE}
                    AND state NOT IN ('RECONCILED','FAILED')
                    AND (?='dataset' OR resource_key='dataset' OR resource_key=?) LIMIT 1""",
                (*astuple(scope), resource_key, resource_key),
            ).fetchone():
                raise OperationConflict("Resource has an unfinished operation")
            now = time.time()
            operation_id = "op_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO operations(operation_id,owner_id,workspace_scope,generation_id,
                   binding_fingerprint,action,idempotency_key,payload,payload_digest,actor_id,
                   resource_key,state,revision,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'INTENT_RECORDED',1,?,?)""",
                (
                    operation_id,
                    *astuple(scope),
                    action,
                    idempotency_key,
                    serialized,
                    digest,
                    actor_id,
                    resource_key,
                    now,
                    now,
                ),
            )
            if document_ref_to_unpublish is not None:
                self._document(connection, scope, document_ref_to_unpublish)
                changed = connection.execute(
                    f"""UPDATE documents SET visibility='draft',revision=revision+1,updated_at=?
                        WHERE {_SCOPE} AND document_ref=? AND visibility='active'""",
                    (now, *astuple(scope), document_ref_to_unpublish),
                ).rowcount
                if changed:
                    self._bump_visibility(connection, scope)
            if action == "delete_dataset":
                changed = connection.execute(
                    f"""UPDATE documents SET visibility='draft',revision=revision+1,updated_at=?
                        WHERE {_SCOPE} AND visibility='active'""",
                    (now, *astuple(scope)),
                ).rowcount
                if changed:
                    self._bump_visibility(connection, scope)
            operation = self._get(connection, scope, operation_id)
            self._event(connection, operation, "created")
            return operation

    def get_operation(self, scope: OperationScope, operation_id: str) -> dict | None:
        with self._connection() as connection:
            return self._get(connection, scope, operation_id)

    def get_operation_for_generation(self, scope: OperationScope, operation_id: str) -> dict | None:
        """Read historical intents across a binding change; never grants write access."""
        with self._connection() as connection:
            return self._operation(
                connection.execute(
                    f"SELECT * FROM operations WHERE {_GENERATION} AND operation_id=?",
                    (*astuple(scope)[:3], operation_id),
                ).fetchone()
            )

    def list_operations(self, scope: OperationScope, limit: int = 50) -> list[dict]:
        with self._connection() as connection:
            return [
                self._operation(row)
                for row in connection.execute(
                    f"SELECT * FROM operations WHERE {_SCOPE} ORDER BY created_at DESC,operation_id LIMIT ?",
                    (*astuple(scope), max(0, min(int(limit), 1000))),
                )
            ]

    def list_operations_for_generation(self, scope: OperationScope, limit: int = 50) -> list[dict]:
        with self._connection() as connection:
            return [
                self._operation(row)
                for row in connection.execute(
                    f"SELECT * FROM operations WHERE {_GENERATION} ORDER BY created_at DESC,operation_id LIMIT ?",
                    (*astuple(scope)[:3], max(0, min(int(limit), 1000))),
                )
            ]

    def claim(
        self,
        scope: OperationScope,
        operation_id: str,
        worker_id: str,
        lease_seconds: float = 60,
    ) -> dict | None:
        _nonempty(worker_id, "worker_id")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        with self._connection(write=True) as connection:
            operation = self._get(connection, scope, operation_id)
            now = time.time()
            if operation is None or operation["state"] in _TERMINAL:
                return None
            live_lease = (
                operation["lease_expires_at"] is not None and operation["lease_expires_at"] > now
            )
            if live_lease and not (
                operation["lease_owner"] == worker_id
                and operation["state"] in {"ACKNOWLEDGED", "RESULT_UNKNOWN", "RECONCILING"}
            ):
                return None
            state = operation["state"]
            if not live_lease and state in {"SEND_STARTED", "RECONCILING"}:
                state = "RESULT_UNKNOWN"
            connection.execute(
                f"""UPDATE operations SET state=?,revision=revision+1,lease_owner=?,
                    lease_expires_at=?,lease_seconds=?,updated_at=? WHERE {_SCOPE} AND operation_id=?""",
                (
                    state,
                    worker_id,
                    now + lease_seconds,
                    lease_seconds,
                    now,
                    *astuple(scope),
                    operation_id,
                ),
            )
            claimed = self._get(connection, scope, operation_id)
            self._event(connection, claimed, "claimed")
            return claimed

    def transition(
        self,
        scope: OperationScope,
        operation_id: str,
        *,
        worker_id: str,
        expected_revision: int,
        state: str,
        result: dict | None = None,
        error_type: str | None = None,
    ) -> dict:
        serialized = _json(result) if result is not None else None
        if error_type is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", error_type):
            raise ValueError("error_type must be a safe error code")
        with self._connection(write=True) as connection:
            operation = self._get(connection, scope, operation_id)
            now = time.time()
            if (
                operation is None
                or operation["revision"] != expected_revision
                or operation["lease_owner"] != worker_id
                or operation["lease_expires_at"] is None
                or operation["lease_expires_at"] <= now
            ):
                raise OperationConflict("Operation revision or worker lease is no longer valid")
            if state not in _TRANSITIONS.get(operation["state"], set()):
                raise OperationConflict("Operation state does not allow this transition")
            terminal = state in _TERMINAL
            connection.execute(
                f"""UPDATE operations SET state=?,revision=revision+1,result=COALESCE(?,result),
                    error_type=?,lease_owner=?,lease_expires_at=?,updated_at=?
                    WHERE {_SCOPE} AND operation_id=? AND revision=?""",
                (
                    state,
                    serialized,
                    error_type,
                    None if terminal else worker_id,
                    None if terminal else now + operation["lease_seconds"],
                    now,
                    *astuple(scope),
                    operation_id,
                    expected_revision,
                ),
            )
            updated = self._get(connection, scope, operation_id)
            self._event(connection, updated, "transition")
            return updated

    def pending_operations(self) -> list[dict]:
        """Server-only recovery inventory across owners, with original scopes."""
        with self._connection() as connection:
            return [
                self._operation(row)
                for row in connection.execute(
                    "SELECT * FROM operations WHERE state NOT IN ('RECONCILED','FAILED','NEEDS_ATTENTION') ORDER BY created_at,operation_id"
                )
            ]

    def reauthorize_reconciliation(
        self,
        scope: OperationScope,
        operation_id: str,
        *,
        expected_revision: int,
        binding_snapshot: dict,
        actor_id: str,
    ) -> dict:
        """Replace a caller-verified principal for read-only reconciliation only.

        The caller must freshly authorize the actor and exact remote binding.
        With no worker identity supplied, any live lease prevents takeover.
        """
        _nonempty(actor_id, "actor_id")
        _json(binding_snapshot)
        with self._connection(write=True) as connection:
            operation = self._get(connection, scope, operation_id)
            now = time.time()
            if (
                operation is None
                or operation["revision"] != expected_revision
                or operation["state"] not in {"NEEDS_ATTENTION", "RESULT_UNKNOWN"}
                or (
                    operation["lease_expires_at"] is not None
                    and operation["lease_expires_at"] > now
                )
            ):
                raise OperationConflict(
                    "Operation state, revision or lease prevents reauthorization"
                )
            previous = operation["payload"].get("binding_snapshot")
            if (
                not isinstance(previous, dict)
                or binding_snapshot.get("actor_id") != actor_id
                or binding_snapshot.get("actor_role") not in {"admin", "user"}
                or binding_snapshot.get("resource_owner_id") != scope.owner_id
                or binding_snapshot.get("workspace_scope") != scope.workspace_scope
                or binding_snapshot.get("generation_id") != scope.generation_id
                or binding_snapshot.get("kb_ref") != previous.get("kb_ref")
                or not isinstance(binding_snapshot.get("binding_fingerprint"), str)
                or not binding_snapshot["binding_fingerprint"]
                or type(binding_snapshot.get("require_write")) is not bool
            ):
                raise OperationConflict(
                    "Reauthorization cannot change the recorded knowledge identity"
                )
            payload = _json({**operation["payload"], "binding_snapshot": binding_snapshot})
            connection.execute(
                f"""UPDATE operations SET payload=?,payload_digest=?,actor_id=?,
                    state='RESULT_UNKNOWN',revision=revision+1,error_type=NULL,
                    lease_owner=NULL,lease_expires_at=NULL,lease_seconds=NULL,updated_at=?
                    WHERE {_SCOPE} AND operation_id=? AND revision=?""",
                (
                    payload,
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    actor_id,
                    now,
                    *astuple(scope),
                    operation_id,
                    expected_revision,
                ),
            )
            updated = self._get(connection, scope, operation_id)
            self._event(connection, updated, "reconciliation_reauthorized")
            return updated

    def pending_binding_activations(self) -> list[dict]:
        """Recover local binding publication after the remote create is reconciled."""
        with self._connection() as connection:
            operations = (
                self._operation(row)
                for row in connection.execute(
                    "SELECT * FROM operations WHERE action='create_dataset' AND state='RECONCILED' ORDER BY created_at,operation_id"
                )
            )
            return [
                operation
                for operation in operations
                if (operation["result"] or {}).get("binding_activated") is not True
            ]

    def mark_binding_activated(self, scope: OperationScope, operation_id: str) -> dict:
        """Record local publication against the original scope without resending."""
        with self._connection(write=True) as connection:
            operation = self._get(connection, scope, operation_id)
            if (
                operation is None
                or operation["action"] != "create_dataset"
                or operation["state"] != "RECONCILED"
            ):
                raise OperationConflict("Only a reconciled dataset creation can be activated")
            result = operation["result"] or {}
            if result.get("binding_activated") is True:
                return operation
            connection.execute(
                f"UPDATE operations SET result=?,revision=revision+1,updated_at=? WHERE {_SCOPE} AND operation_id=?",
                (
                    _json({**result, "binding_activated": True}),
                    time.time(),
                    *astuple(scope),
                    operation_id,
                ),
            )
            updated = self._get(connection, scope, operation_id)
            self._event(connection, updated, "binding_activated")
            return updated

    def events(self, scope: OperationScope, operation_id: str, after_id: int = 0) -> list[dict]:
        with self._connection() as connection:
            if self._get(connection, scope, operation_id) is None:
                return []
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM operation_events WHERE operation_id=? AND id>? ORDER BY id",
                    (operation_id, after_id),
                )
            ]

    @staticmethod
    def _bump_visibility(connection: sqlite3.Connection, scope: OperationScope) -> None:
        connection.execute(
            """INSERT INTO visibility_revisions(owner_id,workspace_scope,generation_id,binding_fingerprint,revision)
               VALUES(?,?,?,?,1) ON CONFLICT(owner_id,workspace_scope,generation_id,binding_fingerprint)
               DO UPDATE SET revision=revision+1""",
            astuple(scope),
        )

    @staticmethod
    def _document(connection: sqlite3.Connection, scope: OperationScope, document_ref: str) -> dict:
        row = connection.execute(
            f"SELECT * FROM documents WHERE {_SCOPE} AND document_ref=?",
            (*astuple(scope), document_ref),
        ).fetchone()
        if row is None:
            raise ValueError("Document reference was not found in this knowledge scope")
        return dict(row)

    def register_document(
        self,
        scope: OperationScope,
        remote_id: str,
        *,
        visibility: str = "draft",
        title: str = "",
    ) -> dict:
        _nonempty(remote_id, "remote_id")
        if visibility not in _VISIBILITY:
            raise ValueError("Unknown document visibility")
        with self._connection(write=True) as connection:
            existing = connection.execute(
                f"SELECT * FROM documents WHERE {_SCOPE} AND remote_id=?",
                (*astuple(scope), remote_id),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            ref = "doc_" + uuid.uuid4().hex
            now = time.time()
            connection.execute(
                """INSERT INTO documents(document_ref,owner_id,workspace_scope,generation_id,
                    binding_fingerprint,remote_id,title,visibility,revision,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
                (ref, *astuple(scope), remote_id, title, visibility, now, now),
            )
            self._bump_visibility(connection, scope)
            return self._document(connection, scope, ref)

    def resolve_document(self, scope: OperationScope, document_ref: str) -> str:
        with self._connection() as connection:
            return self._document(connection, scope, document_ref)["remote_id"]

    def list_documents(self, scope: OperationScope) -> list[dict]:
        with self._connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM documents WHERE {_SCOPE} ORDER BY created_at,document_ref",
                    astuple(scope),
                )
            ]

    def visibility_snapshot(self, scope: OperationScope) -> tuple[int, list[str]]:
        with self._connection() as connection:
            revision = connection.execute(
                f"SELECT revision FROM visibility_revisions WHERE {_SCOPE}",
                astuple(scope),
            ).fetchone()
            active = [
                row["remote_id"]
                for row in connection.execute(
                    f"SELECT remote_id FROM documents WHERE {_SCOPE} AND visibility='active' ORDER BY remote_id",
                    astuple(scope),
                )
            ]
            return (revision["revision"] if revision else 0), active

    def set_visibility(
        self,
        scope: OperationScope,
        document_ref: str,
        visibility: str,
        *,
        expected_revision: int | None = None,
    ) -> dict:
        if visibility not in _VISIBILITY:
            raise ValueError("Unknown document visibility")
        with self._connection(write=True) as connection:
            document = self._document(connection, scope, document_ref)
            if expected_revision is not None and document["revision"] != expected_revision:
                raise OperationConflict("Document visibility revision has changed")
            if (
                visibility == "active"
                and connection.execute(
                    f"""SELECT 1 FROM operations WHERE {_SCOPE}
                    AND state NOT IN ('RECONCILED','FAILED')
                    AND resource_key IN ('dataset',?,?) LIMIT 1""",
                    (
                        *astuple(scope),
                        "document:" + document["remote_id"],
                        "cancel:" + document["remote_id"],
                    ),
                ).fetchone()
            ):
                raise OperationConflict(
                    "Document has an unfinished operation and cannot be published"
                )
            if document["visibility"] == visibility:
                return document
            if document["visibility"] == "deleted":
                raise OperationConflict("A deleted document cannot be republished")
            connection.execute(
                f"UPDATE documents SET visibility=?,revision=revision+1,updated_at=? WHERE {_SCOPE} AND document_ref=?",
                (visibility, time.time(), *astuple(scope), document_ref),
            )
            self._bump_visibility(connection, scope)
            return self._document(connection, scope, document_ref)

    def register_chunk(self, scope: OperationScope, document_ref: str, remote_chunk_id: str) -> str:
        _nonempty(remote_chunk_id, "remote_chunk_id")
        with self._connection(write=True) as connection:
            self._document(connection, scope, document_ref)
            existing = connection.execute(
                "SELECT chunk_ref FROM chunks WHERE document_ref=? AND remote_id=? AND invalidated=0",
                (document_ref, remote_chunk_id),
            ).fetchone()
            if existing is not None:
                return existing["chunk_ref"]
            ref = "chunk_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO chunks(chunk_ref,document_ref,remote_id) VALUES(?,?,?)",
                (ref, document_ref, remote_chunk_id),
            )
            return ref

    def resolve_chunk(self, scope: OperationScope, document_ref: str, chunk_ref: str) -> str:
        with self._connection() as connection:
            self._document(connection, scope, document_ref)
            row = connection.execute(
                "SELECT remote_id FROM chunks WHERE document_ref=? AND chunk_ref=? AND invalidated=0",
                (document_ref, chunk_ref),
            ).fetchone()
            if row is None:
                raise ValueError("Chunk reference was not found in this knowledge document")
            return row["remote_id"]

    def invalidate_chunks(self, scope: OperationScope, document_ref: str) -> None:
        with self._connection(write=True) as connection:
            self._document(connection, scope, document_ref)
            connection.execute(
                "UPDATE chunks SET invalidated=1 WHERE document_ref=?", (document_ref,)
            )
