"""ONVIF device adapter (Profile S/T oriented, capability-gated).

Implemented operations
    GetSystemDateAndTime    clock-skew compensation (unauthenticated, per spec)
    GetDeviceInformation    vendor / model / firmware / serial
    GetCapabilities         service XAddrs (ver10)
    GetServices             service XAddrs + namespaces (ver10/20)
    GetProfiles             media profiles (Media and Media2)
    GetStreamUri            RTSP URI per profile
    GetSnapshotUri          JPEG snapshot URI
    SetSystemDateAndTime    device time synchronization
    GetConfigurations       PTZ capability detection
    ContinuousMove / RelativeMove / AbsoluteMove / Stop / GetStatus
    GetPresets / SetPreset / GotoPreset / RemovePreset
    CreatePullPointSubscription / PullMessages (event ingestion)

VERIFICATION STATUS (honest): every operation above is implemented and the
response parsing is unit tested against recorded ONVIF XML fixtures. NONE of
it has been executed against physical camera hardware in this environment.
Real-device compatibility is an external qualification activity — see
docs/verification/external-qualification-plan.md.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from typing import Any

from vms_contracts.enums import StreamRole

from app.core.timeutil import utcnow
from app.devices.base import (
    Capabilities,
    CapabilityNotSupported,
    Credentials,
    DeviceAdapter,
    DeviceEndpoint,
    DeviceError,
    DeviceInfo,
    DeviceUnauthorized,
    PtzStatus,
    StreamProfile,
    classify_profiles,
)
from app.devices.onvif_soap import (
    NS,
    SoapClient,
    SoapFault,
    attr_float,
    find_all,
    find_text,
)
from app.logging_setup import get_logger

log = get_logger("devices.onvif")

DEVICE_ACTION = "http://www.onvif.org/ver10/device/wsdl/"
MEDIA_ACTION = "http://www.onvif.org/ver10/media/wsdl/"
PTZ_ACTION = "http://www.onvif.org/ver20/ptz/wsdl/"
EVENT_ACTION = "http://www.onvif.org/ver10/events/wsdl/"


class OnvifAdapter(DeviceAdapter):
    name = "onvif"
    protocol = "onvif"

    def __init__(
        self,
        endpoint: DeviceEndpoint,
        credentials: Credentials,
        *,
        timeout_s: float = 10.0,
        device_service_path: str = "/onvif/device_service",
    ) -> None:
        super().__init__(endpoint, credentials, timeout_s=timeout_s)
        scheme = "https" if endpoint.use_tls else "http"
        host = endpoint.ip or endpoint.host
        self.device_url = f"{scheme}://{host}:{endpoint.port}{device_service_path}"
        self.client = SoapClient(
            url=self.device_url,
            username=credentials.username,
            password=credentials.password,
            timeout_s=timeout_s,
        )
        self.services: dict[str, str] = {}
        self.media_url: str | None = None
        self.media2_url: str | None = None
        self.ptz_url: str | None = None
        self.events_url: str | None = None
        self._clock_synced = False

    async def _ensure_clock(self) -> None:
        if self._clock_synced:
            return
        try:
            await self.client.synchronize_clock()
        except SoapFault as exc:
            log.debug("onvif.clock_sync_failed", url=self.device_url, error=str(exc))
        self._clock_synced = True

    async def _call(self, body: str, *, action: str, url: str | None = None) -> ET.Element:
        await self._ensure_clock()
        try:
            return await self.client.call(body, action=action, url=url or self.device_url)
        except SoapFault as exc:
            if exc.is_auth_failure:
                raise DeviceUnauthorized("Camera rejected the stored ONVIF credentials") from exc
            raise DeviceError(f"ONVIF call failed: {exc}", code="onvif_fault") from exc

    # ------------------------------------------------------------------
    async def probe(self) -> tuple[DeviceInfo, Capabilities]:
        caps = Capabilities(probed_at=utcnow())
        info = DeviceInfo()

        await self._discover_services(caps)

        try:
            body = '<tds:GetDeviceInformation xmlns:tds="{}"/>'.format(NS["tds"])
            result = await self._call(body, action=DEVICE_ACTION + "GetDeviceInformation")
            info = DeviceInfo(
                vendor=find_text(result, ".//tds:Manufacturer"),
                model=find_text(result, ".//tds:Model"),
                firmware=find_text(result, ".//tds:FirmwareVersion"),
                serial=find_text(result, ".//tds:SerialNumber"),
            )
            caps.onvif_device = True
        except DeviceUnauthorized:
            raise
        except DeviceError as exc:
            caps.probe_errors.append(f"GetDeviceInformation: {exc}")

        await self._probe_ptz(caps)

        # Profile S requires media + RTSP streaming; claimed only when a stream
        # URI was actually retrieved.
        if caps.onvif_media:
            try:
                profiles = await self.list_profiles()
                caps.profile_s = any(p.url for p in profiles)
                caps.audio = any(p.audio_codec for p in profiles)
            except DeviceError as exc:
                caps.probe_errors.append(f"GetProfiles: {exc}")

        if not (caps.onvif_device or caps.onvif_media):
            raise DeviceError(
                "Device did not answer any ONVIF service call. If this is a plain RTSP "
                "camera, onboard it with protocol=rtsp instead.",
                code="onvif_unreachable",
            )
        return info, caps

    async def _discover_services(self, caps: Capabilities) -> None:
        body = (
            '<tds:GetServices xmlns:tds="{}">'
            "<tds:IncludeCapability>false</tds:IncludeCapability>"
            "</tds:GetServices>".format(NS["tds"])
        )
        try:
            result = await self._call(body, action=DEVICE_ACTION + "GetServices")
            for service in find_all(result, ".//tds:Service"):
                namespace = find_text(service, "tds:Namespace")
                xaddr = find_text(service, "tds:XAddr")
                if namespace and xaddr:
                    self.services[namespace] = self._rewrite_host(xaddr)
        except DeviceError as exc:
            caps.probe_errors.append(f"GetServices: {exc}")

        if not self.services:
            body = (
                '<tds:GetCapabilities xmlns:tds="{}">'
                "<tds:Category>All</tds:Category></tds:GetCapabilities>".format(NS["tds"])
            )
            try:
                result = await self._call(body, action=DEVICE_ACTION + "GetCapabilities")
                for tag, namespace in (
                    ("tt:Media", NS["trt"]),
                    ("tt:PTZ", NS["tptz"]),
                    ("tt:Events", NS["tev"]),
                    ("tt:Device", NS["tds"]),
                ):
                    element = result.find(f".//{tag}", NS)
                    if element is not None:
                        xaddr = find_text(element, "tt:XAddr")
                        if xaddr:
                            self.services[namespace] = self._rewrite_host(xaddr)
            except DeviceError as exc:
                caps.probe_errors.append(f"GetCapabilities: {exc}")

        self.media_url = self.services.get(NS["trt"])
        self.media2_url = self.services.get(NS["tr2"])
        self.ptz_url = self.services.get(NS["tptz"])
        self.events_url = self.services.get(NS["tev"])

        caps.onvif_media = bool(self.media_url)
        caps.onvif_media2 = bool(self.media2_url)
        caps.onvif_events = bool(self.events_url)
        caps.onvif_analytics = any("analytics" in ns.lower() for ns in self.services)
        caps.onvif_recording_g = any("recording" in ns.lower() for ns in self.services)
        caps.profile_g = caps.onvif_recording_g
        caps.profile_t = caps.onvif_media2
        caps.profile_m = caps.onvif_analytics

    def _rewrite_host(self, xaddr: str) -> str:
        """Force the address we already validated.

        Devices frequently advertise an XAddr with a stale or internal IP, and
        a malicious device could otherwise redirect us elsewhere (SSRF pivot).
        """
        from app.media.url_builder import replace_host

        return replace_host(xaddr, self.endpoint.ip or self.endpoint.host, None)

    async def _probe_ptz(self, caps: Capabilities) -> None:
        if not self.ptz_url:
            return
        body = '<tptz:GetConfigurations xmlns:tptz="{}"/>'.format(NS["tptz"])
        try:
            result = await self._call(
                body, action=PTZ_ACTION + "GetConfigurations", url=self.ptz_url
            )
        except DeviceError as exc:
            caps.probe_errors.append(f"PTZ GetConfigurations: {exc}")
            return

        configs = find_all(result, ".//tptz:PTZConfiguration") or find_all(
            result, ".//tt:PTZConfiguration"
        )
        if not configs:
            return
        caps.onvif_ptz = True
        for config in configs:
            if config.find(".//tt:DefaultContinuousPanTiltVelocitySpace", NS) is not None:
                caps.ptz_continuous = True
            if (
                config.find(".//tt:DefaultAbsolutePantTiltPositionSpace", NS) is not None
                or config.find(".//tt:DefaultAbsolutePanTiltPositionSpace", NS) is not None
            ):
                caps.ptz_absolute = True
            if config.find(".//tt:DefaultRelativePanTiltTranslationSpace", NS) is not None:
                caps.ptz_relative = True

        try:
            presets = await self.ptz_list_presets()
            caps.ptz_presets = presets is not None
        except (DeviceError, CapabilityNotSupported):
            caps.ptz_presets = False

        if not (caps.ptz_continuous or caps.ptz_absolute or caps.ptz_relative):
            caps.probe_errors.append("PTZ service present but no movement space advertised")

    # ------------------------------------------------------------------
    async def list_profiles(self) -> list[StreamProfile]:
        if not self.media_url and not self.media2_url:
            raise DeviceError("device exposes no ONVIF media service", code="onvif_no_media")

        use_media2 = bool(self.media2_url) and not self.media_url
        url = self.media2_url if use_media2 else self.media_url
        prefix, namespace = ("tr2", NS["tr2"]) if use_media2 else ("trt", NS["trt"])
        body = f'<{prefix}:GetProfiles xmlns:{prefix}="{namespace}"/>'
        result = await self._call(body, action=MEDIA_ACTION + "GetProfiles", url=url)

        profiles: list[StreamProfile] = []
        elements = find_all(result, f".//{prefix}:Profiles") or find_all(result, ".//tt:Profiles")
        for element in elements:
            token = element.get("token")
            if not token:
                continue
            encoder = element.find(".//tt:VideoEncoderConfiguration", NS)
            audio = element.find(".//tt:AudioEncoderConfiguration", NS)
            resolution = encoder.find("tt:Resolution", NS) if encoder is not None else None
            rate = encoder.find("tt:RateControl", NS) if encoder is not None else None
            profile = StreamProfile(
                role=StreamRole.MAIN,
                token=token,
                name=find_text(element, "tt:Name") or token,
                codec=find_text(encoder, "tt:Encoding") if encoder is not None else None,
                width=_to_int(
                    find_text(resolution, "tt:Width") if resolution is not None else None
                ),
                height=_to_int(
                    find_text(resolution, "tt:Height") if resolution is not None else None
                ),
                fps=_to_float(find_text(rate, "tt:FrameRateLimit") if rate is not None else None),
                bitrate_kbps=_to_int(
                    find_text(rate, "tt:BitrateLimit") if rate is not None else None
                ),
                audio_codec=find_text(audio, "tt:Encoding") if audio is not None else None,
            )
            try:
                profile.url = await self.stream_uri(token, use_media2=use_media2, url=url)
            except DeviceError as exc:
                log.debug("onvif.stream_uri_failed", token=token, error=str(exc))
            profiles.append(profile)

        if not profiles:
            raise DeviceError("device returned no media profiles", code="onvif_no_profiles")
        return list(classify_profiles([p for p in profiles if p.url] or profiles).values())

    async def stream_uri(
        self, profile_token: str, *, use_media2: bool = False, url: str | None = None
    ) -> str:
        if use_media2:
            body = (
                '<tr2:GetStreamUri xmlns:tr2="{}">'
                "<tr2:Protocol>RTSP</tr2:Protocol>"
                "<tr2:ProfileToken>{}</tr2:ProfileToken></tr2:GetStreamUri>".format(
                    NS["tr2"], profile_token
                )
            )
        else:
            body = (
                '<trt:GetStreamUri xmlns:trt="{}" xmlns:tt="{}">'
                "<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
                "</trt:StreamSetup>"
                "<trt:ProfileToken>{}</trt:ProfileToken></trt:GetStreamUri>".format(
                    NS["trt"], NS["tt"], profile_token
                )
            )
        result = await self._call(
            body, action=MEDIA_ACTION + "GetStreamUri", url=url or self.media2_url or self.media_url
        )
        uri = find_text(result, ".//tt:Uri") or find_text(result, ".//tr2:Uri")
        if not uri:
            raise DeviceError("device did not return a stream URI", code="onvif_no_stream_uri")
        return self._rewrite_host(uri)

    async def snapshot_uri(self) -> str | None:
        if not self.media_url:
            return None
        try:
            profiles = await self.list_profiles()
        except DeviceError:
            return None
        token = next((p.token for p in profiles if p.token), None)
        if not token:
            return None
        body = (
            '<trt:GetSnapshotUri xmlns:trt="{}">'
            "<trt:ProfileToken>{}</trt:ProfileToken></trt:GetSnapshotUri>".format(NS["trt"], token)
        )
        try:
            result = await self._call(
                body, action=MEDIA_ACTION + "GetSnapshotUri", url=self.media_url
            )
        except DeviceError:
            return None
        uri = find_text(result, ".//tt:Uri")
        return self._rewrite_host(uri) if uri else None

    async def synchronize_time(self, now: dt.datetime | None = None) -> bool:
        m = (now or utcnow()).astimezone(dt.UTC)
        body = (
            '<tds:SetSystemDateAndTime xmlns:tds="{}" xmlns:tt="{}">'
            "<tds:DateTimeType>Manual</tds:DateTimeType>"
            "<tds:DaylightSavings>false</tds:DaylightSavings>"
            "<tds:UTCDateTime><tt:Time><tt:Hour>{:d}</tt:Hour>"
            "<tt:Minute>{:d}</tt:Minute><tt:Second>{:d}</tt:Second></tt:Time>"
            "<tt:Date><tt:Year>{:d}</tt:Year><tt:Month>{:d}</tt:Month>"
            "<tt:Day>{:d}</tt:Day></tt:Date></tds:UTCDateTime>"
            "</tds:SetSystemDateAndTime>".format(
                NS["tds"], NS["tt"], m.hour, m.minute, m.second, m.year, m.month, m.day
            )
        )
        await self._call(body, action=DEVICE_ACTION + "SetSystemDateAndTime")
        return True

    # -- PTZ -------------------------------------------------------------
    def _require_ptz(self) -> str:
        if not self.ptz_url:
            raise CapabilityNotSupported("PTZ")
        return self.ptz_url

    async def _profile_token(self) -> str:
        token = next((p.token for p in await self.list_profiles() if p.token), None)
        if not token:
            raise DeviceError("no profile token available for PTZ", code="onvif_no_profiles")
        return token

    async def ptz_continuous(
        self, pan: float, tilt: float, zoom: float, speed: float, duration_s: float
    ) -> None:
        url = self._require_ptz()
        token = await self._profile_token()
        body = (
            '<tptz:ContinuousMove xmlns:tptz="{}" xmlns:tt="{}">'
            "<tptz:ProfileToken>{}</tptz:ProfileToken>"
            '<tptz:Velocity><tt:PanTilt x="{:.3f}" y="{:.3f}"/>'
            '<tt:Zoom x="{:.3f}"/></tptz:Velocity>'
            "<tptz:Timeout>PT{:d}S</tptz:Timeout></tptz:ContinuousMove>".format(
                NS["tptz"],
                NS["tt"],
                token,
                pan * speed,
                tilt * speed,
                zoom * speed,
                max(1, int(duration_s)),
            )
        )
        await self._call(body, action=PTZ_ACTION + "ContinuousMove", url=url)

    async def ptz_relative(self, pan: float, tilt: float, zoom: float, speed: float) -> None:
        url = self._require_ptz()
        token = await self._profile_token()
        body = (
            '<tptz:RelativeMove xmlns:tptz="{}" xmlns:tt="{}">'
            "<tptz:ProfileToken>{}</tptz:ProfileToken>"
            '<tptz:Translation><tt:PanTilt x="{:.3f}" y="{:.3f}"/>'
            '<tt:Zoom x="{:.3f}"/></tptz:Translation>'
            '<tptz:Speed><tt:PanTilt x="{:.3f}" y="{:.3f}"/><tt:Zoom x="{:.3f}"/>'
            "</tptz:Speed></tptz:RelativeMove>".format(
                NS["tptz"], NS["tt"], token, pan, tilt, zoom, speed, speed, speed
            )
        )
        await self._call(body, action=PTZ_ACTION + "RelativeMove", url=url)

    async def ptz_absolute(self, pan: float, tilt: float, zoom: float, speed: float) -> None:
        url = self._require_ptz()
        token = await self._profile_token()
        body = (
            '<tptz:AbsoluteMove xmlns:tptz="{}" xmlns:tt="{}">'
            "<tptz:ProfileToken>{}</tptz:ProfileToken>"
            '<tptz:Position><tt:PanTilt x="{:.3f}" y="{:.3f}"/>'
            '<tt:Zoom x="{:.3f}"/></tptz:Position>'
            '<tptz:Speed><tt:PanTilt x="{:.3f}" y="{:.3f}"/><tt:Zoom x="{:.3f}"/>'
            "</tptz:Speed></tptz:AbsoluteMove>".format(
                NS["tptz"], NS["tt"], token, pan, tilt, zoom, speed, speed, speed
            )
        )
        await self._call(body, action=PTZ_ACTION + "AbsoluteMove", url=url)

    async def ptz_stop(self) -> None:
        url = self._require_ptz()
        token = await self._profile_token()
        body = (
            '<tptz:Stop xmlns:tptz="{}"><tptz:ProfileToken>{}</tptz:ProfileToken>'
            "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>"
            "</tptz:Stop>".format(NS["tptz"], token)
        )
        await self._call(body, action=PTZ_ACTION + "Stop", url=url)

    async def ptz_status(self) -> PtzStatus:
        url = self._require_ptz()
        token = await self._profile_token()
        body = (
            '<tptz:GetStatus xmlns:tptz="{}">'
            "<tptz:ProfileToken>{}</tptz:ProfileToken></tptz:GetStatus>".format(NS["tptz"], token)
        )
        result = await self._call(body, action=PTZ_ACTION + "GetStatus", url=url)
        pan_tilt = result.find(".//tt:Position/tt:PanTilt", NS)
        zoom = result.find(".//tt:Position/tt:Zoom", NS)
        moving = (find_text(result, ".//tt:MoveStatus/tt:PanTilt") or "").upper() == "MOVING"
        return PtzStatus(
            pan=attr_float(pan_tilt, "x"),
            tilt=attr_float(pan_tilt, "y"),
            zoom=attr_float(zoom, "x"),
            moving=moving,
        )

    async def ptz_list_presets(self) -> list[dict[str, Any]]:
        url = self._require_ptz()
        token = await self._profile_token()
        body = (
            '<tptz:GetPresets xmlns:tptz="{}">'
            "<tptz:ProfileToken>{}</tptz:ProfileToken></tptz:GetPresets>".format(NS["tptz"], token)
        )
        result = await self._call(body, action=PTZ_ACTION + "GetPresets", url=url)
        return [
            {"token": e.get("token"), "name": find_text(e, "tt:Name") or e.get("token")}
            for e in find_all(result, ".//tptz:Preset") + find_all(result, ".//tt:Preset")
        ]

    async def ptz_save_preset(self, name: str, token: str | None = None) -> str:
        url = self._require_ptz()
        profile = await self._profile_token()
        token_xml = (f"<tptz:PresetToken>{token}</tptz:PresetToken>") if token else ""
        body = (
            '<tptz:SetPreset xmlns:tptz="{}">'
            "<tptz:ProfileToken>{}</tptz:ProfileToken>"
            "<tptz:PresetName>{}</tptz:PresetName>{}</tptz:SetPreset>".format(
                NS["tptz"], profile, name, token_xml
            )
        )
        result = await self._call(body, action=PTZ_ACTION + "SetPreset", url=url)
        return find_text(result, ".//tptz:PresetToken") or token or name

    async def ptz_goto_preset(self, token: str, speed: float = 0.5) -> None:
        url = self._require_ptz()
        profile = await self._profile_token()
        body = (
            '<tptz:GotoPreset xmlns:tptz="{}" xmlns:tt="{}">'
            "<tptz:ProfileToken>{}</tptz:ProfileToken>"
            "<tptz:PresetToken>{}</tptz:PresetToken>"
            '<tptz:Speed><tt:PanTilt x="{:.3f}" y="{:.3f}"/><tt:Zoom x="{:.3f}"/>'
            "</tptz:Speed></tptz:GotoPreset>".format(
                NS["tptz"], NS["tt"], profile, token, speed, speed, speed
            )
        )
        await self._call(body, action=PTZ_ACTION + "GotoPreset", url=url)

    async def ptz_remove_preset(self, token: str) -> None:
        url = self._require_ptz()
        profile = await self._profile_token()
        body = (
            '<tptz:RemovePreset xmlns:tptz="{}">'
            "<tptz:ProfileToken>{}</tptz:ProfileToken>"
            "<tptz:PresetToken>{}</tptz:PresetToken></tptz:RemovePreset>".format(
                NS["tptz"], profile, token
            )
        )
        await self._call(body, action=PTZ_ACTION + "RemovePreset", url=url)

    # -- Events ----------------------------------------------------------
    async def create_pullpoint(self, initial_timeout: str = "PT60S") -> str:
        if not self.events_url:
            raise CapabilityNotSupported("ONVIF events")
        body = (
            '<tev:CreatePullPointSubscription xmlns:tev="{}">'
            "<tev:InitialTerminationTime>{}</tev:InitialTerminationTime>"
            "</tev:CreatePullPointSubscription>".format(NS["tev"], initial_timeout)
        )
        result = await self._call(
            body, action=EVENT_ACTION + "CreatePullPointSubscription", url=self.events_url
        )
        address = find_text(result, ".//wsa:Address")
        if not address:
            raise DeviceError(
                "device did not return a PullPoint address", code="onvif_no_pullpoint"
            )
        return self._rewrite_host(address)

    async def pull_messages(
        self,
        pullpoint_url: str,
        *,
        timeout: str = "PT10S",  # noqa: ASYNC109 - ONVIF duration string, not a Python timeout
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        body = (
            '<tev:PullMessages xmlns:tev="{}"><tev:Timeout>{}</tev:Timeout>'
            "<tev:MessageLimit>{:d}</tev:MessageLimit></tev:PullMessages>".format(
                NS["tev"], timeout, limit
            )
        )
        result = await self._call(body, action=EVENT_ACTION + "PullMessages", url=pullpoint_url)
        return parse_notification_messages(result)


def parse_notification_messages(body: ET.Element) -> list[dict[str, Any]]:
    """Translate ONVIF NotificationMessage elements into normalized dicts."""
    messages: list[dict[str, Any]] = []
    for notification in find_all(body, ".//wsnt:NotificationMessage"):
        topic = find_text(notification, "wsnt:Topic") or ""
        message = notification.find(".//tt:Message", NS)
        if message is None:
            message = notification.find(".//wsnt:Message", NS)
        data: dict[str, Any] = {"topic": topic.strip(), "source": {}, "data": {}}
        if message is not None:
            data["utc_time"] = message.get("UtcTime")
            data["property_operation"] = message.get("PropertyOperation")
            for group, key in (
                ("tt:Source/tt:SimpleItem", "source"),
                ("tt:Data/tt:SimpleItem", "data"),
            ):
                for item in message.findall(group, NS):
                    name = item.get("Name")
                    if name:
                        data[key][name] = item.get("Value")
        messages.append(data)
    return messages


def _to_int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
