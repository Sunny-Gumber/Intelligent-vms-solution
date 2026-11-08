"""Recording index — the authoritative map from time to media files.

Playback, export, retention and continuity reporting all depend on this being
correct, so the rules are strict:

  * A segment row is created in state WRITING before the recorder opens the
    file, so a crash always leaves a traceable record.
  * A segment becomes FINALIZED only after the file has been atomically
    renamed from `.part` and its size measured. Nothing else sets FINALIZED.
  * Queries never return WRITING segments to playback/export; partially
    written files are not evidence.
  * Time ranges are half-open [started_at, ended_at) in UTC.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from vms_contracts.enums import SegmentState

from app.core.timeutil import ensure_utc, utcnow
from app.db.models import RecordingSegment, StorageVolume
from app.logging_setup import get_logger

log = get_logger("recording.index")


def open_segment(session: Session, *, camera_id: str, camera_name: str | None, role: str,
                 volume_id: str, relative_path: str, started_at: dt.datetime,
                 reason: str = "continuous", segment_id: str | None = None,
                 has_gap_before: bool = False) -> RecordingSegment:
    segment = RecordingSegment(
        camera_id=camera_id, camera_name=camera_name, stream_role=role,
        volume_id=volume_id, relative_path=relative_path,
        started_at=ensure_utc(started_at), state=SegmentState.WRITING.value,
        recording_reason=reason, has_gap_before=has_gap_before)
    if segment_id:
        segment.id = segment_id
    session.add(segment)
    session.flush()
    return segment


def finalize_segment(session: Session, segment_id: str, *, ended_at: dt.datetime,
                     size_bytes: int | None, codec: str | None = None,
                     checksum: str | None = None) -> RecordingSegment | None:
    segment = session.get(RecordingSegment, segment_id)
    if segment is None:
        log.warning("recording.finalize_unknown_segment", segment_id=segment_id)
        return None
    if segment.state == SegmentState.FINALIZED.value:
        return segment

    segment.ended_at = ensure_utc(ended_at)
    segment.duration_s = max(0.0, (segment.ended_at - segment.started_at).total_seconds())
    segment.size_bytes = size_bytes
    segment.codec = codec or segment.codec
    segment.checksum = checksum

    if not size_bytes:
        # An empty file is a failure, not a recording. CORRUPT keeps it out of
        # playback and makes the gap visible in continuity reports.
        segment.state = SegmentState.CORRUPT.value
        log.warning("recording.segment_empty", segment_id=segment_id,
                    camera_id=segment.camera_id)
    else:
        segment.state = SegmentState.FINALIZED.value
    return segment


def mark_corrupt(session: Session, segment_id: str, reason: str) -> None:
    segment = session.get(RecordingSegment, segment_id)
    if segment is None:
        return
    segment.state = SegmentState.CORRUPT.value
    log.warning("recording.segment_corrupt", segment_id=segment_id, reason=reason)


def segments_in_range(session: Session, camera_id: str, start: dt.datetime,
                      end: dt.datetime, *, role: str | None = None,
                      states: tuple[str, ...] = (SegmentState.FINALIZED.value,)
                      ) -> list[RecordingSegment]:
    """All segments overlapping [start, end)."""
    start, end = ensure_utc(start), ensure_utc(end)
    stmt = (select(RecordingSegment)
            .where(RecordingSegment.camera_id == camera_id,
                   RecordingSegment.state.in_(states),
                   RecordingSegment.started_at < end,
                   or_(RecordingSegment.ended_at.is_(None),
                       RecordingSegment.ended_at > start))
            .order_by(RecordingSegment.started_at))
    if role:
        stmt = stmt.where(RecordingSegment.stream_role == role)
    return list(session.execute(stmt).scalars().all())


def availability(session: Session, camera_id: str, start: dt.datetime,
                 end: dt.datetime) -> tuple[dt.datetime | None, dt.datetime | None, float]:
    """Return (first_start, last_end, recorded_seconds) within the window."""
    start, end = ensure_utc(start), ensure_utc(end)
    segments = segments_in_range(session, camera_id, start, end)
    if not segments:
        return None, None, 0.0
    recorded = 0.0
    for segment in segments:
        seg_start = max(segment.started_at, start)
        seg_end = min(segment.ended_at or utcnow(), end)
        recorded += max(0.0, (seg_end - seg_start).total_seconds())
    return (segments[0].started_at,
            segments[-1].ended_at or segments[-1].started_at, recorded)


def timeline_buckets(session: Session, camera_id: str, start: dt.datetime,
                     end: dt.datetime, bucket_count: int = 96) -> list[dict]:
    """Density buckets for the playback timeline widget."""
    start, end = ensure_utc(start), ensure_utc(end)
    total = max(1.0, (end - start).total_seconds())
    width = total / max(1, bucket_count)
    segments = segments_in_range(session, camera_id, start, end)

    buckets: list[dict] = []
    for i in range(bucket_count):
        b_start = start + dt.timedelta(seconds=i * width)
        b_end = b_start + dt.timedelta(seconds=width)
        recorded = 0.0
        for segment in segments:
            seg_end = segment.ended_at or utcnow()
            overlap_start = max(segment.started_at, b_start)
            overlap_end = min(seg_end, b_end)
            if overlap_end > overlap_start:
                recorded += (overlap_end - overlap_start).total_seconds()
        buckets.append({"start": b_start, "end": b_end,
                        "recorded_s": round(min(recorded, width), 2),
                        "has_gap": recorded < width * 0.95})
    return buckets


def latest_segment(session: Session, camera_id: str,
                   role: str | None = None) -> RecordingSegment | None:
    stmt = (select(RecordingSegment).where(RecordingSegment.camera_id == camera_id)
            .order_by(RecordingSegment.started_at.desc()).limit(1))
    if role:
        stmt = stmt.where(RecordingSegment.stream_role == role)
    return session.execute(stmt).scalar_one_or_none()


def resolve_path(session: Session, segment: RecordingSegment) -> Path | None:
    """Absolute path of a segment, or None if its volume is gone."""
    from app.storage.allocator import resolve_segment_path

    if not segment.volume_id:
        return None
    volume = session.get(StorageVolume, segment.volume_id)
    if volume is None:
        return None
    try:
        return resolve_segment_path(volume, segment.relative_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("recording.path_resolve_failed", segment_id=segment.id,
                    error=str(exc))
        return None


def storage_summary(session: Session, camera_id: str | None = None) -> dict:
    stmt = select(func.count(), func.coalesce(func.sum(RecordingSegment.size_bytes), 0),
                  func.min(RecordingSegment.started_at),
                  func.max(RecordingSegment.ended_at)).where(
        RecordingSegment.state == SegmentState.FINALIZED.value)
    if camera_id:
        stmt = stmt.where(RecordingSegment.camera_id == camera_id)
    count, total_bytes, oldest, newest = session.execute(stmt).one()
    return {"segments": int(count or 0), "bytes": int(total_bytes or 0),
            "oldest": oldest, "newest": newest}
