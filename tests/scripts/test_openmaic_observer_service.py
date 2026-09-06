from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gzip
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "openmaic_observer_service.py"
AUTH_TOKEN = "observer-run-token-" + "9" * 48
CANDIDATE_TOKEN = "candidate-health-token-" + "8" * 48
CANDIDATE_ADMIN_HEALTH_URL = "https://candidate.example.test/api/v1/system/teaching-health"
CANDIDATE_OBSERVER_HEALTH_URL = CANDIDATE_ADMIN_HEALTH_URL + "/observer-canary"
CANDIDATE_SHA256 = "a" * 64
IMAGE_LOCK_SHA256 = "b" * 64
OBSERVER_PUBLIC_KEY_SPKI_SHA256 = "c" * 64


def _module():
    name = "openmaic_observer_service_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _config(module, *, state_limit: int = 64):
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    return module.ObserverServiceConfig(
        auth_token=module.SecretStr(AUTH_TOKEN),
        auth_expires_at=now + timedelta(minutes=10),
        candidate_health_token=module.SecretStr(CANDIDATE_TOKEN),
        candidate_health_url=CANDIDATE_OBSERVER_HEALTH_URL,
        candidate_sha256=CANDIDATE_SHA256,
        environment_id="environment-dedicated-01",
        image_lock_sha256=IMAGE_LOCK_SHA256,
        observer_id="shared-ingress-observer-environment-dedicated-01",
        observer_public_key_spki_sha256=OBSERVER_PUBLIC_KEY_SPKI_SHA256,
        observer_url="https://observer.example.test",
        release_run_id="run-dedicated-01",
        shared_ingress_control_url=("https://shared-ingress.example.test/v1/control-canaries"),
        shared_ingress_upstream_url="https://openmaic-upstream.example.test",
        state_limit=state_limit,
    )


@pytest.mark.asyncio
async def test_candidate_health_rejects_another_release_candidate_digest() -> None:
    module = _module()
    app = module.OpenMAICObserverService(
        _config(module),
        now=lambda: datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
    )
    response = module.HTTPResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "candidateSha256": "9" * 64,
                "components": {
                    "openmaic_shared": {
                        "status": "healthy",
                        "service": "openmaic",
                        "mode": "shared",
                    }
                },
            }
        ).encode(),
    )

    assert await app._valid_candidate_health(response) is False


async def _request(
    app,
    method: str,
    path: str,
    *,
    host: str,
    token: str = AUTH_TOKEN,
    body: bytes = b"",
    content_type: bytes | None = None,
    query_string: bytes = b"",
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    headers = [(b"host", host.encode()), (b"authorization", f"Bearer {token}".encode())]
    if content_type is not None:
        headers.append((b"content-type", content_type))
    headers.extend(extra_headers)
    events = iter(({"type": "http.request", "body": body, "more_body": False},))
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return next(events)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query_string,
            "headers": headers,
        },
        receive,
        send,
    )
    start, response = sent
    response_headers = {
        key.decode("ascii"): value.decode("ascii") for key, value in start["headers"]
    }
    return int(start["status"]), response_headers, bytes(response["body"])


def _control_body(canary_id: str) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 1,
            "runId": "run-dedicated-01",
            "environmentId": "environment-dedicated-01",
            "canaryId": canary_id,
            "kind": "openmaic-shared-ingress-control",
        },
        separators=(",", ":"),
    ).encode()


def _proxy_config(module):
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    return module.ObserverServiceConfig(
        auth_token=module.SecretStr(AUTH_TOKEN),
        auth_expires_at=now + timedelta(minutes=10),
        candidate_health_token=module.SecretStr(CANDIDATE_TOKEN),
        candidate_health_url=CANDIDATE_OBSERVER_HEALTH_URL,
        candidate_sha256=CANDIDATE_SHA256,
        environment_id="environment-dedicated-01",
        image_lock_sha256=IMAGE_LOCK_SHA256,
        observer_id="shared-ingress-observer-environment-dedicated-01",
        observer_public_key_spki_sha256=OBSERVER_PUBLIC_KEY_SPKI_SHA256,
        observer_url="https://observer.example.test",
        release_run_id="run-dedicated-01",
        shared_ingress_control_url=("https://shared-ingress.example.test/v1/control-canaries"),
        shared_ingress_upstream_url="https://openmaic-upstream.example.test",
        state_limit=64,
    )


def _service_with_candidate_proxy(module, config, *, now):
    app = None

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        if url == config.candidate_health_url:
            assert app is not None
            challenge = headers["X-YFeiSTAI-Observer-Canary"]
            status, _, _ = await _request(
                app,
                "GET",
                "/api/yfeistai/v1/health",
                host="shared-ingress.example.test",
                extra_headers=((b"x-yfeistai-observer-canary", challenge.encode("ascii")),),
            )
            assert status == 200
            return module.HTTPResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "candidateSha256": CANDIDATE_SHA256,
                        "components": {
                            "openmaic_shared": {
                                "status": "healthy",
                                "service": "openmaic",
                                "mode": "shared",
                            }
                        },
                    }
                ).encode(),
            )
        return module.HTTPResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"service":"openmaic"}',
        )

    app = module.OpenMAICObserverService(config, now=now, requester=requester)
    return app


def test_observer_service_control_is_atomic_idempotent_and_read_only() -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    app = _service_with_candidate_proxy(module, _config(module), now=lambda: now)

    async def scenario() -> None:
        status, _, before_body = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert status == 200
        assert json.loads(before_body)["requestCount"] == 0

        first = _control_body("canary-01")
        status, _, _ = await _request(
            app,
            "POST",
            "/v1/control-canaries",
            host="shared-ingress.example.test",
            body=first,
            content_type=b"application/json",
        )
        assert status == 202
        status, _, _ = await _request(
            app,
            "POST",
            "/v1/control-canaries",
            host="shared-ingress.example.test",
            body=first,
            content_type=b"application/json",
        )
        assert status == 202

        await asyncio.gather(
            *(
                _request(
                    app,
                    "POST",
                    "/v1/control-canaries",
                    host="shared-ingress.example.test",
                    body=_control_body(f"canary-{index:02d}"),
                    content_type=b"application/json",
                )
                for index in range(2, 22)
            )
        )
        status, _, after_body = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert status == 200
        after = json.loads(after_body)
        assert after == {
            "schemaVersion": 1,
            "runId": "run-dedicated-01",
            "environmentId": "environment-dedicated-01",
            "observationId": "shared-ingress-run-dedicated-01",
            "requestCount": 21,
            "lastCanaryId": after["lastCanaryId"],
        }
        assert after["lastCanaryId"] in {f"canary-{index:02d}" for index in range(2, 22)}

        _, _, unchanged_body = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert unchanged_body == after_body

    asyncio.run(scenario())


