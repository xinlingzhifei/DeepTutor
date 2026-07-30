from __future__ import annotations


def test_permission_requires_matching_scope() -> None:
    from deeptutor.teaching.permissions import ScopedPermission

    grant = ScopedPermission(
        permission="classroom.edit",
        scope_type="course",
        scope_id="course-a",
    )
    assert grant.allows("classroom.edit", "course", "course-a")
    assert not grant.allows("classroom.edit", "course", "course-b")


def test_permission_name_is_an_exact_runtime_match() -> None:
    from deeptutor.teaching.permissions import ScopedPermission

    grant = ScopedPermission(
        permission="classroom.*",
        scope_type="tenant",
        scope_id="tenant-a",
    )
    assert not grant.allows("classroom.edit", "tenant", "tenant-a")
    assert not grant.allows("classroom.unknown", "tenant", "tenant-a")


def test_default_role_templates_are_exact() -> None:
    from deeptutor.teaching.permissions import DEFAULT_ROLE_PERMISSIONS

    assert DEFAULT_ROLE_PERMISSIONS == {
        "platform_admin": frozenset(
            {
                "tenant.manage",
                "template.manage",
                "policy.manage",
                "classroom.approve",
                "classroom.publish",
            }
        ),
        "org_admin": frozenset(
            {
                "tenant.manage",
                "classroom.*",
                "source.use",
                "learning_event.read",
            }
        ),
        "content_author": frozenset(
            {
                "classroom.create",
                "classroom.edit",
                "classroom.submit",
                "source.use",
            }
        ),
        "content_reviewer": frozenset(
            {
                "classroom.approve",
                "learning_event.read",
            }
        ),
        "teacher": frozenset(
            {
                "classroom.create",
                "classroom.edit",
                "classroom.submit",
                "classroom.publish",
                "classroom.assign",
                "source.use",
                "learning_event.read",
            }
        ),
        "student": frozenset(
            {
                "classroom.generate.micro",
                "classroom.generate.full",
            }
        ),
    }


def test_classroom_wildcard_expands_only_to_known_permissions() -> None:
    from deeptutor.teaching.permissions import (
        KNOWN_PERMISSIONS,
        permissions_for_roles,
    )

    grants = permissions_for_roles(
        {"org_admin"},
        scope_type="tenant",
        scope_id="tenant-a",
    )
    names = {grant.permission for grant in grants}
    assert "classroom.*" not in names
    assert names == {
        permission for permission in KNOWN_PERMISSIONS if permission.startswith("classroom.")
    } | {"tenant.manage", "source.use", "learning_event.read"}


def test_unknown_role_and_unknown_permission_fail_closed() -> None:
    from deeptutor.teaching.permissions import (
        KNOWN_PERMISSIONS,
        permissions_for_roles,
    )

    assert (
        permissions_for_roles(
            {"not-a-role"},
            scope_type="tenant",
            scope_id="tenant-a",
        )
        == frozenset()
    )
    assert "classroom.unknown" not in KNOWN_PERMISSIONS


def test_persisted_role_grants_expand_separately_from_permissions() -> None:
    from deeptutor.teaching.permissions import (
        RoleGrant,
        ScopedPermission,
        permissions_for_grants,
    )

    role_grant = RoleGrant(
        role="teacher",
        scope_type="class",
        scope_id="class-a",
    )

    permissions = permissions_for_grants(
        {role_grant},
        tenant_id="tenant-a",
    )

    assert role_grant not in permissions
    assert permissions
    assert all(isinstance(permission, ScopedPermission) for permission in permissions)
    assert {
        (
            permission.permission,
            permission.scope_type,
            permission.scope_id,
            permission.tenant_id,
        )
        for permission in permissions
        if permission.permission == "classroom.edit"
    } == {("classroom.edit", "class", "class-a", "tenant-a")}


def test_resource_scope_inheritance_uses_only_explicit_ancestry() -> None:
    from deeptutor.teaching.permissions import (
        ResourceScope,
        RoleGrant,
        allows_resource,
        permissions_for_grants,
    )

    def edit_permission(scope_type: str, scope_id: str):
        return next(
            permission
            for permission in permissions_for_grants(
                {RoleGrant("teacher", scope_type, scope_id)},
                tenant_id="tenant-a",
            )
            if permission.permission == "classroom.edit"
        )

    tenant = edit_permission("tenant", "tenant-a")
    course = edit_permission("course", "course-a")
    classroom = edit_permission("class", "class-a")
    same_class = ResourceScope("tenant-a", "course-a", "class-a")

    assert allows_resource(tenant, "classroom.edit", same_class)
    assert allows_resource(course, "classroom.edit", same_class)
    assert allows_resource(classroom, "classroom.edit", same_class)
    assert not allows_resource(
        tenant,
        "classroom.edit",
        ResourceScope("tenant-b", "course-a", "class-a"),
    )
    assert not allows_resource(
        course,
        "classroom.edit",
        ResourceScope("tenant-a", "course-b", "class-a"),
    )
    assert not allows_resource(
        course,
        "classroom.edit",
        ResourceScope("tenant-a", class_id="class-a"),
    )
    assert not allows_resource(
        classroom,
        "classroom.edit",
        ResourceScope("tenant-a", "course-a", "class-b"),
    )


def test_unknown_scopes_and_permissions_fail_closed_for_resources() -> None:
    from deeptutor.teaching.permissions import ResourceScope, ScopedPermission

    resource = ResourceScope("tenant-a", "course-a")
    assert not ScopedPermission(
        "classroom.edit",
        "unknown",
        "course-a",
        "tenant-a",
    ).allows_resource("classroom.edit", resource)
    assert not ScopedPermission(
        "classroom.unknown",
        "course",
        "course-a",
        "tenant-a",
    ).allows_resource("classroom.unknown", resource)


def test_role_grant_orm_metadata_includes_scope_identity_and_lookup_index() -> None:
    from sqlalchemy import CheckConstraint

    from deeptutor.teaching.models.platform import RoleGrant

    table = RoleGrant.__table__
    assert tuple(column.name for column in table.primary_key.columns) == (
        "tenant_id",
        "user_id",
        "role",
        "scope_type",
        "scope_id",
    )
    assert table.c.scope_type.nullable is False
    assert table.c.scope_id.nullable is False
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["ck_role_grants_scope_type"] == ("scope_type IN ('tenant', 'course', 'class')")
    assert {index.name: tuple(column.name for column in index.columns) for index in table.indexes}[
        "ix_role_grants_tenant_user_scope"
    ] == (
        "tenant_id",
        "user_id",
        "scope_type",
        "scope_id",
    )
