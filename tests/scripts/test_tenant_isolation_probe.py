from __future__ import annotations

import asyncio
import hashlib
from http.cookies import SimpleCookie
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts" / "tenant_isolation_probe.py"
    spec = importlib.util.spec_from_file_location("tenant_isolation_probe_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _contract_module():
    path = ROOT / "scripts" / "tenant_isolation_contract.py"
    spec = importlib.util.spec_from_file_location("tenant_isolation_contract_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _identity(module, *, username: str, user_id: str, token: str):
    return module.IdentityCredential(username, user_id, module.SecretStr(token))


def _targets() -> dict[str, object]:
    return {
        "database": {"courseId": "course-tenant-owner"},
        "objects": {
            "bindingId": "binding-tenant-owner",
            "classroomVersionId": "version-tenant-owner",
        },
        "exports": {"exportId": "export-tenant-owner"},
        "events": {
            "sessionId": "session-tenant-owner",
            "eventId": "event-tenant-owner",
            "classroomVersionId": "version-tenant-owner",
        },
    }


def _tenant_cookie(request: httpx.Request) -> str | None:
    cookies = SimpleCookie()
    cookies.load(request.headers.get("Cookie", ""))
    tenant = cookies.get("dt_tenant")
    return tenant.value if tenant is not None else None


def _isolation_handler(
    *,
    foreign_database_status: int = 404,
    drift_object: bool = False,
):
    requests: list[httpx.Request] = []
    owner_object_reads = 0

    policy = {
        "tenantId": "tenant-owner",
        "courseId": "course-tenant-owner",
        "allowStudentMicro": True,
        "allowStudentFull": True,
        "allowedContentModes": ["open_creation"],
        "allowWebSearch": False,
        "requireApprovalForRestrictedTopics": True,
        "minorSafetyMode": "strict",
        "microSceneLimit": 8,
        "fullSceneLimit": 24,
        "dailyStudentUnits": 20,
        "monthlyStudentUnits": 200,
        "updatedBy": "owner-user-id",
        "updatedAt": "2026-08-28T00:00:00Z",
    }
    owner_sources = {
        "items": [
            {
                "bindingId": "binding-tenant-owner",
                "courseId": "course-tenant-owner",
                "classId": None,
                "sourceType": "pdf",
                "title": "owner source",
                "sha256": "1" * 64,
            }
        ]
    }
    export_status = {
        "exportId": "export-tenant-owner",
        "status": "succeeded",
        "format": "html",
        "byteLength": 15,
        "sha256": "2" * 64,
    }
    owner_session = {
        "sessionId": "session-tenant-owner",
        "classroomVersionId": "version-tenant-owner",
        "status": "active",
        "lastEventSequence": 7,
    }
    owner_projection = {
        "classroomVersionId": "version-tenant-owner",
        "sessionCount": 1,
        "completedCount": 0,
        "completionRate": 0.0,
        "completedSceneCount": 0,
        "validQuizCount": 0,
        "correctQuizCount": 0,
        "hintCount": 0,
        "pblMilestoneCount": 0,
        "mastery": [],
        "projectionLagSeconds": 0.0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal owner_object_reads
        requests.append(request)
        tenant_id = _tenant_cookie(request)
        is_owner = tenant_id == "tenant-owner"
        path = request.url.path

        if path == "/api/v1/teaching/courses":
            assert request.method == "GET"
            assert not is_owner
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "course-tenant-foreign",
                            "title": "foreign course",
                        }
                    ]
                },
            )

        if path == "/api/v1/teaching/courses/course-tenant-owner/generation-policy":
            if is_owner:
                assert request.method == "GET"
                return httpx.Response(200, json=policy)
            if request.method == "GET":
                return httpx.Response(
                    foreign_database_status,
                    json=policy if foreign_database_status == 200 else {"detail": "Not found"},
                )
            assert request.method == "PUT"
            assert isinstance(json.loads(request.content), dict)
            return httpx.Response(404, json={"detail": "Not found"})

        if path == "/api/v1/teaching/sources":
            assert request.method == "GET"
            return httpx.Response(200, json=owner_sources if is_owner else {"items": []})

        if path == "/api/v1/classroom-versions/version-tenant-owner/document":
            assert request.method == "GET"
            assert request.headers["X-Classroom-Ticket"] == "owner-document-ticket"
            if is_owner:
                owner_object_reads += 1
                body = (
                    b"owner-document-v2"
                    if drift_object and owner_object_reads == 2
                    else b"owner-document-v1"
                )
                return httpx.Response(200, content=body)
            return httpx.Response(403, json={"detail": "Classroom content access denied"})

        if path == "/api/v1/teaching/sources/binding-tenant-owner":
            assert request.method == "DELETE"
            assert not is_owner
            return httpx.Response(404, json={"detail": "Source not found"})

        if path == "/api/v1/classroom-exports/export-tenant-owner":
            assert request.method == "GET"
            if is_owner:
                return httpx.Response(200, json=export_status)
            return httpx.Response(404, json={"detail": "Export not found"})

        if path == "/api/v1/classroom-exports/export-tenant-owner/download":
            assert request.method == "GET"
            if is_owner:
                return httpx.Response(200, content=b"owner-export-v1")
            return httpx.Response(404, json={"detail": "Export not found"})

        if path == "/api/v1/classroom-sessions/session-tenant-owner":
            assert request.method == "GET"
            if is_owner:
                return httpx.Response(200, json=owner_session)
            return httpx.Response(404, json={"detail": "Learning session not found"})

        if path == "/api/v1/classroom-sessions/session-tenant-owner/event-ticket":
            assert request.method == "POST"
            assert not is_owner
            return httpx.Response(404, json={"detail": "Learning session not found"})

        if path == "/api/v1/classroom-sessions/session-tenant-owner/events":
            assert not is_owner
            assert request.headers["X-Classroom-Ticket"] == "owner-event-ticket"
            assert json.loads(request.content) == {
                "events": [
                    {
                        "schema_version": "1.0",
                        "event_id": "event-tenant-owner",
                        "event_type": "classroom.started",
                        "occurred_at": "2026-08-28T00:00:00Z",
                    }
                ]
            }
            return httpx.Response(403, json={"detail": "Classroom ticket scope denied"})

        if path == "/api/v1/teaching-reports/classrooms/version-tenant-owner":
            assert request.method == "GET"
            assert is_owner
            return httpx.Response(200, json=owner_projection)

        raise AssertionError(f"unexpected request: {request.method} {path}")

    return httpx.MockTransport(handler), requests