def test_observer_service_rejects_auth_origin_method_media_binding_and_size() -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    app = module.OpenMAICObserverService(_config(module), now=lambda: now)

    async def scenario() -> None:
        status, _, response = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
            token="wrong-" + "x" * 48,
        )
        assert status == 401
        assert AUTH_TOKEN.encode() not in response

        assert (
            await _request(
                app,
                "GET",
                "/v1/observations/run-dedicated-01",
                host="shared-ingress.example.test",
            )
        )[0] == 404
        assert (
            await _request(
                app,
                "POST",
                "/v1/observations/run-dedicated-01",
                host="observer.example.test",
            )
        )[0] == 405
        assert (
            await _request(
                app,
                "GET",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
            )
        )[0] == 405
        assert (
            await _request(
                app,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=_control_body("canary-media"),
                content_type=b"text/plain",
            )
        )[0] == 415

        drifted = json.loads(_control_body("canary-drift"))
        drifted["environmentId"] = "other-environment"
        assert (
            await _request(
                app,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=json.dumps(drifted).encode(),
                content_type=b"application/json",
            )
        )[0] == 400
        assert (
            await _request(
                app,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=b"{" + b"x" * 5000,
                content_type=b"application/json",
            )
        )[0] == 413

    asyncio.run(scenario())


