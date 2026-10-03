import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class RuleSnapshot:
    """Immutable alarm-rule data required for event matching.

    Attributes:
        id: Alarm-rule identifier.
        tenant_id: Owning tenant.
        site_id: Optional site scope.
        name: Human-readable rule name.
        event_types: Allowed event types, including optional wildcard.
        severities: Optional source-event severity filter.
        camera_ids: Optional camera filter.
        alarm_severity: Severity assigned to generated alarms.
        cooldown_seconds: Deduplication window in seconds.
    """

    id: str
    tenant_id: str
    site_id: str | None
    name: str
    event_types: frozenset[str]
    severities: frozenset[str]
    camera_ids: frozenset[str]
    alarm_severity: str
    cooldown_seconds: int


def rule_matches(rule: RuleSnapshot, event: dict) -> bool:
    """Return whether a normalized event satisfies an alarm-rule snapshot.

    Args:
        rule: Immutable rule snapshot.
        event: Normalized VMS event dictionary.

    Returns:
        True when tenant/site/type/severity/camera filters match and the event is
        not an ONVIF synchronization initialization event.
    """
    if event.get("tenant_id") != rule.tenant_id:
        return False
    if rule.site_id is not None and event.get("site_id") != rule.site_id:
        return False
    event_type = str(event.get("event_type", ""))
    if "*" not in rule.event_types and event_type not in rule.event_types:
        return False
    if rule.severities and str(event.get("severity", "info")) not in rule.severities:
        return False
    if rule.camera_ids and str(event.get("camera_id", "")) not in rule.camera_ids:
        return False

    attributes = event.get("attributes") or {}
    operation = str(attributes.get("property_operation") or "").lower()
    # SetSynchronizationPoint can emit current property state. Keep it searchable
    # in ClickHouse, but do not open an operator alarm as if it were a fresh event.
    if operation == "initialized":
        return False
    return True


def event_time(event: dict) -> datetime:
    """Return a timezone-aware timestamp for an alarm candidate event.

    Args:
        event: Normalized VMS event dictionary.

    Returns:
        Parsed event timestamp, defaulting to current UTC when absent.

    Raises:
        ValueError: If a supplied timestamp string cannot be parsed.
    """
    value = event.get("timestamp")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def dedupe_key(rule: RuleSnapshot, event: dict) -> str:
    """Build a stable alarm deduplication key for one rule/event pair.

    Args:
        rule: Alarm rule defining the cooldown window.
        event: Normalized VMS event dictionary.

    Returns:
        SHA-256 key based on the event identifier or cooldown time bucket.

    Raises:
        ValueError: If the event timestamp used for a cooldown bucket is invalid.
    """
    cooldown = max(0, int(rule.cooldown_seconds))
    if cooldown:
        bucket = int(event_time(event).timestamp()) // cooldown
        marker = (
            f"{rule.id}\0{event.get('camera_id','')}\0"
            f"{event.get('event_type','')}\0{bucket}"
        )
    else:
        marker = f"{rule.id}\0{event.get('event_id','')}"
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()
