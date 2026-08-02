"""Authorization-aware teaching catalog operations."""

from __future__ import annotations

from typing import Protocol

from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.repositories.catalog import (
    ClassRecord,
    CourseRecord,
    EnrollmentRecord,
)
from deeptutor.teaching.tenant_context import TenantContext


class CatalogAccessDeniedError(PermissionError):
    """The current principal cannot access the requested catalog resource."""


class CatalogRepository(Protocol):
    async def list_courses(self, course_ids: frozenset[str] | None) -> tuple[CourseRecord, ...]: ...

    async def list_courses_for_classes(self, class_ids: frozenset[str]) -> frozenset[str]: ...

    async def get_course(self, course_id: str) -> CourseRecord: ...

    async def create_course(self, course_id: str, title: str) -> CourseRecord: ...

    async def list_classes(
        self, course_id: str, class_ids: frozenset[str] | None
    ) -> tuple[ClassRecord, ...]: ...

    async def get_class(self, class_id: str) -> ClassRecord: ...

    async def create_class(self, course_id: str, class_id: str, name: str) -> ClassRecord: ...

    async def list_enrollments(
        self, class_id: str, learner_id: str | None
    ) -> tuple[EnrollmentRecord, ...]: ...

    async def add_enrollment(self, class_id: str, learner_id: str) -> EnrollmentRecord: ...

    async def remove_enrollment(self, class_id: str, learner_id: str) -> None: ...


def _has_permission(
    context: TenantContext,
    permission: str,
    *,
    course_id: str | None = None,
    class_id: str | None = None,
) -> bool:
    resource = ResourceScope(
        tenant_id=context.tenant_id,
        course_id=course_id,
        class_id=class_id,
    )
    return any(grant.allows_resource(permission, resource) for grant in context.permissions)


def _is_admin(context: TenantContext) -> bool:
    return _has_permission(context, "tenant.manage")


def _teacher_scopes(
    context: TenantContext,
) -> tuple[bool, frozenset[str], frozenset[str]]:
    tenant_access = False
    course_ids: set[str] = set()
    class_ids: set[str] = set()
    for grant in context.permissions:
        if grant.permission not in {"classroom.create", "classroom.edit", "classroom.assign"}:
            continue
        if grant.scope_type == "tenant" and grant.scope_id == context.tenant_id:
            tenant_access = True
        elif grant.scope_type == "course":
            course_ids.add(grant.scope_id)
        elif grant.scope_type == "class":
            class_ids.add(grant.scope_id)
    return tenant_access, frozenset(course_ids), frozenset(class_ids)


class CatalogService:
    def __init__(self, repository: CatalogRepository) -> None:
        self._repository = repository

    async def list_courses(self, context: TenantContext) -> tuple[CourseRecord, ...]:
        if _is_admin(context):
            return await self._repository.list_courses(None)
        tenant_access, course_ids, class_ids = _teacher_scopes(context)
        if tenant_access:
            return await self._repository.list_courses(None)
        if class_ids:
            course_ids |= await self._repository.list_courses_for_classes(class_ids)
        if not course_ids:
            raise CatalogAccessDeniedError("course access denied")
        return await self._repository.list_courses(course_ids)

    async def create_course(
        self, context: TenantContext, *, course_id: str, title: str
    ) -> CourseRecord:
        if not _is_admin(context):
            raise CatalogAccessDeniedError("course management denied")
        return await self._repository.create_course(course_id, title)

    async def list_classes(
        self, context: TenantContext, *, course_id: str
    ) -> tuple[ClassRecord, ...]:
        if _is_admin(context):
            await self._repository.get_course(course_id)
            return await self._repository.list_classes(course_id, None)
        tenant_access, course_ids, class_ids = _teacher_scopes(context)
        if tenant_access:
            await self._repository.get_course(course_id)
            return await self._repository.list_classes(course_id, None)
        if course_id in course_ids:
            await self._repository.get_course(course_id)
            return await self._repository.list_classes(course_id, None)
        if class_ids:
            visible_course_ids = await self._repository.list_courses_for_classes(class_ids)
            if course_id in visible_course_ids:
                return await self._repository.list_classes(course_id, class_ids)
        raise CatalogAccessDeniedError("class access denied")

    async def create_class(
        self,
        context: TenantContext,
        *,
        course_id: str,
        class_id: str,
        name: str,
    ) -> ClassRecord:
        if not (
            _is_admin(context) or _has_permission(context, "classroom.create", course_id=course_id)
        ):
            raise CatalogAccessDeniedError("class management denied")
        return await self._repository.create_class(course_id, class_id, name)

    async def _class_for_management(self, context: TenantContext, class_id: str) -> ClassRecord:
        if not _is_admin(context) and not any(
            grant.permission == "classroom.assign" for grant in context.permissions
        ):
            raise CatalogAccessDeniedError("enrollment management denied")
        teaching_class = await self._repository.get_class(class_id)
        if not (
            _is_admin(context)
            or _has_permission(
                context,
                "classroom.assign",
                course_id=teaching_class.course_id,
                class_id=teaching_class.id,
            )
        ):
            raise CatalogAccessDeniedError("enrollment management denied")
        return teaching_class

    async def list_enrollments(
        self, context: TenantContext, *, class_id: str
    ) -> tuple[EnrollmentRecord, ...]:
        teaching_class = await self._repository.get_class(class_id)
        if _is_admin(context) or _has_permission(
            context,
            "classroom.assign",
            course_id=teaching_class.course_id,
            class_id=teaching_class.id,
        ):
            return await self._repository.list_enrollments(class_id, None)
        # A learner can observe only their own row; an empty result reveals no peer data.
        return await self._repository.list_enrollments(class_id, context.user_id)

    async def add_enrollment(
        self, context: TenantContext, *, class_id: str, learner_id: str
    ) -> EnrollmentRecord:
        await self._class_for_management(context, class_id)
        return await self._repository.add_enrollment(class_id, learner_id)

    async def remove_enrollment(
        self, context: TenantContext, *, class_id: str, learner_id: str
    ) -> None:
        await self._class_for_management(context, class_id)
        await self._repository.remove_enrollment(class_id, learner_id)


__all__ = ["CatalogAccessDeniedError", "CatalogService"]
