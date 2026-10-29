"""Deterministic validation and offline integration checks for forensic derivatives."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

import pytest


MODULE = Path(__file__).resolve().parents[1] / "services" / "forensic_worker" / "enhance.py"
SPEC = importlib.util.spec_from_file_location("forensic_enhance", MODULE)
assert SPEC and SPEC.loader
import types

module = types.ModuleType(SPEC.name)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)
Settings = module.Settings
EnhancementError = module.EnhancementError


def _write_png(path: Path) -> None:
    """Create a low-contrast but genuine RGB PNG without image-library dependencies."""
    width, height = 64, 48
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            value = 22 if x < 32 else 35
            if 10 <= x <= 20 and 10 <= y <= 30:
                value = 55
            row.extend([value, value, value])
        rows.append(row)

    def chunk(name: bytes, body: bytes) -> bytes:
        """Build a PNG chunk with its CRC."""
        payload = name + body
        return struct.pack(">I", len(body)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def test_filter_order_and_options() -> None:
    """Filters are fixed-order numeric arguments with no injected expressions."""
    settings = Settings(
        brightness=0.1,
        contrast=1.25,
        gamma=1.4,
        histogram_strength=0.2,
        denoise=2,
        sharpen=0.7,
        roi=(2, 4, 20, 16),
    )
    graph = module.filter_chain(settings, 64, 48)
    assert graph.startswith("crop=20:16:2:4,hqdn3d=2.000,histeq=strength=0.200:antibanding=none")
    assert "eq=brightness=0.1000:contrast=1.2500:gamma=1.4000" in graph
    assert graph.endswith("unsharp=5:5:0.700:5:5:0")


def test_default_filter_and_validation() -> None:
    """Identity settings are valid; NaN, infinity and malformed ROI are not."""
    assert module.filter_chain(Settings(), 64, 48) == "null"
    for invalid in (float("nan"), float("inf"), -0.21, 0.21):
        with pytest.raises(EnhancementError):
            Settings(brightness=invalid).validate()
    with pytest.raises(EnhancementError):
        Settings(roi=(1, 1, 0, 20)).validate()
    with pytest.raises(EnhancementError):
        module.filter_chain(Settings(roi=(50, 20, 20, 20)), 64, 48)


def test_cli_roi_parser() -> None:
    """CLI accepts precisely four numeric ROI coordinates."""
    assert module._parse_roi("1, 2, 30, 40") == (1, 2, 30, 40)
    with pytest.raises(Exception):
        module._parse_roi("1,2,3")
    with pytest.raises(Exception):
        module._parse_roi("x,2,3,4")


def test_local_only_paths_and_nonoverwrite(tmp_path: Path) -> None:
    """Reject network-like URLs, invalid suffixes, and clobbering originals."""
    source = tmp_path / "original.png"
    _write_png(source)
    with pytest.raises(EnhancementError, match="URLs"):
        module._safe_paths("https://bad.invalid/video.mp4", str(tmp_path / "out.mp4"))
    with pytest.raises(EnhancementError, match="must use"):
        module._safe_paths(str(source), str(tmp_path / "out.mp4"))
    with pytest.raises(EnhancementError, match="overwritten"):
        module._safe_paths(str(source), str(source))
    output = tmp_path / "out.png"
    output.write_bytes(b"existing")
    with pytest.raises(EnhancementError, match="already exists"):
        module._safe_paths(str(source), str(output))


def test_source_bounds_before_decode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Large inputs are rejected before invoking any media decoder."""
    source = tmp_path / "large.png"
    _write_png(source)
    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 1)
    with pytest.raises(EnhancementError, match="size"):
        module.probe_source(source)


def test_hash_streaming(tmp_path: Path) -> None:
    """Source SHA-256 matches a separately computed digest."""
    file = tmp_path / "source.bin"
    content = b"forensic" * 100000
    file.write_bytes(content)
    assert module.sha256_file(file) == hashlib.sha256(content).hexdigest()


@pytest.mark.skipif(not shutil.which("ffprobe") or not shutil.which("ffmpeg"), reason="FFmpeg tools not installed")
def test_image_derivative_is_hashed_and_source_preserved(tmp_path: Path) -> None:
    """Render a dark image to a lossless derivative with verified provenance."""
    source = tmp_path / "dark.png"
    output = tmp_path / "enhanced.png"
    _write_png(source)
    original = source.read_bytes()
    record = module.enhance(
        str(source), str(output),
        Settings(brightness=0.1, gamma=1.3, denoise=2, histogram_strength=0.15, roi=(8, 8, 32, 24)),
    )
    manifest = json.loads((tmp_path / "enhanced.png.forensic.json").read_text(encoding="utf-8"))
    assert original == source.read_bytes()
    assert record["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest["derivative_sha256"] == module.sha256_file(output)
    assert record["filter_graph"].startswith("crop=32:24:8:8")
    assert manifest["audio"] == "not included in derivative"
    assert output.stat().st_size > 0
    with pytest.raises(EnhancementError, match="already exists"):
        module.enhance(str(source), str(output), Settings())


@pytest.mark.skipif(not shutil.which("ffprobe") or not shutil.which("ffmpeg"), reason="FFmpeg tools not installed")
def test_short_video_derivative_is_valid_mp4(tmp_path: Path) -> None:
    """A short offline clip can be processed independently of recording services."""
    source_image = tmp_path / "seed.png"
    source_video = tmp_path / "original.mp4"
    output_video = tmp_path / "enhanced.mp4"
    _write_png(source_image)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-i", str(source_image), "-t", "0.5", "-r", "10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source_video),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    original_hash = module.sha256_file(source_video)
    record = module.enhance(str(source_video), str(output_video), Settings(gamma=1.2, sharpen=0.5))
    assert module.sha256_file(source_video) == original_hash
    assert record["source_sha256"] == original_hash
    assert module.probe_source(output_video)["is_image"] is False
    assert output_video.stat().st_size > 0


@pytest.mark.skipif(not shutil.which("ffprobe") or not shutil.which("ffmpeg"), reason="FFmpeg tools not installed")
def test_media_failure_cleans_temporary_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A render failure leaves no apparently completed output or provenance."""
    source = tmp_path / "original.png"
    output = tmp_path / "output.png"
    _write_png(source)
    real_run = module._run

    def fail_render(command: list[str], timeout: int):
        """Simulate a decoder/encoder failure after safe source probing."""
        if command[0] == "ffmpeg" and "-i" in command:
            raise EnhancementError("Synthetic media failure")
        return real_run(command, timeout)

    monkeypatch.setattr(module, "_run", fail_render)
    with pytest.raises(EnhancementError, match="Synthetic"):
        module.enhance(str(source), str(output), Settings())
    assert not output.exists()
    assert not (tmp_path / "output.png.forensic.json").exists()
    assert list(tmp_path.glob(".forensic-*")) == []
