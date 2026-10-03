#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from phase8_benchmark_common import SystemSampler, build_result, utc_iso, write_result


LOG = logging.getLogger(__name__)


async def attempt(host: str, port: int, timeout: float) -> tuple[bool, float]:
    """Attempt one bounded TCP connection and measure completion latency.

    Args:
        host: TCP destination host.
        port: TCP destination port.
        timeout: Maximum connection wait in seconds.

    Returns:
        A tuple containing connection success and elapsed seconds.

    Raises:
        Exception: Unexpected programming/runtime failures propagate instead of
            being counted as ordinary network failures.
    """
    started = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return False, time.perf_counter() - started

    writer.close()
    try:
        await writer.wait_closed()
    except OSError as exc:
        # The connection itself succeeded; a peer reset during local close is
        # a cleanup condition, not a failed connection attempt.
        LOG.debug("tcp_reconnect_close_failed error=%s", exc.__class__.__name__)

    return True, time.perf_counter() - started


async def run(args) -> dict:
    """Run the configured reconnect workload and build benchmark evidence.

    Args:
        args: Parsed benchmark arguments containing workload and output options.

    Returns:
        A benchmark result dictionary compatible with the Phase 8 schema.

    Raises:
        Exception: Unexpected worker or sampling failures propagate so benchmark
            evidence is not silently produced from a broken run.
    """
    queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=max(1, args.concurrency * 4))
    latencies: list[float] = []
    ok = failed = 0
    lock = asyncio.Lock()

    async def worker(measure: bool):
        nonlocal ok, failed
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                success, elapsed = await attempt(args.host, args.port, args.timeout)
                async with lock:
                    if success:
                        ok += 1
                    else:
                        failed += 1
                    if measure:
                        latencies.append(elapsed)
            finally:
                queue.task_done()

    async def drive(count: int, measure: bool):
        workers = [asyncio.create_task(worker(measure)) for _ in range(max(1, args.concurrency))]
        for index in range(count):
            await queue.put(index)
        for _ in workers:
            await queue.put(None)
        await queue.join()
        await asyncio.gather(*workers)

    warmup_seconds = 0.0
    if args.warmup_attempts:
        warmup_started = time.perf_counter()
        await drive(args.warmup_attempts, False)
        warmup_seconds = time.perf_counter() - warmup_started
        ok = failed = 0

    sampler = SystemSampler()
    samples = []
    stop = asyncio.Event()

    async def sample_loop():
        while not stop.is_set():
            samples.append(sampler.sample())
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.1, args.sample_interval))
            except asyncio.TimeoutError:
                # Expected sample cadence timeout while the benchmark is active.
                continue

    sample_task = asyncio.create_task(sample_loop())
    started_at = utc_iso()
    started = time.perf_counter()
    await drive(args.attempts, True)
    duration = time.perf_counter() - started
    stop.set()
    await sample_task

    return build_result(
        workload_type="tcp-reconnect-storm",
        workload_config={
            "host": args.host,
            "port": args.port,
            "attempts": args.attempts,
            "concurrency": args.concurrency,
            "timeout_seconds": args.timeout,
        },
        started_at=started_at,
        duration_seconds=duration,
        warmup_seconds=warmup_seconds,
        operations_ok=ok,
        operations_failed=failed,
        latencies_seconds=latencies,
        samples=samples,
    )


def main():
    """Parse CLI arguments, execute the reconnect benchmark, and write results.

    Args:
        None. Arguments are read from the process command line.

    Returns:
        None after result files and the summary line are written.

    Raises:
        Exception: Propagates benchmark or output failures to produce a non-zero
            process exit instead of publishing invalid evidence.
    """
    parser = argparse.ArgumentParser(description="Phase 8 TCP reconnect benchmark")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8554)
    parser.add_argument("--attempts", type=int, default=10000)
    parser.add_argument("--warmup-attempts", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    write_result(result, json_path=args.output_json, csv_path=args.output_csv)
    print(
        f"ok={result['result']['operations_ok']} "
        f"failed={result['result']['operations_failed']} "
        f"rate={result['result']['throughput_ops_s']:.2f}/s"
    )


if __name__ == "__main__":
    main()
