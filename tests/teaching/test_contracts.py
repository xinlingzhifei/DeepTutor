from __future__ import annotations

from collections.abc import Callable
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError
import pytest

SHA256 = "a" * 64
OTHER_SHA256 = "b" * 64
OUTLINE_BUNDLE_CONTRACT_SHA256 = (
    "a45b0310d5b58a8e2d461ccfa9d60be24615583825a1f3a4f4460672cbd19ba5"
)
GENERATED_AT = "2026-07-30T08:00:00Z"
CONTRACT_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts" / "classroom"


def committed_schema(filename: str) -> dict[str, object]:
    return json.loads((CONTRACT_SCHEMA_DIR / filename).read_text(encoding="utf-8"))


def valid_teaching_brief() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "brief_id": "brief-1",
        "brief_version": 1,
        "tenant_id": "tenant-1",
        "course_id": "course-1",
        "target_class_id": "class-1",
        "grade_band": "upper_secondary",
        "audience_level": "introductory",
        "classroom_mode": "full",
        "objectives": [
            {
                "objective_id": "objective-1",
                "description": "Explain the core idea of a Fourier series.",
                "knowledge_point_ids": ["kp-1"],
            }
        ],
        "duration_minutes": 20,
        "knowledge_points": [
            {
                "knowledge_point_id": "kp-1",
                "title": "Fourier series",
                "description": "Express periodic functions as trigonometric sums.",
            }
        ],
        "prerequisites": [
            {
                "knowledge_point_id": "kp-1",
                "prerequisite_knowledge_point_ids": ["kp-0"],
            }
        ],
        "assessment": {
            "methods": ["quiz"],
            "success_criteria": ["Correctly identify a Fourier coefficient."],
        },
        "source_snapshot": {
            "snapshot_id": "snapshot-1",
            "created_at": GENERATED_AT,
            "content_sha256": SHA256,
        },
        "source_fragments": [
            {
                "fragment_id": "fragment-1",
                "source_id": "source-1",
                "text": "A Fourier series represents a periodic function.",
                "content_sha256": SHA256,
            }
        ],
        "citations": [
            {
                "citation_id": "citation-1",
                "source_id": "source-1",
                "fragment_id": "fragment-1",
                "label": "Textbook, chapter 2",
            }
        ],
        "source_refs": [
            {
                "citation_id": "citation-1",
                "source_id": "source-1",
                "fragment_id": "fragment-1",
            }
        ],
        "permission_summary": {
            "allowed_source_ids": ["source-1"],
            "allowed_fragment_ids": ["fragment-1"],
            "usage_scope": "classroom_generation",
            "attribution_required": True,
        },
        "content_mode": "source_grounded",
        "network_policy": {
            "allow_web_access": False,
            "allowed_domains": [],
        },
        "media_policy": {
            "allow_generation": True,
            "allowed_mime_types": ["image/png", "audio/mpeg"],
        },
        "template_policy": {
            "template_id": "template-1",
            "template_version": "2",
        },
        "safety_policy": {
            "policy_id": "school-default",
            "blocked_categories": ["violence"],
        },
        "content_sha256": SHA256,
    }


def valid_generation_metadata() -> dict[str, object]:
    return {
        "generator": "openmaic",
        "generator_version": "0.3.1",
        "model_id": "server-selected-model",
        "generated_at": GENERATED_AT,
        "teaching_brief_id": "brief-1",
        "teaching_brief_sha256": SHA256,
        "template_id": "template-1",
        "template_version": "2",
    }


def valid_outline_bundle() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "outline_id": "outline-1",
        "outline_version": 1,
        "confirmation_metadata": {
            "status": "confirmed",
            "confirmed_at": GENERATED_AT,
            "confirmed_by": "teacher-1",
        },
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
                        "fragment_id": "fragment-1",
                    }
                ],
            }
        ],
        "knowledge_coverage": [
            {
                "knowledge_point_id": "kp-1",
                "scene_ids": ["scene-1"],
            }
        ],
        "source_refs": [
            {
                "citation_id": "citation-1",
                "source_id": "source-1",
                "fragment_id": "fragment-1",
            }
        ],
        "estimated_scene_count": 1,
        "generation_metadata": valid_generation_metadata(),
        "contract_sha256": OUTLINE_BUNDLE_CONTRACT_SHA256,
    }


def valid_generation_request() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "tenant_id": "tenant-1",
        "request_id": "request-1",
        "job_id": "job-1",
        "idempotency_key": "classroom-1-outline-1",
        "phase": "outline",
        "classroom_mode": "full",
        "teaching_brief_id": "brief-1",
        "teaching_brief_sha256": SHA256,
        "teaching_brief": valid_teaching_brief(),
        "confirmed_outline": None,
        "confirmed_outline_sha256": None,
        "template_id": "template-1",
        "template_version": "2",
        "scene_budget": 8,
        "duration_minutes": 20,
        "requested_exports": ["classroom_zip", "pptx"],
        "callback_context": "callback-context-1",
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


def valid_classroom_document() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "classroom_id": "classroom-1",
        "classroom_version_id": "classroom-version-1",
        "content_mode": "source_grounded",
        "open_creation": False,
        "openmaic": {
            "dsl_version": "0.1.0",
            "stage": {
                "id": "stage-1",
                "name": "Fourier series",
                "created_at": GENERATED_AT,
                "updated_at": GENERATED_AT,
            },
            "scenes": [
                {
                    "id": "scene-1",
                    "stage_id": "stage-1",
                    "title": "Periodic signals",
                    "order": 0,
                    "type": "slide",
                    "content": {
                        "type": "slide",
                        "canvas": {
                            "width": 1920,
                            "elements": [
                                {
                                    "id": "title",
                                    "meta": {"tags": ["intro", 1, True, None]},
                                }
                            ],
                        },
                    },
                    "actions": [
                        {
                            "type": "speech",
                            "payload": {"text": "Consider a periodic signal."},
                        }
                    ],
                }
            ],
        },
        "interaction_ids": [],
        "source_refs": [
            {
                "citation_id": "citation-1",
                "source_id": "source-1",
                "fragment_id": "fragment-1",
            }
        ],
        "knowledge_point_mappings": [
            {
                "knowledge_point_id": "kp-1",
                "scene_ids": ["scene-1"],
                "source_refs": [
                    {
                        "citation_id": "citation-1",
                        "source_id": "source-1",
                        "fragment_id": "fragment-1",
                    }
                ],
            }
        ],
        "media_manifest": [
            {
                "media_id": "media-1",
                "relative_path": "media/voice.mp3",
                "mime_type": "audio/mpeg",
                "sha256": SHA256,
                "size_bytes": 128,
                "temporary_download_path": (
                    "/api/yfeistai/v1/artifacts/content-job-1/media/voice.mp3"
                ),
                "expires_at": GENERATED_AT,
            }
        ],
        "file_sha256": SHA256,
        "export_manifest": [
            {
                "format": "classroom_zip",
                "relative_path": "exports/classroom.zip",
                "sha256": OTHER_SHA256,
                "size_bytes": 256,
                "mime_type": "application/zip",
                "temporary_download_path": (
                    "/api/yfeistai/v1/artifacts/content-job-1/exports/classroom.zip"
                ),
                "expires_at": GENERATED_AT,
            }
        ],
        "generation_metadata": valid_generation_metadata(),
        "audit_metadata": {
            "template_id": "template-1",
            "template_version": "2",
            "teaching_brief_id": "brief-1",
            "teaching_brief_sha256": SHA256,
            "parent_classroom_version_id": None,
        },
        "validation_result": {
            "valid": True,
            "issues": [],
            "validated_at": GENERATED_AT,
        },
        "migration_records": [
            {
                "from_dsl_version": "0.0.9",
                "to_dsl_version": "0.1.0",
                "migrated_at": GENERATED_AT,
                "migration_id": "migration-1",
            }
        ],
    }


