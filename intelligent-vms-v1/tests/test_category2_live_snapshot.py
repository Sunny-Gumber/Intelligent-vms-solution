from pathlib import Path

ROOT = Path(__file__).parents[1]
WEB = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
RECORDINGS = (ROOT / "services" / "control-api" / "app" / "routers" / "recordings.py").read_text(encoding="utf-8")
RECORDING = (ROOT / "services" / "control-api" / "app" / "services" / "recording.py").read_text(encoding="utf-8")
MEDIA = (ROOT / "services" / "control-api" / "app" / "services" / "mediamtx.py").read_text(encoding="utf-8")


def snapshot_block():
    return WEB.split("async function snapshotTile(tileIndex)", 1)[1].split("function defaultRole", 1)[0]


def test_snapshot_is_active_tile_current_session_only():
    block = snapshot_block()
    assert "tileIndex!==liveWorkspace.activeTile" in block
    assert "session.cameraId!==cameraId" in block
    assert "session.role!==role" in block
    assert "session.generation!==generation" in block
    assert "session.state!=='LIVE'" in block
    assert "video.readyState<2" in block


def test_snapshot_captures_current_viewed_role_without_new_media_reader():
    block = snapshot_block()
    assert "liveWorkspace.roles[tileIndex]" in block
    assert "context.drawImage(video,0,0,canvas.width,canvas.height)" in block
    assert "image/png" in block
    assert "liveAccess(" not in block
    assert "/live/cameras/" not in block
    assert "fetch(" not in block


def test_snapshot_stale_completion_cannot_be_mislabeled_after_camera_or_role_change():
    block = snapshot_block()
    assert "liveWorkspace.assignments[tileIndex]!==cameraId" in block
    assert "liveWorkspace.roles[tileIndex]!==role" in block
    assert "liveWorkspace.generations[tileIndex]!==generation" in block
    assert "liveSessions.get(tileIndex)!==session" in block
    assert "snapshot source changed before completion" in block
    assert "liveWorkspace.generations[tileIndex]===generation)renderLiveGrid()" in block


def test_snapshot_filename_is_generated_from_stable_id_role_and_utc_time():
    filename = WEB.split("function snapshotFilename", 1)[1].split("async function snapshotTile", 1)[0]
    assert "cameraId).replace(/[^A-Za-z0-9_-]/g,'_')" in filename
    assert "['main','sub','third'].includes(role)" in filename
    assert "capturedAt.toISOString()" in filename
    assert "camera.name" not in filename
    assert "site_id" not in filename
    assert "snapshot-'+safeId+'-'+safeRole+'-'+stamp+'.png" in filename


def test_snapshot_download_is_ephemeral_and_cleanup_is_deterministic():
    block = snapshot_block()
    assert "URL.createObjectURL(blob)" in block
    assert "URL.revokeObjectURL(url)" in block
    assert "snapshotBusy.add(tileIndex)" in block
    assert "snapshotBusy.delete(tileIndex)" in block
    assert "localStorage" not in WEB
    assert "sessionStorage" not in WEB


def test_snapshot_has_no_arbitrary_target_or_secret_surface():
    block = snapshot_block()
    for forbidden in ("rtsp://", "http://", "https://", "access_token", "sessionUrl", "shell", "ffmpeg", "filesystem"):
        assert forbidden not in block.lower()
    assert "cameraId" in block
    assert "role" in block


def test_snapshot_failure_is_tile_local_and_does_not_touch_recording_or_ai():
    block = snapshot_block()
    assert "setTileState(tileIndex,'FAILED'" in block
    assert "stopLiveSessions" not in block
    assert "cleanupTile(" not in block
    assert "/recordings/" not in block
    assert "/ai/" not in block


def test_recording_invariants_remain_independent_of_snapshot():
    assert "recording_source(camera)" in RECORDING
    assert "camera.main_path" in RECORDING
    recording_path = MEDIA.split("async def add_or_replace_recording_path", 1)[1].split("async def delete_path", 1)[0]
    assert '"record": True' in recording_path
    assert '"sourceOnDemand": False' in recording_path
    assert '"maxReaders"' not in recording_path
    block = snapshot_block()
    assert "record_stream_key" not in block
    assert "retention" not in block
    assert "placement" not in block


def test_future_clip_export_has_authorized_bounded_recording_foundation():
    assert "await authorized_camera(session, camera_id, principal)" in RECORDINGS
    assert 'duration: float = Query(..., gt=0.0, le=14400.0)' in RECORDINGS
    assert 'format: Literal["fmp4", "mp4"]' in RECORDINGS
    assert "policy.record_stream_key" in RECORDINGS
