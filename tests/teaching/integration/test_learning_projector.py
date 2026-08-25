from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DropSchema

from deeptutor.learning.mastery import compute_mastery
from deeptutor.teaching.repositories.learning_events import (
    LearningEventAppend,
    SqlAlchemyLearningEventRepository,
)
from deeptutor.teaching.schema_names import tenant_schema_name


@dataclass(frozen=True, slots=True)
class ProjectorDatabase:
    engine: AsyncEngine
    tenant_id: str
    schema_name: str


async def _seed_classroom(engine: AsyncEngine, tenant_id: str, schema_name: str) -> None:
    quoted = f'"{schema_name}"'
    sha = "a" * 64
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO platform.tenants (id, name, status, data_plane_mode) "
                "VALUES (:tenant_id, 'Projector tenant', 'active', 'shared')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO platform.tenant_schema_states "
                "(tenant_id, schema_name, revision, status) "
                "VALUES (:tenant_id, :schema_name, '20260825_0020', 'active')"
            ),
            {"tenant_id": tenant_id, "schema_name": schema_name},
        )
        await connection.execute(
            text(f"INSERT INTO {quoted}.courses (id, title) VALUES ('course-1', 'Course')")
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.classes (id, course_id, name) "
                "VALUES ('class-1', 'course-1', 'Class')"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.generation_jobs ("
                "id, tenant_id, job_kind, phase, status, priority, quota_units, "
                "actor_id, owner_id, visibility, request_id, idempotency_key, "
                "resource_course_id, resource_class_id, request_sha256, "
                "data_plane_route_id, provider_profile_id, worker_pool_ref, "
                "queue_ref, request_payload, progress_percent"
                ") VALUES ("
                "'job-1', :tenant_id, 'generation', 'content', 'succeeded', 0, 1, "
                "'teacher-1', 'teacher-1', 'class', 'request-1', 'idempotency-1', "
                "'course-1', 'class-1', :sha, 'route-1', 'provider-1', "
                "'workers-1', 'queue-1', '{}', 100)"
            ),
            {"tenant_id": tenant_id, "sha": sha},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.classroom_assets "
                "(id, tenant_id, owner_id, title, lifecycle_state) "
                "VALUES ('asset-1', :tenant_id, 'teacher-1', 'Classroom', 'published')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.classroom_versions ("
                "id, tenant_id, classroom_id, version_number, generation_job_id, "
                "source_version_id, document_sha256, media_manifest_sha256, "
                "document_object_key"
                ") VALUES ("
                "'source-version-1', :tenant_id, 'asset-1', 1, 'job-1', NULL, :sha, :sha, "
                "'tenants/example/classrooms/asset-1/source-version-1/classroom.json'), ("
                "'version-1', :tenant_id, 'asset-1', 2, NULL, 'source-version-1', :sha, :sha, "
                "'tenants/example/classrooms/asset-1/version-1/classroom.json')"
            ),
            {"tenant_id": tenant_id, "sha": sha},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.assignments "
                "(id, tenant_id, classroom_version_id, class_id, assigned_by) "
                "VALUES ('assignment-1', :tenant_id, 'version-1', 'class-1', 'teacher-1')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.learning_sessions "
                "(id, tenant_id, user_id, classroom_version_id, assignment_id, status) "
                "VALUES ('session-1', :tenant_id, 'student-1', 'version-1', "
                "'assignment-1', 'active')"
            ),
            {"tenant_id": tenant_id},
        )


@pytest_asyncio.fixture
async def projector_database(generation_database) -> ProjectorDatabase:
    tenant_id = f"projector-{uuid.uuid4().hex[:12]}"
    schema_name = tenant_schema_name(tenant_id)
    generation_database.migrate_tenant(tenant_id)
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    await _seed_classroom(engine, tenant_id, schema_name)
    try:
        yield ProjectorDatabase(engine=engine, tenant_id=tenant_id, schema_name=schema_name)
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema_name, cascade=True))
            await connection.execute(
                text("DELETE FROM platform.tenant_schema_states WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text("DELETE FROM platform.tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()


def _document():
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
            ]
        ),
        knowledge_point_mappings=[
            SimpleNamespace(knowledge_point_id="kp-1", scene_ids=["quiz-scene"])
        ],
    )


async def _append(
    database: ProjectorDatabase,
    *,
    event_id: str,
    event_type: str,
    scene_id: str | None = None,
    knowledge_point_id: str | None = None,
    payload: dict[str, object] | None = None,
    session_id: str = "session-1",
) -> None:
    repository = SqlAlchemyLearningEventRepository(database.engine, database.tenant_id)
    await repository.append(
        LearningEventAppend(
            event_id=event_id,
            tenant_id=database.tenant_id,
            session_id=session_id,
            user_id="student-1",
            classroom_version_id="version-1",
            event_type=event_type,
            occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            scene_id=scene_id,
            knowledge_point_id=knowledge_point_id,
            payload=payload or {"schema_version": "1.0"},
        )
    )


async def _backlog_event_ids(database: ProjectorDatabase) -> list[str]:
    async with database.engine.connect() as connection:
        return list(
            await connection.scalars(
                text(
                    "SELECT event_id FROM platform.teaching_learning_projection_backlog "
                    "WHERE tenant_id = :tenant_id ORDER BY event_id"
                ),
                {"tenant_id": database.tenant_id},
            )
        )


