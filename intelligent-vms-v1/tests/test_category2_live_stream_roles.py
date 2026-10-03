import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.auth import Principal
from app.core.config import settings
from app.models.entities import CameraEntity
from app.routers import live_media
from app.services.camera_lifecycle import available_live_roles, main_live_stream_key
from app.services.stream_keys import make_role_stream_key


ROOT = Path(__file__).parents[1]
WEB = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
MEDIA = (ROOT / "services" / "control-api" / "app" / "services" / "mediamtx.py").read_text(encoding="utf-8")
RECORDING = (ROOT / "services" / "control-api" / "app" / "services" / "recording.py").read_text(encoding="utf-8")
AI_MODELS = (ROOT / "services" / "control-api" / "app" / "models" / "entities.py").read_text(encoding="utf-8")


def principal():
    return Principal(subject="viewer-1", roles=frozenset({"viewer"}), tenant_id="tenant-a", site_ids=frozenset({"site-a"}))


def camera(*, sub=True, third=True):
    return CameraEntity(id="camera-1", tenant_id="tenant-a", site_id="site-a", name="Gate", host="192.168.1.20", rtsp_port=554, main_path="/main", sub_path="/sub" if sub else None, third_path="/third" if third else None, stream_key="site-a-gate-abcd1234", third_stream_key="site-a-gate-abcd1234-third" if third else None, media_node_id="media-local-01", enabled=True, desired_state="provisioned", created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))


class Session:
    def __init__(self, cam):
        self.cam = cam

    async def get(self, model, key):
        if model is CameraEntity and self.cam.id == key:
            return self.cam
        return None


@pytest.fixture(autouse=True)
def media_bases(monkeypatch):
    monkeypatch.setattr(settings, "placement_execution_enabled", False)
    monkeypatch.setattr(settings, "mediamtx_webrtc_public_base", "https://media.example/webrtc")
    monkeypatch.setattr(settings, "mediamtx_hls_public_base", "https://media.example/hls")


def fake_issue(**kwargs):
    return "grant", datetime.now(timezone.utc)


def test_available_roles_are_authoritative_and_optional():
    assert available_live_roles(camera()) == ["main", "sub", "third"]
    assert available_live_roles(camera(third=False)) == ["main", "sub"]
    assert available_live_roles(camera(sub=False, third=False)) == ["main"]


def test_explicit_main_uses_distinct_live_path_when_sub_exists(monkeypatch):
    cam = camera()
    issued = {}
    def capture(**kwargs):
        issued.update(kwargs)
        return fake_issue(**kwargs)
    monkeypatch.setattr(live_media, "issue_live_access_token", capture)
    result = asyncio.run(live_media.create_live_access(cam.id, "main", Session(cam), principal()))
    expected = make_role_stream_key(cam.stream_key, "main")
    assert main_live_stream_key(cam) == expected
    assert result.stream_role == "main"
    assert result.path == expected
    assert result.webrtc_url.endswith("/" + expected)
    assert issued["stream_key"] == expected


def test_sub_and_third_grants_bind_selected_existing_path(monkeypatch):
    cam = camera()
    issued = []
    def capture(**kwargs):
        issued.append(kwargs["stream_key"])
        return fake_issue(**kwargs)
    monkeypatch.setattr(live_media, "issue_live_access_token", capture)
    sub = asyncio.run(live_media.create_live_access(cam.id, "sub", Session(cam), principal()))
    third = asyncio.run(live_media.create_live_access(cam.id, "third", Session(cam), principal()))
    assert sub.path == cam.stream_key
    assert third.path == cam.third_stream_key
    assert issued == [cam.stream_key, cam.third_stream_key]


def test_main_only_camera_preserves_default_and_rejects_unavailable_roles(monkeypatch):
    cam = camera(sub=False, third=False)
    monkeypatch.setattr(live_media, "issue_live_access_token", fake_issue)
    default = asyncio.run(live_media.create_live_access(cam.id, None, Session(cam), principal()))
    assert default.stream_role == "main"
    assert default.path == cam.stream_key
    for role in ("sub", "third"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(live_media.create_live_access(cam.id, role, Session(cam), principal()))
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "LIVE_ROLE_UNAVAILABLE"


def test_default_live_role_remains_sub_when_available(monkeypatch):
    cam = camera(sub=True, third=False)
    monkeypatch.setattr(live_media, "issue_live_access_token", fake_issue)
    result = asyncio.run(live_media.create_live_access(cam.id, None, Session(cam), principal()))
    assert result.stream_role == "sub"
    assert result.path == cam.stream_key


def test_active_tile_selector_exposes_only_server_reported_roles():
    assert "available_live_roles" in WEB
    assert "roleOptions(camera,selected)" in WEB
    assert "liveWorkspace.activeTile===i" in WEB
    assert "switchRole(${i},this.value)" in WEB
    assert "stream_role=${encodeURIComponent(role)}" in WEB


def test_role_switch_reuses_generation_fencing_and_is_tile_local():
    assert "liveWorkspace.roles[tileIndex]!==role" in WEB
    assert "liveSessions.set(tileIndex,{pc,sessionUrl,token:access.access_token,cameraId,role" in WEB
    assert "async function switchRole(i,role)" in WEB
    assert "await startTile(i,c.id,role)" in WEB
    assert "Promise.allSettled" in WEB


def test_role_switch_security_contract_does_not_leak_grants():
    assert "Authorization:'Bearer '+access.access_token" in WEB
    assert "candidate.origin!==origin" in WEB
    assert "?token=" not in WEB
    assert "localStorage" not in WEB
    assert "sessionStorage" not in WEB


def test_recording_and_ai_configuration_remain_independent_of_tile_role():
    assert "recording_source(camera)" in RECORDING
    assert "camera.main_path" in RECORDING
    recording_path = MEDIA.split("async def add_or_replace_recording_path", 1)[1].split("async def delete_path", 1)[0]
    assert '"record": True' in recording_path
    assert '"sourceOnDemand": False' in recording_path
    assert '"maxReaders"' not in recording_path
    assert 'stream_role: Mapped[str] = mapped_column(String(16), default="sub")' in AI_MODELS
    switch_block = WEB.split("async function switchRole(i,role)", 1)[1].split("function retryTile", 1)[0]
    assert "/ai/cameras/" not in switch_block


def test_live_paths_keep_mediamtx_reader_ceiling():
    assert '"maxReaders": settings.live_view_max_readers_per_path' in MEDIA


def test_distributed_camera_delete_cleans_explicit_main_role_path():
    """Distributed deletion must not orphan the derived explicit-MAIN live path."""
    cameras_router = (ROOT / "services" / "control-api" / "app" / "routers" / "cameras.py").read_text(encoding="utf-8")
    block = cameras_router.split("async def _delete_distributed_paths", 1)[1].split("@router.delete", 1)[0]
    assert 'make_role_stream_key(camera.stream_key, "main")' in block
    assert "for live_role_key in live_role_keys" in block
    assert "operations.add((assignment.node_id, live_role_key))" in block
    assert "operations.add((cleanup_node_id, live_role_key))" in block
