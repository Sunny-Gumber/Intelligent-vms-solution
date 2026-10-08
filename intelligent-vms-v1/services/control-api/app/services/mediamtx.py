import asyncio
import logging
import shlex
import re
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

# Pinned MediaMTX v1.21.1: internal/api/paginate.go and api/openapi.yaml.
# An empty page query defaults to 0. An empty itemsPerPage query defaults to 100.
# itemCount is the total before pagination. pageCount is the number of pages.
MEDIAMTX_LIST_DEFAULT_PAGE = 0
MEDIAMTX_LIST_DEFAULT_ITEMS_PER_PAGE = 100


class MediaMTXError(RuntimeError):
    """Raised when a MediaMTX configuration or API operation cannot be completed."""


def source_options(source_uri: str, source_fingerprint: str | None) -> dict:
    """Validate internal source transport and explicit device certificate trust.

    Args:
        source_uri: Credential-bearing internal RTSP/RTSPS URI, never returned publicly.
        source_fingerprint: Optional operator-approved SHA-256 leaf certificate hash.

    Returns:
        MediaMTX source options, including an empty pin to clear stale configuration.

    Raises:
        MediaMTXError: If protocol or fingerprint is invalid; input is never echoed.
    """
    try:
        parsed = urlsplit(source_uri)
        valid = parsed.scheme in {"rtsp", "rtsps"} and bool(parsed.hostname) and (parsed.port is None or 1 <= parsed.port <= 65535)
    except ValueError:
        valid = False
    if not valid:
        raise MediaMTXError("Invalid camera source transport")
    if source_fingerprint and (parsed.scheme != "rtsps" or not re.fullmatch(r"[0-9a-fA-F]{64}", source_fingerprint)):
        raise MediaMTXError("Invalid camera source certificate fingerprint")
    return {"source": source_uri, "sourceFingerprint": (source_fingerprint or "").lower()}


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

    async def add_or_replace_path(
        self, stream_key: str, source_uri: str, *, source_fingerprint: str | None = None,
    ) -> None:
        """Create or update one on-demand live stream path.

        Args:
            stream_key: MediaMTX path key exposed by the VMS.
            source_uri: Camera/source RTSP URI used by MediaMTX.
            source_fingerprint: Optional approved RTSPS leaf certificate hash.

        Returns:
            None after the path configuration is accepted.

        Raises:
            MediaMTXError: If the media node rejects both add and patch operations.
            httpx.HTTPError: If the MediaMTX HTTP request fails at transport level.
        """
        await self._upsert(
            stream_key,
            {
                **source_options(source_uri, source_fingerprint),
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
        source_fingerprint: str | None = None,
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
            source_fingerprint: Optional approved RTSPS leaf certificate hash.

        Returns:
            None after the recording path configuration is accepted.

        Raises:
            MediaMTXError: If the callback URL is unsafe or the node rejects the config.
            httpx.HTTPError: If the MediaMTX HTTP request fails at transport level.
        """
        if settings.recording_hook_command:
            hook = settings.recording_hook_command
        else:
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
                raise MediaMTXError(
                    "Recording hook callback must not contain credentials or fragment"
                )
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
                **source_options(source_uri, source_fingerprint),
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
        """Return runtime MediaMTX path state across every upstream page.

        Returns:
            Dictionary with merged ``items``, upstream ``itemCount`` and
            ``pageCount``, and ``truncated``. ``truncated`` is true when the
            configured bound stopped the walk before the catalog was complete.
            A truncated result is not the full path set.

        Raises:
            httpx.HTTPError: If a path-list request fails.
            MediaMTXError: If a page body is not a JSON object.
        """
        return await self._list_paged("/v3/paths/list")

    async def list_config_paths(self) -> dict:
        """Return configured MediaMTX paths across every upstream page.

        Returns:
            Dictionary with merged ``items``, upstream ``itemCount`` and
            ``pageCount``, and ``truncated``. ``truncated`` is true when the
            configured bound stopped the walk before the catalog was complete.
            A truncated result is not the full configured set.

        Raises:
            httpx.HTTPError: If a configuration-list request fails.
            MediaMTXError: If a page body is not a JSON object.
        """
        return await self._list_paged("/v3/config/paths/list")

    async def _list_paged(self, relative_path: str) -> dict:
        """Follow MediaMTX list pages until the catalog ends or the bound is hit.

        Args:
            relative_path: API path such as ``/v3/paths/list``.

        Returns:
            Merged list payload. ``truncated`` is true when ``mediamtx_list_max_items``
            stopped enumeration while upstream ``itemCount`` or ``pageCount`` still
            reported further paths.

        Raises:
            httpx.HTTPError: If a page request fails.
            MediaMTXError: If a page body is not a JSON object.
        """
        bound = int(settings.mediamtx_list_max_items)
        items_per_page = MEDIAMTX_LIST_DEFAULT_ITEMS_PER_PAGE
        max_pages = max(1, (bound + items_per_page - 1) // items_per_page)
        collected: list = []
        truncated = False
        item_count: int | None = None
        page_count: int | None = None

        async with httpx.AsyncClient(timeout=5.0) as client:
            for page in range(MEDIAMTX_LIST_DEFAULT_PAGE, MEDIAMTX_LIST_DEFAULT_PAGE + max_pages):
                if len(collected) >= bound:
                    truncated = True
                    break
                response = await client.get(
                    f"{self.base_url}{relative_path}",
                    params={"page": page, "itemsPerPage": items_per_page},
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise MediaMTXError("MediaMTX list response was not an object")
                page_items = body.get("items", [])
                if not isinstance(page_items, list):
                    raise MediaMTXError("MediaMTX list items was not a list")
                parsed_item_count = _optional_count(body.get("itemCount"))
                parsed_page_count = _optional_count(body.get("pageCount"))
                if parsed_item_count is not None:
                    item_count = parsed_item_count
                if parsed_page_count is not None:
                    page_count = parsed_page_count

                room = bound - len(collected)
                if len(page_items) > room:
                    collected.extend(page_items[:room])
                    truncated = True
                    break
                collected.extend(page_items)

                reached_item_count = item_count is not None and len(collected) >= item_count
                reached_page_count = page_count is not None and (page + 1) >= page_count
                short_page = len(page_items) < items_per_page
                if reached_item_count or reached_page_count or short_page:
                    if item_count is not None and len(collected) < item_count:
                        truncated = True
                    break
            else:
                if item_count is None or len(collected) < item_count:
                    if page_count is None or max_pages < page_count:
                        truncated = True

        if truncated:
            log.warning(
                "mediamtx_list_truncated path=%s collected=%d item_count=%s page_count=%s bound=%d",
                relative_path,
                len(collected),
                "unknown" if item_count is None else item_count,
                "unknown" if page_count is None else page_count,
                bound,
            )
        reported_item_count = item_count if item_count is not None else len(collected)
        if page_count is not None:
            reported_page_count = page_count
        elif collected:
            reported_page_count = 1
        else:
            reported_page_count = 0
        return {
            "itemCount": reported_item_count,
            "pageCount": reported_page_count,
            "items": collected,
            "truncated": truncated,
        }


def _optional_count(value: object) -> int | None:
    """Return a non-negative MediaMTX total, ignoring missing or non-integer values.

    Args:
        value: Raw ``itemCount`` or ``pageCount`` field.

    Returns:
        The count when it is a non-negative integer, otherwise None.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


mediamtx = MediaMTXClient()