def _pbl_document():
    return SimpleNamespace(
        classroom_version_id="source-version-1",
        openmaic=SimpleNamespace(
            scenes=[
                SimpleNamespace(
                    id="pbl-scene",
                    type="pbl",
                    content=SimpleNamespace(
                        milestones=[
                            SimpleNamespace(
                                id="milestone-1",
                                rubric="  Explain the project evidence.  ",
                            )
                        ]
                    ),
                )
            ]
        ),
        knowledge_point_mappings=[
            SimpleNamespace(knowledge_point_id="kp-1", scene_ids=["pbl-scene"])
        ],
    )


def _teacher_context(database: ProjectorDatabase):
    from deeptutor.teaching.permissions import ScopedPermission
    from deeptutor.teaching.tenant_context import TenantContext

    return TenantContext(
        tenant_id=database.tenant_id,
        schema_name=database.schema_name,
        user_id="teacher-1",
        permissions=frozenset(
            {
                ScopedPermission(
                    permission="learning_event.grade",
                    scope_type="class",
                    scope_id="class-1",
                    tenant_id=database.tenant_id,
                )
            }
        ),
    )


class _PblDocuments:
    async def load_version_document(self, context, version_id: str):
        assert version_id == "version-1"
        assert context.tenant_id
        return _pbl_document()


async def _append_pbl(database: ProjectorDatabase, event_id: str) -> None:
    await _append(
        database,
        event_id=event_id,
        event_type="pbl.milestone_completed",
        scene_id="pbl-scene",
        knowledge_point_id="kp-1",
        payload={"milestone_id": "milestone-1"},
    )


async def _grade_pbl(
    database: ProjectorDatabase,
    *,
    event_id: str,
    idempotency_key: str,
    passed: bool,
    score: float | None = None,
    source_reference: str = "review-1",
):
    from deeptutor.teaching.repositories.pbl_grading import (
        SqlAlchemyPblGradingRepository,
    )
    from deeptutor.teaching.services.pbl_grading import PblGradingCommand

    return await SqlAlchemyPblGradingRepository(database.engine).record(
        _teacher_context(database),
        session_id="session-1",
        command=PblGradingCommand(
            event_id=event_id,
            passed=passed,
            score=score,
            source_reference=source_reference,
            idempotency_key=idempotency_key,
        ),
        documents=_PblDocuments(),
    )


@pytest.mark.asyncio
async def test_postgres_pbl_grading_idempotency_and_first_terminal_result_wins(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.services.pbl_grading import PblGradingConflict

    await _append_pbl(projector_database, "event-pbl-idempotent")
    first = await _grade_pbl(
        projector_database,
        event_id="event-pbl-idempotent",
        idempotency_key="grade-key-1",
        passed=True,
        score=0.7,
    )
    same_event_new_key = await _grade_pbl(
        projector_database,
        event_id="event-pbl-idempotent",
        idempotency_key="grade-key-2",
        passed=True,
        score=0.7,
    )
    assert same_event_new_key.result_id == first.result_id

    await _append_pbl(projector_database, "event-pbl-alias-conflict")
    with pytest.raises(PblGradingConflict):
        await _grade_pbl(
            projector_database,
            event_id="event-pbl-alias-conflict",
            idempotency_key="grade-key-2",
            passed=True,
            score=0.7,
        )
    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.connect() as connection:
        aliases = (
            await connection.execute(
                text(
                    f"SELECT idempotency_key, result_id, event_id, request_sha256 FROM "
                    f"{quoted}.pbl_grading_idempotency_keys "
                    "WHERE idempotency_key IN ('grade-key-1', 'grade-key-2') "
                    "ORDER BY idempotency_key"
                )
            )
        ).all()
    assert aliases == [
        ("grade-key-1", first.result_id, "event-pbl-idempotent", first.request_sha256),
        ("grade-key-2", first.result_id, "event-pbl-idempotent", first.request_sha256),
    ]

    with pytest.raises(PblGradingConflict):
        await _grade_pbl(
            projector_database,
            event_id="event-pbl-idempotent",
            idempotency_key="grade-key-1",
            passed=False,
            score=0.7,
        )
    with pytest.raises(PblGradingConflict):
        await _grade_pbl(
            projector_database,
            event_id="event-pbl-idempotent",
            idempotency_key="grade-key-3",
            passed=True,
            score=0.7,
            source_reference="review-variant",
        )


@pytest.mark.asyncio
async def test_postgres_concurrent_pbl_grading_has_one_terminal_winner(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.services.pbl_grading import (
        PblGradingConflict,
        PblGradingRecord,
    )

    await _append_pbl(projector_database, "event-pbl-race")
    outcomes = await asyncio.gather(
        _grade_pbl(
            projector_database,
            event_id="event-pbl-race",
            idempotency_key="grade-race-pass",
            passed=True,
        ),
        _grade_pbl(
            projector_database,
            event_id="event-pbl-race",
            idempotency_key="grade-race-fail",
            passed=False,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, PblGradingRecord) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, PblGradingConflict) for outcome in outcomes) == 1
    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    f"SELECT count(*) FROM {quoted}.pbl_grading_results "
                    "WHERE event_id = 'event-pbl-race'"
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_postgres_late_pbl_grade_requeues_completed_and_failed_safely(
    projector_database: ProjectorDatabase,
) -> None:
    quoted = f'"{projector_database.schema_name}"'
    await _append_pbl(projector_database, "event-pbl-completed")
    await _append_pbl(projector_database, "event-pbl-failed")
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue SET status = 'completed' "
                "WHERE event_id = 'event-pbl-completed'"
            )
        )
        await connection.execute(
            text(
                "DELETE FROM platform.teaching_learning_projection_backlog "
                "WHERE tenant_id = :tenant_id AND event_id = 'event-pbl-completed'"
            ),
            {"tenant_id": projector_database.tenant_id},
        )
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue SET status = 'failed', "
                "available_at = clock_timestamp() + interval '1 day', "
                "last_error_code = 'transient_timeout' "
                "WHERE event_id = 'event-pbl-failed'"
            )
        )

    await _grade_pbl(
        projector_database,
        event_id="event-pbl-completed",
        idempotency_key="grade-late-completed",
        passed=True,
    )
    await _grade_pbl(
        projector_database,
        event_id="event-pbl-failed",
        idempotency_key="grade-late-failed",
        passed=False,
    )

    async with projector_database.engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    f"SELECT event_id, status, available_at <= clock_timestamp(), "
                    f"last_error_code FROM {quoted}.learning_projection_queue "
                    "WHERE event_id IN ('event-pbl-completed', 'event-pbl-failed') "
                    "ORDER BY event_id"
                )
            )
        ).all()
        backlog = await connection.scalar(
            text(
                "SELECT count(*) FROM platform.teaching_learning_projection_backlog "
                "WHERE tenant_id = :tenant_id AND event_id IN "
                "('event-pbl-completed', 'event-pbl-failed')"
            ),
            {"tenant_id": projector_database.tenant_id},
        )
    assert rows == [
        ("event-pbl-completed", "pending", True, None),
        ("event-pbl-failed", "pending", True, None),
    ]
    assert backlog == 2


