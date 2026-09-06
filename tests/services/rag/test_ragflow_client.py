"""RAGFlow v0.27.1 wire contracts, safe failures, and connection probing."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import httpx
import pytest


def _modules():
    root = "deeptutor.services.rag.pipelines.ragflow"
    return importlib.import_module(f"{root}.config"), importlib.import_module(f"{root}.client")


def _client(handler):
    config, client = _modules()
    return client.RagflowClient(
        config.RagflowConfig("https://ragflow.example", "secret-token"),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://ragflow.example",
        "ftp://ragflow.example",
        "https://u:p@ragflow.example",
        "https://ragflow.example?token=secret",
        "https://ragflow.example#part",
        "https://ragflow.example/../evil",
        "https://ragflow.example/%2e%2e/evil",
        "https://ragflow.example\\evil",
        "https://ragflow.example\n",
    ],
)
def test_config_rejects_unsafe_urls_without_echoing_secrets(url):
    config, _ = _modules()
    with pytest.raises(ValueError) as exc:
        config.RagflowConfig(url, "secret-token")
    assert url not in str(exc.value)
    assert "secret-token" not in str(exc.value)


def test_config_is_frozen_redacted_and_http_requires_explicit_opt_in():
    config, _ = _modules()
    cfg = config.RagflowConfig("http://127.0.0.1:9380/", "secret-token", allow_http=True)
    assert cfg.base_url == "http://127.0.0.1:9380"
    assert "secret-token" not in repr(cfg)
    with pytest.raises(AttributeError):
        cfg.api_key = "changed"
    for timeout in [0, -1, float("inf"), float("nan")]:
        with pytest.raises(ValueError):
            config.RagflowConfig("https://ragflow.example", "key", read_timeout=timeout)


@pytest.mark.parametrize(
    "method,args,kwargs,verb,path,body,data,expected",
    [
        (
            "list_datasets",
            (),
            {"page": 2},
            "GET",
            "/datasets",
            None,
            [{"id": "ds"}],
            [{"id": "ds"}],
        ),
        (
            "create_dataset",
            ({"name": "KB"},),
            {},
            "POST",
            "/datasets",
            {"name": "KB"},
            {"id": "ds"},
            {"id": "ds"},
        ),
        (
            "update_dataset",
            ("ds", {"name": "New"}),
            {},
            "PUT",
            "/datasets/ds",
            {"name": "New"},
            None,
            None,
        ),
        ("delete_datasets", (["ds"],), {}, "DELETE", "/datasets", {"ids": ["ds"]}, None, None),
        (
            "list_documents",
            ("ds",),
            {},
            "GET",
            "/datasets/ds/documents",
            None,
            {"docs": [{"id": "doc"}], "total_datasets": 7},
            {"docs": [{"id": "doc"}], "total": 7},
        ),
        (
            "create_document",
            ("ds",),
            {"name": "blank.txt"},
            "POST",
            "/datasets/ds/documents",
            {"name": "blank.txt"},
            {"id": "doc"},
            {"id": "doc"},
        ),
        (
            "update_document",
            ("ds", "doc", {"name": "renamed"}),
            {},
            "PATCH",
            "/datasets/ds/documents/doc",
            {"name": "renamed"},
            None,
            None,
        ),
        (
            "delete_documents",
            ("ds", ["doc"]),
            {},
            "DELETE",
            "/datasets/ds/documents",
            {"ids": ["doc"]},
            None,
            None,
        ),
        (
            "parse_documents",
            ("ds", ["doc"]),
            {},
            "POST",
            "/datasets/ds/chunks",
            {"document_ids": ["doc"]},
            None,
            None,
        ),
        (
            "cancel_parse",
            ("ds", ["doc"]),
            {},
            "DELETE",
            "/datasets/ds/chunks",
            {"document_ids": ["doc"]},
            None,
            None,
        ),
        (
            "ingest_documents",
            (["doc"],),
            {"run": "1", "delete": True},
            "POST",
            "/documents/ingest",
            {"doc_ids": ["doc"], "run": "1", "delete": True},
            True,
            True,
        ),
        (
            "list_chunks",
            ("ds", "doc"),
            {},
            "GET",
            "/datasets/ds/documents/doc/chunks",
            None,
            {"chunks": [{"id": "ch"}], "total": 9},
            {"chunks": [{"id": "ch"}], "total": 9},
        ),
        (
            "get_chunk",
            ("ds", "doc", "ch"),
            {},
            "GET",
            "/datasets/ds/documents/doc/chunks/ch",
            None,
            {"id": "ch"},
            {"id": "ch"},
        ),
        (
            "add_chunk",
            ("ds", "doc", {"content": "hello"}),
            {},
            "POST",
            "/datasets/ds/documents/doc/chunks",
            {"content": "hello"},
            {"chunk": {"id": "ch"}},
            {"chunk": {"id": "ch"}},
        ),
        (
            "update_chunk",
            ("ds", "doc", "ch", {"content": "new"}),
            {},
            "PATCH",
            "/datasets/ds/documents/doc/chunks/ch",
            {"content": "new"},
            None,
            None,
        ),
        (
            "delete_chunks",
            ("ds", "doc", ["ch"]),
            {},
            "DELETE",
            "/datasets/ds/documents/doc/chunks",
            {"chunk_ids": ["ch"]},
            None,
            None,
        ),
        (
            "set_chunk_availability",
            ("ds", "doc", ["ch"], False),
            {},
            "PATCH",
            "/datasets/ds/documents/doc/chunks",
            {"chunk_ids": ["ch"], "available": False},
            True,
            True,
        ),
        (
            "retrieve",
            ({"question": "test", "dataset_ids": ["ds"]},),
            {},
            "POST",
            "/retrieval",
            {"question": "test", "dataset_ids": ["ds"]},
            {"chunks": [], "total": 0},
            {"chunks": [], "total": 0},
        ),
    ],
)
def test_endpoints_use_pinned_methods_bodies_and_normalize_pagination(
    method, args, kwargs, verb, path, body, data, expected
):
    def handler(request):
        assert request.method == verb
        assert request.url.path == "/api/v1" + path
        assert request.headers["Authorization"] == "Bearer secret-token"
        if body is not None:
            assert json.loads(request.content) == body
        if method == "create_document":
            assert request.url.params["type"] == "empty"
        if method == "list_datasets":
            assert request.url.params["page"] == "2"
        return httpx.Response(200, json={"code": 0, "data": data})

    client = _client(handler)
    assert asyncio.run(getattr(client, method)(*args, **kwargs)) == expected


def test_upload_streams_file_and_web_creation_uses_multipart(tmp_path, monkeypatch):
    file_path = tmp_path / "input.txt"
    file_path.write_bytes(b"document content")
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: pytest.fail("upload must stream from file")
    )
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Content-Type"].startswith("multipart/form-data;")
        if request.url.params.get("type") == "web":
            assert b'name="url"' in request.content
            assert b"https://example.com/page" in request.content
            return httpx.Response(200, json={"code": 0, "data": {"id": "webdoc"}})
        else:
            assert b'filename="operation-id.txt"' in request.content
            assert b"document content" in request.content
        return httpx.Response(200, json={"code": 0, "data": [{"id": "doc"}]})

    client = _client(handler)
    assert asyncio.run(client.upload_document("ds", file_path, filename="operation-id.txt")) == [
        {"id": "doc"}
    ]
    assert asyncio.run(
        client.create_document("ds", name="page", url="https://example.com/page")
    ) == {"id": "webdoc"}
    assert len(requests) == 2


@pytest.mark.parametrize(
    "resource_id",
    ["", ".", "..", "../ds", "ds/doc", "ds?x=1", "ds#part", "%2f", "ds\\doc", "ds\n", ".hidden"],
)
def test_path_ids_are_validated_before_sending(resource_id):
    _, module = _modules()
    client = _client(lambda request: pytest.fail("invalid ID must not send"))
    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(client.list_documents(resource_id))
    assert exc.value.error_type == "validation_error"
    assert not exc.value.outcome_unknown


@pytest.mark.parametrize(
    "method,args",
    [
        ("delete_datasets", ([],)),
        ("delete_documents", ("ds", [])),
        ("delete_chunks", ("ds", "doc", [])),
        ("parse_documents", ("ds", [])),
        ("cancel_parse", ("ds", [])),
        ("set_chunk_availability", ("ds", "doc", [], False)),
    ],
)
def test_bulk_mutations_require_nonempty_explicit_ids(method, args):
    _, module = _modules()
    client = _client(lambda request: pytest.fail("empty mutation must not send"))
    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(getattr(client, method)(*args))
    assert exc.value.error_type == "validation_error"


@pytest.mark.parametrize(
    "status,code,unknown,error_type",
    [
        (401, 401, False, "auth_error"),
        (403, 108, False, "auth_error"),
        (200, 109, False, "auth_error"),
        (400, 102, True, "validation_error"),
        (200, 102, True, "validation_error"),
        (503, 500, True, "upstream_error"),
        (200, 0, True, "provider_incompatible"),
    ],
)
def test_writes_never_retry_and_errors_never_expose_upstream_data(
    status, code, unknown, error_type
):
    calls = []

    def handler(request):
        calls.append(request)
        payload = {"code": code, "message": "secret-token https://private.example internal trace"}
        return httpx.Response(status, json=payload)

    _, module = _modules()
    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(_client(handler).create_dataset({"name": "KB"}))
    assert len(calls) == 1
    assert exc.value.outcome_unknown is unknown
    assert exc.value.error_type == error_type
    assert "secret-token" not in str(exc.value)
    assert "private.example" not in repr(exc.value)
    assert exc.value.__cause__ is None


def test_read_retries_are_bounded_and_retrieval_post_is_read_only(monkeypatch):
    _, module = _modules()
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"code": 0, "data": {"chunks": [], "total": 0}})

    assert asyncio.run(_client(handler).retrieve({"question": "q", "dataset_ids": ["ds"]})) == {
        "chunks": [],
        "total": 0,
    }
    assert len(calls) == 3
    assert delays == [2, 2]


def test_write_transport_failure_is_unknown_redacted_and_not_retried():
    _, module = _modules()
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret-token https://private.example", request=request)

    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(_client(handler).delete_datasets(["ds"]))
    assert len(calls) == 1
    assert exc.value.outcome_unknown
    assert exc.value.retryable is False
    assert "secret-token" not in str(exc.value)
    assert exc.value.__cause__ is None


def test_redirects_are_rejected_and_never_followed():
    _, module = _modules()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.example"})

    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(_client(handler).list_datasets())
    assert len(calls) == 1
    assert exc.value.error_type == "provider_incompatible"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"code": "0", "data": []},
        {"code": False, "data": []},
        {"code": 0, "data": [{}]},
        {"code": 0, "data": ["bad"]},
    ],
)
def test_dataset_contract_fails_closed(payload):
    _, module = _modules()
    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(_client(lambda request: httpx.Response(200, json=payload)).list_datasets())
    assert exc.value.error_type == "provider_incompatible"


def test_download_is_bounded_and_json_errors_are_not_files(monkeypatch):
    _, module = _modules()
    client = _client(
        lambda request: httpx.Response(
            200, content=b"file bytes", headers={"Content-Type": "application/pdf"}
        )
    )
    assert asyncio.run(client.download_document("ds", "doc")) == (b"file bytes", "application/pdf")

    client = _client(
        lambda request: httpx.Response(200, json={"code": 102, "message": "secret-token"})
    )
    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(client.download_document("ds", "doc"))
    assert exc.value.error_type == "validation_error"

    monkeypatch.setattr(module, "MAX_DOWNLOAD_BYTES", 5)
    client = _client(
        lambda request: httpx.Response(
            200, content=b"too large", headers={"Content-Type": "text/plain"}
        )
    )
    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(client.download_document("ds", "doc"))
    assert exc.value.error_type == "response_too_large"


def test_health_omits_credentials_and_probe_does_not_claim_version():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("healthz"):
            assert "Authorization" not in request.headers
            return httpx.Response(
                200,
                json={
                    "db": "ok",
                    "redis": "ok",
                    "doc_engine": "ok",
                    "storage": "ok",
                    "status": "ok",
                    "_meta": {"secret-token": "private"},
                },
            )
        return httpx.Response(200, json={"code": 0, "data": []})

    config, module = _modules()
    probe = importlib.import_module("deeptutor.services.rag.pipelines.ragflow.probe")
    cfg = config.RagflowConfig("https://ragflow.example", "secret-token")
    result = asyncio.run(
        probe.probe_server(
            cfg,
            client_factory=lambda cfg: module.RagflowClient(
                cfg, transport=httpx.MockTransport(handler)
            ),
        )
    )
    assert result["ok"] is True
    assert result["health"] == "healthy"
    assert result["auth"] == "valid"
    assert result["contract"] == "compatible"
    assert result["version_unknown"] is True
    assert "secret-token" not in json.dumps(result)
    assert len(seen) == 2


def test_probe_reports_auth_independently_from_health():
    config, module = _modules()
    probe = importlib.import_module("deeptutor.services.rag.pipelines.ragflow.probe")

    def handler(request):
        if request.url.path.endswith("healthz"):
            return httpx.Response(
                200,
                json={
                    "db": "ok",
                    "redis": "ok",
                    "doc_engine": "ok",
                    "storage": "ok",
                    "status": "ok",
                },
            )
        return httpx.Response(401, json={"code": 401, "message": "secret-token"})

    cfg = config.RagflowConfig("https://ragflow.example", "secret-token")
    result = asyncio.run(
        probe.probe_server(
            cfg,
            client_factory=lambda cfg: module.RagflowClient(
                cfg, transport=httpx.MockTransport(handler)
            ),
        )
    )
    assert result["ok"] is False
    assert result["health"] == "healthy"
    assert result["auth"] == "invalid"
    assert result["contract"] == "unknown"


@pytest.mark.parametrize(
    "method,args,kwargs,data",
    [
        (
            "delete_datasets",
            (["ds", "ds2"],),
            {},
            {"success_count": 1, "errors": ["private secret-token"]},
        ),
        ("delete_datasets", (["ds", "ds2"],), {}, {"success_count": 1}),
        ("delete_documents", ("ds", ["doc", "doc2"]), {}, {"deleted": 1}),
        ("create_document", ("ds",), {"name": "empty.txt"}, None),
        ("add_chunk", ("ds", "doc", {"content": "text"}), {}, {"chunk": {}}),
    ],
)
def test_success_envelope_cannot_hide_partial_or_invalid_mutations(method, args, kwargs, data):
    _, module = _modules()
    client = _client(lambda request: httpx.Response(200, json={"code": 0, "data": data}))
    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(getattr(client, method)(*args, **kwargs))
    assert exc.value.outcome_unknown is True
    assert "secret-token" not in str(exc.value)


def test_upload_read_failure_after_send_is_unknown(tmp_path):
    _, module = _modules()
    file_path = tmp_path / "input.txt"
    file_path.write_text("test", encoding="utf-8")

    def handler(request):
        raise OSError("secret-token local file disappeared")

    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(_client(handler).upload_document("ds", file_path, filename="input.txt"))
    assert exc.value.outcome_unknown is True
    assert "secret-token" not in str(exc.value)


def test_upload_empty_success_payload_is_unknown(tmp_path):
    _, module = _modules()
    file_path = tmp_path / "input.txt"
    file_path.write_text("test", encoding="utf-8")
    client = _client(lambda request: httpx.Response(200, json={"code": 0, "data": []}))
    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(client.upload_document("ds", file_path, filename="input.txt"))
    assert exc.value.outcome_unknown is True


def test_read_failures_stop_after_three_attempts(monkeypatch):
    _, module = _modules()
    calls = []

    async def sleep(delay):
        pass

    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("private internal address", request=request)

    with pytest.raises(module.RagflowError) as exc:
        asyncio.run(_client(handler).list_datasets())
    assert len(calls) == 3
    assert exc.value.outcome_unknown is False
