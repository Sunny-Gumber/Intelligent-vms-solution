import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.entities import EventOutboxEntity
from app.services import outbox as outbox_service
from app.services.outbox import (
    OutboxMessage,
    OutboxPayloadTooLarge,
    OutboxWorker,
    apply_failure_state,
    claim_statement,
    enqueue_event,
    enqueue_event_once,
    enqueue_message_once,
    retry_delay_seconds,
)
from tests.time_control import FIXED_NOW


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    """Freeze outbox timestamps while preserving explicit retry inputs."""
    monkeypatch.setattr(outbox_service, "utcnow", lambda: FIXED_NOW)


class AddSession:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)


def event():
    return {
        "event_id": "event-1",
        "tenant_id": "tenant-a",
        "site_id": "site-a",
        "camera_id": "cam-1",
        "timestamp": FIXED_NOW.isoformat(),
        "event_type": "camera_offline",
        "source": "vms",
        "severity": "high",
        "attributes": {},
    }


def outbox_row(*, attempts=0):
    return EventOutboxEntity(
        id="event:event-1",
        topic="vms.events.v1",
        key_text="tenant-a:cam-1",
        payload_json=event(),
        status="retry",
        attempts=attempts,
        next_attempt_at=FIXED_NOW,
        claim_token="claim-1",
        claim_until=FIXED_NOW + timedelta(seconds=30),
    )


def test_enqueue_event_uses_deterministic_message_identity():
    session = AddSession()
    row = enqueue_event(session, event())
    assert row.id == "event:event-1"
    assert row.topic == settings.kafka_topic_events
    assert row.key_text == "tenant-a:cam-1"
    assert session.added == [row]


def test_claim_query_uses_skip_locked_for_multi_replica_safety():
    stmt = claim_statement(FIXED_NOW, 100)
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "CLAIM_UNTIL" in sql
    assert "NEXT_ATTEMPT_AT" in sql
    assert "LIMIT 100" in sql


def test_broker_outage_never_dead_letters_valid_message(monkeypatch):
    old = settings.outbox_max_attempts
    settings.outbox_max_attempts = 3
    monkeypatch.setattr("app.services.outbox.random.uniform", lambda *_args: 0.0)
    try:
        row = outbox_row(attempts=50)
        state = apply_failure_state(
            row,
            error="EventPublisherUnavailable",
            now=FIXED_NOW,
            allow_dead_letter=False,
        )
        assert state == "retry"
        assert row.status == "retry"
        assert row.attempts == 51
        assert row.claim_token is None
        assert row.claim_until is None
    finally:
        settings.outbox_max_attempts = old


def test_poison_message_reaches_dead_letter_at_threshold(monkeypatch):
    old = settings.outbox_max_attempts
    settings.outbox_max_attempts = 3
    monkeypatch.setattr("app.services.outbox.random.uniform", lambda *_args: 0.0)
    try:
        row = outbox_row(attempts=2)
        state = apply_failure_state(
            row,
            error="EventPublisherPayloadError",
            now=FIXED_NOW,
            allow_dead_letter=True,
        )
        assert state == "dead"
        assert row.status == "dead"
        assert row.attempts == 3
    finally:
        settings.outbox_max_attempts = old


def test_retry_backoff_is_bounded(monkeypatch):
    old = settings.outbox_backoff_max_seconds
    settings.outbox_backoff_max_seconds = 30.0
    monkeypatch.setattr("app.services.outbox.random.uniform", lambda *_args: 0.0)
    try:
        assert retry_delay_seconds(1) == 2.0
        assert retry_delay_seconds(100) == 30.0
    finally:
        settings.outbox_backoff_max_seconds = old


def test_claim_lease_allows_crash_recovery_after_publish_before_ack():
    now = FIXED_NOW
    stmt = claim_statement(now, 10)
    text = str(stmt)
    # The claim predicate includes expired claims, which means a worker crash
    # after Kafka publish but before delivered-marking is eventually replayed.
    assert "claim_until" in text
    assert "next_attempt_at" in text



def test_worker_never_publishes_after_claim_is_lost(monkeypatch):
    message = OutboxMessage(
        id="event:event-1",
        topic="vms.events.v1",
        key_text="tenant-a:cam-1",
        payload=event(),
        attempts=0,
        claim_token="claim-old",
    )

    async def fake_claim_batch():
        return [message]

    async def fake_renew_claim(_message):
        return False

    async def should_not_publish(*_args, **_kwargs):
        raise AssertionError("lost claim must not publish")

    monkeypatch.setattr("app.services.outbox.claim_batch", fake_claim_batch)
    monkeypatch.setattr("app.services.outbox.renew_claim", fake_renew_claim)
    monkeypatch.setattr(
        "app.services.outbox.event_publisher.publish_to",
        should_not_publish,
    )

    result = asyncio.run(OutboxWorker().run_once())
    assert result["lost_claim"] == 1
    assert result["published"] == 0



def test_idempotent_event_enqueue_keeps_one_row_under_retry():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(EventOutboxEntity.__table__.create)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            first = await enqueue_event_once(session, event())
            second = await enqueue_event_once(session, event())
            await session.commit()
            count = int(
                (
                    await session.execute(
                        select(func.count(EventOutboxEntity.id))
                    )
                ).scalar_one()
            )

        await engine.dispose()
        return first, second, count

    first, second, count = asyncio.run(scenario())
    assert first is True
    assert second is False
    assert count == 1


def test_idempotent_recording_enqueue_keeps_one_row_under_retry():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(EventOutboxEntity.__table__.create)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        payload = {
            "segment_id": "segment-1",
            "tenant_id": "tenant-a",
            "site_id": "site-a",
            "camera_id": "cam-1",
            "recording_node_id": "node-a",
            "record_stream_key": "cam-1-record",
            "segment_path": "/recordings/1.mp4",
            "segment_start": FIXED_NOW.isoformat(),
            "duration_seconds": 60.0,
            "completed_at": FIXED_NOW.isoformat(),
            "storage_tier": "hot",
            "object_uri": None,
        }

        async with Session() as session:
            first = await enqueue_message_once(
                session,
                message_id="recording:segment-1",
                topic=settings.kafka_topic_recordings,
                key_text="tenant-a:cam-1",
                payload=payload,
            )
            second = await enqueue_message_once(
                session,
                message_id="recording:segment-1",
                topic=settings.kafka_topic_recordings,
                key_text="tenant-a:cam-1",
                payload=payload,
            )
            await session.commit()
            count = int(
                (
                    await session.execute(
                        select(func.count(EventOutboxEntity.id))
                    )
                ).scalar_one()
            )

        await engine.dispose()
        return first, second, count

    first, second, count = asyncio.run(scenario())
    assert first is True
    assert second is False
    assert count == 1



def test_outbox_payload_ceiling_rejects_oversized_message():
    old = settings.outbox_max_payload_bytes
    settings.outbox_max_payload_bytes = 1024
    try:
        oversized = event()
        oversized["attributes"] = {"blob": "x" * 4096}
        with pytest.raises(OutboxPayloadTooLarge):
            enqueue_event(AddSession(), oversized)
    finally:
        settings.outbox_max_payload_bytes = old
