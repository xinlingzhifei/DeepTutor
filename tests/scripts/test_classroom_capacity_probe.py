from __future__ import annotations

import asyncio
import hashlib
from http.cookies import SimpleCookie
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts" / "classroom_capacity_probe.py"
    spec = importlib.util.spec_from_file_location("classroom_capacity_probe_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate() -> dict[str, object]:
    return {
        "sourceRepository": "https://github.com/xinlingzhifei/yFeiSTAI.git",
        "sourceHead": "a" * 40,
        "releaseTag": f"yfeistai-first-release-20260820-{'a' * 8}",
        "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
        "imageDigests": {
            "deeptutor": f"sha256:{'1' * 64}",
            "openmaic": f"sha256:{'2' * 64}",
            "openmaic_render": f"sha256:{'3' * 64}",
        },
    }


def _raw_idempotency_observation(
    *,
    tenant_id: str,
    session_id: str,
    classroom_version_id: str,
    knowledge_point_id: str,
) -> dict[str, object]:
    event_binding = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    event_ids = [
        f"session-{event_binding}-started",
        f"session-{event_binding}-quiz",
        f"session-{event_binding}-completed",
    ]
    request_envelope = {
        "events": [
            {
                "schema_version": "1.0",
                "event_id": event_ids[0],
                "event_type": "classroom.started",
                "occurred_at": "2026-08-27T00:00:00Z",
            },
            {
                "schema_version": "1.0",
                "event_id": event_ids[1],
                "event_type": "quiz.graded",
                "occurred_at": "2026-08-27T00:00:00Z",
                "scene_id": "scene-00",
                "knowledge_point_id": knowledge_point_id,
                "assessment_id": "scene-00",
                "question_id": "question-00",
                "answer": ["answer-a"],
            },
            {
                "schema_version": "1.0",
                "event_id": event_ids[2],
                "event_type": "classroom.completed",
                "occurred_at": "2026-08-27T00:00:00Z",
            },
        ]
    }
    rows = [
        {"eventId": event_id, "seq": sequence}
        for sequence, event_id in enumerate(event_ids, start=1)
    ]
    return {
        "tenantId": tenant_id,
        "sessionId": session_id,
        "classroomVersionId": classroom_version_id,
        "knowledgePointId": knowledge_point_id,
        "eventIds": event_ids,
        "requestEnvelope": request_envelope,
        "requestSha256": hashlib.sha256(
            json.dumps(
                request_envelope,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "firstTicketSha256": "4" * 64,
        "freshTicketSha256": "5" * 64,
        "firstResponse": {
            "statusCode": 202,
            "accepted": rows,
            "duplicate": [],
            "quarantined": [],
        },
        "ticketReplay": {
            "statusCode": 409,
            "detail": "Classroom ticket already used",
        },
        "freshResponse": {
            "statusCode": 202,
            "accepted": [],
            "duplicate": list(rows),
            "quarantined": [],
        },
    }


def _environment(root: Path, *, token: str = "secret-platform-admin-token") -> dict[str, str]:
    return {
        "YFEISTAI_LIVE_FIXTURE_TOKEN": token,
        "YFEISTAI_CANDIDATE_ROOT": str(root),
        "YFEISTAI_RELEASE_RUN_ID": "run-20260827",
        "YFEISTAI_ENVIRONMENT_ID": "acceptance-a",
        "YFEISTAI_CAPACITY_TIMEOUT_SECONDS": "900",
        "WEB_BASE_URL": "https://classroom.example.test",
    }


def test_load_config_binds_exact_worktree_and_candidate_without_serializing_token(
    tmp_path: Path,
) -> None:
    module = _module()
    root = tmp_path.resolve()
    seen: list[Path] = []

    def load_candidate(candidate_root: Path) -> dict[str, object]:
        seen.append(candidate_root)
        return _candidate()

    config = module._load_config(
        _environment(root),
        cwd=root,
        candidate_loader=load_candidate,
    )

    assert seen == [root]
    assert config.candidate_root == root
    assert config.candidate == _candidate()
    assert config.release_run == {
        "runId": "run-20260827",
        "environmentId": "acceptance-a",
    }
    assert config.base_url == "https://classroom.example.test"
    assert config.timeout_seconds == 900
    assert config.admin_token.get_secret_value() == "secret-platform-admin-token"
    assert "secret-platform-admin-token" not in repr(config)


def test_load_config_rejects_candidate_root_that_is_not_current_worktree(tmp_path: Path) -> None:
    module = _module()
    cwd = (tmp_path / "current").resolve()
    other = (tmp_path / "other").resolve()
    cwd.mkdir()
    other.mkdir()

    with pytest.raises(module.CapacityProbeError, match="candidate_root_invalid"):
        module._load_config(
            _environment(other),
            cwd=cwd,
            candidate_loader=lambda _root: _candidate(),
        )


@pytest.mark.parametrize(
    ("base_url", "valid"),
    [
        ("https://classroom.example.test", True),
        ("http://localhost:8001", True),
        ("http://127.0.0.42:8001", True),
        ("http://[::1]:8001", True),
        ("http://classroom.example.test", False),
        ("http://192.0.2.10:8001", False),
    ],
)
def test_base_url_requires_https_except_for_explicit_loopback(
    base_url: str,
    valid: bool,
) -> None:
    assert _module()._valid_base_url(base_url) is valid


def test_cli_accepts_only_the_fixed_first_release_profile() -> None:
    module = _module()

    assert module._parse_args(["--profile", "first-release"]).profile == "first-release"
    with pytest.raises(SystemExit):
        module._parse_args(["--profile", "custom"])
    with pytest.raises(SystemExit):
        module._parse_args(["--profile", "first-release", "--token", "secret"])


def test_tenant_admin_request_binds_authorization_header_and_matching_cookie() -> None:
    module = _module()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async def exercise() -> None:
        async with module.CapacityApi(
            "https://classroom.example.test",
            "secret-platform-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            result = await api.tenant_admin_json(
                "POST",
                "/api/v1/tenants/tenant-a/members",
                tenant_id="tenant-a",
                json_body={"user_id": "student-a", "role": "student"},
            )
        assert result == {"ok": True}

    asyncio.run(exercise())

    assert len(seen) == 1
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer secret-platform-admin-token"
    assert request.headers["X-Tenant-ID"] == "tenant-a"
    assert request.headers["Cookie"] == "dt_tenant=tenant-a"
    assert request.url == "https://classroom.example.test/api/v1/tenants/tenant-a/members"


def test_capacity_api_allows_all_200_session_requests_to_be_in_flight() -> None:
    module = _module()

    async def exercise() -> None:
        async with module.CapacityApi(
            "https://classroom.example.test",
            "secret-platform-admin-token",
        ) as api:
            pool = api._identity_client._transport._pool
            assert pool._max_connections >= module.CAPACITY_PROFILE["executedConcurrentSessions"]

    asyncio.run(exercise())


def test_login_identity_requests_bind_student_cookies_and_tenant_without_platform_admin_authorization() -> (
    None
):
    module = _module()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "dt_token=student-jwt-a; Path=/; HttpOnly; SameSite=Lax"},
                json={
                    "ok": True,
                    "user_id": "student-a",
                    "username": "student-a",
                    "role": "user",
                    "is_admin": False,
                },
            )
        return httpx.Response(201, json={"id": "classroom-a"})

    async def exercise() -> None:
        async with module.CapacityApi(
            "https://classroom.example.test",
            "secret-platform-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            identity = await api.login_identity("student-a", "student-password-a")
            assert "student-jwt-a" not in repr(identity)
            result = await api.tenant_identity_json(
                "POST",
                "/api/v1/student/classrooms",
                identity=identity,
                tenant_id="tenant-a",
                json_body={
                    "courseId": "course-a",
                    "classId": "class-a",
                    "mode": "micro",
                },
                expected_statuses=frozenset({201}),
            )
        assert result == {"id": "classroom-a"}

    asyncio.run(exercise())

    assert len(seen) == 2
    login_request, tenant_request = seen
    assert login_request.url == "https://classroom.example.test/api/v1/auth/login"
    assert json.loads(login_request.content) == {
        "username": "student-a",
        "password": "student-password-a",
    }
    assert "Authorization" not in login_request.headers

    assert tenant_request.url == "https://classroom.example.test/api/v1/student/classrooms"
    assert tenant_request.headers["X-Tenant-ID"] == "tenant-a"
    assert "Authorization" not in tenant_request.headers
    cookies = SimpleCookie()
    cookies.load(tenant_request.headers["Cookie"])
    assert {name: morsel.value for name, morsel in cookies.items()} == {
        "dt_token": "student-jwt-a",
        "dt_tenant": "tenant-a",
    }


def test_prepare_generation_prerequisites_uses_exact_admin_endpoints_and_idempotency_keys() -> None:
    module = _module()
    admin_token = "secret-platform-admin-token"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/teaching/generation-quota-grants":
            return httpx.Response(
                200,
                json={
                    "grantId": "quota-a",
                    "tenantId": "tenant-a",
                    "units": 200,
                    "balance": 200,
                    "created": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "assessmentId": "safety-a",
                "tenantId": "tenant-a",
                "courseId": "course-a",
                "classId": "class-a",
                "mode": "micro",
                "contentMode": "open_creation",
                "webSearchRequested": False,
                "generallySafe": True,
                "minorSafe": True,
                "restrictedTopic": False,
                "reviewedBy": "platform-admin-a",
                "reviewedAt": "2026-08-27T08:00:00Z",
                "assessmentVersion": 1,
                "expiresAt": "2026-08-27T10:00:00Z",
                "created": True,
            },
        )

    async def exercise() -> None:
        async with module.CapacityApi(
            "https://classroom.example.test",
            admin_token,
            transport=httpx.MockTransport(handler),
        ) as api:
            await module._prepare_generation_prerequisites(
                api,
                tenant_id="tenant-a",
                course_id="course-a",
                class_id="class-a",
                run_key="capacity-run-a",
            )

    asyncio.run(exercise())

    assert len(seen) == 2
    quota_request, safety_request = seen
    assert quota_request.url == (
        "https://classroom.example.test/api/v1/teaching/generation-quota-grants"
    )
    assert quota_request.headers["Idempotency-Key"] == "capacity-run-a-tenant-a"
    assert json.loads(quota_request.content) == {"units": 200}

    assert safety_request.url == (
        "https://classroom.example.test/api/v1/teaching/courses/course-a/classes/"
        "class-a/student-safety-assessments"
    )
    assert safety_request.headers["Idempotency-Key"] == ("capacity-run-a-safety-tenant-a")
    assert json.loads(safety_request.content) == {
        "mode": "micro",
        "contentMode": "open_creation",
        "webSearchRequested": False,
        "generallySafe": True,
        "minorSafe": True,
        "restrictedTopic": False,
        "validForSeconds": 7200,
    }

    for request in seen:
        assert request.headers["Authorization"] == f"Bearer {admin_token}"
        assert request.headers["X-Tenant-ID"] == "tenant-a"
        assert request.headers["Cookie"] == "dt_tenant=tenant-a"
        non_authorization_headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() != "authorization"
        }
        assert admin_token not in str(request.url)
        assert admin_token.encode() not in request.content
        assert admin_token not in repr(non_authorization_headers)


def test_tenant_provisioning_accepts_zero_based_success_attempt_count() -> None:
    module = _module()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/tenants/tenant-a/provisioning"
        assert request.content == b""
        return httpx.Response(
            200,
            json={
                "tenant_id": "tenant-a",
                "status": "active",
                "job_id": "job-a",
                "job_status": "completed",
                "attempt_count": 0,
            },
        )

    async def exercise() -> None:
        async with module.CapacityApi(
            "https://classroom.example.test",
            "secret-platform-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            await module._wait_for_tenant_active(
                api,
                tenant_id="tenant-a",
                job_id="job-a",
                end_time=module.time.monotonic() + 1,
            )

    asyncio.run(exercise())


def test_bounded_map_cancels_and_awaits_siblings_before_raising() -> None:
    module = _module()
    sibling_started = asyncio.Event()
    sibling_finished = asyncio.Event()

    async def operation(value: str) -> None:
        if value == "failure":
            await sibling_started.wait()
            raise RuntimeError("primary")
        try:
            sibling_started.set()
            await asyncio.Event().wait()
        finally:
            sibling_finished.set()

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="primary"):
            await module._bounded_map(["sibling", "failure"], operation, limit=2)
        assert sibling_finished.is_set()

    asyncio.run(exercise())


