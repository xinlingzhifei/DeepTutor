from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest
from sqlalchemy import func, make_url, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from deeptutor.api.routers import auth as auth_router
from deeptutor.api.routers import tenants as tenants_router
from deeptutor.multi_user import identity as identity_module
from deeptutor.services import auth as auth_service
from deeptutor.services.auth import TokenPayload
from deeptutor.services.config import PlatformSettings
from deeptutor.teaching import database as database_module
from deeptutor.teaching import tenant_context as tenant_context_module
from deeptutor.teaching.artifacts import temporary_artifact_key, tenant_artifact_prefix
from deeptutor.teaching.models import (
    AuditLog,
    Course,
    PlatformBase,
    Tenant,
    TenantBase,
    TenantMembership,
    TenantProvisioningJob,
    TenantSchemaState,
    TenantStorageCredential,
)
from deeptutor.teaching.models.platform import RoleGrant as RoleGrantModel
from deeptutor.teaching.object_store import (
    LocalClassroomArtifactStore,
    ObjectStoreAccessDenied,
)
from deeptutor.teaching.repositories.tenants import (
    TenantAccess,
    TenantAccessDeniedError,
    TenantRepository,
    TenantSummary,
    get_tenant_repository,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.tenant_provisioning import TenantProvisioningService

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
USER_A = "user-a"
USER_B = "user-b"

TENANT_B_NAME_SENTINEL = "TENANT_B_PRIVATE_NAME_91B7"
COURSE_B_ID_SENTINEL = "course-b-private-91b7"
COURSE_B_TITLE_SENTINEL = "TENANT_B_PRIVATE_COURSE_91B7"
JOB_A = "job-a"
JOB_B_SENTINEL = "job-b-private-91b7"
AUDIT_B_ACTION_SENTINEL = "tenant-b.private.audit.91b7"
AUDIT_B_RESOURCE_SENTINEL = "tenant-b-private-resource-91b7"
POLICY_VERIFIED_ACTION = "tenant.provisioning.policy_verified"
PROVISIONING_JOB_RESOURCE = "provisioning_job"
MEMBER_B_SENTINEL = "member-b-private-91b7"
OBJECT_B_JOB_SENTINEL = "object-job-b-private-91b7"
OBJECT_B_PAYLOAD_SENTINEL = b"TENANT_B_PRIVATE_OBJECT_PAYLOAD_91B7"
AUTH_SECRET_SENTINEL = "tenant-isolation-auth-secret-91b7"
STORAGE_B_SECRET_REF_SENTINEL = "tenant-b-private-secret-ref-91b7"
STORAGE_B_FINGERPRINT_SENTINEL = "tenant-b-private-fingerprint-91b7"


@dataclass(frozen=True, slots=True)
class TenantDatabase:
    url: str


@dataclass(frozen=True, slots=True)
class IsolationHarness:
    database: TenantDatabase | None
    monkeypatch: pytest.MonkeyPatch
    tmp_path: Path

    def require_database(self) -> TenantDatabase:
        assert self.database is not None
        return self.database


@dataclass(frozen=True, slots=True)
class BoundaryObservation:
    outcome: Literal["403", "404", "empty"]
    visible_output: str
    forbidden_sentinels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BoundaryCase:
    name: str
    exercise: Callable[[IsolationHarness], BoundaryObservation]
    requires_database: bool = False


class ApiTenantRepository:
    """Minimal database-boundary substitute behind the real FastAPI chain."""

    def __init__(self, user_a_id: str) -> None:
        self.user_a_id = user_a_id
        self.summaries = {
            TENANT_A: TenantSummary(
                tenant_id=TENANT_A,
                name="Tenant A",
                status="active",
            ),
            TENANT_B: TenantSummary(
                tenant_id=TENANT_B,
                name=TENANT_B_NAME_SENTINEL,
                status="active",
            ),
        }
        self.memberships = {
            (TENANT_A, user_a_id): "active",
            (TENANT_B, USER_B): "active",
        }
        self.roles = {
            (TENANT_A, user_a_id): frozenset({"platform_admin"}),
            (TENANT_B, USER_B): frozenset({"platform_admin"}),
        }
        self.member_roles = {
            (TENANT_B, MEMBER_B_SENTINEL): frozenset({"student"}),
        }
        self.access_attempts: list[tuple[str, str, bool]] = []
        self.grant_replacements: list[tuple[str, str, frozenset[str]]] = []
        self.events: list[tuple[str, str]] = []

    async def get_tenant_access(
        self,
        tenant_id: str,
        user_id: str,
        *,
        is_platform_admin: bool,
    ) -> TenantAccess:
        self.events.append(("repository", f"{tenant_id}:{user_id}:{is_platform_admin}"))
        self.access_attempts.append((tenant_id, user_id, is_platform_admin))
        summary = self.summaries.get(tenant_id)
        if (
            summary is None
            or not is_platform_admin
            and self.memberships.get((tenant_id, user_id)) != "active"
        ):
            raise TenantAccessDeniedError(tenant_id)
        return TenantAccess(
            summary=summary,
            schema_name=tenant_schema_name(tenant_id),
            roles=self.roles.get((tenant_id, user_id), frozenset()),
        )

    async def replace_grants(
        self,
        tenant_id: str,
        user_id: str,
        roles: frozenset[str],
    ) -> None:
        self.grant_replacements.append((tenant_id, user_id, roles))
        self.member_roles[(tenant_id, user_id)] = roles


async def _initialize_database(database_url: str) -> None:
    engine = create_async_engine(database_url)
    tenant_schemas = (tenant_schema_name(TENANT_A), tenant_schema_name(TENANT_B))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA platform"))
            for schema_name in tenant_schemas:
                await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            await connection.run_sync(PlatformBase.metadata.create_all)

        for schema_name in tenant_schemas:
            tenant_engine = engine.execution_options(schema_translate_map={"tenant": schema_name})
            async with tenant_engine.begin() as connection:
                await connection.run_sync(TenantBase.metadata.create_all)

        platform_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with platform_factory() as session:
            async with session.begin():
                session.add_all(
                    [
                        Tenant(id=TENANT_A, name="Tenant A", status="active"),
                        Tenant(
                            id=TENANT_B,
                            name=TENANT_B_NAME_SENTINEL,
                            status="active",
                        ),
                    ]
                )
            async with session.begin():
                session.add_all(
                    [
                        TenantMembership(
                            tenant_id=TENANT_A,
                            user_id=USER_A,
                            status="active",
                        ),
                        TenantMembership(
                            tenant_id=TENANT_B,
                            user_id=USER_B,
                            status="active",
                        ),
                        TenantSchemaState(
                            tenant_id=TENANT_A,
                            schema_name=tenant_schema_name(TENANT_A),
                            revision="20260730_0005",
                            status="active",
                        ),
                        TenantSchemaState(
                            tenant_id=TENANT_B,
                            schema_name=tenant_schema_name(TENANT_B),
                            revision="20260730_0005",
                            status="active",
                        ),
                        TenantStorageCredential(
                            tenant_id=TENANT_A,
                            secret_ref="tenant-a-secret-ref",
                            access_key_fingerprint="tenant-a-fingerprint",
                            status="active",
                        ),
                        TenantStorageCredential(
                            tenant_id=TENANT_B,
                            secret_ref=STORAGE_B_SECRET_REF_SENTINEL,
                            access_key_fingerprint=STORAGE_B_FINGERPRINT_SENTINEL,
                            status="active",
                        ),
                        TenantProvisioningJob(
                            id=JOB_A,
                            tenant_id=TENANT_A,
                            operation="provision",
                            status="pending",
                            attempt_count=0,
                        ),
                        TenantProvisioningJob(
                            id=JOB_B_SENTINEL,
                            tenant_id=TENANT_B,
                            operation="provision",
                            status="pending",
                            attempt_count=0,
                        ),
                        AuditLog(
                            tenant_id=TENANT_B,
                            actor_id=USER_B,
                            action=AUDIT_B_ACTION_SENTINEL,
                            resource_type="private-record",
                            resource_id=AUDIT_B_RESOURCE_SENTINEL,
                        ),
                        AuditLog(
                            tenant_id=TENANT_B,
                            actor_id=None,
                            action=POLICY_VERIFIED_ACTION,
                            resource_type=PROVISIONING_JOB_RESOURCE,
                            resource_id=f"{JOB_A}:0",
                        ),
                    ]
                )

        for tenant_id, course_id, title in (
            (TENANT_A, "course-a", "Tenant A Course"),
            (TENANT_B, COURSE_B_ID_SENTINEL, COURSE_B_TITLE_SENTINEL),
        ):
            tenant_engine = engine.execution_options(
                schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
            )
            tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)
            async with tenant_factory() as session:
                async with session.begin():
                    session.add(Course(id=course_id, title=title, status="active"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def tenant_database() -> Iterator[TenantDatabase]:
    with PostgresContainer(
        "postgres:16-alpine",
        username="isolation_user",
        password="isolation_password",
        dbname="isolation",
    ) as postgres:
        sync_url = make_url(postgres.get_connection_url())
        database_url = sync_url.set(drivername="postgresql+asyncpg").render_as_string(
            hide_password=False
        )
        asyncio.run(_initialize_database(database_url))
        yield TenantDatabase(url=database_url)


def _install_database_settings(harness: IsolationHarness) -> None:
    database = harness.require_database()
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr(database.url),
    )
    harness.monkeypatch.setattr(
        database_module,
        "load_platform_settings",
        lambda: settings,
    )


def _tamper_token(token: str) -> str:
    header, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    return f"{header}.{payload}.{replacement}{signature[1:]}"


def _build_api_app(
    harness: IsolationHarness,
) -> tuple[FastAPI, ApiTenantRepository, str, str]:
    settings = SimpleNamespace(enabled=True)
    users_file = harness.tmp_path / "auth" / "users.json"
    legacy_users_file = harness.tmp_path / "legacy-auth-users.json"
    harness.monkeypatch.setattr(identity_module, "USERS_FILE", users_file)
    harness.monkeypatch.setattr(identity_module, "LEGACY_USERS_FILE", legacy_users_file)
    harness.monkeypatch.setattr(auth_service, "AUTH_ENABLED", True)
    harness.monkeypatch.setattr(auth_service, "AUTH_USERNAME", "")
    harness.monkeypatch.setattr(auth_service, "AUTH_PASSWORD_HASH", "")
    harness.monkeypatch.setattr(auth_service, "AUTH_SECRET", AUTH_SECRET_SENTINEL)
    harness.monkeypatch.setattr(auth_service, "POCKETBASE_ENABLED", False)
    harness.monkeypatch.setattr(
        tenant_context_module,
        "load_platform_settings",
        lambda: settings,
    )
    harness.monkeypatch.setattr(
        tenants_router,
        "load_platform_settings",
        lambda: settings,
    )
    harness.monkeypatch.setattr(
        auth_router,
        "load_platform_settings",
        lambda: settings,
    )
    harness.monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    harness.monkeypatch.setattr(auth_router, "POCKETBASE_ENABLED", False)

    auth_service.add_user("bootstrap-admin", "bootstrap-password", role="admin")
    auth_service.add_user(USER_A, "tenant-a-password", role="user")
    authoritative_user = auth_service.get_user_info(USER_A)
    assert authoritative_user is not None
    assert authoritative_user["role"] == "user"
    user_a_id = str(authoritative_user["id"])
    repository = ApiTenantRepository(user_a_id)
    auth_token = auth_service.create_token(USER_A, role="user", user_id=user_a_id)
    missing_user_token = auth_service.create_token(
        "missing-user",
        role="user",
        user_id="missing-user-id",
    )
    real_decode_token = auth_service.decode_token
    real_get_user_info = auth_service.get_user_info

    def decode_token(token: str) -> TokenPayload | None:
        repository.events.append(("decode", token))
        return real_decode_token(token)

    def get_user_info(username: str) -> dict[str, object] | None:
        repository.events.append(("authoritative_lookup", username))
        return real_get_user_info(username)

    harness.monkeypatch.setattr(auth_router, "decode_token", decode_token)
    harness.monkeypatch.setattr(auth_router, "get_user_info", get_user_info)
    app = FastAPI()
    app.include_router(tenants_router.router, prefix="/api/v1/tenants")
    app.dependency_overrides[get_tenant_repository] = lambda: repository
    return app, repository, auth_token, missing_user_token


def _exercise_database_schema(harness: IsolationHarness) -> BoundaryObservation:
    _install_database_settings(harness)

    async def exercise() -> tuple[object, int, tuple[tuple[str, str], ...]]:
        await database_module.dispose_platform_engine()
        try:
            async with database_module.tenant_session(TENANT_A) as session:
                foreign_course = await session.get(Course, COURSE_B_ID_SENTINEL)
                update_result = await session.execute(
                    update(Course)
                    .where(Course.id == COURSE_B_ID_SENTINEL)
                    .values(title="tenant-a-cross-tenant-update")
                )
                courses = (await session.scalars(select(Course).order_by(Course.id))).all()
                return (
                    foreign_course,
                    update_result.rowcount,
                    tuple((course.id, course.title) for course in courses),
                )
        finally:
            await database_module.dispose_platform_engine()

    foreign_course, updated_rows, listed_courses = asyncio.run(exercise())
    assert foreign_course is None
    assert updated_rows == 0
    assert listed_courses == (("course-a", "Tenant A Course"),)
    return BoundaryObservation(
        outcome="empty",
        visible_output=repr((foreign_course, updated_rows, listed_courses)),
        forbidden_sentinels=(
            COURSE_B_ID_SENTINEL,
            COURSE_B_TITLE_SENTINEL,
            tenant_schema_name(TENANT_B),
        ),
    )


def _exercise_active_tenant_cookie(harness: IsolationHarness) -> BoundaryObservation:
    app, repository, auth_token, _ = _build_api_app(harness)
    tampered_auth_token = _tamper_token(auth_token)

    with TestClient(app, raise_server_exceptions=False) as client:
        tampered_response = client.put(
            "/api/v1/tenants/active",
            headers={"Authorization": f"Bearer {tampered_auth_token}"},
            json={"tenant_id": TENANT_B},
        )
        assert tampered_response.status_code == 401
        assert repository.events == [("decode", tampered_auth_token)]
        assert repository.access_attempts == []
        repository.events.clear()

        response = client.put(
            "/api/v1/tenants/active",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"tenant_id": TENANT_B},
        )

    set_cookie = response.headers.get("set-cookie", "")
    assert response.status_code == 403
    assert "dt_tenant=" not in set_cookie
    assert repository.access_attempts == [(TENANT_B, repository.user_a_id, False)]
    assert repository.events == [
        ("decode", auth_token),
        ("authoritative_lookup", USER_A),
        ("repository", f"{TENANT_B}:{repository.user_a_id}:False"),
    ]
    return BoundaryObservation(
        outcome="403",
        visible_output=f"{response.text}\n{set_cookie}",
        forbidden_sentinels=(
            TENANT_B_NAME_SENTINEL,
            tenant_schema_name(TENANT_B),
            MEMBER_B_SENTINEL,
        ),
    )


