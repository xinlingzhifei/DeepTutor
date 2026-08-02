"""Small Task 7 fixtures kept independent from the large contract test module."""

from __future__ import annotations

from tests.teaching.test_contracts import (  # type: ignore[import-not-found]
    valid_classroom_document,
    valid_generation_request,
    valid_outline_bundle,
)


def valid_content_generation_request() -> dict[str, object]:
    from deeptutor.teaching.contracts import canonical_outline_sha256

    request = valid_generation_request()
    outline = valid_outline_bundle()
    request["phase"] = "content"
    request["confirmed_outline"] = outline
    request["confirmed_outline_sha256"] = canonical_outline_sha256(outline)
    return request


__all__ = [
    "valid_classroom_document",
    "valid_content_generation_request",
    "valid_generation_request",
]
