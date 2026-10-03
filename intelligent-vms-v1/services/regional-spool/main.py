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

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS spool_items (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_spool_due
                    ON spool_items(next_attempt_at, created_at);

                CREATE TABLE IF NOT EXISTS dead_letters (
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
            coalesce: Whether an existing item should be updated in place.

        Returns:
            None after the item is stored or an existing duplicate is retained.

        Raises:
            SpoolPayloadTooLarge: If the serialized body exceeds the configured limit.
            SpoolFull: If queued-item capacity is exhausted.
            sqlite3.Error: If durable storage fails.
        """
        now = time.time()
        body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if len(body_json.encode("utf-8")) > self.max_body_bytes:
            raise SpoolPayloadTooLarge("regional spool payload exceeds configured maximum")
        with self._connect() as db:
            if coalesce and self._exists(db, item_id):
                db.execute(
                    """
                    UPDATE spool_items
                    SET body_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (body_json, now, item_id),
                )
                return

            if self._exists(db, item_id):
                return

            count = db.execute("SELECT COUNT(*) FROM spool_items").fetchone()[0]
            if int(count) >= self.max_items:
                raise SpoolFull("regional spool is full")

            db.execute(
                """
                INSERT INTO spool_items(
                    id, kind, body_json, attempts, next_attempt_at,
                    last_error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (item_id, kind, body_json, 0, 0.0, None, now, now),
            )

    def due(self, limit: int) -> list[dict]:
        """Return a bounded batch of spool items due for delivery.

        Args:
            limit: Maximum number of due items to return.

        Returns:
            Item dictionaries ordered by original creation time and identifier.

        Raises:
            sqlite3.Error: If spool storage cannot be queried.
            ValueError: If a stored JSON body is invalid.
        """
        now = time.time()
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, kind, body_json, attempts
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
            }
            for row in rows
        ]

    def success(self, item_id: str):
        """Delete one successfully delivered spool item.

        Args:
            item_id: Durable spool item identifier.

        Returns:
            None after deletion.

        Raises:
            sqlite3.Error: If durable storage cannot be updated.
        """
        with self._connect() as db:
            db.execute("DELETE FROM spool_items WHERE id=?", (item_id,))

    def retry(self, item_id: str, error: str, delay_seconds: float):
        """Schedule one spool item for a later bounded retry.

        Args:
            item_id: Durable spool item identifier.
            error: Bounded error description.
            delay_seconds: Delay before the item becomes due again.

        Returns:
            None after retry state is persisted.

        Raises:
            sqlite3.Error: If durable storage cannot be updated.
        """
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                UPDATE spool_items
                SET attempts=attempts+1, next_attempt_at=?,
                    last_error=?, updated_at=?
                WHERE id=?
                """,
                (now + delay_seconds, error[:512], now, item_id),
            )

    def dead_letter(self, item_id: str, status_code: int | None, reason: str):
        """Move one terminally failed item into the bounded dead-letter table.

        Args:
            item_id: Durable spool item identifier.
            status_code: Optional terminal HTTP status.
            reason: Bounded failure reason.

        Returns:
            None after the item is dead-lettered or when it no longer exists.

        Raises:
            sqlite3.Error: If durable storage cannot be updated.
        """
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                "SELECT kind, body_json, created_at FROM spool_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                return
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
            db.execute("DELETE FROM spool_items WHERE id=?", (item_id,))
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


async def flush_once(client: httpx.AsyncClient | None = None) -> int:
    """Attempt delivery for one bounded batch of due spool items.

    Args:
        client: Optional reusable HTTP client. A temporary client is created when absent.

    Returns:
        Number of items successfully delivered in this iteration.

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
                    await asyncio.to_thread(store.success, item["id"])
                    delivered += 1
                    continue
                if _terminal(item["kind"], response.status_code):
                    await asyncio.to_thread(
                        store.dead_letter,
                        item["id"],
                        response.status_code,
                        f"HTTP {response.status_code}",
                    )
                    LOG.warning(
                        "spool_dead_letter kind=%s id=%s status=%s",
                        item["kind"],
                        item["id"],
                        response.status_code,
                    )
                    continue
                await asyncio.to_thread(
                    store.retry,
                    item["id"],
                    f"HTTP {response.status_code}",
                    _retry_delay(item["attempts"]),
                )
            except Exception as exc:
                await asyncio.to_thread(
                    store.retry,
                    item["id"],
                    exc.__class__.__name__,
                    _retry_delay(item["attempts"]),
                )
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
