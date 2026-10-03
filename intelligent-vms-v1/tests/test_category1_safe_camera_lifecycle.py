import asyncio
import socket
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from defusedxml import ElementTree as DET

from app.core.auth import Principal
from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.models.schemas import (
    CameraCreate,
    OnvifDiscoverRequest,
    OnvifProbeRequest,
)
from app.routers import cameras, onvif
from app.services import network_policy, onvif_client
from app.services.network_policy import TargetNotAllowed


def principal(*, tenant_id="tenant-a", sites=("site-a",)):
    return Principal(
        subject="operator-1",
        roles=frozenset({"operator"}),
        tenant_id=tenant_id,
        site_ids=frozenset(sites),
    )


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class CameraSession:
    def __init__(self, camera, policy=None):
        self.camera = camera
        self.policy = policy
        self.deleted = False
        self.committed = False
        self.rolled_back = False
        self.added = None

    async def get(self, model, key):
        del model, key
        return self.camera

    async def execute(self, query):
        del query
        return ScalarResult(self.policy)

    def add(self, value):
        self.added = value

    async def flush(self):
        return None

    async def rollback(self):
        self.rolled_back = True

    async def delete(self, value):
        assert value is self.camera
        self.deleted = True

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        del value


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
        username_enc="enc-user",
        password_enc="enc-pass",
        stream_key="site-a-gate",
        media_node_id="media-local-01",
        enabled=True,
        desired_state="provisioned",
    )


def policy(*, enabled=True, mode="continuous"):
    return RecordingPolicyEntity(
        id="policy-1",
        camera_id="camera-1",
        mode=mode,
        enabled=enabled,
        record_stream_key="site-a-gate-record",
        recording_node_id="media-local-01",
        retention_days=7,
        part_duration_ms=1000,
        segment_duration_seconds=900,
        max_part_size_mb=50,
    )


def test_manual_target_rejects_mixed_dns_and_pins_single_answer(monkeypatch):
    monkeypatch.setattr(
        network_policy.settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a":["192.168.0.0/16"]}',
    )

    monkeypatch.setattr(
        network_policy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        ],
    )
    with pytest.raises(TargetNotAllowed):
        network_policy.validate_site_camera_rtsp_target(
            "camera.example",
            554,
            "/main",
            "tenant-a",
            "site-a",
        )

    monkeypatch.setattr(
        network_policy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 0))
        ],
    )
    assert network_policy.validate_site_camera_rtsp_target(
        "camera.example",
        554,
        "/main",
        "tenant-a",
        "site-a",
    ) == ("192.168.1.20", 554, "/main")


def test_public_probe_redacts_service_credentials_query_and_fault_text():
    public = onvif_client.public_probe(
        {
            "xaddr": "http://admin:secret@192.168.1.20/onvif/device?token=x",
            "device_info": {},
            "services": [
                {
                    "namespace": "http://www.onvif.org/ver10/media/wsdl",
                    "xaddr": "http://admin:secret@192.168.1.20/onvif/media?token=x",
                }
            ],
            "features": {},
            "profiles": [],
            "recommended_main_profile_token": None,
            "recommended_sub_profile_token": None,
        }
    )
    rendered = str(public)
    assert "secret" not in rendered
    assert "token=x" not in rendered
    assert public["services"][0]["xaddr"] == "http://192.168.1.20/onvif/media"

    fault = DET.fromstring("<Fault><Reason><Text>synthetic-secret-marker</Text></Reason></Fault>")
    assert "synthetic-secret-marker" not in onvif_client._safe_fault_message(fault)


def test_onvif_probe_and_discovery_enforce_tenant_site_scope(monkeypatch):
    probe_mock = AsyncMock()
    monkeypatch.setattr(onvif, "probe_host", probe_mock)
    wrong_scope = principal(tenant_id="tenant-a", sites=("site-a",))

    with pytest.raises(HTTPException) as probe_error:
        asyncio.run(
            onvif.probe_device(
                OnvifProbeRequest(
                    tenant_id="tenant-b",
                    site_id="site-b",
                    host="192.168.1.20",
                ),
                wrong_scope,
            )
        )
    assert probe_error.value.status_code == 404
    probe_mock.assert_not_awaited()

    with pytest.raises(HTTPException) as discovery_error:
        asyncio.run(
            onvif.discover_devices(
                OnvifDiscoverRequest(
                    tenant_id="tenant-b",
                    site_id="site-b",
                    timeout_seconds=0.5,
                ),
                wrong_scope,
            )
        )
    assert discovery_error.value.status_code == 404


