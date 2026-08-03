from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from io import BytesIO
from types import SimpleNamespace

import pytest

from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.contracts import (
    GenerationMetadata,
    KnowledgeCoverage,
    OutlineBundle,
    OutlineConfirmationMetadata,
    OutlineScene,
    canonical_outline_sha256,
)
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.services.classrooms import (
    ClassroomAccessDenied,
    ClassroomRecord,
    ClassroomService,
    DraftMediaRecord,
    GenerationStage,
    InvalidDraftMedia,
    NewClassroomWorkflow,
    NewDraftMedia,
)
from deeptutor.teaching.tenant_context import TenantContext

NOW = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)


def _context(
    *,
    tenant_id: str = "tenant-a",
    user_id: str = "teacher-a",
    scope_type: str = "class",
    scope_id: str = "class-a",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=f"tenant_{tenant_id}",
        user_id=user_id,
        permissions=permissions_for_roles(
            {"teacher"},
            scope_type=scope_type,
            scope_id=scope_id,
            tenant_id=tenant_id,
        ),
    )


def _request(**changes):
    values = {
        "title": "Motion",
        "course_id": "course-a",
        "class_id": "class-a",
        "objective": "Explain motion",
        "grade_band": "grade-8",
        "audience": "intermediate",
        "duration_minutes": 45,
        "classroom_mode": "full",
        "web_policy": "disabled",
        "allowed_web_domains": [],
        "template_id": "template-a",
        "template_version": "1",
        "knowledge_points": [
            SimpleNamespace(
                knowledge_point_id="kp-motion",
                title="Motion",
                description="Describe displacement and velocity",
            )
        ],
        "content_mode": "open_creation",
        "open_creation_acknowledged": True,
        "source_type": None,
        "source_ref": None,
        "requested_exports": ["classroom_zip"],
    }
    values.update(changes)
    return SimpleNamespace(**values)


class _Repository:
    def __init__(self) -> None:
        self.records: dict[str, ClassroomRecord] = {}
        self.new_workflows: list[NewClassroomWorkflow] = []
        self.validation_reports: list[dict[str, object]] = []
        self.media: dict[tuple[str, str], DraftMediaRecord] = {}

    async def create_workflow(self, workflow: NewClassroomWorkflow) -> ClassroomRecord:
        self.new_workflows.append(workflow)
        record = ClassroomRecord(
            tenant_id=workflow.tenant_id,
            asset_id=workflow.asset_id,
            draft_id=workflow.draft_id,
            job_id=None,
            lifecycle_state="generating_outline",
            status="queued",
            title=workflow.title,
            course_id=workflow.teaching_brief.course_id,
            class_id=workflow.teaching_brief.target_class_id,
            owner_id=workflow.owner_id,
            teaching_brief=workflow.teaching_brief,
            revision=1,
            outline=None,
            document={},
            classroom_version_id=None,
            confirmed_outline_sha256=None,
            validation_report=None,
        )
        self.records[record.asset_id] = record
        return record

    async def list_workflows(self):
        return tuple(self.records.values())

    async def get_workflow(self, asset_id: str):
        return self.records.get(asset_id)

    async def attach_outline_job(self, asset_id: str, job_id: str):
        self.records[asset_id] = replace(self.records[asset_id], job_id=job_id)
        return self.records[asset_id]

    async def save_outline(self, asset_id: str, outline: dict, outline_sha256: str):
        current = self.records[asset_id]
        self.records[asset_id] = replace(
            current,
            lifecycle_state="awaiting_outline",
            status="awaiting_confirmation",
            outline=outline,
            revision=current.revision + 1,
        )
        return self.records[asset_id]

    async def update_outline(
        self, asset_id: str, outline: dict, outline_sha256: str, expected_revision: int
    ):
        current = self.records[asset_id]
        if current.revision != expected_revision:
            return None
        self.records[asset_id] = replace(
            current,
            outline=outline,
            revision=current.revision + 1,
        )
        return self.records[asset_id]

    async def confirm_outline(
        self, asset_id: str, outline: dict, confirmed_outline_sha256: str
    ):
        current = self.records[asset_id]
        self.records[asset_id] = replace(
            current,
            lifecycle_state="generating_content",
            status="queued",
            outline=outline,
            confirmed_outline_sha256=confirmed_outline_sha256,
        )
        return self.records[asset_id]

    async def update_document(self, asset_id, document, document_sha256, expected_revision):
        current = self.records[asset_id]
        if current.revision != expected_revision:
            return None
        self.records[asset_id] = replace(
            current,
            document=document,
            revision=current.revision + 1,
            validation_report=None,
        )
        return self.records[asset_id]

    async def available_media_ids(self, asset_id: str):
        return frozenset()

    async def save_validation_report(self, asset_id, report, report_sha256):
        self.validation_reports.append(report)
        current = self.records[asset_id]
        self.records[asset_id] = replace(current, validation_report=report)
        return self.records[asset_id]

    async def reserve_media(self, media: NewDraftMedia):
        record = DraftMediaRecord(
            id=media.id,
            classroom_id=media.classroom_id,
            mime_type=media.mime_type,
            sha256=media.sha256,
            size_bytes=media.size_bytes,
            object_key=media.object_key,
            ownership_token=media.ownership_token,
            object_revision=None,
        )
        self.media[(media.classroom_id, media.id)] = record
        return record

    async def complete_media(self, asset_id, media_id, object_revision):
        current = self.media[(asset_id, media_id)]
        completed = replace(current, object_revision=object_revision)
        self.media[(asset_id, media_id)] = completed
        return completed

    async def fail_media(self, asset_id, media_id, error_code):
        self.media.pop((asset_id, media_id), None)

    async def get_media(self, asset_id, media_id):
        return self.media.get((asset_id, media_id))


