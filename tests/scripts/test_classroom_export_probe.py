from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts" / "classroom_export_probe.py"
    spec = importlib.util.spec_from_file_location("classroom_export_probe_under_test", path)
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


def _policy_revision(value: int) -> str:
    return f"{value:064x}"


def _policy_operation(value: int) -> str:
    return f"{value:032x}"


def _environment(root: Path, staging: Path) -> dict[str, str]:
    return {
        "YFEISTAI_LIVE_FIXTURE_TOKEN": "secret-platform-admin-token",
        "YFEISTAI_CANDIDATE_ROOT": str(root),
        "YFEISTAI_RELEASE_RUN_ID": "run-20260828",
        "YFEISTAI_ENVIRONMENT_ID": "acceptance-a",
        "YFEISTAI_ACCEPTANCE_TENANT_ID": "tenant-acceptance-a",
        "YFEISTAI_CLASSROOM_EXPORT_TIMEOUT_SECONDS": "900",
        "YFEISTAI_CLASSROOM_EXPORT_STAGING_DIR": str(staging),
        "WEB_BASE_URL": "https://classroom.example.test",
    }


def test_load_config_requires_unique_empty_external_staging_and_hides_token(
    tmp_path: Path,
) -> None:
    module = _module()
    root = tmp_path / "candidate"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()

    config = module._load_config(
        _environment(root, staging),
        cwd=root,
        candidate_loader=lambda _root: _candidate(),
    )

    assert config.candidate_root == root.resolve()
    assert config.staging_dir == staging.resolve()
    assert config.release_run == {
        "runId": "run-20260828",
        "environmentId": "acceptance-a",
    }
    assert config.timeout_seconds == 900
    assert config.tenant_id == "tenant-acceptance-a"
    assert config.admin_token.get_secret_value() == "secret-platform-admin-token"
    assert "secret-platform-admin-token" not in repr(config)


@pytest.mark.parametrize("case", ["inside_candidate", "nonempty", "relative"])
def test_load_config_rejects_unsafe_staging(tmp_path: Path, case: str) -> None:
    module = _module()
    root = tmp_path / "candidate"
    root.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    if case == "inside_candidate":
        staging = root / "raw"
        staging.mkdir()
    elif case == "nonempty":
        (staging / "existing.txt").write_text("occupied", encoding="utf-8")
    else:
        staging = Path("relative-staging")

    with pytest.raises(module.ExportProbeError, match="staging_directory_invalid"):
        module._load_config(
            _environment(root, staging),
            cwd=root,
            candidate_loader=lambda _root: _candidate(),
        )


def test_load_config_rejects_a_symlinked_staging_component(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "candidate"
    actual = tmp_path / "actual"
    link = tmp_path / "linked"
    root.mkdir()
    actual.mkdir()
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(module.ExportProbeError, match="staging_directory_invalid"):
        module._load_config(
            _environment(root, link),
            cwd=root,
            candidate_loader=lambda _root: _candidate(),
        )


@pytest.mark.parametrize("component", ["parent", "root"])
def test_load_config_rejects_a_windows_reparse_staging_component(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    component: str,
) -> None:
    module = _module()
    root = tmp_path / "candidate"
    staging_parent = tmp_path / "staging-parent"
    staging = staging_parent / "staging"
    root.mkdir()
    staging.mkdir(parents=True)
    marked = staging_parent if component == "parent" else staging
    real_lstat = Path.lstat
    monkeypatch.setattr(module, "_REPARSE_POINT_ATTRIBUTE", 0x400)

    def lstat_with_reparse(path: Path):
        observed = real_lstat(path)
        if path == marked:
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_file_attributes=0x400,
            )
        return observed

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse)

    with pytest.raises(module.ExportProbeError, match="staging_directory_invalid"):
        module._load_config(
            _environment(root, staging),
            cwd=root,
            candidate_loader=lambda _root: _candidate(),
        )


def test_staging_guard_rejects_a_windows_reparse_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    staging = tmp_path / "staging"
    staging.mkdir()
    entry = staging / "classroom.zip"
    entry.write_bytes(b"controlled")
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=tmp_path,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=staging,
        timeout_seconds=30,
        tenant_id="tenant-a",
    )
    real_lstat = Path.lstat
    monkeypatch.setattr(module, "_REPARSE_POINT_ATTRIBUTE", 0x400)

    def lstat_with_reparse(path: Path):
        observed = real_lstat(path)
        if path == entry:
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_file_attributes=0x400,
            )
        return observed

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse)

    with pytest.raises(module.ExportProbeError, match="staging_directory_invalid"):
        module._assert_staging_identity(
            config,
            expected_names=frozenset({"classroom.zip"}),
        )


def _identity(module):
    return module.IdentityCredential(
        username="export-teacher-a",
        user_id="teacher-user-a",
        token=module.SecretStr("secret-teacher-session"),
    )


def test_identity_download_is_nonredirecting_secret_scoped_and_streamed_exclusively(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    seen: list[httpx.Request] = []
    chunk_sizes: list[int | None] = []
    body = b"verified-export-bytes"
    original_aiter_bytes = httpx.Response.aiter_bytes

    async def tracked_aiter_bytes(
        response: httpx.Response,
        chunk_size: int | None = None,
    ):
        chunk_sizes.append(chunk_size)
        async for chunk in original_aiter_bytes(response, chunk_size=chunk_size):
            yield chunk

    monkeypatch.setattr(httpx.Response, "aiter_bytes", tracked_aiter_bytes)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": "attachment; filename*=UTF-8''classroom.zip",
                "Content-Length": str(len(body)),
            },
            content=body,
        )

    target = tmp_path / "classroom.zip"

    async def exercise():
        async with module.ExportApi(
            "https://classroom.example.test",
            "secret-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            return await api.tenant_identity_download(
                "/api/v1/classroom-exports/export-a/download",
                identity=_identity(module),
                tenant_id="tenant-a",
                target=target,
                expected_filename="classroom.zip",
                expected_content_type="application/zip",
                max_bytes=1024,
            )

    result = asyncio.run(exercise())

    assert target.read_bytes() == body
    assert result == {
        "contentType": "application/zip",
        "contentDisposition": "attachment; filename*=UTF-8''classroom.zip",
        "byteLength": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    request = seen[0]
    assert request.headers["x-tenant-id"] == "tenant-a"
    assert request.headers["cookie"] == ("dt_token=secret-teacher-session; dt_tenant=tenant-a")
    assert "authorization" not in request.headers
    assert chunk_sizes == [64 * 1024]


def test_identity_download_rejects_a_non_contract_basename_before_request(
    tmp_path: Path,
) -> None:
    module = _module()
    requested = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, content=b"unexpected")

    async def exercise():
        async with module.ExportApi(
            "https://classroom.example.test",
            "secret-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            await api.tenant_identity_download(
                "/api/v1/classroom-exports/export-a/download",
                identity=_identity(module),
                tenant_id="tenant-a",
                target=tmp_path / "other.bin",
                expected_filename="other.bin",
                expected_content_type="application/octet-stream",
                max_bytes=1024,
            )

    with pytest.raises(module.ExportProbeError, match="staging_directory_invalid"):
        asyncio.run(exercise())
    assert requested is False


