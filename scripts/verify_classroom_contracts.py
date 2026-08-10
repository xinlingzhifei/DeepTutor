"""Verify committed classroom JSON Schemas against their Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import TypeAlias

from pydantic import BaseModel

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from deeptutor.teaching.contracts import (  # noqa: E402
    ClassroomDocument,
    ExportJob,
    ExportRequest,
    GenerationJob,
    GenerationRequest,
    OutlineBundle,
    TeachingBrief,
)
from deeptutor.teaching.learning_events import LearningEventBatch  # noqa: E402

JsonObject: TypeAlias = dict[str, object]

CONTRACT_SCHEMA_DIRECTORY = REPOSITORY_ROOT / "contracts" / "classroom"
CONTRACT_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "teaching-brief.schema.json": TeachingBrief,
    "generation-request.schema.json": GenerationRequest,
    "outline-bundle.schema.json": OutlineBundle,
    "classroom-document.schema.json": ClassroomDocument,
    "generation-job.schema.json": GenerationJob,
    "export-request.schema.json": ExportRequest,
    "export-job.schema.json": ExportJob,
    "learning-event.schema.json": LearningEventBatch,
}
CONTRACT_SCHEMA_FILENAMES = tuple(CONTRACT_SCHEMA_MODELS)


def generated_contract_schemas() -> dict[str, JsonObject]:
    return {
        filename: model.model_json_schema(mode="validation", by_alias=True)
        for filename, model in CONTRACT_SCHEMA_MODELS.items()
    }


def _normalized_schema(schema: JsonObject) -> str:
    return json.dumps(
        schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def verify_contract_schemas(
    schema_directory: Path = CONTRACT_SCHEMA_DIRECTORY,
) -> list[str]:
    expected_schemas = generated_contract_schemas()
    errors: list[str] = []
    committed_filenames = {path.name for path in schema_directory.glob("*") if path.is_file()}
    unexpected_filenames = committed_filenames - set(CONTRACT_SCHEMA_FILENAMES)
    errors.extend(f"{filename}: unexpected" for filename in sorted(unexpected_filenames))

    for filename in sorted(CONTRACT_SCHEMA_FILENAMES):
        schema_path = schema_directory / filename
        if not schema_path.is_file():
            errors.append(f"{filename}: missing")
            continue

        try:
            committed = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{filename}: invalid JSON ({exc})")
            continue

        if not isinstance(committed, dict):
            errors.append(f"{filename}: schema root must be an object")
            continue

        if _normalized_schema(committed) != _normalized_schema(expected_schemas[filename]):
            errors.append(f"{filename}: schema drift")

    return errors


def main() -> int:
    errors = verify_contract_schemas()
    if errors:
        print("Classroom contract verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "Classroom contracts verified: "
        f"{len(CONTRACT_SCHEMA_FILENAMES)} schema files match Pydantic models."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
