"""VMS-FIX-036: OSD and privacy-mask writes fail closed without advertised support.

Fakes stand in for the camera. These tests never open a device connection.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from defusedxml import ElementTree as DET

from app.services import onvif_configuration as config
from app.services.onvif_client import MEDIA2_NS, MEDIA_NS, OnvifError


_MISSING = object()
_MUTATIONS = ("/CreateOSD", "/SetOSD", "/CreateMask", "/SetMask")

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

_MASK_POINTS = [(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]


def _pin(monkeypatch):
    """Keep the fake camera address instead of applying site network policy."""
    monkeypatch.setattr(
        config,
        "pin_site_http_xaddr",
        lambda value, tenant_id, site_id: value,
    )


def _mutations(calls):
    """Return SOAP actions that create or modify an OSD or privacy mask.

    Args:
        calls: ``(action, body)`` pairs recorded from the fake client.

    Returns:
        Actions whose suffix is CreateOSD, SetOSD, CreateMask or SetMask.
    """
    return [
        action
        for action, _body in calls
        if any(str(action).endswith(suffix) for suffix in _MUTATIONS)
    ]


def _recorded(soap):
    """Return ``(action, body)`` pairs from an AsyncMock SOAP stub.

    Args:
        soap: AsyncMock installed as ``_soap``.

    Returns:
        Recorded action and body arguments, in call order.
    """
    return [(call.args[1], call.args[2]) for call in soap.await_args_list]


def _assert_rejected(error, calls, fragment):
    """Assert a FIX-023-shaped rejection sent no OSD or mask mutation.

    Args:
        error: Captured ``pytest.raises`` info.
        calls: Recorded ``(action, body)`` SOAP pairs.
        fragment: Text that must appear in the safe error message.

    Returns:
        None.

    Raises:
        AssertionError: If the code, status, message or call log is wrong.
    """
    assert error.value.code == "VALUE_NOT_SUPPORTED"
    assert error.value.status_code == 422
    assert fragment in error.value.message
    assert _mutations(calls) == []


def _osd_row(*, position_type="UpperLeft", text_type="Plain"):
    """Build one stored OSD row.

    Args:
        position_type: Position an update would write back.
        text_type: TextString type an update would write back.

    Returns:
        Normalized OSD dictionary accepted by ``update_osd``.
    """
    return {
        "token": "osd-1",
        "type": "Text",
        "position_type": position_type,
        "x": 0.0,
        "y": 0.0,
        "video_source_configuration_token": "src-conf-main",
        "text_type": text_type,
        "text": "Gate",
    }


def _mask_row(mask_type="Color"):
    """Build one stored privacy-mask row.

    Args:
        mask_type: Mask type an update would write back.

    Returns:
        Normalized mask dictionary accepted by ``update_mask``.
    """
    return {
        "token": "mask-1",
        "type": mask_type,
        "enabled": True,
        "points": [{"x": point[0], "y": point[1]} for point in _MASK_POINTS],
    }


def _elements(tag, values):
    """Render one XML element per value, or one empty element for an empty list.

    Args:
        tag: Element local name.
        values: Advertised values. An empty sequence emits an empty element.

    Returns:
        XML fragment.
    """
    if not values:
        return f"<{tag}></{tag}>"
    return "".join(f"<{tag}>{value}</{tag}>" for value in values)


def _osd_options_xml(*, types=_MISSING, positions=_MISSING):
    """Build a GetOSDOptions document.

    Args:
        types: OSD ``Type`` values. ``_MISSING`` omits the elements.
        positions: ``PositionOption`` values. ``_MISSING`` omits the elements.

    Returns:
        XML string whose parsed types and positions match the arguments.
    """
    parts = ['<OSDOptions><MaximumNumberOfOSDs Total="8"/>']
    if types is not _MISSING:
        parts.append(_elements("Type", types))
    if positions is not _MISSING:
        parts.append(_elements("PositionOption", positions))
    parts.append("</OSDOptions>")
    return "<Envelope>" + "".join(parts) + "</Envelope>"


def _osd_existing_xml(*, position_type="UpperLeft", text_type="Plain"):
    """Build a GetOSDs document containing one OSD.

    Args:
        position_type: Stored position.
        text_type: Stored text-string type.

    Returns:
        XML string ``parse_osds`` turns into one row.
    """
    return (
        "<Envelope><OSD token=\"osd-1\">"
        "<VideoSourceConfigurationToken>src-conf-main</VideoSourceConfigurationToken>"
        "<Type>Text</Type>"
        f"<Position><Type>{position_type}</Type></Position>"
        f"<TextString><Type>{text_type}</Type><PlainText>Gate</PlainText></TextString>"
        "</OSD></Envelope>"
    )


def _mask_options_xml(types=_MISSING):
    """Build a GetMaskOptions document.

    Args:
        types: ``Types`` values. ``_MISSING`` omits the elements. An empty
            sequence emits one empty ``Types`` element.

    Returns:
        XML string ``mask_options`` can parse.
    """
    parts = [
        '<Options RectangleOnly="false">',
        "<MaxMasks>8</MaxMasks><MaxPoints>8</MaxPoints>",
    ]
    if types is not _MISSING:
        parts.append(_elements("Types", types))
    parts.append("</Options>")
    return "<Envelope>" + "".join(parts) + "</Envelope>"


def _install_osd_soap(monkeypatch, options_xml, existing_xml="<Envelope/>"):
    """Stub Media SOAP and capture every OSD call.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        options_xml: GetOSDOptions response.
        existing_xml: GetOSDs response.

    Returns:
        List of ``(action, body)`` pairs appended by the stub.
    """
    _pin(monkeypatch)
    calls = []

    async def soap(_xaddr, action, body, *_args, **_kwargs):
        calls.append((action, body))
        if action.endswith("/GetOSDOptions"):
            return DET.fromstring(options_xml)
        if action.endswith("/GetOSDs"):
            return DET.fromstring(existing_xml)
        if action.endswith("/CreateOSD") or action.endswith("/SetOSD"):
            return DET.fromstring("<Envelope/>")
        raise AssertionError(action)

    monkeypatch.setattr(config, "_soap", soap)
    return calls


def _install_osd_options(monkeypatch, options, *, position_type="UpperLeft", text_type="Plain"):
    """Stub OSD reads so ``_soap`` is only reached for a mutation.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        options: Dictionary returned by ``get_osd_options``.
        position_type: Stored position used when the call is an update.
        text_type: Stored text type used when the call is an update.

    Returns:
        AsyncMock standing in for ``_soap``.
    """
    _pin(monkeypatch)
    soap = AsyncMock(return_value=DET.fromstring("<Envelope/>"))
    monkeypatch.setattr(config, "_soap", soap)
    monkeypatch.setattr(config, "get_osd_options", AsyncMock(return_value=options))
    monkeypatch.setattr(
        config,
        "list_osds",
        AsyncMock(return_value=[_osd_row(position_type=position_type, text_type=text_type)]),
    )
    return soap


def _run_osd(operation, *, position_type="UpperLeft", text_type="Plain"):
    """Create or update one OSD against the installed fake.

    Args:
        operation: ``create`` or ``update``.
        position_type: Position the create payload requests. Updates write the
            stored row's position instead.
        text_type: Text-string type the create payload requests. Updates write
            the stored row's text type instead.

    Returns:
        Readback list from the service.

    Raises:
        OnvifError: When the service rejects the write.
    """
    payload = {
        "text": "Gate",
        "position_type": position_type,
        "x": 0.0,
        "y": 0.0,
        "osd_type": text_type,
    }

    async def run():
        if operation == "create":
            return await config.create_osd(
                MEDIA_SERVICES,
                "src-conf-main",
                payload,
                None,
                None,
                "tenant-a",
                "site-a",
            )
        # A Date or Time OSD rejects text edits before the capability check.
        # Move only the position so the update still reaches SetOSD.
        patch = {"text": "Gate"} if text_type == "Plain" else {"x": 0.25}
        return await config.update_osd(
            MEDIA_SERVICES,
            "osd-1",
            patch,
            None,
            None,
            "tenant-a",
            "site-a",
        )

    return asyncio.run(run())


def _install_mask_soap(monkeypatch, options_xml, row):
    """Stub Media2 SOAP and capture every privacy-mask call.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        options_xml: GetMaskOptions response.
        row: Stored mask returned by the list stub, or None when the list is empty.

    Returns:
        List of ``(action, body)`` pairs appended by the stub.
    """
    _pin(monkeypatch)
    calls = []
    monkeypatch.setattr(
        config,
        "list_masks",
        AsyncMock(return_value=[] if row is None else [row]),
    )

    async def soap(_xaddr, action, body, *_args, **_kwargs):
        calls.append((action, body))
        if action.endswith("/GetMaskOptions"):
            return DET.fromstring(options_xml)
        if action.endswith("/CreateMask") or action.endswith("/SetMask"):
            return DET.fromstring("<Envelope/>")
        raise AssertionError(action)

    monkeypatch.setattr(config, "_soap", soap)
    return calls


def _run_mask(operation, mask_type):
    """Create or update one privacy mask against the installed fake.

    Args:
        operation: ``create`` or ``update``.
        mask_type: Type the create payload requests. Updates write the stored
            row type and only change ``enabled``.

    Returns:
        Readback list from the service.

    Raises:
        OnvifError: When the service rejects the write.
    """

    async def run():
        if operation == "create":
            return await config.create_mask(
                MEDIA_SERVICES,
                "src-conf-main",
                {"points": _MASK_POINTS, "enabled": True, "mask_type": mask_type},
                None,
                None,
                "tenant-a",
                "site-a",
            )
        return await config.update_mask(
            MEDIA_SERVICES,
            "mask-1",
            "src-conf-main",
            {"enabled": False},
            None,
            None,
            "tenant-a",
            "site-a",
        )

    return asyncio.run(run())


def _options_dict(*, types=_MISSING, positions=_MISSING):
    """Build an OSD options dictionary, omitting keys the caller left missing.

    Args:
        types: Value for ``types``, or ``_MISSING`` to omit the key.
        positions: Value for ``positions``, or ``_MISSING`` to omit the key.

    Returns:
        Options dictionary with a maximum OSD count.
    """
    options = {"maximum_total": 8}
    if types is not _MISSING:
        options["types"] = types
    if positions is not _MISSING:
        options["positions"] = positions
    return options


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "types",
    [[], None, _MISSING],
    ids=["empty", "none", "missing"],
)
def test_osd_type_list_empty_or_missing_sends_no_mutation(monkeypatch, operation, types):
    """Reject OSD create and update when the advertised type list cannot be used."""
    options = _options_dict(types=types, positions=["UpperLeft", "LowerRight"])
    soap = _install_osd_options(monkeypatch, options)
    with pytest.raises(OnvifError) as error:
        _run_osd(operation)
    _assert_rejected(error, _recorded(soap), "OSD type")


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    ("types", "text_type"),
    [(["Text", "Plain"], "Date"), (["Image"], "Plain")],
    ids=["unadvertised-text-type", "unadvertised-osd-type"],
)
def test_unadvertised_osd_type_sends_no_mutation(monkeypatch, operation, types, text_type):
    """Reject a type the camera did not include in a non-empty type list."""
    options = _options_dict(types=types, positions=["UpperLeft"])
    soap = _install_osd_options(
        monkeypatch,
        options,
        position_type="UpperLeft",
        text_type=text_type,
    )
    with pytest.raises(OnvifError) as error:
        _run_osd(operation, text_type=text_type)
    _assert_rejected(error, _recorded(soap), "OSD type")


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("text_type", ["Plain", "Date"])
def test_advertised_osd_type_is_written(monkeypatch, operation, text_type):
    """Write an OSD type only when Text and that text type are both advertised."""
    options = _options_dict(
        types=["Text", "Plain", "Date"],
        positions=["UpperLeft"],
    )
    soap = _install_osd_options(monkeypatch, options, text_type=text_type)
    _run_osd(operation, text_type=text_type)
    calls = _recorded(soap)
    assert _mutations(calls) == [
        f"{MEDIA_NS}/{'CreateOSD' if operation == 'create' else 'SetOSD'}"
    ]
    assert f"<tt:Type>{text_type}</tt:Type>" in calls[0][1]
    assert "<tt:Type>Text</tt:Type>" in calls[0][1]


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "positions",
    [[], None, _MISSING],
    ids=["empty", "none", "missing"],
)
def test_osd_position_list_empty_or_missing_sends_no_mutation(monkeypatch, operation, positions):
    """Reject OSD create and update when no position list was advertised."""
    options = _options_dict(types=["Text", "Plain"], positions=positions)
    soap = _install_osd_options(monkeypatch, options, position_type="LowerRight")
    with pytest.raises(OnvifError) as error:
        _run_osd(operation, position_type="LowerRight")
    _assert_rejected(error, _recorded(soap), "OSD position")


@pytest.mark.parametrize("operation", ["create", "update"])
def test_unadvertised_osd_position_sends_no_mutation(monkeypatch, operation):
    """Reject LowerRight when the camera advertised only UpperLeft."""
    options = _options_dict(types=["Text", "Plain"], positions=["UpperLeft"])
    soap = _install_osd_options(monkeypatch, options, position_type="LowerRight")
    with pytest.raises(OnvifError) as error:
        _run_osd(operation, position_type="LowerRight")
    _assert_rejected(error, _recorded(soap), "OSD position")


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("position_type", ["UpperLeft", "LowerRight"])
def test_advertised_osd_position_is_written(monkeypatch, operation, position_type):
    """Write a position only when that position is advertised."""
    options = _options_dict(types=["Text", "Plain"], positions=["UpperLeft", "LowerRight"])
    soap = _install_osd_options(monkeypatch, options, position_type=position_type)
    _run_osd(operation, position_type=position_type)
    calls = _recorded(soap)
    assert _mutations(calls) == [
        f"{MEDIA_NS}/{'CreateOSD' if operation == 'create' else 'SetOSD'}"
    ]
    assert f"<tt:Type>{position_type}</tt:Type>" in calls[0][1]


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "types",
    [[], _MISSING],
    ids=["empty", "missing"],
)
def test_device_xml_osd_type_list_sends_no_mutation(monkeypatch, operation, types):
    """An empty or omitted Type list from GetOSDOptions must not be written."""
    calls = _install_osd_soap(
        monkeypatch,
        _osd_options_xml(types=types, positions=["UpperLeft"]),
        _osd_existing_xml(),
    )
    with pytest.raises(OnvifError) as error:
        _run_osd(operation)
    _assert_rejected(error, calls, "OSD type")


@pytest.mark.parametrize("operation", ["create", "update"])
def test_device_xml_rejects_lower_right_when_only_upper_left_is_advertised(monkeypatch, operation):
    """PositionOption UpperLeft alone must not be followed by a LowerRight write."""
    calls = _install_osd_soap(
        monkeypatch,
        _osd_options_xml(types=["Text", "Plain"], positions=["UpperLeft"]),
        _osd_existing_xml(position_type="LowerRight"),
    )
    with pytest.raises(OnvifError) as error:
        _run_osd(operation, position_type="LowerRight")
    _assert_rejected(error, calls, "OSD position")


@pytest.mark.parametrize("operation", ["create", "update"])
def test_device_xml_writes_advertised_position(monkeypatch, operation):
    """A PositionOption listed by the device is still written."""
    calls = _install_osd_soap(
        monkeypatch,
        _osd_options_xml(types=["Text", "Plain"], positions=["LowerRight"]),
        _osd_existing_xml(position_type="LowerRight"),
    )
    _run_osd(operation, position_type="LowerRight")
    assert _mutations(calls) == [
        f"{MEDIA_NS}/{'CreateOSD' if operation == 'create' else 'SetOSD'}"
    ]
    body = next(body for action, body in calls if action.endswith(_mutations(calls)[0].rsplit("/", 1)[-1]))
    assert "<tt:Type>LowerRight</tt:Type>" in body


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "types",
    [[], None, _MISSING],
    ids=["empty", "none", "missing"],
)
def test_mask_type_list_empty_or_missing_sends_no_mutation(monkeypatch, operation, types):
    """Reject mask create and update when the advertised type list cannot be used."""
    result = {"max_masks": 8, "max_points": 8, "rectangle_only": False}
    if types is not _MISSING:
        result["types"] = types
    _pin(monkeypatch)
    calls = []

    async def soap(_xaddr, action, body, *_args, **_kwargs):
        calls.append((action, body))
        return DET.fromstring("<Envelope><Options/></Envelope>")

    monkeypatch.setattr(config, "_soap", soap)
    monkeypatch.setattr(config, "mask_options", lambda _root: result)
    monkeypatch.setattr(
        config,
        "list_masks",
        AsyncMock(return_value=[_mask_row("Color")]),
    )
    with pytest.raises(OnvifError) as error:
        _run_mask(operation, "Color")
    _assert_rejected(error, calls, "privacy-mask type")


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "types",
    [[], _MISSING],
    ids=["empty", "missing"],
)
def test_device_xml_mask_type_list_sends_no_mutation(monkeypatch, operation, types):
    """An empty or omitted Types list from GetMaskOptions must not be written."""
    calls = _install_mask_soap(monkeypatch, _mask_options_xml(types), _mask_row("Color"))
    with pytest.raises(OnvifError) as error:
        _run_mask(operation, "Color")
    _assert_rejected(error, calls, "privacy-mask type")


@pytest.mark.parametrize("operation", ["create", "update"])
def test_unadvertised_mask_type_sends_no_mutation(monkeypatch, operation):
    """Reject Blurred when the camera advertised only Color."""
    calls = _install_mask_soap(
        monkeypatch,
        _mask_options_xml(["Color"]),
        _mask_row("Blurred"),
    )
    with pytest.raises(OnvifError) as error:
        _run_mask(operation, "Blurred")
    _assert_rejected(error, calls, "privacy-mask type")


@pytest.mark.parametrize("operation", ["create", "update"])
def test_advertised_mask_type_is_written(monkeypatch, operation):
    """Write a privacy-mask type only when that type is advertised."""
    calls = _install_mask_soap(
        monkeypatch,
        _mask_options_xml(["Color", "Blurred"]),
        _mask_row("Color"),
    )
    _run_mask(operation, "Color")
    suffix = "/CreateMask" if operation == "create" else "/SetMask"
    assert _mutations(calls) == [f"{MEDIA2_NS}{suffix}"]
    body = next(body for action, body in calls if action.endswith(suffix))
    assert "<tr2:Type>Color</tr2:Type>" in body
