import asyncio
import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "services" / "regional-spool" / "main.py"


def load_spool(tmp_path, monkeypatch):
    db = tmp_path / "spool.db"
    monkeypatch.setenv("CONTROL_API_URL", "http://control-api:8000")
    monkeypatch.setenv("NODE_ID", "node-a")
    monkeypatch.setenv("NODE_AGENT_TOKEN", "node-secret")
    monkeypatch.setenv("RECORDING_HOOK_TOKEN", "record-secret")
    monkeypatch.setenv("REGIONAL_SPOOL_TOKEN", "spool-secret")
    monkeypatch.setenv("SPOOL_DB_PATH", str(db))
    monkeypatch.setenv("SPOOL_MAX_ITEMS", "100")
    monkeypatch.setenv("SPOOL_BACKOFF_MAX_SECONDS", "5")
    name = f"regional_spool_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class Client:
    def __init__(self, *, statuses=None, error=None):
        self.statuses = list(statuses or [202])
        self.error = error
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        status = self.statuses.pop(0) if self.statuses else 202
        return Response(status)


def test_spool_survives_process_restart(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue(
        "event:e1",
        "event",
        {"event_id": "e1", "camera_id": "cam-1"},
    )

    reopened = mod.Store(str(tmp_path / "spool.db"), 100)
    items = reopened.due(10)
    assert len(items) == 1
    assert items[0]["id"] == "event:e1"
    assert items[0]["body"]["camera_id"] == "cam-1"


def test_event_id_is_locally_idempotent(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    body = {"event_id": "e1", "camera_id": "cam-1"}
    mod.store.enqueue("event:e1", "event", body)
    mod.store.enqueue("event:e1", "event", body)
    queued, dead = mod.store.counts()
    assert (queued, dead) == (1, 0)


def test_heartbeat_coalesces_to_latest_state(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue(
        "heartbeat:node-a",
        "heartbeat",
        {"load": {"active_sources": 1}},
        coalesce=True,
    )
    mod.store.enqueue(
        "heartbeat:node-a",
        "heartbeat",
        {"load": {"active_sources": 9}},
        coalesce=True,
    )
    items = mod.store.due(10)
    assert len(items) == 1
    assert items[0]["body"]["load"]["active_sources"] == 9


def test_successful_backfill_removes_item(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue(
        "event:e1",
        "event",
        {"event_id": "e1", "camera_id": "cam-1"},
    )
    client = Client(statuses=[202])
    delivered = asyncio.run(mod.flush_once(client))
    assert delivered == 1
    assert mod.store.counts() == (0, 0)
    assert client.calls[0][0].endswith("/internal/v1/events/ingest")


def test_network_failure_retains_item_with_backoff(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue(
        "event:e1",
        "event",
        {"event_id": "e1", "camera_id": "cam-1"},
    )
    asyncio.run(mod.flush_once(Client(error=RuntimeError("wan down"))))

    with sqlite3.connect(tmp_path / "spool.db") as db:
        row = db.execute(
            "SELECT attempts, next_attempt_at, last_error FROM spool_items WHERE id=?",
            ("event:e1",),
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] > 0
    assert row[2] == "RuntimeError"


def test_stale_recording_rejection_moves_to_dead_letter(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    body = {
        "path": "cam-1-record",
        "segment_path": "/recordings/100.mp4",
        "duration": "60s",
        "recording_node_id": "node-a",
        "assignment_generation": 1,
    }
    item_id = mod._recording_id(body)
    mod.store.enqueue(item_id, "recording", body)

    asyncio.run(mod.flush_once(Client(statuses=[409])))

    assert mod.store.counts() == (0, 1)
    with sqlite3.connect(tmp_path / "spool.db") as db:
        row = db.execute(
            "SELECT status_code FROM dead_letters WHERE id=?",
            (item_id,),
        ).fetchone()
    assert row == (409,)


def test_heartbeat_404_is_retried_not_dead_lettered(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue(
        "heartbeat:node-a",
        "heartbeat",
        {"load": {"active_sources": 2}},
        coalesce=True,
    )
    asyncio.run(mod.flush_once(Client(statuses=[404])))
    queued, dead = mod.store.counts()
    assert (queued, dead) == (1, 0)


def test_recording_spool_id_is_deterministic(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    body = {
        "path": "cam-1-record",
        "segment_path": "/recordings/100.mp4",
        "duration": "60s",
        "recording_node_id": "node-a",
        "assignment_generation": 3,
    }
    assert mod._recording_id(body) == mod._recording_id(dict(reversed(list(body.items()))))


def test_spool_fails_closed_when_capacity_is_full(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    tiny = mod.Store(str(tmp_path / "tiny.db"), 1)
    tiny.max_items = 1
    tiny.enqueue("event:1", "event", {"event_id": "1"})
    with pytest.raises(mod.SpoolFull):
        tiny.enqueue("event:2", "event", {"event_id": "2"})


def test_heartbeat_coalescing_preserves_existing_backoff(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue(
        "heartbeat:node-a",
        "heartbeat",
        {"load": {"active_sources": 1}},
        coalesce=True,
    )
    asyncio.run(mod.flush_once(Client(statuses=[503])))

    with sqlite3.connect(tmp_path / "spool.db") as db:
        before = db.execute(
            "SELECT attempts, next_attempt_at FROM spool_items WHERE id=?",
            ("heartbeat:node-a",),
        ).fetchone()

    mod.store.enqueue(
        "heartbeat:node-a",
        "heartbeat",
        {"load": {"active_sources": 7}},
        coalesce=True,
    )

    with sqlite3.connect(tmp_path / "spool.db") as db:
        after = db.execute(
            "SELECT attempts, next_attempt_at, body_json FROM spool_items WHERE id=?",
            ("heartbeat:node-a",),
        ).fetchone()

    assert before[0] == 1
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert '"active_sources":7' in after[2]


def test_spool_rejects_oversized_payload(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    tiny = mod.Store(
        str(tmp_path / "payload.db"),
        100,
        max_body_bytes=32,
    )
    with pytest.raises(mod.SpoolPayloadTooLarge):
        tiny.enqueue(
            "event:large",
            "event",
            {"event_id": "large", "blob": "x" * 200},
        )


def test_dead_letters_are_bounded(tmp_path, monkeypatch):
    mod = load_spool(tmp_path, monkeypatch)
    bounded = mod.Store(
        str(tmp_path / "dead.db"),
        100,
        max_dead_letters=2,
    )
    for i in range(3):
        item_id = f"event:{i}"
        bounded.enqueue(item_id, "event", {"event_id": str(i)})
        due = bounded.due(1)
        assert len(due) == 1
        assert due[0]["id"] == item_id
        bounded.dead_letter(due[0]["id"], 422, "invalid", due[0]["revision"])
    assert bounded.counts() == (0, 2)
