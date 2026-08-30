from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from deeptutor.teaching.artifacts import StoredArtifact, classroom_artifact_key
from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.contracts import (
    ClassroomDocument,
    GenerationMetadata,
    GenerationRequest,
    KnowledgeCoverage,
    OutlineBundle,
    OutlineConfirmationMetadata,
    OutlineScene,
    canonical_json_bytes,
    canonical_outline_sha256,
)
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.repositories.classrooms import SqlAlchemyClassroomRepository
from deeptutor.teaching.repositories.jobs import GenerationJobDetails
from deeptutor.teaching.services.batches import (
    BatchItemRecord,
    BatchJobRecord,
    BatchOutlineConflict,
    BatchService,
    SqlAlchemyBatchClassroomGateway,
)
from deeptutor.teaching.services.classrooms import (
    ClassroomAccessDenied,
    ClassroomConfirmationConflict,
    ClassroomIdempotencyConflict,
    ClassroomMediaBinding,
    ClassroomPreflightRejected,
    ClassroomRecord,
    ClassroomRevisionConflict,
    ClassroomService,
    DraftMediaRecord,
    GenerationStage,
    InvalidClassroomState,
    InvalidDraftDocument,
    InvalidDraftMedia,
    NewClassroomWorkflow,
    NewDraftMedia,
    SqlAlchemyClassroomGeneration,
)
from deeptutor.teaching.tenant_context import TenantContext
from tests.teaching_contract_fixtures import valid_classroom_document

NOW = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)


def _canonical_document(
    *,
    classroom_id: str = "classroom-1",
    classroom_version_id: str = "classroom-version-1",
    title: str = "Periodic signals",
    interactive_html: str | None = None,
) -> dict[str, object]:
    payload = valid_classroom_document()
    payload["classroom_id"] = classroom_id
    payload["classroom_version_id"] = classroom_version_id
    payload["content_mode"] = "open_creation"
    payload["open_creation"] = True
    payload["media_manifest"] = []
    payload["export_manifest"] = []
    openmaic = payload["openmaic"]
    assert isinstance(openmaic, dict)
    scenes = openmaic["scenes"]
    assert isinstance(scenes, list) and isinstance(scenes[0], dict)
    scenes[0]["title"] = title
    if interactive_html is not None:
        scenes[0]["type"] = "interactive"
        scenes[0]["content"] = {
            "type": "interactive",
            "html": interactive_html,
            "bridge_version": "1.0",
            "sandbox": {"allow_scripts": True, "allow_same_origin": False},
        }
    provisional = ClassroomDocument.model_validate(payload)
    unhashed = provisional.model_dump(mode="json", by_alias=True, exclude_none=True)
    unhashed.pop("fileSha256")
    payload["file_sha256"] = hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    return ClassroomDocument.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )


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
        "media_policy": "image_audio",
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
        self.media_status: dict[tuple[str, str], str] = {}
        self.version_media: dict[tuple[str, str], SimpleNamespace] = {}
        self.version_media_calls: list[tuple[str, str]] = []
        self.cleanup_transitions: list[str] = []

    async def get_creation(self, idempotency_key: str):
        for record in self.records.values():
            if record.creation_idempotency_key == idempotency_key:
                return record
        return None

    async def create_workflow(self, workflow: NewClassroomWorkflow) -> ClassroomRecord:
        existing = await self.get_creation(workflow.creation_idempotency_key)
        if existing is not None:
            if (
                existing.owner_id != workflow.owner_id
                or existing.creation_request_sha256 != workflow.creation_request_sha256
            ):
                raise ClassroomIdempotencyConflict()
            return existing
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
            creation_idempotency_key=workflow.creation_idempotency_key,
            creation_request_sha256=workflow.creation_request_sha256,
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
        if current.lifecycle_state not in {"generating_outline", "awaiting_outline"}:
            raise InvalidClassroomState("outline state is invalid")
        changed = current.outline != outline
        self.records[asset_id] = replace(
            current,
            lifecycle_state="awaiting_outline",
            status="awaiting_confirmation",
            outline=outline,
            revision=current.revision + 1 if changed else current.revision,
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
        self,
        asset_id: str,
        outline: dict,
        confirmed_outline_sha256: str,
        source_outline_sha256: str,
        *,
        expected_revision: int | None = None,
        expected_outline_sha256: str | None = None,
    ):
        assert len(source_outline_sha256) == 64
        current = self.records[asset_id]
        if (expected_revision is None) != (expected_outline_sha256 is None):
            raise ClassroomConfirmationConflict("confirmed outline conflicts")
        first_confirmation = current.confirmed_outline_sha256 is None
        if expected_revision is not None:
            required_revision = expected_revision if first_confirmation else expected_revision + 1
            if current.revision != required_revision:
                raise ClassroomConfirmationConflict("confirmed outline conflicts")
        self.records[asset_id] = replace(
            current,
            lifecycle_state="generating_content",
            status="queued",
            outline=outline,
            confirmed_outline_sha256=confirmed_outline_sha256,
            revision=current.revision + 1 if first_confirmation else current.revision,
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

    async def available_media_bindings(self, asset_id: str):
        suffixes = {"image/png": ".png", "audio/mpeg": ".mp3", "video/mp4": ".mp4"}
        return tuple(
            ClassroomMediaBinding(
                media_id=record.id,
                relative_name=f"media/{record.id}{suffixes[record.mime_type]}",
                mime_type=record.mime_type,
                sha256=record.sha256,
                size_bytes=record.size_bytes,
            )
            for (classroom_id, _), record in self.media.items()
            if classroom_id == asset_id
            and record.status == "uploaded"
            and record.object_revision is not None
            and record.mime_type in suffixes
        )

    async def save_validation_report(
        self,
        asset_id,
        report,
        report_sha256,
        expected_revision,
        expected_document_sha256,
    ):
        current = self.records[asset_id]
        assert current.revision == expected_revision
        assert (
            hashlib.sha256(canonical_json_bytes(current.document)).hexdigest()
            == expected_document_sha256
        )
        self.validation_reports.append(report)
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
        self.media_status[(media.classroom_id, media.id)] = "writing"
        return record

    async def complete_media(self, asset_id, media_id, object_revision):
        current = self.media[(asset_id, media_id)]
        completed = replace(
            current,
            object_revision=object_revision,
            status="uploaded",
            last_error_code=None,
        )
        self.media[(asset_id, media_id)] = completed
        self.media_status[(asset_id, media_id)] = "uploaded"
        return completed

    async def fail_media(self, asset_id, media_id, error_code):
        current = self.media[(asset_id, media_id)]
        self.media[(asset_id, media_id)] = replace(
            current,
            status="failed",
            last_error_code=error_code,
        )
        self.media_status[(asset_id, media_id)] = "failed"

    async def mark_media_cleanup_pending(self, asset_id, media_id, error_code):
        current = self.media[(asset_id, media_id)]
        pending = replace(
            current,
            status="cleanup_pending",
            last_error_code=error_code,
        )
        self.media[(asset_id, media_id)] = pending
        self.media_status[(asset_id, media_id)] = "cleanup_pending"
        self.cleanup_transitions.append("cleanup_pending")
        return pending

    async def finish_media_cleanup(self, asset_id, media_id, error_code):
        current = self.media[(asset_id, media_id)]
        self.media[(asset_id, media_id)] = replace(
            current,
            object_revision=None,
            status="failed",
            last_error_code=error_code,
        )
        self.media_status[(asset_id, media_id)] = "failed"
        self.cleanup_transitions.append("failed")

    async def get_media_receipt(self, asset_id, media_id):
        return self.media.get((asset_id, media_id))

    async def list_cleanup_pending(self, asset_id, *, limit=8):
        return tuple(
            receipt
            for (classroom_id, _), receipt in self.media.items()
            if classroom_id == asset_id and receipt.status == "cleanup_pending"
        )[:limit]

    async def get_media(self, asset_id, media_id):
        return self.media.get((asset_id, media_id))

    async def get_bound_version_media(self, asset_id, media_id):
        self.version_media_calls.append((asset_id, media_id))
        return self.version_media.get((asset_id, media_id))


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
            contract_sha256=("a45b0310d5b58a8e2d461ccfa9d60be24615583825a1f3a4f4460672cbd19ba5"),
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
        asset_id,
        draft_id,
        job_id,
        confirmed_outline,
        confirmed_outline_sha256,
    ) -> GenerationStage:
        assert asset_id
        assert draft_id
        self.content_calls.append((confirmed_outline, confirmed_outline_sha256))
        if self.content_error is not None:
            raise self.content_error
        return GenerationStage(
            job_id=job_id,
            status="queued",
            outline=confirmed_outline,
            classroom_version_id=None,
        )


