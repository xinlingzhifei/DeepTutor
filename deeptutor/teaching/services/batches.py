"""Durable, tenant-scoped classroom batch orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from typing import Any, Protocol

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deeptutor.teaching.brief_builder import TeachingBriefBuilder
from deeptutor.teaching.contracts import (
    OutlineBundle,
    canonical_json_bytes,
    canonical_outline_sha256,
)
from deeptutor.teaching.models.classrooms import BatchItem, BatchJob, ClassroomDraft
from deeptutor.teaching.models.jobs import GenerationJob
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.quota import InsufficientQuota
from deeptutor.teaching.repositories.jobs import (
    GenerationJobDetails,
    GenerationJobRequest,
    IdempotencyConflict,
    SqlAlchemyGenerationJobRepository,
    build_explicit_retry_request,
)
from deeptutor.teaching.scheduler import PRIORITY_RANK
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.classrooms import (
    ClassroomAccessDenied,
    ClassroomConfirmationConflict,
    ClassroomIdempotencyConflict,
    ClassroomPreflightRejected,
    ClassroomService,
    ClassroomServiceError,
    SqlAlchemyClassroomGeneration,
    matches_reviewed_outline_binding,
)
from deeptutor.teaching.source_snapshots import SourceAccessDenied
from deeptutor.teaching.tenant_context import TenantContext

_ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_BATCH_ID_PATTERN = re.compile(r"^batch-[0-9a-f]{20}-[0-9a-f]{32}$")
_SAFE_SOURCE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class BatchServiceError(RuntimeError):
    """Base class for stable classroom batch failures."""


class InvalidBatchRequest(BatchServiceError, ValueError):
    """The caller supplied an invalid batch request before side effects."""


class BatchAccessDenied(BatchServiceError, PermissionError):
    """The caller cannot operate on the requested classroom batch."""


class BatchNotFound(BatchServiceError, LookupError):
    """The batch is unavailable in the active tenant."""


class BatchIdempotencyConflict(BatchServiceError):
    """A deterministic batch identity is bound to different immutable input."""


class InvalidBatchState(BatchServiceError):
    """The requested batch transition is not allowed."""


class BatchOutlineConflict(BatchServiceError):
    """The confirmed outline is not the revision and hash the author reviewed."""


class BatchPersistenceError(BatchServiceError):
    """Stored batch state is unavailable or internally inconsistent."""


class BatchItemRejected(BatchServiceError):
    """One deterministic item input cannot start a classroom workflow."""


@dataclass(frozen=True, slots=True)
class BatchItemInput:
    id: str
    classroom: object

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or _ITEM_ID_PATTERN.fullmatch(self.id) is None:
            raise InvalidBatchRequest("batch item id is invalid")


@dataclass(frozen=True, slots=True)
class BatchReplayKnowledgePoint:
    knowledge_point_id: str
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class BatchReplayClassroomRequest:
    title: str
    course_id: str
    class_id: str
    objective: str
    grade_band: str
    audience: str
    duration_minutes: int
    classroom_mode: str
    web_policy: str
    allowed_web_domains: tuple[str, ...]
    template_id: str
    template_version: str
    knowledge_points: tuple[BatchReplayKnowledgePoint, ...]
    content_mode: str
    open_creation_acknowledged: bool
    source_type: str | None
    source_ref: str | None
    requested_exports: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchItemRecord:
    id: str
    batch_id: str
    status: str
    generation_job_id: str | None = None
    classroom_draft_id: str | None = None
    classroom_asset_id: str | None = None
    resource_course_id: str | None = None
    resource_class_id: str | None = None


@dataclass(frozen=True, slots=True)
class BatchJobRecord:
    id: str
    tenant_id: str
    actor_id: str
    status: str
    item_count: int
    succeeded_count: int
    failed_count: int
    items: tuple[BatchItemRecord, ...]
    created_at: Any | None = None
    updated_at: Any | None = None


@dataclass(frozen=True, slots=True)
class BatchRetryResult:
    parent_item_id: str
    item: BatchItemRecord


@dataclass(frozen=True, slots=True)
class BatchAccessScope:
    tenant_wide: bool = False
    course_ids: tuple[str, ...] = ()
    class_ids: tuple[str, ...] = ()

    @property
    def allows_any(self) -> bool:
        return self.tenant_wide or bool(self.course_ids) or bool(self.class_ids)


class BatchRepository(Protocol):
    async def create(
        self,
        batch_id: str,
        actor_id: str,
        item_ids: tuple[str, ...],
    ) -> BatchJobRecord: ...

    async def get(self, batch_id: str) -> BatchJobRecord | None: ...

    async def list(
        self,
        *,
        access_scope: BatchAccessScope | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[BatchJobRecord, ...]: ...

    async def bind_item(
        self,
        batch_id: str,
        item_id: str,
        *,
        generation_job_id: str,
        classroom_draft_id: str,
        classroom_asset_id: str,
        status: str,
    ) -> BatchItemRecord: ...

    async def set_item_status(
        self,
        batch_id: str,
        item_id: str,
        status: str,
    ) -> BatchItemRecord: ...

    async def rebind_failed_item(
        self,
        batch_id: str,
        item_id: str,
        *,
        expected_job_id: str,
        new_job_id: str,
    ) -> BatchItemRecord: ...

    async def bind_rejected_item(
        self,
        batch_id: str,
        item_id: str,
        *,
        generation_job_id: str,
    ) -> BatchItemRecord: ...


class BatchClassroomGateway(Protocol):
    async def create(
        self,
        context: TenantContext,
        request: object,
        *,
        batch_id: str,
        item_id: str,
        retry_of_job_id: str | None = None,
    ) -> object: ...

    async def get(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> object | None: ...

    async def confirm_outline(
        self,
        context: TenantContext,
        asset_id: str,
        *,
        expected_revision: int,
        expected_outline_sha256: str,
    ) -> object: ...


class BatchJobGateway(Protocol):
    async def retry(
        self,
        context: TenantContext,
        *,
        batch_id: str,
        item_id: str,
        job_id: str,
    ) -> str: ...

    async def cancel_unstarted(
        self,
        context: TenantContext,
        *,
        job_id: str,
    ) -> bool: ...

    async def record_rejected(
        self,
        context: TenantContext,
        *,
        batch_id: str,
        item_id: str,
        request: object,
        retry_of_job_id: str | None = None,
    ) -> str: ...

    async def rejected_input(
        self,
        context: TenantContext,
        *,
        job_id: str,
    ) -> object: ...


_ITEM_TRANSITIONS = {
    "queued": frozenset(
        {"queued", "running", "awaiting_confirmation", "succeeded", "failed", "canceled"}
    ),
    "running": frozenset({"running", "awaiting_confirmation", "succeeded", "failed", "canceled"}),
    "awaiting_confirmation": frozenset({"awaiting_confirmation", "queued", "failed", "canceled"}),
    "succeeded": frozenset({"succeeded"}),
    "failed": frozenset({"failed"}),
    "canceled": frozenset({"canceled"}),
}


def _validate_pagination(limit: int, offset: int) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 100
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
    ):
        raise InvalidBatchRequest("batch pagination is invalid")


def _batch_access_scope(context: TenantContext) -> BatchAccessScope:
    tenant_wide = False
    course_ids: set[str] = set()
    class_ids: set[str] = set()
    for grant in context.permissions:
        if grant.permission != "classroom.edit" or not grant.scope_id:
            continue
        grant_tenant_id = grant.tenant_id
        if grant.scope_type == "tenant" and grant_tenant_id is None:
            grant_tenant_id = grant.scope_id
        if grant_tenant_id != context.tenant_id:
            continue
        if grant.scope_type == "tenant" and grant.scope_id == context.tenant_id:
            tenant_wide = True
        elif grant.scope_type == "course":
            course_ids.add(grant.scope_id)
        elif grant.scope_type == "class":
            class_ids.add(grant.scope_id)
    return BatchAccessScope(
        tenant_wide=tenant_wide,
        course_ids=tuple(sorted(course_ids)),
        class_ids=tuple(sorted(class_ids)),
    )


class SqlAlchemyBatchRepository:
    """Persist one tenant's batch aggregate and retry bindings with row locks."""

    def __init__(self, engine: AsyncEngine, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(translated, expire_on_commit=False)

    @staticmethod
    def _physical_item_id(batch_id: str, ordinal: int, item_id: str) -> str:
        value = f"{batch_id}:{ordinal:03d}:{item_id}"
        if len(value) > 128:
            raise ValueError("batch item id is too long")
        return value

    @staticmethod
    def _logical_item_id(batch_id: str, physical_id: str) -> str:
        prefix = f"{batch_id}:"
        if not physical_id.startswith(prefix):
            raise BatchPersistenceError("stored batch item identity is invalid")
        remainder = physical_id[len(prefix) :]
        ordinal, separator, item_id = remainder.partition(":")
        if (
            not separator
            or len(ordinal) != 3
            or not ordinal.isdigit()
            or _ITEM_ID_PATTERN.fullmatch(item_id) is None
        ):
            raise BatchPersistenceError("stored batch item identity is invalid")
        return item_id

    async def _item_rows(
        self,
        session: AsyncSession,
        batch_id: str,
        *,
        lock: bool = False,
    ):
        statement = (
            select(
                BatchItem,
                ClassroomDraft.classroom_id,
                GenerationJob.resource_course_id,
                GenerationJob.resource_class_id,
            )
            .outerjoin(
                ClassroomDraft,
                ClassroomDraft.id == BatchItem.classroom_draft_id,
            )
            .outerjoin(
                GenerationJob,
                (GenerationJob.id == BatchItem.generation_job_id)
                & (GenerationJob.tenant_id == BatchItem.tenant_id),
            )
            .where(
                BatchItem.tenant_id == self._tenant_id,
                BatchItem.batch_job_id == batch_id,
            )
            .order_by(BatchItem.id)
        )
        if lock:
            statement = statement.with_for_update(of=BatchItem)
        return (await session.execute(statement)).all()

    def _item_record(
        self,
        batch_id: str,
        model: BatchItem,
        classroom_asset_id: str | None,
        resource_course_id: str | None = None,
        resource_class_id: str | None = None,
    ) -> BatchItemRecord:
        return BatchItemRecord(
            id=self._logical_item_id(batch_id, model.id),
            batch_id=batch_id,
            status=model.status,
            generation_job_id=model.generation_job_id,
            classroom_draft_id=model.classroom_draft_id,
            classroom_asset_id=classroom_asset_id,
            resource_course_id=resource_course_id,
            resource_class_id=resource_class_id,
        )

    async def _record(
        self,
        session: AsyncSession,
        batch: BatchJob,
    ) -> BatchJobRecord:
        rows = await self._item_rows(session, batch.id)
        items = tuple(self._item_record(batch.id, row[0], row[1], row[2], row[3]) for row in rows)
        if len(items) != batch.item_count:
            raise BatchPersistenceError("stored batch item count is invalid")
        return BatchJobRecord(
            id=batch.id,
            tenant_id=batch.tenant_id,
            actor_id=batch.actor_id,
            status=batch.status,
            item_count=batch.item_count,
            succeeded_count=batch.succeeded_count,
            failed_count=batch.failed_count,
            items=items,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
        )

    @staticmethod
    def _aggregate_status(statuses: tuple[str, ...]) -> str:
        succeeded = statuses.count("succeeded")
        failed = statuses.count("failed")
        canceled = statuses.count("canceled")
        terminal = succeeded + failed + canceled == len(statuses)
        if terminal and succeeded == len(statuses):
            return "succeeded"
        if terminal and succeeded:
            return "partially_succeeded"
        if terminal and failed:
            return "failed"
        if terminal:
            return "canceled"
        if "awaiting_confirmation" in statuses:
            return "awaiting_confirmation"
        if any(status != "queued" for status in statuses):
            return "running"
        return "queued"

    async def _refresh_locked(
        self,
        session: AsyncSession,
        batch: BatchJob,
        *,
        item_changed: bool,
    ) -> None:
        items = tuple(
            await session.scalars(
                select(BatchItem)
                .where(
                    BatchItem.tenant_id == self._tenant_id,
                    BatchItem.batch_job_id == batch.id,
                )
                .order_by(BatchItem.id)
                .with_for_update()
            )
        )
        statuses = tuple(item.status for item in items)
        if len(statuses) != batch.item_count or not statuses:
            raise BatchPersistenceError("stored batch item count is invalid")
        succeeded_count = statuses.count("succeeded")
        failed_count = statuses.count("failed")
        status = self._aggregate_status(statuses)
        aggregate_changed = (
            batch.succeeded_count != succeeded_count
            or batch.failed_count != failed_count
            or batch.status != status
        )
        if not item_changed and not aggregate_changed:
            return
        batch.succeeded_count = succeeded_count
        batch.failed_count = failed_count
        batch.status = status
        batch.updated_at = func.now()
        await session.flush()
        await session.refresh(batch, attribute_names=("updated_at",))

    async def _reconcile_locked(
        self,
        session: AsyncSession,
        batch: BatchJob,
    ) -> None:
        rows = (
            await session.execute(
                select(
                    BatchItem,
                    GenerationJob.status,
                    GenerationJob.job_kind,
                    GenerationJob.phase,
                    GenerationJob.classroom_draft_id,
                    ClassroomDraft.generation_job_id,
                    ClassroomDraft.confirmed_outline_sha256,
                )
                .outerjoin(
                    GenerationJob,
                    (GenerationJob.id == BatchItem.generation_job_id)
                    & (GenerationJob.tenant_id == BatchItem.tenant_id),
                )
                .outerjoin(
                    ClassroomDraft,
                    (ClassroomDraft.id == BatchItem.classroom_draft_id)
                    & (ClassroomDraft.tenant_id == BatchItem.tenant_id),
                )
                .where(
                    BatchItem.tenant_id == self._tenant_id,
                    BatchItem.batch_job_id == batch.id,
                )
                .order_by(BatchItem.id)
                .with_for_update(of=BatchItem)
            )
        ).all()
        item_changed = False
        for (
            item,
            job_status,
            job_kind,
            job_phase,
            job_draft_id,
            draft_job_id,
            confirmed_outline_sha256,
        ) in rows:
            if job_status is None:
                continue
            reconciled = _item_status(str(job_status))
            if reconciled == item.status:
                continue
            if item.status == "awaiting_confirmation" and reconciled in {
                "queued",
                "running",
                "succeeded",
            }:
                if not (
                    job_kind == "generation"
                    and job_phase == "content"
                    and item.classroom_draft_id is not None
                    and job_draft_id == item.classroom_draft_id
                    and draft_job_id == item.generation_job_id
                    and confirmed_outline_sha256 is not None
                ):
                    raise BatchPersistenceError("stored batch item state is invalid")
                item.status = reconciled
                item.updated_at = func.now()
                item_changed = True
                continue
            allowed = _ITEM_TRANSITIONS.get(item.status, frozenset())
            if reconciled not in allowed:
                if item.status in {"succeeded", "canceled"}:
                    continue
                raise BatchPersistenceError("stored batch item state is invalid")
            item.status = reconciled
            item.updated_at = func.now()
            item_changed = True
        await self._refresh_locked(session, batch, item_changed=item_changed)

    async def create(
        self,
        batch_id: str,
        actor_id: str,
        item_ids: tuple[str, ...],
    ) -> BatchJobRecord:
        if _BATCH_ID_PATTERN.fullmatch(batch_id) is None or not actor_id or len(actor_id) > 128:
            raise ValueError("batch identity is invalid")
        expected_ids = tuple(
            self._physical_item_id(batch_id, ordinal, item_id)
            for ordinal, item_id in enumerate(item_ids)
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    idempotency_prefix = f"{batch_id.rsplit('-', 1)[0]}-"
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:batch_idempotency_lock, 0))"
                        ),
                        {"batch_idempotency_lock": idempotency_prefix},
                    )
                    matching_batches = tuple(
                        await session.scalars(
                            select(BatchJob)
                            .where(
                                BatchJob.tenant_id == self._tenant_id,
                                BatchJob.id.like(f"{idempotency_prefix}%"),
                            )
                            .with_for_update()
                        )
                    )
                    if len(matching_batches) > 1:
                        raise BatchPersistenceError("stored batch idempotency state is invalid")
                    batch = matching_batches[0] if matching_batches else None
                    if batch is not None and batch.id != batch_id:
                        raise BatchIdempotencyConflict("batch idempotency key conflicts")
                    if batch is None:
                        batch = BatchJob(
                            id=batch_id,
                            tenant_id=self._tenant_id,
                            actor_id=actor_id,
                            status="queued",
                            item_count=len(item_ids),
                            succeeded_count=0,
                            failed_count=0,
                        )
                        session.add(batch)
                        await session.flush()
                        session.add_all(
                            BatchItem(
                                id=physical_id,
                                tenant_id=self._tenant_id,
                                batch_job_id=batch_id,
                                generation_job_id=None,
                                classroom_draft_id=None,
                                status="queued",
                            )
                            for physical_id in expected_ids
                        )
                        await session.flush()
                    else:
                        stored_ids = tuple(
                            await session.scalars(
                                select(BatchItem.id)
                                .where(
                                    BatchItem.tenant_id == self._tenant_id,
                                    BatchItem.batch_job_id == batch_id,
                                )
                                .order_by(BatchItem.id)
                                .with_for_update()
                            )
                        )
                        if batch.actor_id != actor_id or stored_ids != expected_ids:
                            raise BatchIdempotencyConflict("batch idempotency key conflicts")
                    return await self._record(session, batch)
        except IntegrityError as exc:
            raise BatchPersistenceError("batch persistence conflicts") from exc

    async def get(self, batch_id: str) -> BatchJobRecord | None:
        async with self._session_factory() as session:
            async with session.begin():
                batch = await session.scalar(
                    select(BatchJob)
                    .where(
                        BatchJob.id == batch_id,
                        BatchJob.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if batch is None:
                    return None
                await self._reconcile_locked(session, batch)
                return await self._record(session, batch)

    async def list(
        self,
        *,
        access_scope: BatchAccessScope | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[BatchJobRecord, ...]:
        _validate_pagination(limit, offset)
        scope = access_scope or BatchAccessScope()
        if not scope.allows_any:
            return ()
        allowed_conditions = []
        if scope.course_ids:
            allowed_conditions.append(GenerationJob.resource_course_id.in_(scope.course_ids))
        if scope.class_ids:
            allowed_conditions.append(GenerationJob.resource_class_id.in_(scope.class_ids))
        unauthorized_conditions = [
            GenerationJob.id.is_(None),
            GenerationJob.resource_course_id.is_(None),
            GenerationJob.resource_class_id.is_(None),
            GenerationJob.resource_course_id == "",
            GenerationJob.resource_class_id == "",
        ]
        if not scope.tenant_wide:
            unauthorized_conditions.append(~or_(*allowed_conditions))
        has_items = exists(
            select(BatchItem.id).where(
                BatchItem.tenant_id == BatchJob.tenant_id,
                BatchItem.batch_job_id == BatchJob.id,
            )
        ).correlate(BatchJob)
        has_unauthorized_item = exists(
            select(BatchItem.id)
            .select_from(BatchItem)
            .outerjoin(
                GenerationJob,
                (GenerationJob.id == BatchItem.generation_job_id)
                & (GenerationJob.tenant_id == BatchItem.tenant_id),
            )
            .where(
                BatchItem.tenant_id == BatchJob.tenant_id,
                BatchItem.batch_job_id == BatchJob.id,
                or_(*unauthorized_conditions),
            )
        ).correlate(BatchJob)
        async with self._session_factory() as session:
            async with session.begin():
                batch_ids = tuple(
                    await session.scalars(
                        select(BatchJob.id)
                        .where(
                            BatchJob.tenant_id == self._tenant_id,
                            has_items,
                            ~has_unauthorized_item,
                        )
                        .order_by(BatchJob.created_at.desc(), BatchJob.id)
                        .limit(limit)
                        .offset(offset)
                    )
                )
        records = []
        for batch_id in batch_ids:
            record = await self.get(batch_id)
            if record is not None:
                records.append(record)
        return tuple(records)

    async def _lock_item(
        self,
        session: AsyncSession,
        batch_id: str,
        item_id: str,
    ) -> tuple[BatchJob, BatchItem]:
        batch = await session.scalar(
            select(BatchJob)
            .where(
                BatchJob.id == batch_id,
                BatchJob.tenant_id == self._tenant_id,
            )
            .with_for_update()
        )
        if batch is None:
            raise BatchNotFound("batch not found")
        rows = tuple(
            await session.scalars(
                select(BatchItem)
                .where(
                    BatchItem.tenant_id == self._tenant_id,
                    BatchItem.batch_job_id == batch_id,
                )
                .with_for_update()
            )
        )
        item = next(
            (
                candidate
                for candidate in rows
                if self._logical_item_id(batch_id, candidate.id) == item_id
            ),
            None,
        )
        if item is None:
            raise BatchNotFound("batch item not found")
        return batch, item

    @staticmethod
    def _transition_item(item: BatchItem, target: str) -> bool:
        allowed = _ITEM_TRANSITIONS.get(item.status)
        if allowed is None or target not in allowed:
            raise InvalidBatchState("batch item transition is invalid")
        if item.status == target:
            return False
        item.status = target
        item.updated_at = func.now()
        return True

    async def bind_item(
        self,
        batch_id: str,
        item_id: str,
        *,
        generation_job_id: str,
        classroom_draft_id: str,
        classroom_asset_id: str,
        status: str,
    ) -> BatchItemRecord:
        async with self._session_factory() as session:
            async with session.begin():
                batch, item = await self._lock_item(session, batch_id, item_id)
                draft = await session.scalar(
                    select(ClassroomDraft)
                    .where(
                        ClassroomDraft.id == classroom_draft_id,
                        ClassroomDraft.tenant_id == self._tenant_id,
                        ClassroomDraft.classroom_id == classroom_asset_id,
                        ClassroomDraft.generation_job_id == generation_job_id,
                    )
                    .with_for_update()
                )
                if draft is None:
                    raise BatchPersistenceError("batch classroom binding is unavailable")
                if item.generation_job_id is not None and (
                    item.generation_job_id != generation_job_id
                    or item.classroom_draft_id != classroom_draft_id
                ):
                    raise BatchIdempotencyConflict("batch item binding conflicts")
                binding_changed = (
                    item.generation_job_id != generation_job_id
                    or item.classroom_draft_id != classroom_draft_id
                )
                item.generation_job_id = generation_job_id
                item.classroom_draft_id = classroom_draft_id
                status_changed = self._transition_item(item, status)
                await self._refresh_locked(
                    session,
                    batch,
                    item_changed=binding_changed or status_changed,
                )
                return self._item_record(batch_id, item, classroom_asset_id)

    async def set_item_status(
        self,
        batch_id: str,
        item_id: str,
        status: str,
    ) -> BatchItemRecord:
        async with self._session_factory() as session:
            async with session.begin():
                batch, item = await self._lock_item(session, batch_id, item_id)
                item_changed = self._transition_item(item, status)
                asset_id = None
                if item.classroom_draft_id is not None:
                    asset_id = await session.scalar(
                        select(ClassroomDraft.classroom_id).where(
                            ClassroomDraft.id == item.classroom_draft_id,
                            ClassroomDraft.tenant_id == self._tenant_id,
                        )
                    )
                await self._refresh_locked(
                    session,
                    batch,
                    item_changed=item_changed,
                )
                return self._item_record(batch_id, item, asset_id)

    async def bind_rejected_item(
        self,
        batch_id: str,
        item_id: str,
        *,
        generation_job_id: str,
    ) -> BatchItemRecord:
        async with self._session_factory() as session:
            async with session.begin():
                batch, item = await self._lock_item(session, batch_id, item_id)
                job = await session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.id == generation_job_id,
                        GenerationJob.tenant_id == self._tenant_id,
                        GenerationJob.batch_id == batch_id,
                        GenerationJob.classroom_draft_id.is_(None),
                        GenerationJob.status == "failed",
                        GenerationJob.priority == PRIORITY_RANK["batch"],
                        GenerationJob.error_code == "batch_item_rejected",
                    )
                    .with_for_update()
                )
                if job is None:
                    raise BatchPersistenceError("rejected batch job is unavailable")
                if item.generation_job_id not in {None, generation_job_id}:
                    raise BatchIdempotencyConflict("batch item binding conflicts")
                binding_changed = (
                    item.generation_job_id != generation_job_id
                    or item.classroom_draft_id is not None
                )
                item.generation_job_id = generation_job_id
                item.classroom_draft_id = None
                status_changed = self._transition_item(item, "failed")
                await self._refresh_locked(
                    session,
                    batch,
                    item_changed=binding_changed or status_changed,
                )
                return self._item_record(batch_id, item, None)

    async def rebind_failed_item(
        self,
        batch_id: str,
        item_id: str,
        *,
        expected_job_id: str,
        new_job_id: str,
    ) -> BatchItemRecord:
        async with self._session_factory() as session:
            async with session.begin():
                batch, item = await self._lock_item(session, batch_id, item_id)
                job = await session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.id == new_job_id,
                        GenerationJob.tenant_id == self._tenant_id,
                        GenerationJob.batch_id == batch_id,
                        GenerationJob.retry_of_job_id == expected_job_id,
                    )
                    .with_for_update()
                )
                if (
                    job is None
                    or job.job_kind != "generation"
                    or job.priority != PRIORITY_RANK["batch"]
                ):
                    raise BatchPersistenceError("batch retry binding is unavailable")
                target_status = _item_status(job.status)
                if item.generation_job_id == new_job_id:
                    if item.classroom_draft_id != job.classroom_draft_id:
                        raise BatchPersistenceError("batch retry binding is invalid")
                    draft = None
                    if item.classroom_draft_id is not None:
                        draft = await session.scalar(
                            select(ClassroomDraft)
                            .where(
                                ClassroomDraft.id == item.classroom_draft_id,
                                ClassroomDraft.tenant_id == self._tenant_id,
                            )
                            .with_for_update()
                        )
                        if draft is None or draft.generation_job_id != new_job_id:
                            raise BatchPersistenceError("batch retry draft is unavailable")
                    elif job.status != "failed" or job.error_code != "batch_item_rejected":
                        raise BatchPersistenceError("batch retry draft is unavailable")
                    if item.status != target_status:
                        item_changed = self._transition_item(item, target_status)
                        await self._refresh_locked(
                            session,
                            batch,
                            item_changed=item_changed,
                        )
                    return self._item_record(
                        batch_id,
                        item,
                        draft.classroom_id if draft is not None else None,
                    )
                if item.status != "failed" or item.generation_job_id != expected_job_id:
                    raise InvalidBatchState("only a failed batch item can be retried")
                if (
                    item.classroom_draft_id is not None
                    and job.classroom_draft_id != item.classroom_draft_id
                ):
                    raise BatchPersistenceError("batch retry draft binding is invalid")
                draft = None
                if job.classroom_draft_id is not None:
                    draft = await session.scalar(
                        select(ClassroomDraft)
                        .where(
                            ClassroomDraft.id == job.classroom_draft_id,
                            ClassroomDraft.tenant_id == self._tenant_id,
                        )
                        .with_for_update()
                    )
                    if draft is None or draft.generation_job_id not in {
                        expected_job_id,
                        new_job_id,
                    }:
                        raise BatchPersistenceError("batch retry draft is unavailable")
                    draft.generation_job_id = new_job_id
                elif job.status != "failed" or job.error_code != "batch_item_rejected":
                    raise BatchPersistenceError("batch retry draft is unavailable")
                item.generation_job_id = new_job_id
                item.classroom_draft_id = job.classroom_draft_id
                item.status = target_status
                item.updated_at = func.now()
                await self._refresh_locked(session, batch, item_changed=True)
                return self._item_record(
                    batch_id,
                    item,
                    draft.classroom_id if draft is not None else None,
                )


