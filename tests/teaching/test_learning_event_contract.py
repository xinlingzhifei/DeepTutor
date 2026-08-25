from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from pydantic import ValidationError
import pytest

from deeptutor.teaching.learning_events import (
    LearningEventBatch,
    validate_learning_event,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _base(event_id: str, event_type: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": NOW.isoformat(),
    }


def valid_events() -> list[dict[str, object]]:
    return [
        _base("event-start", "classroom.started"),
        {
            **_base("event-scene", "scene.completed"),
            "scene_id": "scene-slide",
            "knowledge_point_id": "kp-slide",
        },
        {
            **_base("event-quiz", "quiz.graded"),
            "scene_id": "scene-quiz",
            "knowledge_point_id": "kp-quiz",
            "assessment_id": "scene-quiz",
            "question_id": "question-1",
            "answer": ["option-a"],
        },
        {
            **_base("event-hint", "hint.used"),
            "scene_id": "scene-quiz",
            "knowledge_point_id": "kp-quiz",
            "hint_id": "hint-1",
        },
        {
            **_base("event-pbl", "pbl.milestone_completed"),
            "scene_id": "scene-pbl",
            "knowledge_point_id": "kp-pbl",
            "milestone_id": "milestone-1",
        },
        _base("event-complete", "classroom.completed"),
    ]


def test_contract_accepts_only_the_six_versioned_event_variants() -> None:
    batch = LearningEventBatch.model_validate({"events": valid_events()})

    assert [event.event_type for event in batch.events] == [
        "classroom.started",
        "scene.completed",
        "quiz.graded",
        "hint.used",
        "pbl.milestone_completed",
        "classroom.completed",
    ]

    unknown = _base("event-unknown", "interactive.event")
    with pytest.raises(ValidationError):
        LearningEventBatch.model_validate({"events": [unknown]})


@pytest.mark.parametrize(
    "forged_field",
    ["tenant_id", "user_id", "session_id", "classroom_version_id"],
)
def test_contract_forbids_client_authority_fields(forged_field: str) -> None:
    event = _base("event-start", "classroom.started")
    event[forged_field] = "forged"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LearningEventBatch.model_validate({"events": [event]})


@pytest.mark.parametrize(
    "forged_field",
    [
        "score",
        "passed",
        "correctness",
        "grading_source",
        "grader_id",
        "graded_by",
        "tenant_id",
        "user_id",
        "session_id",
        "classroom_version_id",
    ],
)
def test_pbl_completion_forbids_client_grading_and_authority_fields(
    forged_field: str,
) -> None:
    event = deepcopy(valid_events()[4])
    event[forged_field] = True if forged_field in {"passed", "correctness"} else "forged"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LearningEventBatch.model_validate({"events": [event]})


def test_quiz_event_requires_real_assessment_question_and_knowledge_bindings() -> None:
    quiz = valid_events()[2]
    for field in ("assessment_id", "question_id", "knowledge_point_id", "scene_id"):
        invalid = deepcopy(quiz)
        invalid.pop(field)
        with pytest.raises(ValidationError):
            LearningEventBatch.model_validate({"events": [invalid]})


def test_batch_is_limited_to_one_hundred_events() -> None:
    event = _base("event-start", "classroom.started")
    with pytest.raises(ValidationError):
        LearningEventBatch.model_validate({"events": [event] * 101})


def test_committed_learning_event_json_schema_matches_model_and_validates_examples() -> None:
    schema_path = Path("contracts/classroom/learning-event.schema.json")
    committed = json.loads(schema_path.read_text(encoding="utf-8"))
    generated = LearningEventBatch.model_json_schema(mode="validation", by_alias=True)

    assert committed == generated
    validator = Draft202012Validator(committed)
    validator.validate({"events": valid_events()})


def test_quiz_and_knowledge_bindings_are_checked_against_pinned_document() -> None:
    document = SimpleNamespace(
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
    valid = LearningEventBatch.model_validate({"events": [valid_events()[2]]}).events[0]
    missing_assessment = valid.model_copy(update={"assessment_id": "missing"})
    missing_question = valid.model_copy(update={"question_id": "missing"})
    wrong_knowledge = valid.model_copy(update={"knowledge_point_id": "kp-other"})

    assert validate_learning_event(valid, document) is None
    assert validate_learning_event(missing_assessment, document) == "assessment_not_in_version"
    assert validate_learning_event(missing_question, document) == "question_not_in_assessment"
    assert validate_learning_event(wrong_knowledge, document) == "knowledge_point_not_in_scene"