def test_cleanup_operations_are_bounded_best_effort_and_await_every_started_task() -> None:
    module = _module()
    attempted: list[int] = []
    finalized: list[int] = []
    active = 0
    peak = 0

    def operation(value: int):
        async def run() -> None:
            nonlocal active, peak
            attempted.append(value)
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                if value == 2:
                    raise RuntimeError("cleanup failure")
            finally:
                active -= 1
                finalized.append(value)

        return run

    async def exercise() -> bool:
        return await module._run_cleanup_operations(
            [operation(value) for value in range(5)],
            limit=2,
            timeout_seconds=1,
        )

    assert asyncio.run(exercise()) is True
    assert sorted(attempted) == sorted(finalized) == list(range(5))
    assert peak == 2


def test_run_capacity_probe_reports_primary_and_cleanup_failure_without_masking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-platform-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=tmp_path,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        timeout_seconds=60,
    )
    material = module.IdentityMaterial(
        "capacity-run-a",
        "student-a",
        module.SecretStr("student-password-a"),
        "report-a",
        module.SecretStr("report-password-a"),
    )

    class Api:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def login_identity(self, username, _password):
            return module.IdentityCredential(
                username,
                "student-user-a" if username == "student-a" else "report-user-a",
                module.SecretStr(f"{username}-token"),
            )

    async def create_identity(_api, *, username, **_kwargs):
        return "student-user-a" if username == "student-a" else "report-user-a"

    async def execute(_config, _api, *, sessions, **_kwargs):
        sessions.append(module.SessionFixture(0, "tenant-a", "asset-a", "version-a", "session-a"))
        raise module.CapacityProbeError("primary_failure")

    async def cleanup_session(*_args, **_kwargs):
        raise RuntimeError("cleanup failure")

    async def delete_identity(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module, "CapacityApi", lambda *_args, **_kwargs: Api())
    monkeypatch.setattr(module, "_identity_material", lambda _config: material)
    monkeypatch.setattr(module, "_create_identity", create_identity)
    monkeypatch.setattr(module, "_execute_capacity_probe", execute)
    monkeypatch.setattr(module, "_cleanup_session", cleanup_session)
    monkeypatch.setattr(module, "_delete_identity", delete_identity)

    with pytest.raises(module.CapacityProbeError, match="capacity_probe_and_cleanup_failed") as exc:
        asyncio.run(module._run_capacity_probe(config))

    assert isinstance(exc.value.__cause__, module.CapacityProbeError)
    assert str(exc.value.__cause__) == "primary_failure"


