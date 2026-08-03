"""Classroom review, immutable publication, assignment, and migration API."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from deeptutor.teaching.services.publications import (
    ActiveLearningConflict,
    PublicationAccessDenied,
    PublicationConflict,
    PublicationError,
    PublicationNotFound,
    PublicationPersistenceError,
    PublicationService,
)
from deeptutor.teaching.services.reviews import (
    ReviewAccessDenied,
    ReviewBlocked,
    ReviewConflict,
    ReviewError,
    ReviewNotFound,
    ReviewPersistenceError,
    ReviewService,
    ReviewValidationStale,
)
from deeptutor.teaching.tenant_context import TenantContext, require_tenant

router = APIRouter()


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        populate_by_name=True,
    )


class SubmitReviewRequest(_ApiModel):
    scope: Literal["class", "tenant", "platform"]
    class_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.scope == "class") != (self.class_id is not None):
            raise ValueError("class publication scope requires classId")
        return self


class ReviewDecisionRequest(_ApiModel):
    comment: str = Field(min_length=1, max_length=4000)


class PublishRequest(_ApiModel):
    scope: Literal["class", "tenant", "platform"]
    class_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.scope == "class") != (self.class_id is not None):
            raise ValueError("class publication scope requires classId")
        return self


class AssignRequest(_ApiModel):
    class_id: str = Field(min_length=1, max_length=64)


class MigrateRequest(_ApiModel):
    old_version_id: str = Field(min_length=1, max_length=128)
    new_version_id: str = Field(min_length=1, max_length=128)
    class_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=4000)


class ReviewResponse(_ApiModel):
    id: str
    asset_id: str
    draft_id: str
    draft_revision: int
    document_sha256: str
    validation_report_sha256: str
    submitted_by: str
    scope: str
    class_id: str | None
    status: str
    warnings: list[dict[str, object]]
    reviewer_id: str | None
    comment: str | None


class ReviewListResponse(_ApiModel):
    items: list[ReviewResponse]


class PublishedVersionResponse(_ApiModel):
    version_id: str
    asset_id: str
    version_number: int
    document_sha256: str
    publication_scope: str
    class_id: str | None
    idempotency_key: str


class AssignmentResponse(_ApiModel):
    assignment_id: str
    asset_id: str
    version_id: str
    class_id: str
    assigned_by: str
    idempotency_key: str
    revoked_at: object | None


class MigrationResponse(_ApiModel):
    migration_id: str
    old_assignment_id: str
    old_version_id: str
    new_version_id: str
    new_assignment_id: str | None
    class_id: str
    actor_id: str
    reason: str
    outcome: str
    idempotency_key: str


class ReviewServiceLike(Protocol):
    async def submit(self, context, asset_id, *, scope, class_id, idempotency_key): ...
    async def list(self, context): ...
    async def approve(self, context, review_id, comment): ...
    async def reject(self, context, review_id, comment): ...


class PublicationServiceLike(Protocol):
    async def publish(self, context, asset_id, *, scope, class_id, idempotency_key): ...
    async def assign(self, context, version_id, *, class_id, idempotency_key): ...
    async def migrate(
        self,
        context,
        assignment_id,
        *,
        old_version_id,
        new_version_id,
        class_id,
        reason,
        idempotency_key,
    ): ...


def get_review_service(
    context: TenantContext = Depends(require_tenant),
) -> ReviewService:
    from deeptutor.teaching.database import get_platform_engine
    from deeptutor.teaching.services.review_repository import (
        SqlAlchemyReviewRepository,
    )

    return ReviewService(
        SqlAlchemyReviewRepository(get_platform_engine(), context.tenant_id)
    )


def get_publication_service(
    context: TenantContext = Depends(require_tenant),
) -> PublicationService:
    from deeptutor.teaching.database import get_platform_engine
    from deeptutor.teaching.services.publication_repository import (
        SqlAlchemyPublicationRepository,
    )

    return PublicationService(
        SqlAlchemyPublicationRepository(get_platform_engine(), context.tenant_id)
    )


def _idempotency_header() -> Header:
    return Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )


async def _call(operation):
    try:
        return await operation
    except ReviewValidationStale:
        raise HTTPException(status_code=409, detail="Classroom validation is stale") from None
    except ReviewBlocked:
        raise HTTPException(status_code=409, detail="Classroom validation has blockers") from None
    except ReviewAccessDenied:
        raise HTTPException(status_code=403, detail="Classroom review access denied") from None
    except ReviewNotFound:
        raise HTTPException(status_code=404, detail="Classroom review not found") from None
    except ReviewConflict:
        raise HTTPException(status_code=409, detail="Classroom review conflicts") from None
    except ActiveLearningConflict as exc:
        detail = (
            "Class has active learning sessions"
            if "active learning" in str(exc)
            else "Class learning-state guard is unavailable"
        )
        raise HTTPException(status_code=409, detail=detail) from None
    except PublicationAccessDenied:
        raise HTTPException(status_code=403, detail="Classroom publication access denied") from None
    except PublicationNotFound:
        raise HTTPException(status_code=404, detail="Classroom publication not found") from None
    except PublicationConflict:
        raise HTTPException(status_code=409, detail="Classroom publication conflicts") from None
    except (ReviewPersistenceError, PublicationPersistenceError, SQLAlchemyError):
        raise HTTPException(status_code=503, detail="Classroom workflow is unavailable") from None
    except (ReviewError, PublicationError):
        raise HTTPException(status_code=422, detail="Classroom workflow request is invalid") from None


@router.post("/classrooms/{asset_id}/submit", response_model=ReviewResponse, status_code=201)
async def submit_classroom(
    asset_id: str,
    request: SubmitReviewRequest,
    idempotency_key: Annotated[str, _idempotency_header()],
    context: TenantContext = Depends(require_tenant),
    service: ReviewServiceLike = Depends(get_review_service),
) -> ReviewResponse:
    return ReviewResponse.model_validate(
        await _call(
            service.submit(
                context,
                asset_id,
                scope=request.scope,
                class_id=request.class_id,
                idempotency_key=idempotency_key,
            )
        ),
        from_attributes=True,
    )


@router.get("/classroom-reviews", response_model=ReviewListResponse)
async def list_classroom_reviews(
    context: TenantContext = Depends(require_tenant),
    service: ReviewServiceLike = Depends(get_review_service),
) -> ReviewListResponse:
    records = await _call(service.list(context))
    return ReviewListResponse(
        items=[ReviewResponse.model_validate(item, from_attributes=True) for item in records]
    )


async def _decision(
    review_id: str,
    request: ReviewDecisionRequest,
    context: TenantContext,
    service: ReviewServiceLike,
    decision: Literal["approve", "reject"],
) -> ReviewResponse:
    operation = getattr(service, decision)(context, review_id, request.comment)
    return ReviewResponse.model_validate(await _call(operation), from_attributes=True)


@router.post("/classroom-reviews/{review_id}/approve", response_model=ReviewResponse)
async def approve_classroom_review(
    review_id: str,
    request: ReviewDecisionRequest,
    context: TenantContext = Depends(require_tenant),
    service: ReviewServiceLike = Depends(get_review_service),
) -> ReviewResponse:
    return await _decision(review_id, request, context, service, "approve")


@router.post("/classroom-reviews/{review_id}/reject", response_model=ReviewResponse)
async def reject_classroom_review(
    review_id: str,
    request: ReviewDecisionRequest,
    context: TenantContext = Depends(require_tenant),
    service: ReviewServiceLike = Depends(get_review_service),
) -> ReviewResponse:
    return await _decision(review_id, request, context, service, "reject")


@router.post("/classrooms/{asset_id}/publish", response_model=PublishedVersionResponse, status_code=201)
async def publish_classroom(
    asset_id: str,
    request: PublishRequest,
    idempotency_key: Annotated[str, _idempotency_header()],
    context: TenantContext = Depends(require_tenant),
    service: PublicationServiceLike = Depends(get_publication_service),
) -> PublishedVersionResponse:
    return PublishedVersionResponse.model_validate(
        await _call(
            service.publish(
                context,
                asset_id,
                scope=request.scope,
                class_id=request.class_id,
                idempotency_key=idempotency_key,
            )
        ),
        from_attributes=True,
    )


@router.post("/classroom-versions/{version_id}/assign", response_model=AssignmentResponse, status_code=201)
async def assign_classroom_version(
    version_id: str,
    request: AssignRequest,
    idempotency_key: Annotated[str, _idempotency_header()],
    context: TenantContext = Depends(require_tenant),
    service: PublicationServiceLike = Depends(get_publication_service),
) -> AssignmentResponse:
    return AssignmentResponse.model_validate(
        await _call(
            service.assign(
                context,
                version_id,
                class_id=request.class_id,
                idempotency_key=idempotency_key,
            )
        ),
        from_attributes=True,
    )


@router.post("/classroom-assignments/{assignment_id}/migrate", response_model=MigrationResponse)
async def migrate_classroom_assignment(
    assignment_id: str,
    request: MigrateRequest,
    idempotency_key: Annotated[str, _idempotency_header()],
    context: TenantContext = Depends(require_tenant),
    service: PublicationServiceLike = Depends(get_publication_service),
) -> MigrationResponse:
    return MigrationResponse.model_validate(
        await _call(
            service.migrate(
                context,
                assignment_id,
                old_version_id=request.old_version_id,
                new_version_id=request.new_version_id,
                class_id=request.class_id,
                reason=request.reason,
                idempotency_key=idempotency_key,
            )
        ),
        from_attributes=True,
    )


__all__ = [
    "get_publication_service",
    "get_review_service",
    "router",
]
