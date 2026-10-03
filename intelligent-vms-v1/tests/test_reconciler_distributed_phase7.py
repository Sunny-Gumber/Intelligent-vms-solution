import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.services import reconciler
from tests.time_control import FIXED_NOW, FrozenDateTime


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    """Freeze reconciler lease checks for deterministic distributed tests."""
    monkeypatch.setattr(reconciler, "datetime", FrozenDateTime)


class FakeClient:
    def __init__(self, configured):
        self.configured = set(configured)
        self.deleted = []

    async def list_config_paths(self):
        return {"items": [{"name": name} for name in sorted(self.configured)]}

    async def delete_path(self, stream_key):
        self.deleted.append(stream_key)
        self.configured.discard(stream_key)


def node(node_id):
    return InfrastructureNodeEntity(
        id=node_id,
        name=node_id,
        region_id="r1",
        roles_json=["recording"],
        endpoints_json={"api_url": f"http://{node_id}:9997"},
        capacity_json={},
        load_json={},
        state="active",
        enabled=True,
        heartbeat_at=FIXED_NOW,
        generation=1,
    )


def test_distributed_recording_cleanup_executes_all_stale_nodes(monkeypatch):
    camera = CameraEntity(
        id="cam-1",
        tenant_id="t",
        site_id="s",
        name="Gate",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="gate-live",
        media_node_id="media-a",
        desired_state="provisioned",
        enabled=True,
    )
    policy = RecordingPolicyEntity(
        id="rp-1",
        camera_id="cam-1",
        enabled=True,
        mode="continuous",
        record_stream_key="gate-live-record",
        recording_node_id="record-c",
        retention_days=7,
        part_duration_ms=1000,
        segment_duration_seconds=900,
        max_part_size_mb=50,
    )
    assignment = PlacementAssignmentEntity(
        id="pa-1",
        camera_id="cam-1",
        role="recording",
        region_id="r1",
        node_id="record-c",
        cleanup_node_ids_json=["record-a", "record-b"],
        generation=3,
        applied_generation=3,
        active=True,
        reason="failover",
        lease_expires_at=FIXED_NOW + timedelta(seconds=60),
        assigned_at=FIXED_NOW,
    )

    clients = {
        "record-a": FakeClient({"gate-live-record"}),
        "record-b": FakeClient({"gate-live-record"}),
        "record-c": FakeClient({"gate-live-record"}),
    }

    async def fake_media(n):
        return clients[n.id]

    monkeypatch.setattr(reconciler.node_clients, "media", fake_media)

    changed, failed, last = asyncio.run(
        reconciler._distributed_reconcile(
            [camera],
            {"cam-1": policy},
            {("cam-1", "recording"): assignment},
            {node_id: node(node_id) for node_id in clients},
            max_changes=10,
        )
    )

    assert failed == 0
    assert last == "cam-1"
    assert assignment.cleanup_node_ids_json == []
    assert clients["record-a"].deleted == ["gate-live-record"]
    assert clients["record-b"].deleted == ["gate-live-record"]
    assert policy.recording_node_id == "record-c"


def test_recording_path_is_reapplied_when_generation_changes(monkeypatch):
    camera = CameraEntity(
        id="cam-failback",
        tenant_id="t",
        site_id="s",
        name="Gate",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="gate-live",
        media_node_id="media-a",
        desired_state="provisioned",
        enabled=True,
    )
    policy = RecordingPolicyEntity(
        id="rp-failback",
        camera_id=camera.id,
        enabled=True,
        mode="continuous",
        record_stream_key="gate-live-record",
        recording_node_id="record-a",
        retention_days=7,
        part_duration_ms=1000,
        segment_duration_seconds=900,
        max_part_size_mb=50,
    )
    assignment = PlacementAssignmentEntity(
        id="pa-failback",
        camera_id=camera.id,
        role="recording",
        region_id="r1",
        node_id="record-a",
        cleanup_node_ids_json=[],
        generation=4,
        applied_generation=1,
        active=True,
        reason="failback",
        lease_expires_at=FIXED_NOW + timedelta(seconds=60),
        assigned_at=FIXED_NOW,
    )
    client = FakeClient({"gate-live-record"})

    async def fake_media(_node):
        return client

    applied = []

    async def fake_provision(_camera, _policy, **kwargs):
        applied.append(
            (kwargs["recording_node_id"], kwargs["assignment_generation"])
        )

    monkeypatch.setattr(reconciler.node_clients, "media", fake_media)
    monkeypatch.setattr(reconciler, "provision_recording", fake_provision)

    changed, failed, last = asyncio.run(
        reconciler._distributed_reconcile(
            [camera],
            {"cam-failback": policy},
            {("cam-failback", "recording"): assignment},
            {"record-a": node("record-a")},
            max_changes=10,
        )
    )

    assert failed == 0
    assert changed == 1
    assert last == "cam-failback"
    assert applied == [("record-a", 4)]
    assert assignment.applied_generation == 4