def test_observer_service_expires_auth_and_bounds_replay_state() -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    expired = module.OpenMAICObserverService(
        _config(module),
        now=lambda: now + timedelta(minutes=11),
    )
    bounded = _service_with_candidate_proxy(
        module,
        _config(module, state_limit=1),
        now=lambda: now,
    )

    async def scenario() -> None:
        assert (
            await _request(
                expired,
                "GET",
                "/v1/observations/run-dedicated-01",
                host="observer.example.test",
            )
        )[0] == 401
        first = _control_body("canary-first")
        assert (
            await _request(
                bounded,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=first,
                content_type=b"application/json",
            )
        )[0] == 202
        assert (
            await _request(
                bounded,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=first,
                content_type=b"application/json",
            )
        )[0] == 202
        assert (
            await _request(
                bounded,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=_control_body("canary-second"),
                content_type=b"application/json",
            )
        )[0] == 429
        _, _, observation = await _request(
            bounded,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert json.loads(observation)["requestCount"] == 1

    asyncio.run(scenario())


def test_observer_service_loads_restricted_secret_file_without_following_links(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    secret_path = (tmp_path / "observer-auth.token").resolve()
    secret_path.write_text(AUTH_TOKEN + "\n", encoding="utf-8")
    candidate_secret_path = (tmp_path / "candidate-health.token").resolve()
    candidate_secret_path.write_text(CANDIDATE_TOKEN + "\n", encoding="utf-8")
    if os.name == "posix":
        secret_path.chmod(0o600)
        candidate_secret_path.chmod(0o600)
    environment = {
        "YFEISTAI_RELEASE_RUN_ID": "run-dedicated-01",
        "YFEISTAI_ENVIRONMENT_ID": "environment-dedicated-01",
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ID": ("shared-ingress-observer-environment-dedicated-01"),
        "YFEISTAI_SHARED_INGRESS_OBSERVER_URL": "https://observer.example.test",
        "YFEISTAI_SHARED_INGRESS_CONTROL_URL": (
            "https://shared-ingress.example.test/v1/control-canaries"
        ),
        "YFEISTAI_SHARED_INGRESS_AUTH_TOKEN_FILE": str(secret_path),
        "YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT": "2026-08-31T00:10:00Z",
        "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE": str(candidate_secret_path),
        "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL": (CANDIDATE_OBSERVER_HEALTH_URL),
        "YFEISTAI_SHARED_INGRESS_UPSTREAM_URL": "https://openmaic-upstream.example.test",
        "YFEISTAI_CANDIDATE_SHA256": CANDIDATE_SHA256,
        "YFEISTAI_IMAGE_LOCK_SHA256": IMAGE_LOCK_SHA256,
        "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256": OBSERVER_PUBLIC_KEY_SPKI_SHA256,
    }

    config = module.load_config(environment, now=lambda: now)

    assert config.auth_token.get_secret_value() == AUTH_TOKEN
    assert AUTH_TOKEN not in repr(config)
    changed = dict(environment)
    changed["YFEISTAI_SHARED_INGRESS_AUTH_TOKEN_FILE"] = "relative.token"
    with pytest.raises(module.OpenMAICObserverServiceError, match="auth_secret_invalid"):
        module.load_config(changed, now=lambda: now)
    alias = tmp_path / "observer-auth-alias.token"
    try:
        alias.symlink_to(secret_path)
    except OSError:
        return
    changed["YFEISTAI_SHARED_INGRESS_AUTH_TOKEN_FILE"] = str(alias)
    with pytest.raises(module.OpenMAICObserverServiceError, match="auth_secret_invalid"):
        module.load_config(changed, now=lambda: now)


def test_observer_secret_loader_rejects_same_size_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    secret_path = (tmp_path / "observer-auth.token").resolve()
    secret_path.write_text(AUTH_TOKEN + "\n", encoding="utf-8")
    if os.name == "posix":
        secret_path.chmod(0o600)
    real_fstat = module.os.fstat
    calls = 0

    def drifting_fstat(descriptor):
        nonlocal calls
        calls += 1
        details = real_fstat(descriptor)
        if calls != 2:
            return details
        return SimpleNamespace(
            st_dev=details.st_dev,
            st_ino=details.st_ino,
            st_size=details.st_size,
            st_mtime_ns=details.st_mtime_ns + 1,
            st_ctime_ns=details.st_ctime_ns,
            st_mode=details.st_mode,
            st_uid=details.st_uid,
        )

    monkeypatch.setattr(module.os, "fstat", drifting_fstat)

    with pytest.raises(module.OpenMAICObserverServiceError, match="auth_secret_invalid"):
        module._load_auth_secret(str(secret_path))


def test_observer_secret_loader_binds_full_lstat_to_open_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    secret_path = (tmp_path / "observer-auth.token").resolve()
    secret_path.write_text(AUTH_TOKEN, encoding="ascii")
    if module.os.name == "posix":
        secret_path.chmod(0o600)
    real_lstat = module.Path.lstat
    calls = 0

    def drifting_lstat(path):
        nonlocal calls
        details = real_lstat(path)
        if module.Path(path) != secret_path:
            return details
        calls += 1
        if calls != 2:
            return details
        return SimpleNamespace(
            st_dev=details.st_dev,
            st_ino=details.st_ino,
            st_size=details.st_size,
            st_mtime_ns=details.st_mtime_ns + 1,
            st_ctime_ns=details.st_ctime_ns,
            st_mode=details.st_mode,
            st_uid=details.st_uid,
        )

    monkeypatch.setattr(module.Path, "lstat", drifting_lstat)

    with pytest.raises(module.OpenMAICObserverServiceError, match="auth_secret_invalid"):
        module._load_auth_secret(str(secret_path))


def test_observer_and_candidate_tokens_require_distinct_files_identities_and_values(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    observer_path = (tmp_path / "observer.token").resolve()
    candidate_path = (tmp_path / "candidate.token").resolve()
    observer_path.write_text(AUTH_TOKEN, encoding="ascii")
    candidate_path.write_text(AUTH_TOKEN, encoding="ascii")
    if os.name == "posix":
        observer_path.chmod(0o600)
        candidate_path.chmod(0o600)
    environment = {
        "YFEISTAI_RELEASE_RUN_ID": "run-dedicated-01",
        "YFEISTAI_ENVIRONMENT_ID": "environment-dedicated-01",
        "YFEISTAI_SHARED_INGRESS_OBSERVER_ID": "observer-dedicated-01",
        "YFEISTAI_SHARED_INGRESS_OBSERVER_URL": "https://observer.example.test",
        "YFEISTAI_SHARED_INGRESS_CONTROL_URL": (
            "https://shared-ingress.example.test/v1/control-canaries"
        ),
        "YFEISTAI_SHARED_INGRESS_AUTH_TOKEN_FILE": str(observer_path),
        "YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT": "2026-08-31T00:10:00Z",
        "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE": str(candidate_path),
        "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL": CANDIDATE_OBSERVER_HEALTH_URL,
        "YFEISTAI_SHARED_INGRESS_UPSTREAM_URL": "https://openmaic-upstream.example.test",
    }

    with pytest.raises(module.OpenMAICObserverServiceError, match="service_config_invalid"):
        module.load_config(environment, now=lambda: now)

    candidate_path.write_text(CANDIDATE_TOKEN, encoding="ascii")
    environment["YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE"] = str(observer_path)
    with pytest.raises(module.OpenMAICObserverServiceError, match="service_config_invalid"):
        module.load_config(environment, now=lambda: now)


def test_direct_service_config_rejects_equal_observer_and_candidate_tokens() -> None:
    module = _module()
    config = replace(
        _config(module),
        candidate_health_token=module.SecretStr(AUTH_TOKEN),
    )

    with pytest.raises(module.OpenMAICObserverServiceError, match="service_config_invalid"):
        module.OpenMAICObserverService(config)


def test_candidate_health_rejects_the_platform_admin_route_as_a_fallback() -> None:
    module = _module()
    config = replace(
        _config(module),
        candidate_health_url=CANDIDATE_ADMIN_HEALTH_URL,
    )

    with pytest.raises(module.OpenMAICObserverServiceError, match="service_config_invalid"):
        module.OpenMAICObserverService(config)


def test_observer_attestation_is_canonical_atomic_secret_free_and_no_replace(
    tmp_path: Path,
) -> None:
    module = _module()
    observed_at = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    config = _config(module)
    target = tmp_path / "openmaic-shared-ingress-observer-attestation.json"

    body = module.publish_observer_attestation(
        config,
        target,
        observed_at=observed_at,
    )

    assert target.read_bytes() == body
    assert AUTH_TOKEN.encode() not in body
    assert json.loads(body) == {
        "schemaVersion": 3,
        "producer": "openmaic-shared-ingress-observer",
        "releaseRun": {
            "runId": "run-dedicated-01",
            "environmentId": "environment-dedicated-01",
        },
        "candidateBinding": {
            "candidateSha256": CANDIDATE_SHA256,
            "imageLockSha256": IMAGE_LOCK_SHA256,
        },
        "attestationChallenge": module.openmaic_observer_attestation_challenge(
            OBSERVER_PUBLIC_KEY_SPKI_SHA256
        ),
        "observedAt": "2026-08-31T00:00:00Z",
        "observer": {
            "observerId": "shared-ingress-observer-environment-dedicated-01",
            "observerUrl": "https://observer.example.test",
            "sharedIngressControlUrl": ("https://shared-ingress.example.test/v1/control-canaries"),
            "candidateHealthUrl": CANDIDATE_OBSERVER_HEALTH_URL,
            "sharedIngressUpstreamUrl": "https://openmaic-upstream.example.test",
            "internalProxyUrl": "http://openmaic-shared-ingress-observer:8000",
            "authExpiresAt": "2026-08-31T00:10:00Z",
        },
    }
    assert list(tmp_path.glob(".*.staged")) == []
    with pytest.raises(module.OpenMAICObserverServiceError, match="attestation_exists"):
        module.publish_observer_attestation(config, target, observed_at=observed_at)
    assert target.read_bytes() == body


def test_observer_attestation_canonical_body_binds_normalized_internal_proxy_url() -> None:
    module = _module()
    observed_at = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    config = replace(
        _config(module),
        internal_proxy_url="http://OPENMAIC-SHARED-INGRESS-OBSERVER:8000",
    )

    body = module.canonical_observer_attestation(config, observed_at=observed_at)
    report = json.loads(body)

    assert report["observer"]["internalProxyUrl"] == (
        "http://openmaic-shared-ingress-observer:8000"
    )
    assert body == module.canonical_openmaic_shared_ingress_observer_attestation(report)

    conflicting = replace(
        _config(module),
        observer_url="https://proxy.example.test:8000",
        internal_proxy_url="http://proxy.example.test:8000",
    )
    with pytest.raises(module.OpenMAICObserverServiceError, match="service_config_invalid"):
        module.OpenMAICObserverService(conflicting)


def test_observer_attestation_rejects_copy_instead_of_same_inode_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    target = tmp_path / "observer-attestation.json"

    def copy_instead_of_link(source, destination, **kwargs) -> None:
        source_fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
        try:
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs.get("dst_dir_fd"),
            )
            try:
                while chunk := os.read(source_fd, 8192):
                    os.write(destination_fd, chunk)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)

    monkeypatch.setattr(module.os, "link", copy_instead_of_link)

    with pytest.raises(
        module.OpenMAICObserverServiceError,
        match="attestation_committed_but_unverified",
    ):
        module.publish_observer_attestation(
            _config(module),
            target,
            observed_at=datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
        )


def test_observer_attestation_classifies_published_metadata_failure_as_committed_but_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    target = tmp_path / "observer-attestation.json"
    linked = False
    metadata_failed = False
    real_link = module.os.link

    def link_then_mark_published(*args, **kwargs) -> None:
        nonlocal linked
        real_link(*args, **kwargs)
        linked = True

    monkeypatch.setattr(module.os, "link", link_then_mark_published)
    if module.os.name == "posix":
        real_stat = module.os.stat

        def fail_first_published_stat(path, *args, **kwargs):
            nonlocal metadata_failed
            if (
                linked
                and not metadata_failed
                and path == target.name
                and kwargs.get("dir_fd") is not None
                and kwargs.get("follow_symlinks") is False
            ):
                metadata_failed = True
                raise OSError("published-stat-failure")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(module.os, "stat", fail_first_published_stat)
    else:
        path_type = type(target)
        real_lstat = path_type.lstat

        def fail_first_published_lstat(path, *args, **kwargs):
            nonlocal metadata_failed
            if linked and not metadata_failed and path == target:
                metadata_failed = True
                raise OSError("published-lstat-failure")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(path_type, "lstat", fail_first_published_lstat)

    with pytest.raises(
        module.OpenMAICObserverServiceError,
        match="attestation_committed_but_unverified",
    ):
        module.publish_observer_attestation(
            _config(module),
            target,
            observed_at=datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
        )

    assert linked is True
    assert metadata_failed is True
    assert target.exists()
    assert tuple(tmp_path.glob(".*.staged")) == ()


def test_parent_descriptor_close_helper_does_not_retry_after_close_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    descriptor_path = tmp_path / "parent-descriptor"
    descriptor_path.write_bytes(b"descriptor")
    descriptor = os.open(descriptor_path, os.O_RDONLY)
    real_close = module.os.close
    close_attempts: list[int] = []

    def close_then_raise(value: int) -> None:
        close_attempts.append(value)
        real_close(value)
        raise OSError("parent-close-after-release")

    monkeypatch.setattr(module.os, "close", close_then_raise)

    with pytest.raises(OSError, match="parent-close-after-release"):
        module._close_parent_descriptor(descriptor)

    assert close_attempts == [descriptor]
    replacement_descriptor = os.open(descriptor_path, os.O_RDONLY)
    real_close(replacement_descriptor)


def test_observer_attestation_accepts_parent_close_failure_after_durable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    config = _config(module)
    observed_at = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    target = tmp_path / "observer-attestation.json"
    expected = module.canonical_observer_attestation(config, observed_at=observed_at)
    real_close = module.os.close
    close_attempts: list[int | None] = []

    def close_after_commit_once(descriptor: int | None) -> None:
        close_attempts.append(descriptor)
        if descriptor is not None:
            real_close(descriptor)
        if len(close_attempts) == 1:
            raise OSError("parent-close-after-release")

    monkeypatch.setattr(module, "_close_parent_descriptor", close_after_commit_once)

    assert (
        module.publish_observer_attestation(
            config,
            target,
            observed_at=observed_at,
        )
        == expected
    )
    assert target.read_bytes() == expected
    assert tuple(tmp_path.glob(".*.staged")) == ()
    with pytest.raises(module.OpenMAICObserverServiceError, match="attestation_exists"):
        module.publish_observer_attestation(
            config,
            target,
            observed_at=observed_at,
        )
    assert len(close_attempts) == 2


def _exception_tree_messages(error: BaseException) -> tuple[str, ...]:
    messages: list[str] = []
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        messages.append(str(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
    return tuple(messages)


def test_observer_attestation_preserves_publish_and_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    target = tmp_path / "observer-attestation.json"
    real_unlink = module.os.unlink

    def fail_publish(*_args, **_kwargs) -> None:
        raise OSError("primary-publish-failure")

    def fail_staged_cleanup(path, *args, **kwargs) -> None:
        if str(path).endswith(".staged"):
            raise OSError("staged-cleanup-failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", fail_publish)
    monkeypatch.setattr(module.os, "unlink", fail_staged_cleanup)

    try:
        with pytest.raises(BaseExceptionGroup) as caught:
            module.publish_observer_attestation(
                _config(module),
                target,
                observed_at=datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
            )

        messages = "\n".join(_exception_tree_messages(caught.value))
        assert "attestation_publish_failed" in messages
        assert "primary-publish-failure" in messages
        assert "attestation_cleanup_failed" in messages
        assert "staged-cleanup-failure" in messages
    finally:
        for staged in tmp_path.glob(".*.staged"):
            real_unlink(staged)


def test_observer_attestation_reports_committed_cleanup_incomplete_with_real_residual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    target = tmp_path / "observer-attestation.json"
    real_unlink = module.os.unlink

    def fail_staged_cleanup(path, *args, **kwargs) -> None:
        if str(path).endswith(".staged"):
            raise OSError("staged-cleanup-failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "unlink", fail_staged_cleanup)

    try:
        with pytest.raises(
            module.OpenMAICObserverServiceError,
            match="attestation_committed_but_cleanup_incomplete",
        ) as caught:
            module.publish_observer_attestation(
                _config(module),
                target,
                observed_at=datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
            )

        staged = tuple(tmp_path.glob(".*.staged"))
        assert len(staged) == 1
        assert target.exists()
        messages = "\n".join(_exception_tree_messages(caught.value))
        assert "staged-cleanup-failure" in messages
        assert str(staged[0].resolve()) in messages
    finally:
        for path in (*tmp_path.glob(".*.staged"), target):
            try:
                real_unlink(path)
            except FileNotFoundError:
                pass


def test_control_canary_traverses_candidate_shared_route_and_same_proxy_counter() -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    app = None
    calls: list[tuple[str, str]] = []

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        calls.append((method, url))
        if url == CANDIDATE_OBSERVER_HEALTH_URL:
            challenge = headers.pop("X-YFeiSTAI-Observer-Canary")
            assert headers == {
                "Accept": "application/json",
                "Authorization": "Bearer " + CANDIDATE_TOKEN,
            }
            assert len(challenge) >= 48
            assert app is not None
            status, _, _ = await _request(
                app,
                "GET",
                "/api/yfeistai/v1/health",
                host="shared-ingress.example.test",
                extra_headers=((b"x-yfeistai-observer-canary", challenge.encode("ascii")),),
            )
            assert status == 200
            return module.HTTPResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "candidateSha256": CANDIDATE_SHA256,
                        "components": {
                            "openmaic_shared": {
                                "status": "healthy",
                                "service": "openmaic",
                                "mode": "shared",
                            }
                        },
                    }
                ).encode(),
            )
        assert url == "https://openmaic-upstream.example.test/api/yfeistai/v1/health"
        assert method == "GET"
        assert content == b""
        assert "x-yfeistai-observer-canary" not in headers
        assert timeout_seconds > 0
        assert max_response_bytes > 0
        return module.HTTPResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"service":"openmaic"}',
        )

    app = module.OpenMAICObserverService(
        _proxy_config(module),
        now=lambda: now,
        requester=requester,
    )

    async def scenario() -> None:
        status, _, response = await _request(
            app,
            "POST",
            "/v1/control-canaries",
            host="shared-ingress.example.test",
            body=_control_body("control-canary-01"),
            content_type=b"application/json",
            extra_headers=(
                (b"forwarded", b"host=attacker.example.test"),
                (b"x-forwarded-host", b"attacker.example.test"),
            ),
        )
        assert status == 202
        assert json.loads(response) == {"accepted": True, "canaryId": "control-canary-01"}
        _, _, observation = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert json.loads(observation)["requestCount"] == 1
        assert json.loads(observation)["lastCanaryId"] == "control-canary-01"

    asyncio.run(scenario())
    assert calls == [
        ("GET", CANDIDATE_OBSERVER_HEALTH_URL),
        ("GET", "https://openmaic-upstream.example.test/api/yfeistai/v1/health"),
    ]


