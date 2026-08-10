"""Atomic ingestion of signed classroom events into the append-only log."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from deeptutor.teaching.learning_events import (
    LearningEventBase,
    LearningEventBatch,
    validate_learning_event,
)
from deeptutor.teaching.models import LearningEvent, LearningEventQuarantine
from deeptutor.teaching.repositories.learning_events import (
    LearningEventAppend,
    LearningEventBindingError,
    SqlAlchemyLearningEventRepository,
)
from deeptutor.teaching.tenant_context import TenantContext
from deeptutor.teaching.tickets import ClassroomTicketClaims


class LearningSessionServiceLike(Protocol):
    async def get(self, context: TenantContext, *, session_id: str): ...

    async def consume_event_ticket(
        self,
        context: TenantContext,
        *,
        session_id: str,
        token: str,
        protected_action,
    ): ...


class ClassroomVersionDocumentLoader(Protocol):
    async def load_version_document(
        self,
        context: TenantContext,
        version_id: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class IngestedEvent:
    event_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class QuarantinedEvent:
    event_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class LearningEventIngestionResult:
    accepted: tuple[IngestedEvent, ...]
    duplicate: tuple[IngestedEvent, ...]
    quarantined: tuple[QuarantinedEvent, ...]


class ClassroomLearningEventIngestionService:
    """Validate against one immutable version and consume the write ticket atomically."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        sessions: LearningSessionServiceLike,
        document_loader: ClassroomVersionDocumentLoader,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._document_loader = document_loader

    async def ingest(
        self,
        context: TenantContext,
        *,
        session_id: str,
        token: str,
        batch: LearningEventBatch,
    ) -> LearningEventIngestionResult:
        session = await self._sessions.get(context, session_id=session_id)
        document = await self._document_loader.load_version_document(
            context,
            session.classroom_version_id,
        )
        repository = SqlAlchemyLearningEventRepository(self._engine, context.tenant_id)

        async def persist(
            database_session: AsyncSession,
            claims: ClassroomTicketClaims,
        ) -> LearningEventIngestionResult:
            accepted: list[IngestedEvent] = []
            duplicate: list[IngestedEvent] = []
            quarantined: list[QuarantinedEvent] = []
            for event in batch.events:
                reason = validate_learning_event(event, document)
                if reason is not None:
                    await self._quarantine(
                        database_session,
                        claims=claims,
                        event=event,
                        reason=reason,
                    )
                    quarantined.append(QuarantinedEvent(event_id=event.event_id, reason=reason))
                    continue

                stored = await repository.append_in_session(
                    database_session,
                    LearningEventAppend(
                        event_id=event.event_id,
                        tenant_id=claims.tenant_id,
                        session_id=claims.session_id,
                        user_id=claims.user_id,
                        classroom_version_id=claims.classroom_version_id,
                        event_type=event.event_type,
                        occurred_at=event.occurred_at,
                        scene_id=getattr(event, "scene_id", None),
                        knowledge_point_id=getattr(
                            event,
                            "knowledge_point_id",
                            None,
                        ),
                        payload=event.model_dump(mode="json"),
                    ),
                )
                outcome = IngestedEvent(event_id=stored.event_id, seq=stored.seq)
                if stored.outcome == "accepted":
                    accepted.append(outcome)
                else:
                    duplicate.append(outcome)
            return LearningEventIngestionResult(
                accepted=tuple(accepted),
                duplicate=tuple(duplicate),
                quarantined=tuple(quarantined),
            )

        return await self._sessions.consume_event_ticket(
            context,
            session_id=session_id,
            token=token,
            protected_action=persist,
        )

    @staticmethod
    async def _quarantine(
        database_session: AsyncSession,
        *,
        claims: ClassroomTicketClaims,
        event: LearningEventBase,
        reason: str,
    ) -> None:
        await database_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:event_id, 0))"),
            {"event_id": event.event_id},
        )
        accepted = await database_session.scalar(
            select(LearningEvent).where(LearningEvent.event_id == event.event_id)
        )
        if accepted is not None:
            raise LearningEventBindingError(
                "learning event id is already bound to an accepted event"
            )

        payload = event.model_dump(mode="json")
        existing = await database_session.scalar(
            select(LearningEventQuarantine)
            .where(LearningEventQuarantine.event_id == event.event_id)
            .order_by(LearningEventQuarantine.id)
            .limit(1)
        )
        if existing is not None:
            if (
                existing.tenant_id != claims.tenant_id
                or existing.session_id != claims.session_id
                or existing.user_id != claims.user_id
                or existing.classroom_version_id != claims.classroom_version_id
                or existing.event_type != event.event_type
                or existing.occurred_at != event.occurred_at
                or existing.knowledge_point_id != getattr(event, "knowledge_point_id", None)
                or existing.payload != payload
                or existing.reason_code != reason
            ):
                raise LearningEventBindingError(
                    "learning event id was reused with changed event facts"
                )
            return

        database_session.add(
            LearningEventQuarantine(
                event_id=event.event_id,
                tenant_id=claims.tenant_id,
                session_id=claims.session_id,
                user_id=claims.user_id,
                classroom_version_id=claims.classroom_version_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                knowledge_point_id=getattr(event, "knowledge_point_id", None),
                payload=payload,
                reason_code=reason,
                details=None,
            )
        )
        await database_session.flush()


__all__ = [
    "ClassroomLearningEventIngestionService",
    "ClassroomVersionDocumentLoader",
    "IngestedEvent",
    "LearningEventIngestionResult",
    "QuarantinedEvent",
]
