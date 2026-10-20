"""Counting and business-intelligence aggregation.

Counts derive from stored analytics records, so they work identically for
camera-side and server-side detections.

LIMITATION STATED PLAINLY: accuracy is entirely determined by the quality of
the upstream detector. This module does not claim counting accuracy; it
reports what it was given.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timeutil import ensure_utc
from app.db.models import AnalyticsEvent


@dataclass
class CountBucket:
    start: dt.datetime
    end: dt.datetime
    counts: dict[str, int]
    unique_tracks: int


def count_by_interval(session: Session, *, camera_ids: list[str], start: dt.datetime,
                      end: dt.datetime, interval_minutes: int = 60,
                      object_classes: list[str] | None = None,
                      zone_id: str | None = None) -> list[CountBucket]:
    start, end = ensure_utc(start), ensure_utc(end)
    stmt = select(AnalyticsEvent).where(AnalyticsEvent.camera_id.in_(camera_ids),
                                        AnalyticsEvent.occurred_at >= start,
                                        AnalyticsEvent.occurred_at < end)
    if object_classes:
        stmt = stmt.where(AnalyticsEvent.object_class.in_(object_classes))
    if zone_id:
        stmt = stmt.where(AnalyticsEvent.zone_id == zone_id)

    rows = session.execute(stmt.order_by(AnalyticsEvent.occurred_at)).scalars().all()

    width = dt.timedelta(minutes=max(1, interval_minutes))
    buckets: list[CountBucket] = []
    cursor = start
    while cursor < end:
        bucket_end = min(cursor + width, end)
        counts: dict[str, int] = defaultdict(int)
        tracks: set[str] = set()
        for row in rows:
            if cursor <= row.occurred_at < bucket_end:
                counts[row.object_class] += 1
                if row.track_id:
                    tracks.add(row.track_id)
        buckets.append(CountBucket(cursor, bucket_end, dict(counts), len(tracks)))
        cursor = bucket_end
    return buckets


def directional_counts(session: Session, *, camera_id: str, zone_id: str,
                       start: dt.datetime, end: dt.datetime) -> dict[str, int]:
    rows = session.execute(
        select(AnalyticsEvent).where(
            AnalyticsEvent.camera_id == camera_id, AnalyticsEvent.zone_id == zone_id,
            AnalyticsEvent.rule == "tripwire",
            AnalyticsEvent.occurred_at >= ensure_utc(start),
            AnalyticsEvent.occurred_at < ensure_utc(end))).scalars().all()
    counts = {"in": 0, "out": 0}
    for row in rows:
        direction = (row.attributes or {}).get("direction")
        if direction in counts:
            counts[direction] += 1
    counts["net"] = counts["in"] - counts["out"]
    return counts


def peak_occupancy(buckets: list[CountBucket]) -> CountBucket | None:
    return max(buckets, key=lambda b: b.unique_tracks, default=None)


def class_totals(session: Session, *, camera_ids: list[str], start: dt.datetime,
                 end: dt.datetime) -> dict[str, int]:
    rows = session.execute(
        select(AnalyticsEvent.object_class, func.count())
        .where(AnalyticsEvent.camera_id.in_(camera_ids),
               AnalyticsEvent.occurred_at >= ensure_utc(start),
               AnalyticsEvent.occurred_at < ensure_utc(end))
        .group_by(AnalyticsEvent.object_class)).all()
    return {object_class: int(count) for object_class, count in rows}
