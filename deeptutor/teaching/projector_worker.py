"""Lease-fenced projection worker for durable classroom learning events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import secrets
from typing import Protocol

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from deeptutor.teaching.models import (
    LearningEvent,
    LearningEventQuarantine,
    LearningProgress,
    LearningProjectionQueueItem,
    LearningSession,
    MasteryEvidence,
    MasteryLevel,
    QuizAttempt,
    Tenant,
    TenantSchemaState,
)
from deeptutor.teaching.projectors.mastery import (
    DeterministicProjectionError,
    MasteryProjector,
    ProjectionEvent,
    QuizEvaluation,
)
from deeptutor.teaching.projectors.progress import ProgressState, project_progress
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.classroom_content import ClassroomContentUnavailable

_FINAL_QUEUE_STATUSES = ("completed", "quarantined")
_MINIMUM_SCHEMA_REVISION = "20260810_0016"


class ProjectionLeaseLost(RuntimeError):
    """The queue lease expired or was superseded by another worker."""


@dataclass(frozen=True, slots=True)
class ProjectionClaim:
    event: ProjectionEvent
    lease_owner: str
    lease_token: str


class ProjectionDocuments(Protocol):
    async def load_version_document(self, tenant_id: str, version_id: str) -> object: ...


class ProjectionQueueRepository(Protocol):
    async def active_tenant_ids(self) -> tuple[str, ...]: ...

    async def claim(
        self,
        tenant_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> ProjectionClaim | None: ...

    async def heartbeat(
        self,
        claim: ProjectionClaim,
        *,
        lease_seconds: int,
    ) -> None: ...

    async def project(self, claim: ProjectionClaim, *, document: object | None) -> None: ...

    async def quarantine(self, claim: ProjectionClaim, *, reason_code: str) -> None: ...

    async def retry(self, claim: ProjectionClaim, *, error_code: str) -> None: ...


def _projection_event(model: LearningEvent) -> ProjectionEvent:
    return ProjectionEvent(
        event_id=model.event_id,
        tenant_id=model.tenant_id,
        session_id=model.session_id,
        user_id=model.user_id,
        classroom_version_id=model.classroom_version_id,
        seq=model.seq,
        event_type=model.event_type,
        occurred_at=model.occurred_at,
        scene_id=model.scene_id,
        knowledge_point_id=model.knowledge_point_id,
        payload=dict(model.payload),
    )


def _clear_lease(item: LearningProjectionQueueItem) -> None:
    item.lease_owner = None
    item.lease_token = None
    item.lease_expires_at = None
    item.heartbeat_at = None


def _mastery_lock_key(tenant_id: str, user_id: str, knowledge_point_id: str) -> str:
    return (
        f"{len(tenant_id)}:{tenant_id}"
        f"{len(user_id)}:{user_id}"
        f"{len(knowledge_point_id)}:{knowledge_point_id}"
    )


class _SessionMasteryRepository:
    def __init__(self, session: AsyncSession, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def record_quiz_evidence(
        self,
        event: ProjectionEvent,
        evaluation: QuizEvaluation,
    ) -> bool:
        lock_key = _mastery_lock_key(
            self._tenant_id,
            event.user_id,
            evaluation.knowledge_point_id,
        )
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
        graded_at = await self._session.scalar(select(func.clock_timestamp()))
        inserted_attempt = await self._session.scalar(
            postgresql_insert(QuizAttempt)
            .values(
                event_id=event.event_id,
                tenant_id=self._tenant_id,
                session_id=event.session_id,
                user_id=event.user_id,
                classroom_version_id=event.classroom_version_id,
                assessment_id=evaluation.assessment_id,
                question_id=evaluation.question_id,
                knowledge_point_id=evaluation.knowledge_point_id,
                answer_payload=evaluation.answer_payload,
                is_correct=evaluation.correct,
                score=evaluation.score,
                grading_source=evaluation.grading_source,
                graded_at=graded_at,
            )
            .on_conflict_do_nothing(index_elements=[QuizAttempt.event_id])
            .returning(QuizAttempt.id)
        )
        if inserted_attempt is None:
            return False
        await self._session.execute(
            postgresql_insert(MasteryEvidence)
            .values(
                event_id=event.event_id,
                tenant_id=self._tenant_id,
                session_id=event.session_id,
                user_id=event.user_id,
                classroom_version_id=event.classroom_version_id,
                knowledge_point_id=evaluation.knowledge_point_id,
                evidence_type="quiz",
                correctness=evaluation.correct,
                score=evaluation.score,
                grading_source=evaluation.grading_source,
            )
            .on_conflict_do_nothing(index_elements=[MasteryEvidence.event_id])
        )
        return True

    async def list_correctness(
        self,
        user_id: str,
        knowledge_point_id: str,
    ) -> tuple[list[bool], str]:
        values = (
            await self._session.execute(
                select(MasteryEvidence.correctness, MasteryEvidence.event_id)
                .join(LearningEvent, LearningEvent.event_id == MasteryEvidence.event_id)
                .join(LearningSession, LearningSession.id == LearningEvent.session_id)
                .where(
                    MasteryEvidence.user_id == user_id,
                    MasteryEvidence.knowledge_point_id == knowledge_point_id,
                )
                .order_by(
                    LearningSession.started_at,
                    LearningEvent.session_id,
                    LearningEvent.seq,
                )
            )
        ).all()
        if not values:
            raise RuntimeError("inserted mastery evidence is unavailable")
        return [bool(correctness) for correctness, _event_id in values], str(values[-1][1])

    async def upsert_mastery(
        self,
        *,
        user_id: str,
        knowledge_point_id: str,
        level: float,
        evidence_count: int,
        last_evidence_event_id: str,
    ) -> None:
        statement = postgresql_insert(MasteryLevel).values(
            tenant_id=self._tenant_id,
            user_id=user_id,
            knowledge_point_id=knowledge_point_id,
            level=level,
            evidence_count=evidence_count,
            last_evidence_event_id=last_evidence_event_id,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[MasteryLevel.user_id, MasteryLevel.knowledge_point_id],
                set_={
                    "tenant_id": self._tenant_id,
                    "level": level,
                    "evidence_count": evidence_count,
                    "last_evidence_event_id": last_evidence_event_id,
                    "updated_at": func.clock_timestamp(),
                },
            )
        )

    async def get_mastery(self, user_id: str, knowledge_point_id: str) -> float:
        level = await self._session.scalar(
            select(MasteryLevel.level).where(
                MasteryLevel.user_id == user_id,
                MasteryLevel.knowledge_point_id == knowledge_point_id,
            )
        )
        return float(level or 0.0)

    async def evidence_count(self, user_id: str, knowledge_point_id: str) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(MasteryEvidence)
            .where(
                MasteryEvidence.user_id == user_id,
                MasteryEvidence.knowledge_point_id == knowledge_point_id,
            )
        )
        return int(count or 0)


class SqlAlchemyProjectionQueueRepository:
    """Claim and project queue items under tenant-bound database transactions."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._platform_sessions = async_sessionmaker(engine, expire_on_commit=False)

    def _tenant_sessions(self, tenant_id: str) -> async_sessionmaker[AsyncSession]:
        translated = self._engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        return async_sessionmaker(translated, expire_on_commit=False)

    async def active_tenant_ids(self) -> tuple[str, ...]:
        async with self._platform_sessions() as session:
            rows = (
                await session.execute(
                    select(Tenant.id, TenantSchemaState.schema_name)
                    .join(TenantSchemaState, TenantSchemaState.tenant_id == Tenant.id)
                    .where(
                        Tenant.status == "active",
                        TenantSchemaState.status == "active",
                        TenantSchemaState.revision >= _MINIMUM_SCHEMA_REVISION,
                    )
                    .order_by(Tenant.id)
                )
            ).all()
            return tuple(
                tenant_id
                for tenant_id, schema_name in rows
                if schema_name == tenant_schema_name(tenant_id)
            )

    @staticmethod
    async def _store_quarantine(
        session: AsyncSession,
        event: LearningEvent,
        reason_code: str,
    ) -> None:
        existing = await session.scalar(
            select(LearningEventQuarantine.id).where(
                LearningEventQuarantine.event_id == event.event_id,
                LearningEventQuarantine.reason_code == reason_code,
            )
        )
        if existing is not None:
            return
        session.add(
            LearningEventQuarantine(
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                session_id=event.session_id,
                user_id=event.user_id,
                classroom_version_id=event.classroom_version_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                knowledge_point_id=event.knowledge_point_id,
                payload=dict(event.payload),
                reason_code=reason_code,
                details=None,
            )
        )

    async def _quarantine_exhausted_leases(
        self,
        session: AsyncSession,
        tenant_id: str,
    ) -> None:
        queue_item = aliased(LearningProjectionQueueItem)
        event = aliased(LearningEvent)
        rows = (
            await session.execute(
                select(queue_item, event)
                .join(event, event.event_id == queue_item.event_id)
                .where(
                    queue_item.tenant_id == tenant_id,
                    queue_item.status == "running",
                    queue_item.lease_expires_at <= func.clock_timestamp(),
                    queue_item.attempt_count >= queue_item.max_attempts,
                )
                .with_for_update(skip_locked=True, of=queue_item)
                .limit(25)
            )
        ).all()
        for item, stored_event in rows:
            await self._store_quarantine(
                session,
                stored_event,
                "projection_attempts_exhausted",
            )
            item.status = "quarantined"
            item.last_error_code = "projection_attempts_exhausted"
            _clear_lease(item)

    async def claim(
        self,
        tenant_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> ProjectionClaim | None:
        if not owner or lease_seconds <= 0:
            raise ValueError("projection lease settings are invalid")
        session_factory = self._tenant_sessions(tenant_id)
        async with session_factory() as session:
            async with session.begin():
                await self._quarantine_exhausted_leases(session, tenant_id)
                queue_item = aliased(LearningProjectionQueueItem)
                event = aliased(LearningEvent)
                prior_queue = aliased(LearningProjectionQueueItem)
                prior_event = aliased(LearningEvent)
                unfinished_prior = (
                    select(prior_event.event_id)
                    .join(prior_queue, prior_queue.event_id == prior_event.event_id)
                    .where(
                        prior_event.session_id == event.session_id,
                        prior_event.seq < event.seq,
                        prior_queue.status.not_in(_FINAL_QUEUE_STATUSES),
                    )
                    .correlate(event)
                    .exists()
                )
                available = and_(
                    queue_item.status.in_(("pending", "failed")),
                    queue_item.available_at <= func.clock_timestamp(),
                )
                expired = and_(
                    queue_item.status == "running",
                    queue_item.lease_expires_at <= func.clock_timestamp(),
                )
                row = (
                    await session.execute(
                        select(queue_item, event)
                        .join(event, event.event_id == queue_item.event_id)
                        .where(
                            queue_item.tenant_id == tenant_id,
                            queue_item.attempt_count < queue_item.max_attempts,
                            or_(available, expired),
                            ~unfinished_prior,
                        )
                        .order_by(event.received_at, event.session_id, event.seq)
                        .with_for_update(skip_locked=True, of=queue_item)
                        .limit(1)
                    )
                ).first()
                if row is None:
                    return None
                item, stored_event = row
                now = await session.scalar(select(func.clock_timestamp()))
                token = secrets.token_hex(16)
                item.status = "running"
                item.attempt_count += 1
                item.lease_owner = owner
                item.lease_token = token
                item.lease_expires_at = now + timedelta(seconds=lease_seconds)
                item.heartbeat_at = now
                item.last_error_code = None
                return ProjectionClaim(
                    event=_projection_event(stored_event),
                    lease_owner=owner,
                    lease_token=token,
                )

    @staticmethod
    async def _locked_claim(
        session: AsyncSession,
        claim: ProjectionClaim,
    ) -> tuple[LearningProjectionQueueItem, LearningEvent]:
        item = await session.scalar(
            select(LearningProjectionQueueItem)
            .where(LearningProjectionQueueItem.event_id == claim.event.event_id)
            .with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            item is None
            or item.status != "running"
            or item.lease_owner != claim.lease_owner
            or item.lease_token != claim.lease_token
            or item.lease_expires_at is None
            or item.lease_expires_at <= now
        ):
            raise ProjectionLeaseLost("projection lease is no longer owned")
        event = await session.scalar(
            select(LearningEvent).where(LearningEvent.event_id == claim.event.event_id)
        )
        if event is None or _projection_event(event) != claim.event:
            raise DeterministicProjectionError("projection_event_binding_invalid")
        return item, event

    async def heartbeat(
        self,
        claim: ProjectionClaim,
        *,
        lease_seconds: int,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("projection lease settings are invalid")
        session_factory = self._tenant_sessions(claim.event.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                item, _event = await self._locked_claim(session, claim)
                now = await session.scalar(select(func.clock_timestamp()))
                item.heartbeat_at = now
                item.lease_expires_at = now + timedelta(seconds=lease_seconds)

    @staticmethod
    async def _apply_progress(
        session: AsyncSession,
        event: ProjectionEvent,
    ) -> None:
        completed_scene_count = await session.scalar(
            select(func.count(func.distinct(LearningEvent.scene_id))).where(
                LearningEvent.session_id == event.session_id,
                LearningEvent.event_type == "scene.completed",
                LearningEvent.scene_id.is_not(None),
                LearningEvent.seq <= event.seq,
            )
        )
        progress = await session.scalar(
            select(LearningProgress)
            .where(LearningProgress.session_id == event.session_id)
            .with_for_update()
        )
        current = None
        if progress is not None:
            if progress.last_event_seq >= event.seq:
                return
            current = ProgressState(
                status=progress.status,
                last_event_id=progress.last_event_id or "",
                last_event_seq=progress.last_event_seq,
                completed_scene_count=progress.completed_scene_count,
                last_scene_id=progress.last_scene_id,
                completed_at=progress.completed_at,
            )
        projected = project_progress(
            current,
            event,
            completed_scene_count=int(completed_scene_count or 0),
        )
        if progress is None:
            session.add(
                LearningProgress(
                    session_id=event.session_id,
                    tenant_id=event.tenant_id,
                    user_id=event.user_id,
                    classroom_version_id=event.classroom_version_id,
                    status=projected.status,
                    last_event_id=projected.last_event_id,
                    last_event_seq=projected.last_event_seq,
                    completed_scene_count=projected.completed_scene_count,
                    last_scene_id=projected.last_scene_id,
                    completed_at=projected.completed_at,
                )
            )
            return
        progress.status = projected.status
        progress.last_event_id = projected.last_event_id
        progress.last_event_seq = projected.last_event_seq
        progress.completed_scene_count = projected.completed_scene_count
        progress.last_scene_id = projected.last_scene_id
        progress.completed_at = projected.completed_at

    async def project(self, claim: ProjectionClaim, *, document: object | None) -> None:
        session_factory = self._tenant_sessions(claim.event.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                item, _event = await self._locked_claim(session, claim)
                await self._apply_progress(session, claim.event)
                mastery = MasteryProjector(
                    _SessionMasteryRepository(session, claim.event.tenant_id)
                )
                await mastery.apply(claim.event, document=document)
                item.status = "completed"
                item.last_error_code = None
                _clear_lease(item)

    async def quarantine(self, claim: ProjectionClaim, *, reason_code: str) -> None:
        session_factory = self._tenant_sessions(claim.event.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                item, event = await self._locked_claim(session, claim)
                await self._store_quarantine(session, event, reason_code)
                item.status = "quarantined"
                item.last_error_code = reason_code
                _clear_lease(item)

    async def retry(self, claim: ProjectionClaim, *, error_code: str) -> None:
        session_factory = self._tenant_sessions(claim.event.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                item, event = await self._locked_claim(session, claim)
                if item.attempt_count >= item.max_attempts:
                    reason = "projection_attempts_exhausted"
                    await self._store_quarantine(session, event, reason)
                    item.status = "quarantined"
                    item.last_error_code = reason
                    _clear_lease(item)
                    return
                now = await session.scalar(select(func.clock_timestamp()))
                delay_seconds = min(60, 2 ** min(item.attempt_count, 6))
                item.status = "failed"
                item.available_at = now + timedelta(seconds=delay_seconds)
                item.last_error_code = error_code[:64]
                _clear_lease(item)


class LearningProjectionWorker:
    def __init__(
        self,
        *,
        documents: ProjectionDocuments,
        worker_id: str,
        engine: AsyncEngine | None = None,
        repository: ProjectionQueueRepository | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if repository is None:
            if engine is None:
                raise ValueError("projection repository is unavailable")
            repository = SqlAlchemyProjectionQueueRepository(engine)
        elif engine is not None:
            raise ValueError("provide either an engine or a projection repository")
        if not worker_id or lease_seconds <= 0:
            raise ValueError("projection worker settings are invalid")
        self._repository = repository
        self._documents = documents
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._tenant_cursor = 0

    async def _heartbeat_until_cancelled(self, claim: ProjectionClaim) -> None:
        interval = max(0.1, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await self._repository.heartbeat(
                claim,
                lease_seconds=self._lease_seconds,
            )

    async def _load_document_with_heartbeat(self, claim: ProjectionClaim) -> object:
        load_task = asyncio.create_task(
            self._documents.load_version_document(
                claim.event.tenant_id,
                claim.event.classroom_version_id,
            )
        )
        heartbeat_task = asyncio.create_task(self._heartbeat_until_cancelled(claim))
        try:
            done, _pending = await asyncio.wait(
                {load_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                await heartbeat_task
            return await load_task
        finally:
            for task in (load_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(load_task, heartbeat_task, return_exceptions=True)

    async def run_once(self, *, tenant_id: str | None = None) -> bool:
        if tenant_id is not None:
            tenant_ids = (tenant_id,)
            cursor_start = 0
        else:
            active = await self._repository.active_tenant_ids()
            if not active:
                return False
            cursor_start = self._tenant_cursor % len(active)
            tenant_ids = active[cursor_start:] + active[:cursor_start]
        for offset, candidate_tenant_id in enumerate(tenant_ids):
            claim = await self._repository.claim(
                candidate_tenant_id,
                owner=self._worker_id,
                lease_seconds=self._lease_seconds,
            )
            if claim is None:
                continue
            if tenant_id is None:
                self._tenant_cursor = (cursor_start + offset + 1) % len(tenant_ids)
            try:
                await self._repository.heartbeat(
                    claim,
                    lease_seconds=self._lease_seconds,
                )
                document = None
                if claim.event.event_type == "quiz.graded":
                    document = await self._load_document_with_heartbeat(claim)
                await self._repository.heartbeat(
                    claim,
                    lease_seconds=self._lease_seconds,
                )
                await self._repository.project(claim, document=document)
            except DeterministicProjectionError as exc:
                try:
                    await self._repository.quarantine(
                        claim,
                        reason_code=exc.reason_code,
                    )
                except ProjectionLeaseLost:
                    pass
            except ProjectionLeaseLost:
                pass
            except Exception as exc:
                error_code = (
                    "transient_classroom_document_unavailable"
                    if isinstance(exc, ClassroomContentUnavailable)
                    else f"transient_{type(exc).__name__.lower()}"
                )
                try:
                    await self._repository.retry(claim, error_code=error_code)
                except ProjectionLeaseLost:
                    pass
            return True
        if tenant_id is None:
            self._tenant_cursor = (cursor_start + 1) % len(tenant_ids)
        return False


__all__ = [
    "LearningProjectionWorker",
    "ProjectionClaim",
    "ProjectionLeaseLost",
    "SqlAlchemyProjectionQueueRepository",
]
