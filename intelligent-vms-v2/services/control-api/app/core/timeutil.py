"""Time handling.

Product-wide rule: every timestamp stored, logged or transmitted is
timezone-aware UTC. Local time exists only at the presentation layer, derived
from the site timezone. This eliminates the classic VMS bug where recordings
"disappear" across DST boundaries.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.UTC


def utcnow() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Coerce to aware UTC.

    Naive datetimes are treated as UTC. We do NOT guess a local timezone,
    because silently guessing is how footage gets exported from the wrong hour.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_site_time(value: dt.datetime, timezone: str) -> dt.datetime:
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    return ensure_utc(value).astimezone(tz)


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def iso(value: dt.datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> dt.datetime:
    return ensure_utc(dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00")))


def floor_to(value: dt.datetime, seconds: int) -> dt.datetime:
    value = ensure_utc(value)
    epoch = int(value.timestamp())
    return dt.datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)


def overlaps(a_start: dt.datetime, a_end: dt.datetime,
             b_start: dt.datetime, b_end: dt.datetime) -> bool:
    return ensure_utc(a_start) < ensure_utc(b_end) and ensure_utc(b_start) < ensure_utc(a_end)


def clamp_range(start: dt.datetime, end: dt.datetime,
                max_seconds: int) -> tuple[dt.datetime, dt.datetime, bool]:
    """Bound a requested time range. Returns (start, end, was_clamped)."""
    start, end = ensure_utc(start), ensure_utc(end)
    if (end - start).total_seconds() > max_seconds:
        return start, start + dt.timedelta(seconds=max_seconds), True
    return start, end, False
