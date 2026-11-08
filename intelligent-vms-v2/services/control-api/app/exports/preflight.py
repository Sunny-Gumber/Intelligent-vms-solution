"""Export pre-flight checks.

Running out of disk half-way through an export is both a support call and a
risk of filling the volume recording depends on. The estimate is derived from
the real source files, never guessed.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    estimated_bytes: int
    free_bytes: int
    reason: str | None = None


def estimate_output_bytes(sources: list[Path], headroom: float = 1.25) -> int:
    total = 0
    for path in sources:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return int(total * max(1.0, headroom))


def preflight(sources: list[Path], destination_dir: Path,
              headroom: float = 1.25) -> PreflightResult:
    estimated = estimate_output_bytes(sources, headroom)
    destination_dir.mkdir(parents=True, exist_ok=True)
    try:
        usage = shutil.disk_usage(str(destination_dir))
    except OSError as exc:
        return PreflightResult(False, estimated, 0, f"export volume unreadable: {exc}")
    if usage.free < estimated:
        return PreflightResult(False, estimated, usage.free,
                               f"export needs about {estimated / 1e6:.1f} MB but only "
                               f"{usage.free / 1e6:.1f} MB is free on the export volume")
    return PreflightResult(True, estimated, usage.free)