class SqlAlchemyBatchClassroomGateway:
    """Run batch items through the existing two-stage classroom service."""

    def __init__(
        self,
        classroom_repository,
        brief_builder: TeachingBriefBuilder,
        job_repository: SqlAlchemyGenerationJobRepository,
        data_plane_selector,
        store_provider,
    ) -> None:
        self._classroom_repository = classroom_repository
        self._brief_builder = brief_builder
        self._job_repository = job_repository
        self._data_plane_selector = data_plane_selector
        self._store_provider = store_provider

    def _service(
        self,
        *,
        batch_id: str | None = None,
        retry_of_job_id: str | None = None,
    ) -> ClassroomService:
        generation = SqlAlchemyClassroomGeneration(
            self._job_repository,
            self._data_plane_selector,
            **(
                {
                    "priority": "batch",
                    "batch_id": batch_id,
                    "retry_of_job_id": retry_of_job_id,
                }
                if batch_id is not None
                else {}
            ),
        )
        return ClassroomService(
            self._classroom_repository,
            self._brief_builder,
            generation,
            self._store_provider,
        )

    async def create(
        self,
        context: TenantContext,
        request: object,
        *,
        batch_id: str,
        item_id: str,
        retry_of_job_id: str | None = None,
    ) -> object:
        try:
            return await self._service(
                batch_id=batch_id,
                retry_of_job_id=retry_of_job_id,
            ).create(
                context,
                request,
                idempotency_key=f"{batch_id}:{item_id}",
            )
        except ClassroomIdempotencyConflict:
            raise BatchIdempotencyConflict("batch item input conflicts") from None
        except IdempotencyConflict:
            raise BatchIdempotencyConflict("batch item job conflicts") from None
        except ClassroomAccessDenied:
            raise BatchAccessDenied("batch item access is denied") from None
        except SourceAccessDenied:
            raise BatchAccessDenied("batch source access is denied") from None
        except ClassroomPreflightRejected as exc:
            raise BatchItemRejected(str(exc)) from None
        except InsufficientQuota as exc:
            raise BatchItemRejected(str(exc)) from None

    async def get(
        self,
        context: TenantContext,
        asset_id: str,
    ) -> object | None:
        try:
            return await self._service().get(context, asset_id)
        except ClassroomServiceError:
            raise BatchOutlineConflict("outline recovery conflicts") from None

    async def confirm_outline(
        self,
        context: TenantContext,
        asset_id: str,
        *,
        expected_revision: int,
        expected_outline_sha256: str,
    ) -> object:
        try:
            return await self._service().confirm_outline(
                context,
                asset_id,
                expected_revision=expected_revision,
                expected_outline_sha256=expected_outline_sha256,
            )
        except ClassroomConfirmationConflict:
            raise BatchOutlineConflict("outline review binding conflicts") from None


