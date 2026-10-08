import json
import re
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.models.schemas import EventRead
from app.services.search_page import (
    EventSearchPage,
    _MAX_SCAN_BATCHES,
    _consume_batch,
    _progress_token,
    _safe_row_id,
)


class EventSearchError(RuntimeError):
    """Raised when the ClickHouse event-search backend is unavailable."""


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


def _event_cursor(payload: dict):
    event_id = payload.get("event_id")
    if not isinstance(event_id, str):
        return None
    safe_id = _safe_row_id(event_id)
    timestamp = _parse_timestamp(payload.get("timestamp"))
    if safe_id is None or timestamp is None:
        return None
    return timestamp, safe_id


def _interpret_event_line(line: str):
    try:
        payload = json.loads(line)
    except (ValueError, TypeError):
        return None, None, "invalid_json"
    if not isinstance(payload, dict):
        return None, None, "invalid_shape"
    cursor = _event_cursor(payload)
    try:
        raw = payload.pop("attributes_json", "{}")
        payload["attributes"] = json.loads(raw) if isinstance(raw, str) else {}
        if not isinstance(payload["attributes"], dict):
            raise TypeError("attributes")
        EventRead.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        return None, cursor, "invalid_row"
    return payload, cursor, None


class EventSearchClient:
    """Query normalized VMS events from ClickHouse with bounded scoped filters."""

    def __init__(self):
        self.base_url = settings.clickhouse_url.rstrip("/")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", settings.clickhouse_database):
            raise RuntimeError("Invalid ClickHouse database identifier")
        self.database = settings.clickhouse_database

    async def search(
        self,
        *,
        tenant_id: str | None,
        allowed_sites: list[str] | None,
        site_id: str | None,
        camera_id: str | None,
        event_type: str | None,
        severity: str | None,
        start: datetime,
        end: datetime,
        limit: int,
        before_timestamp: datetime | None = None,
        before_event_id: str | None = None,
    ) -> EventSearchPage:
        """Search scoped event history with parameterized ClickHouse predicates.

        Malformed backend lines are skipped and counted. The scan continues
        until `limit` valid events are collected or the backend is exhausted, so
        a malformed row cannot make this page look like the last page.

        Args:
            tenant_id: Optional tenant restriction derived from caller scope.
            allowed_sites: Optional explicit site allowlist.
            site_id: Optional site filter.
            camera_id: Optional camera filter.
            event_type: Optional normalized event-type filter.
            severity: Optional severity filter.
            start: Inclusive search-window start.
            end: Exclusive search-window end.
            limit: Maximum number of valid events to return.
            before_timestamp: Optional exclusive keyset timestamp.
            before_event_id: Optional exclusive keyset event id.

        Returns:
            Valid event dictionaries ordered newest first. `partial` and
            `skipped_rows` report malformed lines skipped while filling the
            page. `exhausted` is true when no further valid event remains.

        Raises:
            EventSearchError: If the ClickHouse HTTP request fails or the scan
                cannot advance past a repeated window.
        """
        if allowed_sites is not None and site_id is None and not allowed_sites:
            return EventSearchPage([], skipped_rows=0, exhausted=True)

        clauses = [
            "timestamp >= parseDateTime64BestEffort({start:String}, 3, 'UTC')",
            "timestamp < parseDateTime64BestEffort({end:String}, 3, 'UTC')",
        ]
        params: dict[str, str] = {
            "param_start": start.isoformat(),
            "param_end": end.isoformat(),
            "date_time_input_format": "best_effort",
        }

        def add_equal(column: str, name: str, value: str | None):
            if value is not None:
                clauses.append(f"{column} = {{{name}:String}}")
                params[f"param_{name}"] = value

        add_equal("tenant_id", "tenant", tenant_id)
        add_equal("site_id", "site", site_id)
        add_equal("camera_id", "camera", camera_id)
        add_equal("event_type", "event_type", event_type)
        add_equal("severity", "severity", severity)

        if allowed_sites is not None and site_id is None:
            site_terms = []
            for index, value in enumerate(allowed_sites):
                name = f"allowed_site_{index}"
                site_terms.append(f"site_id = {{{name}:String}}")
                params[f"param_{name}"] = value
            clauses.append("(" + " OR ".join(site_terms) + ")")

        cursor = None
        if before_timestamp is not None:
            cursor = (before_timestamp, before_event_id or "")
        return await self._fill_page(clauses, params, limit=limit, cursor=cursor)

    async def _fill_page(self, clauses, base_params, *, limit: int, cursor) -> EventSearchPage:
        collected: list[dict] = []
        skipped = 0
        offset = 0
        exhausted = False
        previous_progress = None
        if limit <= 0:
            return EventSearchPage([], skipped_rows=0, exhausted=True)

        for _ in range(_MAX_SCAN_BATCHES):
            progress = _progress_token(cursor, offset)
            if progress == previous_progress:
                raise EventSearchError("Event search page could not advance")
            previous_progress = progress
            batch_limit = limit - len(collected)
            lines = await self._fetch_batch(clauses, base_params, cursor, batch_limit, offset)
            if not lines:
                exhausted = True
                break
            scan = _consume_batch(lines, collected, limit, _interpret_event_line, "event_search")
            skipped += scan.skipped
            if len(collected) >= limit:
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
            raise EventSearchError("Event search page could not advance")

        return EventSearchPage(collected, skipped_rows=skipped, exhausted=exhausted)

    async def _fetch_batch(self, clauses, base_params, cursor, batch_limit: int, offset: int) -> list[str]:
        batch_clauses = list(clauses)
        params = dict(base_params)
        params["param_limit"] = str(batch_limit)
        params["param_offset"] = str(offset)
        if cursor is not None:
            before_timestamp, before_event_id = cursor
            params["param_before_timestamp"] = before_timestamp.isoformat()
            if before_event_id:
                params["param_before_event_id"] = before_event_id
                batch_clauses.append(
                    "(timestamp < parseDateTime64BestEffort({before_timestamp:String}, 3, 'UTC') "
                    "OR (timestamp = parseDateTime64BestEffort({before_timestamp:String}, 3, 'UTC') "
                    "AND event_id < {before_event_id:String}))"
                )
            else:
                batch_clauses.append(
                    "timestamp < parseDateTime64BestEffort({before_timestamp:String}, 3, 'UTC')"
                )
        query = f"""
SELECT
 event_id, tenant_id, site_id, camera_id, timestamp, event_type, object_type,
 source, confidence, zone_id, severity, snapshot_uri, recording_start,
 recording_end, attributes_json, ingested_at
FROM {self.database}.events
WHERE {' AND '.join(batch_clauses)}
ORDER BY timestamp DESC, event_id DESC
LIMIT 1 BY event_id
LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}
FORMAT JSONEachRow
"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.base_url + "/", params=params, content=query)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EventSearchError("Event search store unavailable") from exc
        return [line for line in response.text.splitlines() if line.strip()]


event_search = EventSearchClient()
