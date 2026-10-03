import ipaddress
import json
import socket
from urllib.parse import urlsplit

from app.core.config import settings

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class TargetNotAllowed(ValueError):
    """Raised when an ONVIF/RTSP target violates configured network policy."""


def _parse_networks(values: str | list[str]) -> list[Network]:
    """Parse configured CIDR values into network objects.

    Args:
        values: Comma-separated CIDRs or a list of CIDR strings.

    Returns:
        Parsed IP network objects.

    Raises:
        ValueError: If a configured CIDR is invalid.
    """
    raw_values = values.split(",") if isinstance(values, str) else values
    return [
        ipaddress.ip_network(str(value).strip(), strict=False)
        for value in raw_values
        if str(value).strip()
    ]


def _networks():
    return _parse_networks(settings.onvif_allowed_cidrs)


def _site_networks(tenant_id: str, site_id: str) -> list[Network]:
    """Return explicitly configured camera networks for one tenant/site.

    Args:
        tenant_id: Tenant owning the camera network.
        site_id: Site owning the camera network.

    Returns:
        Non-empty list of configured site camera networks.

    Raises:
        TargetNotAllowed: If the site mapping is absent, malformed or empty.
    """
    try:
        mapping = json.loads(settings.onvif_site_allowed_cidrs_json or "{}")
    except json.JSONDecodeError as exc:
        raise TargetNotAllowed("site camera network policy is invalid") from exc
    if not isinstance(mapping, dict):
        raise TargetNotAllowed("site camera network policy is invalid")
    values = mapping.get(f"{tenant_id}/{site_id}")
    if values is None:
        raise TargetNotAllowed("site camera network policy is not configured")
    if not isinstance(values, (str, list)):
        raise TargetNotAllowed("site camera network policy is invalid")
    try:
        networks = _parse_networks(values)
    except ValueError as exc:
        raise TargetNotAllowed("site camera network policy is invalid") from exc
    if not networks:
        raise TargetNotAllowed("site camera network policy is empty")
    return networks


def address_allowed(
    address: str,
    *,
    networks: list[Network] | None = None,
) -> bool:
    """Return whether one resolved IP address is allowed for camera access.

    Args:
        address: IPv4 or IPv6 address string.
        networks: Optional explicit CIDR policy. When omitted, use global policy.

    Returns:
        True when public hosts are enabled or the address belongs to an allowed CIDR.

    Raises:
        ValueError: If the address is not a valid IP literal.
    """
    ip = ipaddress.ip_address(address)
    if networks is None and settings.onvif_allow_public_hosts:
        return True
    effective_networks = networks if networks is not None else _networks()
    return any(ip in network for network in effective_networks)


def resolve_allowed_host(
    host: str,
    *,
    networks: list[Network] | None = None,
) -> list[str]:
    """Resolve a host and require every resolved address to satisfy policy.

    Args:
        host: DNS name or IPv4/IPv6 literal, including optional IPv6 brackets.
        networks: Optional explicit CIDR policy. When omitted, use global policy.

    Returns:
        Sorted resolved address strings.

    Raises:
        TargetNotAllowed: If resolution is empty or any address is outside policy.
        OSError: If DNS resolution fails.
    """
    # Bracketed IPv6 is accepted.
    clean = host.strip("[]")
    try:
        addresses = [str(ipaddress.ip_address(clean))]
    except ValueError:
        infos = socket.getaddrinfo(clean, None, type=socket.SOCK_STREAM)
        addresses = sorted({item[4][0] for item in infos})

    if not addresses:
        raise TargetNotAllowed("host did not resolve")
    denied = [
        addr
        for addr in addresses
        if not address_allowed(addr, networks=networks)
    ]
    if denied:
        raise TargetNotAllowed("target address is outside configured ONVIF camera networks")
    return addresses


def resolve_site_allowed_host(host: str, tenant_id: str, site_id: str) -> list[str]:
    """Resolve a host and enforce the exact tenant/site camera CIDR policy.

    Args:
        host: Camera DNS name or IP literal.
        tenant_id: Tenant owning the requested site.
        site_id: Site whose camera networks may be reached.

    Returns:
        Sorted allowed resolved addresses.

    Raises:
        TargetNotAllowed: If the site policy is absent or any address is outside it.
        OSError: If DNS resolution fails.
    """
    return resolve_allowed_host(
        host,
        networks=_site_networks(tenant_id, site_id),
    )


