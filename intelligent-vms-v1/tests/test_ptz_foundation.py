import asyncio
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.auth import Principal
from app.models.schemas import PtzMoveRequest
from app.routers import ptz as ptz_router
from app.services import ptz


def principal():
    return Principal(
        subject="operator-1",
        roles=frozenset({"operator"}),
        tenant_id="tenant-a",
        site_ids=frozenset({"site-a"}),
    )


def test_ptz_vector_validation_rejects_zero_nonfinite_and_out_of_range():
    context = uuid4()
    with pytest.raises(ValidationError):
        PtzMoveRequest(pan=0, tilt=0, zoom=0, generation=1, context_id=context)
    for value in [float("nan"), float("inf"), float("-inf"), 1.01, -1.01]:
        with pytest.raises(ValidationError):
            PtzMoveRequest(pan=value, tilt=0, zoom=0, generation=1, context_id=context)


def test_generation_fence_stop_priority_and_context_restart():
    async def run():
        ptz_router._generations.clear()
        ptz_router._last_move_at.clear()
        p = principal()
        first = str(uuid4())
        second = str(uuid4())
        await ptz_router._claim_generation(p, "camera-1", 10, first, movement=False)
        await ptz_router._claim_generation(p, "camera-1", 11, first, movement=False)
        with pytest.raises(HTTPException) as stale:
            await ptz_router._claim_generation(p, "camera-1", 10, first, movement=False)
        assert stale.value.status_code == 409
        # A fresh desktop PTZ context starts safely at generation 1.
        await ptz_router._claim_generation(p, "camera-1", 1, second, movement=False)

    asyncio.run(run())


def test_stop_is_not_rate_limited_and_move_rate_is_bounded():
    async def run():
        ptz_router._generations.clear()
        ptz_router._last_move_at.clear()
        p = principal()
        context = str(uuid4())
        key = (p.subject, "camera-1", context)
        ptz_router._generations[key] = 1
        ptz_router._last_move_at[key] = ptz_router.time.monotonic() + 100.0
        with pytest.raises(HTTPException) as limited:
            await ptz_router._claim_generation(p, "camera-1", 2, context, movement=True)
        assert limited.value.status_code == 429
        # STOP has priority and remains accepted immediately.
        await ptz_router._claim_generation(p, "camera-1", 3, context, movement=False)

    asyncio.run(run())


def test_capability_discovery_is_live_and_axis_specific(monkeypatch):
    async def fake_soap(*args, **kwargs):
        return ET.fromstring(
            '<GetNodesResponse><PTZNode>'
            '<SupportedPTZSpaces><ContinuousPanTiltVelocitySpace/>'
            '</SupportedPTZSpaces></PTZNode></GetNodesResponse>'
        )

    monkeypatch.setattr(ptz, "_service", lambda *_: "http://camera.invalid/ptz")
    monkeypatch.setattr(ptz, "_soap", fake_soap)
    result = asyncio.run(
        ptz.capabilities([], "profile-main", None, None, "tenant-a", "site-a")
    )
    assert result["ptz"] is True
    assert result["pan_tilt"] is True
    assert result["zoom"] is False
    assert result["presets"] is False


def test_continuous_move_and_stop_use_normalized_server_side_contract(monkeypatch):
    calls = []

    async def fake_soap(xaddr, action, body, *args, **kwargs):
        calls.append((xaddr, action, body))
        return ET.fromstring("<Response/>")

    async def fake_caps(*args, **kwargs):
        return {"ptz": True, "pan_tilt": True, "zoom": True, "presets": False}

    monkeypatch.setattr(ptz, "_service", lambda *_: "http://camera.invalid/ptz")
    monkeypatch.setattr(ptz, "_soap", fake_soap)
    monkeypatch.setattr(ptz, "capabilities", fake_caps)

    async def run():
        await ptz.continuous_move(
            [], "profile-main", -0.65, 0.0, 0.0, "user", "secret", "tenant-a", "site-a"
        )
        await ptz.stop([], "profile-main", "user", "secret", "tenant-a", "site-a")

    asyncio.run(run())
    assert calls[0][1].endswith("/ContinuousMove")
    assert 'x="-0.65"' in calls[0][2]
    assert "secret" not in calls[0][2]
    assert calls[1][1].endswith("/Stop")
    assert "<PanTilt>true</PanTilt>" in calls[1][2]
    assert "<Zoom>true</Zoom>" in calls[1][2]


def test_router_preserves_server_authority_and_safe_error_boundary():
    source = (
        Path(__file__).parents[1]
        / "services"
        / "control-api"
        / "app"
        / "routers"
        / "ptz.py"
    ).read_text(encoding="utf-8")
    assert 'require_roles("admin", "operator")' in source
    assert "authorized_camera(session, camera_id, principal)" in source
    assert "decrypt_secret(camera.username_enc)" in source
    assert "decrypt_secret(camera.password_enc)" in source
    assert "Camera returned" not in source
    assert "Authorization" not in source
