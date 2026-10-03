import json
import re
from datetime import datetime

import httpx

from app.core.config import settings


class EventSearchError(RuntimeError):
    """Raised when the ClickHouse event-search backend is unavailable."""


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
    ) -> list[dict]:
        """Search scoped event history with parameterized ClickHouse predicates.

        Args:
            tenant_id: Optional tenant restriction derived from caller scope.
            allowed_sites: Optional explicit site allowlist.
            site_id: Optional site filter.
            camera_id: Optional camera filter.
            event_type: Optional normalized event-type filter.
            severity: Optional severity filter.
            start: Inclusive search-window start.
            end: Exclusive search-window end.
            limit: Maximum number of returned events.

        Returns:
            Event dictionaries ordered newest first, with attributes decoded.

        Raises:
            EventSearchError: If the ClickHouse HTTP request fails.
        """
        clauses = [
            "timestamp >= parseDateTime64BestEffort({start:String}, 3, 'UTC')",
            "timestamp < parseDateTime64BestEffort({end:String}, 3, 'UTC')",
        ]
        params: dict[str, str] = {
            "param_start": start.isoformat(),
            "param_end": end.isoformat(),
            "param_limit": str(limit),
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
            if not allowed_sites:
                return []
            site_terms = []
            for index, value in enumerate(allowed_sites):
                name = f"allowed_site_{index}"
                site_terms.append(f"site_id = {{{name}:String}}")
                params[f"param_{name}"] = value
            clauses.append("(" + " OR ".join(site_terms) + ")")

        query = f"""
SELECT
 event_id, tenant_id, site_id, camera_id, timestamp, event_type, object_type,
 source, confidence, zone_id, severity, snapshot_uri, recording_start,
 recording_end, attributes_json, ingested_at
FROM {self.database}.events
WHERE {' AND '.join(clauses)}
ORDER BY timestamp DESC, event_id DESC
LIMIT 1 BY event_id
LIMIT {{limit:UInt32}}
FORMAT JSONEachRow
"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.base_url + "/", params=params, content=query)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EventSearchError("Event search store unavailable") from exc

        rows = []
        for line in response.text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                raw = row.pop("attributes_json", "{}")
                row["attributes"] = json.loads(raw) if isinstance(raw, str) else {}
                rows.append(row)
            except (ValueError, TypeError):
                continue
        return rows


event_search = EventSearchClient()
