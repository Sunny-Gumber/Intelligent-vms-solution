"""F21: expected ONVIF HTTP errors must not be rewritten as HTTP 502.

Configuration routes catch Exception and call _http_error. _managed_profile
raises HTTPException for a bad role, so an expected 422 became 502. Device and
service HTTPException details must be discarded on every ONVIF route. These
tests use an in-memory database and fakes only. Device transport is blocked.
"""

import ast
import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal, get_principal
from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.security import encrypt_secret
from app.db.base import Base
from app.db.session import get_session
from app.models.entities import CameraCapabilityEntity, CameraEntity
from app.routers import onvif as onvif_router
from app.services import onvif_client
from app.services import onvif_configuration as onvif_config
from app.services.network_policy import TargetNotAllowed
from app.services.onvif_client import OnvifError

CAMERA_ID = "cam-fix-021"
HOST = "192.0.2.20"
USERNAME = "synthetic-onvif-user"
SECRET = "synthetic-secret-marker"
ROLE = "not-a-role"
DEVICE_FAULT = (
    "raw-device-body from "
    f"http://{USERNAME}:{SECRET}@{HOST}/onvif/device_service"
)
LEAK = (
    "raw-device-body from "
    f"http://{USERNAME}:{SECRET}@{HOST}/onvif/device_service "
    f"<wsse:Password>{SECRET}</wsse:Password> token=synthetic-token-marker"
)
MARKERS = (
    SECRET,
    USERNAME,
    HOST,
    "raw-device-body",
    "/onvif/device_service",
    "wsse:Password",
    "synthetic-token-marker",
)
ROLE_CODE = "INVALID_STREAM_ROLE"
ROLE_MESSAGE = "Role must be main, sub or third"
STALE_CODE = "CAPABILITY_REFRESH_REQUIRED"
STALE_MESSAGE = "Selected profile is not present in the stored ONVIF snapshot"
FORGED = {"code": STALE_CODE, "message": LEAK}


class _BlockedAsyncClient:
    """Refuse any real ONVIF HTTP call made during these tests."""

    def __init__(self, *_args, **_kwargs):
        raise AssertionError("ONVIF device transport is not used in this test")


def _principal() -> Principal:
    return Principal("synthetic-actor", frozenset({"admin"}), "tenant-a", frozenset({"site-a"}))


def _camera() -> CameraEntity:
    return CameraEntity(
        id=CAMERA_ID,
        tenant_id="tenant-a",
        site_id="site-a",
        name="Gate",
        host=HOST,
        main_path="/main",
        stream_key="gate-fix-021",
        username_enc=encrypt_secret(USERNAME),
        password_enc=encrypt_secret(SECRET),
    )


def _capability(mode: str, services: list | None = None) -> CameraCapabilityEntity | None:
    if mode == "absent":
        return None
    profile = {
        "token": "profile-main",
        "video_encoder_configuration_token": "enc-main",
        "video_source_token": "vs-1",
    }
    token = "profile-main"
    sub_token = None
    if mode == "ready":
        profile["video_source_configuration_token"] = "vsc-1"
    elif mode == "stale":
        token = "stale-token"
    elif mode == "no-source-token":
        pass
    elif mode == "unselected":
        token = None
    elif mode == "role-conflict":
        profile["video_source_configuration_token"] = "vsc-1"
        sub_token = "profile-sub"
    else:
        raise AssertionError(mode)
    return CameraCapabilityEntity(
        id="cap-fix-021",
        camera_id=CAMERA_ID,
        onvif_xaddr=f"http://{HOST}/onvif/device_service",
        device_info_json={},
        services_json=list(services or []),
        features_json={},
        profiles_json=[profile],
        main_profile_token=token,
        sub_profile_token=sub_token,
        probed_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
    )


