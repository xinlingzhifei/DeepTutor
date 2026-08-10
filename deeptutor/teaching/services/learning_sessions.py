"""Trusted classroom learning-session lifecycle service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeVar
import uuid

from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from deeptutor.teaching.models import (
    Assignment,
    ClassLearningState,
    ClassroomAsset,
    ClassroomDraft,
    ClassroomTicketConsumption,
    ClassroomVersion,
    Enrollment,
    LearningSession,
    StudentClassroomAssetRecord,
    TeachingClass,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext
from deeptutor.teaching.tickets import (
    ClassroomTicketClaims,
    ClassroomTicketService,
    TicketReplay,
)

_ProtectedResult = TypeVar("_ProtectedResult")


class LearningSessionError(RuntimeError):
    """Base error for a rejected learning-session operation."""


class LearningSessionAuthorityError(LearningSessionError):
    """The caller did not supply exactly one supported authority reference."""


@dataclass(frozen=True, slots=True)
class LearningSessionRecord:
    id: str
    tenant_id: str
    user_id: str
    classroom_version_id: str
    assignment_id: str | None
    student_asset_id: str | None
    status: str
    last_cursor: dict[str, object] | None
    started_at: datetime
    completed_at: datetime | None


class LearningSessionService:
    """Create server-bound classroom sessions from trusted tenant context."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        ticket_service: ClassroomTicketService,
    ) -> None:
        self._engine = engine
        self._ticket_service = ticket_service

    def _session_factory(
        self,
        context: TenantContext,
    ) -> async_sessionmaker[AsyncSession]:
        expected_schema = tenant_schema_name(context.tenant_id)
        if context.schema_name != expected_schema:
            raise LearningSessionAuthorityError("tenant schema binding is invalid")
        translated = self._engine.execution_options(
            schema_translate_map={"tenant": expected_schema}
        )
        return async_sessionmaker(translated, expire_on_commit=False)

    @staticmethod
    def _record(model: LearningSession) -> LearningSessionRecord:
        return LearningSessionRecord(
            id=model.id,
            tenant_id=model.tenant_id,
            user_id=model.user_id,
            classroom_version_id=model.classroom_version_id,
            assignment_id=model.assignment_id,
            student_asset_id=model.student_asset_id,
            status=model.status,
            last_cursor=(dict(model.last_cursor) if model.last_cursor is not None else None),
            started_at=model.started_at,
            completed_at=model.completed_at,
        )

    @staticmethod
    async def _increment_class_state(
        database_session: AsyncSession,
        *,
        context: TenantContext,
        class_id: str,
    ) -> None:
        await database_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:state_key, 0))"),
            {"state_key": (f"{len(context.tenant_id)}:{context.tenant_id}{class_id}")},
        )
        state = await database_session.scalar(
            select(ClassLearningState)
            .where(
                ClassLearningState.class_id == class_id,
                ClassLearningState.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if state is None:
            database_session.add(
                ClassLearningState(
                    class_id=class_id,
                    tenant_id=context.tenant_id,
                    state="active",
                    active_session_count=1,
                    updated_by=context.user_id,
                )
            )
            return
        if state.active_session_count < 0:
            raise LearningSessionAuthorityError("class learning state is invalid")
        state.active_session_count += 1
        state.state = "active"
        state.updated_by = context.user_id

    @staticmethod
    async def _decrement_class_state(
        database_session: AsyncSession,
        *,
        context: TenantContext,
        class_id: str,
    ) -> None:
        await database_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:state_key, 0))"),
            {"state_key": (f"{len(context.tenant_id)}:{context.tenant_id}{class_id}")},
        )
        state = await database_session.scalar(
            select(ClassLearningState)
            .where(
                ClassLearningState.class_id == class_id,
                ClassLearningState.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if state is None or state.state != "active" or state.active_session_count <= 0:
            raise LearningSessionAuthorityError("class learning state is invalid")
        state.active_session_count -= 1
        state.state = "active" if state.active_session_count else "idle"
        state.updated_by = context.user_id

    @staticmethod
    async def _assignment_version(
        database_session: AsyncSession,
        *,
        context: TenantContext,
        assignment_id: str,
    ) -> tuple[str, str]:
        row = (
            await database_session.execute(
                select(Assignment, TeachingClass, Enrollment)
                .join(
                    TeachingClass,
                    TeachingClass.id == Assignment.class_id,
                )
                .join(
                    Enrollment,
                    and_(
                        Enrollment.class_id == Assignment.class_id,
                        Enrollment.learner_id == context.user_id,
                    ),
                )
                .where(
                    Assignment.id == assignment_id,
                    Assignment.tenant_id == context.tenant_id,
                    Assignment.revoked_at.is_(None),
                    TeachingClass.status == "active",
                    Enrollment.status == "active",
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise LearningSessionAuthorityError("assignment is not available to learner")
        assignment: Assignment = row[0]
        return assignment.classroom_version_id, assignment.class_id

    @staticmethod
    async def _personal_version(
        database_session: AsyncSession,
        *,
        context: TenantContext,
        student_asset_id: str,
    ) -> str:
        versions = (
            (
                await database_session.scalars(
                    select(ClassroomVersion)
                    .select_from(StudentClassroomAssetRecord)
                    .join(
                        ClassroomAsset,
                        and_(
                            ClassroomAsset.id == StudentClassroomAssetRecord.asset_id,
                            ClassroomAsset.tenant_id == StudentClassroomAssetRecord.tenant_id,
                        ),
                    )
                    .join(
                        ClassroomDraft,
                        and_(
                            ClassroomDraft.classroom_id == ClassroomAsset.id,
                            ClassroomDraft.tenant_id == ClassroomAsset.tenant_id,
                        ),
                    )
                    .join(
                        ClassroomVersion,
                        and_(
                            ClassroomVersion.generation_job_id == ClassroomDraft.generation_job_id,
                            ClassroomVersion.tenant_id == ClassroomDraft.tenant_id,
                            ClassroomVersion.classroom_id == ClassroomAsset.id,
                        ),
                    )
                    .where(
                        StudentClassroomAssetRecord.asset_id == student_asset_id,
                        StudentClassroomAssetRecord.tenant_id == context.tenant_id,
                        ClassroomAsset.owner_id == context.user_id,
                        ClassroomDraft.generation_job_id.is_not(None),
                    )
                    .with_for_update()
                )
            )
            .unique()
            .all()
        )
        if len(versions) != 1:
            raise LearningSessionAuthorityError("personal classroom version binding is unavailable")
        return versions[0].id

    async def create(
        self,
        context: TenantContext,
        *,
        assignment_id: str | None = None,
        student_asset_id: str | None = None,
    ) -> LearningSessionRecord:
        if (assignment_id is None) == (student_asset_id is None):
            raise LearningSessionAuthorityError(
                "exactly one assignment_id or student_asset_id is required"
            )
        authority_id = assignment_id if assignment_id is not None else student_asset_id
        if authority_id is None or not authority_id.strip():
            raise LearningSessionAuthorityError("authority reference must not be blank")

        session_factory = self._session_factory(context)
        async with session_factory() as database_session:
            async with database_session.begin():
                class_id: str | None = None
                if assignment_id is not None:
                    classroom_version_id, class_id = await self._assignment_version(
                        database_session,
                        context=context,
                        assignment_id=assignment_id,
                    )
                else:
                    assert student_asset_id is not None
                    classroom_version_id = await self._personal_version(
                        database_session,
                        context=context,
                        student_asset_id=student_asset_id,
                    )

                if class_id is not None:
                    await self._increment_class_state(
                        database_session,
                        context=context,
                        class_id=class_id,
                    )
                model = LearningSession(
                    id=uuid.uuid4().hex,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    classroom_version_id=classroom_version_id,
                    assignment_id=assignment_id,
                    student_asset_id=student_asset_id,
                    status="active",
                    last_cursor={"last_event_seq": 0},
                )
                database_session.add(model)
                await database_session.flush()
                await database_session.refresh(model)
                record = self._record(model)
        return record

    async def _close(
        self,
        context: TenantContext,
        *,
        session_id: str,
        target_status: Literal["completed", "abandoned"],
    ) -> LearningSessionRecord:
        if not session_id.strip():
            raise LearningSessionAuthorityError("session_id must not be blank")
        session_factory = self._session_factory(context)
        async with session_factory() as database_session:
            async with database_session.begin():
                model = await database_session.scalar(
                    select(LearningSession)
                    .where(
                        LearningSession.id == session_id,
                        LearningSession.tenant_id == context.tenant_id,
                        LearningSession.user_id == context.user_id,
                    )
                    .with_for_update()
                )
                if model is None or model.status != "active":
                    raise LearningSessionAuthorityError(
                        "learning session is not active for learner"
                    )
                if model.assignment_id is not None:
                    class_id = await database_session.scalar(
                        select(Assignment.class_id).where(
                            Assignment.id == model.assignment_id,
                            Assignment.tenant_id == context.tenant_id,
                        )
                    )
                    if class_id is None:
                        raise LearningSessionAuthorityError(
                            "learning session assignment is unavailable"
                        )
                    await self._decrement_class_state(
                        database_session,
                        context=context,
                        class_id=class_id,
                    )
                model.status = target_status
                completed_at = await database_session.scalar(select(func.clock_timestamp()))
                if completed_at is None:
                    raise LearningSessionError("database clock is unavailable")
                model.completed_at = completed_at
                await database_session.flush()
                await database_session.refresh(model)
                record = self._record(model)
        return record

    async def complete(
        self,
        context: TenantContext,
        *,
        session_id: str,
    ) -> LearningSessionRecord:
        return await self._close(
            context,
            session_id=session_id,
            target_status="completed",
        )

    async def abandon(
        self,
        context: TenantContext,
        *,
        session_id: str,
    ) -> LearningSessionRecord:
        return await self._close(
            context,
            session_id=session_id,
            target_status="abandoned",
        )

    async def issue_event_ticket(
        self,
        context: TenantContext,
        *,
        session_id: str,
        ttl_seconds: int = 300,
    ) -> str:
        session_factory = self._session_factory(context)
        async with session_factory() as database_session:
            async with database_session.begin():
                model = await database_session.scalar(
                    select(LearningSession)
                    .where(
                        LearningSession.id == session_id,
                        LearningSession.tenant_id == context.tenant_id,
                        LearningSession.user_id == context.user_id,
                        LearningSession.status == "active",
                    )
                    .with_for_update()
                )
                if model is None:
                    raise LearningSessionAuthorityError(
                        "learning session is not active for learner"
                    )
                return self._ticket_service.issue(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    session_id=model.id,
                    classroom_version_id=model.classroom_version_id,
                    allowed_action="learning_event.append",
                    ttl_seconds=ttl_seconds,
                )

    async def consume_event_ticket(
        self,
        context: TenantContext,
        *,
        session_id: str,
        token: str,
        protected_action: Callable[
            [AsyncSession, ClassroomTicketClaims],
            Awaitable[_ProtectedResult],
        ],
    ) -> _ProtectedResult:
        session_factory = self._session_factory(context)
        async with session_factory() as database_session:
            async with database_session.begin():
                model = await database_session.scalar(
                    select(LearningSession)
                    .where(
                        LearningSession.id == session_id,
                        LearningSession.tenant_id == context.tenant_id,
                        LearningSession.user_id == context.user_id,
                        LearningSession.status == "active",
                    )
                    .with_for_update()
                )
                if model is None:
                    raise LearningSessionAuthorityError(
                        "learning session is not active for learner"
                    )
                claims = self._ticket_service.verify(
                    token,
                    expected_tenant_id=context.tenant_id,
                    expected_user_id=context.user_id,
                    expected_session_id=model.id,
                    expected_version_id=model.classroom_version_id,
                    expected_action="learning_event.append",
                )
                consumed = ClassroomTicketConsumption(
                    jti=claims.jti,
                    tenant_id=context.tenant_id,
                    session_id=model.id,
                    user_id=context.user_id,
                    classroom_version_id=model.classroom_version_id,
                    allowed_action="learning_event.append",
                    issued_at=datetime.fromtimestamp(claims.iat, tz=UTC),
                    expires_at=datetime.fromtimestamp(claims.exp, tz=UTC),
                )
                database_session.add(consumed)
                try:
                    await database_session.flush()
                except IntegrityError:
                    raise TicketReplay("classroom ticket was already consumed") from None
                return await protected_action(database_session, claims)


__all__ = [
    "LearningSessionAuthorityError",
    "LearningSessionError",
    "LearningSessionRecord",
    "LearningSessionService",
]
