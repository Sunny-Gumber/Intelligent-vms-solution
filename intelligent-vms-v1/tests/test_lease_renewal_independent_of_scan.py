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
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import Settings, settings
from app.core.effective_authority import effective_authority_active
from app.core.placement_renewal import (
    PlacementRenewalBudgetError,
    assert_placement_renewal_budget,
    assert_settings_renewal_budget,
)
from app.db.base import Base
from app.models.entities import CameraAIPolicyEntity, CameraEntity, RecordingPolicyEntity, ServiceStateEntity
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
    # One PostgreSQL sample of that page was inside 2s and too close to use 2s
    # as the deadline. The default deadline stays above that sample.
    assert settings.placement_renewal_max_run_seconds >= 4
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


def test_page_longer_than_the_lease_timeline_is_rejected():
    """QA-014-302. A max run of the slow page is not silently inside the lease.

    Charging that run on the grant and again on the revisit, with the default
    allowances, is 77.904s. Startup rejects it. The lease stays 60s.
    """

    try:
        Settings(
            placement_lease_seconds=60,
            placement_interval_seconds=10,
            placement_renewal_batch_size=21000,
            placement_renewal_max_assignments=21000,
            placement_renewal_max_run_seconds=20.702,
        )
    except ValidationError as exc:
        message = str(exc)
        assert "renewal budget" in message.lower(), message
        assert ">= lease 60s" in message, message
    else:
        raise AssertionError("a 20.702s page was accepted against a 60s lease")


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


def _repro_budget_kwargs():
    """The QA-014-101 inputs. Charging retries on every cycle rejects them."""
    return {
        "placement_lease_seconds": 60,
        "placement_interval_seconds": 10,
        "placement_renewal_batch_size": 1,
        "placement_renewal_max_assignments": 3,
        "placement_renewal_max_run_seconds": 2,
        "placement_renewal_missed_lock_cycles": 1,
        "placement_renewal_fence_poll_seconds": 5,
        "placement_renewal_clock_skew_seconds": 5,
        "placement_renewal_safety_margin_seconds": 1,
        "placement_renewal_lock_retry_seconds": 3,
        "placement_renewal_lock_retry_limit": 4,
    }


def test_retry_sleeps_are_inside_the_renewal_budget():
    """Retry sleeps on every cycle, not only on a full miss, stay inside the lease.

    The QA-014-101 inputs are 3 pages, a 2s max run, a 9s retry sleep, and 11s
    of allowances. Charging that retry sleep on each successful page, and
    charging the revisit's own max run, makes the required budget 95s.
    Required equal to the lease stays rejected. The stock defaults stay inside
    60s when their page really finishes inside the configured max run.
    """

    try:
        Settings(**_repro_budget_kwargs())
    except ValidationError as exc:
        message = str(exc)
        assert "renewal budget" in message.lower(), message
        assert "retry_sleep=9s" in message, message
        assert ">= lease 60s" in message, message
    else:
        raise AssertionError("budget 59s plus 9s of retry sleep was accepted against a 60s lease")

    try:
        assert_placement_renewal_budget(
            max_assignments=3,
            batch_size=1,
            interval_seconds=10,
            max_run_seconds=2,
            missed_lock_cycles=1,
            fence_poll_seconds=5,
            clock_skew_seconds=5,
            safety_margin_seconds=1,
            lock_retry_seconds=3,
            lock_retry_limit=4,
            lease_seconds=95,
        )
    except PlacementRenewalBudgetError as exc:
        assert "required 95s >= lease 95s" in str(exc), str(exc)
    else:
        raise AssertionError("required == lease was accepted")

    assert assert_settings_renewal_budget(settings) < settings.placement_lease_seconds


