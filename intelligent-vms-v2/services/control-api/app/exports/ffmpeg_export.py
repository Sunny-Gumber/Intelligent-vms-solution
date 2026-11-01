"""FFmpeg invocation for clip export and snapshot extraction.

Design choices
--------------
* Multiple source segments are joined with the `concat` demuxer, performing a
  stream copy: an hour of H.265 exports in seconds and the evidence is
  bit-identical to what was recorded.
* `-ss` is placed AFTER `-i` (accurate seek). Placing it before would be
  faster but seeks to the nearest keyframe and can silently shift the clip by
  seconds — unacceptable for evidence.
* Cancellation is cooperative: terminate the process, delete the partial file,
  record the job as CANCELLED.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.logging_setup import get_logger, redact_text

log = get_logger("exports.ffmpeg")


@dataclass
class ExportResult:
    ok: bool
    cancelled: bool = False
    error: str | None = None
    output_bytes: int = 0


def write_concat_list(sources: list[Path], list_path: Path) -> Path:
    """Write an FFmpeg concat list with correct quoting.

    Our own paths never contain quotes (ids are hex), but imported files
    might, and a malformed list would read an unintended file.
    """
    lines = [
        f"file '{str(s.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
        for s in sources
    ]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


async def export_clip(
    *,
    sources: list[Path],
    output: Path,
    skip_seconds: float = 0.0,
    duration_seconds: float | None = None,
    transcode: bool = False,
    ffmpeg_path: str = "ffmpeg",
    cancel: asyncio.Event | None = None,
    on_progress: Callable[[float], Awaitable[None]] | None = None,
) -> ExportResult:
    if shutil.which(ffmpeg_path) is None:
        return ExportResult(False, error=f"{ffmpeg_path} not found on PATH")
    if not sources:
        return ExportResult(False, error="no source segments supplied")

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    list_path = output.parent / "sources.txt"
    write_concat_list(sources, list_path)

    args = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
    ]

    if skip_seconds > 0:
        args += ["-ss", f"{skip_seconds:.3f}"]  # accurate seek (after -i)
    if duration_seconds and duration_seconds > 0:
        args += ["-t", f"{duration_seconds:.3f}"]

    if transcode:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac"]
    else:
        args += ["-c", "copy"]

    # The working file is "<name>.part", whose extension FFmpeg cannot map to a
    # container, so the format is stated explicitly.
    container = {"mp4": "mp4", "mkv": "matroska", "mov": "mov"}.get(
        output.suffix.lstrip(".").lower(), "mp4"
    )
    args += [
        "-movflags",
        "+faststart",
        "-f",
        container,
        "-progress",
        "pipe:1",
        "-nostats",
        str(partial),
    ]

    log.info("export.ffmpeg_start", output=str(output), sources=len(sources), transcode=transcode)

    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return ExportResult(False, error=f"failed to start ffmpeg: {exc}")

    async def pump_progress() -> None:
        if process.stdout is None:
            return
        total = duration_seconds or 0.0
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("out_time_ms=") and total > 0 and on_progress:
                try:
                    micros = int(text.split("=", 1)[1])
                except ValueError:
                    continue
                await on_progress(min(0.99, (micros / 1_000_000) / total))

    async def watch_cancel() -> None:
        if cancel is None:
            return
        await cancel.wait()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()

    stderr_chunks: list[bytes] = []

    async def read_stderr() -> None:
        if process.stderr is None:
            return
        while True:
            chunk = await process.stderr.readline()
            if not chunk:
                return
            stderr_chunks.append(chunk)
            del stderr_chunks[:-20]

    tasks = [asyncio.create_task(pump_progress()), asyncio.create_task(read_stderr())]
    if cancel is not None:
        tasks.append(asyncio.create_task(watch_cancel()))

    returncode = await process.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    try:
        list_path.unlink()
    except OSError:
        pass

    if cancel is not None and cancel.is_set():
        _remove(partial)
        return ExportResult(False, cancelled=True)

    if returncode != 0:
        detail = redact_text(b"".join(stderr_chunks).decode("utf-8", "replace"))[:400]
        _remove(partial)
        return ExportResult(False, error=f"ffmpeg exited {returncode}: {detail}")

    if (
        not await asyncio.to_thread(partial.is_file)
        or (await asyncio.to_thread(partial.stat)).st_size == 0
    ):
        _remove(partial)
        return ExportResult(False, error="ffmpeg produced an empty file")

    partial.replace(output)
    size = (await asyncio.to_thread(output.stat)).st_size
    if on_progress:
        await on_progress(1.0)
    log.info("export.ffmpeg_done", output=str(output), bytes=size)
    return ExportResult(True, output_bytes=size)


async def extract_snapshot(
    *,
    source: Path,
    output: Path,
    offset_s: float = 0.0,
    ffmpeg_path: str = "ffmpeg",
    timeout_s: float = 30.0,
) -> bool:
    """Single JPEG frame from a recorded file (alarm thumbnails)."""
    if shutil.which(ffmpeg_path) is None:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{max(0.0, offset_s):.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(output),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        returncode = await asyncio.wait_for(process.wait(), timeout=timeout_s)
    except (TimeoutError, OSError):
        return False
    return (
        returncode == 0
        and await asyncio.to_thread(output.is_file)
        and (await asyncio.to_thread(output.stat)).st_size > 0
    )


async def grab_live_snapshot(
    *,
    source_url: str,
    output: Path,
    transport: str = "tcp",
    ffmpeg_path: str = "ffmpeg",
    timeout_s: float = 15.0,
) -> bool:
    """One frame directly from a camera (manual snapshot button)."""
    if shutil.which(ffmpeg_path) is None:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-rtsp_transport",
        transport,
        "-rw_timeout",
        str(int(timeout_s * 1_000_000)),
        "-i",
        source_url,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(output),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        returncode = await asyncio.wait_for(process.wait(), timeout=timeout_s + 5)
    except (TimeoutError, OSError):
        return False
    return (
        returncode == 0
        and await asyncio.to_thread(output.is_file)
        and (await asyncio.to_thread(output.stat)).st_size > 0
    )


def _remove(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
