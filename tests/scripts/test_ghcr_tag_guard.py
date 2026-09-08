"""Offline typed GHCR admission checks; no registry or credentials are contacted."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/xinlingzhifei/yfeistai-ragflow"
TAG = "yfeistai-first-release-20260908-aaaaaaaa"
ACTOR = "test-actor"
SECRET = "fixture-github-secret"
BEARER = "fixture-registry-secret"
ABSENT = {"errors": [{"code": "MANIFEST_UNKNOWN", "message": "manifest unknown"}]}


def _guard():
    path = ROOT / "scripts/ghcr_tag_guard.py"
    assert path.is_file(), "typed GHCR tag guard has not been implemented"
    spec = importlib.util.spec_from_file_location("ghcr_tag_guard_test_target", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status, body, content_type="application/json"):
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.content_type = content_type
        self.read_limits = []

    def getheader(self, name, default=None):
        assert name.lower() == "content-type"
        return self.content_type

    def read(self, limit):
        self.read_limits.append(limit)
        return self.body[:limit]


def _transport(monkeypatch, guard, responses):
    calls = []
    connections = []

    class Connection:
        def __init__(self, host, *, timeout):
            assert host == "ghcr.io"
            assert 0 < timeout <= 30
            self.closed = False
            connections.append(self)

        def request(self, method, path, *, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            response = responses[len(calls) - 1]
            if isinstance(response, Exception):
                raise response
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(guard.http.client, "HTTPSConnection", Connection)
    return calls, connections


def _invoke(guard, **overrides):
    arguments = dict(image_repository=IMAGE, release_tag=TAG, actor=ACTOR, github_token=SECRET)
    arguments.update(overrides)
    return guard.require_absent(**arguments)


def test_authenticated_exact_manifest_unknown_is_the_only_success(monkeypatch):
    guard = _guard()
    responses = [Response(200, {"token": BEARER}), Response(404, ABSENT)]
    calls, connections = _transport(monkeypatch, guard, responses)
    assert _invoke(guard) == {
        "stage": "manifest",
        "status": 404,
        "errorCodes": ["MANIFEST_UNKNOWN"],
    }
    assert [(method, path) for method, path, _ in calls] == [
        (
            "GET",
            "/token?service=ghcr.io&scope=repository%3Axinlingzhifei%2Fyfeistai-ragflow%3Apull",
        ),
        ("GET", f"/v2/xinlingzhifei/yfeistai-ragflow/manifests/{TAG}"),
    ]
    assert (
        calls[0][2]["Authorization"]
        == "Basic " + base64.b64encode(f"{ACTOR}:{SECRET}".encode()).decode()
    )
    assert calls[1][2]["Authorization"] == f"Bearer {BEARER}"
    assert SECRET not in calls[1][1] and BEARER not in calls[1][1]
    assert all(connection.closed for connection in connections)
    assert all(response.read_limits == [guard.MAX_BODY_BYTES + 1] for response in responses)


@pytest.mark.parametrize("stage", ["token", "manifest"])
@pytest.mark.parametrize("status", [200, 201, 301, 302, 307, 308, 401, 403, 404, 429, 500])
def test_other_http_statuses_never_authorize_publication(monkeypatch, stage, status):
    if (stage, status) in {("token", 200), ("manifest", 404)}:
        return
    guard = _guard()
    response = Response(status, ABSENT)
    responses = [response] if stage == "token" else [Response(200, {"token": BEARER}), response]
    calls, connections = _transport(monkeypatch, guard, responses)
    with pytest.raises(guard.GuardFailure) as error:
        _invoke(guard)
    assert error.value.diagnostic() == {"stage": stage, "status": status, "errorCodes": []}
    assert len(calls) == (1 if stage == "token" else 2)
    assert response.read_limits == []  # No redirect following or arbitrary error-body logging.
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"errors": []},
        {"errors": "MANIFEST_UNKNOWN"},
        {"errors": ["MANIFEST_UNKNOWN"]},
        {"errors": [{"message": "MANIFEST_UNKNOWN"}]},
        {"errors": [{"code": "NAME_UNKNOWN"}]},
        {"errors": [{"code": "MANIFEST_UNKNOWN"}, {"code": "DENIED"}]},
        {"errors": [{"code": "MANIFEST_UNKNOWN"}, {"code": "fixture-untrusted-code"}]},
        {"errors": [{"code": "MANIFEST_UNKNOWN", "unexpected": True}]},
        {"errors": [{"code": "MANIFEST_UNKNOWN"}], "unexpected": True},
        b'{"errors":[{"code":"DENIED","code":"MANIFEST_UNKNOWN"}]}',
        b'{"errors":[],"errors":[{"code":"MANIFEST_UNKNOWN"}]}',
        b'{"errors":[{"code":"MANIFEST_UNKNOWN","detail":{"x":1,"x":2}}]}',
        b'{"errors":[{"code":"MANIFEST_UNKNOWN","detail":NaN}]}',
        b'\xef\xbb\xbf{"errors":[{"code":"MANIFEST_UNKNOWN"}]}',
        b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]} trailing',
        b"<html>MANIFEST_UNKNOWN</html>",
        b"\xff",
        b"[]",
        b"null",
        b"",
    ],
)
def test_ambiguous_malformed_or_mixed_manifest_errors_fail_closed(monkeypatch, body):
    guard = _guard()
    _transport(monkeypatch, guard, [Response(200, {"token": BEARER}), Response(404, body)])
    with pytest.raises(guard.GuardFailure):
        _invoke(guard)


@pytest.mark.parametrize("stage", ["token", "manifest"])
@pytest.mark.parametrize(
    "content_type", [None, "text/html", "text/plain", "application/json,text/html"]
)
def test_json_media_type_is_required(monkeypatch, stage, content_type):
    guard = _guard()
    response = Response(
        200 if stage == "token" else 404,
        {"token": BEARER} if stage == "token" else ABSENT,
        content_type,
    )
    responses = [response] if stage == "token" else [Response(200, {"token": BEARER}), response]
    _transport(monkeypatch, guard, responses)
    with pytest.raises(guard.GuardFailure):
        _invoke(guard)
    assert response.read_limits == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"token": ""},
        {"token": 1},
        {"token": "bad\r\nheader"},
        {"token": "bad token"},
        {"token": "非ASCII"},
        {"token": BEARER, "access_token": "different"},
        {"token": BEARER, "errors": ABSENT["errors"]},
        {"token": BEARER, "unexpected": True},
        b'{"token":"first","token":"second"}',
        b"<html>token</html>",
    ],
)
def test_invalid_token_response_stops_before_manifest(monkeypatch, body):
    guard = _guard()
    calls, _ = _transport(monkeypatch, guard, [Response(200, body)])
    with pytest.raises(guard.GuardFailure):
        _invoke(guard)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"access_token": BEARER},
        {
            "token": BEARER,
            "access_token": BEARER,
            "expires_in": 60,
            "issued_at": "2026-09-08T00:00:00Z",
        },
    ],
)
def test_standard_token_aliases_are_supported(monkeypatch, body):
    guard = _guard()
    _transport(monkeypatch, guard, [Response(200, body), Response(404, ABSENT)])
    assert _invoke(guard)["status"] == 404


@pytest.mark.parametrize("stage", ["token", "manifest"])
def test_oversized_response_is_bounded_and_rejected(monkeypatch, stage):
    guard = _guard()
    valid = {"token": BEARER} if stage == "token" else ABSENT
    body = json.dumps(valid).encode() + b" " * guard.MAX_BODY_BYTES
    response = Response(200 if stage == "token" else 404, body)
    responses = [response] if stage == "token" else [Response(200, {"token": BEARER}), response]
    _transport(monkeypatch, guard, responses)
    with pytest.raises(guard.GuardFailure):
        _invoke(guard)
    assert response.read_limits == [guard.MAX_BODY_BYTES + 1]


@pytest.mark.parametrize(
    "override",
    [
        {"image_repository": "ghcr.io/example/other"},
        {"image_repository": IMAGE + "/../other"},
        {"release_tag": "latest"},
        {"release_tag": TAG + "\n"},
        {"release_tag": TAG + "/../other"},
        {"release_tag": TAG.upper()},
        {"release_tag": "yfeistai-first-release-20260230-aaaaaaaa"},
        {"release_tag": "yfeistai-first-release-20260908-00000000"},
        {"actor": ""},
        {"actor": "name:password"},
        {"actor": "a\r\nb"},
        {"github_token": ""},
        {"github_token": "a b"},
        {"github_token": "a\r\nb"},
    ],
)
def test_unsafe_identity_or_missing_credentials_never_makes_requests(monkeypatch, override):
    guard = _guard()
    calls, _ = _transport(monkeypatch, guard, [])
    with pytest.raises(guard.GuardFailure) as error:
        _invoke(guard, **override)
    assert error.value.diagnostic() == {"stage": "input", "status": None, "errorCodes": []}
    assert calls == []


@pytest.mark.parametrize("failure", ["network", "untrusted_code", "malformed", "success"])
def test_cli_uses_only_explicit_environment_credentials_and_safe_diagnostics(
    monkeypatch, capsys, failure
):
    guard = _guard()
    monkeypatch.setenv("GITHUB_ACTOR", ACTOR)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    second = {
        "network": OSError(f"sensitive {SECRET} {BEARER}"),
        "untrusted_code": Response(404, {"errors": [{"code": SECRET, "message": BEARER}]}),
        "malformed": Response(404, f"{SECRET} {BEARER}".encode()),
        "success": Response(404, ABSENT),
    }[failure]
    _transport(monkeypatch, guard, [Response(200, {"token": BEARER}), second])
    assert guard.main(["--image-repository", IMAGE, "--release-tag", TAG]) == (
        0 if failure == "success" else 1
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert SECRET not in output and BEARER not in output
    diagnostic = json.loads(output)
    assert set(diagnostic) == {"stage", "status", "errorCodes"}
    assert diagnostic["stage"] == "manifest"
    assert diagnostic["errorCodes"] == (["MANIFEST_UNKNOWN"] if failure == "success" else [])


def test_cli_does_not_accept_or_echo_credentials_in_arguments(monkeypatch, capsys):
    guard = _guard()
    calls, _ = _transport(monkeypatch, guard, [])
    assert guard.main(["--image-repository", IMAGE, "--release-tag", TAG, "--token", SECRET]) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out + output.err)["stage"] == "input"
    assert calls == []


@pytest.mark.parametrize("stage", ["token", "manifest"])
def test_excessively_nested_json_has_only_safe_cli_diagnostics(monkeypatch, capsys, stage):
    guard = _guard()
    monkeypatch.setenv("GITHUB_ACTOR", ACTOR)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    body = b'{"detail":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    response = Response(200 if stage == "token" else 404, body)
    responses = [response] if stage == "token" else [Response(200, {"token": BEARER}), response]
    _transport(monkeypatch, guard, responses)
    assert guard.main(["--image-repository", IMAGE, "--release-tag", TAG]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "stage": stage,
        "status": 200 if stage == "token" else 404,
        "errorCodes": [],
    }


@pytest.mark.parametrize("missing", ["GITHUB_ACTOR", "GITHUB_TOKEN"])
def test_cli_requires_explicit_environment_not_docker_or_gh_credentials(
    monkeypatch, capsys, missing
):
    guard = _guard()
    monkeypatch.setenv("GITHUB_ACTOR", ACTOR)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    monkeypatch.setenv("GH_TOKEN", "must-not-be-used")
    monkeypatch.delenv(missing)
    calls, _ = _transport(monkeypatch, guard, [])
    assert guard.main(["--image-repository", IMAGE, "--release-tag", TAG]) == 1
    assert json.loads(capsys.readouterr().out)["stage"] == "input"
    assert calls == []
