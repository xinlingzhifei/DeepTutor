from __future__ import annotations

import copy
import hashlib
import json

import pytest

from tests.teaching_contract_fixtures import (
    valid_classroom_document,
    valid_content_generation_request,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _engine_result() -> dict[str, object]:
    document = valid_classroom_document()
    without_hash = dict(document)
    without_hash.pop("file_sha256")
    document["file_sha256"] = hashlib.sha256(_canonical(without_hash)).hexdigest()
    document_sha = hashlib.sha256(_canonical(document)).hexdigest()
    media = document["media_manifest"][0]
    return {
        "classroomId": document["classroom_id"],
        "classroomDocument": document,
        "classroomDocumentSha256": document_sha,
        "mediaManifestSha256": hashlib.sha256(
            _canonical(document["media_manifest"])
        ).hexdigest(),
        "artifacts": [
            {
                "relativePath": "classroom.json",
                "sha256": document_sha,
                "bytes": len(_canonical(document)),
                "mime": "application/json",
                "downloadPath": "/api/yfeistai/v1/artifacts/job-1/classroom.json",
                "expiresAt": "2030-07-30T09:00:00Z",
            },
            {
                "relativePath": media["relative_path"],
                "sha256": media["sha256"],
                "bytes": media["size_bytes"],
                "mime": media["mime_type"],
                "downloadPath": (
                    "/api/yfeistai/v1/artifacts/job-1/" + media["relative_path"]
                ),
                "expiresAt": "2030-07-30T09:00:00Z",
            },
        ],
    }


def test_validates_contract_dsl_sources_media_and_tenant_paths() -> None:
    from deeptutor.teaching.artifact_validation import validate_generation_result

    request = valid_content_generation_request()
    result = validate_generation_result(
        tenant_id="tenant-1",
        job_id="job-1",
        request_payload=request,
        result_payload=_engine_result(),
    )

    assert result.classroom_id == "classroom-1"
    assert result.document_artifact.relative_name == "classroom.json"
    assert len(result.artifacts) == 2
    assert all(key.startswith("tenants/tenant-1/") for key in result.target_keys(1))


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda result: result.update(classroomDocumentSha256="0" * 64), "hash_invalid"),
        (
            lambda result: result["classroomDocument"]["openmaic"]["scenes"][0].update(
                type="unknown"
            ),
            "dsl_invalid",
        ),
        (
            lambda result: result["classroomDocument"]["source_refs"][0].update(
                source_id="source-other"
            ),
            "source_invalid",
        ),
        (
            lambda result: result["artifacts"][1].update(mime="application/pdf"),
            "media_invalid",
        ),
        (
            lambda result: result["artifacts"][0].update(
                downloadPath="/api/yfeistai/v1/artifacts/job-other/classroom.json"
            ),
            "tenant_prefix_invalid",
        ),
    ],
)
def test_bad_output_is_rejected_before_promotion(mutator, code: str) -> None:
    from deeptutor.teaching.artifact_validation import (
        ArtifactValidationError,
        validate_generation_result,
    )

    request = valid_content_generation_request()
    result = _engine_result()
    mutator(result)

    with pytest.raises(ArtifactValidationError) as caught:
        validate_generation_result(
            tenant_id="tenant-1",
            job_id="job-1",
            request_payload=request,
            result_payload=result,
        )
    assert caught.value.code == code


def test_protocol_relative_network_resource_is_denied_by_policy() -> None:
    from deeptutor.teaching.artifact_validation import (
        ArtifactValidationError,
        validate_generation_result,
    )

    request = valid_content_generation_request()
    result = _engine_result()
    scene = result["classroomDocument"]["openmaic"]["scenes"][0]
    scene["type"] = "interactive"
    scene["content"] = {
        "type": "interactive",
        "html": '<img src="//attacker.invalid/a.png">',
        "bridge_version": "1.0",
        "sandbox": {"allow_scripts": True, "allow_same_origin": False},
    }
    result["classroomDocument"]["interaction_ids"] = [scene["id"]]
    without_hash = copy.deepcopy(result["classroomDocument"])
    without_hash.pop("file_sha256")
    result["classroomDocument"]["file_sha256"] = hashlib.sha256(
        _canonical(without_hash)
    ).hexdigest()
    result["classroomDocumentSha256"] = hashlib.sha256(
        _canonical(result["classroomDocument"])
    ).hexdigest()
    result["artifacts"][0]["sha256"] = result["classroomDocumentSha256"]
    result["artifacts"][0]["bytes"] = len(_canonical(result["classroomDocument"]))

    with pytest.raises(ArtifactValidationError) as caught:
        validate_generation_result(
            tenant_id="tenant-1",
            job_id="job-1",
            request_payload=request,
            result_payload=result,
        )
    assert caught.value.code == "policy_denied"