def test_platform_admin_provisions_fixtures_but_role_requests_use_real_login_without_admin_auth() -> (
    None
):
    module = _module()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/v1/auth/users":
            username = json.loads(request.content)["username"]
            return httpx.Response(
                201,
                json={
                    "ok": True,
                    "user_id": f"{username}-id",
                    "username": username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if path.endswith("/members"):
            tenant_id = path.split("/")[4]
            user_id = json.loads(request.content)["user_id"]
            role = json.loads(request.content)["role"]
            return httpx.Response(
                200,
                json={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "roles": [role],
                    "grants": [{"role": role, "scope_type": "tenant", "scope_id": tenant_id}],
                },
            )
        if path == "/api/v1/auth/login":
            username = json.loads(request.content)["username"]
            return httpx.Response(
                200,
                headers={"Set-Cookie": f"dt_token={username}-session; Path=/; HttpOnly"},
                json={
                    "ok": True,
                    "user_id": f"{username}-id",
                    "username": username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if path == "/api/v1/teaching/courses":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async def exercise() -> None:
        async with module.TenantIsolationApi(
            "https://classroom.example.test",
            "platform-admin-secret",
            transport=httpx.MockTransport(handler),
        ) as api:
            for username in ("isolation-owner", "isolation-foreign"):
                await api.admin_json(
                    "POST",
                    "/api/v1/auth/users",
                    json_body={"username": username, "password": f"{username}-password"},
                    expected_statuses=frozenset({201}),
                )
            for tenant_id, username, role in (
                ("tenant-owner", "isolation-owner", "teacher"),
                ("tenant-foreign", "isolation-foreign", "student"),
            ):
                await api.tenant_admin_json(
                    "POST",
                    f"/api/v1/tenants/{tenant_id}/members",
                    tenant_id=tenant_id,
                    json_body={"user_id": f"{username}-id", "role": role},
                )
            owner = await api.login_identity("isolation-owner", "isolation-owner-password")
            foreign = await api.login_identity("isolation-foreign", "isolation-foreign-password")
            await api.tenant_identity_json(
                "GET",
                "/api/v1/teaching/courses",
                identity=owner,
                tenant_id="tenant-owner",
                headers={"Authorization": "Bearer platform-admin-secret"},
            )
            await api.tenant_identity_json(
                "GET",
                "/api/v1/teaching/courses",
                identity=foreign,
                tenant_id="tenant-foreign",
                headers={"Authorization": "Bearer platform-admin-secret"},
            )

    asyncio.run(exercise())

    admin_requests = seen[:4]
    login_requests = seen[4:6]
    role_requests = seen[6:]
    assert len(admin_requests) == 4
    assert all(
        request.headers["Authorization"] == "Bearer platform-admin-secret"
        for request in admin_requests
    )
    assert [request.url.path for request in login_requests] == [
        "/api/v1/auth/login",
        "/api/v1/auth/login",
    ]
    assert [json.loads(request.content)["username"] for request in login_requests] == [
        "isolation-owner",
        "isolation-foreign",
    ]
    assert len(role_requests) == 2
    for request, tenant_id, token in zip(
        role_requests,
        ("tenant-owner", "tenant-foreign"),
        ("isolation-owner-session", "isolation-foreign-session"),
        strict=True,
    ):
        assert "Authorization" not in request.headers
        assert request.headers["X-Tenant-ID"] == tenant_id
        cookies = SimpleCookie()
        cookies.load(request.headers["Cookie"])
        assert {name: morsel.value for name, morsel in cookies.items()} == {
            "dt_token": token,
            "dt_tenant": tenant_id,
        }


def test_candidate_response_limit_is_enforced_while_streaming() -> None:
    module = _module()
    module._MAX_RESPONSE_BYTES = 3

    class ChunkedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ab"
            yield b"cd"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedBody())

    async def exercise() -> None:
        async with module.TenantIsolationApi(
            "https://classroom.example.test",
            "platform-admin-secret",
            transport=httpx.MockTransport(handler),
        ) as api:
            with pytest.raises(
                module.TenantIsolationProbeError,
                match="candidate_response_too_large",
            ):
                await api.admin_response("GET", "/api/v1/teaching/courses")

    asyncio.run(exercise())


def test_role_requests_reject_header_cookie_tenant_conflicts_before_transport() -> None:
    module = _module()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    identity = _identity(
        module,
        username="isolation-owner",
        user_id="owner-user-id",
        token="secret-session-owner-7f19",
    )

    async def exercise() -> None:
        async with module.TenantIsolationApi(
            "https://classroom.example.test",
            "platform-admin-secret",
            transport=httpx.MockTransport(handler),
        ) as api:
            for headers in (
                {"X-Tenant-ID": "tenant-foreign"},
                {"Cookie": "dt_tenant=tenant-foreign"},
            ):
                with pytest.raises(
                    module.TenantIsolationProbeError,
                    match="tenant_binding_conflict",
                ):
                    await api.tenant_identity_json(
                        "GET",
                        "/api/v1/teaching/courses",
                        identity=identity,
                        tenant_id="tenant-owner",
                        headers=headers,
                    )

    asyncio.run(exercise())
    assert seen == []


def test_active_tenant_pair_is_rechecked_and_must_be_distinct_before_target_construction() -> None:
    module = _module()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        tenant_id = request.url.path.split("/")[4]
        return httpx.Response(
            200,
            json={
                "tenant_id": tenant_id,
                "status": "active",
                "job_id": f"provision-{tenant_id}",
                "job_status": "completed",
                "attempt_count": 1,
            },
        )

    async def exercise() -> None:
        async with module.TenantIsolationApi(
            "https://classroom.example.test",
            "platform-admin-secret",
            transport=httpx.MockTransport(handler),
        ) as api:
            assert await module.resolve_active_tenant_pair(
                api,
                ("tenant-owner", "tenant-foreign"),
            ) == ("tenant-owner", "tenant-foreign")
            with pytest.raises(module.TenantIsolationProbeError, match="tenant_pair_invalid"):
                await module.resolve_active_tenant_pair(
                    api,
                    ("tenant-owner", "tenant-owner"),
                )

    asyncio.run(exercise())
    assert seen == [
        "/api/v1/tenants/tenant-owner/provisioning",
        "/api/v1/tenants/tenant-foreign/provisioning",
    ]


def test_real_owner_targets_are_denied_to_foreign_role_then_reread_unchanged() -> None:
    module = _module()
    transport, requests = _isolation_handler()
    owner = _identity(
        module,
        username="isolation-owner",
        user_id="owner-user-id",
        token="secret-session-owner-7f19",
    )
    foreign = _identity(
        module,
        username="isolation-foreign",
        user_id="foreign-user-id",
        token="secret-session-foreign-2c84",
    )

    async def exercise() -> list[dict[str, object]]:
        async with module.TenantIsolationApi(
            "https://classroom.example.test",
            "platform-admin-secret",
            transport=transport,
        ) as api:
            return await module.verify_isolation_targets(
                api,
                owner_identity=owner,
                owner_tenant_id="tenant-owner",
                foreign_identity=foreign,
                foreign_tenant_id="tenant-foreign",
                targets=_targets(),
                document_ticket=module.SecretStr("owner-document-ticket"),
                event_ticket=module.SecretStr("owner-event-ticket"),
            )

    observations = asyncio.run(exercise())

    assert [observation["layer"] for observation in observations] == [
        "database",
        "objects",
        "exports",
        "events",
    ]
    assert [_operation["name"] for _operation in observations[0]["operations"]] == [
        "owner-policy-before",
        "foreign-list",
        "foreign-read",
        "foreign-write",
        "owner-policy-after",
    ]
    assert [_operation["name"] for _operation in observations[1]["operations"]] == [
        "owner-source-list-before",
        "owner-document-before",
        "foreign-source-list",
        "foreign-document-read",
        "foreign-source-delete",
        "owner-source-list-after",
        "owner-document-after",
    ]
    assert [_operation["name"] for _operation in observations[2]["operations"]] == [
        "owner-status-before",
        "owner-download-before",
        "foreign-status-read",
        "foreign-download-read",
        "owner-status-after",
        "owner-download-after",
    ]
    assert [_operation["name"] for _operation in observations[3]["operations"]] == [
        "owner-session-before",
        "owner-projection-before",
        "foreign-session-read",
        "foreign-ticket-issue",
        "foreign-event-ingest",
        "owner-session-after",
        "owner-projection-after",
    ]
    assert [observation["target"]["resourceIds"] for observation in observations] == [
        {"courseId": "course-tenant-owner"},
        {
            "bindingId": "binding-tenant-owner",
            "classroomVersionId": "version-tenant-owner",
        },
        {"exportId": "export-tenant-owner"},
        {
            "sessionId": "session-tenant-owner",
            "eventId": "event-tenant-owner",
            "classroomVersionId": "version-tenant-owner",
        },
    ]
    contract = _contract_module()
    candidate = {"sourceHead": "a" * 40}
    release_run = {"runId": "run-a", "environmentId": "acceptance-a"}
    capacity_report_sha256 = "9" * 64
    report = {
        "schemaVersion": 1,
        "producer": "tenant-isolation-probe",
        "candidate": candidate,
        "releaseRun": release_run,
        "observedAt": "2026-08-28T00:00:00Z",
        "baseUrl": "https://classroom.example.test",
        "capacityProof": {
            "reportSha256": capacity_report_sha256,
            "tenantIds": ["tenant-owner", "tenant-foreign"],
        },
        "principals": [
            {
                "tenantId": "tenant-owner",
                "actorId": "owner-user-id",
                "role": "user",
                "membershipStatus": "active",
            },
            {
                "tenantId": "tenant-foreign",
                "actorId": "foreign-user-id",
                "role": "user",
                "membershipStatus": "active",
            },
        ],
        "crossTenantPrincipal": {
            "tenantId": "tenant-foreign",
            "actorId": "owner-user-id",
            "role": "student",
            "membershipStatus": "active",
        },
        "observations": observations,
    }
    body = contract.canonical_tenant_isolation_report(report)
    parsed = contract.parse_tenant_isolation_report(
        body,
        candidate=candidate,
        release_run=release_run,
        expected_base_url="https://classroom.example.test",
        expected_capacity_report_sha256=capacity_report_sha256,
        expected_capacity_tenant_ids=("tenant-owner", "tenant-foreign"),
        forbidden_secret_values=(
            b"platform-admin-secret",
            b"secret-session-owner-7f19",
            b"secret-session-foreign-2c84",
            b"owner-document-ticket",
            b"owner-event-ticket",
        ),
    )
    assert contract.derive_tenant_isolation_checks(parsed) == {
        "databaseIsolated": True,
        "objectsIsolated": True,
        "exportsIsolated": True,
        "eventsIsolated": True,
    }

    event_requests = [
        request
        for request in requests
        if _tenant_cookie(request) == "tenant-foreign"
        and "session-tenant-owner" in request.url.path
    ]
    assert [request.url.path for request in event_requests] == [
        "/api/v1/classroom-sessions/session-tenant-owner",
        "/api/v1/classroom-sessions/session-tenant-owner/event-ticket",
        "/api/v1/classroom-sessions/session-tenant-owner/events",
    ]
    assert all(
        "dt_token=secret-session-owner-7f19" in request.headers["Cookie"]
        for request in event_requests
    )
    foreign_course_lists = [
        request
        for request in requests
        if _tenant_cookie(request) == "tenant-foreign"
        and request.url.path == "/api/v1/teaching/courses"
    ]
    assert len(foreign_course_lists) == 1
    assert "dt_token=secret-session-owner-7f19" in foreign_course_lists[0].headers["Cookie"]
    assert all("Authorization" not in request.headers for request in requests)
    serialized = json.dumps(observations, sort_keys=True).lower()
    for secret in (
        "platform-admin-secret",
        "secret-session-owner-7f19",
        "secret-session-foreign-2c84",
        "owner-document-ticket",
        "owner-event-ticket",
    ):
        assert secret not in serialized


def test_isolation_verification_fails_closed_when_foreign_access_succeeds() -> None:
    module = _module()
    transport, _requests = _isolation_handler(foreign_database_status=200)

    async def exercise() -> None:
        async with module.TenantIsolationApi(
            "https://classroom.example.test",
            "platform-admin-secret",
            transport=transport,
        ) as api:
            with pytest.raises(
                module.TenantIsolationProbeError,
                match="tenant_isolation_failed",
            ):
                await module.verify_isolation_targets(
                    api,
                    owner_identity=_identity(
                        module,
                        username="isolation-owner",
                        user_id="owner-user-id",
                        token="owner-session",
                    ),
                    owner_tenant_id="tenant-owner",
                    foreign_identity=_identity(
                        module,
                        username="isolation-foreign",
                        user_id="foreign-user-id",
                        token="foreign-session",
                    ),
                    foreign_tenant_id="tenant-foreign",
                    targets=_targets(),
                    document_ticket=module.SecretStr("owner-document-ticket"),
                    event_ticket=module.SecretStr("owner-event-ticket"),
                )

    asyncio.run(exercise())


def test_isolation_verification_fails_closed_when_owner_state_drifts() -> None:
    module = _module()
    transport, _requests = _isolation_handler(drift_object=True)

    async def exercise() -> None:
        async with module.TenantIsolationApi(
            "https://classroom.example.test",
            "platform-admin-secret",
            transport=transport,
        ) as api:
            with pytest.raises(
                module.TenantIsolationProbeError,
                match="tenant_isolation_failed",
            ):
                await module.verify_isolation_targets(
                    api,
                    owner_identity=_identity(
                        module,
                        username="isolation-owner",
                        user_id="owner-user-id",
                        token="owner-session",
                    ),
                    owner_tenant_id="tenant-owner",
                    foreign_identity=_identity(
                        module,
                        username="isolation-foreign",
                        user_id="foreign-user-id",
                        token="foreign-session",
                    ),
                    foreign_tenant_id="tenant-foreign",
                    targets=_targets(),
                    document_ticket=module.SecretStr("owner-document-ticket"),
                    event_ticket=module.SecretStr("owner-event-ticket"),
                )

    asyncio.run(exercise())


def test_identity_cleanup_uses_expected_user_id_and_preserves_an_aba_replacement() -> None:
    module = _module()
    calls: list[tuple[str, str, object | None]] = []

    class Api:
        async def admin_json(self, method, path, *, json_body=None, **_kwargs):
            calls.append((method, path, json_body))
            raise module.TenantIsolationProbeError("candidate_request_failed")

        async def admin_list_json(self, method, path, **_kwargs):
            calls.append((method, path, None))
            return [
                {
                    "id": "replacement-user-id",
                    "username": "isolation-owner",
                    "role": "user",
                    "created_at": "2026-08-28T00:00:00Z",
                    "disabled": False,
                    "avatar": "",
                }
            ]

    with pytest.raises(module.TenantIsolationProbeError, match="identity_cleanup_failed"):
        asyncio.run(
            module.delete_identity_with_reconciliation(
                Api(),
                username="isolation-owner",
                expected_user_id="original-user-id",
            )
        )

    assert calls == [
        (
            "DELETE",
            "/api/v1/auth/users/isolation-owner",
            {"expected_user_id": "original-user-id"},
        ),
        ("GET", "/api/v1/auth/users", None),
    ]


def test_identity_creation_preflight_fails_before_any_account_write() -> None:
    module = _module()
    calls: list[tuple[str, str]] = []

    class Api:
        async def admin_list_json(self, method, path, **_kwargs):
            calls.append((method, path))
            raise module.TenantIsolationProbeError("candidate_request_rejected")

        async def admin_json(self, *_args, **_kwargs):
            pytest.fail("identity creation must not run after a failed backend preflight")

    with pytest.raises(
        module.TenantIsolationProbeError,
        match="identity_creation_preflight_failed",
    ):
        asyncio.run(
            module.ensure_identity_creation_ready(
                Api(),
                usernames=("isolation-owner", "isolation-foreign"),
            )
        )

    assert calls == [("GET", "/api/v1/auth/users")]


def test_membership_cleanup_uses_exact_expected_tuple() -> None:
    module = _module()
    calls: list[tuple[object, ...]] = []

    class Api:
        async def tenant_admin_response(
            self,
            method,
            path,
            *,
            tenant_id,
            json_body,
        ):
            calls.append((method, path, tenant_id, json_body))
            return httpx.Response(204, content=b"")

    asyncio.run(
        module.delete_membership_with_reconciliation(
            Api(),
            tenant_id="tenant-owner",
            expected_user_id="owner-user-id",
        )
    )

    assert calls == [
        (
            "DELETE",
            "/api/v1/tenants/tenant-owner/members/owner-user-id",
            "tenant-owner",
            {
                "expected_tenant_id": "tenant-owner",
                "expected_user_id": "owner-user-id",
            },
        )
    ]


def test_membership_cleanup_rejects_an_uncontracted_not_found_response() -> None:
    module = _module()
    calls: list[tuple[object, ...]] = []

    class Api:
        async def tenant_admin_response(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return httpx.Response(404, json={"detail": "Not Found"})

        async def tenant_admin_json(self, *_args, **_kwargs):
            pytest.fail("generic 404 must fail before provisioning reconciliation")

    with pytest.raises(module.TenantIsolationProbeError, match="membership_cleanup_failed"):
        asyncio.run(
            module.delete_membership_with_reconciliation(
                Api(),
                tenant_id="tenant-owner",
                expected_user_id="owner-user-id",
            )
        )

    assert len(calls) == 1


def test_membership_cleanup_accepts_only_the_exact_member_tombstone() -> None:
    module = _module()
    calls: list[tuple[str, str]] = []

    class Api:
        async def tenant_admin_response(self, method, path, **_kwargs):
            calls.append((method, path))
            return httpx.Response(404, json={"detail": "Tenant membership not found"})

        async def tenant_admin_json(self, method, path, **_kwargs):
            calls.append((method, path))
            return {"tenant_id": "tenant-owner"}

    asyncio.run(
        module.delete_membership_with_reconciliation(
            Api(),
            tenant_id="tenant-owner",
            expected_user_id="owner-user-id",
        )
    )

    assert calls == [
        ("DELETE", "/api/v1/tenants/tenant-owner/members/owner-user-id"),
        ("GET", "/api/v1/tenants/tenant-owner/provisioning"),
    ]


def test_cleanup_preserves_owner_credentials_when_reversible_resources_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    owner = _identity(
        module,
        username="isolation-owner",
        user_id="owner-user-id",
        token="owner-session-token",
    )
    state = module.IsolationCleanupState(
        tenant_id="tenant-owner",
        user_id="owner-user-id",
        class_id="class-owner",
        enrollment_active=True,
    )
    membership_calls: list[tuple[str, str]] = []
    identity_calls: list[tuple[str, str]] = []

    async def fail_resources(*_args, **_kwargs):
        raise module.TenantIsolationProbeError("fixture_cleanup_failed")

    async def cleanup_membership(_api, *, tenant_id, expected_user_id):
        membership_calls.append((tenant_id, expected_user_id))

    async def cleanup_identity(_api, *, username, expected_user_id):
        identity_calls.append((username, expected_user_id))

    monkeypatch.setattr(module, "cleanup_reversible_fixture_resources", fail_resources)
    monkeypatch.setattr(module, "delete_membership_with_reconciliation", cleanup_membership)
    monkeypatch.setattr(module, "delete_identity_with_reconciliation", cleanup_identity)

    cleanup_failed = asyncio.run(
        module._cleanup_isolation_state(
            object(),
            cleanup_state=state,
            owner_identity=owner,
            owner_user_id="owner-user-id",
            memberships=[
                ("tenant-owner", "owner-user-id"),
                ("tenant-foreign", "foreign-user-id"),
                ("tenant-foreign", "owner-user-id"),
            ],
            created=[
                ("isolation-owner", "owner-user-id"),
                ("isolation-foreign", "foreign-user-id"),
            ],
            checkpoint=lambda *_args: None,
        )
    )

    assert cleanup_failed is True
    assert membership_calls == [("tenant-foreign", "foreign-user-id")]
    assert identity_calls == [("isolation-foreign", "foreign-user-id")]


def test_cleanup_preserves_an_identity_when_membership_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    membership_calls: list[tuple[str, str]] = []
    identity_calls: list[tuple[str, str]] = []

    async def cleanup_membership(_api, *, tenant_id, expected_user_id):
        membership_calls.append((tenant_id, expected_user_id))
        if expected_user_id == "owner-user-id":
            raise module.TenantIsolationProbeError("membership_cleanup_failed")

    async def cleanup_identity(_api, *, username, expected_user_id):
        identity_calls.append((username, expected_user_id))

    monkeypatch.setattr(module, "delete_membership_with_reconciliation", cleanup_membership)
    monkeypatch.setattr(module, "delete_identity_with_reconciliation", cleanup_identity)

    cleanup_failed = asyncio.run(
        module._cleanup_isolation_state(
            object(),
            cleanup_state=None,
            owner_identity=None,
            owner_user_id="owner-user-id",
            memberships=[
                ("tenant-owner", "owner-user-id"),
                ("tenant-foreign", "foreign-user-id"),
            ],
            created=[
                ("isolation-owner", "owner-user-id"),
                ("isolation-foreign", "foreign-user-id"),
            ],
            checkpoint=lambda *_args: None,
        )
    )

    assert cleanup_failed is True
    assert membership_calls == [
        ("tenant-foreign", "foreign-user-id"),
        ("tenant-owner", "owner-user-id"),
    ]
    assert identity_calls == [("isolation-foreign", "foreign-user-id")]


def test_identity_material_is_stable_for_same_release_cleanup_retry(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)

    first = module._identity_material(config, attempt_id="attempt-retry")
    second = module._identity_material(config, attempt_id="attempt-retry")
    next_attempt = module._identity_material(config, attempt_id="attempt-next")

    assert second == first
    assert next_attempt != first
    assert "secret-platform-admin-token" not in first.owner_username
    assert "secret-platform-admin-token" not in first.foreign_username


def test_cleanup_recovery_journal_is_secret_free_and_replayed_before_new_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)
    attempt_id = "attempt-retry"
    material = module._identity_material(config, attempt_id=attempt_id)
    state = module.IsolationCleanupState(
        tenant_id="tenant-owner",
        user_id="owner-user-id",
        class_id="class-owner",
        enrollment_active=True,
        source_binding_id="binding-owner",
        learning_session_id="session-owner",
    )
    created = (
        (material.owner_username, "owner-user-id"),
        (material.foreign_username, "foreign-user-id"),
    )
    memberships = (
        ("tenant-owner", "owner-user-id"),
        ("tenant-foreign", "foreign-user-id"),
        ("tenant-foreign", "owner-user-id"),
    )

    module._write_cleanup_recovery_state(
        config,
        attempt_id=attempt_id,
        material=material,
        cleanup_state=state,
        memberships=memberships,
        created=created,
    )
    recovery_path = module._cleanup_recovery_path(config)
    persisted = recovery_path.read_bytes()
    for secret in (
        config.admin_token.get_secret_value().encode(),
        material.owner_password.get_secret_value().encode(),
        material.foreign_password.get_secret_value().encode(),
    ):
        assert secret not in persisted

    events: list[object] = []

    class Api:
        async def login_identity(self, username, password):
            events.append(("login", username, password.get_secret_value()))
            return module.IdentityCredential(
                username,
                "owner-user-id",
                module.SecretStr("owner-recovery-session"),
            )

    async def cleanup(api, **kwargs):
        assert isinstance(api, Api)
        events.append(("cleanup", kwargs))
        return False

    monkeypatch.setattr(module, "_cleanup_isolation_state", cleanup)

    asyncio.run(module._recover_pending_cleanup(Api(), config=config))

    assert events[0] == (
        "login",
        material.owner_username,
        material.owner_password.get_secret_value(),
    )
    assert events[1][0] == "cleanup"
    cleanup_kwargs = dict(events[1][1])
    assert callable(cleanup_kwargs.pop("checkpoint"))
    assert cleanup_kwargs == {
        "cleanup_state": state,
        "owner_identity": module.IdentityCredential(
            material.owner_username,
            "owner-user-id",
            module.SecretStr("owner-recovery-session"),
        ),
        "owner_user_id": "owner-user-id",
        "memberships": memberships,
        "created": created,
    }
    assert not recovery_path.exists()


def test_failed_cleanup_recovery_keeps_the_bound_journal_for_another_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)
    attempt_id = "attempt-retry"
    material = module._identity_material(config, attempt_id=attempt_id)
    module._write_cleanup_recovery_state(
        config,
        attempt_id=attempt_id,
        material=material,
        cleanup_state=None,
        memberships=(("tenant-owner", "owner-user-id"),),
        created=(
            (material.owner_username, "owner-user-id"),
            (material.foreign_username, "foreign-user-id"),
        ),
    )
    recovery_path = module._cleanup_recovery_path(config)

    async def fail_cleanup(*_args, **_kwargs):
        return True

    monkeypatch.setattr(module, "_cleanup_isolation_state", fail_cleanup)

    with pytest.raises(module.TenantIsolationProbeError, match="cleanup_recovery_failed"):
        asyncio.run(module._recover_pending_cleanup(object(), config=config))

    assert recovery_path.is_file()


def test_cleanup_recovery_rejects_tampering_before_any_api_call(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)
    attempt_id = "attempt-retry"
    material = module._identity_material(config, attempt_id=attempt_id)
    module._write_cleanup_recovery_state(
        config,
        attempt_id=attempt_id,
        material=material,
        cleanup_state=None,
        memberships=(),
        created=((material.owner_username, "owner-user-id"),),
    )
    recovery_path = module._cleanup_recovery_path(config)
    tampered = json.loads(recovery_path.read_text(encoding="utf-8"))
    tampered["created"][0]["userId"] = "unrelated-user-id"
    recovery_path.write_text(json.dumps(tampered), encoding="utf-8")

    class NoApiCalls:
        def __getattr__(self, name):
            pytest.fail(f"tampered recovery must not call API method {name}")

    with pytest.raises(module.TenantIsolationProbeError, match="cleanup_recovery_invalid"):
        asyncio.run(module._recover_pending_cleanup(NoApiCalls(), config=config))


def test_cleanup_recovery_lock_rejects_a_concurrent_runner_before_http(
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)

    with module._cleanup_recovery_lock(config):
        with pytest.raises(module.TenantIsolationProbeError, match="cleanup_recovery_locked"):
            with module._cleanup_recovery_lock(config):
                pytest.fail("a second runner must never acquire the same cleanup lock")


def test_cleanup_recovery_authenticates_an_unconfirmed_identity_intent_before_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)
    attempt_id = "attempt-retry"
    material = module._identity_material(config, attempt_id=attempt_id)
    module._write_cleanup_recovery_state(
        config,
        attempt_id=attempt_id,
        material=material,
        cleanup_state=None,
        memberships=(),
        created=(),
    )
    cleanup_called = False
    login_called = False

    class Api:
        async def admin_list_json(self, _method, _path, **_kwargs):
            return [
                {
                    "id": "unrelated-user-id",
                    "username": material.owner_username,
                    "role": "user",
                    "is_admin": False,
                }
            ]

        async def login_identity(self, _username, _password):
            nonlocal login_called
            login_called = True
            raise module.TenantIsolationProbeError("identity_login_failed")

    async def cleanup(*_args, **_kwargs):
        nonlocal cleanup_called
        cleanup_called = True
        return False

    monkeypatch.setattr(module, "_cleanup_isolation_state", cleanup)

    with pytest.raises(module.TenantIsolationProbeError, match="cleanup_recovery_failed"):
        asyncio.run(module._recover_pending_cleanup(Api(), config=config))

    assert cleanup_called is False
    assert login_called is True
    assert module._cleanup_recovery_path(config).is_file()


def test_foreign_cleanup_failure_preserves_owner_memberships_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    membership_calls: list[tuple[str, str]] = []
    identity_calls: list[tuple[str, str]] = []
    checkpoints: list[object] = []

    async def cleanup_membership(_api, *, tenant_id, expected_user_id):
        membership_calls.append((tenant_id, expected_user_id))
        if expected_user_id == "foreign-user-id":
            raise module.TenantIsolationProbeError("membership_cleanup_failed")

    async def cleanup_identity(_api, *, username, expected_user_id):
        identity_calls.append((username, expected_user_id))

    monkeypatch.setattr(module, "delete_membership_with_reconciliation", cleanup_membership)
    monkeypatch.setattr(module, "delete_identity_with_reconciliation", cleanup_identity)

    cleanup_failed = asyncio.run(
        module._cleanup_isolation_state(
            object(),
            cleanup_state=None,
            owner_identity=None,
            owner_user_id="owner-user-id",
            memberships=[
                ("tenant-owner", "owner-user-id"),
                ("tenant-foreign", "foreign-user-id"),
                ("tenant-foreign", "owner-user-id"),
            ],
            created=[
                ("isolation-owner", "owner-user-id"),
                ("isolation-foreign", "foreign-user-id"),
            ],
            checkpoint=lambda *args: checkpoints.append(args),
        )
    )

    assert cleanup_failed is True
    assert membership_calls == [("tenant-foreign", "foreign-user-id")]
    assert identity_calls == []
    assert checkpoints == []


def test_recovery_checkpoints_an_empty_state_before_journal_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)
    attempt_id = "attempt-retry"
    material = module._identity_material(config, attempt_id=attempt_id)
    module._write_cleanup_recovery_state(
        config,
        attempt_id=attempt_id,
        material=material,
        cleanup_state=None,
        memberships=(),
        created=(
            (material.owner_username, "owner-user-id"),
            (material.foreign_username, "foreign-user-id"),
        ),
    )

    async def cleanup(*_args, **_kwargs):
        return False

    def fail_remove(_config):
        raise module.TenantIsolationProbeError("cleanup_recovery_remove_failed")

    monkeypatch.setattr(module, "_cleanup_isolation_state", cleanup)
    monkeypatch.setattr(module, "_remove_cleanup_recovery_state", fail_remove)

    with pytest.raises(module.TenantIsolationProbeError, match="cleanup_recovery_remove_failed"):
        asyncio.run(module._recover_pending_cleanup(object(), config=config))

    pending = module._read_cleanup_recovery_state(config)
    assert pending is not None
    _persisted_material, recovery = pending
    assert recovery.cleanup_state is None
    assert recovery.memberships == ()
    assert recovery.created == ()


