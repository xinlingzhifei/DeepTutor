"""Request-local tenant selection and concrete permission resolution."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException, status

from deeptutor.api.routers.auth import (
    require_auth,
    require_platform_identity_from_payload,
)
from deeptutor.multi_user.context import (
    get_current_tenant_or_none,
    get_current_user,
    get_current_user_or_none,
    set_current_tenant,
)
from deeptutor.services.auth import TokenPayload
from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.permissions import (
    KNOWN_PERMISSIONS,
    ScopedPermission,
    permissions_for_grants,
    permissions_for_roles,
)
from deeptutor.teaching.repositories.tenants import (
    TenantAccessDeniedError,
    TenantNotActiveError,
    TenantNotFoundError,
    TenantRepository,
    get_tenant_repository,
)
from deeptutor.teaching.schema_names import tenant_schema_name

LOCAL_TENANT_ID = "local"
LOCAL_TENANT_NAME = "Local"


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    schema_name: str
    user_id: str
    permissions: frozenset[ScopedPermission]


def _local_tenant_context(user_id: str) -> TenantContext:
    permissions = frozenset(
        ScopedPermission(
            permission=permission,
            scope_type="tenant",
            scope_id=LOCAL_TENANT_ID,
            tenant_id=LOCAL_TENANT_ID,
        )
        for permission in KNOWN_PERMISSIONS
    )
    return TenantContext(
        tenant_id=LOCAL_TENANT_ID,
        schema_name=tenant_schema_name(LOCAL_TENANT_ID),
        user_id=user_id,
        permissions=permissions,
    )


def resolve_runtime_tenant_context() -> TenantContext:
    """Return the tenant installed by a trusted server entry point."""

    context = get_current_tenant_or_none()
    if context is not None:
        return context
    raise RuntimeError("tenant context is not installed")


def _requested_tenant(
    header_value: str | None,
    cookie_value: str | None,
) -> str | None:
    normalized_header = header_value.strip() if header_value is not None else None
    if cookie_value is None:
        return None
    tenant_id = cookie_value.strip()
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant selection cannot be empty",
        )
    if normalized_header and normalized_header != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conflicting tenant selection",
        )
    return tenant_id


def _raise_http_for_repository_error(error: Exception) -> None:
    if isinstance(error, TenantNotActiveError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant is not active",
        ) from error
    if isinstance(error, TenantNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        ) from error
    if isinstance(error, TenantAccessDeniedError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active membership for tenant",
        ) from error
    raise error


async def require_tenant(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    dt_tenant: str | None = Cookie(default=None),
    payload: TokenPayload | None = Depends(require_auth),
    repository: TenantRepository = Depends(get_tenant_repository),
) -> TenantContext:
    """Resolve and install one trusted request tenant."""

    platform_enabled = load_platform_settings().enabled
    requested_tenant_id = None
    if platform_enabled:
        require_platform_identity_from_payload(payload)
        requested_tenant_id = _requested_tenant(x_tenant_id, dt_tenant)

    context = await resolve_tenant_context(
        requested_tenant_id=requested_tenant_id,
        repository=repository,
    )
    set_current_tenant(context)
    return context


async def resolve_tenant_context(
    *,
    requested_tenant_id: str | None,
    repository: TenantRepository,
    require_explicit_selection: bool = False,
) -> TenantContext:
    """Resolve one trusted tenant for HTTP or a long-lived connection."""

    if not load_platform_settings().enabled:
        return _local_tenant_context(get_current_user().id)

    current_user = get_current_user_or_none()
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant platform requires authentication",
        )

    if requested_tenant_id is not None:
        requested_tenant_id = requested_tenant_id.strip()
        if not requested_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tenant selection cannot be empty",
            )

    is_platform_admin = current_user.role == "admin"
    if require_explicit_selection and requested_tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Select an active tenant first",
        )
    if is_platform_admin and requested_tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Select an active tenant first",
        )

    if requested_tenant_id is None:
        choices = await repository.list_tenants(
            current_user.id,
            is_platform_admin=False,
        )
        if not choices:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No active tenant membership",
            )
        if len(choices) != 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Select an active tenant first",
            )
        requested_tenant_id = choices[0].tenant_id

    try:
        access = await repository.get_tenant_access(
            requested_tenant_id,
            current_user.id,
            is_platform_admin=is_platform_admin,
        )
    except (TenantAccessDeniedError, TenantNotActiveError, TenantNotFoundError) as exc:
        _raise_http_for_repository_error(exc)
        raise AssertionError("unreachable") from exc

    permissions = permissions_for_grants(
        access.grants,
        tenant_id=access.summary.tenant_id,
    )
    if is_platform_admin:
        permissions |= permissions_for_roles(
            {"platform_admin"},
            scope_type="tenant",
            scope_id=access.summary.tenant_id,
        )
    return TenantContext(
        tenant_id=access.summary.tenant_id,
        schema_name=access.schema_name,
        user_id=current_user.id,
        permissions=permissions,
    )
