from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from tests.teaching.integration.test_learning_projector import (
    ProjectorDatabase,
    projector_database,
)


async def _seed_report_facts(database: ProjectorDatabase) -> None:
    quoted = f'"{database.schema_name}"'
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.generation_jobs ("
                "id, tenant_id, job_kind, phase, status, priority, quota_units, "
                "actor_id, owner_id, visibility, request_id, idempotency_key, "
                "resource_course_id, resource_class_id, request_sha256, "
                "data_plane_route_id, provider_profile_id, worker_pool_ref, "
                "queue_ref, request_payload, progress_percent) VALUES ("
                "'job-private', :tenant_id, 'generation', 'content', 'succeeded', "
                "0, 1, 'student-1', 'student-1', 'private', 'request-private', "
                "'idempotency-private', 'course-1', 'class-1', :sha, 'route-1', "
                "'provider-1', 'workers-1', 'queue-private', '{}', 100)"
            ),
            {"tenant_id": database.tenant_id, "sha": "b" * 64},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.classroom_assets "
                "(id, tenant_id, owner_id, title, lifecycle_state) VALUES "
                "('asset-private', :tenant_id, 'student-1', 'Private', 'editing')"
            ),
            {"tenant_id": database.tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.classroom_versions "
                "(id, tenant_id, classroom_id, version_number, generation_job_id, "
                "document_sha256, media_manifest_sha256, document_object_key) VALUES "
                "('version-private', :tenant_id, 'asset-private', 1, 'job-private', "
                ":sha, :sha, 'private/classroom.json')"
            ),
            {"tenant_id": database.tenant_id, "sha": "b" * 64},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.classes (id, course_id, name) "
                "VALUES ('class-2', 'course-1', 'Other class')"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.enrollments (class_id, learner_id, status) VALUES "
                "('class-1', 'student-1', 'active'), "
                "('class-2', 'student-1', 'active'), "
                "('class-1', 'student-without-session', 'active')"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.course_generation_policies "
                "(course_id, tenant_id, allowed_content_modes, daily_student_units, "
                "monthly_student_units, updated_by) VALUES "
                "('course-1', :tenant_id, 'open_creation', 10, 100, 'teacher-1')"
            ),
            {"tenant_id": database.tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.student_generation_requests "
                "(id, tenant_id, learner_id, course_id, class_id, mode, content_mode, "
                "web_search_requested, scene_min, scene_max, duration_minutes_min, "
                "duration_minutes_max, estimated_units, quota_state, "
                "requires_outline_confirmation, decision_outcome, decision_reason, "
                "evaluated_checks) VALUES "
                "('request-personal', :tenant_id, 'student-1', 'course-1', "
                "'class-1', 'micro', 'open_creation', false, 1, 3, 1, 10, 1, "
                "'settled', false, 'accepted', 'allowed', 'policy')"
            ),
            {"tenant_id": database.tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.student_classroom_assets "
                "(asset_id, tenant_id, request_id) VALUES "
                "('asset-private', :tenant_id, 'request-personal')"
            ),
            {"tenant_id": database.tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.assignments "
                "(id, tenant_id, classroom_version_id, class_id, assigned_by) "
                "VALUES ('assignment-2', :tenant_id, 'version-1', 'class-2', 'teacher-2')"
            ),
            {"tenant_id": database.tenant_id},
        )
        await connection.execute(
            text(
                f"UPDATE {quoted}.learning_sessions SET status='completed', "
                "completed_at=clock_timestamp() WHERE id='session-1'"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.learning_sessions "
                "(id, tenant_id, user_id, classroom_version_id, assignment_id, status) "
                "VALUES ('session-2', :tenant_id, 'student-1', 'version-1', "
                "'assignment-2', 'active')"
            ),
            {"tenant_id": database.tenant_id},
        )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.learning_sessions "
                "(id, tenant_id, user_id, classroom_version_id, student_asset_id, "
                "status) VALUES ('session-personal', :tenant_id, 'student-1', "
                "'version-private', 'asset-private', 'active')"
            ),
            {"tenant_id": database.tenant_id},
        )
        for event_id, session_id, seq, event_type, kp in (
            ("event-class-1-quiz", "session-1", 1, "quiz.graded", "kp-shared"),
            ("event-class-1-hint", "session-1", 2, "hint.used", None),
            ("event-class-1-pbl", "session-1", 3, "pbl.milestone_completed", None),
            ("event-class-2-quiz", "session-2", 1, "quiz.graded", "kp-shared"),
            ("event-personal-hint", "session-personal", 1, "hint.used", None),
        ):
            await connection.execute(
                text(
                    f"INSERT INTO {quoted}.learning_events "
                    "(event_id, tenant_id, session_id, user_id, classroom_version_id, "
                    "seq, event_type, occurred_at, knowledge_point_id, payload) VALUES "
                    "(:event_id, :tenant_id, :session_id, 'student-1', 'version-1', "
                    ":seq, :event_type, clock_timestamp(), :kp, '{}')"
                ),
                {
                    "event_id": event_id,
                    "tenant_id": database.tenant_id,
                    "session_id": session_id,
                    "seq": seq,
                    "event_type": event_type,
                    "kp": kp,
                },
            )
            if session_id == "session-personal":
                await connection.execute(
                    text(
                        f"UPDATE {quoted}.learning_events SET classroom_version_id="
                        "'version-private' WHERE event_id=:event_id"
                    ),
                    {"event_id": event_id},
                )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted}.learning_projection_queue "
                    "(event_id, tenant_id, session_id, status) "
                    "VALUES (:event_id, :tenant_id, :session_id, 'completed')"
                ),
                {
                    "event_id": event_id,
                    "tenant_id": database.tenant_id,
                    "session_id": session_id,
                },
            )
        for event_id, session_id, correct in (
            ("event-class-1-quiz", "session-1", True),
            ("event-class-2-quiz", "session-2", False),
        ):
            await connection.execute(
                text(
                    f"INSERT INTO {quoted}.quiz_attempts "
                    "(event_id, tenant_id, session_id, user_id, classroom_version_id, "
                    "assessment_id, question_id, knowledge_point_id, answer_payload, "
                    "is_correct, score, grading_source, graded_at) VALUES "
                    "(:event_id, :tenant_id, :session_id, 'student-1', 'version-1', "
                    "'quiz-1', 'question-1', 'kp-shared', '{}', :correct, :score, "
                    "'published_answer', clock_timestamp())"
                ),
                {
                    "event_id": event_id,
                    "tenant_id": database.tenant_id,
                    "session_id": session_id,
                    "correct": correct,
                    "score": 1.0 if correct else 0.0,
                },
            )
            await connection.execute(
                text(
                    f"INSERT INTO {quoted}.mastery_evidence "
                    "(event_id, tenant_id, session_id, user_id, classroom_version_id, "
                    "knowledge_point_id, evidence_type, correctness, score, grading_source) "
                    "VALUES (:event_id, :tenant_id, :session_id, 'student-1', 'version-1', "
                    "'kp-shared', 'quiz', :correct, :score, 'published_answer')"
                ),
                {
                    "event_id": event_id,
                    "tenant_id": database.tenant_id,
                    "session_id": session_id,
                    "correct": correct,
                    "score": 1.0 if correct else 0.0,
                },
            )
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.learning_progress "
                "(session_id, tenant_id, user_id, classroom_version_id, status, "
                "last_event_id, last_event_seq, completed_scene_count, completed_at) "
                "VALUES ('session-1', :tenant_id, 'student-1', 'version-1', 'completed', "
                "'event-class-1-pbl', 3, 2, clock_timestamp())"
            ),
            {"tenant_id": database.tenant_id},
        )
        for event_id, session_id in (
            ("quarantine-class-1", "session-1"),
            ("quarantine-class-2", "session-2"),
            ("quarantine-personal", "session-personal"),
        ):
            await connection.execute(
                text(
                    f"INSERT INTO {quoted}.learning_event_quarantine "
                    "(event_id, tenant_id, session_id, user_id, classroom_version_id, "
                    "event_type, occurred_at, payload, reason_code, details) VALUES "
                    "(:event_id, :tenant_id, :session_id, 'student-1', 'version-1', "
                    "'quiz.graded', clock_timestamp(), '{\"answer\":\"SECRET\"}', "
                    "'quiz_answer_invalid', '{\"provider\":\"SECRET\"}')"
                ),
                {
                    "event_id": event_id,
                    "tenant_id": database.tenant_id,
                    "session_id": session_id,
                },
            )
            if session_id == "session-personal":
                await connection.execute(
                    text(
                        f"UPDATE {quoted}.learning_event_quarantine SET "
                        "classroom_version_id='version-private' WHERE event_id=:event_id"
                    ),
                    {"event_id": event_id},
                )