@pytest.mark.parametrize(
    ("detail", "should_pass"),
    [
        ("source binding not found", True),
        ("gateway route not found", False),
    ],
)
def test_owner_resource_cleanup_accepts_only_the_exact_tombstone(
    detail: str,
    should_pass: bool,
) -> None:
    module = _module()

    class Response:
        status_code = 404

        @staticmethod
        def json():
            return {"detail": detail}

    class Api:
        async def tenant_identity_response(self, *_args, **_kwargs):
            return Response()

    operation = module._delete_owner_resource(
        Api(),
        path="/api/v1/teaching/sources/binding-owner",
        identity=_identity(
            module,
            username="isolation-owner",
            user_id="owner-user-id",
            token="owner-session",
        ),
        tenant_id="tenant-owner",
        expected_not_found_detail="source binding not found",
    )
    if should_pass:
        asyncio.run(operation)
    else:
        with pytest.raises(module.TenantIsolationProbeError, match="fixture_cleanup_failed"):
            asyncio.run(operation)


def test_reversible_fixture_cleanup_completes_session_then_removes_binding_and_enrollment() -> None:
    module = _module()
    calls: list[tuple[str, str]] = []
    owner = _identity(
        module,
        username="isolation-owner",
        user_id="owner-user-id",
        token="owner-session-token",
    )
    state = module.IsolationCleanupState(
        tenant_id="tenant-owner",
        user_id="owner-user-id",
        class_id="class-owner",
        enrollment_active=True,
        source_binding_id="binding-owner",
        learning_session_id="session-owner",
    )

    class Api:
        async def tenant_identity_json(self, method, path, **_kwargs):
            calls.append((method, path))
            return {"id": "session-owner", "status": "completed"}

        async def tenant_identity_response(self, method, path, **_kwargs):
            calls.append((method, path))
            return httpx.Response(204, content=b"")

    asyncio.run(
        module.cleanup_reversible_fixture_resources(
            Api(),
            state=state,
            owner_identity=owner,
        )
    )

    assert calls == [
        ("POST", "/api/v1/classroom-sessions/session-owner/complete"),
        ("DELETE", "/api/v1/teaching/sources/binding-owner"),
        (
            "DELETE",
            "/api/v1/teaching/classes/class-owner/enrollments/owner-user-id",
        ),
    ]
    assert state.learning_session_id is None
    assert state.source_binding_id is None
    assert state.enrollment_active is False