@asynccontextmanager
async def _api(monkeypatch, mode: str = "ready", services: list | None = None):
    # Patch the ONVIF module's client only. The test transport keeps the real class.
    real_client = httpx.AsyncClient
    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", _BlockedAsyncClient)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(onvif_router.router)

    async def identity():
        return _principal()

    async def session_dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_principal] = identity
    app.dependency_overrides[get_session] = session_dependency
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(_camera())
            capability = _capability(mode, services)
            if capability is not None:
                session.add(capability)
            await session.commit()
        transport = httpx.ASGITransport(app=app)
        async with real_client(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await engine.dispose()


def _url(template: str, role: str = ROLE) -> str:
    return template.format(camera_id=CAMERA_ID, role=role)


def _assert_error(response: httpx.Response, status: int, code: str, message: str) -> None:
    for marker in MARKERS:
        assert marker not in response.text
    assert response.status_code == status
    body = response.json()
    assert body["detail"] == {"code": code, "message": message}
    assert body["error"]["code"] == code
    assert body["error"]["message"] == message


async def _send(client: httpx.AsyncClient, method: str, template: str, body, role: str = ROLE):
    return await client.request(method, _url(template, role), json=body)


def _run(scenario) -> None:
    asyncio.run(scenario())


MANAGED_ROLE_ROUTES = [
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/encoder/{role}", None, id="get-encoder"),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/encoder/{role}",
        {"width": 1280, "height": 720},
        id="put-encoder",
    ),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/imaging/{role}", None, id="get-imaging"),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/imaging/{role}",
        {"brightness": 50},
        id="put-imaging",
    ),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/orientation/{role}", None, id="get-orientation"),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/orientation/{role}",
        {"flip": True},
        id="put-orientation",
    ),
    pytest.param(
        "GET",
        "/api/v1/onvif/cameras/{camera_id}/video-source-modes/{role}",
        None,
        id="get-video-source-modes",
    ),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/video-source-modes/{role}",
        {"mode_token": "mode-a"},
        id="put-video-source-mode",
    ),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/video-standard/{role}", None, id="get-video-standard"),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/video-standard/{role}",
        {"standard": "PAL"},
        id="put-video-standard",
    ),
    pytest.param(
        "POST",
        "/api/v1/onvif/cameras/{camera_id}/osds?role={role}",
        {"text": "Gate"},
        id="post-osd",
    ),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/osds/camera-name?role={role}",
        {},
        id="put-camera-name-osd",
    ),
    pytest.param(
        "GET",
        "/api/v1/onvif/cameras/{camera_id}/privacy-masks/{role}",
        None,
        id="get-privacy-masks",
    ),
    pytest.param(
        "POST",
        "/api/v1/onvif/cameras/{camera_id}/privacy-masks/{role}",
        {"points": [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5]]},
        id="post-privacy-mask",
    ),
    pytest.param(
        "PATCH",
        "/api/v1/onvif/cameras/{camera_id}/privacy-masks/{role}/mask-1",
        {"enabled": False},
        id="patch-privacy-mask",
    ),
    pytest.param(
        "DELETE",
        "/api/v1/onvif/cameras/{camera_id}/privacy-masks/{role}/mask-1",
        None,
        id="delete-privacy-mask",
    ),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/profiles/{role}",
        {"profile_token": "profile-main"},
        id="put-profile-role",
    ),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/profiles/{role}/codec",
        {"encoding": "H264"},
        id="put-codec-role",
    ),
]

MISSING_SNAPSHOT_ROUTES = [
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/encoder/main", None, id="encoder"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/imaging/main", None, id="imaging"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/osds", None, id="osds"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/privacy-masks/main", None, id="privacy-masks"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/date-time", None, id="date-time"),
]

STALE_PROFILE_ROUTES = [
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/encoder/main", None, id="encoder"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/imaging/main", None, id="imaging"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/orientation/main", None, id="orientation"),
    pytest.param(
        "POST",
        "/api/v1/onvif/cameras/{camera_id}/osds?role=main",
        {"text": "Gate"},
        id="osd",
    ),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/privacy-masks/main", None, id="privacy-masks"),
]

MISSING_SOURCE_TOKEN_ROUTES = [
    pytest.param(
        "POST",
        "/api/v1/onvif/cameras/{camera_id}/osds?role=main",
        {"text": "Gate"},
        id="osd",
    ),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/osds/camera-name?role=main",
        {},
        id="camera-name-osd",
    ),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/privacy-masks/main", None, id="get-privacy-masks"),
    pytest.param(
        "POST",
        "/api/v1/onvif/cameras/{camera_id}/privacy-masks/main",
        {"points": [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5]]},
        id="post-privacy-mask",
    ),
]

