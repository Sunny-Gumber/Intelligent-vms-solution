from pathlib import Path
from datetime import datetime, timedelta, timezone

from app.models.schemas import RecordingTimespan
from app.routers.recordings import _clip_filename, _coverage_is_continuous

ROOT=Path(__file__).parents[1]
ROUTER=(ROOT/"services/control-api/app/routers/recordings.py").read_text(encoding="utf-8")
CONFIG=(ROOT/"services/control-api/app/core/config.py").read_text(encoding="utf-8")
RECORDING=(ROOT/"services/control-api/app/services/recording.py").read_text(encoding="utf-8")
MEDIA=(ROOT/"services/control-api/app/services/mediamtx.py").read_text(encoding="utf-8")
WEB=(ROOT/"web/index.html").read_text(encoding="utf-8")


def export_block():
    shared = ROUTER.split("async def stream_recording_clip",1)[1].split('@router.get("/cameras/{camera_id}/export")',1)[0]
    route = ROUTER.split('@router.get("/cameras/{camera_id}/export")',1)[1].split("def _parse_duration",1)[0]
    return shared + route


def test_export_is_authorized_operator_camera_operation():
    b=export_block()
    assert 'authorized_camera(session, camera_id, principal)' in b
    assert 'require_roles("admin", "operator")' in b
    assert 'policy.record_stream_key' in b
    assert 'rtsp://' not in b.lower() and 'file://' not in b.lower()
    assert 'segment_path' not in b and 'filesystem' not in b.lower()


def test_export_time_and_duration_are_bounded_and_future_rejected():
    b=export_block()
    assert 'start.tzinfo is None' in b
    assert 'recording_export_max_duration_seconds' in b
    assert 'start > now or end > now' in b
    assert 'recording_export_max_duration_seconds: int = Field(default=900' in CONFIG


def test_export_requires_continuous_recording_coverage():
    b=export_block()
    assert '_coverage_is_continuous(spans, start, end)' in b
    assert 'does not have continuous recording coverage' in b
    assert 'does not have continuous finalized recording coverage' in b
    helper=ROUTER.split('def _coverage_is_continuous',1)[1].split('def _clip_filename',1)[0]
    assert 'if span.start > cursor' in helper
    assert 'return False' in helper


def test_distributed_export_rejects_cross_node_clip():
    b=export_block()
    assert 'nodes = {str(row["recording_node_id"])' in b
    assert 'if len(nodes) != 1' in b
    assert 'crosses a recording-node boundary' in b
    assert 'get_node(session, next(iter(nodes)), required_role="recording")' in b


def test_export_uses_mp4_playback_not_ffmpeg_or_second_ingest():
    b=export_block()
    assert 'playback.open_stream(' in b
    assert '"mp4"' in b
    assert 'recording_source(' not in b
    assert 'provision_recording(' not in b
    assert 'ffmpeg' not in b.lower()
    assert 'subprocess' not in b.lower()
    assert 'shell' not in b.lower()


def test_export_concurrency_slot_is_bounded_and_released():
    b=export_block()
    assert 'asyncio.wait_for(_export_slots.acquire(), timeout=0.05)' in b
    assert 'recording_export_max_concurrent_per_process: int = Field(default=4' in CONFIG
    assert b.count('_export_slots.release()') >= 3
    assert 'Recording export concurrency limit reached' in b
    assert 'settings.recording_export_io_timeout_seconds' in b


def test_download_headers_are_attachment_safe_and_no_internal_path_is_exposed():
    b=export_block()
    assert 'Content-Disposition' in b
    assert '_clip_filename(camera.id, start, end)' in b
    assert 'X-Content-Type-Options' in b and 'nosniff' in b
    assert 'private, no-store' in b
    fn=ROUTER.split('def _clip_filename',1)[1].split('@router.get',1)[0]
    assert 're.sub(r"[^A-Za-z0-9_-]", "_", camera_id)' in fn
    assert 'strftime("%Y%m%dT%H%M%SZ")' in fn
    assert 'camera.name' not in fn


def test_recording_invariants_are_not_mutated_by_export():
    b=export_block()
    assert 'put_policy(' not in b and 'provision_recording(' not in b and 'delete_path(' not in b
    assert 'recording_source(camera)' in RECORDING
    rp=MEDIA.split('async def add_or_replace_recording_path',1)[1].split('async def delete_path',1)[0]
    assert '"record": True' in rp and '"sourceOnDemand": False' in rp
    assert '"maxReaders"' not in rp


def test_live_ai_and_snapshot_remain_independent():
    b=export_block()
    assert '/live/' not in b and '/ai/' not in b
    assert 'liveWorkspace.roles' not in b
    assert 'snapshotTile' in WEB and 'image/png' in WEB
    assert 'downloadSelectedClip' in WEB
    assert '/recordings/cameras/${encodeURIComponent(p.cameraId)}/export' in WEB


def test_ui_exports_selected_recorded_span_not_active_live_role():
    assert 'playbackSelection={cameraId:id,start:s.start,duration:s.duration}' in WEB
    fn=WEB.split('function downloadSelectedClip()',1)[1].split('function closePlayback',1)[0]
    assert 'playbackSelection' in fn
    assert 'liveWorkspace.roles' not in fn
    assert 'stream_role' not in fn


def test_coverage_helper_accepts_contiguous_multisegment_and_rejects_gap():
    start=datetime(2026,10,1,8,0,tzinfo=timezone.utc)
    a=RecordingTimespan(start=start,duration=30,end=start+timedelta(seconds=30))
    b=RecordingTimespan(start=start+timedelta(seconds=30),duration=30,end=start+timedelta(seconds=60))
    assert _coverage_is_continuous([b,a],start,start+timedelta(seconds=60))
    gap=RecordingTimespan(start=start+timedelta(seconds=31),duration=29,end=start+timedelta(seconds=60))
    assert not _coverage_is_continuous([a,gap],start,start+timedelta(seconds=60))


def test_clip_filename_sanitizes_camera_id_and_uses_utc_bounds():
    start=datetime(2026,10,1,8,0,tzinfo=timezone.utc)
    end=start+timedelta(seconds=60)
    name=_clip_filename("../camera/secret\r\nX-Bad: yes",start,end)
    assert name == "clip-___camera_secret__X-Bad__yes-20261001T080000Z-20261001T080100Z.mp4"
    assert "/" not in name and "\r" not in name and "\n" not in name


def test_export_does_not_require_recording_to_still_be_enabled():
    b=export_block()
    assert "not policy.enabled" not in b
    assert 'if not policy:' in b
