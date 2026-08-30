from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json

import pytest


def test_retry_categories_and_backoff_are_bounded() -> None:
    from deeptutor.teaching.job_errors import (
        NON_RETRYABLE_ERROR_CATEGORIES,
        RETRYABLE_ERROR_CATEGORIES,
        retry_delay_seconds,
    )

    assert RETRYABLE_ERROR_CATEGORIES == {
        "connect_timeout",
        "read_timeout",
        "provider_429",
        "provider_5xx",
        "engine_unavailable",
        "worker_lost",
    }
    assert NON_RETRYABLE_ERROR_CATEGORIES == {
        "permission_denied",
        "policy_denied",
        "source_snapshot_invalid",
        "contract_invalid",
        "confirmed_outline_hash_mismatch",
        "data_plane_unavailable",
    }
    assert retry_delay_seconds(1) == 1
    assert retry_delay_seconds(2) == 2
    assert retry_delay_seconds(20) == 300


def test_dsl_repair_budget_never_exceeds_two() -> None:
    from deeptutor.teaching.job_errors import can_repair_dsl

    assert can_repair_dsl(0)
    assert can_repair_dsl(1)
    assert not can_repair_dsl(2)
    assert not can_repair_dsl(3)


@pytest.mark.parametrize(
    ("code", "category", "retryable"),
    [
        ("CONNECT_TIMEOUT", "connect_timeout", True),
        ("PROVIDER_429", "provider_429", True),
        ("MP4_RENDER_UNAVAILABLE", "engine_unavailable", True),
        ("POLICY_DENIED", "policy_denied", False),
        ("CONFIRMED_OUTLINE_HASH_MISMATCH", "confirmed_outline_hash_mismatch", False),
        ("UNKNOWN_FAILURE", "contract_invalid", False),
    ],
)
def test_engine_terminal_codes_use_the_fixed_retry_taxonomy(
    code: str,
    category: str,
    retryable: bool,
) -> None:
    from deeptutor.teaching.job_errors import classify_engine_error_code

    failure = classify_engine_error_code(code)
    assert failure.category == category
    assert failure.retryable is retryable


@pytest.mark.parametrize(
    "error_kind",
    [
        "route-unavailable",
        "route-configuration-unavailable",
        "service-secret-unavailable",
    ],
)
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("shared", "data_plane_unavailable"),
        ("dedicated", "dedicated_data_plane_unavailable"),
    ],
)
def test_data_plane_setup_failures_use_mode_aware_non_retryable_codes(
    error_kind: str,
    mode: str,
    expected_code: str,
) -> None:
    from deeptutor.teaching.job_errors import classify_worker_error
    from deeptutor.teaching.openmaic.auth import ServiceSecretUnavailable
    from deeptutor.teaching.openmaic.data_planes import (
        DataPlaneConfigurationUnavailable,
        DataPlaneUnavailable,
    )

    errors = {
        "route-unavailable": DataPlaneUnavailable(),
        "route-configuration-unavailable": DataPlaneConfigurationUnavailable(),
        "service-secret-unavailable": ServiceSecretUnavailable(),
    }

    failure = classify_worker_error(errors[error_kind], data_plane_mode=mode)

    assert failure.category == "data_plane_unavailable"
    assert failure.code == expected_code
    assert not failure.retryable


def test_programming_value_error_is_not_misclassified_as_data_plane_unavailable() -> None:
    from deeptutor.teaching.job_errors import classify_worker_error

    failure = classify_worker_error(
        ValueError("unrelated programming defect"),
        data_plane_mode="dedicated",
    )

    assert failure.category == "contract_invalid"
    assert failure.code == "worker_contract_invalid"
    assert not failure.retryable


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("shared", "openmaic_unavailable"),
        ("dedicated", "dedicated_data_plane_unavailable"),
    ],
)
def test_transport_unavailability_keeps_retry_policy_with_mode_aware_code(
    mode: str,
    expected_code: str,
) -> None:
    from deeptutor.teaching.job_errors import classify_worker_error
    from deeptutor.teaching.openmaic.client import OpenMAICUnavailable

    failure = classify_worker_error(
        OpenMAICUnavailable(),
        data_plane_mode=mode,
    )

    assert failure.category == "engine_unavailable"
    assert failure.code == expected_code
    assert failure.retryable


