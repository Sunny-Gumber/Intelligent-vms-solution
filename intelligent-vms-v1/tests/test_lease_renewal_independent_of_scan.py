"""Regression for F13: lease renewal must not wait on the camera scan page.

The camera scan visits only placement_batch_size cameras per run, then the
controller sleeps. When that gap exceeds the lease, healthy owners are fenced
even though their heartbeats are fresh. These tests use a fake clock and no
live cluster. The multi-page case fails on unmodified fbd592f.
"""

import asyncio
import importlib.util
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, settings
from app.core.effective_authority import effective_authority_active
from app.db.base import Base
from app.models.entities import CameraEntity
from app.models.placement import (
    InfrastructureNodeEntity,
    PlacementAssignmentEntity,
    PlacementRevocationEntity,
)
from app.services import placement


T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
ORIGINAL_LEASE_SECONDS = 5
GRACE_SECONDS = 2
NODE_ID = "node-a"


class MutableClock(datetime):
    """Datetime stand-in whose now() follows an explicit instant."""

    instant = T0

    @classmethod
    def now(cls, tz=None):
        """Return the current test instant.

        Args:
            tz: Timezone requested by production code.

        Returns:
            The configured instant, naive only when no timezone is supplied.
        """
        current = cls.instant
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _capacity():
    return {
        "max_ingress_mbps": 1000,
        "max_egress_mbps": 1000,
        "max_sources": 1000,
    }


def _load():
    return {"ingress_mbps": 10, "egress_mbps": 10, "active_sources": 1}


def _node(node_id, heartbeat_at, *, authority_mode="central_online"):
    return InfrastructureNodeEntity(
        id=node_id,
        name=node_id,
        region_id=settings.placement_default_region,
        roles_json=["media"],
        state="active",
        enabled=True,
        capacity_json=_capacity(),
        load_json=_load(),
        heartbeat_at=heartbeat_at,
        authority_mode=authority_mode,
    )


def _camera(camera_id):
    return CameraEntity(
        id=camera_id,
        tenant_id="tenant-a",
        site_id="site-a",
        name=camera_id,
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key=camera_id,
        media_node_id=NODE_ID,
        enabled=True,
        desired_state="provisioned",
    )


def _assignment(camera_id, node_id, lease_expires_at):
    return PlacementAssignmentEntity(
        id=f"pa-{camera_id}",
        camera_id=camera_id,
        role="media",
        region_id=settings.placement_default_region,
        node_id=node_id,
        cleanup_node_ids_json=[],
        generation=1,
        applied_generation=1,
        active=True,
        reason="initial",
        lease_expires_at=lease_expires_at,
        autonomy_expires_at=None,
        assigned_at=T0,
    )


def _configure_budget(
    monkeypatch,
    *,
    lease,
    interval,
    scan_batch,
    renewal_batch,
    max_assignments,
):
    """Apply the placement budget fields that exist on this Settings object.

    Unmodified main has no renewal-budget fields. Skipping those keeps the
    regression on the camera-scan lease instead of failing inside setattr.
    """
    values = {
        "placement_lease_seconds": lease,
        "placement_batch_size": scan_batch,
        "placement_interval_seconds": interval,
        "placement_renewal_batch_size": renewal_batch,
        "placement_renewal_max_assignments": max_assignments,
    }
    fields = type(settings).model_fields
    for name, value in values.items():
        if name in fields:
            monkeypatch.setattr(settings, name, value)


async def _session_factory(tmp_path, name):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return factory


async def _seed(factory, rows):
    async with factory() as session:
        async with session.begin():
            session.add_all(rows)


async def _touch_heartbeat(factory, node_id, when):
    async with factory() as session:
        async with session.begin():
            node = await session.get(InfrastructureNodeEntity, node_id)
            node.heartbeat_at = when


