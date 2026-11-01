"""Vendor-neutral device abstraction.

Every camera integration implements DeviceAdapter. The rest of the product only
talks to this interface, so a vendor quirk can never leak into the recorder,
the UI or the database.

CAPABILITY HONESTY RULE (GOLDEN RULE 1.3 / M2-R20)
Capability fields default to False and become True only when the device
actually demonstrated the capability during a probe. Features are gated on
these flags: if ptz_continuous is False the PTZ API returns 501 rather than
pretending to move the camera.
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

from vms_contracts.enums import StreamRole, TransportProtocol


@dataclass
class Credentials:
    username: str | None = None
    password: str | None = None

    @property
    def present(self) -> bool:
        return bool(self.username)


@dataclass
class DeviceEndpoint:
    """Where and how to reach a device. `ip` is the guard-validated address."""

    host: str
    ip: str
    port: int
    rtsp_port: int = 554
    use_tls: bool = False
    transport: TransportProtocol = TransportProtocol.RTSP_TCP


@dataclass
class Capabilities:
    onvif_device: bool = False
    onvif_media: bool = False
    onvif_media2: bool = False
    onvif_ptz: bool = False
    onvif_events: bool = False
    onvif_analytics: bool = False
    onvif_recording_g: bool = False
    profile_s: bool = False
    profile_t: bool = False
    profile_g: bool = False
    profile_m: bool = False
    ptz_continuous: bool = False
    ptz_absolute: bool = False
    ptz_relative: bool = False
    ptz_presets: bool = False
    audio: bool = False
    probed_at: dt.datetime | None = None
    probe_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.probed_at:
            data["probed_at"] = self.probed_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Capabilities:
        data = dict(data or {})
        probed = data.pop("probed_at", None)
        errors = data.pop("probe_errors", []) or []
        known = {f for f in cls.__dataclass_fields__ if f not in {"probed_at", "probe_errors"}}
        obj = cls(**{k: bool(v) for k, v in data.items() if k in known})
        obj.probe_errors = list(errors)
        if isinstance(probed, str):
            try:
                obj.probed_at = dt.datetime.fromisoformat(probed)
            except ValueError:
                obj.probed_at = None
        elif isinstance(probed, dt.datetime):
            obj.probed_at = probed
        return obj

    @property
    def any_ptz(self) -> bool:
        return self.ptz_continuous or self.ptz_absolute or self.ptz_relative or self.ptz_presets


@dataclass
class DeviceInfo:
    vendor: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    mac: str | None = None


@dataclass
class StreamProfile:
    role: StreamRole
    token: str | None = None
    name: str | None = None
    url: str | None = None  # may embed credentials -> stored encrypted
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: int | None = None
    audio_codec: str | None = None

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


@dataclass
class PtzStatus:
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    moving: bool = False


class DeviceError(RuntimeError):
    def __init__(self, message: str, code: str = "device_error") -> None:
        super().__init__(message)
        self.code = code


class DeviceUnauthorized(DeviceError):
    def __init__(self, message: str = "device rejected credentials") -> None:
        super().__init__(message, code="device_unauthorized")


class CapabilityNotSupported(DeviceError):
    def __init__(self, capability: str) -> None:
        super().__init__(f"device does not support {capability}", code="capability_not_supported")
        self.capability = capability


class DeviceAdapter(abc.ABC):
    """Interface every camera integration must implement.

    Implementations MUST never raise raw library exceptions (wrap in
    DeviceError), must bound every network operation with a timeout, and must
    treat all device responses as untrusted input.
    """

    name: str = "generic"
    protocol: str = "rtsp"

    def __init__(
        self, endpoint: DeviceEndpoint, credentials: Credentials, *, timeout_s: float = 10.0
    ) -> None:
        self.endpoint = endpoint
        self.credentials = credentials
        self.timeout_s = timeout_s

    @abc.abstractmethod
    async def probe(self) -> tuple[DeviceInfo, Capabilities]:
        """Contact the device and report what it genuinely supports."""

    @abc.abstractmethod
    async def list_profiles(self) -> list[StreamProfile]:
        """Enumerate media profiles / stream URLs."""

    async def synchronize_time(self, now: dt.datetime | None = None) -> bool:
        raise CapabilityNotSupported("time synchronization")

    async def ptz_continuous(
        self, pan: float, tilt: float, zoom: float, speed: float, duration_s: float
    ) -> None:
        raise CapabilityNotSupported("continuous PTZ")

    async def ptz_relative(self, pan: float, tilt: float, zoom: float, speed: float) -> None:
        raise CapabilityNotSupported("relative PTZ")

    async def ptz_absolute(self, pan: float, tilt: float, zoom: float, speed: float) -> None:
        raise CapabilityNotSupported("absolute PTZ")

    async def ptz_stop(self) -> None:
        raise CapabilityNotSupported("PTZ stop")

    async def ptz_status(self) -> PtzStatus:
        raise CapabilityNotSupported("PTZ status")

    async def ptz_list_presets(self) -> list[dict[str, Any]]:
        raise CapabilityNotSupported("PTZ presets")

    async def ptz_save_preset(self, name: str, token: str | None = None) -> str:
        raise CapabilityNotSupported("PTZ presets")

    async def ptz_goto_preset(self, token: str, speed: float = 0.5) -> None:
        raise CapabilityNotSupported("PTZ presets")

    async def ptz_remove_preset(self, token: str) -> None:
        raise CapabilityNotSupported("PTZ presets")

    async def snapshot_uri(self) -> str | None:
        return None

    async def close(self) -> None:
        return None


def classify_profiles(profiles: list[StreamProfile]) -> dict[StreamRole, StreamProfile]:
    """Map device profiles to main/sub/third by resolution.

    Devices label profiles inconsistently ("Profile_1", "MainStream",
    "stream2"), so ordering by pixel count is the only vendor-neutral rule
    that holds in practice.
    """
    ordered = sorted(profiles, key=lambda p: (p.pixels, p.bitrate_kbps or 0), reverse=True)
    out: dict[StreamRole, StreamProfile] = {}
    for role, profile in zip(
        (StreamRole.MAIN, StreamRole.SUB, StreamRole.THIRD), ordered, strict=False
    ):
        profile.role = role
        out[role] = profile
    return out