@pytest.mark.asyncio
async def test_dedicated_worker_fails_route_unavailability_with_stable_terminal_code() -> None:
    from deeptutor.teaching.contracts import GenerationRequest, canonical_json_bytes
    from deeptutor.teaching.openmaic.data_planes import DataPlaneUnavailable
    from deeptutor.teaching.repositories.jobs import ClaimedJobPayload
    from deeptutor.teaching.scheduler import ClaimedGenerationJob
    from deeptutor.teaching.worker import GenerationWorker
    from tests.teaching.test_contracts import valid_generation_request

    request = valid_generation_request()
    payload = canonical_json_bytes(GenerationRequest.model_validate(request)).decode()
    claim = ClaimedGenerationJob(
        tenant_id="tenant-1",
        job_id="job-1",
        job_kind="generation",
        phase="outline",
        status="generating_outline",
        slot_pool="generation",
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        attempt_count=1,
        lease_owner="dedicated-worker-1",
        lease_token="a" * 64,
        lease_expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        global_slot_id=1,
        tenant_slot_id=2,
    )

    class Scheduler:
        async def claim(self, *_args, **_kwargs):
            return claim

    class Repository:
        def __init__(self) -> None:
            self.failures: list[tuple[str, str]] = []
            self.retries: list[tuple[str, str]] = []

        async def load_claimed_payload(self, _claim):
            return ClaimedJobPayload(
                request_payload=payload,
                request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                idempotency_key="key-1",
                export_format=None,
                cancel_requested=False,
                dsl_repair_attempts=0,
            )

        async def heartbeat_claim(self, *_args, **_kwargs):
            return None

        async def fail_claim(self, _claim, *, error_category, error_code):
            self.failures.append((error_category, error_code))

        async def retry_claim(self, _claim, *, error_category, error_code, **_kwargs):
            self.retries.append((error_category, error_code))

    class Clients:
        async def client_for_claim(self, _claim):
            raise DataPlaneUnavailable()

    repository = Repository()
    worker = GenerationWorker(
        scheduler=Scheduler(),
        repository=repository,
        clients=Clients(),
        stores=object(),
        worker_id="dedicated-worker-1",
    )

    assert await worker.run_once(
        slot_pool="generation",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
    )
    assert repository.failures == [("data_plane_unavailable", "dedicated_data_plane_unavailable")]
    assert repository.retries == []


@pytest.mark.asyncio
async def test_worker_heartbeats_claim_before_client_selection_and_submission() -> None:
    from deeptutor.teaching.contracts import GenerationRequest, canonical_json_bytes
    from deeptutor.teaching.openmaic.client import EngineJob
    from deeptutor.teaching.repositories.jobs import ClaimedJobPayload
    from deeptutor.teaching.scheduler import ClaimedGenerationJob
    from deeptutor.teaching.worker import GenerationWorker
    from tests.teaching.test_contracts import valid_generation_request

    request = valid_generation_request()
    payload = canonical_json_bytes(GenerationRequest.model_validate(request)).decode()
    claim = ClaimedGenerationJob(
        tenant_id="tenant-1",
        job_id="job-1",
        job_kind="generation",
        phase="outline",
        status="generating_outline",
        slot_pool="generation",
        data_plane_mode="shared",
        data_plane_route_id="shared-primary",
        provider_profile_id="provider-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        attempt_count=1,
        lease_owner="worker-1",
        lease_token="a" * 64,
        lease_expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        global_slot_id=1,
        tenant_slot_id=2,
    )
    events: list[str] = []

    class Repository:
        async def load_claimed_payload(self, _claim):
            events.append("payload")
            return ClaimedJobPayload(
                request_payload=payload,
                request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                idempotency_key="key-1",
                export_format=None,
                cancel_requested=False,
                dsl_repair_attempts=0,
            )

        async def heartbeat_claim(self, _claim, *, lease_seconds):
            assert lease_seconds > 0
            events.append("heartbeat")

        async def complete_outline(self, _claim, *, result_payload):
            assert result_payload
            events.append("complete")

    class Client:
        async def submit_outline(self, _request):
            events.append("submit")
            return EngineJob(
                tenant_id="tenant-1",
                job_id="job-1",
                kind="outline",
                status="succeeded",
                payload={"result": {"outline": _draft_outline()}},
            )

    class Clients:
        async def client_for_claim(self, _claim):
            events.append("client")
            return Client()

    async def blocked_sleep(_seconds: float) -> None:
        await asyncio.Future()

    worker = GenerationWorker(
        scheduler=object(),
        repository=Repository(),
        clients=Clients(),
        stores=object(),
        worker_id="worker-1",
        sleep=blocked_sleep,
    )

    await worker._run_claim(claim)

    assert events == ["payload", "heartbeat", "client", "submit", "complete"]


