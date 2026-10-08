import json
import math
import re
import uuid
from datetime import datetime, timezone

from app.services.outbox import enqueue_event_once

ANALYTICS = {"human","vehicle","tripwire","intrusion","anpr","face","object_count"}
SAFE_REF = re.compile(r"^(model|provider)://[A-Za-z0-9._/@:+-]{1,480}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
# Case-insensitive substring match shared by write rejection and read redaction.
# "token" also matches max_tokens and token_limit. batch_size does not match.
SENSITIVE_CONFIG = re.compile(r"(password|passwd|secret|token|api[_-]?key|credential)", re.I)
_KEY_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_JSON_SEPARATORS = (",", ":")
MAX_PROVIDER_CONFIG_KEYS = 64
MAX_PROVIDER_CONFIG_KEY_LENGTH = 128
MAX_PROVIDER_CONFIG_STRING_LENGTH = 1024
MAX_PROVIDER_CONFIG_DEPTH = 8
MAX_PROVIDER_CONFIG_CONTAINER_JSON_CHARS = 4096
MAX_PROVIDER_CONFIG_JSON_CHARS = 65536
# Stay under the default 4300-digit integer-to-string limit so size checks
# measure JSON length instead of surfacing the interpreter conversion error.
MAX_PROVIDER_CONFIG_INTEGER_BITS = 12288


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


def _compact_json(value: object) -> str:
    return json.dumps(value, separators=_JSON_SEPARATORS, ensure_ascii=False, allow_nan=False)


def _key_path(path: str, key: str) -> str:
    if _KEY_PATH.fullmatch(key):
        return f"{path}.{key}"
    return path + "[" + _compact_json(key) + "]"


def _secret_message(path: str, key: str) -> str:
    if len(key) <= MAX_PROVIDER_CONFIG_KEY_LENGTH:
        shown = _key_path(path, key)
    else:
        shown = f"{path}.<redacted-key>"
    return f"provider_config must not contain secrets at {shown}; use deployment secret injection"


def _checked_json_size(size: int, path: str, depth: int) -> None:
    if depth > 0 and size > MAX_PROVIDER_CONFIG_CONTAINER_JSON_CHARS:
        raise ValueError(f"provider_config value is too large at {path}")
    if size > MAX_PROVIDER_CONFIG_JSON_CHARS:
        raise ValueError("provider_config exceeds the total size bound")


def _copy_scalar(value: object, path: str) -> tuple[object, int]:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        if isinstance(value, str) and len(value) > MAX_PROVIDER_CONFIG_STRING_LENGTH:
            raise ValueError(f"provider_config string is too large at {path}")
        return value, len(_compact_json(value))
    if isinstance(value, int):
        if value.bit_length() > MAX_PROVIDER_CONFIG_INTEGER_BITS:
            raise ValueError(f"provider_config value is too large at {path}")
        try:
            rendered = _compact_json(value)
        except ValueError:
            raise ValueError(f"provider_config value is too large at {path}") from None
        return value, len(rendered)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"provider_config contains a non-JSON value at {path}")
        return value, len(_compact_json(value))
    raise ValueError(f"provider_config contains a non-JSON value at {path}")


def _copy_object(value: dict, path: str, depth: int, ancestors: set[int], *, redact: bool) -> tuple[dict, int]:
    if depth > MAX_PROVIDER_CONFIG_DEPTH:
        raise ValueError(f"provider_config nesting exceeds max depth at {path}")
    identity = id(value)
    if identity in ancestors:
        raise ValueError(f"provider_config contains a cycle at {path}")
    if not redact and depth == 0 and len(value) > MAX_PROVIDER_CONFIG_KEYS:
        raise ValueError("provider_config has too many keys")
    ancestors.add(identity)
    try:
        cleaned: dict = {}
        size = 2
        first = True
        for raw_key, item in value.items():
            key = str(raw_key)
            if SENSITIVE_CONFIG.search(key):
                if redact:
                    continue
                raise ValueError(_secret_message(path, key))
            if len(key) > MAX_PROVIDER_CONFIG_KEY_LENGTH:
                if redact:
                    continue
                raise ValueError(f"provider_config key is too large at {path}")
            child_path = _key_path(path, key)
            try:
                child, child_size = _copy_provider_json(item, child_path, depth + 1, ancestors, redact=redact)
            except ValueError:
                if not redact:
                    raise
                continue
            extra = child_size if first else child_size + 1
            extra += len(_compact_json(key)) + 1
            next_size = size + extra
            try:
                _checked_json_size(next_size, path, depth)
            except ValueError:
                if not redact:
                    raise
                break
            size = next_size
            first = False
            cleaned[key] = child
        return cleaned, size
    finally:
        ancestors.discard(identity)


