from __future__ import annotations

import json
import logging

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
