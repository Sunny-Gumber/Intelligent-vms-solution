"""VMS-FIX-042: an older or equal node heartbeat must not replace a newer one.

PostgreSQL is required because the handler's conditional update is the
behavior under test. The gate matches the FIX-033 race reproductions: set
``VMS_TEST_POSTGRES_URL`` or ``VMS_ALARM_TRANSACTION_TEST_DATABASE_URL`` to a
disposable database. With neither variable set, the test skips.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal
from app.models.placement import InfrastructureNodeEntity
from app.models.placement_schemas import NodeHeartbeat
from app.routers import placement

ROOT = Path(__file__).parents[1]
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
NODE_ID = "fix042-heartbeat-node"


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
    """Migrate a disposable Postgres database for the heartbeat regression."""
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


def _cpu(load: dict) -> float:
    return float(load["cpu"])


async def _stored(sessions):
    async with sessions() as session:
        row = await session.get(InfrastructureNodeEntity, NODE_ID)
        return row.heartbeat_at, dict(row.load_json), row.authority_mode


async def _send(sessions, service, payload):
    async with sessions() as session:
        return await placement.heartbeat_node(NODE_ID, payload, session, service)


async def _heartbeat_order(url: str) -> None:
    assert T0 + timedelta(seconds=20) < datetime.now(timezone.utc)
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = Principal("svc", frozenset({"admin"}), "*", frozenset({"*"}), node_id=NODE_ID)
    newer_at = T0 + timedelta(seconds=20)
    older_at = T0 + timedelta(seconds=5)
    try:
        async with sessions() as session:
            existing = await session.get(InfrastructureNodeEntity, NODE_ID)
            if existing is not None:
                await session.delete(existing)
                await session.commit()
            session.add(
                InfrastructureNodeEntity(
                    id=NODE_ID,
                    name=NODE_ID,
                    region_id="region-a",
                    roles_json=["media"],
                    state="active",
                    enabled=True,
                    endpoints_json={},
                    capacity_json={},
                    load_json={"cpu": 0.0},
                    heartbeat_at=T0,
                    authority_mode="central_online",
                )
            )
            await session.commit()

        first = await _send(
            sessions,
            service,
            NodeHeartbeat(load={"cpu": 9.0}, authority_mode="regional_autonomous", observed_at=older_at),
        )
        assert first.heartbeat_at == older_at
        assert _cpu(first.load) == 9.0
        assert first.authority_mode == "regional_autonomous"

        second = await _send(
            sessions,
            service,
            NodeHeartbeat(load={"cpu": 1.0}, authority_mode="central_online", observed_at=newer_at),
        )
        assert second.heartbeat_at == newer_at
        assert _cpu(second.load) == 1.0
        assert second.authority_mode == "central_online"

        rejected = await _send(
            sessions,
            service,
            NodeHeartbeat(load={"cpu": 9.0}, authority_mode="regional_autonomous", observed_at=older_at),
        )
        stored_at, stored_load, stored_mode = await _stored(sessions)
        assert rejected.heartbeat_at == newer_at
        assert _cpu(rejected.load) == 1.0
        assert rejected.authority_mode == "central_online"
        assert stored_at == newer_at
        assert _cpu(stored_load) == 1.0
        assert stored_mode == "central_online"

        equal = await _send(
            sessions,
            service,
            NodeHeartbeat(load={"cpu": 4.0}, authority_mode="regional_autonomous", observed_at=newer_at),
        )
        stored_at, stored_load, stored_mode = await _stored(sessions)
        assert equal.heartbeat_at == newer_at
        assert _cpu(equal.load) == 1.0
        assert equal.authority_mode == "central_online"
        assert stored_at == newer_at
        assert _cpu(stored_load) == 1.0
        assert stored_mode == "central_online"

        before = datetime.now(timezone.utc)
        future_at = before + timedelta(hours=1)
        clamped = await _send(
            sessions,
            service,
            NodeHeartbeat(load={"cpu": 2.0}, authority_mode="central_online", observed_at=future_at),
        )
        after = datetime.now(timezone.utc)
        stored_at, stored_load, stored_mode = await _stored(sessions)
        assert before <= clamped.heartbeat_at <= after
        assert clamped.heartbeat_at != future_at
        assert _cpu(clamped.load) == 2.0
        assert clamped.authority_mode == "central_online"
        assert stored_at == clamped.heartbeat_at
        assert _cpu(stored_load) == 2.0
        assert stored_mode == "central_online"
    finally:
        async with sessions() as session:
            row = await session.get(InfrastructureNodeEntity, NODE_ID)
            if row is not None:
                await session.delete(row)
                await session.commit()
        await engine.dispose()


def test_older_or_equal_heartbeat_does_not_overwrite_newer_node_state(postgres_url):
    """A stale or equal observation leaves the newer heartbeat, load and mode."""
    asyncio.run(_heartbeat_order(postgres_url))
