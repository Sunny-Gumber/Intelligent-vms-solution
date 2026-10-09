import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.entities import CameraEntity
from app.core.auth import Principal
from app.models.placement import (
    InfrastructureNodeEntity,
    PlacementAssignmentEntity,
    PlacementRevocationEntity,
)
from app.models.placement_schemas import NodeHeartbeat
from app.routers import placement as placement_router
from app.routers import recordings as recordings_router
from app.routers.placement import heartbeat_node
from app.routers.recordings import _validate_recording_evidence, _validate_recording_fence
from app.services import placement
from tests.time_control import FIXED_NOW, FrozenDateTime


MODULE_PATH = Path(__file__).parents[1] / "services" / "node-agent" / "main.py"
SPEC = importlib.util.spec_from_file_location("node_agent_autonomy", MODULE_PATH)
node_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(node_agent)


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    """Freeze control, node-agent, and recording clock reads for autonomy tests."""
    monkeypatch.setattr(node_agent, "datetime", FrozenDateTime)
    monkeypatch.setattr(placement, "datetime", FrozenDateTime)
    monkeypatch.setattr(placement_router, "datetime", FrozenDateTime)
    monkeypatch.setattr(recordings_router, "datetime", FrozenDateTime)


def agent_settings(tmp_path):
    return node_agent.NodeAgentSettings.model_validate(
        {
            "NODE_ID": "node-a",
            "REGION_ID": "region-a",
            "NODE_ROLES": "media,recording",
            "CONTROL_API_URL": "http://control-api:8000",
            "NODE_AGENT_TOKEN": "secret-token",
            "MEDIAMTX_API_URL": "http://mediamtx:9997",
            "NODE_FENCING_ENABLED": True,
            "FENCE_STATE_PATH": str(tmp_path / "fence-state.json"),
            "FENCE_EXPIRY_GRACE_SECONDS": 0,
        }
    )


def camera():
    return CameraEntity(
        id="cam-1",
        tenant_id="t",
        site_id="s",
        name="camera",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="cam-1",
        media_node_id="node-a",
        desired_state="provisioned",
        enabled=True,
    )


def snapshot_node(node_id="node-a"):
    return placement.NodeSnapshot(
        id=node_id,
        region_id="region-a",
        roles=frozenset({"media"}),
        state="active",
        enabled=True,
        capacity={"max_sources": 100},
        load={"active_sources": 1},
        heartbeat_at=FIXED_NOW,
    )


def assignment(*, lease_seconds=-5, autonomy_seconds=60, revoked=False):
    return {
        "assignment_id": "pa-1",
        "camera_id": "cam-1",
        "role": "media",
        "generation": 3,
        "lease_expires_at": (
            FIXED_NOW + timedelta(seconds=lease_seconds)
        ).isoformat(),
        "autonomy_expires_at": (
            FIXED_NOW + timedelta(seconds=autonomy_seconds)
        ).isoformat(),
        "execution_key": "cam-1",
        "fenced": False,
        "revoked": revoked,
    }


def test_assign_renews_pregranted_autonomy_window():
    old = settings.placement_offline_autonomy_seconds
    settings.placement_offline_autonomy_seconds = 300
    try:
        row = PlacementAssignmentEntity(
            id="pa-1",
            camera_id="cam-1",
            role="media",
            region_id="region-a",
            node_id="node-a",
            cleanup_node_ids_json=[],
            generation=2,
            applied_generation=2,
            active=True,
            reason="initial",
            lease_expires_at=FIXED_NOW + timedelta(seconds=30),
            assigned_at=FIXED_NOW,
        )
        before = FIXED_NOW
        updated, changed = asyncio.run(
            placement._assign(
                None,
                camera(),
                "media",
                "region-a",
                snapshot_node(),
                row,
                reason="renew",
            )
        )
        assert changed is False
        assert updated.autonomy_expires_at is not None
        assert updated.autonomy_expires_at >= before + timedelta(seconds=295)
    finally:
        settings.placement_offline_autonomy_seconds = old