def _exercise_permission_scope(harness: IsolationHarness) -> BoundaryObservation:
    app, repository, auth_token, missing_user_token = _build_api_app(harness)
    original_roles = repository.member_roles[(TENANT_B, MEMBER_B_SENTINEL)]

    with TestClient(app, raise_server_exceptions=False) as client:
        missing_user_response = client.put(
            f"/api/v1/tenants/{TENANT_B}/members/{MEMBER_B_SENTINEL}/grants",
            headers={
                "Authorization": f"Bearer {missing_user_token}",
                "Cookie": f"dt_tenant={TENANT_A}",
            },
            json={"roles": ["teacher"]},
        )
        assert missing_user_response.status_code == 401
        assert repository.events == [
            ("decode", missing_user_token),
            ("authoritative_lookup", "missing-user"),
        ]
        assert repository.access_attempts == []
        repository.events.clear()

        response = client.put(
            f"/api/v1/tenants/{TENANT_B}/members/{MEMBER_B_SENTINEL}/grants",
            headers={
                "Authorization": f"Bearer {auth_token}",
                "Cookie": f"dt_tenant={TENANT_A}",
            },
            json={"roles": ["teacher"]},
        )

    assert response.status_code == 403
    assert repository.access_attempts == [(TENANT_A, repository.user_a_id, False)]
    assert repository.events == [
        ("decode", auth_token),
        ("authoritative_lookup", USER_A),
        ("repository", f"{TENANT_A}:{repository.user_a_id}:False"),
    ]
    assert repository.grant_replacements == []
    assert repository.member_roles[(TENANT_B, MEMBER_B_SENTINEL)] == original_roles
    return BoundaryObservation(
        outcome="403",
        visible_output=f"{response.text}\n{repository.grant_replacements!r}",
        forbidden_sentinels=(
            TENANT_B_NAME_SENTINEL,
            tenant_schema_name(TENANT_B),
            MEMBER_B_SENTINEL,
        ),
    )