class _ReplayableGeneration(_Generation):
    def __init__(self, *, fail_first_start: bool = False) -> None:
        super().__init__()
        self.fail_first_start = fail_first_start
        self.stages: dict[str, GenerationStage] = {}
        self.return_mismatched_stage = False

    async def start_outline(self, **kwargs) -> GenerationStage:
        asset_id = kwargs["asset_id"]
        if self.fail_first_start:
            self.fail_first_start = False
            self.start_calls.append(tuple(kwargs.values()))
            raise RuntimeError("selector unavailable")
        stage = self.stages.get(asset_id)
        if stage is None:
            stage = await super().start_outline(**kwargs)
            self.stages[asset_id] = stage
        else:
            self.start_calls.append(tuple(kwargs.values()))
        return stage

    async def get_stage(self, *, context, job_id):
        for stage in self.stages.values():
            if stage.job_id == job_id:
                if self.return_mismatched_stage:
                    return replace(stage, job_id="job-mismatched-binding")
                return stage
        raise AssertionError("unknown generation stage")


class _AttachFailsOnceRepository(_Repository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_attach = True

    async def attach_outline_job(self, asset_id: str, job_id: str):
        if self.fail_next_attach:
            self.fail_next_attach = False
            raise RuntimeError("attach response lost")
        return await super().attach_outline_job(asset_id, job_id)


class _AttachCommitsThenFailsOnceRepository(_Repository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_attach = True

    async def attach_outline_job(self, asset_id: str, job_id: str):
        record = await super().attach_outline_job(asset_id, job_id)
        if self.fail_next_attach:
            self.fail_next_attach = False
            raise RuntimeError("attach response lost after commit")
        return record


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

    async def reconcile_verified(
        self,
        key,
        sha256,
        size,
        *,
        content_type,
        ownership_token,
    ):
        payload = self.content.get(key)
        if payload is None:
            return None
        assert hashlib.sha256(payload).hexdigest() == sha256
        assert len(payload) == size
        return StoredArtifact(
            key=key,
            sha256=sha256,
            size=size,
            content_type=content_type,
            ownership_token=ownership_token,
            revision="revision-1",
        )

    async def delete_owned(self, artifact: StoredArtifact) -> None:
        self.content.pop(artifact.key, None)


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
    return (
        ClassroomService(repository, builder, generation, None, clock=lambda: NOW),
        repository,
        generation,
    )


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
@pytest.mark.parametrize(
    "classroom_request",
    (
        _request(classroom_mode="compact"),
        _request(duration_minutes=0),
        _request(content_mode="source_grounded", source_type=None, source_ref=None),
    ),
)
async def test_invalid_creation_input_is_explicit_preflight_without_workflow_or_job(
    classroom_request,
) -> None:
    context = _context()
    repository = _Repository()
    generation = _Generation()
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        generation,
        None,
    )

    with pytest.raises(ClassroomPreflightRejected):
        await service.create(context, classroom_request)

    assert repository.new_workflows == []
    assert generation.start_calls == []


@pytest.mark.asyncio
async def test_create_retry_after_selector_failure_reuses_durable_workflow() -> None:
    context = _context()
    repository = _Repository()
    generation = _ReplayableGeneration(fail_first_start=True)
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        generation,
        None,
        clock=lambda: NOW,
    )
    request = _request()

    with pytest.raises(RuntimeError, match="selector unavailable"):
        await service.create(context, request)
    first = repository.new_workflows[0]

    recovered = await service.create(context, request)

    assert recovered.asset_id == first.asset_id
    assert recovered.draft_id == first.draft_id
    assert len(repository.new_workflows) == 1


@pytest.mark.asyncio
async def test_create_retry_after_job_creation_reuses_workflow_and_job() -> None:
    context = _context()
    repository = _AttachFailsOnceRepository()
    generation = _ReplayableGeneration()
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        generation,
        None,
        clock=lambda: NOW,
    )
    request = _request()

    with pytest.raises(RuntimeError, match="attach response lost"):
        await service.create(context, request)
    first = repository.new_workflows[0]
    first_stage = generation.stages[first.asset_id]

    recovered = await service.create(context, request)

    assert (recovered.asset_id, recovered.draft_id, recovered.job_id) == (
        first.asset_id,
        first.draft_id,
        first_stage.job_id,
    )
    assert len(repository.new_workflows) == 1
    assert len(generation.stages) == 1


