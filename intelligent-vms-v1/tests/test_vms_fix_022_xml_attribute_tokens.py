"""VMS-FIX-022: OSD and privacy-mask tokens must survive XML attribute quoting.

The generated SOAP is parsed with the standard-library XML parser. Fakes stand
in for the camera; these tests never open a device connection.
"""

import asyncio
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest
from defusedxml import ElementTree as DET

from app.services import onvif_configuration as config
from app.services.onvif_client import OnvifError, _envelope


MEDIA_SERVICES = [
    {
        "namespace": "http://www.onvif.org/ver10/media/wsdl",
        "xaddr": "http://192.168.1.20/onvif/media",
    },
    {
        "namespace": "http://www.onvif.org/ver20/media/wsdl",
        "xaddr": "http://192.168.1.20/onvif/media2",
    },
]

# One value carries every accepted special character. The second is the audit
# attribute-injection fixture. The rest are legal XML 1.0 whitespace that a
# parser must return unchanged.
ROUND_TRIP_TOKENS = [
    "cam\"era'&<osd>",
    'a" injected="1',
    "a\tb",
    "line\nbreak",
    "cr\rback",
]
OSD_TEXT = 'Gate & "name" <cam>'
MASK_POINTS = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _one(root, local_name: str):
    matches = [element for element in root.iter() if _local(element.tag) == local_name]
    assert len(matches) == 1
    return matches[0]


def _parsed_soap(body: str):
    return ET.fromstring(_envelope(body, None, None))


def _pin(monkeypatch):
    monkeypatch.setattr(
        config,
        "pin_site_http_xaddr",
        lambda value, tenant_id, site_id: value,
    )


def _osd_row(token: str, *, position_type: str = "UpperLeft") -> dict:
    return {
        "token": token,
        "type": "Text",
        "position_type": position_type,
        "x": 0.0,
        "y": 0.0,
        "video_source_configuration_token": "src-conf-main",
        "text_type": "Plain",
        "text": "old",
    }


def _mask_row(token: str) -> dict:
    return {
        "token": token,
        "type": "Color",
        "enabled": True,
        "points": [{"x": point[0], "y": point[1]} for point in MASK_POINTS],
    }


def _install_osd(monkeypatch, token: str, *, position_type: str = "UpperLeft"):
    _pin(monkeypatch)
    calls = []

    async def soap(_xaddr, action, body, *_args, **_kwargs):
        calls.append((action, body))
        return DET.fromstring("<Envelope/>")

    monkeypatch.setattr(config, "_soap", soap)
    monkeypatch.setattr(
        config,
        "get_osd_options",
        AsyncMock(return_value={"types": ["Text"], "maximum_total": 8}),
    )
    monkeypatch.setattr(
        config,
        "list_osds",
        AsyncMock(return_value=[_osd_row(token, position_type=position_type)]),
    )
    return calls


def _install_mask(monkeypatch, token: str):
    _pin(monkeypatch)
    calls = []
    options = DET.fromstring(
        """
        <Envelope><Options RectangleOnly="false">
          <MaxMasks>8</MaxMasks><MaxPoints>8</MaxPoints><Types>Color</Types>
        </Options></Envelope>
        """
    )

    async def soap(_xaddr, action, body, *_args, **_kwargs):
        calls.append((action, body))
        if action.endswith("/GetMaskOptions"):
            return options
        return DET.fromstring("<Envelope/>")

    monkeypatch.setattr(config, "_soap", soap)
    monkeypatch.setattr(config, "list_masks", AsyncMock(return_value=[_mask_row(token)]))
    return calls


def _osd_payload(token: str, **extra) -> dict:
    payload = {
        "text": OSD_TEXT,
        "position_type": "UpperLeft",
        "x": 0.0,
        "y": 0.0,
        "osd_type": "Plain",
        "token": token,
    }
    payload.update(extra)
    return payload


def _mask_payload(token: str, **extra) -> dict:
    payload = {
        "points": MASK_POINTS,
        "enabled": True,
        "mask_type": "Color",
        "token": token,
    }
    payload.update(extra)
    return payload


def _run_osd(monkeypatch, operation: str, token: str, **extra) -> str:
    calls = _install_osd(
        monkeypatch,
        token,
        position_type=extra.get("position_type", "UpperLeft"),
    )

    async def run():
        if operation == "create":
            await config.create_osd(
                MEDIA_SERVICES,
                "src-conf-main",
                _osd_payload(token, **extra),
                "admin",
                "synthetic-secret-marker",
                "tenant-a",
                "site-a",
            )
        elif operation == "update":
            await config.update_osd(
                MEDIA_SERVICES,
                token,
                {"text": OSD_TEXT, **extra},
                "admin",
                "synthetic-secret-marker",
                "tenant-a",
                "site-a",
            )
        else:
            await config.delete_osd(
                MEDIA_SERVICES,
                token,
                "admin",
                "synthetic-secret-marker",
                "tenant-a",
                "site-a",
            )

    asyncio.run(run())
    suffix = {"create": "/CreateOSD", "update": "/SetOSD", "delete": "/DeleteOSD"}[operation]
    body = next(body for action, body in calls if action.endswith(suffix))
    assert "synthetic-secret-marker" not in body
    return body


