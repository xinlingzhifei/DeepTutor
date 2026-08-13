from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from deeptutor.learning.mastery import compute_mastery


@pytest.mark.asyncio
async def test_duplicate_quiz_event_changes_mastery_once() -> None:
    from deeptutor.teaching.projectors.mastery import MasteryProjector, ProjectionEvent

    class Repository:
        def __init__(self) -> None:
            self.correctness: list[bool] = []
            self.event_ids: set[str] = set()
            self.ordered_event_ids: list[str] = []
            self.level = 0.0

        async def record_quiz_evidence(self, event, evaluation) -> bool:
            if event.event_id in self.event_ids:
                return False
            self.event_ids.add(event.event_id)
            self.ordered_event_ids.append(event.event_id)
            self.correctness.append(evaluation.correct)
            return True

        async def list_correctness(
            self,
            user_id: str,
            knowledge_point_id: str,
        ) -> tuple[list[bool], str]:
            return list(self.correctness), self.ordered_event_ids[-1]

        async def upsert_mastery(
            self,
            *,
            user_id: str,
            knowledge_point_id: str,
            level: float,
            evidence_count: int,
            last_evidence_event_id: str,
        ) -> None:
            self.level = level

        async def get_mastery(self, user_id: str, knowledge_point_id: str) -> float:
            return self.level

        async def evidence_count(self, user_id: str, knowledge_point_id: str) -> int:
            return len(self.correctness)

    document = SimpleNamespace(
        openmaic=SimpleNamespace(
            scenes=[
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
                )
            ]
        ),
        knowledge_point_mappings=[
            SimpleNamespace(
                knowledge_point_id="kp-1",
                scene_ids=["quiz-scene"],
            )
        ],
    )
    repository = Repository()
    projector = MasteryProjector(repository)
    event = ProjectionEvent(
        event_id="quiz-event-1",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="quiz.graded",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        scene_id="quiz-scene",
        knowledge_point_id="kp-1",
        payload={
            "assessment_id": "quiz-scene",
            "question_id": "question-1",
            "answer": ["option-a"],
        },
    )

    await projector.apply(event, document=document)
    await projector.apply(event, document=document)

    assert await projector.evidence_count("student-a", "kp-1") == 1
    assert await projector.mastery("student-a", "kp-1") == compute_mastery([True])


@pytest.mark.asyncio
async def test_mastery_uses_last_event_from_stable_evidence_order() -> None:
    from deeptutor.teaching.projectors.mastery import MasteryProjector

    class Repository:
        def __init__(self) -> None:
            self.last_evidence_event_id: str | None = None

        async def record_quiz_evidence(self, event, evaluation) -> bool:
            return True

        async def list_correctness(
            self,
            user_id: str,
            knowledge_point_id: str,
        ) -> tuple[list[bool], str]:
            return [False, True], "event-newer"

        async def upsert_mastery(self, **values) -> None:
            self.last_evidence_event_id = values["last_evidence_event_id"]

    repository = Repository()
    projector = MasteryProjector(repository)

    await projector.apply(
        _choice_event(answer=["option-a", "option-b"], event_id="event-older"),
        document=_choice_document(),
    )

    assert repository.last_evidence_event_id == "event-newer"


def _choice_event(*, answer: object, event_id: str = "event-choice"):
    from deeptutor.teaching.projectors.mastery import ProjectionEvent

    return ProjectionEvent(
        event_id=event_id,
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=1,
        event_type="quiz.graded",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        scene_id="quiz-scene",
        knowledge_point_id="kp-1",
        payload={
            "assessment_id": "quiz-scene",
            "question_id": "question-1",
            "answer": answer,
            "correct": False,
            "score": 0.0,
        },
    )


def _choice_document(*, question_type: str = "multiple_choice"):
    return SimpleNamespace(
        openmaic=SimpleNamespace(
            scenes=[
                SimpleNamespace(
                    id="quiz-scene",
                    type="quiz",
                    content=SimpleNamespace(
                        questions=[
                            SimpleNamespace(
                                id="question-1",
                                question_type=question_type,
                                options=[
                                    SimpleNamespace(id="option-a"),
                                    SimpleNamespace(id="option-b"),
                                ],
                                correct_option_ids=["option-a", "option-b"],
                            )
                        ]
                    ),
                )
            ]
        ),
        knowledge_point_mappings=[
            SimpleNamespace(knowledge_point_id="kp-1", scene_ids=["quiz-scene"])
        ],
    )