class _Generation:
    def __init__(self) -> None:
        self.start_calls = []
        self.content_calls = []
        self.content_error: Exception | None = None

    async def start_outline(
        self,
        *,
        context,
        asset_id,
        draft_id,
        teaching_brief,
        requested_exports,
    ) -> GenerationStage:
        self.start_calls.append((context, asset_id, draft_id, teaching_brief, requested_exports))
        job_id = "job-0123456789abcdef0123456789abcdef"
        outline = OutlineBundle(
            schema_version="1.0",
            outline_id=f"outline-{job_id}",
            outline_version=1,
            confirmation_metadata=OutlineConfirmationMetadata(status="draft"),
            title="Motion outline",
            language="en-US",
            scenes=[
                OutlineScene(
                    scene_id="scene-1",
                    title="Motion",
                    summary="Introduce motion.",
                    knowledge_point_ids=["kp-motion"],
                    source_refs=[],
                )
            ],
            knowledge_coverage=[
                KnowledgeCoverage(
                    knowledge_point_id="kp-motion",
                    scene_ids=["scene-1"],
                )
            ],
            source_refs=[],
            estimated_scene_count=1,
            generation_metadata=GenerationMetadata(
                generator="openmaic",
                generator_version="0.3.1",
                model_id="server-selected-model",
                generated_at=NOW,
                teaching_brief_id=teaching_brief.brief_id,
                teaching_brief_sha256=teaching_brief.content_sha256,
                template_id=teaching_brief.template_policy.template_id,
                template_version=teaching_brief.template_policy.template_version,
            ),
            contract_sha256=(
                "a45b0310d5b58a8e2d461ccfa9d60be24615583825a1f3a4f4460672cbd19ba5"
            ),
        )
        return GenerationStage(
            job_id=job_id,
            status="awaiting_confirmation",
            outline=outline,
            classroom_version_id=None,
        )

    async def get_stage(self, *, context, job_id):
        raise AssertionError("create result already carries the outline")

    async def start_content(
        self,
        *,
        context,
        job_id,
        confirmed_outline,
        confirmed_outline_sha256,
    ) -> GenerationStage:
        self.content_calls.append((confirmed_outline, confirmed_outline_sha256))
        if self.content_error is not None:
            raise self.content_error
        return GenerationStage(
            job_id=job_id,
            status="queued",
            outline=confirmed_outline,
            classroom_version_id=None,
        )


class _Upload:
    def __init__(self, body: bytes, mime_type: str) -> None:
        self._body = BytesIO(body)
        self.content_type = mime_type
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    async def close(self) -> None:
        self.closed = True


class _Store:
    tenant_id = "tenant-a"

    def __init__(self) -> None:
        self.put_calls = []
        self.content: dict[str, bytes] = {}

    async def put_verified(
        self,
        key,
        body,
        sha256,
        size,
        *,
        content_type,
        ownership_token,
    ):
        payload = b"".join([chunk async for chunk in body])
        assert hashlib.sha256(payload).hexdigest() == sha256
        assert len(payload) == size
        self.put_calls.append((key, content_type, ownership_token))
        self.content[key] = payload
        return SimpleNamespace(revision="revision-1")

    async def open(self, key):
        async def stream():
            yield self.content[key]

        return stream()


class _StoreProvider:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def store_for_tenant(self, tenant_id: str):
        assert tenant_id == self.store.tenant_id
        return self.store


def _service(context: TenantContext):
    repository = _Repository()
    generation = _Generation()
    builder = TeachingBriefBuilder(context, object())
    return ClassroomService(repository, builder, generation, None, clock=lambda: NOW), repository, generation


def _service_with_store(context: TenantContext):
    repository = _Repository()
    generation = _Generation()
    store = _Store()
    builder = TeachingBriefBuilder(context, object())
    return (
        ClassroomService(
            repository,
            builder,
            generation,
            _StoreProvider(store),
            clock=lambda: NOW,
        ),
        repository,
        store,
    )


@pytest.mark.asyncio
async def test_full_creation_uses_brief_builder_and_enqueues_outline_only() -> None:
    context = _context()
    service, repository, generation = _service(context)

    result = await service.create(context, _request())

    assert result.status == "awaiting_confirmation"
    assert result.outline is not None
    assert result.classroom_version_id is None
    assert len(repository.new_workflows) == 1
    assert repository.new_workflows[0].teaching_brief.classroom_mode == "full"
    assert len(generation.start_calls) == 1
    assert generation.content_calls == []


