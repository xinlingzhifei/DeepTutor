"""Atomic SQLAlchemy persistence for trusted PBL teacher grading results."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.models import (
    Assignment,
    ClassroomVersion,
    LearningEvent,
    LearningProjectionQueueItem,
    LearningSession,
    PblGradingIdempotencyKey,
    PblGradingResult,
    TeachingClass,
)
from deeptutor.teaching.repositories.metric_rollups import (
    insert_learning_projection_backlog,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.pbl_grading import (
    PblGradingBinding,
    PblGradingCommand,
    PblGradingConflict,
    PblGradingDocumentLoader,
    PblGradingRecord,
    PblGradingValidationError,
    derive_pbl_evaluation,
    projection_queue_action,
    require_grading_permission,
    resolve_existing_result,
)
from deeptutor.teaching.tenant_context import TenantContext


def _record(model: PblGradingResult) -> PblGradingRecord:
    return PblGradingRecord(
        result_id=model.id,
        event_id=model.event_id,
        passed=bool(model.correctness),
        score=float(model.score) if model.score is not None else None,
        source_reference=model.source_reference,
        grading_source=model.grading_source,
        graded_at=model.graded_at,
        request_sha256=model.request_sha256,
    )


class SqlAlchemyPblGradingRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _sessions(self, tenant_id: str) -> async_sessionmaker[AsyncSession]:
        translated = self._engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        return async_sessionmaker(translated, expire_on_commit=False)

    @staticmethod
    async def _existing(
        session: AsyncSession,
        tenant_id: str,
        command: PblGradingCommand,
    ) -> PblGradingRecord | None:
        by_key = await session.scalar(
            select(PblGradingResult)
            .join(
                PblGradingIdempotencyKey,
                PblGradingIdempotencyKey.result_id == PblGradingResult.id,
            )
            .where(
                PblGradingIdempotencyKey.tenant_id == tenant_id,
                PblGradingIdempotencyKey.idempotency_key == command.idempotency_key,
            )
        )
        by_event = await session.scalar(
            select(PblGradingResult).where(PblGradingResult.event_id == command.event_id)
        )
        return resolve_existing_result(
            existing_by_key=_record(by_key) if by_key is not None else None,
            existing_by_event=_record(by_event) if by_event is not None else None,
            request_sha256=command.request_sha256,
        )

    @staticmethod
    async def _bind_idempotency_key(
        session: AsyncSession,
        *,
        tenant_id: str,
        command: PblGradingCommand,
        result: PblGradingRecord,
    ) -> None:
        statement = (
            postgresql_insert(PblGradingIdempotencyKey)
            .values(
                tenant_id=tenant_id,
                idempotency_key=command.idempotency_key,
                result_id=result.result_id,
                event_id=result.event_id,
                request_sha256=result.request_sha256,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    PblGradingIdempotencyKey.tenant_id,
                    PblGradingIdempotencyKey.idempotency_key,
                ]
            )
            .returning(PblGradingIdempotencyKey.result_id)
        )
        inserted = await session.scalar(statement)
        if inserted is not None:
            return
        existing = await session.scalar(
            select(PblGradingIdempotencyKey).where(
                PblGradingIdempotencyKey.tenant_id == tenant_id,
                PblGradingIdempotencyKey.idempotency_key == command.idempotency_key,
            )
        )
        if existing is None or (
            existing.result_id,
            existing.event_id,
            existing.request_sha256,
        ) != (
            result.result_id,
            result.event_id,
            result.request_sha256,
        ):
            raise PblGradingConflict("PBL grading result conflicts")

    async def record(
        self,
        context: TenantContext,
        *,
        session_id: str,
        command: PblGradingCommand,
        documents: PblGradingDocumentLoader,
    ) -> PblGradingRecord:
        session_id = session_id.strip()
        if not session_id:
            raise PblGradingValidationError("session id is invalid")
        session_factory = self._sessions(context.tenant_id)
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(
                                f"pbl-grade:{context.tenant_id}:{command.idempotency_key}",
                                0,
                            )
                        )
                    )
                )
                queue_item = await session.scalar(
                    select(LearningProjectionQueueItem)
                    .where(LearningProjectionQueueItem.event_id == command.event_id)
                    .with_for_update()
                )
                if queue_item is None:
                    raise PblGradingValidationError("PBL event is unavailable")
                event = await session.scalar(
                    select(LearningEvent)
                    .where(LearningEvent.event_id == command.event_id)
                    .with_for_update()
                )
                if (
                    event is None
                    or event.session_id != session_id
                    or queue_item.tenant_id != event.tenant_id
                    or queue_item.session_id != event.session_id
                ):
                    raise PblGradingValidationError("PBL event binding is invalid")
                learning_session = await session.scalar(
                    select(LearningSession)
                    .where(LearningSession.id == event.session_id)
                    .with_for_update()
                )
                if learning_session is None:
                    raise PblGradingValidationError("PBL session binding is invalid")
                classroom_version = await session.scalar(
                    select(ClassroomVersion)
                    .where(ClassroomVersion.id == learning_session.classroom_version_id)
                    .with_for_update()
                )
                if classroom_version is None or classroom_version.tenant_id != context.tenant_id:
                    raise PblGradingValidationError("PBL classroom version is invalid")
                assignment = None
                teaching_class = None
                if learning_session.assignment_id is not None:
                    assignment = await session.scalar(
                        select(Assignment)
                        .where(Assignment.id == learning_session.assignment_id)
                        .with_for_update()
                    )
                    if assignment is not None:
                        teaching_class = await session.scalar(
                            select(TeachingClass)
                            .where(TeachingClass.id == assignment.class_id)
                            .with_for_update()
                        )
                binding = PblGradingBinding(
                    event_id=event.event_id,
                    event_tenant_id=event.tenant_id,
                    event_session_id=event.session_id,
                    event_user_id=event.user_id,
                    event_classroom_version_id=event.classroom_version_id,
                    document_version_id=(
                        classroom_version.source_version_id or classroom_version.id
                    ),
                    event_type=event.event_type,
                    event_scene_id=event.scene_id,
                    event_knowledge_point_id=event.knowledge_point_id,
                    event_payload=dict(event.payload),
                    session_id=learning_session.id,
                    session_tenant_id=learning_session.tenant_id,
                    session_user_id=learning_session.user_id,
                    session_classroom_version_id=learning_session.classroom_version_id,
                    assignment_id=assignment.id if assignment is not None else None,
                    assignment_tenant_id=(assignment.tenant_id if assignment is not None else None),
                    assignment_classroom_version_id=(
                        assignment.classroom_version_id if assignment is not None else None
                    ),
                    course_id=(teaching_class.course_id if teaching_class is not None else None),
                    class_id=(assignment.class_id if assignment is not None else None),
                )
                require_grading_permission(context, binding)
                existing = await self._existing(session, context.tenant_id, command)
                if existing is not None:
                    await self._bind_idempotency_key(
                        session,
                        tenant_id=context.tenant_id,
                        command=command,
                        result=existing,
                    )
                    return existing
                queue_action = projection_queue_action(queue_item.status)
                document = await documents.load_version_document(
                    context,
                    learning_session.classroom_version_id,
                )
                evaluation = derive_pbl_evaluation(
                    binding,
                    document,
                    passed=command.passed,
                    score=command.score,
                )
                now = await session.scalar(select(func.clock_timestamp()))
                if now is None:
                    raise RuntimeError("database clock is unavailable")
                model = PblGradingResult(
                    id=f"pbl-result-{uuid4().hex}",
                    event_id=event.event_id,
                    tenant_id=context.tenant_id,
                    session_id=event.session_id,
                    user_id=event.user_id,
                    classroom_version_id=event.classroom_version_id,
                    document_version_id=evaluation.document_version_id,
                    scene_id=evaluation.scene_id,
                    milestone_id=evaluation.milestone_id,
                    knowledge_point_id=evaluation.knowledge_point_id,
                    rubric_sha256=evaluation.rubric_sha256,
                    correctness=evaluation.correct,
                    score=evaluation.score,
                    grading_source="teacher_review",
                    source_reference=command.source_reference,
                    graded_by=context.user_id,
                    graded_at=now,
                    idempotency_key=command.idempotency_key,
                    request_sha256=command.request_sha256,
                )
                session.add(model)
                await session.flush()
                record = _record(model)
                await self._bind_idempotency_key(
                    session,
                    tenant_id=context.tenant_id,
                    command=command,
                    result=record,
                )
                if queue_action in {"requeue", "retry_now"}:
                    queue_item.status = "pending"
                    queue_item.available_at = now
                    queue_item.lease_owner = None
                    queue_item.lease_token = None
                    queue_item.lease_expires_at = None
                    queue_item.heartbeat_at = None
                    queue_item.last_error_code = None
                if queue_action == "requeue":
                    queue_item.attempt_count = 0
                    await insert_learning_projection_backlog(
                        session,
                        tenant_id=context.tenant_id,
                        event_id=event.event_id,
                        received_at=event.received_at,
                    )
                return record


__all__ = ["SqlAlchemyPblGradingRepository"]