def _exercise_object_store_prefix(harness: IsolationHarness) -> BoundaryObservation:
    async def body() -> AsyncIterator[bytes]:
        yield OBJECT_B_PAYLOAD_SENTINEL

    async def exercise() -> tuple[tuple[str, ...], tuple[str, ...]]:
        root = harness.tmp_path / "tenant-objects"
        tenant_a_store = LocalClassroomArtifactStore(root, TENANT_A)
        tenant_b_store = LocalClassroomArtifactStore(root, TENANT_B)
        tenant_b_key = temporary_artifact_key(
            TENANT_B,
            OBJECT_B_JOB_SENTINEL,
            "private.bin",
        )
        digest = hashlib.sha256(OBJECT_B_PAYLOAD_SENTINEL).hexdigest()
        await tenant_b_store.put_verified(
            tenant_b_key,
            body(),
            digest,
            len(OBJECT_B_PAYLOAD_SENTINEL),
        )
        assert await tenant_b_store.exists(tenant_b_key)

        denied_messages: list[str] = []
        for operation in (
            tenant_a_store.open(tenant_b_key),
            tenant_a_store.delete(tenant_b_key),
            tenant_a_store.list_prefix(tenant_artifact_prefix(TENANT_B)),
        ):
            try:
                await operation
            except ObjectStoreAccessDenied as error:
                denied_messages.append(str(error))
            else:
                pytest.fail("tenant A unexpectedly accessed tenant B's object prefix")

        listed_by_a = await tenant_a_store.list_prefix(tenant_artifact_prefix(TENANT_A))
        return tuple(denied_messages), listed_by_a

    denied_messages, listed_by_a = asyncio.run(exercise())
    assert len(denied_messages) == 3
    assert listed_by_a == ()
    tenant_b_key = temporary_artifact_key(
        TENANT_B,
        OBJECT_B_JOB_SENTINEL,
        "private.bin",
    )
    return BoundaryObservation(
        outcome="403",
        visible_output=repr((denied_messages, listed_by_a)),
        forbidden_sentinels=(
            tenant_b_key,
            OBJECT_B_JOB_SENTINEL,
            OBJECT_B_PAYLOAD_SENTINEL.decode(),
        ),
    )