@pytest.mark.asyncio
async def test_create_retry_after_committed_attach_reads_and_checks_bound_job() -> None:
    context = _context()
    repository = _AttachCommitsThenFailsOnceRepository()
    generation = _ReplayableGeneration()
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        generation,
        None,
        clock=lambda: NOW,
    )
    request = _request()

    with pytest.raises(RuntimeError, match="after commit"):
        await service.create(context, request)
    persisted = next(iter(repository.records.values()))
    assert persisted.job_id is not None

    recovered = await service.create(context, request)

    assert recovered.job_id == persisted.job_id
    assert len(repository.new_workflows) == 1
    assert len(generation.start_calls) == 1


@pytest.mark.asyncio
async def test_create_retry_rejects_a_mismatched_durable_job_stage() -> None:
    context = _context()
    repository = _AttachCommitsThenFailsOnceRepository()
    generation = _ReplayableGeneration()
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        generation,
        None,
        clock=lambda: NOW,
    )
    request = _request()

    with pytest.raises(RuntimeError, match="after commit"):
        await service.create(context, request)
    persisted = next(iter(repository.records.values()))
    assert persisted.job_id is not None
    generation.return_mismatched_stage = True

    with pytest.raises(InvalidClassroomState, match="binding"):
        await service.create(context, request)


@pytest.mark.asyncio
async def test_create_idempotency_key_rejects_a_different_request() -> None:
    context = _context()
    service, repository, generation = _service(context)

    await service.create(context, _request(), idempotency_key="classroom-request-1")

    with pytest.raises(ClassroomIdempotencyConflict):
        await service.create(
            context,
            _request(objective="Explain acceleration"),
            idempotency_key="classroom-request-1",
        )

    assert len(repository.new_workflows) == 1
    assert len(generation.start_calls) == 1


@pytest.mark.asyncio
async def test_create_idempotency_hash_includes_media_policy() -> None:
    context = _context()
    service, repository, generation = _service(context)

    await service.create(context, _request(), idempotency_key="classroom-media-policy")

    with pytest.raises(ClassroomIdempotencyConflict):
        await service.create(
            context,
            _request(media_policy="text_only"),
            idempotency_key="classroom-media-policy",
        )

    assert len(repository.new_workflows) == 1
    assert len(generation.start_calls) == 1


@pytest.mark.asyncio
async def test_different_explicit_keys_create_distinct_identical_classrooms() -> None:
    context = _context()
    service, repository, generation = _service(context)
    request = _request()

    first = await service.create(
        context,
        request,
        idempotency_key="classroom-request-1",
    )
    second = await service.create(
        context,
        request,
        idempotency_key="classroom-request-2",
    )

    assert first.asset_id != second.asset_id
    assert len(repository.new_workflows) == 2
    assert len(generation.start_calls) == 2


@pytest.mark.asyncio
async def test_create_without_header_derives_a_reusable_request_key() -> None:
    context = _context()
    repository = _Repository()
    generation = _ReplayableGeneration()
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        generation,
        None,
        clock=lambda: NOW,
    )
    request = _request()

    first = await service.create(context, request)
    retried = await service.create(context, request)

    assert retried.asset_id == first.asset_id
    assert retried.draft_id == first.draft_id
    assert retried.creation_idempotency_key.startswith("auto-")
    assert len(repository.new_workflows) == 1
    assert len(generation.start_calls) == 1


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
async def test_review_bound_confirmation_retries_after_content_requeue_failure() -> None:
    context = _context()
    service, repository, generation = _service(context)
    created = await service.create(context, _request())
    reviewed_revision = created.revision
    reviewed_sha256 = canonical_outline_sha256(OutlineBundle.model_validate(created.outline))
    generation.content_error = RuntimeError("queue unavailable")

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.confirm_outline(
            context,
            created.asset_id,
            expected_revision=reviewed_revision,
            expected_outline_sha256=reviewed_sha256,
        )

    persisted = repository.records[created.asset_id]
    assert persisted.lifecycle_state == "generating_content"
    assert persisted.revision == reviewed_revision + 1
    generation.content_error = None

    recovered = await service.confirm_outline(
        context,
        created.asset_id,
        expected_revision=reviewed_revision,
        expected_outline_sha256=reviewed_sha256,
    )

    assert recovered.revision == reviewed_revision + 1
    assert len(generation.content_calls) == 2


@pytest.mark.asyncio
async def test_review_bound_confirmation_recovery_rejects_tampered_confirmed_outline() -> None:
    context = _context()
    service, repository, generation = _service(context)
    created = await service.create(context, _request())
    reviewed_revision = created.revision
    reviewed_sha256 = canonical_outline_sha256(OutlineBundle.model_validate(created.outline))
    generation.content_error = RuntimeError("queue unavailable")
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.confirm_outline(
            context,
            created.asset_id,
            expected_revision=reviewed_revision,
            expected_outline_sha256=reviewed_sha256,
        )
    persisted = repository.records[created.asset_id]
    confirmed = OutlineBundle.model_validate(persisted.outline)
    tampered = confirmed.model_copy(update={"title": "Tampered after confirmation"})
    repository.records[created.asset_id] = replace(
        persisted,
        outline=tampered.model_dump(mode="json", by_alias=True, exclude_none=True),
        confirmed_outline_sha256=canonical_outline_sha256(tampered),
    )
    generation.content_error = None

    with pytest.raises(ClassroomConfirmationConflict):
        await service.confirm_outline(
            context,
            created.asset_id,
            expected_revision=reviewed_revision,
            expected_outline_sha256=reviewed_sha256,
        )


class _SingleRecoveryBatchRepository:
    def __init__(self, batch: BatchJobRecord) -> None:
        self.batch = batch

    async def get(self, batch_id: str):
        return self.batch if self.batch.id == batch_id else None

    async def set_item_status(self, batch_id: str, item_id: str, status: str):
        assert batch_id == self.batch.id
        items = tuple(
            replace(item, status=status) if item.id == item_id else item
            for item in self.batch.items
        )
        self.batch = replace(self.batch, status=status, items=items)
        return next(item for item in items if item.id == item_id)


async def _batch_confirmation_recovery_fixture(
    context: TenantContext,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = _Repository()
    generation = _ReplayableGeneration()
    classroom_service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        generation,
        None,
        clock=lambda: NOW,
    )
    created = await classroom_service.create(context, _request())
    batch = BatchJobRecord(
        id="batch-confirm-recovery",
        tenant_id=context.tenant_id,
        actor_id=context.user_id,
        status="awaiting_confirmation",
        item_count=1,
        succeeded_count=0,
        failed_count=0,
        items=(
            BatchItemRecord(
                id="item-a",
                batch_id="batch-confirm-recovery",
                status="awaiting_confirmation",
                generation_job_id=created.job_id,
                classroom_draft_id=created.draft_id,
                classroom_asset_id=created.asset_id,
                resource_course_id="course-a",
                resource_class_id="class-a",
            ),
        ),
    )
    batch_repository = _SingleRecoveryBatchRepository(batch)
    gateway = SqlAlchemyBatchClassroomGateway(None, None, None, None, None)
    monkeypatch.setattr(gateway, "_service", lambda **kwargs: classroom_service)
    return (
        BatchService(batch_repository, gateway),
        batch_repository,
        repository,
        generation,
        created,
    )


