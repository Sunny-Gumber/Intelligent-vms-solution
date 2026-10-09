"""VMS-FIX-015: an unreachable MediaMTX probe is role readiness, not zero load.

Disposable sqlite and in-process fakes only. No live MediaMTX, no cluster.
"""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal
from app.core.config import settings
from app.db.base import Base
from app.models.entities import CameraAIPolicyEntity, CameraEntity, RecordingPolicyEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.models.placement_schemas import NodeHeartbeat
from app.routers.placement import heartbeat_node
from app.services.placement import NodeSnapshot, choose_node, node_eligible
from app.services import placement


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
REGION = "default-region"
COUNT_KEYS = (
    "active_sources",
    "active_recordings",
    "mediamtx_configured_paths",
    "mediamtx_live_sources",
    "mediamtx_recording_paths",
)


def _load_node_agent():
    path = ROOT / "services" / "node-agent" / "main.py"
    spec = importlib.util.spec_from_file_location("node_agent_fix_015", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _agent(node_agent, roles="media,recording"):
    return node_agent.NodeAgent(node_agent.NodeAgentSettings.model_validate({
        "NODE_ID": "node-a",
        "REGION_ID": REGION,
        "NODE_ROLES": roles,
        "CONTROL_API_URL": "http://control-api:8000",
        "NODE_AGENT_TOKEN": "secret-token",
        "MEDIAMTX_API_URL": "http://mediamtx:9997",
    }))


class _Page:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _install_probe(monkeypatch, agent, action):
    async def get(*_args, **_kwargs):
        if isinstance(action, Exception):
            raise action
        return _Page(action)

    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})
    monkeypatch.setattr(agent._client, "get", get)


def _assert_unknown_not_zero(payload, roles):
    readiness = payload["role_readiness"]
    for role in roles:
        assert readiness[role] == "unknown"
        assert readiness[role] != 0
    assert "ai" not in readiness
    load = payload["load"]
    for key in COUNT_KEYS:
        assert key not in load


def test_mediamtx_down_timeout_and_malformed_publish_unknown_not_zero(monkeypatch):
    """A down, timing-out, or malformed probe is unknown, never a count of 0."""
    node_agent = _load_node_agent()
    cases = (
        httpx.ConnectError("connection refused"),
        httpx.TimeoutException("timed out"),
        {"paths": [{"name": "cam"}]},
        ["not-an-object"],
        {"items": [], "itemCount": "0", "pageCount": "0"},
    )
    for action in cases:
        agent = _agent(node_agent)
        _install_probe(monkeypatch, agent, action)
        payload = asyncio.run(agent.build_heartbeat_payload())
        asyncio.run(agent.close())
        _assert_unknown_not_zero(payload, ("media", "recording"))


def test_heartbeat_is_posted_while_mediamtx_is_down(monkeypatch):
    """Liveness is still posted when the media probe cannot answer."""
    node_agent = _load_node_agent()
    agent = _agent(node_agent, roles="media,ai")
    _install_probe(monkeypatch, agent, httpx.ConnectError("down"))
    captured = {}

    async def post(_method, _path, payload):
        captured["payload"] = payload
        return 204

    monkeypatch.setattr(agent, "_request", post)
    status = asyncio.run(agent.heartbeat())
    asyncio.run(agent.close())
    assert status == 204
    _assert_unknown_not_zero(captured["payload"], ("media",))
    assert captured["payload"]["observed_at"]


def test_ai_only_probe_does_not_mark_the_ai_role(monkeypatch):
    """An AI-only node does not invent media readiness or AI load."""
    node_agent = _load_node_agent()
    agent = _agent(node_agent, roles="ai")
    called = []

    async def get(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("AI-only node must not probe MediaMTX")

    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})
    monkeypatch.setattr(agent._client, "get", get)
    payload = asyncio.run(agent.build_heartbeat_payload())
    asyncio.run(agent.close())
    assert called == []
    assert payload["role_readiness"] == {}
    assert "ai" not in payload["role_readiness"]
    assert "active_ai_jobs" not in payload["load"]
    assert "ai_mpix_s" not in payload["load"]


def test_empty_catalog_is_ready_with_a_real_zero(monkeypatch):
    """A complete empty catalog is spare capacity, with an explicit ready role."""
    node_agent = _load_node_agent()
    agent = _agent(node_agent, roles="media")
    _install_probe(monkeypatch, agent, {"itemCount": 0, "pageCount": 0, "items": []})
    payload = asyncio.run(agent.build_heartbeat_payload())
    asyncio.run(agent.close())
    assert payload["role_readiness"] == {"media": "ready"}
    assert payload["load"]["active_sources"] == 0.0