def test_run_capacity_probe_cleans_resources_before_deleting_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-platform-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=tmp_path,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        timeout_seconds=60,
    )
    material = module.IdentityMaterial(
        "capacity-run-a",
        "student-a",
        module.SecretStr("student-password-a"),
        "report-a",
        module.SecretStr("report-password-a"),
    )
    events: list[str] = []

    class Api:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def login_identity(self, username, _password):
            return module.IdentityCredential(
                username,
                "student-user-a" if username == "student-a" else "report-user-a",
                module.SecretStr(f"{username}-token"),
            )

    async def create_identity(_api, *, username, **_kwargs):
        return "student-user-a" if username == "student-a" else "report-user-a"

    async def execute(_config, _api, *, sessions, **_kwargs):
        sessions.append(module.SessionFixture(0, "tenant-a", "asset-a", "version-a", "session-a"))
        raise module.CapacityProbeError("primary_failure")

    async def cleanup_session(*_args, **_kwargs):
        events.append("resource:start")
        await asyncio.sleep(0)
        events.append("resource:end")

    async def delete_identity(_api, username):
        events.append(f"identity:{username}")

    monkeypatch.setattr(module, "CapacityApi", lambda *_args, **_kwargs: Api())
    monkeypatch.setattr(module, "_identity_material", lambda _config: material)
    monkeypatch.setattr(module, "_create_identity", create_identity)
    monkeypatch.setattr(module, "_execute_capacity_probe", execute)
    monkeypatch.setattr(module, "_cleanup_session", cleanup_session)
    monkeypatch.setattr(module, "_delete_identity", delete_identity)

    with pytest.raises(module.CapacityProbeError, match="primary_failure"):
        asyncio.run(module._run_capacity_probe(config))

    assert events[:2] == ["resource:start", "resource:end"]
    assert events[2:] == ["identity:report-a", "identity:student-a"]


def test_exercise_session_ingests_exact_quiz_and_waits_for_report_increment() -> None:
    module = _module()
    calls: list[tuple[str, str, object, object]] = []
    student = module.IdentityCredential(
        "student-a",
        "student-user-a",
        module.SecretStr("student-token-a"),
    )
    reporter = module.IdentityCredential(
        "report-a",
        "report-user-a",
        module.SecretStr("report-token-a"),
    )
    session = module.SessionFixture(
        7,
        "tenant-a",
        "asset-a",
        "version-a",
        "session-a",
    )
    material = module.IdentityMaterial(
        "capacity-run-a",
        "student-a",
        module.SecretStr("student-password-a"),
        "report-a",
        module.SecretStr("report-password-a"),
    )
    quiz = module.QuizEvidence("scene-a", "question-a", "kp-a", ["answer-a"])

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
        ):
            calls.append((method, path, json_body, headers))
            assert tenant_id == "tenant-a"
            if path.endswith("/event-ticket"):
                assert identity == student
                assert expected_statuses == frozenset({200})
                return {"ticket": "ticket-a", "expires_in": 300}
            if path.endswith("/events"):
                assert identity == student
                assert headers == {"X-Classroom-Ticket": "ticket-a"}
                assert expected_statuses == frozenset({202})
                events = json_body["events"]
                assert [event["event_type"] for event in events] == [
                    "classroom.started",
                    "quiz.graded",
                    "classroom.completed",
                ]
                assert events[1]["assessment_id"] == events[1]["scene_id"] == "scene-a"
                assert events[1]["answer"] == ["answer-a"]
                return {
                    "accepted": [
                        {"event_id": event["event_id"], "seq": index + 1}
                        for index, event in enumerate(events)
                    ],
                    "duplicate": [],
                    "quarantined": [],
                }
            if "/teaching-reports/classrooms/" in path:
                assert identity == reporter
                return {
                    "classroomVersionId": "version-a",
                    "sessionCount": 1,
                    "completedCount": 1,
                    "completionRate": 1.0,
                    "completedSceneCount": 0,
                    "validQuizCount": 1,
                    "correctQuizCount": 1,
                    "hintCount": 0,
                    "pblMilestoneCount": 0,
                    "mastery": [{"knowledgePointId": "kp-a", "level": 0.8, "evidenceCount": 1}],
                    "projectionLagSeconds": 0.1,
                }
            if path.endswith("/complete"):
                assert identity == student
                return {
                    "id": "session-a",
                    "tenant_id": "tenant-a",
                    "user_id": "student-user-a",
                    "classroom_version_id": "version-a",
                    "assignment_id": None,
                    "student_asset_id": "asset-a",
                    "status": "completed",
                    "last_cursor": {"last_event_seq": 3},
                    "started_at": "2026-08-27T08:00:00Z",
                    "completed_at": "2026-08-27T08:01:00Z",
                }
            raise AssertionError(path)

    async def exercise():
        return await module._exercise_session(
            Api(),
            material=material,
            session=session,
            quiz=quiz,
            student=student,
            report_identity=reporter,
            baseline=module.ReportSnapshot(0, 0, 0),
            end_time=module.time.monotonic() + 1,
        )

    current, event_sample, projection_sample, completion = asyncio.run(exercise())

    assert current == module.ReportSnapshot(1, 1, 1)
    assert event_sample["subjectId"] == projection_sample["subjectId"] == "session-a"
    assert event_sample["success"] is projection_sample["success"] is True
    assert completion == {
        "sessionId": "session-a",
        "tenantId": "tenant-a",
        "status": "completed",
    }
    assert [path for _method, path, _body, _headers in calls] == [
        "/api/v1/classroom-sessions/session-a/event-ticket",
        "/api/v1/classroom-sessions/session-a/events",
        "/api/v1/teaching-reports/classrooms/version-a",
        "/api/v1/classroom-sessions/session-a/complete",
    ]


