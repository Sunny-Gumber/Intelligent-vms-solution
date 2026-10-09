"""VMS-FIX-044: camera delete must not leave an ACTIVE manual recording.

One interleaving of the FIX-033 camera-delete race. Delete pauses after its
active-session stop and before it deletes the camera. Start then tries to
insert. The assertions are the same final state the reproduction requires:
delete commits, the camera row is gone, and no ACTIVE session remains.

PostgreSQL only. Without ``VMS_TEST_POSTGRES_URL`` or
``VMS_ALARM_TRANSACTION_TEST_DATABASE_URL`` the test skips. Point the URL at
a disposable database; the fixture migrates it and truncates application rows.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

from app.core.auth import Principal
from app.core.config import settings
from app.models.entities import CameraEntity, ManualRecordingSessionEntity, RecordingPolicyEntity
from app.routers import cameras, manual_recordings
from app.services.recording import make_record_stream_key


ROOT = Path(__file__).parents[1]
COMPETITOR_TIMEOUT = 3.0
HOLDER_TIMEOUT = 10.0
ATTEMPT_TIMEOUT = 20.0
ADMIN = Principal(
    "fix044-operator",
    frozenset({"admin", "operator"}),
    "tenant-a",
    frozenset({"site-a"}),
)
TRUNCATE = (
    "TRUNCATE event_history, alarm_instances, alarm_rules, "
    "manual_recording_sessions, recording_health_state, recording_policies, "
    "camera_health_state, cameras, infrastructure_nodes RESTART IDENTITY CASCADE"
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
    """Migrate a disposable Postgres database and truncate it after the module."""
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
    asyncio.run(_truncate(url))


async def _truncate(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(TRUNCATE))
    finally:
        await engine.dispose()


async def _await_release(release: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(release.wait(), HOLDER_TIMEOUT)
    except asyncio.TimeoutError:
        return


async def _finish_or_release(task: asyncio.Task, release: asyncio.Event) -> bool:
    """Return True when ``task`` is still waiting on the holder's lock."""
    done, _pending = await asyncio.wait({task}, timeout=COMPETITOR_TIMEOUT)
    blocked = task not in done
    if blocked:
        release.set()
    await asyncio.wait_for(task, HOLDER_TIMEOUT)
    return blocked


async def _interleave(url: str) -> dict:
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"lock_timeout": "8s", "statement_timeout": "15s"}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    camera_id = "cam-fix044"
    try:
        async with sessions() as session:
            await session.execute(text(TRUNCATE))
            session.add(
                CameraEntity(
                    id=camera_id,
                    tenant_id="tenant-a",
                    site_id="site-a",
                    name="Fix044",
                    host="192.0.2.44",
                    main_path="/main",
                    stream_key="stream-fix044",
                )
            )
            session.add(
                RecordingPolicyEntity(
                    id="pol-fix044",
                    camera_id=camera_id,
                    mode="continuous",
                    enabled=True,
                    record_stream_key=make_record_stream_key("stream-fix044"),
                    recording_node_id="media-local-01",
                    retention_days=7,
                    part_duration_ms=1000,
                    segment_duration_seconds=900,
                    max_part_size_mb=50,
                )
            )
            await session.commit()

        original_delete = cameras.mediamtx.delete_path
        original_execute = AsyncSession.execute
        previous_placement = settings.placement_execution_enabled
        settings.placement_execution_enabled = False

        async def noop_delete(_path: str) -> None:
            return None

        cameras.mediamtx.delete_path = noop_delete
        updated = asyncio.Event()
        release = asyncio.Event()

        async def wrapped_execute(self, statement, *args, **kwargs):
            result = await original_execute(self, statement, *args, **kwargs)
            if isinstance(statement, Update) and getattr(statement.table, "name", "") == "manual_recording_sessions":
                updated.set()
                await _await_release(release)
            return result

        delete_error = None
        start = {"error": None, "state": None, "camera_id": None}
        AsyncSession.execute = wrapped_execute
        blocked = False
        try:
            async def do_delete():
                nonlocal delete_error
                async with sessions() as session:
                    try:
                        await cameras.delete_camera(camera_id, session, ADMIN)
                    except HTTPException as exc:
                        delete_error = f"{exc.status_code}: {exc.detail}"

            async def do_start():
                async with sessions() as session:
                    try:
                        started = await manual_recordings.start_manual_recording(camera_id, session, ADMIN)
                        start["state"] = started.state
                        start["camera_id"] = started.camera_id
                    except HTTPException as exc:
                        start["error"] = f"{exc.status_code}: {exc.detail}"

            deleting = asyncio.create_task(do_delete())
            try:
                await asyncio.wait_for(updated.wait(), HOLDER_TIMEOUT)
            except asyncio.TimeoutError:
                release.set()
                await asyncio.wait_for(deleting, HOLDER_TIMEOUT)
                return {
                    "ok": False,
                    "error": "delete did not update manual sessions",
                    "delete_error": delete_error,
                }
            start_task = asyncio.create_task(do_start())
            blocked = await _finish_or_release(start_task, release)
            if not blocked:
                release.set()
            await asyncio.wait_for(deleting, HOLDER_TIMEOUT)
        finally:
            AsyncSession.execute = original_execute
            cameras.mediamtx.delete_path = original_delete
            settings.placement_execution_enabled = previous_placement
            release.set()

        async with sessions() as session:
            rows = (await session.execute(select(ManualRecordingSessionEntity))).scalars().all()
            stored = [
                {"state": row.state, "camera_id": row.camera_id, "stopped_at": str(row.stopped_at)}
                for row in rows
            ]
            camera_exists = await session.get(CameraEntity, camera_id) is not None
        active = [row for row in stored if row["state"] == "ACTIVE"]
        ok = (
            delete_error is None
            and camera_exists is False
            and not active
            and all(row["state"] == "STOPPED" for row in stored)
        )
        return {
            "ok": ok,
            "blocked": blocked,
            "delete_error": delete_error,
            "start": start,
            "stored": stored,
            "camera_exists": camera_exists,
        }
    finally:
        await engine.dispose()


def test_delete_during_manual_start_leaves_no_active_session(postgres_url):
    """A start that races the delete gap must not remain ACTIVE after delete."""

    async def scenario():
        return await asyncio.wait_for(_interleave(postgres_url), ATTEMPT_TIMEOUT)

    outcome = asyncio.run(scenario())
    assert outcome["ok"], outcome
    assert outcome["delete_error"] is None
    assert outcome["camera_exists"] is False
    assert all(row["state"] == "STOPPED" for row in outcome["stored"])
    assert not any(row["state"] == "ACTIVE" for row in outcome["stored"])