@pytest.mark.parametrize(
    "random_prefix",
    [
        pytest.param("-", id="leading-hyphen"),
        pytest.param("_", id="leading-underscore"),
        pytest.param("A", id="leading-letter-control"),
    ],
)
def test_control_canary_satisfies_real_health_receiver_contracts(
    random_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from deeptutor.api.routers import teaching_health
    from deeptutor.teaching.health_probes import ACTIVE_PROBE_COMPONENTS, ActiveHealthProbeService
    from deeptutor.teaching.openmaic.client import OpenMAICContractHealthClient

    module = _module()
    config = _proxy_config(module)
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _size: random_prefix + "7" * 63)
    monkeypatch.setattr(teaching_health, "_observer_health_token", lambda: CANDIDATE_TOKEN)
    monkeypatch.setenv("YFEISTAI_CANDIDATE_SHA256", CANDIDATE_SHA256)
    candidate_app = FastAPI()
    candidate_app.include_router(teaching_health.router)
    candidate_responses: list[tuple[int, object]] = []
    upstream_calls = 0
    health_document = {
        "service": "openmaic",
        "upstreamCommit": "0cf2a330411681190e89f48e20f305345ff99f87",
        "appVersion": "0.3.1",
        "contractVersions": ["1.0"],
        "capabilities": ["outline", "content", "micro", "export", "cancel", "artifact-manifest"],
        "exportFormats": ["classroom_zip", "pptx", "offline_html", "mp4"],
    }

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        nonlocal upstream_calls
        if url == CANDIDATE_OBSERVER_HEALTH_URL:
            async with module.httpx.AsyncClient(
                transport=module.httpx.ASGITransport(app=candidate_app),
                trust_env=False,
            ) as candidate_http:
                response = await candidate_http.request(
                    method, url, headers=headers, content=content
                )
            candidate_responses.append((response.status_code, response.json()))
            return module.HTTPResponse(
                response.status_code,
                dict(response.headers),
                response.content,
            )
        assert url == "https://openmaic-upstream.example.test/api/yfeistai/v1/health"
        assert method == "GET"
        assert content == b""
        assert "x-yfeistai-observer-canary" not in headers
        upstream_calls += 1
        return module.HTTPResponse(
            200,
            {"content-type": "application/json"},
            json.dumps(health_document).encode(),
        )

    app = module.OpenMAICObserverService(
        config,
        now=lambda: datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
        requester=requester,
    )

    async def scenario() -> None:
        async def unrelated_probe() -> None:
            pass

        async with module.httpx.AsyncClient(
            transport=module.httpx.ASGITransport(app=app),
            trust_env=False,
        ) as proxy_http:
            client = OpenMAICContractHealthClient(proxy_http, base_url=config.internal_proxy_url)
            # Isolate route/database composition, not either canary receiver contract.
            probes = dict.fromkeys(ACTIVE_PROBE_COMPONENTS, unrelated_probe)
            probes["openmaic_shared"] = client.assert_compatible
            active_probes = ActiveHealthProbeService(probes)
            candidate_app.dependency_overrides[teaching_health.get_active_health_probe_service] = (
                lambda: active_probes
            )
            status, _, response = await _request(
                app,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=_control_body("receiver-contract-01"),
                content_type=b"application/json",
            )

        assert status == 202, (json.loads(response), candidate_responses)
        assert json.loads(response) == {"accepted": True, "canaryId": "receiver-contract-01"}
        assert candidate_responses == [
            (
                200,
                {
                    "candidateSha256": CANDIDATE_SHA256,
                    "components": {
                        "openmaic_shared": {
                            "status": "healthy",
                            "service": "openmaic",
                            "mode": "shared",
                        }
                    },
                },
            )
        ]
        status, _, observation = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert status == 200
        assert json.loads(observation)["requestCount"] == 1
        assert json.loads(observation)["lastCanaryId"] == "receiver-contract-01"

    asyncio.run(scenario())
    assert upstream_calls == 1


