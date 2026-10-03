import asyncio
import json
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import EventOutboxEntity
from app.services.events import EventPublisherPayloadError, EventPublisherUnavailable, event_publisher

log = logging.getLogger(__name__)

PENDING_STATES = ("pending", "retry")


class OutboxPayloadTooLarge(ValueError):
    """Raised when a transactional-outbox payload exceeds the configured limit."""


def validate_payload_size(payload: dict) -> None:
    """Validate the serialized payload against the configured outbox size limit.

    Args:
        payload: Message payload to serialize and measure.

    Returns:
        None when the payload is within the configured limit.

    Raises:
        OutboxPayloadTooLarge: If the serialized payload exceeds the limit.
        TypeError: If serialization cannot handle a supplied object.
    """
    encoded = json.dumps(
        payload,
        default=str,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > max(1024, settings.outbox_max_payload_bytes):
        raise OutboxPayloadTooLarge(
            f"outbox payload exceeds {settings.outbox_max_payload_bytes} bytes"
        )


@dataclass(frozen=True)
class OutboxMessage:
    """Immutable claimed message passed from storage to the publisher worker.

    Attributes:
        id: Durable message identifier.
        topic: Kafka destination topic.
        key_text: Text partitioning key.
        payload: Message body.
        attempts: Number of prior failed delivery attempts.
        claim_token: Lease token proving worker ownership.
    """

    id: str
    topic: str
    key_text: str
    payload: dict
    attempts: int
    claim_token: str


def utcnow() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(timezone.utc)


def retry_delay_seconds(attempts: int) -> float:
    """Calculate bounded exponential retry delay with jitter.

    Args:
        attempts: Delivery-attempt count used for backoff.

    Returns:
        Retry delay in seconds, capped by the configured maximum.
    """
    base = min(
        max(1.0, float(settings.outbox_backoff_max_seconds)),
        float(2 ** min(max(0, attempts), 8)),
    )
    jitter = random.uniform(0.0, min(1.0, base * 0.2))
    return min(float(settings.outbox_backoff_max_seconds), base + jitter)


def enqueue_message(
    session,
    *,
    message_id: str,
    topic: str,
    key_text: str,
    payload: dict,
) -> EventOutboxEntity:
    """Stage one outbox message in the caller's current transaction.

    Args:
        session: SQLAlchemy session owning the transaction.
        message_id: Durable unique message identifier.
        topic: Kafka destination topic.
        key_text: Text partitioning key.
        payload: Message payload.

    Returns:
        Newly created EventOutboxEntity added to the session.

    Raises:
        OutboxPayloadTooLarge: If the payload exceeds the configured limit.
    """
    validate_payload_size(payload)
    now = utcnow()
    row = EventOutboxEntity(
        id=message_id,
        topic=topic,
        key_text=key_text,
        payload_json=dict(payload),
        status="pending",
        attempts=0,
        next_attempt_at=now,
    )
    session.add(row)
    return row


def enqueue_event(session, event: dict) -> EventOutboxEntity:
    """Stage one normalized VMS event for transactional broker delivery.

    Args:
        session: SQLAlchemy session owning the transaction.
        event: Normalized VMS event dictionary.

    Returns:
        Newly created EventOutboxEntity.

    Raises:
        KeyError: If required event identity fields are missing.
        OutboxPayloadTooLarge: If the event exceeds the configured limit.
    """
    event_id = str(event["event_id"])
    key_text = f"{event.get('tenant_id', 'default')}:{event['camera_id']}"
    return enqueue_message(
        session,
        message_id=f"event:{event_id}",
        topic=settings.kafka_topic_events,
        key_text=key_text,
        payload=event,
    )


async def enqueue_message_once(
    session,
    *,
    message_id: str,
    topic: str,
    key_text: str,
    payload: dict,
) -> bool:
    """Atomically enqueue one deterministic message ID.

    Production Postgres and local SQLite both use ON CONFLICT DO NOTHING so
    concurrent retries cannot race into a duplicate row.
    """
    validate_payload_size(payload)
    values = {
        "id": message_id,
        "topic": topic,
        "key_text": key_text,
        "payload_json": dict(payload),
        "status": "pending",
        "attempts": 0,
        "next_attempt_at": utcnow(),
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        existing = await session.get(EventOutboxEntity, message_id)
        if existing is not None:
            return False
        session.add(EventOutboxEntity(**values))
        return True

    result = await session.execute(
        dialect_insert(EventOutboxEntity)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[EventOutboxEntity.id])
    )
    return bool(result.rowcount)


async def enqueue_event_once(session, event: dict) -> bool:
    """Atomically enqueue one normalized event by deterministic identifier.

    Args:
        session: SQLAlchemy session owning the transaction.
        event: Normalized VMS event dictionary.

    Returns:
        True when inserted, False when the deterministic message already exists.

    Raises:
        KeyError: If required event identity fields are missing.
        OutboxPayloadTooLarge: If the event exceeds the configured limit.
        Exception: Database failures propagate to the caller.
    """
    event_id = str(event["event_id"])
    return await enqueue_message_once(
        session,
        message_id=f"event:{event_id}",
        topic=settings.kafka_topic_events,
        key_text=f"{event.get('tenant_id', 'default')}:{event['camera_id']}",
        payload=event,
    )


def claim_statement(now: datetime, limit: int):
    """Build the bounded row-locking query used to claim publishable messages.

    Args:
        now: Current UTC time for retry and claim-expiry predicates.
        limit: Maximum number of rows to claim.

    Returns:
        SQLAlchemy select statement using SKIP LOCKED for replica safety.
    """
    return (
        select(EventOutboxEntity)
        .where(
            EventOutboxEntity.status.in_(PENDING_STATES),
            EventOutboxEntity.next_attempt_at <= now,
            or_(
                EventOutboxEntity.claim_until.is_(None),
                EventOutboxEntity.claim_until <= now,
            ),
        )
        .order_by(EventOutboxEntity.created_at, EventOutboxEntity.id)
        .limit(max(1, limit))
        .with_for_update(skip_locked=True)
    )


async def claim_batch() -> list[OutboxMessage]:
    """Claim a bounded batch of due messages for one worker lease.

    Returns:
        Immutable claimed messages sharing the new claim token.

    Raises:
        Exception: Database failures propagate to the worker loop.
    """
    now = utcnow()
    token = str(uuid.uuid4())
    lease_until = now + timedelta(seconds=max(5, settings.outbox_claim_seconds))
    async with SessionLocal() as session:
        async with session.begin():
            rows = list(
                (
                    await session.execute(
                        claim_statement(now, settings.outbox_batch_size)
                    )
                ).scalars().all()
            )
            messages: list[OutboxMessage] = []
            for row in rows:
                row.claim_token = token
                row.claim_until = lease_until
                messages.append(
                    OutboxMessage(
                        id=row.id,
                        topic=row.topic,
                        key_text=row.key_text,
                        payload=dict(row.payload_json or {}),
                        attempts=int(row.attempts or 0),
                        claim_token=token,
                    )
                )
            return messages


async def renew_claim(message: OutboxMessage) -> bool:
    """Renew one claimed-message lease immediately before publishing.

    Args:
        message: Claimed outbox message.

    Returns:
        True when this worker still owns the claim; otherwise False.

    Raises:
        Exception: Database failures propagate to the worker loop.
    """
    now = utcnow()
    lease_until = now + timedelta(seconds=max(5, settings.outbox_claim_seconds))
    async with SessionLocal() as session:
        async with session.begin():
            row = await session.get(EventOutboxEntity, message.id, with_for_update=True)
            if row is None or row.claim_token != message.claim_token:
                return False
            row.claim_until = lease_until
            return True


async def mark_delivered(message: OutboxMessage) -> bool:
    """Mark one still-owned outbox message as delivered.

    Args:
        message: Claimed outbox message.

    Returns:
        True when delivery is persisted; False when the claim was lost.

    Raises:
        Exception: Database failures propagate to the worker loop.
    """
    now = utcnow()
    async with SessionLocal() as session:
        async with session.begin():
            row = await session.get(EventOutboxEntity, message.id, with_for_update=True)
            if row is None or row.claim_token != message.claim_token:
                return False
            row.status = "delivered"
            row.delivered_at = now
            row.claim_token = None
            row.claim_until = None
            row.last_error = None
            return True


def apply_failure_state(
    row: EventOutboxEntity,
    *,
    error: str,
    now: datetime,
    allow_dead_letter: bool,
) -> str:
    """Apply retry or dead-letter state after one delivery failure.

    Args:
        row: Locked outbox entity being updated.
        error: Bounded failure description stored for operators.
        now: Current UTC time.
        allow_dead_letter: Whether repeated failure may transition to dead state.

    Returns:
        Resulting state name: retry or dead.
    """
    attempts = int(row.attempts or 0) + 1
    row.attempts = attempts
    row.last_error = error[:1000]
    row.claim_token = None
    row.claim_until = None

    if allow_dead_letter and attempts >= max(1, settings.outbox_max_attempts):
        row.status = "dead"
        row.next_attempt_at = now
        return "dead"

    row.status = "retry"
    row.next_attempt_at = now + timedelta(
        seconds=retry_delay_seconds(attempts)
    )
    return "retry"


async def mark_failed(
    message: OutboxMessage,
    error: str,
    *,
    allow_dead_letter: bool,
) -> str:
    """Persist delivery failure state while the worker still owns the claim.

    Args:
        message: Claimed outbox message.
        error: Bounded failure description.
        allow_dead_letter: Whether repeated poison-message failure may dead-letter it.

    Returns:
        retry, dead or lost_claim.

    Raises:
        Exception: Database failures propagate to the worker loop.
    """
    now = utcnow()
    async with SessionLocal() as session:
        async with session.begin():
            row = await session.get(EventOutboxEntity, message.id, with_for_update=True)
            if row is None or row.claim_token != message.claim_token:
                return "lost_claim"

            return apply_failure_state(
                row,
                error=error,
                now=now,
                allow_dead_letter=allow_dead_letter,
            )


async def cleanup_outbox() -> int:
    """Delete one bounded batch of expired delivered or dead rows.

    Returns:
        Number of outbox rows deleted.

    Raises:
        Exception: Database failures propagate to the worker loop.
    """
    now = utcnow()
    delivered_before = now - timedelta(
        hours=max(1, settings.outbox_delivered_retention_hours)
    )
    dead_before = now - timedelta(days=max(1, settings.outbox_dead_retention_days))
    limit = max(1, settings.outbox_cleanup_batch_size)

    async with SessionLocal() as session:
        async with session.begin():
            ids = list(
                (
                    await session.execute(
                        select(EventOutboxEntity.id)
                        .where(
                            or_(
                                (
                                    (EventOutboxEntity.status == "delivered")
                                    & (EventOutboxEntity.delivered_at < delivered_before)
                                ),
                                (
                                    (EventOutboxEntity.status == "dead")
                                    & (EventOutboxEntity.updated_at < dead_before)
                                ),
                            )
                        )
                        .order_by(EventOutboxEntity.updated_at)
                        .limit(limit)
                    )
                ).scalars().all()
            )
            if not ids:
                return 0
            await session.execute(
                delete(EventOutboxEntity).where(EventOutboxEntity.id.in_(ids))
            )
            return len(ids)


async def outbox_counts() -> dict[str, int]:
    """Return current transactional-outbox counts by known state.

    Returns:
        Dictionary containing pending, retry, dead and delivered counts.

    Raises:
        Exception: Database query failures propagate to the caller.
    """
    counts = {"pending": 0, "retry": 0, "dead": 0, "delivered": 0}
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(EventOutboxEntity.status, func.count(EventOutboxEntity.id))
                .group_by(EventOutboxEntity.status)
            )
        ).all()
    for state, count in rows:
        if state in counts:
            counts[state] = int(count)
    return counts