def _valid_job_base(
    *,
    status: str,
    job_id: str,
    request_id: str,
    idempotency_key: str,
    work_pool: str,
    reservation_id: str,
    reserved_units: int,
    actual_units: int,
    estimated_amount: float,
    actual_amount: float,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "tenant_id": "tenant-1",
        "job_id": job_id,
        "request_id": request_id,
        "classroom_draft_id": "draft-1",
        "batch_id": None,
        "idempotency_key": idempotency_key,
        "status": status,
        "attempt": 0,
        "progress_percent": 10,
        "work_pool": work_pool,
        "quota_reservation": {
            "reservation_id": reservation_id,
            "reserved_units": reserved_units,
            "actual_units": actual_units,
            "unit": "credits",
        },
        "cost_summary": {
            "currency": "USD",
            "estimated_amount": estimated_amount,
            "actual_amount": actual_amount,
        },
        "heartbeat_at": None,
        "lease_id": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "temporary_artifact": None,
        "final_artifact": None,
        "artifact_manifest": [],
        "output_sha256": None,
        "error": None,
        "created_at": GENERATED_AT,
        "updated_at": GENERATED_AT,
        "started_at": None,
        "canceled_at": None,
        "completed_at": None,
    }


def _apply_terminal_job_state(
    payload: dict[str, object],
    *,
    status: str,
    artifact_id: str,
    error: dict[str, object],
) -> None:
    if status == "succeeded":
        payload.update(
            {
                "progress_percent": 100,
                "started_at": GENERATED_AT,
                "completed_at": GENERATED_AT,
                "output_sha256": OTHER_SHA256,
                "final_artifact": {
                    "artifact_id": artifact_id,
                    "status": "ready",
                    "sha256": OTHER_SHA256,
                },
            }
        )
    elif status == "failed":
        payload.update(
            {
                "started_at": GENERATED_AT,
                "completed_at": GENERATED_AT,
                "error": error,
            }
        )
    elif status == "canceled":
        payload.update(
            {
                "started_at": GENERATED_AT,
                "canceled_at": GENERATED_AT,
                "completed_at": GENERATED_AT,
            }
        )


def valid_generation_job(status: str = "queued") -> dict[str, object]:
    payload = _valid_job_base(
        status=status,
        job_id="job-1",
        request_id="request-1",
        idempotency_key="classroom-1-outline-1",
        work_pool="shared-generation",
        reservation_id="quota-1",
        reserved_units=100,
        actual_units=10,
        estimated_amount=0.5,
        actual_amount=0.1,
    )
    payload.update(
        {
            "phase": "outline",
            "model_id": "server-selected-model",
            "input_sha256": SHA256,
        }
    )
    _apply_terminal_job_state(
        payload,
        status=status,
        artifact_id="artifact-final-1",
        error={
            "category": "provider",
            "code": "provider_unavailable",
            "message": "Provider returned 503.",
            "retryable": True,
            "diagnostic_summary": "Upstream service unavailable.",
        },
    )
    if status == "succeeded":
        payload["artifact_manifest"] = [
            {
                "kind": "dsl_json",
                "relative_path": "classroom/document.json",
                "sha256": OTHER_SHA256,
                "size_bytes": 512,
                "mime_type": "application/json",
                "temporary_download_path": "downloads/classroom/document.json",
                "expires_at": GENERATED_AT,
            },
            {
                "kind": "media",
                "relative_path": "media/voice.mp3",
                "sha256": SHA256,
                "size_bytes": 128,
                "mime_type": "audio/mpeg",
                "temporary_download_path": "downloads/media/voice.mp3",
                "expires_at": GENERATED_AT,
            },
        ]
    return payload


def valid_export_job(status: str = "queued") -> dict[str, object]:
    payload = _valid_job_base(
        status=status,
        job_id="export-job-1",
        request_id="export-request-1",
        idempotency_key="classroom-1-export-1",
        work_pool="pptx-export",
        reservation_id="quota-export-1",
        reserved_units=5,
        actual_units=1,
        estimated_amount=0.05,
        actual_amount=0.01,
    )
    payload.update(
        {
            "phase": "queued",
            "format": "pptx",
            "renderer_id": "openmaic-pptx",
            "input_classroom_document_sha256": SHA256,
            "input_media_manifest_sha256": OTHER_SHA256,
        }
    )
    if status == "succeeded":
        payload["phase"] = "completed"
    _apply_terminal_job_state(
        payload,
        status=status,
        artifact_id="artifact-export-1",
        error={
            "category": "artifact",
            "code": "export_failed",
            "message": "PPTX materialization failed.",
            "retryable": False,
            "diagnostic_summary": "Renderer rejected one slide.",
        },
    )
    if status == "succeeded":
        payload["artifact_manifest"] = [
            {
                "kind": "export",
                "relative_path": "exports/classroom.pptx",
                "sha256": OTHER_SHA256,
                "size_bytes": 1024,
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                ),
                "temporary_download_path": "downloads/exports/classroom.pptx",
                "expires_at": GENERATED_AT,
            }
        ]
    return payload


PayloadFactory = Callable[..., dict[str, object]]
TOP_LEVEL_CONTRACT_CASES: list[tuple[str, str, PayloadFactory]] = [
    ("TeachingBrief", "teaching-brief.schema.json", valid_teaching_brief),
    ("GenerationRequest", "generation-request.schema.json", valid_generation_request),
    ("OutlineBundle", "outline-bundle.schema.json", valid_outline_bundle),
    ("ClassroomDocument", "classroom-document.schema.json", valid_classroom_document),
    ("GenerationJob", "generation-job.schema.json", valid_generation_job),
    ("ExportRequest", "export-request.schema.json", valid_export_request),
    ("ExportJob", "export-job.schema.json", valid_export_job),
]


def test_generation_request_never_contains_provider_secret() -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    fields = set(GenerationRequest.model_fields)
    assert {
        "provider_id",
        "provider_api_key",
        "provider_base_url",
        "provider_route_id",
        "model_route",
    }.isdisjoint(fields)
    assert {
        "schema_version",
        "tenant_id",
        "request_id",
        "job_id",
        "idempotency_key",
        "phase",
        "classroom_mode",
        "teaching_brief_id",
        "teaching_brief_sha256",
        "teaching_brief",
        "confirmed_outline",
        "confirmed_outline_sha256",
        "template_id",
        "template_version",
        "scene_budget",
        "duration_minutes",
        "requested_exports",
        "callback_context",
        "data_plane_route_id",
        "priority",
    } <= fields
    route_schema = GenerationRequest.model_json_schema()["properties"]["dataPlaneRouteId"]
    assert "opaque control-plane routing key" in route_schema["description"]
    assert "provider" in route_schema["description"].lower()


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
    from deeptutor.teaching.contracts import (
        GenerationRequest,
        OutlineBundle,
        canonical_outline_sha256,
    )

    request = valid_generation_request()
    outline = OutlineBundle.model_validate(valid_outline_bundle())
    request.update(
        {
            "phase": "content",
            "confirmed_outline": valid_outline_bundle(),
            "confirmed_outline_sha256": canonical_outline_sha256(outline),
        }
    )

    assert GenerationRequest.model_validate(request).phase == "content"


def test_content_phase_rejects_draft_outline_in_model_and_schema() -> None:
    from deeptutor.teaching.contracts import (
        GenerationRequest,
        OutlineBundle,
        canonical_outline_sha256,
    )

    outline_payload = valid_outline_bundle()
    confirmation = outline_payload["confirmation_metadata"]
    assert isinstance(confirmation, dict)
    confirmation.update(
        {
            "status": "draft",
            "confirmed_at": None,
            "confirmed_by": None,
        }
    )
    outline = OutlineBundle.model_validate(outline_payload)
    request = valid_generation_request()
    request.update(
        {
            "phase": "content",
            "confirmed_outline": outline_payload,
            "confirmed_outline_sha256": canonical_outline_sha256(outline),
        }
    )

    with pytest.raises(ValidationError, match="confirmed outline status"):
        GenerationRequest.model_validate(request)

    camel_payload = GenerationRequest.model_validate(valid_generation_request()).model_dump(
        mode="json"
    )
    camel_payload.update(
        {
            "phase": "content",
            "confirmedOutline": outline.model_dump(mode="json"),
            "confirmedOutlineSha256": canonical_outline_sha256(outline),
        }
    )
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("generation-request.schema.json")).validate(
            camel_payload
        )


