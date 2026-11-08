"""Operational dashboards and downloadable reports."""

from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select
from vms_contracts.enums import (
    AlarmState,
    CameraState,
    ExportState,
    Permission,
    RecordingState,
    SegmentState,
)

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession
from app.core.rbac import accessible_camera_ids
from app.core.timeutil import utcnow
from app.db.models import (
    Alarm,
    AnalyticsEvent,
    AuditEntry,
    Camera,
    ExportJob,
    LiveSession,
    RecordingSegment,
    StorageVolume,
)
from app.health import metrics

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/dashboard", summary="Operations dashboard")
def dashboard(session: DbSession, container: Ctr, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    allowed = accessible_camera_ids(session, principal)

    def camera_filter(statement):  # noqa: ANN001
        return statement if allowed is None else statement.where(Camera.id.in_(allowed))

    states = dict(session.execute(
        camera_filter(select(Camera.state, func.count()).group_by(Camera.state))).all())
    recording_states = dict(session.execute(
        camera_filter(select(Camera.recording_state, func.count())
                      .group_by(Camera.recording_state))).all())
    total_cameras = int(session.execute(
        camera_filter(select(func.count()).select_from(Camera))).scalar_one())

    volumes = session.execute(select(StorageVolume)).scalars().all()
    total_bytes = sum(v.total_bytes or 0 for v in volumes)
    free_bytes = sum(v.free_bytes or 0 for v in volumes)

    recorded = session.execute(
        select(func.count(), func.coalesce(func.sum(RecordingSegment.size_bytes), 0))
        .where(RecordingSegment.state == SegmentState.FINALIZED.value)).one()

    return {
        "generated_at": utcnow(),
        "cameras": {
            "total": total_cameras,
            "online": int(states.get(CameraState.ONLINE.value, 0)),
            "offline": int(states.get(CameraState.OFFLINE.value, 0)),
            "unauthorized": int(states.get(CameraState.UNAUTHORIZED.value, 0)),
            "disabled": int(states.get(CameraState.DISABLED.value, 0)),
            "recording": int(recording_states.get(RecordingState.RECORDING.value, 0)),
            "recording_failed": int(
                recording_states.get(RecordingState.FAILED.value, 0)),
        },
        "storage": {
            "volumes": len(volumes), "total_bytes": total_bytes,
            "free_bytes": free_bytes,
            "used_percent": (round((1 - free_bytes / total_bytes) * 100, 1)
                             if total_bytes else None),
            "recorded_segments": int(recorded[0] or 0),
            "recorded_bytes": int(recorded[1] or 0),
        },
        "alarms": {
            "open": int(session.execute(
                select(func.count()).select_from(Alarm)
                .where(Alarm.state.in_((AlarmState.NEW.value,
                                        AlarmState.ACTIVE.value)))).scalar_one()),
            "acknowledged": int(session.execute(
                select(func.count()).select_from(Alarm)
                .where(Alarm.state == AlarmState.ACKNOWLEDGED.value)).scalar_one()),
        },
        "sessions": {
            "live": int(session.execute(
                select(func.count()).select_from(LiveSession)
                .where(LiveSession.closed_at.is_(None),
                       LiveSession.expires_at > utcnow())).scalar_one()),
            "limit": container.settings.max_live_sessions,
        },
        "exports": {
            "running": int(session.execute(
                select(func.count()).select_from(ExportJob)
                .where(ExportJob.state.in_((ExportState.QUEUED.value,
                                            ExportState.RUNNING.value)))).scalar_one()),
        },
        "system": metrics.collect().to_dict(),
        "bandwidth": metrics.bandwidth_summary(session),
        "events_pipeline": container.events.stats(),
        "license": container.entitlements.to_dto(total_cameras),
    }


@router.get("/analytics-summary", summary="Analytics statistics by source and class")
def analytics_summary(session: DbSession, principal: CurrentPrincipal,
                      since: dt.datetime | None = None) -> dict:
    principal.require(Permission.LIVE_VIEW)
    since = since or (utcnow() - dt.timedelta(days=1))
    rows = session.execute(
        select(AnalyticsEvent.source, AnalyticsEvent.object_class, func.count())
        .where(AnalyticsEvent.occurred_at >= since)
        .group_by(AnalyticsEvent.source, AnalyticsEvent.object_class)).all()
    summary: dict[str, dict[str, int]] = {}
    for source, object_class, count in rows:
        summary.setdefault(source, {})[object_class] = int(count)
    return {"since": since, "by_source": summary,
            "note": ("camera_edge = produced by the camera; server_inference = "
                     "produced by this server; external = pushed by a third-party "
                     "engine.")}


@router.get("/audit", summary="Audit activity")
def audit_report(session: DbSession, principal: CurrentPrincipal,
                 action: str | None = None, actor_id: str | None = None,
                 since: dt.datetime | None = None,
                 limit: int = Query(200, ge=1, le=1000)) -> dict:
    principal.require(Permission.AUDIT_READ)
    rows = audit.search(session, action=action, actor_id=actor_id, since=since,
                        limit=limit)
    return {"count": len(rows), "items": [{
        "at": r.at, "actor": r.actor_name or r.actor_id, "action": r.action,
        "object_type": r.object_type, "object_id": r.object_id, "outcome": r.outcome,
        "client_ip": r.client_ip, "correlation_id": r.correlation_id,
        "detail": r.detail} for r in rows]}


@router.get("/export/csv", summary="Download a report as CSV")
def export_csv(session: DbSession, principal: CurrentPrincipal,
               report: str = Query("cameras",
                                   pattern="^(cameras|alarms|audit|exports)$"),
               since: dt.datetime | None = None) -> Response:
    principal.require(Permission.LIVE_VIEW)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    since = since or (utcnow() - dt.timedelta(days=7))

    if report == "cameras":
        writer.writerow(["id", "name", "host", "state", "recording_state", "site_id",
                         "last_seen_at"])
        for camera in session.execute(select(Camera).order_by(Camera.name)).scalars():
            writer.writerow([camera.id, camera.name, camera.host, camera.state,
                             camera.recording_state, camera.site_id or "",
                             camera.last_seen_at or ""])
    elif report == "alarms":
        writer.writerow(["id", "title", "severity", "state", "camera_id",
                         "first_event_at", "acknowledged_by", "resolved_at"])
        for alarm in session.execute(
                select(Alarm).where(Alarm.first_event_at >= since)
                .order_by(Alarm.first_event_at.desc())).scalars():
            writer.writerow([alarm.id, alarm.title, alarm.severity, alarm.state,
                             alarm.camera_id or "", alarm.first_event_at,
                             alarm.acknowledged_by or "", alarm.resolved_at or ""])
    elif report == "exports":
        principal.require(Permission.EXPORT_CREATE)
        writer.writerow(["id", "camera_id", "state", "requested_start",
                         "requested_end", "size_bytes", "sha256", "requested_by"])
        for job in session.execute(
                select(ExportJob).where(ExportJob.created_at >= since)).scalars():
            writer.writerow([job.id, job.camera_id, job.state, job.requested_start,
                             job.requested_end, job.size_bytes or "", job.sha256 or "",
                             job.requested_by])
    else:
        principal.require(Permission.AUDIT_READ)
        writer.writerow(["at", "actor", "action", "object_type", "object_id",
                         "outcome", "client_ip"])
        for entry in session.execute(
                select(AuditEntry).where(AuditEntry.at >= since)
                .order_by(AuditEntry.at.desc()).limit(5000)).scalars():
            writer.writerow([entry.at, entry.actor_name or entry.actor_id,
                             entry.action, entry.object_type or "",
                             entry.object_id or "", entry.outcome,
                             entry.client_ip or ""])

    return Response(content=buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{report}-report.csv"'})
