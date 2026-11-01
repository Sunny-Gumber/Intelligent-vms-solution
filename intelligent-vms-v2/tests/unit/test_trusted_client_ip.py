"""Forwarding headers are interpreted only by the trusted server boundary."""
from __future__ import annotations

import pytest
from app.core.deps import client_ip
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


@pytest.mark.parametrize("peer,header,expected", [
    ("198.51.100.9", "1.2.3.4", "198.51.100.9"),
    ("127.0.0.1", "198.51.100.9", "198.51.100.9"),
    ("127.0.0.1", "spoof, 198.51.100.9, 127.0.0.1", "198.51.100.9"),
    ("::1", "2001:db8::1", "2001:db8::1"),
    ("198.51.100.9", "malformed", "198.51.100.9"),
    ("198.51.100.9", "", "198.51.100.9"),
])
async def test_canonical_proxy_address(peer, header, expected):
    observed = []
    async def app(scope, receive, send):
        observed.append(client_ip(Request(scope)))
    scope = {"type": "http", "client": (peer, 1234), "scheme": "http",
             "headers": [(b"x-forwarded-for", header.encode())]}
    middleware = ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1", "::1"])
    await middleware(scope, None, None)
    assert observed == [expected]
