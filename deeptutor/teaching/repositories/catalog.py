"""Tenant-scoped persistence for teaching courses, classes, and enrollments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.models.platform import Tenant, TenantMembership
from deeptutor.teaching.models.tenant import Course, Enrollment, TeachingClass
from deeptutor.teaching.schema_names import tenant_schema_name


class CatalogRepositoryError(RuntimeError):
    """Base class for stable catalog persistence failures."""


class CatalogNotFoundError(CatalogRepositoryError):
    """A catalog resource does not exist in the selected tenant."""


class CatalogConflictError(CatalogRepositoryError):
    """A catalog write conflicts with existing tenant state."""


@dataclass(frozen=True, slots=True)
class CourseRecord:
    id: str
    title: str
    status: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClassRecord:
    id: str
    course_id: str
    name: str
    status: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EnrollmentRecord:
    class_id: str
    learner_id: str
    status: str
    created_at: datetime | None = None


def _course_record(model: Course) -> CourseRecord:
    return CourseRecord(model.id, model.title, model.status, model.created_at)


def _class_record(model: TeachingClass) -> ClassRecord:
    return ClassRecord(model.id, model.course_id, model.name, model.status, model.created_at)


def _enrollment_record(model: Enrollment) -> EnrollmentRecord:
    return EnrollmentRecord(model.class_id, model.learner_id, model.status, model.created_at)


class SqlAlchemyCatalogRepository:
    """Catalog repository bound to exactly one translated tenant schema."""

    def __init__(self, tenant_id: str, engine: AsyncEngine | None = None) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        translated = (engine or get_platform_engine()).execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(translated, expire_on_commit=False)

    async def list_courses(
        self,
        course_ids: frozenset[str] | None,
    ) -> tuple[CourseRecord, ...]:
        if course_ids == frozenset():
            return ()
        statement = select(Course).where(Course.status == "active")
        if course_ids is not None:
            statement = statement.where(Course.id.in_(course_ids))
        async with self._session_factory() as session:
            models = await session.scalars(statement.order_by(Course.title, Course.id))
            return tuple(_course_record(model) for model in models)

    async def list_courses_for_classes(
        self,
        class_ids: frozenset[str],
    ) -> frozenset[str]:
        if not class_ids:
            return frozenset()
        async with self._session_factory() as session:
            values = await session.scalars(
                select(TeachingClass.course_id).where(
                    TeachingClass.id.in_(class_ids),
                    TeachingClass.status == "active",
                )
            )
            return frozenset(values)

    async def get_course(self, course_id: str) -> CourseRecord:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(Course).where(Course.id == course_id, Course.status == "active")
            )
            if model is None:
                raise CatalogNotFoundError("course not found")
            return _course_record(model)

    async def create_course(self, course_id: str, title: str) -> CourseRecord:
        async with self._session_factory() as session:
            async with session.begin():
                model = Course(id=course_id, title=title)
                session.add(model)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise CatalogConflictError("course already exists") from exc
                return _course_record(model)

    async def list_classes(
        self,
        course_id: str,
        class_ids: frozenset[str] | None,
    ) -> tuple[ClassRecord, ...]:
        if class_ids == frozenset():
            return ()
        statement = select(TeachingClass).where(
            TeachingClass.course_id == course_id,
            TeachingClass.status == "active",
        )
        if class_ids is not None:
            statement = statement.where(TeachingClass.id.in_(class_ids))
        async with self._session_factory() as session:
            models = await session.scalars(statement.order_by(TeachingClass.name, TeachingClass.id))
            return tuple(_class_record(model) for model in models)

    async def get_class(self, class_id: str) -> ClassRecord:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(TeachingClass).where(
                    TeachingClass.id == class_id,
                    TeachingClass.status == "active",
                )
            )
            if model is None:
                raise CatalogNotFoundError("class not found")
            return _class_record(model)

    async def create_class(self, course_id: str, class_id: str, name: str) -> ClassRecord:
        async with self._session_factory() as session:
            async with session.begin():
                course = await session.scalar(
                    select(Course).where(Course.id == course_id, Course.status == "active")
                )
                if course is None:
                    raise CatalogNotFoundError("course not found")
                model = TeachingClass(id=class_id, course_id=course_id, name=name)
                session.add(model)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise CatalogConflictError("class already exists") from exc
                return _class_record(model)

    async def list_enrollments(
        self,
        class_id: str,
        learner_id: str | None,
    ) -> tuple[EnrollmentRecord, ...]:
        statement = select(Enrollment).where(
            Enrollment.class_id == class_id,
            Enrollment.status == "active",
        )
        if learner_id is not None:
            statement = statement.where(Enrollment.learner_id == learner_id)
        async with self._session_factory() as session:
            models = await session.scalars(statement.order_by(Enrollment.learner_id))
            return tuple(_enrollment_record(model) for model in models)

    async def list_active_enrollment_class_ids(
        self,
        course_id: str,
        learner_id: str,
    ) -> tuple[str, ...]:
        """Return active classes for one learner inside one active course."""

        statement = (
            select(TeachingClass.id)
            .join(Course, Course.id == TeachingClass.course_id)
            .join(Enrollment, Enrollment.class_id == TeachingClass.id)
            .where(
                Course.id == course_id,
                Course.status == "active",
                TeachingClass.status == "active",
                Enrollment.learner_id == learner_id,
                Enrollment.status == "active",
            )
            .order_by(TeachingClass.id)
        )
        async with self._session_factory() as session:
            return tuple(await session.scalars(statement))

    async def add_enrollment(self, class_id: str, learner_id: str) -> EnrollmentRecord:
        async with self._session_factory() as session:
            async with session.begin():
                teaching_class = await session.scalar(
                    select(TeachingClass).where(
                        TeachingClass.id == class_id,
                        TeachingClass.status == "active",
                    )
                )
                if teaching_class is None:
                    raise CatalogNotFoundError("class not found")
                active_member = await session.scalar(
                    select(TenantMembership.user_id)
                    .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                    .where(
                        TenantMembership.tenant_id == self._tenant_id,
                        TenantMembership.user_id == learner_id,
                        TenantMembership.status == "active",
                        Tenant.status == "active",
                    )
                )
                if active_member is None:
                    raise CatalogNotFoundError("learner is not an active tenant member")
                existing = await session.get(
                    Enrollment,
                    {"class_id": class_id, "learner_id": learner_id},
                )
                if existing is not None:
                    if existing.status == "active":
                        return _enrollment_record(existing)
                    existing.status = "active"
                    await session.flush()
                    return _enrollment_record(existing)
                model = Enrollment(class_id=class_id, learner_id=learner_id)
                session.add(model)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise CatalogConflictError("enrollment conflicts with existing state") from exc
                return _enrollment_record(model)

    async def remove_enrollment(self, class_id: str, learner_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    delete(Enrollment).where(
                        Enrollment.class_id == class_id,
                        Enrollment.learner_id == learner_id,
                    )
                )
                if result.rowcount != 1:
                    raise CatalogNotFoundError("enrollment not found")


__all__ = [
    "CatalogConflictError",
    "CatalogNotFoundError",
    "ClassRecord",
    "CourseRecord",
    "EnrollmentRecord",
    "SqlAlchemyCatalogRepository",
]