def test_identity_download_rejects_staging_directory_replacement_during_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    staging = tmp_path / "staging"
    displaced = tmp_path / "staging-displaced"
    staging.mkdir()
    target = staging / "classroom.zip"
    body = b"verified-export-bytes"
    real_open = os.open
    raced = False

    def racing_open(path, flags, mode=0o777, *args, **kwargs):
        nonlocal raced
        is_target = Path(path).name == target.name and bool(flags & os.O_CREAT)
        if is_target and not raced:
            raced = True
            staging.rename(displaced)
            staging.mkdir()
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": "attachment; filename*=UTF-8''classroom.zip",
            },
            content=body,
        )

    async def exercise():
        async with module.ExportApi(
            "https://classroom.example.test",
            "secret-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            await api.tenant_identity_download(
                "/api/v1/classroom-exports/export-a/download",
                identity=_identity(module),
                tenant_id="tenant-a",
                target=target,
                expected_filename="classroom.zip",
                expected_content_type="application/zip",
                max_bytes=1024,
            )

    with pytest.raises(module.ExportProbeError, match="staging_directory_invalid"):
        asyncio.run(exercise())
    assert raced is True


@pytest.mark.parametrize(
    ("status", "headers", "content", "error"),
    [
        (302, {"Location": "https://storage.invalid/leak"}, b"", "export_download_rejected"),
        (
            200,
            {
                "Content-Type": "text/plain",
                "Content-Disposition": "attachment; filename*=UTF-8''classroom.zip",
            },
            b"wrong",
            "export_download_invalid",
        ),
        (
            200,
            {
                "Content-Type": "application/zip",
                "Content-Disposition": "attachment; filename*=UTF-8''wrong.zip",
            },
            b"wrong",
            "export_download_invalid",
        ),
        (
            200,
            {
                "Content-Type": "application/zip",
                "Content-Disposition": "attachment; filename*=UTF-8''classroom.zip",
            },
            b"too-large",
            "export_download_too_large",
        ),
    ],
)
def test_identity_download_rejects_redirect_metadata_and_size_without_partial_file(
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
    content: bytes,
    error: str,
) -> None:
    module = _module()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, content=content)

    target = tmp_path / "classroom.zip"

    async def exercise():
        async with module.ExportApi(
            "https://classroom.example.test",
            "secret-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            await api.tenant_identity_download(
                "/api/v1/classroom-exports/export-a/download",
                identity=_identity(module),
                tenant_id="tenant-a",
                target=target,
                expected_filename="classroom.zip",
                expected_content_type="application/zip",
                max_bytes=4,
            )

    with pytest.raises(module.ExportProbeError, match=error):
        asyncio.run(exercise())
    assert not target.exists()


@pytest.mark.parametrize("terminal", ["failed", "canceled"])
def test_wait_for_export_fails_closed_on_unsuccessful_terminal_status(
    terminal: str,
) -> None:
    module = _module()
    api = SimpleNamespace()

    async def tenant_identity_json(*_args, **_kwargs):
        return {
            "job_id": "export-a",
            "job_kind": "export",
            "phase": "export",
            "status": terminal,
            "progress_percent": 50,
            "waiting_reason": None,
            "cancellable": False,
            "retryable": True,
            "outline": None,
            "error_category": "worker",
            "error_code": "EXPORT_FAILED",
            "retry_of_job_id": None,
            "export_format": "pptx",
            "download_ready": False,
        }

    api.tenant_identity_json = tenant_identity_json
    with pytest.raises(module.ExportProbeError, match="export_job_failed"):
        asyncio.run(
            module._wait_for_export(
                api,
                export_id="export-a",
                export_format="pptx",
                identity=_identity(module),
                tenant_id="tenant-a",
                end_time=module.time.monotonic() + 2,
            )
        )


def test_wait_for_export_requires_succeeded_100_and_download_ready() -> None:
    module = _module()
    responses = [
        {
            "job_id": "export-a",
            "job_kind": "export",
            "phase": "export",
            "status": "queued",
            "progress_percent": 0,
            "waiting_reason": "queued",
            "cancellable": True,
            "retryable": False,
            "outline": None,
            "error_category": None,
            "error_code": None,
            "retry_of_job_id": None,
            "export_format": "offline_html",
            "download_ready": False,
        },
        {
            "job_id": "export-a",
            "job_kind": "export",
            "phase": "export",
            "status": "succeeded",
            "progress_percent": 100,
            "waiting_reason": None,
            "cancellable": False,
            "retryable": False,
            "outline": None,
            "error_category": None,
            "error_code": None,
            "retry_of_job_id": None,
            "export_format": "offline_html",
            "download_ready": True,
        },
    ]

    class Api:
        async def tenant_identity_json(self, *_args, **_kwargs):
            return responses.pop(0)

    result = asyncio.run(
        module._wait_for_export(
            Api(),
            export_id="export-a",
            export_format="offline_html",
            identity=_identity(module),
            tenant_id="tenant-a",
            end_time=module.time.monotonic() + 2,
        )
    )

    assert result["status"] == "succeeded"
    assert result["progress_percent"] == 100
    assert result["download_ready"] is True
    assert responses == []


