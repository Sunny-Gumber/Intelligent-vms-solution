"""Fail-closed budget for lease renewal that does not follow the camera scan.

The node drops a cached lease at that lease's end. It learns a replacement
only on its next fence poll, and clock skew can make the cached deadline
arrive early. The granting run can acquire the lock immediately. Every later
cycle, including the revisit of the first page, can sleep its lock retries
before the attempt that acquires the lock. A fully missed cycle sleeps those
retries and then the interval, and it does not spend max_run. The revisit
spends its own max_run between the stamp and the commit, and the node then
waits a fence poll before it observes the new lease. There is no interval
after that commit in the observation. The budget therefore requires:

    pages * (retry_sleep + max_run + interval)
        + missed_lock_cycles * (retry_sleep + interval)
        + max_run
        + fence_poll
        + clock_skew
        + safety_margin
        < lease

pages is ceil(max_assignments / batch_size). missed_lock_cycles is at least 1.
retry_sleep is (attempts - 1) times the retry delay. The last attempt does not
sleep. The extra max_run is the revisit, which the pages term does not include.
Fence grace is not added to the lease and is not spent as extra budget: using
it would loosen the node side to make the arithmetic fit. The lease duration
is never reduced to force a pass.

Fence poll and clock skew default to the node-agent's own defaults (5s and
5s). This module does not change the node-agent. An operator who lengthens
the node poll or skew warning must raise the matching budget allowance or
startup rejects the combination.
"""

from __future__ import annotations

import math

# The placement controller has always slept at least this long. The budget
# uses the same floor so it cannot assume a faster loop than the process runs.
MINIMUM_PLACEMENT_INTERVAL_SECONDS = 2.0


class PlacementRenewalRunExceeded(RuntimeError):
    """A locked placement run did not finish inside its configured max run.

    The attempt is rolled back. No lease written by that attempt is committed.
    Callers log this and leave the previous lease in place. The lease duration
    is not shortened, and fence grace is not spent, to hide the overrun.
    """


