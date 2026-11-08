"""Camera discovery.

Two mechanisms, both bounded:

1. WS-Discovery multicast probe (239.255.255.250:3702), the standard ONVIF
   mechanism. Bounded by wall-clock timeout; results de-duplicated by UUID.

2. Targeted unicast probe of an operator-supplied CIDR. The range is expanded
   by NetworkGuard, which refuses anything larger than the configured limit.
   THIS IS NOT A GENERAL PORT SCANNER: only configured ONVIF ports are
   touched, only inside permitted ranges, and every run is audited.

VERIFICATION NOTE: multicast behaviour cannot be exercised in a build
container. Socket handling and response parsing are unit tested with captured
ProbeMatch payloads; live discovery is an external qualification item.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.core.netguard import NetworkGuard, NetworkGuardError
from app.devices.onvif_soap import NS
from app.logging_setup import get_logger

log = get_logger("devices.discovery")

WS_DISCOVERY_ADDRESS = "239.255.255.250"
WS_DISCOVERY_PORT = 3702

PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{message_id}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">
      http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""


@dataclass
class DiscoveryHit:
    address: str
    port: int = 80
    xaddrs: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    uuid: str | None = None

    @property
    def vendor(self) -> str | None:
        return (self._scope_value("hardware") or self._scope_value("mfr")
                or self._scope_value("manufacturer"))

    @property
    def model(self) -> str | None:
        return self._scope_value("name") or self._scope_value("hardware")

    def _scope_value(self, key: str) -> str | None:
        token = f"/{key}/"
        for scope in self.scopes:
            if token in scope.lower():
                return scope.rsplit("/", 1)[-1].replace("%20", " ") or None
        return None


class WsDiscovery:
    def __init__(self, guard: NetworkGuard, *, timeout_s: float = 5.0,
                 bind_address: str = "0.0.0.0") -> None:  # noqa: S104 - local UDP discovery socket
        self.guard = guard
        self.timeout_s = timeout_s
        self.bind_address = bind_address

    async def probe(self) -> list[DiscoveryHit]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._probe_blocking)

    def _probe_blocking(self) -> list[DiscoveryHit]:
        message = PROBE_TEMPLATE.format(message_id=uuid.uuid4())
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                            struct.pack("b", 2))
            sock.bind((self.bind_address, 0))
            sock.settimeout(0.5)
            sock.sendto(message.encode("utf-8"),
                        (WS_DISCOVERY_ADDRESS, WS_DISCOVERY_PORT))

            deadline = time.monotonic() + self.timeout_s
            seen: dict[str, DiscoveryHit] = {}
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(65535)
                except TimeoutError:
                    continue
                except OSError as exc:
                    log.warning("discovery.socket_error", error=str(exc))
                    break
                hit = parse_probe_match(data.decode("utf-8", "replace"), addr[0])
                if hit is None:
                    continue
                try:
                    self.guard.check_ip(hit.address)
                except NetworkGuardError:
                    continue
                seen[hit.uuid or hit.address] = hit
            return list(seen.values())
        except OSError as exc:
            log.warning("discovery.unavailable", error=str(exc))
            return []
        finally:
            sock.close()


def parse_probe_match(payload: str, source_address: str) -> DiscoveryHit | None:
    try:
        root = fromstring(payload)
    except (ET.ParseError, DefusedXmlException):
        return None
    match = root.find(".//d:ProbeMatch", NS)
    if match is None:
        return None
    xaddrs = (match.findtext("d:XAddrs", default="", namespaces=NS) or "").split()
    scopes = (match.findtext("d:Scopes", default="", namespaces=NS) or "").split()
    endpoint = match.findtext(".//wsa:Address", default="", namespaces=NS) or None

    address, port = source_address, 80
    for xaddr in xaddrs:
        host, parsed_port = _split_xaddr(xaddr)
        if host:
            address, port = host, parsed_port
            break
    return DiscoveryHit(address=address, port=port, xaddrs=xaddrs, scopes=scopes,
                        uuid=endpoint)


def _split_xaddr(xaddr: str) -> tuple[str | None, int]:
    parsed = urlparse(xaddr)
    if not parsed.hostname:
        return None, 80
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


async def probe_unicast_range(guard: NetworkGuard, cidr: str, ports: list[int], *,
                              timeout_s: float = 1.0,
                              concurrency: int = 64) -> list[DiscoveryHit]:
    """TCP-connect probe of permitted hosts on permitted ONVIF ports."""
    hosts = guard.expand_discovery_range(cidr)
    semaphore = asyncio.Semaphore(concurrency)
    hits: list[DiscoveryHit] = []

    async def check(host: str, port: int) -> None:
        async with semaphore:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=timeout_s)
            except (TimeoutError, OSError):
                return
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            hits.append(DiscoveryHit(address=host, port=port))

    await asyncio.gather(*(check(h, p) for h in hosts for p in ports))
    merged: dict[str, DiscoveryHit] = {}
    for hit in hits:
        merged.setdefault(hit.address, hit)
    return list(merged.values())
