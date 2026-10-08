"""Regression for F03: failover must not overlap a live partitioned lease.

Defaults under test are lease 60s, stale detection 30s, autonomy 0s, and
node fence grace 2s. A fake clock drives both the placement controller and
the partitioned node-agent. No WAN and no wall clock.
"""

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.base import Base
from app.models.entities import CameraEntity
from app.models.placement import (
    InfrastructureNodeEntity,
    PlacementAssignmentEntity,
    PlacementRevocationEntity,
)
from app.services import placement


MODULE_PATH = Path(__file__).parents[1] / "services" / "node-agent" / "main.py"
SPEC = importlib.util.spec_from_file_location("node_agent_effective_authority", MODULE_PATH)
node_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(node_agent)

T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
LEASE_SECONDS = 60
STALE_SECONDS = 30
GRACE_SECONDS = 2
CAMERA_ID = "cam-boundary"
STREAM_KEY = "cam-boundary"


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


def _capacity():
    return {
        "max_ingress_mbps": 1000,
        "max_egress_mbps": 1000,
        "max_sources": 1000,
    }


def _load():
    return {"ingress_mbps": 100, "egress_mbps": 50, "active_sources": 1}


def _node(node_id, heartbeat_at):
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
        authority_mode="central_online",
    )


async def _seed(session_factory, *, autonomy_seconds=None):
    camera = CameraEntity(
        id=CAMERA_ID,
        tenant_id="tenant-a",
        site_id="site-a",
        name="Boundary camera",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key=STREAM_KEY,
        media_node_id="node-a",
        enabled=True,
        desired_state="provisioned",
    )
    assignment = PlacementAssignmentEntity(
        id="pa-boundary",
        camera_id=CAMERA_ID,
        role="media",
        region_id=settings.placement_default_region,
        node_id="node-a",
        cleanup_node_ids_json=[],
        generation=1,
        applied_generation=1,
        active=True,
        reason="initial",
        lease_expires_at=T0 + timedelta(seconds=LEASE_SECONDS),
        autonomy_expires_at=(
            None if autonomy_seconds is None else T0 + timedelta(seconds=autonomy_seconds)
        ),
        assigned_at=T0,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add_all(
                [
                    _node("node-a", T0),
                    _node("node-b", T0),
                    camera,
                    assignment,
                ]
            )


async def _touch_heartbeat(session_factory, node_id, when):
    async with session_factory() as session:
        async with session.begin():
            node = await session.get(InfrastructureNodeEntity, node_id)
            node.heartbeat_at = when


async def _control_assignment(session_factory):
    async with session_factory() as session:
        return (
            await session.execute(
                select(PlacementAssignmentEntity).where(
                    PlacementAssignmentEntity.camera_id == CAMERA_ID,
                    PlacementAssignmentEntity.role == "media",
                )
            )
        ).scalar_one()


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _partitioned_agent(tmp_path, monkeypatch, *, autonomy_seconds=None):
    agent_settings = node_agent.NodeAgentSettings.model_validate(
        {
            "NODE_ID": "node-a",
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
    agent = node_agent.NodeAgent(agent_settings)
    key = agent._fence_key(CAMERA_ID, "media")
    agent._fence_state["assignments"][key] = {
        "assignment_id": "pa-boundary",
        "camera_id": CAMERA_ID,
        "role": "media",
        "generation": 1,
        "lease_expires_at": (T0 + timedelta(seconds=LEASE_SECONDS)).isoformat(),
        "autonomy_expires_at": (
            None
            if autonomy_seconds is None
            else (T0 + timedelta(seconds=autonomy_seconds)).isoformat()
        ),
        "execution_key": STREAM_KEY,
        "execution_keys": [STREAM_KEY],
        "fenced": False,
        "revoked": False,
    }

    async def fake_delete(role, execution_key):
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)
    return agent, key


async def _node_a_executing(agent, key):
    await agent._enforce_cached_expiry()
    item = agent._fence_state["assignments"][key]
    return not bool(item.get("fenced"))


async def _prepare(tmp_path, monkeypatch, *, autonomy_seconds=None):
    assert settings.placement_lease_seconds == LEASE_SECONDS
    assert settings.placement_node_stale_seconds == STALE_SECONDS
    assert settings.placement_offline_autonomy_seconds == 0
    assert settings.placement_fence_expiry_grace_seconds == GRACE_SECONDS

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'boundary.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await _seed(session_factory, autonomy_seconds=autonomy_seconds)
    monkeypatch.setattr(placement, "SessionLocal", session_factory)
    monkeypatch.setattr(placement, "datetime", MutableClock)
    monkeypatch.setattr(node_agent, "datetime", MutableClock)
    agent, key = _partitioned_agent(tmp_path, monkeypatch, autonomy_seconds=autonomy_seconds)
    return session_factory, agent, key


def test_partitioned_owner_and_successor_are_never_both_authorized(tmp_path, monkeypatch):
    """Stale detection must not authorize node B while node A still holds lease+grace."""

    async def scenario():
        session_factory, agent, key = await _prepare(tmp_path, monkeypatch)
        try:
            # Just after stale detection, at lease expiry, and during the grace
            # second, A is still executing. Control must not have authorized B.
            for offset in (STALE_SECONDS + 1, LEASE_SECONDS, LEASE_SECONDS + GRACE_SECONDS - 1):
                MutableClock.instant = T0 + timedelta(seconds=offset)
                await _touch_heartbeat(session_factory, "node-b", MutableClock.instant)
                await placement.run_placement_once()
                executing = await _node_a_executing(agent, key)
                owner = (await _control_assignment(session_factory)).node_id
                successor_authorized = owner == "node-b"
                assert not (executing and successor_authorized), (
                    f"split-brain at +{offset}s: node A executing={executing} "
                    f"control_owner={owner} lease={LEASE_SECONDS}s grace={GRACE_SECONDS}s "
                    f"stale={STALE_SECONDS}s autonomy=0"
                )
                assert executing and owner == "node-a", (
                    f"partitioned owner lost authority early at +{offset}s: "
                    f"executing={executing} control_owner={owner}"
                )

            # At lease+grace the old owner fences and only then may B be authorized.
            MutableClock.instant = T0 + timedelta(seconds=LEASE_SECONDS + GRACE_SECONDS)
            await _touch_heartbeat(session_factory, "node-b", MutableClock.instant)
            executing = await _node_a_executing(agent, key)
            await placement.run_placement_once()
            owner = (await _control_assignment(session_factory)).node_id
            assert not executing, "node A kept executing at the effective-authority deadline"
            assert owner == "node-b", f"control did not hand over at the deadline, owner={owner}"
            assert not (executing and owner == "node-b")
            async with session_factory() as session:
                revocation = (
                    await session.execute(
                        select(PlacementRevocationEntity).where(
                            PlacementRevocationEntity.node_id == "node-a",
                            PlacementRevocationEntity.revoked_generation == 1,
                        )
                    )
                ).scalar_one()
            assert _as_utc(revocation.valid_until) == T0 + timedelta(
                seconds=LEASE_SECONDS + GRACE_SECONDS
            )
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_live_autonomy_extends_the_shared_boundary_past_lease_and_grace(tmp_path, monkeypatch):
    """A later autonomy grant keeps both sides from handing over at lease+grace."""

    async def scenario():
        session_factory, agent, key = await _prepare(tmp_path, monkeypatch, autonomy_seconds=90)
        try:
            MutableClock.instant = T0 + timedelta(seconds=LEASE_SECONDS + GRACE_SECONDS)
            await _touch_heartbeat(session_factory, "node-b", MutableClock.instant)
            executing = await _node_a_executing(agent, key)
            await placement.run_placement_once()
            owner = (await _control_assignment(session_factory)).node_id
            assert executing and owner == "node-a"

            MutableClock.instant = T0 + timedelta(seconds=90)
            await _touch_heartbeat(session_factory, "node-b", MutableClock.instant)
            executing = await _node_a_executing(agent, key)
            await placement.run_placement_once()
            owner = (await _control_assignment(session_factory)).node_id
            assert not executing
            assert owner == "node-b"
            assert not (executing and owner == "node-b")
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_fresh_owner_is_renewed_for_a_full_lease(tmp_path, monkeypatch):
    """Closing the overlap must not shorten the lease of a healthy owner."""

    async def scenario():
        session_factory, agent, key = await _prepare(tmp_path, monkeypatch)
        try:
            MutableClock.instant = T0 + timedelta(seconds=10)
            await _touch_heartbeat(session_factory, "node-a", MutableClock.instant)
            await _touch_heartbeat(session_factory, "node-b", MutableClock.instant)
            await placement.run_placement_once()
            row = await _control_assignment(session_factory)
            executing = await _node_a_executing(agent, key)
            assert row.node_id == "node-a"
            assert row.generation == 1
            assert _as_utc(row.lease_expires_at) == MutableClock.instant + timedelta(seconds=LEASE_SECONDS)
            assert executing
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_default_grace_is_one_shared_constant():
    """Control and the node-agent must default to the same fence grace."""
    from app.core import effective_authority

    assert effective_authority.DEFAULT_FENCE_EXPIRY_GRACE_SECONDS == GRACE_SECONDS
    assert settings.placement_fence_expiry_grace_seconds == (
        effective_authority.DEFAULT_FENCE_EXPIRY_GRACE_SECONDS
    )
    grace_field = node_agent.NodeAgentSettings.model_fields["fence_expiry_grace_seconds"]
    assert grace_field.default == effective_authority.DEFAULT_FENCE_EXPIRY_GRACE_SECONDS
    assert Path(node_agent._AUTHORITY.__file__).resolve() == Path(effective_authority.__file__).resolve()


def test_effective_authority_edges():
    """Cover the deadline, acknowledgement, missing input, and invalid grace."""
    from app.core.effective_authority import (
        effective_authority_active,
        effective_authority_deadline,
    )

    lease = T0 + timedelta(seconds=LEASE_SECONDS)
    grace = GRACE_SECONDS
    during_grace = T0 + timedelta(seconds=LEASE_SECONDS + 1)
    deadline = T0 + timedelta(seconds=LEASE_SECONDS + GRACE_SECONDS)
    assert effective_authority_active(lease, None, during_grace, grace)
    assert effective_authority_deadline(lease, None, grace) == deadline
    assert not effective_authority_active(lease, None, deadline, grace)

    later_autonomy = T0 + timedelta(seconds=90)
    assert effective_authority_active(lease, later_autonomy, deadline, grace)
    assert not effective_authority_active(lease, later_autonomy, later_autonomy, grace)
    earlier_autonomy = T0 + timedelta(seconds=10)
    assert effective_authority_deadline(lease, earlier_autonomy, grace) == deadline

    assert not effective_authority_active(
        lease,
        later_autonomy,
        T0,
        grace,
        acknowledged_fenced=True,
    )
    assert effective_authority_deadline(None, None, grace) is None
    assert not effective_authority_active(None, None, T0, grace)
    assert effective_authority_active(None, later_autonomy, deadline, grace)

    naive_lease = lease.replace(tzinfo=None)
    naive_now = during_grace.replace(tzinfo=None)
    assert effective_authority_active(naive_lease, None, naive_now, grace)

    try:
        effective_authority_active(lease, None, T0, -1)
    except ValueError as exc:
        assert "grace" in str(exc)
    else:
        raise AssertionError("negative grace must be rejected")
