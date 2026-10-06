import base64
import hashlib
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx
from defusedxml import ElementTree as DET

from app.core.config import settings
from app.services.rtsp import build_rtsp_uri, validate_source_path
from app.services.network_policy import (
    pin_site_http_xaddr,
    pin_site_rtsp_uri,
    validate_http_xaddr,
    validate_rtsp_uri,
    validate_site_http_xaddr,
    validate_site_rtsp_uri,
)


SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
IMAGING_NS = "http://www.onvif.org/ver20/imaging/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
EVENT_NS = "http://www.onvif.org/ver10/events/wsdl"
WSA_NS = "http://www.w3.org/2005/08/addressing"
WSNT_NS = "http://docs.oasis-open.org/wsn/b-2"


class OnvifError(RuntimeError):
    """Represent a bounded ONVIF/device failure safe for API translation."""

    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(root, local_name: str) -> str | None:
    for el in root.iter():
        if _local(el.tag) == local_name and el.text is not None:
            return el.text.strip()
    return None


def _child_text(root, local_name: str) -> str | None:
    for el in list(root):
        if _local(el.tag) == local_name and el.text is not None:
            return el.text.strip()
    return None


def _wsse(username: str | None, password: str | None) -> str:
    if not username:
        return ""
    raw_nonce = os.urandom(20)
    created = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    digest = hashlib.sha1(raw_nonce + created.encode("utf-8") + (password or "").encode("utf-8")).digest()
    return f"""
    <wsse:Security s:mustUnderstand="1"
      xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
      xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{escape(username)}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{base64.b64encode(digest).decode()}</wsse:Password>
        <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(raw_nonce).decode()}</wsse:Nonce>
        <wsu:Created>{created}</wsu:Created>
      </wsse:UsernameToken>
    </wsse:Security>"""


def _envelope(
    body: str,
    username: str | None,
    password: str | None,
    extra_header_xml: str = "",
) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" xmlns:tds="{DEVICE_NS}" xmlns:trt="{MEDIA_NS}" xmlns:tr2="{MEDIA2_NS}" xmlns:timg="{IMAGING_NS}" xmlns:tt="{SCHEMA_NS}" xmlns:tev="{EVENT_NS}" xmlns:wsa="{WSA_NS}" xmlns:wsnt="{WSNT_NS}">
  <s:Header>{_wsse(username, password)}{extra_header_xml}</s:Header>
  <s:Body>{body}</s:Body>