@pytest.mark.asyncio
async def test_postgres_pbl_grading_rolls_back_if_backlog_restore_conflicts(
    projector_database: ProjectorDatabase,
) -> None:
    from sqlalchemy.exc import IntegrityError

    quoted = f'"{projector_database.schema_name}"'
    await _append_pbl(projector_database, "event-pbl-rollback")
    stale_received_at = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue SET status = 'completed' "
                "WHERE event_id = 'event-pbl-rollback'"
            )
        )
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO platform.teaching_learning_projection_backlog "
                "(tenant_id, event_id, received_at) VALUES "
                "(:tenant_id, 'event-pbl-rollback', :received_at)"
            ),
            {
                "tenant_id": projector_database.tenant_id,
                "received_at": stale_received_at,
            },
        )

    with pytest.raises(IntegrityError) as caught:
        await _grade_pbl(
            projector_database,
            event_id="event-pbl-rollback",
            idempotency_key="grade-rollback",
            passed=True,
        )
    database_error = caught.value.orig
    assert getattr(database_error, "sqlstate", None) == "23505"
    assert "pk_teaching_learning_projection_backlog" in str(database_error)

    async with projector_database.engine.connect() as connection:
        queue_status = await connection.scalar(
            text(
                f"SELECT status FROM {quoted}.learning_projection_queue "
                "WHERE event_id = 'event-pbl-rollback'"
            )
        )
        result_count = await connection.scalar(
            text(
                f"SELECT count(*) FROM {quoted}.pbl_grading_results "
                "WHERE event_id = 'event-pbl-rollback'"
            )
        )
        backlog = (
            await connection.execute(
                text(
                    "SELECT tenant_id, event_id, received_at FROM "
                    "platform.teaching_learning_projection_backlog "
                    "WHERE tenant_id = :tenant_id AND event_id = 'event-pbl-rollback'"
                ),
                {"tenant_id": projector_database.tenant_id},
            )
        ).one()
    assert queue_status == "completed"
    assert result_count == 0
    assert backlog == (
        projector_database.tenant_id,
        "event-pbl-rollback",
        stale_received_at,
    )


