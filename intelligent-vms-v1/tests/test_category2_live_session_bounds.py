import asyncio
from pathlib import Path

from app.core.config import settings
from app.services.mediamtx import MediaMTXClient
from app.services.reconciler import _live_path_needs_apply


def test_live_path_has_nonzero_configurable_reader_ceiling(monkeypatch):
    captured = {}

    async def fake_upsert(stream_key, payload):
        captured["stream_key"] = stream_key
        captured["payload"] = payload

    monkeypatch.setattr(settings, "live_view_max_readers_per_path", 3)
    client = MediaMTXClient("http://media.invalid")
    monkeypatch.setattr(client, "_upsert", fake_upsert)

    asyncio.run(client.add_or_replace_path("camera-live", "rtsp://10.0.0.2/sub"))

    assert captured["stream_key"] == "camera-live"
    assert captured["payload"]["sourceOnDemand"] is True
    assert captured["payload"]["record"] is False
    assert captured["payload"]["maxReaders"] == 3


def test_recording_path_does_not_inherit_live_reader_ceiling(monkeypatch):
    captured = {}

    async def fake_upsert(stream_key, payload):
        captured["payload"] = payload

    monkeypatch.setattr(settings, "live_view_max_readers_per_path", 1)
    client = MediaMTXClient("http://media.invalid")
    monkeypatch.setattr(client, "_upsert", fake_upsert)

    asyncio.run(
        client.add_or_replace_recording_path(
            "camera-record",
            "rtsp://10.0.0.2/main",
            retention_days=7,
            part_duration_ms=1000,
            segment_duration_seconds=900,
            max_part_size_mb=50,
        )
    )

    assert captured["payload"]["sourceOnDemand"] is False
    assert captured["payload"]["record"] is True
    assert "maxReaders" not in captured["payload"]


def test_live_reader_default_is_bounded_and_documented():
    assert settings.live_view_max_readers_per_path > 0
    root = Path(__file__).parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")
    policy = (root / "docs" / "security" / "LIVE_VIEW_SESSION_RESOURCE_POLICY.md").read_text(
        encoding="utf-8"
    )
    assert "LIVE_VIEW_MAX_READERS_PER_PATH=16" in example
    assert "safety" in policy.lower()
    assert "capacity" in policy.lower()


def test_live_resource_policy_keeps_recording_contract_separate():
    root = Path(__file__).parents[1]
    media = (
        root / "services" / "control-api" / "app" / "services" / "mediamtx.py"
    ).read_text(encoding="utf-8")
    assert '"maxReaders": settings.live_view_max_readers_per_path' in media
    recording_section = media.split("async def add_or_replace_recording_path", 1)[1]
    recording_payload = recording_section.split("async def delete_path", 1)[0]
    assert '"maxReaders"' not in recording_payload



def test_existing_live_path_is_reapplied_when_reader_policy_is_stale(monkeypatch):
    monkeypatch.setattr(settings, "live_view_max_readers_per_path", 4)

    assert _live_path_needs_apply({"camera-live"}, {"camera-live": 0}, "camera-live") is True
    assert _live_path_needs_apply({"camera-live"}, {"camera-live": 4}, "camera-live") is False
    assert _live_path_needs_apply(set(), {}, "camera-live") is True
