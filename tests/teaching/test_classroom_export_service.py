from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.tenant_context import TenantContext
from tests.teaching.test_contracts import valid_classroom_document


def _context(
    *,
    tenant_id: str = "tenant-a",
    user_id: str = "teacher-a",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=f"tenant_{tenant_id}",
        user_id=user_id,
        permissions=permissions_for_roles(
            {"teacher"},
            scope_type="class",
            scope_id="class-a",
            tenant_id=tenant_id,
        ),
    )


def _document_bytes() -> bytes:
    payload = valid_classroom_document()
    payload["media_manifest"] = []
    parsed = ClassroomDocument.model_validate(payload)
    return canonical_json_bytes(parsed)


def _oversized_document_bytes() -> bytes:
    from deeptutor.teaching.export_worker import MAX_EXPORT_DOCUMENT_BYTES

    payload = valid_classroom_document()
    payload["media_manifest"] = []
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list) and isinstance(scenes[0], dict)
    scenes[0]["content"] = {
        "type": "slide",
        "canvas": {"text": "x" * MAX_EXPORT_DOCUMENT_BYTES},
    }
    provisional = ClassroomDocument.model_validate(payload)
    raw = provisional.model_dump(mode="json", by_alias=True, exclude_none=True)
    raw.pop("fileSha256")
    payload["file_sha256"] = hashlib.sha256(canonical_json_bytes(raw)).hexdigest()
    return canonical_json_bytes(ClassroomDocument.model_validate(payload))


def _source(*, revision: int = 3, version_id: str | None = None):
    from deeptutor.teaching.services.exports import ExportSource

    document = _document_bytes()
    return ExportSource(
        tenant_id="tenant-a",
        asset_id="asset-a",
        owner_id="teacher-a",
        course_id="course-a",
        class_id="class-a",
        classroom_draft_id=None if version_id else "draft-a",
        classroom_version_id=version_id,
        draft_revision=None if version_id else revision,
        document=document,
        document_sha256=hashlib.sha256(document).hexdigest(),
        media_manifest_sha256=hashlib.sha256(canonical_json_bytes([])).hexdigest(),
        media=(),
    )


class _Repository:
    def __init__(self) -> None:
        self.draft = _source()
        self.version = _source(version_id="version-a")
        self.records = {}
        self.reserve_calls = []

    async def get_draft_source(self, asset_id: str):
        return self.draft if asset_id == self.draft.asset_id else None

    async def get_version_source(self, version_id: str):
        return self.version if version_id == self.version.classroom_version_id else None

    async def reserve(self, command):
        self.reserve_calls.append(command)
        existing = self.records.get(command.idempotency_key)
        if existing is not None:
            return existing
        from deeptutor.teaching.services.exports import ExportRecord

        record = ExportRecord.from_command(command)
        self.records[command.idempotency_key] = record
        return record

    async def confirm_input(self, export_id, receipt):
        for key, record in tuple(self.records.items()):
            if record.export_id == export_id:
                updated = replace(record, input_receipt=receipt)
                self.records[key] = updated
                return updated
        raise AssertionError("unknown export")

    async def bind_job(self, export_id: str, job_id: str):
        for key, record in tuple(self.records.items()):
            if record.export_id == export_id:
                updated = replace(record, job_id=job_id, status="quota_reserved")
                self.records[key] = updated
                return updated
        raise AssertionError("unknown export")

    async def get(self, export_id: str):
        return next(
            (item for item in self.records.values() if item.export_id == export_id),
            None,
        )


class _Materializer:
    def __init__(self) -> None:
        self.plans = []

    async def materialize(self, plan):
        from deeptutor.teaching.services.exports import ExportInputReceipt

        self.plans.append(plan)
        return ExportInputReceipt(
            manifest_object_key=(
                f"tenants/{plan.tenant_id}/export-inputs/{plan.export_id}/manifest.json"
            ),
            manifest_sha256="f" * 64,
        )


class _Jobs:
    def __init__(self, repository: _Repository) -> None:
        self.commands = []
        self.repository = repository

    async def enqueue(self, command):
        self.commands.append(command)
        return await self.repository.bind_job(command.export_id, command.job_id)


def _service(*, mp4_enabled: bool = False):
    from deeptutor.teaching.services.exports import ClassroomExportService

    repository = _Repository()
    materializer = _Materializer()
    jobs = _Jobs(repository)
    service = ClassroomExportService(
        repository,
        materializer,
        jobs,
        mp4_enabled=lambda tenant_id: mp4_enabled,
    )
    return service, repository, materializer, jobs


