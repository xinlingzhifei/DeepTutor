from __future__ import annotations

import inspect

import pytest

from deeptutor.teaching.services.learning_sessions import (
    LearningSessionAuthorityError,
    LearningSessionService,
)
from deeptutor.teaching.tenant_context import TenantContext


def _context() -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_a",
        user_id="student-a",
        permissions=frozenset(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assignment_id", "student_asset_id"),
    [(None, None), ("assignment-a", "asset-a")],
)
async def test_create_session_requires_exactly_one_server_authority_reference(
    assignment_id: str | None,
    student_asset_id: str | None,
) -> None:
    service = LearningSessionService(engine=object(), ticket_service=object())

    with pytest.raises(LearningSessionAuthorityError):
        await service.create(
            _context(),
            assignment_id=assignment_id,
            student_asset_id=student_asset_id,
        )


def test_create_session_api_has_no_client_tenant_user_or_version_authority() -> None:
    parameters = inspect.signature(LearningSessionService.create).parameters

    assert "tenant_id" not in parameters
    assert "user_id" not in parameters
    assert "classroom_version_id" not in parameters
    assert {"context", "assignment_id", "student_asset_id"}.issubset(parameters)
