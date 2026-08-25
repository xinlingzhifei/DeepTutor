from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import multiprocessing
from pathlib import Path

import pytest

from deeptutor.services.memory.paths import memory_path_service_override
from deeptutor.services.path_service import PathService
from deeptutor.teaching.projectors.mastery import ProjectionEvent


def _project_classroom_memory_in_process(
    workspace_root: str,
    ready,
    go,
) -> None:
    import asyncio

    from deeptutor.teaching.projectors.memory import (
        ClassroomMemoryAggregate,
        ClassroomMemoryProjector,
    )

    event = ProjectionEvent(
        event_id="event-cross-process",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 12, 8, 0, tzinfo=UTC),
        payload={},
        scene_id="scene-a",
    )
    ready.put(True)
    if not go.wait(60):
        raise RuntimeError("classroom memory process start timed out")
    asyncio.run(
        ClassroomMemoryProjector().project(
            event,
            aggregate=ClassroomMemoryAggregate("active", 1, 0, 0, ()),
            target_path_service=PathService(workspace_root=Path(workspace_root)),
        )
    )


def test_classroom_is_a_registered_memory_surface() -> None:
    from deeptutor.services.memory.paths import SURFACES

    assert "classroom" in SURFACES


@pytest.mark.asyncio
async def test_projector_writes_only_sanitized_learning_facts_to_target_user(
    tmp_path,
) -> None:
    from deeptutor.teaching.projectors.memory import (
        ClassroomMemoryAggregate,
        ClassroomMemoryProjector,
    )

    current = PathService(workspace_root=tmp_path / "current-user")
    target = PathService(workspace_root=tmp_path / "student-a")
    event = ProjectionEvent(
        event_id="FULL-EVENT-ID-SECRET",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=7,
        event_type="quiz.graded",
        occurred_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        scene_id="quiz-1",
        knowledge_point_id="kp-fractions",
        payload={
            "answer": "FULL-ANSWER-SECRET",
            "provider": "PROVIDER-SECRET",
            "object_key": "OBJECT-KEY-SECRET",
            "other_user_id": "student-b-secret",
        },
    )
    aggregate = ClassroomMemoryAggregate(
        status="completed",
        completed_scene_count=4,
        valid_quiz_count=2,
        correct_quiz_count=1,
        difficult_knowledge_points=("kp-fractions",),
    )

    with memory_path_service_override(current):
        await ClassroomMemoryProjector().project(
            event,
            aggregate=aggregate,
            target_path_service=target,
        )

    target_files = tuple((target.get_memory_dir()).rglob("*"))
    persisted = "\n".join(
        path.read_text(encoding="utf-8") for path in target_files if path.is_file()
    )
    assert "quiz.graded" in persisted
    assert "kp-fractions" in persisted
    assert "Completed scenes: 4" in persisted
    assert "Valid quizzes: 2; correct: 1" in persisted
    assert not current.get_memory_dir().exists()
    for secret in (
        "FULL-ANSWER-SECRET",
        "PROVIDER-SECRET",
        "OBJECT-KEY-SECRET",
        "student-b-secret",
        "FULL-EVENT-ID-SECRET",
    ):
        assert secret not in persisted