@pytest.mark.asyncio
async def test_batch_gateway_recovers_confirmation_after_precommit_requeue_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    (
        service,
        batch_repository,
        repository,
        generation,
        created,
    ) = await _batch_confirmation_recovery_fixture(context, monkeypatch)
    reviewed_revision = created.revision
    reviewed_sha256 = canonical_outline_sha256(OutlineBundle.model_validate(created.outline))
    generation.content_error = RuntimeError("content requeue failed before commit")

    with pytest.raises(RuntimeError, match="before commit"):
        await service.confirm_outline(
            context,
            batch_repository.batch.id,
            "item-a",
            revision=reviewed_revision,
            outline_sha256=reviewed_sha256,
        )

    persisted = repository.records[created.asset_id]
    assert persisted.lifecycle_state == "generating_content"
    assert persisted.revision == reviewed_revision + 1
    assert batch_repository.batch.items[0].status == "awaiting_confirmation"
    generation.content_error = None

    recovered = await service.confirm_outline(
        context,
        batch_repository.batch.id,
        "item-a",
        revision=reviewed_revision,
        outline_sha256=reviewed_sha256,
    )

    assert recovered.items[0].status == "queued"
    assert repository.records[created.asset_id].revision == reviewed_revision + 1
    assert len(generation.content_calls) == 2


@pytest.mark.asyncio
async def test_batch_gateway_recovery_rejects_tampered_persisted_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    (
        service,
        batch_repository,
        repository,
        generation,
        created,
    ) = await _batch_confirmation_recovery_fixture(context, monkeypatch)
    reviewed_revision = created.revision
    reviewed_sha256 = canonical_outline_sha256(OutlineBundle.model_validate(created.outline))
    generation.content_error = RuntimeError("content requeue failed before commit")
    with pytest.raises(RuntimeError, match="before commit"):
        await service.confirm_outline(
            context,
            batch_repository.batch.id,
            "item-a",
            revision=reviewed_revision,
            outline_sha256=reviewed_sha256,
        )
    persisted = repository.records[created.asset_id]
    confirmed = OutlineBundle.model_validate(persisted.outline)
    tampered = confirmed.model_copy(update={"title": "Tampered confirmed outline"})
    repository.records[created.asset_id] = replace(
        persisted,
        outline=tampered.model_dump(mode="json", by_alias=True, exclude_none=True),
        confirmed_outline_sha256=canonical_outline_sha256(tampered),
    )
    generation.content_error = None

    with pytest.raises(BatchOutlineConflict):
        await service.confirm_outline(
            context,
            batch_repository.batch.id,
            "item-a",
            revision=reviewed_revision,
            outline_sha256=reviewed_sha256,
        )


class _ConcurrentConfirmationRepository(_Repository):
    def __init__(self) -> None:
        super().__init__()
        self._arrived = 0
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()

    async def confirm_outline(
        self,
        asset_id: str,
        outline: dict,
        confirmed_outline_sha256: str,
        source_outline_sha256: str,
    ):
        assert len(source_outline_sha256) == 64
        self._arrived += 1
        if self._arrived == 2:
            self._ready.set()
        await self._ready.wait()
        async with self._lock:
            current = self.records[asset_id]
            if current.confirmed_outline_sha256 is None:
                self.records[asset_id] = replace(
                    current,
                    lifecycle_state="generating_content",
                    status="queued",
                    outline=outline,
                    confirmed_outline_sha256=confirmed_outline_sha256,
                )
            return self.records[asset_id]


@pytest.mark.asyncio
async def test_concurrent_duplicate_confirmations_reuse_server_canonical_payload() -> None:
    context = _context()
    repository = _ConcurrentConfirmationRepository()
    generation = _Generation()
    times = iter((NOW, NOW + timedelta(seconds=1)))
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        generation,
        None,
        clock=lambda: next(times),
    )
    created = await service.create(context, _request())

    first, second = await asyncio.gather(
        service.confirm_outline(context, created.asset_id),
        service.confirm_outline(context, created.asset_id),
    )

    assert first.confirmed_outline_sha256 == second.confirmed_outline_sha256
    persisted = repository.records[created.asset_id]
    assert generation.content_calls == [
        (
            OutlineBundle.model_validate(persisted.outline),
            persisted.confirmed_outline_sha256,
        ),
        (
            OutlineBundle.model_validate(persisted.outline),
            persisted.confirmed_outline_sha256,
        ),
    ]


class _LostRequeueResponseRepository:
    def __init__(self, details: GenerationJobDetails) -> None:
        self.details = details
        self.requeue_calls = 0

    async def get_job_details(self, tenant_id: str, job_id: str):
        assert (tenant_id, job_id) == (self.details.tenant_id, self.details.job_id)
        return self.details

    async def requeue_confirmed_content(
        self,
        tenant_id: str,
        job_id: str,
        *,
        request_payload: str,
        request_sha256: str,
    ) -> bool:
        assert (tenant_id, job_id) == (self.details.tenant_id, self.details.job_id)
        self.requeue_calls += 1
        self.details = replace(
            self.details,
            phase="content",
            status="queued",
            request_payload=request_payload,
            request_sha256=request_sha256,
        )
        raise RuntimeError("requeue response lost")


