from contextlib import asynccontextmanager

import pytest
from sqlalchemy import CheckConstraint


class FakeEngine:
    def __init__(self) -> None:
        self.execution_options_seen = None
        self.connection_is_open = False

    def execution_options(self, **options):
        self.execution_options_seen = options
        return self

    @asynccontextmanager
    async def connect(self):
        self.connection_is_open = True
        try:
            yield object()
        finally:
            self.connection_is_open = False


@pytest.fixture
def fake_engine():
    return FakeEngine()


@pytest.mark.asyncio
async def test_tenant_session_uses_schema_translate_map(fake_engine):
    from deeptutor.teaching.database import tenant_connection

    async with tenant_connection(fake_engine, "t_acme"):
        pass

    assert fake_engine.execution_options_seen == {
        "schema_translate_map": {"tenant": "tenant_bf4fcb0bb5997635"}
    }
    assert fake_engine.connection_is_open is False


def test_model_metadata_uses_only_platform_and_logical_tenant_schemas():
    from deeptutor.teaching.models import PlatformBase, TenantBase

    assert set(PlatformBase.metadata.tables) == {
        "platform.audit_log",
        "platform.data_plane_routes",
        "platform.generation_queue",
        "platform.generation_slots",
        "platform.outbox_messages",
        "platform.provider_profiles",
        "platform.role_grants",
        "platform.tenant_default_policy_states",
        "platform.tenant_knowledge_entitlements",
        "platform.tenant_memberships",
        "platform.tenant_provisioning_jobs",
        "platform.tenant_scheduler_state",
        "platform.tenant_schema_states",
        "platform.tenant_storage_credentials",
        "platform.tenant_storage_states",
        "platform.tenants",
    }
    assert set(TenantBase.metadata.tables) == {
        "tenant.approvals",
        "tenant.artifact_promotion_states",
        "tenant.assignments",
        "tenant.batch_items",
        "tenant.batch_jobs",
        "tenant.classroom_artifacts",
        "tenant.classroom_assets",
        "tenant.classroom_drafts",
        "tenant.classroom_exports",
        "tenant.classroom_versions",
        "tenant.classes",
        "tenant.courses",
        "tenant.enrollments",
        "tenant.generation_jobs",
        "tenant.publications",
        "tenant.quota_ledger",
        "tenant.source_snapshots",
        "tenant.source_uploads",
        "tenant.teaching_briefs",
        "tenant.tenant_source_bindings",
    }

    cross_schema_foreign_keys = set()
    for table in TenantBase.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            if foreign_key.target_fullname.startswith("tenant."):
                continue
            cross_schema_foreign_keys.add(
                (
                    table.fullname,
                    foreign_key.parent.name,
                    foreign_key.target_fullname,
                )
            )
    assert cross_schema_foreign_keys == {
        (
            "tenant.generation_jobs",
            "tenant_id",
            "platform.tenants.id",
        )
    }


def test_knowledge_entitlement_identity_includes_resource_owner() -> None:
    from deeptutor.teaching.models import TenantKnowledgeEntitlement

    table = TenantKnowledgeEntitlement.__table__

    assert tuple(table.primary_key.columns.keys()) == (
        "tenant_id",
        "knowledge_resource_id",
        "resource_owner_id",
    )
    resource_id_check = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_tenant_knowledge_entitlements_resource_id"
    )
    assert str(resource_id_check.sqltext) == (
        "knowledge_resource_id ~ "
        "'^(admin|user):kb:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        "[0-9a-f]{4}-[0-9a-f]{12}$'"
    )


def test_tenant_storage_credentials_metadata_excludes_plaintext_secrets():
    from deeptutor.teaching.models import TenantStorageCredential

    table = TenantStorageCredential.__table__
    assert table.schema == "platform"
    assert set(table.columns.keys()) == {
        "tenant_id",
        "secret_ref",
        "access_key_fingerprint",
        "status",
        "rotated_at",
        "created_at",
        "updated_at",
    }
    assert {"access_key", "secret_key"}.isdisjoint(table.columns.keys())


def test_data_plane_metadata_separates_routes_from_provider_secret_references():
    from deeptutor.teaching.models import DataPlaneRoute, ProviderProfile

    assert set(DataPlaneRoute.__table__.columns.keys()) == {
        "id",
        "tenant_id",
        "owner_key",
        "mode",
        "base_url",
        "worker_pool",
        "queue_name",
        "provider_profile_id",
        "status",
        "health_status",
        "health_checked_at",
        "created_at",
        "updated_at",
    }
    assert "schema_name" not in DataPlaneRoute.__table__.columns
    assert set(ProviderProfile.__table__.columns.keys()) == {
        "id",
        "scope",
        "tenant_id",
        "owner_key",
        "provider_type",
        "model_name",
        "api_base_url",
        "secret_ref",
        "status",
        "created_at",
        "updated_at",
    }
    assert {
        "api_key",
        "access_key",
        "secret_key",
        "token",
        "password",
    }.isdisjoint(ProviderProfile.__table__.columns.keys())
