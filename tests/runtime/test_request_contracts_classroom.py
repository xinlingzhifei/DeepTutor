"""Public request contract for the interactive classroom capability."""

from __future__ import annotations

import importlib

import pytest

from deeptutor.runtime.request_contracts import (
    get_capability_request_schema,
    validate_capability_config,
)


def _validate(raw_config: dict[str, object]) -> dict[str, object]:
    return validate_capability_config("interactive_classroom", raw_config)


def test_classroom_request_requires_explicit_student_choice() -> None:
    with pytest.raises(ValueError, match="mode"):
        _validate({"course_id": "course-a", "question": "Explain Fourier transform"})


@pytest.mark.parametrize("mode", ["micro", "full"])
def test_classroom_request_applies_safe_defaults(mode: str) -> None:
    assert _validate({"mode": mode, "course_id": "course-a"}) == {
        "mode": mode,
        "course_id": "course-a",
        "question": "",
        "content_mode": "source_grounded",
    }


@pytest.mark.parametrize(
    "raw_config, field",
    [
        ({"mode": "preview", "course_id": "course-a"}, "mode"),
        ({"mode": "micro", "course_id": ""}, "course_id"),
        ({"mode": "micro", "course_id": "   "}, "course_id"),
        ({"mode": "micro", "course_id": "course-a", "question": "x" * 4001}, "question"),
        (
            {
                "mode": "micro",
                "course_id": "course-a",
                "content_mode": "external_source",
            },
            "content_mode",
        ),
    ],
)
def test_classroom_request_rejects_invalid_public_fields(
    raw_config: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        _validate(raw_config)


@pytest.mark.parametrize(
    "trusted_field",
    ["userId", "tenantId", "objectKey", "providerProfileId"],
)
def test_classroom_request_rejects_client_supplied_trusted_fields(
    trusted_field: str,
) -> None:
    with pytest.raises(ValueError, match=trusted_field):
        _validate(
            {
                "mode": "micro",
                "course_id": "course-a",
                trusted_field: "attacker-controlled",
            }
        )


def test_classroom_request_model_forbids_extra_fields_directly() -> None:
    module = importlib.import_module(
        "deeptutor.agents.interactive_classroom.request_config"
    )
    model_type = module.InteractiveClassroomRequestConfig
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        model_type.model_validate(
            {
                "mode": "micro",
                "course_id": "course-a",
                "tenantId": "tenant-from-client",
            }
        )


def test_classroom_manifest_schema_uses_unified_request_contract() -> None:
    schema = get_capability_request_schema("interactive_classroom")

    assert set(schema["required"]) == {"mode", "course_id"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["mode"]["enum"] == ["micro", "full"]
    assert schema["properties"]["question"]["maxLength"] == 4000
    assert schema["properties"]["content_mode"]["default"] == "source_grounded"
