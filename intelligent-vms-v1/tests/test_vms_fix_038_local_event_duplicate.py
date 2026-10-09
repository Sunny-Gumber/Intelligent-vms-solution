"""VMS-FIX-038: a duplicate local event returns False and the transaction commits.

The lost read is the same interleaving as the FIX-033 barrier: both callers
miss the existing ``event_id`` and both insert. ``AsyncSession.get`` is forced
to miss so this check does not depend on timing. The row is already committed.
On the old check-then-insert path the second insert raises and rolls the
companion write back. ``INSERT ... ON CONFLICT DO NOTHING`` returns False and
the companion write commits.

SQLite runs in the default suite. PostgreSQL uses the FIX-033 gate: set
``VMS_TEST_POSTGRES_URL`` or ``VMS_ALARM_TRANSACTION_TEST_DATABASE_URL`` to a
disposable database. With neither variable set, the PostgreSQL test skips.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.entities import EventHistoryEntity
from app.services.local_event_store import persist_local_event_once

ROOT = Path(__file__).parents[1]
PRIMARY_ID = "fix038-primary"
COMPANION_ID = "fix038-companion"
WHEN = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


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
    """Migrate a disposable Postgres database for the local-event regression."""
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


def _event(event_id: str, event_type: str, probe: str) -> dict:
    return {
        "event_id": event_id,
        "tenant_id": "tenant-fix038",
        "site_id": "site-fix038",
        "camera_id": "cam-fix038",
        "timestamp": WHEN,
        "event_type": event_type,
        "object_type": None,
        "source": "vms",
        "confidence": None,
        "zone_id": None,
        "severity": "info",
        "snapshot_uri": None,
        "recording_start": None,
        "recording_end": None,
        "attributes": {"probe": probe},
    }


async def _duplicate_keeps_transaction(session_factory) -> None:
    primary = _event(PRIMARY_ID, "motion", "original")
    companion = _event(COMPANION_ID, "camera_offline", "companion")
    raced = _event(PRIMARY_ID, "tamper", "lost-update")

    async with session_factory() as session:
        assert await persist_local_event_once(session, primary) is True
        await session.commit()

    original_get = AsyncSession.get

    async def lost_read(self, entity, ident, *args, **kwargs):
        if entity is EventHistoryEntity and str(ident) == PRIMARY_ID:
            return None
        return await original_get(self, entity, ident, *args, **kwargs)

    AsyncSession.get = lost_read
    try:
        async with session_factory() as session:
            assert await persist_local_event_once(session, companion) is True
            assert await persist_local_event_once(session, raced) is False
            await session.commit()
    finally:
        AsyncSession.get = original_get

    async with session_factory() as session:
        rows = (await session.execute(select(EventHistoryEntity))).scalars().all()
        stored = {
            row.event_id: (row.event_type, dict(row.attributes_json or {}))
            for row in rows
            if row.event_id in {PRIMARY_ID, COMPANION_ID}
        }
    assert stored == {
        PRIMARY_ID: ("motion", {"probe": "original"}),
        COMPANION_ID: ("camera_offline", {"probe": "companion"}),
    }


def test_duplicate_local_event_keeps_sqlite_transaction(tmp_path):
    """A duplicate event_id must not roll back the caller's other SQLite writes."""

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fix038.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(EventHistoryEntity.__table__.create)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            await _duplicate_keeps_transaction(sessions)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_duplicate_local_event_keeps_postgres_transaction(postgres_url):
    """A duplicate event_id must not roll back the caller's other Postgres writes."""

    async def scenario():
        engine = create_async_engine(postgres_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                await session.execute(
                    text("DELETE FROM event_history WHERE event_id IN (:primary, :companion)"),
                    {"primary": PRIMARY_ID, "companion": COMPANION_ID},
                )
                await session.commit()
            await _duplicate_keeps_transaction(sessions)
        finally:
            async with sessions() as session:
                await session.execute(
                    text("DELETE FROM event_history WHERE event_id IN (:primary, :companion)"),
                    {"primary": PRIMARY_ID, "companion": COMPANION_ID},
                )
                await session.commit()
            await engine.dispose()

    asyncio.run(scenario())
