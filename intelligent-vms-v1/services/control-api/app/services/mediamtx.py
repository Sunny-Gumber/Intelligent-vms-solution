import asyncio
import logging
import shlex
import re
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import settings
from app.services.mediamtx_list_page import MediaMTXListPageError, assess_mediamtx_list_page

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
            Dictionary with merged ``items``, the page-0 ``itemCount`` and
            ``pageCount``, ``truncated``, and ``inconsistent``. A truncated or
            inconsistent result is not the full path set.

        Raises:
            httpx.HTTPError: If a path-list request fails.
            MediaMTXError: If a page body is not the v1.21.1 list object.
        """
        return await self._list_paged("/v3/paths/list")

    async def list_config_paths(self) -> dict:
        """Return configured MediaMTX paths across every upstream page.

        Returns:
            Dictionary with merged ``items``, the page-0 ``itemCount`` and
            ``pageCount``, ``truncated``, and ``inconsistent``. A truncated or
            inconsistent result is not the full configured set.

        Raises:
            httpx.HTTPError: If a configuration-list request fails.
            MediaMTXError: If a page body is not the v1.21.1 list object.
        """
        return await self._list_paged("/v3/config/paths/list")

    async def _list_paged(self, relative_path: str) -> dict:
        """Follow MediaMTX list pages until the catalog ends or the bound is hit.

        Args:
            relative_path: API path such as ``/v3/paths/list``.

        Returns:
            Merged list payload. ``truncated`` or ``inconsistent`` means the
            items are not the complete catalog. An HTTP, timeout, or malformed
            page raises instead of returning a partial catalog.

        Raises:
            httpx.HTTPError: If a page request fails.
            MediaMTXError: If a page body is not the v1.21.1 list object.
        """
        bound = int(settings.mediamtx_list_max_items)

        async with httpx.AsyncClient(timeout=5.0) as client:
            async def fetch_page(page: int, items_per_page: int):
                return await client.get(
                    f"{self.base_url}{relative_path}",
                    params={"page": page, "itemsPerPage": items_per_page},
                )

            result = await _enumerate_mediamtx_list(fetch_page, bound=bound)
        if result["truncated"]:
            log.warning(
                "mediamtx_list_truncated path=%s collected=%d item_count=%s page_count=%s bound=%d",
                relative_path,
                len(result["items"]),
                result.get("itemCount"),
                result.get("pageCount"),
                bound,
            )
        return result


def _list_payload(
    *,
    items: list,
    item_count: int | None,
    page_count: int | None,
    truncated: bool,
    inconsistent: bool,
) -> dict:
    return {
        "itemCount": item_count,
        "pageCount": page_count,
        "items": items,
        "truncated": truncated,
        "inconsistent": inconsistent,
    }


def _names_conflict(seen: set[str], names: list[str]) -> bool:
    if len(names) != len(set(names)):
        return True
    return any(name in seen for name in names)


async def _walk_mediamtx_list_once(fetch_page, *, bound: int) -> dict:
    """Read one bounded pass of a MediaMTX list.

    Args:
        fetch_page: Coroutine ``(page, items_per_page) -> response``.
        bound: Maximum items to keep before reporting truncation.

        Returns:
        List payload. ``inconsistent`` is true when a page fails the shared
        count contract, a later page changes those totals, a name repeats, or
        the finished unique count does not equal ``itemCount``. Counts are
        the upstream values, never a length rewritten into ``itemCount``.

    Raises:
        httpx.HTTPError: If a page request fails. Failures are not retried here.
        MediaMTXError: If a page body is not the v1.21.1 list object.
    """
    per_page = MEDIAMTX_LIST_DEFAULT_ITEMS_PER_PAGE
    max_pages = max(1, (bound + per_page - 1) // per_page)
    collected: list = []
    seen: set[str] = set()
    baseline_items: int | None = None
    baseline_pages: int | None = None

    for page in range(MEDIAMTX_LIST_DEFAULT_PAGE, MEDIAMTX_LIST_DEFAULT_PAGE + max_pages):
        if baseline_items is not None and len(collected) >= bound and len(seen) < baseline_items:
            return _list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=True,
                inconsistent=False,
            )
        response = await fetch_page(page, per_page)
        response.raise_for_status()
        body = response.json()
        try:
            assessed = assess_mediamtx_list_page(body, page=page, per_page=per_page)
        except MediaMTXListPageError as exc:
            raise MediaMTXError(str(exc)) from exc
        if not assessed["accepted"]:
            if baseline_items is None:
                return _list_payload(
                    items=assessed["items"],
                    item_count=assessed["item_count"],
                    page_count=assessed["page_count"],
                    truncated=False,
                    inconsistent=True,
                )
            return _list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=True,
            )

        item_count = assessed["item_count"]
        page_count = assessed["page_count"]
        page_items = assessed["items"]
        names = assessed["names"]
        if baseline_items is None:
            baseline_items = item_count
            baseline_pages = page_count
        elif item_count != baseline_items or page_count != baseline_pages:
            return _list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=True,
            )

        if baseline_pages == 0:
            empty = page == 0 and baseline_items == 0 and not names
            return _list_payload(
                items=[],
                item_count=0,
                page_count=0,
                truncated=False,
                inconsistent=not empty,
            )
        if page >= baseline_pages:
            return _list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=True,
            )

        expected_len = len(names)
        room = bound - len(collected)
        if room < expected_len:
            if len(names) < room or _names_conflict(seen, names[:room]):
                return _list_payload(
                    items=collected,
                    item_count=baseline_items,
                    page_count=baseline_pages,
                    truncated=False,
                    inconsistent=True,
                )
            collected.extend(page_items[:room])
            return _list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=True,
                inconsistent=False,
            )
        if _names_conflict(seen, names):
            return _list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=True,
            )
        seen.update(names)
        collected.extend(page_items)
        if page + 1 >= baseline_pages:
            return _list_payload(
                items=collected,
                item_count=baseline_items,
                page_count=baseline_pages,
                truncated=False,
                inconsistent=len(seen) != baseline_items,
            )

    if baseline_items is not None and len(seen) == baseline_items:
        return _list_payload(
            items=collected,
            item_count=baseline_items,
            page_count=baseline_pages,
            truncated=False,
            inconsistent=False,
        )
    return _list_payload(
        items=collected,
        item_count=baseline_items,
        page_count=baseline_pages,
        truncated=baseline_items is not None and len(collected) >= bound and len(seen) < baseline_items,
        inconsistent=not (baseline_items is not None and len(collected) >= bound and len(seen) < baseline_items),
    )


async def _enumerate_mediamtx_list(fetch_page, *, bound: int) -> dict:
    """Walk a MediaMTX list, retrying one inconsistent pass before failing closed.

    Args:
        fetch_page: Coroutine ``(page, items_per_page) -> response``.
        bound: Maximum items kept in one pass.

    Returns:
        List payload. A still-inconsistent second pass is returned with
        ``inconsistent`` true and must not be treated as complete state.

    Raises:
        httpx.HTTPError: If a page request fails. Transport failures are not retried.
        MediaMTXError: If a page body is not the v1.21.1 list object.
    """
    first = await _walk_mediamtx_list_once(fetch_page, bound=bound)
    if not first["inconsistent"]:
        return first
    log.warning("mediamtx_list_inconsistent_retry")
    second = await _walk_mediamtx_list_once(fetch_page, bound=bound)
    if second["inconsistent"]:
        log.error(
            "mediamtx_list_inconsistent item_count=%s page_count=%s collected=%d",
            second.get("itemCount"),
            second.get("pageCount"),
            len(second.get("items") or []),
        )
    return second


mediamtx = MediaMTXClient()