class PlacementRenewalBudgetError(ValueError):
    """The configured renewal budget cannot refresh every owner before the lease.

    Raised when settings are loaded and when the controller starts. A live
    population above the ceiling does not raise this error: that run skips
    renewal, logs a critical alert, and still scans and fails over.
    Callers must not shorten the lease to silence this error.
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


def _retry_sleep_seconds(lock_retry_seconds: float, lock_retry_limit: int) -> float:
    """Return the backoff a full lock miss actually sleeps.

    The loop sleeps between attempts and not after the last one.

    Args:
        lock_retry_seconds: Delay between tries for the placement execution lock.
        lock_retry_limit: Total lock attempts in one cycle, including the first.

    Returns:
        Seconds slept when every attempt fails: (attempts - 1) times the delay.
    """
    return float(lock_retry_seconds) * max(0, int(lock_retry_limit) - 1)


def renewal_budget_seconds(
    *,
    max_assignments: int,
    batch_size: int,
    interval_seconds: float,
    max_run_seconds: float,
    missed_lock_cycles: int,
    fence_poll_seconds: float,
    clock_skew_seconds: float,
    safety_margin_seconds: float,
    lock_retry_seconds: float,
    lock_retry_limit: int,
) -> tuple[float, int, float, float]:
    """Return the conservative renewal budget and its page arithmetic.

    Args:
        max_assignments: Ceiling of active assignments the budget must cover.
        batch_size: Assignments renewed on one successful controller run.
        interval_seconds: Configured controller interval, before the 2 second floor.
        max_run_seconds: Configured upper bound on one locked placement run.
        missed_lock_cycles: Extra full cycles reserved after retries are exhausted.
        fence_poll_seconds: Allowance for the node learning a new lease late.
        clock_skew_seconds: Allowance for the node clock running ahead of control.
        safety_margin_seconds: Extra slack that must remain inside the lease.
        lock_retry_seconds: Delay between tries for the placement execution lock.
        lock_retry_limit: Total lock attempts in one cycle, including the first.

    Returns:
        Tuple of required seconds, page count, one successful cycle's seconds
        (retry sleep plus max run plus the interval), and the retry sleep a
        cycle spends before its last lock attempt. Required seconds also
        include the revisit's own max run, which is not part of the page cycle.

    Raises:
        PlacementRenewalBudgetError: When an input cannot be used in the formula.
    """
    if max_assignments <= 0 or batch_size <= 0:
        raise PlacementRenewalBudgetError(
            "placement renewal budget rejected: "
            "placement_renewal_max_assignments and placement_renewal_batch_size must be > 0"
        )
    if missed_lock_cycles < 1:
        raise PlacementRenewalBudgetError(
            "placement renewal budget rejected: "
            "placement_renewal_missed_lock_cycles must be >= 1"
        )
    if max_run_seconds < 0 or fence_poll_seconds < 1 or clock_skew_seconds < 0 or safety_margin_seconds <= 0:
        raise PlacementRenewalBudgetError(
            "placement renewal budget rejected: max run must be >= 0, fence poll >= 1, "
            "clock skew >= 0, and safety margin > 0"
        )
    interval = resolved_placement_interval_seconds(interval_seconds)
    pages = math.ceil(max_assignments / batch_size)
    retry_sleep = _retry_sleep_seconds(lock_retry_seconds, lock_retry_limit)
    # The granting run can acquire immediately. Every later cycle, including
    # the revisit, can burn every retry before the attempt that gets the lock.
    # pages * max_run stops at the last other page. The revisit spends another
    # max_run after its stamp and before its commit.
    page_cycle = retry_sleep + float(max_run_seconds) + interval
    miss_cycle = retry_sleep + interval
    required = (
        pages * page_cycle
        + int(missed_lock_cycles) * miss_cycle
        + float(max_run_seconds)
        + float(fence_poll_seconds)
        + float(clock_skew_seconds)
        + float(safety_margin_seconds)
    )
    return required, pages, page_cycle, retry_sleep


def assert_placement_renewal_budget(
    *,
    max_assignments: int,
    batch_size: int,
    interval_seconds: float,
    max_run_seconds: float,
    missed_lock_cycles: int,
    fence_poll_seconds: float,
    clock_skew_seconds: float,
    safety_margin_seconds: float,
    lock_retry_seconds: float,
    lock_retry_limit: int,
    lease_seconds: int,
) -> float:
    """Reject a renewal cadence that is not strictly inside the lease.

    A pages*interval comparison is not enough. Charging retry sleeps only on a
    fully missed lock is not enough either: a cycle that fails the first
    attempts and then acquires still sleeps those retries before it stamps
    now. The probe that accepted 56s against a 60s lease committed the
    replacement at 81s.

    Args:
        max_assignments: Ceiling of active assignments the budget must cover.
        batch_size: Assignments renewed on one successful controller run.
        interval_seconds: Configured controller interval, before the 2 second floor.
        max_run_seconds: Configured upper bound on one locked placement run.
        missed_lock_cycles: Extra full cycles reserved after retries are exhausted.
        fence_poll_seconds: Allowance for the node learning a new lease late.
        clock_skew_seconds: Allowance for the node clock running ahead of control.
        safety_margin_seconds: Extra slack that must remain inside the lease.
        lock_retry_seconds: Delay between tries for the placement execution lock.
        lock_retry_limit: Total lock attempts in one controller cycle, including the first.
        lease_seconds: Full placement lease. This function does not change it.

    Returns:
        The required budget in seconds when it is strictly less than the lease.

    Raises:
        PlacementRenewalBudgetError: When the inputs are unusable, the lock
            retry window is not well under the interval, or the required
            budget is greater than or equal to the lease.
    """
    if lease_seconds <= 0:
        raise PlacementRenewalBudgetError(
            "placement renewal budget rejected: placement_lease_seconds must be > 0; "
            "the lease is not shortened"
        )
    if lock_retry_limit < 1 or lock_retry_seconds <= 0:
        raise PlacementRenewalBudgetError(
            "placement renewal budget rejected: lock retry limit must be >= 1 "
            "and lock retry seconds must be > 0"
        )
    interval = resolved_placement_interval_seconds(interval_seconds)
    retry_window = float(lock_retry_seconds) * (int(lock_retry_limit) - 1)
    if retry_window >= interval:
        raise PlacementRenewalBudgetError(
            "placement renewal budget rejected: lock retry window "
            f"{_seconds_text(retry_window)}s must stay under the "
            f"{_seconds_text(interval)}s cycle interval"
        )
    required, pages, cycle, retry_sleep = renewal_budget_seconds(
        max_assignments=max_assignments,
        batch_size=batch_size,
        interval_seconds=interval_seconds,
        max_run_seconds=max_run_seconds,
        missed_lock_cycles=missed_lock_cycles,
        fence_poll_seconds=fence_poll_seconds,
        clock_skew_seconds=clock_skew_seconds,
        safety_margin_seconds=safety_margin_seconds,
        lock_retry_seconds=lock_retry_seconds,
        lock_retry_limit=lock_retry_limit,
    )
    page_interval = pages * interval
    if required >= lease_seconds:
        raise PlacementRenewalBudgetError(
            "placement renewal budget exceeded: "
            f"required {_seconds_text(required)}s >= lease {lease_seconds}s "
            f"(pages={pages}, page_interval={_seconds_text(page_interval)}s, "
            f"cycle={_seconds_text(cycle)}s, missed_lock_cycles={int(missed_lock_cycles)}, "
            f"retry_sleep={_seconds_text(retry_sleep)}s, "
            f"fence_poll={_seconds_text(fence_poll_seconds)}s, "
            f"clock_skew={_seconds_text(clock_skew_seconds)}s, "
            f"safety_margin={_seconds_text(safety_margin_seconds)}s). "
            "A pages*interval bound inside the lease is not sufficient. Every "
            "cycle may spend its lock retries before it acquires, a fully "
            "missed cycle adds that retry sleep plus the interval, and the "
            "revisit spends its own max run before the fence poll. "
            "Raise the renewal batch or lower the interval; do not shorten the lease "
            "and do not spend fence grace as extra life."
        )
    return required


def assert_settings_renewal_budget(values) -> float:
    """Validate the renewal budget on a settings object.

    Args:
        values: Settings instance or any object with the placement renewal fields.

    Returns:
        The required budget in seconds when the configuration holds.

    Raises:
        PlacementRenewalBudgetError: When the configuration cannot meet the lease.
    """
    return assert_placement_renewal_budget(
        max_assignments=values.placement_renewal_max_assignments,
        batch_size=values.placement_renewal_batch_size,
        interval_seconds=values.placement_interval_seconds,
        max_run_seconds=values.placement_renewal_max_run_seconds,
        missed_lock_cycles=values.placement_renewal_missed_lock_cycles,
        fence_poll_seconds=values.placement_renewal_fence_poll_seconds,
        clock_skew_seconds=values.placement_renewal_clock_skew_seconds,
        safety_margin_seconds=values.placement_renewal_safety_margin_seconds,
        lock_retry_seconds=values.placement_renewal_lock_retry_seconds,
        lock_retry_limit=values.placement_renewal_lock_retry_limit,
        lease_seconds=values.placement_lease_seconds,
    )
