"""Alarm rule management and validation.

Rules are DATA, not code. The condition language is the restricted expression
evaluated by app.events_engine.ingest.conditions_match (eq/ne/gt/gte/lt/lte/
in/contains with all/any grouping), which has no attribute access, no imports
and no eval.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from vms_contracts.enums import EventSeverity, EventType

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.models import AlarmRule

VALID_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"}
MAX_CONDITION_DEPTH = 4


def validate_conditions(conditions: dict[str, Any], depth: int = 0) -> None:
    if not conditions:
        return
    if depth > MAX_CONDITION_DEPTH:
        raise ValidationError("Condition nesting is too deep", code="condition_too_deep")
    if "all" in conditions or "any" in conditions:
        group = conditions.get("all") or conditions.get("any") or []
        if not isinstance(group, list):
            raise ValidationError("'all'/'any' must contain a list of conditions",
                                  code="invalid_condition")
        for child in group:
            validate_conditions(child, depth + 1)
        return
    if "field" not in conditions:
        raise ValidationError("A condition needs a 'field'", code="invalid_condition")
    if conditions.get("op", "eq") not in VALID_OPS:
        raise ValidationError(
            f"Unsupported operator {conditions.get('op')!r}. Allowed: {sorted(VALID_OPS)}",
            code="invalid_operator")


def validate_event_types(types: list[str]) -> list[str]:
    known = {item.value for item in EventType}
    unknown = [t for t in types if t not in known]
    if unknown:
        raise ValidationError(f"Unknown event types: {unknown}", code="unknown_event_type")
    return types


def create_rule(session: Session, *, name: str, event_types: list[str],
                severity: str = EventSeverity.MEDIUM.value, **kwargs: Any) -> AlarmRule:
    if session.execute(select(AlarmRule).where(AlarmRule.name == name)).scalar_one_or_none():
        raise ConflictError(f"A rule named {name!r} already exists", code="rule_exists")
    validate_event_types(event_types)
    validate_conditions(kwargs.get("conditions") or {})
    rule = AlarmRule(name=name, event_types=event_types, severity=severity,
                     **{k: v for k, v in kwargs.items() if hasattr(AlarmRule, k)})
    session.add(rule)
    session.flush()
    return rule


def update_rule(session: Session, rule_id: str, changes: dict[str, Any]) -> AlarmRule:
    rule = session.get(AlarmRule, rule_id)
    if rule is None:
        raise NotFoundError("Rule not found", code="rule_not_found")
    if "event_types" in changes:
        validate_event_types(changes["event_types"])
    if "conditions" in changes:
        validate_conditions(changes["conditions"] or {})
    for key, value in changes.items():
        if hasattr(rule, key):
            setattr(rule, key, value)
    return rule


def delete_rule(session: Session, rule_id: str) -> None:
    rule = session.get(AlarmRule, rule_id)
    if rule is None:
        raise NotFoundError("Rule not found", code="rule_not_found")
    session.delete(rule)


def list_rules(session: Session) -> list[AlarmRule]:
    return list(session.execute(select(AlarmRule).order_by(AlarmRule.name)).scalars().all())


def default_rules() -> list[dict[str, Any]]:
    """Shipped defaults so a fresh install already alarms on the basics."""
    return [
        {"name": "Camera offline", "event_types": [EventType.CAMERA_OFFLINE.value],
         "severity": EventSeverity.HIGH.value, "raise_alarm": True,
         "suppress_seconds": 120},
        {"name": "Recording failed", "event_types": [EventType.RECORDING_FAILED.value],
         "severity": EventSeverity.HIGH.value, "raise_alarm": True,
         "suppress_seconds": 300},
        {"name": "Storage critical",
         "event_types": [EventType.STORAGE_CRITICAL.value, EventType.STORAGE_FAILED.value],
         "severity": EventSeverity.CRITICAL.value, "raise_alarm": True,
         "suppress_seconds": 600},
        {"name": "Camera credentials rejected",
         "event_types": [EventType.CAMERA_UNAUTHORIZED.value],
         "severity": EventSeverity.MEDIUM.value, "raise_alarm": True,
         "suppress_seconds": 900},
    ]
