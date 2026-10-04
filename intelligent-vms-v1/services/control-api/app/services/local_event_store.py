from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.core.config import settings
from app.models.entities import EventHistoryEntity


async def persist_local_event_once(session, event: dict) -> bool:
    """Persist one normalized event in the reduced-profile event store."""
    event_id = str(event["event_id"])
    existing = await session.get(EventHistoryEntity, event_id)
    if existing is not None:
        return False
    values = dict(event)
    attributes = dict(values.pop("attributes", {}) or {})
    timestamp = values.get("timestamp")
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    values["timestamp"] = timestamp
    for field in ("recording_start", "recording_end"):
        value = values.get(field)
        if isinstance(value, str):
            values[field] = datetime.fromisoformat(value.replace("Z", "+00:00"))
    session.add(EventHistoryEntity(**values, attributes_json=attributes))
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(1, settings.event_local_retention_days)
    )
    await session.execute(
        delete(EventHistoryEntity).where(EventHistoryEntity.timestamp < cutoff)
    )
    return True
