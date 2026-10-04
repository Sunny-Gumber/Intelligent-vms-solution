"""Server-side ONVIF PTZ foundation.

The desktop client never receives camera credentials or ONVIF endpoints.  All
device calls remain inside the control plane and are constrained by the camera
site network policy.
"""

import math
from xml.sax.saxutils import escape

from app.services.network_policy import pin_site_http_xaddr
from app.services.onvif_client import OnvifError, _local, _service_xaddr, _soap

PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"


def _unsupported(message: str) -> OnvifError:
    return OnvifError("UNSUPPORTED_CAPABILITY", message, 422)


def _service(services: list[dict], tenant_id: str, site_id: str) -> str:
    xaddr = _service_xaddr(services, "/ptz/wsdl")
    if not xaddr:
        raise _unsupported("Camera did not advertise ONVIF PTZ service")
    return pin_site_http_xaddr(xaddr, tenant_id, site_id)


def _finite_unit(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value < -1.0 or value > 1.0:
        raise OnvifError("INVALID_PTZ_VECTOR", f"{name} must be between -1 and 1", 422)
    return value


async def capabilities(
    services: list[dict],
    profile_token: str | None,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> dict:
    """Discover continuous pan/tilt and zoom support from the camera PTZ node."""
    if not profile_token:
        raise _unsupported("Camera has no managed ONVIF profile for PTZ")
    xaddr = _service(services, tenant_id, site_id)
    root = await _soap(
        xaddr,
        f"{PTZ_NS}/GetNodes",
        f'<GetNodes xmlns="{PTZ_NS}"/>',
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    locals_ = {_local(element.tag) for element in root.iter()}
    return {
        "ptz": True,
        "pan_tilt": "ContinuousPanTiltVelocitySpace" in locals_,
        "zoom": "ContinuousZoomVelocitySpace" in locals_,
        "presets": False,
        "profile_token_present": True,
    }


async def continuous_move(
    services: list[dict],
    profile_token: str | None,
    pan: float,
    tilt: float,
    zoom: float,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> None:
    """Issue one bounded ONVIF ContinuousMove after live capability verification."""
    if not profile_token:
        raise _unsupported("Camera has no managed ONVIF profile for PTZ")
    pan = _finite_unit("pan", pan)
    tilt = _finite_unit("tilt", tilt)
    zoom = _finite_unit("zoom", zoom)
    if pan == 0.0 and tilt == 0.0 and zoom == 0.0:
        raise OnvifError("INVALID_PTZ_VECTOR", "PTZ move vector must not be zero", 422)

    discovered = await capabilities(
        services, profile_token, username, password, tenant_id, site_id
    )
    if (pan != 0.0 or tilt != 0.0) and not discovered["pan_tilt"]:
        raise _unsupported("Camera does not advertise continuous pan/tilt velocity")
    if zoom != 0.0 and not discovered["zoom"]:
        raise _unsupported("Camera does not advertise continuous zoom velocity")

    velocity = ""
    if pan != 0.0 or tilt != 0.0:
        velocity += f'<tt:PanTilt x="{pan:.6g}" y="{tilt:.6g}"/>'
    if zoom != 0.0:
        velocity += f'<tt:Zoom x="{zoom:.6g}"/>'
    body = (
        f'<ContinuousMove xmlns="{PTZ_NS}">'
        f"<ProfileToken>{escape(profile_token)}</ProfileToken>"
        f"<Velocity>{velocity}</Velocity>"
        "</ContinuousMove>"
    )
    await _soap(
        _service(services, tenant_id, site_id),
        f"{PTZ_NS}/ContinuousMove",
        body,
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )


async def stop(
    services: list[dict],
    profile_token: str | None,
    username: str | None,
    password: str | None,
    tenant_id: str,
    site_id: str,
) -> None:
    """Stop both continuous pan/tilt and zoom for one managed ONVIF profile."""
    if not profile_token:
        raise _unsupported("Camera has no managed ONVIF profile for PTZ")
    body = (
        f'<Stop xmlns="{PTZ_NS}">'
        f"<ProfileToken>{escape(profile_token)}</ProfileToken>"
        "<PanTilt>true</PanTilt><Zoom>true</Zoom>"
        "</Stop>"
    )
    await _soap(
        _service(services, tenant_id, site_id),
        f"{PTZ_NS}/Stop",
        body,
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
