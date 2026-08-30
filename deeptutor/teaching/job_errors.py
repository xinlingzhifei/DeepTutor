"""Stable worker error taxonomy and retry decisions."""

from __future__ import annotations

from dataclasses import dataclass

from deeptutor.teaching.openmaic.auth import ServiceSecretUnavailable
from deeptutor.teaching.openmaic.client import (
    OpenMAICPollingExhausted,
    OpenMAICRequestFailed,
    OpenMAICTimeout,
    OpenMAICUnavailable,
)
from deeptutor.teaching.openmaic.data_planes import (
    DataPlaneConfigurationUnavailable,
    DataPlaneMode,
    DataPlaneUnavailable,
)

RETRYABLE_ERROR_CATEGORIES = {
    "connect_timeout",
    "read_timeout",
    "provider_429",
    "provider_5xx",
    "engine_unavailable",
    "worker_lost",
}
NON_RETRYABLE_ERROR_CATEGORIES = {
    "permission_denied",
    "policy_denied",
    "source_snapshot_invalid",
    "contract_invalid",
    "confirmed_outline_hash_mismatch",
    "data_plane_unavailable",
}
MAX_RETRY_DELAY_SECONDS = 5 * 60
MAX_DSL_REPAIR_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class JobFailure:
    category: str
    code: str
    retryable: bool


def retry_delay_seconds(attempt_count: int) -> int:
    """Return a one-based exponential retry delay capped at five minutes."""

    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int):
        raise TypeError("attempt_count must be an integer")
    if attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    return min(MAX_RETRY_DELAY_SECONDS, 2 ** min(attempt_count - 1, 30))


def can_repair_dsl(repair_attempts: int) -> bool:
    if isinstance(repair_attempts, bool) or not isinstance(repair_attempts, int):
        return False
    return 0 <= repair_attempts < MAX_DSL_REPAIR_ATTEMPTS


def classify_worker_error(
    error: BaseException,
    *,
    data_plane_mode: DataPlaneMode | None = None,
) -> JobFailure:
    """Map transport and provider failures without leaking their messages."""

    if isinstance(
        error,
        (
            DataPlaneConfigurationUnavailable,
            DataPlaneUnavailable,
            ServiceSecretUnavailable,
        ),
    ):
        return JobFailure(
            "data_plane_unavailable",
            (
                "dedicated_data_plane_unavailable"
                if data_plane_mode == "dedicated"
                else "data_plane_unavailable"
            ),
            False,
        )
    if isinstance(error, OpenMAICRequestFailed):
        if error.status_code == 429:
            return JobFailure("provider_429", "openmaic_429", True)
        if 500 <= error.status_code <= 599:
            return JobFailure("provider_5xx", "openmaic_5xx", True)
        if error.status_code in {401, 403}:
            return JobFailure("permission_denied", "openmaic_permission_denied", False)
        return JobFailure("contract_invalid", "openmaic_request_rejected", False)
    if isinstance(error, OpenMAICTimeout):
        return JobFailure("read_timeout", "openmaic_timeout", True)
    if isinstance(error, OpenMAICPollingExhausted):
        return JobFailure("read_timeout", "openmaic_polling_exhausted", True)
    if isinstance(error, OpenMAICUnavailable):
        return JobFailure(
            "engine_unavailable",
            (
                "dedicated_data_plane_unavailable"
                if data_plane_mode == "dedicated"
                else "openmaic_unavailable"
            ),
            True,
        )
    return JobFailure("contract_invalid", "worker_contract_invalid", False)


def classify_engine_error_code(code: object) -> JobFailure:
    """Classify a stable engine terminal code without message parsing."""

    normalized = str(code).strip().lower() if isinstance(code, str) else ""
    mapping = {
        "connect_timeout": "connect_timeout",
        "read_timeout": "read_timeout",
        "mp4_render_timeout": "read_timeout",
        "provider_429": "provider_429",
        "provider_5xx": "provider_5xx",
        "engine_unavailable": "engine_unavailable",
        "mp4_render_unavailable": "engine_unavailable",
        "worker_lost": "worker_lost",
        "permission_denied": "permission_denied",
        "policy_denied": "policy_denied",
        "source_snapshot_invalid": "source_snapshot_invalid",
        "contract_invalid": "contract_invalid",
        "confirmed_outline_hash_mismatch": "confirmed_outline_hash_mismatch",
    }
    category = mapping.get(normalized, "contract_invalid")
    return JobFailure(
        category=category,
        code=normalized or "engine_failed",
        retryable=category in RETRYABLE_ERROR_CATEGORIES,
    )


__all__ = [
    "JobFailure",
    "MAX_DSL_REPAIR_ATTEMPTS",
    "MAX_RETRY_DELAY_SECONDS",
    "NON_RETRYABLE_ERROR_CATEGORIES",
    "RETRYABLE_ERROR_CATEGORIES",
    "can_repair_dsl",
    "classify_engine_error_code",
    "classify_worker_error",
    "retry_delay_seconds",
]
