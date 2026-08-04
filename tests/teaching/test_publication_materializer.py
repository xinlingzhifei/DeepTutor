from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
import hashlib

import pytest

from deeptutor.teaching.artifacts import (
    ArtifactManifestEntry,
    ClassroomArtifactManifest,
    classroom_artifact_key,
    temporary_artifact_key,
)
from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.object_store import (
    ClassroomArtifactPromotionService,
    LocalClassroomArtifactStore,
)
from deeptutor.teaching.services.publication_materializer import (
    ClassroomPublicationMaterializer,
    publication_manifest,
    publication_manifest_sha256,
)
from deeptutor.teaching.services.publications import (
    PublicationMaterializationPlan,
    PublicationMediaSource,
    publication_media_manifest_sha256,
)
from tests.teaching_contract_fixtures import valid_classroom_document


async def _body(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


async def _read_all(body: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in body])


class _TrackingStore(LocalClassroomArtifactStore):
    def __init__(self, root, tenant_id: str) -> None:
        super().__init__(root, tenant_id)
        self.reconcile_calls: list[str] = []

    async def reconcile_verified(
        self,
        key: str,
        sha256: str,
        size: int,
        *,
        content_type: str,
        ownership_token: str,
    ):
        self.reconcile_calls.append(key)
        return await super().reconcile_verified(
            key,
            sha256,
            size,
            content_type=content_type,
            ownership_token=ownership_token,
        )


class _StoreProvider:
    def __init__(self, store: _TrackingStore) -> None:
        self.store = store

    async def store_for_tenant(self, tenant_id: str):
        assert tenant_id == self.store.tenant_id
        return self.store


def _document(
    media: bytes,
    *,
    download_path: str = (
        "/api/yfeistai/v1/artifacts/content-job-a/media/voice.mp3"
    ),
) -> bytes:
    payload = valid_classroom_document()
    payload["classroom_id"] = "asset-a"
    payload["classroom_version_id"] = "source-version-a"
    payload["export_manifest"] = []
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    manifest[0].update(
        sha256=hashlib.sha256(media).hexdigest(),
        size_bytes=len(media),
        temporary_download_path=download_path,
    )
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list) and isinstance(scenes[0], dict)
    scenes[0]["type"] = "interactive"
    scenes[0]["content"] = {
        "type": "interactive",
        "html": "<div><img data-media-id='media-1' alt='Narration'></div>",
        "bridge_version": "1.0",
        "sandbox": {"allow_scripts": True, "allow_same_origin": False},
    }
    provisional = ClassroomDocument.model_validate(payload)
    unhashed = provisional.model_dump(mode="json", by_alias=True, exclude_none=True)
    unhashed.pop("fileSha256")
    payload["file_sha256"] = hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    return canonical_json_bytes(ClassroomDocument.model_validate(payload))


def _plan(document: bytes, source: PublicationMediaSource) -> PublicationMaterializationPlan:
    plan = PublicationMaterializationPlan(
        reservation_id="publication-reservation-a",
        tenant_id="tenant-a",
        asset_id="asset-a",
        review_id="review-a",
        draft_id="draft-a",
        draft_revision=5,
        source_version_id="source-version-a",
        version_id="published-version-a",
        version_number=2,
        document=document,
        document_sha256=hashlib.sha256(document).hexdigest(),
        validation_report_sha256="b" * 64,
        media_manifest_sha256=publication_media_manifest_sha256(document),
        manifest_sha256="",
        media=(source,),
        status="prepared",
    )
    return replace(
        plan,
        manifest_sha256=publication_manifest_sha256(publication_manifest(plan)),
    )


def _source(document: bytes, *, kind: str, object_key: str, ownership=None, revision=None):
    parsed = ClassroomDocument.model_validate_json(document)
    item = parsed.media_manifest[0]
    return PublicationMediaSource(
        media_id=item.media_id,
        relative_name=item.relative_path,
        mime_type=item.mime_type,
        sha256=item.sha256,
        size_bytes=item.size_bytes,
        source_kind=kind,
        object_key=object_key,
        ownership_token=ownership,
        object_revision=revision,
    )


def test_media_manifest_hash_covers_exact_canonical_document_manifest() -> None:
    media = b"ID3-media"
    first = _document(media)
    second = _document(
        media,
        download_path="/api/yfeistai/v1/artifacts/content-job-b/media/voice.mp3",
    )

    assert publication_media_manifest_sha256(first) != publication_media_manifest_sha256(second)


def test_publication_media_source_rejects_an_unknown_source_kind() -> None:
    document = _document(b"ID3-media")

    with pytest.raises(ValueError, match="publication source kind is unsupported"):
        _source(
            document,
            kind="external_object",
            object_key="tenants/tenant-a/temporary/untrusted/media/voice.mp3",
        )


@pytest.mark.asyncio
async def test_version_artifact_is_copied_without_mutable_upload_reconciliation(
    tmp_path,
) -> None:
    media = b"ID3-generated-media"
    document = _document(media)
    store = _TrackingStore(tmp_path, "tenant-a")
    source_manifest = ClassroomArtifactManifest(
        tenant_id="tenant-a",
        job_id="generation-a",
        asset_id="asset-a",
        version=1,
        entries=(
            ArtifactManifestEntry(
                relative_name="classroom.json",
                content_type="application/json",
                sha256=hashlib.sha256(document).hexdigest(),
                size=len(document),
            ),
            ArtifactManifestEntry(
                relative_name="media/voice.mp3",
                content_type="audio/mpeg",
                sha256=hashlib.sha256(media).hexdigest(),
                size=len(media),
            ),
        ),
    )
    await ClassroomArtifactPromotionService(store).promote(
        source_manifest,
        {"classroom.json": _body(document), "media/voice.mp3": _body(media)},
    )
    source_key = classroom_artifact_key("tenant-a", "asset-a", 1, "media/voice.mp3")

    result = await ClassroomPublicationMaterializer(_StoreProvider(store)).materialize(
        _plan(document, _source(document, kind="version_artifact", object_key=source_key))
    )

    assert store.reconcile_calls == []
    published = next(item for item in result.artifacts if item.media_id == "media-1")
    assert await _read_all(await store.open(published.object_key)) == media


@pytest.mark.asyncio
async def test_draft_upload_keeps_ownership_receipt_reconciliation(tmp_path) -> None:
    media = b"ID3-uploaded-media"
    document = _document(media)
    store = _TrackingStore(tmp_path, "tenant-a")
    key = temporary_artifact_key("tenant-a", "draft-asset-a", "media/voice.mp3")
    stored = await store.put_verified(
        key,
        _body(media),
        hashlib.sha256(media).hexdigest(),
        len(media),
        content_type="audio/mpeg",
        ownership_token="c" * 32,
    )
    revision = stored.revision or stored.version_id
    assert revision is not None

    await ClassroomPublicationMaterializer(_StoreProvider(store)).materialize(
        _plan(
            document,
            _source(
                document,
                kind="draft_upload",
                object_key=key,
                ownership="c" * 32,
                revision=revision,
            ),
        )
    )

    assert store.reconcile_calls == [key]
