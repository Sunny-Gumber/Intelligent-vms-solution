"""Recording gap detection and continuity reporting.

A "gap" is any interval inside a window where recording was expected but no
finalized segment exists, longer than `tolerance_s` (segment boundaries
legitimately produce sub-second holes).

Honest scope: gaps are derived from the index. If the index itself was never
written — e.g. the process was SIGKILLed before flushing — the recovery pass
in app/recording/recovery.py reconciles disk against index at startup before
gaps are computed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import ensure_utc, utcnow
from app.db.models import RecordingGap
from app.recording.index import segments_in_range


@dataclass(frozen=True)
class Gap:
    start: dt.datetime
    end: dt.datetime
    cause: str | None = None

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end,
                "duration_s": round(self.duration_s, 2), "cause": self.cause}


def find_gaps(session: Session, camera_id: str, start: dt.datetime, end: dt.datetime, *,
              tolerance_s: float = 3.0, role: str | None = None) -> list[Gap]:
    start, end = ensure_utc(start), ensure_utc(end)
    segments = segments_in_range(session, camera_id, start, end, role=role)
    gaps: list[Gap] = []
    cursor = start

    for segment in segments:
        seg_start = max(segment.started_at, start)
        if (seg_start - cursor).total_seconds() > tolerance_s:
            gaps.append(Gap(cursor, seg_start,
                            "no recording" if cursor == start
                            else "recording interrupted"))
        cursor = max(cursor, min(segment.ended_at or utcnow(), end))

    if (end - cursor).total_seconds() > tolerance_s:
        gaps.append(Gap(cursor, end, "no recording"))
    return gaps


def continuity_percent(session: Session, camera_id: str, start: dt.datetime,
                       end: dt.datetime, *, tolerance_s: float = 3.0) -> float:
    start, end = ensure_utc(start), ensure_utc(end)
    window = max(1.0, (end - start).total_seconds())
    missing = sum(g.duration_s for g in find_gaps(session, camera_id, start, end,
                                                  tolerance_s=tolerance_s))
    return round(max(0.0, (window - missing) / window) * 100.0, 2)


def record_gap(session: Session, camera_id: str, gap: Gap,
               role: str = "main") -> RecordingGap:
    row = RecordingGap(camera_id=camera_id, stream_role=role, started_at=gap.start,
                       ended_at=gap.end, duration_s=gap.duration_s, cause=gap.cause)
    session.add(row)
    return row


def persist_new_gaps(session: Session, camera_id: str, start: dt.datetime,
                     end: dt.datetime, *, tolerance_s: float = 3.0,
                     role: str = "main") -> int:
    """Store newly detected gaps, avoiding duplicates for the same interval."""
    existing = {(row.started_at, row.ended_at) for row in session.execute(
        select(RecordingGap).where(RecordingGap.camera_id == camera_id,
                                   RecordingGap.started_at >= ensure_utc(start),
                                   RecordingGap.ended_at <= ensure_utc(end))
    ).scalars().all()}
    added = 0
    for gap in find_gaps(session, camera_id, start, end, tolerance_s=tolerance_s,
                         role=role):
        if (gap.start, gap.end) not in existing:
            record_gap(session, camera_id, gap, role)
            added += 1
    return added
