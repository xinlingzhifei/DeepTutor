"""Teacher classroom authoring workflow and pre-publication validation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
from html import unescape
from html.parser import HTMLParser
import json
import re
import secrets
import tempfile
from typing import Any, AsyncIterator, Protocol
from urllib.parse import quote, unquote, urlsplit
import zipfile

from deeptutor.teaching.artifacts import (
    ArtifactManifestEntry,
    ArtifactManifestError,
    StoredArtifact,
    temporary_artifact_key,
)
from deeptutor.teaching.brief_builder import (
    KnowledgePointSpec,
    TeachingBriefBuilder,
    TeachingBriefSpec,
)
from deeptutor.teaching.contracts import (
    ClassroomDocument,
    GenerationRequest,
    OutlineBundle,
    OutlineConfirmationMetadata,
    TeachingBrief,
    canonical_json_bytes,
    canonical_outline_sha256,
    validate_outline_binding,
)
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelector
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.repositories.jobs import (
    GenerationJobDetails,
    GenerationJobRequest,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.tenant_context import TenantContext


class ClassroomServiceError(RuntimeError):
    """Base class for stable classroom workflow failures."""


class InvalidDraftDocument(ClassroomServiceError, ValueError):
    """An editor document contains an untrusted external reference."""


class ClassroomAccessDenied(ClassroomServiceError, PermissionError):
    """The caller lacks the required resource-scoped classroom permission."""


class ClassroomNotFound(ClassroomServiceError, LookupError):
    """The classroom is unavailable in the active tenant and resource scope."""


class ClassroomRevisionConflict(ClassroomServiceError):
    """The mutable classroom draft revision changed before this update."""


class ClassroomConfirmationConflict(ClassroomServiceError):
    """A repeated outline confirmation does not match its durable binding."""


class ClassroomIdempotencyConflict(ClassroomServiceError):
    """A classroom creation key is already bound to a different request."""


class InvalidClassroomState(ClassroomServiceError):
    """The requested authoring operation is invalid in the current lifecycle state."""


class ClassroomPreflightRejected(InvalidClassroomState):
    """Creation input was rejected before a workflow or generation job existed."""


class InvalidDraftMedia(ClassroomServiceError, ValueError):
    """An uploaded draft media file failed the bounded integrity checks."""


@dataclass(frozen=True, slots=True)
class ClassroomRecord:
    tenant_id: str
    asset_id: str
    draft_id: str
    job_id: str | None
    lifecycle_state: str
    status: str
    title: str
    course_id: str
    class_id: str
    owner_id: str
    teaching_brief: TeachingBrief | None
    revision: int
    outline: dict[str, Any] | None
    document: dict[str, Any]
    classroom_version_id: str | None
    confirmed_outline_sha256: str | None
    validation_report: dict[str, Any] | None
    validation_revision: int | None = None
    validation_document_sha256: str | None = None
    creation_idempotency_key: str | None = None
    creation_request_sha256: str | None = None
    student_generation_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class TeacherDraftCopyRecord:
    asset_id: str
    draft_id: str
    source_student_asset_id: str
    owner_id: str
    status: str
    revision: int


@dataclass(frozen=True, slots=True)
class NewClassroomWorkflow:
    tenant_id: str
    asset_id: str
    draft_id: str
    owner_id: str
    title: str
    teaching_brief: TeachingBrief
    creation_idempotency_key: str
    creation_request_sha256: str
    initial_lifecycle_state: str = "generating_outline"
    student_generation_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationStage:
    job_id: str
    status: str
    outline: OutlineBundle | None
    classroom_version_id: str | None


@dataclass(frozen=True, slots=True)
class NewDraftMedia:
    id: str
    classroom_id: str
    uploaded_by: str
    object_key: str = field(repr=False)
    mime_type: str
    sha256: str
    size_bytes: int
    ownership_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DraftMediaRecord:
    id: str
    classroom_id: str
    mime_type: str
    sha256: str
    size_bytes: int
    object_key: str = field(repr=False)
    ownership_token: str = field(repr=False)
    object_revision: str | None = field(repr=False)
    status: str = "writing"
    last_error_code: str | None = None

    @property
    def relative_path(self) -> str:
        return draft_media_relative_path(self.id, self.mime_type)


@dataclass(frozen=True, slots=True)
class DraftMediaContent:
    id: str
    mime_type: str
    sha256: str
    size_bytes: int
    body: AsyncIterator[bytes] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ClassroomMediaBinding:
    media_id: str
    relative_name: str
    mime_type: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class BoundClassroomMedia:
    id: str
    classroom_id: str
    relative_path: str
    mime_type: str
    sha256: str
    size_bytes: int
    object_key: str = field(repr=False)


class ClassroomRepository(Protocol):
    async def get_creation(self, idempotency_key: str) -> ClassroomRecord | None: ...

    async def create_workflow(self, workflow: NewClassroomWorkflow) -> ClassroomRecord: ...

    async def list_workflows(self) -> tuple[ClassroomRecord, ...]: ...

    async def get_workflow(self, asset_id: str) -> ClassroomRecord | None: ...

    async def get_student_workflow(
        self,
        request_id: str,
    ) -> ClassroomRecord | None: ...

    async def attach_generation_job(
        self,
        asset_id: str,
        job_id: str,
        phase: str,
    ) -> ClassroomRecord: ...

    async def start_student_generation(
        self,
        asset_id: str,
        mode: str,
    ) -> ClassroomRecord: ...

    async def mark_canceled(self, asset_id: str) -> ClassroomRecord: ...

    async def copy_student_to_teacher_draft(
        self,
        source_asset_id: str,
        target_asset_id: str,
        target_draft_id: str,
        copy_id: str,
        copied_by: str,
    ) -> TeacherDraftCopyRecord: ...

    async def attach_outline_job(self, asset_id: str, job_id: str) -> ClassroomRecord: ...

    async def save_outline(
        self,
        asset_id: str,
        outline: dict[str, Any],
        outline_sha256: str,
    ) -> ClassroomRecord: ...

    async def update_outline(
        self,
        asset_id: str,
        outline: dict[str, Any],
        outline_sha256: str,
        expected_revision: int,
    ) -> ClassroomRecord | None: ...

    async def confirm_outline(
        self,
        asset_id: str,
        outline: dict[str, Any],
        confirmed_outline_sha256: str,
        source_outline_sha256: str,
        *,
        expected_revision: int | None = None,
        expected_outline_sha256: str | None = None,
    ) -> ClassroomRecord: ...

    async def mark_generation_succeeded(
        self,
        asset_id: str,
        job_id: str,
    ) -> ClassroomRecord: ...

    async def update_document(
        self,
        asset_id: str,
        document: dict[str, Any],
        document_sha256: str,
        expected_revision: int,
    ) -> ClassroomRecord | None: ...

    async def available_media_bindings(
        self,
        asset_id: str,
    ) -> tuple[ClassroomMediaBinding, ...]: ...

    async def save_validation_report(
        self,
        asset_id: str,
        report: dict[str, object],
        report_sha256: str,
        expected_revision: int,
        expected_document_sha256: str,
    ) -> ClassroomRecord | None: ...

    async def reserve_media(self, media: NewDraftMedia) -> DraftMediaRecord: ...

    async def complete_media(
        self,
        asset_id: str,
        media_id: str,
        object_revision: str,
    ) -> DraftMediaRecord: ...

    async def fail_media(
        self,
        asset_id: str,
        media_id: str,
        error_code: str,
    ) -> None: ...

    async def mark_media_cleanup_pending(
        self,
        asset_id: str,
        media_id: str,
        error_code: str,
    ) -> DraftMediaRecord: ...

    async def finish_media_cleanup(
        self,
        asset_id: str,
        media_id: str,
        error_code: str,
    ) -> None: ...

    async def get_media_receipt(
        self,
        asset_id: str,
        media_id: str,
    ) -> DraftMediaRecord | None: ...

    async def list_cleanup_pending(
        self,
        asset_id: str,
        *,
        limit: int = 8,
    ) -> tuple[DraftMediaRecord, ...]: ...

    async def get_media(
        self,
        asset_id: str,
        media_id: str,
    ) -> DraftMediaRecord | None: ...

    async def get_bound_version_media(
        self,
        asset_id: str,
        media_id: str,
    ) -> BoundClassroomMedia | None: ...


class ClassroomGeneration(Protocol):
    async def start_outline(
        self,
        *,
        context: TenantContext,
        asset_id: str,
        draft_id: str,
        teaching_brief: TeachingBrief,
        requested_exports: tuple[str, ...],
    ) -> GenerationStage: ...

    async def get_stage(
        self,
        *,
        context: TenantContext,
        job_id: str,
    ) -> GenerationStage: ...

    async def start_content(
        self,
        *,
        context: TenantContext,
        asset_id: str,
        draft_id: str,
        job_id: str,
        confirmed_outline: OutlineBundle,
        confirmed_outline_sha256: str,
    ) -> GenerationStage: ...


class DraftMediaUpload(Protocol):
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> None: ...


class DraftMediaStore(Protocol):
    async def put_verified(
        self,
        key: str,
        body: AsyncIterator[bytes],
        sha256: str,
        size: int,
        *,
        content_type: str,
        ownership_token: str,
    ) -> StoredArtifact: ...

    async def reconcile_verified(
        self,
        key: str,
        sha256: str,
        size: int,
        *,
        content_type: str,
        ownership_token: str,
    ) -> StoredArtifact | None: ...

    async def open(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete_owned(self, artifact: StoredArtifact) -> None: ...


class DraftMediaStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> DraftMediaStore: ...


_MEDIA_ID_PATTERN = re.compile(r"^media-[0-9a-f]{32}$")
_CANONICAL_MEDIA_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_CONTROLLED_ARTIFACT_PREFIX = "/api/yfeistai/v1/artifacts/"
_MAX_OPPORTUNISTIC_MEDIA_CLEANUPS = 8
_CREATION_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_RAW_REFERENCE_FIELDS = frozenset(
    {
        "action",
        "downloadurl",
        "fileurl",
        "formaction",
        "href",
        "objectkey",
        "objectpath",
        "pattern",
        "poster",
        "relativepath",
        "s3key",
        "src",
        "storagekey",
        "uri",
        "url",
    }
)


def _creation_request_sha256(request: object) -> str:
    payload = {
        "allowedWebDomains": list(getattr(request, "allowed_web_domains")),
        "audience": getattr(request, "audience"),
        "classId": getattr(request, "class_id"),
        "classroomMode": getattr(request, "classroom_mode"),
        "contentMode": getattr(request, "content_mode"),
        "courseId": getattr(request, "course_id"),
        "durationMinutes": getattr(request, "duration_minutes"),
        "gradeBand": getattr(request, "grade_band"),
        "knowledgePoints": [
            {
                "description": point.description,
                "knowledgePointId": point.knowledge_point_id,
                "title": point.title,
            }
            for point in getattr(request, "knowledge_points")
        ],
        "mediaPolicy": getattr(request, "media_policy", "image_audio"),
        "objective": getattr(request, "objective"),
        "openCreationAcknowledged": getattr(
            request,
            "open_creation_acknowledged",
        ),
        "requestedExports": list(getattr(request, "requested_exports")),
        "sourceRef": getattr(request, "source_ref"),
        "sourceType": getattr(request, "source_type"),
        "templateId": getattr(request, "template_id"),
        "templateVersion": getattr(request, "template_version"),
        "title": getattr(request, "title"),
        "webPolicy": getattr(request, "web_policy"),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _creation_key(
    context: TenantContext,
    request_sha256: str,
    supplied: str | None,
) -> str:
    if supplied is not None:
        if _CREATION_IDEMPOTENCY_KEY_PATTERN.fullmatch(supplied) is None:
            raise InvalidClassroomState("classroom idempotency key is invalid")
        return supplied
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "requestSha256": request_sha256,
                "tenantId": context.tenant_id,
                "userId": context.user_id,
            }
        )
    ).hexdigest()
    return f"auto-{digest}"


def _creation_identifier(prefix: str, tenant_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "idempotencyKey": idempotency_key,
                "tenantId": tenant_id,
            }
        )
    ).hexdigest()
    return f"{prefix}-{digest[:32]}"


_MEDIA_REFERENCE_FIELD = re.compile(
    r"^(?:audio|file|image|media|poster|video)(?:href|path|ref|reference|src|uri|url)$"
)
_UNSAFE_SCHEMES = (
    "blob:",
    "data:",
    "file:",
    "ftp:",
    "http:",
    "https:",
    "javascript:",
    "s3:",
    "vbscript:",
    "ws:",
    "wss:",
)
_ALLOWED_HTML_TAGS = frozenset(
    {
        "a",
        "article",
        "b",
        "br",
        "button",
        "canvas",
        "circle",
        "div",
        "em",
        "footer",
        "g",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "i",
        "img",
        "input",
        "label",
        "li",
        "line",
        "main",
        "ol",
        "option",
        "p",
        "path",
        "polygon",
        "polyline",
        "rect",
        "section",
        "select",
        "small",
        "span",
        "strong",
        "svg",
        "table",
        "tbody",
        "td",
        "text",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_VOID_HTML_TAGS = frozenset({"br", "hr", "img", "input"})
_GLOBAL_HTML_ATTRIBUTES = frozenset(
    {"class", "dir", "hidden", "id", "lang", "role", "tabindex", "title"}
)
_HTML_ATTRIBUTES_BY_TAG = {
    "a": frozenset({"href"}),
    "button": frozenset({"disabled", "name", "type", "value"}),
    "canvas": frozenset({"height", "width"}),
    "circle": frozenset({"cx", "cy", "fill", "r", "stroke"}),
    "img": frozenset({"alt", "data-media-id", "height", "width"}),
    "input": frozenset(
        {
            "checked",
            "disabled",
            "max",
            "min",
            "name",
            "placeholder",
            "step",
            "type",
            "value",
        }
    ),
    "label": frozenset({"for"}),
    "line": frozenset({"stroke", "x1", "x2", "y1", "y2"}),
    "option": frozenset({"disabled", "selected", "value"}),
    "path": frozenset({"d", "fill", "stroke"}),
    "polygon": frozenset({"fill", "points", "stroke"}),
    "polyline": frozenset({"fill", "points", "stroke"}),
    "rect": frozenset({"fill", "height", "rx", "ry", "stroke", "width", "x", "y"}),
    "select": frozenset({"disabled", "multiple", "name"}),
    "svg": frozenset({"height", "viewbox", "width"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
}
_VALIDATION_SECTION_NAMES = (
    "dsl_integrity",
    "media_integrity",
    "knowledge_point_coverage",
    "source_traceability",
    "unsupported_claims",
    "quiz_answerability",
    "interactive_security",
    "accessibility",
    "export_readiness",
)
_MAX_DRAFT_MEDIA_BYTES = 100 * 1024 * 1024
_DRAFT_MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


def draft_media_relative_path(media_id: str, mime_type: str) -> str:
    """Return the durable portable path for one server-issued draft upload."""

    suffix = _DRAFT_MEDIA_SUFFIXES.get(mime_type)
    if _MEDIA_ID_PATTERN.fullmatch(media_id) is None or suffix is None:
        raise InvalidDraftMedia("draft media identity is invalid")
    return f"media/{media_id}{suffix}"


def _walk(value: object, path: str = "$"):
    yield path, None, value
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidDraftDocument("draft document has an unsafe reference")
            child = f"{path}.{key}"
            yield child, key, item
            yield from _walk(item, child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def _normalized_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _field_tokens(value: str) -> tuple[str, ...]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return tuple(token.casefold() for token in re.findall(r"[A-Za-z0-9]+", separated))


def _decoded_reference(value: str) -> str:
    decoded = value
    for _ in range(8):
        candidate = unescape(unquote(decoded))
        if candidate == decoded:
            return decoded
        decoded = candidate
    if unescape(unquote(decoded)) != decoded:
        raise InvalidDraftDocument("draft document has an unsafe reference")
    return decoded


def _contains_unsafe_reference(value: str) -> bool:
    decoded = _decoded_reference(value)
    compact = "".join(
        character
        for character in decoded.casefold()
        if not character.isspace() and ord(character) >= 32 and ord(character) != 127
    )
    return (
        any(scheme in compact for scheme in _UNSAFE_SCHEMES)
        or compact.startswith("//")
        or "tenants/" in compact
        or "/tenants/" in compact
    )


def _contains_css_url(value: str) -> bool:
    compact = "".join(
        character
        for character in _decoded_reference(value).casefold()
        if not character.isspace() and ord(character) >= 32 and ord(character) != 127
    )
    return "url(" in compact


def _looks_like_reference_value(value: object) -> bool:
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ):
        return True
    if not isinstance(value, str):
        return False
    decoded = _decoded_reference(value).strip()
    if _contains_unsafe_reference(decoded) or "\\" in decoded:
        return True
    if decoded.startswith(("/", "./", "../")):
        return True
    if re.search(r"(?:^|/)\.\.?(/|$)", decoded):
        return True
    if "/" in decoded and not any(character.isspace() for character in decoded):
        return True
    return re.fullmatch(r"[^\s/\\]+\.[A-Za-z0-9]{1,10}(?:[?#].*)?", decoded) is not None


def _is_raw_reference_field(key: str) -> bool:
    normalized_key = _normalized_field_name(key)
    tokens = _field_tokens(key)
    return (
        normalized_key in _RAW_REFERENCE_FIELDS
        or _MEDIA_REFERENCE_FIELD.fullmatch(normalized_key) is not None
        or any(token in {"href", "src", "uri", "url"} for token in tokens)
    )


def _is_reference_semantic_field(key: str, value: object) -> bool:
    if _is_raw_reference_field(key):
        return True
    normalized_key = _normalized_field_name(key)
    tokens = set(_field_tokens(key))
    reference_context = {
        "asset",
        "audio",
        "file",
        "image",
        "media",
        "object",
        "poster",
        "resource",
        "storage",
        "thumbnail",
        "video",
    }
    if normalized_key.endswith("srcset"):
        return True
    if "path" in tokens:
        if tokens.intersection(reference_context):
            return True
        return normalized_key == "path" and _looks_like_reference_value(value)
    if tokens.intersection({"asset", "resource", "thumbnail"}):
        return _looks_like_reference_value(value)
    return False


def _media_id_field_kind(key: str) -> str | None:
    normalized = _normalized_field_name(key)
    if normalized.endswith("mediaids"):
        return "plural"
    if normalized.endswith("mediaid"):
        return "singular"
    return None


class _InteractiveHtmlValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.media_ids: set[str] = set()
        self._open_tags: list[str] = []

    @staticmethod
    def _unsafe() -> InvalidDraftDocument:
        return InvalidDraftDocument("draft document has an unsafe reference")

    def _validate_attributes(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        allowed = _HTML_ATTRIBUTES_BY_TAG.get(tag, frozenset())
        seen: set[str] = set()
        for raw_name, raw_value in attrs:
            name = raw_name.casefold()
            if name in seen or name.startswith("on") or name == "style":
                raise self._unsafe()
            seen.add(name)
            if (
                name not in _GLOBAL_HTML_ATTRIBUTES
                and name not in allowed
                and not name.startswith("aria-")
                and not name.startswith("data-")
            ):
                raise self._unsafe()
            value = raw_value or ""
            normalized_name = _normalized_field_name(name)
            if _contains_css_url(value):
                raise self._unsafe()
            if name == "href":
                if not re.fullmatch(r"#[A-Za-z][A-Za-z0-9_.:-]*", value):
                    raise self._unsafe()
            elif _is_raw_reference_field(name) or _contains_unsafe_reference(value):
                raise self._unsafe()
            if name == "data-media-id":
                if _CANONICAL_MEDIA_ID_PATTERN.fullmatch(value) is None:
                    raise self._unsafe()
                self.media_ids.add(value)
            elif normalized_name.endswith(("url", "uri", "src", "href", "path")):
                raise self._unsafe()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        if tag not in _ALLOWED_HTML_TAGS:
            raise self._unsafe()
        self._validate_attributes(tag, attrs)
        if tag not in _VOID_HTML_TAGS:
            self._open_tags.append(tag)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        if tag not in _ALLOWED_HTML_TAGS:
            raise self._unsafe()
        self._validate_attributes(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if not self._open_tags or self._open_tags[-1] != tag:
            raise self._unsafe()
        self._open_tags.pop()

    def handle_data(self, data: str) -> None:
        if _contains_unsafe_reference(data):
            raise self._unsafe()

    def handle_decl(self, _decl: str) -> None:
        raise self._unsafe()

    def handle_pi(self, _data: str) -> None:
        raise self._unsafe()

    def unknown_decl(self, _data: str) -> None:
        raise self._unsafe()

    def validated_media_ids(self, value: str) -> frozenset[str]:
        try:
            self.feed(value)
            self.close()
        except InvalidDraftDocument:
            raise
        except Exception:
            raise self._unsafe() from None
        if self._open_tags:
            raise self._unsafe()
        return frozenset(self.media_ids)


def _validate_interactive_html(value: object) -> frozenset[str]:
    if not isinstance(value, str) or not value.strip():
        raise InvalidDraftDocument("draft document has an unsafe reference")
    return _InteractiveHtmlValidator().validated_media_ids(value)


def _canonical_classroom_document(document: Mapping[str, Any]) -> ClassroomDocument:
    if not isinstance(document, Mapping):
        raise InvalidDraftDocument("draft document is invalid")
    try:
        parsed = ClassroomDocument.model_validate(document)
    except Exception:
        raise InvalidDraftDocument("draft document is not a portable classroom") from None
    if canonical_json_bytes(document) != canonical_json_bytes(parsed):
        raise InvalidDraftDocument("draft document is not canonical")
    raw = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    file_sha256 = raw.pop("fileSha256")
    if hashlib.sha256(canonical_json_bytes(raw)).hexdigest() != file_sha256:
        raise InvalidDraftDocument("draft document file hash is invalid")
    return parsed


def _materialize_edited_classroom_document(
    document: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise InvalidDraftDocument("draft document is invalid")
    try:
        parsed = ClassroomDocument.model_validate(document)
    except Exception:
        raise InvalidDraftDocument("draft document is not a portable classroom") from None
    canonical = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    canonical.pop("fileSha256")
    original_without_hash = dict(document)
    if "fileSha256" not in original_without_hash:
        raise InvalidDraftDocument("draft document is not canonical")
    original_without_hash.pop("fileSha256")
    if canonical_json_bytes(original_without_hash) != canonical_json_bytes(canonical):
        raise InvalidDraftDocument("draft document is not canonical")
    canonical["fileSha256"] = hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()
    return canonical


def _validate_portable_path(value: str) -> None:
    decoded = _decoded_reference(value)
    parsed = urlsplit(decoded)
    if (
        not decoded
        or "\\" in decoded
        or "\x00" in decoded
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or decoded.startswith("//")
        or _contains_unsafe_reference(decoded)
        or any(segment in {"", ".", ".."} for segment in decoded.strip("/").split("/"))
    ):
        raise InvalidDraftDocument("draft document has an unsafe reference")


def _validate_controlled_artifact_path(value: str, relative_path: str) -> None:
    _validate_portable_path(value)
    if not value.startswith(_CONTROLLED_ARTIFACT_PREFIX):
        raise InvalidDraftDocument("draft document media must use a controlled artifact path")
    remainder = value[len(_CONTROLLED_ARTIFACT_PREFIX) :]
    encoded_job_id, separator, _encoded_path = remainder.partition("/")
    try:
        job_id = unquote(encoded_job_id, errors="strict")
    except UnicodeError:
        raise InvalidDraftDocument(
            "draft document media must use a controlled artifact path"
        ) from None
    safe = "-_.!~*'()"
    expected_path = "/".join(quote(segment, safe=safe) for segment in relative_path.split("/"))
    expected = f"{_CONTROLLED_ARTIFACT_PREFIX}{quote(job_id, safe=safe)}/{expected_path}"
    if not separator or _CANONICAL_MEDIA_ID_PATTERN.fullmatch(job_id) is None or value != expected:
        raise InvalidDraftDocument("draft document media must use a controlled artifact path")


def _embedded_media_ids(
    value: object,
    media_ids_by_path: Mapping[str, str],
) -> frozenset[str]:
    media_ids: set[str] = set()
    for _path, key, nested in _walk(value):
        normalized_key = _normalized_field_name(key) if isinstance(key, str) else None
        media_id_kind = _media_id_field_kind(key) if isinstance(key, str) else None
        if normalized_key == "html":
            media_ids.update(_validate_interactive_html(nested))
        elif media_id_kind == "singular":
            if not isinstance(nested, str) or _CANONICAL_MEDIA_ID_PATTERN.fullmatch(nested) is None:
                raise InvalidDraftDocument("draft document has an unsafe reference")
            media_ids.add(nested)
        elif media_id_kind == "plural":
            if not isinstance(nested, list) or any(
                not isinstance(item, str) or _CANONICAL_MEDIA_ID_PATTERN.fullmatch(item) is None
                for item in nested
            ):
                raise InvalidDraftDocument("draft document has an unsafe reference")
            media_ids.update(nested)
        elif normalized_key is not None and _is_reference_semantic_field(key, nested):
            if not isinstance(nested, str) or nested not in media_ids_by_path:
                raise InvalidDraftDocument("draft document has an unsafe reference")
            media_ids.add(media_ids_by_path[nested])
    return frozenset(media_ids)


def validate_draft_document_references(
    document: Mapping[str, Any],
    *,
    available_media_bindings: tuple[ClassroomMediaBinding, ...] = (),
) -> frozenset[str]:
    """Validate one canonical document against trusted immutable media receipts."""

    parsed = _canonical_classroom_document(document)
    bindings = {item.media_id: item for item in available_media_bindings}
    if len(bindings) != len(available_media_bindings):
        raise InvalidDraftDocument("available classroom media bindings conflict")
    declared_ids: set[str] = set()
    declared_paths: set[str] = set()
    media_ids_by_path: dict[str, str] = {}
    for item in parsed.media_manifest:
        if (
            _CANONICAL_MEDIA_ID_PATTERN.fullmatch(item.media_id) is None
            or item.media_id in declared_ids
            or item.relative_path in declared_paths
        ):
            raise InvalidDraftDocument("classroom media manifest conflicts")
        try:
            ArtifactManifestEntry(
                relative_name=item.relative_path,
                content_type=item.mime_type,
                sha256=item.sha256,
                size=item.size_bytes,
            ).validate()
        except ArtifactManifestError:
            raise InvalidDraftDocument("classroom media manifest is invalid") from None
        legacy_item = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        temporary_download_path = legacy_item.get("temporaryDownloadPath")
        if isinstance(temporary_download_path, str):
            _validate_controlled_artifact_path(
                temporary_download_path,
                item.relative_path,
            )
        binding = bindings.get(item.media_id)
        if binding is None or (
            binding.relative_name,
            binding.mime_type,
            binding.sha256,
            binding.size_bytes,
        ) != (
            item.relative_path,
            item.mime_type,
            item.sha256,
            item.size_bytes,
        ):
            raise InvalidDraftDocument("draft document references unavailable media")
        declared_ids.add(item.media_id)
        declared_paths.add(item.relative_path)
        media_ids_by_path[item.relative_path] = item.media_id
    for item in parsed.export_manifest:
        try:
            ArtifactManifestEntry(
                relative_name=item.relative_path,
                content_type=item.mime_type,
                sha256=item.sha256,
                size=item.size_bytes,
            ).validate()
        except ArtifactManifestError:
            raise InvalidDraftDocument("classroom export manifest is invalid") from None
        _validate_controlled_artifact_path(
            item.temporary_download_path,
            item.relative_path,
        )
    referenced_ids: set[str] = set()
    for scene in parsed.openmaic.scenes:
        referenced_ids.update(
            _embedded_media_ids(
                {
                    "content": scene.content.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                    "actions": scene.actions,
                },
                media_ids_by_path,
            )
        )
    if referenced_ids != declared_ids:
        raise InvalidDraftDocument(
            "draft document media manifest must match referenced media exactly"
        )
    return frozenset(declared_ids)


def _issue(severity: str, code: str, message: str, path: str) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "path": path,
    }


def _section(issues: list[dict[str, str]]) -> dict[str, object]:
    status = "pass"
    if any(issue["severity"] == "error" for issue in issues):
        status = "error"
    elif issues:
        status = "warning"
    return {"status": status, "issues": issues}


def build_validation_report(
    document: Mapping[str, Any],
    *,
    required_knowledge_point_ids: tuple[str, ...],
    grounded: bool,
    available_media_bindings: tuple[ClassroomMediaBinding, ...],
) -> dict[str, object]:
    """Build the persisted, actionable nine-section Task 4 quality report."""

    parsed = _canonical_classroom_document(document)
    document_view = {
        "dslVersion": parsed.openmaic.dsl_version,
        "scenes": [
            scene.model_dump(mode="json", by_alias=True, exclude_none=True)
            for scene in parsed.openmaic.scenes
        ],
        "knowledgePointMappings": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in parsed.knowledge_point_mappings
        ],
        "sourceRefs": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in parsed.source_refs
        ],
    }
    sections: dict[str, dict[str, object]] = {
        name: _section([]) for name in _VALIDATION_SECTION_NAMES
    }
    dsl_issues: list[dict[str, str]] = []
    scenes = document_view["scenes"]
    if document_view["dslVersion"] != "0.1.0":
        dsl_issues.append(
            _issue("error", "dsl_version_invalid", "Use DSL version 0.1.0.", "$.dslVersion")
        )
    if not isinstance(scenes, list) or not scenes:
        dsl_issues.append(
            _issue("error", "scenes_missing", "Add at least one classroom scene.", "$.scenes")
        )
        scenes = []
    sections["dsl_integrity"] = _section(dsl_issues)

    media_issues: list[dict[str, str]] = []
    try:
        validate_draft_document_references(
            document,
            available_media_bindings=available_media_bindings,
        )
    except InvalidDraftDocument:
        media_issues.append(
            _issue(
                "error",
                "media_reference_unsafe",
                "Replace external media references with uploaded media IDs.",
                "$",
            )
        )
    sections["media_integrity"] = _section(media_issues)

    mappings = document_view["knowledgePointMappings"]
    mapped_ids = (
        {
            item.get("knowledgePointId")
            for item in mappings
            if isinstance(item, Mapping) and isinstance(item.get("knowledgePointId"), str)
        }
        if isinstance(mappings, list)
        else set()
    )
    coverage_issues = [
        _issue(
            "warning",
            "knowledge_point_uncovered",
            "Map this knowledge point to at least one scene.",
            f"$.knowledgePointMappings[{point_id}]",
        )
        for point_id in required_knowledge_point_ids
        if point_id not in mapped_ids
    ]
    sections["knowledge_point_coverage"] = _section(coverage_issues)

    source_refs = document_view["sourceRefs"]
    traceability_issues: list[dict[str, str]] = []
    if grounded and (not isinstance(source_refs, list) or not source_refs):
        traceability_issues.append(
            _issue(
                "warning",
                "source_trace_missing",
                "Attach at least one approved source reference.",
                "$.sourceRefs",
            )
        )
    sections["source_traceability"] = _section(traceability_issues)
    sections["unsupported_claims"] = _section(
        [
            _issue(
                "warning",
                "claims_need_review",
                "Review factual claims that do not cite an approved source.",
                "$.scenes",
            )
        ]
        if grounded and traceability_issues
        else []
    )

    quiz_issues: list[dict[str, str]] = []
    security_issues: list[dict[str, str]] = []
    accessibility_issues: list[dict[str, str]] = []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, Mapping):
            dsl_issues.append(
                _issue("error", "scene_invalid", "Replace the invalid scene.", f"$.scenes[{index}]")
            )
            continue
        content = scene.get("content")
        if scene.get("type") == "quiz" and isinstance(content, Mapping):
            questions = content.get("questions")
            if not isinstance(questions, list) or any(
                not isinstance(question, Mapping) or not question.get("correctOptionIds")
                for question in questions
            ):
                quiz_issues.append(
                    _issue(
                        "error",
                        "quiz_answer_missing",
                        "Set at least one correct answer for every quiz question.",
                        f"$.scenes[{index}].content.questions",
                    )
                )
        if scene.get("type") == "interactive" and isinstance(content, Mapping):
            html = content.get("html")
            try:
                _validate_interactive_html(html)
            except InvalidDraftDocument:
                security_issues.append(
                    _issue(
                        "error",
                        "interactive_html_unsafe",
                        "Use only approved HTML elements, attributes, and opaque media IDs.",
                        f"$.scenes[{index}].content.html",
                    )
                )
        if not isinstance(scene.get("title"), str) or not scene.get("title", "").strip():
            accessibility_issues.append(
                _issue(
                    "warning",
                    "scene_title_missing",
                    "Add a descriptive scene title.",
                    f"$.scenes[{index}].title",
                )
            )
        if scene.get("type") == "interactive":
            accessibility_issues.append(
                _issue(
                    "warning",
                    "interactive_accessibility_review",
                    "Verify keyboard navigation and an accessible text alternative.",
                    f"$.scenes[{index}]",
                )
            )
    sections["dsl_integrity"] = _section(dsl_issues)
    sections["quiz_answerability"] = _section(quiz_issues)
    sections["interactive_security"] = _section(security_issues)
    sections["accessibility"] = _section(accessibility_issues)

    blocking_sections = {
        "dsl_integrity",
        "media_integrity",
        "quiz_answerability",
        "interactive_security",
    }
    blocking = any(sections[name]["status"] == "error" for name in blocking_sections)
    sections["export_readiness"] = _section(
        [
            _issue(
                "error",
                "export_blocked",
                "Resolve all severe validation findings before export.",
                "$",
            )
        ]
        if blocking
        else []
    )

    all_issues = [
        issue
        for section in sections.values()
        for issue in section["issues"]
        if isinstance(issue, dict)
    ]
    severe = [issue for issue in all_issues if issue["severity"] == "error"]
    warnings = [issue for issue in all_issues if issue["severity"] == "warning"]
    return {
        "valid": not severe,
        "severeFindings": severe,
        "warnings": warnings,
        "sections": sections,
    }


def _allows(
    context: TenantContext,
    permission: str,
    *,
    course_id: str,
    class_id: str,
) -> bool:
    resource = ResourceScope(
        tenant_id=context.tenant_id,
        course_id=course_id,
        class_id=class_id,
    )
    return any(grant.allows_resource(permission, resource) for grant in context.permissions)


def _outline_payload(outline: OutlineBundle) -> dict[str, Any]:
    return outline.model_dump(mode="json", by_alias=True, exclude_none=True)


def _draft_outline_sha256(outline: OutlineBundle) -> str:
    draft = outline.model_copy(
        update={"confirmation_metadata": OutlineConfirmationMetadata(status="draft")}
    )
    return canonical_outline_sha256(draft)


def matches_reviewed_outline_binding(
    *,
    lifecycle_state: str,
    revision: int,
    outline: OutlineBundle,
    confirmed_outline_sha256: str | None,
    expected_revision: int,
    expected_outline_sha256: str,
) -> bool:
    """Accept a reviewed draft or its one-step durable confirmation recovery."""

    if revision == expected_revision:
        return hmac.compare_digest(
            canonical_outline_sha256(outline),
            expected_outline_sha256,
        )
    metadata = outline.confirmation_metadata
    return (
        lifecycle_state == "generating_content"
        and revision == expected_revision + 1
        and metadata.status == "confirmed"
        and metadata.confirmed_at is not None
        and metadata.confirmed_by is not None
        and confirmed_outline_sha256 is not None
        and hmac.compare_digest(
            _draft_outline_sha256(outline),
            expected_outline_sha256,
        )
        and hmac.compare_digest(
            canonical_outline_sha256(outline),
            confirmed_outline_sha256,
        )
    )


def _is_durable_confirmed_outline_recovery(
    record: ClassroomRecord,
    stage_outline: OutlineBundle,
) -> bool:
    if (
        record.lifecycle_state != "generating_content"
        or record.revision <= 1
        or record.outline is None
        or record.confirmed_outline_sha256 is None
        or stage_outline.confirmation_metadata.status != "draft"
    ):
        return False
    try:
        persisted = OutlineBundle.model_validate(record.outline)
    except Exception:
        return False
    metadata = persisted.confirmation_metadata
    return (
        metadata.status == "confirmed"
        and metadata.confirmed_at is not None
        and metadata.confirmed_by is not None
        and hmac.compare_digest(
            canonical_outline_sha256(persisted),
            record.confirmed_outline_sha256,
        )
        and hmac.compare_digest(
            _draft_outline_sha256(persisted),
            canonical_outline_sha256(stage_outline),
        )
    )


def _sha256_payload(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _media_magic_matches(mime_type: str, prefix: bytes) -> bool:
    if mime_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if mime_type == "image/gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    if mime_type == "audio/mpeg":
        return prefix.startswith(b"ID3") or (
            len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
        )
    if mime_type in {"audio/wav", "audio/x-wav"}:
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE"
    if mime_type == "video/mp4":
        return len(prefix) >= 12 and prefix[4:8] == b"ftyp"
    if mime_type.endswith("presentationml.presentation"):
        return prefix.startswith(b"PK\x03\x04")
    return False


async def _stage_media(upload: DraftMediaUpload, mime_type: str):
    handle = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    try:
        while True:
            chunk = await upload.read(64 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise InvalidDraftMedia("draft media stream is invalid")
            size += len(chunk)
            if size > _MAX_DRAFT_MEDIA_BYTES:
                raise InvalidDraftMedia("draft media exceeds the 100 MiB limit")
            if len(prefix) < 4096:
                prefix.extend(chunk[: 4096 - len(prefix)])
            digest.update(chunk)
            handle.write(chunk)
        if size == 0 or not _media_magic_matches(mime_type, bytes(prefix)):
            raise InvalidDraftMedia("draft media content does not match its MIME type")
        if mime_type.endswith("presentationml.presentation"):
            handle.seek(0)
            try:
                with zipfile.ZipFile(handle) as archive:
                    names = frozenset(archive.namelist())
            except (OSError, zipfile.BadZipFile):
                raise InvalidDraftMedia(
                    "draft media content does not match its MIME type"
                ) from None
            if "[Content_Types].xml" not in names or not any(
                name.startswith("ppt/") for name in names
            ):
                raise InvalidDraftMedia("draft media content does not match its MIME type")
        handle.seek(0)
        return handle, digest.hexdigest(), size
    except BaseException:
        handle.close()
        raise
    finally:
        await upload.close()


async def _spooled_chunks(handle) -> AsyncIterator[bytes]:
    while chunk := handle.read(64 * 1024):
        yield chunk


def _generation_request_payload(request: GenerationRequest) -> str:
    return json.dumps(
        request.model_dump(mode="json", by_alias=True, exclude_none=False),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SqlAlchemyClassroomGeneration:
    """Adapter from teacher authoring to the Plan 02 durable job state machine."""

    def __init__(
        self,
        repository: SqlAlchemyGenerationJobRepository,
        selector: DataPlaneSelector,
        *,
        priority: str = "teacher",
        batch_id: str | None = None,
        retry_of_job_id: str | None = None,
    ) -> None:
        if priority not in {"teacher", "batch"}:
            raise ValueError("classroom generation priority is invalid")
        if (priority == "batch") != (batch_id is not None):
            raise ValueError("batch generation requires a batch id")
        if retry_of_job_id is not None and batch_id is None:
            raise ValueError("classroom generation retry requires a batch id")
        self._repository = repository
        self._selector = selector
        self._priority = priority
        self._batch_id = batch_id
        self._retry_of_job_id = retry_of_job_id

    @staticmethod
    def _job_id(tenant_id: str, asset_id: str) -> str:
        digest = hashlib.sha256(f"{tenant_id}\0{asset_id}\0outline".encode()).hexdigest()
        return f"job-{digest[:48]}"

    @staticmethod
    def _stage(details: GenerationJobDetails) -> GenerationStage:
        outline = None
        if details.result_payload is not None and details.phase == "outline":
            try:
                outline = OutlineBundle.model_validate_json(details.result_payload)
            except Exception:
                raise InvalidClassroomState("outline result is unavailable") from None
        return GenerationStage(
            job_id=details.job_id,
            status=details.status,
            outline=outline,
            classroom_version_id=None,
        )

    async def start_outline(
        self,
        *,
        context: TenantContext,
        asset_id: str,
        draft_id: str,
        teaching_brief: TeachingBrief,
        requested_exports: tuple[str, ...],
    ) -> GenerationStage:
        selection = await self._selector.resolve(context.tenant_id)
        if selection is None:
            raise InvalidClassroomState("generation data plane is unavailable")
        job_id = self._job_id(context.tenant_id, asset_id)
        generation = GenerationRequest(
            schema_version="1.0",
            tenant_id=context.tenant_id,
            request_id=f"request-{job_id[4:]}",
            job_id=job_id,
            idempotency_key=f"classroom-outline-{asset_id}",
            phase="outline",
            classroom_mode="full",
            teaching_brief_id=teaching_brief.brief_id,
            teaching_brief_sha256=teaching_brief.content_sha256,
            teaching_brief=teaching_brief,
            confirmed_outline=None,
            confirmed_outline_sha256=None,
            template_id=teaching_brief.template_policy.template_id,
            template_version=teaching_brief.template_policy.template_version,
            scene_budget=max(1, min(100, teaching_brief.duration_minutes // 3)),
            duration_minutes=teaching_brief.duration_minutes,
            requested_exports=list(requested_exports),
            callback_context=draft_id,
            data_plane_route_id=selection.route_ref,
            priority=self._priority,
        )
        payload = _generation_request_payload(generation)
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        await self._repository.create_job_and_reserve(
            GenerationJobRequest(
                tenant_id=context.tenant_id,
                job_id=job_id,
                job_kind="generation",
                phase="outline",
                export_format=None,
                priority=self._priority,
                quota_units=max(1, teaching_brief.duration_minutes),
                actor_id=context.user_id,
                owner_id=context.user_id,
                visibility="class",
                request_id=generation.request_id,
                idempotency_key=generation.idempotency_key,
                request_sha256=payload_sha256,
                data_plane_mode=selection.mode,
                data_plane_route_id=selection.route_ref,
                provider_profile_id=selection.provider_profile_ref,
                worker_pool_ref=selection.worker_pool_ref,
                queue_ref=selection.queue_ref,
                request_payload=payload,
                classroom_draft_id=draft_id,
                batch_id=self._batch_id,
                retry_of_job_id=self._retry_of_job_id,
                resource_course_id=teaching_brief.course_id,
                resource_class_id=teaching_brief.target_class_id,
                public_request_sha256=payload_sha256,
            )
        )
        details = await self._repository.get_job_details(context.tenant_id, job_id)
        if details is None:
            raise InvalidClassroomState("generation job is unavailable")
        return self._stage(details)

    async def get_stage(
        self,
        *,
        context: TenantContext,
        job_id: str,
    ) -> GenerationStage:
        details = await self._repository.get_job_details(context.tenant_id, job_id)
        if details is None or details.tenant_id != context.tenant_id:
            raise ClassroomNotFound("classroom job not found")
        return self._stage(details)

    async def start_content(
        self,
        *,
        context: TenantContext,
        asset_id: str,
        draft_id: str,
        job_id: str,
        confirmed_outline: OutlineBundle,
        confirmed_outline_sha256: str,
    ) -> GenerationStage:
        details = await self._repository.get_job_details(context.tenant_id, job_id)
        if details is None or details.tenant_id != context.tenant_id:
            raise InvalidClassroomState("outline cannot start content generation")
        if details.phase == "content":
            return self._idempotent_content_stage(
                details,
                context=context,
                asset_id=asset_id,
                draft_id=draft_id,
                job_id=job_id,
                confirmed_outline=confirmed_outline,
                confirmed_outline_sha256=confirmed_outline_sha256,
            )
        if (
            details.job_kind != "generation"
            or details.phase != "outline"
            or details.status != "awaiting_confirmation"
            or details.result_payload is None
        ):
            raise InvalidClassroomState("outline cannot start content generation")
        try:
            original = GenerationRequest.model_validate_json(details.request_payload)
            issued = OutlineBundle.model_validate_json(details.result_payload)
            self._validate_job_request_identity(
                details,
                original,
                context,
                asset_id,
                draft_id,
                job_id,
            )
            validate_outline_binding(
                issued,
                original,
                expected_confirmation_status="draft",
            )
            validate_outline_binding(
                confirmed_outline,
                original,
                expected_confirmation_status="confirmed",
            )
        except InvalidClassroomState:
            raise
        except Exception:
            raise InvalidClassroomState("confirmed outline is invalid") from None
        if not hmac.compare_digest(
            confirmed_outline_sha256,
            canonical_outline_sha256(confirmed_outline),
        ):
            raise InvalidClassroomState("confirmed outline hash is invalid")
        content_payload = original.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
        content_payload.update(
            phase="content",
            confirmedOutline=_outline_payload(confirmed_outline),
            confirmedOutlineSha256=confirmed_outline_sha256,
        )
        content_request = GenerationRequest.model_validate(content_payload)
        payload = _generation_request_payload(content_request)
        requeued = await self._repository.requeue_confirmed_content(
            context.tenant_id,
            job_id,
            request_payload=payload,
            request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        )
        if not requeued:
            raced = await self._repository.get_job_details(context.tenant_id, job_id)
            if raced is None:
                raise InvalidClassroomState("outline cannot start content generation")
            return self._idempotent_content_stage(
                raced,
                context=context,
                asset_id=asset_id,
                draft_id=draft_id,
                job_id=job_id,
                confirmed_outline=confirmed_outline,
                confirmed_outline_sha256=confirmed_outline_sha256,
            )
        updated = await self._repository.get_job_details(context.tenant_id, job_id)
        if updated is None:
            raise InvalidClassroomState("generation job is unavailable")
        return self._stage(updated)

    @staticmethod
    def _validate_job_request_identity(
        details: GenerationJobDetails,
        request: GenerationRequest,
        context: TenantContext,
        asset_id: str,
        draft_id: str,
        job_id: str,
    ) -> None:
        if (
            details.job_kind != "generation"
            or request.tenant_id != context.tenant_id
            or job_id != SqlAlchemyClassroomGeneration._job_id(context.tenant_id, asset_id)
            or request.job_id != job_id
            or request.request_id != details.request_id
            or request.idempotency_key != details.idempotency_key
            or request.data_plane_route_id != details.data_plane_route_id
            or details.classroom_draft_id != draft_id
            or request.callback_context != draft_id
        ):
            raise InvalidClassroomState("confirmed outline job binding is invalid")

    def _idempotent_content_stage(
        self,
        details: GenerationJobDetails,
        *,
        context: TenantContext,
        asset_id: str,
        draft_id: str,
        job_id: str,
        confirmed_outline: OutlineBundle,
        confirmed_outline_sha256: str,
    ) -> GenerationStage:
        if details.status not in {
            "queued",
            "generating_content",
            "validating",
            "materializing",
            "succeeded",
            "failed",
            "canceled",
        }:
            raise InvalidClassroomState("content generation state is invalid")
        try:
            request = GenerationRequest.model_validate_json(details.request_payload)
            self._validate_job_request_identity(
                details,
                request,
                context,
                asset_id,
                draft_id,
                job_id,
            )
            validate_outline_binding(
                confirmed_outline,
                request,
                expected_confirmation_status="confirmed",
            )
        except InvalidClassroomState:
            raise
        except Exception:
            raise InvalidClassroomState("confirmed content binding is invalid") from None
        if (
            request.phase != "content"
            or request.confirmed_outline is None
            or request.confirmed_outline_sha256 is None
            or not hmac.compare_digest(
                confirmed_outline_sha256,
                canonical_outline_sha256(confirmed_outline),
            )
            or not hmac.compare_digest(
                confirmed_outline_sha256,
                request.confirmed_outline_sha256,
            )
            or canonical_json_bytes(confirmed_outline)
            != canonical_json_bytes(request.confirmed_outline)
        ):
            raise InvalidClassroomState("confirmed content binding is invalid")
        return self._stage(details)


class ClassroomService:
    """Coordinate one tenant-safe draft with the durable generation job kernel."""

    def __init__(
        self,
        repository: ClassroomRepository,
        brief_builder: TeachingBriefBuilder,
        generation: ClassroomGeneration,
        store_provider: DraftMediaStoreProvider | None,
        *,
        clock=lambda: datetime.now(timezone.utc),
        student_owner_only: bool = False,
    ) -> None:
        self._repository = repository
        self._brief_builder = brief_builder
        self._generation = generation
        self._store_provider = store_provider
        self._clock = clock
        self._student_owner_only = student_owner_only

    async def create(
        self,
        context: TenantContext,
        request: object,
        idempotency_key: str | None = None,
    ) -> ClassroomRecord:
        course_id = str(getattr(request, "course_id"))
        class_id = str(getattr(request, "class_id"))
        if not _allows(
            context,
            "classroom.create",
            course_id=course_id,
            class_id=class_id,
        ):
            raise ClassroomAccessDenied("classroom creation is denied")
        if getattr(request, "classroom_mode") != "full":
            raise ClassroomPreflightRejected("teacher classroom must use full mode")

        try:
            request_sha256 = _creation_request_sha256(request)
            creation_key = _creation_key(context, request_sha256, idempotency_key)
        except ValueError as exc:
            raise ClassroomPreflightRejected(str(exc)) from None
        existing = await self._repository.get_creation(creation_key)
        if existing is not None:
            self._validate_creation_binding(
                context,
                existing,
                creation_key,
                request_sha256,
            )
            return await self._resume_creation(
                context,
                existing,
                tuple(getattr(request, "requested_exports")),
            )

        try:
            points = tuple(
                KnowledgePointSpec(
                    knowledge_point_id=point.knowledge_point_id,
                    title=point.title,
                    description=point.description,
                )
                for point in getattr(request, "knowledge_points")
            )
            brief_spec = TeachingBriefSpec(
                course_id=course_id,
                class_id=class_id,
                objective=getattr(request, "objective"),
                grade_band=getattr(request, "grade_band"),
                audience=getattr(request, "audience"),
                duration_minutes=getattr(request, "duration_minutes"),
                classroom_mode="full",
                web_policy=getattr(request, "web_policy"),
                template_id=getattr(request, "template_id"),
                template_version=getattr(request, "template_version"),
                knowledge_points=points,
                content_mode=getattr(request, "content_mode"),
                open_creation_acknowledged=getattr(
                    request,
                    "open_creation_acknowledged",
                ),
                allowed_web_domains=tuple(getattr(request, "allowed_web_domains")),
                media_policy=getattr(request, "media_policy", "image_audio"),
            )
            content_mode = getattr(request, "content_mode")
            source_type = getattr(request, "source_type")
            source_ref = getattr(request, "source_ref")
            if content_mode == "open_creation":
                if source_type is not None or source_ref is not None:
                    raise ClassroomPreflightRejected("open creation cannot select a source")
                built = self._brief_builder.open_creation(brief_spec)
            else:
                if source_type is None or source_ref is None:
                    raise ClassroomPreflightRejected("source-grounded creation requires a source")
                if source_type == "knowledge_base":
                    built = await self._brief_builder.from_kb(source_ref, brief_spec)
                elif source_type == "pdf":
                    built = await self._brief_builder.from_pdf(source_ref, brief_spec)
                else:
                    raise ClassroomPreflightRejected("classroom source is invalid")
        except ClassroomPreflightRejected:
            raise
        except ValueError as exc:
            raise ClassroomPreflightRejected(str(exc)) from None

        asset_id = _creation_identifier("asset", context.tenant_id, creation_key)
        draft_id = _creation_identifier("draft", context.tenant_id, creation_key)
        record = await self._repository.create_workflow(
            NewClassroomWorkflow(
                tenant_id=context.tenant_id,
                asset_id=asset_id,
                draft_id=draft_id,
                owner_id=context.user_id,
                title=str(getattr(request, "title")),
                teaching_brief=built.contract,
                creation_idempotency_key=creation_key,
                creation_request_sha256=request_sha256,
            )
        )
        self._validate_creation_binding(
            context,
            record,
            creation_key,
            request_sha256,
        )
        return await self._resume_creation(
            context,
            record,
            tuple(getattr(request, "requested_exports")),
        )

    @staticmethod
    def _validate_creation_binding(
        context: TenantContext,
        record: ClassroomRecord,
        idempotency_key: str,
        request_sha256: str,
    ) -> None:
        if (
            record.tenant_id != context.tenant_id
            or record.owner_id != context.user_id
            or record.creation_idempotency_key != idempotency_key
            or record.creation_request_sha256 is None
            or not hmac.compare_digest(
                record.creation_request_sha256,
                request_sha256,
            )
            or record.asset_id != _creation_identifier("asset", context.tenant_id, idempotency_key)
            or record.draft_id != _creation_identifier("draft", context.tenant_id, idempotency_key)
        ):
            raise ClassroomIdempotencyConflict("classroom idempotency key conflicts")

    async def _resume_creation(
        self,
        context: TenantContext,
        record: ClassroomRecord,
        requested_exports: tuple[str, ...],
    ) -> ClassroomRecord:
        brief = record.teaching_brief
        if brief is None:
            raise InvalidClassroomState("teaching brief is unavailable")
        if record.job_id is None:
            stage = await self._generation.start_outline(
                context=context,
                asset_id=record.asset_id,
                draft_id=record.draft_id,
                teaching_brief=brief,
                requested_exports=requested_exports,
            )
            if stage.classroom_version_id is not None:
                raise InvalidClassroomState("outline stage cannot create a classroom version")
            record = await self._repository.attach_outline_job(
                record.asset_id,
                stage.job_id,
            )
        else:
            stage = await self._generation.get_stage(
                context=context,
                job_id=record.job_id,
            )
        if record.job_id != stage.job_id:
            raise InvalidClassroomState("outline generation binding is invalid")
        if stage.classroom_version_id is not None:
            raise InvalidClassroomState("outline stage cannot create a classroom version")
        if stage.status == "awaiting_confirmation" and stage.outline is not None:
            payload = _outline_payload(stage.outline)
            record = await self._repository.save_outline(
                record.asset_id,
                payload,
                canonical_outline_sha256(stage.outline),
            )
        return record

    def _can_edit(self, context: TenantContext, record: ClassroomRecord) -> bool:
        if record.tenant_id != context.tenant_id:
            return False
        if self._student_owner_only:
            return (
                record.student_generation_request_id is not None
                and record.owner_id == context.user_id
            )
        if record.student_generation_request_id is not None:
            return False
        return _allows(
            context,
            "classroom.edit",
            course_id=record.course_id,
            class_id=record.class_id,
        )

    async def list(self, context: TenantContext) -> tuple[ClassroomRecord, ...]:
        records = await self._repository.list_workflows()
        return tuple(record for record in records if self._can_edit(context, record))

    async def get(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> ClassroomRecord | None:
        record = await self._repository.get_workflow(asset_id)
        if record is None or not self._can_edit(context, record):
            return None
        await self._reconcile_pending_media(context, asset_id)
        if record.job_id is not None:
            stage = await self._generation.get_stage(
                context=context,
                job_id=record.job_id,
            )
            if stage.classroom_version_id is not None and stage.status not in {"succeeded"}:
                raise InvalidClassroomState("classroom version has invalid job state")
            if stage.status == "awaiting_confirmation" and stage.outline is not None:
                if stage.outline.confirmation_metadata.status != "draft":
                    raise InvalidClassroomState("outline recovery state is invalid")
                if record.lifecycle_state in {"generating_outline", "awaiting_outline"}:
                    record = await self._repository.save_outline(
                        asset_id,
                        _outline_payload(stage.outline),
                        canonical_outline_sha256(stage.outline),
                    )
                elif not _is_durable_confirmed_outline_recovery(
                    record,
                    stage.outline,
                ):
                    raise InvalidClassroomState("outline recovery state is invalid")
            elif stage.status == "succeeded" and record.classroom_version_id is not None:
                record = await self._repository.mark_generation_succeeded(
                    asset_id,
                    record.job_id,
                )
        return record

    async def _reconcile_pending_media(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> None:
        try:
            receipts = await self._repository.list_cleanup_pending(
                asset_id,
                limit=_MAX_OPPORTUNISTIC_MEDIA_CLEANUPS,
            )
        except Exception:
            return
        for receipt in receipts:
            try:
                await self.reconcile_media_cleanup(context, asset_id, receipt.id)
            except Exception:
                continue

    async def get_draft(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> ClassroomRecord | None:
        return await self.get(context, asset_id)

    async def _editable_record(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> ClassroomRecord:
        record = await self._repository.get_workflow(asset_id)
        if record is None or not self._can_edit(context, record):
            raise ClassroomNotFound("classroom not found")
        return record

    async def update_outline(
        self,
        context: TenantContext,
        asset_id: str,
        outline: dict[str, Any],
        expected_revision: int,
    ) -> ClassroomRecord:
        record = await self._editable_record(context, asset_id)
        if record.lifecycle_state != "awaiting_outline":
            raise InvalidClassroomState("outline is not editable")
        parsed = OutlineBundle.model_validate(outline)
        if parsed.confirmation_metadata.status != "draft":
            raise InvalidClassroomState("confirmed outline cannot be edited")
        payload = _outline_payload(parsed)
        updated = await self._repository.update_outline(
            asset_id,
            payload,
            canonical_outline_sha256(parsed),
            expected_revision,
        )
        if updated is None:
            raise ClassroomRevisionConflict("draft revision is stale")
        return updated

    async def confirm_outline(
        self,
        context: TenantContext,
        asset_id: str,
        *,
        expected_revision: int | None = None,
        expected_outline_sha256: str | None = None,
    ) -> ClassroomRecord:
        record = await self._editable_record(context, asset_id)
        if (
            record.lifecycle_state not in {"awaiting_outline", "generating_content"}
            or record.outline is None
            or record.job_id is None
        ):
            raise InvalidClassroomState("outline cannot be confirmed")
        outline = OutlineBundle.model_validate(record.outline)
        if (expected_revision is None) != (expected_outline_sha256 is None):
            raise InvalidClassroomState("outline review binding is incomplete")
        if expected_revision is not None:
            assert expected_outline_sha256 is not None
            if not matches_reviewed_outline_binding(
                lifecycle_state=record.lifecycle_state,
                revision=record.revision,
                outline=outline,
                confirmed_outline_sha256=record.confirmed_outline_sha256,
                expected_revision=expected_revision,
                expected_outline_sha256=expected_outline_sha256,
            ):
                raise ClassroomConfirmationConflict("confirmed outline conflicts")
        if record.lifecycle_state == "awaiting_outline":
            confirmed = outline.model_copy(
                update={
                    "confirmation_metadata": OutlineConfirmationMetadata(
                        status="confirmed",
                        confirmed_at=self._clock(),
                        confirmed_by=context.user_id,
                    )
                }
            )
        else:
            confirmed = outline
            if (
                confirmed.confirmation_metadata.status != "confirmed"
                or record.confirmed_outline_sha256 is None
            ):
                raise InvalidClassroomState("confirmed outline is unavailable")
        source_outline_sha256 = _draft_outline_sha256(outline)
        outline_sha256 = canonical_outline_sha256(confirmed)
        review_binding = (
            {
                "expected_revision": expected_revision,
                "expected_outline_sha256": expected_outline_sha256,
            }
            if expected_revision is not None
            else {}
        )
        persisted = await self._repository.confirm_outline(
            asset_id,
            _outline_payload(confirmed),
            outline_sha256,
            source_outline_sha256,
            **review_binding,
        )
        if persisted.outline is None or persisted.confirmed_outline_sha256 is None:
            raise InvalidClassroomState("confirmed outline is unavailable")
        try:
            persisted_outline = OutlineBundle.model_validate(persisted.outline)
        except Exception:
            raise InvalidClassroomState("confirmed outline is unavailable") from None
        if (
            persisted_outline.confirmation_metadata.status != "confirmed"
            or not hmac.compare_digest(
                persisted.confirmed_outline_sha256,
                canonical_outline_sha256(persisted_outline),
            )
            or not hmac.compare_digest(
                source_outline_sha256,
                _draft_outline_sha256(persisted_outline),
            )
        ):
            raise InvalidClassroomState("confirmed outline binding is invalid")
        stage = await self._generation.start_content(
            context=context,
            asset_id=asset_id,
            draft_id=record.draft_id,
            job_id=record.job_id,
            confirmed_outline=persisted_outline,
            confirmed_outline_sha256=persisted.confirmed_outline_sha256,
        )
        if stage.classroom_version_id is not None:
            raise InvalidClassroomState("content was not queued safely")
        return persisted

    async def update_draft(
        self,
        context: TenantContext,
        asset_id: str,
        document: dict[str, Any],
        expected_revision: int,
    ) -> ClassroomRecord:
        record = await self._editable_record(context, asset_id)
        if record.lifecycle_state != "editing":
            raise InvalidClassroomState("classroom draft is not editable")
        canonical_document = _materialize_edited_classroom_document(document)
        parsed = _canonical_classroom_document(canonical_document)
        if (
            record.classroom_version_id is None
            or parsed.classroom_id != asset_id
            or parsed.classroom_version_id != record.classroom_version_id
        ):
            raise InvalidDraftDocument("draft document classroom binding is invalid")
        available = await self._repository.available_media_bindings(asset_id)
        validate_draft_document_references(
            canonical_document,
            available_media_bindings=available,
        )
        document_sha256 = _sha256_payload(canonical_document)
        updated = await self._repository.update_document(
            asset_id,
            canonical_document,
            document_sha256,
            expected_revision,
        )
        if updated is None:
            raise ClassroomRevisionConflict("draft revision is stale")
        return updated

    async def validate(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> ClassroomRecord:
        record = await self._editable_record(context, asset_id)
        if record.lifecycle_state != "editing":
            raise InvalidClassroomState("classroom draft cannot be validated")
        available = await self._repository.available_media_bindings(asset_id)
        brief = record.teaching_brief
        if brief is None:
            raise InvalidClassroomState("teaching brief is unavailable")
        parsed = _canonical_classroom_document(record.document)
        if (
            record.classroom_version_id is None
            or parsed.classroom_id != asset_id
            or parsed.classroom_version_id != record.classroom_version_id
        ):
            raise InvalidDraftDocument("draft document classroom binding is invalid")
        canonical_document = parsed.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        report = build_validation_report(
            canonical_document,
            required_knowledge_point_ids=tuple(
                point.knowledge_point_id for point in brief.knowledge_points
            ),
            grounded=brief.content_mode == "source_grounded",
            available_media_bindings=available,
        )
        document_sha256 = _sha256_payload(canonical_document)
        report.update(
            draftRevision=record.revision,
            documentSha256=document_sha256,
        )
        persisted = await self._repository.save_validation_report(
            asset_id,
            report,
            _sha256_payload(report),
            record.revision,
            document_sha256,
        )
        if persisted is None:
            raise ClassroomRevisionConflict("draft revision is stale")
        return persisted

    async def upload_media(
        self,
        context: TenantContext,
        asset_id: str,
        upload: DraftMediaUpload,
        declared_sha256: str,
    ) -> DraftMediaRecord:
        await self._editable_record(context, asset_id)
        if self._store_provider is None:
            await upload.close()
            raise InvalidDraftMedia("draft media storage is unavailable")
        mime_type = upload.content_type or ""
        if mime_type not in _DRAFT_MEDIA_SUFFIXES:
            await upload.close()
            raise InvalidDraftMedia("draft media MIME type is unsupported")
        if not re.fullmatch(r"[0-9a-f]{64}", declared_sha256):
            await upload.close()
            raise InvalidDraftMedia("draft media SHA-256 is invalid")
        staged, actual_sha256, size_bytes = await _stage_media(upload, mime_type)
        try:
            if actual_sha256 != declared_sha256:
                raise InvalidDraftMedia("draft media SHA-256 does not match")
            media_id = f"media-{secrets.token_hex(16)}"
            relative_path = draft_media_relative_path(media_id, mime_type)
            object_key = temporary_artifact_key(
                context.tenant_id,
                f"draft-{asset_id}",
                relative_path,
            )
            ownership_token = secrets.token_hex(16)
            await self._repository.reserve_media(
                NewDraftMedia(
                    id=media_id,
                    classroom_id=asset_id,
                    uploaded_by=context.user_id,
                    object_key=object_key,
                    mime_type=mime_type,
                    sha256=actual_sha256,
                    size_bytes=size_bytes,
                    ownership_token=ownership_token,
                )
            )
            store: DraftMediaStore | None = None
            try:
                store = await self._store_provider.store_for_tenant(context.tenant_id)
                artifact = await store.put_verified(
                    object_key,
                    _spooled_chunks(staged),
                    actual_sha256,
                    size_bytes,
                    content_type=mime_type,
                    ownership_token=ownership_token,
                )
                if artifact.revision is None:
                    raise InvalidDraftMedia("draft media storage receipt is incomplete")
                return await self._repository.complete_media(
                    asset_id,
                    media_id,
                    artifact.revision,
                )
            except BaseException:
                cleanup = asyncio.create_task(
                    self._recover_media_upload(
                        asset_id,
                        media_id,
                        store,
                    )
                )
                await asyncio.shield(cleanup)
                raise
        finally:
            staged.close()

    @staticmethod
    def _matches_media_receipt(
        artifact: StoredArtifact,
        receipt: DraftMediaRecord,
    ) -> bool:
        return (
            artifact.key == receipt.object_key
            and hmac.compare_digest(artifact.sha256, receipt.sha256)
            and artifact.size == receipt.size_bytes
            and artifact.content_type == receipt.mime_type
            and artifact.ownership_token is not None
            and hmac.compare_digest(
                artifact.ownership_token,
                receipt.ownership_token,
            )
            and (artifact.revision is not None or artifact.version_id is not None)
        )

    async def _finish_media_cleanup(
        self,
        store: DraftMediaStore,
        receipt: DraftMediaRecord,
    ) -> bool:
        try:
            artifact = await store.reconcile_verified(
                receipt.object_key,
                receipt.sha256,
                receipt.size_bytes,
                content_type=receipt.mime_type,
                ownership_token=receipt.ownership_token,
            )
            if artifact is not None:
                if not self._matches_media_receipt(artifact, receipt):
                    return False
                await store.delete_owned(artifact)
            await self._repository.finish_media_cleanup(
                receipt.classroom_id,
                receipt.id,
                "upload_failed",
            )
        except Exception:
            return False
        return True

    async def _recover_media_upload(
        self,
        asset_id: str,
        media_id: str,
        store: DraftMediaStore | None,
    ) -> bool:
        try:
            receipt = await self._repository.mark_media_cleanup_pending(
                asset_id,
                media_id,
                "upload_failed",
            )
        except Exception:
            return False
        if store is None:
            return False
        return await self._finish_media_cleanup(store, receipt)

    async def reconcile_media_cleanup(
        self,
        context: TenantContext,
        asset_id: str,
        media_id: str,
    ) -> bool:
        await self._editable_record(context, asset_id)
        if _MEDIA_ID_PATTERN.fullmatch(media_id) is None:
            return False
        receipt = await self._repository.get_media_receipt(asset_id, media_id)
        if receipt is None:
            return False
        if receipt.status == "failed":
            return True
        if receipt.status != "cleanup_pending" or self._store_provider is None:
            return False
        try:
            store = await self._store_provider.store_for_tenant(context.tenant_id)
        except Exception:
            return False
        return await self._finish_media_cleanup(store, receipt)

    async def get_media(
        self,
        context: TenantContext,
        asset_id: str,
        media_id: str,
    ) -> DraftMediaContent | None:
        record = await self._repository.get_workflow(asset_id)
        if record is None or not self._can_edit(context, record):
            return None
        if _CANONICAL_MEDIA_ID_PATTERN.fullmatch(media_id) is None or self._store_provider is None:
            return None
        media: DraftMediaRecord | BoundClassroomMedia | None = None
        if _MEDIA_ID_PATTERN.fullmatch(media_id) is not None:
            upload = await self._repository.get_media(asset_id, media_id)
            if upload is not None and upload.object_revision is not None:
                media = upload
        if media is None:
            media = await self._repository.get_bound_version_media(asset_id, media_id)
        if media is None:
            return None
        store = await self._store_provider.store_for_tenant(context.tenant_id)
        return DraftMediaContent(
            id=media.id,
            mime_type=media.mime_type,
            sha256=media.sha256,
            size_bytes=media.size_bytes,
            body=await store.open(media.object_key),
        )


__all__ = [
    "BoundClassroomMedia",
    "ClassroomAccessDenied",
    "ClassroomConfirmationConflict",
    "ClassroomIdempotencyConflict",
    "ClassroomNotFound",
    "ClassroomPreflightRejected",
    "ClassroomRecord",
    "ClassroomRevisionConflict",
    "ClassroomService",
    "ClassroomServiceError",
    "DraftMediaContent",
    "DraftMediaRecord",
    "GenerationStage",
    "InvalidDraftMedia",
    "InvalidDraftDocument",
    "InvalidClassroomState",
    "NewClassroomWorkflow",
    "NewDraftMedia",
    "SqlAlchemyClassroomGeneration",
    "build_validation_report",
    "draft_media_relative_path",
    "matches_reviewed_outline_binding",
    "validate_draft_document_references",
]