def test_site_move_between_reads_does_not_extend_the_old_owner(tmp_path, monkeypatch):
    """A site move that lands after the renewal read and before the scan read.

    Renewal sees region-a and would extend node-a. The scan sees region-b and
    would not keep node-a. The lease must stay put so the scan can defer on
    the old lease instead of a fresh 60s grant.
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
        factory = await _session_factory(tmp_path, "split-region.db")
        original_lease = T0 + timedelta(seconds=50)
        await _seed(
            factory,
            [
                _node("node-a", T0, region_id="region-a"),
                _node("node-b", T0, region_id="region-b"),
                _camera("cam-01"),
                _camera("cam-02"),
                _assignment("cam-01", "node-a", original_lease, region_id="region-a"),
                _assignment("cam-02", "node-a", original_lease, region_id="region-a"),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        calls = {"n": 0}

        def site_region(_site_regions, _camera):
            calls["n"] += 1
            if calls["n"] <= 2:
                return "region-a"
            return "region-b"

        monkeypatch.setattr(placement, "_site_region_id", site_region)
        MutableClock.instant = T0 + timedelta(seconds=10)
        await _touch_heartbeat(factory, "node-a", MutableClock.instant)
        await _touch_heartbeat(factory, "node-b", MutableClock.instant)
        result = await placement.run_placement_once()
        assert calls["n"] >= 4, calls["n"]
        assert result["renewed"] == 0
        assert result["moved"] == 0
        assert result["deferred_autonomy"] == 2
        for row in await _assignments(factory):
            assert row.node_id == "node-a"
            assert row.generation == 1
            assert row.region_id == "region-a"
            assert _as_utc(row.lease_expires_at) == original_lease

    asyncio.run(scenario())


def test_stale_flush_does_not_extend_the_successor(tmp_path, monkeypatch):
    """A lease write observed for generation 1 must not land on generation 2.

    The loaded row is failed over to node-b before the renewal UPDATE. The
    successor keeps the lease it already had, not now+60 from the stale owner.
    """

    async def scenario():
        lease = 60
        _configure_budget(
            monkeypatch,
            lease=lease,
            interval=10,
            scan_batch=10,
            renewal_batch=10,
            max_assignments=10,
        )
        factory = await _session_factory(tmp_path, "successor.db")
        successor_lease = T0 + timedelta(seconds=60)
        await _seed(
            factory,
            [
                _node("node-a", T0),
                _node(
                    "node-b",
                    T0 - timedelta(seconds=31),
                    region_id="region-b",
                ),
                _camera("cam-successor"),
                _assignment(
                    "cam-successor",
                    "node-a",
                    T0 + timedelta(seconds=40),
                ),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        real_owner = placement._owner_to_keep

        def failover_under_renewal(nodes, existing, role, region_id, now):
            kept = real_owner(nodes, existing, role, region_id, now)
            if (
                existing is not None
                and existing.camera_id == "cam-successor"
                and existing.generation == 1
            ):
                existing.node_id = "node-b"
                existing.generation = 2
                existing.lease_expires_at = successor_lease
            return kept

        monkeypatch.setattr(placement, "_owner_to_keep", failover_under_renewal)
        MutableClock.instant = T0 + timedelta(seconds=30)
        await _touch_heartbeat(factory, "node-a", MutableClock.instant)
        result = await placement.run_placement_once()
        row = (await _assignments(factory))[0]
        assert result["moved"] == 0
        assert row.node_id == "node-b"
        assert row.generation == 2
        assert _as_utc(row.lease_expires_at) == successor_lease
        assert _as_utc(row.lease_expires_at) != MutableClock.instant + timedelta(seconds=lease)

    asyncio.run(scenario())


def test_extend_where_generation_does_not_match_leaves_the_successor(tmp_path, monkeypatch):
    """The UPDATE predicate misses a row that already belongs to the next owner."""

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=60,
            interval=10,
            scan_batch=10,
            renewal_batch=10,
            max_assignments=10,
        )
        factory = await _session_factory(tmp_path, "predicate.db")
        successor_lease = T0 + timedelta(seconds=60)
        await _seed(
            factory,
            [
                _node("node-a", T0),
                _node("node-b", T0),
                _camera("cam-01"),
                _assignment("cam-01", "node-a", T0 + timedelta(seconds=40)),
            ],
        )
        async with factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(PlacementAssignmentEntity).where(
                            PlacementAssignmentEntity.camera_id == "cam-01"
                        )
                    )
                ).scalar_one()
                row.node_id = "node-b"
                row.generation = 2
                row.lease_expires_at = successor_lease
                await session.flush()
                set_committed_value(row, "node_id", "node-a")
                set_committed_value(row, "generation", 1)
                set_committed_value(row, "lease_expires_at", T0 + timedelta(seconds=40))
                extended = await placement._extend_owner_lease(
                    session,
                    row,
                    T0 + timedelta(seconds=30),
                    node_id="node-a",
                    generation=1,
                )
                assert extended is False
        row = (await _assignments(factory))[0]
        assert row.node_id == "node-b"
        assert row.generation == 2
        assert _as_utc(row.lease_expires_at) == successor_lease

    asyncio.run(scenario())


def test_recording_disabled_between_reads_is_not_renewed(tmp_path, monkeypatch):
    """Recording disabled at the scan's policy read is not given a new lease.

    The renewal read still sees the policy as required. Media, which the scan
    still requires, is renewed. A run that starts with recording already
    disabled is covered by the existing unneeded-role test.
    """

    async def scenario():
        lease = 60
        _configure_budget(
            monkeypatch,
            lease=lease,
            interval=10,
            scan_batch=10,
            renewal_batch=10,
            max_assignments=10,
        )
        factory = await _session_factory(tmp_path, "recording-split.db")
        original_lease = T0 + timedelta(seconds=ORIGINAL_LEASE_SECONDS)
        await _seed(
            factory,
            [
                _node(
                    NODE_ID,
                    T0,
                    roles=("media", "recording"),
                    capacity=_capacity_all(),
                    load=_load_all(),
                ),
                _camera("cam-01"),
                _recording_policy("cam-01", enabled=True),
                _assignment("cam-01", NODE_ID, original_lease, assignment_id="pa-cam-01-media"),
                _assignment(
                    "cam-01",
                    NODE_ID,
                    original_lease,
                    role="recording",
                    assignment_id="pa-cam-01-recording",
                ),
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        real_role = placement._role_required
        recording_checks = {"n": 0}

        def role_required(role, recording_policy, ai_policy):
            if role != "recording":
                return real_role(role, recording_policy, ai_policy)
            recording_checks["n"] += 1
            if recording_checks["n"] == 1:
                return True
            return False

        monkeypatch.setattr(placement, "_role_required", role_required)
        MutableClock.instant = T0
        result = await placement.run_placement_once()
        rows = {(row.camera_id, row.role): row for row in await _assignments(factory)}
        assert recording_checks["n"] >= 2
        assert result["renewed"] == 1
        assert result["moved"] == 0
        media = rows[("cam-01", "media")]
        recording = rows[("cam-01", "recording")]
        assert _as_utc(media.lease_expires_at) == T0 + timedelta(seconds=lease)
        assert media.generation == 1
        assert _as_utc(recording.lease_expires_at) == original_lease
        assert recording.generation == 1
        assert recording.node_id == NODE_ID

    asyncio.run(scenario())


def test_exhausted_lock_attempts_are_logged(tmp_path, monkeypatch, caplog):
    """Three cycles that never take the lock are visible and leave the lease."""

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=60,
            interval=10,
            scan_batch=10,
            renewal_batch=10,
            max_assignments=10,
            lock_retry_limit=4,
        )
        factory = await _session_factory(tmp_path, "lock-log.db")
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
        attempts = {"n": 0}

        async def never(_session):
            attempts["n"] += 1
            return False

        async def no_sleep(_delay):
            return None

        monkeypatch.setattr(placement, "_leader_lock", never)
        monkeypatch.setattr(placement.asyncio, "sleep", no_sleep)
        caplog.set_level(logging.CRITICAL, logger="app.services.placement")
        MutableClock.instant = T0
        for _ in range(3):
            result = await placement.run_placement_once()
            assert result["renewed"] == 0
            assert result["scanned"] == 0
            assert result["moved"] == 0
            assert result["renewal_lock_not_acquired"] is True
        assert attempts["n"] == 12
        assert caplog.text.count("placement_renewal_lock_not_acquired") == 3
        assert "attempts=4" in caplog.text
        row = (await _assignments(factory))[0]
        assert row.node_id == NODE_ID
        assert _as_utc(row.lease_expires_at) == original_lease

    asyncio.run(scenario())


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _authority_database_urls():
    """SQLite always. PostgreSQL when a local server is accepting connections.

    The hosted unit job has no PostgreSQL service. This is not a skip of a
    failing assertion: the SQLite case is collected everywhere, and the
    PostgreSQL case is collected when the server is up.
    """
    urls = ["sqlite"]
    configured = os.environ.get("VMS_PLACEMENT_AUTHORITY_TEST_DATABASE_URL")
    if configured:
        urls.append(configured)
        return urls
    if _tcp_open("127.0.0.1", 5432):
        urls.append("postgresql+asyncpg://vms:vms@127.0.0.1:5432/vms_fix_014")
    return urls


async def _open_database(tmp_path, name, url):
    if url == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    else:
        engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    return factory, engine


def _cursor(key, value):
    return ServiceStateEntity(key=key, value_json=value)


def _qa_201_budget_kwargs():
    """The configuration QA-014-201 showed startup accepting at required 56s."""
    return {
        "placement_lease_seconds": 60,
        "placement_interval_seconds": 5,
        "placement_renewal_batch_size": 1,
        "placement_renewal_max_assignments": 9,
        "placement_renewal_max_run_seconds": 0,
        "placement_renewal_missed_lock_cycles": 1,
        "placement_renewal_fence_poll_seconds": 1,
        "placement_renewal_clock_skew_seconds": 0,
        "placement_renewal_safety_margin_seconds": 1,
        "placement_renewal_lock_retry_seconds": 2,
        "placement_renewal_lock_retry_limit": 3,
    }


def test_successful_lock_retries_are_inside_the_renewal_budget(tmp_path, monkeypatch):
    """QA-014-201. Retries before a successful acquire have to fit in the lease.

    Startup must reject lease 60, interval 5, batch 1, ceiling 9, max run 0,
    one reserved miss, fence 1, skew 0, margin 1, retry 2s and 3 attempts.
    The same numbers on the controller loop, with the first cycle acquiring
    immediately and every later cycle sleeping 4s before it acquires, commit
    cam-01's replacement at 12:01:21.
    """

    try:
        Settings(**_qa_201_budget_kwargs())
    except ValidationError as exc:
        message = str(exc)
        assert "max run must be > 0" in message, message
    else:
        raise AssertionError("max run 0 was accepted as configuration")
    try:
        assert_placement_renewal_budget(
            max_assignments=9,
            batch_size=1,
            interval_seconds=5,
            max_run_seconds=0,
            missed_lock_cycles=1,
            fence_poll_seconds=1,
            clock_skew_seconds=0,
            safety_margin_seconds=1,
            lock_retry_seconds=2,
            lock_retry_limit=3,
            lease_seconds=60,
        )
    except PlacementRenewalBudgetError as exc:
        message = str(exc)
        assert "renewal budget" in message.lower(), message
        assert "required 92s >= lease 60s" in message, message
        assert "retry_sleep=4s" in message, message
    else:
        raise AssertionError("budget 56s was accepted; successful cycles can spend 4s of retries")

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=60,
            interval=5,
            scan_batch=1,
            renewal_batch=1,
            max_assignments=9,
            max_run=0,
            lock_retry_seconds=2,
            lock_retry_limit=3,
        )
        monkeypatch.setattr(placement, "assert_settings_renewal_budget", lambda _values: 0.0)
        factory = await _session_factory(tmp_path, "retry-timeline.db")
        cameras = [f"cam-{index:02d}" for index in range(1, 10)]
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                *[_camera(camera_id) for camera_id in cameras],
                *[_assignment(camera_id, NODE_ID, T0 + timedelta(seconds=50)) for camera_id in cameras],
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        controller = _load_controller()
        calls = {"n": 0}

        async def lock(_session):
            calls["n"] += 1
            number = calls["n"]
            acquired = number == 1 or (number - 2) % 3 == 2
            if acquired:
                async with factory() as heartbeat_session:
                    async with heartbeat_session.begin():
                        node = await heartbeat_session.get(InfrastructureNodeEntity, NODE_ID)
                        node.heartbeat_at = MutableClock.instant
            return acquired

        async def advance(delay):
            MutableClock.instant = MutableClock.instant + timedelta(seconds=delay)

        monkeypatch.setattr(placement, "_leader_lock", lock)
        monkeypatch.setattr(placement.asyncio, "sleep", advance)
        monkeypatch.setattr(controller.asyncio, "sleep", advance)
        MutableClock.instant = T0
        stamps = []
        for index in range(10):
            await placement.run_placement_once()
            stamps.append(MutableClock.instant)
            if index == 0:
                first = {row.camera_id: row for row in await _assignments(factory)}["cam-01"]
                assert _as_utc(first.lease_expires_at) == T0 + timedelta(seconds=60)
                assert first.node_id == NODE_ID
                assert first.generation == 1
            if index < 9:
                await controller.asyncio.sleep(
                    controller.resolved_placement_interval_seconds(settings.placement_interval_seconds)
                )
        assert stamps[0] == T0
        assert stamps[7] == T0 + timedelta(seconds=63)
        assert stamps[9] == T0 + timedelta(seconds=81)
        commit_at = stamps[9]
        assert commit_at > T0 + timedelta(seconds=62)
        assert not effective_authority_active(T0 + timedelta(seconds=60), None, commit_at, 0)
        assert not effective_authority_active(
            T0 + timedelta(seconds=60),
            None,
            commit_at,
            GRACE_SECONDS,
        )
        renewed = {row.camera_id: row for row in await _assignments(factory)}["cam-01"]
        assert renewed.node_id == NODE_ID
        assert renewed.generation == 1
        assert _as_utc(renewed.lease_expires_at) == commit_at + timedelta(seconds=60)

    asyncio.run(scenario())


async def _move_site_after_renewal_read(factory):
    async def move():
        async with factory() as other:
            async with other.begin():
                region = (
                    await other.execute(
                        select(SiteRegionEntity).where(SiteRegionEntity.site_id == "site-a")
                    )
                ).scalar_one()
                region.region_id = "region-b"

    return move


@pytest.mark.parametrize("database_url", _authority_database_urls())
def test_off_page_site_move_committed_by_a_second_session_is_not_renewed(
    tmp_path, monkeypatch, database_url
):
    """QA-014-202. A site move committed after the renewal read must not extend.

    Scan batch 1 and the camera cursor already at cam-a, so this run scans
    cam-b only. A second session commits region-b. cam-a is not on that page
    and must keep the lease it had.
    """

    async def scenario():
        factory, engine = await _open_database(tmp_path, "off-page-region.db", database_url)
        try:
            _configure_budget(
                monkeypatch,
                lease=60,
                interval=2,
                scan_batch=1,
                renewal_batch=1,
                max_assignments=10,
                lock_retry_limit=1,
            )
            original_lease = T0 + timedelta(seconds=50)
            await _seed(
                factory,
                [
                    _node("node-a", T0, region_id="region-a"),
                    _node("node-b", T0, region_id="region-b"),
                    _site_region("site-a", "region-a"),
                    _camera("cam-a"),
                    _camera("cam-b"),
                    _assignment(
                        "cam-a",
                        "node-a",
                        original_lease,
                        region_id="region-a",
                        assignment_id="pa-cam-a",
                    ),
                    _assignment(
                        "cam-b",
                        "node-a",
                        original_lease,
                        region_id="region-a",
                        assignment_id="pa-cam-b",
                    ),
                    _cursor(placement.CURSOR_KEY, {"camera_id": "cam-a"}),
                    _cursor(placement.RENEWAL_CURSOR_KEY, {}),
                ],
            )
            monkeypatch.setattr(placement, "SessionLocal", factory)
            monkeypatch.setattr(placement, "datetime", MutableClock)
            real_collect = placement._collect_renewal_candidates
            move_site = await _move_site_after_renewal_read(factory)

            async def collecting(*args, **kwargs):
                result = await real_collect(*args, **kwargs)
                await move_site()
                return result

            monkeypatch.setattr(placement, "_collect_renewal_candidates", collecting)
            MutableClock.instant = T0 + timedelta(seconds=10)
            await _touch_heartbeat(factory, "node-a", MutableClock.instant)
            await _touch_heartbeat(factory, "node-b", MutableClock.instant)
            result = await placement.run_placement_once()
            rows = {row.camera_id: row for row in await _assignments(factory)}
            assert result["renewed"] == 0, result
            assert result["moved"] == 0, result
            assert result["deferred_autonomy"] == 1, result
            assert result["scanned"] == 1, result
            for camera_id in ("cam-a", "cam-b"):
                assert rows[camera_id].node_id == "node-a"
                assert rows[camera_id].generation == 1
                assert rows[camera_id].region_id == "region-a"
                assert _as_utc(rows[camera_id].lease_expires_at) == original_lease

            monkeypatch.setattr(settings, "placement_batch_size", 10)
            monkeypatch.setattr(settings, "placement_renewal_batch_size", 10)
            monkeypatch.setattr(placement, "_collect_renewal_candidates", real_collect)
            follow = await placement.run_placement_once()
            rows = {row.camera_id: row for row in await _assignments(factory)}
            assert follow["renewed"] == 0, follow
            assert follow["moved"] == 0, follow
            assert follow["deferred_autonomy"] == 2, follow
            assert _as_utc(rows["cam-a"].lease_expires_at) == original_lease
            assert rows["cam-a"].node_id == "node-a"
            assert rows["cam-a"].generation == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


async def _disable_policies(factory, camera_ids):
    async def disable():
        async with factory() as other:
            async with other.begin():
                policies = (
                    await other.execute(
                        select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id.in_(camera_ids))
                    )
                ).scalars().all()
                for policy in policies:
                    policy.enabled = False
                ai_rows = (
                    await other.execute(
                        select(CameraAIPolicyEntity).where(CameraAIPolicyEntity.camera_id.in_(camera_ids))
                    )
                ).scalars().all()
                for policy in ai_rows:
                    policy.enabled = False

    return disable


@pytest.mark.parametrize("database_url", _authority_database_urls())
def test_recording_disabled_by_a_second_session_is_not_renewed(tmp_path, monkeypatch, database_url):
    """QA-014-203. A committed enabled=false is not renewed from the identity map.

    The disabling row is committed by a second session. This does not patch
    _role_required. Media, which stays required, is still renewed.
    """

    async def scenario():
        factory, engine = await _open_database(tmp_path, "recording-commit.db", database_url)
        try:
            lease = 60
            _configure_budget(
                monkeypatch,
                lease=lease,
                interval=2,
                scan_batch=10,
                renewal_batch=10,
                max_assignments=10,
                lock_retry_limit=1,
            )
            original_lease = T0 + timedelta(seconds=50)
            await _seed(
                factory,
                [
                    _node(
                        "node-a",
                        T0,
                        roles=("media", "recording"),
                        capacity=_capacity_all(),
                        load=_load_all(),
                    ),
                    _camera("cam-a"),
                    _camera("cam-b"),
                    _recording_policy("cam-a", enabled=True),
                    _recording_policy("cam-b", enabled=True),
                    _assignment("cam-a", "node-a", original_lease, assignment_id="pa-cam-a-media"),
                    _assignment(
                        "cam-a",
                        "node-a",
                        original_lease,
                        role="recording",
                        assignment_id="pa-cam-a-recording",
                    ),
                    _assignment("cam-b", "node-a", original_lease, assignment_id="pa-cam-b-media"),
                    _assignment(
                        "cam-b",
                        "node-a",
                        original_lease,
                        role="recording",
                        assignment_id="pa-cam-b-recording",
                    ),
                    _cursor(placement.CURSOR_KEY, {}),
                    _cursor(placement.RENEWAL_CURSOR_KEY, {}),
                ],
            )
            monkeypatch.setattr(placement, "SessionLocal", factory)
            monkeypatch.setattr(placement, "datetime", MutableClock)
            real_collect = placement._collect_renewal_candidates
            disable = await _disable_policies(factory, ["cam-a", "cam-b"])

            async def collecting(*args, **kwargs):
                result = await real_collect(*args, **kwargs)
                await disable()
                return result

            monkeypatch.setattr(placement, "_collect_renewal_candidates", collecting)
            MutableClock.instant = T0 + timedelta(seconds=10)
            await _touch_heartbeat(factory, "node-a", MutableClock.instant)
            result = await placement.run_placement_once()
            rows = {(row.camera_id, row.role): row for row in await _assignments(factory)}
            assert result["renewed"] == 2, result
            assert result["moved"] == 0, result
            fresh_lease = MutableClock.instant + timedelta(seconds=lease)
            for camera_id in ("cam-a", "cam-b"):
                media = rows[(camera_id, "media")]
                recording = rows[(camera_id, "recording")]
                assert _as_utc(media.lease_expires_at) == fresh_lease
                assert media.generation == 1
                assert _as_utc(recording.lease_expires_at) == original_lease
                assert recording.generation == 1
                assert recording.node_id == "node-a"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("database_url", _authority_database_urls())
def test_off_page_recording_and_ai_disabled_by_a_second_session_are_not_renewed(
    tmp_path, monkeypatch, database_url
):
    """A policy commit for a camera the scan page did not load is not renewed."""

    async def scenario():
        factory, engine = await _open_database(tmp_path, "off-page-policy.db", database_url)
        try:
            lease = 60
            _configure_budget(
                monkeypatch,
                lease=lease,
                interval=2,
                scan_batch=1,
                renewal_batch=3,
                max_assignments=10,
                lock_retry_limit=1,
            )
            original_lease = T0 + timedelta(seconds=50)
            await _seed(
                factory,
                [
                    _node(
                        "node-a",
                        T0,
                        roles=("media", "recording", "ai"),
                        capacity=_capacity_all(),
                        load=_load_all(),
                    ),
                    _camera("cam-a"),
                    _camera("cam-b"),
                    _recording_policy("cam-a", enabled=True),
                    _assignment("cam-a", "node-a", original_lease, assignment_id="pa-cam-a-media"),
                    _assignment(
                        "cam-a",
                        "node-a",
                        original_lease,
                        role="recording",
                        assignment_id="pa-cam-a-recording",
                    ),
                    _assignment(
                        "cam-a",
                        "node-a",
                        original_lease,
                        role="ai",
                        assignment_id="pa-cam-a-ai",
                    ),
                    _assignment("cam-b", "node-a", original_lease, assignment_id="pa-cam-b-media"),
                    _cursor(placement.CURSOR_KEY, {"camera_id": "cam-a"}),
                    _cursor(placement.RENEWAL_CURSOR_KEY, {}),
                ],
            )
            await _seed(factory, [CameraAIPolicyEntity(id="ai-cam-a", camera_id="cam-a", enabled=True)])
            monkeypatch.setattr(placement, "SessionLocal", factory)
            monkeypatch.setattr(placement, "datetime", MutableClock)
            real_collect = placement._collect_renewal_candidates
            disable = await _disable_policies(factory, ["cam-a"])

            async def collecting(*args, **kwargs):
                result = await real_collect(*args, **kwargs)
                await disable()
                return result

            monkeypatch.setattr(placement, "_collect_renewal_candidates", collecting)
            MutableClock.instant = T0 + timedelta(seconds=10)
            await _touch_heartbeat(factory, "node-a", MutableClock.instant)
            result = await placement.run_placement_once()
            rows = {(row.camera_id, row.role): row for row in await _assignments(factory)}
            fresh_lease = MutableClock.instant + timedelta(seconds=lease)
            assert result["moved"] == 0, result
            assert _as_utc(rows[("cam-a", "media")].lease_expires_at) == fresh_lease
            assert _as_utc(rows[("cam-a", "recording")].lease_expires_at) == original_lease
            assert _as_utc(rows[("cam-a", "ai")].lease_expires_at) == original_lease
            assert rows[("cam-a", "recording")].generation == 1
            assert rows[("cam-a", "ai")].generation == 1
            assert rows[("cam-a", "recording")].node_id == "node-a"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("database_url", _authority_database_urls())
def test_failover_does_not_overwrite_a_newer_generation(tmp_path, monkeypatch, caplog, database_url):
    """QA-014-204. Failover must not replace a generation another session committed.

    cam-z is expired on node-a. node-b is the eligible successor. Before the
    failover write, a second session commits node-c at generation 9.
    """

    async def scenario():
        factory, engine = await _open_database(tmp_path, "failover-generation.db", database_url)
        try:
            _configure_budget(
                monkeypatch,
                lease=60,
                interval=2,
                scan_batch=10,
                renewal_batch=10,
                max_assignments=10,
                lock_retry_limit=1,
            )
            successor_lease = T0 + timedelta(seconds=90)
            await _seed(
                factory,
                [
                    _node("node-a", T0 - timedelta(seconds=40)),
                    _node("node-b", T0),
                    _node("node-c", T0 - timedelta(seconds=40)),
                    _camera("cam-z"),
                    _assignment("cam-z", "node-a", T0, assignment_id="pa-cam-z"),
                    _cursor(placement.CURSOR_KEY, {}),
                    _cursor(placement.RENEWAL_CURSOR_KEY, {}),
                ],
            )
            monkeypatch.setattr(placement, "SessionLocal", factory)
            monkeypatch.setattr(placement, "datetime", MutableClock)
            real_assign = placement._assign

            async def commit_successor():
                async with factory() as other:
                    async with other.begin():
                        row = (
                            await other.execute(
                                select(PlacementAssignmentEntity).where(
                                    PlacementAssignmentEntity.camera_id == "cam-z"
                                )
                            )
                        ).scalar_one()
                        row.node_id = "node-c"
                        row.generation = 9
                        row.lease_expires_at = successor_lease

            async def assigning(*args, **kwargs):
                await asyncio.wait_for(commit_successor(), 3)
                return await real_assign(*args, **kwargs)

            monkeypatch.setattr(placement, "_assign", assigning)
            caplog.set_level(logging.CRITICAL, logger="app.services.placement")
            MutableClock.instant = T0 + timedelta(seconds=10)
            await _touch_heartbeat(factory, "node-b", MutableClock.instant)
            result = await placement.run_placement_once()
            row = (await _assignments(factory))[0]
            assert result["moved"] == 0, result
            assert result["deferred_autonomy"] >= 1, result
            assert row.node_id == "node-c"
            assert row.generation == 9
            assert _as_utc(row.lease_expires_at) == successor_lease
            assert "placement_failover_superseded" in caplog.text
            assert "cam-z" in caplog.text
            assert await _revocations(factory) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _qa_301_budget_kwargs():
    """The configuration QA-014-301 showed startup accepting at required 59.75s."""
    return {
        "placement_lease_seconds": 60,
        "placement_interval_seconds": 3,
        "placement_renewal_batch_size": 1,
        "placement_renewal_max_assignments": 6,
        "placement_renewal_max_run_seconds": 4,
        "placement_renewal_missed_lock_cycles": 3,
        "placement_renewal_fence_poll_seconds": 1,
        "placement_renewal_clock_skew_seconds": 0,
        "placement_renewal_safety_margin_seconds": 1,
        "placement_renewal_lock_retry_seconds": 0.25,
        "placement_renewal_lock_retry_limit": 4,
    }


def _observed_renewal_seconds(
    *,
    pages,
    interval_seconds,
    max_run_seconds,
    missed_lock_cycles,
    fence_poll_seconds,
    lock_retry_seconds,
    lock_retry_limit,
):
    """Replay the controller timeline without calling the budget formula.

    The granting run acquires immediately. Every later successful run, including
    the revisit, burns every retry and then max_run. A full miss burns the
    retries and the interval and does not spend max_run. Observation is the
    revisit commit plus one fence poll. No interval is added after that commit.
    """
    interval = max(2.0, float(interval_seconds))
    retry_sleep = float(lock_retry_seconds) * max(0, int(lock_retry_limit) - 1)
    max_run = float(max_run_seconds)
    observed = max_run + interval
    for _page in range(pages - 1):
        observed += retry_sleep + max_run + interval
    for _miss in range(int(missed_lock_cycles)):
        observed += retry_sleep + interval
    observed += retry_sleep + max_run
    observed += float(fence_poll_seconds)
    return observed


def test_revisit_max_run_is_inside_the_renewal_budget(tmp_path, monkeypatch):
    """QA-014-301. The revisit's own max run has to be inside the lease.

    Startup must reject lease 60, interval 3, batch 1, ceiling 6, max run 4,
    three missed cycles, fence 1, skew 0, margin 1, retry 0.25s and 4 attempts.
    With the assert bypassed, the same controller loop commits the revisit at
    12:01:01.75 and the 1s fence poll observes it at 12:01:02.75.
    """

    try:
        Settings(**_qa_301_budget_kwargs())
    except ValidationError as exc:
        message = str(exc)
        assert "renewal budget" in message.lower(), message
        assert "required 63.75s >= lease 60s" in message, message
    else:
        raise AssertionError("budget 59.75s was accepted; the revisit still spends max_run")

    async def scenario():
        _configure_budget(
            monkeypatch,
            lease=60,
            interval=3,
            scan_batch=1,
            renewal_batch=1,
            max_assignments=6,
            max_run=4,
            missed_lock_cycles=3,
            fence_poll=1,
            clock_skew=0,
            safety_margin=1,
            lock_retry_seconds=0.25,
            lock_retry_limit=4,
        )
        monkeypatch.setattr(placement, "assert_settings_renewal_budget", lambda _values: 0.0)
        factory = await _session_factory(tmp_path, "revisit-timeline.db")
        cameras = [f"cam-{index:02d}" for index in range(1, 7)]
        await _seed(
            factory,
            [
                _node(NODE_ID, T0),
                *[_camera(camera_id) for camera_id in cameras],
                *[
                    _assignment(camera_id, NODE_ID, T0 + timedelta(seconds=50))
                    for camera_id in cameras
                ],
            ],
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        monkeypatch.setattr(placement, "datetime", MutableClock)
        controller = _load_controller()
        state = {"phase": "pages", "page": 0, "attempt": 0, "miss": 0}

        async def lock(_session):
            if state["phase"] == "pages" and state["page"] == 0:
                state["page"] = 1
                acquired = True
            elif state["phase"] == "pages":
                if state["attempt"] < 3:
                    state["attempt"] += 1
                    return False
                state["attempt"] = 0
                state["page"] += 1
                if state["page"] == 6:
                    state["phase"] = "miss"
                acquired = True
            elif state["phase"] == "miss":
                state["attempt"] += 1
                if state["attempt"] < 4:
                    return False
                state["attempt"] = 0
                state["miss"] += 1
                if state["miss"] == 3:
                    state["phase"] = "revisit"
                return False
            else:
                if state["attempt"] < 3:
                    state["attempt"] += 1
                    return False
                acquired = True
            if acquired:
                async with factory() as heartbeat_session:
                    async with heartbeat_session.begin():
                        node = await heartbeat_session.get(InfrastructureNodeEntity, NODE_ID)
                        node.heartbeat_at = MutableClock.instant
                MutableClock.instant = MutableClock.instant + timedelta(seconds=4)
            return True

        async def advance(delay):
            MutableClock.instant = MutableClock.instant + timedelta(seconds=delay)

        monkeypatch.setattr(placement, "_leader_lock", lock)
        monkeypatch.setattr(placement.asyncio, "sleep", advance)
        monkeypatch.setattr(controller.asyncio, "sleep", advance)
        MutableClock.instant = T0
        stamps = []
        leases = []
        for index in range(10):
            await placement.run_placement_once()
            stamps.append(MutableClock.instant)
            row = {item.camera_id: item for item in await _assignments(factory)}["cam-01"]
            leases.append(_as_utc(row.lease_expires_at))
            if index < 9:
                await controller.asyncio.sleep(
                    controller.resolved_placement_interval_seconds(settings.placement_interval_seconds)
                )
        assert stamps[0] == T0 + timedelta(seconds=4)
        assert stamps[5] == T0 + timedelta(seconds=42.75)
        assert leases[6] == T0 + timedelta(seconds=60)
        assert leases[8] == T0 + timedelta(seconds=60)
        commit_at = stamps[9]
        assert commit_at == T0 + timedelta(seconds=61.75)
        observation = commit_at + timedelta(seconds=1)
        lease_end = T0 + timedelta(seconds=60)
        assert observation == T0 + timedelta(seconds=62.75)
        assert observation > lease_end + timedelta(seconds=GRACE_SECONDS)
        assert not effective_authority_active(lease_end, None, observation, 0)
        assert not effective_authority_active(lease_end, None, observation, GRACE_SECONDS)
        renewed = {item.camera_id: item for item in await _assignments(factory)}["cam-01"]
        assert renewed.node_id == NODE_ID
        assert renewed.generation == 1
        assert _as_utc(renewed.lease_expires_at) == commit_at - timedelta(seconds=4) + timedelta(seconds=60)

    asyncio.run(scenario())


def test_every_accepted_renewal_budget_observes_before_lease_end():
    """Accepted settings observe the revisit before lease end, without fence grace.

    The timeline is independent of renewal_budget_seconds. QA-014-301's point
    and the tight grid point that used to be accepted are in the set, and both
    are rejected.
    """

    lease = 60
    ceilings = (1, 4, 6, 21000)
    batches = (1, 2, 21000)
    intervals = (2, 3, 5, 10)
    max_runs = (0, 2, 4, 10)
    missed_cycles = (1, 3)
    fences = (1, 5)
    skews = (0, 5)
    margins = (1, 5)
    retries = ((0.25, 4), (0.75, 2), (2, 3), (3, 4))
    accepted = 0
    for ceiling in ceilings:
        for batch in batches:
            for interval in intervals:
                for max_run in max_runs:
                    for missed in missed_cycles:
                        for fence in fences:
                            for skew in skews:
                                for margin in margins:
                                    for retry_seconds, retry_limit in retries:
                                        pages = math.ceil(ceiling / batch)
                                        observed = _observed_renewal_seconds(
                                            pages=pages,
                                            interval_seconds=interval,
                                            max_run_seconds=max_run,
                                            missed_lock_cycles=missed,
                                            fence_poll_seconds=fence,
                                            lock_retry_seconds=retry_seconds,
                                            lock_retry_limit=retry_limit,
                                        )
                                        try:
                                            required = assert_placement_renewal_budget(
                                                max_assignments=ceiling,
                                                batch_size=batch,
                                                interval_seconds=interval,
                                                max_run_seconds=max_run,
                                                missed_lock_cycles=missed,
                                                fence_poll_seconds=fence,
                                                clock_skew_seconds=skew,
                                                safety_margin_seconds=margin,
                                                lock_retry_seconds=retry_seconds,
                                                lock_retry_limit=retry_limit,
                                                lease_seconds=lease,
                                            )
                                        except PlacementRenewalBudgetError:
                                            continue
                                        accepted += 1
                                        assert observed + skew + margin < lease, (
                                            ceiling,
                                            batch,
                                            interval,
                                            max_run,
                                            observed,
                                        )
                                        assert abs(required - (observed + skew + margin)) < 1e-9, (
                                            required,
                                            observed,
                                            skew,
                                            margin,
                                        )
    assert accepted > 0

    qa_observed = _observed_renewal_seconds(
        pages=6,
        interval_seconds=3,
        max_run_seconds=4,
        missed_lock_cycles=3,
        fence_poll_seconds=1,
        lock_retry_seconds=0.25,
        lock_retry_limit=4,
    )
    assert qa_observed == 62.75
    tight_observed = _observed_renewal_seconds(
        pages=4,
        interval_seconds=2,
        max_run_seconds=10,
        missed_lock_cycles=1,
        fence_poll_seconds=5,
        lock_retry_seconds=0.75,
        lock_retry_limit=2,
    )
    assert tight_observed == 68.75
    for kwargs, observed in (
        (
            dict(
                max_assignments=6,
                batch_size=1,
                interval_seconds=3,
                max_run_seconds=4,
                missed_lock_cycles=3,
                fence_poll_seconds=1,
                clock_skew_seconds=0,
                safety_margin_seconds=1,
                lock_retry_seconds=0.25,
                lock_retry_limit=4,
            ),
            qa_observed,
        ),
        (
            dict(
                max_assignments=4,
                batch_size=1,
                interval_seconds=2,
                max_run_seconds=10,
                missed_lock_cycles=1,
                fence_poll_seconds=5,
                clock_skew_seconds=0,
                safety_margin_seconds=1,
                lock_retry_seconds=0.75,
                lock_retry_limit=2,
            ),
            tight_observed,
        ),
    ):
        try:
            assert_placement_renewal_budget(**kwargs, lease_seconds=60)
        except PlacementRenewalBudgetError as exc:
            assert "renewal budget" in str(exc).lower(), str(exc)
        else:
            raise AssertionError(f"observation {observed}s was accepted against a 60s lease")


def test_max_run_overrun_is_logged_and_the_controller_continues(monkeypatch, caplog):
    """A rolled-back max run is critical and does not stop the controller."""

    controller = _load_controller()

    async def scenario():
        async def overrun():
            return {
                "scanned": 0,
                "moved": 0,
                "unplaced": 0,
                "deferred_autonomy": 0,
                "renewed": 0,
                "renewal_budget_exceeded": False,
                "renewal_lock_not_acquired": False,
                "renewal_max_run_exceeded": True,
                "cursor": None,
            }

        async def stop_after_one_cycle(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr(controller, "run_placement_once", overrun)
        monkeypatch.setattr(controller.asyncio, "sleep", stop_after_one_cycle)
        caplog.set_level(logging.CRITICAL, logger="placement-controller")
        try:
            await controller.main()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("controller did not reach the interval sleep")
        assert "placement_renewal_max_run_exceeded" in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("database_url", _authority_database_urls())
def test_max_run_overrun_rolls_the_lease_back(tmp_path, monkeypatch, caplog, database_url):
    """QA-014-302. A run past max_run commits no lease, including an earlier chunk.

    Two owners are due. The first chunk is written inside the transaction and
    the second chunk finds the deadline already past. Both leases stay put.
    On PostgreSQL the same rollback happens when statement_timeout cancels the
    lease statement.
    """

    async def scenario():
        factory, engine = await _open_database(tmp_path, "max-run.db", database_url)
        try:
            _configure_budget(
                monkeypatch,
                lease=60,
                interval=10,
                scan_batch=10,
                renewal_batch=10,
                max_assignments=10,
                max_run=1,
                lock_retry_limit=1,
            )
            monkeypatch.setattr(placement, "_RENEWAL_LEASE_WRITE_CHUNK", 1)
            original_lease = T0 + timedelta(seconds=50)
            await _seed(
                factory,
                [
                    _node(NODE_ID, T0),
                    _camera("cam-01"),
                    _camera("cam-02"),
                    _assignment("cam-01", NODE_ID, original_lease, assignment_id="pa-cam-01"),
                    _assignment("cam-02", NODE_ID, original_lease, assignment_id="pa-cam-02"),
                ],
            )
            monkeypatch.setattr(placement, "SessionLocal", factory)
            monkeypatch.setattr(placement, "datetime", MutableClock)
            original_hook = placement._before_renewal_lease_write
            calls = {"n": 0}

            async def trip_on_second_chunk(session=None):
                calls["n"] += 1
                if calls["n"] >= 2:
                    placement._RUN_DEADLINE.set(time.monotonic() - 1)
                if original_hook.__code__.co_argcount:
                    await original_hook(session)
                else:
                    await original_hook()

            monkeypatch.setattr(placement, "_before_renewal_lease_write", trip_on_second_chunk)
            caplog.set_level(logging.CRITICAL, logger="app.services.placement")
            MutableClock.instant = T0 + timedelta(seconds=10)
            await _touch_heartbeat(factory, NODE_ID, MutableClock.instant)
            result = await placement.run_placement_once()
            assert result["renewal_max_run_exceeded"] is True, result
            assert result["renewed"] == 0
            assert result["moved"] == 0
            assert "placement_renewal_max_run_exceeded" in caplog.text
            rows = {row.camera_id: row for row in await _assignments(factory)}
            assert rows["cam-01"].node_id == NODE_ID
            assert rows["cam-02"].node_id == NODE_ID
            assert rows["cam-01"].generation == 1
            assert rows["cam-02"].generation == 1
            assert _as_utc(rows["cam-01"].lease_expires_at) == original_lease
            assert _as_utc(rows["cam-02"].lease_expires_at) == original_lease

            if database_url != "sqlite":
                monkeypatch.setattr(placement, "_before_renewal_lease_write", original_hook)

                async def cancelled_statement(session, _chunk, _new_lease, _autonomy):
                    await session.execute(text("SELECT pg_sleep(2)"))
                    return set()

                monkeypatch.setattr(placement, "_extend_lease_chunk", cancelled_statement)
                result = await placement.run_placement_once()
                assert result["renewal_max_run_exceeded"] is True, result
                assert result["renewed"] == 0
                rows = {row.camera_id: row for row in await _assignments(factory)}
                assert _as_utc(rows["cam-01"].lease_expires_at) == original_lease
                assert _as_utc(rows["cam-02"].lease_expires_at) == original_lease
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_renewal_invalidation_is_logged_and_the_controller_continues(monkeypatch, caplog):
    """A rolled-back chunk split is critical and does not stop the controller."""

    controller = _load_controller()

    async def scenario():
        async def invalidated():
            return {
                "scanned": 0,
                "moved": 0,
                "unplaced": 0,
                "deferred_autonomy": 0,
                "renewed": 0,
                "renewal_budget_exceeded": False,
                "renewal_lock_not_acquired": False,
                "renewal_max_run_exceeded": False,
                "renewal_invalidated": True,
                "cursor": None,
            }

        async def stop_after_one_cycle(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr(controller, "run_placement_once", invalidated)
        monkeypatch.setattr(controller.asyncio, "sleep", stop_after_one_cycle)
        caplog.set_level(logging.CRITICAL, logger="placement-controller")
        try:
            await controller.main()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("controller did not reach the interval sleep")
        assert "placement_renewal_invalidated" in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("case_name", ["site", "recording"])
@pytest.mark.parametrize("database_url", _authority_database_urls())
def test_later_chunk_invalidation_rolls_the_earlier_chunk_back(
    tmp_path, monkeypatch, caplog, database_url, case_name
):
    """QA-014-401. A later chunk that misses authority rolls the earlier chunk back.

    QA used PostgreSQL 16, 4,000 assignments, and a chunk size of 2,000, and
    paused before the second chunk. This is the same split with four
    assignments and a chunk size of two. The first chunk's UPDATE has already
    run. A site move, or a disable of only the later chunk's recording
    policies, is visible to the second chunk. The attempt must commit neither
    chunk. On PostgreSQL the change is a second session's commit. SQLite holds
    the write lock after the first update, so the equivalent change is flushed
    on the open placement transaction between the second and third row.
    """

    async def scenario():
        factory, engine = await _open_database(tmp_path, f"chunk-{case_name}.db", database_url)
        try:
            lease = 60
            _configure_budget(
                monkeypatch,
                lease=lease,
                interval=10,
                scan_batch=10,
                renewal_batch=10,
                max_assignments=10,
                max_run=4,
                lock_retry_limit=1,
            )
            monkeypatch.setattr(placement, "_RENEWAL_LEASE_WRITE_CHUNK", 2)
            camera_ids = [f"cam-{index:02d}" for index in range(1, 5)]
            role = "recording" if case_name == "recording" else "media"
            original_lease = T0
            node_a = (
                _node(
                    "node-a",
                    T0,
                    region_id="region-a",
                    roles=("media", "recording"),
                    capacity=_capacity_all(),
                    load=_load_all(),
                )
                if case_name == "recording"
                else _node("node-a", T0, region_id="region-a")
            )
            rows = [
                node_a,
                _node("node-b", T0, region_id="region-b"),
                _site_region("site-a", "region-a"),
                *[_camera(camera_id) for camera_id in camera_ids],
                *[
                    _assignment(
                        camera_id,
                        "node-a",
                        original_lease,
                        role=role,
                        region_id="region-a",
                        assignment_id=f"pa-{camera_id}",
                    )
                    for camera_id in camera_ids
                ],
            ]
            if case_name == "recording":
                rows.extend(_recording_policy(camera_id, enabled=True) for camera_id in camera_ids)
            await _seed(factory, rows)
            monkeypatch.setattr(placement, "SessionLocal", factory)
            monkeypatch.setattr(placement, "datetime", MutableClock)
            original_hook = placement._before_renewal_lease_write
            original_keep = placement._keep_owner
            holder = {"session": None}

            async def remember_session(session, *args, **kwargs):
                holder["session"] = session
                return await original_keep(session, *args, **kwargs)

            if database_url == "sqlite":
                monkeypatch.setattr(placement, "_keep_owner", remember_session)

            async def apply_change(session):
                if case_name == "site":
                    region = (
                        await session.execute(
                            select(SiteRegionEntity).where(SiteRegionEntity.site_id == "site-a")
                        )
                    ).scalar_one()
                    region.region_id = "region-b"
                    return
                policies = (
                    await session.execute(
                        select(RecordingPolicyEntity).where(
                            RecordingPolicyEntity.camera_id.in_(["cam-03", "cam-04"])
                        )
                    )
                ).scalars().all()
                assert {policy.camera_id for policy in policies} == {"cam-03", "cam-04"}
                for policy in policies:
                    policy.enabled = False

            async def invalidate(session):
                if database_url == "sqlite":
                    active = session if session is not None else holder["session"]
                    assert active is not None
                    await apply_change(active)
                    await active.flush()
                    return
                async with factory() as other:
                    async with other.begin():
                        await apply_change(other)

            calls = {"n": 0}
            # PostgreSQL calls the hook once per chunk. SQLite calls it once per
            # row. Chunk size 2 means the second chunk starts on call 2 or 3.
            pause_at = 3 if database_url == "sqlite" else 2

            async def pause_before_later_chunk(session=None):
                calls["n"] += 1
                if calls["n"] == pause_at:
                    await invalidate(session)
                if original_hook.__code__.co_argcount:
                    await original_hook(session)
                else:
                    await original_hook()

            monkeypatch.setattr(placement, "_before_renewal_lease_write", pause_before_later_chunk)
            caplog.set_level(logging.CRITICAL, logger="app.services.placement")
            MutableClock.instant = T0 + timedelta(seconds=10)
            await _touch_heartbeat(factory, "node-a", MutableClock.instant)
            await _touch_heartbeat(factory, "node-b", MutableClock.instant)
            result = await placement.run_placement_once()
            assert result["renewed"] == 0, (result, calls)
            assert result["moved"] == 0, result
            assert result["deferred_autonomy"] == 0, result
            assert result["renewal_max_run_exceeded"] is False, result
            assert result["renewal_invalidated"] is True, result
            assert "placement_renewal_invalidated" in caplog.text
            stored = {row.camera_id: row for row in await _assignments(factory)}
            assert list(stored) == camera_ids
            for camera_id in camera_ids:
                assert stored[camera_id].node_id == "node-a"
                assert stored[camera_id].generation == 1
                assert stored[camera_id].role == role
                assert _as_utc(stored[camera_id].lease_expires_at) == original_lease

            async with factory() as check:
                region = (
                    await check.execute(
                        select(SiteRegionEntity).where(SiteRegionEntity.site_id == "site-a")
                    )
                ).scalar_one()
                if case_name == "site" and database_url != "sqlite":
                    assert region.region_id == "region-b"
                else:
                    assert region.region_id == "region-a"
                if case_name == "recording":
                    policies = {
                        policy.camera_id: policy.enabled
                        for policy in (
                            await check.execute(select(RecordingPolicyEntity))
                        ).scalars().all()
                    }
                    if database_url == "sqlite":
                        assert policies == {camera_id: True for camera_id in camera_ids}
                    else:
                        assert policies["cam-01"] is True
                        assert policies["cam-02"] is True
                        assert policies["cam-03"] is False
                        assert policies["cam-04"] is False

            monkeypatch.setattr(placement, "_before_renewal_lease_write", original_hook)
            follow = await placement.run_placement_once()
            fresh_lease = MutableClock.instant + timedelta(seconds=lease)
            if case_name == "site" and database_url != "sqlite":
                assert follow["renewed"] == 0, follow
                assert follow["deferred_autonomy"] == 0, follow
                assert follow["moved"] == 4, follow
                moved_rows = {row.camera_id: row for row in await _assignments(factory)}
                for camera_id in camera_ids:
                    assert moved_rows[camera_id].node_id == "node-b"
                    assert moved_rows[camera_id].generation == 2
                    assert _as_utc(moved_rows[camera_id].lease_expires_at) == fresh_lease
            elif case_name == "recording" and database_url != "sqlite":
                assert follow["renewed"] == 2, follow
                follow_rows = {
                    (row.camera_id, row.role): row for row in await _assignments(factory)
                }
                for camera_id in ("cam-01", "cam-02"):
                    renewed_row = follow_rows[(camera_id, "recording")]
                    assert renewed_row.node_id == "node-a"
                    assert renewed_row.generation == 1
                    assert _as_utc(renewed_row.lease_expires_at) == fresh_lease
                for camera_id in ("cam-03", "cam-04"):
                    held = follow_rows[(camera_id, "recording")]
                    assert held.node_id == "node-a"
                    assert held.generation == 1
                    assert _as_utc(held.lease_expires_at) == original_lease
            elif case_name == "recording":
                assert follow["renewed"] == 4, follow
                follow_rows = {
                    (row.camera_id, row.role): row for row in await _assignments(factory)
                }
                for camera_id in camera_ids:
                    renewed_row = follow_rows[(camera_id, "recording")]
                    assert renewed_row.node_id == "node-a"
                    assert renewed_row.generation == 1
                    assert _as_utc(renewed_row.lease_expires_at) == fresh_lease
            else:
                assert follow["renewed"] == 4, follow
                assert follow["moved"] == 0, follow
                follow_rows = {row.camera_id: row for row in await _assignments(factory)}
                for camera_id in camera_ids:
                    assert follow_rows[camera_id].node_id == "node-a"
                    assert follow_rows[camera_id].generation == 1
                    assert _as_utc(follow_rows[camera_id].lease_expires_at) == fresh_lease
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_max_run_zero_is_rejected_as_configuration():
    """QA-014-502. A max run of 0 is not a configuration that can start.

    The budget formula still accepts 0, which reserves no run time. That is
    how the accepted grid asks the question. Settings, which is what the
    environment loads, rejects it.
    """

    try:
        Settings(placement_renewal_max_run_seconds=0)
    except ValidationError as exc:
        assert "max run must be > 0" in str(exc), str(exc)
    else:
        raise AssertionError("max run 0 was accepted as configuration")
    required = assert_placement_renewal_budget(
        max_assignments=21000,
        batch_size=21000,
        interval_seconds=10,
        max_run_seconds=0,
        missed_lock_cycles=1,
        fence_poll_seconds=5,
        clock_skew_seconds=5,
        safety_margin_seconds=5,
        lock_retry_seconds=0.25,
        lock_retry_limit=4,
        lease_seconds=60,
    )
    assert required == 36.5


def _postgresql_database_urls():
    return [url for url in _authority_database_urls() if url != "sqlite"]


if _postgresql_database_urls():

    @pytest.mark.parametrize("case_name", ["site", "recording"])
    @pytest.mark.parametrize("database_url", _postgresql_database_urls())
    def test_recheck_window_cannot_commit_changed_authority(
        tmp_path, monkeypatch, caplog, database_url, case_name
    ):
        """QA-014-501. Authority cannot commit between the recheck and the commit.

        The second session tries to move the site, or disable the recording
        policies, after the pre-commit check returns and before this transaction
        commits. Its wait is bounded by PostgreSQL lock_timeout. On the head
        that still has the gap, that change commits and the renewal reports
        renewed rows under the new authority. With the authority rows locked
        through commit, the change waits, times out, and the leases commit
        under the authority the check locked.

        A lease granted that way stays until lease end plus fence grace. The
        next run does not extend the stale owner. After the deadline it failsover
        the moved site. A recording disable is not renewed again.
        """

        async def scenario():
            factory, engine = await _open_database(tmp_path, f"window-{case_name}.db", database_url)
            try:
                lease = 60
                _configure_budget(
                    monkeypatch,
                    lease=lease,
                    interval=10,
                    scan_batch=10,
                    renewal_batch=10,
                    max_assignments=10,
                    max_run=4,
                    lock_retry_limit=1,
                )
                monkeypatch.setattr(placement, "_RENEWAL_LEASE_WRITE_CHUNK", 2)
                camera_ids = [f"cam-{index:02d}" for index in range(1, 5)]
                role = "recording" if case_name == "recording" else "media"
                original_lease = T0
                node_a = (
                    _node(
                        "node-a",
                        T0,
                        region_id="region-a",
                        roles=("media", "recording"),
                        capacity=_capacity_all(),
                        load=_load_all(),
                    )
                    if case_name == "recording"
                    else _node("node-a", T0, region_id="region-a")
                )
                seeded = [
                    node_a,
                    _node("node-b", T0, region_id="region-b"),
                    _site_region("site-a", "region-a"),
                    *[_camera(camera_id) for camera_id in camera_ids],
                    *[
                        _assignment(
                            camera_id,
                            "node-a",
                            original_lease,
                            role=role,
                            region_id="region-a",
                            assignment_id=f"pa-{camera_id}",
                        )
                        for camera_id in camera_ids
                    ],
                ]
                if case_name == "recording":
                    for camera_id in camera_ids:
                        policy = _recording_policy(camera_id, enabled=True)
                        policy.recording_node_id = "node-a"
                        seeded.append(policy)
                await _seed(factory, seeded)
                monkeypatch.setattr(placement, "SessionLocal", factory)
                monkeypatch.setattr(placement, "datetime", MutableClock)
                original_recheck = placement._reject_stale_extended_leases
                gate = asyncio.Event()
                done = asyncio.Event()
                outcome = {"committed": None, "error": None}

                async def race_authority_change():
                    try:
                        await asyncio.wait_for(gate.wait(), 5)
                        async with factory() as other:
                            try:
                                async with other.begin():
                                    await other.execute(
                                        text("SELECT set_config('lock_timeout', '500', true)")
                                    )
                                    if case_name == "site":
                                        await other.execute(
                                            text(
                                                "UPDATE site_regions SET region_id = 'region-b' "
                                                "WHERE site_id = 'site-a'"
                                            )
                                        )
                                    else:
                                        result = await other.execute(
                                            text("UPDATE recording_policies SET enabled = false")
                                        )
                                        assert result.rowcount == 4
                                outcome["committed"] = True
                            except Exception as exc:
                                outcome["committed"] = False
                                outcome["error"] = exc
                    finally:
                        done.set()

                async def recheck_then_race(session):
                    await original_recheck(session)
                    gate.set()
                    await asyncio.wait_for(done.wait(), 2)

                monkeypatch.setattr(placement, "_reject_stale_extended_leases", recheck_then_race)
                caplog.set_level(logging.CRITICAL, logger="app.services.placement")
                MutableClock.instant = T0 + timedelta(seconds=10)
                await _touch_heartbeat(factory, "node-a", MutableClock.instant)
                await _touch_heartbeat(factory, "node-b", MutableClock.instant)
                racer = asyncio.create_task(race_authority_change())
                try:
                    result = await placement.run_placement_once()
                finally:
                    if not racer.done():
                        racer.cancel()
                    await asyncio.wait_for(racer, 3)
                fresh_lease = MutableClock.instant + timedelta(seconds=lease)
                assert outcome["committed"] is False, (outcome, result)
                assert outcome["error"] is not None
                assert "lock timeout" in str(outcome["error"]).lower(), outcome["error"]
                assert result["renewed"] == 4, result
                assert result["renewal_invalidated"] is False, result
                assert "placement_renewal_invalidated" not in caplog.text
                stored = {row.camera_id: row for row in await _assignments(factory)}
                for camera_id in camera_ids:
                    assert stored[camera_id].node_id == "node-a"
                    assert stored[camera_id].generation == 1
                    assert _as_utc(stored[camera_id].lease_expires_at) == fresh_lease
                async with factory() as check:
                    region = (
                        await check.execute(
                            select(SiteRegionEntity).where(SiteRegionEntity.site_id == "site-a")
                        )
                    ).scalar_one()
                    assert region.region_id == "region-a"
                    if case_name == "recording":
                        enabled = {
                            policy.camera_id: policy.enabled
                            for policy in (
                                await check.execute(select(RecordingPolicyEntity))
                            ).scalars().all()
                        }
                        assert enabled == {camera_id: True for camera_id in camera_ids}

                monkeypatch.setattr(placement, "_reject_stale_extended_leases", original_recheck)
                async with factory() as other:
                    async with other.begin():
                        if case_name == "site":
                            region = (
                                await other.execute(
                                    select(SiteRegionEntity).where(SiteRegionEntity.site_id == "site-a")
                                )
                            ).scalar_one()
                            region.region_id = "region-b"
                        else:
                            for policy in (
                                await other.execute(select(RecordingPolicyEntity))
                            ).scalars().all():
                                policy.enabled = False
                MutableClock.instant = MutableClock.instant + timedelta(seconds=1)
                await _touch_heartbeat(factory, "node-a", MutableClock.instant)
                await _touch_heartbeat(factory, "node-b", MutableClock.instant)
                follow = await placement.run_placement_once()
                if case_name == "recording":
                    follow_rows = {
                        (row.camera_id, row.role): row for row in await _assignments(factory)
                    }
                    later_lease = MutableClock.instant + timedelta(seconds=lease)
                    for camera_id in camera_ids:
                        recording = follow_rows[(camera_id, "recording")]
                        assert recording.node_id == "node-a"
                        assert recording.generation == 1
                        assert _as_utc(recording.lease_expires_at) == fresh_lease
                        assert _as_utc(recording.lease_expires_at) != later_lease
                else:
                    assert follow["renewed"] == 0, follow
                    assert follow["deferred_autonomy"] == 4, follow
                    assert follow["moved"] == 0, follow
                    follow_rows = {row.camera_id: row for row in await _assignments(factory)}
                    for camera_id in camera_ids:
                        assert follow_rows[camera_id].node_id == "node-a"
                        assert follow_rows[camera_id].generation == 1
                        assert _as_utc(follow_rows[camera_id].lease_expires_at) == fresh_lease
                    grace = float(settings.placement_fence_expiry_grace_seconds)
                    MutableClock.instant = fresh_lease + timedelta(seconds=grace)
                    await _touch_heartbeat(factory, "node-a", MutableClock.instant)
                    await _touch_heartbeat(factory, "node-b", MutableClock.instant)
                    failover = await placement.run_placement_once()
                    assert failover["renewed"] == 0, failover
                    assert failover["deferred_autonomy"] == 0, failover
                    assert failover["moved"] == 4, failover
                    moved_rows = {row.camera_id: row for row in await _assignments(factory)}
                    for camera_id in camera_ids:
                        assert moved_rows[camera_id].node_id == "node-b"
                        assert moved_rows[camera_id].generation == 2
            finally:
                await engine.dispose()

        asyncio.run(scenario())
