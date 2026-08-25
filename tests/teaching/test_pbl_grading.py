from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from deeptutor.teaching.permissions import ResourceScope, ScopedPermission
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext


def _document(
    *,
    version_id: str = "version-a",
    scene_type: str = "pbl",
    milestone_id: str = "milestone-a",
    rubric: str = "  Explain the design tradeoffs.  ",
    knowledge_points: tuple[str, ...] = ("kp-a",),
):
    return SimpleNamespace(
        classroom_version_id=version_id,
        openmaic=SimpleNamespace(
            scenes=[
                SimpleNamespace(
                    id="scene-a",
                    type=scene_type,
                    content=SimpleNamespace(
                        milestones=[
                            SimpleNamespace(id=milestone_id, rubric=rubric),
                        ]
                    ),
                )
            ]
        ),
        knowledge_point_mappings=[
            SimpleNamespace(knowledge_point_id=knowledge_point, scene_ids=["scene-a"])
            for knowledge_point in knowledge_points
        ],
    )


def _binding(**changes):
    from deeptutor.teaching.services.pbl_grading import PblGradingBinding

    values = {
        "event_id": "event-a",
        "event_tenant_id": "tenant-a",
        "event_session_id": "session-a",
        "event_user_id": "student-a",
        "event_classroom_version_id": "version-a",
        "event_type": "pbl.milestone_completed",
        "event_scene_id": "scene-a",
        "event_knowledge_point_id": "kp-a",
        "event_payload": {"milestone_id": "milestone-a"},
        "session_id": "session-a",
        "session_tenant_id": "tenant-a",
        "session_user_id": "student-a",
        "session_classroom_version_id": "version-a",
        "assignment_id": "assignment-a",
        "assignment_tenant_id": "tenant-a",
        "assignment_classroom_version_id": "version-a",
        "course_id": "course-a",
        "class_id": "class-a",
    }
    values.update(changes)
    return PblGradingBinding(**values)


def _context(*, role_scope: ResourceScope | None = None) -> TenantContext:
    permissions = frozenset()
    if role_scope is not None:
        scope_type = "class" if role_scope.class_id else "course"
        scope_id = role_scope.class_id or role_scope.course_id
        permissions = frozenset(
            {
                ScopedPermission(
                    permission="learning_event.grade",
                    scope_type=scope_type,
                    scope_id=str(scope_id),
                    tenant_id=role_scope.tenant_id,
                )
            }
        )
    return TenantContext(
        tenant_id="tenant-a",
        schema_name=tenant_schema_name("tenant-a"),
        user_id="teacher-a",
        permissions=permissions,
    )


def test_valid_teacher_grading_derives_all_authority_from_bound_facts() -> None:
    from deeptutor.teaching.services.pbl_grading import derive_pbl_evaluation

    evaluation = derive_pbl_evaluation(
        _binding(),
        _document(),
        passed=True,
        score=0.8,
    )

    assert evaluation.knowledge_point_id == "kp-a"
    assert evaluation.correct is True
    assert evaluation.score == 0.8
    assert evaluation.grading_source == "teacher_review"
    assert evaluation.rubric_sha256 == (
        "9c5756e892c873a06a1811c597cae4cb07091d33cf224fb0ef990a536175e80c"
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"event_tenant_id": "tenant-b"}, "pbl_event_binding_invalid"),
        ({"event_session_id": "session-b"}, "pbl_event_binding_invalid"),
        ({"event_user_id": "student-b"}, "pbl_event_binding_invalid"),
        ({"event_classroom_version_id": "version-b"}, "pbl_event_binding_invalid"),
        ({"event_type": "scene.completed"}, "pbl_event_type_invalid"),
        ({"event_scene_id": None}, "pbl_scene_invalid"),
        ({"event_payload": {}}, "pbl_milestone_invalid"),
        ({"event_payload": {"milestone_id": "missing"}}, "pbl_milestone_invalid"),
        ({"event_knowledge_point_id": "kp-wrong"}, "pbl_knowledge_point_invalid"),
        ({"assignment_id": None}, "pbl_class_authority_missing"),
    ],
)
def test_grading_fails_closed_for_invalid_event_session_assignment_bindings(
    changes: dict[str, object],
    reason: str,
) -> None:
    from deeptutor.teaching.projectors.mastery import DeterministicProjectionError
    from deeptutor.teaching.services.pbl_grading import derive_pbl_evaluation

    with pytest.raises(DeterministicProjectionError, match=reason):
        derive_pbl_evaluation(_binding(**changes), _document(), passed=True, score=None)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (_document(version_id="version-b"), "pbl_document_binding_invalid"),
        (_document(scene_type="slide"), "pbl_scene_invalid"),
        (_document(milestone_id="missing"), "pbl_milestone_invalid"),
        (_document(rubric="   "), "pbl_rubric_invalid"),
        (_document(knowledge_points=()), "pbl_knowledge_point_ambiguous"),
        (_document(knowledge_points=("kp-a", "kp-b")), "pbl_knowledge_point_ambiguous"),
    ],
)
def test_grading_fails_closed_for_untrusted_or_ambiguous_document(
    document: object,
    reason: str,
) -> None:
    from deeptutor.teaching.projectors.mastery import DeterministicProjectionError
    from deeptutor.teaching.services.pbl_grading import derive_pbl_evaluation

    with pytest.raises(DeterministicProjectionError, match=reason):
        derive_pbl_evaluation(_binding(), document, passed=False, score=0.2)