def _copy_array(value: list, path: str, depth: int, ancestors: set[int], *, redact: bool) -> tuple[list, int]:
    if depth > MAX_PROVIDER_CONFIG_DEPTH:
        raise ValueError(f"provider_config nesting exceeds max depth at {path}")
    identity = id(value)
    if identity in ancestors:
        raise ValueError(f"provider_config contains a cycle at {path}")
    ancestors.add(identity)
    try:
        cleaned: list = []
        size = 2
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]"
            try:
                child, child_size = _copy_provider_json(item, child_path, depth + 1, ancestors, redact=redact)
            except ValueError:
                if not redact:
                    raise
                continue
            extra = child_size if not cleaned else child_size + 1
            next_size = size + extra
            try:
                _checked_json_size(next_size, path, depth)
            except ValueError:
                if not redact:
                    raise
                break
            size = next_size
            cleaned.append(child)
        return cleaned, size
    finally:
        ancestors.discard(identity)


def _copy_provider_json(
    value: object,
    path: str,
    depth: int,
    ancestors: set[int],
    *,
    redact: bool,
) -> tuple[object, int]:
    if isinstance(value, dict):
        return _copy_object(value, path, depth, ancestors, redact=redact)
    if isinstance(value, list):
        return _copy_array(value, path, depth, ancestors, redact=redact)
    return _copy_scalar(value, path)


def validate_provider_config(value: dict) -> dict:
    """Validate bounded provider configuration without allowing embedded secrets.

    The sensitive-key pattern is the case-insensitive substring expression
    ``password|passwd|secret|token|api[_-]?key|credential``. It is applied to
    every object key at any depth, including dictionaries inside lists. Matching
    is the same check the top-level keys have always used, so ``max_tokens`` and
    ``token_limit`` are rejected because they contain ``token``. ``batch_size``
    does not match and is preserved.

    Nesting deeper than ``MAX_PROVIDER_CONFIG_DEPTH`` is rejected. Object and
    array sizes use the character length of compact JSON (``separators=(",", ":")``,
    non-ASCII kept as characters), not ``repr``. Nested containers are limited to
    ``MAX_PROVIDER_CONFIG_CONTAINER_JSON_CHARS``. The whole document is limited to
    ``MAX_PROVIDER_CONFIG_JSON_CHARS``. Cycles and values that are not JSON
    objects, arrays, strings, finite numbers, booleans, or null are rejected.

    Args:
        value: Provider configuration supplied with an AI model/provider definition.

    Returns:
        A deep copy containing only accepted JSON values.

    Raises:
        ValueError: If the configuration is not a bounded JSON object. Secret
            errors name the key path and do not include the rejected value.
    """
    if not isinstance(value, dict):
        raise ValueError("provider_config must be a JSON object")
    cleaned, _size = _copy_provider_json(value, "provider_config", 0, set(), redact=False)
    return cleaned


def redact_provider_config(value: object) -> dict:
    """Remove secret-like provider keys so public readback cannot return them.

    The same pattern and depth walk as ``validate_provider_config`` is used, so
    rows stored before nested keys were rejected still lose those keys. Cycles,
    non-JSON values, and nodes past the depth or JSON size bounds are omitted
    rather than echoed. Legitimate keys such as ``batch_size`` are kept.

    Args:
        value: Persisted provider configuration, which may predate this check.

    Returns:
        A JSON object safe to place on a policy read or list response. An
        unusable root becomes an empty object.
    """
    if not isinstance(value, dict):
        return {}
    try:
        cleaned, _size = _copy_provider_json(value, "provider_config", 0, set(), redact=True)
    except ValueError:
        return {}
    if not isinstance(cleaned, dict):
        return {}
    return cleaned


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
