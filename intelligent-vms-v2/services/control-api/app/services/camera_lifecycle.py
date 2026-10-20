"""Camera lifecycle: create, update, replace, delete — consistently.

Deleting a camera touches many subsystems; doing it in the wrong order is how
a VMS ends up with orphaned recorders, stuck gateway paths and unplayable
evidence. The ordered sequence implemented here:

  1. Disable the camera so no scheduler picks it up again.
  2. Stop recording tasks so the recorder finalizes its current segment.
  3. Close live sessions and release the media gateway path.
  4. Release any PTZ lock (stopping movement first).
  5. Detach recordings: segments KEEP their camera_id as a plain value, so
     evidence survives and remains playable and exportable.
  6. Delete streams, credentials, schedules, presets, zones (cascade).
  7. Write the audit record.

Replacement (swapping failed hardware) preserves the camera id, its
recordings, permissions and layout placement, and only re-probes the device.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from vms_contracts.enums import CameraState, RecordingState

from app.core.audit import Action, record as audit_record
from app.core.errors import ConflictError, NotFoundError
from app.core.timeutil import utcnow
from app.db.models import (
    Camera,
    CameraStream,
    PtzLock,
    RecordingSchedule,
    RecordingSegment,
    RecordingTask,
)
from app.logging_setup import get_logger

log = get_logger("camera.lifecycle")


@dataclass
class DeletionReport:
    camera_id: str
    recording_tasks_stopped: int = 0
    live_sessions_closed: int = 0
    ptz_released: bool = False
    segments_preserved: int = 0


async def delete_camera(session: Session, camera_id: str, *, actor_id: str,
                        actor_name: str | None, session_broker=None,  # noqa: ANN001
                        ptz_service=None, client_ip: str | None = None,
                        keep_recordings: bool = True) -> DeletionReport:
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise NotFoundError("Camera not found", code="camera_not_found")

    report = DeletionReport(camera_id=camera_id)

    # 1. stop the scheduler restarting anything
    camera.enabled = False
    camera.state = CameraState.DISABLED.value
    session.flush()

    # 2. stop recording tasks
    result = session.execute(
        update(RecordingTask)
        .where(RecordingTask.camera_id == camera_id, RecordingTask.active.is_(True))
        .values(active=False, ends_at=utcnow(), state=RecordingState.STOPPING.value))
    report.recording_tasks_stopped = int(result.rowcount or 0)

    # 3. live sessions + gateway path
    if session_broker is not None:
        report.live_sessions_closed = await session_broker.close_for_camera(
            session, camera_id, reason="camera_deleted")

    # 4. PTZ
    lock = session.get(PtzLock, camera_id)
    if lock is not None:
        if ptz_service is not None:
            try:
                await ptz_service.stop_cameras(session, [camera_id])
            except Exception as exc:  # noqa: BLE001
                log.warning("camera.delete_ptz_stop_failed", camera_id=camera_id,
                            error=str(exc))
        lock = session.get(PtzLock, camera_id)
        if lock is not None:
            session.delete(lock)
        report.ptz_released = True

    # 5. recordings are preserved by design
    report.segments_preserved = int(session.execute(
        select(func.count()).select_from(RecordingSegment)
        .where(RecordingSegment.camera_id == camera_id)).scalar_one())

    # 6. delete configuration rows (cascade handles the children)
    session.delete(camera)
    session.flush()

    audit_record(session, action=Action.CAMERA_DELETE, actor_id=actor_id,
                 actor_name=actor_name, object_type="camera", object_id=camera_id,
                 client_ip=client_ip,
                 detail={"recordings_preserved": report.segments_preserved,
                         "keep_recordings": keep_recordings,
                         "live_sessions_closed": report.live_sessions_closed})
    log.info("camera.deleted", camera_id=camera_id,
             segments_preserved=report.segments_preserved)
    return report


async def replace_camera(session: Session, camera_id: str, *, host: str,
                         port: int | None, username: str | None, password: str | None,
                         actor_id: str, actor_name: str | None, prober,  # noqa: ANN001
                         credential_store, session_broker=None,  # noqa: ANN001
                         client_ip: str | None = None) -> Camera:
    """Point an existing camera record at replacement hardware."""
    from app.devices.probe import apply_probe

    camera = session.get(Camera, camera_id)
    if camera is None:
        raise NotFoundError("Camera not found", code="camera_not_found")

    previous = {"host": camera.host, "port": camera.port, "serial": camera.serial}

    if session_broker is not None:
        await session_broker.close_for_camera(session, camera_id,
                                              reason="camera_replaced")

    camera.host = host
    if port:
        camera.port = port
    if username is not None or password is not None:
        existing_username = camera.credential.username if camera.credential else None
        credential_store.set(session, camera, username or existing_username, password)
    camera.serial = None
    camera.mac = None
    camera.capabilities = {}
    camera.state = CameraState.PROVISIONING.value
    camera.consecutive_failures = 0
    session.flush()

    outcome = await prober.probe(camera)
    apply_probe(session, camera, outcome, credential_store)
    if not outcome.ok:
        raise ConflictError(
            f"Replacement device could not be validated: {outcome.error}",
            code=outcome.error_code or "replacement_probe_failed")

    audit_record(session, action=Action.CAMERA_REPLACE, actor_id=actor_id,
                 actor_name=actor_name, object_type="camera", object_id=camera_id,
                 client_ip=client_ip,
                 detail={"previous": previous, "new_host": host,
                         "new_serial": camera.serial})
    log.info("camera.replaced", camera_id=camera_id, host=host)
    return camera


def set_enabled(session: Session, camera: Camera, enabled: bool) -> None:
    camera.enabled = enabled
    if not enabled:
        camera.state = CameraState.DISABLED.value
        session.execute(
            update(RecordingTask)
            .where(RecordingTask.camera_id == camera.id, RecordingTask.active.is_(True))
            .values(active=False, ends_at=utcnow()))
    elif camera.state == CameraState.DISABLED.value:
        camera.state = CameraState.PROVISIONING.value


def ensure_default_schedule(session: Session, camera: Camera,
                            timezone: str = "UTC") -> None:
    existing = session.execute(
        select(RecordingSchedule)
        .where(RecordingSchedule.camera_id == camera.id)).scalar_one_or_none()
    if existing is None:
        session.add(RecordingSchedule(camera_id=camera.id, name="default", enabled=True,
                                      mode=camera.recording_mode, blocks=[],
                                      timezone=timezone))


def primary_stream(camera: Camera) -> CameraStream | None:
    for role in ("main", "sub", "third"):
        for stream in camera.streams:
            if stream.role == role and stream.enabled:
                return stream
    return None
