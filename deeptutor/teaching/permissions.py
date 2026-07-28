"""Tenant-scoped permission grants and fixed role templates."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

KNOWN_PERMISSIONS = frozenset(
    {
        "tenant.manage",
        "template.manage",
        "policy.manage",
        "classroom.create",
        "classroom.edit",
        "classroom.submit",
        "classroom.approve",
        "classroom.publish",
        "classroom.assign",
        "classroom.generate.micro",
        "classroom.generate.full",
        "source.use",
        "learning_event.read",
    }
)

DEFAULT_ROLE_PERMISSIONS = {
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


@dataclass(frozen=True, slots=True)
class ScopedPermission:
    """A concrete permission bound to one exact business scope."""

    permission: str
    scope_type: str
    scope_id: str

    def allows(self, permission: str, scope_type: str, scope_id: str) -> bool:
        """Return true only for a known, exact permission and scope match."""

        return (
            self.permission in KNOWN_PERMISSIONS
            and permission in KNOWN_PERMISSIONS
            and self.permission == permission
            and self.scope_type == scope_type
            and self.scope_id == scope_id
        )


RoleGrant = ScopedPermission


def permissions_for_roles(
    roles: Iterable[str],
    *,
    scope_type: str,
    scope_id: str,
) -> frozenset[ScopedPermission]:
    """Expand known role templates into concrete, tenant-scoped grants."""

    requested_roles = frozenset(roles)
    if not requested_roles or not requested_roles.issubset(DEFAULT_ROLE_PERMISSIONS):
        return frozenset()

    permission_names: set[str] = set()
    classroom_permissions = {
        permission for permission in KNOWN_PERMISSIONS if permission.startswith("classroom.")
    }
    for role in requested_roles:
        for permission in DEFAULT_ROLE_PERMISSIONS[role]:
            if permission == "classroom.*":
                permission_names.update(classroom_permissions)
            elif permission in KNOWN_PERMISSIONS:
                permission_names.add(permission)

    return frozenset(
        ScopedPermission(
            permission=permission,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        for permission in permission_names
    )
