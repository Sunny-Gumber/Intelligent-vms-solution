"""F21: expected ONVIF HTTP errors must not be rewritten as HTTP 502.

Configuration routes catch Exception and call _http_error. _managed_profile
raises HTTPException for a bad role, so an expected 422 became 502. These tests
use an in-memory database and fakes only. Device transport is blocked.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
MARKERS = (SECRET, USERNAME, HOST, "raw-device-body", "/onvif/device_service")
ROLE_CODE = "INVALID_STREAM_ROLE"
ROLE_MESSAGE = "Role must be main, sub or third"
STALE_CODE = "CAPABILITY_REFRESH_REQUIRED"
STALE_MESSAGE = "Selected profile is not present in the stored ONVIF snapshot"


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
    if mode == "ready":
        profile["video_source_configuration_token"] = "vsc-1"
    elif mode == "stale":
        token = "stale-token"
    elif mode == "no-source-token":
        pass
    elif mode == "unselected":
        token = None
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
