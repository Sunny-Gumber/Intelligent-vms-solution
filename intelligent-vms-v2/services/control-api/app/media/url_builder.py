"""Safe construction and sanitisation of media URLs.

Two hard rules, both enforced here and verified by tests:

1. Credentials are percent-encoded. A password containing `@`, `:` or `/`
   (very common on customer sites) otherwise produces a malformed URL that
   silently connects to the wrong host.

2. Any URL leaving this process towards a client passes through
   sanitize_for_display(), which strips userinfo. Clients receive a gateway
   URL and a short-lived token, never the camera URL.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import quote, urlparse, urlunparse

SAFE_PATH_CHARS = "/?&=:,.~_-%"


def _host_literal(host: str) -> str:
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]"
    except ValueError:
        pass
    return host


def build_rtsp_url(*, host: str, port: int = 554, path: str = "/",
                   username: str | None = None, password: str | None = None,
                   use_tls: bool = False) -> str:
    scheme = "rtsps" if use_tls else "rtsp"
    userinfo = ""
    if username:
        userinfo = quote(username, safe="")
        if password:
            userinfo += ":" + quote(password, safe="")
        userinfo += "@"
    if not path.startswith("/"):
        path = "/" + path
    return (f"{scheme}://{userinfo}{_host_literal(host)}:{int(port)}"
            f"{quote(path, safe=SAFE_PATH_CHARS)}")


def with_credentials(url: str, username: str | None, password: str | None) -> str:
    """Inject credentials into a URL that lacks them (ONVIF returns bare URLs)."""
    if not username:
        return url
    parsed = urlparse(url)
    if parsed.username:
        return url
    userinfo = quote(username, safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    netloc = f"{userinfo}@{_host_literal(parsed.hostname or '')}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def sanitize_for_display(url: str | None) -> str | None:
    """Remove userinfo so a URL is safe in a UI, log or support bundle."""
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.hostname:
        return url
    netloc = _host_literal(parsed.hostname)
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def contains_credentials(url: str | None) -> bool:
    return bool(urlparse(url).username) if url else False


def replace_host(url: str, new_host: str, new_port: int | None = None) -> str:
    """Rewrite the host, preserving credentials — used for DNS pinning."""
    parsed = urlparse(url)
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    port = new_port or parsed.port
    netloc = f"{userinfo}{_host_literal(new_host)}"
    if port:
        netloc += f":{port}"
    return urlunparse(parsed._replace(netloc=netloc))


def gateway_path(camera_id: str, stream_role: str) -> str:
    """Stable media-gateway path: no credentials, no customer-identifying name."""
    return f"cam_{camera_id}_{stream_role}"
