"""Privacy-minimized classroom facts projected into one learner's memory."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Iterator

from deeptutor.services.memory.document import Document, Entry, parse, serialize
from deeptutor.services.memory.ids import new_entry_id
from deeptutor.services.memory.paths import (
    l2_file,
    memory_path_service_override,
    trace_file,
)
from deeptutor.services.memory.trace import TraceEvent
from deeptutor.services.path_service import PathService
from deeptutor.teaching.projectors.mastery import ProjectionEvent

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class ClassroomMemoryAggregate:
    status: str
    completed_scene_count: int
    valid_quiz_count: int
    correct_quiz_count: int
    difficult_knowledge_points: tuple[str, ...]
    projection_revision: int = 0


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _event_ref(event: ProjectionEvent) -> str:
    """Map one server event to a deterministic, time-ordered trace id."""

    binding = f"{len(event.tenant_id)}:{event.tenant_id}{len(event.event_id)}:{event.event_id}"
    timestamp_ms = int(event.occurred_at.timestamp() * 1000) & ((1 << 50) - 1)
    entropy = int.from_bytes(hashlib.sha256(binding.encode("utf-8")).digest()[:10], "big")
    return f"classroom:{_encode(timestamp_ms, 10)}{_encode(entropy, 16)}"


def _event_summary(event: ProjectionEvent) -> dict[str, object]:
    """Allow-list non-sensitive facts; never copy arbitrary event payload."""

    summary: dict[str, object] = {
        "seq": event.seq,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.isoformat(),
        "classroom_version_id": event.classroom_version_id,
    }
    if event.scene_id is not None:
        summary["scene_id"] = event.scene_id
    if event.knowledge_point_id is not None:
        summary["knowledge_point_id"] = event.knowledge_point_id
    return summary


def _l2_facts(aggregate: ClassroomMemoryAggregate) -> tuple[tuple[str, str], ...]:
    facts: list[tuple[str, str]] = [
        ("Completion", f"Status: {aggregate.status}"),
        ("Completion", f"Completed scenes: {aggregate.completed_scene_count}"),
        (
            "Valid quizzes",
            f"Valid quizzes: {aggregate.valid_quiz_count}; correct: {aggregate.correct_quiz_count}",
        ),
    ]
    facts.extend(
        ("Difficult knowledge points", f"Difficult knowledge point: {knowledge_point_id}")
        for knowledge_point_id in aggregate.difficult_knowledge_points
    )
    return tuple(facts)


def _upsert_l2(path: Path, aggregate: ClassroomMemoryAggregate) -> None:
    doc = (
        parse(path.read_text(encoding="utf-8"))
        if path.exists()
        else Document(title="classroom memory")
    )
    # L2 is one current aggregate, not a per-event log. Stable refs point to
    # the classroom surface rather than accumulating a full event history.
    for section, entries in doc.sections:
        entries[:] = [entry for entry in entries if "classroom" not in entry.refs]
    for section, text in _l2_facts(aggregate):
        doc.section_entries(section).append(
            Entry(id=new_entry_id(), section=section, text=text, refs=["classroom"])
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(serialize(doc), encoding="utf-8")
    tmp.replace(path)


def _read_revision(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        raw = path.read_text(encoding="ascii").strip()
        revision = int(raw)
    except (OSError, ValueError) as exc:
        raise OSError("classroom memory revision fence is invalid") from exc
    if revision < 0:
        raise OSError("classroom memory revision fence is invalid")
    return revision


def _write_revision(path: Path, revision: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{revision}\n", encoding="ascii")
    tmp.replace(path)


@contextmanager
def _exclusive_memory_lock(path: Path) -> Iterator[None]:
    """Serialize a target user's classroom L1/L2 read-modify-write cycle."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _append_l1(path: Path, event: TraceEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _project_files(
    event: ProjectionEvent,
    aggregate: ClassroomMemoryAggregate,
    target_path_service: PathService,
) -> None:
    ref = _event_ref(event)
    memory_dir = target_path_service.get_memory_dir()
    with _exclusive_memory_lock(memory_dir / ".classroom.lock"):
        with memory_path_service_override(target_path_service):
            l1_path = trace_file("classroom", event.occurred_at.date())
            existing = l1_path.read_text(encoding="utf-8") if l1_path.exists() else ""
            if f'"id":"{ref}"' not in existing:
                _append_l1(
                    l1_path,
                    TraceEvent(
                        id=ref,
                        ts=event.occurred_at.isoformat(),
                        surface="classroom",
                        kind=event.event_type,
                        payload=_event_summary(event),
                        session_id=event.session_id,
                    ),
                )
                persisted = l1_path.read_text(encoding="utf-8") if l1_path.exists() else ""
                if f'"id":"{ref}"' not in persisted:
                    raise OSError("classroom memory trace was not persisted")
            revision_path = memory_dir / ".classroom.revision"
            if _read_revision(revision_path) > aggregate.projection_revision:
                return
            _upsert_l2(l2_file("classroom"), aggregate)
            _write_revision(revision_path, aggregate.projection_revision)


class ClassroomMemoryTargetResolver:
    """Resolve a trusted per-user PathService without constructing raw paths."""

    def path_service_for_user(self, user_id: str) -> PathService:
        from deeptutor.multi_user.models import LOCAL_ADMIN_ID

        if user_id == LOCAL_ADMIN_ID:
            from deeptutor.multi_user.paths import get_admin_path_service

            return get_admin_path_service()
        if not _USER_ID_RE.fullmatch(user_id) or user_id.upper() in _WINDOWS_RESERVED_NAMES:
            raise ValueError("classroom memory user id is invalid")
        from deeptutor.multi_user.paths import (
            USERS_ROOT,
            get_path_service_for_scope,
            scope_for_user,
        )

        scope = scope_for_user(user_id, is_admin=False)
        root = scope.root.resolve()
        users_root = USERS_ROOT.resolve()
        if root.parent != users_root:
            raise ValueError("classroom memory user path is invalid")
        return get_path_service_for_scope(scope)


class ClassroomMemoryProjector:
    """Write L1/L2 under an explicitly supplied target PathService."""

    async def project(
        self,
        event: ProjectionEvent,
        *,
        aggregate: ClassroomMemoryAggregate,
        target_path_service: PathService,
    ) -> None:
        await asyncio.to_thread(
            _project_files,
            event,
            aggregate,
            target_path_service,
        )


__all__ = [
    "ClassroomMemoryAggregate",
    "ClassroomMemoryProjector",
    "ClassroomMemoryTargetResolver",
]
