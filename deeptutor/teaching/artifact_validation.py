"""Validate engine output completely before immutable object promotion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from deeptutor.teaching.artifacts import (
    ArtifactManifestEntry,
    ArtifactManifestError,
    classroom_artifact_key,
    tenant_artifact_prefix,
)
from deeptutor.teaching.contracts import (
    ClassroomDocument,
    ExportRequest,
    GenerationRequest,
    canonical_json_bytes,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOWNLOAD_PATH = re.compile(
    r"^/api/yfeistai/v1/artifacts/(?P<job>[A-Za-z0-9._~-]+)/(?P<name>.+)$"
)
_NETWORK_RESOURCE = re.compile(
    r"(?P<url>(?:(?:https?|wss?|file):\s*//|(?<!:)//)[^\s'\"<>]+)",
    re.IGNORECASE,
)
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_EXPORT_ARTIFACT_BINDINGS = {
    "classroom_zip": (".zip", "application/zip"),
    "pptx": (
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "offline_html": (".html", "text/html"),
    "mp4": (".mp4", "video/mp4"),
}


class ArtifactValidationError(ValueError):
    """Stable validation failure safe to persist on a tenant job."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EngineArtifact:
    relative_name: str
    sha256: str
    size: int
    content_type: str
    download_path: str
    expires_at: datetime

    def promotion_entry(self) -> ArtifactManifestEntry:
        return ArtifactManifestEntry(
            relative_name=self.relative_name,
            content_type=self.content_type,
            sha256=self.sha256,
            size=self.size,
        )


@dataclass(frozen=True, slots=True)
class ValidatedClassroomOutput:
    tenant_id: str
    classroom_id: str
    classroom_version_id: str
    document: ClassroomDocument
    document_sha256: str
    media_manifest_sha256: str
    artifacts: tuple[EngineArtifact, ...]

    @property
    def document_artifact(self) -> EngineArtifact:
        return next(item for item in self.artifacts if item.relative_name == "classroom.json")

    def target_keys(self, version: int) -> tuple[str, ...]:
        keys = tuple(
            classroom_artifact_key(
                self.tenant_id,
                self.classroom_id,
                version,
                artifact.relative_name,
            )
            for artifact in self.artifacts
        )
        prefix = tenant_artifact_prefix(self.tenant_id)
        if any(not key.startswith(prefix) for key in keys):
            raise ArtifactValidationError("tenant_prefix_invalid")
        return keys


@dataclass(frozen=True, slots=True)
class ValidatedExportOutput:
    tenant_id: str
    format: str
    input_classroom_document_sha256: str
    input_media_manifest_sha256: str
    artifact: EngineArtifact


def _required_mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArtifactValidationError(code)
    return value


def _field(value: Mapping[str, Any], snake: str, camel: str | None = None) -> Any:
    if snake in value:
        return value[snake]
    return value.get(camel or snake)