def _generation_job_request(details: GenerationJobDetails) -> GenerationJobRequest:
    priority = next(
        (name for name, rank in PRIORITY_RANK.items() if rank == details.priority),
        None,
    )
    if priority is None:
        raise InvalidBatchState("batch job priority is invalid")
    return GenerationJobRequest(
        tenant_id=details.tenant_id,
        job_id=details.job_id,
        job_kind=details.job_kind,
        phase=details.phase,
        export_format=details.export_format,
        priority=priority,
        quota_units=details.quota_units,
        actor_id=details.actor_id,
        owner_id=details.owner_id,
        visibility=details.visibility,
        request_id=details.request_id,
        idempotency_key=details.idempotency_key,
        request_sha256=details.request_sha256,
        data_plane_mode=details.data_plane_mode,
        data_plane_route_id=details.data_plane_route_id,
        provider_profile_id=details.provider_profile_id,
        worker_pool_ref=details.worker_pool_ref,
        queue_ref=details.queue_ref,
        request_payload=details.request_payload,
        classroom_draft_id=details.classroom_draft_id,
        batch_id=details.batch_id,
        resource_course_id=details.resource_course_id,
        resource_class_id=details.resource_class_id,
        public_request_sha256=details.public_request_sha256,
        retry_of_job_id=details.retry_of_job_id,
    )