def test_unknown_or_not_ready_is_not_spare_capacity_and_ai_stays_independent():
    """Explicit unready media/recording cannot win on a zero or a leaked count."""
    media_capacity = {"max_ingress_mbps": 1000, "max_egress_mbps": 1000, "max_sources": 1000}
    ai_capacity = {"max_ai_mpix_s": 1000, "max_ai_jobs": 1000}
    zeros = {"ingress_mbps": 0.0, "egress_mbps": 0.0, "active_sources": 0.0}
    measured = {"ingress_mbps": 10.0, "egress_mbps": 10.0, "active_sources": 10.0}

    def snap(node_id, *, roles, capacity, load, readiness=None):
        return NodeSnapshot(
            id=node_id,
            region_id="r1",
            roles=frozenset(roles),
            state="active",
            enabled=True,
            capacity=capacity,
            load=load,
            heartbeat_at=NOW,
            authority_mode="central_online",
            role_readiness=readiness,
        )

    unknown = snap("unknown", roles={"media"}, capacity=media_capacity, load=zeros, readiness={"media": "unknown"})
    not_ready = snap(
        "not-ready",
        roles={"media"},
        capacity=media_capacity,
        load={"ingress_mbps": 1.0, "egress_mbps": 1.0, "active_sources": 4.0},
        readiness={"media": "not_ready"},
    )
    ready_zero = snap("ready-zero", roles={"media"}, capacity=media_capacity, load=zeros, readiness={"media": "ready"})
    busy = snap("busy", roles={"media"}, capacity=media_capacity, load=measured)
    mixed = snap(
        "mixed",
        roles={"media", "ai"},
        capacity={**media_capacity, **ai_capacity},
        load={**zeros, "ai_mpix_s": 1.0, "active_ai_jobs": 1.0},
        readiness={"media": "unknown"},
    )

    assert node_eligible(unknown, "media", "r1", NOW) is False
    assert node_eligible(not_ready, "media", "r1", NOW) is False
    assert choose_node([unknown, not_ready], "media", "r1", NOW) is None
    assert node_eligible(ready_zero, "media", "r1", NOW) is True
    assert choose_node([unknown, busy], "media", "r1", NOW).id == "busy"
    assert node_eligible(mixed, "media", "r1", NOW) is False
    assert node_eligible(mixed, "ai", "r1", NOW) is True


def test_role_readiness_rejects_a_numeric_zero():
    """A count of 0 is not a readiness state."""
    with pytest.raises(ValidationError, match="ready, not_ready, or unknown"):
        NodeHeartbeat(
            load={"active_sources": 0.0},
            role_readiness={"media": "0"},
            observed_at=NOW,
        )


def _utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _admin():
    return Principal("admin", frozenset({"admin"}), "*", frozenset({"*"}))


def _capacity():
    return {
        "max_ingress_mbps": 1000,
        "max_egress_mbps": 1000,
        "max_sources": 1000,
        "max_recordings": 1000,
        "max_ai_mpix_s": 1000,
        "max_ai_jobs": 1000,
    }


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
        media_node_id="node-unready",
        enabled=True,
        desired_state="provisioned",
    )


def _policies(camera_id):
    return [
        RecordingPolicyEntity(
            id=f"rec-{camera_id}",
            camera_id=camera_id,
            enabled=True,
            mode="continuous",
            record_stream_key=f"record-{camera_id}",
        ),
        CameraAIPolicyEntity(
            id=f"ai-{camera_id}",
            camera_id=camera_id,
            enabled=True,
        ),
    ]


def _assignment(camera_id, role, node_id, lease):
    return PlacementAssignmentEntity(
        id=f"pa-{camera_id}-{role}",
        camera_id=camera_id,
        role=role,
        region_id=REGION,
        node_id=node_id,
        cleanup_node_ids_json=[],
        generation=1,
        applied_generation=1,
        active=True,
        reason="initial",
        lease_expires_at=lease,
        autonomy_expires_at=None,
        assigned_at=NOW,
    )


async def _session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fix015.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return factory


async def _send(factory, node_id, payload):
    async with factory() as session:
        return await heartbeat_node(node_id, payload, session, _admin())


async def _rows(factory, model):
    async with factory() as session:
        return list((await session.execute(select(model))).scalars().all())


