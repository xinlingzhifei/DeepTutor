"""Versioned cross-language contracts for classroom generation and export."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    model_validator,
)
from typing_extensions import TypeAliasType

SCHEMA_VERSION = "1.0"

NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
Sha256 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
GenerationPhase = Literal["outline", "content", "micro"]
GenerationPriority = Literal[
    "student_micro",
    "interaction",
    "teacher",
    "full",
    "batch",
]
ClassroomMode = Literal["micro", "full"]
ContentMode = Literal["source_grounded", "open_creation"]
OpaqueIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^:\s]+$"),
]
JsonValue = TypeAliasType(
    "JsonValue",
    str | int | FiniteFloat | bool | None | list["JsonValue"] | dict[str, "JsonValue"],
)


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def canonical_json_bytes(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        populate_by_name=True,
    )

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("by_alias", True)
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(*args, **kwargs)


class SourceSnapshot(_ContractModel):
    snapshot_id: NonEmptyString
    created_at: AwareDatetime
    content_sha256: Sha256


class SourceFragment(_ContractModel):
    fragment_id: NonEmptyString
    source_id: NonEmptyString
    text: NonEmptyString
    content_sha256: Sha256


SourceSnippet = SourceFragment


class SourceCitation(_ContractModel):
    citation_id: NonEmptyString
    source_id: NonEmptyString
    fragment_id: NonEmptyString
    label: NonEmptyString


class SourceReference(_ContractModel):
    citation_id: NonEmptyString
    source_id: NonEmptyString
    fragment_id: NonEmptyString


class PermissionSummary(_ContractModel):
    allowed_source_ids: list[NonEmptyString]
    allowed_fragment_ids: list[NonEmptyString]
    usage_scope: NonEmptyString
    attribution_required: bool


class KnowledgePoint(_ContractModel):
    knowledge_point_id: NonEmptyString
    title: NonEmptyString
    description: NonEmptyString


class KnowledgePrerequisite(_ContractModel):
    knowledge_point_id: NonEmptyString
    prerequisite_knowledge_point_ids: list[NonEmptyString] = Field(min_length=1)


class TeachingObjective(_ContractModel):
    objective_id: NonEmptyString
    description: NonEmptyString
    knowledge_point_ids: list[NonEmptyString] = Field(min_length=1)


class AssessmentPlan(_ContractModel):
    methods: list[
        Literal[
            "quiz",
            "discussion",
            "project",
            "observation",
            "self_assessment",
        ]
    ] = Field(min_length=1)
    success_criteria: list[NonEmptyString] = Field(min_length=1)


class NetworkPolicy(_ContractModel):
    allow_web_access: bool
    allowed_domains: list[NonEmptyString]


class MediaPolicy(_ContractModel):
    allow_generation: bool
    allowed_mime_types: list[NonEmptyString]


class TemplatePolicy(_ContractModel):
    template_id: NonEmptyString
    template_version: NonEmptyString


class SafetyPolicy(_ContractModel):
    policy_id: NonEmptyString
    blocked_categories: list[NonEmptyString]


def _teaching_brief_schema_extra(schema: dict[str, object]) -> None:
    schema["$comment"] = (
        "Draft 2020-12 enforces non-empty source structures for source_grounded. "
        "SourceFragment fragmentId, SourceCitation citationId, and SourceReference "
        "triple uniqueness plus cross-array lineage require semantic validation "
        "against permissionSummary allowlists."
    )
    schema["allOf"] = [
        {
            "if": {
                "required": ["contentMode"],
                "properties": {"contentMode": {"const": "source_grounded"}},
            },
            "then": {
                "properties": {
                    "sourceSnapshot": {"not": {"type": "null"}},
                    "sourceFragments": {"minItems": 1},
                    "citations": {"minItems": 1},
                    "sourceRefs": {"minItems": 1},
                    "permissionSummary": {
                        "properties": {
                            "allowedSourceIds": {
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "allowedFragmentIds": {
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                        }
                    },
                }
            },
        }
    ]


class TeachingBrief(_ContractModel):
    model_config = ConfigDict(json_schema_extra=_teaching_brief_schema_extra)

    schema_version: Literal["1.0"]
    brief_id: NonEmptyString
    brief_version: int = Field(ge=1)
    tenant_id: NonEmptyString
    course_id: NonEmptyString
    target_class_id: NonEmptyString
    grade_band: NonEmptyString
    audience_level: NonEmptyString
    classroom_mode: ClassroomMode
    objectives: list[TeachingObjective] = Field(min_length=1)
    duration_minutes: int = Field(ge=1)
    knowledge_points: list[KnowledgePoint] = Field(min_length=1)
    prerequisites: list[KnowledgePrerequisite]
    assessment: AssessmentPlan
    source_snapshot: SourceSnapshot | None
    source_fragments: list[SourceFragment]
    citations: list[SourceCitation]
    source_refs: list[SourceReference]
    permission_summary: PermissionSummary
    content_mode: ContentMode
    network_policy: NetworkPolicy
    media_policy: MediaPolicy
    template_policy: TemplatePolicy
    safety_policy: SafetyPolicy
    content_sha256: Sha256

    @model_validator(mode="after")
    def require_grounded_sources(self) -> TeachingBrief:
        if self.content_mode == "source_grounded":
            if (
                self.source_snapshot is None
                or not self.source_fragments
                or not self.citations
                or not self.source_refs
            ):
                raise ValueError(
                    "source-grounded brief requires snapshot, fragments, citations, and source refs"
                )
            allowed_source_ids = set(self.permission_summary.allowed_source_ids)
            allowed_fragment_ids = set(self.permission_summary.allowed_fragment_ids)
            if not allowed_source_ids or not allowed_fragment_ids:
                raise ValueError("source-grounded brief requires allowed source and fragment IDs")
            if len(allowed_source_ids) != len(self.permission_summary.allowed_source_ids) or len(
                allowed_fragment_ids
            ) != len(self.permission_summary.allowed_fragment_ids):
                raise ValueError(
                    "source-grounded brief permission allowlists cannot contain duplicates"
                )
            referenced_source_ids = {
                item.source_id
                for collection in (
                    self.source_fragments,
                    self.citations,
                    self.source_refs,
                )
                for item in collection
            }
            unauthorized_source_ids = referenced_source_ids - allowed_source_ids
            if unauthorized_source_ids:
                raise ValueError(
                    "source-grounded brief contains source IDs outside permission summary"
                )
            fragment_ids = [item.fragment_id for item in self.source_fragments]
            if len(fragment_ids) != len(set(fragment_ids)):
                raise ValueError("source-grounded brief fragment IDs must be unique")
            fragments_by_id = {item.fragment_id: item for item in self.source_fragments}
            if set(fragment_ids) != allowed_fragment_ids:
                raise ValueError(
                    "source-grounded brief fragments must exactly match allowed fragment IDs"
                )
            citation_ids = [item.citation_id for item in self.citations]
            if len(citation_ids) != len(set(citation_ids)):
                raise ValueError("source-grounded brief citation IDs must be unique")
            citations_by_id = {item.citation_id: item for item in self.citations}
            for citation in self.citations:
                fragment = fragments_by_id.get(citation.fragment_id)
                if fragment is None or fragment.source_id != citation.source_id:
                    raise ValueError(
                        "source-grounded brief citation does not match an authorized fragment"
                    )
            source_ref_triples = [
                (item.citation_id, item.source_id, item.fragment_id) for item in self.source_refs
            ]
            if len(source_ref_triples) != len(set(source_ref_triples)):
                raise ValueError("source-grounded brief source ref triples must be unique")
            for source_ref in self.source_refs:
                citation = citations_by_id.get(source_ref.citation_id)
                if citation is None or (
                    citation.source_id,
                    citation.fragment_id,
                ) != (
                    source_ref.source_id,
                    source_ref.fragment_id,
                ):
                    raise ValueError("source-grounded brief source ref does not match a citation")
        return self


class GenerationMetadata(_ContractModel):
    generator: NonEmptyString
    generator_version: NonEmptyString
    model_id: NonEmptyString
    generated_at: AwareDatetime
    teaching_brief_id: NonEmptyString
    teaching_brief_sha256: Sha256
    template_id: NonEmptyString
    template_version: NonEmptyString


def _outline_confirmation_schema_extra(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {
                "required": ["status"],
                "properties": {"status": {"const": "confirmed"}},
            },
            "then": {
                "required": ["confirmedAt", "confirmedBy"],
                "properties": {
                    "confirmedAt": {"type": "string", "format": "date-time"},
                    "confirmedBy": {"type": "string", "minLength": 1},
                },
            },
        }
    ]


class OutlineConfirmationMetadata(_ContractModel):
    model_config = ConfigDict(json_schema_extra=_outline_confirmation_schema_extra)

    status: Literal["draft", "confirmed"]
    confirmed_at: AwareDatetime | None = None
    confirmed_by: NonEmptyString | None = None

    @model_validator(mode="after")
    def require_confirmation_audit(self) -> OutlineConfirmationMetadata:
        if self.status == "confirmed" and (self.confirmed_at is None or self.confirmed_by is None):
            raise ValueError("confirmed outline requires confirmation time and actor")
        return self


class OutlineScene(_ContractModel):
    scene_id: NonEmptyString
    title: NonEmptyString
    summary: NonEmptyString
    knowledge_point_ids: list[NonEmptyString] = Field(min_length=1)
    source_refs: list[SourceReference]


class KnowledgeCoverage(_ContractModel):
    knowledge_point_id: NonEmptyString
    scene_ids: list[NonEmptyString] = Field(min_length=1)


class OutlineBundle(_ContractModel):
    schema_version: Literal["1.0"]
    outline_id: NonEmptyString
    outline_version: int = Field(ge=1)
    confirmation_metadata: OutlineConfirmationMetadata
    title: NonEmptyString
    language: NonEmptyString
    scenes: list[OutlineScene] = Field(min_length=1)
    knowledge_coverage: list[KnowledgeCoverage] = Field(min_length=1)
    source_refs: list[SourceReference]
    estimated_scene_count: int = Field(ge=1)
    generation_metadata: GenerationMetadata
    contract_sha256: Sha256


def _normalize_typed_canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("canonical datetime must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _normalize_typed_canonical_value(value.value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _normalize_typed_canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_typed_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_outline_json_bytes(
    value: OutlineBundle | dict[str, object],
) -> bytes:
    outline = OutlineBundle.model_validate(value)
    typed_value = outline.model_dump(
        mode="python",
        by_alias=True,
        exclude_none=True,
    )
    return canonical_json_bytes(_normalize_typed_canonical_value(typed_value))


def canonical_outline_sha256(
    value: OutlineBundle | dict[str, object],
) -> str:
    return hashlib.sha256(canonical_outline_json_bytes(value)).hexdigest()


class ExportFormat(str, Enum):
    CLASSROOM_ZIP = "classroom_zip"
    PPTX = "pptx"
    OFFLINE_HTML = "offline_html"
    MP4 = "mp4"


def _generation_request_schema_extra(schema: dict[str, object]) -> None:
    confirmed_fields = ["confirmedOutline", "confirmedOutlineSha256"]
    non_null_pair = {
        "required": confirmed_fields,
        "properties": {
            "confirmedOutline": {"not": {"type": "null"}},
            "confirmedOutlineSha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
        },
    }
    schema["$comment"] = (
        "confirmedOutlineSha256 is the SHA-256 of the canonical UTF-8 JSON "
        "serialization of the entire confirmed OutlineBundle: camelCase keys, "
        "omitted nulls, schema-declared RFC 3339 timestamps normalized to UTC "
        "with a Z suffix, sorted keys, compact separators, and UTF-8 bytes."
    )
    schema["allOf"] = [
        {
            "oneOf": [
                {
                    "not": {
                        "anyOf": [
                            {"required": ["confirmedOutline"]},
                            {"required": ["confirmedOutlineSha256"]},
                        ]
                    }
                },
                {
                    "required": confirmed_fields,
                    "properties": {
                        "confirmedOutline": {"type": "null"},
                        "confirmedOutlineSha256": {"type": "null"},
                    },
                },
                non_null_pair,
            ]
        },
        {
            "if": {
                "required": ["phase"],
                "properties": {"phase": {"const": "content"}},
            },
            "then": non_null_pair,
        },
        {
            "if": {
                "required": ["phase"],
                "properties": {"phase": {"const": "content"}},
            },
            "then": {
                "properties": {
                    "confirmedOutline": {
                        "properties": {
                            "confirmationMetadata": {
                                "required": ["status"],
                                "properties": {
                                    "status": {"const": "confirmed"},
                                },
                            }
                        }
                    }
                }
            },
        },
        {
            "oneOf": [
                {
                    "required": ["phase", "classroomMode"],
                    "properties": {
                        "phase": {"const": "micro"},
                        "classroomMode": {"const": "micro"},
                    },
                },
                {
                    "required": ["phase", "classroomMode"],
                    "properties": {
                        "phase": {"enum": ["outline", "content"]},
                        "classroomMode": {"const": "full"},
                    },
                },
            ]
        },
        {
            "if": {
                "required": ["classroomMode"],
                "properties": {"classroomMode": {"const": "micro"}},
            },
            "then": {
                "properties": {
                    "teachingBrief": {
                        "properties": {
                            "classroomMode": {"const": "micro"},
                        }
                    }
                }
            },
        },
        {
            "if": {
                "required": ["classroomMode"],
                "properties": {"classroomMode": {"const": "full"}},
            },
            "then": {
                "properties": {
                    "teachingBrief": {
                        "properties": {
                            "classroomMode": {"const": "full"},
                        }
                    }
                }
            },
        },
    ]


class GenerationRequest(_ContractModel):
    model_config = ConfigDict(
        json_schema_extra=_generation_request_schema_extra,
    )

    schema_version: Literal["1.0"]
    tenant_id: NonEmptyString
    request_id: NonEmptyString
    job_id: NonEmptyString
    idempotency_key: NonEmptyString
    phase: GenerationPhase
    classroom_mode: ClassroomMode
    teaching_brief_id: NonEmptyString
    teaching_brief_sha256: Sha256
    teaching_brief: TeachingBrief
    confirmed_outline: OutlineBundle | None = None
    confirmed_outline_sha256: Sha256 | None = Field(
        default=None,
        description=(
            "SHA-256 of the canonical UTF-8 JSON serialization of the entire "
            "confirmed OutlineBundle."
        ),
    )
    template_id: NonEmptyString
    template_version: NonEmptyString
    scene_budget: int = Field(ge=1)
    duration_minutes: int = Field(ge=1)
    requested_exports: list[ExportFormat] = Field(min_length=1)
    callback_context: OpaqueIdentifier
    data_plane_route_id: OpaqueIdentifier = Field(
        description=(
            "opaque control-plane routing key resolved by server configuration; "
            "not a provider identifier, secret, URL, or client-selectable model route."
        )
    )
    priority: GenerationPriority

    @model_validator(mode="after")
    def validate_request_links(self) -> GenerationRequest:
        expected_classroom_mode = "micro" if self.phase == "micro" else "full"
        if self.classroom_mode != expected_classroom_mode:
            raise ValueError("generation phase and classroom mode must agree")
        if self.classroom_mode != self.teaching_brief.classroom_mode:
            raise ValueError(
                "generation request classroom mode must match embedded teaching brief classroom mode"
            )
        has_outline = self.confirmed_outline is not None
        has_hash = self.confirmed_outline_sha256 is not None
        if self.phase == "content" and not (has_outline and has_hash):
            raise ValueError("content phase requires a confirmed outline and hash")
        if (
            self.phase == "content"
            and self.confirmed_outline is not None
            and self.confirmed_outline.confirmation_metadata.status != "confirmed"
        ):
            raise ValueError("content phase requires confirmed outline status")
        if has_outline != has_hash:
            raise ValueError("confirmed outline and hash must be provided together")
        if self.teaching_brief_id != self.teaching_brief.brief_id:
            raise ValueError("teaching brief identity does not match embedded brief")
        if self.teaching_brief_sha256 != self.teaching_brief.content_sha256:
            raise ValueError("teaching brief hash does not match embedded brief")
        if (
            self.confirmed_outline is not None
            and self.confirmed_outline_sha256 != canonical_outline_sha256(self.confirmed_outline)
        ):
            raise ValueError("confirmed outline hash does not match canonical JSON")
        return self


class OpenMaicStage(_ContractModel):
    id: NonEmptyString
    name: NonEmptyString
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SlideSceneContent(_ContractModel):
    type: Literal["slide"]
    canvas: dict[str, JsonValue]


class QuizOption(_ContractModel):
    id: NonEmptyString
    label: NonEmptyString


class QuizQuestion(_ContractModel):
    id: NonEmptyString
    prompt: NonEmptyString
    question_type: Literal[
        "single_choice",
        "multiple_choice",
        "short_answer",
    ]
    options: list[QuizOption]
    correct_option_ids: list[NonEmptyString]
    explanation: NonEmptyString


class QuizSceneContent(_ContractModel):
    type: Literal["quiz"]
    questions: list[QuizQuestion] = Field(min_length=1)


class InteractiveSandbox(_ContractModel):
    allow_scripts: Literal[True]
    allow_same_origin: Literal[False]


class InteractiveSceneContent(_ContractModel):
    type: Literal["interactive"]
    html: NonEmptyString
    bridge_version: Literal["1.0"]
    sandbox: InteractiveSandbox


class PblRole(_ContractModel):
    id: NonEmptyString
    name: NonEmptyString
    brief: NonEmptyString


class PblMilestone(_ContractModel):
    id: NonEmptyString
    title: NonEmptyString
    rubric: NonEmptyString


class PblSceneContent(_ContractModel):
    type: Literal["pbl"]
    scenario: NonEmptyString
    roles: list[PblRole] = Field(min_length=1)
    milestones: list[PblMilestone] = Field(min_length=1)


class _OpenMaicSceneBase(_ContractModel):
    id: NonEmptyString
    stage_id: NonEmptyString
    title: NonEmptyString
    order: int = Field(ge=0)
    actions: list[dict[str, JsonValue]] = Field(default_factory=list)


class SlideScene(_OpenMaicSceneBase):
    type: Literal["slide"]
    content: SlideSceneContent


class QuizScene(_OpenMaicSceneBase):
    type: Literal["quiz"]
    content: QuizSceneContent


class InteractiveScene(_OpenMaicSceneBase):
    type: Literal["interactive"]
    content: InteractiveSceneContent


class PblScene(_OpenMaicSceneBase):
    type: Literal["pbl"]
    content: PblSceneContent


ClassroomScene = Annotated[
    SlideScene | QuizScene | InteractiveScene | PblScene,
    Field(discriminator="type"),
]
SceneContent = SlideSceneContent | QuizSceneContent | InteractiveSceneContent | PblSceneContent
ClassroomStage = OpenMaicStage


class OpenMaicDocument(_ContractModel):
    dsl_version: Literal["0.1.0"]
    stage: OpenMaicStage
    scenes: list[ClassroomScene] = Field(min_length=1)


class KnowledgePointMapping(_ContractModel):
    knowledge_point_id: NonEmptyString
    scene_ids: list[NonEmptyString] = Field(min_length=1)
    source_refs: list[SourceReference]


class MediaManifestItem(_ContractModel):
    media_id: NonEmptyString
    relative_path: NonEmptyString
    mime_type: NonEmptyString
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    temporary_download_path: NonEmptyString
    expires_at: AwareDatetime


class ExportManifestItem(_ContractModel):
    format: ExportFormat
    relative_path: NonEmptyString
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    mime_type: NonEmptyString
    temporary_download_path: NonEmptyString
    expires_at: AwareDatetime


class ClassroomAuditMetadata(_ContractModel):
    template_id: NonEmptyString
    template_version: NonEmptyString
    teaching_brief_id: NonEmptyString
    teaching_brief_sha256: Sha256
    parent_classroom_version_id: NonEmptyString | None = None


class ValidationIssue(_ContractModel):
    severity: Literal["error", "warning"]
    code: NonEmptyString
    message: NonEmptyString
    path: NonEmptyString


class ValidationResult(_ContractModel):
    valid: bool
    issues: list[ValidationIssue]
    validated_at: AwareDatetime


class MigrationRecord(_ContractModel):
    from_dsl_version: NonEmptyString
    to_dsl_version: NonEmptyString
    migrated_at: AwareDatetime
    migration_id: NonEmptyString


def _classroom_document_schema_extra(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {
                "required": ["contentMode"],
                "properties": {"contentMode": {"const": "source_grounded"}},
            },
            "then": {
                "properties": {
                    "openCreation": {"const": False},
                    "sourceRefs": {"minItems": 1},
                }
            },
        },
        {
            "if": {
                "required": ["contentMode"],
                "properties": {"contentMode": {"const": "open_creation"}},
            },
            "then": {"properties": {"openCreation": {"const": True}}},
        },
    ]


class ClassroomDocument(_ContractModel):
    model_config = ConfigDict(json_schema_extra=_classroom_document_schema_extra)

    schema_version: Literal["1.0"]
    classroom_id: NonEmptyString
    classroom_version_id: NonEmptyString
    content_mode: ContentMode
    open_creation: bool
    openmaic: OpenMaicDocument
    interaction_ids: list[NonEmptyString]
    source_refs: list[SourceReference]
    knowledge_point_mappings: list[KnowledgePointMapping] = Field(min_length=1)
    media_manifest: list[MediaManifestItem]
    file_sha256: Sha256
    export_manifest: list[ExportManifestItem]
    generation_metadata: GenerationMetadata
    audit_metadata: ClassroomAuditMetadata
    validation_result: ValidationResult
    migration_records: list[MigrationRecord]

    @model_validator(mode="after")
    def validate_content_mode(self) -> ClassroomDocument:
        expected_open_creation = self.content_mode == "open_creation"
        if self.open_creation != expected_open_creation:
            raise ValueError("open creation flag must agree with classroom content mode")
        if self.content_mode == "source_grounded" and not self.source_refs:
            raise ValueError("source-grounded classroom requires at least one source ref")
        return self


class JobError(_ContractModel):
    category: Literal[
        "validation",
        "quota",
        "provider",
        "timeout",
        "canceled",
        "internal",
        "artifact",
    ]
    code: NonEmptyString
    message: NonEmptyString
    retryable: bool
    diagnostic_summary: NonEmptyString


class QuotaReservation(_ContractModel):
    reservation_id: NonEmptyString
    reserved_units: int = Field(ge=0)
    actual_units: int = Field(ge=0)
    unit: Literal["credits", "tokens", "seconds"]


class CostSummary(_ContractModel):
    currency: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Z]{3}$"),
    ]
    estimated_amount: FiniteFloat = Field(ge=0)
    actual_amount: FiniteFloat = Field(ge=0)


def _artifact_state_schema_extra(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {
                "required": ["status"],
                "properties": {"status": {"const": "ready"}},
            },
            "then": {
                "required": ["sha256"],
                "properties": {
                    "sha256": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    }
                },
            },
        }
    ]


class ArtifactState(_ContractModel):
    model_config = ConfigDict(json_schema_extra=_artifact_state_schema_extra)

    artifact_id: NonEmptyString
    status: Literal["pending", "materializing", "ready", "failed", "discarded"]
    sha256: Sha256 | None = None

    @model_validator(mode="after")
    def require_ready_hash(self) -> ArtifactState:
        if self.status == "ready" and self.sha256 is None:
            raise ValueError("ready artifact requires sha256")
        return self


ArtifactManifestKind = Literal["dsl_json", "media", "export"]


class ArtifactManifestItem(_ContractModel):
    kind: ArtifactManifestKind
    relative_path: NonEmptyString
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    mime_type: NonEmptyString
    temporary_download_path: NonEmptyString
    expires_at: AwareDatetime


_LEASE_FIELDS = ["leaseId", "leaseOwner", "leaseExpiresAt", "heartbeatAt"]


def _job_schema_extra(
    schema: dict[str, object],
    *,
    running_statuses: list[str],
    required_succeeded_manifest_kinds: list[str],
) -> None:
    lease_all_non_null = {
        "required": _LEASE_FIELDS,
        "properties": {
            "leaseId": {"type": "string", "minLength": 1},
            "leaseOwner": {"type": "string", "minLength": 1},
            "leaseExpiresAt": {"type": "string", "format": "date-time"},
            "heartbeatAt": {"type": "string", "format": "date-time"},
        },
    }
    lease_all_null = {
        "required": _LEASE_FIELDS,
        "properties": {field_name: {"type": "null"} for field_name in _LEASE_FIELDS},
    }
    lease_all_absent = {
        "not": {"anyOf": [{"required": [field_name]} for field_name in _LEASE_FIELDS]}
    }
    nonready_temporary_artifact = {
        "anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "required": ["status"],
                "properties": {
                    "status": {
                        "enum": [
                            "pending",
                            "materializing",
                            "failed",
                            "discarded",
                        ]
                    }
                },
            },
        ]
    }
    schema["$comment"] = (
        "Draft 2020-12 enforces terminal, artifact, and lease structure. "
        "For succeeded jobs, finalArtifact.sha256 and the primary artifactManifest "
        "item sha256 equality with outputSha256 require semantic validation."
    )
    succeeded_manifest = {
        "allOf": [
            {
                "contains": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {"kind": {"const": kind}},
                },
                "minContains": 1,
            }
            for kind in required_succeeded_manifest_kinds
        ]
    }
    schema["allOf"] = [
        {
            "oneOf": [
                lease_all_absent,
                lease_all_null,
                lease_all_non_null,
            ]
        },
        {
            "if": {
                "required": ["status"],
                "properties": {"status": {"enum": running_statuses}},
            },
            "then": lease_all_non_null,
        },
        {
            "if": {
                "required": ["status"],
                "properties": {"status": {"const": "succeeded"}},
            },
            "then": {
                "required": [
                    "outputSha256",
                    "finalArtifact",
                    "artifactManifest",
                    "completedAt",
                ],
                "properties": {
                    "outputSha256": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                    "finalArtifact": {
                        "type": "object",
                        "required": ["status"],
                        "properties": {"status": {"const": "ready"}},
                    },
                    "completedAt": {"type": "string", "format": "date-time"},
                    "error": {"type": "null"},
                    "artifactManifest": succeeded_manifest,
                },
            },
        },
        {
            "if": {
                "required": ["status"],
                "properties": {"status": {"const": "failed"}},
            },
            "then": {
                "required": ["error", "completedAt"],
                "properties": {
                    "error": {"type": "object"},
                    "completedAt": {"type": "string", "format": "date-time"},
                    "outputSha256": {"type": "null"},
                    "finalArtifact": {"type": "null"},
                    "temporaryArtifact": nonready_temporary_artifact,
                },
            },
        },
        {
            "if": {
                "required": ["status"],
                "properties": {"status": {"const": "canceled"}},
            },
            "then": {
                "required": ["canceledAt", "completedAt"],
                "properties": {
                    "canceledAt": {"type": "string", "format": "date-time"},
                    "completedAt": {"type": "string", "format": "date-time"},
                    "outputSha256": {"type": "null"},
                    "finalArtifact": {"type": "null"},
                    "temporaryArtifact": nonready_temporary_artifact,
                },
            },
        },
    ]


def _generation_job_schema_extra(schema: dict[str, object]) -> None:
    _job_schema_extra(
        schema,
        running_statuses=[
            "generating_outline",
            "generating_content",
            "validating",
            "materializing",
        ],
        required_succeeded_manifest_kinds=["dsl_json", "media"],
    )


def _export_job_schema_extra(schema: dict[str, object]) -> None:
    _job_schema_extra(
        schema,
        running_statuses=[
            "exporting",
            "validating",
            "materializing",
        ],
        required_succeeded_manifest_kinds=["export"],
    )


class _JobContract(_ContractModel):
    schema_version: Literal["1.0"]
    tenant_id: NonEmptyString
    job_id: NonEmptyString
    request_id: NonEmptyString
    classroom_draft_id: NonEmptyString
    batch_id: NonEmptyString | None = None
    idempotency_key: NonEmptyString
    attempt: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    work_pool: NonEmptyString
    quota_reservation: QuotaReservation
    cost_summary: CostSummary
    heartbeat_at: AwareDatetime | None = None
    lease_id: NonEmptyString | None = None
    lease_owner: NonEmptyString | None = None
    lease_expires_at: AwareDatetime | None = None
    temporary_artifact: ArtifactState | None = None
    final_artifact: ArtifactState | None = None
    artifact_manifest: list[ArtifactManifestItem]
    output_sha256: Sha256 | None = None
    error: JobError | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    canceled_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None

    def _validate_job_status(
        self,
        status: str,
        *,
        running_statuses: set[str],
        required_succeeded_manifest_kinds: set[str],
        primary_manifest_kind: str,
    ) -> None:
        lease_values = (
            self.lease_id,
            self.lease_owner,
            self.lease_expires_at,
            self.heartbeat_at,
        )
        lease_count = sum(value is not None for value in lease_values)
        if lease_count not in {0, len(lease_values)}:
            raise ValueError("lease fields must be all present or all absent")
        if status in running_statuses and lease_count == 0:
            raise ValueError("running job requires a complete lease")
        if status in {"succeeded", "failed", "canceled"} and (self.completed_at is None):
            raise ValueError("terminal job requires completed_at")
        if status == "succeeded":
            if self.output_sha256 is None:
                raise ValueError("succeeded job requires output_sha256")
            if self.final_artifact is None or self.final_artifact.status != "ready":
                raise ValueError("succeeded job requires a ready final artifact")
            if self.final_artifact.sha256 != self.output_sha256:
                raise ValueError("final artifact sha256 must match output_sha256")
            manifest_kinds = {item.kind for item in self.artifact_manifest}
            missing_manifest_kinds = required_succeeded_manifest_kinds - manifest_kinds
            if missing_manifest_kinds:
                raise ValueError("succeeded job artifact manifest is missing required file kinds")
            if not any(
                item.kind == primary_manifest_kind and item.sha256 == self.output_sha256
                for item in self.artifact_manifest
            ):
                raise ValueError("primary artifact manifest sha256 must match output_sha256")
            if self.error is not None:
                raise ValueError("succeeded job cannot contain an error")
        elif status in {"failed", "canceled"}:
            if self.output_sha256 is not None or self.final_artifact is not None:
                raise ValueError("failed or canceled job cannot contain output or final artifact")
            if self.temporary_artifact is not None and self.temporary_artifact.status == "ready":
                raise ValueError(
                    "failed or canceled job may retain only a non-ready temporary artifact"
                )
            if status == "failed" and self.error is None:
                raise ValueError("failed job requires an error")
            if status == "canceled" and self.canceled_at is None:
                raise ValueError("canceled job requires canceled_at")


class GenerationJobStatus(str, Enum):
    CREATED = "created"
    QUOTA_RESERVED = "quota_reserved"
    QUEUED = "queued"
    GENERATING_OUTLINE = "generating_outline"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    GENERATING_CONTENT = "generating_content"
    VALIDATING = "validating"
    MATERIALIZING = "materializing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class GenerationJob(_JobContract):
    model_config = ConfigDict(json_schema_extra=_generation_job_schema_extra)

    status: GenerationJobStatus
    phase: GenerationPhase
    model_id: NonEmptyString
    input_sha256: Sha256

    @model_validator(mode="after")
    def validate_terminal_status(self) -> GenerationJob:
        self._validate_job_status(
            self.status.value,
            running_statuses={
                "generating_outline",
                "generating_content",
                "validating",
                "materializing",
            },
            required_succeeded_manifest_kinds={"dsl_json", "media"},
            primary_manifest_kind="dsl_json",
        )
        return self


class ExportPolicy(_ContractModel):
    include_source_attribution: bool
    allow_external_links: bool


class ExportRequest(_ContractModel):
    schema_version: Literal["1.0"]
    tenant_id: NonEmptyString
    job_id: NonEmptyString
    idempotency_key: NonEmptyString
    classroom_document_sha256: Sha256
    media_manifest_sha256: Sha256
    format: ExportFormat
    language: NonEmptyString
    export_policy: ExportPolicy


class ExportJobStatus(str, Enum):
    CREATED = "created"
    QUOTA_RESERVED = "quota_reserved"
    QUEUED = "queued"
    EXPORTING = "exporting"
    VALIDATING = "validating"
    MATERIALIZING = "materializing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ExportJobPhase(str, Enum):
    QUEUED = "queued"
    EXPORTING = "exporting"
    VALIDATING = "validating"
    MATERIALIZING = "materializing"
    COMPLETED = "completed"


class ExportJob(_JobContract):
    model_config = ConfigDict(json_schema_extra=_export_job_schema_extra)

    status: ExportJobStatus
    phase: ExportJobPhase
    format: ExportFormat
    renderer_id: NonEmptyString
    input_classroom_document_sha256: Sha256
    input_media_manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_terminal_status(self) -> ExportJob:
        self._validate_job_status(
            self.status.value,
            running_statuses={
                "exporting",
                "validating",
                "materializing",
            },
            required_succeeded_manifest_kinds={"export"},
            primary_manifest_kind="export",
        )
        return self


__all__ = [
    "ArtifactManifestItem",
    "ArtifactManifestKind",
    "ArtifactState",
    "AssessmentPlan",
    "ClassroomAuditMetadata",
    "ClassroomDocument",
    "ClassroomMode",
    "ClassroomScene",
    "ClassroomStage",
    "ContentMode",
    "CostSummary",
    "ExportFormat",
    "ExportJob",
    "ExportJobPhase",
    "ExportJobStatus",
    "ExportManifestItem",
    "ExportPolicy",
    "ExportRequest",
    "GenerationJob",
    "GenerationJobStatus",
    "GenerationMetadata",
    "GenerationPhase",
    "GenerationPriority",
    "GenerationRequest",
    "InteractiveScene",
    "InteractiveSceneContent",
    "InteractiveSandbox",
    "JobError",
    "JsonValue",
    "KnowledgeCoverage",
    "KnowledgePoint",
    "KnowledgePointMapping",
    "KnowledgePrerequisite",
    "MediaManifestItem",
    "MediaPolicy",
    "MigrationRecord",
    "NetworkPolicy",
    "OpenMaicDocument",
    "OpenMaicStage",
    "OutlineBundle",
    "OutlineConfirmationMetadata",
    "OutlineScene",
    "PblScene",
    "PblSceneContent",
    "PblMilestone",
    "PblRole",
    "PermissionSummary",
    "QuizOption",
    "QuizQuestion",
    "QuizScene",
    "QuizSceneContent",
    "QuotaReservation",
    "SCHEMA_VERSION",
    "SafetyPolicy",
    "SceneContent",
    "Sha256",
    "SlideScene",
    "SlideSceneContent",
    "SourceCitation",
    "SourceFragment",
    "SourceReference",
    "SourceSnapshot",
    "SourceSnippet",
    "TeachingBrief",
    "TeachingObjective",
    "TemplatePolicy",
    "ValidationIssue",
    "ValidationResult",
    "canonical_json_bytes",
    "canonical_outline_json_bytes",
    "canonical_outline_sha256",
    "canonical_sha256",
]