@pytest.mark.asyncio
async def test_worker_initial_heartbeat_lease_loss_prevents_client_resolution() -> None:
    from deeptutor.teaching.contracts import GenerationRequest, canonical_json_bytes
    from deeptutor.teaching.repositories.jobs import ClaimedJobPayload, JobLeaseLost
    from deeptutor.teaching.scheduler import ClaimedGenerationJob
    from deeptutor.teaching.worker import GenerationWorker
    from tests.teaching.test_contracts import valid_generation_request

    request = valid_generation_request()
    payload = canonical_json_bytes(GenerationRequest.model_validate(request)).decode()
    claim = ClaimedGenerationJob(
        tenant_id="tenant-1",
        job_id="job-1",
        job_kind="generation",
        phase="outline",
        status="generating_outline",
        slot_pool="generation",
        data_plane_mode="dedicated",
        data_plane_route_id="dedicated-tenant-1",
        provider_profile_id="provider-tenant-1",
        worker_pool_ref="generation-tenant-1",
        queue_ref="openmaic.tenant-1",
        attempt_count=1,
        lease_owner="worker-1",
        lease_token="a" * 64,
        lease_expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        global_slot_id=1,
        tenant_slot_id=2,
    )
    events: list[str] = []

    class Repository:
        async def load_claimed_payload(self, _claim):
            events.append("payload")
            return ClaimedJobPayload(
                request_payload=payload,
                request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                idempotency_key="key-1",
                export_format=None,
                cancel_requested=False,
                dsl_repair_attempts=0,
            )

        async def heartbeat_claim(self, _claim, *, lease_seconds):
            assert lease_seconds > 0
            events.append("heartbeat")
            raise JobLeaseLost("lease expired before external work")

    class Clients:
        async def client_for_claim(self, _claim):
            events.append("client")
            raise AssertionError("lost lease must prevent client resolution")

    worker = GenerationWorker(
        scheduler=object(),
        repository=Repository(),
        clients=Clients(),
        stores=object(),
        worker_id="worker-1",
    )

    with pytest.raises(JobLeaseLost, match="lease expired before external work"):
        await worker._run_claim(claim)

    assert events == ["payload", "heartbeat"]


def test_worker_constants_lock_the_lease_protocol() -> None:
    from deeptutor.teaching.worker import HEARTBEAT_SECONDS, LEASE_SECONDS

    assert LEASE_SECONDS == 60
    assert HEARTBEAT_SECONDS == 15