@pytest.mark.asyncio
async def test_draft_export_rejects_stale_revision_before_materialization() -> None:
    from deeptutor.teaching.services.exports import ExportRevisionConflict

    service, repository, materializer, jobs = _service()

    with pytest.raises(ExportRevisionConflict):
        await service.create_for_draft(
            _context(),
            "asset-a",
            "pptx",
            expected_revision=2,
            idempotency_key="export-key-a",
        )

    assert repository.reserve_calls == []
    assert materializer.plans == []
    assert jobs.commands == []


@pytest.mark.asyncio
async def test_oversized_input_is_rejected_before_reservation_or_materialization() -> None:
    from deeptutor.teaching.services.exports import InvalidExportInput

    service, repository, materializer, jobs = _service()
    document = _oversized_document_bytes()
    repository.version = replace(
        repository.version,
        document=document,
        document_sha256=hashlib.sha256(document).hexdigest(),
    )

    with pytest.raises(InvalidExportInput, match="staging limits"):
        await service.create_for_version(
            _context(),
            "version-a",
            "pptx",
            idempotency_key="export-key-oversized",
        )

    assert repository.reserve_calls == []
    assert materializer.plans == []
    assert jobs.commands == []


@pytest.mark.asyncio
async def test_published_export_pins_immutable_version_hash_after_draft_edit() -> None:
    service, repository, materializer, _ = _service()

    exported = await service.create_for_version(
        _context(),
        "version-a",
        "classroom_zip",
        idempotency_key="export-key-version",
    )
    pinned_hash = exported.input_document_sha256
    repository.draft = replace(
        repository.draft,
        document=b'{"changed":true}',
        document_sha256=hashlib.sha256(b'{"changed":true}').hexdigest(),
        draft_revision=4,
    )

    assert exported.classroom_version_id == "version-a"
    assert exported.classroom_draft_id is None
    assert exported.input_document_sha256 == pinned_hash
    assert materializer.plans[0].document_sha256 == pinned_hash


@pytest.mark.asyncio
async def test_same_idempotency_binding_returns_same_export_and_job() -> None:
    service, _, materializer, jobs = _service()

    first = await service.create_for_draft(
        _context(),
        "asset-a",
        "offline_html",
        expected_revision=3,
        idempotency_key="export-key-repeat",
    )
    repeated = await service.create_for_draft(
        _context(),
        "asset-a",
        "offline_html",
        expected_revision=3,
        idempotency_key="export-key-repeat",
    )

    assert repeated.export_id == first.export_id
    assert repeated.job_id == first.job_id
    assert len(materializer.plans) == 1
    assert len(jobs.commands) == 1


@pytest.mark.asyncio
async def test_mp4_is_denied_by_default_tenant_policy() -> None:
    from deeptutor.teaching.services.exports import ExportPolicyDenied

    service, repository, materializer, jobs = _service()

    with pytest.raises(ExportPolicyDenied):
        await service.create_for_version(
            _context(),
            "version-a",
            "mp4",
            idempotency_key="export-key-mp4",
        )

    assert repository.reserve_calls == []
    assert materializer.plans == []
    assert jobs.commands == []


@pytest.mark.asyncio
async def test_other_tenant_cannot_read_export() -> None:
    service, _, _, _ = _service()
    exported = await service.create_for_version(
        _context(),
        "version-a",
        "pptx",
        idempotency_key="export-key-hidden",
    )

    assert await service.get(_context(tenant_id="tenant-b"), exported.export_id) is None


@pytest.mark.asyncio
async def test_job_gateway_uses_hash_only_request_and_atomic_export_binding() -> None:
    from types import SimpleNamespace

    from deeptutor.teaching.contracts import ExportRequest
    from deeptutor.teaching.services.export_jobs import SqlAlchemyExportJobGateway
    from deeptutor.teaching.services.exports import ExportJobCommand

    service, repository, _, _ = _service()
    exported = await service.create_for_version(
        _context(),
        "version-a",
        "pptx",
        idempotency_key="gateway-source",
    )
    repository.records["gateway-source"] = replace(exported, job_id=None)
    calls = []

    class Jobs:
        async def create_export_job_and_reserve(self, request, *, export_id):
            calls.append((request, export_id))
            repository.records["gateway-source"] = replace(
                repository.records["gateway-source"],
                job_id=request.job_id,
                status="quota_reserved",
            )

    class Selector:
        async def resolve(self, tenant_id):
            return SimpleNamespace(
                route_ref="shared-primary",
                provider_profile_ref="platform-default",
                mode="shared",
                worker_pool_ref="shared-generation",
                queue_ref="openmaic.shared",
            )

    gateway = SqlAlchemyExportJobGateway(Jobs(), repository, Selector())
    command = ExportJobCommand(
        tenant_id="tenant-a",
        export_id=exported.export_id,
        job_id=exported.export_id,
        actor_id="teacher-a",
        owner_id="teacher-a",
        course_id="course-a",
        class_id="class-a",
        export_format="pptx",
        idempotency_key="gateway-source",
        document_sha256=exported.input_document_sha256,
        media_manifest_sha256=exported.input_media_manifest_sha256,
        input_manifest_sha256="f" * 64,
        input_manifest_object_key="tenants/tenant-a/export-inputs/fixed/manifest.json",
    )

    bound = await gateway.enqueue(command)
    request, export_id = calls[0]
    frozen = ExportRequest.model_validate_json(request.request_payload)

    assert export_id == exported.export_id
    assert bound.job_id == exported.export_id
    assert frozen.idempotency_key == exported.export_id
    assert frozen.classroom_document_sha256 == exported.input_document_sha256
    assert "manifest" not in request.request_payload
    assert "object" not in request.request_payload.lower()