DEVICE_FAILURE_ROUTES = [
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/encoder/main", None, "get_encoder", id="encoder"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/imaging/main", None, "get_imaging", id="imaging"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/osds", None, "list_osds", id="osds"),
    pytest.param(
        "GET",
        "/api/v1/onvif/cameras/{camera_id}/privacy-masks/main",
        None,
        "list_masks",
        id="privacy-masks",
    ),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/date-time", None, "get_date_time", id="date-time"),
]

PROBE_BODY = {
    "tenant_id": "tenant-a",
    "site_id": "site-a",
    "host": HOST,
    "username": USERNAME,
    "password": SECRET,
}

PASSTHROUGH_ROUTES = [
    pytest.param("POST", "/api/v1/onvif/probe", PROBE_BODY, "probe_host", id="probe"),
    pytest.param(
        "DELETE",
        "/api/v1/onvif/cameras/{camera_id}/profiles/third",
        None,
        "commit_source_mutation",
        id="clear-third-profile",
    ),
    pytest.param(
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/profiles/main/codec",
        {"encoding": "H264"},
        "probe_xaddr",
        id="codec-select",
    ),
    pytest.param(
        "GET",
        "/api/v1/onvif/cameras/{camera_id}/date-time",
        None,
        "get_date_time",
        id="date-time",
    ),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/ir", None, "supported_auxiliary_commands", id="ir"),
    pytest.param("GET", "/api/v1/onvif/cameras/{camera_id}/osds", None, "list_osds", id="osd-list"),
    pytest.param(
        "PATCH",
        "/api/v1/onvif/cameras/{camera_id}/osds/osd-1",
        {"text": "Gate"},
        "update_osd",
        id="osd-update",
    ),
    pytest.param(
        "DELETE",
        "/api/v1/onvif/cameras/{camera_id}/osds/osd-1",
        None,
        "delete_osd",
        id="osd-delete",
    ),
    pytest.param(
        "POST",
        "/api/v1/onvif/cameras/{camera_id}/refresh",
        None,
        "probe_xaddr",
        id="refresh",
    ),
]


@pytest.mark.parametrize(("method", "template", "body"), MANAGED_ROLE_ROUTES)
def test_invalid_managed_role_stays_422(monkeypatch, method, template, body):
    """An unknown stream role stays 422 on every route that checks it."""

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await _send(client, method, template, body)
        _assert_error(response, 422, ROLE_CODE, ROLE_MESSAGE)

    _run(scenario)


@pytest.mark.parametrize(("method", "template", "body"), MISSING_SNAPSHOT_ROUTES)
def test_missing_capability_snapshot_stays_404(monkeypatch, method, template, body):
    """A camera with no stored ONVIF snapshot stays 404."""

    async def scenario():
        async with _api(monkeypatch, mode="absent") as client:
            response = await _send(client, method, template, body, role="main")
        _assert_error(
            response,
            404,
            "ONVIF_CAPABILITY_NOT_FOUND",
            "ONVIF capability snapshot not found",
        )

    _run(scenario)


@pytest.mark.parametrize(("method", "template", "body"), STALE_PROFILE_ROUTES)
def test_stale_profile_stays_409(monkeypatch, method, template, body):
    """A selected profile missing from the snapshot stays 409."""

    async def scenario():
        async with _api(monkeypatch, mode="stale") as client:
            response = await _send(client, method, template, body, role="main")
        _assert_error(response, 409, STALE_CODE, STALE_MESSAGE)

    _run(scenario)


@pytest.mark.parametrize(("method", "template", "body"), MISSING_SOURCE_TOKEN_ROUTES)
def test_missing_source_configuration_token_stays_409(monkeypatch, method, template, body):
    """OSD and privacy-mask routes keep the designed 409 when the source token is absent."""

    async def scenario():
        async with _api(monkeypatch, mode="no-source-token") as client:
            response = await _send(client, method, template, body, role="main")
        _assert_error(
            response,
            409,
            STALE_CODE,
            "Video source configuration token is missing; refresh capabilities",
        )

    _run(scenario)


def test_unselected_profile_stays_422(monkeypatch):
    """A managed role with no selected profile stays the designed 422."""

    async def scenario():
        async with _api(monkeypatch, mode="unselected") as client:
            response = await _send(
                client,
                "GET",
                "/api/v1/onvif/cameras/{camera_id}/encoder/main",
                None,
                role="main",
            )
        _assert_error(
            response,
            422,
            "UNSUPPORTED_CAPABILITY",
            "Requested managed stream role has no selected profile",
        )

    _run(scenario)


