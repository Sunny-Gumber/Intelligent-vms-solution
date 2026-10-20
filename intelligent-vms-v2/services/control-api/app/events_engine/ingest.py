"""Event ingestion pipeline and rule evaluation.

Flow for every event, regardless of origin (device monitor, recorder, storage
monitor, ONVIF pull-point, analytics worker, external API):

    normalize -> dedupe -> persist -> evaluate rules
      -> (alarm | record | snapshot | notify) -> broadcast to subscribers

Back-pressure: the queue is bounded. When full, low-priority events are
dropped first and the system SAYS SO (a dropped counter surfaced in health)
rather than pretending everything is fine.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from vms_contracts.enums import AnalyticsSource, EventSeverity, EventType

from app.config import Settings, get_settings
from app.core.timeutil import ensure_utc, utcnow
from app.db.models import AlarmRule, Event
from app.db.session import session_scope
from app.events_engine import alarms as alarm_service
from app.events_engine.dedupe import DedupeWindow, SuppressionRegistry, dedupe_key
from app.logging_setup import get_logger

log = get_logger("events")

Subscriber = Callable[[dict[str, Any]], Awaitable[None]]
LOW_PRIORITY = {EventSeverity.INFO.value, EventSeverity.LOW.value}


@dataclass
class IncomingEvent:
    type: EventType | str
    severity: EventSeverity | str = EventSeverity.INFO
    camera_id: str | None = None
    site_id: str | None = None
    source: str = "system"
    occurred_at: dt.datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    discriminator: str | None = None
    snapshot_id: str | None = None

    @property
    def type_value(self) -> str:
        return self.type.value if isinstance(self.type, EventType) else str(self.type)

    @property
    def severity_value(self) -> str:
        return (self.severity.value if isinstance(self.severity, EventSeverity)
                else str(self.severity))


@dataclass
class RuleAction:
    rule_id: str
    raise_alarm: bool
    start_recording: bool
    record_seconds: int
    capture_snapshot: bool
    notify_channels: list[str]
    severity: str
    title: str


class EventPipeline:
    def __init__(self, settings: Settings | None = None, *, recording_trigger=None,  # noqa: ANN001
                 snapshot_service=None, notifier=None) -> None:  # noqa: ANN001
        self.settings = settings or get_settings()
        self.queue: asyncio.Queue[IncomingEvent] = asyncio.Queue(
            maxsize=self.settings.event_queue_max)
        self.dedupe = DedupeWindow(window_s=self.settings.event_dedupe_window_s)
        self.suppression = SuppressionRegistry()
        self.subscribers: set[Subscriber] = set()
        self.recording_trigger = recording_trigger
        self.snapshot_service = snapshot_service
        self.notifier = notifier
        self.dropped = 0
        self.processed = 0
        self.stop_event = asyncio.Event()

    # -- submission -----------------------------------------------------
    async def submit(self, event: IncomingEvent | dict) -> bool:
        incoming = _coerce(event)
        try:
            self.queue.put_nowait(incoming)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            if incoming.severity_value in LOW_PRIORITY:
                log.warning("events.queue_full_dropped", type=incoming.type_value,
                            dropped_total=self.dropped)
                return False
            # Make room by discarding one low-priority event for a serious one.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(incoming)
                log.warning("events.queue_full_evicted", type=incoming.type_value)
                return True
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                return False

    def submit_nowait(self, event: IncomingEvent | dict) -> bool:
        try:
            self.queue.put_nowait(_coerce(event))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    # -- processing -----------------------------------------------------
    async def run(self) -> None:
        log.info("events.pipeline_started", queue_max=self.settings.event_queue_max)
        while not self.stop_event.is_set():
            try:
                incoming = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self.process(incoming)
            except Exception as exc:  # noqa: BLE001
                log.error("events.process_failed", type=incoming.type_value,
                          error=str(exc), exc_info=True)
            finally:
                self.queue.task_done()

    async def process(self, incoming: IncomingEvent) -> None:
        key = dedupe_key(incoming.type_value, incoming.camera_id, incoming.discriminator)
        if self.dedupe.check(key).suppressed:
            log.debug("events.deduplicated", key=key)
            return

        actions: list[RuleAction] = []
        with session_scope() as session:
            event = Event(type=incoming.type_value, severity=incoming.severity_value,
                          camera_id=incoming.camera_id, site_id=incoming.site_id,
                          source=incoming.source,
                          occurred_at=ensure_utc(incoming.occurred_at or utcnow()),
                          payload=dict(incoming.payload), dedupe_key=key,
                          snapshot_id=incoming.snapshot_id)
            session.add(event)
            session.flush()

            actions = self._match_rules(session, incoming)
            for action in actions:
                if self.suppression.is_suppressed(action.rule_id):
                    continue
                if action.raise_alarm:
                    alarm_service.raise_alarm(
                        session, title=action.title,
                        severity=EventSeverity(action.severity),
                        camera_id=incoming.camera_id, site_id=incoming.site_id,
                        rule_id=action.rule_id, dedupe_key=key, event=event,
                        snapshot_id=incoming.snapshot_id)
                rule = session.get(AlarmRule, action.rule_id)
                if rule is not None and rule.suppress_seconds:
                    self.suppression.suppress(action.rule_id, rule.suppress_seconds)

            broadcast = {"id": event.id, "type": event.type, "severity": event.severity,
                         "camera_id": event.camera_id, "site_id": event.site_id,
                         "occurred_at": event.occurred_at.isoformat(),
                         "payload": event.payload, "alarm_id": event.alarm_id}
            event_id = event.id

        # Side effects happen outside the DB transaction so a slow webhook
        # cannot hold a write lock open.
        for action in actions:
            if action.start_recording and self.recording_trigger and incoming.camera_id:
                try:
                    await self.recording_trigger(incoming.camera_id, action.record_seconds)
                except Exception as exc:  # noqa: BLE001
                    log.error("events.recording_trigger_failed",
                              camera_id=incoming.camera_id, error=str(exc))
            if action.capture_snapshot and self.snapshot_service and incoming.camera_id:
                try:
                    await self.snapshot_service(incoming.camera_id, event_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("events.snapshot_failed", camera_id=incoming.camera_id,
                                error=str(exc))
            if action.notify_channels and self.notifier:
                try:
                    await self.notifier(action.notify_channels, broadcast)
                except Exception as exc:  # noqa: BLE001
                    log.error("events.notify_failed", error=str(exc))

        await self._broadcast(broadcast)
        self.processed += 1

    # -- rules ----------------------------------------------------------
    def _match_rules(self, session: Session, incoming: IncomingEvent) -> list[RuleAction]:
        rules = session.execute(
            select(AlarmRule).where(AlarmRule.enabled.is_(True))).scalars().all()
        matched: list[RuleAction] = []
        for rule in rules:
            if rule.event_types and incoming.type_value not in rule.event_types:
                continue
            if rule.camera_ids and incoming.camera_id not in rule.camera_ids:
                continue
            if rule.site_ids and incoming.site_id not in rule.site_ids:
                continue
            if not conditions_match(rule.conditions or {}, incoming.payload):
                continue
            if not schedule_active(rule.schedule or {}, incoming.occurred_at or utcnow()):
                continue
            matched.append(RuleAction(
                rule_id=rule.id, raise_alarm=rule.raise_alarm,
                start_recording=rule.start_recording, record_seconds=rule.record_seconds,
                capture_snapshot=rule.capture_snapshot,
                notify_channels=list(rule.notify_channels or []),
                severity=rule.severity,
                title=f"{rule.name}: {incoming.type_value}"))
        return matched

    # -- subscribers ----------------------------------------------------
    def subscribe(self, subscriber: Subscriber) -> None:
        self.subscribers.add(subscriber)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self.subscribers.discard(subscriber)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        dead: list[Subscriber] = []
        for subscriber in list(self.subscribers):
            try:
                await subscriber(message)
            except Exception:  # noqa: BLE001 — a dead client must not break ingestion
                dead.append(subscriber)
        for subscriber in dead:
            self.subscribers.discard(subscriber)

    def stop(self) -> None:
        self.stop_event.set()

    def stats(self) -> dict[str, int]:
        return {"queued": self.queue.qsize(), "processed": self.processed,
                "dropped": self.dropped, "subscribers": len(self.subscribers)}


# --------------------------------------------------------------------------
def _coerce(event: IncomingEvent | dict) -> IncomingEvent:
    if isinstance(event, IncomingEvent):
        return event
    return IncomingEvent(type=event.get("type", EventType.SYSTEM_HEALTH),
                         severity=event.get("severity", EventSeverity.INFO),
                         camera_id=event.get("camera_id"), site_id=event.get("site_id"),
                         source=event.get("source", "system"),
                         occurred_at=event.get("occurred_at"),
                         payload=event.get("payload", {}) or {},
                         discriminator=event.get("discriminator"),
                         snapshot_id=event.get("snapshot_id"))


def conditions_match(conditions: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Restricted, auditable condition language.

        {"field": "object_class", "op": "eq", "value": "person"}
        {"all": [ {...}, {...} ]}      {"any": [ {...} ]}

    Supported ops: eq, ne, gt, gte, lt, lte, in, contains. There is no
    attribute access, no imports and no eval, so an operator-defined rule can
    never execute arbitrary logic.
    """
    if not conditions:
        return True
    if "all" in conditions:
        return all(conditions_match(c, payload) for c in conditions["all"])
    if "any" in conditions:
        return any(conditions_match(c, payload) for c in conditions["any"])

    op = conditions.get("op", "eq")
    expected = conditions.get("value")
    actual = payload.get(conditions.get("field"))

    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "gt":
            return actual is not None and float(actual) > float(expected)
        if op == "gte":
            return actual is not None and float(actual) >= float(expected)
        if op == "lt":
            return actual is not None and float(actual) < float(expected)
        if op == "lte":
            return actual is not None and float(actual) <= float(expected)
        if op == "in":
            return actual in (expected or [])
        if op == "contains":
            return expected in (actual or "")
    except (TypeError, ValueError):
        return False
    return False


def schedule_active(schedule: dict[str, Any], moment: dt.datetime) -> bool:
    """Optional rule schedule: {"days": [0..6], "start": "18:00", "end": "06:00"}."""
    if not schedule:
        return True
    moment = ensure_utc(moment)
    days = schedule.get("days")
    if days and moment.weekday() not in days:
        return False
    start, end = schedule.get("start"), schedule.get("end")
    if not (start and end):
        return True
    current = moment.strftime("%H:%M")
    if start <= end:
        return start <= current < end
    return current >= start or current < end     # window crosses midnight


def analytics_event_payload(object_class: str, confidence: float | None,
                            source: AnalyticsSource, **extra: Any) -> dict[str, Any]:
    payload = {"object_class": object_class, "confidence": confidence,
               "analytics_source": source.value}
    payload.update(extra)
    return payload


# Backwards-compatible aliases. These helpers were private in earlier drafts and
# are referenced by external rule-authoring tooling; the public names above are
# canonical.
_conditions_match = conditions_match
_schedule_active = schedule_active