</s:Envelope>""".encode("utf-8")


def _safe_fault_message(root) -> str:
    """Return a public-safe SOAP fault message without device-provided text."""
    del root
    return "Camera returned an ONVIF SOAP fault"


async def _soap(
    xaddr: str,
    action: str,
    body: str,
    username: str | None,
    password: str | None,
    *,
    operation_timeout_seconds: float | None = None,
    extra_header_xml: str = "",
    tenant_id: str | None = None,
    site_id: str | None = None,
):
    if tenant_id is not None and site_id is not None:
        validate_site_http_xaddr(xaddr, tenant_id, site_id)
    else:
        validate_http_xaddr(xaddr)
    timeout = httpx.Timeout(
        operation_timeout_seconds or settings.onvif_operation_timeout_seconds,
        connect=settings.onvif_connect_timeout_seconds,
    )
    auth = httpx.DigestAuth(username, password or "") if username else None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, auth=auth) as client:
            response = await client.post(
                xaddr,
                content=_envelope(body, username, password, extra_header_xml),
                headers={
                    "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
                    "Accept": "application/soap+xml, text/xml",
                },
            )
    except httpx.TimeoutException as exc:
        raise OnvifError("NETWORK_UNREACHABLE", "ONVIF operation timed out", 504) from exc
    except httpx.HTTPError as exc:
        raise OnvifError("NETWORK_UNREACHABLE", "ONVIF device could not be reached", 502) from exc

    if response.status_code in {401, 403}:
        raise OnvifError("AUTH_FAILED", "Camera rejected ONVIF credentials", 401)
    if len(response.content) > settings.onvif_max_response_bytes:
        raise OnvifError("DEVICE_SERVICE_INVALID", "ONVIF response exceeded configured size limit")
    try:
        root = DET.fromstring(response.content)
    except Exception as exc:
        raise OnvifError("DEVICE_SERVICE_INVALID", "Camera returned invalid ONVIF XML") from exc

    if response.status_code >= 400 or any(_local(el.tag) == "Fault" for el in root.iter()):
        raise OnvifError("SOAP_FAULT", _safe_fault_message(root), 502)
    return root


def parse_device_info(root) -> dict:
    """Extract standard ONVIF device-information fields from SOAP XML.

    Args:
        root: Parsed ONVIF XML root.

    Returns:
        Dictionary containing manufacturer, model, firmware, serial and hardware ID.
    """
    wanted = ["Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId"]
    return {name: _first_text(root, name) for name in wanted}


def parse_services(root) -> list[dict]:
    """Extract advertised ONVIF service namespaces and endpoint URLs.

    Args:
        root: Parsed GetServices SOAP XML root.

    Returns:
        Service dictionaries containing namespace and xaddr values.
    """
    services = []
    for el in root.iter():
        if _local(el.tag) != "Service":
            continue
        namespace = _child_text(el, "Namespace")
        xaddr = _child_text(el, "XAddr")
        if namespace and xaddr:
            services.append({"namespace": namespace, "xaddr": xaddr})
    return services


def features_from_services(services: list[dict], capabilities_root=None) -> dict:
    """Infer vendor-neutral ONVIF feature flags from services/capabilities.

    Args:
        services: Parsed ONVIF service descriptors.
        capabilities_root: Optional parsed GetCapabilities XML root.

    Returns:
        Boolean capability map for media, PTZ, events, imaging, analytics and
        recording/search/replay services.
    """
    namespaces = " ".join(item.get("namespace", "").lower() for item in services)
    result = {
        "media": "/media/" in namespaces or "/media/wsdl" in namespaces,
        "media2": "/media2/" in namespaces or "/media2/wsdl" in namespaces,
        "ptz": "/ptz/" in namespaces or "/ptz/wsdl" in namespaces,
        "events": "/events/" in namespaces or "/events/wsdl" in namespaces,
        "imaging": "/imaging/" in namespaces or "/imaging/wsdl" in namespaces,
        "analytics": "/analytics/" in namespaces or "/analytics/wsdl" in namespaces,
        "recording": "/recording/" in namespaces,
        "search": "/search/" in namespaces,
        "replay": "/replay/" in namespaces,
    }
    if capabilities_root is not None:
        for el in capabilities_root.iter():
            name = _local(el.tag).lower()
            if name in {"media", "ptz", "events", "imaging", "analytics"}:
                result[name] = True
    return result


def parse_profiles(root) -> list[dict]:
    """Parse bounded ONVIF media profile metadata.

    Args:
        root: Parsed GetProfiles SOAP XML root.

    Returns:
        Profile dictionaries with token, codec, resolution, FPS, bitrate and
        placeholders for stream URIs.

    Raises:
        ValueError: If advertised numeric profile fields cannot be converted.
    """
    profiles = []
    for el in root.iter():
        if _local(el.tag) != "Profiles":
            continue
        token = el.attrib.get("token") or el.attrib.get("Token")
        if not token:
            continue
        video = next((x for x in el.iter() if _local(x.tag) == "VideoEncoderConfiguration"), None)
        source = next((x for x in el.iter() if _local(x.tag) == "VideoSourceConfiguration"), None)
        resolution = next((x for x in (video.iter() if video is not None else []) if _local(x.tag) == "Resolution"), None)
        rate = next((x for x in (video.iter() if video is not None else []) if _local(x.tag) == "RateControl"), None)
        width = int(_child_text(resolution, "Width")) if resolution is not None and _child_text(resolution, "Width") else None
        height = int(_child_text(resolution, "Height")) if resolution is not None and _child_text(resolution, "Height") else None
        fps_text = _child_text(rate, "FrameRateLimit") if rate is not None else None
        bitrate_text = _child_text(rate, "BitrateLimit") if rate is not None else None
        profiles.append(
            {
                "token": token,
                "name": _child_text(el, "Name"),
                "encoding": _child_text(video, "Encoding") if video is not None else None,
                "width": width,
                "height": height,
                "fps": float(fps_text) if fps_text else None,
                "bitrate_kbps": int(float(bitrate_text)) if bitrate_text else None,
                "video_encoder_configuration_token": (
                    video.attrib.get("token") if video is not None else None
                ),
                "video_source_configuration_token": (
                    source.attrib.get("token") if source is not None else None
                ),
                "video_source_token": (
                    _child_text(source, "SourceToken") if source is not None else None
                ),
                "stream_uri": None,
                "_raw_stream_uri": None,
            }
        )
    return profiles


def select_main_sub(profiles: list[dict]) -> tuple[str | None, str | None]:
    """Select main/sub profile tokens by resolution, bitrate and frame rate.

    Args:
        profiles: Parsed ONVIF media profile dictionaries.

    Returns:
        Tuple of main-profile token and optional sub-profile token.
    """
    if not profiles:
        return None, None

    def score(p):
        pixels = (p.get("width") or 0) * (p.get("height") or 0)
        return pixels, p.get("bitrate_kbps") or 0, p.get("fps") or 0

    ordered = sorted(profiles, key=score)
    main = ordered[-1]["token"]
    sub = ordered[0]["token"] if len(ordered) > 1 and ordered[0]["token"] != main else None
    return main, sub


def sanitize_rtsp_uri(uri: str | None) -> str | None:
    """Remove credentials, query and fragment data from an RTSP URI.

    Args:
        uri: Optional RTSP/RTSPS URI returned by a camera.

    Returns:
        Sanitized URI containing only scheme, host, optional port and path, or
        None when no URI is supplied.

    Raises:
        ValueError: If URL parsing encounters an invalid port.
    """
    if not uri:
        return None
    p = urlsplit(uri)
    host = p.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{p.port}" if p.port else ""
    return urlunsplit((p.scheme, f"{host}{port}", p.path, "", ""))


def sanitize_http_uri(uri: str | None) -> str | None:
    """Remove credentials, query and fragment data from an HTTP service URI.

    Args:
        uri: Optional HTTP/HTTPS URI returned by a camera.

    Returns:
        Sanitized URI containing scheme, host, optional port and path only.
    """
    if not uri:
        return None
    parsed = urlsplit(uri)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def inject_rtsp_credentials(uri: str, username: str | None, password: str | None) -> str:
    """Inject percent-encoded camera credentials into a validated RTSP URI.

    Args:
        uri: Camera RTSP/RTSPS URI.
        username: Optional camera username.
        password: Optional camera password.

    Returns:
        RTSP URI with encoded credentials when a username is supplied.

    Raises:
        TargetNotAllowed: If the URI target violates network policy.
        ValueError: If URL parsing encounters an invalid port.
    """
    validate_rtsp_uri(uri)
    p = urlsplit(uri)
    host = p.hostname or ""
    path = (p.path or "/") + ("?" + p.query if p.query else "")
    return build_rtsp_uri(host, p.port or (322 if p.scheme == "rtsps" else 554), path,
                          username, password, source_protocol=p.scheme)


def stream_parts(uri: str) -> tuple[str, int, str]:
    """Split a validated RTSP URI into host, port and path/query.

    Args:
        uri: Camera RTSP/RTSPS URI.

    Returns:
        Tuple containing hostname, explicit/default port and stream path/query.

    Raises:
        TargetNotAllowed: If the target violates network policy.
        ValueError: If URL parsing encounters an invalid port.
    """
    validate_rtsp_uri(uri)
    p = urlsplit(uri)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    validate_source_path(path)
    default_port = 322 if p.scheme == "rtsps" else 554
    return p.hostname or "", p.port or default_port, path


def _service_xaddr(services: list[dict], fragment: str) -> str | None:
    fragment = fragment.lower()
    for item in services:
        ns = item.get("namespace", "").lower()
        if fragment in ns:
            return item.get("xaddr")
    return None


async def identify_xaddr(
    xaddr: str,
    username: str | None,
    password: str | None,
    *,
    tenant_id: str | None = None,
    site_id: str | None = None,
    operation_timeout_seconds: float | None = None,
) -> dict:
    """Read bounded ONVIF identity fields without enumerating media profiles.

    Args:
        xaddr: ONVIF device-service endpoint.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Optional tenant for exact site-network enforcement.
        site_id: Optional site for exact camera-network enforcement.
        operation_timeout_seconds: Optional timeout for the identity operation.

    Returns:
        Pinned device-service address and parsed device-information fields.

    Raises:
        OnvifError: If authentication, network or XML handling fails.
        TargetNotAllowed: If the target violates camera-network policy.
    """
    if tenant_id is not None and site_id is not None:
        xaddr = pin_site_http_xaddr(xaddr, tenant_id, site_id)
    else:
        validate_http_xaddr(xaddr)
    root = await _soap(
        xaddr,
        f"{DEVICE_NS}/GetDeviceInformation",
        "<tds:GetDeviceInformation/>",
        username,
        password,
        operation_timeout_seconds=operation_timeout_seconds,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return {"xaddr": xaddr, "device_info": parse_device_info(root)}


async def probe_xaddr(
    xaddr: str,
    username: str | None,
    password: str | None,
    *,
    tenant_id: str | None = None,
    site_id: str | None = None,
) -> dict:
    """Probe one ONVIF device-service endpoint and enumerate usable streams.

    Args:
        xaddr: ONVIF device-service HTTP/HTTPS endpoint.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Optional tenant used for exact site-network enforcement.
        site_id: Optional site used for exact camera CIDR enforcement.

    Returns:
        Probe dictionary containing device info, services, features, profiles and
        recommended main/sub profile tokens.

    Raises:
        OnvifError: If device/media services, authentication, XML or stream URI
            retrieval fails.
        TargetNotAllowed: If camera-advertised service/stream targets violate
            network policy.
    """
    if tenant_id is not None and site_id is not None:
        xaddr = pin_site_http_xaddr(xaddr, tenant_id, site_id)
    else:
        validate_http_xaddr(xaddr)
    device_info_root = await _soap(
        xaddr,
        f"{DEVICE_NS}/GetDeviceInformation",
        "<tds:GetDeviceInformation/>",
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    services_root = await _soap(
        xaddr,
        f"{DEVICE_NS}/GetServices",
        "<tds:GetServices><tds:IncludeCapability>true</tds:IncludeCapability></tds:GetServices>",
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    capabilities_root = await _soap(
        xaddr,
        f"{DEVICE_NS}/GetCapabilities",
        "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )

    services = parse_services(services_root)
    if tenant_id is not None and site_id is not None:
        services = [
            {
                **item,
                "xaddr": pin_site_http_xaddr(
                    item["xaddr"],
                    tenant_id,
                    site_id,
                ),
            }
            for item in services
        ]
    else:
        for item in services:
            validate_http_xaddr(item["xaddr"])
    features = features_from_services(services, capabilities_root)
    media_xaddr = _service_xaddr(services, "/ver10/media/wsdl")
    if not media_xaddr:
        # Older devices often return Media/XAddr in GetCapabilities.
        for el in capabilities_root.iter():
            if _local(el.tag) == "Media":
                media_xaddr = _first_text(el, "XAddr")
                if media_xaddr:
                    break
    if not media_xaddr:
        raise OnvifError("MEDIA_SERVICE_UNAVAILABLE", "Camera did not advertise ONVIF Media service")
    if tenant_id is not None and site_id is not None:
        media_xaddr = pin_site_http_xaddr(media_xaddr, tenant_id, site_id)
    else:
        validate_http_xaddr(media_xaddr)

    profiles_root = await _soap(
        media_xaddr,
        f"{MEDIA_NS}/GetProfiles",
        "<trt:GetProfiles/>",
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )
    profiles = parse_profiles(profiles_root)
    if not profiles:
        raise OnvifError("NO_MEDIA_PROFILES", "Camera returned no usable ONVIF media profiles")

    for profile in profiles[:32]:
        token = escape(profile["token"])
        root = await _soap(
            media_xaddr,
            f"{MEDIA_NS}/GetStreamUri",
            (
                "<trt:GetStreamUri>"
                "<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
                "</trt:StreamSetup>"
                f"<trt:ProfileToken>{token}</trt:ProfileToken>"
                "</trt:GetStreamUri>"
            ),
            username,
            password,
            tenant_id=tenant_id,
            site_id=site_id,
        )
        uri = _first_text(root, "Uri")
        if uri:
            if tenant_id is not None and site_id is not None:
                uri = pin_site_rtsp_uri(uri, tenant_id, site_id)
                validate_site_rtsp_uri(uri, tenant_id, site_id)
            else:
                validate_rtsp_uri(uri)
            profile["_raw_stream_uri"] = uri
            profile["stream_uri"] = sanitize_rtsp_uri(uri)

    if not any(p.get("_raw_stream_uri") for p in profiles):
        raise OnvifError("NO_STREAM_URI", "Camera returned profiles but no RTSP stream URI")

    main_token, sub_token = select_main_sub([p for p in profiles if p.get("_raw_stream_uri")])
    return {
        "xaddr": xaddr,
        "device_info": parse_device_info(device_info_root),
        "services": services,
        "features": features,
        "profiles": profiles,
        "recommended_main_profile_token": main_token,
        "recommended_sub_profile_token": sub_token,
    }


async def probe_host(
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    device_service_path: str = "/onvif/device_service",
    scheme: str = "http",
    *,
    tenant_id: str | None = None,
    site_id: str | None = None,
) -> dict:
    """Build a device-service URL from host settings and run an ONVIF probe.

    Args:
        host: Camera hostname or IP address.
        port: ONVIF HTTP/HTTPS port.
        username: Optional camera username.
        password: Optional camera password.
        device_service_path: ONVIF device-service path.
        scheme: HTTP scheme, normally http or https.
        tenant_id: Optional tenant used for exact site-network enforcement.
        site_id: Optional site used for exact camera CIDR enforcement.

    Returns:
        Full probe result from :func:`probe_xaddr`.

    Raises:
        OnvifError: If the ONVIF probe fails.
        TargetNotAllowed: If the generated/advertised target violates policy.
    """
    if (
        device_service_path.startswith("//")
        or "?" in device_service_path
        or "#" in device_service_path
        or "\\" in device_service_path
    ):
        raise OnvifError(
            "DEVICE_SERVICE_INVALID",
            "ONVIF device-service path is invalid",
            422,
        )
    if not device_service_path.startswith("/"):
        device_service_path = "/" + device_service_path
    xaddr = f"{scheme}://{host}:{port}{device_service_path}"
    return await probe_xaddr(
        xaddr,
        username,
        password,
        tenant_id=tenant_id,
        site_id=site_id,
    )


def public_probe(probe: dict) -> dict:
    """Remove internal raw stream URIs before returning ONVIF probe data publicly.

    Args:
        probe: Internal ONVIF probe dictionary.

    Returns:
        Shallow public copy whose profiles omit underscore-prefixed internal fields.
    """
    public = dict(probe)
    public["xaddr"] = sanitize_http_uri(probe.get("xaddr"))
    public["services"] = [
        {
            "namespace": item.get("namespace"),
            "xaddr": sanitize_http_uri(item.get("xaddr")),
        }
        for item in probe.get("services", [])
    ]
    public["profiles"] = [
        {k: v for k, v in profile.items() if not k.startswith("_")}
        for profile in probe.get("profiles", [])
    ]
    return public