class SqlAlchemyBatchJobGateway:
    """Create explicit retry jobs and cancel only work that has not started."""

    def __init__(self, repository: SqlAlchemyGenerationJobRepository) -> None:
        self._repository = repository

    async def retry(
        self,
        context: TenantContext,
        *,
        batch_id: str,
        item_id: str,
        job_id: str,
    ) -> str:
        details = await self._repository.get_job_details(context.tenant_id, job_id)
        if (
            details is None
            or details.tenant_id != context.tenant_id
            or details.batch_id != batch_id
            or details.status != "failed"
            or details.priority != PRIORITY_RANK["batch"]
        ):
            raise InvalidBatchState("batch item cannot be retried")
        digest = hashlib.sha256(
            f"{context.tenant_id}\0{batch_id}\0{item_id}\0{job_id}".encode()
        ).hexdigest()
        new_job_id = f"job-{digest[:48]}"
        public_request_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "batchId": batch_id,
                    "itemId": item_id,
                    "retryOfJobId": job_id,
                }
            )
        ).hexdigest()
        retry = build_explicit_retry_request(
            _generation_job_request(details),
            job_id=new_job_id,
            request_id=f"request-{digest[:48]}",
            idempotency_key=f"batch-retry-{digest}",
            actor_id=context.user_id,
            public_request_sha256=public_request_sha256,
        )
        try:
            await self._repository.create_job_and_reserve(retry)
        except IdempotencyConflict:
            raise BatchIdempotencyConflict("batch retry conflicts") from None
        return new_job_id

    async def record_rejected(
        self,
        context: TenantContext,
        *,
        batch_id: str,
        item_id: str,
        request: object,
        retry_of_job_id: str | None = None,
    ) -> str:
        classroom = _normalized_classroom_request(request)
        envelope = {
            "schemaVersion": "1.0",
            "kind": "batch_classroom_rejected",
            "tenantId": context.tenant_id,
            "batchId": batch_id,
            "itemId": item_id,
            "classroom": classroom,
        }
        payload = canonical_json_bytes(envelope).decode("utf-8")
        request_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "tenantId": context.tenant_id,
                    "batchId": batch_id,
                    "itemId": item_id,
                    "retryOfJobId": retry_of_job_id,
                    "requestSha256": request_sha256,
                }
            )
        ).hexdigest()
        job_id = f"job-{digest[:48]}"
        rejected = GenerationJobRequest(
            tenant_id=context.tenant_id,
            job_id=job_id,
            job_kind="generation",
            phase="outline",
            export_format=None,
            priority="batch",
            quota_units=max(1, int(classroom["durationMinutes"])),
            actor_id=context.user_id,
            owner_id=context.user_id,
            visibility="class",
            request_id=f"request-{digest[:48]}",
            idempotency_key=f"batch-rejected-{digest}",
            request_sha256=request_sha256,
            data_plane_mode=None,
            data_plane_route_id="batch-rejected",
            provider_profile_id="batch-rejected",
            worker_pool_ref="batch-rejected",
            queue_ref="batch-rejected",
            request_payload=payload,
            classroom_draft_id=None,
            batch_id=batch_id,
            resource_course_id=str(classroom["courseId"]),
            resource_class_id=str(classroom["classId"]),
            public_request_sha256=request_sha256,
            retry_of_job_id=retry_of_job_id,
        )
        try:
            record = await self._repository.create_rejected_batch_job(rejected)
        except IdempotencyConflict:
            raise BatchIdempotencyConflict("rejected batch job conflicts") from None
        if record.status != "failed":
            raise BatchPersistenceError("rejected batch job is not terminal")
        return record.job_id

    async def rejected_input(
        self,
        context: TenantContext,
        *,
        job_id: str,
    ) -> object:
        details = await self._repository.get_job_details(context.tenant_id, job_id)
        if (
            details is None
            or details.tenant_id != context.tenant_id
            or details.status != "failed"
            or details.priority != PRIORITY_RANK["batch"]
            or details.classroom_draft_id is not None
            or details.error_code != "batch_item_rejected"
            or details.public_request_sha256 is None
        ):
            raise InvalidBatchState("rejected batch input is unavailable")
        try:
            envelope = json.loads(details.request_payload)
        except (TypeError, ValueError):
            raise BatchPersistenceError("stored rejected batch input is invalid") from None
        if (
            not isinstance(envelope, Mapping)
            or set(envelope)
            != {
                "schemaVersion",
                "kind",
                "tenantId",
                "batchId",
                "itemId",
                "classroom",
            }
            or envelope["schemaVersion"] != "1.0"
            or envelope["kind"] != "batch_classroom_rejected"
            or envelope["tenantId"] != context.tenant_id
            or envelope["batchId"] != details.batch_id
            or canonical_json_bytes(envelope).decode("utf-8") != details.request_payload
            or not hmac.compare_digest(
                hashlib.sha256(details.request_payload.encode()).hexdigest(),
                details.request_sha256,
            )
            or not hmac.compare_digest(
                details.request_sha256,
                details.public_request_sha256,
            )
        ):
            raise BatchPersistenceError("stored rejected batch input is invalid")
        return _replay_classroom_request(envelope["classroom"])

    async def cancel_unstarted(
        self,
        context: TenantContext,
        *,
        job_id: str,
    ) -> bool:
        cancellation = await self._repository.request_cancel(
            context.tenant_id,
            job_id,
            only_if_unstarted=True,
        )
        if cancellation is None:
            return False
        if cancellation.running:
            raise BatchPersistenceError("running batch item was selected for cancellation")
        return True