@pytest.mark.parametrize(
    ("phase", "classroom_mode"),
    [
        ("micro", "full"),
        ("outline", "micro"),
        ("content", "micro"),
    ],
)
def test_generation_phase_and_classroom_mode_are_coupled(
    phase: str,
    classroom_mode: str,
) -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    request = valid_generation_request()
    request.update({"phase": phase, "classroom_mode": classroom_mode})
    if phase == "content":
        from deeptutor.teaching.contracts import OutlineBundle, canonical_outline_sha256

        outline = OutlineBundle.model_validate(valid_outline_bundle())
        request["confirmed_outline"] = valid_outline_bundle()
        request["confirmed_outline_sha256"] = canonical_outline_sha256(outline)

    with pytest.raises(ValidationError, match="classroom mode"):
        GenerationRequest.model_validate(request)

    dumped = GenerationRequest.model_validate(valid_generation_request()).model_dump(mode="json")
    dumped.update({"phase": phase, "classroomMode": classroom_mode})
    if phase == "content":
        from deeptutor.teaching.contracts import OutlineBundle, canonical_outline_sha256

        outline = OutlineBundle.model_validate(valid_outline_bundle())
        dumped["confirmedOutline"] = outline.model_dump(mode="json")
        dumped["confirmedOutlineSha256"] = canonical_outline_sha256(outline)
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("generation-request.schema.json")).validate(dumped)


@pytest.mark.parametrize(
    ("phase", "request_mode", "brief_mode"),
    [
        ("outline", "full", "micro"),
        ("micro", "micro", "full"),
    ],
)
def test_generation_request_mode_matches_embedded_teaching_brief(
    phase: str,
    request_mode: str,
    brief_mode: str,
) -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    payload = valid_generation_request()
    payload.update({"phase": phase, "classroom_mode": request_mode})
    teaching_brief = payload["teaching_brief"]
    assert isinstance(teaching_brief, dict)
    teaching_brief["classroom_mode"] = brief_mode
    with pytest.raises(ValidationError, match="embedded teaching brief classroom mode"):
        GenerationRequest.model_validate(payload)

    valid_payload = valid_generation_request()
    valid_payload.update({"phase": phase, "classroom_mode": request_mode})
    valid_brief = valid_payload["teaching_brief"]
    assert isinstance(valid_brief, dict)
    valid_brief["classroom_mode"] = request_mode
    dumped = GenerationRequest.model_validate(valid_payload).model_dump(mode="json")
    dumped["teachingBrief"]["classroomMode"] = brief_mode
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("generation-request.schema.json")).validate(dumped)


def test_generation_request_rejects_noncanonical_outline_hash() -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    request = valid_generation_request()
    request.update(
        {
            "confirmed_outline": valid_outline_bundle(),
            "confirmed_outline_sha256": SHA256,
        }
    )

    with pytest.raises(
        ValidationError,
        match="confirmed outline hash does not match canonical JSON",
    ):
        GenerationRequest.model_validate(request)


def test_generation_request_default_dump_is_camel_case() -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    dumped = GenerationRequest.model_validate(valid_generation_request()).model_dump(mode="json")

    assert "schemaVersion" in dumped
    assert "tenantId" in dumped
    assert "dataPlaneRouteId" in dumped
    assert "schema_version" not in dumped
    assert "tenant_id" not in dumped


def test_contract_config_is_pydantic_2_0_compatible_and_serializes_aliases() -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    config = GenerationRequest.model_config
    assert config["populate_by_name"] is True

    request = GenerationRequest.model_validate(valid_generation_request())
    dumped = request.model_dump(mode="json")
    dumped_json = json.loads(request.model_dump_json())
    assert dumped_json == dumped
    assert "schemaVersion" in dumped_json
    assert "schema_version" not in dumped_json


def test_generation_request_json_schema_rejects_content_without_outline() -> None:
    schema = committed_schema("generation-request.schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    from deeptutor.teaching.contracts import GenerationRequest

    payload = GenerationRequest.model_validate(valid_generation_request()).model_dump(mode="json")
    payload["phase"] = "content"

    with pytest.raises(JsonSchemaValidationError):
        validator.validate(payload)


@pytest.mark.parametrize("phase", ["outline", "content", "micro"])
@pytest.mark.parametrize(
    ("outline_present", "hash_present"),
    [(True, False), (False, True)],
)
def test_generation_request_json_schema_rejects_half_paired_outline(
    phase: str,
    outline_present: bool,
    hash_present: bool,
) -> None:
    schema = committed_schema("generation-request.schema.json")
    validator = Draft202012Validator(schema)

    from deeptutor.teaching.contracts import GenerationRequest, OutlineBundle

    payload = GenerationRequest.model_validate(valid_generation_request()).model_dump(mode="json")
    payload["phase"] = phase
    outline_key = "confirmedOutline" if "confirmedOutline" in payload else "confirmed_outline"
    hash_key = (
        "confirmedOutlineSha256"
        if "confirmedOutlineSha256" in payload
        else "confirmed_outline_sha256"
    )
    payload[outline_key] = (
        OutlineBundle.model_validate(valid_outline_bundle()).model_dump(mode="json")
        if outline_present
        else None
    )
    payload[hash_key] = SHA256 if hash_present else None

    with pytest.raises(JsonSchemaValidationError):
        validator.validate(payload)


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
        ("GenerationRequest", valid_generation_request(), "provider_id"),
        ("GenerationRequest", valid_generation_request(), "provider_route_id"),
        ("GenerationRequest", valid_generation_request(), "model_route"),
        ("GenerationRequest", valid_generation_request(), "object_store_secret"),
        ("TeachingBrief", valid_teaching_brief(), "provider_api_key"),
        ("OutlineBundle", valid_outline_bundle(), "provider_api_key"),
        ("ClassroomDocument", valid_classroom_document(), "object_store_secret"),
        ("GenerationJob", valid_generation_job(), "provider_api_key"),
        ("ExportRequest", valid_export_request(), "provider_api_key"),
        ("ExportRequest", valid_export_request(), "object_store_access_key"),
        ("ExportJob", valid_export_job(), "object_store_access_key"),
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
        "schemaVersion",
        "tenantId",
        "requestId",
        "jobId",
        "idempotencyKey",
        "phase",
        "classroomMode",
        "teachingBriefId",
        "teachingBriefSha256",
        "teachingBrief",
        "templateId",
        "templateVersion",
        "sceneBudget",
        "durationMinutes",
        "requestedExports",
        "callbackContext",
        "dataPlaneRouteId",
        "priority",
    }
    assert generation_schema["properties"]["schemaVersion"]["const"] == "1.0"
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
    assert generation_schema["properties"]["teachingBrief"]["$ref"].endswith("/TeachingBrief")
    assert any(
        option.get("$ref", "").endswith("/OutlineBundle")
        for option in generation_schema["properties"]["confirmedOutline"]["anyOf"]
    )

    brief_schema = TeachingBrief.model_json_schema(mode="validation")
    assert {
        "briefId",
        "briefVersion",
        "tenantId",
        "courseId",
        "targetClassId",
        "gradeBand",
        "audienceLevel",
        "classroomMode",
        "objectives",
        "durationMinutes",
        "knowledgePoints",
        "prerequisites",
        "assessment",
        "sourceSnapshot",
        "sourceFragments",
        "citations",
        "sourceRefs",
        "permissionSummary",
        "contentMode",
        "networkPolicy",
        "mediaPolicy",
        "templatePolicy",
        "safetyPolicy",
        "contentSha256",
    } <= set(brief_schema["required"])
    assert brief_schema["properties"]["objectives"]["minItems"] == 1
    assert brief_schema["$defs"]["SourceFragment"]["additionalProperties"] is False
    assert brief_schema["$defs"]["PermissionSummary"]["additionalProperties"] is False
    assert "allowedFragmentIds" in brief_schema["$defs"]["PermissionSummary"]["required"]

    classroom_schema = ClassroomDocument.model_json_schema(mode="validation")
    assert {
        "classroomVersionId",
        "contentMode",
        "openCreation",
        "openmaic",
        "interactionIds",
        "sourceRefs",
        "knowledgePointMappings",
        "mediaManifest",
        "fileSha256",
        "exportManifest",
        "generationMetadata",
        "auditMetadata",
        "validationResult",
        "migrationRecords",
    } <= set(classroom_schema["required"])
    assert "dslVersion" not in classroom_schema["properties"]
    openmaic_ref = classroom_schema["properties"]["openmaic"]["$ref"]
    openmaic_schema = classroom_schema["$defs"][openmaic_ref.rsplit("/", 1)[-1]]
    assert openmaic_schema["properties"]["dslVersion"]["const"] == "0.1.0"
    assert openmaic_schema["properties"]["scenes"]["minItems"] == 1

    export_schema = ExportRequest.model_json_schema(mode="validation")
    format_ref = export_schema["properties"]["format"]["$ref"]
    export_format_schema = export_schema["$defs"][format_ref.rsplit("/", 1)[-1]]
    assert export_format_schema["enum"] == [
        "classroom_zip",
        "pptx",
        "offline_html",
        "mp4",
    ]


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory"),
    TOP_LEVEL_CONTRACT_CASES,
)
def test_all_top_level_contracts_round_trip_as_camel_case_and_validate_schema(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
) -> None:
    from deeptutor.teaching import contracts

    model = getattr(contracts, model_name)
    payload = payload_factory()
    parsed = model.model_validate(payload)
    dumped = parsed.model_dump(mode="json")

    assert "schemaVersion" in dumped
    assert "schema_version" not in dumped
    schema = committed_schema(schema_filename)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(dumped)
    assert model.model_validate(dumped).model_dump(mode="json") == dumped


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory"),
    TOP_LEVEL_CONTRACT_CASES,
)
def test_all_top_level_contracts_fail_closed_on_extra_secrets(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
) -> None:
    from deeptutor.teaching import contracts

    model = getattr(contracts, model_name)
    dumped = model.model_validate(payload_factory()).model_dump(mode="json")
    dumped["providerApiKey"] = "must-not-cross-the-contract"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(dumped)
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema(schema_filename)).validate(dumped)


