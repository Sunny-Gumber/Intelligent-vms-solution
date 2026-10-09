import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request, status


LOG = logging.getLogger("regional-spool")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def _http_base(value: str, name: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{name} must be an http/https URL with host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must not contain credentials/query/fragment")
    return value.rstrip("/")


CONTROL_API_URL = _http_base(
    os.getenv("CONTROL_API_URL", "http://control-api:8000"),
    "CONTROL_API_URL",
)
NODE_ID = os.getenv("NODE_ID", "media-local-01").strip()
NODE_AGENT_TOKEN = os.getenv("NODE_AGENT_TOKEN", "")
RECORDING_HOOK_TOKEN = os.getenv("RECORDING_HOOK_TOKEN", "")
REGIONAL_SPOOL_TOKEN = os.getenv("REGIONAL_SPOOL_TOKEN", "")
SPOOL_DB_PATH = os.getenv("SPOOL_DB_PATH", "/var/lib/vms-spool/spool.db")
SPOOL_MAX_ITEMS = max(100, int(os.getenv("SPOOL_MAX_ITEMS", "100000")))
SPOOL_MAX_DEAD_LETTERS = max(
    100, int(os.getenv("SPOOL_MAX_DEAD_LETTERS", "10000"))
)
SPOOL_MAX_BODY_BYTES = max(
    1024, int(os.getenv("SPOOL_MAX_BODY_BYTES", "262144"))
)
SPOOL_BATCH_SIZE = max(1, min(1000, int(os.getenv("SPOOL_BATCH_SIZE", "100"))))
SPOOL_FLUSH_INTERVAL_SECONDS = max(
    0.2, float(os.getenv("SPOOL_FLUSH_INTERVAL_SECONDS", "1"))
)
SPOOL_BACKOFF_MAX_SECONDS = max(
    5.0, float(os.getenv("SPOOL_BACKOFF_MAX_SECONDS", "60"))
)
SPOOL_REQUEST_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("SPOOL_REQUEST_TIMEOUT_SECONDS", "5"))
)
# sqlite3.connect waits 5 seconds by default. Ordinary spool reads and writes
# keep that budget. Schema upgrade waits longer so a second opener can block in
# BEGIN IMMEDIATE until the peer commits, instead of raising "database is locked"
# while both try to migrate a legacy spool file at startup.
SPOOL_SQLITE_BUSY_TIMEOUT_MS = 5_000
SPOOL_SCHEMA_BUSY_TIMEOUT_MS = 30_000
# Each statement is idempotent. They run inside one immediate transaction so a
# second opener can repeat them after the first commits, and an interrupt rolls
# every statement back together. CREATE TABLE IF NOT EXISTS does not add
# revision to a legacy spool_items table; _ensure_spool_revision does that.
_SPOOL_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS spool_items (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        body_json TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_spool_due
        ON spool_items(next_attempt_at, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS dead_letters (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        body_json TEXT NOT NULL,
        status_code INTEGER,
        reason TEXT,
        created_at REAL NOT NULL,
        failed_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS spool_revision_marks (
        id TEXT PRIMARY KEY,
        high_revision INTEGER NOT NULL
    )
    """,
)
INITIAL_SPOOL_REVISION = 1
SPOOL_REVISION_MAX = 2**63 - 1


def _next_revision(current: int) -> int:
    """Return the next spool revision, refusing values SQLite cannot store as integers.

    Args:
        current: Highest revision already issued for this spool key. Zero means none.

    Returns:
        The following positive revision.

    Raises:
        OverflowError: If current is not an int, or the next value would exceed int64.
    """
    if isinstance(current, bool) or not isinstance(current, int):
        raise OverflowError("spool revision is not an integer")
    if current < 0 or current >= SPOOL_REVISION_MAX:
        raise OverflowError("spool revision cannot increase past int64")
    return current + 1


def _require_revision(revision: int) -> int:
    """Reject a revision that was not issued by the spool store.

    Args:
        revision: Revision captured when the item was read for sending.

    Returns:
        The same revision when it is a positive integer.

    Raises:
        ValueError: If revision is a bool or is not a positive integer.
    """
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("spool item revision must be a positive integer")
    return revision


class SpoolFull(RuntimeError):
    """Raised when the regional spool reaches its configured queued-item limit."""


class SpoolPayloadTooLarge(RuntimeError):
    """Raised when a regional spool payload exceeds the configured body limit."""


class Store:
    """Persist regional events, heartbeats and recording hooks in durable SQLite."""

    def __init__(
        self,
        path: str,
        max_items: int,
        *,
        max_dead_letters: int = SPOOL_MAX_DEAD_LETTERS,
        max_body_bytes: int = SPOOL_MAX_BODY_BYTES,
    ):
        self.path = Path(path)
        self.max_items = max_items
        self.max_dead_letters = max_dead_letters
        self.max_body_bytes = max_body_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self, *, busy_timeout_ms: int = SPOOL_SQLITE_BUSY_TIMEOUT_MS):
        """Open the spool database with WAL, full sync, and a busy timeout.

        Args:
            busy_timeout_ms: Milliseconds SQLite retries while another
                connection holds the write lock. Schema upgrade passes a longer
                value than ordinary queue operations.

        Returns:
            An open connection with sqlite3.Row rows.

        Raises:
            sqlite3.Error: If the file or the connection pragmas cannot be set.
        """
        conn = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init(self):
        # executescript() commits before it runs, so it cannot stay inside the
        # write transaction. Concurrent openers then overlap: one holds
        # BEGIN IMMEDIATE while the other's DDL hits "database is locked" once
        # the busy timeout expires. One immediate transaction covers every
        # idempotent statement. The waiter blocks in BEGIN IMMEDIATE without a
        # shared lock, then runs the same statements after the peer commits.
        # A crash before commit rolls the whole upgrade back, including the
        # index and revision-mark table, and leaves existing spool rows in place.
        with self._connect(busy_timeout_ms=SPOOL_SCHEMA_BUSY_TIMEOUT_MS) as db:
            db.execute("BEGIN IMMEDIATE")
            for statement in _SPOOL_SCHEMA_STATEMENTS:
                db.execute(statement)
            self._ensure_spool_revision(db)

    def _ensure_spool_revision(self, db) -> None:
        """Add spool_items.revision in place when an older database lacks it.

        CREATE TABLE IF NOT EXISTS does not change a table that already exists.
        Existing rows keep their bodies, retry state, and row ids. They receive
        revision 1 so a later acknowledgement can name the body that was read.
        The caller holds the immediate write transaction for the whole upgrade.
        This method must not issue BEGIN: a nested BEGIN IMMEDIATE raises
        "cannot start a transaction within a transaction" and would roll the
        upgrade back.

        Args:
            db: Open SQLite connection that already holds the immediate write lock.

        Returns:
            None after the column is present and each current row has a
            high-water mark at least as large as its stored revision.

        Raises:
            sqlite3.Error: If the table cannot be altered.
        """
        if not self._has_revision_column(db):
            try:
                db.execute(
                    "ALTER TABLE spool_items ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
                if not self._has_revision_column(db):
                    raise
        self._ensure_revision_marks(db)

    def _ensure_revision_marks(self, db) -> None:
        """Keep the highest issued revision for each spool id across deletes.

        The mark is not removed when a row is acknowledged. A later coalesce that
        has to insert the key again continues past that mark, so an acknowledgement
        already in flight cannot match the new row.

        Args:
            db: Open SQLite connection holding the write lock.

        Returns:
            None after every current spool row is covered by a mark.

        Raises:
            sqlite3.Error: If the mark table cannot be created or filled.
        """
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS spool_revision_marks (
                id TEXT PRIMARY KEY,
                high_revision INTEGER NOT NULL
            )
            """
        )
        # WHERE true is required. SQLite rejects INSERT...SELECT...ON CONFLICT
        # without a WHERE clause ("near DO: syntax error") on 3.45.
        db.execute(
            """
            INSERT INTO spool_revision_marks(id, high_revision)
            SELECT id, revision FROM spool_items WHERE true
            ON CONFLICT(id) DO UPDATE SET
                high_revision = MAX(
                    spool_revision_marks.high_revision,
                    excluded.high_revision
                )
            """
        )

    def _remember_revision(self, db, item_id: str, revision: int) -> None:
        """Record that this revision has been issued for the spool id.

        Args:
            db: Open SQLite connection holding the write lock.
            item_id: Spool item identifier.
            revision: Revision just stored or about to be stored.

        Returns:
            None.

        Raises:
            sqlite3.Error: If the mark cannot be stored.
        """
        db.execute(
            """
            INSERT INTO spool_revision_marks(id, high_revision) VALUES(?, ?)
            ON CONFLICT(id) DO UPDATE SET
                high_revision = MAX(
                    spool_revision_marks.high_revision,
                    excluded.high_revision
                )
            """,
            (item_id, revision),
        )

    def _claim_revision(self, db, item_id: str) -> int:
        """Allocate a revision strictly above any revision ever issued for this id.

        Args:
            db: Open SQLite connection holding the write lock.
            item_id: Spool item identifier being inserted.

        Returns:
            The revision to store on the new row.

        Raises:
            OverflowError: If the next revision would exceed int64.
            sqlite3.Error: If the mark cannot be read or stored.
        """
        row = db.execute(
            "SELECT high_revision FROM spool_revision_marks WHERE id=?",
            (item_id,),
        ).fetchone()
        current = 0 if row is None else row["high_revision"]
        revision = _next_revision(current)
        self._remember_revision(db, item_id, revision)
        return revision

    def _has_revision_column(self, db) -> bool:
        """Return whether spool_items already stores a revision.

        Args:
            db: Open SQLite connection for this spool file.

        Returns:
            True when the revision column is present.

        Raises:
            sqlite3.Error: If table metadata cannot be read.
        """
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(spool_items)").fetchall()
        }
        return "revision" in columns

    def _exists(self, db, item_id: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM spool_items WHERE id=?",
                (item_id,),
            ).fetchone()
            is not None
        )

    def enqueue(self, item_id: str, kind: str, body: dict, *, coalesce: bool = False):
        """Durably enqueue one regional item, optionally coalescing by identifier.

        Args:
            item_id: Deterministic spool item identifier.
            kind: Item kind: recording, event or heartbeat.
            body: JSON-serializable payload.
            coalesce: Whether an existing item should be replaced in place.
                Replacement stores the next revision above every revision
                already issued for this id, including after the previous row
                was deleted, so an acknowledgement already in flight cannot
                remove the new body.

        Returns:
            None after the item is stored or an existing duplicate is retained.
            A coalesced write that finds the previous row gone inserts this body
            instead of dropping it.

        Raises:
            SpoolPayloadTooLarge: If the serialized body exceeds the configured limit.
            SpoolFull: If queued-item capacity is exhausted.
            OverflowError: If the next revision would exceed int64.
            sqlite3.Error: If durable storage fails.
        """
        now = time.time()
        body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if len(body_json.encode("utf-8")) > self.max_body_bytes:
            raise SpoolPayloadTooLarge("regional spool payload exceeds configured maximum")
        self._release_coalesce_for_test(item_id, coalesce)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._write_enqueued(db, item_id, kind, body_json, now, coalesce)

    def _release_coalesce_for_test(self, item_id: str, coalesce: bool) -> None:
        """Run the optional read/write-gap hook before the coalesced write lock.

        ``_after_coalesce_read`` is unset in production. Tests set it to call
        success, retry, or dead_letter on another connection while the previous
        revision is still stored. The following immediate transaction inserts
        the new body when that acknowledgement removes the row.

        Args:
            item_id: Spool item identifier being coalesced.
            coalesce: Whether this enqueue replaces an existing item.

        Returns:
            None.
        """
        hook = getattr(self, "_after_coalesce_read", None)
        if not coalesce or hook is None:
            return
        with self._connect() as db:
            if self._exists(db, item_id):
                hook(item_id)

    def _write_enqueued(
        self,
        db,
        item_id: str,
        kind: str,
        body_json: str,
        now: float,
        coalesce: bool,
    ) -> None:
        """Insert or replace one spool row inside the caller's write transaction.

        Args:
            db: Open SQLite connection that already holds the write lock.
            item_id: Durable spool item identifier.
            kind: Item kind: recording, event or heartbeat.
            body_json: Serialized payload within the configured size limit.
            now: Timestamp stored on insert or coalesced update.
            coalesce: Whether an existing row should be replaced in place.

        Returns:
            None after the row is inserted, replaced, or left unchanged.

        Raises:
            SpoolFull: If a new row would exceed queued-item capacity.
            OverflowError: If the next revision would exceed int64.
            sqlite3.Error: If durable storage fails.
        """
        if coalesce and self._exists(db, item_id):
            # A newer body invalidates any revision already read for sending.
            current = db.execute(
                "SELECT revision FROM spool_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if current is not None:
                revision = _next_revision(current["revision"])
                updated = db.execute(
                    """
                    UPDATE spool_items
                    SET body_json=?, updated_at=?, revision=?
                    WHERE id=? AND revision=?
                    """,
                    (body_json, now, revision, item_id, current["revision"]),
                )
                if updated.rowcount == 1:
                    self._remember_revision(db, item_id, revision)
                    return
        elif self._exists(db, item_id):
            return

        count = db.execute("SELECT COUNT(*) FROM spool_items").fetchone()[0]
        if int(count) >= self.max_items:
            raise SpoolFull("regional spool is full")
        revision = self._claim_revision(db, item_id)
        db.execute(
            """
            INSERT INTO spool_items(
                id, kind, body_json, attempts, next_attempt_at,
                last_error, created_at, updated_at, revision
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id,
                kind,
                body_json,
                0,
                0.0,
                None,
                now,
                now,
                revision,
            ),
        )

    def due(self, limit: int) -> list[dict]:
        """Return a bounded batch of spool items due for delivery.

        Args:
            limit: Maximum number of due items to return.

        Returns:
            Item dictionaries ordered by original creation time and identifier.
            Each dictionary includes the stored revision that acknowledgements
            must pass back.

        Raises:
            sqlite3.Error: If spool storage cannot be queried.
            ValueError: If a stored JSON body is invalid.
        """
        now = time.time()
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, kind, body_json, attempts, revision
                FROM spool_items
                WHERE next_attempt_at <= ?
                ORDER BY created_at, id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "body": json.loads(row["body_json"]),
                "attempts": int(row["attempts"]),
                "revision": int(row["revision"]),
            }
            for row in rows
        ]

    def success(self, item_id: str, revision: int):
        """Delete one delivered spool item only when its revision still matches.

        Args:
            item_id: Durable spool item identifier.
            revision: Revision read for the send that is being acknowledged.

        Returns:
            True when that revision was deleted. False when it is no longer
            stored, including when a newer revision replaced it. A false result
            is not a delivery.

        Raises:
            ValueError: If revision is not a positive integer.
            sqlite3.Error: If durable storage cannot be updated.
        """
        revision = _require_revision(revision)
        with self._connect() as db:
            deleted = db.execute(
                "DELETE FROM spool_items WHERE id=? AND revision=?",
                (item_id, revision),
            )
        return deleted.rowcount == 1

    def retry(self, item_id: str, error: str, delay_seconds: float, revision: int):
        """Schedule the read revision for a later bounded retry.

        A newer revision under the same id keeps its body and its existing
        due time. The stale attempt does not increment that row's attempts.

        Args:
            item_id: Durable spool item identifier.
            error: Bounded error description.
            delay_seconds: Delay before this revision becomes due again.
            revision: Revision read for the attempt that is being retried.

        Returns:
            True when that revision was rescheduled. False when it is no longer
            stored. A false result does not change the newer row.

        Raises:
            ValueError: If revision is not a positive integer.
            sqlite3.Error: If durable storage cannot be updated.
        """
        revision = _require_revision(revision)
        now = time.time()
        with self._connect() as db:
            updated = db.execute(
                """
                UPDATE spool_items
                SET attempts=attempts+1, next_attempt_at=?,
                    last_error=?, updated_at=?
                WHERE id=? AND revision=?
                """,
                (now + delay_seconds, error[:512], now, item_id, revision),
            )
        return updated.rowcount == 1

    def dead_letter(
        self, item_id: str, status_code: int | None, reason: str, revision: int
    ):
        """Move the read revision into the bounded dead-letter table.

        Args:
            item_id: Durable spool item identifier.
            status_code: Optional terminal HTTP status.
            reason: Bounded failure reason.
            revision: Revision read for the attempt that failed terminally.

        Returns:
            True when that revision was moved to dead letters. False when it is
            no longer stored. A false result is not a dead letter, and a newer
            revision under the same id stays queued.

        Raises:
            ValueError: If revision is not a positive integer.
            sqlite3.Error: If durable storage cannot be updated.
            RuntimeError: If the matching revision disappears after it was read
                inside this write transaction.
        """
        revision = _require_revision(revision)
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT kind, body_json, created_at
                FROM spool_items
                WHERE id=? AND revision=?
                """,
                (item_id, revision),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                """
                INSERT OR REPLACE INTO dead_letters(
                    id, kind, body_json, status_code, reason, created_at, failed_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    item_id,
                    row["kind"],
                    row["body_json"],
                    status_code,
                    reason[:512],
                    row["created_at"],
                    now,
                ),
            )
            deleted = db.execute(
                "DELETE FROM spool_items WHERE id=? AND revision=?",
                (item_id, revision),
            )
            if deleted.rowcount != 1:
                raise RuntimeError(
                    "spool dead-letter did not remove the revision that was read"
                )
            overflow = int(
                db.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
            ) - self.max_dead_letters
            if overflow > 0:
                db.execute(
                    """
                    DELETE FROM dead_letters
                    WHERE id IN (
                        SELECT id FROM dead_letters
                        ORDER BY failed_at ASC, id ASC
                        LIMIT ?
                    )
                    """,
                    (overflow,),
                )
        return True

    def counts(self) -> tuple[int, int]:
        """Return current queued and dead-letter spool counts.

        Returns:
            Tuple of queued-item count and dead-letter count.

        Raises:
            sqlite3.Error: If durable storage cannot be queried.
        """
        with self._connect() as db:
            queued = int(db.execute("SELECT COUNT(*) FROM spool_items").fetchone()[0])
            dead = int(db.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])
        return queued, dead


store = Store(
    SPOOL_DB_PATH,
    SPOOL_MAX_ITEMS,
    max_dead_letters=SPOOL_MAX_DEAD_LETTERS,
    max_body_bytes=SPOOL_MAX_BODY_BYTES,
)


def _recording_id(body: dict) -> str:
    material = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "recording:" + hashlib.sha256(material).hexdigest()


def _auth_recording(token: str | None):
    if not RECORDING_HOOK_TOKEN:
        raise HTTPException(503, "Recording hook token is not configured")
    if not token or not hmac.compare_digest(token, RECORDING_HOOK_TOKEN):
        raise HTTPException(401, "Invalid recording hook token")


def _auth_spool(token: str | None):
    if not REGIONAL_SPOOL_TOKEN:
        raise HTTPException(503, "Regional spool token is not configured")
    if not token or not hmac.compare_digest(token, REGIONAL_SPOOL_TOKEN):
        raise HTTPException(401, "Invalid regional spool token")


def _auth_node(authorization: str | None):
    expected = f"Bearer {NODE_AGENT_TOKEN}"
    if not NODE_AGENT_TOKEN:
        raise HTTPException(503, "Node agent token is not configured")
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "Invalid node agent token")


async def _enqueue(item_id: str, kind: str, body: dict, *, coalesce: bool = False):
    try:
        await asyncio.to_thread(store.enqueue, item_id, kind, body, coalesce=coalesce)
    except SpoolFull as exc:
        raise HTTPException(507, str(exc)) from exc
    except SpoolPayloadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop the regional spool background flush loop.

    Args:
        app: FastAPI application owning the lifecycle.

    Yields:
        Control to FastAPI while the flush task is active.
    """
    task = asyncio.create_task(flush_loop(), name="regional-spool-flush")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Intelligent VMS Regional Spool", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    """Return regional spool queue/dead-letter health counters.

    Returns:
        Health dictionary with queued and dead-letter counts.
    """
    queued, dead = await asyncio.to_thread(store.counts)
    return {"ok": True, "queued": queued, "dead_letters": dead}


@app.post("/v1/recording/segments/complete", status_code=status.HTTP_202_ACCEPTED)
async def queue_recording(
    path: str = Form(...),
    segment_path: str = Form(...),
    duration: str = Form(...),
    recording_node_id: str | None = Form(default=None),
    assignment_generation: int | None = Form(default=None),
    x_recording_hook_token: str | None = Header(default=None),
):
    """Durably queue one recording-segment completion hook.

    Args:
        path: Recording path key.
        segment_path: Completed segment path.
        duration: MediaMTX segment duration value.
        recording_node_id: Optional distributed recording node identity.
        assignment_generation: Optional fencing generation.
        x_recording_hook_token: Shared recording-hook authentication token.

    Returns:
        Accepted response containing the deterministic spool identifier.

    Raises:
        HTTPException: If authentication fails or the spool cannot accept the item.
    """
    _auth_recording(x_recording_hook_token)
    body = {
        "path": path,
        "segment_path": segment_path,
        "duration": duration,
        "recording_node_id": recording_node_id,
        "assignment_generation": assignment_generation,
    }
    item_id = _recording_id(body)
    await _enqueue(item_id, "recording", body)
    return {"queued": True, "spool_id": item_id}


@app.post("/v1/events", status_code=status.HTTP_202_ACCEPTED)
async def queue_event(
    request: Request,
    x_regional_spool_token: str | None = Header(default=None),
):
    """Durably queue one normalized regional event.

    Args:
        request: Request containing the event JSON object.
        x_regional_spool_token: Regional spool authentication token.

    Returns:
        Accepted response containing the event spool identifier.

    Raises:
        HTTPException: If authentication, event validation or spool acceptance fails.
    """
    _auth_spool(x_regional_spool_token)
    body = await request.json()
    if not isinstance(body, dict) or not body.get("event_id"):
        raise HTTPException(422, "event_id is required")
    item_id = f"event:{body['event_id']}"
    await _enqueue(item_id, "event", body)
    return {"queued": True, "spool_id": item_id}


@app.post("/v1/heartbeat", status_code=status.HTTP_202_ACCEPTED)
async def queue_heartbeat(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Durably coalesce the latest regional node heartbeat.

    Args:
        request: Request containing the heartbeat JSON object.
        authorization: Node-agent bearer authorization header.

    Returns:
        Accepted response containing the coalesced heartbeat spool identifier.

    Raises:
        HTTPException: If authentication, validation or spool acceptance fails.
    """
    _auth_node(authorization)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "heartbeat body must be an object")
    item_id = f"heartbeat:{NODE_ID}"
    await _enqueue(item_id, "heartbeat", body, coalesce=True)
    return {"queued": True, "spool_id": item_id}


def _retry_delay(attempts: int) -> float:
    base = min(SPOOL_BACKOFF_MAX_SECONDS, float(2 ** min(max(attempts, 0), 6)))
    return min(
        SPOOL_BACKOFF_MAX_SECONDS,
        max(0.2, base + random.uniform(0.0, min(1.0, base * 0.2))),
    )


def _terminal(kind: str, status_code: int) -> bool:
    if kind == "heartbeat":
        # 404 may simply mean an administrator has not registered the node yet.
        return status_code in {400, 401, 403, 422}
    return status_code in {400, 401, 403, 404, 409, 422}


async def _deliver(client: httpx.AsyncClient, item: dict) -> httpx.Response:
    kind = item["kind"]
    body = item["body"]
    if kind == "recording":
        return await client.post(
            f"{CONTROL_API_URL}/internal/v1/recording/segments/complete",
            data=body,
            headers={"X-Recording-Hook-Token": RECORDING_HOOK_TOKEN},
        )
    if kind == "event":
        return await client.post(
            f"{CONTROL_API_URL}/internal/v1/events/ingest",
            json=body,
            headers={"X-Regional-Spool-Token": REGIONAL_SPOOL_TOKEN},
        )
    if kind == "heartbeat":
        return await client.post(
            f"{CONTROL_API_URL}/api/v1/infrastructure/nodes/{NODE_ID}/heartbeat",
            json=body,
            headers={"Authorization": f"Bearer {NODE_AGENT_TOKEN}"},
        )
    raise RuntimeError(f"unsupported spool kind {kind}")


def _log_stale_ack(item: dict, operation: str, status_code: int | None = None) -> None:
    """Record an acknowledgement that did not match the stored revision.

    Args:
        item: Spool item read for sending. Only kind, id, and revision are logged.
        operation: Acknowledgement that missed: success, retry, or dead_letter.
        status_code: HTTP status when the miss followed an HTTP response.

    Returns:
        None.
    """
    if status_code is None:
        LOG.info(
            "spool_stale_ack kind=%s id=%s revision=%s op=%s",
            item["kind"],
            item["id"],
            item["revision"],
            operation,
        )
        return
    LOG.info(
        "spool_stale_ack kind=%s id=%s revision=%s op=%s status=%s",
        item["kind"],
        item["id"],
        item["revision"],
        operation,
        status_code,
    )


async def flush_once(client: httpx.AsyncClient | None = None) -> int:
    """Attempt delivery for one bounded batch of due spool items.

    Args:
        client: Optional reusable HTTP client. A temporary client is created when absent.

    Returns:
        Number of read revisions whose successful HTTP response still matched
        the stored row. A stale acknowledgement is not counted.

    Raises:
        Exception: Unexpected spool/client failures propagate to the caller.
    """
    items = await asyncio.to_thread(store.due, SPOOL_BATCH_SIZE)
    if not items:
        return 0
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=SPOOL_REQUEST_TIMEOUT_SECONDS)
    delivered = 0
    try:
        for item in items:
            try:
                response = await _deliver(client, item)
                if 200 <= response.status_code < 300:
                    applied = await asyncio.to_thread(
                        store.success, item["id"], item["revision"]
                    )
                    if applied:
                        delivered += 1
                    else:
                        _log_stale_ack(item, "success", response.status_code)
                    continue
                if _terminal(item["kind"], response.status_code):
                    applied = await asyncio.to_thread(
                        store.dead_letter,
                        item["id"],
                        response.status_code,
                        f"HTTP {response.status_code}",
                        item["revision"],
                    )
                    if applied:
                        LOG.warning(
                            "spool_dead_letter kind=%s id=%s status=%s",
                            item["kind"],
                            item["id"],
                            response.status_code,
                        )
                    else:
                        _log_stale_ack(item, "dead_letter", response.status_code)
                    continue
                applied = await asyncio.to_thread(
                    store.retry,
                    item["id"],
                    f"HTTP {response.status_code}",
                    _retry_delay(item["attempts"]),
                    item["revision"],
                )
                if not applied:
                    _log_stale_ack(item, "retry", response.status_code)
            except Exception as exc:
                applied = await asyncio.to_thread(
                    store.retry,
                    item["id"],
                    exc.__class__.__name__,
                    _retry_delay(item["attempts"]),
                    item["revision"],
                )
                if not applied:
                    _log_stale_ack(item, "retry")
        return delivered
    finally:
        if owns_client:
            await client.aclose()


async def flush_loop():
    """Continuously flush due regional spool items with bounded retry handling.

    Returns:
        None under normal operation; the coroutine runs until cancelled.
    """
    while True:
        try:
            await flush_once()
        except Exception:
            LOG.exception("spool_flush_failed")
        await asyncio.sleep(SPOOL_FLUSH_INTERVAL_SECONDS)
