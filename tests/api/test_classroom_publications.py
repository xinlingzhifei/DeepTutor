"""Immutable classroom publication, assignment, and migration acceptance tests."""

from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import classroom_reviews
from deeptutor.teaching.permissions import ScopedPermission
from deeptutor.teaching.services.publications import (
    ActiveLearningConflict,
    AssignmentRecord,
    AssignmentTarget,
    MigrationRecord,
    PublicationAccessDenied,
    PublicationConflict,
    PublicationService,
    PublicationTarget,
    PublishedVersionRecord,
    VersionTarget,
)
from deeptutor.teaching.services.reviews import ReviewPolicy
from deeptutor.teaching.tenant_context import TenantContext


def _context(
    user_id: str,
    *permissions: tuple[str, str, str],
    tenant_id: str = "tenant-a",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        schema_name=f"tenant_{tenant_id}",
        user_id=user_id,
        permissions=frozenset(
            ScopedPermission(
                permission=permission,
                scope_type=scope_type,
                scope_id=scope_id,
                tenant_id=tenant_id,
            )
            for permission, scope_type, scope_id in permissions
        ),
    )


class _PublicationRepository:
    def __init__(self, *, self_publish: bool = False) -> None:
        self.policy = ReviewPolicy(teacher_self_publish=self_publish)
        self.target = PublicationTarget(
            tenant_id="tenant-a",
            asset_id="asset-1",
            owner_id="teacher-1",
            course_id="course-a",
            class_id="class-a",
            review_id="review-1",
            review_scope="class",
            review_status="pending",
            submitted_by="teacher-1",
            draft_revision=4,
            document_sha256="a" * 64,
        )
        self.versions: list[PublishedVersionRecord] = []
        self.assignments: dict[str, AssignmentRecord] = {}
        self.version_targets: dict[str, VersionTarget] = {}
        self.active_learning = False
        self.guard_known = True
        self.migrations: dict[str, MigrationRecord] = {}

    async def get_policy(self) -> ReviewPolicy:
        return self.policy

    async def get_publication_target(self, asset_id: str) -> PublicationTarget | None:
        return self.target if asset_id == self.target.asset_id else None

    async def publish(self, command) -> PublishedVersionRecord:
        existing = next(
            (item for item in self.versions if item.idempotency_key == command.idempotency_key),
            None,
        )
        if existing is not None:
            return existing
        version = PublishedVersionRecord(
            version_id=f"version-{len(self.versions) + 1}",
            asset_id=command.asset_id,
            version_number=len(self.versions) + 1,
            document_sha256=self.target.document_sha256,
            publication_scope=command.scope,
            class_id=command.class_id,
            idempotency_key=command.idempotency_key,
        )
        self.versions.append(version)
        self.version_targets[version.version_id] = VersionTarget(
            tenant_id="tenant-a",
            version_id=version.version_id,
            asset_id=command.asset_id,
            course_id="course-a",
            publication_scope=command.scope,
            publication_class_id=command.class_id,
        )
        return version

    async def get_version_target(self, version_id: str) -> VersionTarget | None:
        return self.version_targets.get(version_id)

    async def assign(self, command) -> AssignmentRecord:
        existing = next(
            (
                item
                for item in self.assignments.values()
                if item.idempotency_key == command.idempotency_key
            ),
            None,
        )
        if existing is not None:
            return existing
        for item in self.assignments.values():
            if (
                item.class_id == command.class_id
                and item.asset_id == command.asset_id
                and item.revoked_at is None
                and item.version_id != command.version_id
            ):
                raise PublicationConflict("explicit migration is required")
        record = AssignmentRecord(
            assignment_id=f"assignment-{len(self.assignments) + 1}",
            tenant_id="tenant-a",
            asset_id=command.asset_id,
            version_id=command.version_id,
            class_id=command.class_id,
            assigned_by=command.actor_id,
            idempotency_key=command.idempotency_key,
            revoked_at=None,
        )
        self.assignments[record.assignment_id] = record
        return record

    async def get_assignment_target(self, assignment_id: str) -> AssignmentTarget | None:
        assignment = self.assignments.get(assignment_id)
        if assignment is None:
            return None
        return AssignmentTarget(
            tenant_id=assignment.tenant_id,
            assignment_id=assignment.assignment_id,
            asset_id=assignment.asset_id,
            version_id=assignment.version_id,
            course_id="course-a",
            class_id=assignment.class_id,
            revoked_at=assignment.revoked_at,
        )

    async def get_migration(self, idempotency_key: str) -> MigrationRecord | None:
        return self.migrations.get(idempotency_key)

    async def migrate(self, command) -> MigrationRecord:
        existing = self.migrations.get(command.idempotency_key)
        if existing is not None:
            return existing
        outcome = "succeeded"
        new_assignment_id = None
        if not self.guard_known:
            outcome = "refused_guard_unavailable"
        elif self.active_learning:
            outcome = "refused_active_learning"
        else:
            old = self.assignments[command.assignment_id]
            new_assignment_id = f"assignment-{len(self.assignments) + 1}"
            self.assignments[old.assignment_id] = replace(old, revoked_at="now")
            self.assignments[new_assignment_id] = AssignmentRecord(
                assignment_id=new_assignment_id,
                tenant_id=old.tenant_id,
                asset_id=old.asset_id,
                version_id=command.new_version_id,
                class_id=old.class_id,
                assigned_by=command.actor_id,
                idempotency_key=f"migration:{command.idempotency_key}",
                revoked_at=None,
            )
        record = MigrationRecord(
            migration_id=f"migration-{len(self.migrations) + 1}",
            tenant_id="tenant-a",
            old_assignment_id=command.assignment_id,
            old_version_id=command.old_version_id,
            new_version_id=command.new_version_id,
            new_assignment_id=new_assignment_id,
            class_id=command.class_id,
            actor_id=command.actor_id,
            reason=command.reason,
            outcome=outcome,
            idempotency_key=command.idempotency_key,
        )
        self.migrations[command.idempotency_key] = record
        return record