@pytest.mark.parametrize(
    ("field_name", "bad_value", "camel_name"),
    [
        ("source_snapshot", None, "sourceSnapshot"),
        ("source_fragments", [], "sourceFragments"),
        ("citations", [], "citations"),
        ("source_refs", [], "sourceRefs"),
    ],
)
def test_source_grounded_brief_requires_authorized_source_material(
    field_name: str,
    bad_value: object,
    camel_name: str,
) -> None:
    from deeptutor.teaching.contracts import TeachingBrief

    payload = valid_teaching_brief()
    payload[field_name] = bad_value
    with pytest.raises(
        ValidationError,
        match="source-grounded brief requires snapshot, fragments, citations, and source refs",
    ):
        TeachingBrief.model_validate(payload)

    dumped = TeachingBrief.model_validate(valid_teaching_brief()).model_dump(mode="json")
    dumped[camel_name] = bad_value
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("teaching-brief.schema.json")).validate(dumped)


@pytest.mark.parametrize(
    ("field_name", "camel_name"),
    [
        ("allowed_source_ids", "allowedSourceIds"),
        ("allowed_fragment_ids", "allowedFragmentIds"),
    ],
)
def test_source_grounded_brief_requires_nonempty_source_and_fragment_allowlists(
    field_name: str,
    camel_name: str,
) -> None:
    from deeptutor.teaching.contracts import TeachingBrief

    payload = valid_teaching_brief()
    permission_summary = payload["permission_summary"]
    assert isinstance(permission_summary, dict)
    permission_summary[field_name] = []
    with pytest.raises(ValidationError, match="allowed source and fragment"):
        TeachingBrief.model_validate(payload)

    dumped = TeachingBrief.model_validate(valid_teaching_brief()).model_dump(mode="json")
    dumped["permissionSummary"][camel_name] = []
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("teaching-brief.schema.json")).validate(dumped)


@pytest.mark.parametrize(
    "camel_name",
    ["allowedSourceIds", "allowedFragmentIds"],
)
def test_source_grounded_schema_rejects_duplicate_permission_allowlist_ids(
    camel_name: str,
) -> None:
    from deeptutor.teaching.contracts import TeachingBrief

    dumped = TeachingBrief.model_validate(valid_teaching_brief()).model_dump(mode="json")
    allowlist = dumped["permissionSummary"][camel_name]
    allowlist.append(allowlist[0])
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("teaching-brief.schema.json")).validate(dumped)


@pytest.mark.parametrize("collection_name", ["source_fragments", "citations", "source_refs"])
def test_source_grounded_brief_rejects_source_ids_outside_allowed_set(
    collection_name: str,
) -> None:
    from deeptutor.teaching.contracts import TeachingBrief

    payload = valid_teaching_brief()
    collection = payload[collection_name]
    assert isinstance(collection, list)
    first = collection[0]
    assert isinstance(first, dict)
    first["source_id"] = "source-not-authorized"
    with pytest.raises(ValidationError, match="outside permission summary"):
        TeachingBrief.model_validate(payload)

    schema = committed_schema("teaching-brief.schema.json")
    assert "cross-array" in schema["$comment"].lower()
    assert "semantic validation" in schema["$comment"].lower()
    dumped = TeachingBrief.model_validate(valid_teaching_brief()).model_dump(mode="json")
    camel_collection = {
        "source_fragments": "sourceFragments",
        "citations": "citations",
        "source_refs": "sourceRefs",
    }[collection_name]
    dumped_collection = dumped[camel_collection]
    assert isinstance(dumped_collection, list)
    dumped_collection[0]["sourceId"] = "source-not-authorized"
    # Draft 2020-12 cannot compare values across sibling arrays. The committed
    # schema documents that boundary; Pydantic owns the membership check.
    Draft202012Validator(schema).validate(dumped)


