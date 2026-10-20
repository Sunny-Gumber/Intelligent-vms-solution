"""Segment path allocation and storage failover.

Layout on disk (stable, human-navigable, safe to back up with rsync):

    <volume>/recordings/<camera_id>/<YYYY>/<MM>/<DD>/<role>/<epoch>_<id>.mp4

Properties:
  * Date directories keep any single directory small, which matters on
    ext4/NTFS when a camera records continuously for months.
  * The camera id (not its name) is used, so renaming never orphans
    recordings.
  * Every path goes through core.paths.resolve_within, so a crafted camera id
    can never escape the volume root.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.paths import resolve_within, safe_name
from app.core.timeutil import ensure_utc
from app.db.models import Camera, StorageVolume
from app.logging_setup import get_logger
from app.storage import volumes as volume_service

log = get_logger("storage.allocator")

RECORDINGS_SUBDIR = "recordings"


@dataclass(frozen=True)
class Allocation:
    volume: StorageVolume
    absolute_path: Path
    relative_path: str
    temp_path: Path

    @property
    def volume_id(self) -> str:
        return self.volume.id


def build_relative_path(camera_id: str, role: str, start: dt.datetime, segment_id: str,
                        extension: str = "mp4") -> str:
    start = ensure_utc(start)
    safe_name(camera_id)
    safe_name(segment_id)
    role_clean = safe_name(role)
    return (f"{RECORDINGS_SUBDIR}/{camera_id}/{start:%Y}/{start:%m}/{start:%d}/"
            f"{role_clean}/{int(start.timestamp())}_{segment_id}.{extension}")


def allocate(session: Session, camera: Camera, *, role: str, start: dt.datetime,
             segment_id: str, extension: str = "mp4",
             required_bytes: int | None = None) -> Allocation:
    """Choose a volume and build the segment path, creating directories.

    Raises RuntimeError when no volume can accept the write; the caller turns
    that into a `recording.failed` event rather than losing video silently.
    """
    volume = volume_service.volume_for_camera(session, camera)
    if volume is None:
        raise RuntimeError("no writable storage volume is available")

    if required_bytes and volume.free_bytes is not None \
            and volume.free_bytes < required_bytes:
        for candidate in volume_service.writable_volumes(session):
            if candidate.free_bytes is None or candidate.free_bytes >= required_bytes:
                volume = candidate
                break
        else:
            raise RuntimeError(f"no volume has the required {required_bytes} bytes free")

    relative = build_relative_path(camera.id, role, start, segment_id, extension)
    absolute = resolve_within(Path(volume.path), relative)
    absolute.parent.mkdir(parents=True, exist_ok=True)
    return Allocation(volume=volume, absolute_path=absolute, relative_path=relative,
                      temp_path=absolute.with_suffix(absolute.suffix + ".part"))


def resolve_segment_path(volume: StorageVolume, relative_path: str) -> Path:
    """Resolve a stored relative path back to disk, with confinement."""
    return resolve_within(Path(volume.path), relative_path)
