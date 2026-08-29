"""Live tenant-isolation probe for one first-release candidate."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from http.cookies import SimpleCookie
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any, Callable, NamedTuple
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr
from pypdf import PdfWriter

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from render_platform_compose import validate_image_lock_bindings  # noqa: E402
from tenant_isolation_contract import (  # noqa: E402
    TENANT_ISOLATION_PRODUCER,
    TENANT_ISOLATION_SCHEMA_VERSION,
    canonical_tenant_isolation_report,
    derive_tenant_isolation_checks,
    parse_tenant_isolation_report,
)
from verify_classroom_release import (  # noqa: E402
    read_capacity_profile_attestation_artifact,
)

_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_RESPONSE_BYTES = 128 * 1024 * 1024
_MAX_CLEANUP_RECOVERY_BYTES = 64 * 1024
_CLEANUP_RECOVERY_NAME = ".tenant-isolation-cleanup.json"
_CLEANUP_RECOVERY_SCHEMA_VERSION = 1
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "objectkey",
    "password",
    "secret",
    "ticket",
    "token",
)
_TARGET_TYPES = {
    "database": "course",
    "objects": "classroom_source_document",
    "exports": "classroom_export",
    "events": "learning_session_event",
}


class TenantIsolationProbeError(RuntimeError):
    """Fail-closed tenant-isolation probe error."""


class IdentityCredential(NamedTuple):
    username: str
    user_id: str
    token: SecretStr


class IdentityMaterial(NamedTuple):
    owner_username: str
    owner_password: SecretStr
    foreign_username: str
    foreign_password: SecretStr


class IsolationFixture(NamedTuple):
    targets: dict[str, object]
    document_ticket: SecretStr
    event_ticket: SecretStr


@dataclass(slots=True)
class IsolationCleanupState:
    tenant_id: str
    user_id: str
    class_id: str | None = None
    enrollment_active: bool = False
    source_binding_id: str | None = None
    learning_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class IsolationCleanupRecovery:
    attempt_id: str
    identity_intents: tuple[str, ...]
    cleanup_state: IsolationCleanupState | None
    memberships: tuple[tuple[str, str], ...]
    created: tuple[tuple[str, str], ...]


class ProbeConfig:
    __slots__ = (
        "admin_token",
        "base_url",
        "candidate",
        "candidate_root",
        "capacity_attestation_path",
        "capacity_attestation_sha256",
        "capacity_tenant_ids",
        "release_run",
        "timeout_seconds",
    )

    def __init__(
        self,
        *,
        admin_token: SecretStr,
        base_url: str,
        candidate: Mapping[str, object],
        candidate_root: Path,
        capacity_attestation_path: Path,
        capacity_attestation_sha256: str,
        capacity_tenant_ids: Sequence[str],
        release_run: Mapping[str, str],
        timeout_seconds: int,
    ) -> None:
        self.admin_token = admin_token
        self.base_url = base_url
        self.candidate = dict(candidate)
        self.candidate_root = candidate_root
        self.capacity_attestation_path = capacity_attestation_path
        self.capacity_attestation_sha256 = capacity_attestation_sha256
        self.capacity_tenant_ids = tuple(capacity_tenant_ids)
        self.release_run = dict(release_run)
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            "ProbeConfig(admin_token=SecretStr('**********'), "
            f"base_url={self.base_url!r}, candidate_root={self.candidate_root!r}, "
            f"capacity_attestation_path={self.capacity_attestation_path!r}, "
            f"capacity_attestation_sha256={self.capacity_attestation_sha256!r}, "
            f"capacity_tenant_ids={self.capacity_tenant_ids!r}, "
            f"release_run={self.release_run!r}, timeout_seconds={self.timeout_seconds!r})"
        )


CandidateLoader = Callable[[Path], Mapping[str, object]]


def _default_candidate_loader(candidate_root: Path) -> dict[str, object]:
    lock = validate_image_lock_bindings(
        candidate_root / "deploy" / "image-lock.json",
        compose_paths=(
            candidate_root / "docker-compose.platform.yml",
            candidate_root / "docker-compose.data-plane.yml",
        ),
        require_candidate=True,
    )
    candidate = lock.get("candidate")
    if not isinstance(candidate, dict):
        raise TenantIsolationProbeError("candidate_invalid")
    return json.loads(json.dumps(candidate))


def _valid_base_url(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.rstrip("/"):
        return False
    parsed = urlsplit(value)
    if parsed.scheme == "http":
        hostname = parsed.hostname
        if hostname != "localhost":
            try:
                if hostname is None or not ipaddress.ip_address(hostname).is_loopback:
                    return False
            except ValueError:
                return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )


def _public_id(value: object, error: str = "tenant_isolation_target_invalid") -> str:
    if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
        raise TenantIsolationProbeError(error)
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TenantIsolationProbeError("tenant_isolation_payload_invalid") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class TenantIsolationApi:
    """Redirect-free HTTP boundary separating admin and role sessions."""

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        timeout = httpx.Timeout(timeout_seconds)
        self._base_url = base_url
        self._transport = transport
        self._admin_client = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )
        self._identity_client = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> TenantIsolationApi:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._admin_client.aclose()
        await self._identity_client.aclose()

    @staticmethod
    def _path(path: str) -> str:
        if not path.startswith("/api/v1/") or path.startswith("//"):
            raise TenantIsolationProbeError("request_path_invalid")
        return path

    @staticmethod
    def _json_response(
        response: httpx.Response,
        *,
        expected_statuses: frozenset[int],
    ) -> dict[str, Any]:
        if response.status_code not in expected_statuses:
            raise TenantIsolationProbeError("candidate_request_rejected")
        try:
            body = response.json()
        except (UnicodeError, ValueError) as exc:
            raise TenantIsolationProbeError("candidate_response_invalid") from exc
        if not isinstance(body, dict):
            raise TenantIsolationProbeError("candidate_response_invalid")
        return body

    async def _bounded_response(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: object,
    ) -> httpx.Response:
        request = client.build_request(method, self._path(path), **kwargs)
        try:
            response = await client.send(request, stream=True)
            try:
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None:
                    try:
                        parsed_size = int(declared_size)
                        if parsed_size < 0:
                            raise ValueError
                        if parsed_size > _MAX_RESPONSE_BYTES:
                            raise TenantIsolationProbeError("candidate_response_too_large")
                    except ValueError as exc:
                        raise TenantIsolationProbeError("candidate_response_invalid") from exc
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise TenantIsolationProbeError("candidate_response_too_large")
                    body.extend(chunk)
                headers = [
                    (name, value)
                    for name, value in response.headers.raw
                    if name.lower()
                    not in {b"content-encoding", b"content-length", b"transfer-encoding"}
                ]
                return httpx.Response(
                    response.status_code,
                    headers=headers,
                    content=bytes(body),
                    request=request,
                )
            finally:
                await response.aclose()
        except TenantIsolationProbeError:
            raise
        except httpx.HTTPError as exc:
            raise TenantIsolationProbeError("candidate_request_failed") from exc

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, object] = {"headers": dict(headers or {})}
        if json_body is not None:
            kwargs["json"] = json_body
        return await self._bounded_response(client, method, path, **kwargs)

    async def admin_response(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        return await self._request(
            self._admin_client,
            method,
            path,
            headers=headers,
            json_body=json_body,
        )

    async def admin_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        response = await self.admin_response(
            method,
            path,
            headers=headers,
            json_body=json_body,
        )
        return self._json_response(response, expected_statuses=expected_statuses)

    async def admin_list_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        expected_statuses: frozenset[int] = frozenset({200}),
    ) -> list[Any]:
        response = await self.admin_response(method, path, headers=headers)
        if response.status_code not in expected_statuses:
            raise TenantIsolationProbeError("candidate_request_rejected")
        try:
            body = response.json()
        except (UnicodeError, ValueError) as exc:
            raise TenantIsolationProbeError("candidate_response_invalid") from exc
        if not isinstance(body, list):
            raise TenantIsolationProbeError("candidate_response_invalid")
        return body

    @staticmethod
    def _tenant_admin_headers(
        tenant_id: str,
        headers: Mapping[str, str] | None,
    ) -> dict[str, str]:
        tenant_id = _public_id(tenant_id, "tenant_id_invalid")
        bound = dict(headers or {})
        normalized = httpx.Headers(bound)
        supplied = normalized.get("X-Tenant-ID")
        if supplied is not None and supplied != tenant_id:
            raise TenantIsolationProbeError("tenant_binding_conflict")
        cookies = SimpleCookie()
        try:
            cookies.load(normalized.get("Cookie", ""))
        except Exception as exc:
            raise TenantIsolationProbeError("tenant_binding_conflict") from exc
        supplied_cookie_tenant = cookies.get("dt_tenant")
        if supplied_cookie_tenant is not None and supplied_cookie_tenant.value != tenant_id:
            raise TenantIsolationProbeError("tenant_binding_conflict")
        for key in tuple(bound):
            if key.lower() in {"authorization", "cookie", "x-tenant-id"}:
                bound.pop(key)
        bound["X-Tenant-ID"] = tenant_id
        bound["Cookie"] = f"dt_tenant={tenant_id}"
        return bound

    async def tenant_admin_json(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        return await self.admin_json(
            method,
            path,
            headers=self._tenant_admin_headers(tenant_id, headers),
            json_body=json_body,
            expected_statuses=expected_statuses,
        )

    async def tenant_admin_response(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        return await self.admin_response(
            method,
            path,
            headers=self._tenant_admin_headers(tenant_id, headers),
            json_body=json_body,
        )

    @staticmethod
    def _identity_headers(
        identity: IdentityCredential,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        tenant_id = _public_id(tenant_id, "tenant_id_invalid")
        supplied = httpx.Headers(dict(headers or {}))
        supplied_tenant = supplied.get("X-Tenant-ID")
        if supplied_tenant is not None and supplied_tenant != tenant_id:
            raise TenantIsolationProbeError("tenant_binding_conflict")
        cookies = SimpleCookie()
        try:
            cookies.load(supplied.get("Cookie", ""))
        except Exception as exc:
            raise TenantIsolationProbeError("tenant_binding_conflict") from exc
        supplied_cookie_tenant = cookies.get("dt_tenant")
        if supplied_cookie_tenant is not None and supplied_cookie_tenant.value != tenant_id:
            raise TenantIsolationProbeError("tenant_binding_conflict")
        bound = dict(headers or {})
        for key in tuple(bound):
            if key.lower() in {"authorization", "cookie", "x-tenant-id"}:
                bound.pop(key)
        bound["X-Tenant-ID"] = tenant_id
        bound["Cookie"] = f"dt_token={identity.token.get_secret_value()}; dt_tenant={tenant_id}"
        return bound

    async def login_identity(
        self,
        username: str,
        password: str | SecretStr,
    ) -> IdentityCredential:
        username = _public_id(username, "identity_invalid")
        secret = password.get_secret_value() if isinstance(password, SecretStr) else password
        if not isinstance(secret, str) or not secret:
            raise TenantIsolationProbeError("identity_login_failed")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                follow_redirects=False,
                timeout=self._identity_client.timeout,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await self._bounded_response(
                    client,
                    "POST",
                    "/api/v1/auth/login",
                    json={"username": username, "password": secret},
                )
        except TenantIsolationProbeError as exc:
            raise TenantIsolationProbeError("identity_login_failed") from exc
        body = self._json_response(response, expected_statuses=frozenset({200}))
        token = response.cookies.get("dt_token")
        user_id = body.get("user_id")
        if (
            body.get("ok") is not True
            or body.get("username") != username
            or body.get("role") != "user"
            or body.get("is_admin") is not False
            or not isinstance(user_id, str)
            or _PUBLIC_ID.fullmatch(user_id) is None
            or not isinstance(token, str)
            or not token
        ):
            raise TenantIsolationProbeError("identity_login_failed")
        return IdentityCredential(username, user_id, SecretStr(token))

    async def tenant_identity_response(
        self,
        method: str,
        path: str,
        *,
        identity: IdentityCredential,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        return await self._request(
            self._identity_client,
            method,
            path,
            headers=self._identity_headers(identity, tenant_id, headers),
            json_body=json_body,
        )

    async def tenant_identity_json(
        self,
        method: str,
        path: str,
        *,
        identity: IdentityCredential,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        response = await self.tenant_identity_response(
            method,
            path,
            identity=identity,
            tenant_id=tenant_id,
            headers=headers,
            json_body=json_body,
        )
        return self._json_response(response, expected_statuses=expected_statuses)

    async def tenant_identity_multipart_json(
        self,
        method: str,
        path: str,
        *,
        identity: IdentityCredential,
        tenant_id: str,
        data: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        response = await self._bounded_response(
            self._identity_client,
            method,
            path,
            headers=self._identity_headers(identity, tenant_id),
            data=dict(data),
            files=dict(files),
        )
        return self._json_response(response, expected_statuses=expected_statuses)


async def resolve_active_tenant_pair(
    api: TenantIsolationApi,
    candidates: Sequence[str],
) -> tuple[str, str]:
    if (
        isinstance(candidates, (str, bytes))
        or len(candidates) != 2
        or candidates[0] == candidates[1]
    ):
        raise TenantIsolationProbeError("tenant_pair_invalid")
    tenant_ids = tuple(_public_id(item, "tenant_pair_invalid") for item in candidates)
    for tenant_id in tenant_ids:
        body = await api.tenant_admin_json(
            "GET",
            f"/api/v1/tenants/{tenant_id}/provisioning",
            tenant_id=tenant_id,
            expected_statuses=frozenset({200}),
        )
        if (
            body.get("tenant_id") != tenant_id
            or body.get("status") != "active"
            or body.get("job_status") != "completed"
        ):
            raise TenantIsolationProbeError("tenant_pair_inactive")
    return tenant_ids[0], tenant_ids[1]


def _list_target_omitted(response: httpx.Response, target_id: str, field: str) -> bool:
    try:
        body = response.json()
    except (UnicodeError, ValueError) as exc:
        raise TenantIsolationProbeError("tenant_isolation_failed") from exc
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise TenantIsolationProbeError("tenant_isolation_failed")
    observed: list[str] = []
    for item in body["items"]:
        if not isinstance(item, dict) or not isinstance(item.get(field), str):
            raise TenantIsolationProbeError("tenant_isolation_failed")
        observed.append(item[field])
    return target_id not in observed


def _state_bytes(name: str, response: httpx.Response) -> bytes:
    if "document" in name or "download" in name:
        return response.content
    try:
        value = response.json()
    except (UnicodeError, ValueError) as exc:
        raise TenantIsolationProbeError("tenant_isolation_failed") from exc
    if not isinstance(value, dict):
        raise TenantIsolationProbeError("tenant_isolation_failed")
    if "projection" in name:
        value = dict(value)
        value.pop("projectionLagSeconds", None)
    return _canonical_json(value)


def _operation(
    *,
    name: str,
    phase: str,
    method: str,
    path: str,
    tenant_id: str,
    actor_id: str,
    target_id: str,
    response: httpx.Response,
    kind: str,
    request_body: object | None = None,
) -> dict[str, object]:
    state_sha256 = None
    target_omitted = None
    if kind in {"owner-state", "owner-list", "foreign-list"}:
        state_sha256 = _sha256(_state_bytes(name, response))
    if kind in {"owner-list", "foreign-list"}:
        list_field = "id" if name == "foreign-list" else "bindingId"
        target_omitted = _list_target_omitted(response, target_id, list_field)
    error_code = None
    if kind == "foreign-deny":
        error_code = {403: "forbidden", 404: "not_found"}.get(response.status_code)
    return {
        "name": name,
        "phase": phase,
        "method": method,
        "path": path,
        "tenantId": tenant_id,
        "actorId": actor_id,
        "statusCode": response.status_code,
        "observedTargetId": target_id,
        "requestSha256": _sha256(_canonical_json(request_body))
        if request_body is not None
        else None,
        "stateSha256": state_sha256,
        "errorCode": error_code,
        "targetOmitted": target_omitted,
    }


async def _observe(
    api: TenantIsolationApi,
    *,
    name: str,
    phase: str,
    method: str,
    path: str,
    tenant_id: str,
    identity: IdentityCredential,
    target_id: str,
    kind: str,
    headers: Mapping[str, str] | None = None,
    request_body: object | None = None,
) -> dict[str, object]:
    response = await api.tenant_identity_response(
        method,
        path,
        identity=identity,
        tenant_id=tenant_id,
        headers=headers,
        json_body=request_body,
    )
    return _operation(
        name=name,
        phase=phase,
        method=method,
        path=path,
        tenant_id=tenant_id,
        actor_id=identity.user_id,
        target_id=target_id,
        response=response,
        kind=kind,
        request_body=request_body,
    )


def _target_ids(targets: Mapping[str, object], layer: str) -> dict[str, str]:
    raw = targets.get(layer)
    expected = {
        "database": ("courseId",),
        "objects": ("bindingId", "classroomVersionId"),
        "exports": ("exportId",),
        "events": ("sessionId", "eventId", "classroomVersionId"),
    }[layer]
    if not isinstance(raw, dict) or set(raw) != set(expected):
        raise TenantIsolationProbeError("tenant_isolation_target_invalid")
    return {key: _public_id(raw[key]) for key in expected}


def _observation(
    sequence: int,
    layer: str,
    resources: Mapping[str, str],
    owner_tenant_id: str,
    owner_actor_id: str,
    operations: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "layer": layer,
        "target": {
            "targetType": _TARGET_TYPES[layer],
            "resourceIds": dict(resources),
            "ownerTenantId": owner_tenant_id,
            "ownerActorId": owner_actor_id,
        },
        "operations": operations,
    }


async def verify_isolation_targets(
    api: TenantIsolationApi,
    *,
    owner_identity: IdentityCredential,
    owner_tenant_id: str,
    foreign_identity: IdentityCredential,
    foreign_tenant_id: str,
    targets: Mapping[str, object],
    document_ticket: SecretStr,
    event_ticket: SecretStr,
) -> list[dict[str, object]]:
    owner_tenant_id = _public_id(owner_tenant_id, "tenant_pair_invalid")
    foreign_tenant_id = _public_id(foreign_tenant_id, "tenant_pair_invalid")
    if owner_tenant_id == foreign_tenant_id or owner_identity.user_id == foreign_identity.user_id:
        raise TenantIsolationProbeError("tenant_pair_invalid")

    database = _target_ids(targets, "database")
    objects = _target_ids(targets, "objects")
    exports = _target_ids(targets, "exports")
    events = _target_ids(targets, "events")
    course_id = database["courseId"]
    policy_path = f"/api/v1/teaching/courses/{course_id}/generation-policy"
    policy_write = {
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
    }

    database_operations = [
        await _observe(
            api,
            name="owner-policy-before",
            phase="owner-before",
            method="GET",
            path=policy_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=course_id,
            kind="owner-state",
        ),
        await _observe(
            api,
            name="foreign-list",
            phase="foreign-check",
            method="GET",
            path="/api/v1/teaching/courses",
            tenant_id=foreign_tenant_id,
            identity=owner_identity,
            target_id=course_id,
            kind="foreign-list",
        ),
        await _observe(
            api,
            name="foreign-read",
            phase="foreign-check",
            method="GET",
            path=policy_path,
            tenant_id=foreign_tenant_id,
            identity=foreign_identity,
            target_id=course_id,
            kind="foreign-deny",
        ),
        await _observe(
            api,
            name="foreign-write",
            phase="foreign-check",
            method="PUT",
            path=policy_path,
            tenant_id=foreign_tenant_id,
            identity=foreign_identity,
            target_id=course_id,
            kind="foreign-deny",
            request_body=policy_write,
        ),
        await _observe(
            api,
            name="owner-policy-after",
            phase="owner-after",
            method="GET",
            path=policy_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=course_id,
            kind="owner-state",
        ),
    ]

    binding_id = objects["bindingId"]
    version_id = objects["classroomVersionId"]
    document_path = f"/api/v1/classroom-versions/{version_id}/document"
    document_headers = {"X-Classroom-Ticket": document_ticket.get_secret_value()}
    object_operations = [
        await _observe(
            api,
            name="owner-source-list-before",
            phase="owner-before",
            method="GET",
            path="/api/v1/teaching/sources",
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=binding_id,
            kind="owner-list",
        ),
        await _observe(
            api,
            name="owner-document-before",
            phase="owner-before",
            method="GET",
            path=document_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=version_id,
            kind="owner-state",
            headers=document_headers,
        ),
        await _observe(
            api,
            name="foreign-source-list",
            phase="foreign-check",
            method="GET",
            path="/api/v1/teaching/sources",
            tenant_id=foreign_tenant_id,
            identity=foreign_identity,
            target_id=binding_id,
            kind="foreign-list",
        ),
        await _observe(
            api,
            name="foreign-document-read",
            phase="foreign-check",
            method="GET",
            path=document_path,
            tenant_id=foreign_tenant_id,
            identity=foreign_identity,
            target_id=version_id,
            kind="foreign-deny",
            headers=document_headers,
        ),
        await _observe(
            api,
            name="foreign-source-delete",
            phase="foreign-check",
            method="DELETE",
            path=f"/api/v1/teaching/sources/{binding_id}",
            tenant_id=foreign_tenant_id,
            identity=foreign_identity,
            target_id=binding_id,
            kind="foreign-deny",
        ),
        await _observe(
            api,
            name="owner-source-list-after",
            phase="owner-after",
            method="GET",
            path="/api/v1/teaching/sources",
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=binding_id,
            kind="owner-list",
        ),
        await _observe(
            api,
            name="owner-document-after",
            phase="owner-after",
            method="GET",
            path=document_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=version_id,
            kind="owner-state",
            headers=document_headers,
        ),
    ]

    export_id = exports["exportId"]
    status_path = f"/api/v1/classroom-exports/{export_id}"
    download_path = f"{status_path}/download"
    export_operations = [
        await _observe(
            api,
            name="owner-status-before",
            phase="owner-before",
            method="GET",
            path=status_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=export_id,
            kind="owner-state",
        ),
        await _observe(
            api,
            name="owner-download-before",
            phase="owner-before",
            method="GET",
            path=download_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=export_id,
            kind="owner-state",
        ),
        await _observe(
            api,
            name="foreign-status-read",
            phase="foreign-check",
            method="GET",
            path=status_path,
            tenant_id=foreign_tenant_id,
            identity=foreign_identity,
            target_id=export_id,
            kind="foreign-deny",
        ),
        await _observe(
            api,
            name="foreign-download-read",
            phase="foreign-check",
            method="GET",
            path=download_path,
            tenant_id=foreign_tenant_id,
            identity=foreign_identity,
            target_id=export_id,
            kind="foreign-deny",
        ),
        await _observe(
            api,
            name="owner-status-after",
            phase="owner-after",
            method="GET",
            path=status_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=export_id,
            kind="owner-state",
        ),
        await _observe(
            api,
            name="owner-download-after",
            phase="owner-after",
            method="GET",
            path=download_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=export_id,
            kind="owner-state",
        ),
    ]

    session_id = events["sessionId"]
    event_id = events["eventId"]
    event_version_id = events["classroomVersionId"]
    session_path = f"/api/v1/classroom-sessions/{session_id}"
    projection_path = f"/api/v1/teaching-reports/classrooms/{event_version_id}"
    event_body = {
        "events": [
            {
                "schema_version": "1.0",
                "event_id": event_id,
                "event_type": "classroom.started",
                "occurred_at": "2026-08-28T00:00:00Z",
            }
        ]
    }
    # Keep the actor constant while switching only the tenant binding. Session
    # reads also filter by actor, so a different account would make a denial
    # ambiguous and could hide a missing tenant predicate.
    event_operations = [
        await _observe(
            api,
            name="owner-session-before",
            phase="owner-before",
            method="GET",
            path=session_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=session_id,
            kind="owner-state",
        ),
        await _observe(
            api,
            name="owner-projection-before",
            phase="owner-before",
            method="GET",
            path=projection_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=event_version_id,
            kind="owner-state",
        ),
        await _observe(
            api,
            name="foreign-session-read",
            phase="foreign-check",
            method="GET",
            path=session_path,
            tenant_id=foreign_tenant_id,
            identity=owner_identity,
            target_id=session_id,
            kind="foreign-deny",
        ),
        await _observe(
            api,
            name="foreign-ticket-issue",
            phase="foreign-check",
            method="POST",
            path=f"{session_path}/event-ticket",
            tenant_id=foreign_tenant_id,
            identity=owner_identity,
            target_id=session_id,
            kind="foreign-deny",
        ),
        await _observe(
            api,
            name="foreign-event-ingest",
            phase="foreign-check",
            method="POST",
            path=f"{session_path}/events",
            tenant_id=foreign_tenant_id,
            identity=owner_identity,
            target_id=event_id,
            kind="foreign-deny",
            headers={"X-Classroom-Ticket": event_ticket.get_secret_value()},
            request_body=event_body,
        ),
        await _observe(
            api,
            name="owner-session-after",
            phase="owner-after",
            method="GET",
            path=session_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=session_id,
            kind="owner-state",
        ),
        await _observe(
            api,
            name="owner-projection-after",
            phase="owner-after",
            method="GET",
            path=projection_path,
            tenant_id=owner_tenant_id,
            identity=owner_identity,
            target_id=event_version_id,
            kind="owner-state",
        ),
    ]

    observations = [
        _observation(
            0,
            "database",
            database,
            owner_tenant_id,
            owner_identity.user_id,
            database_operations,
        ),
        _observation(
            1,
            "objects",
            objects,
            owner_tenant_id,
            owner_identity.user_id,
            object_operations,
        ),
        _observation(
            2,
            "exports",
            exports,
            owner_tenant_id,
            owner_identity.user_id,
            export_operations,
        ),
        _observation(
            3,
            "events",
            events,
            owner_tenant_id,
            owner_identity.user_id,
            event_operations,
        ),
    ]
    report_shell = {
        "capacityProof": {"tenantIds": [owner_tenant_id, foreign_tenant_id]},
        "principals": [
            {
                "tenantId": owner_tenant_id,
                "actorId": owner_identity.user_id,
                "role": "user",
                "membershipStatus": "active",
            },
            {
                "tenantId": foreign_tenant_id,
                "actorId": foreign_identity.user_id,
                "role": "user",
                "membershipStatus": "active",
            },
        ],
        "crossTenantPrincipal": {
            "tenantId": foreign_tenant_id,
            "actorId": owner_identity.user_id,
            "role": "student",
            "membershipStatus": "active",
        },
        "observations": observations,
    }
    checks = derive_tenant_isolation_checks(report_shell)
    if not checks or any(value is not True for value in checks.values()):
        raise TenantIsolationProbeError("tenant_isolation_failed")
    return observations


async def _listed_identity_id(
    api: TenantIsolationApi,
    *,
    username: str,
    error_code: str = "identity_cleanup_failed",
) -> str | None:
    try:
        raw_users = await api.admin_list_json("GET", "/api/v1/auth/users")
    except TenantIsolationProbeError as exc:
        raise TenantIsolationProbeError(error_code) from exc
    match: str | None = None
    for raw_user in raw_users:
        if not isinstance(raw_user, dict):
            raise TenantIsolationProbeError("identity_cleanup_failed")
        user_id = raw_user.get("id")
        observed_username = raw_user.get("username")
        if observed_username != username:
            continue
        if (
            not isinstance(user_id, str)
            or _PUBLIC_ID.fullmatch(user_id) is None
            or match is not None
        ):
            raise TenantIsolationProbeError("identity_cleanup_failed")
        match = user_id
    return match


async def ensure_identity_creation_ready(
    api: TenantIsolationApi,
    *,
    usernames: Sequence[str],
) -> None:
    for username in usernames:
        username = _public_id(username, "identity_creation_preflight_failed")
        if (
            await _listed_identity_id(
                api,
                username=username,
                error_code="identity_creation_preflight_failed",
            )
            is not None
        ):
            raise TenantIsolationProbeError("identity_creation_preflight_failed")


async def delete_membership_with_reconciliation(
    api: TenantIsolationApi,
    *,
    tenant_id: str,
    expected_user_id: str,
) -> None:
    tenant_id = _public_id(tenant_id, "membership_cleanup_failed")
    expected_user_id = _public_id(expected_user_id, "membership_cleanup_failed")
    for _attempt in range(2):
        try:
            response = await api.tenant_admin_response(
                "DELETE",
                f"/api/v1/tenants/{tenant_id}/members/{expected_user_id}",
                tenant_id=tenant_id,
                json_body={
                    "expected_tenant_id": tenant_id,
                    "expected_user_id": expected_user_id,
                },
            )
        except TenantIsolationProbeError:
            continue
        if response.status_code == 204 and response.content == b"":
            return
        if response.status_code == 404:
            try:
                tombstone = response.json()
            except (UnicodeError, ValueError) as exc:
                raise TenantIsolationProbeError("membership_cleanup_failed") from exc
            if tombstone != {"detail": "Tenant membership not found"}:
                raise TenantIsolationProbeError("membership_cleanup_failed")
            try:
                provisioning = await api.tenant_admin_json(
                    "GET",
                    f"/api/v1/tenants/{tenant_id}/provisioning",
                    tenant_id=tenant_id,
                    expected_statuses=frozenset({200}),
                )
            except TenantIsolationProbeError as exc:
                raise TenantIsolationProbeError("membership_cleanup_failed") from exc
            if provisioning.get("tenant_id") == tenant_id:
                return
        raise TenantIsolationProbeError("membership_cleanup_failed")
    raise TenantIsolationProbeError("membership_cleanup_failed")


async def delete_identity_with_reconciliation(
    api: TenantIsolationApi,
    *,
    username: str,
    expected_user_id: str,
) -> None:
    username = _public_id(username, "identity_cleanup_failed")
    expected_user_id = _public_id(expected_user_id, "identity_cleanup_failed")
    try:
        body = await api.admin_json(
            "DELETE",
            f"/api/v1/auth/users/{username}",
            json_body={"expected_user_id": expected_user_id},
            expected_statuses=frozenset({200}),
        )
    except TenantIsolationProbeError:
        body = None
    if body == {"ok": True}:
        return
    observed_user_id = await _listed_identity_id(api, username=username)
    if observed_user_id is None:
        return
    raise TenantIsolationProbeError("identity_cleanup_failed")


def _contains_sensitive_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                return True
            if _contains_sensitive_field(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_field(item) for item in value)
    return False


def _validate_report_for_stdout(body: bytes) -> None:
    try:
        report = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TenantIsolationProbeError("tenant_isolation_report_invalid") from exc
    if not isinstance(report, dict) or _contains_sensitive_field(report):
        raise TenantIsolationProbeError("tenant_isolation_report_invalid")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("first-release",))
    return parser.parse_args(argv)


def _load_config(
    environment: Mapping[str, str],
    *,
    cwd: Path,
    candidate_loader: CandidateLoader = _default_candidate_loader,
) -> ProbeConfig:
    raw_root = environment.get("YFEISTAI_CANDIDATE_ROOT")
    if not isinstance(raw_root, str) or not raw_root:
        raise TenantIsolationProbeError("candidate_root_invalid")
    try:
        candidate_root = Path(raw_root).resolve(strict=True)
        current_root = Path(cwd).resolve(strict=True)
    except OSError as exc:
        raise TenantIsolationProbeError("candidate_root_invalid") from exc
    if candidate_root != current_root:
        raise TenantIsolationProbeError("candidate_root_invalid")

    token = environment.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise TenantIsolationProbeError("fixture_token_unavailable")
    token = token.strip()

    release_run: dict[str, str] = {}
    for field, name in (
        ("runId", "YFEISTAI_RELEASE_RUN_ID"),
        ("environmentId", "YFEISTAI_ENVIRONMENT_ID"),
    ):
        value = environment.get(name)
        if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
            raise TenantIsolationProbeError("release_identity_invalid")
        release_run[field] = value

    base_url = environment.get("WEB_BASE_URL")
    if not _valid_base_url(base_url):
        raise TenantIsolationProbeError("base_url_invalid")
    assert isinstance(base_url, str)

    raw_timeout = environment.get("YFEISTAI_TENANT_ISOLATION_TIMEOUT_SECONDS")
    try:
        timeout_seconds = int(raw_timeout or "")
    except ValueError as exc:
        raise TenantIsolationProbeError("timeout_invalid") from exc
    if timeout_seconds < 60 or timeout_seconds > 86_400:
        raise TenantIsolationProbeError("timeout_invalid")

    raw_capacity_path = environment.get("YFEISTAI_CAPACITY_ATTESTATION_PATH")
    raw_capacity_sha256 = environment.get("YFEISTAI_CAPACITY_ATTESTATION_SHA256")
    if (
        not isinstance(raw_capacity_path, str)
        or not raw_capacity_path
        or not Path(raw_capacity_path).is_absolute()
        or not isinstance(raw_capacity_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", raw_capacity_sha256) is None
        or raw_capacity_sha256 == "0" * 64
    ):
        raise TenantIsolationProbeError("capacity_attestation_invalid")
    capacity_path = Path(os.path.abspath(raw_capacity_path))
    expected_capacity_path = candidate_root / "runtime" / "capacity-profile-attestation.json"
    if capacity_path != expected_capacity_path:
        raise TenantIsolationProbeError("capacity_attestation_invalid")
    try:
        capacity_body, capacity_sha256 = read_capacity_profile_attestation_artifact(
            capacity_path,
            bundle_root=candidate_root,
        )
    except (OSError, ValueError) as exc:
        raise TenantIsolationProbeError("capacity_attestation_invalid") from exc
    if capacity_sha256 != raw_capacity_sha256:
        raise TenantIsolationProbeError("capacity_attestation_invalid")

    raw_tenant_ids = environment.get("YFEISTAI_CAPACITY_TENANT_IDS")
    try:
        tenant_ids = json.loads(raw_tenant_ids or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise TenantIsolationProbeError("capacity_tenant_ids_invalid") from exc
    if (
        not isinstance(tenant_ids, list)
        or len(tenant_ids) != 2
        or tenant_ids[0] == tenant_ids[1]
        or any(
            not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None
            for value in tenant_ids
        )
    ):
        raise TenantIsolationProbeError("capacity_tenant_ids_invalid")

    try:
        candidate = dict(candidate_loader(candidate_root))
    except TenantIsolationProbeError:
        raise
    except Exception as exc:
        raise TenantIsolationProbeError("candidate_invalid") from exc
    if not candidate:
        raise TenantIsolationProbeError("candidate_invalid")
    return ProbeConfig(
        admin_token=SecretStr(token),
        base_url=base_url,
        candidate=candidate,
        candidate_root=candidate_root,
        capacity_attestation_path=capacity_path,
        capacity_attestation_sha256=raw_capacity_sha256,
        capacity_tenant_ids=tenant_ids,
        release_run=release_run,
        timeout_seconds=timeout_seconds,
    )


def _identity_material(config: ProbeConfig, *, attempt_id: str) -> IdentityMaterial:
    attempt_id = _public_id(attempt_id, "cleanup_recovery_invalid")
    binding = _canonical_json(
        {
            "candidate": config.candidate,
            "releaseRun": config.release_run,
            "attemptId": attempt_id,
            "purpose": "tenant-isolation-identity-v1",
        }
    )
    key = config.admin_token.get_secret_value().encode("utf-8")
    digest = hmac.new(key, binding, hashlib.sha256).hexdigest()
    owner_password = hmac.new(key, f"{digest}:owner".encode(), hashlib.sha256).hexdigest()
    foreign_password = hmac.new(key, f"{digest}:foreign".encode(), hashlib.sha256).hexdigest()
    suffix = digest[:16]
    return IdentityMaterial(
        owner_username=f"isolation-owner-{suffix}",
        owner_password=SecretStr(f"Iso9!{owner_password}"),
        foreign_username=f"isolation-foreign-{suffix}",
        foreign_password=SecretStr(f"Iso9!{foreign_password}"),
    )


def _cleanup_recovery_path(config: ProbeConfig) -> Path:
    path = Path(os.path.abspath(config.capacity_attestation_path.parent / _CLEANUP_RECOVERY_NAME))
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise TenantIsolationProbeError("cleanup_recovery_path_invalid") from exc
    if os.path.normcase(str(resolved_parent)) != os.path.normcase(str(path.parent)):
        raise TenantIsolationProbeError("cleanup_recovery_path_invalid")
    if os.path.lexists(path) and path.is_symlink():
        raise TenantIsolationProbeError("cleanup_recovery_path_invalid")
    return path


@contextmanager
def _cleanup_recovery_lock(config: ProbeConfig) -> Iterator[None]:
    journal_path = _cleanup_recovery_path(config)
    path = journal_path.with_name(".tenant-isolation-cleanup.lock")
    if os.path.lexists(path):
        observed = os.lstat(path)
        if not stat.S_ISREG(observed.st_mode) or path.is_symlink():
            raise TenantIsolationProbeError("cleanup_recovery_lock_invalid")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise TenantIsolationProbeError("cleanup_recovery_lock_invalid") from exc

    locked = False
    try:
        os.chmod(path, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except (BlockingIOError, OSError) as exc:
        raise TenantIsolationProbeError("cleanup_recovery_locked") from exc
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _cleanup_state_payload(state: IsolationCleanupState | None) -> object:
    if state is None:
        return None
    return {
        "tenantId": state.tenant_id,
        "userId": state.user_id,
        "classId": state.class_id,
        "enrollmentActive": state.enrollment_active,
        "sourceBindingId": state.source_binding_id,
        "learningSessionId": state.learning_session_id,
    }


def _cleanup_recovery_fingerprint(config: ProbeConfig) -> str:
    key = config.admin_token.get_secret_value().encode("utf-8")
    return hmac.new(
        key,
        b"yfeistai-tenant-isolation-cleanup-key-v1",
        hashlib.sha256,
    ).hexdigest()[:32]


def _cleanup_recovery_mac(config: ProbeConfig, payload: Mapping[str, object]) -> str:
    key = config.admin_token.get_secret_value().encode("utf-8")
    body = _canonical_json(payload)
    return hmac.new(
        key,
        b"yfeistai-tenant-isolation-cleanup-journal-v1\0" + body,
        hashlib.sha256,
    ).hexdigest()


def _parse_cleanup_recovery(
    value: object,
    *,
    config: ProbeConfig,
    material: IdentityMaterial,
) -> IsolationCleanupRecovery:
    expected_keys = {
        "schemaVersion",
        "attemptId",
        "keyFingerprint",
        "candidate",
        "releaseRun",
        "baseUrl",
        "capacityAttestationSha256",
        "ownerUsername",
        "identityIntents",
        "created",
        "memberships",
        "cleanupState",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schemaVersion") != _CLEANUP_RECOVERY_SCHEMA_VERSION
        or value.get("keyFingerprint") != _cleanup_recovery_fingerprint(config)
        or value.get("candidate") != config.candidate
        or value.get("releaseRun") != config.release_run
        or value.get("baseUrl") != config.base_url
        or value.get("capacityAttestationSha256") != config.capacity_attestation_sha256
        or value.get("ownerUsername") != material.owner_username
        or _contains_sensitive_field(value)
    ):
        raise TenantIsolationProbeError("cleanup_recovery_invalid")

    attempt_id = _public_id(value.get("attemptId"), "cleanup_recovery_invalid")

    raw_identity_intents = value.get("identityIntents")
    if not isinstance(raw_identity_intents, list) or tuple(raw_identity_intents) != (
        material.owner_username,
        material.foreign_username,
    ):
        raise TenantIsolationProbeError("cleanup_recovery_invalid")
    identity_intents = tuple(
        _public_id(item, "cleanup_recovery_invalid") for item in raw_identity_intents
    )

    raw_created = value.get("created")
    if not isinstance(raw_created, list) or len(raw_created) > 2:
        raise TenantIsolationProbeError("cleanup_recovery_invalid")
    created: list[tuple[str, str]] = []
    allowed_usernames = {material.owner_username, material.foreign_username}
    for item in raw_created:
        if not isinstance(item, dict) or set(item) != {"username", "userId"}:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        username = _public_id(item.get("username"), "cleanup_recovery_invalid")
        user_id = _public_id(item.get("userId"), "cleanup_recovery_invalid")
        if username not in allowed_usernames:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        created.append((username, user_id))
    if len(set(created)) != len(created):
        raise TenantIsolationProbeError("cleanup_recovery_invalid")
    created_usernames = {username for username, _user_id in created}
    created_user_ids = {user_id for _username, user_id in created}
    if len(created_usernames) != len(created) or len(created_user_ids) != len(created):
        raise TenantIsolationProbeError("cleanup_recovery_invalid")

    raw_memberships = value.get("memberships")
    if not isinstance(raw_memberships, list) or len(raw_memberships) > 3:
        raise TenantIsolationProbeError("cleanup_recovery_invalid")
    memberships: list[tuple[str, str]] = []
    allowed_tenants = set(config.capacity_tenant_ids)
    for item in raw_memberships:
        if not isinstance(item, dict) or set(item) != {"tenantId", "userId"}:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        tenant_id = _public_id(item.get("tenantId"), "cleanup_recovery_invalid")
        user_id = _public_id(item.get("userId"), "cleanup_recovery_invalid")
        if tenant_id not in allowed_tenants or user_id not in created_user_ids:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        memberships.append((tenant_id, user_id))
    if len(set(memberships)) != len(memberships):
        raise TenantIsolationProbeError("cleanup_recovery_invalid")

    raw_state = value.get("cleanupState")
    cleanup_state: IsolationCleanupState | None = None
    if raw_state is not None:
        if not isinstance(raw_state, dict) or set(raw_state) != {
            "tenantId",
            "userId",
            "classId",
            "enrollmentActive",
            "sourceBindingId",
            "learningSessionId",
        }:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        tenant_id = _public_id(raw_state.get("tenantId"), "cleanup_recovery_invalid")
        user_id = _public_id(raw_state.get("userId"), "cleanup_recovery_invalid")
        if (
            tenant_id not in allowed_tenants
            or material.owner_username not in created_usernames
            or (material.owner_username, user_id) not in created
            or not isinstance(raw_state.get("enrollmentActive"), bool)
        ):
            raise TenantIsolationProbeError("cleanup_recovery_invalid")

        optional_ids: dict[str, str | None] = {}
        for field in ("classId", "sourceBindingId", "learningSessionId"):
            item = raw_state.get(field)
            optional_ids[field] = (
                None if item is None else _public_id(item, "cleanup_recovery_invalid")
            )
        cleanup_state = IsolationCleanupState(
            tenant_id=tenant_id,
            user_id=user_id,
            class_id=optional_ids["classId"],
            enrollment_active=raw_state["enrollmentActive"],
            source_binding_id=optional_ids["sourceBindingId"],
            learning_session_id=optional_ids["learningSessionId"],
        )

    return IsolationCleanupRecovery(
        attempt_id=attempt_id,
        identity_intents=identity_intents,
        cleanup_state=cleanup_state,
        memberships=tuple(memberships),
        created=tuple(created),
    )


def _write_cleanup_recovery_state(
    config: ProbeConfig,
    *,
    attempt_id: str,
    material: IdentityMaterial,
    cleanup_state: IsolationCleanupState | None,
    memberships: Sequence[tuple[str, str]],
    created: Sequence[tuple[str, str]],
) -> None:
    attempt_id = _public_id(attempt_id, "cleanup_recovery_invalid")
    if material != _identity_material(config, attempt_id=attempt_id):
        raise TenantIsolationProbeError("cleanup_recovery_invalid")
    payload = {
        "schemaVersion": _CLEANUP_RECOVERY_SCHEMA_VERSION,
        "attemptId": attempt_id,
        "keyFingerprint": _cleanup_recovery_fingerprint(config),
        "candidate": config.candidate,
        "releaseRun": config.release_run,
        "baseUrl": config.base_url,
        "capacityAttestationSha256": config.capacity_attestation_sha256,
        "ownerUsername": material.owner_username,
        "identityIntents": [material.owner_username, material.foreign_username],
        "created": [{"username": username, "userId": user_id} for username, user_id in created],
        "memberships": [
            {"tenantId": tenant_id, "userId": user_id} for tenant_id, user_id in memberships
        ],
        "cleanupState": _cleanup_state_payload(cleanup_state),
    }
    _parse_cleanup_recovery(payload, config=config, material=material)
    signed = dict(payload)
    signed["hmacSha256"] = _cleanup_recovery_mac(config, payload)
    body = _canonical_json(signed) + b"\n"
    if len(body) > _MAX_CLEANUP_RECOVERY_BYTES:
        raise TenantIsolationProbeError("cleanup_recovery_invalid")

    path = _cleanup_recovery_path(config)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        raise TenantIsolationProbeError("cleanup_recovery_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def _read_cleanup_recovery_state(
    config: ProbeConfig,
) -> tuple[IdentityMaterial, IsolationCleanupRecovery] | None:
    path = _cleanup_recovery_path(config)
    if not os.path.lexists(path):
        return None
    try:
        observed = os.lstat(path)
        if not stat.S_ISREG(observed.st_mode) or path.is_symlink():
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        if os.name != "nt" and stat.S_IMODE(observed.st_mode) & 0o077:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                body = handle.read(_MAX_CLEANUP_RECOVERY_BYTES + 1)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(body) > _MAX_CLEANUP_RECOVERY_BYTES:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate cleanup recovery key")
                result[key] = item
            return result

        value = json.loads(body, object_pairs_hook=reject_duplicates)
        if not isinstance(value, dict) or set(value) != {
            "schemaVersion",
            "attemptId",
            "keyFingerprint",
            "candidate",
            "releaseRun",
            "baseUrl",
            "capacityAttestationSha256",
            "ownerUsername",
            "identityIntents",
            "created",
            "memberships",
            "cleanupState",
            "hmacSha256",
        }:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        observed_mac = value.get("hmacSha256")
        if not isinstance(observed_mac, str) or re.fullmatch(r"[0-9a-f]{64}", observed_mac) is None:
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        unsigned = dict(value)
        del unsigned["hmacSha256"]
        expected_mac = _cleanup_recovery_mac(config, unsigned)
        if not hmac.compare_digest(observed_mac, expected_mac):
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        attempt_id = _public_id(unsigned.get("attemptId"), "cleanup_recovery_invalid")
        material = _identity_material(config, attempt_id=attempt_id)
    except TenantIsolationProbeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TenantIsolationProbeError("cleanup_recovery_invalid") from exc
    recovery = _parse_cleanup_recovery(unsigned, config=config, material=material)
    return material, recovery


def _remove_cleanup_recovery_state(config: ProbeConfig) -> None:
    path = _cleanup_recovery_path(config)
    if not os.path.lexists(path):
        return
    try:
        observed = os.lstat(path)
        if not stat.S_ISREG(observed.st_mode) or path.is_symlink():
            raise TenantIsolationProbeError("cleanup_recovery_invalid")
        os.unlink(path)
    except TenantIsolationProbeError:
        raise
    except OSError as exc:
        raise TenantIsolationProbeError("cleanup_recovery_remove_failed") from exc


async def _recover_pending_cleanup(
    api: TenantIsolationApi,
    *,
    config: ProbeConfig,
) -> None:
    pending = _read_cleanup_recovery_state(config)
    if pending is None:
        return
    material, recovery = pending

    created = list(recovery.created)
    known_usernames = {username for username, _user_id in created}
    try:
        for username in recovery.identity_intents:
            if username in known_usernames:
                continue
            user_id = await _listed_identity_id(api, username=username)
            if user_id is not None:
                password = (
                    material.owner_password
                    if username == material.owner_username
                    else material.foreign_password
                )
                verified = await api.login_identity(username, password)
                if verified.username != username or verified.user_id != user_id:
                    raise TenantIsolationProbeError("cleanup_recovery_failed")
                created.append((username, user_id))
                known_usernames.add(username)
    except Exception as exc:
        raise TenantIsolationProbeError("cleanup_recovery_failed") from exc

    owner_identity: IdentityCredential | None = None
    if recovery.cleanup_state is not None:
        try:
            owner_identity = await api.login_identity(
                material.owner_username,
                material.owner_password,
            )
        except Exception as exc:
            raise TenantIsolationProbeError("cleanup_recovery_failed") from exc
        if (
            owner_identity.username != material.owner_username
            or owner_identity.user_id != recovery.cleanup_state.user_id
        ):
            raise TenantIsolationProbeError("cleanup_recovery_failed")

    owner_user_id = next(
        (user_id for username, user_id in created if username == material.owner_username),
        None,
    )

    def checkpoint(
        cleanup_state: IsolationCleanupState | None,
        memberships: Sequence[tuple[str, str]],
        confirmed_created: Sequence[tuple[str, str]],
    ) -> None:
        _write_cleanup_recovery_state(
            config,
            attempt_id=recovery.attempt_id,
            material=material,
            cleanup_state=cleanup_state,
            memberships=memberships,
            created=confirmed_created,
        )

    try:
        cleanup_failed = await _cleanup_isolation_state(
            api,
            cleanup_state=recovery.cleanup_state,
            owner_identity=owner_identity,
            owner_user_id=owner_user_id,
            memberships=recovery.memberships,
            created=tuple(created),
            checkpoint=checkpoint,
        )
    except Exception as exc:
        raise TenantIsolationProbeError("cleanup_recovery_failed") from exc
    if cleanup_failed:
        raise TenantIsolationProbeError("cleanup_recovery_failed")
    checkpoint(None, (), ())
    _remove_cleanup_recovery_state(config)


async def _create_identity(
    api: TenantIsolationApi,
    *,
    username: str,
    password: SecretStr,
) -> str:
    body = await api.admin_json(
        "POST",
        "/api/v1/auth/users",
        json_body={"username": username, "password": password.get_secret_value()},
        expected_statuses=frozenset({201}),
    )
    user_id = _public_id(body.get("user_id"), "identity_create_invalid")
    if (
        set(body) != {"ok", "user_id", "username", "role", "is_admin"}
        or body.get("ok") is not True
        or body.get("username") != username
        or body.get("role") != "user"
        or body.get("is_admin") is not False
    ):
        raise TenantIsolationProbeError("identity_create_invalid")
    return user_id


async def _bind_identity_to_tenant(
    api: TenantIsolationApi,
    *,
    tenant_id: str,
    user_id: str,
    roles: tuple[str, ...] = ("platform_admin", "teacher"),
) -> None:
    grants = [{"role": role, "scope_type": "tenant", "scope_id": tenant_id} for role in roles]
    body = await api.tenant_admin_json(
        "POST",
        f"/api/v1/tenants/{tenant_id}/members",
        tenant_id=tenant_id,
        json_body={"user_id": user_id, "grants": grants},
        expected_statuses=frozenset({200}),
    )
    if (
        set(body) != {"tenant_id", "user_id", "roles", "grants"}
        or body.get("tenant_id") != tenant_id
        or body.get("user_id") != user_id
        or set(body.get("roles", ())) != set(roles)
        or not isinstance(body.get("grants"), list)
        or {
            (grant.get("role"), grant.get("scope_type"), grant.get("scope_id"))
            for grant in body["grants"]
            if isinstance(grant, dict)
        }
        != {(role, "tenant", tenant_id) for role in roles}
    ):
        raise TenantIsolationProbeError("tenant_membership_invalid")


async def _build_isolation_fixture(
    api: TenantIsolationApi,
    *,
    config: ProbeConfig,
    owner_identity: IdentityCredential,
    owner_tenant_id: str,
    cleanup_state: IsolationCleanupState | None = None,
    persist_cleanup_state: Callable[[], None] | None = None,
) -> IsolationFixture:
    if cleanup_state is not None and (
        cleanup_state.tenant_id != owner_tenant_id
        or cleanup_state.user_id != owner_identity.user_id
    ):
        raise TenantIsolationProbeError("tenant_isolation_cleanup_state_invalid")
    suffix = hashlib.sha256(owner_identity.username.encode("utf-8")).hexdigest()[:16]
    run_key = f"isolation-{suffix}"
    course_id = f"course-{suffix}"
    class_id = f"class-{suffix}"

    course = await api.tenant_identity_json(
        "POST",
        "/api/v1/teaching/courses",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        json_body={"id": course_id, "title": "Tenant isolation acceptance"},
        expected_statuses=frozenset({201}),
    )
    if course.get("id") != course_id or course.get("status") != "active":
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    policy = {
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
    }
    policy_response = await api.tenant_identity_json(
        "PUT",
        f"/api/v1/teaching/courses/{course_id}/generation-policy",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        json_body=policy,
        expected_statuses=frozenset({200}),
    )
    if (
        policy_response.get("tenantId") != owner_tenant_id
        or policy_response.get("courseId") != course_id
        or policy_response.get("updatedBy") != owner_identity.user_id
    ):
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    classroom = await api.tenant_identity_json(
        "POST",
        f"/api/v1/teaching/courses/{course_id}/classes",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        json_body={"id": class_id, "name": "Tenant isolation acceptance"},
        expected_statuses=frozenset({201}),
    )
    if (
        classroom.get("id") != class_id
        or classroom.get("courseId") != course_id
        or classroom.get("status") != "active"
    ):
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    if cleanup_state is not None:
        cleanup_state.class_id = class_id
        cleanup_state.enrollment_active = True
        if persist_cleanup_state is not None:
            persist_cleanup_state()
    enrollment = await api.tenant_identity_json(
        "POST",
        f"/api/v1/teaching/classes/{class_id}/enrollments",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        json_body={"userId": owner_identity.user_id},
        expected_statuses=frozenset({201}),
    )
    if (
        enrollment.get("classId") != class_id
        or enrollment.get("userId") != owner_identity.user_id
        or enrollment.get("status") != "active"
    ):
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")
    pdf_buffer = io.BytesIO()
    pdf_writer = PdfWriter()
    pdf_writer.add_blank_page(width=612, height=792)
    pdf_writer.add_metadata({"/Title": "Tenant isolation acceptance"})
    pdf_writer.write(pdf_buffer)
    source = await api.tenant_identity_multipart_json(
        "POST",
        "/api/v1/teaching/sources/pdf",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        data={"courseId": course_id, "classId": class_id},
        files={
            "file": (
                "tenant-isolation.pdf",
                pdf_buffer.getvalue(),
                "application/pdf",
            )
        },
        expected_statuses=frozenset({201}),
    )
    binding_id = _public_id(
        source.get("bindingId"),
        "tenant_isolation_fixture_invalid",
    )
    if (
        source.get("sourceType") != "pdf"
        or source.get("courseId") != course_id
        or source.get("classId") != class_id
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
    ):
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")
    if cleanup_state is not None:
        cleanup_state.source_binding_id = binding_id
        if persist_cleanup_state is not None:
            persist_cleanup_state()

    quota = await api.tenant_admin_json(
        "POST",
        "/api/v1/teaching/generation-quota-grants",
        tenant_id=owner_tenant_id,
        headers={"Idempotency-Key": f"{run_key}-quota"},
        json_body={"units": 20},
        expected_statuses=frozenset({200}),
    )
    if (
        quota.get("tenantId") != owner_tenant_id
        or quota.get("units") != 20
        or isinstance(quota.get("balance"), bool)
        or not isinstance(quota.get("balance"), int)
        or quota["balance"] < 20
    ):
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    created = await api.tenant_identity_json(
        "POST",
        "/api/v1/classrooms",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        headers={"Idempotency-Key": f"{run_key}-classroom"},
        json_body={
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
        expected_statuses=frozenset({202}),
    )
    asset_id = _public_id(created.get("assetId"), "tenant_isolation_fixture_invalid")
    if created.get("ownerId") != owner_identity.user_id:
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")
    await api.tenant_identity_json(
        "POST",
        f"/api/v1/classrooms/{asset_id}/confirm-outline",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        expected_statuses=frozenset({202}),
    )

    end_time = time.monotonic() + config.timeout_seconds
    generated: dict[str, Any]
    while True:
        generated = await api.tenant_identity_json(
            "GET",
            f"/api/v1/classrooms/{asset_id}",
            identity=owner_identity,
            tenant_id=owner_tenant_id,
            expected_statuses=frozenset({200}),
        )
        if generated.get("status") in {"failed", "canceled"}:
            raise TenantIsolationProbeError("tenant_isolation_fixture_failed")
        if (
            generated.get("status") == "succeeded"
            and generated.get("lifecycleState") == "editing"
            and isinstance(generated.get("document"), dict)
        ):
            break
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.sleep(min(0.25, remaining))

    validated = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classrooms/{asset_id}/validate",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        expected_statuses=frozenset({200}),
    )
    validation = validated.get("validationReport")
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    review = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classrooms/{asset_id}/submit",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        headers={"Idempotency-Key": f"{run_key}-review"},
        json_body={"scope": "class", "classId": class_id},
        expected_statuses=frozenset({201}),
    )
    review_id = _public_id(review.get("id"), "tenant_isolation_fixture_invalid")
    if review.get("assetId") != asset_id or review.get("status") != "pending":
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")
    approved = await api.tenant_admin_json(
        "POST",
        f"/api/v1/classroom-reviews/{review_id}/approve",
        tenant_id=owner_tenant_id,
        json_body={"comment": "First-release tenant isolation acceptance"},
        expected_statuses=frozenset({200}),
    )
    if approved.get("id") != review_id or approved.get("status") != "approved":
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    published = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classrooms/{asset_id}/publish",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        headers={"Idempotency-Key": f"{run_key}-publish"},
        json_body={"scope": "class", "classId": class_id},
        expected_statuses=frozenset({201}),
    )
    version_id = _public_id(
        published.get("versionId"),
        "tenant_isolation_fixture_invalid",
    )
    if published.get("assetId") != asset_id or published.get("classId") != class_id:
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    assignment = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-versions/{version_id}/assign",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        headers={"Idempotency-Key": f"{run_key}-assignment"},
        json_body={"classId": class_id},
        expected_statuses=frozenset({201}),
    )
    assignment_id = _public_id(
        assignment.get("assignmentId"),
        "tenant_isolation_fixture_invalid",
    )
    if assignment.get("versionId") != version_id or assignment.get("classId") != class_id:
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    session = await api.tenant_identity_json(
        "POST",
        "/api/v1/classroom-sessions",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        json_body={"assignment_id": assignment_id},
        expected_statuses=frozenset({201}),
    )
    session_id = _public_id(session.get("id"), "tenant_isolation_fixture_invalid")
    if (
        session.get("tenant_id") != owner_tenant_id
        or session.get("user_id") != owner_identity.user_id
        or session.get("classroom_version_id") != version_id
        or session.get("assignment_id") != assignment_id
    ):
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")
    if cleanup_state is not None:
        cleanup_state.learning_session_id = session_id
        if persist_cleanup_state is not None:
            persist_cleanup_state()

    document_ticket_response = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-sessions/{session_id}/read-ticket",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        json_body={
            "action": "classroom.document.read",
            "resource_id": version_id,
        },
        expected_statuses=frozenset({200}),
    )
    event_ticket_response = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-sessions/{session_id}/event-ticket",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        expected_statuses=frozenset({200}),
    )
    document_ticket = document_ticket_response.get("ticket")
    event_ticket = event_ticket_response.get("ticket")
    if (
        not isinstance(document_ticket, str)
        or not document_ticket
        or not isinstance(event_ticket, str)
        or not event_ticket
    ):
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    export = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-versions/{version_id}/exports",
        identity=owner_identity,
        tenant_id=owner_tenant_id,
        headers={"Idempotency-Key": f"{run_key}-offline-html"},
        json_body={"format": "offline_html"},
        expected_statuses=frozenset({202}),
    )
    export_id = _public_id(export.get("job_id"), "tenant_isolation_fixture_invalid")
    while export.get("status") != "succeeded":
        if export.get("status") in {"failed", "canceled"}:
            raise TenantIsolationProbeError("tenant_isolation_fixture_failed")
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.sleep(min(0.25, remaining))
        export = await api.tenant_identity_json(
            "GET",
            f"/api/v1/classroom-exports/{export_id}",
            identity=owner_identity,
            tenant_id=owner_tenant_id,
            expected_statuses=frozenset({200}),
        )
    if export.get("job_id") != export_id or export.get("download_ready") is not True:
        raise TenantIsolationProbeError("tenant_isolation_fixture_invalid")

    return IsolationFixture(
        targets={
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
        },
        document_ticket=SecretStr(document_ticket),
        event_ticket=SecretStr(event_ticket),
    )


async def _delete_owner_resource(
    api: TenantIsolationApi,
    *,
    path: str,
    identity: IdentityCredential,
    tenant_id: str,
    expected_not_found_detail: str,
) -> None:
    for _attempt in range(2):
        try:
            response = await api.tenant_identity_response(
                "DELETE",
                path,
                identity=identity,
                tenant_id=tenant_id,
            )
        except TenantIsolationProbeError:
            continue
        if response.status_code == 204:
            return
        if response.status_code == 404:
            try:
                tombstone = response.json()
            except (UnicodeError, ValueError):
                tombstone = None
            if tombstone == {"detail": expected_not_found_detail}:
                return
        raise TenantIsolationProbeError("fixture_cleanup_failed")
    raise TenantIsolationProbeError("fixture_cleanup_failed")


async def cleanup_reversible_fixture_resources(
    api: TenantIsolationApi,
    *,
    state: IsolationCleanupState,
    owner_identity: IdentityCredential,
) -> None:
    cleanup_failed = False
    session_id = state.learning_session_id
    if session_id is not None:
        try:
            try:
                completed = await api.tenant_identity_json(
                    "POST",
                    f"/api/v1/classroom-sessions/{session_id}/complete",
                    identity=owner_identity,
                    tenant_id=state.tenant_id,
                    expected_statuses=frozenset({200}),
                )
            except TenantIsolationProbeError:
                completed = await api.tenant_identity_json(
                    "GET",
                    f"/api/v1/classroom-sessions/{session_id}",
                    identity=owner_identity,
                    tenant_id=state.tenant_id,
                    expected_statuses=frozenset({200}),
                )
            if completed.get("id") != session_id or completed.get("status") != "completed":
                raise TenantIsolationProbeError("fixture_cleanup_failed")
            state.learning_session_id = None
        except Exception:
            cleanup_failed = True

    binding_id = state.source_binding_id
    if binding_id is not None:
        try:
            await _delete_owner_resource(
                api,
                path=f"/api/v1/teaching/sources/{binding_id}",
                identity=owner_identity,
                tenant_id=state.tenant_id,
                expected_not_found_detail="source binding not found",
            )
            state.source_binding_id = None
        except Exception:
            cleanup_failed = True

    if state.enrollment_active and state.class_id is not None:
        try:
            await _delete_owner_resource(
                api,
                path=(f"/api/v1/teaching/classes/{state.class_id}/enrollments/{state.user_id}"),
                identity=owner_identity,
                tenant_id=state.tenant_id,
                expected_not_found_detail="enrollment not found",
            )
            state.enrollment_active = False
        except Exception:
            cleanup_failed = True

    if cleanup_failed:
        raise TenantIsolationProbeError("fixture_cleanup_failed")


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _cleanup_isolation_state(
    api: TenantIsolationApi,
    *,
    cleanup_state: IsolationCleanupState | None,
    owner_identity: IdentityCredential | None,
    owner_user_id: str | None,
    memberships: Sequence[tuple[str, str]],
    created: Sequence[tuple[str, str]],
    checkpoint: Callable[
        [
            IsolationCleanupState | None,
            Sequence[tuple[str, str]],
            Sequence[tuple[str, str]],
        ],
        None,
    ],
) -> bool:
    cleanup_failed = False
    blocked_user_ids: set[str] = set()

    observed_owner_ids = {
        value
        for value in (
            owner_user_id,
            cleanup_state.user_id if cleanup_state is not None else None,
            owner_identity.user_id if owner_identity is not None else None,
        )
        if value is not None
    }
    if len(observed_owner_ids) > 1:
        return True
    resolved_owner_id = next(iter(observed_owner_ids), None)

    if cleanup_state is not None:
        if owner_identity is None:
            cleanup_failed = True
        else:
            try:
                async with asyncio.timeout(20):
                    await cleanup_reversible_fixture_resources(
                        api,
                        state=cleanup_state,
                        owner_identity=owner_identity,
                    )
            except Exception:
                cleanup_failed = True
        if cleanup_failed and resolved_owner_id is not None:
            blocked_user_ids.add(resolved_owner_id)

    membership_entries = tuple(memberships)
    created_entries = tuple(created)
    non_owner_memberships = tuple(
        entry for entry in membership_entries if entry[1] != resolved_owner_id
    )
    owner_memberships = tuple(
        entry for entry in membership_entries if entry[1] == resolved_owner_id
    )
    non_owner_created = tuple(entry for entry in created_entries if entry[1] != resolved_owner_id)
    owner_created = tuple(entry for entry in created_entries if entry[1] == resolved_owner_id)

    for tenant_id, user_id in reversed(non_owner_memberships):
        if user_id in blocked_user_ids:
            continue
        try:
            async with asyncio.timeout(20):
                await delete_membership_with_reconciliation(
                    api,
                    tenant_id=tenant_id,
                    expected_user_id=user_id,
                )
        except Exception:
            cleanup_failed = True
            blocked_user_ids.add(user_id)

    for username, user_id in reversed(non_owner_created):
        if user_id in blocked_user_ids:
            continue
        try:
            async with asyncio.timeout(20):
                await delete_identity_with_reconciliation(
                    api,
                    username=username,
                    expected_user_id=user_id,
                )
        except Exception:
            cleanup_failed = True

    if cleanup_failed:
        return True
    if resolved_owner_id is None:
        return False

    try:
        checkpoint(None, owner_memberships, owner_created)
    except Exception:
        return True

    for tenant_id, user_id in reversed(owner_memberships):
        try:
            async with asyncio.timeout(20):
                await delete_membership_with_reconciliation(
                    api,
                    tenant_id=tenant_id,
                    expected_user_id=user_id,
                )
        except Exception:
            return True

    try:
        checkpoint(None, (), owner_created)
    except Exception:
        return True

    for username, user_id in reversed(owner_created):
        try:
            async with asyncio.timeout(20):
                await delete_identity_with_reconciliation(
                    api,
                    username=username,
                    expected_user_id=user_id,
                )
        except Exception:
            return True

    return False


async def _run_tenant_isolation_probe(config: ProbeConfig) -> bytes:
    with _cleanup_recovery_lock(config):
        return await _run_tenant_isolation_probe_locked(config)


async def _run_tenant_isolation_probe_locked(config: ProbeConfig) -> bytes:
    created: list[tuple[str, str]] = []
    memberships: list[tuple[str, str]] = []
    owner_identity: IdentityCredential | None = None
    owner_user_id: str | None = None
    cleanup_state: IsolationCleanupState | None = None
    primary_failure: BaseException | None = None
    cleanup_failed = False
    body: bytes | None = None
    async with TenantIsolationApi(
        config.base_url,
        config.admin_token.get_secret_value(),
        timeout_seconds=float(config.timeout_seconds),
    ) as api:
        await _recover_pending_cleanup(api, config=config)
        attempt_id = f"attempt-{secrets.token_hex(16)}"
        material = _identity_material(config, attempt_id=attempt_id)

        def persist_cleanup_recovery() -> None:
            _write_cleanup_recovery_state(
                config,
                attempt_id=attempt_id,
                material=material,
                cleanup_state=cleanup_state,
                memberships=memberships,
                created=created,
            )

        def checkpoint_cleanup_recovery(
            checkpoint_state: IsolationCleanupState | None,
            checkpoint_memberships: Sequence[tuple[str, str]],
            checkpoint_created: Sequence[tuple[str, str]],
        ) -> None:
            _write_cleanup_recovery_state(
                config,
                attempt_id=attempt_id,
                material=material,
                cleanup_state=checkpoint_state,
                memberships=checkpoint_memberships,
                created=checkpoint_created,
            )

        try:
            async with asyncio.timeout(config.timeout_seconds):
                persist_cleanup_recovery()
                await ensure_identity_creation_ready(
                    api,
                    usernames=(material.owner_username, material.foreign_username),
                )
                owner_user_id = await _create_identity(
                    api,
                    username=material.owner_username,
                    password=material.owner_password,
                )
                created.append((material.owner_username, owner_user_id))
                persist_cleanup_recovery()
                foreign_user_id = await _create_identity(
                    api,
                    username=material.foreign_username,
                    password=material.foreign_password,
                )
                created.append((material.foreign_username, foreign_user_id))
                persist_cleanup_recovery()
                owner_tenant_id, foreign_tenant_id = config.capacity_tenant_ids
                memberships.append((owner_tenant_id, owner_user_id))
                persist_cleanup_recovery()
                await _bind_identity_to_tenant(
                    api,
                    tenant_id=owner_tenant_id,
                    user_id=owner_user_id,
                )
                memberships.append((foreign_tenant_id, foreign_user_id))
                persist_cleanup_recovery()
                await _bind_identity_to_tenant(
                    api,
                    tenant_id=foreign_tenant_id,
                    user_id=foreign_user_id,
                )
                memberships.append((foreign_tenant_id, owner_user_id))
                persist_cleanup_recovery()
                await _bind_identity_to_tenant(
                    api,
                    tenant_id=foreign_tenant_id,
                    user_id=owner_user_id,
                    roles=("student",),
                )
                owner_identity = await api.login_identity(
                    material.owner_username,
                    material.owner_password,
                )
                foreign_identity = await api.login_identity(
                    material.foreign_username,
                    material.foreign_password,
                )
                if (
                    owner_identity.user_id != owner_user_id
                    or foreign_identity.user_id != foreign_user_id
                ):
                    raise TenantIsolationProbeError("identity_login_failed")
                active_pair = await resolve_active_tenant_pair(
                    api,
                    config.capacity_tenant_ids,
                )
                cleanup_state = IsolationCleanupState(
                    tenant_id=active_pair[0],
                    user_id=owner_identity.user_id,
                )
                persist_cleanup_recovery()
                fixture = await _build_isolation_fixture(
                    api,
                    config=config,
                    owner_identity=owner_identity,
                    owner_tenant_id=active_pair[0],
                    cleanup_state=cleanup_state,
                    persist_cleanup_state=persist_cleanup_recovery,
                )
                observations = await verify_isolation_targets(
                    api,
                    owner_identity=owner_identity,
                    owner_tenant_id=active_pair[0],
                    foreign_identity=foreign_identity,
                    foreign_tenant_id=active_pair[1],
                    targets=fixture.targets,
                    document_ticket=fixture.document_ticket,
                    event_ticket=fixture.event_ticket,
                )
                report = {
                    "schemaVersion": TENANT_ISOLATION_SCHEMA_VERSION,
                    "producer": TENANT_ISOLATION_PRODUCER,
                    "candidate": config.candidate,
                    "releaseRun": config.release_run,
                    "observedAt": _observed_at(),
                    "baseUrl": config.base_url,
                    "capacityProof": {
                        "reportSha256": config.capacity_attestation_sha256,
                        "tenantIds": list(active_pair),
                    },
                    "principals": [
                        {
                            "tenantId": active_pair[0],
                            "actorId": owner_identity.user_id,
                            "role": "user",
                            "membershipStatus": "active",
                        },
                        {
                            "tenantId": active_pair[1],
                            "actorId": foreign_identity.user_id,
                            "role": "user",
                            "membershipStatus": "active",
                        },
                    ],
                    "crossTenantPrincipal": {
                        "tenantId": active_pair[1],
                        "actorId": owner_identity.user_id,
                        "role": "student",
                        "membershipStatus": "active",
                    },
                    "observations": observations,
                }
                body = canonical_tenant_isolation_report(report)
                forbidden = (
                    config.admin_token.get_secret_value().encode("utf-8"),
                    material.owner_password.get_secret_value().encode("utf-8"),
                    material.foreign_password.get_secret_value().encode("utf-8"),
                    owner_identity.token.get_secret_value().encode("utf-8"),
                    foreign_identity.token.get_secret_value().encode("utf-8"),
                    fixture.document_ticket.get_secret_value().encode("utf-8"),
                    fixture.event_ticket.get_secret_value().encode("utf-8"),
                )
                parsed = parse_tenant_isolation_report(
                    body,
                    candidate=config.candidate,
                    release_run=config.release_run,
                    expected_base_url=config.base_url,
                    expected_capacity_report_sha256=config.capacity_attestation_sha256,
                    expected_capacity_tenant_ids=active_pair,
                    forbidden_secret_values=forbidden,
                )
                checks = derive_tenant_isolation_checks(parsed)
                if not checks or any(value is not True for value in checks.values()):
                    raise TenantIsolationProbeError("tenant_isolation_failed")
        except TimeoutError:
            primary_failure = TenantIsolationProbeError("tenant_isolation_probe_timeout")
        except KeyboardInterrupt as exc:
            primary_failure = exc
        except Exception as exc:
            primary_failure = exc

        cleanup_failed = await _cleanup_isolation_state(
            api,
            cleanup_state=cleanup_state,
            owner_identity=owner_identity,
            owner_user_id=owner_user_id,
            memberships=memberships,
            created=created,
            checkpoint=checkpoint_cleanup_recovery,
        )
        if not cleanup_failed:
            try:
                _write_cleanup_recovery_state(
                    config,
                    attempt_id=attempt_id,
                    material=material,
                    cleanup_state=None,
                    memberships=(),
                    created=(),
                )
                _remove_cleanup_recovery_state(config)
            except TenantIsolationProbeError:
                cleanup_failed = True

    if primary_failure is not None:
        if isinstance(primary_failure, KeyboardInterrupt):
            raise primary_failure
        if cleanup_failed:
            raise TenantIsolationProbeError(
                "tenant_isolation_probe_and_cleanup_failed"
            ) from primary_failure
        raise primary_failure
    if cleanup_failed:
        raise TenantIsolationProbeError("tenant_isolation_cleanup_failed")
    if body is None:
        raise TenantIsolationProbeError("tenant_isolation_probe_failed")
    return body


def _write_stdout_report(body: bytes) -> None:
    _validate_report_for_stdout(body)
    try:
        written = sys.stdout.buffer.write(body)
        if written != len(body):
            raise OSError("short stdout write")
        sys.stdout.buffer.flush()
    except OSError as exc:
        raise TenantIsolationProbeError("tenant_isolation_stdout_failed") from exc


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _parse_args(argv)
        config = _load_config(os.environ, cwd=Path.cwd())
        body = asyncio.run(_run_tenant_isolation_probe(config))
        _write_stdout_report(body)
    except TenantIsolationProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("tenant_isolation_probe_interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("tenant_isolation_probe_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
