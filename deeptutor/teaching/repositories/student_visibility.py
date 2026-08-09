"""SQL boundary that keeps student classroom assets out of teacher workflows."""

from __future__ import annotations

from sqlalchemy import and_, exists, select

from deeptutor.teaching.models.student_generation import StudentClassroomAssetRecord


def teacher_asset_visible(asset_id, tenant_id):
    return ~exists(
        select(StudentClassroomAssetRecord.asset_id).where(
            and_(
                StudentClassroomAssetRecord.asset_id == asset_id,
                StudentClassroomAssetRecord.tenant_id == tenant_id,
            )
        )
    )


__all__ = ["teacher_asset_visible"]
