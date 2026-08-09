"""Trusted runtime wiring for interactive student classrooms."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from deeptutor.teaching.models.student_generation import CourseGenerationPolicyRecord
from deeptutor.teaching.models.tenant import Course, Enrollment, TeachingClass
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.repositories.catalog import (
    SqlAlchemyCatalogRepository,
    StudentClassroomOptionBinding,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.tenant_context import TenantContext


def _context() -> TenantContext:
    return TenantContext(
        tenant_id="tenant-runtime",
        schema_name=tenant_schema_name("tenant-runtime"),
        user_id="learner-current",
        permissions=frozenset(),
    )


async def _catalog_with_active_enrollments(count: int):
    context = _context()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    translated = engine.execution_options(
        schema_translate_map={"tenant": context.schema_name}
    )
    async with translated.begin() as connection:
        await connection.execute(text(f'ATTACH DATABASE ":memory:" AS "{context.schema_name}"'))
        await connection.run_sync(
            lambda sync_connection: Course.metadata.create_all(
                sync_connection,
                tables=[
                    Course.__table__,
                    TeachingClass.__table__,
                    Enrollment.__table__,
                    CourseGenerationPolicyRecord.__table__,
                ],
            )
        )

    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            session.add(Course(id="course-a", title="Signals", status="active"))
            session.add(
                CourseGenerationPolicyRecord(
                    course_id="course-a",
                    tenant_id=context.tenant_id,
                    allow_student_micro=True,
                    allow_student_full=True,
                    allowed_content_modes="source_grounded,open_creation",
                    allow_web_search=False,
                    require_approval_for_restricted_topics=True,
                    minor_safety_mode=True,
                    micro_scene_limit=5,
                    full_scene_limit=24,
                    daily_student_units=20,
                    monthly_student_units=100,
                    updated_by="teacher-a",
                )
            )
            session.add(
                Course(id="course-inactive", title="Archived", status="inactive")
            )
            session.add(Course(id="course-no-policy", title="No policy", status="active"))
            session.add(
                TeachingClass(
                    id="class-no-policy",
                    course_id="course-no-policy",
                    name="no policy",
                    status="active",
                )
            )
            session.add(
                Enrollment(
                    class_id="class-no-policy",
                    learner_id=context.user_id,
                    status="active",
                )
            )
            for index in range(count):
                class_id = f"class-{index}"
                session.add(
                    TeachingClass(
                        id=class_id,
                        course_id="course-a",
                        name=class_id,
                        status="active",
                    )
                )
                session.add(
                    Enrollment(
                        class_id=class_id,
                        learner_id=context.user_id,
                        status="active",
                    )
                )
            session.add_all(
                [
                    TeachingClass(
                        id="class-inactive",
                        course_id="course-a",
                        name="inactive class",
                        status="inactive",
                    ),
                    Enrollment(
                        class_id="class-inactive",
                        learner_id=context.user_id,
                        status="active",
                    ),
                    TeachingClass(
                        id="class-left",
                        course_id="course-a",
                        name="left class",
                        status="active",
                    ),
                    Enrollment(
                        class_id="class-left",
                        learner_id=context.user_id,
                        status="inactive",
                    ),
                    TeachingClass(
                        id="class-inactive-course",
                        course_id="course-inactive",
                        name="archived course class",
                        status="active",
                    ),
                    Enrollment(
                        class_id="class-inactive-course",
                        learner_id=context.user_id,
                        status="active",
                    ),
                ]
            )
    return context, engine, SqlAlchemyCatalogRepository(context.tenant_id, engine)


@pytest.mark.asyncio
async def test_options_repository_joins_active_course_class_enrollment_and_policy() -> None:
    context, engine, repository = await _catalog_with_active_enrollments(1)
    try:
        records = await repository.list_student_classroom_option_bindings(
            context.user_id
        )

        assert records == (
            StudentClassroomOptionBinding(
                course_id="course-a",
                title="Signals",
                class_id="class-0",
                allow_student_micro=True,
                allow_student_full=True,
                allowed_content_modes="source_grounded,open_creation",
            ),
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_options_service_fails_closed_on_ambiguous_or_unpermitted_courses() -> None:
    from deeptutor.teaching.services.student_classroom_runtime import (
        StudentClassroomOptionsService,
    )

    class Repository:
        async def list_student_classroom_option_bindings(self, learner_id: str):
            assert learner_id == "learner-current"
            return (
                StudentClassroomOptionBinding(
                    "course-a",
                    "Signals",
                    "class-a",
                    True,
                    True,
                    "source_grounded,open_creation",
                ),
                StudentClassroomOptionBinding(
                    "course-ambiguous",
                    "Ambiguous",
                    "class-b1",
                    True,
                    False,
                    "source_grounded",
                ),
                StudentClassroomOptionBinding(
                    "course-ambiguous",
                    "Ambiguous",
                    "class-b2",
                    True,
                    False,
                    "source_grounded",
                ),
                StudentClassroomOptionBinding(
                    "course-invalid-policy",
                    "Invalid policy",
                    "class-c",
                    True,
                    False,
                    "open_creation,source_grounded",
                ),
                StudentClassroomOptionBinding(
                    "course-no-permission",
                    "No permission",
                    "class-d",
                    True,
                    False,
                    "source_grounded",
                ),
                StudentClassroomOptionBinding(
                    "course-no-mode",
                    "No mode",
                    "class-e",
                    False,
                    False,
                    "source_grounded",
                ),
            )

    context = TenantContext(
        tenant_id="tenant-runtime",
        schema_name=tenant_schema_name("tenant-runtime"),
        user_id="learner-current",
        permissions=frozenset(
            {
                ScopedPermission(
                    permission="classroom.generate.micro",
                    scope_type="class",
                    scope_id="class-a",
                    tenant_id="tenant-runtime",
                ),
                ScopedPermission(
                    permission="classroom.generate.micro",
                    scope_type="class",
                    scope_id="class-e",
                    tenant_id="tenant-runtime",
                ),
            }
        ),
    )

    options = await StudentClassroomOptionsService(Repository()).list(context)

    assert len(options) == 1
    assert options[0].course_id == "course-a"
    assert options[0].title == "Signals"
    assert options[0].allowed_modes == ("micro",)
    assert options[0].allowed_content_modes == (
        "source_grounded",
        "open_creation",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_class_binding_requires_exactly_one_active_enrollment(count: int) -> None:
    from deeptutor.teaching.services.student_classroom_runtime import (
        StudentClassroomBindingUnavailable,
        resolve_student_class_id,
    )

    context, engine, repository = await _catalog_with_active_enrollments(count)
    try:
        if count == 1:
            assert (
                await resolve_student_class_id(
                    context,
                    "course-a",
                    repository=repository,
                )
                == "class-0"
            )
        else:
            with pytest.raises(
                StudentClassroomBindingUnavailable,
                match="exactly one active class enrollment",
            ):
                await resolve_student_class_id(
                    context,
                    "course-a",
                    repository=repository,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_class_binding_rejects_enrollment_for_inactive_course() -> None:
    from deeptutor.teaching.services.student_classroom_runtime import (
        StudentClassroomBindingUnavailable,
        resolve_student_class_id,
    )

    context, engine, repository = await _catalog_with_active_enrollments(0)
    try:
        with pytest.raises(
            StudentClassroomBindingUnavailable,
            match="exactly one active class enrollment",
        ):
            await resolve_student_class_id(
                context,
                "course-inactive",
                repository=repository,
            )
    finally:
        await engine.dispose()


def test_shared_builder_composes_task2_student_classroom_service() -> None:
    from deeptutor.teaching.services.student_classroom_runtime import (
        build_student_classroom_service,
    )
    from deeptutor.teaching.services.student_classrooms import StudentClassroomService

    dependencies = {
        "request_repository": SimpleNamespace(name="requests"),
        "classroom_repository": SimpleNamespace(name="classrooms"),
        "source_repository": SimpleNamespace(name="sources"),
        "store_provider": SimpleNamespace(name="store"),
        "job_repository": SimpleNamespace(name="jobs"),
        "data_plane_selector": SimpleNamespace(name="selector"),
        "cancellation_gateway": SimpleNamespace(name="cancellation"),
    }

    service = build_student_classroom_service(_context(), **dependencies)

    assert isinstance(service, StudentClassroomService)
    assert service._policy_service._repository is dependencies["request_repository"]
    assert service._workflow._repository is dependencies["classroom_repository"]
    assert service._workflow._request_repository is dependencies["request_repository"]
