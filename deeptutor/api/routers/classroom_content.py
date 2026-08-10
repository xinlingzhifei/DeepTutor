"""Authenticated yFeiSTAI classroom document and media delivery."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.database import get_platform_engine
from deeptutor.teaching.services.classroom_content import (
    ClassroomContentAccessDenied,
    ClassroomContentIntegrityError,
    ClassroomContentNotFound,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant
from deeptutor.teaching.tickets import (
    ClassroomTicketService,
    TicketExpired,
    TicketInvalid,
    TicketScopeError,
)

router = APIRouter()

ReadAction = Literal[
    "classroom.document.read",
    "classroom.media.read",
    "classroom.export.read",
]


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadTicketRequest(_ApiModel):
    action: ReadAction
    resource_id: str = Field(min_length=1, max_length=128)


class ReadTicketResponse(_ApiModel):
    ticket: str
    expires_in: int


class ClassroomContentServiceLike(Protocol):
    async def issue_read_ticket(
        self,
        context: TenantContext,
        *,
        session_id: str,
        action: ReadAction,
        resource_id: str,
        ttl_seconds: int = 60,
    ) -> str: ...

    async def open_document(
        self,
        context: TenantContext,
        *,
        version_id: str,
        token: str | None,
    ): ...

    async def open_media(
        self,
        context: TenantContext,
        *,
        version_id: str,
        media_id: str,
        token: str | None,
    ): ...


def _build_classroom_content_service(settings, *, ticket_service):
    from deeptutor.teaching.processes import RuntimeStoreProvider
    from deeptutor.teaching.services.classroom_content import (
        ClassroomContentService,
        SqlAlchemyClassroomContentRepository,
    )

    return ClassroomContentService(
        repository=SqlAlchemyClassroomContentRepository(engine=get_platform_engine()),
        stores=RuntimeStoreProvider(settings),
        ticket_service=ticket_service,
    )


def get_classroom_content_reader_service():
    """Build teacher content reads without loading the classroom ticket secret."""

    return _build_classroom_content_service(
        load_platform_settings(),
        ticket_service=None,
    )


def get_classroom_content_service():
    settings = load_platform_settings()
    return _build_classroom_content_service(
        settings,
        ticket_service=ClassroomTicketService.from_settings(settings),
    )


def get_classroom_content_service_factory():
    """Defer the ticket-backed service until a ticketed read is requested."""

    return get_classroom_content_service


async def _call(operation):
    try:
        return await operation
    except TicketExpired:
        raise HTTPException(status_code=401, detail="Classroom ticket expired") from None
    except TicketScopeError:
        raise HTTPException(status_code=403, detail="Classroom ticket scope denied") from None
    except TicketInvalid:
        raise HTTPException(status_code=401, detail="Classroom ticket invalid") from None
    except ClassroomContentAccessDenied:
        raise HTTPException(status_code=403, detail="Classroom content access denied") from None
    except ClassroomContentNotFound:
        raise HTTPException(status_code=404, detail="Classroom content not found") from None
    except ClassroomContentIntegrityError:
        raise HTTPException(
            status_code=503,
            detail="Classroom content is unavailable",
        ) from None


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(self, resource, *, headers: dict[str, str]) -> None:
        self._resource = resource
        super().__init__(
            resource.iter_chunks(),
            media_type=resource.mime_type,
            headers=headers,
        )

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._resource.close()


def classroom_content_response(
    resource,
    *,
    attachment_filename: str | None = None,
) -> StreamingResponse:
    headers = {
        "Content-Length": str(resource.size_bytes),
        "ETag": f'"sha256-{resource.sha256}"',
        "Cache-Control": "private, no-store",
    }
    if attachment_filename is not None:
        filename = quote(attachment_filename, safe="")
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename}"
    return _ClosingStreamingResponse(resource, headers=headers)


@router.post(
    "/classroom-sessions/{session_id}/read-ticket",
    response_model=ReadTicketResponse,
)
async def issue_classroom_read_ticket(
    session_id: str,
    request: ReadTicketRequest,
    context: TenantContext = Depends(require_tenant),
    service: ClassroomContentServiceLike = Depends(get_classroom_content_service),
) -> ReadTicketResponse:
    token = await _call(
        service.issue_read_ticket(
            context,
            session_id=session_id,
            action=request.action,
            resource_id=request.resource_id,
            ttl_seconds=60,
        )
    )
    return ReadTicketResponse(ticket=token, expires_in=60)


@router.get("/classroom-versions/{version_id}/document")
async def get_classroom_version_document(
    version_id: str,
    ticket: Annotated[
        str | None,
        Header(alias="X-Classroom-Ticket", min_length=1, max_length=8192),
    ] = None,
    context: TenantContext = Depends(require_tenant),
    reader_service: ClassroomContentServiceLike = Depends(get_classroom_content_reader_service),
    ticket_service_factory=Depends(get_classroom_content_service_factory),
):
    service = reader_service if ticket is None else ticket_service_factory()
    return classroom_content_response(
        await _call(
            service.open_document(
                context,
                version_id=version_id,
                token=ticket,
            )
        )
    )


@router.get("/classroom-versions/{version_id}/media/{media_id}")
async def get_classroom_version_media(
    version_id: str,
    media_id: str,
    ticket: Annotated[
        str | None,
        Header(alias="X-Classroom-Ticket", min_length=1, max_length=8192),
    ] = None,
    context: TenantContext = Depends(require_tenant),
    reader_service: ClassroomContentServiceLike = Depends(get_classroom_content_reader_service),
    ticket_service_factory=Depends(get_classroom_content_service_factory),
):
    service = reader_service if ticket is None else ticket_service_factory()
    return classroom_content_response(
        await _call(
            service.open_media(
                context,
                version_id=version_id,
                media_id=media_id,
                token=ticket,
            )
        )
    )


__all__ = [
    "classroom_content_response",
    "get_classroom_content_reader_service",
    "get_classroom_content_service",
    "get_classroom_content_service_factory",
    "router",
]