async def _assignments(factory):
    async with factory() as session:
        rows = (
            await session.execute(
                select(PlacementAssignmentEntity).order_by(PlacementAssignmentEntity.camera_id)
            )
        ).scalars().all()
    return rows


async def _revocations(factory):
    async with factory() as session:
        return (
            await session.execute(select(PlacementRevocationEntity))
        ).scalars().all()


def _load_node_agent():
    module_path = Path(__file__).parents[1] / "services" / "node-agent" / "main.py"
    spec = importlib.util.spec_from_file_location("node_agent_lease_renewal", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_controller():
    module_path = Path(__file__).parents[1] / "services" / "placement-controller" / "main.py"
    spec = importlib.util.spec_from_file_location(
        "placement_controller_lease_renewal",
        module_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_multipage_scan_slower_than_lease_renews_healthy_owners(tmp_path, monkeypatch):
    """Several pages whose scan gap exceeds the lease still renew healthy owners.

    Fencing is enabled. Owners are not moved. The lease is a full term from
    the fake clock, not a shortened term.
    """

    camera_count = 6
    scan_batch = 2
    interval = 10
    lease = 25
    pages = math.ceil(camera_count / scan_batch)
    scan_gap = pages * interval
    assert scan_gap > lease, "the fixture must reproduce a scan slower than the lease"
    assert settings.placement_fence_expiry_grace_seconds == GRACE_SECONDS

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=lease,
            interval=interval,
            scan_batch=scan_batch,
            renewal_batch=camera_count,
            max_assignments=camera_count,
        )
        factory = await _session_factory(tmp_path, "multipage.db")
        original_lease = T0 + timedelta(seconds=ORIGINAL_LEASE_SECONDS)
        cameras = [f"cam-{index:02d}" for index in range(1, camera_count + 1)]
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                *[_camera(camera_id) for camera_id in cameras],
                *[
                    _assignment(camera_id, NODE_ID, original_lease)
                    for camera_id in cameras
                ],
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        node_agent = _load_node_agent()
        monkeypatch.setattr(node_agent, "datetime", MutableClock)
        agent = node_agent.NodeAgent(
            node_agent.NodeAgentSettings.model_validate(
                {
                    "NODE_ID": NODE_ID,
                    "REGION_ID": settings.placement_default_region,
                    "NODE_ROLES": "media",
                    "CONTROL_API_URL": "http://control-api:8000",
                    "NODE_AGENT_TOKEN": "test-node-token",
                    "MEDIAMTX_API_URL": "http://mediamtx:9997",
                    "NODE_FENCING_ENABLED": True,
                    "FENCE_STATE_PATH": str(tmp_path / "fence-state.json"),
                    "FENCE_EXPIRY_GRACE_SECONDS": GRACE_SECONDS,
                }
            )
        )
        assert agent.settings.node_fencing_enabled is True

        async def _fence_delete(_role, _execution_key):
            return True

        monkeypatch.setattr(agent, "_delete_execution_key", _fence_delete)
        try:
            MutableClock.instant = T0
            first = await placement.run_placement_once()
            assert first["scanned"] == scan_batch
            assert first["moved"] == 0
            rows = await _assignments(factory)
            expected = T0 + timedelta(seconds=lease)
            for row in rows:
                assert row.node_id == NODE_ID
                assert row.generation == 1
                assert _as_utc(row.lease_expires_at) == expected, (
                    f"{row.camera_id} was not renewed independently of the scan page: "
                    f"lease={_as_utc(row.lease_expires_at).isoformat()} "
                    f"expected={expected.isoformat()} owner={row.node_id} "
                    f"generation={row.generation} scanned={first['scanned']}"
                )
            assert first["renewed"] == camera_count
            assert await _revocations(factory) == []

            # The original lease is already dead at lease+grace. A fencing-enabled
            # node that has polled the renewed lease must still be executing.
            last = next(row for row in rows if row.camera_id == cameras[-1])
            original_deadline = original_lease + timedelta(seconds=GRACE_SECONDS)
            assert not effective_authority_active(
                original_lease,
                None,
                original_deadline,
                GRACE_SECONDS,
            )
            await agent._apply_fence_snapshot(
                {
                    "server_time": T0.isoformat(),
                    "assignments": [
                        {
                            "assignment_id": last.id,
                            "camera_id": last.camera_id,
                            "role": last.role,
                            "generation": last.generation,
                            "lease_expires_at": _as_utc(last.lease_expires_at).isoformat(),
                            "autonomy_expires_at": None,
                            "execution_key": last.camera_id,
                            "execution_keys": [last.camera_id],
                        }
                    ],
                    "revocations": [],
                }
            )
            MutableClock.instant = original_deadline
            await agent._enforce_cached_expiry()
            cached = agent._fence_state["assignments"][f"{last.camera_id}:media"]
            assert not cached.get("fenced"), (
                f"fencing-enabled node fenced {last.camera_id} "
                f"lease={cached.get('lease_expires_at')} at {original_deadline.isoformat()}"
            )

            # Same instant must not shorten the lease that was just granted.
            # The fencing check above advanced the clock; put it back first.
            MutableClock.instant = T0
            again = await placement.run_placement_once()
            assert again["moved"] == 0
            for row in await _assignments(factory):
                assert row.node_id == NODE_ID
                assert row.generation == 1
                assert _as_utc(row.lease_expires_at) == expected

            for step in range(1, pages + 1):
                MutableClock.instant = T0 + timedelta(seconds=step * interval)
                await _touch_heartbeat(factory, NODE_ID, MutableClock.instant)
                result = await placement.run_placement_once()
                assert result["moved"] == 0
                refreshed = MutableClock.instant + timedelta(seconds=lease)
                for row in await _assignments(factory):
                    assert row.node_id == NODE_ID, row.camera_id
                    assert row.generation == 1, row.camera_id
                    assert _as_utc(row.lease_expires_at) == refreshed, row.camera_id
            assert MutableClock.instant > original_lease
            assert (pages * interval) > lease
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_renewal_is_bounded_to_one_page_per_run(tmp_path, monkeypatch):
    """One run renews a bounded page, including an owner the camera scan skipped."""

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=25,
            interval=10,
            scan_batch=1,
            renewal_batch=2,
            max_assignments=4,
        )
        factory = await _session_factory(tmp_path, "bounded.db")
        original_lease = T0 + timedelta(seconds=ORIGINAL_LEASE_SECONDS)
        cameras = [f"cam-{index:02d}" for index in range(1, 5)]
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                *[_camera(camera_id) for camera_id in cameras],
                *[_assignment(camera_id, NODE_ID, original_lease) for camera_id in cameras],
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        MutableClock.instant = T0
        first = await placement.run_placement_once()
        assert first["scanned"] == 1
        assert first["moved"] == 0
        rows = {row.camera_id: row for row in await _assignments(factory)}
        # cam-02 is outside the single-camera scan page and inside the renewal page.
        assert _as_utc(rows["cam-02"].lease_expires_at) == T0 + timedelta(seconds=25), (
            "owner outside the camera scan page was not renewed"
        )
        assert first["renewed"] == 2
        assert _as_utc(rows["cam-04"].lease_expires_at) == original_lease, (
            "renewal rewrote owners beyond the bounded page"
        )
        for row in rows.values():
            assert row.node_id == NODE_ID
            assert row.generation == 1

        MutableClock.instant = T0 + timedelta(seconds=10)
        await _touch_heartbeat(factory, NODE_ID, MutableClock.instant)
        second = await placement.run_placement_once()
        assert second["moved"] == 0
        assert second["renewed"] == 2
        rows = {row.camera_id: row for row in await _assignments(factory)}
        assert _as_utc(rows["cam-04"].lease_expires_at) == MutableClock.instant + timedelta(seconds=25)
        assert rows["cam-04"].node_id == NODE_ID
        assert rows["cam-02"].generation == 1
        # The scan visits cam-02 on this run and grants a full lease. It must
        # not be shorter than the lease the renewal page already stored.
        assert _as_utc(rows["cam-02"].lease_expires_at) == MutableClock.instant + timedelta(seconds=25)
        assert _as_utc(rows["cam-02"].lease_expires_at) > T0 + timedelta(seconds=25)

    asyncio.run(scenario())


