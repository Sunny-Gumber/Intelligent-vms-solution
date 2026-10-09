import math
from datetime import datetime
from typing import AsyncIterator

import httpx

from app.core.config import settings


UPSTREAM_TIMEOUT_DETAIL = "Recording playback upstream timed out"
UPSTREAM_FAILURE_DETAIL = "Recording playback upstream failed"


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

    def _stream_timeout(self, timeout_seconds: float | None) -> httpx.Timeout:
        """Build a finite timeout for one playback or export stream.

        Args:
            timeout_seconds: Export's single per-phase limit, when the caller
                passed one. Ordinary playback passes None.

        Returns:
            An httpx timeout whose connect, read, write, and pool values are
            all finite. Export uses ``timeout_seconds`` for every phase.
            Ordinary playback uses the configured connect limit and applies the
            configured read limit to read, write, and pool.

        Raises:
            PlaybackError: If ordinary-playback settings are not finite and
                greater than zero. Export values are handed to httpx unchanged.
        """
        if timeout_seconds is not None:
            # Export keeps one duration for connect, read, write, and pool.
            return httpx.Timeout(timeout_seconds)
        connect = settings.recording_playback_connect_timeout_seconds
        read = settings.recording_playback_read_timeout_seconds
        if not _positive_finite(connect) or not _positive_finite(read):
            raise PlaybackError("Playback upstream timeout configuration is invalid", 500)
        return httpx.Timeout(float(read), connect=float(connect))

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
            timeout_seconds: Single per-phase timeout for export. When omitted,
                ordinary playback uses the configured connect and read limits.
                None does not disable the timeout.

        Returns:
            Tuple of the open httpx client and streaming response. Caller must close both.

        Raises:
            PlaybackError: If the playback node returns an error status, if
                ordinary playback times out (504) or cannot reach the upstream
                (502), or if the ordinary-playback timeout settings are invalid.
            httpx.TimeoutException: If ``timeout_seconds`` was passed and that
                export timeout elapses. The client is closed before this raises.
            asyncio.CancelledError: If the caller is cancelled. The client is
                closed before this propagates.
        """
        params = {
            "path": path,
            "start": start.isoformat(),
            "duration": str(duration),
            "format": fmt,
        }
        headers = {"Range": range_header} if range_header else {}
        resolved = self._stream_timeout(timeout_seconds)
        client = httpx.AsyncClient(timeout=resolved)
        response: httpx.Response | None = None
        handed_off = False
        try:
            request = client.build_request("GET", f"{self.base_url}/get", params=params, headers=headers)
            response = await client.send(request, stream=True)
            if response.status_code >= 400:
                raise PlaybackError(
                    f"Playback node returned status {response.status_code}",
                    404 if response.status_code == 404 else 502,
                )
            handed_off = True
            return client, response
        except httpx.TimeoutException as exc:
            if timeout_seconds is not None:
                raise
            raise PlaybackError(UPSTREAM_TIMEOUT_DETAIL, 504) from exc
        except httpx.HTTPError as exc:
            if timeout_seconds is not None:
                raise
            raise PlaybackError(UPSTREAM_FAILURE_DETAIL, 502) from exc
        finally:
            if not handed_off:
                await _aclose_stream(response, client)


def _positive_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and number > 0


async def _aclose_stream(response: httpx.Response | None, client: httpx.AsyncClient) -> None:
    """Close a playback response and its client without raising a second error."""
    if response is not None:
        try:
            await response.aclose()
        except Exception:
            pass
    try:
        await client.aclose()
    except Exception:
        pass


playback_client = PlaybackClient()