def _run_outline_worker(result: dict[str, object]):
    from deeptutor.teaching.contracts import GenerationRequest, canonical_json_bytes
    from deeptutor.teaching.openmaic.client import EngineJob
    from deeptutor.teaching.repositories.jobs import ClaimedJobPayload
    from deeptutor.teaching.scheduler import ClaimedGenerationJob
    from deeptutor.teaching.worker import GenerationWorker
    from tests.teaching.test_contracts import valid_generation_request

    request = valid_generation_request()
    payload = canonical_json_bytes(GenerationRequest.model_validate(request)).decode()
    claim = ClaimedGenerationJob(
        tenant_id="tenant-1",
        job_id="job-1",
        job_kind="generation",
        phase="outline",
        status="generating_outline",
        slot_pool="generation",
        data_plane_mode="shared",
        data_plane_route_id="shared-primary",
        provider_profile_id="provider-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        attempt_count=1,
        lease_owner="worker-1",
        lease_token="a" * 64,
        lease_expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        global_slot_id=1,
        tenant_slot_id=2,
    )

    class Scheduler:
        async def claim(self, *_args, **_kwargs):
            return claim

    class Repository:
        def __init__(self) -> None:
            self.completed: list[str] = []
            self.failures: list[tuple[str, str]] = []

        async def load_claimed_payload(self, _claim):
            return ClaimedJobPayload(
                request_payload=payload,
                request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                idempotency_key="key-1",
                export_format=None,
                cancel_requested=False,
                dsl_repair_attempts=0,
            )

        async def complete_outline(self, _claim, *, result_payload):
            self.completed.append(result_payload)

        async def fail_claim(self, _claim, *, error_category, error_code):
            self.failures.append((error_category, error_code))

        async def heartbeat_claim(self, *_args, **_kwargs):
            return None

    class Client:
        async def submit_outline(self, _request):
            return EngineJob(
                tenant_id="tenant-1",
                job_id="job-1",
                kind="outline",
                status="succeeded",
                payload={"result": result},
            )

    class Clients:
        async def client_for_claim(self, _claim):
            return Client()

    async def blocked_sleep(_seconds: float) -> None:
        await asyncio.Future()

    repository = Repository()
    worker = GenerationWorker(
        scheduler=Scheduler(),
        repository=repository,
        clients=Clients(),
        stores=object(),
        worker_id="worker-1",
        sleep=blocked_sleep,
    )
    assert asyncio.run(
        worker.run_once(
            slot_pool="generation",
            data_plane_route_id="shared-primary",
            provider_profile_id="provider-default",
            worker_pool_ref="shared-generation",
            queue_ref="openmaic.shared",
        )
    )
    return repository


def _draft_outline() -> dict[str, object]:
    from deeptutor.teaching.contracts import OutlineBundle
    from tests.teaching.test_contracts import valid_outline_bundle

    outline = valid_outline_bundle()
    outline["outline_id"] = "outline-job-1"
    outline["confirmation_metadata"] = {"status": "draft"}
    return OutlineBundle.model_validate(outline).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )


def _unbound_draft(reason: str) -> dict[str, object]:
    outline = copy.deepcopy(_draft_outline())
    confirmation = outline["confirmationMetadata"]
    metadata = outline["generationMetadata"]
    scenes = outline["scenes"]
    coverage = outline["knowledgeCoverage"]
    assert isinstance(confirmation, dict)
    assert isinstance(metadata, dict)
    assert isinstance(scenes, list)
    assert isinstance(coverage, list)
    scene = scenes[0]
    knowledge_coverage = coverage[0]
    assert isinstance(scene, dict)
    assert isinstance(knowledge_coverage, dict)
    if reason == "draft_confirmation_audit":
        confirmation.update(
            confirmedAt="2026-07-30T08:05:00Z",
            confirmedBy="teacher-1",
        )
    elif reason == "confirmed":
        confirmation.update(
            status="confirmed",
            confirmedAt="2026-07-30T08:05:00Z",
            confirmedBy="teacher-1",
        )
    elif reason == "outline_id":
        outline["outlineId"] = "outline-job-other"
    elif reason == "brief_id":
        metadata["teachingBriefId"] = "brief-other"
    elif reason == "generator_version":
        metadata["generatorVersion"] = "0.3.2"
    elif reason == "model_id":
        metadata["modelId"] = "private-provider-model"
    elif reason == "contract_sha256":
        outline["contractSha256"] = "b" * 64
    elif reason == "missing_sources":
        outline["sourceRefs"] = []
        scene["sourceRefs"] = []
    elif reason == "duplicate_scene_knowledge":
        scene["knowledgePointIds"] = ["kp-1", "kp-1"]
    elif reason == "duplicate_coverage":
        coverage.append(copy.deepcopy(knowledge_coverage))
    elif reason == "duplicate_coverage_scene":
        knowledge_coverage["sceneIds"] = ["scene-1", "scene-1"]
    else:
        raise AssertionError(f"unknown outline mutation: {reason}")
    return outline