def test_unsatisfiable_renewal_budget_is_rejected_at_startup():
    """7000 assignments, batch 1000 and a 10s interval cannot fit a 60s lease."""

    try:
        Settings(
            placement_lease_seconds=60,
            placement_interval_seconds=10,
            placement_renewal_batch_size=1000,
            placement_renewal_max_assignments=7000,
        )
    except ValidationError as exc:
        message = str(exc).lower()
        assert "renewal budget" in message, message
        assert "70" in message, message
    else:
        raise AssertionError(
            "startup accepted a renewal budget that cannot cover 7000 assignments "
            "before the 60s lease (batch 1000, interval 10s, cycle 70s)"
        )


def test_controller_rejects_unsatisfiable_budget_before_the_loop(monkeypatch):
    """The placement controller must fail closed before sleeping into another scan."""

    controller = _load_controller()
    validator = getattr(controller, "validate_placement_renewal_budget", None)
    assert validator is not None, (
        "placement controller does not reject an unsatisfiable renewal budget before the loop"
    )
    monkeypatch.setattr(settings, "placement_lease_seconds", 60)
    monkeypatch.setattr(settings, "placement_interval_seconds", 10, raising=False)
    monkeypatch.setattr(settings, "placement_renewal_batch_size", 1000, raising=False)
    monkeypatch.setattr(settings, "placement_renewal_max_assignments", 7000, raising=False)
    try:
        validator()
    except Exception as exc:
        assert "renewal budget" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("controller startup validator accepted cycle 70s against a 60s lease")