@pytest.mark.asyncio
async def test_edited_outline_hash_is_server_computed_and_bound_to_content_stage() -> None:
    context = _context()
    service, _, generation = _service(context)
    created = await service.create(context, _request())
    edited = dict(created.outline or {})
    edited["title"] = "Teacher edited motion outline"
    updated = await service.update_outline(
        context,
        created.asset_id,
        edited,
        created.revision,
    )

    confirmed = await service.confirm_outline(context, created.asset_id)

    content_outline, content_hash = generation.content_calls[0]
    assert content_outline.confirmation_metadata.status == "confirmed"
    assert content_outline.confirmation_metadata.confirmed_by == context.user_id
    assert content_hash == canonical_outline_sha256(content_outline)
    assert confirmed.confirmed_outline_sha256 == content_hash
    assert confirmed.classroom_version_id is None
    assert confirmed.lifecycle_state == "generating_content"
    assert updated.outline["title"] == "Teacher edited motion outline"


@pytest.mark.asyncio
async def test_confirmed_outline_is_durable_before_content_requeue() -> None:
    context = _context()
    service, repository, generation = _service(context)
    created = await service.create(context, _request())
    generation.content_error = RuntimeError("queue unavailable")

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.confirm_outline(context, created.asset_id)

    persisted = repository.records[created.asset_id]
    assert persisted.lifecycle_state == "generating_content"
    assert persisted.confirmed_outline_sha256 is not None
    confirmed = OutlineBundle.model_validate(persisted.outline)
    assert confirmed.confirmation_metadata.status == "confirmed"
    assert persisted.confirmed_outline_sha256 == canonical_outline_sha256(confirmed)
    assert persisted.classroom_version_id is None


@pytest.mark.asyncio
async def test_class_scoped_teacher_cannot_create_or_view_another_class() -> None:
    context = _context(scope_id="class-a")
    service, repository, _ = _service(context)

    with pytest.raises(ClassroomAccessDenied):
        await service.create(context, _request(class_id="class-b"))

    repository.records["asset-b"] = ClassroomRecord(
        tenant_id="tenant-a",
        asset_id="asset-b",
        draft_id="draft-b",
        job_id=None,
        lifecycle_state="editing",
        status="succeeded",
        title="Hidden",
        course_id="course-b",
        class_id="class-b",
        owner_id="teacher-b",
        teaching_brief=None,
        revision=1,
        outline=None,
        document={},
        classroom_version_id=None,
        confirmed_outline_sha256=None,
        validation_report=None,
    )

    assert await service.get(context, "asset-b") is None


@pytest.mark.asyncio
async def test_validation_is_persisted_with_blocking_and_warning_findings() -> None:
    context = _context()
    service, repository, _ = _service(context)
    created = await service.create(context, _request())
    current = repository.records[created.asset_id]
    repository.records[created.asset_id] = replace(
        current,
        lifecycle_state="editing",
        status="succeeded",
        document={
            "dslVersion": "0.1.0",
            "scenes": [
                {
                    "id": "scene-1",
                    "type": "interactive",
                    "title": "Unsafe",
                    "content": {"html": "<script>alert(1)</script>"},
                }
            ],
            "mediaIds": [],
            "knowledgePointMappings": [],
            "sourceRefs": [],
        },
    )

    result = await service.validate(context, created.asset_id)

    assert result.validation_report["valid"] is False
    assert result.validation_report["severeFindings"]
    assert result.validation_report["warnings"]
    assert repository.validation_reports == [result.validation_report]


@pytest.mark.asyncio
async def test_media_upload_validates_content_and_uses_opaque_asset_scoped_id() -> None:
    context = _context()
    service, repository, store = _service_with_store(context)
    created = await service.create(context, _request())
    body = b"\x89PNG\r\n\x1a\nimage"
    digest = hashlib.sha256(body).hexdigest()
    upload = _Upload(body, "image/png")

    media = await service.upload_media(context, created.asset_id, upload, digest)

    assert media.id.startswith("media-")
    assert len(media.id) == len("media-") + 32
    assert media.object_key.startswith(
        f"tenants/{context.tenant_id}/temporary/draft-{created.asset_id}/media/"
    )
    assert media.object_key not in repr(media)
    assert media.object_revision == "revision-1"
    assert upload.closed is True
    assert len(repository.media) == 1
    assert len(store.put_calls) == 1


@pytest.mark.asyncio
async def test_media_upload_rejects_spoofed_mime_and_sha_before_storage() -> None:
    context = _context()
    service, _, store = _service_with_store(context)
    created = await service.create(context, _request())
    body = b"not a png"

    with pytest.raises(InvalidDraftMedia):
        await service.upload_media(
            context,
            created.asset_id,
            _Upload(body, "image/png"),
            hashlib.sha256(body).hexdigest(),
        )
    with pytest.raises(InvalidDraftMedia):
        await service.upload_media(
            context,
            created.asset_id,
            _Upload(b"\x89PNG\r\n\x1a\nimage", "image/png"),
            "0" * 64,
        )

    assert store.put_calls == []