def _generation_details(
    *,
    context: TenantContext,
    created: ClassroomRecord,
    outline: OutlineBundle,
) -> GenerationJobDetails:
    assert created.job_id is not None
    assert created.teaching_brief is not None
    request = GenerationRequest(
        schema_version="1.0",
        tenant_id=context.tenant_id,
        request_id="request-classroom-recovery",
        job_id=created.job_id,
        idempotency_key=f"classroom-outline-{created.asset_id}",
        phase="outline",
        classroom_mode="full",
        teaching_brief_id=created.teaching_brief.brief_id,
        teaching_brief_sha256=created.teaching_brief.content_sha256,
        teaching_brief=created.teaching_brief,
        confirmed_outline=None,
        confirmed_outline_sha256=None,
        template_id=created.teaching_brief.template_policy.template_id,
        template_version=created.teaching_brief.template_policy.template_version,
        scene_budget=15,
        duration_minutes=created.teaching_brief.duration_minutes,
        requested_exports=["classroom_zip"],
        callback_context=created.draft_id,
        data_plane_route_id="route-a",
        priority="teacher",
    )
    payload = json.dumps(
        request.model_dump(mode="json", by_alias=True, exclude_none=False),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return GenerationJobDetails(
        tenant_id=context.tenant_id,
        job_id=created.job_id,
        job_kind="generation",
        phase="outline",
        export_format=None,
        status="awaiting_confirmation",
        priority=20,
        quota_units=45,
        actor_id=context.user_id,
        owner_id=context.user_id,
        visibility="class",
        request_id=request.request_id,
        idempotency_key=request.idempotency_key,
        classroom_draft_id=created.draft_id,
        batch_id=None,
        resource_course_id=created.course_id,
        resource_class_id=created.class_id,
        public_request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        data_plane_mode="shared",
        data_plane_route_id="route-a",
        provider_profile_id="provider-a",
        worker_pool_ref="workers-a",
        queue_ref="queue-a",
        request_payload=payload,
        progress_percent=50,
        attempt_count=0,
        waiting_reason=None,
        cancel_requested=False,
        error_category=None,
        error_code=None,
        result_payload=canonical_json_bytes(outline).decode(),
        result_ref=None,
        retry_of_job_id=None,
    )


@pytest.mark.asyncio
async def test_content_requeue_retry_recovers_after_committed_response_is_lost() -> None:
    context = _context()
    service, _, _ = _service(context)
    created = await service.create(context, _request())
    asset_id = "asset-confirmation-recovery"
    job_id = SqlAlchemyClassroomGeneration._job_id(context.tenant_id, asset_id)
    created = replace(
        created,
        asset_id=asset_id,
        draft_id="draft-confirmation-recovery",
        job_id=job_id,
    )
    issued = OutlineBundle.model_validate(created.outline).model_copy(
        update={"outline_id": f"outline-{job_id}"}
    )
    confirmed = issued.model_copy(
        update={
            "confirmation_metadata": OutlineConfirmationMetadata(
                status="confirmed",
                confirmed_at=NOW,
                confirmed_by=context.user_id,
            )
        }
    )
    repository = _LostRequeueResponseRepository(
        _generation_details(context=context, created=created, outline=issued)
    )
    generation = SqlAlchemyClassroomGeneration(repository, object())
    digest = canonical_outline_sha256(confirmed)

    with pytest.raises(RuntimeError, match="response lost"):
        await generation.start_content(
            context=context,
            asset_id=created.asset_id,
            draft_id=created.draft_id,
            job_id=job_id,
            confirmed_outline=confirmed,
            confirmed_outline_sha256=digest,
        )

    recovered = await generation.start_content(
        context=context,
        asset_id=created.asset_id,
        draft_id=created.draft_id,
        job_id=job_id,
        confirmed_outline=confirmed,
        confirmed_outline_sha256=digest,
    )

    assert recovered.status == "queued"
    assert repository.requeue_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["asset", "draft"])
async def test_content_confirmation_rejects_mismatched_classroom_binding(
    binding: str,
) -> None:
    context = _context()
    service, _, _ = _service(context)
    created = await service.create(context, _request())
    asset_id = "asset-confirmation-binding"
    job_id = SqlAlchemyClassroomGeneration._job_id(context.tenant_id, asset_id)
    created = replace(
        created,
        asset_id=asset_id,
        draft_id="draft-confirmation-binding",
        job_id=job_id,
    )
    issued = OutlineBundle.model_validate(created.outline).model_copy(
        update={"outline_id": f"outline-{job_id}"}
    )
    confirmed = issued.model_copy(
        update={
            "confirmation_metadata": OutlineConfirmationMetadata(
                status="confirmed",
                confirmed_at=NOW,
                confirmed_by=context.user_id,
            )
        }
    )
    repository = _LostRequeueResponseRepository(
        _generation_details(context=context, created=created, outline=issued)
    )
    generation = SqlAlchemyClassroomGeneration(repository, object())

    with pytest.raises(InvalidClassroomState, match="binding"):
        await generation.start_content(
            context=context,
            asset_id="asset-other" if binding == "asset" else created.asset_id,
            draft_id="draft-other" if binding == "draft" else created.draft_id,
            job_id=job_id,
            confirmed_outline=confirmed,
            confirmed_outline_sha256=canonical_outline_sha256(confirmed),
        )

    assert repository.requeue_calls == 0


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
        classroom_version_id="classroom-version-validation",
        document=_canonical_document(
            classroom_id=created.asset_id,
            classroom_version_id="classroom-version-validation",
            title="Unsafe",
            interactive_html="<script>alert(1)</script>",
        ),
    )

    result = await service.validate(context, created.asset_id)

    assert result.validation_report["valid"] is False
    assert result.validation_report["severeFindings"]
    assert result.validation_report["warnings"]
    assert result.validation_report["draftRevision"] == current.revision
    assert (
        result.validation_report["documentSha256"]
        == hashlib.sha256(
            canonical_json_bytes(repository.records[created.asset_id].document)
        ).hexdigest()
    )
    assert repository.validation_reports == [result.validation_report]


class _StaleValidationRepository(_Repository):
    async def save_validation_report(
        self,
        asset_id,
        report,
        report_sha256,
        expected_revision,
        expected_document_sha256,
    ):
        current = self.records[asset_id]
        self.records[asset_id] = replace(
            current,
            revision=current.revision + 1,
            document=_canonical_document(
                classroom_id=current.asset_id,
                classroom_version_id=current.classroom_version_id or "missing",
                title="New draft",
            ),
            validation_report=None,
        )
        return None


@pytest.mark.asyncio
async def test_validation_report_cannot_commit_after_concurrent_draft_update() -> None:
    context = _context()
    repository = _StaleValidationRepository()
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        _Generation(),
        None,
        clock=lambda: NOW,
    )
    created = await service.create(context, _request())
    current = repository.records[created.asset_id]
    repository.records[created.asset_id] = replace(
        current,
        lifecycle_state="editing",
        status="succeeded",
        classroom_version_id="classroom-version-validation",
        document=_canonical_document(
            classroom_id=created.asset_id,
            classroom_version_id="classroom-version-validation",
            title="Old draft",
        ),
    )

    with pytest.raises(ClassroomRevisionConflict, match="stale"):
        await service.validate(context, created.asset_id)

    latest = repository.records[created.asset_id]
    assert latest.validation_report is None
    assert latest.document["openmaic"]["scenes"][0]["title"] == "New draft"