def _exercise_data_plane_route(harness: IsolationHarness) -> BoundaryObservation:
    _install_database_settings(harness)

    async def exercise() -> tuple[tuple[str, ...], str]:
        await database_module.dispose_platform_engine()
        try:
            async with database_module.platform_session() as session:
                credential_row = (
                    await session.execute(
                        select(
                            TenantStorageCredential.secret_ref,
                            TenantStorageCredential.access_key_fingerprint,
                        ).where(TenantStorageCredential.tenant_id == TENANT_B)
                    )
                ).one_or_none()
            assert credential_row is not None
            assert tuple(credential_row) == (
                STORAGE_B_SECRET_REF_SENTINEL,
                STORAGE_B_FINGERPRINT_SENTINEL,
            )

            repository = TenantRepository()
            listed = await repository.list_tenants(
                USER_A,
                is_platform_admin=False,
            )
            try:
                await repository.get_tenant_access(
                    TENANT_B,
                    USER_A,
                    is_platform_admin=False,
                )
            except TenantAccessDeniedError as error:
                denied_output = str(error)
            else:
                pytest.fail("tenant A unexpectedly resolved tenant B's data-plane route")
            return tuple(summary.tenant_id for summary in listed), denied_output
        finally:
            await database_module.dispose_platform_engine()

    listed_tenants, denied_output = asyncio.run(exercise())
    assert listed_tenants == (TENANT_A,)
    assert denied_output == TENANT_B
    return BoundaryObservation(
        outcome="403",
        visible_output=repr((listed_tenants, denied_output)),
        forbidden_sentinels=(
            TENANT_B_NAME_SENTINEL,
            tenant_schema_name(TENANT_B),
            STORAGE_B_SECRET_REF_SENTINEL,
            STORAGE_B_FINGERPRINT_SENTINEL,
        ),
    )


