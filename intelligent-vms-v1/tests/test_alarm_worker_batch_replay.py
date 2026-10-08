"""Regression tests for alarm-worker batch replay (VMS-FIX-010 / F18).

The consumer is an in-memory fake. getmany advances the fetch position the way
aiokafka does, commit records that position, and seek rewinds it. No production
Kafka broker is used.
"""

import asyncio
import importlib.util
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.entities import AlarmInstanceEntity, AlarmRuleEntity

TOPIC = "vms.events.v1"
MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "alarm-worker" / "main.py"


def _load_worker():
    spec = importlib.util.spec_from_file_location("alarm_worker_main", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORKER = _load_worker()


class _TopicPartition:
    """Hashable stand-in for aiokafka's TopicPartition."""

    def __init__(self, topic, partition):
        self.topic = topic
        self.partition = partition

    def __hash__(self):
        return hash((self.topic, self.partition))

    def __eq__(self, other):
        return isinstance(other, _TopicPartition) and (self.topic, self.partition) == (
            other.topic,
            other.partition,
        )

    def __repr__(self):
        return f"TopicPartition({self.topic}, {self.partition})"


class _Record:
    """One fetched consumer record."""

    def __init__(self, topic, partition, offset, value):
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.value = value


class FakeConsumer:
    """Fake manual-commit consumer.

    getmany returns available records and advances the partition position to
    one past the last returned offset. commit() with no offsets records that
    current position, which is how a later successful batch can skip a failed
    one. seek() puts the position back so the next getmany can replay.
    """

    def __init__(self, trace, max_polls=8, on_poll=None):
        self.trace = trace
        self.max_polls = max_polls
        self.on_poll = on_poll
        self.logs = {}
        self.positions = {}
        self.committed = []
        self.seeks = []
        self.polls = 0
        self.started = False
        self.stopped = False
        self.constructor_args = ()
        self.constructor_kwargs = {}

    def add(self, record):
        partition = _TopicPartition(record.topic, record.partition)
        self.logs.setdefault(partition, []).append(record)
        self.positions.setdefault(partition, record.offset)

    def bind_constructor(self, *args, **kwargs):
        self.constructor_args = args
        self.constructor_kwargs = kwargs
        return self

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def getmany(self, timeout_ms=0, max_records=None):
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll(self)
        if self.polls > self.max_polls:
            raise asyncio.CancelledError()
        remaining = max_records or 10**9
        fetched = {}
        for partition, records in self.logs.items():
            position = self.positions.get(partition, 0)
            available = sorted(
                (record for record in records if record.offset >= position),
                key=lambda record: record.offset,
            )
            taken = available[:remaining]
            if not taken:
                continue
            fetched[partition] = taken
            self.positions[partition] = taken[-1].offset + 1
            remaining -= len(taken)
            if remaining <= 0:
                break
        return fetched

    async def commit(self, offsets=None):
        if offsets is None:
            snapshot = dict(self.positions)
            mode = "position"
        else:
            snapshot = {}
            for partition, value in offsets.items():
                snapshot[partition] = value[0] if isinstance(value, tuple) else value
            mode = "explicit"
        stored = {"mode": mode, "offsets": snapshot}
        self.committed.append(stored)
        self.trace.add("commit", mode=mode, offsets=_readable_offsets(snapshot))

    def seek(self, partition, offset):
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("Offset must be a positive integer")
        self.seeks.append((partition, offset))
        self.positions[partition] = offset
        self.trace.add("seek", partition=partition.partition, offset=offset)


class _Trace:
    def __init__(self):
        self.events = []

    def add(self, kind, **details):
        self.events.append((kind, details))

    def render(self):
        return "\n".join(f"{kind} {details}" for kind, details in self.events)


def _readable_offsets(offsets):
    return {f"{partition.topic}:{partition.partition}": offset for partition, offset in offsets.items()}


def _event(event_id, camera_id="cam-1"):
    return {
        "event_id": event_id,
        "tenant_id": "tenant-a",
        "site_id": "site-1",
        "camera_id": camera_id,
        "timestamp": "2026-09-24T10:00:05+00:00",
        "event_type": "motion",
        "severity": "medium",
        "attributes": {},
    }


def _record(partition, offset, value):
    return _Record(TOPIC, partition, offset, value)


def _committed_offset(entry, partition):
    for topic_partition, offset in entry["offsets"].items():
        if topic_partition.partition == partition:
            return offset
    return None


async def _drive(monkeypatch, tmp_path, records, fail_once=(), always_fail=(), on_poll=None, poison_limit=None, max_polls=8):
    """Run the worker loop against the fake consumer until it stops itself."""
    trace = _Trace()
    attempts = {}
    slept = []
    consumer = FakeConsumer(trace, max_polls=max_polls, on_poll=on_poll)
    for record in records:
        consumer.add(record)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alarm-replay.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(
            AlarmRuleEntity(
                id="rule-1",
                tenant_id="tenant-a",
                site_id="site-1",
                name="Motion",
                enabled=True,
                event_types_json=["motion"],
                severities_json=[],
                camera_ids_json=[],
                alarm_severity="high",
                cooldown_seconds=0,
            )
        )
        await session.commit()

    tripped = set()
    original_create = WORKER.create_alarm

    async def create_alarm(rule, event):
        event_id = str(event.get("event_id", ""))
        attempts[event_id] = attempts.get(event_id, 0) + 1
        if event_id in always_fail:
            trace.add("apply", event_id=event_id, outcome="raised")
            raise RuntimeError("synthetic poison record")
        if event_id in fail_once and event_id not in tripped:
            tripped.add(event_id)
            trace.add("apply", event_id=event_id, outcome="raised")
            raise RuntimeError("synthetic alarm database outage")
        created = await original_create(rule, event)
        trace.add("apply", event_id=event_id, outcome="created" if created else "deduped")
        return created

    async def fast_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(WORKER, "AIOKafkaConsumer", consumer.bind_constructor)
    monkeypatch.setattr(WORKER, "SessionLocal", sessions)
    monkeypatch.setattr(WORKER, "create_alarm", create_alarm)
    monkeypatch.setattr(WORKER.asyncio, "sleep", fast_sleep)
    if poison_limit is not None:
        monkeypatch.setattr(WORKER, "POISON_RETRY_LIMIT", poison_limit, raising=False)

    try:
        try:
            await WORKER.main()
        except asyncio.CancelledError:
            trace.add("cancelled")
        async with sessions() as session:
            stored = (await session.execute(select(AlarmInstanceEntity))).scalars().all()
    finally:
        await engine.dispose()

    rows = {}
    for row in stored:
        rows[row.event_id] = rows.get(row.event_id, 0) + 1
    return {
        "consumer": consumer,
        "attempts": attempts,
        "rows": rows,
        "slept": slept,
        "trace": trace,
    }


def _arrive_on_second_poll(partition, offset, value):
    state = {"added": False}

    def on_poll(consumer):
        if consumer.polls == 2 and not state["added"]:
            state["added"] = True
            consumer.add(_record(partition, offset, value))

    return on_poll


def test_failed_batch_is_not_committed_past_and_is_replayed(monkeypatch, tmp_path):
    """A later successful record must not commit a batch that failed before it."""
    result = asyncio.run(
        _drive(
            monkeypatch,
            tmp_path,
            records=[
                _record(0, 0, _event("event-0")),
                _record(0, 1, _event("event-1")),
            ],
            fail_once=("event-1",),
            on_poll=_arrive_on_second_poll(0, 2, _event("event-2")),
        )
    )
    text = result["trace"].render()
    created = set()
    committed_past_failure = False
    for kind, details in result["trace"].events:
        if kind == "apply" and details["outcome"] == "created" and details["event_id"] == "event-1":
            created.add("event-1")
        if kind == "commit":
            committed = details["offsets"].get(f"{TOPIC}:0")
            if committed is not None and committed > 1 and "event-1" not in created:
                committed_past_failure = True
    assert not committed_past_failure, f"committed past failed event-1 before it was processed:\n{text}"
    assert result["attempts"].get("event-1", 0) >= 2, f"failed event-1 was not replayed:\n{text}"
    assert any(partition.partition == 0 and offset <= 1 for partition, offset in result["consumer"].seeks), (
        f"consumer was not rewound to the failed batch:\n{text}"
    )
    assert result["rows"].get("event-1") == 1, f"replay did not recover event-1: rows={result['rows']}\n{text}"
    assert result["rows"].get("event-2") == 1, f"record after the failed batch was lost: rows={result['rows']}\n{text}"


def test_replayed_event_does_not_open_a_second_alarm(monkeypatch, tmp_path):
    """Reprocessing a record after rewind must not mint a second alarm row."""
    result = asyncio.run(
        _drive(
            monkeypatch,
            tmp_path,
            records=[
                _record(0, 0, _event("event-0")),
                _record(0, 1, _event("event-1")),
            ],
            fail_once=("event-1",),
            on_poll=_arrive_on_second_poll(0, 2, _event("event-2")),
        )
    )
    text = result["trace"].render()
    assert result["attempts"].get("event-0", 0) >= 2, (
        f"recovery did not replay event-0; attempts={result['attempts']} rows={result['rows']}\n{text}"
    )
    assert result["rows"].get("event-0") == 1, (
        f"replay opened a second alarm for event-0; attempts={result['attempts']} rows={result['rows']}\n{text}"
    )
    deduped = [
        details
        for kind, details in result["trace"].events
        if kind == "apply" and details["event_id"] == "event-0" and details["outcome"] == "deduped"
    ]
    assert deduped, f"replayed event-0 was not reported as a dedupe:\n{text}"


def test_other_partition_success_does_not_commit_failed_partition(monkeypatch, tmp_path):
    """A successful partition must not commit another partition's failed records."""
    result = asyncio.run(
        _drive(
            monkeypatch,
            tmp_path,
            records=[
                _record(0, 0, _event("event-a")),
                _record(1, 0, _event("event-b")),
            ],
            fail_once=("event-b",),
            on_poll=_arrive_on_second_poll(0, 1, _event("event-c")),
        )
    )
    text = result["trace"].render()
    created = set()
    for kind, details in result["trace"].events:
        if kind == "apply" and details["outcome"] == "created":
            created.add(details["event_id"])
        if kind == "commit" and details["offsets"].get(f"{TOPIC}:1", 0) > 0:
            assert "event-b" in created, f"committed partition 1 past failed event-b:\n{text}"
    assert "event-b" in created, f"failed partition was not replayed: rows={result['rows']}\n{text}"
    rewound = {partition.partition for partition, offset in result["consumer"].seeks if offset == 0}
    assert rewound >= {0, 1}, f"both partitions in the failed fetch were not rewound: {result['consumer'].seeks}\n{text}"
    assert result["rows"].get("event-b") == 1
    assert result["rows"].get("event-a") == 1
    assert result["rows"].get("event-c") == 1


def test_poison_record_is_bounded_and_observable(monkeypatch, tmp_path, caplog):
    """One permanently bad record is skipped after a fixed number of attempts."""
    caplog.set_level(logging.INFO, logger="alarm-worker")
    result = asyncio.run(
        _drive(
            monkeypatch,
            tmp_path,
            records=[
                _record(0, 0, _event("event-poison")),
                _record(0, 1, _event("event-good")),
            ],
            always_fail=("event-poison",),
            on_poll=_arrive_on_second_poll(0, 2, _event("event-later")),
            poison_limit=3,
        )
    )
    text = result["trace"].render()
    problems = []
    if result["attempts"].get("event-poison") != 3:
        problems.append(f"poison attempts={result['attempts'].get('event-poison')} expected 3")
    if "alarm_poison_record" not in caplog.text:
        problems.append("missing alarm_poison_record log")
    elif "offset=0" not in caplog.text:
        problems.append("poison log did not identify offset 0")
    if result["rows"].get("event-good") != 1:
        problems.append(f"good record was not processed: rows={result['rows']}")
    if result["rows"].get("event-later") != 1:
        problems.append(f"later record was not processed: rows={result['rows']}")
    if len(result["consumer"].seeks) != 2:
        problems.append(f"expected 2 rewinds before the poison skip, seeks={result['consumer'].seeks}")
    if result["rows"].get("event-poison", 0) != 0:
        problems.append(f"poison record opened an alarm: rows={result['rows']}")
    commits = result["consumer"].committed
    if not commits or _committed_offset(commits[-1], 0) != 3:
        problems.append(f"expected a final commit at offset 3, commits={commits}")
    assert not problems, "\n".join(problems) + "\n" + text + "\n" + caplog.text


def test_non_object_payload_is_bounded_poison(monkeypatch, tmp_path, caplog):
    """A payload that is not an event object is a bounded, logged poison record."""
    caplog.set_level(logging.INFO, logger="alarm-worker")
    result = asyncio.run(
        _drive(
            monkeypatch,
            tmp_path,
            records=[
                _record(0, 0, ["not", "an", "event"]),
                _record(0, 1, _event("event-good")),
            ],
            poison_limit=2,
        )
    )
    text = result["trace"].render()
    problems = []
    if "alarm_poison_record" not in caplog.text:
        problems.append("missing alarm_poison_record log")
    if result["rows"].get("event-good") != 1:
        problems.append(f"following record was not processed: rows={result['rows']}")
    if len(result["consumer"].seeks) != 1:
        problems.append(f"expected 1 rewind before the poison skip, seeks={result['consumer'].seeks}")
    commits = result["consumer"].committed
    if not commits or _committed_offset(commits[-1], 0) != 2:
        problems.append(f"expected a final commit at offset 2, commits={commits}")
    assert not problems, "\n".join(problems) + "\n" + text + "\n" + caplog.text


def test_successful_batch_commits_without_rewind(monkeypatch, tmp_path):
    """A fully processed batch still commits its next offsets and does not rewind."""
    result = asyncio.run(
        _drive(
            monkeypatch,
            tmp_path,
            records=[
                _record(0, 0, _event("event-0")),
                _record(0, 1, _event("event-1")),
            ],
        )
    )
    text = result["trace"].render()
    consumer = result["consumer"]
    assert consumer.constructor_kwargs["enable_auto_commit"] is False
    assert consumer.seeks == [], text
    assert result["attempts"] == {"event-0": 1, "event-1": 1}, result["attempts"]
    assert result["rows"] == {"event-0": 1, "event-1": 1}, result["rows"]
    assert len(consumer.committed) == 1, consumer.committed
    assert _committed_offset(consumer.committed[0], 0) == 2, consumer.committed
