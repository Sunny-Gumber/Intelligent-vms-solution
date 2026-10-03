import re
import uuid
from datetime import datetime, timezone

from app.services.outbox import enqueue_event_once

ANALYTICS = {"human","vehicle","tripwire","intrusion","anpr","face","object_count"}
SAFE_REF = re.compile(r"^(model|provider)://[A-Za-z0-9._/@:+-]{1,480}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
SENSITIVE_CONFIG = re.compile(r"(password|passwd|secret|token|api[_-]?key|credential)", re.I)


def validate_artifact_ref(value: str) -> str:
    """Validate an AI artifact reference against the approved catalog schemes.

    Args:
        value: Candidate model:// or provider:// artifact reference.

    Returns:
        The validated reference unchanged.

    Raises:
        ValueError: If the reference is outside the approved catalog syntax.
    """
    if not SAFE_REF.fullmatch(value):
        raise ValueError("artifact_ref must be an approved model:// or provider:// catalog reference")
    return value


def validate_sha256(value: str | None) -> str | None:
    """Validate and normalize an optional SHA-256 artifact digest.

    Args:
        value: Optional hexadecimal SHA-256 string.

    Returns:
        Lowercase digest, or None when no digest is supplied.

    Raises:
        ValueError: If a supplied digest is not exactly 64 hexadecimal characters.
    """
    if value is not None and not SHA256.fullmatch(value):
        raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
    return value.lower() if value else None


def validate_provider_config(value: dict) -> dict:
    """Validate bounded provider configuration without allowing embedded secrets.

    Args:
        value: Provider configuration supplied with an AI model/provider definition.

    Returns:
        A shallow sanitized copy with bounded key/value sizes.

    Raises:
        ValueError: If the configuration is oversized or contains secret-like keys.
    """
    if len(value) > 64:
        raise ValueError("provider_config has too many keys")
    clean = {}
    for key, item in value.items():
        key = str(key)[:128]
        if SENSITIVE_CONFIG.search(key):
            raise ValueError("provider_config must not contain secrets; use deployment secret injection")
        if isinstance(item, (dict, list)) and len(repr(item)) > 4096:
            raise ValueError("provider_config value is too large")
        if isinstance(item, str) and len(item) > 1024:
            raise ValueError("provider_config string is too large")
        clean[key] = item
    return clean


def normalize_detection_event(*, camera, model, result_id: str, observed_at: datetime, detection, index: int) -> dict:
    """Normalize one AI detection into the vendor-neutral VMS event contract.

    Args:
        camera: Camera entity that produced the detection.
        model: Optional AI model entity, or None for camera-generated metadata.
        result_id: Stable upstream result identifier.
        observed_at: Detection observation time.
        detection: Validated detection payload.
        index: Detection position within the upstream result.

    Returns:
        Normalized event dictionary with a deterministic event identifier.

    Raises:
        ValueError: If the detection event type is unsupported.
    """
    event_type = detection.event_type
    if event_type not in ANALYTICS:
        raise ValueError("unsupported AI event type")
    fingerprint = f"{camera.id}\0{model.id if model else 'camera'}\0{result_id}\0{index}\0{event_type}"
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))
    object_type = detection.object_type
    if object_type is None and event_type in {"human","vehicle","face"}:
        object_type = "person" if event_type in {"human","face"} else "vehicle"
    attrs = {
        "ai_result_id": result_id[:128],
        "model_id": model.id if model else None,
        "model_name": model.name if model else "camera_metadata",
        "model_version": model.version if model else None,
        "bbox": detection.bbox,
        **(detection.attributes or {}),
    }
    attrs = {str(k)[:128]: v for k,v in list(attrs.items())[:64]}
    return {
        "event_id": event_id,
        "tenant_id": camera.tenant_id,
        "site_id": camera.site_id,
        "camera_id": camera.id,
        "timestamp": observed_at.astimezone(timezone.utc).isoformat(),
        "event_type": event_type,
        "object_type": object_type,
        "source": "ai",
        "confidence": detection.confidence,
        "zone_id": detection.zone_id,
        "severity": detection.severity,
        "snapshot_uri": None,
        "recording_start": None,
        "recording_end": None,
        "attributes": attrs,
    }


async def publish_ai_events(
    *,
    session,
    camera,
    model,
    result_id: str,
    observed_at: datetime,
    detections: list,
) -> int:
    """Normalize and enqueue unique AI detections in the current DB transaction.

    Args:
        session: Database session used by the transactional outbox.
        camera: Camera entity that produced the detections.
        model: Optional AI model entity.
        result_id: Stable upstream AI result identifier.
        observed_at: Observation timestamp shared by the result.
        detections: Validated detection objects to publish.

    Returns:
        Number of newly enqueued events; duplicate deterministic events are skipped.

    Raises:
        ValueError: If any detection cannot be normalized.
        Exception: Database/outbox failures propagate to the caller.
    """
    count = 0
    for index, detection in enumerate(detections):
        event = normalize_detection_event(
            camera=camera,
            model=model,
            result_id=result_id,
            observed_at=observed_at,
            detection=detection,
            index=index,
        )
        if await enqueue_event_once(session, event):
            count += 1
    return count