def test_old_agent_zero_does_not_renew_or_attract_and_ai_stays_independent(tmp_path, monkeypatch):
    """A fresh heartbeat with a zero media/recording count is not spare capacity.

    The payload omits role_readiness, which is the older agent. Media and
    recording leases stay put and new work goes to the measured node. AI on
    the same node still renews and still accepts new work.
    """

    async def scenario():
        factory = await _session_factory(tmp_path)
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        observed = datetime.now(timezone.utc) - timedelta(seconds=1)
        original_lease = datetime.now(timezone.utc) + timedelta(seconds=20)
        roles = ["media", "recording", "ai"]
        zero = {
            "ingress_mbps": 0.0,
            "egress_mbps": 0.0,
            "active_sources": 0.0,
            "active_recordings": 0.0,
            "ai_mpix_s": 1.0,
            "active_ai_jobs": 1.0,
        }
        measured = {
            "ingress_mbps": 10.0,
            "egress_mbps": 10.0,
            "active_sources": 10.0,
            "active_recordings": 4.0,
            "ai_mpix_s": 5.0,
            "active_ai_jobs": 5.0,
        }
        async with factory() as session:
            async with session.begin():
                session.add_all([
                    InfrastructureNodeEntity(
                        id="node-unready",
                        name="node-unready",
                        region_id=REGION,
                        roles_json=roles,
                        state="active",
                        enabled=True,
                        capacity_json=_capacity(),
                        load_json={},
                        heartbeat_at=stale,
                        authority_mode="central_online",
                    ),
                    InfrastructureNodeEntity(
                        id="node-ready",
                        name="node-ready",
                        region_id=REGION,
                        roles_json=roles,
                        state="active",
                        enabled=True,
                        capacity_json=_capacity(),
                        load_json=measured,
                        heartbeat_at=observed,
                        authority_mode="central_online",
                    ),
                    _camera("cam-keep"),
                    _camera("cam-new"),
                    *_policies("cam-keep"),
                    *_policies("cam-new"),
                    *[
                        _assignment("cam-keep", role, "node-unready", original_lease)
                        for role in roles
                    ],
                ])
        fresh = await _send(
            factory,
            "node-unready",
            NodeHeartbeat(load=zero, authority_mode="central_online", observed_at=observed),
        )
        assert _utc(fresh.heartbeat_at) == _utc(observed)
        assert (datetime.now(timezone.utc) - _utc(fresh.heartbeat_at)).total_seconds() < settings.placement_node_stale_seconds
        assert fresh.load["active_sources"] == 0.0

        monkeypatch.setattr(placement, "SessionLocal", factory)
        result = await placement.run_placement_once()
        assignments = {
            (row.camera_id, row.role): row
            for row in await _rows(factory, PlacementAssignmentEntity)
        }
        return result, assignments, original_lease, fresh

    result, assignments, original_lease, fresh = asyncio.run(scenario())
    assert result["renewed"] == 1
    kept_media = assignments[("cam-keep", "media")]
    kept_recording = assignments[("cam-keep", "recording")]
    kept_ai = assignments[("cam-keep", "ai")]
    assert kept_media.node_id == "node-unready"
    assert kept_recording.node_id == "node-unready"
    assert _utc(kept_media.lease_expires_at) == _utc(original_lease)
    assert _utc(kept_recording.lease_expires_at) == _utc(original_lease)
    assert _utc(kept_ai.lease_expires_at) > _utc(original_lease)
    assert assignments[("cam-new", "media")].node_id == "node-ready"
    assert assignments[("cam-new", "recording")].node_id == "node-ready"
    assert assignments[("cam-new", "ai")].node_id == "node-unready"
    assert fresh.role_readiness["media"] == "unknown"
    assert fresh.role_readiness["recording"] == "unknown"
    assert "ai" not in fresh.role_readiness


