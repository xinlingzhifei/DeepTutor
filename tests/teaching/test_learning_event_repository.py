from __future__ import annotations

from datetime import UTC, datetime
import importlib
import importlib.util

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB


def _learning_models():
    assert importlib.util.find_spec("deeptutor.teaching.models.learning") is not None
    return importlib.import_module("deeptutor.teaching.models.learning")


def _learning_repository():
    assert importlib.util.find_spec("deeptutor.teaching.repositories.learning_events") is not None
    return importlib.import_module("deeptutor.teaching.repositories.learning_events")


def test_learning_models_declare_the_eight_plan06_tables() -> None:
    models = _learning_models()

    assert {
        models.LearningSession.__table__.name,
        models.LearningEvent.__table__.name,
        models.LearningProjectionQueueItem.__table__.name,
        models.QuizAttempt.__table__.name,
        models.MasteryEvidence.__table__.name,
        models.MasteryLevel.__table__.name,
        models.LearningProgress.__table__.name,
        models.LearningEventQuarantine.__table__.name,
    } == {
        "learning_sessions",
        "learning_events",
        "learning_projection_queue",
        "quiz_attempts",
        "mastery_evidence",
        "mastery_levels",
        "learning_progress",
        "learning_event_quarantine",
    }


def test_learning_event_metadata_keeps_payload_jsonb_and_query_fields_independent() -> None:
    models = _learning_models()
    table = models.LearningEvent.__table__

    assert isinstance(table.c.payload.type, JSONB)
    assert {
        "event_type",
        "occurred_at",
        "session_id",
        "classroom_version_id",
        "knowledge_point_id",
    }.issubset(table.c.keys())
    assert {
        "ix_learning_events_event_type",
        "ix_learning_events_occurred_at",
        "ix_learning_events_session_id",
        "ix_learning_events_classroom_version_id",
        "ix_learning_events_knowledge_point_id",
    }.issubset({index.name for index in table.indexes})


def test_learning_event_metadata_enforces_idempotency_and_session_order() -> None:
    models = _learning_models()

    def unique_columns(model) -> set[tuple[str, ...]]:
        return {
            tuple(constraint.columns.keys())
            for constraint in model.__table__.constraints
            if isinstance(constraint, UniqueConstraint)
        }

    assert ("event_id",) in unique_columns(models.LearningEvent)
    assert ("session_id", "seq") in unique_columns(models.LearningEvent)
    assert ("event_id",) in unique_columns(models.QuizAttempt)
    assert ("event_id",) in unique_columns(models.MasteryEvidence)
    assert ("user_id", "knowledge_point_id") in unique_columns(models.MasteryLevel)


def test_learning_session_requires_exactly_one_authority_reference() -> None:
    models = _learning_models()
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in models.LearningSession.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert checks["ck_learning_sessions_authority"] == (
        "(assignment_id IS NOT NULL AND student_asset_id IS NULL) OR "
        "(assignment_id IS NULL AND student_asset_id IS NOT NULL)"
    )


def test_append_command_rejects_naive_occurrence_time() -> None:
    repository = _learning_repository()

    with pytest.raises(ValueError, match="occurred_at must be timezone-aware"):
        repository.LearningEventAppend(
            event_id="event-1",
            tenant_id="tenant-a",
            session_id="session-1",
            user_id="student-1",
            classroom_version_id="version-1",
            event_type="scene.completed",
            occurred_at=datetime(2026, 8, 10, 12, 0),
            payload={"scene_id": "scene-1"},
        )

    command = repository.LearningEventAppend(
        event_id="event-1",
        tenant_id="tenant-a",
        session_id="session-1",
        user_id="student-1",
        classroom_version_id="version-1",
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        payload={"scene_id": "scene-1"},
    )
    assert command.event_id == "event-1"


def test_learning_revision_is_the_tenant_schema_head() -> None:
    from deeptutor.teaching.provisioning_worker import TENANT_SCHEMA_REVISION

    assert TENANT_SCHEMA_REVISION == "20260824_0018"


