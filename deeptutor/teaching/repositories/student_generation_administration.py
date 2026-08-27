"""Platform-admin persistence for trusted student generation prerequisites."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
from typing import Literal

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.models import (
    AuditLog,
    Course,
    CourseGenerationPolicyRecord,
    QuotaLedger,
    StudentSafetyAssessmentRecord,
    TeachingClass,
    Tenant,
)
from deeptutor.teaching.schema_names import tenant_schema_name

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_PUBLIC_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,256}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class StudentGenerationAdministrationError(RuntimeError):
    """Base class for stable administration failures."""


class StudentGenerationAdministrationNotFound(StudentGenerationAdministrationError):
    """The selected tenant or catalog binding is unavailable."""


class StudentGenerationAdministrationConflict(StudentGenerationAdministrationError):
    """An idempotency key is already bound to different input."""


@dataclass(frozen=True, slots=True)
class QuotaGrantView:
    grant_id: str
    tenant_id: str
    units: int
    balance: int
    created: bool


@dataclass(frozen=True, slots=True)
class StudentSafetyAssessmentView:
    assessment_id: str
    tenant_id: str
    course_id: str
    class_id: str
    mode: str
    content_mode: str
    web_search_requested: bool
    generally_safe: bool
    minor_safe: bool
    restricted_topic: bool
    reviewed_by: str
    reviewed_at: datetime
    assessment_version: int
    expires_at: datetime
    created: bool


def _required_id(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or _PUBLIC_ID.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _idempotency_key(value: str) -> str:
    if not isinstance(value, str) or _PUBLIC_IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ValueError("idempotency_key is invalid")
    return value


def _record_id(prefix: str, tenant_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{prefix}\0{tenant_id}\0{idempotency_key}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:48]}"


def _advisory_lock_key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _safety_binding_lock_key(
    tenant_id: str,
    course_id: str,
    class_id: str,
    mode: str,
    content_mode: str,
    web_search_requested: bool,
) -> str:
    return _advisory_lock_key(
        "student-safety-assessment",
        tenant_id,
        course_id,
        class_id,
        mode,
        content_mode,
        "1" if web_search_requested else "0",
    )


def _quota_view(model: QuotaLedger, *, balance: int, created: bool) -> QuotaGrantView:
    return QuotaGrantView(
        grant_id=model.id,
        tenant_id=model.tenant_id,
        units=model.units,
        balance=balance,
        created=created,
    )


def _assessment_view(
    model: StudentSafetyAssessmentRecord,
    *,
    created: bool,
) -> StudentSafetyAssessmentView:
    return StudentSafetyAssessmentView(
        assessment_id=model.id,
        tenant_id=model.tenant_id,
        course_id=model.course_id,
        class_id=model.class_id,
        mode=model.mode,
        content_mode=model.content_mode,
        web_search_requested=model.web_search_requested,
        generally_safe=model.generally_safe,
        minor_safe=model.minor_safe,
        restricted_topic=model.restricted_topic,
        reviewed_by=model.reviewed_by,
        reviewed_at=model.reviewed_at,
        assessment_version=model.assessment_version,
        expires_at=model.requested_expires_at,
        created=created,
    )


class SqlAlchemyStudentGenerationAdministrationRepository:
    """Write idempotent quota and safety evidence in one tenant schema."""

    def __init__(
        self,
        tenant_id: str,
        engine: AsyncEngine | None = None,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._tenant_id = _required_id(tenant_id, "tenant_id", 64)
        if session_factory is None:
            translated = (engine or get_platform_engine()).execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            session_factory = async_sessionmaker(translated, expire_on_commit=False)
        self._session_factory = session_factory

    def _assessment_id(self, idempotency_key: str) -> str:
        return _record_id("safety", self._tenant_id, idempotency_key)

    async def _lock_active_tenant(self, session: AsyncSession) -> None:
        tenant_id = await session.scalar(
            select(Tenant.id)
            .where(Tenant.id == self._tenant_id, Tenant.status == "active")
            .with_for_update()
        )
        if tenant_id != self._tenant_id:
            raise StudentGenerationAdministrationNotFound("tenant is unavailable")

    async def _quota_balance(self, session: AsyncSession) -> int:
        balance = await session.scalar(
            select(func.coalesce(func.sum(QuotaLedger.units), 0)).where(
                QuotaLedger.tenant_id == self._tenant_id
            )
        )
        if isinstance(balance, bool) or not isinstance(balance, int):
            raise StudentGenerationAdministrationError("quota balance is unavailable")
        return balance

    async def grant_quota(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        units: int,
    ) -> QuotaGrantView:
        actor_id = _required_id(actor_id, "actor_id", 128)
        idempotency_key = _idempotency_key(idempotency_key)
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise ValueError("units must be a positive integer")
        grant_id = _record_id("quota", self._tenant_id, idempotency_key)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {
                        "lock_key": _advisory_lock_key(
                            "generation-quota-grant",
                            self._tenant_id,
                            grant_id,
                        )
                    },
                )
                await self._lock_active_tenant(session)
                existing = await session.scalar(
                    select(QuotaLedger).where(QuotaLedger.id == grant_id).with_for_update()
                )
                if existing is not None:
                    if (
                        existing.tenant_id != self._tenant_id
                        or existing.job_id is not None
                        or existing.entry_type != "grant"
                        or existing.units != units
                    ):
                        raise StudentGenerationAdministrationConflict(
                            "quota grant idempotency conflict"
                        )
                    return _quota_view(
                        existing,
                        balance=await self._quota_balance(session),
                        created=False,
                    )
                grant = QuotaLedger(
                    id=grant_id,
                    tenant_id=self._tenant_id,
                    job_id=None,
                    entry_type="grant",
                    units=units,
                )
                session.add_all(
                    (
                        grant,
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=actor_id,
                            action="teaching.generation_quota.granted",
                            resource_type="quota_grant",
                            resource_id=grant_id,
                        ),
                    )
                )
                await session.flush()
                return _quota_view(
                    grant,
                    balance=await self._quota_balance(session),
                    created=True,
                )

    @staticmethod
    def _matches_assessment(
        model: StudentSafetyAssessmentRecord,
        *,
        tenant_id: str,
        course_id: str,
        class_id: str,
        mode: str,
        content_mode: str,
        web_search_requested: bool,
        generally_safe: bool,
        minor_safe: bool,
        restricted_topic: bool,
        valid_for_seconds: int,
    ) -> bool:
        return (
            model.tenant_id == tenant_id
            and model.course_id == course_id
            and model.class_id == class_id
            and model.mode == mode
            and model.content_mode == content_mode
            and model.web_search_requested is web_search_requested
            and model.generally_safe is generally_safe
            and model.minor_safe is minor_safe
            and model.restricted_topic is restricted_topic
            and model.valid_for_seconds == valid_for_seconds
        )

    async def _lock_catalog_binding(
        self,
        session: AsyncSession,
        *,
        course_id: str,
        class_id: str,
    ) -> None:
        active_class_id = await session.scalar(
            select(TeachingClass.id)
            .join(Course, Course.id == TeachingClass.course_id)
            .join(
                CourseGenerationPolicyRecord,
                and_(
                    CourseGenerationPolicyRecord.course_id == Course.id,
                    CourseGenerationPolicyRecord.tenant_id == self._tenant_id,
                ),
            )
            .where(
                TeachingClass.id == class_id,
                TeachingClass.course_id == course_id,
                TeachingClass.status == "active",
                Course.status == "active",
            )
            .with_for_update()
        )
        if active_class_id != class_id:
            raise StudentGenerationAdministrationNotFound(
                "student safety catalog binding is unavailable"
            )

    @staticmethod
    def _end_current_assessments(
        current: Iterable[StudentSafetyAssessmentRecord],
        decision_time: datetime,
    ) -> None:
        for assessment in current:
            assessment.expires_at = decision_time

    async def create_safety_assessment(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        course_id: str,
        class_id: str,
        mode: Literal["micro", "full"],
        content_mode: Literal["source_grounded", "open_creation"],
        web_search_requested: bool,
        generally_safe: bool,
        minor_safe: bool,
        restricted_topic: bool,
        valid_for_seconds: int,
    ) -> StudentSafetyAssessmentView:
        actor_id = _required_id(actor_id, "actor_id", 128)
        idempotency_key = _idempotency_key(idempotency_key)
        course_id = _required_id(course_id, "course_id", 64)
        class_id = _required_id(class_id, "class_id", 64)
        if mode not in {"micro", "full"}:
            raise ValueError("mode is invalid")
        if content_mode not in {"source_grounded", "open_creation"}:
            raise ValueError("content_mode is invalid")
        flags = (
            web_search_requested,
            generally_safe,
            minor_safe,
            restricted_topic,
        )
        if any(not isinstance(value, bool) for value in flags):
            raise ValueError("student safety flags are invalid")
        if (
            isinstance(valid_for_seconds, bool)
            or not isinstance(valid_for_seconds, int)
            or not 60 <= valid_for_seconds <= 86_400
        ):
            raise ValueError("valid_for_seconds is invalid")
        assessment_id = self._assessment_id(idempotency_key)
        lock_keys = sorted(
            {
                _advisory_lock_key(
                    "student-safety-idempotency",
                    self._tenant_id,
                    assessment_id,
                ),
                _safety_binding_lock_key(
                    self._tenant_id,
                    course_id,
                    class_id,
                    mode,
                    content_mode,
                    web_search_requested,
                ),
            }
        )
        async with self._session_factory() as session:
            async with session.begin():
                for lock_key in lock_keys:
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                        {"lock_key": lock_key},
                    )
                await self._lock_active_tenant(session)
                existing = await session.scalar(
                    select(StudentSafetyAssessmentRecord)
                    .where(StudentSafetyAssessmentRecord.id == assessment_id)
                    .with_for_update()
                )
                if existing is not None:
                    if not self._matches_assessment(
                        existing,
                        tenant_id=self._tenant_id,
                        course_id=course_id,
                        class_id=class_id,
                        mode=mode,
                        content_mode=content_mode,
                        web_search_requested=web_search_requested,
                        generally_safe=generally_safe,
                        minor_safe=minor_safe,
                        restricted_topic=restricted_topic,
                        valid_for_seconds=valid_for_seconds,
                    ):
                        raise StudentGenerationAdministrationConflict(
                            "student safety idempotency conflict"
                        )
                    return _assessment_view(existing, created=False)
                await self._lock_catalog_binding(
                    session,
                    course_id=course_id,
                    class_id=class_id,
                )
                decision_time = await session.scalar(text("SELECT clock_timestamp()"))
                if not isinstance(decision_time, datetime) or decision_time.tzinfo is None:
                    raise StudentGenerationAdministrationError(
                        "student safety database time is unavailable"
                    )
                current = tuple(
                    await session.scalars(
                        select(StudentSafetyAssessmentRecord)
                        .where(
                            StudentSafetyAssessmentRecord.tenant_id == self._tenant_id,
                            StudentSafetyAssessmentRecord.course_id == course_id,
                            StudentSafetyAssessmentRecord.class_id == class_id,
                            StudentSafetyAssessmentRecord.mode == mode,
                            StudentSafetyAssessmentRecord.content_mode == content_mode,
                            StudentSafetyAssessmentRecord.web_search_requested
                            == web_search_requested,
                            StudentSafetyAssessmentRecord.reviewed_at <= decision_time,
                            StudentSafetyAssessmentRecord.expires_at > decision_time,
                        )
                        .with_for_update()
                    )
                )
                version = await session.scalar(
                    select(
                        func.coalesce(
                            func.max(StudentSafetyAssessmentRecord.assessment_version),
                            0,
                        )
                    ).where(
                        StudentSafetyAssessmentRecord.tenant_id == self._tenant_id,
                        StudentSafetyAssessmentRecord.course_id == course_id,
                        StudentSafetyAssessmentRecord.class_id == class_id,
                        StudentSafetyAssessmentRecord.mode == mode,
                        StudentSafetyAssessmentRecord.content_mode == content_mode,
                        StudentSafetyAssessmentRecord.web_search_requested == web_search_requested,
                    )
                )
                if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                    raise StudentGenerationAdministrationError(
                        "student safety assessment version is unavailable"
                    )
                self._end_current_assessments(current, decision_time)
                requested_expires_at = decision_time + timedelta(seconds=valid_for_seconds)
                assessment = StudentSafetyAssessmentRecord(
                    id=assessment_id,
                    tenant_id=self._tenant_id,
                    course_id=course_id,
                    class_id=class_id,
                    mode=mode,
                    content_mode=content_mode,
                    web_search_requested=web_search_requested,
                    generally_safe=generally_safe,
                    minor_safe=minor_safe,
                    restricted_topic=restricted_topic,
                    reviewed_by=actor_id,
                    reviewed_at=decision_time,
                    assessment_version=version + 1,
                    valid_for_seconds=valid_for_seconds,
                    requested_expires_at=requested_expires_at,
                    expires_at=requested_expires_at,
                )
                session.add_all(
                    (
                        assessment,
                        AuditLog(
                            tenant_id=self._tenant_id,
                            actor_id=actor_id,
                            action="teaching.student_safety.assessed",
                            resource_type="student_safety_assessment",
                            resource_id=assessment_id,
                        ),
                    )
                )
                await session.flush()
                return _assessment_view(assessment, created=True)
