from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import sys
from types import SimpleNamespace
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.api.routers import auth as auth_router
from deeptutor.api.routers import tenants as tenants_router
from deeptutor.services import auth as auth_service
from deeptutor.services import pocketbase_client
from deeptutor.services.auth import TokenPayload
from deeptutor.teaching import tenant_context as tenant_context_module
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.repositories import tenants as tenant_repositories
from deeptutor.teaching.repositories.tenants import (
    GrantResourceNotFoundError,
    ProvisioningSummary,
    TenantAccess,
    TenantAccessDeniedError,
    TenantConflictError,
    TenantNotActiveError,
    TenantNotFoundError,
    TenantSummary,
    build_accessible_tenants_statement,
    build_tenant_access_statement,
    get_tenant_repository,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.tenant_provisioning import (
    TenantProvisioningService,
    get_tenant_provisioning_service,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant


def _user_record(
    user_id: str,
    role: str,
    *,
    username: str | None = None,
    disabled: bool = False,
) -> dict[str, Any]:
    return {
        "id": user_id,
        "username": username or user_id,
        "role": role,
        "created_at": "",
        "disabled": disabled,
        "avatar": "",
    }


class FakeTenantRepository:
    def __init__(self) -> None:
        self.tenants: dict[str, TenantSummary] = {}
        self.memberships: dict[tuple[str, str], str] = {}
        self.roles: dict[tuple[str, str], frozenset[str]] = {}
        self.scoped_access_grants: dict[tuple[str, str], tuple[Any, ...]] = {}
        self.provisioning: dict[str, ProvisioningSummary] = {}
        self.created_provisioning = 0
        self.create_calls = 0
        self.provisioning_reads = 0
        self.list_calls = 0
        self.list_admin_flags: list[bool] = []
        self.access_calls: list[tuple[str, str, bool]] = []
        self.member_calls: list[tuple[str, str, frozenset[str]]] = []
        self.scoped_member_calls: list[tuple[str, str, frozenset[tuple[str, str, str]]]] = []
        self.grant_calls: list[tuple[str, str, frozenset[str]]] = []
        self.scoped_grant_calls: list[tuple[str, str, frozenset[tuple[str, str, str]]]] = []
        self.scoped_grant_error: Exception | None = None
        self.activation_calls: list[tuple[str, str, int]] = []
        self.failure_calls: list[tuple[str, str, int]] = []
        self.policy_calls: list[tuple[str, str, int]] = []
        self.ready_routes: set[str] = set()
        self.ready_storage: set[str] = set()
        self.verified_policy_attempts: set[tuple[str, str, int]] = set()
        self.failure_text = ""

    def add_tenant(
        self,
        tenant_id: str,
        *,
        name: str | None = None,
        status: str = "active",
    ) -> None:
        self.tenants[tenant_id] = TenantSummary(
            tenant_id=tenant_id,
            name=name or tenant_id,
            status=status,
        )

    def add_member(
        self,
        tenant_id: str,
        user_id: str,
        *,
        status: str = "active",
        roles: frozenset[str] = frozenset({"student"}),
    ) -> None:
        self.memberships[(tenant_id, user_id)] = status
        self.roles[(tenant_id, user_id)] = roles

    def set_provisioning(
        self,
        tenant_id: str,
        job_id: str,
        *,
        status: str,
        job_status: str,
        attempt_count: int,
    ) -> None:
        existing_tenant = self.tenants.get(tenant_id)
        self.add_tenant(
            tenant_id,
            name=existing_tenant.name if existing_tenant else None,
            status=status,
        )
        self.provisioning[tenant_id] = ProvisioningSummary(
            tenant_id=tenant_id,
            status=status,
            job_id=job_id,
            job_status=job_status,
            attempt_count=attempt_count,
        )

    async def list_tenants(
        self,
        user_id: str,
        *,
        is_platform_admin: bool,
    ) -> tuple[TenantSummary, ...]:
        self.list_calls += 1
        self.list_admin_flags.append(is_platform_admin)
        selectable = []
        for summary in self.tenants.values():
            if summary.status != "active":
                continue
            if is_platform_admin or self.memberships.get((summary.tenant_id, user_id)) == "active":
                selectable.append(summary)
        return tuple(sorted(selectable, key=lambda item: item.tenant_id))

    async def get_tenant_access(
        self,
        tenant_id: str,
        user_id: str,
        *,
        is_platform_admin: bool,
    ) -> TenantAccess:
        self.access_calls.append((tenant_id, user_id, is_platform_admin))
        summary = self.tenants.get(tenant_id)
        if summary is None:
            if is_platform_admin:
                raise TenantNotFoundError(tenant_id)
            raise TenantAccessDeniedError(tenant_id)
        if not is_platform_admin and self.memberships.get((tenant_id, user_id)) != "active":
            raise TenantAccessDeniedError(tenant_id)
        if summary.status != "active":
            raise TenantNotActiveError(tenant_id)
        roles = self.roles.get((tenant_id, user_id), frozenset())
        grants = self.scoped_access_grants.get((tenant_id, user_id))
        if grants is not None:
            return SimpleNamespace(
                summary=summary,
                schema_name=tenant_schema_name(tenant_id),
                roles=roles,
                grants=grants,
            )
        return TenantAccess(
            summary=summary,
            schema_name=tenant_schema_name(tenant_id),
            roles=roles,
        )

    async def create_provisioning(
        self,
        *,
        tenant_id: str,
        job_id: str,
        name: str,
    ) -> ProvisioningSummary:
        self.create_calls += 1
        existing = self.provisioning.get(tenant_id)
        if existing is not None:
            if self.tenants[tenant_id].name != name:
                raise TenantConflictError("idempotency payload conflict")
            if existing.status == existing.job_status == "failed":
                retried = replace(
                    existing,
                    status="provisioning",
                    job_status="pending",
                    attempt_count=existing.attempt_count + 1,
                )
                self.provisioning[tenant_id] = retried
                self.tenants[tenant_id] = replace(
                    self.tenants[tenant_id],
                    status="provisioning",
                )
                return retried
            return existing
        self.created_provisioning += 1
        self.add_tenant(tenant_id, name=name, status="provisioning")
        summary = ProvisioningSummary(
            tenant_id=tenant_id,
            status="provisioning",
            job_id=job_id,
            job_status="pending",
            attempt_count=0,
        )
        self.provisioning[tenant_id] = summary
        return summary

    async def get_provisioning(self, tenant_id: str) -> ProvisioningSummary:
        self.provisioning_reads += 1
        try:
            return self.provisioning[tenant_id]
        except KeyError as exc:
            raise TenantNotFoundError(tenant_id) from exc

    async def activate_if_ready(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        self.activation_calls.append((tenant_id, job_id, expected_attempt_count))
        summary = self.provisioning[tenant_id]
        current_attempt = (tenant_id, job_id, expected_attempt_count)
        if (
            not self._is_current_attempt(summary, job_id, expected_attempt_count)
            or tenant_id not in self.ready_routes
            or tenant_id not in self.ready_storage
            or current_attempt not in self.verified_policy_attempts
        ):
            return False
        self.provisioning[tenant_id] = replace(
            summary,
            status="active",
            job_status="completed",
        )
        self.tenants[tenant_id] = replace(self.tenants[tenant_id], status="active")
        return True

    async def mark_provisioning_failed(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        self.failure_calls.append((tenant_id, job_id, expected_attempt_count))
        summary = self.provisioning[tenant_id]
        if not self._is_current_attempt(summary, job_id, expected_attempt_count):
            return False
        self.provisioning[tenant_id] = replace(
            summary,
            status="failed",
            job_status="failed",
        )
        self.tenants[tenant_id] = replace(self.tenants[tenant_id], status="failed")
        return True

    async def record_policy_verified(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        self.policy_calls.append((tenant_id, job_id, expected_attempt_count))
        summary = self.provisioning[tenant_id]
        if not self._is_current_attempt(summary, job_id, expected_attempt_count):
            return False
        self.verified_policy_attempts.add((tenant_id, job_id, expected_attempt_count))
        return True

    @staticmethod
    def _is_current_attempt(
        summary: ProvisioningSummary,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        return (
            summary.job_id == job_id
            and summary.status == "provisioning"
            and summary.job_status in {"pending", "running"}
            and summary.attempt_count == expected_attempt_count
        )

    async def upsert_member(
        self,
        tenant_id: str,
        user_id: str,
        roles: frozenset[str],
    ) -> None:
        self.member_calls.append((tenant_id, user_id, roles))
        self.add_member(tenant_id, user_id, roles=roles)

    async def upsert_member_with_scoped_grants(
        self,
        tenant_id: str,
        user_id: str,
        grants: frozenset[Any],
    ) -> None:
        normalized = frozenset((grant.role, grant.scope_type, grant.scope_id) for grant in grants)
        self.scoped_member_calls.append((tenant_id, user_id, normalized))
        self.add_member(
            tenant_id,
            user_id,
            roles=frozenset(role for role, _scope_type, _scope_id in normalized),
        )

    async def replace_grants(
        self,
        tenant_id: str,
        user_id: str,
        roles: frozenset[str],
    ) -> None:
        if self.memberships.get((tenant_id, user_id)) != "active":
            raise TenantAccessDeniedError(tenant_id)
        self.grant_calls.append((tenant_id, user_id, roles))
        self.roles[(tenant_id, user_id)] = roles

    async def replace_scoped_grants(
        self,
        tenant_id: str,
        user_id: str,
        grants: frozenset[Any],
    ) -> None:
        if self.scoped_grant_error is not None:
            raise self.scoped_grant_error
        normalized = frozenset((grant.role, grant.scope_type, grant.scope_id) for grant in grants)
        self.scoped_grant_calls.append((tenant_id, user_id, normalized))
        self.roles[(tenant_id, user_id)] = frozenset(
            role for role, _scope_type, _scope_id in normalized
        )


def _set_platform_enabled(monkeypatch: Any, enabled: bool) -> None:
    settings = SimpleNamespace(enabled=enabled)
    monkeypatch.setattr(
        tenant_context_module,
        "load_platform_settings",
        lambda: settings,
    )
    monkeypatch.setattr(tenants_router, "load_platform_settings", lambda: settings)
    monkeypatch.setattr(auth_router, "load_platform_settings", lambda: settings)


def _authenticated_app(
    monkeypatch: Any,
    repository: FakeTenantRepository,
    *,
    user_id: str = "u-alice",
    role: str = "user",
    platform_enabled: bool = True,
) -> FastAPI:
    _set_platform_enabled(monkeypatch, platform_enabled)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    payload = TokenPayload(username=user_id, role=role, user_id=user_id)
    monkeypatch.setattr(
        auth_router,
        "get_user_info",
        lambda username: _user_record(user_id, role, username=username),
    )

    async def fake_auth() -> TokenPayload:
        auth_router._install_current_user(payload)
        return payload

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1/auth")
    app.include_router(tenants_router.router, prefix="/api/v1/tenants")

    @app.get("/context")
    async def read_context(context: TenantContext = Depends(require_tenant)) -> dict:
        return {
            "tenant_id": context.tenant_id,
            "schema_name": context.schema_name,
            "permissions": sorted(item.permission for item in context.permissions),
            "permission_scopes": sorted(
                (
                    item.permission,
                    item.scope_type,
                    item.scope_id,
                )
                for item in context.permissions
            ),
        }

    app.dependency_overrides[auth_router.require_auth] = fake_auth
    app.dependency_overrides[get_tenant_repository] = lambda: repository
    app.dependency_overrides[get_tenant_provisioning_service] = lambda: TenantProvisioningService(
        repository
    )
    return app


def _pocketbase_chain_app(
    monkeypatch: Any,
    repository: FakeTenantRepository,
    record: Any,
) -> tuple[FastAPI, dict[tuple[str, str], tuple[dict[str, Any], float]]]:
    class FakeAuthStore:
        def save(self, token: str, _record: Any) -> None:
            self.token = token

    class FakeUsers:
        def auth_refresh(self) -> Any:
            return SimpleNamespace(token="refreshed-token", record=record)

    class FakePocketBase:
        def __init__(self, _url: str) -> None:
            self.auth_store = FakeAuthStore()

        def collection(self, name: str) -> FakeUsers:
            assert name == "users"
            return FakeUsers()

    token_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
    monkeypatch.setitem(
        sys.modules,
        "pocketbase",
        SimpleNamespace(PocketBase=FakePocketBase),
    )
    monkeypatch.setattr(
        pocketbase_client,
        "_pocketbase_settings",
        lambda: {"url": "http://pocketbase.test", "admin_email": "", "admin_password": ""},
    )
    monkeypatch.setattr(pocketbase_client, "_TOKEN_CACHE", token_cache)
    monkeypatch.setattr(auth_service, "POCKETBASE_ENABLED", True)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_router, "POCKETBASE_ENABLED", True)
    _set_platform_enabled(monkeypatch, True)

    def fail_local_user_lookup(_username: str) -> None:
        raise AssertionError("PocketBase identity must not query the local user store")

    monkeypatch.setattr(auth_router, "get_user_info", fail_local_user_lookup)
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1/auth")

    @app.get("/context")
    async def read_context(context: TenantContext = Depends(require_tenant)) -> dict:
        return {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
        }

    app.dependency_overrides[get_tenant_repository] = lambda: repository
    return app, token_cache


def _assert_repository_unused(repository: FakeTenantRepository) -> None:
    assert repository.list_calls == 0
    assert repository.access_calls == []
    assert repository.create_calls == 0
    assert repository.provisioning_reads == 0
    assert repository.member_calls == []
    assert repository.grant_calls == []


def _control_plane_requests(tenant_id: str) -> tuple[tuple[Any, ...], ...]:
    return (
        ("POST", "/api/v1/tenants", {"name": "Calculus"}, None),
        ("GET", f"/api/v1/tenants/{tenant_id}/provisioning", None, None),
        (
            "POST",
            f"/api/v1/tenants/{tenant_id}/members",
            {"user_id": "u-student"},
            {"Cookie": f"dt_tenant={tenant_id}"},
        ),
        (
            "PUT",
            f"/api/v1/tenants/{tenant_id}/members/u-student/grants",
            {"roles": ["student"]},
            {"Cookie": f"dt_tenant={tenant_id}"},
        ),
    )


def test_local_platform_disabled_does_not_touch_repository(monkeypatch) -> None:
    class RepositoryThatMustNotBeUsed(FakeTenantRepository):
        async def list_tenants(self, *args: Any, **kwargs: Any) -> tuple:
            raise AssertionError("platform-disabled requests must not query the DB")

        async def get_tenant_access(self, *args: Any, **kwargs: Any) -> TenantAccess:
            raise AssertionError("platform-disabled requests must not query the DB")

    repository = RepositoryThatMustNotBeUsed()
    _set_platform_enabled(monkeypatch, False)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    app = FastAPI()

    @app.get("/context")
    async def read_context(context: TenantContext = Depends(require_tenant)) -> dict:
        return {
            "tenant_id": context.tenant_id,
            "schema_name": context.schema_name,
        }

    app.dependency_overrides[get_tenant_repository] = lambda: repository

    response = TestClient(app).get(
        "/context",
        headers={
            "X-Tenant-ID": "tenant-b",
            "Cookie": "dt_tenant=tenant-a",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "local",
        "schema_name": tenant_schema_name("local"),
    }
    assert repository.list_calls == 0
    assert repository.access_calls == []


def test_platform_enabled_without_auth_fails_closed_before_repository(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_member(
        "tenant-a",
        "local-admin",
        roles=frozenset({"platform_admin"}),
    )
    app = _authenticated_app(monkeypatch, repository)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )

    async def auth_disabled() -> None:
        auth_router._install_current_user(None)
        return None

    app.dependency_overrides[auth_router.require_auth] = auth_disabled
    requests = (
        ("GET", "/context", None, {"Cookie": "dt_tenant=tenant-a"}),
        ("GET", "/api/v1/tenants/mine", None, None),
        (
            "PUT",
            "/api/v1/tenants/active",
            {"tenant_id": "tenant-a"},
            None,
        ),
        *_control_plane_requests("tenant-a"),
        ("GET", "/api/v1/auth/status", None, None),
    )

    with TestClient(app) as client:
        responses = [
            client.request(method, path, json=body, headers=headers)
            for method, path, body, headers in requests
        ]

    assert [response.status_code for response in responses] == [503] * len(requests)
    _assert_repository_unused(repository)


def test_platform_disabled_control_plane_routes_return_409_without_repository(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    app = _authenticated_app(
        monkeypatch,
        repository,
        platform_enabled=False,
    )
    app.dependency_overrides.pop(auth_router.require_auth)
    requests = _control_plane_requests("local")

    with TestClient(app) as client:
        responses = [
            client.request(method, path, json=body, headers=headers)
            for method, path, body, headers in requests
        ]

    assert [response.status_code for response in responses] == [409] * len(requests)
    _assert_repository_unused(repository)


def test_authoritative_role_downgrade_removes_admin_and_tenant_bypasses(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-root",
        role="admin",
    )
    monkeypatch.setattr(
        auth_router,
        "get_user_info",
        lambda _username: _user_record("u-root", "user"),
    )

    with TestClient(app) as client:
        created = client.post("/api/v1/tenants", json={"name": "Calculus"})
        mine = client.get("/api/v1/tenants/mine")
        selected = client.get("/context", headers={"Cookie": "dt_tenant=tenant-a"})

    assert created.status_code == 403
    assert mine.status_code == 200
    assert mine.json() == {"tenants": []}
    assert selected.status_code == 403
    assert repository.create_calls == 0
    assert repository.list_admin_flags == [False]
    assert repository.access_calls == [("tenant-a", "u-root", False)]


@pytest.mark.parametrize("account_state", ["deleted", "disabled", "id-mismatch"])
def test_invalid_authoritative_user_is_rejected_and_status_is_anonymous(
    monkeypatch,
    account_state: str,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-root",
        role="admin",
    )
    stale_payload = TokenPayload(username="u-root", role="admin", user_id="u-root")
    user_info = None
    if account_state != "deleted":
        user_info = _user_record(
            "u-other" if account_state == "id-mismatch" else "u-root",
            "admin",
            username="u-root",
            disabled=account_state == "disabled",
        )
    monkeypatch.setattr(auth_router, "get_user_info", lambda _username: user_info)
    monkeypatch.setattr(auth_router, "decode_token", lambda _token: stale_payload)
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )

    with TestClient(app) as client:
        context_response = client.get(
            "/context",
            headers={"Cookie": "dt_tenant=tenant-a"},
        )
        create_response = client.post(
            "/api/v1/tenants",
            json={"name": "Calculus"},
        )
        status_response = client.get(
            "/api/v1/auth/status",
            headers={"Authorization": "Bearer stale"},
        )

    assert context_response.status_code == 401
    assert create_response.status_code == 401
    assert status_response.status_code == 200
    assert status_response.json()["authenticated"] is False
    assert status_response.json()["role"] is None
    assert status_response.json()["tenants"] == []
    _assert_repository_unused(repository)


def test_single_active_membership_is_selected_automatically_despite_header(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_tenant("tenant-b")
    repository.add_member("tenant-a", "u-alice", roles=frozenset({"teacher"}))
    app = _authenticated_app(monkeypatch, repository)

    response = TestClient(app).get(
        "/context",
        headers={"X-Tenant-ID": "tenant-b"},
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"
    assert "classroom.edit" in response.json()["permissions"]
    assert repository.access_calls == [("tenant-a", "u-alice", False)]


def test_context_expands_persisted_grants_at_their_real_resource_scope(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_member("tenant-a", "u-teacher", roles=frozenset({"teacher"}))
    repository.scoped_access_grants[("tenant-a", "u-teacher")] = (
        SimpleNamespace(role="teacher", scope_type="class", scope_id="class-a"),
    )
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-teacher",
    )

    response = TestClient(app).get(
        "/context",
        headers={"Cookie": "dt_tenant=tenant-a"},
    )

    assert response.status_code == 200
    edit_scopes = {
        tuple(scope)
        for scope in response.json()["permission_scopes"]
        if scope[0] == "classroom.edit"
    }
    assert edit_scopes == {("classroom.edit", "class", "class-a")}


def test_header_only_with_multiple_memberships_requires_controlled_switch(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    for tenant_id in ("tenant-a", "tenant-b"):
        repository.add_tenant(tenant_id)
        repository.add_member(tenant_id, "u-alice")
    app = _authenticated_app(monkeypatch, repository)

    response = TestClient(app).get(
        "/context",
        headers={"X-Tenant-ID": "tenant-a"},
    )

    assert response.status_code == 409
    assert "select" in response.json()["detail"].lower()
    assert repository.access_calls == []


def test_conflicting_header_and_cookie_fail_closed_without_access_lookup(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    for tenant_id in ("tenant-a", "tenant-b"):
        repository.add_tenant(tenant_id)
        repository.add_member(tenant_id, "u-alice")
    app = _authenticated_app(monkeypatch, repository)

    response = TestClient(app).get(
        "/context",
        headers={
            "X-Tenant-ID": "tenant-b",
            "Cookie": "dt_tenant=tenant-a",
        },
    )

    assert response.status_code == 400
    assert repository.access_calls == []


def test_matching_header_and_cookie_use_cookie_selection(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_member("tenant-a", "u-alice")
    app = _authenticated_app(monkeypatch, repository)

    response = TestClient(app).get(
        "/context",
        headers={
            "X-Tenant-ID": " tenant-a ",
            "Cookie": "dt_tenant=tenant-a",
        },
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"
    assert repository.access_calls == [("tenant-a", "u-alice", False)]


def test_empty_tenant_cookie_remains_invalid_when_header_is_present(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_member("tenant-a", "u-alice")
    app = _authenticated_app(monkeypatch, repository)

    response = TestClient(app).get(
        "/context",
        headers={
            "X-Tenant-ID": "tenant-a",
            "Cookie": "dt_tenant=",
        },
    )

    assert response.status_code == 400
    assert repository.access_calls == []


def test_normal_user_cannot_select_non_member_tenant(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    app = _authenticated_app(monkeypatch, repository)

    response = TestClient(app).put(
        "/api/v1/tenants/active",
        json={"tenant_id": "tenant-a"},
    )

    assert response.status_code == 403
    assert "dt_tenant=" not in response.headers.get("set-cookie", "")
    assert repository.access_calls == [("tenant-a", "u-alice", False)]


def test_inactive_membership_rejects_active_tenant_cookie(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_member("tenant-a", "u-alice")
    repository.memberships[("tenant-a", "u-alice")] = "inactive"
    app = _authenticated_app(monkeypatch, repository)

    response = TestClient(app).get(
        "/context",
        headers={"Cookie": "dt_tenant=tenant-a"},
    )

    assert response.status_code == 403
    assert repository.access_calls == [("tenant-a", "u-alice", False)]


def test_non_member_cannot_probe_inactive_tenant_state(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-p", status="provisioning")
    app = _authenticated_app(monkeypatch, repository)

    response = TestClient(app).put(
        "/api/v1/tenants/active",
        json={"tenant_id": "tenant-p"},
    )

    assert response.status_code == 403
    assert "dt_tenant=" not in response.headers.get("set-cookie", "")


def test_provisioning_tenant_is_not_selectable_or_listed(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-p", status="provisioning")
    repository.add_member("tenant-p", "u-alice")
    app = _authenticated_app(monkeypatch, repository)

    with TestClient(app) as client:
        context_response = client.get(
            "/context",
            headers={"Cookie": "dt_tenant=tenant-p"},
        )
        mine_response = client.get("/api/v1/tenants/mine")

    assert context_response.status_code == 409
    assert mine_response.status_code == 200
    assert mine_response.json() == {"tenants": []}


def test_platform_admin_requires_explicit_active_tenant(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-root",
        role="admin",
    )

    with TestClient(app) as client:
        missing = client.get("/context")
        switched = client.put(
            "/api/v1/tenants/active",
            json={"tenant_id": "tenant-a"},
        )
        selected = client.get("/context")

    assert missing.status_code == 409
    assert switched.status_code == 200
    assert selected.status_code == 200
    assert selected.json()["tenant_id"] == "tenant-a"
    assert "tenant.manage" in selected.json()["permissions"]


def test_auth_admin_mapping_is_independent_of_unknown_database_roles(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.roles[("tenant-a", "u-root")] = frozenset({"unknown-role"})
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-root",
        role="admin",
    )

    response = TestClient(app).get(
        "/context",
        headers={"Cookie": "dt_tenant=tenant-a"},
    )

    assert response.status_code == 200
    assert "tenant.manage" in response.json()["permissions"]


def test_active_cookie_uses_auth_cookie_security_attributes(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_member("tenant-a", "u-alice")
    app = _authenticated_app(monkeypatch, repository)
    monkeypatch.setattr(auth_router, "_SECURE", True)
    monkeypatch.setattr(auth_router, "_SAMESITE", "none")

    response = TestClient(app).put(
        "/api/v1/tenants/active",
        json={"tenant_id": "tenant-a"},
    )

    cookie = response.headers["set-cookie"].lower()
    assert response.status_code == 200
    assert response.json() == {"active_tenant_id": "tenant-a"}
    assert "dt_tenant=tenant-a" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=none" in cookie
    assert "path=/" in cookie


def test_logout_clears_auth_and_tenant_cookies_with_same_attributes(
    monkeypatch,
) -> None:
    monkeypatch.setattr(auth_router, "_SECURE", True)
    monkeypatch.setattr(auth_router, "_SAMESITE", "none")
    app = FastAPI()
    app.add_api_route("/logout", auth_router.logout, methods=["POST"])

    response = TestClient(app).post("/logout")

    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert any(item.startswith("dt_token=") for item in cookies)
    assert any(item.startswith("dt_tenant=") for item in cookies)
    for cookie in cookies:
        lowered = cookie.lower()
        assert "max-age=0" in lowered
        assert "secure" in lowered
        assert "samesite=none" in lowered
        assert "path=/" in lowered


def test_create_tenant_idempotency_binds_normalized_name(monkeypatch) -> None:
    repository = FakeTenantRepository()
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-root",
        role="admin",
    )

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/tenants",
            json={"name": "  Calculus  "},
            headers={"Idempotency-Key": "request-1"},
        )
        same_payload = client.post(
            "/api/v1/tenants",
            json={"name": "Calculus"},
            headers={"Idempotency-Key": "request-1"},
        )
        conflict = client.post(
            "/api/v1/tenants",
            json={"name": "Different retry body"},
            headers={"Idempotency-Key": "request-1"},
        )
        mine = client.get("/api/v1/tenants/mine")

    assert first.status_code == same_payload.status_code == 202
    assert same_payload.json() == first.json()
    assert conflict.status_code == 409
    assert first.json()["status"] == "provisioning"
    assert repository.created_provisioning == 1
    assert mine.json() == {"tenants": []}


def test_create_tenant_without_idempotency_key_creates_each_time(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-root",
        role="admin",
    )

    with TestClient(app) as client:
        first = client.post("/api/v1/tenants", json={"name": "One"})
        second = client.post("/api/v1/tenants", json={"name": "One"})

    assert first.status_code == second.status_code == 202
    assert first.json()["tenant_id"] != second.json()["tenant_id"]
    assert repository.created_provisioning == 2


def test_failed_idempotent_create_binds_name_then_requeues_same_pair(monkeypatch) -> None:
    repository = FakeTenantRepository()
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-root",
        role="admin",
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tenants",
            json={"name": "Calculus"},
            headers={"Idempotency-Key": "retryable-request"},
        )
        tenant_id = created.json()["tenant_id"]
        job_id = created.json()["job_id"]
        repository.set_provisioning(
            tenant_id,
            job_id,
            status="failed",
            job_status="failed",
            attempt_count=2,
        )
        before = repository.provisioning[tenant_id]

        conflict = client.post(
            "/api/v1/tenants",
            json={"name": "Physics"},
            headers={"Idempotency-Key": "retryable-request"},
        )
        after_conflict = repository.provisioning[tenant_id]
        retried = client.post(
            "/api/v1/tenants",
            json={"name": "  Calculus  "},
            headers={"Idempotency-Key": "retryable-request"},
        )

    assert conflict.status_code == 409
    assert after_conflict == before
    assert retried.status_code == 202
    assert retried.json() == {
        "tenant_id": tenant_id,
        "status": "provisioning",
        "job_id": job_id,
    }
    assert repository.provisioning[tenant_id] == ProvisioningSummary(
        tenant_id=tenant_id,
        status="provisioning",
        job_id=job_id,
        job_status="pending",
        attempt_count=3,
    )
    assert repository.created_provisioning == 1


@pytest.mark.parametrize(
    ("route_ready", "storage_ready", "policy_ready", "expected"),
    [
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
        (True, True, True, True),
    ],
)
def test_provisioning_activation_uses_persisted_current_attempt_prerequisites(
    route_ready: bool,
    storage_ready: bool,
    policy_ready: bool,
    expected: bool,
) -> None:
    repository = FakeTenantRepository()
    service = TenantProvisioningService(repository)
    repository.set_provisioning(
        "tenant-p",
        "job-p",
        status="provisioning",
        job_status="pending",
        attempt_count=2,
    )
    if route_ready:
        repository.ready_routes.add("tenant-p")
    if storage_ready:
        repository.ready_storage.add("tenant-p")
    if policy_ready:
        repository.verified_policy_attempts.add(("tenant-p", "job-p", 2))

    completed = asyncio.run(service.complete_if_ready("tenant-p", "job-p", 2))

    assert completed is expected
    assert repository.activation_calls == [("tenant-p", "job-p", 2)]
    assert repository.provisioning["tenant-p"].status == ("active" if expected else "provisioning")


def test_attempt_state_machine_rejects_failed_and_stale_callbacks_after_retry() -> None:
    repository = FakeTenantRepository()
    service = TenantProvisioningService(repository)
    created = asyncio.run(
        service.create(
            actor_id="u-root",
            name="Calculus",
            idempotency_key="retry-state",
        )
    )
    repository.set_provisioning(
        created.tenant_id,
        created.job_id,
        status="provisioning",
        job_status="running",
        attempt_count=2,
    )
    repository.ready_routes.add(created.tenant_id)
    repository.ready_storage.add(created.tenant_id)
    repository.verified_policy_attempts.add((created.tenant_id, created.job_id, 2))

    current_failed = asyncio.run(service.mark_failed(created.tenant_id, created.job_id, 2))
    failed_completed = asyncio.run(service.complete_if_ready(created.tenant_id, created.job_id, 2))
    failed_attempt_count = repository.provisioning[created.tenant_id].attempt_count
    retried = asyncio.run(
        service.create(
            actor_id="u-root",
            name=" Calculus ",
            idempotency_key="retry-state",
        )
    )
    stale_failed = asyncio.run(service.mark_failed(created.tenant_id, created.job_id, 2))
    policy_recorded = asyncio.run(
        service.record_policy_verified(
            created.tenant_id,
            created.job_id,
            expected_attempt_count=3,
        )
    )
    old_completed = asyncio.run(
        service.complete_if_ready(
            created.tenant_id,
            created.job_id,
            expected_attempt_count=2,
        )
    )
    current_completed = asyncio.run(
        service.complete_if_ready(
            created.tenant_id,
            created.job_id,
            expected_attempt_count=3,
        )
    )

    assert current_failed is True
    assert failed_completed is False
    assert failed_attempt_count == 2
    assert retried.attempt_count == 3
    assert stale_failed is False
    assert policy_recorded is True
    assert old_completed is False
    assert current_completed is True
    assert repository.provisioning[created.tenant_id].attempt_count == 3


def test_provisioning_status_never_exposes_sensitive_failure_text(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.set_provisioning(
        "tenant-p",
        "job-p",
        status="failed",
        job_status="failed",
        attempt_count=2,
    )
    repository.failure_text = "password=super-secret; provider=s3"
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-root",
        role="admin",
    )

    response = TestClient(app).get("/api/v1/tenants/tenant-p/provisioning")

    assert response.status_code == 200
    assert response.json()["attempt_count"] == 2
    rendered = response.text.lower()
    assert "super-secret" not in rendered
    assert "password" not in rendered
    assert "provider" not in rendered
    assert "reason" not in response.json()


def test_member_and_grant_writes_require_matching_tenant_manage_scope(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_tenant("tenant-b")
    repository.add_member(
        "tenant-a",
        "u-manager",
        roles=frozenset({"org_admin"}),
    )
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-manager",
    )

    with TestClient(app) as client:
        added = client.post(
            "/api/v1/tenants/tenant-a/members",
            headers={"Cookie": "dt_tenant=tenant-a"},
            json={"user_id": "u-student"},
        )
        scoped_added = client.post(
            "/api/v1/tenants/tenant-a/members",
            headers={"Cookie": "dt_tenant=tenant-a"},
            json={
                "user_id": "u-class-teacher",
                "grants": [
                    {
                        "role": "teacher",
                        "scope_type": "class",
                        "scope_id": "class-a",
                    }
                ],
            },
        )
        ambiguous_add = client.post(
            "/api/v1/tenants/tenant-a/members",
            headers={"Cookie": "dt_tenant=tenant-a"},
            json={
                "user_id": "u-ambiguous",
                "role": "teacher",
                "grants": [
                    {
                        "role": "teacher",
                        "scope_type": "class",
                        "scope_id": "class-a",
                    }
                ],
            },
        )
        replaced = client.put(
            "/api/v1/tenants/tenant-a/members/u-student/grants",
            headers={"Cookie": "dt_tenant=tenant-a"},
            json={"roles": ["teacher"]},
        )
        scoped = client.put(
            "/api/v1/tenants/tenant-a/members/u-student/grants",
            headers={"Cookie": "dt_tenant=tenant-a"},
            json={
                "grants": [
                    {
                        "role": "teacher",
                        "scope_type": "class",
                        "scope_id": "class-a",
                    }
                ]
            },
        )
        ambiguous = client.put(
            "/api/v1/tenants/tenant-a/members/u-student/grants",
            headers={"Cookie": "dt_tenant=tenant-a"},
            json={
                "roles": ["teacher"],
                "grants": [
                    {
                        "role": "teacher",
                        "scope_type": "class",
                        "scope_id": "class-a",
                    }
                ],
            },
        )
        repository.scoped_grant_error = GrantResourceNotFoundError(
            "resource is outside the path tenant"
        )
        missing_resource = client.put(
            "/api/v1/tenants/tenant-a/members/u-student/grants",
            headers={"Cookie": "dt_tenant=tenant-a"},
            json={
                "grants": [
                    {
                        "role": "teacher",
                        "scope_type": "course",
                        "scope_id": "shared-course-id",
                    }
                ]
            },
        )
        wrong_path = client.post(
            "/api/v1/tenants/tenant-b/members",
            headers={"Cookie": "dt_tenant=tenant-a"},
            json={"user_id": "u-other"},
        )

    assert added.status_code == 200
    assert added.json()["roles"] == ["student"]
    assert repository.member_calls == []
    assert scoped_added.status_code == 200
    assert scoped_added.json()["grants"] == [
        {
            "role": "teacher",
            "scope_type": "class",
            "scope_id": "class-a",
        }
    ]
    assert repository.scoped_member_calls == [
        (
            "tenant-a",
            "u-student",
            frozenset({("student", "tenant", "tenant-a")}),
        ),
        (
            "tenant-a",
            "u-class-teacher",
            frozenset({("teacher", "class", "class-a")}),
        ),
    ]
    assert ambiguous_add.status_code == 422
    assert replaced.status_code == 200
    assert repository.grant_calls == [("tenant-a", "u-student", frozenset({"teacher"}))]
    assert scoped.status_code == 200
    assert scoped.json()["grants"] == [
        {
            "role": "teacher",
            "scope_type": "class",
            "scope_id": "class-a",
        }
    ]
    assert repository.scoped_grant_calls == [
        (
            "tenant-a",
            "u-student",
            frozenset({("teacher", "class", "class-a")}),
        )
    ]
    assert ambiguous.status_code == 422
    assert missing_resource.status_code == 404
    assert wrong_path.status_code == 403
    assert all(call[0] == "tenant-a" for call in repository.member_calls)


def test_member_write_without_exact_permission_is_denied(monkeypatch) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_member(
        "tenant-a",
        "u-teacher",
        roles=frozenset({"teacher"}),
    )
    app = _authenticated_app(
        monkeypatch,
        repository,
        user_id="u-teacher",
    )

    response = TestClient(app).post(
        "/api/v1/tenants/tenant-a/members",
        headers={"Cookie": "dt_tenant=tenant-a"},
        json={"user_id": "u-student"},
    )

    assert response.status_code == 403
    assert repository.member_calls == []


def test_repository_selection_statements_pin_tenant_user_and_status_filters() -> None:
    member_sql = str(
        build_accessible_tenants_statement(
            "u-alice",
            is_platform_admin=False,
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    access_sql = str(
        build_tenant_access_statement(
            "tenant-a",
            "u-alice",
            is_platform_admin=False,
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    for sql in (member_sql, access_sql):
        assert "tenants.status = 'active'" in sql
        assert "tenant_memberships.status = 'active'" in sql
        assert "tenant_memberships.user_id = 'u-alice'" in sql
    assert "tenants.id = 'tenant-a'" in access_sql
    for fragment in (
        "left outer join platform.role_grants",
        "role_grants.tenant_id = platform.tenant_memberships.tenant_id",
        "role_grants.user_id = platform.tenant_memberships.user_id",
    ):
        assert fragment in access_sql

    admin_access_sql = _compiled_sql(
        build_tenant_access_statement(
            "tenant-a",
            "u-admin",
            is_platform_admin=True,
        )
    )
    for fragment in (
        "left outer join platform.tenant_memberships",
        "tenant_memberships.user_id = 'u-admin'",
        "tenant_memberships.status = 'active'",
        "left outer join platform.role_grants",
    ):
        assert fragment in admin_access_sql


def _compiled_sql(statement: Any) -> str:
    return " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    ).lower()


def _assert_sql(statement: Any, *fragments: str) -> None:
    sql = statement if isinstance(statement, str) else _compiled_sql(statement)
    assert all(fragment in sql for fragment in fragments), sql


def test_write_statements_bind_scope_state_and_conflict_keys() -> None:
    from deeptutor.teaching.repositories.tenants import (
        build_activation_lock_statement,
        build_active_tenant_lock_statement,
        build_failed_job_retry_statement,
        build_failed_tenant_retry_statement,
        build_membership_upsert_statement,
        build_provisioning_advisory_lock_statement,
        build_role_delete_statement,
        build_role_insert_statement,
    )

    role_args = ("tenant-a", "u-student")
    _assert_sql(
        _compiled_sql(build_active_tenant_lock_statement("tenant-a"))
        + _compiled_sql(build_membership_upsert_statement(*role_args)),
        "tenants.id = 'tenant-a'",
        "tenants.status = 'active'",
        "for update",
        "'tenant-a'",
        "'u-student'",
        "on conflict (tenant_id, user_id) do update",
    )
    _assert_sql(
        _compiled_sql(build_role_delete_statement(*role_args))
        + _compiled_sql(
            build_role_insert_statement(
                *role_args,
                frozenset({"student", "teacher"}),
            )
        ),
        "role_grants.tenant_id = 'tenant-a'",
        "role_grants.user_id = 'u-student'",
        "values ('tenant-a', 'u-student'",
    )
    _assert_sql(
        _compiled_sql(build_provisioning_advisory_lock_statement("tenant-a"))
        + _compiled_sql(build_failed_tenant_retry_statement("tenant-a"))
        + _compiled_sql(build_failed_job_retry_statement("tenant-a", "job-a", 2)),
        "pg_advisory_xact_lock",
        "tenants.id = 'tenant-a'",
        "tenant_provisioning_jobs.id = 'job-a'",
        "tenant_provisioning_jobs.tenant_id = 'tenant-a'",
        "tenant_provisioning_jobs.operation = 'provision'",
        "tenant_provisioning_jobs.status = 'failed'",
        "tenant_provisioning_jobs.attempt_count = 2",
        "attempt_count + 1",
    )
    worker_builder = getattr(
        tenant_repositories,
        "build_worker_attempt_lock_statement",
        None,
    )
    assert worker_builder is not None
    _assert_sql(
        worker_builder("tenant-a", "job-a", 2),
        "tenants.id = 'tenant-a'",
        "tenant_provisioning_jobs.id = 'job-a'",
        "tenant_provisioning_jobs.tenant_id = 'tenant-a'",
        "tenant_provisioning_jobs.operation = 'provision'",
        "tenant_provisioning_jobs.attempt_count = 2",
        "tenant_provisioning_jobs.status in ('pending', 'running')",
        "tenants.status = 'provisioning'",
        "for update",
    )
    _assert_sql(
        build_activation_lock_statement("tenant-a", "job-a", 2),
        "data_plane_routes.tenant_id = 'tenant-a'",
        "data_plane_routes.status = 'active'",
        f"data_plane_routes.schema_name = '{tenant_schema_name('tenant-a')}'",
        "tenant_storage_credentials.tenant_id = 'tenant-a'",
        "tenant_storage_credentials.status = 'active'",
        "audit_log.action = 'tenant.provisioning.policy_verified'",
        "audit_log.resource_type = 'provisioning_job'",
        "audit_log.resource_id = 'job-a:2'",
        "for update",
    )


class _Result:
    def __init__(self, value: Any = None, rowcount: int = 1) -> None:
        self.value = value
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> Any:
        return self.value

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        if self.value is None:
            return []
        return list(self.value)


class _RecordingSession:
    def __init__(
        self,
        execute_results: tuple[_Result, ...] = (),
        scalar_results: tuple[Any, ...] = (),
    ) -> None:
        self.execute_results = list(execute_results)
        self.scalar_results = list(scalar_results)
        self.trace: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def begin(self) -> Any:
        self.trace.append(("begin", None))
        try:
            yield
        except Exception:
            self.trace.append(("rollback", None))
            raise
        self.trace.append(("commit", None))

    async def execute(self, statement: Any) -> _Result:
        self.trace.append(("execute", statement))
        return self.execute_results.pop(0) if self.execute_results else _Result()

    async def scalar(self, statement: Any) -> Any:
        self.trace.append(("scalar", statement))
        if not self.scalar_results:
            raise AssertionError("unexpected scalar call")
        return self.scalar_results.pop(0)

    def add(self, instance: Any) -> None:
        self.trace.append(("add", instance))

    async def flush(self) -> None:
        self.trace.append(("flush", None))


class _AccessRevokedBetweenQueriesSession:
    def __init__(self) -> None:
        self.trace: list[tuple[str, Any]] = []

    async def execute(self, statement: Any) -> _Result:
        self.trace.append(("execute", statement))
        if "role_grants" in _compiled_sql(statement):
            return _Result(())
        return _Result(
            {
                "tenant_id": "tenant-a",
                "name": "Tenant A",
                "status": "active",
                "schema_name": tenant_schema_name("tenant-a"),
            }
        )

    async def scalar(self, statement: Any) -> None:
        self.trace.append(("scalar", statement))
        return None


def _install_recording_session(monkeypatch: Any, session: _RecordingSession) -> None:
    @asynccontextmanager
    async def recording_platform_session() -> Any:
        yield session

    monkeypatch.setattr(
        tenant_repositories,
        "platform_session",
        recording_platform_session,
    )


def _install_recording_tenant_session(
    monkeypatch: Any,
    session: _RecordingSession,
    tenant_calls: list[str],
) -> None:
    @asynccontextmanager
    async def recording_tenant_session(tenant_id: str) -> Any:
        tenant_calls.append(tenant_id)
        yield session

    monkeypatch.setattr(
        tenant_repositories,
        "tenant_session",
        recording_tenant_session,
    )


def test_access_revoked_between_selection_and_grant_load_fails_closed(
    monkeypatch,
) -> None:
    session = _AccessRevokedBetweenQueriesSession()
    _install_recording_session(monkeypatch, session)

    with pytest.raises(TenantAccessDeniedError):
        asyncio.run(
            tenant_repositories.TenantRepository().get_tenant_access(
                "tenant-a",
                "u-revoked",
                is_platform_admin=False,
            )
        )


def test_platform_admin_access_without_membership_remains_available(
    monkeypatch,
) -> None:
    session = _RecordingSession(
        execute_results=(
            _Result(
                (
                    {
                        "tenant_id": "tenant-a",
                        "name": "Tenant A",
                        "status": "active",
                        "schema_name": tenant_schema_name("tenant-a"),
                        "grant_role": None,
                        "grant_scope_type": None,
                        "grant_scope_id": None,
                    },
                )
            ),
        )
    )
    _install_recording_session(monkeypatch, session)

    access = asyncio.run(
        tenant_repositories.TenantRepository().get_tenant_access(
            "tenant-a",
            "u-platform-admin",
            is_platform_admin=True,
        )
    )

    assert access.summary.tenant_id == "tenant-a"
    assert access.grants == frozenset()
    assert _trace_names(session) == ("execute",)


def test_scoped_grant_replacement_validates_resources_before_atomic_replace(
    monkeypatch,
) -> None:
    from deeptutor.teaching.permissions import RoleGrant

    session = _RecordingSession(
        execute_results=(
            _Result(("course-a",)),
            _Result((("class-a", "course-a"),)),
            _Result(),
            _Result(),
        ),
        scalar_results=("u-teacher",),
    )
    tenant_calls: list[str] = []
    _install_recording_tenant_session(monkeypatch, session, tenant_calls)
    grants = frozenset(
        {
            RoleGrant("teacher", "course", "course-a"),
            RoleGrant("teacher", "class", "class-a"),
        }
    )

    asyncio.run(
        tenant_repositories.TenantRepository().replace_scoped_grants(
            "tenant-a",
            "u-teacher",
            grants,
        )
    )

    assert tenant_calls == ["tenant-a"]
    assert _trace_names(session)[-2:] == ("flush", "commit")
    course_sql = _compiled_sql(session.trace[2][1])
    class_sql = _compiled_sql(session.trace[3][1])
    delete_sql = _compiled_sql(session.trace[4][1])
    assert "courses.id in ('course-a')" in course_sql
    assert "courses.status = 'active'" in course_sql
    for fragment in (
        "classes.id in ('class-a')",
        "classes.course_id",
        "join tenant.courses",
        "classes.status = 'active'",
        "courses.status = 'active'",
    ):
        assert fragment in class_sql
    assert "delete from platform.role_grants" in delete_sql


def test_scoped_member_upsert_validates_resources_before_membership_activation(
    monkeypatch,
) -> None:
    from deeptutor.teaching.permissions import RoleGrant

    session = _RecordingSession(
        execute_results=(
            _Result(("course-a",)),
            _Result(),
            _Result(),
            _Result(),
        ),
        scalar_results=("tenant-a",),
    )
    tenant_calls: list[str] = []
    _install_recording_tenant_session(monkeypatch, session, tenant_calls)

    asyncio.run(
        tenant_repositories.TenantRepository().upsert_member_with_scoped_grants(
            "tenant-a",
            "u-teacher",
            frozenset({RoleGrant("teacher", "course", "course-a")}),
        )
    )

    assert tenant_calls == ["tenant-a"]
    assert _trace_names(session) == (
        "begin",
        "scalar",
        "execute",
        "execute",
        "execute",
        "execute",
        "flush",
        "commit",
    )
    assert "courses.id in ('course-a')" in _compiled_sql(session.trace[2][1])
    assert "insert into platform.tenant_memberships" in _compiled_sql(session.trace[3][1])


def test_invalid_scoped_member_resource_does_not_activate_membership(
    monkeypatch,
) -> None:
    from deeptutor.teaching.permissions import RoleGrant

    session = _RecordingSession(
        execute_results=(_Result(()),),
        scalar_results=("tenant-a",),
    )
    tenant_calls: list[str] = []
    _install_recording_tenant_session(monkeypatch, session, tenant_calls)
    operation = tenant_repositories.TenantRepository().upsert_member_with_scoped_grants(
        "tenant-a",
        "u-teacher",
        frozenset({RoleGrant("teacher", "course", "missing-course")}),
    )

    with pytest.raises(GrantResourceNotFoundError):
        asyncio.run(operation)

    assert tenant_calls == ["tenant-a"]
    assert _trace_names(session) == ("begin", "scalar", "execute", "rollback")
    assert "tenant_memberships" not in _compiled_sql(session.trace[2][1])


def test_missing_scoped_resource_rolls_back_before_deleting_existing_grants(
    monkeypatch,
) -> None:
    from deeptutor.teaching.permissions import RoleGrant
    from deeptutor.teaching.repositories.tenants import (
        GrantResourceNotFoundError,
    )

    session = _RecordingSession(
        execute_results=(_Result(()),),
        scalar_results=("u-teacher",),
    )
    tenant_calls: list[str] = []
    _install_recording_tenant_session(monkeypatch, session, tenant_calls)
    operation = tenant_repositories.TenantRepository().replace_scoped_grants(
        "tenant-a",
        "u-teacher",
        frozenset({RoleGrant("teacher", "course", "shared-course-id")}),
    )

    with pytest.raises(GrantResourceNotFoundError):
        asyncio.run(operation)

    assert tenant_calls == ["tenant-a"]
    assert _trace_names(session)[-1] == "rollback"
    executed_sql = [
        _compiled_sql(statement) for name, statement in session.trace if name == "execute"
    ]
    assert len(executed_sql) == 1
    assert all("delete from platform.role_grants" not in sql for sql in executed_sql)


def test_tenant_scope_mismatch_is_rejected_before_opening_a_transaction() -> None:
    from deeptutor.teaching.permissions import RoleGrant
    from deeptutor.teaching.repositories.tenants import InvalidGrantScopeError

    with pytest.raises(InvalidGrantScopeError):
        asyncio.run(
            tenant_repositories.TenantRepository().replace_scoped_grants(
                "tenant-a",
                "u-teacher",
                frozenset({RoleGrant("teacher", "tenant", "tenant-b")}),
            )
        )


def _trace_names(session: _RecordingSession) -> tuple[str, ...]:
    return tuple(name for name, _value in session.trace)


def _failed_retry_session(
    tenant_rowcount: int,
    job_rowcount: int,
) -> _RecordingSession:
    existing = {
        "tenant_id": "tenant-a",
        "name": "Tenant A",
        "status": "failed",
        "job_id": "job-a",
        "job_status": "failed",
        "attempt_count": 2,
    }
    return _RecordingSession(
        execute_results=(
            _Result(),
            _Result(existing),
            _Result(rowcount=tenant_rowcount),
            _Result(rowcount=job_rowcount),
        )
    )


@pytest.mark.parametrize(
    ("case", "expected_trace"),
    [
        (
            "membership",
            ("begin", "scalar", "execute", "execute", "execute", "flush", "commit"),
        ),
        (
            "create",
            ("begin", "execute", "execute", "scalar", "add", "add", "flush", "commit"),
        ),
        (
            "retry",
            ("begin", "execute", "execute", "execute", "execute", "flush", "commit"),
        ),
    ],
)
def test_write_transactions_order_lock_mutations_flush_and_commit(
    monkeypatch,
    case: str,
    expected_trace: tuple[str, ...],
) -> None:
    repository = tenant_repositories.TenantRepository()
    tenant_calls: list[str] = []
    if case == "membership":
        session = _RecordingSession(
            execute_results=(_Result(), _Result(), _Result()),
            scalar_results=("tenant-a",),
        )
        operation = repository.upsert_member("tenant-a", "u-student", frozenset({"student"}))
    elif case == "create":
        session = _RecordingSession(
            execute_results=(_Result(), _Result()),
            scalar_results=(None,),
        )
        operation = repository.create_provisioning(
            tenant_id="tenant-a", job_id="job-a", name="Tenant A"
        )
    else:
        session = _failed_retry_session(1, 1)
        operation = repository.create_provisioning(
            tenant_id="tenant-a", job_id="job-a", name="Tenant A"
        )
    if case == "membership":
        _install_recording_tenant_session(monkeypatch, session, tenant_calls)
    else:
        _install_recording_session(monkeypatch, session)

    result = asyncio.run(operation)

    assert _trace_names(session) == expected_trace
    if case == "membership":
        assert tenant_calls == ["tenant-a"]
    if case == "retry":
        assert result.attempt_count == 3


@pytest.mark.parametrize(("tenant_rowcount", "job_rowcount"), [(0, 1), (1, 0)])
def test_failed_retry_rowcount_mismatch_rolls_back(
    monkeypatch,
    tenant_rowcount: int,
    job_rowcount: int,
) -> None:
    session = _failed_retry_session(tenant_rowcount, job_rowcount)
    _install_recording_session(monkeypatch, session)
    operation = tenant_repositories.TenantRepository().create_provisioning(
        tenant_id="tenant-a", job_id="job-a", name="Tenant A"
    )

    with pytest.raises(tenant_repositories.TenantConflictError, match="retry"):
        asyncio.run(operation)

    assert _trace_names(session)[-1] == "rollback"


def test_existing_idempotency_payload_conflict_rolls_back_before_mutation(monkeypatch) -> None:
    session = _failed_retry_session(1, 1)
    _install_recording_session(monkeypatch, session)
    operation = tenant_repositories.TenantRepository().create_provisioning(
        tenant_id="tenant-a",
        job_id="job-a",
        name="Different Tenant",
    )

    with pytest.raises(TenantConflictError, match="idempotency"):
        asyncio.run(operation)

    assert _trace_names(session) == ("begin", "execute", "execute", "rollback")


@pytest.mark.parametrize(
    ("method", "start", "expected"),
    [
        (
            "mark_provisioning_failed",
            ("provisioning", "running"),
            ("failed", "failed"),
        ),
        (
            "activate_if_ready",
            ("provisioning", "running"),
            ("active", "completed"),
        ),
    ],
)
def test_provisioning_transitions_bind_attempt_lock_and_flush_both_rows(
    monkeypatch,
    method: str,
    start: tuple[str, str],
    expected: tuple[str, str],
) -> None:
    tenant = SimpleNamespace(status=start[0])
    job = SimpleNamespace(status=start[1], attempt_count=2)
    session = _RecordingSession(execute_results=(_Result((tenant, job)),))
    _install_recording_session(monkeypatch, session)

    transitioned = asyncio.run(
        getattr(tenant_repositories.TenantRepository(), method)("tenant-a", "job-a", 2)
    )

    assert transitioned is True
    assert (tenant.status, job.status, job.attempt_count) == (*expected, 2)
    assert _trace_names(session) == (
        "begin",
        "execute",
        "flush",
        "commit",
    )
    statement = _compiled_sql(session.trace[1][1])
    for fragment in (
        "tenants.id = 'tenant-a'",
        "tenant_provisioning_jobs.tenant_id = 'tenant-a'",
        "tenant_provisioning_jobs.id = 'job-a'",
        "tenant_provisioning_jobs.operation = 'provision'",
        "tenant_provisioning_jobs.attempt_count = 2",
        "tenant_provisioning_jobs.status in ('pending', 'running')",
        "tenants.status = 'provisioning'",
        "for update",
    ):
        assert fragment in statement


@pytest.mark.parametrize(
    "method",
    ["activate_if_ready", "mark_provisioning_failed", "record_policy_verified"],
)
def test_stale_or_unready_worker_callback_returns_false_without_mutation(
    monkeypatch,
    method: str,
) -> None:
    session = _RecordingSession(execute_results=(_Result(None),))
    _install_recording_session(monkeypatch, session)

    transitioned = asyncio.run(
        getattr(tenant_repositories.TenantRepository(), method)("tenant-a", "job-a", 1)
    )

    assert transitioned is False
    assert _trace_names(session) == ("begin", "execute", "commit")


def test_record_policy_verified_binds_current_attempt_and_persists_safe_event(
    monkeypatch,
) -> None:
    tenant = SimpleNamespace(status="provisioning")
    job = SimpleNamespace(status="running", attempt_count=2)
    session = _RecordingSession(execute_results=(_Result((tenant, job)),))
    _install_recording_session(monkeypatch, session)

    recorded = asyncio.run(
        tenant_repositories.TenantRepository().record_policy_verified(
            "tenant-a",
            "job-a",
            2,
        )
    )

    assert recorded is True
    assert _trace_names(session) == ("begin", "execute", "add", "flush", "commit")
    audit = session.trace[2][1]
    assert (
        audit.tenant_id,
        audit.actor_id,
        audit.action,
        audit.resource_type,
        audit.resource_id,
    ) == (
        "tenant-a",
        None,
        "tenant.provisioning.policy_verified",
        "provisioning_job",
        "job-a:2",
    )
    _assert_sql(
        session.trace[1][1],
        "tenants.id = 'tenant-a'",
        "tenant_provisioning_jobs.id = 'job-a'",
        "tenant_provisioning_jobs.operation = 'provision'",
        "tenant_provisioning_jobs.attempt_count = 2",
        "tenant_provisioning_jobs.status in ('pending', 'running')",
        "for update",
    )


def test_auth_status_returns_only_tenant_summaries_and_valid_active_id(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a", name="Tenant A")
    repository.add_tenant("tenant-p", status="provisioning")
    repository.add_member("tenant-a", "u-alice")
    repository.add_member("tenant-p", "u-alice")
    _set_platform_enabled(monkeypatch, True)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        auth_router,
        "decode_token",
        lambda _token: TokenPayload(
            username="alice",
            role="admin",
            user_id="u-alice",
        ),
    )
    monkeypatch.setattr(
        auth_router,
        "get_user_info",
        lambda _username: _user_record("u-alice", "user", username="alice"),
    )
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1/auth")

    response = TestClient(app).get(
        "/api/v1/auth/status",
        headers={
            "Authorization": "Bearer valid",
            "Cookie": "dt_tenant=tenant-a",
        },
    )

    assert response.status_code == 200
    assert response.json()["role"] == "user"
    assert response.json()["is_admin"] is False
    assert response.json()["active_tenant_id"] == "tenant-a"
    assert response.json()["tenants"] == [
        {
            "tenant_id": "tenant-a",
            "name": "Tenant A",
            "status": "active",
        }
    ]
    rendered = response.text.lower()
    for forbidden in ("permissions", "schema_name", "secret", "provider"):
        assert forbidden not in rendered
    assert repository.list_admin_flags == [False]


def test_auth_status_omits_inactive_membership_and_active_cookie(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a", name="Tenant A")
    repository.add_member("tenant-a", "u-alice")
    repository.memberships[("tenant-a", "u-alice")] = "inactive"
    app = _authenticated_app(monkeypatch, repository)
    monkeypatch.setattr(
        auth_router,
        "decode_token",
        lambda _token: TokenPayload(
            username="u-alice",
            role="user",
            user_id="u-alice",
        ),
    )
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )

    response = TestClient(app).get(
        "/api/v1/auth/status",
        headers={
            "Authorization": "Bearer valid",
            "Cookie": "dt_tenant=tenant-a",
        },
    )

    assert response.status_code == 200
    assert response.json()["active_tenant_id"] is None
    assert response.json()["tenants"] == []
    assert repository.list_admin_flags == [False]


def test_validate_pb_token_cache_is_scoped_to_normalized_provider_url(
    monkeypatch,
) -> None:
    provider_state = {"url": "https://pocketbase-a.test/"}
    auth_refresh_calls: list[str] = []

    class FakeAuthStore:
        def save(self, token: str, _record: Any) -> None:
            self.token = token

    class FakeUsers:
        def __init__(self, provider_url: str) -> None:
            self.provider_url = provider_url

        def auth_refresh(self) -> Any:
            auth_refresh_calls.append(self.provider_url)
            if self.provider_url == "https://pocketbase-a.test":
                return SimpleNamespace(
                    token="refreshed-token",
                    record=SimpleNamespace(
                        id="pb-admin",
                        email="admin@example.com",
                        role="admin",
                    ),
                )
            raise RuntimeError("token rejected by provider B")

    class FakePocketBase:
        def __init__(self, provider_url: str) -> None:
            self.provider_url = provider_url
            self.auth_store = FakeAuthStore()

        def collection(self, name: str) -> FakeUsers:
            assert name == "users"
            return FakeUsers(self.provider_url)

    token_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
    monkeypatch.setitem(
        sys.modules,
        "pocketbase",
        SimpleNamespace(PocketBase=FakePocketBase),
    )
    monkeypatch.setattr(
        pocketbase_client,
        "load_integrations_settings",
        lambda: {
            "pocketbase_url": provider_state["url"],
            "pocketbase_admin_email": "",
            "pocketbase_admin_password": "",
        },
    )
    monkeypatch.setattr(pocketbase_client, "_TOKEN_CACHE", token_cache)

    try:
        first_payload = pocketbase_client.validate_pb_token("shared-token")
        cached_payload = pocketbase_client.validate_pb_token("shared-token")
        provider_state["url"] = "https://pocketbase-b.test/"
        rejected_payload = pocketbase_client.validate_pb_token("shared-token")

        assert first_payload == {
            "id": "pb-admin",
            "username": "admin@example.com",
            "role": "admin",
        }
        assert cached_payload == first_payload
        assert rejected_payload is None
        assert auth_refresh_calls == [
            "https://pocketbase-a.test",
            "https://pocketbase-b.test",
        ]
    finally:
        token_cache.clear()


def test_pocketbase_auth_refresh_chain_preserves_identity_and_tenant_membership(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a", name="Tenant A")
    repository.add_member("tenant-a", "pb-user")
    app, token_cache = _pocketbase_chain_app(
        monkeypatch,
        repository,
        SimpleNamespace(
            id="pb-user",
            email="pb@example.com",
            role="user",
        ),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        status_response = client.get(
            "/api/v1/auth/status",
            headers={
                "Authorization": "Bearer pb-token",
                "Cookie": "dt_tenant=tenant-a",
            },
        )
        context_response = client.get(
            "/context",
            headers={
                "Authorization": "Bearer pb-token",
                "Cookie": "dt_tenant=tenant-a",
            },
        )

    assert status_response.status_code == 200
    assert status_response.json()["authenticated"] is True
    assert status_response.json()["user_id"] == "pb-user"
    assert status_response.json()["role"] == "user"
    assert status_response.json()["avatar"] == ""
    assert status_response.json()["active_tenant_id"] == "tenant-a"
    assert status_response.json()["tenants"] == [
        {"tenant_id": "tenant-a", "name": "Tenant A", "status": "active"}
    ]
    assert context_response.status_code == 200
    assert context_response.json() == {
        "tenant_id": "tenant-a",
        "user_id": "pb-user",
    }
    assert token_cache[("http://pocketbase.test", "pb-token")][0]["id"] == "pb-user"
    assert repository.list_admin_flags == [False]
    assert repository.access_calls == [("tenant-a", "pb-user", False)]


@pytest.mark.parametrize(
    "record",
    [
        SimpleNamespace(id="", email="pb@example.com", role="user"),
        SimpleNamespace(id="pb-user", email="pb@example.com", role="owner"),
    ],
    ids=["missing-id", "unknown-role"],
)
def test_pocketbase_auth_refresh_chain_rejects_invalid_identity(
    monkeypatch,
    record: Any,
) -> None:
    repository = FakeTenantRepository()
    app, _token_cache = _pocketbase_chain_app(monkeypatch, repository, record)

    with TestClient(app, raise_server_exceptions=False) as client:
        status_response = client.get(
            "/api/v1/auth/status",
            headers={"Authorization": "Bearer invalid-pb-token"},
        )
        context_response = client.get(
            "/context",
            headers={
                "Authorization": "Bearer invalid-pb-token",
                "Cookie": "dt_tenant=tenant-a",
            },
        )

    assert status_response.status_code == 200
    assert status_response.json()["authenticated"] is False
    assert status_response.json()["tenants"] == []
    assert context_response.status_code == 401
    assert repository.list_calls == 0
    assert repository.access_calls == []


def test_auth_status_drops_stale_tenant_cookie_and_anonymous_has_no_tenants(
    monkeypatch,
) -> None:
    repository = FakeTenantRepository()
    repository.add_tenant("tenant-a")
    repository.add_member("tenant-a", "u-alice")
    _set_platform_enabled(monkeypatch, True)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1/auth")

    monkeypatch.setattr(
        auth_router,
        "decode_token",
        lambda token: (
            TokenPayload(username="alice", role="user", user_id="u-alice") if token else None
        ),
    )
    monkeypatch.setattr(
        auth_router,
        "get_user_info",
        lambda _username: _user_record("u-alice", "user", username="alice"),
    )
    with TestClient(app) as client:
        stale = client.get(
            "/api/v1/auth/status",
            headers={
                "Authorization": "Bearer valid",
                "Cookie": "dt_tenant=tenant-stale",
            },
        )
        anonymous = client.get("/api/v1/auth/status")

    assert stale.json()["active_tenant_id"] is None
    assert anonymous.json()["authenticated"] is False
    assert anonymous.json()["tenants"] == []
    assert anonymous.json()["active_tenant_id"] is None


def test_auth_status_platform_disabled_returns_local_without_repository(
    monkeypatch,
) -> None:
    _set_platform_enabled(monkeypatch, False)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)

    def fail_if_repository_loaded() -> None:
        raise AssertionError("platform-disabled auth status must not touch DB")

    monkeypatch.setattr(
        auth_router,
        "get_user_info",
        lambda _username: (_ for _ in ()).throw(
            AssertionError("platform-disabled auth status must not touch user store")
        ),
    )
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        fail_if_repository_loaded,
    )
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1/auth")

    response = TestClient(app).get("/api/v1/auth/status")

    assert response.status_code == 200
    assert response.json()["active_tenant_id"] == "local"
    assert response.json()["tenants"] == [
        {
            "tenant_id": "local",
            "name": "Local",
            "status": "active",
        }
    ]
