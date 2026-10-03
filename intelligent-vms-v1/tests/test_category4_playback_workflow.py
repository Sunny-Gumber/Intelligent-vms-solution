from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models.schemas import RecordingTimespan
from app.routers.recordings import _bounded_playback_duration, _playback_timespans


ROOT = Path(__file__).parents[1]
WEB = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
ROUTER = (
    ROOT / "services" / "control-api" / "app" / "routers" / "recordings.py"
).read_text(encoding="utf-8")
PLAYBACK = (
    ROOT / "services" / "control-api" / "app" / "services" / "playback.py"
).read_text(encoding="utf-8")


def playback_route() -> str:
    return ROUTER.split('@router.get("/cameras/{camera_id}/play")', 1)[1].split(
        "def _coverage_is_continuous", 1
    )[0]


def web_playback_block() -> str:
    return WEB.split("function localDateValue", 1)[1].split("function esc(s)", 1)[0]


def test_catalog_scoped_workflow_is_reachable_from_active_camera():
    assert 'id="tile-playback-${i}"' in WEB
    assert "openPlaybackForTile(${i})" in WEB
    assert 'id="pbDate" type="date"' in WEB
    assert 'id="pbTime" type="time"' in WEB
    assert 'id="pbLoad"' in WEB
    assert 'id="timeline"' in WEB
    assert 'id="pbPlay"' in WEB
    assert 'id="pbPause"' in WEB
    assert 'id="pbClipStart"' in WEB
    assert 'id="pbClipEnd"' in WEB
    assert 'id="pbexport"' in WEB
    assert "function returnToLive(){closePlayback(true)}" in WEB


def test_timeline_queries_are_timezone_aware_bounded_and_generation_fenced():
    block = web_playback_block()
    assert "window.start.toISOString()" in block
    assert "window.end.toISOString()" in block
    assert "generation=++playbackState.generation" in block
    assert "generation!==playbackState.generation" in block
    assert "cameraId!==activeCameraId()" in block

    timeline = ROUTER.split(
        '@router.get("/cameras/{camera_id}/timeline"', 1
    )[1].split('@router.get("/cameras/{camera_id}/play")', 1)[0]
    assert "await authorized_camera(session, camera_id, principal)" in timeline
    assert "start.tzinfo is None or end.tzinfo is None" in timeline
    assert "recording_query_max_window_hours" in timeline
    assert "recording_query_max_segments" in timeline


def test_playback_timespans_reject_malformed_and_clip_to_requested_window():
    start = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=10)
    items = [
        {"start": (start - timedelta(minutes=1)).isoformat(), "duration": 120},
        {"start": "2026-10-02T08:05:00", "duration": 30},
        {"start": start.isoformat(), "duration": -1},
        {"start": "bad", "duration": 10},
    ]
    spans = _playback_timespans(items, start, end)
    assert len(spans) == 1
    assert spans[0].start == start
    assert spans[0].end == start + timedelta(minutes=1)


def test_playback_never_bridges_a_recording_gap():
    start = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    first = RecordingTimespan(
        start=start,
        duration=30,
        end=start + timedelta(seconds=30),
    )
    second = RecordingTimespan(
        start=start + timedelta(seconds=35),
        duration=30,
        end=start + timedelta(seconds=65),
    )
    assert _bounded_playback_duration([first, second], start, 60) == 30
    assert _bounded_playback_duration([second], start, 20) is None


def test_playback_can_cross_only_contiguous_local_spans():
    start = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    first = RecordingTimespan(
        start=start,
        duration=30,
        end=start + timedelta(seconds=30),
    )
    second = RecordingTimespan(
        start=start + timedelta(seconds=30),
        duration=30,
        end=start + timedelta(seconds=60),
    )
    assert _bounded_playback_duration([second, first], start, 50) == 50


def test_play_route_reauthorizes_camera_and_resolves_server_side_recording_source():
    block = playback_route()
    assert 'require_roles("admin", "operator", "viewer")' in block
    assert "camera = await authorized_camera(session, camera_id, principal)" in block
    assert "_policy_for_camera(session, camera_id)" in block
    assert "policy.record_stream_key" in block
    assert "recording_index.segment_for_start(" in block
    assert "get_node(" in block
    assert "node_clients.playback(node)" in block
    assert "path: str" not in block
    assert "url: str" not in block
    assert "rtsp://" not in block.lower()
    assert "file://" not in block.lower()


