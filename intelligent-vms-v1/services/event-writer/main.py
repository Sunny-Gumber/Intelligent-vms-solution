import asyncio
import json
import os

import httpx
from aiokafka import AIOKafkaConsumer

KAFKA = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
EVENT_TOPIC = os.getenv("KAFKA_TOPIC_EVENTS", "vms.events.v1")
RECORDING_TOPIC = os.getenv("KAFKA_TOPIC_RECORDINGS", "vms.recordings.v1")
CH = os.getenv("CLICKHOUSE_URL", "http://clickhouse:8123").rstrip("/")
DB = os.getenv("CLICKHOUSE_DATABASE", "vms")
RECORDING_TTL_DAYS = max(30, int(os.getenv("RECORDING_METADATA_TTL_DAYS", "400")))

EVENTS_SQL = f"""
CREATE TABLE IF NOT EXISTS {DB}.events
(
  event_id String,
  tenant_id LowCardinality(String),
  site_id LowCardinality(String),
  camera_id String,
  timestamp DateTime64(3, 'UTC'),
  event_type LowCardinality(String),
  object_type LowCardinality(Nullable(String)),
  source LowCardinality(String),
  confidence Nullable(Float32),
  zone_id Nullable(String),
  severity LowCardinality(String),
  snapshot_uri Nullable(String),
  recording_start Nullable(DateTime64(3, 'UTC')),
  recording_end Nullable(DateTime64(3, 'UTC')),
  attributes_json String,
  ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(timestamp)
ORDER BY (tenant_id, site_id, camera_id, timestamp, event_type, event_id)
TTL timestamp + INTERVAL 180 DAY
"""

RECORDINGS_SQL = f"""
CREATE TABLE IF NOT EXISTS {DB}.recording_segments
(
  segment_id String,
  tenant_id LowCardinality(String),
  site_id LowCardinality(String),
  camera_id String,
  recording_node_id LowCardinality(String),
  record_stream_key String,
  segment_path String,
  segment_start DateTime64(3, 'UTC'),
  duration_seconds Float64,
  completed_at DateTime64(3, 'UTC'),
  storage_tier LowCardinality(String),
  object_uri Nullable(String),
  indexed_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(indexed_at)
PARTITION BY toYYYYMM(segment_start)
ORDER BY (tenant_id, site_id, camera_id, segment_start, segment_id)
TTL segment_start + INTERVAL {RECORDING_TTL_DAYS} DAY
"""


def event_row(event: dict) -> dict:
    """Convert a normalized VMS event into a ClickHouse event row.

    Args:
        event: Normalized VMS event dictionary.

    Returns:
        ClickHouse-compatible event row.

    Raises:
        KeyError: If required event identity fields are missing.
    """
    return {
        "event_id": event["event_id"],
        "tenant_id": event.get("tenant_id", "default"),
        "site_id": event["site_id"],
        "camera_id": event["camera_id"],
        "timestamp": event["timestamp"],
        "event_type": event["event_type"],
        "object_type": event.get("object_type"),
        "source": event["source"],
        "confidence": event.get("confidence"),
        "zone_id": event.get("zone_id"),
        "severity": event.get("severity", "info"),
        "snapshot_uri": event.get("snapshot_uri"),
        "recording_start": event.get("recording_start"),
        "recording_end": event.get("recording_end"),
        "attributes_json": json.dumps(event.get("attributes", {}), separators=(",", ":")),
    }


def recording_row(item: dict) -> dict:
    """Convert recording metadata into a ClickHouse recording-segment row.

    Args:
        item: Normalized recording-segment event.

    Returns:
        ClickHouse-compatible recording row.

    Raises:
        KeyError: If required recording metadata is missing.
    """
    return {
        "segment_id": item["segment_id"],
        "tenant_id": item["tenant_id"],
        "site_id": item["site_id"],
        "camera_id": item["camera_id"],
        "recording_node_id": item["recording_node_id"],
        "record_stream_key": item["record_stream_key"],
        "segment_path": item["segment_path"],
        "segment_start": item["segment_start"],
        "duration_seconds": item["duration_seconds"],
        "completed_at": item["completed_at"],
        "storage_tier": item.get("storage_tier", "hot"),
        "object_uri": item.get("object_uri"),
    }


async def init_clickhouse(client: httpx.AsyncClient):
    """Create the ClickHouse database and required tables if absent.

    Args:
        client: Reusable ClickHouse HTTP client.

    Returns:
        None after DDL succeeds.

    Raises:
        httpx.HTTPError: If ClickHouse rejects the DDL.
    """
    response = await client.post(f"{CH}/", params={"query": f"CREATE DATABASE IF NOT EXISTS {DB}"})
    response.raise_for_status()
    for ddl in (EVENTS_SQL, RECORDINGS_SQL):
        response = await client.post(f"{CH}/", content=ddl)
        response.raise_for_status()


async def insert_rows(client: httpx.AsyncClient, table: str, rows: list[dict]):
    """Insert one JSONEachRow batch into an approved writer table.

    Args:
        client: Reusable ClickHouse HTTP client.
        table: Destination table selected by this worker.
        rows: Rows to serialize and insert.

    Returns:
        None after insertion, or immediately for an empty batch.

    Raises:
        httpx.HTTPError: If ClickHouse rejects the insert.
        TypeError: If row serialization fails.
    """
    if not rows:
        return
    body = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n"
    query = f"INSERT INTO {DB}.{table} FORMAT JSONEachRow"
    response = await client.post(
        f"{CH}/",
        params={"query": query, "date_time_input_format": "best_effort"},
        content=body,
    )
    response.raise_for_status()


async def main():
    """Run the Kafka-to-ClickHouse event and recording writer indefinitely.

    Returns:
        None under normal operation; runs until cancelled.

    Raises:
        Exception: Consumer, serialization or ClickHouse failures propagate.
    """
    consumer = AIOKafkaConsumer(
        EVENT_TOPIC,
        RECORDING_TOPIC,
        bootstrap_servers=KAFKA,
        group_id="vms-clickhouse-writer-v2",
        enable_auto_commit=False,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        await init_clickhouse(client)
        await consumer.start()
        try:
            while True:
                records = await consumer.getmany(timeout_ms=500, max_records=2000)
                event_by_id: dict[str, dict] = {}
                recording_by_id: dict[str, dict] = {}
                for tp, messages in records.items():
                    if tp.topic == EVENT_TOPIC:
                        for message in messages:
                            row = event_row(message.value)
                            event_by_id[row["event_id"]] = row
                    elif tp.topic == RECORDING_TOPIC:
                        for message in messages:
                            row = recording_row(message.value)
                            recording_by_id[row["segment_id"]] = row
                if not event_by_id and not recording_by_id:
                    continue
                await insert_rows(client, "events", list(event_by_id.values()))
                await insert_rows(client, "recording_segments", list(recording_by_id.values()))
                await consumer.commit()
        finally:
            await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())
