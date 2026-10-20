"""Recording health: never lose video silently.

Product rule M4-R12: a recording failure must always become a visible,
queryable event. This module owns Camera.recording_state transitions and emits
the matching events.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session
from vms_contracts.enums import EventSeverity, EventType, RecordingMode, RecordingState

from app.core.timeutil import utcnow
from app.db.models import Camera, RecordingTask
from app.logging_setup import get_logger

log = get_logger("recording.health")


def set_state(session: Session, camera: Camera, state: RecordingState,
              detail: str | None = None) -> list[dict]:
    """Transition recording state, returning events to publish."""
    previous = camera.recording_state
    camera.recording_state = state.value
    camera.recording_detail = detail
    if previous == state.value:
        return []

    events: list[dict] = []
    if state is RecordingState.RECORDING:
        events.append(_event(EventType.RECORDING_STARTED, EventSeverity.INFO,
                             camera, detail))
    elif state is RecordingState.FAILED:
        events.append(_event(EventType.RECORDING_FAILED, EventSeverity.HIGH,
                             camera, detail))
    elif state is RecordingState.IDLE and previous in (RecordingState.RECORDING.value,
                                                       RecordingState.RETRYING.value):
        events.append(_event(EventType.RECORDING_STOPPED, EventSeverity.INFO,
                             camera, detail))

    log.info("recording.state", camera_id=camera.id, previous=previous,
             state=state.value, detail=detail)
    return events


def _event(event_type: EventType, severity: EventSeverity, camera: Camera,
           detail: str | None) -> dict:
    return {"type": event_type, "severity": severity, "camera_id": camera.id,
            "site_id": camera.site_id,
            "payload": {"camera": camera.name, "detail": detail}}


def stalled_recorders(session: Session,
                      *, stale_after_s: int = 120) -> list[RecordingTask]:
    """Active tasks whose worker heartbeat has stopped."""
    cutoff = utcnow() - dt.timedelta(seconds=stale_after_s)
    return list(session.execute(
        select(RecordingTask).where(RecordingTask.active.is_(True),
                                    RecordingTask.worker_id.is_not(None),
                                    RecordingTask.heartbeat_at < cutoff)).scalars().all())


def expected_recording_cameras(session: Session) -> list[Camera]:
    return list(session.execute(
        select(Camera).where(
            Camera.enabled.is_(True),
            Camera.recording_mode.in_((RecordingMode.CONTINUOUS.value,
                                       RecordingMode.SCHEDULED.value)))).scalars().all())


def summarize(session: Session) -> dict[str, int]:
    cameras = list(session.execute(select(Camera)).scalars().all())
    return {"total": len(cameras),
            "recording": sum(1 for c in cameras
                             if c.recording_state == RecordingState.RECORDING.value),
            "retrying": sum(1 for c in cameras
                            if c.recording_state == RecordingState.RETRYING.value),
            "failed": sum(1 for c in cameras
                          if c.recording_state == RecordingState.FAILED.value),
            "idle": sum(1 for c in cameras
                        if c.recording_state == RecordingState.IDLE.value)}
