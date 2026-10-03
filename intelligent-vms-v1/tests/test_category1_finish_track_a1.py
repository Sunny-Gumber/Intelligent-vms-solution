import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from defusedxml import ElementTree as DET

from app.core.auth import Principal
from app.models.entities import CameraEntity
from app.models.schemas import (
    OnvifCodecProfileSelect,
    OnvifQrOnboardRequest,
    OnvifSerialOnboardRequest,
)
from app.routers import cameras, onvif as onvif_router
from app.services import camera_lifecycle, onvif_configuration
from app.services.camera_drivers import DriverDescriptor, DriverRegistry
from app.services.network_policy import TargetNotAllowed, pin_site_rtsp_uri
from app.services.onvif_client import OnvifError
from app.services.onvif_onboarding import QR_PREFIX, connection_from_xaddr, decode_qr_payload
from app.services.stream_keys import make_role_stream_key


def principal():
    return Principal(
        subject="operator-1",
        roles=frozenset({"operator"}),
        tenant_id="tenant-a",
        site_ids=frozenset({"site-a"}),
    )


def camera():
    return CameraEntity(
        id="camera-1",
        tenant_id="tenant-a",
        site_id="site-a",
        name="Gate",
        host="192.168.1.20",
        rtsp_port=554,
        main_path="/main",
        sub_path="/sub",
        third_path="/third",
        third_stream_key="site-a-gate-third",
        username_enc=None,
        password_enc=None,
        stream_key="site-a-gate",
        media_node_id="media-local-01",
        enabled=True,
        desired_state="provisioned",
        created_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )


