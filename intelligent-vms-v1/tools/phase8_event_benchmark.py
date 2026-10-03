#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from datetime import datetime, timezone

import httpx

from phase8_benchmark_common import SystemSampler, build_result, utc_iso, write_result


EVENT_TYPES = ("motion", "human", "vehicle", "intrusion", "camera_offline", "tamper")


def make_event(index: int, cameras: int, run_id: str) -> dict:
    """Build one deterministic synthetic event for a benchmark run.

    Args:
        index: Event sequence index.
        cameras: Synthetic camera population size.
        run_id: Benchmark run identifier used in deterministic event IDs.

    Returns:
        Normalized synthetic VMS event dictionary.
    """
    camera_num = index % max(1, cameras)
    return {
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{index}")),
        "tenant_id": f"tenant-{camera_num % 10:02d}",
        "site_id": f"site-{camera_num % 500:04d}",
        "camera_id": f"cam-{camera_num:07d}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": EVENT_TYPES[index % len(EVENT_TYPES)],
        "object_type": (None, "person", "vehicle")[index % 3],
        "source": ("camera", "ai", "vms")[index % 3],
        "confidence": 0.90,
        "severity": ("info", "low", "medium", "high")[index % 4],
        "attributes": {"synthetic": True, "benchmark_run": run_id},
    }


async def _drive(
    *,
    client: httpx.AsyncClient,
    url: str,
    cameras: int,
    count: int,
    concurrency: int,
    run_id: str,
    measure: bool,
) -> tuple[int, int, list[float]]:
    queue: asyncio.Queue[tuple[int, dict] | None] = asyncio.Queue(maxsize=max(1, concurrency * 4))
    ok = failed = 0
    latencies: list[float] = []
    lock = asyncio.Lock()

    async def worker():
        nonlocal ok, failed
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                _, payload = item
                started = time.perf_counter()
                try:
                    response = await client.post(url, json=payload)
                    success = 200 <= response.status_code < 300
                except Exception:
                    success = False
                elapsed = time.perf_counter() - started
                async with lock:
                    if success:
                        ok += 1
                    else:
                        failed += 1
                    if measure:
                        latencies.append(elapsed)
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(max(1, concurrency))]
    for index in range(count):
        await queue.put((index, make_event(index, cameras, run_id)))
    for _ in workers:
        await queue.put(None)
    await queue.join()
    await asyncio.gather(*workers)
    return ok, failed, latencies


async def run(args) -> dict:
    """Run the Phase-8 HTTP event-ingest benchmark and collect evidence.

    Args:
        args: Parsed benchmark arguments.

    Returns:
        Benchmark result dictionary compatible with the Phase-8 result schema.
    """
    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    limits = httpx.Limits(
        max_connections=max(1, args.concurrency),
        max_keepalive_connections=max(1, args.concurrency),
    )
    url = args.api.rstrip("/") + args.path
    run_id = str(uuid.uuid4())

    async with httpx.AsyncClient(
        timeout=args.timeout,
        limits=limits,
        headers=headers,
    ) as client:
        warmup_seconds = 0.0
        if args.warmup_events > 0:
            warmup_started = time.perf_counter()
            await _drive(
                client=client,
                url=url,
                cameras=args.cameras,
                count=args.warmup_events,
                concurrency=args.concurrency,
                run_id=run_id + "-warmup",
                measure=False,
            )
            warmup_seconds = time.perf_counter() - warmup_started

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

        sample_task = asyncio.create_task(sample_loop())
        started_at = utc_iso()
        started = time.perf_counter()
        ok, failed, latencies = await _drive(
            client=client,
            url=url,
            cameras=args.cameras,
            count=args.events,
            concurrency=args.concurrency,
            run_id=run_id,
            measure=True,
        )
        duration = time.perf_counter() - started
        stop.set()
        await sample_task

    return build_result(
        workload_type="event-ingest-http",
        workload_config={
            "api": args.api,
            "path": args.path,
            "cameras": args.cameras,
            "events": args.events,
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
        extra_metrics={"requested_events": args.events},
    )


def main():
    """Parse event benchmark arguments, execute the run and write evidence."""
    parser = argparse.ArgumentParser(description="Phase 8 VMS event-ingest benchmark")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--path", default="/api/v1/events")
    parser.add_argument("--cameras", type=int, default=1000)
    parser.add_argument("--events", type=int, default=10000)
    parser.add_argument("--warmup-events", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--token", default="")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    write_result(result, json_path=args.output_json, csv_path=args.output_csv)
    print(
        f"accepted={result['result']['operations_ok']} "
        f"failed={result['result']['operations_failed']} "
        f"rate={result['result']['throughput_ops_s']:.2f}/s "
        f"p95_ms={result['result']['latency']['p95_ms']}"
    )


if __name__ == "__main__":
    main()
