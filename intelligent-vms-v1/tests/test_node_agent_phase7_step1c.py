import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).parents[1] / "services" / "node-agent" / "main.py"
SPEC = importlib.util.spec_from_file_location("node_agent_main", MODULE_PATH)
node_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(node_agent)


def make_settings(**overrides):
    payload = {
        "NODE_ID": "node-a",
        "REGION_ID": "region-a",
        "NODE_ROLES": "media",
        "CONTROL_API_URL": "http://control-api:8000",
        "NODE_AGENT_TOKEN": "secret-token",
        "MEDIAMTX_API_URL": "http://mediamtx:9997",
    }
    payload.update(overrides)
    return node_agent.NodeAgentSettings.model_validate(payload)


def test_config_rejects_invalid_control_api_url():
    with pytest.raises(Exception):
        make_settings(CONTROL_API_URL="ftp://bad-url")


def test_config_requires_mediamtx_for_media_or_recording_role():
    with pytest.raises(Exception):
        make_settings(MEDIAMTX_API_URL=None)
    with pytest.raises(Exception):
        make_settings(NODE_ROLES="recording", MEDIAMTX_API_URL=None)


def test_safe_number_normalizes_invalid_values():
    assert node_agent._safe_number(float("nan")) == 0.0
    assert node_agent._safe_number(float("inf")) == 0.0
    assert node_agent._safe_number(-1) == 0.0


def test_backoff_progression_respects_cap_without_jitter(monkeypatch):
    monkeypatch.setattr(node_agent.random, "uniform", lambda *_args: 0.0)
    assert node_agent._next_backoff(1.0, 60.0, 0.2) == 1.0
    assert node_agent._next_backoff(120.0, 60.0, 0.2) == 60.0


def test_backoff_never_returns_zero_under_max_negative_jitter(monkeypatch):
    monkeypatch.setattr(node_agent.random, "uniform", lambda *_args: -1.0)
    value = node_agent._next_backoff(1.0, 60.0, 1.0)
    assert value == node_agent.MIN_BACKOFF_SLEEP_SECONDS
    assert value > 0.0


def test_mediamtx_unreachable_returns_reachable_zero(monkeypatch):
    settings = make_settings()
    agent = node_agent.NodeAgent(settings)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(agent._client, "get", boom)
    result = asyncio.run(agent._probe_mediamtx())
    assert result["mediamtx_reachable"] == 0.0
    assert result["mediamtx_live_sources"] == 0.0
    assert result["mediamtx_recording_paths"] == 0.0
    asyncio.run(agent.close())


def test_heartbeat_payload_contains_placement_and_telemetry_keys(monkeypatch):
    settings = make_settings()
    agent = node_agent.NodeAgent(settings)
    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 8_000_000.0, "net_tx_bps": 4_000_000.0})

    async def fake_probe():
        return {
            "mediamtx_reachable": 1.0,
            "mediamtx_configured_paths": 12.0,
            "mediamtx_live_sources": 10.0,
            "mediamtx_recording_paths": 2.0,
        }

    monkeypatch.setattr(agent, "_probe_mediamtx", fake_probe)
    payload = asyncio.run(agent.build_heartbeat_payload())
    assert "ingress_mbps" in payload["load"]
    assert "egress_mbps" in payload["load"]
    assert "active_sources" in payload["load"]
    assert "mediamtx_reachable" in payload["load"]
    assert payload["observed_at"].endswith("+00:00")
    asyncio.run(agent.close())


def test_log_redaction_hides_token():
    redacted = node_agent._redact("token=abc123", "abc123")
    assert "abc123" not in redacted


def test_unreachable_control_api_uses_bounded_backoff(monkeypatch):
    settings = make_settings(
        HEARTBEAT_BACKOFF_INITIAL_SECONDS=1.0,
        HEARTBEAT_BACKOFF_MAX_SECONDS=2.0,
        HEARTBEAT_BACKOFF_JITTER_RATIO=0.0,
    )
    agent = node_agent.NodeAgent(settings)
    sleeps: list[float] = []

    async def boom(*_args, **_kwargs):
        raise RuntimeError("api down")

    async def stop_sleep(seconds: float):
        sleeps.append(seconds)
        raise StopAsyncIteration

    monkeypatch.setattr(agent, "heartbeat", boom)
    monkeypatch.setattr(node_agent.asyncio, "sleep", stop_sleep)
    with pytest.raises(StopAsyncIteration):
        asyncio.run(agent.run_forever())
    assert sleeps and sleeps[0] <= settings.heartbeat_backoff_max_seconds
    asyncio.run(agent.close())


