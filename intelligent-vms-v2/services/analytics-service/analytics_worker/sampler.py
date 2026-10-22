"""Frame sampling.

Decoding every frame of every camera is the fastest way to melt a VMS server.
The sampler pulls frames from a camera's SUB stream at a configured low rate
(default 2 fps) using FFmpeg's rawvideo output, so analytics cost is bounded
and predictable regardless of camera frame rate.

It is a separate process from recording (ADR-0010): if this dies or is starved
of CPU, recording is unaffected.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from typing import Self

import numpy as np
from app.logging_setup import get_logger, redact_text

log = get_logger("analytics.sampler")


@dataclass
class SamplerConfig:
    source_url: str
    width: int = 640
    height: int = 360
    fps: float = 2.0
    transport: str = "tcp"
    ffmpeg_path: str = "ffmpeg"
    rw_timeout_us: int = 15_000_000
    is_file_source: bool = False
    extra_input_args: tuple[str, ...] = ()

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 3


class FrameSampler:
    """Yields decoded BGR frames as numpy arrays at a bounded rate."""

    def __init__(self, config: SamplerConfig, *, name: str = "sampler") -> None:
        self.config = config
        self.name = name
        self.process: asyncio.subprocess.Process | None = None
        self.frames_read = 0
        self._stop = asyncio.Event()

    def build_command(self) -> list[str]:
        cfg = self.config
        args = [cfg.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if not cfg.is_file_source:
            args += ["-rtsp_transport", cfg.transport,
                     "-rw_timeout", str(cfg.rw_timeout_us)]
        args += list(cfg.extra_input_args)
        args += [
            "-i", cfg.source_url,
            "-vf", f"fps={cfg.fps},scale={cfg.width}:{cfg.height}",
            "-pix_fmt", "bgr24",
            "-f", "rawvideo",
            "-an", "-sn",
            "pipe:1",
        ]
        return args

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.stop()

    async def start(self) -> None:
        if shutil.which(self.config.ffmpeg_path) is None:
            raise FileNotFoundError(f"{self.config.ffmpeg_path} not found on PATH")
        command = self.build_command()
        log.info("sampler.starting", name=self.name,
                 command=redact_text(" ".join(command))[:400])
        self.process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def frames(self):
        """Async generator of (height, width, 3) uint8 BGR frames."""
        if self.process is None:
            await self.start()
        assert self.process is not None and self.process.stdout is not None

        cfg = self.config
        while not self._stop.is_set():
            try:
                raw = await self.process.stdout.readexactly(cfg.frame_bytes)
            except asyncio.IncompleteReadError:
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("sampler.read_failed", name=self.name, error=str(exc))
                return
            self.frames_read += 1
            yield np.frombuffer(raw, dtype=np.uint8).reshape(cfg.height, cfg.width, 3)

    async def stop(self) -> None:
        self._stop.set()
        if self.process is None or self.process.returncode is not None:
            return
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError):
            try:
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                pass
