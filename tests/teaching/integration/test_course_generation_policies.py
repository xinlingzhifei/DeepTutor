from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from deeptutor.teaching.models.student_generation import CourseGenerationPolicyRecord
from deeptutor.teaching.models.tenant import Course
from deeptutor.teaching.policies.student_generation import ContentMode, CourseGenerationPolicy
from deeptutor.teaching.repositories.catalog import (
    CatalogNotFoundError,
    SqlAlchemyCatalogRepository,
)
from deeptutor.teaching.schema_names import tenant_schema_name

_TENANT_A = "tenant-policy-a"
_TENANT_B = "tenant-policy-b"


def _policy(
    *,
    allow_student_full: bool = True,
    allowed_content_modes: frozenset[ContentMode] = frozenset({"source_grounded", "open_creation"}),
) -> CourseGenerationPolicy:
    return CourseGenerationPolicy(
        allow_student_micro=True,
        allow_student_full=allow_student_full,
        allowed_content_modes=allowed_content_modes,
        allow_web_search=True,
        require_approval_for_restricted_topics=True,
        minor_safety_mode=True,
        micro_scene_limit=4,
        full_scene_limit=18,
        daily_student_units=40,
        monthly_student_units=400,
    )


async def _policy_database() -> AsyncEngine:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    for tenant_id in (_TENANT_A, _TENANT_B):
        schema_name = tenant_schema_name(tenant_id)
        translated = engine.execution_options(schema_translate_map={"tenant": schema_name})
        async with translated.begin() as connection:
            await connection.execute(text(f'ATTACH DATABASE ":memory:" AS "{schema_name}"'))
            await connection.run_sync(
                lambda sync_connection: Course.metadata.create_all(
                    sync_connection,
                    tables=[Course.__table__, CourseGenerationPolicyRecord.__table__],
                )
            )
    return engine


async def _seed_courses(
    engine: AsyncEngine,
    tenant_id: str,
    *courses: Course,
) -> None:
    translated = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            session.add_all(courses)


async def _stored_policy_rows(
    engine: AsyncEngine,
    tenant_id: str,
) -> tuple[tuple[str, str, str, str], ...]:
    translated = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    session_factory = async_sessionmaker(translated, expire_on_commit=False)
    async with session_factory() as session:
        models = await session.scalars(
            select(CourseGenerationPolicyRecord).order_by(CourseGenerationPolicyRecord.course_id)
        )
        return tuple(
            (
                model.course_id,
                model.tenant_id,
                model.allowed_content_modes,
                model.updated_by,
            )
            for model in models
        )


@pytest.mark.asyncio
async def test_course_policy_replace_is_tenant_scoped_and_canonical() -> None:
    engine = await _policy_database()
    try:
        await _seed_courses(
            engine,
            _TENANT_A,
            Course(id="course-shared", title="Tenant A course", status="active"),
        )
        await _seed_courses(
            engine,
            _TENANT_B,
            Course(id="course-shared", title="Tenant B course", status="active"),
        )
        repository_a = SqlAlchemyCatalogRepository(_TENANT_A, engine)
        repository_b = SqlAlchemyCatalogRepository(_TENANT_B, engine)

        replaced = await repository_a.replace_course_generation_policy(
            "course-shared",
            _policy(),
            "platform-admin-a",
        )
        loaded = await repository_a.get_course_generation_policy("course-shared")

        assert loaded == replaced
        assert loaded.tenant_id == _TENANT_A
        assert loaded.course_id == "course-shared"
        assert loaded.allowed_content_modes == frozenset({"source_grounded", "open_creation"})
        assert loaded.updated_by == "platform-admin-a"
        assert await _stored_policy_rows(engine, _TENANT_A) == (
            (
                "course-shared",
                _TENANT_A,
                "source_grounded,open_creation",
                "platform-admin-a",
            ),
        )
        with pytest.raises(
            CatalogNotFoundError,
            match="course generation policy not found",
        ):
            await repository_b.get_course_generation_policy("course-shared")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_course_policy_replace_updates_one_row_without_cross_tenant_leakage() -> None:
    engine = await _policy_database()
    try:
        await _seed_courses(
            engine,
            _TENANT_A,
            Course(id="course-shared", title="Tenant A course", status="active"),
        )
        await _seed_courses(
            engine,
            _TENANT_B,
            Course(id="course-shared", title="Tenant B course", status="active"),
        )
        repository_a = SqlAlchemyCatalogRepository(_TENANT_A, engine)
        repository_b = SqlAlchemyCatalogRepository(_TENANT_B, engine)
        policy = _policy()

        first = await repository_a.replace_course_generation_policy(
            "course-shared",
            policy,
            "platform-admin-a",
        )
        same_admin_replay = await repository_a.replace_course_generation_policy(
            "course-shared",
            policy,
            "platform-admin-a",
        )
        different_admin_replay = await repository_a.replace_course_generation_policy(
            "course-shared",
            policy,
            "platform-admin-b",
        )

        assert same_admin_replay == first
        assert different_admin_replay.updated_by == "platform-admin-b"
        assert different_admin_replay.updated_at != first.updated_at
        assert (
            await repository_a.get_course_generation_policy("course-shared")
            == different_admin_replay
        )
        tenant_b_policy = await repository_b.replace_course_generation_policy(
            "course-shared",
            _policy(
                allow_student_full=False,
                allowed_content_modes=frozenset({"source_grounded"}),
            ),
            "platform-admin-tenant-b",
        )

        assert tenant_b_policy.tenant_id == _TENANT_B
        assert tenant_b_policy.allow_student_full is False
        assert tenant_b_policy.allowed_content_modes == frozenset({"source_grounded"})
        assert (
            await repository_a.get_course_generation_policy("course-shared")
            == different_admin_replay
        )
        assert await _stored_policy_rows(engine, _TENANT_A) == (
            (
                "course-shared",
                _TENANT_A,
                "source_grounded,open_creation",
                "platform-admin-b",
            ),
        )
        assert await _stored_policy_rows(engine, _TENANT_B) == (
            (
                "course-shared",
                _TENANT_B,
                "source_grounded",
                "platform-admin-tenant-b",
            ),
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_course_policy_get_rejects_missing_or_inactive_course() -> None:
    engine = await _policy_database()
    try:
        await _seed_courses(
            engine,
            _TENANT_A,
            Course(id="course-active", title="Active course", status="active"),
            Course(id="course-inactive", title="Inactive course", status="inactive"),
        )
        repository = SqlAlchemyCatalogRepository(_TENANT_A, engine)

        for course_id in ("course-missing", "course-inactive"):
            with pytest.raises(CatalogNotFoundError, match="course not found"):
                await repository.get_course_generation_policy(course_id)
            with pytest.raises(CatalogNotFoundError, match="course not found"):
                await repository.replace_course_generation_policy(
                    course_id,
                    _policy(),
                    "platform-admin-a",
                )

        with pytest.raises(
            CatalogNotFoundError,
            match="course generation policy not found",
        ):
            await repository.get_course_generation_policy("course-active")
    finally:
        await engine.dispose()
