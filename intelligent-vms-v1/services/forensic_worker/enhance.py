"""Bounded, offline CPU forensic derivatives for authorized recording exports.

This worker has no network endpoints and never opens camera streams. A future
VMS service must stage an already-authorized source copy before invoking it.
It is not a legal admissibility or object-identification certification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv"})
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_VIDEO_SECONDS = 120.0
MAX_PIXEL_COUNT = 3840 * 2160
PROBE_TIMEOUT_SECONDS = 20
PROCESS_TIMEOUT_SECONDS = 240
MANIFEST_SCHEMA = "vms-forensic-derivative-v1"


class EnhancementError(ValueError):
    """Expected validation or media-processing error, safe to show to users."""


@dataclass(frozen=True)
class Settings:
    """Validated, numeric-only filter configuration.

    Parameters:
        brightness: FFmpeg brightness offset in [-0.2, 0.2].
        contrast: Contrast multiplier in [0.5, 3.0].
        gamma: Approximate gamma in [0.4, 3.0].
        histogram_strength: Global histogram equalization in [0, 0.5].
        denoise: HQDN3D spatial luma strength in [0, 10].
        sharpen: Unsharp luma amount in [0, 2].
        roi: Optional x,y,width,height crop in original frame coordinates.
    """

    brightness: float = 0.0
    contrast: float = 1.0
    gamma: float = 1.0
    histogram_strength: float = 0.0
    denoise: float = 0.0
    sharpen: float = 0.0
    roi: tuple[int, int, int, int] | None = None

    def validate(self) -> None:
        """Reject out-of-range or non-finite filter values.

        Raises:
            EnhancementError: If any parameter is invalid.
        """
        ranges = {
            "brightness": (-0.2, 0.2),
            "contrast": (0.5, 3.0),
            "gamma": (0.4, 3.0),
            "histogram_strength": (0.0, 0.5),
            "denoise": (0.0, 10.0),
            "sharpen": (0.0, 2.0),
        }
        for name, (minimum, maximum) in ranges.items():
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise EnhancementError(f"{name} must be a finite number")
            if not minimum <= value <= maximum:
                raise EnhancementError(f"{name} must be between {minimum} and {maximum}")
        if self.roi is not None:
            if len(self.roi) != 4 or any(type(value) is not int for value in self.roi):
                raise EnhancementError("ROI must contain four integer coordinates")
            x, y, width, height = self.roi
            if x < 0 or y < 0 or width < 2 or height < 2:
                raise EnhancementError("ROI needs nonnegative x/y and width/height >= 2")


def sha256_file(path: Path) -> str:
    """Calculate a file digest without reading the entire file into memory.

    Args:
        path: Local file to hash.

    Returns:
        Hexadecimal SHA-256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a fixed-argument media command without a shell.

    Args:
        command: Executable and positional argument vector.
        timeout: Hard wall-clock limit in seconds.

    Returns:
        Completed subprocess result.

    Raises:
        EnhancementError: When a binary is absent, times out, or returns nonzero.
    """
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnhancementError("Required media tool unavailable or timed out") from exc
    if result.returncode != 0:
        # Media errors can echo sensitive file paths or input metadata.
        raise EnhancementError("Media tool rejected the input or processing options")
    return result


def probe_source(source: Path) -> dict[str, object]:
    """Inspect local media and reject unbounded or unsupported source content.

    Args:
        source: Existing local source file.

    Returns:
        Width, height, duration and image/video classification.

    Raises:
        EnhancementError: For invalid media or excessive resource requirements.
    """
    if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        raise EnhancementError("Source must be a local PNG/JPEG/MP4/MOV/MKV file")
    if source.stat().st_size == 0 or source.stat().st_size > MAX_SOURCE_BYTES:
        raise EnhancementError("Source size exceeds the allowed bounds")
    raw = _run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,width,height:format=duration", "-of", "json",
            str(source),
        ],
        PROBE_TIMEOUT_SECONDS,
    ).stdout
    try:
        payload = json.loads(raw)
        video = next(item for item in payload["streams"] if item["codec_type"] == "video")
        width, height = int(video["width"]), int(video["height"])
    except (ValueError, TypeError, KeyError, StopIteration, json.JSONDecodeError) as exc:
        raise EnhancementError("Source does not contain a supported video/image stream") from exc
    if width <= 0 or height <= 0 or width * height > MAX_PIXEL_COUNT:
        raise EnhancementError("Source dimensions exceed the configured pixel budget")
    is_image = source.suffix.lower() in IMAGE_EXTENSIONS
    duration = None
    if not is_image:
        try:
            duration = float(payload["format"]["duration"])
        except (ValueError, TypeError, KeyError) as exc:
            raise EnhancementError("Video duration is missing or invalid") from exc
        if not math.isfinite(duration) or not 0 < duration <= MAX_VIDEO_SECONDS:
            raise EnhancementError("Video duration exceeds the configured limit")
    return {"width": width, "height": height, "duration_seconds": duration, "is_image": is_image}


def filter_chain(settings: Settings, width: int, height: int) -> str:
    """Build a fixed, auditable FFmpeg filter chain from validated numeric settings.

    Args:
        settings: Validated enhancement configuration.
        width: Original image width.
        height: Original image height.

    Returns:
        Filter graph used for the derivative.

    Raises:
        EnhancementError: For invalid settings or out-of-frame ROI.
    """
    settings.validate()
    if width <= 0 or height <= 0:
        raise EnhancementError("Frame dimensions must be positive")
    filters = []
    if settings.roi:
        x, y, crop_width, crop_height = settings.roi
        if x + crop_width > width or y + crop_height > height:
            raise EnhancementError("ROI falls outside the original frame")
        filters.append(f"crop={crop_width}:{crop_height}:{x}:{y}")
    if settings.denoise:
        filters.append(f"hqdn3d={settings.denoise:.3f}")
    if settings.histogram_strength:
        filters.append(f"histeq=strength={settings.histogram_strength:.3f}:antibanding=none")
    if (settings.brightness, settings.contrast, settings.gamma) != (0.0, 1.0, 1.0):
        filters.append(
            f"eq=brightness={settings.brightness:.4f}:contrast={settings.contrast:.4f}:"
            f"gamma={settings.gamma:.4f}"
        )
    if settings.sharpen:
        filters.append(f"unsharp=5:5:{settings.sharpen:.3f}:5:5:0")
    return ",".join(filters) if filters else "null"


def _safe_paths(source_path: str, output_path: str) -> tuple[Path, Path, Path]:
    """Validate local-only input and non-overwriting destination paths.

    Args:
        source_path: Offline source supplied to the worker.
        output_path: New derivative filename.

    Returns:
        Source, derivative and manifest paths.

    Raises:
        EnhancementError: When paths violate the offline export contract.
    """
    if "://" in source_path or "://" in output_path:
        raise EnhancementError("URLs and network media sources are not accepted")
    try:
        source = Path(source_path).resolve(strict=True)
        output = Path(output_path).absolute()
        output.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise EnhancementError("Input file and output directory must exist locally") from exc
    if not source.is_file() or not output.parent.is_dir():
        raise EnhancementError("Input must be a file and destination must be a directory")
    is_image = source.suffix.lower() in IMAGE_EXTENSIONS
    expected = ".png" if is_image else ".mp4"
    if output.suffix.lower() != expected:
        raise EnhancementError(f"Derivative must use {expected} extension")
    manifest = Path(str(output) + ".forensic.json")
    if output.resolve() == source:
        raise EnhancementError("Original evidence cannot be overwritten")
    if any(path.exists() or path.is_symlink() for path in (output, manifest)):
        raise EnhancementError("Derivative or manifest already exists; refusing overwrite")
    return source, output, manifest


def enhance(source_path: str, output_path: str, settings: Settings) -> dict[str, object]:
    """Process an authorized local export into a bounded, hashed derivative.

    Args:
        source_path: Existing offline file, never an RTSP/network URL.
        output_path: New local PNG or MP4 path.
        settings: Pure filter settings.

    Returns:
        Forensic processing manifest. It is not a court admissibility claim.

    Raises:
        EnhancementError: On validation failure or media processing failure.
    """
    source, output, manifest = _safe_paths(source_path, output_path)
    settings.validate()
    media = probe_source(source)
    graph = filter_chain(settings, int(media["width"]), int(media["height"]))
    original_hash = sha256_file(source)
    version = _run(["ffmpeg", "-version"], PROBE_TIMEOUT_SECONDS).stdout.splitlines()[0]
    descriptor, temporary_name = tempfile.mkstemp(prefix=".forensic-", suffix=output.suffix, dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    descriptor, temporary_manifest_name = tempfile.mkstemp(prefix=".forensic-manifest-", dir=output.parent)
    os.close(descriptor)
    temporary_manifest = Path(temporary_manifest_name)
    succeeded = False
    try:
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1",
            "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-map_metadata", "-1", "-vf", graph,
        ]
        if media["is_image"]:
            command.extend(["-frames:v", "1", "-c:v", "png", "-pix_fmt", "rgb24", str(temporary)])
        else:
            command.extend([
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
            ])
        _run(command, PROCESS_TIMEOUT_SECONDS)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise EnhancementError("Processing produced no derivative")
        if sha256_file(source) != original_hash:
            raise EnhancementError("Source changed while processing; derivative discarded")
        record: dict[str, object] = {
            "schema": MANIFEST_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_filename": source.name,
            "source_sha256": original_hash,
            "derivative_filename": output.name,
            "derivative_sha256": sha256_file(temporary),
            "source_media": media,
            "settings": asdict(settings),
            "filter_graph": graph,
            "processor": version,
            "audio": "not included in derivative",
            "disclaimer": (
                "Enhanced interpretation only. Original data was preserved. "
                "Filters cannot recover details the sensor did not capture."
            ),
        }
        temporary_manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # A manifest write failure must not leave a misleading completed result.
        os.replace(temporary, output)
        os.replace(temporary_manifest, manifest)
        succeeded = True
        return record
    finally:
        temporary.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        if not succeeded:
            output.unlink(missing_ok=True)


def _parse_roi(value: str) -> tuple[int, int, int, int]:
    """Parse four comma-separated ROI integers for the CLI.

    Args:
        value: x,y,width,height expression.

    Returns:
        Four signed coordinates validated later.
    """
    try:
        coordinates = tuple(int(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI must be x,y,width,height") from exc
    if len(coordinates) != 4:
        raise argparse.ArgumentTypeError("ROI must be x,y,width,height")
    return coordinates


def main(argv: list[str] | None = None) -> int:
    """Run the offline command-line worker and return a process exit code.

    Args:
        argv: Optional arguments, defaults to command-line arguments.

    Returns:
        Zero on success and two for a handled failure.
    """
    parser = argparse.ArgumentParser(description="Intelligent VMS offline forensic image/video enhancement")
    parser.add_argument("--input", required=True, help="Existing authorized local export")
    parser.add_argument("--output", required=True, help="New PNG (images) or MP4 (clips)")
    parser.add_argument("--brightness", type=float, default=0.0)
    parser.add_argument("--contrast", type=float, default=1.0)
    parser.add_argument("--gamma", type=float, default=1.0)
    parser.add_argument("--histogram-strength", type=float, default=0.0)
    parser.add_argument("--denoise", type=float, default=0.0)
    parser.add_argument("--sharpen", type=float, default=0.0)
    parser.add_argument("--roi", type=_parse_roi, default=None)
    args = parser.parse_args(argv)
    try:
        result = enhance(
            args.input,
            args.output,
            Settings(
                brightness=args.brightness,
                contrast=args.contrast,
                gamma=args.gamma,
                histogram_strength=args.histogram_strength,
                denoise=args.denoise,
                sharpen=args.sharpen,
                roi=args.roi,
            ),
        )
    except EnhancementError as exc:
        print(f"Enhancement rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