@pytest.mark.asyncio
async def test_projector_fails_when_the_l1_trace_was_not_persisted(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching.projectors import memory as memory_module
    from deeptutor.teaching.projectors.memory import (
        ClassroomMemoryAggregate,
        ClassroomMemoryProjector,
    )

    def dropped_append(_path, _event) -> None:
        return None

    monkeypatch.setattr(memory_module, "_append_l1", dropped_append)
    target = PathService(workspace_root=tmp_path / "student-a")
    event = ProjectionEvent(
        event_id="event-dropped",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        payload={},
        scene_id="scene-a",
    )

    with pytest.raises(OSError, match="trace"):
        await ClassroomMemoryProjector().project(
            event,
            aggregate=ClassroomMemoryAggregate("active", 1, 0, 0, ()),
            target_path_service=target,
        )
    assert not (target.get_memory_dir() / "L2" / "classroom.md").exists()


@pytest.mark.asyncio
async def test_projector_replays_an_old_event_into_its_original_trace_day(
    tmp_path,
) -> None:
    from deeptutor.teaching.projectors.memory import (
        ClassroomMemoryAggregate,
        ClassroomMemoryProjector,
    )

    target = PathService(workspace_root=tmp_path / "student-a")
    event = ProjectionEvent(
        event_id="event-from-yesterday",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 12, 8, 0, tzinfo=UTC),
        payload={},
        scene_id="scene-a",
    )

    await ClassroomMemoryProjector().project(
        event,
        aggregate=ClassroomMemoryAggregate("active", 1, 0, 0, ()),
        target_path_service=target,
    )

    path = target.get_memory_dir() / "trace" / "classroom" / "2026-08-12.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["kind"] == "scene.completed"
    assert row["payload"]["seq"] == 1
    assert "event-from-yesterday" not in path.read_text(encoding="utf-8")