def encoded_qr(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return QR_PREFIX + encoded


def test_qr_payload_decodes_without_credentials():
    value = encoded_qr(
        {
            "tenant_id": "tenant-a",
            "site_id": "site-a",
            "name": "Gate",
            "host": "192.168.1.20",
            "port": 80,
            "scheme": "http",
            "device_service_path": "/onvif/device_service",
        }
    )

    decoded = decode_qr_payload(value)

    assert decoded["host"] == "192.168.1.20"
    assert "username" not in decoded and "password" not in decoded


def test_qr_payload_rejects_secret_fields():
    value = encoded_qr(
        {
            "tenant_id": "tenant-a",
            "site_id": "site-a",
            "name": "Gate",
            "host": "192.168.1.20",
            "password": "synthetic-secret-marker",
        }
    )

    with pytest.raises(OnvifError) as error:
        decode_qr_payload(value)

    assert error.value.code == "INVALID_QR_PAYLOAD"
    assert "synthetic-secret-marker" not in error.value.message


def test_connection_from_xaddr_uses_safe_connection_fields_only():
    result = connection_from_xaddr(
        "https://192.168.1.20:8443/onvif/device_service"
    )

    assert result == {
        "host": "192.168.1.20",
        "port": 8443,
        "scheme": "https",
        "device_service_path": "/onvif/device_service",
    }


def test_connection_from_xaddr_rejects_credentials_query_and_fragment():
    for value in (
        "http://user:secret@192.168.1.20/onvif/device_service",
        "http://192.168.1.20/onvif/device_service?token=secret",
        "http://192.168.1.20/onvif/device_service#secret",
    ):
        with pytest.raises(OnvifError) as error:
            connection_from_xaddr(value)
        assert error.value.code == "DEVICE_SERVICE_INVALID"
        assert "secret" not in error.value.message


def test_role_stream_key_is_stable_and_bounded():
    assert make_role_stream_key("site-a-gate-12345678", "third") == (
        "site-a-gate-12345678-third"
    )


def test_single_node_source_lifecycle_provisions_third_path(monkeypatch):
    cam = camera()
    calls = []
    monkeypatch.setattr(
        camera_lifecycle.mediamtx,
        "add_or_replace_path",
        AsyncMock(side_effect=lambda key, source: calls.append((key, source))),
    )
    monkeypatch.setattr(camera_lifecycle, "decrypt_secret", lambda value: None)

    asyncio.run(camera_lifecycle.apply_single_node_source(cam, None))

    assert [key for key, _source in calls] == [
        cam.stream_key,
        make_role_stream_key(cam.stream_key, "main"),
        cam.third_stream_key,
    ]
    assert calls[1][1].endswith("/main")
    assert calls[2][1].endswith("/third")


def test_camera_read_exposes_third_public_urls(monkeypatch):
    cam = camera()
    monkeypatch.setattr(cameras.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(
        cameras.settings,
        "mediamtx_webrtc_public_base",
        "https://media.example/webrtc",
    )
    monkeypatch.setattr(
        cameras.settings,
        "mediamtx_hls_public_base",
        "https://media.example/hls",
    )

    result = cameras.to_read(cam)

    assert result.third_stream_key == cam.third_stream_key
    assert result.third_webrtc_url.endswith("/site-a-gate-third")
    assert result.third_hls_url.endswith("/site-a-gate-third")


def test_serial_onboarding_requires_exact_match_and_reuses_onboard(monkeypatch):
    monkeypatch.setattr(onvif_router, "require_site_network_policy", lambda *_args: None)
    monkeypatch.setattr(onvif_router, "require_local_discovery_site", lambda *_args: None)
    monkeypatch.setattr(
        onvif_router,
        "pin_site_http_xaddr",
        lambda value, *_args: value,
    )
    monkeypatch.setattr(
        onvif_router,
        "discover",
        lambda _timeout: [
            {"xaddrs": ["http://192.168.1.20/onvif/device_service"]}
        ],
    )
    monkeypatch.setattr(
        onvif_router,
        "identify_xaddr",
        AsyncMock(
            return_value={
                "xaddr": "http://192.168.1.20/onvif/device_service",
                "device_info": {"SerialNumber": "SN-001"},
            }
        ),
    )
    created = SimpleNamespace(id="camera-created")
    onboard = AsyncMock(return_value=created)
    monkeypatch.setattr(onvif_router, "onboard_device", onboard)

    result = asyncio.run(
        onvif_router.onboard_by_serial(
            OnvifSerialOnboardRequest(
                tenant_id="tenant-a",
                site_id="site-a",
                name="Gate",
                serial_number="SN-001",
            ),
            object(),
            principal(),
        )
    )

    assert result is created
    request = onboard.await_args.args[0]
    assert request.host == "192.168.1.20"
    assert request.device_service_path == "/onvif/device_service"


def test_qr_onboarding_keeps_credentials_outside_qr(monkeypatch):
    value = encoded_qr(
        {
            "tenant_id": "tenant-a",
            "site_id": "site-a",
            "name": "Gate",
            "host": "192.168.1.20",
            "port": 80,
            "scheme": "http",
            "device_service_path": "/onvif/device_service",
        }
    )
    created = SimpleNamespace(id="camera-created")
    onboard = AsyncMock(return_value=created)
    monkeypatch.setattr(onvif_router, "onboard_device", onboard)

    result = asyncio.run(
        onvif_router.onboard_by_qr(
            OnvifQrOnboardRequest(
                qr_payload=value,
                username="operator",
                password="synthetic-secret-marker",
            ),
            object(),
            principal(),
        )
    )

    assert result is created
    request = onboard.await_args.args[0]
    assert request.password == "synthetic-secret-marker"
    assert "synthetic-secret-marker" not in value


def test_codec_profile_selection_maps_mjpeg_to_jpeg(monkeypatch):
    cam = camera()
    capability = SimpleNamespace(
        main_profile_token="main-h264",
        sub_profile_token="sub-h264",
        third_profile_token=None,
        onvif_xaddr="http://192.168.1.20/onvif/device_service",
    )
    monkeypatch.setattr(
        onvif_router,
        "_camera_configuration_context",
        AsyncMock(return_value=(cam, capability, None, None)),
    )
    monkeypatch.setattr(
        onvif_router,
        "probe_xaddr",
        AsyncMock(
            return_value={
                "profiles": [
                    {
                        "token": "jpeg-low",
                        "encoding": "JPEG",
                        "width": 640,
                        "height": 360,
                        "bitrate_kbps": 512,
                        "fps": 10,
                        "_raw_stream_uri": "rtsp://192.168.1.20/jpeg",
                    }
                ]
            }
        ),
    )
    apply_profile = AsyncMock(return_value=SimpleNamespace(camera_id=cam.id))
    monkeypatch.setattr(onvif_router, "_apply_managed_profile", apply_profile)

    asyncio.run(
        onvif_router.select_profile_by_codec(
            cam.id,
            "third",
            OnvifCodecProfileSelect(encoding="MJPEG"),
            object(),
            principal(),
        )
    )

    assert apply_profile.await_args.args[1] == "jpeg-low"


def test_composite_vertical_flip_writes_rotation_and_mirror(monkeypatch):
    config_root = DET.fromstring(
        """
        <Envelope><Configuration token="src-conf-main">
          <Extension>
            <Rotate><Mode>OFF</Mode><Degree>0</Degree></Rotate>
            <Mirror>false</Mirror>
          </Extension>
        </Configuration></Envelope>
        """
    )
    initial = {
        "current": {"flip": False},
        "options": {
            "rotation_modes": ["OFF", "ON"],
            "rotation_degrees": [0, 180],
            "mirror_supported": True,
            "flip_supported": True,
        },
        "_configuration_root": config_root,
        "_media_xaddr": "http://192.168.1.20/onvif/media",
    }
    readback = {"current": {"flip": True}, "options": initial["options"]}
    monkeypatch.setattr(
        onvif_configuration,
        "get_orientation",
        AsyncMock(side_effect=[initial, readback]),
    )
    soap = AsyncMock(return_value=DET.fromstring("<Envelope/>"))
    monkeypatch.setattr(onvif_configuration, "_soap", soap)

    result = asyncio.run(
        onvif_configuration.set_orientation(
            [],
            {"token": "profile-main"},
            {"flip": True},
            None,
            None,
            "tenant-a",
            "site-a",
        )
    )

    body = soap.await_args.args[2]
    assert "<Degree>180</Degree>" in body
    assert ">true</" in body
    assert result["current"]["flip"] is True


def test_driver_registry_requires_explicit_adapter_match():
    class ExampleDriver:
        descriptor = DriverDescriptor(
            driver_id="example",
            manufacturer="Example",
            display_name="Example adapter",
            version="1.0",
            capabilities=("onboard",),
        )

        def matches(self, manufacturer, model=None):
            return manufacturer.casefold() == "example"

    registry = DriverRegistry()
    registry.register(ExampleDriver())

    assert registry.resolve("Example") is not None
    assert registry.resolve("Other") is None
    assert registry.descriptors()[0].driver_id == "example"


def test_fencing_execution_keys_include_third_media_path():
    from app.services.fencing import _execution_keys

    cam = camera()

    assert _execution_keys(cam, "media", None) == [
        cam.stream_key,
        make_role_stream_key(cam.stream_key, "main"),
        cam.third_stream_key,
    ]


def test_node_agent_revocation_deletes_all_media_execution_keys(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    module_path = (
        Path(__file__).parents[1] / "services" / "node-agent" / "main.py"
    )
    spec = importlib.util.spec_from_file_location("node_agent_category1_finish", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    settings = module.NodeAgentSettings(
        NODE_ID="node-a",
        REGION_ID="region-a",
        NODE_ROLES=["media"],
        NODE_AGENT_TOKEN="test-token",
        CONTROL_API_URL="http://control-api:8000",
        MEDIAMTX_API_URL="http://mediamtx:9997",
        FENCE_STATE_PATH=str(tmp_path / "fence.json"),
    )
    agent = module.NodeAgent(settings)
    deleted = []

    async def fake_delete(role, key):
        deleted.append((role, key))
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", fake_delete)
    monkeypatch.setattr(agent, "_ack_revocation", AsyncMock(return_value=True))

    snapshot = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "node_id": "node-a",
        "assignments": [],
        "revocations": [
            {
                "revocation_id": "rev-1",
                "assignment_id": "assign-1",
                "camera_id": "camera-1",
                "role": "media",
                "revoked_generation": 2,
                "execution_key": "site-a-gate",
                "execution_keys": ["site-a-gate", "site-a-gate-third"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    asyncio.run(agent._apply_fence_snapshot(snapshot))
    asyncio.run(agent.close())

    assert deleted == [
        ("media", "site-a-gate"),
        ("media", "site-a-gate-third"),
    ]


def test_single_node_source_lifecycle_removes_stale_third_path(monkeypatch):
    cam = camera()
    cam.third_path = None
    deleted = []
    monkeypatch.setattr(
        camera_lifecycle.mediamtx,
        "add_or_replace_path",
        AsyncMock(),
    )
    monkeypatch.setattr(
        camera_lifecycle.mediamtx,
        "delete_path",
        AsyncMock(side_effect=lambda key: deleted.append(key)),
    )
    monkeypatch.setattr(camera_lifecycle, "decrypt_secret", lambda value: None)

    asyncio.run(camera_lifecycle.apply_single_node_source(cam, None))

    assert deleted == [cam.third_stream_key]


def test_serial_onboarding_returns_not_found_for_nonmatching_identity(monkeypatch):
    monkeypatch.setattr(onvif_router, "require_site_network_policy", lambda *_args: None)
    monkeypatch.setattr(onvif_router, "require_local_discovery_site", lambda *_args: None)
    monkeypatch.setattr(
        onvif_router,
        "pin_site_http_xaddr",
        lambda value, *_args: value,
    )
    monkeypatch.setattr(
        onvif_router,
        "discover",
        lambda _timeout: [
            {"xaddrs": ["http://192.168.1.21/onvif/device_service"]}
        ],
    )
    monkeypatch.setattr(
        onvif_router,
        "identify_xaddr",
        AsyncMock(
            return_value={
                "xaddr": "http://192.168.1.21/onvif/device_service",
                "device_info": {"SerialNumber": "OTHER"},
            }
        ),
    )

    with pytest.raises(Exception) as error:
        asyncio.run(
            onvif_router.onboard_by_serial(
                OnvifSerialOnboardRequest(
                    tenant_id="tenant-a",
                    site_id="site-a",
                    name="Gate",
                    serial_number="SN-001",
                ),
                object(),
                principal(),
            )
        )

    assert getattr(error.value, "status_code", None) == 404
    assert error.value.detail["code"] == "SERIAL_NOT_FOUND"


def test_placement_revocation_snapshots_old_media_execution_keys():
    from app.models.placement import PlacementAssignmentEntity, PlacementRevocationEntity
    from app.services import placement

    cam = camera()
    assignment = PlacementAssignmentEntity(
        id="assign-1",
        camera_id=cam.id,
        role="media",
        region_id="region-a",
        node_id="node-a",
        generation=3,
        active=True,
        reason="initial",
        lease_expires_at=datetime.now(timezone.utc),
        assigned_at=datetime.now(timezone.utc),
    )

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class FakeSession:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def execute(self, _query):
            return EmptyResult()

    session = FakeSession()
    node = placement.NodeSnapshot(
        id="node-b",
        region_id="region-a",
        roles=frozenset({"media"}),
        state="active",
        enabled=True,
        capacity={},
        load={},
        heartbeat_at=datetime.now(timezone.utc),
        authority_mode="central_online",
    )

    asyncio.run(
        placement._assign(
            session,
            cam,
            "media",
            "region-a",
            node,
            assignment,
            reason="failover",
            now=datetime.now(timezone.utc),
        )
    )

    revocations = [
        value for value in session.added if isinstance(value, PlacementRevocationEntity)
    ]
    assert len(revocations) == 1
    assert revocations[0].execution_keys_json == [
        cam.stream_key,
        make_role_stream_key(cam.stream_key, "main"),
        cam.third_stream_key,
    ]


def test_distributed_source_refresh_replaces_existing_third_stream(monkeypatch):
    from app.models.placement import PlacementAssignmentEntity
    from app.services import reconciler

    cam = camera()
    assignment = PlacementAssignmentEntity(
        id="assign-1",
        camera_id=cam.id,
        role="media",
        region_id="region-a",
        node_id="node-a",
        generation=4,
        applied_generation=None,
        active=True,
        reason="source-refresh",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        assigned_at=datetime.now(timezone.utc),
    )
    client = SimpleNamespace(
        list_config_paths=AsyncMock(
            return_value={
                "items": [
                    {"name": cam.stream_key},
                    {"name": make_role_stream_key(cam.stream_key, "main")},
                    {"name": cam.third_stream_key},
                ]
            }
        ),
        add_or_replace_path=AsyncMock(),
        delete_path=AsyncMock(),
    )
    monkeypatch.setattr(
        reconciler.node_clients,
        "media",
        AsyncMock(return_value=client),
    )
    monkeypatch.setattr(reconciler, "decrypt_secret", lambda value: None)

    changed, failed, _last = asyncio.run(
        reconciler._distributed_reconcile(
            [cam],
            {},
            {(cam.id, "media"): assignment},
            {"node-a": SimpleNamespace(id="node-a")},
            max_changes=10,
        )
    )

    refreshed = [call.args[0] for call in client.add_or_replace_path.await_args_list]
    assert refreshed == [
        cam.stream_key,
        make_role_stream_key(cam.stream_key, "main"),
        cam.third_stream_key,
    ]
    assert changed == 3
    assert failed == 0
    assert assignment.applied_generation == assignment.generation
    assert cam.desired_state == "provisioned"


def test_distributed_third_refresh_failure_keeps_generation_unapplied(monkeypatch):
    from app.models.placement import PlacementAssignmentEntity
    from app.services import reconciler

    cam = camera()
    assignment = PlacementAssignmentEntity(
        id="assign-1",
        camera_id=cam.id,
        role="media",
        region_id="region-a",
        node_id="node-a",
        generation=5,
        applied_generation=None,
        active=True,
        reason="source-refresh",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        assigned_at=datetime.now(timezone.utc),
    )

    async def replace_path(key, _source):
        if key == cam.third_stream_key:
            raise RuntimeError("synthetic third refresh failure")

    client = SimpleNamespace(
        list_config_paths=AsyncMock(
            return_value={
                "items": [
                    {"name": cam.stream_key},
                    {"name": cam.third_stream_key},
                ]
            }
        ),
        add_or_replace_path=AsyncMock(side_effect=replace_path),
        delete_path=AsyncMock(),
    )
    monkeypatch.setattr(
        reconciler.node_clients,
        "media",
        AsyncMock(return_value=client),
    )
    monkeypatch.setattr(reconciler, "decrypt_secret", lambda value: None)

    _changed, failed, _last = asyncio.run(
        reconciler._distributed_reconcile(
            [cam],
            {},
            {(cam.id, "media"): assignment},
            {"node-a": SimpleNamespace(id="node-a")},
            max_changes=10,
        )
    )

    assert failed == 1
    assert assignment.applied_generation is None
    assert cam.desired_state != "provisioned"



def test_site_rtsp_pinning_rejects_camera_supplied_credentials(monkeypatch):
    from app.services import network_policy

    monkeypatch.setattr(
        network_policy,
        "resolve_site_pinned_host",
        lambda *_args: "192.168.1.20",
    )

    with pytest.raises(TargetNotAllowed) as error:
        pin_site_rtsp_uri(
            "rtsp://device-user:synthetic-secret-marker@camera.local/live",
            "tenant-a",
            "site-a",
        )

    assert "synthetic-secret-marker" not in str(error.value)


def test_onboard_cleanup_attempts_all_paths_and_redacts_exception_text(monkeypatch, caplog):
    secret = "synthetic-secret-marker"
    delete_path = AsyncMock(
        side_effect=[RuntimeError(f"upstream failed with {secret}"), None]
    )
    monkeypatch.setattr(onvif_router.mediamtx, "delete_path", delete_path)

    asyncio.run(
        onvif_router._cleanup_provisioned_paths(
            ["site-a-gate", "site-a-gate-third"]
        )
    )

    assert [call.args[0] for call in delete_path.await_args_list] == [
        "site-a-gate-third",
        "site-a-gate",
    ]
    assert "onvif_onboard_cleanup_failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text
