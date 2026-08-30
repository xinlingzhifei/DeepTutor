from __future__ import annotations

import asyncio
import hashlib
from http.cookies import SimpleCookie
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://candidate.example.test"
ADMIN_TOKEN = "platform-admin-token-must-never-appear"
TEACHER_SESSION = "teacher-session-must-never-appear"
RUNTIME_ATTESTATION_SHA256 = "a" * 64


def _load_module(name: str, path: Path):
    assert path.is_file(), "OpenMAIC shared smoke probe is missing"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _probe_module():
    return _load_module(
        "openmaic_smoke_probe_under_test",
        ROOT / "scripts" / "openmaic_smoke_probe.py",
    )


def _contract_module():
    return _load_module(
        "openmaic_smoke_contract_for_probe_test",
        ROOT / "scripts" / "openmaic_smoke_contract.py",
    )


def _candidate() -> dict[str, object]:
    return {
        "sourceRepository": "xinlingzhifei/DeepTutor",
        "sourceHead": "c" * 40,
        "releaseTag": "yfeistai-first-release-20260829-cccccccc",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": "sha256:" + "d" * 64,
            "openmaic": "sha256:" + "e" * 64,
            "openmaic_render": "sha256:" + "f" * 64,
        },
    }


def _tenant_create_idempotency_key() -> str:
    binding = f"{_candidate()['sourceHead']}\0run-openmaic-shared-smoke\0tenant"
    return f"openmaic-shared-{hashlib.sha256(binding.encode()).hexdigest()[:24]}"


def test_shared_probe_loads_only_the_fixed_candidate_bound_environment(tmp_path: Path) -> None:
    module = _probe_module()
    root = tmp_path / "candidate"
    root.mkdir()
    environment = {
        "YFEISTAI_LIVE_FIXTURE_TOKEN": ADMIN_TOKEN,
        "YFEISTAI_CANDIDATE_ROOT": str(root),
        "YFEISTAI_RELEASE_RUN_ID": "run-openmaic-shared-smoke",
        "YFEISTAI_ENVIRONMENT_ID": "environment-openmaic-shared-smoke",
        "YFEISTAI_RUNTIME_ATTESTATION_SHA256": RUNTIME_ATTESTATION_SHA256,
        "YFEISTAI_OPENMAIC_SMOKE_TIMEOUT_SECONDS": "570",
        "WEB_BASE_URL": BASE_URL,
    }

    config = module._load_config(
        environment,
        plane="shared",
        cwd=root,
        candidate_loader=lambda _root: _candidate(),
    )

    assert config.plane == "shared"
    assert config.dedicated_tenant_id is None
    assert config.candidate_root == root.resolve()
    assert config.candidate == _candidate()
    assert config.release_run == {
        "runId": "run-openmaic-shared-smoke",
        "environmentId": "environment-openmaic-shared-smoke",
    }
    assert config.runtime_attestation_sha256 == RUNTIME_ATTESTATION_SHA256
    assert config.timeout_seconds == 570
    assert ADMIN_TOKEN not in repr(config)


def test_dedicated_probe_loads_only_a_strict_pre_registered_tenant(
    tmp_path: Path,
) -> None:
    module = _probe_module()
    root = tmp_path / "candidate"
    root.mkdir()
    environment = {
        "YFEISTAI_LIVE_FIXTURE_TOKEN": ADMIN_TOKEN,
        "YFEISTAI_CANDIDATE_ROOT": str(root),
        "YFEISTAI_RELEASE_RUN_ID": "run-openmaic-dedicated-smoke",
        "YFEISTAI_ENVIRONMENT_ID": "environment-openmaic-dedicated-smoke",
        "YFEISTAI_RUNTIME_ATTESTATION_SHA256": RUNTIME_ATTESTATION_SHA256,
        "YFEISTAI_OPENMAIC_SMOKE_TIMEOUT_SECONDS": "570",
        "YFEISTAI_DEDICATED_TENANT_ID": "tenant-dedicated-smoke",
        "WEB_BASE_URL": BASE_URL,
    }

    arguments = module._parse_args(["--plane", "dedicated", "--profile", "first-release"])
    config = module._load_config(
        environment,
        plane=arguments.plane,
        cwd=root,
        candidate_loader=lambda _root: _candidate(),
    )

    assert config.plane == "dedicated"
    assert config.dedicated_tenant_id == "tenant-dedicated-smoke"
    assert ADMIN_TOKEN not in repr(config)

    for invalid in (None, "", "../tenant", "tenant dedicated"):
        changed = dict(environment)
        if invalid is None:
            changed.pop("YFEISTAI_DEDICATED_TENANT_ID")
        else:
            changed["YFEISTAI_DEDICATED_TENANT_ID"] = invalid
        with pytest.raises(
            module.OpenMAICSmokeProbeError,
            match="dedicated_tenant_invalid",
        ):
            module._load_config(
                changed,
                plane="dedicated",
                cwd=root,
                candidate_loader=lambda _root: _candidate(),
            )


def _classroom(
    *,
    status: str,
    lifecycle_state: str,
    course_id: str,
    class_id: str,
    classroom_version_id: str | None = None,
) -> dict[str, object]:
    has_outline = lifecycle_state not in {"generating_outline"}
    return {
        "assetId": "asset-shared-smoke",
        "draftId": "draft-shared-smoke",
        "jobId": "job-shared-smoke",
        "lifecycleState": lifecycle_state,
        "status": status,
        "title": "OpenMAIC shared-plane acceptance",
        "courseId": course_id,
        "classId": class_id,
        "ownerId": "teacher-user-shared-smoke",
        "revision": 1,
        "outline": {"title": "Shared-plane acceptance outline"} if has_outline else None,
        "document": (
            {
                "dslVersion": "0.1.0",
                "classroomId": "asset-shared-smoke",
                "classroomVersionId": classroom_version_id,
            }
            if classroom_version_id is not None
            else None
        ),
        "classroomVersionId": classroom_version_id,
        "confirmedOutlineSha256": "b" * 64,
        "validationReport": None,
        "idempotencyKey": "openmaic-shared-smoke-classroom",
    }