def test_multiple_choice_is_recomputed_server_side_and_answer_order_is_ignored() -> None:
    from deeptutor.teaching.projectors.mastery import evaluate_quiz

    evaluation = evaluate_quiz(
        _choice_event(answer=["option-b", "option-a"]),
        _choice_document(),
    )

    assert evaluation.correct is True
    assert evaluation.score == 1.0
    assert evaluation.grading_source == "published_answer"


@pytest.mark.parametrize("answer", ([], ["option-a", "option-b"], ["option-a", "option-a"]))
def test_single_choice_requires_exactly_one_unique_selected_option(answer: list[str]) -> None:
    from deeptutor.teaching.projectors.mastery import (
        DeterministicProjectionError,
        evaluate_quiz,
    )

    document = _choice_document(question_type="single_choice")
    document.openmaic.scenes[0].content.questions[0].correct_option_ids = ["option-a"]

    with pytest.raises(DeterministicProjectionError, match="quiz_answer_invalid"):
        evaluate_quiz(_choice_event(answer=answer), document)


def test_choice_answer_rejects_option_ids_missing_from_published_question() -> None:
    from deeptutor.teaching.projectors.mastery import (
        DeterministicProjectionError,
        evaluate_quiz,
    )

    with pytest.raises(DeterministicProjectionError, match="quiz_answer_invalid"):
        evaluate_quiz(
            _choice_event(answer=["option-not-published"]),
            _choice_document(question_type="single_choice"),
        )


def test_quiz_rejects_client_selected_knowledge_point_for_multi_question_scene() -> None:
    from deeptutor.teaching.projectors.mastery import (
        DeterministicProjectionError,
        evaluate_quiz,
    )

    document = _choice_document(question_type="single_choice")
    document.openmaic.scenes[0].content.questions[0].correct_option_ids = ["option-a"]
    document.openmaic.scenes[0].content.questions.append(
        SimpleNamespace(
            id="question-2",
            question_type="single_choice",
            options=[SimpleNamespace(id="option-c"), SimpleNamespace(id="option-d")],
            correct_option_ids=["option-c"],
        )
    )
    document.knowledge_point_mappings.append(
        SimpleNamespace(knowledge_point_id="kp-2", scene_ids=["quiz-scene"])
    )

    with pytest.raises(
        DeterministicProjectionError,
        match="quiz_knowledge_point_ambiguous",
    ):
        evaluate_quiz(_choice_event(answer=["option-a"]), document)


@pytest.mark.asyncio
async def test_short_answer_without_trusted_grader_is_progress_only() -> None:
    from deeptutor.teaching.projectors.mastery import MasteryProjector

    class Repository:
        async def record_quiz_evidence(self, event, evaluation) -> bool:
            raise AssertionError("short answers must not become mastery evidence")

        async def get_mastery(self, user_id: str, knowledge_point_id: str) -> float:
            return 0.25

        async def evidence_count(self, user_id: str, knowledge_point_id: str) -> int:
            return 1

    document = _choice_document(question_type="short_answer")
    document.openmaic.scenes[0].content.questions[0].correct_option_ids = []
    document.knowledge_point_mappings.append(
        SimpleNamespace(knowledge_point_id="kp-2", scene_ids=["quiz-scene"])
    )
    projector = MasteryProjector(Repository())

    changed = await projector.apply(
        _choice_event(answer="student response"),
        document=document,
    )

    assert changed is False
    assert await projector.mastery("student-a", "kp-1") == 0.25


@pytest.mark.asyncio
async def test_pbl_without_trusted_grader_or_teacher_result_is_progress_only() -> None:
    from deeptutor.teaching.projectors.mastery import MasteryProjector, ProjectionEvent

    class Repository:
        async def record_quiz_evidence(self, event, evaluation) -> bool:
            raise AssertionError("an ungraded PBL milestone must not become mastery evidence")

        async def get_mastery(self, user_id: str, knowledge_point_id: str) -> float:
            return 0.4

        async def evidence_count(self, user_id: str, knowledge_point_id: str) -> int:
            return 2

    event = ProjectionEvent(
        event_id="event-pbl",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="student-a",
        classroom_version_id="version-a",
        seq=2,
        event_type="pbl.milestone_completed",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        scene_id="pbl-scene",
        knowledge_point_id="kp-1",
        payload={"milestone_id": "milestone-1"},
    )
    projector = MasteryProjector(Repository())

    assert await projector.apply(event, document=SimpleNamespace()) is False
    assert await projector.mastery("student-a", "kp-1") == 0.4