def test_event_ingestion_requires_positive_strictly_increasing_accepted_sequences() -> None:
    module = _module()
    event_ids = ("event-a", "event-b", "event-c")
    valid = {
        "accepted": [
            {"event_id": event_id, "seq": sequence}
            for sequence, event_id in enumerate(event_ids, start=1)
        ],
        "duplicate": [],
        "quarantined": [],
    }

    assert module._validate_event_ingestion(valid, event_ids) == (1, 2, 3)

    for sequences in ((0, 1, 2), (1, 3, 2), (1, 1, 2)):
        invalid = {
            **valid,
            "accepted": [
                {"event_id": event_id, "seq": sequence}
                for event_id, sequence in zip(event_ids, sequences, strict=True)
            ],
        }
        with pytest.raises(module.CapacityProbeError, match="learning_event_ingestion_invalid"):
            module._validate_event_ingestion(invalid, event_ids)


def test_sequence_zero_event_ingestion_proves_ticket_and_duplicate_idempotency() -> None:
    module = _module()
    student = module.IdentityCredential(
        "student-a",
        "student-user-a",
        module.SecretStr("student-token-a"),
    )
    session = module.SessionFixture(0, "tenant-a", "asset-a", "version-a", "session-a")
    material = module.IdentityMaterial(
        "capacity-run-a",
        "student-a",
        module.SecretStr("student-password-a"),
        "report-a",
        module.SecretStr("report-password-a"),
    )
    quiz = module.QuizEvidence("scene-a", "question-a", "kp-a", ["answer-a"])
    calls: list[tuple[str, str, object, object, frozenset[int]]] = []

    class Api:
        async def tenant_identity_json(
            self,
            method,
            path,
            *,
            json_body=None,
            headers=None,
            expected_statuses=frozenset({200, 201, 202}),
            **_kwargs,
        ):
            calls.append((method, path, json_body, headers, expected_statuses))
            if path.endswith("/event-ticket"):
                ticket = "ticket-first" if len(calls) == 1 else "ticket-fresh"
                return {"ticket": ticket, "expires_in": 300}
            events = json_body["events"]
            rows = [
                {"event_id": event["event_id"], "seq": index}
                for index, event in enumerate(events, start=11)
            ]
            if expected_statuses == frozenset({409}):
                return {"detail": "Classroom ticket already used"}
            if headers == {"X-Classroom-Ticket": "ticket-fresh"}:
                return {"accepted": [], "duplicate": rows, "quarantined": []}
            return {"accepted": rows, "duplicate": [], "quarantined": []}

    async def exercise():
        gate = module._StartGate(1)
        return await module._ingest_session_events(
            Api(),
            material=material,
            session=session,
            quiz=quiz,
            student=student,
            start_gate=gate,
        )

    sample, observation = asyncio.run(exercise())
    event_binding = hashlib.sha256(b"session-a").hexdigest()
    expected_event_ids = [
        f"session-{event_binding}-started",
        f"session-{event_binding}-quiz",
        f"session-{event_binding}-completed",
    ]
    expected_rows = [
        {"eventId": event_id, "seq": sequence}
        for event_id, sequence in zip(expected_event_ids, (11, 12, 13), strict=True)
    ]

    assert sample["success"] is True
    assert observation == {
        "tenantId": "tenant-a",
        "sessionId": "session-a",
        "classroomVersionId": "version-a",
        "knowledgePointId": "kp-a",
        "eventIds": expected_event_ids,
        "requestEnvelope": calls[1][2],
        "requestSha256": hashlib.sha256(
            json.dumps(
                calls[1][2],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "firstTicketSha256": module.hashlib.sha256(b"ticket-first").hexdigest(),
        "freshTicketSha256": module.hashlib.sha256(b"ticket-fresh").hexdigest(),
        "firstResponse": {
            "statusCode": 202,
            "accepted": expected_rows,
            "duplicate": [],
            "quarantined": [],
        },
        "ticketReplay": {
            "statusCode": 409,
            "detail": "Classroom ticket already used",
        },
        "freshResponse": {
            "statusCode": 202,
            "accepted": [],
            "duplicate": expected_rows,
            "quarantined": [],
        },
    }
    assert [entry[4] for entry in calls] == [
        frozenset({200}),
        frozenset({202}),
        frozenset({409}),
        frozenset({200}),
        frozenset({202}),
    ]
    assert "ticket-first" not in json.dumps(observation)
    assert "ticket-fresh" not in json.dumps(observation)
    event_bodies = [
        body for _method, path, body, _headers, _statuses in calls if path.endswith("/events")
    ]
    assert len(event_bodies) == 3
    assert len({id(body) for body in event_bodies}) == 1


def test_report_reread_requires_expected_session_and_completion_counts() -> None:
    module = _module()
    reporter = module.IdentityCredential(
        "report-a",
        "report-user-a",
        module.SecretStr("report-token-a"),
    )
    session = module.SessionFixture(0, "tenant-a", "asset-a", "version-a", "session-a")

    class Api:
        async def tenant_identity_json(self, *_args, **_kwargs):
            return {
                "classroomVersionId": "version-a",
                "sessionCount": 4,
                "completedCount": 3,
                "completionRate": 0.75,
                "completedSceneCount": 0,
                "validQuizCount": 4,
                "correctQuizCount": 4,
                "hintCount": 0,
                "pblMilestoneCount": 0,
                "mastery": [{"knowledgePointId": "kp-a", "level": 0.8, "evidenceCount": 4}],
                "projectionLagSeconds": 0.1,
            }

    async def exercise() -> None:
        with pytest.raises(module.CapacityProbeError, match="teaching_report_invalid"):
            await module._load_report_snapshot(
                Api(),
                session=session,
                report_identity=reporter,
                knowledge_point_id="kp-a",
                expected_session_count=4,
                expected_completed_count=4,
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("mutation", ["job_phase", "job_progress", "asset_status", "owner", "mode"])
def test_ready_classroom_requires_terminal_job_and_bound_succeeded_asset(
    mutation: str,
) -> None:
    module = _module()
    student = module.IdentityCredential(
        "student-a",
        "student-user-a",
        module.SecretStr("student-token-a"),
    )
    generation = module.GenerationFixture(0, "tenant-a", "asset-a", "job-a")
    job = {
        "job_id": "job-a",
        "job_kind": "generation",
        "phase": "content",
        "status": "succeeded",
        "progress_percent": 100,
        "waiting_reason": None,
        "cancellable": False,
        "retryable": False,
        "outline": None,
        "error_category": None,
        "error_code": None,
        "retry_of_job_id": None,
        "export_format": None,
        "download_ready": False,
    }
    asset = {
        "assetId": "asset-a",
        "requestId": "request-a",
        "approvalId": None,
        "generationJobId": "job-a",
        "status": "succeeded",
        "courseId": "course-a",
        "classId": "class-a",
        "mode": "micro",
        "ownerId": "student-user-a",
        "revision": 2,
        "outline": None,
        "classroomVersionId": "version-a",
    }
    if mutation == "job_phase":
        job["phase"] = "outline"
    elif mutation == "job_progress":
        job["progress_percent"] = 101
    elif mutation == "asset_status":
        asset["status"] = "materializing"
    elif mutation == "owner":
        asset["ownerId"] = "other-user"
    else:
        asset["mode"] = "full"

    class Api:
        async def tenant_identity_json(self, _method, path, **_kwargs):
            return asset if "/student-classrooms/" in path else job

    async def exercise() -> None:
        with pytest.raises(module.CapacityProbeError, match="generation_status_invalid"):
            await module._wait_for_ready_classroom(
                Api(),
                generation=generation,
                student=student,
                end_time=module.time.monotonic() + 0.2,
            )

    asyncio.run(exercise())


def test_concurrent_session_workload_uses_one_200_way_barrier_and_rereads_every_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    material = module.IdentityMaterial(
        "capacity-run-a",
        "student-a",
        module.SecretStr("student-password-a"),
        "report-a",
        module.SecretStr("report-password-a"),
    )
    student = module.IdentityCredential(
        "student-a",
        "student-user-a",
        module.SecretStr("student-token-a"),
    )
    reporter = module.IdentityCredential(
        "report-a",
        "report-user-a",
        module.SecretStr("report-token-a"),
    )
    sessions = [
        module.SessionFixture(
            sequence,
            f"tenant-{sequence // 4:02d}",
            f"asset-{sequence // 4:02d}",
            f"version-{sequence // 4:02d}",
            f"session-{sequence:03d}",
        )
        for sequence in range(200)
    ]
    quizzes = {
        f"tenant-{sequence:02d}": module.QuizEvidence(
            f"scene-{sequence:02d}",
            f"question-{sequence:02d}",
            f"kp-{sequence:02d}",
            ["answer-a"],
        )
        for sequence in range(50)
    }
    active = 0
    peak = 0
    report_reads: dict[str, int] = {}
    completed_calls: list[str] = []
    verified_calls: list[str] = []

    async def ingest(_api, *, session, start_gate, **_kwargs):
        nonlocal active, peak
        await start_gate.wait()
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return (
            {
                "metric": "event_ingest",
                "tenantId": session.tenant_id,
                "subjectId": session.session_id,
                "sequence": session.sequence,
                "latencyMs": 1.0,
                "success": True,
            },
            _raw_idempotency_observation(
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                classroom_version_id=session.version_id,
                knowledge_point_id=quizzes[session.tenant_id].knowledge_point_id,
            )
            if session.sequence == 0
            else None,
        )

    async def load_report(_api, *, session, **_kwargs):
        reads = report_reads.get(session.tenant_id, 0)
        report_reads[session.tenant_id] = reads + 1
        value = 0 if reads == 0 else 4
        return module.ReportSnapshot(value, value, value)

    async def complete(_api, *, session, **_kwargs):
        completed_calls.append(session.session_id)
        return {
            "sessionId": session.session_id,
            "tenantId": session.tenant_id,
            "status": "completed",
        }

    async def verify_completed(_api, *, session, **_kwargs):
        verified_calls.append(session.session_id)

    monkeypatch.setattr(module, "_ingest_session_events", ingest)
    monkeypatch.setattr(module, "_load_report_snapshot", load_report)
    monkeypatch.setattr(module, "_complete_session", complete)
    monkeypatch.setattr(module, "_verify_completed_session", verify_completed)

    completed_sessions: set[str] = set()

    async def exercise():
        return await module._exercise_concurrent_sessions(
            object(),
            material=material,
            sessions=sessions,
            quizzes=quizzes,
            student=student,
            report_identity=reporter,
            completed_sessions=completed_sessions,
            end_time=module.time.monotonic() + 5,
        )

    event_samples, projection_samples, completions, observations, idempotency = asyncio.run(
        exercise()
    )

    assert peak == 200
    assert len(event_samples) == len(projection_samples) == len(completions) == 200
    assert len(observations) == 1
    assert len(observations[0]["active"]) == 200
    assert len(completed_calls) == len(verified_calls) == len(completed_sessions) == 200
    assert set(completed_calls) == set(verified_calls) == completed_sessions
    assert idempotency["quizProjection"] == {
        "expectedDelta": 4,
        "baseline": {
            "classroomVersionId": "version-00",
            "sessionCount": 4,
            "completedCount": 0,
            "knowledgePointId": "kp-00",
            "validQuizCount": 0,
            "correctQuizCount": 0,
            "evidenceCount": 0,
        },
        "visible": {
            "classroomVersionId": "version-00",
            "sessionCount": 4,
            "completedCount": 0,
            "knowledgePointId": "kp-00",
            "validQuizCount": 4,
            "correctQuizCount": 4,
            "evidenceCount": 4,
        },
        "reread": {
            "classroomVersionId": "version-00",
            "sessionCount": 4,
            "completedCount": 4,
            "knowledgePointId": "kp-00",
            "validQuizCount": 4,
            "correctQuizCount": 4,
            "evidenceCount": 4,
        },
    }


def test_execute_capacity_probe_builds_self_validating_fixed_50_52_200_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-platform-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=tmp_path,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        timeout_seconds=30,
    )
    material = module.IdentityMaterial(
        "capacity-run-a",
        "student-a",
        module.SecretStr("student-password-a"),
        "report-a",
        module.SecretStr("report-password-a"),
    )
    student = module.IdentityCredential(
        "student-a",
        "student-user-a",
        module.SecretStr("student-token-a"),
    )
    reporter = module.IdentityCredential(
        "report-a",
        "report-user-a",
        module.SecretStr("report-token-a"),
    )

    def resource(sequence: int, phase: str) -> dict[str, object]:
        return {
            "sequence": sequence,
            "phase": phase,
            "observedAt": f"2026-08-27T08:00:0{sequence}Z",
            "available": True,
            "totalRssBytes": 100 + sequence,
            "limitBytes": 1_000,
            "availableBytes": 900 - sequence,
            "limitSource": "cgroup",
            "usageRatio": (100 + sequence) / 1_000,
            "partial": False,
            "processes": [{"label": "backend", "count": 1, "rssBytes": 100 + sequence}],
        }

    async def create_tenant(_api, *, sequence, **_kwargs):
        return module.TenantFixture(
            sequence,
            f"tenant-{sequence:02d}",
            f"course-{sequence:02d}",
            f"class-{sequence:02d}",
        )

    async def capture_resource(_api, *, sequence, phase):
        return resource(sequence, phase)

    async def submit_generation(_api, *, sequence, fixture, student):
        del student
        generation = module.GenerationFixture(
            sequence,
            fixture.tenant_id,
            f"asset-{sequence:02d}",
            f"job-{sequence:02d}",
        )
        return generation, {
            "metric": "job_submission_visible",
            "tenantId": fixture.tenant_id,
            "subjectId": generation.job_id,
            "sequence": sequence,
            "latencyMs": 1.0,
            "success": True,
        }

    async def observe_scheduler(_api, *, job_tenants, finished, **_kwargs):
        await finished.wait()
        assert len(job_tenants) == 52
        ordered = sorted(job_tenants, key=lambda job_id: int(job_id.rsplit("-", 1)[1]))
        claims = [
            {"sequence": sequence, "jobId": job_id, "tenantId": job_tenants[job_id]}
            for sequence, job_id in enumerate(ordered)
        ]
        active_ids = ordered[:20]
        observations = [
            {
                "sequence": 0,
                "active": [
                    {"jobId": job_id, "tenantId": job_tenants[job_id]} for job_id in active_ids
                ],
            }
        ]
        return claims, observations, resource(1, "generation_saturated")

    async def wait_ready(_api, *, generation, **_kwargs):
        return module.ReadyClassroom(
            generation.sequence,
            generation.tenant_id,
            generation.asset_id,
            generation.job_id,
            f"version-{generation.sequence:02d}",
        )

    async def create_session(_api, *, sequence, classroom, student):
        del student
        session = module.SessionFixture(
            sequence,
            classroom.tenant_id,
            classroom.asset_id,
            classroom.version_id,
            f"session-{sequence:03d}",
        )
        return session, {
            "metric": "core_api",
            "tenantId": session.tenant_id,
            "subjectId": session.session_id,
            "sequence": sequence,
            "latencyMs": 1.0,
            "success": True,
        }

    async def verify_session(*_args, **_kwargs):
        return None

    async def load_quiz(*_args, **_kwargs):
        return module.QuizEvidence("scene-a", "question-a", "kp-a", ["answer-a"])

    report_reads: dict[str, int] = {}

    async def load_report(_api, *, session, **_kwargs):
        reads = report_reads.get(session.tenant_id, 0)
        report_reads[session.tenant_id] = reads + 1
        value = 0 if reads == 0 else 4
        return module.ReportSnapshot(value, value, value)

    async def ingest_session(_api, *, session, start_gate, **_kwargs):
        await start_gate.wait()
        return (
            {
                "metric": "event_ingest",
                "tenantId": session.tenant_id,
                "subjectId": session.session_id,
                "sequence": session.sequence,
                "latencyMs": 1.0,
                "success": True,
            },
            _raw_idempotency_observation(
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                classroom_version_id=session.version_id,
                knowledge_point_id="kp-a",
            )
            if session.sequence == 0
            else None,
        )

    async def complete_session(_api, *, session, **_kwargs):
        return {
            "sessionId": session.session_id,
            "tenantId": session.tenant_id,
            "status": "completed",
        }

    async def verify_completed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module, "_create_tenant_fixture", create_tenant)
    monkeypatch.setattr(module, "_capture_resource", capture_resource)
    monkeypatch.setattr(module, "_submit_generation", submit_generation)
    monkeypatch.setattr(module, "_observe_scheduler", observe_scheduler)
    monkeypatch.setattr(module, "_wait_for_ready_classroom", wait_ready)
    monkeypatch.setattr(module, "_create_session", create_session)
    monkeypatch.setattr(module, "_verify_active_session", verify_session)
    monkeypatch.setattr(module, "_load_quiz_evidence", load_quiz)
    monkeypatch.setattr(module, "_load_report_snapshot", load_report)
    monkeypatch.setattr(module, "_ingest_session_events", ingest_session)
    monkeypatch.setattr(module, "_complete_session", complete_session)
    monkeypatch.setattr(module, "_verify_completed_session", verify_completed)

    async def exercise() -> bytes:
        return await module._execute_capacity_probe(
            config,
            object(),
            material=material,
            student=student,
            report_identity=reporter,
            generations=[],
            ready_jobs=set(),
            sessions=[],
            completed_sessions=set(),
            end_time=module.time.monotonic() + 5,
        )

    body = asyncio.run(exercise())
    parsed = module.parse_capacity_profile_report(
        body,
        candidate=_candidate(),
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        expected_base_url="https://classroom.example.test",
    )
    summary = module.derive_capacity_profile_summary(parsed)

    assert len(parsed["rawSamples"]) == 652
    assert len(parsed["schedulerClaims"]) == 52
    assert len(parsed["sessionCompletions"]) == 200
    assert module.derive_learning_event_idempotency_checks(parsed) == {
        "duplicateCountedOnce": True,
        "ticketReplayRejected": True,
        "projectionVisible": True,
    }
    assert summary["checks"] == {
        "thresholdsPassed": True,
        "rawSamplesRecorded": True,
        "resourceObservationsRecorded": True,
        "resourceAccountingComplete": True,
        "resourceBoundaryStable": True,
    }


def test_scheduler_snapshot_requires_exact_capacity_and_bound_target_claims() -> None:
    module = _module()
    snapshot = {
        "schemaVersion": 1,
        "observedAt": "2026-08-27T08:00:00Z",
        "jobs": [
            {
                "jobId": "job-a",
                "tenantId": "tenant-a",
                "workerPoolRef": "shared-generation",
                "status": "claimed",
                "claimedAt": "2026-08-27T08:00:00Z",
            }
        ],
        "claimEvents": [
            {
                "cursor": 41,
                "jobId": "job-a",
                "tenantId": "tenant-a",
                "claimedAt": "2026-08-27T08:00:00Z",
            }
        ],
        "missingJobIds": [],
        "pools": [
            {
                "workerPoolRef": "shared-generation",
                "globalSlotCapacity": 20,
                "tenantSlotCapacities": [{"tenantId": "tenant-a", "capacity": 2}],
                "active": [{"jobId": "job-a", "tenantId": "tenant-a", "ordinal": 0}],
            }
        ],
    }

    parsed = module._parse_scheduler_snapshot(snapshot, {"job-a": "tenant-a"})

    assert parsed.active == (("job-a", "tenant-a"),)
    assert parsed.claim_events == ((41, "job-a", "tenant-a"),)
    assert parsed.observed_at == "2026-08-27T08:00:00Z"

    snapshot["pools"][0]["active"][0]["tenantId"] = "tenant-other"
    with pytest.raises(module.CapacityProbeError, match="scheduler_snapshot_invalid"):
        module._parse_scheduler_snapshot(snapshot, {"job-a": "tenant-a"})


def test_scheduler_snapshot_compares_fractional_timestamps_chronologically() -> None:
    module = _module()
    snapshot = {
        "schemaVersion": 1,
        "observedAt": "2026-08-27T08:00:00.100Z",
        "jobs": [
            {
                "jobId": "job-a",
                "tenantId": "tenant-a",
                "workerPoolRef": "shared-generation",
                "status": "claimed",
                "claimedAt": "2026-08-27T08:00:00Z",
            }
        ],
        "claimEvents": [
            {
                "cursor": 41,
                "jobId": "job-a",
                "tenantId": "tenant-a",
                "claimedAt": "2026-08-27T08:00:00.050Z",
            }
        ],
        "missingJobIds": [],
        "pools": [
            {
                "workerPoolRef": "shared-generation",
                "globalSlotCapacity": 20,
                "tenantSlotCapacities": [{"tenantId": "tenant-a", "capacity": 2}],
                "active": [{"jobId": "job-a", "tenantId": "tenant-a", "ordinal": 0}],
            }
        ],
    }

    assert module._parse_scheduler_snapshot(snapshot, {"job-a": "tenant-a"}).active == (
        ("job-a", "tenant-a"),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "queued_timestamp",
        "claimed_timestamp",
        "claim_replay_timestamp",
        "active_set",
        "duplicate_ordinal",
        "ordinal_out_of_range",
    ],
)
def test_scheduler_snapshot_replays_queue_claim_timestamps_and_slot_ordinals(
    mutation: str,
) -> None:
    module = _module()
    targets = {"job-a": "tenant-a", "job-b": "tenant-b"}
    snapshot = {
        "schemaVersion": 1,
        "observedAt": "2026-08-27T08:00:01Z",
        "jobs": [
            {
                "jobId": "job-a",
                "tenantId": "tenant-a",
                "workerPoolRef": "shared-generation",
                "status": "claimed",
                "claimedAt": "2026-08-27T08:00:00Z",
            },
            {
                "jobId": "job-b",
                "tenantId": "tenant-b",
                "workerPoolRef": "shared-generation",
                "status": "queued",
                "claimedAt": None,
            },
        ],
        "claimEvents": [
            {
                "cursor": 41,
                "jobId": "job-a",
                "tenantId": "tenant-a",
                "claimedAt": "2026-08-27T08:00:00Z",
            }
        ],
        "missingJobIds": [],
        "pools": [
            {
                "workerPoolRef": "shared-generation",
                "globalSlotCapacity": 20,
                "tenantSlotCapacities": [
                    {"tenantId": "tenant-a", "capacity": 2},
                    {"tenantId": "tenant-b", "capacity": 2},
                ],
                "active": [{"jobId": "job-a", "tenantId": "tenant-a", "ordinal": 0}],
            }
        ],
    }
    if mutation == "queued_timestamp":
        snapshot["jobs"][1]["claimedAt"] = "2026-08-27T08:00:00Z"
    elif mutation == "claimed_timestamp":
        snapshot["jobs"][0]["claimedAt"] = None
    elif mutation == "claim_replay_timestamp":
        snapshot["claimEvents"][0]["claimedAt"] = "2026-08-27T07:59:59Z"
    elif mutation == "active_set":
        snapshot["pools"][0]["active"] = []
    elif mutation == "duplicate_ordinal":
        snapshot["jobs"][1]["status"] = "claimed"
        snapshot["jobs"][1]["claimedAt"] = "2026-08-27T08:00:00Z"
        snapshot["claimEvents"].append(
            {
                "cursor": 42,
                "jobId": "job-b",
                "tenantId": "tenant-b",
                "claimedAt": "2026-08-27T08:00:00Z",
            }
        )
        snapshot["pools"][0]["active"].append(
            {"jobId": "job-b", "tenantId": "tenant-b", "ordinal": 0}
        )
    else:
        snapshot["pools"][0]["active"][0]["ordinal"] = 20

    with pytest.raises(module.CapacityProbeError, match="scheduler_snapshot_invalid"):
        module._parse_scheduler_snapshot(snapshot, targets)


