"""Transactional tenant repository for immutable classroom publication."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deeptutor.teaching.models.classrooms import (
    ClassroomAsset,
    ClassroomVersion,
    Publication,
    transition,
)
from deeptutor.teaching.models.platform import AuditLog
from deeptutor.teaching.schema_names import tenant_schema_name

_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")
_IMMUTABLE_TRIGGER_MESSAGE = "immutable classroom record"


def _required(value: str, name: str, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _sha256(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in _LOWER_HEX_DIGITS for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class ClassroomDocumentReference:
    """Immutable object-store identity for one classroom document."""

    sha256: str
    media_manifest_sha256: str
    object_key: str

    def __post_init__(self) -> None:
        _sha256(self.sha256, "sha256")
        _sha256(self.media_manifest_sha256, "media_manifest_sha256")
        _required(self.object_key, "object_key", 512)


@dataclass(frozen=True, slots=True)
class PublishedClassroomVersion:
    """Inputs committed together when an approved classroom is published."""

    id: str
    classroom_id: str
    version_number: int
    generation_job_id: str
    document: ClassroomDocumentReference
    publication_id: str
    actor_id: str
    scope: str
    class_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.id, "id", 128)
        _required(self.classroom_id, "classroom_id", 128)
        _required(self.generation_job_id, "generation_job_id", 64)
        _required(self.publication_id, "publication_id", 128)
        _required(self.actor_id, "actor_id", 128)
        if self.version_number <= 0:
            raise ValueError("version_number must be positive")
        if self.scope not in {"private", "class", "tenant"}:
            raise ValueError("scope is invalid")
        if self.scope == "class":
            if self.class_id is None:
                raise ValueError("class scope requires class_id")
            _required(self.class_id, "class_id", 64)
        elif self.class_id is not None:
            raise ValueError("class_id is only valid for class scope")


class ImmutableVersionError(RuntimeError):
    """A caller attempted to mutate an immutable classroom version."""


class ClassroomVersionNotFoundError(LookupError):
    """The requested version does not exist in the active tenant."""


class ClassroomAssetNotFoundError(LookupError):
    """The requested classroom asset does not exist in the active tenant."""


def _is_immutable_trigger_error(exc: DBAPIError) -> bool:
    return _IMMUTABLE_TRIGGER_MESSAGE in str(exc.orig).lower()


class SqlAlchemyClassroomRepository:
    """Publish and protect classroom versions inside one tenant schema."""

    def __init__(self, engine: AsyncEngine, tenant_id: str) -> None:
        _required(tenant_id, "tenant_id", 64)
        translated = engine.execution_options(
            schema_translate_map={"tenant": tenant_schema_name(tenant_id)}
        )
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(
            translated,
            expire_on_commit=False,
        )

    async def insert_published_version(
        self,
        published: PublishedClassroomVersion,
    ) -> ClassroomVersion:
        """Atomically freeze a version, record publication/audit, and advance its asset."""

        async with self._session_factory() as session:
            async with session.begin():
                asset = await session.scalar(
                    select(ClassroomAsset)
                    .where(
                        ClassroomAsset.id == published.classroom_id,
                        ClassroomAsset.tenant_id == self._tenant_id,
                    )
                    .with_for_update()
                )
                if asset is None:
                    raise ClassroomAssetNotFoundError(published.classroom_id)

                next_state = transition(asset.lifecycle_state, "published")
                version = ClassroomVersion(
                    id=published.id,
                    tenant_id=self._tenant_id,
                    classroom_id=published.classroom_id,
                    version_number=published.version_number,
                    generation_job_id=published.generation_job_id,
                    document_sha256=published.document.sha256,
                    media_manifest_sha256=published.document.media_manifest_sha256,
                    document_object_key=published.document.object_key,
                )
                session.add(version)
                await session.flush()

                session.add(
                    Publication(
                        id=published.publication_id,
                        tenant_id=self._tenant_id,
                        classroom_id=published.classroom_id,
                        classroom_version_id=published.id,
                        actor_id=published.actor_id,
                        scope=published.scope,
                        class_id=published.class_id,
                    )
                )
                session.add(
                    AuditLog(
                        tenant_id=self._tenant_id,
                        actor_id=published.actor_id,
                        action="teaching.classroom.published",
                        resource_type="classroom_version",
                        resource_id=published.id,
                    )
                )
                asset.lifecycle_state = next_state
                asset.current_published_version_id = published.id
                asset.updated_at = func.now()
                await session.flush()
                return version

    async def replace_document(
        self,
        version_id: str,
        document: ClassroomDocumentReference,
    ) -> None:
        """Attempt a replacement while mapping the database immutability fence."""

        _required(version_id, "version_id", 128)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await session.execute(
                        update(ClassroomVersion)
                        .where(
                            ClassroomVersion.id == version_id,
                            ClassroomVersion.tenant_id == self._tenant_id,
                        )
                        .values(
                            document_sha256=document.sha256,
                            media_manifest_sha256=document.media_manifest_sha256,
                            document_object_key=document.object_key,
                        )
                    )
                    if result.rowcount != 1:
                        raise ClassroomVersionNotFoundError(version_id)
        except DBAPIError as exc:
            if _is_immutable_trigger_error(exc):
                raise ImmutableVersionError(version_id) from None
            raise

    async def delete_version(self, version_id: str) -> None:
        """Attempt deletion while mapping the database immutability fence."""

        _required(version_id, "version_id", 128)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await session.execute(
                        delete(ClassroomVersion).where(
                            ClassroomVersion.id == version_id,
                            ClassroomVersion.tenant_id == self._tenant_id,
                        )
                    )
                    if result.rowcount != 1:
                        raise ClassroomVersionNotFoundError(version_id)
        except DBAPIError as exc:
            if _is_immutable_trigger_error(exc):
                raise ImmutableVersionError(version_id) from None
            raise


__all__ = [
    "ClassroomAssetNotFoundError",
    "ClassroomDocumentReference",
    "ClassroomVersionNotFoundError",
    "ImmutableVersionError",
    "PublishedClassroomVersion",
    "SqlAlchemyClassroomRepository",
]
