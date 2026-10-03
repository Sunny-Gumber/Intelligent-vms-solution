import asyncio
import shlex
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import settings


class MediaMTXError(RuntimeError):
    """Raised when a MediaMTX configuration or API operation cannot be completed."""


class MediaMTXClient:
    """Configure and inspect one MediaMTX node through its HTTP API."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.mediamtx_api_url).rstrip("/")
        self._mutation_lock = asyncio.Lock()

    async def _upsert(self, stream_key: str, payload: dict) -> None:
        async with self._mutation_lock:
            async with httpx.AsyncClient(timeout=10.0) as client:
                add = await client.post(
                    f"{self.base_url}/v3/config/paths/add/{quote(stream_key, safe='')}",
                    json=payload,
                )
                if add.status_code in (200, 201, 204):
                    return
                patch = await client.patch(
                    f"{self.base_url}/v3/config/paths/patch/{quote(stream_key, safe='')}",
                    json=payload,
                )
                if patch.status_code not in (200, 204):
                    raise MediaMTXError(
                        f"Media node rejected path config: add={add.status_code}, patch={patch.status_code}"
                    )

    async def add_or_replace_path(self, stream_key: str, source_uri: str) -> None:
        """Create or update one on-demand live stream path.

        Args:
            stream_key: MediaMTX path key exposed by the VMS.
            source_uri: Camera/source RTSP URI used by MediaMTX.

        Returns:
            None after the path configuration is accepted.

        Raises:
            MediaMTXError: If the media node rejects both add and patch operations.
            httpx.HTTPError: If the MediaMTX HTTP request fails at transport level.
        """
        await self._upsert(
            stream_key,
            {
                "source": source_uri,
                "sourceOnDemand": True,
                "rtspTransport": "tcp",
                "record": False,
                "maxReaders": settings.live_view_max_readers_per_path,
            },
        )

    async def add_or_replace_recording_path(
        self,
        stream_key: str,
        source_uri: str,
        *,
        retention_days: int,
        part_duration_ms: int,
        segment_duration_seconds: int,
        max_part_size_mb: int,
        recording_node_id: str | None = None,
        assignment_generation: int | None = None,
    ) -> None:
        """Create or update one continuous recording path and completion hook.

        Args:
            stream_key: MediaMTX recording path key.
            source_uri: Camera/source RTSP URI.
            retention_days: Recording retention duration in days.
            part_duration_ms: fMP4 part duration in milliseconds.
            segment_duration_seconds: Recording segment duration in seconds.
            max_part_size_mb: Maximum fMP4 part size in megabytes.
            recording_node_id: Optional distributed recorder identity included in hooks.
            assignment_generation: Optional fencing generation included in hooks.

        Returns:
            None after the recording path configuration is accepted.

        Raises:
            MediaMTXError: If the callback URL is unsafe or the node rejects the config.
            httpx.HTTPError: If the MediaMTX HTTP request fails at transport level.
        """
        node_arg = (
            f' --data-urlencode "recording_node_id={recording_node_id}"'
            if recording_node_id
            else ""
        )
        generation_arg = (
            f' --data-urlencode "assignment_generation={assignment_generation}"'
            if assignment_generation is not None
            else ""
        )
        callback = urlsplit(settings.recording_hook_callback_url)
        if callback.scheme not in {"http", "https"} or not callback.hostname:
            raise MediaMTXError("Recording hook callback must be an HTTP/HTTPS URL")
        if callback.username or callback.password or callback.fragment:
            raise MediaMTXError("Recording hook callback must not contain credentials or fragment")
        callback_arg = shlex.quote(settings.recording_hook_callback_url)
        hook = (
            'curl -fsS --retry 2 --connect-timeout 2 -X POST '
            '-H "X-Recording-Hook-Token: $RECORDING_HOOK_TOKEN" '
            '--data-urlencode "path=$MTX_PATH" '
            '--data-urlencode "segment_path=$MTX_SEGMENT_PATH" '
            '--data-urlencode "duration=$MTX_SEGMENT_DURATION"'
            + node_arg
            + generation_arg
            + f' {callback_arg} >/dev/null'
        )
        await self._upsert(
            stream_key,
            {
                "source": source_uri,
                "sourceOnDemand": False,
                "rtspTransport": "tcp",
                "record": True,
                "recordPath": settings.recording_path_template,
                "recordFormat": "fmp4",
                "recordPartDuration": f"{part_duration_ms}ms",
                "recordMaxPartSize": f"{max_part_size_mb}M",
                "recordSegmentDuration": f"{segment_duration_seconds}s",
                "recordDeleteAfter": f"{retention_days * 24}h",
                "runOnRecordSegmentComplete": hook,
            },
        )

    async def delete_path(self, stream_key: str) -> None:
        """Delete one MediaMTX path if it exists.

        Args:
            stream_key: Path key to remove.

        Returns:
            None when the path is deleted or already absent.

        Raises:
            MediaMTXError: If MediaMTX returns an unexpected delete status.
            httpx.HTTPError: If the HTTP request fails at transport level.
        """
        async with self._mutation_lock:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.delete(
                    f"{self.base_url}/v3/config/paths/delete/{quote(stream_key, safe='')}"
                )
                if response.status_code not in (200, 204, 404):
                    raise MediaMTXError(f"Media node delete failed: {response.status_code}")

    async def list_paths(self) -> dict:
        """Return runtime MediaMTX path state.

        Returns:
            MediaMTX runtime path-list response as a dictionary.

        Raises:
            httpx.HTTPError: If the path-list request fails.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{self.base_url}/v3/paths/list")
            response.raise_for_status()
            return response.json()

    async def list_config_paths(self) -> dict:
        """Return configured MediaMTX paths.

        Returns:
            MediaMTX path-configuration response as a dictionary.

        Raises:
            httpx.HTTPError: If the configuration-list request fails.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{self.base_url}/v3/config/paths/list")
            response.raise_for_status()
            return response.json()


mediamtx = MediaMTXClient()
