"""Export-specific helpers shared by the durable generation worker."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
from typing import AsyncIterator, Literal, Protocol

from deeptutor.teaching.artifacts import export_input_key
from deeptutor.teaching.contracts import ExportRequest, canonical_json_bytes
from deeptutor.teaching.openmaic.client import EngineJob

MAX_EXPORT_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_EXPORT_MEDIA_BYTES = 128 * 1024 * 1024
MAX_EXPORT_TOTAL_BYTES = 512 * 1024 * 1024
MAX_EXPORT_MEDIA_FILES = 256


@dataclass(frozen=True, slots=True)
class ExportInputSnapshot:
    """Hashes pinned by the immutable export job request payload."""

    classroom_document_sha256: str
    media_manifest_sha256: str
    request_sha256: str

    @classmethod
    def from_canonical_payload(cls, payload: str) -> ExportInputSnapshot:
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("export request payload is invalid") from None
        if not isinstance(raw, dict):
            raise ValueError("export request payload is invalid")
        canonical = json.dumps(
            raw,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical != payload:
            raise ValueError("export request payload must be canonical JSON")
        request = ExportRequest.model_validate(raw)
        return cls(
            classroom_document_sha256=request.classroom_document_sha256,
            media_manifest_sha256=request.media_manifest_sha256,
            request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        )


class ExportSubmissionClient(Protocol):
    async def submit_export(self, request: ExportRequest) -> EngineJob: ...


@dataclass(frozen=True, slots=True)
class ExportInputArtifact:
    media_id: str | None
    relative_name: str
    object_key: str = field(repr=False)
    sha256: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class ExportInputBundle:
    tenant_id: str
    job_id: str
    idempotency_key: str
    request_sha256: str
    document: ExportInputArtifact
    media: tuple[ExportInputArtifact, ...]
    media_manifest_sha256: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ExportInputFileDeclaration:
    file_id: str
    kind: Literal["document", "media"]
    media_id: str | None
    relative_name: str
    sha256: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class ExportInputDeclaration:
    tenant_id: str
    job_id: str
    idempotency_key: str
    classroom_document_sha256: str
    media_manifest_sha256: str
    source_manifest_sha256: str
    files: tuple[ExportInputFileDeclaration, ...]

    def canonical_payload(self) -> bytes:
        return canonical_json_bytes(
            {
                "schemaVersion": 1,
                "tenantId": self.tenant_id,
                "jobId": self.job_id,
                "idempotencyKey": self.idempotency_key,
                "classroomDocumentSha256": self.classroom_document_sha256,
                "mediaManifestSha256": self.media_manifest_sha256,
                "sourceManifestSha256": self.source_manifest_sha256,
                "files": [
                    {
                        "fileId": item.file_id,
                        "kind": item.kind,
                        "mediaId": item.media_id,
                        "relativePath": item.relative_name,
                        "mimeType": item.mime_type,
                        "sha256": item.sha256,
                        "sizeBytes": item.size_bytes,
                    }
                    for item in self.files
                ],
            }
        )

    @property
    def declaration_sha256(self) -> str:
        return hashlib.sha256(self.canonical_payload()).hexdigest()


@dataclass(frozen=True, slots=True)
class ExportInputCommitReceipt:
    tenant_id: str
    job_id: str
    idempotency_key: str
    declaration_sha256: str
    classroom_document_sha256: str
    media_manifest_sha256: str
    receipt_sha256: str

    def validate(self, declaration: ExportInputDeclaration) -> None:
        payload = canonical_json_bytes(
            {
                "schemaVersion": 1,
                "tenantId": self.tenant_id,
                "jobId": self.job_id,
                "idempotencyKey": self.idempotency_key,
                "declarationSha256": self.declaration_sha256,
                "classroomDocumentSha256": self.classroom_document_sha256,
                "mediaManifestSha256": self.media_manifest_sha256,
                "status": "committed",
            }
        )
        if (
            self.tenant_id != declaration.tenant_id
            or self.job_id != declaration.job_id
            or self.idempotency_key != declaration.idempotency_key
            or not hmac.compare_digest(
                self.declaration_sha256,
                declaration.declaration_sha256,
            )
            or not hmac.compare_digest(
                self.classroom_document_sha256,
                declaration.classroom_document_sha256,
            )
            or not hmac.compare_digest(
                self.media_manifest_sha256,
                declaration.media_manifest_sha256,
            )
            or not hmac.compare_digest(
                self.receipt_sha256,
                hashlib.sha256(payload).hexdigest(),
            )
        ):
            raise ValueError("OpenMAIC export input receipt is invalid")


class ExportInputStore(Protocol):
    async def open(self, key: str) -> AsyncIterator[bytes]: ...


class StagedExportClient(ExportSubmissionClient, Protocol):
    async def reserve_export_input(
        self,
        declaration: ExportInputDeclaration,
    ) -> None: ...

    async def upload_export_input_file(
        self,
        declaration: ExportInputDeclaration,
        file: ExportInputFileDeclaration,
        body: AsyncIterator[bytes],
    ) -> None: ...

    async def commit_export_input(
        self,
        declaration: ExportInputDeclaration,
    ) -> ExportInputCommitReceipt: ...


class _VerifiedStream:
    def __init__(self, source: AsyncIterator[bytes], artifact: ExportInputArtifact) -> None:
        self._source = source
        self._artifact = artifact
        self.complete = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._read()

    async def _read(self) -> AsyncIterator[bytes]:
        digest = hashlib.sha256()
        size = 0
        async for chunk in self._source:
            if not isinstance(chunk, bytes):
                raise ValueError("export input stream is invalid")
            size += len(chunk)
            if size > self._artifact.size_bytes:
                raise ValueError("export input size exceeds its receipt")
            digest.update(chunk)
            yield chunk
        if size != self._artifact.size_bytes or not hmac.compare_digest(
            digest.hexdigest(), self._artifact.sha256
        ):
            raise ValueError("export input integrity verification failed")
        self.complete = True


def _sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("export input manifest hash is invalid")
    return value


async def load_export_input_bundle(
    store: ExportInputStore,
    *,
    tenant_id: str,
    job_id: str,
    manifest_object_key: str,
    manifest_sha256: str,
) -> ExportInputBundle:
    """Load and validate one committed yFeiSTAI-only input manifest."""

    if manifest_object_key != export_input_key(tenant_id, job_id, "manifest.json"):
        raise ValueError("export input manifest key is invalid")
    expected_sha256 = _sha256(manifest_sha256)
    payload = bytearray()
    async for chunk in await store.open(manifest_object_key):
        if not isinstance(chunk, bytes):
            raise ValueError("export input manifest stream is invalid")
        payload.extend(chunk)
        if len(payload) > 1024 * 1024:
            raise ValueError("export input manifest is too large")
    raw_bytes = bytes(payload)
    if not hmac.compare_digest(hashlib.sha256(raw_bytes).hexdigest(), expected_sha256):
        raise ValueError("export input manifest hash is invalid")
    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("export input manifest is invalid") from None
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "schemaVersion",
            "tenantId",
            "exportId",
            "jobId",
            "idempotencyKey",
            "requestSha256",
            "classroomDocumentSha256",
            "mediaManifestSha256",
            "entries",
        }
        or raw.get("schemaVersion") != 1
        or raw.get("tenantId") != tenant_id
        or raw.get("exportId") != job_id
        or raw.get("jobId") != job_id
        or not isinstance(raw.get("idempotencyKey"), str)
        or not raw["idempotencyKey"]
        or canonical_json_bytes(raw) != raw_bytes
        or not isinstance(raw.get("entries"), list)
        or not 1 <= len(raw["entries"]) <= 257
    ):
        raise ValueError("export input manifest binding is invalid")
    request_sha256 = _sha256(raw.get("requestSha256"))
    idempotency_key = raw["idempotencyKey"]
    document_sha256 = _sha256(raw.get("classroomDocumentSha256"))
    media_manifest_sha256 = _sha256(raw.get("mediaManifestSha256"))
    artifacts: list[ExportInputArtifact] = []
    for index, value in enumerate(raw["entries"]):
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "kind",
                "mediaId",
                "relativeName",
                "objectKey",
                "mimeType",
                "sha256",
                "sizeBytes",
            }
            or value.get("kind") not in {"document", "media"}
            or not isinstance(value.get("relativeName"), str)
            or not isinstance(value.get("mimeType"), str)
            or not value["mimeType"]
            or isinstance(value.get("sizeBytes"), bool)
            or not isinstance(value.get("sizeBytes"), int)
            or value["sizeBytes"] < 0
        ):
            raise ValueError("export input manifest entry is invalid")
        kind = value["kind"]
        media_id = value.get("mediaId")
        if (kind == "document" and media_id is not None) or (
            kind == "media" and (not isinstance(media_id, str) or not media_id)
        ):
            raise ValueError("export input manifest entry binding is invalid")
        relative_name = value["relativeName"]
        object_key = value.get("objectKey")
        if object_key != export_input_key(tenant_id, job_id, relative_name):
            raise ValueError("export input manifest object key is invalid")
        artifacts.append(
            ExportInputArtifact(
                media_id=media_id,
                relative_name=relative_name,
                object_key=object_key,
                sha256=_sha256(value.get("sha256")),
                size_bytes=value["sizeBytes"],
                mime_type=value["mimeType"],
            )
        )
        if index == 0 and (
            kind != "document"
            or relative_name != "classroom.json"
            or value["mimeType"] != "application/json"
        ):
            raise ValueError("export input document entry is invalid")
        if index > 0 and kind != "media":
            raise ValueError("export input media entry is invalid")
    if not hmac.compare_digest(artifacts[0].sha256, document_sha256):
        raise ValueError("export input document hash binding is invalid")
    return ExportInputBundle(
        tenant_id=tenant_id,
        job_id=job_id,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        document=artifacts[0],
        media=tuple(artifacts[1:]),
        media_manifest_sha256=media_manifest_sha256,
        manifest_sha256=expected_sha256,
    )


def _file_id(index: int, artifact: ExportInputArtifact) -> str:
    digest = hashlib.sha256(
        f"{index}\0{artifact.relative_name}\0{artifact.sha256}".encode()
    ).hexdigest()
    return f"file-{digest[:24]}"


def export_input_declaration(
    request: ExportRequest,
    bundle: ExportInputBundle,
) -> ExportInputDeclaration:
    if (
        bundle.tenant_id != request.tenant_id
        or bundle.job_id != request.job_id
        or bundle.idempotency_key != request.idempotency_key
        or not hmac.compare_digest(
            bundle.document.sha256,
            request.classroom_document_sha256,
        )
        or not hmac.compare_digest(
            bundle.media_manifest_sha256,
            request.media_manifest_sha256,
        )
        or bundle.document.relative_name != "classroom.json"
        or bundle.document.mime_type != "application/json"
        or len(bundle.media) > MAX_EXPORT_MEDIA_FILES
    ):
        raise ValueError("export input bundle does not match the pinned request")
    artifacts = (bundle.document, *bundle.media)
    if (
        bundle.document.size_bytes > MAX_EXPORT_DOCUMENT_BYTES
        or any(item.size_bytes > MAX_EXPORT_MEDIA_BYTES for item in bundle.media)
        or sum(item.size_bytes for item in artifacts) > MAX_EXPORT_TOTAL_BYTES
        or len({item.relative_name for item in artifacts}) != len(artifacts)
    ):
        raise ValueError("export input bundle exceeds the staging limits")
    files = tuple(
        ExportInputFileDeclaration(
            file_id=_file_id(index, artifact),
            kind="document" if index == 0 else "media",
            media_id=None if index == 0 else artifact.media_id,
            relative_name=artifact.relative_name,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            mime_type=artifact.mime_type,
        )
        for index, artifact in enumerate(artifacts)
    )
    return ExportInputDeclaration(
        tenant_id=bundle.tenant_id,
        job_id=bundle.job_id,
        idempotency_key=request.idempotency_key,
        classroom_document_sha256=request.classroom_document_sha256,
        media_manifest_sha256=request.media_manifest_sha256,
        source_manifest_sha256=bundle.manifest_sha256,
        files=files,
    )


async def submit_pinned_export(
    client: ExportSubmissionClient,
    request: ExportRequest,
) -> EngineJob:
    """Keep the export endpoint explicit at the worker boundary."""

    return await client.submit_export(request)


async def stage_and_submit_pinned_export(
    client: StagedExportClient,
    store: ExportInputStore,
    request: ExportRequest,
    bundle: ExportInputBundle,
) -> EngineJob:
    """Read immutable yFeiSTAI input, stage it, then submit hashes only."""

    declaration = export_input_declaration(request, bundle)
    await client.reserve_export_input(declaration)
    artifacts = (bundle.document, *bundle.media)
    for artifact, declared in zip(artifacts, declaration.files, strict=True):
        stream = _VerifiedStream(await store.open(artifact.object_key), artifact)
        await client.upload_export_input_file(declaration, declared, stream)
        if not stream.complete:
            raise ValueError("export input staging did not consume the fixed snapshot")
    receipt = await client.commit_export_input(declaration)
    receipt.validate(declaration)
    return await client.submit_export(request)


__all__ = [
    "ExportInputArtifact",
    "ExportInputBundle",
    "ExportInputCommitReceipt",
    "ExportInputDeclaration",
    "ExportInputFileDeclaration",
    "ExportInputSnapshot",
    "MAX_EXPORT_DOCUMENT_BYTES",
    "MAX_EXPORT_MEDIA_BYTES",
    "MAX_EXPORT_MEDIA_FILES",
    "MAX_EXPORT_TOTAL_BYTES",
    "export_input_declaration",
    "load_export_input_bundle",
    "stage_and_submit_pinned_export",
    "submit_pinned_export",
]
