"""Run one candidate-bound OpenMAIC shared-plane generation smoke probe."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any, Callable, NamedTuple
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from openmaic_smoke_contract import (  # noqa: E402
    OPENMAIC_SMOKE_PRODUCER,
    OPENMAIC_SMOKE_SCHEMA_VERSION,
    canonical_openmaic_smoke_report,
    parse_openmaic_smoke_report,
)
from render_platform_compose import validate_image_lock_bindings  # noqa: E402

_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOCAL_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_RESPONSE_BYTES = 1024 * 1024
_MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
_TITLE = "OpenMAIC shared-plane acceptance"
_SHARED_BINDING = {
    "routeId": "shared-primary",
    "providerProfileId": "platform-default",
    "workerPoolRef": "shared-generation",
    "queueRef": "openmaic.shared",
}


class OpenMAICSmokeProbeError(RuntimeError):
    """Stable, secret-free failure raised by the live probe."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise OpenMAICSmokeProbeError("arguments_invalid")


class ProbeConfig:
    __slots__ = (
        "admin_token",
        "base_url",
        "candidate",
        "candidate_root",
        "release_run",
        "runtime_attestation_sha256",
        "timeout_seconds",
    )

    def __init__(
        self,
        *,
        admin_token: SecretStr,
        base_url: str,
        candidate: Mapping[str, object],
        candidate_root: Path,
        release_run: Mapping[str, str],
        runtime_attestation_sha256: str,
        timeout_seconds: int,
    ) -> None:
        self.admin_token = admin_token
        self.base_url = base_url
        self.candidate = dict(candidate)
        self.candidate_root = candidate_root
        self.release_run = dict(release_run)
        self.runtime_attestation_sha256 = runtime_attestation_sha256
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            "ProbeConfig(admin_token=SecretStr('**********'), "
            f"base_url={self.base_url!r}, candidate_root={self.candidate_root!r}, "
            f"release_run={self.release_run!r}, "
            f"runtime_attestation_sha256={self.runtime_attestation_sha256!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


class _FixtureMaterial(NamedTuple):
    teacher_username: str
    teacher_password: SecretStr
    tenant_idempotency_key: str
    tenant_name: str
    resource_suffix: str
    classroom_idempotency_key: str
    quota_idempotency_key: str


class _DocumentEvidence(NamedTuple):
    body: bytes
    sha256: str
    etag: str


