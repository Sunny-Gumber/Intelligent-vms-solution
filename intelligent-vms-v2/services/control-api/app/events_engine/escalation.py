"""Alarm escalation.

If an alarm is not acknowledged within the rule's escalate_after_s, the
escalation level increases and the configured channels are notified. Levels
are capped so a forgotten alarm cannot notify forever.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from vms_contracts.enums import AlarmState

from app.core.timeutil import utcnow
from app.db.models import Alarm, AlarmRule
from app.logging_setup import get_logger

log = get_logger("alarms.escalation")

MAX_ESCALATION_LEVEL = 3


@dataclass(frozen=True)
class Escalation:
    alarm_id: str
    level: int
    channels: list[str]
    title: str


def due_escalations(session: Session, *, now: dt.datetime | None = None) -> list[Escalation]:
    now = now or utcnow()
    alarms = session.execute(
        select(Alarm).where(
            Alarm.state.in_((AlarmState.NEW.value, AlarmState.ACTIVE.value)),
            Alarm.rule_id.is_not(None),
            Alarm.escalation_level < MAX_ESCALATION_LEVEL)).scalars().all()

    out: list[Escalation] = []
    for alarm in alarms:
        rule = session.get(AlarmRule, alarm.rule_id) if alarm.rule_id else None
        if rule is None or not rule.escalate_after_s:
            continue
        reference = alarm.escalated_at or alarm.first_event_at
        if (now - reference).total_seconds() < rule.escalate_after_s:
            continue
        out.append(Escalation(alarm_id=alarm.id, level=alarm.escalation_level + 1,
                              channels=list(rule.escalate_channels or []),
                              title=alarm.title))
    return out


def mark_escalated(session: Session, alarm_id: str, level: int) -> None:
    alarm = session.get(Alarm, alarm_id)
    if alarm is None:
        return
    alarm.escalation_level = min(level, MAX_ESCALATION_LEVEL)
    alarm.escalated_at = utcnow()
    alarm.version += 1
    log.warning("alarm.escalated", alarm_id=alarm_id, level=alarm.escalation_level)
