from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deeptutor.api.routers import teaching_catalog as teaching_router
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.policies.student_generation import CourseGenerationPolicy
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


@dataclass(frozen=True)
class _CourseGenerationPolicy:
    tenant_id: str
    course_id: str
    allow_student_micro: bool
    allow_student_full: bool
    allowed_content_modes: frozenset[str]
    allow_web_search: bool
    require_approval_for_restricted_topics: bool
    minor_safety_mode: bool
    micro_scene_limit: int
    full_scene_limit: int
    daily_student_units: int
    monthly_student_units: int
    updated_by: str
    updated_at: datetime


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
        self.course_generation_policies: dict[str, _CourseGenerationPolicy] = {}
        self.ineligible_learners: set[str] = set()

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

    async def get_course_generation_policy(self, course_id: str) -> _CourseGenerationPolicy:
        from deeptutor.teaching.repositories.catalog import CatalogNotFoundError

        course = self.courses.get(course_id)
        if course is None or course.status != "active":
            raise CatalogNotFoundError("course not found")
        try:
            return self.course_generation_policies[course_id]
        except KeyError as exc:
            raise CatalogNotFoundError("course generation policy not found") from exc

    async def replace_course_generation_policy(
        self,
        course_id: str,
        policy: CourseGenerationPolicy,
        updated_by: str,
    ) -> _CourseGenerationPolicy:
        from deeptutor.teaching.repositories.catalog import CatalogNotFoundError

        assert type(policy) is CourseGenerationPolicy
        course = self.courses.get(course_id)
        if course is None or course.status != "active":
            raise CatalogNotFoundError("course not found")
        existing = self.course_generation_policies.get(course_id)
        if (
            existing is not None
            and existing.allow_student_micro == policy.allow_student_micro
            and existing.allow_student_full == policy.allow_student_full
            and existing.allowed_content_modes == policy.allowed_content_modes
            and existing.allow_web_search == policy.allow_web_search
            and existing.require_approval_for_restricted_topics
            == policy.require_approval_for_restricted_topics
            and existing.minor_safety_mode == policy.minor_safety_mode
            and existing.micro_scene_limit == policy.micro_scene_limit
            and existing.full_scene_limit == policy.full_scene_limit
            and existing.daily_student_units == policy.daily_student_units
            and existing.monthly_student_units == policy.monthly_student_units
            and existing.updated_by == updated_by
        ):
            return existing
        record = _CourseGenerationPolicy(
            tenant_id="tenant-a",
            course_id=course_id,
            allow_student_micro=policy.allow_student_micro,
            allow_student_full=policy.allow_student_full,
            allowed_content_modes=policy.allowed_content_modes,
            allow_web_search=policy.allow_web_search,
            require_approval_for_restricted_topics=(policy.require_approval_for_restricted_topics),
            minor_safety_mode=policy.minor_safety_mode,
            micro_scene_limit=policy.micro_scene_limit,
            full_scene_limit=policy.full_scene_limit,
            daily_student_units=policy.daily_student_units,
            monthly_student_units=policy.monthly_student_units,
            updated_by=updated_by,
            updated_at=(
                existing.updated_at + timedelta(microseconds=1)
                if existing is not None
                else datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
            ),
        )
        self.course_generation_policies[course_id] = record
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
        from deeptutor.teaching.repositories.catalog import CatalogNotFoundError

        if learner_id in self.ineligible_learners:
            raise CatalogNotFoundError("learner is not an active tenant member")
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


def _course_generation_policy_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "allowStudentMicro": True,
        "allowStudentFull": True,
        "allowedContentModes": ["open_creation", "source_grounded"],
        "allowWebSearch": True,
        "requireApprovalForRestrictedTopics": True,
        "minorSafetyMode": True,
        "microSceneLimit": 4,
        "fullSceneLimit": 18,
        "dailyStudentUnits": 40,
        "monthlyStudentUnits": 400,
    }
    payload.update(changes)
    return payload


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
    assert [item["id"] for item in response.json()["items"]] == ["course-a"]


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
    assert {item["id"] for item in listed.json()["items"]} == {
        "course-a",
        "course-b",
        "course-c",
    }
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

    assert [item["id"] for item in courses.json()["items"]] == ["course-a"]
    assert [item["id"] for item in classes.json()["items"]] == ["class-a"]


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

    assert {item["id"] for item in courses.json()["items"]} == {"course-a", "course-b"}
    assert [item["id"] for item in classes.json()["items"]] == ["class-b"]


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


def test_enrollment_rejects_unknown_inactive_and_other_tenant_users_without_leaking() -> None:
    repository = _CatalogRepository()
    repository.ineligible_learners = {
        "student-missing",
        "student-inactive",
        "student-other-tenant",
    }
    context = _context(
        "teacher-a",
        "teacher",
        scope_type="class",
        scope_id="class-a",
    )
    client = _client(context, repository)

    responses = [
        client.post(
            "/api/v1/teaching/classes/class-a/enrollments",
            json={"userId": learner_id},
        )
        for learner_id in sorted(repository.ineligible_learners)
    ]

    assert {response.status_code for response in responses} == {404}
    assert {response.json()["detail"] for response in responses} == {
        "learner is not an active tenant member"
    }
    assert all(
        ("class-a", learner_id) not in repository.enrollments
        for learner_id in repository.ineligible_learners
    )


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
    assert listed.json() == {
        "items": [
            {
                "classId": "class-a",
                "userId": "student-a",
                "status": "active",
                "createdAt": None,
            }
        ]
    }
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


