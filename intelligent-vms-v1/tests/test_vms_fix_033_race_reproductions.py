"""Deterministic reproductions for the VMS-FIX-033 conditional races.

PostgreSQL cases use the migrated schema and two sessions at the server's
default isolation. Each reproduced race asserts the correct outcome, so it
fails on this tree. Those tests are marked ``known_race`` and deselected from
the default suite (see ``tests/conftest.py``). They are not skipped and not
xfailed.

Run the reproductions::

    VMS_TEST_POSTGRES_URL=postgresql+asyncpg://vms:vms@127.0.0.1:5432/vms_fix_033 \\
        pytest -m known_race -q tests/test_vms_fix_033_race_reproductions.py

``VMS_ALARM_TRANSACTION_TEST_DATABASE_URL`` is accepted as the existing
equivalent. The database is migrated and its application rows are truncated.
Point it only at a disposable database. Without either variable the PostgreSQL
tests skip; the ONVIF reproduction does not need a database.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from app.core.auth import Principal
from app.core.config import settings
from app.models.entities import (
    AlarmInstanceEntity,
    AlarmRuleEntity,
    CameraEntity,
    EventHistoryEntity,
    ManualRecordingSessionEntity,
    RecordingHealthStateEntity,
    RecordingPolicyEntity,
)
from app.models.placement import InfrastructureNodeEntity
from app.models.placement_schemas import NodeHeartbeat
from app.routers import alarms, cameras, manual_recordings, placement
from app.services.local_event_store import persist_local_event_once
from app.services.recording import make_record_stream_key
from app.services.recording_health import record_segment_completion

ROOT = Path(__file__).parents[1]
ATTEMPTS = 20
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
ADMIN = Principal(
    "admin-user",
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
    """Migrate a disposable Postgres database and return its SQLAlchemy URL."""
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
    return url


def _fail(name: str, isolation: str, rows: list[dict]) -> None:
    bad = [row for row in rows if not row.get("ok")]
    assert not bad, (
        f"{name}: {len(rows)} attempts, isolation={isolation}, "
        f"{len(bad)} violated the correct outcome. sample={bad[0]!r}"
    )


async def _reset(sessions) -> None:
    async with sessions() as session:
        await session.execute(text(TRUNCATE))
        await session.commit()


async def _isolation(sessions) -> str:
    async with sessions() as session:
        value = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
        await session.rollback()
        return value


async def _open(url: str):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    isolation = await _isolation(sessions)
    return engine, sessions, isolation


def _worker():
    path = ROOT / "services" / "onvif-event-worker" / "main.py"
    spec = importlib.util.spec_from_file_location("onvif_event_worker_fix033", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _onvif_once(worker, *, unexpected: bool) -> dict:
    """Drive one supervisor cycle through the real unsubscribe function."""
    from app.services import onvif_events as onvif_events_mod
    from app.services.onvif_client import OnvifError
    from app.services.onvif_events import PullPointSubscription

    target = worker.Target(
        camera_id="cam-onvif",
        tenant_id="tenant-a",
        site_id="site-a",
        media_node_id="media-local-01",
        username=None,
        password=None,
        event_xaddr="http://192.0.2.40/onvif/event",
    )
    calls = {"pull": 0, "unsub": 0, "create": 0}
    hold = asyncio.Event()
    camera_tasks: list[asyncio.Task] = []
    original_create = asyncio.create_task
    original_wait = asyncio.wait_for
    original_soap = onvif_events_mod._soap

    async def create_pullpoint(*_args, **_kwargs):
        calls["create"] += 1
        return PullPointSubscription(
            address="http://192.0.2.40/onvif/events",
            tenant_id="tenant-a",
            site_id="site-a",
        )

    async def set_synchronization_point(*_args, **_kwargs):
        return None

    async def pull_messages(*_args, **_kwargs):
        calls["pull"] += 1
        if calls["pull"] == 1:
            raise ConnectionError("synthetic pull failure")
        await hold.wait()
        return None

    async def failing_soap(*_args, **_kwargs):
        if unexpected:
            raise RuntimeError("synthetic unexpected unsubscribe failure")
        raise OnvifError("UNSUBSCRIBE_FAILED", "synthetic device unsubscribe failure")

    async def unsubscribe(subscription, username, password):
        calls["unsub"] += 1
        onvif_events_mod._soap = failing_soap
        try:
            await onvif_events_mod.unsubscribe(subscription, username, password)
        finally:
            onvif_events_mod._soap = original_soap

    def track(coro, *args, **kwargs):
        task = original_create(coro, *args, **kwargs)
        if task.get_name().startswith("onvif-event-"):
            camera_tasks.append(task)
        return task

    async def fast_wait(awaitable, timeout=None):
        if timeout is not None and timeout >= 1:
            timeout = 0.01
        return await original_wait(awaitable, timeout)

    async def targets():
        return [target]

    logging.getLogger("onvif-event-worker").setLevel(logging.CRITICAL)
    worker.REFRESH_SECONDS = 0.01
    worker.load_targets = targets
    worker.create_pullpoint = create_pullpoint
    worker.set_synchronization_point = set_synchronization_point
    worker.pull_messages = pull_messages
    worker.unsubscribe = unsubscribe
    asyncio.create_task = track
    asyncio.wait_for = fast_wait
    supervisor = original_create(worker.supervisor())
    try:
        for _ in range(200):
            if calls["unsub"] >= 1:
                break
            await asyncio.sleep(0.02)
        else:
            return {"ok": False, "error": "unsubscribe was not reached", "calls": dict(calls)}
        # Several supervisor refreshes after the unsubscribe returns or kills the task.
        await asyncio.sleep(0.25)
        done = [task for task in camera_tasks if task.done()]
        live = [task for task in camera_tasks if not task.done()]
        error = None
        if done and not done[-1].cancelled():
            exc = done[-1].exception()
            error = f"{type(exc).__name__}: {exc}" if exc else None
        supervised = len(live) == 1 and calls["pull"] >= 2 and error is None
        return {
            "ok": supervised,
            "unexpected": unexpected,
            "pull": calls["pull"],
            "unsub": calls["unsub"],
            "create": calls["create"],
            "camera_tasks": len(camera_tasks),
            "live_camera_tasks": len(live),
            "task_error": error,
        }
    finally:
        hold.set()
        supervisor.cancel()
        for task in camera_tasks:
            task.cancel()
        await asyncio.gather(supervisor, *camera_tasks, return_exceptions=True)
        asyncio.create_task = original_create
        asyncio.wait_for = original_wait
        onvif_events_mod._soap = original_soap


async def _local_event_once(sessions, attempt: int) -> dict:
    event = {
        "event_id": f"evt-{attempt}",
        "tenant_id": "tenant-a",
        "site_id": "site-a",
        "camera_id": "cam-local",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "motion",
        "object_type": None,
        "source": "vms",
        "confidence": None,
        "zone_id": None,
        "severity": "info",
        "snapshot_uri": None,
        "recording_start": None,
        "recording_end": None,
        "attributes": {"probe": "synthetic"},
    }
    first_read = asyncio.Event()
    release = asyncio.Event()
    original = AsyncSession.get
    hits = {"n": 0}

    async def wrapped(self, entity, ident, *args, **kwargs):
        row = await original(self, entity, ident, *args, **kwargs)
        if entity is EventHistoryEntity and str(ident) == event["event_id"]:
            hits["n"] += 1
            if hits["n"] == 1:
                first_read.set()
                await release.wait()
        return row

    async def ingest():
        async with sessions() as session:
            inserted = await persist_local_event_once(session, event)
            await session.commit()
            return inserted

    AsyncSession.get = wrapped
    left_out = {"value": None, "error": None}
    right_out = {"value": None, "error": None}
    try:
        left = asyncio.create_task(ingest())
        await asyncio.wait_for(first_read.wait(), 5)
        try:
            right_out["value"] = await ingest()
        except IntegrityError as exc:
            right_out["error"] = f"IntegrityError: {exc.orig}"
        release.set()
        try:
            left_out["value"] = await left
        except IntegrityError as exc:
            left_out["error"] = f"IntegrityError: {exc.orig}"
        async with sessions() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM event_history WHERE event_id = :event_id"),
                    {"event_id": event["event_id"]},
                )
            ).scalar_one()
        values = [left_out["value"], right_out["value"]]
        ok = (
            count == 1
            and left_out["error"] is None
            and right_out["error"] is None
            and sorted(values) == [False, True]
        )
        return {"ok": ok, "count": count, "left": left_out, "right": right_out}
    finally:
        AsyncSession.get = original
        release.set()


async def _alarm_once(sessions, attempt: int) -> dict:
    rule_id = f"rule-{attempt}"
    alarm_id = f"alarm-{attempt}"
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
                event_id=f"evt-alarm-{attempt}",
                tenant_id="tenant-a",
                site_id="site-a",
                camera_id="cam-a",
                event_type="motion",
                severity="high",
                state="open",
                message="synthetic",
                dedupe_key=f"dedupe-{attempt}",
                opened_at=T0,
                last_event_at=T0,
            )
        )
        await session.commit()

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
                await release.wait()
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

    AsyncSession.get = wrapped
    try:
        ack_task = asyncio.create_task(do_ack())
        await asyncio.wait_for(loaded.wait(), 5)
        async with sessions() as session:
            body = await alarms.close_alarm(alarm_id, session, ADMIN)
            closed["state"] = body.state
            closed["status"] = 200
        release.set()
        await ack_task
    finally:
        AsyncSession.get = original
        release.set()
    async with sessions() as session:
        row = await session.get(AlarmInstanceEntity, alarm_id)
        final = row.state
    return {
        "ok": final == "closed" and ack["status"] == 409,
        "final": final,
        "ack": ack,
        "close": closed,
    }


async def _manual_once(sessions, attempt: int) -> dict:
    camera_id = f"cam-m-{attempt}"
    session_id = f"manual-{attempt}"
    max_stop = T0 + timedelta(seconds=100)
    stop_at = T0 + timedelta(seconds=40)
    async with sessions() as session:
        session.add(
            CameraEntity(
                id=camera_id,
                tenant_id="tenant-a",
                site_id="site-a",
                name="Gate",
                host="192.0.2.10",
                main_path="/main",
                stream_key=f"stream-m-{attempt}",
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

    def _unlocked_manual_select(statement) -> bool:
        if not isinstance(statement, Select):
            return False
        if statement._for_update_arg is not None:
            return False
        return "manual_recording_sessions" in str(statement)

    async def wrapped_execute(self, statement, *args, **kwargs):
        result = await original_execute(self, statement, *args, **kwargs)
        if _unlocked_manual_select(statement):
            listed.set()
            await release.wait()
        return result

    manual_recordings.datetime = Clock
    AsyncSession.execute = wrapped_execute
    try:
        async def do_list():
            async with sessions() as session:
                return await manual_recordings.active_manual_recordings(session, ADMIN)

        listing = asyncio.create_task(do_list())
        await asyncio.wait_for(listed.wait(), 5)
        phase["mode"] = "stop"
        async with sessions() as session:
            await manual_recordings.stop_manual_recording(session_id, session, ADMIN)
        async with sessions() as session:
            midpoint = await session.get(ManualRecordingSessionEntity, session_id)
            mid_at = midpoint.stopped_at
        phase["mode"] = "list"
        release.set()
        await listing
    finally:
        AsyncSession.execute = original_execute
        manual_recordings.datetime = original_dt
        release.set()
    async with sessions() as session:
        final = await session.get(ManualRecordingSessionEntity, session_id)
    return {
        "ok": final.state == "STOPPED" and final.stopped_at == stop_at,
        "mid_stopped_at": mid_at.isoformat(),
        "final_stopped_at": final.stopped_at.isoformat() if final.stopped_at else None,
        "expected_stop": stop_at.isoformat(),
        "max_stop": max_stop.isoformat(),
        "final_state": final.state,
    }


async def _heartbeat_once(sessions, attempt: int, *, concurrent: bool) -> dict:
    node_id = f"node-{attempt}-{'c' if concurrent else 's'}"
    async with sessions() as session:
        session.add(
            InfrastructureNodeEntity(
                id=node_id,
                name=node_id,
                region_id="region-a",
                roles_json=["media"],
                state="active",
                enabled=True,
                endpoints_json={},
                capacity_json={},
                load_json={"cpu": 0},
                heartbeat_at=T0,
                authority_mode="central_online",
            )
        )
        await session.commit()
    newer_at = T0 + timedelta(seconds=20)
    older_at = T0 + timedelta(seconds=5)
    newer = NodeHeartbeat(load={"cpu": 1.0}, authority_mode="central_online", observed_at=newer_at)
    older = NodeHeartbeat(load={"cpu": 9.0}, authority_mode="regional_autonomous", observed_at=older_at)
    service = Principal("svc", frozenset({"admin"}), "tenant-a", frozenset({"*"}), node_id=node_id)

    async def send(payload):
        async with sessions() as session:
            await placement.heartbeat_node(node_id, payload, session, service)

    if concurrent:
        original_commit = AsyncSession.commit
        started = asyncio.Event()
        release = asyncio.Event()
        hits = {"n": 0}

        async def wrapped_commit(self):
            hits["n"] += 1
            if hits["n"] == 1:
                started.set()
                await release.wait()
            return await original_commit(self)

        AsyncSession.commit = wrapped_commit
        try:
            older_task = asyncio.create_task(send(older))
            await asyncio.wait_for(started.wait(), 5)
            await send(newer)
            release.set()
            await older_task
        finally:
            AsyncSession.commit = original_commit
            release.set()
    else:
        await send(newer)
        await send(older)

    async with sessions() as session:
        row = await session.get(InfrastructureNodeEntity, node_id)
        stored_at = row.heartbeat_at
        load = dict(row.load_json)
        mode = row.authority_mode
    return {
        "ok": stored_at == newer_at and float(load.get("cpu", -1)) == 1.0 and mode == "central_online",
        "concurrent": concurrent,
        "stored_heartbeat_at": stored_at.isoformat(),
        "stored_load": load,
        "stored_mode": mode,
        "expected": newer_at.isoformat(),
    }


async def _health_once(sessions, attempt: int) -> dict:
    camera_id = f"cam-h-{attempt}"
    async with sessions() as session:
        session.add(
            CameraEntity(
                id=camera_id,
                tenant_id="tenant-a",
                site_id="site-a",
                name="Health",
                host="192.0.2.20",
                main_path="/main",
                stream_key=f"stream-h-{attempt}",
            )
        )
        session.add(
            RecordingPolicyEntity(
                id=f"pol-h-{attempt}",
                camera_id=camera_id,
                mode="continuous",
                enabled=True,
                record_stream_key=make_record_stream_key(f"stream-h-{attempt}"),
                recording_node_id="media-local-01",
                retention_days=7,
                part_duration_ms=1000,
                segment_duration_seconds=900,
                max_part_size_mb=50,
            )
        )
        session.add(
            RecordingHealthStateEntity(
                camera_id=camera_id,
                last_segment_id="seg-base",
                last_segment_completed_at=T0,
                gap_deadline_at=T0 + timedelta(seconds=1800),
                recording_node_id="media-local-01",
                assignment_generation=1,
                observed_at=T0,
            )
        )
        await session.commit()

    loaded = asyncio.Event()
    release = asyncio.Event()
    original = AsyncSession.get
    hits = {"n": 0}

    async def wrapped(self, entity, ident, *args, **kwargs):
        row = await original(self, entity, ident, *args, **kwargs)
        if entity is RecordingHealthStateEntity and str(ident) == camera_id:
            hits["n"] += 1
            if hits["n"] == 1:
                loaded.set()
                await release.wait()
        return row

    async def apply(segment_id: str, completed_at: datetime):
        async with sessions() as session:
            policy = (
                await session.execute(
                    select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id == camera_id)
                )
            ).scalar_one()
            await record_segment_completion(
                session,
                policy,
                segment_id=segment_id,
                completed_at=completed_at,
                recording_node_id="media-local-01",
                assignment_generation=2,
                observed_at=completed_at,
            )
            await session.commit()

    AsyncSession.get = wrapped
    try:
        earlier = asyncio.create_task(apply("seg-earlier", T0 + timedelta(seconds=10)))
        await asyncio.wait_for(loaded.wait(), 5)
        await apply("seg-later", T0 + timedelta(seconds=20))
        release.set()
        await earlier
    finally:
        AsyncSession.get = original
        release.set()
    async with sessions() as session:
        row = await session.get(RecordingHealthStateEntity, camera_id)
    expected = T0 + timedelta(seconds=20)
    return {
        "ok": row.last_segment_id == "seg-later" and row.last_segment_completed_at == expected,
        "segment": row.last_segment_id,
        "completed_at": row.last_segment_completed_at.isoformat(),
        "expected": expected.isoformat(),
    }


async def _delete_once(sessions, attempt: int) -> dict:
    camera_id = f"cam-d-{attempt}"
    async with sessions() as session:
        session.add(
            CameraEntity(
                id=camera_id,
                tenant_id="tenant-a",
                site_id="site-a",
                name="Delete",
                host="192.0.2.30",
                main_path="/main",
                stream_key=f"stream-d-{attempt}",
            )
        )
        session.add(
            RecordingPolicyEntity(
                id=f"pol-d-{attempt}",
                camera_id=camera_id,
                mode="continuous",
                enabled=True,
                record_stream_key=make_record_stream_key(f"stream-d-{attempt}"),
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
            await release.wait()
        return result

    delete_error = None
    start = {"error": None, "state": None, "camera_id": None}
    AsyncSession.execute = wrapped_execute
    try:
        async def do_delete():
            nonlocal delete_error
            async with sessions() as session:
                try:
                    await cameras.delete_camera(camera_id, session, ADMIN)
                except HTTPException as exc:
                    delete_error = f"{exc.status_code}: {exc.detail}"

        deleting = asyncio.create_task(do_delete())
        await asyncio.wait_for(updated.wait(), 5)
        async with sessions() as session:
            try:
                started = await manual_recordings.start_manual_recording(camera_id, session, ADMIN)
                start["state"] = started.state
                start["camera_id"] = started.camera_id
            except HTTPException as exc:
                start["error"] = f"{exc.status_code}: {exc.detail}"
        release.set()
        await deleting
    finally:
        AsyncSession.execute = original_execute
        cameras.mediamtx.delete_path = original_delete
        settings.placement_execution_enabled = previous_placement
        release.set()

    async with sessions() as session:
        rows = (
            await session.execute(select(ManualRecordingSessionEntity))
        ).scalars().all()
        stored = [
            {"state": row.state, "camera_id": row.camera_id, "stopped_at": str(row.stopped_at)}
            for row in rows
        ]
        camera_left = await session.get(CameraEntity, camera_id) is not None
    active = [row for row in stored if row["state"] == "ACTIVE"]
    # A deleted camera must not keep an ACTIVE manual session, including one
    # whose camera_id was cleared by ON DELETE SET NULL.
    ok = not (camera_left is False and active)
    ok = ok and not any(row["state"] == "ACTIVE" and row["camera_id"] is None for row in stored)
    return {
        "ok": ok,
        "delete_error": delete_error,
        "start": start,
        "stored": stored,
        "camera_exists": camera_left,
    }


async def _repeat(url: str, scenario) -> tuple[str, list[dict]]:
    engine, sessions, isolation = await _open(url)
    rows = []
    try:
        for attempt in range(ATTEMPTS):
            await _reset(sessions)
            try:
                rows.append(await scenario(sessions, attempt))
            except Exception as exc:
                rows.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return isolation, rows
    finally:
        await engine.dispose()


def test_onvif_known_unsubscribe_error_retries():
    """A known ONVIF unsubscribe failure is swallowed and the camera loop retries."""

    async def scenario():
        worker = _worker()
        rows = []
        for _ in range(3):
            rows.append(await _onvif_once(worker, unexpected=False))
        return rows

    rows = asyncio.run(scenario())
    bad = [row for row in rows if not row.get("ok")]
    assert not bad, rows


@pytest.mark.known_race
def test_local_event_duplicate_insert_is_idempotent(postgres_url):
    """Concurrent local-event inserts of one id return one success and one False."""

    async def scenario():
        return await _repeat(postgres_url, _local_event_once)

    isolation, rows = asyncio.run(scenario())
    _fail("local-event duplicate insert", isolation, rows)


@pytest.mark.known_race
def test_onvif_unexpected_unsubscribe_keeps_camera_supervised():
    """An unexpected unsubscribe error must not leave the camera unsupervised."""

    async def scenario():
        worker = _worker()
        return [await _onvif_once(worker, unexpected=True) for _ in range(ATTEMPTS)]

    rows = asyncio.run(scenario())
    bad = [row for row in rows if not row.get("ok")]
    assert not bad, (
        f"ONVIF unexpected unsubscribe: {len(rows)} attempts, "
        f"{len(bad)} left the camera unsupervised. sample={bad[0]!r}"
    )


@pytest.mark.known_race
def test_alarm_close_is_not_overwritten_by_stale_acknowledge(postgres_url):
    """A committed close stays closed when a stale acknowledge finishes later."""

    async def scenario():
        return await _repeat(postgres_url, _alarm_once)

    isolation, rows = asyncio.run(scenario())
    _fail("alarm acknowledge versus close", isolation, rows)


@pytest.mark.known_race
def test_manual_list_expiry_preserves_earlier_stop(postgres_url):
    """List expiry must not move stopped_at later than an earlier explicit stop."""

    async def scenario():
        return await _repeat(postgres_url, _manual_once)

    isolation, rows = asyncio.run(scenario())
    _fail("manual list-expiry versus earlier stop", isolation, rows)


@pytest.mark.known_race
def test_out_of_order_heartbeat_does_not_regress(postgres_url):
    """An older heartbeat must not replace a newer observation, load, or mode."""

    async def scenario():
        engine, sessions, isolation = await _open(postgres_url)
        rows = []
        try:
            for attempt in range(ATTEMPTS):
                await _reset(sessions)
                for concurrent in (False, True):
                    try:
                        rows.append(await _heartbeat_once(sessions, attempt, concurrent=concurrent))
                    except Exception as exc:
                        rows.append({"ok": False, "concurrent": concurrent, "error": f"{type(exc).__name__}: {exc}"})
                    await _reset(sessions)
            return isolation, rows
        finally:
            await engine.dispose()

    isolation, rows = asyncio.run(scenario())
    bad = [row for row in rows if not row.get("ok")]
    sequential = [row for row in bad if not row.get("concurrent")]
    concurrent = [row for row in bad if row.get("concurrent")]
    assert not bad, (
        f"out-of-order heartbeat: {len(rows)} attempts, isolation={isolation}, "
        f"{len(sequential)} sequential and {len(concurrent)} concurrent violated the newer observation. "
        f"sequential_sample={sequential[:1]!r} concurrent_sample={concurrent[:1]!r}"
    )


@pytest.mark.known_race
def test_recording_health_completion_is_monotonic(postgres_url):
    """An older segment completion must not overwrite a newer one."""

    async def scenario():
        return await _repeat(postgres_url, _health_once)

    isolation, rows = asyncio.run(scenario())
    _fail("recording-health monotonic write", isolation, rows)


@pytest.mark.known_race
def test_camera_delete_leaves_no_active_manual_recording(postgres_url):
    """Deleting a camera must not leave an ACTIVE manual session behind."""

    async def scenario():
        return await _repeat(postgres_url, _delete_once)

    isolation, rows = asyncio.run(scenario())
    _fail("camera delete versus active manual recording", isolation, rows)
