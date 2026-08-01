from __future__ import annotations

import asyncio
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


def test_worker_constants_lock_the_lease_protocol() -> None:
    from deeptutor.teaching.worker import HEARTBEAT_SECONDS, LEASE_SECONDS

    assert LEASE_SECONDS == 60
    assert HEARTBEAT_SECONDS == 15


def test_explicit_retry_requires_a_new_job_identity() -> None:
    from deeptutor.teaching.repositories.jobs import build_explicit_retry_request

    original_payload = (
        '{"idempotencyKey":"key-old","jobId":"job-old",'
        '"requestId":"request-old"}'
    )
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
            self.failures: list[tuple[str, str]] = []
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

        async def fail_claim(self, _claim, *, error_category, error_code):
            self.failures.append((error_category, error_code))

        async def heartbeat_claim(self, *_args, **_kwargs):
            raise AssertionError("short test must not need a heartbeat")

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
    assert repository.failures == [("contract_invalid", "hash_invalid")]
    assert repository.finalized == 0


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
