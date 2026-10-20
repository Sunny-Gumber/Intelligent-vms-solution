"""Retention and storage rotation.

Deletion is the most dangerous operation in a VMS. Safeguards:

  1. Only FINALIZED or CORRUPT segments older than the camera's retention are
     eligible. WRITING segments are never touched.
  2. Paths are re-derived through core.paths confinement before unlink, so a
     malformed index row cannot delete outside the volume.
  3. Index rows are marked DELETED rather than removed, preserving the record
     that footage existed and when it was removed.
  4. Every batch is audited with counts and reclaimed bytes.
  5. Emergency rotation (free space below threshold) deletes OLDEST-FIRST and
     raises a storage event — it never silently shortens retention without a
     trace.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from vms_contracts.enums import SegmentState

from app.config import Settings, get_settings
from app.core.timeutil import utcnow
from app.db.models import Camera, RecordingSegment, StorageVolume
from app.logging_setup import get_logger
from app.recording.index import resolve_path
from app.storage import volumes as volume_service

log = get_logger("recording.retention")


@dataclass
class RetentionResult:
    examined: int = 0
    deleted: int = 0
    bytes_reclaimed: int = 0
    missing_files: int = 0
    errors: list[str] = field(default_factory=list)
    emergency: bool = False

    def merge(self, other: "RetentionResult") -> "RetentionResult":
        self.examined += other.examined
        self.deleted += other.deleted
        self.bytes_reclaimed += other.bytes_reclaimed
        self.missing_files += other.missing_files
        self.errors.extend(other.errors)
        self.emergency = self.emergency or other.emergency
        return self


def retention_days_for(camera: Camera | None, settings: Settings) -> int:
    if camera is None:
        return settings.retention_default_days
    return camera.retention_days or settings.retention_default_days


def _delete_segment(session: Session, segment: RecordingSegment,
                    result: RetentionResult, *, dry_run: bool) -> None:
    path = resolve_path(session, segment)
    size = segment.size_bytes or 0
    if path is None:
        result.errors.append(f"segment {segment.id}: volume missing")
        return
    if not path.is_file():
        result.missing_files += 1
    elif not dry_run:
        try:
            path.unlink()
        except OSError as exc:
            result.errors.append(f"segment {segment.id}: {exc}")
            return
    if not dry_run:
        segment.state = SegmentState.DELETED.value
    result.deleted += 1
    result.bytes_reclaimed += size


def apply_age_retention(session: Session, *, settings: Settings | None = None,
                        dry_run: bool = False, limit: int = 5000) -> RetentionResult:
    settings = settings or get_settings()
    result = RetentionResult()
    now = utcnow()

    cameras = {c.id: c for c in session.execute(select(Camera)).scalars().all()}
    default_cutoff = now - dt.timedelta(days=settings.retention_default_days)

    for segment in session.execute(
            select(RecordingSegment)
            .where(RecordingSegment.state.in_((SegmentState.FINALIZED.value,
                                               SegmentState.CORRUPT.value)),
                   RecordingSegment.started_at < default_cutoff)
            .order_by(RecordingSegment.started_at).limit(limit)).scalars().all():
        result.examined += 1
        days = retention_days_for(cameras.get(segment.camera_id), settings)
        if segment.started_at >= now - dt.timedelta(days=days):
            continue      # this camera keeps footage longer than the default
        _delete_segment(session, segment, result, dry_run=dry_run)

    # Cameras with a SHORTER retention than the default need their own pass.
    for camera in cameras.values():
        days = retention_days_for(camera, settings)
        if days >= settings.retention_default_days:
            continue
        cutoff = now - dt.timedelta(days=days)
        for segment in session.execute(
                select(RecordingSegment)
                .where(RecordingSegment.camera_id == camera.id,
                       RecordingSegment.state.in_((SegmentState.FINALIZED.value,
                                                   SegmentState.CORRUPT.value)),
                       RecordingSegment.started_at < cutoff)
                .order_by(RecordingSegment.started_at).limit(limit)).scalars().all():
            result.examined += 1
            _delete_segment(session, segment, result, dry_run=dry_run)

    if result.deleted:
        log.info("retention.age_pass", deleted=result.deleted,
                 bytes=result.bytes_reclaimed, dry_run=dry_run)
    return result


def apply_space_rotation(session: Session, volume: StorageVolume, *,
                         settings: Settings | None = None, dry_run: bool = False,
                         batch: int = 200) -> RetentionResult:
    """Free space on a volume by deleting the oldest recordings first."""
    settings = settings or get_settings()
    result = RetentionResult(emergency=True)

    usage = volume_service.measure(volume.path)
    if usage is None:
        result.errors.append(f"volume {volume.name} is unreachable")
        return result

    target_percent = max(volume.min_free_percent, settings.retention_min_free_percent)
    if usage.free_percent >= target_percent:
        result.emergency = False
        return result

    needed = int(usage.total_bytes * (target_percent / 100.0)) - usage.free_bytes
    log.warning("retention.space_rotation_started", volume=volume.name,
                free_percent=usage.free_percent, target_percent=target_percent,
                needed_bytes=needed)

    while result.bytes_reclaimed < needed:
        rows = session.execute(
            select(RecordingSegment)
            .where(RecordingSegment.volume_id == volume.id,
                   RecordingSegment.state.in_((SegmentState.FINALIZED.value,
                                               SegmentState.CORRUPT.value)))
            .order_by(RecordingSegment.started_at).limit(batch)).scalars().all()
        if not rows:
            result.errors.append(
                f"volume {volume.name} is below its free-space threshold but has no "
                "deletable recordings left")
            break
        for segment in rows:
            result.examined += 1
            _delete_segment(session, segment, result, dry_run=dry_run)
            if result.bytes_reclaimed >= needed:
                break
        if dry_run:
            break

    log.warning("retention.space_rotation_finished", volume=volume.name,
                deleted=result.deleted, bytes=result.bytes_reclaimed)
    return result


def run_retention(session: Session, *, settings: Settings | None = None,
                  dry_run: bool = False) -> RetentionResult:
    settings = settings or get_settings()
    result = apply_age_retention(session, settings=settings, dry_run=dry_run)
    for volume in volume_service.writable_volumes(session):
        result.merge(apply_space_rotation(session, volume, settings=settings,
                                          dry_run=dry_run))
    return result


def purge_deleted_rows(session: Session, older_than_days: int = 365) -> int:
    """Finally remove tombstones once they are no longer useful for reporting."""
    cutoff = utcnow() - dt.timedelta(days=older_than_days)
    rows = session.execute(
        select(RecordingSegment).where(
            RecordingSegment.state == SegmentState.DELETED.value,
            RecordingSegment.started_at < cutoff)).scalars().all()
    for row in rows:
        session.delete(row)
    return len(rows)


def estimate_days_remaining(session: Session, volume: StorageVolume,
                            daily_bytes: int) -> float | None:
    if daily_bytes <= 0:
        return None
    usage = volume_service.measure(Path(volume.path))
    return round(usage.free_bytes / daily_bytes, 2) if usage else None