def test_stage_exports_posts_polls_and_downloads_all_four_fixed_formats(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = tmp_path / "candidate"
    staging = tmp_path / "staging"
    candidate_root.mkdir()
    staging.mkdir()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=candidate_root,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=staging,
        timeout_seconds=30,
        tenant_id="tenant-a",
    )
    material = module.FixtureMaterial(
        "export-run-a",
        "teacher-a",
        module.SecretStr("teacher-password-a"),
    )
    fixture = module.PublishedFixture(
        "tenant-a",
        "version-a",
        "d" * 64,
        _identity(module),
    )
    requests: list[tuple[str, str, object, object]] = []

    class Api:
        async def tenant_identity_json(
            self,
            method,
            path,
            *,
            headers=None,
            json_body=None,
            **_kwargs,
        ):
            requests.append((method, path, headers, json_body))
            export_format = (
                str(json_body["format"])
                if method == "POST"
                else path.rsplit("/", 1)[-1].removeprefix("export-")
            )
            return {
                "job_id": f"export-{export_format}",
                "job_kind": "export",
                "phase": "export",
                "status": "queued" if method == "POST" else "succeeded",
                "progress_percent": 0 if method == "POST" else 100,
                "waiting_reason": "queued" if method == "POST" else None,
                "cancellable": method == "POST",
                "retryable": False,
                "outline": None,
                "error_category": None,
                "error_code": None,
                "retry_of_job_id": None,
                "export_format": export_format,
                "download_ready": method != "POST",
            }

        async def tenant_identity_download(
            self,
            path,
            *,
            target,
            expected_filename,
            expected_content_type,
            **_kwargs,
        ):
            body = f"body:{path}".encode()
            target.write_bytes(body)
            return {
                "contentType": expected_content_type,
                "contentDisposition": (f"attachment; filename*=UTF-8''{expected_filename}"),
                "byteLength": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }

    exports = asyncio.run(
        module._stage_exports(
            config,
            Api(),
            material=material,
            fixture=fixture,
            end_time=module.time.monotonic() + 5,
        )
    )

    assert tuple(exports) == module.CLASSROOM_EXPORT_KINDS
    assert {item[0] for item in requests} == {"POST", "GET"}
    assert len(requests) == 8
    for export_format in module.CLASSROOM_EXPORT_KINDS:
        filename = module.CLASSROOM_EXPORT_PATHS[export_format]
        assert (staging / filename).is_file()
        assert exports[export_format]["exportId"] == f"export-{export_format}"
        assert exports[export_format]["jobId"] == f"export-{export_format}"
        assert exports[export_format]["relativePath"] == filename
        post = next(
            item for item in requests if item[0] == "POST" and item[3] == {"format": export_format}
        )
        assert post[1] == "/api/v1/classroom-versions/version-a/exports"
        assert post[2] == {"Idempotency-Key": f"export-run-a-{export_format}"}


def test_stage_exports_rejects_staging_directory_injection_between_downloads(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = tmp_path / "candidate"
    staging = tmp_path / "staging"
    candidate_root.mkdir()
    staging.mkdir()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=candidate_root,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=staging,
        timeout_seconds=30,
        tenant_id="tenant-a",
    )
    fixture = module.PublishedFixture(
        "tenant-a",
        "version-a",
        "d" * 64,
        _identity(module),
    )
    material = module.FixtureMaterial(
        "export-run-a",
        "teacher-a",
        module.SecretStr("teacher-password-a"),
    )
    posts: list[str] = []

    class Api:
        async def tenant_identity_json(self, method, _path, *, json_body, **_kwargs):
            export_format = str(json_body["format"])
            posts.append(export_format)
            return {
                "job_id": f"export-{export_format}",
                "job_kind": "export",
                "phase": "export",
                "status": "succeeded",
                "progress_percent": 100,
                "waiting_reason": None,
                "cancellable": False,
                "retryable": False,
                "outline": None,
                "error_category": None,
                "error_code": None,
                "retry_of_job_id": None,
                "export_format": export_format,
                "download_ready": True,
            }

        async def tenant_identity_download(
            self,
            _path,
            *,
            target,
            expected_filename,
            expected_content_type,
            **_kwargs,
        ):
            body = b"artifact"
            target.write_bytes(body)
            (staging / "unexpected.txt").write_text("injected", encoding="utf-8")
            return {
                "contentType": expected_content_type,
                "contentDisposition": (f"attachment; filename*=UTF-8''{expected_filename}"),
                "byteLength": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }

    with pytest.raises(module.ExportProbeError, match="staging_directory_invalid"):
        asyncio.run(
            module._stage_exports(
                config,
                Api(),
                material=material,
                fixture=fixture,
                end_time=module.time.monotonic() + 5,
            )
        )
    assert posts == ["classroom_zip"]


def test_stage_exports_fails_closed_when_create_returns_a_terminal_failure(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = tmp_path / "candidate"
    staging = tmp_path / "staging"
    candidate_root.mkdir()
    staging.mkdir()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=candidate_root,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=staging,
        timeout_seconds=30,
        tenant_id="tenant-a",
    )
    fixture = module.PublishedFixture(
        "tenant-a",
        "version-a",
        "d" * 64,
        _identity(module),
    )
    calls: list[tuple[str, str]] = []

    class Api:
        async def tenant_identity_json(self, method, path, **_kwargs):
            calls.append((method, path))
            return {
                "job_id": "export-classroom_zip",
                "job_kind": "export",
                "phase": "export",
                "status": "failed",
                "progress_percent": 0,
                "waiting_reason": None,
                "cancellable": False,
                "retryable": True,
                "outline": None,
                "error_category": "worker",
                "error_code": "EXPORT_FAILED",
                "retry_of_job_id": None,
                "export_format": "classroom_zip",
                "download_ready": False,
            }

        async def tenant_identity_download(self, *_args, **_kwargs):
            raise AssertionError("failed exports must never be downloaded")

    with pytest.raises(module.ExportProbeError, match="export_job_failed"):
        asyncio.run(
            module._stage_exports(
                config,
                Api(),
                material=module.FixtureMaterial(
                    "export-run-a",
                    "teacher-a",
                    module.SecretStr("teacher-password-a"),
                ),
                fixture=fixture,
                end_time=module.time.monotonic() + 5,
            )
        )
    assert calls == [
        ("POST", "/api/v1/classroom-versions/version-a/exports"),
    ]


def _classroom_response(
    *,
    status: str,
    lifecycle: str,
    document: dict[str, object] | None = None,
    validation: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "assetId": "asset-a",
        "draftId": "draft-a",
        "jobId": "job-a",
        "lifecycleState": lifecycle,
        "status": status,
        "title": "Classroom export acceptance",
        "courseId": "course-0123456789abcdef",
        "classId": "class-0123456789abcdef",
        "ownerId": "teacher-user-a",
        "revision": 1,
        "outline": {"title": "Export outline"},
        "document": document,
        "classroomVersionId": None,
        "confirmedOutlineSha256": "c" * 64 if lifecycle == "editing" else None,
        "validationReport": validation,
        "idempotencyKey": "export-0123456789abcdef-classroom",
    }


def test_confirm_outline_response_is_exact_and_bound_to_the_created_classroom() -> None:
    module = _module()
    response = _classroom_response(
        status="queued",
        lifecycle="generating_content",
    )

    confirmed = module._confirmed_classroom_response(
        response,
        asset_id="asset-a",
        draft_id="draft-a",
        owner_id="teacher-user-a",
    )

    assert confirmed == response


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("unexpected", "field"),
        ("assetId", "asset-other"),
        ("draftId", "draft-other"),
        ("ownerId", "teacher-user-other"),
        ("lifecycleState", "awaiting_outline"),
    ],
)
def test_confirm_outline_response_rejects_schema_or_classroom_binding_drift(
    mutation: str,
    value: object,
) -> None:
    module = _module()
    response = _classroom_response(
        status="queued",
        lifecycle="generating_content",
    )
    response[mutation] = value

    with pytest.raises(module.ExportProbeError, match="classroom_fixture_invalid"):
        module._confirmed_classroom_response(
            response,
            asset_id="asset-a",
            draft_id="draft-a",
            owner_id="teacher-user-a",
        )