def test_internal_proxy_serves_real_signed_client_health_and_deduplicates_observation() -> None:
    from deeptutor.teaching.openmaic.auth import ServiceRequest, signed_service_headers
    from deeptutor.teaching.openmaic.client import OpenMAICClient

    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    config = _proxy_config(module)
    service_secret = module.SecretStr("internal-health-service-secret")
    signed_headers = signed_service_headers(
        ServiceRequest(
            method="GET",
            path="/api/yfeistai/v1/health",
            tenant_id="tenant-dedicated-01",
            job_id="health",
            timestamp=int(now.timestamp()),
            idempotency_key="",
            body=b"",
        ),
        service_secret,
    )
    health_document = {
        "service": "openmaic",
        "upstreamCommit": "0cf2a330411681190e89f48e20f305345ff99f87",
        "appVersion": "0.3.1",
        "contractVersions": ["1.0"],
        "capabilities": ["outline", "content", "micro", "export", "cancel", "artifact-manifest"],
        "exportFormats": ["classroom_zip", "pptx", "offline_html", "mp4"],
    }
    forwarded = 0

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        nonlocal forwarded
        forwarded += 1
        assert method == "GET"
        assert url == "https://openmaic-upstream.example.test/api/yfeistai/v1/health"
        assert headers == {"accept": "*/*", **signed_headers}
        assert content == b""
        assert timeout_seconds == 20.0
        assert max_response_bytes == module._MAX_PROXY_RESPONSE_BYTES
        return module.HTTPResponse(
            200,
            {"content-type": "application/json"},
            json.dumps(health_document).encode(),
        )

    app = module.OpenMAICObserverService(config, now=lambda: now, requester=requester)

    async def scenario() -> None:
        async with module.httpx.AsyncClient(transport=module.httpx.ASGITransport(app=app)) as http:
            client = OpenMAICClient(
                http,
                base_url=config.internal_proxy_url,
                tenant_id="tenant-dedicated-01",
                route_id="shared-route-01",
                service_secret=service_secret,
                now_seconds=lambda: now.timestamp(),
            )
            health = await client.health()
            assert health.service == "openmaic"
            assert health.upstream_commit == health_document["upstreamCommit"]
            assert await client.health() == health
        status, _, observation = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert status == 200
        assert json.loads(observation)["requestCount"] == 1
        assert json.loads(observation)["lastCanaryId"] is None

    asyncio.run(scenario())
    assert forwarded == 2


