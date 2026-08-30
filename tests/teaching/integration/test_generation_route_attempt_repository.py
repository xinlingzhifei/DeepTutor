from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import uuid

import pytest
from sqlalchemy import func, make_url, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from deeptutor.teaching.models import (
    DataPlaneRoute,
    GenerationRouteAttempt,
    ProviderProfile,
    Tenant,
)
from deeptutor.teaching.models.jobs import GenerationJob
from deeptutor.teaching.openmaic.data_planes import JobRouteAttemptConflict
from deeptutor.teaching.repositories.data_planes import SqlAlchemyDataPlaneRepository
from deeptutor.teaching.repositories.jobs import JobLeaseLost
from deeptutor.teaching.schema_names import tenant_schema_name

ROOT = Path(__file__).resolve().parents[3]


def _migration_script():
    path = ROOT / "scripts" / "migrate_teaching.py"
    spec = importlib.util.spec_from_file_location("route_attempt_migrate_teaching", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_app_role_can_write_route_attempts_only_through_the_lease_fenced_function(
    generation_database,
    monkeypatch,
) -> None:
    tenant_id = f"route-fence-{uuid.uuid4().hex[:12]}"
    other_tenant_id = f"route-other-{uuid.uuid4().hex[:12]}"
    job_id = "job-1"
    lease_token = "a" * 64
    route_id = f"dedicated-{tenant_id}"
    profile_id = f"provider-{tenant_id}"
    worker_pool = f"generation-{tenant_id}"
    queue_ref = f"openmaic.{tenant_id}"
    other_route_id = f"dedicated-{other_tenant_id}"
    other_profile_id = f"provider-{other_tenant_id}"
    engine = create_async_engine(generation_database.url, poolclass=NullPool)
    platform_sessions = async_sessionmaker(engine, expire_on_commit=False)
    translated_engine = engine.execution_options(
        schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
    )
    tenant_sessions = async_sessionmaker(translated_engine, expire_on_commit=False)

    try:
        async with platform_sessions() as session:
            async with session.begin():
                session.add_all(
                    [
                        Tenant(
                            id=tenant_id,
                            name=tenant_id,
                            status="active",
                            data_plane_mode="dedicated",
                        ),
                        Tenant(
                            id=other_tenant_id,
                            name=other_tenant_id,
                            status="active",
                            data_plane_mode="dedicated",
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        ProviderProfile(
                            id=profile_id,
                            scope="dedicated",
                            tenant_id=tenant_id,
                            owner_key=tenant_id,
                            provider_type="openai-compatible",
                            model_name="route-fence-model",
                            api_base_url="https://provider.invalid/v1",
                            secret_ref=f"service-secret:{tenant_id}",
                            status="active",
                        ),
                        ProviderProfile(
                            id=other_profile_id,
                            scope="dedicated",
                            tenant_id=other_tenant_id,
                            owner_key=other_tenant_id,
                            provider_type="openai-compatible",
                            model_name="route-fence-other-model",
                            api_base_url="https://provider.invalid/v1",
                            secret_ref=f"service-secret:{other_tenant_id}",
                            status="active",
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        DataPlaneRoute(
                            id=route_id,
                            tenant_id=tenant_id,
                            owner_key=tenant_id,
                            mode="dedicated",
                            base_url="https://dedicated.invalid/api",
                            worker_pool=worker_pool,
                            queue_name=queue_ref,
                            provider_profile_id=profile_id,
                            status="active",
                            health_status="healthy",
                        ),
                        DataPlaneRoute(
                            id=other_route_id,
                            tenant_id=other_tenant_id,
                            owner_key=other_tenant_id,
                            mode="dedicated",
                            base_url="https://other.invalid/api",
                            worker_pool=f"generation-{other_tenant_id}",
                            queue_name=f"openmaic.{other_tenant_id}",
                            provider_profile_id=other_profile_id,
                            status="active",
                            health_status="healthy",
                        ),
                    ]
                )
        generation_database.migrate_tenant(tenant_id)
        async with platform_sessions() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO platform.tenant_schema_states ("
                        "tenant_id, schema_name, revision, status, verified_at, updated_at) "
                        "VALUES (:tenant_id, :schema_name, '20260830_0023', "
                        "'active', now(), now()) ON CONFLICT (tenant_id) DO UPDATE SET "
                        "schema_name = EXCLUDED.schema_name, "
                        "revision = EXCLUDED.revision, status = EXCLUDED.status, "
                        "verified_at = EXCLUDED.verified_at, updated_at = EXCLUDED.updated_at"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "schema_name": tenant_schema_name(tenant_id),
                    },
                )

        app_password = "ROUTE_ATTEMPT_APP_PASSWORD_SENTINEL_2026"
        migration_password = "ROUTE_ATTEMPT_MIGRATION_PASSWORD_SENTINEL_2026"
        migration_script = _migration_script()
        await migration_script._execute_role_bootstrap(
            engine,
            app_password=app_password,
            migration_password=migration_password,
        )
        quoted_tenant_schema = '"' + tenant_schema_name(tenant_id) + '"'
        async with engine.begin() as connection:
            # PostgreSQL row-locking clauses require UPDATE on every locked table.
            await connection.execute(
                text("GRANT USAGE, CREATE ON SCHEMA platform TO yfeistai_migrator")
            )
            await connection.execute(
                text(f"GRANT USAGE ON SCHEMA {quoted_tenant_schema} TO yfeistai_migrator")
            )
            await connection.execute(
                text(
                    "GRANT SELECT, UPDATE ON TABLE platform.tenants, "
                    "platform.tenant_schema_states, platform.data_plane_routes, "
                    "platform.provider_profiles "
                    "TO yfeistai_migrator"
                )
            )
            await connection.execute(
                text(
                    "GRANT SELECT, INSERT ON TABLE platform.generation_route_attempts "
                    "TO yfeistai_migrator"
                )
            )
            await connection.execute(
                text(
                    "GRANT SELECT, UPDATE ON TABLE "
                    f"{quoted_tenant_schema}.generation_jobs "
                    "TO yfeistai_migrator"
                )
            )
        await migration_script._grant_app_access(
            engine,
            (tenant_schema_name(tenant_id),),
        )
        app_url = make_url(generation_database.url).set(
            username="yfeistai_app",
            password=app_password,
        )
        app_engine = create_async_engine(app_url, poolclass=NullPool)
        app_sessions = async_sessionmaker(app_engine, expire_on_commit=False)

        seeded_at = datetime.now(UTC)
        async with tenant_sessions() as session:
            async with session.begin():
                session.add(
                    GenerationJob(
                        id=job_id,
                        tenant_id=tenant_id,
                        job_kind="generation",
                        phase="content",
                        export_format=None,
                        status="generating_content",
                        priority=200,
                        quota_units=1,
                        actor_id="teacher-1",
                        owner_id="teacher-1",
                        visibility="private",
                        request_id="request-1",
                        idempotency_key="idempotency-1",
                        classroom_draft_id=None,
                        batch_id=None,
                        request_sha256="b" * 64,
                        data_plane_mode="dedicated",
                        data_plane_route_id=route_id,
                        provider_profile_id=profile_id,
                        worker_pool_ref=worker_pool,
                        queue_ref=queue_ref,
                        request_payload="{}",
                        attempt_count=1,
                        lease_owner="worker-1",
                        lease_token=lease_token,
                        lease_expires_at=seeded_at + timedelta(minutes=5),
                        heartbeat_at=seeded_at,
                    )
                )
                session.add(
                    GenerationJob(
                        id="job-unavailable",
                        tenant_id=tenant_id,
                        job_kind="generation",
                        phase="content",
                        export_format=None,
                        status="generating_content",
                        priority=200,
                        quota_units=1,
                        actor_id="teacher-1",
                        owner_id="teacher-1",
                        visibility="private",
                        request_id="request-unavailable",
                        idempotency_key="idempotency-unavailable",
                        classroom_draft_id=None,
                        batch_id=None,
                        request_sha256="d" * 64,
                        data_plane_mode="dedicated",
                        data_plane_route_id=route_id,
                        provider_profile_id=profile_id,
                        worker_pool_ref=worker_pool,
                        queue_ref=queue_ref,
                        request_payload="{}",
                        attempt_count=1,
                        lease_owner="worker-1",
                        lease_token=lease_token,
                        lease_expires_at=seeded_at + timedelta(minutes=5),
                        heartbeat_at=seeded_at,
                    )
                )
                for invalid_job_id, invalid_route, invalid_profile, invalid_pool, invalid_queue in (
                    (
                        "missing-route-job",
                        "missing-route",
                        "missing-profile",
                        "missing-workers",
                        "missing-queue",
                    ),
                    (
                        "cross-tenant-route-job",
                        other_route_id,
                        other_profile_id,
                        f"generation-{other_tenant_id}",
                        f"openmaic.{other_tenant_id}",
                    ),
                    (
                        "mismatched-route-job",
                        route_id,
                        profile_id,
                        worker_pool,
                        "mismatched-queue",
                    ),
                ):
                    session.add(
                        GenerationJob(
                            id=invalid_job_id,
                            tenant_id=tenant_id,
                            job_kind="generation",
                            phase="content",
                            export_format=None,
                            status="generating_content",
                            priority=200,
                            quota_units=1,
                            actor_id="teacher-1",
                            owner_id="teacher-1",
                            visibility="private",
                            request_id=f"request-{invalid_job_id}",
                            idempotency_key=f"idempotency-{invalid_job_id}",
                            classroom_draft_id=None,
                            batch_id=None,
                            request_sha256="c" * 64,
                            data_plane_mode="dedicated",
                            data_plane_route_id=invalid_route,
                            provider_profile_id=invalid_profile,
                            worker_pool_ref=invalid_pool,
                            queue_ref=invalid_queue,
                            request_payload="{}",
                            attempt_count=1,
                            lease_owner="worker-1",
                            lease_token=lease_token,
                            lease_expires_at=seeded_at + timedelta(minutes=5),
                            heartbeat_at=seeded_at,
                        )
                    )

        async with app_engine.connect() as connection:
            privilege_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT privilege_type FROM information_schema.role_table_grants "
                            "WHERE grantee = 'yfeistai_app' "
                            "AND table_schema = 'platform' "
                            "AND table_name = 'generation_route_attempts' "
                            "ORDER BY privilege_type"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert privilege_rows == []
            owners = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT procedure.proname, "
                            "pg_catalog.pg_get_userbyid(procedure.proowner) "
                            "FROM pg_catalog.pg_proc AS procedure "
                            "JOIN pg_catalog.pg_namespace AS namespace "
                            "ON namespace.oid = procedure.pronamespace "
                            "WHERE namespace.nspname = 'platform' "
                            "AND procedure.proname IN ("
                            "'record_generation_route_attempt', "
                            "'read_generation_route_attempts')"
                        )
                    )
                ).all()
            )
            assert owners == {
                "read_generation_route_attempts": "yfeistai_migrator",
                "record_generation_route_attempt": "yfeistai_migrator",
            }
            execute_privileges = (
                await connection.execute(
                    text(
                        "SELECT has_function_privilege(current_user, "
                        "'platform.record_generation_route_attempt(text, text, text, "
                        "integer, text, text, text, text, text, text, text, text, "
                        "text, text, text)', "
                        "'EXECUTE'), has_function_privilege(current_user, "
                        "'platform.read_generation_route_attempts(text, text, text, "
                        "text, text, text, text)', 'EXECUTE')"
                    )
                )
            ).one()
            assert tuple(execute_privileges) == (True, True)
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "SELECT * FROM platform.generation_route_attempts "
                        "WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
            await connection.rollback()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO platform.generation_route_attempts ("
                        "tenant_id, job_id, attempt_count, phase, decision, "
                        "data_plane_mode, data_plane_route_id, provider_profile_id, "
                        "worker_pool_ref, queue_ref, worker_id) VALUES ("
                        ":tenant_id, :job_id, 1, 'content', 'selected', 'dedicated', "
                        ":route_id, :profile_id, :worker_pool, :queue_ref, 'worker-1')"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "job_id": job_id,
                        "route_id": route_id,
                        "profile_id": profile_id,
                        "worker_pool": worker_pool,
                        "queue_ref": queue_ref,
                    },
                )
            await connection.rollback()

        @asynccontextmanager
        async def sessions():
            async with app_sessions() as session:
                yield session

        monkeypatch.setattr(
            "deeptutor.teaching.repositories.data_planes.platform_session",
            sessions,
        )
        repository = SqlAlchemyDataPlaneRepository()
        selection = await repository.resolve_worker_selection(
            tenant_id=tenant_id,
            route_id=route_id,
            provider_profile_id=profile_id,
            worker_pool_ref=worker_pool,
            queue_ref=queue_ref,
        )
        assert selection is not None
        record = {
            "tenant_id": tenant_id,
            "job_id": job_id,
            "phase": "content",
            "attempt_count": 1,
            "mode": "dedicated",
            "data_plane_route_id": route_id,
            "provider_profile_id": profile_id,
            "worker_pool_ref": worker_pool,
            "queue_ref": queue_ref,
            "worker_id": "worker-1",
            "lease_token": lease_token,
            "outcome": "selected",
            "config_revision": selection.config_revision,
            "route_config_digest": selection.route_config_digest,
            "provider_config_digest": selection.provider_config_digest,
        }

        await repository.record_job_route_attempt(**record)
        await repository.record_job_route_attempt(**record)
        async with platform_sessions() as session:
            async with session.begin():
                await session.execute(
                    update(DataPlaneRoute)
                    .where(DataPlaneRoute.id == route_id)
                    .values(status="disabled", health_status="unhealthy")
                )
                await session.execute(
                    update(ProviderProfile)
                    .where(ProviderProfile.id == profile_id)
                    .values(status="disabled")
                )
        with pytest.raises(JobLeaseLost):
            await repository.record_job_route_attempt(**{**record, "job_id": "job-unavailable"})
        await repository.record_job_route_attempt(
            **{
                **record,
                "job_id": "job-unavailable",
                "outcome": "unavailable",
                "config_revision": None,
                "route_config_digest": None,
                "provider_config_digest": None,
            }
        )
        async with platform_sessions() as session:
            async with session.begin():
                await session.execute(
                    update(DataPlaneRoute)
                    .where(DataPlaneRoute.id == route_id)
                    .values(status="active", health_status="healthy")
                )
                await session.execute(
                    update(ProviderProfile)
                    .where(ProviderProfile.id == profile_id)
                    .values(status="active")
                )

        summary = await repository.resolve_job_route_audit(
            tenant_id,
            job_id,
            phase="content",
            expected_attempt_count=1,
            expected_data_plane_mode="dedicated",
            expected_route_id=route_id,
            expected_provider_profile_id=profile_id,
            expected_worker_pool_ref=worker_pool,
            expected_queue_ref=queue_ref,
        )
        assert summary is not None
        assert summary.dedicated_attempt_count == 1
        assert summary.final_phase_selected is True

        with pytest.raises(JobRouteAttemptConflict):
            await repository.record_job_route_attempt(
                **{
                    **record,
                    "outcome": "unavailable",
                    "config_revision": None,
                    "route_config_digest": None,
                    "provider_config_digest": None,
                }
            )

        for invalid_record in (
            {
                **record,
                "job_id": "missing-route-job",
                "data_plane_route_id": "missing-route",
                "provider_profile_id": "missing-profile",
                "worker_pool_ref": "missing-workers",
                "queue_ref": "missing-queue",
            },
            {
                **record,
                "job_id": "cross-tenant-route-job",
                "data_plane_route_id": other_route_id,
                "provider_profile_id": other_profile_id,
                "worker_pool_ref": f"generation-{other_tenant_id}",
                "queue_ref": f"openmaic.{other_tenant_id}",
            },
            {
                **record,
                "job_id": "mismatched-route-job",
                "queue_ref": "mismatched-queue",
            },
        ):
            with pytest.raises(JobLeaseLost):
                await repository.record_job_route_attempt(**invalid_record)

        async with platform_sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(GenerationRouteAttempt)
                    .where(
                        GenerationRouteAttempt.tenant_id == tenant_id,
                        GenerationRouteAttempt.job_id == job_id,
                    )
                )
                == 1
            )

        async with tenant_sessions() as session:
            async with session.begin():
                await session.execute(
                    update(GenerationJob)
                    .where(GenerationJob.id == job_id)
                    .values(lease_token="c" * 64)
                )
        with pytest.raises(JobLeaseLost):
            await repository.record_job_route_attempt(**record)

        async with tenant_sessions() as session:
            async with session.begin():
                await session.execute(
                    update(GenerationJob)
                    .where(GenerationJob.id == job_id)
                    .values(
                        lease_token=lease_token,
                        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    )
                )
        with pytest.raises(JobLeaseLost):
            await repository.record_job_route_attempt(**record)

        async with platform_sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(GenerationRouteAttempt)
                    .where(
                        GenerationRouteAttempt.tenant_id == tenant_id,
                        GenerationRouteAttempt.job_id == job_id,
                    )
                )
                == 1
            )
    finally:
        if "app_engine" in locals():
            await app_engine.dispose()
        await engine.dispose()