def test_autonomy_active_is_hard_failover_deferral_boundary():
    now = FIXED_NOW
    row = PlacementAssignmentEntity(
        id="pa-1",
        camera_id="cam-1",
        role="media",
        region_id="region-a",
        node_id="node-a",
        cleanup_node_ids_json=[],
        generation=2,
        active=True,
        reason="initial",
        lease_expires_at=now - timedelta(seconds=1),
        autonomy_expires_at=now + timedelta(seconds=30),
        assigned_at=now,
    )
    assert placement.autonomy_active(row, now)
    row.autonomy_expires_at = now - timedelta(seconds=1)
    assert not placement.autonomy_active(row, now)


def test_expired_central_lease_continues_with_live_offline_authority(tmp_path, monkeypatch):
    agent = node_agent.NodeAgent(agent_settings(tmp_path))
    key = agent._fence_key("cam-1", "media")
    agent._fence_state["assignments"][key] = assignment(
        lease_seconds=-10,
        autonomy_seconds=60,
    )
    deleted = []

    async def fake_delete(role, execution_key):
        deleted.append((role, execution_key))
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)
    asyncio.run(agent._enforce_cached_expiry())

    assert deleted == []
    assert agent._fence_state["assignments"][key]["fenced"] is False
    asyncio.run(agent.close())


def test_autonomy_hard_deadline_fences_cached_owner(tmp_path, monkeypatch):
    agent = node_agent.NodeAgent(agent_settings(tmp_path))
    key = agent._fence_key("cam-1", "media")
    agent._fence_state["assignments"][key] = assignment(
        lease_seconds=-10,
        autonomy_seconds=-1,
    )
    deleted = []

    async def fake_delete(role, execution_key):
        deleted.append((role, execution_key))
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)
    asyncio.run(agent._enforce_cached_expiry())

    assert deleted == [("media", "cam-1")]
    assert agent._fence_state["assignments"][key]["fenced"] is True
    asyncio.run(agent.close())


def test_revoked_generation_never_uses_offline_autonomy(tmp_path, monkeypatch):
    agent = node_agent.NodeAgent(agent_settings(tmp_path))
    key = agent._fence_key("cam-1", "media")
    agent._fence_state["assignments"][key] = assignment(
        lease_seconds=60,
        autonomy_seconds=600,
        revoked=True,
    )
    deleted = []

    async def fake_delete(role, execution_key):
        deleted.append((role, execution_key))
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)
    asyncio.run(agent._enforce_cached_expiry())

    assert deleted == [("media", "cam-1")]
    asyncio.run(agent.close())


def test_control_loss_enters_regional_autonomous_only_with_live_grant(tmp_path, monkeypatch):
    agent = node_agent.NodeAgent(agent_settings(tmp_path))
    key = agent._fence_key("cam-1", "media")
    agent._fence_state["assignments"][key] = assignment(
        lease_seconds=-5,
        autonomy_seconds=60,
    )

    async def fail_snapshot():
        raise RuntimeError("wan down")

    async def fake_delete(_role, _execution_key):
        return True

    monkeypatch.setattr(agent, "_fetch_fence_snapshot", fail_snapshot)
    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)

    asyncio.run(agent.fence_once())
    assert agent._fence_state["authority_mode"] == "regional_autonomous"
    asyncio.run(agent.close())


def test_control_loss_without_live_grant_enters_fenced_degraded(tmp_path, monkeypatch):
    agent = node_agent.NodeAgent(agent_settings(tmp_path))
    key = agent._fence_key("cam-1", "media")
    agent._fence_state["assignments"][key] = assignment(
        lease_seconds=-5,
        autonomy_seconds=-1,
    )

    async def fail_snapshot():
        raise RuntimeError("wan down")

    async def fake_delete(_role, _execution_key):
        return True

    monkeypatch.setattr(agent, "_fetch_fence_snapshot", fail_snapshot)
    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)

    asyncio.run(agent.fence_once())
    assert agent._fence_state["authority_mode"] == "fenced_degraded"
    assert agent._fence_state["assignments"][key]["fenced"] is True
    asyncio.run(agent.close())