@pytest.mark.parametrize(
    "violation",
    [
        "fragment_outside_allowlist",
        "nonexistent_allowed_fragment",
        "duplicate_allowed_source",
        "duplicate_allowed_fragment",
        "duplicate_fragment_id",
        "citation_unknown_fragment",
        "citation_wrong_source",
        "duplicate_citation_id",
        "source_ref_unknown_citation",
        "source_ref_wrong_source",
        "source_ref_wrong_fragment",
        "duplicate_source_ref",
    ],
)
def test_source_grounded_brief_rejects_broken_source_lineage(
    violation: str,
) -> None:
    from deeptutor.teaching.contracts import TeachingBrief

    payload = valid_teaching_brief()
    permission_summary = payload["permission_summary"]
    fragments = payload["source_fragments"]
    citations = payload["citations"]
    source_refs = payload["source_refs"]
    assert isinstance(permission_summary, dict)
    assert isinstance(fragments, list)
    assert isinstance(citations, list)
    assert isinstance(source_refs, list)

    if violation == "fragment_outside_allowlist":
        fragments[0]["fragment_id"] = "fragment-not-authorized"
    elif violation == "nonexistent_allowed_fragment":
        permission_summary["allowed_fragment_ids"].append("fragment-missing")
    elif violation == "duplicate_allowed_source":
        permission_summary["allowed_source_ids"].append("source-1")
    elif violation == "duplicate_allowed_fragment":
        permission_summary["allowed_fragment_ids"].append("fragment-1")
    elif violation == "duplicate_fragment_id":
        fragments.append(copy.deepcopy(fragments[0]))
    elif violation == "citation_unknown_fragment":
        citations[0]["fragment_id"] = "fragment-missing"
    elif violation == "citation_wrong_source":
        permission_summary["allowed_source_ids"].append("source-2")
        citations[0]["source_id"] = "source-2"
    elif violation == "duplicate_citation_id":
        citations.append(copy.deepcopy(citations[0]))
    elif violation == "source_ref_unknown_citation":
        source_refs[0]["citation_id"] = "citation-missing"
    elif violation == "source_ref_wrong_source":
        permission_summary["allowed_source_ids"].append("source-2")
        source_refs[0]["source_id"] = "source-2"
    elif violation == "source_ref_wrong_fragment":
        source_refs[0]["fragment_id"] = "fragment-missing"
    elif violation == "duplicate_source_ref":
        source_refs.append(copy.deepcopy(source_refs[0]))
    else:
        raise AssertionError(f"unhandled violation: {violation}")

    with pytest.raises(ValidationError, match="source-grounded brief"):
        TeachingBrief.model_validate(payload)


def test_source_lineage_cross_array_boundary_is_documented_for_json_schema() -> None:
    from deeptutor.teaching.contracts import TeachingBrief

    schema = committed_schema("teaching-brief.schema.json")
    comment = schema["$comment"].lower()
    assert "fragment" in comment
    assert "citation" in comment
    assert "sourceref" in comment
    assert "semantic validation" in comment

    dumped = TeachingBrief.model_validate(valid_teaching_brief()).model_dump(mode="json")
    dumped["citations"][0]["sourceId"] = "source-not-matching-fragment"
    # Draft 2020-12 cannot join sibling arrays by their identifier fields.
    Draft202012Validator(schema).validate(dumped)


def test_open_creation_brief_allows_no_source_material() -> None:
    from deeptutor.teaching.contracts import TeachingBrief

    payload = valid_teaching_brief()
    payload.update(
        {
            "content_mode": "open_creation",
            "source_snapshot": None,
            "source_fragments": [],
            "citations": [],
            "source_refs": [],
        }
    )
    permission_summary = payload["permission_summary"]
    assert isinstance(permission_summary, dict)
    permission_summary["allowed_source_ids"] = []
    permission_summary["allowed_fragment_ids"] = []

    dumped = TeachingBrief.model_validate(payload).model_dump(mode="json")
    Draft202012Validator(committed_schema("teaching-brief.schema.json")).validate(dumped)


def test_teaching_brief_contains_integration_design_contract_fields() -> None:
    from deeptutor.teaching.contracts import TeachingBrief

    required = set(TeachingBrief.model_json_schema()["required"])
    assert {
        "briefId",
        "briefVersion",
        "tenantId",
        "courseId",
        "targetClassId",
        "gradeBand",
        "audienceLevel",
        "classroomMode",
        "objectives",
        "durationMinutes",
        "knowledgePoints",
        "prerequisites",
        "assessment",
        "sourceSnapshot",
        "sourceFragments",
        "citations",
        "sourceRefs",
        "permissionSummary",
        "contentMode",
        "networkPolicy",
        "mediaPolicy",
        "templatePolicy",
        "safetyPolicy",
        "contentSha256",
    } <= required
    assert TeachingBrief.model_json_schema()["properties"]["contentMode"]["enum"] == [
        "source_grounded",
        "open_creation",
    ]


def test_outline_bundle_contains_version_confirmation_and_coverage_metadata() -> None:
    from deeptutor.teaching.contracts import OutlineBundle

    required = set(OutlineBundle.model_json_schema()["required"])
    assert {
        "outlineVersion",
        "confirmationMetadata",
        "title",
        "language",
        "scenes",
        "knowledgeCoverage",
        "sourceRefs",
        "estimatedSceneCount",
        "generationMetadata",
        "contractSha256",
    } <= required


def test_openmaic_document_accepts_all_scene_discriminators_losslessly() -> None:
    from deeptutor.teaching.contracts import ClassroomDocument

    payload = valid_classroom_document()
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list)
    scenes.extend(
        [
            {
                "id": "scene-quiz",
                "stage_id": "stage-1",
                "title": "Check understanding",
                "order": 1,
                "type": "quiz",
                "content": {
                    "type": "quiz",
                    "questions": [
                        {
                            "id": "question-1",
                            "prompt": "Which function is periodic?",
                            "question_type": "single_choice",
                            "options": [
                                {"id": "a", "label": "sin(x)"},
                                {"id": "b", "label": "x"},
                            ],
                            "correct_option_ids": ["a"],
                            "explanation": "Sine repeats every 2π.",
                        }
                    ],
                },
            },
            {
                "id": "scene-interactive",
                "stage_id": "stage-1",
                "title": "Explore frequencies",
                "order": 2,
                "type": "interactive",
                "content": {
                    "type": "interactive",
                    "html": "<button id='frequency-slider'>Explore</button>",
                    "bridge_version": "1.0",
                    "sandbox": {
                        "allow_scripts": True,
                        "allow_same_origin": False,
                    },
                },
                "actions": [{"type": "highlight", "target": "frequency-slider"}],
            },
            {
                "id": "scene-pbl",
                "stage_id": "stage-1",
                "title": "Build a signal analyzer",
                "order": 3,
                "type": "pbl",
                "content": {
                    "type": "pbl",
                    "scenario": "Build a Fourier signal analyzer for the school lab.",
                    "roles": [
                        {
                            "id": "engineer",
                            "name": "Signal engineer",
                            "brief": "Design and explain the analyzer.",
                        }
                    ],
                    "milestones": [
                        {
                            "id": "m1",
                            "title": "Working prototype",
                            "rubric": "Identifies at least three signal frequencies.",
                        }
                    ],
                },
            },
        ]
    )
    payload["interaction_ids"] = ["scene-quiz", "scene-interactive", "scene-pbl"]

    parsed = ClassroomDocument.model_validate(payload)
    dumped = parsed.model_dump(mode="json")
    Draft202012Validator(committed_schema("classroom-document.schema.json")).validate(dumped)
    assert dumped["openmaic"]["dslVersion"] == "0.1.0"
    assert [scene["type"] for scene in dumped["openmaic"]["scenes"]] == [
        "slide",
        "quiz",
        "interactive",
        "pbl",
    ]
    assert dumped["openmaic"]["scenes"][0]["content"]["canvas"]["elements"][0]["meta"]["tags"] == [
        "intro",
        1,
        True,
        None,
    ]
    assert dumped["openmaic"]["scenes"][2]["actions"][0]["target"] == ("frequency-slider")


