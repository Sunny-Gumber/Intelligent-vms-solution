import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.models.placement import PlacementAssignmentEntity, PlacementRevocationEntity
from app.routers import recordings as recordings_router
from app.routers.recordings import _validate_recording_fence
from app.services import fencing, placement
from tests.time_control import FIXED_NOW, FrozenDateTime


MODULE_PATH = Path(__file__).parents[1] / "services" / "node-agent" / "main.py"
SPEC = importlib.util.spec_from_file_location("node_agent_fencing", MODULE_PATH)
node_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(node_agent)


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    """Freeze internal Phase 7 clock reads for deterministic fencing tests."""
    monkeypatch.setattr(node_agent, "datetime", FrozenDateTime)
    monkeypatch.setattr(placement, "datetime", FrozenDateTime)
    monkeypatch.setattr(recordings_router, "datetime", FrozenDateTime)


def make_agent_settings(tmp_path, **overrides):
    payload = {
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
    payload.update(overrides)
    return node_agent.NodeAgentSettings.model_validate(payload)


def camera():
    return CameraEntity(
        id="cam-1",
        tenant_id="tenant-a",
        site_id="site-a",
        name="Gate",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="gate-live",
        media_node_id="node-a",
        enabled=True,
        desired_state="provisioned",
    )


def assignment(node_id="node-a", generation=1, role="recording", lease_seconds=60):
    return PlacementAssignmentEntity(
        id="pa-1",
        camera_id="cam-1",
        role=role,
        region_id="region-a",
        node_id=node_id,
        cleanup_node_ids_json=[],
        generation=generation,
        active=True,
        reason="initial",
        lease_expires_at=FIXED_NOW + timedelta(seconds=lease_seconds),
        assigned_at=FIXED_NOW,
    )


class FakeScalars:
    def __init__(self, rows):
        self.rows = list(rows)

    def all(self):
        return list(self.rows)


class FakeResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return FakeScalars(self.rows)


class AssignSession:
    def __init__(self, pending=None):
        self.added = []
        self.pending = list(pending or [])

    def add(self, row):
        self.added.append(row)

    async def execute(self, _query):
        return FakeResult(self.pending)


def test_assignment_change_creates_durable_revocation():
    cam = camera()
    row = assignment(node_id="node-a", generation=7, role="media")
    session = AssignSession()

    target = placement.NodeSnapshot(
        id="node-b",
        region_id="region-a",
        roles=frozenset({"media"}),
        state="active",
        enabled=True,
        capacity={"max_sources": 10},
        load={"active_sources": 1},
        heartbeat_at=FIXED_NOW,
    )

    updated, changed = asyncio.run(
        placement._assign(
            session,
            cam,
            "media",
            "region-a",
            target,
            row,
            reason="failover",
        )
    )

    assert changed
    assert updated.node_id == "node-b"
    assert updated.generation == 8
    assert updated.cleanup_node_ids_json == ["node-a"]
    revocations = [x for x in session.added if isinstance(x, PlacementRevocationEntity)]
    assert len(revocations) == 1
    assert revocations[0].node_id == "node-a"
    assert revocations[0].revoked_generation == 7


def test_failback_cancels_obsolete_revocation_for_new_owner():
    cam = camera()
    row = assignment(node_id="node-b", generation=2, role="media")
    row.cleanup_node_ids_json = ["node-a"]
    pending = PlacementRevocationEntity(
        id="rev-a",
        assignment_id=row.id,
        camera_id=row.camera_id,
        role="media",
        node_id="node-a",
        revoked_generation=1,
        reason="failover",
    )
    session = AssignSession([pending])

    target = placement.NodeSnapshot(
        id="node-a",
        region_id="region-a",
        roles=frozenset({"media"}),
        state="active",
        enabled=True,
        capacity={"max_sources": 10},
        load={"active_sources": 1},
        heartbeat_at=FIXED_NOW,
    )

    updated, changed = asyncio.run(
        placement._assign(
            session,
            cam,
            "media",
            "region-a",
            target,
            row,
            reason="failback",
        )
    )

    assert changed
    assert updated.node_id == "node-a"
    assert updated.generation == 3
    assert "node-a" not in updated.cleanup_node_ids_json
    assert pending.cancelled_at is not None


def test_recording_fence_accepts_only_current_node_generation_and_live_lease():
    row = assignment(node_id="record-b", generation=4, role="recording", lease_seconds=60)
    _validate_recording_fence(row, "record-b", 4)

    with pytest.raises(HTTPException) as stale_node:
        _validate_recording_fence(row, "record-a", 4)
    assert stale_node.value.status_code == 409

    with pytest.raises(HTTPException) as stale_generation:
        _validate_recording_fence(row, "record-b", 3)
    assert stale_generation.value.status_code == 409

    expired = assignment(node_id="record-b", generation=4, role="recording", lease_seconds=-1)
    with pytest.raises(HTTPException) as expired_error:
        _validate_recording_fence(expired, "record-b", 4)
    assert expired_error.value.status_code == 409


def test_node_agent_persists_generation_and_fences_expired_work(tmp_path, monkeypatch):
    settings = make_agent_settings(tmp_path)
    agent = node_agent.NodeAgent(settings)
    deleted = []

    async def fake_delete(role, execution_key):
        deleted.append((role, execution_key))
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)

    expired = FIXED_NOW - timedelta(seconds=10)
    snapshot = {
        "server_time": FIXED_NOW.isoformat(),
        "node_id": "node-a",
        "assignments": [
            {
                "assignment_id": "pa-1",
                "camera_id": "cam-1",
                "role": "recording",
                "generation": 9,
                "lease_expires_at": expired.isoformat(),
                "execution_key": "gate-live-record",
            }
        ],
        "revocations": [],
    }

    asyncio.run(agent._apply_fence_snapshot(snapshot))
    asyncio.run(agent._enforce_cached_expiry())

    key = agent._fence_key("cam-1", "recording")
    assert agent._fence_state["assignments"][key]["generation"] == 9
    assert agent._fence_state["assignments"][key]["fenced"] is True
    assert deleted == [("recording", "gate-live-record")]
    assert Path(settings.fence_state_path).exists()
    asyncio.run(agent.close())


