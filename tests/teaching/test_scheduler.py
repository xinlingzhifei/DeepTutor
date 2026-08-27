from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.teaching import scheduler as scheduler_module
from deeptutor.teaching.dispatcher import (
    build_job_queue_transition_statement,
    build_outbox_claim_statement,
)
from deeptutor.teaching.job_route_binding import (
    build_locked_job_binding_statement,
)
from deeptutor.teaching.scheduler import (
    GENERATION_GLOBAL_SLOT_LIMIT,
    PRIORITY_RANK,
    STANDARD_TENANT_SLOT_LIMIT,
    FairScheduler,
    build_tenant_claim_statement,
    eligible_queue_wait_seconds,
    slot_pool_for,
)


def _postgresql_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_priority_order_protects_interactive_and_teacher_work() -> None:
    assert PRIORITY_RANK["student_micro"] > PRIORITY_RANK["interaction"]
    assert PRIORITY_RANK["interaction"] > PRIORITY_RANK["teacher"]
    assert PRIORITY_RANK["teacher"] > PRIORITY_RANK["full"]
    assert PRIORITY_RANK["full"] > PRIORITY_RANK["batch"]


def test_generation_capacity_contract_is_twenty_global_and_two_per_tenant() -> None:
    assert GENERATION_GLOBAL_SLOT_LIMIT == 20
    assert STANDARD_TENANT_SLOT_LIMIT == 2


def test_generation_claim_audit_is_secret_free_and_bound_to_the_claim() -> None:
    queue_job = SimpleNamespace(
        tenant_id="tenant-a",
        job_id="job-a",
        job_kind="generation",
    )

    audit = scheduler_module._generation_claim_audit(queue_job, worker_id="worker-a")

    assert audit is not None
    assert audit.tenant_id == "tenant-a"
    assert audit.actor_id == "worker-a"
    assert audit.action == "generation.job_claimed"
    assert audit.resource_type == "generation_job"
    assert audit.resource_id == "job-a"
    assert "lease" not in vars(audit)
    queue_job.job_kind = "export"
    assert scheduler_module._generation_claim_audit(queue_job, worker_id="worker-a") is None


def test_mp4_exports_use_a_separate_slot_pool() -> None:
    assert slot_pool_for("generation", None) == "generation"
    for export_format in ("classroom_zip", "pptx", "offline_html"):
        assert slot_pool_for("export", export_format) == "generation"
    assert slot_pool_for("export", "mp4") == "mp4_export"


@pytest.mark.parametrize(
    ("job_kind", "export_format"),
    [
        ("generation", "mp4"),
        ("export", None),
        ("export", "pdf"),
        ("unknown", None),
    ],
)
def test_slot_pool_rejects_inconsistent_job_shapes(
    job_kind: str,
    export_format: str | None,
) -> None:
    with pytest.raises(ValueError):
        slot_pool_for(job_kind, export_format)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("data_plane_route_id", "shared:primary"),
        ("data_plane_route_id", "shared primary"),
        ("provider_profile_id", "provider\nforged"),
        ("worker_pool_ref", "pool\rforged"),
        ("queue_ref", "queue\nforged"),
        ("worker_id", "worker\nforged"),
    ],
)
def test_claim_rejects_untrusted_runtime_binding_fields(
    field: str,
    value: str,
) -> None:
    arguments = {
        "data_plane_route_id": "shared-primary",
        "provider_profile_id": "platform-default",
        "worker_pool_ref": "shared-generation",
        "queue_ref": "openmaic.shared",
        "worker_id": "worker-1",
        "lease_seconds": 60,
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        asyncio.run(FairScheduler().claim("generation", **arguments))


def test_slot_initialization_rejects_a_forged_worker_pool() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            FairScheduler().ensure_slots(
                [],
                worker_pool_ref="shared\nforged",
                slot_pool="generation",
                global_limit=20,
                tenant_limit=2,
            )
        )


