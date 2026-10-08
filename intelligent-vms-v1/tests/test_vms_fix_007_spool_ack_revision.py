"""Revision-safe regional spool acknowledgement (VMS-FIX-007 / F08).

Heartbeats coalesce under one spool id. An acknowledgement of a revision that
was already read for sending must not delete, reschedule, or dead-letter a
newer body stored under that same id.
"""

import asyncio
import importlib.util
import logging
import sqlite3
import threading
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

    assert tables == ["dead_letters", "spool_items", "spool_revision_marks"]
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


OLD_BODY = '{"load":{"active_sources":1}}'


class _ExecuteProxy:
    """Connection stand-in that can observe SQL before SQLite runs it.

    sqlite3.Connection.execute is read-only, so tests wrap the connection
    instead of replacing that method.
    """

    def __init__(self, connection, before_execute):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_before_execute", before_execute)

    def execute(self, sql, *params):
        self._before_execute(sql)
        return self._connection.execute(sql, *params)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._connection.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)


def _write_legacy_spool(path: Path) -> None:
    with sqlite3.connect(path) as db:
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


def _apply_old_revision(store, operation, item_id, revision):
    if operation == "success":
        store.success(item_id, revision)
    elif operation == "retry":
        store.retry(item_id, "HTTP 503", 30.0, revision)
    else:
        store.dead_letter(item_id, 422, "HTTP 422", revision)


@pytest.mark.parametrize("operation", ["success", "retry", "dead_letter"])
def test_qa007_coalesce_gap_keeps_newer_body(tmp_path, monkeypatch, operation):
    """A newer coalesce survives an acknowledgement in the read/write gap."""
    mod = load_spool(tmp_path, monkeypatch)
    db_path = tmp_path / "spool.db"
    mod.store.enqueue(
        HEARTBEAT_ID,
        "heartbeat",
        {"load": {"active_sources": 1}},
        coalesce=True,
    )
    old_revision = mod.store.due(1)[0]["revision"]
    calls = {"hook": 0, "injected": 0}

    def acknowledge(item_id, *, via_hook):
        if via_hook:
            calls["hook"] += 1
        else:
            calls["injected"] += 1
        if calls["hook"] + calls["injected"] > 1:
            return
        _apply_old_revision(mod.store, operation, item_id, old_revision)

    mod.store._after_coalesce_read = lambda item_id: acknowledge(item_id, via_hook=True)

    real_connect = mod.sqlite3.connect

    def connect(*args, **kwargs):
        state = {"immediate": False, "injected": False}

        def before_execute(sql):
            text = sql if isinstance(sql, str) else ""
            if text.lstrip().startswith("BEGIN IMMEDIATE"):
                state["immediate"] = True
            if (
                not state["injected"]
                and not state["immediate"]
                and "revision=revision+1" in text
            ):
                state["injected"] = True
                acknowledge(HEARTBEAT_ID, via_hook=False)

        return _ExecuteProxy(real_connect(*args, **kwargs), before_execute)

    monkeypatch.setattr(mod.sqlite3, "connect", connect)
    mod.store.enqueue(
        HEARTBEAT_ID,
        "heartbeat",
        {"load": {"active_sources": 9}},
        coalesce=True,
    )

    row = _queued_row(db_path, HEARTBEAT_ID)
    assert row is not None
    assert row[0] == NEWER_BODY
    assert calls["hook"] == 1

    if operation == "retry":
        assert row[1] == 1
        assert row[2] > 0
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE spool_items SET next_attempt_at=0 WHERE id=?",
                (HEARTBEAT_ID,),
            )
    else:
        assert row[1] == 0
        assert row[2] == 0.0

    with sqlite3.connect(db_path) as db:
        dead = db.execute("SELECT body_json FROM dead_letters").fetchall()
    if operation == "dead_letter":
        assert [item[0] for item in dead] == [OLD_BODY]
    else:
        assert dead == []

    pending = mod.store.due(10)
    assert len(pending) == 1
    assert pending[0]["body"]["load"]["active_sources"] == 9
    assert pending[0]["revision"] == old_revision + 1

    follow_up = Client(statuses=[202])
    delivered = asyncio.run(mod.flush_once(follow_up))
    assert delivered == 1
    assert follow_up.calls[0][1]["json"]["load"]["active_sources"] == 9
    assert mod.store.counts() == (0, 1 if operation == "dead_letter" else 0)


