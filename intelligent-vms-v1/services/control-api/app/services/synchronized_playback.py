"""Compute per-camera gaps and shared coverage from trusted recording spans."""

from collections import defaultdict
from datetime import datetime, timezone

from app.models.schemas import (
    PlaybackCoverageInterval,
    PlaybackCoverageTrack,
    SynchronizedPlaybackCoverage,
)


def build_playback_coverage(
    start: datetime,
    end: datetime,
    spans_by_camera: dict[str, list[tuple[datetime, datetime]]],
) -> SynchronizedPlaybackCoverage:
    """Clip and merge recording spans, then intersect coverage across cameras."""
    tracks = []
    boundaries: dict[datetime, int] = defaultdict(int)
    for camera_id, spans in spans_by_camera.items():
        recorded = _merge_spans(start, end, spans)
        gaps = []
        cursor = start
        for span in recorded:
            if cursor < span.start:
                gaps.append(PlaybackCoverageInterval(start=cursor, end=span.start))
            cursor = span.end
            boundaries[span.start] += 1
            boundaries[span.end] -= 1
        if cursor < end:
            gaps.append(PlaybackCoverageInterval(start=cursor, end=end))
        tracks.append(PlaybackCoverageTrack(camera_id=camera_id, recorded=recorded, gaps=gaps))

    common = []
    active = 0
    common_start = None
    for boundary, change in sorted(boundaries.items()):
        active += change
        if active == len(tracks):
            if common_start is None:
                common_start = boundary
        elif common_start is not None:
            common.append(PlaybackCoverageInterval(start=common_start, end=boundary))
            common_start = None
    return SynchronizedPlaybackCoverage(start=start, end=end, tracks=tracks, common_coverage=common)


def _merge_spans(
    start: datetime,
    end: datetime,
    spans: list[tuple[datetime, datetime]],
) -> list[PlaybackCoverageInterval]:
    merged: list[PlaybackCoverageInterval] = []
    for span_start, span_end in sorted(spans):
        left, right = max(start, span_start), min(end, span_end)
        if left >= right:
            continue
        left, right = left.astimezone(timezone.utc), right.astimezone(timezone.utc)
        if merged and left <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, right)
        else:
            merged.append(PlaybackCoverageInterval(start=left, end=right))
    return merged
