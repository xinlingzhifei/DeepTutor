"""Resource-scoped classroom learning report queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.learning.mastery import compute_mastery
from deeptutor.teaching.models import (
    Assignment,
    ClassroomVersion,
    LearningEvent,
    LearningEventQuarantine,
    LearningProgress,
    LearningProjectionQueueItem,
    LearningSession,
    MasteryEvidence,
    QuizAttempt,
    TeachingClass,
)
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext


class TeachingReportError(RuntimeError):
    """Base error for learning-report reads."""


class TeachingReportAccessDenied(TeachingReportError):
    """The caller has no report permission for the requested resource."""


class TeachingReportNotFound(TeachingReportError):
    """The requested report resource does not exist in the selected tenant."""


@dataclass(frozen=True, slots=True)
class LearningReportMetrics:
    session_count: int
    completed_count: int
    completion_rate: float
    completed_scene_count: int
    valid_quiz_count: int
    correct_quiz_count: int
    hint_count: int
    pbl_milestone_count: int
    mastery: tuple[dict[str, object], ...]
    projection_lag_seconds: float


@dataclass(frozen=True, slots=True)
class QuarantinedLearningEvent:
    event_id: str
    event_type: str
    classroom_version_id: str
    reason_code: str
    quarantined_at: datetime | str
    knowledge_point_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReportAccessScope:
    """Concrete SQL filter derived from trusted permission grants."""

    tenant_wide: bool = False
    course_ids: frozenset[str] = frozenset()
    class_ids: frozenset[str] = frozenset()


class TeachingReportRepository(Protocol):
    async def class_scope(
        self,
        tenant_id: str,
        class_id: str,
    ) -> ResourceScope | None: ...

    async def class_report(
        self,
        tenant_id: str,
        class_id: str,
        user_id: str | None = None,
    ) -> LearningReportMetrics: ...

    async def student_in_class(
        self,
        tenant_id: str,
        class_id: str,
        user_id: str,
    ) -> bool: ...

    async def version_scopes(
        self,
        tenant_id: str,
        version_id: str,
    ) -> tuple[ResourceScope, ...] | None: ...

    async def classroom_report(
        self,
        tenant_id: str,
        version_id: str,
        access: ReportAccessScope,
    ) -> LearningReportMetrics: ...

    async def quarantine(
        self,
        tenant_id: str,
        access: ReportAccessScope,
    ) -> tuple[QuarantinedLearningEvent | dict[str, object], ...]: ...


def _allows(context: TenantContext, resource: ResourceScope) -> bool:
    return any(
        permission.allows_resource("learning_event.read", resource)
        for permission in context.permissions
    )


def _validate_context(context: TenantContext) -> None:
    if context.schema_name != tenant_schema_name(context.tenant_id):
        raise TeachingReportAccessDenied("learning report access denied")


def _report_access(context: TenantContext) -> ReportAccessScope:
    tenant_wide = False
    course_ids: set[str] = set()
    class_ids: set[str] = set()
    for permission in context.permissions:
        if (
            permission.permission != "learning_event.read"
            or permission.tenant_id != context.tenant_id
        ):
            continue
        if permission.scope_type == "tenant" and permission.scope_id == context.tenant_id:
            tenant_wide = True
        elif permission.scope_type == "course":
            course_ids.add(permission.scope_id)
        elif permission.scope_type == "class":
            class_ids.add(permission.scope_id)
    if not tenant_wide and not course_ids and not class_ids:
        raise TeachingReportAccessDenied("learning report access denied")
    return ReportAccessScope(
        tenant_wide=tenant_wide,
        course_ids=frozenset(course_ids),
        class_ids=frozenset(class_ids),
    )


class TeachingReportService:
    """Authorize resource ancestry before issuing narrowed repository reads."""

    def __init__(self, repository: TeachingReportRepository) -> None:
        self._repository = repository

    async def class_report(
        self,
        context: TenantContext,
        class_id: str,
        *,
        user_id: str | None = None,
    ) -> LearningReportMetrics:
        _validate_context(context)
        resource = await self._repository.class_scope(context.tenant_id, class_id)
        if resource is None:
            raise TeachingReportNotFound("class report not found")
        if resource.tenant_id != context.tenant_id or not _allows(context, resource):
            raise TeachingReportAccessDenied("class report access denied")
        if user_id is not None and not await self._repository.student_in_class(
            context.tenant_id,
            class_id,
            user_id,
        ):
            raise TeachingReportNotFound("student report not found")
        return await self._repository.class_report(
            context.tenant_id,
            class_id,
            user_id,
        )

    async def classroom_report(
        self,
        context: TenantContext,
        version_id: str,
    ) -> LearningReportMetrics:
        _validate_context(context)
        resources = await self._repository.version_scopes(context.tenant_id, version_id)
        if resources is None:
            raise TeachingReportNotFound("classroom report not found")
        allowed_class_ids = frozenset(
            resource.class_id
            for resource in resources
            if resource.tenant_id == context.tenant_id
            and resource.class_id is not None
            and _allows(context, resource)
        )
        tenant_wide = any(
            resource.tenant_id == context.tenant_id
            and resource.course_id is None
            and resource.class_id is None
            and _allows(context, resource)
            for resource in resources
        )
        if not tenant_wide and not allowed_class_ids:
            raise TeachingReportAccessDenied("classroom report access denied")
        return await self._repository.classroom_report(
            context.tenant_id,
            version_id,
            ReportAccessScope(
                tenant_wide=tenant_wide,
                class_ids=allowed_class_ids,
            ),
        )

    async def quarantine(
        self,
        context: TenantContext,
    ) -> tuple[QuarantinedLearningEvent, ...]:
        _validate_context(context)
        access = _report_access(context)
        rows = await self._repository.quarantine(context.tenant_id, access)
        sanitized: list[QuarantinedLearningEvent] = []
        for row in rows:
            if isinstance(row, QuarantinedLearningEvent):
                sanitized.append(row)
                continue
            sanitized.append(
                QuarantinedLearningEvent(
                    event_id=str(row["event_id"]),
                    event_type=str(row["event_type"]),
                    classroom_version_id=str(row["classroom_version_id"]),
                    reason_code=str(row["reason_code"]),
                    quarantined_at=row["quarantined_at"],  # type: ignore[arg-type]
                    knowledge_point_id=(
                        str(row["knowledge_point_id"])
                        if row.get("knowledge_point_id") is not None
                        else None
                    ),
                )
            )
        return tuple(sanitized)


@dataclass(frozen=True, slots=True)
class _ReportSelection:
    class_ids: frozenset[str]
    version_id: str | None = None
    user_id: str | None = None
    include_personal: bool = False


class SqlAlchemyTeachingReportRepository:
    """Tenant-schema report reads whose filters are fixed before querying facts."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _sessions(self, tenant_id: str) -> async_sessionmaker[AsyncSession]:
        translated = self._engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        return async_sessionmaker(translated, expire_on_commit=False)

    async def class_scope(
        self,
        tenant_id: str,
        class_id: str,
    ) -> ResourceScope | None:
        async with self._sessions(tenant_id)() as session:
            course_id = await session.scalar(
                select(TeachingClass.course_id).where(TeachingClass.id == class_id)
            )
        if course_id is None:
            return None
        return ResourceScope(tenant_id, course_id, class_id)

    async def student_in_class(
        self,
        tenant_id: str,
        class_id: str,
        user_id: str,
    ) -> bool:
        async with self._sessions(tenant_id)() as session:
            learning_session = await session.scalar(
                select(LearningSession.id)
                .join(Assignment, Assignment.id == LearningSession.assignment_id)
                .where(
                    LearningSession.tenant_id == tenant_id,
                    LearningSession.user_id == user_id,
                    Assignment.tenant_id == tenant_id,
                    Assignment.class_id == class_id,
                )
                .limit(1)
            )
        return learning_session is not None

    async def version_scopes(
        self,
        tenant_id: str,
        version_id: str,
    ) -> tuple[ResourceScope, ...] | None:
        async with self._sessions(tenant_id)() as session:
            exists = await session.scalar(
                select(ClassroomVersion.id).where(
                    ClassroomVersion.id == version_id,
                    ClassroomVersion.tenant_id == tenant_id,
                )
            )
            if exists is None:
                return None
            rows = (
                await session.execute(
                    select(TeachingClass.course_id, TeachingClass.id)
                    .join(Assignment, Assignment.class_id == TeachingClass.id)
                    .where(
                        Assignment.classroom_version_id == version_id,
                        Assignment.tenant_id == tenant_id,
                    )
                    .distinct()
                    .order_by(TeachingClass.id)
                )
            ).all()
            personal = await session.scalar(
                select(LearningSession.id)
                .where(
                    LearningSession.tenant_id == tenant_id,
                    LearningSession.classroom_version_id == version_id,
                    LearningSession.student_asset_id.is_not(None),
                )
                .limit(1)
            )
        resources = [ResourceScope(tenant_id, course_id, class_id) for course_id, class_id in rows]
        if personal is not None:
            resources.append(ResourceScope(tenant_id))
        return tuple(resources)

    @staticmethod
    def _session_ids(tenant_id: str, selection: _ReportSelection):
        if selection.include_personal:
            statement = select(LearningSession.id).where(LearningSession.tenant_id == tenant_id)
        else:
            statement = (
                select(LearningSession.id)
                .join(Assignment, Assignment.id == LearningSession.assignment_id)
                .where(
                    LearningSession.tenant_id == tenant_id,
                    Assignment.tenant_id == tenant_id,
                    Assignment.class_id.in_(selection.class_ids),
                )
            )
        if selection.version_id is not None:
            statement = statement.where(
                LearningSession.classroom_version_id == selection.version_id
            )
        if selection.user_id is not None:
            statement = statement.where(LearningSession.user_id == selection.user_id)
        return statement.subquery()

    @staticmethod
    async def _mastery(
        session: AsyncSession,
        tenant_id: str,
        session_ids,
    ) -> tuple[dict[str, object], ...]:
        rows = (
            await session.execute(
                select(
                    MasteryEvidence.user_id,
                    MasteryEvidence.knowledge_point_id,
                    MasteryEvidence.correctness,
                )
                .join(LearningEvent, LearningEvent.event_id == MasteryEvidence.event_id)
                .join(LearningSession, LearningSession.id == LearningEvent.session_id)
                .where(MasteryEvidence.session_id.in_(select(session_ids.c.id)))
                .where(
                    MasteryEvidence.tenant_id == tenant_id,
                    LearningEvent.tenant_id == tenant_id,
                    LearningSession.tenant_id == tenant_id,
                )
                .order_by(
                    MasteryEvidence.user_id,
                    MasteryEvidence.knowledge_point_id,
                    LearningSession.started_at,
                    LearningEvent.occurred_at,
                    LearningEvent.seq,
                    LearningEvent.event_id,
                )
            )
        ).all()
        by_learner: dict[tuple[str, str], list[bool]] = {}
        for user_id, knowledge_point_id, correctness in rows:
            by_learner.setdefault((user_id, knowledge_point_id), []).append(bool(correctness))
        by_knowledge: dict[str, list[tuple[float, int]]] = {}
        for (_user_id, knowledge_point_id), correctness in by_learner.items():
            by_knowledge.setdefault(knowledge_point_id, []).append(
                (compute_mastery(correctness), len(correctness))
            )
        return tuple(
            {
                "knowledge_point_id": knowledge_point_id,
                "level": sum(level for level, _count in values) / len(values),
                "evidence_count": sum(count for _level, count in values),
            }
            for knowledge_point_id, values in sorted(by_knowledge.items())
        )

    async def _report(self, tenant_id: str, selection: _ReportSelection) -> LearningReportMetrics:
        session_ids = self._session_ids(tenant_id, selection)
        async with self._sessions(tenant_id)() as session:
            session_count, completed_count = (
                await session.execute(
                    select(
                        func.count(LearningSession.id),
                        func.count(LearningSession.id).filter(
                            LearningSession.status == "completed"
                        ),
                    ).where(LearningSession.id.in_(select(session_ids.c.id)))
                )
            ).one()
            completed_scene_count = await session.scalar(
                select(func.coalesce(func.sum(LearningProgress.completed_scene_count), 0)).where(
                    LearningProgress.tenant_id == tenant_id,
                    LearningProgress.session_id.in_(select(session_ids.c.id)),
                )
            )
            valid_quiz_count, correct_quiz_count = (
                await session.execute(
                    select(
                        func.count(QuizAttempt.id),
                        func.count(QuizAttempt.id).filter(QuizAttempt.is_correct.is_(True)),
                    ).where(
                        QuizAttempt.tenant_id == tenant_id,
                        QuizAttempt.session_id.in_(select(session_ids.c.id)),
                    )
                )
            ).one()
            hint_count, pbl_milestone_count = (
                await session.execute(
                    select(
                        func.count(LearningEvent.id).filter(
                            LearningEvent.event_type == "hint.used"
                        ),
                        func.count(LearningEvent.id).filter(
                            LearningEvent.event_type == "pbl.milestone_completed"
                        ),
                    ).where(
                        LearningEvent.tenant_id == tenant_id,
                        LearningEvent.session_id.in_(select(session_ids.c.id)),
                    )
                )
            ).one()
            projection_lag = await session.scalar(
                select(
                    func.coalesce(
                        func.max(
                            func.greatest(
                                0.0,
                                func.extract(
                                    "epoch",
                                    case(
                                        (
                                            LearningProjectionQueueItem.status.in_(
                                                ("completed", "quarantined")
                                            ),
                                            LearningProjectionQueueItem.updated_at,
                                        ),
                                        else_=func.clock_timestamp(),
                                    )
                                    - LearningEvent.received_at,
                                ),
                            )
                        ),
                        0.0,
                    )
                )
                .select_from(LearningProjectionQueueItem)
                .join(
                    LearningEvent,
                    LearningEvent.event_id == LearningProjectionQueueItem.event_id,
                )
                .where(LearningEvent.session_id.in_(select(session_ids.c.id)))
                .where(
                    LearningProjectionQueueItem.tenant_id == tenant_id,
                    LearningEvent.tenant_id == tenant_id,
                )
            )
            mastery = await self._mastery(session, tenant_id, session_ids)
        sessions = int(session_count or 0)
        completed = int(completed_count or 0)
        return LearningReportMetrics(
            session_count=sessions,
            completed_count=completed,
            completion_rate=completed / sessions if sessions else 0.0,
            completed_scene_count=int(completed_scene_count or 0),
            valid_quiz_count=int(valid_quiz_count or 0),
            correct_quiz_count=int(correct_quiz_count or 0),
            hint_count=int(hint_count or 0),
            pbl_milestone_count=int(pbl_milestone_count or 0),
            mastery=mastery,
            projection_lag_seconds=float(projection_lag or 0.0),
        )

    async def class_report(
        self,
        tenant_id: str,
        class_id: str,
        user_id: str | None = None,
    ) -> LearningReportMetrics:
        return await self._report(
            tenant_id,
            _ReportSelection(frozenset({class_id}), user_id=user_id),
        )

    async def classroom_report(
        self,
        tenant_id: str,
        version_id: str,
        access: ReportAccessScope,
    ) -> LearningReportMetrics:
        include_personal = access.tenant_wide
        return await self._report(
            tenant_id,
            _ReportSelection(
                access.class_ids,
                version_id=version_id,
                include_personal=include_personal,
            ),
        )

    async def quarantine(
        self,
        tenant_id: str,
        access: ReportAccessScope,
    ) -> tuple[QuarantinedLearningEvent, ...]:
        conditions = []
        if not access.tenant_wide:
            if access.course_ids:
                conditions.append(TeachingClass.course_id.in_(access.course_ids))
            if access.class_ids:
                conditions.append(Assignment.class_id.in_(access.class_ids))
            if not conditions:
                return ()
        statement = (
            select(LearningEventQuarantine)
            .join(LearningSession, LearningSession.id == LearningEventQuarantine.session_id)
            .outerjoin(
                Assignment,
                and_(
                    Assignment.id == LearningSession.assignment_id,
                    Assignment.tenant_id == tenant_id,
                ),
            )
            .outerjoin(TeachingClass, TeachingClass.id == Assignment.class_id)
            .where(
                LearningEventQuarantine.tenant_id == tenant_id,
                LearningSession.tenant_id == tenant_id,
            )
        )
        if conditions:
            statement = statement.where(or_(*conditions))
        statement = statement.order_by(
            LearningEventQuarantine.quarantined_at.desc(),
            LearningEventQuarantine.event_id,
        ).limit(200)
        async with self._sessions(tenant_id)() as session:
            rows = tuple(await session.scalars(statement))
        return tuple(
            QuarantinedLearningEvent(
                event_id=row.event_id,
                event_type=row.event_type,
                classroom_version_id=row.classroom_version_id,
                reason_code=row.reason_code,
                quarantined_at=row.quarantined_at,
                knowledge_point_id=row.knowledge_point_id,
            )
            for row in rows
        )


__all__ = [
    "LearningReportMetrics",
    "QuarantinedLearningEvent",
    "ReportAccessScope",
    "SqlAlchemyTeachingReportRepository",
    "TeachingReportAccessDenied",
    "TeachingReportError",
    "TeachingReportNotFound",
    "TeachingReportRepository",
    "TeachingReportService",
]
