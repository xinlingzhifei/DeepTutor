"""Run the fixed, candidate-bound first-release classroom capacity probe."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any, NamedTuple, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from capacity_profile_contract import (  # noqa: E402
    CAPACITY_MODEL,
    CAPACITY_PRODUCER,
    CAPACITY_PROFILE,
    CAPACITY_RESOURCE_PHASES,
    CAPACITY_RESOURCE_SOURCE,
    CAPACITY_SCHEMA_VERSION,
    CAPACITY_WORKLOAD,
    canonical_capacity_profile_report,
    derive_capacity_profile_summary,
    parse_capacity_profile_report,
)
from render_platform_compose import validate_image_lock_bindings  # noqa: E402

_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
MAX_SCHEDULER_OBSERVATIONS = 256
_CLEANUP_TIMEOUT_SECONDS = 20.0
_CLEANUP_CONCURRENCY = 20


class CapacityProbeError(RuntimeError):
    """A fixed, secret-free probe failure safe to write to stderr."""


class ProbeConfig:
    __slots__ = (
        "admin_token",
        "base_url",
        "candidate",
        "candidate_root",
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
        release_run: Mapping[str, str],
        timeout_seconds: int,
    ) -> None:
        self.admin_token = admin_token
        self.base_url = base_url
        self.candidate = dict(candidate)
        self.candidate_root = candidate_root
        self.release_run = dict(release_run)
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            "ProbeConfig(admin_token=SecretStr('**********'), "
            f"base_url={self.base_url!r}, candidate_root={self.candidate_root!r}, "
            f"release_run={self.release_run!r}, timeout_seconds={self.timeout_seconds!r})"
        )


class SchedulerSnapshot(NamedTuple):
    observed_at: str
    active: tuple[tuple[str, str], ...]
    claim_events: tuple[tuple[int, str, str], ...]
    global_capacity: int | None
    tenant_capacities: tuple[tuple[str, int], ...]


class QuizEvidence(NamedTuple):
    scene_id: str
    question_id: str
    knowledge_point_id: str
    answer: list[str]


class IdentityCredential(NamedTuple):
    username: str
    user_id: str
    token: SecretStr


class IdentityMaterial(NamedTuple):
    run_key: str
    student_username: str
    student_password: SecretStr
    report_username: str
    report_password: SecretStr


class TenantFixture(NamedTuple):
    sequence: int
    tenant_id: str
    course_id: str
    class_id: str


class GenerationFixture(NamedTuple):
    sequence: int
    tenant_id: str
    asset_id: str
    job_id: str
    course_id: str | None = None
    class_id: str | None = None
    owner_id: str | None = None


class ReadyClassroom(NamedTuple):
    sequence: int
    tenant_id: str
    asset_id: str
    job_id: str
    version_id: str


class SessionFixture(NamedTuple):
    sequence: int
    tenant_id: str
    asset_id: str
    version_id: str
    session_id: str


class ReportSnapshot(NamedTuple):
    valid_quiz_count: int
    correct_quiz_count: int
    evidence_count: int


class _StartGate:
    __slots__ = ("_arrived", "_event", "_parties")

    def __init__(self, parties: int) -> None:
        if parties <= 0:
            raise ValueError("start gate requires at least one party")
        self._arrived = 0
        self._event = asyncio.Event()
        self._parties = parties

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived > self._parties:
            raise CapacityProbeError("learning_session_invalid")
        if self._arrived == self._parties:
            self._event.set()
        await self._event.wait()


_T = TypeVar("_T")


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
        raise CapacityProbeError("candidate_invalid")
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


def _load_config(
    environ: Mapping[str, str],
    *,
    cwd: Path,
    candidate_loader: CandidateLoader = _default_candidate_loader,
) -> ProbeConfig:
    raw_root = environ.get("YFEISTAI_CANDIDATE_ROOT")
    if not isinstance(raw_root, str) or not raw_root:
        raise CapacityProbeError("candidate_root_invalid")
    candidate_root = Path(raw_root)
    try:
        candidate_root = candidate_root.resolve(strict=True)
        current_root = Path(cwd).resolve(strict=True)
    except OSError as exc:
        raise CapacityProbeError("candidate_root_invalid") from exc
    if candidate_root != current_root:
        raise CapacityProbeError("candidate_root_invalid")

    token = environ.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise CapacityProbeError("fixture_token_unavailable")
    token = token.strip()

    release_run: dict[str, str] = {}
    for field, name in (
        ("runId", "YFEISTAI_RELEASE_RUN_ID"),
        ("environmentId", "YFEISTAI_ENVIRONMENT_ID"),
    ):
        value = environ.get(name)
        if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
            raise CapacityProbeError("release_identity_invalid")
        release_run[field] = value

    base_url = environ.get("WEB_BASE_URL")
    if not _valid_base_url(base_url):
        raise CapacityProbeError("base_url_invalid")
    assert isinstance(base_url, str)

    raw_timeout = environ.get("YFEISTAI_CAPACITY_TIMEOUT_SECONDS")
    try:
        timeout_seconds = int(raw_timeout or "")
    except ValueError as exc:
        raise CapacityProbeError("timeout_invalid") from exc
    if timeout_seconds < 60 or timeout_seconds > 86_400:
        raise CapacityProbeError("timeout_invalid")

    try:
        candidate = dict(candidate_loader(candidate_root))
    except CapacityProbeError:
        raise
    except Exception as exc:
        raise CapacityProbeError("candidate_invalid") from exc
    if not candidate:
        raise CapacityProbeError("candidate_invalid")
    return ProbeConfig(
        admin_token=SecretStr(token),
        base_url=base_url,
        candidate=candidate,
        candidate_root=candidate_root,
        release_run=release_run,
        timeout_seconds=timeout_seconds,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("first-release",))
    return parser.parse_args(argv)


class CapacityApi:
    """Small fail-closed HTTP boundary for the deployed candidate."""

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url
        self._admin_token = SecretStr(admin_token)
        self._transport = transport
        self._admin_client = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            trust_env=False,
        )
        self._identity_client = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=int(CAPACITY_PROFILE["executedConcurrentSessions"]),
                max_keepalive_connections=int(CAPACITY_PROFILE["executedConcurrentSessions"]),
            ),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> CapacityApi:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._admin_client.aclose()
        await self._identity_client.aclose()

    @staticmethod
    def _response_json(
        response: httpx.Response,
        *,
        expected_statuses: frozenset[int],
    ) -> dict[str, Any]:
        if response.status_code not in expected_statuses:
            raise CapacityProbeError("candidate_request_rejected")
        try:
            body = response.json()
        except (UnicodeError, ValueError) as exc:
            raise CapacityProbeError("candidate_response_invalid") from exc
        if not isinstance(body, dict):
            raise CapacityProbeError("candidate_response_invalid")
        return body

    async def _request_with_client(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        if not path.startswith("/api/v1/") or path.startswith("//"):
            raise CapacityProbeError("request_path_invalid")
        request_kwargs: dict[str, object] = {"headers": dict(headers or {})}
        if json_body is not None:
            request_kwargs["json"] = json_body
        try:
            return await client.request(method, path, **request_kwargs)
        except httpx.HTTPError as exc:
            raise CapacityProbeError("candidate_request_failed") from exc

    async def _json_request_with_client(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        response = await self._request_with_client(
            client,
            method,
            path,
            headers=headers,
            json_body=json_body,
        )
        return self._response_json(response, expected_statuses=expected_statuses)

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        return await self._json_request_with_client(
            self._admin_client,
            method,
            path,
            headers=headers,
            json_body=json_body,
            expected_statuses=expected_statuses,
        )

    async def admin_json(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        headers: Mapping[str, str] | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        return await self._json_request(
            method,
            path,
            json_body=json_body,
            headers=headers,
            expected_statuses=expected_statuses,
        )

    async def tenant_admin_json(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        json_body: object | None = None,
        headers: Mapping[str, str] | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        if _PUBLIC_ID.fullmatch(tenant_id) is None:
            raise CapacityProbeError("tenant_id_invalid")
        bound_headers = {
            **dict(headers or {}),
            "X-Tenant-ID": tenant_id,
            "Cookie": f"dt_tenant={tenant_id}",
        }
        return await self._json_request(
            method,
            path,
            json_body=json_body,
            headers=bound_headers,
            expected_statuses=expected_statuses,
        )

    async def login_identity(
        self,
        username: str,
        password: str | SecretStr,
    ) -> IdentityCredential:
        if _PUBLIC_ID.fullmatch(username) is None:
            raise CapacityProbeError("identity_invalid")
        secret = password if isinstance(password, SecretStr) else SecretStr(password)
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                follow_redirects=False,
                timeout=self._identity_client.timeout,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": username,
                        "password": secret.get_secret_value(),
                    },
                )
        except httpx.HTTPError as exc:
            raise CapacityProbeError("identity_login_failed") from exc
        body = self._response_json(response, expected_statuses=frozenset({200}))
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
            raise CapacityProbeError("identity_login_failed")
        return IdentityCredential(
            username=username,
            user_id=user_id,
            token=SecretStr(token),
        )

    async def tenant_identity_json(
        self,
        method: str,
        path: str,
        *,
        identity: IdentityCredential,
        tenant_id: str,
        json_body: object | None = None,
        headers: Mapping[str, str] | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202}),
    ) -> dict[str, Any]:
        if _PUBLIC_ID.fullmatch(tenant_id) is None:
            raise CapacityProbeError("tenant_id_invalid")
        bound_headers = {
            **dict(headers or {}),
            "X-Tenant-ID": tenant_id,
            "Cookie": (f"dt_token={identity.token.get_secret_value()}; dt_tenant={tenant_id}"),
        }
        bound_headers.pop("Authorization", None)
        return await self._json_request_with_client(
            self._identity_client,
            method,
            path,
            headers=bound_headers,
            json_body=json_body,
            expected_statuses=expected_statuses,
        )

    async def tenant_identity_document(
        self,
        path: str,
        *,
        identity: IdentityCredential,
        tenant_id: str,
        ticket: str,
    ) -> dict[str, Any]:
        if _PUBLIC_ID.fullmatch(tenant_id) is None or not ticket:
            raise CapacityProbeError("classroom_document_invalid")
        response = await self._request_with_client(
            self._identity_client,
            "GET",
            path,
            headers={
                "X-Tenant-ID": tenant_id,
                "Cookie": (f"dt_token={identity.token.get_secret_value()}; dt_tenant={tenant_id}"),
                "X-Classroom-Ticket": ticket,
            },
        )
        if response.status_code != 200 or len(response.content) > 16 * 1024 * 1024:
            raise CapacityProbeError("classroom_document_invalid")
        try:
            document = response.json()
        except (UnicodeError, ValueError) as exc:
            raise CapacityProbeError("classroom_document_invalid") from exc
        if not isinstance(document, dict):
            raise CapacityProbeError("classroom_document_invalid")
        return document


async def _prepare_generation_prerequisites(
    api: CapacityApi,
    *,
    tenant_id: str,
    course_id: str,
    class_id: str,
    run_key: str,
) -> None:
    quota = await api.tenant_admin_json(
        "POST",
        "/api/v1/teaching/generation-quota-grants",
        tenant_id=tenant_id,
        headers={"Idempotency-Key": f"{run_key}-{tenant_id}"},
        json_body={"units": 200},
        expected_statuses=frozenset({200}),
    )
    if (
        set(quota) != {"grantId", "tenantId", "units", "balance", "created"}
        or quota.get("tenantId") != tenant_id
        or quota.get("units") != 200
        or isinstance(quota.get("balance"), bool)
        or not isinstance(quota.get("balance"), int)
        or int(quota["balance"]) < 200
        or not isinstance(quota.get("created"), bool)
    ):
        raise CapacityProbeError("generation_prerequisites_invalid")
    safety = await api.tenant_admin_json(
        "POST",
        (f"/api/v1/teaching/courses/{course_id}/classes/{class_id}/student-safety-assessments"),
        tenant_id=tenant_id,
        headers={"Idempotency-Key": f"{run_key}-safety-{tenant_id}"},
        json_body={
            "mode": "micro",
            "contentMode": "open_creation",
            "webSearchRequested": False,
            "generallySafe": True,
            "minorSafe": True,
            "restrictedTopic": False,
            "validForSeconds": 7200,
        },
        expected_statuses=frozenset({200}),
    )
    expected = {
        "assessmentId",
        "tenantId",
        "courseId",
        "classId",
        "mode",
        "contentMode",
        "webSearchRequested",
        "generallySafe",
        "minorSafe",
        "restrictedTopic",
        "reviewedBy",
        "reviewedAt",
        "assessmentVersion",
        "expiresAt",
        "created",
    }
    if (
        set(safety) != expected
        or safety.get("tenantId") != tenant_id
        or safety.get("courseId") != course_id
        or safety.get("classId") != class_id
        or safety.get("mode") != "micro"
        or safety.get("contentMode") != "open_creation"
        or safety.get("webSearchRequested") is not False
        or safety.get("generallySafe") is not True
        or safety.get("minorSafe") is not True
        or safety.get("restrictedTopic") is not False
        or not isinstance(safety.get("created"), bool)
    ):
        raise CapacityProbeError("generation_prerequisites_invalid")


def _required_string(value: object) -> str:
    if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
        raise CapacityProbeError("scheduler_snapshot_invalid")
    return value


def _strict_nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapacityProbeError("scheduler_snapshot_invalid")
    return value


def _timestamp_value(value: object) -> datetime:
    if not isinstance(value, str) or _OBSERVED_AT.fullmatch(value) is None:
        raise CapacityProbeError("scheduler_snapshot_invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise CapacityProbeError("scheduler_snapshot_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CapacityProbeError("scheduler_snapshot_invalid")
    return parsed


def _timestamp(value: object) -> str:
    _timestamp_value(value)
    return str(value)


def _parse_scheduler_snapshot(
    raw: object,
    job_tenants: Mapping[str, str],
) -> SchedulerSnapshot:
    expected_keys = {
        "schemaVersion",
        "observedAt",
        "jobs",
        "claimEvents",
        "missingJobIds",
        "pools",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected_keys
        or raw.get("schemaVersion") != 1
        or not job_tenants
    ):
        raise CapacityProbeError("scheduler_snapshot_invalid")
    observed_at = _timestamp(raw.get("observedAt"))
    observed_time = _timestamp_value(observed_at)
    targets = dict(job_tenants)
    if any(
        _PUBLIC_ID.fullmatch(job_id) is None or _PUBLIC_ID.fullmatch(tenant_id) is None
        for job_id, tenant_id in targets.items()
    ):
        raise CapacityProbeError("scheduler_snapshot_invalid")

    missing = raw.get("missingJobIds")
    jobs = raw.get("jobs")
    events = raw.get("claimEvents")
    pools = raw.get("pools")
    if not all(isinstance(value, list) for value in (missing, jobs, events, pools)):
        raise CapacityProbeError("scheduler_snapshot_invalid")
    assert isinstance(missing, list)
    assert isinstance(jobs, list)
    assert isinstance(events, list)
    assert isinstance(pools, list)
    if (
        len(set(missing)) != len(missing)
        or any(item not in targets for item in missing)
        or len(pools) > 1
    ):
        raise CapacityProbeError("scheduler_snapshot_invalid")

    seen_jobs: set[str] = set()
    job_states: dict[str, tuple[str, str | None, str]] = {}
    for item in jobs:
        if not isinstance(item, dict) or set(item) != {
            "jobId",
            "tenantId",
            "workerPoolRef",
            "status",
            "claimedAt",
        }:
            raise CapacityProbeError("scheduler_snapshot_invalid")
        job_id = _required_string(item.get("jobId"))
        tenant_id = _required_string(item.get("tenantId"))
        worker_pool_ref = _required_string(item.get("workerPoolRef"))
        status = item.get("status")
        claimed_at = item.get("claimedAt")
        if (
            targets.get(job_id) != tenant_id
            or job_id in seen_jobs
            or status not in {"queued", "claimed"}
            or (status == "queued" and claimed_at is not None)
            or (status == "claimed" and _timestamp_value(claimed_at) > observed_time)
        ):
            raise CapacityProbeError("scheduler_snapshot_invalid")
        seen_jobs.add(job_id)
        job_states[job_id] = (str(status), claimed_at, worker_pool_ref)
    if seen_jobs | set(missing) != set(targets) or seen_jobs & set(missing):
        raise CapacityProbeError("scheduler_snapshot_invalid")

    claim_events: list[tuple[int, str, str]] = []
    event_times: dict[str, str] = {}
    previous_cursor = -1
    for item in events:
        if not isinstance(item, dict) or set(item) != {
            "cursor",
            "jobId",
            "tenantId",
            "claimedAt",
        }:
            raise CapacityProbeError("scheduler_snapshot_invalid")
        cursor = _strict_nonnegative_int(item.get("cursor"))
        job_id = _required_string(item.get("jobId"))
        tenant_id = _required_string(item.get("tenantId"))
        claimed_at = _timestamp(item.get("claimedAt"))
        if (
            cursor <= previous_cursor
            or targets.get(job_id) != tenant_id
            or job_id in event_times
            or _timestamp_value(claimed_at) > observed_time
        ):
            raise CapacityProbeError("scheduler_snapshot_invalid")
        previous_cursor = cursor
        event_times[job_id] = claimed_at
        claim_events.append((cursor, job_id, tenant_id))

    claimed_jobs = {
        job_id for job_id, (status, _claimed_at, _pool) in job_states.items() if status == "claimed"
    }
    queued_jobs = set(job_states) - claimed_jobs
    if any(job_id in event_times for job_id in queued_jobs) or any(
        job_id not in event_times
        or _timestamp_value(event_times[job_id]) < _timestamp_value(job_states[job_id][1])
        for job_id in claimed_jobs
    ):
        raise CapacityProbeError("scheduler_snapshot_invalid")

    active: list[tuple[str, str]] = []
    tenant_capacities: list[tuple[str, int]] = []
    global_capacity: int | None = None
    if pools:
        pool = pools[0]
        if not isinstance(pool, dict) or set(pool) != {
            "workerPoolRef",
            "globalSlotCapacity",
            "tenantSlotCapacities",
            "active",
        }:
            raise CapacityProbeError("scheduler_snapshot_invalid")
        worker_pool_ref = _required_string(pool.get("workerPoolRef"))
        global_capacity = _strict_nonnegative_int(pool.get("globalSlotCapacity"))
        if global_capacity != 20:
            raise CapacityProbeError("scheduler_snapshot_invalid")
        capacities = pool.get("tenantSlotCapacities")
        active_rows = pool.get("active")
        if not isinstance(capacities, list) or not isinstance(active_rows, list):
            raise CapacityProbeError("scheduler_snapshot_invalid")
        seen_tenants: set[str] = set()
        for item in capacities:
            if not isinstance(item, dict) or set(item) != {"tenantId", "capacity"}:
                raise CapacityProbeError("scheduler_snapshot_invalid")
            tenant_id = _required_string(item.get("tenantId"))
            capacity = _strict_nonnegative_int(item.get("capacity"))
            if tenant_id in seen_tenants or tenant_id not in targets.values() or capacity != 2:
                raise CapacityProbeError("scheduler_snapshot_invalid")
            seen_tenants.add(tenant_id)
            tenant_capacities.append((tenant_id, capacity))
        seen_active: set[str] = set()
        seen_ordinals: set[int] = set()
        tenant_active_counts: dict[str, int] = {}
        for item in active_rows:
            if not isinstance(item, dict) or set(item) != {"jobId", "tenantId", "ordinal"}:
                raise CapacityProbeError("scheduler_snapshot_invalid")
            job_id = _required_string(item.get("jobId"))
            tenant_id = _required_string(item.get("tenantId"))
            ordinal = _strict_nonnegative_int(item.get("ordinal"))
            if (
                targets.get(job_id) != tenant_id
                or job_id in seen_active
                or job_id not in claimed_jobs
                or job_states[job_id][2] != worker_pool_ref
                or ordinal >= global_capacity
                or ordinal in seen_ordinals
            ):
                raise CapacityProbeError("scheduler_snapshot_invalid")
            seen_active.add(job_id)
            seen_ordinals.add(ordinal)
            tenant_active_counts[tenant_id] = tenant_active_counts.get(tenant_id, 0) + 1
            active.append((job_id, tenant_id))
        capacities_by_tenant = dict(tenant_capacities)
        if (
            len(active) > global_capacity
            or seen_active != claimed_jobs
            or any(
                count > capacities_by_tenant.get(tenant_id, 0)
                for tenant_id, count in tenant_active_counts.items()
            )
        ):
            raise CapacityProbeError("scheduler_snapshot_invalid")
    elif claimed_jobs:
        raise CapacityProbeError("scheduler_snapshot_invalid")

    return SchedulerSnapshot(
        observed_at=observed_at,
        active=tuple(active),
        claim_events=tuple(claim_events),
        global_capacity=global_capacity,
        tenant_capacities=tuple(tenant_capacities),
    )


def _resource_observation(
    sequence: int,
    phase: str,
    raw: object,
    *,
    observed_at: str,
) -> dict[str, object]:
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "available",
            "total_rss_bytes",
            "limit_bytes",
            "available_bytes",
            "limit_source",
            "usage_ratio",
            "partial",
            "processes",
        }
        or raw.get("available") is not True
        or raw.get("partial") is not False
        or raw.get("limit_source") not in {"cgroup", "host"}
        or _OBSERVED_AT.fullmatch(observed_at) is None
    ):
        raise CapacityProbeError("resource_observation_invalid")
    numeric = ("total_rss_bytes", "limit_bytes", "available_bytes")
    if any(
        isinstance(raw.get(name), bool) or not isinstance(raw.get(name), int) or int(raw[name]) < 0
        for name in numeric
    ):
        raise CapacityProbeError("resource_observation_invalid")
    total = int(raw["total_rss_bytes"])
    limit = int(raw["limit_bytes"])
    available = int(raw["available_bytes"])
    ratio = raw.get("usage_ratio")
    if (
        limit <= 0
        or available > limit
        or type(ratio) not in {int, float}
        or not math.isfinite(float(ratio))
        or not math.isclose(float(ratio), total / limit, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise CapacityProbeError("resource_observation_invalid")
    raw_processes = raw.get("processes")
    if not isinstance(raw_processes, list) or not raw_processes:
        raise CapacityProbeError("resource_observation_invalid")
    processes: list[dict[str, object]] = []
    labels: set[str] = set()
    for item in raw_processes:
        if not isinstance(item, dict) or set(item) != {"label", "count", "rss_bytes"}:
            raise CapacityProbeError("resource_observation_invalid")
        label = item.get("label")
        count = item.get("count")
        rss = item.get("rss_bytes")
        if (
            not isinstance(label, str)
            or not label
            or label in labels
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or isinstance(rss, bool)
            or not isinstance(rss, int)
            or rss < 0
        ):
            raise CapacityProbeError("resource_observation_invalid")
        labels.add(label)
        processes.append({"label": label, "count": count, "rssBytes": rss})
    if sum(int(item["rssBytes"]) for item in processes) != total:
        raise CapacityProbeError("resource_observation_invalid")
    return {
        "sequence": sequence,
        "phase": phase,
        "observedAt": observed_at,
        "available": True,
        "totalRssBytes": total,
        "limitBytes": limit,
        "availableBytes": available,
        "limitSource": raw["limit_source"],
        "usageRatio": float(ratio),
        "partial": False,
        "processes": processes,
    }


def _select_quiz_evidence(document: object) -> QuizEvidence:
    if not isinstance(document, dict):
        raise CapacityProbeError("quiz_evidence_invalid")
    openmaic = document.get("openmaic")
    mappings = document.get("knowledgePointMappings")
    if not isinstance(openmaic, dict) or not isinstance(mappings, list):
        raise CapacityProbeError("quiz_evidence_invalid")
    scenes = openmaic.get("scenes")
    if not isinstance(scenes, list):
        raise CapacityProbeError("quiz_evidence_invalid")
    mapped: dict[str, list[str]] = {}
    for item in mappings:
        if not isinstance(item, dict):
            raise CapacityProbeError("quiz_evidence_invalid")
        knowledge_point_id = item.get("knowledgePointId")
        scene_ids = item.get("sceneIds")
        if (
            not isinstance(knowledge_point_id, str)
            or _PUBLIC_ID.fullmatch(knowledge_point_id) is None
            or not isinstance(scene_ids, list)
        ):
            raise CapacityProbeError("quiz_evidence_invalid")
        for scene_id in scene_ids:
            if not isinstance(scene_id, str):
                raise CapacityProbeError("quiz_evidence_invalid")
            mapped.setdefault(scene_id, []).append(knowledge_point_id)
    for scene in scenes:
        if not isinstance(scene, dict) or scene.get("type") != "quiz":
            continue
        scene_id = scene.get("id")
        content = scene.get("content")
        if (
            not isinstance(scene_id, str)
            or len(mapped.get(scene_id, ())) != 1
            or not isinstance(content, dict)
            or not isinstance(content.get("questions"), list)
        ):
            continue
        for question in content["questions"]:
            if not isinstance(question, dict):
                continue
            question_id = question.get("id")
            question_type = question.get("questionType")
            correct = question.get("correctOptionIds")
            if (
                isinstance(question_id, str)
                and _PUBLIC_ID.fullmatch(question_id) is not None
                and question_type in {"single_choice", "multiple_choice"}
                and isinstance(correct, list)
                and bool(correct)
                and all(isinstance(item, str) and item for item in correct)
                and (question_type != "single_choice" or len(correct) == 1)
            ):
                return QuizEvidence(
                    scene_id=scene_id,
                    question_id=question_id,
                    knowledge_point_id=mapped[scene_id][0],
                    answer=list(correct),
                )
    raise CapacityProbeError("quiz_evidence_invalid")


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _remaining(end_time: float) -> float:
    remaining = end_time - time.monotonic()
    if remaining <= 0:
        raise CapacityProbeError("capacity_probe_timeout")
    return remaining


async def _bounded_map(
    values: Sequence[_T],
    operation: Callable[[_T], Awaitable[Any]],
    *,
    limit: int,
) -> list[Any]:
    semaphore = asyncio.Semaphore(limit)

    async def run(value: _T) -> Any:
        async with semaphore:
            return await operation(value)

    tasks = [asyncio.create_task(run(value)) for value in values]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _run_cleanup_operations(
    operations: Sequence[Callable[[], Awaitable[None]]],
    *,
    limit: int,
    timeout_seconds: float,
) -> bool:
    semaphore = asyncio.Semaphore(limit)

    async def run(operation: Callable[[], Awaitable[None]]) -> None:
        async with semaphore:
            await operation()

    tasks = [asyncio.create_task(run(operation)) for operation in operations]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return True
    return any(isinstance(result, BaseException) for result in results)


def _identity_material(config: ProbeConfig) -> IdentityMaterial:
    nonce = secrets.token_bytes(32)
    binding = canonical_capacity_profile_report(
        {"candidate": config.candidate, "releaseRun": config.release_run}
    )
    token = config.admin_token.get_secret_value().encode("utf-8")
    digest = hmac.new(token, binding + nonce, hashlib.sha256).hexdigest()

    def password(label: str) -> SecretStr:
        value = hmac.new(token, f"{digest}:{label}".encode(), hashlib.sha256).hexdigest()
        return SecretStr(f"Cap9!{value}")

    suffix = digest[:16]
    return IdentityMaterial(
        run_key=f"capacity-{suffix}",
        student_username=f"capacity-student-{suffix}",
        student_password=password("student"),
        report_username=f"capacity-report-{suffix}",
        report_password=password("report"),
    )


def _exact_keys(
    value: object,
    expected: frozenset[str],
    error: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise CapacityProbeError(error)
    return value


def _public_id(value: object, error: str) -> str:
    if not isinstance(value, str) or _PUBLIC_ID.fullmatch(value) is None:
        raise CapacityProbeError(error)
    return value


def _nonnegative_int(value: object, error: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapacityProbeError(error)
    return value


async def _create_identity(
    api: CapacityApi,
    *,
    username: str,
    password: SecretStr,
) -> str:
    body = _exact_keys(
        await api.admin_json(
            "POST",
            "/api/v1/auth/users",
            json_body={"username": username, "password": password.get_secret_value()},
            expected_statuses=frozenset({201}),
        ),
        frozenset({"ok", "user_id", "username", "role", "is_admin"}),
        "identity_create_invalid",
    )
    user_id = _public_id(body.get("user_id"), "identity_create_invalid")
    if (
        body.get("ok") is not True
        or body.get("username") != username
        or body.get("role") != "user"
        or body.get("is_admin") is not False
    ):
        raise CapacityProbeError("identity_create_invalid")
    return user_id


async def _delete_identity(api: CapacityApi, username: str) -> None:
    body = await api.admin_json(
        "DELETE",
        f"/api/v1/auth/users/{username}",
        expected_statuses=frozenset({200}),
    )
    if body != {"ok": True}:
        raise CapacityProbeError("identity_cleanup_failed")


def _validate_member(
    raw: object,
    *,
    tenant_id: str,
    user_id: str,
    role: str,
) -> None:
    body = _exact_keys(
        raw,
        frozenset({"tenant_id", "user_id", "roles", "grants"}),
        "tenant_fixture_invalid",
    )
    expected_grant = {
        "role": role,
        "scope_type": "tenant",
        "scope_id": tenant_id,
    }
    if (
        body.get("tenant_id") != tenant_id
        or body.get("user_id") != user_id
        or body.get("roles") != [role]
        or body.get("grants") != [expected_grant]
    ):
        raise CapacityProbeError("tenant_fixture_invalid")


async def _wait_for_tenant_active(
    api: CapacityApi,
    *,
    tenant_id: str,
    job_id: str,
    end_time: float,
) -> None:
    while True:
        body = _exact_keys(
            await api.admin_json(
                "GET",
                f"/api/v1/tenants/{tenant_id}/provisioning",
                expected_statuses=frozenset({200}),
            ),
            frozenset({"tenant_id", "status", "job_id", "job_status", "attempt_count"}),
            "tenant_provisioning_invalid",
        )
        if body.get("tenant_id") != tenant_id or body.get("job_id") != job_id:
            raise CapacityProbeError("tenant_provisioning_invalid")
        _nonnegative_int(body.get("attempt_count"), "tenant_provisioning_invalid")
        if body.get("status") == "active" and body.get("job_status") == "completed":
            return
        if body.get("status") == "failed" or body.get("job_status") == "failed":
            raise CapacityProbeError("tenant_provisioning_failed")
        if body.get("status") != "provisioning" or body.get("job_status") not in {
            "pending",
            "running",
        }:
            raise CapacityProbeError("tenant_provisioning_invalid")
        await asyncio.sleep(min(0.25, _remaining(end_time)))


async def _create_tenant_fixture(
    api: CapacityApi,
    *,
    sequence: int,
    material: IdentityMaterial,
    student_user_id: str,
    report_user_id: str,
    end_time: float,
) -> TenantFixture:
    tenant_result = _exact_keys(
        await api.admin_json(
            "POST",
            "/api/v1/tenants",
            headers={"Idempotency-Key": f"{material.run_key}-tenant-{sequence:02d}"},
            json_body={"name": f"Capacity acceptance {sequence:02d}"},
            expected_statuses=frozenset({202}),
        ),
        frozenset({"tenant_id", "status", "job_id"}),
        "tenant_create_invalid",
    )
    tenant_id = _public_id(tenant_result.get("tenant_id"), "tenant_create_invalid")
    job_id = _public_id(tenant_result.get("job_id"), "tenant_create_invalid")
    if tenant_result.get("status") not in {"provisioning", "active"}:
        raise CapacityProbeError("tenant_create_invalid")
    await _wait_for_tenant_active(
        api,
        tenant_id=tenant_id,
        job_id=job_id,
        end_time=end_time,
    )

    for user_id, role in ((student_user_id, "student"), (report_user_id, "org_admin")):
        member = await api.tenant_admin_json(
            "POST",
            f"/api/v1/tenants/{tenant_id}/members",
            tenant_id=tenant_id,
            json_body={"user_id": user_id, "role": role},
            expected_statuses=frozenset({200}),
        )
        _validate_member(
            member,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
        )

    suffix = f"{material.run_key.removeprefix('capacity-')}-{sequence:02d}"
    course_id = f"course-{suffix}"
    class_id = f"class-{suffix}"
    course = _exact_keys(
        await api.tenant_admin_json(
            "POST",
            "/api/v1/teaching/courses",
            tenant_id=tenant_id,
            json_body={"id": course_id, "title": f"Capacity course {sequence:02d}"},
            expected_statuses=frozenset({201}),
        ),
        frozenset({"id", "title", "status", "createdAt"}),
        "tenant_fixture_invalid",
    )
    if course.get("id") != course_id or course.get("status") != "active":
        raise CapacityProbeError("tenant_fixture_invalid")
    classroom = _exact_keys(
        await api.tenant_admin_json(
            "POST",
            f"/api/v1/teaching/courses/{course_id}/classes",
            tenant_id=tenant_id,
            json_body={"id": class_id, "name": f"Capacity class {sequence:02d}"},
            expected_statuses=frozenset({201}),
        ),
        frozenset({"id", "courseId", "name", "status", "createdAt"}),
        "tenant_fixture_invalid",
    )
    if (
        classroom.get("id") != class_id
        or classroom.get("courseId") != course_id
        or classroom.get("status") != "active"
    ):
        raise CapacityProbeError("tenant_fixture_invalid")
    policy = _exact_keys(
        await api.tenant_admin_json(
            "PUT",
            f"/api/v1/teaching/courses/{course_id}/generation-policy",
            tenant_id=tenant_id,
            json_body={
                "allowStudentMicro": True,
                "allowStudentFull": False,
                "allowedContentModes": ["open_creation"],
                "allowWebSearch": False,
                "requireApprovalForRestrictedTopics": True,
                "minorSafetyMode": True,
                "microSceneLimit": 5,
                "fullSceneLimit": 6,
                "dailyStudentUnits": 200,
                "monthlyStudentUnits": 200,
            },
            expected_statuses=frozenset({200}),
        ),
        frozenset(
            {
                "tenantId",
                "courseId",
                "allowStudentMicro",
                "allowStudentFull",
                "allowedContentModes",
                "allowWebSearch",
                "requireApprovalForRestrictedTopics",
                "minorSafetyMode",
                "microSceneLimit",
                "fullSceneLimit",
                "dailyStudentUnits",
                "monthlyStudentUnits",
                "updatedBy",
                "updatedAt",
            }
        ),
        "tenant_fixture_invalid",
    )
    if (
        policy.get("tenantId") != tenant_id
        or policy.get("courseId") != course_id
        or policy.get("allowStudentMicro") is not True
        or policy.get("allowStudentFull") is not False
        or policy.get("allowedContentModes") != ["open_creation"]
        or policy.get("allowWebSearch") is not False
        or policy.get("microSceneLimit") != 5
        or policy.get("dailyStudentUnits") != 200
        or policy.get("monthlyStudentUnits") != 200
    ):
        raise CapacityProbeError("tenant_fixture_invalid")
    enrollment = _exact_keys(
        await api.tenant_admin_json(
            "POST",
            f"/api/v1/teaching/classes/{class_id}/enrollments",
            tenant_id=tenant_id,
            json_body={"userId": student_user_id},
            expected_statuses=frozenset({201}),
        ),
        frozenset({"classId", "userId", "status", "createdAt"}),
        "tenant_fixture_invalid",
    )
    if (
        enrollment.get("classId") != class_id
        or enrollment.get("userId") != student_user_id
        or enrollment.get("status") != "active"
    ):
        raise CapacityProbeError("tenant_fixture_invalid")
    await _prepare_generation_prerequisites(
        api,
        tenant_id=tenant_id,
        course_id=course_id,
        class_id=class_id,
        run_key=material.run_key,
    )
    return TenantFixture(sequence, tenant_id, course_id, class_id)


async def _capture_resource(
    api: CapacityApi,
    *,
    sequence: int,
    phase: str,
) -> dict[str, object]:
    if sequence >= len(CAPACITY_RESOURCE_PHASES) or CAPACITY_RESOURCE_PHASES[sequence] != phase:
        raise CapacityProbeError("resource_observation_invalid")
    raw = await api.admin_json(
        "GET",
        "/api/v1/system/memory",
        expected_statuses=frozenset({200}),
    )
    return _resource_observation(sequence, phase, raw, observed_at=_observed_at())


def _validate_generation_response(
    raw: object,
    *,
    tenant_id: str,
    owner_id: str,
    fixture: TenantFixture,
) -> tuple[str, str]:
    body = _exact_keys(
        raw,
        frozenset(
            {
                "assetId",
                "requestId",
                "approvalId",
                "generationJobId",
                "status",
                "courseId",
                "classId",
                "mode",
                "ownerId",
                "revision",
                "outline",
                "classroomVersionId",
            }
        ),
        "generation_submission_invalid",
    )
    asset_id = _public_id(body.get("assetId"), "generation_submission_invalid")
    job_id = _public_id(body.get("generationJobId"), "generation_submission_invalid")
    _public_id(body.get("requestId"), "generation_submission_invalid")
    if (
        body.get("courseId") != fixture.course_id
        or body.get("classId") != fixture.class_id
        or body.get("mode") != "micro"
        or body.get("ownerId") != owner_id
        or body.get("approvalId") is not None
        or body.get("status")
        not in {
            "quota_reserved",
            "queued",
            "claimed",
            "generating_outline",
            "awaiting_confirmation",
            "generating_content",
            "validating",
            "materializing",
            "succeeded",
        }
        or _nonnegative_int(body.get("revision"), "generation_submission_invalid") < 1
        or tenant_id != fixture.tenant_id
    ):
        raise CapacityProbeError("generation_submission_invalid")
    return asset_id, job_id


async def _submit_generation(
    api: CapacityApi,
    *,
    sequence: int,
    fixture: TenantFixture,
    student: IdentityCredential,
) -> tuple[GenerationFixture, dict[str, object]]:
    started = time.perf_counter()
    response = await api.tenant_identity_json(
        "POST",
        "/api/v1/student-classrooms",
        identity=student,
        tenant_id=fixture.tenant_id,
        json_body={
            "courseId": fixture.course_id,
            "classId": fixture.class_id,
            "mode": "micro",
            "contentMode": "open_creation",
            "webSearchRequested": False,
        },
        expected_statuses=frozenset({202}),
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    asset_id, job_id = _validate_generation_response(
        response,
        tenant_id=fixture.tenant_id,
        owner_id=student.user_id,
        fixture=fixture,
    )
    return (
        GenerationFixture(
            sequence,
            fixture.tenant_id,
            asset_id,
            job_id,
            fixture.course_id,
            fixture.class_id,
            student.user_id,
        ),
        {
            "metric": "job_submission_visible",
            "tenantId": fixture.tenant_id,
            "subjectId": job_id,
            "sequence": sequence,
            "latencyMs": latency_ms,
            "success": True,
        },
    )


async def _observe_scheduler(
    api: CapacityApi,
    *,
    job_tenants: dict[str, str],
    finished: asyncio.Event,
    end_time: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    claims: dict[str, tuple[int, str]] = {}
    observations: list[dict[str, object]] = []
    previous_active: tuple[tuple[str, str], ...] | None = None
    recorded_peak = (0, 0)
    saturated_resource_task: asyncio.Task[dict[str, object]] | None = None
    try:
        while True:
            targets = dict(job_tenants)
            if targets:
                raw = await api.admin_json(
                    "POST",
                    "/api/v1/system/generation-scheduler-snapshot",
                    json_body={"jobIds": list(targets)},
                    expected_statuses=frozenset({200}),
                )
                snapshot = _parse_scheduler_snapshot(raw, targets)
                for cursor, job_id, tenant_id in snapshot.claim_events:
                    existing = claims.get(job_id)
                    claim = (cursor, tenant_id)
                    if existing is not None and existing != claim:
                        raise CapacityProbeError("scheduler_snapshot_invalid")
                    claims[job_id] = claim
                active_state = tuple(sorted(snapshot.active))
                tenant_counts: dict[str, int] = {}
                for _job_id, tenant_id in active_state:
                    tenant_counts[tenant_id] = tenant_counts.get(tenant_id, 0) + 1
                peak = (len(active_state), max(tenant_counts.values(), default=0))
                if active_state != previous_active:
                    active = [
                        {"jobId": job_id, "tenantId": tenant_id}
                        for job_id, tenant_id in active_state
                    ]
                    observation = {"sequence": len(observations), "active": active}
                    if len(observations) < MAX_SCHEDULER_OBSERVATIONS:
                        observations.append(observation)
                    elif peak > recorded_peak:
                        observation["sequence"] = len(observations) - 1
                        observations[-1] = observation
                    previous_active = active_state
                    recorded_peak = max(recorded_peak, peak)
                if (
                    peak
                    == (
                        CAPACITY_PROFILE["sharedGenerationSlots"],
                        CAPACITY_PROFILE["defaultTenantSlots"],
                    )
                    and saturated_resource_task is None
                ):
                    saturated_resource_task = asyncio.create_task(
                        _capture_resource(
                            api,
                            sequence=1,
                            phase="generation_saturated",
                        )
                    )
                if finished.is_set():
                    if len(targets) != CAPACITY_WORKLOAD["generationJobsSubmitted"] or set(
                        claims
                    ) != set(targets):
                        raise CapacityProbeError("scheduler_claims_incomplete")
                    if saturated_resource_task is None:
                        raise CapacityProbeError("scheduler_saturation_unobserved")
                    break
            elif finished.is_set():
                raise CapacityProbeError("scheduler_claims_incomplete")
            await asyncio.sleep(min(0.05, _remaining(end_time)))
        saturated_resource = await saturated_resource_task
    except BaseException:
        if saturated_resource_task is not None and not saturated_resource_task.done():
            saturated_resource_task.cancel()
            await asyncio.gather(saturated_resource_task, return_exceptions=True)
        raise
    ordered_claims = [
        {"sequence": sequence, "jobId": job_id, "tenantId": tenant_id}
        for sequence, (job_id, (_cursor, tenant_id)) in enumerate(
            sorted(claims.items(), key=lambda item: item[1][0])
        )
    ]
    return ordered_claims, observations, saturated_resource


def _validate_job_status(raw: object, expected_job_id: str) -> str:
    body = _exact_keys(
        raw,
        frozenset(
            {
                "job_id",
                "job_kind",
                "phase",
                "status",
                "progress_percent",
                "waiting_reason",
                "cancellable",
                "retryable",
                "outline",
                "error_category",
                "error_code",
                "retry_of_job_id",
                "export_format",
                "download_ready",
            }
        ),
        "generation_status_invalid",
    )
    status = body.get("status")
    progress = body.get("progress_percent")
    if (
        body.get("job_id") != expected_job_id
        or body.get("job_kind") != "generation"
        or body.get("phase") != "content"
        or status
        not in {
            "created",
            "quota_reserved",
            "queued",
            "generating_content",
            "validating",
            "materializing",
            "succeeded",
            "failed",
            "canceled",
        }
        or isinstance(progress, bool)
        or not isinstance(progress, int)
        or progress < 0
        or progress > 100
        or (status == "succeeded" and progress != 100)
        or body.get("export_format") is not None
        or body.get("download_ready") is not False
    ):
        raise CapacityProbeError("generation_status_invalid")
    return str(status)


async def _wait_for_ready_classroom(
    api: CapacityApi,
    *,
    generation: GenerationFixture,
    student: IdentityCredential,
    end_time: float,
) -> ReadyClassroom:
    while True:
        status = _validate_job_status(
            await api.tenant_identity_json(
                "GET",
                f"/api/v1/classroom-jobs/{generation.job_id}",
                identity=student,
                tenant_id=generation.tenant_id,
                expected_statuses=frozenset({200}),
            ),
            generation.job_id,
        )
        if status in {"failed", "canceled"}:
            raise CapacityProbeError("generation_job_failed")
        if status == "succeeded":
            asset = _exact_keys(
                await api.tenant_identity_json(
                    "GET",
                    f"/api/v1/student-classrooms/{generation.asset_id}",
                    identity=student,
                    tenant_id=generation.tenant_id,
                    expected_statuses=frozenset({200}),
                ),
                frozenset(
                    {
                        "assetId",
                        "requestId",
                        "approvalId",
                        "generationJobId",
                        "status",
                        "courseId",
                        "classId",
                        "mode",
                        "ownerId",
                        "revision",
                        "outline",
                        "classroomVersionId",
                    }
                ),
                "generation_status_invalid",
            )
            version_id = asset.get("classroomVersionId")
            if (
                asset.get("assetId") == generation.asset_id
                and asset.get("generationJobId") == generation.job_id
                and asset.get("status") == "succeeded"
                and asset.get("mode") == "micro"
                and asset.get("ownerId") == student.user_id
                and (generation.course_id is None or asset.get("courseId") == generation.course_id)
                and (generation.class_id is None or asset.get("classId") == generation.class_id)
                and (generation.owner_id is None or asset.get("ownerId") == generation.owner_id)
                and _nonnegative_int(asset.get("revision"), "generation_status_invalid") >= 1
                and isinstance(version_id, str)
                and _PUBLIC_ID.fullmatch(version_id) is not None
            ):
                return ReadyClassroom(
                    generation.sequence,
                    generation.tenant_id,
                    generation.asset_id,
                    generation.job_id,
                    version_id,
                )
            raise CapacityProbeError("generation_status_invalid")
        await asyncio.sleep(min(0.2, _remaining(end_time)))


def _validate_session_response(
    raw: object,
    *,
    tenant_id: str,
    user_id: str,
    asset_id: str,
    version_id: str,
    expected_status: str,
) -> str:
    body = _exact_keys(
        raw,
        frozenset(
            {
                "id",
                "tenant_id",
                "user_id",
                "classroom_version_id",
                "assignment_id",
                "student_asset_id",
                "status",
                "last_cursor",
                "started_at",
                "completed_at",
            }
        ),
        "learning_session_invalid",
    )
    session_id = _public_id(body.get("id"), "learning_session_invalid")
    if (
        body.get("tenant_id") != tenant_id
        or body.get("user_id") != user_id
        or body.get("classroom_version_id") != version_id
        or body.get("assignment_id") is not None
        or body.get("student_asset_id") != asset_id
        or body.get("status") != expected_status
    ):
        raise CapacityProbeError("learning_session_invalid")
    if expected_status == "active" and body.get("completed_at") is not None:
        raise CapacityProbeError("learning_session_invalid")
    if expected_status == "completed" and not isinstance(body.get("completed_at"), str):
        raise CapacityProbeError("learning_session_invalid")
    return session_id


async def _create_session(
    api: CapacityApi,
    *,
    sequence: int,
    classroom: ReadyClassroom,
    student: IdentityCredential,
) -> tuple[SessionFixture, dict[str, object]]:
    started = time.perf_counter()
    raw = await api.tenant_identity_json(
        "POST",
        "/api/v1/classroom-sessions",
        identity=student,
        tenant_id=classroom.tenant_id,
        json_body={"student_asset_id": classroom.asset_id},
        expected_statuses=frozenset({201}),
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    session_id = _validate_session_response(
        raw,
        tenant_id=classroom.tenant_id,
        user_id=student.user_id,
        asset_id=classroom.asset_id,
        version_id=classroom.version_id,
        expected_status="active",
    )
    return (
        SessionFixture(
            sequence,
            classroom.tenant_id,
            classroom.asset_id,
            classroom.version_id,
            session_id,
        ),
        {
            "metric": "core_api",
            "tenantId": classroom.tenant_id,
            "subjectId": session_id,
            "sequence": sequence,
            "latencyMs": latency_ms,
            "success": True,
        },
    )


async def _verify_active_session(
    api: CapacityApi,
    *,
    session: SessionFixture,
    student: IdentityCredential,
) -> None:
    session_id = _validate_session_response(
        await api.tenant_identity_json(
            "GET",
            f"/api/v1/classroom-sessions/{session.session_id}",
            identity=student,
            tenant_id=session.tenant_id,
            expected_statuses=frozenset({200}),
        ),
        tenant_id=session.tenant_id,
        user_id=student.user_id,
        asset_id=session.asset_id,
        version_id=session.version_id,
        expected_status="active",
    )
    if session_id != session.session_id:
        raise CapacityProbeError("learning_session_invalid")


def _validate_ticket(raw: object, error: str) -> str:
    body = _exact_keys(raw, frozenset({"ticket", "expires_in"}), error)
    ticket = body.get("ticket")
    if (
        not isinstance(ticket, str)
        or not ticket
        or isinstance(body.get("expires_in"), bool)
        or not isinstance(body.get("expires_in"), int)
        or int(body["expires_in"]) <= 0
    ):
        raise CapacityProbeError(error)
    return ticket


async def _load_quiz_evidence(
    api: CapacityApi,
    *,
    session: SessionFixture,
    student: IdentityCredential,
) -> QuizEvidence:
    ticket = _validate_ticket(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/classroom-sessions/{session.session_id}/read-ticket",
            identity=student,
            tenant_id=session.tenant_id,
            json_body={
                "action": "classroom.document.read",
                "resource_id": session.version_id,
            },
            expected_statuses=frozenset({200}),
        ),
        "classroom_document_invalid",
    )
    document = await api.tenant_identity_document(
        f"/api/v1/classroom-versions/{session.version_id}/document",
        identity=student,
        tenant_id=session.tenant_id,
        ticket=ticket,
    )
    return _select_quiz_evidence(document)


def _report_snapshot(raw: object, *, knowledge_point_id: str) -> ReportSnapshot:
    body = _exact_keys(
        raw,
        frozenset(
            {
                "classroomVersionId",
                "sessionCount",
                "completedCount",
                "completionRate",
                "completedSceneCount",
                "validQuizCount",
                "correctQuizCount",
                "hintCount",
                "pblMilestoneCount",
                "mastery",
                "projectionLagSeconds",
            }
        ),
        "teaching_report_invalid",
    )
    for field in (
        "sessionCount",
        "completedCount",
        "completedSceneCount",
        "validQuizCount",
        "correctQuizCount",
        "hintCount",
        "pblMilestoneCount",
    ):
        _nonnegative_int(body.get(field), "teaching_report_invalid")
    mastery = body.get("mastery")
    if not isinstance(mastery, list):
        raise CapacityProbeError("teaching_report_invalid")
    matching: list[int] = []
    seen: set[str] = set()
    for item in mastery:
        row = _exact_keys(
            item,
            frozenset({"knowledgePointId", "level", "evidenceCount"}),
            "teaching_report_invalid",
        )
        kp_id = _public_id(row.get("knowledgePointId"), "teaching_report_invalid")
        if kp_id in seen:
            raise CapacityProbeError("teaching_report_invalid")
        seen.add(kp_id)
        evidence_count = _nonnegative_int(row.get("evidenceCount"), "teaching_report_invalid")
        level = row.get("level")
        if type(level) not in {int, float} or not math.isfinite(float(level)):
            raise CapacityProbeError("teaching_report_invalid")
        if kp_id == knowledge_point_id:
            matching.append(evidence_count)
    return ReportSnapshot(
        valid_quiz_count=int(body["validQuizCount"]),
        correct_quiz_count=int(body["correctQuizCount"]),
        evidence_count=matching[0] if matching else 0,
    )


async def _load_report_snapshot(
    api: CapacityApi,
    *,
    session: SessionFixture,
    report_identity: IdentityCredential,
    knowledge_point_id: str,
    expected_session_count: int | None = None,
    expected_completed_count: int | None = None,
) -> ReportSnapshot:
    raw = await api.tenant_identity_json(
        "GET",
        f"/api/v1/teaching-reports/classrooms/{session.version_id}",
        identity=report_identity,
        tenant_id=session.tenant_id,
        expected_statuses=frozenset({200}),
    )
    body = _exact_keys(
        raw,
        frozenset(
            {
                "classroomVersionId",
                "sessionCount",
                "completedCount",
                "completionRate",
                "completedSceneCount",
                "validQuizCount",
                "correctQuizCount",
                "hintCount",
                "pblMilestoneCount",
                "mastery",
                "projectionLagSeconds",
            }
        ),
        "teaching_report_invalid",
    )
    if body.get("classroomVersionId") != session.version_id:
        raise CapacityProbeError("teaching_report_invalid")
    session_count = _nonnegative_int(body.get("sessionCount"), "teaching_report_invalid")
    completed_count = _nonnegative_int(body.get("completedCount"), "teaching_report_invalid")
    if (expected_session_count is not None and session_count != expected_session_count) or (
        expected_completed_count is not None and completed_count != expected_completed_count
    ):
        raise CapacityProbeError("teaching_report_invalid")
    return _report_snapshot(body, knowledge_point_id=knowledge_point_id)


def _learning_events(
    *,
    material: IdentityMaterial,
    session: SessionFixture,
    quiz: QuizEvidence,
) -> tuple[list[dict[str, object]], tuple[str, str, str]]:
    event_ids = (
        f"{material.run_key}-event-{session.sequence:03d}-started",
        f"{material.run_key}-event-{session.sequence:03d}-quiz",
        f"{material.run_key}-event-{session.sequence:03d}-completed",
    )
    occurred_at = _observed_at()
    return (
        [
            {
                "schema_version": "1.0",
                "event_id": event_ids[0],
                "event_type": "classroom.started",
                "occurred_at": occurred_at,
            },
            {
                "schema_version": "1.0",
                "event_id": event_ids[1],
                "event_type": "quiz.graded",
                "occurred_at": occurred_at,
                "scene_id": quiz.scene_id,
                "knowledge_point_id": quiz.knowledge_point_id,
                "assessment_id": quiz.scene_id,
                "question_id": quiz.question_id,
                "answer": quiz.answer,
            },
            {
                "schema_version": "1.0",
                "event_id": event_ids[2],
                "event_type": "classroom.completed",
                "occurred_at": occurred_at,
            },
        ],
        event_ids,
    )


def _validate_event_ingestion(raw: object, event_ids: tuple[str, str, str]) -> tuple[int, int, int]:
    body = _exact_keys(
        raw,
        frozenset({"accepted", "duplicate", "quarantined"}),
        "learning_event_ingestion_invalid",
    )
    if body.get("duplicate") != [] or body.get("quarantined") != []:
        raise CapacityProbeError("learning_event_ingestion_invalid")
    accepted = body.get("accepted")
    if not isinstance(accepted, list) or len(accepted) != 3:
        raise CapacityProbeError("learning_event_ingestion_invalid")
    observed_ids: list[str] = []
    sequences: list[int] = []
    for item in accepted:
        row = _exact_keys(
            item,
            frozenset({"event_id", "seq"}),
            "learning_event_ingestion_invalid",
        )
        observed_ids.append(_public_id(row.get("event_id"), "learning_event_ingestion_invalid"))
        sequences.append(_nonnegative_int(row.get("seq"), "learning_event_ingestion_invalid"))
    if (
        observed_ids != list(event_ids)
        or any(sequence <= 0 for sequence in sequences)
        or any(current <= previous for previous, current in zip(sequences, sequences[1:]))
    ):
        raise CapacityProbeError("learning_event_ingestion_invalid")
    return sequences[0], sequences[1], sequences[2]


async def _ingest_session_events(
    api: CapacityApi,
    *,
    material: IdentityMaterial,
    session: SessionFixture,
    quiz: QuizEvidence,
    student: IdentityCredential,
    start_gate: _StartGate,
) -> dict[str, object]:
    ticket = _validate_ticket(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/classroom-sessions/{session.session_id}/event-ticket",
            identity=student,
            tenant_id=session.tenant_id,
            expected_statuses=frozenset({200}),
        ),
        "learning_event_ticket_invalid",
    )
    events, event_ids = _learning_events(material=material, session=session, quiz=quiz)
    await start_gate.wait()
    started = time.perf_counter()
    result = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-sessions/{session.session_id}/events",
        identity=student,
        tenant_id=session.tenant_id,
        headers={"X-Classroom-Ticket": ticket},
        json_body={"events": events},
        expected_statuses=frozenset({202}),
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    _validate_event_ingestion(result, event_ids)
    return {
        "metric": "event_ingest",
        "tenantId": session.tenant_id,
        "subjectId": session.session_id,
        "sequence": session.sequence,
        "latencyMs": latency_ms,
        "success": True,
    }


async def _complete_session(
    api: CapacityApi,
    *,
    session: SessionFixture,
    student: IdentityCredential,
) -> dict[str, str]:
    completed = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-sessions/{session.session_id}/complete",
        identity=student,
        tenant_id=session.tenant_id,
        expected_statuses=frozenset({200}),
    )
    completed_id = _validate_session_response(
        completed,
        tenant_id=session.tenant_id,
        user_id=student.user_id,
        asset_id=session.asset_id,
        version_id=session.version_id,
        expected_status="completed",
    )
    if completed_id != session.session_id:
        raise CapacityProbeError("learning_session_invalid")
    return {
        "sessionId": session.session_id,
        "tenantId": session.tenant_id,
        "status": "completed",
    }


async def _verify_completed_session(
    api: CapacityApi,
    *,
    session: SessionFixture,
    student: IdentityCredential,
) -> None:
    completed_id = _validate_session_response(
        await api.tenant_identity_json(
            "GET",
            f"/api/v1/classroom-sessions/{session.session_id}",
            identity=student,
            tenant_id=session.tenant_id,
            expected_statuses=frozenset({200}),
        ),
        tenant_id=session.tenant_id,
        user_id=student.user_id,
        asset_id=session.asset_id,
        version_id=session.version_id,
        expected_status="completed",
    )
    if completed_id != session.session_id:
        raise CapacityProbeError("learning_session_invalid")


async def _exercise_concurrent_sessions(
    api: CapacityApi,
    *,
    material: IdentityMaterial,
    sessions: Sequence[SessionFixture],
    quizzes: Mapping[str, QuizEvidence],
    student: IdentityCredential,
    report_identity: IdentityCredential,
    completed_sessions: set[str],
    end_time: float,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, str]],
    list[dict[str, object]],
]:
    expected_session_count = int(CAPACITY_PROFILE["executedConcurrentSessions"])
    if len(sessions) != expected_session_count:
        raise CapacityProbeError("learning_session_invalid")
    sessions_by_tenant: dict[str, list[SessionFixture]] = {}
    for session in sorted(sessions, key=lambda item: item.sequence):
        sessions_by_tenant.setdefault(session.tenant_id, []).append(session)
    if (
        set(sessions_by_tenant) != set(quizzes)
        or len(sessions_by_tenant) != CAPACITY_PROFILE["executedTenants"]
        or any(len(items) != 4 for items in sessions_by_tenant.values())
    ):
        raise CapacityProbeError("learning_session_invalid")

    tenant_ids = sorted(sessions_by_tenant)

    async def load_baseline(tenant_id: str) -> tuple[str, ReportSnapshot]:
        selected = sessions_by_tenant[tenant_id][0]
        snapshot = await _load_report_snapshot(
            api,
            session=selected,
            report_identity=report_identity,
            knowledge_point_id=quizzes[tenant_id].knowledge_point_id,
            expected_session_count=4,
            expected_completed_count=0,
        )
        if snapshot != ReportSnapshot(0, 0, 0):
            raise CapacityProbeError("teaching_report_invalid")
        return tenant_id, snapshot

    baselines = dict(await _bounded_map(tenant_ids, load_baseline, limit=20))
    start_gate = _StartGate(expected_session_count)

    async def ingest(session: SessionFixture) -> dict[str, object]:
        return await _ingest_session_events(
            api,
            material=material,
            session=session,
            quiz=quizzes[session.tenant_id],
            student=student,
            start_gate=start_gate,
        )

    projection_started = time.perf_counter()
    event_samples = await _bounded_map(list(sessions), ingest, limit=expected_session_count)

    async def wait_projection(tenant_id: str) -> tuple[str, float, ReportSnapshot]:
        baseline = baselines[tenant_id]
        expected = ReportSnapshot(
            baseline.valid_quiz_count + 4,
            baseline.correct_quiz_count + 4,
            baseline.evidence_count + 4,
        )
        selected = sessions_by_tenant[tenant_id][0]
        while True:
            current = await _load_report_snapshot(
                api,
                session=selected,
                report_identity=report_identity,
                knowledge_point_id=quizzes[tenant_id].knowledge_point_id,
                expected_session_count=4,
                expected_completed_count=0,
            )
            if current == expected:
                return (
                    tenant_id,
                    round((time.perf_counter() - projection_started) * 1000, 3),
                    current,
                )
            if (
                current.valid_quiz_count < baseline.valid_quiz_count
                or current.correct_quiz_count < baseline.correct_quiz_count
                or current.evidence_count < baseline.evidence_count
                or current.valid_quiz_count > expected.valid_quiz_count
                or current.correct_quiz_count > expected.correct_quiz_count
                or current.evidence_count > expected.evidence_count
            ):
                raise CapacityProbeError("mastery_projection_invalid")
            await asyncio.sleep(min(0.1, _remaining(end_time)))

    projection_results = await _bounded_map(tenant_ids, wait_projection, limit=20)
    projection_latencies = {
        tenant_id: latency for tenant_id, latency, _snapshot in projection_results
    }
    expected_reports = {tenant_id: snapshot for tenant_id, _latency, snapshot in projection_results}
    projection_samples = [
        {
            "metric": "mastery_projection_visible",
            "tenantId": session.tenant_id,
            "subjectId": session.session_id,
            "sequence": session.sequence,
            "latencyMs": projection_latencies[session.tenant_id],
            "success": True,
        }
        for session in sessions
    ]

    async def complete(session: SessionFixture) -> dict[str, str]:
        result = await _complete_session(api, session=session, student=student)
        completed_sessions.add(session.session_id)
        return result

    completions = await _bounded_map(
        list(sessions),
        complete,
        limit=expected_session_count,
    )
    await _bounded_map(
        list(sessions),
        lambda session: _verify_completed_session(api, session=session, student=student),
        limit=expected_session_count,
    )

    async def reread_report(tenant_id: str) -> None:
        selected = sessions_by_tenant[tenant_id][0]
        current = await _load_report_snapshot(
            api,
            session=selected,
            report_identity=report_identity,
            knowledge_point_id=quizzes[tenant_id].knowledge_point_id,
            expected_session_count=4,
            expected_completed_count=4,
        )
        if current != expected_reports[tenant_id]:
            raise CapacityProbeError("mastery_projection_invalid")

    await _bounded_map(tenant_ids, reread_report, limit=20)
    observations = [
        {
            "sequence": 0,
            "active": [
                {"sessionId": session.session_id, "tenantId": session.tenant_id}
                for session in sorted(sessions, key=lambda item: item.sequence)
            ],
        }
    ]
    return event_samples, projection_samples, completions, observations


async def _exercise_session(
    api: CapacityApi,
    *,
    material: IdentityMaterial,
    session: SessionFixture,
    quiz: QuizEvidence,
    student: IdentityCredential,
    report_identity: IdentityCredential,
    baseline: ReportSnapshot,
    end_time: float,
) -> tuple[ReportSnapshot, dict[str, object], dict[str, object], dict[str, str]]:
    ticket = _validate_ticket(
        await api.tenant_identity_json(
            "POST",
            f"/api/v1/classroom-sessions/{session.session_id}/event-ticket",
            identity=student,
            tenant_id=session.tenant_id,
            expected_statuses=frozenset({200}),
        ),
        "learning_event_ticket_invalid",
    )
    events, event_ids = _learning_events(material=material, session=session, quiz=quiz)
    projection_started = time.perf_counter()
    ingestion_started = time.perf_counter()
    result = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-sessions/{session.session_id}/events",
        identity=student,
        tenant_id=session.tenant_id,
        headers={"X-Classroom-Ticket": ticket},
        json_body={"events": events},
        expected_statuses=frozenset({202}),
    )
    ingestion_latency = round((time.perf_counter() - ingestion_started) * 1000, 3)
    _validate_event_ingestion(result, event_ids)

    expected = ReportSnapshot(
        baseline.valid_quiz_count + 1,
        baseline.correct_quiz_count + 1,
        baseline.evidence_count + 1,
    )
    while True:
        current = await _load_report_snapshot(
            api,
            session=session,
            report_identity=report_identity,
            knowledge_point_id=quiz.knowledge_point_id,
        )
        if current == expected:
            break
        if (
            current.valid_quiz_count < baseline.valid_quiz_count
            or current.correct_quiz_count < baseline.correct_quiz_count
            or current.evidence_count < baseline.evidence_count
            or current.valid_quiz_count > expected.valid_quiz_count
            or current.correct_quiz_count > expected.correct_quiz_count
            or current.evidence_count > expected.evidence_count
        ):
            raise CapacityProbeError("mastery_projection_invalid")
        await asyncio.sleep(min(0.1, _remaining(end_time)))
    projection_latency = round((time.perf_counter() - projection_started) * 1000, 3)

    completed = await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-sessions/{session.session_id}/complete",
        identity=student,
        tenant_id=session.tenant_id,
        expected_statuses=frozenset({200}),
    )
    completed_id = _validate_session_response(
        completed,
        tenant_id=session.tenant_id,
        user_id=student.user_id,
        asset_id=session.asset_id,
        version_id=session.version_id,
        expected_status="completed",
    )
    if completed_id != session.session_id:
        raise CapacityProbeError("learning_session_invalid")
    return (
        current,
        {
            "metric": "event_ingest",
            "tenantId": session.tenant_id,
            "subjectId": session.session_id,
            "sequence": session.sequence,
            "latencyMs": ingestion_latency,
            "success": True,
        },
        {
            "metric": "mastery_projection_visible",
            "tenantId": session.tenant_id,
            "subjectId": session.session_id,
            "sequence": session.sequence,
            "latencyMs": projection_latency,
            "success": True,
        },
        {
            "sessionId": session.session_id,
            "tenantId": session.tenant_id,
            "status": "completed",
        },
    )


async def _cleanup_session(
    api: CapacityApi,
    session: SessionFixture,
    student: IdentityCredential,
) -> None:
    await api.tenant_identity_json(
        "POST",
        f"/api/v1/classroom-sessions/{session.session_id}/complete",
        identity=student,
        tenant_id=session.tenant_id,
        expected_statuses=frozenset({200}),
    )


async def _cleanup_generation(
    api: CapacityApi,
    generation: GenerationFixture,
    student: IdentityCredential,
) -> None:
    await api.tenant_identity_json(
        "POST",
        f"/api/v1/student-classrooms/{generation.asset_id}/cancel",
        identity=student,
        tenant_id=generation.tenant_id,
        expected_statuses=frozenset({202}),
    )


async def _execute_capacity_probe(
    config: ProbeConfig,
    api: CapacityApi,
    *,
    material: IdentityMaterial,
    student: IdentityCredential,
    report_identity: IdentityCredential,
    generations: list[GenerationFixture],
    ready_jobs: set[str],
    sessions: list[SessionFixture],
    completed_sessions: set[str],
    end_time: float,
) -> bytes:
    tenant_values = list(range(int(CAPACITY_PROFILE["executedTenants"])))

    async def create_tenant(sequence: int) -> TenantFixture:
        return await _create_tenant_fixture(
            api,
            sequence=sequence,
            material=material,
            student_user_id=student.user_id,
            report_user_id=report_identity.user_id,
            end_time=end_time,
        )

    tenants = sorted(
        await _bounded_map(tenant_values, create_tenant, limit=10),
        key=lambda item: item.sequence,
    )
    if len({item.tenant_id for item in tenants}) != len(tenant_values):
        raise CapacityProbeError("tenant_fixture_invalid")

    resource_observations = [await _capture_resource(api, sequence=0, phase="baseline")]

    job_tenants: dict[str, str] = {}
    observer_finished = asyncio.Event()
    observer = asyncio.create_task(
        _observe_scheduler(
            api,
            job_tenants=job_tenants,
            finished=observer_finished,
            end_time=end_time,
        )
    )
    job_samples: list[dict[str, object]] = []

    async def submit(specification: tuple[int, TenantFixture]):
        sequence, fixture = specification
        generation, sample = await _submit_generation(
            api,
            sequence=sequence,
            fixture=fixture,
            student=student,
        )
        generations.append(generation)
        job_tenants[generation.job_id] = generation.tenant_id
        return generation, sample

    first_wave = [(0, tenants[0]), (1, tenants[0])]
    first_wave.extend((index + 1, tenants[index]) for index in range(1, len(tenants)))
    try:
        submitted = await _bounded_map(first_wave, submit, limit=20)
        submitted.append(await submit((51, tenants[0])))
        job_samples.extend(sample for _generation, sample in submitted)
        generations.sort(key=lambda item: item.sequence)
        if (
            len(generations) != CAPACITY_WORKLOAD["generationJobsSubmitted"]
            or len({item.job_id for item in generations}) != len(generations)
            or len({item.asset_id for item in generations}) != len(generations)
        ):
            raise CapacityProbeError("generation_submission_invalid")

        async def wait_ready(generation: GenerationFixture) -> ReadyClassroom:
            ready = await _wait_for_ready_classroom(
                api,
                generation=generation,
                student=student,
                end_time=end_time,
            )
            ready_jobs.add(generation.job_id)
            return ready

        ready = sorted(
            await _bounded_map(generations, wait_ready, limit=20),
            key=lambda item: item.sequence,
        )
        observer_finished.set()
        scheduler_claims, scheduler_observations, generation_resource = await observer
    except BaseException:
        observer.cancel()
        try:
            await observer
        except asyncio.CancelledError:
            pass
        raise
    resource_observations.append(generation_resource)

    classroom_by_tenant: dict[str, ReadyClassroom] = {}
    for classroom in ready:
        classroom_by_tenant.setdefault(classroom.tenant_id, classroom)
    if set(classroom_by_tenant) != {tenant.tenant_id for tenant in tenants}:
        raise CapacityProbeError("generation_status_invalid")

    session_specs: list[tuple[int, ReadyClassroom]] = []
    for tenant in tenants:
        classroom = classroom_by_tenant[tenant.tenant_id]
        for offset in range(4):
            session_specs.append((tenant.sequence * 4 + offset, classroom))

    async def create_session(specification: tuple[int, ReadyClassroom]):
        sequence, classroom = specification
        session, sample = await _create_session(
            api,
            sequence=sequence,
            classroom=classroom,
            student=student,
        )
        sessions.append(session)
        return session, sample

    created_sessions = await _bounded_map(session_specs, create_session, limit=40)
    sessions.sort(key=lambda item: item.sequence)
    core_samples = [sample for _session, sample in created_sessions]
    if len(sessions) != CAPACITY_WORKLOAD["learningSessionsStarted"] or len(
        {item.session_id for item in sessions}
    ) != len(sessions):
        raise CapacityProbeError("learning_session_invalid")

    await _bounded_map(
        sessions,
        lambda session: _verify_active_session(api, session=session, student=student),
        limit=40,
    )
    resource_observations.append(
        await _capture_resource(api, sequence=2, phase="sessions_saturated")
    )

    sessions_by_tenant: dict[str, list[SessionFixture]] = {}
    for session in sessions:
        sessions_by_tenant.setdefault(session.tenant_id, []).append(session)

    async def load_quiz(tenant: TenantFixture):
        selected = sessions_by_tenant[tenant.tenant_id][0]
        return tenant.tenant_id, await _load_quiz_evidence(
            api,
            session=selected,
            student=student,
        )

    quiz_pairs = await _bounded_map(tenants, load_quiz, limit=20)
    quizzes = dict(quiz_pairs)
    if len(quizzes) != len(tenants):
        raise CapacityProbeError("quiz_evidence_invalid")

    (
        event_samples,
        projection_samples,
        session_completions,
        session_observations,
    ) = await _exercise_concurrent_sessions(
        api,
        material=material,
        sessions=sessions,
        quizzes=quizzes,
        student=student,
        report_identity=report_identity,
        completed_sessions=completed_sessions,
        end_time=end_time,
    )
    resource_observations.append(await _capture_resource(api, sequence=3, phase="final"))

    raw_samples = sorted(
        core_samples + event_samples + job_samples + projection_samples,
        key=lambda sample: (
            (
                "core_api",
                "event_ingest",
                "job_submission_visible",
                "mastery_projection_visible",
            ).index(str(sample["metric"])),
            int(sample["sequence"]),
        ),
    )
    report = {
        "schemaVersion": CAPACITY_SCHEMA_VERSION,
        "producer": CAPACITY_PRODUCER,
        "capacityModel": CAPACITY_MODEL,
        "candidate": config.candidate,
        "releaseRun": config.release_run,
        "observedAt": _observed_at(),
        "baseUrl": config.base_url,
        "profile": CAPACITY_PROFILE,
        "workload": CAPACITY_WORKLOAD,
        "rawSamples": raw_samples,
        "schedulerSource": "admin-atomic-db-claim-audit",
        "schedulerClaims": scheduler_claims,
        "schedulerObservations": scheduler_observations,
        "sessionObservations": session_observations,
        "sessionCompletions": sorted(
            session_completions,
            key=lambda item: next(
                session.sequence for session in sessions if session.session_id == item["sessionId"]
            ),
        ),
        "resourceSource": CAPACITY_RESOURCE_SOURCE,
        "resourceObservations": resource_observations,
    }
    body = canonical_capacity_profile_report(report)
    parsed = parse_capacity_profile_report(
        body,
        candidate=config.candidate,
        release_run=config.release_run,
        expected_base_url=config.base_url,
    )
    summary = derive_capacity_profile_summary(parsed)
    checks = summary.get("checks")
    if (
        not isinstance(checks, dict)
        or not checks
        or any(value is not True for value in checks.values())
    ):
        raise CapacityProbeError("capacity_thresholds_failed")
    return body


async def _run_capacity_probe(config: ProbeConfig) -> bytes:
    material = _identity_material(config)
    created_usernames: list[str] = []
    generations: list[GenerationFixture] = []
    ready_jobs: set[str] = set()
    sessions: list[SessionFixture] = []
    completed_sessions: set[str] = set()
    failure: BaseException | None = None
    body: bytes | None = None
    cleanup_failed = False
    end_time = time.monotonic() + config.timeout_seconds
    async with CapacityApi(
        config.base_url,
        config.admin_token.get_secret_value(),
    ) as api:
        student: IdentityCredential | None = None
        report_identity: IdentityCredential | None = None
        try:
            async with asyncio.timeout(_remaining(end_time)):
                student_user_id = await _create_identity(
                    api,
                    username=material.student_username,
                    password=material.student_password,
                )
                created_usernames.append(material.student_username)
                report_user_id = await _create_identity(
                    api,
                    username=material.report_username,
                    password=material.report_password,
                )
                created_usernames.append(material.report_username)
                student = await api.login_identity(
                    material.student_username,
                    material.student_password,
                )
                report_identity = await api.login_identity(
                    material.report_username,
                    material.report_password,
                )
                if student.user_id != student_user_id or report_identity.user_id != report_user_id:
                    raise CapacityProbeError("identity_login_failed")
                body = await _execute_capacity_probe(
                    config,
                    api,
                    material=material,
                    student=student,
                    report_identity=report_identity,
                    generations=generations,
                    ready_jobs=ready_jobs,
                    sessions=sessions,
                    completed_sessions=completed_sessions,
                    end_time=end_time,
                )
        except TimeoutError:
            failure = CapacityProbeError("capacity_probe_timeout")
        except BaseException as exc:
            failure = exc

        resource_cleanup_operations: list[Callable[[], Awaitable[None]]] = []
        if student is not None:
            for session in reversed(sessions):
                if session.session_id in completed_sessions:
                    continue

                async def cleanup_session(session: SessionFixture = session) -> None:
                    await _cleanup_session(api, session, student)

                resource_cleanup_operations.append(cleanup_session)
            for generation in reversed(generations):
                if generation.job_id in ready_jobs:
                    continue

                async def cleanup_generation(
                    generation: GenerationFixture = generation,
                ) -> None:
                    await _cleanup_generation(api, generation, student)

                resource_cleanup_operations.append(cleanup_generation)
        identity_cleanup_operations: list[Callable[[], Awaitable[None]]] = []
        for username in reversed(created_usernames):

            async def delete_identity(username: str = username) -> None:
                await _delete_identity(api, username)

            identity_cleanup_operations.append(delete_identity)
        try:
            cleanup_failed = await _run_cleanup_operations(
                resource_cleanup_operations,
                limit=_CLEANUP_CONCURRENCY,
                timeout_seconds=_CLEANUP_TIMEOUT_SECONDS,
            )
        except BaseException:
            cleanup_failed = True
        try:
            cleanup_failed = (
                await _run_cleanup_operations(
                    identity_cleanup_operations,
                    limit=_CLEANUP_CONCURRENCY,
                    timeout_seconds=_CLEANUP_TIMEOUT_SECONDS,
                )
                or cleanup_failed
            )
        except BaseException:
            cleanup_failed = True
    if failure is not None:
        if cleanup_failed:
            raise CapacityProbeError("capacity_probe_and_cleanup_failed") from failure
        raise failure
    if cleanup_failed:
        raise CapacityProbeError("capacity_cleanup_failed")
    if body is None:
        raise CapacityProbeError("capacity_probe_failed")
    return body


def main(argv: Sequence[str] | None = None) -> int:
    _parse_args(argv)
    try:
        config = _load_config(os.environ, cwd=Path.cwd())
        body = asyncio.run(_run_capacity_probe(config))
    except CapacityProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("capacity_probe_interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("capacity_probe_failed", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