def test_play_route_rejects_future_and_gap_start_and_bounds_node_playback():
    block = playback_route()
    assert "start.tzinfo is None" in block
    assert "playback start must not be in the future" in block
    assert "No recording is available at requested start" in block
    assert "_bounded_playback_duration(" in block
    assert "segment_end - start" in block
    assert "duration: float = Query(..., gt=0.0, le=14400.0)" in block


def test_range_seek_and_stream_cleanup_remain_bounded():
    block = playback_route()
    assert 'request.headers.get("range")' in block
    assert "async for chunk in upstream.aiter_bytes()" in block
    assert "await upstream.aclose()" in block
    assert "await client.aclose()" in block
    assert "Range" in PLAYBACK
    assert "await client.aclose()" in PLAYBACK


def test_ui_handles_no_recording_multiple_spans_gaps_and_rapid_selection():
    block = web_playback_block()
    assert "No recording available on this date." in block
    assert "recorded span(s) available on this date." in block
    assert "No recording at this timeline position." in block
    assert "selectPlaybackSpan(id,s,new Date(requestedMs))" in block
    assert "playbackState.playGeneration" in block
    assert "resetPlaybackPlayer()" in block


def test_ui_seek_stays_inside_selected_recording_span():
    block = web_playback_block()
    assert "selectedMs=Math.min(end.getTime()-1,Math.max(start.getTime(),requested.getTime()))" in block
    assert "duration=Math.min(PLAYBACK_MAX_SECONDS" in block
    assert "playbackSelection.spanStart=start.toISOString()" in block
    assert "playbackSelection.spanEnd=end.toISOString()" in block
    assert 'step="0.001"' in WEB
    assert "date.getMilliseconds()" in block
    assert "controls playsinline" in WEB
    assert 'ontimeupdate="updatePlaybackClock()"' in WEB


def test_selected_clip_uses_existing_authorized_export_without_path_or_role_injection():
    block = web_playback_block()
    assert "start>=spanStart&&end<=spanEnd&&end>start" in block
    assert "const p=playbackSelection" in block
    assert "/recordings/cameras/${encodeURIComponent(p.cameraId)}/export" in block
    assert "start=${encodeURIComponent(start.toISOString())}" in block
    assert "duration=${encodeURIComponent(duration)}" in block
    assert "record_stream_key" not in block
    assert "recording_node_id" not in block
    assert "liveWorkspace.roles" not in block


def test_active_camera_change_and_page_exit_cleanup_playback_without_stopping_live():
    assert "syncPlaybackToActiveCamera()" in WEB
    assert "if(playbackState.cameraId&&!authorizedIds.has(playbackState.cameraId))closePlayback(false)" in WEB
    assert "playbackState.generation++;playbackState.playGeneration++" in WEB
    assert "player.removeAttribute('src');player.load()" in WEB
    assert "window.addEventListener('pagehide',()=>{closePlayback(false)})" in WEB
    assert "window.addEventListener('pagehide',()=>{stopLiveSessions()})" in WEB
    close_block = web_playback_block().split("function closePlayback", 1)[1].split(
        "function returnToLive", 1
    )[0]
    assert "cleanupTile(" not in close_block
    assert "stopLiveSessions(" not in close_block


def test_playback_does_not_mutate_recording_retention_live_ai_snapshot_or_manual_state():
    block = playback_route() + web_playback_block()
    for forbidden in (
        "put_policy(",
        "provision_recording(",
        "delete_path(",
        "saveRecordingPolicy(",
        "/ai/",
        "/manual-recordings/",
        "snapshotTile(",
        "switchRole(",
    ):
        assert forbidden not in block


def test_browser_playback_adds_no_posix_or_shell_assumptions():
    block = web_playback_block()
    assert "/recordings/%path" not in block
    assert "/var/lib/" not in block
    assert "file://" not in block.lower()
    assert "ffmpeg" not in block.lower()
    assert "subprocess" not in block.lower()
    assert "shell" not in block.lower()
