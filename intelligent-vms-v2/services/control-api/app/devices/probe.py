"""Camera validation and capability probing used during onboarding."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from vms_contracts.enums import CameraState, StreamRole

from app.config import Settings, get_settings
from app.core.netguard import NetworkGuard, NetworkGuardError
from app.core.timeutil import utcnow
from app.db.models import Camera, CameraStream
from app.devices.base import (
    Capabilities,
    Credentials,
    DeviceEndpoint,
    DeviceError,
    DeviceInfo,
    DeviceUnauthorized,
    StreamProfile,
)
from app.devices.registry import build_adapter, resolve_adapter_name
from app.logging_setup import get_logger
from app.media.url_builder import contains_credentials, sanitize_for_display

log = get_logger("devices.probe")


@dataclass
class ProbeOutcome:
    ok: bool
    state: CameraState
    info: DeviceInfo = field(default_factory=DeviceInfo)
    capabilities: Capabilities = field(default_factory=Capabilities)
    profiles: list[StreamProfile] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None


class DeviceProber:
    def __init__(self, guard: NetworkGuard, credential_store,  # noqa: ANN001
                 settings: Settings | None = None) -> None:
        self.guard = guard
        self.credentials = credential_store
        self.settings = settings or get_settings()

    def endpoint_for(self, camera: Camera) -> DeviceEndpoint:
        dest = self.guard.resolve_pinned(camera.host, camera.port)
        return DeviceEndpoint(host=camera.host, ip=dest.ip, port=camera.port,
                              rtsp_port=camera.rtsp_port,
                              use_tls=camera.transport.startswith("rtsps"))

    def _configured_main_path(self, camera: Camera) -> str | None:
        """Operator-supplied RTSP path, so we do not guess vendor defaults."""
        for stream in camera.streams:
            if stream.role == StreamRole.MAIN.value and stream.source_url:
                url = stream.source_url
                if stream.source_url_encrypted:
                    url = self.credentials.decrypt_stream_url(camera.id, url)
                parsed = urlparse(url)
                return parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return None

    async def build(self, camera: Camera, credentials: Credentials | None = None):
        endpoint = self.endpoint_for(camera)
        creds = credentials or self.credentials.get(camera)
        name = resolve_adapter_name(adapter=camera.adapter, protocol=camera.protocol,
                                    vendor=camera.vendor)
        return build_adapter(name, endpoint, creds,
                             timeout_s=self.settings.discovery_timeout_s,
                             ffprobe_path=self.settings.ffprobe_path,
                             main_path=self._configured_main_path(camera))

    async def probe(self, camera: Camera,
                    credentials: Credentials | None = None) -> ProbeOutcome:
        try:
            adapter = await self.build(camera, credentials)
        except NetworkGuardError as exc:
            return ProbeOutcome(False, CameraState.ERROR, error=str(exc),
                                error_code=exc.code)

        try:
            info, capabilities = await adapter.probe()
            profiles = await adapter.list_profiles()
        except DeviceUnauthorized as exc:
            return ProbeOutcome(False, CameraState.UNAUTHORIZED, error=str(exc),
                                error_code="device_unauthorized")
        except DeviceError as exc:
            return ProbeOutcome(False, CameraState.ERROR, error=str(exc),
                                error_code=getattr(exc, "code", "device_error"))
        except Exception as exc:  # noqa: BLE001
            log.error("device.probe_unexpected", camera_id=camera.id, error=str(exc),
                      exc_info=True)
            return ProbeOutcome(False, CameraState.ERROR, error=str(exc)[:300],
                                error_code="probe_failed")
        finally:
            await adapter.close()

        return ProbeOutcome(True, CameraState.ONLINE, info=info,
                            capabilities=capabilities, profiles=profiles)


def apply_probe(session: Session, camera: Camera, outcome: ProbeOutcome,
                credential_store) -> None:  # noqa: ANN001
    """Persist probe results onto the camera and its streams."""
    camera.state = outcome.state.value
    camera.state_detail = outcome.error
    camera.last_seen_at = utcnow() if outcome.ok else camera.last_seen_at
    camera.consecutive_failures = 0 if outcome.ok else camera.consecutive_failures + 1

    if not outcome.ok:
        return

    camera.capabilities = outcome.capabilities.to_dict()
    camera.vendor = outcome.info.vendor or camera.vendor
    camera.model = outcome.info.model or camera.model
    camera.firmware = outcome.info.firmware or camera.firmware
    camera.serial = outcome.info.serial or camera.serial
    if outcome.info.mac:
        from app.devices.dedupe import normalize_mac

        camera.mac = normalize_mac(outcome.info.mac)

    existing = {s.role: s for s in camera.streams}
    for profile in outcome.profiles:
        role = (profile.role.value if isinstance(profile.role, StreamRole)
                else str(profile.role))
        stream = existing.get(role)
        if stream is None:
            stream = CameraStream(camera_id=camera.id, role=role)
            session.add(stream)
            camera.streams.append(stream)

        if profile.url:
            if contains_credentials(profile.url):
                stream.source_url = credential_store.encrypt_stream_url(
                    camera.id, profile.url)
                stream.source_url_encrypted = True
            else:
                stream.source_url = profile.url
                stream.source_url_encrypted = False
        stream.onvif_profile_token = profile.token
        stream.codec = profile.codec
        stream.width = profile.width
        stream.height = profile.height
        stream.fps = profile.fps
        stream.bitrate_kbps = profile.bitrate_kbps
        stream.audio_codec = profile.audio_codec
        stream.transport = camera.transport
        stream.probed_at = utcnow()
        stream.record = (role == StreamRole.MAIN.value)

    log.info("device.probed", camera_id=camera.id, vendor=camera.vendor,
             model=camera.model, profiles=len(outcome.profiles),
             main_url=sanitize_for_display(
                 next((p.url for p in outcome.profiles if p.url), None)))
