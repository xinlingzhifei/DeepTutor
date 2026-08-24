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
        "mediaManifestSha256": hashlib.sha256(_canonical(document["media_manifest"])).hexdigest(),
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
                "downloadPath": ("/api/yfeistai/v1/artifacts/job-1/" + media["relative_path"]),
                "expiresAt": "2030-07-30T09:00:00Z",
            },
        ],
    }


def _rehash_document_result(result: dict[str, object]) -> None:
    document = result["classroomDocument"]
    without_hash = copy.deepcopy(document)
    without_hash.pop("file_sha256")
    document["file_sha256"] = hashlib.sha256(_canonical(without_hash)).hexdigest()
    document_sha256 = hashlib.sha256(_canonical(document)).hexdigest()
    result["classroomDocumentSha256"] = document_sha256
    result["artifacts"][0]["sha256"] = document_sha256
    result["artifacts"][0]["bytes"] = len(_canonical(document))


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
    ("scene_type", "content"),
    [
        ("slide", {"type": "slide", "canvas": {"elements": []}}),
        (
            "quiz",
            {
                "type": "quiz",
                "questions": [
                    {
                        "id": "question-1",
                        "prompt": "Which signal is periodic?",
                        "question_type": "single_choice",
                        "options": [
                            {"id": "a", "label": "Sine"},
                            {"id": "b", "label": "Noise"},
                        ],
                        "correct_option_ids": ["a"],
                        "explanation": "A sine wave repeats.",
                    }
                ],
            },
        ),
        (
            "interactive",
            {
                "type": "interactive",
                "html": "<button>Explore</button>",
                "bridge_version": "1.0",
                "sandbox": {"allow_scripts": True, "allow_same_origin": False},
            },
        ),
        (
            "pbl",
            {
                "type": "pbl",
                "scenario": "Build a signal analyzer.",
                "roles": [
                    {
                        "id": "engineer",
                        "name": "Signal engineer",
                        "brief": "Design the analyzer.",
                    }
                ],
                "milestones": [
                    {
                        "id": "prototype",
                        "title": "Prototype",
                        "rubric": "Identifies a periodic signal.",
                    }
                ],
            },
        ),
    ],
)
def test_interaction_ids_cover_every_non_slide_scene(
    scene_type: str,
    content: dict[str, object],
) -> None:
    from deeptutor.teaching.artifact_validation import validate_generation_result

    result = _engine_result()
    document = result["classroomDocument"]
    scene = document["openmaic"]["scenes"][0]
    scene["type"] = scene_type
    scene["content"] = content
    document["interaction_ids"] = [] if scene_type == "slide" else [scene["id"]]
    _rehash_document_result(result)

    validated = validate_generation_result(
        tenant_id="tenant-1",
        job_id="job-1",
        request_payload=valid_content_generation_request(),
        result_payload=result,
    )

    assert validated.document.interaction_ids == document["interaction_ids"]


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


@pytest.mark.parametrize(
    ("mutator", "reason"),
    (
        (lambda result: result.update(artifacts=[]), "missing_artifact"),
        (
            lambda result: result["artifacts"][0].update(bytes=512 * 1024 * 1024 + 1),
            "size_mismatch",
        ),
    ),
)
def test_output_artifact_shape_exposes_only_fixed_metric_reason(mutator, reason: str) -> None:
    from deeptutor.teaching.artifact_validation import (
        ArtifactValidationError,
        validate_generation_result,
    )

    result = _engine_result()
    mutator(result)

    with pytest.raises(ArtifactValidationError) as caught:
        validate_generation_result(
            tenant_id="tenant-1",
            job_id="job-1",
            request_payload=valid_content_generation_request(),
            result_payload=result,
        )

    assert caught.value.metric_reason == reason


@pytest.mark.parametrize(
    ("case", "code", "reason"),
    (
        ("policy", "policy_denied", None),
        ("receipt", "media_invalid", "receipt_mismatch"),
        ("hash", "hash_invalid", "hash_mismatch"),
    ),
)
def test_media_validation_separates_policy_receipt_and_hash_metrics(
    case: str,
    code: str,
    reason: str | None,
) -> None:
    from deeptutor.teaching.artifact_validation import (
        ArtifactValidationError,
        validate_generation_result,
    )

    request = valid_content_generation_request()
    result = _engine_result()
    if case == "policy":
        request["teaching_brief"]["media_policy"]["allowed_mime_types"] = ["image/png"]
    elif case == "receipt":
        result["artifacts"][1]["mime"] = "application/pdf"
    else:
        result["artifacts"][1]["sha256"] = "b" * 64

    with pytest.raises(ArtifactValidationError) as caught:
        validate_generation_result(
            tenant_id="tenant-1",
            job_id="job-1",
            request_payload=request,
            result_payload=result,
        )

    assert caught.value.code == code
    assert caught.value.metric_reason == reason


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
