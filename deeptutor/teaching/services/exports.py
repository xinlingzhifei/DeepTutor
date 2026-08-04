"""Pinned classroom export orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import inspect
from typing import AsyncIterator, Awaitable, Callable, Literal, Protocol, cast

from deeptutor.teaching.artifacts import export_input_key
from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.object_store import (
    ClassroomArtifactStore,
    ObjectStoreConflictError,
)
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.tenant_context import TenantContext

ExportFormat = Literal["classroom_zip", "pptx", "offline_html", "mp4"]
ExportStatus = Literal[
    "preparing_input",
    "input_ready",
    "quota_reserved",
    "queued",
    "exporting",
    "validating",
    "materializing",
    "succeeded",
    "failed",
    "canceled",
]

_EXPORT_FORMATS = frozenset({"classroom_zip", "pptx", "offline_html", "mp4"})


class ClassroomExportError(RuntimeError):
    """Base class for stable classroom export failures."""


class ExportNotFound(ClassroomExportError, LookupError):
    """The requested source or export is unavailable in the active tenant."""


class ExportAccessDenied(ClassroomExportError, PermissionError):
    """The actor cannot export the selected classroom resource."""


class ExportRevisionConflict(ClassroomExportError):
    """A draft revision changed before its export could be pinned."""


class ExportIdempotencyConflict(ClassroomExportError):
    """An idempotency key is bound to different export input."""


class ExportPolicyDenied(ClassroomExportError):
    """Tenant policy denies the requested export format."""


class InvalidExportInput(ClassroomExportError, ValueError):
    """The pinned document is not a canonical portable classroom."""


@dataclass(frozen=True, slots=True)
class ExportSourceMedia:
    media_id: str
    relative_name: str
    object_key: str = field(repr=False)
    sha256: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class ExportSource:
    tenant_id: str
    asset_id: str
    owner_id: str
    course_id: str | None
    class_id: str | None
    classroom_draft_id: str | None
    classroom_version_id: str | None
    draft_revision: int | None
    document: bytes = field(repr=False)
    document_sha256: str
    media_manifest_sha256: str
    media: tuple[ExportSourceMedia, ...]

    def __post_init__(self) -> None:
        draft = self.classroom_draft_id is not None
        version = self.classroom_version_id is not None
        if draft == version:
            raise ValueError("export source must select one draft or version")
        if draft != (self.draft_revision is not None):
            raise ValueError("draft export revision binding is invalid")


@dataclass(frozen=True, slots=True)
class ExportCommand:
    tenant_id: str
    export_id: str
    job_id: str
    idempotency_key: str
    request_sha256: str
    actor_id: str
    export_format: ExportFormat
    asset_id: str
    owner_id: str
    course_id: str | None
    class_id: str | None
    classroom_draft_id: str | None
    classroom_version_id: str | None
    draft_revision: int | None
    document: bytes = field(repr=False)
    document_sha256: str
    media_manifest_sha256: str
    media: tuple[ExportSourceMedia, ...]


ExportInputPlan = ExportCommand


@dataclass(frozen=True, slots=True)
class ExportInputReceipt:
    manifest_object_key: str = field(repr=False)
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ExportRecord:
    tenant_id: str
    export_id: str
    job_id: str | None
    idempotency_key: str
    request_sha256: str
    created_by: str
    owner_id: str
    course_id: str | None
    class_id: str | None
    asset_id: str
    export_format: ExportFormat
    classroom_draft_id: str | None
    classroom_version_id: str | None
    draft_revision: int | None
    input_document_sha256: str
    input_media_manifest_sha256: str
    status: ExportStatus
    progress_percent: int = 0
    waiting_reason: str | None = None
    error_category: str | None = None
    error_code: str | None = None
    retry_of_job_id: str | None = None
    input_receipt: ExportInputReceipt | None = None
    relative_name: str | None = None
    object_key: str | None = field(default=None, repr=False)
    sha256: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None

    @classmethod
    def from_command(cls, command: ExportCommand) -> ExportRecord:
        return cls(
            tenant_id=command.tenant_id,
            export_id=command.export_id,
            job_id=None,
            idempotency_key=command.idempotency_key,
            request_sha256=command.request_sha256,
            created_by=command.actor_id,
            owner_id=command.owner_id,
            course_id=command.course_id,
            class_id=command.class_id,
            asset_id=command.asset_id,
            export_format=command.export_format,
            classroom_draft_id=command.classroom_draft_id,
            classroom_version_id=command.classroom_version_id,
            draft_revision=command.draft_revision,
            input_document_sha256=command.document_sha256,
            input_media_manifest_sha256=command.media_manifest_sha256,
            status="preparing_input",
        )


@dataclass(frozen=True, slots=True)
class ExportJobCommand:
    tenant_id: str
    export_id: str
    job_id: str
    actor_id: str
    owner_id: str
    course_id: str | None
    class_id: str | None
    export_format: ExportFormat
    idempotency_key: str
    document_sha256: str
    media_manifest_sha256: str
    input_manifest_sha256: str
    input_manifest_object_key: str = field(repr=False)


class ClassroomExportRepository(Protocol):
    async def get_draft_source(self, asset_id: str) -> ExportSource | None: ...

    async def get_version_source(self, version_id: str) -> ExportSource | None: ...

    async def reserve(self, command: ExportCommand) -> ExportRecord: ...

    async def confirm_input(
        self,
        export_id: str,
        receipt: ExportInputReceipt,
    ) -> ExportRecord: ...

    async def get(self, export_id: str) -> ExportRecord | None: ...


class ExportInputMaterializer(Protocol):
    async def materialize(self, plan: ExportInputPlan) -> ExportInputReceipt: ...


class ExportInputStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> ClassroomArtifactStore: ...


class ExportJobGateway(Protocol):
    async def enqueue(self, command: ExportJobCommand) -> ExportRecord: ...


def _identifier(tenant_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"classroom-export\0{tenant_id}\0{idempotency_key}".encode()
    ).hexdigest()
    return f"export-{digest[:40]}"


def _can_access(context: TenantContext, source: ExportSource) -> bool:
    if context.tenant_id != source.tenant_id:
        return False
    if context.user_id == source.owner_id:
        return True
    resource = ResourceScope(
        tenant_id=source.tenant_id,
        course_id=source.course_id,
        class_id=source.class_id,
    )
    return any(
        grant.allows_resource(permission, resource)
        for grant in context.permissions
        for permission in (
            "classroom.edit",
            "classroom.approve",
            "classroom.publish",
            "tenant.manage",
        )
    )


def _can_access_record(context: TenantContext, record: ExportRecord) -> bool:
    if context.tenant_id != record.tenant_id:
        return False
    if context.user_id == record.owner_id:
        return True
    resource = ResourceScope(
        tenant_id=record.tenant_id,
        course_id=record.course_id,
        class_id=record.class_id,
    )
    return any(
        grant.allows_resource(permission, resource)
        for grant in context.permissions
        for permission in (
            "classroom.edit",
            "classroom.approve",
            "classroom.publish",
            "tenant.manage",
        )
    )


def _validate_source(source: ExportSource) -> None:
    if hashlib.sha256(source.document).hexdigest() != source.document_sha256:
        raise InvalidExportInput("classroom document hash is invalid")
    try:
        document = ClassroomDocument.model_validate_json(source.document)
    except Exception:
        raise InvalidExportInput("classroom document is not portable") from None
    if canonical_json_bytes(document) != source.document:
        raise InvalidExportInput("classroom document is not canonical")
    dumped = document.model_dump(mode="json", by_alias=True, exclude_none=False)
    media_sha256 = hashlib.sha256(
        canonical_json_bytes(dumped["mediaManifest"])
    ).hexdigest()
    if not hmac.compare_digest(media_sha256, source.media_manifest_sha256):
        raise InvalidExportInput("classroom media manifest hash is invalid")
    declared = {
        (item.media_id, item.relative_path): (
            item.mime_type,
            item.sha256,
            item.size_bytes,
        )
        for item in document.media_manifest
    }
    supplied = {
        (item.media_id, item.relative_name): (
            item.mime_type,
            item.sha256,
            item.size_bytes,
        )
        for item in source.media
    }
    if declared != supplied:
        raise InvalidExportInput("classroom media artifacts are incomplete")


def _request_sha256(
    source: ExportSource,
    export_format: ExportFormat,
    actor_id: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "tenantId": source.tenant_id,
                "assetId": source.asset_id,
                "classroomDraftId": source.classroom_draft_id,
                "classroomVersionId": source.classroom_version_id,
                "draftRevision": source.draft_revision,
                "documentSha256": source.document_sha256,
                "mediaManifestSha256": source.media_manifest_sha256,
                "format": export_format,
                "actorId": actor_id,
            }
        )
    ).hexdigest()


async def _bytes_body(value: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(value), 64 * 1024):
        yield value[offset : offset + 64 * 1024]


def _ownership_token(plan: ExportInputPlan, relative_name: str) -> str:
    return hashlib.sha256(
        (
            f"export-input-object\0{plan.tenant_id}\0"
            f"{plan.export_id}\0{relative_name}"
        ).encode()
    ).hexdigest()[:32]


async def _put_immutable(
    store: ClassroomArtifactStore,
    *,
    plan: ExportInputPlan,
    relative_name: str,
    body: AsyncIterator[bytes],
    sha256: str,
    size_bytes: int,
    mime_type: str,
):
    key = export_input_key(plan.tenant_id, plan.export_id, relative_name)
    ownership_token = _ownership_token(plan, relative_name)
    try:
        return await store.put_verified(
            key,
            body,
            sha256,
            size_bytes,
            content_type=mime_type,
            ownership_token=ownership_token,
        )
    except ObjectStoreConflictError:
        existing = await store.reconcile_verified(
            key,
            sha256,
            size_bytes,
            content_type=mime_type,
            ownership_token=ownership_token,
        )
        if existing is None:
            raise
        return existing


class ClassroomExportInputMaterializer:
    """Copy one exact source into a create-only yFeiSTAI export snapshot."""

    def __init__(self, stores: ExportInputStoreProvider) -> None:
        self._stores = stores

    async def materialize(self, plan: ExportInputPlan) -> ExportInputReceipt:
        _validate_source(
            ExportSource(
                tenant_id=plan.tenant_id,
                asset_id=plan.asset_id,
                owner_id=plan.owner_id,
                course_id=plan.course_id,
                class_id=plan.class_id,
                classroom_draft_id=plan.classroom_draft_id,
                classroom_version_id=plan.classroom_version_id,
                draft_revision=plan.draft_revision,
                document=plan.document,
                document_sha256=plan.document_sha256,
                media_manifest_sha256=plan.media_manifest_sha256,
                media=plan.media,
            )
        )
        store = await self._stores.store_for_tenant(plan.tenant_id)
        document = await _put_immutable(
            store,
            plan=plan,
            relative_name="classroom.json",
            body=_bytes_body(plan.document),
            sha256=plan.document_sha256,
            size_bytes=len(plan.document),
            mime_type="application/json",
        )
        stored_media = []
        for media in plan.media:
            stored_media.append(
                (
                    media,
                    await _put_immutable(
                        store,
                        plan=plan,
                        relative_name=media.relative_name,
                        body=await store.open(media.object_key),
                        sha256=media.sha256,
                        size_bytes=media.size_bytes,
                        mime_type=media.mime_type,
                    ),
                )
            )
        manifest_document = canonical_json_bytes(
            {
                "schemaVersion": 1,
                "tenantId": plan.tenant_id,
                "exportId": plan.export_id,
                "jobId": plan.job_id,
                "idempotencyKey": plan.job_id,
                "requestSha256": plan.request_sha256,
                "classroomDocumentSha256": plan.document_sha256,
                "mediaManifestSha256": plan.media_manifest_sha256,
                "entries": [
                    {
                        "kind": "document",
                        "mediaId": None,
                        "relativeName": "classroom.json",
                        "objectKey": document.key,
                        "mimeType": document.content_type,
                        "sha256": document.sha256,
                        "sizeBytes": document.size,
                    },
                    *(
                        {
                            "kind": "media",
                            "mediaId": media.media_id,
                            "relativeName": media.relative_name,
                            "objectKey": artifact.key,
                            "mimeType": artifact.content_type,
                            "sha256": artifact.sha256,
                            "sizeBytes": artifact.size,
                        }
                        for media, artifact in stored_media
                    ),
                ],
            }
        )
        manifest_sha256 = hashlib.sha256(manifest_document).hexdigest()
        manifest = await _put_immutable(
            store,
            plan=plan,
            relative_name="manifest.json",
            body=_bytes_body(manifest_document),
            sha256=manifest_sha256,
            size_bytes=len(manifest_document),
            mime_type="application/json",
        )
        return ExportInputReceipt(
            manifest_object_key=manifest.key,
            manifest_sha256=manifest.sha256,
        )


class ClassroomExportService:
    """Pin one source, materialize immutable input, then enqueue one export job."""

    def __init__(
        self,
        repository: ClassroomExportRepository,
        materializer: ExportInputMaterializer,
        jobs: ExportJobGateway,
        *,
        mp4_enabled: Callable[[str], bool | Awaitable[bool]],
    ) -> None:
        self._repository = repository
        self._materializer = materializer
        self._jobs = jobs
        self._mp4_enabled = mp4_enabled

    async def _format(self, tenant_id: str, value: str) -> ExportFormat:
        if value not in _EXPORT_FORMATS:
            raise InvalidExportInput("export format is unsupported")
        export_format = cast(ExportFormat, value)
        if export_format == "mp4":
            enabled = self._mp4_enabled(tenant_id)
            if inspect.isawaitable(enabled):
                enabled = await enabled
            if not enabled:
                raise ExportPolicyDenied("mp4 export is disabled")
        return export_format

    async def create_for_draft(
        self,
        context: TenantContext,
        asset_id: str,
        export_format: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ExportRecord:
        source = await self._repository.get_draft_source(asset_id)
        if source is None or source.tenant_id != context.tenant_id:
            raise ExportNotFound("classroom draft not found")
        if not _can_access(context, source):
            raise ExportAccessDenied("classroom export is denied")
        if source.draft_revision != expected_revision:
            raise ExportRevisionConflict("classroom draft revision is stale")
        return await self._create(context, source, export_format, idempotency_key)

    async def create_for_version(
        self,
        context: TenantContext,
        version_id: str,
        export_format: str,
        *,
        idempotency_key: str,
    ) -> ExportRecord:
        source = await self._repository.get_version_source(version_id)
        if source is None or source.tenant_id != context.tenant_id:
            raise ExportNotFound("classroom version not found")
        if not _can_access(context, source):
            raise ExportAccessDenied("classroom export is denied")
        return await self._create(context, source, export_format, idempotency_key)

    async def _create(
        self,
        context: TenantContext,
        source: ExportSource,
        value: str,
        idempotency_key: str,
    ) -> ExportRecord:
        if not idempotency_key or len(idempotency_key) > 128:
            raise InvalidExportInput("idempotency key is invalid")
        export_format = await self._format(context.tenant_id, value)
        _validate_source(source)
        export_id = _identifier(context.tenant_id, idempotency_key)
        request_sha256 = _request_sha256(source, export_format, context.user_id)
        command = ExportCommand(
            tenant_id=context.tenant_id,
            export_id=export_id,
            job_id=export_id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            actor_id=context.user_id,
            export_format=export_format,
            asset_id=source.asset_id,
            owner_id=source.owner_id,
            course_id=source.course_id,
            class_id=source.class_id,
            classroom_draft_id=source.classroom_draft_id,
            classroom_version_id=source.classroom_version_id,
            draft_revision=source.draft_revision,
            document=source.document,
            document_sha256=source.document_sha256,
            media_manifest_sha256=source.media_manifest_sha256,
            media=source.media,
        )
        record = await self._repository.reserve(command)
        if (
            record.tenant_id != command.tenant_id
            or record.export_id != command.export_id
            or record.idempotency_key != command.idempotency_key
            or not hmac.compare_digest(record.request_sha256, command.request_sha256)
        ):
            raise ExportIdempotencyConflict("export idempotency key conflicts")
        if record.input_receipt is None:
            receipt = await self._materializer.materialize(command)
            record = await self._repository.confirm_input(record.export_id, receipt)
        if record.job_id is None:
            receipt = record.input_receipt
            if receipt is None:
                raise ClassroomExportError("export input receipt is unavailable")
            record = await self._jobs.enqueue(
                ExportJobCommand(
                    tenant_id=record.tenant_id,
                    export_id=record.export_id,
                    job_id=command.job_id,
                    actor_id=context.user_id,
                    owner_id=source.owner_id,
                    course_id=source.course_id,
                    class_id=source.class_id,
                    export_format=export_format,
                    idempotency_key=idempotency_key,
                    document_sha256=source.document_sha256,
                    media_manifest_sha256=source.media_manifest_sha256,
                    input_manifest_sha256=receipt.manifest_sha256,
                    input_manifest_object_key=receipt.manifest_object_key,
                )
            )
        return record

    async def get(
        self,
        context: TenantContext,
        export_id: str,
    ) -> ExportRecord | None:
        record = await self._repository.get(export_id)
        if record is None or record.tenant_id != context.tenant_id:
            return None
        return record if _can_access_record(context, record) else None


__all__ = [
    "ClassroomExportError",
    "ClassroomExportInputMaterializer",
    "ClassroomExportService",
    "ExportAccessDenied",
    "ExportCommand",
    "ExportIdempotencyConflict",
    "ExportInputPlan",
    "ExportInputReceipt",
    "ExportJobCommand",
    "ExportNotFound",
    "ExportPolicyDenied",
    "ExportRecord",
    "ExportRevisionConflict",
    "ExportSource",
    "ExportSourceMedia",
    "InvalidExportInput",
]