def test_scheduler_observer_starts_before_all_jobs_and_deduplicates_unchanged_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    all_targets = {
        f"job-{index:02d}": ("tenant-00" if index in {0, 1, 51} else f"tenant-{index - 1:02d}")
        for index in range(52)
    }
    job_tenants = {job_id: all_targets[job_id] for job_id in sorted(all_targets)[:20]}
    finished = asyncio.Event()
    calls: list[list[str]] = []

    def snapshot(targets: dict[str, str], *, final: bool) -> dict[str, object]:
        ordered = sorted(targets)
        if final:
            active_ids = ordered[:20]
            jobs = [
                {
                    "jobId": job_id,
                    "tenantId": targets[job_id],
                    "workerPoolRef": "shared-generation",
                    "status": "claimed",
                    "claimedAt": "2026-08-27T08:00:00Z",
                }
                for job_id in active_ids
            ]
            missing = ordered[20:]
            active = [
                {"jobId": job_id, "tenantId": targets[job_id], "ordinal": ordinal}
                for ordinal, job_id in enumerate(active_ids)
            ]
        else:
            jobs = [
                {
                    "jobId": job_id,
                    "tenantId": targets[job_id],
                    "workerPoolRef": "shared-generation",
                    "status": "claimed",
                    "claimedAt": "2026-08-27T08:00:00Z",
                }
                for job_id in ordered
            ]
            missing = []
            active = [
                {"jobId": job_id, "tenantId": targets[job_id], "ordinal": ordinal}
                for ordinal, job_id in enumerate(ordered)
            ]
        claim_ids = sorted(all_targets) if final else ordered
        return {
            "schemaVersion": 1,
            "observedAt": "2026-08-27T08:00:01Z",
            "jobs": jobs,
            "claimEvents": [
                {
                    "cursor": index + 1,
                    "jobId": job_id,
                    "tenantId": all_targets[job_id],
                    "claimedAt": "2026-08-27T08:00:00Z",
                }
                for index, job_id in enumerate(claim_ids)
            ],
            "missingJobIds": missing,
            "pools": [
                {
                    "workerPoolRef": "shared-generation",
                    "globalSlotCapacity": 20,
                    "tenantSlotCapacities": [
                        {"tenantId": tenant_id, "capacity": 2}
                        for tenant_id in sorted(set(targets.values()))
                    ],
                    "active": active,
                }
            ],
        }

    class Api:
        async def admin_json(self, _method, _path, *, json_body, **_kwargs):
            targets = {job_id: job_tenants[job_id] for job_id in json_body["jobIds"]}
            calls.append(list(json_body["jobIds"]))
            final = len(calls) > 1
            if not final:
                job_tenants.update(all_targets)
            else:
                finished.set()
            return snapshot(targets, final=final)

    async def capture_resource(*_args, **_kwargs):
        return {"phase": "generation_saturated"}

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(module, "_capture_resource", capture_resource)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    claims, observations, resource = asyncio.run(
        module._observe_scheduler(
            Api(),
            job_tenants=job_tenants,
            finished=finished,
            end_time=module.time.monotonic() + 1,
        )
    )

    assert len(calls[0]) == 20
    assert len(claims) == 52
    assert len(observations) == 1
    assert len(observations[0]["active"]) == 20
    assert resource == {"phase": "generation_saturated"}


