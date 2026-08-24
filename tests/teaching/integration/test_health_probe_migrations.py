"""PostgreSQL coverage for the authoritative tenant migration health ledger."""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.health_probes import (
    TEACHING_SCHEMA_REVISION,
    HealthProbeFailure,
    MigrationHealthProbe,
    SqlAlchemyMigrationHealthRepository,
)
from deeptutor.teaching.models import Tenant, TenantSchemaState
from deeptutor.teaching.schema_names import tenant_schema_name


async def _delete_tenants(session_factory, tenant_ids: tuple[str, ...]) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))


@pytest.mark.asyncio
async def test_migration_health_ledger_accepts_current_platform_and_all_active_tenants(
    generation_database,
) -> None:
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_ids = ("health-current-a", "health-current-b")
    repository = SqlAlchemyMigrationHealthRepository(sessions)
    try:
        async with sessions() as session:
            async with session.begin():
                for tenant_id in tenant_ids:
                    session.add(
                        Tenant(
                            id=tenant_id,
                            name=tenant_id,
                            status="active",
                            data_plane_mode="shared",
                        )
                    )
                    session.add(
                        TenantSchemaState(
                            tenant_id=tenant_id,
                            schema_name=tenant_schema_name(tenant_id),
                            revision=TEACHING_SCHEMA_REVISION,
                            status="active",
                        )
                    )

        snapshot = await repository.fetch_snapshot()

        assert snapshot.platform_revision == TEACHING_SCHEMA_REVISION
        assert snapshot.active_tenants == 2
        assert snapshot.current_tenants == 2
        assert snapshot.missing_tenants == 0
        assert snapshot.outdated_tenants == 0
        await MigrationHealthProbe(repository)()
    finally:
        await _delete_tenants(sessions, tenant_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_health_ledger_detects_missing_and_outdated_active_tenants(
    generation_database,
) -> None:
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    missing_tenant = "health-missing"
    outdated_tenant = "health-outdated"
    tenant_ids = (missing_tenant, outdated_tenant)
    repository = SqlAlchemyMigrationHealthRepository(sessions)
    try:
        async with sessions() as session:
            async with session.begin():
                session.add_all(
                    [
                        Tenant(
                            id=missing_tenant,
                            name=missing_tenant,
                            status="active",
                            data_plane_mode="shared",
                        ),
                        Tenant(
                            id=outdated_tenant,
                            name=outdated_tenant,
                            status="active",
                            data_plane_mode="shared",
                        ),
                        TenantSchemaState(
                            tenant_id=outdated_tenant,
                            schema_name=tenant_schema_name(outdated_tenant),
                            revision="outdated-revision",
                            status="active",
                        ),
                    ]
                )

        snapshot = await repository.fetch_snapshot()
        assert snapshot.active_tenants == 2
        assert snapshot.current_tenants == 0
        assert snapshot.missing_tenants == 1
        assert snapshot.outdated_tenants == 1
        with pytest.raises(HealthProbeFailure, match="tenant_revision_missing"):
            await MigrationHealthProbe(repository)()

        async with sessions() as session:
            async with session.begin():
                session.add(
                    TenantSchemaState(
                        tenant_id=missing_tenant,
                        schema_name=tenant_schema_name(missing_tenant),
                        revision=TEACHING_SCHEMA_REVISION,
                        status="active",
                    )
                )

        updated_snapshot = await repository.fetch_snapshot()
        assert updated_snapshot.missing_tenants == 0
        assert updated_snapshot.outdated_tenants == 1
        with pytest.raises(HealthProbeFailure, match="tenant_revision_outdated"):
            await MigrationHealthProbe(repository)()
    finally:
        await _delete_tenants(sessions, tenant_ids)
        await engine.dispose()