def test_camera_create_media_error_is_redacted(monkeypatch):
    session = CameraSession(camera())
    monkeypatch.setattr(cameras.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(
        cameras,
        "validate_site_camera_rtsp_target",
        lambda host, port, path, tenant_id, site_id: ("192.168.1.20", port, path),
    )
    monkeypatch.setattr(cameras, "encrypt_secret", lambda value: f"enc:{bool(value)}")
    media = AsyncMock(
        side_effect=RuntimeError(
            "rtsp://operator:synthetic-secret-marker@192.168.1.20/main"
        )
    )
    monkeypatch.setattr(cameras.mediamtx, "add_or_replace_path", media)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            cameras.create_camera(
                CameraCreate(
                    tenant_id="tenant-a",
                    site_id="site-a",
                    name="Gate",
                    host="camera.example",
                    rtsp_port=554,
                    main_path="/main",
                    username="operator",
                    password="synthetic-secret-marker",
                ),
                session,
                principal(),
            )
        )
    assert error.value.status_code == 502
    assert "synthetic-secret-marker" not in str(error.value.detail)
    assert session.rolled_back is True


def test_camera_health_allowlists_detail(monkeypatch):
    cam = camera()
    session = CameraSession(cam)
    monkeypatch.setattr(cameras.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(
        cameras.mediamtx,
        "list_paths",
        AsyncMock(
            return_value={
                "items": [
                    {
                        "name": cam.stream_key,
                        "ready": True,
                        "tracks": ["H264", "AAC"],
                        "sourceReady": True,
                        "source": "rtsp://operator:synthetic-secret-marker@192.168.1.20/main",
                    }
                ]
            }
        ),
    )
    result = asyncio.run(cameras.camera_health(cam.id, session, principal()))
    assert result.ready is True
    assert result.tracks == ["H264", "AAC"]
    assert result.detail == {
        "ready": True,
        "tracks": ["H264", "AAC"],
        "sourceReady": True,
    }
    assert "synthetic-secret-marker" not in str(result)


def test_single_node_delete_removes_recording_then_live(monkeypatch):
    cam = camera()
    session = CameraSession(cam, policy())
    monkeypatch.setattr(cameras.settings, "placement_execution_enabled", False)
    delete_path = AsyncMock()
    monkeypatch.setattr(cameras.mediamtx, "delete_path", delete_path)

    asyncio.run(cameras.delete_camera(cam.id, session, principal()))

    assert [call.args[0] for call in delete_path.await_args_list] == [
        "site-a-gate-record",
        "site-a-gate-third",
        "site-a-gate-main",
        "site-a-gate",
    ]
    assert session.deleted is True
    assert session.committed is True


def test_single_node_delete_fails_closed_when_recording_cleanup_fails(monkeypatch):
    cam = camera()
    session = CameraSession(cam, policy())
    monkeypatch.setattr(cameras.settings, "placement_execution_enabled", False)
    delete_path = AsyncMock(side_effect=RuntimeError("synthetic-secret-marker"))
    monkeypatch.setattr(cameras.mediamtx, "delete_path", delete_path)

    with pytest.raises(HTTPException) as error:
        asyncio.run(cameras.delete_camera(cam.id, session, principal()))

    assert error.value.status_code == 503
    assert "synthetic-secret-marker" not in str(error.value.detail)
    assert session.deleted is False
    assert session.committed is False


def test_single_node_delete_skips_disabled_recording_path(monkeypatch):
    cam = camera()
    session = CameraSession(cam, policy(enabled=False))
    monkeypatch.setattr(cameras.settings, "placement_execution_enabled", False)
    delete_path = AsyncMock()
    monkeypatch.setattr(cameras.mediamtx, "delete_path", delete_path)

    asyncio.run(cameras.delete_camera(cam.id, session, principal()))

    assert [call.args[0] for call in delete_path.await_args_list] == [
        "site-a-gate-third",
        "site-a-gate-main",
        "site-a-gate",
    ]


def test_site_network_policy_blocks_cross_site_targets(monkeypatch):
    monkeypatch.setattr(
        network_policy.settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a":["192.168.1.0/24"],"tenant-a/site-b":["192.168.2.0/24"]}',
    )

    assert network_policy.pin_site_http_xaddr(
        "http://192.168.1.20/onvif/device_service",
        "tenant-a",
        "site-a",
    ) == "http://192.168.1.20/onvif/device_service"

    with pytest.raises(TargetNotAllowed):
        network_policy.pin_site_http_xaddr(
            "http://192.168.1.20/onvif/device_service",
            "tenant-a",
            "site-b",
        )

    with pytest.raises(TargetNotAllowed):
        network_policy.pin_site_http_xaddr(
            "http://192.168.1.20/onvif/device_service",
            "tenant-a",
            "site-missing",
        )


def test_discovery_requires_current_process_to_be_local_to_site(monkeypatch):
    monkeypatch.setattr(
        network_policy.settings,
        "onvif_discovery_local_sites",
        "tenant-a/site-a",
    )
    network_policy.require_local_discovery_site("tenant-a", "site-a")
    with pytest.raises(TargetNotAllowed):
        network_policy.require_local_discovery_site("tenant-a", "site-b")


def test_probe_rejects_camera_advertised_cross_site_service(monkeypatch):
    monkeypatch.setattr(
        network_policy.settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a":["192.168.1.0/24"]}',
    )
    device_info = DET.fromstring("<Device/>")
    services = DET.fromstring(
        "<Services><Service>"
        "<Namespace>http://www.onvif.org/ver10/media/wsdl</Namespace>"
        "<XAddr>http://192.168.2.20/onvif/media_service</XAddr>"
        "</Service></Services>"
    )
    capabilities = DET.fromstring("<Capabilities/>")
    soap = AsyncMock(side_effect=[device_info, services, capabilities])
    monkeypatch.setattr(onvif_client, "_soap", soap)

    with pytest.raises(TargetNotAllowed):
        asyncio.run(
            onvif_client.probe_xaddr(
                "http://192.168.1.20/onvif/device_service",
                None,
                None,
                tenant_id="tenant-a",
                site_id="site-a",
            )
        )
    assert soap.await_count == 3


def test_authorized_probe_passes_site_scope_to_client(monkeypatch):
    probe_result = {
        "xaddr": "http://192.168.1.20/onvif/device_service",
        "device_info": {},
        "services": [],
        "features": {},
        "profiles": [],
        "recommended_main_profile_token": None,
        "recommended_sub_profile_token": None,
    }
    probe_mock = AsyncMock(return_value=probe_result)
    monkeypatch.setattr(onvif, "probe_host", probe_mock)

    result = asyncio.run(
        onvif.probe_device(
            OnvifProbeRequest(
                tenant_id="tenant-a",
                site_id="site-a",
                host="192.168.1.20",
            ),
            principal(),
        )
    )

    assert result["xaddr"] == "http://192.168.1.20/onvif/device_service"
    assert probe_mock.await_args.kwargs == {
        "tenant_id": "tenant-a",
        "site_id": "site-a",
    }


def test_discovery_denies_nonlocal_site_before_socket(monkeypatch):
    monkeypatch.setattr(
        network_policy.settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a":["192.168.1.0/24"]}',
    )
    monkeypatch.setattr(
        network_policy.settings,
        "onvif_discovery_local_sites",
        "",
    )
    called = {"value": False}

    def unexpected_discover(_timeout):
        called["value"] = True
        return []

    monkeypatch.setattr(onvif, "discover", unexpected_discover)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            onvif.discover_devices(
                OnvifDiscoverRequest(
                    tenant_id="tenant-a",
                    site_id="site-a",
                    timeout_seconds=0.5,
                ),
                principal(),
            )
        )

    assert error.value.status_code == 400
    assert error.value.detail["code"] == "TARGET_NOT_ALLOWED"
    assert called["value"] is False


