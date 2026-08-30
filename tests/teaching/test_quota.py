from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from deeptutor.teaching.models.jobs import (
    GenerationJob,
    InvalidJobTransition,
    require_job_transition,
)
from deeptutor.teaching.quota import InsufficientQuota, reserve_quota
from deeptutor.teaching.repositories.jobs import (
    GenerationJobRequest,
    require_repository_transition,
)


def test_quota_reservation_returns_the_remaining_balance() -> None:
    assert reserve_quota(balance=10, requested_units=4) == 6


def test_quota_reservation_rejects_oversubscription() -> None:
    with pytest.raises(InsufficientQuota):
        reserve_quota(balance=3, requested_units=4)


def test_generation_request_sha_must_bind_the_canonical_payload_bytes() -> None:
    payload = '{"request":"bound"}'
    request = GenerationJobRequest(
        tenant_id="tenant-a",
        job_id="job-a",
        job_kind="generation",
        phase="outline",
        export_format=None,
        priority="teacher",
        quota_units=1,
        actor_id="teacher-a",
        owner_id="teacher-a",
        visibility="class",
        request_id="request-a",
        idempotency_key="idempotency-a",
        request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        data_plane_mode="shared",
        data_plane_route_id="shared-primary",
        provider_profile_id="platform-default",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        request_payload=payload,
    )

    assert request.request_sha256 == hashlib.sha256(payload.encode()).hexdigest()
    with pytest.raises(ValueError, match="does not match"):
        replace(
            request,
            request_sha256="0" * 64,
        )
    noncanonical = '{"request": "bound"}'
    with pytest.raises(ValueError, match="canonical"):
        replace(
            request,
            request_payload=noncanonical,
            request_sha256=hashlib.sha256(noncanonical.encode()).hexdigest(),
        )
    with pytest.raises(ValueError, match="lowercase"):
        replace(
            request,
            request_sha256=request.request_sha256.upper(),
        )
    for invalid_route in ("shared:primary", "shared primary", "shared\nprimary"):
        with pytest.raises(ValueError, match="data_plane_route_id"):
            replace(request, data_plane_route_id=invalid_route)
    for field in ("provider_profile_id", "worker_pool_ref", "queue_ref"):
        with pytest.raises(ValueError):
            replace(request, **{field: "trusted\nforged"})


@pytest.mark.parametrize("requested_units", [0, -1, True])
def test_quota_reservation_requires_positive_integer_units(
    requested_units: int,
) -> None:
    with pytest.raises(ValueError):
        reserve_quota(balance=10, requested_units=requested_units)


@pytest.mark.parametrize(
    ("job_kind", "states"),
    [
        (
            "generation",
            (
                "created",
                "quota_reserved",
                "queued",
                "generating_outline",
                "awaiting_confirmation",
                "queued",
                "generating_content",
                "validating",
                "materializing",
                "succeeded",
            ),
        ),
        (
            "export",
            (
                "created",
                "quota_reserved",
                "queued",
                "exporting",
                "validating",
                "materializing",
                "succeeded",
            ),
        ),
    ],
)
def test_job_state_machines_accept_only_the_declared_forward_path(
    job_kind: str,
    states: tuple[str, ...],
) -> None:
    for current, target in zip(states, states[1:]):
        require_job_transition(job_kind, current, target)


@pytest.mark.parametrize(
    ("job_kind", "current", "target"),
    [
        ("generation", "created", "queued"),
        ("generation", "queued", "exporting"),
        ("export", "queued", "generating_outline"),
        ("generation", "succeeded", "generating_content"),
        ("export", "failed", "exporting"),
        ("export", "canceled", "queued"),
        ("unknown", "created", "quota_reserved"),
        ("generation", "unknown", "queued"),
        ("generation", "unknown", "failed"),
        ("export", "unknown", "canceled"),
    ],
)
def test_job_state_machines_reject_jumps_and_terminal_revival(
    job_kind: str,
    current: str,
    target: str,
) -> None:
    with pytest.raises(InvalidJobTransition):
        require_job_transition(job_kind, current, target)


@pytest.mark.parametrize(
    ("current", "terminal"),
    [
        ("queued", "failed"),
        ("queued", "canceled"),
        ("awaiting_confirmation", "failed"),
        ("awaiting_confirmation", "canceled"),
        ("materializing", "failed"),
        ("materializing", "canceled"),
    ],
)
def test_nonterminal_jobs_can_end_without_reentering_the_running_path(
    current: str,
    terminal: str,
) -> None:
    require_job_transition("generation", current, terminal)
    require_job_transition("export", "exporting", terminal)


def test_repository_transition_cannot_cross_a_lease_or_terminal_boundary() -> None:
    require_repository_transition(
        "generation",
        "generating_content",
        "validating",
    )
    require_repository_transition(
        "generation",
        "validating",
        "materializing",
    )

    for current, target in (
        ("queued", "generating_outline"),
        ("generating_outline", "awaiting_confirmation"),
        ("queued", "canceled"),
        ("materializing", "succeeded"),
    ):
        with pytest.raises(InvalidJobTransition):
            require_repository_transition("generation", current, target)


def test_generation_job_orm_declares_the_platform_tenant_foreign_key() -> None:
    foreign_keys = {
        (
            foreign_key.parent.name,
            foreign_key.target_fullname,
            foreign_key.ondelete,
        )
        for foreign_key in GenerationJob.__table__.foreign_keys
    }

    assert foreign_keys == {
        ("retry_of_job_id", "tenant.generation_jobs.id", "RESTRICT"),
        ("tenant_id", "platform.tenants.id", "CASCADE"),
    }
