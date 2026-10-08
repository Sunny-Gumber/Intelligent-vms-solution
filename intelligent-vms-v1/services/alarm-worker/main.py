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
BATCH_POLL_TIMEOUT_MS = 500
BATCH_MAX_RECORDS = 1000
BATCH_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_POISON_RETRY_LIMIT = 3


def _parse_poison_retry_limit(raw):
    """Return a poison-retry bound of at least one attempt.

    Args:
        raw: Configured retry limit, typically an int or numeric string.

    Returns:
        The limit, or the default when the value is not a positive integer.
    """
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_POISON_RETRY_LIMIT


POISON_RETRY_LIMIT = _parse_poison_retry_limit(
    os.getenv("ALARM_POISON_RETRY_LIMIT", str(DEFAULT_POISON_RETRY_LIMIT))
)


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


def _sort_offset(message):
    """Return a sort key that keeps unreadable offsets after real ones.

    Args:
        message: Fetched consumer record.

    Returns:
        The integer offset, or a value past any real offset when it is unreadable.
    """
    try:
        return int(message.offset)
    except (AttributeError, TypeError, ValueError):
        return 2**63


def _record_identity(message):
    """Return the stable topic, partition, and offset identity of one record.

    Args:
        message: Fetched consumer record.

    Returns:
        Tuple of topic, partition, and offset.

    Raises:
        AttributeError, TypeError, ValueError: The record has no usable offset.
    """
    return (str(message.topic), int(message.partition), int(message.offset))


def _rewind_fetched_batch(consumer, records):
    """Seek every partition in this fetch back to its first record.

    getmany has already advanced those positions. Leaving any of them forward
    lets a later commit skip records from this failed batch, including records
    on a partition that was fetched but not applied.

    Args:
        consumer: Manual-commit consumer that produced the fetch.
        records: Mapping of topic partition to fetched records.

    Returns:
        None after each non-empty partition is seeked.

    Raises:
        Exception: Consumer seek failures propagate to the caller.
    """
    for topic_partition, messages in records.items():
        if not messages:
            continue
        readable_offsets = []
        for message in messages:
            try:
                readable_offsets.append(int(message.offset))
            except (AttributeError, TypeError, ValueError):
                continue
        if not readable_offsets:
            continue
        first_offset = min(readable_offsets)
        log.info(
            "alarm_batch_replay topic=%s partition=%s offset=%s",
            getattr(topic_partition, "topic", ""),
            getattr(topic_partition, "partition", ""),
            first_offset,
        )
        consumer.seek(topic_partition, first_offset)


def _next_offsets(records):
    """Return the next commit offset for each partition in a handled fetch.

    Args:
        records: Mapping of topic partition to fetched records.

    Returns:
        Mapping of topic partition to one past the highest fetched offset.
        Empty partitions are omitted so they are not committed.
    """
    offsets = {}
    for topic_partition, messages in records.items():
        readable_offsets = []
        for message in messages:
            try:
                readable_offsets.append(int(message.offset))
            except (AttributeError, TypeError, ValueError):
                continue
        if not readable_offsets:
            continue
        offsets[topic_partition] = max(readable_offsets) + 1
    return offsets


async def _apply_matching_rules(cache, event):
    """Open deduplicated alarms for one event.

    Replay is safe because create_alarm absorbs the unique dedupe-key conflict
    and returns False instead of inserting a second row.

    Args:
        cache: Enabled-rule cache.
        event: Normalized VMS event object.

    Returns:
        None after every matching rule has been applied.

    Raises:
        TypeError: The payload is not a JSON object.
        Exception: Rule lookup or database failures propagate to the batch handler.
    """
    if not isinstance(event, dict):
        raise TypeError("alarm event payload must be a JSON object")
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


class _BatchReplay:
    """Replay a failed fetch and give up on a poison record after a fixed limit.

    Skipped poison offsets are remembered only in this process. A crash before
    commit still resumes from the last committed offset, so a skip is durable
    only after the handled batch is committed.
    """

    def __init__(self, poison_retry_limit):
        self.poison_retry_limit = poison_retry_limit
        self._failures = {}
        self._skipped = set()

    async def consume(self, consumer, cache, records):
        """Apply one fetch. Commit only after every record is applied or skipped.

        Args:
            consumer: Manual-commit consumer that produced the fetch.
            cache: Enabled-rule cache.
            records: Mapping of topic partition to fetched records.

        Returns:
            None after the handled offsets are committed, or after a retryable
            failure rewinds the fetch without committing.

        Raises:
            Exception: Commit failures propagate so the caller can rewind.
        """
        for _topic_partition, messages in records.items():
            for message in sorted(messages, key=_sort_offset):
                try:
                    key = _record_identity(message)
                except (AttributeError, TypeError, ValueError):
                    log.error(
                        "alarm_poison_record topic=%s partition=%s offset=%s attempts=1 reason=unreadable_offset",
                        getattr(message, "topic", ""),
                        getattr(message, "partition", ""),
                        getattr(message, "offset", ""),
                    )
                    continue
                if key in self._skipped:
                    continue
                try:
                    await _apply_matching_rules(cache, message.value)
                except Exception:
                    attempts = self._failures.get(key, 0) + 1
                    self._failures[key] = attempts
                    log.exception(
                        "alarm_batch_failed topic=%s partition=%s offset=%s attempt=%s limit=%s",
                        key[0],
                        key[1],
                        key[2],
                        attempts,
                        self.poison_retry_limit,
                    )
                    if attempts >= self.poison_retry_limit:
                        self._skipped.add(key)
                        self._failures.pop(key, None)
                        log.error(
                            "alarm_poison_record topic=%s partition=%s offset=%s attempts=%s",
                            key[0],
                            key[1],
                            key[2],
                            attempts,
                        )
                        continue
                    _rewind_fetched_batch(consumer, records)
                    await asyncio.sleep(BATCH_RETRY_BACKOFF_SECONDS)
                    return
                self._failures.pop(key, None)
        offsets = _next_offsets(records)
        if offsets:
            await consumer.commit(offsets)


async def _consume_fetched_batch(consumer, cache, records, replay):
    """Handle one fetch and rewind it when an unexpected error prevents commit.

    Args:
        consumer: Manual-commit consumer that produced the fetch.
        cache: Enabled-rule cache.
        records: Mapping of topic partition to fetched records.
        replay: Retry and poison state for this process.

    Returns:
        None after the batch is committed or rewound.

    Raises:
        Exception: Rewind failures after an unexpected batch error propagate.
    """
    try:
        await replay.consume(consumer, cache, records)
    except Exception:
        log.exception("alarm_batch_failed")
        _rewind_fetched_batch(consumer, records)
        await asyncio.sleep(BATCH_RETRY_BACKOFF_SECONDS)


async def main():
    """Run the alarm Kafka consumer indefinitely.

    A failed fetch is seeked back and replayed. Offsets are committed only for
    partitions whose fetched records were applied or deliberately skipped as
    poison. Replayed records do not open a second alarm.

    Returns:
        None under normal operation; the coroutine runs until cancelled.

    Raises:
        Exception: Startup failures, or rewind failures after an unexpected
            batch error, propagate. CancelledError stops the loop.
    """
    cache = RuleCache()
    replay = _BatchReplay(_parse_poison_retry_limit(POISON_RETRY_LIMIT))
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
            records = await consumer.getmany(
                timeout_ms=BATCH_POLL_TIMEOUT_MS,
                max_records=BATCH_MAX_RECORDS,
            )
            if not records:
                continue
            await _consume_fetched_batch(consumer, cache, records, replay)
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())