def test_projector_serializes_the_same_event_across_processes(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    go = context.Event()
    target = tmp_path / "student-a"
    processes = [
        context.Process(
            target=_project_classroom_memory_in_process,
            args=(str(target), ready, go),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for _process in processes:
        assert ready.get(timeout=60) is True
    go.set()
    for process in processes:
        process.join(60)

    assert [process.exitcode for process in processes] == [0, 0]
    rows = [
        json.loads(line)
        for line in (target / "memory" / "trace" / "classroom" / "2026-08-12.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "scene.completed"


@pytest.mark.asyncio
async def test_projector_is_idempotent_under_concurrency_and_keeps_one_aggregate(
    tmp_path,
) -> None:
    from deeptutor.services.memory.document import parse
    from deeptutor.teaching.projectors.memory import (
        ClassroomMemoryAggregate,
        ClassroomMemoryProjector,
    )

    target = PathService(workspace_root=tmp_path / "student-a")
    projector = ClassroomMemoryProjector()
    first = ProjectionEvent(
        event_id="event-1",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        payload={},
        scene_id="scene-a",
    )
    aggregate = ClassroomMemoryAggregate(
        status="active",
        completed_scene_count=1,
        valid_quiz_count=0,
        correct_quiz_count=0,
        difficult_knowledge_points=(),
    )

    import asyncio

    await asyncio.gather(
        *(
            projector.project(first, aggregate=aggregate, target_path_service=target)
            for _ in range(8)
        )
    )
    second = replace(
        first,
        event_id="event-2",
        seq=2,
        event_type="classroom.completed",
    )
    await projector.project(second, aggregate=aggregate, target_path_service=target)
    other_tenant = replace(
        first,
        tenant_id="tenant-b",
    )
    await projector.project(
        other_tenant,
        aggregate=aggregate,
        target_path_service=target,
    )

    trace_rows = [
        json.loads(line)
        for path in (target.get_memory_dir() / "trace" / "classroom").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    from deeptutor.services.memory.ids import is_trace_id

    assert len(trace_rows) == 3
    assert all(is_trace_id(row["id"]) for row in trace_rows)
    assert len(trace_rows) == 3
    assert len({row["id"] for row in trace_rows}) == 3
    assert [row["kind"] for row in trace_rows].count("scene.completed") == 2
    assert [row["kind"] for row in trace_rows].count("classroom.completed") == 1
    l2 = parse((target.get_memory_dir() / "L2" / "classroom.md").read_text(encoding="utf-8"))
    assert len(l2.all_entries()) == 3


@pytest.mark.asyncio
async def test_projector_never_replaces_a_newer_aggregate_after_lease_loss(
    tmp_path,
) -> None:
    from deeptutor.teaching.projectors.memory import (
        ClassroomMemoryAggregate,
        ClassroomMemoryProjector,
    )

    target = PathService(workspace_root=tmp_path / "student-a")
    event = ProjectionEvent(
        event_id="event-newer",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=2,
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        payload={},
        scene_id="scene-b",
    )
    projector = ClassroomMemoryProjector()
    await projector.project(
        event,
        aggregate=ClassroomMemoryAggregate(
            "active",
            2,
            1,
            1,
            (),
            projection_revision=20,
        ),
        target_path_service=target,
    )
    await projector.project(
        replace(event, event_id="event-stale", seq=1, scene_id="scene-a"),
        aggregate=ClassroomMemoryAggregate(
            "active",
            1,
            0,
            0,
            ("kp-stale",),
            projection_revision=10,
        ),
        target_path_service=target,
    )

    l2 = (target.get_memory_dir() / "L2" / "classroom.md").read_text(encoding="utf-8")
    assert (target.get_memory_dir() / ".classroom.revision").read_text(
        encoding="ascii"
    ).strip() == "20"
    assert "Completed scenes: 2" in l2
    assert "kp-stale" not in l2


@pytest.mark.asyncio
async def test_projector_fails_closed_when_the_revision_fence_is_corrupt(
    tmp_path,
) -> None:
    from deeptutor.teaching.projectors.memory import (
        ClassroomMemoryAggregate,
        ClassroomMemoryProjector,
    )

    target = PathService(workspace_root=tmp_path / "student-a")
    event = ProjectionEvent(
        event_id="event-a",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        payload={},
        scene_id="scene-a",
    )
    projector = ClassroomMemoryProjector()
    await projector.project(
        event,
        aggregate=ClassroomMemoryAggregate("active", 2, 0, 0, (), projection_revision=20),
        target_path_service=target,
    )
    revision = target.get_memory_dir() / ".classroom.revision"
    revision.write_text("not-a-revision\n", encoding="ascii")

    with pytest.raises(OSError, match="revision"):
        await projector.project(
            replace(event, event_id="event-b", seq=2),
            aggregate=ClassroomMemoryAggregate(
                "active", 1, 0, 0, ("kp-stale",), projection_revision=30
            ),
            target_path_service=target,
        )

    l2 = (target.get_memory_dir() / "L2" / "classroom.md").read_text(encoding="utf-8")
    assert "Completed scenes: 2" in l2
    assert "kp-stale" not in l2


@pytest.mark.parametrize(
    "user_id",
    (
        "",
        ".",
        "..",
        "../other-user",
        r"..\other-user",
        "/absolute/user",
        r"C:\absolute\user",
        "student.name",
        "student.",
        "CON",
        "nul",
        "COM1",
        "LPT9",
    ),
)
def test_memory_target_resolver_rejects_unsafe_or_admin_identity(user_id: str) -> None:
    from deeptutor.teaching.projectors.memory import ClassroomMemoryTargetResolver

    resolver = ClassroomMemoryTargetResolver()

    with pytest.raises(ValueError, match="user id"):
        resolver.path_service_for_user(user_id)


@pytest.mark.parametrize(
    "user_id",
    (
        "student-a",
        "student_name_1",
        "3f1bb8ec-7487-4f2d-9aae-62d519ed1281",
    ),
)
def test_memory_target_resolver_accepts_normal_platform_user_ids(user_id: str) -> None:
    from deeptutor.multi_user.paths import USERS_ROOT
    from deeptutor.teaching.projectors.memory import ClassroomMemoryTargetResolver

    service = ClassroomMemoryTargetResolver().path_service_for_user(user_id)

    assert service.workspace_root == (USERS_ROOT / user_id).resolve()


def test_memory_target_resolver_maps_local_admin_to_admin_workspace() -> None:
    from deeptutor.multi_user.paths import ADMIN_WORKSPACE_ROOT
    from deeptutor.teaching.projectors.memory import ClassroomMemoryTargetResolver

    service = ClassroomMemoryTargetResolver().path_service_for_user("local-admin")

    assert service.workspace_root == ADMIN_WORKSPACE_ROOT.resolve()


@pytest.mark.asyncio
async def test_learning_worker_projects_database_then_memory_before_completion() -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.memory import ClassroomMemoryAggregate

    event = ProjectionEvent(
        event_id="event-worker",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        payload={},
        scene_id="scene-a",
    )
    claim = __import__(
        "deeptutor.teaching.projector_worker", fromlist=["ProjectionClaim"]
    ).ProjectionClaim(event, "worker-a", "token-a")
    aggregate = ClassroomMemoryAggregate("active", 1, 0, 0, ())
    calls: list[str] = []

    class Repository:
        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, *args, **kwargs):
            return claim

        async def heartbeat(self, *args, **kwargs):
            calls.append("heartbeat")

        async def project_for_memory(self, claimed, *, document):
            calls.append("database")
            return aggregate

        async def complete(self, claimed):
            calls.append("completed")

    class Memory:
        async def project(self, claimed_event, *, aggregate, target_path_service):
            assert claimed_event is event
            assert target_path_service == "target-student-a"
            calls.append("memory")

    class Targets:
        def path_service_for_user(self, user_id):
            assert user_id == "student-a"
            return f"target-{user_id}"

    class Documents:
        async def load_version_document(self, *args):
            raise AssertionError("non-quiz event needs no document")

    worker = LearningProjectionWorker(
        repository=Repository(),
        documents=Documents(),
        worker_id="worker-a",
        memory_projector=Memory(),
        memory_targets=Targets(),
    )

    assert await worker.run_once() is True
    assert calls.index("database") < calls.index("memory") < calls.index("completed")


@pytest.mark.asyncio
async def test_learning_worker_reprojects_pbl_on_memory_completion_handoff_same_claim() -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.mastery import PblProjectionDocumentRequired
    from deeptutor.teaching.projectors.memory import ClassroomMemoryAggregate

    event = ProjectionEvent(
        event_id="event-pbl-memory-race",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="pbl.milestone_completed",
        occurred_at=datetime(2026, 8, 25, 8, 0, tzinfo=UTC),
        payload={"milestone_id": "milestone-1"},
        scene_id="pbl-scene",
        knowledge_point_id="kp-1",
    )
    claim = ProjectionClaim(event, "worker-a", "token-a")
    progress_only = ClassroomMemoryAggregate("completed", 1, 0, 0, ())
    graded = ClassroomMemoryAggregate("completed", 1, 0, 0, ("kp-1",))

    class Repository:
        def __init__(self) -> None:
            self.attempt_count = 5
            self.max_attempts = 5
            self.projected: list[tuple[ProjectionClaim, object | None]] = []
            self.completed: list[ProjectionClaim] = []
            self.actions: list[str] = []

        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, *args, **kwargs):
            return claim

        async def heartbeat(self, claimed, **kwargs):
            assert claimed is claim

        async def has_pbl_evaluation(self, loaded_event):
            assert loaded_event is event
            return False

        async def project_for_memory(self, claimed, *, document):
            assert claimed is claim
            self.projected.append((claimed, document))
            return progress_only if document is None else graded

        async def complete(self, claimed):
            assert claimed is claim
            self.completed.append(claimed)
            if len(self.completed) == 1:
                raise PblProjectionDocumentRequired("pbl_document_required")

        async def retry(self, *args, **kwargs):
            self.actions.append("retry")

        async def quarantine(self, *args, **kwargs):
            self.actions.append("quarantine")

    class Memory:
        def __init__(self) -> None:
            self.aggregates: list[ClassroomMemoryAggregate] = []

        async def project(self, claimed_event, *, aggregate, target_path_service):
            assert claimed_event is event
            assert target_path_service == "target-student-a"
            self.aggregates.append(aggregate)

    class Targets:
        def path_service_for_user(self, user_id):
            return f"target-{user_id}"

    class Documents:
        def __init__(self) -> None:
            self.loads: list[tuple[str, str]] = []

        async def load_version_document(self, tenant_id, version_id):
            self.loads.append((tenant_id, version_id))
            return "lineage-validated-document"

    repository = Repository()
    memory = Memory()
    documents = Documents()
    worker = LearningProjectionWorker(
        repository=repository,
        documents=documents,
        worker_id="worker-a",
        memory_projector=memory,
        memory_targets=Targets(),
    )

    assert await worker.run_once() is True
    assert repository.projected == [
        (claim, None),
        (claim, "lineage-validated-document"),
    ]
    assert repository.completed == [claim, claim]
    assert memory.aggregates == [progress_only, graded]
    assert documents.loads == [("tenant-a", "version-a")]
    assert repository.attempt_count == repository.max_attempts == 5
    assert repository.actions == []


@pytest.mark.asyncio
async def test_learning_worker_retries_memory_failure_without_database_completion() -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.memory import ClassroomMemoryAggregate

    event = ProjectionEvent(
        event_id="event-memory-fails",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        payload={},
    )
    claim = ProjectionClaim(event, "worker-a", "token-a")
    actions: list[str] = []

    class Repository:
        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, *args, **kwargs):
            return claim

        async def heartbeat(self, *args, **kwargs):
            return None

        async def project_for_memory(self, *args, **kwargs):
            return ClassroomMemoryAggregate("active", 0, 0, 0, ())

        async def complete(self, *args, **kwargs):
            actions.append("completed")

        async def retry(self, claimed, *, error_code):
            actions.append(error_code)

    class Memory:
        async def project(self, *args, **kwargs):
            raise OSError("memory unavailable")

    class Targets:
        def path_service_for_user(self, user_id):
            return object()

    class Documents:
        async def load_version_document(self, *args):
            raise AssertionError

    worker = LearningProjectionWorker(
        repository=Repository(),
        documents=Documents(),
        worker_id="worker-a",
        memory_projector=Memory(),
        memory_targets=Targets(),
    )

    assert await worker.run_once() is True
    assert actions == ["transient_oserror"]


@pytest.mark.asyncio
async def test_learning_worker_never_writes_memory_for_rejected_projection() -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker, ProjectionClaim
    from deeptutor.teaching.projectors.mastery import DeterministicProjectionError
    from deeptutor.teaching.projectors.memory import ClassroomMemoryAggregate

    event = ProjectionEvent(
        event_id="event-invalid",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="quiz.graded",
        occurred_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
        payload={},
        scene_id="quiz-a",
        knowledge_point_id="kp-a",
    )
    claim = ProjectionClaim(event, "worker-a", "token-a")
    actions: list[str] = []

    class Repository:
        async def active_tenant_ids(self):
            return ("tenant-a",)

        async def claim(self, *args, **kwargs):
            return claim

        async def heartbeat(self, *args, **kwargs):
            return None

        async def project_for_memory(self, *args, **kwargs):
            actions.append("database")
            raise DeterministicProjectionError("quiz_answer_invalid")

        async def complete(self, *args, **kwargs):
            actions.append("completed")

        async def quarantine(self, claimed, *, reason_code):
            actions.append(f"quarantine:{reason_code}")

    class Memory:
        async def project(self, *args, **kwargs):
            actions.append("memory")

    class Targets:
        def path_service_for_user(self, user_id):
            return object()

    class Documents:
        async def load_version_document(self, *args):
            return object()

    worker = LearningProjectionWorker(
        repository=Repository(),
        documents=Documents(),
        worker_id="worker-a",
        memory_projector=Memory(),
        memory_targets=Targets(),
    )

    assert await worker.run_once() is True
    assert actions == ["database", "quarantine:quiz_answer_invalid"]
