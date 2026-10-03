from datetime import datetime
from typing import AsyncIterator

import httpx

from app.core.config import settings


class PlaybackError(RuntimeError):
    """Represent a bounded playback/timeline failure safe for API translation."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class PlaybackClient:
    """Access one MediaMTX playback service for timeline and stream retrieval."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.mediamtx_playback_internal_url).rstrip("/")

    async def list_timespans(
        self,
        path: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        """List recorded timespans available for one recording path.

        Args:
            path: MediaMTX recording path key.
            start: Optional timeline start.
            end: Optional timeline end.

        Returns:
            Playback-service timespan dictionaries.

        Raises:
            PlaybackError: If the service is unavailable or returns invalid data.
        """
        params: dict[str, str] = {"path": path}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/list", params=params)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PlaybackError("Recording timeline service unavailable") from exc
        if not isinstance(data, list):
            raise PlaybackError("Invalid recording timeline response")
        return data

    async def open_stream(
        self,
        path: str,
        start: datetime,
        duration: float,
        fmt: str,
        range_header: str | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[httpx.AsyncClient, httpx.Response]:
        """Open a streaming playback response without buffering the recording.

        Args:
            path: MediaMTX recording path key.
            start: Playback start timestamp.
            duration: Requested playback duration in seconds.
            fmt: Playback format expected by the MediaMTX playback service.
            range_header: Optional HTTP Range header from the downstream client.
            timeout_seconds: Optional per-operation HTTP timeout for bounded export use.

        Returns:
            Tuple of the open httpx client and streaming response. Caller must close both.

        Raises:
            PlaybackError: If the playback node returns an error status.
            Exception: Transport/build failures propagate after client cleanup.
        """
        params = {
            "path": path,
            "start": start.isoformat(),
            "duration": str(duration),
            "format": fmt,
        }
        headers = {"Range": range_header} if range_header else {}
        client = httpx.AsyncClient(timeout=timeout_seconds)
        try:
            request = client.build_request("GET", f"{self.base_url}/get", params=params, headers=headers)
            response = await client.send(request, stream=True)
            if response.status_code >= 400:
                await response.aclose()
                await client.aclose()
                raise PlaybackError(
                    f"Playback node returned status {response.status_code}",
                    404 if response.status_code == 404 else 502,
                )
            return client, response
        except Exception:
            await client.aclose()
            raise


playback_client = PlaybackClient()
