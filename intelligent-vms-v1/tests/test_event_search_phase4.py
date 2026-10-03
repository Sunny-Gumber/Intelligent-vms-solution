import asyncio
from datetime import datetime, timezone

from app.services.event_search import EventSearchClient


def test_clickhouse_database_identifier_is_valid():
    client = EventSearchClient()
    assert client.database


def test_allowed_sites_empty_short_circuits_without_network():
    client = EventSearchClient()

    async def run():
        return await client.search(
            tenant_id="tenant-1",
            allowed_sites=[],
            site_id=None,
            camera_id=None,
            event_type=None,
            severity=None,
            start=datetime(2026, 9, 24, tzinfo=timezone.utc),
            end=datetime(2026, 9, 25, tzinfo=timezone.utc),
            limit=10,
        )

    assert asyncio.run(run()) == []