@pytest.mark.parametrize(
    ("status_code", "operation"),
    [
        pytest.param(202, "success", id="success"),
        pytest.param(503, "retry", id="retry"),
        pytest.param(422, "dead_letter", id="dead_letter"),
    ],
)
def test_qa007_stale_ack_is_not_counted_or_dead_lettered(
    tmp_path, monkeypatch, caplog, status_code, operation
):
    """A missed revision is a stale ack, not a delivery or a dead letter."""
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue(
        HEARTBEAT_ID,
        "heartbeat",
        {"load": {"active_sources": 1}},
        coalesce=True,
    )
    client = ReplaceWhileSending(mod.store, status_code)
    with caplog.at_level(logging.INFO, logger="regional-spool"):
        delivered = asyncio.run(mod.flush_once(client))
    assert delivered == 0
    assert "spool_stale_ack" in caplog.text
    assert f"op={operation}" in caplog.text
    assert "spool_dead_letter" not in caplog.text
    assert _queued_row(tmp_path / "spool.db", HEARTBEAT_ID) == (NEWER_BODY, 0, 0.0)
    assert mod.store.counts() == (1, 0)
    follow_up = Client(statuses=[202])
    assert asyncio.run(mod.flush_once(follow_up)) == 1
    assert follow_up.calls[0][1]["json"]["load"]["active_sources"] == 9


def test_qa007_matching_dead_letter_still_logs(tmp_path, monkeypatch, caplog):
    """A terminal failure of the current revision is still a dead letter."""
    mod = load_spool(tmp_path, monkeypatch)
    mod.store.enqueue("event:e1", "event", {"event_id": "e1"})
    with caplog.at_level(logging.INFO, logger="regional-spool"):
        delivered = asyncio.run(mod.flush_once(Client(statuses=[422])))
    assert delivered == 0
    assert "spool_dead_letter" in caplog.text
    assert "spool_stale_ack" not in caplog.text
    assert mod.store.counts() == (0, 1)