def test_cli_never_writes_token_password_cookie_or_ticket_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    module = _module()
    config = SimpleNamespace(
        candidate={"sourceHead": "a" * 40},
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        base_url="https://classroom.example.test",
    )
    forbidden_body = (
        b'{"token":"admin-secret","password":"role-secret",'
        b'"cookie":"session-secret","ticket":"content-secret"}\n'
    )
    monkeypatch.setattr(module, "_parse_args", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_load_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        module,
        "_run_tenant_isolation_probe",
        lambda _config: _async_value(forbidden_body),
    )

    assert module.main([]) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert captured.err == b"tenant_isolation_report_invalid\n"
    for secret in (b"admin-secret", b"role-secret", b"session-secret", b"content-secret"):
        assert secret not in captured.err


async def _async_value(value: bytes) -> bytes:
    return value


def _live_candidate() -> dict[str, object]:
    return {
        "sourceRepository": "https://github.com/xinlingzhifei/yFeiSTAI.git",
        "sourceHead": "a" * 40,
        "releaseTag": f"yfeistai-first-release-20260828-{'a' * 8}",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": f"sha256:{'1' * 64}",
            "openmaic": f"sha256:{'2' * 64}",
            "openmaic_render": f"sha256:{'3' * 64}",
        },
    }


def _write_capacity_attestation(root: Path) -> tuple[Path, str]:
    path = root / "runtime" / "capacity-profile-attestation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b'{"candidate":"capacity-bound-live-proof","schemaVersion":1}\n'
    path.write_bytes(body)
    return path.resolve(), hashlib.sha256(body).hexdigest()


