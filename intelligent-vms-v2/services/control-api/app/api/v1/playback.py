"""Playback search, timeline and manifests."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from vms_contracts.dto import PlaybackManifest
from vms_contracts.enums import Permission, StreamRole

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip, load_camera
from app.core.errors import ValidationError
from app.core.timeutil import utcnow
from app.playback import resolver
from app.recording import gaps as gap_service
from app.recording import index

router = APIRouter(prefix="/playback", tags=["playback"])


class SyncRequest(BaseModel):
    camera_ids: list[str]
    start: dt.datetime
    end: dt.datetime


@router.post("/synchronized", summary="Synchronized multi-camera playback manifests")
def synchronized(payload: SyncRequest, session: DbSession, container: Ctr,
                 principal: CurrentPrincipal) -> dict:
    for camera_id in payload.camera_ids:
        load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
    manifests = resolver.synchronized_timeline(session, payload.camera_ids,
                                               payload.start, payload.end,
                                               settings=container.settings)
    return {"requested_from": payload.start, "requested_to": payload.end,
            "cameras": manifests}


@router.get("/{camera_id}/manifest", response_model=PlaybackManifest,
            summary="Recorded footage for a time range")
def manifest(request: Request, camera_id: str, start: dt.datetime, end: dt.datetime,
             session: DbSession, container: Ctr, principal: CurrentPrincipal,
             role: StreamRole = StreamRole.MAIN) -> PlaybackManifest:
    load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
    result = resolver.build_manifest(session, camera_id, start, end, role=role,
                                     settings=container.settings)
    audit.record(session, action=audit.Action.PLAYBACK_REQUEST,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="camera", object_id=camera_id,
                 client_ip=client_ip(request),
                 detail={"start": start.isoformat(), "end": end.isoformat(),
                         "segments": len(result.segments)})
    session.commit()
    return result


@router.get("/{camera_id}/timeline", summary="Timeline density buckets")
def timeline(camera_id: str, start: dt.datetime, end: dt.datetime, session: DbSession,
             principal: CurrentPrincipal, buckets: int = Query(96, ge=10, le=500)) -> dict:
    load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
    return {"camera_id": camera_id,
            "buckets": index.timeline_buckets(session, camera_id, start, end, buckets),
            "gaps": [g.to_dict() for g in
                     gap_service.find_gaps(session, camera_id, start, end)]}


@router.get("/{camera_id}/availability", summary="Days with recordings (calendar view)")
def availability(camera_id: str, month: str, session: DbSession,
                 principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
    try:
        start = dt.datetime.strptime(month, "%Y-%m").replace(tzinfo=dt.UTC)
    except ValueError as exc:
        raise ValidationError("month must be formatted YYYY-MM",
                              code="invalid_month") from exc

    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    days = []
    cursor = start
    while cursor < end:
        next_day = cursor + dt.timedelta(days=1)
        _, _, recorded = index.availability(session, camera_id, cursor, next_day)
        days.append({"date": cursor.date().isoformat(),
                     "recorded_s": round(recorded, 1),
                     "coverage_percent": round(min(100.0, recorded / 864.0), 1)})
        cursor = next_day
    return {"camera_id": camera_id, "month": month, "days": days}


@router.get("/{camera_id}/latest", summary="Most recent recorded moment")
def latest(camera_id: str, session: DbSession, principal: CurrentPrincipal) -> dict:
    load_camera(session, principal, camera_id, Permission.PLAYBACK_VIEW)
    segment = index.latest_segment(session, camera_id)
    if segment is None:
        return {"camera_id": camera_id, "has_recording": False,
                "message": "No recordings exist for this camera."}
    reference = segment.ended_at or segment.started_at
    return {"camera_id": camera_id, "has_recording": True,
            "latest_segment_id": segment.id, "started_at": segment.started_at,
            "ended_at": segment.ended_at, "state": segment.state,
            "age_s": round((utcnow() - reference).total_seconds(), 1)}