@pytest.mark.asyncio
async def test_postgres_pbl_grading_never_revives_quarantined_event(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.services.pbl_grading import PblGradingConflict

    quoted = f'"{projector_database.schema_name}"'
    await _append_pbl(projector_database, "event-pbl-quarantined")
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue SET status = 'quarantined' "
                "WHERE event_id = 'event-pbl-quarantined'"
            )
        )
        await connection.execute(
            text(
                "DELETE FROM platform.teaching_learning_projection_backlog "
                "WHERE tenant_id = :tenant_id AND event_id = 'event-pbl-quarantined'"
            ),
            {"tenant_id": projector_database.tenant_id},
        )

    with pytest.raises(PblGradingConflict):
        await _grade_pbl(
            projector_database,
            event_id="event-pbl-quarantined",
            idempotency_key="grade-quarantined",
            passed=True,
        )
    async with projector_database.engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    f"SELECT status FROM {quoted}.learning_projection_queue "
                    "WHERE event_id = 'event-pbl-quarantined'"
                )
            )
            == "quarantined"
        )
        assert (
            await connection.scalar(
                text(
                    f"SELECT count(*) FROM {quoted}.pbl_grading_results "
                    "WHERE event_id = 'event-pbl-quarantined'"
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_postgres_identical_pbl_replay_survives_later_quarantine_without_queue_mutation(
    projector_database: ProjectorDatabase,
) -> None:
    quoted = f'"{projector_database.schema_name}"'
    await _append_pbl(projector_database, "event-pbl-quarantined-replay")
    first = await _grade_pbl(
        projector_database,
        event_id="event-pbl-quarantined-replay",
        idempotency_key="grade-quarantine-replay",
        passed=True,
    )
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue SET status = 'quarantined' "
                "WHERE event_id = 'event-pbl-quarantined-replay'"
            )
        )

    replay = await _grade_pbl(
        projector_database,
        event_id="event-pbl-quarantined-replay",
        idempotency_key="grade-quarantine-replay",
        passed=True,
    )

    async with projector_database.engine.connect() as connection:
        queue_status = await connection.scalar(
            text(
                f"SELECT status FROM {quoted}.learning_projection_queue "
                "WHERE event_id = 'event-pbl-quarantined-replay'"
            )
        )
        backlog_count = await connection.scalar(
            text(
                "SELECT count(*) FROM platform.teaching_learning_projection_backlog "
                "WHERE tenant_id = :tenant_id "
                "AND event_id = 'event-pbl-quarantined-replay'"
            ),
            {"tenant_id": projector_database.tenant_id},
        )
    assert replay.result_id == first.result_id
    assert queue_status == "quarantined"
    assert backlog_count == 0


@pytest.mark.asyncio
async def test_postgres_pbl_grading_migration_constraints_are_exact(
    projector_database: ProjectorDatabase,
) -> None:
    async with projector_database.engine.connect() as connection:
        constraints = dict(
            (
                await connection.execute(
                    text(
                        "SELECT constraint_name, pg_get_constraintdef(pg_constraint.oid) "
                        "FROM information_schema.table_constraints JOIN pg_constraint "
                        "ON pg_constraint.conname = constraint_name "
                        "WHERE table_schema = :schema_name "
                        "AND table_name = 'pbl_grading_results'"
                    ),
                    {"schema_name": projector_database.schema_name},
                )
            ).all()
        )
        alias_constraints = dict(
            (
                await connection.execute(
                    text(
                        "SELECT constraint_name, pg_get_constraintdef(pg_constraint.oid) "
                        "FROM information_schema.table_constraints JOIN pg_constraint "
                        "ON pg_constraint.conname = constraint_name "
                        "WHERE table_schema = :schema_name "
                        "AND table_name = 'pbl_grading_idempotency_keys'"
                    ),
                    {"schema_name": projector_database.schema_name},
                )
            ).all()
        )
    assert "UNIQUE (event_id)" in constraints["uq_pbl_grading_results_event_id"]
    assert (
        "UNIQUE (tenant_id, idempotency_key)"
        in constraints["uq_pbl_grading_results_tenant_idempotency"]
    )
    assert "grading_source" in constraints["ck_pbl_grading_results_grading_source"]
    assert "score" in constraints["ck_pbl_grading_results_score"]
    assert (
        "FOREIGN KEY (event_id)" in constraints["fk_pbl_grading_results_event_id_learning_events"]
    )
    assert (
        "FOREIGN KEY (document_version_id)"
        in constraints["fk_pbl_grading_results_document_version"]
    )
    assert (
        "UNIQUE (id, tenant_id, event_id, request_sha256)"
        in constraints["uq_pbl_grading_results_alias_binding"]
    )
    assert (
        "PRIMARY KEY (tenant_id, idempotency_key)"
        in alias_constraints["pk_pbl_grading_idempotency_keys"]
    )
    assert (
        "FOREIGN KEY (result_id, tenant_id, event_id, request_sha256)"
        in alias_constraints["fk_pbl_grading_idempotency_keys_result"]
    )


@pytest.mark.asyncio
async def test_postgres_ungraded_pbl_projects_progress_without_loading_document(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker

    class Documents:
        async def load_version_document(self, tenant_id: str, version_id: str):
            raise AssertionError("ungraded PBL must not load a classroom document")

    await _append_pbl(projector_database, "event-pbl-progress-only")
    worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=Documents(),
        worker_id="projector-pbl-progress",
    )

    assert await worker.run_once(tenant_id=projector_database.tenant_id) is True

    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.connect() as connection:
        state = (
            await connection.execute(
                text(
                    f"SELECT queue.status, progress.last_event_id FROM "
                    f"{quoted}.learning_projection_queue AS queue JOIN "
                    f"{quoted}.learning_progress AS progress "
                    "ON progress.session_id = queue.session_id "
                    "WHERE queue.event_id = 'event-pbl-progress-only'"
                )
            )
        ).one()
        evidence_count = await connection.scalar(
            text(
                f"SELECT count(*) FROM {quoted}.mastery_evidence "
                "WHERE event_id = 'event-pbl-progress-only'"
            )
        )
    assert state == ("completed", "event-pbl-progress-only")
    assert evidence_count == 0


@pytest.mark.asyncio
async def test_postgres_graded_pbl_uses_published_source_lineage_for_mastery(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker

    class Documents:
        def __init__(self) -> None:
            self.loads: list[tuple[str, str]] = []

        async def load_version_document(self, tenant_id: str, version_id: str):
            self.loads.append((tenant_id, version_id))
            return _pbl_document()

    await _append_pbl(projector_database, "event-pbl-source-lineage")
    result = await _grade_pbl(
        projector_database,
        event_id="event-pbl-source-lineage",
        idempotency_key="grade-source-lineage",
        passed=True,
    )
    documents = Documents()
    worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=documents,
        worker_id="projector-pbl-source",
    )

    assert await worker.run_once(tenant_id=projector_database.tenant_id) is True

    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.connect() as connection:
        lineage = (
            await connection.execute(
                text(
                    f"SELECT classroom_version_id, document_version_id FROM "
                    f"{quoted}.pbl_grading_results WHERE id = :result_id"
                ),
                {"result_id": result.result_id},
            )
        ).one()
        evidence = (
            await connection.execute(
                text(
                    f"SELECT evidence_type, correctness FROM {quoted}.mastery_evidence "
                    "WHERE event_id = 'event-pbl-source-lineage'"
                )
            )
        ).one()
    assert documents.loads == [(projector_database.tenant_id, "version-1")]
    assert lineage == ("version-1", "source-version-1")
    assert evidence == ("pbl", True)


@pytest.mark.asyncio
async def test_postgres_memory_projection_closes_late_pbl_grade_race_on_same_maxed_claim(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker

    class Documents:
        def __init__(self) -> None:
            self.loads: list[tuple[str, str]] = []

        async def load_version_document(self, tenant_id: str, version_id: str):
            self.loads.append((tenant_id, version_id))
            return _pbl_document()

    class Memory:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.aggregates: list[object] = []

        async def project(self, event, *, aggregate, target_path_service):
            assert event.event_id == "event-pbl-memory-race"
            assert target_path_service == "target-student-1"
            self.aggregates.append(aggregate)
            if len(self.aggregates) == 1:
                self.started.set()
                await self.release.wait()

    class Targets:
        def path_service_for_user(self, user_id: str):
            return f"target-{user_id}"

    await _append_pbl(projector_database, "event-pbl-memory-race")
    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue SET max_attempts = 1 "
                "WHERE event_id = 'event-pbl-memory-race'"
            )
        )

    documents = Documents()
    memory = Memory()
    worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=documents,
        worker_id="projector-pbl-memory-race",
        memory_projector=memory,
        memory_targets=Targets(),
    )
    work = asyncio.create_task(worker.run_once(tenant_id=projector_database.tenant_id))
    try:
        await asyncio.wait_for(memory.started.wait(), timeout=5)
        await asyncio.wait_for(
            _grade_pbl(
                projector_database,
                event_id="event-pbl-memory-race",
                idempotency_key="grade-memory-race",
                passed=False,
            ),
            timeout=5,
        )
    finally:
        memory.release.set()
    assert await asyncio.wait_for(work, timeout=5) is True

    async with projector_database.engine.connect() as connection:
        queue = (
            await connection.execute(
                text(
                    f"SELECT status, attempt_count, max_attempts, last_error_code "
                    f"FROM {quoted}.learning_projection_queue "
                    "WHERE event_id = 'event-pbl-memory-race'"
                )
            )
        ).one()
        evidence = (
            await connection.execute(
                text(
                    f"SELECT evidence_type, correctness FROM {quoted}.mastery_evidence "
                    "WHERE event_id = 'event-pbl-memory-race'"
                )
            )
        ).all()
        quiz_count = await connection.scalar(
            text(
                f"SELECT count(*) FROM {quoted}.quiz_attempts "
                "WHERE event_id = 'event-pbl-memory-race'"
            )
        )
        mastery = (
            await connection.execute(
                text(
                    f"SELECT level, evidence_count FROM {quoted}.mastery_levels "
                    "WHERE user_id = 'student-1' AND knowledge_point_id = 'kp-1'"
                )
            )
        ).one()
        quarantine_count = await connection.scalar(
            text(
                f"SELECT count(*) FROM {quoted}.learning_event_quarantine "
                "WHERE event_id = 'event-pbl-memory-race'"
            )
        )

    assert tuple(queue) == ("completed", 1, 1, None)
    assert evidence == [("pbl", False)]
    assert quiz_count == 0
    assert tuple(mastery) == (compute_mastery([False]), 1)
    assert quarantine_count == 0
    assert await _backlog_event_ids(projector_database) == []
    assert documents.loads == [(projector_database.tenant_id, "version-1")]
    assert len(memory.aggregates) == 2
    assert memory.aggregates[0].difficult_knowledge_points == ()
    assert memory.aggregates[1].difficult_knowledge_points == ("kp-1",)