def _job(
    status: str,
    progress_percent: int,
    *,
    phase: str = "content",
) -> dict[str, object]:
    return {
        "job_id": "job-shared-smoke",
        "job_kind": "generation",
        "phase": phase,
        "status": status,
        "progress_percent": progress_percent,
        "waiting_reason": None,
        "cancellable": status != "succeeded",
        "retryable": False,
        "outline": (
            {"title": "Shared-plane acceptance outline"}
            if phase == "outline" and status == "awaiting_confirmation"
            else None
        ),
        "error_category": None,
        "error_code": None,
        "retry_of_job_id": None,
        "export_format": None,
        "download_ready": False,
    }


def _request_json(request: httpx.Request) -> dict[str, object]:
    parsed = json.loads(request.content)
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize(
    "mutation",
    ("classroom_id", "classroom_version_id", "dsl_version", "content_length", "etag"),
)
def test_teacher_document_rejects_unbound_or_header_mismatched_materialization(
    mutation: str,
) -> None:
    module = _probe_module()
    payload: dict[str, object] = {
        "schemaVersion": "1.0",
        "classroomId": "asset-shared-smoke",
        "classroomVersionId": "version-shared-smoke",
        "openmaic": {"dslVersion": "0.1.0"},
    }
    if mutation == "classroom_id":
        payload["classroomId"] = "asset-other"
    elif mutation == "classroom_version_id":
        payload["classroomVersionId"] = "version-other"
    elif mutation == "dsl_version":
        payload["openmaic"] = {"dslVersion": "0.0.0"}
    document = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    digest = hashlib.sha256(document).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(document) + (1 if mutation == "content_length" else 0)),
        "ETag": f'"sha256-{("0" * 64 if mutation == "etag" else digest)}"',
    }

    async def exercise() -> None:
        async with module._OpenMAICSmokeApi(
            base_url=BASE_URL,
            admin_token=ADMIN_TOKEN,
            timeout_seconds=30,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=document, headers=headers)
            ),
        ) as api:
            with pytest.raises(module.OpenMAICSmokeProbeError, match="classroom_document_invalid"):
                await api.teacher_document(
                    "/api/v1/classroom-versions/version-shared-smoke/document",
                    tenant_id="tenant-shared-smoke",
                    asset_id="asset-shared-smoke",
                    classroom_version_id="version-shared-smoke",
                )

    asyncio.run(exercise())