def test_portable_scene_content_matches_task4_without_arbitrary_config() -> None:
    from deeptutor.teaching.contracts import (
        ClassroomDocument,
        InteractiveSceneContent,
        PblSceneContent,
    )

    interactive = {
        "type": "interactive",
        "html": "<button>Run</button>",
        "bridge_version": "1.0",
        "sandbox": {
            "allow_scripts": True,
            "allow_same_origin": False,
        },
    }
    pbl = {
        "type": "pbl",
        "scenario": "Investigate a noisy classroom signal.",
        "roles": [
            {
                "id": "analyst",
                "name": "Signal analyst",
                "brief": "Find the dominant frequencies.",
            }
        ],
        "milestones": [
            {
                "id": "m1",
                "title": "Frequency report",
                "rubric": "Names and justifies the dominant frequencies.",
            }
        ],
    }

    assert InteractiveSceneContent.model_validate(interactive).model_dump(mode="json") == {
        "type": "interactive",
        "html": "<button>Run</button>",
        "bridgeVersion": "1.0",
        "sandbox": {
            "allowScripts": True,
            "allowSameOrigin": False,
        },
    }
    assert PblSceneContent.model_validate(pbl).model_dump(mode="json")["scenario"] == (
        "Investigate a noisy classroom signal."
    )
    with pytest.raises(ValidationError):
        InteractiveSceneContent.model_validate(
            {"type": "interactive", "config": {"kind": "simulation"}}
        )
    with pytest.raises(ValidationError):
        PblSceneContent.model_validate({"type": "pbl", "config": {"kind": "project"}})
    with pytest.raises(ValidationError):
        InteractiveSceneContent.model_validate({**interactive, "html": ""})
    with pytest.raises(ValidationError):
        PblSceneContent.model_validate({**pbl, "scenario": ""})
    with pytest.raises(ValidationError):
        PblSceneContent.model_validate({**pbl, "roles": []})
    with pytest.raises(ValidationError):
        PblSceneContent.model_validate({**pbl, "milestones": []})

    definitions = ClassroomDocument.model_json_schema()["$defs"]
    interactive_schema = definitions["InteractiveSceneContent"]
    assert set(interactive_schema["properties"]) == {
        "type",
        "html",
        "bridgeVersion",
        "sandbox",
    }
    assert set(interactive_schema["required"]) == {
        "type",
        "html",
        "bridgeVersion",
        "sandbox",
    }
    pbl_schema = definitions["PblSceneContent"]
    assert set(pbl_schema["properties"]) == {
        "type",
        "scenario",
        "roles",
        "milestones",
    }
    assert set(pbl_schema["required"]) == {
        "type",
        "scenario",
        "roles",
        "milestones",
    }


@pytest.mark.parametrize(
    ("manifest_name", "field_name", "camel_name"),
    [
        ("media_manifest", "sha256", "sha256"),
        ("media_manifest", "size_bytes", "sizeBytes"),
        ("media_manifest", "mime_type", "mimeType"),
        ("export_manifest", "sha256", "sha256"),
        ("export_manifest", "size_bytes", "sizeBytes"),
        ("export_manifest", "mime_type", "mimeType"),
        ("export_manifest", "temporary_download_path", "temporaryDownloadPath"),
        ("export_manifest", "expires_at", "expiresAt"),
    ],
)
def test_manifest_files_require_integrity_and_export_download_metadata(
    manifest_name: str,
    field_name: str,
    camel_name: str,
) -> None:
    from deeptutor.teaching.contracts import ClassroomDocument

    payload = valid_classroom_document()
    manifest = payload[manifest_name]
    assert isinstance(manifest, list)
    item = manifest[0]
    assert isinstance(item, dict)
    del item[field_name]
    with pytest.raises(ValidationError):
        ClassroomDocument.model_validate(payload)

    dumped = ClassroomDocument.model_validate(valid_classroom_document()).model_dump(mode="json")
    dumped_manifest = dumped[
        "mediaManifest" if manifest_name == "media_manifest" else "exportManifest"
    ]
    del dumped_manifest[0][camel_name]
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("classroom-document.schema.json")).validate(dumped)


def test_media_manifest_deprecated_download_metadata_is_optional() -> None:
    from deeptutor.teaching.contracts import ClassroomDocument

    payload = valid_classroom_document()
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    del manifest[0]["temporary_download_path"]
    del manifest[0]["expires_at"]

    dumped = ClassroomDocument.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )

    assert "temporaryDownloadPath" not in dumped["mediaManifest"][0]
    assert "expiresAt" not in dumped["mediaManifest"][0]
    schema = committed_schema("classroom-document.schema.json")
    media_schema = schema["$defs"]["MediaManifestItem"]
    assert "temporaryDownloadPath" not in media_schema["required"]
    assert "expiresAt" not in media_schema["required"]
    assert media_schema["properties"]["temporaryDownloadPath"]["deprecated"] is True
    assert media_schema["properties"]["expiresAt"]["deprecated"] is True
    Draft202012Validator(schema).validate(dumped)


def test_omitted_media_download_metadata_stays_omitted_in_normal_model_dumps() -> None:
    from deeptutor.teaching.contracts import ClassroomDocument, MediaManifestItem

    payload = valid_classroom_document()
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    del manifest[0]["temporary_download_path"]
    del manifest[0]["expires_at"]

    entry = MediaManifestItem.model_validate(manifest[0])
    direct = entry.model_dump(mode="json", by_alias=True)
    nested = ClassroomDocument.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
    )["mediaManifest"][0]

    assert "temporaryDownloadPath" not in direct
    assert "expiresAt" not in direct
    assert "temporaryDownloadPath" not in nested
    assert "expiresAt" not in nested


@pytest.mark.parametrize(
    ("field_name", "camel_name"),
    [
        ("temporary_download_path", "temporaryDownloadPath"),
        ("expires_at", "expiresAt"),
    ],
)
def test_optional_media_download_metadata_rejects_explicit_null(
    field_name: str,
    camel_name: str,
) -> None:
    from deeptutor.teaching.contracts import ClassroomDocument

    payload = valid_classroom_document()
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    manifest[0][field_name] = None
    with pytest.raises(ValidationError):
        ClassroomDocument.model_validate(payload)

    dumped = ClassroomDocument.model_validate(valid_classroom_document()).model_dump(
        mode="json"
    )
    dumped["mediaManifest"][0][camel_name] = None
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("classroom-document.schema.json")).validate(
            dumped
        )


@pytest.mark.parametrize("content_type", ["quiz", "interactive", "pbl"])
def test_openmaic_scene_type_must_match_content_discriminator(
    content_type: str,
) -> None:
    from deeptutor.teaching.contracts import ClassroomDocument

    payload = valid_classroom_document()
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list)
    scene = scenes[0]
    assert isinstance(scene, dict)
    if content_type == "quiz":
        scene["content"] = {"type": "quiz", "questions": []}
    else:
        scene["content"] = {"type": content_type, "config": {}}

    with pytest.raises(ValidationError):
        ClassroomDocument.model_validate(payload)

    dumped = ClassroomDocument.model_validate(valid_classroom_document()).model_dump(mode="json")
    if content_type == "quiz":
        dumped["openmaic"]["scenes"][0]["content"] = {
            "type": "quiz",
            "questions": [],
        }
    else:
        dumped["openmaic"]["scenes"][0]["content"] = {
            "type": content_type,
            "config": {},
        }
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("classroom-document.schema.json")).validate(dumped)


def test_openmaic_json_value_rejects_non_json_python_objects() -> None:
    from deeptutor.teaching.contracts import ClassroomDocument

    payload = valid_classroom_document()
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list)
    scene = scenes[0]
    assert isinstance(scene, dict)
    content = scene["content"]
    assert isinstance(content, dict)
    canvas = content["canvas"]
    assert isinstance(canvas, dict)
    canvas["not_json"] = object()

    with pytest.raises(ValidationError):
        ClassroomDocument.model_validate(payload)


def test_classroom_content_mode_and_open_creation_flag_must_agree() -> None:
    from deeptutor.teaching.contracts import ClassroomDocument

    payload = valid_classroom_document()
    payload["open_creation"] = True
    with pytest.raises(ValidationError, match="open creation flag"):
        ClassroomDocument.model_validate(payload)

    dumped = ClassroomDocument.model_validate(valid_classroom_document()).model_dump(mode="json")
    dumped["openCreation"] = True
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("classroom-document.schema.json")).validate(dumped)


def test_generation_request_brief_identity_and_hash_must_match_embedded_brief() -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    wrong_id = valid_generation_request()
    wrong_id["teaching_brief_id"] = "brief-other"
    with pytest.raises(ValidationError, match="teaching brief identity"):
        GenerationRequest.model_validate(wrong_id)

    wrong_hash = valid_generation_request()
    wrong_hash["teaching_brief_sha256"] = OTHER_SHA256
    with pytest.raises(ValidationError, match="teaching brief hash"):
        GenerationRequest.model_validate(wrong_hash)