@pytest.mark.asyncio
async def test_worker_projects_distinct_progress_and_server_graded_mastery_atomically(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker

    class Documents:
        async def load_version_document(self, tenant_id: str, version_id: str):
            assert (tenant_id, version_id) == (projector_database.tenant_id, "version-1")
            return _document()

    await _append(projector_database, event_id="event-start", event_type="classroom.started")
    for event_id in ("event-scene-1", "event-scene-duplicate"):
        await _append(
            projector_database,
            event_id=event_id,
            event_type="scene.completed",
            scene_id="scene-1",
        )
    await _append(
        projector_database,
        event_id="event-quiz",
        event_type="quiz.graded",
        scene_id="quiz-scene",
        knowledge_point_id="kp-1",
        payload={
            "assessment_id": "quiz-scene",
            "question_id": "question-1",
            "answer": ["option-a"],
            "correct": False,
            "score": 0.0,
        },
    )
    assert await _backlog_event_ids(projector_database) == [
        "event-quiz",
        "event-scene-1",
        "event-scene-duplicate",
        "event-start",
    ]
    worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=Documents(),
        worker_id="projector-a",
    )

    worked = 0
    while await worker.run_once(tenant_id=projector_database.tenant_id):
        worked += 1
    assert worked == 4

    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.connect() as connection:
        progress = (
            await connection.execute(
                text(
                    f"SELECT last_event_seq, completed_scene_count FROM {quoted}.learning_progress "
                    "WHERE session_id = 'session-1'"
                )
            )
        ).one()
        attempt = (
            await connection.execute(
                text(
                    f"SELECT is_correct, score, grading_source FROM {quoted}.quiz_attempts "
                    "WHERE event_id = 'event-quiz'"
                )
            )
        ).one()
        mastery = (
            await connection.execute(
                text(
                    f"SELECT level, evidence_count FROM {quoted}.mastery_levels "
                    "WHERE user_id = 'student-1' AND knowledge_point_id = 'kp-1'"
                )
            )
        ).one()
        statuses = list(
            await connection.scalars(
                text(f"SELECT status FROM {quoted}.learning_projection_queue ORDER BY event_id")
            )
        )

    assert tuple(progress) == (4, 1)
    assert tuple(attempt) == (True, 1.0, "published_answer")
    assert tuple(mastery) == (0.5, 1)
    assert statuses == ["completed"] * 4
    assert await _backlog_event_ids(projector_database) == []


