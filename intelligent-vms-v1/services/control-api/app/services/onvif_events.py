from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from app.services.network_policy import (
    pin_site_http_xaddr,
    validate_site_http_xaddr,
)
from app.services.onvif_client import (
    OnvifError,
    _first_text,
    _http_authority,
    _local,
    _soap,
)

EVENT_NS = "http://www.onvif.org/ver10/events/wsdl"
CREATE_ACTION = f"{EVENT_NS}/EventPortType/CreatePullPointSubscriptionRequest"
PULL_ACTION = f"{EVENT_NS}/PullPointSubscription/PullMessagesRequest"
SYNC_ACTION = f"{EVENT_NS}/PullPointSubscription/SetSynchronizationPointRequest"
UNSUBSCRIBE_ACTION = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PullPointSubscription:
    """Describe a validated ONVIF PullPoint subscription endpoint.

    Parameters:
        address: Site-approved, IP-pinned subscription-manager URL.
        tenant_id: Tenant owning the camera and subscription.
        site_id: Site whose camera network may be reached.
        reference_parameters_xml: Optional WS-Addressing reference parameters.
        logical_address: Original camera-advertised WS-Addressing endpoint.
        request_host_header: Original HTTP Host authority preserved after IP pinning.
        tls_server_name: Original HTTPS SNI/certificate hostname preserved after
            IP pinning.

    Returns:
        An immutable subscription descriptor.

    Raises:
        No domain-specific exception is raised by construction; callers validate
        camera-provided addresses before creating this object.
    """

    address: str
    tenant_id: str
    site_id: str
    reference_parameters_xml: str = ""
    logical_address: str | None = None
    request_host_header: str | None = None
    tls_server_name: str | None = None


def event_service_xaddr(
    services: list[dict],
    *,
    tenant_id: str,
    site_id: str,
) -> str | None:
    """Return a site-approved ONVIF event-service URL for final request pinning.

    Args:
        services: ONVIF service descriptors discovered from the camera.
        tenant_id: Tenant owning the camera.
        site_id: Site whose camera network may be reached.

    Returns:
        The validated camera-advertised event-service URL when present. The final
        credential-bearing SOAP request re-resolves and IP-pins it atomically.

    Raises:
        TargetNotAllowed: If site policy is absent or the URL violates it.
        OSError: If a camera hostname cannot be resolved.
    """
    for item in services:
        namespace = str(item.get("namespace", "")).lower()
        if "/events/" in namespace or namespace.endswith("/events/wsdl"):
            xaddr = item.get("xaddr")
            if xaddr:
                return validate_site_http_xaddr(xaddr, tenant_id, site_id)
    return None


def _addressing_header(action: str, target: str, reference_xml: str = "") -> str:
    return (
        f"<wsa:Action>{escape(action)}</wsa:Action>"
        f"<wsa:To>{escape(target)}</wsa:To>"
        f"{reference_xml}"
    )


def _subscription_from_root(
    root,
    *,
    tenant_id: str,
    site_id: str,
) -> PullPointSubscription:
    subscription_reference = next(
        (el for el in root.iter() if _local(el.tag) == "SubscriptionReference"),
        None,
    )
    if subscription_reference is None:
        raise RuntimeError("ONVIF device returned no PullPoint subscription reference")

    address = _first_text(subscription_reference, "Address")
    if not address:
        raise RuntimeError("ONVIF device returned no PullPoint subscription address")
    logical_address = address
    address = pin_site_http_xaddr(logical_address, tenant_id, site_id)
    request_host_header, tls_server_name = _http_authority(logical_address)

    reference_xml = ""
    reference_parameters = next(
        (
            el
            for el in subscription_reference.iter()
            if _local(el.tag) == "ReferenceParameters"
        ),
        None,
    )
    if reference_parameters is not None:
        reference_xml = "".join(
            ET.tostring(child, encoding="unicode")
            for child in list(reference_parameters)
        )
    return PullPointSubscription(
        address=address,
        tenant_id=tenant_id,
        site_id=site_id,
        reference_parameters_xml=reference_xml,
        logical_address=logical_address,
        request_host_header=request_host_header,
        tls_server_name=tls_server_name,
    )


