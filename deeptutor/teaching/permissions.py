"""Resource-scoped role grants, permissions, and fixed role templates."""

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

KNOWN_SCOPE_TYPES = frozenset({"tenant", "course", "class"})

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


@dataclass(frozen=True, slots=True)
class RoleGrant:
    """One persisted role template bound to a resource scope."""

    role: str
    scope_type: str
    scope_id: str


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """Explicit ancestry for one tenant-owned resource."""

    tenant_id: str
    course_id: str | None = None
    class_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScopedPermission:
    """One expanded permission bound to a resource scope."""

    permission: str
    scope_type: str
    scope_id: str
    tenant_id: str | None = None

    def allows(self, permission: str, scope_type: str, scope_id: str) -> bool:
        """Return true only for a known, exact permission and scope match."""

        return (
            self.permission in KNOWN_PERMISSIONS
            and permission in KNOWN_PERMISSIONS
            and self.scope_type in KNOWN_SCOPE_TYPES
            and scope_type in KNOWN_SCOPE_TYPES
            and self.permission == permission
            and self.scope_type == scope_type
            and self.scope_id == scope_id
        )

    def allows_resource(
        self,
        permission: str,
        resource: ResourceScope,
    ) -> bool:
        """Apply explicit tenant/course/class inheritance to one resource."""

        return allows_resource(self, permission, resource)


def allows_resource(
    grant: ScopedPermission,
    permission: str,
    resource: ResourceScope,
) -> bool:
    """Return whether a scoped permission covers explicit resource ancestry."""

    if (
        grant.permission not in KNOWN_PERMISSIONS
        or permission not in KNOWN_PERMISSIONS
        or grant.permission != permission
        or grant.scope_type not in KNOWN_SCOPE_TYPES
        or not grant.scope_id
        or not resource.tenant_id
        or resource.course_id == ""
        or resource.class_id == ""
    ):
        return False

    grant_tenant_id = grant.tenant_id
    if grant.scope_type == "tenant" and grant_tenant_id is None:
        grant_tenant_id = grant.scope_id
    if grant_tenant_id is None or grant_tenant_id != resource.tenant_id:
        return False

    if grant.scope_type == "tenant":
        return grant.scope_id == resource.tenant_id
    if grant.scope_type == "course":
        return resource.course_id is not None and grant.scope_id == resource.course_id
    return resource.class_id is not None and grant.scope_id == resource.class_id


def permissions_for_roles(
    roles: Iterable[str],
    *,
    scope_type: str,
    scope_id: str,
    tenant_id: str | None = None,
) -> frozenset[ScopedPermission]:
    """Expand known role templates into concrete scoped permissions."""

    requested_roles = frozenset(roles)
    if (
        not requested_roles
        or not requested_roles.issubset(DEFAULT_ROLE_PERMISSIONS)
        or scope_type not in KNOWN_SCOPE_TYPES
        or not scope_id
    ):
        return frozenset()
    effective_tenant_id = tenant_id
    if scope_type == "tenant":
        effective_tenant_id = effective_tenant_id or scope_id
        if effective_tenant_id != scope_id:
            return frozenset()
    elif effective_tenant_id is None:
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
            tenant_id=effective_tenant_id,
        )
        for permission in permission_names
    )


def permissions_for_grants(
    grants: Iterable[RoleGrant],
    *,
    tenant_id: str,
) -> frozenset[ScopedPermission]:
    """Expand each persisted role grant at its own trusted scope."""

    permissions: set[ScopedPermission] = set()
    for grant in grants:
        permissions.update(
            permissions_for_roles(
                {grant.role},
                scope_type=grant.scope_type,
                scope_id=grant.scope_id,
                tenant_id=tenant_id,
            )
        )
    return frozenset(permissions)
