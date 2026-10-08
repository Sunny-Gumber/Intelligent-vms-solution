"""Regression for F13: lease renewal must not wait on the camera scan page.

The camera scan visits only placement_batch_size cameras per run, then the
controller sleeps. When that gap exceeds the lease, healthy owners are fenced
even though their heartbeats are fresh. These tests use a fake clock and no
live cluster. The multi-page case fails on unmodified fbd592f.

Renewal keeps an owner only when the scan would keep that same owner: current
site region, current policy, and current role need. The budget is the
conservative bound, not pages times the interval.
"""

import asyncio
import importlib.util
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, settings
from app.core.effective_authority import effective_authority_active
from app.core.placement_renewal import assert_settings_renewal_budget
from app.db.base import Base
from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.models.placement import (
    InfrastructureNodeEntity,
    PlacementAssignmentEntity,
    PlacementRevocationEntity,
    SiteRegionEntity,
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


def _capacity_all():
    return {
        "max_ingress_mbps": 1000,
        "max_egress_mbps": 1000,
        "max_sources": 1000,
        "max_record_mbps": 1000,
        "max_recordings": 1000,
        "max_ai_mpix_s": 1000,
        "max_ai_jobs": 1000,
    }


def _load_all():
    return {
        "ingress_mbps": 10,
        "egress_mbps": 10,
        "active_sources": 1,
        "record_mbps": 10,
        "active_recordings": 1,
        "ai_mpix_s": 10,
        "active_ai_jobs": 1,
    }


def _node(
    node_id,
    heartbeat_at,
    *,
    authority_mode="central_online",
    region_id=None,
    roles=None,
    capacity=None,
    load=None,
):
    return InfrastructureNodeEntity(
        id=node_id,
        name=node_id,
        region_id=region_id or settings.placement_default_region,
        roles_json=list(roles or ["media"]),
        state="active",
        enabled=True,
        capacity_json=capacity or _capacity(),
        load_json=load or _load(),
        heartbeat_at=heartbeat_at,
        authority_mode=authority_mode,
    )


def _camera(camera_id, *, site_id="site-a"):
    return CameraEntity(
        id=camera_id,
        tenant_id="tenant-a",
        site_id=site_id,
        name=camera_id,
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key=camera_id,
        media_node_id=NODE_ID,
        enabled=True,
        desired_state="provisioned",
    )


def _assignment(
    camera_id,
    node_id,
    lease_expires_at,
    *,
    role="media",
    region_id=None,
    assignment_id=None,
):
    return PlacementAssignmentEntity(
        id=assignment_id or f"pa-{camera_id}",
        camera_id=camera_id,
        role=role,
        region_id=region_id or settings.placement_default_region,
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


def _site_region(site_id, region_id):
    return SiteRegionEntity(
        id=f"sr-{site_id}",
        tenant_id="tenant-a",
        site_id=site_id,
        region_id=region_id,
    )


def _recording_policy(camera_id, *, enabled):
    return RecordingPolicyEntity(
        id=f"rp-{camera_id}",
        camera_id=camera_id,
        enabled=enabled,
        mode="continuous" if enabled else "disabled",
        record_stream_key=f"rec-{camera_id}",
    )


def _configure_budget(
    monkeypatch,
    *,
    lease,
    interval,
    scan_batch,
    renewal_batch,
    max_assignments,
    max_run=0.0,
    missed_lock_cycles=1,
    fence_poll=1.0,
    clock_skew=0.0,
    safety_margin=1.0,
    lock_retry_seconds=0.25,
    lock_retry_limit=4,
):
    """Apply a renewal budget that is valid for this test's lease.

    Allowance defaults are the field minima. A 25s lease cannot carry the
    production fence-poll and skew allowances, and those tests are about the
    scan gap, not the production margin. Tests of the production formula
    construct Settings() directly and leave the allowances at their defaults.
    """
    values = {
        "placement_lease_seconds": lease,
        "placement_batch_size": scan_batch,
        "placement_interval_seconds": interval,
        "placement_renewal_batch_size": renewal_batch,
        "placement_renewal_max_assignments": max_assignments,
        "placement_renewal_max_run_seconds": max_run,
        "placement_renewal_missed_lock_cycles": missed_lock_cycles,
        "placement_renewal_fence_poll_seconds": fence_poll,
        "placement_renewal_clock_skew_seconds": clock_skew,
        "placement_renewal_safety_margin_seconds": safety_margin,
        "placement_renewal_lock_retry_seconds": lock_retry_seconds,
        "placement_renewal_lock_retry_limit": lock_retry_limit,
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

    lease = 60

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=lease,
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
        assert _as_utc(rows["cam-02"].lease_expires_at) == T0 + timedelta(seconds=lease), (
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
        granted = MutableClock.instant + timedelta(seconds=lease)
        assert _as_utc(rows["cam-04"].lease_expires_at) == granted
        assert rows["cam-04"].node_id == NODE_ID
        assert rows["cam-02"].generation == 1
        # The scan visits cam-02 on this run and grants a full lease. It must
        # not be shorter than the lease the renewal page already stored.
        assert _as_utc(rows["cam-02"].lease_expires_at) == granted
        assert _as_utc(rows["cam-02"].lease_expires_at) > T0 + timedelta(seconds=lease)

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


def test_population_over_the_renewal_ceiling_skips_renewal_and_keeps_failover(
    tmp_path, monkeypatch, caplog
):
    """A live population above the ceiling skips renewal and still fails over.

    The static budget is unchanged: Settings() still rejects a configuration
    that cannot cover the ceiling. A live count above that ceiling does not
    raise and does not exit the controller. Independent renewal is skipped.
    The camera scan still moves an owner whose effective authority has ended.
    """

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=60,
            interval=10,
            scan_batch=1,
            renewal_batch=2,
            max_assignments=2,
        )
        factory = await _session_factory(tmp_path, "ceiling.db")
        expired = T0
        short_lease = T0 + timedelta(seconds=ORIGINAL_LEASE_SECONDS)
        deadline = expired + timedelta(seconds=GRACE_SECONDS)
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                _node("node-stale", deadline - timedelta(seconds=31)),
                _camera("cam-01"),
                _camera("cam-02"),
                _camera("cam-03"),
                _assignment("cam-01", "node-stale", expired),
                _assignment("cam-02", NODE_ID, short_lease),
                _assignment("cam-03", NODE_ID, short_lease),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        MutableClock.instant = deadline
        await _touch_heartbeat(factory, NODE_ID, MutableClock.instant)
        caplog.set_level(logging.CRITICAL, logger="app.services.placement")
        result = await placement.run_placement_once()
        assert result["renewal_budget_exceeded"] is True
        assert result["renewed"] == 0
        assert result["scanned"] == 1
        assert result["moved"] == 1
        assert "placement_renewal_budget_exceeded" in caplog.text
        assert "active_assignments=3" in caplog.text
        assert "ceiling=2" in caplog.text
        rows = {row.camera_id: row for row in await _assignments(factory)}
        assert rows["cam-01"].node_id == NODE_ID
        assert rows["cam-01"].generation == 2
        for camera_id in ("cam-02", "cam-03"):
            assert rows[camera_id].node_id == NODE_ID
            assert rows[camera_id].generation == 1
            assert _as_utc(rows[camera_id].lease_expires_at) == short_lease

    asyncio.run(scenario())


def test_population_over_ceiling_does_not_stop_the_controller(monkeypatch, caplog):
    """The controller logs the ceiling and continues into the next sleep."""

    controller = _load_controller()

    async def scenario():
        async def over_ceiling():
            return {
                "scanned": 1,
                "moved": 1,
                "unplaced": 0,
                "deferred_autonomy": 0,
                "renewed": 0,
                "renewal_budget_exceeded": True,
                "cursor": "cam-01",
            }

        async def stop_after_one_cycle(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr(controller, "run_placement_once", over_ceiling)
        monkeypatch.setattr(controller.asyncio, "sleep", stop_after_one_cycle)
        caplog.set_level(logging.CRITICAL, logger="placement-controller")
        try:
            await controller.main()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("controller did not reach the interval sleep")
        assert "placement_renewal_budget_exceeded" in caplog.text

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
            lock_retry_limit=1,
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
    """Defaults keep the 60s lease and cover 7000 cameras times three roles.

    The accepted bound is the conservative formula, including one missed lock,
    the node fence poll, clock skew and a safety margin. pages times the
    interval being inside the lease is not enough.
    """

    assert settings.placement_lease_seconds == 60
    assert settings.placement_renewal_max_assignments >= 7000 * 3
    assert settings.placement_renewal_batch_size >= 1
    assert settings.placement_interval_seconds >= 2
    assert settings.placement_renewal_missed_lock_cycles >= 1
    assert settings.placement_renewal_fence_poll_seconds >= 5
    assert settings.placement_renewal_clock_skew_seconds >= 5
    assert settings.placement_renewal_safety_margin_seconds >= 1
    required = assert_settings_renewal_budget(settings)
    pages = math.ceil(
        settings.placement_renewal_max_assignments / settings.placement_renewal_batch_size
    )
    page_interval = pages * settings.placement_interval_seconds
    assert required < settings.placement_lease_seconds
    assert required > page_interval


def test_page_interval_inside_the_lease_is_rejected():
    """ceil(4/2)*10 = 20 against a 25s lease must fail once poll and a miss count.

    Production allowances are left in place. The old pages*interval check
    accepted this pair. The node drops the lease at its end and only learns
    the replacement on the next fence poll.
    """

    try:
        Settings(
            placement_lease_seconds=25,
            placement_interval_seconds=10,
            placement_renewal_batch_size=2,
            placement_renewal_max_assignments=4,
        )
    except ValidationError as exc:
        message = str(exc)
        lowered = message.lower()
        assert "renewal budget" in lowered, message
        assert "fence_poll" in lowered, message
        assert "page_interval=20s" in message, message
        assert "lease 25s" in message, message
    else:
        raise AssertionError("budget 20 < 25 was accepted")


def test_lock_retry_window_must_stay_under_the_interval():
    """A retry backoff that lasts a full cycle is not a short backoff."""

    try:
        Settings(
            placement_lease_seconds=60,
            placement_interval_seconds=10,
            placement_renewal_batch_size=21000,
            placement_renewal_max_assignments=21000,
            placement_renewal_lock_retry_seconds=6,
            placement_renewal_lock_retry_limit=3,
        )
    except ValidationError as exc:
        assert "lock retry window" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("a 12s lock retry window was accepted against a 10s interval")


def test_moved_site_is_not_renewed_onto_the_old_region(tmp_path, monkeypatch):
    """A healthy owner in the old region is not renewed after the site moves.

    The assignment still records region-a. The camera's site is region-b and a
    region-b node is eligible. Renewal must not extend the region-a lease, or
    the scan can never hand the camera over. After lease plus grace the scan
    moves it.
    """

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=60,
            interval=10,
            scan_batch=10,
            renewal_batch=10,
            max_assignments=10,
        )
        factory = await _session_factory(tmp_path, "region-move.db")
        original_lease = T0 + timedelta(seconds=60)
        await _seed(
            factory,
            [
                _node("node-a", T0, region_id="region-a"),
                _node("node-b", T0, region_id="region-b"),
                _site_region("site-moved", "region-b"),
                _camera("cam-moved", site_id="site-moved"),
                _assignment(
                    "cam-moved",
                    "node-a",
                    original_lease,
                    region_id="region-a",
                ),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)

        MutableClock.instant = T0 + timedelta(seconds=10)
        await _touch_heartbeat(factory, "node-a", MutableClock.instant)
        await _touch_heartbeat(factory, "node-b", MutableClock.instant)
        held = await placement.run_placement_once()
        row = (await _assignments(factory))[0]
        assert held["renewed"] == 0
        assert held["moved"] == 0
        assert held["deferred_autonomy"] >= 1
        assert row.node_id == "node-a"
        assert row.generation == 1
        assert row.region_id == "region-a"
        assert _as_utc(row.lease_expires_at) == original_lease

        deadline = original_lease + timedelta(seconds=GRACE_SECONDS)
        MutableClock.instant = deadline
        await _touch_heartbeat(factory, "node-b", MutableClock.instant)
        handed = await placement.run_placement_once()
        row = (await _assignments(factory))[0]
        assert handed["moved"] == 1
        assert row.node_id == "node-b"
        assert row.region_id == "region-b"
        assert row.generation == 2

    asyncio.run(scenario())


def test_stale_recorded_region_still_renews_the_current_owner(tmp_path, monkeypatch):
    """An owner already in the current site region is renewed off the scan page.

    The assignment still says region-a. The node and the site are region-b.
    Judging eligibility from the stored region leaves this row, and every
    later off-page row, on the short lease.
    """

    async def scenario():
        lease = 60
        _configure_budget(
            monkeypatch,
            lease=lease,
            interval=10,
            scan_batch=1,
            renewal_batch=10,
            max_assignments=10,
        )
        factory = await _session_factory(tmp_path, "region-stale.db")
        original_lease = T0 + timedelta(seconds=ORIGINAL_LEASE_SECONDS)
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                _node("node-b", T0, region_id="region-b"),
                _site_region("site-b", "region-b"),
                _camera("cam-01"),
                _camera("cam-02", site_id="site-b"),
                _assignment("cam-01", NODE_ID, original_lease),
                _assignment(
                    "cam-02",
                    "node-b",
                    original_lease,
                    region_id="region-a",
                ),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        MutableClock.instant = T0
        result = await placement.run_placement_once()
        rows = {row.camera_id: row for row in await _assignments(factory)}
        assert result["scanned"] == 1
        assert result["moved"] == 0
        assert result["renewed"] == 2
        assert rows["cam-02"].node_id == "node-b"
        assert rows["cam-02"].generation == 1
        assert rows["cam-02"].region_id == "region-a"
        assert _as_utc(rows["cam-02"].lease_expires_at) == T0 + timedelta(seconds=lease)

    asyncio.run(scenario())


def test_unneeded_recording_and_ai_leases_are_not_extended(tmp_path, monkeypatch):
    """A disabled recording policy and a missing AI policy are not renewed.

    The camera is off the scan page, so only the independent pass could extend
    those leases. Media, which the scan still requires, is renewed.
    """

    async def scenario():
        lease = 60
        _configure_budget(
            monkeypatch,
            lease=lease,
            interval=10,
            scan_batch=1,
            renewal_batch=10,
            max_assignments=10,
        )
        factory = await _session_factory(tmp_path, "roles.db")
        original_lease = T0 + timedelta(seconds=ORIGINAL_LEASE_SECONDS)
        roles = ("media", "recording", "ai")
        await _seed(
            factory,
            [
                _node(
                    NODE_ID,
                    T0,
                    roles=roles,
                    capacity=_capacity_all(),
                    load=_load_all(),
                ),
                _camera("cam-01"),
                _camera("cam-02"),
                _recording_policy("cam-02", enabled=False),
                _assignment("cam-01", NODE_ID, original_lease),
                *[
                    _assignment(
                        "cam-02",
                        NODE_ID,
                        original_lease,
                        role=role,
                        assignment_id=f"pa-cam-02-{role}",
                    )
                    for role in roles
                ],
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        MutableClock.instant = T0
        result = await placement.run_placement_once()
        rows = {
            (row.camera_id, row.role): row for row in await _assignments(factory)
        }
        assert result["scanned"] == 1
        assert result["moved"] == 0
        assert result["renewed"] == 2
        media = rows[("cam-02", "media")]
        assert media.node_id == NODE_ID
        assert media.generation == 1
        assert _as_utc(media.lease_expires_at) == T0 + timedelta(seconds=lease)
        for role in ("recording", "ai"):
            row = rows[("cam-02", role)]
            assert row.node_id == NODE_ID
            assert row.generation == 1
            assert _as_utc(row.lease_expires_at) == original_lease

    asyncio.run(scenario())


def test_one_missed_lock_renews_before_the_deadline(tmp_path, monkeypatch):
    """One missed lock sleeps the short backoff and still renews in time.

    The old path slept the whole cycle after a miss. That refreshed the grant
    after a lease that expired at T0+8. The retry must stay under the interval
    and the new lease must be taken from the instant after that backoff.
    """

    async def scenario():
        lease = 60
        retry_seconds = 0.25
        _configure_budget(
            monkeypatch,
            lease=lease,
            interval=10,
            scan_batch=10,
            renewal_batch=10,
            max_assignments=10,
            lock_retry_seconds=retry_seconds,
            lock_retry_limit=4,
        )
        factory = await _session_factory(tmp_path, "missed-lock.db")
        deadline = T0 + timedelta(seconds=8)
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                _camera("cam-01"),
                _assignment("cam-01", NODE_ID, deadline),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        attempts = {"count": 0}
        slept = []

        async def miss_once(_session):
            attempts["count"] += 1
            return attempts["count"] > 1

        async def backoff(delay):
            slept.append(delay)
            MutableClock.instant = MutableClock.instant + timedelta(seconds=delay)

        monkeypatch.setattr(placement, "_leader_lock", miss_once)
        monkeypatch.setattr(placement.asyncio, "sleep", backoff)
        MutableClock.instant = T0
        result = await placement.run_placement_once()
        assert attempts["count"] == 2
        assert slept == [retry_seconds]
        assert all(delay < 10 for delay in slept)
        assert MutableClock.instant < deadline
        row = (await _assignments(factory))[0]
        assert result["renewed"] == 1
        assert result["moved"] == 0
        assert row.node_id == NODE_ID
        assert _as_utc(row.lease_expires_at) == MutableClock.instant + timedelta(seconds=lease)

    asyncio.run(scenario())
