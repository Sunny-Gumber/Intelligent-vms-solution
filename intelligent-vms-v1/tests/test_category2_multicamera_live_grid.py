from pathlib import Path

ROOT = Path(__file__).parents[1]
WEB = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
MEDIA = (ROOT / "services" / "control-api" / "app" / "services" / "mediamtx.py").read_text(encoding="utf-8")


def test_grid_is_bounded_to_supported_fixed_layouts():
    assert "if(![1,4,9,16].includes(count))return" in WEB
    for label in ("1×1", "2×2", "3×3", "4×4"):
        assert label in WEB
    assert "capacity remains externally qualified" in WEB


def test_grid_assigns_only_authorized_inventory_and_prevents_duplicates():
    assert "cameraCache=await jsonFetch(`${API}/cameras`)" in WEB
    assert "const authorizedIds=new Set(cameraCache.map(c=>c.id))" in WEB
    assert "liveWorkspace.assignments.some((id,i)=>i!==tileIndex&&id===cameraId)" in WEB
    assert "cameraCache.filter(c=>!assigned.has(c.id))" in WEB


def test_tile_cleanup_is_idempotent_and_layout_shrink_releases_sessions():
    assert "liveSessions.delete(tileIndex)" in WEB
    assert "session.pc.close()" in WEB
    assert "method:'DELETE'" in WEB
    assert "video.srcObject=null" in WEB
    assert "cleanupTile(count+n)" in WEB
    assert "Promise.allSettled" in WEB


def test_stale_async_completion_cannot_resurrect_replaced_camera():
    assert "liveWorkspace.generations[tileIndex]" in WEB
    assert "current!==liveWorkspace.generations[tileIndex]" in WEB
    assert "liveWorkspace.assignments[tileIndex]!==cameraId" in WEB
    assert "if(sessionUrl)fetch(sessionUrl,{method:'DELETE'" in WEB


def test_whep_security_contract_is_preserved():
    assert "Authorization:'Bearer '+access.access_token" in WEB
    assert "candidate.origin!==origin" in WEB
    assert "cross-origin WHEP session rejected" in WEB
    assert "?token=" not in WEB
    assert "localStorage" not in WEB
    assert "sessionStorage" not in WEB
    assert "dangerouslySetInnerHTML" not in WEB


def test_failures_are_tile_local_and_retry_is_manual_not_infinite():
    assert "setTileState(tileIndex,'FAILED'" in WEB
    assert "function retryTile(i)" in WEB
    assert "reader limit reached" in WEB
    assert "setInterval(retry" not in WEB
    assert "setTimeout(retry" not in WEB
    assert "while(true)" not in WEB


def test_unmount_and_page_exit_attempt_session_cleanup():
    assert "window.addEventListener('pagehide',()=>{stopLiveSessions()})" in WEB


def test_camera_labels_are_escaped_before_html_rendering():
    assert "esc(c.name)" in WEB
    assert "esc(c.site_id)" in WEB


def test_recording_path_remains_separate_from_live_reader_policy():
    recording = MEDIA.split("async def add_or_replace_recording_path", 1)[1].split(
        "async def delete_path", 1
    )[0]
    assert '"record": True' in recording
    assert '"sourceOnDemand": False' in recording
    assert '"maxReaders"' not in recording
    assert '"maxReaders": settings.live_view_max_readers_per_path' in MEDIA


def test_grid_reconciliation_reattaches_existing_media_streams():
    assert "session.stream" in WEB
    assert "video.srcObject=session.stream" in WEB
    assert "liveSessions.forEach" in WEB