@pytest.mark.parametrize("create_already_ready", (False, True))
def test_shared_probe_uses_one_admin_token_and_formal_two_stage_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    create_already_ready: bool,
) -> None:
    module = _probe_module()
    contract = _contract_module()
    captured_forbidden_secrets: tuple[bytes, ...] = ()
    original_parse = module.parse_openmaic_smoke_report

    def capture_parse(*args: object, **kwargs: object):
        nonlocal captured_forbidden_secrets
        raw = kwargs.get("forbidden_secret_values")
        assert isinstance(raw, tuple)
        captured_forbidden_secrets = raw
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(module, "parse_openmaic_smoke_report", capture_parse)
    document = (
        b'{"classroomId":"asset-shared-smoke","classroomVersionId":'
        b'"version-shared-smoke","openmaic":{"dslVersion":"0.1.0"},'
        b'"schemaVersion":"1.0"}\n'
    )
    release_run = {
        "runId": "run-openmaic-shared-smoke",
        "environmentId": "environment-openmaic-shared-smoke",
    }
    config = module.ProbeConfig(
        admin_token=module.SecretStr(ADMIN_TOKEN),
        base_url=BASE_URL,
        candidate=_candidate(),
        candidate_root=tmp_path,
        dedicated_tenant_id=None,
        plane="shared",
        release_run=release_run,
        runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        timeout_seconds=30,
    )
    assert ADMIN_TOKEN not in repr(config)
    monkeypatch.setattr(module.secrets, "token_bytes", lambda _size: b"\x01" * 16)
    expected_material = module._fixture_material(config)
    assert expected_material.tenant_name != "OpenMAIC shared-plane acceptance"
    assert expected_material.resource_suffix != "shared-smoke"
    course_id = f"course-{expected_material.resource_suffix}"
    class_id = f"class-{expected_material.resource_suffix}"

    seen: list[httpx.Request] = []
    provisioning_reads = 0
    job_reads = 0
    classroom_reads = 0
    teacher_username = ""
    teacher_password = ""

    def assert_admin(request: httpx.Request) -> None:
        assert request.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"
        assert TEACHER_SESSION not in request.headers.get("Cookie", "")
        assert ADMIN_TOKEN not in str(request.url)
        assert ADMIN_TOKEN.encode() not in request.content

    def assert_selected_admin(request: httpx.Request) -> None:
        assert_admin(request)
        cookies = SimpleCookie()
        cookies.load(request.headers["Cookie"])
        assert {name: morsel.value for name, morsel in cookies.items()} == {
            "dt_tenant": "tenant-shared-smoke"
        }

    def assert_teacher(request: httpx.Request) -> None:
        assert "Authorization" not in request.headers
        assert request.headers["X-Tenant-ID"] == "tenant-shared-smoke"
        cookies = SimpleCookie()
        cookies.load(request.headers["Cookie"])
        assert {name: morsel.value for name, morsel in cookies.items()} == {
            "dt_token": TEACHER_SESSION,
            "dt_tenant": "tenant-shared-smoke",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal classroom_reads, job_reads, provisioning_reads, teacher_password, teacher_username
        seen.append(request)
        method = request.method
        path = request.url.path

        if (method, path) == ("POST", "/api/v1/tenants"):
            assert_admin(request)
            assert _request_json(request) == {"name": expected_material.tenant_name}
            assert request.headers["Idempotency-Key"] == expected_material.tenant_idempotency_key
            assert request.headers["Idempotency-Key"] == _tenant_create_idempotency_key()
            assert request.headers["Idempotency-Key"] != "openmaic-shared-smoke-tenant"
            return httpx.Response(
                202,
                json={
                    "tenant_id": "tenant-shared-smoke",
                    "status": "provisioning",
                    "job_id": "provision-shared-smoke",
                },
            )
        if (method, path) == (
            "GET",
            "/api/v1/tenants/tenant-shared-smoke/provisioning",
        ):
            assert_admin(request)
            provisioning_reads += 1
            return httpx.Response(
                200,
                json={
                    "tenant_id": "tenant-shared-smoke",
                    "status": "provisioning" if provisioning_reads == 1 else "active",
                    "job_id": "provision-shared-smoke",
                    "job_status": "running" if provisioning_reads == 1 else "completed",
                    "attempt_count": provisioning_reads - 1,
                },
            )
        if (method, path) == ("PUT", "/api/v1/tenants/active"):
            if "Authorization" in request.headers:
                assert_admin(request)
                assert "Cookie" not in request.headers
            else:
                cookies = SimpleCookie()
                cookies.load(request.headers["Cookie"])
                assert {name: morsel.value for name, morsel in cookies.items()} == {
                    "dt_token": TEACHER_SESSION
                }
            assert _request_json(request) == {"tenant_id": "tenant-shared-smoke"}
            return httpx.Response(
                200,
                headers={
                    "Set-Cookie": "dt_tenant=tenant-shared-smoke; Path=/; HttpOnly; SameSite=Lax"
                },
                json={"active_tenant_id": "tenant-shared-smoke"},
            )
        if (method, path) == ("GET", "/api/v1/auth/users"):
            assert_selected_admin(request)
            return httpx.Response(200, json=[])
        if (method, path) == ("POST", "/api/v1/auth/users"):
            assert_selected_admin(request)
            payload = _request_json(request)
            assert set(payload) == {"username", "password"}
            teacher_username = str(payload["username"])
            teacher_password = str(payload["password"])
            assert teacher_username == expected_material.teacher_username
            assert teacher_password == expected_material.teacher_password.get_secret_value()
            assert teacher_password and teacher_password != ADMIN_TOKEN
            return httpx.Response(
                201,
                json={
                    "ok": True,
                    "user_id": "teacher-user-shared-smoke",
                    "username": teacher_username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if (method, path) == (
            "POST",
            "/api/v1/tenants/tenant-shared-smoke/members",
        ):
            assert_selected_admin(request)
            assert request.headers["X-Tenant-ID"] == "tenant-shared-smoke"
            assert _request_json(request) == {
                "user_id": "teacher-user-shared-smoke",
                "role": "teacher",
            }
            return httpx.Response(
                200,
                json={
                    "tenant_id": "tenant-shared-smoke",
                    "user_id": "teacher-user-shared-smoke",
                    "roles": ["teacher"],
                    "grants": [
                        {
                            "role": "teacher",
                            "scope_type": "tenant",
                            "scope_id": "tenant-shared-smoke",
                        }
                    ],
                },
            )
        if (method, path) == ("POST", "/api/v1/auth/login"):
            assert "Authorization" not in request.headers
            assert _request_json(request) == {
                "username": teacher_username,
                "password": teacher_password,
            }
            return httpx.Response(
                200,
                headers={
                    "Set-Cookie": (f"dt_token={TEACHER_SESSION}; Path=/; HttpOnly; SameSite=Lax")
                },
                json={
                    "ok": True,
                    "user_id": "teacher-user-shared-smoke",
                    "username": teacher_username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if (method, path) == ("POST", "/api/v1/teaching/courses"):
            assert_selected_admin(request)
            assert request.headers["X-Tenant-ID"] == "tenant-shared-smoke"
            payload = _request_json(request)
            assert payload == {
                "id": course_id,
                "title": "OpenMAIC shared-plane acceptance",
            }
            return httpx.Response(
                201,
                json={
                    **payload,
                    "status": "active",
                    "createdAt": "2026-08-29T00:00:00Z",
                },
            )
        if (method, path) == (
            "POST",
            f"/api/v1/teaching/courses/{course_id}/classes",
        ):
            assert_teacher(request)
            payload = _request_json(request)
            assert payload == {
                "id": class_id,
                "name": "OpenMAIC shared-plane acceptance",
            }
            return httpx.Response(
                201,
                json={
                    **payload,
                    "courseId": course_id,
                    "status": "active",
                    "createdAt": "2026-08-29T00:00:00Z",
                },
            )
        if (method, path) == (
            "POST",
            f"/api/v1/teaching/classes/{class_id}/enrollments",
        ):
            assert_teacher(request)
            assert _request_json(request) == {"userId": "teacher-user-shared-smoke"}
            return httpx.Response(
                201,
                json={
                    "classId": class_id,
                    "userId": "teacher-user-shared-smoke",
                    "status": "active",
                    "createdAt": "2026-08-29T00:00:00Z",
                },
            )
        if (method, path) == (
            "POST",
            "/api/v1/teaching/generation-quota-grants",
        ):
            assert_selected_admin(request)
            assert request.headers["X-Tenant-ID"] == "tenant-shared-smoke"
            assert _request_json(request) == {"units": 20}
            return httpx.Response(
                200,
                json={
                    "grantId": "quota-shared-smoke",
                    "tenantId": "tenant-shared-smoke",
                    "units": 20,
                    "balance": 20,
                    "created": True,
                },
            )
        if (method, path) == ("POST", "/api/v1/classrooms"):
            assert_teacher(request)
            payload = _request_json(request)
            assert payload["courseId"] == course_id
            assert payload["classId"] == class_id
            assert payload["classroomMode"] == "full"
            assert payload["contentMode"] == "open_creation"
            assert payload["openCreationAcknowledged"] is True
            assert payload["requestedExports"] == ["offline_html"]
            return httpx.Response(
                202,
                json=_classroom(
                    status=("awaiting_confirmation" if create_already_ready else "quota_reserved"),
                    lifecycle_state=(
                        "awaiting_outline" if create_already_ready else "generating_outline"
                    ),
                    course_id=course_id,
                    class_id=class_id,
                ),
            )
        if (method, path) == (
            "POST",
            "/api/v1/classrooms/asset-shared-smoke/confirm-outline",
        ):
            assert_teacher(request)
            assert request.content == b""
            return httpx.Response(
                202,
                json=_classroom(
                    status="queued",
                    lifecycle_state="generating_content",
                    course_id=course_id,
                    class_id=class_id,
                ),
            )
        if (method, path) == ("GET", "/api/v1/classroom-jobs/job-shared-smoke"):
            assert_teacher(request)
            job_reads += 1
            if job_reads == 1:
                response = _job("generating_outline", 30, phase="outline")
            elif job_reads == 2:
                response = _job("awaiting_confirmation", 50, phase="outline")
            elif job_reads == 3:
                response = _job("generating_content", 70)
            else:
                response = _job("succeeded", 100)
            return httpx.Response(
                200,
                json=response,
            )
        if (method, path) == ("GET", "/api/v1/classrooms/asset-shared-smoke"):
            assert_teacher(request)
            classroom_reads += 1
            if classroom_reads == 1:
                response = _classroom(
                    status="generating_outline",
                    lifecycle_state="generating_outline",
                    course_id=course_id,
                    class_id=class_id,
                )
            elif classroom_reads == 2:
                response = _classroom(
                    status="awaiting_confirmation",
                    lifecycle_state="awaiting_outline",
                    course_id=course_id,
                    class_id=class_id,
                )
            elif classroom_reads == 3:
                response = _classroom(
                    status="generating_content",
                    lifecycle_state="generating_content",
                    course_id=course_id,
                    class_id=class_id,
                )
            else:
                response = _classroom(
                    status="succeeded",
                    lifecycle_state="editing",
                    course_id=course_id,
                    class_id=class_id,
                    classroom_version_id="version-shared-smoke",
                )
            return httpx.Response(
                200,
                json=response,
            )
        if (method, path) == (
            "GET",
            "/api/v1/system/classroom-jobs/tenant-shared-smoke/job-shared-smoke/binding",
        ):
            assert_selected_admin(request)
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 1,
                    "tenantId": "tenant-shared-smoke",
                    "jobId": "job-shared-smoke",
                    "jobKind": "generation",
                    "phase": "content",
                    "status": "succeeded",
                    "progressPercent": 100,
                    "classroomVersionId": "version-shared-smoke",
                    "dataPlaneMode": "shared",
                    "dataPlaneRouteId": "shared-primary",
                    "routeTenantId": None,
                    "routeOwnerKey": "shared",
                    "providerProfileId": "platform-default",
                    "providerScope": "shared",
                    "providerTenantId": None,
                    "providerOwnerKey": "shared",
                    "workerPoolRef": "shared-generation",
                    "queueRef": "openmaic.shared",
                    "attemptCount": 1,
                    "sharedRouteAttemptCount": 1,
                    "dedicatedRouteAttemptCount": 0,
                    "selectedRouteAttemptCount": 1,
                    "unavailableRouteAttemptCount": 0,
                    "routeAttemptHistoryComplete": True,
                },
            )
        if (method, path) == (
            "GET",
            "/api/v1/classroom-versions/version-shared-smoke/document",
        ):
            assert_teacher(request)
            return httpx.Response(
                200,
                content=document,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(document)),
                    "ETag": f'"sha256-{hashlib.sha256(document).hexdigest()}"',
                },
            )
        if (method, path) == (
            "DELETE",
            f"/api/v1/teaching/classes/{class_id}/enrollments/teacher-user-shared-smoke",
        ):
            assert_selected_admin(request)
            assert request.headers["X-Tenant-ID"] == "tenant-shared-smoke"
            assert request.content == b""
            return httpx.Response(204)
        if (method, path) == (
            "DELETE",
            "/api/v1/tenants/tenant-shared-smoke/members/teacher-user-shared-smoke",
        ):
            assert_selected_admin(request)
            assert request.headers["X-Tenant-ID"] == "tenant-shared-smoke"
            assert _request_json(request) == {
                "expected_tenant_id": "tenant-shared-smoke",
                "expected_user_id": "teacher-user-shared-smoke",
            }
            return httpx.Response(204)
        if (method, path) == ("DELETE", f"/api/v1/auth/users/{teacher_username}"):
            assert_selected_admin(request)
            assert _request_json(request) == {"expected_user_id": "teacher-user-shared-smoke"}
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {method} {path}")

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    body = asyncio.run(
        module._run_openmaic_smoke_probe(
            config,
            transport=httpx.MockTransport(handler),
        )
    )

    assert teacher_password
    for secret in (ADMIN_TOKEN, teacher_password, TEACHER_SESSION):
        assert secret.encode() not in body
    assert TEACHER_SESSION.encode() in captured_forbidden_secrets
    parsed = contract.parse_openmaic_smoke_report(
        body,
        candidate=_candidate(),
        release_run=release_run,
        expected_base_url=BASE_URL,
        expected_runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        forbidden_secret_values=tuple(
            secret.encode() for secret in (ADMIN_TOKEN, teacher_password, TEACHER_SESSION)
        ),
    )
    assert contract.canonical_openmaic_smoke_report(parsed) == body
    assert parsed["fixture"] == {
        "tenantId": "tenant-shared-smoke",
        "teacherUserId": "teacher-user-shared-smoke",
        "courseId": course_id,
        "classId": class_id,
    }
    assert parsed["binding"] == {
        "routeId": "shared-primary",
        "providerProfileId": "platform-default",
        "workerPoolRef": "shared-generation",
        "queueRef": "openmaic.shared",
    }
    assert parsed["generation"] == {
        "jobId": "job-shared-smoke",
        "jobStatus": "succeeded",
        "assetId": "asset-shared-smoke",
        "classroomStatus": "succeeded",
        "classroomVersionId": "version-shared-smoke",
        "documentSha256": hashlib.sha256(document).hexdigest(),
        "documentSizeBytes": len(document),
        "documentEtag": f'"sha256-{hashlib.sha256(document).hexdigest()}"',
    }
    assert contract.derive_openmaic_shared_plane_checks(parsed) == {"sharedGenerationPassed": True}
    assert provisioning_reads == 2
    assert job_reads == 4
    assert classroom_reads == 4
    assert [(request.method, request.url.path) for request in seen] == [
        ("POST", "/api/v1/tenants"),
        ("GET", "/api/v1/tenants/tenant-shared-smoke/provisioning"),
        ("GET", "/api/v1/tenants/tenant-shared-smoke/provisioning"),
        ("PUT", "/api/v1/tenants/active"),
        ("GET", "/api/v1/auth/users"),
        ("POST", "/api/v1/auth/users"),
        ("POST", "/api/v1/tenants/tenant-shared-smoke/members"),
        ("POST", "/api/v1/auth/login"),
        ("PUT", "/api/v1/tenants/active"),
        ("POST", "/api/v1/teaching/courses"),
        ("POST", f"/api/v1/teaching/courses/{course_id}/classes"),
        ("POST", f"/api/v1/teaching/classes/{class_id}/enrollments"),
        ("POST", "/api/v1/teaching/generation-quota-grants"),
        ("POST", "/api/v1/classrooms"),
        ("GET", "/api/v1/classroom-jobs/job-shared-smoke"),
        ("GET", "/api/v1/classroom-jobs/job-shared-smoke"),
        ("GET", "/api/v1/classrooms/asset-shared-smoke"),
        ("GET", "/api/v1/classrooms/asset-shared-smoke"),
        ("POST", "/api/v1/classrooms/asset-shared-smoke/confirm-outline"),
        ("GET", "/api/v1/classroom-jobs/job-shared-smoke"),
        ("GET", "/api/v1/classroom-jobs/job-shared-smoke"),
        ("GET", "/api/v1/classrooms/asset-shared-smoke"),
        ("GET", "/api/v1/classrooms/asset-shared-smoke"),
        (
            "GET",
            "/api/v1/system/classroom-jobs/tenant-shared-smoke/job-shared-smoke/binding",
        ),
        ("GET", "/api/v1/classroom-versions/version-shared-smoke/document"),
        (
            "DELETE",
            f"/api/v1/teaching/classes/{class_id}/enrollments/teacher-user-shared-smoke",
        ),
        (
            "DELETE",
            "/api/v1/tenants/tenant-shared-smoke/members/teacher-user-shared-smoke",
        ),
        ("DELETE", f"/api/v1/auth/users/{teacher_username}"),
    ]


def test_dedicated_probe_uses_pre_registered_tenant_without_provisioning_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _probe_module()
    contract = _contract_module()
    tenant_id = "tenant-dedicated-smoke"
    teacher_user_id = "teacher-user-dedicated-smoke"
    asset_id = "asset-dedicated-smoke"
    job_id = "job-dedicated-smoke"
    version_id = "version-dedicated-smoke"
    release_run = {
        "runId": "run-openmaic-dedicated-smoke",
        "environmentId": "environment-openmaic-dedicated-smoke",
    }
    config = module.ProbeConfig(
        admin_token=module.SecretStr(ADMIN_TOKEN),
        base_url=BASE_URL,
        candidate=_candidate(),
        candidate_root=tmp_path,
        dedicated_tenant_id=tenant_id,
        plane="dedicated",
        release_run=release_run,
        runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        timeout_seconds=30,
    )
    monkeypatch.setattr(module.secrets, "token_bytes", lambda _size: b"\x02" * 16)
    material = module._fixture_material(config)
    course_id = f"course-{material.resource_suffix}"
    class_id = f"class-{material.resource_suffix}"
    document = (
        b'{"classroomId":"asset-dedicated-smoke","classroomVersionId":'
        b'"version-dedicated-smoke","openmaic":{"dslVersion":"0.1.0"},'
        b'"schemaVersion":"1.0"}\n'
    )
    seen: list[httpx.Request] = []
    job_reads = 0
    classroom_reads = 0
    teacher_username = ""
    teacher_password = ""

    def classroom(
        *,
        status: str,
        lifecycle_state: str,
        classroom_version_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "assetId": asset_id,
            "draftId": "draft-dedicated-smoke",
            "jobId": job_id,
            "lifecycleState": lifecycle_state,
            "status": status,
            "title": "OpenMAIC dedicated-plane acceptance",
            "courseId": course_id,
            "classId": class_id,
            "ownerId": teacher_user_id,
            "revision": 1,
            "outline": (
                None
                if lifecycle_state == "generating_outline"
                else {"title": "Dedicated-plane acceptance outline"}
            ),
            "document": (
                {
                    "dslVersion": "0.1.0",
                    "classroomId": asset_id,
                    "classroomVersionId": classroom_version_id,
                }
                if classroom_version_id is not None
                else None
            ),
            "classroomVersionId": classroom_version_id,
            "confirmedOutlineSha256": "b" * 64,
            "validationReport": None,
            "idempotencyKey": material.classroom_idempotency_key,
        }

    def job(status: str, progress: int, *, phase: str = "content") -> dict[str, object]:
        return {
            "job_id": job_id,
            "job_kind": "generation",
            "phase": phase,
            "status": status,
            "progress_percent": progress,
            "waiting_reason": None,
            "cancellable": status != "succeeded",
            "retryable": False,
            "outline": (
                {"title": "Dedicated-plane acceptance outline"}
                if phase == "outline" and status == "awaiting_confirmation"
                else None
            ),
            "error_category": None,
            "error_code": None,
            "retry_of_job_id": None,
            "export_format": None,
            "download_ready": False,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal classroom_reads, job_reads, teacher_password, teacher_username
        seen.append(request)
        method = request.method
        path = request.url.path
        if (method, path) == ("PUT", "/api/v1/tenants/active"):
            cookie = (
                f"dt_tenant={tenant_id}; Path=/; HttpOnly; SameSite=Lax"
                if "Authorization" in request.headers
                else f"dt_tenant={tenant_id}; Path=/; HttpOnly; SameSite=Lax"
            )
            assert _request_json(request) == {"tenant_id": tenant_id}
            return httpx.Response(
                200,
                headers={"Set-Cookie": cookie},
                json={"active_tenant_id": tenant_id},
            )
        if (method, path) == ("GET", "/api/v1/auth/users"):
            return httpx.Response(200, json=[])
        if (method, path) == ("POST", "/api/v1/auth/users"):
            payload = _request_json(request)
            teacher_username = str(payload["username"])
            teacher_password = str(payload["password"])
            return httpx.Response(
                201,
                json={
                    "ok": True,
                    "user_id": teacher_user_id,
                    "username": teacher_username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if (method, path) == ("POST", f"/api/v1/tenants/{tenant_id}/members"):
            return httpx.Response(
                200,
                json={
                    "tenant_id": tenant_id,
                    "user_id": teacher_user_id,
                    "roles": ["teacher"],
                    "grants": [
                        {
                            "role": "teacher",
                            "scope_type": "tenant",
                            "scope_id": tenant_id,
                        }
                    ],
                },
            )
        if (method, path) == ("POST", "/api/v1/auth/login"):
            assert _request_json(request) == {
                "username": teacher_username,
                "password": teacher_password,
            }
            return httpx.Response(
                200,
                headers={
                    "Set-Cookie": (f"dt_token={TEACHER_SESSION}; Path=/; HttpOnly; SameSite=Lax")
                },
                json={
                    "ok": True,
                    "user_id": teacher_user_id,
                    "username": teacher_username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if (method, path) == ("POST", "/api/v1/teaching/courses"):
            payload = _request_json(request)
            return httpx.Response(201, json={**payload, "status": "active"})
        if (method, path) == (
            "POST",
            f"/api/v1/teaching/courses/{course_id}/classes",
        ):
            payload = _request_json(request)
            return httpx.Response(
                201,
                json={**payload, "courseId": course_id, "status": "active"},
            )
        if (method, path) == (
            "POST",
            f"/api/v1/teaching/classes/{class_id}/enrollments",
        ):
            return httpx.Response(
                201,
                json={
                    "classId": class_id,
                    "userId": teacher_user_id,
                    "status": "active",
                },
            )
        if (method, path) == ("POST", "/api/v1/teaching/generation-quota-grants"):
            return httpx.Response(
                200,
                json={"tenantId": tenant_id, "units": 20, "balance": 20},
            )
        if (method, path) == ("POST", "/api/v1/classrooms"):
            return httpx.Response(
                202,
                json=classroom(status="quota_reserved", lifecycle_state="generating_outline"),
            )
        if (method, path) == ("GET", f"/api/v1/classroom-jobs/{job_id}"):
            job_reads += 1
            responses = (
                job("generating_outline", 30, phase="outline"),
                job("awaiting_confirmation", 50, phase="outline"),
                job("generating_content", 70),
                job("succeeded", 100),
            )
            return httpx.Response(200, json=responses[min(job_reads - 1, 3)])
        if (method, path) == ("GET", f"/api/v1/classrooms/{asset_id}"):
            classroom_reads += 1
            responses = (
                classroom(status="generating_outline", lifecycle_state="generating_outline"),
                classroom(status="awaiting_confirmation", lifecycle_state="awaiting_outline"),
                classroom(status="generating_content", lifecycle_state="generating_content"),
                classroom(
                    status="succeeded",
                    lifecycle_state="editing",
                    classroom_version_id=version_id,
                ),
            )
            return httpx.Response(200, json=responses[min(classroom_reads - 1, 3)])
        if (method, path) == (
            "POST",
            f"/api/v1/classrooms/{asset_id}/confirm-outline",
        ):
            return httpx.Response(
                202,
                json=classroom(status="queued", lifecycle_state="generating_content"),
            )
        if (method, path) == (
            "GET",
            f"/api/v1/system/classroom-jobs/{tenant_id}/{job_id}/binding",
        ):
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 1,
                    "tenantId": tenant_id,
                    "jobId": job_id,
                    "jobKind": "generation",
                    "phase": "content",
                    "status": "succeeded",
                    "progressPercent": 100,
                    "classroomVersionId": version_id,
                    "dataPlaneMode": "dedicated",
                    "dataPlaneRouteId": "dedicated-tenant-smoke",
                    "routeTenantId": tenant_id,
                    "routeOwnerKey": tenant_id,
                    "providerProfileId": "provider-tenant-smoke",
                    "providerScope": "dedicated",
                    "providerTenantId": tenant_id,
                    "providerOwnerKey": tenant_id,
                    "workerPoolRef": "generation-tenant-smoke",
                    "queueRef": "openmaic.tenant-smoke",
                    "attemptCount": 2,
                    "sharedRouteAttemptCount": 0,
                    "dedicatedRouteAttemptCount": 2,
                    "selectedRouteAttemptCount": 1,
                    "unavailableRouteAttemptCount": 1,
                    "routeAttemptHistoryComplete": True,
                },
            )
        if (method, path) == (
            "GET",
            f"/api/v1/classroom-versions/{version_id}/document",
        ):
            digest = hashlib.sha256(document).hexdigest()
            return httpx.Response(
                200,
                content=document,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(document)),
                    "ETag": f'"sha256-{digest}"',
                },
            )
        if (method, path) == (
            "DELETE",
            f"/api/v1/teaching/classes/{class_id}/enrollments/{teacher_user_id}",
        ):
            return httpx.Response(204)
        if (method, path) == (
            "DELETE",
            f"/api/v1/tenants/{tenant_id}/members/{teacher_user_id}",
        ):
            return httpx.Response(204)
        if (method, path) == ("DELETE", f"/api/v1/auth/users/{teacher_username}"):
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {method} {path}")

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    body = asyncio.run(
        module._run_openmaic_smoke_probe(
            config,
            transport=httpx.MockTransport(handler),
        )
    )

    for secret in (ADMIN_TOKEN, teacher_password, TEACHER_SESSION, "PROVIDER_SECRET_SENTINEL"):
        assert secret.encode() not in body
    parsed = contract.parse_openmaic_smoke_report(
        body,
        candidate=_candidate(),
        release_run=release_run,
        expected_base_url=BASE_URL,
        expected_runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        forbidden_secret_values=tuple(
            secret.encode() for secret in (ADMIN_TOKEN, teacher_password, TEACHER_SESSION)
        ),
        expected_plane="dedicated",
    )
    assert parsed["fixture"]["tenantId"] == tenant_id
    assert parsed["binding"] == {
        "routeId": "dedicated-tenant-smoke",
        "routeTenantId": tenant_id,
        "routeOwnerKey": tenant_id,
        "providerProfileId": "provider-tenant-smoke",
        "providerScope": "dedicated",
        "providerTenantId": tenant_id,
        "providerOwnerKey": tenant_id,
        "workerPoolRef": "generation-tenant-smoke",
        "queueRef": "openmaic.tenant-smoke",
        "attemptCount": 2,
        "sharedRouteAttemptCount": 0,
        "dedicatedRouteAttemptCount": 2,
        "selectedRouteAttemptCount": 1,
        "unavailableRouteAttemptCount": 1,
        "routeAttemptHistoryComplete": True,
    }
    assert contract.derive_openmaic_dedicated_plane_checks(parsed) == {
        "dedicatedGenerationPassed": True,
        "noSharedClientIssued": True,
    }
    request_pairs = [(request.method, request.url.path) for request in seen]
    assert ("POST", "/api/v1/tenants") not in request_pairs
    assert not any(
        fragment in request.url.path
        for request in seen
        for fragment in ("data-plane-routes", "provider-profiles", "provider-secrets")
    )
    assert [pair for pair in request_pairs if pair[0] == "DELETE"] == [
        (
            "DELETE",
            f"/api/v1/teaching/classes/{class_id}/enrollments/{teacher_user_id}",
        ),
        ("DELETE", f"/api/v1/tenants/{tenant_id}/members/{teacher_user_id}"),
        ("DELETE", f"/api/v1/auth/users/{teacher_username}"),
    ]


def test_shared_probe_compensates_created_identity_after_mid_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _probe_module()
    config = module.ProbeConfig(
        admin_token=module.SecretStr(ADMIN_TOKEN),
        base_url=BASE_URL,
        candidate=_candidate(),
        candidate_root=tmp_path,
        dedicated_tenant_id=None,
        plane="shared",
        release_run={
            "runId": "run-openmaic-shared-smoke",
            "environmentId": "environment-openmaic-shared-smoke",
        },
        runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        timeout_seconds=30,
    )
    monkeypatch.setattr(module.secrets, "token_bytes", lambda _size: b"\x01" * 16)
    material = module._fixture_material(config)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        seen.append((method, path))
        if (method, path) == ("POST", "/api/v1/tenants"):
            return httpx.Response(
                202,
                json={
                    "tenant_id": "tenant-shared-smoke",
                    "status": "active",
                    "job_id": "provision-shared-smoke",
                },
            )
        if (method, path) == (
            "GET",
            "/api/v1/tenants/tenant-shared-smoke/provisioning",
        ):
            return httpx.Response(
                200,
                json={
                    "tenant_id": "tenant-shared-smoke",
                    "status": "active",
                    "job_id": "provision-shared-smoke",
                    "job_status": "completed",
                    "attempt_count": 0,
                },
            )
        if (method, path) == ("PUT", "/api/v1/tenants/active"):
            return httpx.Response(
                200,
                headers={
                    "Set-Cookie": "dt_tenant=tenant-shared-smoke; Path=/; HttpOnly; SameSite=Lax"
                },
                json={"active_tenant_id": "tenant-shared-smoke"},
            )
        if (method, path) == ("GET", "/api/v1/auth/users"):
            return httpx.Response(200, json=[])
        if (method, path) == ("POST", "/api/v1/auth/users"):
            assert _request_json(request) == {
                "username": material.teacher_username,
                "password": material.teacher_password.get_secret_value(),
            }
            return httpx.Response(
                201,
                json={
                    "ok": True,
                    "user_id": "teacher-user-shared-smoke",
                    "username": material.teacher_username,
                    "role": "admin",
                    "is_admin": True,
                },
            )
        if (method, path) == (
            "DELETE",
            f"/api/v1/auth/users/{material.teacher_username}",
        ):
            assert _request_json(request) == {"expected_user_id": "teacher-user-shared-smoke"}
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {method} {path}")

    with pytest.raises(module.OpenMAICSmokeProbeError, match="teacher_create_invalid"):
        asyncio.run(
            module._run_openmaic_smoke_probe(
                config,
                transport=httpx.MockTransport(handler),
            )
        )

    assert seen == [
        ("POST", "/api/v1/tenants"),
        ("GET", "/api/v1/tenants/tenant-shared-smoke/provisioning"),
        ("PUT", "/api/v1/tenants/active"),
        ("GET", "/api/v1/auth/users"),
        ("POST", "/api/v1/auth/users"),
        ("DELETE", f"/api/v1/auth/users/{material.teacher_username}"),
    ]


@pytest.mark.parametrize("lost_stage", ("identity", "membership", "enrollment"))
def test_shared_probe_recovers_commit_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lost_stage: str,
) -> None:
    module = _probe_module()
    config = module.ProbeConfig(
        admin_token=module.SecretStr(ADMIN_TOKEN),
        base_url=BASE_URL,
        candidate=_candidate(),
        candidate_root=tmp_path,
        dedicated_tenant_id=None,
        plane="shared",
        release_run={
            "runId": "run-openmaic-shared-smoke",
            "environmentId": "environment-openmaic-shared-smoke",
        },
        runtime_attestation_sha256=RUNTIME_ATTESTATION_SHA256,
        timeout_seconds=30,
    )
    monkeypatch.setattr(module.secrets, "token_bytes", lambda _size: b"\x01" * 16)
    material = module._fixture_material(config)
    course_id = f"course-{material.resource_suffix}"
    class_id = f"class-{material.resource_suffix}"
    seen: list[tuple[str, str]] = []
    identity_committed = False

    def response_lost(request: httpx.Request) -> None:
        raise httpx.ReadError("response lost after commit", request=request)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal identity_committed
        method = request.method
        path = request.url.path
        seen.append((method, path))
        if (method, path) == ("POST", "/api/v1/tenants"):
            return httpx.Response(
                202,
                json={
                    "tenant_id": "tenant-shared-smoke",
                    "status": "active",
                    "job_id": "provision-shared-smoke",
                },
            )
        if (method, path) == (
            "GET",
            "/api/v1/tenants/tenant-shared-smoke/provisioning",
        ):
            return httpx.Response(
                200,
                json={
                    "tenant_id": "tenant-shared-smoke",
                    "status": "active",
                    "job_id": "provision-shared-smoke",
                    "job_status": "completed",
                    "attempt_count": 0,
                },
            )
        if (method, path) == ("PUT", "/api/v1/tenants/active"):
            cookie = "dt_tenant=tenant-shared-smoke; Path=/; HttpOnly; SameSite=Lax"
            return httpx.Response(
                200,
                headers={"Set-Cookie": cookie},
                json={"active_tenant_id": "tenant-shared-smoke"},
            )
        if (method, path) == ("GET", "/api/v1/auth/users"):
            users = (
                [
                    {
                        "id": "teacher-user-shared-smoke",
                        "username": material.teacher_username,
                        "role": "user",
                        "created_at": "2026-08-29T00:00:00Z",
                        "disabled": False,
                        "avatar": "",
                    }
                ]
                if identity_committed
                else []
            )
            return httpx.Response(200, json=users)
        if (method, path) == ("POST", "/api/v1/auth/users"):
            identity_committed = True
            if lost_stage == "identity":
                response_lost(request)
            return httpx.Response(
                201,
                json={
                    "ok": True,
                    "user_id": "teacher-user-shared-smoke",
                    "username": material.teacher_username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if (method, path) == (
            "POST",
            "/api/v1/tenants/tenant-shared-smoke/members",
        ):
            if lost_stage == "membership":
                response_lost(request)
            return httpx.Response(
                200,
                json={
                    "tenant_id": "tenant-shared-smoke",
                    "user_id": "teacher-user-shared-smoke",
                    "roles": ["teacher"],
                    "grants": [
                        {
                            "role": "teacher",
                            "scope_type": "tenant",
                            "scope_id": "tenant-shared-smoke",
                        }
                    ],
                },
            )
        if (method, path) == ("POST", "/api/v1/auth/login"):
            return httpx.Response(
                200,
                headers={
                    "Set-Cookie": f"dt_token={TEACHER_SESSION}; Path=/; HttpOnly; SameSite=Lax"
                },
                json={
                    "ok": True,
                    "user_id": "teacher-user-shared-smoke",
                    "username": material.teacher_username,
                    "role": "user",
                    "is_admin": False,
                },
            )
        if (method, path) == ("POST", "/api/v1/teaching/courses"):
            return httpx.Response(
                201,
                json={
                    "id": course_id,
                    "title": "OpenMAIC shared-plane acceptance",
                    "status": "active",
                },
            )
        if (method, path) == (
            "POST",
            f"/api/v1/teaching/courses/{course_id}/classes",
        ):
            return httpx.Response(
                201,
                json={
                    "id": class_id,
                    "courseId": course_id,
                    "name": "OpenMAIC shared-plane acceptance",
                    "status": "active",
                },
            )
        if (method, path) == (
            "POST",
            f"/api/v1/teaching/classes/{class_id}/enrollments",
        ):
            assert lost_stage == "enrollment"
            response_lost(request)
        if (method, path) == (
            "DELETE",
            f"/api/v1/teaching/classes/{class_id}/enrollments/teacher-user-shared-smoke",
        ):
            return httpx.Response(204)
        if (method, path) == (
            "DELETE",
            "/api/v1/tenants/tenant-shared-smoke/members/teacher-user-shared-smoke",
        ):
            return httpx.Response(204)
        if (method, path) == (
            "DELETE",
            f"/api/v1/auth/users/{material.teacher_username}",
        ):
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {method} {path}")

    with pytest.raises(module.OpenMAICSmokeProbeError, match="candidate_request_failed"):
        asyncio.run(
            module._run_openmaic_smoke_probe(
                config,
                transport=httpx.MockTransport(handler),
            )
        )

    expected_cleanup = {
        "identity": [
            ("GET", "/api/v1/auth/users"),
            ("DELETE", f"/api/v1/auth/users/{material.teacher_username}"),
        ],
        "membership": [
            (
                "DELETE",
                "/api/v1/tenants/tenant-shared-smoke/members/teacher-user-shared-smoke",
            ),
            ("DELETE", f"/api/v1/auth/users/{material.teacher_username}"),
        ],
        "enrollment": [
            (
                "DELETE",
                f"/api/v1/teaching/classes/{class_id}/enrollments/teacher-user-shared-smoke",
            ),
            (
                "DELETE",
                "/api/v1/tenants/tenant-shared-smoke/members/teacher-user-shared-smoke",
            ),
            ("DELETE", f"/api/v1/auth/users/{material.teacher_username}"),
        ],
    }[lost_stage]
    assert seen[-len(expected_cleanup) :] == expected_cleanup


@pytest.mark.parametrize("failed_cleanup", ("enrollment", "membership"))
def test_fixture_cleanup_stops_before_deleting_parent_on_child_failure(
    failed_cleanup: str,
) -> None:
    module = _probe_module()
    state = module._FixtureCleanupState(
        tenant_id="tenant-shared-smoke",
        teacher_username="openmaic-shared-teacher",
    )
    state.identity_attempted = True
    state.teacher_user_id = "teacher-user-shared-smoke"
    state.membership_attempted = True
    state.class_id = "class-shared-smoke"
    state.enrollment_attempted = True
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call = (request.method, request.url.path)
        seen.append(call)
        if "/enrollments/" in request.url.path:
            return httpx.Response(500 if failed_cleanup == "enrollment" else 204)
        if "/members/" in request.url.path:
            return httpx.Response(409)
        raise AssertionError(f"parent cleanup must not run after {failed_cleanup} failure: {call}")

    async def exercise() -> None:
        async with module._OpenMAICSmokeApi(
            base_url=BASE_URL,
            admin_token=ADMIN_TOKEN,
            timeout_seconds=30,
            transport=httpx.MockTransport(handler),
        ) as api:
            with pytest.raises(module.OpenMAICSmokeProbeError, match="fixture_cleanup_failed"):
                await api.cleanup_fixture(state)

    asyncio.run(exercise())
    enrollment_delete = (
        "DELETE",
        "/api/v1/teaching/classes/class-shared-smoke/enrollments/teacher-user-shared-smoke",
    )
    membership_delete = (
        "DELETE",
        "/api/v1/tenants/tenant-shared-smoke/members/teacher-user-shared-smoke",
    )
    expected = (
        [enrollment_delete, enrollment_delete]
        if failed_cleanup == "enrollment"
        else [enrollment_delete, membership_delete, membership_delete]
    )
    assert seen == expected


def test_fixture_cleanup_accepts_only_exact_absent_tombstones() -> None:
    module = _probe_module()
    state = module._FixtureCleanupState(
        tenant_id="tenant-shared-smoke",
        teacher_username="openmaic-shared-teacher",
    )
    state.identity_attempted = True
    state.teacher_user_id = "teacher-user-shared-smoke"
    state.membership_attempted = True
    state.class_id = "class-shared-smoke"
    state.enrollment_attempted = True
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call = (request.method, request.url.path)
        seen.append(call)
        if "/enrollments/" in request.url.path:
            return httpx.Response(404, json={"detail": "enrollment not found"})
        if "/members/" in request.url.path:
            return httpx.Response(404, json={"detail": "Tenant membership not found"})
        if call == ("DELETE", "/api/v1/auth/users/openmaic-shared-teacher"):
            return httpx.Response(404, json={"detail": "User not found"})
        if call == ("GET", "/api/v1/auth/users"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected cleanup request: {call}")

    async def exercise() -> None:
        async with module._OpenMAICSmokeApi(
            base_url=BASE_URL,
            admin_token=ADMIN_TOKEN,
            timeout_seconds=30,
            transport=httpx.MockTransport(handler),
        ) as api:
            await api.cleanup_fixture(state)

    asyncio.run(exercise())
    assert seen == [
        (
            "DELETE",
            "/api/v1/teaching/classes/class-shared-smoke/enrollments/teacher-user-shared-smoke",
        ),
        (
            "DELETE",
            "/api/v1/tenants/tenant-shared-smoke/members/teacher-user-shared-smoke",
        ),
        ("DELETE", "/api/v1/auth/users/openmaic-shared-teacher"),
        ("GET", "/api/v1/auth/users"),
    ]
