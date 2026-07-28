"""Tenant provisioning workflow without infrastructure side effects."""

from __future__ import annotations

import hashlib
from typing import Protocol
import uuid

from fastapi import Depends

from deeptutor.teaching.repositories.tenants import (
    ProvisioningSummary,
    TenantRepository,
    get_tenant_repository,
)


class ProvisioningRepository(Protocol):
    async def create_provisioning(
        self,
        *,
        tenant_id: str,
        job_id: str,
        name: str,
    ) -> ProvisioningSummary: ...

    async def activate_if_ready(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool: ...

    async def mark_provisioning_failed(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool: ...

    async def record_policy_verified(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool: ...


def _stable_ids(actor_id: str, idempotency_key: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{actor_id}\0{idempotency_key}".encode("utf-8")).hexdigest()
    opaque = digest[:62]
    return f"t_{opaque}", f"j_{opaque}"


class TenantProvisioningService:
    """Create provisioning intent and gate its eventual atomic activation."""

    def __init__(self, repository: ProvisioningRepository) -> None:
        self._repository = repository

    async def create(
        self,
        *,
        actor_id: str,
        name: str,
        idempotency_key: str | None,
    ) -> ProvisioningSummary:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("tenant name is required")

        normalized_key = idempotency_key.strip() if idempotency_key else None
        if normalized_key:
            tenant_id, job_id = _stable_ids(actor_id, normalized_key)
        else:
            tenant_id = f"t_{uuid.uuid4().hex}"
            job_id = f"j_{uuid.uuid4().hex}"
        return await self._repository.create_provisioning(
            tenant_id=tenant_id,
            job_id=job_id,
            name=normalized_name,
        )

    async def complete_if_ready(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        """Delegate persisted prerequisite validation for one exact attempt."""

        return await self._repository.activate_if_ready(
            tenant_id,
            job_id,
            expected_attempt_count,
        )

    async def mark_failed(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        """Fail the current attempt without accepting or storing free text."""

        return await self._repository.mark_provisioning_failed(
            tenant_id,
            job_id,
            expected_attempt_count,
        )

    async def record_policy_verified(
        self,
        tenant_id: str,
        job_id: str,
        expected_attempt_count: int,
    ) -> bool:
        """Persist the fixed policy event for one exact current attempt."""

        return await self._repository.record_policy_verified(
            tenant_id,
            job_id,
            expected_attempt_count,
        )


def get_tenant_provisioning_service(
    repository: TenantRepository = Depends(get_tenant_repository),
) -> TenantProvisioningService:
    """FastAPI-replaceable provisioning service dependency."""

    return TenantProvisioningService(repository)