@pytest.mark.parametrize(("method", "template", "body", "target"), DEVICE_FAILURE_ROUTES)
def test_device_failure_stays_generic_502(monkeypatch, method, template, body, target):
    """A device or transport failure stays 502 and does not echo the device body."""
    called = {"value": False}

    async def device_fault(*_args, **_kwargs):
        called["value"] = True
        raise RuntimeError(DEVICE_FAULT)

    monkeypatch.setattr(onvif_router, target, device_fault)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await _send(client, method, template, body, role="main")
        _assert_error(response, 502, "ONVIF_ERROR", "ONVIF operation failed")
        assert called["value"] is True

    _run(scenario)


def test_onvif_error_status_stays_on_configuration_route(monkeypatch):
    """A bounded OnvifError keeps its status instead of becoming a generic 502."""

    async def unsupported(*_args, **_kwargs):
        raise OnvifError("UNSUPPORTED_CAPABILITY", "Camera did not advertise media service", 422)

    monkeypatch.setattr(onvif_router, "get_encoder", unsupported)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await _send(
                client,
                "GET",
                "/api/v1/onvif/cameras/{camera_id}/encoder/main",
                None,
                role="main",
            )
        _assert_error(response, 422, "UNSUPPORTED_CAPABILITY", "Camera did not advertise media service")

    _run(scenario)


@pytest.mark.parametrize(("method", "template", "body", "target"), PASSTHROUGH_ROUTES)
def test_service_http_exception_is_sanitized(monkeypatch, method, template, body, target):
    """A service HTTPException is not trusted, even when its status looks like 409."""

    async def leaked(*_args, **_kwargs):
        raise HTTPException(409, {"code": STALE_CODE, "message": DEVICE_FAULT})

    monkeypatch.setattr(onvif_router, target, leaked)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await _send(client, method, template, body, role="main")
        _assert_error(response, 502, "ONVIF_ERROR", "ONVIF operation failed")

    _run(scenario)


def test_http_error_sanitizes_device_http_exception():
    """_http_error drops a device HTTPException instead of returning its detail."""
    original = HTTPException(502, DEVICE_FAULT)
    http = onvif_router._http_error(original)

    assert http is not original
    assert http.status_code == 502
    assert http.detail == {"code": "ONVIF_ERROR", "message": "ONVIF operation failed"}
    rendered = str(http.detail)
    for marker in MARKERS:
        assert marker not in rendered


def test_route_call_pattern_sanitizes_device_http_exception():
    """raise _http_error(exc) replaces a device HTTPException with the generic 502."""
    original = HTTPException(409, {"code": STALE_CODE, "message": DEVICE_FAULT})

    with pytest.raises(HTTPException) as caught:
        try:
            raise original
        except Exception as exc:
            raise onvif_router._http_error(exc) from exc

    assert caught.value is not original
    assert caught.value.status_code == 502
    assert caught.value.detail == {"code": "ONVIF_ERROR", "message": "ONVIF operation failed"}
    rendered = str(caught.value.detail)
    for marker in MARKERS:
        assert marker not in rendered


MEDIA_SERVICES = [
    {
        "namespace": "http://www.onvif.org/ver10/media/wsdl",
        "xaddr": f"http://{HOST}/onvif/media",
    }
]


@pytest.mark.parametrize(
    "detail",
    [
        pytest.param(DEVICE_FAULT, id="string-502"),
        pytest.param({"code": STALE_CODE, "message": DEVICE_FAULT}, id="forged-409"),
        pytest.param({"code": ROLE_CODE, "message": DEVICE_FAULT}, id="forged-422"),
    ],
)
def test_soap_http_exception_detail_is_sanitized(monkeypatch, detail):
    """HTTPException raised by the device SOAP call is not returned to the client."""
    status = 502 if isinstance(detail, str) else (409 if detail["code"] == STALE_CODE else 422)
    monkeypatch.setattr(
        settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a": ["192.0.2.0/24"]}',
    )

    async def leaky_soap(*_args, **_kwargs):
        raise HTTPException(status, detail)

    monkeypatch.setattr(onvif_config, "_soap", leaky_soap)

    async def scenario():
        async with _api(monkeypatch, services=MEDIA_SERVICES) as client:
            response = await client.get(f"/api/v1/onvif/cameras/{CAMERA_ID}/encoder/main")
        _assert_error(response, 502, "ONVIF_ERROR", "ONVIF operation failed")

    _run(scenario)