def test_worker_unwraps_and_validates_the_exact_outline_result_envelope() -> None:
    from deeptutor.teaching.contracts import OutlineBundle, canonical_json_bytes

    outline = _draft_outline()
    repository = _run_outline_worker({"outline": outline})

    assert repository.completed == [
        canonical_json_bytes(OutlineBundle.model_validate(outline)).decode()
    ]
    assert repository.failures == []


@pytest.mark.parametrize(
    "result",
    [
        {"outline": _draft_outline(), "unexpected": True},
        {"outline": {"outline": _draft_outline()}},
        {"outline": {"schemaVersion": "1.0"}},
    ],
)
def test_worker_rejects_malformed_outline_result_envelopes(
    result: dict[str, object],
) -> None:
    repository = _run_outline_worker(result)

    assert repository.completed == []
    assert repository.failures == [("contract_invalid", "contract_invalid")]


@pytest.mark.parametrize(
    "reason",
    [
        "draft_confirmation_audit",
        "confirmed",
        "outline_id",
        "brief_id",
        "generator_version",
        "model_id",
        "contract_sha256",
        "missing_sources",
        "duplicate_scene_knowledge",
        "duplicate_coverage",
        "duplicate_coverage_scene",
    ],
)
def test_worker_rejects_semantically_unbound_outline_drafts(reason: str) -> None:
    repository = _run_outline_worker({"outline": _unbound_draft(reason)})

    assert repository.completed == []
    assert repository.failures == [("contract_invalid", "contract_invalid")]


def test_explicit_retry_requires_a_new_job_identity() -> None:
    from deeptutor.teaching.repositories.jobs import build_explicit_retry_request

    original_payload = '{"idempotencyKey":"key-old","jobId":"job-old","requestId":"request-old"}'
    original_sha = hashlib.sha256(original_payload.encode()).hexdigest()
    original = __import__(
        "deeptutor.teaching.repositories.jobs", fromlist=["GenerationJobRequest"]
    ).GenerationJobRequest(
        tenant_id="tenant-a",
        job_id="job-old",
        job_kind="generation",
        phase="content",
        export_format=None,
        priority="teacher",
        quota_units=2,
        actor_id="teacher-a",
        owner_id="teacher-a",
        visibility="private",
        request_id="request-old",
        idempotency_key="key-old",
        request_sha256=original_sha,
        data_plane_mode="shared",
        data_plane_route_id="shared-primary",
        provider_profile_id="provider-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        request_payload=original_payload,
    )

    with pytest.raises(ValueError, match="new job identity"):
        build_explicit_retry_request(
            original,
            job_id="job-old",
            request_id="request-new",
            idempotency_key="key-new",
        )

    retried = build_explicit_retry_request(
        original,
        job_id="job-new",
        request_id="request-new",
        idempotency_key="key-new",
    )
    assert retried.job_id == "job-new"
    assert retried.retry_of_job_id == "job-old"
    assert json.loads(retried.request_payload) == {
        "idempotencyKey": "key-new",
        "jobId": "job-new",
        "requestId": "request-new",
    }
    assert retried.request_sha256 == hashlib.sha256(retried.request_payload.encode()).hexdigest()


def test_lease_claim_rejects_expired_heartbeat_timestamp() -> None:
    from deeptutor.teaching.worker import LeaseHeartbeat

    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    heartbeat = LeaseHeartbeat(
        lease_token="a" * 64,
        lease_expires_at=now,
    )
    assert heartbeat.expired(now)


