from __future__ import annotations

import asyncio
import hashlib
import json

import pytest


def test_export_input_is_pinned_and_materialization_is_idempotent() -> None:
    from deeptutor.teaching.artifact_validation import validate_export_result

    request = {
        "schema_version": "1.0",
        "tenant_id": "tenant-a",
        "job_id": "export-1",
        "idempotency_key": "export-key-1",
        "classroom_document_sha256": "a" * 64,
        "media_manifest_sha256": "b" * 64,
        "format": "pptx",
        "language": "zh-CN",
        "export_policy": {
            "include_source_attribution": True,
            "allow_external_links": False,
        },
    }
    payload = {
        "status": "succeeded",
        "format": "pptx",
        "artifact": {
            "relativePath": "exports/classroom.pptx",
            "sha256": "c" * 64,
            "bytes": 128,
            "mime": (
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
            "downloadPath": (
                "/api/yfeistai/v1/artifacts/export-1/exports/classroom.pptx"
            ),
            "expiresAt": "2030-07-30T09:00:00Z",
        },
    }
    validated = validate_export_result(
        tenant_id="tenant-a",
        job_id="export-1",
        request_payload=request,
        result_payload=payload,
    )

    assert validated.input_classroom_document_sha256 == "a" * 64
    assert validated.input_media_manifest_sha256 == "b" * 64
    assert validated.artifact.relative_name == "exports/classroom.pptx"


def test_export_result_artifact_must_match_requested_format() -> None:
    from deeptutor.teaching.artifact_validation import (
        ArtifactValidationError,
        validate_export_result,
    )

    request = {
        "schema_version": "1.0",
        "tenant_id": "tenant-a",
        "job_id": "export-1",
        "idempotency_key": "export-key-1",
        "classroom_document_sha256": "a" * 64,
        "media_manifest_sha256": "b" * 64,
        "format": "pptx",
        "language": "zh-CN",
        "export_policy": {
            "include_source_attribution": True,
            "allow_external_links": False,
        },
    }
    payload = {
        "status": "succeeded",
        "format": "pptx",
        "artifact": {
            "relativePath": "exports/classroom.zip",
            "sha256": "c" * 64,
            "bytes": 128,
            "mime": "application/zip",
            "downloadPath": ("/api/yfeistai/v1/artifacts/export-1/exports/classroom.zip"),
            "expiresAt": "2030-07-30T09:00:00Z",
        },
    }

    with pytest.raises(ArtifactValidationError, match="artifact_invalid"):
        validate_export_result(
            tenant_id="tenant-a",
            job_id="export-1",
            request_payload=request,
            result_payload=payload,
        )


def test_offline_html_export_normalizes_safe_mime_parameters() -> None:
    from deeptutor.teaching.artifact_validation import validate_export_result

    request = {
        "schema_version": "1.0",
        "tenant_id": "tenant-a",
        "job_id": "export-1",
        "idempotency_key": "export-key-1",
        "classroom_document_sha256": "a" * 64,
        "media_manifest_sha256": "b" * 64,
        "format": "offline_html",
        "language": "zh-CN",
        "export_policy": {
            "include_source_attribution": True,
            "allow_external_links": False,
        },
    }
    payload = {
        "status": "succeeded",
        "format": "offline_html",
        "artifact": {
            "relativePath": "exports/classroom.html",
            "sha256": "c" * 64,
            "bytes": 128,
            "mime": "text/html; charset=utf-8",
            "downloadPath": ("/api/yfeistai/v1/artifacts/export-1/exports/classroom.html"),
            "expiresAt": "2030-07-30T09:00:00Z",
        },
    }

    validated = validate_export_result(
        tenant_id="tenant-a",
        job_id="export-1",
        request_payload=request,
        result_payload=payload,
    )

    assert validated.artifact.content_type == "text/html"


def test_export_request_materializer_is_called_only_once_for_same_job() -> None:
    from deeptutor.teaching.export_worker import ExportInputSnapshot

    payload = json.dumps(
        {
            "schemaVersion": "1.0",
            "tenantId": "tenant-a",
            "jobId": "export-1",
            "idempotencyKey": "key-1",
            "classroomDocumentSha256": "a" * 64,
            "mediaManifestSha256": "b" * 64,
            "format": "pptx",
            "language": "zh-CN",
            "exportPolicy": {
                "includeSourceAttribution": True,
                "allowExternalLinks": False,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    snapshot = ExportInputSnapshot.from_canonical_payload(payload)
    assert snapshot.classroom_document_sha256 == "a" * 64
    assert snapshot.media_manifest_sha256 == "b" * 64
    assert snapshot.request_sha256 == hashlib.sha256(payload.encode()).hexdigest()


def test_export_worker_uses_export_submission_not_content_submission() -> None:
    from deeptutor.teaching.export_worker import submit_pinned_export

    calls: list[str] = []

    class Client:
        async def submit_export(self, request):
            calls.append(request.job_id)
            return request

        async def submit_content(self, request):
            raise AssertionError("content endpoint must not be used")

    request = __import__(
        "deeptutor.teaching.contracts", fromlist=["ExportRequest"]
    ).ExportRequest.model_validate(
        {
            "schema_version": "1.0",
            "tenant_id": "tenant-a",
            "job_id": "export-1",
            "idempotency_key": "key-1",
            "classroom_document_sha256": "a" * 64,
            "media_manifest_sha256": "b" * 64,
            "format": "pptx",
            "language": "zh-CN",
            "export_policy": {
                "include_source_attribution": True,
                "allow_external_links": False,
            },
        }
    )
    asyncio.run(submit_pinned_export(Client(), request))
    assert calls == ["export-1"]


@pytest.mark.asyncio
async def test_export_worker_stages_verified_formal_inputs_before_submission() -> None:
    from deeptutor.teaching.export_worker import (
        ExportInputArtifact,
        ExportInputBundle,
        stage_and_submit_pinned_export,
    )

    document = b'{"canonical":true}'
    media = b"media-bytes"
    calls: list[str] = []

    async def chunks(value: bytes):
        midpoint = max(1, len(value) // 2)
        yield value[:midpoint]
        yield value[midpoint:]

    class Store:
        async def open(self, key: str):
            calls.append(f"open:{key}")
            if key.endswith("classroom.json"):
                return chunks(document)
            return chunks(media)

    class Client:
        async def reserve_export_input(self, declaration):
            calls.append("reserve")
            assert all(not hasattr(item, "object_key") for item in declaration.files)

        async def upload_export_input_file(self, declaration, file, body):
            calls.append(f"upload:{file.kind}")
            value = b"".join([chunk async for chunk in body])
            assert value == (document if file.kind == "document" else media)

        async def commit_export_input(self, declaration):
            from deeptutor.teaching.contracts import canonical_json_bytes
            from deeptutor.teaching.export_worker import ExportInputCommitReceipt

            calls.append("commit")
            receipt_payload = canonical_json_bytes(
                {
                    "schemaVersion": 1,
                    "tenantId": declaration.tenant_id,
                    "jobId": declaration.job_id,
                    "idempotencyKey": declaration.idempotency_key,
                    "declarationSha256": declaration.declaration_sha256,
                    "classroomDocumentSha256": declaration.classroom_document_sha256,
                    "mediaManifestSha256": declaration.media_manifest_sha256,
                    "status": "committed",
                }
            )
            return ExportInputCommitReceipt(
                tenant_id=declaration.tenant_id,
                job_id=declaration.job_id,
                idempotency_key=declaration.idempotency_key,
                declaration_sha256=declaration.declaration_sha256,
                classroom_document_sha256=declaration.classroom_document_sha256,
                media_manifest_sha256=declaration.media_manifest_sha256,
                receipt_sha256=hashlib.sha256(receipt_payload).hexdigest(),
            )

        async def submit_export(self, request):
            calls.append("submit")
            return request

    request = __import__(
        "deeptutor.teaching.contracts", fromlist=["ExportRequest"]
    ).ExportRequest.model_validate(
        {
            "schema_version": "1.0",
            "tenant_id": "tenant-a",
            "job_id": "export-1",
            "idempotency_key": "key-1",
            "classroom_document_sha256": hashlib.sha256(document).hexdigest(),
            "media_manifest_sha256": "b" * 64,
            "format": "pptx",
            "language": "zh-CN",
            "export_policy": {
                "include_source_attribution": True,
                "allow_external_links": False,
            },
        }
    )
    bundle = ExportInputBundle(
        tenant_id="tenant-a",
        job_id="export-1",
        idempotency_key="key-1",
        request_sha256="d" * 64,
        document=ExportInputArtifact(
            media_id=None,
            relative_name="classroom.json",
            object_key="tenants/tenant-a/export-inputs/export-1/classroom.json",
            sha256=hashlib.sha256(document).hexdigest(),
            size_bytes=len(document),
            mime_type="application/json",
        ),
        media=(
            ExportInputArtifact(
                media_id="media-1",
                relative_name="media/voice.mp3",
                object_key="tenants/tenant-a/export-inputs/export-1/media/voice.mp3",
                sha256=hashlib.sha256(media).hexdigest(),
                size_bytes=len(media),
                mime_type="audio/mpeg",
            ),
        ),
        media_manifest_sha256="b" * 64,
        manifest_sha256="c" * 64,
    )

    await stage_and_submit_pinned_export(Client(), Store(), request, bundle)

    assert calls == [
        "reserve",
        "open:tenants/tenant-a/export-inputs/export-1/classroom.json",
        "upload:document",
        "open:tenants/tenant-a/export-inputs/export-1/media/voice.mp3",
        "upload:media",
        "commit",
        "submit",
    ]


@pytest.mark.asyncio
async def test_export_worker_never_submits_when_staging_fails() -> None:
    from deeptutor.teaching.export_worker import (
        ExportInputArtifact,
        ExportInputBundle,
        stage_and_submit_pinned_export,
    )

    submitted = False

    async def chunks():
        yield b"wrong"

    class Store:
        async def open(self, key: str):
            return chunks()

    class Client:
        async def reserve_export_input(self, declaration):
            return None

        async def upload_export_input_file(self, declaration, file, body):
            async for _ in body:
                pass

        async def commit_export_input(self, declaration):
            raise AssertionError("commit must not follow invalid input")

        async def submit_export(self, request):
            nonlocal submitted
            submitted = True

    request = __import__(
        "deeptutor.teaching.contracts", fromlist=["ExportRequest"]
    ).ExportRequest.model_validate(
        {
            "schema_version": "1.0",
            "tenant_id": "tenant-a",
            "job_id": "export-2",
            "idempotency_key": "key-2",
            "classroom_document_sha256": "a" * 64,
            "media_manifest_sha256": "b" * 64,
            "format": "pptx",
            "language": "zh-CN",
            "export_policy": {
                "include_source_attribution": True,
                "allow_external_links": False,
            },
        }
    )
    bundle = ExportInputBundle(
        tenant_id="tenant-a",
        job_id="export-2",
        idempotency_key="key-2",
        request_sha256="d" * 64,
        document=ExportInputArtifact(
            media_id=None,
            relative_name="classroom.json",
            object_key="tenants/tenant-a/export-inputs/export-2/classroom.json",
            sha256="a" * 64,
            size_bytes=5,
            mime_type="application/json",
        ),
        media=(),
        media_manifest_sha256="b" * 64,
        manifest_sha256="c" * 64,
    )

    with pytest.raises(ValueError, match="integrity verification failed"):
        await stage_and_submit_pinned_export(Client(), Store(), request, bundle)

    assert submitted is False