async def create_pullpoint(
    event_xaddr: str,
    username: str | None,
    password: str | None,
    *,
    tenant_id: str,
    site_id: str,
    initial_termination: str | None = None,
) -> PullPointSubscription:
    """Create and validate a PullPoint subscription on an ONVIF device.

    Args:
        event_xaddr: Validated ONVIF event-service URL.
        username: Optional camera username.
        password: Optional camera password.
        tenant_id: Tenant owning the camera.
        site_id: Site whose camera network may be reached.
        initial_termination: Optional ONVIF subscription lifetime expression.

    Returns:
        The validated PullPoint subscription descriptor.

    Raises:
        OnvifError: If the device request or SOAP response fails.
        TargetNotAllowed: If the event or returned subscription endpoint violates
            the exact site camera-network policy.
        OSError: If a camera-provided hostname cannot be resolved.
        RuntimeError: If the response omits the required subscription reference.
    """
    termination_xml = (
        f"<tev:InitialTerminationTime>{escape(initial_termination)}</tev:InitialTerminationTime>"
        if initial_termination
        else ""
    )
    root = await _soap(
        event_xaddr,
        CREATE_ACTION,
        (
            "<tev:CreatePullPointSubscription>"
            f"{termination_xml}"
            "</tev:CreatePullPointSubscription>"
        ),
        username,
        password,
        extra_header_xml=_addressing_header(CREATE_ACTION, event_xaddr),
        tenant_id=tenant_id,
        site_id=site_id,
    )
    return _subscription_from_root(
        root,
        tenant_id=tenant_id,
        site_id=site_id,
    )


async def set_synchronization_point(
    subscription: PullPointSubscription,
    username: str | None,
    password: str | None,
) -> None:
    """Request an ONVIF event synchronization point for a subscription.

    Args:
        subscription: Active PullPoint subscription.
        username: Optional camera username.
        password: Optional camera password.

    Returns:
        None after the synchronization request succeeds.

    Raises:
        OnvifError: If the camera rejects or cannot complete the request.
        TargetNotAllowed: If the stored subscription target violates site policy.
        OSError: If target validation cannot resolve a required hostname.
    """
    await _soap(
        subscription.address,
        SYNC_ACTION,
        "<tev:SetSynchronizationPoint/>",
        username,
        password,
        extra_header_xml=_addressing_header(
            SYNC_ACTION,
            subscription.logical_address or subscription.address,
            subscription.reference_parameters_xml,
        ),
        tenant_id=subscription.tenant_id,
        site_id=subscription.site_id,
        request_host_header=subscription.request_host_header,
        tls_server_name=subscription.tls_server_name,
    )


async def pull_messages(
    subscription: PullPointSubscription,
    username: str | None,
    password: str | None,
    *,
    timeout: str = "PT30S",
    message_limit: int = 32,
):
    """Pull a bounded batch of notifications from an ONVIF subscription.

    Args:
        subscription: Active PullPoint subscription.
        username: Optional camera username.
        password: Optional camera password.
        timeout: ONVIF PullMessages timeout expression.
        message_limit: Requested message batch size, clamped to 1..256.

    Returns:
        Parsed SOAP XML for the PullMessages response.

    Raises:
        OnvifError: If the camera request or response fails.
        TargetNotAllowed: If the stored subscription target violates site policy.
        OSError: If target validation cannot resolve a required hostname.
    """
    message_limit = max(1, min(256, int(message_limit)))
    return await _soap(
        subscription.address,
        PULL_ACTION,
        (
            "<tev:PullMessages>"
            f"<tev:Timeout>{timeout}</tev:Timeout>"
            f"<tev:MessageLimit>{message_limit}</tev:MessageLimit>"
            "</tev:PullMessages>"
        ),
        username,
        password,
        operation_timeout_seconds=35.0,
        extra_header_xml=_addressing_header(
            PULL_ACTION,
            subscription.logical_address or subscription.address,
            subscription.reference_parameters_xml,
        ),
        tenant_id=subscription.tenant_id,
        site_id=subscription.site_id,
        request_host_header=subscription.request_host_header,
        tls_server_name=subscription.tls_server_name,
    )


