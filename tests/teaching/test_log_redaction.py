from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from deeptutor.logging.formatters import ContextFilter, JsonlFormatter
from deeptutor.teaching.health import log_generation_failure


def test_teaching_logs_redact_sensitive_values(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="deeptutor.teaching.health")

    log_generation_failure(
        tenant_id="tenant-a",
        job_id="job-a",
        route_id="route-a",
        error_code="provider_unavailable",
        source_text="private textbook content",
        provider_key="sk-secret",
    )

    assert "private textbook content" not in caplog.text
    assert "sk-secret" not in caplog.text
    record = caplog.records[-1]
    assert record.tenant_id == "tenant-a"
    assert record.job_id == "job-a"
    assert record.route_id == "route-a"
    assert record.error_code == "provider_unavailable"
    assert not hasattr(record, "source_text")
    assert not hasattr(record, "provider_key")


def test_structured_teaching_log_contains_only_allowlisted_context(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="deeptutor.teaching.health")
    log_generation_failure(
        tenant_id="tenant-a",
        job_id="job-a",
        route_id="route-a",
        error_code="generation_failed",
        source_text="do not log me",
        provider_key="do-not-log-me",
    )
    record = caplog.records[-1]
    assert ContextFilter().filter(record)

    payload = json.loads(JsonlFormatter().format(record))

    assert payload["message"] == "Classroom generation failed"
    assert payload["context"] == {
        "tenant_id": "tenant-a",
        "job_id": "job-a",
        "route_id": "route-a",
        "error_code": "generation_failed",
    }


@pytest.mark.asyncio
async def test_generation_worker_routes_sensitive_failure_through_redacted_logger(
    monkeypatch,
    caplog,
) -> None:
    from deeptutor.teaching.contracts import GenerationRequest, canonical_json_bytes
    from deeptutor.teaching.repositories.jobs import ClaimedJobPayload
    import deeptutor.teaching.worker as worker_module
    from tests.teaching.test_contracts import valid_generation_request

    source_text = "private textbook content"
    provider_key = "sk-secret"
    failure = RuntimeError(f"source={source_text}; provider={provider_key}")
    request_payload = canonical_json_bytes(
        GenerationRequest.model_validate(valid_generation_request())
    ).decode()
    claim = SimpleNamespace(
        tenant_id="tenant-a",
        job_id="job-a",
        job_kind="generation",
        data_plane_mode="shared",
        data_plane_route_id="route-a",
        provider_profile_id=provider_key,
        attempt_count=1,
    )

    class Scheduler:
        async def claim(self, *_args, **_kwargs):
            return claim

    class Repository:
        async def load_claimed_payload(self, _claim) -> ClaimedJobPayload:
            return ClaimedJobPayload(
                request_payload=request_payload,
                request_sha256=hashlib.sha256(request_payload.encode()).hexdigest(),
                idempotency_key="key-a",
                export_format=None,
                cancel_requested=False,
                dsl_repair_attempts=0,
            )

        async def heartbeat_claim(self, *_args, **_kwargs) -> None:
            return None

        async def fail_claim(self, _claim, *, error_category, error_code) -> None:
            assert error_category == "contract_invalid"
            assert error_code == "worker_contract_invalid"

    class Clients:
        async def client_for_claim(self, _claim):
            raise failure

    worker = worker_module.GenerationWorker(
        scheduler=Scheduler(),
        repository=Repository(),
        clients=Clients(),
        stores=object(),
        worker_id="worker-a",
    )
    observed: list[dict[str, object]] = []

    def capture_redacted_failure(**context: object) -> None:
        observed.append(context)
        log_generation_failure(**context)

    monkeypatch.setattr(
        worker_module,
        "log_generation_failure",
        capture_redacted_failure,
        raising=False,
    )
    caplog.set_level(logging.ERROR, logger="deeptutor.teaching.health")

    assert await worker.run_once(
        slot_pool="generation",
        data_plane_route_id="route-a",
        provider_profile_id=provider_key,
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
    )

    assert observed == [
        {
            "tenant_id": "tenant-a",
            "job_id": "job-a",
            "route_id": "route-a",
            "error_code": "worker_contract_invalid",
            "source_text": failure,
            "provider_key": provider_key,
        }
    ]
    assert source_text not in caplog.text
    assert provider_key not in caplog.text
