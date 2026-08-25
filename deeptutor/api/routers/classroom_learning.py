"""Authenticated classroom learning-session and event-ingestion APIs."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from deeptutor.api.routers.classroom_content import get_classroom_content_service
from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.learning_events import LearningEventBatch
from deeptutor.teaching.repositories.learning_events import (
    LearningEventBindingError,
    LearningSessionUnavailable,
)
from deeptutor.teaching.services.learning_sessions import (
    LearningSessionAuthorityError,
    LearningSessionError,
    LearningSessionService,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant
from deeptutor.teaching.tickets import (
    ClassroomTicketService,
    TicketExpired,
    TicketInvalid,
    TicketReplay,
    TicketScopeError,
)

router = APIRouter()
MAX_EVENT_BATCH_BYTES = 256 * 1024


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LearningSessionCreateRequest(_ApiModel):
    assignment_id: str | None = Field(default=None, min_length=1, max_length=128)
    student_asset_id: str | None = Field(default=None, min_length=1, max_length=128)


class LearningSessionCursorRequest(_ApiModel):
    cursor: dict[str, JsonValue]


class LearningSessionResponse(_ApiModel):
    id: str
    tenant_id: str
    user_id: str
    classroom_version_id: str
    assignment_id: str | None
    student_asset_id: str | None
    status: str
    last_cursor: dict[str, JsonValue] | None
    started_at: datetime
    completed_at: datetime | None


class TicketResponse(_ApiModel):
    ticket: str
    expires_in: int


class EventAcceptedResponse(_ApiModel):
    event_id: str
    seq: int


class EventQuarantinedResponse(_ApiModel):
    event_id: str
    reason: str


class EventIngestionResponse(_ApiModel):
    accepted: list[EventAcceptedResponse]
    duplicate: list[EventAcceptedResponse]
    quarantined: list[EventQuarantinedResponse]


NonEmptyTrimmedString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


class PblGradingRequest(_ApiModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    event_id: str = Field(alias="eventId", min_length=1, max_length=128)
    passed: bool = Field(strict=True)
    score: float | None = Field(default=None, strict=True, ge=0, le=1)
    source_reference: NonEmptyTrimmedString = Field(alias="sourceReference")


class PblGradingResponse(_ApiModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    result_id: str = Field(alias="resultId")
    event_id: str = Field(alias="eventId")
    passed: bool
    score: float | None
    source_reference: str = Field(alias="sourceReference")
    grading_source: str = Field(alias="gradingSource")
    graded_at: datetime = Field(alias="gradedAt")


class LearningSessionServiceLike(Protocol):
    async def create(
        self,
        context: TenantContext,
        *,
        assignment_id: str | None = None,
        student_asset_id: str | None = None,
    ): ...

    async def get(self, context: TenantContext, *, session_id: str): ...

    async def update_cursor(
        self,
        context: TenantContext,
        *,
        session_id: str,
        cursor: dict[str, JsonValue],
    ): ...

    async def complete(self, context: TenantContext, *, session_id: str): ...

    async def issue_event_ticket(
        self,
        context: TenantContext,
        *,
        session_id: str,
        ttl_seconds: int = 300,
    ) -> str: ...


class LearningEventIngestionServiceLike(Protocol):
    async def ingest(
        self,
        context: TenantContext,
        *,
        session_id: str,
        token: str,
        batch: LearningEventBatch,
    ): ...


class PblGradingServiceLike(Protocol):
    async def record(self, context: TenantContext, *, session_id: str, command): ...


def get_learning_session_service() -> LearningSessionService:
    return LearningSessionService(
        engine=get_platform_engine(),
        ticket_service=ClassroomTicketService.from_settings(load_platform_settings()),
    )


def get_learning_event_ingestion_service(
    sessions: LearningSessionService = Depends(get_learning_session_service),
    document_loader=Depends(get_classroom_content_service),
):
    from deeptutor.teaching.services.classroom_learning import (
        ClassroomLearningEventIngestionService,
    )

    return ClassroomLearningEventIngestionService(
        engine=get_platform_engine(),
        sessions=sessions,
        document_loader=document_loader,
    )


def get_pbl_grading_service(
    document_loader=Depends(get_classroom_content_service),
):
    from deeptutor.teaching.repositories.pbl_grading import (
        SqlAlchemyPblGradingRepository,
    )
    from deeptutor.teaching.services.pbl_grading import PblGradingService

    return PblGradingService(
        SqlAlchemyPblGradingRepository(get_platform_engine()),
        document_loader,
    )


async def _call(operation: Awaitable[Any]):
    try:
        return await operation
    except TicketExpired:
        raise HTTPException(status_code=401, detail="Classroom ticket expired") from None
    except TicketScopeError:
        raise HTTPException(status_code=403, detail="Classroom ticket scope denied") from None
    except TicketReplay:
        raise HTTPException(status_code=409, detail="Classroom ticket already used") from None
    except TicketInvalid:
        raise HTTPException(status_code=401, detail="Classroom ticket invalid") from None
    except (LearningSessionAuthorityError, LearningEventBindingError):
        raise HTTPException(status_code=403, detail="Learning session access denied") from None
    except LearningSessionUnavailable:
        raise HTTPException(status_code=409, detail="Learning session is unavailable") from None
    except LearningSessionError:
        raise HTTPException(status_code=503, detail="Learning session is unavailable") from None


async def _call_pbl_grading(operation: Awaitable[Any]):
    from deeptutor.teaching.projectors.mastery import DeterministicProjectionError
    from deeptutor.teaching.services.classroom_content import ClassroomContentError
    from deeptutor.teaching.services.pbl_grading import (
        PblGradingAccessDenied,
        PblGradingConflict,
        PblGradingError,
        PblGradingValidationError,
    )

    try:
        return await operation
    except PblGradingAccessDenied:
        raise HTTPException(status_code=403, detail="PBL grading access denied") from None
    except PblGradingConflict:
        raise HTTPException(status_code=409, detail="PBL grading result conflicts") from None
    except (PblGradingValidationError, DeterministicProjectionError):
        raise HTTPException(status_code=422, detail="PBL grading request is invalid") from None
    except (PblGradingError, ClassroomContentError, SQLAlchemyError):
        raise HTTPException(status_code=503, detail="PBL grading is unavailable") from None


def _session_response(record: object) -> LearningSessionResponse:
    return LearningSessionResponse.model_validate(record, from_attributes=True)


@router.post(
    "/classroom-sessions",
    response_model=LearningSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_learning_session(
    request: LearningSessionCreateRequest,
    context: TenantContext = Depends(require_tenant),
    service: LearningSessionServiceLike = Depends(get_learning_session_service),
) -> LearningSessionResponse:
    return _session_response(
        await _call(
            service.create(
                context,
                assignment_id=request.assignment_id,
                student_asset_id=request.student_asset_id,
            )
        )
    )


@router.get("/classroom-sessions/{session_id}", response_model=LearningSessionResponse)
async def get_learning_session(
    session_id: str,
    context: TenantContext = Depends(require_tenant),
    service: LearningSessionServiceLike = Depends(get_learning_session_service),
) -> LearningSessionResponse:
    return _session_response(await _call(service.get(context, session_id=session_id)))


@router.post(
    "/classroom-sessions/{session_id}/event-ticket",
    response_model=TicketResponse,
)
async def issue_learning_event_ticket(
    session_id: str,
    context: TenantContext = Depends(require_tenant),
    service: LearningSessionServiceLike = Depends(get_learning_session_service),
) -> TicketResponse:
    ticket = await _call(
        service.issue_event_ticket(context, session_id=session_id, ttl_seconds=300)
    )
    return TicketResponse(ticket=ticket, expires_in=300)


@router.put("/classroom-sessions/{session_id}/cursor", response_model=LearningSessionResponse)
async def update_learning_session_cursor(
    session_id: str,
    request: LearningSessionCursorRequest,
    context: TenantContext = Depends(require_tenant),
    service: LearningSessionServiceLike = Depends(get_learning_session_service),
) -> LearningSessionResponse:
    return _session_response(
        await _call(
            service.update_cursor(
                context,
                session_id=session_id,
                cursor=request.cursor,
            )
        )
    )


@router.post("/classroom-sessions/{session_id}/complete", response_model=LearningSessionResponse)
async def complete_learning_session(
    session_id: str,
    context: TenantContext = Depends(require_tenant),
    service: LearningSessionServiceLike = Depends(get_learning_session_service),
) -> LearningSessionResponse:
    return _session_response(await _call(service.complete(context, session_id=session_id)))


@router.post(
    "/classroom-sessions/{session_id}/events",
    response_model=EventIngestionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def append_learning_events(
    session_id: str,
    request: Request,
    ticket: Annotated[
        str,
        Header(alias="X-Classroom-Ticket", min_length=1, max_length=8192),
    ],
    context: TenantContext = Depends(require_tenant),
    service: LearningEventIngestionServiceLike = Depends(get_learning_event_ingestion_service),
) -> EventIngestionResponse:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_EVENT_BATCH_BYTES:
            raise HTTPException(status_code=413, detail="Learning event batch is too large")
        body.extend(chunk)
    try:
        batch = LearningEventBatch.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_context=False, include_url=False),
        ) from None
    result = await _call(
        service.ingest(
            context,
            session_id=session_id,
            token=ticket,
            batch=batch,
        )
    )
    return EventIngestionResponse.model_validate(result, from_attributes=True)


@router.post(
    "/classroom-sessions/{session_id}/pbl-results",
    response_model=PblGradingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_pbl_grading_result(
    session_id: str,
    request: PblGradingRequest,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ],
    context: TenantContext = Depends(require_tenant),
    service: PblGradingServiceLike = Depends(get_pbl_grading_service),
) -> PblGradingResponse:
    from deeptutor.teaching.services.pbl_grading import PblGradingCommand

    result = await _call_pbl_grading(
        service.record(
            context,
            session_id=session_id,
            command=PblGradingCommand(
                event_id=request.event_id,
                passed=request.passed,
                score=request.score,
                source_reference=request.source_reference,
                idempotency_key=idempotency_key,
            ),
        )
    )
    return PblGradingResponse.model_validate(result, from_attributes=True)


__all__ = [
    "MAX_EVENT_BATCH_BYTES",
    "get_learning_event_ingestion_service",
    "get_learning_session_service",
    "get_pbl_grading_service",
    "router",
]
