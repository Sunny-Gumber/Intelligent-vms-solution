from datetime import datetime, timedelta, timezone

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
        Updated per-camera RecordingHealthStateEntity.

    Raises:
        Exception: Database/session failures propagate to the caller.
    """
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at or datetime.now(timezone.utc)

    health = await session.get(RecordingHealthStateEntity, policy.camera_id)
    if health is None:
        health = RecordingHealthStateEntity(camera_id=policy.camera_id)
        session.add(health)

    previous = health.last_segment_completed_at
    if previous is not None:
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        if completed_at < previous:
            return health

    health.last_segment_id = segment_id
    health.last_segment_completed_at = completed_at
    health.gap_deadline_at = recording_gap_deadline(
        completed_at,
        policy.segment_duration_seconds,
    )
    health.recording_node_id = recording_node_id
    health.assignment_generation = assignment_generation
    health.observed_at = observed_at
    return health