def _teacher() -> TenantContext:
    return _context(
        "teacher-1",
        ("classroom.publish", "class", "class-a"),
        ("classroom.assign", "class", "class-a"),
    )


@pytest.mark.asyncio
async def test_teacher_self_publish_requires_explicit_policy_and_own_class() -> None:
    disabled = PublicationService(_PublicationRepository(self_publish=False))
    with pytest.raises(PublicationAccessDenied):
        await disabled.publish(
            _teacher(),
            "asset-1",
            scope="class",
            class_id="class-a",
            idempotency_key="publish-key-1",
        )

    enabled_repository = _PublicationRepository(self_publish=True)
    enabled = PublicationService(enabled_repository)
    published = await enabled.publish(
        _teacher(),
        "asset-1",
        scope="class",
        class_id="class-a",
        idempotency_key="publish-key-1",
    )
    assert published.version_number == 1

    with pytest.raises(PublicationAccessDenied):
        await enabled.publish(
            _teacher(),
            "asset-1",
            scope="class",
            class_id="class-b",
            idempotency_key="publish-key-2",
        )


@pytest.mark.asyncio
async def test_org_and_platform_publication_require_approved_review() -> None:
    repository = _PublicationRepository()
    repository.target = replace(
        repository.target,
        review_scope="tenant",
        review_status="pending",
    )
    service = PublicationService(repository)
    publisher = _context(
        "publisher-1",
        ("classroom.publish", "tenant", "tenant-a"),
    )
    with pytest.raises(PublicationAccessDenied):
        await service.publish(
            publisher,
            "asset-1",
            scope="tenant",
            class_id=None,
            idempotency_key="publish-key-1",
        )

    repository.target = replace(repository.target, review_status="approved")
    assert (
        await service.publish(
            publisher,
            "asset-1",
            scope="tenant",
            class_id=None,
            idempotency_key="publish-key-1",
        )
    ).publication_scope == "tenant"

    repository.target = replace(
        repository.target,
        review_scope="platform",
        review_status="approved",
    )
    with pytest.raises(PublicationAccessDenied):
        await service.publish(
            publisher,
            "asset-1",
            scope="platform",
            class_id=None,
            idempotency_key="publish-key-2",
        )


@pytest.mark.asyncio
async def test_existing_assignment_stays_on_old_version_after_new_publish() -> None:
    repository = _PublicationRepository(self_publish=True)
    service = PublicationService(repository)
    old = await service.publish(
        _teacher(),
        "asset-1",
        scope="class",
        class_id="class-a",
        idempotency_key="publish-key-1",
    )
    assignment = await service.assign(
        _teacher(),
        old.version_id,
        class_id="class-a",
        idempotency_key="assign-key-1",
    )

    repository.target = replace(repository.target, document_sha256="c" * 64)
    new = await service.publish(
        _teacher(),
        "asset-1",
        scope="class",
        class_id="class-a",
        idempotency_key="publish-key-2",
    )

    assert new.version_id != old.version_id
    assert repository.assignments[assignment.assignment_id].version_id == old.version_id


@pytest.mark.asyncio
async def test_assignment_is_idempotent_and_cross_class_is_denied() -> None:
    repository = _PublicationRepository(self_publish=True)
    service = PublicationService(repository)
    version = await service.publish(
        _teacher(),
        "asset-1",
        scope="class",
        class_id="class-a",
        idempotency_key="publish-key-1",
    )
    first = await service.assign(
        _teacher(),
        version.version_id,
        class_id="class-a",
        idempotency_key="assign-key-1",
    )
    retried = await service.assign(
        _teacher(),
        version.version_id,
        class_id="class-a",
        idempotency_key="assign-key-1",
    )
    assert retried == first

    with pytest.raises(PublicationAccessDenied):
        await service.assign(
            _teacher(),
            version.version_id,
            class_id="class-b",
            idempotency_key="assign-key-2",
        )


