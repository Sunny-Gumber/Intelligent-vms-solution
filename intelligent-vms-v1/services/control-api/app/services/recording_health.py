from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import RecordingHealthStateEntity, RecordingPolicyEntity


def recording_gap_window_seconds(segment_duration_seconds: int) -> int:
    """Calculate the allowed interval before a continuous recording is gap-stale.

    Args:
        segment_duration_seconds: Configured MediaMTX segment duration.

    Returns:
        Gap window in seconds using the configured floor and segment multiplier.
    """
    duration = max(1, int(segment_duration_seconds))
    multiplier = max(1.0, float(settings.observability_recording_gap_segment_multiplier))
    derived = int(duration * multiplier)
    return max(int(settings.observability_recording_gap_min_seconds), derived)


def recording_gap_deadline(
    completed_at: datetime,
    segment_duration_seconds: int,
) -> datetime:
    """Calculate the recording-gap deadline after one completed segment.

    Args:
        completed_at: Completion timestamp of the accepted segment.
        segment_duration_seconds: Current recording segment duration.

    Returns:
        Timezone-aware UTC deadline after which the recording becomes gap-stale.
    """
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    return completed_at + timedelta(
        seconds=recording_gap_window_seconds(segment_duration_seconds)
    )


async def sync_recording_health_policy(
    session: AsyncSession,
    policy: RecordingPolicyEntity,
    *,
    was_enabled: bool,
    now: datetime | None = None,
) -> RecordingHealthStateEntity | None:
    """Synchronize gap tracking after a recording policy change.

    Args:
        session: Database session participating in the policy transaction.
        policy: Updated recording policy.
        was_enabled: Whether continuous recording was enabled before this update.
        now: Optional deterministic clock value.

    Returns:
        Updated recording-health row, or None when recording is disabled and no
        prior health row exists.

    Raises:
        Exception: Database/session failures propagate to the caller.
    """
    now = now or datetime.now(timezone.utc)
    health = await session.get(RecordingHealthStateEntity, policy.camera_id)

    active = bool(policy.enabled and policy.mode == "continuous")
    if not active:
        if health is not None:
            health.gap_deadline_at = None
            health.observed_at = now
        return health

    if health is None:
        health = RecordingHealthStateEntity(camera_id=policy.camera_id)
        session.add(health)

    if not was_enabled or health.last_segment_completed_at is None:
        baseline = now
    else:
        baseline = health.last_segment_completed_at
        if baseline.tzinfo is None:
            baseline = baseline.replace(tzinfo=timezone.utc)

    health.gap_deadline_at = recording_gap_deadline(
        baseline,
        policy.segment_duration_seconds,
    )
    health.observed_at = now
    return health


async def record_segment_completion(
    session: AsyncSession,
    policy: RecordingPolicyEntity,
    *,
    segment_id: str,
    completed_at: datetime,
    recording_node_id: str,
    assignment_generation: int | None,
    observed_at: datetime | None = None,
) -> RecordingHealthStateEntity:
    """Advance durable recording health after an accepted segment completion.

    Args:
        session: Database session sharing the recording-hook transaction.
        policy: Recording policy owning the completed segment.
        segment_id: Deterministic completed-segment identifier.
        completed_at: Segment completion timestamp.
        recording_node_id: Recorder node that produced the segment.
        assignment_generation: Optional distributed placement generation.
        observed_at: Optional deterministic clock value for health observation.

    Returns:
        Stored per-camera RecordingHealthStateEntity. An older completion leaves
        the newer row unchanged.

    Raises:
        Exception: Database/session failures propagate to the caller.
        RuntimeError: The health row is still missing after the insert attempt.
    """
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at or datetime.now(timezone.utc)
    # Do not assign the loaded ORM fields. A dirty object is flushed on commit
    # and would replace this predicate with the stale completion.
    values = {
        "last_segment_id": segment_id,
        "last_segment_completed_at": completed_at,
        "gap_deadline_at": recording_gap_deadline(
            completed_at,
            policy.segment_duration_seconds,
        ),
        "recording_node_id": recording_node_id,
        "assignment_generation": assignment_generation,
        "observed_at": observed_at,
    }

    health = await session.get(RecordingHealthStateEntity, policy.camera_id)
    if health is None:
        # Two first-time writers can both miss the row. ON CONFLICT keeps the
        # second insert from aborting the caller's transaction.
        await session.execute(
            _recording_health_insert(session)(RecordingHealthStateEntity)
            .values(camera_id=policy.camera_id, **values)
            .on_conflict_do_nothing(index_elements=[RecordingHealthStateEntity.camera_id])
        )
        health = await session.get(RecordingHealthStateEntity, policy.camera_id)
    if health is None:
        raise RuntimeError("recording health row missing after insert")

    await session.execute(
        update(RecordingHealthStateEntity)
        .where(
            RecordingHealthStateEntity.camera_id == policy.camera_id,
            or_(
                RecordingHealthStateEntity.last_segment_completed_at.is_(None),
                RecordingHealthStateEntity.last_segment_completed_at < completed_at,
                # An equal instant is not older, so it still advances. The
                # strict `<` clause is what rejects a stale completion.
                RecordingHealthStateEntity.last_segment_completed_at == completed_at,
            ),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    session.expire(health)
    await session.refresh(health)
    return _detach_aware_health(session, health)


def _recording_health_insert(session):
    """Return the dialect INSERT that can ignore a duplicate camera row.

    Args:
        session: Session whose bind selects PostgreSQL or SQLite.

    Returns:
        SQLAlchemy insert constructor for that dialect.

    Raises:
        RuntimeError: The bound dialect cannot express ON CONFLICT.
    """
    name = session.get_bind().dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        raise RuntimeError(f"recording health insert is not supported for dialect {name}")
    return dialect_insert


def _detach_aware_health(
    session: AsyncSession,
    health: RecordingHealthStateEntity,
) -> RecordingHealthStateEntity:
    """Detach the refreshed row and return UTC-aware completion timestamps.

    SQLite hands back naive datetimes. Attaching UTC on a persistent object
    would mark it dirty and let commit flush over the conditional update.

    Args:
        session: Session that loaded ``health``.
        health: Refreshed recording-health row.

    Returns:
        The same instance, detached, with naive timestamps marked as UTC.
    """
    session.expunge(health)
    health.last_segment_completed_at = _aware_utc(health.last_segment_completed_at)
    health.gap_deadline_at = _aware_utc(health.gap_deadline_at)
    health.observed_at = _aware_utc(health.observed_at)
    return health


def _aware_utc(value: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC without changing an aware value.

    Args:
        value: Timestamp loaded from the database, or None.

    Returns:
        The same instant with UTC attached when the driver omitted it.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)