def test_probe_rejects_credential_bearing_advertised_service(monkeypatch):
    monkeypatch.setattr(
        network_policy.settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a":["192.168.1.0/24"]}',
    )
    device_info = DET.fromstring("<Device/>")
    services = DET.fromstring(
        "<Services><Service>"
        "<Namespace>http://www.onvif.org/ver10/media/wsdl</Namespace>"
        "<XAddr>http://admin:synthetic-secret-marker@192.168.1.20/onvif/media</XAddr>"
        "</Service></Services>"
    )
    capabilities = DET.fromstring("<Capabilities/>")
    soap = AsyncMock(side_effect=[device_info, services, capabilities])
    monkeypatch.setattr(onvif_client, "_soap", soap)

    with pytest.raises(TargetNotAllowed) as error:
        asyncio.run(
            onvif_client.probe_xaddr(
                "http://192.168.1.20/onvif/device_service",
                None,
                None,
                tenant_id="tenant-a",
                site_id="site-a",
            )
        )

    assert "synthetic-secret-marker" not in str(error.value)


def test_probe_infers_only_unambiguous_single_site(monkeypatch):
    probe_result = {
        "xaddr": "http://192.168.1.20/onvif/device_service",
        "device_info": {},
        "services": [],
        "features": {},
        "profiles": [],
        "recommended_main_profile_token": None,
        "recommended_sub_profile_token": None,
    }
    probe_mock = AsyncMock(return_value=probe_result)
    monkeypatch.setattr(onvif, "probe_host", probe_mock)

    asyncio.run(
        onvif.probe_device(
            OnvifProbeRequest(
                tenant_id="tenant-a",
                host="192.168.1.20",
            ),
            principal(sites=("site-a",)),
        )
    )
    assert probe_mock.await_args.kwargs["site_id"] == "site-a"

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            onvif.probe_device(
                OnvifProbeRequest(
                    tenant_id="tenant-a",
                    host="192.168.1.20",
                ),
                principal(sites=("site-a", "site-b")),
            )
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "SITE_REQUIRED"