async def requeue_dead(message_id: str) -> bool:
    """Move one dead-letter message back to retry state.

    Args:
        message_id: Durable outbox message identifier.

    Returns:
        True when a dead message is requeued; False otherwise.

    Raises:
        Exception: Database failures propagate to the caller.
    """
    async with SessionLocal() as session:
        async with session.begin():
            row = await session.get(EventOutboxEntity, message_id, with_for_update=True)
            if row is None or row.status != "dead":
                return False
            row.status = "retry"
            row.attempts = 0
            row.next_attempt_at = utcnow()
            row.claim_token = None
            row.claim_until = None
            row.last_error = None
            return True


class OutboxWorker:
    """Dispatch transactional-outbox messages with claim leases and bounded retries."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_cleanup = 0.0

    async def start(self):
        """Start the background dispatcher when outbox and Kafka are enabled.

        Returns:
            None. Repeated starts are idempotent while a worker task exists.
        """
        if (
            not settings.outbox_enabled
            or not settings.kafka_bootstrap_servers
            or self._task is not None
        ):
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="event-outbox-worker")

    async def stop(self):
        """Cancel and release the background dispatcher task.

        Returns:
            None after the worker task has stopped.
        """
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self) -> dict[str, int]:
        """Claim, publish and persist one bounded dispatch batch.

        Returns:
            Counters for claimed, published, retried, dead and lost-claim messages.

        Raises:
            Exception: Unhandled database or publisher failures propagate.
        """
        messages = await claim_batch()
        published = retried = dead = lost_claim = 0

        for message in messages:
            # A slow broker may make a large batch outlive its initial claim
            # lease. Renew and verify ownership immediately before publish; if
            # another replica already reclaimed it, this worker must not send.
            if not await renew_claim(message):
                lost_claim += 1
                continue
            try:
                await event_publisher.publish_to(
                    message.topic,
                    message.payload,
                    message.key_text.encode("utf-8"),
                    require_available=True,
                )
            except EventPublisherPayloadError as exc:
                state = await mark_failed(
                    message,
                    exc.__class__.__name__,
                    allow_dead_letter=True,
                )
                if state == "retry":
                    retried += 1
                elif state == "dead":
                    dead += 1
                else:
                    lost_claim += 1
                continue
            except EventPublisherUnavailable as exc:
                # Broker/network outages are infrastructure failures, not poison
                # messages. Keep retrying with bounded backoff even after the
                # normal poison-message attempt threshold.
                state = await mark_failed(
                    message,
                    exc.__class__.__name__,
                    allow_dead_letter=False,
                )
                if state == "retry":
                    retried += 1
                else:
                    lost_claim += 1
                continue
            except Exception as exc:
                state = await mark_failed(
                    message,
                    exc.__class__.__name__,
                    allow_dead_letter=True,
                )
                if state == "retry":
                    retried += 1
                elif state == "dead":
                    dead += 1
                else:
                    lost_claim += 1
                continue

            if await mark_delivered(message):
                published += 1
            else:
                lost_claim += 1

        return {
            "claimed": len(messages),
            "published": published,
            "retried": retried,
            "dead": dead,
            "lost_claim": lost_claim,
        }

    async def _run(self):
        while not self._stop.is_set():
            try:
                stats = await self.run_once()
                if any(stats.values()):
                    log.info("outbox_dispatch %s", stats)

                now = asyncio.get_running_loop().time()
                if (
                    now - self._last_cleanup
                    >= max(5.0, settings.outbox_cleanup_interval_seconds)
                ):
                    await cleanup_outbox()
                    self._last_cleanup = now
            except EventPublisherUnavailable:
                log.warning("outbox_broker_unavailable")
            except Exception:
                log.exception("outbox_run_failed")

            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(0.1, settings.outbox_poll_interval_seconds),
                )
            except asyncio.TimeoutError:
                pass


outbox_worker = OutboxWorker()