def test_bad_manifest_fails_before_store_or_version_finalization() -> None:
    from deeptutor.teaching.contracts import canonical_json_bytes
    from deeptutor.teaching.openmaic.client import EngineJob
    from deeptutor.teaching.repositories.jobs import ClaimedJobPayload
    from deeptutor.teaching.scheduler import ClaimedGenerationJob
    from deeptutor.teaching.worker import GenerationWorker
    from tests.teaching.test_artifact_validation import _engine_result
    from tests.teaching_contract_fixtures import valid_content_generation_request

    request = valid_content_generation_request()
    payload = canonical_json_bytes(request).decode()
    result = _engine_result()
    result["classroomDocumentSha256"] = "0" * 64
    claim = ClaimedGenerationJob(
        tenant_id="tenant-1",
        job_id="job-1",
        job_kind="generation",
        phase="content",
        status="generating_content",
        slot_pool="generation",
        data_plane_mode="shared",
        data_plane_route_id="shared-primary",
        provider_profile_id="provider-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        attempt_count=1,
        lease_owner="worker-1",
        lease_token="a" * 64,
        lease_expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        global_slot_id=1,
        tenant_slot_id=2,
    )

    class Scheduler:
        async def claim(self, *_args, **_kwargs):
            return claim

    class Repository:
        def __init__(self) -> None:
            self.transitions: list[tuple[str, str]] = []
            self.failures: list[tuple[str, str, str | None]] = []
            self.finalized = 0

        async def load_claimed_payload(self, _claim):
            return ClaimedJobPayload(
                request_payload=payload,
                request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                idempotency_key="key-1",
                export_format=None,
                cancel_requested=False,
                dsl_repair_attempts=0,
            )

        async def transition_claim(self, _claim, **values):
            self.transitions.append((values["expected_status"], values["target_status"]))

        async def fail_claim(
            self,
            _claim,
            *,
            error_category,
            error_code,
            artifact_validation_reason=None,
        ):
            self.failures.append((error_category, error_code, artifact_validation_reason))

        async def heartbeat_claim(self, *_args, **_kwargs):
            return None

        async def finalize_generation(self, *_args, **_kwargs):
            self.finalized += 1

    class Client:
        async def submit_content(self, _request):
            return EngineJob(
                tenant_id="tenant-1",
                job_id="job-1",
                kind="content",
                status="succeeded",
                payload={"result": result},
            )

    class Clients:
        async def client_for_claim(self, _claim):
            return Client()

    class Stores:
        async def store_for_tenant(self, _tenant_id):
            raise AssertionError("invalid artifacts must never reach object storage")

    async def blocked_sleep(_seconds: float) -> None:
        await asyncio.Future()

    repository = Repository()
    worker = GenerationWorker(
        scheduler=Scheduler(),
        repository=repository,
        clients=Clients(),
        stores=Stores(),
        worker_id="worker-1",
        sleep=blocked_sleep,
    )
    claimed = asyncio.run(
        worker.run_once(
            slot_pool="generation",
            data_plane_route_id="shared-primary",
            provider_profile_id="provider-default",
            worker_pool_ref="shared-generation",
            queue_ref="openmaic.shared",
        )
    )

    assert claimed
    assert repository.transitions == [("generating_content", "validating")]
    assert repository.failures == [("contract_invalid", "hash_invalid", "hash_mismatch")]
    assert repository.finalized == 0


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("hash_invalid", "hash_mismatch"),
        ("artifact_commit_missing", "missing_artifact"),
        ("artifact_target_mismatch", "receipt_mismatch"),
        ("artifact_size_invalid", "size_mismatch"),
        ("contract_invalid", "schema_invalid"),
        ("private_new_code", "unknown"),
        ("source_invalid", None),
        ("policy_denied", None),
    ),
)
def test_output_artifact_validation_reason_is_fixed_and_excludes_input_policy(
    code: str,
    expected: str | None,
) -> None:
    from deeptutor.teaching.worker import _artifact_validation_metric_reason

    assert _artifact_validation_metric_reason(code) == expected


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("missing", ("artifact_commit_missing", "missing_artifact")),
        ("schema", ("artifact_invalid", "schema_invalid")),
        ("receipt", ("artifact_invalid", "receipt_mismatch")),
        ("hash", ("hash_invalid", "hash_mismatch")),
        ("size", ("artifact_invalid", "size_mismatch")),
        ("untagged", ("artifact_invalid", "unknown")),
        ("arbitrary", ("artifact_invalid", "unknown")),
        ("configuration", None),
    ),
)
def test_output_store_error_translation_requires_a_fixed_typed_reason(
    kind: str,
    expected: tuple[str, str] | None,
) -> None:
    from deeptutor.teaching.object_store import (
        ObjectStoreConfigurationError,
        ObjectStoreIntegrityError,
        ObjectStoreNotFound,
    )
    from deeptutor.teaching.worker import _translate_output_store_error

    if kind == "missing":
        error = ObjectStoreNotFound("private detail")
    elif kind == "configuration":
        error = ObjectStoreConfigurationError("private detail")
    else:
        reason = None if kind == "untagged" else f"{kind}_mismatch"
        if kind == "schema":
            reason = "schema_invalid"
        error = ObjectStoreIntegrityError(
            "private detail",
            validation_reason=reason,
        )

    translated = _translate_output_store_error(error)

    if expected is None:
        assert translated is None
    else:
        assert translated is not None
        assert (translated.code, translated.metric_reason) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_kind", "expected"),
    (
        ("missing", ("artifact_commit_missing", "missing_artifact")),
        ("hash", ("hash_invalid", "hash_mismatch")),
    ),
)
async def test_promoted_document_readback_translates_typed_store_failures(
    error_kind: str,
    expected: tuple[str, str],
) -> None:
    from deeptutor.teaching.artifact_validation import ArtifactValidationError
    from deeptutor.teaching.object_store import (
        ObjectStoreIntegrityError,
        ObjectStoreNotFound,
    )
    from deeptutor.teaching.worker import _load_promoted_classroom_document

    class Store:
        async def open(self, _key):
            if error_kind == "missing":
                raise ObjectStoreNotFound("private detail")
            raise ObjectStoreIntegrityError(
                "private detail",
                validation_reason="hash_mismatch",
            )

    with pytest.raises(ArtifactValidationError) as caught:
        await _load_promoted_classroom_document(
            Store(),
            object_key="tenants/tenant-1/classrooms/classroom-1/versions/1/classroom.json",
            expected_sha256="a" * 64,
            expected_size=1,
            expected_media_manifest_sha256="b" * 64,
            expected_classroom_id="classroom-1",
            expected_classroom_version_id="version-1",
        )

    assert (caught.value.code, caught.value.metric_reason) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_kind", "expected"),
    (
        ("missing", ("artifact_commit_missing", "missing_artifact")),
        ("receipt", ("artifact_invalid", "receipt_mismatch")),
    ),
)
async def test_confirmed_output_publish_translates_store_failures_at_every_call_site(
    error_kind: str,
    expected: tuple[str, str],
) -> None:
    from deeptutor.teaching.artifact_validation import ArtifactValidationError
    from deeptutor.teaching.object_store import (
        ObjectStoreIntegrityError,
        ObjectStoreNotFound,
    )
    from deeptutor.teaching.worker import _confirmed_output_publish

    class Store:
        async def confirmed_publish(self, _manifest):
            if error_kind == "missing":
                raise ObjectStoreNotFound("private detail")
            raise ObjectStoreIntegrityError(
                "private detail",
                validation_reason="receipt_mismatch",
            )

    with pytest.raises(ArtifactValidationError) as caught:
        await _confirmed_output_publish(Store(), object())

    assert (caught.value.code, caught.value.metric_reason) == expected