def test_restart_restores_offline_grant_and_authority_state(tmp_path):
    first = node_agent.NodeAgent(agent_settings(tmp_path))
    key = first._fence_key("cam-1", "media")
    first._fence_state["authority_mode"] = "regional_autonomous"
    first._fence_state["assignments"][key] = assignment(
        lease_seconds=-5,
        autonomy_seconds=60,
    )
    first._save_fence_state()
    asyncio.run(first.close())

    second = node_agent.NodeAgent(agent_settings(tmp_path))
    assert second._fence_state["authority_mode"] == "regional_autonomous"
    assert second._fence_state["assignments"][key]["autonomy_expires_at"]
    asyncio.run(second.close())


def test_successful_fence_snapshot_returns_to_central_online(tmp_path, monkeypatch):
    agent = node_agent.NodeAgent(agent_settings(tmp_path))
    agent._fence_state["authority_mode"] = "regional_autonomous"

    async def snapshot():
        return {
            "server_time": FIXED_NOW.isoformat(),
            "node_id": "node-a",
            "assignments": [],
            "revocations": [],
        }

    monkeypatch.setattr(agent, "_fetch_fence_snapshot", snapshot)
    asyncio.run(agent.fence_once())
    assert agent._fence_state["authority_mode"] == "central_online"
    asyncio.run(agent.close())


class ScalarResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class RecordingFenceSession:
    def __init__(self, assignment_row, revocation_row=None):
        self.assignment_row = assignment_row
        self.revocation_row = revocation_row
        self.calls = 0

    async def execute(self, _query):
        self.calls += 1
        if self.calls == 1:
            return ScalarResult(self.assignment_row)
        return ScalarResult(self.revocation_row)


def test_current_recording_generation_accepts_evidence_inside_autonomy_window():
    now = FIXED_NOW
    row = PlacementAssignmentEntity(
        id="pa-rec",
        camera_id="cam-1",
        role="recording",
        region_id="region-a",
        node_id="node-a",
        cleanup_node_ids_json=[],
        generation=5,
        active=True,
        reason="initial",
        lease_expires_at=now - timedelta(seconds=30),
        autonomy_expires_at=now + timedelta(seconds=60),
        assigned_at=now,
    )
    _validate_recording_fence(
        row,
        "node-a",
        5,
        evidence_time=now,
    )


def test_historical_recording_backfill_accepts_only_old_authority_window():
    now = FIXED_NOW
    current = PlacementAssignmentEntity(
        id="pa-rec",
        camera_id="cam-1",
        role="recording",
        region_id="region-a",
        node_id="node-b",
        cleanup_node_ids_json=["node-a"],
        generation=6,
        active=True,
        reason="failover",
        lease_expires_at=now + timedelta(seconds=60),
        autonomy_expires_at=now + timedelta(seconds=120),
        assigned_at=now,
    )
    revoked = PlacementRevocationEntity(
        id="rev-rec",
        assignment_id=current.id,
        camera_id="cam-1",
        role="recording",
        node_id="node-a",
        revoked_generation=5,
        reason="failover",
        valid_until=now - timedelta(seconds=10),
    )
    session = RecordingFenceSession(current, revoked)

    asyncio.run(
        _validate_recording_evidence(
            session,
            camera_id="cam-1",
            recording_node_id="node-a",
            assignment_generation=5,
            evidence_time=now - timedelta(seconds=20),
        )
    )


