"""Event-triggered recording windows.

POST-EVENT: implemented. An event creates or extends a RecordingTask with an
`ends_at` in the future; the recorder keeps writing until then.

PRE-EVENT: NOT implemented in this release, and deliberately not faked.
Capturing video from BEFORE a trigger requires a continuously running ring
buffer per camera, which changes the resource model of the recorder. The
design is in docs/architecture/pre-event-buffer.md, and
assert_pre_event_supported() raises a clear 501 if VMS_PRE_EVENT_SECONDS is
set above 0, so no operator can believe they have pre-event footage when they
do not.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.errors import NotImplementedFeatureError
from app.core.timeutil import utcnow
from app.db.models import RecordingTask
from app.logging_setup import get_logger

log = get_logger("recording.trigger")


def assert_pre_event_supported(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.pre_event_seconds and settings.pre_event_seconds > 0:
        raise NotImplementedFeatureError(
            "Pre-event recording is not implemented in this release. Set "
            "VMS_PRE_EVENT_SECONDS=0. See docs/LIMITATIONS.md.",
            code="pre_event_not_implemented")


def trigger_recording(session: Session, camera_id: str, *, seconds: int,
                      reason: str = "event", stream_role: str = "main",
                      requested_by: str | None = None) -> RecordingTask:
    """Start or extend an event-driven recording window (idempotent)."""
    ends_at = utcnow() + dt.timedelta(seconds=max(1, seconds))
    task = session.execute(
        select(RecordingTask).where(
            RecordingTask.camera_id == camera_id,
            RecordingTask.stream_role == stream_role,
            RecordingTask.reason == reason,
            RecordingTask.active.is_(True))).scalar_one_or_none()

    if task is not None:
        if task.ends_at is None or task.ends_at < ends_at:
            task.ends_at = ends_at
            task.version += 1
        log.info("recording.trigger_extended", camera_id=camera_id, reason=reason,
                 ends_at=ends_at.isoformat())
        return task

    task = RecordingTask(camera_id=camera_id, stream_role=stream_role, reason=reason,
                         active=True, starts_at=utcnow(), ends_at=ends_at,
                         requested_by=requested_by)
    session.add(task)
    session.flush()
    log.info("recording.trigger_started", camera_id=camera_id, reason=reason,
             ends_at=ends_at.isoformat())
    return task


def stop_trigger(session: Session, camera_id: str, *, reason: str = "event",
                 stream_role: str = "main") -> bool:
    task = session.execute(
        select(RecordingTask).where(
            RecordingTask.camera_id == camera_id,
            RecordingTask.stream_role == stream_role,
            RecordingTask.reason == reason,
            RecordingTask.active.is_(True))).scalar_one_or_none()
    if task is None:
        return False
    task.active = False
    task.ends_at = utcnow()
    task.version += 1
    return True


def expire_finished_tasks(session: Session) -> int:
    rows = session.execute(
        select(RecordingTask).where(RecordingTask.active.is_(True),
                                    RecordingTask.ends_at.is_not(None),
                                    RecordingTask.ends_at <= utcnow())).scalars().all()
    for task in rows:
        task.active = False
        task.version += 1
    return len(rows)
