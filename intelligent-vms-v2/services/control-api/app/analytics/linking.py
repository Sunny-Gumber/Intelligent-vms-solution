"""Link analytics results to the recorded footage that contains them.

A detection is only evidence if an operator can jump straight to the video.
Linking is done by time against the recording index and stored on the
analytics row so later searches are cheap.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session
from vms_contracts.enums import SegmentState

from app.core.timeutil import ensure_utc
from app.db.models import AnalyticsEvent, RecordingSegment


def link_segment(session: Session, record: AnalyticsEvent) -> str | None:
    segment = session.execute(
        select(RecordingSegment).where(
            RecordingSegment.camera_id == record.camera_id,
            RecordingSegment.state == SegmentState.FINALIZED.value,
            RecordingSegment.started_at <= record.occurred_at,
            RecordingSegment.ended_at > record.occurred_at).limit(1)).scalar_one_or_none()
    if segment is None:
        return None
    record.segment_id = segment.id
    return segment.id


def backfill_links(session: Session, *, since: dt.datetime, limit: int = 1000) -> int:
    rows = session.execute(
        select(AnalyticsEvent).where(AnalyticsEvent.segment_id.is_(None),
                                     AnalyticsEvent.occurred_at >= ensure_utc(since))
        .limit(limit)).scalars().all()
    return sum(1 for row in rows if link_segment(session, row))


def playback_jump(session: Session, analytics_id: str, pre_roll_s: int = 5,
                  post_roll_s: int = 15) -> dict | None:
    """Everything a client needs to open playback at a detection."""
    record = session.get(AnalyticsEvent, analytics_id)
    if record is None:
        return None
    segment_id = record.segment_id or link_segment(session, record)
    return {"camera_id": record.camera_id,
            "start": record.occurred_at - dt.timedelta(seconds=pre_roll_s),
            "end": record.occurred_at + dt.timedelta(seconds=post_roll_s),
            "segment_id": segment_id, "has_recording": segment_id is not None,
            "bbox": record.bbox, "object_class": record.object_class,
            "source": record.source}
