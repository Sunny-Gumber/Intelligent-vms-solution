"""Revision-safe regional spool acknowledgement (VMS-FIX-007 / F08).

Heartbeats coalesce under one spool id. An acknowledgement of a revision that
was already read for sending must not delete, reschedule, or dead-letter a
newer body stored under that same id.
"""

import asyncio
import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "services" / "regional-spool" / "main.py"
HEARTBEAT_ID = "heartbeat:node-a"
LEGACY_CREATED_AT = 1_700_000_000.0
LEGACY_UPDATED_AT = 1_700_000_100.0
LEGACY_FAILED_AT = 1_700_000_200.0
LEGACY_BODY = '{"load":{"active_sources":4}}'
LEGACY_DEAD_BODY = '{"event_id":"legacy"}'
NEWER_BODY = '{"load":{"active_sources":9}}'


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
    name = f"regional_spool_fix007_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class Client:
    def __init__(self, *, statuses=None):
        self.statuses = list(statuses or [202])
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status = self.statuses.pop(0) if self.statuses else 202
        return Response(status)


class ReplaceWhileSending:
    """Enqueue a newer heartbeat while the older POST is still in flight."""

    def __init__(self, store, status_code):
        self.store = store
        self.status_code = status_code
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.store.enqueue(
            HEARTBEAT_ID,
            "heartbeat",
            {"load": {"active_sources": 9}},
            coalesce=True,
        )
        return Response(self.status_code)


def _queued_row(db_path, item_id):
    with sqlite3.connect(db_path) as db:
        return db.execute(
            """
            SELECT body_json, attempts, next_attempt_at
            FROM spool_items
            WHERE id=?
            """,
            (item_id,),
        ).fetchone()


@pytest.mark.parametrize(
    "status_code",
    [
        pytest.param(202, id="success"),
        pytest.param(503, id="retry"),
        pytest.param(422, id="dead_letter"),
    ],
)
def test_inflight_ack_keeps_newer_heartbeat_sendable(tmp_path, monkeypatch, status_code):
    """Success, retry, and dead-letter of the in-flight revision keep the newer body."""
    mod = load_spool(tmp_path, monkeypatch)
    db_path = tmp_path / "spool.db"
    mod.store.enqueue(
        HEARTBEAT_ID,
        "heartbeat",
        {"load": {"active_sources": 1}},
        coalesce=True,
    )
    inflight = mod.store.due(10)
    assert len(inflight) == 1
    assert inflight[0]["body"]["load"]["active_sources"] == 1
    old_revision = inflight[0].get("revision")

    client = ReplaceWhileSending(mod.store, status_code)
    asyncio.run(mod.flush_once(client))

    assert len(client.calls) == 1
    assert client.calls[0][1]["json"]["load"]["active_sources"] == 1
    assert _queued_row(db_path, HEARTBEAT_ID) == (NEWER_BODY, 0, 0.0)
    assert isinstance(old_revision, int)
    assert mod.store.counts() == (1, 0)

    pending = mod.store.due(10)
    assert len(pending) == 1
    assert pending[0]["body"]["load"]["active_sources"] == 9
    assert pending[0]["revision"] == old_revision + 1
    assert pending[0]["attempts"] == 0

    follow_up = Client(statuses=[202])
    delivered = asyncio.run(mod.flush_once(follow_up))
    assert delivered == 1
    assert follow_up.calls[0][1]["json"]["load"]["active_sources"] == 9
    assert mod.store.counts() == (0, 0)


def test_direct_stale_revision_ack_leaves_newer_body(tmp_path, monkeypatch):
    """Each Store acknowledgement is a no-op when the stored revision moved on."""
    mod = load_spool(tmp_path, monkeypatch)
    db_path = tmp_path / "spool.db"
    mod.store.enqueue(
        HEARTBEAT_ID,
        "heartbeat",
        {"load": {"active_sources": 1}},
        coalesce=True,
    )
    inflight = mod.store.due(10)
    old_revision = inflight[0]["revision"]
    mod.store.enqueue(
        HEARTBEAT_ID,
        "heartbeat",
        {"load": {"active_sources": 9}},
        coalesce=True,
    )

    mod.store.success(HEARTBEAT_ID, old_revision)
    mod.store.retry(HEARTBEAT_ID, "HTTP 503", 30.0, old_revision)
    mod.store.dead_letter(HEARTBEAT_ID, 422, "HTTP 422", old_revision)

    assert _queued_row(db_path, HEARTBEAT_ID) == (NEWER_BODY, 0, 0.0)
    pending = mod.store.due(10)
    assert len(pending) == 1
    assert pending[0]["revision"] == old_revision + 1
    assert pending[0]["body"]["load"]["active_sources"] == 9
    assert mod.store.counts() == (1, 0)

    follow_up = Client(statuses=[202])
    delivered = asyncio.run(mod.flush_once(follow_up))
    assert delivered == 1
    assert follow_up.calls[0][1]["json"]["load"]["active_sources"] == 9
    assert mod.store.counts() == (0, 0)


