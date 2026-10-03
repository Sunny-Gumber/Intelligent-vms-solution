import asyncio

from app.core.config import settings
from app.services.mediamtx import MediaMTXClient


def test_recording_hook_contains_assigned_node_id():
    client = MediaMTXClient("http://media-a:9997")
    captured = {}

    async def fake_upsert(stream_key, payload):
        captured["stream_key"] = stream_key
        captured["payload"] = payload

    client._upsert = fake_upsert
    asyncio.run(
        client.add_or_replace_recording_path(
            "cam-record",
            "rtsp://10.0.0.10/main",
            retention_days=7,
            part_duration_ms=1000,
            segment_duration_seconds=900,
            max_part_size_mb=50,
            recording_node_id="recording-a",
            assignment_generation=7,
        )
    )

    hook = captured["payload"]["runOnRecordSegmentComplete"]
    assert "recording_node_id=recording-a" in hook
    assert "assignment_generation=7" in hook
    assert settings.recording_hook_callback_url in hook
