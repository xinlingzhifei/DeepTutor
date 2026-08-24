"""Durable OpenMAIC workers with fenced leases and atomic DB finalization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Protocol

from pydantic import ValidationError

from deeptutor.teaching.artifact_validation import (
    ArtifactValidationError,
    ValidatedClassroomOutput,
    ValidatedExportOutput,
    validate_export_result,
    validate_generation_result,
)
from deeptutor.teaching.artifacts import ClassroomArtifactManifest, classroom_artifact_key
from deeptutor.teaching.contracts import (
    ClassroomDocument,
    ExportRequest,
    GenerationRequest,
    OutlineBundle,
    canonical_json_bytes,
    validate_outline_binding,
)
from deeptutor.teaching.export_worker import (
    ExportInputCommitReceipt,
    ExportInputDeclaration,
    ExportInputFileDeclaration,
    load_export_input_bundle,
    stage_and_submit_pinned_export,
)
from deeptutor.teaching.job_errors import (
    JobFailure,
    can_repair_dsl,
    classify_engine_error_code,
    classify_worker_error,
    retry_delay_seconds,
)
from deeptutor.teaching.object_store import (
    ClassroomArtifactPromotionService,
    ClassroomArtifactStore,
    ObjectStoreIntegrityError,
    ObjectStoreNotFound,
)
from deeptutor.teaching.openmaic.client import EngineJob, OpenMAICRequestFailed
from deeptutor.teaching.repositories.jobs import (
    CancellationRequest,
    JobLeaseLost,
    MaterializedArtifactInput,
    SqlAlchemyGenerationJobRepository,
)
from deeptutor.teaching.scheduler import ClaimedGenerationJob, FairScheduler

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15
_MAX_CLASSROOM_DOCUMENT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LeaseHeartbeat:
    lease_token: str
    lease_expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return self.lease_expires_at <= now


class WorkerClient(Protocol):
    async def submit_outline(self, request: GenerationRequest) -> EngineJob: ...

    async def submit_content(self, request: GenerationRequest) -> EngineJob: ...

    async def submit_export(self, request: ExportRequest) -> EngineJob: ...

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

    async def poll(self, engine_job_id: str) -> EngineJob: ...

    async def cancel(self, engine_job_id: str) -> None: ...

    def stream_artifact(self, path: str) -> AsyncIterator[bytes]: ...


class WorkerClientProvider(Protocol):
    async def client_for_claim(self, claim: ClaimedGenerationJob) -> WorkerClient: ...

    async def client_for_cancellation(self, request: CancellationRequest) -> WorkerClient: ...


class WorkerStoreProvider(Protocol):
    async def store_for_tenant(self, tenant_id: str) -> ClassroomArtifactStore: ...


class DslRepairer(Protocol):
    async def repair(
        self,
        *,
        client: WorkerClient,
        request: GenerationRequest,
        result_payload: Mapping[str, Any],
        attempt: int,
    ) -> Mapping[str, Any]: ...


class _HeartbeatGuard:
    def __init__(
        self,
        repository: SqlAlchemyGenerationJobRepository,
        claim: ClaimedGenerationJob,
        *,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._repository = repository
        self._claim = claim
        self._sleep = sleep
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None

    async def __aenter__(self) -> _HeartbeatGuard:
        self._task = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                await self._sleep(HEARTBEAT_SECONDS)
                if self._stop.is_set():
                    break
                await self._repository.heartbeat_claim(
                    self._claim,
                    lease_seconds=LEASE_SECONDS,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc

    def assert_current(self) -> None:
        if self._error is not None:
            raise JobLeaseLost("worker heartbeat lost its lease") from self._error

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def _parse_canonical_payload(payload: str, expected_sha256: str) -> dict[str, Any]:
    if hashlib.sha256(payload.encode()).hexdigest() != expected_sha256:
        raise ArtifactValidationError("contract_invalid")
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError):
        raise ArtifactValidationError("contract_invalid") from None
    if not isinstance(parsed, dict):
        raise ArtifactValidationError("contract_invalid")
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if canonical != payload:
        raise ArtifactValidationError("contract_invalid")
    return parsed


def _manifest_sha256(manifest: ClassroomArtifactManifest) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "tenantId": manifest.tenant_id,
                "jobId": manifest.job_id,
                "assetId": manifest.asset_id,
                "version": manifest.version,
                "entries": [
                    {
                        "relativeName": entry.relative_name,
                        "contentType": entry.content_type,
                        "sha256": entry.sha256,
                        "size": entry.size,
                    }
                    for entry in manifest.entries
                ],
            }
        )
    ).hexdigest()


def _translate_output_store_error(
    error: Exception,
) -> ArtifactValidationError | None:
    if isinstance(error, ObjectStoreNotFound):
        return ArtifactValidationError(
            "artifact_commit_missing",
            metric_reason="missing_artifact",
        )
    if not isinstance(error, ObjectStoreIntegrityError):
        return None
    mapping = {
        "schema_invalid": "artifact_invalid",
        "receipt_mismatch": "artifact_invalid",
        "hash_mismatch": "hash_invalid",
        "size_mismatch": "artifact_invalid",
    }
    reason = error.validation_reason if error.validation_reason in mapping else "unknown"
    code = mapping.get(reason, "artifact_invalid")
    return ArtifactValidationError(
        code,
        metric_reason=reason,
    )


def _translate_output_promotion_error(
    error: Exception,
) -> ArtifactValidationError | None:
    if isinstance(error, OpenMAICRequestFailed) and error.status_code == 404:
        return ArtifactValidationError(
            "artifact_commit_missing",
            metric_reason="missing_artifact",
        )
    return _translate_output_store_error(error)


async def _confirmed_output_publish(
    store: ClassroomArtifactStore,
    manifest: ClassroomArtifactManifest,
):
    try:
        return await store.confirmed_publish(manifest)
    except (ObjectStoreIntegrityError, ObjectStoreNotFound) as exc:
        translated = _translate_output_store_error(exc)
        if translated is not None:
            raise translated from None
        raise


async def _load_promoted_classroom_document(
    store: ClassroomArtifactStore,
    *,
    object_key: str,
    expected_sha256: str,
    expected_size: int,
    expected_media_manifest_sha256: str,
    expected_classroom_id: str,
    expected_classroom_version_id: str,
) -> str:
    """Read back the committed object and return only exact canonical DSL bytes."""

    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 1
        or expected_size > _MAX_CLASSROOM_DOCUMENT_BYTES
    ):
        raise ArtifactValidationError(
            "artifact_invalid",
            metric_reason="size_mismatch",
        )
    body = bytearray()
    try:
        stream = await store.open(object_key)
        async for chunk in stream:
            if not isinstance(chunk, bytes):
                raise ArtifactValidationError("artifact_invalid")
            body.extend(chunk)
            if len(body) > expected_size or len(body) > _MAX_CLASSROOM_DOCUMENT_BYTES:
                raise ArtifactValidationError(
                    "artifact_invalid",
                    metric_reason="size_mismatch",
                )
    except (ObjectStoreIntegrityError, ObjectStoreNotFound) as exc:
        translated = _translate_output_store_error(exc)
        if translated is not None:
            raise translated from None
        raise
    payload = bytes(body)
    if len(payload) != expected_size:
        raise ArtifactValidationError(
            "artifact_invalid",
            metric_reason="size_mismatch",
        )
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ArtifactValidationError("hash_invalid")
    try:
        document = ClassroomDocument.model_validate_json(payload)
    except (ValidationError, ValueError, UnicodeError):
        raise ArtifactValidationError("dsl_invalid") from None
    if canonical_json_bytes(document) != payload:
        raise ArtifactValidationError("hash_invalid")
    raw = document.model_dump(mode="json", by_alias=True, exclude_none=True)
    file_sha256 = raw.pop("fileSha256")
    if (
        hashlib.sha256(canonical_json_bytes(raw)).hexdigest() != file_sha256
        or hashlib.sha256(canonical_json_bytes(raw["mediaManifest"])).hexdigest()
        != expected_media_manifest_sha256
        or document.classroom_id != expected_classroom_id
        or document.classroom_version_id != expected_classroom_version_id
    ):
        raise ArtifactValidationError("hash_invalid")
    return payload.decode("utf-8")


def _validation_failure(error: ArtifactValidationError) -> JobFailure:
    if error.code == "policy_denied":
        return JobFailure("policy_denied", error.code, False)
    if error.code == "source_invalid":
        return JobFailure("source_snapshot_invalid", error.code, False)
    return JobFailure("contract_invalid", error.code, False)


def _artifact_validation_metric_reason(code: str) -> str | None:
    """Map generated-output failures to the fixed, non-sensitive metric contract."""

    if code in {"source_invalid", "policy_denied"}:
        return None
    if code == "hash_invalid":
        return "hash_mismatch"
    if code == "artifact_commit_missing":
        return "missing_artifact"
    if code in {"artifact_target_mismatch", "tenant_prefix_invalid", "media_invalid"}:
        return "receipt_mismatch"
    if code == "artifact_size_invalid":
        return "size_mismatch"
    if code in {"artifact_invalid", "contract_invalid", "dsl_invalid"}:
        return "schema_invalid"
    return "unknown"


def _mark_output_validation(error: ArtifactValidationError) -> ArtifactValidationError:
    return ArtifactValidationError(
        error.code,
        metric_reason=(
            error.metric_reason
            if error.metric_reason is not None
            else _artifact_validation_metric_reason(error.code)
        ),
    )


def _validated_outline_result(
    result_payload: Mapping[str, Any],
    request: GenerationRequest,
) -> OutlineBundle:
    if set(result_payload) != {"outline"}:
        raise ArtifactValidationError("contract_invalid")
    try:
        outline = OutlineBundle.model_validate(result_payload["outline"])
    except ValidationError:
        raise ArtifactValidationError("contract_invalid") from None
    try:
        validate_outline_binding(
            outline,
            request,
            expected_confirmation_status="draft",
        )
    except ValueError:
        raise ArtifactValidationError("contract_invalid")
    return outline


class GenerationWorker:
    """Claim, execute, validate, publish, and finalize one durable job."""

    def __init__(
        self,
        *,
        scheduler: FairScheduler,
        repository: SqlAlchemyGenerationJobRepository,
        clients: WorkerClientProvider,
        stores: WorkerStoreProvider,
        worker_id: str,
        job_kind: str | None = None,
        repairer: DslRepairer | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if job_kind not in {None, "generation", "export"}:
            raise ValueError("job_kind is invalid")
        self._scheduler = scheduler
        self._repository = repository
        self._clients = clients
        self._stores = stores
        self._worker_id = worker_id
        self._job_kind = job_kind
        self._repairer = repairer
        self._sleep = sleep

    async def run_once(
        self,
        *,
        slot_pool: str,
        data_plane_route_id: str,
        provider_profile_id: str,
        worker_pool_ref: str,
        queue_ref: str,
    ) -> bool:
        claim = await self._scheduler.claim(
            slot_pool,
            data_plane_route_id=data_plane_route_id,
            provider_profile_id=provider_profile_id,
            worker_pool_ref=worker_pool_ref,
            queue_ref=queue_ref,
            worker_id=self._worker_id,
            lease_seconds=LEASE_SECONDS,
            job_kind=self._job_kind,
        )
        if claim is None:
            return False
        if self._job_kind is not None and claim.job_kind != self._job_kind:
            raise RuntimeError("scheduler returned an unexpected job kind")
        try:
            await self._run_claim(claim)
        except JobLeaseLost:
            return True
        except ArtifactValidationError as exc:
            failure = _validation_failure(exc)
            try:
                failure_kwargs: dict[str, str] = {
                    "error_category": failure.category,
                    "error_code": failure.code,
                }
                if exc.metric_reason is not None:
                    failure_kwargs["artifact_validation_reason"] = exc.metric_reason
                await self._repository.fail_claim(
                    claim,
                    **failure_kwargs,
                )
            except JobLeaseLost:
                pass
        except Exception as exc:
            failure = classify_worker_error(exc)
            try:
                if failure.retryable:
                    await self._repository.retry_claim(
                        claim,
                        error_category=failure.category,
                        error_code=failure.code,
                        delay_seconds=retry_delay_seconds(claim.attempt_count),
                    )
                else:
                    await self._repository.fail_claim(
                        claim,
                        error_category=failure.category,
                        error_code=failure.code,
                    )
            except JobLeaseLost:
                pass
        return True

    async def _run_claim(self, claim: ClaimedGenerationJob) -> None:
        payload = await self._repository.load_claimed_payload(claim)
        request_payload = _parse_canonical_payload(
            payload.request_payload,
            payload.request_sha256,
        )
        client = await self._clients.client_for_claim(claim)
        async with _HeartbeatGuard(
            self._repository,
            claim,
            sleep=self._sleep,
        ) as heartbeat:
            if payload.cancel_requested:
                await client.cancel(claim.job_id)
                heartbeat.assert_current()
                await self._repository.cancel_claim(claim)
                return
            if claim.job_kind == "export":
                request = ExportRequest.model_validate(request_payload)
                if request.tenant_id != claim.tenant_id or request.job_id != claim.job_id:
                    raise ArtifactValidationError("contract_invalid")
                location = await self._repository.get_export_input_location(
                    claim.tenant_id,
                    claim.job_id,
                )
                if location is None:
                    raise ArtifactValidationError("source_invalid")
                store = await self._stores.store_for_tenant(claim.tenant_id)
                try:
                    bundle = await load_export_input_bundle(
                        store,
                        tenant_id=claim.tenant_id,
                        job_id=claim.job_id,
                        manifest_object_key=location.manifest_object_key,
                        manifest_sha256=location.manifest_sha256,
                    )
                    submitted = await stage_and_submit_pinned_export(
                        client,
                        store,
                        request,
                        bundle,
                    )
                except ValueError:
                    raise ArtifactValidationError("source_invalid") from None
            else:
                request = GenerationRequest.model_validate(request_payload)
                if request.tenant_id != claim.tenant_id or request.job_id != claim.job_id:
                    raise ArtifactValidationError("contract_invalid")
                submitted = (
                    await client.submit_outline(request)
                    if claim.phase == "outline"
                    else await client.submit_content(request)
                )
            terminal = (
                submitted
                if submitted.status in {"succeeded", "failed", "canceled"}
                else await client.poll(submitted.job_id)
            )
            heartbeat.assert_current()
            if terminal.status == "canceled":
                await self._repository.cancel_claim(claim)
                return
            if terminal.status != "succeeded":
                error = terminal.payload.get("error")
                code = error.get("code") if isinstance(error, Mapping) else "engine_failed"
                failure = classify_engine_error_code(code)
                if failure.retryable:
                    await self._repository.retry_claim(
                        claim,
                        error_category=failure.category,
                        error_code=failure.code,
                        delay_seconds=retry_delay_seconds(claim.attempt_count),
                    )
                else:
                    await self._repository.fail_claim(
                        claim,
                        error_category=failure.category,
                        error_code=failure.code,
                    )
                return
            result_payload = terminal.payload.get("result")
            if not isinstance(result_payload, Mapping):
                raise ArtifactValidationError(
                    "contract_invalid",
                    metric_reason=(None if claim.phase == "outline" else "schema_invalid"),
                )
            if claim.phase == "outline":
                outline = _validated_outline_result(result_payload, request)
                await self._repository.complete_outline(
                    claim,
                    result_payload=canonical_json_bytes(outline).decode(),
                )
                return
            if claim.job_kind == "export":
                try:
                    await self._materialize_export(
                        claim,
                        client,
                        request_payload,
                        result_payload,
                        heartbeat,
                    )
                except ArtifactValidationError as exc:
                    raise _mark_output_validation(exc) from exc
            else:
                try:
                    await self._materialize_classroom(
                        claim,
                        client,
                        request_payload,
                        result_payload,
                        heartbeat,
                    )
                except ArtifactValidationError as exc:
                    raise _mark_output_validation(exc) from exc

    async def _validated_classroom(
        self,
        claim: ClaimedGenerationJob,
        client: WorkerClient,
        request_payload: Mapping[str, Any],
        result_payload: Mapping[str, Any],
    ) -> ValidatedClassroomOutput:
        current = result_payload
        while True:
            try:
                return validate_generation_result(
                    tenant_id=claim.tenant_id,
                    job_id=claim.job_id,
                    request_payload=request_payload,
                    result_payload=current,
                )
            except ArtifactValidationError as exc:
                payload = await self._repository.load_claimed_payload(claim)
                if (
                    exc.code != "dsl_invalid"
                    or self._repairer is None
                    or not can_repair_dsl(payload.dsl_repair_attempts)
                ):
                    raise
                attempt = await self._repository.increment_dsl_repair(claim)
                current = await self._repairer.repair(
                    client=client,
                    request=GenerationRequest.model_validate(request_payload),
                    result_payload=current,
                    attempt=attempt,
                )

    async def _promote(
        self,
        *,
        claim: ClaimedGenerationJob,
        client: WorkerClient,
        manifest: ClassroomArtifactManifest,
        download_paths: Mapping[str, str],
        target_keys: tuple[str, ...],
        heartbeat: _HeartbeatGuard,
    ) -> str:
        manifest_sha256 = _manifest_sha256(manifest)
        await self._repository.bind_promotion_manifest(
            claim,
            manifest_sha256=manifest_sha256,
        )
        store = await self._stores.store_for_tenant(claim.tenant_id)
        confirmed = await _confirmed_output_publish(store, manifest)
        if confirmed is None:
            bodies = {
                entry.relative_name: client.stream_artifact(download_paths[entry.relative_name])
                for entry in manifest.entries
            }
            try:
                await ClassroomArtifactPromotionService(store).promote(manifest, bodies)
            except Exception as exc:
                # A commit-marker write or post-commit cleanup can be ambiguous.
                # Only an exact, durable commit marker authorizes recovery.
                confirmed = await _confirmed_output_publish(store, manifest)
                if confirmed is None:
                    translated = _translate_output_promotion_error(exc)
                    if translated is not None:
                        raise translated from None
                    raise
            else:
                confirmed = await _confirmed_output_publish(store, manifest)
            heartbeat.assert_current()
            if confirmed is None:
                raise ArtifactValidationError(
                    "artifact_commit_missing",
                    metric_reason="missing_artifact",
                )
        if tuple(artifact.key for artifact in confirmed) != target_keys:
            raise ArtifactValidationError(
                "artifact_target_mismatch",
                metric_reason="receipt_mismatch",
            )
        heartbeat.assert_current()
        await self._repository.mark_object_committed(
            claim,
            manifest_sha256=manifest_sha256,
        )
        return manifest_sha256

    async def _materialize_classroom(
        self,
        claim: ClaimedGenerationJob,
        client: WorkerClient,
        request_payload: Mapping[str, Any],
        result_payload: Mapping[str, Any],
        heartbeat: _HeartbeatGuard,
    ) -> None:
        await self._repository.transition_claim(
            claim,
            expected_status="generating_content",
            target_status="validating",
            progress_percent=80,
        )
        output = await self._validated_classroom(
            claim,
            client,
            request_payload,
            result_payload,
        )
        heartbeat.assert_current()
        await self._repository.transition_claim(
            claim,
            expected_status="validating",
            target_status="materializing",
            progress_percent=90,
        )
        target = await self._repository.prepare_promotion(
            claim,
            classroom_id=output.classroom_id,
        )
        manifest = ClassroomArtifactManifest(
            tenant_id=claim.tenant_id,
            job_id=claim.job_id,
            asset_id=target.classroom_id,
            version=target.version_number,
            entries=tuple(artifact.promotion_entry() for artifact in output.artifacts),
        )
        target_keys = output.target_keys(target.version_number)
        manifest_sha256 = await self._promote(
            claim=claim,
            client=client,
            manifest=manifest,
            download_paths={
                artifact.relative_name: artifact.download_path for artifact in output.artifacts
            },
            target_keys=target_keys,
            heartbeat=heartbeat,
        )
        document_artifact = output.document_artifact
        document_key = dict(zip(output.artifacts, target_keys, strict=True))[document_artifact]
        store = await self._stores.store_for_tenant(claim.tenant_id)
        document_payload = await _load_promoted_classroom_document(
            store,
            object_key=document_key,
            expected_sha256=document_artifact.sha256,
            expected_size=document_artifact.size,
            expected_media_manifest_sha256=output.media_manifest_sha256,
            expected_classroom_id=output.classroom_id,
            expected_classroom_version_id=output.classroom_version_id,
        )
        heartbeat.assert_current()
        await self._repository.finalize_generation(
            claim,
            classroom_version_id=output.classroom_version_id,
            document_payload=document_payload,
            document_sha256=output.document_sha256,
            media_manifest_sha256=output.media_manifest_sha256,
            manifest_sha256=manifest_sha256,
            artifacts=tuple(
                MaterializedArtifactInput(
                    relative_name=artifact.relative_name,
                    object_key=object_key,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size,
                    mime_type=artifact.content_type,
                    artifact_kind=(
                        "dsl_json" if artifact.relative_name == "classroom.json" else "media"
                    ),
                )
                for artifact, object_key in zip(output.artifacts, target_keys, strict=True)
            ),
        )

    async def _materialize_export(
        self,
        claim: ClaimedGenerationJob,
        client: WorkerClient,
        request_payload: Mapping[str, Any],
        result_payload: Mapping[str, Any],
        heartbeat: _HeartbeatGuard,
    ) -> None:
        await self._repository.transition_claim(
            claim,
            expected_status="exporting",
            target_status="validating",
            progress_percent=80,
        )
        output: ValidatedExportOutput = validate_export_result(
            tenant_id=claim.tenant_id,
            job_id=claim.job_id,
            request_payload=request_payload,
            result_payload=result_payload,
        )
        await self._repository.transition_claim(
            claim,
            expected_status="validating",
            target_status="materializing",
            progress_percent=90,
        )
        target = await self._repository.prepare_export_promotion(claim)
        manifest = ClassroomArtifactManifest(
            tenant_id=claim.tenant_id,
            job_id=claim.job_id,
            asset_id=target.classroom_id,
            version=target.version_number,
            entries=(output.artifact.promotion_entry(),),
        )
        target_key = classroom_artifact_key(
            claim.tenant_id,
            target.classroom_id,
            target.version_number,
            output.artifact.relative_name,
        )
        manifest_sha256 = await self._promote(
            claim=claim,
            client=client,
            manifest=manifest,
            download_paths={output.artifact.relative_name: output.artifact.download_path},
            target_keys=(target_key,),
            heartbeat=heartbeat,
        )
        await self._repository.finalize_export(
            claim,
            input_document_sha256=output.input_classroom_document_sha256,
            input_media_manifest_sha256=output.input_media_manifest_sha256,
            manifest_sha256=manifest_sha256,
            artifact=MaterializedArtifactInput(
                relative_name=output.artifact.relative_name,
                object_key=target_key,
                sha256=output.artifact.sha256,
                size_bytes=output.artifact.size,
                mime_type=output.artifact.content_type,
                artifact_kind="export",
            ),
        )

    async def cancel(self, tenant_id: str, job_id: str) -> bool:
        request = await self._repository.request_cancel(tenant_id, job_id)
        if request is None:
            return False
        if request.running:
            client = await self._clients.client_for_cancellation(request)
            await client.cancel(job_id)
            await self._repository.finish_requested_cancellation(tenant_id, job_id)
        return True


class GenerationLeaseReaper:
    def __init__(self, repository: SqlAlchemyGenerationJobRepository) -> None:
        self._repository = repository

    async def run_once(self) -> bool:
        return await self._repository.reap_one_expired() is not None


__all__ = [
    "GenerationLeaseReaper",
    "GenerationWorker",
    "HEARTBEAT_SECONDS",
    "LEASE_SECONDS",
    "LeaseHeartbeat",
]
