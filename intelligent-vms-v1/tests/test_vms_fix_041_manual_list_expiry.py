"""VMS-FIX-041: list expiry must not lengthen an earlier explicit stop.

PostgreSQL is required for the lost-update regression. The gate matches the
FIX-033 race reproductions: set ``VMS_TEST_POSTGRES_URL`` or
``VMS_ALARM_TRANSACTION_TEST_DATABASE_URL`` to a disposable database. With
neither variable set, the PostgreSQL test skips. A separate in-memory SQLite
test covers ordinary cap expiry, which is the default local database.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import Principal
from app.db.base import Base
from app.models.entities import CameraEntity, ManualRecordingSessionEntity
from app.routers import manual_recordings

ROOT = Path(__file__).parents[1]
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
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
    """Migrate a disposable Postgres database for the list-expiry regression."""
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


async def _insert(sessions, camera_id: str, session_id: str, max_stop: datetime) -> None:
    async with sessions() as session:
        session.add(
            CameraEntity(
                id=camera_id,
                tenant_id="tenant-a",
                site_id="site-a",
                name="Gate",
                host="192.0.2.41",
                main_path="/main",
                stream_key=f"stream-{camera_id}",
            )
        )
        session.add(
            ManualRecordingSessionEntity(
                id=session_id,
                tenant_id="tenant-a",
                site_id="site-a",
                camera_id=camera_id,
                operator_subject="admin-user",
                state="ACTIVE",
                started_at=T0,
                max_stop_at=max_stop,
            )
        )
        await session.commit()


async def _delete(sessions, camera_id: str, session_id: str) -> None:
    async with sessions() as session:
        row = await session.get(ManualRecordingSessionEntity, session_id)
        if row is not None:
            await session.delete(row)
        camera = await session.get(CameraEntity, camera_id)
        if camera is not None:
            await session.delete(camera)
        await session.commit()


async def _stored(sessions, session_id: str):
    async with sessions() as session:
        row = await session.get(ManualRecordingSessionEntity, session_id)
        return row.state, row.stopped_at


def _is_active_list(statement) -> bool:
    rendered = " ".join(str(statement).split())
    if "manual_recording_sessions" not in rendered or " WHERE " not in rendered:
        return False
    where = rendered.split(" WHERE ", 1)[1]
    return ".state " in where or ".state=" in where


async def _earlier_stop_kept(url: str) -> None:
    """List selects ACTIVE, stop commits, then list expiry must not rewrite it."""
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"lock_timeout": "8s", "statement_timeout": "15s"}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    camera_id = "fix041-cam-race"
    session_id = "fix041-manual-race"
    max_stop = T0 + timedelta(seconds=100)
    stop_at = T0 + timedelta(seconds=40)
    try:
        async with sessions() as session:
            isolation = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
            await session.rollback()
        assert isolation == "read committed"
        await _insert(sessions, camera_id, session_id, max_stop)

        phase = {"mode": "list"}

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                if phase["mode"] == "stop":
                    return stop_at
                return max_stop

        original_dt = manual_recordings.datetime
        original_execute = AsyncSession.execute
        listed = asyncio.Event()
        release = asyncio.Event()

        async def wrapped_execute(self, statement, *args, **kwargs):
            result = await original_execute(self, statement, *args, **kwargs)
            if _is_active_list(statement):
                listed.set()
                try:
                    await asyncio.wait_for(release.wait(), HOLDER_TIMEOUT)
                except asyncio.TimeoutError:
                    return result
            return result

        manual_recordings.datetime = Clock
        AsyncSession.execute = wrapped_execute
        listing = None
        try:
            async def do_list():
                async with sessions() as session:
                    return await manual_recordings.active_manual_recordings(session, ADMIN)

            async def do_stop():
                async with sessions() as session:
                    return await manual_recordings.stop_manual_recording(session_id, session, ADMIN)

            listing = asyncio.create_task(do_list())
            await asyncio.wait_for(listed.wait(), HOLDER_TIMEOUT)
            phase["mode"] = "stop"
            stopped = await asyncio.wait_for(do_stop(), HOLDER_TIMEOUT)
            async with sessions() as session:
                midpoint = await session.get(ManualRecordingSessionEntity, session_id)
            assert midpoint.stopped_at == stop_at
            phase["mode"] = "list"
            release.set()
            visible = await asyncio.wait_for(listing, HOLDER_TIMEOUT)
        finally:
            AsyncSession.execute = original_execute
            manual_recordings.datetime = original_dt
            release.set()
            if listing is not None and not listing.done():
                listing.cancel()
                await asyncio.gather(listing, return_exceptions=True)

        state, stored_stop = await _stored(sessions, session_id)
        assert stopped.state == "STOPPED"
        assert stopped.stopped_at == stop_at
        assert state == "STOPPED"
        assert stored_stop == stop_at
        assert stored_stop != max_stop
        assert [item.id for item in visible] == []

        open_camera = "fix041-cam-open"
        open_session = "fix041-manual-open"
        open_cap = T0 + timedelta(seconds=100)
        try:
            await _insert(sessions, open_camera, open_session, open_cap)
            manual_recordings.datetime = type(
                "OpenClock",
                (datetime,),
                {"now": classmethod(lambda cls, tz=None: T0 + timedelta(seconds=10))},
            )
            async with sessions() as session:
                visible = await manual_recordings.active_manual_recordings(session, ADMIN)
            state, stored_stop = await _stored(sessions, open_session)
            assert [item.id for item in visible] == [open_session]
            assert visible[0].state == "ACTIVE"
            assert state == "ACTIVE"
            assert stored_stop is None
        finally:
            manual_recordings.datetime = original_dt
            await _delete(sessions, open_camera, open_session)

        cap_camera = "fix041-cam-cap"
        cap_session = "fix041-manual-cap"
        try:
            await _insert(sessions, cap_camera, cap_session, max_stop)
            manual_recordings.datetime = type(
                "CapClock",
                (datetime,),
                {"now": classmethod(lambda cls, tz=None: max_stop)},
            )
            async with sessions() as session:
                visible = await manual_recordings.active_manual_recordings(session, ADMIN)
            state, stored_stop = await _stored(sessions, cap_session)
            assert [item.id for item in visible] == []
            assert state == "STOPPED"
            assert stored_stop == max_stop
        finally:
            manual_recordings.datetime = original_dt
            await _delete(sessions, cap_camera, cap_session)
    finally:
        await _delete(sessions, camera_id, session_id)
        await engine.dispose()


def test_list_expiry_keeps_an_earlier_explicit_stop(postgres_url):
    """A committed stop at 12:00:40Z stays there when list expiry runs afterwards."""
    asyncio.run(_earlier_stop_kept(postgres_url))


def test_sqlite_cap_expiry_stops_an_active_session_at_max_stop():
    """The default SQLite database still finalizes an overdue active session."""

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        camera_id = "fix041-sqlite-cam"
        session_id = "fix041-sqlite-manual"
        max_stop = T0 + timedelta(seconds=100)
        original_dt = manual_recordings.datetime
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            await _insert(sessions, camera_id, session_id, max_stop)
            manual_recordings.datetime = type(
                "CapClock",
                (datetime,),
                {"now": classmethod(lambda cls, tz=None: max_stop)},
            )
            async with sessions() as session:
                visible = await manual_recordings.active_manual_recordings(session, ADMIN)
            state, stored_stop = await _stored(sessions, session_id)
            # SQLite returns TIMESTAMP values without tzinfo. The column is UTC.
            if stored_stop is not None and stored_stop.tzinfo is None:
                stored_stop = stored_stop.replace(tzinfo=timezone.utc)
            assert visible == []
            assert state == "STOPPED"
            assert stored_stop == max_stop
        finally:
            manual_recordings.datetime = original_dt
            await engine.dispose()

    asyncio.run(scenario())
