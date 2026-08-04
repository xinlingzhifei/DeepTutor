from __future__ import annotations

import hashlib
import json

import pytest

from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from tests.teaching.test_contracts import valid_classroom_document


def _canonical_document() -> tuple[ClassroomDocument, bytes, str]:
    raw = ClassroomDocument.model_validate(valid_classroom_document()).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    raw["mediaManifest"] = []
    without_hash = dict(raw)
    without_hash.pop("fileSha256")
    raw["fileSha256"] = hashlib.sha256(canonical_json_bytes(without_hash)).hexdigest()
    document = ClassroomDocument.model_validate(raw)
    body = canonical_json_bytes(document)
    media_sha256 = hashlib.sha256(canonical_json_bytes([])).hexdigest()
    return document, body, media_sha256


class _Store:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.opened: list[str] = []

    async def open(self, key: str):
        self.opened.append(key)

        async def chunks():
            midpoint = len(self.body) // 2
            yield self.body[:midpoint]
            yield self.body[midpoint:]

        return chunks()


@pytest.mark.asyncio
async def test_promoted_document_is_read_back_and_bound_to_exact_canonical_bytes() -> None:
    from deeptutor.teaching.worker import _load_promoted_classroom_document

    document, body, media_sha256 = _canonical_document()
    store = _Store(body)

    loaded = await _load_promoted_classroom_document(
        store,  # type: ignore[arg-type]
        object_key="tenants/tenant-1/classrooms/classroom-1/versions/1/classroom.json",
        expected_sha256=hashlib.sha256(body).hexdigest(),
        expected_size=len(body),
        expected_media_manifest_sha256=media_sha256,
        expected_classroom_id=document.classroom_id,
        expected_classroom_version_id=document.classroom_version_id,
    )

    assert loaded == body.decode("utf-8")
    assert store.opened == ["tenants/tenant-1/classrooms/classroom-1/versions/1/classroom.json"]


@pytest.mark.asyncio
async def test_promoted_document_rejects_noncanonical_or_hash_divergent_storage() -> None:
    from deeptutor.teaching.artifact_validation import ArtifactValidationError
    from deeptutor.teaching.worker import _load_promoted_classroom_document

    document, body, media_sha256 = _canonical_document()
    noncanonical = json.dumps(json.loads(body), ensure_ascii=False, indent=2).encode()

    with pytest.raises(ArtifactValidationError, match="hash_invalid"):
        await _load_promoted_classroom_document(
            _Store(noncanonical),  # type: ignore[arg-type]
            object_key="tenants/tenant-1/classrooms/classroom-1/versions/1/classroom.json",
            expected_sha256=hashlib.sha256(noncanonical).hexdigest(),
            expected_size=len(noncanonical),
            expected_media_manifest_sha256=media_sha256,
            expected_classroom_id=document.classroom_id,
            expected_classroom_version_id=document.classroom_version_id,
        )


def test_generation_repository_requires_the_same_canonical_document_binding() -> None:
    from deeptutor.teaching.repositories.jobs import (
        _validate_generation_document_payload,
    )

    document, body, media_sha256 = _canonical_document()

    parsed = _validate_generation_document_payload(
        body.decode(),
        classroom_id=document.classroom_id,
        classroom_version_id=document.classroom_version_id,
        document_sha256=hashlib.sha256(body).hexdigest(),
        media_manifest_sha256=media_sha256,
    )

    assert parsed == document
    with pytest.raises(ValueError, match="binding"):
        _validate_generation_document_payload(
            body.decode(),
            classroom_id="another-classroom",
            classroom_version_id=document.classroom_version_id,
            document_sha256=hashlib.sha256(body).hexdigest(),
            media_manifest_sha256=media_sha256,
        )
