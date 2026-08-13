"""Pure learning-progress state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .mastery import ProjectionEvent


@dataclass(frozen=True, slots=True)
class ProgressState:
    status: str
    last_event_id: str
    last_event_seq: int
    completed_scene_count: int
    last_scene_id: str | None
    completed_at: datetime | None


def project_progress(
    current: ProgressState | None,
    event: ProjectionEvent,
    *,
    completed_scene_count: int,
) -> ProgressState:
    """Advance progress from server-derived event order and a distinct-scene count."""

    completed_at = current.completed_at if current is not None else None
    status = current.status if current is not None else "active"
    if event.event_type == "classroom.completed" and status != "completed":
        status = "completed"
        completed_at = event.occurred_at
    return ProgressState(
        status=status,
        last_event_id=event.event_id,
        last_event_seq=event.seq,
        completed_scene_count=completed_scene_count,
        last_scene_id=event.scene_id or (current.last_scene_id if current is not None else None),
        completed_at=completed_at,
    )


__all__ = ["ProgressState", "project_progress"]
