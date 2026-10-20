"""FFmpeg segment recorder.

How a segment is produced, and why
----------------------------------
FFmpeg is invoked with the `segment` muxer writing into a per-session SPOOL
directory plus a CSV segment list. FFmpeg appends one CSV line
(`file,start_offset,end_offset`) the moment a segment is closed, which is our
authoritative "this file is complete" signal.

The supervisor then moves the finished file from the spool to its dated final
path with `os.replace` — an atomic rename inside the same volume.
Consequences:

  * A reader never sees a partially written file at its final path.
  * A crash leaves files in the spool, which startup recovery reconciles.
  * Timestamps come from FFmpeg's own segment offsets added to the recording
    start instant, so they survive clock jitter on the camera.

Stream copy (`-c copy`) is the default: no transcoding, no quality loss,
minimal CPU. Transcoding happens only when a recording profile requests it.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.core.timeutil import utcnow
from app.logging_setup import get_logger, redact_text

log = get_logger("recorder.segmenter")

SegmentCallback = Callable[["CompletedSegment"], Awaitable[None]]


@dataclass(frozen=True)
class CompletedSegment:
    path: Path
    started_at: dt.datetime
    ended_at: dt.datetime
    size_bytes: int
    index: int

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


@dataclass
class SegmenterConfig:
    source_url: str
    spool_dir: Path
    segment_seconds: int = 300
    transport: str = "tcp"
    ffmpeg_path: str = "ffmpeg"
    record_audio: bool = True
    transcode: bool = False
    video_codec: str = "libx264"
    extension: str = "mp4"
    rw_timeout_us: int = 15_000_000
    extra_input_args: list[str] = field(default_factory=list)
    is_file_source: bool = False     # test/bench sources (file, lavfi)


class SegmentRecorder:
    """Owns one FFmpeg process recording one stream."""

    def __init__(self, config: SegmenterConfig, on_segment: SegmentCallback, *,
                 name: str = "recorder") -> None:
        self.config = config
        self.on_segment = on_segment
        self.name = name
        self.process: asyncio.subprocess.Process | None = None
        self.started_at: dt.datetime | None = None
        self.last_segment_at: dt.datetime | None = None
        self.segments_written = 0
        self.bytes_written = 0
        self._stderr_tail: list[str] = []
        self._seen_rows = 0
        self._stop = asyncio.Event()

    def build_command(self, list_path: Path, pattern: Path) -> list[str]:
        cfg = self.config
        args: list[str] = [cfg.ffmpeg_path, "-hide_banner", "-loglevel", "warning",
                           "-nostdin"]

        if not cfg.is_file_source:
            args += ["-rtsp_transport", cfg.transport,
                     "-rw_timeout", str(cfg.rw_timeout_us),
                     "-use_wallclock_as_timestamps", "1"]
        args += cfg.extra_input_args
        args += ["-i", cfg.source_url]

        args += (["-c:v", cfg.video_codec, "-preset", "veryfast", "-g", "50"]
                 if cfg.transcode else ["-c:v", "copy"])
        args += (["-c:a", "aac" if cfg.transcode else "copy"]
                 if cfg.record_audio else ["-an"])

        args += [
            "-f", "segment",
            "-segment_time", str(cfg.segment_seconds),
            "-segment_format", "mp4",
            "-segment_format_options",
            "movflags=+faststart+frag_keyframe+empty_moov",
            "-reset_timestamps", "1",
            "-segment_list", str(list_path),
            "-segment_list_type", "csv",
            "-segment_list_flags", "+live",
            str(pattern),
        ]
        return args

    async def run(self) -> int:
        """Run until the process exits or stop() is called. Returns exit code."""
        cfg = self.config
        if shutil.which(cfg.ffmpeg_path) is None:
            raise FileNotFoundError(f"{cfg.ffmpeg_path} not found on PATH")

        cfg.spool_dir.mkdir(parents=True, exist_ok=True)
        list_path = cfg.spool_dir / "segments.csv"
        if list_path.exists():
            list_path.unlink()
        pattern = cfg.spool_dir / f"seg_%05d.{cfg.extension}"
        self._seen_rows = 0

        command = self.build_command(list_path, pattern)
        log.info("recorder.starting", name=self.name,
                 command=redact_text(" ".join(command))[:500])

        self.started_at = utcnow()
        self.process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)

        watcher = asyncio.create_task(self._watch_segment_list(list_path))
        stderr_reader = asyncio.create_task(self._drain_stderr())

        try:
            returncode = await self.process.wait()
        finally:
            await asyncio.sleep(0.3)        # let the watcher see the final line
            watcher.cancel()
            stderr_reader.cancel()
            await asyncio.gather(watcher, stderr_reader, return_exceptions=True)
            await self._scan_segment_list(list_path)   # final sweep

        log.info("recorder.exited", name=self.name, returncode=returncode,
                 segments=self.segments_written)
        return returncode

    async def _watch_segment_list(self, list_path: Path) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            await self._scan_segment_list(list_path)

    async def _scan_segment_list(self, list_path: Path) -> None:
        if not list_path.is_file() or self.started_at is None:
            return
        try:
            with open(list_path, newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        except OSError:
            return

        for index in range(self._seen_rows, len(rows)):
            row = rows[index]
            if len(row) < 3:
                continue
            filename, start_offset, end_offset = row[0], row[1], row[2]
            path = self.config.spool_dir / Path(filename).name
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
                start = self.started_at + dt.timedelta(seconds=float(start_offset))
                end = self.started_at + dt.timedelta(seconds=float(end_offset))
            except (OSError, ValueError):
                continue
            self._seen_rows = index + 1
            self.segments_written += 1
            self.bytes_written += size
            self.last_segment_at = utcnow()
            try:
                await self.on_segment(CompletedSegment(path=path, started_at=start,
                                                       ended_at=end, size_bytes=size,
                                                       index=index))
            except Exception as exc:  # noqa: BLE001
                log.error("recorder.segment_callback_failed", name=self.name,
                          error=str(exc), exc_info=True)
        self._seen_rows = len(rows)

    async def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            text = redact_text(line.decode("utf-8", "replace").strip())
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-20]
                log.debug("recorder.ffmpeg", name=self.name, message=text[:300])

    @property
    def stderr_tail(self) -> str:
        return " | ".join(self._stderr_tail[-5:])

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def is_stalled(self, stall_timeout_s: float) -> bool:
        """No segment produced for longer than expected -> the stream is dead.

        This catches the nasty failure where FFmpeg keeps the socket open but
        receives no frames, which otherwise looks healthy forever.
        """
        if not self.is_running or self.started_at is None:
            return False
        reference = self.last_segment_at or self.started_at
        allowed = self.config.segment_seconds + stall_timeout_s
        return (utcnow() - reference).total_seconds() > allowed

    async def stop(self, timeout_s: float = 10.0) -> None:
        """Ask FFmpeg to finish cleanly so the last segment is not truncated."""
        self._stop.set()
        if self.process is None or self.process.returncode is not None:
            return
        try:
            self.process.terminate()       # SIGTERM -> ffmpeg writes trailers
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            log.warning("recorder.force_kill", name=self.name)
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()


def finalize_into(spool_file: Path, final_path: Path) -> int:
    """Atomically move a completed spool file to its final indexed location."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    size = spool_file.stat().st_size
    os.replace(spool_file, final_path)
    return size
