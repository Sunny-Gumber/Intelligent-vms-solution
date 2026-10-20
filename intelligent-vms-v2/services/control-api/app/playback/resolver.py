"""Playback resolution.

The contract enforced here (M5-R12, M5-R13):
  * A playback request ALWAYS returns within a bounded time with a definite
    answer: here is the footage, or here is precisely why there is none.
  * Missing, deleted or corrupt media produces a structured error with the
    exact gap — never an infinite spinner, never footage from another time.
  * Returned timestamps are the REAL recorded instants, so an operator can see
    they received 14:03:12-14:05:00 when they asked for 14:00:00-14:05:00.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from vms_contracts.dto import PlaybackManifest, SegmentOut
from vms_contracts.enums import StreamRole

from app.config import Settings, get_settings
from app.core.errors import MediaUnavailableError, ValidationError
from app.core.timeutil import clamp_range, ensure_utc, utcnow
from app.db.models import RecordingSegment
from app.logging_setup import get_logger
from app.recording import gaps as gap_service
from app.recording import index

log = get_logger("playback")

MAX_PLAYBACK_WINDOW_S = 24 * 3600


@dataclass
class ResolvedMedia:
    segments: list[RecordingSegment]
    paths: list[Path]
    actual_start: dt.datetime
    actual_end: dt.datetime
    missing: list[str]


def build_manifest(session: Session, camera_id: str, start: dt.datetime, end: dt.datetime, *,
                   role: StreamRole = StreamRole.MAIN,
                   settings: Settings | None = None) -> PlaybackManifest:
    settings = settings or get_settings()
    start, end = ensure_utc(start), ensure_utc(end)
    if end <= start:
        raise ValidationError("The end time must be after the start time",
                              code="invalid_time_range")
    start, end, clamped = clamp_range(start, end, MAX_PLAYBACK_WINDOW_S)

    segments = index.segments_in_range(session, camera_id, start, end, role=role.value)
    found_gaps = gap_service.find_gaps(session, camera_id, start, end,
                                       tolerance_s=settings.gap_tolerance_s, role=role.value)

    message = None
    if clamped:
        message = (f"The requested range was longer than {MAX_PLAYBACK_WINDOW_S // 3600} "
                   "hours and was shortened.")
    if not segments:
        message = ("No recording exists for this camera in the requested period. Check the "
                   "recording schedule and retention policy.")

    return PlaybackManifest(
        camera_id=camera_id, requested_from=start, requested_to=end,
        available_from=segments[0].started_at if segments else None,
        available_to=(segments[-1].ended_at or segments[-1].started_at) if segments else None,
        segments=[_to_dto(s) for s in segments],
        gaps=[g.to_dict() for g in found_gaps], message=message)


def resolve_media(session: Session, camera_id: str, start: dt.datetime, end: dt.datetime, *,
                  role: StreamRole = StreamRole.MAIN,
                  require_complete: bool = False) -> ResolvedMedia:
    """Resolve on-disk files for a range, verifying each one exists.

    require_complete=True (used by export) raises when any file is missing,
    because an export that silently skips footage is evidentially unsound.
    """
    start, end = ensure_utc(start), ensure_utc(end)
    segments = index.segments_in_range(session, camera_id, start, end, role=role.value)
    if not segments:
        raise MediaUnavailableError(
            "No recorded footage exists for the requested period",
            code="no_recording_in_range",
            extra={"camera_id": camera_id, "requested_start": start.isoformat(),
                   "requested_end": end.isoformat()})

    paths: list[Path] = []
    usable: list[RecordingSegment] = []
    missing: list[str] = []

    for segment in segments:
        path = index.resolve_path(session, segment)
        if path is None or not path.is_file():
            missing.append(segment.id)
            log.warning("playback.segment_missing_on_disk", segment_id=segment.id,
                        camera_id=camera_id, relative_path=segment.relative_path)
            continue
        if segment.size_bytes is not None and segment.size_bytes <= 0:
            missing.append(segment.id)
            continue
        paths.append(path)
        usable.append(segment)

    if not usable:
        raise MediaUnavailableError(
            "The index lists recordings for this period but the media files are not "
            "present on storage. Run storage diagnostics.",
            code="media_files_missing",
            extra={"camera_id": camera_id, "missing_segments": missing[:20]})

    if missing and require_complete:
        raise MediaUnavailableError(
            f"{len(missing)} of {len(segments)} recording files are missing or empty. "
            "Export refused: it must not silently omit footage.",
            code="incomplete_media", extra={"missing_segments": missing[:20]})

    return ResolvedMedia(segments=usable, paths=paths,
                         actual_start=max(usable[0].started_at, start),
                         actual_end=min(usable[-1].ended_at or utcnow(), end),
                         missing=missing)


def seek_offset(segment: RecordingSegment, moment: dt.datetime) -> float:
    """Seconds into a segment file for a wall-clock instant."""
    return max(0.0, (ensure_utc(moment) - segment.started_at).total_seconds())


def synchronized_timeline(session: Session, camera_ids: list[str], start: dt.datetime,
                          end: dt.datetime, *,
                          settings: Settings | None = None) -> dict[str, PlaybackManifest]:
    """Manifests for several cameras over one shared window.

    The server guarantees identical requested_from/requested_to across cameras
    so client timelines line up even when availability differs.
    """
    settings = settings or get_settings()
    if len(camera_ids) > 16:
        raise ValidationError("Synchronized playback supports at most 16 cameras",
                              code="too_many_cameras")
    return {cid: build_manifest(session, cid, start, end, settings=settings)
            for cid in camera_ids}


def _to_dto(segment: RecordingSegment) -> SegmentOut:
    return SegmentOut(id=segment.id, camera_id=segment.camera_id,
                      stream_role=StreamRole(segment.stream_role),
                      started_at=segment.started_at, ended_at=segment.ended_at,
                      duration_s=segment.duration_s, size_bytes=segment.size_bytes,
                      state=segment.state, has_gap_before=segment.has_gap_before)
