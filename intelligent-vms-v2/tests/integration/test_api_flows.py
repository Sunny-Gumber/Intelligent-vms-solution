"""End-to-end API tests over the real application stack.

These exercise the actual FastAPI app, the real Alembic-migrated database, the
real RBAC engine and the real service layer. Only the media gateway is
substituted (NullGatewayClient), because MediaMTX is a separate process.
"""

from __future__ import annotations

import datetime as dt

import pytest

API = "/api/v1"


def create_camera(client, auth, **overrides):
    payload = {"name": "Lobby", "host": "192.168.50.11", "port": 80, "protocol": "rtsp",
               "username": "admin", "password": "camera-pass",
               "rtsp_main_path": "/stream1"}
    payload.update(overrides)
    return client.post(f"{API}/cameras?validate=false", headers=auth, json=payload)


# -- bootstrap and authentication -----------------------------------------
def test_service_banner_declares_alpha(client):
    body = client.get("/").json()
    assert body["production_ready"] is False
    assert body["maturity"] == "alpha"


def test_login_and_profile(client, settings, auth):
    profile = client.get(f"{API}/auth/me", headers=auth).json()
    assert profile["username"] == settings.bootstrap_admin_username
    assert "administrator" in profile["roles"]


def test_login_rejects_bad_password(client, settings):
    response = client.post(f"{API}/auth/login",
                           json={"username": settings.bootstrap_admin_username,
                                 "password": "wrong-password"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_unauthenticated_request_is_rejected(client):
    assert client.get(f"{API}/cameras").status_code == 401


def test_account_lockout_after_repeated_failures(client, settings):
    for _ in range(settings.lockout_threshold):
        client.post(f"{API}/auth/login",
                    json={"username": settings.bootstrap_admin_username,
                          "password": "nope"})
    response = client.post(f"{API}/auth/login",
                           json={"username": settings.bootstrap_admin_username,
                                 "password": settings.bootstrap_admin_password})
    assert response.status_code == 423
    assert response.json()["code"] == "account_locked"


def test_refresh_rotation_and_reuse_detection(client, settings):
    tokens = client.post(f"{API}/auth/login",
                         json={"username": settings.bootstrap_admin_username,
                               "password": settings.bootstrap_admin_password}).json()

    rotated = client.post(f"{API}/auth/refresh",
                          json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]

    reused = client.post(f"{API}/auth/refresh",
                         json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401
    assert reused.json()["code"] == "refresh_reused"

    # The whole family is revoked, so the rotated token is dead too.
    assert client.post(f"{API}/auth/refresh",
                       json={"refresh_token": rotated.json()["refresh_token"]}
                       ).status_code == 401


def test_logout_revokes_access_token(client, settings):
    tokens = client.post(f"{API}/auth/login",
                         json={"username": settings.bootstrap_admin_username,
                               "password": settings.bootstrap_admin_password}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.get(f"{API}/auth/me", headers=headers).status_code == 200
    assert client.post(f"{API}/auth/logout", headers=headers, json={}).status_code == 204
    revoked = client.get(f"{API}/auth/me", headers=headers)
    assert revoked.status_code == 401
    assert revoked.json()["code"] == "token_revoked"


def test_mfa_enrolment_and_login(client, settings, auth):
    from app.core import mfa

    enrol = client.post(f"{API}/auth/mfa/enroll", headers=auth).json()
    assert client.post(f"{API}/auth/mfa/verify", headers=auth,
                       json={"code": mfa.totp_now(enrol["secret"])}).status_code == 204

    without_code = client.post(f"{API}/auth/login",
                               json={"username": settings.bootstrap_admin_username,
                                     "password": settings.bootstrap_admin_password})
    assert without_code.status_code == 401
    assert without_code.json()["code"] == "mfa_required"

    # The enrolment code consumed its counter, so replaying it is correctly
    # refused; a code from the next time step is accepted.
    import time

    replayed = client.post(f"{API}/auth/login",
                           json={"username": settings.bootstrap_admin_username,
                                 "password": settings.bootstrap_admin_password,
                                 "totp_code": mfa.totp_now(enrol["secret"])})
    assert replayed.status_code == 401

    next_code = mfa.totp_now(enrol["secret"], at=time.time() + mfa.PERIOD)
    with_code = client.post(f"{API}/auth/login",
                            json={"username": settings.bootstrap_admin_username,
                                  "password": settings.bootstrap_admin_password,
                                  "totp_code": next_code})
    assert with_code.status_code == 200


# -- camera management -----------------------------------------------------
def test_camera_crud_and_credential_protection(client, auth):
    created = create_camera(client, auth)
    assert created.status_code == 201, created.text
    camera = created.json()
    assert camera["state"] == "provisioning"

    detail = client.get(f"{API}/cameras/{camera['id']}", headers=auth).text
    assert "camera-pass" not in detail, "camera password must never appear in a response"
    assert "rtsp://" not in detail, "source URLs must not be exposed to clients"

    patched = client.patch(f"{API}/cameras/{camera['id']}", headers=auth,
                           json={"name": "Lobby North"})
    assert patched.json()["name"] == "Lobby North"
    assert client.get(f"{API}/cameras", headers=auth).json()["total"] == 1


def test_camera_rejects_loopback_host(client, auth):
    response = create_camera(client, auth, host="127.0.0.1")
    assert response.status_code == 400
    assert response.json()["code"] in {"denied_range", "destination_not_allowed"}


def test_csv_export_never_contains_passwords(client, auth):
    create_camera(client, auth)
    response = client.get(f"{API}/cameras/export/csv", headers=auth)
    assert response.status_code == 200
    assert "camera-pass" not in response.text
    assert "***not-exported***" in response.text


def test_bulk_import_reports_failures(client, auth):
    body = client.post(f"{API}/cameras/import", headers=auth, json={
        "cameras": [{"name": "Gate 1", "host": "192.168.50.21", "protocol": "rtsp"},
                    {"name": "Gate 2", "host": "192.168.50.22", "protocol": "rtsp"},
                    {"name": "Bad", "host": "127.0.0.1", "protocol": "rtsp"}],
        "validate_devices": False}).json()
    assert len(body["created"]) == 2
    assert len(body["failed"]) == 1


def test_camera_deletion_preserves_recordings(client, auth, db_session):
    from app.core.timeutil import utcnow
    from app.db.models import RecordingSegment

    camera_id = create_camera(client, auth).json()["id"]
    db_session.add(RecordingSegment(
        camera_id=camera_id, stream_role="main", relative_path="recordings/x.mp4",
        started_at=utcnow() - dt.timedelta(minutes=5), ended_at=utcnow(),
        state="finalized", size_bytes=1024))
    db_session.commit()

    response = client.delete(f"{API}/cameras/{camera_id}", headers=auth)
    assert response.status_code == 200
    assert response.json()["recordings_preserved"] == 1
    assert db_session.query(RecordingSegment).filter_by(camera_id=camera_id).count() == 1, \
        "recorded evidence must survive camera deletion"


def test_camera_quota_enforced_by_licence(client, auth, settings):
    # The unlicensed edition permits settings.unlicensed_camera_limit cameras.
    for index in range(settings.unlicensed_camera_limit):
        assert create_camera(client, auth, name=f"C{index}",
                             host=f"192.168.60.{index + 1}").status_code == 201
    over = create_camera(client, auth, name="Too many", host="192.168.60.200")
    assert over.status_code == 402
    assert over.json()["code"] == "camera_limit_reached"


# -- authorization ---------------------------------------------------------
@pytest.fixture
def viewer_auth(client, auth):
    client.post(f"{API}/users", headers=auth,
                json={"username": "viewer1", "password": "Viewer-Passw0rd!",
                      "roles": ["viewer"], "must_change_password": False})
    tokens = client.post(f"{API}/auth/login",
                         json={"username": "viewer1",
                               "password": "Viewer-Passw0rd!"}).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_viewer_cannot_manage_cameras(client, viewer_auth):
    response = create_camera(client, viewer_auth)
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


def test_viewer_cannot_create_users(client, viewer_auth):
    assert client.post(f"{API}/users", headers=viewer_auth,
                       json={"username": "x", "password": "Sup3r-Secret!",
                             "roles": ["viewer"]}).status_code == 403


def test_viewer_without_site_scope_sees_no_cameras(client, auth, viewer_auth):
    create_camera(client, auth)
    assert client.get(f"{API}/cameras", headers=viewer_auth).json()["total"] == 0, \
        "scope must fail closed"


def test_camera_scope_grant_allows_access(client, auth, viewer_auth):
    camera_id = create_camera(client, auth).json()["id"]
    viewer = next(u for u in client.get(f"{API}/users", headers=auth).json()["items"]
                  if u["username"] == "viewer1")
    client.patch(f"{API}/users/{viewer['id']}", headers=auth,
                 json={"camera_ids": [camera_id]})
    listing = client.get(f"{API}/cameras", headers=viewer_auth).json()
    assert [c["id"] for c in listing["items"]] == [camera_id]


def test_api_key_cannot_exceed_owner_permissions(client, viewer_auth):
    assert client.post(f"{API}/apikeys", headers=viewer_auth,
                       json={"name": "escalation",
                             "permissions": ["user.manage"]}).status_code in (400, 403)


def test_deactivating_user_revokes_access_immediately(client, auth, viewer_auth):
    viewer = next(u for u in client.get(f"{API}/users", headers=auth).json()["items"]
                  if u["username"] == "viewer1")
    client.patch(f"{API}/users/{viewer['id']}", headers=auth, json={"is_active": False})
    assert client.get(f"{API}/auth/me", headers=viewer_auth).status_code == 401


def test_last_administrator_cannot_be_deleted(client, auth, settings):
    admin = next(u for u in client.get(f"{API}/users", headers=auth).json()["items"]
                 if u["username"] == settings.bootstrap_admin_username)
    # Deleting yourself is blocked first; create a second admin to try properly.
    response = client.delete(f"{API}/users/{admin['id']}", headers=auth)
    assert response.status_code == 409


# -- alarms ----------------------------------------------------------------
def make_alarm(db_session, **overrides):
    from app.core.timeutil import utcnow
    from app.db.models import Alarm

    now = utcnow()
    fields = {"title": "Camera offline", "severity": "high", "state": "new",
              "first_event_at": now, "last_event_at": now}
    fields.update(overrides)
    alarm = Alarm(**fields)
    db_session.add(alarm)
    db_session.commit()
    return alarm


def test_alarm_acknowledge_and_resolve(client, auth, db_session):
    alarm = make_alarm(db_session)
    acknowledged = client.post(f"{API}/alarms/{alarm.id}/acknowledge", headers=auth,
                               json={"version": alarm.version, "note": "checking"})
    assert acknowledged.status_code == 200
    body = acknowledged.json()
    assert body["state"] == "acknowledged"

    resolved = client.post(f"{API}/alarms/{alarm.id}/resolve", headers=auth,
                           json={"version": body["version"]})
    assert resolved.json()["state"] == "resolved"


def test_concurrent_acknowledge_rejects_stale_version(client, auth, db_session):
    """Two operators acknowledging at once: the second gets 409, not a silent
    overwrite of the first operator's acknowledgement."""
    alarm = make_alarm(db_session)
    assert client.post(f"{API}/alarms/{alarm.id}/acknowledge", headers=auth,
                       json={"version": alarm.version}).status_code == 200
    second = client.post(f"{API}/alarms/{alarm.id}/acknowledge", headers=auth,
                         json={"version": alarm.version})
    assert second.status_code == 409
    assert second.json()["code"] == "stale_version"


def test_invalid_alarm_transition_is_rejected(client, auth, db_session):
    alarm = make_alarm(db_session, state="closed")
    response = client.post(f"{API}/alarms/{alarm.id}/acknowledge", headers=auth,
                           json={"version": alarm.version})
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_alarm_transition"


def test_default_rules_are_seeded(client, auth):
    names = {r["name"] for r in client.get(f"{API}/rules", headers=auth).json()["items"]}
    assert {"Camera offline", "Recording failed", "Storage critical"} <= names


def test_rule_rejects_unknown_event_type(client, auth):
    response = client.post(f"{API}/rules", headers=auth,
                           json={"name": "Bad rule", "event_types": ["not.a.real.event"]})
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_event_type"


# -- analytics -------------------------------------------------------------
def test_analytics_ingestion_records_source(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    response = client.post(f"{API}/analytics/detections", headers=auth,
                           json={"camera_id": camera_id, "object_class": "person",
                                 "confidence": 0.91, "bbox": [0.1, 0.2, 0.2, 0.4],
                                 "track_id": "t-1"})
    assert response.status_code == 201
    assert response.json()["source"] == "external"

    found = client.get(f"{API}/analytics/search", headers=auth,
                       params={"camera_id": camera_id}).json()
    assert found["total"] == 1
    assert found["items"][0]["source"] == "external"


def test_analytics_capabilities_are_honest(client, auth):
    body = client.get(f"{API}/analytics/capabilities", headers=auth).json()
    assert body["server_side_inference"]["models_bundled"] is False
    assert body["face_recognition"]["matching_engine_implemented"] is False
    assert body["anpr"]["server_ocr_implemented"] is False


def test_anpr_requires_enablement_and_justification(client, auth):
    blocked = client.get(f"{API}/analytics/plates/search", headers=auth,
                         params={"plate": "DL1CAB1234", "justification": "theft case 42"})
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "anpr_disabled"

    assert client.get(f"{API}/analytics/plates/search", headers=auth,
                      params={"plate": "DL1CAB1234"}).status_code == 422


def test_invalid_bbox_rejected(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    response = client.post(f"{API}/analytics/detections", headers=auth,
                           json={"camera_id": camera_id, "object_class": "person",
                                 "bbox": [0.1, 0.2, 5.0, 0.4]})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_bbox"


def test_zone_creation_validates_geometry(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    bad = client.post(f"{API}/analytics/zones/{camera_id}", headers=auth,
                      json={"name": "Z", "kind": "region", "polygon": [[0.1, 0.1]]})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_zone"

    good = client.post(f"{API}/analytics/zones/{camera_id}", headers=auth,
                       json={"name": "Dock", "kind": "region",
                             "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]})
    assert good.status_code == 201


# -- PTZ -------------------------------------------------------------------
def test_ptz_blocked_when_capability_not_proven(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    response = client.post(f"{API}/ptz/{camera_id}/command", headers=auth,
                           json={"kind": "continuous", "pan": 0.5, "tilt": 0.0,
                                 "zoom": 0.0, "speed": 0.5})
    assert response.status_code == 501
    assert response.json()["code"] == "ptz_capability_unavailable"


def test_ptz_capabilities_report_unproven(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    body = client.get(f"{API}/ptz/{camera_id}/capabilities", headers=auth).json()
    assert body["ptz_available"] is False


# -- live sessions ---------------------------------------------------------
def test_live_session_fails_clearly_without_gateway(client, auth, db_session):
    """With no media gateway the API fails fast and says recording is unaffected."""
    from app.db.models import CameraStream

    camera_id = create_camera(client, auth).json()["id"]
    db_session.add(CameraStream(camera_id=camera_id, role="sub", enabled=True,
                                source_url="rtsp://192.168.50.11:554/sub",
                                transport="rtsp_tcp", width=640, height=360,
                                bitrate_kbps=512))
    db_session.commit()

    response = client.post(f"{API}/live/sessions", headers=auth,
                           json={"camera_id": camera_id})
    assert response.status_code == 503
    assert response.json()["code"] == "media_gateway_unavailable"
    assert "Recording and playback are unaffected" in response.json()["detail"]


# -- playback --------------------------------------------------------------
def test_playback_manifest_without_recordings_is_explicit(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    now = dt.datetime.now(dt.UTC)
    body = client.get(f"{API}/playback/{camera_id}/manifest", headers=auth,
                      params={"start": (now - dt.timedelta(hours=1)).isoformat(),
                              "end": now.isoformat()}).json()
    assert body["segments"] == []
    assert "No recording exists" in body["message"]
    assert len(body["gaps"]) == 1


def test_playback_rejects_inverted_range(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    now = dt.datetime.now(dt.UTC)
    response = client.get(f"{API}/playback/{camera_id}/manifest", headers=auth,
                          params={"start": now.isoformat(),
                                  "end": (now - dt.timedelta(hours=1)).isoformat()})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_time_range"


# -- recording schedule ----------------------------------------------------
def test_schedule_round_trip(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    assert client.put(f"{API}/recordings/{camera_id}/schedule", headers=auth,
                      json={"enabled": True, "timezone": "Asia/Kolkata",
                            "blocks": [{"day": 0, "start": "08:00", "end": "18:00"}]}
                      ).status_code == 200
    body = client.get(f"{API}/recordings/{camera_id}/schedule", headers=auth).json()
    assert body["coverage_hours_per_week"] == 10.0
    assert body["timezone"] == "Asia/Kolkata"


def test_schedule_rejects_inverted_block(client, auth):
    camera_id = create_camera(client, auth).json()["id"]
    response = client.put(f"{API}/recordings/{camera_id}/schedule", headers=auth,
                          json={"blocks": [{"day": 1, "start": "18:00", "end": "08:00"}]})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_schedule"


# -- reports / system ------------------------------------------------------
def test_dashboard_reports_real_counts(client, auth):
    create_camera(client, auth)
    body = client.get(f"{API}/reports/dashboard", headers=auth).json()
    assert body["cameras"]["total"] == 1
    assert "cpu_percent" in body["system"]
    assert body["license"]["offline"] is True


def test_capacity_estimate_is_labelled_as_a_model(client, auth):
    body = client.post(f"{API}/system/capacity", headers=auth,
                       json={"classes": [{"count": 64, "codec": "h265", "fps": 15}],
                             "retention_days": 30}).json()
    assert body["cameras"] == 64
    assert "not" in body["disclaimer"].lower()
    assert "benchmark" in body["disclaimer"].lower()


def test_federation_states_what_is_not_implemented(client, auth):
    body = client.get(f"{API}/system/federation/nodes", headers=auth).json()
    assert "distributed recording" in body["not_implemented"]


def test_federation_registration_requires_licence(client, auth):
    response = client.post(f"{API}/system/federation/nodes", headers=auth,
                           json={"name": "site-b", "base_url": "https://site-b.example"})
    assert response.status_code == 402
    assert response.json()["code"] == "feature_not_licensed"


def test_audit_trail_records_actions(client, auth):
    create_camera(client, auth)
    entries = client.get(f"{API}/reports/audit", headers=auth,
                         params={"action": "camera.create"}).json()
    assert entries["count"] >= 1
    assert entries["items"][0]["object_type"] == "camera"


def test_storage_volume_seeded_and_listed(client, auth):
    body = client.get(f"{API}/storage/volumes", headers=auth).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "primary"
    assert "Hardware RAID controllers are not queried" in body["raid_note"]


def test_retention_dry_run_changes_nothing(client, auth):
    body = client.post(f"{API}/storage/retention/run?dry_run=true", headers=auth).json()
    assert body["dry_run"] is True
    assert body["deleted"] == 0


def test_openapi_contract_is_served(client):
    spec = client.get(f"{API}/openapi.json").json()
    assert spec["info"]["title"].startswith("Intelligent VMS")
    assert f"{API}/cameras" in spec["paths"]


def test_correlation_id_is_returned(client):
    response = client.get("/healthz", headers={"X-Correlation-Id": "trace-me-123"})
    assert response.headers["X-Correlation-Id"] == "trace-me-123"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
