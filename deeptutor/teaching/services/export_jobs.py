"""Build and atomically bind durable jobs for pinned classroom exports."""

from __future__ import annotations

import hashlib
from typing import Protocol

from deeptutor.teaching.contracts import (
    ExportPolicy,
    ExportRequest,
    canonical_json_bytes,
)
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneSelection,
    DataPlaneUnavailable,
)
from deeptutor.teaching.repositories.jobs import GenerationJobRequest
from deeptutor.teaching.services.exports import (
    ExportJobCommand,
    ExportRecord,
)


class ExportDataPlaneSelector(Protocol):
    async def resolve(self, tenant_id: str) -> DataPlaneSelection | None: ...


class ExportGenerationJobRepository(Protocol):
    async def create_export_job_and_reserve(
        self,
        request: GenerationJobRequest,
        *,
        export_id: str,
    ) -> object: ...


class ExportRecordRepository(Protocol):
    async def get(self, export_id: str) -> ExportRecord | None: ...


class SqlAlchemyExportJobGateway:
    """Reserve quota, create outbox work, and bind an export in one transaction."""

    def __init__(
        self,
        jobs: ExportGenerationJobRepository,
        exports: ExportRecordRepository,
        selector: ExportDataPlaneSelector,
    ) -> None:
        self._jobs = jobs
        self._exports = exports
        self._selector = selector

    async def enqueue(self, command: ExportJobCommand) -> ExportRecord:
        selection = await self._selector.resolve(command.tenant_id)
        if selection is None:
            raise DataPlaneUnavailable()
        request = ExportRequest(
            schema_version="1.0",
            tenant_id=command.tenant_id,
            job_id=command.job_id,
            idempotency_key=command.job_id,
            classroom_document_sha256=command.document_sha256,
            media_manifest_sha256=command.media_manifest_sha256,
            format=command.export_format,
            language="zh-CN",
            export_policy=ExportPolicy(
                include_source_attribution=True,
                allow_external_links=False,
            ),
        )
        payload = canonical_json_bytes(request).decode("utf-8")
        await self._jobs.create_export_job_and_reserve(
            GenerationJobRequest(
                tenant_id=command.tenant_id,
                job_id=command.job_id,
                job_kind="export",
                phase="export",
                export_format=command.export_format,
                priority="teacher",
                quota_units=1,
                actor_id=command.actor_id,
                owner_id=command.owner_id,
                visibility="private",
                request_id=command.job_id,
                idempotency_key=command.job_id,
                request_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                data_plane_mode=selection.mode,
                data_plane_route_id=selection.route_ref,
                provider_profile_id=selection.provider_profile_ref,
                worker_pool_ref=selection.worker_pool_ref,
                queue_ref=selection.queue_ref,
                request_payload=payload,
                resource_course_id=command.course_id,
                resource_class_id=command.class_id,
            ),
            export_id=command.export_id,
        )
        record = await self._exports.get(command.export_id)
        if record is None or record.job_id != command.job_id:
            raise RuntimeError("classroom export job binding is unavailable")
        return record


__all__ = ["SqlAlchemyExportJobGateway"]