def _live_environment(
    root: Path,
    capacity_path: Path,
    capacity_sha256: str,
    *,
    tenant_ids: object = ("tenant-owner", "tenant-foreign"),
) -> dict[str, str]:
    return {
        "YFEISTAI_LIVE_FIXTURE_TOKEN": "secret-platform-admin-token",
        "YFEISTAI_CANDIDATE_ROOT": str(root),
        "YFEISTAI_RELEASE_RUN_ID": "run-tenant-isolation",
        "YFEISTAI_ENVIRONMENT_ID": "acceptance-tenant-isolation",
        "YFEISTAI_TENANT_ISOLATION_TIMEOUT_SECONDS": "600",
        "YFEISTAI_CAPACITY_ATTESTATION_PATH": str(capacity_path),
        "YFEISTAI_CAPACITY_ATTESTATION_SHA256": capacity_sha256,
        "YFEISTAI_CAPACITY_TENANT_IDS": json.dumps(tenant_ids),
        "WEB_BASE_URL": "https://classroom.example.test",
    }


def test_load_config_binds_current_candidate_and_capacity_attestation_without_leaking_token(
    tmp_path: Path,
) -> None:
    module = _module()
    root = (tmp_path / "candidate").resolve()
    root.mkdir()
    capacity_path, capacity_sha256 = _write_capacity_attestation(root)
    seen: list[Path] = []

    def load_candidate(candidate_root: Path) -> dict[str, object]:
        seen.append(candidate_root)
        return _live_candidate()

    config = module._load_config(
        _live_environment(root, capacity_path, capacity_sha256),
        cwd=root,
        candidate_loader=load_candidate,
    )

    assert seen == [root]
    assert config.candidate_root == root
    assert config.candidate == _live_candidate()
    assert config.release_run == {
        "runId": "run-tenant-isolation",
        "environmentId": "acceptance-tenant-isolation",
    }
    assert config.base_url == "https://classroom.example.test"
    assert config.timeout_seconds == 600
    assert config.capacity_attestation_path == capacity_path
    assert config.capacity_attestation_sha256 == capacity_sha256
    assert config.capacity_tenant_ids == ("tenant-owner", "tenant-foreign")
    assert config.admin_token.get_secret_value() == "secret-platform-admin-token"
    assert "secret-platform-admin-token" not in repr(config)


def test_load_config_fails_closed_when_fixed_capacity_reader_rejects_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    root = (tmp_path / "candidate").resolve()
    root.mkdir()
    capacity_path, capacity_sha256 = _write_capacity_attestation(root)
    seen: list[tuple[Path, Path]] = []

    def reject_capacity(path: Path, *, bundle_root: Path):
        seen.append((path, bundle_root))
        raise ValueError("simulated no-follow rejection")

    monkeypatch.setattr(
        module,
        "read_capacity_profile_attestation_artifact",
        reject_capacity,
    )

    with pytest.raises(module.TenantIsolationProbeError, match="capacity_attestation_invalid"):
        module._load_config(
            _live_environment(root, capacity_path, capacity_sha256),
            cwd=root,
            candidate_loader=lambda _root: _live_candidate(),
        )

    assert seen == [(capacity_path, root)]


