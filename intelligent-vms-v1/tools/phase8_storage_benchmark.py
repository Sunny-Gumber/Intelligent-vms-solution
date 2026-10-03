#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
import time
from pathlib import Path

from phase8_benchmark_common import SystemSampler, build_result, utc_iso, write_result


def _write_stream(path: Path, total_bytes: int, chunk_bytes: int, fsync: bool) -> tuple[int, list[float]]:
    written = 0
    latencies = []
    block = b"\0" * chunk_bytes
    with path.open("wb", buffering=0) as handle:
        while written < total_bytes:
            payload = block[: min(chunk_bytes, total_bytes - written)]
            started = time.perf_counter()
            handle.write(payload)
            if fsync:
                os.fsync(handle.fileno())
            latencies.append(time.perf_counter() - started)
            written += len(payload)
    return written, latencies


async def run(args) -> dict:
    """Run the synthetic concurrent storage-write baseline.

    Args:
        args: Parsed storage benchmark arguments.

    Returns:
        Phase-8 benchmark result dictionary with write throughput/latency evidence.

    Raises:
        ValueError: If stream or chunk byte sizes are non-positive.
    """
    target_dir = Path(args.path)
    target_dir.mkdir(parents=True, exist_ok=True)
    bytes_per_stream = int(args.mib_per_stream * 1024 * 1024)
    chunk_bytes = int(args.chunk_mib * 1024 * 1024)
    if bytes_per_stream <= 0 or chunk_bytes <= 0:
        raise ValueError("mib-per-stream and chunk-mib must be positive")

    sampler = SystemSampler()
    samples = []
    stop = asyncio.Event()

    async def sample_loop():
        while not stop.is_set():
            samples.append(sampler.sample())
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.1, args.sample_interval))
            except asyncio.TimeoutError:
                pass

    paths = [
        target_dir / f"phase8-storage-{os.getpid()}-{index:04d}.bin"
        for index in range(args.streams)
    ]
    sample_task = asyncio.create_task(sample_loop())
    started_at = utc_iso()
    started = time.perf_counter()
    results = await asyncio.gather(
        *[
            asyncio.to_thread(
                _write_stream,
                path,
                bytes_per_stream,
                chunk_bytes,
                args.fsync,
            )
            for path in paths
        ]
    )
    duration = time.perf_counter() - started
    stop.set()
    await sample_task

    total_bytes = sum(item[0] for item in results)
    latencies = [latency for _, items in results for latency in items]
    if not args.keep_files:
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass

    result = build_result(
        workload_type="synthetic-storage-write",
        workload_config={
            "path": str(target_dir.resolve()),
            "streams": args.streams,
            "mib_per_stream": args.mib_per_stream,
            "chunk_mib": args.chunk_mib,
            "fsync_each_chunk": args.fsync,
        },
        started_at=started_at,
        duration_seconds=duration,
        warmup_seconds=0.0,
        operations_ok=len(latencies),
        operations_failed=0,
        latencies_seconds=latencies,
        samples=samples,
        storage_path=str(target_dir),
        extra_metrics={
            "bytes_written": total_bytes,
            "aggregate_write_mbps": (total_bytes * 8 / duration / 1_000_000) if duration > 0 else None,
            "aggregate_write_MBps": (total_bytes / duration / 1_000_000) if duration > 0 else None,
        },
    )
    return result


def main():
    """Parse storage benchmark arguments, execute baseline and write evidence."""
    parser = argparse.ArgumentParser(
        description="Phase 8 synthetic concurrent storage write baseline; not VMS recording certification"
    )
    parser.add_argument("--path", default=tempfile.gettempdir())
    parser.add_argument("--streams", type=int, default=4)
    parser.add_argument("--mib-per-stream", type=float, default=256)
    parser.add_argument("--chunk-mib", type=float, default=1)
    parser.add_argument("--fsync", action="store_true")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv")
    args = parser.parse_args()
    if args.streams < 1:
        raise SystemExit("streams must be >= 1")
    result = asyncio.run(run(args))
    write_result(result, json_path=args.output_json, csv_path=args.output_csv)
    print(
        f"bytes={result['result']['bytes_written']} "
        f"write_MBps={result['result']['aggregate_write_MBps']:.2f} "
        f"p95_chunk_ms={result['result']['latency']['p95_ms']}"
    )


if __name__ == "__main__":
    main()
