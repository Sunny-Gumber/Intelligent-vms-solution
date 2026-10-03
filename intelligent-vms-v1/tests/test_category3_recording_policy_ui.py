from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.schemas import RecordingPolicyUpdate


ROOT = Path(__file__).parents[1]
WEB = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
RECORDINGS = (
    ROOT / "services" / "control-api" / "app" / "routers" / "recordings.py"
).read_text(encoding="utf-8")
MANUAL = (
    ROOT / "services" / "control-api" / "app" / "routers" / "manual_recordings.py"
).read_text(encoding="utf-8")
RECORDING = (
    ROOT / "services" / "control-api" / "app" / "services" / "recording.py"
).read_text(encoding="utf-8")
MEDIA = (
    ROOT / "services" / "control-api" / "app" / "services" / "mediamtx.py"
).read_text(encoding="utf-8")


def test_active_camera_continuous_recording_controls_are_reachable_and_bounded():
    assert 'id="recordingEnable"' in WEB
    assert 'id="recordingDisable"' in WEB
    assert 'id="recordingRetention"' in WEB
    assert 'min="1" max="3650" step="1"' in WEB
    assert 'id="recordingSaveRetention"' in WEB
    assert "saveRecordingPolicy(true)" in WEB
    assert "saveRecordingPolicy(false)" in WEB
    assert "refreshRecordingControls()" in WEB


def test_policy_read_distinguishes_unconfigured_from_failures_and_fences_stale_results():
    helper = WEB.split("async function recordingPolicy(id)", 1)[1].split(
        "function defaultRecordingPolicy", 1
    )[0]
    assert "encodeURIComponent(id)" in helper
    assert "if(r.status===404)return null" in helper
    assert "if(!r.ok)throw new Error(await r.text())" in helper

    refresh = WEB.split("async function refreshRecordingControls()", 1)[1].split(
        "async function saveRecordingPolicy", 1
    )[0]
    assert "generation=++recordingPolicyGeneration" in refresh
    assert "generation!==recordingPolicyGeneration" in refresh
    assert "activeCameraId()!==cameraId" in refresh


def test_policy_write_refreshes_then_preserves_authoritative_recorder_parameters():
    payload = WEB.split("function recordingPolicyPayload", 1)[1].split(
        "function retentionDaysInput", 1
    )[0]
    assert "part_duration_ms:base.part_duration_ms??1000" in payload
    assert "segment_duration_seconds:base.segment_duration_seconds??900" in payload
    assert "max_part_size_mb:base.max_part_size_mb??50" in payload

    save = WEB.split("async function saveRecordingPolicy", 1)[1].split(
        "async function saveRecordingRetention", 1
    )[0]
    assert save.index("const current=await recordingPolicy(cameraId)") < save.index(
        "method:'PUT'"
    )
    assert "recordingPolicyPayload(current,enable,retentionDays)" in save
    assert "liveWorkspace.roles" not in save
    assert "aiPolicy" not in save
    assert "manual-recordings" not in save
    assert "MediaRecorder" not in save
    assert "async function toggleRecord" not in WEB


def test_retention_bounds_match_backend_contract():
    assert RecordingPolicyUpdate(retention_days=1).retention_days == 1
    assert RecordingPolicyUpdate(retention_days=3650).retention_days == 3650
    with pytest.raises(ValidationError):
        RecordingPolicyUpdate(retention_days=0)
    with pytest.raises(ValidationError):
        RecordingPolicyUpdate(retention_days=3651)


def test_disabling_continuous_recording_cannot_cut_an_active_manual_interval():
    helper = RECORDINGS.split("async def _policy_for_camera", 1)[1].split(
        "async def get_policy", 1
    )[0]
    assert "lock: bool = False" in helper
    assert "stmt = stmt.with_for_update()" in helper

    put_policy = RECORDINGS.split("async def put_policy", 1)[1].split(
        '@router.get("/cameras/{camera_id}/timeline"', 1
    )[0]
    assert "_policy_for_camera(session, camera_id, lock=True)" in put_policy
    assert 'ManualRecordingSessionEntity.state == "ACTIVE"' in put_policy
    assert "ManualRecordingSessionEntity.max_stop_at > now" in put_policy
    assert "Stop active manual recording sessions before disabling continuous recording" in put_policy

    manual_start = MANUAL.split("async def start_manual_recording", 1)[1].split(
        '@router.get("/active"', 1
    )[0]
    assert "_policy_for_camera(session, camera.id, lock=True)" in manual_start


def test_manual_start_ui_requires_loaded_continuous_policy():
    render = WEB.split("function renderManualState()", 1)[1].split(
        "async function refreshManualRecordings", 1
    )[0]
    assert "activeRecordingPolicy.enabled" in render
    assert "activeRecordingPolicy.mode==='continuous'" in render


def test_main_source_copy_recording_invariant_is_unchanged():
    assert "recording_source(camera)" in RECORDING
    assert "camera.main_path" in RECORDING
    record_path = MEDIA.split("async def add_or_replace_recording_path", 1)[1].split(
        "async def delete_path", 1
    )[0]
    assert '"record": True' in record_path
    assert '"sourceOnDemand": False' in record_path
    assert '"recordDeleteAfter": f"{retention_days * 24}h"' in record_path
    assert '"maxReaders"' not in record_path