def test_population_over_the_renewal_ceiling_fails_closed(tmp_path, monkeypatch):
    """A live population above the configured ceiling is an observable budget error."""

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=60,
            interval=10,
            scan_batch=10,
            renewal_batch=2,
            max_assignments=2,
        )
        factory = await _session_factory(tmp_path, "ceiling.db")
        original_lease = T0 + timedelta(seconds=60)
        cameras = ["cam-01", "cam-02", "cam-03"]
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                *[_camera(camera_id) for camera_id in cameras],
                *[_assignment(camera_id, NODE_ID, original_lease) for camera_id in cameras],
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        MutableClock.instant = T0
        try:
            await placement.run_placement_once()
        except Exception as exc:
            message = str(exc).lower()
            assert "renewal budget" in message, message
            assert "exceed" in message, message
        else:
            raise AssertionError(
                "live assignments above placement_renewal_max_assignments did not fail closed"
            )
        for row in await _assignments(factory):
            assert _as_utc(row.lease_expires_at) == original_lease
            assert row.node_id == NODE_ID
            assert row.generation == 1

    asyncio.run(scenario())


def test_stale_or_ineligible_owner_is_not_renewed_and_defers(tmp_path, monkeypatch):
    """Ineligible owners keep their lease and wait for the shared authority deadline."""

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=60,
            interval=10,
            scan_batch=10,
            renewal_batch=10,
            max_assignments=10,
        )
        assert settings.placement_offline_autonomy_seconds == 0
        assert settings.placement_fence_expiry_grace_seconds == GRACE_SECONDS
        factory = await _session_factory(tmp_path, "stale.db")
        original_lease = T0 + timedelta(seconds=60)
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                _node("node-stale", T0 - timedelta(seconds=31)),
                _node("node-degraded", T0, authority_mode="fenced_degraded"),
                _camera("cam-healthy"),
                _camera("cam-stale"),
                _camera("cam-degraded"),
                _assignment("cam-healthy", NODE_ID, original_lease),
                _assignment("cam-stale", "node-stale", original_lease),
                _assignment("cam-degraded", "node-degraded", original_lease),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)

        MutableClock.instant = T0 + timedelta(seconds=10)
        await _touch_heartbeat(factory, NODE_ID, MutableClock.instant)
        early = await placement.run_placement_once()
        rows = {row.camera_id: row for row in await _assignments(factory)}
        assert rows["cam-healthy"].node_id == NODE_ID
        assert rows["cam-healthy"].generation == 1
        assert _as_utc(rows["cam-healthy"].lease_expires_at) == (
            MutableClock.instant + timedelta(seconds=60)
        )
        assert rows["cam-stale"].node_id == "node-stale"
        assert rows["cam-stale"].generation == 1
        assert _as_utc(rows["cam-stale"].lease_expires_at) == original_lease
        assert rows["cam-degraded"].node_id == "node-degraded"
        assert rows["cam-degraded"].generation == 1
        assert _as_utc(rows["cam-degraded"].lease_expires_at) == original_lease
        assert early["deferred_autonomy"] == 2
        assert early["moved"] == 0
        assert await _revocations(factory) == []

        deadline = original_lease + timedelta(seconds=GRACE_SECONDS)
        MutableClock.instant = deadline
        await _touch_heartbeat(factory, NODE_ID, MutableClock.instant)
        handed = await placement.run_placement_once()
        rows = {row.camera_id: row for row in await _assignments(factory)}
        assert rows["cam-healthy"].node_id == NODE_ID
        assert rows["cam-healthy"].generation == 1
        assert rows["cam-stale"].node_id == NODE_ID
        assert rows["cam-stale"].generation == 2
        assert rows["cam-degraded"].node_id == NODE_ID
        assert rows["cam-degraded"].generation == 2
        assert handed["moved"] == 2
        revocations = await _revocations(factory)
        assert len(revocations) == 2
        for revocation in revocations:
            assert _as_utc(revocation.valid_until) == deadline

    asyncio.run(scenario())


