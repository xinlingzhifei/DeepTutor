from __future__ import annotations

import asyncio
import hashlib
import json


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
