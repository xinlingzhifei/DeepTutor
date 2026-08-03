"""Canonical, tamper-evident identity for one grounded retrieval view.

The digest describes only the context and provenance returned by one search.
It is deliberately not presented as a global index version.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

RETRIEVAL_CONTEXT_KIND = "retrieval_context"

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


def _provenance(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    normalized: list[dict[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        item: dict[str, object] = {}
        for key in _PROVENANCE_KEYS:
            raw = candidate.get(key)
            if isinstance(raw, bool) or raw is None:
                continue
            if isinstance(raw, (int, float)):
                item[key] = raw
                continue
            text = str(raw).strip()
            if text:
                item[key] = text
        if item:
            normalized.append(item)
    return normalized


def canonical_retrieval_fragments(result: dict[str, Any]) -> list[dict[str, object]]:
    """Return context fragments whose text is known to be retrieval output."""

    raw_sources = result.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    fragments: list[dict[str, object]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        content = str(
            source.get("content") or source.get("text") or source.get("snippet") or ""
        ).strip()
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

    if result.get("content_kind") != RETRIEVAL_CONTEXT_KIND:
        return []
    content = str(result.get("content") or "").strip()
    if not content:
        return []
    provenance = _provenance(result.get("retrieval_provenance") or sources)
    if not provenance:
        return []
    return [{"content": content, "provenance": provenance}]


def canonical_retrieval_view_payload(result: dict[str, Any]) -> dict[str, object]:
    return {
        "content_kind": RETRIEVAL_CONTEXT_KIND,
        "fragments": canonical_retrieval_fragments(result),
        "mode": str(result.get("mode") or ""),
        "provider": str(result.get("provider") or "").strip(),
        "schema_version": 1,
    }


def canonical_retrieval_view_signature(result: dict[str, Any]) -> str:
    payload = canonical_retrieval_view_payload(result)
    if not payload["provider"] or not payload["fragments"]:
        return ""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


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
    "stamp_retrieval_view_signature",
]