def test_renewal_does_not_run_without_the_execution_lock(tmp_path, monkeypatch):
    """A missed placement execution lock must not renew or move owners."""

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=25,
            interval=10,
            scan_batch=1,
            renewal_batch=10,
            max_assignments=10,
        )
        factory = await _session_factory(tmp_path, "lock.db")
        original_lease = T0 + timedelta(seconds=ORIGINAL_LEASE_SECONDS)
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                _camera("cam-01"),
                _assignment("cam-01", NODE_ID, original_lease),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)

        async def not_leader(_session):
            return False

        monkeypatch.setattr(placement, "_leader_lock", not_leader)
        MutableClock.instant = T0
        result = await placement.run_placement_once()
        assert result["scanned"] == 0
        assert result["moved"] == 0
        assert result.get("renewed", 0) == 0
        row = (await _assignments(factory))[0]
        assert row.node_id == NODE_ID
        assert _as_utc(row.lease_expires_at) == original_lease

    asyncio.run(scenario())


def test_default_budget_covers_reviewed_fleet_without_shortening_lease():
    """Defaults keep the 60s lease and can visit 7000 cameras times three roles."""

    assert settings.placement_lease_seconds == 60
    ceiling = getattr(settings, "placement_renewal_max_assignments", None)
    batch = getattr(settings, "placement_renewal_batch_size", None)
    interval = getattr(settings, "placement_interval_seconds", None)
    assert ceiling is not None and batch is not None and interval is not None, (
        "renewal budget settings are missing; lease renewal is still tied to the camera scan"
    )
    assert ceiling >= 7000 * 3
    assert batch >= 1
    assert interval >= 2
    cycle = math.ceil(ceiling / batch) * interval
    assert cycle < settings.placement_lease_seconds, (
        f"default renewal cycle {cycle}s is not strictly inside the 60s lease"
    )