@pytest.mark.parametrize("score", (-0.01, 1.01, float("nan")))
def test_score_is_a_bounded_audit_fact_and_never_derives_correctness(score: float) -> None:
    from deeptutor.teaching.services.pbl_grading import PblGradingValidationError

    with pytest.raises(PblGradingValidationError, match="score"):
        PblGradingValidationError.validate_score(score)


def test_grading_permission_uses_real_assignment_class_scope() -> None:
    from deeptutor.teaching.services.pbl_grading import require_grading_permission

    binding = _binding()
    require_grading_permission(
        _context(role_scope=ResourceScope("tenant-a", "course-a", "class-a")),
        binding,
    )

    for denied in (
        _context(),
        _context(role_scope=ResourceScope("tenant-a", "course-a", "class-b")),
        _context(role_scope=ResourceScope("tenant-a", "course-b")),
    ):
        with pytest.raises(PermissionError, match="grading access denied"):
            require_grading_permission(denied, binding)

    with pytest.raises(PermissionError, match="grading access denied"):
        require_grading_permission(
            _context(role_scope=ResourceScope("tenant-a", "course-a", "class-a")),
            _binding(assignment_id=None),
        )


def test_idempotency_and_first_terminal_result_are_immutable() -> None:
    from deeptutor.teaching.services.pbl_grading import (
        PblGradingConflict,
        PblGradingRecord,
        resolve_existing_result,
    )

    existing = PblGradingRecord(
        result_id="result-a",
        event_id="event-a",
        passed=True,
        score=0.8,
        source_reference="review-42",
        grading_source="teacher_review",
        graded_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
        request_sha256="a" * 64,
    )
    assert (
        resolve_existing_result(
            existing_by_key=existing,
            existing_by_event=existing,
            request_sha256="a" * 64,
        )
        is existing
    )

    with pytest.raises(PblGradingConflict):
        resolve_existing_result(
            existing_by_key=existing,
            existing_by_event=existing,
            request_sha256="b" * 64,
        )
    with pytest.raises(PblGradingConflict):
        resolve_existing_result(
            existing_by_key=None,
            existing_by_event=existing,
            request_sha256="b" * 64,
        )


def test_queue_transition_requeues_completed_but_never_revives_quarantine() -> None:
    from deeptutor.teaching.services.pbl_grading import (
        PblGradingConflict,
        projection_queue_action,
    )

    assert projection_queue_action("completed") == "requeue"
    assert projection_queue_action("pending") == "preserve"
    assert projection_queue_action("failed") == "retry_now"
    assert projection_queue_action("running") == "preserve"
    with pytest.raises(PblGradingConflict, match="quarantined"):
        projection_queue_action("quarantined")


class _AsyncContext:
    def __init__(self, value, *, transaction: bool = False) -> None:
        self.value = value
        self.transaction = transaction

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, *_args) -> None:
        if self.transaction:
            self.value.committed = exc_type is None
            self.value.rolled_back = exc_type is not None


