import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings


class RecordingIndexError(RuntimeError):
    """Raised when recording-index queries cannot be completed safely."""


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
    ) -> list[dict]:
        """Return completed recording segments overlapping a bounded time window.

        Args:
            tenant_id: Tenant scope for the camera.
            site_id: Site scope for the camera.
            camera_id: Camera identifier.
            start: Inclusive timezone-aware playback window start.
            end: Exclusive timezone-aware playback window end.
            limit: Maximum candidate segments to fetch.

        Returns:
            Deduplicated segment dictionaries with parsed start/end timestamps.

        Raises:
            RecordingIndexError: If timestamps are naive or ClickHouse is unavailable.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise RecordingIndexError("recording index timestamps require timezone")
        if end <= start:
            return []

        # Segment duration is bounded by the recording policy to <= 6 hours.
        # Query a bounded lookback, then perform exact overlap filtering in Python.
        query_start = start - timedelta(hours=6)
        params = {
            "param_tenant": tenant_id,
            "param_site": site_id,
            "param_camera": camera_id,
            "param_start": query_start.isoformat(),
            "param_end": end.isoformat(),
            "param_limit": str(max(1, min(limit, 10000))),
            "date_time_input_format": "best_effort",
        }
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
ORDER BY segment_start ASC, segment_id ASC
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

        rows: list[dict] = []
        seen: set[str] = set()
        for line in response.text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                segment_id = str(row["segment_id"])
                if segment_id in seen:
                    continue
                seen.add(segment_id)
                segment_start = datetime.fromisoformat(
                    str(row["segment_start"]).replace("Z", "+00:00")
                )
                if segment_start.tzinfo is None:
                    segment_start = segment_start.replace(tzinfo=timezone.utc)
                duration = float(row["duration_seconds"])
                segment_end = segment_start + timedelta(seconds=duration)
                if segment_end <= start or segment_start >= end:
                    continue
                row["segment_start"] = segment_start
                row["segment_end"] = segment_end
                row["duration_seconds"] = duration
                rows.append(row)
            except (KeyError, TypeError, ValueError):
                continue
        return rows

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
