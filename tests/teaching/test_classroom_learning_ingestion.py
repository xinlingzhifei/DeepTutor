from __future__ import annotations

from types import SimpleNamespace

import pytest

from deeptutor.teaching.learning_events import LearningEventBatch
from deeptutor.teaching.models import LearningEvent, LearningEventQuarantine
from deeptutor.teaching.repositories.learning_events import (
    LearningEventAppendResult,
    LearningEventBindingError,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext


def _context() -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name=tenant_schema_name("tenant-a"),
        user_id="student-a",
        permissions=frozenset(),
    )


def _document() -> object:
    return SimpleNamespace(
        openmaic=SimpleNamespace(
            scenes=[
                SimpleNamespace(
                    id="scene-quiz",
                    type="quiz",
                    content=SimpleNamespace(
                        questions=[SimpleNamespace(id="question-1")],
                    ),
                )
            ]
        ),
        knowledge_point_mappings=[
            SimpleNamespace(
                knowledge_point_id="kp-quiz",
                scene_ids=["scene-quiz"],
            )
        ],
    )


@pytest.mark.asyncio
async def test_ingestion_uses_ticket_claims_and_returns_per_item_outcomes(monkeypatch) -> None:
    from deeptutor.teaching.services import classroom_learning

    context = _context()
    appended: list[object] = []

    class DatabaseSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.flush_count = 0

        def add(self, value: object) -> None:
            self.added.append(value)

        async def execute(self, _statement, _parameters=None) -> None:
            return None

        async def scalar(self, statement):
            entity = statement.column_descriptions[0].get("entity")
            if entity is LearningEvent:
                return None
            if entity is LearningEventQuarantine:
                return next(
                    (value for value in self.added if isinstance(value, LearningEventQuarantine)),
                    None,
                )
            return None

        async def flush(self) -> None:
            self.flush_count += 1

    database_session = DatabaseSession()

    class Sessions:
        async def get(self, get_context, *, session_id):
            assert (get_context, session_id) == (context, "session-a")
            return SimpleNamespace(classroom_version_id="version-a")

        async def consume_event_ticket(
            self,
            consume_context,
            *,
            session_id,
            token,
            protected_action,
        ):
            assert (consume_context, session_id, token) == (
                context,
                "session-a",
                "event-ticket",
            )
            claims = SimpleNamespace(
                tenant_id="tenant-a",
                user_id="student-a",
                session_id="session-a",
                classroom_version_id="version-a",
            )
            return await protected_action(database_session, claims)

    class Loader:
        async def load_version_document(self, load_context, version_id):
            assert (load_context, version_id) == (context, "version-a")
            return _document()

    class Repository:
        async def append_in_session(self, received_session, event):
            assert received_session is database_session
            appended.append(event)
            outcome = "accepted" if len(appended) == 1 else "duplicate"
            return LearningEventAppendResult(
                event_id=event.event_id,
                outcome=outcome,
                seq=1,
            )

    monkeypatch.setattr(
        classroom_learning,
        "SqlAlchemyLearningEventRepository",
        lambda _engine, tenant_id: (
            Repository() if tenant_id == "tenant-a" else pytest.fail("wrong tenant")
        ),
    )
    service = classroom_learning.ClassroomLearningEventIngestionService(
        engine=object(),
        sessions=Sessions(),
        document_loader=Loader(),
    )
    event = {
        "schema_version": "1.0",
        "event_id": "event-1",
        "event_type": "scene.completed",
        "occurred_at": "2026-08-10T12:00:00Z",
        "scene_id": "scene-quiz",
        "knowledge_point_id": "kp-quiz",
    }
    quarantined = {
        "schema_version": "1.0",
        "event_id": "event-q",
        "event_type": "quiz.graded",
        "occurred_at": "2026-08-10T12:00:01Z",
        "scene_id": "scene-quiz",
        "knowledge_point_id": "kp-quiz",
        "assessment_id": "missing",
        "question_id": "question-1",
        "answer": ["option-a"],
    }
    batch = LearningEventBatch.model_validate({"events": [event, event, quarantined, quarantined]})

    result = await service.ingest(
        context,
        session_id="session-a",
        token="event-ticket",
        batch=batch,
    )

    assert [(item.event_id, item.seq) for item in result.accepted] == [("event-1", 1)]
    assert [(item.event_id, item.seq) for item in result.duplicate] == [("event-1", 1)]
    assert [(item.event_id, item.reason) for item in result.quarantined] == [
        ("event-q", "assessment_not_in_version"),
        ("event-q", "assessment_not_in_version"),
    ]
    assert all(
        (
            event.tenant_id,
            event.user_id,
            event.session_id,
            event.classroom_version_id,
        )
        == ("tenant-a", "student-a", "session-a", "version-a")
        for event in appended
    )
    assert len(database_session.added) == 1
    assert database_session.flush_count == 1


@pytest.mark.asyncio
async def test_quarantine_rejects_changed_facts_for_the_same_event_id() -> None:
    from deeptutor.teaching.services.classroom_learning import (
        ClassroomLearningEventIngestionService,
    )

    class DatabaseSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, value: object) -> None:
            self.added.append(value)

        async def execute(self, _statement, _parameters=None) -> None:
            return None

        async def scalar(self, statement):
            entity = statement.column_descriptions[0].get("entity")
            if entity is LearningEvent:
                return None
            if entity is LearningEventQuarantine:
                return next(
                    (value for value in self.added if isinstance(value, LearningEventQuarantine)),
                    None,
                )
            return None

        async def flush(self) -> None:
            return None

    batch = LearningEventBatch.model_validate(
        {
            "events": [
                {
                    "schema_version": "1.0",
                    "event_id": "event-q",
                    "event_type": "quiz.graded",
                    "occurred_at": "2026-08-10T12:00:01Z",
                    "scene_id": "scene-quiz",
                    "knowledge_point_id": "kp-quiz",
                    "assessment_id": "missing",
                    "question_id": "question-1",
                    "answer": ["option-a"],
                },
                {
                    "schema_version": "1.0",
                    "event_id": "event-q",
                    "event_type": "quiz.graded",
                    "occurred_at": "2026-08-10T12:00:01Z",
                    "scene_id": "scene-quiz",
                    "knowledge_point_id": "kp-quiz",
                    "assessment_id": "missing",
                    "question_id": "question-1",
                    "answer": ["option-b"],
                },
            ]
        }
    )
    claims = SimpleNamespace(
        tenant_id="tenant-a",
        user_id="student-a",
        session_id="session-a",
        classroom_version_id="version-a",
    )
    database_session = DatabaseSession()

    await ClassroomLearningEventIngestionService._quarantine(
        database_session,
        claims=claims,
        event=batch.events[0],
        reason="assessment_not_in_version",
    )

    with pytest.raises(LearningEventBindingError, match="changed event facts"):
        await ClassroomLearningEventIngestionService._quarantine(
            database_session,
            claims=claims,
            event=batch.events[1],
            reason="assessment_not_in_version",
        )

    assert len(database_session.added) == 1
