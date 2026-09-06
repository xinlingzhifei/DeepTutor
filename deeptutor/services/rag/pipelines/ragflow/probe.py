"""Read-only connection probe; health is not proof of authentication or version."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .client import RagflowClient, RagflowError
from .config import RagflowConfig


async def probe_server(
    config: RagflowConfig,
    client_factory: Callable[[RagflowConfig], RagflowClient] | None = None,
) -> dict[str, Any]:
    client = (client_factory or RagflowClient)(config)
    result: dict[str, Any] = {
        "ok": False,
        "health": "unknown",
        "auth": "unknown",
        "contract": "unknown",
        "version_unknown": True,
        "contract_version": "0.27.1",
        "checks": {},
    }
    try:
        health = await client.health()
        result["health"] = (
            "healthy" if all(value == "ok" for value in health.values()) else "unhealthy"
        )
        result["checks"]["health"] = health
    except RagflowError as exc:
        result["checks"]["health"] = {"error_type": exc.error_type}
        if exc.error_type == "provider_incompatible":
            result["contract"] = "incompatible"
    try:
        await client.list_datasets(page=1, page_size=1)
        result["auth"] = "valid"
        if result["contract"] != "incompatible":
            result["contract"] = "compatible"
    except RagflowError as exc:
        result["checks"]["datasets"] = {"error_type": exc.error_type}
        if exc.error_type == "auth_error":
            result["auth"] = "invalid"
        elif exc.error_type == "provider_incompatible":
            result["contract"] = "incompatible"
    result["ok"] = (
        result["health"] == "healthy"
        and result["auth"] == "valid"
        and result["contract"] == "compatible"
    )
    return result
