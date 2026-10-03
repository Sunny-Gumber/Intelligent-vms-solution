import re
import uuid


def slug(value: str) -> str:
    """Normalize arbitrary text into a bounded stream-key slug.

    Args:
        value: Source text to normalize.

    Returns:
        Lowercase alphanumeric/hyphen slug limited to 48 characters.
    """
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:48] or "camera"


def make_stream_key(site_id: str, name: str) -> str:
    """Create a collision-resistant camera stream key.

    Args:
        site_id: Site identifier used as the first slug component.
        name: Camera/display name used as the second slug component.

    Returns:
        Stream key containing normalized site/name plus an eight-character UUID suffix.
    """
    return f"{slug(site_id)}-{slug(name)}-{uuid.uuid4().hex[:8]}"



def make_role_stream_key(live_stream_key: str, role: str) -> str:
    """Derive a stable auxiliary media-path key from the primary live key.

    Args:
        live_stream_key: Stable primary camera stream key.
        role: Auxiliary role name, currently expected to be third.

    Returns:
        Bounded role-specific MediaMTX path key.

    Raises:
        ValueError: If the derived value exceeds the storage bound.
    """
    role_slug = slug(role)
    value = f"{live_stream_key}-{role_slug}"
    if len(value) > 200:
        raise ValueError("derived role stream key exceeds maximum length")
    return value
