"""Fail-closed budget for lease renewal that does not follow the camera scan.

The camera scan may take longer than the lease. Renewal has its own page size
and a configured assignment ceiling. The time to visit that ceiling must be
strictly shorter than the lease. Fence grace is not added: grace is only the
failover overlap, and using it here would let a healthy owner reach expiry.
The lease duration is never reduced to make a failing budget pass.
"""

from __future__ import annotations

import math

# The placement controller has always slept at least this long. The budget
# uses the same floor so it cannot assume a faster loop than the process runs.
MINIMUM_PLACEMENT_INTERVAL_SECONDS = 2.0


class PlacementRenewalBudgetError(ValueError):
    """Renewal cannot visit every configured owner before the lease expires.

    Raised when settings are loaded and when a placement run sees a population
    above the configured ceiling. Callers must not shorten the lease to silence it.
    """


def resolved_placement_interval_seconds(configured: float) -> float:
    """Return the interval the controller actually sleeps.

    Args:
        configured: PLACEMENT_INTERVAL_SECONDS as supplied by settings.

    Returns:
        The configured interval, or the historical 2 second floor when the
        configured value is lower. The floor is not a lease reduction.
    """
    return max(MINIMUM_PLACEMENT_INTERVAL_SECONDS, float(configured))


def _seconds_text(value: float) -> str:
    if float(value) == int(value):
        return str(int(value))
    return str(value)


def assert_placement_renewal_budget(
    *,
    max_assignments: int,
    batch_size: int,
    interval_seconds: float,
    lease_seconds: int,
) -> float:
    """Reject a renewal cadence that cannot visit every owner before lease expiry.

    The cycle is ceil(max_assignments / batch_size) multiplied by the interval
    the controller sleeps. One visit grants a full lease, so the next visit to
    that same owner must happen strictly before that lease expires.

    Args:
        max_assignments: Configured ceiling of active assignments renewal must cover.
        batch_size: Assignments renewed on one controller run.
        interval_seconds: Configured controller interval, before the 2 second floor.
        lease_seconds: Full placement lease. This function does not change it.

    Returns:
        The renewal cycle length in seconds when the budget holds.

    Raises:
        PlacementRenewalBudgetError: When an input is not positive or the cycle
            is greater than or equal to the lease.
    """
    if lease_seconds <= 0:
        raise PlacementRenewalBudgetError(
            "placement renewal budget rejected: placement_lease_seconds must be > 0; "
            "the lease is not shortened"
        )
    if max_assignments <= 0 or batch_size <= 0:
        raise PlacementRenewalBudgetError(
            "placement renewal budget rejected: "
            "placement_renewal_max_assignments and placement_renewal_batch_size must be > 0"
        )
    interval = resolved_placement_interval_seconds(interval_seconds)
    pages = math.ceil(max_assignments / batch_size)
    cycle = pages * interval
    if cycle >= lease_seconds:
        raise PlacementRenewalBudgetError(
            "placement renewal budget exceeded: "
            f"cycle {_seconds_text(cycle)}s >= lease {lease_seconds}s "
            f"(max_assignments={max_assignments}, batch_size={batch_size}, "
            f"interval_seconds={_seconds_text(interval)}). "
            "Raise the renewal batch or lower the interval; do not shorten the lease."
        )
    return cycle