def _normalized_classroom_request(request: object) -> dict[str, object]:
    source_ref = getattr(request, "source_ref", None)
    if source_ref is not None and (
        not isinstance(source_ref, str) or _SAFE_SOURCE_REF_PATTERN.fullmatch(source_ref) is None
    ):
        raise ValueError("batch source reference is invalid")
    points = []
    for point in getattr(request, "knowledge_points", ()):
        points.append(
            {
                "knowledgePointId": getattr(point, "knowledge_point_id"),
                "title": getattr(point, "title"),
                "description": getattr(point, "description"),
            }
        )
    return {
        "title": getattr(request, "title"),
        "courseId": getattr(request, "course_id"),
        "classId": getattr(request, "class_id"),
        "objective": getattr(request, "objective"),
        "gradeBand": getattr(request, "grade_band"),
        "audience": getattr(request, "audience"),
        "durationMinutes": getattr(request, "duration_minutes"),
        "classroomMode": getattr(request, "classroom_mode"),
        "webPolicy": getattr(request, "web_policy"),
        "allowedWebDomains": list(getattr(request, "allowed_web_domains", ())),
        "templateId": getattr(request, "template_id"),
        "templateVersion": getattr(request, "template_version"),
        "knowledgePoints": points,
        "contentMode": getattr(request, "content_mode"),
        "openCreationAcknowledged": getattr(
            request,
            "open_creation_acknowledged",
        ),
        "sourceType": getattr(request, "source_type", None),
        "sourceRef": source_ref,
        "requestedExports": list(getattr(request, "requested_exports")),
    }