@pytest.mark.asyncio
async def test_sql_reports_filter_metrics_mastery_and_quarantine_by_class(
    projector_database: ProjectorDatabase,
) -> None:
    from deeptutor.teaching.permissions import ResourceScope
    from deeptutor.teaching.services.reports import (
        ReportAccessScope,
        SqlAlchemyTeachingReportRepository,
    )

    await _seed_report_facts(projector_database)
    repository = SqlAlchemyTeachingReportRepository(projector_database.engine)

    assert await repository.student_in_class(projector_database.tenant_id, "class-1", "student-1")
    assert not await repository.student_in_class(
        projector_database.tenant_id, "class-1", "student-outside"
    )
    assert not await repository.student_in_class(
        projector_database.tenant_id,
        "class-1",
        "student-without-session",
    )
    metrics = await repository.class_report(
        projector_database.tenant_id,
        "class-1",
        "student-1",
    )
    classroom = await repository.classroom_report(
        projector_database.tenant_id,
        "version-1",
        ReportAccessScope(class_ids=frozenset({"class-1"})),
    )
    quarantined = await repository.quarantine(
        projector_database.tenant_id,
        ReportAccessScope(class_ids=frozenset({"class-1"})),
    )
    version_scopes = await repository.version_scopes(
        projector_database.tenant_id,
        "version-1",
    )
    tenant_wide = await repository.classroom_report(
        projector_database.tenant_id,
        "version-1",
        ReportAccessScope(
            tenant_wide=True,
            class_ids=frozenset({"class-1", "class-2"}),
        ),
    )
    private_scopes = await repository.version_scopes(
        projector_database.tenant_id,
        "version-private",
    )
    private_report = await repository.classroom_report(
        projector_database.tenant_id,
        "version-private",
        ReportAccessScope(tenant_wide=True),
    )
    tenant_quarantine = await repository.quarantine(
        projector_database.tenant_id,
        ReportAccessScope(tenant_wide=True),
    )

    assert metrics.session_count == 1
    assert metrics.completed_count == 1
    assert metrics.completion_rate == 1.0
    assert metrics.completed_scene_count == 2
    assert (metrics.valid_quiz_count, metrics.correct_quiz_count) == (1, 1)
    assert (metrics.hint_count, metrics.pbl_milestone_count) == (1, 1)
    assert metrics.mastery == (
        {"knowledge_point_id": "kp-shared", "level": 0.5, "evidence_count": 1},
    )
    assert metrics.projection_lag_seconds >= 0
    assert classroom == metrics
    assert [row.event_id for row in quarantined] == ["quarantine-class-1"]
    assert version_scopes is not None
    assert {scope.class_id for scope in version_scopes} == {
        "class-1",
        "class-2",
    }
    assert tenant_wide.session_count == 2
    assert tenant_wide.hint_count == 1
    assert private_scopes is not None
    assert private_scopes == (ResourceScope(projector_database.tenant_id),)
    assert private_report.session_count == 1
    assert private_report.hint_count == 1
    assert {row.event_id for row in tenant_quarantine} == {
        "quarantine-class-1",
        "quarantine-class-2",
        "quarantine-personal",
    }


