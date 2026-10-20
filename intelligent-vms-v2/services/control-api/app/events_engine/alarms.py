"""Alarm state machine with optimistic concurrency.

State machine (M8-R11). Illegal transitions are rejected, not ignored:

    new ──► active ──► acknowledged ──► resolved ──► closed
     └──────┴──────────────┴───────────────┴────────► closed

Concurrency (M8-R19): two operators acknowledging the same alarm at the same
instant is normal in a control room. Every mutation requires the caller's
observed `version`; the UPDATE is conditional on that version, so the loser
receives 409 `stale_version` with the current state rather than overwriting
the winner's acknowledgement.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from vms_contracts.enums import AlarmState, EventSeverity

from app.core.errors import ConflictError, NotFoundError, StaleVersionError, ValidationError
from app.core.timeutil import utcnow
from app.db.models import Alarm, Event
from app.logging_setup import get_logger

log = get_logger("alarms")

ALLOWED_TRANSITIONS: dict[AlarmState, set[AlarmState]] = {
    AlarmState.NEW: {AlarmState.ACTIVE, AlarmState.ACKNOWLEDGED, AlarmState.CLOSED},
    AlarmState.ACTIVE: {AlarmState.ACKNOWLEDGED, AlarmState.RESOLVED, AlarmState.CLOSED},
    AlarmState.ACKNOWLEDGED: {AlarmState.RESOLVED, AlarmState.CLOSED, AlarmState.ACTIVE},
    AlarmState.RESOLVED: {AlarmState.CLOSED, AlarmState.ACTIVE},
    AlarmState.CLOSED: set(),
}


def assert_version(alarm: Alarm, expected_version: int) -> None:
    """Version is checked BEFORE the transition rule.

    When two operators act on the same alarm, the loser should be told
    "someone else changed this" (stale_version) rather than the less helpful
    "that transition is illegal", which is only a symptom of the race.
    """
    if alarm.version != expected_version:
        raise StaleVersionError(
            f"This alarm was updated by {alarm.acknowledged_by or 'another operator'} "
            f"while you were viewing it. It is now {alarm.state}.",
            extra={"current_state": alarm.state, "current_version": alarm.version})


def assert_transition(current: AlarmState, target: AlarmState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ConflictError(f"An alarm cannot move from {current.value} to {target.value}",
                            code="invalid_alarm_transition",
                            extra={"current": current.value, "requested": target.value})


def _apply(session: Session, alarm_id: str, expected_version: int, values: dict,
           target: AlarmState) -> Alarm:
    """Version-checked UPDATE. Returns the refreshed alarm or raises 409."""
    result = session.execute(
        update(Alarm).where(Alarm.id == alarm_id, Alarm.version == expected_version)
        .values(**values, version=expected_version + 1))
    if (result.rowcount or 0) == 0:
        current = session.get(Alarm, alarm_id)
        if current is None:
            raise NotFoundError("Alarm not found", code="alarm_not_found")
        session.refresh(current)
        assert_version(current, expected_version)
    session.flush()
    alarm = session.get(Alarm, alarm_id)
    session.refresh(alarm)
    log.info("alarm.transition", alarm_id=alarm_id, state=target.value, version=alarm.version)
    return alarm


def raise_alarm(session: Session, *, title: str, severity: EventSeverity,
                camera_id: str | None = None, site_id: str | None = None,
                rule_id: str | None = None, dedupe_key: str | None = None,
                event: Event | None = None, snapshot_id: str | None = None,
                aggregate_window_s: int = 300) -> Alarm:
    """Create an alarm, or aggregate into an open one with the same key."""
    now = utcnow()
    if dedupe_key:
        existing = session.execute(
            select(Alarm).where(
                Alarm.dedupe_key == dedupe_key,
                Alarm.state.in_((AlarmState.NEW.value, AlarmState.ACTIVE.value,
                                 AlarmState.ACKNOWLEDGED.value)),
                Alarm.last_event_at >= now - dt.timedelta(seconds=aggregate_window_s))
            .order_by(Alarm.last_event_at.desc())).scalars().first()
        if existing is not None:
            existing.last_event_at = now
            existing.event_count += 1
            if existing.state == AlarmState.NEW.value:
                existing.state = AlarmState.ACTIVE.value
            existing.version += 1
            if event is not None:
                event.alarm_id = existing.id
            return existing

    alarm = Alarm(rule_id=rule_id, state=AlarmState.NEW.value, severity=severity.value,
                  camera_id=camera_id, site_id=site_id, title=title[:256],
                  dedupe_key=dedupe_key, first_event_at=now, last_event_at=now,
                  event_count=1, snapshot_id=snapshot_id)
    session.add(alarm)
    session.flush()
    if event is not None:
        event.alarm_id = alarm.id
    log.info("alarm.raised", alarm_id=alarm.id, severity=severity.value, title=title[:80])
    return alarm


def acknowledge(session: Session, alarm_id: str, *, actor_id: str, version: int,
                note: str | None = None) -> Alarm:
    alarm = session.get(Alarm, alarm_id)
    if alarm is None:
        raise NotFoundError("Alarm not found", code="alarm_not_found")
    assert_version(alarm, version)
    assert_transition(AlarmState(alarm.state), AlarmState.ACKNOWLEDGED)
    return _apply(session, alarm_id, version,
                  {"state": AlarmState.ACKNOWLEDGED.value, "acknowledged_by": actor_id,
                   "acknowledged_at": utcnow(), "note": note or alarm.note},
                  AlarmState.ACKNOWLEDGED)


def resolve(session: Session, alarm_id: str, *, actor_id: str, version: int,
            note: str | None = None) -> Alarm:
    alarm = session.get(Alarm, alarm_id)
    if alarm is None:
        raise NotFoundError("Alarm not found", code="alarm_not_found")
    assert_version(alarm, version)
    assert_transition(AlarmState(alarm.state), AlarmState.RESOLVED)
    return _apply(session, alarm_id, version,
                  {"state": AlarmState.RESOLVED.value, "resolved_by": actor_id,
                   "resolved_at": utcnow(), "note": note or alarm.note},
                  AlarmState.RESOLVED)


def close(session: Session, alarm_id: str, *, actor_id: str, version: int) -> Alarm:
    alarm = session.get(Alarm, alarm_id)
    if alarm is None:
        raise NotFoundError("Alarm not found", code="alarm_not_found")
    assert_version(alarm, version)
    assert_transition(AlarmState(alarm.state), AlarmState.CLOSED)
    return _apply(session, alarm_id, version,
                  {"state": AlarmState.CLOSED.value, "closed_at": utcnow()}, AlarmState.CLOSED)


def assign(session: Session, alarm_id: str, *, assignee_id: str, version: int) -> Alarm:
    alarm = session.get(Alarm, alarm_id)
    if alarm is None:
        raise NotFoundError("Alarm not found", code="alarm_not_found")
    assert_version(alarm, version)
    if alarm.state == AlarmState.CLOSED.value:
        raise ConflictError("A closed alarm cannot be assigned", code="alarm_closed")
    return _apply(session, alarm_id, version, {"assigned_to": assignee_id},
                  AlarmState(alarm.state))


def auto_close_stale(session: Session, *, hours: int = 72) -> int:
    cutoff = utcnow() - dt.timedelta(hours=hours)
    rows = session.execute(
        select(Alarm).where(Alarm.state == AlarmState.RESOLVED.value,
                            Alarm.resolved_at < cutoff)).scalars().all()
    for alarm in rows:
        alarm.state = AlarmState.CLOSED.value
        alarm.closed_at = utcnow()
        alarm.version += 1
    return len(rows)


def search(session: Session, *, states: list[str] | None = None,
           severities: list[str] | None = None, camera_ids: list[str] | None = None,
           since: dt.datetime | None = None, until: dt.datetime | None = None,
           assigned_to: str | None = None, limit: int = 100,
           offset: int = 0) -> tuple[list[Alarm], int]:
    def apply_filters(statement):  # noqa: ANN001
        if states:
            statement = statement.where(Alarm.state.in_(states))
        if severities:
            statement = statement.where(Alarm.severity.in_(severities))
        if camera_ids is not None:
            statement = statement.where(Alarm.camera_id.in_(camera_ids))
        if since:
            statement = statement.where(Alarm.first_event_at >= since)
        if until:
            statement = statement.where(Alarm.first_event_at <= until)
        if assigned_to:
            statement = statement.where(Alarm.assigned_to == assigned_to)
        return statement

    total = int(session.execute(
        apply_filters(select(func.count()).select_from(Alarm))).scalar_one())
    rows = session.execute(
        apply_filters(select(Alarm)).order_by(Alarm.first_event_at.desc())
        .limit(limit).offset(offset)).scalars().all()
    return list(rows), total


def validate_note(note: str | None) -> str | None:
    if note is not None and len(note) > 1000:
        raise ValidationError("Note is too long (max 1000 characters)", code="note_too_long")
    return note
