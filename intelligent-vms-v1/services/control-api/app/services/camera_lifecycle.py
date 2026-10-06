import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decrypt_secret
from app.services.coordination import require_placement_execution_lock
from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.models.placement import PlacementAssignmentEntity
from app.services.mediamtx import mediamtx
from app.services.recording import provision_recording
from app.services.rtsp import build_rtsp_uri, source_trust_options
from app.services.stream_keys import make_role_stream_key

log = logging.getLogger(__name__)


def live_source(camera: CameraEntity) -> str:
    """Build the live RTSP source for a camera's current persisted fields.

    Args:
        camera: Camera entity containing encrypted credentials and stream paths.

    Returns:
        Credential-bearing RTSP source URI for live viewing.

    Raises:
        RuntimeError: If encrypted credentials cannot be decrypted.
        ValueError: If camera connection fields cannot form a valid RTSP URI.
    """
    return build_rtsp_uri(
        camera.host,
        camera.rtsp_port,
        camera.sub_path or camera.main_path,
        decrypt_secret(camera.username_enc),
        decrypt_secret(camera.password_enc),
        source_protocol=getattr(camera, "source_protocol", None) or "rtsp",
    )


def main_live_stream_key(camera: CameraEntity) -> str:
    """Return the live MAIN path key without changing the legacy default live key."""
    return make_role_stream_key(camera.stream_key, "main") if camera.sub_path else camera.stream_key


def available_live_roles(camera: CameraEntity) -> list[str]:
    """Return configured viewer-selectable stream roles in stable UI order."""
    roles = ["main"]
    if camera.sub_path:
        roles.append("sub")
    if camera.third_path and camera.third_stream_key:
        roles.append("third")
    return roles


def main_live_source(camera: CameraEntity) -> str:
    """Build the camera MAIN RTSP source for explicit live viewing."""
    return build_rtsp_uri(
        camera.host,
        camera.rtsp_port,
        camera.main_path,
        decrypt_secret(camera.username_enc),
        decrypt_secret(camera.password_enc),
        source_protocol=getattr(camera, "source_protocol", None) or "rtsp",
    )


def third_source(camera: CameraEntity) -> str | None:
    """Build the optional third-stream RTSP source for a managed camera.

    Args:
        camera: Camera containing an optional third stream path.

    Returns:
        Credential-bearing RTSP source URI, or None when no third path is configured.

    Raises:
        RuntimeError: If encrypted credentials cannot be decrypted.
        ValueError: If connection fields cannot form a valid RTSP URI.
    """
    if not camera.third_path:
        return None
    return build_rtsp_uri(
        camera.host,
        camera.rtsp_port,
        camera.third_path,
        decrypt_secret(camera.username_enc),
        decrypt_secret(camera.password_enc),
        source_protocol=getattr(camera, "source_protocol", None) or "rtsp",
    )


def source_snapshot(camera: CameraEntity) -> dict[str, object]:
    """Capture mutable source fields needed to restore one camera.

    Args:
        camera: Camera whose physical-source state is being changed.

    Returns:
        Dictionary containing transport fields, encrypted credentials and desired state.
    """
    return {
        "host": camera.host,
        "rtsp_port": camera.rtsp_port,
        "source_protocol": camera.source_protocol or "rtsp",
        "source_fingerprint": camera.source_fingerprint,
        "main_path": camera.main_path,
        "sub_path": camera.sub_path,
        "third_path": camera.third_path,
        "third_stream_key": camera.third_stream_key,
        "username_enc": camera.username_enc,
        "password_enc": camera.password_enc,
        "desired_state": camera.desired_state,
    }


def restore_source_snapshot(camera: CameraEntity, snapshot: dict[str, object]) -> None:
    """Restore one camera's mutable source fields from a prior snapshot.

    Args:
        camera: Camera entity to mutate.
        snapshot: Dictionary produced by :func:`source_snapshot`.

    Returns:
        None after restoring all tracked fields.
    """
    camera.host = str(snapshot["host"])
    camera.rtsp_port = int(snapshot["rtsp_port"])
    camera.source_protocol = str(snapshot["source_protocol"])
    camera.source_fingerprint = snapshot["source_fingerprint"]
    camera.main_path = str(snapshot["main_path"])
    camera.sub_path = snapshot["sub_path"] if snapshot["sub_path"] is None else str(snapshot["sub_path"])
    camera.third_path = (
        snapshot["third_path"]
        if snapshot["third_path"] is None
        else str(snapshot["third_path"])
    )
    camera.third_stream_key = (
        snapshot["third_stream_key"]
        if snapshot["third_stream_key"] is None
        else str(snapshot["third_stream_key"])
    )
    camera.username_enc = (
        snapshot["username_enc"]
        if snapshot["username_enc"] is None
        else str(snapshot["username_enc"])
    )
    camera.password_enc = (
        snapshot["password_enc"]
        if snapshot["password_enc"] is None
        else str(snapshot["password_enc"])
    )
    camera.desired_state = str(snapshot["desired_state"])


async def recording_policy(
    session: AsyncSession,
    camera_id: str,
) -> RecordingPolicyEntity | None:
    """Load the optional recording policy for one camera.

    Args:
        session: Database session used for lookup.
        camera_id: Camera identifier.

    Returns:
        Recording policy when configured, otherwise None.
    """
    return (
        await session.execute(
            select(RecordingPolicyEntity).where(
                RecordingPolicyEntity.camera_id == camera_id
            )
        )
    ).scalar_one_or_none()