class _RepositorySession:
    def __init__(self, *, queue_status: str, fail_backlog: bool = False) -> None:
        from deeptutor.teaching.models import (
            Assignment,
            LearningEvent,
            LearningProjectionQueueItem,
            LearningSession,
            PblGradingResult,
            TeachingClass,
        )

        now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        self.queue = SimpleNamespace(
            event_id="event-a",
            tenant_id="tenant-a",
            session_id="session-a",
            status=queue_status,
            available_at=now,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            last_error_code=None,
        )
        self.entities = {
            LearningProjectionQueueItem: self.queue,
            LearningEvent: SimpleNamespace(
                event_id="event-a",
                tenant_id="tenant-a",
                session_id="session-a",
                user_id="student-a",
                classroom_version_id="version-a",
                event_type="pbl.milestone_completed",
                scene_id="scene-a",
                knowledge_point_id="kp-a",
                payload={"milestone_id": "milestone-a"},
                received_at=now,
            ),
            LearningSession: SimpleNamespace(
                id="session-a",
                tenant_id="tenant-a",
                user_id="student-a",
                classroom_version_id="version-a",
                assignment_id="assignment-a",
            ),
            Assignment: SimpleNamespace(
                id="assignment-a",
                tenant_id="tenant-a",
                classroom_version_id="version-a",
                class_id="class-a",
            ),
            TeachingClass: SimpleNamespace(id="class-a", course_id="course-a"),
            PblGradingResult: None,
        }
        self.now = now
        self.fail_backlog = fail_backlog
        self.executed_tables: list[str] = []
        self.added: list[object] = []
        self.committed = False
        self.rolled_back = False

    def begin(self):
        return _AsyncContext(self, transaction=True)

    async def scalar(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is None:
            return self.now
        return self.entities.get(entity)

    async def execute(self, statement):
        table = getattr(statement, "table", None)
        if table is not None:
            self.executed_tables.append(table.name)
        if self.fail_backlog and getattr(table, "name", None) == (
            "teaching_learning_projection_backlog"
        ):
            raise RuntimeError("backlog write failed")
        return SimpleNamespace()

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _RepositoryFactory:
    def __init__(self, session: _RepositorySession) -> None:
        self.session = session

    def __call__(self):
        return _AsyncContext(self.session)


class _Documents:
    async def load_version_document(self, context, version_id):
        assert (context.tenant_id, version_id) == ("tenant-a", "version-a")
        return _document()


def _grading_command():
    from deeptutor.teaching.services.pbl_grading import PblGradingCommand

    return PblGradingCommand(
        event_id="event-a",
        passed=True,
        score=0.8,
        source_reference="review-42",
        idempotency_key="grade-request-1",
    )


@pytest.mark.asyncio
async def test_sql_repository_atomically_requeues_late_completed_event() -> None:
    from deeptutor.teaching.repositories.pbl_grading import (
        SqlAlchemyPblGradingRepository,
    )

    session = _RepositorySession(queue_status="completed")
    repository = object.__new__(SqlAlchemyPblGradingRepository)
    repository._sessions = lambda _tenant_id: _RepositoryFactory(session)

    result = await repository.record(
        _context(role_scope=ResourceScope("tenant-a", "course-a", "class-a")),
        session_id="session-a",
        command=_grading_command(),
        documents=_Documents(),
    )

    assert result.event_id == "event-a"
    assert result.grading_source == "teacher_review"
    assert session.queue.status == "pending"
    assert session.committed is True
    assert session.rolled_back is False
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_sql_repository_makes_failed_event_immediately_claimable_without_new_backlog() -> (
    None
):
    from deeptutor.teaching.repositories.pbl_grading import (
        SqlAlchemyPblGradingRepository,
    )

    session = _RepositorySession(queue_status="failed")
    session.queue.available_at = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    session.queue.last_error_code = "transient_timeout"
    repository = object.__new__(SqlAlchemyPblGradingRepository)
    repository._sessions = lambda _tenant_id: _RepositoryFactory(session)

    await repository.record(
        _context(role_scope=ResourceScope("tenant-a", "course-a", "class-a")),
        session_id="session-a",
        command=_grading_command(),
        documents=_Documents(),
    )

    assert session.queue.status == "pending"
    assert session.queue.available_at == session.now
    assert session.queue.last_error_code is None
    assert "teaching_learning_projection_backlog" not in session.executed_tables


@pytest.mark.asyncio
async def test_sql_repository_never_revives_quarantined_event() -> None:
    from deeptutor.teaching.repositories.pbl_grading import (
        SqlAlchemyPblGradingRepository,
    )
    from deeptutor.teaching.services.pbl_grading import PblGradingConflict

    session = _RepositorySession(queue_status="quarantined")
    repository = object.__new__(SqlAlchemyPblGradingRepository)
    repository._sessions = lambda _tenant_id: _RepositoryFactory(session)

    with pytest.raises(PblGradingConflict, match="quarantined"):
        await repository.record(
            _context(role_scope=ResourceScope("tenant-a", "course-a", "class-a")),
            session_id="session-a",
            command=_grading_command(),
            documents=_Documents(),
        )

    assert session.queue.status == "quarantined"
    assert session.added == []
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_sql_repository_rolls_back_result_and_requeue_when_backlog_write_fails() -> None:
    from deeptutor.teaching.repositories.pbl_grading import (
        SqlAlchemyPblGradingRepository,
    )

    session = _RepositorySession(queue_status="completed", fail_backlog=True)
    repository = object.__new__(SqlAlchemyPblGradingRepository)
    repository._sessions = lambda _tenant_id: _RepositoryFactory(session)

    with pytest.raises(RuntimeError, match="backlog write failed"):
        await repository.record(
            _context(role_scope=ResourceScope("tenant-a", "course-a", "class-a")),
            session_id="session-a",
            command=_grading_command(),
            documents=_Documents(),
        )

    assert session.committed is False
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_sql_repository_returns_same_result_without_requeueing_again() -> None:
    from deeptutor.teaching.models import PblGradingResult
    from deeptutor.teaching.repositories.pbl_grading import (
        SqlAlchemyPblGradingRepository,
    )

    command = _grading_command()
    session = _RepositorySession(queue_status="completed")
    session.entities[PblGradingResult] = SimpleNamespace(
        id="result-a",
        event_id="event-a",
        correctness=True,
        score=0.8,
        source_reference="review-42",
        grading_source="teacher_review",
        graded_at=session.now,
        request_sha256=command.request_sha256,
    )
    repository = object.__new__(SqlAlchemyPblGradingRepository)
    repository._sessions = lambda _tenant_id: _RepositoryFactory(session)

    result = await repository.record(
        _context(role_scope=ResourceScope("tenant-a", "course-a", "class-a")),
        session_id="session-a",
        command=command,
        documents=_Documents(),
    )

    assert result.result_id == "result-a"
    assert session.queue.status == "completed"
    assert session.added == []


@pytest.mark.asyncio
async def test_sql_repository_denies_missing_and_cross_class_authority() -> None:
    from deeptutor.teaching.models import LearningSession
    from deeptutor.teaching.repositories.pbl_grading import (
        SqlAlchemyPblGradingRepository,
    )
    from deeptutor.teaching.services.pbl_grading import PblGradingAccessDenied

    denied_contexts = (
        _context(),
        _context(role_scope=ResourceScope("tenant-a", "course-a", "class-b")),
    )
    for denied_context in denied_contexts:
        session = _RepositorySession(queue_status="pending")
        repository = object.__new__(SqlAlchemyPblGradingRepository)
        repository._sessions = lambda _tenant_id: _RepositoryFactory(session)
        with pytest.raises(PblGradingAccessDenied):
            await repository.record(
                denied_context,
                session_id="session-a",
                command=_grading_command(),
                documents=_Documents(),
            )

    student_session = _RepositorySession(queue_status="pending")
    student_session.entities[LearningSession].assignment_id = None
    repository = object.__new__(SqlAlchemyPblGradingRepository)
    repository._sessions = lambda _tenant_id: _RepositoryFactory(student_session)
    with pytest.raises(PblGradingAccessDenied):
        await repository.record(
            _context(role_scope=ResourceScope("tenant-a", "course-a", "class-a")),
            session_id="session-a",
            command=_grading_command(),
            documents=_Documents(),
        )