@pytest.mark.asyncio
async def test_lease_fencing_reclaims_expired_work_and_blocks_later_session_seq(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import (
        ProjectionLeaseLost,
        SqlAlchemyProjectionQueueRepository,
    )

    await _append(projector_database, event_id="event-1", event_type="classroom.started")
    await _append(projector_database, event_id="event-2", event_type="hint.used")
    assert await _backlog_event_ids(projector_database) == ["event-1", "event-2"]
    repository = SqlAlchemyProjectionQueueRepository(projector_database.engine)

    first = await repository.claim(
        projector_database.tenant_id,
        owner="worker-a",
        lease_seconds=60,
    )
    assert first is not None and (first.event.event_id, first.event.seq) == ("event-1", 1)
    assert (
        await repository.claim(
            projector_database.tenant_id,
            owner="worker-b",
            lease_seconds=60,
        )
        is None
    )

    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue "
                "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE event_id = 'event-1'"
            )
        )

    reclaimed = await repository.claim(
        projector_database.tenant_id,
        owner="worker-b",
        lease_seconds=60,
    )
    assert reclaimed is not None
    assert reclaimed.event.event_id == first.event.event_id
    assert reclaimed.lease_token != first.lease_token
    with pytest.raises(ProjectionLeaseLost):
        await repository.project(first, document=None)
    assert await _backlog_event_ids(projector_database) == ["event-1", "event-2"]

    await repository.project(reclaimed, document=None)
    assert await _backlog_event_ids(projector_database) == ["event-2"]
    second = await repository.claim(
        projector_database.tenant_id,
        owner="worker-b",
        lease_seconds=60,
    )
    assert second is not None and (second.event.event_id, second.event.seq) == ("event-2", 2)


@pytest.mark.asyncio
async def test_transient_failure_backs_off_then_quarantines_at_attempt_limit(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import LearningProjectionWorker

    class Documents:
        async def load_version_document(self, tenant_id: str, version_id: str):
            raise RuntimeError("temporary object-store outage")

    await _append(
        projector_database,
        event_id="event-transient",
        event_type="quiz.graded",
        scene_id="quiz-scene",
        knowledge_point_id="kp-1",
        payload={
            "assessment_id": "quiz-scene",
            "question_id": "question-1",
            "answer": ["option-a"],
        },
    )
    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue SET max_attempts = 2 "
                "WHERE event_id = 'event-transient'"
            )
        )
    worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=Documents(),
        worker_id="projector-retry",
    )

    assert await worker.run_once(tenant_id=projector_database.tenant_id) is True
    async with projector_database.engine.connect() as connection:
        first = (
            await connection.execute(
                text(
                    f"SELECT status, attempt_count, lease_owner IS NULL, "
                    "lease_token IS NULL, lease_expires_at IS NULL, heartbeat_at IS NULL, "
                    f"available_at > updated_at FROM {quoted}.learning_projection_queue "
                    "WHERE event_id = 'event-transient'"
                )
            )
        ).one()
    assert tuple(first) == ("failed", 1, True, True, True, True, True)
    assert await _backlog_event_ids(projector_database) == ["event-transient"]

    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue "
                "SET available_at = clock_timestamp() - interval '1 second' "
                "WHERE event_id = 'event-transient'"
            )
        )
    assert await worker.run_once(tenant_id=projector_database.tenant_id) is True
    async with projector_database.engine.connect() as connection:
        final = (
            await connection.execute(
                text(
                    f"SELECT status, attempt_count, last_error_code "
                    f"FROM {quoted}.learning_projection_queue "
                    "WHERE event_id = 'event-transient'"
                )
            )
        ).one()
        reason = await connection.scalar(
            text(
                f"SELECT reason_code FROM {quoted}.learning_event_quarantine "
                "WHERE event_id = 'event-transient'"
            )
        )
        derived_counts = (
            await connection.execute(
                text(
                    f"SELECT "
                    f"(SELECT count(*) FROM {quoted}.learning_progress), "
                    f"(SELECT count(*) FROM {quoted}.quiz_attempts), "
                    f"(SELECT count(*) FROM {quoted}.mastery_evidence), "
                    f"(SELECT count(*) FROM {quoted}.mastery_levels)"
                )
            )
        ).one()
    assert tuple(final) == ("quarantined", 2, "projection_attempts_exhausted")
    assert reason == "projection_attempts_exhausted"
    assert tuple(derived_counts) == (0, 0, 0, 0)
    assert await _backlog_event_ids(projector_database) == []