def test_qa007_duplicate_column_migration_keeps_rows(tmp_path, monkeypatch):
    """A duplicate-column error while adding revision does not drop the spool."""
    mod = load_spool(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    _write_legacy_spool(legacy_path)
    real_connect = mod.sqlite3.connect

    def connect(*args, **kwargs):
        raw = real_connect(*args, **kwargs)
        state = {"raised": False}

        def before_execute(sql):
            text = sql if isinstance(sql, str) else ""
            if "ADD COLUMN revision" in text and not state["raised"]:
                state["raised"] = True
                raw.execute(sql)
                raise sqlite3.OperationalError("duplicate column name: revision")

        return _ExecuteProxy(raw, before_execute)

    monkeypatch.setattr(mod.sqlite3, "connect", connect)
    opened = mod.Store(str(legacy_path), 100)
    pending = opened.due(1)
    assert len(pending) == 1
    assert pending[0]["revision"] == 1
    assert pending[0]["attempts"] == 3
    assert pending[0]["body"]["load"]["active_sources"] == 4
    with sqlite3.connect(legacy_path) as db:
        stored = db.execute(
            "SELECT body_json, attempts, revision FROM spool_items WHERE id=?",
            (HEARTBEAT_ID,),
        ).fetchone()
    assert stored == (LEGACY_BODY, 3, 1)


def test_qa007_concurrent_openers_upgrade_legacy_database(tmp_path, monkeypatch):
    """Two Store openers can add revision without losing the existing row."""
    mod = load_spool(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    _write_legacy_spool(legacy_path)
    real_connect = mod.sqlite3.connect
    barrier = threading.Barrier(2)

    def connect(*args, **kwargs):
        raw = real_connect(*args, **kwargs)

        def before_execute(sql):
            text = sql if isinstance(sql, str) else ""
            if "PRAGMA table_info(spool_items)" not in text:
                return
            peeked = raw.execute(sql).fetchall()
            names = [row["name"] if hasattr(row, "keys") else row[1] for row in peeked]
            if "revision" in names:
                return
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass

        return _ExecuteProxy(raw, before_execute)

    monkeypatch.setattr(mod.sqlite3, "connect", connect)
    errors = []
    started = threading.Barrier(2)

    def open_store():
        started.wait()
        try:
            mod.Store(str(legacy_path), 100)
        except Exception as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")

    threads = [threading.Thread(target=open_store) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with sqlite3.connect(legacy_path) as db:
        stored = db.execute(
            "SELECT body_json, attempts, created_at, revision FROM spool_items WHERE id=?",
            (HEARTBEAT_ID,),
        ).fetchone()
    assert stored == (LEGACY_BODY, 3, LEGACY_CREATED_AT, 1)
    reopened = mod.Store(str(legacy_path), 100)
    pending = reopened.due(1)
    assert len(pending) == 1
    assert pending[0]["body"]["load"]["active_sources"] == 4
    assert pending[0]["revision"] == 1


BODY_N1 = '{"n":1}'
BODY_N9 = '{"n":9}'
BODY_N10 = '{"n":10}'


def _ack_revision(store, operation, item_id, revision):
    if operation == "success":
        return store.success(item_id, revision)
    if operation == "retry":
        return store.retry(item_id, "HTTP 503", 3600, revision)
    return store.dead_letter(item_id, 422, "HTTP 422", revision)


def _mark_revision(db_path, item_id):
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT revision FROM spool_items WHERE id=?",
            (item_id,),
        ).fetchone()
    return None if row is None else row[0]


@pytest.mark.parametrize("operation", ["success", "retry", "dead_letter"])
def test_qa007_101_second_inflight_ack_misses_reinserted_body(
    tmp_path, monkeypatch, operation
):
    """A second ack of revision 1 must not match a body inserted after the first ack."""
    mod = load_spool(tmp_path, monkeypatch)
    db_path = tmp_path / "spool.db"
    mod.store.enqueue(HEARTBEAT_ID, "heartbeat", {"n": 1}, coalesce=True)
    read = mod.store.due(1)
    assert read[0]["body"] == {"n": 1}
    assert read[0]["revision"] == 1

    early = _ack_revision(mod.store, operation, HEARTBEAT_ID, 1)
    assert early is True
    mod.store.enqueue(HEARTBEAT_ID, "heartbeat", {"n": 9}, coalesce=True)
    late = _ack_revision(mod.store, operation, HEARTBEAT_ID, 1)
    assert late is False

    row = _queued_row(db_path, HEARTBEAT_ID)
    assert row is not None
    assert row[0] == BODY_N9
    stored_revision = _mark_revision(db_path, HEARTBEAT_ID)
    assert stored_revision != 1
    with sqlite3.connect(db_path) as db:
        dead = db.execute(
            "SELECT body_json, status_code, reason FROM dead_letters"
        ).fetchall()
    if operation == "dead_letter":
        assert dead == [(BODY_N1, 422, "HTTP 422")]
        assert row[1] == 0
        assert row[2] == 0.0
    elif operation == "retry":
        assert dead == []
        assert row[1] == 1
        assert row[2] > 0
    else:
        assert dead == []
        assert row[1] == 0
        assert row[2] == 0.0

    if operation == "retry":
        assert mod.store.success(HEARTBEAT_ID, stored_revision) is True
        mod.store.enqueue(HEARTBEAT_ID, "heartbeat", {"n": 10}, coalesce=True)
        assert _queued_row(db_path, HEARTBEAT_ID)[0] == BODY_N10
        reinserted = _mark_revision(db_path, HEARTBEAT_ID)
        assert reinserted not in (None, 1)
        assert mod.store.success(HEARTBEAT_ID, 1) is False
        pending = mod.store.due(1)
        assert pending[0]["body"] == {"n": 10}
        assert pending[0]["revision"] == reinserted
        follow_up = Client(statuses=[202])
        assert asyncio.run(mod.flush_once(follow_up)) == 1
        assert follow_up.calls[0][1]["json"] == {"n": 10}
        return

    if row[2] > 0:
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE spool_items SET next_attempt_at=0 WHERE id=?",
                (HEARTBEAT_ID,),
            )
    pending = mod.store.due(1)
    assert pending[0]["body"] == {"n": 9}
    assert pending[0]["revision"] == stored_revision
    assert mod.store.success(HEARTBEAT_ID, 1) is False
    follow_up = Client(statuses=[202])
    assert asyncio.run(mod.flush_once(follow_up)) == 1
    assert follow_up.calls[0][1]["json"] == {"n": 9}


class _HoldClient:
    """Block inside post until the test releases this flusher."""

    def __init__(self, status_code, release):
        self.status_code = status_code
        self.release = release
        self.calls = []
        self.entered = asyncio.Event()

    async def post(self, url, **kwargs):
        self.calls.append(kwargs.get("json"))
        self.entered.set()
        await self.release.wait()
        return Response(self.status_code)


async def _two_flush_once(mod, status_code):
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    client_a = _HoldClient(status_code, release_a)
    client_b = _HoldClient(status_code, release_b)
    task_a = asyncio.create_task(mod.flush_once(client_a))
    await client_a.entered.wait()
    task_b = asyncio.create_task(mod.flush_once(client_b))
    await client_b.entered.wait()
    release_a.set()
    result_a = await task_a
    mod.store.enqueue(HEARTBEAT_ID, "heartbeat", {"n": 9}, coalesce=True)
    release_b.set()
    result_b = await task_b
    return result_a, result_b, client_a, client_b


@pytest.mark.parametrize(
    "status_code",
    [
        pytest.param(202, id="success"),
        pytest.param(503, id="retry"),
        pytest.param(422, id="dead_letter"),
    ],
)
def test_qa007_101_two_flush_once_calls_keep_reinserted_body(
    tmp_path, monkeypatch, caplog, status_code
):
    """Two flushers that both read revision 1 must not ack the later body."""
    mod = load_spool(tmp_path, monkeypatch)
    db_path = tmp_path / "spool.db"
    mod.store.enqueue(HEARTBEAT_ID, "heartbeat", {"n": 1}, coalesce=True)
    with caplog.at_level(logging.INFO, logger="regional-spool"):
        result_a, result_b, client_a, client_b = asyncio.run(
            _two_flush_once(mod, status_code)
        )
    assert client_a.calls == [{"n": 1}]
    assert client_b.calls == [{"n": 1}]
    assert result_a == (1 if status_code == 202 else 0)
    assert result_b == 0
    row = _queued_row(db_path, HEARTBEAT_ID)
    assert row is not None
    assert row[0] == BODY_N9
    assert _mark_revision(db_path, HEARTBEAT_ID) != 1
    if status_code == 422:
        assert caplog.text.count("spool_dead_letter") == 1
        assert "spool_stale_ack" in caplog.text
        assert "op=dead_letter" in caplog.text
        with sqlite3.connect(db_path) as db:
            dead = db.execute("SELECT body_json FROM dead_letters").fetchall()
        assert [item[0] for item in dead] == [BODY_N1]
    elif status_code == 503:
        assert "spool_stale_ack" in caplog.text
        assert "op=retry" in caplog.text
        assert "spool_dead_letter" not in caplog.text
        assert row[1] == 1
        assert row[2] > 0
        current = _mark_revision(db_path, HEARTBEAT_ID)
        assert mod.store.success(HEARTBEAT_ID, current) is True
        mod.store.enqueue(HEARTBEAT_ID, "heartbeat", {"n": 10}, coalesce=True)
        assert _queued_row(db_path, HEARTBEAT_ID)[0] == BODY_N10
        assert _mark_revision(db_path, HEARTBEAT_ID) not in (None, 1)
        assert mod.store.success(HEARTBEAT_ID, 1) is False
    else:
        assert "spool_stale_ack" in caplog.text
        assert "op=success" in caplog.text
        assert mod.store.counts() == (1, 0)
    assert mod.store.success(HEARTBEAT_ID, 1) is False


def test_qa007_101_revision_does_not_pass_int64(tmp_path, monkeypatch):
    """Coalesce refuses to store a revision SQLite cannot keep as an integer."""
    mod = load_spool(tmp_path, monkeypatch)
    db_path = tmp_path / "spool.db"
    mod.store.enqueue(HEARTBEAT_ID, "heartbeat", {"n": 1}, coalesce=True)
    maximum = 2**63 - 1
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE spool_items SET revision=? WHERE id=?",
            (maximum, HEARTBEAT_ID),
        )
        try:
            db.execute(
                "UPDATE spool_revision_marks SET high_revision=? WHERE id=?",
                (maximum, HEARTBEAT_ID),
            )
        except sqlite3.OperationalError:
            pass
    with pytest.raises(OverflowError):
        mod.store.enqueue(HEARTBEAT_ID, "heartbeat", {"n": 2}, coalesce=True)
    assert _queued_row(db_path, HEARTBEAT_ID)[0] == BODY_N1
    assert _mark_revision(db_path, HEARTBEAT_ID) == maximum