@pytest.mark.asyncio
async def test_explicit_migration_is_audited_idempotent_and_refuses_active_learning() -> None:
    repository = _PublicationRepository(self_publish=True)
    service = PublicationService(repository)
    old = await service.publish(
        _teacher(),
        "asset-1",
        scope="class",
        class_id="class-a",
        idempotency_key="publish-key-1",
    )
    assignment = await service.assign(
        _teacher(),
        old.version_id,
        class_id="class-a",
        idempotency_key="assign-key-1",
    )
    repository.target = replace(repository.target, document_sha256="c" * 64)
    new = await service.publish(
        _teacher(),
        "asset-1",
        scope="class",
        class_id="class-a",
        idempotency_key="publish-key-2",
    )

    repository.active_learning = True
    with pytest.raises(ActiveLearningConflict):
        await service.migrate(
            _teacher(),
            assignment.assignment_id,
            old_version_id=old.version_id,
            new_version_id=new.version_id,
            class_id="class-a",
            reason="use corrected lesson",
            idempotency_key="migration-key-1",
        )
    refused = repository.migrations["migration-key-1"]
    assert refused.reason == "use corrected lesson"
    assert refused.outcome == "refused_active_learning"
    assert repository.assignments[assignment.assignment_id].revoked_at is None

    repository.active_learning = False
    migrated = await service.migrate(
        _teacher(),
        assignment.assignment_id,
        old_version_id=old.version_id,
        new_version_id=new.version_id,
        class_id="class-a",
        reason="use corrected lesson",
        idempotency_key="migration-key-2",
    )
    retried = await service.migrate(
        _teacher(),
        assignment.assignment_id,
        old_version_id=old.version_id,
        new_version_id=new.version_id,
        class_id="class-a",
        reason="use corrected lesson",
        idempotency_key="migration-key-2",
    )
    assert migrated == retried
    assert migrated.outcome == "succeeded"
    assert repository.assignments[assignment.assignment_id].revoked_at == "now"


@pytest.mark.asyncio
async def test_migration_guard_missing_state_fails_closed() -> None:
    repository = _PublicationRepository(self_publish=True)
    repository.guard_known = False
    service = PublicationService(repository)
    old = await service.publish(
        _teacher(),
        "asset-1",
        scope="class",
        class_id="class-a",
        idempotency_key="publish-key-1",
    )
    assignment = await service.assign(
        _teacher(), old.version_id, class_id="class-a", idempotency_key="assign-key-1"
    )
    repository.target = replace(repository.target, document_sha256="c" * 64)
    new = await service.publish(
        _teacher(),
        "asset-1",
        scope="class",
        class_id="class-a",
        idempotency_key="publish-key-2",
    )
    with pytest.raises(ActiveLearningConflict, match="unavailable"):
        await service.migrate(
            _teacher(),
            assignment.assignment_id,
            old_version_id=old.version_id,
            new_version_id=new.version_id,
            class_id="class-a",
            reason="use corrected lesson",
            idempotency_key="migration-key-1",
        )


def _client(service: PublicationService, context: TenantContext) -> TestClient:
    app = FastAPI()
    app.include_router(classroom_reviews.router, prefix="/api/v1")
    app.dependency_overrides[classroom_reviews.require_tenant] = lambda: context
    app.dependency_overrides[classroom_reviews.get_publication_service] = lambda: service
    return TestClient(app)


def test_active_learning_migration_refusal_is_fixed_safe_api_error() -> None:
    repository = _PublicationRepository(self_publish=True)
    repository.active_learning = True
    service = PublicationService(repository)
    teacher = _teacher()

    async def prepare() -> tuple[PublishedVersionRecord, AssignmentRecord, PublishedVersionRecord]:
        old = await service.publish(
            teacher,
            "asset-1",
            scope="class",
            class_id="class-a",
            idempotency_key="publish-key-1",
        )
        assignment = await service.assign(
            teacher,
            old.version_id,
            class_id="class-a",
            idempotency_key="assign-key-1",
        )
        repository.target = replace(repository.target, document_sha256="c" * 64)
        new = await service.publish(
            teacher,
            "asset-1",
            scope="class",
            class_id="class-a",
            idempotency_key="publish-key-2",
        )
        return old, assignment, new

    import asyncio

    old, assignment, new = asyncio.run(prepare())
    response = _client(service, teacher).post(
        f"/api/v1/classroom-assignments/{assignment.assignment_id}/migrate",
        headers={"Idempotency-Key": "migration-key-1"},
        json={
            "oldVersionId": old.version_id,
            "newVersionId": new.version_id,
            "classId": "class-a",
            "reason": "use corrected lesson",
        },
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Class has active learning sessions"}