@pytest.mark.asyncio
async def test_draft_update_recomputes_a_stale_but_well_formed_file_hash() -> None:
    context = _context()
    service, repository, _ = _service(context)
    created = await service.create(context, _request())
    version_id = "classroom-version-edit"
    current = replace(
        repository.records[created.asset_id],
        lifecycle_state="editing",
        status="succeeded",
        classroom_version_id=version_id,
        document=_canonical_document(
            classroom_id=created.asset_id,
            classroom_version_id=version_id,
            title="Before edit",
        ),
    )
    repository.records[created.asset_id] = current
    edited = _canonical_document(
        classroom_id=created.asset_id,
        classroom_version_id=version_id,
        title="After edit",
    )
    edited["fileSha256"] = "f" * 64

    updated = await service.update_draft(
        context,
        created.asset_id,
        edited,
        current.revision,
    )

    unhashed = dict(updated.document)
    file_sha256 = unhashed.pop("fileSha256")
    assert file_sha256 != "f" * 64
    assert file_sha256 == hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    assert repository.records[created.asset_id].document == updated.document


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_kind",
    [
        "snake_case_hash",
        "duplicate_hash_alias",
        "snake_case_field",
        "explicit_none",
        "extra_field",
        "unsafe_reference",
    ],
)
async def test_draft_update_rehash_only_relaxes_the_hash_value(
    invalid_kind: str,
) -> None:
    context = _context()
    service, repository, _ = _service(context)
    created = await service.create(context, _request())
    version_id = "classroom-version-edit"
    original = _canonical_document(
        classroom_id=created.asset_id,
        classroom_version_id=version_id,
        title="Before edit",
    )
    current = replace(
        repository.records[created.asset_id],
        lifecycle_state="editing",
        status="succeeded",
        classroom_version_id=version_id,
        document=original,
    )
    repository.records[created.asset_id] = current
    edited = _canonical_document(
        classroom_id=created.asset_id,
        classroom_version_id=version_id,
        title="Rejected representation",
    )
    if invalid_kind == "snake_case_hash":
        edited["file_sha256"] = edited.pop("fileSha256")
    elif invalid_kind == "duplicate_hash_alias":
        edited["file_sha256"] = edited["fileSha256"]
    elif invalid_kind == "snake_case_field":
        edited["schema_version"] = edited.pop("schemaVersion")
    elif invalid_kind == "explicit_none":
        audit_metadata = edited["auditMetadata"]
        assert isinstance(audit_metadata, dict)
        audit_metadata["parentClassroomVersionId"] = None
    elif invalid_kind == "extra_field":
        edited["unexpectedField"] = True
    else:
        openmaic = edited["openmaic"]
        assert isinstance(openmaic, dict)
        scenes = openmaic["scenes"]
        assert isinstance(scenes, list) and isinstance(scenes[0], dict)
        scenes[0]["content"] = {
            "type": "slide",
            "canvas": {"src": "https://attacker.invalid/image.png"},
        }

    with pytest.raises(InvalidDraftDocument):
        await service.update_draft(
            context,
            created.asset_id,
            edited,
            current.revision,
        )

    assert repository.records[created.asset_id].document == original


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
    assert media.relative_path == f"media/{media.id}.png"
    assert media.object_key.endswith(media.relative_path)
    assert upload.closed is True
    assert len(repository.media) == 1
    assert len(store.put_calls) == 1
    streamed = await service.get_media(context, created.asset_id, media.id)
    assert streamed is not None
    assert b"".join([chunk async for chunk in streamed.body]) == body
    assert repository.version_media_calls == []


@pytest.mark.asyncio
async def test_authorized_draft_media_read_falls_back_to_exact_bound_version_media() -> None:
    context = _context()
    service, repository, store = _service_with_store(context)
    created = await service.create(context, _request())
    media_id = "generated-media-1"
    body = b"\x89PNG\r\n\x1a\ngenerated"
    object_key = (
        f"tenants/{context.tenant_id}/classrooms/{created.asset_id}/versions/1/media/{media_id}.png"
    )
    repository.version_media[(created.asset_id, media_id)] = SimpleNamespace(
        id=media_id,
        classroom_id=created.asset_id,
        relative_path=f"media/{media_id}.png",
        mime_type="image/png",
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        object_key=object_key,
    )
    store.content[object_key] = body

    media = await service.get_media(context, created.asset_id, media_id)

    assert media is not None
    assert media.id == media_id
    assert b"".join([chunk async for chunk in media.body]) == body
    assert repository.version_media_calls == [(created.asset_id, media_id)]

    denied = await service.get_media(
        _context(user_id="teacher-b", scope_id="class-b"),
        created.asset_id,
        media_id,
    )
    assert denied is None
    assert repository.version_media_calls == [(created.asset_id, media_id)]


@pytest.mark.asyncio
async def test_teacher_media_read_hides_a_student_classroom_marker() -> None:
    context = _context()
    service, repository, store = _service_with_store(context)
    created = await service.create(context, _request())
    media_id = "generated-media-student"
    repository.records[created.asset_id] = replace(
        repository.records[created.asset_id],
        owner_id="student-a",
        student_generation_request_id="student-request-a",
    )
    repository.version_media[(created.asset_id, media_id)] = SimpleNamespace(
        id=media_id,
        classroom_id=created.asset_id,
        relative_path=f"media/{media_id}.png",
        mime_type="image/png",
        sha256="a" * 64,
        size_bytes=1,
        object_key="must-not-open",
    )

    hidden = await service.get_media(context, created.asset_id, media_id)

    assert hidden is None
    assert repository.version_media_calls == []
    assert "must-not-open" not in store.content


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("classroomId", "asset-b"),
        ("classroomVersionId", "version-b"),
    ],
)
def test_bound_version_media_requires_document_asset_and_base_version_binding(
    field_name: str,
    value: str,
) -> None:
    document = _canonical_document(
        classroom_id="asset-a",
        classroom_version_id="version-a",
    )
    document[field_name] = value
    unhashed = dict(document)
    unhashed.pop("fileSha256")
    document["fileSha256"] = hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    encoded = canonical_json_bytes(document).decode("utf-8")
    draft = SimpleNamespace(
        classroom_id="asset-a",
        base_version_id="version-a",
        document=encoded,
        document_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )

    assert SqlAlchemyClassroomRepository._verified_draft_document(draft) is None


