"""Media gateway control-plane client (MediaMTX).

Architecture (ADR-0006): Intelligent VMS does NOT implement RTSP/WebRTC
transport itself. It programs a proven media gateway (MediaMTX, MIT licensed)
which pulls from the camera and republishes as WebRTC/WHEP for browsers and
RTSP for native clients. The control API only manipulates gateway paths and
issues short-lived viewing tokens.

Consequences:
  * The camera's RTSP URL and credentials stay between gateway and camera;
    clients never receive them.
  * A gateway crash cannot corrupt the database or stop recording (the
    recorder pulls from the camera directly through FFmpeg).
  * If the gateway is unreachable, live view fails with a clear 503 rather
    than hanging.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.outbound import internal_client
from app.logging_setup import get_logger, redact_text

log = get_logger("media.gateway")


class GatewayError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class GatewayPath:
    name: str
    source: str
    ready: bool = False
    readers: int = 0
    bytes_received: int = 0


class MediaGatewayClient:
    """Thin, timeout-bounded wrapper over the MediaMTX HTTP control API."""

    def __init__(self, base_url: str, *, username: str | None = None,
                 password: str | None = None, timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password) if username else None
        self.timeout_s = timeout_s

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with internal_client(self.timeout_s) as client:
            try:
                response = await client.request(method, f"{self.base_url}{path}",
                                                auth=self._auth, **kwargs)
            except httpx.HTTPError as exc:
                raise GatewayError(
                    f"media gateway unreachable at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise GatewayError(
                f"media gateway returned {response.status_code}: "
                f"{redact_text(response.text)[:200]}", response.status_code)
        return response

    async def health(self) -> bool:
        try:
            await self._request("GET", "/v3/paths/list",
                                params={"page": 0, "itemsPerPage": 1})
            return True
        except GatewayError:
            return False

    async def ensure_path(self, name: str, source_url: str, *, transport: str = "tcp",
                          read_user: str | None = None,
                          read_pass: str | None = None) -> None:
        """Create or update an on-demand pull path.

        sourceOnDemand means the gateway only connects to the camera while a
        viewer is watching, which keeps bandwidth bounded on large systems.
        """
        config: dict[str, Any] = {
            "source": source_url, "sourceOnDemand": True,
            "sourceOnDemandStartTimeout": "10s", "sourceOnDemandCloseAfter": "10s",
            "rtspTransport": transport}
        if read_user:
            config["readUser"] = read_user
            config["readPass"] = read_pass or ""
        try:
            await self._request("POST", f"/v3/config/paths/add/{name}", json=config)
        except GatewayError as exc:
            if exc.status == 400:
                await self._request("PATCH", f"/v3/config/paths/patch/{name}",
                                    json=config)
            else:
                raise

    async def remove_path(self, name: str) -> None:
        try:
            await self._request("DELETE", f"/v3/config/paths/delete/{name}")
        except GatewayError as exc:
            if exc.status not in (400, 404):
                raise

    async def path_status(self, name: str) -> GatewayPath | None:
        try:
            response = await self._request("GET", f"/v3/paths/get/{name}")
        except GatewayError as exc:
            if exc.status in (404, 400):
                return None
            raise
        data = response.json()
        return GatewayPath(name=data.get("name", name),
                           source=(data.get("source") or {}).get("type", "unknown"),
                           ready=bool(data.get("ready")),
                           readers=len(data.get("readers") or []),
                           bytes_received=int(data.get("bytesReceived") or 0))

    async def list_paths(self) -> list[GatewayPath]:
        response = await self._request("GET", "/v3/paths/list",
                                       params={"page": 0, "itemsPerPage": 1000})
        return [GatewayPath(name=i.get("name", ""),
                            source=(i.get("source") or {}).get("type", "unknown"),
                            ready=bool(i.get("ready")),
                            readers=len(i.get("readers") or []),
                            bytes_received=int(i.get("bytesReceived") or 0))
                for i in response.json().get("items", [])]

    async def kick_readers(self, name: str) -> int:
        """Disconnect viewers of a path (used when access is revoked)."""
        kicked = 0
        for kind in ("webrtcsessions", "rtspsessions", "hlsmuxers"):
            try:
                response = await self._request(
                    "GET", f"/v3/{kind}/list", params={"page": 0, "itemsPerPage": 1000})
            except GatewayError:
                continue
            for item in response.json().get("items", []):
                if item.get("path") != name or not item.get("id"):
                    continue
                try:
                    await self._request("POST", f"/v3/{kind}/kick/{item['id']}")
                    kicked += 1
                except GatewayError:
                    continue
        return kicked


class NullGatewayClient(MediaGatewayClient):
    """Used in tests and when live streaming is intentionally disabled.

    Reports itself as unavailable rather than pretending paths exist, so no
    code path can mistake a disabled gateway for a working one.
    """

    def __init__(self) -> None:
        super().__init__("http://disabled.invalid")

    async def health(self) -> bool:
        return False

    async def ensure_path(self, *args: Any, **kwargs: Any) -> None:
        raise GatewayError("media gateway is not configured")

    async def remove_path(self, name: str) -> None:
        return None

    async def path_status(self, name: str) -> GatewayPath | None:
        return None

    async def list_paths(self) -> list[GatewayPath]:
        return []

    async def kick_readers(self, name: str) -> int:
        return 0