def test_old_agent_positive_count_still_places_and_explicit_ready_zero_is_spare(tmp_path, monkeypatch):
    """A positive older count still places. An explicit ready zero is spare."""

    async def scenario():
        factory = await _session_factory(tmp_path)
        observed = datetime.now(timezone.utc) - timedelta(seconds=1)
        media_capacity = {
            "max_ingress_mbps": 1000,
            "max_egress_mbps": 1000,
            "max_sources": 1000,
        }
        low = {"ingress_mbps": 1.0, "egress_mbps": 1.0, "active_sources": 1.0}
        high = {"ingress_mbps": 20.0, "egress_mbps": 20.0, "active_sources": 20.0}
        spare = {"ingress_mbps": 0.0, "egress_mbps": 0.0, "active_sources": 0.0}

        def node(node_id):
            return InfrastructureNodeEntity(
                id=node_id,
                name=node_id,
                region_id=REGION,
                roles_json=["media"],
                state="active",
                enabled=True,
                capacity_json=media_capacity,
                load_json={},
                heartbeat_at=observed - timedelta(hours=1),
                authority_mode="central_online",
            )

        async with factory() as session:
            async with session.begin():
                session.add_all([
                    node("node-low"),
                    node("node-high"),
                    _camera("cam-old"),
                ])
        low_read = await _send(
            factory,
            "node-low",
            NodeHeartbeat(load=low, authority_mode="central_online", observed_at=observed),
        )
        await _send(
            factory,
            "node-high",
            NodeHeartbeat(load=high, authority_mode="central_online", observed_at=observed),
        )
        assert low_read.role_readiness is None
        monkeypatch.setattr(placement, "SessionLocal", factory)
        await placement.run_placement_once()

        async with factory() as session:
            async with session.begin():
                session.add_all([node("node-spare"), _camera("cam-spare")])
        spare_read = await _send(
            factory,
            "node-spare",
            NodeHeartbeat(
                load=spare,
                role_readiness={"media": "ready"},
                authority_mode="central_online",
                observed_at=observed,
            ),
        )
        assert spare_read.role_readiness == {"media": "ready"}
        await placement.run_placement_once()
        assignments = {
            (row.camera_id, row.role): row
            for row in await _rows(factory, PlacementAssignmentEntity)
        }
        return assignments

    assignments = asyncio.run(scenario())
    assert assignments[("cam-old", "media")].node_id == "node-low"
    assert assignments[("cam-spare", "media")].node_id == "node-spare"


def test_explicit_unknown_blocks_a_zero_that_would_look_idle(tmp_path, monkeypatch):
    """A new agent that reports unknown is not chosen, even if a count of 0 leaks."""

    async def scenario():
        factory = await _session_factory(tmp_path)
        observed = datetime.now(timezone.utc) - timedelta(seconds=1)
        capacity = {"max_ingress_mbps": 1000, "max_egress_mbps": 1000, "max_sources": 100}
        leaked = {"ingress_mbps": 0.0, "egress_mbps": 0.0, "active_sources": 0.0}
        measured = {"ingress_mbps": 5.0, "egress_mbps": 5.0, "active_sources": 5.0}

        def node(node_id):
            return InfrastructureNodeEntity(
                id=node_id,
                name=node_id,
                region_id=REGION,
                roles_json=["media", "recording"],
                state="active",
                enabled=True,
                capacity_json={**capacity, "max_recordings": 100},
                load_json={},
                heartbeat_at=observed - timedelta(hours=1),
                authority_mode="central_online",
            )

        async with factory() as session:
            async with session.begin():
                session.add_all([
                    node("node-leaked"),
                    node("node-measured"),
                    _camera("cam-leak"),
                    RecordingPolicyEntity(
                        id="rec-cam-leak",
                        camera_id="cam-leak",
                        enabled=True,
                        mode="continuous",
                        record_stream_key="record-cam-leak",
                    ),
                ])
        await _send(
            factory,
            "node-leaked",
            NodeHeartbeat(
                load={**leaked, "active_recordings": 0.0},
                role_readiness={"media": "unknown", "recording": "not_ready"},
                authority_mode="central_online",
                observed_at=observed,
            ),
        )
        await _send(
            factory,
            "node-measured",
            NodeHeartbeat(
                load={**measured, "active_recordings": 2.0},
                role_readiness={"media": "ready", "recording": "ready"},
                authority_mode="central_online",
                observed_at=observed,
            ),
        )
        monkeypatch.setattr(placement, "SessionLocal", factory)
        await placement.run_placement_once()
        assignments = {
            (row.camera_id, row.role): row
            for row in await _rows(factory, PlacementAssignmentEntity)
        }
        return assignments

    assignments = asyncio.run(scenario())
    assert assignments[("cam-leak", "media")].node_id == "node-measured"
    assert assignments[("cam-leak", "recording")].node_id == "node-measured"


def test_role_readiness_migration_is_additive_and_drops_nothing():
    """The schema change adds one nullable column and drops nothing."""
    text = (ROOT / "migrations" / "versions" / "0020_node_role_readiness.py").read_text(encoding="utf-8")
    assert 'revision: str = "0020"' in text
    assert 'down_revision: Union[str, Sequence[str], None] = "0019"' in text
    assert "role_readiness_json" in text
    assert "nullable=True" in text
    lowered = text.lower()
    for forbidden in ("drop_column", "drop_table", "drop_index", "op.drop"):
        assert forbidden not in lowered
