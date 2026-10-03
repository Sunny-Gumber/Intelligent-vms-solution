from app.core.security import decrypt_secret
from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.services.mediamtx import MediaMTXClient, mediamtx
from app.services.rtsp import build_rtsp_uri


def make_record_stream_key(live_stream_key: str) -> str:
    """Derive the deterministic recording path key from a live stream key.

    Args:
        live_stream_key: Existing live-stream key.

    Returns:
        Recording stream key using the stable -record suffix.
    """
    return f"{live_stream_key}-record"


def recording_source(camera: CameraEntity) -> str:
    """Build the credential-bearing main-stream RTSP source for recording.

    Args:
        camera: Persisted camera entity.

    Returns:
        RTSP URI for the camera main stream.

    Raises:
        RuntimeError: If encrypted credentials cannot be decrypted.
        ValueError: If camera connection fields cannot form a valid URI.
    """
    return build_rtsp_uri(
        camera.host,
        camera.rtsp_port,
        camera.main_path,
        decrypt_secret(camera.username_enc),
        decrypt_secret(camera.password_enc),
    )


async def provision_recording(
    camera: CameraEntity,
    policy: RecordingPolicyEntity,
    *,
    client: MediaMTXClient | None = None,
    recording_node_id: str | None = None,
    assignment_generation: int | None = None,
) -> None:
    """Apply or remove continuous recording configuration for one camera.

    Args:
        camera: Camera whose main stream is recorded.
        policy: Persisted recording policy.
        client: Optional target MediaMTX client; defaults to the local media node.
        recording_node_id: Optional distributed recorder identity for hook fencing.
        assignment_generation: Optional placement generation for hook fencing.

    Returns:
        None after the target recording path is applied or removed.

    Raises:
        MediaMTXError: If the media node rejects the requested configuration.
        RuntimeError: If encrypted camera credentials cannot be decrypted.
    """
    target = client or mediamtx
    if not policy.enabled or policy.mode != "continuous":
        await target.delete_path(policy.record_stream_key)
        return
    await target.add_or_replace_recording_path(
        policy.record_stream_key,
        recording_source(camera),
        retention_days=policy.retention_days,
        part_duration_ms=policy.part_duration_ms,
        segment_duration_seconds=policy.segment_duration_seconds,
        max_part_size_mb=policy.max_part_size_mb,
        recording_node_id=recording_node_id,
        assignment_generation=assignment_generation,
    )