def test_create_fixture_uses_formal_teacher_catalog_generation_review_and_publish_apis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = tmp_path / "candidate"
    staging = tmp_path / "staging"
    candidate_root.mkdir()
    staging.mkdir()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=candidate_root,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=staging,
        timeout_seconds=30,
        tenant_id="tenant-a",
    )
    material = module.FixtureMaterial(
        "export-0123456789abcdef",
        "export-teacher-0123456789abcdef",
        module.SecretStr("teacher-password-a"),
    )
    state = module.ProbeState()
    calls: list[tuple[str, str]] = []

    def review(status: str) -> dict[str, object]:
        return {
            "id": "review-a",
            "assetId": "asset-a",
            "draftId": "draft-a",
            "draftRevision": 1,
            "documentSha256": "d" * 64,
            "validationReportSha256": "e" * 64,
            "submittedBy": "teacher-user-a",
            "scope": "class",
            "classId": "class-0123456789abcdef",
            "status": status,
            "warnings": [],
            "reviewerId": "admin-a" if status == "approved" else None,
            "comment": "approved" if status == "approved" else None,
        }

    class Api:
        async def admin_json(self, method, path, **_kwargs):
            calls.append((method, path))
            if path == "/api/v1/auth/users":
                return {
                    "ok": True,
                    "user_id": "teacher-user-a",
                    "username": material.teacher_username,
                    "role": "user",
                    "is_admin": False,
                }
            if path.endswith("/approve"):
                return review("approved")
            raise AssertionError(path)

        async def login_identity(self, username, _password):
            calls.append(("POST", "/api/v1/auth/login"))
            return module.IdentityCredential(
                username,
                "teacher-user-a",
                module.SecretStr("teacher-session-a"),
            )

        async def tenant_admin_json(self, method, path, **_kwargs):
            calls.append((method, path))
            if path.endswith("/members"):
                return {
                    "tenant_id": "tenant-a",
                    "user_id": "teacher-user-a",
                    "roles": ["teacher"],
                    "grants": [
                        {
                            "role": "teacher",
                            "scope_type": "tenant",
                            "scope_id": "tenant-a",
                        }
                    ],
                }
            if path == "/api/v1/teaching/courses":
                json_body = _kwargs["json_body"]
                return {
                    "id": json_body["id"],
                    "title": json_body["title"],
                    "status": "active",
                    "createdAt": "2026-08-28T00:00:00Z",
                }
            if path == "/api/v1/teaching/generation-quota-grants":
                return {
                    "grantId": "grant-a",
                    "tenantId": "tenant-a",
                    "units": 20,
                    "balance": 20,
                    "created": True,
                }
            if path.endswith("/approve"):
                return review("approved")
            raise AssertionError(path)

        async def tenant_identity_json(
            self,
            method,
            path,
            *,
            json_body=None,
            **_kwargs,
        ):
            calls.append((method, path))
            if path == "/api/v1/teaching/courses":
                raise AssertionError("teacher must not create tenant courses")
            if path.endswith("/classes"):
                return {
                    "id": json_body["id"],
                    "courseId": "course-0123456789abcdef",
                    "name": json_body["name"],
                    "status": "active",
                    "createdAt": "2026-08-28T00:00:00Z",
                }
            if path.endswith("/enrollments"):
                return {
                    "classId": "class-0123456789abcdef",
                    "userId": "teacher-user-a",
                    "status": "active",
                    "createdAt": "2026-08-28T00:00:00Z",
                }
            if path == "/api/v1/classrooms":
                assert json_body["requestedExports"] == list(module.CLASSROOM_EXPORT_KINDS)
                return _classroom_response(
                    status="awaiting_confirmation",
                    lifecycle="awaiting_outline",
                )
            if path.endswith("/confirm-outline"):
                return _classroom_response(
                    status="queued",
                    lifecycle="generating_content",
                )
            if path.endswith("/validate"):
                return _classroom_response(
                    status="succeeded",
                    lifecycle="editing",
                    document={"dslVersion": "0.1.0"},
                    validation={"valid": True},
                )
            if path.endswith("/submit"):
                return review("pending")
            if path.endswith("/publish"):
                return {
                    "versionId": "version-a",
                    "assetId": "asset-a",
                    "versionNumber": 1,
                    "documentSha256": "d" * 64,
                    "publicationScope": "class",
                    "classId": "class-0123456789abcdef",
                    "idempotencyKey": "export-0123456789abcdef-publish",
                }
            raise AssertionError(path)

    async def configure_policy(_api, *, state):
        calls.append(("PUT", "/api/v1/classroom-export-policy"))
        state.mp4_policy_original = False
        state.mp4_policy_configured = True

    async def wait_generated(*_args, **_kwargs):
        calls.append(("GET", "/api/v1/classrooms/asset-a"))
        return _classroom_response(
            status="succeeded",
            lifecycle="editing",
            document={"dslVersion": "0.1.0"},
        )

    monkeypatch.setattr(module, "_configure_mp4_policy", configure_policy)
    monkeypatch.setattr(module, "_wait_for_generated_classroom", wait_generated)

    fixture = asyncio.run(
        module._create_fixture(
            config,
            Api(),
            material=material,
            state=state,
            end_time=module.time.monotonic() + 5,
        )
    )

    assert fixture.tenant_id == "tenant-a"
    assert fixture.classroom_version_id == "version-a"
    assert fixture.document_sha256 == "d" * 64
    assert state.teacher_username == material.teacher_username
    assert state.teacher_user_id == "teacher-user-a"
    expected_paths = {
        "/api/v1/auth/users",
        "/api/v1/auth/login",
        "/api/v1/tenants/tenant-a/members",
        "/api/v1/classroom-export-policy",
        "/api/v1/teaching/courses",
        "/api/v1/teaching/courses/course-0123456789abcdef/classes",
        "/api/v1/teaching/classes/class-0123456789abcdef/enrollments",
        "/api/v1/teaching/generation-quota-grants",
        "/api/v1/classrooms",
        "/api/v1/classrooms/asset-a/confirm-outline",
        "/api/v1/classrooms/asset-a",
        "/api/v1/classrooms/asset-a/validate",
        "/api/v1/classrooms/asset-a/submit",
        "/api/v1/classroom-reviews/review-a/approve",
        "/api/v1/classrooms/asset-a/publish",
    }
    assert {path for _method, path in calls} == expected_paths


