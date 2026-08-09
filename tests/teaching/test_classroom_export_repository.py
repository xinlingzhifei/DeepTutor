from __future__ import annotations

import hashlib

import pytest

from deeptutor.teaching.contracts import (
    ClassroomDocument,
    ExportPolicy,
    ExportRequest,
    canonical_json_bytes,
)
from deeptutor.teaching.models.classrooms import (
    ClassroomExport,
    ClassroomPublicationMaterialization,
)
from deeptutor.teaching.repositories.exports import (
    SqlAlchemyClassroomExportRepository,
    _source_media,
)
from deeptutor.teaching.repositories.jobs import (
    GenerationJobRequest,
    SqlAlchemyGenerationJobRepository,
)
from tests.teaching.test_contracts import valid_classroom_document


def test_published_version_media_comes_from_exact_finalized_receipts() -> None:
    body = b"ID3-published-media"
    payload = valid_classroom_document()
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    manifest[0]["sha256"] = hashlib.sha256(body).hexdigest()
    manifest[0]["size_bytes"] = len(body)
    document = ClassroomDocument.model_validate(payload)
    confirmed = canonical_json_bytes(
        [
            {
                "relativeName": "classroom.json",
                "objectKey": "tenants/tenant-a/classrooms/asset-a/versions/2/classroom.json",
                "sha256": "a" * 64,
                "sizeBytes": 100,
                "mimeType": "application/json",
                "artifactKind": "dsl_json",
                "mediaId": None,
            },
            {
                "relativeName": document.media_manifest[0].relative_path,
                "objectKey": "tenants/tenant-a/classrooms/asset-a/versions/2/media/voice.mp3",
                "sha256": hashlib.sha256(body).hexdigest(),
                "sizeBytes": len(body),
                "mimeType": "audio/mpeg",
                "artifactKind": "media",
                "mediaId": document.media_manifest[0].media_id,
            },
        ]
    ).decode()
    publication = ClassroomPublicationMaterialization(
        status="finalized",
        confirmed_artifacts=confirmed,
    )

    media = _source_media(document, publication=publication)

    assert len(media) == 1
    assert media[0].media_id == document.media_manifest[0].media_id
    assert media[0].object_key.endswith("/media/voice.mp3")


class _EmptyResult:
    def one_or_none(self):
        return None


class _StatementCaptureSession:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _EmptyResult()


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", ["_draft_parts", "_version_parts"])
async def test_export_source_queries_exclude_student_classroom_assets(loader: str) -> None:
    repository = object.__new__(SqlAlchemyClassroomExportRepository)
    repository._tenant_id = "tenant-a"
    session = _StatementCaptureSession()

    assert await getattr(repository, loader)(session, "source-a") is None

    sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "student_classroom_assets" in sql
    assert "NOT (EXISTS" in sql


def _job_request(
    exported: ClassroomExport,
    *,
    document_sha256: str | None = None,
) -> GenerationJobRequest:
    request = ExportRequest(
        schema_version="1.0",
        tenant_id="tenant-a",
        job_id=exported.id,
        idempotency_key=exported.id,
        classroom_document_sha256=(
            document_sha256 or exported.input_document_sha256 or ""
        ),
        media_manifest_sha256=exported.input_media_manifest_sha256 or "",
        format="pptx",
        language="zh-CN",
        export_policy=ExportPolicy(
            include_source_attribution=True,
            allow_external_links=False,
        ),
    )
    payload = canonical_json_bytes(request).decode()
    return GenerationJobRequest(
        tenant_id="tenant-a",
        job_id=exported.id,
        job_kind="export",
        phase="export",
        export_format="pptx",
        priority="teacher",
        quota_units=1,
        actor_id="teacher-a",
        owner_id="teacher-a",
        visibility="private",
        request_id=exported.id,
        idempotency_key=exported.id,
        request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        request_payload=payload,
    )


@pytest.mark.asyncio
async def test_atomic_job_binding_compares_the_frozen_request_to_export_pins() -> None:
    exported = ClassroomExport(
        id="export-fixed",
        tenant_id="tenant-a",
        classroom_id="asset-a",
        classroom_version_id="version-a",
        classroom_draft_id=None,
        draft_revision=None,
        generation_job_id=None,
        export_format="pptx",
        input_document_sha256="a" * 64,
        input_media_manifest_sha256="b" * 64,
        idempotency_key="public-key",
        request_sha256="c" * 64,
        input_manifest_object_key="tenants/tenant-a/export-inputs/export-fixed/manifest.json",
        input_manifest_sha256="d" * 64,
        status="input_ready",
        created_by="teacher-a",
    )

    class Session:
        async def scalar(self, _statement):
            return exported

    with pytest.raises(ValueError, match="does not match"):
        await SqlAlchemyGenerationJobRepository._bind_export_job(
            Session(),  # type: ignore[arg-type]
            _job_request(exported, document_sha256="e" * 64),
            exported.id,
        )
    assert exported.generation_job_id is None

    await SqlAlchemyGenerationJobRepository._bind_export_job(
        Session(),  # type: ignore[arg-type]
        _job_request(exported),
        exported.id,
    )
    assert exported.generation_job_id == exported.id
    assert exported.status == "quota_reserved"
