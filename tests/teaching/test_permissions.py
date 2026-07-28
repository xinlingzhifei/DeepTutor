from __future__ import annotations


def test_permission_requires_matching_scope() -> None:
    from deeptutor.teaching.permissions import RoleGrant

    grant = RoleGrant(
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
    } | {"source.use", "learning_event.read"}


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