def test_scheduler_observer_bounds_high_churn_state_history_without_losing_peak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    job_tenants = {
        f"job-{index:02d}": ("tenant-00" if index in {0, 1, 51} else f"tenant-{index - 1:02d}")
        for index in range(52)
    }
    ordered = sorted(job_tenants)
    finished = asyncio.Event()
    calls = 0

    class Api:
        async def admin_json(self, _method, _path, *, json_body, **_kwargs):
            nonlocal calls
            assert set(json_body["jobIds"]) == set(ordered)
            offset = calls % 2
            active_ids = ordered[offset : 20 + offset]
            calls += 1
            if calls == 300:
                finished.set()
            return {
                "schemaVersion": 1,
                "observedAt": "2026-08-27T08:00:01Z",
                "jobs": [
                    {
                        "jobId": job_id,
                        "tenantId": job_tenants[job_id],
                        "workerPoolRef": "shared-generation",
                        "status": "claimed",
                        "claimedAt": "2026-08-27T08:00:00Z",
                    }
                    for job_id in active_ids
                ],
                "claimEvents": [
                    {
                        "cursor": index + 1,
                        "jobId": job_id,
                        "tenantId": job_tenants[job_id],
                        "claimedAt": "2026-08-27T08:00:00Z",
                    }
                    for index, job_id in enumerate(ordered)
                ],
                "missingJobIds": [job_id for job_id in ordered if job_id not in active_ids],
                "pools": [
                    {
                        "workerPoolRef": "shared-generation",
                        "globalSlotCapacity": 20,
                        "tenantSlotCapacities": [
                            {"tenantId": tenant_id, "capacity": 2}
                            for tenant_id in sorted(set(job_tenants.values()))
                        ],
                        "active": [
                            {
                                "jobId": job_id,
                                "tenantId": job_tenants[job_id],
                                "ordinal": ordinal,
                            }
                            for ordinal, job_id in enumerate(active_ids)
                        ],
                    }
                ],
            }

    async def capture_resource(*_args, **_kwargs):
        return {"phase": "generation_saturated"}

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(module, "_capture_resource", capture_resource)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    claims, observations, _resource = asyncio.run(
        module._observe_scheduler(
            Api(),
            job_tenants=job_tenants,
            finished=finished,
            end_time=module.time.monotonic() + 60,
        )
    )

    assert calls == 300
    assert len(claims) == 52
    assert len(observations) <= 256
    assert max(len(item["active"]) for item in observations) == 20


