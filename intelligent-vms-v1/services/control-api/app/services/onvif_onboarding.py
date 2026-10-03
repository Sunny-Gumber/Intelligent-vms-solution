import base64
import binascii
import json
from urllib.parse import urlsplit

from app.services.onvif_client import OnvifError


QR_PREFIX = "vms-onvif:v1:"
_ALLOWED_KEYS = {
    "tenant_id",
    "site_id",
    "name",
    "host",
    "port",
    "scheme",
    "device_service_path",
    "main_profile_token",
    "sub_profile_token",
    "third_profile_token",
}


def decode_qr_payload(value: str) -> dict:
    """Decode one credential-free versioned VMS ONVIF QR payload.

    Args:
        value: Versioned QR text containing base64url-encoded JSON.

    Returns:
        Validated onboarding field dictionary with no credential fields.

    Raises:
        OnvifError: If version, encoding, JSON shape, size or field policy is invalid.
    """
    if not value.startswith(QR_PREFIX):
        raise OnvifError("INVALID_QR_PAYLOAD", "QR payload version is not supported", 422)
    encoded = value[len(QR_PREFIX):]
    if not encoded or len(encoded) > 4096:
        raise OnvifError("INVALID_QR_PAYLOAD", "QR payload is invalid", 422)
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            (encoded + padding).encode("ascii"), altchars=b"-_", validate=True
        )
        if len(raw) > 3072:
            raise ValueError("payload too large")
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise OnvifError("INVALID_QR_PAYLOAD", "QR payload is invalid", 422) from exc
    if not isinstance(parsed, dict):
        raise OnvifError("INVALID_QR_PAYLOAD", "QR payload is invalid", 422)
    if any(key not in _ALLOWED_KEYS for key in parsed):
        raise OnvifError("INVALID_QR_PAYLOAD", "QR payload contains unsupported fields", 422)
    forbidden = {"username", "password", "token", "secret", "credential"}
    if any(str(key).lower() in forbidden for key in parsed):
        raise OnvifError("INVALID_QR_PAYLOAD", "QR payload must not contain credentials", 422)
    return parsed


def connection_from_xaddr(xaddr: str) -> dict:
    """Convert a validated ONVIF device-service XAddr into connection fields.

    Args:
        xaddr: HTTP/HTTPS ONVIF device-service endpoint.

    Returns:
        Host, port, scheme and device-service path without query/fragment data.

    Raises:
        OnvifError: If the endpoint cannot be represented safely.
    """
    try:
        parsed = urlsplit(xaddr)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid scheme or host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise OnvifError(
            "DEVICE_SERVICE_INVALID",
            "Camera returned an invalid ONVIF device-service endpoint",
            502,
        ) from exc
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise OnvifError(
            "DEVICE_SERVICE_INVALID",
            "Camera returned an invalid ONVIF device-service endpoint",
            502,
        )
    path = parsed.path or "/onvif/device_service"
    return {
        "host": parsed.hostname,
        "port": port,
        "scheme": parsed.scheme,
        "device_service_path": path,
    }