def test_legacy_spool_database_upgrades_in_place(tmp_path, monkeypatch):
    """Opening a pre-revision spool adds revision and keeps every existing row."""
    mod = load_spool(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_path) as db:
        db.executescript(
            """
            CREATE TABLE spool_items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                body_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX ix_spool_due
                ON spool_items(next_attempt_at, created_at);
            CREATE TABLE dead_letters (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                body_json TEXT NOT NULL,
                status_code INTEGER,
                reason TEXT,
                created_at REAL NOT NULL,
                failed_at REAL NOT NULL
            );
            """
        )
        db.execute(
            """
            INSERT INTO spool_items(
                id, kind, body_json, attempts, next_attempt_at,
                last_error, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                HEARTBEAT_ID,
                "heartbeat",
                LEGACY_BODY,
                3,
                0.0,
                "HTTP 503",
                LEGACY_CREATED_AT,
                LEGACY_UPDATED_AT,
            ),
        )
        db.execute(
            """
            INSERT INTO dead_letters(
                id, kind, body_json, status_code, reason, created_at, failed_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "event:legacy",
                "event",
                LEGACY_DEAD_BODY,
                409,
                "kept",
                LEGACY_CREATED_AT,
                LEGACY_FAILED_AT,
            ),
        )
        queued_rowid = db.execute(
            "SELECT rowid FROM spool_items WHERE id=?",
            (HEARTBEAT_ID,),
        ).fetchone()[0]
        dead_rowid = db.execute(
            "SELECT rowid FROM dead_letters WHERE id=?",
            ("event:legacy",),
        ).fetchone()[0]

    upgraded = mod.Store(str(legacy_path), 100)
    reopened = mod.Store(str(legacy_path), 100)

    with sqlite3.connect(legacy_path) as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        columns = [
            row[1] for row in db.execute("PRAGMA table_info(spool_items)").fetchall()
        ]
        stored = db.execute(
            """
            SELECT rowid, kind, body_json, attempts, next_attempt_at,
                   last_error, created_at, updated_at, revision
            FROM spool_items WHERE id=?
            """,
            (HEARTBEAT_ID,),
        ).fetchone()
        dead = db.execute(
            """
            SELECT rowid, kind, body_json, status_code, reason, created_at, failed_at
            FROM dead_letters WHERE id=?
            """,
            ("event:legacy",),
        ).fetchone()

    assert tables == ["dead_letters", "spool_items"]
    assert columns[:8] == [
        "id",
        "kind",
        "body_json",
        "attempts",
        "next_attempt_at",
        "last_error",
        "created_at",
        "updated_at",
    ]
    assert columns[-1] == "revision"
    assert stored == (
        queued_rowid,
        "heartbeat",
        LEGACY_BODY,
        3,
        0.0,
        "HTTP 503",
        LEGACY_CREATED_AT,
        LEGACY_UPDATED_AT,
        1,
    )
    assert dead == (
        dead_rowid,
        "event",
        LEGACY_DEAD_BODY,
        409,
        "kept",
        LEGACY_CREATED_AT,
        LEGACY_FAILED_AT,
    )

    pending = reopened.due(10)
    assert len(pending) == 1
    assert pending[0]["id"] == HEARTBEAT_ID
    assert pending[0]["revision"] == 1
    assert pending[0]["attempts"] == 3
    assert pending[0]["body"]["load"]["active_sources"] == 4
    assert upgraded.counts() == (1, 1)

    old_revision = pending[0]["revision"]
    reopened.enqueue(
        HEARTBEAT_ID,
        "heartbeat",
        {"load": {"active_sources": 9}},
        coalesce=True,
    )
    reopened.success(HEARTBEAT_ID, old_revision)
    reopened.retry(HEARTBEAT_ID, "HTTP 503", 30.0, old_revision)
    reopened.dead_letter(HEARTBEAT_ID, 422, "HTTP 422", old_revision)

    with sqlite3.connect(legacy_path) as db:
        after = db.execute(
            """
            SELECT body_json, attempts, next_attempt_at, created_at, revision
            FROM spool_items WHERE id=?
            """,
            (HEARTBEAT_ID,),
        ).fetchone()
        dead_count = db.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
    assert after == (NEWER_BODY, 3, 0.0, LEGACY_CREATED_AT, old_revision + 1)
    assert dead_count == 1

    ready = reopened.due(10)
    assert len(ready) == 1
    assert ready[0]["body"]["load"]["active_sources"] == 9
    send = Client(statuses=[202])
    response = asyncio.run(
        send.post(
            "http://control-api:8000/api/v1/infrastructure/nodes/node-a/heartbeat",
            json=ready[0]["body"],
        )
    )
    assert response.status_code == 202
    reopened.success(ready[0]["id"], ready[0]["revision"])
    assert send.calls[0][1]["json"]["load"]["active_sources"] == 9
    assert reopened.counts() == (0, 1)


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_acknowledgement_rejects_revision_that_was_not_read(
    tmp_path, monkeypatch, revision
):
    """Success, retry, and dead-letter refuse a revision the store did not issue."""
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue("event:e1", "event", {"event_id": "e1"})
    with pytest.raises(ValueError):
        mod.store.success("event:e1", revision)
    with pytest.raises(ValueError):
        mod.store.retry("event:e1", "HTTP 503", 1.0, revision)
    with pytest.raises(ValueError):
        mod.store.dead_letter("event:e1", 422, "invalid", revision)
    assert mod.store.counts() == (1, 0)
