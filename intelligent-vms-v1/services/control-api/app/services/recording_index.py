import json
import math
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


# MediaMTX rejects a completed segment longer than one day. The indexed range
# uses that same bound so a long segment is not hidden by a shorter lookback.
_MAX_SEGMENT_DURATION = timedelta(days=1)
_MAX_SEGMENT_DURATION_SECONDS = 24 * 60 * 60
# Candidates already limited to rows that cover the instant. One page is enough
# for a placement overlap; a full page of malformed rows is scanned again.
_COVERING_CANDIDATE_LIMIT = 32

# Exclusive segment end at microsecond resolution. The if() keeps a non-finite
# duration from being rounded into an interval that would fail the query.
_EXCLUSIVE_SEGMENT_END_SQL = f"""segment_start + toIntervalMicrosecond(toInt64(round(
  if(isFinite(duration_seconds) AND duration_seconds > 0 AND duration_seconds <= {_MAX_SEGMENT_DURATION_SECONDS}, duration_seconds, 0) * 1000000
)))"""
_FINITE_DURATION_SQL = f"""
  AND isFinite(duration_seconds)
  AND duration_seconds > 0
  AND duration_seconds <= {_MAX_SEGMENT_DURATION_SECONDS}
"""


def _exclusive_segment_end(segment_start: datetime, duration_seconds: float) -> datetime:
    """Return the exclusive segment end at microsecond resolution.

    Args:
        segment_start: Inclusive timezone-aware segment start.
        duration_seconds: Segment length in seconds.

    Returns:
        First instant that is outside the segment.

    Raises:
        ValueError: If the duration is not a finite length inside the one-day maximum.
    """
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        raise ValueError("unsupported duration")
    if not math.isfinite(duration_seconds):
        raise ValueError("unsupported duration")
    if duration_seconds <= 0 or duration_seconds > _MAX_SEGMENT_DURATION_SECONDS:
        raise ValueError("unsupported duration")
    microseconds = int(round(float(duration_seconds) * 1_000_000))
    if microseconds <= 0:
        raise ValueError("unsupported duration")
    return segment_start + timedelta(microseconds=microseconds)


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
        if (
            isinstance(row["duration_seconds"], bool)
            or not math.isfinite(duration)
            or duration < 0
            or duration > _MAX_SEGMENT_DURATION_SECONDS
        ):
            raise ValueError("unsupported duration")
        if duration == 0:
            seen.add(segment_id)
            return None, (segment_start, segment_id), None
        segment_end = _exclusive_segment_end(segment_start, duration)
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

        The store applies the overlap predicate before `LIMIT`: a segment is
        in range when its start is before `end` and its exclusive end is after
        `start`. Pages walk oldest first by `(segment_start, segment_id)`.
        Malformed backend lines are skipped and counted. The scan continues
        until `limit` overlapping segments are collected or the backend is
        exhausted. Pass the returned `next_after_*` cursor to read the next
        page. A missing cursor means the walk is finished. The timeline and
        export routes request one page up to `recording_query_max_segments`
        and do not expose this cursor.

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
        # Completed segments are at most one day. Bound the primary-key range
        # by that maximum, then keep only rows that overlap [start, end).
        query_start = start - _MAX_SEGMENT_DURATION
        params = {
            "param_tenant": tenant_id,
            "param_site": site_id,
            "param_camera": camera_id,
            "param_start": query_start.isoformat(),
            "param_end": end.isoformat(),
            "param_window_start": start.isoformat(),
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
  AND (segment_start > parseDateTime64BestEffort({after_start:String}, 6, 'UTC')
    OR (segment_start = parseDateTime64BestEffort({after_start:String}, 6, 'UTC')
        AND segment_id > {after_segment_id:String}))
"""
            else:
                cursor_clause = """
  AND segment_start > parseDateTime64BestEffort({after_start:String}, 6, 'UTC')
"""
        query = f"""
SELECT
 segment_id, recording_node_id, record_stream_key, segment_path,
 segment_start, duration_seconds, completed_at, storage_tier, object_uri
FROM {self.database}.recording_segments
WHERE tenant_id = {{tenant:String}}
  AND site_id = {{site:String}}
  AND camera_id = {{camera:String}}
  AND segment_start >= parseDateTime64BestEffort({{start:String}}, 6, 'UTC')
  AND segment_start < parseDateTime64BestEffort({{end:String}}, 6, 'UTC')
{_FINITE_DURATION_SQL}
  AND {_EXCLUSIVE_SEGMENT_END_SQL} > parseDateTime64BestEffort({{window_start:String}}, 6, 'UTC')
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

        The store keeps only rows whose half-open interval contains `start`
        (`segment_start <= start < segment_end`) and applies `LIMIT` after that
        predicate. Overlapping owners, such as a placement move, resolve to the
        greatest `segment_start` and then the greatest `segment_id`. The
        camera's current node is not an input. Equal to `segment_start` is
        inside; equal to `segment_end`, and one microsecond later, is outside.

        Args:
            tenant_id: Tenant scope for the camera.
            site_id: Site scope for the camera.
            camera_id: Camera identifier.
            start: Timezone-aware playback instant.

        Returns:
            Covering segment dictionary, or None when no completed segment covers it.
            None tells playback to ask the current recorder, which must prove its
            own coverage before streaming.

        Raises:
            RecordingIndexError: If the timestamp is naive or the recording-index
                query fails.
        """
        if start.tzinfo is None:
            raise RecordingIndexError("recording index timestamps require timezone")
        window_end = start + timedelta(microseconds=1)
        cursor = None
        previous_progress = None
        seen: set[str] = set()

        def interpret(line: str):
            return _interpret_segment_line(line, start=start, end=window_end, seen=seen)

        for _ in range(_MAX_SCAN_BATCHES):
            progress = _progress_token(cursor, 0)
            if progress == previous_progress:
                raise RecordingIndexError("Recording index page could not advance")
            previous_progress = progress
            lines = await self._fetch_covering(
                tenant_id=tenant_id,
                site_id=site_id,
                camera_id=camera_id,
                instant=start,
                cursor=cursor,
                batch_limit=_COVERING_CANDIDATE_LIMIT,
            )
            if not lines:
                return None
            covering: list[dict] = []
            last_cursor = None
            for line in lines:
                row, row_cursor, _reason = interpret(line)
                if row_cursor is not None:
                    last_cursor = row_cursor
                if row is not None and row["segment_start"] <= start < row["segment_end"]:
                    covering.append(row)
            if covering:
                return max(covering, key=lambda row: (row["segment_start"], str(row["segment_id"])))
            if len(lines) < _COVERING_CANDIDATE_LIMIT or last_cursor is None:
                return None
            cursor = last_cursor
        raise RecordingIndexError("Recording index page could not advance")

    async def _fetch_covering(
        self,
        *,
        tenant_id: str,
        site_id: str,
        camera_id: str,
        instant: datetime,
        cursor,
        batch_limit: int,
    ) -> list[str]:
        not_before = instant - _MAX_SEGMENT_DURATION
        params = {
            "param_tenant": tenant_id,
            "param_site": site_id,
            "param_camera": camera_id,
            "param_instant": instant.isoformat(),
            "param_not_before": not_before.isoformat(),
            "param_limit": str(batch_limit),
            "date_time_input_format": "best_effort",
        }
        cursor_clause = ""
        if cursor is not None:
            before_start, before_id = cursor
            params["param_before_start"] = before_start.isoformat()
            if before_id:
                params["param_before_segment_id"] = before_id
                cursor_clause = """
  AND (segment_start < parseDateTime64BestEffort({before_start:String}, 6, 'UTC')
    OR (segment_start = parseDateTime64BestEffort({before_start:String}, 6, 'UTC')
        AND segment_id < {before_segment_id:String}))
"""
            else:
                cursor_clause = """
  AND segment_start < parseDateTime64BestEffort({before_start:String}, 6, 'UTC')
"""
        query = f"""
SELECT
 segment_id, recording_node_id, record_stream_key, segment_path,
 segment_start, duration_seconds, completed_at, storage_tier, object_uri
FROM {self.database}.recording_segments
WHERE tenant_id = {{tenant:String}}
  AND site_id = {{site:String}}
  AND camera_id = {{camera:String}}
  AND segment_start >= parseDateTime64BestEffort({{not_before:String}}, 6, 'UTC')
  AND segment_start <= parseDateTime64BestEffort({{instant:String}}, 6, 'UTC')
{_FINITE_DURATION_SQL}
  AND {_EXCLUSIVE_SEGMENT_END_SQL} > parseDateTime64BestEffort({{instant:String}}, 6, 'UTC')
{cursor_clause}
ORDER BY segment_start DESC, segment_id DESC
LIMIT 1 BY segment_id
LIMIT {{limit:UInt32}}
FORMAT JSONEachRow
"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.base_url + "/", params=params, content=query)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RecordingIndexError("Recording index unavailable") from exc
        return [line for line in response.text.splitlines() if line.strip()]


recording_index = RecordingIndexClient()