def _exercise_audit_log(harness: IsolationHarness) -> BoundaryObservation:
    _install_database_settings(harness)

    async def platform_snapshot() -> tuple[
        tuple[str, str],
        tuple[tuple[str, str, str, str | None], ...],
    ]:
        async with database_module.platform_session() as session:
            state = (
                await session.execute(
                    select(Tenant.status, TenantProvisioningJob.status)
                    .join(
                        TenantProvisioningJob,
                        TenantProvisioningJob.tenant_id == Tenant.id,
                    )
                    .where(
                        Tenant.id == TENANT_A,
                        TenantProvisioningJob.id == JOB_A,
                    )
                )
            ).one()
            audits = (
                await session.execute(
                    select(
                        AuditLog.tenant_id,
                        AuditLog.action,
                        AuditLog.resource_type,
                        AuditLog.resource_id,
                    )
                    .where(AuditLog.tenant_id.in_((TENANT_A, TENANT_B)))
                    .order_by(AuditLog.id)
                )
            ).all()
            return tuple(state), tuple(tuple(row) for row in audits)

    async def set_tenant_status(status: str) -> None:
        async with database_module.platform_session() as session:
            async with session.begin():
                await session.execute(
                    update(Tenant).where(Tenant.id.in_((TENANT_A, TENANT_B))).values(status=status)
                )

    async def exercise() -> tuple[bool, bool, tuple[str, str]]:
        await database_module.dispose_platform_engine()
        try:
            await set_tenant_status("provisioning")
            before_state, before_audits = await platform_snapshot()
            assert before_state == ("provisioning", "pending")
            assert before_audits == (
                (
                    TENANT_B,
                    AUDIT_B_ACTION_SENTINEL,
                    "private-record",
                    AUDIT_B_RESOURCE_SENTINEL,
                ),
                (
                    TENANT_B,
                    POLICY_VERIFIED_ACTION,
                    PROVISIONING_JOB_RESOURCE,
                    f"{JOB_A}:0",
                ),
            )

            # Audit has no user-facing read surface in Plan01. Its production
            # tenant identity is the provisioning worker's exact tenant/job
            # attempt, exercised here through the real service and repository.
            service = TenantProvisioningService(TenantRepository())
            cross_tenant_read = await service.complete_if_ready(
                TENANT_A,
                JOB_A,
                0,
            )
            cross_tenant_write = await service.record_policy_verified(
                TENANT_A,
                JOB_B_SENTINEL,
                0,
            )

            after_state, after_audits = await platform_snapshot()
            assert cross_tenant_read is False
            assert cross_tenant_write is False
            assert after_state == before_state
            assert after_audits == before_audits
            return cross_tenant_read, cross_tenant_write, after_state
        finally:
            await set_tenant_status("active")
            await database_module.dispose_platform_engine()

    cross_tenant_read, cross_tenant_write, after_state = asyncio.run(exercise())
    return BoundaryObservation(
        outcome="empty",
        visible_output=repr(
            (
                cross_tenant_read,
                cross_tenant_write,
                after_state,
            )
        ),
        forbidden_sentinels=(
            JOB_B_SENTINEL,
            AUDIT_B_ACTION_SENTINEL,
            AUDIT_B_RESOURCE_SENTINEL,
            POLICY_VERIFIED_ACTION,
            f"{JOB_A}:0",
        ),
    )