@pytest.mark.parametrize(
    ("method", "path", "query", "expected_status"),
    [
        ("GET", "/healthz", b"", 200),
        ("POST", "/healthz", b"", 404),
        ("GET", "/healthz", b"unexpected=1", 404),
        ("GET", "/v1/observations/run-dedicated-01", b"", 404),
        ("POST", "/v1/control-canaries", b"", 404),
        ("GET", "/unknown", b"", 404),
        ("GET", "/api/yfeistai/v10/health", b"", 404),
    ],
)
def test_internal_proxy_keeps_probe_and_control_authorities_isolated(
    method: str, path: str, query: bytes, expected_status: int
) -> None:
    module = _module()

    async def requester(**kwargs):
        raise AssertionError("internal probe and control paths must not reach upstream")

    app = module.OpenMAICObserverService(
        _proxy_config(module),
        now=lambda: datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
        requester=requester,
    )
    status, _, body = asyncio.run(
        _request(
            app,
            method,
            path,
            host="openmaic-shared-ingress-observer:8000",
            body=_control_body("internal-control-must-not-run"),
            content_type=b"application/json",
            query_string=query,
        )
    )
    assert status == expected_status
    assert json.loads(body) == (
        {"status": "healthy"} if expected_status == 200 else {"error": "not_found"}
    )


@pytest.mark.parametrize(
    "host", ["shared-ingress.example.test", "openmaic-shared-ingress-observer:8000"]
)
def test_real_shared_fallback_uses_same_counter_and_ambiguous_headers_fail_closed(
    host: str,
) -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    forwarded: list[tuple[str, str, bytes]] = []

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        forwarded.append((method, url, content))
        return module.HTTPResponse(
            status_code=202,
            headers={"content-type": "application/json"},
            body=b'{"accepted":true}',
        )

    app = module.OpenMAICObserverService(
        _proxy_config(module),
        now=lambda: now,
        requester=requester,
    )
    signed_headers = (
        (b"x-yfeistai-tenant-id", b"tenant-dedicated-01"),
        (b"x-yfeistai-job-id", b"job-fallback-01"),
        (b"x-yfeistai-timestamp", b"1788134400"),
        (b"x-yfeistai-idempotency-key", b"fallback-attempt-01"),
        (b"x-yfeistai-signature", b"a" * 64),
    )

    async def scenario() -> None:
        status, _, _ = await _request(
            app,
            "POST",
            "/api/yfeistai/v1/jobs",
            host=host,
            body=b'{"jobId":"job-fallback-01"}',
            content_type=b"application/json",
            extra_headers=signed_headers,
        )
        assert status == 202
        _, _, observation = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert json.loads(observation)["requestCount"] == 1

        duplicate_authorization = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
            extra_headers=((b"authorization", f"Bearer {AUTH_TOKEN}".encode()),),
        )
        assert duplicate_authorization[0] == 400
        duplicate_host = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
            extra_headers=((b"host", b"observer.example.test"),),
        )
        assert duplicate_host[0] == 400
        duplicate_signature = await _request(
            app,
            "POST",
            "/api/yfeistai/v1/jobs",
            host=host,
            body=b"{}",
            content_type=b"application/json",
            extra_headers=(*signed_headers, (b"x-yfeistai-signature", b"b" * 64)),
        )
        assert duplicate_signature[0] == 400
        query_not_signed = await _request(
            app,
            "POST",
            "/api/yfeistai/v1/jobs",
            host=host,
            body=b"{}",
            content_type=b"application/json",
            query_string=b"source=unsigned",
            extra_headers=signed_headers,
        )
        assert query_not_signed[0] == 400
        for unsafe_path in (
            "/api/yfeistai/v1//jobs",
            "/api/yfeistai/v1/./jobs",
            "/api/yfeistai/v1/../jobs",
        ):
            assert (
                await _request(
                    app,
                    "POST",
                    unsafe_path,
                    host=host,
                    body=b"{}",
                    content_type=b"application/json",
                    extra_headers=signed_headers,
                )
            )[0] == 400

    asyncio.run(scenario())
    assert forwarded == [
        (
            "POST",
            "https://openmaic-upstream.example.test/api/yfeistai/v1/jobs",
            b'{"jobId":"job-fallback-01"}',
        )
    ]


@pytest.mark.parametrize(
    "host", ["shared-ingress.example.test", "openmaic-shared-ingress-observer:8000"]
)
@pytest.mark.parametrize("failure_status", [401, 500])
def test_failed_signed_upstream_does_not_commit_counter_or_exhaust_event_state(
    failure_status: int, host: str
) -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    statuses = iter((failure_status, 202))
    forwarded = 0

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        nonlocal forwarded
        forwarded += 1
        return module.HTTPResponse(
            status_code=next(statuses),
            headers={"content-type": "application/json"},
            body=b"{}",
        )

    app = module.OpenMAICObserverService(
        replace(_proxy_config(module), state_limit=1),
        now=lambda: now,
        requester=requester,
    )
    signed = (
        (b"x-yfeistai-tenant-id", b"tenant-01"),
        (b"x-yfeistai-job-id", b"job-01"),
        (b"x-yfeistai-timestamp", b"1788134400"),
        (b"x-yfeistai-idempotency-key", b"forged-attempt-01"),
        (b"x-yfeistai-signature", b"a" * 64),
    )

    async def scenario() -> None:
        first = await _request(
            app,
            "POST",
            "/api/yfeistai/v1/jobs",
            host=host,
            body=b"{}",
            content_type=b"application/json",
            extra_headers=signed,
        )
        assert first[0] == failure_status
        _, _, observation = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert json.loads(observation)["requestCount"] == 0

        second = await _request(
            app,
            "POST",
            "/api/yfeistai/v1/jobs",
            host=host,
            body=b"{}",
            content_type=b"application/json",
            extra_headers=(
                *signed[:-2],
                (b"x-yfeistai-idempotency-key", b"real-attempt-02"),
                signed[-1],
            ),
        )
        assert second[0] == 202

    asyncio.run(scenario())
    assert forwarded == 2


