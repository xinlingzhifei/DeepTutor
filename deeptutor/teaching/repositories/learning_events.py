"""Atomic append repository for tenant classroom learning events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.models import (
    LearningEvent,
    LearningEventQuarantine,
    LearningProjectionQueueItem,
    LearningSession,
)
from deeptutor.teaching.schema_names import tenant_schema_name


class LearningEventRepositoryError(RuntimeError):
    """Base error for a rejected learning-event append."""


class LearningEventBindingError(LearningEventRepositoryError):
    """The requested tenant, user, session, or classroom binding is invalid."""


class LearningSessionUnavailable(LearningEventRepositoryError):
    """The server-owned session is absent or no longer accepts new events."""


@dataclass(frozen=True, slots=True)
class LearningEventAppend:
    event_id: str
    tenant_id: str
    session_id: str
    user_id: str
    classroom_version_id: str
    event_type: str
    occurred_at: datetime
    payload: dict[str, object]
    scene_id: str | None = None
    knowledge_point_id: str | None = None

    def __post_init__(self) -> None:
        required = {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "classroom_version_id": self.classroom_version_id,
            "event_type": self.event_type,
        }
        if any(not value.strip() for value in required.values()):
            raise ValueError("learning event identifiers must not be blank")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a JSON object")


@dataclass(frozen=True, slots=True)
class LearningEventAppendResult:
    event_id: str
    outcome: Literal["accepted", "duplicate"]
    seq: int


class SqlAlchemyLearningEventRepository:
    """Serialize appends per session and persist event plus queue atomically."""

    def __init__(self, engine: AsyncEngine, tenant_id: str) -> None:
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(
            translated,
            expire_on_commit=False,
        )

    async def append(self, event: LearningEventAppend) -> LearningEventAppendResult:
        async with self._session_factory() as database_session:
            async with database_session.begin():
                return await self.append_in_session(database_session, event)

    async def append_in_session(
        self,
        database_session: AsyncSession,
        event: LearningEventAppend,
    ) -> LearningEventAppendResult:
        """Append inside a transaction owned by the caller."""
        if event.tenant_id != self._tenant_id:
            raise LearningEventBindingError("learning event tenant does not match repository")

        await database_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:event_id, 0))"),
            {"event_id": event.event_id},
        )
        learning_session = await database_session.scalar(
            select(LearningSession)
            .where(
                LearningSession.id == event.session_id,
                LearningSession.tenant_id == self._tenant_id,
            )
            .with_for_update()
        )
        if learning_session is None:
            raise LearningSessionUnavailable("learning session is unavailable")
        if (
            learning_session.user_id != event.user_id
            or learning_session.classroom_version_id != event.classroom_version_id
        ):
            raise LearningEventBindingError(
                "learning event does not match its server-owned session"
            )

        existing = await database_session.scalar(
            select(LearningEvent).where(LearningEvent.event_id == event.event_id)
        )
        if existing is not None:
            if (
                existing.tenant_id != self._tenant_id
                or existing.session_id != event.session_id
                or existing.user_id != event.user_id
                or existing.classroom_version_id != event.classroom_version_id
            ):
                raise LearningEventBindingError(
                    "learning event id belongs to another session binding"
                )
            if (
                existing.event_type != event.event_type
                or existing.occurred_at != event.occurred_at
                or existing.scene_id != event.scene_id
                or existing.knowledge_point_id != event.knowledge_point_id
                or existing.payload != event.payload
            ):
                raise LearningEventBindingError(
                    "learning event id was reused with changed event facts"
                )
            return LearningEventAppendResult(
                event_id=existing.event_id,
                outcome="duplicate",
                seq=existing.seq,
            )
        quarantined = await database_session.scalar(
            select(LearningEventQuarantine).where(
                LearningEventQuarantine.event_id == event.event_id
            )
        )
        if quarantined is not None:
            raise LearningEventBindingError(
                "learning event id is already bound to a quarantined event"
            )
        if learning_session.status != "active":
            raise LearningSessionUnavailable("learning session does not accept new events")

        seq = learning_session.next_seq
        learning_session.next_seq = seq + 1
        stored = LearningEvent(
            event_id=event.event_id,
            tenant_id=self._tenant_id,
            session_id=event.session_id,
            user_id=event.user_id,
            classroom_version_id=event.classroom_version_id,
            seq=seq,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            scene_id=event.scene_id,
            knowledge_point_id=event.knowledge_point_id,
            payload=dict(event.payload),
        )
        database_session.add(stored)
        await database_session.flush()
        database_session.add(
            LearningProjectionQueueItem(
                event_id=event.event_id,
                tenant_id=self._tenant_id,
                session_id=event.session_id,
            )
        )
        await database_session.flush()
        return LearningEventAppendResult(
            event_id=event.event_id,
            outcome="accepted",
            seq=seq,
        )

    async def count_events(self, session_id: str) -> int:
        async with self._session_factory() as database_session:
            count = await database_session.scalar(
                select(func.count())
                .select_from(LearningEvent)
                .where(
                    LearningEvent.tenant_id == self._tenant_id,
                    LearningEvent.session_id == session_id,
                )
            )
        return int(count or 0)

    async def count_projection_items(self, session_id: str) -> int:
        async with self._session_factory() as database_session:
            count = await database_session.scalar(
                select(func.count())
                .select_from(LearningProjectionQueueItem)
                .where(
                    LearningProjectionQueueItem.tenant_id == self._tenant_id,
                    LearningProjectionQueueItem.session_id == session_id,
                )
            )
        return int(count or 0)


__all__ = [
    "LearningEventAppend",
    "LearningEventAppendResult",
    "LearningEventBindingError",
    "LearningEventRepositoryError",
    "LearningSessionUnavailable",
    "SqlAlchemyLearningEventRepository",
]
