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
                "VALUES (:tenant_id, :schema_name, '20260824_0018', 'active')"
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
                "document_sha256, media_manifest_sha256, document_object_key"
                ") VALUES ("
                "'version-1', :tenant_id, 'asset-1', 1, 'job-1', :sha, :sha, "
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

    await repository.project(reclaimed, document=None)
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
