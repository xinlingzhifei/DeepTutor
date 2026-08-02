from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deeptutor.api.routers import teaching_catalog as teaching_router
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


@dataclass(frozen=True)
class _Course:
    id: str
    title: str
    status: str = "active"
    created_at: object | None = None


@dataclass(frozen=True)
class _Class:
    id: str
    course_id: str
    name: str
    status: str = "active"
    created_at: object | None = None


@dataclass(frozen=True)
class _Enrollment:
    class_id: str
    learner_id: str
    status: str = "active"
    created_at: object | None = None


class _CatalogRepository:
    def __init__(self) -> None:
        self.courses = {
            "course-a": _Course("course-a", "Course A"),
            "course-b": _Course("course-b", "Course B"),
        }
        self.classes = {
            "class-a": _Class("class-a", "course-a", "Class A"),
            "class-a-other": _Class("class-a-other", "course-a", "Class A Other"),
            "class-b": _Class("class-b", "course-b", "Class B"),
        }
        self.enrollments = {
            ("class-a", "student-a"): _Enrollment("class-a", "student-a"),
            ("class-a", "student-b"): _Enrollment("class-a", "student-b"),
        }

    async def list_courses(self, course_ids):
        return tuple(
            course
            for course_id, course in self.courses.items()
            if course_ids is None or course_id in course_ids
        )

    async def list_courses_for_classes(self, class_ids):
        return frozenset(
            teaching_class.course_id
            for class_id, teaching_class in self.classes.items()
            if class_id in class_ids
        )

    async def get_course(self, course_id):
        from deeptutor.teaching.repositories.catalog import CatalogNotFoundError

        try:
            return self.courses[course_id]
        except KeyError as exc:
            raise CatalogNotFoundError("course not found") from exc

    async def create_course(self, course_id, title):
        from deeptutor.teaching.repositories.catalog import CatalogConflictError

        if course_id in self.courses:
            raise CatalogConflictError("course already exists")
        record = _Course(course_id, title)
        self.courses[course_id] = record
        return record

    async def list_classes(self, course_id, class_ids):
        return tuple(
            teaching_class
            for class_id, teaching_class in self.classes.items()
            if teaching_class.course_id == course_id
            and (class_ids is None or class_id in class_ids)
        )

    async def get_class(self, class_id):
        from deeptutor.teaching.repositories.catalog import CatalogNotFoundError

        try:
            return self.classes[class_id]
        except KeyError as exc:
            raise CatalogNotFoundError("class not found") from exc

    async def create_class(self, course_id, class_id, name):
        from deeptutor.teaching.repositories.catalog import (
            CatalogConflictError,
            CatalogNotFoundError,
        )

        if course_id not in self.courses:
            raise CatalogNotFoundError("course not found")
        if class_id in self.classes:
            raise CatalogConflictError("class already exists")
        record = _Class(class_id, course_id, name)
        self.classes[class_id] = record
        return record

    async def list_enrollments(self, class_id, learner_id):
        return tuple(
            enrollment
            for (enrollment_class_id, enrollment_user_id), enrollment in self.enrollments.items()
            if enrollment_class_id == class_id
            and (learner_id is None or enrollment_user_id == learner_id)
        )

    async def add_enrollment(self, class_id, learner_id):
        record = _Enrollment(class_id, learner_id)
        self.enrollments[(class_id, learner_id)] = record
        return record

    async def remove_enrollment(self, class_id, learner_id):
        from deeptutor.teaching.repositories.catalog import CatalogNotFoundError

        try:
            del self.enrollments[(class_id, learner_id)]
        except KeyError as exc:
            raise CatalogNotFoundError("enrollment not found") from exc


def _context(user_id: str, role: str, *, scope_type: str, scope_id: str) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant_a",
        user_id=user_id,
        permissions=permissions_for_roles(
            {role},
            scope_type=scope_type,
            scope_id=scope_id,
            tenant_id="tenant-a",
        ),
    )


def _client(context: TenantContext, repository: object) -> TestClient:
    app = FastAPI()
    app.include_router(teaching_router.router, prefix="/api/v1/teaching")
    app.dependency_overrides[require_tenant] = lambda: context
    app.dependency_overrides[teaching_router.get_catalog_repository] = lambda: repository
    return TestClient(app)


def test_teacher_course_list_contains_only_granted_course() -> None:
    repository = _CatalogRepository()
    context = _context(
        "teacher-a",
        "teacher",
        scope_type="course",
        scope_id="course-a",
    )

    response = _client(context, repository).get("/api/v1/teaching/courses")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["course-a"]


def test_student_cannot_enroll_another_student() -> None:
    repository = _CatalogRepository()
    context = _context(
        "student-a",
        "student",
        scope_type="class",
        scope_id="class-a",
    )

    response = _client(context, repository).post(
        "/api/v1/teaching/classes/class-a/enrollments",
        json={"userId": "student-b"},
    )

    assert response.status_code == 403


def test_org_admin_can_create_and_list_current_tenant_courses() -> None:
    repository = _CatalogRepository()
    context = _context(
        "admin-a",
        "org_admin",
        scope_type="tenant",
        scope_id="tenant-a",
    )
    client = _client(context, repository)

    created = client.post(
        "/api/v1/teaching/courses",
        json={"id": "course-c", "title": "Course C"},
    )
    listed = client.get("/api/v1/teaching/courses")

    assert created.status_code == 201
    assert created.json()["id"] == "course-c"
    assert {item["id"] for item in listed.json()} == {"course-a", "course-b", "course-c"}
    assert "tenantId" not in created.json()


