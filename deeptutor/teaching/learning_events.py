"""Versioned, client-supplied classroom learning-event contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


class _LearningEventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LearningEventBase(_LearningEventModel):
    schema_version: Literal["1.0"]
    event_id: str = Field(min_length=1, max_length=128)
    occurred_at: AwareDatetime


class ClassroomStartedEvent(LearningEventBase):
    event_type: Literal["classroom.started"]


class SceneCompletedEvent(LearningEventBase):
    event_type: Literal["scene.completed"]
    scene_id: str = Field(min_length=1, max_length=128)
    knowledge_point_id: str | None = Field(default=None, min_length=1, max_length=128)


class QuizGradedEvent(LearningEventBase):
    event_type: Literal["quiz.graded"]
    scene_id: str = Field(min_length=1, max_length=128)
    knowledge_point_id: str = Field(min_length=1, max_length=128)
    assessment_id: str = Field(min_length=1, max_length=128)
    question_id: str = Field(min_length=1, max_length=128)
    answer: JsonValue


class HintUsedEvent(LearningEventBase):
    event_type: Literal["hint.used"]
    scene_id: str = Field(min_length=1, max_length=128)
    knowledge_point_id: str | None = Field(default=None, min_length=1, max_length=128)
    hint_id: str = Field(min_length=1, max_length=128)


class PblMilestoneCompletedEvent(LearningEventBase):
    event_type: Literal["pbl.milestone_completed"]
    scene_id: str = Field(min_length=1, max_length=128)
    knowledge_point_id: str | None = Field(default=None, min_length=1, max_length=128)
    milestone_id: str = Field(min_length=1, max_length=128)


class ClassroomCompletedEvent(LearningEventBase):
    event_type: Literal["classroom.completed"]


LearningEvent: TypeAlias = Annotated[
    ClassroomStartedEvent
    | SceneCompletedEvent
    | QuizGradedEvent
    | HintUsedEvent
    | PblMilestoneCompletedEvent
    | ClassroomCompletedEvent,
    Field(discriminator="event_type"),
]


class LearningEventBatch(_LearningEventModel):
    events: list[LearningEvent] = Field(min_length=1, max_length=100)


def validate_learning_event(event: LearningEvent, document: object) -> str | None:
    """Return a stable quarantine reason when an event misses pinned content."""

    scenes = {scene.id: scene for scene in getattr(getattr(document, "openmaic"), "scenes")}
    scene_id = getattr(event, "scene_id", None)
    scene = scenes.get(scene_id) if scene_id is not None else None
    if scene_id is not None and scene is None:
        return "scene_not_in_version"

    if isinstance(event, QuizGradedEvent):
        if (
            event.assessment_id != event.scene_id
            or scene is None
            or getattr(scene, "type", None) != "quiz"
        ):
            return "assessment_not_in_version"
        question_ids = {question.id for question in getattr(getattr(scene, "content"), "questions")}
        if event.question_id not in question_ids:
            return "question_not_in_assessment"

    if isinstance(event, PblMilestoneCompletedEvent):
        if scene is None or getattr(scene, "type", None) != "pbl":
            return "milestone_not_in_version"
        milestone_ids = {
            milestone.id for milestone in getattr(getattr(scene, "content"), "milestones")
        }
        if event.milestone_id not in milestone_ids:
            return "milestone_not_in_version"

    knowledge_point_id = getattr(event, "knowledge_point_id", None)
    if knowledge_point_id is not None:
        allowed_scenes = {
            mapped_scene_id
            for mapping in getattr(document, "knowledge_point_mappings")
            if mapping.knowledge_point_id == knowledge_point_id
            for mapped_scene_id in mapping.scene_ids
        }
        if scene_id is None or scene_id not in allowed_scenes:
            return "knowledge_point_not_in_scene"
    return None


def event_occurred_at(event: LearningEventBase) -> datetime:
    """Return the aware event time with a concrete datetime type for persistence."""

    return event.occurred_at


__all__ = [
    "ClassroomCompletedEvent",
    "ClassroomStartedEvent",
    "HintUsedEvent",
    "LearningEvent",
    "LearningEventBase",
    "LearningEventBatch",
    "PblMilestoneCompletedEvent",
    "QuizGradedEvent",
    "SceneCompletedEvent",
    "event_occurred_at",
    "validate_learning_event",
]