def _parse_expiry(value: object) -> datetime:
    if not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value):
        raise ArtifactValidationError("artifact_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ArtifactValidationError("artifact_invalid") from None
    if parsed.tzinfo is None:
        raise ArtifactValidationError("artifact_invalid")
    return parsed


def _parse_artifact(
    value: object,
    *,
    job_id: str,
    now: datetime,
) -> EngineArtifact:
    raw = _required_mapping(value, "artifact_invalid")
    relative_name = _field(raw, "relative_path", "relativePath")
    sha256 = _field(raw, "sha256")
    size = _field(raw, "size_bytes", "bytes")
    content_type = _field(raw, "mime_type", "mime")
    download_path = _field(raw, "temporary_download_path", "downloadPath")
    expires_at = _field(raw, "expires_at", "expiresAt")
    if (
        not isinstance(relative_name, str)
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > _MAX_ARTIFACT_BYTES
        or not isinstance(content_type, str)
        or not isinstance(download_path, str)
    ):
        raise ArtifactValidationError("artifact_invalid")
    if re.fullmatch(r"[^\s/]+/[^\s;/]+(?:\s*;\s*[^\r\n]+)?", content_type) is None:
        raise ArtifactValidationError("artifact_invalid")
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    try:
        entry = ArtifactManifestEntry(
            relative_name=relative_name,
            content_type=normalized_content_type,
            sha256=sha256,
            size=size,
        )
        entry.validate()
    except (ArtifactManifestError, ValueError):
        raise ArtifactValidationError("media_invalid") from None
    parsed_path = urlsplit(download_path)
    match = _DOWNLOAD_PATH.fullmatch(download_path)
    if (
        parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.query
        or parsed_path.fragment
        or match is None
        or match.group("job") != job_id
        or match.group("name") != relative_name
    ):
        raise ArtifactValidationError("tenant_prefix_invalid")
    parsed_expiry = _parse_expiry(expires_at)
    if parsed_expiry <= now:
        raise ArtifactValidationError("artifact_invalid")
    return EngineArtifact(
        relative_name=relative_name,
        sha256=sha256,
        size=size,
        content_type=normalized_content_type,
        download_path=download_path,
        expires_at=parsed_expiry,
    )


def _raw_document_without_hash(raw_document: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(raw_document)
    if "fileSha256" in value:
        value.pop("fileSha256")
    else:
        value.pop("file_sha256", None)
    return value


def _validate_document_hashes(
    raw_document: Mapping[str, Any],
    document: ClassroomDocument,
    declared_document_sha256: object,
) -> str:
    if not isinstance(declared_document_sha256, str):
        raise ArtifactValidationError("hash_invalid")
    expected_file_hash = hashlib.sha256(
        canonical_json_bytes(_raw_document_without_hash(raw_document))
    ).hexdigest()
    if document.file_sha256 != expected_file_hash:
        raise ArtifactValidationError("hash_invalid")
    document_sha256 = hashlib.sha256(canonical_json_bytes(raw_document)).hexdigest()
    if document_sha256 != declared_document_sha256:
        raise ArtifactValidationError("hash_invalid")
    return document_sha256


def _validate_dsl(document: ClassroomDocument) -> None:
    stage_id = document.openmaic.stage.id
    scene_ids: list[str] = []
    interaction_ids: list[str] = []
    for expected_order, scene in enumerate(document.openmaic.scenes):
        if scene.stage_id != stage_id or scene.order != expected_order:
            raise ArtifactValidationError("dsl_invalid")
        if scene.id in scene_ids:
            raise ArtifactValidationError("dsl_invalid")
        scene_ids.append(scene.id)
        if scene.type != "slide":
            interaction_ids.append(scene.id)
    if document.interaction_ids != interaction_ids:
        raise ArtifactValidationError("dsl_invalid")
    for mapping in document.knowledge_point_mappings:
        if any(scene_id not in scene_ids for scene_id in mapping.scene_ids):
            raise ArtifactValidationError("dsl_invalid")


def _source_triple(value: object) -> tuple[str, str, str]:
    raw = value.model_dump() if hasattr(value, "model_dump") else _required_mapping(
        value, "source_invalid"
    )
    return (
        str(_field(raw, "citation_id", "citationId")),
        str(_field(raw, "source_id", "sourceId")),
        str(_field(raw, "fragment_id", "fragmentId")),
    )


def _validate_sources(document: ClassroomDocument, request: GenerationRequest) -> None:
    brief = request.teaching_brief
    allowed_sources = set(brief.permission_summary.allowed_source_ids)
    allowed_fragments = set(brief.permission_summary.allowed_fragment_ids)
    allowed_refs = {_source_triple(item) for item in brief.source_refs}
    document_refs = {_source_triple(item) for item in document.source_refs}
    mapping_refs = {
        _source_triple(item)
        for mapping in document.knowledge_point_mappings
        for item in mapping.source_refs
    }
    if document.content_mode == "source_grounded" and not document_refs:
        raise ArtifactValidationError("source_invalid")
    for citation_id, source_id, fragment_id in document_refs | mapping_refs:
        if (
            not citation_id
            or source_id not in allowed_sources
            or fragment_id not in allowed_fragments
            or (citation_id, source_id, fragment_id) not in allowed_refs
        ):
            raise ArtifactValidationError("source_invalid")


def _validate_policy(document: ClassroomDocument, request: GenerationRequest) -> None:
    policy = request.teaching_brief.network_policy
    if not document.validation_result.valid or any(
        issue.severity == "error" for issue in document.validation_result.issues
    ):
        raise ArtifactValidationError("policy_denied")
    for scene in document.openmaic.scenes:
        if scene.type != "interactive":
            continue
        for match in _NETWORK_RESOURCE.finditer(scene.content.html):
            raw_url = re.sub(r"\s+", "", match.group("url"))
            if not policy.allow_web_access or raw_url.lower().startswith("file:"):
                raise ArtifactValidationError("policy_denied")
            parsed = urlsplit(
                f"https:{raw_url}" if raw_url.startswith("//") else raw_url
            )
            hostname = (parsed.hostname or "").lower()
            if not hostname or not any(
                hostname == domain.lower()
                or (
                    domain.startswith("*.")
                    and hostname.endswith(domain[1:].lower())
                    and hostname != domain[2:].lower()
                )
                for domain in policy.allowed_domains
            ):
                raise ArtifactValidationError("policy_denied")


def _validate_media(
    document: ClassroomDocument,
    request: GenerationRequest,
    artifacts: tuple[EngineArtifact, ...],
    declared_manifest_sha256: object,
    raw_media_manifest: object,
) -> str:
    media_sha256 = hashlib.sha256(canonical_json_bytes(raw_media_manifest)).hexdigest()
    if media_sha256 != declared_manifest_sha256:
        raise ArtifactValidationError("hash_invalid")
    allowed_mimes = set(request.teaching_brief.media_policy.allowed_mime_types)
    if document.media_manifest and not request.teaching_brief.media_policy.allow_generation:
        raise ArtifactValidationError("policy_denied")
    artifacts_by_name = {item.relative_name: item for item in artifacts}
    if len(artifacts_by_name) != len(artifacts):
        raise ArtifactValidationError("media_invalid")
    for media in document.media_manifest:
        artifact = artifacts_by_name.get(media.relative_path)
        if (
            artifact is None
            or media.mime_type not in allowed_mimes
            or artifact.content_type != media.mime_type
            or artifact.sha256 != media.sha256
            or artifact.size != media.size_bytes
        ):
            raise ArtifactValidationError("media_invalid")
    declared_names = {"classroom.json", *(item.relative_path for item in document.media_manifest)}
    if set(artifacts_by_name) != declared_names:
        raise ArtifactValidationError("media_invalid")
    if sum(item.size for item in artifacts) > _MAX_TOTAL_BYTES:
        raise ArtifactValidationError("media_invalid")
    return media_sha256


def validate_generation_result(
    *,
    tenant_id: str,
    job_id: str,
    request_payload: Mapping[str, Any],
    result_payload: Mapping[str, Any],
    now: datetime | None = None,
) -> ValidatedClassroomOutput:
    """Validate the frozen request and every generated output binding."""

    reference_time = now or datetime.now(UTC)
    try:
        request = GenerationRequest.model_validate(request_payload)
    except ValidationError:
        raise ArtifactValidationError("contract_invalid") from None
    if request.tenant_id != tenant_id or request.job_id != job_id:
        raise ArtifactValidationError("contract_invalid")
    raw_document = _required_mapping(
        _field(result_payload, "classroom_document", "classroomDocument"),
        "contract_invalid",
    )
    try:
        document = ClassroomDocument.model_validate(raw_document)
    except ValidationError:
        raise ArtifactValidationError("dsl_invalid") from None
    _validate_dsl(document)
    _validate_sources(document, request)
    document_sha256 = _validate_document_hashes(
        raw_document,
        document,
        _field(result_payload, "classroom_document_sha256", "classroomDocumentSha256"),
    )
    raw_artifacts = _field(result_payload, "artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ArtifactValidationError("artifact_invalid")
    artifacts = tuple(
        _parse_artifact(item, job_id=job_id, now=reference_time) for item in raw_artifacts
    )
    document_artifact = next(
        (item for item in artifacts if item.relative_name == "classroom.json"), None
    )
    if document_artifact is None or document_artifact.sha256 != document_sha256:
        raise ArtifactValidationError("hash_invalid")
    raw_media = _field(raw_document, "media_manifest", "mediaManifest")
    media_sha256 = _validate_media(
        document,
        request,
        artifacts,
        _field(result_payload, "media_manifest_sha256", "mediaManifestSha256"),
        raw_media,
    )
    _validate_policy(document, request)
    classroom_id = _field(result_payload, "classroom_id", "classroomId")
    if not isinstance(classroom_id, str) or classroom_id != document.classroom_id:
        raise ArtifactValidationError("contract_invalid")
    return ValidatedClassroomOutput(
        tenant_id=tenant_id,
        classroom_id=classroom_id,
        classroom_version_id=document.classroom_version_id,
        document=document,
        document_sha256=document_sha256,
        media_manifest_sha256=media_sha256,
        artifacts=artifacts,
    )


def validate_export_result(
    *,
    tenant_id: str,
    job_id: str,
    request_payload: Mapping[str, Any],
    result_payload: Mapping[str, Any],
    now: datetime | None = None,
) -> ValidatedExportOutput:
    reference_time = now or datetime.now(UTC)
    try:
        request = ExportRequest.model_validate(request_payload)
    except ValidationError:
        raise ArtifactValidationError("contract_invalid") from None
    if request.tenant_id != tenant_id or request.job_id != job_id:
        raise ArtifactValidationError("contract_invalid")
    if _field(result_payload, "status") != "succeeded":
        raise ArtifactValidationError("contract_invalid")
    result_format = _field(result_payload, "format")
    if result_format != request.format.value:
        raise ArtifactValidationError("contract_invalid")
    artifact = _parse_artifact(
        _field(result_payload, "artifact"),
        job_id=job_id,
        now=reference_time,
    )
    expected_suffix, expected_content_type = _EXPORT_ARTIFACT_BINDINGS[request.format.value]
    if (
        PurePosixPath(artifact.relative_name).suffix.lower() != expected_suffix
        or artifact.content_type != expected_content_type
    ):
        raise ArtifactValidationError("artifact_invalid")
    return ValidatedExportOutput(
        tenant_id=tenant_id,
        format=request.format.value,
        input_classroom_document_sha256=request.classroom_document_sha256,
        input_media_manifest_sha256=request.media_manifest_sha256,
        artifact=artifact,
    )


__all__ = [
    "ArtifactValidationError",
    "EngineArtifact",
    "ValidatedClassroomOutput",
    "ValidatedExportOutput",
    "validate_export_result",
    "validate_generation_result",
]
