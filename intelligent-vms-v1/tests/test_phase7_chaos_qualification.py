import asyncio
import importlib.util
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.services import reconciler
from app.services.events import EventPublisherUnavailable
from app.services.outbox import OutboxMessage, OutboxWorker
from tests.time_control import FIXED_NOW, FrozenDateTime


SPOOL_MODULE_PATH = (
    Path(__file__).parents[1] / "services" / "regional-spool" / "main.py"
)


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    """Freeze reconciler lease checks used by deterministic chaos scenarios."""
    monkeypatch.setattr(reconciler, "datetime", FrozenDateTime)


def _recording_node(node_id: str) -> InfrastructureNodeEntity:
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
        authority_mode="central_online",
        generation=1,
    )


class _Client:
    def __init__(self, configured=()):
        self.configured = set(configured)
        self.deleted = []

    async def list_config_paths(self):
        return {"items": [{"name": name} for name in sorted(self.configured)]}

    async def delete_path(self, stream_key):
        self.deleted.append(stream_key)
        self.configured.discard(stream_key)


def test_temporary_postgres_failure_does_not_kill_outbox_worker(monkeypatch):
    worker = OutboxWorker()
    calls = []

    async def flaky_run_once():
        calls.append("run")
        if len(calls) == 1:
            # claim_batch/session failure is represented by an arbitrary DB error.
            raise RuntimeError("postgres temporarily unavailable")
        worker._stop.set()
        return {
            "claimed": 0,
            "published": 0,
            "retried": 0,
            "dead": 0,
            "lost_claim": 0,
        }

    async def no_cleanup():
        return 0

    async def immediate_wait(_awaitable, timeout):
        # Consume the Event.wait coroutine so Python does not warn.
        try:
            _awaitable.close()
        except AttributeError:
            pass
        return None

    monkeypatch.setattr(worker, "run_once", flaky_run_once)
    monkeypatch.setattr("app.services.outbox.cleanup_outbox", no_cleanup)
    monkeypatch.setattr("app.services.outbox.asyncio.wait_for", immediate_wait)

    asyncio.run(worker._run())

    assert calls == ["run", "run"]


def test_kafka_outage_then_recovery_retries_same_outbox_message(monkeypatch):
    message = OutboxMessage(
        id="event:event-chaos",
        topic="vms.events.v1",
        key_text="tenant-a:cam-1",
        payload={
            "event_id": "event-chaos",
            "tenant_id": "tenant-a",
            "site_id": "site-a",
            "camera_id": "cam-1",
            "timestamp": FIXED_NOW.isoformat(),
            "event_type": "camera_offline",
            "source": "vms",
        },
        attempts=0,
        claim_token="claim-1",
    )
    state = {"cycle": 0, "failed": 0, "delivered": 0}

    async def claim_batch():
        state["cycle"] += 1
        return [message] if state["cycle"] <= 2 else []

    async def renew_claim(_message):
        return True

    async def publish(*_args, **_kwargs):
        if state["cycle"] == 1:
            raise EventPublisherUnavailable("kafka down")

    async def mark_failed(_message, _error, *, allow_dead_letter):
        assert allow_dead_letter is False
        state["failed"] += 1
        return "retry"

    async def mark_delivered(_message):
        state["delivered"] += 1
        return True

    monkeypatch.setattr("app.services.outbox.claim_batch", claim_batch)
    monkeypatch.setattr("app.services.outbox.renew_claim", renew_claim)
    monkeypatch.setattr("app.services.outbox.event_publisher.publish_to", publish)
    monkeypatch.setattr("app.services.outbox.mark_failed", mark_failed)
    monkeypatch.setattr("app.services.outbox.mark_delivered", mark_delivered)

    first = asyncio.run(OutboxWorker().run_once())
    second = asyncio.run(OutboxWorker().run_once())

    assert first["retried"] == 1
    assert first["dead"] == 0
    assert second["published"] == 1
    assert state == {"cycle": 2, "failed": 1, "delivered": 1}