def test_execute_probe_emits_only_the_canonical_candidate_bound_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    candidate_root = tmp_path / "candidate"
    staging = tmp_path / "staging"
    candidate_root.mkdir()
    staging.mkdir()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=candidate_root,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=staging,
        timeout_seconds=30,
        tenant_id="tenant-a",
    )
    teacher = _identity(module)
    material = module.FixtureMaterial(
        "export-run-a",
        "teacher-a",
        module.SecretStr("teacher-password-a"),
    )
    fixture = module.PublishedFixture("tenant-a", "version-a", "d" * 64, teacher)
    exports = {
        kind: {
            "exportId": f"export-{kind}",
            "jobId": f"export-{kind}",
            "status": "succeeded",
            "progressPercent": 100,
            "downloadReady": True,
            "relativePath": module.CLASSROOM_EXPORT_PATHS[kind],
            "contentType": module.CLASSROOM_EXPORT_CONTENT_TYPES[kind],
            "contentDisposition": (
                f"attachment; filename*=UTF-8''{module.CLASSROOM_EXPORT_PATHS[kind]}"
            ),
            "byteLength": 1,
            "sha256": str(index) * 64,
        }
        for index, kind in enumerate(module.CLASSROOM_EXPORT_KINDS, start=1)
    }
    captured: dict[str, object] = {}

    async def create_fixture(*_args, **_kwargs):
        return fixture

    async def stage_exports(*_args, **_kwargs):
        return exports

    def derive(body, **kwargs):
        captured["body"] = json.loads(body)
        captured["kwargs"] = kwargs
        return {"fourFormatsDownloaded": True}

    monkeypatch.setattr(module, "_create_fixture", create_fixture)
    monkeypatch.setattr(module, "_stage_exports", stage_exports)
    monkeypatch.setattr(module, "derive_classroom_export_checks", derive)

    body = asyncio.run(
        module._execute_export_probe(
            config,
            object(),
            material=material,
            state=module.ProbeState(),
            end_time=module.time.monotonic() + 5,
        )
    )

    report = json.loads(body)
    assert set(report) == {
        "schemaVersion",
        "producer",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "tenantId",
        "classroomVersionId",
        "documentSha256",
        "exports",
    }
    assert report["schemaVersion"] == module.CLASSROOM_EXPORT_SCHEMA_VERSION
    assert report["producer"] == module.CLASSROOM_EXPORT_PRODUCER
    assert report["candidate"] == _candidate()
    assert report["releaseRun"] == config.release_run
    assert report["tenantId"] == "tenant-a"
    assert report["exports"] == exports
    assert report["observedAt"].endswith("Z")
    assert captured["kwargs"] == {
        "artifact_root": staging,
        "candidate": _candidate(),
        "release_run": config.release_run,
        "expected_base_url": config.base_url,
    }
    serialized = json.dumps(report).lower()
    for forbidden in ("token", "cookie", "password", "ticket", "secret"):
        assert forbidden not in serialized


def test_run_probe_always_cleans_resources_before_identity_and_cleanup_failure_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    order: list[str] = []
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=tmp_path,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=tmp_path,
        timeout_seconds=30,
    )

    class Api:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(module, "ExportApi", lambda *_args, **_kwargs: Api())

    async def execute(_config, _api, *, state, **_kwargs):
        state.tenant_id = "tenant-a"
        state.teacher_username = "teacher-a"
        state.teacher_user_id = "teacher-user-a"
        return b"report\n"

    async def cleanup_resources(*_args, **_kwargs):
        order.append("resources")
        raise module.ExportProbeError("resource_cleanup_failed")

    async def cleanup_identity(*_args, **_kwargs):
        order.append("identity")

    monkeypatch.setattr(module, "_execute_export_probe", execute)
    monkeypatch.setattr(module, "_cleanup_resources", cleanup_resources)
    monkeypatch.setattr(module, "_cleanup_identity", cleanup_identity)

    with pytest.raises(module.ExportProbeError, match="classroom_export_cleanup_failed"):
        asyncio.run(module._run_export_probe(config))
    assert order == ["resources", "identity"]


def test_run_probe_restores_policy_and_deletes_identity_after_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    calls: list[tuple[str, str, object | None]] = []
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=tmp_path,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=tmp_path,
        timeout_seconds=30,
        tenant_id="tenant-a",
    )

    class Api:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def tenant_admin_json(
            self,
            method,
            path,
            *,
            json_body=None,
            **_kwargs,
        ):
            calls.append((method, path, json_body))
            return {
                "tenant_id": "tenant-a",
                "allow_mp4": bool(json_body["allow_mp4"]),
                "exists": True,
                "revision": _policy_revision(3),
                "operation_id": json_body["operation_id"],
            }

        async def admin_json(self, method, path, *, json_body=None, **_kwargs):
            calls.append((method, path, json_body))
            return {"ok": True}

    monkeypatch.setattr(module, "ExportApi", lambda *_args, **_kwargs: Api())
    material = module.FixtureMaterial(
        "export-run-a",
        "teacher-a",
        module.SecretStr("teacher-password-a"),
    )
    monkeypatch.setattr(module, "_fixture_material", lambda _config: material)

    async def execute(_config, _api, *, state, **_kwargs):
        state.teacher_username = "teacher-a"
        state.teacher_user_id = "teacher-user-a"
        state.mp4_policy_original = False
        state.mp4_policy_original_exists = True
        state.mp4_policy_original_revision = _policy_revision(1)
        state.mp4_policy_original_operation_id = _policy_operation(1)
        state.mp4_policy_cleanup_revision = _policy_revision(2)
        state.mp4_policy_enable_operation_id = _policy_operation(2)
        state.mp4_policy_configured = True
        raise module.ExportProbeError("export_job_failed")

    monkeypatch.setattr(module, "_execute_export_probe", execute)
    monkeypatch.setattr(
        module.secrets,
        "token_hex",
        lambda _size: _policy_operation(3),
    )

    with pytest.raises(module.ExportProbeError, match="export_job_failed"):
        asyncio.run(module._run_export_probe(config))
    assert calls == [
        (
            "PUT",
            "/api/v1/classroom-export-policy",
            {
                "expected_revision": _policy_revision(2),
                "allow_mp4": False,
                "operation_id": _policy_operation(3),
            },
        ),
        (
            "DELETE",
            "/api/v1/auth/users/teacher-a",
            {"expected_user_id": "teacher-user-a"},
        ),
    ]