@pytest.mark.parametrize("failure", ["upstream-500", "cancelled"])
def test_failed_canary_proxy_attempt_rolls_back_for_same_canary_retry(failure: str) -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    app = None
    upstream_attempts = 0

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        nonlocal upstream_attempts
        if url == CANDIDATE_OBSERVER_HEALTH_URL:
            assert app is not None
            try:
                status, _, _ = await _request(
                    app,
                    "GET",
                    "/api/yfeistai/v1/health",
                    host="shared-ingress.example.test",
                    extra_headers=(
                        (
                            b"x-yfeistai-observer-canary",
                            headers["X-YFeiSTAI-Observer-Canary"].encode("ascii"),
                        ),
                    ),
                )
            except asyncio.CancelledError:
                raise
            healthy = status == 200
            return module.HTTPResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    {
                        "candidateSha256": CANDIDATE_SHA256,
                        "components": {
                            "openmaic_shared": {
                                "status": "healthy" if healthy else "unhealthy",
                                "service": "openmaic",
                                "mode": "shared",
                            }
                        },
                    }
                ).encode(),
            )
        upstream_attempts += 1
        if upstream_attempts == 1:
            if failure == "cancelled":
                raise asyncio.CancelledError
            return module.HTTPResponse(500, {"content-type": "application/json"}, b"{}")
        return module.HTTPResponse(200, {"content-type": "application/json"}, b"{}")

    app = module.OpenMAICObserverService(
        _proxy_config(module),
        now=lambda: now,
        requester=requester,
    )

    async def scenario() -> None:
        first = _request(
            app,
            "POST",
            "/v1/control-canaries",
            host="shared-ingress.example.test",
            body=_control_body("retry-control-01"),
            content_type=b"application/json",
        )
        if failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            assert (await first)[0] == 502
        _, _, observation = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert json.loads(observation)["requestCount"] == 0
        assert (
            await _request(
                app,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=_control_body("retry-control-01"),
                content_type=b"application/json",
            )
        )[0] == 202

    asyncio.run(scenario())


def test_unhealthy_candidate_health_rolls_back_canary_observation_for_same_id_retry() -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    app = None
    candidate_attempts = 0
    upstream_attempts = 0

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        nonlocal candidate_attempts, upstream_attempts
        if url == CANDIDATE_OBSERVER_HEALTH_URL:
            candidate_attempts += 1
            assert app is not None
            challenge = headers["X-YFeiSTAI-Observer-Canary"]
            status, _, _ = await _request(
                app,
                "GET",
                "/api/yfeistai/v1/health",
                host="shared-ingress.example.test",
                extra_headers=((b"x-yfeistai-observer-canary", challenge.encode("ascii")),),
            )
            assert status == 200
            return module.HTTPResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    {
                        "candidateSha256": CANDIDATE_SHA256,
                        "components": {
                            "openmaic_shared": {
                                "status": ("unhealthy" if candidate_attempts == 1 else "healthy"),
                                "service": "openmaic",
                                "mode": "shared",
                            }
                        },
                    }
                ).encode(),
            )
        upstream_attempts += 1
        assert method == "GET"
        assert url == "https://openmaic-upstream.example.test/api/yfeistai/v1/health"
        assert content == b""
        assert "x-yfeistai-observer-canary" not in headers
        assert timeout_seconds > 0
        assert max_response_bytes > 0
        return module.HTTPResponse(200, {"content-type": "application/json"}, b"{}")

    app = module.OpenMAICObserverService(
        _proxy_config(module),
        now=lambda: now,
        requester=requester,
    )

    async def scenario() -> None:
        first = await _request(
            app,
            "POST",
            "/v1/control-canaries",
            host="shared-ingress.example.test",
            body=_control_body("unhealthy-retry-01"),
            content_type=b"application/json",
        )
        assert first[0] == 502
        assert json.loads(first[2]) == {"error": "control_canary_failed"}
        _, _, first_observation = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert json.loads(first_observation)["requestCount"] == 0
        assert json.loads(first_observation)["lastCanaryId"] is None

        second = await _request(
            app,
            "POST",
            "/v1/control-canaries",
            host="shared-ingress.example.test",
            body=_control_body("unhealthy-retry-01"),
            content_type=b"application/json",
        )
        assert second[0] == 202
        assert json.loads(second[2]) == {
            "accepted": True,
            "canaryId": "unhealthy-retry-01",
        }
        _, _, second_observation = await _request(
            app,
            "GET",
            "/v1/observations/run-dedicated-01",
            host="observer.example.test",
        )
        assert json.loads(second_observation)["requestCount"] == 1
        assert json.loads(second_observation)["lastCanaryId"] == "unhealthy-retry-01"

    asyncio.run(scenario())
    assert candidate_attempts == 2
    assert upstream_attempts == 2


@pytest.mark.parametrize(
    "scenario",
    ["bypass", "missing", "wrong", "post", "body", "signed", "concurrent"],
)
def test_control_canary_rejects_noncausal_or_ambiguous_candidate_health(
    scenario: str,
) -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    app = None

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        if url == CANDIDATE_OBSERVER_HEALTH_URL:
            challenge = headers["X-YFeiSTAI-Observer-Canary"]
            if scenario != "bypass":
                extra_headers: tuple[tuple[bytes, bytes], ...] = ()
                request_method = "GET"
                request_body = b""
                if scenario not in {"missing", "concurrent"}:
                    supplied = "wrong-challenge" if scenario == "wrong" else challenge
                    extra_headers = ((b"x-yfeistai-observer-canary", supplied.encode("ascii")),)
                if scenario == "post":
                    request_method = "POST"
                elif scenario == "body":
                    request_body = b"unexpected"
                elif scenario == "signed":
                    extra_headers = (
                        *extra_headers,
                        (b"x-yfeistai-signature", b"a" * 64),
                    )
                assert app is not None
                if scenario == "concurrent":
                    await _request(
                        app,
                        "GET",
                        "/api/yfeistai/v1/health",
                        host="shared-ingress.example.test",
                    )
                    extra_headers = ((b"x-yfeistai-observer-canary", challenge.encode("ascii")),)
                await _request(
                    app,
                    request_method,
                    "/api/yfeistai/v1/health",
                    host="shared-ingress.example.test",
                    body=request_body,
                    extra_headers=extra_headers,
                )
            return module.HTTPResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "candidateSha256": CANDIDATE_SHA256,
                        "components": {
                            "openmaic_shared": {
                                "status": "healthy",
                                "service": "openmaic",
                                "mode": "shared",
                            }
                        },
                    }
                ).encode(),
            )
        assert "authorization" not in headers
        assert "x-yfeistai-observer-canary" not in headers
        return module.HTTPResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"service":"openmaic"}',
        )

    app = module.OpenMAICObserverService(
        _proxy_config(module),
        now=lambda: now,
        requester=requester,
    )

    async def scenario_request() -> None:
        status, _, _ = await _request(
            app,
            "POST",
            "/v1/control-canaries",
            host="shared-ingress.example.test",
            body=_control_body("control-canary-negative-01"),
            content_type=b"application/json",
        )
        assert status == 502

    asyncio.run(scenario_request())