def resolve_site_pinned_host(host: str, tenant_id: str, site_id: str) -> str:
    """Resolve and pin one host inside an exact tenant/site camera network.

    Args:
        host: Camera DNS name or IP literal.
        tenant_id: Tenant owning the requested site.
        site_id: Site whose camera network may be reached.

    Returns:
        One site-approved IP address.

    Raises:
        TargetNotAllowed: If the host is outside policy or resolves ambiguously.
        OSError: If DNS resolution fails.
    """
    addresses = resolve_site_allowed_host(host, tenant_id, site_id)
    if len(addresses) != 1:
        raise TargetNotAllowed(
            "camera hostname must resolve to exactly one site-approved address"
        )
    return addresses[0]


def require_site_network_policy(tenant_id: str, site_id: str) -> None:
    """Require an explicit non-empty camera CIDR policy for one site.

    Args:
        tenant_id: Tenant owning the requested site.
        site_id: Site whose camera network is required.

    Returns:
        None when a valid non-empty policy exists.

    Raises:
        TargetNotAllowed: If the site camera-network policy is absent or invalid.
    """
    _site_networks(tenant_id, site_id)


def require_local_discovery_site(tenant_id: str, site_id: str) -> None:
    """Require the current process to be declared local to a discovery site.

    Args:
        tenant_id: Requested discovery tenant.
        site_id: Requested discovery site.

    Returns:
        None when the process is explicitly allowed for the site.

    Raises:
        TargetNotAllowed: If this process is not configured for that site's LAN.
    """
    configured = {
        value.strip()
        for value in settings.onvif_discovery_local_sites.split(",")
        if value.strip()
    }
    if "*" in configured or f"{tenant_id}/{site_id}" in configured:
        return
    raise TargetNotAllowed("ONVIF discovery is not local to the requested site")


def resolve_pinned_host(host: str) -> str:
    """Resolve and pin one camera host to a policy-approved IP address.

    Args:
        host: Camera DNS name or IP literal.

    Returns:
        One validated IP address suitable for downstream connection strings.

    Raises:
        TargetNotAllowed: If the target resolves outside policy or to multiple
            addresses, because downstream re-resolution could change destination.
        OSError: If DNS resolution fails.
    """
    addresses = resolve_allowed_host(host)
    if len(addresses) != 1:
        raise TargetNotAllowed(
            "camera hostname must resolve to exactly one allowed address"
        )
    return addresses[0]