def test_network_rate_math_is_non_negative(monkeypatch):
    settings = make_settings()
    agent = node_agent.NodeAgent(settings)
    fake_psutil = SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(total=1000, used=500),
        disk_usage=lambda _path: SimpleNamespace(total=1000, free=500),
        cpu_percent=lambda interval=None: 10.0,
    )
    counters = [(100, 200), (90, 190)]

    def fake_net():
        return counters.pop(0)

    times = [10.0, 11.0]
    monkeypatch.setattr(node_agent, "psutil", fake_psutil)
    monkeypatch.setattr(agent, "_network_counters", fake_net)
    monkeypatch.setattr(node_agent.time, "monotonic", lambda: times.pop(0) if times else 11.0)
    agent._measure_host()
    payload = agent._measure_host()
    assert payload["net_rx_bps"] >= 0.0
    assert payload["net_tx_bps"] >= 0.0
    asyncio.run(agent.close())


def test_heartbeat_payload_never_sends_trusted_endpoints(monkeypatch):
    settings = make_settings()
    agent = node_agent.NodeAgent(settings)
    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})

    async def fake_probe():
        return {
            "mediamtx_reachable": 1.0,
            "mediamtx_configured_paths": 0.0,
            "mediamtx_live_sources": 0.0,
            "mediamtx_recording_paths": 0.0,
        }

    monkeypatch.setattr(agent, "_probe_mediamtx", fake_probe)
    payload = asyncio.run(agent.build_heartbeat_payload())
    assert "endpoints" not in payload
    asyncio.run(agent.close())


def test_heartbeat_payload_never_sends_capacity_or_endpoints(monkeypatch):
    settings = make_settings()
    agent = node_agent.NodeAgent(settings)
    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})

    async def fake_probe():
        return {
            "mediamtx_reachable": 1.0,
            "mediamtx_configured_paths": 0.0,
            "mediamtx_live_sources": 0.0,
            "mediamtx_recording_paths": 0.0,
        }

    monkeypatch.setattr(agent, "_probe_mediamtx", fake_probe)
    payload = asyncio.run(agent.build_heartbeat_payload())
    assert "endpoints" not in payload
    assert "capacity" not in payload
    asyncio.run(agent.close())


def test_run_forever_heartbeats_first_and_never_self_registers(monkeypatch):
    settings = make_settings(
        HEARTBEAT_BACKOFF_INITIAL_SECONDS=1.0,
        HEARTBEAT_BACKOFF_MAX_SECONDS=1.0,
        HEARTBEAT_BACKOFF_JITTER_RATIO=0.0,
    )
    agent = node_agent.NodeAgent(settings)
    calls: list[str] = []

    async def heartbeat():
        calls.append("heartbeat")
        return 404

    async def stop_sleep(_seconds: float):
        raise StopAsyncIteration

    monkeypatch.setattr(agent, "heartbeat", heartbeat)
    monkeypatch.setattr(node_agent.asyncio, "sleep", stop_sleep)

    with pytest.raises(StopAsyncIteration):
        asyncio.run(agent.run_forever())

    assert calls == ["heartbeat"]
    assert not hasattr(agent, "upsert")
    asyncio.run(agent.close())


def test_recording_probe_counts_only_record_paths(monkeypatch):
    settings = make_settings(NODE_ROLES="recording")
    agent = node_agent.NodeAgent(settings)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "items": [
                    {"name": "cam-a", "source": {"type": "rtspSource"}},
                    {"name": "cam-a-record", "source": {"type": "rtspSource"}},
                    {"name": "cam-b-record", "source": {"type": "rtspSource"}},
                ]
            }

    async def fake_get(*_args, **_kwargs):
        return Response()

    monkeypatch.setattr(agent._client, "get", fake_get)
    result = asyncio.run(agent._probe_mediamtx())
    assert result["mediamtx_live_sources"] == 1.0
    assert result["mediamtx_recording_paths"] == 2.0
    asyncio.run(agent.close())


def test_ai_role_does_not_invent_placement_load(monkeypatch):
    settings = make_settings(NODE_ROLES="ai", MEDIAMTX_API_URL=None)
    agent = node_agent.NodeAgent(settings)
    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})
    payload = asyncio.run(agent.build_heartbeat_payload())
    assert "ai_mpix_s" not in payload["load"]
    assert "active_ai_jobs" not in payload["load"]
    asyncio.run(agent.close())
