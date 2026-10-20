"""Recording control: manual recording, schedules, profiles, continuity."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from vms_contracts.enums import Permission, RecordingMode

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip, load_camera
from app.core.errors import ValidationError
from app.core.timeutil import utcnow
from app.db.models import RecordingProfile, RecordingSchedule, RecordingTask
from app.recording import gaps as gap_service
from app.recording import health, index, planner, triggers

router = APIRouter(prefix="/recordings", tags=["recordings"])


class ManualRecording(BaseModel):
    seconds: int = Field(default=300, ge=10, le=7200)
    stream_role: str = "main"


class ScheduleBlockIn(BaseModel):
    day: int = Field(ge=0, le=6)
    start: str
    end: str
    mode: RecordingMode = RecordingMode.CONTINUOUS


class ScheduleIn(BaseModel):
    enabled: bool = True
    mode: RecordingMode = RecordingMode.CONTINUOUS
    timezone: str = "UTC"
    blocks: list[ScheduleBlockIn] = []


class ProfileIn(BaseModel):
    name: str
    stream_role: str = "main"
    segment_seconds: int = Field(default=300, ge=10, le=3600)
    retention_days: int = Field(default=14, ge=1, le=3650)
    record_audio: bool = True
    post_event_seconds: int = Field(default=30, ge=0, le=600)
    transcode: bool = False
    description: str | None = None


@router.get("/profiles", summary="Recording profiles")
def list_profiles(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.PLAYBACK_VIEW)
    rows = session.execute(
        select(RecordingProfile).order_by(RecordingProfile.name)).scalars().all()
    return {"items": [{"id": p.id, "name": p.name, "stream_role": p.stream_role,
                       "segment_seconds": p.segment_seconds,
                       "retention_days": p.retention_days,
                       "record_audio": p.record_audio,
                       "post_event_seconds": p.post_event_seconds,
                       "transcode": p.transcode, "description": p.description}
                      for p in rows]}


@router.post("/profiles", status_code=201, summary="Create a recording profile")
def create_profile(request: Request, payload: ProfileIn, session: DbSession,
                   principal: CurrentPrincipal) -> dict:
    principal.require(Permission.SCHEDULE_MANAGE)
    profile = RecordingProfile(**payload.model_dump())
    session.add(profile)
    session.flush()
    audit.record(session, action=audit.Action.SCHEDULE_UPDATE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="recording_profile", object_id=profile.id,
                 client_ip=client_ip(request), detail={"name": payload.name})
    session.commit()
    return {"id": profile.id, "name": profile.name}


@router.get("/health/summary", summary="Fleet recording health")
def recording_health(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.PLAYBACK_VIEW)
    stalled = health.stalled_recorders(session)
    return {"summary": health.summarize(session),
            "stalled_workers": [{"camera_id": t.camera_id, "worker": t.worker_id,
                                 "heartbeat_at": t.heartbeat_at} for t in stalled],
            "checked_at": utcnow()}


@router.post("/{camera_id}/start", summary="Start manual recording")
def start_manual(request: Request, camera_id: str, payload: ManualRecording,
                 session: DbSession, principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.RECORDING_MANUAL)
    task = triggers.trigger_recording(session, camera_id, seconds=payload.seconds,
                                      reason="manual",
                                      stream_role=payload.stream_role,
                                      requested_by=principal.user_id)
    audit.record(session, action=audit.Action.RECORDING_START_MANUAL,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="camera", object_id=camera_id,
                 client_ip=client_ip(request), detail={"seconds": payload.seconds})
    session.commit()
    return {"camera_id": camera_id, "task_id": task.id, "ends_at": task.ends_at}


@router.post("/{camera_id}/stop", summary="Stop manual recording")
def stop_manual(request: Request, camera_id: str, session: DbSession,
                principal: CurrentPrincipal, stream_role: str = "main") -> dict:
    load_camera(session, principal, camera_id, Permission.RECORDING_MANUAL)
    stopped = triggers.stop_trigger(session, camera_id, reason="manual",
                                    stream_role=stream_role)
    audit.record(session, action=audit.Action.RECORDING_STOP_MANUAL,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="camera", object_id=camera_id,
                 client_ip=client_ip(request))
    session.commit()
    return {"camera_id": camera_id, "stopped": stopped}


@router.get("/{camera_id}/status", summary="Recording status for a camera")
def status(camera_id: str, session: DbSession, principal: CurrentPrincipal) -> dict:
    camera = load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
    tasks = session.execute(
        select(RecordingTask).where(RecordingTask.camera_id == camera_id,
                                    RecordingTask.active.is_(True))).scalars().all()
    latest = index.latest_segment(session, camera_id)
    return {
        "camera_id": camera_id, "recording_mode": camera.recording_mode,
        "recording_state": camera.recording_state, "detail": camera.recording_detail,
        "active_tasks": [{"id": t.id, "reason": t.reason, "ends_at": t.ends_at,
                          "worker": t.worker_id, "state": t.state} for t in tasks],
        "latest_segment": None if latest is None else {
            "id": latest.id, "started_at": latest.started_at,
            "ended_at": latest.ended_at, "state": latest.state,
            "size_bytes": latest.size_bytes},
        "storage": index.storage_summary(session, camera_id),
    }


@router.get("/{camera_id}/schedule", summary="Recording schedule")
def get_schedule(camera_id: str, session: DbSession,
                 principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
    schedule = session.execute(
        select(RecordingSchedule)
        .where(RecordingSchedule.camera_id == camera_id)).scalar_one_or_none()
    if schedule is None:
        return {"camera_id": camera_id, "enabled": False, "blocks": [],
                "message": "No schedule configured; the camera's recording mode applies."}
    blocks = planner.parse_blocks(schedule.blocks)
    active, mode = planner.should_record(blocks, schedule.timezone)
    return {"camera_id": camera_id, "enabled": schedule.enabled, "mode": schedule.mode,
            "timezone": schedule.timezone, "blocks": schedule.blocks,
            "coverage_hours_per_week": planner.coverage_hours(blocks),
            "recording_now": active, "current_mode": mode.value if mode else None,
            "next_transition": planner.next_transition(blocks, schedule.timezone)}


@router.put("/{camera_id}/schedule", summary="Replace the recording schedule")
def put_schedule(request: Request, camera_id: str, payload: ScheduleIn,
                 session: DbSession, principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.SCHEDULE_MANAGE)
    raw_blocks = [b.model_dump(mode="json") for b in payload.blocks]
    try:
        planner.parse_blocks(raw_blocks)
    except ValueError as exc:
        raise ValidationError(str(exc), code="invalid_schedule") from exc

    schedule = session.execute(
        select(RecordingSchedule)
        .where(RecordingSchedule.camera_id == camera_id)).scalar_one_or_none()
    if schedule is None:
        schedule = RecordingSchedule(camera_id=camera_id, name="default")
        session.add(schedule)
    schedule.enabled = payload.enabled
    schedule.mode = payload.mode.value
    schedule.timezone = payload.timezone
    schedule.blocks = raw_blocks

    audit.record(session, action=audit.Action.SCHEDULE_UPDATE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="camera", object_id=camera_id,
                 client_ip=client_ip(request), detail={"blocks": len(raw_blocks)})
    session.commit()
    return {"camera_id": camera_id, "blocks": raw_blocks, "enabled": payload.enabled}


@router.get("/{camera_id}/continuity", summary="Recording continuity report")
def continuity(camera_id: str, start: dt.datetime, end: dt.datetime,
               session: DbSession, container: Ctr,
               principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
    tolerance = container.settings.gap_tolerance_s
    found = gap_service.find_gaps(session, camera_id, start, end,
                                  tolerance_s=tolerance)
    return {"camera_id": camera_id, "from": start, "to": end,
            "continuity_percent": gap_service.continuity_percent(
                session, camera_id, start, end, tolerance_s=tolerance),
            "gap_count": len(found),
            "total_gap_seconds": round(sum(g.duration_s for g in found), 1),
            "gaps": [g.to_dict() for g in found[:200]]}
