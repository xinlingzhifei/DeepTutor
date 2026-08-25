"""Trusted, immutable teacher grading for PBL milestone completion events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
from typing import Protocol

from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.projectors.mastery import (
    DeterministicProjectionError,
    PblEvaluation,
)
from deeptutor.teaching.tenant_context import TenantContext


class PblGradingError(RuntimeError):
    """Base class for fixed-safe PBL grading errors."""


class PblGradingAccessDenied(PblGradingError, PermissionError):
    """The actor lacks authority for the event's real assignment class."""


class PblGradingConflict(PblGradingError):
    """A terminal result or idempotency key conflicts with this request."""


class PblGradingValidationError(PblGradingError, ValueError):
    """The public grading request is invalid."""

    @staticmethod
    def validate_score(score: float | None) -> float | None:
        if score is None:
            return None
        if isinstance(score, bool) or not isinstance(score, (float, int)):
            raise PblGradingValidationError("score is invalid")
        normalized = float(score)
        if not math.isfinite(normalized) or normalized < 0 or normalized > 1:
            raise PblGradingValidationError("score is invalid")
        return normalized


class PblGradingUnavailable(PblGradingError):
    """The grading transaction or its immutable document is unavailable."""


@dataclass(frozen=True, slots=True)
class PblGradingCommand:
    event_id: str
    passed: bool
    score: float | None
    source_reference: str
    idempotency_key: str

    def __post_init__(self) -> None:
        event_id = self.event_id.strip()
        source_reference = self.source_reference.strip()
        idempotency_key = self.idempotency_key.strip()
        if (
            not event_id
            or len(event_id) > 128
            or not source_reference
            or len(source_reference) > 256
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise PblGradingValidationError("grading request is invalid")
        if not isinstance(self.passed, bool):
            raise PblGradingValidationError("passed is invalid")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "source_reference", source_reference)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "score", self.validate_score(self.score))

    @staticmethod
    def validate_score(score: float | None) -> float | None:
        return PblGradingValidationError.validate_score(score)

    @property
    def request_sha256(self) -> str:
        encoded = json.dumps(
            {
                "eventId": self.event_id,
                "passed": self.passed,
                "score": self.score,
                "sourceReference": self.source_reference,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PblGradingRecord:
    result_id: str
    event_id: str
    passed: bool
    score: float | None
    source_reference: str
    grading_source: str
    graded_at: datetime
    request_sha256: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PblGradingBinding:
    event_id: str
    event_tenant_id: str
    event_session_id: str
    event_user_id: str
    event_classroom_version_id: str
    event_type: str
    event_scene_id: str | None
    event_knowledge_point_id: str | None
    event_payload: dict[str, object]
    session_id: str
    session_tenant_id: str
    session_user_id: str
    session_classroom_version_id: str
    assignment_id: str | None
    assignment_tenant_id: str | None
    assignment_classroom_version_id: str | None
    course_id: str | None
    class_id: str | None


class PblGradingDocumentLoader(Protocol):
    async def load_version_document(
        self,
        context: TenantContext,
        version_id: str,
    ) -> object: ...


class PblGradingRepository(Protocol):
    async def record(
        self,
        context: TenantContext,
        *,
        session_id: str,
        command: PblGradingCommand,
        documents: PblGradingDocumentLoader,
    ) -> PblGradingRecord: ...


def require_grading_permission(
    context: TenantContext,
    binding: PblGradingBinding,
) -> None:
    if (
        binding.assignment_id is None
        or binding.course_id is None
        or binding.class_id is None
        or context.tenant_id != binding.event_tenant_id
    ):
        raise PblGradingAccessDenied("PBL grading access denied")
    resource = ResourceScope(
        tenant_id=binding.event_tenant_id,
        course_id=binding.course_id,
        class_id=binding.class_id,
    )
    if not any(
        permission.allows_resource("learning_event.grade", resource)
        for permission in context.permissions
    ):
        raise PblGradingAccessDenied("PBL grading access denied")


def derive_pbl_evaluation(
    binding: PblGradingBinding,
    document: object,
    *,
    passed: bool,
    score: float | None,
) -> PblEvaluation:
    """Derive and validate every authoritative grading field server-side."""

    if binding.assignment_id is None:
        raise DeterministicProjectionError("pbl_class_authority_missing")
    if (
        binding.event_id == ""
        or binding.event_tenant_id != binding.session_tenant_id
        or binding.event_session_id != binding.session_id
        or binding.event_user_id != binding.session_user_id
        or binding.event_classroom_version_id != binding.session_classroom_version_id
        or binding.assignment_tenant_id != binding.session_tenant_id
        or binding.assignment_classroom_version_id != binding.session_classroom_version_id
    ):
        raise DeterministicProjectionError("pbl_event_binding_invalid")
    if binding.event_type != "pbl.milestone_completed":
        raise DeterministicProjectionError("pbl_event_type_invalid")
    if getattr(document, "classroom_version_id", None) != binding.event_classroom_version_id:
        raise DeterministicProjectionError("pbl_document_binding_invalid")
    if not binding.event_scene_id:
        raise DeterministicProjectionError("pbl_scene_invalid")
    scenes = {
        scene.id: scene for scene in getattr(getattr(document, "openmaic", None), "scenes", ())
    }
    scene = scenes.get(binding.event_scene_id)
    if scene is None or getattr(scene, "type", None) != "pbl":
        raise DeterministicProjectionError("pbl_scene_invalid")
    milestone_id = binding.event_payload.get("milestone_id")
    if not isinstance(milestone_id, str) or not milestone_id:
        raise DeterministicProjectionError("pbl_milestone_invalid")
    milestones = {
        milestone.id: milestone
        for milestone in getattr(getattr(scene, "content", None), "milestones", ())
    }
    milestone = milestones.get(milestone_id)
    if milestone is None:
        raise DeterministicProjectionError("pbl_milestone_invalid")
    rubric = getattr(milestone, "rubric", None)
    normalized_rubric = rubric.strip() if isinstance(rubric, str) else ""
    if not normalized_rubric:
        raise DeterministicProjectionError("pbl_rubric_invalid")
    knowledge_points = {
        mapping.knowledge_point_id
        for mapping in getattr(document, "knowledge_point_mappings", ())
        if binding.event_scene_id in set(mapping.scene_ids)
    }
    if len(knowledge_points) != 1:
        raise DeterministicProjectionError("pbl_knowledge_point_ambiguous")
    knowledge_point_id = next(iter(knowledge_points))
    if (
        binding.event_knowledge_point_id is not None
        and binding.event_knowledge_point_id != knowledge_point_id
    ):
        raise DeterministicProjectionError("pbl_knowledge_point_invalid")
    normalized_score = PblGradingValidationError.validate_score(score)
    return PblEvaluation(
        event_id=binding.event_id,
        tenant_id=binding.event_tenant_id,
        session_id=binding.event_session_id,
        user_id=binding.event_user_id,
        classroom_version_id=binding.event_classroom_version_id,
        scene_id=binding.event_scene_id,
        milestone_id=milestone_id,
        knowledge_point_id=knowledge_point_id,
        rubric_sha256=hashlib.sha256(normalized_rubric.encode("utf-8")).hexdigest(),
        correct=passed,
        score=normalized_score,
        grading_source="teacher_review",
    )


def resolve_existing_result(
    *,
    existing_by_key: PblGradingRecord | None,
    existing_by_event: PblGradingRecord | None,
    request_sha256: str,
) -> PblGradingRecord | None:
    for existing in (existing_by_key, existing_by_event):
        if existing is not None and existing.request_sha256 != request_sha256:
            raise PblGradingConflict("PBL grading result conflicts")
    if (
        existing_by_key is not None
        and existing_by_event is not None
        and existing_by_key.result_id != existing_by_event.result_id
    ):
        raise PblGradingConflict("PBL grading result conflicts")
    return existing_by_key or existing_by_event


def projection_queue_action(status: str) -> str:
    if status == "quarantined":
        raise PblGradingConflict("quarantined event cannot be graded")
    if status == "completed":
        return "requeue"
    if status in {"pending", "failed", "running"}:
        return "preserve"
    raise PblGradingConflict("projection queue state conflicts")


class PblGradingService:
    def __init__(
        self,
        repository: PblGradingRepository,
        documents: PblGradingDocumentLoader,
    ) -> None:
        self._repository = repository
        self._documents = documents

    async def record(
        self,
        context: TenantContext,
        *,
        session_id: str,
        command: PblGradingCommand,
    ) -> PblGradingRecord:
        return await self._repository.record(
            context,
            session_id=session_id,
            command=command,
            documents=self._documents,
        )


__all__ = [
    "PblGradingAccessDenied",
    "PblGradingBinding",
    "PblGradingCommand",
    "PblGradingConflict",
    "PblGradingError",
    "PblGradingRecord",
    "PblGradingService",
    "PblGradingUnavailable",
    "PblGradingValidationError",
    "derive_pbl_evaluation",
    "projection_queue_action",
    "require_grading_permission",
    "resolve_existing_result",
]
