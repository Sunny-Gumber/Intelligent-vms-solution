"""Network destination guard — SSRF and rogue-discovery prevention.

Threat model
    A VMS accepts hostnames and URLs from (a) operators onboarding cameras and
    (b) the cameras themselves (ONVIF XAddrs, GetStreamUri responses). Both are
    untrusted. Without a guard, an attacker can make the server connect to
    169.254.169.254 (cloud metadata), to loopback admin ports, or turn camera
    discovery into an internal port scanner.

Controls
    1. Scheme allow-list.
    2. Port allow-list.
    3. Literal and resolved IPs checked against deny CIDRs.
    4. Optional strict allow-list of camera CIDRs (recommended in production).
    5. DNS-rebinding mitigation: resolve_pinned() resolves ONCE, validates the
       address, and returns it so callers connect to the validated IP.
    6. Discovery range cap so a scan cannot enumerate a /8.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

ALLOWED_SCHEMES = {"http", "https", "rtsp", "rtsps"}


class NetworkGuardError(ValueError):
    """Raised when a destination is not permitted. Maps to HTTP 400, never 500."""

    def __init__(self, message: str, code: str = "destination_not_allowed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GuardPolicy:
    allow_private: bool = True
    allow_cidrs: tuple[str, ...] = ()
    deny_cidrs: tuple[str, ...] = (
        "127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/10", "0.0.0.0/8")
    allowed_ports: tuple[int, ...] = (80, 443, 554, 322, 8000, 8080, 8443, 8554)
    max_discovery_hosts: int = 1024

    @classmethod
    def from_settings(cls, settings) -> GuardPolicy:  # noqa: ANN001
        return cls(allow_private=settings.allow_private_camera_networks,
                   allow_cidrs=tuple(settings.camera_allow_cidrs),
                   deny_cidrs=tuple(settings.camera_deny_cidrs),
                   allowed_ports=tuple(settings.camera_allowed_ports),
                   max_discovery_hosts=settings.discovery_max_hosts)


@dataclass(frozen=True)
class ValidatedDestination:
    host: str            # original hostname as supplied
    ip: str              # validated, pinned IP address
    port: int
    scheme: str | None = None


class NetworkGuard:
    def __init__(self, policy: GuardPolicy | None = None) -> None:
        self.policy = policy or GuardPolicy()
        self._deny = [ipaddress.ip_network(c, strict=False) for c in self.policy.deny_cidrs]
        self._allow = [ipaddress.ip_network(c, strict=False) for c in self.policy.allow_cidrs]

    def check_ip(self, ip_text: str):
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise NetworkGuardError(f"{ip_text!r} is not a valid IP address",
                                    "invalid_ip") from exc
        for net in self._deny:
            if ip in net:
                raise NetworkGuardError(f"destination {ip} is in denied range {net}",
                                        "denied_range")
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise NetworkGuardError(f"destination {ip} is a reserved address",
                                    "reserved_address")
        if self._allow:
            if not any(ip in net for net in self._allow):
                raise NetworkGuardError(
                    f"destination {ip} is outside the configured camera networks",
                    "outside_allow_list")
            return ip
        if ip.is_private and not self.policy.allow_private:
            raise NetworkGuardError(f"private destination {ip} is not permitted by policy",
                                    "private_denied")
        return ip

    def check_port(self, port: int) -> int:
        if not (1 <= port <= 65535):
            raise NetworkGuardError(f"port {port} out of range", "invalid_port")
        if self.policy.allowed_ports and port not in self.policy.allowed_ports:
            raise NetworkGuardError(
                f"port {port} is not in the permitted camera port list", "port_not_allowed")
        return port

    def resolve_pinned(self, host: str, port: int, *, resolver=None) -> ValidatedDestination:
        """Resolve once and validate.

        Callers MUST connect to `.ip`, not re-resolve `.host`; otherwise a
        DNS-rebinding attacker can return a safe IP for the check and a
        loopback IP for the connection.
        """
        self.check_port(port)
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False

        if literal:
            return ValidatedDestination(host=host, ip=str(self.check_ip(host)), port=port)

        addresses = (resolver or _default_resolver)(host, port)
        if not addresses:
            raise NetworkGuardError(f"could not resolve host {host!r}", "dns_failure")
        last_error: NetworkGuardError | None = None
        for addr in addresses:
            try:
                ip = self.check_ip(addr)
            except NetworkGuardError as exc:
                last_error = exc
                continue
            return ValidatedDestination(host=host, ip=str(ip), port=port)
        raise last_error or NetworkGuardError(f"no permitted address for {host!r}",
                                              "dns_failure")

    def validate_url(self, url: str, *, resolver=None) -> ValidatedDestination:
        parsed = urlparse(url)
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise NetworkGuardError(f"scheme {parsed.scheme!r} is not permitted",
                                    "scheme_not_allowed")
        if not parsed.hostname:
            raise NetworkGuardError("URL has no host component", "invalid_url")
        port = parsed.port or _default_port(parsed.scheme)
        dest = self.resolve_pinned(parsed.hostname, port, resolver=resolver)
        return ValidatedDestination(host=dest.host, ip=dest.ip, port=dest.port,
                                    scheme=parsed.scheme)

    def pin_url(self, url: str, *, resolver=None) -> tuple[str, ValidatedDestination]:
        """Return the URL rewritten to the validated IP, plus the destination."""
        dest = self.validate_url(url, resolver=resolver)
        parsed = urlparse(url)
        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        host = f"[{dest.ip}]" if ":" in dest.ip else dest.ip
        return urlunparse(parsed._replace(netloc=f"{userinfo}{host}:{dest.port}")), dest

    def expand_discovery_range(self, cidr: str) -> list[str]:
        """Expand a CIDR for unicast probing with a hard size cap."""
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise NetworkGuardError(f"{cidr!r} is not a valid network", "invalid_cidr") from exc
        if net.num_addresses > self.policy.max_discovery_hosts:
            raise NetworkGuardError(
                f"discovery range {cidr} contains {net.num_addresses} hosts; the limit is "
                f"{self.policy.max_discovery_hosts}. Narrow the range — this product will "
                "not scan large networks.", "range_too_large")
        hosts: list[str] = []
        for ip in (net.hosts() if net.num_addresses > 2 else net):
            try:
                self.check_ip(str(ip))
            except NetworkGuardError:
                continue
            hosts.append(str(ip))
        return hosts


def _default_port(scheme: str) -> int:
    return {"http": 80, "https": 443, "rtsp": 554, "rtsps": 322}.get(scheme, 80)


def _default_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen
