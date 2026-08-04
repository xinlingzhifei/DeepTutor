"""Immutable classroom publication, assignment, and migration policy."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Literal, Protocol

from deeptutor.teaching.contracts import ClassroomDocument, canonical_json_bytes
from deeptutor.teaching.permissions import ResourceScope
from deeptutor.teaching.services.reviews import ReviewPolicy
from deeptutor.teaching.tenant_context import TenantContext

PublicationScope = Literal["class", "tenant", "platform"]


class PublicationError(RuntimeError):
    """Base class for fixed-safe publication failures."""


class PublicationAccessDenied(PublicationError, PermissionError):
    """The actor cannot publish, assign, or migrate the resource."""


class PublicationNotFound(PublicationError, LookupError):
    """The resource is not visible in the active tenant."""


class PublicationConflict(PublicationError):
    """The resource state or idempotency binding conflicts."""


class PublicationValidationStale(PublicationConflict):
    """The reviewed validation binding no longer matches the draft."""


class PublicationPersistenceError(PublicationError):
    """Publication persistence is unavailable or inconsistent."""


class ActiveLearningConflict(PublicationConflict):
    """Assignment migration is blocked by the learning-state guard."""


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    tenant_id: str
    asset_id: str
    owner_id: str
    course_id: str
    class_id: str
    review_id: str
    review_scope: PublicationScope
    review_status: Literal["pending", "approved", "rejected"]
    submitted_by: str
    draft_revision: int
    document_sha256: str


@dataclass(frozen=True, slots=True)
class PublishedVersionRecord:
    version_id: str
    asset_id: str
    version_number: int
    document_sha256: str
    publication_scope: PublicationScope
    class_id: str | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PublicationMediaSource:
    media_id: str
    relative_name: str
    mime_type: str
    sha256: str
    size_bytes: int
    source_kind: Literal["draft_upload", "version_artifact"]
    object_key: str = field(repr=False)
    ownership_token: str | None = field(default=None, repr=False)
    object_revision: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.source_kind == "draft_upload":
            if self.ownership_token is None or self.object_revision is None:
                raise ValueError("draft upload publication source is incomplete")
        elif self.source_kind == "version_artifact":
            if self.ownership_token is not None or self.object_revision is not None:
                raise ValueError("version artifact publication source has mutable ownership")
        else:
            raise ValueError("publication source kind is unsupported")


@dataclass(frozen=True, slots=True)
class PublicationMaterializationPlan:
    reservation_id: str
    tenant_id: str
    asset_id: str
    review_id: str
    draft_id: str
    draft_revision: int
    source_version_id: str
    version_id: str
    version_number: int
    document: bytes = field(repr=False)
    document_sha256: str
    validation_report_sha256: str
    media_manifest_sha256: str
    manifest_sha256: str
    media: tuple[PublicationMediaSource, ...]
    status: Literal["prepared", "object_committed", "finalized"]


@dataclass(frozen=True, slots=True)
class MaterializedPublicationArtifact:
    relative_name: str
    object_key: str = field(repr=False)
    sha256: str
    size_bytes: int
    mime_type: str
    artifact_kind: Literal["dsl_json", "media"]
    media_id: str | None


@dataclass(frozen=True, slots=True)
class ConfirmedPublicationMaterialization:
    manifest_sha256: str
    media_manifest_sha256: str
    artifacts: tuple[MaterializedPublicationArtifact, ...]

    @property
    def document(self) -> MaterializedPublicationArtifact:
        matches = tuple(
            artifact for artifact in self.artifacts if artifact.artifact_kind == "dsl_json"
        )
        if len(matches) != 1:
            raise PublicationPersistenceError("confirmed publication document is invalid")
        return matches[0]


def validated_publication_document(document: bytes) -> ClassroomDocument:
    try:
        parsed = ClassroomDocument.model_validate_json(document)
    except Exception:
        raise PublicationPersistenceError("reviewed draft document is invalid") from None
    if canonical_json_bytes(parsed) != document:
        raise PublicationPersistenceError("reviewed draft document is not canonical")
    raw = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    file_sha256 = raw.pop("fileSha256")
    if hashlib.sha256(canonical_json_bytes(raw)).hexdigest() != file_sha256:
        raise PublicationPersistenceError("reviewed draft document file hash is invalid")
    return parsed


def publication_media_manifest_sha256(document: bytes) -> str:
    parsed = validated_publication_document(document)
    payload = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    return hashlib.sha256(canonical_json_bytes(payload["mediaManifest"])).hexdigest()


class PublicationMaterializer(Protocol):
    async def materialize(
        self,
        plan: PublicationMaterializationPlan,
    ) -> ConfirmedPublicationMaterialization: ...


@dataclass(frozen=True, slots=True)
class VersionTarget:
    tenant_id: str
    version_id: str
    asset_id: str
    course_id: str
    publication_scope: PublicationScope
    publication_class_id: str | None


@dataclass(frozen=True, slots=True)
class AssignmentRecord:
    assignment_id: str
    tenant_id: str
    asset_id: str
    version_id: str
    class_id: str
    assigned_by: str
    idempotency_key: str
    revoked_at: object | None


@dataclass(frozen=True, slots=True)
class AssignmentTarget:
    tenant_id: str
    assignment_id: str
    asset_id: str
    version_id: str
    course_id: str
    class_id: str
    revoked_at: object | None


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    migration_id: str
    tenant_id: str
    old_assignment_id: str
    old_version_id: str
    new_version_id: str
    new_assignment_id: str | None
    class_id: str
    actor_id: str
    reason: str
    outcome: Literal[
        "succeeded",
        "refused_active_learning",
        "refused_guard_unavailable",
    ]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PublishCommand:
    tenant_id: str
    asset_id: str
    actor_id: str
    scope: PublicationScope
    class_id: str | None
    idempotency_key: str
    allow_self_publish: bool
    review_id: str
    draft_revision: int
    document_sha256: str


@dataclass(frozen=True, slots=True)
class AssignCommand:
    tenant_id: str
    asset_id: str
    version_id: str
    class_id: str
    actor_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MigrateAssignmentCommand:
    tenant_id: str
    assignment_id: str
    old_version_id: str
    new_version_id: str
    class_id: str
    actor_id: str
    reason: str
    idempotency_key: str


class PublicationRepository(Protocol):
    async def get_policy(self) -> ReviewPolicy: ...

    async def get_publication_target(
        self,
        asset_id: str,
    ) -> PublicationTarget | None: ...

    async def publish(
        self,
        command: PublishCommand,
        materializer: PublicationMaterializer,
    ) -> PublishedVersionRecord: ...

    async def get_version_target(self, version_id: str) -> VersionTarget | None: ...

    async def assign(self, command: AssignCommand) -> AssignmentRecord: ...

    async def get_assignment_target(
        self,
        assignment_id: str,
    ) -> AssignmentTarget | None: ...

    async def get_migration(
        self,
        idempotency_key: str,
    ) -> MigrationRecord | None: ...

    async def migrate(self, command: MigrateAssignmentCommand) -> MigrationRecord: ...


def _allows(
    context: TenantContext,
    permission: str,
    *,
    course_id: str,
    class_id: str,
) -> bool:
    resource = ResourceScope(
        tenant_id=context.tenant_id,
        course_id=course_id,
        class_id=class_id,
    )
    return any(grant.allows_resource(permission, resource) for grant in context.permissions)


class PublicationService:
    """Authorize immutable publication and explicit assignment movement."""

    def __init__(
        self,
        repository: PublicationRepository,
        materializer: PublicationMaterializer | None = None,
    ) -> None:
        self._repository = repository
        self._materializer = materializer

    async def publish(
        self,
        context: TenantContext,
        asset_id: str,
        *,
        scope: PublicationScope,
        class_id: str | None,
        idempotency_key: str,
    ) -> PublishedVersionRecord:
        target = await self._repository.get_publication_target(asset_id)
        if target is None or target.tenant_id != context.tenant_id:
            raise PublicationNotFound("classroom publication target was not found")
        if not _allows(
            context,
            "classroom.publish",
            course_id=target.course_id,
            class_id=target.class_id,
        ):
            raise PublicationAccessDenied("classroom publication is denied")
        if scope == "class":
            if class_id != target.class_id:
                raise PublicationAccessDenied("class publication scope is denied")
        elif class_id is not None:
            raise PublicationConflict("class_id is only valid for class publication")
        if target.review_scope != scope:
            raise PublicationConflict("publication scope conflicts with review")

        policy = await self._repository.get_policy()
        approved = target.review_status == "approved"
        self_publish = (
            scope == "class"
            and policy.teacher_self_publish
            and target.owner_id == context.user_id
            and target.submitted_by == context.user_id
            and target.class_id == class_id
        )
        if scope == "platform":
            if not _allows(
                context,
                "template.manage",
                course_id=target.course_id,
                class_id=target.class_id,
            ):
                raise PublicationAccessDenied("platform publication is denied")
            if policy.platform_template_requires_review and not approved:
                raise PublicationAccessDenied("platform publication requires approval")
        elif scope == "tenant":
            if policy.org_content_requires_review and not approved:
                raise PublicationAccessDenied("organization publication requires approval")
        elif not approved and not self_publish:
            raise PublicationAccessDenied("class publication requires approval")

        if self._materializer is None:
            raise PublicationPersistenceError("classroom publication materializer is unavailable")
        return await self._repository.publish(
            PublishCommand(
                tenant_id=context.tenant_id,
                asset_id=asset_id,
                actor_id=context.user_id,
                scope=scope,
                class_id=class_id,
                idempotency_key=idempotency_key,
                allow_self_publish=self_publish,
                review_id=target.review_id,
                draft_revision=target.draft_revision,
                document_sha256=target.document_sha256,
            ),
            self._materializer,
        )

    async def assign(
        self,
        context: TenantContext,
        version_id: str,
        *,
        class_id: str,
        idempotency_key: str,
    ) -> AssignmentRecord:
        target = await self._repository.get_version_target(version_id)
        if target is None or target.tenant_id != context.tenant_id:
            raise PublicationNotFound("classroom version was not found")
        if target.publication_scope == "private":
            raise PublicationAccessDenied("private classroom cannot be assigned")
        if target.publication_scope == "class" and target.publication_class_id != class_id:
            raise PublicationAccessDenied("classroom version is not published to class")
        if not _allows(
            context,
            "classroom.assign",
            course_id=target.course_id,
            class_id=class_id,
        ):
            raise PublicationAccessDenied("classroom assignment is denied")
        return await self._repository.assign(
            AssignCommand(
                tenant_id=context.tenant_id,
                asset_id=target.asset_id,
                version_id=version_id,
                class_id=class_id,
                actor_id=context.user_id,
                idempotency_key=idempotency_key,
            )
        )

    async def migrate(
        self,
        context: TenantContext,
        assignment_id: str,
        *,
        old_version_id: str,
        new_version_id: str,
        class_id: str,
        reason: str,
        idempotency_key: str,
    ) -> MigrationRecord:
        existing = await self._repository.get_migration(idempotency_key)
        if existing is not None:
            if (
                existing.tenant_id != context.tenant_id
                or existing.old_assignment_id != assignment_id
                or existing.old_version_id != old_version_id
                or existing.new_version_id != new_version_id
                or existing.class_id != class_id
                or existing.actor_id != context.user_id
                or existing.reason != reason.strip()
            ):
                raise PublicationConflict("migration idempotency key conflicts")
            if existing.outcome == "refused_active_learning":
                raise ActiveLearningConflict("class has active learning sessions")
            if existing.outcome == "refused_guard_unavailable":
                raise ActiveLearningConflict("class learning-state guard is unavailable")
            return existing
        old = await self._repository.get_assignment_target(assignment_id)
        if old is None or old.tenant_id != context.tenant_id:
            raise PublicationNotFound("classroom assignment was not found")
        if (
            old.version_id != old_version_id
            or old.class_id != class_id
            or old.revoked_at is not None
        ):
            raise PublicationConflict("classroom assignment binding conflicts")
        target = await self._repository.get_version_target(new_version_id)
        if (
            target is None
            or target.tenant_id != context.tenant_id
            or target.asset_id != old.asset_id
        ):
            raise PublicationAccessDenied("migration target is not accessible")
        if target.publication_scope == "private" or (
            target.publication_scope == "class" and target.publication_class_id != class_id
        ):
            raise PublicationAccessDenied("migration target is not published to class")
        if not _allows(
            context,
            "classroom.assign",
            course_id=old.course_id,
            class_id=class_id,
        ):
            raise PublicationAccessDenied("classroom migration is denied")
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 4000:
            raise PublicationConflict("migration reason is invalid")
        record = await self._repository.migrate(
            MigrateAssignmentCommand(
                tenant_id=context.tenant_id,
                assignment_id=assignment_id,
                old_version_id=old_version_id,
                new_version_id=new_version_id,
                class_id=class_id,
                actor_id=context.user_id,
                reason=normalized_reason,
                idempotency_key=idempotency_key,
            )
        )
        if record.outcome == "refused_active_learning":
            raise ActiveLearningConflict("class has active learning sessions")
        if record.outcome == "refused_guard_unavailable":
            raise ActiveLearningConflict("class learning-state guard is unavailable")
        return record


__all__ = [
    "ActiveLearningConflict",
    "AssignCommand",
    "AssignmentRecord",
    "AssignmentTarget",
    "ConfirmedPublicationMaterialization",
    "MaterializedPublicationArtifact",
    "MigrateAssignmentCommand",
    "MigrationRecord",
    "PublicationAccessDenied",
    "PublicationConflict",
    "PublicationError",
    "PublicationMaterializationPlan",
    "PublicationMaterializer",
    "PublicationMediaSource",
    "PublicationNotFound",
    "PublicationPersistenceError",
    "PublicationService",
    "PublicationTarget",
    "PublicationValidationStale",
    "PublishCommand",
    "PublishedVersionRecord",
    "VersionTarget",
    "publication_media_manifest_sha256",
]