@pytest.mark.parametrize(
    "invalid_case",
    (
        "different-candidate-root",
        "capacity-outside-candidate",
        "capacity-relative-path",
        "capacity-sha-mismatch",
        "capacity-sha-zero",
        "one-capacity-tenant",
        "duplicate-capacity-tenants",
        "three-capacity-tenants",
        "unsafe-capacity-tenant",
        "unsafe-base-url",
    ),
)
def test_load_config_rejects_cross_worktree_or_untrusted_capacity_binding(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    module = _module()
    root = (tmp_path / "candidate").resolve()
    root.mkdir()
    capacity_path, capacity_sha256 = _write_capacity_attestation(root)
    environment = _live_environment(root, capacity_path, capacity_sha256)
    cwd = root

    if invalid_case == "different-candidate-root":
        cwd = (tmp_path / "other-worktree").resolve()
        cwd.mkdir()
        expected = "candidate_root_invalid"
    elif invalid_case == "capacity-outside-candidate":
        outside = (tmp_path / "other-worktree").resolve()
        outside.mkdir()
        outside_path, outside_sha256 = _write_capacity_attestation(outside)
        environment["YFEISTAI_CAPACITY_ATTESTATION_PATH"] = str(outside_path)
        environment["YFEISTAI_CAPACITY_ATTESTATION_SHA256"] = outside_sha256
        expected = "capacity_attestation_invalid"
    elif invalid_case == "capacity-relative-path":
        environment["YFEISTAI_CAPACITY_ATTESTATION_PATH"] = (
            "runtime/capacity-profile-attestation.json"
        )
        expected = "capacity_attestation_invalid"
    elif invalid_case == "capacity-sha-mismatch":
        environment["YFEISTAI_CAPACITY_ATTESTATION_SHA256"] = "f" * 64
        expected = "capacity_attestation_invalid"
    elif invalid_case == "capacity-sha-zero":
        environment["YFEISTAI_CAPACITY_ATTESTATION_SHA256"] = "0" * 64
        expected = "capacity_attestation_invalid"
    elif invalid_case == "one-capacity-tenant":
        environment["YFEISTAI_CAPACITY_TENANT_IDS"] = '["tenant-owner"]'
        expected = "capacity_tenant_ids_invalid"
    elif invalid_case == "duplicate-capacity-tenants":
        environment["YFEISTAI_CAPACITY_TENANT_IDS"] = '["tenant-owner","tenant-owner"]'
        expected = "capacity_tenant_ids_invalid"
    elif invalid_case == "three-capacity-tenants":
        environment["YFEISTAI_CAPACITY_TENANT_IDS"] = (
            '["tenant-owner","tenant-foreign","tenant-extra"]'
        )
        expected = "capacity_tenant_ids_invalid"
    elif invalid_case == "unsafe-capacity-tenant":
        environment["YFEISTAI_CAPACITY_TENANT_IDS"] = '["tenant-owner","../tenant-foreign"]'
        expected = "capacity_tenant_ids_invalid"
    else:
        environment["WEB_BASE_URL"] = "http://classroom.example.test"
        expected = "base_url_invalid"

    with pytest.raises(module.TenantIsolationProbeError, match=expected):
        module._load_config(
            environment,
            cwd=cwd,
            candidate_loader=lambda _root: _live_candidate(),
        )


def _live_probe_config(module, tmp_path: Path):
    capacity_path, capacity_sha256 = _write_capacity_attestation(tmp_path)
    return SimpleNamespace(
        admin_token=module.SecretStr("secret-platform-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_live_candidate(),
        candidate_root=tmp_path,
        release_run={
            "runId": "run-tenant-isolation",
            "environmentId": "acceptance-tenant-isolation",
        },
        timeout_seconds=600,
        capacity_attestation_path=capacity_path,
        capacity_attestation_sha256=capacity_sha256,
        capacity_tenant_ids=("tenant-owner", "tenant-foreign"),
    )


def _identity_material(module):
    return SimpleNamespace(
        owner_username="isolation-owner",
        owner_password=module.SecretStr("owner-password"),
        foreign_username="isolation-foreign",
        foreign_password=module.SecretStr("foreign-password"),
    )


def test_build_isolation_fixture_creates_complete_owner_resources_through_formal_apis() -> None:
    module = _module()
    owner = _identity(
        module,
        username="isolation-owner",
        user_id="owner-user-id",
        token="owner-session",
    )
    tenant_id = "tenant-owner"
    suffix = hashlib.sha256(owner.username.encode()).hexdigest()[:16]
    run_key = f"isolation-{suffix}"
    course_id = f"course-{suffix}"
    class_id = f"class-{suffix}"
    asset_id = "asset-owner"
    review_id = "review-owner"
    version_id = "version-owner"
    assignment_id = "assignment-owner"
    session_id = "session-owner"
    export_id = "export-owner"
    binding_id = "binding-owner"
    document_ticket = "document-ticket-secret"
    event_ticket = "event-ticket-secret"
    expected_calls = [
        {
            "channel": "owner",
            "method": "POST",
            "path": "/api/v1/teaching/courses",
            "body": {"id": course_id, "title": "Tenant isolation acceptance"},
            "headers": {},
            "statuses": frozenset({201}),
            "response": {"id": course_id, "status": "active"},
        },
        {
            "channel": "owner",
            "method": "PUT",
            "path": f"/api/v1/teaching/courses/{course_id}/generation-policy",
            "body": {
                "allowStudentMicro": False,
                "allowStudentFull": False,
                "allowedContentModes": ["open_creation"],
                "allowWebSearch": False,
                "requireApprovalForRestrictedTopics": True,
                "minorSafetyMode": True,
                "microSceneLimit": 1,
                "fullSceneLimit": 1,
                "dailyStudentUnits": 0,
                "monthlyStudentUnits": 0,
            },
            "headers": {},
            "statuses": frozenset({200}),
            "response": {
                "tenantId": tenant_id,
                "courseId": course_id,
                "updatedBy": owner.user_id,
            },
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/teaching/courses/{course_id}/classes",
            "body": {"id": class_id, "name": "Tenant isolation acceptance"},
            "headers": {},
            "statuses": frozenset({201}),
            "response": {
                "id": class_id,
                "courseId": course_id,
                "status": "active",
            },
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/teaching/classes/{class_id}/enrollments",
            "body": {"userId": owner.user_id},
            "headers": {},
            "statuses": frozenset({201}),
            "response": {
                "classId": class_id,
                "userId": owner.user_id,
                "status": "active",
            },
        },
        {
            "channel": "owner-multipart",
            "method": "POST",
            "path": "/api/v1/teaching/sources/pdf",
            "body": {"courseId": course_id, "classId": class_id},
            "headers": {},
            "statuses": frozenset({201}),
            "response": {
                "bindingId": binding_id,
                "sourceType": "pdf",
                "courseId": course_id,
                "classId": class_id,
                "sha256": "1" * 64,
            },
        },
        {
            "channel": "admin",
            "method": "POST",
            "path": "/api/v1/teaching/generation-quota-grants",
            "body": {"units": 20},
            "headers": {"Idempotency-Key": f"{run_key}-quota"},
            "statuses": frozenset({200}),
            "response": {"tenantId": tenant_id, "units": 20, "balance": 20},
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": "/api/v1/classrooms",
            "body": {
                "title": "Tenant isolation acceptance",
                "courseId": course_id,
                "classId": class_id,
                "objective": "Verify tenant data isolation across all protected layers",
                "gradeBand": "grade-8",
                "audience": "intermediate",
                "durationMinutes": 15,
                "classroomMode": "full",
                "webPolicy": "disabled",
                "mediaPolicy": "text_only",
                "templateId": "first-release-acceptance",
                "templateVersion": "1",
                "knowledgePoints": [
                    {
                        "knowledgePointId": "kp-tenant-isolation",
                        "title": "Tenant isolation",
                        "description": "Verify protected resources stay tenant scoped",
                    }
                ],
                "contentMode": "open_creation",
                "openCreationAcknowledged": True,
                "requestedExports": ["offline_html"],
            },
            "headers": {"Idempotency-Key": f"{run_key}-classroom"},
            "statuses": frozenset({202}),
            "response": {"assetId": asset_id, "ownerId": owner.user_id},
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/classrooms/{asset_id}/confirm-outline",
            "body": None,
            "headers": {},
            "statuses": frozenset({202}),
            "response": {"status": "queued"},
        },
        {
            "channel": "owner",
            "method": "GET",
            "path": f"/api/v1/classrooms/{asset_id}",
            "body": None,
            "headers": {},
            "statuses": frozenset({200}),
            "response": {
                "status": "succeeded",
                "lifecycleState": "editing",
                "document": {"schemaVersion": 1},
            },
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/classrooms/{asset_id}/validate",
            "body": None,
            "headers": {},
            "statuses": frozenset({200}),
            "response": {"validationReport": {"valid": True}},
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/classrooms/{asset_id}/submit",
            "body": {"scope": "class", "classId": class_id},
            "headers": {"Idempotency-Key": f"{run_key}-review"},
            "statuses": frozenset({201}),
            "response": {"id": review_id, "assetId": asset_id, "status": "pending"},
        },
        {
            "channel": "admin",
            "method": "POST",
            "path": f"/api/v1/classroom-reviews/{review_id}/approve",
            "body": {"comment": "First-release tenant isolation acceptance"},
            "headers": {},
            "statuses": frozenset({200}),
            "response": {"id": review_id, "status": "approved"},
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/classrooms/{asset_id}/publish",
            "body": {"scope": "class", "classId": class_id},
            "headers": {"Idempotency-Key": f"{run_key}-publish"},
            "statuses": frozenset({201}),
            "response": {
                "versionId": version_id,
                "assetId": asset_id,
                "classId": class_id,
            },
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/classroom-versions/{version_id}/assign",
            "body": {"classId": class_id},
            "headers": {"Idempotency-Key": f"{run_key}-assignment"},
            "statuses": frozenset({201}),
            "response": {
                "assignmentId": assignment_id,
                "versionId": version_id,
                "classId": class_id,
            },
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": "/api/v1/classroom-sessions",
            "body": {"assignment_id": assignment_id},
            "headers": {},
            "statuses": frozenset({201}),
            "response": {
                "id": session_id,
                "tenant_id": tenant_id,
                "user_id": owner.user_id,
                "classroom_version_id": version_id,
                "assignment_id": assignment_id,
            },
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/classroom-sessions/{session_id}/read-ticket",
            "body": {
                "action": "classroom.document.read",
                "resource_id": version_id,
            },
            "headers": {},
            "statuses": frozenset({200}),
            "response": {"ticket": document_ticket},
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/classroom-sessions/{session_id}/event-ticket",
            "body": None,
            "headers": {},
            "statuses": frozenset({200}),
            "response": {"ticket": event_ticket},
        },
        {
            "channel": "owner",
            "method": "POST",
            "path": f"/api/v1/classroom-versions/{version_id}/exports",
            "body": {"format": "offline_html"},
            "headers": {"Idempotency-Key": f"{run_key}-offline-html"},
            "statuses": frozenset({202}),
            "response": {
                "job_id": export_id,
                "status": "queued",
                "download_ready": False,
            },
        },
        {
            "channel": "owner",
            "method": "GET",
            "path": f"/api/v1/classroom-exports/{export_id}",
            "body": None,
            "headers": {},
            "statuses": frozenset({200}),
            "response": {
                "job_id": export_id,
                "status": "succeeded",
                "download_ready": True,
            },
        },
    ]
    observed_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    def consume(
        channel: str,
        method: str,
        path: str,
        *,
        body: object,
        headers: object,
        statuses: object,
    ) -> dict[str, object]:
        expected = expected_calls[len(observed_calls)]
        actual = {
            "channel": channel,
            "method": method,
            "path": path,
            "body": body,
            "headers": headers or {},
            "statuses": statuses,
        }
        assert actual == {key: expected[key] for key in actual}
        observed_calls.append(actual)
        return expected["response"]

    class Api:
        async def tenant_identity_json(
            self,
            method,
            path,
            *,
            identity,
            tenant_id,
            json_body=None,
            headers=None,
            expected_statuses=frozenset({200, 201, 202}),
            **_kwargs,
        ):
            assert identity is owner
            assert tenant_id == "tenant-owner"
            return consume(
                "owner",
                method,
                path,
                body=json_body,
                headers=headers,
                statuses=expected_statuses,
            )

        async def tenant_identity_multipart_json(
            self,
            method,
            path,
            *,
            identity,
            tenant_id,
            data,
            files,
            headers=None,
            expected_statuses=frozenset({200, 201, 202}),
            **_kwargs,
        ):
            assert identity is owner
            assert tenant_id == "tenant-owner"
            assert set(files) == {"file"}
            filename, payload, media_type = files["file"]
            assert filename == "tenant-isolation.pdf"
            assert media_type == "application/pdf"
            assert isinstance(payload, bytes) and payload.startswith(b"%PDF-")
            return consume(
                "owner-multipart",
                method,
                path,
                body=data,
                headers=headers,
                statuses=expected_statuses,
            )

        async def tenant_admin_json(
            self,
            method,
            path,
            *,
            tenant_id,
            json_body=None,
            headers=None,
            expected_statuses=frozenset({200, 201, 202}),
            **_kwargs,
        ):
            assert tenant_id == "tenant-owner"
            return consume(
                "admin",
                method,
                path,
                body=json_body,
                headers=headers,
                statuses=expected_statuses,
            )

    async def no_wait(seconds: float) -> None:
        sleep_calls.append(seconds)

    module.asyncio = SimpleNamespace(sleep=no_wait)
    fixture = asyncio.run(
        module._build_isolation_fixture(
            Api(),
            config=SimpleNamespace(timeout_seconds=30),
            owner_identity=owner,
            owner_tenant_id=tenant_id,
        )
    )

    assert len(observed_calls) == len(expected_calls) == 19
    assert [call["path"] for call in observed_calls] == [call["path"] for call in expected_calls]
    assert sleep_calls == [0.25]
    assert fixture.targets == {
        "database": {"courseId": course_id},
        "objects": {
            "bindingId": binding_id,
            "classroomVersionId": version_id,
        },
        "exports": {"exportId": export_id},
        "events": {
            "sessionId": session_id,
            "eventId": f"event-{suffix}",
            "classroomVersionId": version_id,
        },
    }
    assert fixture.document_ticket.get_secret_value() == document_ticket
    assert fixture.event_ticket.get_secret_value() == event_ticket
    assert isinstance(fixture.document_ticket, module.SecretStr)
    assert isinstance(fixture.event_ticket, module.SecretStr)
    serialized_targets = json.dumps(fixture.targets, sort_keys=True)
    assert document_ticket not in serialized_targets
    assert event_ticket not in serialized_targets
    assert "objectKey" not in serialized_targets
    public_ids = {
        course_id,
        binding_id,
        version_id,
        export_id,
        session_id,
        fixture.targets["events"]["eventId"],
    }
    assert len(public_ids) == 6


def test_run_probe_uses_formal_user_membership_login_and_role_scoped_fixture_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)
    material = _identity_material(module)
    events: list[object] = []
    created_ids = {
        material.owner_username: "owner-user-id",
        material.foreign_username: "foreign-user-id",
    }

    class Api:
        async def __aenter__(self):
            events.append("api-enter")
            return self

        async def __aexit__(self, *_args):
            events.append("api-exit")

        async def admin_list_json(self, method, path, **_kwargs):
            assert method == "GET"
            assert path == "/api/v1/auth/users"
            events.append("identity-preflight")
            return []

        async def admin_json(
            self,
            method,
            path,
            *,
            json_body=None,
            expected_statuses=frozenset({200, 201, 202}),
            **_kwargs,
        ):
            assert method == "POST"
            assert path == "/api/v1/auth/users"
            assert expected_statuses == frozenset({201})
            assert set(json_body) == {"username", "password"}
            username = json_body["username"]
            assert (
                json_body["password"]
                == {
                    material.owner_username: "owner-password",
                    material.foreign_username: "foreign-password",
                }[username]
            )
            events.append(("create", username))
            return {
                "ok": True,
                "user_id": created_ids[username],
                "username": username,
                "role": "user",
                "is_admin": False,
            }

        async def tenant_admin_json(
            self,
            method,
            path,
            *,
            tenant_id,
            json_body=None,
            **_kwargs,
        ):
            assert method == "POST"
            assert path == f"/api/v1/tenants/{tenant_id}/members"
            expected_user_id = json_body["user_id"]
            roles = (
                ("student",)
                if tenant_id == "tenant-foreign" and expected_user_id == "owner-user-id"
                else ("platform_admin", "teacher")
            )
            expected_grants = [
                {"role": role, "scope_type": "tenant", "scope_id": tenant_id} for role in roles
            ]
            assert json_body == {
                "user_id": expected_user_id,
                "grants": expected_grants,
            }
            events.append(("membership", tenant_id, expected_user_id))
            return {
                "tenant_id": tenant_id,
                "user_id": expected_user_id,
                "roles": list(roles),
                "grants": expected_grants,
            }

        async def login_identity(self, username, password):
            assert (
                password.get_secret_value()
                == {
                    material.owner_username: "owner-password",
                    material.foreign_username: "foreign-password",
                }[username]
            )
            events.append(("login", username))
            return module.IdentityCredential(
                username,
                created_ids[username],
                module.SecretStr(f"{username}-session"),
            )

    def api_factory(base_url, admin_token, *, timeout_seconds, **_kwargs):
        assert base_url == config.base_url
        assert admin_token == "secret-platform-admin-token"
        assert timeout_seconds == config.timeout_seconds
        return Api()

    async def resolve_pair(api, candidates):
        assert isinstance(api, Api)
        assert candidates == ("tenant-owner", "tenant-foreign")
        events.append("capacity-recheck")
        return candidates

    fixture = SimpleNamespace(
        targets=_targets(),
        document_ticket=module.SecretStr("owner-document-ticket"),
        event_ticket=module.SecretStr("owner-event-ticket"),
    )

    async def build_fixture(
        api,
        *,
        config,
        owner_identity,
        owner_tenant_id,
        cleanup_state,
        persist_cleanup_state,
    ):
        assert isinstance(api, Api)
        assert config is not None
        assert owner_identity.username == material.owner_username
        assert owner_identity.user_id == "owner-user-id"
        assert owner_identity.token.get_secret_value() == "isolation-owner-session"
        assert owner_tenant_id == "tenant-owner"
        assert cleanup_state == module.IsolationCleanupState(
            tenant_id="tenant-owner",
            user_id="owner-user-id",
        )
        assert callable(persist_cleanup_state)
        events.append("role-scoped-fixture")
        return fixture

    observations = [{"strict": "observation-matrix"}]

    async def verify_targets(api, **kwargs):
        assert isinstance(api, Api)
        assert set(kwargs) == {
            "owner_identity",
            "owner_tenant_id",
            "foreign_identity",
            "foreign_tenant_id",
            "targets",
            "document_ticket",
            "event_ticket",
        }
        assert kwargs["owner_identity"].username == material.owner_username
        assert kwargs["owner_identity"].user_id == "owner-user-id"
        assert kwargs["owner_identity"].token.get_secret_value() == ("isolation-owner-session")
        assert kwargs["foreign_identity"].username == material.foreign_username
        assert kwargs["foreign_identity"].user_id == "foreign-user-id"
        assert kwargs["foreign_identity"].token.get_secret_value() == ("isolation-foreign-session")
        assert kwargs["owner_tenant_id"] == "tenant-owner"
        assert kwargs["foreign_tenant_id"] == "tenant-foreign"
        assert kwargs["targets"] == _targets()
        assert kwargs["document_ticket"] is fixture.document_ticket
        assert kwargs["event_ticket"] is fixture.event_ticket
        events.append("verify")
        return observations

    parsed_report: dict[str, object] = {}

    def canonical(report):
        assert report == parsed_report
        events.append("canonical")
        return b"canonical-tenant-isolation-report\n"

    def parse_report(body, **kwargs):
        assert body == b"canonical-tenant-isolation-report\n"
        assert kwargs["candidate"] == config.candidate
        assert kwargs["release_run"] == config.release_run
        assert kwargs["expected_base_url"] == config.base_url
        assert kwargs["expected_capacity_report_sha256"] == (config.capacity_attestation_sha256)
        assert kwargs["expected_capacity_tenant_ids"] == (
            "tenant-owner",
            "tenant-foreign",
        )
        assert set(kwargs["forbidden_secret_values"]) == {
            b"secret-platform-admin-token",
            b"owner-password",
            b"foreign-password",
            b"isolation-owner-session",
            b"isolation-foreign-session",
            b"owner-document-ticket",
            b"owner-event-ticket",
        }
        events.append("parse")
        return parsed_report

    def derive(report):
        assert report is parsed_report
        events.append("derive")
        return {
            "databaseIsolated": True,
            "objectsIsolated": True,
            "exportsIsolated": True,
            "eventsIsolated": True,
        }

    async def cleanup(api, *, username, expected_user_id):
        assert isinstance(api, Api)
        assert expected_user_id == created_ids[username]
        events.append(("cleanup", username, expected_user_id))

    async def cleanup_membership(api, *, tenant_id, expected_user_id):
        assert isinstance(api, Api)
        events.append(("membership-cleanup", tenant_id, expected_user_id))

    def capture_report(report):
        parsed_report.update(report)
        return canonical(report)

    monkeypatch.setattr(module, "TenantIsolationApi", api_factory)
    monkeypatch.setattr(
        module,
        "_identity_material",
        lambda _config, **_kwargs: material,
        raising=False,
    )
    monkeypatch.setattr(module, "resolve_active_tenant_pair", resolve_pair)
    monkeypatch.setattr(module, "_build_isolation_fixture", build_fixture, raising=False)
    monkeypatch.setattr(module, "verify_isolation_targets", verify_targets)
    monkeypatch.setattr(module, "canonical_tenant_isolation_report", capture_report, raising=False)
    monkeypatch.setattr(module, "parse_tenant_isolation_report", parse_report, raising=False)
    monkeypatch.setattr(module, "derive_tenant_isolation_checks", derive)
    monkeypatch.setattr(module, "delete_membership_with_reconciliation", cleanup_membership)
    monkeypatch.setattr(module, "delete_identity_with_reconciliation", cleanup)

    body = asyncio.run(module._run_tenant_isolation_probe(config))

    assert body == b"canonical-tenant-isolation-report\n"
    assert parsed_report == {
        "schemaVersion": 1,
        "producer": "tenant-isolation-probe",
        "candidate": config.candidate,
        "releaseRun": config.release_run,
        "observedAt": parsed_report["observedAt"],
        "baseUrl": config.base_url,
        "capacityProof": {
            "reportSha256": config.capacity_attestation_sha256,
            "tenantIds": ["tenant-owner", "tenant-foreign"],
        },
        "principals": [
            {
                "tenantId": "tenant-owner",
                "actorId": "owner-user-id",
                "role": "user",
                "membershipStatus": "active",
            },
            {
                "tenantId": "tenant-foreign",
                "actorId": "foreign-user-id",
                "role": "user",
                "membershipStatus": "active",
            },
        ],
        "crossTenantPrincipal": {
            "tenantId": "tenant-foreign",
            "actorId": "owner-user-id",
            "role": "student",
            "membershipStatus": "active",
        },
        "observations": observations,
    }
    assert isinstance(parsed_report["observedAt"], str)
    assert parsed_report["observedAt"].endswith("Z")
    assert events == [
        "api-enter",
        "identity-preflight",
        "identity-preflight",
        ("create", "isolation-owner"),
        ("create", "isolation-foreign"),
        ("membership", "tenant-owner", "owner-user-id"),
        ("membership", "tenant-foreign", "foreign-user-id"),
        ("membership", "tenant-foreign", "owner-user-id"),
        ("login", "isolation-owner"),
        ("login", "isolation-foreign"),
        "capacity-recheck",
        "role-scoped-fixture",
        "verify",
        "canonical",
        "parse",
        "derive",
        ("membership-cleanup", "tenant-foreign", "foreign-user-id"),
        ("cleanup", "isolation-foreign", "foreign-user-id"),
        ("membership-cleanup", "tenant-foreign", "owner-user-id"),
        ("membership-cleanup", "tenant-owner", "owner-user-id"),
        ("cleanup", "isolation-owner", "owner-user-id"),
        "api-exit",
    ]


def test_run_probe_reports_primary_and_cleanup_failure_without_masking_primary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = _live_probe_config(module, tmp_path)
    material = _identity_material(module)
    cleanup_calls: list[tuple[str, str]] = []

    class Api:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def admin_list_json(self, _method, _path, **_kwargs):
            return []

        async def admin_json(self, _method, _path, *, json_body=None, **_kwargs):
            username = json_body["username"]
            return {
                "ok": True,
                "user_id": f"{username}-id",
                "username": username,
                "role": "user",
                "is_admin": False,
            }

        async def tenant_admin_json(
            self,
            _method,
            _path,
            *,
            tenant_id,
            json_body=None,
            **_kwargs,
        ):
            roles = (
                ("student",)
                if tenant_id == "tenant-foreign" and json_body["user_id"] == "isolation-owner-id"
                else ("platform_admin", "teacher")
            )
            grants = [
                {"role": role, "scope_type": "tenant", "scope_id": tenant_id} for role in roles
            ]
            return {
                "tenant_id": tenant_id,
                "user_id": json_body["user_id"],
                "roles": list(roles),
                "grants": grants,
            }

        async def login_identity(self, username, _password):
            return module.IdentityCredential(
                username,
                f"{username}-id",
                module.SecretStr(f"{username}-session"),
            )

    async def fixture_failure(*_args, **_kwargs):
        raise module.TenantIsolationProbeError("fixture_creation_failed")

    async def cleanup_failure(_api, *, username, expected_user_id):
        cleanup_calls.append((username, expected_user_id))
        raise module.TenantIsolationProbeError("identity_cleanup_failed")

    async def cleanup_membership(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module, "TenantIsolationApi", lambda *_args, **_kwargs: Api())
    monkeypatch.setattr(
        module,
        "_identity_material",
        lambda _config, **_kwargs: material,
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "resolve_active_tenant_pair",
        lambda _api, candidates: _async_value(tuple(candidates)),
    )
    monkeypatch.setattr(module, "_build_isolation_fixture", fixture_failure, raising=False)
    monkeypatch.setattr(module, "delete_membership_with_reconciliation", cleanup_membership)
    monkeypatch.setattr(module, "delete_identity_with_reconciliation", cleanup_failure)

    with pytest.raises(
        module.TenantIsolationProbeError,
        match="tenant_isolation_probe_and_cleanup_failed",
    ) as caught:
        asyncio.run(module._run_tenant_isolation_probe(config))

    assert isinstance(caught.value.__cause__, module.TenantIsolationProbeError)
    assert str(caught.value.__cause__) == "fixture_creation_failed"
    assert cleanup_calls == [("isolation-foreign", "isolation-foreign-id")]