def _replay_classroom_request(payload: object) -> BatchReplayClassroomRequest:
    if not isinstance(payload, Mapping):
        raise BatchPersistenceError("stored rejected batch input is invalid")
    try:
        raw_points = payload["knowledgePoints"]
        if not isinstance(raw_points, list):
            raise TypeError
        points = tuple(
            BatchReplayKnowledgePoint(
                knowledge_point_id=point["knowledgePointId"],
                title=point["title"],
                description=point["description"],
            )
            for point in raw_points
            if isinstance(point, Mapping)
        )
        if len(points) != len(raw_points):
            raise TypeError
        request = BatchReplayClassroomRequest(
            title=payload["title"],
            course_id=payload["courseId"],
            class_id=payload["classId"],
            objective=payload["objective"],
            grade_band=payload["gradeBand"],
            audience=payload["audience"],
            duration_minutes=payload["durationMinutes"],
            classroom_mode=payload["classroomMode"],
            web_policy=payload["webPolicy"],
            allowed_web_domains=tuple(payload["allowedWebDomains"]),
            template_id=payload["templateId"],
            template_version=payload["templateVersion"],
            knowledge_points=points,
            content_mode=payload["contentMode"],
            open_creation_acknowledged=payload["openCreationAcknowledged"],
            source_type=payload["sourceType"],
            source_ref=payload["sourceRef"],
            requested_exports=tuple(payload["requestedExports"]),
        )
    except (KeyError, TypeError):
        raise BatchPersistenceError("stored rejected batch input is invalid") from None
    try:
        normalized = _normalized_classroom_request(request)
    except (AttributeError, TypeError, ValueError):
        raise BatchPersistenceError("stored rejected batch input is invalid") from None
    if canonical_json_bytes(normalized) != canonical_json_bytes(payload):
        raise BatchPersistenceError("stored rejected batch input is invalid")
    return request


