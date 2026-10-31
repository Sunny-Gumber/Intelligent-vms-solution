"""Outbound HTTP with mandatory timeouts and SSRF guarding.

Module is named `outbound` (not `http`) so it can never shadow the standard
library `http` package.

No code in this product may construct a bare httpx client. Every outbound call
goes through here so that a timeout is always set (an un-timed call to a hung
camera would pin a worker thread forever), destinations are validated before
connection, redirects are disabled (a camera must not redirect us to
localhost), and responses are size-capped.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.netguard import GuardPolicy, NetworkGuard, NetworkGuardError  # noqa: F401

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class OutboundHttp:
    def __init__(self, guard: NetworkGuard | None = None, *,
                 timeout: httpx.Timeout | None = None,
                 max_bytes: int = MAX_RESPONSE_BYTES, verify: bool | str = True) -> None:
        self.guard = guard or NetworkGuard(GuardPolicy())
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.max_bytes = max_bytes
        self.verify = verify

    async def request(self, method: str, url: str, *, guarded: bool = True,
                      **kwargs: Any) -> httpx.Response:
        target = url
        if guarded:
            target, dest = self.guard.pin_url(url)
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Host", dest.host)
            kwargs["headers"] = headers

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False,
                                     verify=self.verify) as client:
            response = await client.request(method, target, **kwargs)
            if len(response.content) > self.max_bytes:
                raise httpx.ReadError(f"response from {url} exceeded {self.max_bytes} bytes")
            return response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


def internal_client(timeout_s: float = 10.0) -> httpx.AsyncClient:
    """Client for trusted internal services (media gateway on loopback).

    Still time-bounded; unguarded only because the target is an
    operator-configured internal endpoint, not device-supplied input.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=False)