def test_available_and_read_media_share_exact_canonical_version_binding() -> None:
    media_id = "media-generated"
    payload = valid_classroom_document()
    payload["classroom_id"] = "asset-a"
    payload["classroom_version_id"] = "version-a"
    payload["export_manifest"] = []
    manifest = payload["media_manifest"]
    assert isinstance(manifest, list) and isinstance(manifest[0], dict)
    manifest[0].update(
        media_id=media_id,
        relative_path="media/generated.png",
        mime_type="image/png",
        sha256="a" * 64,
        size_bytes=8,
    )
    document = ClassroomDocument.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    unhashed = dict(document)
    unhashed.pop("fileSha256")
    document["fileSha256"] = hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    encoded = canonical_json_bytes(document).decode()
    draft = SimpleNamespace(
        tenant_id="tenant-a",
        classroom_id="asset-a",
        base_version_id="version-a",
        document=encoded,
        document_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
    )
    version = SimpleNamespace(
        tenant_id="tenant-a",
        id="version-a",
        classroom_id="asset-a",
        version_number=1,
    )
    artifact = SimpleNamespace(
        id="artifact-a",
        tenant_id="tenant-a",
        classroom_version_id="version-a",
        artifact_kind="media",
        relative_name="media/generated.png",
        mime_type="image/png",
        sha256="a" * 64,
        size_bytes=8,
        object_key=classroom_artifact_key(
            "tenant-a",
            "asset-a",
            1,
            "media/generated.png",
        ),
    )
    repository = object.__new__(SqlAlchemyClassroomRepository)
    repository._tenant_id = "tenant-a"

    resolved = repository._verified_bound_version_media(
        "asset-a",
        draft,
        version,
        (artifact,),
    )

    assert resolved is not None and resolved[0].id == media_id
    assert (
        repository._verified_bound_version_media(
            "asset-a",
            draft,
            version,
            (
                SimpleNamespace(
                    **{
                        **vars(artifact),
                        "object_key": "tenants/tenant-a/other",
                    }
                ),
            ),
        )
        is None
    )
    assert (
        repository._verified_bound_version_media(
            "asset-a",
            draft,
            version,
            (artifact, artifact),
        )
        is None
    )


@pytest.mark.parametrize(
    ("mime_type", "suffix"),
    [
        ("audio/x-wav", ".wav"),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".pptx",
        ),
    ],
)
def test_draft_media_relative_path_uses_the_upload_mime_mapping(
    mime_type: str,
    suffix: str,
) -> None:
    from deeptutor.teaching.services.classrooms import draft_media_relative_path

    media_id = "media-0123456789abcdef0123456789abcdef"

    assert draft_media_relative_path(media_id, mime_type) == f"media/{media_id}{suffix}"


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


class _AfterWriteFailureStore(_Store):
    def __init__(self, *, cleanup_failures: int = 0) -> None:
        super().__init__()
        self.cleanup_failures = cleanup_failures
        self.reconcile_calls: list[tuple[str, str]] = []
        self.delete_calls: list[StoredArtifact] = []

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
        self.content[key] = payload
        self.put_calls.append((key, content_type, ownership_token))
        raise RuntimeError("object store response lost")

    async def reconcile_verified(
        self,
        key,
        sha256,
        size,
        *,
        content_type,
        ownership_token,
    ):
        self.reconcile_calls.append((key, ownership_token))
        return await super().reconcile_verified(
            key,
            sha256,
            size,
            content_type=content_type,
            ownership_token=ownership_token,
        )

    async def delete_owned(self, artifact: StoredArtifact) -> None:
        self.delete_calls.append(artifact)
        if self.cleanup_failures:
            self.cleanup_failures -= 1
            raise RuntimeError("cleanup unavailable")
        await super().delete_owned(artifact)


class _CleanupListingRepository(_Repository):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_list_calls: list[tuple[str, int]] = []

    async def list_cleanup_pending(self, asset_id: str, *, limit: int = 8):
        self.cleanup_list_calls.append((asset_id, limit))
        return tuple(
            receipt
            for (classroom_id, _), receipt in self.media.items()
            if classroom_id == asset_id and receipt.status == "cleanup_pending"
        )[:limit]


def _seed_cleanup_pending(
    repository: _Repository,
    store: _Store,
    asset_id: str,
    index: int = 0,
) -> DraftMediaRecord:
    media_id = f"media-{index:032x}"
    body = b"\x89PNG\r\n\x1a\nimage-" + str(index).encode()
    receipt = DraftMediaRecord(
        id=media_id,
        classroom_id=asset_id,
        mime_type="image/png",
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        object_key=(f"tenants/tenant-a/temporary/draft-{asset_id}/media/{media_id}.png"),
        ownership_token=f"{index + 1:032x}",
        object_revision=None,
        status="cleanup_pending",
        last_error_code="upload_failed",
    )
    repository.media[(asset_id, media_id)] = receipt
    repository.media_status[(asset_id, media_id)] = "cleanup_pending"
    store.content[receipt.object_key] = body
    return receipt


def _service_with_custom_store(context: TenantContext, store: _Store):
    repository = _Repository()
    return (
        ClassroomService(
            repository,
            TeachingBriefBuilder(context, object()),
            _Generation(),
            _StoreProvider(store),
            clock=lambda: NOW,
        ),
        repository,
    )


@pytest.mark.asyncio
async def test_authorized_asset_read_recovers_pending_media_without_media_id() -> None:
    context = _context()
    repository = _CleanupListingRepository()
    store = _AfterWriteFailureStore(cleanup_failures=1)
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        _Generation(),
        _StoreProvider(store),
        clock=lambda: NOW,
    )
    created = await service.create(context, _request())
    repository.records[created.asset_id] = replace(
        repository.records[created.asset_id],
        job_id=None,
        lifecycle_state="editing",
        status="succeeded",
    )
    body = b"\x89PNG\r\n\x1a\nimage"
    with pytest.raises(RuntimeError, match="response lost"):
        await service.upload_media(
            context,
            created.asset_id,
            _Upload(body, "image/png"),
            hashlib.sha256(body).hexdigest(),
        )
    ((asset_id, media_id), _) = next(iter(repository.media.items()))
    assert repository.media_status[(asset_id, media_id)] == "cleanup_pending"

    result = await service.get(context, created.asset_id)

    assert result is not None
    assert repository.cleanup_list_calls == [(created.asset_id, 8)]
    assert repository.media_status[(asset_id, media_id)] == "failed"
    assert store.content == {}


@pytest.mark.asyncio
async def test_unauthorized_asset_read_does_not_probe_or_reconcile_pending_media() -> None:
    owner_context = _context()
    repository = _CleanupListingRepository()
    store = _AfterWriteFailureStore()
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(owner_context, object()),
        _Generation(),
        _StoreProvider(store),
        clock=lambda: NOW,
    )
    created = await service.create(owner_context, _request())
    receipt = _seed_cleanup_pending(repository, store, created.asset_id)

    result = await service.get(
        _context(user_id="teacher-b", scope_id="class-b"),
        created.asset_id,
    )

    assert result is None
    assert repository.cleanup_list_calls == []
    assert store.reconcile_calls == []
    assert receipt.object_key in store.content


