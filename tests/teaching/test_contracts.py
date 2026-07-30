from __future__ import annotations

import copy
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

SHA256 = "a" * 64
OTHER_SHA256 = "b" * 64
GENERATED_AT = "2026-07-30T08:00:00Z"


def valid_teaching_brief() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "source_mode": "grounded",
        "source_snippets": [
            {
                "snippet_id": "snippet-1",
                "source_id": "source-1",
                "text": "A Fourier series represents a periodic function.",
                "content_sha256": SHA256,
            }
        ],
        "citations": [
            {
                "citation_id": "citation-1",
                "source_id": "source-1",
                "snippet_id": "snippet-1",
                "label": "Textbook, chapter 2",
            }
        ],
        "permission_summary": {
            "allowed_source_ids": ["source-1"],
            "usage_scope": "classroom_generation",
            "attribution_required": True,
        },
        "knowledge_points": [
            {
                "knowledge_point_id": "kp-1",
                "title": "Fourier series",
                "description": "Express periodic functions as trigonometric sums.",
            }
        ],
        "objectives": [
            {
                "objective_id": "objective-1",
                "description": "Explain the core idea of a Fourier series.",
                "knowledge_point_ids": ["kp-1"],
            }
        ],
        "duration_minutes": 20,
        "content_sha256": SHA256,
    }


def valid_generation_metadata() -> dict[str, object]:
    return {
        "generator": "openmaic",
        "generator_version": "0.3.1",
        "model_id": "server-selected-model",
        "generated_at": GENERATED_AT,
        "teaching_brief_sha256": SHA256,
    }


def valid_outline_bundle() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "outline_id": "outline-1",
        "title": "Fourier series introduction",
        "language": "en-US",
        "scenes": [
            {
                "scene_id": "scene-1",
                "title": "Periodic signals",
                "summary": "Connect periodic signals to sums of sinusoids.",
                "knowledge_point_ids": ["kp-1"],
                "source_refs": [
                    {
                        "citation_id": "citation-1",
                        "source_id": "source-1",
                        "snippet_id": "snippet-1",
                    }
                ],
            }
        ],
        "generation_metadata": valid_generation_metadata(),
        "content_sha256": OTHER_SHA256,
    }


def valid_generation_request() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "tenant_id": "tenant-1",
        "job_id": "job-1",
        "idempotency_key": "classroom-1-outline-1",
        "phase": "outline",
        "teaching_brief": valid_teaching_brief(),
        "confirmed_outline": None,
        "confirmed_outline_sha256": None,
        "data_plane_route_id": "shared-primary",
        "priority": "teacher",
    }


def valid_export_request() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "tenant_id": "tenant-1",
        "job_id": "export-1",
        "idempotency_key": "classroom-1-export-1",
        "classroom_document_sha256": SHA256,
        "media_manifest_sha256": OTHER_SHA256,
        "format": "pptx",
        "language": "en-US",
        "export_policy": {
            "include_source_attribution": True,
            "allow_external_links": False,
        },
    }


def test_generation_request_never_contains_provider_secret() -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    fields = set(GenerationRequest.model_fields)
    assert "provider_api_key" not in fields
    assert "provider_base_url" not in fields
    assert {
        "schema_version",
        "tenant_id",
        "job_id",
        "idempotency_key",
        "phase",
        "teaching_brief",
        "confirmed_outline",
        "confirmed_outline_sha256",
        "data_plane_route_id",
        "priority",
    } == fields


@pytest.mark.parametrize(
    ("outline", "outline_hash"),
    [
        (None, None),
        (valid_outline_bundle(), None),
        (None, OTHER_SHA256),
    ],
)
def test_content_phase_requires_confirmed_outline_and_hash(
    outline: dict[str, object] | None,
    outline_hash: str | None,
) -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    request = valid_generation_request()
    request.update(
        {
            "phase": "content",
            "confirmed_outline": outline,
            "confirmed_outline_sha256": outline_hash,
        }
    )

    with pytest.raises(
        ValidationError,
        match="content phase requires a confirmed outline and hash",
    ):
        GenerationRequest.model_validate(request)


def test_content_phase_accepts_confirmed_outline_and_hash() -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    request = valid_generation_request()
    request.update(
        {
            "phase": "content",
            "confirmed_outline": valid_outline_bundle(),
            "confirmed_outline_sha256": OTHER_SHA256,
        }
    )

    assert GenerationRequest.model_validate(request).phase == "content"


def test_export_contract_uses_only_supported_formats() -> None:
    from deeptutor.teaching.contracts import ExportFormat

    assert {item.value for item in ExportFormat} == {
        "classroom_zip",
        "pptx",
        "offline_html",
        "mp4",
    }


@pytest.mark.parametrize(
    ("model_name", "payload", "extra_field"),
    [
        ("GenerationRequest", valid_generation_request(), "provider_api_key"),
        ("GenerationRequest", valid_generation_request(), "provider_base_url"),
        ("GenerationRequest", valid_generation_request(), "object_store_secret"),
        ("ExportRequest", valid_export_request(), "provider_api_key"),
        ("ExportRequest", valid_export_request(), "object_store_access_key"),
    ],
)
def test_top_level_contracts_reject_credentials_and_extra_fields(
    model_name: str,
    payload: dict[str, object],
    extra_field: str,
) -> None:
    from deeptutor.teaching import contracts

    payload[extra_field] = "must-not-cross-the-contract"
    model = getattr(contracts, model_name)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(payload)


