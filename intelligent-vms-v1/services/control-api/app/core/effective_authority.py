"""Shared effective-authority boundary for control and the node-agent.

One function decides when an unacknowledged owner may still be executing.
Placement must not authorize a successor before that instant, and the
node-agent must fence at that same instant. Lease, grace, and any later
autonomy deadline are included. Acknowledged fencing ends authority early.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Node-agent FENCE_EXPIRY_GRACE_SECONDS and control failover use this default.
# Changing one side without the other re-opens a split-brain window.
DEFAULT_FENCE_EXPIRY_GRACE_SECONDS = 2.0


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to aware UTC.

    Args:
        value: Aware or naive timestamp. Naive values are treated as UTC.

    Returns:
        Timezone-aware UTC datetime.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def effective_authority_deadline(
    lease_expires_at: datetime | None,
    autonomy_expires_at: datetime | None,
    grace_seconds: float,
) -> datetime | None:
    """Return the instant unacknowledged authority stops being valid.

    The deadline is lease expiry plus grace, extended to a later autonomy
    expiry when one is present. Callers treat authority as live only while
    now is strictly earlier than this instant, so a successor may be
    authorized at the deadline without overlapping the old owner.

    Args:
        lease_expires_at: Central lease expiry. Naive values are UTC.
        autonomy_expires_at: Optional pre-granted offline deadline.
        grace_seconds: Non-negative fence grace added to the lease. Deployments
            must pass the same value the node-agent will honor.

    Returns:
        The effective deadline, or None when neither a lease nor an autonomy
        deadline is present.

    Raises:
        ValueError: If grace_seconds is negative.
    """
    if grace_seconds < 0:
        raise ValueError("fence expiry grace seconds must be >= 0")
    deadline: datetime | None = None
    if lease_expires_at is not None:
        deadline = _as_utc(lease_expires_at) + timedelta(seconds=grace_seconds)
    if autonomy_expires_at is not None:
        autonomy = _as_utc(autonomy_expires_at)
        if deadline is None or autonomy > deadline:
            deadline = autonomy
    return deadline


def effective_authority_active(
    lease_expires_at: datetime | None,
    autonomy_expires_at: datetime | None,
    now: datetime,
    grace_seconds: float,
    *,
    acknowledged_fenced: bool = False,
) -> bool:
    """Return whether this owner may still be executing at now.

    Args:
        lease_expires_at: Central lease expiry. Naive values are UTC.
        autonomy_expires_at: Optional pre-granted offline deadline.
        now: Evaluation time. Naive values are treated as UTC.
        grace_seconds: Non-negative fence grace added to the lease.
        acknowledged_fenced: True when this generation's fencing has been
            acknowledged. Acknowledgement ends authority before the deadline.

    Returns:
        True while the owner may still be executing. False at the deadline
        itself and when fencing has been acknowledged.

    Raises:
        ValueError: If grace_seconds is negative.
    """
    if acknowledged_fenced:
        return False
    deadline = effective_authority_deadline(
        lease_expires_at,
        autonomy_expires_at,
        grace_seconds,
    )
    if deadline is None:
        return False
    return _as_utc(now) < deadline
