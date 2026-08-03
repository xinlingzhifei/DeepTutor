"""Teacher classroom authoring workflow and pre-publication validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
import tempfile
from typing import Any, AsyncIterator, Protocol
import zipfile

from deeptutor.teaching.artifacts import StoredArtifact, temporary_artifact_key
from deeptutor.teaching.brief_builder import (
    KnowledgePointSpec,
    TeachingBriefBuilder,
    TeachingBriefSpec,
)
from deeptutor.teaching.contracts import (
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


class InvalidClassroomState(ClassroomServiceError):
    """The requested authoring operation is invalid in the current lifecycle state."""


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


@dataclass(frozen=True, slots=True)
class NewClassroomWorkflow:
    tenant_id: str
    asset_id: str
    draft_id: str
    owner_id: str
    title: str
    teaching_brief: TeachingBrief


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


@dataclass(frozen=True, slots=True)
class DraftMediaContent:
    id: str
    mime_type: str
    sha256: str
    size_bytes: int
    body: AsyncIterator[bytes] = field(repr=False)


class ClassroomRepository(Protocol):
    async def create_workflow(self, workflow: NewClassroomWorkflow) -> ClassroomRecord: ...

    async def list_workflows(self) -> tuple[ClassroomRecord, ...]: ...

    async def get_workflow(self, asset_id: str) -> ClassroomRecord | None: ...

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

    async def available_media_ids(self, asset_id: str) -> frozenset[str]: ...

    async def save_validation_report(
        self,
        asset_id: str,
        report: dict[str, object],
        report_sha256: str,
    ) -> ClassroomRecord: ...

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

    async def get_media(
        self,
        asset_id: str,
        media_id: str,
    ) -> DraftMediaRecord | None: ...


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

    async def open(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete_owned(self, artifact: StoredArtifact) -> None: ...


class DraftMediaStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> DraftMediaStore: ...


_MEDIA_ID_PATTERN = re.compile(r"^media-[0-9a-f]{32}$")
_FORBIDDEN_REFERENCE_KEYS = frozenset(
    {"objectkey", "object_key", "url", "uri", "href", "downloadurl"}
)
_FORBIDDEN_REFERENCE_PREFIXES = (
    "http://",
    "https://",
    "s3://",
    "file://",
    "tenants/",
    "/tenants/",
)
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


def validate_draft_document_references(document: Mapping[str, Any]) -> frozenset[str]:
    """Reject client object-store identities and return opaque media references."""

    if not isinstance(document, Mapping):
        raise InvalidDraftDocument("draft document is invalid")
    media_ids: set[str] = set()
    for _path, key, value in _walk(document):
        normalized_key = key.lower() if isinstance(key, str) else None
        if normalized_key in _FORBIDDEN_REFERENCE_KEYS:
            raise InvalidDraftDocument("draft document has an unsafe reference")
        if isinstance(value, str) and value.strip().lower().startswith(
            _FORBIDDEN_REFERENCE_PREFIXES
        ):
            raise InvalidDraftDocument("draft document has an unsafe reference")
        if normalized_key == "mediaid":
            if not isinstance(value, str) or _MEDIA_ID_PATTERN.fullmatch(value) is None:
                raise InvalidDraftDocument("draft document has an unsafe reference")
            media_ids.add(value)
        elif normalized_key == "mediaids":
            if not isinstance(value, list) or any(
                not isinstance(item, str) or _MEDIA_ID_PATTERN.fullmatch(item) is None
                for item in value
            ):
                raise InvalidDraftDocument("draft document has an unsafe reference")
            media_ids.update(value)
    return frozenset(media_ids)


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
    available_media_ids: frozenset[str],
) -> dict[str, object]:
    """Build the persisted, actionable nine-section Task 4 quality report."""

    sections: dict[str, dict[str, object]] = {
        name: _section([]) for name in _VALIDATION_SECTION_NAMES
    }
    dsl_issues: list[dict[str, str]] = []
    scenes = document.get("scenes")
    if document.get("dslVersion") != "0.1.0":
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
        referenced_media = validate_draft_document_references(document)
    except InvalidDraftDocument:
        referenced_media = frozenset()
        media_issues.append(
            _issue(
                "error",
                "media_reference_unsafe",
                "Replace external media references with uploaded media IDs.",
                "$",
            )
        )
    for media_id in sorted(referenced_media - available_media_ids):
        media_issues.append(
            _issue(
                "error",
                "media_missing",
                "Upload the referenced media again.",
                f"$.mediaIds[{media_id}]",
            )
        )
    sections["media_integrity"] = _section(media_issues)

    mappings = document.get("knowledgePointMappings")
    mapped_ids = {
        item.get("knowledgePointId")
        for item in mappings
        if isinstance(item, Mapping) and isinstance(item.get("knowledgePointId"), str)
    } if isinstance(mappings, list) else set()
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

    source_refs = document.get("sourceRefs")
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
            lowered = html.lower() if isinstance(html, str) else ""
            if any(token in lowered for token in ("<script", "javascript:", " onerror=", " onclick=")):
                security_issues.append(
                    _issue(
                        "error",
                        "interactive_script_unsafe",
                        "Remove scripts and inline event handlers from interactive HTML.",
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
    return any(
        grant.allows_resource(permission, resource) for grant in context.permissions
    )


def _outline_payload(outline: OutlineBundle) -> dict[str, Any]:
    return outline.model_dump(mode="json", by_alias=True, exclude_none=True)


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
                raise InvalidDraftMedia("draft media content does not match its MIME type") from None
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


class SqlAlchemyClassroomGeneration:
    """Adapter from teacher authoring to the Plan 02 durable job state machine."""

    def __init__(
        self,
        repository: SqlAlchemyGenerationJobRepository,
        selector: DataPlaneSelector,
    ) -> None:
        self._repository = repository
        self._selector = selector

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
            priority="teacher",
        )
        payload = canonical_json_bytes(generation).decode("utf-8")
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        await self._repository.create_job_and_reserve(
            GenerationJobRequest(
                tenant_id=context.tenant_id,
                job_id=job_id,
                job_kind="generation",
                phase="outline",
                export_format=None,
                priority="teacher",
                quota_units=max(1, teaching_brief.duration_minutes),
                actor_id=context.user_id,
                owner_id=context.user_id,
                visibility="class",
                request_id=generation.request_id,
                idempotency_key=generation.idempotency_key,
                request_sha256=payload_sha256,
                data_plane_route_id=selection.route_ref,
                provider_profile_id=selection.provider_profile_ref,
                worker_pool_ref=selection.worker_pool_ref,
                queue_ref=selection.queue_ref,
                request_payload=payload,
                classroom_draft_id=draft_id,
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
        job_id: str,
        confirmed_outline: OutlineBundle,
        confirmed_outline_sha256: str,
    ) -> GenerationStage:
        details = await self._repository.get_job_details(context.tenant_id, job_id)
        if (
            details is None
            or details.tenant_id != context.tenant_id
            or details.job_kind != "generation"
            or details.phase != "outline"
            or details.status != "awaiting_confirmation"
            or details.result_payload is None
        ):
            raise InvalidClassroomState("outline cannot start content generation")
        try:
            original = GenerationRequest.model_validate_json(details.request_payload)
            issued = OutlineBundle.model_validate_json(details.result_payload)
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
            exclude_none=True,
        )
        content_payload.update(
            phase="content",
            confirmedOutline=_outline_payload(confirmed_outline),
            confirmedOutlineSha256=confirmed_outline_sha256,
        )
        content_request = GenerationRequest.model_validate(content_payload)
        payload = canonical_json_bytes(content_request).decode("utf-8")
        requeued = await self._repository.requeue_confirmed_content(
            context.tenant_id,
            job_id,
            request_payload=payload,
            request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        )
        if not requeued:
            raise InvalidClassroomState("outline cannot start content generation")
        updated = await self._repository.get_job_details(context.tenant_id, job_id)
        if updated is None:
            raise InvalidClassroomState("generation job is unavailable")
        return self._stage(updated)


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
    ) -> None:
        self._repository = repository
        self._brief_builder = brief_builder
        self._generation = generation
        self._store_provider = store_provider
        self._clock = clock

    async def create(self, context: TenantContext, request: object) -> ClassroomRecord:
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
            raise InvalidClassroomState("teacher classroom must use full mode")

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
                request, "open_creation_acknowledged"
            ),
            allowed_web_domains=tuple(getattr(request, "allowed_web_domains")),
        )
        content_mode = getattr(request, "content_mode")
        source_type = getattr(request, "source_type")
        source_ref = getattr(request, "source_ref")
        if content_mode == "open_creation":
            if source_type is not None or source_ref is not None:
                raise InvalidClassroomState("open creation cannot select a source")
            built = self._brief_builder.open_creation(brief_spec)
        else:
            if source_type is None or source_ref is None:
                raise InvalidClassroomState("source-grounded creation requires a source")
            if source_type == "knowledge_base":
                built = await self._brief_builder.from_kb(source_ref, brief_spec)
            elif source_type == "pdf":
                built = await self._brief_builder.from_pdf(source_ref, brief_spec)
            else:
                raise InvalidClassroomState("classroom source is invalid")

        asset_id = f"asset-{secrets.token_hex(16)}"
        draft_id = f"draft-{secrets.token_hex(16)}"
        record = await self._repository.create_workflow(
            NewClassroomWorkflow(
                tenant_id=context.tenant_id,
                asset_id=asset_id,
                draft_id=draft_id,
                owner_id=context.user_id,
                title=str(getattr(request, "title")),
                teaching_brief=built.contract,
            )
        )
        stage = await self._generation.start_outline(
            context=context,
            asset_id=asset_id,
            draft_id=draft_id,
            teaching_brief=built.contract,
            requested_exports=tuple(getattr(request, "requested_exports")),
        )
        record = await self._repository.attach_outline_job(asset_id, stage.job_id)
        if stage.classroom_version_id is not None:
            raise InvalidClassroomState("outline stage cannot create a classroom version")
        if stage.status == "awaiting_confirmation" and stage.outline is not None:
            payload = _outline_payload(stage.outline)
            record = await self._repository.save_outline(
                asset_id,
                payload,
                canonical_outline_sha256(stage.outline),
            )
        return record

    def _can_edit(self, context: TenantContext, record: ClassroomRecord) -> bool:
        return (
            record.tenant_id == context.tenant_id
            and _allows(
                context,
                "classroom.edit",
                course_id=record.course_id,
                class_id=record.class_id,
            )
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
        if record.job_id is not None:
            stage = await self._generation.get_stage(
                context=context,
                job_id=record.job_id,
            )
            if stage.classroom_version_id is not None and stage.status not in {
                "succeeded"
            }:
                raise InvalidClassroomState("classroom version has invalid job state")
            if stage.status == "awaiting_confirmation" and stage.outline is not None:
                record = await self._repository.save_outline(
                    asset_id,
                    _outline_payload(stage.outline),
                    canonical_outline_sha256(stage.outline),
                )
            elif stage.status == "succeeded" and record.classroom_version_id is not None:
                record = await self._repository.mark_generation_succeeded(
                    asset_id,
                    record.job_id,
                )
        return record

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
    ) -> ClassroomRecord:
        record = await self._editable_record(context, asset_id)
        if (
            record.lifecycle_state not in {"awaiting_outline", "generating_content"}
            or record.outline is None
            or record.job_id is None
        ):
            raise InvalidClassroomState("outline cannot be confirmed")
        outline = OutlineBundle.model_validate(record.outline)
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
        outline_sha256 = canonical_outline_sha256(confirmed)
        persisted = await self._repository.confirm_outline(
            asset_id,
            _outline_payload(confirmed),
            outline_sha256,
        )
        stage = await self._generation.start_content(
            context=context,
            job_id=record.job_id,
            confirmed_outline=confirmed,
            confirmed_outline_sha256=outline_sha256,
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
        media_ids = validate_draft_document_references(document)
        available = await self._repository.available_media_ids(asset_id)
        if not media_ids.issubset(available):
            raise InvalidDraftDocument("draft document references unavailable media")
        try:
            document_sha256 = _sha256_payload(document)
        except (TypeError, ValueError):
            raise InvalidDraftDocument("draft document is invalid") from None
        updated = await self._repository.update_document(
            asset_id,
            document,
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
        available = await self._repository.available_media_ids(asset_id)
        brief = record.teaching_brief
        if brief is None:
            raise InvalidClassroomState("teaching brief is unavailable")
        report = build_validation_report(
            record.document,
            required_knowledge_point_ids=tuple(
                point.knowledge_point_id for point in brief.knowledge_points
            ),
            grounded=brief.content_mode == "source_grounded",
            available_media_ids=available,
        )
        return await self._repository.save_validation_report(
            asset_id,
            report,
            _sha256_payload(report),
        )

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
            object_key = temporary_artifact_key(
                context.tenant_id,
                f"draft-{asset_id}",
                f"media/{media_id}{_DRAFT_MEDIA_SUFFIXES[mime_type]}",
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
            store = await self._store_provider.store_for_tenant(context.tenant_id)
            artifact: StoredArtifact | None = None
            try:
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
                await self._repository.fail_media(
                    asset_id,
                    media_id,
                    "upload_failed",
                )
                if artifact is not None:
                    try:
                        await store.delete_owned(artifact)
                    except Exception:
                        pass
                raise
        finally:
            staged.close()

    async def get_media(
        self,
        context: TenantContext,
        asset_id: str,
        media_id: str,
    ) -> DraftMediaContent | None:
        record = await self._repository.get_workflow(asset_id)
        if record is None or not self._can_edit(context, record):
            return None
        if _MEDIA_ID_PATTERN.fullmatch(media_id) is None:
            return None
        media = await self._repository.get_media(asset_id, media_id)
        if media is None or media.object_revision is None or self._store_provider is None:
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
    "ClassroomAccessDenied",
    "ClassroomNotFound",
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
    "validate_draft_document_references",
]
