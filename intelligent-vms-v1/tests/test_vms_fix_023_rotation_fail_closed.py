"""VMS-FIX-023: rotation degree writes fail closed without advertised support.

Fakes stand in for the camera. These tests never open a device connection.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from defusedxml import ElementTree as DET

from app.services import onvif_configuration as config
from app.services.onvif_client import MEDIA_NS, OnvifError


_MISSING = object()


def _configuration_root():
    return DET.fromstring(
        """
        <Envelope><Configuration token="src-conf-main">
          <Extension>
            <Rotate><Mode>OFF</Mode><Degree>0</Degree></Rotate>
            <Mirror>false</Mirror>
          </Extension>
        </Configuration></Envelope>
        """
    )


def _state(*, degrees=_MISSING, modes=("OFF", "ON", "AUTO")):
    """Build a get_orientation fake with optional advertised degrees."""
    options = {
        "rotation_modes": list(modes),
        "mirror_supported": True,
        "flip_supported": False,
    }
    if degrees is not _MISSING:
        options["rotation_degrees"] = degrees
    return {
        "current": {
            "rotation_mode": "OFF",
            "rotation_degree": 0,
            "mirror": False,
            "flip": False,
        },
        "options": options,
        "_configuration_root": _configuration_root(),
        "_media_xaddr": "http://192.168.1.20/onvif/media",
    }


def _install(monkeypatch, state):
    """Stub orientation reads and capture every SOAP call set_orientation makes."""
    soap = AsyncMock(return_value=DET.fromstring("<Envelope/>"))
    monkeypatch.setattr(config, "_soap", soap)
    readback = {
        "current": dict(state["current"]),
        "options": dict(state["options"]),
    }
    monkeypatch.setattr(
        config,
        "get_orientation",
        AsyncMock(side_effect=[state, readback]),
    )
    return soap


def _set(changes):
    return config.set_orientation(
        [],
        {"token": "profile-main"},
        changes,
        None,
        None,
        "tenant-a",
        "site-a",
    )


@pytest.mark.parametrize(
    "degrees",
    [[], None, _MISSING],
    ids=["empty", "none", "missing"],
)
@pytest.mark.parametrize("degree", [0, 90])
def test_unadvertised_degree_list_sends_no_soap(monkeypatch, degrees, degree):
    """Reject a supplied degree when the camera advertises no degree list."""
    soap = _install(monkeypatch, _state(degrees=degrees))

    with pytest.raises(OnvifError) as error:
        asyncio.run(_set({"rotation_degree": degree}))

    assert error.value.code == "VALUE_NOT_SUPPORTED"
    assert error.value.status_code == 422
    assert soap.await_count == 0


def test_degree_outside_advertised_list_sends_no_soap(monkeypatch):
    """Reject a degree the camera did not include in a non-empty list."""
    soap = _install(monkeypatch, _state(degrees=[0, 90, 180]))

    with pytest.raises(OnvifError) as error:
        asyncio.run(_set({"rotation_degree": 270}))

    assert error.value.code == "VALUE_NOT_SUPPORTED"
    assert error.value.status_code == 422
    assert soap.await_count == 0


@pytest.mark.parametrize("degree", [0, 90, 180])
def test_advertised_degree_is_written(monkeypatch, degree):
    """Write a degree only when that degree is in the advertised list."""
    soap = _install(monkeypatch, _state(degrees=[0, 90, 180]))

    asyncio.run(_set({"rotation_degree": degree}))

    assert soap.await_count == 1
    action, body = soap.await_args.args[1], soap.await_args.args[2]
    assert action == f"{MEDIA_NS}/SetVideoSourceConfiguration"
    assert f"<Degree>{degree}</Degree>" in body


@pytest.mark.parametrize("mode", ["OFF", "ON", "AUTO"])
def test_empty_rotation_modes_send_no_soap(monkeypatch, mode):
    """Reject ON/OFF/AUTO when the camera advertises no rotation modes."""
    soap = _install(monkeypatch, _state(degrees=[0, 90, 180], modes=[]))

    with pytest.raises(OnvifError) as error:
        asyncio.run(_set({"rotation_mode": mode}))

    assert error.value.code == "VALUE_NOT_SUPPORTED"
    assert error.value.status_code == 422
    assert soap.await_count == 0


def test_empty_degrees_still_allow_advertised_rotation_mode(monkeypatch):
    """A mode-only change stays valid when the degree list is empty."""
    soap = _install(monkeypatch, _state(degrees=[], modes=["OFF", "ON", "AUTO"]))

    asyncio.run(_set({"rotation_mode": "AUTO"}))

    assert soap.await_count == 1
    action, body = soap.await_args.args[1], soap.await_args.args[2]
    assert action == f"{MEDIA_NS}/SetVideoSourceConfiguration"
    assert "<Mode>AUTO</Mode>" in body
    assert "<Degree>0</Degree>" in body