def test_identity_cleanup_recovers_ownership_after_an_ambiguous_create() -> None:
    module = _module()
    material = module.FixtureMaterial(
        "export-run-a",
        "export-teacher-a",
        module.SecretStr("teacher-password-a"),
    )
    calls: list[tuple[str, str, object | None]] = []

    class Api:
        async def login_identity(self, username, password):
            assert username == material.teacher_username
            assert password.get_secret_value() == "teacher-password-a"
            calls.append(("POST", "/api/v1/auth/login", None))
            return module.IdentityCredential(
                username,
                "teacher-user-a",
                module.SecretStr("teacher-session-a"),
            )

        async def admin_json(self, method, path, *, json_body=None, **_kwargs):
            calls.append((method, path, json_body))
            return {"ok": True}

    state = module.ProbeState()
    state.teacher_username = material.teacher_username

    asyncio.run(module._cleanup_identity(Api(), state, material=material))

    assert calls == [
        ("POST", "/api/v1/auth/login", None),
        (
            "DELETE",
            "/api/v1/auth/users/export-teacher-a",
            {"expected_user_id": "teacher-user-a"},
        ),
    ]


def _listed_user(*, user_id: str, username: str) -> dict[str, object]:
    return {
        "id": user_id,
        "username": username,
        "role": "user",
        "created_at": "2026-08-28T00:00:00Z",
        "disabled": False,
        "avatar": "",
    }


def _identity_cleanup_state(module):
    state = module.ProbeState()
    state.teacher_username = "export-teacher-a"
    state.teacher_user_id = "teacher-user-a"
    state.teacher_identity = module.IdentityCredential(
        "export-teacher-a",
        "teacher-user-a",
        module.SecretStr("teacher-session-a"),
    )
    material = module.FixtureMaterial(
        "export-run-a",
        "export-teacher-a",
        module.SecretStr("teacher-password-a"),
    )
    return state, material


def test_identity_cleanup_accepts_missing_user_after_delete_response_is_lost() -> None:
    module = _module()
    state, material = _identity_cleanup_state(module)
    calls: list[tuple[str, str]] = []

    class Api:
        async def admin_json(self, method, path, **_kwargs):
            calls.append((method, path))
            raise module.ExportProbeError("candidate_request_failed")

        async def admin_list_json(self, method, path, **_kwargs):
            calls.append((method, path))
            return []

    asyncio.run(module._cleanup_identity(Api(), state, material=material))

    assert calls == [
        ("DELETE", "/api/v1/auth/users/export-teacher-a"),
        ("GET", "/api/v1/auth/users"),
    ]
    assert state.teacher_username is None
    assert state.teacher_user_id is None


def test_identity_cleanup_retries_once_after_same_identity_is_still_listed() -> None:
    module = _module()
    state, material = _identity_cleanup_state(module)
    calls: list[tuple[str, str]] = []
    delete_attempts = 0

    class Api:
        async def admin_json(self, method, path, **_kwargs):
            nonlocal delete_attempts
            calls.append((method, path))
            delete_attempts += 1
            if delete_attempts == 1:
                raise module.ExportProbeError("candidate_request_failed")
            return {"ok": True}

        async def admin_list_json(self, method, path, **_kwargs):
            calls.append((method, path))
            return [
                _listed_user(
                    user_id="teacher-user-a",
                    username="export-teacher-a",
                )
            ]

    asyncio.run(module._cleanup_identity(Api(), state, material=material))

    assert calls == [
        ("DELETE", "/api/v1/auth/users/export-teacher-a"),
        ("GET", "/api/v1/auth/users"),
        ("DELETE", "/api/v1/auth/users/export-teacher-a"),
    ]


def test_identity_cleanup_reconciles_a_second_lost_delete_response() -> None:
    module = _module()
    state, material = _identity_cleanup_state(module)
    calls: list[tuple[str, str]] = []
    listings = iter(
        (
            [
                _listed_user(
                    user_id="teacher-user-a",
                    username="export-teacher-a",
                )
            ],
            [],
        )
    )

    class Api:
        async def admin_json(self, method, path, **_kwargs):
            calls.append((method, path))
            raise module.ExportProbeError("candidate_request_failed")

        async def admin_list_json(self, method, path, **_kwargs):
            calls.append((method, path))
            return next(listings)

    asyncio.run(module._cleanup_identity(Api(), state, material=material))

    assert calls == [
        ("DELETE", "/api/v1/auth/users/export-teacher-a"),
        ("GET", "/api/v1/auth/users"),
        ("DELETE", "/api/v1/auth/users/export-teacher-a"),
        ("GET", "/api/v1/auth/users"),
    ]


def test_identity_cleanup_never_deletes_a_changed_identity_after_reconciliation() -> None:
    module = _module()
    state, material = _identity_cleanup_state(module)
    calls: list[tuple[str, str]] = []

    class Api:
        async def admin_json(self, method, path, **_kwargs):
            calls.append((method, path))
            raise module.ExportProbeError("candidate_request_failed")

        async def admin_list_json(self, method, path, **_kwargs):
            calls.append((method, path))
            return [
                _listed_user(
                    user_id="teacher-user-replacement",
                    username="export-teacher-a",
                )
            ]

    with pytest.raises(module.ExportProbeError, match="identity_cleanup_failed"):
        asyncio.run(module._cleanup_identity(Api(), state, material=material))

    assert calls == [
        ("DELETE", "/api/v1/auth/users/export-teacher-a"),
        ("GET", "/api/v1/auth/users"),
    ]


def test_identity_cleanup_rejects_nonexact_user_list_schema() -> None:
    module = _module()
    state, material = _identity_cleanup_state(module)

    class Api:
        async def admin_json(self, *_args, **_kwargs):
            raise module.ExportProbeError("candidate_request_failed")

        async def admin_list_json(self, *_args, **_kwargs):
            record = _listed_user(
                user_id="teacher-user-a",
                username="export-teacher-a",
            )
            record["unexpected"] = True
            return [record]

    with pytest.raises(module.ExportProbeError, match="identity_cleanup_failed"):
        asyncio.run(module._cleanup_identity(Api(), state, material=material))


def test_admin_list_json_accepts_only_a_json_array() -> None:
    module = _module()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _listed_user(
                    user_id="teacher-user-a",
                    username="export-teacher-a",
                )
            ],
        )

    async def exercise():
        async with module.ExportApi(
            "https://classroom.example.test",
            "secret-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            return await api.admin_list_json(
                "GET",
                "/api/v1/auth/users",
                expected_statuses=frozenset({200}),
            )

    assert asyncio.run(exercise()) == [
        _listed_user(
            user_id="teacher-user-a",
            username="export-teacher-a",
        )
    ]


