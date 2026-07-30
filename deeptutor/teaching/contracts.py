"""Versioned cross-language contracts for classroom generation and export."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

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


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceSnippet(_ContractModel):
    snippet_id: NonEmptyString
    source_id: NonEmptyString
    text: NonEmptyString
    content_sha256: Sha256


class SourceCitation(_ContractModel):
    citation_id: NonEmptyString
    source_id: NonEmptyString
    snippet_id: NonEmptyString
    label: NonEmptyString


class PermissionSummary(_ContractModel):
    allowed_source_ids: list[NonEmptyString]
    usage_scope: NonEmptyString
    attribution_required: bool


class KnowledgePoint(_ContractModel):
    knowledge_point_id: NonEmptyString
    title: NonEmptyString
    description: NonEmptyString


class TeachingObjective(_ContractModel):
    objective_id: NonEmptyString
    description: NonEmptyString
    knowledge_point_ids: list[NonEmptyString] = Field(min_length=1)


class TeachingBrief(_ContractModel):
    schema_version: Literal["1.0"]
    source_mode: Literal["grounded", "open"]
    source_snippets: list[SourceSnippet]
    citations: list[SourceCitation]
    permission_summary: PermissionSummary
    knowledge_points: list[KnowledgePoint] = Field(min_length=1)
    objectives: list[TeachingObjective] = Field(min_length=1)
    duration_minutes: int = Field(ge=1)
    content_sha256: Sha256


class SourceReference(_ContractModel):
    citation_id: NonEmptyString
    source_id: NonEmptyString
    snippet_id: NonEmptyString


class GenerationMetadata(_ContractModel):
    generator: NonEmptyString
    generator_version: NonEmptyString
    model_id: NonEmptyString
    generated_at: AwareDatetime
    teaching_brief_sha256: Sha256


class OutlineScene(_ContractModel):
    scene_id: NonEmptyString
    title: NonEmptyString
    summary: NonEmptyString
    knowledge_point_ids: list[NonEmptyString] = Field(min_length=1)
    source_refs: list[SourceReference]


class OutlineBundle(_ContractModel):
    schema_version: Literal["1.0"]
    outline_id: NonEmptyString
    title: NonEmptyString
    language: NonEmptyString
    scenes: list[OutlineScene] = Field(min_length=1)
    generation_metadata: GenerationMetadata
    content_sha256: Sha256


class GenerationRequest(_ContractModel):
    schema_version: Literal["1.0"]
    tenant_id: NonEmptyString
    job_id: NonEmptyString
    idempotency_key: NonEmptyString
    phase: GenerationPhase
    teaching_brief: TeachingBrief
    confirmed_outline: OutlineBundle | None = None
    confirmed_outline_sha256: Sha256 | None = None
    data_plane_route_id: NonEmptyString
    priority: GenerationPriority

    @model_validator(mode="after")
    def require_confirmed_outline(self) -> GenerationRequest:
        if self.phase == "content" and (
            self.confirmed_outline is None or self.confirmed_outline_sha256 is None
        ):
            raise ValueError("content phase requires a confirmed outline and hash")
        return self


class ClassroomStage(_ContractModel):
    stage_id: NonEmptyString
    title: NonEmptyString
    scene_ids: list[NonEmptyString] = Field(min_length=1)


class SceneContent(_ContractModel):
    type: Literal["slide", "quiz", "interactive", "pbl"]
    body: NonEmptyString


class ClassroomScene(_ContractModel):
    scene_id: NonEmptyString
    title: NonEmptyString
    content: SceneContent
    source_refs: list[SourceReference] = Field(alias="sourceRefs")
    knowledge_point_ids: list[NonEmptyString] = Field(
        min_length=1,
        alias="knowledgePointIds",
    )


class KnowledgePointMapping(_ContractModel):
    knowledge_point_id: NonEmptyString = Field(alias="knowledgePointId")
    scene_ids: list[NonEmptyString] = Field(
        min_length=1,
        alias="sceneIds",
    )
    source_refs: list[SourceReference] = Field(alias="sourceRefs")


class MediaManifestItem(_ContractModel):
    media_id: NonEmptyString = Field(alias="mediaId")
    relative_path: NonEmptyString = Field(alias="relativePath")
    mime_type: NonEmptyString = Field(alias="mimeType")
    sha256: Sha256
    size_bytes: int = Field(ge=0, alias="sizeBytes")


class ValidationIssue(_ContractModel):
    severity: Literal["error", "warning"]
    code: NonEmptyString
    message: NonEmptyString
    path: NonEmptyString


class ValidationResult(_ContractModel):
    valid: bool
    issues: list[ValidationIssue]
    validated_at: AwareDatetime = Field(alias="validatedAt")


class ClassroomDocument(_ContractModel):
    schema_version: Literal["1.0"]
    classroom_id: NonEmptyString = Field(alias="classroomId")
    dsl_version: NonEmptyString = Field(alias="dslVersion")
    stage: ClassroomStage
    scenes: list[ClassroomScene] = Field(min_length=1)
    source_refs: list[SourceReference] = Field(alias="sourceRefs")
    knowledge_point_mappings: list[KnowledgePointMapping] = Field(
        min_length=1,
        alias="knowledgePointMappings",
    )
    media_manifest: list[MediaManifestItem] = Field(
        min_length=1,
        alias="mediaManifest",
    )
    generation_metadata: GenerationMetadata = Field(alias="generationMetadata")
    validation_result: ValidationResult = Field(alias="validationResult")


class JobError(_ContractModel):
    code: NonEmptyString
    message: NonEmptyString
    retryable: bool


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


class GenerationJob(_ContractModel):
    schema_version: Literal["1.0"]
    tenant_id: NonEmptyString
    job_id: NonEmptyString
    idempotency_key: NonEmptyString
    status: GenerationJobStatus
    phase: GenerationPhase
    attempt: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    input_sha256: Sha256
    output_sha256: Sha256 | None = None
    error: JobError | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None


class ExportFormat(str, Enum):
    CLASSROOM_ZIP = "classroom_zip"
    PPTX = "pptx"
    OFFLINE_HTML = "offline_html"
    MP4 = "mp4"


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


class ExportJob(_ContractModel):
    schema_version: Literal["1.0"]
    tenant_id: NonEmptyString
    job_id: NonEmptyString
    idempotency_key: NonEmptyString
    status: ExportJobStatus
    phase: ExportJobPhase
    format: ExportFormat
    attempt: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    input_classroom_document_sha256: Sha256
    input_media_manifest_sha256: Sha256
    output_sha256: Sha256 | None = None
    error: JobError | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None


__all__ = [
    "ClassroomDocument",
    "ClassroomScene",
    "ClassroomStage",
    "ExportFormat",
    "ExportJob",
    "ExportJobPhase",
    "ExportJobStatus",
    "ExportPolicy",
    "ExportRequest",
    "GenerationJob",
    "GenerationJobStatus",
    "GenerationMetadata",
    "GenerationPhase",
    "GenerationPriority",
    "GenerationRequest",
    "JobError",
    "KnowledgePoint",
    "KnowledgePointMapping",
    "MediaManifestItem",
    "OutlineBundle",
    "OutlineScene",
    "PermissionSummary",
    "SCHEMA_VERSION",
    "SceneContent",
    "Sha256",
    "SourceCitation",
    "SourceReference",
    "SourceSnippet",
    "TeachingBrief",
    "TeachingObjective",
    "ValidationIssue",
    "ValidationResult",
]