def validate_camera_rtsp_target(host: str, port: int, path: str) -> tuple[str, int, str]:
    """Validate and pin manual RTSP camera connection fields.

    Args:
        host: Camera DNS name or IP literal.
        port: RTSP TCP port.
        path: RTSP path, optionally including a query string.

    Returns:
        Tuple of pinned IP, validated port and normalized path.

    Raises:
        TargetNotAllowed: If host, port or path is invalid or disallowed.
        OSError: If DNS resolution fails.
    """
    if not 1 <= port <= 65535:
        raise TargetNotAllowed("invalid RTSP port")
    if not path.startswith("/") or len(path) > 2048:
        raise TargetNotAllowed("invalid RTSP path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise TargetNotAllowed("invalid RTSP path")
    if "@" in path:
        raise TargetNotAllowed("credentials are not allowed in RTSP path")
    return resolve_pinned_host(host), port, path


def validate_site_camera_rtsp_target(
    host: str,
    port: int,
    path: str,
    tenant_id: str,
    site_id: str,
) -> tuple[str, int, str]:
    """Validate and pin manual RTSP fields inside one tenant/site network.

    Args:
        host: Camera DNS name or IP literal.
        port: RTSP TCP port.
        path: RTSP path, optionally including a query string.
        tenant_id: Tenant owning the requested site.
        site_id: Site whose camera network may be reached.

    Returns:
        Tuple of pinned site-approved IP, validated port and normalized path.

    Raises:
        TargetNotAllowed: If target fields or site network policy are invalid.
        OSError: If DNS resolution fails.
    """
    if not 1 <= port <= 65535:
        raise TargetNotAllowed("invalid RTSP port")
    if not path.startswith("/") or len(path) > 2048:
        raise TargetNotAllowed("invalid RTSP path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise TargetNotAllowed("invalid RTSP path")
    if "@" in path:
        raise TargetNotAllowed("credentials are not allowed in RTSP path")
    return resolve_site_pinned_host(host, tenant_id, site_id), port, path


def validate_site_http_xaddr(url: str, tenant_id: str, site_id: str) -> str:
    """Validate an ONVIF HTTP/HTTPS URL against an exact site policy.

    Args:
        url: Candidate ONVIF service URL.
        tenant_id: Tenant owning the requested site.
        site_id: Site whose camera network may be reached.

    Returns:
        Validated URL unchanged.

    Raises:
        TargetNotAllowed: If the URL or resolved host is not allowed for the site.
        OSError: If DNS resolution fails.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TargetNotAllowed("invalid ONVIF service URL")
    if parsed.username is not None or parsed.password is not None:
        raise TargetNotAllowed("credentials are not allowed in ONVIF service URL")
    resolve_site_allowed_host(parsed.hostname, tenant_id, site_id)
    return url


def validate_site_rtsp_uri(url: str, tenant_id: str, site_id: str) -> str:
    """Validate an RTSP/RTSPS URL against an exact site policy.

    Args:
        url: Candidate RTSP/RTSPS stream URI.
        tenant_id: Tenant owning the requested site.
        site_id: Site whose camera network may be reached.

    Returns:
        Validated URI unchanged.

    Raises:
        TargetNotAllowed: If the URI or resolved host is not allowed for the site.
        OSError: If DNS resolution fails.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise TargetNotAllowed("invalid RTSP stream URI")
    resolve_site_allowed_host(parsed.hostname, tenant_id, site_id)
    return url


def pin_site_http_xaddr(url: str, tenant_id: str, site_id: str) -> str:
    """Pin an ONVIF service hostname to its one site-approved IP.

    Args:
        url: Candidate ONVIF service URL.
        tenant_id: Tenant owning the requested site.
        site_id: Site whose camera network may be reached.

    Returns:
        URL with the hostname replaced by one validated site-approved IP.

    Raises:
        TargetNotAllowed: If the target is invalid, unconfigured or ambiguous.
        OSError: If DNS resolution fails.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TargetNotAllowed("invalid ONVIF service URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise TargetNotAllowed("credentials are not allowed in ONVIF service URL")
    host = resolve_site_pinned_host(parsed.hostname, tenant_id, site_id)
    if ":" in host:
        host = f"[{host}]"
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    port = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"{userinfo}{host}{port}").geturl()


def pin_site_rtsp_uri(url: str, tenant_id: str, site_id: str) -> str:
    """Pin an RTSP stream hostname to its one site-approved IP.

    Args:
        url: Candidate RTSP/RTSPS stream URI.
        tenant_id: Tenant owning the requested site.
        site_id: Site whose camera network may be reached.

    Returns:
        URI with the hostname replaced by one validated site-approved IP.

    Raises:
        TargetNotAllowed: If the target is invalid, unconfigured or ambiguous.
        OSError: If DNS resolution fails.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise TargetNotAllowed("invalid RTSP stream URI")
    if parsed.username is not None or parsed.password is not None:
        raise TargetNotAllowed("credentials are not allowed in RTSP stream URI")
    host = resolve_site_pinned_host(parsed.hostname, tenant_id, site_id)
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"{host}{port}").geturl()


def validate_http_xaddr(url: str) -> str:
    """Validate an ONVIF HTTP/HTTPS service URL against network policy.

    Args:
        url: Candidate ONVIF service URL.

    Returns:
        The validated URL unchanged.

    Raises:
        TargetNotAllowed: If the URL shape or resolved host is not permitted.
        OSError: If DNS resolution fails.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TargetNotAllowed("invalid ONVIF service URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise TargetNotAllowed("credentials are not allowed in ONVIF service URL")
    resolve_allowed_host(parsed.hostname)
    return url


def validate_rtsp_uri(url: str) -> str:
    """Validate an RTSP/RTSPS stream URL against network policy.

    Args:
        url: Candidate camera stream URI.

    Returns:
        The validated URI unchanged.

    Raises:
        TargetNotAllowed: If the URI shape or resolved host is not permitted.
        OSError: If DNS resolution fails.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise TargetNotAllowed("invalid RTSP stream URI")
    resolve_allowed_host(parsed.hostname)
    return url