@pytest.mark.asyncio
async def test_complete_and_quarantine_delete_only_their_terminal_backlog(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import SqlAlchemyProjectionQueueRepository

    await _append(
        projector_database,
        event_id="event-complete",
        event_type="classroom.started",
    )
    await _append(
        projector_database,
        event_id="event-quarantine",
        event_type="hint.used",
    )
    repository = SqlAlchemyProjectionQueueRepository(projector_database.engine)

    completed = await repository.claim(
        projector_database.tenant_id,
        owner="projector-terminal",
        lease_seconds=60,
    )
    assert completed is not None and completed.event.event_id == "event-complete"
    await repository.complete(completed)
    assert await _backlog_event_ids(projector_database) == ["event-quarantine"]

    quarantined = await repository.claim(
        projector_database.tenant_id,
        owner="projector-terminal",
        lease_seconds=60,
    )
    assert quarantined is not None and quarantined.event.event_id == "event-quarantine"
    await repository.quarantine(quarantined, reason_code="projection_event_invalid")
    assert await _backlog_event_ids(projector_database) == []


@pytest.mark.asyncio
async def test_claim_sweeps_expired_exhausted_lease_and_deletes_backlog(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import SqlAlchemyProjectionQueueRepository

    await _append(
        projector_database,
        event_id="event-expired-exhausted",
        event_type="classroom.started",
    )
    repository = SqlAlchemyProjectionQueueRepository(projector_database.engine)
    claimed = await repository.claim(
        projector_database.tenant_id,
        owner="projector-expired",
        lease_seconds=60,
    )
    assert claimed is not None
    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_projection_queue "
                "SET attempt_count = max_attempts, "
                "lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE event_id = 'event-expired-exhausted'"
            )
        )

    assert (
        await repository.claim(
            projector_database.tenant_id,
            owner="projector-other",
            lease_seconds=60,
        )
        is None
    )
    async with projector_database.engine.connect() as connection:
        status = await connection.scalar(
            text(
                f"SELECT status FROM {quoted}.learning_projection_queue "
                "WHERE event_id = 'event-expired-exhausted'"
            )
        )
        reason = await connection.scalar(
            text(
                f"SELECT reason_code FROM {quoted}.learning_event_quarantine "
                "WHERE event_id = 'event-expired-exhausted'"
            )
        )
    assert (status, reason) == ("quarantined", "projection_attempts_exhausted")
    assert await _backlog_event_ids(projector_database) == []


@pytest.mark.asyncio
async def test_missing_terminal_backlog_fails_closed_and_rolls_back_queue_status(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import SqlAlchemyProjectionQueueRepository
    from deeptutor.teaching.repositories.metric_rollups import MetricRollupConsistencyError

    await _append(
        projector_database,
        event_id="event-missing-backlog",
        event_type="classroom.started",
    )
    repository = SqlAlchemyProjectionQueueRepository(projector_database.engine)
    claim = await repository.claim(
        projector_database.tenant_id,
        owner="projector-missing",
        lease_seconds=60,
    )
    assert claim is not None
    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM platform.teaching_learning_projection_backlog "
                "WHERE tenant_id = :tenant_id AND event_id = 'event-missing-backlog'"
            ),
            {"tenant_id": projector_database.tenant_id},
        )

    with pytest.raises(
        MetricRollupConsistencyError,
        match="learning projection backlog row is missing",
    ):
        await repository.complete(claim)

    async with projector_database.engine.connect() as connection:
        queue = (
            await connection.execute(
                text(
                    f"SELECT status, lease_owner, lease_token FROM {quoted}.learning_projection_queue "
                    "WHERE event_id = 'event-missing-backlog'"
                )
            )
        ).one()
    assert tuple(queue) == ("running", claim.lease_owner, claim.lease_token)


@pytest.mark.asyncio
async def test_cross_session_same_knowledge_point_updates_are_serialized_and_ordered(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import SqlAlchemyProjectionQueueRepository

    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_sessions SET started_at = '2026-08-10T12:00:00Z' "
                "WHERE id = 'session-1'"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.learning_sessions "
                "(id, tenant_id, user_id, classroom_version_id, assignment_id, status, "
                "started_at) VALUES ('session-2', :tenant_id, 'student-1', 'version-1', "
                "'assignment-1', 'active', '2026-08-10T12:01:00Z')"
            ),
            {"tenant_id": projector_database.tenant_id},
        )
    for session_id, answer in (("session-1", ["option-b"]), ("session-2", ["option-a"])):
        await _append(
            projector_database,
            event_id=f"event-{session_id}",
            event_type="quiz.graded",
            scene_id="quiz-scene",
            knowledge_point_id="kp-1",
            payload={
                "assessment_id": "quiz-scene",
                "question_id": "question-1",
                "answer": answer,
            },
            session_id=session_id,
        )
    repository = SqlAlchemyProjectionQueueRepository(projector_database.engine)
    first = await repository.claim(
        projector_database.tenant_id,
        owner="projector-a",
        lease_seconds=60,
    )
    second = await repository.claim(
        projector_database.tenant_id,
        owner="projector-b",
        lease_seconds=60,
    )
    assert first is not None and second is not None

    await asyncio.gather(
        repository.project(first, document=_document()),
        repository.project(second, document=_document()),
    )

    async with projector_database.engine.connect() as connection:
        mastery = (
            await connection.execute(
                text(
                    f"SELECT level, evidence_count FROM {quoted}.mastery_levels "
                    "WHERE user_id = 'student-1' AND knowledge_point_id = 'kp-1'"
                )
            )
        ).one()
        completed = await connection.scalar(
            text(
                f"SELECT count(*) FROM {quoted}.learning_projection_queue "
                "WHERE status = 'completed'"
            )
        )
        evidence_count = await connection.scalar(
            text(
                f"SELECT count(*) FROM {quoted}.mastery_evidence "
                "WHERE user_id = 'student-1' AND knowledge_point_id = 'kp-1'"
            )
        )

    assert mastery[0] == pytest.approx(compute_mastery([False, True]))
    assert mastery[1] == 2
    assert evidence_count == 2
    assert completed == 2