def test_unreachable_old_recording_node_keeps_cleanup_obligation_for_retry(monkeypatch):
    camera = CameraEntity(
        id="cam-1",
        tenant_id="tenant-a",
        site_id="site-a",
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
        camera_id=camera.id,
        enabled=True,
        mode="continuous",
        record_stream_key="gate-live-record",
        recording_node_id="record-b",
        retention_days=7,
        part_duration_ms=1000,
        segment_duration_seconds=900,
        max_part_size_mb=50,
    )
    assignment = PlacementAssignmentEntity(
        id="pa-1",
        camera_id=camera.id,
        role="recording",
        region_id="r1",
        node_id="record-b",
        cleanup_node_ids_json=["record-a"],
        generation=2,
        applied_generation=2,
        active=True,
        reason="failover",
        lease_expires_at=FIXED_NOW + timedelta(seconds=60),
        assigned_at=FIXED_NOW,
    )

    current = _Client({"gate-live-record"})
    old = _Client({"gate-live-record"})
    old_reachable = {"value": False}

    async def media(node):
        if node.id == "record-a" and not old_reachable["value"]:
            raise RuntimeError("old node offline")
        return old if node.id == "record-a" else current

    monkeypatch.setattr(reconciler.node_clients, "media", media)
    nodes = {
        "record-a": _recording_node("record-a"),
        "record-b": _recording_node("record-b"),
    }

    changed, failed, _ = asyncio.run(
        reconciler._distributed_reconcile(
            [camera],
            {camera.id: policy},
            {(camera.id, "recording"): assignment},
            nodes,
            max_changes=10,
        )
    )
    assert assignment.cleanup_node_ids_json == ["record-a"]
    assert current.deleted == []
    assert old.deleted == []
    assert failed >= 1

    old_reachable["value"] = True
    changed2, failed2, _ = asyncio.run(
        reconciler._distributed_reconcile(
            [camera],
            {camera.id: policy},
            {(camera.id, "recording"): assignment},
            nodes,
            max_changes=10,
        )
    )
    assert failed2 == 0
    assert assignment.cleanup_node_ids_json == []
    assert old.deleted == ["gate-live-record"]
    assert changed2 >= 1


def _load_spool(tmp_path, monkeypatch):
    db = tmp_path / "chaos-spool.db"
    monkeypatch.setenv("CONTROL_API_URL", "http://control-api:8000")
    monkeypatch.setenv("NODE_ID", "node-a")
    monkeypatch.setenv("NODE_AGENT_TOKEN", "node-secret")
    monkeypatch.setenv("RECORDING_HOOK_TOKEN", "record-secret")
    monkeypatch.setenv("REGIONAL_SPOOL_TOKEN", "spool-secret")
    monkeypatch.setenv("SPOOL_DB_PATH", str(db))
    monkeypatch.setenv("SPOOL_MAX_ITEMS", "100")
    monkeypatch.setenv("SPOOL_BATCH_SIZE", "100")
    monkeypatch.setenv("SPOOL_BACKOFF_MAX_SECONDS", "5")
    name = "regional_spool_chaos_test"
    spec = importlib.util.spec_from_file_location(name, SPOOL_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "time", lambda: FIXED_NOW.timestamp())
    monkeypatch.setattr(module.random, "uniform", lambda *_args: 0.0)
    return module


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _StormClient:
    def __init__(self):
        self.fail = True
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise RuntimeError("wan flap")
        return _Response(202)


def test_reconnect_storm_backlog_drains_without_duplicate_spool_items(tmp_path, monkeypatch):
    mod = _load_spool(tmp_path, monkeypatch)
    client = _StormClient()

    for i in range(25):
        body = {"event_id": f"event-{i}", "camera_id": "cam-1"}
        mod.store.enqueue(f"event:event-{i}", "event", body)
        # Repeated source retry must not create a duplicate queue row.
        mod.store.enqueue(f"event:event-{i}", "event", body)

    assert mod.store.counts() == (25, 0)

    # Repeated WAN failures retain all items and only advance retry metadata.
    for _ in range(3):
        asyncio.run(mod.flush_once(client))
        with sqlite3.connect(tmp_path / "chaos-spool.db") as db:
            db.execute("UPDATE spool_items SET next_attempt_at=0")
            db.commit()
        assert mod.store.counts() == (25, 0)

    client.fail = False
    delivered = asyncio.run(mod.flush_once(client))

    assert delivered == 25
    assert mod.store.counts() == (0, 0)
