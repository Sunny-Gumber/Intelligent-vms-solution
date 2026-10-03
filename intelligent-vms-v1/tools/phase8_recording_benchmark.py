#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from phase8_benchmark_common import SystemSampler, build_result, utc_iso, write_result


def directory_snapshot(root: Path) -> tuple[int, int]:
    """Count files and total bytes beneath a recording directory.

    Args:
        root: Directory tree to measure.

    Returns:
        Tuple of file count and total bytes; unreadable files are skipped.
    """
    files = 0
    total_bytes = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files += 1
        total_bytes += int(stat.st_size)
    return files, total_bytes


async def run(args) -> dict:
    """Measure recording-directory growth while collecting system samples.

    Args:
        args: Parsed benchmark arguments.

    Returns:
        Phase-8 benchmark result dictionary.

    Raises:
        ValueError: If the recording directory/duration is invalid.
    """
    root = Path(args.path)
    if not root.exists() or not root.is_dir():
        raise ValueError("recording path must exist and be a directory")
    if args.duration <= 0:
        raise ValueError("duration must be positive")

    before_files, before_bytes = directory_snapshot(root)
    sampler = SystemSampler()
    samples = []
    started_at = utc_iso()
    started = time.perf_counter()
    deadline = started + args.duration

    while True:
        samples.append(sampler.sample())
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        await asyncio.sleep(min(max(0.1, args.sample_interval), remaining))

    duration = time.perf_counter() - started
    after_files, after_bytes = directory_snapshot(root)
    delta_files = max(0, after_files - before_files)
    delta_bytes = max(0, after_bytes - before_bytes)

    return build_result(
        workload_type="recording-directory-growth",
        workload_config={
            "path": str(root.resolve()),
            "expected_streams": args.expected_streams,
            "expected_bitrate_kbps_per_stream": args.expected_bitrate_kbps,
            "duration_requested_seconds": args.duration,
        },
        started_at=started_at,
        duration_seconds=duration,
        warmup_seconds=0.0,
        operations_ok=delta_files,
        operations_failed=0,
        latencies_seconds=[],
        samples=samples,
        storage_path=str(root),
        extra_metrics={
            "files_before": before_files,
            "files_after": after_files,
            "new_files": delta_files,
            "bytes_before": before_bytes,
            "bytes_after": after_bytes,
            "bytes_growth": delta_bytes,
            "observed_recording_mbps": (
                delta_bytes * 8 / duration / 1_000_000 if duration > 0 else None
            ),
            "expected_aggregate_mbps": (
                args.expected_streams * args.expected_bitrate_kbps / 1000.0
                if args.expected_streams is not None
                and args.expected_bitrate_kbps is not None
                else None
            ),
        },
    )


def main():
    """Parse recording benchmark arguments, execute measurement and write evidence."""
    parser = argparse.ArgumentParser(
        description="Measure isolated benchmark recording-directory growth while VMS recording is active"
    )
    parser.add_argument("--path", required=True)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--expected-streams", type=int)
    parser.add_argument("--expected-bitrate-kbps", type=float)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv")
    args = parser.parse_args()
    if args.expected_streams is not None and args.expected_streams < 1:
        raise SystemExit("expected-streams must be >= 1")
    result = asyncio.run(run(args))
    write_result(result, json_path=args.output_json, csv_path=args.output_csv)
    print(
        f"new_files={result['result']['new_files']} "
        f"bytes_growth={result['result']['bytes_growth']} "
        f"observed_recording_mbps={result['result']['observed_recording_mbps']}"
    )


if __name__ == "__main__":
    main()
