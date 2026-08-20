"""Acceptance coverage for the complete classroom learning evidence loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from deeptutor.services.config import PlatformSettings
from deeptutor.services.path_service import PathService
from deeptutor.teaching.learning_events import LearningEventBatch
from deeptutor.teaching.projector_worker import LearningProjectionWorker
from deeptutor.teaching.projectors.memory import ClassroomMemoryProjector
from deeptutor.teaching.services.classroom_learning import (
    ClassroomLearningEventIngestionService,
)
from deeptutor.teaching.services.learning_sessions import LearningSessionService
from deeptutor.teaching.services.reports import SqlAlchemyTeachingReportRepository
from deeptutor.teaching.tenant_context import TenantContext
from deeptutor.teaching.tickets import (
    ClassroomTicketService,
    TicketExpired,
    TicketReplay,
    TicketScopeError,
)
from tests.generation_database_plugin import generation_database as generation_database
from tests.teaching.integration.test_learning_projector import (
    ProjectorDatabase,
    projector_database,
)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _document() -> SimpleNamespace:
    return SimpleNamespace(
        openmaic=SimpleNamespace(
            scenes=[
                SimpleNamespace(id="scene-1", type="slide", content=SimpleNamespace()),
                SimpleNamespace(
                    id="quiz-scene",
                    type="quiz",
                    content=SimpleNamespace(
                        questions=[
                            SimpleNamespace(
                                id="question-1",
                                question_type="single_choice",
                                options=[
                                    SimpleNamespace(id="option-a"),
                                    SimpleNamespace(id="option-b"),
                                ],
                                correct_option_ids=["option-a"],
                            )
                        ]
                    ),
                ),
                SimpleNamespace(
                    id="pbl-scene",
                    type="pbl",
                    content=SimpleNamespace(
                        milestones=[SimpleNamespace(id="milestone-1")],
                    ),
                ),
            ]
        ),
        knowledge_point_mappings=[
            SimpleNamespace(
                knowledge_point_id="kp-1",
                scene_ids=["quiz-scene"],
            )
        ],
    )


def _normal_events(now: datetime) -> list[dict[str, object]]:
    common = {"schema_version": "1.0"}
    return [
        {
            **common,
            "event_id": "loop-start",
            "event_type": "classroom.started",
            "occurred_at": now,
        },
        {
            **common,
            "event_id": "loop-scene",
            "event_type": "scene.completed",
            "occurred_at": now + timedelta(milliseconds=1),
            "scene_id": "scene-1",
        },
        {
            **common,
            "event_id": "loop-quiz",
            "event_type": "quiz.graded",
            "occurred_at": now + timedelta(milliseconds=2),
            "scene_id": "quiz-scene",
            "knowledge_point_id": "kp-1",
            "assessment_id": "quiz-scene",
            "question_id": "question-1",
            "answer": ["option-a"],
        },
        {
            **common,
            "event_id": "loop-hint",
            "event_type": "hint.used",
            "occurred_at": now + timedelta(milliseconds=3),
            "scene_id": "quiz-scene",
            "knowledge_point_id": "kp-1",
            "hint_id": "hint-1",
        },
        {
            **common,
            "event_id": "loop-pbl",
            "event_type": "pbl.milestone_completed",
            "occurred_at": now + timedelta(milliseconds=4),
            "scene_id": "pbl-scene",
            "milestone_id": "milestone-1",
        },
        {
            **common,
            "event_id": "loop-complete",
            "event_type": "classroom.completed",
            "occurred_at": now + timedelta(milliseconds=5),
        },
    ]


@pytest.mark.asyncio
async def test_classroom_learning_loop_is_private_idempotent_and_visible_within_sixty_seconds(
    projector_database: ProjectorDatabase,  # noqa: F811 - imported pytest fixture
    tmp_path,
) -> None:
    learner_id = "student-loop"
    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.enrollments (class_id, learner_id, status) "
                "VALUES ('class-1', :learner_id, 'active')"
            ),
            {"learner_id": learner_id},
        )

    clock = _Clock(datetime.now(UTC))
    secret_file = tmp_path / "classroom-ticket-secret"
    secret_file.write_text("classroom-loop-secret-" + "a" * 48, encoding="utf-8")
    tickets = ClassroomTicketService.from_settings(
        PlatformSettings(classroom_ticket_secret_file=secret_file),
        clock=clock,
    )
    sessions = LearningSessionService(
        engine=projector_database.engine,
        ticket_service=tickets,
    )
    context = TenantContext(
        tenant_id=projector_database.tenant_id,
        schema_name=projector_database.schema_name,
        user_id=learner_id,
        permissions=frozenset(),
    )
    document = _document()

    class IngestionDocuments:
        async def load_version_document(self, load_context, version_id):
            assert load_context == context
            assert version_id == "version-1"
            return document

    ingestion = ClassroomLearningEventIngestionService(
        engine=projector_database.engine,
        sessions=sessions,
        document_loader=IngestionDocuments(),
    )
    learning_session = await sessions.create(context, assignment_id="assignment-1")
    assert learning_session.classroom_version_id == "version-1"
    assert learning_session.user_id == learner_id

    events = _normal_events(clock.value)
    normal_batch = LearningEventBatch.model_validate({"events": events})
    normal_token = await sessions.issue_event_ticket(
        context,
        session_id=learning_session.id,
    )
    normal = await ingestion.ingest(
        context,
        session_id=learning_session.id,
        token=normal_token,
        batch=normal_batch,
    )
    assert [row.event_id for row in normal.accepted] == [str(event["event_id"]) for event in events]
    assert normal.duplicate == ()
    assert normal.quarantined == ()

    with pytest.raises(TicketReplay):
        await ingestion.ingest(
            context,
            session_id=learning_session.id,
            token=normal_token,
            batch=normal_batch,
        )

    duplicate_token = await sessions.issue_event_ticket(
        context,
        session_id=learning_session.id,
    )
    duplicate = await ingestion.ingest(
        context,
        session_id=learning_session.id,
        token=duplicate_token,
        batch=LearningEventBatch.model_validate({"events": [events[2]]}),
    )
    assert duplicate.accepted == ()
    assert [row.event_id for row in duplicate.duplicate] == ["loop-quiz"]

    bad_binding_token = await sessions.issue_event_ticket(
        context,
        session_id=learning_session.id,
    )
    bad_bindings = await ingestion.ingest(
        context,
        session_id=learning_session.id,
        token=bad_binding_token,
        batch=LearningEventBatch.model_validate(
            {
                "events": [
                    {
                        "schema_version": "1.0",
                        "event_id": "loop-bad-scene",
                        "event_type": "scene.completed",
                        "occurred_at": clock.value + timedelta(milliseconds=6),
                        "scene_id": "scene-from-another-version",
                    },
                    {
                        "schema_version": "1.0",
                        "event_id": "loop-bad-kp",
                        "event_type": "hint.used",
                        "occurred_at": clock.value + timedelta(milliseconds=7),
                        "scene_id": "quiz-scene",
                        "knowledge_point_id": "kp-from-another-scene",
                        "hint_id": "hint-2",
                    },
                ]
            }
        ),
    )
    assert [(row.event_id, row.reason) for row in bad_bindings.quarantined] == [
        ("loop-bad-scene", "scene_not_in_version"),
        ("loop-bad-kp", "knowledge_point_not_in_scene"),
    ]

    wrong_version_token = tickets.issue(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=learning_session.id,
        classroom_version_id="version-from-another-session",
        allowed_action="learning_event.append",
        ttl_seconds=300,
    )
    with pytest.raises(TicketScopeError):
        await ingestion.ingest(
            context,
            session_id=learning_session.id,
            token=wrong_version_token,
            batch=LearningEventBatch.model_validate({"events": [events[3]]}),
        )

    expired_token = await sessions.issue_event_ticket(
        context,
        session_id=learning_session.id,
    )
    clock.value += timedelta(seconds=301)
    with pytest.raises(TicketExpired):
        await ingestion.ingest(
            context,
            session_id=learning_session.id,
            token=expired_token,
            batch=LearningEventBatch.model_validate({"events": [events[3]]}),
        )

    completed = await sessions.complete(context, session_id=learning_session.id)
    assert completed.status == "completed"

    target = PathService(workspace_root=tmp_path / learner_id)

    class ProjectionTargets:
        def path_service_for_user(self, user_id: str):
            assert user_id == learner_id
            return target

    class ProjectionDocuments:
        async def load_version_document(self, tenant_id: str, version_id: str):
            assert tenant_id == projector_database.tenant_id
            assert version_id == "version-1"
            return document

    worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=ProjectionDocuments(),
        worker_id="learning-loop-projector",
        memory_projector=ClassroomMemoryProjector(),
        memory_targets=ProjectionTargets(),
    )
    projection_started = datetime.now(UTC)
    projected = 0
    while await worker.run_once(tenant_id=projector_database.tenant_id):
        projected += 1
    projection_elapsed = (datetime.now(UTC) - projection_started).total_seconds()
    assert projected == 6
    assert projection_elapsed < 60

    report = await SqlAlchemyTeachingReportRepository(projector_database.engine).class_report(
        projector_database.tenant_id,
        "class-1",
        learner_id,
    )
    assert report.session_count == 1
    assert report.completed_count == 1
    assert report.completion_rate == 1.0
    assert report.completed_scene_count == 1
    assert (report.valid_quiz_count, report.correct_quiz_count) == (1, 1)
    assert (report.hint_count, report.pbl_milestone_count) == (1, 1)
    assert report.mastery == ({"knowledge_point_id": "kp-1", "level": 0.5, "evidence_count": 1},)

    async with projector_database.engine.connect() as connection:
        event_count = await connection.scalar(
            text(f"SELECT count(*) FROM {quoted}.learning_events WHERE session_id=:session_id"),
            {"session_id": learning_session.id},
        )
        completed_queue_count = await connection.scalar(
            text(
                f"SELECT count(*) FROM {quoted}.learning_projection_queue "
                "WHERE session_id=:session_id AND status='completed'"
            ),
            {"session_id": learning_session.id},
        )
        quarantine_count = await connection.scalar(
            text(
                f"SELECT count(*) FROM {quoted}.learning_event_quarantine "
                "WHERE session_id=:session_id"
            ),
            {"session_id": learning_session.id},
        )
        evidence_count = await connection.scalar(
            text(f"SELECT count(*) FROM {quoted}.mastery_evidence WHERE session_id=:session_id"),
            {"session_id": learning_session.id},
        )
    assert (event_count, completed_queue_count, quarantine_count, evidence_count) == (
        6,
        6,
        2,
        1,
    )

    memory_files = tuple(target.get_memory_dir().rglob("*"))
    memory_text = "\n".join(
        path.read_text(encoding="utf-8") for path in memory_files if path.is_file()
    )
    assert memory_text.count('"surface":"classroom"') == 6
    assert "Status: completed" in memory_text
    assert "Completed scenes: 1" in memory_text
    assert "Valid quizzes: 1; correct: 1" in memory_text
    assert "FULL-ANSWER-SECRET" not in memory_text