def test_control_cancellation_clears_pending_challenge_for_the_next_request() -> None:
    module = _module()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    app = None
    candidate_calls = 0

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        nonlocal candidate_calls
        if url == CANDIDATE_OBSERVER_HEALTH_URL:
            candidate_calls += 1
            if candidate_calls == 1:
                raise asyncio.CancelledError
            assert app is not None
            status, _, _ = await _request(
                app,
                "GET",
                "/api/yfeistai/v1/health",
                host="shared-ingress.example.test",
                extra_headers=(
                    (
                        b"x-yfeistai-observer-canary",
                        headers["X-YFeiSTAI-Observer-Canary"].encode("ascii"),
                    ),
                ),
            )
            assert status == 200
            return module.HTTPResponse(
                200,
                {"content-type": "application/json"},
                b'{"candidateSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","components":{"openmaic_shared":{"status":"healthy","service":"openmaic","mode":"shared"}}}',
            )
        return module.HTTPResponse(200, {"content-type": "application/json"}, b"{}")

    app = module.OpenMAICObserverService(
        _proxy_config(module),
        now=lambda: now,
        requester=requester,
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await _request(
                app,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=_control_body("cancelled-control-01"),
                content_type=b"application/json",
            )
        status, _, _ = await _request(
            app,
            "POST",
            "/v1/control-canaries",
            host="shared-ingress.example.test",
            body=_control_body("replacement-control-01"),
            content_type=b"application/json",
        )
        assert status == 202

    asyncio.run(scenario())


def test_control_rechecks_token_after_body_read_and_control_lock_queue() -> None:
    module = _module()
    valid = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    expired = valid + timedelta(minutes=11)
    current = valid
    candidate_calls = 0
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def requester(*, method, url, headers, content, timeout_seconds, max_response_bytes):
        nonlocal candidate_calls
        candidate_calls += 1
        first_entered.set()
        await release_first.wait()
        return module.HTTPResponse(
            200,
            {"content-type": "application/json"},
            b'{"candidateSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","components":{"openmaic_shared":{"status":"unhealthy","service":"openmaic","mode":"shared"}}}',
        )

    app = module.OpenMAICObserverService(
        _proxy_config(module),
        now=lambda: current,
        requester=requester,
    )

    async def request_with_expiry_during_body() -> tuple[int, dict[str, str], bytes]:
        nonlocal current
        events = iter(({"type": "http.request", "body": _control_body("body-expiry-01")},))
        sent: list[dict[str, object]] = []

        async def receive():
            nonlocal current
            current = expired
            return next(events)

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/control-canaries",
                "query_string": b"",
                "headers": [
                    (b"host", b"shared-ingress.example.test"),
                    (b"authorization", f"Bearer {AUTH_TOKEN}".encode()),
                    (b"content-type", b"application/json"),
                ],
            },
            receive,
            send,
        )
        return int(sent[0]["status"]), {}, bytes(sent[1]["body"])

    async def scenario() -> None:
        nonlocal current
        body_expiry = asyncio.create_task(request_with_expiry_during_body())
        await asyncio.sleep(0)
        release_first.set()
        assert (await body_expiry)[0] == 401
        assert candidate_calls == 0

        current = valid
        first_entered.clear()
        release_first.clear()
        first = asyncio.create_task(
            _request(
                app,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=_control_body("lock-holder-01"),
                content_type=b"application/json",
            )
        )
        await first_entered.wait()
        second = asyncio.create_task(
            _request(
                app,
                "POST",
                "/v1/control-canaries",
                host="shared-ingress.example.test",
                body=_control_body("lock-waiter-01"),
                content_type=b"application/json",
            )
        )
        await asyncio.sleep(0)
        current = expired
        release_first.set()
        assert (await first)[0] == 502
        assert (await second)[0] == 401
        assert candidate_calls == 1

    asyncio.run(scenario())


def test_default_requester_enforces_exchange_deadline_and_identity_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    original_client = module.httpx.AsyncClient

    class SlowStream(module.httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.03)
            yield b"a"

    async def slow_handler(_request):
        await asyncio.sleep(0.03)
        return module.httpx.Response(200, stream=SlowStream())

    def slow_client_factory(**kwargs):
        kwargs["transport"] = module.httpx.MockTransport(slow_handler)
        return original_client(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", slow_client_factory)

    async def deadline_scenario() -> None:
        with pytest.raises(module.OpenMAICObserverServiceError):
            response = await module._default_requester(
                method="GET",
                url="https://upstream.example.test/health",
                headers={},
                content=b"",
                timeout_seconds=0.05,
                max_response_bytes=64,
            )
            assert not isinstance(response.body, bytes)
            async for _chunk in response.body:
                pass

    asyncio.run(deadline_scenario())

    async def encoded_handler(_request):
        return module.httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(b"encoded-body"),
        )

    def encoded_client_factory(**kwargs):
        kwargs["transport"] = module.httpx.MockTransport(encoded_handler)
        return original_client(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", encoded_client_factory)

    async def encoding_scenario() -> None:
        with pytest.raises(module.OpenMAICObserverServiceError, match="upstream_invalid"):
            await module._default_requester(
                method="GET",
                url="https://upstream.example.test/health",
                headers={},
                content=b"",
                timeout_seconds=1,
                max_response_bytes=64,
            )

    asyncio.run(encoding_scenario())


def test_default_requester_streams_and_stops_at_the_response_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    consumed = 0

    class Stream(module.httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal consumed
            for chunk in (b"a" * 64, b"b", b"must-not-be-read"):
                consumed += 1
                yield chunk

    async def handler(_request):
        return module.httpx.Response(200, stream=Stream())

    original_client = module.httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = module.httpx.MockTransport(handler)
        return original_client(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", client_factory)

    async def scenario() -> None:
        received = bytearray()
        with pytest.raises(module.OpenMAICObserverServiceError, match="upstream_invalid"):
            response = await module._default_requester(
                method="GET",
                url="https://upstream.example.test/health",
                headers={},
                content=b"",
                timeout_seconds=1,
                max_response_bytes=64,
            )
            assert not isinstance(response.body, bytes)
            async for chunk in response.body:
                received.extend(chunk)
        assert received == b"a" * 64

    asyncio.run(scenario())
    assert consumed == 2
