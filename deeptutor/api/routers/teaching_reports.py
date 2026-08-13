"""Tenant-scoped classroom learning reports."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.services.reports import (
    LearningReportMetrics,
    QuarantinedLearningEvent,
    TeachingReportAccessDenied,
    TeachingReportNotFound,
    TeachingReportService,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant

router = APIRouter()


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, extra="forbid")


class MasteryResponse(_ApiModel):
    knowledge_point_id: str
    level: float = Field(ge=0, le=1)
    evidence_count: int = Field(ge=0)


class ReportMetricsResponse(_ApiModel):
    session_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    completed_scene_count: int = Field(ge=0)
    valid_quiz_count: int = Field(ge=0)
    correct_quiz_count: int = Field(ge=0)
    hint_count: int = Field(ge=0)
    pbl_milestone_count: int = Field(ge=0)
    mastery: list[MasteryResponse]
    projection_lag_seconds: float = Field(ge=0)


class ClassReportResponse(ReportMetricsResponse):
    class_id: str


class StudentReportResponse(ReportMetricsResponse):
    class_id: str
    user_id: str


class ClassroomReportResponse(ReportMetricsResponse):
    classroom_version_id: str


class QuarantineItemResponse(_ApiModel):
    event_id: str
    event_type: str
    classroom_version_id: str
    reason_code: str
    quarantined_at: datetime | str
    knowledge_point_id: str | None = None


class QuarantineListResponse(_ApiModel):
    items: list[QuarantineItemResponse]


def get_teaching_report_service() -> TeachingReportService:
    from deeptutor.teaching.services.reports import SqlAlchemyTeachingReportRepository

    return TeachingReportService(SqlAlchemyTeachingReportRepository(get_platform_engine()))


async def _result(operation):
    try:
        return await operation
    except TeachingReportAccessDenied as exc:
        raise HTTPException(status_code=403, detail="Learning report access denied") from exc
    except TeachingReportNotFound as exc:
        raise HTTPException(status_code=404, detail="Learning report not found") from exc


def _metrics(model: LearningReportMetrics) -> dict[str, object]:
    return {
        "session_count": model.session_count,
        "completed_count": model.completed_count,
        "completion_rate": model.completion_rate,
        "completed_scene_count": model.completed_scene_count,
        "valid_quiz_count": model.valid_quiz_count,
        "correct_quiz_count": model.correct_quiz_count,
        "hint_count": model.hint_count,
        "pbl_milestone_count": model.pbl_milestone_count,
        "mastery": list(model.mastery),
        "projection_lag_seconds": model.projection_lag_seconds,
    }


@router.get("/classes/{class_id}", response_model=ClassReportResponse)
async def class_report(
    class_id: str,
    context: Annotated[TenantContext, Depends(require_tenant)],
    service: Annotated[TeachingReportService, Depends(get_teaching_report_service)],
) -> ClassReportResponse:
    model = await _result(service.class_report(context, class_id))
    return ClassReportResponse(class_id=class_id, **_metrics(model))


@router.get(
    "/classes/{class_id}/students/{user_id}",
    response_model=StudentReportResponse,
)
async def student_report(
    class_id: str,
    user_id: str,
    context: Annotated[TenantContext, Depends(require_tenant)],
    service: Annotated[TeachingReportService, Depends(get_teaching_report_service)],
) -> StudentReportResponse:
    model = await _result(service.class_report(context, class_id, user_id=user_id))
    return StudentReportResponse(class_id=class_id, user_id=user_id, **_metrics(model))


@router.get("/classrooms/{version_id}", response_model=ClassroomReportResponse)
async def classroom_report(
    version_id: str,
    context: Annotated[TenantContext, Depends(require_tenant)],
    service: Annotated[TeachingReportService, Depends(get_teaching_report_service)],
) -> ClassroomReportResponse:
    model = await _result(service.classroom_report(context, version_id))
    return ClassroomReportResponse(classroom_version_id=version_id, **_metrics(model))


@router.get("/quarantine", response_model=QuarantineListResponse)
async def quarantine_report(
    context: Annotated[TenantContext, Depends(require_tenant)],
    service: Annotated[TeachingReportService, Depends(get_teaching_report_service)],
) -> QuarantineListResponse:
    rows: tuple[QuarantinedLearningEvent, ...] = await _result(service.quarantine(context))
    return QuarantineListResponse(
        items=[QuarantineItemResponse.model_validate(row, from_attributes=True) for row in rows]
    )


__all__ = ["get_teaching_report_service", "router"]
