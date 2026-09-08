"""Local companion contract, never a platform archive member or deployment proof.

All expected identities and engine inputs must come from the caller's trusted
channel, not from the receipts being checked. This joins validated snapshots;
it does not authenticate Actions, recheck a registry, or observe a deployment.
This helper returns metadata only. Persistence belongs to the established
release publication transaction; no filesystem publication API is provided.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re

if __package__:
    from . import ragflow_engine_release as engine
    from . import render_platform_compose as platform
else:
    import ragflow_engine_release as engine
    import render_platform_compose as platform

canonical_bytes = engine.canonical_bytes


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


@contextmanager
def _binding_lease(
    *,
    candidate_root: Path,
    expected_source_repository: str,
    expected_source_head: str,
    expected_release_tag: str,
    expected_platform_run_id: str,
    expected_platform_run_attempt: int,
    deployment_batch_id: str,
    engine_receipt: bytes,
    engine_expected_inputs: Mapping,
):
    if (
        not isinstance(deployment_batch_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", deployment_batch_id) is None
    ):
        raise ValueError("deployment batch ID must be a safe explicit identifier")
    if not isinstance(engine_receipt, bytes) or not engine_receipt:
        raise ValueError("engine receipt bytes are required")
    if not isinstance(engine_expected_inputs, Mapping):
        raise ValueError("trusted engine expected inputs are required")
    engine_inputs = dict(engine_expected_inputs)
    identity = {
        "sourceRepository": expected_source_repository,
        "sourceHead": expected_source_head,
        "releaseTag": expected_release_tag,
    }
    expected_engine_identity = {
        "source_repository": expected_source_repository,
        "source_head": expected_source_head,
        "release_tag": expected_release_tag,
    }
    if any(engine_inputs.get(key) != value for key, value in expected_engine_identity.items()):
        raise ValueError("trusted engine identity differs from the explicit platform candidate")

    root = Path(candidate_root)
    intent_path = root / "artifacts/promotion_intent.json"
    receipt_path = root / "artifacts/promotion_receipt.json"
    with (
        platform._candidate_artifact_descriptor_lease(
            root,
            (intent_path, receipt_path),
            max_bytes={
                platform._path_key(path): platform.MAX_PROMOTION_DOCUMENT_BYTES
                for path in (intent_path, receipt_path)
            },
        ) as receipts,
        platform.validated_candidate_artifact_lease(root, expected_candidate=identity) as candidate,
    ):
        receipt = platform.validate_promotion_receipt(
            Path(candidate_root),
            expected_github_run_id=expected_platform_run_id,
            expected_github_run_attempt=expected_platform_run_attempt,
        )
        validated_engine = engine.validate_receipt(engine_receipt, **engine_inputs)
        receipt_body = receipts.contents[platform._path_key(receipt_path)]
        if receipt_body != canonical_bytes(receipt):
            raise ValueError("promotion receipt changed from the original validated snapshot")
        document = {
            "schemaVersion": 1,
            "kind": "ragflow-platform-companion-binding",
            "validationScope": "local-contract-only",
            "deploymentBatchId": deployment_batch_id,
            **identity,
            "platform": {
                "imageLockSha256": _digest(candidate.image_lock_bytes),
                "promotionReceiptSha256": _digest(receipt_body),
                "runId": receipt["githubRunId"],
                "runAttempt": receipt["githubRunAttempt"],
            },
            "engine": {
                "receiptSha256": _digest(engine_receipt),
                "image": validated_engine["image"],
                "runId": validated_engine["ci"]["runId"],
                "runAttempt": validated_engine["ci"]["runAttempt"],
            },
        }
        receipts.check_unchanged()
        candidate.check_unchanged()
        yield document
        # The enclosing public lease revalidates its original handles on exit.


def create_binding(**expected_inputs) -> dict:
    """Return a snapshot binding after both contracts and the source lease pass."""
    with _binding_lease(**expected_inputs) as document:
        result = document
    return result


def validate_binding(body: bytes, **expected_inputs) -> dict:
    """Reject altered/noncanonical bindings, including replay into another batch."""
    with _binding_lease(**expected_inputs) as document:
        if not isinstance(body, bytes) or body != canonical_bytes(document):
            raise ValueError("companion binding is noncanonical or differs from expected inputs")
    return document


def binding_bytes(**expected_inputs) -> bytes:
    """Serialize inside both original input leases; return only after final checks."""
    with _binding_lease(**expected_inputs) as document:
        body = canonical_bytes(document)
    return body