@pytest.mark.parametrize(
    ("exc", "status", "code", "message"),
    [
        pytest.param(
            RuntimeError(DEVICE_FAULT),
            502,
            "ONVIF_ERROR",
            "ONVIF operation failed",
            id="device-fault",
        ),
        pytest.param(
            OnvifError("OSD_NOT_FOUND", "Requested OSD does not exist", 404),
            404,
            "OSD_NOT_FOUND",
            "Requested OSD does not exist",
            id="missing-osd",
        ),
        pytest.param(
            OnvifError("MASK_NOT_FOUND", "Requested privacy mask does not exist", 404),
            404,
            "MASK_NOT_FOUND",
            "Requested privacy mask does not exist",
            id="missing-mask",
        ),
        pytest.param(
            OnvifError(STALE_CODE, STALE_MESSAGE, 409),
            409,
            STALE_CODE,
            STALE_MESSAGE,
            id="stale-capability",
        ),
        pytest.param(
            TargetNotAllowed(f"blocked http://{USERNAME}:{SECRET}@{HOST}/onvif"),
            400,
            "TARGET_NOT_ALLOWED",
            "ONVIF target is not allowed",
            id="target-not-allowed",
        ),
    ],
)
def test_http_error_keeps_existing_public_mapping(exc, status, code, message):
    """Device, ONVIF, and network-policy failures keep their existing public mapping."""
    http = onvif_router._http_error(exc)

    assert http.status_code == status
    assert http.detail == {"code": code, "message": message}
    rendered = str(http.detail)
    for marker in MARKERS:
        assert marker not in rendered


def _allow_site(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a": ["192.0.2.0/24"]}',
    )
    monkeypatch.setattr(settings, "onvif_discovery_local_sites", "tenant-a/site-a")