def test_mp4_policy_is_read_enabled_and_restored_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    calls: list[tuple[str, str, object | None]] = []
    responses = iter(
        (
            {
                "tenant_id": "tenant-a",
                "allow_mp4": False,
                "exists": True,
                "revision": _policy_revision(1),
                "operation_id": _policy_operation(7),
            },
            {
                "tenant_id": "tenant-a",
                "allow_mp4": True,
                "exists": True,
                "revision": _policy_revision(2),
                "operation_id": _policy_operation(1),
            },
            {
                "tenant_id": "tenant-a",
                "allow_mp4": False,
                "exists": True,
                "revision": _policy_revision(3),
                "operation_id": _policy_operation(2),
            },
        )
    )

    class Api:
        async def tenant_admin_json(
            self,
            method,
            path,
            *,
            json_body=None,
            **_kwargs,
        ):
            calls.append((method, path, json_body))
            return next(responses)

    state = module.ProbeState()
    state.tenant_id = "tenant-a"
    operations = iter((_policy_operation(1), _policy_operation(2)))
    monkeypatch.setattr(module.secrets, "token_hex", lambda _size: next(operations))

    async def exercise():
        await module._configure_mp4_policy(Api(), state=state)
        await module._cleanup_resources(Api(), state)

    asyncio.run(exercise())

    assert calls == [
        ("GET", "/api/v1/classroom-export-policy", None),
        (
            "PUT",
            "/api/v1/classroom-export-policy",
            {
                "allow_mp4": True,
                "expected_revision": _policy_revision(1),
                "operation_id": _policy_operation(1),
            },
        ),
        (
            "PUT",
            "/api/v1/classroom-export-policy",
            {
                "expected_revision": _policy_revision(2),
                "allow_mp4": False,
                "operation_id": _policy_operation(2),
            },
        ),
    ]
    assert state.mp4_policy_configured is False


def test_mp4_policy_absent_row_is_restored_as_a_generation_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    calls: list[tuple[str, str, object | None]] = []
    responses = iter(
        (
            {
                "tenant_id": "tenant-a",
                "allow_mp4": False,
                "exists": False,
                "revision": "absent",
                "operation_id": None,
            },
            {
                "tenant_id": "tenant-a",
                "allow_mp4": True,
                "exists": True,
                "revision": _policy_revision(1),
                "operation_id": _policy_operation(1),
            },
            {
                "tenant_id": "tenant-a",
                "allow_mp4": False,
                "exists": False,
                "revision": _policy_revision(2),
                "operation_id": _policy_operation(2),
            },
        )
    )

    class Api:
        async def tenant_admin_json(
            self,
            method,
            path,
            *,
            json_body=None,
            **_kwargs,
        ):
            calls.append((method, path, json_body))
            return next(responses)

    state = module.ProbeState()
    state.tenant_id = "tenant-a"
    operations = iter((_policy_operation(1), _policy_operation(2)))
    monkeypatch.setattr(module.secrets, "token_hex", lambda _size: next(operations))

    async def exercise():
        await module._configure_mp4_policy(Api(), state=state)
        await module._cleanup_resources(Api(), state)

    asyncio.run(exercise())

    assert calls == [
        ("GET", "/api/v1/classroom-export-policy", None),
        (
            "PUT",
            "/api/v1/classroom-export-policy",
            {
                "allow_mp4": True,
                "expected_revision": "absent",
                "operation_id": _policy_operation(1),
            },
        ),
        (
            "DELETE",
            "/api/v1/classroom-export-policy",
            {
                "expected_revision": _policy_revision(1),
                "operation_id": _policy_operation(2),
            },
        ),
    ]
    assert state.mp4_policy_configured is False


def test_mp4_policy_lost_responses_are_reconciled_by_operation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    calls: list[tuple[str, object | None]] = []
    operations = iter((_policy_operation(1), _policy_operation(2)))
    monkeypatch.setattr(module.secrets, "token_hex", lambda _size: next(operations))
    get_responses = iter(
        (
            {
                "tenant_id": "tenant-a",
                "allow_mp4": False,
                "exists": True,
                "revision": _policy_revision(1),
                "operation_id": _policy_operation(7),
            },
            {
                "tenant_id": "tenant-a",
                "allow_mp4": True,
                "exists": True,
                "revision": _policy_revision(2),
                "operation_id": _policy_operation(1),
            },
            {
                "tenant_id": "tenant-a",
                "allow_mp4": False,
                "exists": True,
                "revision": _policy_revision(3),
                "operation_id": _policy_operation(2),
            },
        )
    )

    class Api:
        async def tenant_admin_json(self, method, _path, *, json_body=None, **_kwargs):
            calls.append((method, json_body))
            if method == "GET":
                return next(get_responses)
            raise module.ExportProbeError("candidate_request_failed")

    state = module.ProbeState()
    state.tenant_id = "tenant-a"

    async def exercise():
        await module._configure_mp4_policy(Api(), state=state)
        await module._cleanup_resources(Api(), state)

    asyncio.run(exercise())

    assert calls == [
        ("GET", None),
        (
            "PUT",
            {
                "allow_mp4": True,
                "expected_revision": _policy_revision(1),
                "operation_id": _policy_operation(1),
            },
        ),
        ("GET", None),
        (
            "PUT",
            {
                "expected_revision": _policy_revision(2),
                "allow_mp4": False,
                "operation_id": _policy_operation(2),
            },
        ),
        ("GET", None),
    ]
    assert state.mp4_policy_configured is False


def test_mp4_policy_already_enabled_is_not_rewritten() -> None:
    module = _module()
    calls: list[tuple[str, str, object | None]] = []

    class Api:
        async def tenant_admin_json(
            self,
            method,
            path,
            *,
            json_body=None,
            **_kwargs,
        ):
            calls.append((method, path, json_body))
            return {
                "tenant_id": "tenant-a",
                "allow_mp4": True,
                "exists": True,
                "revision": _policy_revision(7),
                "operation_id": _policy_operation(7),
            }

    state = module.ProbeState()
    state.tenant_id = "tenant-a"

    async def exercise():
        await module._configure_mp4_policy(Api(), state=state)
        await module._cleanup_resources(Api(), state)

    asyncio.run(exercise())

    assert calls == [("GET", "/api/v1/classroom-export-policy", None)]
    assert state.mp4_policy_configured is False


def test_mp4_policy_restore_failure_stops_resource_cleanup() -> None:
    module = _module()

    class Api:
        async def tenant_admin_json(self, *_args, json_body=None, **_kwargs):
            return {
                "tenant_id": "tenant-a",
                "allow_mp4": False,
                "exists": True,
                "revision": _policy_revision(2),
                "operation_id": (json_body.get("operation_id") if json_body is not None else None),
            }

    state = module.ProbeState()
    state.tenant_id = "tenant-a"
    state.mp4_policy_original = False
    state.mp4_policy_original_exists = True
    state.mp4_policy_original_revision = _policy_revision(1)
    state.mp4_policy_original_operation_id = _policy_operation(1)
    state.mp4_policy_cleanup_revision = _policy_revision(2)
    state.mp4_policy_enable_operation_id = _policy_operation(2)
    state.mp4_policy_configured = True

    with pytest.raises(module.ExportProbeError, match="resource_cleanup_failed"):
        asyncio.run(module._cleanup_resources(Api(), state))