async def unsubscribe(
    subscription: PullPointSubscription,
    username: str | None,
    password: str | None,
) -> None:
    """Best-effort unsubscribe without hiding unexpected programming failures.

    Args:
        subscription: Active PullPoint subscription to release.
        username: Optional camera username.
        password: Optional camera password.

    Returns:
        None. Known ONVIF cleanup failures are logged and tolerated because the
        subscription has a finite lifetime.

    Raises:
        TargetNotAllowed: If the stored subscription target violates site policy.
        OSError: If target validation cannot resolve a required hostname.
        Exception: Unexpected non-ONVIF failures propagate instead of being
            silently swallowed.
    """
    try:
        await _soap(
            subscription.address,
            UNSUBSCRIBE_ACTION,
            "<wsnt:Unsubscribe/>",
            username,
            password,
            extra_header_xml=_addressing_header(
                UNSUBSCRIBE_ACTION,
                subscription.logical_address or subscription.address,
                subscription.reference_parameters_xml,
            ),
            tenant_id=subscription.tenant_id,
            site_id=subscription.site_id,
            request_host_header=subscription.request_host_header,
            tls_server_name=subscription.tls_server_name,
        )
    except OnvifError as exc:
        # A finite subscription will expire, so a known camera/network cleanup
        # failure must not block worker shutdown. Keep it observable.
        log.warning("onvif_unsubscribe_failed code=%s", exc.code)


def _simple_items(root) -> dict[str, str]:
    result: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) != "SimpleItem":
            continue
        name = el.attrib.get("Name") or el.attrib.get("name")
        value = el.attrib.get("Value") or el.attrib.get("value")
        if name and value is not None and len(result) < 128:
            result[str(name)[:128]] = str(value)[:1024]
    return result


def _event_type(topic: str) -> str:
    value = topic.lower()
    if "motion" in value:
        return "motion"
    if any(x in value for x in ("tamper", "globalscenechange", "imagetoo", "signal loss")):
        return "tamper"
    if any(x in value for x in ("digitalinput", "digital/input", "inputtrigger")):
        return "digital_input"
    if any(x in value for x in ("linecross", "linedetector", "tripwire")):
        return "tripwire"
    if any(x in value for x in ("intrusion", "fielddetector", "regionenter", "regionexit")):
        return "intrusion"
    if any(x in value for x in ("licenseplate", "plate", "anpr")):
        return "anpr"
    if "face" in value:
        return "face"
    if any(x in value for x in ("peoplecount", "objectcount", "counter")):
        return "object_count"
    return "onvif_event"


def _bool_value(items: dict[str, str]) -> bool | None:
    for key in ("IsMotion", "State", "LogicalState", "Alarm", "Active"):
        if key not in items:
            continue
        value = items[key].strip().lower()
        if value in {"true", "1", "active", "on", "yes"}:
            return True
        if value in {"false", "0", "inactive", "off", "no"}:
            return False
    return None


def parse_notifications(root) -> list[dict]:
    """Normalize ONVIF notification XML into bounded vendor-neutral events.

    Args:
        root: Parsed ONVIF PullMessages XML root element.

    Returns:
        A list of normalized event dictionaries.

    Raises:
        AttributeError: If the supplied object is not an XML-like element tree.
    """
    events: list[dict] = []
    for notification in root.iter():
        if _local(notification.tag) != "NotificationMessage":
            continue

        topic = ""
        message_node = None
        for el in notification.iter():
            local = _local(el.tag)
            if local == "Topic" and el.text:
                topic = el.text.strip()
            elif local == "Message" and (
                "UtcTime" in el.attrib
                or "PropertyOperation" in el.attrib
                or any(_local(child.tag) in {"Source", "Data", "Key"} for child in list(el))
            ):
                message_node = el

        source = message_node if message_node is not None else notification
        items = _simple_items(source)
        active = _bool_value(items)
        utc_text = message_node.attrib.get("UtcTime") if message_node is not None else None
        operation = (
            message_node.attrib.get("PropertyOperation") if message_node is not None else None
        )

        try:
            event_time = (
                datetime.fromisoformat(utc_text.replace("Z", "+00:00"))
                if utc_text
                else datetime.now(timezone.utc)
            )
        except ValueError:
            event_time = datetime.now(timezone.utc)

        events.append(
            {
                "timestamp": event_time,
                "event_type": _event_type(topic),
                "topic": topic[:512],
                "active": active,
                "property_operation": operation,
                "items": items,
            }
        )
    return events