@pytest.mark.asyncio
async def test_output_promotion_counts_a_declared_artifact_missing_on_first_stream_read(
    tmp_path,
) -> None:
    from deeptutor.teaching.artifact_validation import ArtifactValidationError
    from deeptutor.teaching.artifacts import (
        ArtifactManifestEntry,
        ClassroomArtifactManifest,
        classroom_artifact_key,
    )
    from deeptutor.teaching.object_store import LocalClassroomArtifactStore
    from deeptutor.teaching.openmaic.client import OpenMAICRequestFailed
    from deeptutor.teaching.scheduler import ClaimedGenerationJob
    from deeptutor.teaching.worker import GenerationWorker

    payload = b"{}"
    manifest = ClassroomArtifactManifest(
        tenant_id="tenant-1",
        job_id="job-1",
        asset_id="classroom-1",
        version=1,
        entries=(
            ArtifactManifestEntry(
                "classroom.json",
                "application/json",
                hashlib.sha256(payload).hexdigest(),
                len(payload),
            ),
        ),
    )
    claim = ClaimedGenerationJob(
        tenant_id="tenant-1",
        job_id="job-1",
        job_kind="generation",
        phase="content",
        status="materializing",
        slot_pool="generation",
        data_plane_mode="shared",
        data_plane_route_id="shared-primary",
        provider_profile_id="provider-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        attempt_count=1,
        lease_owner="worker-1",
        lease_token="a" * 64,
        lease_expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        global_slot_id=1,
        tenant_slot_id=2,
    )

    class Repository:
        async def bind_promotion_manifest(self, *_args, **_kwargs):
            return None

        async def mark_object_committed(self, *_args, **_kwargs):
            raise AssertionError("missing output must not be committed")

    class Stores:
        async def store_for_tenant(self, _tenant_id):
            return LocalClassroomArtifactStore(tmp_path, "tenant-1")

    class Client:
        async def stream_artifact(self, _path):
            raise OpenMAICRequestFailed(404)
            yield b"unreachable"

    class Heartbeat:
        def assert_current(self):
            return None

    worker = GenerationWorker(
        scheduler=object(),
        repository=Repository(),
        clients=object(),
        stores=Stores(),
        worker_id="worker-1",
    )
    target_key = classroom_artifact_key(
        "tenant-1",
        "classroom-1",
        1,
        "classroom.json",
    )

    with pytest.raises(ArtifactValidationError) as caught:
        await worker._promote(
            claim=claim,
            client=Client(),
            manifest=manifest,
            download_paths={"classroom.json": "/api/yfeistai/v1/artifacts/job-1/classroom.json"},
            target_keys=(target_key,),
            heartbeat=Heartbeat(),
        )

    assert (caught.value.code, caught.value.metric_reason) == (
        "artifact_commit_missing",
        "missing_artifact",
    )