def _run_mask(monkeypatch, operation: str, token: str, **extra) -> str:
    calls = _install_mask(monkeypatch, token)

    async def run():
        if operation == "create":
            await config.create_mask(
                MEDIA_SERVICES,
                "src-conf-main",
                _mask_payload(token, **extra),
                "admin",
                "synthetic-secret-marker",
                "tenant-a",
                "site-a",
            )
        elif operation == "update":
            await config.update_mask(
                MEDIA_SERVICES,
                token,
                "src-conf-main",
                {"enabled": False, **extra},
                "admin",
                "synthetic-secret-marker",
                "tenant-a",
                "site-a",
            )
        else:
            await config.delete_mask(
                MEDIA_SERVICES,
                token,
                "src-conf-main",
                "admin",
                "synthetic-secret-marker",
                "tenant-a",
                "site-a",
            )

    asyncio.run(run())
    suffix = {"create": "/CreateMask", "update": "/SetMask", "delete": "/DeleteMask"}[operation]
    body = next(body for action, body in calls if action.endswith(suffix))
    assert "synthetic-secret-marker" not in body
    return body


def _assert_no_injected_attribute(root) -> None:
    assert all("injected" not in element.attrib for element in root.iter())


@pytest.mark.parametrize("kind", ["osd", "mask"])
@pytest.mark.parametrize("operation", ["create", "update", "delete"])
@pytest.mark.parametrize("token", ROUND_TRIP_TOKENS)
def test_osd_and_mask_tokens_round_trip(monkeypatch, kind, operation, token):
    """Parse create, update, and delete SOAP and recover the original token."""
    if kind == "osd":
        body = _run_osd(monkeypatch, operation, token)
    else:
        body = _run_mask(monkeypatch, operation, token)
    root = _parsed_soap(body)
    _assert_no_injected_attribute(root)

    if operation == "delete":
        local_name = "OSDToken" if kind == "osd" else "Token"
        element = _one(root, local_name)
        assert element.text == token
        assert element.attrib == {}
        return

    local_name = "OSD" if kind == "osd" else "Mask"
    element = _one(root, local_name)
    assert element.attrib["token"] == token
    assert set(element.attrib) == {"token"}
    if kind == "osd":
        assert _one(root, "PlainText").text == OSD_TEXT


@pytest.mark.parametrize("operation", ["create", "update"])
def test_osd_position_attributes_do_not_split(monkeypatch, operation):
    """A quoted coordinate must stay one attribute on OSD create and update."""
    x = '0" injected="1'
    y = "0.5"
    body = _run_osd(
        monkeypatch,
        operation,
        "osd-1",
        position_type="Custom",
        x=x,
        y=y,
    )
    root = _parsed_soap(body)
    pos = _one(root, "Pos")
    assert pos.attrib["x"] == x
    assert pos.attrib["y"] == y
    assert set(pos.attrib) == {"x", "y"}
    _assert_no_injected_attribute(root)


@pytest.mark.parametrize("operation", ["create", "update"])
def test_mask_point_attributes_do_not_split(monkeypatch, operation):
    """A quoted polygon coordinate must stay one attribute on mask writes."""
    x = '0" injected="1'
    points = [(x, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]
    if operation == "create":
        body = _run_mask(monkeypatch, operation, "mask-1", points=points)
    else:
        body = _run_mask(monkeypatch, operation, "mask-1", points=points)
    root = _parsed_soap(body)
    point = next(element for element in root.iter() if _local(element.tag) == "Point")
    assert point.attrib["x"] == x
    assert point.attrib["y"] == "-0.5"
    assert set(point.attrib) == {"x", "y"}
    _assert_no_injected_attribute(root)


@pytest.mark.parametrize(
    ("kind", "operation"),
    [
        ("osd", "create"),
        ("osd", "update"),
        ("osd", "delete"),
        ("mask", "create"),
        ("mask", "update"),
        ("mask", "delete"),
    ],
)
@pytest.mark.parametrize("token", ["bad\x00token", "bad\x08token", "bad\x0btoken", "bad\x1ftoken", "bad\ud800token", "bad\ufffetoken"])
def test_xml10_forbidden_characters_are_rejected(monkeypatch, kind, operation, token):
    """Forbidden XML 1.0 characters are rejected instead of being stripped."""
    with pytest.raises(OnvifError) as error:
        if kind == "osd":
            _run_osd(monkeypatch, operation, token)
        else:
            _run_mask(monkeypatch, operation, token)
    assert error.value.code == "INVALID_XML_VALUE"
    assert "\x00" not in error.value.message
    assert token not in error.value.message
