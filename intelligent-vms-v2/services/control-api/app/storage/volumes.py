"""Storage volume management, capacity monitoring and health.

A VMS fails in the field in two storage-shaped ways: the disk silently fills
up, or a volume disappears and recording stops unnoticed. Both are handled:

  * refresh_volume() measures real free space and transitions the volume
    through online -> degraded -> full -> offline, emitting an event on every
    transition.
  * writable_volumes() is the single source of truth the recorder consults
    before starting a segment. A volume below its threshold is simply not
    offered, so the recorder fails over instead of filling the disk.

Network shares (SMB/NFS) are supported as MOUNTED PATHS. Mount management is
the operating system's job; this product monitors the mount point but does not
mount it.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vms_contracts.enums import EventSeverity, EventType, StorageState, VolumeKind

from app.config import Settings, get_settings
from app.core.paths import PathConfinementError, ensure_writable_dir
from app.core.timeutil import utcnow
from app.db.models import Camera, StorageVolume
from app.logging_setup import get_logger

log = get_logger("storage")


@dataclass(frozen=True)
class VolumeUsage:
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def free_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return round(self.free_bytes / self.total_bytes * 100.0, 2)


def measure(path: str | Path) -> VolumeUsage | None:
    try:
        usage = shutil.disk_usage(str(path))
    except OSError as exc:
        log.warning("storage.measure_failed", path=str(path), error=str(exc))
        return None
    return VolumeUsage(total_bytes=usage.total, used_bytes=usage.used,
                       free_bytes=usage.free)


def create_volume(session: Session, *, name: str, path: str,
                  kind: VolumeKind = VolumeKind.LOCAL, min_free_percent: float = 10.0,
                  priority: int = 100, quota_bytes: int | None = None) -> StorageVolume:
    """Register a volume after proving the path is usable.

    The write test is deliberate: a path that exists but is read-only (a common
    result of a failed NFS remount) must be rejected at configuration time, not
    discovered at 3 a.m. when recording stops.
    """
    target = Path(path)
    try:
        ensure_writable_dir(target)
    except PathConfinementError as exc:
        raise ValueError(f"storage path is not usable: {exc}") from exc

    volume = StorageVolume(name=name, path=str(target.resolve()), kind=kind.value,
                           min_free_percent=min_free_percent, priority=priority,
                           quota_bytes=quota_bytes)
    session.add(volume)
    session.flush()
    refresh_volume(session, volume)
    return volume


def refresh_volume(session: Session, volume: StorageVolume,
                   settings: Settings | None = None) -> tuple[StorageState, list[dict]]:
    """Update usage and state. Returns (state, events_to_raise)."""
    settings = settings or get_settings()
    previous = volume.state
    events: list[dict] = []

    usage = measure(volume.path)
    volume.last_checked_at = utcnow()

    if usage is None:
        volume.state = StorageState.OFFLINE.value
        volume.state_detail = "path is not reachable"
        volume.total_bytes = volume.free_bytes = volume.used_bytes = None
    else:
        volume.total_bytes = usage.total_bytes
        volume.free_bytes = usage.free_bytes
        volume.used_bytes = usage.used_bytes
        free_percent = usage.free_percent

        if not os.access(volume.path, os.W_OK):
            volume.state = StorageState.READONLY.value
            volume.state_detail = "filesystem is not writable"
        elif free_percent <= settings.storage_critical_free_percent:
            volume.state = StorageState.FULL.value
            volume.state_detail = f"only {free_percent}% free"
        elif free_percent <= max(volume.min_free_percent,
                                 settings.storage_warn_free_percent):
            volume.state = StorageState.DEGRADED.value
            volume.state_detail = f"{free_percent}% free is below the configured threshold"
        else:
            volume.state = StorageState.ONLINE.value
            volume.state_detail = None

    if volume.state != previous:
        severity, event_type = _state_event(volume.state)
        events.append({"type": event_type, "severity": severity,
                       "payload": {"volume_id": volume.id, "volume": volume.name,
                                   "state": volume.state, "previous_state": previous,
                                   "detail": volume.state_detail,
                                   "free_bytes": volume.free_bytes,
                                   "total_bytes": volume.total_bytes}})
        log.warning("storage.state_changed", volume=volume.name, previous=previous,
                    state=volume.state, detail=volume.state_detail)

    return StorageState(volume.state), events


def _state_event(state: str) -> tuple[EventSeverity, EventType]:
    return {
        StorageState.ONLINE.value: (EventSeverity.INFO, EventType.SYSTEM_HEALTH),
        StorageState.DEGRADED.value: (EventSeverity.MEDIUM, EventType.STORAGE_WARNING),
        StorageState.FULL.value: (EventSeverity.HIGH, EventType.STORAGE_CRITICAL),
        StorageState.READONLY.value: (EventSeverity.HIGH, EventType.STORAGE_FAILED),
        StorageState.OFFLINE.value: (EventSeverity.CRITICAL, EventType.STORAGE_FAILED),
    }.get(state, (EventSeverity.MEDIUM, EventType.STORAGE_WARNING))


def refresh_all(session: Session, settings: Settings | None = None) -> list[dict]:
    events: list[dict] = []
    for volume in session.execute(select(StorageVolume)).scalars().all():
        _, volume_events = refresh_volume(session, volume, settings)
        events.extend(volume_events)
    return events


def writable_volumes(session: Session) -> list[StorageVolume]:
    """Volumes the recorder may write to right now, best first."""
    volumes = session.execute(
        select(StorageVolume).where(StorageVolume.enabled.is_(True))).scalars().all()
    usable = [v for v in volumes
              if v.state in (StorageState.ONLINE.value, StorageState.DEGRADED.value)]
    return sorted(usable, key=lambda v: (v.priority, -(v.free_bytes or 0)))


def volume_for_camera(session: Session, camera: Camera) -> StorageVolume | None:
    """Resolve the volume a camera should record to, with failover.

    Returns None when nothing is writable; the recorder reports that as a
    storage failure instead of silently dropping video.
    """
    if camera.storage_volume_id:
        assigned = session.get(StorageVolume, camera.storage_volume_id)
        if assigned and assigned.enabled and assigned.state in (
                StorageState.ONLINE.value, StorageState.DEGRADED.value):
            return assigned
        if assigned:
            log.warning("storage.failover", camera_id=camera.id, volume=assigned.name,
                        state=assigned.state)
    candidates = writable_volumes(session)
    return candidates[0] if candidates else None


def camera_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Camera.storage_volume_id, func.count())
        .where(Camera.storage_volume_id.is_not(None))
        .group_by(Camera.storage_volume_id)).all()
    return {volume_id: int(count) for volume_id, count in rows}
