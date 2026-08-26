"""Tenant-scoped persistence for teaching courses, classes, and enrollments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.models.platform import Tenant, TenantMembership
from deeptutor.teaching.models.student_generation import CourseGenerationPolicyRecord
from deeptutor.teaching.models.tenant import Course, Enrollment, TeachingClass
from deeptutor.teaching.policies.student_generation import ContentMode, CourseGenerationPolicy
from deeptutor.teaching.schema_names import tenant_schema_name

_CONTENT_MODE_ORDER: tuple[ContentMode, ...] = ("source_grounded", "open_creation")


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


@dataclass(frozen=True, slots=True)
class CourseGenerationPolicyView:
    tenant_id: str
    course_id: str
    allow_student_micro: bool
    allow_student_full: bool
    allowed_content_modes: frozenset[ContentMode]
    allow_web_search: bool
    require_approval_for_restricted_topics: bool
    minor_safety_mode: bool
    micro_scene_limit: int
    full_scene_limit: int
    daily_student_units: int
    monthly_student_units: int
    updated_by: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StudentClassroomOptionBinding:
    """Internal class binding and policy facts for one enrolled course."""

    course_id: str
    title: str
    class_id: str
    allow_student_micro: bool
    allow_student_full: bool
    allowed_content_modes: str


def _course_record(model: Course) -> CourseRecord:
    return CourseRecord(model.id, model.title, model.status, model.created_at)


def _class_record(model: TeachingClass) -> ClassRecord:
    return ClassRecord(model.id, model.course_id, model.name, model.status, model.created_at)


def _enrollment_record(model: Enrollment) -> EnrollmentRecord:
    return EnrollmentRecord(model.class_id, model.learner_id, model.status, model.created_at)


def _course_generation_policy_view(
    model: CourseGenerationPolicyRecord,
) -> CourseGenerationPolicyView:
    return CourseGenerationPolicyView(
        tenant_id=model.tenant_id,
        course_id=model.course_id,
        allow_student_micro=model.allow_student_micro,
        allow_student_full=model.allow_student_full,
        allowed_content_modes=frozenset(model.allowed_content_modes.split(",")),
        allow_web_search=model.allow_web_search,
        require_approval_for_restricted_topics=model.require_approval_for_restricted_topics,
        minor_safety_mode=model.minor_safety_mode,
        micro_scene_limit=model.micro_scene_limit,
        full_scene_limit=model.full_scene_limit,
        daily_student_units=model.daily_student_units,
        monthly_student_units=model.monthly_student_units,
        updated_by=model.updated_by,
        updated_at=model.updated_at,
    )


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

    async def get_course_generation_policy(
        self,
        course_id: str,
    ) -> CourseGenerationPolicyView:
        async with self._session_factory() as session:
            active_course_id = await session.scalar(
                select(Course.id).where(Course.id == course_id, Course.status == "active")
            )
            if active_course_id is None:
                raise CatalogNotFoundError("course not found")
            model = await session.scalar(
                select(CourseGenerationPolicyRecord).where(
                    CourseGenerationPolicyRecord.course_id == course_id,
                    CourseGenerationPolicyRecord.tenant_id == self._tenant_id,
                )
            )
            if model is None:
                raise CatalogNotFoundError("course generation policy not found")
            return _course_generation_policy_view(model)

    async def replace_course_generation_policy(
        self,
        course_id: str,
        policy: CourseGenerationPolicy,
        updated_by: str,
    ) -> CourseGenerationPolicyView:
        canonical_modes = ",".join(
            mode for mode in _CONTENT_MODE_ORDER if mode in policy.allowed_content_modes
        )
        async with self._session_factory() as session:
            async with session.begin():
                active_course_id = await session.scalar(
                    select(Course.id)
                    .where(Course.id == course_id, Course.status == "active")
                    .with_for_update()
                )
                if active_course_id is None:
                    raise CatalogNotFoundError("course not found")
                model = await session.scalar(
                    select(CourseGenerationPolicyRecord)
                    .where(
                        CourseGenerationPolicyRecord.course_id == course_id,
                        CourseGenerationPolicyRecord.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if model is None:
                    model = CourseGenerationPolicyRecord(
                        course_id=course_id,
                        tenant_id=self._tenant_id,
                        allow_student_micro=policy.allow_student_micro,
                        allow_student_full=policy.allow_student_full,
                        allowed_content_modes=canonical_modes,
                        allow_web_search=policy.allow_web_search,
                        require_approval_for_restricted_topics=(
                            policy.require_approval_for_restricted_topics
                        ),
                        minor_safety_mode=policy.minor_safety_mode,
                        micro_scene_limit=policy.micro_scene_limit,
                        full_scene_limit=policy.full_scene_limit,
                        daily_student_units=policy.daily_student_units,
                        monthly_student_units=policy.monthly_student_units,
                        updated_by=updated_by,
                    )
                    session.add(model)
                    await session.flush()
                    await session.refresh(model)
                    return _course_generation_policy_view(model)

                unchanged = (
                    model.allow_student_micro == policy.allow_student_micro
                    and model.allow_student_full == policy.allow_student_full
                    and model.allowed_content_modes == canonical_modes
                    and model.allow_web_search == policy.allow_web_search
                    and model.require_approval_for_restricted_topics
                    == policy.require_approval_for_restricted_topics
                    and model.minor_safety_mode == policy.minor_safety_mode
                    and model.micro_scene_limit == policy.micro_scene_limit
                    and model.full_scene_limit == policy.full_scene_limit
                    and model.daily_student_units == policy.daily_student_units
                    and model.monthly_student_units == policy.monthly_student_units
                    and model.updated_by == updated_by
                )
                if unchanged:
                    return _course_generation_policy_view(model)

                model.allow_student_micro = policy.allow_student_micro
                model.allow_student_full = policy.allow_student_full
                model.allowed_content_modes = canonical_modes
                model.allow_web_search = policy.allow_web_search
                model.require_approval_for_restricted_topics = (
                    policy.require_approval_for_restricted_topics
                )
                model.minor_safety_mode = policy.minor_safety_mode
                model.micro_scene_limit = policy.micro_scene_limit
                model.full_scene_limit = policy.full_scene_limit
                model.daily_student_units = policy.daily_student_units
                model.monthly_student_units = policy.monthly_student_units
                model.updated_by = updated_by
                model.updated_at = datetime.now(timezone.utc)
                await session.flush()
                await session.refresh(model)
                return _course_generation_policy_view(model)

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

    async def list_student_classroom_option_bindings(
        self,
        learner_id: str,
    ) -> tuple[StudentClassroomOptionBinding, ...]:
        """Load only active enrollment rows backed by a course policy."""

        statement = (
            select(
                Course.id,
                Course.title,
                TeachingClass.id,
                CourseGenerationPolicyRecord.allow_student_micro,
                CourseGenerationPolicyRecord.allow_student_full,
                CourseGenerationPolicyRecord.allowed_content_modes,
            )
            .select_from(Course)
            .join(TeachingClass, TeachingClass.course_id == Course.id)
            .join(Enrollment, Enrollment.class_id == TeachingClass.id)
            .join(
                CourseGenerationPolicyRecord,
                CourseGenerationPolicyRecord.course_id == Course.id,
            )
            .where(
                Course.status == "active",
                TeachingClass.status == "active",
                Enrollment.learner_id == learner_id,
                Enrollment.status == "active",
                CourseGenerationPolicyRecord.tenant_id == self._tenant_id,
            )
            .order_by(Course.title, Course.id, TeachingClass.id)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            return tuple(
                StudentClassroomOptionBinding(
                    course_id=course_id,
                    title=title,
                    class_id=class_id,
                    allow_student_micro=allow_student_micro,
                    allow_student_full=allow_student_full,
                    allowed_content_modes=allowed_content_modes,
                )
                for (
                    course_id,
                    title,
                    class_id,
                    allow_student_micro,
                    allow_student_full,
                    allowed_content_modes,
                ) in rows
            )

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
    "CourseGenerationPolicyView",
    "CourseRecord",
    "EnrollmentRecord",
    "SqlAlchemyCatalogRepository",
]
