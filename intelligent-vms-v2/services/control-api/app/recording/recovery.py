"""Crash recovery and index/disk reconciliation.

Runs at service startup, before any recorder is scheduled. Answers two
questions honestly:

  1. Which segments were WRITING when the process died?
     -> If the `.part` file exists and is non-empty it is finalized; otherwise
        it is marked CORRUPT. Either way the row never stays WRITING, which
        would block retention forever.

  2. Which files exist on disk but are absent from the index?
     -> Reported as orphans. They are NOT auto-imported: adopting unknown
        files as evidence without review is unacceptable in a security
        product.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from vms_contracts.enums import SegmentState

from app.core.timeutil import utcnow
from app.db.models import RecordingSegment, RecordingTask, StorageVolume
from app.logging_setup import get_logger
from app.recording.index import resolve_path

log = get_logger("recording.recovery")


@dataclass
class RecoveryReport:
    writing_found: int = 0
    finalized: int = 0
    marked_corrupt: int = 0
    orphans_found: int = 0
    tasks_released: int = 0
    details: list[str] = field(default_factory=list)


def recover_writing_segments(session: Session) -> RecoveryReport:
    report = RecoveryReport()
    rows = session.execute(
        select(RecordingSegment)
        .where(RecordingSegment.state == SegmentState.WRITING.value)).scalars().all()
    report.writing_found = len(rows)

    for segment in rows:
        final_path = resolve_path(session, segment)
        if final_path is None:
            segment.state = SegmentState.CORRUPT.value
            report.marked_corrupt += 1
            report.details.append(f"{segment.id}: storage volume missing")
            continue

        part_path = final_path.with_suffix(final_path.suffix + ".part")
        source = (final_path if final_path.is_file()
                  else (part_path if part_path.is_file() else None))
        if source is None:
            segment.state = SegmentState.CORRUPT.value
            report.marked_corrupt += 1
            report.details.append(f"{segment.id}: no file on disk")
            continue

        size = source.stat().st_size
        if size <= 0:
            try:
                source.unlink()
            except OSError:
                pass
            segment.state = SegmentState.CORRUPT.value
            report.marked_corrupt += 1
            report.details.append(f"{segment.id}: zero-byte file removed")
            continue

        if source == part_path:
            try:
                os.replace(part_path, final_path)
            except OSError as exc:
                segment.state = SegmentState.CORRUPT.value
                report.marked_corrupt += 1
                report.details.append(f"{segment.id}: could not finalize ({exc})")
                continue

        segment.size_bytes = size
        segment.ended_at = segment.ended_at or _mtime(final_path)
        segment.duration_s = max(
            0.0, ((segment.ended_at or utcnow()) - segment.started_at).total_seconds())
        segment.state = SegmentState.FINALIZED.value
        report.finalized += 1

    if rows:
        log.warning("recording.recovered_writing_segments",
                    found=report.writing_found, finalized=report.finalized,
                    corrupt=report.marked_corrupt)
    return report


def release_stale_tasks(session: Session, *, stale_after_s: int = 120) -> int:
    """Clear worker ownership so another recorder can pick the camera up."""
    cutoff = utcnow() - dt.timedelta(seconds=stale_after_s)
    released = 0
    for task in session.execute(
            select(RecordingTask).where(RecordingTask.worker_id.is_not(None))).scalars():
        if task.heartbeat_at is None or task.heartbeat_at < cutoff:
            task.worker_id = None
            task.state = "idle"
            released += 1
    if released:
        log.warning("recording.released_stale_tasks", count=released)
    return released


def scan_orphans(session: Session, *, limit_per_volume: int = 500) -> RecoveryReport:
    """Find media files on disk with no index row."""
    report = RecoveryReport()
    for volume in session.execute(select(StorageVolume)).scalars().all():
        root = Path(volume.path) / "recordings"
        if not root.is_dir():
            continue
        known = set(session.execute(
            select(RecordingSegment.relative_path)
            .where(RecordingSegment.volume_id == volume.id)).scalars().all())
        found = 0
        for path in root.rglob("*.mp4"):
            relative = path.relative_to(Path(volume.path)).as_posix()
            if relative in known:
                continue
            report.orphans_found += 1
            report.details.append(f"{volume.name}:{relative}")
            found += 1
            if found >= limit_per_volume:
                break
    if report.orphans_found:
        log.warning("recording.orphans_detected", count=report.orphans_found)
    return report


def run_startup_recovery(session: Session) -> RecoveryReport:
    report = recover_writing_segments(session)
    report.tasks_released = release_stale_tasks(session)
    orphans = scan_orphans(session)
    report.orphans_found = orphans.orphans_found
    report.details.extend(orphans.details[:50])
    return report


def _mtime(path: Path) -> dt.datetime:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return utcnow()