def test_node_agent_reloads_cached_lease_after_restart_and_fences_offline(tmp_path, monkeypatch):
    settings = make_agent_settings(tmp_path)
    first = node_agent.NodeAgent(settings)
    key = first._fence_key("cam-1", "media")
    first._fence_state["assignments"][key] = {
        "assignment_id": "pa-1",
        "camera_id": "cam-1",
        "role": "media",
        "generation": 3,
        "lease_expires_at": (FIXED_NOW - timedelta(seconds=5)).isoformat(),
        "execution_key": "gate-live",
        "fenced": False,
    }
    first._save_fence_state()
    asyncio.run(first.close())

    second = node_agent.NodeAgent(settings)
    deleted = []

    async def fake_delete(role, execution_key):
        deleted.append((role, execution_key))
        return True

    monkeypatch.setattr(second, "_delete_execution_key", fake_delete)
    asyncio.run(second._enforce_cached_expiry())

    assert second._fence_state["assignments"][key]["generation"] == 3
    assert second._fence_state["assignments"][key]["fenced"] is True
    assert deleted == [("media", "gate-live")]
    asyncio.run(second.close())


def test_node_agent_revocation_deletes_then_acks(tmp_path, monkeypatch):
    settings = make_agent_settings(tmp_path)
    agent = node_agent.NodeAgent(settings)
    key = agent._fence_key("cam-1", "recording")
    agent._fence_state["assignments"][key] = {
        "assignment_id": "pa-1",
        "camera_id": "cam-1",
        "role": "recording",
        "generation": 5,
        "lease_expires_at": (FIXED_NOW + timedelta(seconds=60)).isoformat(),
        "execution_key": "gate-live-record",
        "fenced": False,
    }

    deleted = []
    acked = []

    async def fake_delete(role, execution_key):
        deleted.append((role, execution_key))
        return True

    async def fake_ack(revocation_id):
        acked.append(revocation_id)
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)
    monkeypatch.setattr(agent, "_ack_revocation", fake_ack)

    snapshot = {
        "server_time": FIXED_NOW.isoformat(),
        "node_id": "node-a",
        "assignments": [],
        "revocations": [
            {
                "revocation_id": "rev-1",
                "assignment_id": "pa-1",
                "camera_id": "cam-1",
                "role": "recording",
                "revoked_generation": 5,
                "execution_key": "gate-live-record",
                "created_at": FIXED_NOW.isoformat(),
            }
        ],
    }

    asyncio.run(agent._apply_fence_snapshot(snapshot))

    assert deleted == [("recording", "gate-live-record")]
    assert acked == ["rev-1"]
    tombstone = agent._fence_state["assignments"][key]
    assert tombstone["revoked"] is True
    assert tombstone["generation"] == 5

    # A stale local process that reasserts the path is fenced again on the
    # next cycle even though the control-plane revocation was already ACKed.
    asyncio.run(agent._enforce_cached_expiry())
    assert deleted == [
        ("recording", "gate-live-record"),
        ("recording", "gate-live-record"),
    ]
    asyncio.run(agent.close())


