import asyncio
import json
from datetime import datetime, timezone

from app.services.recording_index import RecordingIndexClient


class FakeResponse:
    def __init__(self, lines):
        self.text = "\n".join(json.dumps(line) for line in lines)

    def raise_for_status(self):
        return None


class FakeAsyncClient:
    def __init__(self, *args, lines=None, **kwargs):
        self.lines = lines or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return FakeResponse(self.lines)


def row(segment_id, node, start, duration):
    return {
        "segment_id": segment_id,
        "recording_node_id": node,
        "record_stream_key": "cam-record",
        "segment_path": f"/recordings/{segment_id}.mp4",
        "segment_start": start,
        "duration_seconds": duration,
        "completed_at": start,
        "storage_tier": "hot",
        "object_uri": None,
    }


def test_index_filters_non_overlapping_and_deduplicates(monkeypatch):
    lines = [
        row("old", "node-a", "2026-09-26T00:00:00+00:00", 60),
        row("a", "node-a", "2026-09-26T01:00:00+00:00", 60),
        row("a", "node-a", "2026-09-26T01:00:00+00:00", 60),
        row("b", "node-b", "2026-09-26T01:01:00+00:00", 60),
    ]

    import app.services.recording_index as module

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(lines=lines),
    )

    client = RecordingIndexClient()
    rows = asyncio.run(
        client.segments(
            tenant_id="t",
            site_id="s",
            camera_id="cam",
            start=datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 26, 1, 2, tzinfo=timezone.utc),
        )
    )

    assert [item["segment_id"] for item in rows] == ["a", "b"]
    assert [item["recording_node_id"] for item in rows] == ["node-a", "node-b"]


def test_segment_for_start_returns_historical_owner(monkeypatch):
    lines = [
        row("a", "node-a", "2026-09-26T01:00:00+00:00", 60),
        row("b", "node-b", "2026-09-26T01:01:00+00:00", 60),
    ]

    import app.services.recording_index as module

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(lines=lines),
    )

    client = RecordingIndexClient()
    selected = asyncio.run(
        client.segment_for_start(
            tenant_id="t",
            site_id="s",
            camera_id="cam",
            start=datetime(2026, 9, 26, 1, 1, 30, tzinfo=timezone.utc),
        )
    )

    assert selected is not None
    assert selected["segment_id"] == "b"
    assert selected["recording_node_id"] == "node-b"
