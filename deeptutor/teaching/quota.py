"""Quota ledger arithmetic shared by generation job repositories."""

from __future__ import annotations


class InsufficientQuota(RuntimeError):
    """The tenant does not have enough granted units for this reservation."""


def reserve_quota(*, balance: int, requested_units: int) -> int:
    """Return the post-reservation balance or fail without partial mutation."""

    if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
        raise ValueError("balance must be a non-negative integer")
    if (
        isinstance(requested_units, bool)
        or not isinstance(requested_units, int)
        or requested_units <= 0
    ):
        raise ValueError("requested_units must be a positive integer")
    if requested_units > balance:
        raise InsufficientQuota("tenant quota is insufficient")
    return balance - requested_units
