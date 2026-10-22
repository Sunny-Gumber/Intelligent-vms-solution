"""Media gateway admission and active revocation (ADR-0015, Batch 1 R1-R9).

EVIDENCE CLASS
  Real: Alembic-migrated database, SessionBroker (token minting, hashing and
  verify_play_token), RBAC/scope evaluation, the HTTP route and its fail-closed
  handling, MediaConnectionRegistry, MediaGatewayClient request construction.
  Test doubles (labelled): GatewayDouble stands in for the MediaMTX process;
  NoCameraCredentials stands in for app.devices.credentials.CredentialStore.

  These tests prove the control-plane decisions. They do NOT prove that
  MediaMTX calls the hook, honours its answer, or that a kicked viewer's video
  actually stops. That is tests/gateway/test_mediamtx_gateway.py (real binary).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
from types import SimpleNamespace

import httpx
import pytest

HOOK_SECRET = "hook-secret-for-tests-0123456789abcdef"
GATEWAY_USER = "vms-control"
GATEWAY_PASSWORD = "gateway-admin-password-0123"
GATEWAY_IP = "172.31.250.10"
OUTSIDE_IP = "198.51.100.7"
SYNTHETIC_SECRET = "SYNTHETIC-TOKEN-DO-NOT-LOG-9f8e7d6c5b4a"
HOOK = "/internal/media/auth"


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------
def _gateway_double():
    from app.media.mediamtx_client import GatewayError, NullGatewayClient

    class GatewayDouble(NullGatewayClient):
        """Stands in for the MediaMTX process; records control-plane calls."""

        def __init__(self) -> None:
            super().__init__()
            self.paths: dict[str, str] = {}
            self.kicked: list[tuple[str, str]] = []
            self.fail_kicks = False

        async def ensure_path(self, name, source_url, **kwargs):
            self.paths[name] = source_url

        async def kick_connection(self, protocol: str, connection_id: str) -> bool:
            if self.fail_kicks:
                raise GatewayError("gateway unreachable", 503)
            self.kicked.append((protocol, connection_id))
            return True

    return GatewayDouble()


class NoCameraCredentials:
    """Stands in for CredentialStore (not part of the Batch 1 handoff)."""

    def get(self, camera):
        return SimpleNamespace(username=None, password=None)

    def decrypt_stream_url(self, camera_id, value):
        return value


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def media(settings, db_session):
    from app.api.internal.media_auth import router
    from app.core.rbac import BUILTIN_ROLES
    from app.db.models import (
        Camera,
        CameraStream,
        Role,
        User,
        UserCameraScope,
        UserRole,
    )
    from app.logging_setup import configure_logging
    from app.media.session_broker import SessionBroker, SessionRequest
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    configure_logging("DEBUG", json_output=True)
    settings.media_auth_hook_secret = HOOK_SECRET
    settings.media_auth_allowed_callers = [f"{GATEWAY_IP}/32"]
    settings.media_gateway_user = GATEWAY_USER
    settings.media_gateway_password = GATEWAY_PASSWORD
    settings.media_connection_max_per_session = 4
    settings.media_kick_max_attempts = 3

    roles = {}
    for name, permissions in BUILTIN_ROLES.items():
        role = Role(name=name, builtin=True, permissions=[p.value for p in permissions])
        db_session.add(role)
        roles[name] = role
    db_session.flush()

    cameras = {}
    for key, host in (("a", "192.168.50.11"), ("b", "192.168.50.12")):
        camera = Camera(name=f"Camera {key.upper()}", host=host, protocol="rtsp")
        db_session.add(camera)
        db_session.flush()
        db_session.add(CameraStream(camera_id=camera.id, role="main", enabled=True,
                                    source_url=f"rtsp://{host}:554/main",
                                    transport="rtsp_tcp", width=1920, height=1080))
        cameras[key] = camera

    users = {}
    for name in ("viewer1", "viewer2"):
        user = User(username=name)
        db_session.add(user)
        db_session.flush()
        db_session.add(UserRole(user_id=user.id, role_id=roles["viewer"].id))
        db_session.add(UserCameraScope(user_id=user.id, camera_id=cameras["a"].id))
        users[name] = user
    db_session.commit()
    for user in users.values():
        db_session.refresh(user)

    gateway = _gateway_double()
    broker = SessionBroker(gateway, NoCameraCredentials(), settings)

    def open_session(user_key: str, camera_key: str = "a"):
        result = asyncio.run(broker.open(db_session, SessionRequest(
            camera_id=cameras[camera_key].id, user_id=users[user_key].id)))
        db_session.commit()
        return result

    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(settings=settings, sessions=broker)
    client = TestClient(app, client=(GATEWAY_IP, 40000))
    outsider = TestClient(app, client=(OUTSIDE_IP, 40000))
    yield SimpleNamespace(settings=settings, db=db_session, broker=broker, gateway=gateway,
                          app=app, client=client, outsider=outsider, users=users,
                          cameras=cameras, roles=roles, open_session=open_session)
    client.close()
    outsider.close()


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def call_hook(client, body: dict, *, auth: str | None = None, headers: dict | None = None):
    merged = {"Authorization": auth if auth is not None else basic("mediamtx", HOOK_SECRET)}
    merged.update(headers or {})
    return client.post(HOOK, json=body, headers=merged)


def read(session, *, protocol="webrtc", conn="conn-1", **overrides) -> dict:
    body = {"user": "", "password": "", "token": session.play_token, "ip": "10.0.0.50",
            "action": "read", "path": session.path, "protocol": protocol, "id": conn,
            "query": "", "userAgent": "test"}
    body.update(overrides)
    return body


def open_bindings(db):
    from app.db.models import MediaConnection
    db.expire_all()
    return db.query(MediaConnection).filter(MediaConnection.closed_at.is_(None)).all()


# --------------------------------------------------------------------------
# A / R1: admission against the real broker, models and RBAC
# --------------------------------------------------------------------------
def test_valid_webrtc_bearer_session_is_admitted_and_bound(media):
    s = media.open_session("viewer1")
    response = call_hook(media.client, read(s))
    assert response.status_code == 204
    assert response.content == b""
    bindings = open_bindings(media.db)
    assert [(b.live_session_id, b.protocol, b.gateway_conn_id) for b in bindings] == [
        (s.session_id, "webrtc", "conn-1")]


def test_valid_rtsp_basic_session_is_admitted(media):
    s = media.open_session("viewer1")
    body = read(s, protocol="rtsp", conn="rtsp-1", token="", user=s.session_id,
                password=s.play_token)
    assert call_hook(media.client, body).status_code == 204


def test_valid_hls_read_is_admitted_but_not_bound(media):
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s, protocol="hls", conn="")).status_code == 204
    assert open_bindings(media.db) == []


@pytest.mark.parametrize("change", [
    {"token": "", "password": ""},
    {"token": "not-a-real-token"},
    {"token": "x" * 300},
])
def test_missing_malformed_or_wrong_token_is_denied(media, change):
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s, **change)).status_code == 401


def test_valid_token_for_another_camera_path_is_denied(media):
    s_a = media.open_session("viewer1", "a")
    from app.media.url_builder import gateway_path
    other_path = gateway_path(media.cameras["b"].id, "main")
    assert call_hook(media.client, read(s_a, path=other_path)).status_code == 401


def test_expired_session_is_denied(media):
    from app.db.models import LiveSession
    s = media.open_session("viewer1")
    record = media.db.get(LiveSession, s.session_id)
    record.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    media.db.commit()
    assert call_hook(media.client, read(s)).status_code == 401


def test_closed_session_is_denied(media):
    s = media.open_session("viewer1")
    asyncio.run(media.broker.close(media.db, s.session_id, reason="client_closed"))
    media.db.commit()
    assert call_hook(media.client, read(s)).status_code == 401


def test_disabled_account_is_denied(media):
    s = media.open_session("viewer1")
    media.users["viewer1"].is_active = False
    media.db.commit()
    assert call_hook(media.client, read(s)).status_code == 401


def test_locked_account_is_denied(media):
    s = media.open_session("viewer1")
    media.users["viewer1"].is_locked = True
    media.users["viewer1"].locked_until = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    media.db.commit()
    assert call_hook(media.client, read(s)).status_code == 401


def test_permission_removed_after_issue_is_denied(media):
    from app.db.models import UserCameraScope
    s = media.open_session("viewer1")
    media.db.query(UserCameraScope).filter_by(user_id=media.users["viewer1"].id).delete()
    media.db.commit()
    assert call_hook(media.client, read(s)).status_code == 401


def test_disabled_camera_is_denied(media):
    s = media.open_session("viewer1")
    media.cameras["a"].enabled = False
    media.db.commit()
    assert call_hook(media.client, read(s)).status_code == 401


def test_basic_user_must_match_the_session(media):
    s = media.open_session("viewer1")
    body = read(s, protocol="rtsp", token="", user="someone-else", password=s.play_token)
    assert call_hook(media.client, body).status_code == 401


# --------------------------------------------------------------------------
# R6: token model and URL policy
# --------------------------------------------------------------------------
def test_rest_access_token_is_not_media_authority(media):
    from app.core.security import create_access_token
    s = media.open_session("viewer1")
    rest_token, _ = create_access_token(media.users["viewer1"], ["live.view"],
                                        media.settings)
    assert call_hook(media.client, read(s, token=rest_token)).status_code == 401


def test_play_token_is_not_rest_authority(media):
    from app.core.errors import AuthenticationError
    from app.core.security import decode_access_token
    s = media.open_session("viewer1")
    with pytest.raises(AuthenticationError):
        decode_access_token(s.play_token, media.settings)


@pytest.mark.parametrize("query", ["token={tok}", "pass={tok}", "jwt=abc", "a=1&Token=x"])
def test_credentials_in_url_query_are_rejected_even_when_valid(media, query):
    s = media.open_session("viewer1")
    body = read(s, query=query.format(tok=s.play_token))
    assert call_hook(media.client, body).status_code == 401


def test_harmless_query_parameters_are_allowed(media):
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s, query="lowLatency=1")).status_code == 204


def test_kickable_protocol_without_connection_id_is_denied(media):
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s, conn="")).status_code == 401


@pytest.mark.parametrize("connection_id", ["bad?id", "bad/id", "bad id", "x" * 129])
def test_invalid_gateway_connection_id_is_denied_at_admission(media, connection_id):
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s, conn=connection_id)).status_code == 401


# --------------------------------------------------------------------------
# R5: caller authentication and trust configuration
# --------------------------------------------------------------------------
def test_caller_without_basic_credentials_is_denied(media):
    s = media.open_session("viewer1")
    assert media.client.post(HOOK, json=read(s)).status_code == 401


def test_caller_with_wrong_hook_secret_is_denied(media):
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s),
                     auth=basic("mediamtx", "wrong-" + HOOK_SECRET)).status_code == 401


def test_caller_outside_allowed_network_is_denied_even_with_secret(media):
    s = media.open_session("viewer1")
    assert call_hook(media.outsider, read(s)).status_code == 401


def test_forwarded_header_and_payload_ip_do_not_establish_trust(media):
    s = media.open_session("viewer1")
    body = read(s, ip=GATEWAY_IP)
    response = call_hook(media.outsider, body, headers={"X-Forwarded-For": GATEWAY_IP})
    assert response.status_code == 401


@pytest.mark.parametrize("change", [
    {"media_auth_hook_secret": None},
    {"media_auth_hook_secret": "too-short"},
    {"media_auth_allowed_callers": []},
    {"media_auth_allowed_callers": ["0.0.0.0/0"]},
    {"media_auth_allowed_callers": ["not-a-network"]},
    {"media_gateway_password": None},
])
def test_invalid_trust_configuration_denies_everything(media, change):
    s = media.open_session("viewer1")
    for key, value in change.items():
        setattr(media.settings, key, value)
    assert media.settings.media_auth_problems()
    assert call_hook(media.client, read(s)).status_code == 401


def test_shipped_defaults_are_not_trusted():
    from app.config import Settings
    assert Settings(_env_file=None).media_auth_problems()


# --------------------------------------------------------------------------
# R4: gateway administration through the hook
# --------------------------------------------------------------------------
def test_control_plane_api_action_with_service_credentials_is_admitted(media):
    body = {"user": GATEWAY_USER, "password": GATEWAY_PASSWORD, "action": "api",
            "path": "", "protocol": "", "id": "", "query": "", "ip": GATEWAY_IP}
    assert call_hook(media.client, body).status_code == 204


@pytest.mark.parametrize("user,password", [
    ("", ""), (GATEWAY_USER, "wrong"), ("admin", GATEWAY_PASSWORD)])
def test_api_action_without_service_credentials_is_denied(media, user, password):
    body = {"user": user, "password": password, "action": "api", "path": "",
            "protocol": "", "id": "", "query": "", "ip": "10.0.0.9"}
    assert call_hook(media.client, body).status_code == 401


def test_viewer_play_token_cannot_administer_the_gateway(media):
    s = media.open_session("viewer1")
    body = read(s, action="api", user=s.session_id, password=s.play_token)
    assert call_hook(media.client, body).status_code == 401


@pytest.mark.parametrize("action", ["publish", "playback", "metrics", "pprof", "other"])
def test_other_actions_are_never_admitted(media, action):
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s, action=action)).status_code == 401


def test_unexposed_protocol_is_denied(media):
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s, protocol="srt")).status_code == 401


def test_ensure_path_refuses_legacy_per_path_credentials():
    from app.media.mediamtx_client import GatewayError, MediaGatewayClient
    client = MediaGatewayClient("http://gateway.invalid:9997")
    with pytest.raises(GatewayError):
        asyncio.run(client.ensure_path("cam_x_main", "rtsp://cam/x", read_user="u",
                                       read_pass="p"))


# --------------------------------------------------------------------------
# R9: fail closed, malformed input, secret hygiene
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"action": 5}',
                                 b'{"token": "' + b"x" * 2000 + b'"}'])
def test_malformed_payloads_are_denied(media, raw):
    response = media.client.post(HOOK, content=raw, headers={
        "Authorization": basic("mediamtx", HOOK_SECRET), "Content-Type": "application/json"})
    assert response.status_code == 401
    assert response.content == b""


def test_missing_application_container_denies(media):
    media.app.state.container = None
    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s)).status_code == 401


def test_broker_failure_denies_and_never_logs_secret_text(media, caplog, capsys):
    s = media.open_session("viewer1")

    def explode(*args, **kwargs):
        raise RuntimeError(f"database exploded near token={SYNTHETIC_SECRET}")

    media.broker.verify_play_token = explode
    caplog.set_level(logging.DEBUG)
    response = call_hook(media.client, read(s, token=SYNTHETIC_SECRET))
    assert response.status_code == 401
    captured = caplog.text + capsys.readouterr().out
    assert "media_auth.failed_closed" in captured
    assert "RuntimeError" in captured
    assert SYNTHETIC_SECRET not in captured


def test_denials_never_log_presented_tokens_or_queries(media, caplog, capsys):
    s = media.open_session("viewer1")
    caplog.set_level(logging.DEBUG)
    call_hook(media.client, read(s, token=SYNTHETIC_SECRET))
    call_hook(media.client, read(s, query=f"token={SYNTHETIC_SECRET}"))
    call_hook(media.client, read(s), auth=basic("mediamtx", SYNTHETIC_SECRET))
    captured = caplog.text + capsys.readouterr().out
    assert SYNTHETIC_SECRET not in captured
    assert s.play_token not in captured


def test_denied_request_does_not_persist_a_binding(media):
    from app.db.models import UserCameraScope
    s = media.open_session("viewer1")
    media.db.query(UserCameraScope).filter_by(user_id=media.users["viewer1"].id).delete()
    media.db.commit()
    assert call_hook(media.client, read(s, conn="never")).status_code == 401
    assert open_bindings(media.db) == []


# --------------------------------------------------------------------------
# R7: active revocation (control-plane decision + targeted kick request)
# --------------------------------------------------------------------------
def _two_viewers(media):
    s1 = media.open_session("viewer1")
    s2 = media.open_session("viewer2")
    assert s1.path == s2.path, "both viewers watch the same camera path"
    assert call_hook(media.client, read(s1, conn="conn-v1")).status_code == 204
    assert call_hook(media.client, read(s2, conn="conn-v2")).status_code == 204
    return s1, s2


def _enforce(media, registry=None, *, same_session=False):
    """One revocation cycle. Like the lifecycle loop, each cycle normally gets
    a fresh database session; same_session=True reuses the test's long-lived
    session (holding stale identity-map copies) to prove the decision does
    not depend on session freshness."""
    from app.db.session import session_scope
    registry = registry or media.broker.connections
    if same_session:
        result = asyncio.run(registry.enforce(media.db))
        media.db.commit()
        return result
    with session_scope() as db:
        result = asyncio.run(registry.enforce(db))
    media.db.expire_all()
    return result


def test_closing_one_session_kicks_only_that_viewer(media):
    s1, _s2 = _two_viewers(media)
    asyncio.run(media.broker.close(media.db, s1.session_id, reason="client_closed",
                                   user_id=media.users["viewer1"].id))
    media.db.commit()
    assert media.gateway.kicked == [("webrtc", "conn-v1")]
    assert [b.gateway_conn_id for b in open_bindings(media.db)] == ["conn-v2"]
    assert _enforce(media).kicked == 0


def test_logout_revocation_from_sync_code_is_enforced_by_cycle(media):
    _two_viewers(media)
    media.broker.revoke_user_sessions(media.db, media.users["viewer1"].id, "logout")
    media.db.commit()
    result = _enforce(media)
    assert media.gateway.kicked == [("webrtc", "conn-v1")]
    assert result.reasons == {"session_logout": 1}


def test_permission_change_kicks_and_closes_the_session(media):
    from app.db.models import LiveSession, UserCameraScope
    s1, _ = _two_viewers(media)
    media.db.query(UserCameraScope).filter_by(user_id=media.users["viewer1"].id).delete()
    media.db.commit()
    result = _enforce(media)
    assert media.gateway.kicked == [("webrtc", "conn-v1")]
    assert result.reasons == {"permission_revoked": 1}
    media.db.expire_all()
    assert media.db.get(LiveSession, s1.session_id).close_reason == "permission_revoked"


def test_permission_change_is_seen_even_through_a_stale_session(media):
    from app.db.models import UserCameraScope
    _two_viewers(media)
    media.db.query(UserCameraScope).filter_by(user_id=media.users["viewer1"].id).delete()
    media.db.commit()
    _enforce(media, same_session=True)
    assert media.gateway.kicked == [("webrtc", "conn-v1")]


def test_revocation_policy_scan_fairly_checks_bindings_across_batches(media):
    from app.db.models import MediaConnection

    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s, conn="conn-first")).status_code == 204
    assert call_hook(media.client, read(s, conn="conn-second")).status_code == 204
    media.settings.media_revocation_batch = 1

    _enforce(media)
    media.db.expire_all()
    rows = media.db.query(MediaConnection).filter_by(live_session_id=s.session_id).all()
    checked_after_first = {row.gateway_conn_id for row in rows if row.last_policy_check_at}
    assert len(checked_after_first) == 1

    _enforce(media)
    media.db.expire_all()
    rows = media.db.query(MediaConnection).filter_by(live_session_id=s.session_id).all()
    checked_after_second = {row.gateway_conn_id for row in rows if row.last_policy_check_at}
    assert len(checked_after_second) == 2
    assert checked_after_first < checked_after_second


def test_account_disablement_kicks_existing_viewer(media):
    _two_viewers(media)
    media.users["viewer1"].is_active = False
    media.db.commit()
    _enforce(media)
    assert media.gateway.kicked == [("webrtc", "conn-v1")]


def test_session_expiry_kicks_established_connection(media):
    from app.db.models import LiveSession
    s1, _ = _two_viewers(media)
    media.db.get(LiveSession, s1.session_id).expires_at = (
        dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
    media.db.commit()
    result = _enforce(media)
    assert media.gateway.kicked == [("webrtc", "conn-v1")]
    assert result.reasons == {"session_expired": 1}


def test_revocation_survives_control_api_restart(media):
    from app.media.connection_registry import MediaConnectionRegistry
    _two_viewers(media)
    media.broker.revoke_user_sessions(media.db, media.users["viewer1"].id, "deactivated")
    media.db.commit()
    fresh = MediaConnectionRegistry(media.gateway, media.settings)  # new process
    _enforce(media, fresh)
    assert media.gateway.kicked == [("webrtc", "conn-v1")]


def test_failed_kicks_are_retried_then_abandoned_loudly(media, caplog, capsys):
    from app.db.models import MediaConnection
    _two_viewers(media)
    media.broker.revoke_user_sessions(media.db, media.users["viewer1"].id, "logout")
    media.db.commit()
    media.gateway.fail_kicks = True
    caplog.set_level(logging.DEBUG)
    results = [_enforce(media) for _ in range(media.settings.media_kick_max_attempts)]
    assert [r.kick_failures for r in results] == [1, 1, 1]
    assert results[-1].abandoned == 1
    media.db.expire_all()
    row = media.db.query(MediaConnection).filter_by(gateway_conn_id="conn-v1").one()
    assert row.close_reason == "kick_failed"
    assert "media.revocation_kick_abandoned" in caplog.text + capsys.readouterr().out
    assert _enforce(media).kick_failures == 0, "retries are bounded"


def test_revocation_kicks_are_concurrent_and_batch_bounded(media):
    s = media.open_session("viewer1")
    for connection_id in ("batch-1", "batch-2", "batch-3"):
        assert call_hook(media.client, read(s, conn=connection_id)).status_code == 204
    media.broker.revoke_user_sessions(media.db, media.users["viewer1"].id, "logout")
    media.db.commit()
    media.settings.media_revocation_batch = 10
    media.settings.media_revocation_kick_concurrency = 2
    active = 0
    max_active = 0

    async def delayed_kick(_protocol, _connection_id):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1

    media.gateway.kick_connection = delayed_kick
    result = _enforce(media)
    assert result.kicked == 2
    assert max_active == 2
    assert len(open_bindings(media.db)) == 1


def test_revocation_kick_timeout_is_bounded(media):
    from app.db.models import MediaConnection

    s = media.open_session("viewer1")
    assert call_hook(media.client, read(s)).status_code == 204
    media.broker.revoke_user_sessions(media.db, media.users["viewer1"].id, "logout")
    media.db.commit()
    media.settings.media_revocation_kick_concurrency = 1
    media.settings.media_revocation_kick_timeout_s = 0.01

    async def stalled_kick(_protocol, _connection_id):
        await asyncio.Event().wait()

    media.gateway.kick_connection = stalled_kick
    result = _enforce(media)
    media.db.expire_all()
    binding = media.db.query(MediaConnection).filter_by(gateway_conn_id="conn-1").one()
    assert result.kick_failures == 1
    assert binding.kick_attempts == 1
    assert binding.closed_at is None


def test_reconnects_beyond_the_bound_supersede_the_oldest(media):
    media.settings.media_connection_max_per_session = 2
    s = media.open_session("viewer1")
    for conn in ("r1", "r2", "r3"):
        assert call_hook(media.client, read(s, conn=conn)).status_code == 204
    _enforce(media)
    assert media.gateway.kicked == [("webrtc", "r1")]
    assert sorted(b.gateway_conn_id for b in open_bindings(media.db)) == ["r2", "r3"]


def test_reauthentication_of_a_revoked_connection_is_denied(media):
    _two_viewers(media)
    media.broker.revoke_user_sessions(media.db, media.users["viewer1"].id, "logout")
    media.db.commit()
    _enforce(media)
    s1b = media.open_session("viewer1")
    assert call_hook(media.client, read(s1b, conn="conn-v1")).status_code == 401


def test_connection_id_cannot_be_rebound_to_another_session(media):
    _s1, s2 = _two_viewers(media)
    assert call_hook(media.client, read(s2, conn="conn-v1")).status_code == 401


def test_closed_bindings_are_purged_after_retention(media):
    from app.db.models import MediaConnection
    _two_viewers(media)
    media.broker.revoke_user_sessions(media.db, media.users["viewer1"].id, "logout")
    media.db.commit()
    _enforce(media)
    row = media.db.query(MediaConnection).filter_by(gateway_conn_id="conn-v1").one()
    row.closed_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
    media.db.commit()
    assert _enforce(media).purged == 1


# --------------------------------------------------------------------------
# Real MediaGatewayClient request construction (HTTP transport double)
# --------------------------------------------------------------------------
@pytest.fixture
def recorded_gateway(monkeypatch):
    from app.media import mediamtx_client

    requests: list[httpx.Request] = []
    status = {"code": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status["code"], text="")

    monkeypatch.setattr(mediamtx_client, "internal_client",
                        lambda timeout_s=10.0: httpx.AsyncClient(
                            transport=httpx.MockTransport(handler)))
    client = mediamtx_client.MediaGatewayClient(
        "http://mediamtx-admin:9997", username=GATEWAY_USER, password=GATEWAY_PASSWORD)
    return SimpleNamespace(client=client, requests=requests, status=status)


def test_kick_targets_one_session_with_service_credentials(recorded_gateway):
    assert asyncio.run(recorded_gateway.client.kick_connection("webrtc", "abc-123"))
    request = recorded_gateway.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v3/webrtcsessions/kick/abc-123"
    assert request.headers["authorization"] == basic(GATEWAY_USER, GATEWAY_PASSWORD)


def test_kick_of_vanished_session_counts_as_done(recorded_gateway):
    recorded_gateway.status["code"] = 404
    assert asyncio.run(recorded_gateway.client.kick_connection("rtsp", "gone"))


def test_kick_when_gateway_errors_is_retryable(recorded_gateway):
    from app.media.mediamtx_client import GatewayError
    recorded_gateway.status["code"] = 503
    with pytest.raises(GatewayError):
        asyncio.run(recorded_gateway.client.kick_connection("webrtc", "abc"))


@pytest.mark.parametrize("protocol,conn", [
    ("hls", "x"),
    ("webrtc", "../paths"),
    ("webrtc", "abc?query"),
    ("webrtc", "x" * 129),
    ("webrtc", ""),
])
def test_kick_rejects_unkickable_protocols_and_bad_ids(recorded_gateway, protocol, conn):
    with pytest.raises(ValueError):
        asyncio.run(recorded_gateway.client.kick_connection(protocol, conn))
    assert recorded_gateway.requests == []
