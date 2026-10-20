"""Minimal ONVIF SOAP transport.

Why hand-rolled instead of a WSDL stack (ADR-0007)
    Generated WSDL clients require shipping the ONVIF WSDL/XSD set and some
    stacks fetch schema imports over the network. For an appliance that must
    install and run fully offline, a focused SOAP client over httpx with
    explicit XML parsing is smaller, auditable, and has no hidden network
    dependency.

Security properties
    * WS-Security UsernameToken with password DIGEST (nonce + UTC created),
      which Profile S devices expect; the plaintext password never crosses
      the wire.
    * Device clock skew is compensated via GetSystemDateAndTime, because many
      cameras ship with wrong clocks and would otherwise reject every digest.
    * ElementTree does not resolve external entities; bodies are size-capped
      by the caller.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.timeutil import utcnow
from app.logging_setup import get_logger

log = get_logger("devices.onvif.soap")

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
}

PASSWORD_DIGEST_TYPE = ("http://docs.oasis-open.org/wss/2004/01/"
                        "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
BASE64_TYPE = ("http://docs.oasis-open.org/wss/2004/01/"
               "oasis-200401-wss-soap-message-security-1.0#Base64Binary")


class SoapFault(RuntimeError):
    def __init__(self, code: str, reason: str, http_status: int | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.http_status = http_status

    @property
    def is_auth_failure(self) -> bool:
        haystack = f"{self.code} {self.reason}".lower()
        return any(t in haystack for t in ("notauthorized", "unauthorized",
                                           "authentication", "sender not authorized",
                                           "failedauthentication"))


@dataclass
class SoapClient:
    url: str
    username: str | None = None
    password: str | None = None
    timeout_s: float = 10.0
    verify_tls: bool = False          # cameras almost always use self-signed certs
    clock_offset_s: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    def _security_header(self) -> str:
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = (utcnow() + dt.timedelta(seconds=self.clock_offset_s)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(
            nonce + created.encode("utf-8")
            + (self.password or "").encode("utf-8")).digest()).decode("ascii")
        return (f'<wsse:Security xmlns:wsse="{NS["wsse"]}" xmlns:wsu="{NS["wsu"]}" '
                f's:mustUnderstand="1"><wsse:UsernameToken>'
                f"<wsse:Username>{_escape(self.username)}</wsse:Username>"
                f'<wsse:Password Type="{PASSWORD_DIGEST_TYPE}">{digest}</wsse:Password>'
                f'<wsse:Nonce EncodingType="{BASE64_TYPE}">'
                f"{base64.b64encode(nonce).decode('ascii')}</wsse:Nonce>"
                f"<wsu:Created>{created}</wsu:Created>"
                f"</wsse:UsernameToken></wsse:Security>")

    def envelope(self, body: str, *, authenticated: bool = True,
                 extra_header: str = "") -> str:
        header = (self._security_header() if authenticated else "") + extra_header
        header_xml = f"<s:Header>{header}</s:Header>" if header else ""
        namespaces = " ".join(f'xmlns:{p}="{u}"' for p, u in NS.items() if p != "s")
        return (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<s:Envelope xmlns:s="{NS["s"]}" {namespaces}>'
                f"{header_xml}<s:Body>{body}</s:Body></s:Envelope>")

    async def call(self, body: str, *, action: str | None = None,
                   authenticated: bool = True, url: str | None = None,
                   extra_header: str = "") -> ET.Element:
        payload = self.envelope(body, authenticated=authenticated,
                                extra_header=extra_header)
        content_type = "application/soap+xml; charset=utf-8"
        if action:
            content_type += f'; action="{action}"'

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s),
                                     verify=self.verify_tls,
                                     follow_redirects=False) as client:
            try:
                response = await client.post(
                    url or self.url, content=payload.encode("utf-8"),
                    headers={"Content-Type": content_type, **self.headers})
            except httpx.HTTPError as exc:
                raise SoapFault("transport", str(exc)[:300]) from exc

        if response.status_code == 401:
            raise SoapFault("NotAuthorized", "device returned HTTP 401", 401)
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise SoapFault("parse_error",
                            f"device returned non-XML response ({response.status_code})",
                            response.status_code) from exc

        fault = root.find(".//s:Fault", NS)
        if fault is not None:
            code = (_text(fault.find(".//s:Subcode/s:Value", NS))
                    or _text(fault.find(".//s:Code/s:Value", NS)) or "Fault")
            reason = _text(fault.find(".//s:Reason/s:Text", NS)) or "unspecified SOAP fault"
            raise SoapFault(code, reason, response.status_code)

        if response.status_code >= 400:
            raise SoapFault("http_error", f"HTTP {response.status_code}",
                            response.status_code)

        body_el = root.find("s:Body", NS)
        return body_el if body_el is not None else root

    async def synchronize_clock(self) -> float:
        """Measure device clock offset so digest authentication succeeds."""
        result = await self.call(
            f'<tds:GetSystemDateAndTime xmlns:tds="{NS["tds"]}"/>', authenticated=False,
            action="http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime")
        utc = result.find(".//tt:UTCDateTime", NS)
        if utc is None:
            return 0.0
        try:
            date_el, time_el = utc.find("tt:Date", NS), utc.find("tt:Time", NS)
            device_time = dt.datetime(
                int(_text(date_el.find("tt:Year", NS))),
                int(_text(date_el.find("tt:Month", NS))),
                int(_text(date_el.find("tt:Day", NS))),
                int(_text(time_el.find("tt:Hour", NS))),
                int(_text(time_el.find("tt:Minute", NS))),
                int(_text(time_el.find("tt:Second", NS))), tzinfo=dt.timezone.utc)
        except (AttributeError, TypeError, ValueError):
            return 0.0
        self.clock_offset_s = (device_time - utcnow()).total_seconds()
        if abs(self.clock_offset_s) > 60:
            log.info("onvif.clock_skew_compensated", url=self.url,
                     offset_s=round(self.clock_offset_s, 1))
        return self.clock_offset_s


def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    return (element.text or "").strip() or None


def find_text(root: ET.Element, path: str) -> str | None:
    return _text(root.find(path, NS))


def find_all(root: ET.Element, path: str) -> list[ET.Element]:
    return root.findall(path, NS)


def attr_float(element: ET.Element | None, name: str) -> float | None:
    if element is None:
        return None
    value = element.get(name)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_capabilities(body: ET.Element) -> dict[str, Any]:
    """Extract service availability from GetCapabilities or GetServices."""
    out: dict[str, Any] = {"services": {}}
    for service in find_all(body, ".//tds:Service"):
        namespace = find_text(service, "tds:Namespace")
        xaddr = find_text(service, "tds:XAddr")
        if namespace and xaddr:
            out["services"][namespace] = xaddr
    for tag, key in (("tt:Device", "device"), ("tt:Media", "media"), ("tt:PTZ", "ptz"),
                     ("tt:Events", "events"), ("tt:Analytics", "analytics"),
                     ("tt:Recording", "recording")):
        element = body.find(f".//{tag}", NS)
        if element is not None:
            xaddr = find_text(element, "tt:XAddr")
            if xaddr:
                out[key] = xaddr
    return out
