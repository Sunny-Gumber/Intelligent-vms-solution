from copy import deepcopy
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from app.services.network_policy import pin_site_http_xaddr
from app.services.onvif_client import (
    DEVICE_NS,
    IMAGING_NS,
    MEDIA2_NS,
    MEDIA_NS,
    OnvifError,
    _child_text,
    _first_text,
    _local,
    _service_xaddr,
    _soap,
    _xml_attr,
    _xml_text,
)


def _unsupported(message: str) -> OnvifError:
    return OnvifError("UNSUPPORTED_CAPABILITY", message, 422)


def _first_element(root, local_name: str):
    return next((element for element in root.iter() if _local(element.tag) == local_name), None)


def _child(root, local_name: str):
    return next((element for element in list(root) if _local(element.tag) == local_name), None)


def _text(root, local_name: str) -> str | None:
    element = _first_element(root, local_name)
    if element is None or element.text is None:
        return None
    return element.text.strip()


def _float_text(root, local_name: str) -> float | None:
    value = _text(root, local_name)
    return float(value) if value not in {None, ""} else None


def _int_text(root, local_name: str) -> int | None:
    value = _text(root, local_name)
    return int(float(value)) if value not in {None, ""} else None


def _bool_text(root, local_name: str) -> bool | None:
    value = _text(root, local_name)
    if value is None:
        return None
    if value.lower() in {"true", "1"}:
        return True
    if value.lower() in {"false", "0"}:
        return False
    return None


def _range(root, local_name: str) -> dict[str, float] | None:
    node = _first_element(root, local_name)
    if node is None:
        return None
    minimum = _child_text(node, "Min")
    maximum = _child_text(node, "Max")
    if minimum is None or maximum is None:
        return None
    return {"min": float(minimum), "max": float(maximum)}


def _section(root, local_name: str):
    """Return the first nested ONVIF section by local XML name."""
    return _first_element(root, local_name)


def _range_in(root, section_name: str, range_name: str) -> dict[str, float] | None:
    section = _section(root, section_name)
    return _range(section, range_name) if section is not None else None


def _modes_in(root, section_name: str, mode_name: str = "Mode") -> list[str]:
    section = _section(root, section_name)
    return _all_text(section, mode_name) if section is not None else []


def _all_text(root, local_name: str) -> list[str]:
    values = []
    for element in root.iter():
        if _local(element.tag) == local_name and element.text:
            value = element.text.strip()
            if value and value not in values:
                values.append(value)
    return values


def _set_text(root, local_name: str, value: object) -> None:
    element = _first_element(root, local_name)
    if element is None:
        raise _unsupported(f"Camera does not expose writable {local_name}")
    if isinstance(value, bool):
        element.text = "true" if value else "false"
    else:
        element.text = str(value)


def _service(
    services: list[dict],
    fragment: str,
    tenant_id: str,
    site_id: str,
) -> str:
    xaddr = _service_xaddr(services, fragment)
    if not xaddr:
        raise _unsupported(f"Camera did not advertise {fragment} service")
    return pin_site_http_xaddr(xaddr, tenant_id, site_id)


def _optional_token_attr(token: object) -> str:
    """Quote an optional ONVIF token attribute without changing a legal token.

    Args:
        token: Token to place in a ``token`` attribute. ``None`` and ``""`` omit it.

    Returns:
        `` token="..."`` including a leading space, or an empty string.

    Raises:
        OnvifError: If the token contains a character XML 1.0 forbids.
    """
    if token is None or token == "":
        return ""
    return f" token={_xml_attr(token)}"


def _points_xml(points) -> str:
    """Build Media2 polygon point elements with quoted coordinates."""
    return "".join(
        f"<tt:Point x={_xml_attr(point[0])} y={_xml_attr(point[1])}/>"
        for point in points
    )


def _validate_range(name: str, value: float | int | None, bounds: dict | None) -> None:
    if value is None:
        return
    if bounds is None:
        raise _unsupported(f"Camera did not advertise writable range for {name}")
    if float(value) < bounds["min"] or float(value) > bounds["max"]:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            f"Requested {name} is outside the camera-advertised range",
            422,
        )


def _validate_mode(name: str, value: str | None, modes: list[str]) -> None:
    if value is None:
        return
    normalized = {mode.upper() for mode in modes}
    if not normalized or value.upper() not in normalized:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            f"Requested {name} is not advertised by the camera",
            422,
        )


def profile_by_token(profiles: list[dict], token: str | None) -> dict:
    """Return a stored ONVIF profile by token.

    Args:
        profiles: Stored normalized profile dictionaries.
        token: Selected profile token.

    Returns:
        Matching profile dictionary.

    Raises:
        OnvifError: If the profile token is absent or no longer in the snapshot.
    """
    if not token:
        raise _unsupported("Requested managed stream role has no selected profile")
    profile = next((item for item in profiles if item.get("token") == token), None)
    if profile is None:
        raise OnvifError(
            "CAPABILITY_REFRESH_REQUIRED",
            "Selected profile is not present in the stored ONVIF snapshot",
            409,
        )
    return profile


def encoder_configuration(root) -> dict:
    """Parse one ONVIF VideoEncoderConfiguration response.

    Args:
        root: Parsed SOAP response.

    Returns:
        Bounded normalized encoder state.
    """
    config = _first_element(root, "Configuration")
    if config is None:
        raise OnvifError("DEVICE_SERVICE_INVALID", "Camera returned no encoder configuration")
    resolution = _first_element(config, "Resolution")
    rate = _first_element(config, "RateControl")
    return {
        "token": config.attrib.get("token"),
        "encoding": _child_text(config, "Encoding"),
        "width": _int_text(resolution, "Width") if resolution is not None else None,
        "height": _int_text(resolution, "Height") if resolution is not None else None,
        "quality": _float_text(config, "Quality"),
        "fps": _float_text(rate, "FrameRateLimit") if rate is not None else None,
        "encoding_interval": (
            _int_text(rate, "EncodingInterval") if rate is not None else None
        ),
        "bitrate_kbps": _int_text(rate, "BitrateLimit") if rate is not None else None,
        "bitrate_mode": _text(rate, "BitrateType") if rate is not None else None,
        "gov_length": _int_text(config, "GovLength"),
    }


def encoder_options(root) -> dict:
    """Parse standard writable encoder options returned by ONVIF Media.

    Args:
        root: Parsed GetVideoEncoderConfigurationOptions response.

    Returns:
        Normalized supported resolutions, encodings and numeric ranges.
    """
    resolutions: list[dict[str, int]] = []
    for element in root.iter():
        if _local(element.tag) != "ResolutionsAvailable":
            continue
        width = _child_text(element, "Width")
        height = _child_text(element, "Height")
        if width and height:
            item = {"width": int(width), "height": int(height)}
            if item not in resolutions:
                resolutions.append(item)

    encodings = []
    for name in ("H264", "H265", "JPEG", "MPEG4"):
        if any(_local(element.tag).upper() == name for element in root.iter()):
            encodings.append(name)

    bitrate_modes = _all_text(root, "BitrateType")
    return {
        "encodings": encodings,
        "resolutions": resolutions,
        "quality_range": _range(root, "QualityRange"),
        "fps_range": _range(root, "FrameRateRange"),
        "encoding_interval_range": _range(root, "EncodingIntervalRange"),
        "bitrate_range": _range(root, "BitrateRange"),
        "gov_length_range": _range(root, "GovLengthRange"),
        "bitrate_modes": bitrate_modes,
    }


