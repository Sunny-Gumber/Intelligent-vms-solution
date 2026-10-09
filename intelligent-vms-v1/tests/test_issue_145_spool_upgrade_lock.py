"""Concurrent legacy spool upgrade must wait instead of raising database is locked.

Issue #145: two Store openers on a pre-revision spool database intermittently
raise sqlite3.OperationalError: database is locked. SQLite's default busy
timeout is 5 seconds (sqlite3.connect). The upgrade has to take one immediate
write transaction around the whole schema change and keep retrying longer than
that default, then apply the same idempotent statements if the peer commits
first. An aborted upgrade must roll back, including indexes and the revision
mark table, so the legacy rows stay readable for a later open.
"""

import sqlite3
import threading
import time

import pytest

from tests.test_vms_fix_007_spool_ack_revision import (
    HEARTBEAT_ID,
    LEGACY_BODY,
    LEGACY_CREATED_AT,
    _write_legacy_spool,
    load_spool,
)


# sqlite3.connect documents a 5.0 second default busy timeout. The holder keeps
# the write lock this much longer than that, measured from the peer's first
# conflicting statement, so a build that still uses the default must time out.
HOLD_PAST_DEFAULT_BUSY_TIMEOUT_S = 6.0
_DDL_MARKERS = (
    "CREATE TABLE",
    "CREATE INDEX",
    "ALTER TABLE",
    "INSERT INTO SPOOL_REVISION_MARKS",
)


class _ConnectionProxy:
    """Forward SQLite calls and record upgrade statements for one connection."""

    def __init__(self, connection, on_execute, on_script):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_execute", connection.execute)
        object.__setattr__(self, "_executescript", connection.executescript)
        object.__setattr__(self, "_on_execute", on_execute)
        object.__setattr__(self, "_on_script", on_script)

    def execute(self, sql, *params):
        self._on_execute(self._connection, sql)
        return self._execute(sql, *params)

    def executescript(self, script):
        self._on_script(self._connection, script)
        return self._executescript(script)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._connection.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)


def _statement_kind(sql: str) -> str:
    text = sql.lstrip().upper() if isinstance(sql, str) else ""
    if text.startswith("BEGIN IMMEDIATE"):
        return "begin"
    if any(text.startswith(marker) for marker in _DDL_MARKERS):
        return "ddl"
    return ""


