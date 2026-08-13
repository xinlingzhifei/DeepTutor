from __future__ import annotations

from datetime import UTC, datetime

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    (
        "classroom.started",
        "scene.completed",
        "hint.used",
        "classroom.completed",
    ),
)
async def test_engagement_events_do_not_change_mastery(event_type: str) -> None:
    from deeptutor.teaching.projectors.mastery import MasteryProjector, ProjectionEvent

    class Repository:
        async def get_mastery(self, user_id: str, knowledge_point_id: str) -> float:
            return 0.35

        async def evidence_count(self, user_id: str, knowledge_point_id: str) -> int:
            return 2

    projector = MasteryProjector(Repository())
    event = ProjectionEvent(
        event_id=f"event-{event_type}",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type=event_type,
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        scene_id="scene-a" if "." in event_type else None,
        knowledge_point_id="kp-1",
        payload={},
    )

    before = await projector.mastery("student-a", "kp-1")
    await projector.apply(event)
    after = await projector.mastery("student-a", "kp-1")

    assert after == before


def test_progress_uses_distinct_scene_count_and_never_reopens_completion() -> None:
    from deeptutor.teaching.projectors.mastery import ProjectionEvent
    from deeptutor.teaching.projectors.progress import ProgressState, project_progress

    completed = ProjectionEvent(
        event_id="event-complete",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=4,
        event_type="classroom.completed",
        occurred_at=datetime(2026, 8, 10, 12, 4, tzinfo=UTC),
        scene_id=None,
        knowledge_point_id=None,
        payload={},
    )
    late_hint = ProjectionEvent(
        event_id="event-late-hint",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=5,
        event_type="hint.used",
        occurred_at=datetime(2026, 8, 10, 12, 5, tzinfo=UTC),
        scene_id="scene-a",
        knowledge_point_id="kp-1",
        payload={},
    )

    state = project_progress(None, completed, completed_scene_count=2)
    state = project_progress(state, late_hint, completed_scene_count=2)

    assert state == ProgressState(
        status="completed",
        last_event_id="event-late-hint",
        last_event_seq=5,
        completed_scene_count=2,
        last_scene_id="scene-a",
        completed_at=completed.occurred_at,
    )