def _batch_id(
    context: TenantContext,
    idempotency_key: str,
    items: tuple[BatchItemInput, ...],
) -> str:
    try:
        normalized_items = [
            {
                "itemId": item.id,
                "classroom": _normalized_classroom_request(item.classroom),
            }
            for item in items
        ]
        key_digest = hashlib.sha256(f"{context.tenant_id}\0{idempotency_key}".encode()).hexdigest()
        request_digest = hashlib.sha256(canonical_json_bytes(normalized_items)).hexdigest()
    except (AttributeError, TypeError, ValueError):
        raise InvalidBatchRequest("batch item request is invalid") from None
    return f"batch-{key_digest[:20]}-{request_digest[:32]}"


def _item_status(classroom_status: str) -> str:
    if classroom_status == "awaiting_confirmation":
        return "awaiting_confirmation"
    if classroom_status in {"succeeded", "success"}:
        return "succeeded"
    if classroom_status == "failed":
        return "failed"
    if classroom_status == "canceled":
        return "canceled"
    if classroom_status in {
        "generating_outline",
        "generating_content",
        "validating",
        "materializing",
        "running",
    }:
        return "running"
    return "queued"


def _allows_create(context: TenantContext, request: object) -> bool:
    resource = ResourceScope(
        tenant_id=context.tenant_id,
        course_id=str(getattr(request, "course_id")),
        class_id=str(getattr(request, "class_id")),
    )
    return any(grant.allows_resource("classroom.create", resource) for grant in context.permissions)


