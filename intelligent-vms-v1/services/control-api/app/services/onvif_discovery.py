import logging
import socket
import time
import uuid
import xml.etree.ElementTree as ET

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

MULTICAST_ADDR = ("239.255.255.250", 3702)
log = logging.getLogger(__name__)

PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{message_id}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body>
  <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
 </e:Body>
</e:Envelope>"""


def _texts(root, local_name: str) -> list[str]:
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == local_name and el.text:
            out.extend(part for part in el.text.split() if part)
    return out


def parse_probe_match(payload: bytes) -> dict:
    """Parse one WS-Discovery ProbeMatch response into bounded fields.

    Args:
        payload: Raw UDP response bytes from a discovery peer.

    Returns:
        A dictionary containing endpoint reference, service addresses, scopes,
        and advertised device types.

    Raises:
        xml.etree.ElementTree.ParseError: If the response is malformed XML.
        defusedxml.common.DefusedXmlException: If unsafe XML constructs appear.
    """
    root = DET.fromstring(payload)
    eprs = _texts(root, "Address")
    return {
        "endpoint_reference": eprs[0] if eprs else None,
        "xaddrs": _texts(root, "XAddrs"),
        "scopes": _texts(root, "Scopes"),
        "types": _texts(root, "Types"),
    }


def _parse_probe_response(payload: bytes, sender: tuple[str, int]) -> dict | None:
    try:
        return parse_probe_match(payload)
    except (ET.ParseError, DefusedXmlException) as exc:
        log.debug(
            "onvif_discovery_invalid_response sender=%s error=%s",
            sender[0],
            exc.__class__.__name__,
        )
        return None


def discover(timeout_seconds: float = 2.5) -> list[dict]:
    """Discover ONVIF cameras on the local WS-Discovery multicast network.

    Args:
        timeout_seconds: Bounded discovery duration from 0.5 through 5 seconds.

    Returns:
        Deduplicated ProbeMatch dictionaries received during the window.

    Raises:
        OSError: If the UDP socket cannot be created, configured, or used.
    """
    timeout_seconds = max(0.5, min(float(timeout_seconds), 5.0))
    payload = PROBE_TEMPLATE.format(message_id=uuid.uuid4()).encode("utf-8")
    seen: dict[str, dict] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.25)
        sock.sendto(payload, MULTICAST_ADDR)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                data, sender = sock.recvfrom(65535)
            except socket.timeout:
                continue
            item = _parse_probe_response(data, sender)
            if item is None:
                continue
            key = item.get("endpoint_reference") or "|".join(item.get("xaddrs") or [])
            if key:
                seen[key] = item
    finally:
        sock.close()

    return list(seen.values())