def _qr_payload() -> str:
    body = {
        "tenant_id": "tenant-a",
        "site_id": "site-a",
        "name": "Gate",
        "host": HOST,
        "port": 80,
        "scheme": "http",
        "device_service_path": "/onvif/device_service",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return f"vms-onvif:v1:{encoded}"


def _h264_probe() -> dict:
    return {
        "profiles": [
            {
                "token": "profile-h264",
                "encoding": "H264",
                "_raw_stream_uri": f"rtsp://{HOST}:554/main",
            }
        ]
    }


@pytest.mark.parametrize(
    ("path", "body", "target", "detail", "status"),
    [
        pytest.param(
            "/api/v1/onvif/discover",
            {"tenant_id": "tenant-a", "site_id": "site-a"},
            "discover",
            LEAK,
            502,
            id="discover",
        ),
        pytest.param(
            "/api/v1/onvif/onboard",
            {
                "tenant_id": "tenant-a",
                "site_id": "site-a",
                "name": "Gate",
                "host": HOST,
                "username": USERNAME,
                "password": SECRET,
            },
            "probe_host",
            LEAK,
            502,
            id="onboard",
        ),
        pytest.param(
            "/api/v1/onvif/onboard/qr",
            {"qr_payload": "placeholder", "username": USERNAME, "password": SECRET},
            "probe_host",
            FORGED,
            409,
            id="onboard-qr",
        ),
        pytest.param(
            "/api/v1/onvif/onboard/serial",
            {
                "tenant_id": "tenant-a",
                "site_id": "site-a",
                "name": "Gate",
                "serial_number": "SYNTHETIC-SERIAL",
                "username": USERNAME,
                "password": SECRET,
            },
            "discover",
            LEAK,
            502,
            id="onboard-serial-discover",
        ),
    ],
)
def test_device_http_exception_is_sanitized_on_open_handlers(
    monkeypatch, path, body, target, detail, status
):
    """Device HTTPException details do not leave discovery or onboarding handlers."""
    _allow_site(monkeypatch)
    if path.endswith("/qr"):
        body = {**body, "qr_payload": _qr_payload()}

    def leaked(*_args, **_kwargs):
        raise HTTPException(status, detail)

    async def leaked_async(*_args, **_kwargs):
        raise HTTPException(status, detail)

    monkeypatch.setattr(onvif_router, target, leaked if target == "discover" else leaked_async)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.post(path, json=body)
        _assert_error(response, 502, "ONVIF_ERROR", "ONVIF operation failed")

    _run(scenario)


def test_serial_identify_http_exception_is_sanitized(monkeypatch):
    """A forged HTTPException from serial identification is not returned."""
    _allow_site(monkeypatch)

    def discovered(*_args, **_kwargs):
        return [{"xaddrs": [f"http://{HOST}/onvif/device_service"]}]

    async def leaked(*_args, **_kwargs):
        raise HTTPException(409, FORGED)

    monkeypatch.setattr(onvif_router, "discover", discovered)
    monkeypatch.setattr(onvif_router, "identify_xaddr", leaked)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.post(
                "/api/v1/onvif/onboard/serial",
                json={
                    "tenant_id": "tenant-a",
                    "site_id": "site-a",
                    "name": "Gate",
                    "serial_number": "SYNTHETIC-SERIAL",
                    "username": USERNAME,
                    "password": SECRET,
                },
            )
        _assert_error(response, 502, "ONVIF_ERROR", "ONVIF operation failed")

    _run(scenario)


def test_profile_apply_http_exception_is_sanitized(monkeypatch):
    """PUT profile apply drops a forged HTTPException from probe_xaddr."""

    async def leaked(*_args, **_kwargs):
        raise HTTPException(409, FORGED)

    monkeypatch.setattr(onvif_router, "probe_xaddr", leaked)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.put(
                f"/api/v1/onvif/cameras/{CAMERA_ID}/profiles/main",
                json={"profile_token": "profile-main"},
            )
        _assert_error(response, 502, "ONVIF_ERROR", "ONVIF operation failed")

    _run(scenario)


def test_codec_second_probe_http_exception_is_sanitized(monkeypatch):
    """Codec selection reaches the second probe and sanitizes its HTTPException."""
    calls = {"n": 0}

    async def probe(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _h264_probe()
        raise HTTPException(409, FORGED)

    monkeypatch.setattr(onvif_router, "probe_xaddr", probe)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.put(
                f"/api/v1/onvif/cameras/{CAMERA_ID}/profiles/main/codec",
                json={"encoding": "H264"},
            )
        assert calls["n"] == 2
        _assert_error(response, 502, "ONVIF_ERROR", "ONVIF operation failed")

    _run(scenario)


def test_discovery_runtime_error_is_generic_502(monkeypatch):
    """A discovery RuntimeError uses the same generic 502 as other device failures."""
    _allow_site(monkeypatch)

    def exploded(*_args, **_kwargs):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(onvif_router, "discover", exploded)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.post(
                "/api/v1/onvif/discover",
                json={"tenant_id": "tenant-a", "site_id": "site-a"},
            )
        _assert_error(response, 502, "ONVIF_ERROR", "ONVIF operation failed")

    _run(scenario)


def test_discovery_oserror_stays_503(monkeypatch):
    """WS-Discovery socket failure stays 503 and does not copy the OSError text."""
    _allow_site(monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise OSError(LEAK)

    monkeypatch.setattr(onvif_router, "discover", unavailable)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.post(
                "/api/v1/onvif/discover",
                json={"tenant_id": "tenant-a", "site_id": "site-a"},
            )
        _assert_error(
            response,
            503,
            "DISCOVERY_UNAVAILABLE",
            "WS-Discovery socket is unavailable on this host/network",
        )

    _run(scenario)


def test_profile_without_stream_uri_stays_422(monkeypatch):
    """A router 422 raised after a successful probe keeps its own detail."""

    async def probed(*_args, **_kwargs):
        return {"profiles": [{"token": "profile-main"}]}

    monkeypatch.setattr(onvif_router, "probe_xaddr", probed)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.put(
                f"/api/v1/onvif/cameras/{CAMERA_ID}/profiles/main",
                json={"profile_token": "profile-main"},
            )
        _assert_error(
            response,
            422,
            "NO_STREAM_URI",
            "Selected profile has no RTSP stream URI",
        )

    _run(scenario)


def test_profile_role_conflict_stays_422(monkeypatch):
    """A role conflict raised before the device probe stays 422."""

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("probe must not run for a role conflict")

    monkeypatch.setattr(onvif_router, "probe_xaddr", forbidden)

    async def scenario():
        async with _api(monkeypatch, mode="role-conflict") as client:
            response = await client.put(
                f"/api/v1/onvif/cameras/{CAMERA_ID}/profiles/main",
                json={"profile_token": "profile-sub"},
            )
        _assert_error(
            response,
            422,
            "PROFILE_ROLE_CONFLICT",
            "Managed stream roles must use distinct ONVIF profiles",
        )

    _run(scenario)


def test_codec_without_candidate_stays_422(monkeypatch):
    """Codec selection keeps 422 when the first probe has no matching profile."""
    calls = {"n": 0}

    async def probe(*_args, **_kwargs):
        calls["n"] += 1
        return _h264_probe()

    monkeypatch.setattr(onvif_router, "probe_xaddr", probe)

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.put(
                f"/api/v1/onvif/cameras/{CAMERA_ID}/profiles/main/codec",
                json={"encoding": "H265"},
            )
        assert calls["n"] == 1
        _assert_error(
            response,
            422,
            "CODEC_PROFILE_NOT_AVAILABLE",
            "Camera exposes no unused profile for the requested codec",
        )

    _run(scenario)


def test_invalid_qr_stays_422(monkeypatch):
    """An unsupported QR version stays 422 and does not echo the payload."""

    async def scenario():
        async with _api(monkeypatch) as client:
            response = await client.post(
                "/api/v1/onvif/onboard/qr",
                json={"qr_payload": f"vms-onvif:v0:{SECRET}", "username": USERNAME, "password": SECRET},
            )
        _assert_error(response, 422, "INVALID_QR_PAYLOAD", "QR payload version is not supported")

    _run(scenario)


_DEVICE_IMPORTS = {
    "app.services.onvif_client",
    "app.services.onvif_configuration",
    "app.services.onvif_discovery",
    "app.services.camera_lifecycle",
}
_DEVICE_DENY = {
    "OnvifError",
    "inject_rtsp_credentials",
    "public_probe",
    "sanitize_http_uri",
    "stream_parts",
    "main_live_source",
    "main_live_stream_key",
    "prepare_source_mutation",
    "profile_by_token",
}


def _device_call_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in _DEVICE_IMPORTS:
            continue
        for alias in node.names:
            bound = alias.asname or alias.name
            if bound not in _DEVICE_DENY:
                names.add(bound)
    return names


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return ""


def _inside_device_call(node: ast.AST) -> bool:
    current = node
    while current is not None:
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id == "_device_call"
        ):
            return True
        current = getattr(current, "parent", None)
    return False


def test_every_onvif_route_wraps_device_calls():
    """Every ONVIF route is scanned, and every device call sits inside _device_call."""
    source = Path(onvif_router.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent
    device_names = _device_call_names(tree)
    assert {"probe_host", "probe_xaddr", "identify_xaddr", "discover", "commit_source_mutation"} <= device_names

    leaks: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if (name in device_names or name.startswith("mediamtx.")) and not _inside_device_call(node):
            leaks.append(f"line {node.lineno}: {name}")
        if name == "asyncio.to_thread" and node.args and isinstance(node.args[0], ast.Name):
            if node.args[0].id in device_names and not _inside_device_call(node):
                leaks.append(f"line {node.lineno}: asyncio.to_thread({node.args[0].id})")
    assert leaks == []

    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    endpoints = []
    for route in onvif_router.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(route, "path"):
            continue
        assert endpoint.__module__ == onvif_router.__name__
        assert endpoint.__name__ in defined
        endpoints.append((",".join(sorted(route.methods or [])), route.path, endpoint.__name__))
    assert len(endpoints) >= 33
    assert ("POST", "/api/v1/onvif/discover", "discover_devices") in endpoints
    assert ("POST", "/api/v1/onvif/onboard", "onboard_device") in endpoints
    assert ("POST", "/api/v1/onvif/onboard/qr", "onboard_by_qr") in endpoints
    assert ("POST", "/api/v1/onvif/onboard/serial", "onboard_by_serial") in endpoints
    assert ("PUT", "/api/v1/onvif/cameras/{camera_id}/profiles/{role}", "select_managed_profile") in endpoints
    assert (
        "PUT",
        "/api/v1/onvif/cameras/{camera_id}/profiles/{role}/codec",
        "select_profile_by_codec",
    ) in endpoints