class BatchService:
    """Create independent classroom jobs while preserving sibling outcomes."""

    def __init__(
        self,
        repository: BatchRepository,
        classrooms: BatchClassroomGateway,
        jobs: BatchJobGateway | None = None,
    ) -> None:
        self._repository = repository
        self._classrooms = classrooms
        self._jobs = jobs

    async def create(
        self,
        context: TenantContext,
        items: tuple[BatchItemInput, ...],
        *,
        idempotency_key: str,
    ) -> BatchJobRecord:
        if (
            not isinstance(idempotency_key, str)
            or _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None
        ):
            raise InvalidBatchRequest("batch idempotency key is invalid")
        if not items or len(items) > 100:
            raise InvalidBatchRequest("batch must contain between 1 and 100 items")
        item_ids = tuple(item.id for item in items)
        if len(set(item_ids)) != len(item_ids):
            raise InvalidBatchRequest("batch item ids must be unique")

        batch_id = _batch_id(context, idempotency_key, items)
        if any(not _allows_create(context, item.classroom) for item in items):
            raise BatchAccessDenied("batch creation is denied")

        batch = await self._repository.create(batch_id, context.user_id, item_ids)
        if (
            batch.id != batch_id
            or batch.tenant_id != context.tenant_id
            or batch.actor_id != context.user_id
            or tuple(item.id for item in batch.items) != item_ids
        ):
            raise BatchIdempotencyConflict("batch idempotency key conflicts")

        existing_items = {item.id: item for item in batch.items}
        for item in items:
            if existing_items[item.id].generation_job_id is not None:
                continue
            try:
                classroom = await self._classrooms.create(
                    context,
                    item.classroom,
                    batch_id=batch_id,
                    item_id=item.id,
                )
                job_id = getattr(classroom, "job_id", None)
                draft_id = getattr(classroom, "draft_id", None)
                asset_id = getattr(classroom, "asset_id", None)
                if not all(
                    isinstance(value, str) and value for value in (job_id, draft_id, asset_id)
                ):
                    raise InvalidBatchState("batch classroom binding is unavailable")
                await self._repository.bind_item(
                    batch_id,
                    item.id,
                    generation_job_id=job_id,
                    classroom_draft_id=draft_id,
                    classroom_asset_id=asset_id,
                    status=_item_status(str(getattr(classroom, "status"))),
                )
            except BatchIdempotencyConflict:
                raise
            except BatchItemRejected:
                if self._jobs is None:
                    raise InvalidBatchState("rejected batch jobs are unavailable") from None
                rejected_job_id = await self._jobs.record_rejected(
                    context,
                    batch_id=batch_id,
                    item_id=item.id,
                    request=item.classroom,
                )
                await self._repository.bind_rejected_item(
                    batch_id,
                    item.id,
                    generation_job_id=rejected_job_id,
                )

        created = await self._repository.get(batch_id)
        if created is None:
            raise BatchNotFound("batch was not persisted")
        return created

    async def list(
        self,
        context: TenantContext,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[BatchJobRecord, ...]:
        _validate_pagination(limit, offset)
        access_scope = _batch_access_scope(context)
        if not access_scope.allows_any:
            return ()
        batches = await self._repository.list(
            access_scope=access_scope,
            limit=limit,
            offset=offset,
        )
        visible = []
        for batch in batches:
            if batch.tenant_id == context.tenant_id and await self._can_access_batch(
                context,
                batch,
            ):
                visible.append(batch)
        return tuple(visible)

    async def _can_access_batch(
        self,
        context: TenantContext,
        batch: BatchJobRecord,
    ) -> bool:
        if not batch.items:
            return False
        for item in batch.items:
            if item.resource_course_id is None or item.resource_class_id is None:
                return False
            resource = ResourceScope(
                tenant_id=context.tenant_id,
                course_id=item.resource_course_id,
                class_id=item.resource_class_id,
            )
            if not any(
                grant.allows_resource("classroom.edit", resource) for grant in context.permissions
            ):
                return False
        return True

    async def get(
        self,
        context: TenantContext,
        batch_id: str,
    ) -> BatchJobRecord | None:
        batch = await self._repository.get(batch_id)
        if (
            batch is None
            or batch.tenant_id != context.tenant_id
            or not await self._can_access_batch(context, batch)
        ):
            return None
        return batch

    async def confirm_outline(
        self,
        context: TenantContext,
        batch_id: str,
        item_id: str,
        *,
        revision: int,
        outline_sha256: str,
    ) -> BatchJobRecord:
        item = await self._validate_outline_confirmation(
            context,
            batch_id,
            item_id,
            revision=revision,
            outline_sha256=outline_sha256,
        )
        assert item.classroom_asset_id is not None
        confirmed = await self._classrooms.confirm_outline(
            context,
            item.classroom_asset_id,
            expected_revision=revision,
            expected_outline_sha256=outline_sha256,
        )
        await self._repository.set_item_status(
            batch_id,
            item_id,
            _item_status(str(getattr(confirmed, "status"))),
        )
        updated = await self._repository.get(batch_id)
        if updated is None:
            raise BatchNotFound("batch not found")
        return updated

    async def _validate_outline_confirmation(
        self,
        context: TenantContext,
        batch_id: str,
        item_id: str,
        *,
        revision: int,
        outline_sha256: str,
    ) -> BatchItemRecord:
        batch = await self.get(context, batch_id)
        if batch is None:
            raise BatchNotFound("batch not found")
        item = next((candidate for candidate in batch.items if candidate.id == item_id), None)
        if item is None or item.classroom_asset_id is None:
            raise BatchNotFound("batch item not found")
        if item.status != "awaiting_confirmation":
            raise InvalidBatchState("batch item is not awaiting outline confirmation")
        classroom = await self._classrooms.get(context, item.classroom_asset_id)
        if classroom is None:
            raise BatchNotFound("batch classroom not found")
        raw_outline = getattr(classroom, "outline", None)
        try:
            outline = OutlineBundle.model_validate(raw_outline)
        except Exception:
            raise BatchOutlineConflict("outline is unavailable") from None
        current_revision = getattr(classroom, "revision", None)
        if current_revision == revision:
            expected_sha256 = canonical_outline_sha256(outline)
            if not hmac.compare_digest(expected_sha256, outline_sha256):
                raise BatchOutlineConflict("outline hash conflicts")
        elif not isinstance(current_revision, int) or not matches_reviewed_outline_binding(
            lifecycle_state=str(getattr(classroom, "lifecycle_state", "")),
            revision=current_revision,
            outline=outline,
            confirmed_outline_sha256=getattr(
                classroom,
                "confirmed_outline_sha256",
                None,
            ),
            expected_revision=revision,
            expected_outline_sha256=outline_sha256,
        ):
            raise BatchOutlineConflict("outline revision conflicts")
        return item

    async def confirm_outlines(
        self,
        context: TenantContext,
        batch_id: str,
        confirmations: tuple[tuple[str, int, str], ...],
    ) -> BatchJobRecord:
        if not confirmations:
            raise InvalidBatchRequest("at least one outline confirmation is required")
        if len({item_id for item_id, _, _ in confirmations}) != len(confirmations):
            raise InvalidBatchRequest("outline confirmations must be unique")
        validated = []
        for item_id, revision, outline_sha256 in confirmations:
            item = await self._validate_outline_confirmation(
                context,
                batch_id,
                item_id,
                revision=revision,
                outline_sha256=outline_sha256,
            )
            validated.append((item_id, revision, outline_sha256, item))
        for item_id, revision, outline_sha256, item in validated:
            assert item.classroom_asset_id is not None
            confirmed = await self._classrooms.confirm_outline(
                context,
                item.classroom_asset_id,
                expected_revision=revision,
                expected_outline_sha256=outline_sha256,
            )
            await self._repository.set_item_status(
                batch_id,
                item_id,
                _item_status(str(getattr(confirmed, "status"))),
            )
        updated = await self._repository.get(batch_id)
        if updated is None:
            raise BatchNotFound("batch not found")
        return updated

    async def retry_item(
        self,
        context: TenantContext,
        batch_id: str,
        item_id: str,
    ) -> BatchRetryResult:
        batch = await self.get(context, batch_id)
        if batch is None:
            raise BatchNotFound("batch not found")
        if batch.actor_id != context.user_id:
            raise BatchAccessDenied("batch retry is denied")
        item = next((candidate for candidate in batch.items if candidate.id == item_id), None)
        if item is None:
            raise BatchNotFound("batch item not found")
        if item.status != "failed" or item.generation_job_id is None:
            raise InvalidBatchState("only a failed batch item can be retried")
        if self._jobs is None:
            raise InvalidBatchState("batch retry is unavailable")
        if item.classroom_draft_id is None:
            replay = await self._jobs.rejected_input(
                context,
                job_id=item.generation_job_id,
            )
            try:
                classroom = await self._classrooms.create(
                    context,
                    replay,
                    batch_id=batch_id,
                    item_id=item_id,
                    retry_of_job_id=item.generation_job_id,
                )
                new_job_id = getattr(classroom, "job_id", None)
                if not isinstance(new_job_id, str) or not new_job_id:
                    raise InvalidBatchState("retried batch job is unavailable")
            except BatchItemRejected:
                new_job_id = await self._jobs.record_rejected(
                    context,
                    batch_id=batch_id,
                    item_id=item_id,
                    request=replay,
                    retry_of_job_id=item.generation_job_id,
                )
        else:
            new_job_id = await self._jobs.retry(
                context,
                batch_id=batch_id,
                item_id=item_id,
                job_id=item.generation_job_id,
            )
        rebound = await self._repository.rebind_failed_item(
            batch_id,
            item_id,
            expected_job_id=item.generation_job_id,
            new_job_id=new_job_id,
        )
        return BatchRetryResult(parent_item_id=item_id, item=rebound)

    async def cancel(
        self,
        context: TenantContext,
        batch_id: str,
    ) -> BatchJobRecord:
        batch = await self.get(context, batch_id)
        if batch is None:
            raise BatchNotFound("batch not found")
        if batch.actor_id != context.user_id:
            raise BatchAccessDenied("batch cancellation is denied")
        for item in batch.items:
            if item.status != "queued":
                continue
            canceled = item.generation_job_id is None
            if item.generation_job_id is not None and self._jobs is not None:
                canceled = await self._jobs.cancel_unstarted(
                    context,
                    job_id=item.generation_job_id,
                )
            if canceled:
                await self._repository.set_item_status(
                    batch_id,
                    item.id,
                    "canceled",
                )
        updated = await self._repository.get(batch_id)
        if updated is None:
            raise BatchNotFound("batch not found")
        return updated


__all__ = [
    "BatchAccessScope",
    "BatchAccessDenied",
    "BatchIdempotencyConflict",
    "BatchItemInput",
    "BatchItemRejected",
    "BatchItemRecord",
    "BatchJobRecord",
    "BatchNotFound",
    "BatchOutlineConflict",
    "BatchPersistenceError",
    "BatchRetryResult",
    "BatchService",
    "BatchServiceError",
    "InvalidBatchRequest",
    "InvalidBatchState",
    "SqlAlchemyBatchClassroomGateway",
    "SqlAlchemyBatchJobGateway",
    "SqlAlchemyBatchRepository",
]