@pytest.mark.asyncio
async def test_authorized_asset_read_bounds_and_idempotently_retries_cleanup() -> None:
    context = _context()
    repository = _CleanupListingRepository()
    store = _AfterWriteFailureStore()
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        _Generation(),
        _StoreProvider(store),
        clock=lambda: NOW,
    )
    created = await service.create(context, _request())
    repository.records[created.asset_id] = replace(
        repository.records[created.asset_id],
        job_id=None,
        lifecycle_state="editing",
        status="succeeded",
    )
    receipts = [
        _seed_cleanup_pending(repository, store, created.asset_id, index) for index in range(9)
    ]

    assert await service.get(context, created.asset_id) is not None
    assert len(store.reconcile_calls) == 8
    assert repository.media_status[(created.asset_id, receipts[-1].id)] == ("cleanup_pending")

    assert await service.get(context, created.asset_id) is not None
    assert len(store.reconcile_calls) == 9
    assert repository.media_status[(created.asset_id, receipts[-1].id)] == "failed"

    assert await service.get(context, created.asset_id) is not None
    assert len(store.reconcile_calls) == 9
    assert repository.cleanup_list_calls == [(created.asset_id, 8)] * 3


@pytest.mark.asyncio
async def test_opportunistic_cleanup_failure_does_not_block_authorized_read() -> None:
    context = _context()
    repository = _CleanupListingRepository()
    store = _AfterWriteFailureStore(cleanup_failures=1)
    service = ClassroomService(
        repository,
        TeachingBriefBuilder(context, object()),
        _Generation(),
        _StoreProvider(store),
        clock=lambda: NOW,
    )
    created = await service.create(context, _request())
    repository.records[created.asset_id] = replace(
        repository.records[created.asset_id],
        job_id=None,
        lifecycle_state="editing",
        status="succeeded",
    )
    receipt = _seed_cleanup_pending(repository, store, created.asset_id)

    assert await service.get(context, created.asset_id) is not None
    assert repository.media_status[(created.asset_id, receipt.id)] == "cleanup_pending"

    assert await service.get(context, created.asset_id) is not None
    assert repository.media_status[(created.asset_id, receipt.id)] == "failed"


@pytest.mark.asyncio
async def test_media_after_write_failure_reconciles_owned_key_without_guessing() -> None:
    context = _context()
    store = _AfterWriteFailureStore()
    service, repository = _service_with_custom_store(context, store)
    created = await service.create(context, _request())
    body = b"\x89PNG\r\n\x1a\nimage"

    with pytest.raises(RuntimeError, match="response lost"):
        await service.upload_media(
            context,
            created.asset_id,
            _Upload(body, "image/png"),
            hashlib.sha256(body).hexdigest(),
        )

    ((asset_id, media_id), receipt) = next(iter(repository.media.items()))
    assert asset_id == created.asset_id
    assert repository.cleanup_transitions == ["cleanup_pending", "failed"]
    assert repository.media_status[(asset_id, media_id)] == "failed"
    assert store.reconcile_calls == [(receipt.object_key, receipt.ownership_token)]
    assert [artifact.key for artifact in store.delete_calls] == [receipt.object_key]
    assert store.content == {}
    assert receipt.object_key not in repr(receipt)


@pytest.mark.asyncio
async def test_media_cleanup_failure_stays_pending_and_can_retry_idempotently() -> None:
    context = _context()
    store = _AfterWriteFailureStore(cleanup_failures=1)
    service, repository = _service_with_custom_store(context, store)
    created = await service.create(context, _request())
    body = b"\x89PNG\r\n\x1a\nimage"

    with pytest.raises(RuntimeError, match="response lost"):
        await service.upload_media(
            context,
            created.asset_id,
            _Upload(body, "image/png"),
            hashlib.sha256(body).hexdigest(),
        )

    ((asset_id, media_id), _) = next(iter(repository.media.items()))
    assert repository.media_status[(asset_id, media_id)] == "cleanup_pending"
    assert await service.reconcile_media_cleanup(context, asset_id, media_id) is True
    assert repository.media_status[(asset_id, media_id)] == "failed"
    assert store.content == {}
    assert len(store.reconcile_calls) == 2


class _BlockingAfterWriteStore(_AfterWriteFailureStore):
    def __init__(self) -> None:
        super().__init__()
        self.written = asyncio.Event()
        self.release = asyncio.Event()

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
        self.content[key] = payload
        self.put_calls.append((key, content_type, ownership_token))
        self.written.set()
        await self.release.wait()
        raise AssertionError("canceled upload resumed")


class _SentinelCancellationStore(_AfterWriteFailureStore):
    def __init__(self) -> None:
        super().__init__()
        self.cancellation = asyncio.CancelledError("client disconnected")

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
        self.content[key] = payload
        self.put_calls.append((key, content_type, ownership_token))
        raise self.cancellation


@pytest.mark.asyncio
async def test_media_upload_reraises_the_original_cancellation_instance() -> None:
    context = _context()
    store = _SentinelCancellationStore()
    service, repository = _service_with_custom_store(context, store)
    created = await service.create(context, _request())
    body = b"\x89PNG\r\n\x1a\nimage"

    with pytest.raises(asyncio.CancelledError) as caught:
        await service.upload_media(
            context,
            created.asset_id,
            _Upload(body, "image/png"),
            hashlib.sha256(body).hexdigest(),
        )

    assert caught.value is store.cancellation
    ((asset_id, media_id), _) = next(iter(repository.media.items()))
    assert repository.media_status[(asset_id, media_id)] == "failed"
    assert store.content == {}


@pytest.mark.asyncio
async def test_media_upload_cancellation_shields_owned_cleanup() -> None:
    context = _context()
    store = _BlockingAfterWriteStore()
    service, repository = _service_with_custom_store(context, store)
    created = await service.create(context, _request())
    body = b"\x89PNG\r\n\x1a\nimage"
    task = asyncio.create_task(
        service.upload_media(
            context,
            created.asset_id,
            _Upload(body, "image/png"),
            hashlib.sha256(body).hexdigest(),
        )
    )
    await store.written.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    ((asset_id, media_id), _) = next(iter(repository.media.items()))
    assert repository.media_status[(asset_id, media_id)] == "failed"
    assert repository.cleanup_transitions == ["cleanup_pending", "failed"]
    assert store.content == {}