def test_outbox_and_tenant_claims_are_skip_locked() -> None:
    outbox_sql = _postgresql_sql(build_outbox_claim_statement())
    tenant_sql = _postgresql_sql(
        build_tenant_claim_statement(
            "shared-primary",
            "platform-default",
            "shared-generation",
            "openmaic.shared",
            "generation",
        )
    )

    assert "FOR UPDATE" in outbox_sql and "SKIP LOCKED" in outbox_sql
    assert "FOR UPDATE" in tenant_sql and "SKIP LOCKED" in tenant_sql
    assert "tenants.status = 'active'" in outbox_sql
    assert "tenants.status = 'active'" in tenant_sql
    assert "available_at <= now()" in outbox_sql
    assert "last_dispatched_at" in tenant_sql


def test_dispatcher_first_queue_transition_excludes_already_queued_repairs() -> None:
    transition_sql = str(build_job_queue_transition_statement("tenant-a"))

    assert "status = 'quota_reserved'" in transition_sql
    assert "status IN ('quota_reserved', 'queued')" not in transition_sql
    for authoritative_column in (
        "status",
        "job_kind",
        "phase",
        "export_format",
        "priority",
        "data_plane_route_id",
        "provider_profile_id",
        "worker_pool_ref",
        "queue_ref",
    ):
        assert authoritative_column in transition_sql.split("RETURNING", maxsplit=1)[1]


def test_scheduler_queue_wait_measures_only_eligible_nonnegative_time() -> None:
    now = datetime(2026, 8, 25, 4, 0, tzinfo=UTC)

    assert eligible_queue_wait_seconds(now, now - timedelta(seconds=2.5)) == 2.5
    assert eligible_queue_wait_seconds(now, now + timedelta(seconds=1)) == 0.0


@pytest.mark.parametrize(
    ("queue_shape", "job_shape"),
    (
        (
            {
                "job_kind": "generation",
                "phase": "content",
                "slot_pool": "generation",
                "priority": 300,
            },
            {"job_kind": "generation", "phase": "content", "export_format": None, "priority": 500},
        ),
        (
            {"job_kind": "export", "phase": "export", "slot_pool": "generation", "priority": 300},
            {"job_kind": "export", "phase": "export", "export_format": "mp4", "priority": 300},
        ),
        (
            {"job_kind": "export", "phase": "export", "slot_pool": "mp4_export", "priority": 300},
            {"job_kind": "export", "phase": "export", "export_format": "pptx", "priority": 300},
        ),
    ),
)
def test_scheduler_rejects_queue_priority_or_slot_drift_before_claim_side_effects(
    queue_shape: dict[str, object],
    job_shape: dict[str, object],
) -> None:
    queue_job = SimpleNamespace(**queue_shape)

    with pytest.raises(
        scheduler_module.SchedulerClaimConflict,
        match="tenant job shape no longer matches queue projection",
    ):
        scheduler_module._claimed_attempt_count(
            queue_job,
            {"attempt_count": 1, **job_shape},
        )


def test_job_binding_lock_is_complete_and_uses_database_rows_as_authority() -> None:
    sql = _postgresql_sql(
        build_locked_job_binding_statement(
            tenant_id="tenant-a",
            data_plane_route_id="shared-primary",
            provider_profile_id="platform-default",
            worker_pool_ref="shared-generation",
            queue_ref="openmaic.shared",
        )
    )
    sql = sql.replace("platform.", "")

    assert "FOR UPDATE" in sql
    assert "tenants.status = 'active'" in sql
    assert "tenants.data_plane_mode = data_plane_routes.mode" in sql
    assert "data_plane_routes.id = 'shared-primary'" in sql
    assert "data_plane_routes.worker_pool = 'shared-generation'" in sql
    assert "data_plane_routes.queue_name = 'openmaic.shared'" in sql
    assert "data_plane_routes.status = 'active'" in sql
    assert "data_plane_routes.health_status = 'healthy'" in sql
    assert "provider_profiles.id = 'platform-default'" in sql
    assert "provider_profiles.status = 'active'" in sql
    assert "provider_profiles.scope = data_plane_routes.mode" in sql
    assert "provider_profiles.owner_key = data_plane_routes.owner_key" in sql
    assert "data_plane_routes.mode = 'shared'" in sql
    assert "data_plane_routes.tenant_id IS NULL" in sql
    assert "data_plane_routes.owner_key = 'shared'" in sql
    assert "data_plane_routes.mode = 'dedicated'" in sql
    assert "data_plane_routes.tenant_id = tenants.id" in sql
    assert "data_plane_routes.owner_key = tenants.id" in sql
