"""Export-specific helpers shared by the durable generation worker."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from deeptutor.teaching.contracts import ExportRequest
from deeptutor.teaching.openmaic.client import EngineJob


@dataclass(frozen=True, slots=True)
class ExportInputSnapshot:
    """Hashes pinned by the immutable export job request payload."""

    classroom_document_sha256: str
    media_manifest_sha256: str
    request_sha256: str

    @classmethod
    def from_canonical_payload(cls, payload: str) -> ExportInputSnapshot:
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("export request payload is invalid") from None
        if not isinstance(raw, dict):
            raise ValueError("export request payload is invalid")
        canonical = json.dumps(
            raw,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical != payload:
            raise ValueError("export request payload must be canonical JSON")
        request = ExportRequest.model_validate(raw)
        return cls(
            classroom_document_sha256=request.classroom_document_sha256,
            media_manifest_sha256=request.media_manifest_sha256,
            request_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        )


class ExportSubmissionClient(Protocol):
    async def submit_export(self, request: ExportRequest) -> EngineJob: ...


async def submit_pinned_export(
    client: ExportSubmissionClient,
    request: ExportRequest,
) -> EngineJob:
    """Keep the export endpoint explicit at the worker boundary."""

    return await client.submit_export(request)


__all__ = ["ExportInputSnapshot", "submit_pinned_export"]