def test_mp4_policy_cleanup_does_not_retry_a_revision_conflict() -> None:
    module = _module()
    calls: list[tuple[str, object | None]] = []

    class Api:
        async def tenant_admin_json(self, method, _path, *, json_body=None, **_kwargs):
            calls.append((method, json_body))
            raise module.ExportProbeError("candidate_request_rejected")

    state = module.ProbeState()
    state.tenant_id = "tenant-a"
    state.mp4_policy_original = False
    state.mp4_policy_original_exists = True
    state.mp4_policy_original_revision = _policy_revision(1)
    state.mp4_policy_original_operation_id = _policy_operation(1)
    state.mp4_policy_cleanup_revision = _policy_revision(2)
    state.mp4_policy_enable_operation_id = _policy_operation(2)
    state.mp4_policy_configured = True

    with pytest.raises(module.ExportProbeError, match="candidate_request_rejected"):
        asyncio.run(module._cleanup_resources(Api(), state))

    assert calls[0] == (
        "PUT",
        {
            "expected_revision": _policy_revision(2),
            "allow_mp4": False,
            "operation_id": state.mp4_policy_restore_operation_id,
        },
    )
    assert calls[1:] == [
        (
            "GET",
            None,
        )
    ]


def test_mp4_policy_rejects_a_cross_tenant_response() -> None:
    module = _module()

    class Api:
        async def tenant_admin_json(self, *_args, **_kwargs):
            return {
                "tenant_id": "tenant-b",
                "allow_mp4": False,
                "exists": True,
                "revision": _policy_revision(1),
                "operation_id": None,
            }

    state = module.ProbeState()
    state.tenant_id = "tenant-a"

    with pytest.raises(module.ExportProbeError, match="export_policy_invalid"):
        asyncio.run(module._configure_mp4_policy(Api(), state=state))


def test_mp4_policy_rejects_a_non_digest_revision() -> None:
    module = _module()

    class Api:
        async def tenant_admin_json(self, *_args, **_kwargs):
            return {
                "tenant_id": "tenant-a",
                "allow_mp4": False,
                "exists": True,
                "revision": "revision-current",
                "operation_id": None,
            }

    state = module.ProbeState()
    state.tenant_id = "tenant-a"

    with pytest.raises(module.ExportProbeError, match="export_policy_invalid"):
        asyncio.run(module._configure_mp4_policy(Api(), state=state))


def test_run_probe_preserves_keyboard_interrupt_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    config = module.ProbeConfig(
        admin_token=module.SecretStr("secret-admin-token"),
        base_url="https://classroom.example.test",
        candidate=_candidate(),
        candidate_root=tmp_path,
        release_run={"runId": "run-a", "environmentId": "acceptance-a"},
        staging_dir=tmp_path,
        timeout_seconds=30,
        tenant_id="tenant-a",
    )

    class Api:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    async def cleanup_failed(*_args, **_kwargs):
        raise module.ExportProbeError("cleanup_failed")

    monkeypatch.setattr(module, "ExportApi", lambda *_args, **_kwargs: Api())
    monkeypatch.setattr(module, "_execute_export_probe", interrupted)
    monkeypatch.setattr(module, "_cleanup_resources", cleanup_failed)
    monkeypatch.setattr(module, "_cleanup_identity", cleanup_failed)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(module._run_export_probe(config))


def test_main_emits_a_stable_interrupt_without_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    module = _module()

    async def interrupted(_config):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_parse_args", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_load_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(module, "_run_export_probe", interrupted)

    assert module.main([]) == 130
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert captured.err == b"classroom_export_probe_interrupted\n"


def test_main_flushes_the_complete_report_before_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    report = b'{"schemaVersion":1}\n'

    class Buffer:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.flushed = False

        def write(self, body: bytes) -> int:
            self.writes.append(body)
            return len(body)

        def flush(self) -> None:
            self.flushed = True

    buffer = Buffer()
    stdout = SimpleNamespace(buffer=buffer)
    monkeypatch.setattr(module, "_parse_args", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_load_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(module, "_run_export_probe", lambda _config: _async_value(report))
    monkeypatch.setattr(module.sys, "stdout", stdout)

    assert module.main([]) == 0
    assert buffer.writes == [report]
    assert buffer.flushed is True


def test_invalid_arguments_emit_only_a_stable_error_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()

    assert module.main(["--profile", "unsupported"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "classroom_export_arguments_invalid\n"


def test_stdout_report_never_contains_credentials_or_tickets(
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    module = _module()
    report = {
        "schemaVersion": 1,
        "producer": "classroom-export-probe",
        "candidate": _candidate(),
        "releaseRun": {"runId": "run-a", "environmentId": "acceptance-a"},
        "baseUrl": "https://classroom.example.test",
        "tenantId": "tenant-a",
        "classroomVersionId": "version-a",
        "documentSha256": "d" * 64,
        "exports": {},
    }
    body = (json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n").encode()
    monkeypatch.setattr(module, "_load_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(module, "_parse_args", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_run_export_probe", lambda _config: _async_value(body))

    assert module.main([]) == 0
    captured = capsysbinary.readouterr()
    assert captured.out == body
    assert captured.err == b""
    lowered = captured.out.lower()
    for forbidden in (b"token", b"cookie", b"password", b"ticket", b"secret"):
        assert forbidden not in lowered


async def _async_value(value: bytes) -> bytes:
    return value


def test_download_open_is_exclusive_and_never_overwrites(tmp_path: Path) -> None:
    module = _module()
    target = tmp_path / "classroom.mp4"
    target.write_bytes(b"existing")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "video/mp4",
                "Content-Disposition": "attachment; filename*=UTF-8''classroom.mp4",
            },
            content=b"replacement",
        )

    async def exercise():
        async with module.ExportApi(
            "https://classroom.example.test",
            "secret-admin-token",
            transport=httpx.MockTransport(handler),
        ) as api:
            await api.tenant_identity_download(
                "/api/v1/classroom-exports/export-a/download",
                identity=_identity(module),
                tenant_id="tenant-a",
                target=target,
                expected_filename="classroom.mp4",
                expected_content_type="video/mp4",
                max_bytes=1024,
            )

    with pytest.raises(module.ExportProbeError, match="export_staging_conflict"):
        asyncio.run(exercise())
    assert target.read_bytes() == b"existing"


def test_no_real_network_or_subprocess_primitives_are_referenced() -> None:
    source = (ROOT / "scripts" / "classroom_export_probe.py").read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "requests." not in source
    assert "os.system" not in source