async def apply_single_node_source(
    camera: CameraEntity,
    policy: RecordingPolicyEntity | None,
) -> None:
    """Apply current camera source fields to local live/recording paths.

    Args:
        camera: Camera whose source configuration should be applied.
        policy: Optional recording policy.

    Returns:
        None after live and enabled continuous-recording paths are configured.

    Raises:
        Exception: Propagates MediaMTX, URI, credential-decryption or recording errors.
    """
    await mediamtx.add_or_replace_path(camera.stream_key, live_source(camera), **source_trust_options(camera))
    explicit_main_key = make_role_stream_key(camera.stream_key, "main")
    if camera.sub_path:
        await mediamtx.add_or_replace_path(explicit_main_key, main_live_source(camera), **source_trust_options(camera))
    else:
        await mediamtx.delete_path(explicit_main_key)
    third = third_source(camera)
    if third is not None and camera.third_stream_key:
        await mediamtx.add_or_replace_path(camera.third_stream_key, third, **source_trust_options(camera))
    elif camera.third_stream_key:
        await mediamtx.delete_path(camera.third_stream_key)
    if policy is not None and policy.enabled and policy.mode == "continuous":
        await provision_recording(camera, policy)


async def restore_single_node_source(
    camera: CameraEntity,
    policy: RecordingPolicyEntity | None,
    snapshot: dict[str, object],
) -> None:
    """Best-effort restore prior local media configuration after failed mutation.

    Args:
        camera: Camera entity temporarily containing changed source state.
        policy: Optional recording policy.
        snapshot: Prior source snapshot to restore.

    Returns:
        None. Restoration failures are logged without exposing source secrets.
    """
    attempted_third_key = camera.third_stream_key
    recording_changed = _recording_source_changed(camera, snapshot)
    restore_source_snapshot(camera, snapshot)
    if attempted_third_key and attempted_third_key != camera.third_stream_key:
        try:
            await mediamtx.delete_path(attempted_third_key)
        except Exception:
            log.warning("camera_third_restore_cleanup_failed camera_id=%s", camera.id)
    try:
        await apply_single_node_source(camera, policy if recording_changed else None)
    except Exception:
        log.warning("camera_source_restore_failed camera_id=%s", camera.id)


async def mark_distributed_source_dirty(
    session: AsyncSession,
    camera: CameraEntity,
    *,
    refresh_recording: bool = True,
) -> None:
    """Force assigned media/recording paths to refresh without moving ownership.

    Args:
        session: Database session containing placement assignments.
        camera: Camera whose source credentials/transport changed.
        refresh_recording: Whether the recording source changed as well as live media.

    Returns:
        None after clearing applied-generation markers and marking pending state.

    Raises:
        PlacementExecutionBusy: If placement ownership is changing concurrently.
    """
    await require_placement_execution_lock(session)
    assignments = (
        await session.execute(
            select(PlacementAssignmentEntity).where(
                PlacementAssignmentEntity.camera_id == camera.id,
                PlacementAssignmentEntity.role.in_(
                    ["media", "recording"] if refresh_recording else ["media"]
                ),
            )
        )
    ).scalars().all()
    for assignment in assignments:
        assignment.applied_generation = None
    camera.desired_state = "pending-source-refresh"


def _recording_source_changed(camera: CameraEntity, snapshot: dict[str, object]) -> bool:
    if (camera.source_protocol or "rtsp") != snapshot["source_protocol"]:
        return True
    return any(
        getattr(camera, field) != snapshot[field]
        for field in ("host", "rtsp_port", "source_fingerprint", "main_path", "username_enc", "password_enc")
    )


async def prepare_source_mutation(
    session: AsyncSession,
    camera: CameraEntity,
) -> tuple[dict[str, object], RecordingPolicyEntity | None]:
    """Capture rollback state and load recording policy before source mutation.

    Args:
        session: Database session used for recording-policy lookup.
        camera: Camera whose source is about to change.

    Returns:
        Tuple of prior source snapshot and optional recording policy.
    """
    return source_snapshot(camera), await recording_policy(session, camera.id)


async def finalize_source_mutation(
    session: AsyncSession,
    camera: CameraEntity,
    policy: RecordingPolicyEntity | None,
    snapshot: dict[str, object],
) -> None:
    """Apply source mutation according to single-node or placement mode.

    Args:
        session: Database session tracking the camera mutation.
        camera: Camera containing the proposed new source state.
        policy: Optional existing recording policy.
        snapshot: Prior source state retained by the caller for compensation.

    Returns:
        None after external configuration is ready for database commit.

    Raises:
        Exception: If local external provisioning fails.
    """
    recording_changed = _recording_source_changed(camera, snapshot)
    if settings.placement_execution_enabled:
        await mark_distributed_source_dirty(
            session, camera, refresh_recording=recording_changed
        )
        return
    await apply_single_node_source(camera, policy if recording_changed else None)


async def commit_source_mutation(
    session: AsyncSession,
    camera: CameraEntity,
    policy: RecordingPolicyEntity | None,
    snapshot: dict[str, object],
) -> None:
    """Apply and commit a source mutation with single-node compensation.

    Args:
        session: Database session tracking source and related metadata changes.
        camera: Camera containing the proposed new source state.
        policy: Optional recording policy.
        snapshot: Prior source state used to restore local media on failure.

    Returns:
        None after external state and database commit both succeed.

    Raises:
        Exception: Propagates provisioning or commit failures after best-effort
            restoration of the prior local source configuration.
    """
    try:
        await finalize_source_mutation(session, camera, policy, snapshot)
        await session.commit()
    except Exception:
        if not settings.placement_execution_enabled:
            await restore_single_node_source(camera, policy, snapshot)
        await session.rollback()
        raise
