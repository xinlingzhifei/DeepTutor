from __future__ import annotations

import math

import pytest

from deeptutor.services.rag.retrieval_view import (
    bounded_retrieval_view,
    canonical_retrieval_view_signature,
)


def _result(**changes):
    result = {
        "provider": "llamaindex",
        "sources": [
            {"chunk_id": "chunk-1", "content": "bounded context", "source": "book.pdf"}
        ],
    }
    result.update(changes)
    return result


@pytest.mark.parametrize(
    "result",
    (
        _result(sources=[
            {"chunk_id": str(index), "content": "x", "source": "book.pdf"}
            for index in range(21)
        ]),
        _result(sources=[
            {"chunk_id": "chunk", "content": "x" * 20_001, "source": "book.pdf"}
        ]),
        _result(sources=[
            {"chunk_id": "chunk", "content": "x", "source": "book.pdf", "page": math.inf}
        ]),
        {
            "provider": "lightrag-server",
            "content_kind": "retrieval_context",
            "content": "context",
            "sources": [],
            "retrieval_provenance": [
                {"id": str(index)} for index in range(17)
            ],
        },
        _result(sources=[
            {"chunk_id": "chunk", "content": "x", "source": "x" * 513}
        ]),
        _result(sources=[
            {"chunk_id": "chunk", "content": "x", "source": "bad\npath"}
        ]),
    ),
)
def test_retrieval_view_rejects_untrusted_values_over_hard_limits(result) -> None:
    with pytest.raises(ValueError, match="invalid retrieval view"):
        canonical_retrieval_view_signature(result)


def test_retrieval_view_never_stringifies_untrusted_objects() -> None:
    class _Malicious:
        called = False

        def __str__(self) -> str:
            self.called = True
            raise AssertionError("must not stringify")

    malicious = _Malicious()
    result = _result(
        sources=[
            {
                "chunk_id": "chunk",
                "content": "context",
                "source": malicious,
            }
        ]
    )

    with pytest.raises(ValueError, match="invalid retrieval view"):
        canonical_retrieval_view_signature(result)

    assert malicious.called is False


def test_bounded_view_signature_matches_exact_path_free_fragments() -> None:
    bounded = bounded_retrieval_view(
        _result(
            sources=[
                {
                    "chunk_id": "chunk",
                    "content": "bounded context",
                    "file_path": "C:/private/book.pdf",
                }
            ]
        )
    )

    assert bounded["retrieval_view_signature"] == canonical_retrieval_view_signature(
        bounded
    )
    assert bounded["sources"][0]["content"] == "bounded context"
    assert "file_path" not in bounded["sources"][0]
    assert "private" not in str(bounded)