@pytest.mark.asyncio
async def test_input_materializer_commits_an_immutable_recoverable_snapshot(tmp_path) -> None:
    from deeptutor.teaching.artifacts import temporary_artifact_key
    from deeptutor.teaching.object_store import LocalClassroomArtifactStore
    from deeptutor.teaching.services.exports import ClassroomExportInputMaterializer

    media = b"ID3-fixed-media"
    payload = valid_classroom_document()
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list)
    entry = manifest[0]
    assert isinstance(entry, dict)
    entry["sha256"] = hashlib.sha256(media).hexdigest()
    entry["size_bytes"] = len(media)
    document = canonical_json_bytes(ClassroomDocument.model_validate(payload))
    source_key = temporary_artifact_key("tenant-a", "draft-media", "voice.mp3")
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")

    async def media_body():
        yield media[:3]
        yield media[3:]

    await store.put_verified(
        source_key,
        media_body(),
        hashlib.sha256(media).hexdigest(),
        len(media),
        content_type="audio/mpeg",
        ownership_token="1" * 32,
    )

    class Stores:
        async def store_for_tenant(self, tenant_id: str):
            assert tenant_id == "tenant-a"
            return store

    source = _source()
    source = replace(
        source,
        document=document,
        document_sha256=hashlib.sha256(document).hexdigest(),
        media_manifest_sha256=hashlib.sha256(
            canonical_json_bytes(
                ClassroomDocument.model_validate(payload).model_dump(
                    mode="json", by_alias=True, exclude_none=False
                )["mediaManifest"]
            )
        ).hexdigest(),
        media=(
            __import__(
                "deeptutor.teaching.services.exports",
                fromlist=["ExportSourceMedia"],
            ).ExportSourceMedia(
                media_id="media-1",
                relative_name="media/voice.mp3",
                object_key=source_key,
                sha256=hashlib.sha256(media).hexdigest(),
                size_bytes=len(media),
                mime_type="audio/mpeg",
            ),
        ),
    )
    command = __import__(
        "deeptutor.teaching.services.exports", fromlist=["ExportCommand"]
    ).ExportCommand(
        tenant_id="tenant-a",
        export_id="export-fixed",
        job_id="export-fixed",
        idempotency_key="export-fixed-key",
        request_sha256="a" * 64,
        actor_id="teacher-a",
        export_format="pptx",
        asset_id=source.asset_id,
        owner_id=source.owner_id,
        course_id=source.course_id,
        class_id=source.class_id,
        classroom_draft_id=source.classroom_draft_id,
        classroom_version_id=None,
        draft_revision=source.draft_revision,
        document=source.document,
        document_sha256=source.document_sha256,
        media_manifest_sha256=source.media_manifest_sha256,
        media=source.media,
    )
    materializer = ClassroomExportInputMaterializer(Stores())

    first = await materializer.materialize(command)
    replay = await materializer.materialize(command)

    assert replay == first
    manifest_bytes = b"".join(
        [chunk async for chunk in await store.open(first.manifest_object_key)]
    )
    assert hashlib.sha256(manifest_bytes).hexdigest() == first.manifest_sha256
    assert b"temporary/draft-media" not in manifest_bytes
    assert b"export-inputs/export-fixed/classroom.json" in manifest_bytes
    from deeptutor.teaching.export_worker import load_export_input_bundle

    loaded = await load_export_input_bundle(
        store,
        tenant_id="tenant-a",
        job_id="export-fixed",
        manifest_object_key=first.manifest_object_key,
        manifest_sha256=first.manifest_sha256,
    )
    assert loaded.document.sha256 == command.document_sha256
    assert loaded.media_manifest_sha256 == command.media_manifest_sha256
    assert loaded.media[0].media_id == "media-1"