class _FixtureCleanupState:
    __slots__ = (
        "class_id",
        "enrollment_attempted",
        "identity_attempted",
        "membership_attempted",
        "teacher_user_id",
        "teacher_username",
        "tenant_id",
    )

    def __init__(self, *, tenant_id: str, teacher_username: str) -> None:
        self.tenant_id = tenant_id
        self.teacher_username = teacher_username
        self.teacher_user_id: str | None = None
        self.class_id: str | None = None
        self.identity_attempted = False
        self.membership_attempted = False
        self.enrollment_attempted = False


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
        raise OpenMAICSmokeProbeError("candidate_invalid")
    try:
        copied = json.loads(json.dumps(candidate, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise OpenMAICSmokeProbeError("candidate_invalid") from exc
    if not isinstance(copied, dict):
        raise OpenMAICSmokeProbeError("candidate_invalid")
    return copied


def _valid_base_url(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.rstrip("/"):
        return False
    parsed = urlsplit(value)
    if parsed.scheme == "http":
        try:
            if parsed.hostname is None or not ipaddress.ip_address(parsed.hostname).is_loopback:
                return False
        except ValueError:
            return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _public_id(value: object, error: str) -> str:
    if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
        raise OpenMAICSmokeProbeError(error)
    return value


def _local_user_id(value: object, error: str) -> str:
    if not isinstance(value, str) or _LOCAL_USER_ID.fullmatch(value) is None:
        raise OpenMAICSmokeProbeError(error)
    return value


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OpenMAICSmokeProbeError("fixture_derivation_invalid") from exc


def _resource_suffix(run_id: str) -> str:
    prefix = "run-openmaic-"
    candidate = run_id.removeprefix(prefix) if run_id.startswith(prefix) else ""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", candidate or ""):
        return candidate
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:20]


def _fixture_material(config: ProbeConfig) -> _FixtureMaterial:
    source_head = config.candidate.get("sourceHead")
    run_id = config.release_run.get("runId")
    environment_id = config.release_run.get("environmentId")
    if (
        not isinstance(source_head, str)
        or not isinstance(run_id, str)
        or not isinstance(environment_id, str)
    ):
        raise OpenMAICSmokeProbeError("release_identity_invalid")

    tenant_binding = f"{source_head}\0{run_id}\0tenant".encode("utf-8")
    tenant_key = f"openmaic-shared-{hashlib.sha256(tenant_binding).hexdigest()[:24]}"
    nonce = secrets.token_bytes(16)
    credential_binding = _json_bytes(
        {
            "candidate": config.candidate,
            "releaseRun": config.release_run,
            "nonceSha256": hashlib.sha256(nonce).hexdigest(),
        }
    )
    credential_digest = hmac.new(
        config.admin_token.get_secret_value().encode("utf-8"),
        credential_binding,
        hashlib.sha256,
    ).hexdigest()
    suffix = f"{_resource_suffix(run_id)}-{credential_digest[:8]}"
    return _FixtureMaterial(
        teacher_username=f"openmaic-shared-{credential_digest[:24]}",
        teacher_password=SecretStr(f"Oms-{credential_digest[24:56]}-Aa7!"),
        tenant_idempotency_key=tenant_key,
        tenant_name=f"{_TITLE} {tenant_key.removeprefix('openmaic-shared-')}",
        resource_suffix=suffix,
        classroom_idempotency_key=f"openmaic-shared-{credential_digest[:24]}-classroom",
        quota_idempotency_key=f"openmaic-shared-{credential_digest[:24]}-quota",
    )


class _OpenMAICSmokeApi:
    """Redirect-free boundary separating the admin token from teacher cookies."""

    def __init__(
        self,
        *,
        base_url: str,
        admin_token: str,
        timeout_seconds: int,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        timeout = httpx.Timeout(float(timeout_seconds))
        self._admin = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {admin_token}"},
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )
        self._teacher = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> _OpenMAICSmokeApi:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._admin.aclose()
        await self._teacher.aclose()

    @staticmethod
    def _path(path: str) -> str:
        if not path.startswith("/api/v1/") or path.startswith("//"):
            raise OpenMAICSmokeProbeError("request_path_invalid")
        return path

    async def _response(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        expected_status: int,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        max_bytes: int = _MAX_JSON_RESPONSE_BYTES,
    ) -> httpx.Response:
        kwargs: dict[str, object] = {"headers": dict(headers or {})}
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            response = await client.request(method, self._path(path), **kwargs)
        except httpx.HTTPError as exc:
            raise OpenMAICSmokeProbeError("candidate_request_failed") from exc
        if response.is_redirect or response.status_code != expected_status:
            raise OpenMAICSmokeProbeError("candidate_request_rejected")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise OpenMAICSmokeProbeError("candidate_response_invalid") from exc
            if declared_size < 0 or declared_size > max_bytes:
                raise OpenMAICSmokeProbeError("candidate_response_too_large")
        if len(response.content) > max_bytes:
            raise OpenMAICSmokeProbeError("candidate_response_too_large")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except (UnicodeError, ValueError) as exc:
            raise OpenMAICSmokeProbeError("candidate_response_invalid") from exc
        if not isinstance(body, dict):
            raise OpenMAICSmokeProbeError("candidate_response_invalid")
        return body

    async def admin_json(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        tenant_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> dict[str, Any]:
        bound_headers = dict(headers or {})
        if tenant_id is not None:
            bound_headers["X-Tenant-ID"] = tenant_id
        return self._json(
            await self._response(
                self._admin,
                method,
                path,
                expected_status=expected_status,
                headers=bound_headers,
                json_body=json_body,
            )
        )

    async def _admin_users(self) -> list[dict[str, Any]]:
        response = await self._response(
            self._admin,
            "GET",
            "/api/v1/auth/users",
            expected_status=200,
        )
        try:
            body = response.json()
        except (UnicodeError, ValueError) as exc:
            raise OpenMAICSmokeProbeError("candidate_response_invalid") from exc
        if not isinstance(body, list) or any(not isinstance(item, dict) for item in body):
            raise OpenMAICSmokeProbeError("candidate_response_invalid")
        return body

    async def require_identity_absent(self, username: str) -> None:
        users = await self._admin_users()
        for user in users:
            current_username = user.get("username")
            if not isinstance(current_username, str):
                raise OpenMAICSmokeProbeError("candidate_response_invalid")
            if hmac.compare_digest(current_username, username):
                raise OpenMAICSmokeProbeError("teacher_identity_collision")

    async def _recover_identity_id(self, username: str) -> str | None:
        users = await self._admin_users()
        matches = [
            user
            for user in users
            if isinstance(user.get("username"), str)
            and hmac.compare_digest(user["username"], username)
        ]
        if not matches:
            return None
        if len(matches) != 1 or matches[0].get("role") != "user":
            raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
        return _local_user_id(matches[0].get("id"), "fixture_cleanup_failed")

    async def _cleanup_request(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        tenant_id: str | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        headers = {"X-Tenant-ID": tenant_id} if tenant_id is not None else {}
        kwargs: dict[str, object] = {"headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            response = await self._admin.request(method, self._path(path), **kwargs)
        except Exception as exc:
            raise OpenMAICSmokeProbeError("fixture_cleanup_failed") from exc
        if response.is_redirect:
            raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
        if response.status_code not in {expected_status, 404}:
            raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise OpenMAICSmokeProbeError("fixture_cleanup_failed") from exc
            if declared_size < 0 or declared_size > _MAX_JSON_RESPONSE_BYTES:
                raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
        if len(response.content) > _MAX_JSON_RESPONSE_BYTES:
            raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
        return response

    async def _cleanup_no_content(
        self,
        path: str,
        *,
        tenant_id: str,
        not_found_detail: str,
        json_body: object | None = None,
    ) -> None:
        for attempt in range(2):
            try:
                response = await self._cleanup_request(
                    "DELETE",
                    path,
                    expected_status=204,
                    tenant_id=tenant_id,
                    json_body=json_body,
                )
            except OpenMAICSmokeProbeError:
                if attempt == 0:
                    continue
                raise
            if response.status_code == 204 and not response.content:
                return
            if response.status_code == 404:
                try:
                    tombstone = self._json(response)
                except OpenMAICSmokeProbeError as exc:
                    raise OpenMAICSmokeProbeError("fixture_cleanup_failed") from exc
                if tombstone == {"detail": not_found_detail}:
                    return
            raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
        raise OpenMAICSmokeProbeError("fixture_cleanup_failed")

    async def _cleanup_identity_with_reconciliation(
        self,
        *,
        username: str,
        expected_user_id: str,
    ) -> None:
        for attempt in range(2):
            try:
                response = await self._cleanup_request(
                    "DELETE",
                    f"/api/v1/auth/users/{quote(username, safe='')}",
                    expected_status=200,
                    json_body={"expected_user_id": expected_user_id},
                )
            except OpenMAICSmokeProbeError:
                response = None
            if response is not None:
                if response.status_code == 200:
                    try:
                        body = self._json(response)
                    except OpenMAICSmokeProbeError as exc:
                        raise OpenMAICSmokeProbeError("fixture_cleanup_failed") from exc
                    if body == {"ok": True}:
                        return
                    raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
                if response.status_code == 404:
                    try:
                        tombstone = self._json(response)
                    except OpenMAICSmokeProbeError as exc:
                        raise OpenMAICSmokeProbeError("fixture_cleanup_failed") from exc
                    if tombstone != {"detail": "User not found"}:
                        raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
            observed_user_id = await self._recover_identity_id(username)
            if observed_user_id is None:
                return
            if not hmac.compare_digest(observed_user_id, expected_user_id) or attempt == 1:
                raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
        raise OpenMAICSmokeProbeError("fixture_cleanup_failed")

    async def cleanup_fixture(self, state: _FixtureCleanupState) -> None:
        try:
            if not state.identity_attempted:
                return
            user_id = state.teacher_user_id
            if user_id is None:
                user_id = await self._recover_identity_id(state.teacher_username)
            if user_id is None:
                if state.membership_attempted or state.enrollment_attempted:
                    raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
                return
            if state.enrollment_attempted:
                if state.class_id is None:
                    raise OpenMAICSmokeProbeError("fixture_cleanup_failed")
                await self._cleanup_no_content(
                    f"/api/v1/teaching/classes/{quote(state.class_id, safe='')}/enrollments/"
                    f"{quote(user_id, safe='')}",
                    tenant_id=state.tenant_id,
                    not_found_detail="enrollment not found",
                )
            if state.membership_attempted:
                await self._cleanup_no_content(
                    f"/api/v1/tenants/{quote(state.tenant_id, safe='')}/members/"
                    f"{quote(user_id, safe='')}",
                    tenant_id=state.tenant_id,
                    not_found_detail="Tenant membership not found",
                    json_body={
                        "expected_tenant_id": state.tenant_id,
                        "expected_user_id": user_id,
                    },
                )
            await self._cleanup_identity_with_reconciliation(
                username=state.teacher_username,
                expected_user_id=user_id,
            )
        except OpenMAICSmokeProbeError as exc:
            if str(exc) == "fixture_cleanup_failed":
                raise
            raise OpenMAICSmokeProbeError("fixture_cleanup_failed") from exc
        except Exception as exc:
            raise OpenMAICSmokeProbeError("fixture_cleanup_failed") from exc

    async def _select_tenant(
        self,
        client: httpx.AsyncClient,
        *,
        tenant_id: str,
        error: str,
    ) -> None:
        response = await self._response(
            client,
            "PUT",
            "/api/v1/tenants/active",
            expected_status=200,
            json_body={"tenant_id": tenant_id},
        )
        if (
            self._json(response) != {"active_tenant_id": tenant_id}
            or client.cookies.get("dt_tenant") != tenant_id
        ):
            raise OpenMAICSmokeProbeError(error)

    async def select_admin_tenant(self, tenant_id: str) -> None:
        await self._select_tenant(
            self._admin,
            tenant_id=tenant_id,
            error="admin_tenant_selection_invalid",
        )

    async def select_teacher_tenant(self, tenant_id: str) -> None:
        await self._select_tenant(
            self._teacher,
            tenant_id=tenant_id,
            error="teacher_tenant_selection_invalid",
        )

    async def login(self, *, username: str, password: SecretStr) -> tuple[str, SecretStr]:
        response = await self._response(
            self._teacher,
            "POST",
            "/api/v1/auth/login",
            expected_status=200,
            json_body={"username": username, "password": password.get_secret_value()},
        )
        body = self._json(response)
        user_id = _public_id(body.get("user_id"), "teacher_login_invalid")
        session = self._teacher.cookies.get("dt_token")
        if (
            body.get("ok") is not True
            or body.get("username") != username
            or body.get("role") != "user"
            or body.get("is_admin") is not False
            or not isinstance(session, str)
            or not session
            or len(session) > 8192
        ):
            raise OpenMAICSmokeProbeError("teacher_login_invalid")
        return user_id, SecretStr(session)

    async def teacher_json(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        tenant_id: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> dict[str, Any]:
        bound_headers = dict(headers or {})
        bound_headers["X-Tenant-ID"] = tenant_id
        return self._json(
            await self._response(
                self._teacher,
                method,
                path,
                expected_status=expected_status,
                headers=bound_headers,
                json_body=json_body,
            )
        )

    async def teacher_document(
        self,
        path: str,
        *,
        tenant_id: str,
        asset_id: str,
        classroom_version_id: str,
    ) -> _DocumentEvidence:
        response = await self._response(
            self._teacher,
            "GET",
            path,
            expected_status=200,
            headers={"X-Tenant-ID": tenant_id},
            max_bytes=_MAX_DOCUMENT_BYTES,
        )
        if not response.content:
            raise OpenMAICSmokeProbeError("classroom_document_invalid")
        media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise OpenMAICSmokeProbeError("classroom_document_invalid")
        body = bytes(response.content)
        digest = hashlib.sha256(body).hexdigest()
        if (
            response.headers.get("Content-Length") != str(len(body))
            or response.headers.get("ETag") != f'"sha256-{digest}"'
        ):
            raise OpenMAICSmokeProbeError("classroom_document_invalid")
        try:
            document = json.loads(body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OpenMAICSmokeProbeError("classroom_document_invalid") from exc
        openmaic = document.get("openmaic") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("schemaVersion") != "1.0"
            or document.get("classroomId") != asset_id
            or document.get("classroomVersionId") != classroom_version_id
            or not isinstance(openmaic, dict)
            or openmaic.get("dslVersion") != "0.1.0"
        ):
            raise OpenMAICSmokeProbeError("classroom_document_invalid")
        return _DocumentEvidence(body=body, sha256=digest, etag=f'"sha256-{digest}"')


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OpenMAICSmokeProbeError("probe_timeout")
    return remaining


async def _wait_for_tenant(
    api: _OpenMAICSmokeApi,
    *,
    tenant_id: str,
    job_id: str,
    deadline: float,
) -> None:
    while True:
        body = await api.admin_json(
            "GET",
            f"/api/v1/tenants/{tenant_id}/provisioning",
            expected_status=200,
        )
        if body.get("tenant_id") != tenant_id or body.get("job_id") != job_id:
            raise OpenMAICSmokeProbeError("tenant_provisioning_invalid")
        if body.get("status") == "active" and body.get("job_status") == "completed":
            return
        if body.get("status") in {"failed", "deleting", "deleted"} or body.get("job_status") in {
            "failed",
            "canceled",
        }:
            raise OpenMAICSmokeProbeError("tenant_provisioning_failed")
        await asyncio.sleep(min(0.25, _remaining(deadline)))


async def _wait_for_content_job(
    api: _OpenMAICSmokeApi,
    *,
    tenant_id: str,
    job_id: str,
    deadline: float,
) -> None:
    while True:
        body = await api.teacher_json(
            "GET",
            f"/api/v1/classroom-jobs/{job_id}",
            expected_status=200,
            tenant_id=tenant_id,
        )
        if (
            body.get("job_id") != job_id
            or body.get("job_kind") != "generation"
            or body.get("phase") != "content"
        ):
            raise OpenMAICSmokeProbeError("generation_job_invalid")
        status = body.get("status")
        progress = body.get("progress_percent")
        if status == "succeeded":
            if type(progress) is not int or progress != 100:
                raise OpenMAICSmokeProbeError("generation_job_invalid")
            return
        if status in {"failed", "canceled"}:
            raise OpenMAICSmokeProbeError("generation_job_failed")
        if type(progress) is not int or not 0 <= progress < 100:
            raise OpenMAICSmokeProbeError("generation_job_invalid")
        await asyncio.sleep(min(0.25, _remaining(deadline)))


async def _wait_for_outline_job(
    api: _OpenMAICSmokeApi,
    *,
    tenant_id: str,
    job_id: str,
    deadline: float,
) -> None:
    while True:
        body = await api.teacher_json(
            "GET",
            f"/api/v1/classroom-jobs/{job_id}",
            expected_status=200,
            tenant_id=tenant_id,
        )
        if (
            body.get("job_id") != job_id
            or body.get("job_kind") != "generation"
            or body.get("phase") != "outline"
        ):
            raise OpenMAICSmokeProbeError("outline_job_invalid")
        status = body.get("status")
        progress = body.get("progress_percent")
        if status == "awaiting_confirmation":
            if type(progress) is not int or not 0 <= progress <= 100:
                raise OpenMAICSmokeProbeError("outline_job_invalid")
            if not isinstance(body.get("outline"), dict):
                raise OpenMAICSmokeProbeError("outline_job_invalid")
            return
        if status in {"failed", "canceled"}:
            raise OpenMAICSmokeProbeError("outline_job_failed")
        if status not in {"created", "quota_reserved", "queued", "generating_outline"}:
            raise OpenMAICSmokeProbeError("outline_job_invalid")
        if type(progress) is not int or not 0 <= progress < 100:
            raise OpenMAICSmokeProbeError("outline_job_invalid")
        await asyncio.sleep(min(0.25, _remaining(deadline)))


async def _wait_for_outline_classroom(
    api: _OpenMAICSmokeApi,
    *,
    tenant_id: str,
    asset_id: str,
    job_id: str,
    owner_id: str,
    deadline: float,
) -> None:
    while True:
        body = await api.teacher_json(
            "GET",
            f"/api/v1/classrooms/{asset_id}",
            expected_status=200,
            tenant_id=tenant_id,
        )
        if (
            body.get("assetId") != asset_id
            or body.get("jobId") != job_id
            or body.get("ownerId") != owner_id
        ):
            raise OpenMAICSmokeProbeError("classroom_outline_invalid")
        status = body.get("status")
        lifecycle = body.get("lifecycleState")
        if (
            status == "awaiting_confirmation"
            and lifecycle == "awaiting_outline"
            and isinstance(body.get("outline"), dict)
        ):
            return
        if status in {"failed", "canceled"}:
            raise OpenMAICSmokeProbeError("classroom_outline_failed")
        if status not in {"created", "quota_reserved", "queued", "generating_outline"}:
            raise OpenMAICSmokeProbeError("classroom_outline_invalid")
        if lifecycle != "generating_outline" or body.get("outline") is not None:
            raise OpenMAICSmokeProbeError("classroom_outline_invalid")
        await asyncio.sleep(min(0.25, _remaining(deadline)))


async def _wait_for_generated_classroom(
    api: _OpenMAICSmokeApi,
    *,
    tenant_id: str,
    asset_id: str,
    job_id: str,
    owner_id: str,
    deadline: float,
) -> tuple[dict[str, Any], str]:
    while True:
        body = await api.teacher_json(
            "GET",
            f"/api/v1/classrooms/{asset_id}",
            expected_status=200,
            tenant_id=tenant_id,
        )
        if (
            body.get("assetId") != asset_id
            or body.get("jobId") != job_id
            or body.get("ownerId") != owner_id
        ):
            raise OpenMAICSmokeProbeError("classroom_generation_invalid")
        if body.get("status") in {"failed", "canceled"}:
            raise OpenMAICSmokeProbeError("classroom_generation_failed")
        if (
            body.get("status") == "succeeded"
            and body.get("lifecycleState") == "editing"
            and isinstance(body.get("document"), dict)
        ):
            return body, _public_id(
                body.get("classroomVersionId"),
                "classroom_generation_invalid",
            )
        await asyncio.sleep(min(0.25, _remaining(deadline)))


async def _run_openmaic_smoke_probe(
    config: ProbeConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """Create an isolated fixture and prove one materialized shared-plane generation."""

    material = _fixture_material(config)
    deadline = time.monotonic() + config.timeout_seconds
    admin_token = config.admin_token.get_secret_value()
    if not admin_token:
        raise OpenMAICSmokeProbeError("fixture_token_unavailable")

    async with (
        _OpenMAICSmokeApi(
            base_url=config.base_url,
            admin_token=admin_token,
            timeout_seconds=config.timeout_seconds,
            transport=transport,
        ) as api,
        AsyncExitStack() as fixture_cleanup,
    ):
        created_tenant = await api.admin_json(
            "POST",
            "/api/v1/tenants",
            expected_status=202,
            headers={"Idempotency-Key": material.tenant_idempotency_key},
            json_body={"name": material.tenant_name},
        )
        tenant_id = _public_id(created_tenant.get("tenant_id"), "tenant_create_invalid")
        provisioning_job_id = _public_id(created_tenant.get("job_id"), "tenant_create_invalid")
        if created_tenant.get("status") not in {"provisioning", "active"}:
            raise OpenMAICSmokeProbeError("tenant_create_invalid")
        await _wait_for_tenant(
            api,
            tenant_id=tenant_id,
            job_id=provisioning_job_id,
            deadline=deadline,
        )
        await api.select_admin_tenant(tenant_id)

        await api.require_identity_absent(material.teacher_username)
        cleanup_state = _FixtureCleanupState(
            tenant_id=tenant_id,
            teacher_username=material.teacher_username,
        )
        fixture_cleanup.push_async_callback(api.cleanup_fixture, cleanup_state)
        cleanup_state.identity_attempted = True

        created_user = await api.admin_json(
            "POST",
            "/api/v1/auth/users",
            expected_status=201,
            json_body={
                "username": material.teacher_username,
                "password": material.teacher_password.get_secret_value(),
            },
        )
        teacher_user_id = _local_user_id(
            created_user.get("user_id"),
            "teacher_create_invalid",
        )
        cleanup_state.teacher_user_id = teacher_user_id
        if (
            created_user.get("ok") is not True
            or created_user.get("username") != material.teacher_username
            or created_user.get("role") != "user"
            or created_user.get("is_admin") is not False
        ):
            raise OpenMAICSmokeProbeError("teacher_create_invalid")

        cleanup_state.membership_attempted = True
        membership = await api.admin_json(
            "POST",
            f"/api/v1/tenants/{tenant_id}/members",
            expected_status=200,
            tenant_id=tenant_id,
            json_body={"user_id": teacher_user_id, "role": "teacher"},
        )
        if (
            membership.get("tenant_id") != tenant_id
            or membership.get("user_id") != teacher_user_id
            or membership.get("roles") != ["teacher"]
            or membership.get("grants")
            != [{"role": "teacher", "scope_type": "tenant", "scope_id": tenant_id}]
        ):
            raise OpenMAICSmokeProbeError("teacher_membership_invalid")

        logged_in_user_id, teacher_session = await api.login(
            username=material.teacher_username,
            password=material.teacher_password,
        )
        if logged_in_user_id != teacher_user_id:
            raise OpenMAICSmokeProbeError("teacher_login_invalid")
        await api.select_teacher_tenant(tenant_id)

        course_id = f"course-{material.resource_suffix}"
        class_id = f"class-{material.resource_suffix}"
        course = await api.admin_json(
            "POST",
            "/api/v1/teaching/courses",
            expected_status=201,
            tenant_id=tenant_id,
            json_body={"id": course_id, "title": _TITLE},
        )
        if course.get("id") != course_id or course.get("status") != "active":
            raise OpenMAICSmokeProbeError("course_create_invalid")

        classroom = await api.teacher_json(
            "POST",
            f"/api/v1/teaching/courses/{course_id}/classes",
            expected_status=201,
            tenant_id=tenant_id,
            json_body={"id": class_id, "name": _TITLE},
        )
        if (
            classroom.get("id") != class_id
            or classroom.get("courseId") != course_id
            or classroom.get("status") != "active"
        ):
            raise OpenMAICSmokeProbeError("class_create_invalid")

        cleanup_state.class_id = class_id
        cleanup_state.enrollment_attempted = True
        enrollment = await api.teacher_json(
            "POST",
            f"/api/v1/teaching/classes/{class_id}/enrollments",
            expected_status=201,
            tenant_id=tenant_id,
            json_body={"userId": teacher_user_id},
        )
        if (
            enrollment.get("classId") != class_id
            or enrollment.get("userId") != teacher_user_id
            or enrollment.get("status") != "active"
        ):
            raise OpenMAICSmokeProbeError("enrollment_create_invalid")

        quota = await api.admin_json(
            "POST",
            "/api/v1/teaching/generation-quota-grants",
            expected_status=200,
            tenant_id=tenant_id,
            headers={"Idempotency-Key": material.quota_idempotency_key},
            json_body={"units": 20},
        )
        if (
            quota.get("tenantId") != tenant_id
            or quota.get("units") != 20
            or type(quota.get("balance")) is not int
            or quota["balance"] < 20
        ):
            raise OpenMAICSmokeProbeError("generation_quota_invalid")

        created_asset = await api.teacher_json(
            "POST",
            "/api/v1/classrooms",
            expected_status=202,
            tenant_id=tenant_id,
            headers={"Idempotency-Key": material.classroom_idempotency_key},
            json_body={
                "title": _TITLE,
                "courseId": course_id,
                "classId": class_id,
                "objective": "Prove one real OpenMAIC shared-plane classroom generation",
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
                        "knowledgePointId": "kp-openmaic-shared-plane",
                        "title": "OpenMAIC shared-plane generation",
                        "description": "Verify canonical shared-plane materialization",
                    }
                ],
                "contentMode": "open_creation",
                "openCreationAcknowledged": True,
                "requestedExports": ["offline_html"],
            },
        )
        asset_id = _public_id(created_asset.get("assetId"), "classroom_create_invalid")
        job_id = _public_id(created_asset.get("jobId"), "classroom_create_invalid")
        creation_pending = (
            created_asset.get("status")
            in {"created", "quota_reserved", "queued", "generating_outline"}
            and created_asset.get("lifecycleState") == "generating_outline"
            and created_asset.get("outline") is None
        )
        creation_ready = (
            created_asset.get("status") == "awaiting_confirmation"
            and created_asset.get("lifecycleState") == "awaiting_outline"
            and isinstance(created_asset.get("outline"), dict)
        )
        if created_asset.get("ownerId") != teacher_user_id or not (
            creation_pending or creation_ready
        ):
            raise OpenMAICSmokeProbeError("classroom_create_invalid")

        await _wait_for_outline_job(
            api,
            tenant_id=tenant_id,
            job_id=job_id,
            deadline=deadline,
        )
        await _wait_for_outline_classroom(
            api,
            tenant_id=tenant_id,
            asset_id=asset_id,
            job_id=job_id,
            owner_id=teacher_user_id,
            deadline=deadline,
        )

        confirmed = await api.teacher_json(
            "POST",
            f"/api/v1/classrooms/{asset_id}/confirm-outline",
            expected_status=202,
            tenant_id=tenant_id,
        )
        if (
            confirmed.get("assetId") != asset_id
            or confirmed.get("jobId") != job_id
            or confirmed.get("ownerId") != teacher_user_id
            or confirmed.get("lifecycleState") != "generating_content"
        ):
            raise OpenMAICSmokeProbeError("outline_confirmation_invalid")

        await _wait_for_content_job(
            api,
            tenant_id=tenant_id,
            job_id=job_id,
            deadline=deadline,
        )
        generated, classroom_version_id = await _wait_for_generated_classroom(
            api,
            tenant_id=tenant_id,
            asset_id=asset_id,
            job_id=job_id,
            owner_id=teacher_user_id,
            deadline=deadline,
        )

        binding = await api.admin_json(
            "GET",
            f"/api/v1/system/classroom-jobs/{tenant_id}/{job_id}/binding",
            expected_status=200,
        )
        expected_binding_response = {
            "schemaVersion": 1,
            "tenantId": tenant_id,
            "jobId": job_id,
            "jobKind": "generation",
            "phase": "content",
            "status": "succeeded",
            "progressPercent": 100,
            "classroomVersionId": classroom_version_id,
            "dataPlaneRouteId": _SHARED_BINDING["routeId"],
            "providerProfileId": _SHARED_BINDING["providerProfileId"],
            "workerPoolRef": _SHARED_BINDING["workerPoolRef"],
            "queueRef": _SHARED_BINDING["queueRef"],
        }
        if binding != expected_binding_response:
            raise OpenMAICSmokeProbeError("shared_binding_invalid")

        document = await api.teacher_document(
            f"/api/v1/classroom-versions/{classroom_version_id}/document",
            tenant_id=tenant_id,
            asset_id=asset_id,
            classroom_version_id=classroom_version_id,
        )

    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report = {
        "schemaVersion": OPENMAIC_SMOKE_SCHEMA_VERSION,
        "producer": OPENMAIC_SMOKE_PRODUCER,
        "plane": "shared",
        "candidate": config.candidate,
        "releaseRun": config.release_run,
        "observedAt": observed_at,
        "baseUrl": config.base_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": config.runtime_attestation_sha256,
        },
        "fixture": {
            "tenantId": tenant_id,
            "teacherUserId": teacher_user_id,
            "courseId": course_id,
            "classId": class_id,
        },
        "binding": dict(_SHARED_BINDING),
        "generation": {
            "jobId": job_id,
            "jobStatus": "succeeded",
            "assetId": asset_id,
            "classroomStatus": generated["status"],
            "classroomVersionId": classroom_version_id,
            "documentSha256": document.sha256,
            "documentSizeBytes": len(document.body),
            "documentEtag": document.etag,
        },
    }
    body = canonical_openmaic_smoke_report(report)
    parse_openmaic_smoke_report(
        body,
        candidate=config.candidate,
        release_run=config.release_run,
        expected_base_url=config.base_url,
        expected_runtime_attestation_sha256=config.runtime_attestation_sha256,
        forbidden_secret_values=(
            admin_token.encode("utf-8"),
            material.teacher_password.get_secret_value().encode("utf-8"),
            teacher_session.get_secret_value().encode("utf-8"),
        ),
    )
    return body


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _StableArgumentParser(description=__doc__)
    parser.add_argument("--plane", required=True, choices=("shared",))
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
        raise OpenMAICSmokeProbeError("candidate_root_invalid")
    try:
        candidate_root = Path(raw_root).resolve(strict=True)
        current_root = Path(cwd).resolve(strict=True)
    except OSError as exc:
        raise OpenMAICSmokeProbeError("candidate_root_invalid") from exc
    if candidate_root != current_root:
        raise OpenMAICSmokeProbeError("candidate_root_invalid")

    token = environment.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise OpenMAICSmokeProbeError("fixture_token_unavailable")
    token = token.strip()

    release_run: dict[str, str] = {}
    for field, name in (
        ("runId", "YFEISTAI_RELEASE_RUN_ID"),
        ("environmentId", "YFEISTAI_ENVIRONMENT_ID"),
    ):
        value = environment.get(name)
        if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
            raise OpenMAICSmokeProbeError("release_identity_invalid")
        release_run[field] = value

    base_url = environment.get("WEB_BASE_URL")
    if not _valid_base_url(base_url):
        raise OpenMAICSmokeProbeError("base_url_invalid")
    assert isinstance(base_url, str)

    runtime_sha256 = environment.get("YFEISTAI_RUNTIME_ATTESTATION_SHA256")
    if (
        not isinstance(runtime_sha256, str)
        or _SHA256.fullmatch(runtime_sha256) is None
        or runtime_sha256 == "0" * 64
    ):
        raise OpenMAICSmokeProbeError("runtime_attestation_invalid")

    raw_timeout = environment.get("YFEISTAI_OPENMAIC_SMOKE_TIMEOUT_SECONDS")
    try:
        timeout_seconds = int(raw_timeout or "")
    except ValueError as exc:
        raise OpenMAICSmokeProbeError("timeout_invalid") from exc
    if timeout_seconds < 30 or timeout_seconds > 86_400:
        raise OpenMAICSmokeProbeError("timeout_invalid")

    try:
        candidate = dict(candidate_loader(candidate_root))
    except OpenMAICSmokeProbeError:
        raise
    except Exception as exc:
        raise OpenMAICSmokeProbeError("candidate_invalid") from exc
    if not candidate:
        raise OpenMAICSmokeProbeError("candidate_invalid")

    return ProbeConfig(
        admin_token=SecretStr(token),
        base_url=base_url,
        candidate=candidate,
        candidate_root=candidate_root,
        release_run=release_run,
        runtime_attestation_sha256=runtime_sha256,
        timeout_seconds=timeout_seconds,
    )


async def _run_main(config: ProbeConfig) -> bytes:
    try:
        return await _run_openmaic_smoke_probe(config)
    except OpenMAICSmokeProbeError:
        raise
    except (OSError, UnicodeError, ValueError, httpx.HTTPError) as exc:
        raise OpenMAICSmokeProbeError("probe_failed") from exc


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _parse_args(argv)
        config = _load_config(os.environ, cwd=Path.cwd())
        body = asyncio.run(_run_main(config))
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()
        return 0
    except (OpenMAICSmokeProbeError, KeyboardInterrupt):
        sys.stderr.write("openmaic_smoke_probe_failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
