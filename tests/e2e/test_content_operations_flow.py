"""Deterministic acceptance flow for content-operations batch publishing."""

from __future__ import annotations

from dataclasses import replace

import pytest

from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.contracts import canonical_outline_sha256
from deeptutor.teaching.models.classrooms import transition
from deeptutor.teaching.permissions import permissions_for_roles
from deeptutor.teaching.services.batches import (
    BatchItemInput,
    BatchItemRejected,
    BatchService,
)
from deeptutor.teaching.services.classrooms import ClassroomService
from deeptutor.teaching.services.publications import PublicationService
from deeptutor.teaching.services.reviews import (
    ReviewAccessDenied,
    ReviewService,
)
from deeptutor.teaching.tenant_context import TenantContext
from tests.e2e.test_teacher_classroom_flow import (
    NOW,
    ClassroomMemoryGeneration,
    ClassroomMemoryRepository,
    _FlowLedger,
    _LedgerPublicationMaterializer,
    _LedgerPublicationRepository,
    _LedgerReviewRepository,
    classroom_request,
)
from tests.teaching.test_batch_service import _BatchRepository as BatchMemoryRepository
from tests.teaching.test_batch_service import _Jobs as BatchMemoryJobs
from tests.teaching.test_classroom_service import _canonical_document


def _context(user_id: str, roles: set[str]) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant_a",
        user_id=user_id,
        permissions=permissions_for_roles(
            roles,
            scope_type="tenant",
            scope_id="tenant-a",
            tenant_id="tenant-a",
        ),
    )


class _BatchRepository(BatchMemoryRepository):
    async def rebind_failed_item(
        self,
        batch_id,
        item_id,
        *,
        expected_job_id,
        new_job_id,
    ):
        assert self.batch is not None and self.batch.id == batch_id
        current = next(item for item in self.batch.items if item.id == item_id)
        assert current.status == "failed"
        assert current.generation_job_id == expected_job_id
        status = self.job_statuses.get(new_job_id, "queued")
        self.batch = replace(
            self.batch,
            items=tuple(
                replace(
                    item,
                    generation_job_id=new_job_id,
                    status=status,
                )
                if item.id == item_id
                else item
                for item in self.batch.items
            ),
        )
        await self.set_item_status(batch_id, item_id, status)
        assert self.batch is not None
        return next(item for item in self.batch.items if item.id == item_id)


class _ClassroomRepository(ClassroomMemoryRepository):
    async def mark_generation_succeeded(self, asset_id: str, job_id: str):
        record = self.records[asset_id]
        assert record.job_id == job_id
        return record


class _BatchGeneration(ClassroomMemoryGeneration):
    def __init__(self) -> None:
        super().__init__()
        self.stages = {}

    async def start_outline(self, **kwargs):
        stage = await super().start_outline(**kwargs)
        unique = replace(stage, job_id=f"job-{kwargs['asset_id']}")
        self.stages[kwargs["asset_id"]] = unique
        return unique

    async def get_stage(self, *, context, job_id):
        del context
        return next(stage for stage in self.stages.values() if stage.job_id == job_id)

    async def start_content(self, **kwargs):
        stage = await super().start_content(**kwargs)
        queued = replace(stage, job_id=kwargs["job_id"])
        self.stages[kwargs["asset_id"]] = queued
        return queued

    def complete(
        self,
        repository: _ClassroomRepository,
        asset_id: str,
        *,
        succeeded: bool,
    ) -> None:
        record = repository.records[asset_id]
        if not succeeded:
            self.stages[asset_id] = replace(
                self.stages[asset_id],
                status="failed",
            )
            return
        version_id = f"generated-{asset_id}"
        document = _canonical_document(
            classroom_id=asset_id,
            classroom_version_id=version_id,
            title=f"Generated {record.title}",
        )
        repository.records[asset_id] = replace(
            record,
            lifecycle_state=transition("generating_content", "editing"),
            status="succeeded",
            classroom_version_id=version_id,
            document=document,
        )
        self.stages[asset_id] = replace(
            self.stages[asset_id],
            status="succeeded",
            classroom_version_id=version_id,
        )

    def retry(self, repository: _ClassroomRepository, asset_id: str, job_id: str) -> None:
        record = repository.records[asset_id]
        repository.records[asset_id] = replace(record, job_id=job_id, status="queued")
        self.stages[asset_id] = replace(
            self.stages[asset_id],
            job_id=job_id,
            status="queued",
            classroom_version_id=None,
        )


