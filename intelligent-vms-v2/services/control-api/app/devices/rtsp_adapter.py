"""Plain-RTSP adapter.

Supports cameras exposing RTSP but no usable ONVIF service — still the majority
of low-cost devices and many legacy encoders. Capability discovery actually
probes the stream with ffprobe, so reported codec and resolution are MEASURED,
not assumed. PTZ, events and time sync are correctly reported as unsupported.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass

from vms_contracts.enums import StreamRole

from app.core.timeutil import utcnow
from app.devices.base import (
    Capabilities,
    Credentials,
    DeviceAdapter,
    DeviceEndpoint,
    DeviceError,
    DeviceInfo,
    DeviceUnauthorized,
    StreamProfile,
)
from app.logging_setup import get_logger, redact_text

log = get_logger("devices.rtsp")

# Published vendor default paths, tried in order when no path is supplied.
# The product ships no credentials, only path shapes.
COMMON_PATHS: tuple[tuple[str, str], ...] = (
    ("/Streaming/Channels/101", "/Streaming/Channels/102"),
    ("/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"),
    ("/live/ch00_0", "/live/ch01_0"),
    ("/h264Preview_01_main", "/h264Preview_01_sub"),
    ("/stream1", "/stream2"),
    ("/video1", "/video2"),
    ("/media/video1", "/media/video2"),
    ("/11", "/12"),
)


@dataclass
class ProbeResult:
    ok: bool
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: int | None = None
    audio_codec: str | None = None
    error: str | None = None
    unauthorized: bool = False


class RtspAdapter(DeviceAdapter):
    name = "rtsp-generic"
    protocol = "rtsp"

    def __init__(self, endpoint: DeviceEndpoint, credentials: Credentials, *,
                 timeout_s: float = 10.0, main_path: str | None = None,
                 sub_path: str | None = None, ffprobe_path: str = "ffprobe") -> None:
        super().__init__(endpoint, credentials, timeout_s=timeout_s)
        self.main_path = main_path
        self.sub_path = sub_path
        self.ffprobe_path = ffprobe_path

    def build_url(self, path: str) -> str:
        from app.media.url_builder import build_rtsp_url

        return build_rtsp_url(host=self.endpoint.ip or self.endpoint.host,
                              port=self.endpoint.rtsp_port, path=path,
                              username=self.credentials.username,
                              password=self.credentials.password,
                              use_tls=self.endpoint.use_tls)

    async def probe(self) -> tuple[DeviceInfo, Capabilities]:
        caps = Capabilities(probed_at=utcnow())
        candidates = [self.main_path] if self.main_path else [p[0] for p in COMMON_PATHS]

        last_error: str | None = None
        for candidate in candidates:
            result = await probe_stream(self.build_url(candidate),
                                        ffprobe_path=self.ffprobe_path,
                                        timeout_s=self.timeout_s,
                                        transport=self.endpoint.transport.value)
            if result.unauthorized:
                raise DeviceUnauthorized(
                    "RTSP server rejected the supplied credentials (401/403)")
            if result.ok:
                self.main_path = candidate
                caps.audio = bool(result.audio_codec)
                caps.profile_s = False   # RTSP alone proves no ONVIF profile
                return DeviceInfo(), caps
            last_error = result.error

        caps.probe_errors.append(last_error or "no RTSP path responded")
        raise DeviceError(
            "Could not open any RTSP stream on this device. Supply the exact stream "
            "path from the camera's documentation.", code="rtsp_unreachable")

    async def list_profiles(self) -> list[StreamProfile]:
        profiles: list[StreamProfile] = []
        main_path = self.main_path or COMMON_PATHS[0][0]
        main = await probe_stream(self.build_url(main_path),
                                  ffprobe_path=self.ffprobe_path,
                                  timeout_s=self.timeout_s,
                                  transport=self.endpoint.transport.value)
        if main.ok:
            profiles.append(StreamProfile(
                role=StreamRole.MAIN, name="main", url=self.build_url(main_path),
                codec=main.codec, width=main.width, height=main.height, fps=main.fps,
                bitrate_kbps=main.bitrate_kbps, audio_codec=main.audio_codec))

        if self.sub_path:
            sub = await probe_stream(self.build_url(self.sub_path),
                                     ffprobe_path=self.ffprobe_path,
                                     timeout_s=self.timeout_s,
                                     transport=self.endpoint.transport.value)
            if sub.ok:
                profiles.append(StreamProfile(
                    role=StreamRole.SUB, name="sub", url=self.build_url(self.sub_path),
                    codec=sub.codec, width=sub.width, height=sub.height, fps=sub.fps,
                    bitrate_kbps=sub.bitrate_kbps, audio_codec=sub.audio_codec))

        if not profiles:
            raise DeviceError("no playable RTSP profile found", code="rtsp_unreachable")
        return profiles


async def probe_stream(url: str, *, ffprobe_path: str = "ffprobe",
                       timeout_s: float = 10.0,
                       transport: str = "rtsp_tcp") -> ProbeResult:
    """Measure a stream with ffprobe. Never raises; returns a ProbeResult.

    This is the product's definition of "camera is reachable and playable",
    used by onboarding validation, the health monitor and recorder pre-flight.
    """
    if shutil.which(ffprobe_path) is None:
        return ProbeResult(ok=False, error=f"{ffprobe_path} not found on PATH")

    args = [ffprobe_path, "-v", "error",
            "-rtsp_transport", "tcp" if transport.endswith("tcp") else "udp",
            "-rw_timeout", str(int(timeout_s * 1_000_000)),
            "-analyzeduration", "3000000", "-probesize", "2000000",
            "-print_format", "json", "-show_streams", "-show_format", url]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        return ProbeResult(ok=False, error=f"failed to start ffprobe: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(),
                                                timeout=timeout_s + 5)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ProbeResult(ok=False, error=f"probe timed out after {timeout_s:.0f}s")

    if process.returncode != 0:
        detail = redact_text(stderr.decode("utf-8", "replace").strip())[:300]
        unauthorized = any(t in detail.lower() for t in
                           ("401", "403", "unauthorized", "authorization failed"))
        return ProbeResult(ok=False, error=detail or "ffprobe failed",
                           unauthorized=unauthorized)

    try:
        data = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return ProbeResult(ok=False, error="ffprobe returned unparseable output")

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        return ProbeResult(ok=False, error="stream contains no video track")

    bitrate = video.get("bit_rate") or data.get("format", {}).get("bit_rate")
    return ProbeResult(ok=True, codec=video.get("codec_name"),
                       width=_as_int(video.get("width")),
                       height=_as_int(video.get("height")),
                       fps=_parse_fps(video.get("avg_frame_rate")
                                      or video.get("r_frame_rate")),
                       bitrate_kbps=int(int(bitrate) / 1000) if _is_int(bitrate) else None,
                       audio_codec=audio.get("codec_name") if audio else None)


def _as_int(value) -> int | None:  # noqa: ANN001
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_int(value) -> bool:  # noqa: ANN001
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _parse_fps(value: str | None) -> float | None:
    if not value or "/" not in str(value):
        return _as_float(value)
    num, _, den = str(value).partition("/")
    try:
        numerator, denominator = float(num), float(den)
    except ValueError:
        return None
    return round(numerator / denominator, 3) if denominator else None


def _as_float(value) -> float | None:  # noqa: ANN001
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
