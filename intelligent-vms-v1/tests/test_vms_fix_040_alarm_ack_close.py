"""VMS-FIX-040: a stale acknowledge must not overwrite a closed alarm.

PostgreSQL is required because the lost update is a read-committed write race.
Without ``VMS_TEST_POSTGRES_URL`` or ``VMS_ALARM_TRANSACTION_TEST_DATABASE_URL``
the test skips, the same gate as the FIX-033 race reproductions. The database
is migrated with Alembic and only the synthetic ``fix040-`` rows are removed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import Principal
from app.models.entities import AlarmInstanceEntity, AlarmRuleEntity
from app.routers import alarms

ROOT = Path(__file__).parents[1]
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
COMPETITOR_TIMEOUT = 3.0
HOLDER_TIMEOUT = 10.0
ADMIN = Principal(
    "admin-user",
    frozenset({"admin", "operator"}),
    "tenant-a",
    frozenset({"site-a"}),
)


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
    """Migrate a disposable Postgres database for the acknowledge/close regression."""
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


async def _isolation(sessions) -> str:
    async with sessions() as session:
        value = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
        await session.rollback()
        return value


async def _clear(sessions) -> None:
    async with sessions() as session:
        await session.execute(text("DELETE FROM alarm_instances WHERE id LIKE 'fix040-%'"))
        await session.execute(text("DELETE FROM alarm_rules WHERE id LIKE 'fix040-%'"))
        await session.commit()


async def _seed(sessions, alarm_id: str) -> None:
    rule_id = f"{alarm_id}-rule"
    async with sessions() as session:
        session.add(
            AlarmRuleEntity(
                id=rule_id,
                tenant_id="tenant-a",
                site_id="site-a",
                name="Synthetic",
                enabled=True,
                event_types_json=["motion"],
                severities_json=[],
                camera_ids_json=["cam-a"],
                alarm_severity="high",
                cooldown_seconds=60,
            )
        )
        await session.commit()
        session.add(
            AlarmInstanceEntity(
                id=alarm_id,
                rule_id=rule_id,
                event_id=f"evt-{alarm_id}",
                tenant_id="tenant-a",
                site_id="site-a",
                camera_id="cam-a",
                event_type="motion",
                severity="high",
                state="open",
                message="synthetic",
                dedupe_key=f"dedupe-{alarm_id}",
                opened_at=T0,
                last_event_at=T0,
            )
        )
        await session.commit()


async def _stored_state(sessions, alarm_id: str) -> str:
    async with sessions() as session:
        row = await session.get(AlarmInstanceEntity, alarm_id)
        return row.state


async def _acknowledge_open(sessions) -> dict:
    alarm_id = "fix040-open"
    await _seed(sessions, alarm_id)
    async with sessions() as session:
        body = await alarms.acknowledge_alarm(alarm_id, session, ADMIN)
    return {
        "status": 200,
        "state": body.state,
        "acknowledged_by": body.acknowledged_by,
        "stored": await _stored_state(sessions, alarm_id),
    }


async def _acknowledge_closed(sessions) -> dict:
    alarm_id = "fix040-closed"
    await _seed(sessions, alarm_id)
    async with sessions() as session:
        closed = await alarms.close_alarm(alarm_id, session, ADMIN)
    try:
        async with sessions() as session:
            await alarms.acknowledge_alarm(alarm_id, session, ADMIN)
    except HTTPException as exc:
        return {
            "status": exc.status_code,
            "detail": exc.detail,
            "close_state": closed.state,
            "stored": await _stored_state(sessions, alarm_id),
        }
    return {"status": 200, "detail": None, "close_state": closed.state, "stored": await _stored_state(sessions, alarm_id)}


async def _stale_acknowledge(sessions) -> dict:
    """Pause acknowledge after its load so close can commit first when unlocked."""
    alarm_id = "fix040-race"
    await _seed(sessions, alarm_id)
    loaded = asyncio.Event()
    release = asyncio.Event()
    original = AsyncSession.get
    hits = {"n": 0}

    async def wrapped(self, entity, ident, *args, **kwargs):
        row = await original(self, entity, ident, *args, **kwargs)
        if entity is AlarmInstanceEntity and str(ident) == alarm_id:
            hits["n"] += 1
            if hits["n"] == 1:
                loaded.set()
                try:
                    await asyncio.wait_for(release.wait(), HOLDER_TIMEOUT)
                except asyncio.TimeoutError:
                    pass
        return row

    ack = {"status": None, "error": None, "state": None}
    closed = {"status": None, "error": None, "state": None}

    async def do_ack():
        async with sessions() as session:
            try:
                body = await alarms.acknowledge_alarm(alarm_id, session, ADMIN)
                ack["state"] = body.state
                ack["status"] = 200
            except HTTPException as exc:
                ack["error"] = exc.detail
                ack["status"] = exc.status_code

    async def do_close():
        async with sessions() as session:
            try:
                body = await alarms.close_alarm(alarm_id, session, ADMIN)
                closed["state"] = body.state
                closed["status"] = 200
            except HTTPException as exc:
                closed["error"] = exc.detail
                closed["status"] = exc.status_code

    AsyncSession.get = wrapped
    ack_task = asyncio.create_task(do_ack())
    close_task = None
    blocked = False
    try:
        await asyncio.wait_for(loaded.wait(), HOLDER_TIMEOUT)
        close_task = asyncio.create_task(do_close())
        done, _pending = await asyncio.wait({close_task}, timeout=COMPETITOR_TIMEOUT)
        blocked = close_task not in done
        if blocked:
            release.set()
        await asyncio.wait_for(close_task, HOLDER_TIMEOUT)
        if not blocked:
            release.set()
        await asyncio.wait_for(ack_task, HOLDER_TIMEOUT)
    finally:
        AsyncSession.get = original
        release.set()
        if close_task is not None and not close_task.done():
            close_task.cancel()
        if not ack_task.done():
            ack_task.cancel()
    return {
        "blocked": blocked,
        "final": await _stored_state(sessions, alarm_id),
        "ack": ack,
        "close": closed,
    }


def test_closed_alarm_survives_stale_acknowledge(postgres_url):
    """Close stays stored, and acknowledging an already closed alarm returns 409."""

    async def scenario():
        engine = create_async_engine(
            postgres_url,
            connect_args={"server_settings": {"lock_timeout": "8s", "statement_timeout": "15s"}},
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await _clear(sessions)
            isolation = await _isolation(sessions)
            opened = await _acknowledge_open(sessions)
            closed = await _acknowledge_closed(sessions)
            race = await _stale_acknowledge(sessions)
            return isolation, opened, closed, race
        finally:
            try:
                await _clear(sessions)
            finally:
                await engine.dispose()

    isolation, opened, closed, race = asyncio.run(scenario())
    assert isolation == "read committed"
    assert opened == {
        "status": 200,
        "state": "acknowledged",
        "acknowledged_by": "admin-user",
        "stored": "acknowledged",
    }
    assert closed["status"] == 409
    assert closed["detail"] == "Closed alarm cannot be acknowledged"
    assert closed["close_state"] == "closed"
    assert closed["stored"] == "closed"
    assert race["blocked"] is True, race
    assert race["final"] == "closed", race
    assert race["close"]["status"] == 200
    assert race["close"]["state"] == "closed"
    assert race["ack"]["status"] == 200
    assert race["ack"]["state"] == "acknowledged"
