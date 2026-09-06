"""Small async adapter for RAGFlow v0.27.1, with conservative write outcomes.

Every request gets a fresh AsyncClient, so callers may use different event
loops. Reads can retry twice; mutations are never automatically replayed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import random
import re
from typing import Any

import httpx

from .config import RagflowConfig

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_ERROR_MESSAGES = {
    "auth_error": "RAGFlow authentication or access was rejected.",
    "validation_error": "RAGFlow request parameters were rejected.",
    "not_found": "RAGFlow resource was not found.",
    "conflict": "RAGFlow resource state conflicts with the request.",
    "rate_limit": "RAGFlow request rate limit was reached.",
    "upstream_error": "RAGFlow service is unavailable.",
    "transport_error": "RAGFlow connection failed or timed out.",
    "provider_incompatible": "RAGFlow response does not match the supported API contract.",
    "response_too_large": "RAGFlow response exceeds the configured size limit.",
    "file_error": "The local upload file could not be read.",
    "unavailable_chunk": "RAGFlow returned disabled or parent-expanded chunks; parent-child retrieval is not supported.",
}


class RagflowError(RuntimeError):
    """Safe public error; raw HTTP objects and upstream messages stay excluded."""

    def __init__(
        self,
        error_type: str,
        message: str | None = None,
        *,
        status_code: int | None = None,
        outcome_unknown: bool = False,
        retryable: bool = False,
    ) -> None:
        # Callers cannot accidentally expose secrets via a supplied message.
        super().__init__(_ERROR_MESSAGES.get(error_type, "RAGFlow request failed."))
        self.error_type = error_type
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown
        self.retryable = retryable


def _id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise RagflowError("validation_error")
    return value


def _ids(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values:
        raise RagflowError("validation_error")
    return [_id(value) for value in values]


def _shape_error(*, write: bool = False) -> RagflowError:
    return RagflowError("provider_incompatible", outcome_unknown=write)


def _object(value: Any, *, write: bool = False, require_id: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _shape_error(write=write)
    if require_id and (not isinstance(value.get("id"), str) or not _SAFE_ID.fullmatch(value["id"])):
        raise _shape_error(write=write)
    return value


def _objects(value: Any, *, write: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _shape_error(write=write)
    return [_object(item, write=write, require_id=True) for item in value]


def _page(value: Any, key: str) -> dict[str, Any]:
    data = _object(value)
    items = _objects(data.get(key))
    total = data.get("total", data.get("total_datasets"))
    if type(total) is not int or total < 0 or total < len(items):
        raise _shape_error()
    return {
        **{name: item for name, item in data.items() if name != "total_datasets"},
        key: items,
        "total": total,
    }


def _deleted(value: Any, count_key: str, expected: int) -> Any:
    if value is None:
        return None  # Documented older success response only carries code=0.
    data = _object(value, write=True)
    if data.get("errors") or type(data.get(count_key)) is not int or data[count_key] != expected:
        raise _shape_error(write=True)
    return data


def _response_error(code: int, *, status_code: int, write: bool) -> RagflowError:
    if code in (108, 109, 401, 403):
        return RagflowError("auth_error", status_code=status_code)
    if code == 404:
        kind = "not_found"
    elif code == 409:
        kind = "conflict"
    elif code == 429:
        kind = "rate_limit"
    elif code in (101, 102, 103, 400, 422):
        kind = "validation_error"
    else:
        kind = "upstream_error"
    transient = code in (100, 105, 106, 408, 425, 429) or code >= 500
    return RagflowError(
        kind, status_code=status_code, outcome_unknown=write, retryable=transient and not write
    )


def _retry_delay(attempt: int, retry_after: str | None) -> float | None:
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_date = parsedate_to_datetime(retry_after)
                delay = (retry_date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 0
        if not math.isfinite(delay) or delay > 5:
            # Do not sleep unboundedly, or retry earlier than the server permits.
            return None
        return max(0, delay)
    return 0.2 * 2**attempt + random.uniform(0, 0.1)


class RagflowClient:
    def __init__(
        self, config: RagflowConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        write: bool = False,
        health: bool = False,
        download: bool = False,
        upload: bool = False,
        **kwargs: Any,
    ) -> Any:
        for attempt in range(1 if write else 3):
            retry_after = None
            try:
                headers = {"Accept": "*/*" if download else "application/json"}
                if not health:
                    headers["Authorization"] = f"Bearer {self._config.api_key}"
                timeout = httpx.Timeout(
                    self._config.upload_timeout if upload else self._config.read_timeout,
                    connect=self._config.connect_timeout,
                )
                async with httpx.AsyncClient(
                    timeout=timeout,
                    transport=self._transport,
                    headers=headers,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        method, f"{self._config.base_url}/api/v1{path}", **kwargs
                    ) as response:
                        status = response.status_code
                        retry_after = response.headers.get("Retry-After")
                        if status in (401, 403):
                            raise _response_error(status, status_code=status, write=write)
                        if 300 <= status < 400:
                            raise RagflowError(
                                "provider_incompatible", status_code=status, outcome_unknown=write
                            )
                        if not 200 <= status < 300 and not (health and status == 500):
                            raise _response_error(status, status_code=status, write=write)
                        limit = MAX_DOWNLOAD_BYTES if download else MAX_JSON_BYTES
                        length = response.headers.get("Content-Length")
                        if length and length.isdigit() and int(length) > limit:
                            raise RagflowError(
                                "response_too_large", status_code=status, outcome_unknown=write
                            )
                        raw = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            if len(raw) + len(chunk) > limit:
                                raise RagflowError(
                                    "response_too_large", status_code=status, outcome_unknown=write
                                )
                            raw.extend(chunk)
                        content_type = (
                            response.headers.get("Content-Type", "application/octet-stream")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                if download:
                    # Error envelopes can arrive as HTTP 200 and even as octet-stream.
                    if bytes(raw).lstrip().startswith(b"{"):
                        try:
                            envelope = json.loads(raw)
                        except (ValueError, UnicodeError):
                            envelope = None
                        if isinstance(envelope, dict) and "code" in envelope:
                            self._unwrap(envelope, status, write=False)
                            if not response.headers.get("Content-Disposition", "").startswith(
                                "attachment"
                            ):
                                raise _shape_error()
                    if "json" in content_type and not response.headers.get(
                        "Content-Disposition", ""
                    ).startswith("attachment"):
                        raise _shape_error()
                    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", content_type):
                        content_type = "application/octet-stream"
                    return bytes(raw), content_type
                try:
                    envelope = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise _shape_error(write=write) from None
                if health:
                    fields = ("db", "redis", "doc_engine", "storage", "status")
                    if not isinstance(envelope, dict) or any(
                        envelope.get(field) not in ("ok", "nok") for field in fields
                    ):
                        raise _shape_error()
                    return {field: envelope[field] for field in fields}
                return self._unwrap(envelope, status, write=write)
            except httpx.RequestError:
                error = RagflowError("transport_error", outcome_unknown=write, retryable=not write)
            except RagflowError as exc:
                error = exc
            if not error.retryable or attempt == 2:
                raise error from None
            delay = _retry_delay(attempt, retry_after)
            if delay is None:
                raise error from None
            await asyncio.sleep(delay)
        raise RuntimeError("RAGFlow request loop ended unexpectedly.")

    @staticmethod
    def _unwrap(envelope: Any, status: int, *, write: bool) -> Any:
        if not isinstance(envelope, dict) or type(envelope.get("code")) is not int:
            raise _shape_error(write=write)
        if envelope["code"] != 0:
            raise _response_error(envelope["code"], status_code=status, write=write)
        return envelope.get("data")

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/system/healthz", health=True)

    async def list_datasets(self, **params: Any) -> list[dict[str, Any]]:
        return _objects(await self._request("GET", "/datasets", params=params))

    async def create_dataset(self, payload: dict) -> dict:
        return _object(
            await self._request("POST", "/datasets", json=payload, write=True),
            write=True,
            require_id=True,
        )

    async def update_dataset(self, dataset_id: str, payload: dict) -> dict | None:
        data = await self._request("PUT", f"/datasets/{_id(dataset_id)}", json=payload, write=True)
        return _object(data, write=True) if data is not None else None

    async def delete_datasets(self, ids: list[str]) -> Any:
        explicit_ids = _ids(ids)
        data = await self._request("DELETE", "/datasets", json={"ids": explicit_ids}, write=True)
        return _deleted(data, "success_count", len(set(explicit_ids)))

    async def list_documents(self, dataset_id: str, **params: Any) -> dict:
        return _page(
            await self._request("GET", f"/datasets/{_id(dataset_id)}/documents", params=params),
            "docs",
        )

    async def upload_document(
        self, dataset_id: str, file_path: Path, *, filename: str
    ) -> list[dict]:
        path = f"/datasets/{_id(dataset_id)}/documents"
        self._filename(filename)
        send_started = False
        try:
            with Path(file_path).open("rb") as stream:
                send_started = True
                data = await self._request(
                    "POST",
                    path,
                    files={"file": (filename, stream, "application/octet-stream")},
                    write=True,
                    upload=True,
                )
        except OSError:
            raise RagflowError("file_error", outcome_unknown=send_started) from None
        documents = _objects(data, write=True)
        if not documents:
            raise _shape_error(write=True)
        return documents

    @staticmethod
    def _filename(name: str) -> None:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.encode("utf-8")) > 255
            or re.search(r"[\x00-\x1f\x7f/\\]", name)
            or name in (".", "..")
        ):
            raise RagflowError("validation_error")

    async def create_document(self, dataset_id: str, *, name: str, url: str | None = None) -> Any:
        path = f"/datasets/{_id(dataset_id)}/documents"
        self._filename(name)
        if url is None:
            data = await self._request(
                "POST", path, params={"type": "empty"}, json={"name": name}, write=True
            )
        else:
            # The service layer authorizes the crawl target before this call.
            data = await self._request(
                "POST",
                path,
                params={"type": "web"},
                files={"name": (None, name), "url": (None, url)},
                write=True,
                upload=True,
            )
        return _object(data, write=True, require_id=True)

    async def update_document(self, dataset_id: str, document_id: str, payload: dict) -> Any:
        return await self._request(
            "PATCH",
            f"/datasets/{_id(dataset_id)}/documents/{_id(document_id)}",
            json=payload,
            write=True,
        )

    async def download_document(self, dataset_id: str, document_id: str) -> tuple[bytes, str]:
        return await self._request(
            "GET", f"/datasets/{_id(dataset_id)}/documents/{_id(document_id)}", download=True
        )

    async def delete_documents(self, dataset_id: str, ids: list[str]) -> Any:
        explicit_ids = _ids(ids)
        data = await self._request(
            "DELETE",
            f"/datasets/{_id(dataset_id)}/documents",
            json={"ids": explicit_ids},
            write=True,
        )
        return _deleted(data, "deleted", len(set(explicit_ids)))

    async def parse_documents(self, dataset_id: str, document_ids: list[str]) -> Any:
        return await self._request(
            "POST",
            f"/datasets/{_id(dataset_id)}/chunks",
            json={"document_ids": _ids(document_ids)},
            write=True,
        )

    async def cancel_parse(self, dataset_id: str, document_ids: list[str]) -> Any:
        return await self._request(
            "DELETE",
            f"/datasets/{_id(dataset_id)}/chunks",
            json={"document_ids": _ids(document_ids)},
            write=True,
        )

    async def ingest_documents(
        self, document_ids: list[str], *, run: str, delete: bool = False
    ) -> Any:
        if run not in ("1", "2") or type(delete) is not bool:
            raise RagflowError("validation_error")
        return await self._request(
            "POST",
            "/documents/ingest",
            json={"doc_ids": _ids(document_ids), "run": run, "delete": delete},
            write=True,
        )

    async def list_chunks(self, dataset_id: str, document_id: str, **params: Any) -> dict:
        return _page(
            await self._request("GET", self._chunks(dataset_id, document_id), params=params),
            "chunks",
        )

    async def get_chunk(self, dataset_id: str, document_id: str, chunk_id: str) -> dict:
        return _object(
            await self._request("GET", f"{self._chunks(dataset_id, document_id)}/{_id(chunk_id)}"),
            require_id=True,
        )

    async def add_chunk(self, dataset_id: str, document_id: str, payload: dict) -> Any:
        data = _object(
            await self._request(
                "POST", self._chunks(dataset_id, document_id), json=payload, write=True
            ),
            write=True,
        )
        _object(data.get("chunk"), write=True, require_id=True)
        return data

    async def update_chunk(
        self, dataset_id: str, document_id: str, chunk_id: str, payload: dict
    ) -> Any:
        return await self._request(
            "PATCH",
            f"{self._chunks(dataset_id, document_id)}/{_id(chunk_id)}",
            json=payload,
            write=True,
        )

    async def delete_chunks(self, dataset_id: str, document_id: str, chunk_ids: list[str]) -> Any:
        return await self._request(
            "DELETE",
            self._chunks(dataset_id, document_id),
            json={"chunk_ids": _ids(chunk_ids)},
            write=True,
        )

    async def set_chunk_availability(
        self, dataset_id: str, document_id: str, chunk_ids: list[str], available: bool
    ) -> Any:
        if type(available) is not bool:
            raise RagflowError("validation_error")
        return await self._request(
            "PATCH",
            self._chunks(dataset_id, document_id),
            json={"chunk_ids": _ids(chunk_ids), "available": available},
            write=True,
        )

    @staticmethod
    def _chunks(dataset_id: str, document_id: str) -> str:
        return f"/datasets/{_id(dataset_id)}/documents/{_id(document_id)}/chunks"

    async def retrieve(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise RagflowError("validation_error")
        _ids(payload.get("dataset_ids"))
        if "document_ids" in payload and payload["document_ids"]:
            _ids(payload["document_ids"])
        return _page(await self._request("POST", "/retrieval", json=payload), "chunks")