def test_resource_and_quiz_evidence_are_strictly_normalized() -> None:
    module = _module()
    resource = module._resource_observation(
        0,
        "baseline",
        {
            "available": True,
            "total_rss_bytes": 300,
            "limit_bytes": 1_000,
            "available_bytes": 700,
            "limit_source": "cgroup",
            "usage_ratio": 0.3,
            "partial": False,
            "processes": [
                {"label": "backend", "count": 1, "rss_bytes": 200},
                {"label": "supervisor", "count": 1, "rss_bytes": 100},
            ],
        },
        observed_at="2026-08-27T08:00:00Z",
    )
    assert resource["totalRssBytes"] == 300
    assert resource["processes"] == [
        {"label": "backend", "count": 1, "rssBytes": 200},
        {"label": "supervisor", "count": 1, "rssBytes": 100},
    ]

    document = {
        "openmaic": {
            "scenes": [
                {
                    "id": "quiz-a",
                    "type": "quiz",
                    "content": {
                        "questions": [
                            {
                                "id": "question-a",
                                "questionType": "single_choice",
                                "correctOptionIds": ["answer-a"],
                            }
                        ]
                    },
                }
            ]
        },
        "knowledgePointMappings": [{"knowledgePointId": "kp-a", "sceneIds": ["quiz-a"]}],
    }
    evidence = module._select_quiz_evidence(document)
    assert evidence.scene_id == "quiz-a"
    assert evidence.question_id == "question-a"
    assert evidence.knowledge_point_id == "kp-a"
    assert evidence.answer == ["answer-a"]

    document["knowledgePointMappings"].append({"knowledgePointId": "kp-b", "sceneIds": ["quiz-a"]})
    with pytest.raises(module.CapacityProbeError, match="quiz_evidence_invalid"):
        module._select_quiz_evidence(document)