def test_running_cancel_calls_engine_before_terminal_db_update() -> None:
    from deeptutor.teaching.repositories.jobs import CancellationRequest
    from deeptutor.teaching.worker import GenerationWorker

    calls: list[str] = []

    class Repository:
        async def request_cancel(self, tenant_id, job_id):
            calls.append(f"request:{tenant_id}:{job_id}")
            return CancellationRequest(
                tenant_id=tenant_id,
                job_id=job_id,
                running=True,
                phase="content",
                data_plane_route_id="shared-primary",
                provider_profile_id="provider-default",
                worker_pool_ref="shared-generation",
                queue_ref="openmaic.shared",
            )

        async def finish_requested_cancellation(self, tenant_id, job_id):
            calls.append(f"finish:{tenant_id}:{job_id}")
            return True

    class Client:
        async def cancel(self, job_id):
            calls.append(f"engine:{job_id}")

    class Clients:
        async def client_for_cancellation(self, _request):
            return Client()

    worker = GenerationWorker(
        scheduler=object(),
        repository=Repository(),
        clients=Clients(),
        stores=object(),
        worker_id="worker-cancel",
    )
    assert asyncio.run(worker.cancel("tenant-1", "job-1"))
    assert calls == [
        "request:tenant-1:job-1",
        "engine:job-1",
        "finish:tenant-1:job-1",
    ]