class _BatchClassrooms:
    def __init__(
        self,
        service: ClassroomService,
        jobs: "_BatchJobs",
    ) -> None:
        self.service = service
        self.jobs = jobs
        self.assets_by_item: dict[str, str] = {}

    async def create(
        self,
        context,
        request,
        *,
        batch_id,
        item_id,
        retry_of_job_id=None,
    ):
        if retry_of_job_id is not None:
            raise BatchItemRejected("rejected replay is not used in this flow")
        record = await self.service.create(
            context,
            request,
            idempotency_key=f"{batch_id}:{item_id}",
        )
        self.assets_by_item[item_id] = record.asset_id
        self.jobs.created(item_id)
        return record

    async def get(self, context, asset_id):
        return await self.service.get(context, asset_id)

    async def confirm_outline(
        self,
        context,
        asset_id,
        *,
        expected_revision,
        expected_outline_sha256,
    ):
        return await self.service.confirm_outline(
            context,
            asset_id,
            expected_revision=expected_revision,
            expected_outline_sha256=expected_outline_sha256,
        )


class _BatchJobs(BatchMemoryJobs):
    def __init__(
        self,
        generation: _BatchGeneration,
        repository: _ClassroomRepository,
    ) -> None:
        super().__init__()
        self.generation = generation
        self.repository = repository
        self.classrooms: _BatchClassrooms | None = None
        self.retry_calls: list[tuple[str, str, str, str]] = []

    async def retry(self, context, *, batch_id, item_id, job_id):
        retried = await super().retry(
            context,
            batch_id=batch_id,
            item_id=item_id,
            job_id=job_id,
        )
        assert self.classrooms is not None
        self.retry_calls.append((batch_id, item_id, job_id, retried))
        self.generation.retry(
            self.repository,
            self.classrooms.assets_by_item[item_id],
            retried,
        )
        return retried


class _BatchWorker:
    def __init__(
        self,
        repository: BatchMemoryRepository,
        generation: _BatchGeneration,
        classrooms: _BatchClassrooms,
        classroom_repository: _ClassroomRepository,
    ) -> None:
        self.repository = repository
        self.generation = generation
        self.classrooms = classrooms
        self.classroom_repository = classroom_repository

    async def complete(self, batch_id: str, item_id: str, *, succeeded: bool) -> None:
        self.generation.complete(
            self.classroom_repository,
            self.classrooms.assets_by_item[item_id],
            succeeded=succeeded,
        )
        await self.repository.set_item_status(
            batch_id,
            item_id,
            "succeeded" if succeeded else "failed",
        )