@pytest.mark.parametrize("field_name", ["callback_context", "data_plane_route_id"])
def test_generation_request_opaque_routing_fields_reject_urls(
    field_name: str,
) -> None:
    from deeptutor.teaching.contracts import GenerationRequest

    payload = valid_generation_request()
    payload[field_name] = "https://example.invalid/callback"
    with pytest.raises(ValidationError):
        GenerationRequest.model_validate(payload)

    dumped = GenerationRequest.model_validate(valid_generation_request()).model_dump(mode="json")
    camel_name = "callbackContext" if field_name == "callback_context" else "dataPlaneRouteId"
    dumped[camel_name] = "https://example.invalid/callback"
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema("generation-request.schema.json")).validate(dumped)


def test_confirmed_outline_hash_uses_documented_canonical_bundle_json() -> None:
    from deeptutor.teaching.contracts import (
        OutlineBundle,
        canonical_json_bytes,
        canonical_outline_json_bytes,
        canonical_outline_sha256,
        canonical_sha256,
    )

    payload = valid_outline_bundle()
    payload["title"] = "傅里叶级数"
    outline = OutlineBundle.model_validate(payload)
    expected = json.dumps(
        outline.model_dump(mode="json", by_alias=True, exclude_none=True),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert canonical_json_bytes(outline) == expected
    assert canonical_outline_json_bytes(outline) == expected
    assert canonical_sha256(outline) == hashlib.sha256(expected).hexdigest()
    assert canonical_outline_sha256(outline) == hashlib.sha256(expected).hexdigest()
    schema = committed_schema("generation-request.schema.json")
    assert "canonical UTF-8 JSON" in schema["$comment"]
    assert (
        "entire confirmed OutlineBundle"
        in schema["properties"]["confirmedOutlineSha256"]["description"]
    )


def test_outline_hash_normalizes_only_schema_known_rfc3339_fields() -> None:
    from deeptutor.teaching.contracts import (
        OutlineBundle,
        canonical_json_bytes,
        canonical_outline_json_bytes,
        canonical_outline_sha256,
    )

    outline = OutlineBundle.model_validate(valid_outline_bundle())
    dumped = outline.model_dump(mode="json", exclude_none=True)
    utc_offset_dumped = copy.deepcopy(dumped)
    utc_offset_dumped["confirmationMetadata"]["confirmedAt"] = "2026-07-30T16:00:00+08:00"
    utc_offset_dumped["generationMetadata"]["generatedAt"] = "2026-07-30T16:00:00+08:00"
    offset_dumped = copy.deepcopy(utc_offset_dumped)
    offset_dumped["title"] = "2026-07-30T16:00:00+08:00"

    normalized = json.loads(canonical_outline_json_bytes(offset_dumped))
    assert normalized["confirmationMetadata"]["confirmedAt"] == GENERATED_AT
    assert normalized["generationMetadata"]["generatedAt"] == GENERATED_AT
    assert normalized["title"] == "2026-07-30T16:00:00+08:00"
    assert canonical_outline_sha256(outline) == canonical_outline_sha256(dumped)
    assert canonical_outline_sha256(outline) == canonical_outline_sha256(utc_offset_dumped)

    body = {"body": "2026-07-30T16:00:00+08:00"}
    assert canonical_json_bytes(body) == (b'{"body":"2026-07-30T16:00:00+08:00"}')


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory", "status", "field_name"),
    [
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "succeeded",
            "output_sha256",
        ),
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "succeeded",
            "final_artifact",
        ),
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "failed",
            "error",
        ),
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "canceled",
            "canceled_at",
        ),
        (
            "ExportJob",
            "export-job.schema.json",
            valid_export_job,
            "succeeded",
            "output_sha256",
        ),
        (
            "ExportJob",
            "export-job.schema.json",
            valid_export_job,
            "succeeded",
            "final_artifact",
        ),
        (
            "ExportJob",
            "export-job.schema.json",
            valid_export_job,
            "failed",
            "error",
        ),
        (
            "ExportJob",
            "export-job.schema.json",
            valid_export_job,
            "canceled",
            "canceled_at",
        ),
    ],
)
def test_terminal_job_invariants_are_enforced_by_pydantic_and_json_schema(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
    status: str,
    field_name: str,
) -> None:
    from deeptutor.teaching import contracts

    model = getattr(contracts, model_name)
    payload = payload_factory(status)
    payload[field_name] = None
    with pytest.raises(ValidationError):
        model.model_validate(payload)

    valid_dump = model.model_validate(payload_factory(status)).model_dump(mode="json")
    camel_name = {
        "output_sha256": "outputSha256",
        "final_artifact": "finalArtifact",
        "canceled_at": "canceledAt",
        "error": "error",
    }[field_name]
    valid_dump[camel_name] = None
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema(schema_filename)).validate(valid_dump)


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory"),
    [
        ("GenerationJob", "generation-job.schema.json", valid_generation_job),
        ("ExportJob", "export-job.schema.json", valid_export_job),
    ],
)
def test_ready_artifact_requires_sha256_in_model_and_schema(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
) -> None:
    from deeptutor.teaching import contracts

    payload = payload_factory("succeeded")
    artifact = payload["final_artifact"]
    assert isinstance(artifact, dict)
    artifact["sha256"] = None
    with pytest.raises(ValidationError, match="ready artifact requires sha256"):
        getattr(contracts, model_name).model_validate(payload)

    dumped = (
        getattr(contracts, model_name)
        .model_validate(payload_factory("succeeded"))
        .model_dump(mode="json")
    )
    dumped["finalArtifact"]["sha256"] = None
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema(schema_filename)).validate(dumped)