@pytest.mark.asyncio
async def test_inverse_cross_session_projection_keeps_last_evidence_in_stable_order(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import SqlAlchemyProjectionQueueRepository

    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_sessions SET started_at = '2026-08-10T12:00:00Z' "
                "WHERE id = 'session-1'"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.learning_sessions "
                "(id, tenant_id, user_id, classroom_version_id, assignment_id, status, "
                "started_at) VALUES ('session-2', :tenant_id, 'student-1', 'version-1', "
                "'assignment-1', 'active', '2026-08-10T12:01:00Z')"
            ),
            {"tenant_id": projector_database.tenant_id},
        )
    for session_id, answer in (("session-1", ["option-b"]), ("session-2", ["option-a"])):
        await _append(
            projector_database,
            event_id=f"event-{session_id}",
            event_type="quiz.graded",
            scene_id="quiz-scene",
            knowledge_point_id="kp-1",
            payload={
                "assessment_id": "quiz-scene",
                "question_id": "question-1",
                "answer": answer,
            },
            session_id=session_id,
        )

    repository = SqlAlchemyProjectionQueueRepository(projector_database.engine)
    first = await repository.claim(
        projector_database.tenant_id,
        owner="projector-a",
        lease_seconds=60,
    )
    second = await repository.claim(
        projector_database.tenant_id,
        owner="projector-b",
        lease_seconds=60,
    )
    assert first is not None and second is not None
    claims = {claim.event.session_id: claim for claim in (first, second)}

    await repository.project(claims["session-2"], document=_document())
    await repository.project(claims["session-1"], document=_document())

    async with projector_database.engine.connect() as connection:
        mastery = (
            await connection.execute(
                text(
                    f"SELECT level, evidence_count, last_evidence_event_id "
                    f"FROM {quoted}.mastery_levels "
                    "WHERE user_id = 'student-1' AND knowledge_point_id = 'kp-1'"
                )
            )
        ).one()

    assert mastery[0] == pytest.approx(compute_mastery([False, True]))
    assert tuple(mastery[1:]) == (2, "event-session-2")


@pytest.mark.asyncio
async def test_active_tenant_scan_rejects_mismatched_registered_schema(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.projector_worker import SqlAlchemyProjectionQueueRepository

    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE platform.tenant_schema_states SET schema_name = 'tenant_wrong' "
                "WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": projector_database.tenant_id},
        )

    repository = SqlAlchemyProjectionQueueRepository(projector_database.engine)

    assert projector_database.tenant_id not in await repository.active_tenant_ids()


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_all_derived_writes_before_retry(
    projector_database: ProjectorDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.teaching.projector_worker import (
        LearningProjectionWorker,
        _SessionMasteryRepository,
    )

    await _append(
        projector_database,
        event_id="event-atomic-failure",
        event_type="quiz.graded",
        scene_id="quiz-scene",
        knowledge_point_id="kp-1",
        payload={
            "assessment_id": "quiz-scene",
            "question_id": "question-1",
            "answer": ["option-a"],
        },
    )

    async def fail_before_mastery(self, **kwargs) -> None:
        raise RuntimeError("injected failure after evidence writes")

    monkeypatch.setattr(_SessionMasteryRepository, "upsert_mastery", fail_before_mastery)

    class Documents:
        async def load_version_document(self, tenant_id: str, version_id: str):
            return _document()

    worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=Documents(),
        worker_id="projector-atomic-failure",
    )

    assert await worker.run_once(tenant_id=projector_database.tenant_id) is True

    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.connect() as connection:
        queue = (
            await connection.execute(
                text(
                    f"SELECT status, attempt_count, last_error_code "
                    f"FROM {quoted}.learning_projection_queue "
                    "WHERE event_id = 'event-atomic-failure'"
                )
            )
        ).one()
        derived_counts = (
            await connection.execute(
                text(
                    f"SELECT "
                    f"(SELECT count(*) FROM {quoted}.learning_progress), "
                    f"(SELECT count(*) FROM {quoted}.quiz_attempts), "
                    f"(SELECT count(*) FROM {quoted}.mastery_evidence), "
                    f"(SELECT count(*) FROM {quoted}.mastery_levels)"
                )
            )
        ).one()

    assert tuple(queue) == ("failed", 1, "transient_runtimeerror")
    assert tuple(derived_counts) == (0, 0, 0, 0)
    assert await _backlog_event_ids(projector_database) == ["event-atomic-failure"]
