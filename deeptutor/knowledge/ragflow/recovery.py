"""Restart recovery uses recorded actors and bindings, with no default-admin path."""

from __future__ import annotations

import asyncio
import logging

from .binding import activate_managed_binding, restore_activation_binding, restore_binding
from .lifecycle import WORKER_ID, execute_operation, reconcile_operation
from .operations import OperationConflict, OperationScope, OperationStore

logger = logging.getLogger(__name__)


async def recover_once(*, store: OperationStore | None = None) -> dict:
    store = store or OperationStore()
    counts = {"examined": 0, "reconciled": 0, "needs_attention": 0}
    for pending in store.pending_operations():
        counts["examined"] += 1
        scope = OperationScope(**pending["scope"])
        try:
            bound = restore_binding(pending["payload"].get("binding_snapshot", {}))
            if bound.scope != scope:
                raise ValueError("Recorded scope is stale")
        except Exception:
            # Rejecting recovery never invents a principal or accesses a remote target.
            claimed = store.claim(scope, pending["operation_id"], WORKER_ID)
            if claimed:
                try:
                    store.transition(
                        scope,
                        claimed["operation_id"],
                        worker_id=WORKER_ID,
                        expected_revision=claimed["revision"],
                        state="NEEDS_ATTENTION",
                        error_type="recovery_authorization_unavailable",
                    )
                    counts["needs_attention"] += 1
                except OperationConflict:
                    pass
            continue
        try:
            # Only unsent, persisted intents may initiate a request. Unknown POSTs
            # always go through read-only reconciliation.
            method = (
                execute_operation if pending["state"] == "INTENT_RECORDED" else reconcile_operation
            )
            result = await method(bound, pending["operation_id"])
            if result and result["state"] == "RECONCILED":
                counts["reconciled"] += 1
                if result["action"] == "create_dataset" and not bound.binding.get("dataset_id"):
                    activate_managed_binding(bound, result["result"]["dataset_id"])
                    store.mark_binding_activated(scope, result["operation_id"])
        except Exception:
            # The persisted lease/receipt remains the recovery evidence.
            logger.warning(
                "RAGFlow recovery requires another pass for operation %s", pending["operation_id"]
            )
    # A crash can occur on either side of local activation after the remote
    # create receipt was committed. This path checks the recorded target, never POSTs.
    for operation in store.pending_binding_activations():
        scope = OperationScope(**operation["scope"])
        try:
            dataset_id = operation["result"]["dataset_id"]
            bound = restore_activation_binding(operation["payload"]["binding_snapshot"], dataset_id)
            if not bound.binding.get("dataset_id"):
                activate_managed_binding(bound, dataset_id)
            store.mark_binding_activated(scope, operation["operation_id"])
        except Exception:
            logger.warning(
                "RAGFlow binding activation requires attention for operation %s",
                operation["operation_id"],
            )
    return counts


async def run_recovery(stop_event: asyncio.Event, *, interval: float = 5) -> None:
    while not stop_event.is_set():
        try:
            await recover_once()
        except Exception:
            logger.warning("RAGFlow operation recovery pass is unavailable")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