@pytest.mark.parametrize("bad_hash", ["", "A" * 64, "a" * 63, "g" * 64])
def test_hash_fields_accept_only_lowercase_sha256(bad_hash: str) -> None:
    from deeptutor.teaching.contracts import ExportRequest, TeachingBrief

    brief = valid_teaching_brief()
    brief["content_sha256"] = bad_hash
    with pytest.raises(ValidationError):
        TeachingBrief.model_validate(brief)

    export_request = valid_export_request()
    export_request["classroom_document_sha256"] = bad_hash
    with pytest.raises(ValidationError):
        ExportRequest.model_validate(export_request)


def test_generated_schemas_preserve_required_enums_aliases_and_nesting() -> None:
    from deeptutor.teaching.contracts import (
        ClassroomDocument,
        ExportRequest,
        GenerationRequest,
        TeachingBrief,
    )

    generation_schema = GenerationRequest.model_json_schema(mode="validation")
    assert generation_schema["additionalProperties"] is False
    assert set(generation_schema["required"]) == {
        "schema_version",
        "tenant_id",
        "job_id",
        "idempotency_key",
        "phase",
        "teaching_brief",
        "data_plane_route_id",
        "priority",
    }
    assert generation_schema["properties"]["schema_version"]["const"] == "1.0"
    assert generation_schema["properties"]["phase"]["enum"] == [
        "outline",
        "content",
        "micro",
    ]
    assert generation_schema["properties"]["priority"]["enum"] == [
        "student_micro",
        "interaction",
        "teacher",
        "full",
        "batch",
    ]
    assert generation_schema["properties"]["teaching_brief"]["$ref"].endswith("/TeachingBrief")
    assert any(
        option.get("$ref", "").endswith("/OutlineBundle")
        for option in generation_schema["properties"]["confirmed_outline"]["anyOf"]
    )

    brief_schema = TeachingBrief.model_json_schema(mode="validation")
    assert {
        "source_mode",
        "source_snippets",
        "citations",
        "permission_summary",
        "knowledge_points",
        "objectives",
        "duration_minutes",
        "content_sha256",
    } <= set(brief_schema["required"])
    assert brief_schema["properties"]["objectives"]["minItems"] == 1
    assert brief_schema["$defs"]["SourceSnippet"]["additionalProperties"] is False
    assert brief_schema["$defs"]["PermissionSummary"]["additionalProperties"] is False

    classroom_schema = ClassroomDocument.model_json_schema(mode="validation")
    assert {
        "dslVersion",
        "stage",
        "scenes",
        "sourceRefs",
        "knowledgePointMappings",
        "mediaManifest",
        "generationMetadata",
        "validationResult",
    } <= set(classroom_schema["required"])
    assert "dsl_version" not in classroom_schema["properties"]
    assert classroom_schema["properties"]["scenes"]["minItems"] == 1

    export_schema = ExportRequest.model_json_schema(mode="validation")
    format_ref = export_schema["properties"]["format"]["$ref"]
    export_format_schema = export_schema["$defs"][format_ref.rsplit("/", 1)[-1]]
    assert export_format_schema["enum"] == [
        "classroom_zip",
        "pptx",
        "offline_html",
        "mp4",
    ]


def test_all_seven_committed_schemas_match_models() -> None:
    from scripts.verify_classroom_contracts import (
        CONTRACT_SCHEMA_FILENAMES,
        verify_contract_schemas,
    )

    assert set(CONTRACT_SCHEMA_FILENAMES) == {
        "teaching-brief.schema.json",
        "generation-request.schema.json",
        "outline-bundle.schema.json",
        "classroom-document.schema.json",
        "generation-job.schema.json",
        "export-request.schema.json",
        "export-job.schema.json",
    }
    assert verify_contract_schemas() == []


def test_schema_verifier_reports_missing_and_drifted_files(tmp_path: Path) -> None:
    from scripts.verify_classroom_contracts import (
        generated_contract_schemas,
        verify_contract_schemas,
    )

    generated = generated_contract_schemas()
    for filename, schema in generated.items():
        (tmp_path / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    assert verify_contract_schemas(tmp_path) == []

    modified_dir = tmp_path / "modified"
    modified_dir.mkdir()
    for filename, schema in generated.items():
        if filename == "export-job.schema.json":
            continue
        (modified_dir / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    drifted = copy.deepcopy(generated["generation-request.schema.json"])
    drifted["title"] = "DriftedGenerationRequest"
    (modified_dir / "generation-request.schema.json").write_text(
        json.dumps(drifted, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    errors = verify_contract_schemas(modified_dir)
    assert errors == [
        "export-job.schema.json: missing",
        "generation-request.schema.json: schema drift",
    ]
