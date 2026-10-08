"""SEC-06 regression: a persisted SQLite health monitor must run more than once.

The real ``HealthMonitor.run_once`` writes an aware UTC ``observed_at``. SQLite
returns that column as a naive datetime. A later pass subtracts it from an
aware clock and, before the fix, raises ``TypeError``. Probes and MediaMTX are
faked. The database is a file-backed aiosqlite engine, and each pass uses a
new engine plus a new monitor so restart persistence is actual.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.entities import CameraEntity, CameraHealthStateEntity
from app.services import health_monitor as health_monitor_module
from app.services.health_monitor import HealthMonitor, MonitorStats

CAMERA_ID = "camera-fix-016"
T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
HEARTBEAT_SECONDS = 300
_PROBE_READY = {"value": False}


class MovableClock(datetime):
    """Deterministic clock so heartbeat expiry does not depend on wall time."""

    current = T0

    @classmethod
    def now(cls, tz=None):
        """Return the current test instant in the requested timezone.

        Args:
            tz: Timezone requested by production code. None requests a naive value.

        Returns:
            The configured instant, naive only when no timezone is supplied.
        """
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


def _engine(path: Path):
    return create_async_engine("sqlite+aiosqlite:///" + path.as_posix())


def _expect_utc(value: datetime, expected: datetime) -> None:
    observed = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    assert observed.astimezone(timezone.utc) == expected


async def _seed(path: Path) -> None:
    engine = _engine(path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        async with session.begin():
            session.add(
                CameraEntity(
                    id=CAMERA_ID,
                    tenant_id="tenant-a",
                    site_id="site-a",
                    name="Gate",
                    host="192.0.2.20",
                    rtsp_port=554,
                    main_path="/main",
                    sub_path="/sub",
                    stream_key="gate-fix-016",
                    media_node_id="media-local-01",
                    enabled=True,
                    desired_state="provisioned",
                )
            )
    await engine.dispose()


async def _read(path: Path) -> dict:
    engine = _engine(path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.get(CameraHealthStateEntity, CAMERA_ID)
            assert row is not None
            return {
                "state": row.state,
                "failure_count": row.failure_count,
                "success_count": row.success_count,
                "ready": row.ready,
                "observed_at": row.observed_at,
                "changed_at": row.changed_at,
            }
    finally:
        await engine.dispose()


def _raw_observed_at(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "select observed_at from camera_health_state where camera_id = ?",
            (CAMERA_ID,),
        ).fetchone()
    assert stored is not None
    assert isinstance(stored[0], str)
    return stored[0]


async def _fake_probe(_host: str, _port: int, _timeout_seconds: float) -> bool:
    return _PROBE_READY["value"]


async def _fake_list_paths() -> dict:
    return {"items": []}


async def _run_once(path: Path, instant: datetime, transport_ready: bool, monkeypatch) -> None:
    MovableClock.current = instant
    _PROBE_READY["value"] = transport_ready
    engine = _engine(path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(health_monitor_module, "SessionLocal", factory)
    try:
        await HealthMonitor().run_once()
    finally:
        await engine.dispose()


def test_sqlite_monitor_survives_second_persisted_run_restart_and_heartbeat(tmp_path, monkeypatch):
    """Two file-DB passes, a restart, transitions, and heartbeat expiry must succeed."""
    database = tmp_path / "health-fix-016.db"

    monkeypatch.setattr(health_monitor_module, "datetime", MovableClock)
    monkeypatch.setattr(health_monitor_module, "probe_rtsp_transport", _fake_probe)
    monkeypatch.setattr(health_monitor_module.mediamtx, "list_paths", _fake_list_paths)
    monkeypatch.setattr(health_monitor_module, "stats", MonitorStats())
    monkeypatch.setattr(health_monitor_module.settings, "health_failure_threshold", 2)
    monkeypatch.setattr(health_monitor_module.settings, "health_recovery_threshold", 2)
    monkeypatch.setattr(health_monitor_module.settings, "health_heartbeat_seconds", HEARTBEAT_SECONDS)
    monkeypatch.setattr(health_monitor_module.settings, "event_pipeline_enabled", False)
    monkeypatch.setattr(health_monitor_module.settings, "event_local_store_enabled", False)
    monkeypatch.setattr(health_monitor_module.settings, "placement_execution_enabled", False)

    async def scenario() -> None:
        await _seed(database)
        await _run_once(database, T0, False, monkeypatch)
        first = await _read(database)
        assert first["state"] == "degraded"
        assert first["failure_count"] == 1
        assert first["success_count"] == 0
        assert first["observed_at"].tzinfo is None
        _expect_utc(first["observed_at"], T0)
        _expect_utc(first["changed_at"], T0)

        # Fresh engine and monitor. Unmodified code raises TypeError here.
        restarted_at = T0 + timedelta(seconds=10)
        await _run_once(database, restarted_at, False, monkeypatch)
        second = await _read(database)
        assert second["state"] == "offline"
        assert second["failure_count"] == 2
        assert second["success_count"] == 0
        assert second["observed_at"].tzinfo is None
        _expect_utc(second["observed_at"], restarted_at)
        _expect_utc(second["changed_at"], restarted_at)

        recovering_at = restarted_at + timedelta(seconds=1)
        await _run_once(database, recovering_at, True, monkeypatch)
        third = await _read(database)
        assert third["state"] == "offline"
        assert third["failure_count"] == 0
        assert third["success_count"] == 1
        assert third["ready"] is True
        _expect_utc(third["observed_at"], recovering_at)
        _expect_utc(third["changed_at"], restarted_at)

        online_at = recovering_at + timedelta(seconds=1)
        await _run_once(database, online_at, True, monkeypatch)
        fourth = await _read(database)
        assert fourth["state"] == "online"
        assert fourth["success_count"] == 2
        assert fourth["failure_count"] == 0
        _expect_utc(fourth["observed_at"], online_at)
        _expect_utc(fourth["changed_at"], online_at)
        stored_before_noop = _raw_observed_at(database)

        # Inside the heartbeat window, with no state or diagnostic change, the
        # persisted timestamp format must stay byte-for-byte unchanged.
        quiet_at = online_at + timedelta(seconds=HEARTBEAT_SECONDS - 1)
        await _run_once(database, quiet_at, True, monkeypatch)
        quiet = await _read(database)
        assert quiet["state"] == "online"
        assert quiet["success_count"] == 2
        _expect_utc(quiet["observed_at"], online_at)
        _expect_utc(quiet["changed_at"], online_at)
        assert _raw_observed_at(database) == stored_before_noop

        heartbeat_at = online_at + timedelta(seconds=HEARTBEAT_SECONDS)
        await _run_once(database, heartbeat_at, True, monkeypatch)
        refreshed = await _read(database)
        assert refreshed["state"] == "online"
        assert refreshed["success_count"] == 2
        assert refreshed["observed_at"].tzinfo is None
        _expect_utc(refreshed["observed_at"], heartbeat_at)
        _expect_utc(refreshed["changed_at"], online_at)
        assert _raw_observed_at(database) != stored_before_noop

        # Another fresh engine confirms the heartbeat write survived restart.
        persisted = await _read(database)
        assert persisted["state"] == "online"
        assert persisted["failure_count"] == 0
        assert persisted["success_count"] == 2
        _expect_utc(persisted["observed_at"], heartbeat_at)
        _expect_utc(persisted["changed_at"], online_at)

    asyncio.run(scenario())


def test_aware_utc_timestamp_is_not_shifted_or_double_converted():
    """Aware timestamps stay identical; naive SQLite values are labeled UTC once."""
    from app.services.health_monitor import _as_utc

    aware = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    assert _as_utc(aware) is aware
    assert _as_utc(_as_utc(aware)) is aware

    offset = timezone(timedelta(hours=5, minutes=30))
    aware_offset = datetime(2026, 10, 8, 17, 30, tzinfo=offset)
    assert _as_utc(aware_offset) is aware_offset

    naive = datetime(2026, 10, 8, 12, 0, 0)
    labeled = _as_utc(naive)
    assert labeled == datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    assert labeled.utcoffset() == timedelta(0)
    assert _as_utc(labeled) is labeled
    assert _as_utc(None) is None