def test_exact_member_delete_cascades_only_its_tenant_grants(
    tenant_database: TenantDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr(tenant_database.url),
    )
    monkeypatch.setattr(database_module, "load_platform_settings", lambda: settings)
    user_id = "cleanup-member-shared-user"

    async def exercise() -> tuple[int, int, int, int]:
        engine = create_async_engine(tenant_database.url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    for tenant_id in (TENANT_A, TENANT_B):
                        session.add(
                            TenantMembership(
                                tenant_id=tenant_id,
                                user_id=user_id,
                                status="active",
                            )
                        )
                    await session.flush()
                    for tenant_id in (TENANT_A, TENANT_B):
                        session.add(
                            RoleGrantModel(
                                tenant_id=tenant_id,
                                user_id=user_id,
                                role="student",
                                scope_type="tenant",
                                scope_id=tenant_id,
                            )
                        )
            await TenantRepository().delete_member(TENANT_A, user_id)
            async with factory() as session:
                membership_counts = []
                grant_counts = []
                for tenant_id in (TENANT_A, TENANT_B):
                    membership_counts.append(
                        int(
                            await session.scalar(
                                select(func.count())
                                .select_from(TenantMembership)
                                .where(
                                    TenantMembership.tenant_id == tenant_id,
                                    TenantMembership.user_id == user_id,
                                )
                            )
                            or 0
                        )
                    )
                    grant_counts.append(
                        int(
                            await session.scalar(
                                select(func.count())
                                .select_from(RoleGrantModel)
                                .where(
                                    RoleGrantModel.tenant_id == tenant_id,
                                    RoleGrantModel.user_id == user_id,
                                )
                            )
                            or 0
                        )
                    )
                return (*membership_counts, *grant_counts)
        finally:
            await engine.dispose()

    assert asyncio.run(exercise()) == (0, 1, 0, 1)


ACCEPTANCE_MATRIX = (
    BoundaryCase(
        "database_schema",
        _exercise_database_schema,
        requires_database=True,
    ),
    BoundaryCase(
        "active_tenant_cookie",
        _exercise_active_tenant_cookie,
    ),
    BoundaryCase(
        "permission_scope",
        _exercise_permission_scope,
    ),
    BoundaryCase(
        "object_store_prefix",
        _exercise_object_store_prefix,
    ),
    BoundaryCase(
        "data_plane_route",
        _exercise_data_plane_route,
        requires_database=True,
    ),
    BoundaryCase(
        "audit_log",
        _exercise_audit_log,
        requires_database=True,
    ),
)


@pytest.mark.parametrize(
    "case",
    ACCEPTANCE_MATRIX,
    ids=lambda case: case.name,
)
def test_tenant_isolation_acceptance_matrix(
    case: BoundaryCase,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = request.getfixturevalue("tenant_database") if case.requires_database else None
    observation = case.exercise(
        IsolationHarness(
            database=database,
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
    )

    assert observation.outcome in {"403", "404", "empty"}
    for sentinel in observation.forbidden_sentinels:
        assert sentinel not in observation.visible_output