def test_historical_recording_backfill_rejects_after_old_authority_window():
    from fastapi import HTTPException

    now = FIXED_NOW
    current = PlacementAssignmentEntity(
        id="pa-rec",
        camera_id="cam-1",
        role="recording",
        region_id="region-a",
        node_id="node-b",
        cleanup_node_ids_json=["node-a"],
        generation=6,
        active=True,
        reason="failover",
        lease_expires_at=now + timedelta(seconds=60),
        autonomy_expires_at=now + timedelta(seconds=120),
        assigned_at=now,
    )
    revoked = PlacementRevocationEntity(
        id="rev-rec",
        assignment_id=current.id,
        camera_id="cam-1",
        role="recording",
        node_id="node-a",
        revoked_generation=5,
        reason="failover",
        valid_until=now - timedelta(seconds=10),
    )
    session = RecordingFenceSession(current, revoked)

    try:
        asyncio.run(
            _validate_recording_evidence(
                session,
                camera_id="cam-1",
                recording_node_id="node-a",
                assignment_generation=5,
                evidence_time=now,
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("late historical recording metadata must be rejected")


class HeartbeatSession:
    def __init__(self, row):
        self.row = row

    async def get(self, _model, _row_id):
        return self.row

    async def execute(self, statement):
        """Apply the conditional heartbeat UPDATE against the in-memory row."""
        params = statement.compile().params
        clamped = params["heartbeat_at"]
        if self.row.heartbeat_at < clamped:
            self.row.load_json = params["load_json"]
            self.row.authority_mode = params["authority_mode"]
            self.row.heartbeat_at = clamped
            if "role_readiness_json" in params:
                self.row.role_readiness_json = params["role_readiness_json"]
        return None

    def expire(self, _row):
        return None

    async def commit(self):
        return None

    async def refresh(self, _row):
        return None


def test_delayed_spooled_heartbeat_preserves_observation_freshness():
    observed = FIXED_NOW - timedelta(minutes=5)
    row = InfrastructureNodeEntity(
        id="node-a",
        name="node-a",
        region_id="region-a",
        roles_json=["media"],
        state="active",
        enabled=True,
        endpoints_json={},
        capacity_json={"max_sources": 100},
        load_json={},
        # Older than the delayed observation, so that observation is still the
        # newest heartbeat and must be stored at observed_at rather than now.
        heartbeat_at=FIXED_NOW - timedelta(minutes=10),
        authority_mode="central_online",
        generation=1,
    )
    principal = Principal(
        subject="svc-node-a",
        roles=frozenset({"service"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
        node_id="node-a",
    )
    payload = NodeHeartbeat(
        load={"active_sources": 1},
        authority_mode="regional_autonomous",
        observed_at=observed,
    )

    asyncio.run(
        heartbeat_node(
            node_id="node-a",
            payload=payload,
            session=HeartbeatSession(row),
            principal=principal,
        )
    )

    assert row.heartbeat_at == observed
    assert row.authority_mode == "regional_autonomous"
    snapshot = placement.NodeSnapshot(
        id=row.id,
        region_id=row.region_id,
        roles=frozenset(row.roles_json),
        state=row.state,
        enabled=row.enabled,
        capacity=row.capacity_json,
        load=row.load_json,
        heartbeat_at=row.heartbeat_at,
        authority_mode=row.authority_mode,
    )
    assert not placement.node_eligible(
        snapshot,
        "media",
        "region-a",
        FIXED_NOW,
    )


def test_future_heartbeat_timestamp_is_clamped_to_server_time():
    observed = FIXED_NOW + timedelta(hours=1)
    row = InfrastructureNodeEntity(
        id="node-a",
        name="node-a",
        region_id="region-a",
        roles_json=["media"],
        state="active",
        enabled=True,
        endpoints_json={},
        capacity_json={"max_sources": 100},
        load_json={},
        heartbeat_at=FIXED_NOW - timedelta(minutes=5),
        authority_mode="central_online",
        generation=1,
    )
    principal = Principal(
        subject="svc-node-a",
        roles=frozenset({"service"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
        node_id="node-a",
    )
    before = FIXED_NOW
    asyncio.run(
        heartbeat_node(
            node_id="node-a",
            payload=NodeHeartbeat(
                load={"active_sources": 1},
                authority_mode="central_online",
                observed_at=observed,
            ),
            session=HeartbeatSession(row),
            principal=principal,
        )
    )
    after = FIXED_NOW
    assert before <= row.heartbeat_at <= after