__all__ = ["projector_database"]


@pytest.mark.asyncio
async def test_real_worker_commits_database_and_target_user_memory_together(
    projector_database: ProjectorDatabase,
    tmp_path,
) -> None:
    from deeptutor.services.path_service import PathService
    from deeptutor.teaching.projector_worker import LearningProjectionWorker
    from deeptutor.teaching.projectors.memory import ClassroomMemoryProjector
    from tests.teaching.integration.test_learning_projector import _append

    quoted = f'"{projector_database.schema_name}"'
    async with projector_database.engine.begin() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {quoted}.learning_sessions "
                "(id, tenant_id, user_id, classroom_version_id, assignment_id, status) "
                "VALUES ('session-memory-2', :tenant_id, 'student-1', 'version-1', "
                "'assignment-1', 'active')"
            ),
            {"tenant_id": projector_database.tenant_id},
        )
    await _append(
        projector_database,
        event_id="event-memory-smoke",
        event_type="scene.completed",
        scene_id="scene-1",
    )
    await _append(
        projector_database,
        event_id="event-memory-smoke-2",
        event_type="scene.completed",
        scene_id="scene-2",
        session_id="session-memory-2",
    )
    target = PathService(workspace_root=tmp_path / "student-1")

    class Targets:
        def path_service_for_user(self, user_id: str):
            assert user_id == "student-1"
            return target

    class Documents:
        async def load_version_document(self, *args):
            raise AssertionError("scene progress needs no document")

    first_memory_started = asyncio.Event()
    release_first_memory = asyncio.Event()
    memory = ClassroomMemoryProjector()

    class DelayedFirstMemory:
        async def project(self, event, *, aggregate, target_path_service):
            if event.event_id == "event-memory-smoke":
                first_memory_started.set()
                await release_first_memory.wait()
            await memory.project(
                event,
                aggregate=aggregate,
                target_path_service=target_path_service,
            )

    first_worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=Documents(),
        worker_id="projector-memory-first",
        memory_projector=DelayedFirstMemory(),
        memory_targets=Targets(),
    )
    second_worker = LearningProjectionWorker(
        engine=projector_database.engine,
        documents=Documents(),
        worker_id="projector-memory-second",
        memory_projector=DelayedFirstMemory(),
        memory_targets=Targets(),
    )

    first_run = asyncio.create_task(first_worker.run_once(tenant_id=projector_database.tenant_id))
    await asyncio.wait_for(first_memory_started.wait(), timeout=30)
    assert await second_worker.run_once(tenant_id=projector_database.tenant_id)
    release_first_memory.set()
    assert await asyncio.wait_for(first_run, timeout=30)
    async with projector_database.engine.connect() as connection:
        statuses = tuple(
            await connection.scalars(
                text(
                    f"SELECT status FROM {quoted}.learning_projection_queue "
                    "WHERE event_id IN ('event-memory-smoke', 'event-memory-smoke-2') "
                    "ORDER BY event_id"
                )
            )
        )
        progress = await connection.scalar(
            text(
                f"SELECT sum(completed_scene_count) FROM {quoted}.learning_progress "
                "WHERE session_id IN ('session-1', 'session-memory-2')"
            )
        )
    assert (statuses, progress) == (("completed", "completed"), 2)
    trace = next((target.get_memory_dir() / "trace" / "classroom").glob("*.jsonl"))
    trace_body = trace.read_text(encoding="utf-8")
    trace_rows = [json.loads(row) for row in trace_body.splitlines() if row]
    assert [row["kind"] for row in trace_rows] == [
        "scene.completed",
        "scene.completed",
    ]
    assert [row["payload"]["seq"] for row in trace_rows] == [1, 1]
    assert "event-memory-smoke" not in trace_body
    assert "Completed scenes: 2" in (target.get_memory_dir() / "L2" / "classroom.md").read_text(
        encoding="utf-8"
    )
