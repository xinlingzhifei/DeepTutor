from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.dialects import postgresql

from deeptutor.teaching.dispatcher import build_outbox_claim_statement
from deeptutor.teaching.job_route_binding import (
    build_locked_job_binding_statement,
)
from deeptutor.teaching.scheduler import (
    GENERATION_GLOBAL_SLOT_LIMIT,
    PRIORITY_RANK,
    STANDARD_TENANT_SLOT_LIMIT,
    FairScheduler,
    build_tenant_claim_statement,
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
