"""Real-gateway integration harness for media admission (ADR-0015).

EVIDENCE CLASS: real pinned gateway. This is the evidence that settles
finding A; the unit tests only prove the control-plane decision.

STATUS IN THE BATCH 1 OFFLINE ENVIRONMENT: NOT RUN (BLOCKED). GitHub and
Docker Hub were unreachable, so no MediaMTX binary could be obtained.

Run:
    VMS_IT_MEDIAMTX_BIN=/opt/mediamtx-1.21.2/mediamtx \\
        python -m pytest -m gateway tests/gateway -v -rA
Prerequisites: the official v1.21.2 linux_amd64 binary (verify its checksum
against the release's checksums file), ffmpeg + ffprobe with libx264 on PATH,
free local TCP ports. No camera is needed: a second, unauthenticated MediaMTX
instance plays the camera, fed by an ffmpeg test pattern, and the gateway
under test pulls from it exactly as it would pull from a real camera.

What it proves on the pinned binary:
  1. the shipped mediamtx.yml validates;
  2. the Control API rejects unauthenticated calls and accepts the control
     plane's service credentials through the hook (R4);
  3. the control plane can create and inspect paths through that API;
  4. unauthenticated, wrong-token and wrong-path RTSP reads are refused;
  5. an authorized RTSP read is admitted with header (Basic) credentials;
  6. HLS admits header credentials and refuses query-string tokens;
  7. with two viewers on one path, revoking one disconnects only that one
     while the other keeps receiving frames (R7).
WebRTC/WHEP is not driven here (no headless WHEP client in the toolchain);
it shares the hook and kick code paths, but needs its own browser check.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

MEDIAMTX_BIN = os.environ.get("VMS_IT_MEDIAMTX_BIN", "")
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
ROOT = Path(__file__).resolve().parents[2]
HOOK_SECRET = "gateway-it-hook-secret-0123456789abcdef"
GW_USER, GW_PASS = "vms-control", "gateway-it-admin-password-01"

pytestmark = [
    pytest.mark.gateway,
    pytest.mark.skipif(not MEDIAMTX_BIN or not Path(MEDIAMTX_BIN).is_file(),
                       reason="set VMS_IT_MEDIAMTX_BIN to a pinned MediaMTX binary"),
    pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not available"),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port: int, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"nothing listening on {port}")


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class CameraSourceCredentials:
    """The simulator needs no camera credentials (stands in for CredentialStore)."""

    def get(self, camera):
        return SimpleNamespace(username=None, password=None)

    def decrypt_stream_url(self, camera_id, value):
        return value


@pytest.fixture
def stack(settings, migrated_db, tmp_path, request):
    import uvicorn
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
    from app.db.session import session_scope
    from app.media.mediamtx_client import MediaGatewayClient
    from app.media.session_broker import SessionBroker
    from fastapi import FastAPI

    procs: list[subprocess.Popen] = []
    server = None
    thread = None

    def cleanup() -> None:
        if server is not None:
            server.should_exit = True
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if thread is not None:
            thread.join(timeout=5)

    request.addfinalizer(cleanup)
    ports = SimpleNamespace(sim=free_port(), rtsp=free_port(), hls=free_port(),
                            api=free_port(), hook=free_port(),
                            sim_webrtc=free_port(), sim_webrtc_tcp=free_port(),
                            sim_webrtc_udp=free_udp_port(), webrtc=free_port(),
                            webrtc_tcp=free_port(), webrtc_udp=free_udp_port())

    # -- camera simulator (unauthenticated, loopback only) --------------------
    sim_conf = tmp_path / "sim.yml"
    sim_conf.write_text(
        "logLevel: warn\napi: no\nmetrics: no\npprof: no\nplayback: no\n"
        "webrtc: no\nhls: no\nrtmp: no\nsrt: no\n"
        f"rtspAddress: 127.0.0.1:{ports.sim}\nrtspTransports: [tcp]\n"
        f"webrtcAddress: 127.0.0.1:{ports.sim_webrtc}\n"
        f"webrtcLocalTCPAddress: 127.0.0.1:{ports.sim_webrtc_tcp}\n"
        f"webrtcLocalUDPAddress: 127.0.0.1:{ports.sim_webrtc_udp}\n"
        "authMethod: internal\nauthInternalUsers:\n"
        "  - user: any\n    pass:\n    ips: []\n"
        "    permissions:\n      - action: publish\n      - action: read\n"
        "paths:\n  all_others:\n")
    procs.append(subprocess.Popen([MEDIAMTX_BIN, str(sim_conf)]))
    wait_for_port(ports.sim)
    procs.append(subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=10", "-c:v", "libx264", "-preset", "ultrafast",
         "-tune", "zerolatency", "-g", "10", "-f", "rtsp", "-rtsp_transport", "tcp",
         f"rtsp://127.0.0.1:{ports.sim}/cam"]))

    # -- control-API hook server (real router, real broker, real DB) ----------
    settings.media_auth_hook_secret = HOOK_SECRET
    settings.media_auth_allowed_callers = ["127.0.0.1/32"]
    settings.media_gateway_user, settings.media_gateway_password = GW_USER, GW_PASS
    settings.media_public_hls_url = f"http://127.0.0.1:{ports.hls}"
    gateway = MediaGatewayClient(f"http://127.0.0.1:{ports.api}", username=GW_USER,
                                 password=GW_PASS)
    broker = SessionBroker(gateway, CameraSourceCredentials(), settings)
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(settings=settings, sessions=broker)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ports.hook,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for_port(ports.hook)

    # -- gateway under test: the SHIPPED mediamtx.yml + runtime overrides -----
    env = dict(os.environ)
    env.update({
        "MTX_AUTHHTTPADDRESS":
            f"http://mediamtx:{HOOK_SECRET}@127.0.0.1:{ports.hook}/internal/media/auth",
        "MTX_APIADDRESS": f"127.0.0.1:{ports.api}",
        "MTX_RTSPADDRESS": f"127.0.0.1:{ports.rtsp}",
        "MTX_HLSADDRESS": f"127.0.0.1:{ports.hls}",
        "MTX_WEBRTCADDRESS": f"127.0.0.1:{ports.webrtc}",
        "MTX_WEBRTCLOCALTCPADDRESS": f"127.0.0.1:{ports.webrtc_tcp}",
        "MTX_WEBRTCLOCALUDPADDRESS": f"127.0.0.1:{ports.webrtc_udp}",
        "MTX_WEBRTC": "no",
        "MTX_METRICS": "no",
    })
    procs.append(subprocess.Popen([MEDIAMTX_BIN, str(ROOT / "deploy/docker/mediamtx.yml")],
                                  env=env))
    wait_for_port(ports.api)
    wait_for_port(ports.rtsp)

    # -- data: one camera, two scoped viewers --------------------------------
    with session_scope() as db:
        roles = {}
        for name, perms in BUILTIN_ROLES.items():
            roles[name] = Role(name=name, builtin=True, permissions=[p.value for p in perms])
            db.add(roles[name])
        cams = []
        for index in range(2):
            camera = Camera(name=f"sim-{index}", host="127.0.0.1", protocol="rtsp")
            db.add(camera)
            db.flush()
            db.add(CameraStream(camera_id=camera.id, role="main", enabled=True,
                                source_url=f"rtsp://127.0.0.1:{ports.sim}/cam",
                                transport="rtsp_tcp"))
            cams.append(camera.id)
        viewers = []
        for name in ("viewer1", "viewer2"):
            user = User(username=name)
            db.add(user)
            db.flush()
            db.add(UserRole(user_id=user.id, role_id=roles["viewer"].id))
            for camera_id in cams:
                db.add(UserCameraScope(user_id=user.id, camera_id=camera_id))
            viewers.append(user.id)

    def open_session(user_index: int, camera_index: int = 0):
        from app.media.session_broker import SessionRequest
        with session_scope() as db:
            return asyncio.run(broker.open(db, SessionRequest(
                camera_id=cams[camera_index], user_id=viewers[user_index])))

    yield SimpleNamespace(ports=ports, broker=broker, gateway=gateway,
                          open_session=open_session, session_scope=session_scope)


def rtsp_url(port: int, path: str, user: str | None = None, token: str | None = None) -> str:
    # ffmpeg sends RTSP userinfo as an Authorization header after the 401
    # challenge; it is not a query-string credential.
    auth = f"{user}:{token}@" if user else ""
    return f"rtsp://{auth}127.0.0.1:{port}/{path}"


def ffprobe_ok(url: str) -> bool:
    result = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                             "-rw_timeout", "8000000", "-i", url],
                            capture_output=True, timeout=30, check=False)
    return result.returncode == 0


def test_shipped_config_validates_on_pinned_binary():
    result = subprocess.run([MEDIAMTX_BIN, f"--validate-conf={ROOT}/deploy/docker/mediamtx.yml"],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_control_api_requires_service_credentials(stack):
    url = f"http://127.0.0.1:{stack.ports.api}/v3/paths/list"
    assert httpx.get(url, timeout=10).status_code == 401
    assert httpx.get(url, headers={"Authorization": basic("x", "y")},
                     timeout=10).status_code == 401
    assert httpx.get(url, headers={"Authorization": basic(GW_USER, GW_PASS)},
                     timeout=10).status_code == 200


def test_path_creation_and_inspection_through_authorized_api(stack):
    session = stack.open_session(0)
    status = asyncio.run(stack.gateway.path_status(session.path))
    assert status is not None and status.name == session.path


def test_rtsp_reads_require_a_matching_session(stack):
    s_a = stack.open_session(0, 0)
    s_b = stack.open_session(0, 1)
    port = stack.ports.rtsp
    assert not ffprobe_ok(rtsp_url(port, s_a.path))
    assert not ffprobe_ok(rtsp_url(port, s_a.path, s_a.session_id, "wrong-token"))
    assert not ffprobe_ok(rtsp_url(port, s_b.path, s_a.session_id, s_a.play_token))
    assert ffprobe_ok(rtsp_url(port, s_a.path, s_a.session_id, s_a.play_token))


def test_hls_accepts_header_credentials_and_refuses_query_tokens(stack):
    session = stack.open_session(0)
    url = f"http://127.0.0.1:{stack.ports.hls}/{session.path}/index.m3u8"
    header = {"Authorization": basic(session.session_id, session.play_token)}
    assert httpx.get(url, timeout=10, follow_redirects=True).status_code == 401
    assert httpx.get(url, params={"token": session.play_token},
                     timeout=10, follow_redirects=True).status_code == 401
    deadline = time.monotonic() + 20
    status = 0
    while time.monotonic() < deadline:
        status = httpx.get(url, headers=header, timeout=10,
                           follow_redirects=True).status_code
        if status == 200:
            break
        time.sleep(0.5)
    assert status == 200


def _reader(url: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
         "-i", url, "-c", "copy", "-f", "null", "-", "-progress", "pipe:1", "-nostats"],
        stdout=subprocess.PIPE, text=True)


def _media_time_us(proc: subprocess.Popen, seconds: float) -> int:
    """Read stream-copy progress for `seconds`; return the last media timestamp."""
    last = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and proc.poll() is None:
        line = proc.stdout.readline()
        if line.startswith("out_time_us="):
            last = int(line.split("=", 1)[1] or 0)
    return last


def test_revoking_one_viewer_leaves_the_other_playing(stack):
    s1 = stack.open_session(0)
    s2 = stack.open_session(1)
    assert s1.path == s2.path
    port = stack.ports.rtsp
    viewer1 = _reader(rtsp_url(port, s1.path, s1.session_id, s1.play_token))
    viewer2 = _reader(rtsp_url(port, s2.path, s2.session_id, s2.play_token))
    try:
        assert _media_time_us(viewer1, 8) > 0, "viewer 1 must play first"
        assert _media_time_us(viewer2, 8) > 0, "viewer 2 must play first"

        with stack.session_scope() as db:
            asyncio.run(stack.broker.close(db, s1.session_id, reason="test_revoke"))
        with stack.session_scope() as db:
            asyncio.run(stack.broker.connections.enforce(db))

        viewer1.wait(timeout=15)
        before = _media_time_us(viewer2, 2)
        after = _media_time_us(viewer2, 5)
        assert viewer2.poll() is None, "the other viewer must stay connected"
        assert after > before, "the other viewer must keep receiving frames"
    finally:
        for proc in (viewer1, viewer2):
            if proc.poll() is None:
                proc.kill()