def test_node_agent_request_sends_bearer_token(tmp_path, monkeypatch):
    settings = make_agent_settings(tmp_path, NODE_FENCING_ENABLED=False)
    agent = node_agent.NodeAgent(settings)
    captured = {}

    async def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(agent._client, "request", fake_request)
    status = asyncio.run(agent._request("POST", "/heartbeat", {"x": 1}))

    assert status == 200
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["url"] == "http://control-api:8000/heartbeat"
    asyncio.run(agent.close())


class SnapshotSession:
    def __init__(self, assignments, revocations, cameras, policies):
        self.assignments = assignments
        self.revocations = revocations
        self.cameras = cameras
        self.policies = policies
        self.calls = 0

    async def execute(self, query):
        text = str(query)
        if "FROM placement_assignments" in text:
            return FakeResult(self.assignments)
        if "FROM placement_revocations" in text:
            return FakeResult(self.revocations)
        if "FROM cameras" in text:
            return FakeResult(self.cameras)
        if "FROM recording_policies" in text:
            return FakeResult(self.policies)
        raise AssertionError(text)


def test_fence_snapshot_exposes_current_generation_and_durable_revoke():
    cam = camera()
    current = assignment(node_id="node-b", generation=2, role="media")
    revoke = PlacementRevocationEntity(
        id="rev-1",
        assignment_id=current.id,
        camera_id=cam.id,
        role="media",
        node_id="node-b",
        revoked_generation=1,
        reason="failover",
        created_at=FIXED_NOW,
    )
    session = SnapshotSession([current], [revoke], [cam], [])

    snapshot = asyncio.run(fencing.fence_snapshot(session, "node-b"))

    assert snapshot["assignments"][0]["generation"] == 2
    assert snapshot["assignments"][0]["execution_key"] == "gate-live"
    assert snapshot["revocations"][0]["revoked_generation"] == 1


def test_active_newer_generation_clears_revocation_tombstone(tmp_path, monkeypatch):
    settings = make_agent_settings(tmp_path)
    agent = node_agent.NodeAgent(settings)
    key = agent._fence_key("cam-1", "media")
    agent._fence_state["assignments"][key] = {
        "assignment_id": "pa-1",
        "camera_id": "cam-1",
        "role": "media",
        "generation": 2,
        "lease_expires_at": None,
        "execution_key": "gate-live",
        "fenced": True,
        "revoked": True,
    }

    snapshot = {
        "server_time": FIXED_NOW.isoformat(),
        "node_id": "node-a",
        "assignments": [
            {
                "assignment_id": "pa-1",
                "camera_id": "cam-1",
                "role": "media",
                "generation": 4,
                "lease_expires_at": (
                    FIXED_NOW + timedelta(seconds=60)
                ).isoformat(),
                "execution_key": "gate-live",
            }
        ],
        "revocations": [],
    }

    asyncio.run(agent._apply_fence_snapshot(snapshot))
    current = agent._fence_state["assignments"][key]
    assert current["generation"] == 4
    assert current["revoked"] is False
    assert current["fenced"] is False
    asyncio.run(agent.close())


def test_revocation_is_not_acked_when_tombstone_persistence_fails(tmp_path, monkeypatch):
    settings = make_agent_settings(tmp_path)
    agent = node_agent.NodeAgent(settings)
    acked = []

    async def fake_delete(_role, _execution_key):
        return True

    async def fake_ack(revocation_id):
        acked.append(revocation_id)
        return True

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)
    monkeypatch.setattr(agent, "_ack_revocation", fake_ack)
    monkeypatch.setattr(agent, "_save_fence_state", fail_save)

    snapshot = {
        "server_time": FIXED_NOW.isoformat(),
        "node_id": "node-a",
        "assignments": [],
        "revocations": [
            {
                "revocation_id": "rev-durable",
                "assignment_id": "pa-1",
                "camera_id": "cam-1",
                "role": "media",
                "revoked_generation": 2,
                "execution_key": "gate-live",
                "created_at": FIXED_NOW.isoformat(),
            }
        ],
    }

    with pytest.raises(OSError):
        asyncio.run(agent._apply_fence_snapshot(snapshot))

    assert acked == []
    asyncio.run(agent.close())


