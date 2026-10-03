import asyncio
import json
import logging
import os
import uuid

from aiokafka import AIOKafkaConsumer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.models.entities import AlarmInstanceEntity, AlarmRuleEntity
from app.services.alarm_rules import RuleSnapshot, dedupe_key, event_time, rule_matches

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("alarm-worker")

KAFKA = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
TOPIC = os.getenv("KAFKA_TOPIC_EVENTS", "vms.events.v1")
GROUP = os.getenv("ALARM_WORKER_GROUP_ID", "vms-alarm-worker-v1")
CACHE_SECONDS = max(1.0, float(os.getenv("ALARM_RULE_CACHE_SECONDS", "5")))


class RuleCache:
    """Cache enabled alarm rules in an event-type/site lookup index."""

    def __init__(self):
        self.index: dict[tuple[str, str | None, str], list[RuleSnapshot]] = {}
        self.loaded_at = 0.0

    async def refresh_if_needed(self) -> None:
        """Refresh the enabled-rule cache after the configured TTL expires.

        Returns:
            None after the in-memory index is current.

        Raises:
            Exception: Database query failures propagate to the worker loop.
        """
        now = asyncio.get_running_loop().time()
        if self.loaded_at and now - self.loaded_at < CACHE_SECONDS:
            return
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(AlarmRuleEntity).where(AlarmRuleEntity.enabled.is_(True))
                )
            ).scalars().all()

        index: dict[tuple[str, str | None, str], list[RuleSnapshot]] = {}
        for row in rows:
            snapshot = RuleSnapshot(
                id=row.id,
                tenant_id=row.tenant_id,
                site_id=row.site_id,
                name=row.name,
                event_types=frozenset(row.event_types_json or []),
                severities=frozenset(row.severities_json or []),
                camera_ids=frozenset(row.camera_ids_json or []),
                alarm_severity=row.alarm_severity,
                cooldown_seconds=row.cooldown_seconds,
            )
            for event_type in snapshot.event_types:
                index.setdefault(
                    (snapshot.tenant_id, snapshot.site_id, event_type), []
                ).append(snapshot)

        self.index = index
        self.loaded_at = now

    async def candidates(self, event: dict) -> list[RuleSnapshot]:
        """Return deduplicated cached rules that may match one event.

        Args:
            event: Normalized VMS event.

        Returns:
            Candidate RuleSnapshot objects across exact and wildcard scopes.

        Raises:
            Exception: Cache refresh or database failures propagate.
        """
        await self.refresh_if_needed()
        tenant = str(event.get("tenant_id", ""))
        site = str(event.get("site_id", ""))
        event_type = str(event.get("event_type", ""))
        result: list[RuleSnapshot] = []
        seen: set[str] = set()
        for key in (
            (tenant, site, event_type),
            (tenant, site, "*"),
            (tenant, None, event_type),
            (tenant, None, "*"),
        ):
            for rule in self.index.get(key, []):
                if rule.id not in seen:
                    seen.add(rule.id)
                    result.append(rule)
        return result

async def create_alarm(rule: RuleSnapshot, event: dict) -> bool:
    """Persist one deduplicated alarm instance for a matching event.

    Args:
        rule: Matching immutable alarm-rule snapshot.
        event: Normalized VMS event.

    Returns:
        True when a new alarm row is committed; False on dedupe-key conflict.

    Raises:
        Exception: Non-integrity database failures propagate to the worker loop.
    """
    key = dedupe_key(rule, event)
    when = event_time(event)
    row = AlarmInstanceEntity(
        id=str(uuid.uuid4()),
        rule_id=rule.id,
        event_id=str(event.get("event_id", ""))[:128],
        tenant_id=str(event.get("tenant_id", ""))[:128],
        site_id=str(event.get("site_id", ""))[:128],
        camera_id=str(event.get("camera_id", ""))[:36],
        event_type=str(event.get("event_type", ""))[:128],
        severity=rule.alarm_severity,
        state="open",
        message=f"{rule.name}: {event.get('event_type','event')} on camera {event.get('camera_id','unknown')}"[:1000],
        dedupe_key=key,
        opened_at=when,
        last_event_at=when,
    )
    async with SessionLocal() as session:
        session.add(row)
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def main():
    """Run the alarm Kafka consumer indefinitely.

    Returns:
        None under normal operation; the coroutine runs until cancelled.

    Raises:
        Exception: Startup or consumer failures outside the batch guard propagate.
    """
    cache = RuleCache()
    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers=KAFKA,
        group_id=GROUP,
        enable_auto_commit=False,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
    )
    await consumer.start()
    try:
        while True:
            records = await consumer.getmany(timeout_ms=500, max_records=1000)
            if not records:
                continue
            try:
                for _, messages in records.items():
                    for message in messages:
                        event = message.value
                        for rule in await cache.candidates(event):
                            if rule_matches(rule, event):
                                created = await create_alarm(rule, event)
                                if created:
                                    log.info(
                                        "alarm_opened rule_id=%s camera_id=%s event_type=%s",
                                        rule.id,
                                        event.get("camera_id"),
                                        event.get("event_type"),
                                    )
                await consumer.commit()
            except Exception:
                log.exception("alarm_batch_failed")
                await asyncio.sleep(1)
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())