def test_teacher_cannot_create_course_or_access_ungranted_course() -> None:
    repository = _CatalogRepository()
    context = _context(
        "teacher-a",
        "teacher",
        scope_type="course",
        scope_id="course-a",
    )
    client = _client(context, repository)

    create = client.post(
        "/api/v1/teaching/courses",
        json={"id": "course-c", "title": "Course C"},
    )
    list_ungranted = client.get("/api/v1/teaching/courses/course-b/classes")

    assert create.status_code == 403
    assert list_ungranted.status_code == 403


def test_class_scoped_teacher_sees_only_granted_class() -> None:
    repository = _CatalogRepository()
    context = _context(
        "teacher-a",
        "teacher",
        scope_type="class",
        scope_id="class-a",
    )
    client = _client(context, repository)

    courses = client.get("/api/v1/teaching/courses")
    classes = client.get("/api/v1/teaching/courses/course-a/classes")

    assert [item["id"] for item in courses.json()] == ["course-a"]
    assert [item["id"] for item in classes.json()] == ["class-a"]


def test_tenant_scoped_teacher_grant_lists_all_tenant_courses_and_classes() -> None:
    repository = _CatalogRepository()
    context = _context(
        "teacher-a",
        "teacher",
        scope_type="tenant",
        scope_id="tenant-a",
    )
    client = _client(context, repository)

    courses = client.get("/api/v1/teaching/courses")
    classes = client.get("/api/v1/teaching/courses/course-b/classes")

    assert {item["id"] for item in courses.json()} == {"course-a", "course-b"}
    assert [item["id"] for item in classes.json()] == ["class-b"]


def test_teacher_can_create_class_only_inside_granted_course() -> None:
    repository = _CatalogRepository()
    context = _context(
        "teacher-a",
        "teacher",
        scope_type="course",
        scope_id="course-a",
    )
    client = _client(context, repository)

    allowed = client.post(
        "/api/v1/teaching/courses/course-a/classes",
        json={"id": "class-new", "name": "New Class"},
    )
    denied = client.post(
        "/api/v1/teaching/courses/course-b/classes",
        json={"id": "class-forged", "name": "Forged Class"},
    )

    assert allowed.status_code == 201
    assert allowed.json()["courseId"] == "course-a"
    assert denied.status_code == 403


def test_teacher_manages_enrollments_only_in_granted_class() -> None:
    repository = _CatalogRepository()
    context = _context(
        "teacher-a",
        "teacher",
        scope_type="class",
        scope_id="class-a",
    )
    client = _client(context, repository)

    added = client.post(
        "/api/v1/teaching/classes/class-a/enrollments",
        json={"userId": "student-c"},
    )
    denied = client.post(
        "/api/v1/teaching/classes/class-b/enrollments",
        json={"userId": "student-c"},
    )
    removed = client.delete("/api/v1/teaching/classes/class-a/enrollments/student-c")

    assert added.status_code == 201
    assert denied.status_code == 403
    assert removed.status_code == 204


def test_student_reads_only_own_enrollment_and_cannot_remove_peers() -> None:
    repository = _CatalogRepository()
    context = _context(
        "student-a",
        "student",
        scope_type="class",
        scope_id="class-a",
    )
    client = _client(context, repository)

    listed = client.get("/api/v1/teaching/classes/class-a/enrollments")
    removal = client.delete("/api/v1/teaching/classes/class-a/enrollments/student-b")

    assert listed.status_code == 200
    assert listed.json() == [
        {"classId": "class-a", "userId": "student-a", "status": "active", "createdAt": None}
    ]
    assert removal.status_code == 403


def test_tenant_id_cannot_be_injected_into_catalog_payload() -> None:
    repository = _CatalogRepository()
    context = _context(
        "admin-a",
        "org_admin",
        scope_type="tenant",
        scope_id="tenant-a",
    )

    response = _client(context, repository).post(
        "/api/v1/teaching/courses",
        json={"id": "course-x", "title": "Course X", "tenantId": "tenant-b"},
    )

    assert response.status_code == 422
    assert "course-x" not in repository.courses


def test_catalog_conflicts_and_missing_resources_have_stable_statuses() -> None:
    repository = _CatalogRepository()
    context = _context(
        "admin-a",
        "org_admin",
        scope_type="tenant",
        scope_id="tenant-a",
    )
    client = _client(context, repository)

    conflict = client.post(
        "/api/v1/teaching/courses",
        json={"id": "course-a", "title": "Duplicate"},
    )
    missing = client.delete("/api/v1/teaching/classes/class-a/enrollments/student-missing")

    assert conflict.status_code == 409
    assert missing.status_code == 404


def test_teaching_routes_are_not_registered_when_platform_is_disabled() -> None:
    from deeptutor.api.main import _register_teaching_catalog_routes

    app = FastAPI()

    registered = _register_teaching_catalog_routes(
        app,
        enabled=False,
        dependencies=[],
    )

    assert registered is False
    assert all("/teaching/" not in route.path for route in app.routes)


def test_teaching_routes_are_registered_under_the_versioned_prefix() -> None:
    from deeptutor.api.main import _register_teaching_catalog_routes

    app = FastAPI()

    registered = _register_teaching_catalog_routes(
        app,
        enabled=True,
        dependencies=[],
    )

    assert registered is True
    paths = {route.path for route in app.routes}
    assert "/api/v1/teaching/courses" in paths
    assert "/api/v1/teaching/sources/pdf" in paths
    assert "/api/v1/teaching/sources/{binding_id}" in paths