@pytest.mark.parametrize(
    (
        "model_name",
        "schema_filename",
        "payload_factory",
        "removed_kind",
        "expected_kinds",
    ),
    [
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "dsl_json",
            {"dsl_json", "media"},
        ),
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "media",
            {"dsl_json", "media"},
        ),
        (
            "ExportJob",
            "export-job.schema.json",
            valid_export_job,
            "export",
            {"export"},
        ),
    ],
)
def test_succeeded_job_manifest_requires_task4_file_kinds(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
    removed_kind: str,
    expected_kinds: set[str],
) -> None:
    from deeptutor.teaching import contracts

    model = getattr(contracts, model_name)
    valid_payload = payload_factory("succeeded")
    parsed = model.model_validate(valid_payload)
    assert {item.kind for item in parsed.artifact_manifest} == expected_kinds
    Draft202012Validator(committed_schema(schema_filename)).validate(parsed.model_dump(mode="json"))

    payload = payload_factory("succeeded")
    manifest = payload["artifact_manifest"]
    assert isinstance(manifest, list)
    payload["artifact_manifest"] = [
        item for item in manifest if isinstance(item, dict) and item["kind"] != removed_kind
    ]
    with pytest.raises(ValidationError, match="missing required file kinds"):
        model.model_validate(payload)

    dumped = parsed.model_dump(mode="json")
    dumped["artifactManifest"] = [
        item for item in dumped["artifactManifest"] if item["kind"] != removed_kind
    ]
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema(schema_filename)).validate(dumped)


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory"),
    [
        ("GenerationJob", "generation-job.schema.json", valid_generation_job),
        ("ExportJob", "export-job.schema.json", valid_export_job),
    ],
)
@pytest.mark.parametrize(
    ("field_name", "camel_name"),
    [
        ("sha256", "sha256"),
        ("size_bytes", "sizeBytes"),
        ("mime_type", "mimeType"),
        ("temporary_download_path", "temporaryDownloadPath"),
        ("expires_at", "expiresAt"),
    ],
)
def test_job_manifest_every_file_requires_task4_metadata(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
    field_name: str,
    camel_name: str,
) -> None:
    from deeptutor.teaching import contracts

    model = getattr(contracts, model_name)
    payload = payload_factory("succeeded")
    manifest = payload["artifact_manifest"]
    assert isinstance(manifest, list)
    item = manifest[0]
    assert isinstance(item, dict)
    del item[field_name]
    with pytest.raises(ValidationError):
        model.model_validate(payload)

    dumped = model.model_validate(payload_factory("succeeded")).model_dump(mode="json")
    del dumped["artifactManifest"][0][camel_name]
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema(schema_filename)).validate(dumped)


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory"),
    [
        ("GenerationJob", "generation-job.schema.json", valid_generation_job),
        ("ExportJob", "export-job.schema.json", valid_export_job),
    ],
)
def test_succeeded_job_primary_manifest_hash_matches_output_semantically(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
) -> None:
    from deeptutor.teaching import contracts

    model = getattr(contracts, model_name)
    payload = payload_factory("succeeded")
    manifest = payload["artifact_manifest"]
    assert isinstance(manifest, list)
    primary = manifest[0]
    assert isinstance(primary, dict)
    primary["sha256"] = SHA256
    with pytest.raises(ValidationError, match="primary artifact manifest sha256"):
        model.model_validate(payload)

    schema = committed_schema(schema_filename)
    assert "primary artifactManifest" in schema["$comment"]
    dumped = model.model_validate(payload_factory("succeeded")).model_dump(mode="json")
    dumped["artifactManifest"][0]["sha256"] = SHA256
    Draft202012Validator(schema).validate(dumped)


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory", "status", "field_name"),
    [
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "failed",
            "output_sha256",
        ),
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "canceled",
            "final_artifact",
        ),
        (
            "ExportJob",
            "export-job.schema.json",
            valid_export_job,
            "failed",
            "final_artifact",
        ),
        (
            "ExportJob",
            "export-job.schema.json",
            valid_export_job,
            "canceled",
            "output_sha256",
        ),
    ],
)
def test_unsuccessful_terminal_jobs_forbid_output_and_ready_final_artifact(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
    status: str,
    field_name: str,
) -> None:
    from deeptutor.teaching import contracts

    payload = payload_factory(status)
    if field_name == "output_sha256":
        payload[field_name] = OTHER_SHA256
        camel_name = "outputSha256"
        camel_value: object = OTHER_SHA256
    else:
        payload[field_name] = {
            "artifact_id": "artifact-residual",
            "status": "ready",
            "sha256": OTHER_SHA256,
        }
        camel_name = "finalArtifact"
        camel_value = {
            "artifactId": "artifact-residual",
            "status": "ready",
            "sha256": OTHER_SHA256,
        }
    with pytest.raises(ValidationError, match="failed or canceled job"):
        getattr(contracts, model_name).model_validate(payload)

    dumped = (
        getattr(contracts, model_name)
        .model_validate(payload_factory(status))
        .model_dump(mode="json")
    )
    dumped[camel_name] = camel_value
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema(schema_filename)).validate(dumped)


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory"),
    [
        ("GenerationJob", "generation-job.schema.json", valid_generation_job),
        ("ExportJob", "export-job.schema.json", valid_export_job),
    ],
)
def test_succeeded_job_output_hash_matches_final_artifact_semantically(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
) -> None:
    from deeptutor.teaching import contracts

    payload = payload_factory("succeeded")
    artifact = payload["final_artifact"]
    assert isinstance(artifact, dict)
    artifact["sha256"] = SHA256
    with pytest.raises(ValidationError, match="final artifact sha256"):
        getattr(contracts, model_name).model_validate(payload)

    schema = committed_schema(schema_filename)
    assert "semantic validation" in schema["$comment"].lower()
    dumped = (
        getattr(contracts, model_name)
        .model_validate(payload_factory("succeeded"))
        .model_dump(mode="json")
    )
    dumped["finalArtifact"]["sha256"] = SHA256
    # Draft 2020-12 cannot assert equality between sibling values.
    Draft202012Validator(schema).validate(dumped)


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory"),
    [
        ("GenerationJob", "generation-job.schema.json", valid_generation_job),
        ("ExportJob", "export-job.schema.json", valid_export_job),
    ],
)
def test_failed_jobs_may_keep_only_temporary_nonready_residuals(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
) -> None:
    from deeptutor.teaching import contracts

    payload = payload_factory("failed")
    payload["temporary_artifact"] = {
        "artifact_id": "artifact-temporary",
        "status": "failed",
        "sha256": None,
    }
    model = getattr(contracts, model_name)
    dumped = model.model_validate(payload).model_dump(mode="json")
    Draft202012Validator(committed_schema(schema_filename)).validate(dumped)


@pytest.mark.parametrize(
    ("model_name", "schema_filename", "payload_factory"),
    [
        ("GenerationJob", "generation-job.schema.json", valid_generation_job),
        ("ExportJob", "export-job.schema.json", valid_export_job),
    ],
)
def test_job_lease_fields_are_all_present_or_all_absent(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
) -> None:
    from deeptutor.teaching import contracts

    payload = payload_factory()
    payload["lease_id"] = "lease-1"
    with pytest.raises(ValidationError, match="lease fields"):
        getattr(contracts, model_name).model_validate(payload)

    dumped = (
        getattr(contracts, model_name).model_validate(payload_factory()).model_dump(mode="json")
    )
    dumped["leaseId"] = "lease-1"
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema(schema_filename)).validate(dumped)


@pytest.mark.parametrize(
    (
        "model_name",
        "schema_filename",
        "payload_factory",
        "running_status",
        "running_phase",
    ),
    [
        (
            "GenerationJob",
            "generation-job.schema.json",
            valid_generation_job,
            "generating_content",
            "content",
        ),
        (
            "ExportJob",
            "export-job.schema.json",
            valid_export_job,
            "exporting",
            "exporting",
        ),
    ],
)
def test_running_jobs_require_complete_lease(
    model_name: str,
    schema_filename: str,
    payload_factory: PayloadFactory,
    running_status: str,
    running_phase: str,
) -> None:
    from deeptutor.teaching import contracts

    payload = payload_factory(running_status)
    payload["phase"] = running_phase
    with pytest.raises(ValidationError, match="running job requires"):
        getattr(contracts, model_name).model_validate(payload)

    dumped = (
        getattr(contracts, model_name).model_validate(payload_factory()).model_dump(mode="json")
    )
    dumped.update({"status": running_status, "phase": running_phase})
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(committed_schema(schema_filename)).validate(dumped)

    payload.update(
        {
            "lease_id": "lease-1",
            "lease_owner": "worker-1",
            "lease_expires_at": GENERATED_AT,
            "heartbeat_at": GENERATED_AT,
        }
    )
    parsed = getattr(contracts, model_name).model_validate(payload)
    Draft202012Validator(committed_schema(schema_filename)).validate(parsed.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("model_name", "payload_factory"),
    [
        ("GenerationJob", valid_generation_job),
        ("ExportJob", valid_export_job),
    ],
)
def test_job_error_category_is_fixed(
    model_name: str,
    payload_factory: PayloadFactory,
) -> None:
    from deeptutor.teaching import contracts

    payload = payload_factory("failed")
    error = payload["error"]
    assert isinstance(error, dict)
    error["category"] = "surprise"
    with pytest.raises(ValidationError):
        getattr(contracts, model_name).model_validate(payload)


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


@pytest.mark.parametrize("stray_filename", ["stray.schema.json", "README.md"])
def test_schema_verifier_rejects_stray_files(
    tmp_path: Path,
    stray_filename: str,
) -> None:
    from scripts.verify_classroom_contracts import (
        generated_contract_schemas,
        verify_contract_schemas,
    )

    for filename, schema in generated_contract_schemas().items():
        (tmp_path / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    (tmp_path / stray_filename).write_text("{}\n", encoding="utf-8")

    assert verify_contract_schemas(tmp_path) == [
        f"{stray_filename}: unexpected",
    ]
