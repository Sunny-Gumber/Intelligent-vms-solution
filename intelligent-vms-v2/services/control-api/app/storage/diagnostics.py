"""Storage diagnostics: answer "why did recording stop?" quickly."""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from vms_contracts.enums import SegmentState, StorageState

from app.db.models import Camera, RecordingSegment, StorageVolume
from app.storage import volumes as volume_service


@dataclass
class VolumeDiagnostic:
    volume_id: str
    name: str
    path: str
    state: str
    exists: bool
    writable: bool
    write_latency_ms: float | None
    free_percent: float | None
    segment_count: int
    indexed_bytes: int
    missing_files_sampled: int
    sampled: int
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def measure_write_latency(path: Path, payload_bytes: int = 1024 * 256) -> float | None:
    probe = path / f".vms-latency-{os.getpid()}"
    try:
        started = time.perf_counter()
        with open(probe, "wb") as handle:
            handle.write(b"\0" * payload_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        return round((time.perf_counter() - started) * 1000, 2)
    except OSError:
        return None
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def diagnose_volume(session: Session, volume: StorageVolume, *,
                    sample_size: int = 25) -> VolumeDiagnostic:
    path = Path(volume.path)
    exists = path.is_dir()
    writable = exists and os.access(path, os.W_OK)
    usage = volume_service.measure(path) if exists else None

    counts = session.execute(
        select(func.count(), func.coalesce(func.sum(RecordingSegment.size_bytes), 0))
        .where(RecordingSegment.volume_id == volume.id,
               RecordingSegment.state == SegmentState.FINALIZED.value)).one()

    sample = session.execute(
        select(RecordingSegment)
        .where(RecordingSegment.volume_id == volume.id,
               RecordingSegment.state == SegmentState.FINALIZED.value)
        .order_by(RecordingSegment.started_at.desc()).limit(sample_size)).scalars().all()

    missing = sum(1 for s in sample if not (path / s.relative_path).is_file())

    problems: list[str] = []
    if not exists:
        problems.append("storage path does not exist")
    if exists and not writable:
        problems.append("storage path is not writable by the service account")
    if usage and usage.free_percent < volume.min_free_percent:
        problems.append(f"free space {usage.free_percent}% is below the "
                        f"{volume.min_free_percent}% threshold")
    if missing:
        problems.append(f"{missing} of {len(sample)} sampled segments are missing on disk")
    if volume.state in (StorageState.OFFLINE.value, StorageState.READONLY.value):
        problems.append(f"volume state is {volume.state}")

    return VolumeDiagnostic(
        volume_id=volume.id, name=volume.name, path=str(path), state=volume.state,
        exists=exists, writable=writable,
        write_latency_ms=measure_write_latency(path) if writable else None,
        free_percent=usage.free_percent if usage else None,
        segment_count=int(counts[0] or 0), indexed_bytes=int(counts[1] or 0),
        missing_files_sampled=missing, sampled=len(sample), problems=problems)


def diagnose_all(session: Session) -> list[VolumeDiagnostic]:
    return [diagnose_volume(session, v)
            for v in session.execute(select(StorageVolume)).scalars().all()]


def cameras_without_storage(session: Session) -> list[str]:
    """Cameras with no usable volume at all (no failover target exists)."""
    if volume_service.writable_volumes(session):
        return []
    return [c.id for c in session.execute(
        select(Camera).where(Camera.enabled.is_(True))).scalars()]
