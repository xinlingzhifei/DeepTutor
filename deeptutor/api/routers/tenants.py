"""Tenant selection, provisioning intent, and membership administration."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from deeptutor.api.routers.auth import (
    _COOKIE_MAX_AGE,
    _cookie_attrs,
    require_auth,
    require_platform_admin,
    require_platform_enabled,
    require_platform_identity_from_payload,
)
from deeptutor.multi_user.context import get_current_user
from deeptutor.services.auth import TokenPayload
from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.permissions import DEFAULT_ROLE_PERMISSIONS, RoleGrant
from deeptutor.teaching.repositories.tenants import (
    GrantResourceNotFoundError,
    InvalidGrantScopeError,
    ProvisioningSummary,
    TenantAccessDeniedError,
    TenantConflictError,
    TenantNotActiveError,
    TenantNotFoundError,
    TenantRepository,
    TenantSummary,
    UnknownRoleError,
    get_tenant_repository,
)
from deeptutor.teaching.services.tenant_provisioning import (
    TenantProvisioningService,
    get_tenant_provisioning_service,
)
from deeptutor.teaching.tenant_context import (
    LOCAL_TENANT_ID,
    LOCAL_TENANT_NAME,
    TenantContext,
    require_tenant,
)

router = APIRouter()

_TENANT_COOKIE_NAME = "dt_tenant"


class TenantSummaryResponse(BaseModel):
    tenant_id: str
    name: str
    status: str


class TenantListResponse(BaseModel):
    tenants: list[TenantSummaryResponse]


class ActiveTenantRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)

    @field_validator("tenant_id")
    @classmethod
    def normalize_tenant_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tenant_id is required")
        return normalized


class ActiveTenantResponse(BaseModel):
    active_tenant_id: str


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tenant name is required")
        return normalized


class CreateTenantResponse(BaseModel):
    tenant_id: str
    status: str
    job_id: str


class ProvisioningStatusResponse(BaseModel):
    tenant_id: str
    status: str
    job_id: str
    job_status: str
    attempt_count: int


class MemberRoleGrant(BaseModel):
    role: str = Field(min_length=1, max_length=64)
    scope_type: Literal["tenant", "course", "class"]
    scope_id: str = Field(min_length=1, max_length=64)

    @field_validator("role", "scope_id")
    @classmethod
    def normalize_fields(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in DEFAULT_ROLE_PERMISSIONS:
            raise ValueError("unknown role")
        return value


class AddMemberRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    role: str | None = None
    grants: list[MemberRoleGrant] | None = Field(default=None, min_length=1)

    @field_validator("user_id")
    @classmethod
    def normalize_user_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized

    @field_validator("role")
    @classmethod
    def normalize_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized not in DEFAULT_ROLE_PERMISSIONS:
            raise ValueError("unknown role")
        return normalized

    @model_validator(mode="after")
    def validate_grant_shape(self) -> AddMemberRequest:
        if self.grants is not None and self.role is not None:
            raise ValueError("provide exactly one of role or grants")
        if self.grants is None and self.role is None:
            self.role = "student"
        return self


class ReplaceGrantsRequest(BaseModel):
    roles: list[str] | None = Field(default=None, min_length=1)
    grants: list[MemberRoleGrant] | None = Field(default=None, min_length=1)

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if (
            not normalized
            or any(not role for role in normalized)
            or not set(normalized).issubset(DEFAULT_ROLE_PERMISSIONS)
        ):
            raise ValueError("unknown role")
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_one_grant_shape(self) -> ReplaceGrantsRequest:
        if (self.roles is None) == (self.grants is None):
            raise ValueError("provide exactly one of roles or grants")
        return self


class MemberGrantsResponse(BaseModel):
    tenant_id: str
    user_id: str
    roles: list[str]
    grants: list[MemberRoleGrant]


def _member_grants_response(
    tenant_id: str,
    user_id: str,
    grants: frozenset[RoleGrant],
) -> MemberGrantsResponse:
    ordered_grants = sorted(
        grants,
        key=lambda grant: (grant.role, grant.scope_type, grant.scope_id),
    )
    return MemberGrantsResponse(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=sorted({grant.role for grant in grants}),
        grants=[
            MemberRoleGrant(
                role=grant.role,
                scope_type=grant.scope_type,
                scope_id=grant.scope_id,
            )
            for grant in ordered_grants
        ],
    )


def _raise_repository_http(error: Exception) -> None:
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
    if isinstance(error, GrantResourceNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scoped grant resource not found",
        ) from error
    if isinstance(error, InvalidGrantScopeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid scoped grant",
        ) from error
    if isinstance(error, (TenantConflictError, UnknownRoleError)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant operation conflicts with current state",
        ) from error
    raise error


def _summary_response(summary: TenantSummary) -> TenantSummaryResponse:
    return TenantSummaryResponse(
        tenant_id=summary.tenant_id,
        name=summary.name,
        status=summary.status,
    )


def _provisioning_response(
    summary: ProvisioningSummary,
) -> ProvisioningStatusResponse:
    return ProvisioningStatusResponse(
        tenant_id=summary.tenant_id,
        status=summary.status,
        job_id=summary.job_id,
        job_status=summary.job_status,
        attempt_count=summary.attempt_count,
    )


def _require_tenant_management(context: TenantContext, tenant_id: str) -> None:
    if context.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context does not match path tenant",
        )
    if not any(
        permission.allows("tenant.manage", "tenant", tenant_id)
        for permission in context.permissions
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant.manage permission required",
        )


@router.get("/mine", response_model=TenantListResponse)
async def list_my_tenants(
    payload: TokenPayload | None = Depends(require_auth),
    repository: TenantRepository = Depends(get_tenant_repository),
) -> TenantListResponse:
    if not load_platform_settings().enabled:
        return TenantListResponse(
            tenants=[
                TenantSummaryResponse(
                    tenant_id=LOCAL_TENANT_ID,
                    name=LOCAL_TENANT_NAME,
                    status="active",
                )
            ]
        )
    identity = require_platform_identity_from_payload(payload)
    current_user = get_current_user()
    summaries = await repository.list_tenants(
        current_user.id,
        is_platform_admin=identity.role == "admin",
    )
    return TenantListResponse(tenants=[_summary_response(summary) for summary in summaries])


@router.put("/active", response_model=ActiveTenantResponse)
async def set_active_tenant(
    body: ActiveTenantRequest,
    response: Response,
    payload: TokenPayload | None = Depends(require_auth),
    repository: TenantRepository = Depends(get_tenant_repository),
) -> ActiveTenantResponse:
    if not load_platform_settings().enabled:
        if body.tenant_id != LOCAL_TENANT_ID:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tenant not found",
            )
    else:
        identity = require_platform_identity_from_payload(payload)
        current_user = get_current_user()
        try:
            await repository.get_tenant_access(
                body.tenant_id,
                current_user.id,
                is_platform_admin=identity.role == "admin",
            )
        except (
            TenantAccessDeniedError,
            TenantNotActiveError,
            TenantNotFoundError,
        ) as exc:
            _raise_repository_http(exc)
    response.set_cookie(
        value=body.tenant_id,
        max_age=_COOKIE_MAX_AGE,
        **_cookie_attrs(_TENANT_COOKIE_NAME),
    )
    return ActiveTenantResponse(active_tenant_id=body.tenant_id)


@router.post(
    "",
    response_model=CreateTenantResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_tenant(
    body: CreateTenantRequest,
    payload: TokenPayload = Depends(require_platform_admin),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
    service: TenantProvisioningService = Depends(get_tenant_provisioning_service),
) -> CreateTenantResponse:
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 256:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Idempotency-Key",
            )
    try:
        summary = await service.create(
            actor_id=payload.user_id,
            name=body.name,
            idempotency_key=idempotency_key,
        )
    except TenantConflictError as exc:
        _raise_repository_http(exc)
        raise AssertionError("unreachable") from exc
    return CreateTenantResponse(
        tenant_id=summary.tenant_id,
        status=summary.status,
        job_id=summary.job_id,
    )


@router.get(
    "/{tenant_id}/provisioning",
    response_model=ProvisioningStatusResponse,
)
async def get_provisioning_status(
    tenant_id: str,
    _payload: TokenPayload = Depends(require_platform_admin),
    repository: TenantRepository = Depends(get_tenant_repository),
) -> ProvisioningStatusResponse:
    try:
        summary = await repository.get_provisioning(tenant_id)
    except TenantNotFoundError as exc:
        _raise_repository_http(exc)
        raise AssertionError("unreachable") from exc
    return _provisioning_response(summary)


@router.post(
    "/{tenant_id}/members",
    response_model=MemberGrantsResponse,
)
async def add_member(
    tenant_id: str,
    body: AddMemberRequest,
    _platform_enabled: None = Depends(require_platform_enabled),
    context: TenantContext = Depends(require_tenant),
    repository: TenantRepository = Depends(get_tenant_repository),
) -> MemberGrantsResponse:
    _require_tenant_management(context, tenant_id)
    if body.grants is not None:
        grants = frozenset(
            RoleGrant(
                role=grant.role,
                scope_type=grant.scope_type,
                scope_id=grant.scope_id,
            )
            for grant in body.grants
        )
    else:
        assert body.role is not None
        grants = frozenset(
            {
                RoleGrant(
                    role=body.role,
                    scope_type="tenant",
                    scope_id=tenant_id,
                )
            }
        )
    try:
        await repository.upsert_member_with_scoped_grants(
            tenant_id,
            body.user_id,
            grants,
        )
    except (
        GrantResourceNotFoundError,
        InvalidGrantScopeError,
        TenantAccessDeniedError,
        TenantConflictError,
        TenantNotFoundError,
        UnknownRoleError,
    ) as exc:
        _raise_repository_http(exc)
    return _member_grants_response(tenant_id, body.user_id, grants)


@router.put(
    "/{tenant_id}/members/{user_id}/grants",
    response_model=MemberGrantsResponse,
)
async def replace_member_grants(
    tenant_id: str,
    user_id: str,
    body: ReplaceGrantsRequest,
    _platform_enabled: None = Depends(require_platform_enabled),
    context: TenantContext = Depends(require_tenant),
    repository: TenantRepository = Depends(get_tenant_repository),
) -> MemberGrantsResponse:
    _require_tenant_management(context, tenant_id)
    normalized_user_id = user_id.strip()
    if not normalized_user_id or len(normalized_user_id) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id",
        )
    if body.grants is not None:
        grants = frozenset(
            RoleGrant(
                role=grant.role,
                scope_type=grant.scope_type,
                scope_id=grant.scope_id,
            )
            for grant in body.grants
        )
    else:
        roles = frozenset(body.roles or ())
        grants = frozenset(
            RoleGrant(
                role=role,
                scope_type="tenant",
                scope_id=tenant_id,
            )
            for role in roles
        )
    try:
        if body.grants is not None:
            await repository.replace_scoped_grants(
                tenant_id,
                normalized_user_id,
                grants,
            )
        else:
            await repository.replace_grants(
                tenant_id,
                normalized_user_id,
                roles,
            )
    except (
        GrantResourceNotFoundError,
        InvalidGrantScopeError,
        TenantAccessDeniedError,
        TenantConflictError,
        TenantNotFoundError,
        UnknownRoleError,
    ) as exc:
        _raise_repository_http(exc)
    return _member_grants_response(tenant_id, normalized_user_id, grants)
