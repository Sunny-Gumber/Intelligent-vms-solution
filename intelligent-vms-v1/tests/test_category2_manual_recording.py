from pathlib import Path

ROOT=Path(__file__).parents[1]
ENTITIES=(ROOT/"services/control-api/app/models/entities.py").read_text()
ROUTER=(ROOT/"services/control-api/app/routers/manual_recordings.py").read_text()
RECORDINGS=(ROOT/"services/control-api/app/routers/recordings.py").read_text()
CAMERAS=(ROOT/"services/control-api/app/routers/cameras.py").read_text()
WEB=(ROOT/"web/index.html").read_text()
MIGRATION=(ROOT/"migrations/versions/0017_manual_recording_sessions.py").read_text()


def test_durable_shared_state_and_cross_replica_uniqueness():
    assert 'class ManualRecordingSessionEntity' in ENTITIES
    assert 'uq_manual_recording_active_owner_camera' in MIGRATION
    assert "postgresql_where=sa.text(\"state = 'ACTIVE'\")" in MIGRATION
    assert 'operator_subject' in ENTITIES and 'started_at' in ENTITIES and 'max_stop_at' in ENTITIES and 'stopped_at' in ENTITIES
    assert 'module global' not in ROUTER.lower()


def test_start_is_server_timed_authorized_and_idempotent():
    block=ROUTER.split('async def start_manual_recording',1)[1].split('@router.get("/active"',1)[0]
    assert 'authorized_camera(session, camera_id, principal)' in block
    assert 'datetime.now(timezone.utc)' in block
    assert 'policy.enabled' in block and 'policy.mode != "continuous"' in block
    assert 'IntegrityError' in ROUTER
    assert 'return _read(existing)' in block
    assert 'start:' not in block


def test_stop_is_owned_locked_server_timed_and_idempotent():
    block=ROUTER.split('async def stop_manual_recording',1)[1].split('@router.get("/{session_id}/export"',1)[0]
    assert '_owned_session(session, session_id, principal, lock=True)' in block
    assert 'if row.state == "STOPPED"' in block
    assert 'datetime.now(timezone.utc)' in block
    assert 'min(now, _max_stop(row))' in block


def test_operator_ownership_and_scope_are_current():
    assert 'principal.can_access(row.tenant_id, row.site_id)' in ROUTER
    assert 'principal.subject == row.operator_subject or "admin" in principal.roles' in ROUTER
    assert 'Manual recording session not found' in ROUTER


def test_max_duration_auto_finalization_is_server_side():
    assert 'max_stop_at=now + timedelta(seconds=settings.recording_export_max_duration_seconds)' in ROUTER
    assert 'return _utc(row.max_stop_at)' in ROUTER
    assert 'row.stopped_at = _max_stop(row)' in ROUTER
    assert '_finalize_if_expired' in ROUTER
    assert 'setTimeout' not in ROUTER


def test_manual_export_reuses_291_contract_and_has_no_second_recorder():
    block=ROUTER.split('async def export_manual_recording',1)[1]
    assert 'stream_recording_clip(request, camera, start' in block
    assert 'authorized_camera(session, row.camera_id, principal)' in block
    assert 'record_stream_key' not in block
    for forbidden in ('ffmpeg','subprocess','rtsp://','mediamtx','MediaRecorder'):
        assert forbidden.lower() not in ROUTER.lower()
    assert 'async def stream_recording_clip' in RECORDINGS
    assert 'export_clip' in RECORDINGS


def test_camera_delete_finalizes_active_session_and_preserves_history():
    delete=CAMERAS.split('@router.delete("/{camera_id}"',1)[1]
    assert 'ManualRecordingSessionEntity.state == "ACTIVE"' in delete
    assert '.values(state="STOPPED"' in delete
    assert 'ondelete="SET NULL"' in ENTITIES
    assert 'camera_id is None' in ROUTER and '410' in ROUTER


def test_recording_live_ai_snapshot_invariants():
    assert 'put_policy(' not in ROUTER
    assert 'provision_recording(' not in ROUTER
    assert 'delete_path(' not in ROUTER
    assert 'liveWorkspace.roles' not in ROUTER
    assert 'snapshotTile' in WEB and 'image/png' in WEB
    assert 'manual-recordings' in WEB
    assert 'MAIN interval' in WEB


def test_refresh_and_tile_role_changes_do_not_own_authoritative_state():
    assert 'refreshManualRecordings' in WEB
    assert '/manual-recordings/active' in WEB
    assert 'activeManualForCamera(cameraId)' in WEB
    assert 'stream_role' not in ROUTER
    assert 'liveWorkspace.roles' not in ROUTER


def test_migration_constraints_and_chain():
    assert 'down_revision: Union[str, Sequence[str], None] = "0016"' in MIGRATION
    assert "state IN ('ACTIVE','STOPPED')" in MIGRATION
    assert 'stopped_at >= started_at AND stopped_at <= max_stop_at' in MIGRATION


def test_recent_history_is_bounded_and_supports_export_retry_after_reload():
    assert '@router.get("/recent"' in ROUTER
    assert 'limit: int = Query(default=20, ge=1, le=100)' in ROUTER
    assert 'ManualRecordingSessionEntity.started_at.desc()).limit(limit)' in ROUTER
    assert 'recentManualSessions' in WEB
    assert 'Download last manual clip' in WEB
    assert '/manual-recordings/recent?limit=20' in WEB
    assert 'downloadLastManual' in WEB