def test_platform_admin_replaces_and_reads_course_generation_policy() -> None:
    repository = _CatalogRepository()
    context = _context(
        "platform-admin-a",
        "platform_admin",
        scope_type="tenant",
        scope_id="tenant-a",
    )
    client = _client(context, repository)
    payload = _course_generation_policy_payload()
    path = "/api/v1/teaching/courses/course-a/generation-policy"

    created = client.put(path, json=payload)
    read = client.get(path)
    repeated = client.put(path, json=payload)

    assert [created.status_code, read.status_code, repeated.status_code] == [200, 200, 200]
    expected = {
        **payload,
        "allowedContentModes": ["source_grounded", "open_creation"],
        "tenantId": "tenant-a",
        "courseId": "course-a",
        "updatedBy": "platform-admin-a",
    }
    expected_fields = set(expected) | {"updatedAt"}
    bodies = [created.json(), read.json(), repeated.json()]
    for body in bodies:
        assert set(body) == expected_fields
        assert {key: value for key, value in body.items() if key != "updatedAt"} == expected
        assert datetime.fromisoformat(body["updatedAt"].replace("Z", "+00:00")).tzinfo
    assert created.json() == read.json() == repeated.json()

    second_admin = _client(
        _context(
            "platform-admin-b",
            "platform_admin",
            scope_type="tenant",
            scope_id="tenant-a",
        ),
        repository,
    )
    reassigned = second_admin.put(path, json=payload)
    reread = second_admin.get(path)

    assert reassigned.status_code == 200
    assert reread.status_code == 200
    assert reassigned.json()["updatedBy"] == "platform-admin-b"
    assert reassigned.json()["updatedAt"] != repeated.json()["updatedAt"]
    assert reread.json() == reassigned.json()


def test_course_generation_policy_requires_policy_manage() -> None:
    repository = _CatalogRepository()
    path = "/api/v1/teaching/courses/course-missing/generation-policy"
    denied_contexts = (
        _context("org-admin-a", "org_admin", scope_type="tenant", scope_id="tenant-a"),
        _context("teacher-a", "teacher", scope_type="course", scope_id="course-a"),
        _context("student-a", "student", scope_type="class", scope_id="class-a"),
    )

    for context in denied_contexts:
        client = _client(context, repository)
        responses = (
            client.get(path),
            client.put(path, json=_course_generation_policy_payload()),
        )

        assert [response.status_code for response in responses] == [403, 403]
        assert {response.json()["detail"] for response in responses} == {"Catalog access denied"}


def test_course_generation_policy_rejects_untrusted_and_invalid_fields() -> None:
    repository = _CatalogRepository()
    context = _context(
        "platform-admin-a",
        "platform_admin",
        scope_type="tenant",
        scope_id="tenant-a",
    )
    client = _client(context, repository)
    path = "/api/v1/teaching/courses/course-a/generation-policy"
    invalid_changes: tuple[dict[str, object], ...] = (
        {"tenantId": "tenant-b"},
        {"updatedBy": "attacker"},
        {"updatedAt": "2026-08-26T00:00:00Z"},
        {"unexpectedPolicyField": "forbidden"},
        {"allowedContentModes": []},
        {"allowedContentModes": ["source_grounded", "source_grounded"]},
        {"allowedContentModes": ["source_grounded", "untrusted"]},
        {"microSceneLimit": 0},
        {"microSceneLimit": 6},
        {"fullSceneLimit": 0},
        {"fullSceneLimit": 25},
        {"dailyStudentUnits": -1},
        {"monthlyStudentUnits": -1},
    )

    for changes in invalid_changes:
        response = client.put(
            path,
            json=_course_generation_policy_payload(**changes),
        )

        assert response.status_code == 422, (changes, response.text)
    assert repository.course_generation_policies == {}


def test_course_generation_policy_missing_resources_have_stable_statuses() -> None:
    repository = _CatalogRepository()
    repository.courses["course-inactive"] = _Course(
        "course-inactive",
        "Inactive Course",
        status="inactive",
    )
    context = _context(
        "platform-admin-a",
        "platform_admin",
        scope_type="tenant",
        scope_id="tenant-a",
    )
    client = _client(context, repository)
    inactive_path = "/api/v1/teaching/courses/course-inactive/generation-policy"
    missing_course_path = "/api/v1/teaching/courses/course-missing/generation-policy"

    missing_course_responses = (
        client.get(inactive_path),
        client.put(inactive_path, json=_course_generation_policy_payload()),
        client.get(missing_course_path),
        client.put(missing_course_path, json=_course_generation_policy_payload()),
    )
    missing_policy = client.get("/api/v1/teaching/courses/course-a/generation-policy")

    assert [response.status_code for response in missing_course_responses] == [404, 404, 404, 404]
    assert {response.json()["detail"] for response in missing_course_responses} == {
        "course not found"
    }
    assert missing_policy.status_code == 404
    assert missing_policy.json()["detail"] == "course generation policy not found"


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
