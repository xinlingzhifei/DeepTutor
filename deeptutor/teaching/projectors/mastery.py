"""Trusted quiz evidence and mastery projection policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from deeptutor.learning.mastery import compute_mastery


class DeterministicProjectionError(RuntimeError):
    """An event cannot be projected because its durable facts are invalid."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ProjectionEvent:
    event_id: str
    tenant_id: str
    session_id: str
    user_id: str
    classroom_version_id: str
    seq: int
    event_type: str
    occurred_at: datetime
    payload: dict[str, object]
    scene_id: str | None = None
    knowledge_point_id: str | None = None


@dataclass(frozen=True, slots=True)
class QuizEvaluation:
    assessment_id: str
    question_id: str
    knowledge_point_id: str
    answer_payload: dict[str, object]
    correct: bool
    score: float
    grading_source: str = "published_answer"


class MasteryProjectionRepository(Protocol):
    async def record_quiz_evidence(
        self,
        event: ProjectionEvent,
        evaluation: QuizEvaluation,
    ) -> bool: ...

    async def list_correctness(
        self,
        user_id: str,
        knowledge_point_id: str,
    ) -> tuple[list[bool], str]: ...

    async def upsert_mastery(
        self,
        *,
        user_id: str,
        knowledge_point_id: str,
        level: float,
        evidence_count: int,
        last_evidence_event_id: str,
    ) -> None: ...

    async def get_mastery(self, user_id: str, knowledge_point_id: str) -> float: ...

    async def evidence_count(self, user_id: str, knowledge_point_id: str) -> int: ...


def _required_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise DeterministicProjectionError(f"quiz_{name}_invalid")
    return value


def _selected_option_ids(answer: object, *, question_type: str) -> tuple[str, ...]:
    if not isinstance(answer, list) or not all(
        isinstance(option_id, str) and option_id for option_id in answer
    ):
        raise DeterministicProjectionError("quiz_answer_invalid")
    selected = tuple(answer)
    if len(set(selected)) != len(selected):
        raise DeterministicProjectionError("quiz_answer_invalid")
    if question_type == "single_choice" and len(selected) != 1:
        raise DeterministicProjectionError("quiz_answer_invalid")
    if question_type == "multiple_choice" and not selected:
        raise DeterministicProjectionError("quiz_answer_invalid")
    return selected


def evaluate_quiz(event: ProjectionEvent, document: object) -> QuizEvaluation | None:
    """Recompute a choice answer from the immutable published document."""

    if event.scene_id is None or event.knowledge_point_id is None:
        raise DeterministicProjectionError("quiz_binding_invalid")
    assessment_id = _required_string(event.payload, "assessment_id")
    question_id = _required_string(event.payload, "question_id")
    if assessment_id != event.scene_id:
        raise DeterministicProjectionError("quiz_assessment_invalid")

    scenes = {
        scene.id: scene for scene in getattr(getattr(document, "openmaic", None), "scenes", ())
    }
    scene = scenes.get(event.scene_id)
    if scene is None or getattr(scene, "type", None) != "quiz":
        raise DeterministicProjectionError("quiz_assessment_invalid")
    questions = {
        question.id: question
        for question in getattr(getattr(scene, "content", None), "questions", ())
    }
    question = questions.get(question_id)
    if question is None:
        raise DeterministicProjectionError("quiz_question_invalid")

    question_type = getattr(question, "question_type", None)
    if question_type == "short_answer":
        return None

    scene_knowledge_points = {
        mapping.knowledge_point_id
        for mapping in getattr(document, "knowledge_point_mappings", ())
        if event.scene_id in set(mapping.scene_ids)
    }
    if event.knowledge_point_id not in scene_knowledge_points:
        raise DeterministicProjectionError("quiz_knowledge_point_invalid")
    # The published v1 contract maps knowledge points to scenes, not questions.
    # Never trust the client's question-to-knowledge-point attribution when a
    # scene has more than one possible knowledge point.
    if len(scene_knowledge_points) != 1:
        raise DeterministicProjectionError("quiz_knowledge_point_ambiguous")

    if question_type not in {"single_choice", "multiple_choice"}:
        raise DeterministicProjectionError("quiz_question_type_invalid")
    selected = _selected_option_ids(
        event.payload.get("answer"),
        question_type=question_type,
    )
    option_ids = tuple(option.id for option in getattr(question, "options", ()))
    if not option_ids or len(set(option_ids)) != len(option_ids):
        raise DeterministicProjectionError("quiz_question_options_invalid")
    if not set(selected).issubset(option_ids):
        raise DeterministicProjectionError("quiz_answer_invalid")
    expected = tuple(getattr(question, "correct_option_ids", ()))
    if (
        not expected
        or len(set(expected)) != len(expected)
        or not set(expected).issubset(option_ids)
        or (question_type == "single_choice" and len(expected) != 1)
    ):
        raise DeterministicProjectionError("quiz_answer_key_invalid")
    correct = set(selected) == set(expected)
    return QuizEvaluation(
        assessment_id=assessment_id,
        question_id=question_id,
        knowledge_point_id=event.knowledge_point_id,
        answer_payload={"selected_option_ids": list(selected)},
        correct=correct,
        score=1.0 if correct else 0.0,
    )


class MasteryProjector:
    """Apply only trusted, idempotent graded facts to learner mastery."""

    def __init__(self, repository: MasteryProjectionRepository) -> None:
        self._repository = repository

    async def apply(self, event: ProjectionEvent, *, document: object | None = None) -> bool:
        if event.event_type != "quiz.graded":
            return False
        if document is None:
            raise DeterministicProjectionError("classroom_document_unavailable")
        evaluation = evaluate_quiz(event, document)
        if evaluation is None:
            return False
        inserted = await self._repository.record_quiz_evidence(event, evaluation)
        if not inserted:
            return False
        correctness, last_evidence_event_id = await self._repository.list_correctness(
            event.user_id,
            evaluation.knowledge_point_id,
        )
        await self._repository.upsert_mastery(
            user_id=event.user_id,
            knowledge_point_id=evaluation.knowledge_point_id,
            level=compute_mastery(correctness),
            evidence_count=len(correctness),
            last_evidence_event_id=last_evidence_event_id,
        )
        return True

    async def mastery(self, user_id: str, knowledge_point_id: str) -> float:
        return await self._repository.get_mastery(user_id, knowledge_point_id)

    async def evidence_count(self, user_id: str, knowledge_point_id: str) -> int:
        return await self._repository.evidence_count(user_id, knowledge_point_id)


__all__ = [
    "DeterministicProjectionError",
    "MasteryProjector",
    "ProjectionEvent",
    "QuizEvaluation",
    "evaluate_quiz",
]
