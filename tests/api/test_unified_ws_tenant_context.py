"""Trusted tenant binding for the unified WebSocket entry point."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

from fastapi import WebSocketDisconnect
import pytest

from deeptutor.api.routers import auth as auth_router
from deeptutor.api.routers import unified_ws as unified_ws_router
from deeptutor.multi_user.context import (
    get_current_tenant_or_none,
    get_current_user_or_none,
    reset_current_user,
)
from deeptutor.services import auth as auth_service
from deeptutor.services import pocketbase_client
from deeptutor.services.auth import TokenPayload
from deeptutor.teaching import tenant_context as tenant_context_module
from deeptutor.teaching.repositories import tenants as tenant_repositories
from deeptutor.teaching.repositories.tenants import (
    TenantAccess,
    TenantAccessDeniedError,
    TenantSummary,
)


class _TenantRepository:
    def __init__(self) -> None:
        self.access_calls: list[tuple[str, str, bool]] = []

    async def list_tenants(
        self,
        user_id: str,
        *,
        is_platform_admin: bool,
    ) -> tuple[TenantSummary, ...]:
        raise AssertionError("the WebSocket must require an explicit tenant cookie")

    async def get_tenant_access(
        self,
        tenant_id: str,
        user_id: str,
        *,
        is_platform_admin: bool,
    ) -> TenantAccess:
        self.access_calls.append((tenant_id, user_id, is_platform_admin))
        return TenantAccess(
            summary=TenantSummary(tenant_id=tenant_id, name=tenant_id, status="active"),
            schema_name=f"tenant_{tenant_id.replace('-', '_')}",
            roles=frozenset({"student"}),
        )


class _AdminOnlyTenantRepository(_TenantRepository):
    async def get_tenant_access(
        self,
        tenant_id: str,
        user_id: str,
        *,
        is_platform_admin: bool,
    ) -> TenantAccess:
        self.access_calls.append((tenant_id, user_id, is_platform_admin))
        if not is_platform_admin:
            raise TenantAccessDeniedError(tenant_id)
        return TenantAccess(
            summary=TenantSummary(tenant_id=tenant_id, name=tenant_id, status="active"),
            schema_name=f"tenant_{tenant_id.replace('-', '_')}",
        )


class _FakeWebSocket:
    def __init__(
        self,
        *,
        tenant_cookie: str | None,
        inspect_tenant: bool = False,
    ) -> None:
        self.query_params = {"token": "signed-token"}
        self.cookies = {}
        if tenant_cookie is not None:
            self.cookies["dt_tenant"] = tenant_cookie
        self.inspect_tenant = inspect_tenant
        self.accepted = False
        self.close_codes: list[int] = []
        self.sent: list[dict[str, Any]] = []
        self.seen_tenants: list[object | None] = []
        self.receive_calls = 0

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int) -> None:
        self.close_codes.append(code)

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def receive_text(self) -> str:
        self.receive_calls += 1
        if not self.inspect_tenant:
            raise WebSocketDisconnect()
        self.seen_tenants.append(get_current_tenant_or_none())
        if self.receive_calls == 1:
            # A browser cookie can change while this long-lived socket is open.
            # The selected tenant must remain the one verified at the handshake.
            self.cookies["dt_tenant"] = "tenant-b"
            return '{"type":"ping"}'
        raise WebSocketDisconnect()


def _enable_platform_auth(monkeypatch: pytest.MonkeyPatch, repository: object) -> None:
    settings = SimpleNamespace(enabled=True)
    payload = TokenPayload(username="alice", role="user", user_id="u-alice")
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_router, "load_platform_settings", lambda: settings)
    monkeypatch.setattr(tenant_context_module, "load_platform_settings", lambda: settings)
    monkeypatch.setattr(auth_router, "decode_token", lambda _token: payload)
    monkeypatch.setattr(
        auth_router,
        "get_user_info",
        lambda _username: {
            "id": "u-alice",
            "username": "alice",
            "role": "user",
            "disabled": False,
        },
    )
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )


def _enable_pocketbase_refresh(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, Any],
) -> None:
    class FakeAuthStore:
        def save(self, token: str, _record: object) -> None:
            self.token = token

    class FakeUsers:
        def auth_refresh(self) -> object:
            state["calls"] += 1
            if state.get("error"):
                raise RuntimeError("PocketBase unavailable")
            return SimpleNamespace(
                record=SimpleNamespace(
                    id="pb-user",
                    email="pb@example.com",
                    role=state["role"],
                )
            )

    class FakePocketBase:
        def __init__(self, _url: str) -> None:
            self.auth_store = FakeAuthStore()

        def collection(self, name: str) -> FakeUsers:
            assert name == "users"
            return FakeUsers()

    settings = SimpleNamespace(enabled=True)
    monkeypatch.setitem(
        sys.modules,
        "pocketbase",
        SimpleNamespace(PocketBase=FakePocketBase),
    )
    monkeypatch.setattr(
        pocketbase_client,
        "load_integrations_settings",
        lambda: {
            "pocketbase_url": "https://pocketbase.test",
            "pocketbase_admin_email": "",
            "pocketbase_admin_password": "",
        },
    )
    monkeypatch.setattr(pocketbase_client, "_TOKEN_CACHE", {})
    monkeypatch.setattr(auth_service, "POCKETBASE_ENABLED", True)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_router, "POCKETBASE_ENABLED", True)
    monkeypatch.setattr(auth_router, "load_platform_settings", lambda: settings)
    monkeypatch.setattr(tenant_context_module, "load_platform_settings", lambda: settings)


@pytest.mark.asyncio
async def test_ws_auth_uses_authoritative_platform_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(enabled=True)
    stale_admin = TokenPayload(username="alice", role="admin", user_id="u-alice")
    ws = _FakeWebSocket(tenant_cookie="tenant-a")
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_router, "load_platform_settings", lambda: settings)
    monkeypatch.setattr(auth_router, "decode_token", lambda _token: stale_admin)
    monkeypatch.setattr(
        auth_router,
        "get_user_info",
        lambda _username: {
            "id": "u-alice",
            "username": "alice",
            "role": "user",
            "disabled": False,
        },
    )

    token = await auth_router.ws_require_auth(ws)  # type: ignore[arg-type]
    assert token is not auth_router.ws_auth_failed
    try:
        current_user = get_current_user_or_none()
        assert current_user is not None
        assert current_user.role == "user"
    finally:
        reset_current_user(token)


@pytest.mark.asyncio
async def test_pocketbase_ws_handshake_refreshes_stale_cached_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {"role": "admin", "calls": 0, "error": False}
    repository = _AdminOnlyTenantRepository()
    _enable_pocketbase_refresh(monkeypatch, state)
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )

    cached_admin = auth_service.decode_token("signed-token")
    state["role"] = "user"
    still_cached_admin = auth_service.decode_token("signed-token")
    assert cached_admin is not None and cached_admin.role == "admin"
    assert still_cached_admin is not None and still_cached_admin.role == "admin"
    assert state["calls"] == 1

    ws = _FakeWebSocket(tenant_cookie="tenant-without-membership")
    await unified_ws_router.unified_websocket(ws)  # type: ignore[arg-type]

    assert state["calls"] == 2
    assert repository.access_calls == [
        ("tenant-without-membership", "pb-user", False)
    ]
    assert ws.accepted is False
    assert ws.close_codes == [4003]
    assert get_current_user_or_none() is None


@pytest.mark.asyncio
async def test_pocketbase_ws_fresh_validation_network_failure_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {"role": "admin", "calls": 0, "error": False}
    _enable_pocketbase_refresh(monkeypatch, state)
    assert auth_service.decode_token("signed-token") is not None
    state["error"] = True
    ws = _FakeWebSocket(tenant_cookie="tenant-a")

    result = await auth_router.ws_require_auth(ws)  # type: ignore[arg-type]
    try:
        assert result is auth_router.ws_auth_failed
        assert state["calls"] == 2
        assert ws.close_codes == [4001]
        assert get_current_user_or_none() is None
    finally:
        if result is not auth_router.ws_auth_failed:
            reset_current_user(result)


@pytest.mark.asyncio
async def test_ws_auth_does_not_fall_back_to_local_admin_on_tenant_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(enabled=True)
    ws = _FakeWebSocket(tenant_cookie="tenant-a")
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    monkeypatch.setattr(auth_router, "load_platform_settings", lambda: settings)

    result = await auth_router.ws_require_auth(ws)  # type: ignore[arg-type]

    assert result is auth_router.ws_auth_failed
    assert ws.close_codes == [4001]
    assert get_current_user_or_none() is None


@pytest.mark.asyncio
async def test_unified_websocket_pins_handshake_tenant_and_resets_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _TenantRepository()
    _enable_platform_auth(monkeypatch, repository)
    ws = _FakeWebSocket(tenant_cookie="tenant-a", inspect_tenant=True)

    await unified_ws_router.unified_websocket(ws)  # type: ignore[arg-type]

    assert ws.accepted is True
    assert [getattr(context, "tenant_id", None) for context in ws.seen_tenants] == [
        "tenant-a",
        "tenant-a",
    ]
    assert repository.access_calls == [("tenant-a", "u-alice", False)]
    assert ws.sent == [{"type": "pong"}]
    assert get_current_tenant_or_none() is None
    assert get_current_user_or_none() is None


@pytest.mark.asyncio
async def test_unified_websocket_rejects_missing_tenant_cookie_before_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _TenantRepository()
    _enable_platform_auth(monkeypatch, repository)
    ws = _FakeWebSocket(tenant_cookie=None)

    await unified_ws_router.unified_websocket(ws)  # type: ignore[arg-type]

    assert ws.accepted is False
    assert ws.close_codes == [4003]
    assert ws.receive_calls == 0
    assert repository.access_calls == []
    assert get_current_tenant_or_none() is None
    assert get_current_user_or_none() is None


@pytest.mark.asyncio
async def test_unified_websocket_preserves_local_mode_without_repository_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(enabled=False)
    repository = _TenantRepository()
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    monkeypatch.setattr(auth_router, "load_platform_settings", lambda: settings)
    monkeypatch.setattr(tenant_context_module, "load_platform_settings", lambda: settings)
    monkeypatch.setattr(
        tenant_repositories,
        "get_tenant_repository",
        lambda: repository,
    )
    ws = _FakeWebSocket(tenant_cookie=None, inspect_tenant=True)

    await unified_ws_router.unified_websocket(ws)  # type: ignore[arg-type]

    assert ws.accepted is True
    assert [getattr(context, "tenant_id", None) for context in ws.seen_tenants] == [
        "local",
        "local",
    ]
    assert repository.access_calls == []
    assert ws.sent == [{"type": "pong"}]
    assert get_current_tenant_or_none() is None
    assert get_current_user_or_none() is None
