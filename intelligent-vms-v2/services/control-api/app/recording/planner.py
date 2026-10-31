"""Recording schedule evaluation.

A schedule is a list of weekly blocks expressed in the SITE's timezone:

    {"day": 0..6 (Mon..Sun), "start": "08:00", "end": "18:00",
     "mode": "continuous"}

Blocks are evaluated in site-local time, which is the only way to get DST
right: a block ending at 18:00 local must still end at 18:00 local after the
clocks change.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from vms_contracts.enums import RecordingMode

from app.core.timeutil import ensure_utc, utcnow


@dataclass(frozen=True)
class ScheduleBlock:
    day: int                  # 0 = Monday
    start: dt.time
    end: dt.time
    mode: RecordingMode = RecordingMode.CONTINUOUS

    @classmethod
    def parse(cls, raw: dict) -> ScheduleBlock:
        day = int(raw.get("day", 0))
        if not 0 <= day <= 6:
            raise ValueError(f"day must be 0..6, got {day}")
        start = _parse_time(raw.get("start", "00:00"))
        end = _parse_time(raw.get("end", "23:59"))
        if end <= start:
            raise ValueError(f"block end {end} must be after start {start}")
        return cls(day=day, start=start, end=end,
                   mode=RecordingMode(raw.get("mode", RecordingMode.CONTINUOUS.value)))

    def contains(self, local_moment: dt.datetime) -> bool:
        if local_moment.weekday() != self.day:
            return False
        return self.start <= local_moment.time() < self.end


def _parse_time(value: str) -> dt.time:
    hours, _, minutes = str(value).partition(":")
    return dt.time(hour=int(hours), minute=int(minutes or 0))


def parse_blocks(raw_blocks: list[dict] | None) -> list[ScheduleBlock]:
    return [ScheduleBlock.parse(block) for block in (raw_blocks or [])]


def should_record(blocks: list[ScheduleBlock], timezone: str = "UTC",
                  moment: dt.datetime | None = None
                  ) -> tuple[bool, RecordingMode | None]:
    """Decide whether recording is active right now.

    An empty block list means "always record", matching operator expectation
    for a schedule that has been enabled but not yet drawn.
    """
    moment = ensure_utc(moment or utcnow())
    try:
        local = moment.astimezone(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001 - unknown tz falls back to UTC, never crashes recording
        local = moment

    if not blocks:
        return True, RecordingMode.CONTINUOUS
    for block in blocks:
        if block.contains(local):
            return True, block.mode
    return False, None


def next_transition(blocks: list[ScheduleBlock], timezone: str = "UTC",
                    moment: dt.datetime | None = None,
                    horizon_hours: int = 48) -> dt.datetime | None:
    """When the recording state next changes — used to schedule a wake-up."""
    moment = ensure_utc(moment or utcnow())
    if not blocks:
        return None
    active_now, _ = should_record(blocks, timezone, moment)
    cursor = moment
    limit = moment + dt.timedelta(hours=horizon_hours)
    while cursor < limit:
        cursor += dt.timedelta(minutes=1)
        if should_record(blocks, timezone, cursor)[0] != active_now:
            return cursor
    return None


def coverage_hours(blocks: list[ScheduleBlock]) -> float:
    total = 0.0
    base = dt.date(2000, 1, 3)
    for block in blocks:
        total += (dt.datetime.combine(base, block.end)
                  - dt.datetime.combine(base, block.start)).total_seconds() / 3600.0
    return round(total, 2)
