import ipaddress
import re
from urllib.parse import parse_qsl, quote


def source_trust_options(camera: object) -> dict[str, str]:
    """Return optional approved TLS trust for internal media provisioning.

    Args:
        camera: Persisted source configuration; legacy transient objects may omit trust.

    Returns:
        Optional MediaMTX keyword arguments; absence uses normal certificate validation.
    """
    fingerprint = getattr(camera, "source_fingerprint", None)
    return {"source_fingerprint": fingerprint} if fingerprint else {}


def validate_source_path(path: str) -> None:
    """Reject ambiguous URI components and credential query fields without echoing input.

    Args:
        path: Camera stream path with optional vendor query fields.

    Raises:
        ValueError: If the path contains unsafe URI syntax or credential parameters.
    """
    if not path.startswith("/") or len(path) > 2048:
        raise ValueError("invalid camera source path")
    if any(ord(c) <= 32 or ord(c) == 127 for c in path) or any(c in path for c in "@#\\"):
        raise ValueError("invalid camera source path")
    if "://" in path:
        raise ValueError("camera source path must not contain a URL")
    secret_keys = {"user", "username", "password", "passwd", "pwd", "pass", "token", "access_token",
                   "authorization", "credential", "secret", "api_key", "apikey"}
    if any(key.casefold() in secret_keys for key, _ in parse_qsl(path.partition("?")[2])):
        raise ValueError("credentials are not allowed in camera source query")


def build_rtsp_uri(
    host: str, port: int, path: str, username: str | None, password: str | None,
    *, source_protocol: str = "rtsp",
) -> str:
    """Build an internal RTSP/RTSPS URI from already network-pinned camera fields.

    Args:
        host: Camera hostname or IP address.
        port: RTSP port.
        path: Camera stream path, including any query string.
        username: Optional camera username.
        password: Optional camera password.
        source_protocol: Validated persisted protocol, defaulting to legacy RTSP.

    Returns:
        Credential-bearing URI for internal media-node use only.

    Raises:
        ValueError: If protocol, host, port or path has unsafe syntax. Inputs are not echoed.
    """
    if source_protocol not in {"rtsp", "rtsps"}:
        raise ValueError("unsupported camera source protocol")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("invalid camera source port")
    validate_source_path(path)
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?", host):
            raise ValueError("invalid camera source host") from None
    else:
        host = f"[{address}]" if address.version == 6 else str(address)
    auth = ""
    if username is not None:
        user = quote(username, safe="")
        pwd = quote(password or "", safe="")
        auth = f"{user}:{pwd}@"
    return f"{source_protocol}://{auth}{host}:{port}{path}"
