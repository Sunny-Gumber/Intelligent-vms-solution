import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.services.search_page import (
    RecordingIndexPage,
    _MAX_SCAN_BATCHES,
    _consume_batch,
    _progress_token,
)


class RecordingIndexError(RuntimeError):
    """Raised when recording-index queries cannot be completed safely."""


def _parse_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _segment_cursor(row: dict):
    raw_id = row.get("segment_id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        return None
    segment_id = str(raw_id)
    if not segment_id or len(segment_id) > 128:
        return None
    parsed = _parse_timestamp(row.get("segment_start"))
    if parsed is None:
        return None
    return parsed, segment_id


def _interpret_segment_line(line: str, *, start: datetime, end: datetime, seen: set[str]):
    try:
        row = json.loads(line)
    except (ValueError, TypeError):
        return None, None, "invalid_json"
    if not isinstance(row, dict):
        return None, None, "invalid_shape"
    cursor = _segment_cursor(row)
    try:
        segment_id = str(row["segment_id"])
        if segment_id in seen:
            return None, cursor, None
        segment_start = datetime.fromisoformat(str(row["segment_start"]).replace("Z", "+00:00"))
        if segment_start.tzinfo is None:
            segment_start = segment_start.replace(tzinfo=timezone.utc)
        duration = float(row["duration_seconds"])
        segment_end = segment_start + timedelta(seconds=duration)
        if segment_end <= start or segment_start >= end:
            seen.add(segment_id)
            return None, (segment_start, segment_id), None
        seen.add(segment_id)
        row["segment_start"] = segment_start
        row["segment_end"] = segment_end
        row["duration_seconds"] = duration
        return row, (segment_start, segment_id), None
    except (KeyError, TypeError, ValueError):
        return None, cursor, "invalid_row"


class RecordingIndexClient:
    """Query completed recording-segment metadata from ClickHouse."""

    def __init__(self):
        self.base_url = settings.clickhouse_url.rstrip("/")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", settings.clickhouse_database):
            raise RuntimeError("Invalid ClickHouse database identifier")
        self.database = settings.clickhouse_database

    async def segments(
        self,
        *,
        tenant_id: str,
        site_id: str,
        camera_id: str,
        start: datetime,
        end: datetime,
        limit: int = 5000,
        after_segment_start: datetime | None = None,
        after_segment_id: str | None = None,
    ) -> RecordingIndexPage:
        """Return completed recording segments overlapping a bounded time window.

        Malformed backend lines are skipped and counted. The scan continues
        until `limit` overlapping segments are collected or the backend is
        exhausted. Pass the returned `next_after_*` cursor to read the next
        page. A missing cursor means the walk is finished.

        Args:
            tenant_id: Tenant scope for the camera.
            site_id: Site scope for the camera.
            camera_id: Camera identifier.
            start: Inclusive timezone-aware playback window start.
            end: Exclusive timezone-aware playback window end.
            limit: Maximum valid overlapping segments to return.
            after_segment_start: Optional exclusive keyset segment start.
            after_segment_id: Optional exclusive keyset segment id.

        Returns:
            Deduplicated segment dictionaries with parsed start/end timestamps.
            `partial` and `skipped_rows` report malformed lines skipped while
            filling the page. The continuation cursor is empty when the scan
            is exhausted.

        Raises:
            RecordingIndexError: If timestamps are naive, ClickHouse is
                unavailable, or the scan cannot advance past a repeated window.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise RecordingIndexError("recording index timestamps require timezone")
        if after_segment_start is not None and after_segment_start.tzinfo is None:
            raise RecordingIndexError("recording index timestamps require timezone")
        page_limit = max(1, min(limit, 10000))
        if end <= start:
            return RecordingIndexPage([], skipped_rows=0, exhausted=True)

        cursor = None
        if after_segment_start is not None:
            cursor = (after_segment_start, after_segment_id or "")
        collected: list[dict] = []
        skipped = 0
        offset = 0
        exhausted = False
        previous_progress = None
        seen: set[str] = set()

        def interpret(line: str):
            return _interpret_segment_line(line, start=start, end=end, seen=seen)

        for _ in range(_MAX_SCAN_BATCHES):
            progress = _progress_token(cursor, offset)
            if progress == previous_progress:
                raise RecordingIndexError("Recording index page could not advance")
            previous_progress = progress
            batch_limit = page_limit - len(collected)
            lines = await self._fetch_batch(
                tenant_id=tenant_id,
                site_id=site_id,
                camera_id=camera_id,
                start=start,
                end=end,
                cursor=cursor,
                batch_limit=batch_limit,
                offset=offset,
            )
            if not lines:
                exhausted = True
                break
            scan = _consume_batch(lines, collected, page_limit, interpret, "recording_index")
            skipped += scan.skipped
            if len(collected) >= page_limit:
                exhausted = scan.consumed_all and len(lines) < batch_limit
                break
            if len(lines) < batch_limit:
                exhausted = True
                break
            if scan.last_cursor is not None:
                cursor = scan.last_cursor
                offset = scan.trailing_unkeyed
            else:
                offset += len(lines)
        else:
            raise RecordingIndexError("Recording index page could not advance")

        next_start = None
        next_id = None
        if collected and not exhausted:
            last = collected[-1]
            next_start = last["segment_start"]
            next_id = str(last["segment_id"])
        return RecordingIndexPage(
            collected,
            skipped_rows=skipped,
            exhausted=exhausted,
            next_after_segment_start=next_start,
            next_after_segment_id=next_id,
        )

    async def _fetch_batch(
        self,
        *,
        tenant_id: str,
        site_id: str,
        camera_id: str,
        start: datetime,
        end: datetime,
        cursor,
        batch_limit: int,
        offset: int,
    ) -> list[str]:
        # Segment duration is bounded by the recording policy to <= 6 hours.
        # Query a bounded lookback, then perform exact overlap filtering in Python.
        query_start = start - timedelta(hours=6)
        params = {
            "param_tenant": tenant_id,
            "param_site": site_id,
            "param_camera": camera_id,
            "param_start": query_start.isoformat(),
            "param_end": end.isoformat(),
            "param_limit": str(batch_limit),
            "param_offset": str(offset),
            "date_time_input_format": "best_effort",
        }
        cursor_clause = ""
        if cursor is not None:
            after_start, after_id = cursor
            params["param_after_start"] = after_start.isoformat()
            if after_id:
                params["param_after_segment_id"] = after_id
                cursor_clause = """
  AND (segment_start > parseDateTime64BestEffort({after_start:String}, 3, 'UTC')
    OR (segment_start = parseDateTime64BestEffort({after_start:String}, 3, 'UTC')
        AND segment_id > {after_segment_id:String}))
"""
            else:
                cursor_clause = """
  AND segment_start > parseDateTime64BestEffort({after_start:String}, 3, 'UTC')
"""
        query = f"""
SELECT
 segment_id, recording_node_id, record_stream_key, segment_path,
 segment_start, duration_seconds, completed_at, storage_tier, object_uri
FROM {self.database}.recording_segments
WHERE tenant_id = {{tenant:String}}
  AND site_id = {{site:String}}
  AND camera_id = {{camera:String}}
  AND segment_start >= parseDateTime64BestEffort({{start:String}}, 3, 'UTC')
  AND segment_start < parseDateTime64BestEffort({{end:String}}, 3, 'UTC')
{cursor_clause}
ORDER BY segment_start ASC, segment_id ASC
LIMIT 1 BY segment_id
LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}
FORMAT JSONEachRow
"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.base_url + "/", params=params, content=query)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RecordingIndexError("Recording index unavailable") from exc
        return [line for line in response.text.splitlines() if line.strip()]

    async def segment_for_start(
        self,
        *,
        tenant_id: str,
        site_id: str,
        camera_id: str,
        start: datetime,
    ) -> dict | None:
        """Find the completed recording segment covering one playback instant.

        Args:
            tenant_id: Tenant scope for the camera.
            site_id: Site scope for the camera.
            camera_id: Camera identifier.
            start: Timezone-aware playback instant.

        Returns:
            Covering segment dictionary, or None when no completed segment covers it.

        Raises:
            RecordingIndexError: If the underlying recording-index query fails.
        """
        rows = await self.segments(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            start=start,
            end=start + timedelta(seconds=1),
            limit=50,
        )
        covering = [
            row
            for row in rows
            if row["segment_start"] <= start < row["segment_end"]
        ]
        if not covering:
            return None
        # Prefer the latest segment start when rare overlap exists during failover.
        return max(covering, key=lambda row: (row["segment_start"], row["segment_id"]))


recording_index = RecordingIndexClient()
