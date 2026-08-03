"""Shared PostgreSQL allocation fence for immutable classroom versions."""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deeptutor.teaching.models.classrooms import (
    ClassroomPublicationMaterialization,
    ClassroomVersion,
)
from deeptutor.teaching.models.jobs import ArtifactPromotionState

_VERSION_NUMBER_CONSTRAINT = "uq_classroom_versions_tenant_classroom_version"


class ClassroomVersionAllocationError(RuntimeError):
    """A stale writer collided with an allocated classroom version number."""


async def allocate_classroom_version_number(
    session: AsyncSession,
    *,
    tenant_id: str,
    classroom_id: str,
) -> int:
    """Lock one classroom allocation stream and return its next unreserved number."""

    classroom_key = hashlib.sha256(
        f"classroom-promotion\0{tenant_id}\0{classroom_id}".encode()
    ).hexdigest()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:classroom_key, 0))"),
        {"classroom_key": classroom_key},
    )
    persisted_max = int(
        await session.scalar(
            select(func.coalesce(func.max(ClassroomVersion.version_number), 0)).where(
                ClassroomVersion.tenant_id == tenant_id,
                ClassroomVersion.classroom_id == classroom_id,
            )
        )
        or 0
    )
    reserved_max = int(
        await session.scalar(
            select(
                func.coalesce(func.max(ArtifactPromotionState.version_number), 0)
            ).where(
                ArtifactPromotionState.tenant_id == tenant_id,
                ArtifactPromotionState.classroom_id == classroom_id,
            )
        )
        or 0
    )
    publication_max = int(
        await session.scalar(
            select(
                func.coalesce(
                    func.max(ClassroomPublicationMaterialization.version_number),
                    0,
                )
            ).where(
                ClassroomPublicationMaterialization.tenant_id == tenant_id,
                ClassroomPublicationMaterialization.classroom_id == classroom_id,
            )
        )
        or 0
    )
    return max(persisted_max, reserved_max, publication_max) + 1


def raise_for_classroom_version_allocation_conflict(exc: IntegrityError) -> None:
    """Map the database uniqueness fence to the classroom allocation domain."""

    if _VERSION_NUMBER_CONSTRAINT in str(exc.orig):
        raise ClassroomVersionAllocationError(
            "classroom version allocation is stale"
        ) from None


__all__ = [
    "ClassroomVersionAllocationError",
    "allocate_classroom_version_number",
    "raise_for_classroom_version_allocation_conflict",
]
