"""VMS-FIX-043: recording-health completion must stay monotonic.

PostgreSQL is required because the conditional update and the first-insert
conflict are the behavior under test. The gate matches the FIX-033 race
reproductions: set ``VMS_TEST_POSTGRES_URL`` or
``VMS_ALARM_TRANSACTION_TEST_DATABASE_URL`` to a disposable database. With
neither variable set, these tests skip.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.entities import CameraEntity, RecordingHealthStateEntity, RecordingPolicyEntity
from app.services.recording import make_record_stream_key
from app.services.recording_health import record_segment_completion, recording_gap_deadline

ROOT = Path(__file__).parents[1]
ATTEMPTS = 20
COMPETITOR_TIMEOUT = 3.0
HOLDER_TIMEOUT = 10.0
ATTEMPT_TIMEOUT = 20.0
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
LATER_AT = T0 + timedelta(seconds=20)
EARLIER_AT = T0 + timedelta(seconds=10)


def _postgres_url() -> str | None:
    raw = os.environ.get("VMS_TEST_POSTGRES_URL") or os.environ.get(
        "VMS_ALARM_TRANSACTION_TEST_DATABASE_URL"
    )
    if not raw:
        return None
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://") :]
    return raw


@pytest.fixture(scope="module")
def postgres_url():
    """Migrate a disposable Postgres database for the recording-health regression."""
    url = _postgres_url()
    if not url:
        pytest.skip(
            "PostgreSQL race reproductions need VMS_TEST_POSTGRES_URL "
            "or VMS_ALARM_TRANSACTION_TEST_DATABASE_URL"
        )
    if not url.startswith("postgresql"):
        scheme = url.split(":", 1)[0]
        pytest.skip(f"VMS test Postgres URL must use the postgresql scheme, got {scheme}")
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env.setdefault("VMS_SECRET_KEY", "ci-test-key")
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
    )
    yield url


def _engine(url: str):
    return create_async_engine(
        url,
        connect_args={"server_settings": {"lock_timeout": "8s", "statement_timeout": "15s"}},
    )


async def _seed(session, camera_id: str, *, health: bool) -> None:
    session.add(
        CameraEntity(
            id=camera_id,
            tenant_id="tenant-fix043",
            site_id="site-fix043",
            name="Fix043",
            host="192.0.2.43",
            main_path="/main",
            stream_key=f"stream-{camera_id}",
        )
    )
    session.add(
        RecordingPolicyEntity(
            id=f"pol-{camera_id}",
            camera_id=camera_id,
            mode="continuous",
            enabled=True,
            record_stream_key=make_record_stream_key(f"stream-{camera_id}"),
            recording_node_id="node-base",
            retention_days=7,
            part_duration_ms=1000,
            segment_duration_seconds=900,
            max_part_size_mb=50,
        )
    )
    if health:
        session.add(
            RecordingHealthStateEntity(
                camera_id=camera_id,
                last_segment_id=None,
                last_segment_completed_at=None,
                gap_deadline_at=None,
                recording_node_id=None,
                assignment_generation=None,
                observed_at=T0,
            )
        )
    await session.commit()


async def _cleanup(sessions) -> None:
    async with sessions() as session:
        await session.execute(delete(CameraEntity).where(CameraEntity.id.like("fix043-%")))
        await session.commit()


async def _policy(session, camera_id: str) -> RecordingPolicyEntity:
    return (
        await session.execute(
            select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id == camera_id)
        )
    ).scalar_one()


async def _stored(sessions, camera_id: str):
    async with sessions() as session:
        row = await session.get(RecordingHealthStateEntity, camera_id)
        if row is None:
            return None
        return {
            "segment": row.last_segment_id,
            "completed_at": row.last_segment_completed_at,
            "node": row.recording_node_id,
            "generation": row.assignment_generation,
            "gap_deadline_at": row.gap_deadline_at,
        }


def _snapshot(row: RecordingHealthStateEntity) -> dict:
    return {
        "segment": row.last_segment_id,
        "completed_at": row.last_segment_completed_at,
        "node": row.recording_node_id,
        "generation": row.assignment_generation,
        "gap_deadline_at": row.gap_deadline_at,
    }


async def _apply(sessions, camera_id: str, segment_id: str, completed_at: datetime, node: str, generation: int):
    async with sessions() as session:
        policy = await _policy(session, camera_id)
        row = await record_segment_completion(
            session,
            policy,
            segment_id=segment_id,
            completed_at=completed_at,
            recording_node_id=node,
            assignment_generation=generation,
            observed_at=completed_at,
        )
        await session.commit()
        return _snapshot(row)


async def _isolation(sessions) -> str:
    async with sessions() as session:
        value = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
        await session.rollback()
        return value


async def _first_insert_once(sessions, attempt: int, *, later_resumes_last: bool) -> dict:
    label = "later-last" if later_resumes_last else "earlier-last"
    camera_id = f"fix043-{label}-{attempt}"
    async with sessions() as session:
        await _seed(session, camera_id, health=False)

    loaded = asyncio.Event()
    release = asyncio.Event()
    original = AsyncSession.get
    hits = {"n": 0}

    async def wrapped(self, entity, ident, *args, **kwargs):
        row = await original(self, entity, ident, *args, **kwargs)
        if entity is RecordingHealthStateEntity and str(ident) == camera_id and row is None:
            hits["n"] += 1
            if hits["n"] == 1:
                loaded.set()
                try:
                    await asyncio.wait_for(release.wait(), HOLDER_TIMEOUT)
                except asyncio.TimeoutError:
                    return row
        return row

    async def run(segment_id: str, completed_at: datetime, node: str, generation: int):
        try:
            return await _apply(sessions, camera_id, segment_id, completed_at, node, generation)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    # The task started first is the one whose miss is paused. The other writer
    # then commits. Releasing the first task is the stale continuation.
    paused = ("seg-later", LATER_AT, "node-later", 2) if later_resumes_last else (
        "seg-earlier",
        EARLIER_AT,
        "node-earlier",
        1,
    )
    other = ("seg-earlier", EARLIER_AT, "node-earlier", 1) if later_resumes_last else (
        "seg-later",
        LATER_AT,
        "node-later",
        2,
    )
    AsyncSession.get = wrapped
    paused_task = asyncio.create_task(run(*paused))
    other_task = None
    blocked = False
    try:
        if not await _wait(loaded):
            release.set()
            paused_result = await asyncio.wait_for(paused_task, HOLDER_TIMEOUT)
            return {"ok": False, "error": "first insert did not miss the health row", "paused": paused_result}
        other_task = asyncio.create_task(run(*other))
        done, _pending = await asyncio.wait({other_task}, timeout=COMPETITOR_TIMEOUT)
        blocked = other_task not in done
        if blocked:
            release.set()
        other_result = await asyncio.wait_for(other_task, HOLDER_TIMEOUT)
        if not blocked:
            release.set()
        paused_result = await asyncio.wait_for(paused_task, HOLDER_TIMEOUT)
    finally:
        AsyncSession.get = original
        release.set()
        if other_task is not None and not other_task.done():
            other_task.cancel()
        if not paused_task.done():
            paused_task.cancel()

    stored = await _stored(sessions, camera_id)
    expected_gap = recording_gap_deadline(LATER_AT, 900)
    ok = (
        stored is not None
        and "error" not in paused_result
        and "error" not in other_result
        and stored["segment"] == "seg-later"
        and stored["completed_at"] == LATER_AT
        and stored["node"] == "node-later"
        and stored["generation"] == 2
        and stored["gap_deadline_at"] == expected_gap
        and paused_result["segment"] == "seg-later"
        and paused_result["completed_at"] == LATER_AT
    )
    return {
        "ok": ok,
        "blocked": blocked,
        "later_resumes_last": later_resumes_last,
        "paused": paused_result,
        "other": other_result,
        "stored": stored,
    }


async def _wait(event: asyncio.Event) -> bool:
    try:
        await asyncio.wait_for(event.wait(), HOLDER_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        return False


async def _repeat(url: str, later_resumes_last: bool) -> tuple[str, list[dict]]:
    engine = _engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    rows = []
    try:
        isolation = await _isolation(sessions)
        for attempt in range(ATTEMPTS):
            try:
                rows.append(
                    await asyncio.wait_for(
                        _first_insert_once(sessions, attempt, later_resumes_last=later_resumes_last),
                        ATTEMPT_TIMEOUT,
                    )
                )
            except Exception as exc:
                rows.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return isolation, rows
    finally:
        try:
            await _cleanup(sessions)
        finally:
            await engine.dispose()


def _assert_attempts(name: str, isolation: str, rows: list[dict]) -> None:
    bad = [row for row in rows if not row.get("ok")]
    assert not bad, (
        f"{name}: {len(rows)} attempts, isolation={isolation}, "
        f"{len(bad)} violated the correct outcome. sample={bad[0]!r}"
    )


def test_first_insert_older_completion_does_not_overwrite_newer(postgres_url):
    """A first insert that resumes after a newer insert must leave the newer row."""

    async def scenario():
        return await _repeat(postgres_url, later_resumes_last=False)

    isolation, rows = asyncio.run(scenario())
    _assert_attempts("recording-health first insert older resume", isolation, rows)


def test_first_insert_newer_completion_advances_older_row(postgres_url):
    """A newer first insert that resumes after an older insert must advance the row."""

    async def scenario():
        return await _repeat(postgres_url, later_resumes_last=True)

    isolation, rows = asyncio.run(scenario())
    _assert_attempts("recording-health first insert newer resume", isolation, rows)


async def _boundaries(url: str) -> None:
    engine = _engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            await _seed(session, "fix043-boundary", health=False)
            await _seed(session, "fix043-null", health=True)

        newer = await _apply(sessions, "fix043-boundary", "seg-newer", LATER_AT, "node-later", 2)
        older = await _apply(sessions, "fix043-boundary", "seg-older", EARLIER_AT, "node-earlier", 1)
        stored = await _stored(sessions, "fix043-boundary")
        assert newer["segment"] == "seg-newer"
        assert newer["completed_at"] == LATER_AT
        assert older["segment"] == "seg-newer"
        assert older["completed_at"] == LATER_AT
        assert older["node"] == "node-later"
        assert older["generation"] == 2
        assert stored == older

        equal = await _apply(sessions, "fix043-boundary", "seg-equal", LATER_AT, "node-equal", 3)
        stored = await _stored(sessions, "fix043-boundary")
        assert equal["segment"] == "seg-equal"
        assert equal["completed_at"] == LATER_AT
        assert equal["node"] == "node-equal"
        assert equal["generation"] == 3
        assert stored["segment"] == "seg-equal"

        advanced = await _apply(sessions, "fix043-null", "seg-null", EARLIER_AT, "node-null", 4)
        stored = await _stored(sessions, "fix043-null")
        assert advanced["segment"] == "seg-null"
        assert advanced["completed_at"] == EARLIER_AT
        assert stored["segment"] == "seg-null"
        assert stored["gap_deadline_at"] == recording_gap_deadline(EARLIER_AT, 900)
    finally:
        try:
            await _cleanup(sessions)
        finally:
            await engine.dispose()


def test_recording_health_keeps_newer_equal_and_null_boundaries(postgres_url):
    """Older evidence stays put, an equal instant advances, and NULL can be filled."""
    asyncio.run(_boundaries(postgres_url))