@pytest.mark.asyncio
async def test_append_in_session_uses_caller_transaction_without_finishing_it() -> None:
    models = _learning_models()
    repository_module = _learning_repository()

    learning_session = models.LearningSession(
        id="session-1",
        tenant_id="tenant-a",
        user_id="student-1",
        classroom_version_id="version-1",
        assignment_id="assignment-1",
        student_asset_id=None,
        status="active",
        next_seq=1,
        last_cursor={"last_event_seq": 0},
    )

    class CallerManagedSession:
        def __init__(self) -> None:
            self.scalar_results = [learning_session, None, None]
            self.added: list[object] = []
            self.flush_count = 0
            self.finished = False

        async def execute(self, _statement, _parameters=None):
            return None

        async def scalar(self, _statement):
            return self.scalar_results.pop(0)

        def add(self, value: object) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            self.flush_count += 1

        async def commit(self) -> None:
            self.finished = True
            raise AssertionError("repository must not commit the caller transaction")

        async def rollback(self) -> None:
            self.finished = True
            raise AssertionError("repository must not roll back the caller transaction")

        async def close(self) -> None:
            self.finished = True
            raise AssertionError("repository must not close the caller session")

    database_session = CallerManagedSession()
    repository = object.__new__(repository_module.SqlAlchemyLearningEventRepository)
    repository._tenant_id = "tenant-a"
    event = repository_module.LearningEventAppend(
        event_id="event-1",
        tenant_id="tenant-a",
        session_id="session-1",
        user_id="student-1",
        classroom_version_id="version-1",
        event_type="scene.completed",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        payload={"schema_version": "1.0", "scene_id": "scene-1"},
        scene_id="scene-1",
    )

    result = await repository.append_in_session(database_session, event)

    assert (result.outcome, result.seq) == ("accepted", 1)
    assert [type(value) for value in database_session.added] == [
        models.LearningEvent,
        models.LearningProjectionQueueItem,
    ]
    assert database_session.flush_count == 2
    assert database_session.finished is False


@pytest.mark.asyncio
async def test_duplicate_event_id_with_changed_payload_is_rejected() -> None:
    models = _learning_models()
    repository_module = _learning_repository()
    occurred_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    learning_session = models.LearningSession(
        id="session-1",
        tenant_id="tenant-a",
        user_id="student-1",
        classroom_version_id="version-1",
        assignment_id="assignment-1",
        student_asset_id=None,
        status="active",
        next_seq=2,
        last_cursor={"last_event_seq": 1},
    )
    existing = models.LearningEvent(
        event_id="event-1",
        tenant_id="tenant-a",
        session_id="session-1",
        user_id="student-1",
        classroom_version_id="version-1",
        seq=1,
        event_type="scene.completed",
        occurred_at=occurred_at,
        scene_id="scene-1",
        knowledge_point_id="kp-1",
        payload={"schema_version": "1.0", "scene_id": "scene-1"},
    )

    class DatabaseSession:
        def __init__(self) -> None:
            self.scalar_results = [learning_session, existing]

        async def execute(self, _statement, _parameters=None):
            return None

        async def scalar(self, _statement):
            return self.scalar_results.pop(0)

    repository = object.__new__(repository_module.SqlAlchemyLearningEventRepository)
    repository._tenant_id = "tenant-a"
    changed = repository_module.LearningEventAppend(
        event_id="event-1",
        tenant_id="tenant-a",
        session_id="session-1",
        user_id="student-1",
        classroom_version_id="version-1",
        event_type="scene.completed",
        occurred_at=occurred_at,
        payload={"schema_version": "1.0", "scene_id": "scene-2"},
        scene_id="scene-2",
        knowledge_point_id="kp-1",
    )

    with pytest.raises(repository_module.LearningEventBindingError, match="changed"):
        await repository.append_in_session(DatabaseSession(), changed)


@pytest.mark.asyncio
async def test_accepted_event_cannot_reuse_a_quarantined_event_id() -> None:
    models = _learning_models()
    repository_module = _learning_repository()
    occurred_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    learning_session = models.LearningSession(
        id="session-1",
        tenant_id="tenant-a",
        user_id="student-1",
        classroom_version_id="version-1",
        assignment_id="assignment-1",
        student_asset_id=None,
        status="active",
        next_seq=1,
        last_cursor={"last_event_seq": 0},
    )
    quarantined = models.LearningEventQuarantine(
        event_id="event-1",
        tenant_id="tenant-a",
        session_id="session-1",
        user_id="student-1",
        classroom_version_id="version-1",
        event_type="quiz.graded",
        occurred_at=occurred_at,
        knowledge_point_id="kp-1",
        payload={"schema_version": "1.0", "assessment_id": "missing"},
        reason_code="assessment_not_in_version",
        details=None,
    )

    class DatabaseSession:
        def __init__(self) -> None:
            self.scalar_results = [learning_session, None, quarantined]

        async def execute(self, _statement, _parameters=None):
            return None

        async def scalar(self, _statement):
            return self.scalar_results.pop(0)

        def add(self, _value: object) -> None:
            return None

        async def flush(self) -> None:
            return None

    repository = object.__new__(repository_module.SqlAlchemyLearningEventRepository)
    repository._tenant_id = "tenant-a"
    event = repository_module.LearningEventAppend(
        event_id="event-1",
        tenant_id="tenant-a",
        session_id="session-1",
        user_id="student-1",
        classroom_version_id="version-1",
        event_type="scene.completed",
        occurred_at=occurred_at,
        payload={"schema_version": "1.0", "scene_id": "scene-1"},
        scene_id="scene-1",
        knowledge_point_id="kp-1",
    )

    with pytest.raises(repository_module.LearningEventBindingError, match="quarantined"):
        await repository.append_in_session(DatabaseSession(), event)
