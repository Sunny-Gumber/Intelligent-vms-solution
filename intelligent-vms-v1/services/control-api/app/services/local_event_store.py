from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.core.config import settings
from app.models.entities import EventHistoryEntity


def _aware_datetime(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


async def _delete_expired(session) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(1, settings.event_local_retention_days)
    )
    await session.execute(
        delete(EventHistoryEntity).where(EventHistoryEntity.timestamp < cutoff)
    )


async def persist_local_event_once(session, event: dict) -> bool:
    """Persist one normalized event in the reduced-profile event store.

    Production Postgres and local SQLite both use INSERT ... ON CONFLICT DO
    NOTHING on event_id, the same shape as enqueue_message_once. A duplicate
    returns False and leaves the caller's transaction usable.
    """
    event_id = str(event["event_id"])
    values = dict(event)
    attributes = dict(values.pop("attributes", {}) or {})
    values["timestamp"] = _aware_datetime(values.get("timestamp"))
    for field in ("recording_start", "recording_end"):
        values[field] = _aware_datetime(values.get(field))
    ingested_at = _aware_datetime(values.get("ingested_at"))
    if ingested_at is None:
        ingested_at = datetime.now(timezone.utc)
    values["event_id"] = event_id
    values["attributes_json"] = attributes
    values["ingested_at"] = ingested_at
    row = {
        "event_id": values["event_id"],
        "tenant_id": values["tenant_id"],
        "site_id": values["site_id"],
        "camera_id": values["camera_id"],
        "timestamp": values["timestamp"],
        "event_type": values["event_type"],
        "object_type": values.get("object_type"),
        "source": values["source"],
        "confidence": values.get("confidence"),
        "zone_id": values.get("zone_id"),
        "severity": values.get("severity") or "info",
        "snapshot_uri": values.get("snapshot_uri"),
        "recording_start": values.get("recording_start"),
        "recording_end": values.get("recording_end"),
        "attributes_json": values["attributes_json"],
        "ingested_at": values["ingested_at"],
    }

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        existing = await session.get(EventHistoryEntity, event_id)
        if existing is not None:
            return False
        session.add(EventHistoryEntity(**row))
        await _delete_expired(session)
        return True

    result = await session.execute(
        dialect_insert(EventHistoryEntity)
        .values(**row)
        .on_conflict_do_nothing(index_elements=[EventHistoryEntity.event_id])
    )
    inserted = bool(result.rowcount)
    if inserted:
        await _delete_expired(session)
    return inserted