async def get_encoder(
    services: list[dict],
    profile: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Read encoder state/options for one ONVIF profile.

    Args:
        services: Stored ONVIF service descriptors.
        profile: Managed profile snapshot containing configuration tokens.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Dictionary with current encoder configuration and advertised options.

    Raises:
        OnvifError: If the profile/configuration or Media service is unavailable.
        TargetNotAllowed: If the advertised service violates site network policy.
    """
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    config_token = profile.get("video_encoder_configuration_token")
    profile_token = profile.get("token")
    if not config_token or not profile_token:
        raise OnvifError(
            "CAPABILITY_REFRESH_REQUIRED",
            "ONVIF profile configuration tokens are missing; refresh capabilities",
            409,
        )
    configuration_root = await _soap(
        media,
        f"{MEDIA_NS}/GetVideoEncoderConfiguration",
        (
            "<trt:GetVideoEncoderConfiguration>"
            f"<trt:ConfigurationToken>{_xml_text(config_token)}</trt:ConfigurationToken>"
            "</trt:GetVideoEncoderConfiguration>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    options_root = await _soap(
        media,
        f"{MEDIA_NS}/GetVideoEncoderConfigurationOptions",
        (
            "<trt:GetVideoEncoderConfigurationOptions>"
            f"<trt:ConfigurationToken>{_xml_text(config_token)}</trt:ConfigurationToken>"
            f"<trt:ProfileToken>{_xml_text(profile_token)}</trt:ProfileToken>"
            "</trt:GetVideoEncoderConfigurationOptions>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return {
        "current": encoder_configuration(configuration_root),
        "options": encoder_options(options_root),
        "_configuration_root": configuration_root,
        "_media_xaddr": media,
    }


def _validate_encoder_update(current: dict, options: dict, changes: dict) -> None:
    if changes.get("encoding") is not None:
        encoding = changes["encoding"].upper()
        supported = {value.upper() for value in options["encodings"]}
        if not supported or encoding not in supported:
            raise OnvifError(
                "VALUE_NOT_SUPPORTED",
                "Requested encoding is not advertised by the camera",
                422,
            )
        if current.get("encoding") and current["encoding"].upper() != encoding:
            raise _unsupported(
                "Cross-codec encoder switching requires device-qualified Media2 handling"
            )

    if changes.get("width") is not None:
        requested = {"width": changes["width"], "height": changes["height"]}
        if requested not in options["resolutions"]:
            raise OnvifError(
                "VALUE_NOT_SUPPORTED",
                "Requested resolution is not advertised by the camera",
                422,
            )
    _validate_range("quality", changes.get("quality"), options["quality_range"])
    _validate_range("frame rate", changes.get("fps"), options["fps_range"])
    _validate_range(
        "encoding interval",
        changes.get("encoding_interval"),
        options["encoding_interval_range"],
    )
    _validate_range("bitrate", changes.get("bitrate_kbps"), options["bitrate_range"])
    _validate_range("GOV length", changes.get("gov_length"), options["gov_length_range"])
    if changes.get("bitrate_mode") is not None:
        _validate_mode("bitrate mode", changes["bitrate_mode"], options["bitrate_modes"])


async def set_encoder(
    services: list[dict],
    profile: dict,
    changes: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Validate, write and read back one ONVIF encoder configuration.

    Args:
        services: Stored service descriptors.
        profile: Managed profile snapshot.
        changes: Validated encoder fields supplied by the API.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Readback state/options after successful write.

    Raises:
        OnvifError: If options reject the request or ONVIF read/write fails.
    """
    state = await get_encoder(
        services,
        profile,
        username,
        password,
        tenant_id,
        site_id,
    )
    current = state["current"]
    options = state["options"]
    _validate_encoder_update(current, options, changes)

    config = _first_element(state["_configuration_root"], "Configuration")
    if config is None:
        raise OnvifError("DEVICE_SERVICE_INVALID", "Camera returned no encoder configuration")
    config = deepcopy(config)
    if changes.get("encoding") is not None:
        _set_text(config, "Encoding", changes["encoding"])
    if changes.get("width") is not None:
        _set_text(config, "Width", changes["width"])
        _set_text(config, "Height", changes["height"])
    if changes.get("quality") is not None:
        _set_text(config, "Quality", changes["quality"])
    if changes.get("fps") is not None:
        _set_text(config, "FrameRateLimit", changes["fps"])
    if changes.get("encoding_interval") is not None:
        _set_text(config, "EncodingInterval", changes["encoding_interval"])
    if changes.get("bitrate_kbps") is not None:
        _set_text(config, "BitrateLimit", changes["bitrate_kbps"])
    if changes.get("bitrate_mode") is not None:
        _set_text(config, "BitrateType", changes["bitrate_mode"])
    if changes.get("gov_length") is not None:
        _set_text(config, "GovLength", changes["gov_length"])

    xml = ET.tostring(config, encoding="unicode")
    await _soap(
        state["_media_xaddr"],
        f"{MEDIA_NS}/SetVideoEncoderConfiguration",
        (
            "<trt:SetVideoEncoderConfiguration>"
            f"{xml}"
            "<trt:ForcePersistence>true</trt:ForcePersistence>"
            "</trt:SetVideoEncoderConfiguration>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await get_encoder(
        services,
        profile,
        username,
        password,
        tenant_id,
        site_id,
    )


def imaging_settings(root) -> dict:
    """Parse standard ONVIF imaging settings from a SOAP response.

    Args:
        root: Parsed GetImagingSettings response.

    Returns:
        Bounded normalized imaging state.
    """
    settings = _first_element(root, "ImagingSettings")
    if settings is None:
        raise OnvifError("DEVICE_SERVICE_INVALID", "Camera returned no imaging settings")
    white_balance = _first_element(settings, "WhiteBalance")
    backlight = _first_element(settings, "BacklightCompensation")
    wdr = _first_element(settings, "WideDynamicRange")
    exposure = _first_element(settings, "Exposure")
    return {
        "brightness": _float_text(settings, "Brightness"),
        "contrast": _float_text(settings, "Contrast"),
        "saturation": _float_text(settings, "ColorSaturation"),
        "sharpness": _float_text(settings, "Sharpness"),
        "hue": _float_text(settings, "Hue"),
        "ir_cut_filter": _text(settings, "IrCutFilter"),
        "white_balance": {
            "mode": _text(white_balance, "Mode") if white_balance is not None else None,
            "r_gain": (
                _float_text(white_balance, "RGain")
                if white_balance is not None
                else None
            ),
            "b_gain": (
                _float_text(white_balance, "BGain")
                if white_balance is not None
                else None
            ),
        },
        "backlight": {
            "mode": _text(backlight, "Mode") if backlight is not None else None,
            "level": _float_text(backlight, "Level") if backlight is not None else None,
        },
        "wdr": {
            "mode": _text(wdr, "Mode") if wdr is not None else None,
            "level": _float_text(wdr, "Level") if wdr is not None else None,
        },
        "exposure": {
            "mode": _text(exposure, "Mode") if exposure is not None else None,
            "priority": _text(exposure, "Priority") if exposure is not None else None,
            "exposure_time": (
                _float_text(exposure, "ExposureTime") if exposure is not None else None
            ),
            "gain": _float_text(exposure, "Gain") if exposure is not None else None,
            "iris": _float_text(exposure, "Iris") if exposure is not None else None,
        },
        "anti_flicker": (
            _text(settings, "AntiFlicker")
            or _text(settings, "FlickerControl")
            or _text(settings, "PowerLineFrequency")
        ),
    }


def imaging_options(root) -> dict:
    """Parse standard imaging ranges/modes advertised by ONVIF GetOptions.

    Args:
        root: Parsed GetOptions response.

    Returns:
        Normalized option map used for fail-closed validation.
    """
    return {
        "brightness_range": _range(root, "Brightness"),
        "contrast_range": _range(root, "Contrast"),
        "saturation_range": _range(root, "ColorSaturation"),
        "sharpness_range": _range(root, "Sharpness"),
        "hue_range": _range(root, "Hue"),
        "ir_cut_filter_modes": _all_text(root, "IrCutFilterModes"),
        "white_balance_modes": (
            _modes_in(root, "WhiteBalance", "Mode")
            or _all_text(root, "WhiteBalanceModes")
        ),
        "white_balance_r_gain_range": (
            _range_in(root, "WhiteBalance", "RGain")
            or _range_in(root, "WhiteBalance", "CrGain")
        ),
        "white_balance_b_gain_range": (
            _range_in(root, "WhiteBalance", "BGain")
            or _range_in(root, "WhiteBalance", "CbGain")
        ),
        "backlight_modes": _modes_in(root, "BacklightCompensation", "Mode"),
        "backlight_level_range": _range_in(root, "BacklightCompensation", "Level"),
        "wdr_modes": _modes_in(root, "WideDynamicRange", "Mode"),
        "wdr_level_range": _range_in(root, "WideDynamicRange", "Level"),
        "exposure_modes": _modes_in(root, "Exposure", "Mode"),
        "exposure_priorities": _modes_in(root, "Exposure", "Priority"),
        "exposure_time_range": (
            _range_in(root, "Exposure", "ExposureTime")
            or _range_in(root, "Exposure", "MinExposureTime")
            or _range_in(root, "Exposure", "MaxExposureTime")
        ),
        "gain_range": _range_in(root, "Exposure", "Gain"),
        "iris_range": _range_in(root, "Exposure", "Iris"),
        "anti_flicker_modes": (
            _all_text(root, "AntiFlicker")
            or _all_text(root, "FlickerControl")
            or _all_text(root, "PowerLineFrequency")
        ),
    }


async def get_imaging(
    services: list[dict],
    video_source_token: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Read standard imaging settings/options for a video source.

    Args:
        services: Stored service descriptors.
        video_source_token: Physical ONVIF video source token.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Current imaging state/options.

    Raises:
        OnvifError: If Imaging service or source token is unavailable.
    """
    if not video_source_token:
        raise OnvifError(
            "CAPABILITY_REFRESH_REQUIRED",
            "Video source token is missing; refresh ONVIF capabilities",
            409,
        )
    imaging = _service(services, "/ver20/imaging/wsdl", tenant_id, site_id)
    token = _xml_text(video_source_token)
    settings_root = await _soap(
        imaging,
        f"{IMAGING_NS}/GetImagingSettings",
        (
            "<timg:GetImagingSettings>"
            f"<timg:VideoSourceToken>{token}</timg:VideoSourceToken>"
            "</timg:GetImagingSettings>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    options_root = await _soap(
        imaging,
        f"{IMAGING_NS}/GetOptions",
        (
            "<timg:GetOptions>"
            f"<timg:VideoSourceToken>{token}</timg:VideoSourceToken>"
            "</timg:GetOptions>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return {
        "current": imaging_settings(settings_root),
        "options": imaging_options(options_root),
        "_settings_root": settings_root,
        "_imaging_xaddr": imaging,
    }


def _validate_imaging_update(options: dict, changes: dict) -> None:
    _validate_range("brightness", changes.get("brightness"), options["brightness_range"])
    _validate_range("contrast", changes.get("contrast"), options["contrast_range"])
    _validate_range("saturation", changes.get("saturation"), options["saturation_range"])
    _validate_range("sharpness", changes.get("sharpness"), options["sharpness_range"])
    _validate_range("hue", changes.get("hue"), options["hue_range"])
    _validate_mode(
        "IR cut filter",
        changes.get("ir_cut_filter"),
        options["ir_cut_filter_modes"],
    )
    _validate_mode(
        "white balance mode",
        changes.get("white_balance_mode"),
        options["white_balance_modes"],
    )
    _validate_range(
        "white balance red gain",
        changes.get("white_balance_r_gain"),
        options["white_balance_r_gain_range"],
    )
    _validate_range(
        "white balance blue gain",
        changes.get("white_balance_b_gain"),
        options["white_balance_b_gain_range"],
    )
    _validate_mode(
        "backlight mode",
        changes.get("backlight_mode"),
        options["backlight_modes"],
    )
    _validate_range(
        "backlight level",
        changes.get("backlight_level"),
        options["backlight_level_range"],
    )
    _validate_mode("WDR mode", changes.get("wdr_mode"), options["wdr_modes"])
    _validate_range("WDR level", changes.get("wdr_level"), options["wdr_level_range"])
    _validate_mode(
        "exposure mode",
        changes.get("exposure_mode"),
        options["exposure_modes"],
    )
    _validate_mode(
        "exposure priority",
        changes.get("exposure_priority"),
        options["exposure_priorities"],
    )
    _validate_range(
        "exposure time",
        changes.get("exposure_time"),
        options["exposure_time_range"],
    )
    _validate_range("gain", changes.get("gain"), options["gain_range"])
    _validate_range("iris", changes.get("iris"), options["iris_range"])
    if changes.get("anti_flicker") is not None:
        _validate_mode(
            "anti-flicker",
            changes["anti_flicker"],
            options["anti_flicker_modes"],
        )


def _set_alias(root, names: tuple[str, ...], value: object) -> None:
    for name in names:
        if _first_element(root, name) is not None:
            _set_text(root, name, value)
            return
    raise _unsupported(f"Camera does not expose writable {'/'.join(names)}")


async def set_imaging(
    services: list[dict],
    video_source_token: str,
    changes: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Validate, write and read back standard ONVIF imaging settings.

    Args:
        services: Stored service descriptors.
        video_source_token: Physical ONVIF source token.
        changes: Validated imaging fields.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Imaging readback after write.

    Raises:
        OnvifError: If the request is unsupported/outside options or device write fails.
    """
    state = await get_imaging(
        services,
        video_source_token,
        username,
        password,
        tenant_id,
        site_id,
    )
    _validate_imaging_update(state["options"], changes)
    settings = _first_element(state["_settings_root"], "ImagingSettings")
    if settings is None:
        raise OnvifError("DEVICE_SERVICE_INVALID", "Camera returned no imaging settings")
    settings = deepcopy(settings)

    direct = {
        "brightness": ("Brightness",),
        "contrast": ("Contrast",),
        "saturation": ("ColorSaturation",),
        "sharpness": ("Sharpness",),
        "hue": ("Hue",),
        "ir_cut_filter": ("IrCutFilter",),
        "white_balance_mode": ("WhiteBalance", "Mode"),
        "backlight_mode": ("BacklightCompensation", "Mode"),
        "wdr_mode": ("WideDynamicRange", "Mode"),
        "exposure_mode": ("Exposure", "Mode"),
        "exposure_priority": ("Exposure", "Priority"),
    }
    for field, path in direct.items():
        value = changes.get(field)
        if value is None:
            continue
        if len(path) == 1:
            _set_text(settings, path[0], value)
        else:
            parent = _first_element(settings, path[0])
            if parent is None:
                raise _unsupported(f"Camera does not expose writable {path[0]}")
            _set_text(parent, path[1], value)

    aliases = {
        "white_balance_r_gain": ("RGain", "CrGain"),
        "white_balance_b_gain": ("BGain", "CbGain"),
        "backlight_level": ("Level",),
        "wdr_level": ("Level",),
        "exposure_time": ("ExposureTime",),
        "gain": ("Gain",),
        "iris": ("Iris",),
        "anti_flicker": ("AntiFlicker", "FlickerControl", "PowerLineFrequency"),
    }
    for field, names in aliases.items():
        value = changes.get(field)
        if value is None:
            continue
        if field == "backlight_level":
            parent = _first_element(settings, "BacklightCompensation")
            if parent is None:
                raise _unsupported("Camera does not expose BacklightCompensation")
            _set_alias(parent, names, value)
        elif field == "wdr_level":
            parent = _first_element(settings, "WideDynamicRange")
            if parent is None:
                raise _unsupported("Camera does not expose WideDynamicRange")
            _set_alias(parent, names, value)
        elif field in {"exposure_time", "gain", "iris"}:
            parent = _first_element(settings, "Exposure")
            if parent is None:
                raise _unsupported("Camera does not expose Exposure")
            _set_alias(parent, names, value)
        elif field.startswith("white_balance_"):
            parent = _first_element(settings, "WhiteBalance")
            if parent is None:
                raise _unsupported("Camera does not expose WhiteBalance")
            _set_alias(parent, names, value)
        else:
            _set_alias(settings, names, value)

    xml = ET.tostring(settings, encoding="unicode")
    await _soap(
        state["_imaging_xaddr"],
        f"{IMAGING_NS}/SetImagingSettings",
        (
            "<timg:SetImagingSettings>"
            f"<timg:VideoSourceToken>{_xml_text(video_source_token)}</timg:VideoSourceToken>"
            f"{xml}"
            "<timg:ForcePersistence>true</timg:ForcePersistence>"
            "</timg:SetImagingSettings>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await get_imaging(
        services,
        video_source_token,
        username,
        password,
        tenant_id,
        site_id,
    )


def parse_date_time(root) -> dict:
    """Parse ONVIF system date/time state.

    Args:
        root: Parsed GetSystemDateAndTime response.

    Returns:
        Bounded date/time dictionary.
    """
    info = _first_element(root, "SystemDateAndTime")
    if info is None:
        raise OnvifError("DEVICE_SERVICE_INVALID", "Camera returned no system date/time")
    utc = _first_element(info, "UTCDateTime")
    parsed = None
    if utc is not None:
        date = _first_element(utc, "Date")
        time = _first_element(utc, "Time")
        if date is not None and time is not None:
            try:
                parsed = datetime(
                    int(_child_text(date, "Year")),
                    int(_child_text(date, "Month")),
                    int(_child_text(date, "Day")),
                    int(_child_text(time, "Hour")),
                    int(_child_text(time, "Minute")),
                    int(_child_text(time, "Second")),
                    tzinfo=timezone.utc,
                )
            except (TypeError, ValueError):
                parsed = None
    zone = _first_element(info, "TimeZone")
    return {
        "mode": _child_text(info, "DateTimeType"),
        "daylight_savings": _bool_text(info, "DaylightSavings"),
        "timezone": _child_text(zone, "TZ") if zone is not None else None,
        "utc_datetime": parsed,
    }


async def get_date_time(
    device_xaddr: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Read camera system date/time through Device Management.

    Args:
        device_xaddr: Stored device-service endpoint.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Normalized system date/time.

    Raises:
        OnvifError: If the device response is invalid.
    """
    xaddr = pin_site_http_xaddr(device_xaddr, tenant_id, site_id)
    root = await _soap(
        xaddr,
        f"{DEVICE_NS}/GetSystemDateAndTime",
        "<tds:GetSystemDateAndTime/>",
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return parse_date_time(root)


async def set_date_time(
    device_xaddr: str,
    changes: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Write and read back standard ONVIF system date/time.

    Args:
        device_xaddr: Stored device-service endpoint.
        changes: Validated mode/daylight/timezone/UTC settings.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        System date/time readback.

    Raises:
        OnvifError: If the device rejects the write/readback.
    """
    xaddr = pin_site_http_xaddr(device_xaddr, tenant_id, site_id)
    timezone_xml = ""
    if changes.get("timezone"):
        timezone_xml = (
            "<tds:TimeZone>"
            f"<tt:TZ>{_xml_text(changes['timezone'])}</tt:TZ>"
            "</tds:TimeZone>"
        )
    utc_xml = ""
    utc_value = changes.get("utc_datetime")
    if changes["mode"] == "Manual" and utc_value is not None:
        utc = utc_value.astimezone(timezone.utc)
        utc_xml = (
            "<tds:UTCDateTime>"
            "<tt:Time>"
            f"<tt:Hour>{utc.hour}</tt:Hour>"
            f"<tt:Minute>{utc.minute}</tt:Minute>"
            f"<tt:Second>{utc.second}</tt:Second>"
            "</tt:Time>"
            "<tt:Date>"
            f"<tt:Year>{utc.year}</tt:Year>"
            f"<tt:Month>{utc.month}</tt:Month>"
            f"<tt:Day>{utc.day}</tt:Day>"
            "</tt:Date>"
            "</tds:UTCDateTime>"
        )
    await _soap(
        xaddr,
        f"{DEVICE_NS}/SetSystemDateAndTime",
        (
            "<tds:SetSystemDateAndTime>"
            f"<tds:DateTimeType>{changes['mode']}</tds:DateTimeType>"
            f"<tds:DaylightSavings>{str(bool(changes.get('daylight_savings'))).lower()}</tds:DaylightSavings>"
            f"{timezone_xml}{utc_xml}"
            "</tds:SetSystemDateAndTime>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await get_date_time(
        device_xaddr,
        username,
        password,
        tenant_id,
        site_id,
    )


async def supported_auxiliary_commands(
    device_xaddr: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[str]:
    """Read standard auxiliary command capabilities from Device Management.

    Args:
        device_xaddr: Stored device-service endpoint.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Advertised auxiliary command strings.

    Raises:
        OnvifError: If the device rejects the capabilities request.
    """
    xaddr = pin_site_http_xaddr(device_xaddr, tenant_id, site_id)
    root = await _soap(
        xaddr,
        f"{DEVICE_NS}/GetServiceCapabilities",
        "<tds:GetServiceCapabilities/>",
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    commands = []
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.rsplit("}", 1)[-1] == "AuxiliaryCommands":
                commands.extend(
                    item for item in re.split(r"[,\s]+", value) if item
                )
    commands.extend(_all_text(root, "AuxiliaryCommands"))
    return list(dict.fromkeys(commands))


async def set_ir_lamp(
    device_xaddr: str,
    mode: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Send a standard tt:IRLamp auxiliary command when advertised.

    Args:
        device_xaddr: Stored device-service endpoint.
        mode: On, Off or Auto.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Dictionary identifying the accepted standard command.

    Raises:
        OnvifError: If the camera does not advertise the requested IR command.
    """
    command = f"tt:IRLamp|{mode}"
    commands = await supported_auxiliary_commands(
        device_xaddr,
        username,
        password,
        tenant_id,
        site_id,
    )
    if command not in commands:
        raise _unsupported("Camera did not advertise the requested standard IR-lamp command")
    xaddr = pin_site_http_xaddr(device_xaddr, tenant_id, site_id)
    await _soap(
        xaddr,
        f"{DEVICE_NS}/SendAuxiliaryCommand",
        (
            "<tds:SendAuxiliaryCommand>"
            f"<tds:AuxiliaryCommand>{_xml_text(command)}</tds:AuxiliaryCommand>"
            "</tds:SendAuxiliaryCommand>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return {"command": command, "accepted": True}


def parse_osds(root) -> list[dict]:
    """Parse bounded Media OSD configurations.

    Args:
        root: Parsed GetOSDs response.

    Returns:
        List of token/type/text/position dictionaries.
    """
    rows = []
    for element in root.iter():
        if _local(element.tag) not in {"OSDs", "OSD"}:
            continue
        token = element.attrib.get("token")
        if not token:
            continue
        position = _first_element(element, "Position")
        pos = _first_element(position, "Pos") if position is not None else None
        text_string = _first_element(element, "TextString")
        rows.append(
            {
                "token": token,
                "type": _text(element, "Type"),
                "position_type": _text(position, "Type") if position is not None else None,
                "x": float(pos.attrib["x"]) if pos is not None and "x" in pos.attrib else None,
                "y": float(pos.attrib["y"]) if pos is not None and "y" in pos.attrib else None,
                "video_source_configuration_token": _text(
                    element, "VideoSourceConfigurationToken"
                ),
                "text_type": _text(text_string, "Type") if text_string is not None else None,
                "text": _text(text_string, "PlainText") if text_string is not None else None,
            }
        )
    return rows


async def list_osds(
    services: list[dict],
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """List Media OSD configurations.

    Args:
        services: Stored service descriptors.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Bounded OSD configuration list.

    Raises:
        OnvifError: If Media service/OSD operation is unavailable.
    """
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    root = await _soap(
        media,
        f"{MEDIA_NS}/GetOSDs",
        "<trt:GetOSDs/>",
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return parse_osds(root)


async def get_osd_options(
    services: list[dict],
    video_source_configuration_token: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Read ONVIF OSD options for one source configuration.

    Args:
        services: Stored service descriptors.
        video_source_configuration_token: Target source configuration.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Bounded OSD option summary including supported types and maximum count.

    Raises:
        OnvifError: If Media/OSD options are unavailable.
    """
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    root = await _soap(
        media,
        f"{MEDIA_NS}/GetOSDOptions",
        (
            "<trt:GetOSDOptions>"
            f"<trt:VideoSourceConfigurationToken>{_xml_text(video_source_configuration_token)}</trt:VideoSourceConfigurationToken>"
            "</trt:GetOSDOptions>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    types = _all_text(root, "Type")
    maximum = _first_element(root, "MaximumNumberOfOSDs")
    total = None
    if maximum is not None:
        raw = maximum.attrib.get("Total") or _child_text(maximum, "Total")
        if raw:
            total = int(raw)
    return {"types": types, "maximum_total": total}


async def create_osd(
    services: list[dict],
    video_source_configuration_token: str,
    payload: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """Create a text/date/time OSD and return readback list.

    Args:
        services: Stored service descriptors.
        video_source_configuration_token: Target ONVIF source configuration token.
        payload: Validated OSD text/type/position values. An optional ``token``
            key is emitted as the OSD token attribute when it is non-empty.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Full OSD list after creation.

    Raises:
        OnvifError: If the device does not support the requested OSD operation,
            or a supplied token contains a character XML 1.0 forbids.
    """
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    options = await get_osd_options(
        services,
        video_source_configuration_token,
        username,
        password,
        tenant_id,
        site_id,
    )
    existing = await list_osds(
        services,
        username,
        password,
        tenant_id,
        site_id,
    )
    if options["maximum_total"] is not None and len(existing) >= options["maximum_total"]:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Camera has reached its advertised maximum OSD count",
            422,
        )
    if options["types"] and "Text" not in options["types"]:
        raise _unsupported("Camera did not advertise text OSD support")
    plain = ""
    if payload["osd_type"] == "Plain":
        plain = f"<tt:PlainText>{_xml_text(payload['text'])}</tt:PlainText>"
    pos = ""
    if payload["position_type"] == "Custom":
        pos = f"<tt:Pos x={_xml_attr(payload['x'])} y={_xml_attr(payload['y'])}/>"
    body = (
        f"<trt:CreateOSD><trt:OSD{_optional_token_attr(payload.get('token'))}>"
        f"<tt:VideoSourceConfigurationToken>{_xml_text(video_source_configuration_token)}</tt:VideoSourceConfigurationToken>"
        "<tt:Type>Text</tt:Type>"
        "<tt:Position>"
        f"<tt:Type>{_xml_text(payload['position_type'])}</tt:Type>{pos}"
        "</tt:Position>"
        "<tt:TextString>"
        f"<tt:Type>{_xml_text(payload['osd_type'])}</tt:Type>{plain}"
        "</tt:TextString>"
        "</trt:OSD></trt:CreateOSD>"
    )
    await _soap(
        media,
        f"{MEDIA_NS}/CreateOSD",
        body,
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await list_osds(services, username, password, tenant_id, site_id)


async def update_osd(
    services: list[dict],
    osd_token: str,
    payload: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """Modify an existing Media text OSD and return list readback.

    Args:
        services: Stored service descriptors.
        osd_token: OSD token to modify.
        payload: Validated text/position patch.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Full OSD list after modification.

    Raises:
        OnvifError: If the OSD does not exist or cannot be modified.
    """
    existing = await list_osds(services, username, password, tenant_id, site_id)
    row = next((item for item in existing if item["token"] == osd_token), None)
    if row is None:
        raise OnvifError("OSD_NOT_FOUND", "Requested OSD does not exist", 404)
    text_type = row.get("text_type") or "Plain"
    if payload.get("text") is not None and text_type != "Plain":
        raise _unsupported("Only Plain text OSD content can be edited through this endpoint")
    text_value = payload.get("text", row.get("text"))
    x = payload.get("x", row.get("x"))
    y = payload.get("y", row.get("y"))
    position_type = row.get("position_type") or "Custom"
    position = ""
    if position_type == "Custom" and x is not None and y is not None:
        position = f"<tt:Pos x={_xml_attr(x)} y={_xml_attr(y)}/>"
    text_xml = f"<tt:Type>{_xml_text(text_type)}</tt:Type>"
    if text_type == "Plain":
        text_xml += f"<tt:PlainText>{_xml_text(text_value or '')}</tt:PlainText>"
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    body = (
        "<trt:SetOSD>"
        f"<trt:OSD token={_xml_attr(osd_token)}>"
        f"<tt:VideoSourceConfigurationToken>{_xml_text(row.get('video_source_configuration_token') or '')}</tt:VideoSourceConfigurationToken>"
        "<tt:Type>Text</tt:Type>"
        "<tt:Position>"
        f"<tt:Type>{_xml_text(position_type)}</tt:Type>{position}"
        "</tt:Position>"
        f"<tt:TextString>{text_xml}</tt:TextString>"
        "</trt:OSD>"
        "</trt:SetOSD>"
    )
    await _soap(
        media,
        f"{MEDIA_NS}/SetOSD",
        body,
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await list_osds(services, username, password, tenant_id, site_id)


async def delete_osd(
    services: list[dict],
    osd_token: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """Delete one Media OSD and return list readback.

    Args:
        services: Stored service descriptors.
        osd_token: OSD token to delete.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Full OSD list after deletion.
    """
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    await _soap(
        media,
        f"{MEDIA_NS}/DeleteOSD",
        (
            "<trt:DeleteOSD>"
            f"<trt:OSDToken>{_xml_text(osd_token)}</trt:OSDToken>"
            "</trt:DeleteOSD>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await list_osds(services, username, password, tenant_id, site_id)


def parse_masks(root) -> list[dict]:
    """Parse bounded Media2 privacy-mask configurations.

    Args:
        root: Parsed GetMasks response.

    Returns:
        List of mask tokens, type/enabled values and normalized polygons.
    """
    rows = []
    for element in root.iter():
        if _local(element.tag) not in {"Mask", "Masks"}:
            continue
        token = element.attrib.get("token")
        if not token:
            continue
        points = []
        for point in element.iter():
            if _local(point.tag) != "Point":
                continue
            if "x" in point.attrib and "y" in point.attrib:
                points.append(
                    {"x": float(point.attrib["x"]), "y": float(point.attrib["y"])}
                )
        rows.append(
            {
                "token": token,
                "type": _text(element, "Type"),
                "enabled": _bool_text(element, "Enabled"),
                "points": points,
            }
        )
    return rows


def mask_options(root) -> dict:
    """Parse Media2 privacy-mask options using the standard schema.

    Args:
        root: Parsed GetMaskOptions response.

    Returns:
        Maximum masks/points, supported types and rectangle-only constraint.
    """
    options = _first_element(root, "Options")
    if options is None:
        raise OnvifError(
            "DEVICE_SERVICE_INVALID",
            "Camera returned no privacy-mask options",
        )
    rectangle_raw = next(
        (
            value
            for key, value in options.attrib.items()
            if key.rsplit("}", 1)[-1] == "RectangleOnly"
        ),
        None,
    )
    return {
        "max_masks": _int_text(options, "MaxMasks"),
        "max_points": _int_text(options, "MaxPoints"),
        "types": _all_text(options, "Types"),
        "rectangle_only": (
            rectangle_raw.lower() in {"true", "1"}
            if rectangle_raw is not None
            else False
        ),
    }


async def list_masks(
    services: list[dict],
    video_source_configuration_token: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """List Media2 privacy masks for one source configuration.

    Args:
        services: Stored service descriptors.
        video_source_configuration_token: Target source configuration.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Bounded privacy-mask list.

    Raises:
        OnvifError: If Media2/mask support is unavailable.
    """
    media2 = _service(services, "/ver20/media/wsdl", tenant_id, site_id)
    root = await _soap(
        media2,
        f"{MEDIA2_NS}/GetMasks",
        (
            "<tr2:GetMasks>"
            f"<tr2:ConfigurationToken>{_xml_text(video_source_configuration_token)}</tr2:ConfigurationToken>"
            "</tr2:GetMasks>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return parse_masks(root)


async def create_mask(
    services: list[dict],
    video_source_configuration_token: str,
    payload: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """Create a Media2 privacy mask and return readback list.

    Args:
        services: Stored service descriptors.
        video_source_configuration_token: Target source configuration.
        payload: Validated normalized polygon/type/enabled values. An optional
            ``token`` key is emitted as the mask token attribute when it is non-empty.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Privacy-mask list after creation.

    Raises:
        OnvifError: If Media2 mask options reject the request, write fails, or a
            supplied token contains a character XML 1.0 forbids.
    """
    media2 = _service(services, "/ver20/media/wsdl", tenant_id, site_id)
    options_root = await _soap(
        media2,
        f"{MEDIA2_NS}/GetMaskOptions",
        (
            "<tr2:GetMaskOptions>"
            f"<tr2:ConfigurationToken>{_xml_text(video_source_configuration_token)}</tr2:ConfigurationToken>"
            "</tr2:GetMaskOptions>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    options = mask_options(options_root)
    existing = await list_masks(
        services,
        video_source_configuration_token,
        username,
        password,
        tenant_id,
        site_id,
    )
    if options["max_masks"] is not None and len(existing) >= options["max_masks"]:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Camera has reached its advertised maximum privacy-mask count",
            422,
        )
    if options["max_points"] is not None and len(payload["points"]) > options["max_points"]:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Privacy-mask polygon has more points than the camera supports",
            422,
        )
    if options["rectangle_only"] and len(payload["points"]) != 4:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Camera accepts only four-point privacy masks",
            422,
        )
    supported_types = options["types"]
    if supported_types and payload["mask_type"] not in supported_types:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Requested privacy-mask type is not advertised by the camera",
            422,
        )
    points_xml = _points_xml(payload["points"])
    body = (
        f"<tr2:CreateMask><tr2:Mask{_optional_token_attr(payload.get('token'))}>"
        f"<tr2:ConfigurationToken>{_xml_text(video_source_configuration_token)}</tr2:ConfigurationToken>"
        f"<tr2:Type>{_xml_text(payload['mask_type'])}</tr2:Type>"
        f"<tr2:Enabled>{str(payload['enabled']).lower()}</tr2:Enabled>"
        f"<tr2:Polygon>{points_xml}</tr2:Polygon>"
        "</tr2:Mask></tr2:CreateMask>"
    )
    await _soap(
        media2,
        f"{MEDIA2_NS}/CreateMask",
        body,
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await list_masks(
        services,
        video_source_configuration_token,
        username,
        password,
        tenant_id,
        site_id,
    )


async def update_mask(
    services: list[dict],
    mask_token: str,
    video_source_configuration_token: str,
    payload: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """Modify a Media2 privacy mask and return readback list.

    Args:
        services: Stored service descriptors.
        mask_token: Existing mask token.
        video_source_configuration_token: Target source configuration.
        payload: Validated mask patch.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Privacy-mask list after modification.

    Raises:
        OnvifError: If the mask is absent or options reject the requested patch.
    """
    existing = await list_masks(
        services,
        video_source_configuration_token,
        username,
        password,
        tenant_id,
        site_id,
    )
    row = next((item for item in existing if item["token"] == mask_token), None)
    if row is None:
        raise OnvifError("MASK_NOT_FOUND", "Requested privacy mask does not exist", 404)
    points = payload.get("points")
    if points is None:
        points = [(point["x"], point["y"]) for point in row.get("points", [])]
    mask_type = payload.get("mask_type", row.get("type") or "Color")
    enabled = payload.get("enabled")
    if enabled is None:
        enabled = bool(row.get("enabled", True))

    media2 = _service(services, "/ver20/media/wsdl", tenant_id, site_id)
    options_root = await _soap(
        media2,
        f"{MEDIA2_NS}/GetMaskOptions",
        (
            "<tr2:GetMaskOptions>"
            f"<tr2:ConfigurationToken>{_xml_text(video_source_configuration_token)}</tr2:ConfigurationToken>"
            "</tr2:GetMaskOptions>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    options = mask_options(options_root)
    if options["max_points"] is not None and len(points) > options["max_points"]:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Privacy-mask polygon has more points than the camera supports",
            422,
        )
    if options["rectangle_only"] and len(points) != 4:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Camera accepts only four-point privacy masks",
            422,
        )
    supported_types = options["types"]
    if supported_types and mask_type not in supported_types:
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Requested privacy-mask type is not advertised by the camera",
            422,
        )
    points_xml = _points_xml(points)
    body = (
        "<tr2:SetMask>"
        f"<tr2:Mask token={_xml_attr(mask_token)}>"
        f"<tr2:ConfigurationToken>{_xml_text(video_source_configuration_token)}</tr2:ConfigurationToken>"
        f"<tr2:Type>{_xml_text(mask_type)}</tr2:Type>"
        f"<tr2:Enabled>{str(enabled).lower()}</tr2:Enabled>"
        f"<tr2:Polygon>{points_xml}</tr2:Polygon>"
        "</tr2:Mask></tr2:SetMask>"
    )
    await _soap(
        media2,
        f"{MEDIA2_NS}/SetMask",
        body,
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await list_masks(
        services,
        video_source_configuration_token,
        username,
        password,
        tenant_id,
        site_id,
    )


async def delete_mask(
    services: list[dict],
    mask_token: str,
    video_source_configuration_token: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """Delete a Media2 privacy mask and return readback list.

    Args:
        services: Stored service descriptors.
        mask_token: Mask token to remove.
        video_source_configuration_token: Source configuration used for readback.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Privacy-mask list after deletion.
    """
    media2 = _service(services, "/ver20/media/wsdl", tenant_id, site_id)
    await _soap(
        media2,
        f"{MEDIA2_NS}/DeleteMask",
        (
            "<tr2:DeleteMask>"
            f"<tr2:Token>{_xml_text(mask_token)}</tr2:Token>"
            "</tr2:DeleteMask>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await list_masks(
        services,
        video_source_configuration_token,
        username,
        password,
        tenant_id,
        site_id,
    )


def parse_video_source_configuration(root) -> dict:
    """Parse ONVIF VideoSourceConfiguration orientation metadata.

    Args:
        root: Parsed GetVideoSourceConfiguration response.

    Returns:
        Rotation/mirror state and configuration token.

    Raises:
        OnvifError: If no source configuration is returned.
    """
    config = _first_element(root, "Configuration")
    if config is None:
        raise OnvifError(
            "DEVICE_SERVICE_INVALID",
            "Camera returned no video source configuration",
        )
    rotate = _first_element(config, "Rotate")
    mirror = _first_element(config, "Mirror")
    mirror_value = None
    if mirror is not None:
        if mirror.text:
            mirror_value = mirror.text.strip().lower() in {"true", "1", "on"}
        else:
            enabled = _text(mirror, "Enabled")
            mirror_value = (
                enabled.lower() in {"true", "1", "on"} if enabled is not None else None
            )
    return {
        "configuration_token": config.attrib.get("token"),
        "rotation_mode": _text(rotate, "Mode") if rotate is not None else None,
        "rotation_degree": _int_text(rotate, "Degree") if rotate is not None else None,
        "mirror": mirror_value,
        "flip": bool(
            mirror_value
            and (_text(rotate, "Mode") or "").upper() == "ON"
            and (_int_text(rotate, "Degree") or 0) == 180
        ),
    }


def parse_video_source_options(root) -> dict:
    """Parse ONVIF source orientation options.

    Args:
        root: Parsed GetVideoSourceConfigurationOptions response.

    Returns:
        Supported rotate modes/degrees and mirror capability.
    """
    rotate = _first_element(root, "Rotate")
    degrees = []
    if rotate is not None:
        for element in rotate.iter():
            if _local(element.tag) in {"Degree", "Items"} and element.text:
                try:
                    value = int(float(element.text.strip()))
                except ValueError:
                    continue
                if value not in degrees:
                    degrees.append(value)
    rotation_modes = _all_text(rotate, "Mode") if rotate is not None else []
    mirror_supported = _first_element(root, "Mirror") is not None
    return {
        "rotation_modes": rotation_modes,
        "rotation_degrees": degrees,
        "mirror_supported": mirror_supported,
        "flip_supported": (
            mirror_supported
            and 180 in degrees
            and "ON" in {mode.upper() for mode in rotation_modes}
        ),
    }


async def get_orientation(
    services: list[dict],
    profile: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Read ONVIF video-source orientation state/options.

    Args:
        services: Stored service descriptors.
        profile: Managed profile containing source configuration token.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Current orientation state and advertised options.

    Raises:
        OnvifError: If the profile lacks required source configuration metadata.
    """
    token = profile.get("video_source_configuration_token")
    if not token:
        raise OnvifError(
            "CAPABILITY_REFRESH_REQUIRED",
            "Video source configuration token is missing; refresh capabilities",
            409,
        )
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    token_xml = _xml_text(token)
    config_root = await _soap(
        media,
        f"{MEDIA_NS}/GetVideoSourceConfiguration",
        (
            "<trt:GetVideoSourceConfiguration>"
            f"<trt:ConfigurationToken>{token_xml}</trt:ConfigurationToken>"
            "</trt:GetVideoSourceConfiguration>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    options_root = await _soap(
        media,
        f"{MEDIA_NS}/GetVideoSourceConfigurationOptions",
        (
            "<trt:GetVideoSourceConfigurationOptions>"
            f"<trt:ConfigurationToken>{token_xml}</trt:ConfigurationToken>"
            f"<trt:ProfileToken>{_xml_text(profile.get('token') or '')}</trt:ProfileToken>"
            "</trt:GetVideoSourceConfigurationOptions>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return {
        "current": parse_video_source_configuration(config_root),
        "options": parse_video_source_options(options_root),
        "_configuration_root": config_root,
        "_media_xaddr": media,
    }


async def set_orientation(
    services: list[dict],
    profile: dict,
    changes: dict,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Validate, write and read back video-source rotation/mirror settings.

    Args:
        services: Stored service descriptors.
        profile: Managed profile snapshot.
        changes: Validated orientation changes.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Orientation readback.

    Raises:
        OnvifError: If requested orientation is not advertised by the camera.
    """
    state = await get_orientation(
        services,
        profile,
        username,
        password,
        tenant_id,
        site_id,
    )
    options = state["options"]
    composite_flip = changes.get("flip")
    if composite_flip is not None and not options["flip_supported"]:
        raise _unsupported(
            "Camera does not advertise the rotation/mirror combination required for vertical flip"
        )
    _validate_mode(
        "rotation mode",
        changes.get("rotation_mode"),
        options["rotation_modes"],
    )
    degree = changes.get("rotation_degree")
    if degree is not None and options["rotation_degrees"]:
        if degree not in options["rotation_degrees"]:
            raise OnvifError(
                "VALUE_NOT_SUPPORTED",
                "Requested rotation degree is not advertised by the camera",
                422,
            )
    if changes.get("mirror") is not None and not options["mirror_supported"]:
        raise _unsupported("Camera did not advertise standard ONVIF mirror control")

    config = _first_element(state["_configuration_root"], "Configuration")
    if config is None:
        raise OnvifError(
            "DEVICE_SERVICE_INVALID",
            "Camera returned no video source configuration",
        )
    config = deepcopy(config)
    rotate = _first_element(config, "Rotate")
    if composite_flip is not None:
        mirror = _first_element(config, "Mirror")
        if rotate is None or mirror is None:
            raise _unsupported(
                "Camera does not expose rotation/mirror configuration required for vertical flip"
            )
        if composite_flip:
            _set_text(rotate, "Mode", "ON")
            _set_text(rotate, "Degree", 180)
            if list(mirror):
                _set_text(mirror, "Enabled", True)
            else:
                mirror.text = "true"
        else:
            modes = {mode.upper() for mode in options["rotation_modes"]}
            if "OFF" in modes:
                _set_text(rotate, "Mode", "OFF")
            elif "ON" in modes and 0 in options["rotation_degrees"]:
                _set_text(rotate, "Mode", "ON")
                _set_text(rotate, "Degree", 0)
            else:
                raise _unsupported(
                    "Camera cannot restore an unflipped standard orientation"
                )
            if list(mirror):
                _set_text(mirror, "Enabled", False)
            else:
                mirror.text = "false"
    if changes.get("rotation_mode") is not None:
        if rotate is None:
            raise _unsupported("Camera does not expose Rotate configuration")
        _set_text(rotate, "Mode", changes["rotation_mode"])
    if degree is not None:
        if rotate is None:
            raise _unsupported("Camera does not expose Rotate configuration")
        _set_text(rotate, "Degree", degree)
    if changes.get("mirror") is not None:
        mirror = _first_element(config, "Mirror")
        if mirror is None:
            raise _unsupported("Camera does not expose Mirror configuration")
        if list(mirror):
            _set_text(mirror, "Enabled", changes["mirror"])
        else:
            mirror.text = "true" if changes["mirror"] else "false"

    xml = ET.tostring(config, encoding="unicode")
    await _soap(
        state["_media_xaddr"],
        f"{MEDIA_NS}/SetVideoSourceConfiguration",
        (
            "<trt:SetVideoSourceConfiguration>"
            f"{xml}"
            "<trt:ForcePersistence>true</trt:ForcePersistence>"
            "</trt:SetVideoSourceConfiguration>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return await get_orientation(
        services,
        profile,
        username,
        password,
        tenant_id,
        site_id,
    )


def parse_video_source_modes(root) -> list[dict]:
    """Parse standard ONVIF video-source modes.

    Args:
        root: Parsed GetVideoSourceModes response.

    Returns:
        Bounded mode list including token, enabled state, resolution, fps,
        encodings, reboot flag and device description.
    """
    rows = []
    for element in root.iter():
        if _local(element.tag) not in {"VideoSourceModes", "VideoSourceMode"}:
            continue
        token = element.attrib.get("token")
        if not token:
            continue
        resolution = _first_element(element, "MaxResolution")
        rows.append(
            {
                "token": token,
                "enabled": str(element.attrib.get("Enabled", "false")).lower()
                in {"true", "1"},
                "max_fps": _float_text(element, "MaxFramerate"),
                "max_width": (
                    _int_text(resolution, "Width") if resolution is not None else None
                ),
                "max_height": (
                    _int_text(resolution, "Height") if resolution is not None else None
                ),
                "encodings": [
                    item
                    for value in _all_text(element, "Encodings")
                    for item in re.split(r"[,\s]+", value)
                    if item
                ],
                "reboot": _bool_text(element, "Reboot"),
                "description": _text(element, "Description"),
            }
        )
    return rows


async def get_video_source_modes(
    services: list[dict],
    video_source_token: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> list[dict]:
    """List standard ONVIF video-source modes.

    Args:
        services: Stored service descriptors.
        video_source_token: Physical source token.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Video-source mode list.

    Raises:
        OnvifError: If Media source-mode support is unavailable.
    """
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    root = await _soap(
        media,
        f"{MEDIA_NS}/GetVideoSourceModes",
        (
            "<trt:GetVideoSourceModes>"
            f"<trt:VideoSourceToken>{_xml_text(video_source_token)}</trt:VideoSourceToken>"
            "</trt:GetVideoSourceModes>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return parse_video_source_modes(root)


async def set_video_source_mode(
    services: list[dict],
    video_source_token: str,
    mode_token: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Set one advertised ONVIF video-source mode and return readback.

    Args:
        services: Stored service descriptors.
        video_source_token: Physical source token.
        mode_token: Advertised source-mode token.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Dictionary containing reboot indication and readback mode list.

    Raises:
        OnvifError: If mode token is not advertised or write fails.
    """
    modes = await get_video_source_modes(
        services,
        video_source_token,
        username,
        password,
        tenant_id,
        site_id,
    )
    if not any(mode["token"] == mode_token for mode in modes):
        raise OnvifError(
            "VALUE_NOT_SUPPORTED",
            "Requested video-source mode is not advertised by the camera",
            422,
        )
    media = _service(services, "/ver10/media/wsdl", tenant_id, site_id)
    root = await _soap(
        media,
        f"{MEDIA_NS}/SetVideoSourceMode",
        (
            "<trt:SetVideoSourceMode>"
            f"<trt:VideoSourceToken>{_xml_text(video_source_token)}</trt:VideoSourceToken>"
            f"<trt:VideoSourceModeToken>{_xml_text(mode_token)}</trt:VideoSourceModeToken>"
            "</trt:SetVideoSourceMode>"
        ),
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return {
        "reboot": _bool_text(root, "Reboot"),
        "modes": await get_video_source_modes(
            services,
            video_source_token,
            username,
            password,
            tenant_id,
            site_id,
        ),
    }


def advertised_video_standards(modes: list[dict]) -> dict[str, str]:
    """Map explicit PAL/NTSC source-mode descriptions to mode tokens.

    Args:
        modes: Parsed ONVIF video-source modes.

    Returns:
        Standard-to-mode-token mapping only when exactly one advertised mode
        explicitly names PAL or NTSC in its description.
    """
    candidates: dict[str, list[str]] = {"PAL": [], "NTSC": []}
    for mode in modes:
        description = str(mode.get("description") or "")
        for standard in ("PAL", "NTSC"):
            if re.search(rf"\b{standard}\b", description, flags=re.IGNORECASE):
                candidates[standard].append(mode["token"])
    return {
        standard: tokens[0]
        for standard, tokens in candidates.items()
        if len(tokens) == 1
    }


async def get_video_standards(
    services: list[dict],
    video_source_token: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Return conservatively detected PAL/NTSC source-mode mappings.

    Args:
        services: Stored service descriptors.
        video_source_token: Physical source token.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Mapping with available standards and the full mode list.
    """
    modes = await get_video_source_modes(
        services,
        video_source_token,
        username,
        password,
        tenant_id,
        site_id,
    )
    return {"standards": advertised_video_standards(modes), "modes": modes}


async def set_video_standard(
    services: list[dict],
    video_source_token: str,
    standard: str,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Set PAL/NTSC only when exactly one advertised mode explicitly names it.

    Args:
        services: Stored service descriptors.
        video_source_token: Physical source token.
        standard: PAL or NTSC.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Camera tenant.
        site_id: Camera site.

    Returns:
        Source-mode write/readback response.

    Raises:
        OnvifError: If the device does not explicitly map the requested standard
            to exactly one source mode.
    """
    state = await get_video_standards(
        services,
        video_source_token,
        username,
        password,
        tenant_id,
        site_id,
    )
    mode_token = state["standards"].get(standard.upper())
    if not mode_token:
        raise _unsupported(
            "Camera did not explicitly advertise an unambiguous PAL/NTSC source mode"
        )
    result = await set_video_source_mode(
        services,
        video_source_token,
        mode_token,
        username,
        password,
        tenant_id,
        site_id,
    )
    result["standard"] = standard.upper()
    return result
