"""Object-store materialization for one exact reviewed classroom draft."""

from __future__ import annotations

import hashlib
import hmac
from typing import AsyncIterator, Protocol

from deeptutor.teaching.artifacts import (
    ArtifactManifestEntry,
    ClassroomArtifactManifest,
)
from deeptutor.teaching.contracts import canonical_json_bytes
from deeptutor.teaching.object_store import (
    ClassroomArtifactPromotionService,
    ClassroomArtifactStore,
)
from deeptutor.teaching.services.publications import (
    ConfirmedPublicationMaterialization,
    MaterializedPublicationArtifact,
    PublicationConflict,
    PublicationMaterializationPlan,
    PublicationPersistenceError,
    publication_media_manifest_sha256,
    validated_publication_document,
)


class PublicationStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> ClassroomArtifactStore: ...


async def _document_body(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def publication_manifest(
    plan: PublicationMaterializationPlan,
) -> ClassroomArtifactManifest:
    entries = (
        ArtifactManifestEntry(
            relative_name="classroom.json",
            content_type="application/json",
            sha256=plan.document_sha256,
            size=len(plan.document),
        ),
        *(
            ArtifactManifestEntry(
                relative_name=media.relative_name,
                content_type=media.mime_type,
                sha256=media.sha256,
                size=media.size_bytes,
            )
            for media in plan.media
        ),
    )
    return ClassroomArtifactManifest(
        tenant_id=plan.tenant_id,
        job_id=plan.reservation_id,
        asset_id=plan.asset_id,
        version=plan.version_number,
        entries=entries,
    )


def publication_manifest_document(manifest: ClassroomArtifactManifest) -> bytes:
    return canonical_json_bytes(
        {
            "tenantId": manifest.tenant_id,
            "jobId": manifest.job_id,
            "assetId": manifest.asset_id,
            "version": manifest.version,
            "entries": [
                {
                    "relativeName": entry.relative_name,
                    "contentType": entry.content_type,
                    "sha256": entry.sha256,
                    "size": entry.size,
                }
                for entry in manifest.entries
            ],
        }
    )


def publication_manifest_sha256(manifest: ClassroomArtifactManifest) -> str:
    return hashlib.sha256(publication_manifest_document(manifest)).hexdigest()


class ClassroomPublicationMaterializer:
    """Promote canonical draft bytes and receipt-verified media exactly once."""

    def __init__(self, stores: PublicationStoreProvider) -> None:
        self._stores = stores

    @staticmethod
    def _matches_media_receipt(artifact, media) -> bool:
        revision = artifact.revision or artifact.version_id
        return (
            media.ownership_token is not None
            and media.object_revision is not None
            and artifact.key == media.object_key
            and hmac.compare_digest(artifact.sha256, media.sha256)
            and artifact.size == media.size_bytes
            and artifact.content_type == media.mime_type
            and artifact.ownership_token is not None
            and hmac.compare_digest(
                artifact.ownership_token,
                media.ownership_token,
            )
            and revision is not None
            and hmac.compare_digest(revision, media.object_revision)
        )

    async def materialize(
        self,
        plan: PublicationMaterializationPlan,
    ) -> ConfirmedPublicationMaterialization:
        if hashlib.sha256(plan.document).hexdigest() != plan.document_sha256:
            raise PublicationPersistenceError("reviewed draft document is invalid")
        document = validated_publication_document(plan.document)
        expected_media = tuple(
            (
                item.media_id,
                item.relative_path,
                item.mime_type,
                item.sha256,
                item.size_bytes,
            )
            for item in document.media_manifest
        )
        actual_media = tuple(
            (
                item.media_id,
                item.relative_name,
                item.mime_type,
                item.sha256,
                item.size_bytes,
            )
            for item in plan.media
        )
        if (
            actual_media != expected_media
            or publication_media_manifest_sha256(plan.document) != plan.media_manifest_sha256
        ):
            raise PublicationPersistenceError("reviewed draft media manifest is invalid")
        manifest = publication_manifest(plan)
        manifest.validate_for_tenant(plan.tenant_id)
        if publication_manifest_sha256(manifest) != plan.manifest_sha256:
            raise PublicationPersistenceError("publication manifest is invalid")

        store = await self._stores.store_for_tenant(plan.tenant_id)
        confirmed = await store.confirmed_publish(manifest)
        if confirmed is None:
            bodies: dict[str, AsyncIterator[bytes]] = {
                "classroom.json": _document_body(plan.document)
            }
            for media in plan.media:
                if media.source_kind == "draft_upload":
                    if media.ownership_token is None:
                        raise PublicationConflict("draft media receipt conflicts")
                    source = await store.reconcile_verified(
                        media.object_key,
                        media.sha256,
                        media.size_bytes,
                        content_type=media.mime_type,
                        ownership_token=media.ownership_token,
                    )
                    if source is None or not self._matches_media_receipt(source, media):
                        raise PublicationConflict("draft media receipt conflicts")
                bodies[media.relative_name] = await store.open(media.object_key)
            try:
                await ClassroomArtifactPromotionService(store).promote(
                    manifest,
                    bodies,
                )
            except Exception:
                confirmed = await store.confirmed_publish(manifest)
                if confirmed is None:
                    raise
            else:
                confirmed = await store.confirmed_publish(manifest)
        if confirmed is None:
            raise PublicationPersistenceError("publication commit is unavailable")

        media_by_name = {item.relative_name: item for item in plan.media}
        artifacts: list[MaterializedPublicationArtifact] = []
        for entry, artifact in zip(manifest.entries, confirmed, strict=True):
            if (
                artifact.sha256 != entry.sha256
                or artifact.size != entry.size
                or artifact.content_type != entry.content_type
            ):
                raise PublicationPersistenceError("confirmed publication artifact is invalid")
            media = media_by_name.get(entry.relative_name)
            artifacts.append(
                MaterializedPublicationArtifact(
                    relative_name=entry.relative_name,
                    object_key=artifact.key,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size,
                    mime_type=artifact.content_type,
                    artifact_kind=("dsl_json" if media is None else "media"),
                    media_id=media.media_id if media is not None else None,
                )
            )
        result = ConfirmedPublicationMaterialization(
            manifest_sha256=plan.manifest_sha256,
            media_manifest_sha256=plan.media_manifest_sha256,
            artifacts=tuple(artifacts),
        )
        result.document
        return result


__all__ = [
    "ClassroomPublicationMaterializer",
    "PublicationStoreProvider",
    "publication_manifest",
    "publication_manifest_document",
    "publication_manifest_sha256",
]