class AckSession:
    def __init__(self, revocation, assignment):
        self.revocation = revocation
        self.assignment = assignment

    async def get(self, model, row_id):
        if model is PlacementRevocationEntity and row_id == self.revocation.id:
            return self.revocation
        if model is PlacementAssignmentEntity and row_id == self.assignment.id:
            return self.assignment
        return None


def test_ack_revocation_clears_cleanup_node_only_after_old_owner():
    row = assignment(node_id="node-b", generation=2, role="media")
    row.cleanup_node_ids_json = ["node-a"]
    revocation = PlacementRevocationEntity(
        id="rev-old",
        assignment_id=row.id,
        camera_id=row.camera_id,
        role="media",
        node_id="node-a",
        revoked_generation=1,
        reason="failover",
    )
    session = AckSession(revocation, row)

    acknowledged = asyncio.run(
        fencing.acknowledge_revocation(session, "node-a", "rev-old")
    )

    assert acknowledged is True
    assert revocation.acknowledged_at is not None
    assert row.cleanup_node_ids_json == []


def test_current_owner_cannot_ack_old_revocation_for_itself():
    row = assignment(node_id="node-a", generation=4, role="media")
    revocation = PlacementRevocationEntity(
        id="rev-conflict",
        assignment_id=row.id,
        camera_id=row.camera_id,
        role="media",
        node_id="node-a",
        revoked_generation=1,
        reason="failover",
    )
    session = AckSession(revocation, row)

    with pytest.raises(fencing.FenceConflict):
        asyncio.run(
            fencing.acknowledge_revocation(session, "node-a", "rev-conflict")
        )


def test_delayed_assignment_snapshot_cannot_downgrade_local_generation(tmp_path):
    settings = make_agent_settings(tmp_path)
    agent = node_agent.NodeAgent(settings)
    key = agent._fence_key("cam-1", "media")
    agent._fence_state["assignments"][key] = {
        "assignment_id": "pa-new",
        "camera_id": "cam-1",
        "role": "media",
        "generation": 6,
        "lease_expires_at": (
            FIXED_NOW + timedelta(seconds=60)
        ).isoformat(),
        "execution_key": "gate-live",
        "fenced": False,
        "revoked": False,
    }

    delayed = {
        "server_time": FIXED_NOW.isoformat(),
        "node_id": "node-a",
        "assignments": [
            {
                "assignment_id": "pa-old",
                "camera_id": "cam-1",
                "role": "media",
                "generation": 4,
                "lease_expires_at": (
                    FIXED_NOW + timedelta(seconds=60)
                ).isoformat(),
                "execution_key": "gate-live",
            }
        ],
        "revocations": [],
    }

    asyncio.run(agent._apply_fence_snapshot(delayed))
    assert agent._fence_state["assignments"][key]["generation"] == 6
    asyncio.run(agent.close())


def test_delayed_revocation_cannot_delete_newer_failback_generation(tmp_path, monkeypatch):
    settings = make_agent_settings(tmp_path)
    agent = node_agent.NodeAgent(settings)
    key = agent._fence_key("cam-1", "recording")
    agent._fence_state["assignments"][key] = {
        "assignment_id": "pa-current",
        "camera_id": "cam-1",
        "role": "recording",
        "generation": 8,
        "lease_expires_at": (
            FIXED_NOW + timedelta(seconds=60)
        ).isoformat(),
        "execution_key": "gate-live-record",
        "fenced": False,
        "revoked": False,
    }

    deleted = []
    acked = []

    async def fake_delete(role, execution_key):
        deleted.append((role, execution_key))
        return True

    async def fake_ack(revocation_id):
        acked.append(revocation_id)
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)
    monkeypatch.setattr(agent, "_ack_revocation", fake_ack)

    delayed = {
        "server_time": FIXED_NOW.isoformat(),
        "node_id": "node-a",
        "assignments": [],
        "revocations": [
            {
                "revocation_id": "rev-old",
                "assignment_id": "pa-old",
                "camera_id": "cam-1",
                "role": "recording",
                "revoked_generation": 7,
                "execution_key": "gate-live-record",
                "created_at": FIXED_NOW.isoformat(),
            }
        ],
    }

    asyncio.run(agent._apply_fence_snapshot(delayed))

    assert deleted == []
    assert acked == []
    assert agent._fence_state["assignments"][key]["generation"] == 8
    assert agent._fence_state["assignments"][key]["revoked"] is False
    asyncio.run(agent.close())
