"""Canonical, tamper-evident identity for one grounded retrieval view.

The digest describes only the context and provenance returned by one search.
It is deliberately not presented as a global index version.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

RETRIEVAL_CONTEXT_KIND = "retrieval_context"
MAX_RETRIEVAL_SOURCES = 20
MAX_RETRIEVAL_CONTENT_CHARS = 20_000
MAX_PROVENANCE_ROWS = 16
MAX_PROVENANCE_FIELD_CHARS = 512
MAX_PROVIDER_CHARS = 64
MAX_MODE_CHARS = 64

_PROVENANCE_KEYS = (
    "chunk_id",
    "doc_id",
    "document_id",
    "file_path",
    "id",
    "kind",
    "page",
    "reference_id",
    "section",
    "source",
    "source_id",
    "storage_view",
    "title",
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _invalid() -> ValueError:
    return ValueError("invalid retrieval view")


def _text(value: object, *, limit: int, allow_newlines: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise _invalid()
    if not allow_newlines and any(character in value for character in "\r\n"):
        raise _invalid()
    return value.strip()


def _path_free_label(value: object) -> str:
    text = _text(value, limit=MAX_PROVENANCE_FIELD_CHARS)
    return text.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _provenance(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        raise _invalid()
    if len(candidates) > MAX_PROVENANCE_ROWS:
        raise _invalid()
    normalized: list[dict[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise _invalid()
        item: dict[str, object] = {}
        for key in _PROVENANCE_KEYS:
            raw = candidate.get(key)
            if raw is None:
                continue
            if isinstance(raw, bool):
                raise _invalid()
            if isinstance(raw, int):
                item[key] = raw
                continue
            if isinstance(raw, float):
                if not math.isfinite(raw):
                    raise _invalid()
                item[key] = raw
                continue
            text = _text(raw, limit=MAX_PROVENANCE_FIELD_CHARS)
            if text:
                item[key] = text
        if item:
            normalized.append(item)
    return normalized


def canonical_retrieval_fragments(result: dict[str, Any]) -> list[dict[str, object]]:
    """Return context fragments whose text is known to be retrieval output."""

    if not isinstance(result, dict):
        raise _invalid()
    raw_sources = result.get("sources")
    if raw_sources is None:
        sources = []
    elif isinstance(raw_sources, list):
        sources = raw_sources
    else:
        raise _invalid()
    if len(sources) > MAX_RETRIEVAL_SOURCES:
        raise _invalid()
    fragments: list[dict[str, object]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise _invalid()
        content_value: object | None = None
        for key in ("content", "text", "snippet"):
            if key in source:
                content_value = source[key]
                break
        if content_value is None:
            continue
        content = _text(
            content_value,
            limit=MAX_RETRIEVAL_CONTENT_CHARS,
            allow_newlines=True,
        )
        if not content:
            continue
        provenance = _provenance(source)
        if not provenance:
            continue
        fragments.append(
            {
                "content": content,
                "provenance": provenance,
            }
        )
    if fragments:
        return fragments

    content_kind = result.get("content_kind")
    if content_kind != RETRIEVAL_CONTEXT_KIND:
        return []
    content_value = result.get("content")
    if content_value is None:
        return []
    content = _text(
        content_value,
        limit=MAX_RETRIEVAL_CONTENT_CHARS,
        allow_newlines=True,
    )
    if not content:
        return []
    provenance = _provenance(result.get("retrieval_provenance") or sources)
    if not provenance:
        return []
    return [{"content": content, "provenance": provenance}]


def canonical_retrieval_view_payload(result: dict[str, Any]) -> dict[str, object]:
    provider = _text(result.get("provider"), limit=MAX_PROVIDER_CHARS)
    raw_mode = result.get("mode", "")
    mode = _text(raw_mode, limit=MAX_MODE_CHARS) if raw_mode is not None else ""
    return {
        "content_kind": RETRIEVAL_CONTEXT_KIND,
        "fragments": canonical_retrieval_fragments(result),
        "mode": mode,
        "provider": provider,
        "schema_version": 1,
    }


def canonical_retrieval_view_signature(result: dict[str, Any]) -> str:
    payload = canonical_retrieval_view_payload(result)
    if not payload["provider"] or not payload["fragments"]:
        return ""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def bounded_retrieval_view(result: dict[str, Any]) -> dict[str, Any]:
    """Return the exact path-free bounded view allowed past the RAG boundary."""

    payload = canonical_retrieval_view_payload(result)
    safe_sources: list[dict[str, object]] = []
    for position, fragment in enumerate(payload["fragments"]):
        content = fragment["content"]
        provenance = fragment["provenance"]
        provenance_json = _canonical_json(provenance)
        document_digest = hashlib.sha256(provenance_json.encode("utf-8")).hexdigest()
        fragment_digest = hashlib.sha256(
            f"{position}\0{content}\0{document_digest}".encode("utf-8")
        ).hexdigest()
        source: dict[str, object] = {
            "chunk_id": f"retrieval-fragment-{fragment_digest}",
            "content": content,
            "document_id": f"retrieval-document-{document_digest}",
        }
        first = provenance[0]
        page = first.get("page")
        if isinstance(page, (int, float)) and not isinstance(page, bool):
            source["page"] = page
        label = first.get("title") or first.get("section")
        if label:
            safe_label = _path_free_label(label)
            if safe_label:
                source["title"] = safe_label
        safe_sources.append(source)
    bounded: dict[str, Any] = {
        "content_kind": RETRIEVAL_CONTEXT_KIND,
        "mode": payload["mode"],
        "provider": payload["provider"],
        "sources": safe_sources,
    }
    bounded["retrieval_view_signature"] = canonical_retrieval_view_signature(bounded)
    return bounded


def stamp_retrieval_view_signature(result: dict[str, Any]) -> dict[str, Any]:
    signature = canonical_retrieval_view_signature(result)
    if signature:
        result["retrieval_view_signature"] = signature
    return result


__all__ = [
    "RETRIEVAL_CONTEXT_KIND",
    "canonical_retrieval_fragments",
    "canonical_retrieval_view_payload",
    "canonical_retrieval_view_signature",
    "bounded_retrieval_view",
    "stamp_retrieval_view_signature",
]
