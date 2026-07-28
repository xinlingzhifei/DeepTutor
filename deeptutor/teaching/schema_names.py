"""Safe derivation of tenant PostgreSQL schema names."""

from __future__ import annotations

import hashlib


def tenant_schema_name(tenant_id: str) -> str:
    """Return a deterministic identifier without embedding the tenant ID."""

    normalized_tenant_id = tenant_id.strip()
    if not normalized_tenant_id:
        raise ValueError("tenant_id is required")
    digest = hashlib.sha256(normalized_tenant_id.encode("utf-8")).hexdigest()[:16]
    return f"tenant_{digest}"
