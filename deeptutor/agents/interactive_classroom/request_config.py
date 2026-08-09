"""Validated request config for the interactive classroom capability."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class InteractiveClassroomRequestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["micro", "full"]
    course_id: str = Field(min_length=1)
    question: str = Field(default="", max_length=4000)
    content_mode: Literal["source_grounded", "open_creation"] = "source_grounded"

    @field_validator("course_id")
    @classmethod
    def course_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("course_id cannot be blank")
        return value


def validate_interactive_classroom_request_config(
    raw_config: dict[str, Any] | None,
) -> InteractiveClassroomRequestConfig:
    if not isinstance(raw_config, dict):
        raise ValueError("Interactive classroom requires an explicit config object.")
    try:
        return InteractiveClassroomRequestConfig.model_validate(raw_config)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(f"Invalid interactive classroom config: {details}") from exc


__all__ = [
    "InteractiveClassroomRequestConfig",
    "validate_interactive_classroom_request_config",
]
