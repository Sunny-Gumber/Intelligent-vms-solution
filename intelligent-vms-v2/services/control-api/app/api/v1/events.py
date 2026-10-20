"""Events, alarms and rules."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from vms_contracts.dto import AlarmAck, AlarmOut, EventOut
from vms_contracts.enums import AlarmState, EventSeverity, EventType, Permission

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip
from app.core.errors import NotFoundError
from app.core.rbac import accessible_camera_ids
from app.db.models import Alarm, Event
from app.events_engine import alarms as alarm_service
from app.events_engine import rules as rule_service
from app.events_engine.ingest import IncomingEvent

router = APIRouter(tags=["events"])


def alarm_dto(alarm: Alarm) -> AlarmOut:
    return AlarmOut(
        id=alarm.id, rule_id=alarm.rule_id, state=AlarmState(alarm.state),
        severity=EventSeverity(alarm.severity), camera_id=alarm.camera_id,
        site_id=alarm.site_id, title=alarm.title, first_event_at=alarm.first_event_at,
        last_event_at=alarm.last_event_at, event_count=alarm.event_count,
        assigned_to=alarm.assigned_to, acknowledged_by=alarm.acknowledged_by,
        acknowledged_at=alarm.acknowledged_at, resolved_at=alarm.resolved_at,
        closed_at=alarm.closed_at, note=alarm.note, version=alarm.version,
        escalation_level=alarm.escalation_level)


class ExternalEvent(BaseModel):
    type: EventType
    severity: EventSeverity = EventSeverity.MEDIUM
    camera_id: str | None = None
    occurred_at: dt.datetime | None = None
    payload: dict = Field(default_factory=dict)


class AssignRequest(BaseModel):
    assignee_id: str
    version: int


class RuleIn(BaseModel):
    name: str
    event_types: list[str]
    severity: EventSeverity = EventSeverity.MEDIUM
    enabled: bool = True
    camera_ids: list[str] = []
    site_ids: list[str] = []
    conditions: dict = Field(default_factory=dict)
    raise_alarm: bool = True
    start_recording: bool = False
    record_seconds: int = 60
    capture_snapshot: bool = False
    notify_channels: list[str] = []
    suppress_seconds: int = 30
    escalate_after_s: int | None = None
    escalate_channels: list[str] = []
    schedule: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------
@router.get("/events", summary="Search events")
def search_events(session: DbSession, principal: CurrentPrincipal,
                  type: str | None = None, severity: str | None = None,
                  camera_id: str | None = None, since: dt.datetime | None = None,
                  until: dt.datetime | None = None,
                  limit: int = Query(100, ge=1, le=500),
                  offset: int = Query(0, ge=0)) -> dict:
    principal.require(Permission.LIVE_VIEW)
    allowed = accessible_camera_ids(session, principal)

    def apply(statement):  # noqa: ANN001
        if allowed is not None:
            statement = statement.where(Event.camera_id.is_(None)
                                        | Event.camera_id.in_(allowed))
        if type:
            statement = statement.where(Event.type == type)
        if severity:
            statement = statement.where(Event.severity == severity)
        if camera_id:
            statement = statement.where(Event.camera_id == camera_id)
        if since:
            statement = statement.where(Event.occurred_at >= since)
        if until:
            statement = statement.where(Event.occurred_at <= until)
        return statement

    total = int(session.execute(
        apply(select(func.count()).select_from(Event))).scalar_one())
    rows = session.execute(
        apply(select(Event)).order_by(Event.occurred_at.desc())
        .limit(limit).offset(offset)).scalars().all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [EventOut(id=e.id, type=EventType(e.type),
                               severity=EventSeverity(e.severity),
                               camera_id=e.camera_id, site_id=e.site_id,
                               source=e.source, occurred_at=e.occurred_at,
                               received_at=e.received_at, payload=e.payload,
                               dedupe_key=e.dedupe_key, snapshot_id=e.snapshot_id)
                      for e in rows]}


@router.post("/events", status_code=202, summary="Submit an event from an integration")
async def submit_event(payload: ExternalEvent, container: Ctr,
                       principal: CurrentPrincipal) -> dict:
    principal.require(Permission.RULE_MANAGE)
    accepted = await container.events.submit(IncomingEvent(
        type=payload.type, severity=payload.severity, camera_id=payload.camera_id,
        occurred_at=payload.occurred_at, source="external", payload=payload.payload))
    return {"accepted": accepted, "queue": container.events.stats()}


# --------------------------------------------------------------------------
# Alarms
# --------------------------------------------------------------------------
@router.get("/alarms", summary="Search alarms")
def search_alarms(session: DbSession, principal: CurrentPrincipal,
                  state: list[str] | None = Query(None),
                  severity: list[str] | None = Query(None),
                  assigned_to: str | None = None, since: dt.datetime | None = None,
                  until: dt.datetime | None = None,
                  limit: int = Query(100, ge=1, le=500),
                  offset: int = Query(0, ge=0)) -> dict:
    principal.require(Permission.LIVE_VIEW)
    allowed = accessible_camera_ids(session, principal)
    rows, total = alarm_service.search(session, states=state, severities=severity,
                                       camera_ids=allowed, since=since, until=until,
                                       assigned_to=assigned_to, limit=limit,
                                       offset=offset)
    return {"total": total, "limit": limit, "offset": offset,
            "items": [alarm_dto(a) for a in rows]}


@router.get("/alarms/{alarm_id}", response_model=AlarmOut, summary="Alarm detail")
def get_alarm(alarm_id: str, session: DbSession,
              principal: CurrentPrincipal) -> AlarmOut:
    principal.require(Permission.LIVE_VIEW)
    alarm = session.get(Alarm, alarm_id)
    if alarm is None:
        raise NotFoundError("Alarm not found", code="alarm_not_found")
    return alarm_dto(alarm)


@router.get("/alarms/{alarm_id}/events", summary="Events that formed this alarm")
def alarm_events(alarm_id: str, session: DbSession,
                 principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    rows = session.execute(
        select(Event).where(Event.alarm_id == alarm_id)
        .order_by(Event.occurred_at)).scalars().all()
    return {"alarm_id": alarm_id, "count": len(rows),
            "items": [{"id": e.id, "type": e.type, "occurred_at": e.occurred_at,
                       "payload": e.payload} for e in rows]}


@router.post("/alarms/{alarm_id}/acknowledge", response_model=AlarmOut,
             summary="Acknowledge an alarm (version-checked)")
def acknowledge(request: Request, alarm_id: str, payload: AlarmAck, session: DbSession,
                principal: CurrentPrincipal) -> AlarmOut:
    principal.require(Permission.ALARM_ACKNOWLEDGE)
    alarm = alarm_service.acknowledge(session, alarm_id, actor_id=principal.user_id,
                                      version=payload.version,
                                      note=alarm_service.validate_note(payload.note))
    audit.record(session, action=audit.Action.ALARM_ACK, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="alarm",
                 object_id=alarm_id, client_ip=client_ip(request),
                 detail={"version": payload.version})
    session.commit()
    return alarm_dto(alarm)


@router.post("/alarms/{alarm_id}/resolve", response_model=AlarmOut,
             summary="Resolve an alarm")
def resolve(request: Request, alarm_id: str, payload: AlarmAck, session: DbSession,
            principal: CurrentPrincipal) -> AlarmOut:
    principal.require(Permission.ALARM_RESOLVE)
    alarm = alarm_service.resolve(session, alarm_id, actor_id=principal.user_id,
                                  version=payload.version,
                                  note=alarm_service.validate_note(payload.note))
    audit.record(session, action=audit.Action.ALARM_RESOLVE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="alarm",
                 object_id=alarm_id, client_ip=client_ip(request))
    session.commit()
    return alarm_dto(alarm)


@router.post("/alarms/{alarm_id}/close", response_model=AlarmOut,
             summary="Close an alarm")
def close(request: Request, alarm_id: str, payload: AlarmAck, session: DbSession,
          principal: CurrentPrincipal) -> AlarmOut:
    principal.require(Permission.ALARM_RESOLVE)
    alarm = alarm_service.close(session, alarm_id, actor_id=principal.user_id,
                                version=payload.version)
    audit.record(session, action=audit.Action.ALARM_CLOSE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="alarm",
                 object_id=alarm_id, client_ip=client_ip(request))
    session.commit()
    return alarm_dto(alarm)


@router.post("/alarms/{alarm_id}/assign", response_model=AlarmOut,
             summary="Assign an alarm")
def assign(request: Request, alarm_id: str, payload: AssignRequest, session: DbSession,
           principal: CurrentPrincipal) -> AlarmOut:
    principal.require(Permission.ALARM_ACKNOWLEDGE)
    alarm = alarm_service.assign(session, alarm_id, assignee_id=payload.assignee_id,
                                 version=payload.version)
    audit.record(session, action=audit.Action.ALARM_ASSIGN, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="alarm",
                 object_id=alarm_id, client_ip=client_ip(request),
                 detail={"assignee": payload.assignee_id})
    session.commit()
    return alarm_dto(alarm)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
@router.get("/rules", summary="List alarm rules")
def list_rules(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.RULE_MANAGE)
    return {"items": [{
        "id": r.id, "name": r.name, "enabled": r.enabled,
        "event_types": r.event_types, "severity": r.severity,
        "raise_alarm": r.raise_alarm, "start_recording": r.start_recording,
        "record_seconds": r.record_seconds, "capture_snapshot": r.capture_snapshot,
        "notify_channels": r.notify_channels,
        "suppress_seconds": r.suppress_seconds, "conditions": r.conditions,
        "escalate_after_s": r.escalate_after_s,
    } for r in rule_service.list_rules(session)]}


@router.post("/rules", status_code=201, summary="Create an alarm rule")
def create_rule(request: Request, payload: RuleIn, session: DbSession,
                principal: CurrentPrincipal) -> dict:
    principal.require(Permission.RULE_MANAGE)
    data = payload.model_dump()
    name = data.pop("name")
    event_types = data.pop("event_types")
    severity = data.pop("severity").value
    rule = rule_service.create_rule(session, name=name, event_types=event_types,
                                    severity=severity, **data)
    audit.record(session, action=audit.Action.RULE_UPDATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="alarm_rule",
                 object_id=rule.id, client_ip=client_ip(request),
                 detail={"name": name, "action": "create"})
    session.commit()
    return {"id": rule.id, "name": rule.name}


@router.patch("/rules/{rule_id}", summary="Update an alarm rule")
def update_rule(request: Request, rule_id: str, payload: dict, session: DbSession,
                principal: CurrentPrincipal) -> dict:
    principal.require(Permission.RULE_MANAGE)
    rule = rule_service.update_rule(session, rule_id, payload)
    audit.record(session, action=audit.Action.RULE_UPDATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="alarm_rule",
                 object_id=rule_id, client_ip=client_ip(request),
                 detail={"changed": sorted(payload)})
    session.commit()
    return {"id": rule.id, "name": rule.name, "enabled": rule.enabled}


@router.delete("/rules/{rule_id}", status_code=204, summary="Delete an alarm rule")
def delete_rule(request: Request, rule_id: str, session: DbSession,
                principal: CurrentPrincipal) -> Response:
    principal.require(Permission.RULE_MANAGE)
    rule_service.delete_rule(session, rule_id)
    audit.record(session, action=audit.Action.RULE_UPDATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="alarm_rule",
                 object_id=rule_id, client_ip=client_ip(request),
                 detail={"action": "delete"})
    session.commit()
    return Response(status_code=204)