def test_issue_145_concurrent_openers_wait_out_legacy_upgrade(tmp_path, monkeypatch):
    """A peer opener blocked in the upgrade lock retries past the 5s default.

    The first opener parks inside the write transaction after BEGIN IMMEDIATE
    until the second opener has issued its own conflicting statement, then
    holds that lock for six seconds. On the unfixed store the second opener
    raises database is locked. After the fix both openers finish, the legacy
    row is unchanged, and schema SQL runs only after that connection's
    immediate transaction has started.
    """
    mod = load_spool(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    _write_legacy_spool(legacy_path)
    real_connect = mod.sqlite3.connect
    state_lock = threading.Lock()
    holder_id = {"value": None}
    lock_held = threading.Event()
    peer_conflict = threading.Event()
    conflict_at = {"t": None}
    sequences: dict[int, list[str]] = {}
    script_count = {"n": 0}
    errors: list[str] = []

    def _note(connection, kind: str) -> None:
        if not kind:
            return
        sequences.setdefault(id(connection), []).append(kind)

    def _mark_peer(connection) -> None:
        with state_lock:
            holder = holder_id["value"]
        if holder is None or id(connection) == holder:
            return
        if conflict_at["t"] is None:
            conflict_at["t"] = time.monotonic()
        peer_conflict.set()

    def on_execute(connection, sql) -> None:
        kind = _statement_kind(sql)
        if kind == "begin":
            with state_lock:
                already = holder_id["value"]
            if already is None:
                _note(connection, kind)
                return
        if kind == "begin" or kind == "ddl":
            _mark_peer(connection)
        _note(connection, kind)

    def on_script(connection, script) -> None:
        script_count["n"] += 1
        _mark_peer(connection)
        _note(connection, "script")
        _note(connection, _statement_kind(script))

    def connect(*args, **kwargs):
        raw = real_connect(*args, **kwargs)
        original_execute = raw.execute

        def execute(sql, *params):
            kind = _statement_kind(sql)
            on_execute(raw, sql)
            if kind == "begin":
                with state_lock:
                    is_holder = holder_id["value"] is None
                    if is_holder:
                        holder_id["value"] = id(raw)
                if is_holder:
                    result = original_execute(sql, *params)
                    lock_held.set()
                    if not peer_conflict.wait(timeout=10):
                        raise TimeoutError("second opener did not reach the upgrade lock")
                    started = conflict_at["t"]
                    while time.monotonic() - started < HOLD_PAST_DEFAULT_BUSY_TIMEOUT_S:
                        time.sleep(0.02)
                    return result
            return original_execute(sql, *params)

        proxy = _ConnectionProxy(raw, on_execute, on_script)
        # The holder must sleep inside BEGIN IMMEDIATE, after the statement
        # has the write lock and before later schema statements run. The
        # proxy records every statement; this wrapper adds only that park.
        object.__setattr__(proxy, "execute", execute)
        return proxy

    monkeypatch.setattr(mod.sqlite3, "connect", connect)

    def open_store():
        try:
            mod.Store(str(legacy_path), 100)
        except Exception as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")

    first = threading.Thread(target=open_store)
    second = threading.Thread(target=open_store)
    first.start()
    assert lock_held.wait(timeout=5), "first opener did not take the write lock"
    second.start()
    first.join(timeout=20)
    second.join(timeout=20)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert script_count["n"] == 0
    for sequence in sequences.values():
        if "ddl" not in sequence:
            continue
        assert "begin" in sequence
        assert sequence.index("begin") < sequence.index("ddl")
        assert "script" not in sequence

    with sqlite3.connect(legacy_path) as db:
        stored = db.execute(
            "SELECT body_json, attempts, created_at, revision FROM spool_items WHERE id=?",
            (HEARTBEAT_ID,),
        ).fetchone()
        mark = db.execute(
            "SELECT high_revision FROM spool_revision_marks WHERE id=?",
            (HEARTBEAT_ID,),
        ).fetchone()
    assert stored == (LEGACY_BODY, 3, LEGACY_CREATED_AT, 1)
    assert mark == (1,)
    reopened = mod.Store(str(legacy_path), 100)
    pending = reopened.due(1)
    assert len(pending) == 1
    assert pending[0]["id"] == HEARTBEAT_ID
    assert pending[0]["body"]["load"]["active_sources"] == 4
    assert pending[0]["attempts"] == 3
    assert pending[0]["revision"] == 1


def test_issue_145_wal_enable_retries_while_legacy_writer_holds_lock(tmp_path, monkeypatch):
    """Enabling WAL waits out a rollback-journal writer instead of failing at once.

    PRAGMA journal_mode=WAL raises database is locked immediately, and ignores
    busy_timeout, while another connection holds a write transaction on a
    database that is still a rollback journal. Two openers hit that window
    when they both switch a legacy spool to WAL. This test holds that writer
    until the opener has entered the pragma. A build that does not retry the
    pragma loses the opener before the writer releases the lock.
    """
    mod = load_spool(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    _write_legacy_spool(legacy_path)
    holder = sqlite3.connect(legacy_path)
    holder.execute("BEGIN IMMEDIATE")
    assert holder.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    real_connect = mod.sqlite3.connect
    attempted = threading.Event()
    errors: list[str] = []

    class Proxy:
        def __init__(self, connection):
            object.__setattr__(self, "_connection", connection)
            object.__setattr__(self, "_execute", connection.execute)

        def execute(self, sql, *params):
            text = sql if isinstance(sql, str) else ""
            if text.lstrip().upper().startswith("PRAGMA JOURNAL_MODE"):
                attempted.set()
            return self._execute(sql, *params)

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._connection.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __setattr__(self, name, value):
            setattr(self._connection, name, value)

    monkeypatch.setattr(
        mod.sqlite3,
        "connect",
        lambda *args, **kwargs: Proxy(real_connect(*args, **kwargs)),
    )

    def open_store():
        try:
            mod.Store(str(legacy_path), 100)
        except Exception as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")

    opener = threading.Thread(target=open_store)
    opener.start()
    assert attempted.wait(timeout=2), "opener did not attempt to enable WAL"
    # The unfixed pragma returns in the same call. A retry loop is still in
    # that call's caller 200ms later, because the writer has not released.
    opener.join(timeout=0.2)
    still_waiting = opener.is_alive() and errors == []
    holder.close()
    opener.join(timeout=5)

    assert still_waiting
    assert not opener.is_alive()
    assert errors == []
    with sqlite3.connect(legacy_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        stored = db.execute(
            "SELECT body_json, attempts, created_at, revision FROM spool_items WHERE id=?",
            (HEARTBEAT_ID,),
        ).fetchone()
    assert stored == (LEGACY_BODY, 3, LEGACY_CREATED_AT, 1)


def test_issue_145_interrupted_upgrade_rolls_back_legacy_rows(tmp_path, monkeypatch):
    """A failure during ALTER rolls the schema back and keeps the legacy row.

    The next open retries the same upgrade. Indexes and spool_revision_marks
    must not remain committed ahead of the revision column.
    """
    mod = load_spool(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    _write_legacy_spool(legacy_path)
    real_connect = mod.sqlite3.connect
    armed = {"raise": True}

    def connect(*args, **kwargs):
        raw = real_connect(*args, **kwargs)
        original_execute = raw.execute

        def execute(sql, *params):
            text = sql if isinstance(sql, str) else ""
            if armed["raise"] and "ADD COLUMN revision" in text:
                armed["raise"] = False
                raise RuntimeError("interrupted upgrade")
            return original_execute(sql, *params)

        proxy = _ConnectionProxy(raw, lambda *_args: None, lambda *_args: None)
        object.__setattr__(proxy, "execute", execute)
        return proxy

    monkeypatch.setattr(mod.sqlite3, "connect", connect)
    with pytest.raises(RuntimeError, match="interrupted upgrade"):
        mod.Store(str(legacy_path), 100)

    with sqlite3.connect(legacy_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(spool_items)").fetchall()}
        index = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_spool_due'"
        ).fetchone()
        marks = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spool_revision_marks'"
        ).fetchone()
        stored = db.execute(
            "SELECT body_json, attempts, created_at FROM spool_items WHERE id=?",
            (HEARTBEAT_ID,),
        ).fetchone()
    assert "revision" not in columns
    assert index is None
    assert marks is None
    assert stored == (LEGACY_BODY, 3, LEGACY_CREATED_AT)

    reopened = mod.Store(str(legacy_path), 100)
    pending = reopened.due(1)
    assert len(pending) == 1
    assert pending[0]["body"]["load"]["active_sources"] == 4
    assert pending[0]["attempts"] == 3
    assert pending[0]["revision"] == 1
    with sqlite3.connect(legacy_path) as db:
        stored = db.execute(
            "SELECT body_json, attempts, created_at, revision FROM spool_items WHERE id=?",
            (HEARTBEAT_ID,),
        ).fetchone()
    assert stored == (LEGACY_BODY, 3, LEGACY_CREATED_AT, 1)
