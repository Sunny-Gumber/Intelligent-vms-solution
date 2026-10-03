import asyncio
import logging
from pathlib import Path
from datetime import timezone

import pytest
from pydantic import ValidationError

from app.core.security import encrypt_secret
from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.models.schemas import RecordingPolicyUpdate
from app.routers.recordings import _cleanup_created_recording_path, _parse_duration, _segment_start
from app.services.mediamtx import MediaMTXClient
from app.services.recording import make_record_stream_key, recording_source
from app.services.stream_keys import make_role_stream_key
import app.services.reconciler as reconciler


def camera():
    return CameraEntity(
        id="camera-1",
        tenant_id="tenant-a",
        site_id="site-a",
        name="Gate",
        host="10.20.30.40",
        rtsp_port=554,
        main_path="/main?channel=1",
        sub_path="/sub?channel=1",
        username_enc=encrypt_secret("admin"),
        password_enc=encrypt_secret("p@ss word"),
        stream_key="site-a-gate-1234",
        media_node_id="media-local-01",
        enabled=True,
        desired_state="provisioned",
    )


def policy(cam):
    return RecordingPolicyEntity(
        id="policy-1",
        camera_id=cam.id,
        mode="continuous",
        enabled=True,
        record_stream_key=make_record_stream_key(cam.stream_key),
        recording_node_id=cam.media_node_id,
        retention_days=7,
        part_duration_ms=1000,
        segment_duration_seconds=900,
        max_part_size_mb=50,
    )


def test_recording_uses_main_stream_not_live_substream():
    source = recording_source(camera())
    assert source == "rtsp://admin:p%40ss%20word@10.20.30.40:554/main?channel=1"
    assert "/sub" not in source


def test_record_stream_key_is_deterministic():
    assert make_record_stream_key("abc") == "abc-record"


def test_policy_bounds_reject_dangerous_values():
    with pytest.raises(ValidationError):
        RecordingPolicyUpdate(retention_days=0)
    with pytest.raises(ValidationError):
        RecordingPolicyUpdate(segment_duration_seconds=5)
    with pytest.raises(ValidationError):
        RecordingPolicyUpdate(part_duration_ms=50)


def test_mediamtx_continuous_record_path_is_always_on(monkeypatch):
    captured = {}

    async def fake_upsert(stream_key, payload):
        captured["stream_key"] = stream_key
        captured["payload"] = payload

    client = MediaMTXClient("http://media.invalid")
    monkeypatch.setattr(client, "_upsert", fake_upsert)
    asyncio.run(
        client.add_or_replace_recording_path(
            "cam-record",
            "rtsp://10.0.0.2/main",
            retention_days=7,
            part_duration_ms=1000,
            segment_duration_seconds=900,
            max_part_size_mb=50,
        )
    )
    p = captured["payload"]
    assert p["sourceOnDemand"] is False
    assert p["record"] is True
    assert p["recordFormat"] == "fmp4"
    assert p["recordPartDuration"] == "1000ms"
    assert p["recordSegmentDuration"] == "900s"
    assert p["recordDeleteAfter"] == "168h"
    assert "X-Recording-Hook-Token" in p["runOnRecordSegmentComplete"]


def test_hook_duration_and_epoch_segment_start():
    assert _parse_duration("15m0s") == 900
    assert _parse_duration("1.5s") == 1.5
    start = _segment_start("/recordings/cam/2026/09/23/18/1789999999-123456.mp4", 900)
    assert int(start.timestamp()) == 1789999999
    assert start.tzinfo == timezone.utc


def test_reconciler_restores_record_path(monkeypatch):
    cam = camera()
    pol = policy(cam)

    class FakeMedia:
        async def add_or_replace_path(self, *_args, **_kwargs):
            raise AssertionError("live path should already be present")

    called = []

    async def fake_record(camera_arg, policy_arg):
        called.append((camera_arg.id, policy_arg.record_stream_key))

    monkeypatch.setattr(reconciler, "mediamtx", FakeMedia())
    monkeypatch.setattr(reconciler, "provision_recording", fake_record)

    changed, failed = asyncio.run(
        reconciler.reconcile_batch(
            [cam],
            {cam.stream_key, make_role_stream_key(cam.stream_key, "main")},
            {cam.id: pol},
        )
    )
    assert (changed, failed) == (1, 0)
    assert called == [(cam.id, pol.record_stream_key)]


def test_failed_recording_cleanup_is_logged_without_hiding_original_failure(monkeypatch, caplog):
    """Make best-effort rollback cleanup failure observable to operators."""
    async def fail_delete(_stream_key):
        raise RuntimeError("endpoint-secret-must-not-be-logged")

    monkeypatch.setattr("app.routers.recordings.mediamtx.delete_path", fail_delete)
    with caplog.at_level(logging.WARNING, logger="app.routers.recordings"):
        asyncio.run(_cleanup_created_recording_path("camera-1", "camera-1-record"))

    assert "recording_policy_cleanup_failed" in caplog.text
    assert "camera-1" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "endpoint-secret-must-not-be-logged" not in caplog.text


def test_recording_policy_cleanup_uses_pre_rollback_scalar_snapshot():
    source = (Path(__file__).parents[1] / "services/control-api/app/routers/recordings.py").read_text(encoding="utf-8")
    block = source.split("async def put_policy(", 1)[1].split("@router.get", 1)[0]
    snapshot = block.index("cleanup_record_stream_key = row.record_stream_key")
    rollback = block.index("await session.rollback()")
    cleanup = block.index("_cleanup_created_recording_path(cleanup_camera_id, cleanup_record_stream_key)")
    assert snapshot < rollback < cleanup
    after_rollback = block[rollback:cleanup]
    assert "row.record_stream_key" not in after_rollback


def test_segment_start_accepts_mediamtx_required_microsecond_suffix():
    start = _segment_start("/recordings/cam/2026/09/23/18/1789999999-000001.mp4", 1)
    assert int(start.timestamp()) == 1789999999