async def _run_content_operations_flow() -> dict[str, object]:
    author = _context("author-a", {"content_author"})
    reviewer = _context("reviewer-a", {"content_reviewer"})
    publisher = _context("publisher-a", {"org_admin"})
    classrooms_repository = _ClassroomRepository()
    generation = _BatchGeneration()
    classrooms = ClassroomService(
        classrooms_repository,
        TeachingBriefBuilder(author, object()),
        generation,
        None,
        clock=lambda: NOW,
    )
    jobs = _BatchJobs(generation, classrooms_repository)
    classroom_gateway = _BatchClassrooms(classrooms, jobs)
    jobs.classrooms = classroom_gateway
    batch_repository = _BatchRepository()
    batches = BatchService(batch_repository, classroom_gateway, jobs)
    worker = _BatchWorker(
        batch_repository,
        generation,
        classroom_gateway,
        classrooms_repository,
    )
    item_ids = ("lesson-a", "lesson-b", "lesson-c")
    created = await batches.create(
        author,
        tuple(
            BatchItemInput(item_id, classroom_request(title=f"Lesson {item_id[-1].upper()}"))
            for item_id in item_ids
        ),
        idempotency_key="content-operations-flow",
    )
    assert created.status == "awaiting_confirmation"
    confirmations = []
    for item in created.items:
        assert item.classroom_asset_id is not None
        classroom = await classrooms.get(author, item.classroom_asset_id)
        assert classroom is not None and classroom.outline is not None
        confirmations.append(
            (
                item.id,
                classroom.revision,
                canonical_outline_sha256(classroom.outline),
            )
        )
    confirmed = await batches.confirm_outlines(
        author,
        created.id,
        tuple(confirmations),
    )
    assert tuple(item.status for item in confirmed.items) == ("queued", "queued", "queued")

    for item_id in ("lesson-a", "lesson-c"):
        await worker.complete(created.id, item_id, succeeded=True)
    await worker.complete(created.id, "lesson-b", succeeded=False)
    partial = await batches.get(author, created.id)
    assert partial is not None and partial.status == "partially_succeeded"
    partial_list = await batches.list(author)
    assert partial_list == (partial,)
    partial_statuses = tuple(item.status for item in partial.items)

    failed = next(item for item in partial.items if item.id == "lesson-b")
    assert failed.generation_job_id is not None
    retried = await batches.retry_item(author, created.id, "lesson-b")
    retry_call = jobs.retry_calls[-1]
    lesson_b_asset_id = classroom_gateway.assets_by_item["lesson-b"]
    lesson_b_classroom = await classrooms.get(author, lesson_b_asset_id)
    assert lesson_b_classroom is not None
    assert retried.item.classroom_asset_id == lesson_b_classroom.asset_id
    assert retried.item.classroom_draft_id == lesson_b_classroom.draft_id
    assert retried.item.generation_job_id == lesson_b_classroom.job_id
    retry_lineage_preserved = (
        retried.parent_item_id == "lesson-b"
        and retry_call == (
            created.id,
            "lesson-b",
            failed.generation_job_id,
            retried.item.generation_job_id,
        )
        and jobs.lineage[retry_call[3]] == retry_call[2]
        and jobs.counts == {"lesson-a": 1, "lesson-b": 2, "lesson-c": 1}
    )
    await worker.complete(created.id, "lesson-b", succeeded=True)
    finished = await batches.get(author, created.id)
    assert finished is not None and finished.status == "succeeded"
    finished_lesson_b = next(item for item in finished.items if item.id == "lesson-b")
    assert finished_lesson_b.classroom_asset_id == lesson_b_classroom.asset_id
    assert finished_lesson_b.classroom_draft_id == lesson_b_classroom.draft_id
    assert finished_lesson_b.generation_job_id == lesson_b_classroom.job_id
    finished_list = await batches.list(author)
    assert finished_list == (finished,)
    final_statuses = tuple(item.status for item in finished.items)

    publish_item = finished.items[0]
    assert publish_item.classroom_asset_id is not None
    validated = await classrooms.validate(author, publish_item.classroom_asset_id)
    assert validated.validation_report is not None
    ledger = _FlowLedger(classrooms_repository)
    reviews_repository = _LedgerReviewRepository(ledger)
    reviews = ReviewService(reviews_repository)
    submitted = await reviews.submit(
        author,
        validated.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="content-operations-review",
    )
    submitted_classroom = await classrooms.get(author, validated.asset_id)
    assert submitted_classroom is not None
    with pytest.raises(ReviewAccessDenied, match="self-review"):
        await reviews.approve(author, submitted.id, "author cannot self-review")
    approved = await reviews.approve(reviewer, submitted.id, "approved by reviewer")
    approved_classroom = await classrooms.get(author, validated.asset_id)
    assert approved_classroom is not None

    publications_repository = _LedgerPublicationRepository(ledger)
    publication_materializer = _LedgerPublicationMaterializer()
    publications = PublicationService(
        publications_repository,
        publication_materializer,
    )
    before = await publications.library(publisher)
    candidate_before = tuple(item.review_id for item in before.candidates) == (approved.id,)
    candidate_derived_from_review = (
        len(before.candidates) == 1
        and before.candidates[0].review_id == approved.id
        and before.candidates[0].document_sha256 == approved.document_sha256
        and before.candidates[0].draft_revision == approved.draft_revision
        and before.candidates[0].submitted_by == submitted.submitted_by
    )
    published = await publications.publish(
        publisher,
        validated.asset_id,
        scope="tenant",
        class_id=None,
        idempotency_key="content-operations-publish",
    )
    published_classroom = await classrooms.get(author, validated.asset_id)
    assert published_classroom is not None
    after = await publications.library(publisher)

    return {
        "reviewedOutlines": tuple(item_id for item_id, _, _ in confirmations),
        "partialStatuses": partial_statuses,
        "retriedItem": retried.parent_item_id,
        "retryLineagePreserved": retry_lineage_preserved,
        "batchObservedThroughService": (
            partial_list[0].status == "partially_succeeded"
            and finished_list[0].status == "succeeded"
            and batch_repository.list_calls == [(50, 0), (50, 0)]
        ),
        "finalStatuses": final_statuses,
        "submittedBy": submitted.submitted_by,
        "reviewedBy": approved.reviewer_id,
        "candidateBeforePublish": candidate_before,
        "candidateDerivedFromReview": candidate_derived_from_review,
        "publicationLifecycle": (
            submitted_classroom.lifecycle_state,
            approved_classroom.lifecycle_state,
            published_classroom.lifecycle_state,
        ),
        "publishedLibraryVersion": after.items[0].version_id,
        "publishedVersionId": published.version_id,
        "candidateAfterPublish": bool(after.candidates),
    }


@pytest.mark.asyncio
async def test_content_operations_flow_retries_and_publishes_to_tenant_library() -> None:
    evidence = await _run_content_operations_flow()

    assert evidence["reviewedOutlines"] == ("lesson-a", "lesson-b", "lesson-c")
    assert evidence["partialStatuses"] == ("succeeded", "failed", "succeeded")
    assert evidence["retriedItem"] == "lesson-b"
    assert evidence["retryLineagePreserved"] is True
    assert evidence["batchObservedThroughService"] is True
    assert evidence["finalStatuses"] == ("succeeded", "succeeded", "succeeded")
    assert evidence["submittedBy"] == "author-a"
    assert evidence["reviewedBy"] == "reviewer-a"
    assert evidence["candidateBeforePublish"] is True
    assert evidence["candidateDerivedFromReview"] is True
    assert evidence["publicationLifecycle"] == ("submitted", "approved", "published")
    assert evidence["publishedLibraryVersion"] == evidence["publishedVersionId"]
    assert evidence["candidateAfterPublish"] is False
